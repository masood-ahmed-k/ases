# Package AC-C: acceptance 22.7 (crash recovery, all three crash points)

Files you own: `tests/acceptance/test_22_7_crash_recovery.py` (new). Nothing else. You may NOT edit any file under `src/`, and may NOT
edit `tests/acceptance/conftest.py`. Read `r2_rules.md`, `r5_rules.md`, `r6_rules.md` FIRST, then `tests/acceptance/test_scenarios_demo.py`
in full (especially how `world.restart_controller()` is used and commented) and copy its style. Also read `tests/unit/test_reconcile.py`
and `tests/unit/test_intents.py`: the mechanism you are proving at acceptance level (intents, reconcile repairs) is already unit tested
there in isolation; this package proves it end to end through the real `controller.run_pass` and a simulated controller crash.

## Requirement (blueprint.txt around `[p411]`/`[p412]`, section 22.7)
"Kill the controller with SIGKILL during a running card, again during a candidate build, and again between the fast-forward and the
merge-card completion. After each restart: no duplicate cards, no orphan workers, no half-merged state, the ledger is intact, and
every repair is logged."
You cannot literally SIGKILL a test process. Simulate a crash the way `reconcile.py`'s own unit tests do (read them for the exact
pattern): run the real code up to the exact point of interest, then STOP calling it (never let the current Python call stack finish
normally) and inspect what state was left on the board/database/git, as if the process had died there. Concretely: monkeypatch a
function the crashing step calls (e.g., `hermes.kanban_complete` for crash point 3) to raise an exception you catch OUTSIDE the normal
flow (simulating "the process died before this call returned/before the next step ran"), or, more simply, drive `world.run_until`
partway (up to a `run_pass` you deliberately stop after), leave an `intents` row open by monkeypatching whatever step the real code
would do NEXT to instead raise, catch that exception at the test level (proving the crash happened after step N and before step N+1),
then call `world.restart_controller()` and `reconcile.reconcile(world.board, world.repo, world.plan, conn=world.conn, apply=True)`
(read its real signature) and assert the repairs.

## Crash point 1: during a running card
Dispatch a card (`fake.register_worker` with `fw.Sleep(...)` so it is `running` and has NOT finished), then simulate the crash by
simply NOT letting it finish -- call `fake.kill_worker(card_id)` (read its real docstring: "Kill the card's worker process from
outside... The card stays running with a dead PID until the next reclaim phase... books a crash, exactly as Hermes does") to put the
board in the state a real OOM-killed worker or a controller that died mid-run would leave behind on the BOARD side, then
`world.restart_controller()`, then run `reconcile.reconcile(..., apply=True)` and a further pass. Assert:
- No duplicate card: still exactly the cards the plan implies, none doubled.
- The dead worker is reclaimed (via the normal reclaim path OR `reconcile`'s own "running card whose worker is gone" repair, read
  which one actually fires first in the real code -- both may be legitimate depending on whether a `tick()`/dispatch happened before
  reconcile; assert whichever the real code does, and say in your report which path you observed).
- The task eventually completes normally after the restart (`run_until` to done).

## Crash point 2: during a candidate build
This needs the crash to land INSIDE `mergeq.merge_task`'s candidate-build step, after `merge_records` has an open row (or an open
`build_candidate` intent, per package CORE/MR's round 5 work -- read the real current code for exactly what state exists at this
point) but before Gate 3 or the fast-forward. Get a work card to `done` with a PASS verdict (drive it through review normally with
`fw.good_coder`/`fw.reviewer_pass`), so the merge queue is ABOUT to call `merge_task`. Monkeypatch a function INSIDE the candidate-build
path that runs after the row/intent is opened but before it completes (read `mergeq.merge_task`'s real body to find the exact call to
monkeypatch -- likely the Gate 3 `run_gate` call, or the git commit step) to raise, call `world.one_pass()` inside a `pytest.raises` (or
catch it yourself) to simulate the crash, then remove your monkeypatch, `world.restart_controller()`, run `reconcile.reconcile(...,
apply=True)`. Assert:
- The stale `merge_records` row / open intent is either finished (if a landed commit can be found, per `reconcile`'s own "candidate
  without a verdict" rule) or reported as an informational, non-blocking `candidate_discarded` repair (read `reconcile.py`'s real
  finding/repair kind names): the next merge pass redoes the candidate build cleanly, no half-merged state (the integration branch HEAD
  is exactly where it was before the crashed attempt).
- No orphan worker (there was none in this crash point; assert `fake.live_workers()` shows nothing orphaned).
- The ledger (`usage_ingested` / whatever CORE's project-scoped `gate_runs` ended up as) is intact: no stray rows from the aborted
  attempt that would double-count anything, OR document precisely what row WAS left and that it is harmless (an unfinished gate_runs
  row from the failed Gate 3 attempt is expected and fine; a row that looks like it SUCCEEDED when it did not would be the actual bug
  to catch here).
- Every repair `reconcile` made is logged (`events.recent`/`fake.events` -- read the real event kind, likely `reconcile_repair`).
- The task eventually completes normally after the restart.

## Crash point 3: between the fast-forward and the merge-card completion
This is the crash point the reconcile builder's own tests already cover in isolation ("a landed commit with the record but not the
card, and one with neither the record nor the card completed" -- read `test_reconcile.py` for the EXACT scenario names and reproduce
one of them through the REAL controller instead of hand-built fixtures). Get a card to the point where `mergeq.merge_task` has
returned `outcome.merged=True` with a real `squash_commit` (the fast-forward happened, verify with `git` that `integration` moved),
but simulate the crash BEFORE `controller.process_merge_queue` calls `hermes.kanban_complete` on the merge card: monkeypatch
`hermes.kanban_complete` (which `fake.install` already pointed at the `FakeHermes` instance -- monkeypatch the FAKE's method, or wrap
it) to raise the FIRST time it is called for this specific merge card id, call `world.one_pass()` and catch the exception, remove the
monkeypatch, `world.restart_controller()`, run `reconcile.reconcile(..., apply=True)`. Assert:
- The integration branch already has the squash commit (it did land -- a crash here is AFTER the git write, which is the whole point
  of this crash point existing in the blueprint's list).
- `reconcile` finds the commit on `integration` (by its `Merge card: <id>` message, per the real repair rule) and completes the merge
  card itself (`fake.card(merge_card_id)["status"] == "done"`), WITHOUT re-running Gate 3 or re-doing the fast-forward (no second
  squash commit, no duplicate work).
- No duplicate cards, no orphan workers, every repair logged.
- The next task (if this task had a dependent) proceeds correctly, proving the dependent's parent-satisfaction check reads the
  now-`done` merge card correctly after the repair.

## Shared assertions across all three
After EACH crash point's recovery, before moving to the next: `reconcile.reconcile(..., apply=False)` (a second, dry-run call) reports
NOTHING new (idempotence: a clean board after repair stays clean), matching the reconcile builder's own tested guarantee -- reproduce
it here as an end-to-end check, not just trust the unit test. Use THREE separate `world_factory()` worlds (one per crash point, each
with its own single-task or two-task plan) rather than reusing one world for all three, so a mistake in one does not contaminate
another and each test function is independently readable and independently rerunnable.

## Report back
The usual report, plus: for each of the three crash points, EXACTLY which function you monkeypatched to simulate the crash and why
that is a faithful simulation of "the controller process died at this instant" (a git write and a database write are not atomic with
each other, so be precise about what state a real crash could leave that your simulation reproduces). If any of the three could not be
faithfully simulated without a change to product code you are not allowed to make, say so plainly rather than writing a test that
does not actually prove what its name claims.
