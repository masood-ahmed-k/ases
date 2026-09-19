"""questions.py: the human channel, swarm questions and swarm answer (ASES-REC-05, ASES-SEC-01).

The four hermes kanban functions it uses are faked over an in-memory board and the database is a temp sqlite file
with plan_tasks rows inserted directly, so nothing here touches a real board, a real card or a provider."""
import copy
import dataclasses
import json
import types

import pytest

from ases import db, events, hermes, plan as plan_mod, questions
from ases.questions import Question, QuestionError

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


def _card(card_id, title, *, status="blocked", events=(), parents=(), assignee="coder-1"):
    """One card as hermes.kanban_show returns it (the flat task plus the underscore keys)."""
    return {
        "id": card_id, "title": title, "status": status, "assignee": assignee,
        "_events": list(events), "_parents": list(parents), "_children": [], "_runs": [], "_comments": [],
        "_latest_summary": None,
    }


class FakeBoard:
    """hermes.kanban_list, kanban_show, kanban_comment and kanban_unblock over a dict of cards, installed with
    monkeypatch. `calls` records every call in order. A comment is appended to the card and an unblock moves it
    to ready, as Hermes does. Fill `unreadable`, `stale`, `comment_error` or `unblock_error` to make Hermes
    misbehave: kanban_show raises for an unreadable id, kanban_list reports a stale id as blocked whatever it
    is now, and the write functions raise the given error."""

    def __init__(self, monkeypatch, *cards):
        self.cards = {card["id"]: card for card in cards}
        self.calls = []
        self.unreadable = set()
        self.stale = set()
        self.comment_error = None
        self.unblock_error = None
        for name in ("kanban_list", "kanban_show", "kanban_comment", "kanban_unblock"):
            monkeypatch.setattr(hermes, name, getattr(self, name))

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
        self.cards[card_id]["_comments"].append({"author": author or "default", "body": text, "created_at": 0})

    def kanban_unblock(self, board, card_id, reason=None):
        self.calls.append(("unblock", card_id, reason))
        if self.unblock_error:
            raise self.unblock_error
        self.cards[card_id]["status"] = "ready"

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
        question="Which database?", asked_at=1111,
    )]
    assert board.calls[0] == ("list", "blocked")  # only blocked cards are read, then each one is shown


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


def test_no_blocked_cards_lists_nothing_and_reads_no_card(conn, monkeypatch):
    _seed_task(conn, "T1", "w_1", "m_1")
    board = FakeBoard(monkeypatch, _card("w_1", "T1: a", status="running"))

    assert _list(conn) == []
    assert board.names() == ["list"]


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


def test_an_answered_question_leaves_the_list_and_a_new_block_is_a_new_question(conn, monkeypatch):
    # The end-to-end step of blueprint 22.2: one card asks a question, and swarm questions and swarm answer
    # unblock it.
    board = _blocked_work_card(conn, monkeypatch, reason="Which database?", at=1000)
    assert [q.question for q in _list(conn)] == ["Which database?"]

    questions.answer_question("b", "w_1", "Use sqlite.", conn=conn)

    assert board.cards["w_1"]["status"] == "ready"
    assert board.cards["w_1"]["_comments"] == [{"author": "user", "body": "ANSWER: Use sqlite.", "created_at": 0}]
    assert _list(conn) == []  # it left the list by itself

    board.cards["w_1"]["status"] = "blocked"  # the worker gets stuck again and asks something new
    board.cards["w_1"]["_events"].append(_blocked("Which port?", 3000))
    assert [(q.question, q.asked_at) for q in _list(conn)] == [("Which port?", 3000)]


# ---------------------------------------------------------------------------------------------------------
# Through the real hermes wrappers: only the subprocess layer is faked
# ---------------------------------------------------------------------------------------------------------


def _fake_hermes_cli(monkeypatch, listed, shown):
    """Fake only hermes._run, so the real kanban_list / kanban_show / kanban_comment / kanban_unblock run and the
    argument lists they build can be checked. `listed` is what `kanban list --json` prints, `shown` maps a card id
    to what `kanban show <id> --json` prints (the nested shape Hermes really returns). Returns the argument lists
    _run was called with, in order."""
    seen = []

    def fake_run(args, **kwargs):
        seen.append(args)
        verb = args[3]  # ["kanban", "--board", <board>, <verb>, ...]
        out = ""
        if verb == "list":
            out = json.dumps(listed)
        elif verb == "show":
            out = json.dumps(shown[args[4]])
        return types.SimpleNamespace(returncode=0, stdout=out, stderr="")

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


def test_the_real_show_shape_is_read_and_only_blocked_cards_are_asked_for(conn, monkeypatch):
    _seed_task(conn, "T1", "w_1", "m_1")
    listed = [{"id": "w_1", "title": "T1: scaffold", "status": "blocked", "assignee": "coder-1"}]
    seen = _fake_hermes_cli(
        monkeypatch, listed, {"w_1": _real_show("w_1", "T1: scaffold", "blocked", "Which database?", 1789832318)},
    )

    (question,) = _list(conn)

    assert question == Question("w_1", "T1: scaffold", "T1", "work", "coder-1", "Which database?", 1789832318)
    assert seen == [
        ["kanban", "--board", "b", "list", "--status", "blocked", "--json"],
        ["kanban", "--board", "b", "show", "w_1", "--json"],
    ]


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
       question="Which database?", asked_at=1000):
    return Question(card_id, title, task_key, card_kind, assignee, question, asked_at)


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
