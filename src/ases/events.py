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
# Field names that contain "key" but are plain identifiers, never credentials. Without this every event
# stored `"task_key": "[redacted]"` (found 2026-09-19 while building the review-only merge tests), which
# threw away the one field that says which plan task an event is about. Exact names only, so a real
# credential field ("key", "api_key", "access_key") is still redacted wholesale.
_NOT_SECRET_KEYS = frozenset({"task_key", "idempotency_key"})
# Secret shapes that can be recognised by their PREFIX, so an ordinary word, a commit SHA or a UUID never matches:
# OpenAI, OpenRouter and Anthropic sk-, Stripe sk_live_/sk_test_/rk_live_/rk_test_, GitHub ghp_/gho_/ghu_/ghs_/ghr_/
# github_pat_, GitLab glpat-, Slack xox?-, NVIDIA nvapi-, Hugging Face hf_ (34 characters, hence 30 or more here),
# AWS access key ids (AKIA/ASIA), Google API keys (AIza), a Bearer token in a header, a JSON web token, and the header
# line of a PEM private key. Extend as new providers are added; this is a safety net, not the primary control.
_SECRET_VALUE_PATTERN = re.compile(
    r"\b(?:sk-|sk_live_|sk_test_|rk_live_|rk_test_|glpat-|ghp_|gho_|ghu_|ghs_|ghr_|github_pat_|xox[baprs]-|nvapi-)"
    r"[A-Za-z0-9_-]{10,}\b"
    r"|\bhf_[A-Za-z0-9]{30,}\b"
    r"|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"
    r"|\bAIza[0-9A-Za-z_-]{35}\b"
    r"|\bBearer\s+[A-Za-z0-9._~+/=-]{20,}"
    r"|\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)
# A credential is a string. A number, a boolean or a null under a key such as "input_tokens" or "max_tokens" is a
# count or a flag, and blanking it threw away real information (the report builder had to rename its fields to
# "tok_in" and "tok_out" to keep them), so only a non-scalar or a string value is redacted wholesale.
_NEVER_SECRET_SCALARS = (bool, int, float, type(None))


def _redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: (
                "[redacted]"
                if str(k) not in _NOT_SECRET_KEYS and _SECRET_KEY_PATTERN.search(str(k))
                and not isinstance(v, _NEVER_SECRET_SCALARS)
                else _redact(v)
            )
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


def redact_text(text: str) -> str:
    """The value scan alone, for one string that is about to be written somewhere that is not the events table (a
    card body, a comment, a report): every secret-shaped substring becomes [redacted]. Same patterns as redact()."""
    return _SECRET_VALUE_PATTERN.sub("[redacted]", text)


def record(conn: sqlite3.Connection, kind: str, payload: dict | None = None) -> None:
    safe = _redact(payload or {})
    conn.execute(
        "INSERT INTO events (ts, kind, payload) VALUES (?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"), kind, json.dumps(safe, default=str)),
    )


def recent(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    cur = conn.execute("SELECT ts, kind, payload FROM events ORDER BY id DESC LIMIT ?", (limit,))
    return [dict(r) for r in cur.fetchall()]
