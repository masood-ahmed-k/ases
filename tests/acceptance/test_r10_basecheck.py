"""Round 10, package BASECHECK: the base-commit check, end to end through a real controller pass on FakeHermes
(blueprint p169's second sentence, ASES-GIT-01, ASES-GIT-16: "Phase 3 MUST verify the actual base commit before a
worker starts").

The scenario the unit tests (test_guards.py, test_mergeq.py, test_controller_loop.py) cannot exercise on their
own: a work card's branch planted from a commit ASES never wrote -- the signature Current Hermes's remote-tip
worktree_sync default would leave behind, if this Hermes version honoured it (see guards.check_card_base's
module-level comment: it does not, it always branches from the primary checkout's HEAD, which is why the check
exists at all) -- picked up by a REAL dispatch through FakeHermes (fakes/board.py's own real git worktree add),
detected by controller.process_card_base_checks on the very pass it starts running, and blocked through the real
question path (questions.ask_user): never merged, never a silent requeue.
"""
from __future__ import annotations

import json

from ases import questions as questions_mod
from ases.fakes import worker as fw


def _kinds(conn):
    return [row["kind"] for row in conn.execute("SELECT kind FROM events ORDER BY id")]


def _payloads(conn, kind):
    return [json.loads(row["payload"]) for row in conn.execute(
        "SELECT payload FROM events WHERE kind = ? ORDER BY id", (kind,))]


def test_a_branch_planted_from_a_commit_ases_never_wrote_is_detected_blocked_and_never_merged(
    world_factory, one_task_plan, create_cards,
):
    w = world_factory(plan_raw=one_task_plan)

    # Plant swarm/T1-coder ahead of dispatch, from a commit ASES itself never wrote or adopted: exactly the base
    # a remote-tip worktree_sync would leave, and exactly what guards.written_heads(conn, "acceptance") will NOT
    # contain once the world's own adopt_current_head (make_world, conftest.py) runs.
    w.git("checkout", "-q", "-b", "planted-tmp", "integration")
    (w.repo / "planted.txt").write_text("a commit ASES never wrote\n", encoding="utf-8")
    w.git("add", "-A")
    w.git("commit", "-q", "-m", "a commit ASES never wrote")
    planted = w.git("rev-parse", "HEAD")
    w.git("checkout", "-q", "integration")
    w.git("branch", "swarm/T1-coder", planted)
    w.git("branch", "-D", "planted-tmp")
    integration_before = w.git("rev-parse", "integration")

    create_cards(w)
    # A worker that keeps the card `running` for at least one pass (the real gateway dispatches asynchronously;
    # FakeHermes normally runs a scripted worker to completion inside kanban_dispatch itself, which would leave
    # nothing for process_card_base_checks, called right after dispatch in the same pass, to ever see running).
    w.fake.register_worker("coder-1", fw.slow_coder({"a.py": "def add(x, y):\n    return x + y\n"}, seconds=600))

    summary = w.one_pass()

    card = w.card(w.work_card_id("T1"))
    assert card["status"] == "blocked"
    question = questions_mod.open_question(card)
    assert question is not None
    assert "ASES-GIT-01" in question.reason and "swarm/T1-coder" in question.reason

    (violation,) = _payloads(w.conn, "card_base_violation")
    assert violation["task_key"] == "T1" and violation["branch"] == "swarm/T1-coder" and violation["base"] == planted
    assert "card_base_verified" not in _kinds(w.conn)
    assert any("ASES-GIT-01" in warning for warning in summary["warnings"])

    assert w.all_merge_cards_done() is False
    assert w.git("rev-parse", "integration") == integration_before  # nothing landed on the integration branch
