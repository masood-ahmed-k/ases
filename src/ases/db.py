"""SQLite persistence for ASES (section 9.1: db.py; phase 9, "database migrations").

One connection helper, WAL mode, and a numbered list of schema migrations. Every ASES record is keyed by
provider/model, or by Hermes card ID and commit SHA (ASES-ARC-03) -- this module only owns the connection and the
schema; callers hold their own cursors per call, and every write outside a migration is a single autocommit
statement or an explicit transaction the caller controls.

How the schema is built. MIGRATIONS is an ordered list, version 1..N with no gaps. `connect()` reads the database's
version (the highest row in schema_migrations, 0 for a new file) and applies every later migration in order, each in
its OWN transaction that also records `(version, applied_at)`, so a migration that fails leaves the database exactly
where it was and raises MigrationError naming the version. Before the first pending migration touches an existing,
non-empty database file, the file is copied with the SQLite backup API (never a file copy: the database is in WAL
mode and a plain copy of a live WAL database can be torn) to `<name>.bak-v<from>-<UTC timestamp>` next to it, and only
the newest BACKUPS_KEPT such backups are kept. A database NEWER than the highest known version is refused with a
MigrationError and is not modified: an older ASES must never open, let alone downgrade, what a newer one wrote.

Why every migration is idempotent. Before 2026-09-21 the schema was one big CREATE TABLE IF NOT EXISTS script run on
every connect plus a column-adding step, and only the version of the code that last opened the file was recorded (one
row per bump, so a database made before then has GAPS: a file "at version 5" may hold just the row 5). The numbered
steps below are those same statements split by the version that introduced them. Each is written so that running it on
a database that already has its objects is a no-op (IF NOT EXISTS, and a column is added only when missing), which is
what lets a schema 1 to 6 database made by the old code upgrade in place with its rows intact, and lets a database
whose version row is missing (a crash between the tables and the row) be repaired by simply connecting again.

Adding a migration: append a Migration with the next number (never edit or reorder an existing one, a database in the
field has already run it), keep it additive where you can, and add a test that builds the previous schema as plain text
and upgrades it. Version 7 is the first migration written for the new mechanism; it only makes room (a nullable
`project` column and two indexes) and changes no primary key, so the readers and writers can be updated later.
"""
from __future__ import annotations

import dataclasses
import os
import pathlib
import re
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Callable

# The newest backups of one database kept next to it (older ones are removed when a new one is made).
BACKUPS_KEPT = 5


class MigrationError(RuntimeError):
    """The database could not be brought to the version this ASES needs. `version` is the migration that failed, or
    (for a refused newer database) the version the file is at; None when the problem is not one migration."""

    def __init__(self, message: str, *, version: int | None = None):
        super().__init__(message)
        self.version = version


@dataclasses.dataclass(frozen=True)
class Migration:
    """One numbered schema step. `apply` is either SQL text (one or more statements, run one by one inside the
    migration's transaction) or a function taking the connection, for a step plain SQL cannot express (adding a column
    only when it is missing)."""
    version: int
    description: str
    apply: Callable[[sqlite3.Connection], None] | str


# ---------------------------------------------------------------------------------------------
# The schema, split by the version that introduced each object. The statements are the ones the old single
# script held (same text, same comments), so a new database and an upgraded one end up identical.
# ---------------------------------------------------------------------------------------------

# Version 1 (phase 0/1): the migration table itself, the request ledger, the model registry, the event log.
_V1_SQL = """
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
"""

# Version 2 (phase 3): one plan task becomes a work card (W) and a merge card (M) (ASES-TSK-01).
_V2_SQL = """
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
"""

# Version 3: ASES-QG-02, the gate profile content hash pinned at this project's last `swarm approve`, checked again by
# `swarm run` so a diff that quietly changes gate configuration is refused, not trusted.
_V3_SQL = """
CREATE TABLE IF NOT EXISTS gate_pins (
    project TEXT PRIMARY KEY,
    gate_profiles_hash TEXT NOT NULL,
    pinned_at TEXT NOT NULL
);
"""

# Version 4 (2026-09-19). One row per Hermes worker session whose usage has been counted against the request ledger,
# so ingesting the same session twice cannot double count it (ASES-CAP-03); the reviewer's verdict, stored by commit
# SHA (ASES-REV-06, ASES-GIT-03: a verdict belongs to one commit); and the primary checkout HEAD ASES itself last wrote
# or verified, per project (ASES-GIT-12): a HEAD that differs from this, or a dirty primary checkout, was changed by
# something other than the controller.
_V4_SQL = """
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

CREATE TABLE IF NOT EXISTS integrity_state (
    project TEXT PRIMARY KEY,
    expected_head TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

# Columns added to usage_ingested AFTER the table first shipped (attribution to a plan task, ASES-REC-02). They are not
# in the CREATE statement above because a table an earlier build already made cannot gain a column that way.
_USAGE_COLUMNS = (
    ("usage_ingested", "project", "TEXT"),
    ("usage_ingested", "task_key", "TEXT"),
    ("usage_ingested", "card_id", "TEXT"),
)

# Version 5 (2026-09-19). Lineage counters per plan task (ASES-REC-02): every fix card starts a new card-level counter,
# so review rounds, capability and infrastructure failures and re-plans are counted per TASK; the project-level state
# for the global bounds (ASES-CTL-01): when it started, the wall-clock deadline set at Gate P, how many re-plans were
# spent, and why a stopped project stopped; and the intent and completion records for every multi-step action
# (blueprint section 19.4): create cards, run a gate, build a candidate, fast-forward, complete a merge card, revert.
# An intent with no completed_at is what reconcile-on-start looks for after a crash.
_V5_SQL = """
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

CREATE TABLE IF NOT EXISTS project_state (
    project TEXT PRIMARY KEY,
    started_at TEXT,
    deadline_at TEXT,
    replans INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'planning',   -- planning | running | paused | stopped | finished
    stop_reason TEXT,
    updated_at TEXT NOT NULL
);

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

# Version 6 (2026-09-19). Leases on shared resources (ASES-GIT-14): a per-card port block, compose project name,
# database name and temp directory, and singletons such as a shared development database taken through a lock.
# A lease is active while released_at is NULL; the partial unique index makes a second active lease on the same
# resource impossible at the database level, not just in application code. And snapshots of worktrees that no running
# card owns (ASES-GIT-12): only a running card's worker may change its worktree, so a HEAD or a status that moved in
# any other worktree was changed by something else.
_V6_SQL = """
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

CREATE TABLE IF NOT EXISTS worktree_snapshots (
    project TEXT NOT NULL,
    path TEXT NOT NULL,
    head TEXT NOT NULL,
    status_hash TEXT NOT NULL,     -- sha256 of `git status --porcelain -z` output
    taken_at TEXT NOT NULL,
    PRIMARY KEY (project, path)
);
"""

# Version 7 (phase 9). ADDITIVE ONLY: room for the project a row belongs to on the three tables that were keyed by task
# key alone, plus the two indexes the readers will want. Nothing writes the new columns yet (each caller's owner does
# that later); a NULL means "written before this column existed". No primary key changes.
_PROJECT_COLUMNS = (
    ("gate_runs", "project", "TEXT"),
    ("merge_records", "project", "TEXT"),
    ("events", "project", "TEXT"),
)
_V7_SQL = """
CREATE INDEX IF NOT EXISTS idx_gate_runs_project_task_sha ON gate_runs (project, task_key, commit_sha);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events (kind);
"""

# Version 8 (phase 9, ASES-ARC-03 / ASES-GIT-05: "a separate, still-open risk for two projects reusing a task
# key" -- the register's own words for this bug). merge_records' primary key was task_key alone since schema 1:
# two projects that share this database and both run a task keyed 'T1' upsert onto the SAME row, silently
# overwriting each other's candidate_sha, gate3_result, squash_commit, reverted and completed_at. Schema v7 added
# a nullable project column but changed no key (round 6's CORE package: "task_key is still merge_records' only
# primary key, so this cannot stop two projects that reuse a task_key from upserting onto the SAME row"). SQLite
# cannot ALTER a primary key in place, so the table is rebuilt.
#
# The new key is (project, task_key). `project` stays NULLABLE, deliberately NOT NOT NULL: a NULL row means "no
# project is recorded for this merge" (a row that predates this migration and could not be attributed, or a
# caller that still does not pass one), and every other reader of this table written so far treats a NULL
# project as "matches any project, never orphaned" -- gates.last_gate_result's own `(project IS NULL OR
# project = ?)` pattern, which hardening.py's _squash_proof independently converged on for this very table. Some
# NULL is safe to keep exactly because two projects' REAL, distinct names can never collide with each other or
# with NULL; only two NULL rows sharing a task_key would be a problem, and the old schema already made that
# impossible (task_key alone was unique, so at most one row per task_key exists to carry forward).
#
# What NULL is NOT safe for is ON CONFLICT dispatch on a FUTURE write: SQLite indexes -- and so ON CONFLICT --
# treat every NULL as distinct from every other NULL ("NULLs never collide"), proven empirically before writing
# this migration: two `INSERT ... ON CONFLICT(project, task_key) DO UPDATE` calls with project both NULL and the
# same task_key do NOT conflict, they insert two rows. A caller that never names a project would therefore
# accumulate a new row on every candidate build instead of resetting the one it already has (ASES-REC-04). Every
# writer in mergeq.py and reconcile.py is updated in the same change: a real project uses ON CONFLICT(project,
# task_key) exactly as a single-column key used to; a NULL project is found or created by hand (an explicit
# UPDATE ... WHERE project IS NULL, an INSERT only when that matched nothing), so a NULL-project task_key still
# resets in place, exactly as it did when task_key alone was the whole primary key.
#
# Legacy rows (project NULL before this migration runs) are backfilled from plan_tasks, which has been keyed
# (project, task_key) since schema 2: a task_key that plan_tasks attributes to exactly one project is confidently
# backfilled to it; a task_key with zero or more than one plan_tasks project is left NULL rather than guessed
# (report.py, finalgates.py, reconcile.py and evalkit/codetasks.py, the other readers this package owns, are all
# updated to scope their own reads by project with the same NULL-tolerant pattern).
_V8_MERGE_RECORDS_SQL = """
CREATE TABLE merge_records_v8_new (
    project TEXT,
    task_key TEXT NOT NULL,
    candidate_sha TEXT,
    gate3_result TEXT,
    squash_commit TEXT,
    reverted INTEGER NOT NULL DEFAULT 0,
    completed_at TEXT,
    PRIMARY KEY (project, task_key)
);
"""


def _apply_v8(conn: sqlite3.Connection) -> None:
    conn.execute(_V8_MERGE_RECORDS_SQL)
    rows = conn.execute(
        "SELECT task_key, project, candidate_sha, gate3_result, squash_commit, reverted, completed_at "
        "FROM merge_records"
    ).fetchall()
    for row in rows:
        project = row["project"]
        if project is None:
            matches = [
                r[0] for r in conn.execute(
                    "SELECT DISTINCT project FROM plan_tasks WHERE task_key = ?", (row["task_key"],),
                ).fetchall()
            ]
            if len(matches) == 1:
                project = matches[0]
        conn.execute(
            "INSERT INTO merge_records_v8_new (project, task_key, candidate_sha, gate3_result, squash_commit, "
            "reverted, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (project, row["task_key"], row["candidate_sha"], row["gate3_result"], row["squash_commit"],
             row["reverted"], row["completed_at"]),
        )
    conn.execute("DROP TABLE merge_records")
    conn.execute("ALTER TABLE merge_records_v8_new RENAME TO merge_records")


# Version 9 (round 10, package BASECHECK; ASES-GIT-01, ASES-GIT-16, blueprint p169's second sentence: "Phase 3 MUST
# verify the actual base commit before a worker starts"). guards.check_card_base needs the FULL history of every
# primary-checkout HEAD ASES itself has adopted, fast-forwarded to or reverted to for a project, not just the
# CURRENT one integrity_state (schema v4) keeps: a card dispatched before a later merge legitimately has an older
# head as its base. guards.set_expected_head now writes here too, every time it writes integrity_state.
_V9_SQL = """
CREATE TABLE IF NOT EXISTS integrity_heads (
    project TEXT NOT NULL,
    sha TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (project, sha)
);
"""


def _apply_v9(conn: sqlite3.Connection) -> None:
    _run_script(conn, _V9_SQL)
    # Backfill: a database that already has a current expected_head (integrity_state, schema v4) predates
    # integrity_heads and would otherwise start this project's history empty, making its own last-adopted head
    # look like one ASES never wrote. The current head is real ASES-written history either way, so it is carried
    # forward as this project's first known entry.
    for row in conn.execute("SELECT project, expected_head, updated_at FROM integrity_state").fetchall():
        conn.execute(
            "INSERT INTO integrity_heads (project, sha, recorded_at) VALUES (?, ?, ?) "
            "ON CONFLICT(project, sha) DO NOTHING",
            (row["project"], row["expected_head"], row["updated_at"]),
        )


# The one table the runner needs before it can read a version. Also part of migration 1, so the list describes the
# whole schema.
_BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
)
"""


def _statements(script: str) -> list[str]:
    """Split SQL text into single statements. `sqlite3.complete_statement` decides where one ends, so a semicolon
    inside a comment or a string does not cut it short. Comment-only text after the last statement is dropped."""
    found: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            found.append(buffer.strip())
            buffer = ""
    leftover = [ln.strip() for ln in buffer.splitlines() if ln.strip() and not ln.strip().startswith("--")]
    if leftover:
        found.append(buffer.strip())
    return found


def _run_script(conn: sqlite3.Connection, script: str) -> None:
    """Execute SQL text statement by statement. NOT executescript(): that commits the transaction it is called in,
    which would end the migration's transaction after the first script."""
    for statement in _statements(script):
        conn.execute(statement)


def _add_column(conn: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
    """ALTER TABLE ... ADD COLUMN, but only when the column is not there yet (SQLite has no IF NOT EXISTS for it)."""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def _apply_v4(conn: sqlite3.Connection) -> None:
    _run_script(conn, _V4_SQL)
    for table, column, declaration in _USAGE_COLUMNS:
        _add_column(conn, table, column, declaration)


def _apply_v5(conn: sqlite3.Connection) -> None:
    _run_script(conn, _V5_SQL)
    # The attribution columns first shipped inside the schema-5 round, so a database that an intermediate schema-4
    # build left behind can still lack them. Ensuring them here again is a no-op everywhere else.
    for table, column, declaration in _USAGE_COLUMNS:
        _add_column(conn, table, column, declaration)


def _apply_v7(conn: sqlite3.Connection) -> None:
    for table, column, declaration in _PROJECT_COLUMNS:
        _add_column(conn, table, column, declaration)
    _run_script(conn, _V7_SQL)


def _apply_v10(conn: sqlite3.Connection) -> None:
    """STOPDOC.md item 7 (round 19, package STOPGATES): models.sync_from_config used to DELETE a
    model_registry row the moment config/models.yaml stopped declaring it, taking a real recorded smoke test
    with it. `declared` distinguishes "config still names this row" from "a human or an evaluation once
    recorded facts about it": sync_from_config now only flips it to 0 (and models.list_models filters it out,
    so a person still sees exactly the rows config declares), never deletes. DEFAULT 1 on the ALTER TABLE
    means every row an upgraded database already has (by definition still declared, since the old code would
    already have deleted anything that was not) starts out correctly marked."""
    _add_column(conn, "model_registry", "declared", "INTEGER NOT NULL DEFAULT 1")


MIGRATIONS: list[Migration] = [
    Migration(1, "baseline: schema_migrations, requests_ledger, model_registry, events", _V1_SQL),
    Migration(2, "phase 3: plan_tasks, gate_runs, merge_records", _V2_SQL),
    Migration(3, "gate_pins (ASES-QG-02)", _V3_SQL),
    Migration(4, "usage_ingested (and its attribution columns), review_verdicts, integrity_state", _apply_v4),
    Migration(5, "lineage, project_state, intents", _apply_v5),
    Migration(6, "resource_leases, worktree_snapshots", _V6_SQL),
    Migration(7, "project column on gate_runs, merge_records and events; indexes on gate_runs and events", _apply_v7),
    Migration(8, "merge_records rebuilt with PRIMARY KEY (project, task_key), legacy rows backfilled from "
                 "plan_tasks where unambiguous", _apply_v8),
    Migration(9, "integrity_heads: the full history of primary-checkout HEADs ASES has written, for the "
                 "base-commit check (ASES-GIT-01, ASES-GIT-16), backfilled from integrity_state", _apply_v9),
    Migration(10, "model_registry.declared: sync_from_config hides an undeclared row instead of deleting it, "
                  "so a recorded smoke test survives a model briefly dropping out of config (ASES-DOC-04)",
              _apply_v10),
]


def _check_migrations(migrations: list[Migration]) -> None:
    """The list must be 1..N in order: a gap or a repeat would make 'the database is at version V' ambiguous."""
    versions = [m.version for m in migrations]
    if not migrations or versions != list(range(1, len(migrations) + 1)):
        raise MigrationError(f"MIGRATIONS must be numbered 1..N in order with no gaps, got {versions}")


_check_migrations(MIGRATIONS)

# Derived, never typed by hand: the version of the newest migration. (connect() reads MIGRATIONS itself, so a test
# that swaps the list in is honoured; this constant is for code that only wants to print the number.)
SCHEMA_VERSION = MIGRATIONS[-1].version


def latest_version() -> int:
    """The highest schema version this code knows how to build."""
    return MIGRATIONS[-1].version


def current_version(conn: sqlite3.Connection) -> int:
    """The schema version the database is at: the highest row in schema_migrations, 0 when the table is missing or
    empty (a new file). The MAX, not a count, because a database made by the old code holds one row per version bump
    of the code that opened it, with gaps."""
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if has_table is None:
        return 0
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return int(row[0] or 0)


def pending(conn: sqlite3.Connection) -> list[Migration]:
    """The migrations connect() would apply to this database, oldest first: every one above its current version."""
    current = current_version(conn)
    return [m for m in MIGRATIONS if m.version > current]


def backup_path(db_path: str | pathlib.Path, from_version: int, *, now: datetime | None = None) -> pathlib.Path:
    """Where the pre-migration copy of `db_path` goes: `<name>.bak-v<from_version>-<UTC timestamp>` in the same
    directory (`ases.db.bak-v5-20260921T101500Z`). The timestamp has no colon, which Windows forbids in a name, and
    sorts in time order."""
    db_path = pathlib.Path(db_path)
    moment = now or datetime.now(timezone.utc)
    # A naive datetime is taken as UTC (astimezone would read it as local time and the name would depend on the machine).
    moment = moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)
    return db_path.with_name(f"{db_path.name}.bak-v{from_version}-{moment.strftime('%Y%m%dT%H%M%SZ')}")


def _backups(db_path: pathlib.Path) -> list[pathlib.Path]:
    """The backups of db_path in this directory, oldest first (by the timestamp in the name, then the version). A
    partial copy (`.tmp`) and any other file are not backups and are never listed."""
    pattern = re.compile(rf"^{re.escape(db_path.name)}\.bak-v(\d+)-(\d{{8}}T\d{{6}}Z)$")
    found: list[tuple[str, int, pathlib.Path]] = []
    try:
        for entry in db_path.parent.iterdir():
            match = pattern.match(entry.name)
            if match and entry.is_file():
                found.append((match.group(2), int(match.group(1)), entry))
    except OSError:
        return []
    found.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in found]


def _prune_backups(db_path: pathlib.Path, keep: int = BACKUPS_KEPT) -> None:
    """Keep only the newest `keep` backups. Removing an old one is best effort: a file another program has open is left
    for the next upgrade or for `swarm retention`."""
    backups = _backups(db_path)
    doomed = backups[:-keep] if keep > 0 else backups
    for old in doomed:
        try:
            old.unlink()
        except OSError:
            pass


def _backup(conn: sqlite3.Connection, db_path: pathlib.Path, from_version: int) -> pathlib.Path:
    """Copy the live database with the SQLite backup API, to a `.tmp` name first and then renamed into place, so a
    crash mid-copy leaves a partial file that is never mistaken for a backup. The copy is put back into rollback
    (non-WAL) mode so it is ONE file that any tool can open. Refuses (MigrationError) when the copy cannot be made:
    a migration is never applied without the safety net."""
    target = backup_path(db_path, from_version)
    # The process id in the partial's name: two `swarm` commands upgrading the same file in the same second would
    # otherwise write one .tmp file at once.
    partial = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    dest: sqlite3.Connection | None = None
    try:
        dest = sqlite3.connect(str(partial))
        conn.backup(dest)
        try:
            dest.execute("PRAGMA journal_mode=DELETE")
        except sqlite3.Error:
            pass
        dest.close()
        dest = None
        try:
            os.replace(partial, target)
        except OSError:
            # Two processes upgrading in the same second both aim at this name. A file that exists under it got there by
            # an atomic rename of a finished copy, so it is whole and this second copy is redundant. (On Windows the
            # rename onto it fails with "access denied" while the other process is still touching it, which is how
            # this was found.) With no such file the failure is real and is reported below.
            if not target.exists():
                raise
            try:
                partial.unlink()
            except OSError:
                pass  # a stray .tmp is never taken for a backup, and `swarm retention` removes it when it is old
    except (sqlite3.Error, OSError) as exc:
        if dest is not None:
            try:
                dest.close()
            except sqlite3.Error:
                pass
        try:
            partial.unlink()
        except OSError:
            pass
        raise MigrationError(
            f"could not back up {db_path} before migrating it from version {from_version} ({exc}); nothing was "
            f"changed. Free some disk space or fix the directory permissions and try again.",
            version=from_version,
        ) from exc
    _prune_backups(db_path)
    return target


def _refuse_newer(conn: sqlite3.Connection, db_path: pathlib.Path) -> None:
    current, newest = current_version(conn), latest_version()
    if current > newest:
        raise MigrationError(
            f"{db_path} is at schema version {current}, newer than the newest this ASES knows ({newest}). It was "
            f"written by a newer ASES and is refused untouched: upgrade ASES, or restore an older backup "
            f"({db_path.name}.bak-v*).",
            version=current,
        )


def _apply(conn: sqlite3.Connection, migration: Migration) -> None:
    """One migration in one transaction: the changes and the schema_migrations row commit together or not at all.
    BEGIN IMMEDIATE takes the write lock up front, and the version is read again under it, because another process may
    have applied this same migration while this one waited for the lock (two `swarm` commands started together on a
    freshly upgraded ASES)."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        if current_version(conn) >= migration.version:
            conn.execute("ROLLBACK")
            return
        if isinstance(migration.apply, str):
            _run_script(conn, migration.apply)
        else:
            migration.apply(conn)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, datetime('now'))",
            (migration.version,),
        )
        conn.execute("COMMIT")
    except BaseException as exc:
        if conn.in_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        if not isinstance(exc, Exception):
            raise
        try:
            stayed = current_version(conn)
        except sqlite3.Error:
            stayed = migration.version - 1
        raise MigrationError(
            f"migration {migration.version} ({migration.description}) failed and was rolled back, the database stays "
            f"at version {stayed}: {exc}",
            version=migration.version,
        ) from exc


def _upgrade(conn: sqlite3.Connection, db_path: pathlib.Path, had_data: bool) -> None:
    migrations = MIGRATIONS
    _check_migrations(migrations)
    conn.execute(_BOOTSTRAP)
    current = current_version(conn)
    todo = [m for m in migrations if m.version > current]
    if not todo:
        return
    if had_data:
        _backup(conn, db_path, current)
    for migration in todo:
        _apply(conn, migration)


_lock = threading.Lock()


def connect(db_path: str | pathlib.Path) -> sqlite3.Connection:
    """Open (creating if needed) the ASES SQLite database and bring its schema up to date.

    WAL mode, foreign keys on, rows as sqlite3.Row, and autocommit (isolation_level=None): a caller that wants a
    transaction issues BEGIN itself, exactly as before. A database that is already current costs a few reads and no
    write. A database that is behind is backed up (when the file already held data) and then migrated, one migration
    per transaction; see the module docstring. Raises MigrationError for a failed migration, a failed backup, or a
    database written by a newer ASES; the connection is closed first, so no handle is left on the file."""
    db_path = pathlib.Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Asked BEFORE sqlite creates the file: a zero-byte file is a new database, not something to back up.
    had_data = db_path.is_file() and db_path.stat().st_size > 0
    conn = sqlite3.connect(str(db_path), isolation_level=None)  # autocommit; each migration opens its own transaction
    try:
        conn.row_factory = sqlite3.Row
        with _lock:
            _refuse_newer(conn, db_path)  # a pure read: a newer database is closed again without a single write
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            _upgrade(conn, db_path, had_data)
    except BaseException:
        conn.close()
        raise
    return conn
