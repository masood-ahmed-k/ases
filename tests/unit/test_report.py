"""report.py: the project report, swarm status, swarm report and the local page (ASES-OBS-01, ASES-OBS-02,
ASES-SEC-01).

hermes.kanban_show and hermes.kanban_list are faked, and every other way into Hermes fails the test (an autouse
fixture, and _fake_hermes for the tests that fake the two reads), so nothing here can reach a real board. The
database is a temp sqlite file seeded by inserting rows directly."""
import dataclasses
import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

import pytest

from ases import bounds, config, db, events, hermes, ledger, models, plan as plan_mod, questions, report

ROLES = {"lead": "lead", "coder": "coder-1", "reviewer": "reviewer"}
NOW = datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc)
SECRET = "sk-abcdefghijklmnopqrstuvwx"
CODER_MODEL = "qwen/qwen3-coder-plus:free"
REVIEWER_MODEL = "cohere/north-mini-code:free"

BUDGETS = {
    "attempts_per_card": 3, "review_rounds_per_task": 3, "fix_cards_per_task": 2, "replans_per_project": 2,
    "max_cards": 40, "daily_reserve_percent": 10, "review_reserve_requests": 20,
}

# Shaped like config/models.yaml: OpenRouter's 50 requests a day for the reviewer, and xKiro with no known cap.
MODELS_CONFIG = {
    "providers": {
        "openrouter": {"limits": {"per_day_default": 50, "per_day_after_credits": 1000}, "credits_purchased": False},
        "xkiro": {"limits": {}},
    },
    "models": [
        {"provider": "xkiro", "model": CODER_MODEL, "role_class": "coder", "pinned": True,
         "context_length": 1050000, "tool_calling": True},
        {"provider": "openrouter", "model": REVIEWER_MODEL, "role_class": "reviewer", "pinned": True,
         "context_length": 256000, "tool_calling": True},
        {"provider": "xkiro", "model": "minimax/minimax-m3:free", "role_class": "coder_candidate",
         "pinned": False, "context_length": None},
    ],
}


def _task(key, title, role, depends_on, touches):
    return {"key": key, "title": title, "role": role, "depends_on": depends_on, "touches": touches,
            "acceptance": ["done"], "gate_profile": "trivial", "estimated_requests": 5}


PLAN = plan_mod.parse_and_validate({
    "project": "p1",
    "integration_branch": "integration",
    "gate_profiles": {"trivial": ["echo ok"]},
    "tasks": [
        _task("T1", "scaffold", "coder", [], ["a.py"]),
        _task("T2", "wire the database", "coder", ["T1"], ["db.py"]),
        _task("T3", "review it all", "reviewer", ["T2"], []),
    ],
}, known_roles=set(ROLES), max_cards=40)


class _RealHermesReached(BaseException):
    """Not an Exception on purpose: report.py catches Exception around the two Hermes reads it makes, and a test
    that reaches real Hermes must fail loudly instead of having that caught and reported as an unknown card."""


@pytest.fixture(autouse=True)
def _no_real_hermes(monkeypatch):
    """Whatever a test forgets to fake must not reach the user's Hermes."""
    def forbidden(*args, **kwargs):
        raise _RealHermesReached("this test reached Hermes; fake kanban_show and kanban_list with _fake_hermes")

    monkeypatch.setattr(hermes, "_run", forbidden)
    monkeypatch.setattr(hermes, "kanban_show", forbidden)
    monkeypatch.setattr(hermes, "kanban_list", forbidden)


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "ases.db")


def _project(tmp_path, *, budgets=None, name="ases"):
    return config.ProjectConfig(
        name=name, environment="native", data_class="public", workspace_root=tmp_path / "ws",
        ases_home=tmp_path / "home", board="b", integration_branch="integration", roles=ROLES,
        concurrency={}, budgets=BUDGETS if budgets is None else budgets, hermes_tested_version="0.21.3",
        hermes_native_home=tmp_path / "hermes",
    )


def _build(conn, tmp_path, *, models_config=MODELS_CONFIG, now=NOW, project=None, **kwargs):
    return report.build_report(
        "b", PLAN, project or _project(tmp_path), models_config, conn, now=now, **kwargs,
    )


# --- the fake Hermes ------------------------------------------------------------------------------------------


def _card(card_id, status, *, title=None, assignee="coder-1", events=None, comments=None):
    """A card as hermes.kanban_show returns it (the flat task plus _events and _comments)."""
    return {"id": card_id, "status": status, "title": f"title of {card_id}" if title is None else title,
            "assignee": assignee, "_events": events or [], "_comments": comments or []}


def _blocked(reason, created_at=1):
    return {"kind": "blocked", "payload": {"reason": reason}, "created_at": created_at, "run_id": 1}


def _gave_up(failures=3, error="boom", created_at=1):
    """The `gave_up` event Hermes's dispatcher writes when its circuit breaker trips (and NO `blocked` event)."""
    return {"kind": "gave_up", "payload": {"failures": failures, "error": error}, "created_at": created_at,
            "run_id": None}


def _loop(reason, created_at=1):
    """The `block_loop_detected` event a repeated block writes when it sends a card to `triage`."""
    return {"kind": "block_loop_detected", "payload": {"reason": reason, "kind": "needs_input"},
            "created_at": created_at, "run_id": None}


def _asked(text, created_at=1):
    """The comment questions.ask_user writes on a card Hermes will not block."""
    return {"author": "ases", "body": f"ASES QUESTION: {text}", "created_at": created_at}


def _fake_hermes(monkeypatch, cards, *, failing=(), error=None):
    """hermes.kanban_show and hermes.kanban_list over a dict of card id -> card. Any other route into Hermes
    fails the test: a report only ever reads. Returns what was asked for."""
    calls = {"show": [], "list": [], "boards": []}

    def show(board, card_id):
        calls["show"].append(card_id)
        calls["boards"].append(board)
        if card_id in failing:
            raise error or hermes.HermesCommandError(["kanban", "show", card_id], 1, "no such card")
        return dict(cards[card_id])

    def list_cards(board, status=None, assignee=None):
        calls["list"].append((board, status))
        return [dict(card) for card in cards.values() if status is None or card["status"] == status]

    def forbidden(*args, **kwargs):
        raise _RealHermesReached("a report must not call Hermes beyond kanban_show and kanban_list")

    monkeypatch.setattr(hermes, "_run", forbidden)
    monkeypatch.setattr(hermes, "kanban_show", show)
    monkeypatch.setattr(hermes, "kanban_list", list_cards)
    return calls


def _quiet_board(**overrides):
    """Six cards nobody has touched: work cards ready or todo, merge cards created blocked with no event."""
    board = {
        "w1": _card("w1", "ready"), "w2": _card("w2", "todo"), "w3": _card("w3", "todo"),
        "m1": _card("m1", "blocked", assignee=None), "m2": _card("m2", "blocked", assignee=None),
        "m3": _card("m3", "blocked", assignee=None),
    }
    board.update(overrides)
    return board


# --- seeding --------------------------------------------------------------------------------------------------


def _seed_tasks(conn, *, project="p1", fix_cards=(0, 0, 0), prefix=""):
    for number, fixes in enumerate(fix_cards, start=1):
        conn.execute(
            "INSERT INTO plan_tasks (project, task_key, work_card_id, merge_card_id, role, fix_cards, created_at) "
            "VALUES (?, ?, ?, ?, 'coder', ?, datetime('now'))",
            (project, f"T{number}", f"{prefix}w{number}", f"{prefix}m{number}", fixes),
        )


def _event(conn, ts, kind, payload):
    conn.execute("INSERT INTO events (ts, kind, payload) VALUES (?, ?, ?)", (ts, kind, json.dumps(payload)))


def _raw_event(conn, ts, kind, raw):
    conn.execute("INSERT INTO events (ts, kind, payload) VALUES (?, ?, ?)", (ts, kind, raw))


def _gate_run(conn, task_key, gate, sha, result, ran_at, project=None):
    conn.execute(
        "INSERT INTO gate_runs (task_key, gate, commit_sha, result, detail, ran_at, project) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (task_key, gate, sha, result, "GATE-DETAIL-TEXT", ran_at, project),
    )


def _verdict(conn, project, task_key, sha, card_id, outcome, metadata, recorded_at):
    conn.execute(
        "INSERT INTO review_verdicts (project, task_key, commit_sha, card_id, outcome, reviewer_profile, metadata, "
        "recorded_at) VALUES (?, ?, ?, ?, ?, 'reviewer', ?, ?)",
        (project, task_key, sha, card_id, outcome, metadata, recorded_at),
    )


def _merge_record(conn, task_key, candidate, gate3, squash, reverted, completed_at):
    conn.execute(
        "INSERT INTO merge_records (task_key, candidate_sha, gate3_result, squash_commit, reverted, completed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)", (task_key, candidate, gate3, squash, reverted, completed_at),
    )


def _lineage(conn, task_key, rounds, capability, infra, project="p1"):
    conn.execute(
        "INSERT INTO lineage (project, task_key, review_rounds, capability_failures, infra_failures, replans, "
        "updated_at) VALUES (?, ?, ?, ?, ?, 0, datetime('now'))", (project, task_key, rounds, capability, infra),
    )


def _state(conn, *, status="running", started_at=None, deadline_at=None, replans=0, stop_reason=None,
           updated_at="2026-09-19T11:00:00+00:00", project="p1"):
    conn.execute(
        "INSERT INTO project_state (project, started_at, deadline_at, replans, status, stop_reason, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)", (project, started_at, deadline_at, replans, status, stop_reason, updated_at),
    )


def _ingested(conn, session, profile, provider, model, requests, tokens_in, tokens_out, at, project="p1"):
    conn.execute(
        "INSERT INTO usage_ingested (session_id, profile, provider, model, requests, input_tokens, output_tokens, "
        "ingested_at, project, task_key, card_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'T1', 'w1')",
        (session, profile, provider, model, requests, tokens_in, tokens_out, at, project),
    )


@pytest.fixture
def scenario(tmp_path, monkeypatch, conn):
    """One done task with a merge record (T1), one blocked card with a question (T2), one parked card (T3), and a
    row or two in every table a panel reads, all at fixed times before NOW (2026-09-19 12:00 UTC)."""
    _seed_tasks(conn, fix_cards=(1, 2, 0))
    # Another project's rows, with the same task keys, that no panel may show.
    _seed_tasks(conn, project="other", prefix="o")
    _lineage(conn, "T1", 3, 3, 3, project="other")
    _state(conn, status="stopped", replans=2, project="other")
    calls = _fake_hermes(monkeypatch, {
        "w1": _card("w1", "done"), "m1": _card("m1", "done", assignee=None),
        "w2": _card("w2", "blocked", events=[_blocked("which database should the app use?")]),
        "m2": _card("m2", "blocked", assignee=None),
        "w3": _card("w3", "scheduled", assignee="reviewer"), "m3": _card("m3", "blocked", assignee=None),
        "ow3": _card("ow3", "scheduled"),
    })

    _state(conn, started_at="2026-09-19T10:00:00+00:00", deadline_at="2026-09-19T14:00:00+00:00", replans=1)
    _lineage(conn, "T1", 2, 1, 3)
    _lineage(conn, "T3", 3, 0, 0)

    # `now=NOW`: this whole scenario is fixed at 2026-09-19 (every timestamp below is that day), and since round
    # 14 (package CLOCK) the budget panel asks the ledger for usage on the report's OWN day (`now`), not
    # whatever day the wall clock happens to be on when the test runs, so the ledger rows must land on the same
    # day too.
    ledger.record_usage(conn, "openrouter", REVIEWER_MODEL, 37, now=NOW)
    ledger.record_usage(conn, "xkiro", CODER_MODEL, 12, now=NOW)
    _ingested(conn, "s1", "reviewer", "openrouter", REVIEWER_MODEL, 30, 90000, 1500, "2026-09-19 10:45:00")
    _ingested(conn, "s2", "coder-1", "xkiro", CODER_MODEL, 8, 800, 80, "2026-09-19T10:15:00+00:00")
    _ingested(conn, "s3", "coder-1", "xkiro", CODER_MODEL, 4, 400, 40, "2026-09-19 11:15:00")
    _ingested(conn, "s4", "coder-1", "xkiro", CODER_MODEL, 99, 1, 1, "2026-09-18 23:59:59")

    _gate_run(conn, "T1", "gate1", "a" * 40, "pass", "2026-09-19T10:00:00+00:00")
    _gate_run(conn, "T1", "gate3", "b" * 40, "pass", "2026-09-19T10:30:00+00:00")
    _gate_run(conn, "T2", "gate1", "c" * 40, "fail", "2026-09-19T11:00:00+00:00")
    _gate_run(conn, "X9", "gate1", "f" * 40, "pass", "2026-09-19T11:30:00+00:00")
    _gate_run(conn, report.FINAL_GATE_KEY, "gate4", "d" * 40, "pass", "2026-09-19T11:45:00+00:00")
    _merge_record(conn, "T1", "c" * 40, "pass", "b" * 40, 0, "2026-09-19T10:31:00+00:00")
    _merge_record(conn, "T2", "e" * 40, "fail", None, 0, None)
    _merge_record(conn, "X9", "f" * 40, "pass", "f" * 40, 0, "2026-09-19T11:31:00+00:00")
    _verdict(conn, "p1", "T1", "b" * 40, "w1", "PASS",
             json.dumps({"review_outcome": "approved", "gate_tampering_suspected": True}), "2026-09-19T10:35:00+00:00")
    _verdict(conn, "p1", "T2", "e" * 40, "w2", "CHANGES_REQUIRED", None, "2026-09-19T11:05:00+00:00")
    _verdict(conn, "other", "T1", "9" * 40, "ow1", "PASS", None, "2026-09-19T11:59:00+00:00")

    _event(conn, "2026-09-19T10:05:00+00:00", "cards_created", {"task_key": "T1", "work": "w1", "merge": "m1"})
    _event(conn, "2026-09-19T10:40:00+00:00", "merged", {"task_key": "T1", "sha": "b" * 40})
    _event(conn, "2026-09-19T10:50:00+00:00", "card_parked_for_budget",
           {"task_key": "T3", "reason": "budget: needs 5, only 3 usable today"})
    _event(conn, "2026-09-19T11:10:00+00:00", "merge_failed",
           {"task_key": "T2", "detail": "merge conflict: CONFLICT (content) in db.py"})
    _event(conn, "2026-09-19T11:20:00+00:00", "merge_refused_unreviewed",
           {"card_id": "w2", "task_key": "T2", "completed_by": "coder-1", "needs_completion_by": "reviewer"})
    _event(conn, "2026-09-19T11:30:00+00:00", "card_parked_for_budget",
           {"task_key": "T3", "reason": "review budget on openrouter: needs 20, only 13 usable today"})
    _event(conn, "2026-09-19T11:40:00+00:00", "pass_error",
           {"pass": 4, "consecutive": 1, "error": "TimeoutExpired: hermes kanban list timed out"})
    _event(conn, "2026-09-19T11:45:00+00:00", "gate_tamper_suspected", {"task_key": "T1", "detail": "a test was deleted"})
    _event(conn, "2026-09-19T11:50:00+00:00", "fix_card_created", {"task_key": "T2", "fix_card_id": "wfix"})
    _event(conn, "2026-09-19T11:55:00+00:00", "integrity_violation",
           {"problems": ["primary checkout is dirty: M a.py"], "head": "abc", "branch": "integration"})

    models.sync_from_config(conn, MODELS_CONFIG)
    models.record_smoke_test(conn, "openrouter", REVIEWER_MODEL, "pass", "ok")
    project = _project(tmp_path)
    return conn, project, calls


@pytest.fixture
def scenario_report(scenario, tmp_path):
    conn, project, _calls = scenario
    return _build(conn, tmp_path, project=project)


def _bounds(rep):
    return {bound["name"]: bound for bound in rep["project"]["bounds"]}


# --- the report as a whole ------------------------------------------------------------------------------------


def test_empty_database_still_builds_and_renders(conn, tmp_path, monkeypatch):
    """No cards, no events, no gate runs, no lineage, no project_state row, an empty registry: every panel is
    still there, and all three renderings and the JSON work."""
    calls = _fake_hermes(monkeypatch, {})
    rep = _build(conn, tmp_path)

    assert calls["show"] == []  # no card was ever created, so there is nothing to ask Hermes about
    assert rep["cards"]["counts"] == {"not created": 3}
    assert rep["cards"]["merge_queue"] == {"done": 0, "total": 3, "counts": {"not created": 3}}
    assert rep["cards"]["open_questions"] == 0 and rep["cards"]["questions"] == []
    assert rep["quality"] == {"gate_runs": [], "review_verdicts": [], "merge_records": [], "findings": []}
    assert rep["events"] == [] and rep["models"] == []
    assert rep["health"]["read"] == 0 and rep["health"]["recent"] == []
    assert [kind["count"] for kind in rep["health"]["kinds"]] == [0] * len(report.HEALTH_KINDS)
    assert rep["project"]["status"] is None and rep["project"]["started_at"] is None
    assert rep["budget"]["parked"] == [] and rep["budget"]["by_model"] == []
    assert [row["used"] for row in rep["budget"]["providers"]] == [0, 0]
    json.dumps(rep)

    status, text, page = report.render_status(rep), report.render_text(rep), report.render_html(rep)
    assert "Quality: no gate runs recorded" in status
    assert "Health: no events of interest" in status
    assert "Parked: none" in status
    assert "Cards: 3 not created; merge queue 0/3 done" in status
    assert "Gate runs (newest first): none recorded" in text
    assert "Review verdicts (newest first): none recorded" in text
    assert "Merge records: none recorded" in text
    assert "Findings (merge refusals, integrity, tamper; newest first): none recorded" in text
    assert "Parked cards (waiting for budget): none" in text
    assert "Requests today by model and role: none recorded today" in text
    assert "Open questions (swarm questions lists them, swarm answer replies): none" in text
    assert "Newest events first, secrets redacted: no events recorded" in text
    assert "Model registry (pinned first): none in the registry (swarm models syncs it" in text
    assert re.search(r"^wall clock minutes\s+-\s+not set$", text, re.MULTILINE)
    assert "<h2>Models</h2>" in page and "no events recorded" in page


def test_report_has_one_key_per_panel_in_the_blueprints_order(scenario_report):
    assert list(scenario_report) == [
        "generated_at", "project", "budget", "cards", "quality", "health", "events", "models",
    ]


def test_generated_at_is_utc_iso_seconds(conn, tmp_path, monkeypatch):
    _fake_hermes(monkeypatch, {})
    assert _build(conn, tmp_path)["generated_at"] == "2026-09-19T12:00:00+00:00"
    plus_two = timezone(timedelta(hours=2))
    assert _build(conn, tmp_path, now=datetime(2026, 9, 19, 14, 0, 0, 999999, tzinfo=plus_two))[
        "generated_at"] == "2026-09-19T12:00:00+00:00"
    assert _build(conn, tmp_path, now=datetime(2026, 9, 19, 12, 0, 0))["generated_at"] == "2026-09-19T12:00:00+00:00"


def test_without_now_the_report_uses_the_real_clock(conn, tmp_path, monkeypatch):
    _fake_hermes(monkeypatch, {})
    before = datetime.now(timezone.utc)
    rep = _build(conn, tmp_path, now=None)
    after = datetime.now(timezone.utc)
    stamp = datetime.fromisoformat(rep["generated_at"])
    assert before - timedelta(seconds=1) <= stamp <= after + timedelta(seconds=1)


def test_building_a_report_writes_nothing_to_the_database(scenario, tmp_path):
    conn, project, _calls = scenario
    tables = ("events", "gate_runs", "requests_ledger", "plan_tasks", "usage_ingested", "model_registry",
              "project_state", "lineage", "merge_records", "review_verdicts")
    before = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    changes = conn.total_changes
    _build(conn, tmp_path, project=project)
    assert conn.total_changes == changes
    assert {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables} == before


def test_the_report_is_json_serialisable_and_round_trips(scenario_report):
    assert json.loads(json.dumps(scenario_report)) == scenario_report


def test_report_keys_survive_redaction(scenario_report):
    """events.redact replaces the value of any key containing key, token, secret, password, credential or
    authorization. A report field named like that would silently read [redacted] (this is why the token counts
    are tok_in and tok_out), so redacting a secret-free report must change nothing."""
    assert events.redact(scenario_report) == scenario_report
    assert "[redacted]" not in json.dumps(scenario_report)
    row = next(r for r in scenario_report["budget"]["by_model"] if r["provider"] == "xkiro")
    assert (row["tok_in"], row["tok_out"]) == (1200, 120)


# --- Project --------------------------------------------------------------------------------------------------


def test_project_panel_facts(scenario_report):
    project = scenario_report["project"]
    assert {k: v for k, v in project.items() if k != "bounds"} == {
        "name": "ases", "plan_project": "p1", "board": "b", "integration_branch": "integration",
        "data_class": "public", "status": "running", "stop_reason": None,
        "started_at": "2026-09-19T10:00:00+00:00", "deadline_at": "2026-09-19T14:00:00+00:00",
    }


def test_project_panel_reports_a_stop_reason(conn, tmp_path, monkeypatch):
    _fake_hermes(monkeypatch, {})
    _state(conn, status="stopped", stop_reason="project wall clock reached", started_at="2026-09-19T10:00:00+00:00")
    project = _build(conn, tmp_path)["project"]
    assert (project["status"], project["stop_reason"]) == ("stopped", "project wall clock reached")


def test_bounds_from_lineage_plan_tasks_and_project_state(scenario_report):
    bounds = _bounds(scenario_report)
    assert all(set(bound) == {"name", "used", "limit"} for bound in bounds.values())
    expected = {
        "cards in plan": (3, 40), "re-plans": (1, 2), "wall clock minutes": (120, 240),
        "fix cards T1": (1, 2), "fix cards T2": (2, 2), "fix cards T3": (0, 2),
        "review rounds T1": (2, 3), "review rounds T2": (0, 3), "review rounds T3": (3, 3),
        "capability failures T1": (1, 3), "capability failures T2": (0, 3), "capability failures T3": (0, 3),
        "infra failures T1": (3, None), "infra failures T2": (0, None), "infra failures T3": (0, None),
    }
    assert {name: (b["used"], b["limit"]) for name, b in bounds.items()} == expected
    # The other project's lineage, project_state and plan_tasks rows (same task keys) are not this plan's.
    assert [b["name"] for b in scenario_report["project"]["bounds"]][:3] == [
        "cards in plan", "re-plans", "wall clock minutes"]


def test_missing_rows_read_zero_and_the_wall_clock_is_not_set(conn, tmp_path, monkeypatch):
    _fake_hermes(monkeypatch, {})
    bounds = _bounds(_build(conn, tmp_path))
    assert (bounds["re-plans"]["used"], bounds["re-plans"]["limit"]) == (0, 2)
    assert (bounds["wall clock minutes"]["used"], bounds["wall clock minutes"]["limit"]) == (None, None)
    for task in ("T1", "T2", "T3"):
        assert bounds[f"fix cards {task}"]["used"] == 0  # no plan_tasks row yet
        assert bounds[f"review rounds {task}"]["used"] == 0  # no lineage row
        assert bounds[f"capability failures {task}"]["used"] == 0
        assert bounds[f"infra failures {task}"]["used"] == 0


@pytest.mark.parametrize("started, deadline, expected", [
    ("2026-09-19T10:00:00+00:00", "2026-09-19T14:00:00+00:00", (120, 240)),
    ("2026-09-19 10:00:00", "2026-09-19 14:00:00", (120, 240)),           # SQLite's datetime('now') format
    ("2026-09-19T10:00:30+00:00", None, (119, None)),                     # whole minutes, rounded down
    ("2026-09-19T10:00:00+00:00", "", (120, None)),                       # no deadline set
    (None, "2026-09-19T14:00:00+00:00", (None, None)),                    # no start: nothing can be measured
    ("not a time", "2026-09-19T14:00:00+00:00", (None, None)),
    ("2026-09-19T13:00:00+00:00", "2026-09-19T14:00:00+00:00", (0, 60)),  # started in the future: never negative
    (" 2026-09-19T10:00:00+00:00 ", " 2026-09-19T14:00:00+00:00 ", (120, 240)),
    ("   ", "   ", (None, None)),
], ids=["iso", "sqlite", "seconds", "empty deadline", "no start", "unreadable start", "future start",
        "surrounding spaces", "blank"])
def test_wall_clock_bound(conn, tmp_path, monkeypatch, started, deadline, expected):
    _fake_hermes(monkeypatch, {})
    _state(conn, started_at=started, deadline_at=deadline)
    bound = _bounds(_build(conn, tmp_path))["wall clock minutes"]
    assert (bound["used"], bound["limit"]) == expected


@pytest.mark.parametrize("deadline, configured, expected", [
    (None, 300, (120, 300)),                                # no deadline row: the configured minutes are the limit
    ("2026-09-19T14:00:00+00:00", 300, (120, 240)),         # a recorded deadline wins over the configured number
    (None, None, (120, None)),
])
def test_wall_clock_limit_falls_back_to_the_configured_minutes(conn, tmp_path, monkeypatch, deadline, configured,
                                                               expected):
    _fake_hermes(monkeypatch, {})
    _state(conn, started_at="2026-09-19T10:00:00+00:00", deadline_at=deadline)
    budgets = {**BUDGETS, "project_wall_clock_minutes": configured}
    bound = _bounds(_build(conn, tmp_path, project=_project(tmp_path, budgets=budgets)))["wall clock minutes"]
    assert (bound["used"], bound["limit"]) == expected


def test_a_configured_wall_clock_limit_needs_a_start_time_to_measure_against(conn, tmp_path, monkeypatch):
    _fake_hermes(monkeypatch, {})
    budgets = {**BUDGETS, "project_wall_clock_minutes": 300}
    bound = _bounds(_build(conn, tmp_path, project=_project(tmp_path, budgets=budgets)))["wall clock minutes"]
    assert (bound["used"], bound["limit"]) == (None, None)


@pytest.mark.parametrize("status, updated_at, expected_used", [
    ("finished", "2026-09-19T11:00:00+00:00", 60),    # a finished project's clock stopped
    ("finished", "2026-09-19T09:00:00+00:00", 120),   # updated before it started: not believable, use now
    ("finished", "2026-09-19T13:00:00+00:00", 120),   # updated after now (clock skew): never run past now
    ("finished", "2026-09-19T10:00:00+00:00", 0),     # finished the minute it started
    ("finished", "garbage", 120),                     # updated_at is NOT NULL, but it can be unreadable text
])
def test_wall_clock_freezes_only_when_the_project_is_finished(
    conn, tmp_path, monkeypatch, status, updated_at, expected_used,
):
    _fake_hermes(monkeypatch, {})
    _state(conn, status=status, started_at="2026-09-19T10:00:00+00:00", updated_at=updated_at)
    assert _bounds(_build(conn, tmp_path))["wall clock minutes"]["used"] == expected_used


@pytest.mark.parametrize("status", ["running", "paused", "stopped"])
def test_wall_clock_keeps_counting_live_for_every_status_but_finished(conn, tmp_path, monkeypatch, status):
    """bounds._project_wall_clock_status never freezes for any status (a pause or a kill-switch stop does not
    move deadline_at, and neither is a terminal state: killswitch.clear_stop and swarm resume put the project
    back to running with the same deadline). A STOPPED project used to freeze here too (the bug this test
    guards against): that hid the fact that resuming re-evaluates the SAME live clock, which can immediately
    re-pause the project on a deadline that passed while it sat stopped. Only 'finished' may freeze
    (test_wall_clock_freezes_only_when_the_project_is_finished): a finished project is never resumed."""
    _fake_hermes(monkeypatch, {})
    _state(conn, status=status, started_at="2026-09-19T10:00:00+00:00", updated_at="2026-09-19T11:00:00+00:00")
    assert _bounds(_build(conn, tmp_path))["wall clock minutes"]["used"] == 120  # to NOW (12:00), not to updated_at


def test_wall_clock_agrees_with_the_bound_it_describes_for_every_resumable_status(conn, tmp_path, monkeypatch):
    """The concrete regression: report._wall_clock and bounds._project_wall_clock_status must show the SAME
    breach for every status a project can still be resumed from (running, paused, stopped), so a report read
    before `swarm resume` never promises budget that the bound will immediately take back. Only 'finished' is
    allowed to diverge, because bounds.evaluate_bounds is never asked about a finished project again."""
    _fake_hermes(monkeypatch, {})
    started = "2026-09-19T10:00:00+00:00"
    deadline = "2026-09-19T11:30:00+00:00"  # 90 minutes: already passed by NOW (12:00), well before updated_at too
    for status in ("running", "paused", "stopped"):
        conn.execute("DELETE FROM project_state")
        _state(conn, status=status, started_at=started, deadline_at=deadline, updated_at="2026-09-19T10:45:00+00:00")
        report_bound = _bounds(_build(conn, tmp_path))["wall clock minutes"]
        state = bounds.get_state(conn, "p1")
        live = bounds._project_wall_clock_status(state, bounds.Bounds.from_budgets(BUDGETS), NOW)
        assert (report_bound["used"], report_bound["limit"]) == (int(live.used), int(live.limit))
        assert report_bound["used"] >= report_bound["limit"]  # both agree: breached, deadline already passed
        assert live.breached is True


def test_bounds_fall_back_to_the_blueprints_defaults_and_honour_the_config(conn, tmp_path, monkeypatch):
    _fake_hermes(monkeypatch, {})
    defaults = _bounds(_build(conn, tmp_path, project=_project(tmp_path, budgets={})))
    assert defaults["cards in plan"]["limit"] == 40
    assert defaults["re-plans"]["limit"] == 2
    assert defaults["fix cards T1"]["limit"] == 2
    assert defaults["review rounds T1"]["limit"] == 3
    assert defaults["capability failures T1"]["limit"] == 3
    assert defaults["infra failures T1"]["limit"] is None

    configured = {"max_cards": 10, "replans_per_project": 5, "fix_cards_per_task": 1, "review_rounds_per_task": 4,
                  "attempts_per_card": 6}
    bounds = _bounds(_build(conn, tmp_path, project=_project(tmp_path, budgets=configured)))
    assert (bounds["cards in plan"]["limit"], bounds["re-plans"]["limit"], bounds["fix cards T2"]["limit"],
            bounds["review rounds T2"]["limit"], bounds["capability failures T2"]["limit"]) == (10, 5, 1, 4, 6)

    explicit_none = _bounds(_build(conn, tmp_path, project=_project(tmp_path, budgets={"max_cards": None})))
    assert explicit_none["cards in plan"]["limit"] is None  # a config that says "no limit" means it


# --- Budget ---------------------------------------------------------------------------------------------------


def test_capped_provider_arithmetic(scenario_report):
    row = next(r for r in scenario_report["budget"]["providers"] if r["provider"] == "openrouter")
    assert (row["limit"], row["used"], row["remaining"], row["reserve"]) == (50, 37, 13, 5)
    assert scenario_report["budget"]["reserve_percent"] == 10


def test_credits_change_the_limit_and_an_overspent_day_has_nothing_remaining(conn, tmp_path, monkeypatch):
    _fake_hermes(monkeypatch, {})
    ledger.record_usage(conn, "openrouter", "m", 60, now=NOW)  # `_build` below reports as of NOW; same day
    capped = {"providers": {"openrouter": {"limits": {"per_day_default": 50, "per_day_after_credits": 1000},
                                           "credits_purchased": False}}}
    row = _build(conn, tmp_path, models_config=capped)["budget"]["providers"][0]
    assert (row["limit"], row["used"], row["remaining"], row["reserve"]) == (50, 60, 0, 5)

    paid = {"providers": {"openrouter": {"limits": {"per_day_default": 50, "per_day_after_credits": 1000},
                                         "credits_purchased": True}}}
    row = _build(conn, tmp_path, models_config=paid)["budget"]["providers"][0]
    assert (row["limit"], row["used"], row["remaining"], row["reserve"]) == (1000, 60, 940, 100)


def test_uncapped_provider_has_no_numbers_and_reads_no_known_cap(scenario_report):
    row = next(r for r in scenario_report["budget"]["providers"] if r["provider"] == "xkiro")
    assert (row["limit"], row["used"], row["remaining"], row["reserve"]) == (None, 12, None, None)
    assert "Budget: xkiro 12 used (no known cap)" in report.render_status(scenario_report).splitlines()
    text = report.render_text(scenario_report)
    assert re.search(r"^xkiro\s+-\s+12\s+no known cap\s+-", text, re.MULTILINE)
    assert "no known cap" in report.render_html(scenario_report)


def test_a_providers_daily_total_sums_its_models_and_ignores_other_days_and_providers(conn, tmp_path, monkeypatch):
    _fake_hermes(monkeypatch, {})
    ledger.record_usage(conn, "openrouter", "model-a", 10, now=NOW)  # `_build` below reports as of NOW
    ledger.record_usage(conn, "openrouter", "model-b", 5, now=NOW)
    ledger.record_usage(conn, "xkiro", "model-a", 7, now=NOW)
    conn.execute("INSERT INTO requests_ledger (provider, model, utc_date, count, updated_at) "
                 "VALUES ('openrouter', 'model-a', '2000-01-01', 40, 'x')")
    rows = {r["provider"]: r for r in _build(conn, tmp_path)["budget"]["providers"]}
    assert (rows["openrouter"]["used"], rows["openrouter"]["remaining"]) == (15, 35)
    assert rows["xkiro"]["used"] == 7


def test_providers_follow_the_models_config_and_show_a_configured_status(conn, tmp_path, monkeypatch):
    _fake_hermes(monkeypatch, {})
    config_with_status = {"providers": {
        "opencode_free": {"limits": {}, "status": "blocked"}, "xkiro": {"limits": {}},
    }}
    rep = _build(conn, tmp_path, models_config=config_with_status)
    assert [(r["provider"], r["status"]) for r in rep["budget"]["providers"]] == [
        ("opencode_free", "blocked"), ("xkiro", None)]
    assert "Budget: opencode_free 0 used (no known cap), status blocked" in report.render_status(rep)

    empty = _build(conn, tmp_path, models_config={})
    assert empty["budget"]["providers"] == []
    assert "Budget: no providers in the models config" in report.render_status(empty)
    assert "Requests today per provider: no providers in the models config" in report.render_text(empty)


def test_the_daily_reserve_comes_from_the_budgets(conn, tmp_path, monkeypatch):
    """ASES-CAP-03: a budgets block that omits daily_reserve_percent still reserves the blueprint's 10 percent
    (ledger.DEFAULT_DAILY_RESERVE_PERCENT), the same fallback policy.check_budget and bounds.Bounds use, not 0
    -- the report must never claim more usable quota than the gate that actually parks cards allows."""
    _fake_hermes(monkeypatch, {})
    rep = _build(conn, tmp_path, project=_project(tmp_path, budgets={}))
    assert rep["budget"]["reserve_percent"] == 10
    assert rep["budget"]["providers"][0]["reserve"] == 5  # int(50 * 10 / 100)
    rep = _build(conn, tmp_path, project=_project(tmp_path, budgets={"daily_reserve_percent": 0}))
    assert rep["budget"]["reserve_percent"] == 0  # an explicit 0 still means 0, never the default
    assert rep["budget"]["providers"][0]["reserve"] == 0
    rep = _build(conn, tmp_path, project=_project(tmp_path, budgets={"daily_reserve_percent": 25}))
    assert rep["budget"]["reserve_percent"] == 25
    assert rep["budget"]["providers"][0]["reserve"] == 12  # int(50 * 25 / 100): the arithmetic can_afford uses


@pytest.mark.parametrize("now, day, reset", [
    (datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc), "2026-09-19", "2026-09-20T00:00:00+00:00"),
    (datetime(2026, 9, 19, 23, 59, 59, tzinfo=timezone.utc), "2026-09-19", "2026-09-20T00:00:00+00:00"),
    (datetime(2026, 9, 19, 0, 0, 0, tzinfo=timezone.utc), "2026-09-19", "2026-09-20T00:00:00+00:00"),
    (datetime(2026, 12, 31, 5, 0, 0, tzinfo=timezone.utc), "2026-12-31", "2027-01-01T00:00:00+00:00"),
    (datetime(2026, 9, 19, 23, 30, 0, tzinfo=timezone(timedelta(hours=-5))), "2026-09-20",
     "2026-09-21T00:00:00+00:00"),                                          # 04:30 UTC the next day
])
def test_the_utc_day_and_the_next_reset(conn, tmp_path, monkeypatch, now, day, reset):
    _fake_hermes(monkeypatch, {})
    budget = _build(conn, tmp_path, now=now)["budget"]
    assert (budget["day"], budget["next_reset"]) == (day, reset)


def test_requests_today_by_model_and_role(scenario_report):
    by_model = scenario_report["budget"]["by_model"]
    assert by_model == [
        {"provider": "openrouter", "model": REVIEWER_MODEL, "role": "reviewer", "profile": "reviewer",
         "requests": 30, "sessions": 1, "tok_in": 90000, "tok_out": 1500},
        {"provider": "xkiro", "model": CODER_MODEL, "role": "coder", "profile": "coder-1",
         "requests": 12, "sessions": 2, "tok_in": 1200, "tok_out": 120},   # yesterday's 99 is not today's
    ]


def test_requests_today_counts_every_project_and_tolerates_missing_tokens(conn, tmp_path, monkeypatch):
    """The quota is per account, so another project's requests count; a legacy row can lack tokens or a project;
    a profile no role uses has no role."""
    _fake_hermes(monkeypatch, {})
    _ingested(conn, "a", "coder-1", "xkiro", CODER_MODEL, 3, 10, 5, "2026-09-19 08:00:00", project="other")
    conn.execute("INSERT INTO usage_ingested (session_id, profile, provider, model, requests, ingested_at) "
                 "VALUES ('b', 'stranger', 'xkiro', 'm', 5, '2026-09-19 09:00:00')")
    conn.execute("INSERT INTO usage_ingested (session_id, profile, provider, model, requests, ingested_at) "
                 "VALUES ('c', 'stranger', 'xkiro', 'm', 2, 'garbage')")
    by_model = _build(conn, tmp_path)["budget"]["by_model"]
    assert by_model == [
        {"provider": "xkiro", "model": "m", "role": None, "profile": "stranger", "requests": 5, "sessions": 1,
         "tok_in": None, "tok_out": None},
        {"provider": "xkiro", "model": CODER_MODEL, "role": "coder", "profile": "coder-1", "requests": 3,
         "sessions": 1, "tok_in": 10, "tok_out": 5},
    ]


def test_requests_today_are_ordered_and_stop_at_midnight(conn, tmp_path, monkeypatch):
    """Most requests first, then provider, model and profile; the whole UTC day is in, the next day is not."""
    _fake_hermes(monkeypatch, {})
    rows = [
        ("s1", "coder-1", "xkiro", "b", 5, "2026-09-19 00:00:00"),         # the first second of the day
        ("s2", "coder-1", "xkiro", "a", 5, "2026-09-19 23:59:59"),         # the last
        ("s3", "reviewer", "openrouter", "z", 5, "2026-09-19 12:00:00"),
        ("s4", "stranger", "xkiro", "a", 5, "2026-09-19 12:00:00"),        # same provider and model, another profile
        ("s5", "coder-1", "xkiro", "big", 9, "2026-09-19 12:00:00"),
        ("s6", "coder-1", "xkiro", "tomorrow", 50, "2026-09-20 00:00:00"),
    ]
    for session, profile, provider, model, requests, at in rows:
        _ingested(conn, session, profile, provider, model, requests, 1, 1, at)
    by_model = _build(conn, tmp_path)["budget"]["by_model"]
    assert [(r["provider"], r["model"], r["profile"], r["requests"]) for r in by_model] == [
        ("xkiro", "big", "coder-1", 9), ("openrouter", "z", "reviewer", 5), ("xkiro", "a", "coder-1", 5),
        ("xkiro", "a", "stranger", 5), ("xkiro", "b", "coder-1", 5)]


def test_parked_cards_are_this_plans_and_carry_the_latest_reason(scenario, tmp_path):
    conn, project, calls = scenario
    rep = _build(conn, tmp_path, project=project)
    assert rep["budget"]["parked"] == [{
        "task_key": "T3", "card_id": "w3", "title": "title of w3",
        "reason": "review budget on openrouter: needs 20, only 13 usable today",
        "parked_at": "2026-09-19T11:30:00+00:00",
    }]  # ow3 is scheduled too, but it belongs to another project on the same board
    assert rep["budget"]["parked_error"] is None
    assert calls["list"] == [("b", "scheduled")]


def test_a_parked_card_with_no_recorded_reason(conn, tmp_path, monkeypatch):
    _seed_tasks(conn)
    _fake_hermes(monkeypatch, _quiet_board(w2=_card("w2", "scheduled")))
    parked = _build(conn, tmp_path)["budget"]["parked"]
    assert parked == [{"task_key": "T2", "card_id": "w2", "title": "title of w2", "reason": None,
                       "parked_at": None}]
    assert "  T2 title of w2: no reason recorded" in report.render_status(_build(conn, tmp_path)).splitlines()


def test_an_empty_title_and_reason_read_as_dashes(conn, tmp_path, monkeypatch):
    _seed_tasks(conn)
    _event(conn, "2026-09-19T10:00:00+00:00", "card_parked_for_budget", {"task_key": "T3", "reason": ""})
    _fake_hermes(monkeypatch, _quiet_board(w3=_card("w3", "scheduled", title="")))
    rep = _build(conn, tmp_path)
    assert (rep["budget"]["parked"][0]["title"], rep["budget"]["parked"][0]["reason"]) == ("", "")
    assert "  T3: no reason recorded" in report.render_status(rep).splitlines()
    assert re.search(r"^T3\s+w3\s+-\s+-\s+2026-09-19T10:00:00\+00:00$", report.render_text(rep), re.MULTILINE)


def test_a_parked_merge_card_is_found_too(conn, tmp_path, monkeypatch):
    _seed_tasks(conn)
    _event(conn, "2026-09-19T11:00:00+00:00", "card_parked_for_budget", {"task_key": "T1", "reason": "r"})
    _fake_hermes(monkeypatch, _quiet_board(m1=_card("m1", "scheduled", assignee=None)))
    assert [p["card_id"] for p in _build(conn, tmp_path)["budget"]["parked"]] == ["m1"]


def test_parked_events_of_a_task_key_that_is_not_text_are_ignored(conn, tmp_path, monkeypatch):
    _seed_tasks(conn)
    _event(conn, "2026-09-19T11:00:00+00:00", "card_parked_for_budget", {"task_key": ["T2"], "reason": "x"})
    _event(conn, "2026-09-19T11:01:00+00:00", "card_parked_for_budget", {"reason": "no task at all"})
    _fake_hermes(monkeypatch, _quiet_board(w2=_card("w2", "scheduled")))
    assert _build(conn, tmp_path)["budget"]["parked"][0]["reason"] is None


@pytest.mark.parametrize("error", [
    hermes.HermesCommandError(["kanban", "list"], 1, "boom"), hermes.HermesNotFound("no hermes"),
    subprocess.TimeoutExpired("hermes", 30), RuntimeError("weird"),
])
def test_a_failing_list_is_reported_not_raised(conn, tmp_path, monkeypatch, error):
    _seed_tasks(conn)
    _fake_hermes(monkeypatch, _quiet_board())

    def broken(board, status=None, assignee=None):
        raise error

    monkeypatch.setattr(hermes, "kanban_list", broken)
    rep = _build(conn, tmp_path)
    assert rep["budget"]["parked"] == []
    assert rep["budget"]["parked_error"].startswith(type(error).__name__)
    assert rep["cards"]["counts"] == {"ready": 1, "todo": 2}   # the rest of the report is intact
    assert "Parked: unavailable (" + type(error).__name__ in report.render_status(rep)
    assert "Parked cards: unavailable (" in report.render_text(rep)
    assert "Parked cards: unavailable (" in report.render_html(rep)


def test_error_text_is_cut_at_200_characters(conn, tmp_path, monkeypatch):
    _seed_tasks(conn)
    for length, expected in ((186, "RuntimeError: " + "e" * 186), (187, "RuntimeError: " + "e" * 183 + "...")):
        _fake_hermes(monkeypatch, _quiet_board(), failing=("w1",), error=RuntimeError("e" * length))

        def broken(board, status=None, assignee=None, error=RuntimeError("e" * length)):
            raise error

        monkeypatch.setattr(hermes, "kanban_list", broken)
        rep = _build(conn, tmp_path)
        assert rep["cards"]["tasks"][0]["work"]["error"] == expected and len(expected) <= 200
        assert rep["budget"]["parked_error"] == expected


# --- Cards ----------------------------------------------------------------------------------------------------


def test_the_project_panel_reports_the_plan_and_the_arguments_it_was_built_from(conn, tmp_path, monkeypatch):
    _seed_tasks(conn)
    calls = _fake_hermes(monkeypatch, _quiet_board())
    plan = dataclasses.replace(PLAN, integration_branch="main")
    project = dataclasses.replace(_project(tmp_path), data_class="confidential", name="another")
    rep = report.build_report("zboard", plan, project, MODELS_CONFIG, conn, now=NOW)
    assert (rep["project"]["board"], rep["cards"]["board"]) == ("zboard", "zboard")
    assert rep["project"]["integration_branch"] == "main"    # what the merge queue merges into: the plan's
    assert (rep["project"]["data_class"], rep["project"]["name"]) == ("confidential", "another")
    assert set(calls["boards"]) == {"zboard"} and calls["list"] == [("zboard", "scheduled")]


def test_cards_panel_from_the_board(scenario, tmp_path):
    conn, project, calls = scenario
    cards = _build(conn, tmp_path, project=project)["cards"]

    assert cards["board"] == "b" and cards["dashboard"] == "http://127.0.0.1:9119"
    assert cards["tasks"][0] == {
        "task_key": "T1", "title": "title of w1", "role": "coder", "fix_cards": 1,
        "work": {"id": "w1", "status": "done", "assignee": "coder-1", "title": "title of w1", "error": None},
        "merge": {"id": "m1", "status": "done", "assignee": None, "title": "title of m1", "error": None},
    }
    assert [(t["task_key"], t["work"]["status"], t["merge"]["status"]) for t in cards["tasks"]] == [
        ("T1", "done", "done"), ("T2", "blocked", "blocked"), ("T3", "scheduled", "blocked")]
    assert cards["tasks"][2]["work"]["assignee"] == "reviewer" and cards["tasks"][2]["role"] == "reviewer"
    assert cards["counts"] == {"done": 1, "scheduled": 1, "blocked": 1}
    assert cards["merge_queue"] == {"done": 1, "total": 3, "counts": {"done": 1, "blocked": 2}}
    assert cards["open_questions"] == 1
    assert cards["questions"] == [{"card_id": "w2", "task_key": "T2", "kind": "work",
                                   "question": "which database should the app use?"}]
    assert sorted(calls["show"]) == ["m1", "m2", "m3", "w1", "w2", "w3"]   # each once, and only this plan's
    assert set(calls["boards"]) == {"b"}


def test_a_fix_card_is_the_tasks_current_work_card(conn, tmp_path, monkeypatch):
    _seed_tasks(conn, fix_cards=(0, 1, 0))
    conn.execute("UPDATE plan_tasks SET work_card_id = 'wfix' WHERE task_key = 'T2'")
    _fake_hermes(monkeypatch, _quiet_board(wfix=_card("wfix", "running")))
    cards = _build(conn, tmp_path)["cards"]
    assert (cards["tasks"][1]["work"]["id"], cards["tasks"][1]["work"]["status"]) == ("wfix", "running")
    assert cards["tasks"][1]["fix_cards"] == 1
    assert cards["counts"] == {"running": 1, "ready": 1, "todo": 1}


@pytest.mark.parametrize("error", [
    hermes.HermesCommandError(["kanban", "show", "w2"], 1, "no such card"), hermes.HermesNotFound("no hermes"),
    subprocess.TimeoutExpired("hermes", 30), RuntimeError("weird"), KeyError("task"),
])
def test_a_failing_show_becomes_unknown_and_the_rest_is_intact(scenario, tmp_path, monkeypatch, error):
    conn, project, _calls = scenario
    _fake_hermes(monkeypatch, {
        "w1": _card("w1", "done"), "m1": _card("m1", "done", assignee=None),
        "w2": _card("w2", "blocked", events=[_blocked("q?")]), "m2": _card("m2", "blocked", assignee=None),
        "w3": _card("w3", "scheduled"), "m3": _card("m3", "blocked", assignee=None),
    }, failing=("w2",), error=error)
    rep = _build(conn, tmp_path, project=project)

    broken = rep["cards"]["tasks"][1]["work"]
    assert broken["status"] == "unknown" and broken["assignee"] is None and broken["title"] is None
    assert broken["id"] == "w2" and broken["error"].startswith(type(error).__name__)
    assert rep["cards"]["tasks"][1]["title"] == "wire the database"   # falls back to the plan's title
    assert rep["cards"]["tasks"][0]["work"]["status"] == "done"
    assert rep["cards"]["tasks"][2]["work"]["status"] == "scheduled"
    assert rep["cards"]["counts"] == {"done": 1, "scheduled": 1, "unknown": 1}
    assert rep["cards"]["open_questions"] == 0   # a card that cannot be shown asks nothing we can read
    # Every other panel is untouched.
    assert len(rep["events"]) == 10 and rep["quality"]["gate_runs"] and rep["budget"]["by_model"]
    text = report.render_text(rep)
    assert "Cards Hermes could not show" in text
    assert "1 unknown" in report.render_status(rep)


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit, _RealHermesReached])
def test_an_interrupt_is_not_swallowed_as_an_unknown_card_or_an_unreadable_list(conn, tmp_path, monkeypatch, interrupt):
    """Only Exception is caught around the Hermes reads: a Ctrl-C during `swarm status` must stop it."""
    _seed_tasks(conn)
    _fake_hermes(monkeypatch, _quiet_board())

    def interrupted(*args, **kwargs):
        raise interrupt()

    monkeypatch.setattr(hermes, "kanban_show", interrupted)
    with pytest.raises(interrupt):
        _build(conn, tmp_path)
    monkeypatch.setattr(hermes, "kanban_show", lambda board, card_id: _quiet_board()[card_id])
    monkeypatch.setattr(hermes, "kanban_list", interrupted)
    with pytest.raises(interrupt):
        _build(conn, tmp_path)


@pytest.mark.parametrize("answer", [None, [], "a string", {"no": "status"}, {"status": None}, {"status": ""}])
def test_a_show_that_does_not_return_a_card_is_unknown(conn, tmp_path, monkeypatch, answer):
    _seed_tasks(conn)
    _fake_hermes(monkeypatch, _quiet_board())
    monkeypatch.setattr(hermes, "kanban_show", lambda board, card_id: answer)
    rep = _build(conn, tmp_path)
    assert rep["cards"]["counts"] == {"unknown": 3}
    assert {t["work"]["status"] for t in rep["cards"]["tasks"]} == {"unknown"}


def test_a_task_with_no_cards_yet_reads_not_created(conn, tmp_path, monkeypatch):
    _seed_tasks(conn, fix_cards=(0, 0))          # T3 has no plan_tasks row
    conn.execute("UPDATE plan_tasks SET merge_card_id = NULL WHERE task_key = 'T2'")
    calls = _fake_hermes(monkeypatch, _quiet_board())
    cards = _build(conn, tmp_path)["cards"]
    assert cards["tasks"][2]["work"] == {"id": None, "status": "not created", "assignee": None, "title": None,
                                         "error": None}
    assert cards["tasks"][2]["merge"]["status"] == "not created" and cards["tasks"][2]["fix_cards"] == 0
    assert cards["tasks"][2]["title"] == "review it all"
    assert cards["tasks"][1]["merge"]["status"] == "not created" and cards["tasks"][1]["work"]["id"] == "w2"
    assert cards["counts"] == {"ready": 1, "todo": 1, "not created": 1}
    assert sorted(calls["show"]) == ["m1", "w1", "w2"]


def test_status_counts_follow_the_lifecycle_order_and_unknown_statuses_sort_last(conn, tmp_path, monkeypatch):
    _seed_tasks(conn)
    _fake_hermes(monkeypatch, _quiet_board(
        w1=_card("w1", "zzz-new"), w2=_card("w2", "blocked"), w3=_card("w3", "done"),
    ))
    assert list(_build(conn, tmp_path)["cards"]["counts"]) == ["done", "blocked", "zzz-new"]
    assert report._count(["blocked", "aaa", "done", "running", "bbb", "done"]) == {
        "done": 2, "running": 1, "blocked": 1, "aaa": 1, "bbb": 1}
    lifecycle = ["done", "review", "running", "ready", "todo", "scheduled", "blocked", "triage", "archived",
                 "unknown", "not created"]
    assert list(report._count([*reversed(lifecycle), "zzz", "aaa"])) == [*lifecycle, "aaa", "zzz"]


QUESTION_CASES = [
    pytest.param([_blocked("which db?")], "which db?", id="a blocked event with a reason"),
    pytest.param([], None, id="no blocked event: a card that only waits"),
    pytest.param([_blocked("")], None, id="an empty reason"),
    pytest.param([_blocked("   ")], None, id="a blank reason"),
    pytest.param([{"kind": "blocked", "payload": {}, "created_at": 1}], None, id="no reason key"),
    pytest.param([{"kind": "blocked", "payload": {"reason": 5}, "created_at": 1}], None, id="reason not text"),
    pytest.param([{"kind": "blocked", "payload": None, "created_at": 1}], None, id="no payload"),
    pytest.param([_blocked("  which db?  ")], "which db?", id="the reason is stripped"),
    pytest.param([_blocked("old?", 1), _blocked("new?", 5)], "new?", id="the latest by created_at"),
    pytest.param([_blocked("new?", 5), _blocked("old?", 1)], "new?", id="the latest even when listed first"),
    pytest.param([_blocked("old?", "9"), _blocked("new?", 10)], "new?", id="times compare as numbers"),
    pytest.param([_blocked("old?", 1), _blocked("", 5)], None, id="a newer empty reason asks nothing"),
    pytest.param([_blocked("first?", 3), _blocked("second?", 3)], "second?", id="same time: the later listed"),
    pytest.param([_blocked("a?", True), _blocked("b?", 0)], "b?", id="a bool is no time at all"),
    pytest.param([{"kind": "blocked", "payload": '{"reason": "json?"}', "created_at": 1}], "json?",
                 id="a payload that is JSON text"),
    pytest.param([{"kind": "blocked", "payload": "not json", "created_at": 1}], None, id="unreadable payload text"),
    pytest.param(["nonsense", None, {"kind": "commented", "payload": {"reason": "no"}}, _blocked("real?")],
                 "real?", id="other events are ignored"),
    pytest.param([_blocked("numeric?", "7")], "numeric?", id="created_at as numeric text"),
    pytest.param([_blocked("garbled?", "soon")], "garbled?", id="created_at that is not a number"),
    pytest.param([_blocked("a?", 0), _blocked("b?", -5)], "b?", id="a negative time counts as 0"),
    pytest.param([_blocked("a?", float("inf")), _blocked("b?", 0)], "b?", id="an infinite time counts as 0"),
    pytest.param([_blocked("a?", None), _blocked("b?", 0)], "b?", id="a missing time counts as 0"),
]


@pytest.mark.parametrize("card_events, expected", QUESTION_CASES)
def test_open_question_rule_matches_swarm_questions(conn, tmp_path, monkeypatch, card_events, expected):
    _seed_tasks(conn)
    _fake_hermes(monkeypatch, _quiet_board(w1=_card("w1", "blocked", events=card_events)))
    cards = _build(conn, tmp_path)["cards"]
    if expected is None:
        assert cards["questions"] == [] and cards["open_questions"] == 0
    else:
        assert cards["questions"] == [{"card_id": "w1", "task_key": "T1", "kind": "work", "question": expected}]
        assert cards["open_questions"] == 1


@pytest.mark.parametrize("card", [{"id": "w1", "status": "blocked"}, {"id": "w1", "status": "blocked", "_events": None}])
def test_a_blocked_card_without_an_events_list_asks_nothing(conn, tmp_path, monkeypatch, card):
    _seed_tasks(conn)
    _fake_hermes(monkeypatch, _quiet_board(w1=card))
    cards = _build(conn, tmp_path)["cards"]
    assert cards["open_questions"] == 0 and cards["counts"]["blocked"] == 1


def test_a_blocked_event_on_a_card_that_is_not_blocked_is_not_a_question(conn, tmp_path, monkeypatch):
    _seed_tasks(conn)
    _fake_hermes(monkeypatch, _quiet_board(w1=_card("w1", "ready", events=[_blocked("answered long ago?")])))
    assert _build(conn, tmp_path)["cards"]["open_questions"] == 0


@pytest.mark.parametrize("length, expected", [(1000, "x" * 1000), (1001, "x" * 997 + "..."), (5000, "x" * 997 + "...")])
def test_a_long_question_is_cut_in_the_data_at_a_thousand_characters(conn, tmp_path, monkeypatch, length, expected):
    _seed_tasks(conn)
    _fake_hermes(monkeypatch, _quiet_board(w1=_card("w1", "blocked", events=[_blocked("x" * length)])))
    assert _build(conn, tmp_path)["cards"]["questions"][0]["question"] == expected


def test_a_question_is_never_cut_in_the_terminal(conn, tmp_path, monkeypatch):
    _seed_tasks(conn)
    question = "please decide " * 65    # 910 characters, well past a table cell's limit and under the data cut
    _fake_hermes(monkeypatch, _quiet_board(w1=_card("w1", "blocked", events=[_blocked(question)])))
    text = report.render_text(_build(conn, tmp_path))
    assert question.strip() in text


def test_a_merge_card_the_controller_blocked_is_a_question_on_a_merge_card(conn, tmp_path, monkeypatch):
    _seed_tasks(conn)
    _fake_hermes(monkeypatch, _quiet_board(
        m2=_card("m2", "blocked", assignee=None, events=[_blocked("fix-card budget (2) exhausted for T2")]),
    ))
    rep = _build(conn, tmp_path)
    assert rep["cards"]["questions"] == [{"card_id": "m2", "task_key": "T2", "kind": "merge",
                                          "question": "fix-card budget (2) exhausted for T2"}]
    assert rep["cards"]["counts"] == {"ready": 1, "todo": 2}   # no work card is blocked
    assert "Cards: 1 ready, 2 todo, 1 question on merge cards; merge queue 0/3 done" in report.render_status(rep)


def test_questions_on_a_work_card_and_a_merge_card_are_all_counted(conn, tmp_path, monkeypatch):
    _seed_tasks(conn)
    _fake_hermes(monkeypatch, _quiet_board(
        w1=_card("w1", "blocked", events=[_blocked("which db?")]),
        m2=_card("m2", "blocked", assignee=None, events=[_blocked("budget spent")]),
    ))
    rep = _build(conn, tmp_path)
    assert rep["cards"]["open_questions"] == 2
    assert [q["kind"] for q in rep["cards"]["questions"]] == ["work", "merge"]
    assert "Cards: 2 todo, 1 blocked (2 questions); merge queue 0/3 done" in report.render_status(rep)


def test_every_source_of_a_question_is_counted_and_reported_by_source(conn, tmp_path, monkeypatch):
    """Blocked with a reason, given up on by the dispatcher (no `blocked` event), sent to triage by an unblock loop,
    and asked by ASES as a comment on a merge card Hermes will not block: four cards, four sources."""
    _seed_tasks(conn)
    _fake_hermes(monkeypatch, _quiet_board(
        w1=_card("w1", "blocked", events=[_blocked("which db?")]),
        w2=_card("w2", "blocked", events=[_gave_up(3, "HTTP 503")]),
        w3=_card("w3", "triage", events=[_loop("which port?")]),
        m1=_card("m1", "blocked", assignee=None, comments=[_asked("merge failed twice: retry or stop?")]),
    ))

    cards = _build(conn, tmp_path)["cards"]

    assert cards["open_questions"] == 4
    assert [(q["card_id"], q["task_key"], q["kind"], q["question"]) for q in cards["questions"]] == [
        ("w1", "T1", "work", "which db?"),
        ("m1", "T1", "merge", "merge failed twice: retry or stop?"),
        ("w2", "T2", "work", "gave up after 3 failure(s): HTTP 503"),
        ("w3", "T3", "work", "which port?"),
    ]
    assert cards["questions_by_source"] == {"blocked": 1, "gave_up": 1, "block_loop": 1, "ases_comment": 1}
    assert list(cards["questions_by_source"]) == ["blocked", "gave_up", "block_loop", "ases_comment"]
    assert cards["counts"] == {"blocked": 2, "triage": 1}


def test_a_merge_card_created_blocked_is_no_question_even_with_the_blocked_event_hermes_writes_for_it(
    conn, tmp_path, monkeypatch,
):
    """Hermes 0.21.3 records the creation of a card with initial_status blocked as a `blocked` event whose reason is
    "initial_status". The controller creates every merge card that way, so a report that counted it would show every
    task as waiting on a question."""
    _seed_tasks(conn)
    creation = {"kind": "blocked", "payload": {"reason": "initial_status", "status": "blocked", "actor": "ases"},
                "created_at": 1, "run_id": None}
    _fake_hermes(monkeypatch, _quiet_board(**{
        f"m{n}": _card(f"m{n}", "blocked", assignee=None, events=[creation]) for n in (1, 2, 3)
    }))

    cards = _build(conn, tmp_path)["cards"]

    assert cards["open_questions"] == 0 and cards["questions"] == [] and cards["questions_by_source"] == {}
    assert cards["merge_queue"]["counts"] == {"blocked": 3}


def test_questions_by_source_lists_only_the_sources_that_occur_and_counts_repeats(conn, tmp_path, monkeypatch):
    _seed_tasks(conn)
    _fake_hermes(monkeypatch, _quiet_board(
        w1=_card("w1", "blocked", events=[_gave_up(2, "a")]), w2=_card("w2", "blocked", events=[_gave_up(3, "b")]),
        w3=_card("w3", "blocked", events=[_blocked("which db?")]),
    ))
    assert _build(conn, tmp_path)["cards"]["questions_by_source"] == {"blocked": 1, "gave_up": 2}

    _fake_hermes(monkeypatch, _quiet_board())
    cards = _build(conn, tmp_path)["cards"]
    assert cards["questions_by_source"] == {} and cards["open_questions"] == 0


def test_a_question_keeps_its_shape_and_the_source_is_only_in_the_counts(conn, tmp_path, monkeypatch):
    _seed_tasks(conn)
    _fake_hermes(monkeypatch, _quiet_board(w1=_card("w1", "blocked", events=[_gave_up(3, "boom")])))
    cards = _build(conn, tmp_path)["cards"]
    assert cards["questions"] == [{"card_id": "w1", "task_key": "T1", "kind": "work",
                                   "question": "gave up after 3 failure(s): boom"}]
    assert set(cards) >= {"open_questions", "questions", "questions_by_source"}
    json.dumps(cards)


def test_a_triage_card_is_a_question_only_when_something_was_asked_on_it(conn, tmp_path, monkeypatch):
    _seed_tasks(conn)
    _fake_hermes(monkeypatch, _quiet_board(
        w1=_card("w1", "triage", events=[]),                     # an agent-proposed card waiting for validation
        w2=_card("w2", "triage", events=[_loop("which port?")]),
    ))
    cards = _build(conn, tmp_path)["cards"]
    assert [q["card_id"] for q in cards["questions"]] == ["w2"] and cards["open_questions"] == 1
    assert cards["questions_by_source"] == {"block_loop": 1} and cards["counts"] == {"todo": 1, "triage": 2}


@pytest.mark.parametrize("card_kwargs", [
    {"events": [_blocked("q?", 100), {"kind": "unblocked", "payload": None, "created_at": 200}]},
    {"events": [_blocked("q?", 100)], "comments": [{"author": "user", "body": "ANSWER: use sqlite", "created_at": 200}]},
    {"events": [_gave_up(3, "boom", 100)],
     "comments": [{"author": "default", "body": "UNBLOCK: retry", "created_at": 200}]},
    {"comments": [_asked("q?", 100), {"author": "user", "body": "ANSWER: use sqlite", "created_at": 200}]},
], ids=["unblocked-event", "answer-comment", "unblock-comment", "asked-then-answered"])
def test_a_question_that_was_answered_is_not_counted_even_while_the_card_still_reads_blocked(
    conn, tmp_path, monkeypatch, card_kwargs,
):
    _seed_tasks(conn)
    _fake_hermes(monkeypatch, _quiet_board(w1=_card("w1", "blocked", **card_kwargs)))
    cards = _build(conn, tmp_path)["cards"]
    assert cards["open_questions"] == 0 and cards["questions_by_source"] == {} and cards["counts"]["blocked"] == 1


@pytest.mark.parametrize("card_events, source, question", [
    ([_blocked("worker asked", 100), _gave_up(2, "boom", 200)], "gave_up", "gave up after 2 failure(s): boom"),
    ([_gave_up(2, "boom", 100), _blocked("worker asked", 200)], "blocked", "worker asked"),
], ids=["gave-up-is-newer", "blocked-is-newer"])
def test_the_newer_of_a_gave_up_and_a_blocked_event_decides_the_question_and_its_source(
    conn, tmp_path, monkeypatch, card_events, source, question,
):
    _seed_tasks(conn)
    _fake_hermes(monkeypatch, _quiet_board(w1=_card("w1", "blocked", events=card_events)))
    cards = _build(conn, tmp_path)["cards"]
    assert [q["question"] for q in cards["questions"]] == [question]
    assert cards["questions_by_source"] == {source: 1}


def test_the_report_uses_the_one_rule_of_the_questions_module_and_has_none_of_its_own(conn, tmp_path, monkeypatch):
    """`swarm questions`, `swarm answer`, the recovery loop and this report must not be able to drift apart."""
    _seed_tasks(conn)
    _fake_hermes(monkeypatch, _quiet_board(w1=_card("w1", "blocked"), w2=_card("w2", "ready")))
    seen = []

    def fake_open_question(card):
        seen.append((card["id"], card["status"]))
        return questions.OpenQuestion("patched?", 9, "gave_up") if card["id"] == "w1" else None

    monkeypatch.setattr(questions, "open_question", fake_open_question)
    cards = _build(conn, tmp_path)["cards"]

    assert not hasattr(report, "_open_question")
    assert ("w1", "blocked") in seen and ("w2", "ready") in seen                    # every card read goes through it
    assert [q["question"] for q in cards["questions"]] == ["patched?"]
    assert cards["questions_by_source"] == {"gave_up": 1}


def test_a_question_in_the_data_is_redacted_and_ascii_whatever_its_source(conn, tmp_path, monkeypatch):
    _seed_tasks(conn)
    text = f"why {SECRET} caf\N{LATIN SMALL LETTER E WITH ACUTE}?"
    _fake_hermes(monkeypatch, _quiet_board(
        w1=_card("w1", "blocked", events=[_gave_up(3, text)]),
        m1=_card("m1", "blocked", assignee=None, comments=[_asked(text)]),
    ))
    cards = _build(conn, tmp_path)["cards"]
    assert [q["question"] for q in cards["questions"]] == [      # T1's work card, then its merge card
        "gave up after 3 failure(s): why [redacted] caf" + chr(92) + "xe9?", "why [redacted] caf" + chr(92) + "xe9?",
    ]
    assert all(q["question"].isascii() for q in cards["questions"])


def test_the_status_line_attaches_the_questions_to_triage_when_no_work_card_is_blocked(conn, tmp_path, monkeypatch):
    _seed_tasks(conn)
    _fake_hermes(monkeypatch, _quiet_board(w1=_card("w1", "triage", events=[_loop("which port?")])))
    rep = _build(conn, tmp_path)
    assert "Cards: 2 todo, 1 triage (1 question); merge queue 0/3 done" in report.render_status(rep)
    assert "Work cards" in report.render_text(rep) and "which port?" in report.render_text(rep)


def test_the_status_line_keeps_the_questions_on_blocked_when_both_are_present(conn, tmp_path, monkeypatch):
    _seed_tasks(conn)
    _fake_hermes(monkeypatch, _quiet_board(
        w1=_card("w1", "triage", events=[_loop("which port?")]), w2=_card("w2", "blocked", events=[_gave_up()]),
    ))
    assert "Cards: 1 todo, 1 blocked (2 questions), 1 triage; merge queue 0/3 done" in report.render_status(
        _build(conn, tmp_path))


def test_a_gave_up_question_is_in_the_terminal_report_and_the_page(conn, tmp_path, monkeypatch):
    _seed_tasks(conn)
    _fake_hermes(monkeypatch, _quiet_board(w1=_card("w1", "blocked", events=[_gave_up(3, "HTTP 503 <b>")])))
    rep = _build(conn, tmp_path)
    assert "gave up after 3 failure(s): HTTP 503 <b>" in report.render_text(rep)
    assert "gave up after 3 failure(s): HTTP 503 &lt;b&gt;" in report.render_html(rep)


def test_the_cards_line_reads_like_the_blueprints_example(scenario_report):
    """"3 done, 1 running, 1 blocked (1 question)": the counts in lifecycle order, the questions on the blocked."""
    def cards_line(counts, questions):
        rep = json.loads(json.dumps(scenario_report))
        rep["cards"]["counts"] = counts
        rep["cards"]["open_questions"] = questions
        return next(line for line in report.render_status(rep).splitlines() if line.startswith("Cards:"))

    assert cards_line({"done": 3, "running": 1, "blocked": 1}, 1).startswith(
        "Cards: 3 done, 1 running, 1 blocked (1 question);")
    assert cards_line({"done": 3, "running": 1, "blocked": 2}, 2).startswith(
        "Cards: 3 done, 1 running, 2 blocked (2 questions);")
    assert cards_line({"done": 3, "blocked": 1}, 0).startswith("Cards: 3 done, 1 blocked;")
    assert cards_line({}, 0).startswith("Cards: no cards;")


def test_cards_of_another_project_sharing_the_board_are_never_read(scenario):
    conn, _project_cfg, calls = scenario
    assert not any(card_id.startswith("o") for card_id in calls["show"])


# --- Quality --------------------------------------------------------------------------------------------------


def test_quality_panels_gate_runs_omits_another_projects_row_sharing_a_task_key(conn, tmp_path, monkeypatch):
    """Round 19, package GIT12 (ASES-GIT-12, design test B9): PLAN.project is "p1"; a gate_runs row stamped
    "p2" for the SAME task key T1 must never show up in p1's own quality panel, although a legacy NULL row still
    does (report.py used to have no project predicate here at all: any project's row for a shared task key
    leaked into every other project's own panel)."""
    _fake_hermes(monkeypatch, {})
    _gate_run(conn, "T1", "gate1", "a" * 10, "pass", "2026-09-19T10:00:00+00:00", project="p1")
    _gate_run(conn, "T1", "gate1", "b" * 10, "pass", "2026-09-19T10:01:00+00:00", project="p2")
    _gate_run(conn, "T1", "gate1", "c" * 10, "pass", "2026-09-19T10:02:00+00:00", project=None)

    quality = _build(conn, tmp_path)["quality"]

    shown_shas = {row["commit_sha"] for row in quality["gate_runs"]}
    assert shown_shas == {"a" * 10, "c" * 10}  # p1's own row and the legacy NULL row; never p2's


def test_quality_panel_contents_and_order(scenario_report):
    quality = scenario_report["quality"]
    assert quality["gate_runs"] == [
        {"task_key": "__final__", "gate": "gate4", "commit_sha": "d" * 10, "result": "pass",
         "ran_at": "2026-09-19T11:45:00+00:00"},
        {"task_key": "T2", "gate": "gate1", "commit_sha": "c" * 10, "result": "fail",
         "ran_at": "2026-09-19T11:00:00+00:00"},
        {"task_key": "T1", "gate": "gate3", "commit_sha": "b" * 10, "result": "pass",
         "ran_at": "2026-09-19T10:30:00+00:00"},
        {"task_key": "T1", "gate": "gate1", "commit_sha": "a" * 10, "result": "pass",
         "ran_at": "2026-09-19T10:00:00+00:00"},
    ]   # X9 is not a task of this plan; nothing of a gate's output is kept
    assert quality["review_verdicts"] == [
        {"task_key": "T2", "commit_sha": "e" * 10, "card_id": "w2", "outcome": "CHANGES_REQUIRED",
         "reviewer_profile": "reviewer", "tamper_suspected": False, "recorded_at": "2026-09-19T11:05:00+00:00"},
        {"task_key": "T1", "commit_sha": "b" * 10, "card_id": "w1", "outcome": "PASS",
         "reviewer_profile": "reviewer", "tamper_suspected": True, "recorded_at": "2026-09-19T10:35:00+00:00"},
    ]
    assert quality["merge_records"] == [
        {"task_key": "T1", "squash_commit": "b" * 10, "candidate_sha": "c" * 10, "gate3_result": "pass",
         "reverted": False, "completed_at": "2026-09-19T10:31:00+00:00"},
        {"task_key": "T2", "squash_commit": None, "candidate_sha": "e" * 10, "gate3_result": "fail",
         "reverted": False, "completed_at": None},
    ]
    assert quality["findings"] == [
        {"ts": "2026-09-19T11:55:00+00:00", "kind": "integrity_violation", "task_key": None,
         "message": "primary checkout is dirty: M a.py"},
        {"ts": "2026-09-19T11:45:00+00:00", "kind": "gate_tamper_suspected", "task_key": "T1",
         "message": "T1: a test was deleted"},
        {"ts": "2026-09-19T11:20:00+00:00", "kind": "merge_refused_unreviewed", "task_key": "T2",
         "message": 'T2: {"card_id":"w2","completed_by":"coder-1","needs_completion_by":"reviewer"}'},
    ]   # merge_failed, merged and the parking events are not quality findings


def test_gate_output_never_enters_the_report(scenario_report):
    assert "GATE-DETAIL-TEXT" not in json.dumps(scenario_report)


def test_reverted_merge_records_and_no_op_merges(conn, tmp_path, monkeypatch):
    _fake_hermes(monkeypatch, {})
    _merge_record(conn, "T2", "e" * 40, "skipped", None, 0, "2026-09-19T11:00:00+00:00")
    _merge_record(conn, "T1", "c" * 40, "pass", "b" * 40, 1, "2026-09-19T10:31:00+00:00")
    merges = _build(conn, tmp_path)["quality"]["merge_records"]
    assert [(m["task_key"], m["gate3_result"], m["squash_commit"], m["reverted"]) for m in merges] == [
        ("T1", "pass", "b" * 10, True), ("T2", "skipped", None, False)]   # plan order, whatever the insert order
    text = report.render_text(_build(conn, tmp_path))
    assert re.search(r"^T1\s+b{10}\s+c{10}\s+pass\s+yes\s+2026-09-19T10:31:00\+00:00$", text, re.MULTILINE)
    assert re.search(r"^T2\s+-\s+e{10}\s+skipped\s+no\s+2026-09-19T11:00:00\+00:00$", text, re.MULTILINE)


def test_gate_runs_at_the_same_second_keep_their_insertion_order(conn, tmp_path, monkeypatch):
    _fake_hermes(monkeypatch, {})
    for gate in ("gate1", "gate2", "gate3"):
        _gate_run(conn, "T1", gate, "a" * 40, "pass", "2026-09-19T10:00:00+00:00")
    _gate_run(conn, "T1", "old", None, "fail", "2026-09-19T09:00:00+00:00")   # inserted last, but older
    runs = _build(conn, tmp_path)["quality"]["gate_runs"]
    assert [(r["gate"], r["commit_sha"]) for r in runs] == [
        ("gate3", "a" * 10), ("gate2", "a" * 10), ("gate1", "a" * 10), ("old", None)]


def test_quality_lists_are_bounded(conn, tmp_path, monkeypatch):
    _fake_hermes(monkeypatch, {})
    for n in range(25):
        stamp = f"2026-09-19T10:{n:02d}:00+00:00"
        _gate_run(conn, "T1", f"g{n}", "a" * 40, "pass", stamp)
        _verdict(conn, "p1", "T1", f"{n:040x}", "w1", "PASS", None, stamp)
        _event(conn, stamp, "integrity_violation", {"problems": [f"p{n}"]})
    quality = _build(conn, tmp_path)["quality"]
    assert [len(quality[k]) for k in ("gate_runs", "review_verdicts", "findings")] == [20, 20, 20]
    assert quality["gate_runs"][0]["gate"] == "g24" and quality["findings"][0]["message"] == "p24"


@pytest.mark.parametrize("metadata, expected", [
    (None, False), ("", False), ("not json", False), ("[1]", False), ('"true"', False),
    ('{"gate_tampering_suspected": false}', False), ('{"gate_tampering_suspected": "yes"}', False),
    ('{"other": true}', False), ('{"gate_tampering_suspected": true}', True),
])
def test_a_verdict_flags_tampering_only_when_the_reviewer_said_so(conn, tmp_path, monkeypatch, metadata, expected):
    _fake_hermes(monkeypatch, {})
    _verdict(conn, "p1", "T1", "b" * 40, "w1", "PASS", metadata, "2026-09-19T10:35:00+00:00")
    assert _build(conn, tmp_path)["quality"]["review_verdicts"][0]["tamper_suspected"] is expected


def test_quality_findings_cover_the_refusal_family_integrity_and_any_tamper_kind(conn, tmp_path, monkeypatch):
    _fake_hermes(monkeypatch, {})
    kinds = ["merge_refused_unreviewed", "merge_refused_invalid_verdict", "merge_refused_verdict_commit_mismatch",
             "integrity_violation", "gate_tampering_suspected", "TAMPER_found", "test_tamper"]
    ignored = ["merge_refusedX", "merged", "merge_failed", "refused", "gate1_recheck_failed", "temper"]
    for number, kind in enumerate(kinds + ignored):
        _event(conn, f"2026-09-19T10:{number:02d}:00+00:00", kind, {"task_key": "T1"})
    found = [f["kind"] for f in _build(conn, tmp_path)["quality"]["findings"]]
    assert sorted(found) == sorted(kinds)


# --- Health ---------------------------------------------------------------------------------------------------


def test_health_counts_newest_time_and_message_per_kind(scenario_report):
    health = scenario_report["health"]
    assert [k["kind"] for k in health["kinds"]] == list(report.HEALTH_KINDS)
    by_kind = {k["kind"]: k for k in health["kinds"]}
    assert by_kind["card_parked_for_budget"] == {
        "kind": "card_parked_for_budget", "count": 2, "newest_at": "2026-09-19T11:30:00+00:00",
        "newest_message": "T3: review budget on openrouter: needs 20, only 13 usable today"}
    assert by_kind["merge_failed"]["count"] == 1
    assert by_kind["merge_failed"]["newest_message"] == "T2: merge conflict: CONFLICT (content) in db.py"
    assert by_kind["pass_error"]["newest_message"] == "TimeoutExpired: hermes kanban list timed out"
    assert by_kind["integrity_violation"]["newest_message"] == "primary checkout is dirty: M a.py"
    assert by_kind["fix_card_created"]["newest_message"] == 'T2: {"fix_card_id":"wfix"}'
    for unseen in ("usage_ingest_error", "merge_race_retrying", "fix_card_budget_exhausted", "model_mismatch",
                   "should_stop_error", "tamper_check_error", "tamper_blocked"):
        assert by_kind[unseen] == {"kind": unseen, "count": 0, "newest_at": None, "newest_message": None}
    assert health["read"] == 6 and health["window"] == 200
    assert [(e["ts"], e["kind"]) for e in health["recent"]] == [
        ("2026-09-19T11:55:00+00:00", "integrity_violation"), ("2026-09-19T11:50:00+00:00", "fix_card_created"),
        ("2026-09-19T11:40:00+00:00", "pass_error"), ("2026-09-19T11:30:00+00:00", "card_parked_for_budget"),
        ("2026-09-19T11:10:00+00:00", "merge_failed"), ("2026-09-19T10:50:00+00:00", "card_parked_for_budget"),
    ]   # cards_created, merged, merge_refused_unreviewed and the tamper event are not health events


ALL_HEALTH_KINDS = [
    "pass_error", "usage_ingest_error", "merge_failed", "merge_race_retrying", "card_parked_for_budget",
    "integrity_violation", "fix_card_created", "fix_card_budget_exhausted", "model_mismatch", "should_stop_error",
    "tamper_check_error", "tamper_blocked", "security_event",
]


def test_every_health_kind_is_read_and_nothing_else(conn, tmp_path, monkeypatch):
    _fake_hermes(monkeypatch, {})
    assert list(report.HEALTH_KINDS) == ALL_HEALTH_KINDS
    for number, kind in enumerate(ALL_HEALTH_KINDS):
        _event(conn, f"2026-09-19T10:{number:02d}:00+00:00", kind, {"n": number})
    for number, kind in enumerate(["merged", "cards_created", "usage_ingested", "gate1_recheck_failed",
                                   "swarm_stop", "pass_errors", "xpass_error", "question_read_failed"], start=20):
        _event(conn, f"2026-09-19T10:{number:02d}:00+00:00", kind, {"n": number})
    health = _build(conn, tmp_path)["health"]
    assert health["read"] == len(ALL_HEALTH_KINDS)
    assert [(k["kind"], k["count"]) for k in health["kinds"]] == [(kind, 1) for kind in ALL_HEALTH_KINDS]
    # "recent" is capped at _HEALTH_RECENT, so with more kinds than that it no longer holds all of them.
    assert [e["kind"] for e in health["recent"]] == list(reversed(ALL_HEALTH_KINDS))[:report._HEALTH_RECENT]


def test_the_four_round_5_health_kinds_report_did_not_read_yet_are_now_counted(conn, tmp_path, monkeypatch):
    """model_mismatch (usage.py, ASES-RTE-01), should_stop_error (mergeq._stop_requested), and tamper_check_error
    / tamper_blocked (review.py, ASES-QG-03) existed before round 6 but were missing from HEALTH_KINDS. Payload
    shapes match what each module actually records."""
    _fake_hermes(monkeypatch, {})
    _event(conn, "2026-09-19T10:00:00+00:00", "model_mismatch",
           {"session_id": "s1", "profile": "coder-1", "expected": "qwen/pinned", "actual": "qwen/other"})
    _event(conn, "2026-09-19T10:01:00+00:00", "should_stop_error",
           {"task_key": "T1", "step": "gate3", "error": "RuntimeError: database is locked"})
    _event(conn, "2026-09-19T10:02:00+00:00", "tamper_check_error",
           {"task_key": "T1", "card_id": "w1", "head": "a" * 40,
            "reason": "the tamper check could not run: git exited 128"})
    _event(conn, "2026-09-19T10:03:00+00:00", "tamper_blocked",
           {"task_key": "T1", "card_id": "w1", "head": "a" * 40, "detail": "test_x.py: assertion weakened"})

    health = _build(conn, tmp_path)["health"]
    by_kind = {k["kind"]: k for k in health["kinds"]}
    assert by_kind["model_mismatch"]["count"] == 1
    assert by_kind["should_stop_error"]["newest_message"] == "T1: RuntimeError: database is locked"
    assert by_kind["tamper_check_error"]["count"] == 1
    assert by_kind["tamper_blocked"]["newest_message"] == "T1: test_x.py: assertion weakened"


def test_tamper_events_are_counted_in_health_and_also_listed_in_quality(conn, tmp_path, monkeypatch):
    """Pins the round 6 decision (see the comments by HEALTH_KINDS and in _quality_panel): tamper_check_error and
    tamper_blocked are counted by the Health panel (they are in HEALTH_KINDS) AND still listed individually by the
    Quality panel's findings (its "contains tamper" catch-all is a deliberately general net, unchanged). A
    health-panel count and a quality-panel per-event listing are different things and both are kept on purpose;
    change this test on purpose if that decision is ever revisited."""
    _fake_hermes(monkeypatch, {})
    _event(conn, "2026-09-19T10:00:00+00:00", "tamper_blocked",
           {"task_key": "T1", "card_id": "w1", "head": "a" * 40, "detail": "test_x.py: assertion weakened"})

    rep = _build(conn, tmp_path)

    assert {k["kind"]: k["count"] for k in rep["health"]["kinds"]}["tamper_blocked"] == 1
    assert [f["kind"] for f in rep["quality"]["findings"]] == ["tamper_blocked"]


def test_health_says_provider_health_from_real_traffic_is_not_collected(scenario_report):
    note = scenario_report["health"]["note"]
    assert "not collected yet" in note and "429" in note
    assert note in report.render_text(scenario_report)
    assert "not collected yet" in report.render_html(scenario_report)


def test_health_reads_a_bounded_window_and_keeps_a_few_recent(conn, tmp_path, monkeypatch):
    _fake_hermes(monkeypatch, {})
    monkeypatch.setattr(report, "_HEALTH_WINDOW", 5)
    monkeypatch.setattr(report, "_HEALTH_RECENT", 3)
    for n in range(12):
        _event(conn, f"2026-09-19T10:{n:02d}:00+00:00", "merge_failed", {"task_key": "T1", "detail": f"failure {n}"})
    health = _build(conn, tmp_path)["health"]
    assert (health["window"], health["read"]) == (5, 5)
    assert health["kinds"][2]["count"] == 5 and health["kinds"][2]["newest_message"] == "T1: failure 11"
    assert [e["message"] for e in health["recent"]] == ["T1: failure 11", "T1: failure 10", "T1: failure 9"]
    assert "Counts are over the 5 newest health event(s) read (at most 5)." in report.render_text(
        _build(conn, tmp_path))


@pytest.mark.parametrize("payload, message", [
    ({"task_key": "T1", "detail": "merge conflict"}, "T1: merge conflict"),
    ({"detail": "merge conflict"}, "merge conflict"),
    ({"message": "m", "detail": "d", "error": "e", "reason": "r"}, "m"),
    ({"detail": "", "error": "e"}, "e"),
    ({"detail": None, "error": [], "reason": {}, "problems": ["a", "b"]}, "a; b"),
    ({"reason": "r", "problems": ["p"]}, "r"),
    ({"task_key": "T1"}, "T1"),
    ({"task_key": "T1", "fix_card_id": "wfix", "n": 2}, 'T1: {"fix_card_id":"wfix","n":2}'),
    ({"a": 1}, '{"a":1}'),
    ({}, ""),
    ({"detail": "line one\nline two\t\tend"}, "line one line two end"),
    ({"detail": "x" * 300}, "x" * 300),
    ({"detail": "x" * 301}, "x" * 297 + "..."),
    ({"detail": 0}, "0"),
])
def test_an_events_message(conn, tmp_path, monkeypatch, payload, message):
    _fake_hermes(monkeypatch, {})
    _event(conn, "2026-09-19T10:00:00+00:00", "merge_failed", payload)
    assert _build(conn, tmp_path)["health"]["recent"][0]["message"] == message


def test_rows_whose_payload_is_not_a_json_object_are_still_readable(conn, tmp_path, monkeypatch):
    _fake_hermes(monkeypatch, {})
    _raw_event(conn, "2026-09-19T10:00:00+00:00", "pass_error", "this is not json")
    _raw_event(conn, "2026-09-19T10:01:00+00:00", "pass_error", "[1, 2, 3]")
    _raw_event(conn, "2026-09-19T10:02:00+00:00", "pass_error", '"just text"')
    _raw_event(conn, "2026-09-19T10:03:00+00:00", "pass_error", "[" * 100000)   # deeper than the parser allows
    rep = _build(conn, tmp_path)
    assert [e["payload"] for e in rep["events"]][:3] == [
        {"text": "[" * 100000}, {"value": "just text"}, {"value": [1, 2, 3]}]
    assert rep["events"][3]["payload"] == {"text": "this is not json"}
    assert [e["message"] for e in rep["health"]["recent"]][1:] == [
        '{"value":"just text"}', '{"value":[1,2,3]}', '{"text":"this is not json"}']


# --- Events ---------------------------------------------------------------------------------------------------


def test_events_are_newest_first_by_time_then_by_id_and_limited(conn, tmp_path, monkeypatch):
    _fake_hermes(monkeypatch, {})
    _event(conn, "2026-09-19T10:00:00+00:00", "k", {"n": 1})
    _event(conn, "2026-09-19T12:00:00+00:00", "k", {"n": 2})      # inserted early, but the newest
    _event(conn, "2026-09-19T11:00:00+00:00", "k", {"n": 3})
    _event(conn, "2026-09-19T11:00:00+00:00", "k", {"n": 4})      # same second as n=3, written later
    rep = _build(conn, tmp_path)
    assert [e["payload"]["n"] for e in rep["events"]] == [2, 4, 3, 1]
    assert rep["events"][0] == {"ts": "2026-09-19T12:00:00+00:00", "kind": "k", "payload": {"n": 2}}
    assert [e["payload"]["n"] for e in _build(conn, tmp_path, event_limit=2)["events"]] == [2, 4]
    assert _build(conn, tmp_path, event_limit=0)["events"] == []
    assert _build(conn, tmp_path, event_limit=-3)["events"] == []


def test_the_default_is_the_last_forty_events(conn, tmp_path, monkeypatch):
    _fake_hermes(monkeypatch, {})
    for n in range(45):
        _event(conn, f"2026-09-19T10:{n:02d}:00+00:00", "k", {"n": n})
    rep = _build(conn, tmp_path)
    assert len(rep["events"]) == 40 and rep["events"][0]["payload"]["n"] == 44 and rep["events"][-1]["payload"]["n"] == 5


def test_event_payloads_are_redacted_where_they_are_read(conn, tmp_path, monkeypatch):
    """Rows written by hand (or by an older version) are not redacted in the table; the report must do it."""
    _fake_hermes(monkeypatch, {})
    _event(conn, "2026-09-19T10:00:00+00:00", "merge_failed",
           {"task_key": "T1", "detail": f"push used {SECRET}", "api_key": "hunter2-value", "nested": {"token": "t"}})
    payload = _build(conn, tmp_path)["events"][0]["payload"]
    assert payload == {"task_key": "T1", "detail": "push used [redacted]", "api_key": "[redacted]",
                       "nested": {"token": "[redacted]"}}


# --- Two projects sharing a database (events.py package, round 9; ASES-OBS-01) --------------------------------

OTHER_PLAN = plan_mod.parse_and_validate({
    "project": "p2",
    "integration_branch": "integration",
    "gate_profiles": {"trivial": ["echo ok"]},
    "tasks": [_task("T1", "scaffold", "coder", [], ["a.py"])],
}, known_roles=set(ROLES), max_cards=40)


def test_the_events_panel_is_per_project_not_per_database(conn, tmp_path, monkeypatch):
    """BEFORE this package, build_report's "events" panel had no project filter at all: p1's report showed p2's
    events too (see the round 9 before/after proof in the builder's report). Covers both a row attributed through
    the new project column (events.record(..., project=...)) and a legacy-shaped row attributed only through its
    payload, the way every row written before schema v7 is."""
    _fake_hermes(monkeypatch, {})
    events.record(conn, "final_gate_recorded", {"gate": "gate4", "result": "pass"}, project="p1")
    events.record(conn, "final_gate_recorded", {"gate": "gate4", "result": "pass"}, project="p2")
    _event(conn, "2026-09-19T10:00:00+00:00", "legacy_kind", {"project": "p1", "detail": "pre-v7 row, p1"})
    _event(conn, "2026-09-19T10:00:01+00:00", "legacy_kind", {"project": "p2", "detail": "pre-v7 row, p2"})
    _event(conn, "2026-09-19T10:00:02+00:00", "unattributed_kind", {"detail": "no project anywhere"})

    p1_events = _build(conn, tmp_path)["events"]
    p2_events = report.build_report(
        "b", OTHER_PLAN, _project(tmp_path, name="p2"), MODELS_CONFIG, conn, now=NOW,
    )["events"]
    p1_details = {e["payload"].get("detail") for e in p1_events}
    p2_details = {e["payload"].get("detail") for e in p2_events}

    assert sum(1 for e in p1_events if e["kind"] == "final_gate_recorded") == 1   # p1's own, via the column
    assert sum(1 for e in p2_events if e["kind"] == "final_gate_recorded") == 1   # p2's own, via the column
    assert "pre-v7 row, p1" in p1_details and "pre-v7 row, p1" not in p2_details  # legacy row, payload-only
    assert "pre-v7 row, p2" in p2_details and "pre-v7 row, p2" not in p1_details  # p1 never sees p2's legacy row
    assert "no project anywhere" in p1_details and "no project anywhere" in p2_details  # kept for every project


def test_the_quality_panel_findings_are_per_project_not_per_database(conn, tmp_path, monkeypatch):
    """The same leak, for _quality_panel's findings query (merge_refused_*/tamper_*/integrity_violation)."""
    _fake_hermes(monkeypatch, {})
    events.record(conn, "integrity_violation", {"problems": ["p1 problem"]}, project="p1")
    events.record(conn, "integrity_violation", {"problems": ["p2 problem"]}, project="p2")

    p1_findings = _build(conn, tmp_path)["quality"]["findings"]
    p2_findings = report.build_report(
        "b", OTHER_PLAN, _project(tmp_path, name="p2"), MODELS_CONFIG, conn, now=NOW,
    )["quality"]["findings"]

    assert any("p1 problem" in f["message"] for f in p1_findings)
    assert not any("p2 problem" in f["message"] for f in p1_findings)
    assert any("p2 problem" in f["message"] for f in p2_findings)
    assert not any("p1 problem" in f["message"] for f in p2_findings)


def test_the_health_panel_counts_only_this_projects_events_and_unattributed_ones(conn, tmp_path, monkeypatch):
    """Round 9 (architect, after EVENTSPROJ left it as a known gap): _health_panel used to count HEALTH_KINDS
    across the whole database, so p1's report showed p2's merge failures. Now p1 sees its own, the unattributed
    one, and never p2's."""
    _fake_hermes(monkeypatch, {})
    events.record(conn, "merge_failed", {"task_key": "T1", "detail": "p1 failure"}, project="p1")
    events.record(conn, "merge_failed", {"task_key": "T1", "detail": "p2 failure"}, project="p2")
    events.record(conn, "merge_failed", {"task_key": "T9", "detail": "no project anywhere"})

    p1 = report._health_panel(conn, "p1")
    p2 = report._health_panel(conn, "p2")

    count = lambda panel: next(k["count"] for k in panel["kinds"] if k["kind"] == "merge_failed")  # noqa: E731
    assert count(p1) == 2 and count(p2) == 2
    p1_messages = " ".join(r["message"] or "" for r in p1["recent"])
    assert "p1 failure" in p1_messages and "no project anywhere" in p1_messages
    assert "p2 failure" not in p1_messages


def test_a_parked_cards_reason_comes_from_its_own_projects_event_not_another_project_reusing_the_key(
    conn, tmp_path, monkeypatch,
):
    """Round 9 (architect, after EVENTSPROJ left it as a known gap): two projects reuse task key T1; p2's parking
    event is the NEWER one, which the old query (kind only) picked for p1's parked card."""
    _fake_hermes(monkeypatch, {"w1": {"id": "w1", "status": "scheduled", "title": "p1's T1"}})
    _event(conn, "2026-09-19T10:00:00+00:00", "card_parked_for_budget", {"project": "p1", "task_key": "T1",
                                                                        "reason": "p1 reason"})
    _event(conn, "2026-09-19T11:00:00+00:00", "card_parked_for_budget", {"project": "p2", "task_key": "T1",
                                                                        "reason": "p2 reason"})
    task_rows = {"T1": {"work_card_id": "w1", "merge_card_id": None}}

    parked, error = report._parked_cards("b", conn, task_rows, "p1")

    assert error is None
    assert [(p["task_key"], p["reason"]) for p in parked] == [("T1", "p1 reason")]


# --- Models ---------------------------------------------------------------------------------------------------


def test_models_are_listed_pinned_first(scenario_report):
    rows = scenario_report["models"]
    assert [(m["provider"], m["model"], m["pinned"]) for m in rows] == [
        ("openrouter", REVIEWER_MODEL, True), ("xkiro", CODER_MODEL, True),
        ("xkiro", "minimax/minimax-m3:free", False)]
    reviewer = rows[0]
    assert reviewer["role_class"] == "reviewer" and reviewer["context_length"] == 256000
    assert reviewer["context_ok"] is True and reviewer["tool_calling"] is True
    assert reviewer["smoke_test_result"] == "pass" and reviewer["smoke_test_at"]
    assert reviewer["data_policy"] is None
    candidate = rows[2]
    assert candidate["context_length"] is None and candidate["context_ok"] is False
    assert candidate["smoke_test_result"] is None and candidate["smoke_test_at"] is None
    assert candidate["tool_calling"] is None


def test_models_group_by_pinned_then_provider_then_model(conn, tmp_path, monkeypatch):
    _fake_hermes(monkeypatch, {})
    rows = [("z", "b", 0), ("a", "z", 0), ("z", "a", 1), ("a", "b", 1), ("a", "a", 0)]
    for provider, model, pinned in rows:
        conn.execute("INSERT INTO model_registry (provider, model, pinned) VALUES (?, ?, ?)",
                     (provider, model, pinned))
    listed = [(m["provider"], m["model"]) for m in _build(conn, tmp_path)["models"]]
    assert listed == [("a", "b"), ("z", "a"), ("a", "a"), ("a", "z"), ("z", "b")]


def test_a_context_below_the_floor_is_not_ok(conn, tmp_path, monkeypatch):
    _fake_hermes(monkeypatch, {})
    conn.execute("INSERT INTO model_registry (provider, model, context_length, pinned) VALUES ('p', 'small', 32000, 0)")
    assert _build(conn, tmp_path)["models"][0]["context_ok"] is False


# --- Secrets --------------------------------------------------------------------------------------------------


def _leaky_setup(conn, monkeypatch):
    _seed_tasks(conn)
    _event(conn, "2026-09-19T10:00:00+00:00", "merge_failed",
           {"task_key": "T1", "detail": f"push failed with {SECRET}", "api_key": "hunter2-value"})
    _event(conn, "2026-09-19T10:01:00+00:00", "card_parked_for_budget",
           {"task_key": "T3", "reason": f"budget and {SECRET}"})
    _event(conn, "2026-09-19T10:02:00+00:00", "integrity_violation", {"problems": [f"dirty {SECRET}"]})
    _raw_event(conn, "2026-09-19T10:03:00+00:00", "pass_error", f"raw text {SECRET}")
    _gate_run(conn, "T1", "gate1", SECRET, "pass", "2026-09-19T10:04:00+00:00")
    conn.execute("INSERT INTO review_verdicts (project, task_key, commit_sha, card_id, outcome, reviewer_profile, "
                 "recorded_at) VALUES ('p1', 'T1', 'abc', 'w1', 'PASS', ?, '2026-09-19T10:05:00+00:00')", (SECRET,))
    _fake_hermes(monkeypatch, {
        "w1": _card("w1", "blocked", title=f"leaks {SECRET}", events=[_blocked(f"which key, {SECRET}?")]),
        "w2": _card("w2", "ready"), "w3": _card("w3", "scheduled", title=f"also {SECRET}"),
        "m1": _card("m1", "blocked", assignee=None), "m2": _card("m2", "blocked", assignee=None),
        "m3": _card("m3", "blocked", assignee=None),
    }, failing=("w2",), error=RuntimeError(f"token {SECRET} was rejected"))


def _all_outputs(rep, tmp_path, *, dumped=True):
    """Everything the module can produce from `rep`. `dumped` adds json.dumps(rep), which is the caller's own
    dict and not something the module produced, so a test that edited `rep` by hand leaves it out."""
    page, data = report.write_report(rep, tmp_path / "out")
    outputs = {
        "status": report.render_status(rep), "text": report.render_text(rep), "html": report.render_html(rep),
        "file html": page.read_text(encoding="utf-8"), "file json": data.read_text(encoding="utf-8"),
    }
    if dumped:
        outputs["json"] = json.dumps(rep)
    return outputs


def test_secrets_never_reach_any_output(conn, tmp_path, monkeypatch):
    """An event payload holding a provider key shape, and a key named api_key, in the report data, the text, the
    status, the page and both files: in the events, the parked reasons, the questions, the card titles and the
    error text of a card Hermes could not show."""
    _leaky_setup(conn, monkeypatch)
    rep = _build(conn, tmp_path)
    for name, output in _all_outputs(rep, tmp_path).items():
        assert SECRET not in output, name
        assert "hunter2-value" not in output, name
        assert "abcdefghijklmnopqrstuvwx" not in output, name
    assert "[redacted]" in report.render_text(rep)
    assert '"api_key":"[redacted]"' in report.render_text(rep)   # the value is gone; the field name stays
    assert rep["cards"]["tasks"][0]["title"] == "leaks [redacted]"
    assert rep["cards"]["questions"][0]["question"] == "which key, [redacted]?"
    assert rep["budget"]["parked"][0]["reason"] == "budget and [redacted]"


def test_a_report_edited_by_hand_is_redacted_again_by_every_output(conn, tmp_path, monkeypatch):
    _fake_hermes(monkeypatch, {})
    rep = _build(conn, tmp_path)
    rep["events"] = [{"ts": "t", "kind": "k", "payload": {"detail": SECRET, "password": "hunter2-value"}}]
    rep["project"]["name"] = f"name {SECRET}"
    rep["health"]["recent"] = [{"ts": "t", "kind": "pass_error", "message": f"m {SECRET}"}]
    for name, output in _all_outputs(rep, tmp_path, dumped=False).items():
        assert SECRET not in output and "hunter2-value" not in output, name
    assert rep["events"][0]["payload"]["detail"] == SECRET   # the caller's own dict is left alone


# --- ASCII ----------------------------------------------------------------------------------------------------

HOSTILE = "caf\xe9 \U0001F600 \x1b[31mred\x1b[0m"


def _hostile_scenario(conn, monkeypatch):
    _seed_tasks(conn)
    _fake_hermes(monkeypatch, _quiet_board(
        w1=_card("w1", "done", title=HOSTILE), w3=_card("w3", "scheduled", title=HOSTILE),
        w2=_card("w2", "blocked", title="two\nlines", events=[_blocked(f"why {HOSTILE}?")]),
    ))
    _event(conn, "2026-09-19T10:00:00+00:00", "merge_failed",
           {"task_key": "T1", "detail": f"failed on {HOSTILE}", "\xe9": "\N{RIGHTWARDS ARROW}"})
    _event(conn, "2026-09-19T10:01:00+00:00", "card_parked_for_budget", {"task_key": "T3", "reason": HOSTILE})


def test_status_and_text_are_ascii_only(conn, tmp_path, monkeypatch):
    _hostile_scenario(conn, monkeypatch)
    rep = _build(conn, tmp_path, project=_project(tmp_path, name="\xe9cole \U0001F600"))
    for name, output in (("status", report.render_status(rep)), ("text", report.render_text(rep))):
        assert output.isascii(), name
        assert re.fullmatch(r"[\n\x20-\x7e]*", output), name   # and no control character but the line breaks
        assert "caf\\xe9 \\U0001f600 \\x1b[31mred\\x1b[0m" in output, name
    status = report.render_status(rep)
    assert "Project: \\xe9cole \\U0001f600 (plan p1)" in status
    assert "  T3 caf\\xe9 \\U0001f600 \\x1b[31mred\\x1b[0m: caf\\xe9" in status   # the parked card's title and reason
    assert "\\xe9" in report.render_text(rep) and "\\u2192" in report.render_text(rep)   # the payload's field and value


def test_a_newline_in_a_value_does_not_break_a_row(conn, tmp_path, monkeypatch):
    _hostile_scenario(conn, monkeypatch)
    rep = _build(conn, tmp_path)
    text = report.render_text(rep)
    row = next(line for line in text.splitlines() if line.startswith("T2 "))
    assert "two\\nlines" in row and "blocked" in row   # still one line, with the whole row on it
    assert not any(line.startswith("lines") for line in text.splitlines())


def test_ascii_escapes_everything_that_is_not_plain_text():
    assert report._ascii("plain text 123 ~!") == "plain text 123 ~!"
    assert report._ascii("\xe9") == "\\xe9"
    assert report._ascii("\N{RIGHTWARDS ARROW}") == "\\u2192"
    assert report._ascii("\U0001F600") == "\\U0001f600"
    assert report._ascii("\x00\x1b\x7f") == "\\x00\\x1b\\x7f"
    assert report._ascii("\x85\x9f") == "\\x85\\x9f"          # C1 controls
    assert report._ascii("\ud83d") == "\\ud83d"               # a lone surrogate
    assert report._ascii("a\nb\rc\td") == "a\\nb\\rc\\td"
    assert report._ascii(42) == "42" and report._ascii(None) == "None"
    once = report._ascii(HOSTILE + "\n\x00")
    assert report._ascii(once) == once                        # escaped text is already safe


def test_long_cells_are_cut_in_the_terminal_report_but_not_in_the_data_or_the_page(conn, tmp_path, monkeypatch):
    _fake_hermes(monkeypatch, {})
    long_detail = "word " * 100
    _event(conn, "2026-09-19T10:00:00+00:00", "merge_failed", {"task_key": "T1", "detail": long_detail})
    rep = _build(conn, tmp_path)
    assert long_detail.strip() in json.dumps(rep["events"][0]["payload"])
    text = report.render_text(rep)
    assert long_detail not in text
    assert not any(len(line) > 400 for line in text.splitlines())
    events_row = next(
        line for line in text.splitlines() if line.startswith("2026-09-19T10:00:00") and '{"detail":' in line)
    assert events_row.endswith("...") and len(events_row) < 200
    assert long_detail.strip() in report.render_html(rep)
    assert len(rep["health"]["recent"][0]["message"]) == 300   # the message is one line, cut at 300


# --- HTML -----------------------------------------------------------------------------------------------------


class _Structure(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags, self.attrs = [], []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs += attrs


def _structure(page):
    parser = _Structure()
    parser.feed(page)
    return parser


def _hostile_html_scenario(conn, monkeypatch):
    _seed_tasks(conn)
    _fake_hermes(monkeypatch, _quiet_board(
        w1=_card("w1", "ready", title="<script>alert(1)</script> & co"),
        w2=_card("w2", "blocked", title='x" onmouseover="alert(2)', events=[_blocked("<img src=x onerror=alert(3)>?")]),
    ))
    _event(conn, "2026-09-19T10:00:00+00:00", "merge_failed",
           {"task_key": "T1", "detail": "</td></tr></table><script>alert(4)</script>"})


def test_html_escapes_a_script_title_ampersands_and_quotes(conn, tmp_path, monkeypatch):
    _hostile_html_scenario(conn, monkeypatch)
    page = report.render_html(_build(conn, tmp_path))
    assert "<script" not in page.lower()
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; co" in page
    assert "x&quot; onmouseover=&quot;alert(2)" in page
    assert "&lt;img src=x onerror=alert(3)&gt;?" in page                 # the question
    assert "&lt;/td&gt;&lt;/tr&gt;&lt;/table&gt;&lt;script&gt;alert(4)&lt;/script&gt;" in page   # the event
    assert "<img" not in page


def test_html_uses_only_inert_tags_and_attributes(conn, tmp_path, monkeypatch):
    _hostile_html_scenario(conn, monkeypatch)
    structure = _structure(report.render_html(_build(conn, tmp_path)))
    assert set(structure.tags) <= {
        "html", "head", "meta", "title", "style", "body", "p", "h1", "h2", "h3", "section", "div", "table",
        "thead", "tbody", "tr", "th", "td", "dl", "dt", "dd"}
    names = {name for name, _value in structure.attrs}
    assert names <= {"lang", "charset", "http-equiv", "content", "name", "class", "id"}
    assert not any(str(value or "").lower().startswith("javascript:") for _name, value in structure.attrs)


def test_html_is_self_contained_with_a_banner_and_a_policy(scenario_report):
    page = report.render_html(scenario_report)
    for forbidden in ("<script", "<link", "<img", "<iframe", "<object", "<embed", "<form", "src=", "href=",
                      "@import", "url(", "javascript:"):
        assert forbidden not in page.lower(), forbidden
    assert page.startswith("<!DOCTYPE html>\n") and page.endswith("</html>\n")
    assert '<meta charset="utf-8">' in page
    assert "default-src 'none'" in page and "style-src 'unsafe-inline'" in page
    assert ("Local report: generated 2026-09-19T12:00:00+00:00, not served, contains source-code paths and card "
            "text, keep it on this machine") in page
    assert page.count('<p class="banner">') == 1 and page.index('class="banner"') < page.index("<h1>")


def test_html_shows_every_panel_and_its_content(scenario_report):
    page = report.render_html(scenario_report)
    for panel in ("project", "budget", "cards", "quality", "health", "events", "models"):
        assert f'<section id="{panel}">' in page
    assert page.count("<section") == 7 and page.count("</section>") == 7
    assert "<h1>ASES project report: ases</h1>" in page and "<title>ASES project report</title>" in page
    assert '<html lang="en">' in page and 'name="viewport"' in page
    assert "<dt>Board</dt><dd>b</dd>" in page and "<dt>Status</dt><dd>running</dd>" in page      # facts
    assert "<h3>Bounds</h3>" in page and "<th>Bound</th><th>Used</th><th>Limit</th>" in page      # a table
    assert "<tr><td>cards in plan</td><td>3</td><td>40</td></tr>" in page
    assert "<tr><td>infra failures T1</td><td>3</td><td>not set</td></tr>" in page
    empty = json.loads(json.dumps(scenario_report))
    empty["events"], empty["models"] = [], []
    assert '<p class="note">Model registry (pinned first): none in the registry' in report.render_html(empty)   # a note
    for expected in ("title of w2", "which database should the app use?", REVIEWER_MODEL,
                     "merge_refused_unreviewed", "openrouter", "dddddddddd", "http://127.0.0.1:9119"):
        assert expected in page, expected


def test_html_survives_a_lone_surrogate_and_control_characters(conn, tmp_path, monkeypatch):
    _seed_tasks(conn)
    _fake_hermes(monkeypatch, _quiet_board(w1=_card("w1", "ready", title="half \ud83d pair \x00 nul \xe9")))
    rep = _build(conn, tmp_path)
    page = report.render_html(rep)
    assert "half \\ud83d pair \\x00 nul \xe9" in page       # escaped where it cannot be written, kept where it can
    page.encode("utf-8")
    written_page, written_json = report.write_report(rep, tmp_path / "out")
    assert "half \\ud83d pair" in written_page.read_text(encoding="utf-8")
    assert json.loads(written_json.read_text(encoding="utf-8")) == rep


# --- render_status --------------------------------------------------------------------------------------------


def test_status_for_the_seeded_scenario(scenario_report):
    lines = report.render_status(scenario_report).splitlines()
    assert lines[:9] == [
        "ASES status, generated 2026-09-19T12:00:00+00:00 (UTC)",
        "Project: ases (plan p1), board b, branch integration, data class public, status running",
        "Bounds: cards in plan 3/40, re-plans 1/2, wall clock minutes 120/240, fix cards T1 1/2, "
        "review rounds T1 2/3, capability failures T1 1/3, infra failures T1 3 (no limit set), "
        "fix cards T2 2/2, review rounds T3 3/3",
        "Budget: openrouter 37/50 used (13 left, reserve 5)",
        "Budget: xkiro 12 used (no known cap)",
        "Parked: 1 card(s) waiting for budget",
        "  T3 title of w3: review budget on openrouter: needs 20, only 13 usable today",
        "Cards: 1 done, 1 scheduled, 1 blocked (1 question); merge queue 1/3 done",
        "Quality: last gate run __final__ gate4 pass at 2026-09-19T11:45:00+00:00 (commit dddddddddd); "
        "3 recent finding(s) (refusals, integrity, tamper)",
    ]
    assert lines[9:] == [
        "Health (last 5):",
        "  2026-09-19T11:55:00+00:00 integrity_violation: primary checkout is dirty: M a.py",
        "  2026-09-19T11:50:00+00:00 fix_card_created: T2: {\"fix_card_id\":\"wfix\"}",
        "  2026-09-19T11:40:00+00:00 pass_error: TimeoutExpired: hermes kanban list timed out",
        "  2026-09-19T11:30:00+00:00 card_parked_for_budget: T3: review budget on openrouter: needs 20, only 13 "
        "usable today",
        "  2026-09-19T11:10:00+00:00 merge_failed: T2: merge conflict: CONFLICT (content) in db.py",
    ]   # the sixth health event is not shown: the status keeps the last five


def test_status_shows_a_pauses_reason_in_parentheses_after_the_status_word(conn, tmp_path, monkeypatch):
    """ASES-CTL-01: the register's known gap was that a pause's reason was dropped, so swarm status never showed
    why a project was paused. It is now part of project_state and appears right on the status line."""
    _fake_hermes(monkeypatch, {})
    _state(conn, status="paused", stop_reason="project_wall_clock_minutes reached: 240 of 240")
    line = next(l for l in report.render_status(_build(conn, tmp_path)).splitlines() if l.startswith("Project:"))
    assert line.endswith("status paused (project_wall_clock_minutes reached: 240 of 240)")


def test_status_shows_a_stops_reason_the_same_way_a_pauses_is_shown(conn, tmp_path, monkeypatch):
    _fake_hermes(monkeypatch, {})
    _state(conn, status="stopped", stop_reason="swarm stop")
    line = next(l for l in report.render_status(_build(conn, tmp_path)).splitlines() if l.startswith("Project:"))
    assert line.endswith("status stopped (swarm stop)")


def test_status_shows_no_parenthetical_when_there_is_no_reason(conn, tmp_path, monkeypatch):
    _fake_hermes(monkeypatch, {})
    _state(conn, status="running")
    line = next(l for l in report.render_status(_build(conn, tmp_path)).splitlines() if l.startswith("Project:"))
    assert line.endswith("status running")


def test_status_clips_a_long_pause_reason_at_150_characters(conn, tmp_path, monkeypatch):
    _fake_hermes(monkeypatch, {})
    _state(conn, status="paused", stop_reason="x" * 400)
    line = next(l for l in report.render_status(_build(conn, tmp_path)).splitlines() if l.startswith("Project:"))
    assert line.endswith("status paused (" + "x" * 147 + "...)")


def test_status_shows_project_bounds_always_and_task_bounds_only_once_used(conn, tmp_path, monkeypatch):
    _fake_hermes(monkeypatch, {})
    bounds_line = next(l for l in report.render_status(_build(conn, tmp_path)).splitlines() if l.startswith("Bounds:"))
    assert bounds_line == "Bounds: cards in plan 3/40, re-plans 0/2, wall clock minutes not set"


def test_status_without_a_finished_clock_shows_used_time_with_no_limit(conn, tmp_path, monkeypatch):
    _fake_hermes(monkeypatch, {})
    _state(conn, started_at="2026-09-19T10:00:00+00:00")
    line = next(l for l in report.render_status(_build(conn, tmp_path)).splitlines() if l.startswith("Bounds:"))
    assert "wall clock minutes 120 (no limit set)" in line


def test_status_is_one_screen_and_has_no_trailing_newline(scenario_report):
    status = report.render_status(scenario_report)
    assert len(status.splitlines()) <= 25 and not status.endswith("\n")
    assert not report.render_text(scenario_report).endswith("\n")


def test_status_keeps_the_last_five_health_events_only(conn, tmp_path, monkeypatch):
    _fake_hermes(monkeypatch, {})
    for n in range(8):
        _event(conn, f"2026-09-19T10:{n:02d}:00+00:00", "pass_error", {"error": f"e{n}"})
    lines = report.render_status(_build(conn, tmp_path)).splitlines()
    at = lines.index("Health (last 5):")
    assert lines[at + 1:] == [f"  2026-09-19T10:{n:02d}:00+00:00 pass_error: e{n}" for n in (7, 6, 5, 4, 3)]


@pytest.mark.parametrize("length, shown", [(150, "x" * 150), (151, "x" * 147 + "..."), (400, "x" * 147 + "...")])
def test_status_cuts_a_long_reason_or_message_at_150_characters(conn, tmp_path, monkeypatch, length, shown):
    _seed_tasks(conn)
    _event(conn, "2026-09-19T10:00:00+00:00", "card_parked_for_budget", {"task_key": "T3", "reason": "x" * length})
    _event(conn, "2026-09-19T10:01:00+00:00", "merge_failed", {"detail": "x" * length})
    _fake_hermes(monkeypatch, _quiet_board(w3=_card("w3", "scheduled")))
    lines = report.render_status(_build(conn, tmp_path)).splitlines()
    assert f"  T3 title of w3: {shown}" in lines
    assert f"  2026-09-19T10:01:00+00:00 merge_failed: {shown}" in lines


def test_a_newline_in_a_status_value_does_not_add_a_line(conn, tmp_path, monkeypatch):
    _seed_tasks(conn)
    _event(conn, "2026-09-19T10:00:00+00:00", "card_parked_for_budget", {"task_key": "T3", "reason": "why\nnot"})
    _fake_hermes(monkeypatch, _quiet_board(w3=_card("w3", "scheduled", title="two\nlines")))
    lines = report.render_status(_build(conn, tmp_path)).splitlines()
    assert "  T3 two\\nlines: why\\nnot" in lines
    # heading, project, bounds, two budgets, parked and its card, cards, quality, health and its one event
    assert len(lines) == 11


@pytest.mark.parametrize("length, shown", [(120, "t" * 120), (121, "t" * 117 + "...")])
def test_a_table_cell_is_cut_at_120_characters_in_the_terminal_only(conn, tmp_path, monkeypatch, length, shown):
    _seed_tasks(conn)
    _fake_hermes(monkeypatch, _quiet_board(w1=_card("w1", "ready", title="t" * length)))
    rep = _build(conn, tmp_path)
    row = next(line for line in report.render_text(rep).splitlines() if line.startswith("T1 "))
    assert re.match(rf"T1\s+{re.escape(shown)}\s+coder\s", row)
    assert "t" * length in report.render_html(rep)
    assert rep["cards"]["tasks"][0]["title"] == "t" * length


def test_status_with_no_findings_or_a_commit(conn, tmp_path, monkeypatch):
    _fake_hermes(monkeypatch, {})
    _gate_run(conn, "T1", "gate1", None, "fail", "2026-09-19T10:00:00+00:00")
    quality = next(l for l in report.render_status(_build(conn, tmp_path)).splitlines() if l.startswith("Quality:"))
    assert quality == "Quality: last gate run T1 gate1 fail at 2026-09-19T10:00:00+00:00"


def test_status_reads_a_project_with_no_recorded_state(conn, tmp_path, monkeypatch):
    _fake_hermes(monkeypatch, {})
    assert "status not recorded" in report.render_status(_build(conn, tmp_path))
    assert "Status              not recorded" in report.render_text(_build(conn, tmp_path))


# --- render_text ----------------------------------------------------------------------------------------------


def test_text_report_has_a_heading_for_every_panel_in_order(scenario_report):
    text = report.render_text(scenario_report)
    lines = text.splitlines()
    assert lines[:3] == ["ASES project report", "Generated 2026-09-19T12:00:00+00:00 (UTC)", ""]
    headings = [i for i, line in enumerate(lines) if re.fullmatch(r"== [A-Z][a-z]+ ==", line)]
    assert [lines[i] for i in headings] == [
        "== Project ==", "== Budget ==", "== Cards ==", "== Quality ==", "== Health ==", "== Events ==",
        "== Models ==",
    ]
    assert all(lines[i + 1] == "" for i in headings)
    assert lines[-1] != ""


def test_text_report_shows_every_panels_content(scenario_report):
    text = report.render_text(scenario_report)
    for expected in (
        "wall clock minutes", "capability failures T1", "UTC day", "Next reset", "2026-09-20T00:00:00+00:00",
        "10 percent of each capped provider's limit", "Parked cards (waiting for budget)",
        "review budget on openrouter", "Requests today by model and role", REVIEWER_MODEL, "90000", "1500",
        "Hermes dashboard", "http://127.0.0.1:9119", "Work cards", "1 done, 1 scheduled, 1 blocked (1 question)",
        "1/3 merge cards done", "Plan tasks", "title of w1",
        "Open questions (swarm questions lists them, swarm answer replies)", "which database should the app use?",
        "Gate runs (newest first)", "gate4", "Review verdicts (newest first)", "CHANGES_REQUIRED", "Merge records",
        "Findings (merge refusals, integrity, tamper; newest first)", "gate_tamper_suspected",
        "Health events by kind", "Recent health events", "Newest events first, secrets redacted",
        "Model registry (pinned first)", "Context ok",
    ):
        assert expected in text, expected
    for pattern in (
        r"^Integration branch\s+integration$", r"^Data class\s+public$", r"^Started\s+2026-09-19T10:00:00\+00:00$",
        r"^Stop reason\s+-$", r"^wall clock minutes\s+120\s+240$", r"^infra failures T1\s+3\s+not set$",
        r"^fix cards T2\s+2\s+2$",
        r"^T1\s+b{10}\s+c{10}\s+pass\s+no\s+2026-09-19T10:31:00\+00:00$",                 # a merge record
        r"^2026-09-19T10:35:00\+00:00\s+T1\s+b{10}\s+PASS\s+reviewer\s+w1\s+yes$",        # a verdict, tamper flagged
        r"^2026-09-19T11:05:00\+00:00\s+T2\s+e{10}\s+CHANGES_REQUIRED\s+reviewer\s+w2\s+no$",
        r"^2026-09-19T11:55:00\+00:00\s+integrity_violation\s+-\s+primary checkout is dirty: M a\.py$",   # a finding
    ):
        assert re.search(pattern, text, re.MULTILINE), pattern


def _table_lines(text, heading):
    lines = text.splitlines()
    at = lines.index(heading)
    rows = []
    for line in lines[at + 3:]:
        if not line:
            break
        rows.append(line)
    return lines[at + 1], lines[at + 2], rows


def test_text_tables_are_aligned_and_sized_to_their_content(scenario_report):
    text = report.render_text(scenario_report)
    for heading in ("Bounds", "Plan tasks", "Requests today per provider", "Gate runs (newest first)",
                    "Health events by kind", "Model registry (pinned first)"):
        header, separator, rows = _table_lines(text, heading)
        assert re.fullmatch(r"-+(  -+)*", separator), heading
        spans, position = [], 0
        for segment in separator.split("  "):
            spans.append((position, position + len(segment)))
            position += len(segment) + 2
        assert rows, heading
        for start, end in spans:
            widest = max(len(line[start:end].rstrip()) for line in [header, *rows])
            assert widest == end - start, (heading, start, end)          # no column is wider than its content
            for line in [header, *rows]:
                assert line[end:end + 2].strip() == "", (heading, line)   # and no cell runs into the next column
        assert all(line == line.rstrip() for line in [header, separator, *rows])


def test_facts_are_aligned_in_two_columns(scenario_report):
    lines = report.render_text(scenario_report).splitlines()
    at = lines.index("== Project ==") + 2
    facts = lines[at:at + 9]
    assert [line[:20] for line in facts] == [
        "Name                ", "Plan project        ", "Board               ", "Integration branch  ",
        "Data class          ", "Status              ", "Started             ", "Deadline            ",
        "Stop reason         "]
    assert facts[0] == "Name                ases"


def test_text_report_lists_the_cards_hermes_could_not_show(scenario, tmp_path, monkeypatch):
    conn, project, _calls = scenario
    _fake_hermes(monkeypatch, {"w1": _card("w1", "done"), "m1": _card("m1", "done"), "w2": _card("w2", "done"),
                               "m2": _card("m2", "done"), "w3": _card("w3", "done")}, failing=("m3",))
    text = report.render_text(_build(conn, tmp_path, project=project))
    _header, _separator, rows = _table_lines(text, "Cards Hermes could not show")
    assert len(rows) == 1 and re.match(r"m3\s+T3\s+merge\s+HermesCommandError: ", rows[0])


# --- write_report ---------------------------------------------------------------------------------------------


def test_write_report_creates_the_directory_and_both_files(scenario_report, tmp_path):
    target = tmp_path / "reports" / "nested" / "2026-09-19"
    assert not target.exists()
    page, data = report.write_report(scenario_report, target)
    assert (page, data) == (target / "report.html", target / "report.json")
    assert page.is_file() and data.is_file()
    assert page.read_text(encoding="utf-8") == report.render_html(scenario_report)
    assert json.loads(data.read_text(encoding="utf-8")) == scenario_report   # the JSON round-trips
    assert data.read_text(encoding="utf-8").startswith('{\n  "generated_at": "2026-09-19T12:00:00+00:00",\n')
    assert sorted(p.name for p in target.iterdir()) == ["report.html", "report.json"]


def test_write_report_overwrites_and_accepts_a_string_path(scenario_report, tmp_path):
    report.write_report(scenario_report, tmp_path / "out")
    changed = json.loads(json.dumps(scenario_report))
    changed["project"]["name"] = "renamed"
    page, data = report.write_report(changed, str(tmp_path / "out"))
    assert "<h1>ASES project report: renamed</h1>" in page.read_text(encoding="utf-8")
    assert json.loads(data.read_text(encoding="utf-8"))["project"]["name"] == "renamed"


def test_written_files_use_unix_newlines_and_the_json_is_pure_ascii(conn, tmp_path, monkeypatch):
    _seed_tasks(conn)
    _fake_hermes(monkeypatch, _quiet_board(w1=_card("w1", "ready", title="caf\xe9 \U0001F600 \ud83d")))
    rep = _build(conn, tmp_path)
    page, data = report.write_report(rep, tmp_path / "out")
    assert b"\r" not in page.read_bytes() and b"\r" not in data.read_bytes()
    assert data.read_bytes().isascii()
    assert json.loads(data.read_bytes().decode("utf-8")) == rep
    assert "caf\xe9 \U0001F600".encode("utf-8") in page.read_bytes()   # the page is UTF-8, real characters kept
    assert data.read_text(encoding="utf-8").endswith("}\n")
