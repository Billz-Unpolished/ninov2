"""
fetch_poly_history.py — Fetch 24h of resolved BTC 5-min markets from Polymarket.

Queries the Gamma API for each 5-min window slug (btc-updown-5m-{epoch})
and saves results to CSV for verification before running backtest.

Usage:
    python3 fetch_poly_history.py [--hours 24] [--output poly_24h.csv]
"""

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone

import requests

GAMMA_API = "https://gamma-api.polymarket.com"


def fetch_window(epoch_ts):
    """Fetch a single Polymarket BTC 5-min market by epoch timestamp."""
    slug = f"btc-updown-5m-{epoch_ts}"
    url = f"{GAMMA_API}/events/slug/{slug}"

    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            return {"epoch": epoch_ts, "slug": slug, "status": "not_found"}

        data = resp.json()
        markets = data.get("markets", [])
        if not markets:
            return {"epoch": epoch_ts, "slug": slug, "status": "no_markets"}

        market = markets[0]

        # Parse fields that may be JSON strings
        outcome_prices = market.get("outcomePrices", [])
        if isinstance(outcome_prices, str):
            outcome_prices = json.loads(outcome_prices)

        outcomes = market.get("outcomes", [])
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)

        clob_token_ids = market.get("clobTokenIds", [])
        if isinstance(clob_token_ids, str):
            clob_token_ids = json.loads(clob_token_ids)

        # Determine resolution
        winner = None
        resolved = False
        if len(outcome_prices) == 2:
            try:
                p0, p1 = float(outcome_prices[0]), float(outcome_prices[1])
                if p0 >= 0.95:
                    winner = outcomes[0] if outcomes else "Up"
                    resolved = True
                elif p1 >= 0.95:
                    winner = outcomes[1] if outcomes else "Down"
                    resolved = True
                else:
                    # Market still active or prices not settled
                    winner = f"Up={p0}/Down={p1}"
            except (ValueError, TypeError):
                pass

        return {
            "epoch": epoch_ts,
            "slug": slug,
            "status": "resolved" if resolved else "active",
            "title": market.get("question", data.get("title", "")),
            "start_date": data.get("startDate", ""),
            "end_date": market.get("endDate", data.get("endDate", "")),
            "outcomes": "/".join(outcomes) if outcomes else "",
            "outcome_prices": "/".join(str(p) for p in outcome_prices) if outcome_prices else "",
            "winner": winner,
            "resolved": resolved,
            "volume": market.get("volume", ""),
            "condition_id": market.get("conditionId", ""),
            "token_id_up": clob_token_ids[0] if len(clob_token_ids) > 0 else "",
            "token_id_down": clob_token_ids[1] if len(clob_token_ids) > 1 else "",
        }

    except Exception as e:
        return {"epoch": epoch_ts, "slug": slug, "status": f"error: {e}"}


def main():
    parser = argparse.ArgumentParser(description="Fetch Polymarket BTC 5-min history")
    parser.add_argument("--hours", type=int, default=24, help="Hours of history")
    parser.add_argument("--output", type=str, default="poly_24h.csv", help="Output CSV file")
    args = parser.parse_args()

    now = int(time.time())
    start = now - (args.hours * 3600)
    start = start - (start % 300)  # align to 300s boundary

    total_windows = (now - start) // 300
    print(f"Fetching {total_windows} windows ({args.hours}h) from Polymarket...")
    print(f"Time range: {datetime.fromtimestamp(start, tz=timezone.utc)} → {datetime.fromtimestamp(now, tz=timezone.utc)}")
    print()

    results = []
    epoch = start
    count = 0

    while epoch < now:
        count += 1
        dt = datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        sys.stdout.write(f"\r  [{count}/{total_windows}] {dt} — slug: btc-updown-5m-{epoch}")
        sys.stdout.flush()

        row = fetch_window(epoch)
        row["datetime"] = dt
        results.append(row)

        # Rate limit: ~2 req/sec to be safe
        time.sleep(0.5)
        epoch += 300

    print(f"\n\nDone. Fetched {len(results)} windows.")

    # Write CSV
    fieldnames = [
        "datetime", "epoch", "slug", "status", "title",
        "start_date", "end_date", "outcomes", "outcome_prices",
        "winner", "resolved", "volume", "condition_id",
        "token_id_up", "token_id_down",
    ]

    with open(args.output, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)

    # Summary stats
    resolved_count = sum(1 for r in results if r.get("resolved"))
    active_count = sum(1 for r in results if r.get("status") == "active")
    not_found = sum(1 for r in results if r.get("status") == "not_found")
    errors = sum(1 for r in results if str(r.get("status", "")).startswith("error"))
    up_wins = sum(1 for r in results if r.get("winner") == "Up")
    down_wins = sum(1 for r in results if r.get("winner") == "Down")

    print(f"\nSaved to: {args.output}")
    print(f"\n--- Summary ---")
    print(f"  Total windows:  {len(results)}")
    print(f"  Resolved:       {resolved_count}")
    print(f"  Still active:   {active_count}")
    print(f"  Not found:      {not_found}")
    print(f"  Errors:         {errors}")
    print(f"  Up wins:        {up_wins}")
    print(f"  Down wins:      {down_wins}")
    if up_wins + down_wins > 0:
        print(f"  Up %:           {up_wins / (up_wins + down_wins) * 100:.1f}%")


if __name__ == "__main__":
    main()
