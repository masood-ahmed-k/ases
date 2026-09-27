# Round 9 package MERGEPK: merge_records keyed by project and task (read `r9_rules.md`)

Worktree: `C:\Users\masoo\ases-wt\mergepk`, branch `r9/mergepk`. Tier 1 item 4.

## Requirements (quoted from blueprint.txt)
- ASES-ARC-03 (p101): "Every ASES record is keyed by the Hermes card ID and, where code is involved, by the commit SHA. On startup
  the controller reconciles the board, the Git repository and its own database before doing anything else (section 19.4)."
- ASES-GIT-05 (p174): "The integration branch MUST stay runnable. If a post-merge check fails, the queue reverts the squash commit,
  records it, blocks the merge card and opens a fix card."

## The bug
`merge_records` has `task_key TEXT PRIMARY KEY` (`src/ases/db.py`). Schema version 7 added a nullable `project` column to
`gate_runs`, `merge_records` and `events` "and changes no primary key, so the readers and writers can be updated later". `gate_runs`
writers and `gates.last_gate_result` are already project-scoped (read how, and match those semantics). `merge_records` is not:
nothing writes its `project` column, and two projects that share one ASES database and both have a task `T1` overwrite each other's
merge record. The register's ASES-GIT-05 note calls this "a separate, still-open risk for two projects reusing a task key". Round 6's
CORE builder wrote up a migration plan: search `docs/work-orders/builder-findings.md` for it and read it before designing.

## Build
1. A new migration (next version number after the highest on your branch; read the rules at the top of `db.py`: never edit an
   existing migration, and add a test that builds the previous schema as plain text and upgrades it with rows intact) that makes the
   key `(project, task_key)`. SQLite cannot alter a primary key in place: rebuild the table. Legacy rows have no project: choose how
   they are keyed (NULL is NOT safe inside a SQLite composite primary key, NULLs never collide) and document the choice. If a legacy
   row can be attributed to a project unambiguously (for example through `plan_tasks`, which is keyed `(project, task_key)`),
   consider backfilling; say what you did and why.
2. Every writer of `merge_records` writes the project; every reader scopes by project, with the same legacy-row semantics
   `gates.last_gate_result` uses for `gate_runs` (a project-scoped read matches its own rows and legacy rows; never another
   project's). Find every reader and writer with Grep across `src/ases` (expected: `mergeq.py`, `controller.py`, `reconcile.py`,
   `report.py`, `finalgates.py`, `evals.py`/`evalkit` scoring; confirm, do not trust this list).
3. Report only, do not fix: anything else still keyed by task alone that the same collision would hit (the `events` table is a
   separate package in a later wave; the board-wide `kanban_dispatch(board)` noted in `docs/architecture.md` is out of scope).

## Tests (fakes only)
Migration from the previous schema with rows intact; two projects with the same task key in one database keep separate merge
records through a real `process_merge_queue` pass on `FakeHermes` (an acceptance-style test in a NEW file under `tests/acceptance/`
using `world_factory`, or a unit test if a two-project world cannot be built without editing `conftest.py`: say which and why);
every reader returns only its own project's rows plus legacy rows. Before/after proof: the two-project collision test fails on the
old code and passes on yours.

## Files you own
`src/ases/db.py` (the new migration only), the `merge_records` read/write sites in the modules above, and the matching tests. Other
packages edit other parts of `mergeq.py`, `controller.py`, `reconcile.py` and `finalgates.py` on their own branches: touch only the
`merge_records` sites.
