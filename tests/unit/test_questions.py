"""questions.py: the human channel, swarm questions and swarm answer (ASES-REC-05, ASES-SEC-01).

The five hermes kanban functions it uses are faked over an in-memory board and the database is a temp sqlite file
with plan_tasks rows inserted directly, so nothing here touches a real board, a real card or a provider. The board
behaves like the real Hermes 0.21.3 where this module depends on it (read from kanban_db.py and kanban.py,
2026-09-21): a block only takes a card that is ready or running (on any other card the CLI adds its "BLOCKED:"
comment and exits 1), a second block of the same kind after an unblock sends the card to `triage` with a
`block_loop_detected` event, and an unblock adds an "UNBLOCK:" comment and an `unblocked` event."""
import copy
import dataclasses
import json
import subprocess
import types

import pytest

from ases import db, events, hermes, plan as plan_mod, questions, reviewcontract
from ases.questions import OpenQuestion, Question, QuestionError

ROLES = {"lead": "lead", "coder": "coder-1", "reviewer": "reviewer"}
SECRET = "sk-abcdefghijklmnopqrstuvwx"


def _plan(*keys, project="p1"):
    return plan_mod.parse_and_validate({
        "project": project,
        "integration_branch": "integration",
        "gate_profiles": {"trivial": ["echo ok"]},
        "tasks": [
            {"key": key, "title": "task", "role": "coder", "depends_on": [], "touches": [f"f{i}.py"],
             "acceptance": ["exists"], "gate_profile": "trivial", "estimated_requests": 5}
            for i, key in enumerate(keys)
        ],
    }, known_roles=set(ROLES), max_cards=40)


PLAN = _plan("T1", "T2")


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "ases.db")


def _seed_task(conn, key, work_card_id, merge_card_id, project="p1"):
    conn.execute(
        "INSERT INTO plan_tasks (project, task_key, work_card_id, merge_card_id, role, created_at) "
        "VALUES (?, ?, ?, ?, 'coder', datetime('now'))",
        (project, key, work_card_id, merge_card_id),
    )


def _blocked(reason, at=1000):
    """A `blocked` event as hermes.kanban_show returns it under "_events"."""
    return {"kind": "blocked", "payload": {"reason": reason}, "created_at": at, "run_id": 1}


def _created(at=1):
    return {"kind": "created", "payload": {}, "created_at": at, "run_id": None}


def _gave_up(failures=3, error="pid 4242 exited with code 1", at=1000):
    """The `gave_up` event Hermes's dispatcher writes when its circuit breaker trips (and NO `blocked` event)."""
    return {"kind": "gave_up", "run_id": None, "created_at": at, "payload": {
        "failures": failures, "effective_limit": 3, "limit_source": "default", "error": error,
        "trigger_outcome": "crashed", "retry_status": "ready",
    }}


def _loop(reason, at=1000):
    """The `block_loop_detected` event a repeated block writes when it sends a card to triage."""
    return {"kind": "block_loop_detected", "run_id": None, "created_at": at, "payload": {
        "reason": reason, "kind": "needs_input", "recurrences": 2, "source_status": "ready", "limit": 2,
    }}


def _unblocked(at=2000):
    return {"kind": "unblocked", "payload": None, "created_at": at, "run_id": None}


def _asked(text, at=1000, author="ases"):
    """The comment ask_user writes on a card Hermes will not block."""
    return {"author": author, "body": f"ASES QUESTION: {text}", "created_at": at}


def _answer(text="use sqlite", at=2000, author="user", prefix="ANSWER:"):
    return {"author": author, "body": f"{prefix} {text}", "created_at": at}


def _card(card_id, title, *, status="blocked", events=(), parents=(), assignee="coder-1", comments=()):
    """One card as hermes.kanban_show returns it (the flat task plus the underscore keys)."""
    return {
        "id": card_id, "title": title, "status": status, "assignee": assignee,
        "_events": list(events), "_parents": list(parents), "_children": [], "_runs": [],
        "_comments": list(comments), "_latest_summary": None,
    }


class FakeBoard:
    """hermes.kanban_list, kanban_show, kanban_block, kanban_comment and kanban_unblock over a dict of cards,
    installed with monkeypatch. `calls` records every call in order. Everything a call writes is stamped from a
    clock that starts after any time a test seeds (`clock`), so a later write is a newer one.

    It behaves as Hermes does: a comment is appended to the card; an unblock adds an "UNBLOCK: <reason>" comment
    (when it has a reason) and an `unblocked` event and moves the card to ready; a block adds "BLOCKED: <reason>"
    and then, on a `ready` or `running` card, blocks it with a `blocked` event whose payload carries the reason and
    the kind, but on any other card (or with `refuse_block`) exits 1 ("cannot block"); and a second block of the
    same kind on one card sends it to `triage` with a `block_loop_detected` event instead.

    Fill `unreadable`, `stale`, `comment_error`, `unblock_error` or `block_error` to make Hermes misbehave:
    kanban_show raises for an unreadable id, kanban_list reports a stale id under every status whatever it is now,
    and the write functions raise the given error (block_error before it writes anything)."""

    def __init__(self, monkeypatch, *cards):
        self.cards = {card["id"]: card for card in cards}
        self.calls = []
        self.unreadable = set()
        self.stale = set()
        self.comment_error = None
        self.unblock_error = None
        self.block_error = None
        self.refuse_block = False
        self.clock = 5000
        self._recurrences: dict[tuple[str, str | None], int] = {}
        for name in ("kanban_list", "kanban_show", "kanban_comment", "kanban_unblock", "kanban_block"):
            monkeypatch.setattr(hermes, name, getattr(self, name))

    def _tick(self):
        self.clock += 1
        return self.clock

    def _event(self, card, kind, payload):
        card["_events"].append({"kind": kind, "payload": payload, "created_at": self._tick(), "run_id": None})

    def _comment(self, card, author, body):
        card["_comments"].append({"author": author, "body": body, "created_at": self._tick()})

    def kanban_list(self, board, *, status=None, assignee=None):
        self.calls.append(("list", status))
        return [
            {"id": c["id"], "title": c["title"], "status": c["status"], "assignee": c["assignee"]}
            for c in self.cards.values()
            if status is None or c["status"] == status or c["id"] in self.stale
        ]

    def kanban_show(self, board, card_id):
        self.calls.append(("show", card_id))
        if card_id in self.unreadable or card_id not in self.cards:
            raise hermes.HermesCommandError(["kanban", "show", card_id], 1, "no such card")
        return copy.deepcopy(self.cards[card_id])

    def kanban_comment(self, board, card_id, text, *, author=None):
        self.calls.append(("comment", card_id, text, author))
        if self.comment_error:
            raise self.comment_error
        self._comment(self.cards[card_id], author or "default", text)

    def kanban_unblock(self, board, card_id, reason=None):
        self.calls.append(("unblock", card_id, reason))
        if self.unblock_error:
            raise self.unblock_error
        card = self.cards[card_id]
        if reason:
            self._comment(card, "default", f"UNBLOCK: {reason}")
        card["status"] = "ready"
        self._event(card, "unblocked", None)

    def kanban_block(self, board, card_id, reason, *, kind=None):
        self.calls.append(("block", card_id, reason, kind))
        if self.block_error:
            raise self.block_error
        card = self.cards[card_id]
        self._comment(card, "default", f"BLOCKED: {reason}")            # the CLI comments first, whatever comes next
        if self.refuse_block or card["status"] not in ("ready", "running"):
            raise hermes.HermesCommandError(["kanban", "block", card_id], 1, f"cannot block {card_id}")
        recurrences = self._recurrences[(card_id, kind)] = self._recurrences.get((card_id, kind), 0) + 1
        payload = {"reason": reason, "kind": kind, "recurrences": recurrences, "source_status": card["status"]}
        if recurrences >= 2:
            card["status"] = "triage"
            self._event(card, "block_loop_detected", {**payload, "limit": 2})
        else:
            card["status"] = "blocked"
            self._event(card, "blocked", payload)

    def names(self):
        return [call[0] for call in self.calls]


def _list(conn, plan=PLAN):
    return questions.list_questions("b", plan, conn=conn)


def _payloads(conn, kind):
    return [json.loads(row["payload"]) for row in events.recent(conn, limit=100) if row["kind"] == kind]


def _hermes_error(name="comment"):
    return hermes.HermesCommandError(["kanban", name, "w_1"], 1, "hermes refused")


# ---------------------------------------------------------------------------------------------------------
# list_questions: which cards are questions
# ---------------------------------------------------------------------------------------------------------


def test_a_blocked_plan_work_card_with_a_blocked_event_is_listed_with_the_right_fields(conn, monkeypatch):
    _seed_task(conn, "T1", "w_1", "m_1")
    board = FakeBoard(monkeypatch, _card("w_1", "T1: scaffold", events=[_created(), _blocked("Which database?", 1111)]))

    result = _list(conn)

    assert result == [Question(
        card_id="w_1", title="T1: scaffold", task_key="T1", card_kind="work", assignee="coder-1",
        question="Which database?", asked_at=1111, source="blocked",
    )]
    assert board.calls == [("list", "blocked"), ("list", "triage"), ("show", "w_1")]   # both lanes, then each card


def test_a_merge_card_the_controller_blocked_with_a_reason_is_listed_as_kind_merge(conn, monkeypatch):
    _seed_task(conn, "T1", "w_1", "m_1")
    reason = "fix-card budget (2) exhausted for T1; needs a human decision. Last failure: red gate 3"
    FakeBoard(monkeypatch, _card("m_1", "T1: merge", assignee=None, events=[_blocked(reason, 2000)]))

    (question,) = _list(conn)

    assert (question.card_id, question.card_kind, question.task_key) == ("m_1", "merge", "T1")
    assert question.assignee is None
    assert question.question == reason


def test_a_merge_card_that_is_only_waiting_is_not_a_question(conn, monkeypatch):
    # The controller creates every merge card blocked, with no `blocked` event: it is waiting, not asking.
    _seed_task(conn, "T1", "w_1", "m_1")
    FakeBoard(monkeypatch, _card("m_1", "T1: merge", assignee=None, events=[_created()]))

    assert _list(conn) == []


def test_a_blocked_card_of_another_project_that_reuses_the_task_key_is_not_listed(conn, monkeypatch):
    _seed_task(conn, "T1", "w_1", "m_1")
    _seed_task(conn, "T1", "o_w", "o_m", project="other")
    board = FakeBoard(
        monkeypatch,
        _card("w_1", "T1: scaffold", events=[_blocked("mine?")]),
        _card("o_w", "T1: scaffold", events=[_blocked("theirs?")]),
        _card("o_m", "T1: merge", assignee=None, events=[_blocked("theirs too?")]),
    )

    result = _list(conn)

    assert [q.card_id for q in result] == ["w_1"]
    assert ("show", "o_w") not in board.calls and ("show", "o_m") not in board.calls  # not even read


def test_the_other_projects_own_plan_lists_its_own_cards_and_not_ours(conn, monkeypatch):
    _seed_task(conn, "T1", "w_1", "m_1")
    _seed_task(conn, "T1", "o_w", "o_m", project="other")
    FakeBoard(
        monkeypatch,
        _card("w_1", "T1: scaffold", events=[_blocked("mine?")]),
        _card("o_w", "T1: scaffold", events=[_blocked("theirs?")]),
    )

    result = _list(conn, _plan("T1", project="other"))

    assert [(q.card_id, q.question) for q in result] == [("o_w", "theirs?")]


def test_a_fix_card_under_the_work_card_is_listed_as_kind_fix(conn, monkeypatch):
    _seed_task(conn, "T1", "w_1", "m_1")
    FakeBoard(monkeypatch, _card("f_1", "T1: fix (round 1)", parents=["w_1"], events=[_blocked("which way?")]))

    (question,) = _list(conn)

    assert (question.card_id, question.card_kind, question.task_key) == ("f_1", "fix", "T1")


def test_a_fix_card_that_is_now_the_tasks_work_card_is_still_kind_fix(conn, monkeypatch):
    # process_merge_queue repoints plan_tasks.work_card_id at each fix card it opens, so in a live run the fix
    # card is found by its id. It must still read as a fix, not as the task's plain work card.
    _seed_task(conn, "T1", "f_1", "m_1")
    FakeBoard(monkeypatch, _card("f_1", "T1: fix (round 1)", parents=["w_1"], events=[_blocked("which way?")]))

    (question,) = _list(conn)

    assert (question.card_id, question.card_kind, question.task_key) == ("f_1", "fix", "T1")


def test_a_card_is_found_by_a_parent_that_is_a_plan_card_whatever_its_title(conn, monkeypatch):
    _seed_task(conn, "T1", "w_1", "m_1")
    FakeBoard(monkeypatch, _card("x_1", "follow-up", parents=["unrelated", "w_1"], events=[_blocked("ok?")]))

    (question,) = _list(conn)

    assert (question.card_id, question.card_kind, question.task_key) == ("x_1", "other", "T1")


def test_a_card_is_found_by_a_title_that_starts_with_a_task_key_of_the_plan(conn, monkeypatch):
    _seed_task(conn, "T1", "w_1", "m_1")
    FakeBoard(
        monkeypatch,
        _card("x_1", "T2: an unrecorded card", events=[_blocked("found by title")]),
        _card("x_2", "T3: a key this plan does not have", events=[_blocked("not ours")]),
        _card("x_3", "Buy milk", events=[_blocked("not ours either")]),
        _card("x_4", "T22: only a prefix of the key", events=[_blocked("no colon after T2")]),
    )

    result = _list(conn)

    assert [(q.card_id, q.task_key, q.card_kind) for q in result] == [("x_1", "T2", "other")]


def test_the_longest_task_key_wins_when_one_key_is_a_prefix_of_another(conn, monkeypatch):
    FakeBoard(
        monkeypatch,
        _card("x_1", "A:B: nested", events=[_blocked("q1", 1)]),
        _card("x_2", "A: plain", events=[_blocked("q2", 2)]),
    )

    result = _list(conn, _plan("A", "A:B"))

    assert [(q.card_id, q.task_key) for q in result] == [("x_1", "A:B"), ("x_2", "A")]


def test_a_card_is_not_taken_by_title_when_a_parent_or_its_own_id_belongs_to_another_project(conn, monkeypatch):
    # Both cards are titled like this plan's cards, and the keys collide across projects, but each is claimed by
    # the other project's rows (one by a parent, one by its own id), so neither is ours.
    _seed_task(conn, "T1", "w_1", "m_1")
    _seed_task(conn, "T1", "o_w", "o_m", project="other")
    board = FakeBoard(
        monkeypatch,
        _card("f_9", "T1: fix (round 1)", parents=["o_w"], events=[_blocked("theirs, by parent")]),
        _card("o_m", "T1: merge", assignee=None, events=[_blocked("theirs, by id")]),
    )

    assert _list(conn) == []
    assert ("show", "o_m") not in board.calls


def test_a_card_blocked_twice_reports_the_latest_reason_and_time(conn, monkeypatch):
    _seed_task(conn, "T1", "w_1", "m_1")
    unblocked = {"kind": "unblocked", "payload": {}, "created_at": 150, "run_id": None}
    FakeBoard(monkeypatch, _card("w_1", "T1: scaffold", events=[
        _blocked("first question", 100), unblocked, _blocked("second question", 200),
    ]))

    (question,) = _list(conn)

    assert (question.question, question.asked_at) == ("second question", 200)


def test_the_latest_blocked_event_is_chosen_by_time_then_by_position(conn, monkeypatch):
    _seed_task(conn, "T1", "w_1", "m_1")
    _seed_task(conn, "T2", "w_2", "m_2")
    FakeBoard(
        monkeypatch,
        # listed out of order: the one with the larger created_at is the latest, wherever it sits
        _card("w_1", "T1: a", events=[_blocked("newest", 200), _blocked("older", 100)]),
        # the same second: the one listed later is the latest
        _card("w_2", "T2: b", events=[_blocked("earlier in the list", 100), _blocked("later in the list", 100)]),
    )

    result = {q.card_id: q.question for q in _list(conn)}

    assert result == {"w_1": "newest", "w_2": "later in the list"}


@pytest.mark.parametrize("events_list", [
    [],
    [_created()],
    [_blocked("")],
    [_blocked("   \n")],
    [{"kind": "blocked", "payload": {}, "created_at": 5, "run_id": 1}],
    [{"kind": "blocked", "payload": None, "created_at": 5, "run_id": 1}],
    [{"kind": "blocked", "payload": {"reason": 42}, "created_at": 5, "run_id": 1}],
    [{"kind": "blocked", "payload": "not json at all", "created_at": 5, "run_id": 1}],
    [_blocked("an older reason", 100), _blocked("", 200)],
], ids=["no-events", "no-blocked-event", "empty-reason", "blank-reason", "no-reason-key", "null-payload",
        "reason-not-text", "payload-not-json", "latest-reason-empty"])
def test_a_blocked_card_without_a_block_reason_is_not_listed(conn, monkeypatch, events_list):
    _seed_task(conn, "T1", "w_1", "m_1")
    FakeBoard(monkeypatch, _card("w_1", "T1: scaffold", events=events_list))

    assert _list(conn) == []


def test_a_payload_that_arrives_as_a_json_string_is_read(conn, monkeypatch):
    _seed_task(conn, "T1", "w_1", "m_1")
    payload = json.dumps({"reason": "from a string payload"})
    event = {"kind": "blocked", "payload": payload, "created_at": 7, "run_id": 1}
    FakeBoard(monkeypatch, _card("w_1", "T1: scaffold", events=[event]))

    (question,) = _list(conn)

    assert question.question == "from a string payload"


def test_the_reason_is_stripped_and_a_missing_time_reads_as_zero(conn, monkeypatch):
    _seed_task(conn, "T1", "w_1", "m_1")
    event = {"kind": "blocked", "payload": {"reason": "  padded \n"}, "run_id": 1}
    FakeBoard(monkeypatch, _card("w_1", "T1: scaffold", events=[event]))

    (question,) = _list(conn)

    assert (question.question, question.asked_at) == ("padded", 0)


@pytest.mark.parametrize("created_at, expected", [
    ("1789832318", 1789832318), (1789832318.9, 1789832318), (None, 0), ("soon", 0), (True, 0), (-5, 0),
    (float("inf"), 0), (float("nan"), 0),
], ids=["numeric-string", "float", "none", "not-a-number", "boolean", "negative", "infinity", "nan"])
def test_created_at_is_read_as_whole_epoch_seconds_and_anything_else_is_zero(conn, monkeypatch, created_at, expected):
    _seed_task(conn, "T1", "w_1", "m_1")
    event = {"kind": "blocked", "payload": {"reason": "q"}, "created_at": created_at, "run_id": 1}
    FakeBoard(monkeypatch, _card("w_1", "T1: scaffold", events=[event]))

    (question,) = _list(conn)

    assert question.asked_at == expected


def test_questions_are_listed_oldest_first_then_by_card_id(conn, monkeypatch):
    _seed_task(conn, "T1", "w_1", "m_1")
    _seed_task(conn, "T2", "w_2", "m_2")
    no_time = {"kind": "blocked", "payload": {"reason": "no time recorded"}, "run_id": 1}
    FakeBoard(
        monkeypatch,
        _card("w_1", "T1: a", events=[_blocked("newest", 300)]),
        _card("w_2", "T2: b", events=[_blocked("tied, larger id", 100)]),
        _card("m_1", "T1: merge", assignee=None, events=[_blocked("tied, smaller id", 100)]),
        _card("m_2", "T2: merge", assignee=None, events=[no_time]),
    )

    assert [q.card_id for q in _list(conn)] == ["m_2", "m_1", "w_2", "w_1"]


def test_a_card_that_cannot_be_read_is_skipped_and_traced_and_the_rest_are_still_listed(conn, monkeypatch):
    _seed_task(conn, "T1", "w_1", "m_1")
    _seed_task(conn, "T2", "w_2", "m_2")
    board = FakeBoard(
        monkeypatch,
        _card("w_1", "T1: a", events=[_blocked("unreadable")]),
        _card("w_2", "T2: b", events=[_blocked("readable")]),
    )
    board.unreadable.add("w_1")

    result = _list(conn)

    assert [q.card_id for q in result] == ["w_2"]
    (traced,) = _payloads(conn, "question_read_failed")
    assert traced["card_id"] == "w_1"
    assert "HermesCommandError" in traced["error"]


def test_a_failure_of_the_list_itself_is_not_swallowed(conn, monkeypatch):
    def broken_list(board, *, status=None, assignee=None):
        raise hermes.HermesCommandError(["kanban", "list"], 1, "hermes is down")

    monkeypatch.setattr(hermes, "kanban_list", broken_list)

    with pytest.raises(hermes.HermesCommandError):
        _list(conn)


def test_a_card_that_moved_on_between_the_list_and_the_show_is_not_listed(conn, monkeypatch):
    _seed_task(conn, "T1", "w_1", "m_1")
    board = FakeBoard(monkeypatch, _card("w_1", "T1: a", status="ready", events=[_blocked("already answered")]))
    board.stale.add("w_1")

    assert _list(conn) == []
    assert ("show", "w_1") in board.calls  # it was read, and found no longer blocked


def test_a_card_missing_from_the_show_still_gets_its_title_and_assignee_from_the_list(conn, monkeypatch):
    _seed_task(conn, "T1", "w_1", "m_1")
    FakeBoard(monkeypatch, _card("w_1", "T1: from the list", assignee="coder-1", events=[_blocked("q")]))
    shown = {"_events": [_blocked("q")], "_parents": []}  # a show with no id, title, assignee or status
    monkeypatch.setattr(hermes, "kanban_show", lambda board, card_id: dict(shown))

    (question,) = _list(conn)

    assert (question.title, question.assignee, question.card_kind) == ("T1: from the list", "coder-1", "work")


def test_events_that_are_not_dicts_are_ignored_and_the_real_block_is_still_found(conn, monkeypatch):
    _seed_task(conn, "T1", "w_1", "m_1")
    FakeBoard(monkeypatch, _card("w_1", "T1: a", events=[None, "junk", 7, _blocked("the real question")]))

    (question,) = _list(conn)

    assert question.question == "the real question"


def test_parents_that_are_not_card_ids_are_ignored_even_when_they_are_unhashable(conn, monkeypatch):
    # A dict or a list is unhashable, so testing it for membership in a set of card ids would raise TypeError.
    _seed_task(conn, "T1", "w_1", "m_1")
    junk = [None, "", 7, {"id": "w_1"}, ["w_1"]]
    FakeBoard(
        monkeypatch,
        _card("x_1", "follow-up", parents=[*junk, "w_1"], events=[_blocked("found by its one real parent id")]),
        _card("x_2", "follow-up", parents=junk, events=[_blocked("only junk parents, so not ours")]),
    )

    assert [(q.card_id, q.task_key) for q in _list(conn)] == [("x_1", "T1")]


def test_a_show_with_no_events_or_parents_at_all_is_not_a_question_and_does_not_crash(conn, monkeypatch):
    _seed_task(conn, "T1", "w_1", "m_1")
    FakeBoard(monkeypatch, _card("w_1", "T1: a"))
    for bare in ({"status": "blocked"}, {"status": "blocked", "_events": None, "_parents": None}):
        monkeypatch.setattr(hermes, "kanban_show", lambda board, card_id, bare=bare: dict(bare))

        assert _list(conn) == []


def test_plan_tasks_rows_whose_cards_do_not_exist_yet_are_ignored(conn, monkeypatch):
    _seed_task(conn, "T1", "w_1", "m_1")
    _seed_task(conn, "T2", None, None)  # a row before its cards are created
    _seed_task(conn, "T1", None, None, project="other")
    FakeBoard(monkeypatch, _card("w_1", "T1: a", events=[_blocked("still found")]))

    assert [q.card_id for q in _list(conn)] == ["w_1"]


def test_a_listed_card_with_no_id_is_skipped(conn, monkeypatch):
    _seed_task(conn, "T1", "w_1", "m_1")
    FakeBoard(monkeypatch, _card("w_1", "T1: a", events=[_blocked("q")]))
    monkeypatch.setattr(hermes, "kanban_list", lambda board, *, status=None, assignee=None: [
        {"title": "T1: no id at all"}, {"id": "", "title": "T1: empty id"},
    ])

    assert _list(conn) == []


@pytest.mark.parametrize("kind_by_id, title, expected", [
    ("merge", "T1: merge", "merge"),
    ("merge", "T1: fix (round 2)", "merge"),  # a merge card is a merge card whatever it is titled
    ("work", "T1: fix (round 2)", "fix"),  # the live fix card is the row's work card, and still reads as a fix
    (None, "T1: fix (round 2)", "fix"),
    ("work", "T1: scaffold", "work"),
    (None, "T1: scaffold", "other"),
])
def test_the_card_kind_prefers_merge_then_fix_then_work(kind_by_id, title, expected):
    assert questions._kind(kind_by_id, title) == expected


def test_no_blocked_or_triage_cards_lists_nothing_and_reads_no_card(conn, monkeypatch):
    _seed_task(conn, "T1", "w_1", "m_1")
    board = FakeBoard(monkeypatch, _card("w_1", "T1: a", status="running"))

    assert _list(conn) == []
    assert board.calls == [("list", "blocked"), ("list", "triage")]   # the two lanes a question can sit in, no show


# ---------------------------------------------------------------------------------------------------------
# list_questions: the other sources (gave_up, block_loop, ases_comment) and the triage lane
# ---------------------------------------------------------------------------------------------------------


def test_a_card_the_dispatcher_gave_up_on_is_listed_although_it_has_no_blocked_event(conn, monkeypatch):
    # Hermes's circuit breaker writes a `gave_up` event and NO `blocked` event: the case the old rule never saw.
    _seed_task(conn, "T1", "w_1", "m_1")
    FakeBoard(monkeypatch, _card("w_1", "T1: scaffold", events=[_created(), _gave_up(3, "HTTP 503", 1500)]))

    (question,) = _list(conn)

    assert question == Question(
        card_id="w_1", title="T1: scaffold", task_key="T1", card_kind="work", assignee="coder-1",
        question="gave up after 3 failure(s): HTTP 503", asked_at=1500, source="gave_up",
    )


def test_a_triage_card_with_an_unblock_loop_is_listed_and_a_plain_triage_card_is_not(conn, monkeypatch):
    _seed_task(conn, "T1", "w_1", "m_1")
    _seed_task(conn, "T2", "w_2", "m_2")
    board = FakeBoard(
        monkeypatch,
        _card("w_1", "T1: looped", status="triage",
              events=[_blocked("first?", 100), _unblocked(200), _loop("Which port?", 300)]),
        # An agent-proposed card waiting for validation sits in the same lane, and nobody asked anything on it.
        _card("w_2", "T2: proposed", status="triage", events=[_created()]),
    )

    (question,) = _list(conn)

    assert (question.card_id, question.source, question.question, question.asked_at) == (
        "w_1", "block_loop", "Which port?", 300,
    )
    assert ("show", "w_2") in board.calls          # it had to be read to know


@pytest.mark.parametrize("events_list, source, reason", [
    ([_blocked("worker asked", 100), _gave_up(2, "boom", 200)], "gave_up", "gave up after 2 failure(s): boom"),
    ([_gave_up(2, "boom", 100), _blocked("worker asked", 200)], "blocked", "worker asked"),
    ([_gave_up(2, "boom", 200), _blocked("worker asked", 100)], "gave_up", "gave up after 2 failure(s): boom"),
    ([_blocked("worker asked", 200), _gave_up(2, "boom", 100)], "blocked", "worker asked"),
], ids=["gave-up-newer", "blocked-newer", "gave-up-newer-listed-first", "blocked-newer-listed-first"])
def test_a_card_with_a_gave_up_and_a_blocked_event_shows_the_newer_reason(
    conn, monkeypatch, events_list, source, reason,
):
    _seed_task(conn, "T1", "w_1", "m_1")
    FakeBoard(monkeypatch, _card("w_1", "T1: a", events=events_list))

    (question,) = _list(conn)

    assert (question.source, question.question) == (source, reason)


def test_the_questions_of_every_source_are_listed_oldest_first_with_their_source(conn, monkeypatch):
    _seed_task(conn, "T1", "w_1", "m_1")
    _seed_task(conn, "T2", "w_2", "m_2")
    FakeBoard(
        monkeypatch,
        _card("w_2", "T2: b", events=[_blocked("worker asked", 400)]),
        _card("m_1", "T1: merge", assignee=None, comments=[_asked("Retry or stop?", 300)]),
        _card("w_1", "T1: a", status="triage", events=[_loop("Which port?", 200)]),
        _card("m_2", "T2: merge", assignee=None, events=[_gave_up(3, "boom", 100)]),
    )

    result = _list(conn)

    assert [(q.card_id, q.source, q.asked_at) for q in result] == [
        ("m_2", "gave_up", 100), ("w_1", "block_loop", 200), ("m_1", "ases_comment", 300), ("w_2", "blocked", 400),
    ]


def test_a_merge_card_with_only_an_ases_question_is_listed_and_leaves_the_list_once_answered(conn, monkeypatch):
    """A merge card is created blocked (with nothing on it that asks anything), and Hermes will not block a blocked
    card again, so a question the controller has for the person is a comment. It has to show up in swarm questions,
    and swarm answer has to clear it."""
    _seed_task(conn, "T1", "w_1", "m_1")
    asked = _asked("Merge failed twice: retry or stop?", 1500)
    board = FakeBoard(monkeypatch, _card("m_1", "T1: merge", assignee=None, events=[_created()], comments=[asked]))

    (question,) = _list(conn)

    assert (question.card_id, question.card_kind, question.source, question.asked_at) == (
        "m_1", "merge", "ases_comment", 1500,
    )
    assert question.question == "Merge failed twice: retry or stop?"

    answered = questions.answer_question("b", "m_1", "Retry it.", conn=conn)

    assert (answered.card_id, answered.source) == ("m_1", "ases_comment")
    assert board.calls[-2:] == [
        ("comment", "m_1", "ANSWER: Retry it.", "user"), ("unblock", "m_1", "answered by user"),
    ]
    assert _list(conn) == []


@pytest.mark.parametrize("events_list, comments", [
    ([_blocked("q?", 100), _unblocked(200)], []),
    ([_blocked("q?", 100)], [_answer(at=200)]),
    ([_gave_up(at=100)], [_answer(at=200, prefix="UNBLOCK:", author="default")]),
    ([], [_asked("q?", 100), _answer(at=200)]),
    ([_loop("q?", 100)], [_answer(at=200)]),
], ids=["unblocked-event", "answer-comment", "unblock-comment", "asked-then-answered", "loop-then-answered"])
def test_a_question_that_was_answered_is_not_listed_even_while_the_card_still_reads_blocked(
    conn, monkeypatch, events_list, comments,
):
    _seed_task(conn, "T1", "w_1", "m_1")
    FakeBoard(monkeypatch, _card("w_1", "T1: a", events=events_list, comments=comments))

    assert _list(conn) == []


def test_a_card_that_the_list_reports_under_both_lanes_is_read_once(conn, monkeypatch):
    _seed_task(conn, "T1", "w_1", "m_1")
    board = FakeBoard(monkeypatch, _card("w_1", "T1: a", status="triage", events=[_loop("q?", 100)]))
    board.stale.add("w_1")      # the fake lists a stale id under whatever status is asked for

    (question,) = _list(conn)

    assert question.source == "block_loop"
    assert board.calls.count(("show", "w_1")) == 1


def test_a_failure_of_the_triage_list_is_not_swallowed(conn, monkeypatch):
    def list_that_fails_on_triage(board, *, status=None, assignee=None):
        if status == "triage":
            raise hermes.HermesCommandError(["kanban", "list"], 1, "hermes is down")
        return []

    monkeypatch.setattr(hermes, "kanban_list", list_that_fails_on_triage)

    with pytest.raises(hermes.HermesCommandError):
        _list(conn)


def test_a_triage_card_whose_show_names_no_status_is_trusted_to_be_in_the_lane_it_was_listed_in(conn, monkeypatch):
    _seed_task(conn, "T1", "w_1", "m_1")
    FakeBoard(monkeypatch, _card("w_1", "T1: a", status="triage", events=[_loop("q?", 100)]))
    shown = {"_events": [_loop("q?", 100)], "_parents": []}
    monkeypatch.setattr(hermes, "kanban_show", lambda board, card_id: dict(shown))

    (question,) = _list(conn)

    assert (question.source, question.question) == ("block_loop", "q?")


# ---------------------------------------------------------------------------------------------------------
# open_question: what one card is asking
# ---------------------------------------------------------------------------------------------------------


def _oq(status="blocked", events=(), comments=()):
    return questions.open_question(_card("w_1", "T1: a", status=status, events=events, comments=comments))


def test_the_sources_are_the_four_the_listing_and_the_report_count():
    assert questions.SOURCES == ("blocked", "gave_up", "block_loop", "ases_comment")


def test_an_open_question_is_frozen_and_carries_its_three_fields():
    asked = OpenQuestion("q?", 5, "blocked")

    assert (asked.reason, asked.asked_at, asked.source) == ("q?", 5, "blocked")
    with pytest.raises(dataclasses.FrozenInstanceError):
        asked.reason = "changed"


@pytest.mark.parametrize("status", ["ready", "running", "review", "todo", "scheduled", "done", "archived", "", None])
def test_open_question_is_none_for_a_card_that_is_not_blocked_or_in_triage(status):
    everything = [_blocked("q?", 100), _gave_up(at=100), _loop("q?", 100)]

    assert _oq(status, everything, [_asked("q?", 100)]) is None


def test_open_question_is_none_for_a_card_with_no_status_and_for_something_that_is_not_a_card():
    assert questions.open_question({"_events": [_blocked("q?")]}) is None
    for junk in (None, [], "a card", 7):
        assert questions.open_question(junk) is None


# ---------------------------------------------------------------------------------------------------------
# _review_lane_question / _provenance_broken_card_ids / list_questions (round 19 fix round 1, reviewer
# finding, blocker): a card a reviewer-contract PROTOCOL stop marked provenance_broken is stuck in "review"
# forever (Hermes's own block_task accepts only "ready"/"running"), which open_question's own status gate
# always excludes -- these are the narrow, separate path that still makes such a card's question visible to
# `swarm questions` without loosening what open_question or answer_question accept.
# ---------------------------------------------------------------------------------------------------------


def test_review_lane_question_reads_only_the_ases_comment_signal():
    """Deliberately narrower than open_question: a genuine Hermes-native blocked/gave_up/block_loop_detected
    event is not a shape Hermes produces on a card that is not blocked or in triage, so only the ases_comment
    signal is ever looked at here."""
    card = _card("w_1", "T1: a", status="review",
                  events=[_blocked("ignored", 100), _gave_up(at=100), _loop("ignored", 100)],
                  comments=[_asked("reviewer stopped", 200)])

    assert questions._review_lane_question(card) == OpenQuestion("reviewer stopped", 200, "ases_comment")


def test_review_lane_question_respects_an_answer_like_open_question_does():
    card = _card("w_1", "T1: a", status="review", comments=[_asked("q1", 100), _answer(at=200)])

    assert questions._review_lane_question(card) is None


def test_provenance_broken_card_ids_reads_back_the_events_scoped_by_project(conn):
    events.record(conn, reviewcontract.EVENT_PROVENANCE_BROKEN, {
        "project": "p1", "task_key": "T1", "card_id": "w_1", "run_id": 2,
    }, project="p1")
    events.record(conn, reviewcontract.EVENT_PROVENANCE_BROKEN, {
        "project": "p2", "task_key": "T9", "card_id": "w_9", "run_id": 3,
    }, project="p2")

    assert questions._provenance_broken_card_ids(conn, "p1") == {"w_1"}


def test_a_provenance_broken_review_card_is_listed_by_its_ases_comment(conn, monkeypatch):
    """The exact gap the independent review proved live: a card a reviewer corrupted into "review" by calling
    kanban_request_review (D6) is stuck there forever, so without this lane `swarm questions` could never show
    the question ASES's own controller._protocol_stop already posted as a comment on it."""
    _seed_task(conn, "T1", "w_1", "m_1")
    FakeBoard(monkeypatch, _card("w_1", "T1: scaffold", status="review", comments=[_asked("reviewer stopped", 1234)]))
    events.record(conn, reviewcontract.EVENT_PROVENANCE_BROKEN, {
        "project": "p1", "task_key": "T1", "card_id": "w_1", "run_id": 2,
    }, project="p1")

    result = _list(conn)

    assert result == [Question(
        card_id="w_1", title="T1: scaffold", task_key="T1", card_kind="work", assignee="coder-1",
        question="reviewer stopped", asked_at=1234, source="ases_comment",
    )]


def test_a_provenance_broken_card_that_moved_off_review_is_not_listed_by_the_new_lane(conn, monkeypatch):
    _seed_task(conn, "T1", "w_1", "m_1")
    FakeBoard(monkeypatch, _card("w_1", "T1: scaffold", status="done", comments=[_asked("reviewer stopped", 1234)]))
    events.record(conn, reviewcontract.EVENT_PROVENANCE_BROKEN, {
        "project": "p1", "task_key": "T1", "card_id": "w_1", "run_id": 2,
    }, project="p1")

    assert _list(conn) == []


def test_a_provenance_broken_review_card_is_never_answerable(conn, monkeypatch):
    """The visibility fix above must never widen what `swarm answer` accepts: design step 6, "never unblocked
    or reopened by ASES"."""
    _seed_task(conn, "T1", "w_1", "m_1")
    FakeBoard(monkeypatch, _card("w_1", "T1: scaffold", status="review", comments=[_asked("reviewer stopped", 1234)]))
    events.record(conn, reviewcontract.EVENT_PROVENANCE_BROKEN, {
        "project": "p1", "task_key": "T1", "card_id": "w_1", "run_id": 2,
    }, project="p1")

    with pytest.raises(QuestionError, match="review"):
        questions.answer_question("b", "w_1", "use candidate B", conn=conn)


@pytest.mark.parametrize("status, events_list, comments, expected", [
    ("blocked", [_blocked("Which database?", 1111)], [], OpenQuestion("Which database?", 1111, "blocked")),
    ("blocked", [_gave_up(3, "boom", 1200)], [], OpenQuestion("gave up after 3 failure(s): boom", 1200, "gave_up")),
    ("triage", [_loop("Which port?", 1300)], [], OpenQuestion("Which port?", 1300, "block_loop")),
    ("blocked", [], [_asked("Retry or stop?", 1400)], OpenQuestion("Retry or stop?", 1400, "ases_comment")),
    ("triage", [], [_asked("Retry or stop?", 1400)], OpenQuestion("Retry or stop?", 1400, "ases_comment")),
], ids=["blocked", "gave-up", "block-loop-in-triage", "ases-comment", "ases-comment-in-triage"])
def test_open_question_reads_each_of_the_four_sources(status, events_list, comments, expected):
    assert _oq(status, events_list, comments) == expected


def _created_blocked(at=1):
    """The `blocked` event Hermes 0.21.3 writes for a card CREATED blocked (kanban_db.create_task, initial_status)."""
    return {"kind": "blocked", "run_id": None, "created_at": at,
            "payload": {"reason": "initial_status", "status": "blocked", "actor": "ases"}}


@pytest.mark.parametrize("event", [
    _created_blocked(),
    {**_created_blocked(), "payload": {"reason": "  initial_status\n"}},
    {**_created_blocked(), "payload": json.dumps({"reason": "initial_status", "status": "blocked"})},
], ids=["as-written", "padded", "json-text"])
def test_a_card_created_blocked_is_not_a_question_even_with_the_blocked_event_hermes_writes_for_it(event):
    assert questions.CREATED_BLOCKED_REASON == "initial_status"
    assert _oq(events=[_created(), event]) is None
    assert _oq(events=[event]) is None


def test_a_real_block_after_the_creation_block_is_a_question_and_the_creation_block_hides_nothing():
    asked = _oq(events=[_created(), _created_blocked(1), _blocked("fix-card budget spent", 500)])
    assert (asked.source, asked.reason, asked.asked_at) == ("blocked", "fix-card budget spent", 500)

    asked = _oq(events=[_created_blocked(1)], comments=[_asked("Retry or stop?", 300)])
    assert (asked.source, asked.reason) == ("ases_comment", "Retry or stop?")
    assert _oq(events=[_blocked("initial_status is wrong: why?", 5)]).source == "blocked"      # only the bare word


def test_a_merge_card_created_blocked_is_not_listed_or_answerable_but_can_be_asked_about(conn, monkeypatch):
    _seed_task(conn, "T1", "w_1", "m_1")
    board = FakeBoard(
        monkeypatch, _card("m_1", "T1: merge", assignee=None, events=[_created(), _created_blocked()]),
    )
    assert _list(conn) == []
    with pytest.raises(QuestionError, match="card m_1 has no open question"):
        questions.answer_question("b", "m_1", "Go ahead.", conn=conn)
    assert board.names() == ["list", "list", "show", "show"]               # read twice, refused, nothing posted

    assert questions.ask_user("b", _shown(board, "m_1"), "Retry it?", conn=conn) == "commented"
    (question,) = _list(conn)
    assert (question.card_id, question.card_kind, question.source, question.question) == (
        "m_1", "merge", "ases_comment", "Retry it?",
    )


def test_a_block_loop_event_without_a_reason_asks_nothing_but_a_gave_up_event_always_does():
    bare_loop = {"kind": "block_loop_detected", "payload": {"reason": None, "kind": None}, "created_at": 5}
    bare_gave_up = {"kind": "gave_up", "payload": {}, "created_at": 5}

    assert _oq("triage", [bare_loop]) is None
    assert _oq("blocked", [bare_gave_up]) == OpenQuestion("gave up after repeated failures", 5, "gave_up")


@pytest.mark.parametrize("payload, expected", [
    ({"failures": 3, "error": "boom"}, "gave up after 3 failure(s): boom"),
    ({"failures": 1, "error": "boom"}, "gave up after 1 failure(s): boom"),
    ({"failures": "2", "error": "boom"}, "gave up after 2 failure(s): boom"),
    ({"failures": 3}, "gave up after 3 failure(s)"),
    ({"failures": 3, "error": "   "}, "gave up after 3 failure(s)"),
    ({"failures": 3, "error": None}, "gave up after 3 failure(s)"),
    ({"failures": 3, "error": 42}, "gave up after 3 failure(s)"),
    ({"error": "boom"}, "gave up after repeated failures: boom"),
    ({"failures": True, "error": "boom"}, "gave up after repeated failures: boom"),
    ({"failures": "many", "error": "boom"}, "gave up after repeated failures: boom"),
    ({"failures": 3, "error": "  padded \n"}, "gave up after 3 failure(s): padded"),
    ({"failures": 3, "error": "line one\nline two"}, "gave up after 3 failure(s): line one\nline two"),
    ({}, "gave up after repeated failures"),
    (None, "gave up after repeated failures"),
    (json.dumps({"failures": 4, "error": "e"}), "gave up after 4 failure(s): e"),
    ("not json", "gave up after repeated failures"),
], ids=["plain", "one", "count-as-text", "no-error", "blank-error", "null-error", "error-not-text", "no-count",
        "bool-count", "text-count", "error-stripped", "multi-line", "empty-payload", "no-payload", "json-payload",
        "unreadable-payload"])
def test_a_gave_up_reason_is_built_from_the_failure_count_and_the_last_error(payload, expected):
    event = {"kind": "gave_up", "payload": payload, "created_at": 7}

    assert _oq(events=[event]).reason == expected


def test_a_reason_is_redacted_and_escaped_to_ascii_whatever_its_source():
    acute = "\N{LATIN SMALL LETTER E WITH ACUTE}"
    arrow = "\N{RIGHTWARDS ARROW}"
    text = f"Use the key {SECRET}? caf{acute}\nsecond line {arrow} ok\x1b[31mred"
    cases = [
        _oq(events=[_blocked(text)]),
        _oq(events=[_gave_up(2, text)]),
        _oq("triage", [_loop(text)]),
        _oq(comments=[_asked(text)]),
    ]

    for asked in cases:
        assert SECRET not in asked.reason and "[redacted]" in asked.reason
        assert asked.reason.isascii()
        assert "caf" + chr(92) + "xe9" in asked.reason and chr(92) + "u2192" in asked.reason
        assert chr(92) + "x1b[31mred" in asked.reason
        assert "\nsecond line" in asked.reason            # a line break is kept: it is not a control character


@pytest.mark.parametrize("events_list, comments, source, reason", [
    ([_blocked("worker asked", 100), _gave_up(2, "boom", 200)], [], "gave_up", "gave up after 2 failure(s): boom"),
    ([_gave_up(2, "boom", 100), _blocked("worker asked", 200)], [], "blocked", "worker asked"),
    ([_gave_up(2, "boom", 200), _blocked("worker asked", 100)], [], "gave_up", "gave up after 2 failure(s): boom"),
    ([_blocked("worker asked", 100)], [_asked("ASES asks", 200)], "ases_comment", "ASES asks"),
    ([_blocked("worker asked", 200)], [_asked("ASES asks", 100)], "blocked", "worker asked"),
    ([_gave_up(2, "boom", 100)], [_asked("ASES asks", 300)], "ases_comment", "ASES asks"),
    ([_gave_up(2, "boom", 300), _loop("looped?", 200)], [], "gave_up", "gave up after 2 failure(s): boom"),
    ([_loop("looped?", 400), _gave_up(2, "boom", 300)], [], "block_loop", "looped?"),
    ([], [_asked("first", 100), _asked("second", 200)], "ases_comment", "second"),
    ([], [_asked("second", 200), _asked("first", 100)], "ases_comment", "second"),
], ids=["gave-up-newer", "blocked-newer", "gave-up-newer-listed-first", "comment-newer", "event-newer",
        "comment-newer-than-gave-up", "gave-up-newer-than-loop", "loop-newer-than-gave-up", "newer-comment",
        "newer-comment-listed-first"])
def test_the_newest_signal_wins(events_list, comments, source, reason):
    asked = _oq(events=events_list, comments=comments)

    assert (asked.source, asked.reason) == (source, reason)


def test_on_a_tie_a_comment_counts_as_newer_than_an_event():
    asked = _oq(events=[_blocked("worker asked", 100)], comments=[_asked("ASES asks", 100)])

    assert (asked.source, asked.reason) == ("ases_comment", "ASES asks")


def test_on_a_tie_between_two_events_or_two_comments_the_later_position_wins():
    assert _oq(events=[_blocked("first", 100), _gave_up(2, "boom", 100)]).source == "gave_up"
    assert _oq(events=[_gave_up(2, "boom", 100), _blocked("second", 100)]).source == "blocked"
    assert _oq(comments=[_asked("first", 100), _asked("second", 100)]).reason == "second"
    assert _oq(comments=[_asked("second", 100), _asked("first", 100)]).reason == "first"


@pytest.mark.parametrize("events_list, comments, still_open", [
    ([_blocked("q?", 100), _unblocked(200)], [], False),
    ([_blocked("q?", 100)], [_answer(at=200)], False),
    ([_blocked("q?", 100)], [_answer(at=200, prefix="UNBLOCK:", author="default")], False),
    ([_blocked("q?", 100)], [{"author": "user", "body": "  ANSWER: padded", "created_at": 200}], False),
    ([_blocked("q?", 200), _unblocked(100)], [], True),
    ([_blocked("q?", 200)], [_answer(at=100)], True),
    ([_blocked("q?", 100), _unblocked(100)], [], False),
    ([_unblocked(100), _blocked("q?", 100)], [], True),
    ([_blocked("q?", 100)], [_answer(at=100)], False),
    ([], [_asked("q?", 100), _answer(at=200)], False),
    ([], [_asked("q?", 100), _answer(at=100)], False),
    ([], [_answer(at=100), _asked("q?", 100)], True),
    ([_unblocked(100)], [_asked("q?", 100)], True),
    ([_blocked("q?", 100)], [_answer("x", 200, prefix="ANSWERED:")], True),
    ([_blocked("q?", 100)], [{"author": "user", "body": "NOT AN ANSWER: x", "created_at": 200}], True),
    ([_blocked("q?", 100)], [{"author": "user", "body": "answer: lower case is no marker", "created_at": 200}], True),
    ([_blocked("q?", 100)], [{"author": "user", "body": None, "created_at": 200}], True),
], ids=["unblocked-event", "answer", "unblock-comment", "padded-answer", "answer-older-event", "answer-older-comment",
        "tie-events-answer-last", "tie-events-question-last", "tie-comment-is-newer", "ases-then-answer",
        "ases-answer-tie-later-wins", "answer-then-ases-tie", "tie-event-vs-ases-comment", "answered-is-not-answer",
        "not-an-answer", "case-matters", "answer-without-text"])
def test_a_signal_older_than_the_answer_does_not_count(events_list, comments, still_open):
    assert (_oq(events=events_list, comments=comments) is not None) is still_open


def test_a_question_that_comes_back_after_an_answer_is_a_new_question():
    asked = _oq(events=[_gave_up(2, "first", 100), _unblocked(200), _blocked("again?", 300)])
    assert (asked.source, asked.reason, asked.asked_at) == ("blocked", "again?", 300)

    asked = _oq(events=[_blocked("q?", 100), _unblocked(200), _gave_up(2, "boom", 300)])
    assert (asked.source, asked.asked_at) == ("gave_up", 300)


def test_the_newest_blocked_event_decides_even_when_its_reason_is_empty():
    # An older, long-answered reason must not come back because the block that holds the card now says nothing.
    assert _oq(events=[_blocked("an older reason", 100), _blocked("", 200)]) is None
    assert _oq(events=[_blocked("an older reason", 100), _blocked("   \n", 200)]) is None


def test_an_empty_newest_blocked_event_does_not_hide_a_signal_of_another_source():
    asked = _oq(events=[_blocked("an older reason", 100), _blocked("", 200)], comments=[_asked("ASES asks", 300)])

    assert (asked.source, asked.reason) == ("ases_comment", "ASES asks")


@pytest.mark.parametrize("comment, expected", [
    (_asked("Which way?"), "Which way?"),
    ({"author": "ases", "body": "  ASES QUESTION:   Which way?  \n", "created_at": 1}, "Which way?"),
    (_asked("Which way?", author="coder-1"), None),
    (_asked("Which way?", author="ASES"), None),
    ({"author": "ases", "body": "ASES QUESTION:", "created_at": 1}, None),
    ({"author": "ases", "body": "ASES QUESTION:   \n", "created_at": 1}, None),
    ({"author": "ases", "body": "note: ASES QUESTION: Which way?", "created_at": 1}, None),
    ({"author": "ases", "body": "ases question: Which way?", "created_at": 1}, None),
    ({"author": "ases", "body": None, "created_at": 1}, None),
    ({"author": "ases", "body": 5, "created_at": 1}, None),
    ({"body": "ASES QUESTION: Which way?", "created_at": 1}, None),
    ({"author": "default", "body": "BLOCKED: Which way?", "created_at": 1}, None),
], ids=["plain", "padded", "someone-else", "author-case", "no-text", "blank-text", "not-at-the-start",
        "marker-case", "no-body", "body-not-text", "no-author", "blocked-comment-alone"])
def test_which_comments_are_a_question(comment, expected):
    asked = _oq(comments=[comment])

    assert (asked.reason if asked else None) == expected


def test_open_question_ignores_junk_and_never_changes_the_card():
    junk_events = [None, "junk", 7, [], {"kind": "blocked"}, {"kind": "blocked", "payload": "not json"}]
    card = _card(
        "w_1", "T1: a",
        events=[*junk_events, _blocked("real?", 100)],
        comments=[None, "junk", 3, {"author": "ases"}, {"author": "ases", "body": 5}, _asked("real too?", 50)],
    )
    before = copy.deepcopy(card)

    asked = questions.open_question(card)

    assert card == before
    assert (asked.source, asked.reason) == ("blocked", "real?")


@pytest.mark.parametrize("card", [
    {"status": "blocked"},
    {"status": "blocked", "_events": None, "_comments": None},
    {"status": "blocked", "_events": "not a list", "_comments": {"a": 1}},
    {"status": "triage", "_events": [], "_comments": []},
], ids=["bare", "nulls", "not-lists", "empty"])
def test_a_card_with_no_usable_events_or_comments_asks_nothing_and_does_not_crash(card):
    assert questions.open_question(card) is None


@pytest.mark.parametrize("created_at, expected", [
    ("1789832318", 1789832318), (1789832318.9, 1789832318), (None, 0), ("soon", 0), (True, 0), (-5, 0),
    (float("inf"), 0), (float("nan"), 0), (10 ** 18 + 1, 10 ** 18 + 1),
], ids=["numeric-string", "float", "none", "not-a-number", "boolean", "negative", "infinity", "nan",
        "int-not-rounded-through-a-float"])
def test_the_time_of_every_source_is_read_as_whole_epoch_seconds_and_anything_else_is_zero(created_at, expected):
    cases = [
        _oq(events=[{"kind": "blocked", "payload": {"reason": "q"}, "created_at": created_at}]),
        _oq(events=[{"kind": "gave_up", "payload": {"failures": 1}, "created_at": created_at}]),
        _oq("triage", [{**_loop("q"), "created_at": created_at}]),
        _oq(comments=[_asked("q", created_at)]),
    ]

    assert [asked.asked_at for asked in cases] == [expected] * 4


# ---------------------------------------------------------------------------------------------------------
# ask_user: put a question to the person
# ---------------------------------------------------------------------------------------------------------


def _shown(board, card_id="w_1"):
    """The card as a fresh hermes.kanban_show would return it, without counting as a call on the fake."""
    return copy.deepcopy(board.cards[card_id])


@pytest.mark.parametrize("status", ["ready", "running"])
def test_ask_user_blocks_a_ready_or_running_card_with_the_needs_input_kind(monkeypatch, status):
    board = FakeBoard(monkeypatch, _card("w_1", "T1: a", status=status))

    result = questions.ask_user("b", _shown(board), "Which database?")

    assert result == "blocked"
    assert board.calls == [("block", "w_1", "Which database?", "needs_input")]
    assert board.cards["w_1"]["status"] == "blocked"
    assert questions.open_question(_shown(board)) == OpenQuestion("Which database?", board.clock, "blocked")


@pytest.mark.parametrize("status", ["blocked", "triage", "todo", "scheduled", "review", "done"])
def test_ask_user_comments_on_a_card_hermes_will_not_block(monkeypatch, status):
    board = FakeBoard(monkeypatch, _card("w_1", "T1: a", status=status))

    result = questions.ask_user("b", _shown(board), "Which database?")

    assert result == "commented"
    assert board.calls == [("comment", "w_1", "ASES QUESTION: Which database?", "ases")]
    assert board.cards["w_1"]["status"] == status                      # nothing else changed
    asked = questions.open_question(_shown(board))
    if status in ("blocked", "triage"):
        assert asked == OpenQuestion("Which database?", board.clock, "ases_comment")
    else:
        assert asked is None                                           # the card is not held, so it asks nothing


@pytest.mark.parametrize("status", ["ready", "running"])
def test_a_block_hermes_refuses_falls_back_to_the_comment(monkeypatch, status):
    board = FakeBoard(monkeypatch, _card("w_1", "T1: a", status=status))
    board.refuse_block = True      # the card moved since it was read: the CLI comments, then exits 1

    result = questions.ask_user("b", _shown(board), "Which database?")

    assert result == "commented"
    assert board.calls == [
        ("block", "w_1", "Which database?", "needs_input"),
        ("comment", "w_1", "ASES QUESTION: Which database?", "ases"),
    ]


@pytest.mark.parametrize("error", [
    hermes.HermesNotFound("`hermes` is not on PATH"), subprocess.TimeoutExpired("hermes", 30), OSError("handle"),
], ids=["not-found", "timeout", "os-error"])
def test_a_block_that_fails_for_another_reason_than_a_refusal_propagates_and_nothing_is_posted(
    conn, monkeypatch, error,
):
    board = FakeBoard(monkeypatch, _card("w_1", "T1: a", status="ready"))
    board.block_error = error

    with pytest.raises(type(error)):
        questions.ask_user("b", _shown(board), "Which database?", conn=conn)

    assert board.names() == ["block"]
    assert events.recent(conn) == []


def test_a_comment_that_fails_propagates_and_nothing_is_recorded(conn, monkeypatch):
    board = FakeBoard(monkeypatch, _card("w_1", "T1: a", status="blocked"))
    board.comment_error = _hermes_error("comment")

    with pytest.raises(hermes.HermesCommandError):
        questions.ask_user("b", _shown(board), "Which database?", conn=conn)

    assert events.recent(conn) == []


def test_ask_user_writes_the_comment_under_the_author_it_is_given(monkeypatch):
    board = FakeBoard(monkeypatch, _card("w_1", "T1: a", status="blocked"))

    questions.ask_user("b", _shown(board), "Which database?", author="controller")

    assert board.calls == [("comment", "w_1", "ASES QUESTION: Which database?", "controller")]


@pytest.mark.parametrize("status, events_list, comments, text", [
    ("blocked", [_blocked("Which database?", 100)], [], "Which database?"),
    ("blocked", [], [_asked("Which database?", 100)], "Which database?"),
    ("triage", [_loop("Which database?", 100)], [], "Which database?"),
    ("blocked", [_gave_up(3, "boom", 100)], [], "gave up after 3 failure(s): boom"),
    ("blocked", [], [_asked("Which database?", 100)], "  Which\n  database?  "),
    ("blocked", [_blocked("Use the key [redacted]?", 100)], [], f"Use the key {SECRET}?"),
], ids=["blocked-event", "ases-comment", "block-loop", "gave-up", "whitespace", "redacted-secret"])
def test_an_identical_question_that_is_still_open_is_not_asked_again(
    conn, monkeypatch, status, events_list, comments, text,
):
    board = FakeBoard(monkeypatch, _card("w_1", "T1: a", status=status, events=events_list, comments=comments))

    result = questions.ask_user("b", _shown(board), text, conn=conn)

    assert result == "already_asked"
    assert board.calls == []                  # nothing posted, nothing blocked
    assert events.recent(conn) == []          # and nothing logged: a pass that repeats must not spam either


@pytest.mark.parametrize("status", ["review", "ready"])
def test_an_ases_comment_question_is_not_repeated_on_a_card_status_hides_from_open_question(
    conn, monkeypatch, status,
):
    """Round 19 fix round 2 (reviewer finding, major): open_question's own status gate (_QUESTION_STATUSES,
    "blocked"/"triage" only) means it can never see the "ASES QUESTION:" comment ask_user itself already posted
    on a card stuck outside those two statuses -- a reviewer-contract PROTOCOL stop's card (status "review",
    forever, by design: controller._protocol_stop) or an EVIDENCE changes_requested stop's card at the moment it
    is read (status "ready", Hermes's own routing to the implementer, not a block). Before this fix,
    `already = open_question(card)` always came back None here, so ask_user's own dedup below never engaged and
    a second call for the exact same question posted a brand-new comment every time -- confirmed live by the
    independent reviewer against a copy of the source (two distinct run_ids, two "ASES QUESTION:" comments)."""
    board = FakeBoard(monkeypatch, _card("w_1", "T1: a", status=status, comments=[_asked("Which database?", 100)]))

    result = questions.ask_user("b", _shown(board), "Which database?", conn=conn)

    assert result == "already_asked"
    assert board.calls == []                  # nothing posted, nothing blocked
    assert events.recent(conn) == []


def test_a_different_question_is_asked_even_while_another_is_open(monkeypatch):
    board = FakeBoard(monkeypatch, _card("w_1", "T1: a", events=[_blocked("Which database?", 100)]))

    assert questions.ask_user("b", _shown(board), "Which port?") == "commented"

    asked = questions.open_question(_shown(board))
    assert (asked.source, asked.reason) == ("ases_comment", "Which port?")          # the newest is the one shown
    assert questions.ask_user("b", _shown(board), "Which port?") == "already_asked"


@pytest.mark.parametrize("events_list, comments", [
    ([_blocked("Which database?", 100), _unblocked(200)], []),
    ([], [_asked("Which database?", 100), _answer(at=200)]),
    ([], [_asked("Which database?", 100), _answer(at=200, prefix="UNBLOCK:", author="default")]),
], ids=["unblocked", "answered", "unblock-comment"])
def test_a_question_that_was_answered_is_asked_again_when_it_comes_back(monkeypatch, events_list, comments):
    board = FakeBoard(monkeypatch, _card("w_1", "T1: a", events=events_list, comments=comments))

    assert questions.ask_user("b", _shown(board), "Which database?") == "commented"
    assert board.calls == [("comment", "w_1", "ASES QUESTION: Which database?", "ases")]


@pytest.mark.parametrize("status", ["ready", "blocked"])
def test_the_text_is_redacted_before_it_leaves(monkeypatch, status):
    board = FakeBoard(monkeypatch, _card("w_1", "T1: a", status=status))

    questions.ask_user("b", _shown(board), f"Use the key {SECRET} for staging?")

    (call,) = board.calls
    assert SECRET not in call[2] and "sk-abc" not in call[2] and "[redacted]" in call[2]
    assert call[2].endswith("for staging?")


@pytest.mark.parametrize("status", ["ready", "blocked"])
def test_the_text_is_cut_to_1500_characters_and_the_cut_comes_after_the_redaction(monkeypatch, status):
    assert questions.QUESTION_LIMIT == 1500
    board = FakeBoard(monkeypatch, _card("w_1", "T1: a", status=status))
    # A secret that straddles the cut: cutting first would leave a fragment of it, redacting first leaves none.
    text = "a" * 1494 + " " + SECRET + " and more"

    questions.ask_user("b", _shown(board), text)

    (call,) = board.calls
    posted = call[2].removeprefix("ASES QUESTION: ")
    assert posted == "a" * 1494 + " [reda"            # the redacted text, cut at 1500: no fragment of the secret
    assert "sk-" not in posted

    board = FakeBoard(monkeypatch, _card("w_2", "T2: b", status=status))
    questions.ask_user("b", _shown(board, "w_2"), "x" * 5000)
    assert len(board.calls[0][2].removeprefix("ASES QUESTION: ")) == 1500


@pytest.mark.parametrize(
    "text", ["", "   ", "\n\t\r\n", None, "\x00"], ids=["empty", "spaces", "whitespace", "none", "nul"],
)
def test_a_blank_question_is_refused_before_anything_is_touched(conn, monkeypatch, text):
    board = FakeBoard(monkeypatch, _card("w_1", "T1: a", status="ready"))

    with pytest.raises(ValueError, match="empty"):
        questions.ask_user("b", _shown(board), text, conn=conn)

    assert board.calls == [] and events.recent(conn) == []


def test_a_nul_in_the_text_is_dropped_because_it_cannot_travel_in_a_command_line(monkeypatch):
    board = FakeBoard(monkeypatch, _card("w_1", "T1: a", status="blocked"))

    questions.ask_user("b", _shown(board), "Which\x00 database?")

    assert board.calls == [("comment", "w_1", "ASES QUESTION: Which database?", "ases")]


@pytest.mark.parametrize("card", [{}, {"status": "ready"}, {"id": ""}, None, "w_1"])
def test_a_card_with_no_id_is_refused(monkeypatch, card):
    board = FakeBoard(monkeypatch)

    with pytest.raises(ValueError, match="id"):
        questions.ask_user("b", card, "Which database?")

    assert board.calls == []


def test_the_question_asked_event_records_the_card_how_it_was_asked_and_a_preview(conn, monkeypatch):
    board = FakeBoard(
        monkeypatch, _card("w_1", "T1: a", status="ready"), _card("m_1", "T1: merge", status="blocked", assignee=None),
    )

    questions.ask_user("b", _shown(board, "w_1"), "Which database?", conn=conn)
    questions.ask_user("b", _shown(board, "m_1"), f"Retry it? {SECRET} " + "z" * 400, conn=conn)

    first, second = reversed(_payloads(conn, "question_asked"))      # events.recent lists the newest first
    assert first == {"card_id": "w_1", "via": "blocked", "status": "ready", "chars": 15, "question": "Which database?"}
    assert (second["card_id"], second["via"], second["status"]) == ("m_1", "commented", "blocked")
    assert second["chars"] == len("Retry it? [redacted] " + "z" * 400)
    assert len(second["question"]) == 300 and SECRET not in json.dumps(second)


def test_no_event_is_written_without_a_conn(conn, monkeypatch):
    board = FakeBoard(monkeypatch, _card("m_1", "T1: merge", status="blocked", assignee=None))

    assert questions.ask_user("b", _shown(board, "m_1"), "Retry it?") == "commented"

    assert events.recent(conn) == []


def test_a_question_asked_on_a_blocked_merge_card_is_listed_asked_only_once_and_answered(conn, monkeypatch):
    _seed_task(conn, "T1", "w_1", "m_1")
    board = FakeBoard(monkeypatch, _card("m_1", "T1: merge", status="blocked", assignee=None, events=[_created()]))
    assert _list(conn) == []                                  # created blocked and waiting: it asks nothing

    text = "Merge failed twice: retry or stop?"
    assert questions.ask_user("b", _shown(board, "m_1"), text, conn=conn) == "commented"

    (question,) = _list(conn)
    assert (question.card_id, question.card_kind, question.source, question.question) == (
        "m_1", "merge", "ases_comment", text,
    )
    assert questions.ask_user("b", _shown(board, "m_1"), text, conn=conn) == "already_asked"
    assert [call[0] for call in board.calls].count("comment") == 1

    questions.answer_question("b", "m_1", "Retry it.", conn=conn)

    assert _list(conn) == []
    assert questions.open_question(_shown(board, "m_1")) is None


def test_a_second_question_on_a_card_that_was_unblocked_lands_in_triage_and_is_still_found(conn, monkeypatch):
    """Hermes routes a second block of the same kind on one card to `triage` with a block_loop_detected event, and
    `hermes kanban block` still exits 0: ask_user reports "blocked", and the question must still be listed."""
    _seed_task(conn, "T1", "w_1", "m_1")
    board = FakeBoard(monkeypatch, _card("w_1", "T1: a", status="ready"))
    assert questions.ask_user("b", _shown(board), "Which database?") == "blocked"
    questions.answer_question("b", "w_1", "Use sqlite.", conn=conn)              # unblocks: the card is ready again
    assert board.cards["w_1"]["status"] == "ready"

    assert questions.ask_user("b", _shown(board), "Which port?") == "blocked"
    assert board.cards["w_1"]["status"] == "triage"

    (question,) = _list(conn)
    assert (question.source, question.question) == ("block_loop", "Which port?")
    assert questions.ask_user("b", _shown(board), "Which port?") == "already_asked"


# ---------------------------------------------------------------------------------------------------------
# answer_question
# ---------------------------------------------------------------------------------------------------------


def _blocked_work_card(conn, monkeypatch, reason="Which database?", at=1111):
    """T1 with its work card blocked on `reason`. Returns the FakeBoard."""
    _seed_task(conn, "T1", "w_1", "m_1")
    return FakeBoard(monkeypatch, _card("w_1", "T1: scaffold", events=[_blocked(reason, at)]))


def test_the_answer_is_commented_before_the_card_is_unblocked(conn, monkeypatch):
    board = _blocked_work_card(conn, monkeypatch)

    questions.answer_question("b", "w_1", "Use sqlite.", conn=conn)

    assert board.calls == [
        ("show", "w_1"),
        ("comment", "w_1", "ANSWER: Use sqlite.", "user"),
        ("unblock", "w_1", "answered by user"),
    ]
    assert board.cards["w_1"]["status"] == "ready"


def test_a_different_author_is_used_for_the_comment_and_the_unblock_reason(conn, monkeypatch):
    board = _blocked_work_card(conn, monkeypatch)

    questions.answer_question("b", "w_1", "Use sqlite.", conn=conn, author="alice")

    assert ("comment", "w_1", "ANSWER: Use sqlite.", "alice") in board.calls
    assert ("unblock", "w_1", "answered by alice") in board.calls


def test_the_comment_carries_the_stripped_text_and_keeps_its_own_line_breaks(conn, monkeypatch):
    board = _blocked_work_card(conn, monkeypatch)

    questions.answer_question("b", "w_1", "  Use sqlite.\nAnd keep it small.\n\n", conn=conn)

    assert ("comment", "w_1", "ANSWER: Use sqlite.\nAnd keep it small.", "user") in board.calls


@pytest.mark.parametrize("text", ["", " ", "\n\t  \r\n", None], ids=["empty", "space", "whitespace", "none"])
def test_an_empty_answer_is_refused_and_nothing_is_touched(conn, monkeypatch, text):
    board = _blocked_work_card(conn, monkeypatch)

    with pytest.raises(QuestionError, match="empty"):
        questions.answer_question("b", "w_1", text, conn=conn)

    assert board.calls == []  # not even read
    assert events.recent(conn) == []


def test_an_answer_that_looks_like_a_secret_is_refused_without_echoing_it(conn, monkeypatch):
    board = _blocked_work_card(conn, monkeypatch)

    with pytest.raises(QuestionError) as refused:
        questions.answer_question("b", "w_1", SECRET, conn=conn)

    message = str(refused.value)
    assert SECRET not in message and "sk-abc" not in message
    assert "line 1" in message and "ASES-SEC-01" in message
    assert board.calls == []
    assert events.recent(conn) == []


def test_the_refusal_names_the_lines_that_hold_a_secret_and_nothing_else(conn, monkeypatch):
    board = _blocked_work_card(conn, monkeypatch)
    text = f"use the staging key\nkey is {SECRET}\nthen restart\n{SECRET}"

    with pytest.raises(QuestionError) as refused:
        questions.answer_question("b", "w_1", text, conn=conn)

    message = str(refused.value)
    assert "lines 2, 4" in message
    assert SECRET not in message and "staging" not in message
    assert board.calls == []


def test_a_secret_on_a_windows_line_break_is_still_found(conn, monkeypatch):
    board = _blocked_work_card(conn, monkeypatch)

    with pytest.raises(QuestionError, match="line 2"):
        questions.answer_question("b", "w_1", f"fine\r\n{SECRET}\r\n", conn=conn)

    assert board.calls == []


def test_an_answer_that_only_resembles_a_secret_is_delivered(conn, monkeypatch):
    board = _blocked_work_card(conn, monkeypatch)

    questions.answer_question("b", "w_1", "Use sk-lite or +1 for scikit-learn, per the ghp_ docs.", conn=conn)

    assert board.names() == ["show", "comment", "unblock"]


def test_a_card_that_is_not_blocked_has_no_open_question(conn, monkeypatch):
    _seed_task(conn, "T1", "w_1", "m_1")
    board = FakeBoard(monkeypatch, _card("w_1", "T1: scaffold", status="ready", events=[_blocked("old question")]))

    with pytest.raises(QuestionError, match=r"card w_1 has no open question \(its status is ready, not blocked\)"):
        questions.answer_question("b", "w_1", "Use sqlite.", conn=conn)

    assert board.names() == ["show"]  # read, then refused: nothing posted, nothing unblocked


def test_a_refusal_about_a_card_is_pure_ascii_even_for_an_odd_card_id_or_status(conn, monkeypatch):
    acute = "\N{LATIN SMALL LETTER E WITH ACUTE}"
    FakeBoard(monkeypatch, _card(f"t_{acute}", "x", status=f"pr{acute}t"))

    with pytest.raises(QuestionError) as refused:
        questions.answer_question("b", f"t_{acute}", "Use sqlite.", conn=conn)

    message = str(refused.value)
    assert message.isascii()
    assert "card t_" + chr(92) + "xe9 has no open question" in message
    assert "status is pr" + chr(92) + "xe9t" in message


@pytest.mark.parametrize(
    "events_list", [[], [_created()], [_blocked("")]], ids=["no-events", "waiting", "empty-reason"],
)
def test_a_blocked_card_without_a_block_reason_has_no_open_question(conn, monkeypatch, events_list):
    _seed_task(conn, "T1", "w_1", "m_1")
    board = FakeBoard(monkeypatch, _card("m_1", "T1: merge", assignee=None, events=events_list))

    with pytest.raises(QuestionError, match="card m_1 has no open question"):
        questions.answer_question("b", "m_1", "Go ahead.", conn=conn)

    assert board.names() == ["show"]


def test_a_card_that_does_not_exist_raises_the_hermes_error_and_nothing_else_is_called(conn, monkeypatch):
    board = FakeBoard(monkeypatch)

    with pytest.raises(hermes.HermesCommandError):
        questions.answer_question("b", "t_nope", "Use sqlite.", conn=conn)

    assert board.calls == [("show", "t_nope")]
    assert events.recent(conn) == []


def test_a_failed_comment_leaves_the_card_blocked_and_never_unblocks(conn, monkeypatch):
    board = _blocked_work_card(conn, monkeypatch)
    board.comment_error = _hermes_error("comment")

    with pytest.raises(hermes.HermesCommandError):
        questions.answer_question("b", "w_1", "Use sqlite.", conn=conn)

    assert board.names() == ["show", "comment"]
    assert board.cards["w_1"]["status"] == "blocked"
    assert _payloads(conn, "question_answered") == []


def test_a_failed_unblock_after_the_comment_propagates_and_is_not_retried(conn, monkeypatch):
    board = _blocked_work_card(conn, monkeypatch)
    board.unblock_error = _hermes_error("unblock")

    with pytest.raises(hermes.HermesCommandError):
        questions.answer_question("b", "w_1", "Use sqlite.", conn=conn)

    assert board.names() == ["show", "comment", "unblock"]  # one of each: no retry of either
    assert len(board.cards["w_1"]["_comments"]) == 1  # the answer is on the card, which is still blocked
    assert board.cards["w_1"]["status"] == "blocked"
    assert _payloads(conn, "question_answered") == []


def test_the_answered_event_carries_the_card_task_and_length_but_never_the_text(conn, monkeypatch):
    _blocked_work_card(conn, monkeypatch)
    text = "  Use sqlite, the file is data/app.db.  "

    questions.answer_question("b", "w_1", text, conn=conn)

    (row,) = [r for r in events.recent(conn) if r["kind"] == "question_answered"]
    assert json.loads(row["payload"]) == {"card_id": "w_1", "task_key": "T1", "chars": len(text)}
    assert "sqlite" not in row["payload"]


def test_the_answered_question_is_returned_with_the_reason_it_replied_to(conn, monkeypatch):
    _seed_task(conn, "T1", "w_1", "m_1")
    FakeBoard(monkeypatch, _card("w_1", "T1: scaffold", events=[_blocked("old", 100), _blocked("new", 200)]))

    answered = questions.answer_question("b", "w_1", "Use sqlite.", conn=conn)

    assert answered == Question(
        card_id="w_1", title="T1: scaffold", task_key="T1", card_kind="work", assignee="coder-1",
        question="new", asked_at=200,
    )


def test_a_card_that_belongs_to_no_plan_task_is_answered_with_no_task_key(conn, monkeypatch):
    _seed_task(conn, "T1", "w_1", "m_1")
    FakeBoard(monkeypatch, _card("x_1", "a stray card", events=[_blocked("who owns this?")]))

    answered = questions.answer_question("b", "x_1", "Nobody.", conn=conn)

    assert (answered.task_key, answered.card_kind) == (None, "other")
    (payload,) = _payloads(conn, "question_answered")
    assert payload["task_key"] is None


def test_answering_finds_the_task_of_a_merge_card_and_of_a_current_fix_card(conn, monkeypatch):
    _seed_task(conn, "T1", "f_1", "m_1")
    FakeBoard(
        monkeypatch,
        _card("m_1", "T1: merge", assignee=None, events=[_blocked("budget spent")]),
        _card("f_1", "T1: fix (round 1)", parents=["w_1"], events=[_blocked("which way?")]),
    )

    merge = questions.answer_question("b", "m_1", "Retry it.", conn=conn)
    fix = questions.answer_question("b", "f_1", "Take the second way.", conn=conn)

    assert [(a.card_id, a.task_key, a.card_kind) for a in (merge, fix)] == [
        ("m_1", "T1", "merge"), ("f_1", "T1", "fix"),
    ]
    assert (merge.assignee, fix.assignee) == (None, "coder-1")


def test_answering_is_not_scoped_to_a_plan_so_another_projects_card_can_be_answered_by_id(conn, monkeypatch):
    _seed_task(conn, "T1", "w_1", "m_1")
    _seed_task(conn, "T5", "o_w", "o_m", project="other")
    board = FakeBoard(monkeypatch, _card("o_w", "T5: theirs", events=[_blocked("their question?")]))

    answered = questions.answer_question("b", "o_w", "Their answer.", conn=conn)

    assert (answered.task_key, answered.card_kind) == ("T5", "work")
    assert board.names() == ["show", "comment", "unblock"]


def test_a_card_the_dispatcher_gave_up_on_is_answered_and_unblocked_which_resets_its_failure_counter(conn, monkeypatch):
    _seed_task(conn, "T1", "w_1", "m_1")
    board = FakeBoard(monkeypatch, _card("w_1", "T1: scaffold", events=[_gave_up(3, "boom", 1500)]))

    answered = questions.answer_question("b", "w_1", "Use the other provider.", conn=conn)

    assert (answered.source, answered.question, answered.asked_at) == (
        "gave_up", "gave up after 3 failure(s): boom", 1500,
    )
    assert board.calls == [
        ("show", "w_1"),
        ("comment", "w_1", "ANSWER: Use the other provider.", "user"),
        ("unblock", "w_1", "answered by user"),
    ]
    assert board.cards["w_1"]["status"] == "ready"
    assert _list(conn) == []


def test_a_triage_card_gets_the_answer_as_a_comment_and_then_a_refusal_that_says_what_is_left(conn, monkeypatch):
    _seed_task(conn, "T1", "w_1", "m_1")
    board = FakeBoard(monkeypatch, _card("w_1", "T1: looped", status="triage", events=[_loop("Which port?", 1500)]))

    with pytest.raises(QuestionError) as refused:
        questions.answer_question("b", "w_1", "Use 8080, the file is data/app.db.", conn=conn)

    message = str(refused.value)
    assert message.isascii() and "triage" in message
    assert "hermes kanban specify w_1" in message and "auxiliary model call" in message and "re-plan" in message
    assert "8080" not in message and "data/app.db" not in message            # the answer is never echoed back
    assert board.calls == [
        ("show", "w_1"), ("comment", "w_1", "ANSWER: Use 8080, the file is data/app.db.", "user"),
    ]                                                                        # the answer is safe, nothing is unblocked
    assert board.cards["w_1"]["status"] == "triage"
    assert events.recent(conn) == []
    assert _list(conn) == []             # the answer is on the card, so the question reads as answered


def test_a_triage_card_only_ases_asked_about_is_answered_the_same_way_without_claiming_an_unblock_loop(
    conn, monkeypatch,
):
    board = FakeBoard(monkeypatch, _card("w_1", "T1: a", status="triage", comments=[_asked("Retry or stop?", 1500)]))

    with pytest.raises(QuestionError) as refused:
        questions.answer_question("b", "w_1", "Retry it.", conn=conn)

    assert "unblock loop" not in str(refused.value) and "hermes kanban specify w_1" in str(refused.value)
    assert board.calls == [("show", "w_1"), ("comment", "w_1", "ANSWER: Retry it.", "user")]


def test_a_triage_card_that_nobody_asked_anything_on_has_no_open_question(conn, monkeypatch):
    board = FakeBoard(monkeypatch, _card("w_1", "T1: proposed", status="triage", events=[_created()]))

    with pytest.raises(
        QuestionError,
        match=r"card w_1 has no open question \(it is in triage, but no unblock loop or question was recorded\)",
    ):
        questions.answer_question("b", "w_1", "Go ahead.", conn=conn)

    assert board.names() == ["show"]


def test_a_card_whose_question_was_answered_but_is_still_blocked_says_so_and_names_the_unblock(conn, monkeypatch):
    # The answer landed and the unblock did not (or somebody answered by hand): swarm questions no longer lists the
    # card, and a second answer must not go on piling comments on it, so the person is told what is left to do.
    board = FakeBoard(monkeypatch, _card("w_1", "T1: a", events=[_blocked("q?", 100)], comments=[_answer(at=200)]))

    with pytest.raises(
        QuestionError,
        match=r"already answered, but the card is still blocked: release it with `hermes kanban unblock w_1`",
    ):
        questions.answer_question("b", "w_1", "Again.", conn=conn)

    assert board.names() == ["show"]

    board = FakeBoard(
        monkeypatch, _card("w_2", "T2: a", status="triage", events=[_loop("q?", 100)], comments=[_answer(at=200)]),
    )
    with pytest.raises(QuestionError, match=r"still in triage: only `hermes kanban specify w_2` moves it on"):
        questions.answer_question("b", "w_2", "Again.", conn=conn)
    assert board.names() == ["show"]


def test_a_failed_unblock_leaves_a_card_not_listed_any_more_and_a_second_answer_names_the_way_out(conn, monkeypatch):
    board = _blocked_work_card(conn, monkeypatch)
    board.unblock_error = _hermes_error("unblock")
    with pytest.raises(hermes.HermesCommandError):
        questions.answer_question("b", "w_1", "Use sqlite.", conn=conn)
    assert board.cards["w_1"]["status"] == "blocked" and _list(conn) == []

    with pytest.raises(QuestionError, match="hermes kanban unblock w_1"):
        questions.answer_question("b", "w_1", "Use sqlite.", conn=conn)

    assert [call[0] for call in board.calls].count("comment") == 1        # the answer was not posted a second time


def test_an_answered_question_leaves_the_list_and_a_new_block_is_a_new_question(conn, monkeypatch):
    # The end-to-end step of blueprint 22.2: one card asks a question, and swarm questions and swarm answer
    # unblock it.
    board = _blocked_work_card(conn, monkeypatch, reason="Which database?", at=1000)
    assert [q.question for q in _list(conn)] == ["Which database?"]

    questions.answer_question("b", "w_1", "Use sqlite.", conn=conn)

    assert board.cards["w_1"]["status"] == "ready"
    assert [(c["author"], c["body"]) for c in board.cards["w_1"]["_comments"]] == [
        ("user", "ANSWER: Use sqlite."), ("default", "UNBLOCK: answered by user"),   # the CLI's own comment
    ]
    assert _list(conn) == []  # it left the list by itself

    later = board.clock + 100
    board.cards["w_1"]["status"] = "blocked"  # the worker gets stuck again and asks something new
    board.cards["w_1"]["_events"].append(_blocked("Which port?", later))
    assert [(q.question, q.asked_at) for q in _list(conn)] == [("Which port?", later)]


# ---------------------------------------------------------------------------------------------------------
# Through the real hermes wrappers: only the subprocess layer is faked
# ---------------------------------------------------------------------------------------------------------


def _fake_hermes_cli(monkeypatch, listed, shown, *, fail=()):
    """Fake only hermes._run, so the real kanban_list / kanban_show / kanban_block / kanban_comment / kanban_unblock
    run and the argument lists they build can be checked. `listed` is what `kanban list --json` prints (a list, or a
    dict keyed by the --status asked for), `shown` maps a card id to what `kanban show <id> --json` prints (the
    nested shape Hermes really returns), and a verb in `fail` exits 1. Returns the argument lists _run was called
    with, in order."""
    seen = []

    def fake_run(args, **kwargs):
        seen.append(args)
        verb = args[3]  # ["kanban", "--board", <board>, <verb>, ...]
        out = ""
        if verb == "list":
            out = json.dumps(listed.get(args[5], []) if isinstance(listed, dict) else listed)
        elif verb == "show":
            out = json.dumps(shown[args[4]])
        return types.SimpleNamespace(returncode=1 if verb in fail else 0, stdout=out, stderr="cannot " + verb)

    monkeypatch.setattr(hermes, "_run", fake_run)
    return seen


def _real_show(card_id, title, status, reason, at):
    """What `hermes kanban show <id> --json` returns for a card blocked with a reason."""
    return {
        "task": {"id": card_id, "title": title, "status": status, "assignee": "coder-1"},
        "parents": [], "children": [], "runs": [],
        "comments": [{"author": "default", "body": f"BLOCKED: {reason}", "created_at": at}],
        "events": [{"kind": "blocked", "payload": {"reason": reason}, "created_at": at, "run_id": 1}],
        "latest_summary": None,
    }


def test_the_real_show_shape_is_read_and_only_blocked_and_triage_cards_are_asked_for(conn, monkeypatch):
    _seed_task(conn, "T1", "w_1", "m_1")
    listed = {"blocked": [{"id": "w_1", "title": "T1: scaffold", "status": "blocked", "assignee": "coder-1"}]}
    seen = _fake_hermes_cli(
        monkeypatch, listed, {"w_1": _real_show("w_1", "T1: scaffold", "blocked", "Which database?", 1789832318)},
    )

    (question,) = _list(conn)

    assert question == Question("w_1", "T1: scaffold", "T1", "work", "coder-1", "Which database?", 1789832318)
    assert seen == [
        ["kanban", "--board", "b", "list", "--status", "blocked", "--json"],
        ["kanban", "--board", "b", "list", "--status", "triage", "--json"],
        ["kanban", "--board", "b", "show", "w_1", "--json"],
    ]


def test_a_card_the_dispatcher_gave_up_on_is_read_from_the_real_show_shape(conn, monkeypatch):
    """What `hermes kanban show --json` returns for a card the circuit breaker tripped on: a `gave_up` event and a
    comment, and no `blocked` event, which is why a lookup that only knew `blocked` never listed these cards."""
    _seed_task(conn, "T1", "w_1", "m_1")
    listed = {"blocked": [{"id": "w_1", "title": "T1: scaffold", "status": "blocked", "assignee": "coder-1"}]}
    shown = _real_show("w_1", "T1: scaffold", "blocked", "unused", 1789832318)
    shown["events"] = [_gave_up(3, "pid 4242 exited with code 1", 1789832400)]
    shown["comments"] = []
    _fake_hermes_cli(monkeypatch, listed, {"w_1": shown})

    (question,) = _list(conn)

    assert (question.source, question.asked_at) == ("gave_up", 1789832400)
    assert question.question == "gave up after 3 failure(s): pid 4242 exited with code 1"


def test_ask_user_reaches_hermes_as_a_typed_block_or_a_comment_with_the_text_after_a_double_dash(monkeypatch):
    seen = _fake_hermes_cli(monkeypatch, [], {})

    assert questions.ask_user("b", _card("w_1", "T1: a", status="ready"), "-x: which database?") == "blocked"
    assert questions.ask_user("b", _card("m_1", "T1: merge", status="blocked"), "-y: retry it?") == "commented"

    assert seen == [
        ["kanban", "--board", "b", "block", "--kind", "needs_input", "w_1", "--", "-x: which database?"],
        ["kanban", "--board", "b", "comment", "--author", "ases", "m_1", "--", "ASES QUESTION: -y: retry it?"],
    ]


def test_a_block_the_real_cli_exits_1_on_falls_back_to_the_comment(monkeypatch):
    seen = _fake_hermes_cli(monkeypatch, [], {}, fail=("block",))

    assert questions.ask_user("b", _card("w_1", "T1: a", status="ready"), "Which database?") == "commented"

    assert [args[3] for args in seen] == ["block", "comment"]
    assert seen[1][-1] == "ASES QUESTION: Which database?"


def test_the_answer_reaches_hermes_as_a_comment_and_then_an_unblock_with_the_reason_argument(conn, monkeypatch):
    _seed_task(conn, "T1", "w_1", "m_1")
    seen = _fake_hermes_cli(
        monkeypatch, [], {"w_1": _real_show("w_1", "T1: scaffold", "blocked", "Which database?", 1789832318)},
    )

    questions.answer_question("b", "w_1", "-x: use sqlite.", conn=conn)

    assert seen == [
        ["kanban", "--board", "b", "show", "w_1", "--json"],
        ["kanban", "--board", "b", "comment", "--author", "user", "w_1", "--", "ANSWER: -x: use sqlite."],
        ["kanban", "--board", "b", "unblock", "w_1", "--reason=answered by user"],
    ]


# ---------------------------------------------------------------------------------------------------------
# format_questions
# ---------------------------------------------------------------------------------------------------------


def _q(card_id="w_1", *, title="T1: scaffold", task_key="T1", card_kind="work", assignee="coder-1",
       question="Which database?", asked_at=1000, source="blocked"):
    return Question(card_id, title, task_key, card_kind, assignee, question, asked_at, source)


def test_a_question_defaults_to_the_blocked_source():
    assert Question("w_1", "t", "T1", "work", None, "q?", 0).source == "blocked"


@pytest.mark.parametrize("source, label", [
    ("gave_up", "gave up"), ("ases_comment", "asked by ASES"), ("block_loop", "unblock loop"),
])
def test_the_header_names_a_source_that_is_not_the_default(source, label):
    out = questions.format_questions([_q(source=source)], now=1000 + 12 * 60)

    assert out.splitlines()[0] == f"1. w_1 (task T1, work, {label}, asked 12 min ago) - T1: scaffold"


def test_the_default_source_is_not_named_in_the_header():
    header = questions.format_questions([_q(source="blocked")], now=1000).splitlines()[0]

    assert header == "1. w_1 (task T1, work, asked just now) - T1: scaffold"


def test_no_questions_is_the_single_line_no_open_questions():
    assert questions.format_questions([]) == "No open questions."
    assert questions.format_questions(()) == "No open questions."


def test_a_question_is_a_numbered_header_line_and_the_question_indented_under_it():
    out = questions.format_questions([_q()], now=1000 + 12 * 60)

    assert out == (
        "1. w_1 (task T1, work, asked 12 min ago) - T1: scaffold\n"
        "    Which database?"
    )


def test_questions_are_numbered_and_separated_by_a_blank_line_and_every_question_line_is_indented():
    first = _q("w_1", question="Which database?\n\nSqlite or postgres?")
    second = _q("m_1", title="T1: merge", card_kind="merge", question="Fix-card budget spent.", asked_at=400)

    out = questions.format_questions([first, second], now=4000)

    assert out == (
        "1. w_1 (task T1, work, asked 50 min ago) - T1: scaffold\n"
        "    Which database?\n"
        "\n"
        "    Sqlite or postgres?\n"
        "\n"
        "2. m_1 (task T1, merge, asked 1 h ago) - T1: merge\n"
        "    Fix-card budget spent."
    )


def test_a_card_with_no_task_and_no_title_has_a_short_header():
    out = questions.format_questions([_q("x_1", title="", task_key=None, card_kind="other")], now=1000)

    assert out.splitlines()[0] == "1. x_1 (no task, other, asked just now)"


@pytest.mark.parametrize("elapsed, phrase", [
    (0, "just now"), (59, "just now"), (60, "1 min ago"), (61, "1 min ago"), (3599, "59 min ago"),
    (3600, "1 h ago"), (7199, "1 h ago"), (7200, "2 h ago"), (2 * 86400, "48 h ago"), (-500, "just now"),
])
def test_the_age_is_whole_minutes_under_an_hour_and_whole_hours_after(elapsed, phrase):
    out = questions.format_questions([_q(asked_at=1000)], now=1000 + elapsed)

    assert f"asked {phrase})" in out.splitlines()[0]


def test_a_question_with_no_recorded_time_says_so_instead_of_an_age_from_1970():
    out = questions.format_questions([_q(asked_at=0)], now=1_800_000_000)

    assert "asked at an unknown time)" in out.splitlines()[0]
    assert " h ago" not in out


def test_the_current_time_is_used_when_now_is_not_given(monkeypatch):
    monkeypatch.setattr(questions, "time", types.SimpleNamespace(time=lambda: 20000.0))

    out = questions.format_questions([_q(asked_at=20000 - 3 * 3600)])

    assert "asked 3 h ago)" in out


def test_non_ascii_in_a_title_or_a_question_is_escaped_so_the_output_is_pure_ascii():
    # Named escapes keep this source pure ASCII: an e acute, an arrow, curly quotes, an emoji, a u umlaut.
    title = "T1: caf\N{LATIN SMALL LETTER E WITH ACUTE} \N{RIGHTWARDS ARROW} build"
    question = (
        "Use \N{LEFT DOUBLE QUOTATION MARK}quotes\N{RIGHT DOUBLE QUOTATION MARK}, or an emoji "
        "\N{GRINNING FACE}?\nSecond line \N{LATIN SMALL LETTER U WITH DIAERESIS}."
    )

    out = questions.format_questions([_q(title=title, question=question)], now=1000)

    assert out.isascii()
    out.encode("cp1252")  # what the Windows console would do: must not raise
    assert "caf\\xe9 \\u2192 build" in out
    assert "\\u201cquotes\\u201d" in out and "\\U0001f600" in out
    assert "    Second line \\xfc." in out


def test_control_characters_are_shown_as_text_and_never_reach_the_terminal():
    question = "\x1b[31mred\x1b[0m\x07 bell\rcarriage return\x00 nul"

    out = questions.format_questions([_q(question=question)], now=1000)

    assert out.isascii()
    assert not any(ord(c) < 32 and c != "\n" for c in out)
    assert "\\x1b[31mred\\x1b[0m\\x07 bell" in out
    assert "\\x00 nul" in out
    assert "    carriage return" in out  # a bare carriage return is a line break, not an overwrite


def test_tabs_and_printable_ascii_are_kept_while_delete_and_unit_separator_are_escaped():
    out = questions.format_questions([_q(question="col1\tcol2 ~ tilde \x7f del \x1f unit")], now=1000)

    assert out.splitlines()[1] == "    col1\tcol2 ~ tilde " + chr(92) + "x7f del " + chr(92) + "x1f unit"


def test_a_line_break_in_the_title_or_id_cannot_break_the_header_line():
    out = questions.format_questions([_q("w_\n1", title="T1: two\nlines\tand tab")], now=1000)

    header, question_line = out.splitlines()
    assert header == "1. w_ 1 (task T1, work, asked just now) - T1: two lines and tab"
    assert question_line == "    Which database?"


def test_a_question_is_frozen():
    question = _q()

    with pytest.raises(dataclasses.FrozenInstanceError):
        question.question = "changed"


def test_a_question_error_is_an_exception_that_carries_its_message():
    assert issubclass(QuestionError, Exception)
    assert str(QuestionError("nothing posted")) == "nothing posted"
