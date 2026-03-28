"""
poll_midpoints.py — Record live midpoint prices for BTC 5-min markets.

Polls the CLOB /midpoint endpoint every N seconds for each active window,
building a CSV of real mid-window token pricing for backtesting.

Usage:
    python3 poll_midpoints.py [--interval 5] [--hours 1] [--output midpoints.csv]
"""

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

CLOB_HOST = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"


def get_current_window_ts():
    """Return the epoch timestamp of the current 5-min window."""
    now = int(time.time())
    return now - (now % 300)


def get_next_window_ts():
    """Return the epoch timestamp of the next 5-min window."""
    return get_current_window_ts() + 300


def find_market_tokens(epoch_ts):
    """Look up token IDs for a given window epoch."""
    slug = f"btc-updown-5m-{epoch_ts}"
    url = f"{GAMMA_API}/events/slug/{slug}"
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json()
        markets = data.get("markets", [])
        if not markets:
            return None
        market = markets[0]

        clob_token_ids = market.get("clobTokenIds", [])
        if isinstance(clob_token_ids, str):
            clob_token_ids = json.loads(clob_token_ids)

        outcomes = market.get("outcomes", [])
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)

        token_up = token_down = None
        for i, outcome in enumerate(outcomes):
            if outcome.upper() == "UP":
                token_up = clob_token_ids[i]
            elif outcome.upper() == "DOWN":
                token_down = clob_token_ids[i]

        if token_up and token_down:
            return {"slug": slug, "token_up": token_up, "token_down": token_down}
    except Exception as e:
        print(f"[poll] Error finding market {slug}: {e}")
    return None


def fetch_midpoint(token_id):
    """Fetch midpoint price from CLOB API."""
    try:
        resp = requests.get(
            f"{CLOB_HOST}/midpoint",
            params={"token_id": token_id},
            timeout=5,
        )
        if resp.status_code == 200:
            return float(resp.json().get("mid", 0))
    except Exception:
        pass
    return None


def fetch_btc_price():
    """Fetch current BTC price from Binance."""
    try:
        resp = requests.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": "BTCUSDT"},
            timeout=5,
        )
        if resp.status_code == 200:
            return float(resp.json()["price"])
    except Exception:
        pass
    return None


def main():
    parser = argparse.ArgumentParser(description="Poll midpoint prices for BTC 5-min markets")
    parser.add_argument("--interval", type=int, default=5, help="Seconds between polls")
    parser.add_argument("--hours", type=float, default=1, help="Hours to run")
    parser.add_argument("--output", type=str, default="midpoints.csv", help="Output CSV")
    args = parser.parse_args()

    end_time = time.time() + (args.hours * 3600)
    new_file = not os.path.exists(args.output)

    fieldnames = [
        "timestamp", "datetime", "window_epoch", "window_slug",
        "elapsed_in_window", "btc_price",
        "up_midpoint", "down_midpoint", "up_down_sum",
    ]

    f = open(args.output, "a", newline="")
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    if new_file:
        writer.writeheader()

    print(f"Polling midpoints every {args.interval}s for {args.hours}h → {args.output}")
    print(f"Press Ctrl+C to stop.\n")

    current_window = None
    market_tokens = None
    poll_count = 0

    try:
        while time.time() < end_time:
            now = time.time()
            window_ts = get_current_window_ts()

            # Refresh market tokens when window changes
            if window_ts != current_window:
                current_window = window_ts
                market_tokens = find_market_tokens(window_ts)
                if market_tokens:
                    print(f"\n--- New window: {market_tokens['slug']} ---")
                else:
                    print(f"\n--- Window {window_ts}: market not found ---")

            if not market_tokens:
                time.sleep(args.interval)
                continue

            # Fetch prices
            btc_price = fetch_btc_price()
            up_mid = fetch_midpoint(market_tokens["token_up"])
            down_mid = fetch_midpoint(market_tokens["token_down"])

            elapsed = now - window_ts
            dt_str = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

            row = {
                "timestamp": round(now, 2),
                "datetime": dt_str,
                "window_epoch": window_ts,
                "window_slug": market_tokens["slug"],
                "elapsed_in_window": round(elapsed, 1),
                "btc_price": btc_price,
                "up_midpoint": up_mid,
                "down_midpoint": down_mid,
                "up_down_sum": round(up_mid + down_mid, 4) if up_mid and down_mid else None,
            }

            writer.writerow(row)
            f.flush()
            poll_count += 1

            # Console output
            up_str = f"{up_mid:.3f}" if up_mid else "N/A"
            down_str = f"{down_mid:.3f}" if down_mid else "N/A"
            btc_str = f"${btc_price:,.2f}" if btc_price else "N/A"
            print(
                f"  [{dt_str}] +{elapsed:5.1f}s | "
                f"BTC={btc_str} | Up={up_str} Down={down_str} | "
                f"#{poll_count}"
            )

            time.sleep(args.interval)

    except KeyboardInterrupt:
        print(f"\n\nStopped. {poll_count} data points saved to {args.output}")
    finally:
        f.close()


if __name__ == "__main__":
    main()
