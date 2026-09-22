"""The acceptance rig's own tests (ASES-TST-01, ASES-TST-02): the fake Hermes board, the scripted worker and personas, and
the fake provider additions.

The board is only worth building if it follows the real Hermes 0.21.3 rules, so every rule is tested in both directions
(what is allowed, and what is refused) and the expectations quote what hermes_cli/kanban_db.py, kanban_db_dispatch.py and
kanban.py really do. A rule that changes in Hermes will change here on purpose; a public wrapper added to hermes.py fails
test_every_public_hermes_function_has_a_fake_with_the_same_signature until the fake has it too.
"""
import contextlib
import http.client
import inspect
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

from ases import db, gates, guards, hermes, integrity, review, tamper
from ases.fakes import provider as fp
from ases.fakes import worker as fw
from ases.fakes.board import (
    FAKE_PID_BASE, TASK_FIELDS, AgentToolError, FakeHermes,
)
from ases.hermes import HermesCommandError

BOARD = "b"


def new_fake(**kwargs) -> FakeHermes:
    return FakeHermes(board=BOARD, now=1_000_000, **kwargs)


def create(fake, title="T1: work", **kwargs) -> dict:
    return fake.kanban_create(BOARD, title, **kwargs)


def kinds(fake, card_id) -> list[str]:
    return [e["kind"] for e in fake.events(card_id)]


def hang(fake, card, run, workspace):
    fake.agent_hang(card["id"], run_id=run["id"])


def lazy(fake, card, run, workspace):
    """A worker that exits cleanly without a terminal kanban call."""


def finish(fake, card, run, workspace):
    fake.agent_complete(card["id"], summary="done", run_id=run["id"])


def crash(fake, card, run, workspace):
    fake.agent_fail(card["id"], "pid 7 exited with code 1", "crashed", run_id=run["id"])


def hand_off(fake, card, run, workspace):
    fake.agent_request_review(card["id"], summary="ready", reviewer="reviewer", run_id=run["id"])


def refuses(fake_call, *fragments, code=1):
    """Call `fake_call()` and return the HermesCommandError it raises, checking the exit code and that every fragment is in
    the command's output. Asserting on the output text is asserting on what the real CLI prints."""
    with pytest.raises(HermesCommandError) as excinfo:
        fake_call()
    assert excinfo.value.returncode == code
    for fragment in fragments:
        assert fragment in excinfo.value.output, excinfo.value.output
    return excinfo.value


def git(cwd, *args) -> str:
    result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, f"git {' '.join(args)}: {result.stderr}"
    return result.stdout.strip()


def make_repo(tmp_path, files=None):
    """A primary checkout on `integration` with one commit that holds `files`."""
    repo = tmp_path / "primary"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "integration")
    for key, value in (("user.name", "t"), ("user.email", "t@example.invalid"), ("commit.gpgsign", "false"),
                       ("core.autocrlf", "false")):
        git(repo, "config", key, value)
    for name, text in (files or {"README.md": "hi\n"}).items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


# ---------------------------------------------------------------------------------------------
# Creation: idempotent by key, todo versus ready, blocked at birth
# ---------------------------------------------------------------------------------------------


def test_create_returns_the_flat_task_dict_hermes_prints():
    fake = new_fake()

    card = create(fake, "  T1: scaffold  ", assignee="Coder-1", body="Role: coder", workspace="worktree",
                  branch="swarm/T1-coder", project="p_1", idempotency_key="k1", max_runtime="45m", max_retries=3)

    assert list(card) == list(TASK_FIELDS)  # exactly the fields of `kanban show --json`, in order
    assert card["title"] == "T1: scaffold" and card["assignee"] == "coder-1"  # stripped; profile names are lower case
    assert (card["status"], card["workspace_kind"], card["branch_name"]) == ("ready", "worktree", "swarm/T1-coder")
    assert (card["project_id"], card["max_retries"], card["skills"], card["created_by"]) == ("p_1", 3, [], "default")
    assert (card["created_at"], card["started_at"], card["workspace_path"]) == (1_000_000, None, None)
    shown = fake.kanban_show(BOARD, card["id"])
    assert set(shown) == set(TASK_FIELDS) | {"_children", "_parents", "_runs", "_events", "_comments", "_latest_summary"}


def test_ids_look_like_hermes_ids_and_repeat_across_instances():
    first, second = new_fake(), new_fake()

    ids = [create(first, f"card {n}")["id"] for n in range(3)]

    assert ids == [create(second, f"card {n}")["id"] for n in range(3)]
    assert all(card_id.startswith("t_") and len(card_id) == 10 for card_id in ids)
    assert len(set(ids)) == 3


def test_create_is_idempotent_by_key_and_changes_nothing():
    fake = new_fake()
    first = create(fake, "T1: work", idempotency_key="ases-work-p-T1")
    before = fake.snapshot()

    again = create(fake, "T1: work but retitled", assignee="someone", idempotency_key="ases-work-p-T1")

    assert again == first
    assert fake.snapshot() == before  # no card, no event, no id consumed
    assert create(fake, "T2: work", idempotency_key="other")["id"] != first["id"]


def test_an_archived_card_no_longer_answers_its_idempotency_key():
    fake = new_fake()
    first = create(fake, "T1: work", idempotency_key="k")
    fake.kanban_archive(BOARD, [first["id"]])

    assert create(fake, "T1: work", idempotency_key="k")["id"] != first["id"]


def test_a_card_with_an_open_parent_is_todo_and_says_why_and_one_without_is_ready():
    fake = new_fake()
    parent = create(fake, "parent")
    child = create(fake, "child", parent=[parent["id"]])

    assert (parent["status"], child["status"]) == ("ready", "todo")
    assert kinds(fake, child["id"]) == ["created", "dependency_wait"]
    assert fake.events(child["id"], "dependency_wait")[0]["payload"] == {
        "reason": "parent_not_done", "parent": parent["id"]}
    assert fake.card(parent["id"])["_children"] == [child["id"]] and fake.card(child["id"])["_parents"] == [parent["id"]]


def test_a_card_under_a_done_parent_is_ready_and_under_an_archived_parent_is_todo_until_the_next_list():
    fake = new_fake()
    done = create(fake, "done parent")
    fake.kanban_complete(BOARD, done["id"], result="ok")
    gone = create(fake, "archived parent")
    fake.kanban_archive(BOARD, [gone["id"]])

    under_done = create(fake, "under done", parent=[done["id"]])
    under_archived = create(fake, "under archived", parent=[gone["id"]])

    assert under_done["status"] == "ready"
    assert under_archived["status"] == "todo"  # creation only counts `done`; recompute_ready also accepts archived
    assert kinds(fake, under_archived["id"]) == ["created"]  # no parent is still gating, so no dependency_wait
    fake.kanban_list(BOARD)
    assert fake.card(under_archived["id"])["status"] == "ready"
    assert kinds(fake, under_archived["id"]) == ["created", "promoted"]


def test_create_refuses_what_the_cli_refuses():
    fake = new_fake()
    parent = create(fake, "parent")

    refuses(lambda: create(fake, "   "), "title is required")
    refuses(lambda: create(fake, "x", parent=["t_nope"]), "unknown parent task(s): t_nope")
    refuses(lambda: create(fake, "x", initial_status="done"), "initial_status must be one of")
    refuses(lambda: create(fake, "x", branch="swarm/x"), "--branch is only valid with --workspace worktree", code=2)
    refuses(lambda: create(fake, "x", workspace="worktree", branch="a b"), "must not contain whitespace", code=2)
    refuses(lambda: create(fake, "x", max_retries=0), "--max-retries must be >= 1", code=2)
    refuses(lambda: create(fake, "x", max_runtime="soon"), "malformed duration", code=2)
    refuses(lambda: create(fake, "x", workspace="floppy"), "unknown --workspace value", code=2)
    assert [c["id"] for c in fake.cards()] == [parent["id"]]  # nothing was created by a refused call


def test_a_card_created_blocked_carries_a_blocked_event_and_its_block_is_sticky():
    """kanban_db.create_task writes {"reason": "initial_status", ...} for a blocked-at-birth card, and recompute_ready never
    promotes a card whose newest block event is `blocked`: every merge card stays blocked until the controller completes it."""
    fake = new_fake()
    work = create(fake, "T1: work", assignee="c")
    merge = create(fake, "T1: merge", parent=[work["id"]], initial_status="blocked")

    assert (merge["status"], merge["assignee"]) == ("blocked", None)
    assert kinds(fake, merge["id"]) == ["created", "blocked"]
    assert fake.events(merge["id"], "blocked")[0]["payload"] == {
        "reason": "initial_status", "status": "blocked", "actor": "default"}

    fake.kanban_complete(BOARD, work["id"], result="ok")
    fake.kanban_list(BOARD)  # runs recompute_ready, as the CLI does
    assert fake.card(merge["id"])["status"] == "blocked"


def test_without_the_initial_block_event_a_merge_card_is_promoted_when_its_parent_is_done():
    """The other reading of the facts (docs/work-orders/r2_rules.md: "no `blocked` event"): the block is then not sticky, so
    recompute_ready lifts the card as soon as its parents are done. `initial_block_event=False` reproduces it."""
    fake = new_fake()
    fake.initial_block_event = False
    work = create(fake, "T1: work")
    merge = create(fake, "T1: merge", parent=[work["id"]], initial_status="blocked")
    assert kinds(fake, merge["id"]) == ["created"]

    fake.kanban_complete(BOARD, work["id"], result="ok")

    assert fake.card(merge["id"])["status"] == "ready"
    assert kinds(fake, merge["id"])[-1] == "promoted"


# ---------------------------------------------------------------------------------------------
# Links and promotion
# ---------------------------------------------------------------------------------------------


def test_linking_an_open_parent_demotes_a_ready_child_and_leaves_a_blocked_one_alone():
    fake = new_fake()
    fix = create(fake, "T1: fix (round 1)")
    ready_child = create(fake, "ready child")
    blocked_merge = create(fake, "T1: merge", initial_status="blocked")

    fake.kanban_link(BOARD, fix["id"], ready_child["id"])
    fake.kanban_link(BOARD, fix["id"], blocked_merge["id"])

    assert fake.card(ready_child["id"])["status"] == "todo"
    assert fake.events(ready_child["id"], "dependency_wait")[0]["payload"] == {
        "reason": "parent_not_done", "demoted": True, "parent": fix["id"]}
    assert fake.card(blocked_merge["id"])["status"] == "blocked"
    assert kinds(fake, blocked_merge["id"])[-1] == "linked"
    assert fake.card(blocked_merge["id"])["_parents"] == [fix["id"]]


def test_link_refuses_unknown_cards_self_links_and_cycles_and_records_linked_every_time():
    fake = new_fake()
    a, b = create(fake, "a"), create(fake, "b")
    fake.kanban_link(BOARD, a["id"], b["id"])

    refuses(lambda: fake.kanban_link(BOARD, a["id"], "t_nope"), "unknown task(s): t_nope")
    refuses(lambda: fake.kanban_link(BOARD, a["id"], a["id"]), "cannot depend on itself")
    refuses(lambda: fake.kanban_link(BOARD, b["id"], a["id"]), "would create a cycle")
    fake.kanban_link(BOARD, a["id"], b["id"])  # again: the link exists, but the event is written anyway
    assert kinds(fake, b["id"]).count("linked") == 2 and fake.card(b["id"])["_parents"] == [a["id"]]


def test_completing_a_parent_promotes_its_children_and_a_list_promotes_like_the_cli():
    fake = new_fake()
    parent = create(fake, "parent")
    child = create(fake, "child", parent=[parent["id"]])
    assert fake.card(child["id"])["status"] == "todo"

    fake.kanban_complete(BOARD, parent["id"], result="ok")

    assert fake.card(child["id"])["status"] == "ready"
    assert kinds(fake, child["id"])[-1] == "promoted"


def test_reading_a_card_never_promotes_but_listing_does():
    fake = new_fake()
    gone = create(fake, "archived parent")
    fake.kanban_archive(BOARD, [gone["id"]])
    late = create(fake, "late", parent=[gone["id"]])  # todo: creation only counts `done` parents, nothing recomputed

    assert fake.kanban_show(BOARD, late["id"])["status"] == "todo"
    assert fake.card(late["id"])["status"] == "todo"
    assert [c["id"] for c in fake.kanban_list(BOARD, status="ready")] == [late["id"]]  # list ran recompute_ready first
    assert fake.card(late["id"])["status"] == "ready"


def test_archiving_a_parent_promotes_its_children_at_once():
    fake = new_fake()
    parent = create(fake, "parent")
    child = create(fake, "child", parent=[parent["id"]])
    assert fake.card(child["id"])["status"] == "todo"

    fake.kanban_archive(BOARD, [parent["id"]])

    assert fake.card(child["id"])["status"] == "ready"  # an archived parent counts as satisfied


def test_list_hides_archived_cards_unless_asked_and_filters_by_status_and_assignee():
    fake = new_fake()
    a = create(fake, "a", assignee="Coder-1")
    b = create(fake, "b", assignee="reviewer")
    c = create(fake, "c", assignee="coder-1")
    fake.kanban_archive(BOARD, [c["id"]])

    assert [x["id"] for x in fake.kanban_list(BOARD)] == [a["id"], b["id"]]
    assert [x["id"] for x in fake.kanban_list(BOARD, assignee="CODER-1")] == [a["id"]]
    assert [x["id"] for x in fake.kanban_list(BOARD, status="archived")] == [c["id"]]
    assert fake.kanban_list(BOARD, status="running") == []
    refuses(lambda: fake.kanban_list(BOARD, status="sleeping"), "status must be one of")
    assert all(list(x) == list(TASK_FIELDS) for x in fake.kanban_list(BOARD))  # a list entry is the flat dict, nothing more


# ---------------------------------------------------------------------------------------------
# Blocks: the real rules ASES had wrong
# ---------------------------------------------------------------------------------------------


def test_block_comments_first_then_blocks_and_writes_the_blocked_event_with_reason_and_kind():
    fake = new_fake()
    card = create(fake, "T1: work", assignee="c")

    fake.kanban_block(BOARD, card["id"], "which database?", kind="needs_input")

    shown = fake.card(card["id"])
    assert shown["status"] == "blocked"
    assert [(c["author"], c["body"]) for c in shown["_comments"]] == [("default", "BLOCKED: which database?")]
    (event,) = [e for e in shown["_events"] if e["kind"] == "blocked"]
    assert event["payload"] == {"reason": "which database?", "kind": "needs_input", "recurrences": 1,
                                "source_status": "ready"}
    assert [(r["profile"], r["outcome"], r["summary"]) for r in shown["_runs"]] == [("c", "blocked", "which database?")]


def test_blocking_a_card_that_is_already_blocked_raises_after_leaving_its_comment():
    """Every merge card is created blocked, and block_task only accepts `running` or `ready`: the CLI has already added the
    BLOCKED comment when it fails and exits 1."""
    fake = new_fake()
    merge = create(fake, "T1: merge", initial_status="blocked")

    error = refuses(lambda: fake.kanban_block(BOARD, merge["id"], "fix budget exhausted", kind="needs_input"),
                    "cannot block")

    assert str(error).startswith(f"hermes kanban --board {BOARD} block --kind needs_input {merge['id']} --")
    shown = fake.card(merge["id"])
    assert shown["status"] == "blocked"
    assert [c["body"] for c in shown["_comments"]] == ["BLOCKED: fix budget exhausted"]
    assert [e["payload"]["reason"] for e in shown["_events"] if e["kind"] == "blocked"] == ["initial_status"]


@pytest.mark.parametrize("status", ["todo", "review", "done", "scheduled", "archived"])
def test_blocking_a_card_in_any_other_status_is_refused_after_the_comment(status):
    fake = new_fake()
    fake.register_worker("c", hand_off)
    fake.register_worker("reviewer", finish)
    parent = create(fake, "parent")
    card = create(fake, "card", assignee="c", parent=[parent["id"]] if status == "todo" else None)
    if status == "review":
        fake.kanban_dispatch(BOARD, max_spawns=None)
    elif status == "done":
        fake.kanban_complete(BOARD, card["id"], result="x")
    elif status == "scheduled":
        fake.kanban_schedule(BOARD, card["id"], "later")
    elif status == "archived":
        fake.kanban_archive(BOARD, [card["id"]])
    assert fake.card(card["id"])["status"] == status

    refuses(lambda: fake.kanban_block(BOARD, card["id"], "please"), "cannot block")

    assert fake.card(card["id"])["status"] == status
    assert fake.card(card["id"])["_comments"][-1]["body"] == "BLOCKED: please"


def test_a_second_block_of_the_same_kind_after_an_unblock_goes_to_triage_with_block_loop_detected():
    fake = new_fake()
    card = create(fake, "T1: work", assignee="c")
    fake.kanban_block(BOARD, card["id"], "first question", kind="needs_input")
    fake.kanban_unblock(BOARD, card["id"], "first answer")
    assert fake.card(card["id"])["status"] == "ready"

    fake.kanban_block(BOARD, card["id"], "second question", kind="needs_input")

    shown = fake.card(card["id"])
    assert shown["status"] == "triage"
    (loop,) = [e for e in shown["_events"] if e["kind"] == "block_loop_detected"]
    assert loop["payload"] == {"reason": "second question", "kind": "needs_input", "recurrences": 2,
                               "source_status": "ready", "limit": 2}
    assert [e["payload"]["reason"] for e in shown["_events"] if e["kind"] == "blocked"] == ["first question"]
    refuses(lambda: fake.kanban_unblock(BOARD, card["id"], "answer"), "cannot unblock", "not blocked/scheduled")


def test_a_generic_block_loops_too_because_none_equals_none():
    fake = new_fake()
    card = create(fake, "T1: work", assignee="c")
    fake.kanban_block(BOARD, card["id"], "stuck")
    fake.kanban_unblock(BOARD, card["id"])

    fake.kanban_block(BOARD, card["id"], "stuck again")

    assert fake.card(card["id"])["status"] == "triage"


def test_a_block_of_a_different_kind_starts_the_count_again_and_a_dependency_block_waits_in_todo():
    fake = new_fake()
    card = create(fake, "T1: work", assignee="c")
    fake.kanban_block(BOARD, card["id"], "need input", kind="needs_input")
    fake.kanban_unblock(BOARD, card["id"])

    fake.kanban_block(BOARD, card["id"], "provider is down", kind="capability")
    assert fake.card(card["id"])["status"] == "blocked"  # a different kind: recurrences 1, not a loop
    fake.kanban_unblock(BOARD, card["id"])
    fake.kanban_block(BOARD, card["id"], "waiting for the schema", kind="dependency")

    shown = fake.card(card["id"])
    assert shown["status"] == "todo"  # dependency never sits in the human `blocked` bucket
    assert shown["_events"][-1]["kind"] == "dependency_wait"
    assert shown["_events"][-1]["payload"] == {"reason": "waiting for the schema", "kind": "dependency",
                                               "source_status": "ready"}


def test_block_refuses_an_unknown_kind_and_an_unknown_card():
    fake = new_fake()
    card = create(fake, "T1: work", assignee="c")

    refuses(lambda: fake.kanban_block(BOARD, card["id"], "x", kind="whim"), "invalid choice", code=2)
    refuses(lambda: fake.kanban_block(BOARD, "t_nope", "x"), "unknown task t_nope")
    assert fake.card(card["id"])["status"] == "ready"


def test_a_worker_block_writes_the_blocked_event_but_no_blocked_comment():
    fake = new_fake()
    fake.register_worker("c", lambda f, card, run, ws: f.agent_block(card["id"], "which database?", "needs_input", run_id=run["id"]))
    card = create(fake, "T1: work", assignee="c")

    fake.kanban_dispatch(BOARD)

    shown = fake.card(card["id"])
    assert shown["status"] == "blocked" and shown["_comments"] == []
    (run,) = shown["_runs"]
    assert (run["profile"], run["status"], run["outcome"], run["summary"]) == ("c", "blocked", "blocked", "which database?")
    assert run["ended_at"] == 1_000_000 and run["worker_pid"] == FAKE_PID_BASE + 1
    assert fake.live_workers() == []


# ---------------------------------------------------------------------------------------------
# Unblock, schedule, promote
# ---------------------------------------------------------------------------------------------


def test_unblock_resets_the_failure_counter_but_not_the_block_kind():
    fake = new_fake()
    fake.register_worker("c", crash)
    card = create(fake, "T1: work", assignee="c")
    fake.kanban_dispatch(BOARD)  # one crash
    assert fake.snapshot()["tasks"][card["id"]]["consecutive_failures"] == 1
    fake.kanban_block(BOARD, card["id"], "need input", kind="needs_input")

    fake.kanban_unblock(BOARD, card["id"], "here is the input")

    state = fake.snapshot()["tasks"][card["id"]]
    assert (state["consecutive_failures"], state["last_failure_error"]) == (0, None)
    assert (state["block_kind"], state["block_recurrences"]) == ("needs_input", 1)  # so the next same-kind block loops
    shown = fake.card(card["id"])
    assert shown["status"] == "ready"
    assert [c["body"] for c in shown["_comments"]] == ["BLOCKED: need input", "UNBLOCK: here is the input"]
    assert shown["_events"][-1] == {"kind": "unblocked", "payload": None, "created_at": 1_000_000, "run_id": None}


def test_unblock_without_a_reason_writes_no_comment_and_refuses_a_card_that_is_not_blocked():
    fake = new_fake()
    card = create(fake, "T1: work", assignee="c")
    fake.kanban_block(BOARD, card["id"], "q")

    fake.kanban_unblock(BOARD, card["id"])
    assert [c["body"] for c in fake.card(card["id"])["_comments"]] == ["BLOCKED: q"]

    refuses(lambda: fake.kanban_unblock(BOARD, card["id"], "and again"), "cannot unblock", "not blocked/scheduled")
    assert fake.card(card["id"])["_comments"][-1]["body"] == "UNBLOCK: and again"  # the comment is written first


def test_unblock_lands_in_todo_while_a_parent_is_unfinished():
    fake = new_fake()
    card = create(fake, "T1: work", assignee="c")
    fake.kanban_block(BOARD, card["id"], "q")
    fix = create(fake, "T1: fix (round 1)")
    fake.kanban_link(BOARD, fix["id"], card["id"])  # a blocked card is not demoted by a link

    fake.kanban_unblock(BOARD, card["id"], "answered")

    assert fake.card(card["id"])["status"] == "todo"
    assert fake.events(card["id"], "unblocked")[0]["payload"] == {"status": "todo", "resume_status": "ready"}


def test_unblock_returns_a_reviewer_blocked_card_to_review_not_to_its_implementer():
    fake = new_fake()
    fake.register_worker("c", hand_off)
    fake.register_worker("reviewer", lambda f, card, run, ws: f.agent_block(
        card["id"], "need a human", "needs_input", run_id=run["id"]))
    card = create(fake, "reviewed", assignee="c")
    fake.kanban_dispatch(BOARD)  # the coder hands off
    fake.kanban_dispatch(BOARD)  # the reviewer claims it from review and blocks it
    assert fake.card(card["id"])["status"] == "blocked"
    assert fake.events(card["id"], "blocked")[0]["payload"]["source_status"] == "review"

    fake.kanban_unblock(BOARD, card["id"], "go ahead")

    assert fake.card(card["id"])["status"] == "review"
    assert fake.events(card["id"], "unblocked")[0]["payload"] == {"status": "review", "resume_status": "review"}


def test_schedule_parks_a_ready_card_with_a_comment_a_run_and_an_event_and_unblock_returns_it():
    fake = new_fake()
    fake.register_worker("c", finish)
    card = create(fake, "T1: work", assignee="c")

    fake.kanban_schedule(BOARD, card["id"], "budget: needs 10 requests, 3 usable today")

    shown = fake.card(card["id"])
    assert shown["status"] == "scheduled"
    assert [(c["author"], c["body"]) for c in shown["_comments"]] == [
        ("default", "SCHEDULED: budget: needs 10 requests, 3 usable today")]
    assert [(r["outcome"], r["summary"]) for r in shown["_runs"]] == [
        ("scheduled", "budget: needs 10 requests, 3 usable today")]
    assert shown["_events"][-1]["kind"] == "scheduled" and shown["_events"][-1]["payload"] == {
        "reason": "budget: needs 10 requests, 3 usable today"}
    assert fake.kanban_dispatch(BOARD)["spawned"] == []  # not dispatchable while scheduled

    fake.kanban_unblock(BOARD, card["id"])

    assert fake.card(card["id"])["status"] == "ready"
    assert len(fake.kanban_dispatch(BOARD)["spawned"]) == 1


@pytest.mark.parametrize("status", ["review", "done", "scheduled", "triage", "archived"])
def test_schedule_only_works_from_todo_ready_running_and_blocked(status):
    fake = new_fake()
    fake.register_worker("c", hand_off)
    fake.register_worker("reviewer", finish)
    card = create(fake, "card", assignee="c")
    if status == "review":
        fake.kanban_dispatch(BOARD)
    elif status == "done":
        fake.kanban_complete(BOARD, card["id"], result="x")
    elif status == "scheduled":
        fake.kanban_schedule(BOARD, card["id"], "later")
    elif status == "triage":
        fake.kanban_block(BOARD, card["id"], "q")
        fake.kanban_unblock(BOARD, card["id"])
        fake.kanban_block(BOARD, card["id"], "q2")
    elif status == "archived":
        fake.kanban_archive(BOARD, [card["id"]])
    assert fake.card(card["id"])["status"] == status

    refuses(lambda: fake.kanban_schedule(BOARD, card["id"], "later still"), "cannot schedule")

    assert fake.card(card["id"])["status"] == status


def test_promote_works_only_from_todo_or_blocked_and_only_when_every_parent_is_done():
    fake = new_fake()
    fake.register_worker("c", crash)
    parent = create(fake, "parent")
    child = create(fake, "child", parent=[parent["id"]])
    refuses(lambda: fake.kanban_promote(BOARD, child["id"], "go"), "unsatisfied parent dependencies", parent["id"])

    running = create(fake, "running", assignee="c")
    fake.kanban_dispatch(BOARD)
    fake.kanban_dispatch(BOARD)  # two crashes: given up on, blocked with no `blocked` event
    assert fake.card(running["id"])["status"] == "blocked"
    fake.kanban_promote(BOARD, running["id"], "operator says retry")

    shown = fake.card(running["id"])
    assert shown["status"] == "ready"
    assert shown["_events"][-1]["kind"] == "promoted_manual"
    assert shown["_events"][-1]["payload"] == {"actor": "default", "reason": "operator says retry"}
    refuses(lambda: fake.kanban_promote(BOARD, running["id"]), "is 'ready'; promote only applies to 'todo' or 'blocked'")
    refuses(lambda: fake.kanban_promote(BOARD, "t_nope"), "not found")


# ---------------------------------------------------------------------------------------------
# Review: hand-off, requested changes, the controller's own send-back
# ---------------------------------------------------------------------------------------------


def test_request_changes_needs_an_active_review_run_and_reopen_review_does_not():
    """Checked against real Hermes 0.21.3 on 2026-09-19: on a card that merely sits in `review`, request-changes prints
    "task is not in an active review run" and exits 1, while reopen-review lands the card in `ready`."""
    fake = new_fake()
    fake.register_worker("coder-1", hand_off)
    fake.register_worker("reviewer", finish)
    card = create(fake, "T1: work", assignee="coder-1")
    fake.kanban_dispatch(BOARD)
    assert (fake.card(card["id"])["status"], fake.card(card["id"])["assignee"]) == ("review", "reviewer")

    refuses(lambda: fake.kanban_request_changes(BOARD, card["id"], "fix it"),
            "cannot request changes", "task is not in an active review run")
    fake.kanban_reopen_review(BOARD, card["id"], "Gate 1 failed on the controller's re-check")

    shown = fake.card(card["id"])
    assert (shown["status"], shown["assignee"]) == ("ready", "coder-1")  # back to its implementer
    assert [(c["author"], c["body"]) for c in shown["_comments"]] == [
        ("default", "CHANGES REQUESTED: Gate 1 failed on the controller's re-check")]
    assert [e["kind"] for e in shown["_events"]][-2:] == ["review_reopened", "commented"]  # the comment follows the send-back
    assert fake.events(card["id"], "review_reopened")[0]["payload"] == {"status": "ready", "implementer": "coder-1"}
    refuses(lambda: fake.kanban_reopen_review(BOARD, card["id"], "again"), "cannot reopen", "not in review?")


def test_request_changes_from_an_active_review_run_returns_the_card_to_its_implementer():
    fake = new_fake()
    fake.register_worker("coder-1", hand_off)
    fake.register_worker("reviewer", hang)
    card = create(fake, "T1: work", assignee="coder-1")
    fake.kanban_dispatch(BOARD)  # the coder hands off
    fake.kanban_dispatch(BOARD)  # the reviewer claims it from review and is now busy
    assert fake.card(card["id"])["status"] == "running"

    fake.kanban_request_changes(BOARD, card["id"], "  add a test  ")

    shown = fake.card(card["id"])
    assert (shown["status"], shown["assignee"]) == ("ready", "coder-1")
    (run,) = [r for r in shown["_runs"] if r["outcome"] == "changes_requested"]
    assert (run["profile"], run["status"], run["summary"], run["metadata"]) == ("reviewer", "ready", "add a test", None)
    (event,) = [e for e in shown["_events"] if e["kind"] == "changes_requested"]
    assert event["payload"] == {"reason": "add a test", "implementer": "coder-1", "reviewer": "reviewer", "status": "ready"}
    assert event["run_id"] == run["id"]
    refuses(lambda: fake.kanban_request_changes(BOARD, card["id"], "and more"), "not in an active review run")
    refuses(lambda: fake.kanban_request_changes(BOARD, "t_nope", "x"), "task not found")


def test_a_review_hand_off_names_implementer_and_reviewer_and_a_re_review_defaults_to_the_same_reviewer():
    fake = new_fake()
    fake.register_worker("coder-1", lambda f, card, run, ws: f.agent_request_review(
        card["id"], summary="one line\nsecond line", metadata={"commit_sha": "abc1234"}, reviewer=None if run["id"] > 2 else "reviewer",
        run_id=run["id"]))
    fake.register_worker("reviewer", lambda f, card, run, ws: f.agent_request_changes(card["id"], "redo it", run_id=run["id"]))
    card = create(fake, "T1: work", assignee="coder-1")

    fake.kanban_dispatch(BOARD)
    first = fake.events(card["id"], "review_requested")[0]["payload"]
    fake.kanban_dispatch(BOARD)  # the reviewer sends it back
    fake.kanban_dispatch(BOARD)  # the coder hands off again, naming no reviewer this time

    assert first == {"summary": "one line", "implementer": "coder-1", "reviewer": "reviewer"}
    assert fake.events(card["id"], "review_requested")[1]["payload"]["reviewer"] == "reviewer"  # from changes_requested
    assert fake.card(card["id"])["assignee"] == "reviewer" and fake.card(card["id"])["status"] == "review"
    run = fake.card(card["id"])["_runs"][0]
    assert (run["outcome"], run["status"], run["summary"]) == ("review_requested", "review", "one line\nsecond line")
    assert run["metadata"]["commit_sha"] == "abc1234"
    assert run["metadata"]["worker_session_id"] == FakeHermes.session_id_for(card["id"], run["id"])


def test_a_hand_off_needs_a_summary_and_an_installed_reviewer_profile():
    fake = new_fake()
    fake.register_worker("coder-1", hang)
    card = create(fake, "T1: work", assignee="coder-1")
    fake.kanban_dispatch(BOARD)
    run_id = fake.card(card["id"])["_runs"][0]["id"]

    with pytest.raises(AgentToolError, match="summary is required"):
        fake.agent_request_review(card["id"], summary="  ", run_id=run_id)
    with pytest.raises(AgentToolError, match="reviewer profile 'ghost' is not installed"):
        fake.agent_request_review(card["id"], summary="done", reviewer="ghost", run_id=run_id)
    with pytest.raises(AgentToolError, match="metadata must be an object"):
        fake.agent_request_review(card["id"], summary="done", metadata=["x"], run_id=run_id)
    assert fake.card(card["id"])["status"] == "running"


# ---------------------------------------------------------------------------------------------
# Completing, archiving, reclaiming, the model override, comments
# ---------------------------------------------------------------------------------------------


def test_completing_a_blocked_merge_card_synthesizes_a_run_without_a_profile_and_promotes_the_next_task():
    fake = new_fake()
    work = create(fake, "T1: work", assignee="c")
    merge = create(fake, "T1: merge", parent=[work["id"]], initial_status="blocked")
    next_work = create(fake, "T2: work", assignee="c", parent=[merge["id"]])

    refuses(lambda: fake.kanban_complete(BOARD, merge["id"], result="merged"), "cannot complete", "unknown id or terminal state")
    fake.kanban_complete(BOARD, work["id"], result="reviewed")
    fake.kanban_complete(BOARD, merge["id"], result="merged abc1234", metadata={"squash_commit": "abc1234"})

    shown = fake.card(merge["id"])
    assert (shown["status"], shown["result"], shown["completed_at"]) == ("done", "merged abc1234", 1_000_000)
    (run,) = shown["_runs"]
    assert (run["profile"], run["status"], run["outcome"], run["summary"], run["metadata"]) == (
        None, "completed", "completed", "merged abc1234", {"squash_commit": "abc1234"})
    assert (run["started_at"], run["ended_at"], run["worker_pid"]) == (1_000_000, 1_000_000, None)
    assert shown["_latest_summary"] == "merged abc1234"
    assert shown["_events"][-1] == {"kind": "completed", "run_id": run["id"], "created_at": 1_000_000,
                                    "payload": {"result_len": len("merged abc1234"), "summary": "merged abc1234"}}
    assert fake.card(next_work["id"])["status"] == "ready"


def test_a_no_op_merge_completion_keeps_its_metadata_and_completing_twice_is_refused():
    fake = new_fake()
    card = create(fake, "T1: merge", initial_status="blocked")

    fake.kanban_complete(BOARD, card["id"], result="no changes to merge (review-only task)",
                         metadata={"squash_commit": None, "no_op": True})

    assert fake.card(card["id"])["_runs"][0]["metadata"] == {"squash_commit": None, "no_op": True}
    refuses(lambda: fake.kanban_complete(BOARD, card["id"], result="again"), "unknown id or terminal state")


def test_the_controller_cannot_complete_a_card_that_a_live_worker_is_running():
    fake = new_fake()
    fake.register_worker("c", hang)
    card = create(fake, "T1: work", assignee="c")
    fake.kanban_dispatch(BOARD)

    refuses(lambda: fake.kanban_complete(BOARD, card["id"], result="x"), "a live worker is running it", "--force")

    assert fake.card(card["id"])["status"] == "running"
    fake.kanban_reclaim(BOARD, card["id"])
    fake.kanban_complete(BOARD, card["id"], result="x")  # the worker is gone: allowed
    assert fake.card(card["id"])["status"] == "done"


def test_archive_frees_children_ends_a_running_card_and_kills_its_worker():
    fake = new_fake()
    fake.register_worker("c", hang)
    running = create(fake, "running", assignee="c")
    child = create(fake, "child", parent=[running["id"]])
    fake.kanban_dispatch(BOARD)
    assert fake.live_workers()[0]["card_id"] == running["id"]

    fake.kanban_archive(BOARD, [running["id"]])

    shown = fake.card(running["id"])
    assert shown["status"] == "archived"
    (run,) = shown["_runs"]
    assert (run["outcome"], run["status"], run["summary"]) == ("reclaimed", "reclaimed", "task archived with run still active")
    assert kinds(fake, running["id"])[-2:] == ["archived", "archive_worker_termination"]
    assert fake.events(running["id"], "archive_worker_termination")[0]["payload"]["terminated"] is True
    assert fake.live_workers() == []
    assert fake.card(child["id"])["status"] == "ready"  # an archived parent no longer blocks


def test_archive_reports_every_card_it_could_not_archive_after_archiving_the_others():
    fake = new_fake()
    a, b = create(fake, "a"), create(fake, "b")
    fake.kanban_archive(BOARD, [a["id"]])

    error = refuses(lambda: fake.kanban_archive(BOARD, [a["id"], b["id"], "t_nope"]), f"cannot archive {a['id']}",
                    "cannot archive t_nope")

    assert f"Archived {b['id']}" in error.output and fake.card(b["id"])["status"] == "archived"
    fake.kanban_archive(BOARD, [])  # nothing to do, and no call to make


def test_reclaim_ends_the_run_resets_the_counter_and_returns_the_card_to_where_the_run_came_from():
    fake = new_fake()
    fake.register_worker("coder-1", hand_off)
    fake.register_worker("reviewer", hang)
    card = create(fake, "T1: work", assignee="coder-1")
    fake.kanban_dispatch(BOARD)
    fake.kanban_dispatch(BOARD)  # the reviewer is running it, claimed from review

    fake.kanban_reclaim(BOARD, card["id"], reason="swarm stop")

    shown = fake.card(card["id"])
    assert shown["status"] == "review"  # a reviewer run goes back to review, never to an implementation run
    run = shown["_runs"][-1]
    assert (run["outcome"], run["status"], run["error"]) == ("reclaimed", "reclaimed", "manual_reclaim: swarm stop")
    assert run["metadata"] == {"prev_pid": run["worker_pid"], "host_local": True, "termination_attempted": True,
                               "terminated": True, "sigkill": False}
    payload = fake.events(card["id"], "reclaimed")[0]["payload"]
    assert (payload["manual"], payload["reason"], payload["retry_status"]) == (True, "swarm stop", "review")
    assert fake.live_workers() == []
    refuses(lambda: fake.kanban_reclaim(BOARD, card["id"]), "cannot reclaim", "not running or unknown id")
    refuses(lambda: fake.kanban_reclaim(BOARD, "t_nope"), "not running or unknown id")


def test_set_model_pins_and_clears_a_cards_model_and_refuses_a_provider_without_a_model():
    fake = new_fake()
    card = create(fake, "T1: work", assignee="c")

    fake.kanban_set_model(BOARD, card["id"], "gpt-x", provider="openai")
    assert (fake.card(card["id"])["model_override"], fake.card(card["id"])["provider_override"]) == ("gpt-x", "openai")
    assert fake.events(card["id"], "model_override_set")[-1]["payload"] == {"model": "gpt-x", "provider": "openai"}
    fake.kanban_set_model(BOARD, card["id"], None, provider="openai")  # the wrapper sends `none`, and drops the provider

    assert (fake.card(card["id"])["model_override"], fake.card(card["id"])["provider_override"]) == (None, None)
    fake.kanban_archive(BOARD, [card["id"]])
    refuses(lambda: fake.kanban_set_model(BOARD, card["id"], "gpt-y"), "cannot set model override on archived task", code=2)
    refuses(lambda: fake.kanban_set_model(BOARD, "t_nope", "gpt-y"), "no such task")


def test_comments_are_signed_stripped_and_refused_when_blank_or_for_an_unknown_card():
    fake = new_fake()
    card = create(fake, "T1: work")

    fake.kanban_comment(BOARD, card["id"], "  first  ")
    fake.kanban_comment(BOARD, card["id"], "second\nline", author="ases")

    assert [(c["author"], c["body"]) for c in fake.card(card["id"])["_comments"]] == [
        ("default", "first"), ("ases", "second\nline")]
    assert fake.events(card["id"], "commented")[0]["payload"] == {"author": "default", "len": 5}  # the stripped body's length
    refuses(lambda: fake.kanban_comment(BOARD, card["id"], "   "), "comment body is required")
    refuses(lambda: fake.kanban_comment(BOARD, "t_nope", "x"), "unknown task t_nope")


# ---------------------------------------------------------------------------------------------
# The circuit breaker: consecutive failures, max_retries, gave_up
# ---------------------------------------------------------------------------------------------


def test_two_crashes_give_up_at_the_default_limit_with_a_gave_up_event_and_no_blocked_event():
    fake = new_fake()
    fake.register_worker("c", crash)
    card = create(fake, "T1: work", assignee="c")

    fake.kanban_dispatch(BOARD)
    shown = fake.card(card["id"])
    assert shown["status"] == "ready"  # the first failure only counts: Hermes retries
    (run,) = shown["_runs"]
    assert (run["status"], run["outcome"], run["error"]) == ("crashed", "crashed", "pid 7 exited with code 1")
    assert run["metadata"]["retry_status"] == "ready" and run["ended_at"] == 1_000_000
    assert fake.snapshot()["tasks"][card["id"]]["consecutive_failures"] == 1

    fake.kanban_dispatch(BOARD)
    shown = fake.card(card["id"])
    assert shown["status"] == "blocked"  # the second failure trips the breaker
    (gave_up,) = fake.events(card["id"], "gave_up")
    assert {k: gave_up["payload"][k] for k in ("failures", "effective_limit", "limit_source", "trigger_outcome",
                                               "retry_status")} == {
        "failures": 2, "effective_limit": 2, "limit_source": "dispatcher", "trigger_outcome": "crashed",
        "retry_status": "ready"}
    assert gave_up["payload"]["error"] == "pid 7 exited with code 1" and gave_up["run_id"] is None
    assert "blocked" not in kinds(fake, card["id"])  # a give-up writes NO `blocked` event: nothing asks a question
    assert shown["last_failure_error"] == "pid 7 exited with code 1"
    assert fake.kanban_dispatch(BOARD)["spawned"] == []  # and it is not retried again


@pytest.mark.parametrize("max_retries, crashes", [(1, 1), (3, 3)])
def test_max_retries_sets_the_failure_at_which_a_card_is_given_up(max_retries, crashes):
    """--max-retries N trips on the Nth failure (so 3 allows two retries)."""
    fake = new_fake()
    fake.register_worker("c", crash)
    card = create(fake, "T1: work", assignee="c", max_retries=max_retries)

    for _ in range(crashes - 1):
        fake.kanban_dispatch(BOARD)
        assert fake.card(card["id"])["status"] == "ready"
    fake.kanban_dispatch(BOARD)

    assert fake.card(card["id"])["status"] == "blocked"
    payload = fake.events(card["id"], "gave_up")[0]["payload"]
    assert (payload["failures"], payload["effective_limit"], payload["limit_source"]) == (crashes, max_retries, "task")


def test_a_given_up_card_is_not_promoted_by_a_list_and_an_unblock_gives_it_a_fresh_budget():
    fake = new_fake()
    fake.register_worker("c", crash)
    card = create(fake, "T1: work", assignee="c")
    fake.kanban_dispatch(BOARD)
    fake.kanban_dispatch(BOARD)
    assert fake.card(card["id"])["status"] == "blocked"

    fake.kanban_list(BOARD)  # recompute_ready skips a blocked card whose failures reached its limit
    assert fake.card(card["id"])["status"] == "blocked"
    fake.kanban_unblock(BOARD, card["id"], "provider key fixed")

    assert fake.card(card["id"])["status"] == "ready"
    assert fake.snapshot()["tasks"][card["id"]]["consecutive_failures"] == 0
    fake.kanban_dispatch(BOARD)  # the first failure of the new budget
    assert fake.card(card["id"])["status"] == "ready"


def test_a_failure_that_reads_like_a_quota_or_auth_wall_holds_the_card_out_of_dispatch():
    """Real Hermes's respawn guard: a `ready` card whose last failure looks like quota or auth is never respawned."""
    fake = new_fake()
    fake.register_worker("c", lambda f, card, run, ws: f.agent_fail(
        card["id"], "HTTP 403 forbidden: this premium model requires an active paid plan", "crashed", run_id=run["id"]))
    card = create(fake, "T1: work", assignee="c")
    fake.kanban_dispatch(BOARD)
    assert fake.card(card["id"])["status"] == "ready"

    held = fake.kanban_dispatch(BOARD)

    assert held["spawned"] == [] and held["respawn_guarded"] == [{"task_id": card["id"], "reason": "blocker_auth"}]
    assert fake.card(card["id"])["status"] == "ready" and kinds(fake, card["id"])[-1] == "respawn_guarded"
    assert len(fake.runs(card["id"])) == 1


def test_a_rate_limited_run_is_requeued_without_a_failure_and_waits_out_the_cooldown():
    fake = new_fake()
    attempts = []

    def worker(f, card, run, workspace):
        attempts.append(run["id"])
        if len(attempts) == 1:
            f.agent_fail(card["id"], "HTTP 429: quota exceeded", "rate_limited", run_id=run["id"])
        else:
            finish(f, card, run, workspace)

    fake.register_worker("c", worker)
    card = create(fake, "T1: work", assignee="c")

    fake.kanban_dispatch(BOARD)

    shown = fake.card(card["id"])
    assert shown["status"] == "ready" and fake.snapshot()["tasks"][card["id"]]["consecutive_failures"] == 0
    (run,) = shown["_runs"]
    assert (run["status"], run["outcome"], run["error"]) == ("rate_limited", "rate_limited", "HTTP 429: quota exceeded")
    assert fake.events(card["id"], "rate_limited")[0]["payload"]["exit_code"] == 75
    held = fake.kanban_dispatch(BOARD)
    assert held["respawn_guarded"] == [{"task_id": card["id"], "reason": "rate_limit_cooldown"}]
    fake.tick(299)
    assert fake.kanban_dispatch(BOARD)["spawned"] == []
    fake.tick(1)
    assert len(fake.kanban_dispatch(BOARD)["spawned"]) == 1 and fake.card(card["id"])["status"] == "done"


def test_a_spawn_failure_books_spawn_failed_and_a_second_one_gives_up():
    fake = new_fake()
    fake.register_worker("c", finish)
    card = create(fake, "T1: work", assignee="c")
    fake.fail_spawn(card_id=card["id"], error="spawn failed: no such profile", times=2)

    first = fake.kanban_dispatch(BOARD)

    assert first["spawned"] == [] and fake.card(card["id"])["status"] == "ready"
    (run,) = fake.runs(card["id"])
    assert (run["status"], run["outcome"], run["error"], run["worker_pid"]) == (
        "spawn_failed", "spawn_failed", "spawn failed: no such profile", None)
    assert run["metadata"] == {"failures": 1, "retry_status": "ready"}
    assert fake.events(card["id"], "spawn_failed")[0]["payload"] == {
        "error": "spawn failed: no such profile", "failures": 1, "retry_status": "ready"}

    second = fake.kanban_dispatch(BOARD)

    assert second["auto_blocked"] == [card["id"]] and fake.card(card["id"])["status"] == "blocked"
    last = fake.runs(card["id"])[-1]
    assert (last["outcome"], last["status"]) == ("gave_up", "gave_up")
    assert last["metadata"] == {"failures": 2, "trigger_outcome": "spawn_failed", "effective_limit": 2,
                                "limit_source": "dispatcher", "retry_status": "ready"}
    assert fake.events(card["id"], "gave_up")[0]["run_id"] == last["id"]  # the spawn path has a run to name
    assert fake.kanban_dispatch(BOARD)["spawned"] == []


def test_a_hung_worker_is_timed_out_by_tick_and_retried():
    fake = new_fake()
    fake.register_worker("c", hang)
    card = create(fake, "T1: work", assignee="c", max_runtime="10m")
    fake.kanban_dispatch(BOARD)
    assert fake.card(card["id"])["status"] == "running"
    assert [w["orphan"] for w in fake.live_workers()] == [False]

    fake.tick(599)
    assert fake.card(card["id"])["status"] == "running"
    fake.tick(1)

    shown = fake.card(card["id"])
    assert shown["status"] == "ready" and fake.live_workers() == []
    (run,) = shown["_runs"]
    assert (run["status"], run["outcome"], run["error"]) == ("timed_out", "timed_out", "elapsed 600s > limit 600s")
    expected = {"pid": FAKE_PID_BASE + 1, "elapsed_seconds": 600, "limit_seconds": 600, "sigkill": False,
                "retry_status": "ready"}
    assert run["metadata"] == expected and fake.events(card["id"], "timed_out")[0]["payload"] == expected
    assert fake.snapshot()["tasks"][card["id"]]["consecutive_failures"] == 1


def test_two_timeouts_give_up_with_the_timeout_as_the_trigger():
    fake = new_fake()
    fake.register_worker("c", hang)
    card = create(fake, "T1: work", assignee="c", max_runtime="10m")
    fake.kanban_dispatch(BOARD)
    fake.tick(600)
    fake.kanban_dispatch(BOARD)  # respawned on a fresh run

    fake.tick(600)

    assert fake.card(card["id"])["status"] == "blocked"
    payload = fake.events(card["id"], "gave_up")[0]["payload"]
    assert (payload["failures"], payload["trigger_outcome"], payload["retry_status"]) == (2, "timed_out", "ready")
    assert [r["outcome"] for r in fake.runs(card["id"])] == ["timed_out", "timed_out"]
    assert fake.card(card["id"])["_runs"][1]["error"] == "elapsed 600s > limit 600s"  # measured from the SECOND run's start


def test_a_worker_that_exits_without_a_terminal_call_is_a_protocol_violation_with_its_own_streak():
    fake = new_fake()
    fake.register_worker("c", lazy)
    card = create(fake, "T1: work", assignee="c")
    fake.kanban_dispatch(BOARD)
    assert fake.card(card["id"])["status"] == "running" and fake.live_workers() == []  # dead process, card not yet reclaimed
    fake.tick(29)
    assert fake.card(card["id"])["status"] == "running"  # the crash grace: a worker younger than 30 s is not reclaimed
    fake.tick(1)

    shown = fake.card(card["id"])
    assert shown["status"] == "ready"
    (run,) = shown["_runs"]
    assert (run["status"], run["outcome"]) == ("crashed", "crashed")
    assert run["error"].startswith("worker exited cleanly (rc=0) without calling kanban_complete or kanban_block - protocol violation")
    assert run["metadata"]["protocol_violation"] is True and run["metadata"]["exit_code"] == 0
    assert kinds(fake, card["id"])[-1] == "protocol_violation"
    state = fake.snapshot()["tasks"][card["id"]]
    assert state["consecutive_failures"] == 0 and "protocol violation" in state["last_failure_error"]  # its own budget

    for _ in range(3):  # each dispatch books the previous violation and spawns the card again
        result = fake.kanban_dispatch(BOARD)

    assert result["auto_blocked"] == [card["id"]]  # the third violation trips the breaker ...
    payload = fake.events(card["id"], "gave_up")[0]["payload"]
    assert (payload["trigger_outcome"], payload["protocol_violations"], payload["protocol_violation_limit"]) == (
        "crashed", 3, 3)
    assert [r["outcome"] for r in fake.runs(card["id"])[:3]] == ["crashed", "crashed", "crashed"]
    # ... and Hermes 0.21.3 undoes it in the same tick: the trip leaves consecutive_failures at 1 (a violation does not
    # consume the unified counter), recompute_ready only holds a blocked card whose counter reached its limit, so the card
    # is promoted straight back to `ready` and spawned again. Ported from the source on purpose: a controller that waits
    # for a `blocked` card after a protocol-violation give-up would wait for ever.
    assert kinds(fake, card["id"])[-5:] == ["protocol_violation", "gave_up", "promoted", "claimed", "spawned"]
    assert fake.card(card["id"])["status"] == "running"


def test_a_killed_worker_is_booked_as_a_crash_once_the_grace_has_passed():
    fake = new_fake()
    fake.register_worker("c", hang)
    card = create(fake, "T1: work", assignee="c")
    fake.kanban_dispatch(BOARD)
    pid = FAKE_PID_BASE + 1
    fake.kill_worker(card["id"], exit_code=137)
    assert fake.card(card["id"])["status"] == "running"  # still marked running: Hermes has not looked yet

    fake.tick(30)

    shown = fake.card(card["id"])
    assert shown["status"] == "ready"
    (run,) = shown["_runs"]
    assert (run["outcome"], run["error"]) == ("crashed", f"pid {pid} exited with code 137")
    payload = fake.events(card["id"], "crashed")[0]["payload"]
    assert (payload["pid"], payload["exit_kind"], payload["exit_code"], payload["retry_status"]) == (
        pid, "nonzero_exit", 137, "ready")
    assert fake.snapshot()["tasks"][card["id"]]["consecutive_failures"] == 1


def test_a_worker_killed_by_a_signal_reads_killed_by_signal():
    fake = new_fake()
    fake.register_worker("c", hang)
    card = create(fake, "T1: work", assignee="c")
    fake.kanban_dispatch(BOARD)
    fake.kill_worker(card["id"], signal=9)

    fake.tick(30)

    assert fake.runs(card["id"])[0]["error"] == f"pid {FAKE_PID_BASE + 1} killed by signal 9"


def test_a_live_workers_expired_claim_is_extended_and_a_dead_workers_is_reclaimed_and_counted():
    fake = new_fake()
    fake.claim_ttl_seconds = 60
    fake.register_worker("c", hang)
    card = create(fake, "T1: work", assignee="c")
    fake.kanban_dispatch(BOARD)

    fake.tick(61)

    assert fake.card(card["id"])["status"] == "running"  # the worker is alive: its claim is extended, not reclaimed
    extended = fake.events(card["id"], "claim_extended")[0]["payload"]
    assert (extended["reason"], extended["worker_pid"]) == ("pid_alive", FAKE_PID_BASE + 1)
    assert extended["claim_expires_now"] == 1_000_061 + 60
    fake.kill_worker(card["id"])
    fake.tick(61)

    shown = fake.card(card["id"])
    assert shown["status"] == "ready"
    run = shown["_runs"][0]
    assert (run["outcome"], run["error"]) == ("reclaimed", "stale_lock=fake-host:1")
    payload = fake.events(card["id"], "reclaimed")[0]["payload"]
    assert (payload["stale_lock"], payload["host_local"], payload["heartbeat_stale"]) == ("fake-host:1", True, False)
    assert fake.snapshot()["tasks"][card["id"]]["consecutive_failures"] == 1  # a reclaim counts as a failed attempt


def test_a_heartbeat_extends_the_claim_and_records_the_note():
    fake = new_fake()
    fake.claim_ttl_seconds = 60
    fake.register_worker(
        "c", fw.ScriptedWorker([fw.Heartbeat("still going"), fw.Sleep(50), fw.Heartbeat(), fw.Timeout()]))
    card = create(fake, "T1: work", assignee="c")
    fake.kanban_dispatch(BOARD)
    assert fake.snapshot()["tasks"][card["id"]]["claim_expires"] == 1_000_060

    fake.tick(50)

    state = fake.snapshot()["tasks"][card["id"]]
    assert state["last_heartbeat_at"] == 1_000_050 and state["claim_expires"] == 1_000_050 + 60
    assert [e["payload"] for e in fake.events(card["id"], "heartbeat")] == [{"note": "still going"}, None]
    fake.tick(59)
    assert fake.events(card["id"], "claim_extended") == []  # renewed by the heartbeat: the first claim would have lapsed at 60
    fake.tick(2)
    assert len(fake.events(card["id"], "claim_extended")) == 1  # the renewed claim lapsed, and the worker is alive


# ---------------------------------------------------------------------------------------------
# The dispatcher
# ---------------------------------------------------------------------------------------------

DISPATCH_KEYS = {
    "reclaimed", "crashed", "timed_out", "stale", "auto_blocked", "promoted", "reaped_terminal_workers", "spawned",
    "skipped_unassigned", "skipped_nonspawnable", "skipped_per_profile_capped", "auto_assigned_default",
    "respawn_guarded", "rate_limited", "skipped_locked", "memory_pressure",
}


def test_dispatch_spawns_in_creation_order_and_returns_the_shape_of_the_cli_json():
    fake = new_fake()
    fake.register_worker("a", finish)
    fake.register_worker("b", finish)
    first = create(fake, "first", assignee="a")
    second = create(fake, "second", assignee="b")

    result = fake.kanban_dispatch(BOARD)

    assert set(result) == DISPATCH_KEYS
    assert result["spawned"] == [{"task_id": first["id"], "assignee": "a", "workspace": ""},
                                 {"task_id": second["id"], "assignee": "b", "workspace": ""}]
    assert result["reclaimed"] == 0 and result["promoted"] == 0 and result["skipped_locked"] is False
    assert result["memory_pressure"] is None and json.loads(json.dumps(result)) == result
    assert fake.card(first["id"])["status"] == fake.card(second["id"])["status"] == "done"
    (claimed,) = fake.events(first["id"], "claimed")
    assert claimed["payload"] == {"lock": "fake-host:1", "expires": 1_000_000 + 900, "run_id": 1}
    assert kinds(fake, first["id"]) == ["created", "claimed", "spawned", "completed"]


def test_dispatch_honours_max_in_progress():
    fake = new_fake()
    for name in "abcd":
        fake.register_worker(name, hang)
    ids = [create(fake, f"card {n}", assignee=n)["id"] for n in "abcd"]

    first = fake.kanban_dispatch(BOARD)

    assert [s["task_id"] for s in first["spawned"]] == ids[:3]  # kanban.max_in_progress is 3
    assert fake.card(ids[3])["status"] == "ready"  # the fourth stays queued
    assert fake.kanban_dispatch(BOARD)["spawned"] == []  # three running: at the cap
    fake.kanban_block(BOARD, ids[0], "free a slot")
    assert [s["task_id"] for s in fake.kanban_dispatch(BOARD)["spawned"]] == [ids[3]]


def test_dispatch_runs_one_card_per_profile_at_a_time():
    fake = new_fake()
    fake.register_worker("coder-1", hang)
    one = create(fake, "one", assignee="coder-1")
    two = create(fake, "two", assignee="coder-1")

    result = fake.kanban_dispatch(BOARD)

    assert [s["task_id"] for s in result["spawned"]] == [one["id"]]
    assert result["skipped_per_profile_capped"] == [{"task_id": two["id"], "assignee": "coder-1", "current": 1}]
    assert fake.card(two["id"])["status"] == "ready"
    fake.max_in_progress_per_profile = None
    assert [s["task_id"] for s in fake.kanban_dispatch(BOARD)["spawned"]] == [two["id"]]


def test_max_spawns_is_a_live_concurrency_cap_not_a_per_tick_budget():
    fake = new_fake()
    fake.max_in_progress = None
    ids = []
    for name in "abc":
        fake.register_worker(name, hang)
        ids.append(create(fake, f"card {name}", assignee=name)["id"])

    assert len(fake.kanban_dispatch(BOARD, max_spawns=2)["spawned"]) == 2
    assert fake.kanban_dispatch(BOARD, max_spawns=2)["spawned"] == []  # two are running already
    assert [s["task_id"] for s in fake.kanban_dispatch(BOARD, max_spawns=3)["spawned"]] == [ids[2]]


def test_a_dry_run_claims_nothing_and_says_what_it_would_spawn():
    fake = new_fake()
    fake.register_worker("c", finish)
    card = create(fake, "T1: work", assignee="c")
    before = fake.snapshot()

    result = fake.kanban_dispatch(BOARD, dry_run=True)

    assert result["spawned"] == [{"task_id": card["id"], "assignee": "c", "workspace": ""}]
    assert fake.snapshot() == before


def test_unassigned_and_non_profile_assignees_are_reported_not_spawned():
    fake = new_fake()
    fake.register_worker("c", finish)
    nobody = create(fake, "unassigned")
    lane = create(fake, "for a control-plane lane", assignee="orion-cc")
    real = create(fake, "real", assignee="c")

    result = fake.kanban_dispatch(BOARD)

    assert result["skipped_unassigned"] == [nobody["id"]] and result["skipped_nonspawnable"] == [lane["id"]]
    assert [s["task_id"] for s in result["spawned"]] == [real["id"]]


def test_a_card_handed_off_this_pass_is_reviewed_on_the_next_pass_by_the_reviewer_profile():
    fake = new_fake()
    fake.register_worker("coder-1", hand_off)
    fake.register_worker("reviewer", finish)
    card = create(fake, "T1: work", assignee="coder-1")

    first = fake.kanban_dispatch(BOARD)
    assert [s["assignee"] for s in first["spawned"]] == ["coder-1"] and fake.card(card["id"])["status"] == "review"
    second = fake.kanban_dispatch(BOARD)

    assert [s["assignee"] for s in second["spawned"]] == ["reviewer"]
    shown = fake.card(card["id"])
    assert shown["status"] == "done"
    assert [(r["profile"], r["outcome"]) for r in shown["_runs"]] == [
        ("coder-1", "review_requested"), ("reviewer", "completed")]
    assert fake.events(card["id"], "claimed")[1]["payload"]["source_status"] == "review"
    assert shown["_runs"][1]["metadata"]["worker_session_id"] == FakeHermes.session_id_for(card["id"], 2)


def test_review_dispatch_can_be_switched_off_like_kanban_review_dispatch_false():
    fake = new_fake()
    fake.review_dispatch = False
    fake.register_worker("coder-1", hand_off)
    fake.register_worker("reviewer", finish)
    card = create(fake, "T1: work", assignee="coder-1")
    fake.kanban_dispatch(BOARD)

    assert fake.kanban_dispatch(BOARD)["spawned"] == [] and fake.card(card["id"])["status"] == "review"


def test_pause_stops_the_gateway_dispatcher_but_not_the_cli_dispatch():
    """Hermes's `pause` is checked only in the gateway's loop (gateway/kanban_watchers.py), not by `hermes kanban dispatch`."""
    fake = new_fake()
    fake.gateway_dispatch = True
    fake.register_worker("c", hang)
    card = create(fake, "T1: work", assignee="c")

    fake.pause("swarm stop")
    fake.tick(60)
    assert (fake.paused, fake.pause_reason) == (True, "swarm stop")
    assert fake.card(card["id"])["status"] == "ready"  # the gateway does nothing while paused
    assert len(fake.kanban_dispatch(BOARD)["spawned"]) == 1  # the CLI has no pause check: ASES's own dispatch still spawns

    fake.resume()
    assert fake.paused is False and fake.pause_reason is None


def test_the_gateway_spawns_on_a_tick_only_when_asked_and_a_cli_that_honours_pause_spawns_nothing():
    fake = new_fake()
    fake.register_worker("c", hang)
    card = create(fake, "T1: work", assignee="c")
    fake.tick(5)
    assert fake.card(card["id"])["status"] == "ready"  # gateway_dispatch is off: a tick only reclaims and promotes
    fake.pause()
    fake.cli_dispatch_honors_pause = True

    assert fake.kanban_dispatch(BOARD) == {**fake._empty_dispatch_result()}
    fake.resume()
    fake.gateway_dispatch = True
    fake.tick(5)
    assert fake.card(card["id"])["status"] == "running"


def test_a_sleeping_worker_keeps_its_card_running_and_finishes_when_the_clock_reaches_it():
    fake = new_fake()
    fake.register_worker("c", fw.ScriptedWorker([fw.Sleep(120), fw.Complete("late")]))
    card = create(fake, "T1: work", assignee="c")
    fake.kanban_dispatch(BOARD)
    assert fake.card(card["id"])["status"] == "running" and fake.live_workers()[0]["orphan"] is False

    fake.tick(119)
    assert fake.card(card["id"])["status"] == "running"
    fake.tick(1)

    shown = fake.card(card["id"])
    assert shown["status"] == "done" and fake.now == 1_000_120
    (run,) = shown["_runs"]
    assert (run["outcome"], run["summary"], run["ended_at"]) == ("completed", "late", 1_000_120)
    assert fake.live_workers() == []


def test_a_sleeping_worker_that_would_wake_after_its_deadline_is_timed_out_instead():
    fake = new_fake()
    fake.register_worker("c", fw.ScriptedWorker([fw.Sleep(3000), fw.Complete("too late")]))
    card = create(fake, "T1: work", assignee="c", max_runtime="10m")
    fake.kanban_dispatch(BOARD)

    fake.tick(3000)

    shown = fake.card(card["id"])
    assert shown["status"] == "ready" and [r["outcome"] for r in shown["_runs"]] == ["timed_out"]
    assert "completed" not in kinds(fake, card["id"])


def test_a_worker_left_alive_underneath_a_blocked_card_is_an_orphan_until_it_is_reaped():
    fake = new_fake()
    fake.register_worker("c", hang)
    card = create(fake, "T1: work", assignee="c")
    fake.kanban_dispatch(BOARD)
    fake.kanban_block(BOARD, card["id"], "a human decision is needed")  # the controller blocks a running card

    assert [(w["card_id"], w["orphan"]) for w in fake.live_workers()] == [(card["id"], True)]
    fake.tick(119)
    assert fake.live_workers() != []
    fake.tick(1)

    assert fake.live_workers() == []
    assert fake.events(card["id"], "terminal_worker_reaped")[0]["payload"]["terminated"] is True


def test_a_worker_whose_run_was_reclaimed_cannot_act_on_its_successors_run():
    fake = new_fake()
    fake.register_worker("c", hang)
    card = create(fake, "T1: work", assignee="c")
    fake.kanban_dispatch(BOARD)
    old_run = fake.runs(card["id"])[0]["id"]
    fake.kanban_reclaim(BOARD, card["id"])
    fake.kanban_dispatch(BOARD)
    assert fake.runs(card["id"])[1]["id"] != old_run and fake.card(card["id"])["status"] == "running"

    with pytest.raises(AgentToolError, match="could not complete"):
        fake.agent_complete(card["id"], summary="late", run_id=old_run)
    with pytest.raises(AgentToolError, match="could not block"):
        fake.agent_block(card["id"], "too late", run_id=old_run)
    with pytest.raises(AgentToolError, match="could not request review"):
        fake.agent_request_review(card["id"], summary="s", run_id=old_run)
    with pytest.raises(AgentToolError, match="no live run"):
        fake.agent_fail(card["id"], "x", run_id=old_run)
    with pytest.raises(AgentToolError, match="could not heartbeat"):
        fake.agent_heartbeat(card["id"], run_id=old_run)
    assert fake.card(card["id"])["status"] == "running"


def test_agent_calls_on_a_card_that_is_not_running_are_refused():
    fake = new_fake()
    card = create(fake, "T1: work", assignee="c")

    with pytest.raises(AgentToolError, match="could not heartbeat"):
        fake.agent_heartbeat(card["id"])
    with pytest.raises(AgentToolError, match="no live run"):
        fake.agent_fail(card["id"], "x")
    with pytest.raises(AgentToolError, match="not found"):
        fake.agent_comment("t_nope", "hello")
    with pytest.raises(AgentToolError, match="provide at least one of"):
        fake.agent_complete(card["id"])
    with pytest.raises(AgentToolError, match="reason is required"):
        fake.agent_block(card["id"], "  ")
    with pytest.raises(AgentToolError, match="kind must be one of"):
        fake.agent_block(card["id"], "why", kind="whim")
    with pytest.raises(AgentToolError, match="comment body is required"):
        fake.agent_comment(card["id"], "   ")
    with pytest.raises(ValueError, match="unknown failure outcome"):
        fake.agent_fail(card["id"], "x", "exploded")


# ---------------------------------------------------------------------------------------------
# Real git worktrees
# ---------------------------------------------------------------------------------------------


def test_a_worktree_card_gets_a_real_worktree_cut_from_the_integration_tip_at_dispatch_time(tmp_path):
    repo = make_repo(tmp_path)
    fake = new_fake(repo=repo)
    seen = {}

    def worker(f, card, run, workspace):
        seen.update(ws=workspace, head=git(workspace, "rev-parse", "HEAD"), branch=git(workspace, "branch", "--show-current"))
        finish(f, card, run, workspace)

    fake.register_worker("c", worker)
    (repo / "later.txt").write_text("integration moved\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "integration moved on")
    tip = git(repo, "rev-parse", "integration")
    card = create(fake, "T1: work", assignee="c", workspace="worktree", branch="swarm/T1-coder")

    result = fake.kanban_dispatch(BOARD)

    assert seen["ws"] == repo / ".worktrees" / card["id"] and (seen["ws"] / "later.txt").exists()
    assert seen["head"] == tip and seen["branch"] == "swarm/T1-coder"  # cut from the CURRENT integration tip
    assert result["spawned"][0]["workspace"] == str(repo / ".worktrees" / card["id"])
    shown = fake.card(card["id"])
    assert (shown["workspace_path"], shown["branch_name"]) == (str(repo / ".worktrees" / card["id"]), "swarm/T1-coder")
    assert fake.worktree(card["id"]) == repo / ".worktrees" / card["id"]
    assert guards.check_primary_checkout(repo, "integration").ok  # the real integrity guard ignores .worktrees/


def test_a_retried_card_reuses_its_worktree_and_what_the_dead_worker_left_in_it(tmp_path):
    repo = make_repo(tmp_path)
    fake = new_fake(repo=repo)
    seen = []

    def worker(f, card, run, workspace):
        if not seen:
            seen.append(workspace)
            (workspace / "partial.txt").write_text("half done\n")
            f.agent_hang(card["id"], run_id=run["id"])
        else:
            assert workspace == seen[0] and (workspace / "partial.txt").read_text() == "half done\n"
            finish(f, card, run, workspace)

    fake.register_worker("c", worker)
    card = create(fake, "T1: work", assignee="c", workspace="worktree", branch="swarm/T1-coder")
    fake.kanban_dispatch(BOARD)
    fake.kanban_reclaim(BOARD, card["id"])

    fake.kanban_dispatch(BOARD)

    assert fake.card(card["id"])["status"] == "done"


def test_a_worktree_can_attach_to_a_branch_that_already_exists(tmp_path):
    repo = make_repo(tmp_path)
    git(repo, "branch", "swarm/T1-coder")
    fake = new_fake(repo=repo)
    fake.register_worker("c", lambda f, card, run, ws: (finish(f, card, run, ws), None)[1])
    card = create(fake, "T1: work", assignee="c", workspace="worktree", branch="swarm/T1-coder")

    fake.kanban_dispatch(BOARD)

    assert fake.card(card["id"])["status"] == "done"
    assert git(repo / ".worktrees" / card["id"], "branch", "--show-current") == "swarm/T1-coder"


def test_a_worktree_card_without_a_repository_is_a_spawn_failure_and_git_refusals_are_too(tmp_path):
    fake = new_fake()  # no primary checkout
    fake.register_worker("c", finish)
    card = create(fake, "T1: work", assignee="c", workspace="worktree", branch="swarm/T1-coder")

    fake.kanban_dispatch(BOARD)

    (run,) = fake.runs(card["id"])
    assert (run["outcome"], run["worker_pid"]) == ("spawn_failed", None)
    assert run["error"].startswith("workspace: task ") and "no primary checkout" in run["error"]
    assert fake.card(card["id"])["status"] == "ready"

    (tmp_path / "second").mkdir()
    repo = make_repo(tmp_path / "second")
    other = new_fake(repo=repo)
    other.register_worker("a", hang)
    other.register_worker("b", finish)
    first = create(other, "first", assignee="a", workspace="worktree", branch="swarm/shared")
    second = create(other, "second", assignee="b", workspace="worktree", branch="swarm/shared")
    other.kanban_dispatch(BOARD)  # git refuses a second checkout of a branch that is already checked out
    assert other.card(first["id"])["status"] == "running" and other.card(second["id"])["status"] == "ready"
    assert other.runs(second["id"])[0]["error"].startswith("workspace: git worktree add failed for")


def test_scripted_edits_land_in_the_cards_own_worktree_and_nowhere_else(tmp_path):
    repo = make_repo(tmp_path)
    fake = new_fake(repo=repo)
    fake.register_worker("a", fw.ScriptedWorker([fw.Write("a.txt", "from a\n"), fw.Commit("a"), fw.Complete("a done")]))
    fake.register_worker("b", fw.ScriptedWorker([fw.Write("b.txt", "from b\n"), fw.Complete("b done")]))
    tip = git(repo, "rev-parse", "integration")
    card_a = create(fake, "A", assignee="a", workspace="worktree", branch="swarm/A")
    card_b = create(fake, "B", assignee="b", workspace="worktree", branch="swarm/B")

    fake.kanban_dispatch(BOARD)

    ws_a, ws_b = fake.worktree(card_a["id"]), fake.worktree(card_b["id"])
    assert ws_a != ws_b and (ws_a / "a.txt").exists() and not (ws_a / "b.txt").exists()
    assert (ws_b / "b.txt").exists() and not (ws_b / "a.txt").exists()
    assert not (repo / "a.txt").exists() and not (repo / "b.txt").exists()  # the primary checkout is untouched
    assert git(repo, "rev-parse", "integration") == tip and git(repo, "status", "--porcelain", "--untracked-files=no") == ""
    assert git(repo, "log", "-1", "--format=%s", "swarm/A") == "a" and git(repo, "rev-parse", "swarm/B") == tip
    assert git(ws_b, "status", "--porcelain") == "?? b.txt"  # b never committed
    assert guards.check_primary_checkout(repo, "integration", tip).ok


# ---------------------------------------------------------------------------------------------
# The clock, the call log, failure injection, the snapshot, install, signatures
# ---------------------------------------------------------------------------------------------


def test_tick_advances_the_clock_and_every_timestamp_uses_it():
    fake = new_fake()
    card = create(fake, "T1: work", assignee="c")

    fake.tick(5)
    fake.kanban_comment(BOARD, card["id"], "later")
    fake.kanban_block(BOARD, card["id"], "why")

    shown = fake.card(card["id"])
    assert fake.now == 1_000_005
    assert shown["created_at"] == 1_000_000 and shown["_events"][0]["created_at"] == 1_000_000
    assert [e["created_at"] for e in shown["_events"][1:]] == [1_000_005] * 3  # commented (x2, one is the block's), blocked
    assert [c["created_at"] for c in shown["_comments"]] == [1_000_005, 1_000_005]
    assert [r["started_at"] for r in shown["_runs"]] == [1_000_005]
    assert abs(FakeHermes(board=BOARD).now - time.time()) < 5  # without now= the clock starts at the real time


def test_an_armed_failure_fires_before_any_effect_and_only_for_the_named_card():
    fake = new_fake()
    a, b = create(fake, "a", assignee="c"), create(fake, "b", assignee="c")
    fake.fail_next("kanban_block", card_id=a["id"],
                   error=HermesCommandError(["kanban", "block"], 1, "database is locked"))

    fake.kanban_block(BOARD, b["id"], "fine")  # a different card passes through
    refuses(lambda: fake.kanban_block(BOARD, a["id"], "boom"), "database is locked")

    assert fake.card(a["id"])["_comments"] == [] and fake.card(a["id"])["status"] == "ready"  # nothing happened at all
    fake.kanban_block(BOARD, a["id"], "works now")  # it fired once
    assert fake.card(a["id"])["status"] == "blocked"


def test_an_armed_failure_defaults_to_a_hermes_command_error_and_can_repeat():
    fake = new_fake()
    card = create(fake, "T1: work")
    fake.fail_next("kanban_show", times=2)

    refuses(lambda: fake.kanban_show(BOARD, card["id"]), "injected failure")
    refuses(lambda: fake.kanban_show(BOARD, card["id"]), "injected failure")
    assert fake.kanban_show(BOARD, card["id"])["id"] == card["id"]
    with pytest.raises(ValueError, match="not a public function"):
        fake.fail_next("not_a_hermes_function")
    with pytest.raises(ValueError, match="not a public function"):
        fake.fail_next("_run")


def test_the_call_log_records_controller_calls_in_order_and_not_agent_calls_or_inspection():
    fake = new_fake()
    fake.register_worker("c", finish)
    card = create(fake, "T1: work", assignee="c")
    fake.kanban_show(BOARD, card["id"])
    fake.kanban_list(BOARD, status="ready")
    fake.kanban_dispatch(BOARD, dry_run=True)
    fake.card(card["id"])
    fake.cards()
    fake.events(card["id"])
    fake.fail_next("kanban_show")
    with pytest.raises(HermesCommandError):
        fake.kanban_show(BOARD, card["id"])

    assert [c.name for c in fake.calls] == ["kanban_create", "kanban_show", "kanban_list", "kanban_dispatch", "kanban_show"]
    assert fake.calls[1].args == (BOARD, card["id"]) and fake.calls[2].kwargs == {"status": "ready"}
    assert fake.calls[3].kwargs == {"dry_run": True}
    fake.kanban_dispatch(BOARD)  # the worker's own agent_complete is not a controller call
    assert [c.name for c in fake.calls][-1] == "kanban_dispatch" and len(fake.calls) == 6


def test_reads_and_no_ops_leave_the_snapshot_untouched():
    fake = new_fake()
    fake.register_worker("c", finish)
    card = create(fake, "T1: work", assignee="c", idempotency_key="k")
    lane = create(fake, "for a control-plane lane", assignee="orion-cc")
    before = fake.snapshot()

    fake.kanban_show(BOARD, card["id"])
    fake.kanban_list(BOARD)
    fake.card(card["id"]), fake.cards(), fake.events(card["id"]), fake.comments(card["id"]), fake.runs(card["id"])
    create(fake, "T1: work", idempotency_key="k")
    fake.hermes_version(), fake.gateway_status(), fake.run_doctor(), fake.describe()
    fake.kanban_archive(BOARD, [])
    assert fake.snapshot() == before

    fake.kanban_comment(BOARD, lane["id"], "now something changed")
    assert fake.snapshot() != before
    snap = fake.snapshot()
    snap["tasks"][card["id"]]["status"] = "done"  # a snapshot is a copy: editing it changes nothing
    assert fake.card(card["id"])["status"] == "ready"


def test_a_board_other_than_the_fakes_is_refused_even_the_default_board():
    fake = new_fake()

    for board in ("default", "ases-phase3"):
        error = refuses(lambda: fake.kanban_list(board), f"board '{board}' does not exist")
        assert "quietly used the default board" in error.output
    assert fake.cards() == []


def test_install_replaces_every_public_hermes_function_and_forbids_running_a_real_hermes(monkeypatch):
    fake = new_fake().install(monkeypatch)

    card = hermes.kanban_create(BOARD, "T1: work", assignee="c")
    assert hermes.kanban_show(BOARD, card["id"])["status"] == "ready"
    assert [c.name for c in fake.calls] == ["kanban_create", "kanban_show"]
    assert hermes.hermes_version() == "0.21.3" and hermes.gateway_status().running is True
    assert hermes.run_doctor().ok is True
    with pytest.raises(hermes.HermesNotFound):
        hermes.hermes_path()
    with pytest.raises(AssertionError, match="nothing may start a real hermes"):
        hermes._run(["--version"])
    with pytest.raises(HermesCommandError, match="board 'default' does not exist"):
        hermes.kanban_show("default", card["id"])


def test_fail_next_can_still_be_armed_after_install(monkeypatch):
    """Round 6 regression: install() replaces hermes.kanban_show with a bound method of the fake, so the old check
    (inspect.isfunction on hermes.<name>'s CURRENT value) rejected every name once installed. fail_next must validate
    against the real hermes module's public names captured at import time, not a live, monkeypatch-able lookup."""
    fake = new_fake().install(monkeypatch)
    card = hermes.kanban_create(BOARD, "T1: work")

    fake.fail_next("kanban_show")  # used to raise ValueError("... is not a public function ...") here

    with pytest.raises(HermesCommandError, match="injected failure"):
        hermes.kanban_show(BOARD, card["id"])
    assert hermes.kanban_show(BOARD, card["id"])["id"] == card["id"]  # armed once, not forever

    with pytest.raises(ValueError, match="not a public function"):
        fake.fail_next("not_a_hermes_function")
    with pytest.raises(ValueError, match="not a public function"):
        fake.fail_next("_run")
    with pytest.raises(ValueError, match="not a public function"):
        fake.fail_next("card")  # a real method of the fake, but not a hermes-module function: must still be rejected


def test_install_fails_loudly_for_a_hermes_wrapper_the_fake_does_not_have(monkeypatch):
    def kanban_future(board):
        """A wrapper added to hermes.py later."""

    kanban_future.__module__ = hermes.__name__
    monkeypatch.setattr(hermes, "kanban_future", kanban_future, raising=False)

    with pytest.raises(AttributeError, match="FakeHermes has no kanban_future"):
        new_fake().install(monkeypatch)


def test_every_public_hermes_function_has_a_fake_with_the_same_signature():
    """Names, kinds and defaults of every parameter (annotations are cosmetic). A wrapper added to hermes.py later fails
    here until the fake has it, which is the point."""
    fake = new_fake()
    public = [
        (name, function) for name, function in inspect.getmembers(hermes, inspect.isfunction)
        if not name.startswith("_") and function.__module__ == hermes.__name__
    ]
    assert {name for name, _ in public} >= {
        "kanban_create", "kanban_show", "kanban_list", "kanban_link", "kanban_dispatch", "kanban_block",
        "kanban_unblock", "kanban_schedule", "kanban_promote", "kanban_comment", "kanban_complete",
        "kanban_archive", "kanban_reclaim", "kanban_reopen_review", "kanban_request_changes", "kanban_set_model",
        "pause", "resume", "session_usage", "hermes_version", "gateway_status", "run_doctor", "hermes_path",
    }
    for name, function in public:
        theirs = [(p.name, p.kind, p.default) for p in inspect.signature(function).parameters.values()]
        ours = [(p.name, p.kind, p.default) for p in inspect.signature(getattr(fake, name)).parameters.values()]
        assert ours == theirs, f"FakeHermes.{name} differs from hermes.{name}"


def test_version_doctor_gateway_and_hermes_path_are_healthy_by_default_and_overridable():
    fake = new_fake()

    assert fake.hermes_version() == "0.21.3" and fake.gateway_status().running is True
    assert fake.run_doctor().ok is True and fake.run_doctor().exit_code == 0
    fake.version = None
    fake.gateway_running = False
    fake.hermes_path_value = "C:/fake/hermes.exe"
    assert fake.hermes_version() is None and fake.gateway_status().running is False
    assert fake.hermes_path() == "C:/fake/hermes.exe"


def test_session_usage_reports_set_numbers_a_default_for_real_sessions_and_none_for_unknown_ones():
    fake = new_fake()
    fake.register_worker("c", finish)
    card = create(fake, "T1: work", assignee="c")
    fake.kanban_dispatch(BOARD)
    session = fake.runs(card["id"])[0]["metadata"]["worker_session_id"]

    assert fake.session_usage("c", session) == {
        "id": session, "model": "", "api_call_count": 1, "input_tokens": 1000, "output_tokens": 200}
    fake.set_session_usage(session, api_call_count=7, input_tokens=5, output_tokens=6, model="m-1")
    assert fake.session_usage("c", session) == {
        "id": session, "model": "m-1", "api_call_count": 7, "input_tokens": 5, "output_tokens": 6}
    fake.set_session_usage_unknown(session)
    assert fake.session_usage("c", session) is None  # "unknown", never zero
    assert fake.session_usage("c", "sess_nobody_ever_had") is None
    fake.default_session_requests = 4
    other = create(fake, "T2: work", assignee="c")
    fake.kanban_dispatch(BOARD)
    assert fake.session_usage("c", fake.runs(other["id"])[0]["metadata"]["worker_session_id"])["api_call_count"] == 4


# ---------------------------------------------------------------------------------------------
# Scripted workers: steps
# ---------------------------------------------------------------------------------------------

SEED = {
    "README.md": "hi\n",
    "tests/test_feature.py": "def test_feature():\n    assert 1 == 2\n",
    "scripts/test.sh": "#!/bin/sh\npytest\n",
}


def dispatch_one(tmp_path, worker, *, files=None, profile="coder-1", reviewer=None, max_runtime=None, body=None):
    """One worktree card assigned to `profile`, dispatched once. Returns (fake, repo, card id)."""
    repo = make_repo(tmp_path, files)
    fake = new_fake(repo=repo)
    fake.register_worker(profile, worker)
    fake.register_worker("reviewer", reviewer or finish)
    card = create(fake, "T1: work", assignee=profile, workspace="worktree", branch="swarm/T1-coder",
                  max_runtime=max_runtime, body=body)
    fake.kanban_dispatch(BOARD)
    return fake, repo, card["id"]


def test_write_append_modify_and_delete_touch_exact_bytes_in_the_worktree(tmp_path):
    steps = [fw.Write("pkg/a.py", "x = 1\n"), fw.Append("pkg/a.py", "y = 2\n"), fw.Modify("README.md", str.upper),
             fw.Write("gone.txt", "bye\n"), fw.Delete("gone.txt"), fw.Complete("done")]

    fake, repo, cid = dispatch_one(tmp_path, fw.ScriptedWorker(steps))

    ws = fake.worktree(cid)
    assert (ws / "pkg" / "a.py").read_bytes() == b"x = 1\ny = 2\n"  # no newline translation on Windows
    assert (ws / "README.md").read_bytes() == b"HI\n" and not (ws / "gone.txt").exists()
    assert fake.card(cid)["status"] == "done"


def test_commit_commits_everything_changed_and_refuses_an_empty_commit(tmp_path):
    steps = [fw.Write("a.txt", "a\n"), fw.Write("b/c.txt", "c\n"), fw.Commit("two files"), fw.Complete("done")]

    fake, repo, cid = dispatch_one(tmp_path, fw.ScriptedWorker(steps))

    shown = [line for line in git(repo, "show", "--name-only", "--format=%s", "swarm/T1-coder").splitlines() if line]
    assert shown == ["two files", "a.txt", "b/c.txt"]
    assert git(repo, "log", "-1", "--format=%an <%ae>", "swarm/T1-coder") == "ASES fake worker <fake-worker@example.invalid>"
    (tmp_path / "again").mkdir()
    with pytest.raises(RuntimeError, match="nothing to commit"):
        dispatch_one(tmp_path / "again", fw.ScriptedWorker([fw.Commit("empty")]))
    (tmp_path / "third").mkdir()
    fake, repo, cid = dispatch_one(tmp_path / "third", fw.ScriptedWorker([fw.Commit("on purpose", allow_empty=True), fw.Complete("ok")]))
    assert git(repo, "log", "-1", "--format=%s", "swarm/T1-coder") == "on purpose"


def test_untracked_leaves_a_file_that_git_does_not_know_and_an_absolute_path_writes_outside_the_worktree(tmp_path):
    outside = tmp_path / "outside.txt"
    steps = [fw.Write("app.py", "import helper\n"), fw.Commit("app"), fw.Untracked("helper.py", "VALUE = 1\n"),
             fw.Write(str(outside), "escaped\n"), fw.Complete("done")]

    fake, repo, cid = dispatch_one(tmp_path, fw.ScriptedWorker(steps))

    ws = fake.worktree(cid)
    assert git(ws, "status", "--porcelain") == "?? helper.py"
    assert outside.read_text() == "escaped\n" and not (ws / "outside.txt").exists()


def test_request_review_carries_the_head_sha_the_changed_files_and_the_named_reviewer(tmp_path):
    steps = [fw.Write("a.py", "x\n"), fw.Commit("c"), fw.RequestReview("added a.py")]

    fake, repo, cid = dispatch_one(tmp_path, fw.ScriptedWorker(steps))

    (run,) = fake.runs(cid)
    head = git(repo, "rev-parse", "swarm/T1-coder")
    assert run["metadata"]["commit_sha"] == head
    assert (run["metadata"]["changed_files"], run["metadata"]["residual_risk"]) == (["a.py"], "none")
    assert run["summary"] == "added a.py" and fake.card(cid)["assignee"] == "reviewer"


def test_request_review_metadata_you_give_is_used_and_at_head_is_replaced_and_no_reviewer_keeps_the_card(tmp_path):
    steps = [fw.Write("a.py", "x\n"), fw.Commit("c"),
             fw.RequestReview("done", metadata={"commit": "@HEAD", "nested": ["@HEAD", "plain"]}, reviewer=None)]

    fake, repo, cid = dispatch_one(tmp_path, fw.ScriptedWorker(steps))

    head = git(repo, "rev-parse", "swarm/T1-coder")
    (run,) = fake.runs(cid)
    assert (run["metadata"]["commit"], run["metadata"]["nested"]) == (head, [head, "plain"])
    assert "commit_sha" not in run["metadata"]  # nothing was added to the hand-off you wrote
    assert (fake.card(cid)["status"], fake.card(cid)["assignee"]) == ("review", "coder-1")  # it would review itself


def test_comment_heartbeat_and_block_steps_report_through_the_fake(tmp_path):
    steps = [fw.Comment("starting"), fw.Heartbeat("halfway"), fw.Block("which database?", kind="needs_input")]

    fake, repo, cid = dispatch_one(tmp_path, fw.ScriptedWorker(steps))

    shown = fake.card(cid)
    assert shown["status"] == "blocked"
    assert [(c["author"], c["body"]) for c in shown["_comments"]] == [("coder-1", "starting")]
    assert fake.events(cid, "heartbeat")[0]["payload"] == {"note": "halfway"}
    assert fake.events(cid, "blocked")[0]["payload"]["kind"] == "needs_input"


@pytest.mark.parametrize("outcome", ["crashed", "timed_out", "rate_limited", "spawn_failed"])
def test_the_crash_step_books_the_outcome_and_stops_the_script(tmp_path, outcome):
    steps = [fw.Write("before.txt", "1\n"), fw.Crash("it broke", outcome=outcome), fw.Write("after.txt", "2\n")]

    fake, repo, cid = dispatch_one(tmp_path, fw.ScriptedWorker(steps))

    ws = fake.worktree(cid)
    assert (ws / "before.txt").exists() and not (ws / "after.txt").exists()
    shown = fake.card(cid)
    assert shown["status"] == "ready" and (shown["_runs"][0]["outcome"], shown["_runs"][0]["error"]) == (outcome, "it broke")
    assert fake.live_workers() == []


def test_the_timeout_step_hangs_the_worker_until_the_clock_passes_its_max_runtime(tmp_path):
    steps = [fw.Write("a.txt", "a\n"), fw.Timeout(), fw.Write("b.txt", "b\n")]

    fake, repo, cid = dispatch_one(tmp_path, fw.ScriptedWorker(steps), max_runtime="5m")

    ws = fake.worktree(cid)
    assert fake.card(cid)["status"] == "running" and not (ws / "b.txt").exists()
    fake.tick(300)
    shown = fake.card(cid)
    assert shown["status"] == "ready" and shown["_runs"][0]["outcome"] == "timed_out"


def test_the_sleep_step_resumes_the_rest_of_the_script_later_in_the_same_worktree(tmp_path):
    steps = [fw.Write("a.txt", "a\n"), fw.Sleep(60), fw.Write("b.txt", "b\n"), fw.Commit("both"), fw.Complete("done")]

    fake, repo, cid = dispatch_one(tmp_path, fw.ScriptedWorker(steps))

    ws = fake.worktree(cid)
    assert fake.card(cid)["status"] == "running" and (ws / "a.txt").exists() and not (ws / "b.txt").exists()
    fake.tick(60)
    assert fake.card(cid)["status"] == "done"
    assert git(repo, "show", "--name-only", "--format=", "swarm/T1-coder").splitlines() == ["a.txt", "b.txt"]


def test_do_runs_arbitrary_code_a_non_step_is_refused_and_a_script_that_raises_fails_the_dispatch(tmp_path):
    calls = []

    def note(ctx):
        calls.append((ctx.card_id, ctx.profile, ctx.run_id, ctx.workspace.name))
        ctx.fake.agent_complete(ctx.card_id, summary="via do", run_id=ctx.run_id)

    fake, repo, cid = dispatch_one(tmp_path, fw.ScriptedWorker([[fw.Do(note)]]))  # a nested list is flattened
    assert calls == [(cid, "coder-1", 1, cid)] and fake.card(cid)["status"] == "done"
    with pytest.raises(TypeError, match="not a worker step"):
        fw.ScriptedWorker(["nope"])
    with pytest.raises(RuntimeError, match="Sleep is handled by ScriptedWorker"):
        fw.Sleep(1).run(None)

    (tmp_path / "broken").mkdir()
    with pytest.raises(FileNotFoundError):
        dispatch_one(tmp_path / "broken", fw.ScriptedWorker([fw.Delete("no-such-file.txt")]))


# ---------------------------------------------------------------------------------------------
# Factories and composition
# ---------------------------------------------------------------------------------------------


def test_task_key_reads_the_plan_key_off_a_card_title():
    assert fw.task_key({"title": "T10: scaffold"}) == "T10"
    assert fw.task_key({"title": "T1: fix (round 2)"}) == "T1"
    assert fw.task_key({"title": "  T3 : spaced"}) == "T3"
    assert fw.task_key({"title": "no colon here"}) is None and fw.task_key({}) is None


def test_by_task_key_picks_a_worker_per_plan_task_and_a_fix_card_shares_its_tasks_worker():
    fake = new_fake()
    fake.max_in_progress_per_profile = None
    seen = []

    def marker(name):
        def worker(f, card, run, workspace):
            seen.append(name)
            finish(f, card, run, workspace)
        return worker

    fake.register_worker("c", fw.by_task_key({"T1": marker("one"), "T2": marker("two")}))
    for title in ("T1: work", "T2: work", "T1: fix (round 1)"):
        create(fake, title, assignee="c")

    fake.kanban_dispatch(BOARD)

    assert seen == ["one", "two", "one"]
    stranger = create(fake, "T9: unplanned", assignee="c")
    with pytest.raises(KeyError, match="no worker scripted for task 'T9'"):
        fake.kanban_dispatch(BOARD)
    fake.kanban_reclaim(BOARD, stranger["id"])  # the failed dispatch left it claimed
    fake.register_worker("c", fw.by_task_key({"T1": marker("one")}, default=marker("fallback")))
    fake.kanban_dispatch(BOARD)
    assert seen[-1] == "fallback"


def test_sequence_runs_the_next_worker_each_time_the_same_card_comes_back():
    fake = new_fake()
    seen = []

    def marker(name):
        def worker(f, card, run, workspace):
            seen.append(name)
            f.agent_fail(card["id"], "again", "crashed", run_id=run["id"]) if name != "last" else finish(f, card, run, workspace)
        return worker

    fake.register_worker("c", fw.sequence(marker("first"), marker("last")))
    fake.max_in_progress_per_profile = None
    card = create(fake, "T1: work", assignee="c")
    other = create(fake, "T2: work", assignee="c")

    fake.kanban_dispatch(BOARD)  # each card is on its own first visit
    fake.kanban_dispatch(BOARD)

    assert seen == ["first", "first", "last", "last"]
    assert fake.card(card["id"])["status"] == fake.card(other["id"])["status"] == "done"
    with pytest.raises(ValueError, match="at least one worker"):
        fw.sequence()


def test_a_function_of_the_card_is_a_factory_and_a_four_argument_callable_is_a_worker():
    fake = new_fake()
    seen = []

    def factory(card):
        def worker(f, c, run, workspace):
            seen.append(("built for", card["title"]))
            finish(f, c, run, workspace)
        return worker

    fake.register_worker("c", factory)
    create(fake, "T1: work", assignee="c")
    fake.kanban_dispatch(BOARD)

    assert seen == [("built for", "T1: work")]
    assert fw.by_task_key({}).is_factory is True and not hasattr(fw.ScriptedWorker([]), "is_factory")


# ---------------------------------------------------------------------------------------------
# Personas
# ---------------------------------------------------------------------------------------------


def test_good_coder_writes_and_commits_in_its_worktree_and_hands_off_with_the_commit_sha(tmp_path):
    fake, repo, cid = dispatch_one(tmp_path, fw.good_coder({"a.py": "x = 1\n", "pkg/b.py": "y = 2\n"}, "add files"))

    shown = fake.card(cid)
    assert (shown["status"], shown["assignee"]) == ("review", "reviewer")
    head = git(repo, "rev-parse", "swarm/T1-coder")
    assert shown["_runs"][0]["metadata"]["commit_sha"] == head
    assert shown["_runs"][0]["metadata"]["changed_files"] == ["a.py", "pkg/b.py"]
    assert git(repo, "log", "-1", "--format=%s", "swarm/T1-coder") == "add files"
    assert git(repo, "show", "swarm/T1-coder:pkg/b.py") == "y = 2"
    assert git(repo, "rev-parse", "integration") == git(repo, "rev-parse", "swarm/T1-coder~1")  # branched from, not merged into


def test_wrong_coder_hands_in_the_wrong_content_with_a_confident_summary(tmp_path):
    fake, repo, cid = dispatch_one(tmp_path, fw.wrong_coder({"a.py": "def add(x, y):\n    return x - y\n"}))

    assert git(repo, "show", "swarm/T1-coder:a.py") == "def add(x, y):\n    return x - y"
    assert "every acceptance criterion is met" in fake.runs(cid)[0]["summary"]
    assert fake.card(cid)["status"] == "review"


def test_slow_coder_keeps_its_card_running_with_uncommitted_work_until_the_clock_passes_its_time(tmp_path):
    fake, repo, cid = dispatch_one(tmp_path, fw.slow_coder({"a.py": "x = 1\n"}, "add a.py", seconds=600))

    ws = fake.worktree(cid)
    assert fake.card(cid)["status"] == "running" and git(ws, "status", "--porcelain") == "?? a.py"
    assert git(repo, "rev-parse", "swarm/T1-coder") == git(repo, "rev-parse", "integration")  # nothing committed yet
    assert fake.events(cid, "heartbeat")[0]["payload"] == {"note": "still working"}
    fake.tick(599)
    assert fake.card(cid)["status"] == "running"
    fake.tick(1)

    assert fake.card(cid)["status"] == "review" and git(repo, "log", "-1", "--format=%s", "swarm/T1-coder") == "add a.py"


@pytest.mark.parametrize("kind, expected", [
    ("delete_test", "test_file_deleted"), ("skip_marker", "skip_marker"), ("or_true", "unconditional_pass"),
])
def test_tampering_coder_makes_the_diff_the_real_tamper_check_flags(tmp_path, kind, expected):
    fake, repo, cid = dispatch_one(tmp_path, fw.tampering_coder(kind), files=SEED)

    findings = tamper.check_range(repo, "integration", "swarm/T1-coder")

    assert expected in {finding.kind for finding in findings}, findings
    assert fake.card(cid)["status"] == "review"  # it still hands off: catching it is the controller's job


def test_tampering_coder_outside_paths_is_caught_by_the_real_touches_check(tmp_path):
    fake, repo, cid = dispatch_one(tmp_path, fw.tampering_coder("outside_paths"), files=SEED)

    with contextlib.closing(db.connect(tmp_path / "gate.db")) as conn:
        check = review.check_branch(repo, "swarm/T1-coder", "integration", ["echo ok"], ["a.py"], conn=conn, task_key="T1")

    assert (check.ok, check.kind) == (False, "out_of_scope") and "OUTSIDE_TOUCHES.md" in check.detail
    assert integrity.paths_outside_touches(["OUTSIDE_TOUCHES.md"], ["a.py"]) == ["OUTSIDE_TOUCHES.md"]


def test_tampering_coder_untracked_passes_only_in_its_own_worktree_and_fails_a_clean_checkout(tmp_path):
    fake, repo, cid = dispatch_one(tmp_path, fw.tampering_coder("untracked"), files=SEED)
    ws = fake.worktree(cid)

    in_worktree = subprocess.run([sys.executable, "-B", "-c", "import app"], cwd=str(ws), capture_output=True)
    with contextlib.closing(db.connect(tmp_path / "gate.db")) as conn:
        clean = review.check_branch(
            repo, "swarm/T1-coder", "integration", ['python -c "import app"'], ["app.py"], conn=conn, task_key="T1")

    assert in_worktree.returncode == 0 and git(ws, "status", "--porcelain") == "?? helper.py"
    assert (clean.ok, clean.kind) == (False, "gate1_red")  # Gate 1 runs in a clean checkout of the commit


def test_tampering_coder_takes_a_path_override_and_refuses_an_unknown_kind(tmp_path):
    files = {**SEED, "tests/other_test.py": "def test_other():\n    assert False\n"}
    fake, repo, cid = dispatch_one(tmp_path, fw.tampering_coder("delete_test", path="tests/other_test.py"), files=files)

    findings = tamper.check_range(repo, "integration", "swarm/T1-coder")

    assert [f.path for f in findings if f.kind == "test_file_deleted"] == ["tests/other_test.py"]
    with pytest.raises(ValueError, match="unknown tampering kind"):
        fw.tampering_coder("wishful thinking")
    assert set(fw.TAMPER_KINDS) == {"delete_test", "skip_marker", "or_true", "outside_paths", "untracked"}


def test_questioner_asks_once_and_does_its_work_after_the_answer():
    fake = new_fake()
    fake.register_worker("c", fw.questioner("Which database?", then=finish))
    card = create(fake, "T1: work", assignee="c")

    fake.kanban_dispatch(BOARD)

    assert fake.card(card["id"])["status"] == "blocked"
    assert fake.events(card["id"], "blocked")[0]["payload"]["reason"] == "Which database?"
    assert fake.events(card["id"], "blocked")[0]["payload"]["kind"] == "needs_input"
    fake.kanban_unblock(BOARD, card["id"], "sqlite")
    fake.kanban_dispatch(BOARD)
    assert fake.card(card["id"])["status"] == "done"

    plain = new_fake()
    plain.register_worker("c", fw.questioner("Which port?", kind=None))
    other = create(plain, "T1: work", assignee="c")
    plain.kanban_dispatch(BOARD)
    plain.kanban_unblock(BOARD, other["id"])
    plain.kanban_dispatch(BOARD)
    assert plain.events(other["id"], "blocked")[0]["payload"]["kind"] is None
    assert plain.runs(other["id"])[-1]["summary"] == "answered, and done"


def test_crasher_fails_the_first_dispatches_then_recovers_or_is_given_up():
    fake = new_fake()
    fake.register_worker("c", fw.crasher(1, error="pid 9 exited with code 2", exit_code=2))
    card = create(fake, "T1: work", assignee="c")

    fake.kanban_dispatch(BOARD)
    assert fake.card(card["id"])["status"] == "ready" and fake.runs(card["id"])[0]["error"] == "pid 9 exited with code 2"
    assert fake.events(card["id"], "crashed")[0]["payload"]["exit_code"] == 2
    fake.kanban_dispatch(BOARD)
    assert fake.card(card["id"])["status"] == "done" and fake.runs(card["id"])[-1]["summary"] == "recovered after the crashes"

    hopeless = new_fake()
    hopeless.register_worker("c", fw.crasher(5, outcome="timed_out", error="elapsed 2700s > limit 2700s"))
    other = create(hopeless, "T1: work", assignee="c")
    hopeless.kanban_dispatch(BOARD)
    hopeless.kanban_dispatch(BOARD)
    assert hopeless.card(other["id"])["status"] == "blocked"
    assert hopeless.events(other["id"], "gave_up")[0]["payload"]["trigger_outcome"] == "timed_out"


def test_touches_coder_writes_the_files_the_card_body_names_and_falls_back_to_a_key_named_file(tmp_path):
    repo = make_repo(tmp_path)
    fake = new_fake(repo=repo)
    fake.register_worker("coder-1", fw.touches_coder("work"))
    fake.register_worker("reviewer", finish)
    body = "Role: coder\nAcceptance criteria:\n- it works\nTouches: src/*.py, docs/guide.md\nGate profile: trivial"
    create(fake, "T1: scaffold", assignee="coder-1", body=body, workspace="worktree", branch="swarm/T1-coder")
    create(fake, "T2: docs only", assignee="coder-1", body="Role: coder\nGate profile: trivial", workspace="worktree",
           branch="swarm/T2-coder")
    fake.max_in_progress_per_profile = None

    fake.kanban_dispatch(BOARD)

    assert git(repo, "diff", "--name-only", "integration...swarm/T1-coder").splitlines() == ["docs/guide.md", "src/T1.py"]
    assert git(repo, "diff", "--name-only", "integration...swarm/T2-coder").splitlines() == ["T2.txt"]


def two_step(tmp_path, reviewer_worker, coder_worker=None):
    """A coder hands off in the first dispatch and `reviewer_worker` reviews in the second."""
    coder_worker = coder_worker or fw.good_coder({"a.py": "x = 1\n"}, "add a.py")
    fake, repo, cid = dispatch_one(tmp_path, coder_worker, reviewer=reviewer_worker)
    fake.kanban_dispatch(BOARD)
    return fake, repo, cid


def test_reviewer_pass_completes_with_a_verdict_in_both_shapes_naming_the_reviewed_commit(tmp_path):
    fake, repo, cid = two_step(tmp_path, fw.reviewer_pass())

    head = git(repo, "rev-parse", "swarm/T1-coder")
    shown = fake.card(cid)
    assert shown["status"] == "done"
    run = shown["_runs"][-1]
    assert (run["profile"], run["outcome"]) == ("reviewer", "completed")
    metadata = run["metadata"]
    assert (metadata["review_status"], metadata["review_outcome"], metadata["commit"]) == ("PASS", "approved", head)
    verdict = review.validate_verdict(metadata)  # the real validator accepts it
    assert verdict.valid and verdict.outcome == "PASS" and verdict.commit == head
    assert review.verdict_matches_head(verdict, head)


def test_reviewer_pass_can_name_a_specific_commit(tmp_path):
    fake, repo, cid = two_step(tmp_path, fw.reviewer_pass(commit="abcdef1234567"))

    assert fake.runs(cid)[-1]["metadata"]["commit"] == "abcdef1234567"


def test_reviewer_changes_sends_the_card_back_with_the_required_changes_and_the_reviewed_commit(tmp_path):
    fake, repo, cid = two_step(tmp_path, fw.reviewer_changes(["add a test", "handle None"]))

    head = git(repo, "rev-parse", "swarm/T1-coder")
    shown = fake.card(cid)
    assert (shown["status"], shown["assignee"]) == ("ready", "coder-1")
    run = shown["_runs"][-1]
    assert (run["profile"], run["outcome"], run["metadata"]) == ("reviewer", "changes_requested", None)
    assert run["summary"].splitlines() == [
        "CHANGES_REQUIRED: the change does not meet the acceptance criteria.", f"Reviewed commit: {head}",
        "1. add a test", "2. handle None"]


def test_reviewer_wrong_commit_passes_a_commit_that_is_not_the_head_of_the_branch(tmp_path):
    fake, repo, cid = two_step(tmp_path, fw.reviewer_wrong_commit())

    head = git(repo, "rev-parse", "swarm/T1-coder")
    metadata = fake.runs(cid)[-1]["metadata"]
    verdict = review.validate_verdict(metadata)
    assert verdict.valid and verdict.outcome == "PASS"  # a well formed approval ...
    assert metadata["commit"] == git(repo, "rev-parse", "swarm/T1-coder~1") != head
    assert not review.verdict_matches_head(verdict, head)  # ... of the wrong commit


# ---------------------------------------------------------------------------------------------
# The fake provider additions
# ---------------------------------------------------------------------------------------------


def _post(base_url, body=b"{}", *, headers=None, timeout=5, path="/v1/chat/completions", method="POST"):
    request = urllib.request.Request(
        f"{base_url}{path}", data=body if method == "POST" else None, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def test_tool_call_response_is_a_chat_completion_in_which_the_model_calls_a_tool():
    with fp.FakeProvider() as provider:
        provider.enqueue(fp.tool_call_response("terminal", {"command": "curl http://example.invalid"}))

        status, _, body = _post(provider.base_url)

    assert status == 200
    choice = json.loads(body)["choices"][0]
    assert choice["finish_reason"] == "tool_calls" and choice["message"]["content"] is None
    (call,) = choice["message"]["tool_calls"]
    assert (call["id"], call["type"], call["function"]["name"]) == ("call_fake_1", "function", "terminal")
    assert json.loads(call["function"]["arguments"]) == {"command": "curl http://example.invalid"}


def test_several_tool_calls_and_string_arguments_are_sent_as_given():
    response = fp.tool_calls_response([("read_file", {"path": "a.py"}), ("write_file", '{"path": "b.py"}')],
                                      content="working on it", model="m-1")

    message = response.body_json["choices"][0]["message"]
    assert response.body_json["model"] == "m-1" and message["content"] == "working on it"
    assert [(c["id"], c["function"]["name"]) for c in message["tool_calls"]] == [
        ("call_fake_1", "read_file"), ("call_fake_2", "write_file")]
    assert message["tool_calls"][1]["function"]["arguments"] == '{"path": "b.py"}'


def test_a_slow_response_waits_and_a_client_with_a_shorter_timeout_gives_up():
    with fp.FakeProvider() as provider:
        provider.enqueue(fp.slow_response(0.4))
        started = time.monotonic()
        status, _, body = _post(provider.base_url)
        elapsed = time.monotonic() - started
        provider.enqueue(fp.slow_response(0.5, fp.server_error(503)))
        with pytest.raises((TimeoutError, urllib.error.URLError)):
            _post(provider.base_url, timeout=0.1)
        time.sleep(0.5)  # let the slow handler finish before the server is stopped

    assert status == 200 and elapsed >= 0.35 and json.loads(body)["choices"][0]["message"]["content"] == "ok"


def test_a_dropped_connection_gives_the_client_no_response_and_the_server_carries_on():
    with fp.FakeProvider() as provider:
        provider.enqueue(fp.connection_drop())

        with pytest.raises((urllib.error.URLError, ConnectionError, http.client.HTTPException)):
            _post(provider.base_url)
        status, _, _ = _post(provider.base_url)

        assert status == 200 and provider.request_count == 2  # the dropped request was recorded and then answered nothing


def test_the_status_conveniences_are_the_blueprints_cases():
    limited, denied, malformed = fp.rate_limit(3), fp.unauthorized(), fp.malformed_json()

    assert (limited.status, limited.headers["Retry-After"]) == (429, "3")
    assert denied.status == 401 and fp.server_error(503).status == 503
    assert malformed.status == 200 and malformed.render() == b"{not json"
    assert fp.rate_limit().headers["Retry-After"] == "1"


def test_the_request_log_keeps_every_body_and_redacts_credential_headers():
    with fp.FakeProvider() as provider:
        _post(provider.base_url, b'{"messages": [{"role": "user", "content": "hello"}]}',
              headers={"Authorization": "Bearer nvapi-planted-credential-0123456789", "X-Api-Key": "shh",
                       "X-Trace": "trace-1", "Content-Type": "application/json"})
        _post(provider.base_url, b"not json at all")
        _post(provider.base_url, path="/v1/models", method="GET")
        first, second, third = provider.requests

    assert first["path"] == "/v1/chat/completions" and first["method"] == "POST"
    assert first["json"]["messages"][0]["content"] == "hello" and first["body"].startswith(b'{"messages"')
    lowered = {name.lower(): value for name, value in first["headers"].items()}
    assert (lowered["authorization"], lowered["x-api-key"], lowered["x-trace"]) == ("[redacted]", "[redacted]", "trace-1")
    assert "nvapi-planted-credential" not in json.dumps(first["headers"])
    assert (second["json"], second["body"]) == (None, b"not json at all")
    assert (third["method"], third["body"], third["json"]) == ("GET", b"", None)


def test_assert_never_received_passes_a_clean_run_and_names_the_leak_without_printing_it():
    leaked, planted = "PLANTED-SECRET-VALUE-12345", "another-planted-secret-98765"
    with fp.FakeProvider() as provider:
        _post(provider.base_url, json.dumps({"messages": [{"content": "harmless"}]}).encode())
        provider.assert_never_received([leaked, planted])  # nothing planted has arrived yet
        provider.assert_never_received(leaked)
        _post(provider.base_url, json.dumps({"messages": [{"content": f"the key is {planted}"}]}).encode())

        with pytest.raises(AssertionError) as excinfo:
            provider.assert_never_received([leaked, planted])

    message = str(excinfo.value)
    assert "planted secret #1" in message and f"length {len(planted)}" in message and "request #1" in message
    assert leaked not in message and planted not in message  # the failure never becomes a second leak


def test_assert_never_received_finds_a_secret_a_json_escape_hides_and_one_in_the_url_and_bytes():
    escaped = "caf" + chr(0xE9) + "-key-777"  # sent as a JSON escape (ensure_ascii), so the raw bytes do not contain it
    with fp.FakeProvider() as provider:
        _post(provider.base_url, json.dumps({"content": escaped}).encode("ascii"))
        assert escaped.encode("utf-8") not in provider.requests[0]["body"]
        with pytest.raises(AssertionError, match="planted secret #0"):
            provider.assert_never_received(escaped)

    with fp.FakeProvider() as provider:
        _post(provider.base_url, path="/v1/models?key=in-the-url-4321", method="GET")
        with pytest.raises(AssertionError, match="planted secret #0"):
            provider.assert_never_received(b"in-the-url-4321")
        with pytest.raises(ValueError, match="empty secret"):
            provider.assert_never_received("")
        provider.assert_never_received([])


def test_the_provider_serves_its_models_on_get_and_a_scripted_response_still_wins():
    with fp.FakeProvider(models=("model-a", "model-b")) as provider:
        status, _, body = _post(provider.base_url, path="/v1/models", method="GET")
        provider.enqueue(fp.server_error(502))
        scripted, _, _ = _post(provider.base_url, path="/v1/models", method="GET")
        _, _, completion = _post(provider.base_url)

    assert status == 200 and [m["id"] for m in json.loads(body)["data"]] == ["model-a", "model-b"]
    assert scripted == 502 and json.loads(completion)["model"] == "model-a"


def test_a_hermes_endpoint_config_fragment_points_a_profile_at_the_running_provider():
    provider = fp.FakeProvider()
    with pytest.raises(RuntimeError, match="start"):
        provider.hermes_endpoint_config()
    with provider:
        config = provider.hermes_endpoint_config("test-model")
        entry = config["providers"]["ases-fake"]

        assert config["model"] == {"default": "test-model", "provider": "ases-fake"}
        assert entry["api"] == f"{provider.base_url}/v1" and entry["default_model"] == "test-model"
        assert entry["context_length"] >= 65536  # Hermes rejects a custom endpoint declared under 64K
        assert provider.hermes_endpoint_config(provider_name="other", context_length=131072)["providers"]["other"][
            "context_length"] == 131072


# ---------------------------------------------------------------------------------------------
# Edges: refusals, workspaces, the review lane's reserved slot, and the paths the first tests did not reach
# ---------------------------------------------------------------------------------------------


def test_more_create_refusals_and_the_wrapper_leaves_falsy_options_out():
    fake = new_fake()

    refuses(lambda: create(fake, "x", assignee="   "), "profile name cannot be empty")
    refuses(lambda: create(fake, "x", max_runtime="xm"), "malformed duration", code=2)
    refuses(lambda: create(fake, "x", workspace="dir:"), "requires a path after the colon", code=2)
    refuses(lambda: create(fake, "x", workspace="worktree", branch="-b"), "must not start with '-'", code=2)
    card = create(fake, "x", body="", branch="", idempotency_key="", project="")  # hermes.py omits every falsy option

    assert (card["body"], card["branch_name"], card["project_id"]) == (None, None, None)
    assert create(fake, "y", max_runtime="90")["id"] != card["id"]  # a bare number is seconds
    with pytest.raises(KeyError):
        fake.card("t_nope")
    refuses(lambda: fake.kanban_show(BOARD, "t_nope"), "no such task: t_nope")


def test_complete_metadata_must_be_a_json_object_and_a_worker_can_complete_a_card_it_never_ran():
    fake = new_fake()
    card = create(fake, "T1: work", assignee="c")

    refuses(lambda: fake.kanban_complete(BOARD, card["id"], metadata=["not", "an", "object"]),
            "--metadata: must be a JSON object", code=2)
    with pytest.raises(AgentToolError, match="metadata must be an object"):
        fake.agent_complete(card["id"], summary="x", metadata="text")
    with pytest.raises(AgentToolError, match="comment author is required"):
        fake.agent_comment(card["id"], "x", author=" ")
    fake.agent_complete(card["id"], summary="done by hand")  # no run to own: no session id is stamped

    shown = fake.card(card["id"])
    assert shown["status"] == "done" and shown["_runs"][0]["metadata"] is None and shown["_runs"][0]["profile"] == "c"


def test_approving_a_card_in_review_by_hand_records_the_review_approved_note():
    fake = new_fake()
    fake.register_worker("c", hand_off)
    fake.register_worker("reviewer", hang)
    card = create(fake, "T1: work", assignee="c")
    fake.kanban_dispatch(BOARD)
    assert fake.card(card["id"])["status"] == "review"

    fake.kanban_complete(BOARD, card["id"])  # a human approval: no result, no summary, no metadata

    shown = fake.card(card["id"])
    assert shown["status"] == "done"
    approval = shown["_runs"][-1]
    assert (approval["outcome"], approval["summary"], approval["metadata"]) == (
        "completed", "Review approved without additional evidence.", {"source_status": "review", "approval": "manual"})
    assert shown["_events"][-1]["payload"]["summary"] == "Review approved without additional evidence."


def test_agent_request_changes_refuses_a_blank_reason_a_stale_run_and_a_run_that_did_not_come_from_review():
    fake = new_fake()
    fake.register_worker("c", hang)
    card = create(fake, "T1: work", assignee="c")
    fake.kanban_dispatch(BOARD)
    run_id = fake.runs(card["id"])[0]["id"]

    with pytest.raises(AgentToolError, match="reason is required"):
        fake.agent_request_changes(card["id"], "  ", run_id=run_id)
    with pytest.raises(AgentToolError, match="run_id mismatch"):
        fake.agent_request_changes(card["id"], "redo it", run_id=run_id + 5)
    with pytest.raises(AgentToolError, match="active run was not claimed from review"):
        fake.agent_request_changes(card["id"], "redo it", run_id=run_id)  # an implementer's run, not a reviewer's
    with pytest.raises(AgentToolError, match="task is not in an active review run"):
        fake.agent_request_changes(create(fake, "idle", assignee="c")["id"], "redo it")


def test_a_worker_cannot_hand_off_or_complete_once_a_parent_it_depends_on_is_open_again():
    fake = new_fake()
    fake.register_worker("c", hang)
    card = create(fake, "T1: work", assignee="c")
    fake.kanban_dispatch(BOARD)
    reopened = create(fake, "T0: the parent that was reopened")
    fake.kanban_link(BOARD, reopened["id"], card["id"])  # a running child is not demoted, but its parent is open again

    with pytest.raises(AgentToolError, match="parent dependencies are not satisfied"):
        fake.agent_request_review(card["id"], summary="done")
    with pytest.raises(AgentToolError, match="could not complete"):
        fake.agent_complete(card["id"], summary="done")
    assert fake.card(card["id"])["status"] == "running"


def test_agent_hang_and_kill_worker_need_a_live_worker():
    fake = new_fake()
    card = create(fake, "T1: work", assignee="c")

    with pytest.raises(AgentToolError, match="no live worker process to hang"):
        fake.agent_hang(card["id"])
    with pytest.raises(KeyError, match="no worker process to kill"):
        fake.kill_worker(card["id"])


def test_a_worker_that_exits_75_is_a_quota_wall_booked_at_the_next_reclaim_and_never_a_failure():
    fake = new_fake()
    fake.register_worker("c", hang)
    card = create(fake, "T1: work", assignee="c")
    fake.kanban_dispatch(BOARD)
    fake.kill_worker(card["id"], exit_code=75)
    fake.tick(30)

    shown = fake.card(card["id"])
    assert shown["status"] == "ready" and fake.snapshot()["tasks"][card["id"]]["consecutive_failures"] == 0
    (run,) = shown["_runs"]
    assert (run["outcome"], run["status"]) == ("rate_limited", "rate_limited")
    assert run["error"] == f"pid {FAKE_PID_BASE + 1} exited rate-limited (quota wall) - requeued without counting a failure"
    assert fake.events(card["id"], "rate_limited")[0]["payload"]["exit_code"] == 75
    fake.rate_limit_cooldown_seconds = 0  # the cooldown can be switched off, as HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS=0
    assert len(fake.kanban_dispatch(BOARD)["spawned"]) == 1


def test_a_worker_alive_past_a_stale_heartbeat_is_reclaimed_anyway():
    """A live PID whose last heartbeat is over an hour old is wedged: Hermes reclaims it even though it is alive."""
    fake = new_fake()
    fake.claim_ttl_seconds = 60
    fake.register_worker("c", fw.ScriptedWorker([fw.Heartbeat("alive"), fw.Timeout()]))
    card = create(fake, "T1: work", assignee="c")
    fake.kanban_dispatch(BOARD)

    fake.tick(3700)

    shown = fake.card(card["id"])
    assert shown["status"] == "ready" and fake.live_workers() == []
    assert shown["_runs"][0]["error"] == "stale_lock=fake-host:1"
    payload = fake.events(card["id"], "reclaimed")[0]["payload"]
    assert (payload["heartbeat_stale"], payload["last_heartbeat_at"]) == (True, 1_000_000)


def test_unblocking_a_card_with_no_lifecycle_event_resumes_it_in_ready():
    fake = new_fake()
    fake.initial_block_event = False
    merge = create(fake, "T1: merge", initial_status="blocked")
    assert kinds(fake, merge["id"]) == ["created"]

    fake.kanban_unblock(BOARD, merge["id"])

    assert fake.card(merge["id"])["status"] == "ready" and fake.events(merge["id"], "unblocked")[0]["payload"] is None


def test_a_ready_card_whose_parent_is_not_done_is_demoted_at_claim_time_and_a_review_card_too():
    """kanban_db.claim_task is the single enforcement point: never ready to running under an undone parent. White-box: such
    a card arises from a race or a dashboard drag, so the test puts the state there directly."""
    fake = new_fake()
    fake.register_worker("c", finish)
    parent = create(fake, "parent")
    child = create(fake, "child", assignee="c", parent=[parent["id"]])
    fake._tasks[child["id"]].status = "ready"

    result = fake.kanban_dispatch(BOARD)

    assert result["spawned"] == [] and fake.card(child["id"])["status"] == "todo"
    assert fake.events(child["id"], "claim_rejected")[0]["payload"] == {"reason": "parents_not_done"}

    fake.register_worker("coder-1", hand_off)
    fake.register_worker("reviewer", finish)
    reviewed = create(fake, "reviewed", assignee="coder-1")
    fake.kanban_dispatch(BOARD)  # hands off: review
    fake.kanban_link(BOARD, parent["id"], reviewed["id"])  # a parent that reopened meanwhile
    fake.kanban_dispatch(BOARD)
    assert fake.card(reviewed["id"])["status"] == "todo"
    assert fake.events(reviewed["id"], "dependency_wait")[-1]["payload"] == {
        "reason": "parent_reopened", "source_status": "review"}


def test_a_recent_completed_run_holds_a_card_unless_something_requeued_it_since():
    """The respawn guard's `recent_success` rule. White-box: a `done` card put back in `ready` (a dashboard drag)."""
    fake = new_fake()
    fake.register_worker("c", finish)
    card = create(fake, "T1: work", assignee="c")
    fake.kanban_dispatch(BOARD)
    assert fake.card(card["id"])["status"] == "done"
    fake._tasks[card["id"]].status = "ready"  # put back with no event: the guard reads it as a duplicate run

    held = fake.kanban_dispatch(BOARD)
    assert held["respawn_guarded"] == [{"task_id": card["id"], "reason": "recent_success"}] and held["spawned"] == []

    fake.tick(1)
    fake._event(card["id"], "promoted", None)  # an explicit re-queue after the success is a deliberate re-run
    assert len(fake.kanban_dispatch(BOARD)["spawned"]) == 1


def test_the_review_lane_keeps_one_slot_when_the_budget_is_down_to_one():
    """kanban_db_dispatch: with a spawnable review card waiting and a budget of one, the ready lane is held back so a
    sustained ready backlog cannot starve reviews."""
    fake = new_fake()
    fake.max_in_progress = 2
    fake.register_worker("busy", hang)
    fake.register_worker("coder-1", hand_off)
    fake.register_worker("reviewer", finish)
    fake.register_worker("late", hang)
    busy = create(fake, "busy", assignee="busy")
    reviewed = create(fake, "to review", assignee="coder-1")
    fake.kanban_dispatch(BOARD)  # busy runs; the coder hands its card off
    late = create(fake, "late arrival", assignee="late")
    assert fake.card(reviewed["id"])["status"] == "review" and fake.card(busy["id"])["status"] == "running"

    result = fake.kanban_dispatch(BOARD)  # one slot left: it goes to the reviewer, not to the ready card

    assert [s["assignee"] for s in result["spawned"]] == ["reviewer"]
    assert fake.card(late["id"])["status"] == "ready" and fake.card(reviewed["id"])["status"] == "done"


def test_a_review_card_with_no_assignee_is_reported_unassigned():
    fake = new_fake()
    fake.register_worker("coder-1", lambda f, card, run, ws: f.agent_request_review(
        card["id"], summary="done", reviewer=None, run_id=run["id"]))
    card = create(fake, "T1: work", assignee="coder-1")
    fake.kanban_dispatch(BOARD)
    fake._tasks[card["id"]].assignee = None

    assert fake.kanban_dispatch(BOARD)["skipped_unassigned"] == [card["id"]]


def test_dir_and_scratch_workspaces_are_directories_the_worker_is_handed(tmp_path):
    seen = {}

    def worker(f, card, run, workspace):
        seen[card["title"]] = workspace
        finish(f, card, run, workspace)

    fake = new_fake(scratch_root=tmp_path / "scratch")
    fake.max_in_progress_per_profile = None
    fake.register_worker("c", worker)
    shared = tmp_path / "shared"
    directory = create(fake, "dir card", assignee="c", workspace=f"dir:{shared}")
    scratch = create(fake, "scratch card", assignee="c")

    fake.kanban_dispatch(BOARD)

    assert seen["dir card"] == shared and shared.is_dir()
    assert seen["scratch card"] == tmp_path / "scratch" / scratch["id"] and seen["scratch card"].is_dir()
    assert fake.card(directory["id"])["workspace_path"] == str(shared)
    assert fake.card(directory["id"])["workspace_kind"] == "dir"


def test_a_worker_of_a_card_with_no_workspace_that_touches_files_fails_loudly():
    fake = new_fake()  # no repo, no scratch root: a scratch card has nowhere to write
    fake.register_worker("c", fw.ScriptedWorker([fw.Write("a.txt", "x\n")]))
    create(fake, "T1: work", assignee="c")

    with pytest.raises(RuntimeError, match="has no workspace"):
        fake.kanban_dispatch(BOARD)
    assert fake.live_workers() == []  # the failed worker's process is dead


def test_a_second_workspace_failure_gives_the_card_up_and_reports_it():
    fake = new_fake()
    fake.register_worker("c", finish)
    card = create(fake, "T1: work", assignee="c", workspace="worktree", branch="swarm/T1-coder")
    fake.kanban_dispatch(BOARD)

    second = fake.kanban_dispatch(BOARD)

    assert second["auto_blocked"] == [card["id"]] and fake.card(card["id"])["status"] == "blocked"


def test_a_reviewer_with_no_worktree_names_the_commit_the_coder_handed_off_and_fails_without_one():
    fake = new_fake()  # scratch cards: no worktree to look at
    fake.register_worker("c", fw.ScriptedWorker([fw.RequestReview("done", metadata={"commit_sha": "abcdef1234567"})]))
    fake.register_worker("reviewer", fw.reviewer_pass())
    card = create(fake, "T1: work", assignee="c")
    fake.kanban_dispatch(BOARD)
    fake.kanban_dispatch(BOARD)

    assert fake.runs(card["id"])[-1]["metadata"]["commit"] == "abcdef1234567"

    bare = new_fake()
    bare.register_worker("c", fw.ScriptedWorker([fw.RequestReview("done", metadata={"note": "no commit named"})]))
    bare.register_worker("reviewer", fw.reviewer_pass())
    create(bare, "T1: work", assignee="c")
    bare.kanban_dispatch(BOARD)
    with pytest.raises(RuntimeError, match="no worktree and no hand-off commit"):
        bare.kanban_dispatch(BOARD)


def test_a_git_failure_in_a_worker_step_is_an_error_and_the_request_changes_and_crasher_then_paths_run(tmp_path):
    fake, repo, cid = dispatch_one(tmp_path, fw.ScriptedWorker([fw.Complete("done")]))
    ctx = fw.WorkerContext(fake, fake.card(cid), fake.runs(cid)[0], fake.worktree(cid))
    with pytest.raises(RuntimeError, match="git no-such-subcommand failed"):
        ctx.git("no-such-subcommand")
    assert ctx.git("no-such-subcommand", check=False) == ""

    fake = new_fake()
    fake.register_worker("c", hand_off)
    fake.register_worker("reviewer", fw.ScriptedWorker([fw.RequestChanges("please add a test")]))
    card = create(fake, "T1: work", assignee="c")
    fake.kanban_dispatch(BOARD)
    fake.kanban_dispatch(BOARD)
    assert fake.card(card["id"])["status"] == "ready" and fake.runs(card["id"])[-1]["summary"] == "please add a test"

    seen = []

    def then(f, c, r, w):
        seen.append(c["id"])
        finish(f, c, r, w)

    fake = new_fake()
    fake.register_worker("c", fw.crasher(1, then=then))
    other = create(fake, "T1: work", assignee="c")
    fake.kanban_dispatch(BOARD)
    fake.kanban_dispatch(BOARD)
    assert seen == [other["id"]] and fake.card(other["id"])["status"] == "done"


def test_the_skip_marker_persona_needs_a_test_function_to_mark(tmp_path):
    with pytest.raises(ValueError, match="no test function to put a skip marker on"):
        dispatch_one(tmp_path, fw.tampering_coder("skip_marker", path="README.md"), files=SEED)


def test_write_and_commit_and_write_files_build_the_steps_a_script_starts_with():
    steps = fw.write_and_commit({"a.py": "x\n", "b.py": "y\n"}, "two files")

    assert steps == [fw.Write("a.py", "x\n"), fw.Write("b.py", "y\n"), fw.Commit("two files")]
    assert fw.write_files({"a.py": "x\n"}) == [fw.Write("a.py", "x\n")]
    with pytest.raises(NotImplementedError):
        fw.Step().run(None)
