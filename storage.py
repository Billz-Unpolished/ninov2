"""
Minimal Postgres storage helpers for PolyV2.

Stores bot events in a single JSONB-backed table so both the worker bot and
the dashboard can share the same live state through DATABASE_URL.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any


DATABASE_URL = os.getenv("DATABASE_URL")

_connection = None
_schema_ready = False
_disabled = False


def db_enabled() -> bool:
    return bool(DATABASE_URL) and not _disabled


def _connect():
    global _connection, _disabled
    if _disabled or not DATABASE_URL:
        return None

    if _connection is not None and not _connection.closed:
        return _connection

    try:
        import psycopg
    except ImportError:
        _disabled = True
        return None

    try:
        _connection = psycopg.connect(DATABASE_URL, autocommit=True)
        return _connection
    except Exception as exc:
        print(f"[storage] Postgres connection error: {exc}")
        _disabled = True
        return None


def ensure_schema() -> bool:
    global _schema_ready, _disabled
    if _schema_ready:
        return True

    conn = _connect()
    if conn is None:
        return False

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_events (
                    id BIGSERIAL PRIMARY KEY,
                    ts TIMESTAMPTZ NOT NULL,
                    event_type TEXT NOT NULL,
                    window_ts BIGINT NULL,
                    data JSONB NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS bot_events_ts_idx
                ON bot_events (ts DESC)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS bot_events_event_type_idx
                ON bot_events (event_type)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS bot_events_window_ts_idx
                ON bot_events (window_ts)
                """
            )
        _schema_ready = True
        return True
    except Exception as exc:
        print(f"[storage] Schema init error: {exc}")
        _disabled = True
        return False


def insert_event(entry: dict[str, Any]) -> bool:
    global _disabled, _connection
    if not ensure_schema():
        return False

    conn = _connect()
    if conn is None:
        return False

    try:
        ts_value = entry.get("ts")
        if isinstance(ts_value, str):
            ts = datetime.fromisoformat(ts_value)
        else:
            ts = datetime.utcnow()

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bot_events (ts, event_type, window_ts, data)
                VALUES (%s, %s, %s, %s::jsonb)
                """,
                (
                    ts,
                    entry.get("event", "unknown"),
                    entry.get("window_ts"),
                    json.dumps(entry, default=str),
                ),
            )
        return True
    except Exception as exc:
        print(f"[storage] Insert error: {exc}")
        try:
            if _connection is not None:
                _connection.close()
        except Exception:
            pass
        _connection = None
        _disabled = False
        return False


def fetch_events(limit: int | None = None) -> list[dict[str, Any]]:
    if not ensure_schema():
        return []

    conn = _connect()
    if conn is None:
        return []

    query = "SELECT data FROM bot_events ORDER BY ts ASC"
    params: tuple[Any, ...] = ()
    if limit is not None:
        query += " LIMIT %s"
        params = (limit,)

    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
    except Exception as exc:
        print(f"[storage] Read error: {exc}")
        return []

    events: list[dict[str, Any]] = []
    for (payload,) in rows:
        if isinstance(payload, dict):
            events.append(payload)
        elif isinstance(payload, str):
            try:
                events.append(json.loads(payload))
            except json.JSONDecodeError:
                continue
    return events
