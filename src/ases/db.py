"""SQLite persistence for ASES (section 9.1: db.py).

One connection helper, WAL mode, a minimal schema-version marker. Every ASES record is keyed by
provider/model, or later by Hermes card ID and commit SHA (ASES-ARC-03) -- this module only owns the
connection and schema; callers hold their own cursors per call, and every write here is a single
autocommit statement or an explicit transaction the caller controls.
"""
from __future__ import annotations

import pathlib
import sqlite3
import threading

SCHEMA_VERSION = 5

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS requests_ledger (
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    utc_date TEXT NOT NULL,        -- YYYY-MM-DD, UTC
    count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (provider, model, utc_date)
);

CREATE TABLE IF NOT EXISTS model_registry (
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    context_length INTEGER,        -- NULL = undeclared / unverified (ASES-MOD-02)
    tool_calling INTEGER,          -- 0/1/NULL = unknown
    role_class TEXT,
    data_policy TEXT,
    pinned INTEGER NOT NULL DEFAULT 0,
    smoke_test_at TEXT,
    smoke_test_result TEXT,        -- 'pass' | 'fail' | NULL
    smoke_test_detail TEXT,
    PRIMARY KEY (provider, model)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL          -- JSON, secret-redacted before write (ASES-SEC-01)
);

-- Phase 3: one plan task becomes a work card (W) and a merge card (M) (ASES-TSK-01).
CREATE TABLE IF NOT EXISTS plan_tasks (
    project TEXT NOT NULL,
    task_key TEXT NOT NULL,        -- plan.json task key, e.g. 'T1'
    work_card_id TEXT,             -- Hermes kanban card id, once created
    merge_card_id TEXT,
    role TEXT NOT NULL,
    touches TEXT,                  -- JSON list of path globs
    gate_profile TEXT,
    estimated_requests INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    review_rounds INTEGER NOT NULL DEFAULT 0,
    fix_cards INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    PRIMARY KEY (project, task_key)
);

CREATE TABLE IF NOT EXISTS gate_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_key TEXT NOT NULL,
    gate TEXT NOT NULL,            -- 'gate1' | 'gate3' | ...
    commit_sha TEXT,
    result TEXT NOT NULL,          -- 'pass' | 'fail'
    detail TEXT,
    ran_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS merge_records (
    task_key TEXT PRIMARY KEY,
    candidate_sha TEXT,
    gate3_result TEXT,
    squash_commit TEXT,
    reverted INTEGER NOT NULL DEFAULT 0,
    completed_at TEXT
);

-- ASES-QG-02: the gate profile content hash pinned at this project's last `swarm approve`, checked
-- again by `swarm run` so a diff that quietly changes gate configuration is refused, not trusted.
CREATE TABLE IF NOT EXISTS gate_pins (
    project TEXT PRIMARY KEY,
    gate_profiles_hash TEXT NOT NULL,
    pinned_at TEXT NOT NULL
);

-- Schema v4 (2026-09-19). One row per Hermes worker session whose usage has been counted against the request
-- ledger, so ingesting the same session twice cannot double count it (ASES-CAP-03).
CREATE TABLE IF NOT EXISTS usage_ingested (
    session_id TEXT PRIMARY KEY,
    profile TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    requests INTEGER NOT NULL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    ingested_at TEXT NOT NULL
);

-- The reviewer's verdict, stored by commit SHA (ASES-REV-06, ASES-GIT-03: a verdict belongs to one commit).
CREATE TABLE IF NOT EXISTS review_verdicts (
    project TEXT NOT NULL,
    task_key TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    card_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reviewer_profile TEXT NOT NULL,
    metadata TEXT,                 -- JSON, secret-redacted
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (project, task_key, commit_sha)
);

-- The primary checkout HEAD ASES itself last wrote or verified, per project (ASES-GIT-12): a HEAD that differs
-- from this, or a dirty primary checkout, was changed by something other than the controller.
CREATE TABLE IF NOT EXISTS integrity_state (
    project TEXT PRIMARY KEY,
    expected_head TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Schema v5 (2026-09-19). Lineage counters per plan task (ASES-REC-02): every fix card starts a new card-level
-- counter, so review rounds, capability and infrastructure failures and re-plans are counted per TASK.
CREATE TABLE IF NOT EXISTS lineage (
    project TEXT NOT NULL,
    task_key TEXT NOT NULL,
    review_rounds INTEGER NOT NULL DEFAULT 0,
    capability_failures INTEGER NOT NULL DEFAULT 0,
    infra_failures INTEGER NOT NULL DEFAULT 0,
    replans INTEGER NOT NULL DEFAULT 0,
    seen_card TEXT,                            -- the card whose review events seen_events counts
    seen_events INTEGER NOT NULL DEFAULT 0,    -- review events already counted for seen_card (reset on a new card)
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project, task_key)
);

-- Project-level state for the global bounds (ASES-CTL-01): when it started, the wall-clock deadline set at
-- Gate P, how many re-plans were spent, and why a stopped project stopped.
CREATE TABLE IF NOT EXISTS project_state (
    project TEXT PRIMARY KEY,
    started_at TEXT,
    deadline_at TEXT,
    replans INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'planning',   -- planning | running | paused | stopped | finished
    stop_reason TEXT,
    updated_at TEXT NOT NULL
);

-- Intent and completion records for every multi-step action (blueprint section 19.4): create cards, run a gate,
-- build a candidate, fast-forward, complete a merge card, revert. An intent with no completed_at is what
-- reconcile-on-start looks for after a crash.
CREATE TABLE IF NOT EXISTS intents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    detail TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT
);
"""

# Columns added to an EXISTING table after it first shipped. CREATE TABLE IF NOT EXISTS cannot add a column to a
# table an earlier version already made, so each is added here when missing (table, column, declaration).
_ADDED_COLUMNS = (
    ("usage_ingested", "project", "TEXT"),
    ("usage_ingested", "task_key", "TEXT"),
    ("usage_ingested", "card_id", "TEXT"),
)


def _ensure_columns(conn: sqlite3.Connection) -> None:
    for table, column, declaration in _ADDED_COLUMNS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

_lock = threading.Lock()


def connect(db_path: str | pathlib.Path) -> sqlite3.Connection:
    """Open (creating if needed) the ASES SQLite database and ensure the schema exists."""
    db_path = pathlib.Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None)  # autocommit by default
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    with _lock:
        conn.executescript(_SCHEMA)
        _ensure_columns(conn)
        current = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()["v"] or 0
        if current < SCHEMA_VERSION:
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, datetime('now'))",
                (SCHEMA_VERSION,),
            )
    return conn
