"""
backtest.py — Historical data fetcher for backtesting.

Fetches:
  - 1-minute Binance candles for BTC price data
  - Real Polymarket market data via Gamma API (slug-based lookup)
  - Actual token prices and resolution outcomes

Used by compare_runs.py.
"""

import time
import requests


def fetch_historical_candles(symbol="BTCUSDT", interval="1m", hours=24):
    """
    Fetch historical 1-min candles from Binance.

    Returns list of candle dicts sorted by time.
    Binance limits to 1000 candles per request, so we paginate.
    """
    url = "https://api.binance.com/api/v3/klines"
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - (hours * 3600 * 1000)
    all_candles = []

    current_start = start_ms
    while current_start < end_ms:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": current_start,
            "endTime": end_ms,
            "limit": 1000,
        }
        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            raw = resp.json()
        except Exception as e:
            print(f"[backtest] Fetch error: {e}")
            time.sleep(1)
            continue

        if not raw:
            break

        for c in raw:
            all_candles.append({
                "open_time": c[0],
                "open": float(c[1]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4]),
                "volume": float(c[5]),
                "close_time": c[6],
            })

        # Move start to after the last candle
        current_start = raw[-1][6] + 1

        if len(raw) < 1000:
            break

        time.sleep(0.2)  # respect rate limits

    # Deduplicate by open_time
    seen = set()
    unique = []
    for c in all_candles:
        if c["open_time"] not in seen:
            seen.add(c["open_time"])
            unique.append(c)

    unique.sort(key=lambda x: x["open_time"])
    print(f"[backtest] Fetched {len(unique)} candles over {hours}h")
    return unique


def group_into_windows(candles):
    """
    Group 1-min candles into 5-minute windows aligned to epoch % 300.

    Returns list of dicts:
        {
            "window_ts": int (epoch seconds, divisible by 300),
            "open_price": float (first candle open),
            "close_price": float (last candle close),
            "candles": [list of 1-min candles in this window],
            "outcome": "UP" or "DOWN"
        }
    """
    windows = {}
    for c in candles:
        ts_sec = c["open_time"] // 1000
        window_ts = ts_sec - (ts_sec % 300)
        if window_ts not in windows:
            windows[window_ts] = []
        windows[window_ts].append(c)

    result = []
    for wts in sorted(windows.keys()):
        wc = sorted(windows[wts], key=lambda x: x["open_time"])
        if len(wc) < 3:
            continue  # skip incomplete windows
        open_price = wc[0]["open"]
        close_price = wc[-1]["close"]
        outcome = "UP" if close_price >= open_price else "DOWN"
        result.append({
            "window_ts": wts,
            "open_price": open_price,
            "close_price": close_price,
            "candles": wc,
            "outcome": outcome,
        })

    print(f"[backtest] Grouped into {len(result)} 5-min windows")
    return result


def fetch_polymarket_window(epoch_ts):
    """
    Fetch a single Polymarket BTC 5-min market by its epoch timestamp.

    Slug pattern: btc-updown-5m-{epoch}
    Returns dict with market data or None if not found.
    """
    slug = f"btc-updown-5m-{epoch_ts}"
    url = f"https://gamma-api.polymarket.com/events/slug/{slug}"
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json()

        # Extract market info
        markets = data.get("markets", [])
        if not markets:
            return None
        market = markets[0]

        outcome_prices = market.get("outcomePrices", "")
        if isinstance(outcome_prices, str):
            try:
                import json
                outcome_prices = json.loads(outcome_prices)
            except Exception:
                outcome_prices = []

        outcomes = market.get("outcomes", [])
        if isinstance(outcomes, str):
            try:
                import json
                outcomes = json.loads(outcomes)
            except Exception:
                outcomes = []

        clob_token_ids = market.get("clobTokenIds", "")
        if isinstance(clob_token_ids, str):
            try:
                import json
                clob_token_ids = json.loads(clob_token_ids)
            except Exception:
                clob_token_ids = []

        # Determine winner from outcomePrices (resolved = [1,0] or [0,1])
        winner = None
        if len(outcome_prices) == 2:
            try:
                p0, p1 = float(outcome_prices[0]), float(outcome_prices[1])
                if p0 >= 0.95:
                    winner = outcomes[0] if outcomes else "Up"
                elif p1 >= 0.95:
                    winner = outcomes[1] if outcomes else "Down"
            except (ValueError, TypeError):
                pass

        return {
            "slug": slug,
            "epoch_ts": epoch_ts,
            "condition_id": market.get("conditionId"),
            "clob_token_ids": clob_token_ids,
            "outcomes": outcomes,
            "outcome_prices": outcome_prices,
            "winner": winner,
            "resolved": winner is not None,
            "volume": market.get("volume"),
            "end_date": market.get("endDate") or data.get("endDate"),
        }
    except Exception as e:
        print(f"[backtest] Polymarket fetch error for {slug}: {e}")
        return None


def fetch_polymarket_history(hours=24):
    """
    Fetch resolved Polymarket BTC 5-min markets for the past N hours.

    Iterates through epoch timestamps (every 300s) and queries the Gamma API.
    Returns list of resolved market dicts.
    """
    now = int(time.time())
    start = now - (hours * 3600)

    # Align to 300-second boundaries
    start = start - (start % 300)

    resolved = []
    epoch = start
    while epoch < now - 300:  # skip the current/active window
        market = fetch_polymarket_window(epoch)
        if market and market["resolved"]:
            resolved.append(market)
        elif market and not market["resolved"]:
            pass  # market exists but not yet resolved
        # Rate limit: ~2 requests/sec
        time.sleep(0.5)
        epoch += 300

    print(f"[backtest] Fetched {len(resolved)} resolved Polymarket windows over {hours}h")
    return resolved


def _lerp(start, end, ratio):
    """Linear interpolation helper."""
    return start + (end - start) * ratio


def get_real_token_price():
    """
    Baseline token price for unresolved near-coinflip markets.

    Kept for compatibility with older code paths; real pricing should use the
    delta-based estimate_token_price() model below.
    """
    return 0.50


def estimate_token_price(window_open, current_price):
    """
    Estimate token cost from the absolute move vs the window open.

    Piecewise model from the bot build guide:
      delta < 0.005% -> $0.50
      delta ~ 0.02%  -> $0.55
      delta ~ 0.05%  -> $0.65
      delta ~ 0.10%  -> $0.80
      delta ~ 0.15%+ -> $0.92-$0.97
    """
    if not window_open or current_price is None:
        return 0.50

    delta_pct = abs(current_price - window_open) / window_open * 100

    if delta_pct < 0.005:
        price = 0.50
    elif delta_pct < 0.02:
        price = _lerp(0.50, 0.55, (delta_pct - 0.005) / 0.015)
    elif delta_pct < 0.05:
        price = _lerp(0.55, 0.65, (delta_pct - 0.02) / 0.03)
    elif delta_pct < 0.10:
        price = _lerp(0.65, 0.80, (delta_pct - 0.05) / 0.05)
    elif delta_pct < 0.15:
        price = _lerp(0.80, 0.92, (delta_pct - 0.10) / 0.05)
    else:
        capped = min(delta_pct, 0.25)
        price = _lerp(0.92, 0.97, (capped - 0.15) / 0.10)

    return round(min(max(price, 0.50), 0.97), 3)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Fetch historical BTC candles")
    parser.add_argument("--hours", type=int, default=24, help="Hours of history")
    args = parser.parse_args()

    candles = fetch_historical_candles(hours=args.hours)
    windows = group_into_windows(candles)

    up_count = sum(1 for w in windows if w["outcome"] == "UP")
    down_count = len(windows) - up_count
    print(f"UP: {up_count}, DOWN: {down_count}")
