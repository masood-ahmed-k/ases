"""usage.py: the session-as-unit ledger ingest (round 19, package LEDGER; ASES-CAP-02, ASES-CAP-03, ASES-RTE-01).

The window path, the list fallback, orphan/ambiguity handling and settling. hermes.kanban_show,
hermes.session_usage, hermes.kanban_sessions and hermes.kanban_session_ids are all faked (monkeypatch), never a
real `hermes` process, and the database is a temp sqlite file.

The real-numbers fixtures (S1_RUNS, S1_SESSIONS, LEGACY_SESSIONS) are the six real S1 runs of card t_4ae270eb and
the four real legacy usage_ingested rows from r19 LEDGER.md's own validated read of board ases-phase3 (FINDINGS 4
and 8, TESTS TO PROVE IT section 3 and 4), used per architect decision as this package's unit-test fixtures."""
import json
from datetime import datetime, timezone

import pytest

from ases import config, db, hermes, ledger, plan as plan_mod, usage

BOARD = "b"
CARD = "t_4ae270eb"
REVIEWER_MODEL = "cohere/north-mini-code:free"
CODER_MODEL = "qwen/qwen3-coder-plus:free"
ROLES = {"coder": "coder-1", "reviewer": "reviewer"}
MODELS = {
    "providers": {
        "xkiro": {"limits": {}},
        "openrouter": {"limits": {"per_day_default": 50, "per_day_after_credits": 1000},
                       "credits_purchased": False},
    },
    "models": [
        {"provider": "xkiro", "model": CODER_MODEL, "role_class": "coder", "pinned": True},
        {"provider": "openrouter", "model": REVIEWER_MODEL, "role_class": "reviewer", "pinned": True},
    ],
}


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "ases.db")


def _project(tmp_path):
    return config.ProjectConfig(
        name="ases", environment="native", data_class="public", workspace_root=tmp_path / "ws",
        ases_home=tmp_path / "home", board=BOARD, integration_branch="integration", roles=ROLES,
        concurrency={}, budgets={}, hermes_tested_version="0.21.3", hermes_native_home=tmp_path / "hermes",
    )


def _dt(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc)


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


def _run(run_id, started_at, ended_at, *, profile="reviewer"):
    """One entry of a card's runs list, as hermes.kanban_show returns it under "_runs": no worker_session_id, as
    every real changes_requested/crashed/blocked run of card t_4ae270eb is (r19 LEDGER.md FINDINGS 1)."""
    return {"id": run_id, "profile": profile, "status": "done", "outcome": "completed", "summary": "", "error": None,
            "metadata": None, "started_at": started_at, "ended_at": ended_at, "worker_pid": None}


def _card_with_runs(card_id, runs):
    return {
        "id": card_id, "status": "review", "_runs": runs,
        "_events": [{"kind": "spawned", "run_id": r["id"], "payload": {}, "created_at": 0} for r in runs],
    }


def _session(session_id, started_at, ended_at, calls, card_id, *, parent_session_id=None):
    """One line as hermes.kanban_sessions returns it (the `_summary` shape)."""
    return {
        "id": session_id, "started_at": started_at, "ended_at": ended_at, "last_activity_at": ended_at,
        "model": REVIEWER_MODEL, "billing_provider": "openrouter", "api_call_count": calls, "input_tokens": 1000,
        "output_tokens": 100, "parent_session_id": parent_session_id,
        "first_prompt": f"work kanban task {card_id}",
    }


# The six real S1 runs of card t_4ae270eb (r19 LEDGER.md FINDINGS 4, TESTS TO PROVE IT section 3): none carries a
# worker_session_id (changes_requested, crashed and blocked runs never stamp one).
S1_RUNS = [
    (33, 1790561406, 1790561441),
    (35, 1790561467, 1790561528),
    (36, 1790561528, 1790561588),
    (37, 1790561588, 1790561649),
    (39, 1790644586, 1790644608),
    (40, 1790649588, 1790649615),
]
S1_SESSIONS = [
    ("20260928_041007_a30546", 1790561409.933, 1790561501.111, 15),
    ("20260928_041108_3c8fea", 1790561471.795, 1790561500.814, 3),
    ("20260928_041209_4ac8e0", 1790561532.134, 1790561541.739, 0),
    ("20260928_041311_21476a", 1790561598.003, 1790561608.107, 0),
    ("20260929_031628_9fe6f5", 1790644591.997, 1790644612.121, 5),
    ("20260929_043950_0ed95f", 1790649593.936, 1790649620.328, 4),
]
# The four real legacy usage_ingested rows (FINDINGS 8): requests_now is what the ledger already showed before
# this pass, requests_grown is what a fresh export reports now.
LEGACY_SESSIONS = {
    "2234bb": {"profile": "coder-1", "requests_now": 9, "requests_grown": 14, "ended_at": None,
               "last_activity_at": 1790561500.0},                        # still open: reaped, but well on 09-28
    "6a50ee": {"profile": "coder-1", "requests_now": 10, "requests_grown": 10, "ended_at": None,
               "last_activity_at": 1790561450.0},
    "303335": {"profile": "reviewer", "requests_now": 13, "requests_grown": 13, "ended_at": 1790561417.837,
               "last_activity_at": 1790561417.837},
    "2a0f7f": {"profile": "reviewer", "requests_now": 17, "requests_grown": 22, "ended_at": 1790561470.385,
               "last_activity_at": 1790561470.385},
}


def _s1_card():
    return _card_with_runs(CARD, [_run(rid, start, end) for rid, start, end in S1_RUNS])


def _s1_kanban_sessions(profile, started_after, started_before=None, timeout=120):
    return [_session(sid, start, end, calls, CARD) for sid, start, end, calls in S1_SESSIONS]


# ---------------------------------------------------------------------------------------------
# The real S1 replay: the window path alone, on real numbers.
# ---------------------------------------------------------------------------------------------


def test_the_six_real_s1_runs_each_map_to_exactly_one_session_at_the_real_hermes_counts(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(hermes, "kanban_show", lambda board, card_id: _s1_card())
    monkeypatch.setattr(hermes, "kanban_sessions", _s1_kanban_sessions)
    monkeypatch.setattr(hermes, "kanban_session_ids", lambda *a, **k: [])

    ingested = usage.ingest_card_usage(BOARD, CARD, _project(tmp_path), MODELS, conn=conn, now=1790649625.0)

    assert set(ingested) == {sid for sid, *_ in S1_SESSIONS}
    rows = {
        r["session_id"]: (r["run_id"], r["requests"], r["mapped_by"], r["settled"])
        for r in conn.execute("SELECT session_id, run_id, requests, mapped_by, settled FROM usage_ingested")
    }
    assert rows == {
        "20260928_041007_a30546": (33, 15, "window", 1),
        "20260928_041108_3c8fea": (35, 3, "window", 1),
        "20260928_041209_4ac8e0": (36, 0, "window", 1),
        "20260928_041311_21476a": (37, 0, "window", 1),
        "20260929_031628_9fe6f5": (39, 5, "window", 1),
        "20260929_043950_0ed95f": (40, 4, "window", 1),
    }
    # every run closes: each has exactly one counted session and it is settled (window-mapped sessions have
    # already ended)
    states = {r["run_id"]: r["state"] for r in conn.execute("SELECT run_id, state FROM usage_runs")}
    assert states == {rid: "closed" for rid, *_ in S1_RUNS}
    assert ledger.usage_today_for_provider(conn, "openrouter", now=_dt(1790561406)) == 18   # 15+3+0+0, 09-28
    assert ledger.usage_today_for_provider(conn, "openrouter", now=_dt(1790644591)) == 9    # 5+4, 09-29
    # a clean 1:1 mapping: no conflict, ambiguity, orphan or missing-session event
    kinds = {r["kind"] for r in conn.execute("SELECT DISTINCT kind FROM events")}
    assert kinds == {"usage_ingested"}


def test_invariant_ledger_increments_equal_counted_requests(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(hermes, "kanban_show", lambda board, card_id: _s1_card())
    monkeypatch.setattr(hermes, "kanban_sessions", _s1_kanban_sessions)
    monkeypatch.setattr(hermes, "kanban_session_ids", lambda *a, **k: [])

    usage.ingest_card_usage(BOARD, CARD, _project(tmp_path), MODELS, conn=conn, now=1790649625.0)

    ledger_total = conn.execute("SELECT COALESCE(SUM(count), 0) AS n FROM requests_ledger").fetchone()["n"]
    counted_total = conn.execute("SELECT COALESCE(SUM(requests), 0) AS n FROM usage_ingested").fetchone()["n"]
    assert ledger_total == counted_total == 27   # 15+3+0+0+5+4


def test_replay_card_t_4ae270eb_with_legacy_rows_reaches_the_real_hermes_counts(conn, tmp_path, monkeypatch):
    """The architect's own reconciliation numbers (FINDINGS 8, PROPOSED DESIGN section E): seeded with the four
    real legacy rows (openrouter 09-28 = 30, xkiro 09-28 = 19), one controller-order pass (ingest_run_usage then
    settle_open_sessions) with `now` on 2026-09-29 reaches openrouter 09-28 = 53, openrouter 09-29 = 9, xkiro
    09-28 = 24."""
    conn.execute(
        "INSERT INTO plan_tasks (project, task_key, work_card_id, merge_card_id, role, created_at) "
        "VALUES ('p1', 'S1', ?, 'm_S1', 'reviewer', datetime('now'))", (CARD,),
    )
    for session_id, data in LEGACY_SESSIONS.items():
        provider = "openrouter" if data["profile"] == "reviewer" else "xkiro"
        model = REVIEWER_MODEL if data["profile"] == "reviewer" else CODER_MODEL
        conn.execute(
            "INSERT INTO usage_ingested (session_id, profile, provider, model, requests, input_tokens, "
            "output_tokens, ingested_at, project, task_key, card_id) VALUES (?, ?, ?, ?, ?, 1000, 100, "
            "'2026-09-28T02:10:25', 'p1', 'S1', ?)",
            (session_id, data["profile"], provider, model, data["requests_now"], CARD),
        )
        # The real ases.db already had these counted into the ledger before this migration; a bare usage_ingested
        # row here would not, on its own, so the pre-state is seeded the same way _count_session would have.
        ledger.record_usage(conn, provider, model, data["requests_now"], now=_dt(1790561425))
    assert ledger.usage_today_for_provider(conn, "openrouter", now=_dt(1790561425)) == 30
    assert ledger.usage_today_for_provider(conn, "xkiro", now=_dt(1790561425)) == 19

    def legacy_session_usage(profile, session_id, timeout=60):
        data = LEGACY_SESSIONS.get(session_id)
        if data is None:
            return None
        provider = "openrouter" if data["profile"] == "reviewer" else "xkiro"
        return {
            "id": session_id, "model": REVIEWER_MODEL if provider == "openrouter" else CODER_MODEL,
            "api_call_count": data["requests_grown"], "input_tokens": 1000, "output_tokens": 100,
            "started_at": None, "ended_at": data["ended_at"], "last_activity_at": data["last_activity_at"],
            "billing_provider": provider, "parent_session_id": None, "first_prompt": None,
        }

    monkeypatch.setattr(hermes, "kanban_show", lambda board, card_id: _s1_card())
    monkeypatch.setattr(hermes, "kanban_sessions", _s1_kanban_sessions)
    monkeypatch.setattr(hermes, "kanban_session_ids", lambda *a, **k: [])
    monkeypatch.setattr(hermes, "session_usage", legacy_session_usage)

    plan = plan_mod.parse_and_validate({
        "project": "p1", "integration_branch": "integration", "gate_profiles": {"trivial": ["echo ok"]},
        "tasks": [{"key": "S1", "title": "s1", "role": "reviewer", "depends_on": [], "touches": [],
                   "acceptance": ["reviewed"], "gate_profile": "trivial", "estimated_requests": 5}],
    }, known_roles=set(ROLES), max_cards=40)
    project = _project(tmp_path)
    now = 1790650000.0   # 2026-09-29, well past every S1 run's window and more than an hour past 2234bb/6a50ee

    usage.ingest_run_usage(BOARD, plan, project, MODELS, conn=conn, now=now)
    usage.settle_open_sessions(project, MODELS, conn=conn, now=now)

    assert ledger.usage_today_for_provider(conn, "openrouter", now=_dt(1790561425)) == 53
    assert ledger.usage_today_for_provider(conn, "openrouter", now=_dt(1790644591)) == 9
    assert ledger.usage_today_for_provider(conn, "xkiro", now=_dt(1790561425)) == 24


# ---------------------------------------------------------------------------------------------
# Orphan handling: a session already counted for another card, and one this card's own runs cannot explain.
# ---------------------------------------------------------------------------------------------


def test_a_session_already_counted_for_another_card_is_not_counted_again_and_one_conflict_event_is_recorded(
    conn, tmp_path, monkeypatch,
):
    conn.execute(
        "INSERT INTO usage_ingested (session_id, profile, provider, model, requests, input_tokens, output_tokens, "
        "ingested_at, project, task_key, card_id, board, run_id, mapped_by, settled) VALUES "
        "('S_X', 'reviewer', 'openrouter', ?, 7, 100, 10, '2026-09-28T02:10:25', 'p1', 'OTHER', 't_aaaa0001', 'b', 999, "
        "'window', 1)", (REVIEWER_MODEL,),
    )
    card_b = _card_with_runs("t_bbbb0002", [_run(200, 1790561406, 1790561441)])
    monkeypatch.setattr(hermes, "kanban_show", lambda board, card_id: card_b)
    monkeypatch.setattr(hermes, "kanban_sessions", lambda *a, **k: [
        _session("S_X", 1790561409, 1790561420, 3, "t_bbbb0002"),
    ])
    monkeypatch.setattr(hermes, "kanban_session_ids", lambda *a, **k: [])

    ingested = usage.ingest_card_usage(BOARD, "t_bbbb0002", _project(tmp_path), MODELS, conn=conn, now=1790561450.0)

    assert ingested == []
    assert conn.execute("SELECT card_id, requests FROM usage_ingested WHERE session_id = 'S_X'").fetchone()[:] == (
        "t_aaaa0001", 7)   # untouched: still t_aaaa0001's, still 7
    payload = json.loads(conn.execute(
        "SELECT payload FROM events WHERE kind = 'usage_session_conflict'").fetchone()["payload"])
    assert payload == {"session_id": "S_X", "counted_card": "t_aaaa0001", "claiming_card": "t_bbbb0002", "run_id": 999}

    usage.ingest_card_usage(BOARD, "t_bbbb0002", _project(tmp_path), MODELS, conn=conn, now=1790561450.0)
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE kind = 'usage_session_conflict'").fetchone()["n"] == 1


def test_a_prompt_match_before_any_run_is_not_counted_and_is_reported_once(conn, tmp_path, monkeypatch):
    card = _card_with_runs("t_aaaa0001", [_run(400, 1790561500, 1790561600)])   # its only run starts AFTER the session
    monkeypatch.setattr(hermes, "kanban_show", lambda board, card_id: card)
    monkeypatch.setattr(hermes, "kanban_sessions", lambda *a, **k: [
        _session("S_EARLY", 1790561410, 1790561420, 5, "t_aaaa0001"),
    ])
    monkeypatch.setattr(hermes, "kanban_session_ids", lambda *a, **k: [])

    ingested = usage.ingest_card_usage(BOARD, "t_aaaa0001", _project(tmp_path), MODELS, conn=conn, now=1790561650.0)

    assert ingested == []
    assert _count(conn, "usage_ingested") == 0
    payload = json.loads(conn.execute(
        "SELECT payload FROM events WHERE kind = 'usage_session_unattributed'").fetchone()["payload"])
    assert payload == {"session_id": "S_EARLY", "card_id": "t_aaaa0001", "profile": "reviewer", "started_at": 1790561410}

    usage.ingest_card_usage(BOARD, "t_aaaa0001", _project(tmp_path), MODELS, conn=conn, now=1790561650.0)
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE kind = 'usage_session_unattributed'").fetchone()["n"] == 1


def test_two_sessions_matching_one_run_are_each_counted_once_with_one_ambiguity_event(conn, tmp_path, monkeypatch):
    card = _card_with_runs("t_aaaa0001", [_run(300, 1790561406, 1790561500)])
    monkeypatch.setattr(hermes, "kanban_show", lambda board, card_id: card)
    monkeypatch.setattr(hermes, "kanban_sessions", lambda *a, **k: [
        _session("S_ONE", 1790561410, 1790561420, 5, "t_aaaa0001"),
        _session("S_TWO", 1790561430, 1790561440, 2, "t_aaaa0001"),
    ])
    monkeypatch.setattr(hermes, "kanban_session_ids", lambda *a, **k: [])

    ingested = usage.ingest_card_usage(BOARD, "t_aaaa0001", _project(tmp_path), MODELS, conn=conn, now=1790561450.0)

    assert set(ingested) == {"S_ONE", "S_TWO"}   # each real, distinct worker is still counted
    payload = json.loads(conn.execute(
        "SELECT payload FROM events WHERE kind = 'usage_mapping_ambiguous'").fetchone()["payload"])
    assert payload["run_id"] == 300 and set(payload["session_ids"]) == {"S_ONE", "S_TWO"} and payload["requests"] == 7


def test_a_second_session_found_by_the_metadata_path_on_a_later_pass_still_records_the_ambiguity(
        conn, tmp_path, monkeypatch):
    """Round 19 review finding (the reviewer's own reproduction, turned into a test): pass 1, the run's own
    metadata session A cannot be exported yet while the window path counts a genuinely different session B for
    the same run; pass 2, A exports and the metadata path counts it too. The run now has two counted non-lineage
    sessions, so usage_mapping_ambiguous must be recorded whichever path found the second one."""
    run = dict(_run(1, 1000, 1100), metadata={"worker_session_id": "SESSION_A"})
    card = _card_with_runs("t_aaaa0001", [run])
    monkeypatch.setattr(hermes, "kanban_show", lambda board, card_id: card)
    monkeypatch.setattr(hermes, "kanban_sessions", lambda *a, **k: [_session("SESSION_B", 1050, 1090, 4, "t_aaaa0001")])
    monkeypatch.setattr(hermes, "kanban_session_ids", lambda *a, **k: [])
    monkeypatch.setattr(hermes, "session_usage", lambda profile, session_id, timeout=60: None)   # A fails, pass 1

    usage.ingest_card_usage(BOARD, "t_aaaa0001", _project(tmp_path), MODELS, conn=conn, now=1200.0)
    assert conn.execute("SELECT COUNT(*) AS n FROM events WHERE kind = 'usage_mapping_ambiguous'").fetchone()["n"] == 0

    monkeypatch.setattr(hermes, "session_usage", lambda profile, session_id, timeout=60: (
        _session("SESSION_A", 1001, 1099, 6, "t_aaaa0001") if session_id == "SESSION_A" else None))
    usage.ingest_card_usage(BOARD, "t_aaaa0001", _project(tmp_path), MODELS, conn=conn, now=1201.0)

    rows = conn.execute("SELECT session_id, run_id FROM usage_ingested ORDER BY session_id").fetchall()
    assert [(r["session_id"], r["run_id"]) for r in rows] == [("SESSION_A", 1), ("SESSION_B", 1)]
    payload = json.loads(conn.execute(
        "SELECT payload FROM events WHERE kind = 'usage_mapping_ambiguous'").fetchone()["payload"])
    assert payload["run_id"] == 1 and set(payload["session_ids"]) == {"SESSION_A", "SESSION_B"}


# ---------------------------------------------------------------------------------------------
# Lineage: a compression child is counted under its parent's run, never orphaned.
# ---------------------------------------------------------------------------------------------


def test_a_compression_child_is_counted_under_its_parents_run(conn, tmp_path, monkeypatch):
    """r19 LEDGER.md TESTS TO PROVE IT section 4, PROPOSED DESIGN section C.3: a compression child's first
    message is the compression summary, never "work kanban task <card>" (agent/conversation_compression.py
    rebinds HERMES_SESSION_ID to the child), so it never matches KANBAN_PROMPT directly. It is still kept and
    counted under its PARENT's run, with mapped_by='lineage', when its parent_session_id names a session
    already attributed to this card. A late-starting child also gets late_start: true on its own usage_ingested
    event, computed against the parent run's own run_ended_at (round 19 fix round 2, reviewer major: this path
    was implemented but had zero test coverage anywhere in the diff, and its late_start was only ever computed
    for the non-lineage branch, so a genuinely late-starting compression child never got flagged)."""
    card = _card_with_runs("t_aaaa0001", [_run(900, 1790561406, 1790561441)])
    monkeypatch.setattr(hermes, "kanban_show", lambda board, card_id: card)
    child = _session("S_CHILD", 1790561900, 1790561950, 3, "t_aaaa0001", parent_session_id="S_PARENT")
    child["first_prompt"] = "the conversation so far has been compressed; continuing prior work"
    monkeypatch.setattr(hermes, "kanban_sessions", lambda *a, **k: [
        _session("S_PARENT", 1790561410, 1790561420, 5, "t_aaaa0001"),   # counted first: the child's parent
        child,
    ])
    monkeypatch.setattr(hermes, "kanban_session_ids", lambda *a, **k: [])

    ingested = usage.ingest_card_usage(BOARD, "t_aaaa0001", _project(tmp_path), MODELS, conn=conn, now=1790561960.0)

    assert set(ingested) == {"S_PARENT", "S_CHILD"}
    row = conn.execute("SELECT run_id, mapped_by FROM usage_ingested WHERE session_id = 'S_CHILD'").fetchone()
    assert (row["run_id"], row["mapped_by"]) == (900, "lineage")   # counted under the PARENT's run, not orphaned
    payload = json.loads(conn.execute(
        "SELECT payload FROM events WHERE kind = 'usage_ingested' AND "
        "json_extract(payload, '$.session_id') = 'S_CHILD'"
    ).fetchone()["payload"])
    assert payload["late_start"] is True   # S_CHILD starts at 1790561900, long past run 900's run_ended_at 1790561441
    # a lineage session is never itself flagged ambiguous alongside the parent it shares a run with
    assert _count(conn, "events") == 2   # exactly the two usage_ingested events: no ambiguity, orphan or conflict


# ---------------------------------------------------------------------------------------------
# The list fallback and the missing-session declaration.
# ---------------------------------------------------------------------------------------------


def test_a_spawned_run_without_a_session_waits_then_records_one_missing_event(conn, tmp_path, monkeypatch):
    card = _card_with_runs("t_aaaa0001", [_run(500, 1790561406, 1790561441)])
    monkeypatch.setattr(hermes, "kanban_show", lambda board, card_id: card)
    monkeypatch.setattr(hermes, "kanban_sessions", lambda *a, **k: [])          # nothing in the window
    monkeypatch.setattr(hermes, "kanban_session_ids", lambda *a, **k: [])       # nothing in the list fallback

    before = usage.ingest_card_usage(BOARD, "t_aaaa0001", _project(tmp_path), MODELS, conn=conn, now=1790561441 + 100.0)

    assert before == []
    assert _count(conn, "events") == 0   # MISSING_AFTER (900s) has not passed: no event yet, no state change
    assert conn.execute("SELECT state FROM usage_runs WHERE run_id = 500").fetchone()["state"] == "open"

    after = usage.ingest_card_usage(BOARD, "t_aaaa0001", _project(tmp_path), MODELS, conn=conn, now=1790561441 + 901.0)

    assert after == []
    assert conn.execute("SELECT state FROM usage_runs WHERE run_id = 500").fetchone()["state"] == "no_session"
    payload = json.loads(conn.execute(
        "SELECT payload FROM events WHERE kind = 'usage_session_missing'").fetchone()["payload"])
    assert payload == {
        "board": BOARD, "card_id": "t_aaaa0001", "run_id": 500, "profile": "reviewer",
        "run_started_at": 1790561406, "run_ended_at": 1790561441,
    }

    usage.ingest_card_usage(BOARD, "t_aaaa0001", _project(tmp_path), MODELS, conn=conn, now=1790561441 + 2000.0)
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE kind = 'usage_session_missing'").fetchone()["n"] == 1


def test_a_kanban_session_ids_failure_is_retried_and_never_marks_the_run_no_session(conn, tmp_path, monkeypatch):
    """A hermes.kanban_session_ids failure (None: hermes missing, timeout, OSError or non-zero exit) is unknown,
    not a genuine empty answer, and must never close the run out as 'no_session' (round 19 fix round 1, reviewer
    blocker: usage.py's list fallback used to conflate the two, permanently losing any real session a run had)."""
    card = _card_with_runs("t_aaaa0001", [_run(700, 1790561406, 1790561441)])
    monkeypatch.setattr(hermes, "kanban_show", lambda board, card_id: card)
    monkeypatch.setattr(hermes, "kanban_sessions", lambda *a, **k: [])          # nothing in the window
    monkeypatch.setattr(hermes, "kanban_session_ids", lambda *a, **k: None)     # the hermes call itself failed

    ingested = usage.ingest_card_usage(
        BOARD, "t_aaaa0001", _project(tmp_path), MODELS, conn=conn, now=1790561441 + 901.0)

    assert ingested == []
    assert _count(conn, "events") == 0   # unknown is never reported as usage_session_missing
    assert conn.execute("SELECT state FROM usage_runs WHERE run_id = 700").fetchone()["state"] == "open"

    # Once hermes actually answers (even with a genuine empty list), the run is still correctly closed out.
    monkeypatch.setattr(hermes, "kanban_session_ids", lambda *a, **k: [])
    usage.ingest_card_usage(BOARD, "t_aaaa0001", _project(tmp_path), MODELS, conn=conn, now=1790561441 + 902.0)

    assert conn.execute("SELECT state FROM usage_runs WHERE run_id = 700").fetchone()["state"] == "no_session"
    assert _count(conn, "events") == 1
    assert conn.execute("SELECT kind FROM events").fetchone()["kind"] == "usage_session_missing"


def test_a_transient_session_usage_failure_on_one_candidate_never_marks_the_run_no_session(conn, tmp_path, monkeypatch):
    """A hermes.session_usage failure (None) on ONE candidate id inside the list fallback is unknown, not a
    genuine "this id is not a match", and must never let the loop exhaust every candidate id and declare the
    run 'no_session' (round 19 fix round 2, reviewer major: this module's own contract, that None from any
    Hermes call means "unknown, retry later" and never "not found", was applied to the outer
    hermes.kanban_session_ids() call in fix round 1 but not to this per-candidate session_usage() call one
    level deeper, so the run's real session could be permanently lost the one time its own lookup hiccuped)."""
    card = _card_with_runs("t_aaaa0001", [_run(800, 1790561406, 1790561441)])
    monkeypatch.setattr(hermes, "kanban_show", lambda board, card_id: card)
    monkeypatch.setattr(hermes, "kanban_sessions", lambda *a, **k: [])   # the ended-only export never sees it
    monkeypatch.setattr(hermes, "kanban_session_ids", lambda profile, limit=100, timeout=60: ["20260928_041007_a30546"])
    monkeypatch.setattr(hermes, "session_usage", lambda profile, session_id, timeout=60: None)   # transient failure

    ingested = usage.ingest_card_usage(BOARD, "t_aaaa0001", _project(tmp_path), MODELS, conn=conn, now=1790561441 + 901.0)

    assert ingested == []
    assert _count(conn, "events") == 0   # never reported as usage_session_missing while a candidate is uncertain
    assert conn.execute("SELECT state FROM usage_runs WHERE run_id = 800").fetchone()["state"] == "open"

    # Once hermes actually answers for that same id, the run's real session is found and counted, exactly as it
    # would have been had the first pass never hit the transient failure.
    monkeypatch.setattr(hermes, "session_usage", lambda profile, session_id, timeout=60: {
        "id": session_id, "model": REVIEWER_MODEL, "api_call_count": 15, "input_tokens": 100, "output_tokens": 10,
        "started_at": 1790561409.0, "ended_at": None, "last_activity_at": 1790561430.0,
        "billing_provider": "openrouter", "parent_session_id": None, "first_prompt": "work kanban task t_aaaa0001",
    })
    ingested = usage.ingest_card_usage(BOARD, "t_aaaa0001", _project(tmp_path), MODELS, conn=conn, now=1790561441 + 902.0)

    assert ingested == ["20260928_041007_a30546"]
    assert conn.execute(
        "SELECT mapped_by FROM usage_ingested WHERE session_id = '20260928_041007_a30546'"
    ).fetchone()["mapped_by"] == "list"
    assert conn.execute("SELECT state FROM usage_runs WHERE run_id = 800").fetchone()["state"] == "open"


def test_an_open_session_of_a_killed_worker_is_found_through_the_list_fallback(conn, tmp_path, monkeypatch):
    card = _card_with_runs("t_aaaa0001", [_run(600, 1790561406, 1790561441)])
    monkeypatch.setattr(hermes, "kanban_show", lambda board, card_id: card)
    monkeypatch.setattr(hermes, "kanban_sessions", lambda *a, **k: [])   # the ended-only export never sees it
    monkeypatch.setattr(hermes, "kanban_session_ids", lambda profile, limit=100, timeout=60: ["20260928_041007_a30546"])
    monkeypatch.setattr(hermes, "session_usage", lambda profile, session_id, timeout=60: {
        "id": session_id, "model": REVIEWER_MODEL, "api_call_count": 14, "input_tokens": 100, "output_tokens": 10,
        "started_at": 1790561409.0, "ended_at": None, "last_activity_at": 1790561430.0,
        "billing_provider": "openrouter", "parent_session_id": None, "first_prompt": "work kanban task t_aaaa0001",
    })

    ingested = usage.ingest_card_usage(BOARD, "t_aaaa0001", _project(tmp_path), MODELS, conn=conn, now=1790561441 + 901.0)

    assert ingested == ["20260928_041007_a30546"]
    row = conn.execute(
        "SELECT requests, mapped_by, settled FROM usage_ingested WHERE session_id = '20260928_041007_a30546'"
    ).fetchone()
    assert (row["requests"], row["mapped_by"], row["settled"]) == (14, "list", 0)   # settled=0: a killed worker's
                                                                                     # session is never assumed final
    assert conn.execute("SELECT state FROM usage_runs WHERE run_id = 600").fetchone()["state"] == "open"


# ---------------------------------------------------------------------------------------------
# Settling: monotone top-ups, never a decrease, charged to the day the session ran.
# ---------------------------------------------------------------------------------------------


def _seed_open_row(
    conn, session_id, requests, run_id=1, run_started_at=1790561406, run_ended_at=1790561441, *,
    profile="reviewer", provider="openrouter", model=REVIEWER_MODEL,
):
    conn.execute(
        "INSERT INTO usage_ingested (session_id, profile, provider, model, requests, input_tokens, output_tokens, "
        "ingested_at, project, task_key, card_id, board, run_id, mapped_by, settled) VALUES "
        "(?, ?, ?, ?, ?, 100, 10, '2026-09-28T02:10:25', 'p1', 'T1', 't_aaaa0001', 'b', ?, 'window', 0)",
        (session_id, profile, provider, model, requests, run_id),
    )
    conn.execute(
        "INSERT INTO usage_runs (board, run_id, card_id, profile, project, task_key, run_started_at, "
        "run_ended_at, state, updated_at) VALUES ('b', ?, 't_aaaa0001', ?, 'p1', 'T1', ?, ?, 'open', "
        "datetime('now'))", (run_id, profile, run_started_at, run_ended_at),
    )


def test_a_count_that_goes_down_never_decreases_the_ledger_and_records_one_event(conn, tmp_path, monkeypatch):
    _seed_open_row(conn, "S1", 10)
    ledger.record_usage(conn, "openrouter", REVIEWER_MODEL, 10, now=_dt(1790561425))
    monkeypatch.setattr(hermes, "session_usage", lambda profile, session_id, timeout=60: {
        "id": "S1", "model": REVIEWER_MODEL, "api_call_count": 6, "input_tokens": 100, "output_tokens": 10,
        "started_at": None, "ended_at": None, "last_activity_at": None, "billing_provider": "openrouter",
        "parent_session_id": None, "first_prompt": None,
    })

    usage.settle_open_sessions(_project(tmp_path), MODELS, conn=conn, now=1790561441 + 10.0)

    assert ledger.usage_today_for_provider(conn, "openrouter", now=_dt(1790561425)) == 10   # unchanged
    assert conn.execute("SELECT requests FROM usage_ingested WHERE session_id = 'S1'").fetchone()["requests"] == 10
    payload = json.loads(conn.execute(
        "SELECT payload FROM events WHERE kind = 'usage_count_regressed'").fetchone()["payload"])
    assert payload == {"session_id": "S1", "run_id": 1, "was": 10, "now": 6}


def test_a_still_running_session_is_counted_now_and_topped_up_once_it_ends(conn, tmp_path, monkeypatch):
    """2a0f7f's real shape: reported at 17 while open, then 22 once it ended -- the ledger gains exactly the +5
    difference, once, and the row settles."""
    _seed_open_row(conn, "2a0f7f", 17, run_started_at=1790561344, run_ended_at=1790561396)
    ledger.record_usage(conn, "openrouter", REVIEWER_MODEL, 17, now=_dt(1790561344))
    monkeypatch.setattr(hermes, "session_usage", lambda profile, session_id, timeout=60: {
        "id": "2a0f7f", "model": REVIEWER_MODEL, "api_call_count": 22, "input_tokens": 200, "output_tokens": 20,
        "started_at": 1790561344.337, "ended_at": 1790561470.385, "last_activity_at": 1790561470.385,
        "billing_provider": "openrouter", "parent_session_id": None, "first_prompt": None,
    })

    settled = usage.settle_open_sessions(_project(tmp_path), MODELS, conn=conn, now=1790650000.0)

    assert settled == ["2a0f7f"]
    assert ledger.usage_today_for_provider(conn, "openrouter", now=_dt(1790561470)) == 22   # 17 + 5, one top-up
    row = conn.execute("SELECT requests, settled FROM usage_ingested WHERE session_id = '2a0f7f'").fetchone()
    assert (row["requests"], row["settled"]) == (22, 1)
    assert conn.execute("SELECT state FROM usage_runs WHERE run_id = 1").fetchone()["state"] == "closed"


def test_top_ups_are_charged_to_the_day_the_session_ran_not_the_day_it_is_settled(conn, tmp_path, monkeypatch):
    _seed_open_row(conn, "2a0f7f", 17, run_started_at=1790561344, run_ended_at=1790561396)
    monkeypatch.setattr(hermes, "session_usage", lambda profile, session_id, timeout=60: {
        "id": "2a0f7f", "model": REVIEWER_MODEL, "api_call_count": 22, "input_tokens": 200, "output_tokens": 20,
        "started_at": 1790561344.337, "ended_at": 1790561470.385, "last_activity_at": 1790561470.385,
        "billing_provider": "openrouter", "parent_session_id": None, "first_prompt": None,
    })

    usage.settle_open_sessions(_project(tmp_path), MODELS, conn=conn, now=1790650000.0)   # settled a day later

    assert ledger.usage_today_for_provider(conn, "openrouter", now=_dt(1790561470)) == 5    # the session's own day
    assert ledger.usage_today_for_provider(conn, "openrouter", now=_dt(1790650000)) == 0    # never the settle day


def test_a_session_that_never_ends_settles_after_a_stable_count_past_the_deadline(conn, tmp_path, monkeypatch):
    run_ended_at = 1790561230
    _seed_open_row(conn, "2234bb", 9, run_started_at=1790561200, run_ended_at=run_ended_at,
                   profile="coder-1", provider="xkiro", model=CODER_MODEL)
    ledger.record_usage(conn, "xkiro", CODER_MODEL, 9, now=_dt(1790561200))
    monkeypatch.setattr(hermes, "session_usage", lambda profile, session_id, timeout=60: {
        "id": "2234bb", "model": CODER_MODEL, "api_call_count": 14, "input_tokens": 100, "output_tokens": 10,
        "started_at": 1790561200.914, "ended_at": None, "last_activity_at": 1790561500.0,
        "billing_provider": "xkiro", "parent_session_id": None, "first_prompt": None,
    })
    project = _project(tmp_path)

    # Pass 1 (past SETTLE_AFTER already, its very first check): tops up 9 -> 14, but a first check is never
    # "stable" (nothing to compare it against yet), so it does not settle.
    pass_1 = run_ended_at + usage.SETTLE_AFTER + 10.0
    usage.settle_open_sessions(project, MODELS, conn=conn, now=pass_1)
    row = conn.execute("SELECT requests, settled FROM usage_ingested WHERE session_id='2234bb'").fetchone()
    assert (row["requests"], row["settled"]) == (14, 0)

    # Pass 2: same count, still past SETTLE_AFTER, but this check is less than 120s after pass 1's: not stable yet.
    pass_2 = pass_1 + 50.0
    usage.settle_open_sessions(project, MODELS, conn=conn, now=pass_2)
    assert conn.execute("SELECT settled FROM usage_ingested WHERE session_id='2234bb'").fetchone()["settled"] == 0

    # Pass 3: same count, and this check is over 120s after pass 2's own: stable, so it settles.
    pass_3 = pass_2 + 130.0
    usage.settle_open_sessions(project, MODELS, conn=conn, now=pass_3)
    assert conn.execute("SELECT settled FROM usage_ingested WHERE session_id='2234bb'").fetchone()["settled"] == 1
    assert ledger.usage_today_for_provider(conn, "xkiro", now=_dt(1790561300)) == 14   # the one top-up, never repeated
