"""Acceptance 22.6, full scenario (blueprint.txt [p409]/[p410]; Appendix F: ASES-GIT-03, ASES-CTL-01,
ASES-REV-04, ASES-REV-05, ASES-REV-06, ASES-REC-01, ASES-REC-02, ASES-ROL-05, ASES-ROL-06, ASES-ROL-11).

[p410]: "The fake worker violates an acceptance criterion. The Reviewer must return CHANGES_REQUIRED through the
verdict tool call with structured metadata, the card must go back to its implementer, and only the corrected
commit may merge. A commit added after the review voids the approval. Repeat the violation until the lineage
budget is reached: the task must escalate instead of looping."

Round 17: this file replaces tests/acceptance/test_scenarios_demo.py's 22.6 half, which its own docstring called
"cores only" and left for "a later round" -- its one function proved the first three clauses (CHANGES_REQUIRED
through the verdict tool call, the card back to its implementer, only the corrected commit merges) but neither
of the last two ("a commit added after the review voids the approval", "repeat ... until the lineage budget is
reached ... escalate instead of looping") had any test at all, at any level, end to end through the real
controller. That function is moved here verbatim (nothing it asserted is dropped), and the two missing clauses
are added as their own scenarios below. test_scenarios_demo.py itself is removed as part of this round (see the
round 17 report).

Drives the REAL controller (controller.run_pass, review, mergeq, recovery, questions) against
ases.fakes.board.FakeHermes, with real git worktrees and scripted workers. Nothing here starts Hermes, a model,
a network connection or Docker."""
from __future__ import annotations

import json
import subprocess

from ases import events, questions, recovery
from ases import review as review_mod
from ases.fakes import worker as fw

A_PY = "def add(x, y):\n    return x + y\n"
A_WRONG = "def add(x, y):\n    return x - y\n"


def _commit_subjects(git, world) -> list[str]:
    return git(world, "log", "--format=%s", "integration").splitlines()


# ---------------------------------------------------------------------------------------------
# Moved verbatim from the removed test_scenarios_demo.py: CHANGES_REQUIRED through the verdict tool call with
# structured metadata, the card back to its implementer, only the corrected commit merges.
# ---------------------------------------------------------------------------------------------


def test_22_6_reviewer_returns_changes_once_and_only_the_corrected_commit_merges(
    world_factory, one_task_plan, run_until, git,
):
    """Blueprint 22.6: "The fake worker violates an acceptance criterion. The Reviewer must return CHANGES_REQUIRED
    ..., the card must go back to its implementer, and only the corrected commit may merge." ASES-REV-05/06,
    ASES-GIT-03."""
    world = world_factory(plan_raw=one_task_plan)
    fake = world.fake
    fake.register_worker("coder-1", fw.sequence(
        fw.wrong_coder({"a.py": A_WRONG}, "add a.py"),
        fw.good_coder({"a.py": A_PY}, "fix add: return x + y"),
    ))
    fake.register_worker("reviewer", fw.sequence(
        fw.reviewer_changes(["add(x, y) must return x + y, not x - y"]),
        fw.reviewer_pass(),
    ))
    t1 = world.create_cards()["T1"]
    work = t1.work_card_id

    # The wrong commit is reviewed and sent back to its implementer, not merged.
    run_until(world, lambda w: any(e["kind"] == "changes_requested" for e in w.card(work)["_events"]))
    card = world.card(work)
    assert (card["status"], card["assignee"]) == ("ready", "coder-1")
    (changes,) = [e for e in card["_events"] if e["kind"] == "changes_requested"]
    assert "x + y" in changes["payload"]["reason"] and changes["payload"]["implementer"] == "coder-1"
    wrong_commit = git(world, "rev-parse", "swarm/T1-coder")
    assert git(world, "show", f"{wrong_commit}:a.py") == A_WRONG.strip()
    assert world.card(t1.merge_card_id)["status"] == "blocked"
    assert git(world, "rev-parse", "integration") == world.plan_sha  # nothing merged yet

    # The implementer corrects it, the reviewer passes the new commit, and that is what merges.
    run_until(world, lambda w: w.all_merge_cards_done())
    corrected_commit = git(world, "rev-parse", "swarm/T1-coder")
    assert corrected_commit != wrong_commit

    card = world.card(work)
    assert [(r["profile"], r["outcome"]) for r in card["_runs"]] == [
        ("coder-1", "review_requested"), ("reviewer", "changes_requested"),
        ("coder-1", "review_requested"), ("reviewer", "completed"),
    ]
    assert git(world, "show", "integration:a.py") == A_PY.strip()
    assert "x - y" not in git(world, "log", "-p", "integration", "--", "a.py")
    assert _commit_subjects(git, world)[:2] == ["T1: add a", "ASES: publish approved plan (Gate P)"]
    assert git(world, "rev-parse", "integration~1") == world.plan_sha  # one commit landed on top of the plan

    # The approval that counted names the corrected commit, and only that one was recorded (ASES-GIT-03: a
    # verdict belongs to one commit).
    verdicts = world.conn.execute(
        "SELECT commit_sha, outcome, reviewer_profile FROM review_verdicts WHERE task_key = 'T1'").fetchall()
    assert [(v["commit_sha"], v["outcome"], v["reviewer_profile"]) for v in verdicts] == [
        (corrected_commit, "PASS", "reviewer")]
    assert fake.card(work)["_runs"][-1]["metadata"]["commit"] == corrected_commit  # the verdict named the corrected commit

    # The controller never used its own send-back (Gate 1 stayed green); the reviewer's request is what counted
    # as a review round, and the recovery module reads it off the events the fake wrote.
    assert not [call for call in fake.calls if call.name == "kanban_reopen_review"]
    recovery.refresh_review_rounds(world.board, world.plan, conn=world.conn)  # idempotent: only counts what is new
    assert recovery.load_lineage(world.conn, world.plan.project, "T1").review_rounds == 1
    kinds = {e["kind"] for e in events.recent(world.conn, limit=500)}
    assert "gate1_recheck_failed" not in kinds and "merged" in kinds


# ---------------------------------------------------------------------------------------------
# New: "A commit added after the review voids the approval."
# ---------------------------------------------------------------------------------------------


def test_22_6_a_commit_added_after_the_reviewers_pass_voids_the_approval_and_needs_its_own_review(
    world_factory, one_task_plan, run_until, git, monkeypatch,
):
    """Blueprint 22.6 (p410): "A commit added after the review voids the approval." review.check_branch_for_merge's
    own docstring names the exact mechanism this proves end to end (ASES-GIT-03): "a head that does not start
    with the reviewed commit means a later commit exists: stale_review ... a later commit voids the review and
    the gate record ... so it needs its own Gate 1 run and a reviewer PASS of its own before it can merge."

    A scripted worker cannot represent this clause on its own: a coder is never redispatched to a card that is
    already `done`, which is what the work card becomes the instant the reviewer completes it. The extra commit
    is therefore written directly into the card's real worktree, out of band, at the one instant that matters:
    right before the controller's own merge-time check (controller.process_merge_queue's call to
    review.check_branch_for_merge) reads the branch head, reproduced here by wrapping that function once rather
    than hoping two controller passes land on either side of the write."""
    world = world_factory(plan_raw=one_task_plan)
    fake = world.fake
    fake.register_worker("coder-1", fw.good_coder({"a.py": A_PY}, "add a.py"))
    fake.register_worker("reviewer", fw.reviewer_pass())
    t1 = world.create_cards()["T1"]

    real_check = review_mod.check_branch_for_merge
    sneaked = {"done": False, "extra_commit": None}

    def _check_after_a_stray_commit_lands(*args, **kwargs):
        if not sneaked["done"]:
            sneaked["done"] = True
            worktree = fake.worktree(t1.work_card_id)
            a_py = worktree / "a.py"
            a_py.write_text(a_py.read_text(encoding="utf-8") + "# a stray write after the review\n", encoding="utf-8")
            identity = ["-c", "user.name=out of band", "-c", "user.email=out-of-band@example.invalid"]
            subprocess.run(["git", "-C", str(worktree), *identity, "add", "-A"], check=True, capture_output=True)
            subprocess.run(
                ["git", "-C", str(worktree), *identity, "commit", "-q", "-m", "a stray write after the review"],
                check=True, capture_output=True)
            sneaked["extra_commit"] = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "HEAD"], capture_output=True, text=True,
            ).stdout.strip()
        return real_check(*args, **kwargs)

    monkeypatch.setattr(review_mod, "check_branch_for_merge", _check_after_a_stray_commit_lands)

    def _fix_card_opened(w) -> bool:
        return w.conn.execute(
            "SELECT 1 FROM events WHERE kind = 'fix_card_created' AND json_extract(payload, '$.task_key') = 'T1'"
        ).fetchone() is not None

    run_until(world, _fix_card_opened, max_passes=60)
    assert sneaked["done"] is True
    assert git(world, "rev-parse", "integration") == world.plan_sha  # the stale approval never merged anything

    fix_row = world.conn.execute(
        "SELECT payload FROM events WHERE kind = 'fix_card_created' "
        "AND json_extract(payload, '$.task_key') = 'T1'").fetchone()
    fix_card_id = json.loads(fix_row["payload"])["fix_card_id"]
    fix_body = fake.card(fix_card_id)["body"]
    assert "stale_review" in fix_body and "voids the review" in fix_body and "reviewer PASS of its own" in fix_body

    # The stale approval was never even recorded: only a review whose commit matches the head it names reaches
    # review_verdicts at all (ASES-GIT-03, ASES-REV-06).
    assert world.conn.execute("SELECT COUNT(*) FROM review_verdicts WHERE task_key = 'T1'").fetchone()[0] == 0

    # The fix card gets its OWN fresh worktree (a new branch cut from the current integration HEAD, not the
    # tainted one) and merges normally once the reviewer looks at ITS commit: "needs its own Gate 1 run and a
    # reviewer PASS of its own before it can merge."
    run_until(world, lambda w: w.all_merge_cards_done())
    assert git(world, "show", "integration:a.py") == A_PY.strip()  # the original good content, not the stray line
    fix_branch_tip = git(world, "rev-parse", "swarm/T1-fix1")  # the fix card's own branch (row["fix_cards"] + 1 = 1)
    assert fix_branch_tip != sneaked["extra_commit"]  # the tainted commit itself was never reviewed or merged

    # The verdict names the REVIEWED commit (the fix branch's own tip, before the squash that landed it on
    # integration makes a new commit object with the same tree, exactly as the moved cores test above already
    # established for the ordinary case: "the approval that counted names the corrected commit").
    verdicts = world.conn.execute(
        "SELECT commit_sha FROM review_verdicts WHERE task_key = 'T1'").fetchall()
    assert [v["commit_sha"] for v in verdicts] == [fix_branch_tip]  # exactly the fix card's own fresh PASS

    fix_cards_spent = world.conn.execute(
        "SELECT fix_cards FROM plan_tasks WHERE project = ? AND task_key = 'T1'", (world.plan.project,),
    ).fetchone()["fix_cards"]
    assert fix_cards_spent == 1  # the recovery was clean: exactly one fix card, not repeated


# ---------------------------------------------------------------------------------------------
# New: "Repeat the violation until the lineage budget is reached: the task must escalate instead of looping."
# ---------------------------------------------------------------------------------------------


class _CountedWrongCoder:
    """Registered directly (never through fw.sequence, which repeats its LAST worker forever once its list is
    exhausted): each dispatch writes a DIFFERENT still-wrong file, naming its own attempt number, because a card
    the reviewer sends back keeps its SAME worktree and branch across review rounds (fakes/board.py:
    "an existing checkout of this repository is reused"; a fresh worktree is only cut for a fix card or a
    fresh capability-attempt, neither of which this scenario ever reaches), so writing byte-identical content a
    second time would leave the worktree clean and fakes.worker.Commit would refuse it ("nothing to commit").
    The bug stays the same violation (still returns x - y) on every attempt; only the trailing comment changes."""

    def __init__(self) -> None:
        self.attempt = 0

    def __call__(self, fake, card, run, workspace_path) -> None:
        self.attempt += 1
        text = f"def add(x, y):\n    return x - y  # attempt {self.attempt}, still wrong\n"
        worker = fw.wrong_coder({"a.py": text}, f"add a.py (attempt {self.attempt})")
        worker(fake, card, run, workspace_path)


def test_22_6_repeated_violations_escalate_through_replan_then_block_for_user_instead_of_looping_forever(
    world_factory, one_task_plan, run_until, git,
):
    """Blueprint 22.6 (p410): "Repeat the violation until the lineage budget is reached: the task must escalate
    instead of looping." Section 9.3's table and ASES-REC-02 (section 19.3) give the exact two-stage shape:
    "Review rounds per plan task 3: Escalate to the Lead, then to the user" -- recovery.escalation's own
    docstring: "a replan while the task has not been re-planned yet ..., a block_for_user question after that."
    review_rounds_per_task is 3 in this world's budgets (tests/acceptance/conftest.py's BUDGETS, the same
    default config/swarm.yaml ships), so the SAME acceptance violation, sent back over and over without ever
    being corrected, must stop the automatic review loop for good: a re-plan question the first time the budget
    is spent, then -- if the violation continues even after that one re-plan -- a block-for-user question
    instead of a second re-plan, never a fourth, fifth, unbounded round with no end in sight."""
    world = world_factory(plan_raw=one_task_plan)
    fake = world.fake
    fake.register_worker("coder-1", _CountedWrongCoder())
    fake.register_worker("reviewer", fw.reviewer_changes(["add(x, y) must return x + y, not x - y"]))
    t1 = world.create_cards()["T1"]
    work = t1.work_card_id

    def _replan_requested(w) -> bool:
        return w.conn.execute(
            "SELECT 1 FROM events WHERE kind = 'replan_requested' AND json_extract(payload, '$.task_key') = 'T1'"
        ).fetchone() is not None

    run_until(world, _replan_requested, max_passes=60)

    lineage = recovery.load_lineage(world.conn, world.plan.project, "T1")
    assert (lineage.review_rounds, lineage.replans, lineage.fix_cards) == (3, 1, 0)
    card = world.card(work)
    assert card["status"] == "blocked"  # escalated: not redispatched a fourth time
    question = questions.open_question(card)
    assert question is not None
    assert "review-round budget" in question.reason and "may re-plan the task once" in question.reason
    assert git(world, "rev-parse", "integration") == world.plan_sha  # nothing from this task ever merged
    assert world.conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'replan_requested'").fetchone()[0] == 1

    # The person answers (the Lead re-plan itself is not automated, per controller._request_replan's own
    # docstring): the card resumes, the SAME violation continues, and a fourth review round is spent.
    answered = questions.answer_question(world.board, work, "Proceed the same way; do not change the plan.",
                                          conn=world.conn)
    assert answered.question == question.reason
    assert fake.card(work)["status"] == "ready"

    def _lineage_escalated(w) -> bool:
        return w.conn.execute(
            "SELECT 1 FROM events WHERE kind = 'lineage_escalated' AND json_extract(payload, '$.task_key') = 'T1'"
        ).fetchone() is not None

    run_until(world, _lineage_escalated, max_passes=60)

    lineage2 = recovery.load_lineage(world.conn, world.plan.project, "T1")
    assert (lineage2.review_rounds, lineage2.replans) == (4, 1)  # one more round, and STILL only the one re-plan
    card2 = world.card(work)
    # questions.ask_user's own documented rule: "a second block of the same kind after an unblock sends the
    # card to triage" (an unblock-loop safety net, not a bug in this test): the first escalation blocked the
    # card, the person answered it (unblocking it), and this second escalation is another needs_input block on
    # that same card, so Hermes routes it to triage instead of blocked. Either way the loop stopped: the card is
    # NOT back in "ready" for a fifth coder dispatch, and _QUESTION_STATUSES covers triage too, so
    # open_question still finds it.
    assert card2["status"] == "triage"
    question2 = questions.open_question(card2)
    assert question2 is not None and question2.source == "block_loop"
    assert "review-round budget" in question2.reason
    assert "spent again" in question2.reason and "after its one re-plan" in question2.reason
    assert git(world, "rev-parse", "integration") == world.plan_sha  # still nothing merged: escalated, never looped

    # Exactly one of each escalation event ever: the loop stopped for good, it did not re-escalate every pass.
    assert world.conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'replan_requested'").fetchone()[0] == 1
    assert world.conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'lineage_escalated'").fetchone()[0] == 1
    assert fake.card(t1.merge_card_id)["status"] == "blocked"  # never eligible to merge: the work card never passed
