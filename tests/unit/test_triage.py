"""triage.py: agent-proposed cards in triage, validated and promoted or archived (ASES-LED-03).

The four hermes kanban functions it uses (list, show, promote, archive) are faked over an in-memory board, and
the database is a temp sqlite file with plan_tasks rows inserted directly, so nothing here touches a real board,
a real card or a provider. Real Hermes 0.21.3 facts this fake matches (read from the installed
hermes-agent source, 2026-09-21, read-only, never run):

  - A dispatcher-spawned WORKER can call the `kanban_create` tool itself, in-process
    (tools/kanban_tools.py:_handle_create, registered with _check_kanban_mode, NOT in
    _ORCHESTRATOR_TOOLS): that is what Appendix C.2's "propose a follow-up card" maps to. ASES never creates
    the card on the worker's behalf, so this module has no propose_card function, only discovery and
    adjudication of what is already sitting in triage.
  - kanban_create does NOT default to landing in triage. hermes_cli/kanban_db_graph.py:initial_task_state
    checks `initial_status` only for the literal string "blocked"; a card lands in triage only when the
    caller's OWN kanban_create call passes the separate boolean `triage=True`. hermes_cli/kanban_db.py's
    VALID_INITIAL_STATUSES = {"running", "blocked"} does not even include "triage" as a legal initial_status
    value. See test_the_real_worker_side_proposal_mechanism_is_documented below.
"""
import copy
import json

import pytest

from ases import db, events, hermes, plan as plan_mod, recovery, triage
from ases.triage import Decision, TriageCard, TriageError, ValidationResult

ROLES = {"lead", "coder-1", "reviewer"}
REASONABLE_BODY = "The auth endpoint has no rate limiting; a worker should add one before this ships."


def _plan(*keys, project="p1", gate_profiles=None):
    return plan_mod.parse_and_validate({
        "project": project,
        "integration_branch": "integration",
        "gate_profiles": gate_profiles or {"trivial": ["echo ok"]},
        "tasks": [
            {"key": key, "title": "task", "role": "coder-1", "depends_on": [], "touches": [f"f{i}.py"],
             "acceptance": ["exists"], "gate_profile": "trivial", "estimated_requests": 5}
            for i, key in enumerate(keys)
        ],
    }, known_roles=ROLES, max_cards=40)


PLAN = _plan("T1", "T2")


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "ases.db")


def _seed_task(conn, key, work_card_id, merge_card_id, project="p1"):
    conn.execute(
        "INSERT INTO plan_tasks (project, task_key, work_card_id, merge_card_id, role, created_at) "
        "VALUES (?, ?, ?, ?, 'coder-1', datetime('now'))",
        (project, key, work_card_id, merge_card_id),
    )


def _loop(reason, at=100):
    """The `block_loop_detected` event a repeated block writes when it sends a card to triage (questions.py's
    signal for "this triage card is a question, not a proposal")."""
    return {"kind": "block_loop_detected", "run_id": None, "created_at": at, "payload": {
        "reason": reason, "kind": "needs_input", "recurrences": 2, "source_status": "ready", "limit": 2,
    }}


def _card(
    card_id, title, *, status="triage", body=REASONABLE_BODY, created_by="coder-1", created_at=1000,
    events=(), parents=(),
):
    """One card as hermes.kanban_show returns it (the flat task plus the underscore keys), landed the way a
    worker's own `kanban_create(triage=true, ...)` call would leave it."""
    return {
        "id": card_id, "title": title, "body": body, "status": status, "created_by": created_by,
        "created_at": created_at,
        "_events": list(events), "_parents": list(parents), "_children": [], "_runs": [],
        "_comments": [], "_latest_summary": None,
    }


class FakeBoard:
    """hermes.kanban_list, kanban_show, kanban_promote and kanban_archive over a dict of cards, installed with
    monkeypatch. `calls` records every call in order.

    kanban_promote moves a card to "ready" (Hermes: "Promote a todo/blocked card to ready"); kanban_archive
    moves every card given to "archived" (soft: the card stays in `cards`, matching hermes.kanban_archive's own
    "Hermes keeps them"). Fill `unreadable` or `list_error`/`promote_error`/`archive_error` to make Hermes
    misbehave."""

    def __init__(self, monkeypatch, *cards):
        self.cards = {card["id"]: card for card in cards}
        self.calls = []
        self.unreadable = set()
        self.list_error = None
        self.promote_error = None
        self.archive_error = None
        for name in ("kanban_list", "kanban_show", "kanban_promote", "kanban_archive"):
            monkeypatch.setattr(hermes, name, getattr(self, name))

    def kanban_list(self, board, *, status=None, assignee=None):
        self.calls.append(("list", status))
        if self.list_error:
            raise self.list_error
        return [
            {"id": c["id"], "title": c["title"], "status": c["status"]}
            for c in self.cards.values() if status is None or c["status"] == status
        ]

    def kanban_show(self, board, card_id):
        self.calls.append(("show", card_id))
        if card_id in self.unreadable or card_id not in self.cards:
            raise hermes.HermesCommandError(["kanban", "show", card_id], 1, "no such card")
        return copy.deepcopy(self.cards[card_id])

    def kanban_promote(self, board, card_id, reason=None):
        self.calls.append(("promote", card_id, reason))
        if self.promote_error:
            raise self.promote_error
        self.cards[card_id]["status"] = "ready"

    def kanban_archive(self, board, card_ids):
        self.calls.append(("archive", list(card_ids)))
        if self.archive_error:
            raise self.archive_error
        for card_id in card_ids:
            if card_id in self.cards:
                self.cards[card_id]["status"] = "archived"

    def names(self):
        return [call[0] for call in self.calls]


def _list(conn, plan=PLAN):
    return triage.list_triage_cards("b", plan, conn=conn)


def _payloads(conn, kind):
    return [json.loads(row["payload"]) for row in events.recent(conn, limit=100) if row["kind"] == kind]


# ---------------------------------------------------------------------------------------------------------
# list_triage_cards
# ---------------------------------------------------------------------------------------------------------


def test_a_genuinely_proposed_card_is_listed_with_the_right_fields(conn, monkeypatch):
    _seed_task(conn, "T1", "w_1", "m_1")
    board = FakeBoard(monkeypatch, _card(
        "x_1", "T1: add rate limiting", parents=["w_1"], created_by="coder-1", created_at=1500,
    ))

    result = _list(conn)

    assert result == [TriageCard(
        card_id="x_1", title="T1: add rate limiting", body=REASONABLE_BODY, proposed_by="coder-1",
        raised_by_task="T1", created_at=1500,
    )]
    assert board.calls == [("list", "triage"), ("show", "x_1")]


def test_a_block_loop_detected_triage_card_is_a_question_not_a_proposal_and_is_excluded(conn, monkeypatch):
    _seed_task(conn, "T1", "w_1", "m_1")
    FakeBoard(monkeypatch, _card("w_1", "T1: looped", events=[_loop("Which port?", 300)]))

    assert _list(conn) == []


def test_a_plain_triage_card_with_no_signal_at_all_is_listed(conn, monkeypatch):
    # No events, no comments: exactly test 22.15's case, "A card proposed by an agent must stay in triage
    # until it is validated."
    _seed_task(conn, "T1", "w_1", "m_1")
    FakeBoard(monkeypatch, _card("x_1", "T1: proposal", parents=["w_1"]))

    (proposal,) = _list(conn)

    assert proposal.card_id == "x_1"


def test_a_triage_card_of_another_project_that_reuses_the_task_key_is_excluded(conn, monkeypatch):
    _seed_task(conn, "T1", "w_1", "m_1")
    _seed_task(conn, "T1", "o_w", "o_m", project="other")
    board = FakeBoard(
        monkeypatch,
        _card("x_1", "T1: mine", parents=["w_1"]),
        _card("x_2", "T1: theirs by own id claim", parents=["o_w"]),
    )

    result = _list(conn)

    assert [c.card_id for c in result] == ["x_1"]
    assert ("show", "x_2") in board.calls  # it was read (parents are only known after the show)


def test_a_triage_card_claimed_by_its_own_id_in_another_projects_rows_is_excluded(conn, monkeypatch):
    _seed_task(conn, "T1", "w_1", "m_1")
    _seed_task(conn, "T2", "o_1", "o_m", project="other")
    FakeBoard(monkeypatch, _card("o_1", "T1: not really ours"))

    assert _list(conn) == []


def test_an_unattributable_proposal_is_still_listed_with_no_raised_by_task(conn, monkeypatch):
    # Deliberate design choice (documented in triage.list_triage_cards): unlike questions.list_questions, a
    # proposal that names neither a plan_tasks id/parent nor a recognised title prefix is still surfaced,
    # because section 12.4 requires every agent-proposed card to be validated and promoted or archived, and a
    # fresh kanban_create call is not required to title itself with a task-key prefix or link parents=[self].
    _seed_task(conn, "T1", "w_1", "m_1")
    FakeBoard(monkeypatch, _card("x_9", "Add a health check endpoint"))

    (proposal,) = _list(conn)

    assert (proposal.card_id, proposal.raised_by_task) == ("x_9", None)


def test_the_longest_task_key_wins_when_one_key_is_a_prefix_of_another(conn, monkeypatch):
    _seed_task(conn, "T1", "w_1", "m_1")
    _seed_task(conn, "T1:B", "w_2", "m_2")
    FakeBoard(monkeypatch, _card("x_1", "T1:B: nested proposal"), _card("x_2", "T1: plain proposal"))

    result = _list(conn)

    assert {c.card_id: c.raised_by_task for c in result} == {"x_1": "T1:B", "x_2": "T1"}


def test_cards_are_listed_oldest_first_then_by_card_id(conn, monkeypatch):
    _seed_task(conn, "T1", "w_1", "m_1")
    FakeBoard(
        monkeypatch,
        _card("x_2", "T1: b", created_at=300),
        _card("x_1", "T1: a", created_at=100),
        _card("x_3", "T1: c", created_at=100),
    )

    assert [c.card_id for c in _list(conn)] == ["x_1", "x_3", "x_2"]


def test_a_card_that_cannot_be_read_is_skipped_and_traced_and_the_rest_are_still_listed(conn, monkeypatch):
    _seed_task(conn, "T1", "w_1", "m_1")
    board = FakeBoard(monkeypatch, _card("x_1", "T1: unreadable"), _card("x_2", "T1: readable"))
    board.unreadable.add("x_1")

    result = _list(conn)

    assert [c.card_id for c in result] == ["x_2"]
    (traced,) = _payloads(conn, "triage_read_failed")
    assert traced["card_id"] == "x_1"
    assert "HermesCommandError" in traced["error"]


def test_a_failure_of_the_list_itself_is_not_swallowed(conn, monkeypatch):
    board = FakeBoard(monkeypatch)
    board.list_error = hermes.HermesCommandError(["kanban", "list"], 1, "hermes is down")

    with pytest.raises(hermes.HermesCommandError):
        _list(conn)


def test_a_card_that_moved_on_between_the_list_and_the_show_is_not_listed(conn, monkeypatch):
    _seed_task(conn, "T1", "w_1", "m_1")
    board = FakeBoard(monkeypatch, _card("x_1", "T1: a"))
    board.cards["x_1"]["status"] = "ready"  # promoted/archived by someone else between the list and the show
    # kanban_list still reports it under "triage" (a stale listing snapshot), the show reveals it moved on
    monkeypatch.setattr(hermes, "kanban_list", lambda b, *, status=None, assignee=None: [
        {"id": "x_1", "title": "T1: a", "status": "triage"},
    ])

    assert _list(conn) == []


def test_a_card_with_no_id_in_the_listing_is_skipped(conn, monkeypatch):
    FakeBoard(monkeypatch)
    monkeypatch.setattr(hermes, "kanban_list", lambda b, *, status=None, assignee=None: [
        {"title": "T1: no id at all"}, {"id": "", "title": "T1: empty id"},
    ])

    assert _list(conn) == []


# ---------------------------------------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------------------------------------


def _validate(monkeypatch, card, *, plan=None, known_roles=None):
    FakeBoard(monkeypatch, card)
    return triage.validate("b", card["id"], conn=None, plan=plan, known_roles=known_roles)


def test_an_empty_title_is_rejected(monkeypatch):
    result = _validate(monkeypatch, _card("x_1", ""))
    assert result.ok is False
    assert any("title" in p for p in result.problems)


def test_a_blank_body_is_rejected(monkeypatch):
    result = _validate(monkeypatch, _card("x_1", "T1: a", body="   \n  "))
    assert result.ok is False
    assert any("blank" in p for p in result.problems)


def test_a_too_short_body_is_rejected(monkeypatch):
    result = _validate(monkeypatch, _card("x_1", "T1: a", body="x"))
    assert result.ok is False
    assert any("short" in p for p in result.problems)


def test_a_reasonable_plain_english_proposal_is_accepted(monkeypatch):
    result = _validate(monkeypatch, _card("x_1", "T1: add rate limiting", body=REASONABLE_BODY))
    assert result == ValidationResult(ok=True, problems=())


@pytest.mark.parametrize("card_kwargs", [
    {"title": None},
    {"title": 5},
    {"body": None},
    {"body": 5},
    {"body": "{not valid json"},
    {"body": "{}"},
    {"body": '{"role": 5}'},
    {"body": "[1, 2, 3]"},
], ids=["title-none", "title-not-text", "body-none", "body-not-text", "bad-json", "empty-object",
        "role-not-text", "json-not-an-object"])
def test_malformed_input_never_raises(monkeypatch, card_kwargs):
    card = _card("x_1", "T1: a")
    card.update(card_kwargs)
    result = _validate(monkeypatch, card)
    assert isinstance(result, ValidationResult)


def test_a_structured_proposal_with_an_unknown_role_is_rejected(monkeypatch):
    body = json.dumps({"role": "not-a-real-role", "note": "please do this"})
    result = _validate(monkeypatch, _card("x_1", "T1: a", body=body), known_roles=ROLES)
    assert result.ok is False
    assert any("not-a-real-role" in p for p in result.problems)


def test_a_structured_proposal_with_a_known_role_is_accepted(monkeypatch):
    body = json.dumps({"role": "coder-1", "note": "please do this"})
    result = _validate(monkeypatch, _card("x_1", "T1: a", body=body), known_roles=ROLES)
    assert result == ValidationResult(ok=True, problems=())


def test_role_is_not_checked_when_known_roles_is_not_given(monkeypatch):
    body = json.dumps({"role": "definitely-not-a-role"})
    result = _validate(monkeypatch, _card("x_1", "T1: a", body=body))
    assert result.ok is True


def test_a_structured_proposal_with_an_undeclared_gate_profile_is_rejected(monkeypatch):
    body = json.dumps({"gate_profile": "no-such-profile"})
    result = _validate(monkeypatch, _card("x_1", "T1: a", body=body), plan=PLAN)
    assert result.ok is False
    assert any("no-such-profile" in p for p in result.problems)


def test_a_structured_proposal_with_bad_touches_or_acceptance_is_rejected(monkeypatch):
    body = json.dumps({"touches": "not-a-list", "acceptance": []})
    result = _validate(monkeypatch, _card("x_1", "T1: a", body=body))
    assert result.ok is False
    assert len(result.problems) == 2


# ---------------------------------------------------------------------------------------------------------
# promote_card, archive_card, record_decision
# ---------------------------------------------------------------------------------------------------------


def test_promote_card_calls_hermes_then_records_the_decision_and_bumps_lineage(conn, monkeypatch):
    _seed_task(conn, "T1", "w_1", "m_1")
    board = FakeBoard(monkeypatch, _card("x_1", "T1: a"))

    triage.promote_card("b", "x_1", conn=conn, project="p1", raised_by_task="T1", reason="looks good")

    assert board.calls == [("show", "x_1"), ("promote", "x_1", "looks good")]
    assert board.cards["x_1"]["status"] == "ready"
    (decided,) = _payloads(conn, "triage_decision")
    assert decided == {"card_id": "x_1", "decision": "promote", "reason": "looks good", "raised_by_task": "T1"}
    lineage = recovery.load_lineage(conn, "p1", "T1")
    assert lineage.infra_failures == 1


def test_promote_card_with_no_raised_by_task_never_touches_lineage(conn, monkeypatch):
    _seed_task(conn, "T1", "w_1", "m_1")
    FakeBoard(monkeypatch, _card("x_1", "T1: a"))

    triage.promote_card("b", "x_1", conn=conn, project="p1")

    lineage = recovery.load_lineage(conn, "p1", "T1")
    assert lineage.infra_failures == 0
    assert lineage.capability_failures == 0
    assert lineage.review_rounds == 0
    assert lineage.replans == 0


def test_promote_card_refuses_an_invalid_proposal_and_leaves_the_card_untouched(conn, monkeypatch):
    board = FakeBoard(monkeypatch, _card("x_1", "T1: a", body=""))

    with pytest.raises(TriageError, match="x_1"):
        triage.promote_card("b", "x_1", conn=conn, project="p1")

    assert board.names() == ["show"]  # validate's own read only, no promote call
    assert board.cards["x_1"]["status"] == "triage"
    assert _payloads(conn, "triage_decision") == []


def test_promote_card_force_bypasses_a_validation_refusal(conn, monkeypatch):
    board = FakeBoard(monkeypatch, _card("x_1", "T1: a", body=""))

    triage.promote_card("b", "x_1", conn=conn, project="p1", force=True)

    assert board.cards["x_1"]["status"] == "ready"
    assert ("promote", "x_1", None) in board.calls


def test_promote_card_forwards_plan_and_known_roles_to_validate(conn, monkeypatch):
    body = json.dumps({"role": "not-a-role"})
    board = FakeBoard(monkeypatch, _card("x_1", "T1: a", body=body))

    with pytest.raises(TriageError, match="not-a-role"):
        triage.promote_card("b", "x_1", conn=conn, project="p1", known_roles=ROLES)

    assert board.cards["x_1"]["status"] == "triage"


def test_archive_card_calls_hermes_then_records_the_decision(conn, monkeypatch):
    _seed_task(conn, "T1", "w_1", "m_1")
    board = FakeBoard(monkeypatch, _card("x_1", "T1: a"))

    triage.archive_card("b", "x_1", conn=conn, project="p1", raised_by_task="T1", reason="not needed")

    assert board.calls == [("archive", ["x_1"])]
    assert board.cards["x_1"]["status"] == "archived"
    (decided,) = _payloads(conn, "triage_decision")
    assert decided == {"card_id": "x_1", "decision": "archive", "reason": "not needed", "raised_by_task": "T1"}
    assert recovery.load_lineage(conn, "p1", "T1").infra_failures == 1


def test_archive_card_never_validates_first(conn, monkeypatch):
    # Rejecting a bad proposal must always be possible: no validation gate on archive.
    board = FakeBoard(monkeypatch, _card("x_1", "", body=""))

    triage.archive_card("b", "x_1", conn=conn, project="p1")

    assert board.cards["x_1"]["status"] == "archived"


def test_record_decision_normalizes_a_plain_string_decision(conn):
    triage.record_decision(conn, "p1", "x_1", "promote")
    (decided,) = _payloads(conn, "triage_decision")
    assert decided["decision"] == "promote"


def test_record_decision_rejects_an_unknown_decision(conn):
    with pytest.raises(ValueError):
        triage.record_decision(conn, "p1", "x_1", "delete")


def test_record_decision_redacts_a_secret_shaped_reason(conn):
    secret = "sk-abcdefghijklmnopqrstuvwx"
    triage.record_decision(conn, "p1", "x_1", Decision.ARCHIVE, reason=f"leaked key {secret}")
    (decided,) = _payloads(conn, "triage_decision")
    assert secret not in decided["reason"]
    assert "[redacted]" in decided["reason"]


# ---------------------------------------------------------------------------------------------------------
# format_triage
# ---------------------------------------------------------------------------------------------------------


def test_format_triage_with_no_cards():
    assert triage.format_triage([]) == "No proposed cards awaiting validation."


def test_format_triage_is_ascii_and_shows_the_task_title_and_body():
    acute = "\N{LATIN SMALL LETTER E WITH ACUTE}"
    card = TriageCard(
        card_id="x_1", title=f"T1: caf{acute}", body=f"line one\nline two {acute}", proposed_by="coder-1",
        raised_by_task="T1", created_at=1000,
    )

    text = triage.format_triage([card], now=1000 + 30)

    assert text.isascii()
    assert "1. x_1 (raised by task T1, proposed by coder-1, just now)" in text
    assert "- T1: caf" in text
    assert "    line one" in text and "    line two" in text


def test_format_triage_orders_and_numbers_as_given():
    cards = [
        TriageCard("x_1", "a", "body one", None, None, 100),
        TriageCard("x_2", "b", "body two", None, "T1", 200),
    ]

    text = triage.format_triage(cards, now=1_000_000)

    assert text.startswith("1. x_1 (no task found,")
    assert "\n\n2. x_2 (raised by task T1," in text


def test_format_triage_reports_an_unknown_time_when_created_at_is_zero():
    card = TriageCard("x_1", "a", "body", None, None, 0)
    assert "at an unknown time" in triage.format_triage([card])


# ---------------------------------------------------------------------------------------------------------
# The real worker-side proposal mechanism (documented, not exercised: no real Hermes is ever run here)
# ---------------------------------------------------------------------------------------------------------


def test_the_real_worker_side_proposal_mechanism_is_documented():
    """Not exercised against a real Hermes (r6_rules.md: never call a real Hermes). What was verified by
    reading the installed Hermes 0.21.3 source, 2026-09-21, read-only:

    - A dispatcher-spawned WORKER can call `kanban_create` itself, in-process, as a structured tool call:
      tools/kanban_tools.py registers it under the "kanban" toolset with check_fn=_check_kanban_mode
      (`_visible(to_env_worker=True)`), and `kanban_create` is NOT a member of
      `_ORCHESTRATOR_TOOLS = frozenset({"kanban_list", "kanban_unblock"})` (the only two tools hidden from
      task workers), so a plain task worker sees and can call it. This is what Appendix C.2's "propose a
      follow-up card instead of doing it" maps to: a literal tool call the worker makes itself, not a comment
      and not a different tool name.
    - kanban_create does NOT default to landing new cards in triage. hermes_cli/kanban_db_graph.py,
      initial_task_state(conn, parents, initial_status, triage, tenant): checks `initial_status` only for the
      literal string "blocked", then a separate boolean `triage`, then the parents; there is no branch for
      initial_status == "triage" at all. hermes_cli/kanban_db.py:104 sets
      VALID_INITIAL_STATUSES = {"running", "blocked"} (triage is not even a legal initial_status value). A
      card lands in triage only when the worker's OWN kanban_create call passes the separate keyword
      `triage=true` (tools/kanban_tools_schemas.py's KANBAN_CREATE_SCHEMA: "If true, task lands in 'triage'
      instead of 'todo'").

    This is why triage.py has no propose_card helper: the worker creates the proposal itself, already in
    triage, and this module only discovers, validates and adjudicates what is already there."""
    assert triage.list_triage_cards.__doc__  # the finding is written where a reader of the module finds it
