"""Structured event log with secret redaction (section 9.1: events.py; ASES-SEC-01, ASES-OBS-02).

Every event payload is redacted before it touches disk: keys that look like credentials are replaced
wholesale, and string values are scanned for common secret-token shapes. This is defense in depth, not
a substitute for never putting secrets in a payload in the first place.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

_SECRET_KEY_PATTERN = re.compile(r"(key|token|secret|password|credential|authorization)", re.IGNORECASE)
# Common provider-key shapes (OpenAI/OpenRouter sk-..., GitHub ghp_/glpat-, Slack xox...). Extend as
# new providers are added; this is a safety net, not the primary control.
_SECRET_VALUE_PATTERN = re.compile(r"\b(sk-|glpat-|ghp_|gho_|xox[baprs]-)[A-Za-z0-9_-]{10,}\b")


def _redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: ("[redacted]" if _SECRET_KEY_PATTERN.search(str(k)) else _redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    if isinstance(obj, str):
        return _SECRET_VALUE_PATTERN.sub("[redacted]", obj)
    return obj


def redact(payload: dict) -> dict:
    """Exposed separately so callers (e.g. the doctor report) can redact text that isn't going to the DB."""
    return _redact(payload)


def record(conn: sqlite3.Connection, kind: str, payload: dict | None = None) -> None:
    safe = _redact(payload or {})
    conn.execute(
        "INSERT INTO events (ts, kind, payload) VALUES (?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"), kind, json.dumps(safe, default=str)),
    )


def recent(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    cur = conn.execute("SELECT ts, kind, payload FROM events ORDER BY id DESC LIMIT ?", (limit,))
    return [dict(r) for r in cur.fetchall()]
