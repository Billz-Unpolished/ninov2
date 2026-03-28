"""
bot.py — Main Polymarket BTC 5-minute Up/Down Trading Bot.

Lifecycle per 5-min window:
  1. Detect next window open (epoch % 300 == 0)
  2. Record window_open_price from Binance
  3. Poll BTC price every 2s, accumulate tick data
  4. At T+180s (configurable), run strategy.analyze()
  5. If confidence >= threshold, place order via CLOB
  6. Wait for window close, log outcome
  7. Repeat

Modes:
  - safe:   min_bet=$1, requires confidence >= 0.60
  - normal: standard Kelly sizing, confidence >= 0.40
  - degen:  aggressive Kelly, confidence >= 0.25

Usage:
    python bot.py [--mode safe] [--dry-run] [--once]
"""

import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

from strategy import analyze, fetch_candles, fetch_current_price
from backtest import estimate_token_price

load_dotenv()

# ─── Configuration ───────────────────────────────────────────────────────────

MODE_CONFIGS = {
    "safe": {
        "min_bet": 1.0,
        "max_bet": 5.0,
        "confidence_threshold": 0.60,
        "min_score": 3.0,
        "kelly_fraction": 0.10,
        "entry_delay_s": 200,  # wait longer for more data
    },
    "normal": {
        "min_bet": 1.0,
        "max_bet": 20.0,
        "confidence_threshold": 0.40,
        "min_score": 2.0,
        "kelly_fraction": 0.25,
        "entry_delay_s": 180,
    },
    "degen": {
        "min_bet": 1.0,
        "max_bet": 50.0,
        "confidence_threshold": 0.25,
        "min_score": 1.5,
        "kelly_fraction": 0.50,
        "entry_delay_s": 150,
    },
}

POLYMARKET_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137

# Slug pattern for BTC 5-min markets: btc-updown-5m-{epoch_ts}
# Gamma API: https://gamma-api.polymarket.com/events/slug/btc-updown-5m-{epoch}
GAMMA_API = "https://gamma-api.polymarket.com"

LOG_FILE = "bot_log.jsonl"


# ─── Helpers ─────────────────────────────────────────────────────────────────

def log_event(event_type, data):
    """Append a JSON log line. Flushes stdout for Railway/Docker log capture."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event_type,
        **data,
    }
    print(f"[{entry['ts']}] {event_type}: {json.dumps(data, default=str)}", flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        pass


def get_next_window_ts():
    """Return the epoch timestamp of the next 5-min boundary."""
    now = int(time.time())
    return now - (now % 300) + 300


def wait_until(target_ts):
    """Sleep until target epoch timestamp."""
    while True:
        remaining = target_ts - time.time()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 1.0))


def kelly_bet_size(bankroll, confidence, token_price, config):
    """
    Half-Kelly sizing.

    For a binary market paying $1:
      Edge = confidence - token_price
      Kelly fraction = edge / (1 - token_price)  [simplified for binary]
      Bet = bankroll * kelly_fraction * config_fraction
    """
    edge = confidence - token_price
    if edge <= 0:
        return 0

    odds_fraction = 1 - token_price
    if odds_fraction <= 0:
        return 0

    kelly = edge / odds_fraction
    raw_bet = bankroll * kelly * config["kelly_fraction"]
    bet = max(config["min_bet"], min(raw_bet, config["max_bet"], bankroll * 0.5))
    return round(bet, 2)


# ─── Midpoint Pricing ──────────────────────────────────────────────────────

def fetch_midpoint(token_id):
    """
    Fetch live midpoint price for a token from the CLOB API.

    GET https://clob.polymarket.com/midpoint?token_id=<id>
    Returns: float price (e.g. 0.515) or None on error.
    """
    url = f"{POLYMARKET_HOST}/midpoint"
    try:
        resp = requests.get(url, params={"token_id": token_id}, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            mid = float(data.get("mid", 0))
            return mid
    except Exception as e:
        log_event("midpoint_error", {"token_id": token_id[:20] + "...", "error": str(e)})
    return None


def fetch_both_midpoints(token_id_up, token_id_down):
    """
    Fetch midpoint prices for both Up and Down tokens.

    Returns (up_mid, down_mid) or (None, None) on error.
    """
    up_mid = fetch_midpoint(token_id_up)
    down_mid = fetch_midpoint(token_id_down)
    return up_mid, down_mid


# ─── Market Discovery ───────────────────────────────────────────────────────

def find_btc_5min_market(window_ts):
    """
    Find the Polymarket BTC 5-min market for a specific window.

    Slug pattern: btc-updown-5m-{epoch_ts}
    Uses the Gamma API to look up market details.

    Returns (condition_id, token_id_up, token_id_down) or None.
    """
    slug = f"btc-updown-5m-{window_ts}"
    url = f"{GAMMA_API}/events/slug/{slug}"

    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            log_event("market_not_found", {"slug": slug, "status": resp.status_code})
            return None

        data = resp.json()
        markets = data.get("markets", [])
        if not markets:
            log_event("market_no_markets", {"slug": slug})
            return None

        market = markets[0]
        condition_id = market.get("conditionId")

        # Parse clobTokenIds — may be JSON string or list
        clob_token_ids = market.get("clobTokenIds", [])
        if isinstance(clob_token_ids, str):
            import json as _json
            clob_token_ids = _json.loads(clob_token_ids)

        # Parse outcomes
        outcomes = market.get("outcomes", [])
        if isinstance(outcomes, str):
            import json as _json
            outcomes = _json.loads(outcomes)

        if len(clob_token_ids) < 2 or len(outcomes) < 2:
            log_event("market_incomplete", {"slug": slug})
            return None

        # Map outcomes to token IDs
        token_up = None
        token_down = None
        for i, outcome in enumerate(outcomes):
            if outcome.upper() == "UP":
                token_up = clob_token_ids[i]
            elif outcome.upper() == "DOWN":
                token_down = clob_token_ids[i]

        if condition_id and token_up and token_down:
            log_event("market_found", {
                "slug": slug,
                "condition_id": condition_id[:20] + "...",
                "token_up": token_up[:20] + "...",
                "token_down": token_down[:20] + "...",
            })
            return condition_id, token_up, token_down

        log_event("market_parse_error", {"slug": slug, "outcomes": outcomes})
        return None

    except Exception as e:
        log_event("market_search_error", {"slug": slug, "error": str(e)})
        return None


# ─── Order Placement ─────────────────────────────────────────────────────────

def place_order(client, token_id, side, amount, price, dry_run=False):
    """
    Place a limit order on Polymarket CLOB.

    side: "BUY"
    amount: number of shares (= dollar amount / price for $1 binary)
    price: price per share (0.01 to 0.99)
    """
    if dry_run:
        log_event("dry_run_order", {
            "token_id": token_id[:16] + "...",
            "side": side,
            "amount": amount,
            "price": price,
        })
        return {"dry_run": True, "status": "simulated"}

    try:
        from py_clob_client.order_builder.constants import BUY
        from py_clob_client.clob_types import OrderArgs, OrderType

        order_args = OrderArgs(
            price=price,
            size=amount,
            side=BUY,
            token_id=token_id,
        )
        signed_order = client.create_order(order_args)
        result = client.post_order(signed_order)
        log_event("order_placed", {"result": str(result)})
        return result
    except Exception as e:
        log_event("order_error", {"error": str(e), "traceback": traceback.format_exc()})
        return None


# ─── Main Loop ───────────────────────────────────────────────────────────────

def init_clob_client():
    """Initialize the Polymarket CLOB client."""
    pk = os.getenv("POLY_PRIVATE_KEY")
    api_key = os.getenv("POLY_API_KEY")
    api_secret = os.getenv("POLY_API_SECRET")
    api_passphrase = os.getenv("POLY_API_PASSPHRASE")
    funder = os.getenv("POLY_FUNDER_ADDRESS")

    if not pk:
        print("ERROR: POLY_PRIVATE_KEY not set in .env")
        print("Run: python setup_creds.py")
        sys.exit(1)

    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
    except ImportError:
        print("ERROR: py-clob-client not installed. Run: pip install py-clob-client==0.34.5")
        sys.exit(1)

    creds = ApiCreds(
        api_key=api_key,
        api_secret=api_secret,
        api_passphrase=api_passphrase,
    )

    client = ClobClient(
        POLYMARKET_HOST,
        key=pk,
        chain_id=CHAIN_ID,
        creds=creds,
        signature_type=int(os.getenv("POLY_SIGNATURE_TYPE", "1")),
        funder=funder,
    )

    return client


def fetch_live_bankroll(client):
    """
    Fetch actual USDC balance from Polymarket.

    Returns balance in dollars (float) or None on error.
    """
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams
        params = BalanceAllowanceParams(asset_type="COLLATERAL", signature_type=1)
        result = client.get_balance_allowance(params)
        raw = int(result.get("balance", 0))
        balance = raw / 1_000_000  # USDC has 6 decimals
        return balance
    except Exception as e:
        log_event("balance_fetch_error", {"error": str(e)})
        return None


def run_window(client, config, bankroll, dry_run=False):
    """
    Execute one 5-minute trading window.

    Returns (new_bankroll, trade_record).
    """
    window_ts = get_next_window_ts()
    window_end_ts = window_ts + 300

    log_event("waiting_for_window", {
        "window_ts": window_ts,
        "window_utc": datetime.fromtimestamp(window_ts, tz=timezone.utc).isoformat(),
        "seconds_until": round(window_ts - time.time(), 1),
    })

    # Wait for window open
    wait_until(window_ts)

    # Record opening price
    window_open_price = fetch_current_price()
    if window_open_price is None:
        log_event("error", {"msg": "Could not fetch opening price"})
        return bankroll, None

    log_event("window_open", {
        "window_ts": window_ts,
        "open_price": window_open_price,
        "bankroll": bankroll,
    })

    # Find the active market using slug: btc-updown-5m-{epoch}
    # Always look up — needed for live midpoint pricing even in dry-run
    market_info = find_btc_5min_market(window_ts)

    # Poll BTC prices every 2s + token midpoints every 10s
    tick_prices = []
    midpoint_history = []  # [(timestamp, up_mid, down_mid)]
    entry_time = window_ts + config["entry_delay_s"]
    last_midpoint_poll = 0

    while time.time() < entry_time:
        price = fetch_current_price()
        if price:
            tick_prices.append((time.time(), price))

        # Poll midpoints every 10s (less aggressive than BTC price)
        now = time.time()
        if market_info and now - last_midpoint_poll >= 10:
            _, token_up, token_down = market_info
            up_mid, down_mid = fetch_both_midpoints(token_up, token_down)
            if up_mid is not None and down_mid is not None:
                midpoint_history.append((now, up_mid, down_mid))
                log_event("midpoint_poll", {
                    "up": up_mid, "down": down_mid,
                    "elapsed": round(now - window_ts, 1),
                })
            last_midpoint_poll = now

        time.sleep(2)

    # Fetch candles and run strategy
    candles = fetch_candles(limit=30)
    current_price = fetch_current_price() or (tick_prices[-1][1] if tick_prices else None)

    if current_price is None:
        log_event("error", {"msg": "Could not fetch current price at entry time"})
        return bankroll, None

    score, confidence, details = analyze(
        candles=candles,
        window_open_price=window_open_price,
        current_price=current_price,
        tick_prices=tick_prices,
    )

    direction = "UP" if score > 0 else ("DOWN" if score < 0 else "SKIP")

    log_event("analysis", {
        "score": score,
        "confidence": confidence,
        "direction": direction,
        "current_price": current_price,
        "delta_pct": round((current_price - window_open_price) / window_open_price * 100, 6),
        "details": details,
    })

    # Decision: bet or skip?
    should_bet = (
        abs(score) >= config["min_score"]
        and confidence >= config["confidence_threshold"]
        and direction != "SKIP"
    )

    trade_record = {
        "window_ts": window_ts,
        "open_price": window_open_price,
        "entry_price": current_price,
        "score": score,
        "confidence": confidence,
        "direction": direction,
        "bet": False,
        "amount": 0,
        "token_cost": 0,
        "pnl": 0,
        "outcome": None,
    }

    if not should_bet:
        log_event("skip", {"reason": "below threshold", "score": score, "confidence": confidence})
        # Still wait for window end to log outcome
        wait_until(window_end_ts + 5)
        close_price = fetch_current_price()
        if close_price:
            actual = "UP" if close_price >= window_open_price else "DOWN"
            trade_record["outcome"] = actual
            log_event("window_close_skip", {"actual": actual, "close_price": close_price})
        return bankroll, trade_record

    # Fetch LIVE midpoint price from Polymarket orderbook
    token_price = None
    if market_info:
        condition_id, token_up, token_down = market_info
        target_token = token_up if direction == "UP" else token_down
        token_price = fetch_midpoint(target_token)
        if token_price:
            log_event("live_midpoint", {
                "direction": direction,
                "midpoint": token_price,
            })

    # Fallback to estimated price if midpoint unavailable
    if token_price is None or token_price <= 0:
        token_price = estimate_token_price(window_open_price, current_price)
        log_event("midpoint_fallback", {"estimated_price": token_price})

    bet_amount = kelly_bet_size(bankroll, confidence, token_price, config)

    if bet_amount < config["min_bet"]:
        log_event("skip", {"reason": "bet too small", "calculated": bet_amount})
        wait_until(window_end_ts + 5)
        close_price = fetch_current_price()
        if close_price:
            trade_record["outcome"] = "UP" if close_price >= window_open_price else "DOWN"
        return bankroll, trade_record

    trade_record["bet"] = True
    trade_record["amount"] = bet_amount
    trade_record["token_cost"] = token_price

    log_event("placing_bet", {
        "direction": direction,
        "amount": bet_amount,
        "token_price": token_price,
        "bankroll_before": bankroll,
    })

    # Place the order
    if market_info and not dry_run:
        condition_id, token_up, token_down = market_info
        token_id = token_up if direction == "UP" else token_down
        shares = round(bet_amount / token_price, 2)
        result = place_order(client, token_id, "BUY", shares, token_price, dry_run=dry_run)
        log_event("order_result", {"result": str(result)})
    else:
        log_event("dry_run_bet", {
            "direction": direction,
            "amount": bet_amount,
            "token_price": token_price,
        })

    # Wait for window close
    wait_until(window_end_ts + 5)

    # Check outcome
    close_price = fetch_current_price()
    if close_price is None:
        log_event("error", {"msg": "Could not fetch closing price"})
        return bankroll, trade_record

    actual = "UP" if close_price >= window_open_price else "DOWN"
    correct = direction == actual
    trade_record["outcome"] = actual

    if correct:
        pnl = (1.0 - token_price) * bet_amount
        trade_record["pnl"] = round(pnl, 4)
        bankroll += pnl
        log_event("win", {
            "direction": direction,
            "actual": actual,
            "pnl": pnl,
            "bankroll": round(bankroll, 4),
        })
    else:
        pnl = -token_price * bet_amount
        trade_record["pnl"] = round(pnl, 4)
        bankroll += pnl
        log_event("loss", {
            "direction": direction,
            "actual": actual,
            "pnl": pnl,
            "bankroll": round(bankroll, 4),
        })

    return bankroll, trade_record


def main():
    parser = argparse.ArgumentParser(description="Polymarket BTC 5-min Trading Bot")
    parser.add_argument("--mode", choices=["safe", "normal", "degen"], default=None,
                        help="Trading mode (overrides BOT_MODE env var)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run without placing real orders")
    parser.add_argument("--once", action="store_true",
                        help="Run one window then exit")
    parser.add_argument("--bankroll", type=float, default=None,
                        help="Starting bankroll (overrides STARTING_BANKROLL env var)")
    args = parser.parse_args()

    mode = args.mode or os.getenv("BOT_MODE", "safe")
    config = MODE_CONFIGS[mode]

    # Initialize CLOB client
    client = None
    if not args.dry_run:
        client = init_clob_client()
        log_event("client_initialized", {"mode": mode})
    else:
        log_event("dry_run_mode", {"mode": mode})

    # Auto-fetch live balance if no --bankroll override
    if args.bankroll:
        bankroll = args.bankroll
    elif client:
        live_balance = fetch_live_bankroll(client)
        if live_balance is not None:
            bankroll = live_balance
            log_event("live_balance_fetched", {"balance": bankroll})
        else:
            bankroll = float(os.getenv("STARTING_BANKROLL", "1.0"))
            log_event("balance_fallback", {"bankroll": bankroll})
    else:
        bankroll = float(os.getenv("STARTING_BANKROLL", "1.0"))

    print("=" * 60, flush=True)
    print("  POLYMARKET BTC 5-MIN TRADING BOT", flush=True)
    print("=" * 60, flush=True)
    print(f"  Mode:       {mode}", flush=True)
    print(f"  Bankroll:   ${bankroll:.2f} (live balance)", flush=True)
    print(f"  Dry run:    {args.dry_run}", flush=True)
    print(f"  Min bet:    ${config['min_bet']:.2f}", flush=True)
    print(f"  Max bet:    ${config['max_bet']:.2f}", flush=True)
    print(f"  Conf thres: {config['confidence_threshold']:.2f}", flush=True)
    print(f"  Min score:  {config['min_score']:.1f}", flush=True)
    print(f"  Entry delay:{config['entry_delay_s']}s")
    print("=" * 60)

    # Trading loop
    trade_count = 0
    wins = 0
    losses = 0

    try:
        while True:
            bankroll, trade = run_window(client, config, bankroll, dry_run=args.dry_run)

            if trade:
                trade_count += 1
                if trade.get("bet") and trade.get("pnl", 0) > 0:
                    wins += 1
                elif trade.get("bet") and trade.get("pnl", 0) < 0:
                    losses += 1

                log_event("session_stats", {
                    "trade_count": trade_count,
                    "wins": wins,
                    "losses": losses,
                    "win_rate": f"{wins / (wins + losses) * 100:.1f}%" if (wins + losses) > 0 else "N/A",
                    "bankroll": round(bankroll, 4),
                })

            if bankroll <= 0:
                log_event("bankrupt", {"final_bankroll": bankroll})
                print("\nBANKROLL DEPLETED. Stopping.")
                break

            if args.once:
                print("\n--once flag set. Exiting after one window.")
                break

    except KeyboardInterrupt:
        print("\n\nBot stopped by user.")
        log_event("stopped", {
            "bankroll": round(bankroll, 4),
            "trades": trade_count,
            "wins": wins,
            "losses": losses,
        })

    print(f"\nFinal bankroll: ${bankroll:.4f}")
    print(f"Trades: {trade_count} | Wins: {wins} | Losses: {losses}")


if __name__ == "__main__":
    main()
