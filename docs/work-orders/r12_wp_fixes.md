# Round 12: fix the verified audit findings (read `r10_rules.md` first, then `r12_audit_findings.md`)

Three packages, each in its own worktree `C:\Users\masoo\ases-wt\<name>` on branch `r12/<name>`. GATEINFRA and DATAFIX are cut
from the master commit that adds these work orders (MOD02, which changes `cli.py`, `doctor.py`, `models.py` and `recovery.py`,
is merged after them); RUNSTART is cut after MOD02 merges, because it changes `cli.py` and `doctor.py` too. Every rule in `r10_rules.md` applies (zero quota, Write/Edit only, no git stash, own `--basetemp`, `gitexec` for git,
`events.PROJECT_SCOPE_SQL` for events). The findings file `r12_audit_findings.md` (same folder) holds each finding's scenario,
evidence, reproduction and the skeptic's corrected fix: read the ones your package owns in full before touching code. Where the
skeptic's corrected fix differs from the finder's suggestion, the skeptic's wins unless you find evidence against it (say so).
Testing budget this round (owner's instruction): your targeted test files while you work, then ONE full suite at the end; no
repeated full runs.

## GATEINFRA (`gateinfra`): findings 0, 10, 11, 1

Requirements: ASES-QG-01 (p275): "The gate runner executes the project's pinned commands and records the output by commit SHA.
An agent cannot mark anything as passed by saying so: the controller believes only its own gate records." ASES-GIT-05 (p174):
"If a post-merge check fails, the queue reverts the squash commit, records it, blocks the merge card and opens a fix card."
Blueprint p346 (recovery): "An infrastructure failure says nothing about the model or the task".
1. Finding 0 (high): a gate checkout that fails for infrastructure reasons must raise, never return `passed=False`. Add a
   clearly scoped exception in `gates.py` (for example `GateCheckoutError`, or a shared base class with
   `sandbox.SandboxInfrastructureError`) and make every caller that already treats `SandboxInfrastructureError` as "not a red
   gate" (review.py's two Gate 1 paths, mergeq.py's Gate 3 candidate, controller.py's post-merge re-run, finalgates) treat the
   new one the same way. Above all: the post-merge re-run must NEVER revert a landed merge because its throwaway checkout could
   not be created. Reproduce with the finding's method (pre-occupy the checkout path) before and after.
2. Finding 10 (high): `gates._run_commands`' timeout does not bound wall-clock on real Windows (shell=True leaves the child
   tree running and `communicate` waits on its pipes). Use Popen plus `communicate(timeout=...)` and, on timeout, kill the
   WHOLE tree (Windows: `taskkill /PID <pid> /T /F`; POSIX: a new session and `os.killpg`), then collect what output there is.
   Prove it with a real test that a command which spawns a long-lived child returns within the timeout plus a small margin.
   Keep the round 8 scrubbed environment.
3. Finding 11 (medium): when the throwaway gate or merge worktree cannot be removed (a Windows file lock, a surviving child),
   record an event naming the leaked path and context instead of discarding the result silently (gates.py `run_gate` and
   mergeq.py `merge_task` finally blocks), and make the idle-worktree/doctor path able to report leftovers if cheap.
4. Finding 1 (low): the docker CLI subprocesses `sandbox_command_runner`, `docker_available` and `image_present` start should
   not inherit the controller's credentials. Do NOT change `sandbox.default_runner` (its key-leak probes need the real
   environment; round 7 note); give the gate path its own environment the way `profiles._hermes_runner` does.
Files you own: `src/ases/gates.py`, `src/ases/sandbox.py` (the gate-path runner only), the infra-failure handling at the gate
call sites in `review.py`, `mergeq.py` (merge_task only), `controller.py` (the post-merge re-run only), `finalgates.py`, and tests.

## DATAFIX (`datafix`): findings 4, 5, 6, 7

Requirements: ASES-ARC-03 (p101): "Every ASES record is keyed by the Hermes card ID and, where code is involved, by the commit
SHA." ASES-GIT-05 (p174, above).
The common root: schema v8 keeps `merge_records.project` nullable for legacy rows, and SQLite never treats two NULLs as equal,
so a legacy NULL row and a project's own row can coexist for one task_key. Every WRITE must hit exactly one row, and every READ
that picks one row must prefer the project's own row over a legacy one.
1. Finding 4 (high): `mergeq.revert_merge` and `reconcile._mark_reverted` use the NULL-tolerant `(project IS NULL OR project = ?)`
   predicate in an UPDATE, so reverting one project's merge can also mark a legacy row reverted. Use the exact-match idiom
   `mergeq._fast_forward` already uses (a real project matches only its own row; None matches only NULL).
2. Finding 5 (medium): `reconcile._finish_record` can raise a UNIQUE violation or hit the wrong row the same way: exact match.
3. Finding 6 (medium): `evalkit/codetasks.score_swarm_project` double-counts a task that has both rows: fold into one row per
   task_key, the project's own row winning (the pattern `report._quality_panel` uses).
4. Finding 7 (low): `hardening._squash_proof` and `reconcile._merge_record` `fetchone()` from an unordered OR query: order so the
   project's own row comes first (`ORDER BY (project IS NULL)`).
5. Grep `src/ases` yourself for EVERY other `merge_records` statement using the OR predicate and classify it: a write must be
   exact-match, a single-row read must prefer the project's row, a multi-row read must deduplicate by task_key. Fix every one of
   the same shape; list them in your report.
Tests: a database with a legacy NULL row and a project row for the same task_key, exercising each fixed site, each failing on
the old code. Files you own: those statements in `mergeq.py` (revert_merge and upsert/read helpers, NOT merge_task),
`reconcile.py`, `hardening.py`, `evalkit/codetasks.py`, `report.py`/`finalgates.py` only if the grep finds the same shape there,
and tests.

## RUNSTART (`runstart`): findings 2, 8, 3, 9

Requirements: ASES-GIT-12 (p185): "Before a worker starts and after it stops, the controller snapshots git status --porcelain and
HEAD of the primary checkout ... Any change outside the worker's own worktree fails the card and raises a security event."
ASES-REC-04 (p352): "On start the controller compares the board, Git and its database ... It repairs what is safe and blocks
the rest with an explanation." Blueprint p169 (ASES-GIT-01/16): "Phase 3 MUST verify the actual base commit before a worker
starts."
1. Finding 2 (high): `swarm run` startup calls `adopt_current_head`, so a HEAD moved by something other than ASES between two runs
   is adopted into the base-check allow-list. On a restart (the project already has an expected head), compare the primary
   checkout's HEAD with the recorded expected head first and refuse to start, with the same kind of explanation a mid-run
   divergence gets, unless the operator passes an explicit override flag; a first-ever run (no expected head) is unchanged.
   Decide the override flag's name and make it loud in the output.
2. Finding 8 (medium): `swarm run` marks the project `running` and starts its wall clock before reconcile-on-start can refuse it.
   Reorder so reconcile runs first and the project is only started once reconcile has not refused; check whether reconcile needs
   the project already `running` (the skeptic's note) and handle it.
3. Finding 3 (medium): `process_card_base_checks` can check a running card before Hermes has created its branch (Hermes claims
   the card, then creates the worktree). Distinguish "branch does not exist yet" (skip, check next pass, the grace pattern
   `check_idle_worktrees` uses) from "branch exists but its creation commit cannot be read or is wrong" (block, as today). The
   merge-queue refusal stays as the backstop.
4. Finding 9 (low): `doctor._check_log_all_ref_updates`' docstring and "pending" message still say `swarm doctor` has no
   `--repo`; it has one since round 10. Fix both texts.
Files you own: the run-start code in `src/ases/cli.py` (cmd_run, _refuse_unless_startable, _reconcile_on_start), the relevant
functions in `src/ases/guards.py` and `src/ases/controller.py` (process_card_base_checks only), the one check in
`src/ases/doctor.py`, and tests.
