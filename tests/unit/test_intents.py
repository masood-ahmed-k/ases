import itertools

import pytest

from ases import db, intents


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "ases.db")


@pytest.fixture
def clock(monkeypatch):
    """A strictly increasing fake clock, so ordering and 'first completion wins' are not at the mercy of two
    calls landing in the same second."""
    ticks = itertools.count(1)
    monkeypatch.setattr(intents, "_now", lambda: f"2026-09-19T00:00:{next(ticks):02d}+00:00")


def _row(conn, intent_id):
    return conn.execute("SELECT * FROM intents WHERE id = ?", (intent_id,)).fetchone()


def test_the_six_kinds_of_section_19_4_are_distinct_constants():
    kinds = {
        intents.KIND_CREATE_CARDS, intents.KIND_RUN_GATE, intents.KIND_BUILD_CANDIDATE,
        intents.KIND_FAST_FORWARD, intents.KIND_COMPLETE_MERGE_CARD, intents.KIND_REVERT,
    }
    assert len(kinds) == 6
    assert all(isinstance(k, str) and k for k in kinds)


def test_begin_writes_an_open_row_and_returns_its_id(conn):
    first = intents.begin(conn, "p1", intents.KIND_FAST_FORWARD, "T1", "candidate abc")
    second = intents.begin(conn, "p1", intents.KIND_RUN_GATE, "T2")

    assert second > first
    row = _row(conn, first)
    assert (row["project"], row["kind"], row["key"], row["detail"]) == ("p1", "fast_forward", "T1", "candidate abc")
    assert row["completed_at"] is None
    # UTC ISO timestamp to the second, like every other ASES record.
    assert row["started_at"].endswith("+00:00")
    assert "." not in row["started_at"]
    assert _row(conn, second)["detail"] is None


def test_open_intents_lists_oldest_first_and_only_open_ones(conn, clock):
    a = intents.begin(conn, "p1", intents.KIND_CREATE_CARDS, "T1")
    b = intents.begin(conn, "p1", intents.KIND_RUN_GATE, "T2", "gate1")
    c = intents.begin(conn, "p1", intents.KIND_REVERT, "T3")
    intents.complete(conn, b)

    listed = intents.open_intents(conn, "p1")

    assert [i["id"] for i in listed] == [a, c]
    assert set(listed[0]) == {"id", "kind", "key", "detail", "started_at"}
    assert listed[0]["kind"] == "create_cards"
    assert listed[0]["key"] == "T1"
    assert listed[0]["started_at"] < listed[1]["started_at"]


def test_open_intents_is_empty_when_nothing_is_open(conn):
    assert intents.open_intents(conn, "p1") == []
    intents.complete(conn, intents.begin(conn, "p1", intents.KIND_RUN_GATE, "T1"))
    assert intents.open_intents(conn, "p1") == []


def test_complete_sets_completed_at_and_can_replace_the_detail(conn):
    plain = intents.begin(conn, "p1", intents.KIND_RUN_GATE, "T1", "started")
    with_detail = intents.begin(conn, "p1", intents.KIND_RUN_GATE, "T2", "started")

    intents.complete(conn, plain)
    intents.complete(conn, with_detail, "gate green")

    assert _row(conn, plain)["completed_at"] is not None
    assert _row(conn, plain)["detail"] == "started"  # no detail given: the original stays
    assert _row(conn, with_detail)["detail"] == "gate green"


def test_completing_twice_keeps_the_first_time_and_detail(conn, clock):
    intent_id = intents.begin(conn, "p1", intents.KIND_FAST_FORWARD, "T1", "before")

    intents.complete(conn, intent_id, "first")
    first_time = _row(conn, intent_id)["completed_at"]
    intents.complete(conn, intent_id, "second")

    row = _row(conn, intent_id)
    assert row["completed_at"] == first_time
    assert row["detail"] == "first"


def test_completing_an_unknown_id_changes_nothing_and_does_not_raise(conn):
    intents.complete(conn, 424242)
    assert conn.execute("SELECT COUNT(*) FROM intents").fetchone()[0] == 0


def test_the_context_manager_completes_when_the_body_finishes(conn):
    with intents.intent(conn, "p1", intents.KIND_BUILD_CANDIDATE, "T1", "building") as intent_id:
        assert _row(conn, intent_id)["completed_at"] is None  # open while the body runs
        assert [i["id"] for i in intents.open_intents(conn, "p1")] == [intent_id]

    assert _row(conn, intent_id)["completed_at"] is not None
    assert intents.open_intents(conn, "p1") == []


def test_the_context_manager_leaves_the_intent_open_when_the_body_raises(conn):
    """An intent left open by an exception is exactly what reconcile-on-start looks for, so the manager must
    neither close it nor swallow the exception."""
    with pytest.raises(RuntimeError, match="boom"):
        with intents.intent(conn, "p1", intents.KIND_FAST_FORWARD, "T1") as intent_id:
            raise RuntimeError("boom")

    assert _row(conn, intent_id)["completed_at"] is None
    assert [i["id"] for i in intents.open_intents(conn, "p1")] == [intent_id]


def test_mark_recovered_completes_and_appends_the_note_to_the_detail(conn):
    intent_id = intents.begin(conn, "p1", intents.KIND_FAST_FORWARD, "T1", "ff abc")

    intents.mark_recovered(conn, intent_id, "reconcile: merge card completed")

    row = _row(conn, intent_id)
    assert row["completed_at"] is not None
    assert row["detail"] == "ff abc | reconcile: merge card completed"
    assert intents.open_intents(conn, "p1") == []


def test_mark_recovered_on_an_intent_with_no_detail_stores_just_the_note(conn):
    intent_id = intents.begin(conn, "p1", intents.KIND_RUN_GATE, "T1")

    intents.mark_recovered(conn, intent_id, "state consistent")

    assert _row(conn, intent_id)["detail"] == "state consistent"


def test_mark_recovered_leaves_an_already_completed_intent_untouched(conn, clock):
    intent_id = intents.begin(conn, "p1", intents.KIND_RUN_GATE, "T1", "gate1")
    intents.complete(conn, intent_id, "green")
    before = tuple(_row(conn, intent_id))

    intents.mark_recovered(conn, intent_id, "should not appear")

    assert tuple(_row(conn, intent_id)) == before


def test_other_projects_intents_are_invisible(conn):
    mine = intents.begin(conn, "p1", intents.KIND_RUN_GATE, "T1")
    theirs = intents.begin(conn, "p2", intents.KIND_RUN_GATE, "T1")

    assert [i["id"] for i in intents.open_intents(conn, "p1")] == [mine]
    assert [i["id"] for i in intents.open_intents(conn, "p2")] == [theirs]
    assert intents.open_intents(conn, "nobody") == []


def test_secret_shaped_text_in_a_detail_is_redacted_before_it_is_stored(conn):
    """ASES-SEC-01: a gate's output or a git error can end up in a detail, and nothing reaches disk unredacted."""
    secret = "sk-" + "a1b2c3d4e5f6a7b8c9d0"
    intent_id = intents.begin(conn, "p1", intents.KIND_RUN_GATE, "T1", f"gate said {secret}")
    other = intents.begin(conn, "p1", intents.KIND_RUN_GATE, "T2", "clean")

    intents.mark_recovered(conn, other, f"then {secret}")

    assert secret not in _row(conn, intent_id)["detail"]
    assert "[redacted]" in _row(conn, intent_id)["detail"]
    assert secret not in _row(conn, other)["detail"]


def test_every_write_is_a_single_autocommit_statement(conn):
    """No BEGIN anywhere: an open transaction on the shared connection would swallow the caller's own writes."""
    intent_id = intents.begin(conn, "p1", intents.KIND_RUN_GATE, "T1")
    assert not conn.in_transaction
    intents.mark_recovered(conn, intent_id, "n")
    assert not conn.in_transaction
    with intents.intent(conn, "p1", intents.KIND_REVERT, "T2"):
        assert not conn.in_transaction
    assert not conn.in_transaction
