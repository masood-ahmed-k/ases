"""Acceptance 22.7: crash recovery, all three crash points (blueprint [p411]/[p412]).

[p412]: "Kill the controller with SIGKILL during a running card, again during a candidate build, and again
between the fast-forward and the merge-card completion. After each restart: no duplicate cards, no orphan
workers, no half-merged state, the ledger is intact, and every repair is logged."

Requirement IDs this package proves end to end (spec/requirements.yaml, all `verified_by: ... 22.7`):
ASES-REC-04 ("Reconcile-on-start repairs what is safe and blocks the rest"), ASES-ARC-02 ("The controller
never claims cards, spawns workers or keeps its own copy of task status"), ASES-ARC-03 ("ASES records are
keyed by card ID and commit SHA; the controller reconciles on start") and ASES-CAP-02 ("A persisted request
ledger per provider, model and UTC day", checked here for the ledger staying untouched by reconcile itself).
ASES-REC-03 ("Card creation is idempotent") is touched by crash point 2's open create/build intent but is
verified_by 22.15, not this package.

A real SIGKILL of this test process is not possible, so each scenario drives the REAL controller
(controller.run_pass, mergeq, reconcile) up to the exact instant of interest and then stops it the way
tests/unit/test_reconcile.py's own crash-recovery scenarios do (see its "Section 22.7" block and
test_crash_a/b/c): either by never letting a step finish (crash point 1: ases.fakes.board.FakeHermes's own
kill_worker, an out-of-band process kill that runs no ASES code at all) or by making the one function the
crashing step was about to call raise a marker exception caught here, OUTSIDE the normal flow (crash points 2
and 3). A git write and a database write are never atomic with each other, so each scenario's docstring says
precisely which side of that gap its crash lands on, and why that is what a real process death there would
leave behind.

Each crash point uses its own world (world_factory or the default two-task world), never a shared one, so a
mistake in one cannot contaminate another."""
from __future__ import annotations

import json

import pytest

from ases import gates as gates_mod
from ases import reconcile
from ases.fakes import worker as fw

A_PY = "def add(x, y):\n    return x + y\n"


class _SimulatedCrash(Exception):
    """Raised in place of the call the controller was about to make, and caught here, outside the normal
    flow: this is what "the process died at this instant" looks like from the caller's side. A type of its
    own (never HermesCommandError, which a legitimate Hermes refusal would also raise, and never a bare
    RuntimeError) so `pytest.raises` can only ever catch the fault THIS test injected, never mask an
    unrelated bug in the code under test."""


def _repair_events(conn) -> list[dict]:
    """Every reconcile_repair event, oldest first, as its payload dict (events.record stores the payload as a
    JSON string: see ases.events.record and ases.events.recent)."""
    return [
        json.loads(row["payload"])
        for row in conn.execute("SELECT payload FROM events WHERE kind = 'reconcile_repair' ORDER BY id")
    ]


def _repair_kinds(report) -> list[tuple[str, str, bool]]:
    return [(r.task_key, r.kind, r.applied) for r in report.repairs]


def _finding_kinds(report) -> list[tuple[str, str]]:
    return [(f.task_key, f.kind) for f in report.findings]


def _ledger(conn) -> list[tuple]:
    return [tuple(row) for row in conn.execute(
        "SELECT * FROM requests_ledger ORDER BY provider, model, utc_date")]


def _assert_every_applied_repair_is_logged_once(conn, report) -> None:
    """[p412]: "every repair is logged". Each applied repair (never a merely reported, non-applied one, such
    as an apply=False dry run or an informational candidate_discarded) is in the events table exactly once,
    in the order reconcile.reconcile() returned it."""
    applied = [(r.task_key, r.kind) for r in report.repairs if r.applied]
    assert [(e["task_key"], e["kind"]) for e in _repair_events(conn)] == applied


def _assert_second_dry_run_finds_nothing_new(world) -> None:
    """The reconcile builder's own guarantee (test_reconcile.py: "a second run after applying finds nothing
    new"), reproduced here end to end through the real controller and a real database, not just trusted from
    the unit test: a clean board after repair stays clean. apply=False so this check can never itself write
    anything, which is also why it must be the LAST reconcile call in each scenario."""
    again = reconcile.reconcile(world.board, world.repo, world.plan, conn=world.conn, apply=False)
    assert again.clean and again.blocked == []
    assert not any(r.applied for r in again.repairs)


# ---------------------------------------------------------------------------------------------
# Crash point 1: during a running card
# ---------------------------------------------------------------------------------------------


def test_22_7_crash_point_1_during_a_running_card(world_factory, one_task_plan, run_until, git):
    """22.7 crash point 1: "Kill the controller with SIGKILL during a running card."

    Faithful simulation: fake.kill_worker() (its docstring: "Kill the card's worker process from outside...
    The card stays running with a dead PID until the next reclaim phase... books a crash, exactly as Hermes
    does") marks the worker process dead from OUTSIDE the controller, without running a single line of ASES
    code. That is exactly what a controller (or the whole host) dying mid-run leaves on the board side: a
    card still `running`, a run row with a real pid, and that pid no longer alive. No exception is raised or
    caught for this one, because nothing in ASES was even executing at the moment of the kill.

    The coder is scripted to "hang" (Sleep for far longer than this test will ever tick) on its FIRST
    dispatch, simulating a worker still doing real work when the host died, and to finish normally on its
    SECOND dispatch: a fresh worker, started by a later, healthy controller pass, after reconcile has put the
    card back in `ready`. reconcile's own worker-liveness probe is the real one (reconcile.pid_alive, the
    default): FakeHermes hands out pids from 2,100,000,000 upward, "far above every real PID on Windows and
    Linux" (ases/fakes/board.py), so the real process prober correctly reports the killed fake pid as dead
    without a single fake ever needing to be injected for it.
    """
    world = world_factory(plan_raw=one_task_plan)
    fake = world.fake
    fake.register_worker("coder-1", fw.sequence(
        fw.slow_coder({"a.py": A_PY}, seconds=999_999),
        fw.good_coder({"a.py": A_PY}, "add a.py"),
    ))
    t1 = world.create_cards()["T1"]
    assert len(fake.cards()) == 2  # the work card and the merge card: the "no duplicates" baseline starts here

    world.one_pass()  # dispatches T1: the coder writes a.py, heartbeats, then hangs -- the card stays running
    assert fake.card(t1.work_card_id)["status"] == "running"
    assert len(fake.live_workers()) == 1 and fake.live_workers()[0]["orphan"] is False

    fake.kill_worker(t1.work_card_id)
    assert fake.live_workers() == []  # dead: it no longer even shows up as a live, let alone orphan, worker
    ledger_before = _ledger(world.conn)

    world.restart_controller()
    report = reconcile.reconcile(world.board, world.repo, world.plan, conn=world.conn, apply=True)

    # findings/repairs: reconcile's own "running card whose worker is gone" repair, not Hermes's own dispatch-
    # time crash detection (kanban_dispatch's _detect_crashed_workers, gated by a 30-second crash grace) --
    # nothing dispatches or ticks between the kill and this call, so there is nothing else that could have
    # reclaimed it first.
    assert _finding_kinds(report) == [("T1", "worker_gone")]
    assert _repair_kinds(report) == [("T1", "worker_gone_reclaimed", True)]
    assert report.blocked == []
    assert fake.card(t1.work_card_id)["status"] == "ready"  # reclaimed: free to be claimed by a fresh worker
    _assert_every_applied_repair_is_logged_once(world.conn, report)

    # [p412]: no duplicate cards, no orphan workers, no half-merged state, the ledger is intact
    assert len(fake.cards()) == 2
    assert fake.live_workers() == []
    assert reconcile.check(world.board, world.plan.project, conn=world.conn) == []
    assert _ledger(world.conn) == ledger_before  # reconcile's repair never touches requests_ledger

    _assert_second_dry_run_finds_nothing_new(world)

    # and the task completes normally after the restart, via the fresh worker started on a later pass
    run_until(world, lambda w: w.all_merge_cards_done())
    assert git(world, "show", "integration:a.py") == A_PY.strip()


# ---------------------------------------------------------------------------------------------
# Crash point 2: during a candidate build
# ---------------------------------------------------------------------------------------------


def test_22_7_crash_point_2_during_a_candidate_build(world_factory, one_task_plan, run_until, git, monkeypatch):
    """22.7 crash point 2: "again during a candidate build."

    Faithful simulation: mergeq.merge_task opens its build_candidate intent (KIND_BUILD_CANDIDATE) BEFORE it
    asks Gate 3 to run -- one `with intents.intent(..., KIND_BUILD_CANDIDATE, ...)` block wraps BOTH
    `_build_candidate` (which builds the real squash commit in a throwaway worktree) AND the
    `gates.run_gate(..., "gate3", ...)` call -- and only writes the merge_records row for this candidate
    AFTER Gate 3 returns (mergeq._record_candidate, called only once gate_result exists). Monkeypatching
    `gates.run_gate` to raise for a "gate3" call lands the crash strictly after the candidate commit exists
    but before Gate 3 (and so before the fast-forward, which mergeq never reaches): the build_candidate
    intent is left open and NO merge_records row is written for this attempt at all. That is the
    row_written=False shape test_reconcile.py's own test_crash_b_kill_during_a_candidate_build already proves
    in isolation; this reproduces it end to end, through the real controller, review lane and a real Gate 3
    for every OTHER gate call this pass makes.

    The stub only intercepts a "gate3" call (gate_name is run_gate's third positional argument, `repo.py`'s
    own Gate 1 re-check calls the very same function with "gate1"): process_review_lane also calls
    gates.run_gate through review._run_gate1, and a T1 sitting in `review` gets re-checked on the very next
    pass, so a stub that crashed on ANY gate name would crash Gate 1 instead of Gate 3 and prove nothing about
    this crash point.
    """
    world = world_factory(plan_raw=one_task_plan)
    fake = world.fake
    fake.register_worker("coder-1", fw.good_coder({"a.py": A_PY}, "add a.py"))
    t1 = world.create_cards()["T1"]
    head_before = git(world, "rev-parse", "integration")

    real_run_gate = gates_mod.run_gate

    def _crash_only_on_gate3(*args, **kwargs):
        gate_name = args[2] if len(args) > 2 else kwargs.get("gate_name")
        if gate_name == "gate3":
            raise _SimulatedCrash("controller died before Gate 3 (mergeq.merge_task's run_gate call)")
        return real_run_gate(*args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(gates_mod, "run_gate", _crash_only_on_gate3)
        with pytest.raises(_SimulatedCrash):
            # however many passes it takes for the coder to write, commit and hand off, and the reviewer to
            # approve: the merge queue only ever reaches T1 once its work card is done, and this stub crashes
            # that very first attempt, so the loop always stops here rather than merging successfully.
            for _ in range(10):
                world.one_pass()

    assert fake.card(t1.work_card_id)["status"] == "done"        # the review side finished normally
    assert fake.card(t1.merge_card_id)["status"] == "blocked"    # ... but nothing was ever merged
    assert git(world, "rev-parse", "integration") == head_before
    assert world.conn.execute(
        "SELECT COUNT(*) FROM merge_records WHERE task_key = 'T1'").fetchone()[0] == 0
    ledger_before = _ledger(world.conn)

    world.restart_controller()
    report = reconcile.reconcile(world.board, world.repo, world.plan, conn=world.conn, apply=True)

    # T1 itself was never inconsistent (a done work card with a still-blocked, unmerged merge card is the
    # ordinary "not merged yet" state, not a finding): the only thing to recover is the open intent, closed as
    # "consistent, nothing to repair" (reconcile.py: "closing an intent is not an inconsistency").
    assert report.findings == []
    assert _repair_kinds(report) == [("T1", "intent_recovered", True)]
    assert report.clean and report.blocked == []
    _assert_every_applied_repair_is_logged_once(world.conn, report)

    # [p412]: no duplicate cards, no orphan workers (a candidate build spawns no worker at all: there was
    # never one to orphan), no half-merged state, the ledger is intact
    assert len(fake.cards()) == 2
    assert fake.live_workers() == []
    assert reconcile.check(world.board, world.plan.project, conn=world.conn) == []
    assert git(world, "rev-parse", "integration") == head_before
    assert _ledger(world.conn) == ledger_before

    _assert_second_dry_run_finds_nothing_new(world)

    # the next merge pass redoes the candidate build cleanly, with no crash injected this time, and the task
    # completes
    run_until(world, lambda w: w.all_merge_cards_done())
    assert git(world, "show", "integration:a.py") == A_PY.strip()
    row = world.conn.execute(
        "SELECT gate3_result, squash_commit FROM merge_records WHERE task_key = 'T1'").fetchone()
    assert (row["gate3_result"], row["squash_commit"]) == ("pass", git(world, "rev-parse", "integration"))


# ---------------------------------------------------------------------------------------------
# Crash point 3: between the fast-forward and the merge-card completion
# ---------------------------------------------------------------------------------------------


def test_22_7_crash_point_3_between_fast_forward_and_merge_card_completion(world, run_until, git):
    """22.7 crash point 3: "again between the fast-forward and the merge-card completion." Reproduces, through
    the real controller instead of the hand-built fixtures, the exact scenario test_reconcile.py's own
    test_c_a_completed_record_with_the_card_not_done_completes_the_card already proves in isolation: "a landed
    commit with the record but not the card." Uses the default two-task world (T2 depends on T1's MERGE card,
    per blueprint 22.2) so the repair's effect on a gated dependent can be checked too, as this package's own
    instructions ask.

    Faithful simulation: by the time controller.process_merge_queue calls hermes.kanban_complete on the merge
    card, mergeq.merge_task has already returned with the fast-forward done: mergeq._fast_forward runs `git
    merge --ff-only` for real AND updates merge_records (squash_commit, completed_at) itself, before
    process_merge_queue ever sees the outcome. A git write and a database write are never atomic with each
    other, and this is the one crash point in 22.7 that is defined by that exact gap: the commit and the
    record both already exist, and only the board (the merge card's status) has not been told yet -- and
    process_merge_queue also wraps that one kanban_complete call in its own KIND_COMPLETE_MERGE_CARD intent, so
    the crash leaves that open too. Reproduced with fake.fail_next("kanban_complete", card_id=t1.merge_card_id,
    ...): fail_next always fires before the real call's effect (board.py's own docstring for it), so the merge
    card's status is provably untouched by this one call, exactly what "the process died before this call's
    effect was observed" requires; scoped to this one merge card's id, so a coder's own agent_complete on a
    WORK card (a different fake method entirely) is never touched by it, and every other kanban_complete call
    passes straight through.

    (Round 17: this test used to have to monkeypatch hermes.kanban_complete directly instead, because
    fail_next's own validation once rejected every name once a fake was already installed (world/world_factory
    hand out one that always is) -- fixed at round 6 (src/ases/fakes/board.py's _HERMES_PUBLIC_NAMES) but never
    simplified back to the natural call here until this round confirmed, with a card_id-filtered regression
    test of its own (tests/unit/test_fakes.py: test_fail_next_card_id_filter_also_survives_install), that the
    fix covers this exact shape too.)
    """
    fake = world.fake
    pairs = world.create_cards()
    t1, t2 = pairs["T1"], pairs["T2"]

    fake.fail_next(
        "kanban_complete", card_id=t1.merge_card_id,
        error=_SimulatedCrash("controller died after the fast-forward, before kanban_complete on the merge card"))

    with pytest.raises(_SimulatedCrash):
        for _ in range(10):
            world.one_pass()

    row = world.conn.execute(
        "SELECT squash_commit, completed_at FROM merge_records WHERE task_key = 'T1'").fetchone()
    sha = row["squash_commit"]
    assert sha and row["completed_at"]                       # the fast-forward really landed and finished
    assert git(world, "rev-parse", "integration") == sha
    assert fake.card(t1.merge_card_id)["status"] == "blocked"  # ... but the merge card was never told
    assert fake.card(t2.work_card_id)["status"] == "todo"      # T2 is still gated: its parent is not done yet
    commits_before = git(world, "rev-list", "--count", "integration")
    ledger_before = _ledger(world.conn)

    world.restart_controller()
    report = reconcile.reconcile(world.board, world.repo, world.plan, conn=world.conn, apply=True)

    # controller.process_merge_queue also wraps the kanban_complete call itself in a KIND_COMPLETE_MERGE_CARD
    # intent (ases/controller.py), left open by the same crash: reconcile both completes the merge card from
    # the record (merge_card_completed) and then finds the task consistent again and closes that intent
    # (intent_recovered), in that order -- the same two-repair shape as test_reconcile.py's own
    # test_e_an_intent_whose_task_was_repaired_is_recovered_and_says_what_was_repaired.
    assert _finding_kinds(report) == [("T1", "merge_record_without_done_card")]
    assert _repair_kinds(report) == [("T1", "merge_card_completed", True), ("T1", "intent_recovered", True)]
    assert report.blocked == []
    assert fake.card(t1.merge_card_id)["status"] == "done"
    _assert_every_applied_repair_is_logged_once(world.conn, report)

    # [p412]: no duplicate cards, no orphan workers, and specifically no SECOND squash commit or duplicate
    # merge (the fast-forward is never redone, only the card catches up to what git and the database already
    # say happened)
    assert len(fake.cards()) == 4
    assert fake.live_workers() == []
    assert git(world, "rev-parse", "integration") == sha
    assert git(world, "rev-list", "--count", "integration") == commits_before
    assert reconcile.check(world.board, world.plan.project, conn=world.conn) == []
    assert _ledger(world.conn) == ledger_before

    _assert_second_dry_run_finds_nothing_new(world)

    # the dependent task's parent-satisfaction check reads the now-done merge card correctly after the repair
    run_until(world, lambda w: w.all_merge_cards_done())
    assert [fake.card(pair.merge_card_id)["status"] for pair in (t1, t2)] == ["done", "done"]
