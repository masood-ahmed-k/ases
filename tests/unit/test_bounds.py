"""bounds.py: the section 9.3 global bounds, the project_state row and the definition of finished (ASES-CTL-01).

The database is a temp sqlite file and hermes.kanban_show is faked for every test (an autouse fixture, so no test
can reach a real board), so nothing here touches Hermes. Time is injected through `now=` everywhere except the
tests that say they use the real clock."""
import dataclasses
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from ases import bounds, db, events, gates, hermes, ledger, plan as plan_mod

NOW = datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc)
HEAD = "a" * 40
OLDER_HEAD = "b" * 40
NO_PROVIDERS = {"providers": {}, "models": []}

# Shaped like config/models.yaml: one capped provider (OpenRouter's free tier) and one with no published cap.
MODELS = {
    "providers": {
        "openrouter": {"limits": {"per_day_default": 50, "per_day_after_credits": 1000}, "credits_purchased": False},
        "xkiro": {"limits": {}},
    },
    "models": [],
}


class FakeBoard:
    """hermes.kanban_show over a dict of card id -> card dict, or an exception instance to raise. A card that is not
    in the dict is a plain `ready` card. Every call is recorded as (board, card id) in `shown`."""

    def __init__(self):
        self.cards = {}
        self.shown = []

    def show(self, board, card_id):
        self.shown.append((board, card_id))
        card = self.cards.get(card_id, {"id": card_id, "status": "ready", "_runs": []})
        if isinstance(card, Exception):
            raise card
        return card


@pytest.fixture(autouse=True)
def fake_board(monkeypatch):
    fake = FakeBoard()
    monkeypatch.setattr(hermes, "kanban_show", fake.show)
    return fake


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "ases.db")


def _task(key):
    return plan_mod.PlanTask(
        key=key, title=f"task {key}", role="coder", depends_on=(), touches=(), acceptance=("done",),
        gate_profile="g", estimated_requests=5,
    )


def _plan(count=2, project="p1"):
    return plan_mod.Plan(
        project=project, integration_branch="integration", gate_profiles={"g": ["echo ok"]},
        tasks=tuple(_task(f"T{i}") for i in range(1, count + 1)),
    )


def _seed_tasks(conn, plan, *, fix_cards=None):
    """One plan_tasks row per task: work card w_<key>, merge card m_<key>, and the fix cards spent so far."""
    for task in plan.tasks:
        conn.execute(
            "INSERT INTO plan_tasks (project, task_key, work_card_id, merge_card_id, role, fix_cards, created_at) "
            "VALUES (?, ?, ?, ?, 'coder', ?, datetime('now'))",
            (plan.project, task.key, f"w_{task.key}", f"m_{task.key}", (fix_cards or {}).get(task.key, 0)),
        )


def _set_lineage(conn, task_key, *, review_rounds=0, capability_failures=0, infra_failures=0, project="p1"):
    conn.execute(
        "INSERT INTO lineage (project, task_key, review_rounds, capability_failures, infra_failures, updated_at) "
        "VALUES (?, ?, ?, ?, ?, datetime('now'))",
        (project, task_key, review_rounds, capability_failures, infra_failures),
    )


def _evaluate(conn, plan, *, limits=None, models=None, now=NOW, board="b"):
    return bounds.evaluate_bounds(
        board, plan, limits or bounds.Bounds(), NO_PROVIDERS if models is None else models, conn=conn, now=now,
    )


def _one(statuses, name, subject=None):
    found = [s for s in statuses if s.name == name and (subject is None or s.subject == subject)]
    assert len(found) == 1, found
    return found[0]


def _state(conn, project="p1"):
    return bounds.get_state(conn, project)


def _run(started_at, **extra):
    """One entry of a card's runs list as hermes.kanban_show returns it under "_runs"."""
    return {"id": 1, "profile": "coder-1", "status": "running", "outcome": None, "summary": None, "error": None,
            "metadata": None, "started_at": started_at, "ended_at": None, "worker_pid": 4242, **extra}


def _epoch(moment):
    return moment.timestamp()


def _running(card_id, started, *, status="running"):
    return {"id": card_id, "status": status, "_runs": [_run(_epoch(started))]}


def _make_finished(conn, fake_board, plan, *, head=HEAD):
    """Every condition of is_finished true: merge cards done, both final gates green on `head`, report written."""
    _seed_tasks(conn, plan)
    for task in plan.tasks:
        fake_board.cards[f"m_{task.key}"] = {"id": f"m_{task.key}", "status": "done"}
    bounds.record_final_gate(conn, plan.project, "gate4", head, "pass")
    bounds.record_final_gate(conn, plan.project, "gate5", head, "pass")
    bounds.mark_release_report(conn, plan.project, "docs/ases/release-report.md")


# ---------------------------------------------------------------------------------------------
# Bounds and Bounds.from_budgets
# ---------------------------------------------------------------------------------------------


def test_defaults_are_the_section_9_3_table():
    limits = bounds.Bounds()
    assert limits.attempts_per_card == 3
    assert limits.review_rounds_per_task == 3
    assert limits.fix_cards_per_task == 2
    assert limits.replans_per_project == 2
    assert limits.max_cards == 40
    assert limits.card_runtime_minutes == 45
    assert limits.daily_reserve_percent == 10
    assert limits.project_wall_clock_minutes is None


def test_bounds_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        bounds.Bounds().max_cards = 1


@pytest.mark.parametrize("budgets", [{}, None])
def test_from_budgets_with_nothing_gives_the_defaults(budgets):
    assert bounds.Bounds.from_budgets(budgets) == bounds.Bounds()


def test_from_budgets_reads_every_key_into_its_own_field():
    limits = bounds.Bounds.from_budgets({
        "attempts_per_card": 5, "review_rounds_per_task": 6, "fix_cards_per_task": 7, "replans_per_project": 8,
        "max_cards": 9, "card_runtime_minutes": 10, "daily_reserve_percent": 11, "project_wall_clock_minutes": 12,
    })
    assert limits == bounds.Bounds(5, 6, 7, 8, 9, 10, 11, 12)


@pytest.mark.parametrize("key", [
    "attempts_per_card", "review_rounds_per_task", "fix_cards_per_task", "replans_per_project", "max_cards",
    "card_runtime_minutes", "daily_reserve_percent", "project_wall_clock_minutes",
])
def test_from_budgets_one_override_changes_only_its_field(key):
    limits, defaults = bounds.Bounds.from_budgets({key: 7}), bounds.Bounds()
    changed = {f.name for f in dataclasses.fields(limits) if getattr(limits, f.name) != getattr(defaults, f.name)}
    assert changed == {key}
    assert getattr(limits, key) == 7


def test_from_budgets_ignores_unknown_keys_and_takes_the_repo_config_as_it_is():
    # config/swarm.yaml's budgets: section, including review_reserve_requests, which is not a global bound.
    budgets = {
        "attempts_per_card": 3, "review_rounds_per_task": 3, "fix_cards_per_task": 2, "replans_per_project": 2,
        "max_cards": 40, "card_runtime_minutes": 45, "daily_reserve_percent": 10, "review_reserve_requests": 20,
        "something_new": "not a number",
    }
    assert bounds.Bounds.from_budgets(budgets) == bounds.Bounds()


def test_from_budgets_project_wall_clock_may_be_null_or_a_number():
    assert bounds.Bounds.from_budgets({"project_wall_clock_minutes": None}).project_wall_clock_minutes is None
    assert bounds.Bounds.from_budgets({"project_wall_clock_minutes": 90}).project_wall_clock_minutes == 90


@pytest.mark.parametrize("key", [
    "attempts_per_card", "review_rounds_per_task", "fix_cards_per_task", "replans_per_project", "max_cards",
    "card_runtime_minutes", "daily_reserve_percent", "project_wall_clock_minutes",
])
@pytest.mark.parametrize("bad", ["3", 3.0, True, False, [3], {"n": 3}])
def test_from_budgets_rejects_a_non_integer_naming_the_key(key, bad):
    with pytest.raises(ValueError, match=key):
        bounds.Bounds.from_budgets({key: bad})


@pytest.mark.parametrize("key", [
    "attempts_per_card", "review_rounds_per_task", "fix_cards_per_task", "replans_per_project", "max_cards",
    "card_runtime_minutes", "daily_reserve_percent"])
def test_from_budgets_rejects_null_for_a_key_that_needs_a_number(key):
    with pytest.raises(ValueError, match=key):
        bounds.Bounds.from_budgets({key: None})


@pytest.mark.parametrize("key", ["attempts_per_card", "daily_reserve_percent", "project_wall_clock_minutes"])
def test_from_budgets_rejects_a_negative_number_naming_the_key(key):
    with pytest.raises(ValueError, match=key):
        bounds.Bounds.from_budgets({key: -1})


def test_from_budgets_accepts_zero_and_a_reserve_of_exactly_100():
    limits = bounds.Bounds.from_budgets({"fix_cards_per_task": 0, "daily_reserve_percent": 100})
    assert (limits.fix_cards_per_task, limits.daily_reserve_percent) == (0, 100)


def test_from_budgets_rejects_a_reserve_over_100():
    with pytest.raises(ValueError, match="daily_reserve_percent"):
        bounds.Bounds.from_budgets({"daily_reserve_percent": 101})


def test_from_budgets_rejects_something_that_is_not_a_mapping():
    with pytest.raises(ValueError, match="mapping"):
        bounds.Bounds.from_budgets([("max_cards", 3)])


def test_from_budgets_error_text_is_ascii_even_for_a_non_ascii_value():
    # e-acute and a right arrow, built with chr() so this source file itself stays plain ASCII.
    with pytest.raises(ValueError) as excinfo:
        bounds.Bounds.from_budgets({"max_cards": "caf" + chr(233) + " " + chr(8594) + " forty"})
    assert str(excinfo.value).isascii()


# ---------------------------------------------------------------------------------------------
# Project state
# ---------------------------------------------------------------------------------------------


def test_get_state_is_none_for_a_project_with_no_row(conn):
    assert bounds.get_state(conn, "nobody") is None


def test_start_project_creates_a_running_row(conn):
    state = bounds.start_project(conn, "p1", now=NOW)

    assert state == {
        "project": "p1", "started_at": "2026-09-19T12:00:00+00:00", "deadline_at": None, "replans": 0,
        "status": "running", "stop_reason": None, "updated_at": "2026-09-19T12:00:00+00:00",
    }
    assert _state(conn) == state


def test_start_project_deadline_minutes_sets_the_deadline_that_far_from_now(conn):
    state = bounds.start_project(conn, "p1", deadline_minutes=90, now=NOW)
    assert state["deadline_at"] == "2026-09-19T13:30:00+00:00"


@pytest.mark.parametrize("bad", [0, -5, 1.5, "60", True])
def test_start_project_rejects_a_bad_deadline_and_writes_nothing(conn, bad):
    with pytest.raises(ValueError, match="deadline_minutes"):
        bounds.start_project(conn, "p1", deadline_minutes=bad, now=NOW)
    assert _state(conn) is None


def test_start_project_again_leaves_the_row_exactly_as_it_is(conn):
    first = bounds.start_project(conn, "p1", deadline_minutes=60, now=NOW)

    again = bounds.start_project(conn, "p1", deadline_minutes=999, now=NOW + timedelta(hours=3))

    assert again == first
    assert _state(conn) == first  # started_at, the original deadline and updated_at all untouched


def test_start_project_again_with_no_deadline_anywhere_also_leaves_the_row_alone(conn):
    first = bounds.start_project(conn, "p1", now=NOW)

    again = bounds.start_project(conn, "p1", now=NOW + timedelta(hours=3))

    assert again == first
    assert _state(conn)["updated_at"] == "2026-09-19T12:00:00+00:00"


def test_start_project_fills_the_started_at_of_a_running_row_that_never_had_one(conn):
    bounds.set_status(conn, "p1", "running", now=NOW)
    assert _state(conn)["started_at"] is None

    state = bounds.start_project(conn, "p1", now=NOW + timedelta(minutes=7))

    assert (state["status"], state["started_at"]) == ("running", "2026-09-19T12:07:00+00:00")


def test_start_project_makes_a_paused_project_run_again_without_resetting_the_clock(conn):
    bounds.start_project(conn, "p1", deadline_minutes=60, now=NOW)
    bounds.set_status(conn, "p1", "paused", now=NOW + timedelta(minutes=10))

    state = bounds.start_project(conn, "p1", now=NOW + timedelta(minutes=20))

    assert state["status"] == "running"
    assert state["started_at"] == "2026-09-19T12:00:00+00:00"
    assert state["deadline_at"] == "2026-09-19T13:00:00+00:00"
    assert state["updated_at"] == "2026-09-19T12:20:00+00:00"


def test_start_project_fills_a_started_at_that_a_replan_row_never_had(conn):
    assert bounds.add_replan(conn, "p1", now=NOW) == 1
    assert _state(conn)["started_at"] is None

    state = bounds.start_project(conn, "p1", now=NOW + timedelta(minutes=5))

    assert state["status"] == "running"
    assert state["started_at"] == "2026-09-19T12:05:00+00:00"
    assert state["replans"] == 1


def test_start_project_keeps_a_deadline_set_at_gate_p_before_the_project_started(conn):
    bounds.set_deadline(conn, "p1", "2026-09-20T09:00:00+00:00", now=NOW)

    state = bounds.start_project(conn, "p1", deadline_minutes=30, now=NOW + timedelta(minutes=5))

    assert state["deadline_at"] == "2026-09-20T09:00:00+00:00"
    assert state["started_at"] == "2026-09-19T12:05:00+00:00"


def test_start_project_sets_a_deadline_when_the_row_has_none_and_one_is_asked_for(conn):
    bounds.start_project(conn, "p1", now=NOW)

    state = bounds.start_project(conn, "p1", deadline_minutes=30, now=NOW + timedelta(minutes=10))

    assert state["deadline_at"] == "2026-09-19T12:40:00+00:00"
    assert state["started_at"] == "2026-09-19T12:00:00+00:00"


@pytest.mark.parametrize("status", ["stopped", "finished"])
def test_start_project_refuses_a_stopped_or_finished_project_and_changes_nothing(conn, status):
    bounds.start_project(conn, "p1", deadline_minutes=60, now=NOW)
    bounds.set_status(conn, "p1", status, reason="bound reached", now=NOW + timedelta(minutes=1))
    before = _state(conn)

    with pytest.raises(bounds.StateError, match=status):
        bounds.start_project(conn, "p1", now=NOW + timedelta(minutes=2))

    assert _state(conn) == before


@pytest.mark.parametrize("arrives", ["stopped", "finished"])
def test_start_project_is_not_fooled_by_a_status_that_lands_after_the_status_was_read(conn, monkeypatch, arrives):
    """The refusal is part of the UPDATE, not only of the earlier read: a stop written by another process between
    our read and our write must still win, with the same StateError."""
    bounds.set_status(conn, "p1", "planning", now=NOW)
    real_get_state = bounds.get_state
    reads = []

    def stale_first_read(conn_, project):
        state = real_get_state(conn_, project)
        if not reads:  # the first read is the one start_project acts on: the other write lands right after it
            reads.append(state)
            bounds.set_status(conn_, project, arrives, "kill switch", now=NOW)
        return state

    monkeypatch.setattr(bounds, "get_state", stale_first_read)

    with pytest.raises(bounds.StateError, match=arrives):
        bounds.start_project(conn, "p1", now=NOW + timedelta(minutes=1))

    assert reads[0]["status"] == "planning"  # what start_project saw: it acted on a stale status
    assert real_get_state(conn, "p1")["status"] == arrives
    assert real_get_state(conn, "p1")["started_at"] is None  # nothing was started


def test_set_deadline_creates_a_planning_row_with_no_start(conn):
    bounds.set_deadline(conn, "p1", "2026-09-20T09:00:00+00:00", now=NOW)

    assert _state(conn) == {
        "project": "p1", "started_at": None, "deadline_at": "2026-09-20T09:00:00+00:00", "replans": 0,
        "status": "planning", "stop_reason": None, "updated_at": "2026-09-19T12:00:00+00:00",
    }


def test_set_deadline_stores_utc_whatever_offset_it_was_given(conn):
    bounds.set_deadline(conn, "p1", "2026-09-20T11:00:00+02:00", now=NOW)
    assert _state(conn)["deadline_at"] == "2026-09-20T09:00:00+00:00"


def test_set_deadline_reads_a_timestamp_with_no_offset_as_utc(conn):
    bounds.set_deadline(conn, "p1", "2026-09-20T09:00:00", now=NOW)
    assert _state(conn)["deadline_at"] == "2026-09-20T09:00:00+00:00"


def test_set_deadline_replaces_an_earlier_deadline_and_touches_nothing_else(conn):
    bounds.start_project(conn, "p1", deadline_minutes=30, now=NOW)
    bounds.add_replan(conn, "p1", now=NOW)

    bounds.set_deadline(conn, "p1", "2026-09-21T00:00:00+00:00", now=NOW + timedelta(hours=1))

    state = _state(conn)
    assert state["deadline_at"] == "2026-09-21T00:00:00+00:00"
    assert (state["status"], state["started_at"], state["replans"]) == ("running", "2026-09-19T12:00:00+00:00", 1)
    assert state["updated_at"] == "2026-09-19T13:00:00+00:00"


@pytest.mark.parametrize("bad", ["", "soon", "2026-13-45", None, 12.5e300, ["2026-09-20"]])
def test_set_deadline_rejects_something_that_is_not_a_timestamp_and_writes_nothing(conn, bad):
    with pytest.raises(ValueError, match="deadline"):
        bounds.set_deadline(conn, "p1", bad, now=NOW)
    assert _state(conn) is None


@pytest.mark.parametrize("bad", ["done", "RUNNING", "Stopped", "", None, 3])
def test_set_status_rejects_an_unknown_status_and_writes_nothing(conn, bad):
    with pytest.raises(ValueError, match="status"):
        bounds.set_status(conn, "p1", bad, now=NOW)
    assert _state(conn) is None


def test_the_five_statuses_are_planning_running_paused_stopped_finished():
    assert bounds.STATUSES == ("planning", "running", "paused", "stopped", "finished")


@pytest.mark.parametrize("status", ["planning", "running", "paused", "stopped", "finished"])
def test_set_status_accepts_each_status_and_creates_the_row(conn, status):
    bounds.set_status(conn, "p1", status, now=NOW)

    state = _state(conn)
    assert state["status"] == status
    assert (state["started_at"], state["deadline_at"], state["replans"]) == (None, None, 0)
    assert state["updated_at"] == "2026-09-19T12:00:00+00:00"


def test_set_status_records_the_reason_for_a_stop(conn):
    bounds.set_status(conn, "p1", "stopped", "replans_per_project reached: 2 of 2", now=NOW)
    assert _state(conn)["stop_reason"] == "replans_per_project reached: 2 of 2"


def test_set_status_records_the_reason_for_a_pause(conn):
    """ASES-CTL-01, the register's known gap: bounds.set_status(paused) used to drop the reason, kept only in a
    project_paused event. It is now part of project_state like a stop's reason is, so swarm status/report and the
    stop/resume path can show it without going back to the events table."""
    bounds.set_status(conn, "p1", "paused", "project_wall_clock_minutes reached: 240 of 240", now=NOW)
    assert _state(conn)["stop_reason"] == "project_wall_clock_minutes reached: 240 of 240"


@pytest.mark.parametrize("status", ["planning", "running", "finished"])
def test_set_status_stores_a_reason_only_for_stopped_or_paused(conn, status):
    bounds.set_status(conn, "p1", status, "a reason", now=NOW)
    assert _state(conn)["stop_reason"] is None


def test_set_status_stopped_without_a_reason_stores_none(conn):
    bounds.set_status(conn, "p1", "stopped", now=NOW)
    assert _state(conn)["stop_reason"] is None


def test_set_status_paused_without_a_reason_stores_none(conn):
    bounds.set_status(conn, "p1", "paused", now=NOW)
    assert _state(conn)["stop_reason"] is None


def test_set_status_moving_off_stopped_clears_the_old_reason(conn):
    bounds.set_status(conn, "p1", "stopped", "wall clock", now=NOW)
    bounds.set_status(conn, "p1", "running", now=NOW + timedelta(minutes=1))
    assert _state(conn)["stop_reason"] is None


def test_set_status_moving_off_paused_clears_the_old_reason(conn):
    bounds.set_status(conn, "p1", "paused", "wall clock", now=NOW)
    bounds.set_status(conn, "p1", "running", now=NOW + timedelta(minutes=1))
    assert _state(conn)["stop_reason"] is None


def test_set_status_replaces_the_reason_of_an_earlier_stop(conn):
    bounds.set_status(conn, "p1", "stopped", "first", now=NOW)
    bounds.set_status(conn, "p1", "stopped", "second", now=NOW + timedelta(minutes=1))
    assert _state(conn)["stop_reason"] == "second"


def test_set_status_replaces_the_reason_of_an_earlier_pause(conn):
    bounds.set_status(conn, "p1", "paused", "first", now=NOW)
    bounds.set_status(conn, "p1", "paused", "second", now=NOW + timedelta(minutes=1))
    assert _state(conn)["stop_reason"] == "second"


def test_set_status_switching_between_stopped_and_paused_carries_the_new_reason(conn):
    """Both statuses use the same stop_reason column, so going from one to the other must replace it, not merge or
    keep the old one around."""
    bounds.set_status(conn, "p1", "stopped", "kill switch", now=NOW)
    bounds.set_status(conn, "p1", "paused", "wall clock reached", now=NOW + timedelta(minutes=1))
    assert (_state(conn)["status"], _state(conn)["stop_reason"]) == ("paused", "wall clock reached")


def test_set_status_leaves_started_at_the_deadline_and_the_replan_count_alone(conn):
    bounds.start_project(conn, "p1", deadline_minutes=60, now=NOW)
    bounds.add_replan(conn, "p1", now=NOW)

    bounds.set_status(conn, "p1", "paused", now=NOW + timedelta(minutes=5))

    state = _state(conn)
    assert state["status"] == "paused"
    assert state["started_at"] == "2026-09-19T12:00:00+00:00"
    assert state["deadline_at"] == "2026-09-19T13:00:00+00:00"
    assert state["replans"] == 1
    assert state["updated_at"] == "2026-09-19T12:05:00+00:00"


def test_add_replan_from_no_row_creates_a_planning_row_and_returns_one(conn):
    assert bounds.add_replan(conn, "p1", now=NOW) == 1

    state = _state(conn)
    assert (state["replans"], state["status"], state["started_at"]) == (1, "planning", None)


def test_add_replan_counts_up_and_returns_the_new_total(conn):
    assert [bounds.add_replan(conn, "p1", now=NOW) for _ in range(3)] == [1, 2, 3]
    assert _state(conn)["replans"] == 3


def test_add_replan_is_per_project_and_leaves_the_status_alone(conn):
    bounds.start_project(conn, "p1", now=NOW)

    bounds.add_replan(conn, "p1", now=NOW)
    bounds.add_replan(conn, "p1", now=NOW)
    bounds.add_replan(conn, "p2", now=NOW)

    assert (_state(conn, "p1")["replans"], _state(conn, "p1")["status"]) == (2, "running")
    assert _state(conn, "p2")["replans"] == 1


def test_stop_requested_is_false_for_a_project_with_no_row(conn):
    assert bounds.stop_requested(conn, "p1") is False


@pytest.mark.parametrize("status,expected", [
    ("planning", False), ("running", False), ("paused", True), ("stopped", True), ("finished", False),
])
def test_stop_requested_for_each_status(conn, status, expected):
    bounds.set_status(conn, "p1", status, now=NOW)
    assert bounds.stop_requested(conn, "p1") is expected


def test_stop_requested_only_reads_its_own_project(conn):
    bounds.set_status(conn, "p2", "stopped", now=NOW)
    assert bounds.stop_requested(conn, "p1") is False
    assert bounds.stop_requested(conn, "p2") is True


def test_timestamps_are_utc_iso_seconds_whatever_zone_or_precision_now_has(conn):
    aware_elsewhere = datetime(2026, 9, 19, 14, 30, 5, 987654, tzinfo=timezone(timedelta(hours=2)))
    bounds.start_project(conn, "p1", now=aware_elsewhere)
    bounds.start_project(conn, "p2", now=datetime(2026, 9, 19, 12, 0, 0))  # naive: read as UTC

    assert _state(conn, "p1")["started_at"] == "2026-09-19T12:30:05+00:00"
    assert _state(conn, "p2")["started_at"] == "2026-09-19T12:00:00+00:00"


def test_state_helpers_stamp_the_real_utc_time_when_now_is_omitted(conn):
    before = datetime.now(timezone.utc).replace(microsecond=0)
    bounds.start_project(conn, "p1")
    bounds.set_status(conn, "p2", "paused")
    bounds.add_replan(conn, "p3")
    bounds.set_deadline(conn, "p4", "2026-09-20T09:00:00+00:00")
    after = datetime.now(timezone.utc)

    for project in ("p1", "p2", "p3", "p4"):
        assert before <= datetime.fromisoformat(_state(conn, project)["updated_at"]) <= after


# ---------------------------------------------------------------------------------------------
# evaluate_bounds: counters
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("max_cards,breached", [(3, False), (2, True), (1, True)])
def test_cards_per_project_below_at_and_above_the_limit(conn, max_cards, breached):
    status = _one(_evaluate(conn, _plan(count=2), limits=bounds.Bounds(max_cards=max_cards)), bounds.CARDS_PER_PROJECT)
    assert (status.subject, status.used, status.limit, status.breached) == ("project", 2, max_cards, breached)


def test_cards_per_project_counts_tasks_not_cards(conn):
    """A task is two cards (work and merge), but Gate 0's ceiling is on tasks: 40 tasks, not 20."""
    status = _one(_evaluate(conn, _plan(count=40)), bounds.CARDS_PER_PROJECT)
    assert (status.used, status.limit, status.breached) == (40, 40, True)


@pytest.mark.parametrize("spent,breached", [(1, False), (2, True), (3, True)])
def test_fix_cards_per_task_below_at_and_above_the_limit(conn, spent, breached):
    plan = _plan(count=2)
    _seed_tasks(conn, plan, fix_cards={"T1": spent})

    statuses = _evaluate(conn, plan)

    t1 = _one(statuses, bounds.FIX_CARDS_PER_TASK, "T1")
    assert (t1.used, t1.limit, t1.breached) == (spent, 2, breached)
    t2 = _one(statuses, bounds.FIX_CARDS_PER_TASK, "T2")
    assert (t2.used, t2.breached) == (0, False)


@pytest.mark.parametrize("rounds,breached", [(2, False), (3, True), (4, True)])
def test_review_rounds_per_task_below_at_and_above_the_limit(conn, rounds, breached):
    plan = _plan(count=2)
    _set_lineage(conn, "T2", review_rounds=rounds)

    statuses = _evaluate(conn, plan)

    t2 = _one(statuses, bounds.REVIEW_ROUNDS_PER_TASK, "T2")
    assert (t2.used, t2.limit, t2.breached) == (rounds, 3, breached)
    assert _one(statuses, bounds.REVIEW_ROUNDS_PER_TASK, "T1").used == 0


@pytest.mark.parametrize("failures,breached", [(2, False), (3, True), (4, True)])
def test_attempts_are_the_capability_failures_of_the_task_lineage(conn, failures, breached):
    plan = _plan(count=2)
    _set_lineage(conn, "T1", capability_failures=failures)

    statuses = _evaluate(conn, plan)

    t1 = _one(statuses, bounds.ATTEMPTS_PER_CARD, "T1")
    assert (t1.used, t1.limit, t1.breached) == (failures, 3, breached)


def test_infrastructure_failures_do_not_count_as_attempts(conn):
    """Table 31: an infrastructure failure "counts toward attempts only after two in a row", and that
    classification is recovery's. The bound here counts capability failures only."""
    plan = _plan(count=1)
    _set_lineage(conn, "T1", infra_failures=9, review_rounds=0, capability_failures=0)

    assert _one(_evaluate(conn, plan), bounds.ATTEMPTS_PER_CARD, "T1").used == 0


def test_a_missing_lineage_row_and_a_missing_plan_tasks_row_both_count_zero(conn):
    statuses = _evaluate(conn, _plan(count=1))

    for name in (bounds.ATTEMPTS_PER_CARD, bounds.REVIEW_ROUNDS_PER_TASK, bounds.FIX_CARDS_PER_TASK):
        status = _one(statuses, name, "T1")
        assert (status.used, status.breached) == (0, False)


def test_lineage_and_fix_card_counts_are_scoped_to_the_plans_project(conn):
    plan = _plan(count=1, project="p1")
    _set_lineage(conn, "T1", review_rounds=9, capability_failures=9, project="other")
    conn.execute(
        "INSERT INTO plan_tasks (project, task_key, work_card_id, merge_card_id, role, fix_cards, created_at) "
        "VALUES ('other', 'T1', 'x', 'y', 'coder', 9, datetime('now'))"
    )

    statuses = _evaluate(conn, plan)

    for name in (bounds.ATTEMPTS_PER_CARD, bounds.REVIEW_ROUNDS_PER_TASK, bounds.FIX_CARDS_PER_TASK):
        assert _one(statuses, name, "T1").used == 0


def test_a_tasks_limit_comes_from_bounds_not_from_a_hard_coded_default(conn):
    plan = _plan(count=1)
    _set_lineage(conn, "T1", review_rounds=5, capability_failures=6)
    _seed_tasks(conn, plan, fix_cards={"T1": 7})
    limits = bounds.Bounds(attempts_per_card=6, review_rounds_per_task=5, fix_cards_per_task=8)

    statuses = _evaluate(conn, plan, limits=limits)

    assert [(s.name, s.limit, s.breached) for s in statuses[:3]] == [
        (bounds.ATTEMPTS_PER_CARD, 6, True),
        (bounds.REVIEW_ROUNDS_PER_TASK, 5, True),
        (bounds.FIX_CARDS_PER_TASK, 8, False),
    ]


@pytest.mark.parametrize("replans,breached", [(1, False), (2, True), (3, True)])
def test_replans_per_project_below_at_and_above_the_limit(conn, replans, breached):
    for _ in range(replans):
        bounds.add_replan(conn, "p1", now=NOW)

    status = _one(_evaluate(conn, _plan()), bounds.REPLANS_PER_PROJECT)

    assert (status.subject, status.used, status.limit, status.breached) == ("project", replans, 2, breached)


def test_replans_with_no_project_state_row_count_zero(conn):
    status = _one(_evaluate(conn, _plan()), bounds.REPLANS_PER_PROJECT)
    assert (status.used, status.breached) == (0, False)


def test_replans_are_read_for_this_plans_project_only(conn):
    for _ in range(5):
        bounds.add_replan(conn, "other", now=NOW)
    assert _one(_evaluate(conn, _plan(project="p1")), bounds.REPLANS_PER_PROJECT).used == 0


# ---------------------------------------------------------------------------------------------
# evaluate_bounds: provider requests per day
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("used,breached", [(44, False), (45, True), (46, True)])
def test_provider_requests_below_at_and_above_the_limit_minus_the_reserve(conn, used, breached):
    ledger.record_usage(conn, "openrouter", "some-model", n=used)

    status = _one(_evaluate(conn, _plan(), models=MODELS), bounds.PROVIDER_REQUESTS_PER_DAY)

    assert (status.subject, status.used, status.limit, status.breached) == ("openrouter", used, 45, breached)


@pytest.mark.parametrize("cap,percent,expected", [
    (50, 10, 45),    # the blueprint's own example: 50 a day, 10 percent held back
    (50, 0, 50),     # no reserve
    (50, 100, 0),    # the whole cap held back
    (25, 10, 23),    # 2.5 requests held back, rounded down to 2, exactly as ledger.can_afford does
    (1000, 10, 900),
    (1000, 7, 930),
])
def test_provider_limit_is_the_cap_minus_the_reserve_in_whole_requests(conn, cap, percent, expected):
    models = {"providers": {"p": {"limits": {"per_day": cap}}}, "models": []}

    status = _one(_evaluate(conn, _plan(), models=models, limits=bounds.Bounds(daily_reserve_percent=percent)),
                  bounds.PROVIDER_REQUESTS_PER_DAY)

    assert status.limit == expected


def test_provider_limit_matches_what_the_ledger_leaves_usable(conn):
    """The reserve is taken the way ledger.can_afford takes it, so the two never disagree about a day's budget."""
    for cap, percent in ((25, 10), (50, 10), (1000, 7), (33, 15)):
        models = {"providers": {"p": {"limits": {"per_day": cap}}}, "models": []}
        status = _one(_evaluate(conn, _plan(), models=models, limits=bounds.Bounds(daily_reserve_percent=percent)),
                      bounds.PROVIDER_REQUESTS_PER_DAY)
        usable = ledger.can_afford(conn, models["providers"], "p", 1, reserve_percent=percent)
        assert status.limit == usable.limit_today - int(usable.limit_today * percent / 100)


def test_provider_limit_follows_the_credits_purchased_switch(conn):
    models = {"providers": {"openrouter": {
        "limits": {"per_day_default": 50, "per_day_after_credits": 1000}, "credits_purchased": True}}, "models": []}
    assert _one(_evaluate(conn, _plan(), models=models), bounds.PROVIDER_REQUESTS_PER_DAY).limit == 900


def test_provider_requests_sum_across_every_model_on_the_provider(conn):
    ledger.record_usage(conn, "openrouter", "model-a", n=20)
    ledger.record_usage(conn, "openrouter", "model-b", n=25)
    ledger.record_usage(conn, "xkiro", "model-c", n=9999)  # another provider's traffic is not this one's

    status = _one(_evaluate(conn, _plan(), models=MODELS), bounds.PROVIDER_REQUESTS_PER_DAY)

    assert (status.used, status.breached) == (45, True)


def test_a_provider_with_no_cap_gets_no_status(conn):
    ledger.record_usage(conn, "xkiro", "model-c", n=9999)

    statuses = _evaluate(conn, _plan(), models=MODELS)

    assert [s.subject for s in statuses if s.name == bounds.PROVIDER_REQUESTS_PER_DAY] == ["openrouter"]


@pytest.mark.parametrize("models", [None, {}, {"models": []}, {"providers": None}, {"providers": {}}])
def test_no_providers_means_no_provider_statuses(conn, models):
    statuses = bounds.evaluate_bounds("b", _plan(), bounds.Bounds(), models, conn=conn, now=NOW)
    assert [s for s in statuses if s.name == bounds.PROVIDER_REQUESTS_PER_DAY] == []


def test_every_capped_provider_gets_its_own_status(conn):
    models = {"providers": {"a": {"limits": {"per_day": 100}}, "b": {"limits": {"per_day": 20}},
                            "c": {"limits": {}}}, "models": []}
    ledger.record_usage(conn, "b", "m", n=18)

    statuses = [s for s in _evaluate(conn, _plan(), models=models) if s.name == bounds.PROVIDER_REQUESTS_PER_DAY]

    assert [(s.subject, s.used, s.limit, s.breached) for s in statuses] == [("a", 0, 90, False), ("b", 18, 18, True)]


# ---------------------------------------------------------------------------------------------
# evaluate_bounds: wall clock per card
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("minutes,breached", [(44, False), (45, True), (46, True)])
def test_card_wall_clock_below_at_and_above_the_limit(conn, fake_board, minutes, breached):
    plan = _plan(count=1)
    _seed_tasks(conn, plan)
    fake_board.cards["w_T1"] = _running("w_T1", NOW - timedelta(minutes=minutes))

    status = _one(_evaluate(conn, plan), bounds.CARD_WALL_CLOCK)

    assert (status.subject, status.used, status.limit, status.breached) == ("w_T1", minutes, 45, breached)


def test_card_wall_clock_counts_fractions_of_a_minute(conn, fake_board):
    plan = _plan(count=1)
    _seed_tasks(conn, plan)
    fake_board.cards["w_T1"] = _running("w_T1", NOW - timedelta(minutes=44, seconds=59))
    assert _one(_evaluate(conn, plan), bounds.CARD_WALL_CLOCK).breached is False

    fake_board.cards["w_T1"] = _running("w_T1", NOW - timedelta(minutes=45, seconds=1))
    status = _one(_evaluate(conn, plan), bounds.CARD_WALL_CLOCK)
    assert (status.breached, round(status.used, 4)) == (True, round(45 + 1 / 60, 4))


def test_card_wall_clock_uses_the_limit_from_bounds(conn, fake_board):
    plan = _plan(count=1)
    _seed_tasks(conn, plan)
    fake_board.cards["w_T1"] = _running("w_T1", NOW - timedelta(minutes=50))

    status = _one(_evaluate(conn, plan, limits=bounds.Bounds(card_runtime_minutes=60)), bounds.CARD_WALL_CLOCK)

    assert (status.limit, status.breached) == (60, False)


@pytest.mark.parametrize("card_status", ["ready", "todo", "review", "blocked", "scheduled", "done", "triage"])
def test_only_a_running_work_card_has_a_wall_clock(conn, fake_board, card_status):
    plan = _plan(count=1)
    _seed_tasks(conn, plan)
    fake_board.cards["w_T1"] = _running("w_T1", NOW - timedelta(hours=5), status=card_status)

    assert [s for s in _evaluate(conn, plan) if s.name == bounds.CARD_WALL_CLOCK] == []


def test_card_wall_clock_looks_at_work_cards_only_on_the_given_board(conn, fake_board):
    plan = _plan(count=2)
    _seed_tasks(conn, plan)

    _evaluate(conn, plan, board="the-board")

    assert fake_board.shown == [("the-board", "w_T1"), ("the-board", "w_T2")]  # never a merge card


def test_the_current_work_card_is_the_fix_card_once_the_task_was_repointed(conn, fake_board):
    plan = _plan(count=1)
    _seed_tasks(conn, plan, fix_cards={"T1": 1})
    conn.execute("UPDATE plan_tasks SET work_card_id = 'fix_T1' WHERE project = 'p1' AND task_key = 'T1'")
    fake_board.cards["w_T1"] = _running("w_T1", NOW - timedelta(hours=9))  # the outgoing card: not measured
    fake_board.cards["fix_T1"] = _running("fix_T1", NOW - timedelta(minutes=10))

    status = _one(_evaluate(conn, plan), bounds.CARD_WALL_CLOCK)

    assert (status.subject, status.used, status.breached) == ("fix_T1", 10, False)
    assert [card_id for _, card_id in fake_board.shown] == ["fix_T1"]


def test_card_wall_clock_measures_from_the_latest_run_not_the_first_or_the_last_listed(conn, fake_board):
    plan = _plan(count=1)
    _seed_tasks(conn, plan)
    old, latest = _run(_epoch(NOW - timedelta(hours=3))), _run(_epoch(NOW - timedelta(minutes=2)))
    for runs in ([old, latest], [latest, old]):
        fake_board.cards["w_T1"] = {"id": "w_T1", "status": "running", "_runs": runs}

        status = _one(_evaluate(conn, plan), bounds.CARD_WALL_CLOCK)

        assert (status.used, status.breached) == (2, False)


@pytest.mark.parametrize("make", [
    lambda t: t.timestamp(),                            # epoch seconds as a float, as Hermes sessions have it
    lambda t: int(t.timestamp()),                       # as an int
    lambda t: str(int(t.timestamp())),                  # as Hermes hands a run's start over: "1789832318"
    lambda t: t.isoformat(),                            # ISO 8601 with an offset
    lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ"),         # ISO 8601 with a Z
    lambda t: t.replace(tzinfo=None).isoformat(),       # ISO 8601 with no offset: read as UTC
    lambda t: t,                                        # an already parsed datetime
])
def test_card_wall_clock_reads_a_run_start_in_every_shape_hermes_or_asks_use(conn, fake_board, make):
    plan = _plan(count=1)
    _seed_tasks(conn, plan)
    started = NOW - timedelta(minutes=46)
    fake_board.cards["w_T1"] = {"id": "w_T1", "status": "running", "_runs": [_run(make(started))]}

    status = _one(_evaluate(conn, plan), bounds.CARD_WALL_CLOCK)

    assert (status.used, status.breached) == (46, True)


def test_a_start_time_in_the_future_is_zero_minutes_used_never_negative(conn, fake_board):
    plan = _plan(count=1)
    _seed_tasks(conn, plan)
    fake_board.cards["w_T1"] = _running("w_T1", NOW + timedelta(minutes=5))

    status = _one(_evaluate(conn, plan), bounds.CARD_WALL_CLOCK)

    assert (status.used, status.breached) == (0, False)


@pytest.mark.parametrize("failure", [
    hermes.HermesCommandError(["kanban", "show", "w_T1"], 1, "no such card"),
    hermes.HermesNotFound("`hermes` is not on PATH"),
    subprocess.TimeoutExpired(["hermes"], 30),
    RuntimeError("boom"),
    KeyError("task"),
])
def test_a_card_that_cannot_be_read_is_skipped_and_the_others_still_measured(conn, fake_board, failure):
    plan = _plan(count=2)
    _seed_tasks(conn, plan)
    fake_board.cards["w_T1"] = failure
    fake_board.cards["w_T2"] = _running("w_T2", NOW - timedelta(minutes=50))

    statuses = _evaluate(conn, plan)

    wall_clocks = [s for s in statuses if s.name == bounds.CARD_WALL_CLOCK]
    assert [(s.subject, s.breached) for s in wall_clocks] == [("w_T2", True)]


@pytest.mark.parametrize("runs", [
    [],                                                   # no run at all
    None,                                                 # no _runs key value
    [_run(None)],                                         # a run that has not recorded a start
    [_run("yesterday-ish")],                              # a start nobody can read
    [_run(float("nan")), _run(True), _run(["1"])],        # nothing usable in any of them
    ["not a dict"],
])
def test_a_running_card_with_no_readable_run_start_is_not_measured(conn, fake_board, runs):
    plan = _plan(count=1)
    _seed_tasks(conn, plan)
    fake_board.cards["w_T1"] = {"id": "w_T1", "status": "running", "_runs": runs}

    assert [s for s in _evaluate(conn, plan) if s.name == bounds.CARD_WALL_CLOCK] == []


def test_a_task_with_no_work_card_yet_is_not_asked_about(conn, fake_board):
    plan = _plan(count=2)
    _seed_tasks(conn, plan)
    conn.execute("UPDATE plan_tasks SET work_card_id = NULL WHERE task_key = 'T1'")
    conn.execute("UPDATE plan_tasks SET work_card_id = '' WHERE task_key = 'T2'")

    _evaluate(conn, plan)

    assert fake_board.shown == []


def test_a_card_that_kanban_show_returns_as_something_odd_is_skipped(conn, fake_board):
    plan = _plan(count=1)
    _seed_tasks(conn, plan)
    fake_board.cards["w_T1"] = ["not", "a", "card"]

    assert [s for s in _evaluate(conn, plan) if s.name == bounds.CARD_WALL_CLOCK] == []


def test_other_projects_cards_are_not_measured(conn, fake_board):
    plan = _plan(count=1, project="p1")
    conn.execute(
        "INSERT INTO plan_tasks (project, task_key, work_card_id, merge_card_id, role, created_at) "
        "VALUES ('other', 'T1', 'other_w', 'other_m', 'coder', datetime('now'))"
    )
    fake_board.cards["other_w"] = _running("other_w", NOW - timedelta(hours=9))

    assert [s for s in _evaluate(conn, plan) if s.name == bounds.CARD_WALL_CLOCK] == []
    assert fake_board.shown == []


# ---------------------------------------------------------------------------------------------
# evaluate_bounds: project wall clock
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("minutes,breached", [(59, False), (60, True), (61, True)])
def test_project_wall_clock_from_the_configured_minutes_below_at_and_above_the_limit(conn, minutes, breached):
    bounds.start_project(conn, "p1", now=NOW)

    status = _one(
        _evaluate(conn, _plan(), limits=bounds.Bounds(project_wall_clock_minutes=60),
                  now=NOW + timedelta(minutes=minutes)),
        bounds.PROJECT_WALL_CLOCK,
    )

    assert (status.subject, status.used, status.limit, status.breached) == ("project", minutes, 60, breached)


@pytest.mark.parametrize("minutes,breached", [(89, False), (90, True), (91, True)])
def test_project_wall_clock_from_a_deadline_below_at_and_above_it(conn, minutes, breached):
    bounds.start_project(conn, "p1", deadline_minutes=90, now=NOW)

    status = _one(_evaluate(conn, _plan(), now=NOW + timedelta(minutes=minutes)), bounds.PROJECT_WALL_CLOCK)

    assert (status.used, status.limit, status.breached) == (minutes, 90, breached)


def test_project_wall_clock_is_reached_exactly_when_the_deadline_is_even_with_a_fractional_window(conn):
    bounds.start_project(conn, "p1", now=NOW)
    bounds.set_deadline(conn, "p1", "2026-09-19T12:01:30+00:00")  # a 1.5 minute window

    before = _one(_evaluate(conn, _plan(), now=NOW + timedelta(seconds=89)), bounds.PROJECT_WALL_CLOCK)
    at = _one(_evaluate(conn, _plan(), now=NOW + timedelta(seconds=90)), bounds.PROJECT_WALL_CLOCK)

    assert (before.limit, before.breached) == (1.5, False)
    assert at.breached is True


def test_a_deadline_row_wins_over_the_configured_minutes(conn):
    bounds.start_project(conn, "p1", now=NOW)
    bounds.set_deadline(conn, "p1", "2026-09-19T14:00:00+00:00")  # the user extended it to 120 minutes
    limits = bounds.Bounds(project_wall_clock_minutes=60)

    status = _one(_evaluate(conn, _plan(), limits=limits, now=NOW + timedelta(minutes=90)), bounds.PROJECT_WALL_CLOCK)

    assert (status.limit, status.breached) == (120, False)


def test_a_deadline_before_the_start_is_breached_at_once(conn):
    bounds.start_project(conn, "p1", now=NOW)
    bounds.set_deadline(conn, "p1", "2026-09-19T11:00:00+00:00")

    status = _one(_evaluate(conn, _plan(), now=NOW), bounds.PROJECT_WALL_CLOCK)

    assert (status.limit, status.breached) == (0, True)


def test_no_project_wall_clock_status_without_a_limit(conn):
    bounds.start_project(conn, "p1", now=NOW)  # started, but no deadline and no configured minutes
    assert [s for s in _evaluate(conn, _plan(), now=NOW + timedelta(days=30))
            if s.name == bounds.PROJECT_WALL_CLOCK] == []


def test_no_project_wall_clock_status_for_a_project_that_has_not_started(conn):
    limits = bounds.Bounds(project_wall_clock_minutes=1)
    assert [s for s in _evaluate(conn, _plan(), limits=limits) if s.name == bounds.PROJECT_WALL_CLOCK] == []

    bounds.set_deadline(conn, "p1", "2026-09-19T12:30:00+00:00", now=NOW)  # Gate P set a deadline, no start yet
    assert [s for s in _evaluate(conn, _plan(), limits=limits, now=NOW + timedelta(days=1))
            if s.name == bounds.PROJECT_WALL_CLOCK] == []


def test_an_unreadable_started_at_or_deadline_is_treated_as_missing(conn):
    conn.execute(
        "INSERT INTO project_state (project, started_at, deadline_at, replans, status, updated_at) "
        "VALUES ('p1', 'garbage', NULL, 0, 'running', 'x')"
    )
    limits = bounds.Bounds(project_wall_clock_minutes=1)
    assert [s for s in _evaluate(conn, _plan(), limits=limits) if s.name == bounds.PROJECT_WALL_CLOCK] == []

    conn.execute("UPDATE project_state SET started_at = '2026-09-19T12:00:00+00:00', deadline_at = 'garbage'")
    status = _one(_evaluate(conn, _plan(), limits=limits, now=NOW + timedelta(minutes=5)), bounds.PROJECT_WALL_CLOCK)
    assert (status.limit, status.breached) == (1, True)  # falls back to the configured minutes


def test_project_wall_clock_uses_the_real_clock_when_now_is_omitted(conn):
    bounds.start_project(conn, "p1", now=datetime.now(timezone.utc) - timedelta(hours=2))

    statuses = bounds.evaluate_bounds(
        "b", _plan(), bounds.Bounds(project_wall_clock_minutes=60), NO_PROVIDERS, conn=conn,
    )

    status = _one(statuses, bounds.PROJECT_WALL_CLOCK)
    assert status.breached is True
    assert 120 <= status.used < 125


def test_a_naive_now_is_read_as_utc(conn):
    bounds.start_project(conn, "p1", now=NOW)
    limits = bounds.Bounds(project_wall_clock_minutes=60)

    status = _one(
        _evaluate(conn, _plan(), limits=limits, now=datetime(2026, 9, 19, 12, 30)), bounds.PROJECT_WALL_CLOCK,
    )

    assert status.used == 30


def test_the_project_wall_clock_is_read_for_this_plans_project_only(conn):
    bounds.start_project(conn, "other", now=NOW)
    limits = bounds.Bounds(project_wall_clock_minutes=1)
    assert [s for s in _evaluate(conn, _plan(project="p1"), limits=limits, now=NOW + timedelta(hours=1))
            if s.name == bounds.PROJECT_WALL_CLOCK] == []


# ---------------------------------------------------------------------------------------------
# evaluate_bounds: shape of the result
# ---------------------------------------------------------------------------------------------

TABLE_17 = {
    bounds.ATTEMPTS_PER_CARD: "The card blocks; classify and escalate",
    bounds.REVIEW_ROUNDS_PER_TASK: "Escalate to the Lead, then to the user",
    bounds.FIX_CARDS_PER_TASK: "Escalate to the user",
    bounds.REPLANS_PER_PROJECT: "User decision",
    bounds.CARDS_PER_PROJECT: "Gate 0 rejects larger plans",
    bounds.PROVIDER_REQUESTS_PER_DAY: "Park cards until the reset",
    bounds.CARD_WALL_CLOCK: "Hermes terminates and re-queues the card; counts as an attempt",
    bounds.PROJECT_WALL_CLOCK: "Pause and report",
}


def test_every_status_carries_the_table_17_response_word_for_word(conn, fake_board):
    plan = _plan(count=1)
    _seed_tasks(conn, plan)
    fake_board.cards["w_T1"] = _running("w_T1", NOW - timedelta(minutes=1))
    bounds.start_project(conn, "p1", deadline_minutes=60, now=NOW)

    statuses = _evaluate(conn, plan, models=MODELS)

    assert {s.name for s in statuses} == set(TABLE_17)
    for status in statuses:
        assert status.on_reach == TABLE_17[status.name]


def test_the_result_lists_the_bounds_in_table_17_order_one_status_per_subject(conn, fake_board):
    plan = _plan(count=2)
    _seed_tasks(conn, plan)
    fake_board.cards["w_T2"] = _running("w_T2", NOW - timedelta(minutes=1))
    bounds.start_project(conn, "p1", deadline_minutes=60, now=NOW)

    statuses = _evaluate(conn, plan, models=MODELS)

    assert [(s.name, s.subject) for s in statuses] == [
        (bounds.ATTEMPTS_PER_CARD, "T1"), (bounds.ATTEMPTS_PER_CARD, "T2"),
        (bounds.REVIEW_ROUNDS_PER_TASK, "T1"), (bounds.REVIEW_ROUNDS_PER_TASK, "T2"),
        (bounds.FIX_CARDS_PER_TASK, "T1"), (bounds.FIX_CARDS_PER_TASK, "T2"),
        (bounds.REPLANS_PER_PROJECT, "project"),
        (bounds.CARDS_PER_PROJECT, "project"),
        (bounds.PROVIDER_REQUESTS_PER_DAY, "openrouter"),
        (bounds.CARD_WALL_CLOCK, "w_T2"),
        (bounds.PROJECT_WALL_CLOCK, "project"),
    ]


def test_breached_is_exactly_used_at_least_limit_for_every_status(conn, fake_board):
    plan = _plan(count=2)
    _seed_tasks(conn, plan, fix_cards={"T1": 2, "T2": 1})
    _set_lineage(conn, "T1", review_rounds=3, capability_failures=2)
    ledger.record_usage(conn, "openrouter", "m", n=45)
    fake_board.cards["w_T1"] = _running("w_T1", NOW - timedelta(minutes=45))
    bounds.start_project(conn, "p1", deadline_minutes=60, now=NOW)
    bounds.add_replan(conn, "p1", now=NOW)

    statuses = _evaluate(conn, plan, models=MODELS, now=NOW + timedelta(minutes=30))

    assert any(s.breached for s in statuses) and any(not s.breached for s in statuses)
    for status in statuses:
        assert status.breached is (status.used >= status.limit), status


def test_a_bound_status_is_a_frozen_value():
    status = bounds.BoundStatus("max_cards", "project", 1, 2, False, "x")
    with pytest.raises(dataclasses.FrozenInstanceError):
        status.used = 5


def test_evaluate_bounds_writes_nothing(conn, fake_board):
    plan = _plan(count=1)
    _seed_tasks(conn, plan)
    bounds.start_project(conn, "p1", deadline_minutes=60, now=NOW)
    before = [tuple(r) for r in conn.execute("SELECT * FROM project_state")]
    events_before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    _evaluate(conn, plan, models=MODELS, now=NOW + timedelta(hours=5))

    assert [tuple(r) for r in conn.execute("SELECT * FROM project_state")] == before
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == events_before


# ---------------------------------------------------------------------------------------------
# stop_reasons
# ---------------------------------------------------------------------------------------------


def _status(name, subject="project", *, breached=True):
    return bounds.BoundStatus(name, subject, 5 if breached else 0, 5, breached, TABLE_17[name])


def test_stop_reasons_picks_only_the_two_bounds_that_stop_a_project():
    statuses = [_status(name) for name in TABLE_17]  # every bound breached at once

    reasons = bounds.stop_reasons(statuses)

    assert {r.name for r in reasons} == {bounds.REPLANS_PER_PROJECT, bounds.PROJECT_WALL_CLOCK}


def test_the_stopping_bounds_are_replans_and_the_project_wall_clock():
    assert bounds.STOP_BOUNDS == {"replans_per_project", "project_wall_clock_minutes"}


@pytest.mark.parametrize("name", [n for n in TABLE_17 if n not in bounds.STOP_BOUNDS])
def test_a_breached_task_or_card_level_bound_is_an_escalation_not_a_stop_reason(name):
    assert bounds.stop_reasons([_status(name)]) == []


@pytest.mark.parametrize("name", sorted(bounds.STOP_BOUNDS))
def test_a_stopping_bound_that_is_not_breached_is_not_a_stop_reason(name):
    assert bounds.stop_reasons([_status(name, breached=False)]) == []


def test_stop_reasons_keeps_the_order_and_the_statuses_themselves():
    wall = _status(bounds.PROJECT_WALL_CLOCK)
    replans = _status(bounds.REPLANS_PER_PROJECT)
    noise = _status(bounds.FIX_CARDS_PER_TASK, "T1")

    reasons = bounds.stop_reasons([wall, noise, replans])

    assert reasons == [wall, replans]
    assert reasons[0] is wall


def test_stop_reasons_of_nothing_is_nothing():
    assert bounds.stop_reasons([]) == []


def test_stop_reasons_on_a_real_evaluation(conn):
    bounds.start_project(conn, "p1", deadline_minutes=60, now=NOW)
    bounds.add_replan(conn, "p1", now=NOW)
    bounds.add_replan(conn, "p1", now=NOW)
    _set_lineage(conn, "T1", review_rounds=9, capability_failures=9)

    statuses = _evaluate(conn, _plan(), now=NOW + timedelta(hours=2))

    assert [s.name for s in bounds.stop_reasons(statuses)] == [bounds.REPLANS_PER_PROJECT, bounds.PROJECT_WALL_CLOCK]
    assert any(s.breached and s.name == bounds.REVIEW_ROUNDS_PER_TASK for s in statuses)  # breached, but not a stop


# ---------------------------------------------------------------------------------------------
# Final gates
# ---------------------------------------------------------------------------------------------


def _gate_rows(conn):
    return [dict(r) for r in conn.execute(
        "SELECT task_key, gate, commit_sha, result, detail, ran_at FROM gate_runs ORDER BY id"
    )]


def test_record_final_gate_inserts_the_row_gates_run_gate_would(conn):
    bounds.record_final_gate(conn, "p1", "gate4", HEAD, "pass", detail="no findings", now=NOW)

    assert _gate_rows(conn) == [{
        "task_key": "__final__", "gate": "gate4", "commit_sha": HEAD, "result": "pass", "detail": "no findings",
        "ran_at": "2026-09-19T12:00:00+00:00",
    }]


def test_the_final_task_key_and_gates_are_the_documented_ones():
    assert bounds.FINAL_TASK_KEY == "__final__"
    assert bounds.FINAL_GATES == ("gate4", "gate5")


@pytest.mark.parametrize("gate", ["gate1", "gate3", "gate0", "Gate4", "gate4 ", "", None, "gate6"])
def test_record_final_gate_refuses_anything_but_gate4_and_gate5(conn, gate):
    with pytest.raises(ValueError, match="gate"):
        bounds.record_final_gate(conn, "p1", gate, HEAD, "pass", now=NOW)
    assert _gate_rows(conn) == []


@pytest.mark.parametrize("given,stored", [
    ("pass", "pass"), ("fail", "fail"), ("PASS", "pass"), (" Fail ", "fail"), (True, "pass"), (False, "fail"),
])
def test_record_final_gate_takes_pass_or_fail_or_a_bool(conn, given, stored):
    bounds.record_final_gate(conn, "p1", "gate5", HEAD, given, now=NOW)
    assert _gate_rows(conn)[0]["result"] == stored


@pytest.mark.parametrize("bad", ["green", "ok", "", None, 1, 0, "passed"])
def test_record_final_gate_refuses_a_result_that_is_not_pass_or_fail(conn, bad):
    with pytest.raises(ValueError, match="result"):
        bounds.record_final_gate(conn, "p1", "gate4", HEAD, bad, now=NOW)
    assert _gate_rows(conn) == []


@pytest.mark.parametrize("sha", ["", "   ", None])
def test_record_final_gate_refuses_a_blank_commit(conn, sha):
    with pytest.raises(ValueError, match="commit"):
        bounds.record_final_gate(conn, "p1", "gate4", sha, "pass", now=NOW)
    assert _gate_rows(conn) == []


def test_record_final_gate_stores_the_commit_without_surrounding_whitespace(conn):
    bounds.record_final_gate(conn, "p1", "gate4", HEAD + "\n", "pass", now=NOW)
    assert bounds.final_gates_green(conn, HEAD) is False  # only gate4 so far
    assert _gate_rows(conn)[0]["commit_sha"] == HEAD


def test_record_final_gate_redacts_a_secret_quoted_in_the_detail(conn):
    detail = "possible secret added: +token = sk-abcdefghijklmnopqrstuvwx"

    bounds.record_final_gate(conn, "p1", "gate4", HEAD, "fail", detail=detail, now=NOW)

    stored = _gate_rows(conn)[0]["detail"]
    assert "sk-abcdefghijklmnopqrstuvwx" not in stored
    assert "possible secret added" in stored


def test_record_final_gate_with_no_detail_stores_an_empty_one(conn):
    bounds.record_final_gate(conn, "p1", "gate4", HEAD, "pass", now=NOW)
    assert _gate_rows(conn)[0]["detail"] == ""


def test_record_final_gate_records_an_event_naming_the_project(conn):
    bounds.record_final_gate(conn, "p1", "gate5", HEAD, "fail", now=NOW)

    rows = [r for r in events.recent(conn) if r["kind"] == "final_gate_recorded"]
    assert len(rows) == 1
    assert '"project": "p1"' in rows[0]["payload"]
    assert '"gate": "gate5"' in rows[0]["payload"]
    assert HEAD in rows[0]["payload"]
    assert '"result": "fail"' in rows[0]["payload"]


def test_record_final_gate_stamps_the_real_time_when_now_is_omitted(conn):
    before = datetime.now(timezone.utc).replace(microsecond=0)
    bounds.record_final_gate(conn, "p1", "gate4", HEAD, "pass")
    after = datetime.now(timezone.utc)
    assert before <= datetime.fromisoformat(_gate_rows(conn)[0]["ran_at"]) <= after


def test_final_gates_are_not_green_with_no_rows(conn):
    assert bounds.final_gates_green(conn, HEAD) is False


@pytest.mark.parametrize("only", ["gate4", "gate5"])
def test_final_gates_need_both_gates(conn, only):
    bounds.record_final_gate(conn, "p1", only, HEAD, "pass", now=NOW)
    assert bounds.final_gates_green(conn, HEAD) is False


def test_final_gates_are_green_when_both_passed_on_the_commit(conn):
    bounds.record_final_gate(conn, "p1", "gate4", HEAD, "pass", now=NOW)
    bounds.record_final_gate(conn, "p1", "gate5", HEAD, "pass", now=NOW)
    assert bounds.final_gates_green(conn, HEAD) is True


def test_final_gates_do_not_carry_over_from_an_older_commit(conn):
    bounds.record_final_gate(conn, "p1", "gate4", OLDER_HEAD, "pass", now=NOW)
    bounds.record_final_gate(conn, "p1", "gate5", OLDER_HEAD, "pass", now=NOW)

    assert bounds.final_gates_green(conn, OLDER_HEAD) is True
    assert bounds.final_gates_green(conn, HEAD) is False


def test_final_gates_do_not_mix_two_commits(conn):
    bounds.record_final_gate(conn, "p1", "gate4", OLDER_HEAD, "pass", now=NOW)
    bounds.record_final_gate(conn, "p1", "gate5", HEAD, "pass", now=NOW)

    assert bounds.final_gates_green(conn, HEAD) is False
    assert bounds.final_gates_green(conn, OLDER_HEAD) is False


@pytest.mark.parametrize("failing", ["gate4", "gate5"])
def test_a_failed_final_gate_does_not_count(conn, failing):
    for gate in bounds.FINAL_GATES:
        bounds.record_final_gate(conn, "p1", gate, HEAD, "fail" if gate == failing else "pass", now=NOW)
    assert bounds.final_gates_green(conn, HEAD) is False


def test_the_latest_row_for_a_gate_on_a_commit_wins(conn):
    bounds.record_final_gate(conn, "p1", "gate4", HEAD, "fail", now=NOW)
    bounds.record_final_gate(conn, "p1", "gate5", HEAD, "pass", now=NOW)
    assert bounds.final_gates_green(conn, HEAD) is False

    bounds.record_final_gate(conn, "p1", "gate4", HEAD, "pass", now=NOW)   # a re-run went green
    assert bounds.final_gates_green(conn, HEAD) is True

    bounds.record_final_gate(conn, "p1", "gate5", HEAD, "fail", now=NOW)   # and a later one went red again
    assert bounds.final_gates_green(conn, HEAD) is False


def test_a_tasks_own_gate_rows_never_stand_in_for_a_final_gate(conn):
    for gate in ("gate4", "gate5"):
        conn.execute(
            "INSERT INTO gate_runs (task_key, gate, commit_sha, result, detail, ran_at) "
            "VALUES ('T1', ?, ?, 'pass', '', 'x')", (gate, HEAD),
        )
    assert bounds.final_gates_green(conn, HEAD) is False


@pytest.mark.parametrize("head", [None, "", "   "])
def test_no_integration_head_means_no_green_gates(conn, head):
    bounds.record_final_gate(conn, "p1", "gate4", HEAD, "pass", now=NOW)
    bounds.record_final_gate(conn, "p1", "gate5", HEAD, "pass", now=NOW)
    assert bounds.final_gates_green(conn, head) is False


def test_final_gates_green_ignores_whitespace_around_the_head(conn):
    bounds.record_final_gate(conn, "p1", "gate4", HEAD, "pass", now=NOW)
    bounds.record_final_gate(conn, "p1", "gate5", HEAD, "pass", now=NOW)
    assert bounds.final_gates_green(conn, f"{HEAD}\n") is True


# --- schema v7: final_gates_green(project=...) (round 6) -----------------------------------------------------

def test_record_final_gate_now_also_stamps_the_gate_runs_project_column(conn):
    """gate_runs gained a project column (schema v7) after this function was written; it used to record the
    project only in the final_gate_recorded event because the column did not exist yet. It is written to the
    row too now, the same way gates.run_gate writes it, so a project-scoped reader has real data to filter on."""
    bounds.record_final_gate(conn, "p1", "gate4", HEAD, "pass", now=NOW)

    row = conn.execute("SELECT project FROM gate_runs WHERE task_key = ?", (bounds.FINAL_TASK_KEY,)).fetchone()
    assert row["project"] == "p1"


def test_final_gates_green_with_no_project_keeps_the_old_two_argument_behavior(conn):
    """The old, still-default, positional call shape every existing caller (finalgates.py, every test above)
    uses: nothing is filtered by project, matching exactly what this function did before it existed."""
    bounds.record_final_gate(conn, "someone-elses-project", "gate4", HEAD, "pass", now=NOW)
    bounds.record_final_gate(conn, "someone-elses-project", "gate5", HEAD, "pass", now=NOW)

    assert bounds.final_gates_green(conn, HEAD) is True


def test_final_gates_green_scoped_to_a_project_matches_its_own_rows_and_null_legacy_rows(conn):
    bounds.record_final_gate(conn, "p1", "gate4", HEAD, "pass", now=NOW)
    bounds.record_final_gate(conn, "p1", "gate5", HEAD, "pass", now=NOW)
    assert bounds.final_gates_green(conn, HEAD, project="p1") is True

    conn.execute("UPDATE gate_runs SET project = NULL")  # a legacy row, written before schema v7
    assert bounds.final_gates_green(conn, HEAD, project="p1") is True  # NULL still counts


def test_final_gates_green_scoped_to_a_project_never_matches_a_different_projects_rows(conn):
    """Two projects sharing this database, or reusing "__final__" gate rows on the same commit SHA (an
    astronomically unlikely but not impossible collision), must not read each other's final-gate history."""
    bounds.record_final_gate(conn, "p2", "gate4", HEAD, "pass", now=NOW)
    bounds.record_final_gate(conn, "p2", "gate5", HEAD, "pass", now=NOW)

    assert bounds.final_gates_green(conn, HEAD, project="p1") is False
    assert bounds.final_gates_green(conn, HEAD, project="p2") is True


def test_rows_the_gate_runner_itself_writes_for_the_final_task_key_count(conn, tmp_path):
    """gates.run_gate(..., task_key="__final__") is how the gate builder will run Gates 4 and 5, so its rows must
    be exactly what final_gates_green reads (real git, a real throwaway worktree, a command that only echoes)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q", "-b", "integration"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, text=True)
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    for args in (["add", "-A"], ["commit", "-q", "-m", "init"]):
        subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, text=True)
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()

    gates.run_gate(repo, sha, "gate4", ["echo scanned"], conn=conn, task_key=bounds.FINAL_TASK_KEY)
    assert bounds.final_gates_green(conn, sha) is False
    gates.run_gate(repo, sha, "gate5", ["echo smoke"], conn=conn, task_key=bounds.FINAL_TASK_KEY)
    assert bounds.final_gates_green(conn, sha) is True


# ---------------------------------------------------------------------------------------------
# The release report
# ---------------------------------------------------------------------------------------------


def test_no_release_report_until_one_is_marked(conn):
    assert bounds.release_report_written(conn, "p1") is False


def test_marking_the_release_report_records_its_path_in_an_event(conn):
    bounds.mark_release_report(conn, "p1", "docs/ases/release-report.md")

    assert bounds.release_report_written(conn, "p1") is True
    rows = [r for r in events.recent(conn) if r["kind"] == "release_report_written"]
    assert len(rows) == 1
    assert '"path": "docs/ases/release-report.md"' in rows[0]["payload"]
    assert '"project": "p1"' in rows[0]["payload"]


def test_the_release_report_is_per_project(conn):
    bounds.mark_release_report(conn, "p1", "report.md")

    assert bounds.release_report_written(conn, "p1") is True
    assert bounds.release_report_written(conn, "p2") is False


def test_marking_twice_is_fine(conn):
    bounds.mark_release_report(conn, "p1", "report.md")
    bounds.mark_release_report(conn, "p1", "report-v2.md")
    assert bounds.release_report_written(conn, "p1") is True


def test_a_path_object_is_recorded_as_text(conn, tmp_path):
    bounds.mark_release_report(conn, "p1", tmp_path / "report.md")
    assert bounds.release_report_written(conn, "p1") is True


@pytest.mark.parametrize("path", ["", "   ", None])
def test_a_blank_release_report_path_is_refused(conn, path):
    with pytest.raises(ValueError, match="path"):
        bounds.mark_release_report(conn, "p1", path)
    assert bounds.release_report_written(conn, "p1") is False


def test_other_event_kinds_for_the_project_are_not_a_release_report(conn):
    events.record(conn, "release_report_pending", {"project": "p1", "path": "report.md"})
    events.record(conn, "merged", {"project": "p1", "path": "report.md"})
    events.record(conn, "release_report_written", {"project": "someone-else", "path": "report.md"})

    assert bounds.release_report_written(conn, "p1") is False


def test_release_report_written_is_scoped_by_the_new_project_column_and_by_legacy_payloads(conn):
    """events.py package, round 9 (ASES-ARC-03): schema v7's project column, once mark_release_report starts
    filling it, must keep the SAME per-project isolation this reader already had through the payload alone (a
    row written before schema v7, or by any caller that still only names it in the payload)."""
    bounds.mark_release_report(conn, "p2", "p2-report.md")   # written through the column, via mark_release_report
    # A legacy-shaped row for "p1": no project column, the project only in the payload, the way every row written
    # before schema v7 (or before events.record grew its project= argument) still is.
    conn.execute(
        "INSERT INTO events (ts, kind, payload) VALUES (datetime('now'), ?, ?)",
        (bounds.RELEASE_REPORT_EVENT, '{"project": "p1", "path": "p1-legacy-report.md"}'),
    )

    assert bounds.release_report_written(conn, "p1") is True     # p1's legacy, payload-only row is found
    assert bounds.release_report_written(conn, "p2") is True     # p2's row, written through the column, is found
    assert bounds.release_report_written(conn, "p3") is False    # a third project sees neither


# ---------------------------------------------------------------------------------------------
# is_finished and finish_project
# ---------------------------------------------------------------------------------------------


def test_is_finished_when_every_merge_card_is_done_the_gates_are_green_and_the_report_is_written(conn, fake_board):
    plan = _plan(count=3)
    _make_finished(conn, fake_board, plan)

    assert bounds.is_finished("b", plan, HEAD, conn=conn) is True


@pytest.mark.parametrize("missing", ["merge_card", "gate4", "gate5", "report"])
def test_is_finished_is_false_while_any_one_of_the_three_conditions_is_missing(conn, fake_board, missing):
    plan = _plan(count=2)
    _make_finished(conn, fake_board, plan)
    if missing == "merge_card":
        fake_board.cards["m_T2"] = {"id": "m_T2", "status": "blocked"}
    elif missing == "gate4":
        conn.execute("DELETE FROM gate_runs WHERE gate = 'gate4'")
    elif missing == "gate5":
        conn.execute("DELETE FROM gate_runs WHERE gate = 'gate5'")
    else:
        conn.execute("DELETE FROM events WHERE kind = 'release_report_written'")

    assert bounds.is_finished("b", plan, HEAD, conn=conn) is False


@pytest.mark.parametrize("card_status", ["triage", "todo", "scheduled", "ready", "running", "blocked", "review",
                                          "archived", None])
def test_a_merge_card_in_any_state_but_done_is_not_finished(conn, fake_board, card_status):
    plan = _plan(count=2)
    _make_finished(conn, fake_board, plan)
    fake_board.cards["m_T1"] = {"id": "m_T1", "status": card_status}

    assert bounds.is_finished("b", plan, HEAD, conn=conn) is False


def test_is_finished_is_false_when_the_gates_only_passed_on_another_commit(conn, fake_board):
    plan = _plan(count=1)
    _make_finished(conn, fake_board, plan, head=OLDER_HEAD)

    assert bounds.is_finished("b", plan, HEAD, conn=conn) is False
    assert bounds.is_finished("b", plan, OLDER_HEAD, conn=conn) is True


def test_is_finished_is_false_when_the_report_was_written_for_another_project(conn, fake_board):
    plan = _plan(count=1, project="p1")
    _make_finished(conn, fake_board, plan)
    conn.execute("DELETE FROM events WHERE kind = 'release_report_written'")
    bounds.mark_release_report(conn, "p2", "report.md")

    assert bounds.is_finished("b", plan, HEAD, conn=conn) is False


def test_is_finished_is_false_for_a_plan_task_with_no_plan_tasks_row(conn, fake_board):
    plan = _plan(count=2)
    _make_finished(conn, fake_board, plan)
    conn.execute("DELETE FROM plan_tasks WHERE task_key = 'T2'")

    assert bounds.is_finished("b", plan, HEAD, conn=conn) is False


@pytest.mark.parametrize("merge_card_id", [None, ""])
def test_is_finished_is_false_for_a_plan_task_with_no_merge_card_id(conn, fake_board, merge_card_id):
    plan = _plan(count=2)
    _make_finished(conn, fake_board, plan)
    conn.execute("UPDATE plan_tasks SET merge_card_id = ? WHERE task_key = 'T2'", (merge_card_id,))

    assert bounds.is_finished("b", plan, HEAD, conn=conn) is False


@pytest.mark.parametrize("failure", [
    hermes.HermesCommandError(["kanban", "show"], 1, "no such card"), RuntimeError("boom"), OSError("gone"),
])
def test_is_finished_fails_closed_when_a_merge_card_cannot_be_read(conn, fake_board, failure):
    plan = _plan(count=2)
    _make_finished(conn, fake_board, plan)
    fake_board.cards["m_T2"] = failure

    assert bounds.is_finished("b", plan, HEAD, conn=conn) is False


def test_is_finished_is_false_when_kanban_show_returns_something_that_is_not_a_card(conn, fake_board):
    plan = _plan(count=1)
    _make_finished(conn, fake_board, plan)
    fake_board.cards["m_T1"] = "done"

    assert bounds.is_finished("b", plan, HEAD, conn=conn) is False


def test_is_finished_is_false_for_a_plan_with_no_tasks_even_with_gates_and_a_report(conn):
    empty = _plan(count=0)
    bounds.record_final_gate(conn, "p1", "gate4", HEAD, "pass", now=NOW)
    bounds.record_final_gate(conn, "p1", "gate5", HEAD, "pass", now=NOW)
    bounds.mark_release_report(conn, "p1", "report.md")

    assert bounds.is_finished("b", empty, HEAD, conn=conn) is False


def test_is_finished_is_false_with_no_integration_head(conn, fake_board):
    plan = _plan(count=1)
    _make_finished(conn, fake_board, plan)

    assert bounds.is_finished("b", plan, None, conn=conn) is False


def test_is_finished_asks_the_given_board_for_each_merge_card_and_never_a_work_card(conn, fake_board):
    plan = _plan(count=3)
    _make_finished(conn, fake_board, plan)

    assert bounds.is_finished("the-board", plan, HEAD, conn=conn) is True

    assert fake_board.shown == [("the-board", "m_T1"), ("the-board", "m_T2"), ("the-board", "m_T3")]


def test_is_finished_stops_at_the_first_merge_card_that_is_not_done(conn, fake_board):
    plan = _plan(count=3)
    _make_finished(conn, fake_board, plan)
    fake_board.cards["m_T1"] = {"id": "m_T1", "status": "blocked"}

    assert bounds.is_finished("b", plan, HEAD, conn=conn) is False
    assert fake_board.shown == [("b", "m_T1")]


@pytest.mark.parametrize("missing", ["gate4", "gate5", "report"])
def test_is_finished_does_not_ask_the_board_while_a_database_condition_fails(conn, fake_board, missing):
    plan = _plan(count=2)
    _make_finished(conn, fake_board, plan)
    if missing == "report":
        conn.execute("DELETE FROM events WHERE kind = 'release_report_written'")
    else:
        conn.execute("DELETE FROM gate_runs WHERE gate = ?", (missing,))

    assert bounds.is_finished("b", plan, HEAD, conn=conn) is False
    assert fake_board.shown == []


def test_is_finished_reads_only_this_plans_project(conn, fake_board):
    plan = _plan(count=1, project="p1")
    _make_finished(conn, fake_board, plan)
    conn.execute("DELETE FROM plan_tasks WHERE project = 'p1'")
    conn.execute(
        "INSERT INTO plan_tasks (project, task_key, work_card_id, merge_card_id, role, created_at) "
        "VALUES ('other', 'T1', 'ow', 'om', 'coder', datetime('now'))"
    )
    fake_board.cards["om"] = {"id": "om", "status": "done"}

    assert bounds.is_finished("b", plan, HEAD, conn=conn) is False


def test_is_finished_is_not_fooled_by_another_projects_final_gate_rows_on_the_same_head(conn, fake_board):
    """The bug two projects sharing this database used to hit (found by the bounds and MR builders): gate_runs
    had no project column at all until schema v7, and is_finished's own final_gates_green call did not filter by
    it even once the column existed, until this round. A second project's final-gate rows on the exact same
    integration HEAD (a real possibility: two projects can share a database) must not count as this project's."""
    plan = _plan(count=1, project="p1")
    _seed_tasks(conn, plan)
    for task in plan.tasks:
        fake_board.cards[f"m_{task.key}"] = {"id": f"m_{task.key}", "status": "done"}
    bounds.mark_release_report(conn, plan.project, "docs/ases/release-report.md")
    bounds.record_final_gate(conn, "p2", "gate4", HEAD, "pass")  # p2's gates, not p1's
    bounds.record_final_gate(conn, "p2", "gate5", HEAD, "pass")

    assert bounds.is_finished("b", plan, HEAD, conn=conn) is False

    bounds.record_final_gate(conn, plan.project, "gate4", HEAD, "pass")
    bounds.record_final_gate(conn, plan.project, "gate5", HEAD, "pass")

    assert bounds.is_finished("b", plan, HEAD, conn=conn) is True


def test_finish_project_sets_the_status_and_says_it_did(conn, fake_board):
    plan = _plan(count=2)
    bounds.start_project(conn, "p1", now=NOW)
    _make_finished(conn, fake_board, plan)

    assert bounds.finish_project("b", plan, HEAD, conn=conn, now=NOW + timedelta(hours=1)) is True

    state = _state(conn)
    assert state["status"] == "finished"
    assert state["updated_at"] == "2026-09-19T13:00:00+00:00"
    assert state["started_at"] == "2026-09-19T12:00:00+00:00"  # the rest of the row is untouched


def test_finish_project_flips_the_status_once_and_a_second_call_writes_nothing(conn, fake_board):
    plan = _plan(count=1)
    bounds.start_project(conn, "p1", now=NOW)
    _make_finished(conn, fake_board, plan)

    first = bounds.finish_project("b", plan, HEAD, conn=conn, now=NOW + timedelta(hours=1))
    after_first = _state(conn)
    second = bounds.finish_project("b", plan, HEAD, conn=conn, now=NOW + timedelta(hours=2))

    assert (first, second) == (True, False)
    assert _state(conn) == after_first  # not even updated_at moved
    assert after_first["status"] == "finished"


def test_finish_project_does_nothing_while_the_project_is_not_finished(conn, fake_board):
    plan = _plan(count=2)
    bounds.start_project(conn, "p1", now=NOW)
    _make_finished(conn, fake_board, plan)
    fake_board.cards["m_T2"] = {"id": "m_T2", "status": "review"}
    before = _state(conn)

    assert bounds.finish_project("b", plan, HEAD, conn=conn, now=NOW + timedelta(hours=1)) is False

    assert _state(conn) == before
    assert _state(conn)["status"] == "running"


@pytest.mark.parametrize("status", ["stopped", "paused"])
def test_finish_project_never_overrides_a_stop_or_a_pause_and_does_not_ask_the_board(conn, fake_board, status):
    plan = _plan(count=2)
    bounds.start_project(conn, "p1", now=NOW)
    _make_finished(conn, fake_board, plan)
    bounds.set_status(conn, "p1", status, "kill switch", now=NOW + timedelta(minutes=1))
    before = _state(conn)

    assert bounds.finish_project("b", plan, HEAD, conn=conn, now=NOW + timedelta(hours=1)) is False

    assert _state(conn) == before
    assert fake_board.shown == []


@pytest.mark.parametrize("arrives", ["stopped", "paused", "finished"])
def test_finish_project_is_not_fooled_by_a_status_that_lands_after_the_status_was_read(
    conn, fake_board, monkeypatch, arrives,
):
    """The guard is part of the write, not only of the earlier read: a stop, a pause or another finisher writing
    the row while is_finished is still asking the board must survive, and this call must not claim the flip."""
    plan = _plan(count=1)
    bounds.start_project(conn, "p1", now=NOW)
    _make_finished(conn, fake_board, plan)
    real_is_finished = bounds.is_finished

    def status_arrives_during_the_check(*args, **kwargs):
        verdict = real_is_finished(*args, **kwargs)
        bounds.set_status(conn, "p1", arrives, "kill switch", now=NOW + timedelta(minutes=5))
        return verdict

    monkeypatch.setattr(bounds, "is_finished", status_arrives_during_the_check)

    assert bounds.finish_project("b", plan, HEAD, conn=conn, now=NOW + timedelta(hours=1)) is False

    state = _state(conn)
    assert state["status"] == arrives
    assert state["updated_at"] == "2026-09-19T12:05:00+00:00"  # the arriving write, not ours
    assert state["stop_reason"] == ("kill switch" if arrives in ("stopped", "paused") else None)


def test_finish_project_creates_the_row_when_the_project_has_none(conn, fake_board):
    plan = _plan(count=1)
    _make_finished(conn, fake_board, plan)
    assert _state(conn) is None

    assert bounds.finish_project("b", plan, HEAD, conn=conn, now=NOW) is True

    state = _state(conn)
    assert (state["status"], state["replans"], state["started_at"]) == ("finished", 0, None)


def test_finish_project_finishes_a_planning_project_too(conn, fake_board):
    plan = _plan(count=1)
    bounds.add_replan(conn, "p1", now=NOW)
    _make_finished(conn, fake_board, plan)

    assert bounds.finish_project("b", plan, HEAD, conn=conn, now=NOW) is True
    assert _state(conn)["status"] == "finished"
    assert _state(conn)["replans"] == 1


def test_a_finished_project_is_not_restarted_and_is_no_longer_asking_for_a_stop(conn, fake_board):
    plan = _plan(count=1)
    bounds.start_project(conn, "p1", now=NOW)
    _make_finished(conn, fake_board, plan)
    bounds.finish_project("b", plan, HEAD, conn=conn, now=NOW)

    assert bounds.stop_requested(conn, "p1") is False
    with pytest.raises(bounds.StateError):
        bounds.start_project(conn, "p1", now=NOW)


def test_finish_project_stamps_the_real_time_when_now_is_omitted(conn, fake_board):
    plan = _plan(count=1)
    _make_finished(conn, fake_board, plan)
    before = datetime.now(timezone.utc).replace(microsecond=0)

    assert bounds.finish_project("b", plan, HEAD, conn=conn) is True

    assert before <= datetime.fromisoformat(_state(conn)["updated_at"]) <= datetime.now(timezone.utc)


def test_finish_project_is_about_this_plans_project_only(conn, fake_board):
    plan = _plan(count=1, project="p1")
    _make_finished(conn, fake_board, plan)
    bounds.start_project(conn, "other", now=NOW)

    bounds.finish_project("b", plan, HEAD, conn=conn, now=NOW)

    assert _state(conn, "other")["status"] == "running"
    assert _state(conn, "p1")["status"] == "finished"
