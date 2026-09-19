# Package Rc: reconcile-on-start repairs, intent records, orphan workers

Files you own: `src/ases/reconcile.py` (extend; keep `check()` and `Inconsistency` working exactly as they do now, the existing
tests in tests/unit/test_reconcile.py must pass unchanged), `src/ases/intents.py` (new), `tests/unit/test_reconcile.py` (extend),
`tests/unit/test_intents.py` (new). Nothing else.

## Requirements (quote the ids; read blueprint.txt around `[p349]` to `[p353]` and the failure table row "Controller crash")
- ASES-REC-03, section 19.4: "Card creation uses idempotency keys, so repeating a half-finished creation is safe."
- Section 19.4: "Every multi-step action writes an intent record before acting and a completion record after: create cards, run a gate,
  build a candidate, fast-forward, complete a merge card, revert."
- ASES-REC-04: "On start the controller compares the board, Git and its database: merge cards that are done without a merge record, merge
  records without a done card, candidates without a verdict, worktrees without cards, cards without worktrees, running cards whose
  worker is gone. It repairs what is safe and blocks the rest with an explanation."
- Section 19.4: "Orphan worker processes from a previous controller session are found by card ID and terminated before new work starts."
- Section 22.7 (crash recovery test) is the acceptance test this must make possible: kill the controller (a) during a running card,
  (b) during a candidate build, (c) between the fast-forward and the merge-card completion; after each restart: no duplicate cards,
  no orphan workers, no half-merged state, the ledger intact, and every repair logged.
- Existing facts: `merge_task` (src/ases/mergeq.py) writes a `merge_records` row (task_key, candidate_sha, gate3_result,
  squash_commit, reverted, completed_at) with completed_at NULL before the fast-forward and sets squash_commit and completed_at
  after it; the controller then completes the merge card. The squash commit message the controller writes contains the lines
  `Work card: <id>` and `Merge card: <id>` (see controller.process_merge_queue), which is how a merge is recognised in git after a
  crash. A recorded no-op merge has gate3_result "skipped" and squash_commit NULL with completed_at set.

## Build `intents.py`
`begin(conn, project, kind, key, detail=None) -> int` (inserts into the `intents` table, started_at UTC isoformat seconds, returns
the id), `complete(conn, intent_id, detail=None) -> None` (sets completed_at; completing twice keeps the first time),
`open_intents(conn, project) -> list[dict]` (rows with completed_at NULL, oldest first, each dict has id, kind, key, detail,
started_at), and a context manager `intent(conn, project, kind, key, detail=None)` that begins, yields the id, and completes only
when the body did not raise (an intent left open by an exception or a crash is exactly what reconcile looks for). Kinds are an
open vocabulary but document the six of section 19.4 as constants: KIND_CREATE_CARDS, KIND_RUN_GATE, KIND_BUILD_CANDIDATE,
KIND_FAST_FORWARD, KIND_COMPLETE_MERGE_CARD, KIND_REVERT. Also `mark_recovered(conn, intent_id, note)` which completes an open intent
and appends the note to detail. All writes are single statements (no BEGIN), safe under the ASES connection's autocommit mode.

## Extend `reconcile.py`
1. `Repair` frozen dataclass (task_key, kind, detail, applied: bool) and `ReconcileReport` (findings: list[Inconsistency], repairs:
   list[Repair], blocked: list[Inconsistency] (findings that were NOT safely repairable and need a human)) with a `clean` property
   (no findings at all).
2. `pid_alive(pid) -> bool` and `terminate_tree(pid) -> bool`: the two process helpers, cross-platform, and see the WINDOWS TRAP in
   the rules: never os.kill on Windows. On Windows use ctypes OpenProcess/GetExitCodeProcess for liveness and `taskkill /PID n /T /F`
   for termination; on POSIX use os.kill(pid, 0) for liveness and SIGTERM then SIGKILL on the process group for termination. Both
   are the DEFAULT for parameters named `alive` and `killer` below, so tests inject fakes.
3. `worker_pid(card) -> int | None`: the pid of the card's LIVE run: the last dict of `card["_runs"]` whose `ended_at` is empty/None
   and whose `worker_pid` is an int-like, else the card's own `worker_pid` field when its status is running, else None.
4. `reconcile(board, repo, plan, *, conn, apply=True, alive=pid_alive, killer=terminate_tree, command_line=process_command_line)
   -> ReconcileReport` performing, for each plan task (rows of plan_tasks WHERE project = plan.project) and using `hermes.kanban_show`
   plus git in `repo`:
   a. every existing check of `check()` (call it and keep its findings; a card that no longer resolves stays a blocked finding).
   b. MERGE DONE WITHOUT A RECORD: merge card `done`, no completed merge_records row. If the integration branch contains a commit whose
      message has the line `Merge card: <merge_card_id>` (use `git log <integration_branch> --grep=... --format=%H`), repair by writing
      the merge_records row from git (candidate_sha = squash_commit = that commit, gate3_result "recovered", reverted 0, completed_at
      now). If no such commit exists and the card is a review-only task (role not coder) write the no-op record (gate3_result
      "skipped", squash_commit NULL). Otherwise it is a blocked finding (the card says merged but git has no trace).
   c. RECORD WITHOUT A DONE CARD, the crash between the fast-forward and the merge-card completion: a merge_records row with completed_at
      set (or squash_commit set and reachable from the integration branch: `git merge-base --is-ancestor <sha> <integration>`) while
      the merge card is not `done`: complete the merge card with `hermes.kanban_complete(board, id, result="merged <sha> (recovered)",
      metadata={"squash_commit": sha, "recovered": True})`; for a no-op record complete it with the no-op result text
      "no changes to merge (review-only task)" and metadata {"squash_commit": None, "no_op": True, "recovered": True}.
   d. CANDIDATE WITHOUT A VERDICT / UNFINISHED MERGE: a merge_records row with completed_at NULL: if a commit with `Merge card: <id>`
      is on the integration branch the fast-forward did happen, so finish the record (squash_commit, completed_at) and complete the card
      as in (c); if not, the candidate was never landed: leave the row (the merge queue redoes it) and report it as a repair of kind
      "candidate_discarded" with applied False (informational, not blocked).
   e. OPEN INTENTS: for each open intent of the project (intents.open_intents), if reconciliation of its task resolved the state (a,
      b, c or d applied for that task key, or the state is consistent), `intents.mark_recovered`; unresolved open intents are reported
      as blocked findings of kind "open_intent".
   f. RUNNING CARD WHOSE WORKER IS GONE: a plan card with status `running` and a live-run pid that is not `alive(pid)`: repair with
      `hermes.kanban_reclaim(board, id, reason="worker process gone (reconcile-on-start)")`. A running card with no pid at all is a
      blocked finding (cannot tell).
   g. ORPHAN WORKERS: a card of the plan that is NOT running (done, blocked, ready, todo, review, scheduled) but whose latest run's
      worker_pid is alive AND whose process command line (injected `command_line(pid) -> str | None`) contains the card id: terminate
      it with `killer(pid)`; record it as an applied repair. NEVER terminate a process whose command line does not contain that card id
      (the user may have unrelated Hermes sessions) and never one whose command line cannot be read.
   h. WORKTREES WITHOUT CARDS / CARDS WITHOUT WORKTREES: `git worktree list --porcelain` in `repo`; a worktree under `.worktrees/` whose
      directory name is a card id of this plan whose card is `archived` or does not resolve is reported as a finding of kind
      "orphan_worktree" (do NOT remove it: cleanup is a separate hardening phase); a running plan card whose `workspace_path` does not
      exist on disk is a blocked finding of kind "missing_worktree".
   With `apply=False` nothing is changed (no writes, no hermes mutations, no kills): the report says what WOULD be repaired
   (applied False). Every applied repair is recorded once with `events.record(conn, "reconcile_repair", {task_key, kind, detail})`
   and a report is always safe to compute twice (a second run after applying finds nothing new). One failing hermes/git call for a task
   must not stop the other tasks: record a finding of kind "reconcile_error" and continue.
5. `process_command_line(pid) -> str | None`: the command line of a live process (Windows: PowerShell/CIM or `wmic`, POSIX:
   /proc/<pid>/cmdline or `ps -o args= -p`), None when unreadable; used only by the orphan check; default for `command_line`.

## Tests
tests/unit/test_intents.py: begin/complete/open ordering, complete twice keeps the first completed_at, the context manager completes
on success and leaves the intent open when the body raises, mark_recovered appends the note, other projects' intents invisible.
tests/unit/test_reconcile.py (extend; real temp git repos with a first commit on a branch named integration, real commits whose
messages carry `Merge card: <id>`; monkeypatch hermes and inject alive/killer/command_line fakes): each of a-h in the safe and the
unsafe direction; the three crash points of 22.7 as scenarios (a running card whose worker died; a candidate row with no landed commit;
a landed commit with the record but not the card, and one with neither the record nor the card completed); apply=False changes nothing
(assert no hermes mutation call and no DB write); idempotent second run; an orphan whose command line lacks the card id is NEVER killed;
an unreadable command line is never killed; a live running card's worker is not touched; one hermes failure does not stop the others;
existing `check()` behaviour unchanged. Do NOT spawn or kill any real process in a test.
