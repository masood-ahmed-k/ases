"""Intent and completion records for multi-step actions (section 19.4, ASES-REC-04).

"Every multi-step action writes an intent record before acting and a completion record after: create cards, run a
gate, build a candidate, fast-forward, complete a merge card, revert." A crash between the two leaves an intent
with no completed_at, and that open row is exactly what reconcile-on-start looks for: it names the action that was
in flight, so reconcile checks the board, Git and the database for that one task instead of guessing.

The key is the plan task key, or the project name for an action that spans several tasks. Every write here is ONE
statement, so nothing depends on a transaction and the module is safe under the ASES connection's autocommit mode
(db.connect): a crash can leave an intent open but never a half-written row. The detail text is passed through the
event redactor first (ASES-SEC-01), because a gate's output or a git error can end up in it.
"""
from __future__ import annotations

import contextlib
import sqlite3
from datetime import datetime, timezone

from . import events

# The six multi-step actions of section 19.4. The vocabulary is open (any string is accepted), these are the ones
# the controller and merge queue are expected to write and reconcile knows how to interpret.
KIND_CREATE_CARDS = "create_cards"
KIND_RUN_GATE = "run_gate"
KIND_BUILD_CANDIDATE = "build_candidate"
KIND_FAST_FORWARD = "fast_forward"
KIND_COMPLETE_MERGE_CARD = "complete_merge_card"
KIND_REVERT = "revert"

_COLUMNS = ("id", "kind", "key", "detail", "started_at")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean(text: str | None) -> str | None:
    """None stays None; anything else becomes a string with secret-shaped values redacted."""
    if text is None:
        return None
    return events.redact({"detail": str(text)})["detail"]


def begin(conn: sqlite3.Connection, project: str, kind: str, key: str, detail: str | None = None) -> int:
    """Write the intent record BEFORE acting, and return its id. The row stays open (completed_at NULL) until
    complete() or mark_recovered() closes it, so a crash in between leaves it for reconcile-on-start."""
    cur = conn.execute(
        "INSERT INTO intents (project, kind, key, detail, started_at) VALUES (?, ?, ?, ?, ?)",
        (project, kind, key, _clean(detail), _now()),
    )
    return int(cur.lastrowid)


def complete(conn: sqlite3.Connection, intent_id: int, detail: str | None = None) -> None:
    """Write the completion record AFTER acting. Completing twice keeps the FIRST time (and the first detail):
    the WHERE clause only matches an open row, so a repeated call, or a controller and a reconcile pass racing to
    close the same intent, cannot move the timestamp. A detail, when given, replaces the one written at begin."""
    conn.execute(
        "UPDATE intents SET completed_at = ?, detail = COALESCE(?, detail) WHERE id = ? AND completed_at IS NULL",
        (_now(), _clean(detail), intent_id),
    )


def open_intents(conn: sqlite3.Connection, project: str) -> list[dict]:
    """The project's intents with no completed_at, oldest first, each as {id, kind, key, detail, started_at}.
    Another project's intents are never returned. This is what reconcile-on-start reads after a crash."""
    rows = conn.execute(
        "SELECT id, kind, key, detail, started_at FROM intents WHERE project = ? AND completed_at IS NULL "
        "ORDER BY started_at, id",
        (project,),
    ).fetchall()
    return [dict(zip(_COLUMNS, tuple(row))) for row in rows]


@contextlib.contextmanager
def intent(conn: sqlite3.Connection, project: str, kind: str, key: str, detail: str | None = None):
    """begin(), yield the id, and complete() only when the body finished. An exception (or a crash) leaves the
    intent open ON PURPOSE: an action that died half way is exactly what reconcile-on-start has to look at, so
    closing it in a finally block would hide the very thing the record exists to expose."""
    intent_id = begin(conn, project, kind, key, detail)
    yield intent_id
    complete(conn, intent_id)


def mark_recovered(conn: sqlite3.Connection, intent_id: int, note: str) -> None:
    """Close an OPEN intent that reconcile-on-start has dealt with, appending what it found or did to the detail
    (separated from any earlier text by ' | ') so the row records both the interrupted action and its recovery.
    An intent that is already completed is left exactly as it was."""
    text = _clean(note) or ""
    conn.execute(
        "UPDATE intents SET completed_at = ?, "
        "detail = CASE WHEN detail IS NULL OR detail = '' THEN ? ELSE detail || ' | ' || ? END "
        "WHERE id = ? AND completed_at IS NULL",
        (_now(), text, text, intent_id),
    )
