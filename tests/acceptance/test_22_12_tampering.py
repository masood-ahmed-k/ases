"""Acceptance 22.12: gate tampering (blueprint.txt [p421]/[p422]; ASES-QG-02, ASES-QG-03, ASES-REV-05, ASES-GIT-13).

"The fake worker deletes a failing test, then adds a skip marker, then appends || true to the test command, then
edits a file outside its allowed paths, then leaves an untracked file that would make the build pass. Each attempt
must fail Gate 1 with the right finding." [p422]

Each test below drives the REAL controller (controller.run_pass -> review.gate_before_review ->
review.check_branch -> tamper.check_range / gates.run_gate) against a single-task world with a REAL gate profile
(python -m pytest -q on a seeded test file that starts red), using fw.tampering_coder(kind) for the attempt.

review.check_branch runs the checks in this order: scope, then tamper, then Gate 1. spec/requirements.yaml's own
note on ASES-QG-03 (status in_progress, "BUILT AND WIRED, NOT RUN FOR REAL") says why that order matters here:
"the gate-configuration and assertion-weakening findings can never fire ... because the scope check runs first
and a path that reaches the tamper check is already inside the task's touches". That is exactly why
"outside_paths" below is caught by the SCOPE check (out_of_scope), never by tamper.py, and why the other four
kinds need their target files inside the task's declared touches to reach the tamper check at all.

Every attempt is sent back before a reviewer is ever spawned. Hermes's review lane only spawns a reviewer for
cards that were ALREADY sitting in `review` when its own spawn phase started that pass; controller.run_pass runs
the review-lane re-check (which can send a card straight back to `ready`) before dispatch in the SAME pass, so a
card that gets sent back is never seen as "in review" by that pass's spawn phase. But a reopened `ready` card has
no claim lock, so dispatch in that SAME pass immediately re-dispatches it to `coder-1`. tampering_coder's steps
are not safe to run twice on the same branch (Delete on an already-deleted file raises, for one), and
FakeHermes._run_worker deliberately re-raises a worker script's exception rather than turning it into a graceful
"crashed" run ("An exception from a worker script is a bug in the test, so it propagates"). So coder-1 here is
always a fw.sequence of the tampering attempt followed by a harmless fw.questioner, which only calls agent_block
and touches no git state, to absorb that same-pass redispatch safely.
"""
import json

from ases import events
from ases.fakes import worker as fw

PLAN = {
    "project": "acceptance-tamper",
    "integration_branch": "integration",
    "gate_profiles": {"real": ["python -m pytest -q tests/test_feature.py"]},
    "tasks": [
        {
            "key": "T1", "title": "implement the feature", "role": "coder", "depends_on": [],
            "touches": ["tests/test_feature.py", "scripts/test.sh", "app.py", "helper.py"],
            "acceptance": ["tests/test_feature.py passes"], "gate_profile": "real", "estimated_requests": 5,
        },
    ],
}

# A test that fails until someone does the real work: every tampering attempt below tries to turn this gate green
# without doing that work. scripts/test.sh is seeded too (unused by the real gate command above, which calls
# pytest directly) so the or_true kind's default target file already exists to append to, like a real test runner.
SEED = {
    "tests/test_feature.py": 'def test_feature():\n    assert False, "not implemented yet"\n',
    "scripts/test.sh": "#!/bin/sh\npytest -q\n",
}


def _attempt(world_factory, kind: str):
    """Build the one-task world above, dispatch fw.tampering_coder(kind) once, and run the controller until its
    review-lane re-check has sent the card back (a review_reopened event on the work card). Returns (world,
    work_card_id, merge_card_id)."""
    world = world_factory(plan_raw=PLAN, seed=SEED)
    world.fake.register_worker("coder-1", fw.sequence(
        fw.tampering_coder(kind),
        fw.questioner("halting further attempts for this test"),
    ))
    pair = world.create_cards()["T1"]
    work = pair.work_card_id

    world.run_until(lambda w: any(e["kind"] == "review_reopened" for e in w.card(work)["_events"]))
    return world, work, pair.merge_card_id


def _assert_sent_back_before_any_reviewer_ran(world, work: str) -> None:
    """Common to every kind: the card never reached done, and no `reviewer` run ever happened, since the
    controller's own re-check caught the attempt before a reviewer was ever spawned for it."""
    card = world.card(work)
    assert card["status"] != "done"
    assert not any(run["profile"] == "reviewer" for run in card["_runs"])


def test_22_12_delete_test_fails_gate_1_with_test_file_deleted(world_factory):
    """The whole test file is deleted (fw.TAMPER_KINDS' "delete_test", the default path tests/test_feature.py).
    tamper.py sees a deleted, test-shaped file, not a removed test definition inside a modified one, so the
    finding kind is test_file_deleted, not test_deleted (ASES-QG-03). Caught by the tamper check: Gate 1's own
    pytest run never happens for this attempt at all."""
    world, work, merge = _attempt(world_factory, "delete_test")
    _assert_sent_back_before_any_reviewer_ran(world, work)

    recorded = events.recent(world.conn, limit=500)
    kinds = {e["kind"] for e in recorded}
    assert {"tamper_blocked", "gate1_recheck_failed"} <= kinds

    (blocked,) = [e for e in recorded if e["kind"] == "tamper_blocked"]
    payload = json.loads(blocked["payload"])
    assert payload["task_key"] == "T1" and payload["card_id"] == work
    assert "test_file_deleted" in payload["detail"]
    assert "tests/test_feature.py" in payload["detail"]

    # The tamper check short-circuits check_branch before Gate 1 ever runs (ASES-QG-03): no gate_runs row at all.
    assert world.conn.execute(
        "SELECT COUNT(*) FROM gate_runs WHERE task_key = 'T1'").fetchone()[0] == 0


def test_22_12_skip_marker_fails_gate_1_with_skip_marker(world_factory):
    """A @pytest.mark.skip is inserted right before the failing test. Left to Gate 1 alone this would make the
    real pytest run report success (1 skipped, exit 0): exactly why the tamper check, not the gate run, has to
    catch it (ASES-QG-03)."""
    world, work, merge = _attempt(world_factory, "skip_marker")
    _assert_sent_back_before_any_reviewer_ran(world, work)

    (blocked,) = [e for e in events.recent(world.conn, limit=500) if e["kind"] == "tamper_blocked"]
    payload = json.loads(blocked["payload"])
    assert "skip_marker" in payload["detail"]
    assert "pytest.mark.skip" in payload["detail"]


def test_22_12_or_true_fails_gate_1_with_unconditional_pass(world_factory):
    """`pytest || true` is appended to scripts/test.sh. The "|| true" row in tamper.py's marker table has no
    script_only restriction, so it is caught wherever it appears in the diff, not only in a file the approved
    gate profile happens to execute (ASES-QG-03)."""
    world, work, merge = _attempt(world_factory, "or_true")
    _assert_sent_back_before_any_reviewer_ran(world, work)

    (blocked,) = [e for e in events.recent(world.conn, limit=500) if e["kind"] == "tamper_blocked"]
    payload = json.loads(blocked["payload"])
    assert "unconditional_pass" in payload["detail"]
    assert "|| true" in payload["detail"]


def test_22_12_outside_paths_is_caught_by_the_scope_check_not_the_tamper_check(world_factory):
    """Writing OUTSIDE_TOUCHES.md is not a tamper.py finding at all: review.check_branch runs its scope check
    (ASES-GIT-13) before the tamper check, so an out-of-scope diff is refused as "out_of_scope" and the tamper
    check never runs. No tamper_blocked event is recorded for this one; the refusal is the scope check's own
    "diff touches paths outside the card's declared touches" text, left on the card as the reopen-review comment."""
    world, work, merge = _attempt(world_factory, "outside_paths")
    _assert_sent_back_before_any_reviewer_ran(world, work)

    kinds = {e["kind"] for e in events.recent(world.conn, limit=500)}
    assert "gate1_recheck_failed" in kinds
    assert "tamper_blocked" not in kinds  # this kind is a scope violation, never a tamper.py finding

    comments = [c["body"] for c in world.card(work)["_comments"]]
    (reopen_comment,) = [c for c in comments if c.startswith("CHANGES REQUESTED:")]
    assert "outside the card's declared touches" in reopen_comment
    assert "OUTSIDE_TOUCHES.md" in reopen_comment


def test_22_12_untracked_file_cannot_rescue_a_red_gate(world_factory):
    """fw.tampering_coder("untracked") commits app.py and leaves helper.py on disk but never adds it to git.
    gates.run_gate cuts a throwaway worktree from the exact commit (ASES-QG-04), so the untracked helper.py never
    reaches it. The diff itself (just app.py, importing a module that happens not to exist at that commit) has no
    tamper.py finding, so this is the one kind where check_branch reaches Gate 1 for real, runs it, and it is red
    for the ORDINARY reason: the seeded test was never fixed. That is a different, if real, mechanism than the
    blueprint's prose ("leaves an untracked file that would make the build pass") suggests read literally: nothing
    here ever detects a "missing helper module" specifically, because pytest is never pointed at app.py at all."""
    world, work, merge = _attempt(world_factory, "untracked")
    _assert_sent_back_before_any_reviewer_ran(world, work)

    kinds = {e["kind"] for e in events.recent(world.conn, limit=500)}
    assert "gate1_recheck_failed" in kinds
    assert "tamper_blocked" not in kinds  # the diff has no tamper finding; Gate 1 ran for real and came back red

    gate_run = world.conn.execute(
        "SELECT result, detail FROM gate_runs WHERE task_key = 'T1' ORDER BY id DESC LIMIT 1").fetchone()
    assert gate_run["result"] == "fail"
    assert "test_feature" in gate_run["detail"]  # the ORIGINAL seeded test failed; nothing about a missing helper
    assert "helper" not in gate_run["detail"]
