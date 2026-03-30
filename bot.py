"""
bot.py — Main Polymarket BTC 5-minute Up/Down Trading Bot.

Live behavior follows a late snipe model:
  1. Wait for the next 5-minute BTC window to open
  2. Record the opening BTC price and active Polymarket market
  3. Poll BTC every 2s through the full window
  4. Starting at T-10s, run repeated TA checks every 2s
  5. Track the strongest signal and fire on spike/confidence/deadline
  6. Place a FOK buy, retrying until close, then fall back to GTC at $0.95
  7. Wait for close, score the outcome, and update bankroll
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
from storage import insert_event

load_dotenv()

# ─── Configuration ───────────────────────────────────────────────────────────

MODE_CONFIGS = {
    "safe": {
        "display_name": "safe",
        "min_bet": 1.0,
        "confidence_threshold": 0.30,
        "bet_style": "fractional",
        "bankroll_fraction": 0.25,
        "snipe_start_s": 10,
        "hard_deadline_s": 5,
    },
    "aggressive": {
        "display_name": "aggressive",
        "min_bet": 1.0,
        "confidence_threshold": 0.20,
        "bet_style": "profits_only",
        "bankroll_fraction": 1.0,
        "snipe_start_s": 10,
        "hard_deadline_s": 5,
    },
    "normal": {
        "display_name": "aggressive",
        "min_bet": 1.0,
        "confidence_threshold": 0.20,
        "bet_style": "profits_only",
        "bankroll_fraction": 1.0,
        "snipe_start_s": 10,
        "hard_deadline_s": 5,
    },
    "degen": {
        "display_name": "degen",
        "min_bet": 1.0,
        "confidence_threshold": 0.0,
        "bet_style": "all_in",
        "bankroll_fraction": 1.0,
        "snipe_start_s": 10,
        "hard_deadline_s": 5,
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
    try:
        insert_event(entry)
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


def calculate_bet_size(bankroll, starting_bankroll, config):
    """Mode-based bet sizing from the build guide."""
    style = config["bet_style"]

    if style == "fractional":
        amount = bankroll * config["bankroll_fraction"]
    elif style == "profits_only":
        amount = bankroll if bankroll <= starting_bankroll else (bankroll - starting_bankroll)
    elif style == "all_in":
        amount = bankroll
    else:
        amount = config["min_bet"]

    amount = min(amount, bankroll)
    if bankroll >= config["min_bet"]:
        amount = max(amount, config["min_bet"])
    return round(max(amount, 0), 2)


def resolve_mode(mode):
    """Map legacy mode names onto the canonical config."""
    if mode == "normal":
        return "aggressive"
    return mode


def choose_fallback_direction(window_open_price, current_price):
    """Never skip a trade at the hard deadline."""
    if current_price is None or window_open_price is None:
        return "UP"
    return "UP" if current_price >= window_open_price else "DOWN"


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

def place_order(client, token_id, side, amount, price, dry_run=False, order_type="GTC"):
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
        return {"dry_run": True, "status": "simulated", "order_type": order_type}

    try:
        from py_clob_client.order_builder.constants import BUY
        from py_clob_client.clob_types import OrderArgs

        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=amount,
            side=BUY,
        )
        signed_order = client.create_order(order_args)
        result = client.post_order(signed_order, orderType=order_type)
        log_event("order_placed", {"result": str(result), "order_type": order_type, "price": price})
        return result
    except Exception as e:
        log_event("order_error", {"error": str(e), "traceback": traceback.format_exc()})
        return None


def execute_order_until_close(client, token_id, amount_dollars, fallback_price, window_end_ts, dry_run=False):
    """
    Retry FOK buys every 3s until close, then fall back to a GTC bid at $0.95.
    """
    from py_clob_client.clob_types import OrderType

    if dry_run:
        shares = round(amount_dollars / max(fallback_price, 0.01), 2)
        return {
            "status": "dry_run",
            "order_type": "SIMULATED",
            "price": fallback_price,
            "shares": shares,
        }

    while time.time() < window_end_ts:
        live_price = fallback_price
        midpoint = fetch_midpoint(token_id)
        if midpoint:
            live_price = midpoint
        shares = round(amount_dollars / max(live_price, 0.01), 2)
        result = place_order(
            client,
            token_id,
            "BUY",
            shares,
            round(min(max(live_price, 0.01), 0.99), 3),
            dry_run=False,
            order_type=OrderType.FOK,
        )
        if result:
            return {
                "status": "filled",
                "order_type": "FOK",
                "price": round(min(max(live_price, 0.01), 0.99), 3),
                "shares": shares,
                "result": result,
            }
        time.sleep(3)

    limit_price = 0.95
    min_shares = 5.0
    shares = round(max(amount_dollars / limit_price, min_shares), 2)
    result = place_order(
        client,
        token_id,
        "BUY",
        shares,
        limit_price,
        dry_run=False,
        order_type=OrderType.GTC,
    )
    return {
        "status": "posted_fallback",
        "order_type": "GTC",
        "price": limit_price,
        "shares": shares,
        "result": result,
    }


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


def run_window(client, config, bankroll, starting_bankroll, mode_name, dry_run=False):
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
    snipe_start_ts = window_end_ts - config["snipe_start_s"]
    hard_deadline_ts = window_end_ts - config["hard_deadline_s"]
    last_midpoint_poll = 0

    while time.time() < snipe_start_ts:
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

    trade_record = {
        "window_ts": window_ts,
        "open_price": window_open_price,
        "entry_price": None,
        "score": 0,
        "confidence": 0,
        "direction": None,
        "bet": False,
        "amount": 0,
        "token_cost": 0,
        "pnl": 0,
        "outcome": None,
    }

    best_signal = None
    prev_score = None
    trigger_reason = None

    while time.time() < window_end_ts:
        loop_ts = time.time()
        current_price = fetch_current_price() or (tick_prices[-1][1] if tick_prices else None)
        if current_price is not None:
            tick_prices.append((loop_ts, current_price))

        if market_info and loop_ts - last_midpoint_poll >= 10:
            _, token_up, token_down = market_info
            up_mid, down_mid = fetch_both_midpoints(token_up, token_down)
            if up_mid is not None and down_mid is not None:
                midpoint_history.append((loop_ts, up_mid, down_mid))
                log_event("midpoint_poll", {
                    "up": up_mid,
                    "down": down_mid,
                    "elapsed": round(loop_ts - window_ts, 1),
                })
            last_midpoint_poll = loop_ts

        candles = fetch_candles(limit=30)
        score, confidence, details = analyze(
            candles=candles,
            window_open_price=window_open_price,
            current_price=current_price,
            tick_prices=tick_prices,
        )
        direction = "UP" if score > 0 else ("DOWN" if score < 0 else "SKIP")
        signal = {
            "score": score,
            "confidence": confidence,
            "direction": direction,
            "current_price": current_price,
            "details": details,
            "checked_at": loop_ts,
        }
        if direction != "SKIP" and (best_signal is None or abs(score) > abs(best_signal["score"])):
            best_signal = signal

        log_event("analysis_check", {
            "window_ts": window_ts,
            "score": score,
            "confidence": confidence,
            "direction": direction,
            "current_price": current_price,
            "delta_pct": round((current_price - window_open_price) / window_open_price * 100, 6) if current_price else None,
        })

        score_jump = abs(score - prev_score) if prev_score is not None else 0
        prev_score = score

        if direction != "SKIP" and score_jump >= 1.5:
            trigger_reason = "score_spike"
            best_signal = signal
            break
        if direction != "SKIP" and confidence >= config["confidence_threshold"]:
            trigger_reason = "confidence_met"
            best_signal = signal
            break
        if loop_ts >= hard_deadline_ts:
            trigger_reason = "hard_deadline"
            break
        time.sleep(2)

    chosen_signal = best_signal
    if chosen_signal is None:
        current_price = fetch_current_price() or (tick_prices[-1][1] if tick_prices else None) or window_open_price
        chosen_signal = {
            "score": 0.0,
            "confidence": 0.0,
            "direction": choose_fallback_direction(window_open_price, current_price),
            "current_price": current_price,
            "details": {"fallback": True},
        }
    elif trigger_reason == "hard_deadline" and best_signal is None:
        chosen_signal["direction"] = choose_fallback_direction(window_open_price, chosen_signal["current_price"])

    direction = chosen_signal["direction"]
    score = chosen_signal["score"]
    confidence = chosen_signal["confidence"]
    current_price = chosen_signal["current_price"]
    details = chosen_signal["details"]

    trade_record["entry_price"] = current_price
    trade_record["score"] = score
    trade_record["confidence"] = confidence
    trade_record["direction"] = direction

    log_event("analysis", {
        "window_ts": window_ts,
        "score": score,
        "confidence": confidence,
        "direction": direction,
        "trigger_reason": trigger_reason or "best_signal",
        "current_price": current_price,
        "delta_pct": round((current_price - window_open_price) / window_open_price * 100, 6) if current_price else None,
        "details": details,
    })

    token_price = estimate_token_price(window_open_price, current_price)
    if not dry_run and market_info:
        _, token_up, token_down = market_info
        target_token = token_up if direction == "UP" else token_down
        live_mid = fetch_midpoint(target_token)
        if live_mid:
            token_price = live_mid
            log_event("live_midpoint", {
                "window_ts": window_ts,
                "direction": direction,
                "midpoint": token_price,
            })
        else:
            log_event("midpoint_fallback", {"window_ts": window_ts, "estimated_price": token_price})
    else:
        log_event("pricing_model", {"window_ts": window_ts, "estimated_price": token_price})

    bet_amount = calculate_bet_size(bankroll, starting_bankroll, config)
    if bankroll < config["min_bet"]:
        log_event("bankroll_reset", {
            "window_ts": window_ts,
            "previous_bankroll": bankroll,
            "reset_to": starting_bankroll,
            "reason": "below_min_bet",
        })
        bankroll = starting_bankroll
        bet_amount = calculate_bet_size(bankroll, starting_bankroll, config)

    trade_record["bet"] = True
    trade_record["amount"] = bet_amount
    trade_record["token_cost"] = token_price

    log_event("placing_bet", {
        "window_ts": window_ts,
        "mode": mode_name,
        "direction": direction,
        "amount": bet_amount,
        "token_price": token_price,
        "bankroll_before": bankroll,
    })

    if market_info:
        _, token_up, token_down = market_info
        token_id = token_up if direction == "UP" else token_down
        result = execute_order_until_close(
            client,
            token_id,
            bet_amount,
            token_price,
            window_end_ts,
            dry_run=dry_run or client is None,
        )
        log_event("order_result", {"window_ts": window_ts, "result": str(result)})
    else:
        log_event("order_skip", {"window_ts": window_ts, "reason": "market_not_found"})

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
    parser.add_argument("--mode", choices=["safe", "aggressive", "normal", "degen"], default=None,
                        help="Trading mode (overrides BOT_MODE env var)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run without placing real orders")
    parser.add_argument("--once", action="store_true",
                        help="Run one window then exit")
    parser.add_argument("--max-trades", type=int, default=None,
                        help="Stop after N windows")
    parser.add_argument("--bankroll", type=float, default=None,
                        help="Starting bankroll (overrides STARTING_BANKROLL env var)")
    args = parser.parse_args()

    mode = resolve_mode(args.mode or os.getenv("BOT_MODE", "safe"))
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
    starting_bankroll = bankroll

    print("=" * 60, flush=True)
    print("  POLYMARKET BTC 5-MIN TRADING BOT", flush=True)
    print("=" * 60, flush=True)
    print(f"  Mode:       {config['display_name']}", flush=True)
    print(f"  Bankroll:   ${bankroll:.2f} (live balance)", flush=True)
    print(f"  Dry run:    {args.dry_run}", flush=True)
    print(f"  Min bet:    ${config['min_bet']:.2f}", flush=True)
    print(f"  Conf thres: {config['confidence_threshold']:.2f}", flush=True)
    print(f"  Bet style:  {config['bet_style']}", flush=True)
    print(f"  Snipe at:   T-{config['snipe_start_s']}s to T-{config['hard_deadline_s']}s")
    print("=" * 60)

    # Trading loop
    trade_count = 0
    wins = 0
    losses = 0

    try:
        while True:
            bankroll, trade = run_window(
                client,
                config,
                bankroll,
                starting_bankroll,
                mode,
                dry_run=args.dry_run,
            )

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
            if args.max_trades and trade_count >= args.max_trades:
                print(f"\n--max-trades limit ({args.max_trades}) reached. Exiting.")
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
