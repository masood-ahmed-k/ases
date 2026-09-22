# Package CORE: project-scoped gate and merge records, the post-merge revert trigger, and the CHANGES_REQUIRED dead end

Files you own: `src/ases/gates.py`, `src/ases/mergeq.py`, `src/ases/bounds.py`, `src/ases/controller.py`, and their four test files
(`tests/unit/test_gates.py`, `test_mergeq.py`, `test_bounds.py`, `test_controller.py`, `test_controller_loop.py`). Nothing else. Read
`r2_rules.md`, `r5_rules.md`, `r6_rules.md` first (the zero-quota rule applies to everything you do: your tests use fakes, never a
real provider or Hermes call, same as before). No other package touches these six files this round.

## What already exists (read the real code, not this summary)
Schema v7 (`db.py`, already built, do not edit it) added a nullable `project` column to `gate_runs`, `merge_records` and `events`,
plus indexes `idx_gate_runs_project_task_sha` and `idx_events_kind`, "additive only... changes no primary key... the readers and
writers can be updated later." You are the package that updates the two that matter for correctness: `gate_runs` and `merge_records`.
(`events.project` is lower priority and explicitly out of scope for this package: `events.record` is called from roughly a hundred
places across the codebase and a full sweep is a separate, later change. Note in your report if you think it is now urgent.)

## 1. Project-scope `gate_runs` (found by the bounds and MR builders: two projects sharing a database, or reusing a task key, share
   gate results and final-gate rows)
- `gates.py`: `run_gate(repo_path, commit_sha, gate_name, commands, *, conn=None, task_key="", project=None,
  timeout_per_command=120, runner=None)` gains `project`. When given, it is stored in the new `gate_runs.project` column (an INSERT
  with 6 values becomes 7; write the column list explicitly, do not rely on column order). Keep the old behavior exactly when
  `project` is not given (a NULL project column, matching every row written before this change).
  `last_gate_result(conn, task_key, gate, commit_sha, *, project=None)`: when `project` is given, only match rows with that project OR
  a NULL project (an old row, or one written before this change, still counts: never orphan history). Document why in the docstring.
- `bounds.py`: `record_final_gate(conn, project, gate, commit_sha, result, *, detail="", now=None)` already takes `project` as its
  second positional argument, look at what it does with it today (the FG builder's note says it "cannot be the special case" gates.py
  wanted, so it likely already writes a project-scoped row through its own SQL, not through `run_gate`). Read it, and make
  `final_gates_green(conn, project, head)` (it may currently be `final_gates_green(conn, head)`: check, and if it does not take
  `project`, ADD it, matching the call sites you will need to update in `finalgates.py`... but you do not own `finalgates.py` this
  round, so if `final_gates_green`'s signature must change, keep the old positional shape working via a default: `project=None` scans
  every project's `__final__` rows as it does today when `project` is omitted, a caller-supplied `project` scopes to it. This must not
  break `finalgates.py`'s calls (read them; they pass positionally or by keyword, check both) or its tests, which you do not own:
  run `tests/unit/test_finalgates.py` after your change and keep it green.
- Callers inside your four files: `mergeq.merge_task` calls `gates.run_gate` for Gate 3, pass `project=plan_project` (mergeq.py
  already receives `project` as a parameter since round 5, read the real signature first: it may be named differently). `controller.py`
  calls `run_gate` nowhere directly (review.py does, and you do not own review.py this round, so leave review.py's call sites as they
  are; note in your report that they are NOT yet project-scoped, for a later round).

## 2. Project-scope `merge_records` (found by the reconcile and evals builders: `merge_records` is keyed by `task_key` ALONE, so two
   projects with a task "T1" collide)
- `mergeq.py`: read `merge_task`'s current SQL against `merge_records` (an upsert keyed by `task_key`). Change the primary key to
  `(project, task_key)` is a real migration (schema v8): write it. In `db.py` you do NOT own, so instead: add migration 8 yourself IN
  `db.py`... wait, `db.py` is not in your file list. Resolve this by asking: does `merge_records` truly need a new primary key, or does
  it just need every write and read to include `project` in its WHERE clause so a NULL-project legacy row and a wrongly-matched
  cross-project row never collide? Prefer the SMALLER, SAFER fix that stays inside your six files: every `merge_task` call already
  receives a `project` (or `plan_project`, check the real parameter name); scope every read and write of `merge_records` by
  `(task_key, project IS ? OR project = ?)` the same NULL-tolerant pattern as `gate_runs` above, using the column schema v7 already
  added. If, after reading `db.py`'s migration 7 comment and the actual column definition, you conclude the primary key genuinely must
  change to make this safe (a true `INSERT ... ON CONFLICT` upsert cannot be NULL-tolerant on part of a composite key the way a SELECT
  can), STOP and write up exactly why in your report instead of editing `db.py`: this is a decision for the architect, who will dispatch
  a follow-up migration package. Do not touch `db.py` under any circumstances.
- `reconcile.py`'s use of `merge_records` (step b/c/d matching by `Merge card: <id>` in a commit message) is unaffected either way,
  since it looks up by task_key AND the commit message names the exact card id; you do not own `reconcile.py`, do not edit it, but DO
  run `tests/unit/test_reconcile.py` after your change and keep it green (your `merge_records` schema/query change must not break it).

## 3. Wire the post-merge revert (ASES-GIT-05, section 8.1: "The integration branch MUST stay runnable. If a post-merge check fails,
   the queue reverts the squash commit, records it, blocks the merge card and opens a fix card.")
`mergeq.revert_merge` exists (read its real signature) and has its own unit tests, but nothing calls it: `controller.process_merge_queue`
only calls `merge_task`. There is currently no POST-merge check at all (Gate 3 runs BEFORE the fast-forward, which is a pre-merge check;
"post-merge" means something checked the integration branch AFTER the merge landed and found it broken). Build the smallest real
trigger that matches the blueprint sentence:
1. In `process_merge_queue`, immediately after a successful `outcome.merged` (the non-no-op branch, i.e. `outcome.squash_commit` is not
   None), before you complete the merge card, re-run Gate 3's own commands ONE more time on the NEW integration HEAD in a throwaway
   worktree (reuse `gates.run_gate(repo, outcome.squash_commit, "gate3-postmerge", gate_cmds, conn=conn, task_key=key,
   project=plan.project)`), UNLESS the task's role is not "coder" (a no-op merge has nothing to re-check). This is deliberately
   redundant with the pre-merge Gate 3 (the blueprint's "stays runnable" guarantee is exactly this redundancy: a candidate that was
   green a moment ago can be red now because another task's merge landed between the candidate build and this fast-forward, which
   `merge_task`'s own `expected_head` check already prevents for THIS task's own race, but not for a project-level regression another
   task's merge introduced onto a shared file THIS task's touches never named).
2. If that post-merge check is RED: call `mergeq.revert_merge(repo, plan.integration_branch, outcome.squash_commit, key, conn=conn,
   project=plan.project)` (read its real signature; adapt argument names/order to match). Record a `post_merge_reverted` event with the
   task key, the commit and the gate detail (redacted). Then take the SAME path `process_merge_queue` already takes for an ordinary
   merge failure: a `merge_failed` event, a fix card bounded by `fix_cards_per_task` (or a `block_for_user`/`ask_user` escalation once
   the budget is spent, exactly like the existing failure path: read it and reuse it, do not duplicate the fix-card-creation code, factor
   it into a small helper both paths call if it is not one already). Do NOT complete the merge card as done; it stays open for the fix.
3. If the post-merge check is GREEN (the common case: nothing else changed underneath it): proceed exactly as today (complete the merge
   card, record `merged`, `guards_mod.set_expected_head`).
4. Fix `mergeq.revert_merge` itself per the MR builder's finding: it "marks `reverted = 1` even when `git revert` fails" and "never
   aborts a conflicted revert... the primary checkout can be left mid-revert." Fix both: only set `reverted = 1` in the database when
   `git revert` actually succeeded; on a conflict or any non-zero exit, run `git revert --abort` (best effort, never raise if that also
   fails) so the primary checkout is left clean, and return a result the caller can tell apart from a successful revert (read the real
   return type; add a field if there is not already one, keeping the old field names working).
5. A revert that itself fails (git refuses, `--abort` also fails, the checkout is left dirty): halt the run the same way the primary-
   checkout guard does today (an `integrity_violation` event, `run_pass` returns with `integrity` non-empty) rather than silently
   continuing to merge on top of a broken branch. Wire this by having `process_merge_queue` return a signal `run_pass` already knows how
   to read (read `run_pass`'s existing halt path before inventing a new one).

## 4. Fix the CHANGES_REQUIRED-on-a-`done`-card dead end (found by the FK builder: a reviewer that calls `kanban_complete` with a
   CHANGES_REQUIRED or BLOCKED verdict in its metadata, instead of using `request-changes`/`block`, leaves the card `done` forever)
`process_merge_queue` already calls `review_mod.validate_verdict(completed_run.get("metadata"))` and, when `verdict.outcome != "PASS"`,
calls `_refuse_once(conn, "merge_refused_invalid_verdict", ...)` and `continue`s. Read `_refuse_once`: if it only records an event once
and does nothing else, the card sits `done` forever with no path back to its implementer and no visibility to the user. Fix: on a
valid, schema-correct verdict whose outcome is CHANGES_REQUIRED or BLOCKED (not merely malformed), treat it as if the card had gone
through `kanban_reopen_review` instead of `kanban_complete`: call `hermes_mod.kanban_reopen_review(board, work_card["id"],
reason=verdict.summary or "reviewer completed the card with a non-PASS verdict")` so it returns to its implementer, record a
`reviewer_completed_with_changes_requested` event, and do NOT treat it as the unreviewed-refusal path (which is for a genuinely
malformed or missing verdict, keep that path as it is). A verdict that is malformed/unparseable keeps going through `_refuse_once` as
today, once. Update `_refuse_once`'s docstring/behavior only if this needs it; do not change its signature if avoidable.

## Tests
`test_gates.py`: `run_gate(project=...)` stores it; `last_gate_result(project=...)` matches its own project and NULL-project legacy
rows, never another project's row with the same task_key and commit. `test_mergeq.py`: the merge_records NULL-tolerant scoping (or the
composite-key decision write-up), the post-merge revert trigger end to end on a real temp git repo (a task's merge lands, a SECOND
task's merge then breaks a file the first task's Gate 3 would have caught, the post-merge re-check is red, revert fires, `reverted=1`
only on success, `--abort` on a conflicted revert, the fix-card path taken, the merge card stays open), `revert_merge`'s fixed return
value. `test_bounds.py`: `final_gates_green(project=...)` old and new call shapes. `test_controller.py`/`test_controller_loop.py`: the
post-merge check wired into `process_merge_queue` (green passes through unchanged, red reverts and opens a fix card, a revert failure
halts the run), the CHANGES_REQUIRED/BLOCKED dead-end fix (both outcomes reopen the card, a malformed verdict still takes the old
refusal path once). Every new test uses fakes (a real temp git repo is fine and expected; a real Hermes or provider call is not).

## Report back
The usual report, plus: did you have to touch `db.py`'s schema, or was the NULL-tolerant approach for `merge_records` enough? If you
concluded a primary-key migration is genuinely needed, say exactly what it should be.
