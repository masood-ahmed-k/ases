# Package HD: hardening (phase 9): real database migrations, worktree and branch cleanup, log retention, the runbook

Files you own: `src/ases/db.py`, `src/ases/hardening.py` (new), `tests/unit/test_db.py`, `tests/unit/test_hardening.py` (new),
`docs/runbook.md` and `docs/operations.md` (new). Nothing else. Read `r2_rules.md`, `r5_rules.md` and `r5_contracts.md` first (you may
create the two docs files named above; everything else outside src and tests is off limits).

## Requirements (blueprint.txt [p506] phase 9 row, [p446] Appendix A tree, ASES-OBS-02, ASES-ARC-03, ASES-GIT-09, section 19.4)
- Phase 9: "Cleanup of worktrees and branches (hermes worktree prune), database migrations, log retention, documentation, runbook.
  Exit: the whole of section 22 green." Appendix A lists `docs/operations.md` and `docs/runbook.md`.
- ASES-OBS-02 (section 15.2): "Transcripts and logs stay local under a retention setting."
- ASES-ARC-03: ASES records are keyed by card ID and commit SHA and the controller reconciles on start.
- Findings from earlier builders that belong here: a SIGKILL during a candidate build leaves a throwaway `ases-merge-*` worktree in the
  system temp directory registered in git (reconcile ignores it by design); `git worktree prune` is needed; merged `swarm/*` and
  `merge/*` branches pile up.

## 1. `db.py`: real, numbered, backed-up migrations
Today `connect()` runs one big `CREATE TABLE IF NOT EXISTS` script plus `_ensure_columns`, and records only a version number. Replace
the mechanism, keeping `connect(db_path)` and every table and column exactly as they are (all existing tests must pass):
- `MIGRATIONS`: an ordered list of `Migration(version, description, apply)` where `apply(conn)` is a function or SQL. Version 1 is the
  baseline (the whole current schema, idempotent), and versions 2 to 6 are the existing steps expressed as migrations (v4 usage_ingested
  and columns, v5 lineage, project_state, intents, v6 leases and snapshots): a database created by ANY earlier ASES (schema 1 to 5 as
  found on the user's machine: `data/ases.db` is at version 5 in the field) must upgrade cleanly, and a brand-new database must end at
  the same version with the same tables. Then add version 7, ADDITIVE ONLY: a nullable `project TEXT` column on `gate_runs`,
  `merge_records` and `events`, plus an index on `gate_runs (project, task_key, commit_sha)` and on `events (kind)`. (Readers and writers
  are updated by the package that owns each caller later; this migration only makes room. Do not change any primary key.)
- `connect()` applies pending migrations in order, each in its own transaction, recording `(version, applied_at)` per migration in
  `schema_migrations`; a failing migration rolls back, leaves the version where it was, and raises `MigrationError` naming the version.
  Before applying anything to an EXISTING database file that is not empty and is behind, copy it to `<name>.bak-v<from>-<UTC timestamp>`
  next to it (use `sqlite3.Connection.backup`, never a file copy of a live WAL database); prune to the newest 5 backups.
- `SCHEMA_VERSION` is derived from the migration list. `current_version(conn)`, `pending(conn)` and `backup_path(...)` are public and tested.
- A database NEWER than this code (version greater than the highest known) is refused with a clear `MigrationError`, never opened
  and never downgraded.
- Keep `PRAGMA journal_mode=WAL`, `foreign_keys=ON`, `row_factory`, autocommit (`isolation_level=None`) behaviour and the module lock.

## 2. `hardening.py`
1. `clean(repo, integration_branch, *, board, conn, plan_project, apply=False, kanban_show=None, kanban_list=None, temp_root=None,
   now=None) -> CleanReport`: dry run by default. It finds and (only with `apply=True`) removes: (a) `git worktree prune` candidates and
   leftover candidate worktrees named `ases-merge-*` registered in git under the system temp directory (or `temp_root`) whose directory
   is gone or whose merge record is completed or absent; NEVER a worktree of a card that is running, in review, ready, blocked or
   scheduled; (b) local branches `swarm/*` and `merge/*` that are fully merged into the integration branch (`git branch --merged`) AND whose
   plan card is done or archived (look the card up through `plan_tasks` and `kanban_show`; a card that cannot be read is skipped and
   reported); never the integration branch, never the checked-out branch, never a branch with an open worktree. Every candidate has a
   reason string; every removal is an event `hardening_removed`; one failure never stops the rest. `CleanReport` (candidates, removed,
   skipped, errors) and `format_clean_report(report) -> str` (ASCII).
2. `retention(ases_home, days, *, apply=False, now=None, keep_latest=3) -> RetentionReport` and `format_retention_report`: under
   `<ases_home>` remove files older than `days` in `logs/`, `reports/`, `stops/`, `evals/` and old `ases.db.bak-*` backups (keep the
   newest `keep_latest` of each kind regardless of age), dry run by default, never touches `ases.db` itself, never follows symlinks out
   of `ases_home`, refuses when `days` is below 1, and reports bytes freed. `retention_events(conn, days, *, apply=False) -> int` prunes
   `events` rows older than `days` ONLY when asked for explicitly (separate function, default off: the event log is the audit trail).
3. `vacuum(conn) -> tuple[int, int]` (size before and after, best effort).
4. Everything is injectable (`kanban_show`, `kanban_list`, `now`, paths) and never raises.

## 3. Docs (plain Markdown, ASCII, no em dash, no section sign; accurate to the CODE as it is when you finish, read `cli.py` and the modules)
- `docs/operations.md`: what each command does and in what order for a normal project (doctor, plan, critique, approve, run, questions and
  answer, status, report, stop, resume, clean, retention, eval), the files and directories ASES writes and where, the exit codes of
  `swarm run`, the configuration keys of `config/swarm.yaml` and `config/models.yaml`, the request ledger and how parking works.
- `docs/runbook.md`: symptom -> cause -> action tables for the failures the modules define: a card blocked for a question, a card that
  gave up, a merge card blocked for the fix-card budget, a red Gate 1 or Gate 3, a tamper finding, a primary-checkout violation (exit
  code 3), a reconcile block (exit code 5), a stopped or paused project, an exhausted provider quota, a quota reset, a crashed controller,
  orphan workers, leftover worktrees, upgrading Hermes (the tested version is pinned: what to re-verify), rotating a provider key, and how
  to recover the ASES database from a backup. Where a step needs the user's approval say so (Docker, Hermes configuration).

## Tests
Migrations: a fresh database has every table and ends at the newest version; a v5 database built from the OLD schema text (embed it as a
string in the test) upgrades in place with its rows intact and gets a backup file; a failing migration rolls back and raises; a newer
database is refused; backups pruned to 5; the version-7 columns exist and nothing else changed; reconnecting is idempotent. Hardening:
real temp git repos with merged and unmerged `swarm/*` branches, a checked-out branch, a branch with a worktree, cards done and running
(fake `kanban_show`): dry run deletes nothing, apply deletes exactly the safe ones, the integration branch is never touched; a leftover
`ases-merge-*` worktree; retention with old and new files, `keep_latest`, symlink safety, `days < 1` refused, `ases.db` untouched; event
pruning off by default; every report ASCII.
