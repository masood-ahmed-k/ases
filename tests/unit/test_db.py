import os
import pathlib
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

import pytest

from ases import db, guards

# ---------------------------------------------------------------------------------------------
# The OLD schema, as text, taken verbatim from the git history of src/ases/db.py, so an upgrade is proven against
# what really shipped and not against a copy derived from the new code. _OLD_SCHEMA_V5 is the whole `_SCHEMA` of the
# commit whose SCHEMA_VERSION was 5 (the version data/ases.db is at in the field); _OLD_SCHEMA_V6_EXTRA is what the
# next commit (SCHEMA_VERSION 6) added. The old code ran the whole script on every connect and then added three
# usage_ingested columns with ALTER TABLE, and it recorded ONE row: the version of the code that opened the file.
# ---------------------------------------------------------------------------------------------

_OLD_SCHEMA_V5 = """
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

_OLD_SCHEMA_V6_EXTRA = """
-- Schema v6 (2026-09-19). Leases on shared resources (ASES-GIT-14): a per-card port block, compose project name,
-- database name and temp directory, and singletons such as a shared development database taken through a lock.
-- A lease is active while released_at is NULL; the partial unique index makes a second active lease on the same
-- resource impossible at the database level, not just in application code.
CREATE TABLE IF NOT EXISTS resource_leases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    resource TEXT NOT NULL,        -- 'port-block:3', 'singleton:dev-db', ...
    holder TEXT NOT NULL,          -- the card id that holds it
    detail TEXT,                   -- JSON: the values that were handed out
    acquired_at TEXT NOT NULL,
    released_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_resource_leases_active
    ON resource_leases (project, resource) WHERE released_at IS NULL;

-- Snapshots of worktrees that no running card owns (ASES-GIT-12): only a running card's worker may change its
-- worktree, so a HEAD or a status that moved in any other worktree was changed by something else.
CREATE TABLE IF NOT EXISTS worktree_snapshots (
    project TEXT NOT NULL,
    path TEXT NOT NULL,
    head TEXT NOT NULL,
    status_hash TEXT NOT NULL,     -- sha256 of `git status --porcelain -z` output
    taken_at TEXT NOT NULL,
    PRIMARY KEY (project, path)
);
"""

# The tables that did NOT exist yet at an older schema version (from the git history: version 1 had four tables,
# version 2 added the phase 3 tables, version 3 added gate_pins). Version 4 was never committed on its own, so its
# shape here is a RECONSTRUCTION (version 3 plus usage_ingested without its attribution columns, review_verdicts and
# integrity_state); the upgrade must cope with it either way.
_ADDED_AFTER = {
    1: ["plan_tasks", "gate_runs", "merge_records", "gate_pins", "usage_ingested", "review_verdicts",
        "integrity_state", "lineage", "project_state", "intents"],
    2: ["gate_pins", "usage_ingested", "review_verdicts", "integrity_state", "lineage", "project_state", "intents"],
    3: ["usage_ingested", "review_verdicts", "integrity_state", "lineage", "project_state", "intents"],
    4: ["lineage", "project_state", "intents"],
}

# One row per table, so "rows intact" is checked on real content in every table that exists at a version.
_SEED_ROWS = {
    "requests_ledger": ["INSERT INTO requests_ledger VALUES ('openrouter', 'm1', '2026-09-19', 7, '2026-09-19T10:00:00')"],
    "model_registry": ["INSERT INTO model_registry (provider, model, context_length, pinned) "
                       "VALUES ('openrouter', 'm1', 131072, 1)"],
    "events": ["INSERT INTO events (ts, kind, payload) VALUES ('2026-09-19T10:00:00+00:00', 'card_created', '{\"a\": 1}')",
               "INSERT INTO events (ts, kind, payload) VALUES ('2026-09-19T10:05:00+00:00', 'gate_pass', '{}')"],
    "plan_tasks": ["INSERT INTO plan_tasks (project, task_key, work_card_id, merge_card_id, role, touches, "
                   "gate_profile, estimated_requests, fix_cards, created_at) VALUES "
                   "('p1', 'T1', 't_w1', 't_m1', 'coder', '[\"a.py\"]', 'g', 12, 1, '2026-09-19 10:00:00')"],
    "gate_runs": ["INSERT INTO gate_runs (task_key, gate, commit_sha, result, detail, ran_at) VALUES "
                  "('T1', 'gate1', 'abc123', 'pass', 'ok', '2026-09-19T10:01:00')"],
    "merge_records": ["INSERT INTO merge_records (task_key, candidate_sha, gate3_result, squash_commit, reverted, "
                      "completed_at) VALUES ('T1', 'def456', 'pass', 'def456', 0, '2026-09-19T10:02:00')"],
    "gate_pins": ["INSERT INTO gate_pins VALUES ('p1', 'hash1', '2026-09-19 10:00:00')"],
    "usage_ingested": ["INSERT INTO usage_ingested (session_id, profile, provider, model, requests, input_tokens, "
                       "output_tokens, ingested_at) VALUES ('s1', 'coder-1', 'openrouter', 'm1', 4, 100, 50, "
                       "'2026-09-19T10:03:00')"],
    "review_verdicts": ["INSERT INTO review_verdicts VALUES ('p1', 'T1', 'abc123', 't_w1', 'PASS', 'reviewer', "
                        "'{}', '2026-09-19T10:04:00')"],
    "integrity_state": ["INSERT INTO integrity_state VALUES ('p1', 'abc123', '2026-09-19T10:00:00')"],
    "lineage": ["INSERT INTO lineage (project, task_key, review_rounds, capability_failures, updated_at) VALUES "
                "('p1', 'T1', 2, 1, '2026-09-19T10:00:00')"],
    "project_state": ["INSERT INTO project_state (project, started_at, status, updated_at) VALUES "
                      "('p1', '2026-09-19T09:00:00', 'running', '2026-09-19T10:00:00')"],
    "intents": ["INSERT INTO intents (project, kind, key, started_at) VALUES ('p1', 'fast_forward', 'T1', "
                "'2026-09-19T10:02:00')"],
    "resource_leases": ["INSERT INTO resource_leases (project, resource, holder, acquired_at) VALUES "
                        "('p1', 'port-block:3', 't_w1', '2026-09-19T10:00:00')"],
    "worktree_snapshots": ["INSERT INTO worktree_snapshots VALUES ('p1', 'C:/wt/t_w1', 'abc123', 'h', "
                           "'2026-09-19T10:00:00')"],
}

_ALL_TABLES = {
    "schema_migrations", "requests_ledger", "model_registry", "events", "plan_tasks", "gate_runs", "merge_records",
    "gate_pins", "usage_ingested", "review_verdicts", "integrity_state", "lineage", "project_state", "intents",
    "resource_leases", "worktree_snapshots", "integrity_heads", "usage_runs", "usage_orphans",
}


def _make_old_database(path, version, *, seed=True, record_version=True):
    """A database exactly as the ASES of schema `version` left it: its tables, its one version row, its rows."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(str(path), isolation_level=None)
    raw.execute("PRAGMA journal_mode=WAL")
    raw.executescript(_OLD_SCHEMA_V5)
    if version >= 6:
        raw.executescript(_OLD_SCHEMA_V6_EXTRA)
    for table in _ADDED_AFTER.get(version, []):
        raw.execute(f"DROP TABLE {table}")
    if version >= 5:
        # what the old connect() did after the script, on every open (the field database has these three)
        for column in ("project", "task_key", "card_id"):
            raw.execute(f"ALTER TABLE usage_ingested ADD COLUMN {column} TEXT")
    if record_version:
        raw.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (?, '2026-09-19 10:00:00')", (version,))
    if seed:
        existing = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        for table, statements in _SEED_ROWS.items():
            if table in existing:
                for statement in statements:
                    raw.execute(statement)
    raw.close()
    return path


def _user_tables(conn):
    return sorted(r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"))


def _snapshot(conn):
    """{table: (column names, rows in rowid order)} for every user table except schema_migrations."""
    result = {}
    for table in _user_tables(conn):
        if table == "schema_migrations":
            continue
        columns = tuple(r[1] for r in conn.execute(f"PRAGMA table_info({table})"))
        rows = [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        result[table] = (columns, rows)
    return result


def _shape(conn):
    """The schema of a database as a comparable value: every table's columns (name, type, not null, default, pk
    position) and every index (name, table, unique, columns)."""
    tables = {}
    for table in _user_tables(conn):
        tables[table] = [tuple(r)[1:] for r in conn.execute(f"PRAGMA table_info({table})")]
    indexes = {}
    for name, table, sql in conn.execute(
        "SELECT name, tbl_name, sql FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
    ).fetchall():
        listing = [tuple(r) for r in conn.execute(f"PRAGMA index_list({table})") if r[1] == name][0]
        columns = [r[2] for r in conn.execute(f"PRAGMA index_info({name})")]
        # the stored text too, so a partial index keeps its WHERE clause, with whitespace collapsed
        indexes[name] = (table, listing[2], columns, " ".join((sql or "").split()))
    return tables, indexes


def _versions(conn):
    return [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]


def _backups(directory, name="ases.db"):
    return sorted(p.name for p in pathlib.Path(directory).glob(f"{name}.bak-*"))


# --- the original two tests ------------------------------------------------------------------------------------

def test_connect_creates_schema_and_is_idempotent(tmp_path):
    path = tmp_path / "sub" / "ases.db"
    conn = db.connect(path)
    assert path.exists()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"schema_migrations", "requests_ledger", "model_registry", "events"} <= tables

    # Reconnecting must not error or duplicate the migration rows (one row per migration, applied once).
    conn2 = db.connect(path)
    rows = conn2.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    assert rows == len(db.MIGRATIONS)


def test_connect_uses_wal_mode(tmp_path):
    conn = db.connect(tmp_path / "ases.db")
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


# --- the connection contract is unchanged ----------------------------------------------------------------------

def test_connection_keeps_autocommit_row_factory_and_foreign_keys(tmp_path):
    conn = db.connect(tmp_path / "ases.db")

    assert conn.isolation_level is None
    assert conn.row_factory is sqlite3.Row
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.in_transaction is False  # a finished migration never leaves a transaction open behind it
    conn.execute("INSERT INTO requests_ledger VALUES ('p', 'm', '2026-09-21', 1, 't')")  # autocommit: visible at once
    other = sqlite3.connect(str(tmp_path / "ases.db"))
    assert other.execute("SELECT COUNT(*) FROM requests_ledger").fetchone()[0] == 1
    other.close()


# --- the migration list -----------------------------------------------------------------------------------------

def test_migrations_are_numbered_from_one_without_gaps_and_the_version_is_derived():
    assert [m.version for m in db.MIGRATIONS] == list(range(1, len(db.MIGRATIONS) + 1))
    assert db.SCHEMA_VERSION == db.MIGRATIONS[-1].version == db.latest_version() == 11
    for migration in db.MIGRATIONS:
        assert migration.description
        assert isinstance(migration.apply, str) or callable(migration.apply)


@pytest.mark.parametrize("versions", [[], [2], [1, 3], [1, 1, 2], [2, 1], [0, 1]])
def test_a_migration_list_with_a_gap_a_repeat_or_a_bad_start_is_refused(versions):
    listing = [db.Migration(v, "x", "SELECT 1") for v in versions]

    with pytest.raises(db.MigrationError):
        db._check_migrations(listing)


def test_statement_splitter_ignores_semicolons_in_comments_and_strings_and_drops_trailing_comments():
    script = (
        "-- a comment; with a semicolon\n"
        "CREATE TABLE a (x TEXT DEFAULT 'one;two');\n"
        "\n"
        "CREATE TABLE b (y INTEGER   -- trailing; comment\n"
        ");\n"
        "-- nothing follows but this comment\n"
    )

    statements = db._statements(script)

    assert len(statements) == 2
    assert "CREATE TABLE a" in statements[0] and "'one;two'" in statements[0]
    assert "CREATE TABLE b" in statements[1]


def test_statement_splitter_does_not_cut_at_a_semicolon_that_ends_a_comment_or_a_comment_line():
    # a comment line that itself ends in a semicolon, and a column comment that does: neither may end a statement
    script = (
        "-- this comment line ends in a semicolon;\n"
        "CREATE TABLE a (\n"
        "    x INTEGER,   -- note;\n"
        "    y TEXT       -- another note;\n"
        ");\n"
        "CREATE INDEX i ON a (x);\n"
    )

    statements = db._statements(script)

    assert len(statements) == 2
    assert statements[0].count("CREATE TABLE") == 1 and "y TEXT" in statements[0] and statements[0].rstrip().endswith(");")
    assert statements[1] == "CREATE INDEX i ON a (x);"


def test_statement_splitter_keeps_a_final_statement_that_has_no_semicolon():
    assert len(db._statements("CREATE TABLE a (x INTEGER);\nCREATE TABLE b (y INTEGER)")) == 2


# --- a brand-new database ---------------------------------------------------------------------------------------

def test_fresh_database_has_every_table_and_ends_at_the_newest_version(tmp_path):
    conn = db.connect(tmp_path / "ases.db")

    assert set(_user_tables(conn)) == _ALL_TABLES
    assert db.current_version(conn) == db.latest_version() == 11
    assert _versions(conn) == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]  # one row per migration, each stamped
    assert conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE applied_at IS NULL OR applied_at = ''").fetchone()[0] == 0
    assert db.pending(conn) == []


def test_a_new_database_is_not_backed_up_and_a_current_one_is_not_written_or_backed_up_again(tmp_path):
    path = tmp_path / "ases.db"
    first = db.connect(path)
    first.close()

    again = db.connect(path)

    assert _backups(tmp_path) == []
    assert again.total_changes == 0  # nothing was inserted or updated by reconnecting to a current database
    assert _versions(again) == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
    again.close()


def test_a_zero_byte_file_is_a_new_database_and_gets_no_backup(tmp_path):
    path = tmp_path / "ases.db"
    path.write_bytes(b"")

    conn = db.connect(path)

    assert db.current_version(conn) == 11
    assert _backups(tmp_path) == []
    conn.close()


def test_connect_works_for_a_path_with_spaces_and_a_non_ascii_directory(tmp_path):
    path = tmp_path / f"my dir caf{chr(0xE9)}" / "ases.db"
    _make_old_database(path, 5)

    conn = db.connect(path)

    assert db.current_version(conn) == 11
    assert len(_backups(path.parent)) == 1
    conn.close()


# --- current_version and pending on a plain connection ----------------------------------------------------------

def test_current_version_is_zero_without_the_table_and_the_highest_row_with_gaps(tmp_path):
    raw = sqlite3.connect(str(tmp_path / "raw.db"), isolation_level=None)
    assert db.current_version(raw) == 0
    raw.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    assert db.current_version(raw) == 0
    raw.execute("INSERT INTO schema_migrations VALUES (2, 'x')")
    raw.execute("INSERT INTO schema_migrations VALUES (5, 'x')")
    assert db.current_version(raw) == 5  # the MAX: the old code wrote one row per bump, so gaps are normal
    raw.close()


def test_pending_lists_the_higher_migrations_oldest_first(tmp_path):
    path = _make_old_database(tmp_path / "ases.db", 5)
    raw = sqlite3.connect(str(path), isolation_level=None)

    assert [m.version for m in db.pending(raw)] == [6, 7, 8, 9, 10, 11]
    raw.execute("DELETE FROM schema_migrations")
    assert [m.version for m in db.pending(raw)] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
    raw.execute("INSERT INTO schema_migrations VALUES (11, 'x')")
    assert db.pending(raw) == []
    raw.close()


# --- upgrading a database made by ANY earlier ASES --------------------------------------------------------------

@pytest.mark.parametrize("old_version", [1, 2, 3, 4, 5, 6])
def test_a_database_from_any_earlier_schema_upgrades_in_place_with_its_rows_and_a_backup(tmp_path, old_version):
    path = _make_old_database(tmp_path / "ases.db", old_version)
    raw = sqlite3.connect(str(path), isolation_level=None)
    before = _snapshot(raw)
    raw.close()

    conn = db.connect(path)

    assert db.current_version(conn) == 11
    assert set(_user_tables(conn)) == _ALL_TABLES
    after = _snapshot(conn)
    for table, (columns, rows) in before.items():
        # every old row is still there with every old value (the tables only ever GAIN columns)
        old_columns = ", ".join(columns)
        kept = [tuple(r) for r in conn.execute(f"SELECT {old_columns} FROM {table} ORDER BY rowid")]
        assert kept == rows, table
    assert set(after) == set(_ALL_TABLES) - {"schema_migrations"}
    # exactly one backup, named for the version the file was at
    assert len(_backups(tmp_path)) == 1
    assert f".bak-v{old_version}-" in _backups(tmp_path)[0]
    # the version rows: the old one is kept, and each migration that ran is recorded once
    assert _versions(conn) == sorted({old_version, *range(old_version + 1, 12)})
    conn.close()


@pytest.mark.parametrize("old_version", [1, 2, 3, 4, 5, 6])
def test_an_upgraded_database_has_exactly_the_schema_of_a_brand_new_one(tmp_path, old_version):
    fresh = db.connect(tmp_path / "fresh" / "ases.db")
    upgraded = db.connect(_make_old_database(tmp_path / "old" / "ases.db", old_version))

    assert _shape(upgraded) == _shape(fresh)
    fresh.close()
    upgraded.close()


def test_the_field_database_shape_v5_upgrades_and_the_new_columns_are_null_for_old_rows(tmp_path):
    path = _make_old_database(tmp_path / "ases.db", 5)

    conn = db.connect(path)

    for table in ("gate_runs", "events"):
        columns = {r[1]: r for r in conn.execute(f"PRAGMA table_info({table})")}
        assert "project" in columns
        assert columns["project"][2].upper() == "TEXT"
        assert columns["project"][3] == 0  # nullable
        assert columns["project"][4] is None  # no default
        assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE project IS NOT NULL").fetchone()[0] == 0
    # merge_records (schema v8, migration 8) also has a nullable project column with no default, but its one
    # seeded row (task_key 'T1') is NOT null: plan_tasks names exactly one project ('p1') for that task_key, so
    # the migration's backfill step attributes the row to it instead of leaving it NULL.
    columns = {r[1]: r for r in conn.execute("PRAGMA table_info(merge_records)")}
    assert "project" in columns
    assert columns["project"][2].upper() == "TEXT"
    assert columns["project"][3] == 0  # nullable
    assert columns["project"][4] is None  # no default
    assert conn.execute(
        "SELECT project FROM merge_records WHERE task_key = 'T1'"
    ).fetchone()["project"] == "p1"
    # the leases and snapshots of version 6 exist and work, including the partial unique index
    conn.execute("INSERT INTO resource_leases (project, resource, holder, acquired_at) VALUES ('p', 'r', 'h', 't')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO resource_leases (project, resource, holder, acquired_at) VALUES ('p', 'r', 'h2', 't')")
    conn.close()


def test_upgrading_from_the_old_schema_6_changes_nothing_but_the_version_7_8_9_and_10_additions(tmp_path):
    path = _make_old_database(tmp_path / "ases.db", 6)
    raw = sqlite3.connect(str(path), isolation_level=None)
    tables_before, indexes_before = _shape(raw)
    primary_keys_before = {t: [c[0] for c in sorted((c for c in cols if c[4]), key=lambda c: c[4])]
                           for t, cols in tables_before.items()}
    raw.close()

    conn = db.connect(path)
    tables_after, indexes_after = _shape(conn)

    for table, columns in tables_before.items():
        if table == "merge_records":
            continue  # migration 8 rebuilds this one from scratch: checked on its own below
        if table in ("gate_runs", "events"):
            assert tables_after[table][:-1] == columns  # the old columns, unchanged and in order
            assert tables_after[table][-1][0] == "project"  # plus exactly one
        elif table == "usage_ingested":
            assert tables_after[table][:-7] == columns  # the old columns, unchanged and in order
            assert [c[0] for c in tables_after[table][-7:]] == [
                "board", "run_id", "mapped_by", "settled", "last_check_at", "last_check_count", "billing_provider",
            ]  # plus migration 10's own seven (round 19, package LEDGER)
        elif table == "model_registry":
            # migration 11 (round 19, package STOPGATES): one more additive column, `declared`.
            assert tables_after[table][:-1] == columns
            assert tables_after[table][-1][0] == "declared"
        elif table == "schema_migrations":
            assert tables_after[table] == columns
        else:
            assert tables_after[table] == columns, table
    # migrations 9 (round 10, BASECHECK) and 10 (round 19, LEDGER) are the exception to "nothing but additions to
    # existing tables": whole new tables, additive the same way every other migration here is (the rows old
    # tables kept, the checks below, are unaffected by a table that did not exist before).
    assert set(tables_after) - set(tables_before) == {"integrity_heads", "usage_runs", "usage_orphans"}
    assert set(indexes_after) - set(indexes_before) == {"idx_gate_runs_project_task_sha", "idx_events_kind"}
    assert {k: v for k, v in indexes_after.items() if k in indexes_before} == indexes_before

    # merge_records (migration 8): a rebuilt table, not an ALTER, so its columns are NOT the old ones plus one
    # appended -- project moves to the front (matching plan_tasks' own (project, task_key) convention) and
    # task_key gains an explicit NOT NULL it never had as a lone TEXT PRIMARY KEY (SQLite does not imply NOT NULL
    # from PRIMARY KEY alone for a non-INTEGER column). Every other old column keeps its name, type and order.
    old_mr = {c[0]: c for c in tables_before["merge_records"]}
    new_mr = {c[0]: c for c in tables_after["merge_records"]}
    assert [c[0] for c in tables_after["merge_records"]] == [
        "project", "task_key", "candidate_sha", "gate3_result", "squash_commit", "reverted", "completed_at",
    ]
    for name in ("candidate_sha", "gate3_result", "squash_commit", "reverted", "completed_at"):
        assert new_mr[name] == old_mr[name], name
    assert new_mr["task_key"][:3] == (old_mr["task_key"][0], old_mr["task_key"][1], 1)  # type same, now NOT NULL
    assert new_mr["project"] == ("project", "TEXT", 0, None, 1)  # nullable, no default, pk position 1

    primary_keys_after = {t: [c[0] for c in sorted((c for c in cols if c[4]), key=lambda c: c[4])]
                          for t, cols in tables_after.items()}
    primary_keys_before["merge_records"] = ["task_key"]  # what it always was, for the comparison below
    assert primary_keys_after["merge_records"] == ["project", "task_key"]  # the fix: no longer task_key alone
    del primary_keys_after["merge_records"], primary_keys_before["merge_records"]
    assert primary_keys_after["integrity_heads"] == ["project", "sha"]  # the new table's own key
    del primary_keys_after["integrity_heads"]
    assert primary_keys_after["usage_runs"] == ["board", "run_id"]  # the new table's own key
    del primary_keys_after["usage_runs"]
    assert primary_keys_after["usage_orphans"] == ["session_id"]  # the new table's own key
    del primary_keys_after["usage_orphans"]
    assert primary_keys_after == primary_keys_before  # no OTHER table's primary key changed
    conn.close()


def test_the_version_7_indexes_cover_the_columns_the_work_order_names(tmp_path):
    conn = db.connect(tmp_path / "ases.db")

    _, indexes = _shape(conn)

    assert indexes["idx_gate_runs_project_task_sha"][:3] == ("gate_runs", 0, ["project", "task_key", "commit_sha"])
    assert indexes["idx_events_kind"][:3] == ("events", 0, ["kind"])
    assert "WHERE released_at IS NULL" in indexes["idx_resource_leases_active"][3]  # the partial index is intact


def _make_v7_database(path, *, plan_task_rows=(), merge_rows=()):
    """A database at exactly schema version 7: `_make_old_database`'s v6 shape, plus the nullable project column
    and the two indexes v7 added (matching db.py's own `_apply_v7`), the given plan_tasks and merge_records rows,
    and a version 7 row. Built from raw SQL, not from db.py's own code, so migration 8's backfill is tested
    against a real v7 shape rather than a copy of the migration under test."""
    _make_old_database(path, 6, seed=False, record_version=False)
    raw = sqlite3.connect(str(path), isolation_level=None)
    for table in ("gate_runs", "merge_records", "events"):
        raw.execute(f"ALTER TABLE {table} ADD COLUMN project TEXT")
    raw.execute("CREATE INDEX idx_gate_runs_project_task_sha ON gate_runs (project, task_key, commit_sha)")
    raw.execute("CREATE INDEX idx_events_kind ON events (kind)")
    for project, task_key in plan_task_rows:
        raw.execute(
            "INSERT INTO plan_tasks (project, task_key, role, created_at) VALUES (?, ?, 'coder', datetime('now'))",
            (project, task_key),
        )
    for row in merge_rows:
        raw.execute(
            "INSERT INTO merge_records (task_key, project, candidate_sha, gate3_result, squash_commit, reverted, "
            "completed_at) VALUES (?, ?, ?, ?, ?, ?, ?)", row,
        )
    for version in range(1, 8):
        raw.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (?, '2026-09-19 10:00:00')", (version,))
    raw.close()
    return path


def test_migration_8_backfills_legacy_merge_records_from_plan_tasks_only_when_unambiguous(tmp_path):
    """ASES-ARC-03 / ASES-GIT-05 (the register's "a separate, still-open risk for two projects reusing a task
    key"): the migration 8 backfill step, exercised directly against a real v7 shape with every case the work
    order names -- a legacy row plan_tasks attributes to exactly one project, a row that already has one (a
    hypothetical earlier writer), one plan_tasks makes ambiguous (two projects share the task key), and one no
    plan_tasks row names at all."""
    path = _make_v7_database(
        tmp_path / "ases.db",
        plan_task_rows=[("p1", "T1"), ("p2", "T2"), ("p1", "T3"), ("p2", "T3")],  # T3: two projects share it
        merge_rows=[
            ("T1", None, "sha1", "pass", "sha1", 0, "2026-09-19T10:00:00"),  # one plan_tasks match: backfilled
            ("T2", "p2", "sha2", "pass", "sha2", 0, "2026-09-19T10:01:00"),  # already attributed: untouched
            ("T3", None, "sha3", "pass", "sha3", 1, "2026-09-19T10:02:00"),  # ambiguous (p1 AND p2): left NULL
            ("T4", None, "sha4", "fail", None, 0, None),                    # no plan_tasks row at all: left NULL
        ],
    )

    conn = db.connect(path)

    assert db.current_version(conn) == 11
    rows = {r["task_key"]: dict(r) for r in conn.execute(
        "SELECT task_key, project, candidate_sha, gate3_result, squash_commit, reverted, completed_at "
        "FROM merge_records"
    )}
    assert rows["T1"]["project"] == "p1"  # backfilled: plan_tasks names exactly one project for T1
    assert rows["T2"]["project"] == "p2"  # already attributed: left exactly as it was
    assert rows["T3"]["project"] is None  # ambiguous: plan_tasks has BOTH p1 and p2 for T3, not guessed
    assert rows["T4"]["project"] is None  # unattributable: no plan_tasks row names T4 at all
    # every other column of every row survived the rebuild untouched
    expected = {
        "T1": ("sha1", "pass", "sha1", 0, "2026-09-19T10:00:00"),
        "T2": ("sha2", "pass", "sha2", 0, "2026-09-19T10:01:00"),
        "T3": ("sha3", "pass", "sha3", 1, "2026-09-19T10:02:00"),
        "T4": ("sha4", "fail", None, 0, None),
    }
    for key, (candidate, gate3, squash, reverted, completed) in expected.items():
        row = rows[key]
        assert (row["candidate_sha"], row["gate3_result"], row["squash_commit"], row["reverted"],
                row["completed_at"]) == (candidate, gate3, squash, reverted, completed), key
    conn.close()


def test_migration_8_lets_two_projects_keep_separate_rows_for_the_same_task_key(tmp_path):
    """The bug this migration exists to fix: before it, merge_records.task_key alone was the primary key, so two
    projects reusing a task key upserted onto the SAME row. After it, PRIMARY KEY (project, task_key) makes them
    two distinct rows, proven here with a plain INSERT for each (the collision-safety mergeq.py's ON CONFLICT
    upsert relies on)."""
    path = _make_v7_database(tmp_path / "ases.db")
    conn = db.connect(path)

    conn.execute(
        "INSERT INTO merge_records (project, task_key, candidate_sha, gate3_result, squash_commit, reverted, "
        "completed_at) VALUES ('p1', 'T1', 'shaA', 'pass', 'shaA', 0, '2026-09-19T10:00:00')"
    )
    conn.execute(
        "INSERT INTO merge_records (project, task_key, candidate_sha, gate3_result, squash_commit, reverted, "
        "completed_at) VALUES ('p2', 'T1', 'shaB', 'fail', NULL, 0, NULL)"
    )  # would have collided onto the SAME row before this migration: task_key alone used to be the whole key

    rows = {r["project"]: dict(r) for r in conn.execute(
        "SELECT project, candidate_sha, gate3_result, squash_commit FROM merge_records WHERE task_key = 'T1'"
    )}
    assert set(rows) == {"p1", "p2"}
    assert rows["p1"]["candidate_sha"] == "shaA" and rows["p1"]["gate3_result"] == "pass"
    assert rows["p2"]["candidate_sha"] == "shaB" and rows["p2"]["gate3_result"] == "fail"
    conn.close()


# --- migration 9: integrity_heads, backfilled from integrity_state (round 10, package BASECHECK) ---------------

def _make_v8_database(path, *, integrity_rows=()):
    """A database at exactly schema version 8 (_make_v7_database's shape, with migration 8's own rebuild of
    merge_records actually applied so the shape matches, and a version 8 row), with the given integrity_state
    rows -- for testing migration 9's backfill into integrity_heads against a real v8 shape rather than a copy of
    the migration under test. `integrity_rows` is (project, expected_head, updated_at) tuples."""
    _make_v7_database(path)
    raw = sqlite3.connect(str(path), isolation_level=None)
    raw.row_factory = sqlite3.Row
    db._apply_v8(raw)
    raw.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (8, '2026-09-19 10:00:00')")
    for project, sha, updated_at in integrity_rows:
        raw.execute(
            "INSERT INTO integrity_state (project, expected_head, updated_at) VALUES (?, ?, ?)",
            (project, sha, updated_at),
        )
    raw.close()
    return path


def test_migration_9_creates_an_empty_integrity_heads_table(tmp_path):
    path = _make_v8_database(tmp_path / "ases.db")

    conn = db.connect(path)

    assert db.current_version(conn) == 11
    assert conn.execute("SELECT COUNT(*) FROM integrity_heads").fetchone()[0] == 0
    conn.close()


def test_migration_9_backfills_every_projects_current_expected_head(tmp_path):
    """A database that already has a current expected_head (integrity_state, schema v4) predates integrity_heads,
    and its last-adopted head is real ASES-written history either way: it must not start this project's set
    looking as if ASES had never written anything, which would make its own most recent head look unverified."""
    path = _make_v8_database(tmp_path / "ases.db", integrity_rows=[
        ("p1", "a" * 40, "2026-09-19T10:00:00"),
        ("p2", "b" * 40, "2026-09-19T10:01:00"),
    ])

    conn = db.connect(path)

    rows = {tuple(r) for r in conn.execute("SELECT project, sha, recorded_at FROM integrity_heads")}
    assert rows == {
        ("p1", "a" * 40, "2026-09-19T10:00:00"), ("p2", "b" * 40, "2026-09-19T10:01:00"),
    }
    assert guards.written_heads(conn, "p1") == {"a" * 40}
    assert guards.written_heads(conn, "p2") == {"b" * 40}
    conn.close()


def test_migration_9_backfill_is_idempotent_on_a_reconnect(tmp_path):
    path = _make_v8_database(tmp_path / "ases.db", integrity_rows=[("p1", "a" * 40, "2026-09-19T10:00:00")])
    db.connect(path).close()

    conn = db.connect(path)  # a second connect must not fail on the same (project, sha) row again

    assert conn.execute("SELECT COUNT(*) FROM integrity_heads").fetchone()[0] == 1
    conn.close()


# --- migration 10: usage_ingested gains board/run_id/mapped_by/settled/..., usage_runs, usage_orphans (round 19,
# package LEDGER; ASES-CAP-02, ASES-CAP-03, ASES-RTE-01, ASES-MOD-05) -------------------------------------------

def _make_v9_database(path, *, integrity_rows=(), usage_rows=()):
    """A database at exactly schema version 9: `_make_v8_database`'s shape, migration 9's own integrity_heads
    backfill actually applied so the shape matches, the given LEGACY-shaped usage_ingested rows (before migration
    10 added its columns), and a version 9 row -- for testing migration 10 against a real v9 shape rather than a
    copy of the migration under test. `usage_rows` is (session_id, profile, provider, model, requests,
    input_tokens, output_tokens, ingested_at, project, task_key, card_id) tuples."""
    _make_v8_database(path, integrity_rows=integrity_rows)
    raw = sqlite3.connect(str(path), isolation_level=None)
    db._apply_v9(raw)
    raw.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (9, '2026-09-19 10:00:00')")
    for row in usage_rows:
        raw.execute(
            "INSERT INTO usage_ingested (session_id, profile, provider, model, requests, input_tokens, "
            "output_tokens, ingested_at, project, task_key, card_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            row,
        )
    raw.close()
    return path


def test_migration_10_adds_columns_and_tables_and_marks_legacy_rows_unsettled(tmp_path):
    path = _make_v9_database(tmp_path / "ases.db", usage_rows=[
        ("s_legacy", "coder-1", "openrouter", "m1", 4, 100, 50, "2026-09-19T10:03:00", "p1", "T1", "t_w1"),
    ])

    conn = db.connect(path)

    assert db.current_version(conn) == 11
    columns = [r[1] for r in conn.execute("PRAGMA table_info(usage_ingested)")]
    for name in ("board", "run_id", "mapped_by", "settled", "last_check_at", "last_check_count", "billing_provider"):
        assert name in columns
    row = conn.execute(
        "SELECT settled, board, run_id, mapped_by, last_check_at, last_check_count, billing_provider "
        "FROM usage_ingested WHERE session_id = 's_legacy'"
    ).fetchone()
    assert row["settled"] == 0  # legacy rows start unsettled, so settle_open_sessions tops them up
    assert (row["board"], row["run_id"], row["mapped_by"], row["last_check_at"], row["last_check_count"],
            row["billing_provider"]) == (None, None, None, None, None, None)
    assert conn.execute("SELECT COUNT(*) FROM usage_runs").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM usage_orphans").fetchone()[0] == 0
    info = conn.execute("PRAGMA table_info(usage_runs)").fetchall()
    assert {r[1] for r in info} == {
        "board", "run_id", "card_id", "profile", "project", "task_key", "run_started_at", "run_ended_at", "state",
        "updated_at",
    }
    conn.close()


def test_migration_10_is_idempotent_on_a_reconnect(tmp_path):
    path = _make_v9_database(tmp_path / "ases.db", usage_rows=[
        ("s1", "coder-1", "openrouter", "m1", 4, 100, 50, "2026-09-19T10:03:00", "p1", "T1", "t_w1"),
    ])
    db.connect(path).close()

    conn = db.connect(path)

    assert db.current_version(conn) == 11
    assert conn.execute("SELECT COUNT(*) FROM usage_ingested").fetchone()[0] == 1
    conn.close()


def test_a_schema_4_database_whose_usage_table_predates_the_attribution_columns_gets_them(tmp_path):
    path = _make_old_database(tmp_path / "ases.db", 4)
    raw = sqlite3.connect(str(path), isolation_level=None)
    assert [r[1] for r in raw.execute("PRAGMA table_info(usage_ingested)")][-1] == "ingested_at"
    raw.close()

    conn = db.connect(path)

    columns = [r[1] for r in conn.execute("PRAGMA table_info(usage_ingested)")]
    assert columns[-10:-7] == ["project", "task_key", "card_id"]  # migration 5's three, before migration 10's own seven
    conn.close()


def test_a_database_whose_version_row_is_missing_is_repaired_by_connecting(tmp_path):
    # tables and rows present, but the crash came before the version row: version 0, every migration runs over
    # objects that already exist and must be a no-op for them
    path = _make_old_database(tmp_path / "ases.db", 5, record_version=False)
    raw = sqlite3.connect(str(path), isolation_level=None)
    before = _snapshot(raw)
    raw.close()

    conn = db.connect(path)

    assert db.current_version(conn) == 11
    assert _versions(conn) == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
    for table, (columns, rows) in before.items():
        kept = [tuple(r) for r in conn.execute(f"SELECT {', '.join(columns)} FROM {table} ORDER BY rowid")]
        assert kept == rows, table
    assert len(_backups(tmp_path)) == 1 and ".bak-v0-" in _backups(tmp_path)[0]
    conn.close()


def test_a_project_column_that_already_exists_does_not_fail_migration_7(tmp_path):
    path = _make_old_database(tmp_path / "ases.db", 6)
    raw = sqlite3.connect(str(path), isolation_level=None)
    raw.execute("ALTER TABLE gate_runs ADD COLUMN project TEXT")
    raw.close()

    conn = db.connect(path)

    assert [r[1] for r in conn.execute("PRAGMA table_info(gate_runs)")].count("project") == 1
    assert db.current_version(conn) == 11
    conn.close()


def test_reconnecting_an_upgraded_database_is_idempotent(tmp_path):
    path = _make_old_database(tmp_path / "ases.db", 5)
    first = db.connect(path)
    shape = _shape(first)
    versions = _versions(first)
    first.close()

    for _ in range(3):
        again = db.connect(path)
        assert _shape(again) == shape
        assert _versions(again) == versions
        assert again.total_changes == 0
        again.close()

    assert len(_backups(tmp_path)) == 1  # only the one real upgrade made a backup


# --- the backup -------------------------------------------------------------------------------------------------

def test_backup_path_is_next_to_the_database_and_names_the_version_and_a_utc_timestamp(tmp_path):
    moment = datetime(2026, 9, 21, 10, 15, 0, tzinfo=timezone.utc)

    assert db.backup_path(tmp_path / "ases.db", 5, now=moment) == tmp_path / "ases.db.bak-v5-20260921T101500Z"
    assert db.backup_path(str(tmp_path / "x.db"), 0, now=moment).name == "x.db.bak-v0-20260921T101500Z"


def test_backup_path_converts_an_aware_time_to_utc_and_reads_a_naive_one_as_utc(tmp_path):
    from datetime import timedelta
    plus_two = timezone(timedelta(hours=2))

    aware = db.backup_path(tmp_path / "ases.db", 5, now=datetime(2026, 9, 21, 12, 0, 0, tzinfo=plus_two))
    naive = db.backup_path(tmp_path / "ases.db", 5, now=datetime(2026, 9, 21, 10, 0, 0))

    assert aware.name == naive.name == "ases.db.bak-v5-20260921T100000Z"


def test_backup_path_default_time_is_now_in_utc(tmp_path):
    name = db.backup_path(tmp_path / "ases.db", 3).name

    assert name.startswith("ases.db.bak-v3-") and name.endswith("Z")
    assert len(name) == len("ases.db.bak-v3-20260921T101500Z")


def test_the_backup_is_a_whole_single_file_at_the_old_version_with_the_old_rows(tmp_path):
    path = _make_old_database(tmp_path / "ases.db", 5)

    conn = db.connect(path)
    conn.close()

    names = sorted(p.name for p in tmp_path.iterdir() if ".bak-" in p.name)
    assert len(names) == 1  # no -wal, -shm or .tmp beside it
    backup = sqlite3.connect(str(tmp_path / names[0]))
    assert backup.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 5  # the state BEFORE the upgrade
    assert backup.execute("SELECT count FROM requests_ledger").fetchone()[0] == 7
    assert backup.execute("SELECT name FROM sqlite_master WHERE name = 'resource_leases'").fetchone() is None
    assert backup.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
    assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    backup.close()
    assert sorted(p.name for p in tmp_path.iterdir() if ".bak-" in p.name) == names  # opening it left no sidecar


def test_backups_are_pruned_to_the_newest_five_by_timestamp(tmp_path):
    path = _make_old_database(tmp_path / "ases.db", 5)
    # six older backups (mixed versions, so the order must come from the timestamp and not the version), a partial
    # copy, a note, and another database's backups: none of the last three kinds may be counted or removed
    fakes = {
        "ases.db.bak-v3-20200101T000000Z": None, "ases.db.bak-v5-20200102T000000Z": None,
        "ases.db.bak-v4-20200103T000000Z": None, "ases.db.bak-v6-20200104T000000Z": None,
        "ases.db.bak-v2-20200105T000000Z": None, "ases.db.bak-v5-20200106T000000Z": None,
    }
    others = ["ases.db.bak-v5-20200101T000000Z.99.tmp", "ases.db.bak-notes.txt",
              *[f"other.db.bak-v1-2019010{i}T000000Z" for i in range(1, 8)]]
    for name in [*fakes, *others]:
        (tmp_path / name).write_bytes(b"x")

    conn = db.connect(path)
    conn.close()

    remaining = _backups(tmp_path)
    real = [n for n in remaining if n.startswith("ases.db.bak-v") and not n.endswith(".tmp")]
    assert len(real) == db.BACKUPS_KEPT == 5
    assert "ases.db.bak-v3-20200101T000000Z" not in real and "ases.db.bak-v5-20200102T000000Z" not in real
    for kept in ("ases.db.bak-v4-20200103T000000Z", "ases.db.bak-v6-20200104T000000Z",
                 "ases.db.bak-v2-20200105T000000Z", "ases.db.bak-v5-20200106T000000Z"):
        assert kept in real
    new = [n for n in real if n not in fakes]
    assert len(new) == 1 and new[0].startswith("ases.db.bak-v5-") and new[0].split("-")[-1] > "2021"  # the fifth
    for untouched in others:
        assert (tmp_path / untouched).exists(), untouched


def test_a_same_second_backup_another_process_already_made_is_accepted_even_if_the_rename_is_refused(tmp_path, monkeypatch):
    # Found by the multi-process test on Windows: two upgrades in one second aim at the same backup name, and the rename
    # of the second onto the first fails with "access denied". A file under that name is a finished copy, so it is enough.
    path = _make_old_database(tmp_path / "ases.db", 5)
    target = tmp_path / "ases.db.bak-v5-20260921T101500Z"
    target.write_bytes(b"a whole backup made by the other process")
    monkeypatch.setattr(db, "backup_path", lambda p, v, now=None: target)

    def refused(src, dst):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(db.os, "replace", refused)

    conn = db.connect(path)

    assert db.current_version(conn) == 11  # the upgrade went ahead: a backup exists
    assert target.read_bytes() == b"a whole backup made by the other process"  # theirs was left alone
    assert not list(tmp_path.glob("*.tmp"))  # and ours was not left behind
    conn.close()


def test_a_rename_that_fails_with_no_backup_under_that_name_is_reported_and_nothing_is_migrated(tmp_path, monkeypatch):
    path = _make_old_database(tmp_path / "ases.db", 5)

    def refused(src, dst):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(db.os, "replace", refused)

    with pytest.raises(db.MigrationError) as info:
        db.connect(path)

    assert "could not back up" in str(info.value) and info.value.version == 5
    raw = sqlite3.connect(str(path), isolation_level=None)
    assert db.current_version(raw) == 5
    raw.close()
    assert _backups(tmp_path) == [] and not list(tmp_path.glob("*.tmp"))


def test_a_backup_that_cannot_be_made_refuses_the_migration_and_changes_nothing(tmp_path, monkeypatch):
    path = _make_old_database(tmp_path / "ases.db", 5)
    monkeypatch.setattr(db, "backup_path", lambda p, v, now=None: tmp_path / "no_such_dir" / "backup.db")

    with pytest.raises(db.MigrationError) as info:
        db.connect(path)

    assert "back up" in str(info.value) and info.value.version == 5
    raw = sqlite3.connect(str(path), isolation_level=None)
    assert db.current_version(raw) == 5  # not migrated: no backup, no migration
    assert raw.execute("SELECT name FROM sqlite_master WHERE name = 'resource_leases'").fetchone() is None
    raw.close()


# --- a failing migration ----------------------------------------------------------------------------------------

def _boom(conn):
    conn.execute("CREATE TABLE half_done (a INTEGER)")
    conn.execute("INSERT INTO half_done VALUES (1)")
    raise RuntimeError("boom")


def test_a_failing_migration_rolls_back_leaves_the_version_and_raises_naming_it(tmp_path, monkeypatch):
    path = tmp_path / "ases.db"
    db.connect(path).close()
    monkeypatch.setattr(db, "MIGRATIONS", [*db.MIGRATIONS, db.Migration(12, "will fail", _boom)])

    with pytest.raises(db.MigrationError) as info:
        db.connect(path)

    assert info.value.version == 12
    assert "migration 12" in str(info.value) and "boom" in str(info.value) and "version 11" in str(info.value)
    assert isinstance(info.value.__cause__, RuntimeError)
    raw = sqlite3.connect(str(path), isolation_level=None)
    assert db.current_version(raw) == 11  # the version stayed where it was
    assert raw.execute("SELECT name FROM sqlite_master WHERE name = 'half_done'").fetchone() is None  # rolled back
    assert _versions(raw) == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
    raw.close()


def test_a_migration_written_as_sql_is_atomic_across_its_statements(tmp_path, monkeypatch):
    path = tmp_path / "ases.db"
    db.connect(path).close()
    # the first statement works and the second cannot: the whole migration must vanish
    broken = db.Migration(12, "sql that fails half way", "CREATE TABLE t_one (a INTEGER); CREATE TABLE t_one (b INTEGER);")
    monkeypatch.setattr(db, "MIGRATIONS", [*db.MIGRATIONS, broken])

    with pytest.raises(db.MigrationError) as info:
        db.connect(path)

    assert info.value.version == 12
    raw = sqlite3.connect(str(path), isolation_level=None)
    assert raw.execute("SELECT name FROM sqlite_master WHERE name = 't_one'").fetchone() is None
    assert db.current_version(raw) == 11
    raw.close()


def test_each_migration_has_its_own_transaction_so_earlier_ones_survive_a_later_failure(tmp_path, monkeypatch):
    path = tmp_path / "ases.db"
    monkeypatch.setattr(db, "MIGRATIONS", [*db.MIGRATIONS[:3], db.Migration(4, "fails", _boom)])

    with pytest.raises(db.MigrationError) as info:
        db.connect(path)

    assert info.value.version == 4
    raw = sqlite3.connect(str(path), isolation_level=None)
    assert _versions(raw) == [1, 2, 3]  # committed one by one: 1 to 3 stay, 4 is gone
    assert "plan_tasks" in _user_tables(raw) and "gate_pins" in _user_tables(raw)
    assert "half_done" not in _user_tables(raw)
    raw.close()


def test_a_failed_upgrade_can_be_retried_once_the_migration_is_fixed(tmp_path, monkeypatch):
    path = tmp_path / "ases.db"
    db.connect(path).close()
    good = list(db.MIGRATIONS)
    monkeypatch.setattr(db, "MIGRATIONS", [*good, db.Migration(12, "will fail", _boom)])
    with pytest.raises(db.MigrationError):
        db.connect(path)

    monkeypatch.setattr(db, "MIGRATIONS", [*good, db.Migration(12, "fixed", "CREATE TABLE half_done (a INTEGER)")])
    conn = db.connect(path)

    assert db.current_version(conn) == 12
    assert "half_done" in _user_tables(conn)
    conn.close()


def test_a_failed_migration_closes_the_connection_so_the_file_can_be_removed(tmp_path, monkeypatch):
    path = tmp_path / "ases.db"
    db.connect(path).close()
    monkeypatch.setattr(db, "MIGRATIONS", [*db.MIGRATIONS, db.Migration(12, "will fail", _boom)])
    with pytest.raises(db.MigrationError):
        db.connect(path)

    # on Windows a file with an open handle cannot be deleted, so this is a real check that nothing leaked
    for suffix in ("", "-wal", "-shm"):
        target = pathlib.Path(str(path) + suffix)
        if target.exists():
            target.unlink()
    assert not path.exists()


def test_a_migration_already_applied_by_another_process_is_skipped_not_repeated(tmp_path):
    path = _make_old_database(tmp_path / "ases.db", 6)
    raw = sqlite3.connect(str(path), isolation_level=None)
    seventh = next(m for m in db.MIGRATIONS if m.version == 7)

    db._apply(raw, seventh)
    db._apply(raw, seventh)  # what a second process sees after waiting for the write lock: already there

    assert _versions(raw).count(7) == 1
    assert raw.in_transaction is False
    raw.close()


def test_processes_upgrading_the_same_database_at_the_same_time_all_succeed(tmp_path):
    path = _make_old_database(tmp_path / "ases.db", 5)
    go = tmp_path / "go"
    src = str(pathlib.Path(db.__file__).resolve().parents[1])
    code = (
        "import os, sys, time\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from ases import db\n"
        "while not os.path.exists(sys.argv[3]):\n"
        "    time.sleep(0.001)\n"
        "conn = db.connect(sys.argv[2])\n"
        "print(db.current_version(conn))\n"
        "conn.close()\n"
    )
    procs = [
        subprocess.Popen([sys.executable, "-c", code, src, str(path), str(go)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(4)
    ]
    time.sleep(1.5)  # let every child finish importing and reach the wait loop
    go.write_text("go", encoding="utf-8")
    outputs = [(p.communicate(timeout=120), p.returncode) for p in procs]

    for (out, err), code_ in outputs:
        assert code_ == 0, err
        assert out.strip() == "11"
    raw = sqlite3.connect(str(path), isolation_level=None)
    assert _versions(raw) == [5, 6, 7, 8, 9, 10, 11]  # applied once each: a double apply would have failed on the primary key
    assert set(_user_tables(raw)) == _ALL_TABLES
    raw.close()
    assert len(_backups(tmp_path)) >= 1
    assert not list(tmp_path.glob("*.tmp"))  # no partial copy left behind


# --- a database NEWER than this code ----------------------------------------------------------------------------

def test_a_newer_database_is_refused_untouched_and_never_downgraded(tmp_path):
    path = tmp_path / "ases.db"
    raw = sqlite3.connect(str(path), isolation_level=None)
    raw.execute("PRAGMA journal_mode=WAL")
    raw.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    raw.execute("INSERT INTO schema_migrations VALUES (99, '2030-01-01 00:00:00')")
    raw.close()
    before = path.read_bytes()

    with pytest.raises(db.MigrationError) as info:
        db.connect(path)

    assert info.value.version == 99
    assert "99" in str(info.value) and "newer" in str(info.value) and str(db.latest_version()) in str(info.value)
    assert path.read_bytes() == before  # not one byte changed
    assert _backups(tmp_path) == []
    check = sqlite3.connect(str(path), isolation_level=None)
    assert _user_tables(check) == ["schema_migrations"]  # no table was added
    assert _versions(check) == [99]  # and the version was not lowered
    check.close()
    for suffix in ("", "-wal", "-shm"):  # the refused connection was closed: the file can be deleted
        target = pathlib.Path(str(path) + suffix)
        if target.exists():
            target.unlink()


def test_a_database_one_version_ahead_is_refused_and_one_at_the_newest_is_accepted(tmp_path):
    path = tmp_path / "ases.db"
    db.connect(path).close()
    raw = sqlite3.connect(str(path), isolation_level=None)
    raw.execute("INSERT INTO schema_migrations VALUES (12, 'x')")
    raw.close()

    with pytest.raises(db.MigrationError) as info:
        db.connect(path)

    assert info.value.version == 12
