"""
Minimal dashboard for the PolyV2 bot.

Usage:
    python3 dashboard.py
    python3 dashboard.py --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import argparse
import json
import math
import os
from collections import Counter
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from storage import db_enabled, fetch_events


ROOT = Path(__file__).resolve().parent
LOG_PATH = ROOT / "bot_log.jsonl"
DATABASE_URL = os.getenv("DATABASE_URL")

try:
    from bot import MODE_CONFIGS  # type: ignore
except Exception:
    MODE_CONFIGS = {
        "safe": {
            "min_bet": 1.0,
            "max_bet": 5.0,
            "confidence_threshold": 0.60,
            "min_score": 3.0,
            "kelly_fraction": 0.10,
            "entry_delay_s": 200,
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


def read_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []

    events: list[dict[str, Any]] = []
    with path.open() as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                events.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
    return events


def load_events() -> tuple[list[dict[str, Any]], str]:
    if db_enabled():
        events = fetch_events()
        if events:
            return events, "postgres"
    return read_events(LOG_PATH), "log_file"


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def fmt_dt(value: str | None) -> str:
    dt = parse_ts(value)
    if not dt:
        return "-"
    local = dt.astimezone()
    return local.strftime("%Y-%m-%d %H:%M:%S %Z")


def fmt_window_ts(epoch: int | None) -> str:
    if not epoch:
        return "-"
    return datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def fmt_money(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return f"${float(value):,.3f}"
    except (TypeError, ValueError):
        return "-"


def fmt_num(value: Any, digits: int = 3) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def percent(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "-"


def build_state(events: list[dict[str, Any]]) -> dict[str, Any]:
    windows: dict[int, dict[str, Any]] = {}
    event_counts = Counter()
    current_mode = None
    last_event = events[-1] if events else None
    latest_balance = None
    latest_session_stats = None
    latest_analysis = None
    latest_waiting = None
    active_window_ts: int | None = None

    for event in events:
        event_type = event.get("event", "unknown")
        event_counts[event_type] += 1

        if event_type in {"client_initialized", "dry_run_mode"}:
            current_mode = event.get("mode") or current_mode

        if event_type == "session_stats":
            latest_session_stats = event
            latest_balance = event.get("bankroll", latest_balance)

        if event_type in {"win", "loss"}:
            latest_balance = event.get("bankroll", latest_balance)

        if event_type == "window_open":
            latest_balance = event.get("bankroll", latest_balance)

        if event_type == "analysis":
            latest_analysis = event

        if event_type == "waiting_for_window":
            latest_waiting = event

        window_ts = event.get("window_ts")
        if window_ts is not None:
            active_window_ts = int(window_ts)

        if window_ts is None and event_type in {
            "market_found",
            "midpoint_poll",
            "midpoint_error",
            "analysis",
            "live_midpoint",
            "placing_bet",
            "dry_run_bet",
            "skip",
            "window_close_skip",
            "order_error",
            "order_result",
            "win",
            "loss",
        }:
            window_ts = active_window_ts

        if window_ts is None:
            continue

        window = windows.setdefault(
            int(window_ts),
            {
                "window_ts": int(window_ts),
                "opened_at": None,
                "open_price": None,
                "entry_price": None,
                "direction": None,
                "score": None,
                "confidence": None,
                "details": {},
                "midpoint": None,
                "amount": None,
                "token_price": None,
                "skip_reason": None,
                "outcome": None,
                "result": None,
                "pnl": None,
                "bankroll_after": None,
                "dry_run": False,
                "order_error": None,
            },
        )

        if event_type == "window_open":
            window["opened_at"] = event.get("ts")
            window["open_price"] = event.get("open_price")
        elif event_type == "analysis":
            window["entry_price"] = event.get("current_price")
            window["direction"] = event.get("direction")
            window["score"] = event.get("score")
            window["confidence"] = event.get("confidence")
            window["details"] = event.get("details", {})
        elif event_type == "live_midpoint":
            window["midpoint"] = event.get("midpoint")
        elif event_type == "placing_bet":
            window["amount"] = event.get("amount")
            window["token_price"] = event.get("token_price")
            window["direction"] = event.get("direction") or window["direction"]
        elif event_type == "dry_run_bet":
            window["dry_run"] = True
        elif event_type == "skip":
            window["skip_reason"] = event.get("reason")
        elif event_type == "window_close_skip":
            window["outcome"] = event.get("actual")
            window["result"] = "SKIP"
        elif event_type in {"win", "loss"}:
            window["outcome"] = event.get("actual")
            window["result"] = event_type.upper()
            window["pnl"] = event.get("pnl")
            window["bankroll_after"] = event.get("bankroll")
        elif event_type == "order_error":
            window["order_error"] = event.get("error")

    trade_windows = sorted(windows.values(), key=lambda item: item["window_ts"], reverse=True)
    realized = [w for w in trade_windows if w.get("result") in {"WIN", "LOSS"}]
    wins = sum(1 for w in realized if w.get("result") == "WIN")
    losses = sum(1 for w in realized if w.get("result") == "LOSS")
    total_pnl = sum(float(w.get("pnl") or 0) for w in realized)
    win_rate = wins / len(realized) if realized else None
    last_trade = next((w for w in trade_windows if w.get("result") in {"WIN", "LOSS"}), None)

    return {
        "log_path": str(LOG_PATH),
        "event_count": len(events),
        "current_mode": current_mode or "unknown",
        "mode_config": MODE_CONFIGS.get(current_mode or "", {}),
        "current_balance": latest_balance,
        "session_stats": latest_session_stats,
        "latest_analysis": latest_analysis,
        "last_event": last_event,
        "next_window": latest_waiting,
        "trade_windows": trade_windows,
        "recent_trades": trade_windows[:12],
        "wins": wins,
        "losses": losses,
        "realized_trade_count": len(realized),
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "event_counts": event_counts,
        "last_trade": last_trade,
    }


def esc(value: Any) -> str:
    text = "" if value is None else str(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render_indicator_bars(details: dict[str, Any]) -> str:
    bars = []
    ordered = [
        ("window_delta", "Window Delta"),
        ("micro_momentum", "Micro Momentum"),
        ("acceleration", "Acceleration"),
        ("ema_crossover", "EMA Cross"),
        ("rsi", "RSI"),
        ("volume_surge", "Volume Surge"),
        ("tick_trend", "Tick Trend"),
    ]

    for key, label in ordered:
        item = details.get(key, {})
        contribution = item.get("contribution", 0) if isinstance(item, dict) else 0
        magnitude = min(abs(float(contribution)) / 7.0, 1.0) if contribution else 0.0
        width = max(6, int(magnitude * 100)) if contribution else 6
        direction = "pos" if contribution > 0 else ("neg" if contribution < 0 else "neu")
        bars.append(
            f"""
            <div class="indicator">
              <div class="indicator-head">
                <span>{esc(label)}</span>
                <span>{esc(fmt_num(contribution, 1))}</span>
              </div>
              <div class="bar-track">
                <div class="bar {direction}" style="width:{width}%"></div>
              </div>
            </div>
            """
        )
    return "".join(bars)


def render_trade_rows(trades: list[dict[str, Any]]) -> str:
    rows = []
    for trade in trades:
        result = trade.get("result") or ("BET" if trade.get("amount") else "OPEN")
        rows.append(
            f"""
            <tr>
              <td>{esc(fmt_window_ts(trade.get("window_ts")))}</td>
              <td>{esc(trade.get("direction") or "-")}</td>
              <td>{esc(fmt_num(trade.get("score"), 1))}</td>
              <td>{esc(percent(trade.get("confidence")))}</td>
              <td>{esc(fmt_money(trade.get("amount")))}</td>
              <td>{esc(fmt_num(trade.get("token_price"), 3))}</td>
              <td>{esc(trade.get("outcome") or trade.get("skip_reason") or "-")}</td>
              <td class="result {esc(str(result).lower())}">{esc(result)}</td>
              <td>{esc(fmt_money(trade.get("pnl")))}</td>
            </tr>
            """
        )
    return "".join(rows) or '<tr><td colspan="9">No trades yet.</td></tr>'


def render_dashboard(state: dict[str, Any]) -> str:
    mode = state["current_mode"]
    mode_config = state["mode_config"]
    latest_analysis = state.get("latest_analysis") or {}
    analysis_details = latest_analysis.get("details", {})
    recent_trades = state["recent_trades"]
    last_event = state.get("last_event") or {}
    next_window = state.get("next_window") or {}

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="10">
  <title>PolyV2 Dashboard</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f5f1e8;
      --panel: #fffdf8;
      --ink: #1b1b18;
      --muted: #6c6a63;
      --line: #d9d1c1;
      --accent: #0f766e;
      --green: #2d6a4f;
      --red: #b42318;
      --amber: #b7791f;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Menlo, Monaco, Consolas, monospace;
      color: var(--ink);
      background:
        radial-gradient(circle at top right, rgba(15,118,110,0.10), transparent 28%),
        linear-gradient(180deg, #f7f3ea 0%, var(--bg) 100%);
    }}
    .wrap {{
      max-width: 1180px;
      margin: 0 auto;
      padding: 24px;
    }}
    .hero {{
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: end;
      margin-bottom: 20px;
    }}
    h1 {{
      font-size: 34px;
      margin: 0;
      letter-spacing: -0.04em;
    }}
    .sub {{
      color: var(--muted);
      margin-top: 6px;
      font-size: 14px;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(12, 1fr);
      gap: 14px;
    }}
    .card {{
      grid-column: span 12;
      background: rgba(255,253,248,0.88);
      backdrop-filter: blur(6px);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 16px;
      box-shadow: 0 12px 32px rgba(27,27,24,0.06);
    }}
    .card h2 {{
      margin: 0 0 14px;
      font-size: 15px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
    }}
    .stat-grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
    }}
    .stat {{
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: 14px;
      background: #fff;
    }}
    .stat-label {{
      font-size: 12px;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }}
    .stat-value {{
      margin-top: 8px;
      font-size: 28px;
      letter-spacing: -0.04em;
    }}
    .span-7 {{ grid-column: span 7; }}
    .span-5 {{ grid-column: span 5; }}
    .span-6 {{ grid-column: span 6; }}
    .kv {{
      display: grid;
      grid-template-columns: 140px 1fr;
      gap: 8px 12px;
      font-size: 14px;
    }}
    .kv div:nth-child(odd) {{
      color: var(--muted);
    }}
    .indicator {{
      margin-bottom: 12px;
    }}
    .indicator-head {{
      display: flex;
      justify-content: space-between;
      margin-bottom: 6px;
      font-size: 13px;
    }}
    .bar-track {{
      height: 10px;
      border-radius: 999px;
      background: #ece4d6;
      overflow: hidden;
    }}
    .bar {{
      height: 100%;
      border-radius: 999px;
    }}
    .bar.pos {{ background: linear-gradient(90deg, #2d6a4f, #4caf7d); }}
    .bar.neg {{ background: linear-gradient(90deg, #b42318, #e76f51); }}
    .bar.neu {{ background: linear-gradient(90deg, #8f8c84, #b7b2a7); }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }}
    th, td {{
      text-align: left;
      padding: 10px 8px;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
    }}
    th {{
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.08em;
      font-size: 11px;
    }}
    .pill {{
      display: inline-block;
      padding: 5px 10px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: #fff;
      font-size: 12px;
    }}
    .result.win {{ color: var(--green); }}
    .result.loss {{ color: var(--red); }}
    .result.skip {{ color: var(--amber); }}
    .foot {{
      margin-top: 16px;
      color: var(--muted);
      font-size: 12px;
    }}
    @media (max-width: 900px) {{
      .span-7, .span-5, .span-6 {{
        grid-column: span 12;
      }}
      .stat-grid {{
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }}
      .hero {{
        flex-direction: column;
        align-items: start;
      }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="hero">
        <div>
        <h1>PolyV2 Dashboard</h1>
        <div class="sub">Minimal live view of mode, decision system, trades, and balance.</div>
      </div>
      <div class="pill">Last event: {esc(last_event.get("event", "-"))} at {esc(fmt_dt(last_event.get("ts")))}</div>
    </div>

    <div class="card">
      <h2>Topline</h2>
      <div class="stat-grid">
        <div class="stat">
          <div class="stat-label">Mode</div>
          <div class="stat-value">{esc(mode.upper())}</div>
        </div>
        <div class="stat">
          <div class="stat-label">Balance</div>
          <div class="stat-value">{esc(fmt_money(state.get("current_balance")))}</div>
        </div>
        <div class="stat">
          <div class="stat-label">Realized Win Rate</div>
          <div class="stat-value">{esc(percent(state.get("win_rate")))}</div>
        </div>
        <div class="stat">
          <div class="stat-label">Total P&amp;L</div>
          <div class="stat-value">{esc(fmt_money(state.get("total_pnl")))}</div>
        </div>
      </div>
    </div>

    <div class="grid" style="margin-top:14px;">
      <div class="card span-5">
        <h2>Mode Rules</h2>
        <div class="kv">
          <div>Min bet</div><div>{esc(fmt_money(mode_config.get("min_bet")))}</div>
          <div>Max bet</div><div>{esc(fmt_money(mode_config.get("max_bet")))}</div>
          <div>Conf threshold</div><div>{esc(percent(mode_config.get("confidence_threshold")))}</div>
          <div>Min score</div><div>{esc(fmt_num(mode_config.get("min_score"), 1))}</div>
          <div>Kelly fraction</div><div>{esc(percent(mode_config.get("kelly_fraction")))}</div>
          <div>Entry delay</div><div>{esc(mode_config.get("entry_delay_s"))}s</div>
          <div>Next window</div><div>{esc(fmt_window_ts(next_window.get("window_ts")))}</div>
        </div>
      </div>

      <div class="card span-7">
        <h2>Latest Decision</h2>
        <div class="stat-grid" style="margin-bottom:14px;">
          <div class="stat">
            <div class="stat-label">Direction</div>
            <div class="stat-value">{esc(latest_analysis.get("direction", "-"))}</div>
          </div>
          <div class="stat">
            <div class="stat-label">Score</div>
            <div class="stat-value">{esc(fmt_num(latest_analysis.get("score"), 1))}</div>
          </div>
          <div class="stat">
            <div class="stat-label">Confidence</div>
            <div class="stat-value">{esc(percent(latest_analysis.get("confidence")))}</div>
          </div>
          <div class="stat">
            <div class="stat-label">Entry Price</div>
            <div class="stat-value">{esc(fmt_num(latest_analysis.get("current_price"), 2))}</div>
          </div>
        </div>
        {render_indicator_bars(analysis_details)}
      </div>

      <div class="card span-6">
        <h2>Session</h2>
        <div class="kv">
          <div>Trade count</div><div>{esc((state.get("session_stats") or {}).get("trade_count", "-"))}</div>
          <div>Wins</div><div>{esc(state.get("wins"))}</div>
          <div>Losses</div><div>{esc(state.get("losses"))}</div>
          <div>Realized trades</div><div>{esc(state.get("realized_trade_count"))}</div>
          <div>Events in log</div><div>{esc(state.get("event_count"))}</div>
          <div>Data source</div><div>{esc(state.get("data_source"))}</div>
          <div>Log file</div><div>{esc(state.get("log_path"))}</div>
        </div>
      </div>

      <div class="card span-6">
        <h2>Last Trade</h2>
        <div class="kv">
          <div>Window</div><div>{esc(fmt_window_ts((state.get("last_trade") or {}).get("window_ts")))}</div>
          <div>Direction</div><div>{esc((state.get("last_trade") or {}).get("direction", "-"))}</div>
          <div>Outcome</div><div>{esc((state.get("last_trade") or {}).get("outcome", "-"))}</div>
          <div>Result</div><div>{esc((state.get("last_trade") or {}).get("result", "-"))}</div>
          <div>Stake</div><div>{esc(fmt_money((state.get("last_trade") or {}).get("amount")))}</div>
          <div>P&amp;L</div><div>{esc(fmt_money((state.get("last_trade") or {}).get("pnl")))}</div>
          <div>Balance after</div><div>{esc(fmt_money((state.get("last_trade") or {}).get("bankroll_after")))}</div>
        </div>
      </div>

      <div class="card span-12">
        <h2>Recent Windows</h2>
        <table>
          <thead>
            <tr>
              <th>Window</th>
              <th>Direction</th>
              <th>Score</th>
              <th>Confidence</th>
              <th>Bet</th>
              <th>Token</th>
              <th>Outcome / Skip</th>
              <th>Status</th>
              <th>P&amp;L</th>
            </tr>
          </thead>
          <tbody>
            {render_trade_rows(recent_trades)}
          </tbody>
        </table>
      </div>
    </div>

    <div class="foot">Refreshes every 10 seconds. JSON is available at <code>/api/state</code>.</div>
  </div>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        events, source = load_events()
        state = build_state(events)
        state["data_source"] = source

        if parsed.path == "/api/state":
            self._send_json(state)
            return

        if parsed.path in {"/", "/index.html"}:
            self._send_html(render_dashboard(state))
            return

        if parsed.path == "/health":
            self._send_json({"ok": True, "events": len(events)})
            return

        self.send_response(404)
        self.end_headers()
        self.wfile.write(b"Not found")

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: Any) -> None:
        body = json.dumps(payload, default=_json_default).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _json_default(value: Any) -> Any:
    if isinstance(value, Counter):
        return dict(value)
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return str(value)


def main() -> None:
    parser = argparse.ArgumentParser(description="PolyV2 minimal dashboard")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, default=8000, help="Bind port")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print(f"PolyV2 dashboard running on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
