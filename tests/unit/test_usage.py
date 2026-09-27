"""usage.py: real request usage into the request ledger, and the review reserve (ASES-CAP-03).

hermes.kanban_show and hermes.session_usage are faked and the database is a temp sqlite file, so nothing here
touches a real board, a real session or a provider."""
import json
import sqlite3

import pytest

from ases import config, db, events, hermes, ledger, plan as plan_mod, policy, usage

ROLES = {"lead": "lead", "coder": "coder-1", "reviewer": "reviewer"}

CODER_MODEL = "qwen/qwen3-coder-plus:free"
REVIEWER_MODEL = "cohere/north-mini-code:free"

# Shaped like config/models.yaml: the coder is on a provider with no published daily cap, the reviewer is on
# OpenRouter's 50 requests a day, and the lead has a candidate row that is not pinned.
MODELS = {
    "providers": {
        "xkiro": {"limits": {}},
        "openrouter": {"limits": {"per_day_default": 50, "per_day_after_credits": 1000},
                       "credits_purchased": False},
    },
    "models": [
        {"provider": "xkiro", "model": "qwen/qwen3.8-max:free", "role_class": "lead", "pinned": False},
        {"provider": "xkiro", "model": CODER_MODEL, "role_class": "coder", "pinned": True},
        {"provider": "xkiro", "model": "minimax/minimax-m3:free", "role_class": "coder_candidate", "pinned": True},
        {"provider": "openrouter", "model": REVIEWER_MODEL, "role_class": "reviewer", "pinned": True},
    ],
}

# What config/swarm.yaml sets: hold back 10% of a day's cap, and 20 requests for a review pass.
BUDGETS = {"daily_reserve_percent": 10, "review_reserve_requests": 20}

PLAN = plan_mod.parse_and_validate({
    "project": "p1",
    "integration_branch": "integration",
    "gate_profiles": {"trivial": ["echo ok"]},
    "tasks": [
        {"key": "T1", "title": "scaffold", "role": "coder", "depends_on": [], "touches": ["a.py"],
         "acceptance": ["exists"], "gate_profile": "trivial", "estimated_requests": 10},
        {"key": "T2", "title": "review scaffold", "role": "reviewer", "depends_on": ["T1"], "touches": [],
         "acceptance": ["reviewed"], "gate_profile": "trivial", "estimated_requests": 5},
    ],
}, known_roles=set(ROLES), max_cards=40)

_DEFAULT = object()


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "ases.db")


def _project(tmp_path, budgets=None):
    return config.ProjectConfig(
        name="ases", environment="native", data_class="public", workspace_root=tmp_path / "ws",
        ases_home=tmp_path / "home", board="b", integration_branch="integration", roles=ROLES,
        concurrency={}, budgets=BUDGETS if budgets is None else budgets, hermes_tested_version="0.21.3",
        hermes_native_home=tmp_path / "hermes",
    )


def _seed_task(conn, key, work_card_id, project="p1"):
    conn.execute(
        "INSERT INTO plan_tasks (project, task_key, work_card_id, merge_card_id, role, created_at) "
        "VALUES (?, ?, ?, ?, 'coder', datetime('now'))",
        (project, key, work_card_id, f"m_{key}"),
    )


def _run(session, profile="coder-1", *, ended_at=1789832491, metadata=_DEFAULT):
    """One entry of a card's runs list, as hermes.kanban_show returns it under "_runs"."""
    if metadata is _DEFAULT:
        metadata = {"worker_session_id": session}
    return {"id": 1, "profile": profile, "status": "done", "outcome": "completed", "summary": "", "error": None,
            "metadata": metadata, "started_at": "1789832318", "ended_at": ended_at, "worker_pid": 4242}


def _fake_cards(monkeypatch, cards):
    """hermes.kanban_show over a dict of card id -> runs. Returns the list of card ids it was asked for."""
    shown = []

    def fake_show(board, card_id):
        shown.append(card_id)
        return {"id": card_id, "status": "done", "_runs": cards[card_id]}

    monkeypatch.setattr(hermes, "kanban_show", fake_show)
    return shown


def _export(session_id, model, requests, input_tokens=1000, output_tokens=100):
    return {"id": session_id, "model": model, "api_call_count": requests,
            "input_tokens": input_tokens, "output_tokens": output_tokens}


def _fake_exports(monkeypatch, exports):
    """hermes.session_usage over a dict of session id -> what it returns (None is a failed export). The dict
    is read at call time, so a test can change an entry between two calls. Returns the list of
    (profile, session_id) it was asked for."""
    asked = []

    def fake_usage(profile, session_id, timeout=60):
        asked.append((profile, session_id))
        return exports.get(session_id)

    monkeypatch.setattr(hermes, "session_usage", fake_usage)
    return asked


def _ingest(conn, tmp_path):
    return usage.ingest_run_usage("b", PLAN, _project(tmp_path), MODELS, conn=conn)


def _rows(conn):
    cur = conn.execute(
        "SELECT session_id, profile, provider, model, requests, input_tokens, output_tokens "
        "FROM usage_ingested ORDER BY session_id"
    )
    return [dict(r) for r in cur.fetchall()]


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


# ---------------------------------------------------------------------------------------------
# provider_for_profile
# ---------------------------------------------------------------------------------------------


def test_provider_for_profile_maps_a_profile_back_to_its_roles_pinned_provider():
    assert usage.provider_for_profile("coder-1", ROLES, MODELS) == policy.ProfileProvider("xkiro", CODER_MODEL)
    assert usage.provider_for_profile("reviewer", ROLES, MODELS) == policy.ProfileProvider("openrouter", REVIEWER_MODEL)


def test_provider_for_profile_is_none_for_a_profile_no_role_maps_to():
    assert usage.provider_for_profile("someone-else", ROLES, MODELS) is None


def test_provider_for_profile_is_none_when_the_role_has_no_pinned_provider():
    # "lead" is a role, but its only model row is not pinned. A "planner" role has no model row at all.
    assert usage.provider_for_profile("lead", ROLES, MODELS) is None
    assert usage.provider_for_profile("planner-1", {**ROLES, "planner": "planner-1"}, MODELS) is None


@pytest.mark.parametrize("profile", [None, ""])
def test_provider_for_profile_is_none_for_no_profile_even_if_a_role_has_none_set(profile):
    # A run the controller made itself has no profile. It must not match a role whose profile is also unset.
    assert usage.provider_for_profile(profile, {"coder": None, "reviewer": ""}, MODELS) is None
    assert usage.provider_for_profile(profile, ROLES, MODELS) is None


def test_provider_for_profile_keeps_looking_when_several_roles_share_a_profile():
    roles = {"lead": "shared", "coder": "shared"}   # lead has no pinned provider, coder has
    assert usage.provider_for_profile("shared", roles, MODELS) == policy.ProfileProvider("xkiro", CODER_MODEL)


# ---------------------------------------------------------------------------------------------
# ingest_run_usage
# ---------------------------------------------------------------------------------------------


def test_ingest_counts_a_finished_coder_run_and_a_finished_reviewer_run_under_their_own_providers(
    conn, tmp_path, monkeypatch,
):
    _seed_task(conn, "T1", "w1")
    # ended_at arrives as a string for one run and an int for the other: both mean "ended".
    _fake_cards(monkeypatch, {"w1": [
        _run("S_CODER", "coder-1", ended_at="1789832491"), _run("S_REVIEW", "reviewer", ended_at=1789832491),
    ]})
    _fake_exports(monkeypatch, {
        "S_CODER": _export("S_CODER", CODER_MODEL, 12, 5000, 400),
        "S_REVIEW": _export("S_REVIEW", REVIEWER_MODEL, 37, 933438, 13726),
    })

    ingested = _ingest(conn, tmp_path)

    assert ingested == ["S_CODER", "S_REVIEW"]
    assert ledger.usage_today_for_provider(conn, "xkiro") == 12
    assert ledger.usage_today_for_provider(conn, "openrouter") == 37
    assert ledger.usage_today(conn, "xkiro", CODER_MODEL) == 12
    assert ledger.usage_today(conn, "openrouter", REVIEWER_MODEL) == 37
    assert _rows(conn) == [
        {"session_id": "S_CODER", "profile": "coder-1", "provider": "xkiro", "model": CODER_MODEL,
         "requests": 12, "input_tokens": 5000, "output_tokens": 400},
        {"session_id": "S_REVIEW", "profile": "reviewer", "provider": "openrouter", "model": REVIEWER_MODEL,
         "requests": 37, "input_tokens": 933438, "output_tokens": 13726},
    ]
    stamped = conn.execute("SELECT ingested_at FROM usage_ingested").fetchall()
    assert len(stamped) == 2 and all(r["ingested_at"] for r in stamped)
    assert not conn.in_transaction      # the savepoint was released, so the writes are committed


def test_ingest_records_one_usage_ingested_event_per_session(conn, tmp_path, monkeypatch):
    _seed_task(conn, "T1", "w1")
    _fake_cards(monkeypatch, {"w1": [_run("S_CODER", "coder-1"), _run("S_REVIEW", "reviewer")]})
    _fake_exports(monkeypatch, {
        "S_CODER": _export("S_CODER", CODER_MODEL, 12),
        "S_REVIEW": _export("S_REVIEW", REVIEWER_MODEL, 37),
    })

    _ingest(conn, tmp_path)

    payloads = [json.loads(r["payload"]) for r in conn.execute(
        "SELECT payload FROM events WHERE kind = 'usage_ingested' ORDER BY id")]
    assert payloads == [
        {"session_id": "S_CODER", "profile": "coder-1", "provider": "xkiro", "model": CODER_MODEL, "requests": 12},
        {"session_id": "S_REVIEW", "profile": "reviewer", "provider": "openrouter", "model": REVIEWER_MODEL,
         "requests": 37},
    ]


def test_ingest_twice_counts_each_session_once(conn, tmp_path, monkeypatch):
    _seed_task(conn, "T1", "w1")
    _fake_cards(monkeypatch, {"w1": [_run("S_CODER", "coder-1"), _run("S_REVIEW", "reviewer")]})
    asked = _fake_exports(monkeypatch, {
        "S_CODER": _export("S_CODER", CODER_MODEL, 12),
        "S_REVIEW": _export("S_REVIEW", REVIEWER_MODEL, 37),
    })

    first = _ingest(conn, tmp_path)
    second = _ingest(conn, tmp_path)

    assert first == ["S_CODER", "S_REVIEW"]
    assert second == []
    assert asked == [("coder-1", "S_CODER"), ("reviewer", "S_REVIEW")]   # never fetched a second time
    assert ledger.usage_today_for_provider(conn, "xkiro") == 12
    assert ledger.usage_today_for_provider(conn, "openrouter") == 37
    assert _count(conn, "usage_ingested") == 2
    assert conn.execute("SELECT COUNT(*) AS n FROM events WHERE kind = 'usage_ingested'").fetchone()["n"] == 2


def test_ingest_counts_a_session_once_even_when_two_runs_name_it(conn, tmp_path, monkeypatch):
    _seed_task(conn, "T1", "w1")
    _fake_cards(monkeypatch, {"w1": [_run("S1"), _run("S1")]})
    asked = _fake_exports(monkeypatch, {"S1": _export("S1", CODER_MODEL, 12)})

    assert _ingest(conn, tmp_path) == ["S1"]
    assert asked == [("coder-1", "S1")]
    assert ledger.usage_today_for_provider(conn, "xkiro") == 12


@pytest.mark.parametrize("ended_at", [None, ""])
def test_ingest_skips_a_run_that_has_not_ended_and_counts_it_once_it_has(conn, tmp_path, monkeypatch, ended_at):
    _seed_task(conn, "T1", "w1")
    runs = [_run("S1", ended_at=ended_at)]
    _fake_cards(monkeypatch, {"w1": runs})
    asked = _fake_exports(monkeypatch, {"S1": _export("S1", CODER_MODEL, 12)})

    assert _ingest(conn, tmp_path) == []
    assert asked == []
    assert ledger.usage_today_for_provider(conn, "xkiro") == 0
    assert _rows(conn) == []

    runs[0]["ended_at"] = 1789832491    # the worker finished

    assert _ingest(conn, tmp_path) == ["S1"]
    assert ledger.usage_today_for_provider(conn, "xkiro") == 12


@pytest.mark.parametrize("metadata", [
    None,
    "",
    "not json",
    "[]",
    "3",
    {},
    {"worker_pid": 4242},
    {"worker_session_id": ""},
    {"worker_session_id": None},
    {"worker_session_id": 12345},
    '{"worker_pid": 4242}',
    '{"worker_session_id": 12345}',
])
def test_ingest_skips_a_run_without_a_usable_session_id(conn, tmp_path, monkeypatch, metadata):
    _seed_task(conn, "T1", "w1")
    _fake_cards(monkeypatch, {"w1": [_run("S1", metadata=metadata)]})
    asked = _fake_exports(monkeypatch, {"S1": _export("S1", CODER_MODEL, 12)})

    assert _ingest(conn, tmp_path) == []
    assert asked == []
    assert ledger.usage_today_for_provider(conn, "xkiro") == 0
    assert _rows(conn) == []


def test_ingest_skips_a_run_the_controller_made_itself(conn, tmp_path, monkeypatch):
    # A merge card's run: no profile, no metadata, so no session to count.
    _seed_task(conn, "T1", "w1")
    _fake_cards(monkeypatch, {"w1": [_run("S1", profile=None, metadata=None)]})
    asked = _fake_exports(monkeypatch, {"S1": _export("S1", CODER_MODEL, 12)})

    assert _ingest(conn, tmp_path) == []
    assert asked == []
    assert _rows(conn) == []


def test_ingest_reads_metadata_that_arrives_as_a_json_string(conn, tmp_path, monkeypatch):
    _seed_task(conn, "T1", "w1")
    metadata = json.dumps({"worker_session_id": "S1", "commit_sha": "abc123"})
    _fake_cards(monkeypatch, {"w1": [_run("S1", metadata=metadata)]})
    _fake_exports(monkeypatch, {"S1": _export("S1", CODER_MODEL, 12)})

    assert _ingest(conn, tmp_path) == ["S1"]
    assert ledger.usage_today_for_provider(conn, "xkiro") == 12


def test_ingest_skips_a_failing_export_and_retries_it_on_the_next_call(conn, tmp_path, monkeypatch):
    _seed_task(conn, "T1", "w1")
    _fake_cards(monkeypatch, {"w1": [_run("S_CODER", "coder-1"), _run("S_REVIEW", "reviewer")]})
    exports = {"S_CODER": None, "S_REVIEW": _export("S_REVIEW", REVIEWER_MODEL, 5)}
    asked = _fake_exports(monkeypatch, exports)

    first = _ingest(conn, tmp_path)

    assert first == ["S_REVIEW"]            # the failed export did not stop the other session
    assert ledger.usage_today_for_provider(conn, "xkiro") == 0
    assert ledger.usage_today_for_provider(conn, "openrouter") == 5
    assert [r["session_id"] for r in _rows(conn)] == ["S_REVIEW"]   # nothing recorded for the failed one

    exports["S_CODER"] = _export("S_CODER", CODER_MODEL, 8)      # the export works now

    second = _ingest(conn, tmp_path)

    assert second == ["S_CODER"]
    assert asked == [("coder-1", "S_CODER"), ("reviewer", "S_REVIEW"), ("coder-1", "S_CODER")]
    assert ledger.usage_today_for_provider(conn, "xkiro") == 8
    assert ledger.usage_today_for_provider(conn, "openrouter") == 5


def test_ingest_records_a_session_with_no_requests_once_and_never_refetches_it(conn, tmp_path, monkeypatch):
    _seed_task(conn, "T1", "w1")
    _fake_cards(monkeypatch, {"w1": [_run("S1")]})
    asked = _fake_exports(monkeypatch, {"S1": _export("S1", CODER_MODEL, 0, 10, 2)})

    first = _ingest(conn, tmp_path)
    second = _ingest(conn, tmp_path)

    assert first == ["S1"]
    assert second == []
    assert asked == [("coder-1", "S1")]
    assert _count(conn, "requests_ledger") == 0     # nothing to count, and record_usage(0) is never called
    assert _rows(conn) == [{"session_id": "S1", "profile": "coder-1", "provider": "xkiro", "model": CODER_MODEL,
                            "requests": 0, "input_tokens": 10, "output_tokens": 2}]


def test_ingest_counts_a_session_that_made_a_single_request(conn, tmp_path, monkeypatch):
    _seed_task(conn, "T1", "w1")
    _fake_cards(monkeypatch, {"w1": [_run("S1")]})
    _fake_exports(monkeypatch, {"S1": _export("S1", CODER_MODEL, 1)})

    assert _ingest(conn, tmp_path) == ["S1"]
    assert ledger.usage_today_for_provider(conn, "xkiro") == 1


def test_ingest_skips_a_run_whose_profile_maps_to_no_role_or_to_no_pinned_provider(conn, tmp_path, monkeypatch):
    _seed_task(conn, "T1", "w1")
    _fake_cards(monkeypatch, {"w1": [_run("S1", "someone-else"), _run("S2", "lead")]})
    asked = _fake_exports(monkeypatch, {"S1": _export("S1", "m", 4), "S2": _export("S2", "m", 6)})

    assert _ingest(conn, tmp_path) == []
    assert asked == []
    assert _rows(conn) == []
    assert _count(conn, "requests_ledger") == 0


def test_ingest_counts_under_the_model_the_session_reports_and_falls_back_to_the_pinned_one(
    conn, tmp_path, monkeypatch,
):
    _seed_task(conn, "T1", "w1")
    _fake_cards(monkeypatch, {"w1": [_run("S_CODER", "coder-1"), _run("S_REVIEW", "reviewer")]})
    _fake_exports(monkeypatch, {
        "S_CODER": _export("S_CODER", "qwen/some-other-model:free", 4),     # reports a model
        "S_REVIEW": _export("S_REVIEW", "", 6),                             # reports none
    })

    _ingest(conn, tmp_path)

    assert ledger.usage_today(conn, "xkiro", "qwen/some-other-model:free") == 4
    assert ledger.usage_today(conn, "xkiro", CODER_MODEL) == 0
    assert ledger.usage_today(conn, "openrouter", REVIEWER_MODEL) == 6
    assert {r["session_id"]: r["model"] for r in _rows(conn)} == {
        "S_CODER": "qwen/some-other-model:free", "S_REVIEW": REVIEWER_MODEL,
    }


def test_ingest_reads_each_tasks_current_card_within_this_plans_project(conn, tmp_path, monkeypatch):
    # Another project on the same board reuses the task key and is the FIRST row in the table, so a lookup
    # that forgot to filter by project would read its card.
    _seed_task(conn, "T1", "other_project_w1", project="p2")
    _seed_task(conn, "T1", "fix_card_1")                 # plan_tasks was repointed at a fix card
    _seed_task(conn, "T2", "w2")
    shown = _fake_cards(monkeypatch, {
        "fix_card_1": [_run("S_FIX", "coder-1")],
        "w2": [_run("S_REVIEW", "reviewer")],
        "other_project_w1": [_run("S_OTHER", "coder-1")],
        "original_w1": [_run("S_ORIGINAL", "coder-1")],
    })
    _fake_exports(monkeypatch, {
        "S_FIX": _export("S_FIX", CODER_MODEL, 3), "S_REVIEW": _export("S_REVIEW", REVIEWER_MODEL, 4),
        "S_OTHER": _export("S_OTHER", CODER_MODEL, 100), "S_ORIGINAL": _export("S_ORIGINAL", CODER_MODEL, 100),
    })

    ingested = _ingest(conn, tmp_path)

    assert shown == ["fix_card_1", "w2"]      # plan order, current cards only, never the other project's
    assert ingested == ["S_FIX", "S_REVIEW"]
    assert ledger.usage_today_for_provider(conn, "xkiro") == 3


def test_ingest_skips_a_task_that_has_no_card_yet(conn, tmp_path, monkeypatch):
    # T1 has no plan_tasks row at all, T2's row has no work card id.
    conn.execute(
        "INSERT INTO plan_tasks (project, task_key, work_card_id, role, created_at) "
        "VALUES ('p1', 'T2', NULL, 'reviewer', datetime('now'))"
    )
    shown = _fake_cards(monkeypatch, {})
    asked = _fake_exports(monkeypatch, {})

    assert _ingest(conn, tmp_path) == []
    assert shown == []
    assert asked == []


@pytest.mark.parametrize("card", [{"id": "w1"}, {"id": "w1", "_runs": None}, {"id": "w1", "_runs": []}])
def test_ingest_tolerates_a_card_with_no_runs(conn, tmp_path, monkeypatch, card):
    _seed_task(conn, "T1", "w1")
    monkeypatch.setattr(hermes, "kanban_show", lambda board, card_id: card)
    asked = _fake_exports(monkeypatch, {})

    assert _ingest(conn, tmp_path) == []
    assert asked == []


def test_ingest_leaves_nothing_behind_when_a_write_fails_part_way(conn, tmp_path, monkeypatch):
    _seed_task(conn, "T1", "w1")
    _fake_cards(monkeypatch, {"w1": [_run("S1")]})
    _fake_exports(monkeypatch, {"S1": _export("S1", CODER_MODEL, 12)})
    real_record = events.record

    def locked(_conn, kind, payload=None, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(events, "record", locked)
    with pytest.raises(sqlite3.OperationalError):
        _ingest(conn, tmp_path)

    # The ledger increment and the usage_ingested row went together: neither is left, and no transaction is
    # left open. Counted-but-unmarked would double count on the next call, marked-but-uncounted would lose it.
    assert ledger.usage_today_for_provider(conn, "xkiro") == 0
    assert _rows(conn) == []
    assert not conn.in_transaction

    monkeypatch.setattr(events, "record", real_record)

    assert _ingest(conn, tmp_path) == ["S1"]
    assert ledger.usage_today_for_provider(conn, "xkiro") == 12


def test_ingest_nests_inside_a_callers_transaction(conn, tmp_path, monkeypatch):
    _seed_task(conn, "T1", "w1")
    _fake_cards(monkeypatch, {"w1": [_run("S1")]})
    _fake_exports(monkeypatch, {"S1": _export("S1", CODER_MODEL, 12)})

    conn.execute("BEGIN")
    assert _ingest(conn, tmp_path) == ["S1"]
    assert conn.in_transaction              # the caller's transaction was not committed out from under it
    conn.execute("ROLLBACK")

    assert ledger.usage_today_for_provider(conn, "xkiro") == 0
    assert _rows(conn) == []


# ---------------------------------------------------------------------------------------------
# review_budget
# ---------------------------------------------------------------------------------------------


def _fill(conn, used):
    ledger.record_usage(conn, "openrouter", REVIEWER_MODEL, used)


def test_review_budget_is_none_without_a_pinned_reviewer_provider(conn, tmp_path):
    no_reviewer = {**MODELS, "models": [m for m in MODELS["models"] if m["role_class"] != "reviewer"]}
    unpinned = {**MODELS, "models": [{**m, "pinned": False} if m["role_class"] == "reviewer" else m
                                     for m in MODELS["models"]]}

    assert usage.review_budget(conn, no_reviewer, _project(tmp_path)) is None
    assert usage.review_budget(conn, unpinned, _project(tmp_path)) is None


def test_review_budget_is_unaffordable_when_the_reviewer_provider_is_nearly_out(conn, tmp_path):
    _fill(conn, 40)     # 10 of OpenRouter's 50 left: a review needs 20, and 5 more are held back daily

    result = usage.review_budget(conn, MODELS, _project(tmp_path))

    assert result.can_afford is False
    assert result.remaining_today == 10
    assert result.limit_today == 50


def test_review_budget_is_affordable_with_room_left(conn, tmp_path):
    result = usage.review_budget(conn, MODELS, _project(tmp_path))

    assert result.can_afford is True
    assert result.remaining_today == 50
    assert result.limit_today == 50


@pytest.mark.parametrize("budgets, used, affordable", [
    # The review reserve is counted once, on top of the daily reserve (10% of 50 is 5): 25 left, minus 5, is 20.
    ({"review_reserve_requests": 20, "daily_reserve_percent": 10}, 25, True),
    ({"review_reserve_requests": 20, "daily_reserve_percent": 10}, 26, False),
    ({"review_reserve_requests": 20, "daily_reserve_percent": 0}, 30, True),
    ({"review_reserve_requests": 20, "daily_reserve_percent": 0}, 31, False),
    ({"review_reserve_requests": 0, "daily_reserve_percent": 10}, 45, True),
    ({"review_reserve_requests": 0, "daily_reserve_percent": 10}, 46, False),
    # Neither set: nothing is asked for and nothing is held back.
    ({}, 45, True),
])
def test_review_budget_counts_the_review_reserve_once_on_top_of_the_daily_reserve(
    conn, tmp_path, budgets, used, affordable,
):
    ledger.record_usage(conn, "openrouter", "some-other-model", used - 10)   # usage sums across models
    ledger.record_usage(conn, "openrouter", REVIEWER_MODEL, 10)

    result = usage.review_budget(conn, MODELS, _project(tmp_path, budgets))

    assert result.can_afford is affordable
    assert result.remaining_today == 50 - used


def test_review_budget_is_affordable_for_a_reviewer_provider_with_no_known_daily_cap(conn, tmp_path):
    models = {**MODELS, "models": [{**m, "provider": "xkiro"} if m["role_class"] == "reviewer" else m
                                   for m in MODELS["models"]]}
    ledger.record_usage(conn, "xkiro", REVIEWER_MODEL, 10_000)

    result = usage.review_budget(conn, models, _project(tmp_path))

    assert result.can_afford is True
    assert result.remaining_today is None


def test_review_budget_asks_about_the_reviewers_provider_and_not_the_coders(conn, tmp_path):
    models = {
        "providers": {
            "coder_host": {"limits": {"per_day": 10}},
            "openrouter": {"limits": {"per_day": 50}},
        },
        "models": [
            {"provider": "coder_host", "model": CODER_MODEL, "role_class": "coder", "pinned": True},
            {"provider": "openrouter", "model": REVIEWER_MODEL, "role_class": "reviewer", "pinned": True},
        ],
    }
    ledger.record_usage(conn, "coder_host", CODER_MODEL, 10)    # the coder's provider is used up

    assert usage.review_budget(conn, models, _project(tmp_path)).can_afford is True

    _fill(conn, 45)                                             # now the reviewer's is nearly out

    assert usage.review_budget(conn, models, _project(tmp_path)).can_afford is False


# ---------------------------------------------------------------------------------------------
# ASES-RTE-01: "No hidden fallback; the provider and model actually used are recorded". A session that ran on a
# model other than the one pinned for its profile is DETECTED (one model_mismatch event per session) and still
# counted; nothing is failed or stopped.
# ---------------------------------------------------------------------------------------------


def _mismatches(conn):
    return [json.loads(r["payload"]) for r in conn.execute(
        "SELECT payload FROM events WHERE kind = 'model_mismatch' ORDER BY id")]


def _with_models(*extra_rows):
    """MODELS plus more rows, for a test that needs a second provider to list a model."""
    return {**MODELS, "providers": {**MODELS["providers"], "other": {"limits": {}}},
            "models": [*MODELS["models"], *extra_rows]}


def _ingest_with(conn, tmp_path, models):
    return usage.ingest_run_usage("b", PLAN, _project(tmp_path), models, conn=conn)


def test_a_session_on_another_model_records_exactly_one_model_mismatch_event(conn, tmp_path, monkeypatch):
    _seed_task(conn, "T1", "w1")
    _fake_cards(monkeypatch, {"w1": [_run("S1", "coder-1")]})
    asked = _fake_exports(monkeypatch, {"S1": _export("S1", "qwen/some-other-model:free", 4)})

    assert _ingest(conn, tmp_path) == ["S1"]

    assert _mismatches(conn) == [{
        "profile": "coder-1", "expected": CODER_MODEL, "actual": "qwen/some-other-model:free", "session_id": "S1",
    }]
    # Detection only: the session is still counted (an unknown model stays with the profile's provider).
    assert ledger.usage_today_for_provider(conn, "xkiro") == 4
    assert [r["provider"] for r in _rows(conn)] == ["xkiro"]

    assert _ingest(conn, tmp_path) == []            # a second pass never looks at the session again...
    assert len(_mismatches(conn)) == 1              # ...so it can never record its mismatch twice
    assert asked == [("coder-1", "S1")]


def test_two_sessions_on_wrong_models_record_one_event_each(conn, tmp_path, monkeypatch):
    _seed_task(conn, "T1", "w1")
    _fake_cards(monkeypatch, {"w1": [_run("S_A", "coder-1"), _run("S_B", "reviewer")]})
    _fake_exports(monkeypatch, {
        "S_A": _export("S_A", "vendor/model-a:free", 1), "S_B": _export("S_B", "vendor/model-b:free", 2),
    })

    _ingest(conn, tmp_path)

    assert _mismatches(conn) == [
        {"profile": "coder-1", "expected": CODER_MODEL, "actual": "vendor/model-a:free", "session_id": "S_A"},
        {"profile": "reviewer", "expected": REVIEWER_MODEL, "actual": "vendor/model-b:free", "session_id": "S_B"},
    ]


def test_no_model_mismatch_when_the_sessions_ran_on_the_pinned_models(conn, tmp_path, monkeypatch):
    _seed_task(conn, "T1", "w1")
    _fake_cards(monkeypatch, {"w1": [_run("S_CODER", "coder-1"), _run("S_REVIEW", "reviewer")]})
    _fake_exports(monkeypatch, {
        "S_CODER": _export("S_CODER", CODER_MODEL, 12), "S_REVIEW": _export("S_REVIEW", REVIEWER_MODEL, 37),
    })

    _ingest(conn, tmp_path)

    assert _mismatches(conn) == []
    assert _count(conn, "usage_ingested") == 2


@pytest.mark.parametrize("model", ["", None, "   "], ids=["empty", "missing", "blank"])
def test_a_session_that_reports_no_model_is_not_a_mismatch(conn, tmp_path, monkeypatch, model):
    _seed_task(conn, "T1", "w1")
    _fake_cards(monkeypatch, {"w1": [_run("S1", "coder-1")]})
    _fake_exports(monkeypatch, {"S1": _export("S1", model, 3)})

    assert _ingest(conn, tmp_path) == ["S1"]

    assert _mismatches(conn) == []
    assert ledger.usage_today_for_provider(conn, "xkiro") == 3


@pytest.mark.parametrize("reported", [
    f"xkiro/{CODER_MODEL}", f"XKIRO/{CODER_MODEL}", f"  {CODER_MODEL}  ",
], ids=["prefixed", "prefixed-any-case", "padded"])
def test_a_leading_provider_name_on_the_reported_model_is_looked_past(conn, tmp_path, monkeypatch, reported):
    _seed_task(conn, "T1", "w1")
    _fake_cards(monkeypatch, {"w1": [_run("S1", "coder-1")]})
    _fake_exports(monkeypatch, {"S1": _export("S1", reported, 5)})

    _ingest(conn, tmp_path)

    assert _mismatches(conn) == []
    assert ledger.usage_today_for_provider(conn, "xkiro") == 5


def test_a_leading_provider_name_on_the_pinned_model_is_looked_past_too(conn, tmp_path, monkeypatch):
    """The config can spell the pinned model with its provider in front while Hermes reports it without."""
    models = {**MODELS, "models": [
        {**m, "model": f"openrouter/{REVIEWER_MODEL}"} if m["role_class"] == "reviewer" else m
        for m in MODELS["models"]
    ]}
    _seed_task(conn, "T1", "w1")
    _fake_cards(monkeypatch, {"w1": [_run("S1", "reviewer")]})
    _fake_exports(monkeypatch, {"S1": _export("S1", REVIEWER_MODEL, 7)})

    _ingest_with(conn, tmp_path, models)

    assert _mismatches(conn) == []
    assert ledger.usage_today_for_provider(conn, "openrouter") == 7


def test_a_prefix_naming_another_configured_provider_is_a_mismatch_counted_against_that_provider(
    conn, tmp_path, monkeypatch,
):
    """The same model name, but through OpenRouter's door: the coder's requests went to a provider with a 50 a day
    cap, and that is where they must be counted. The event keeps both strings exactly as they were."""
    _seed_task(conn, "T1", "w1")
    _fake_cards(monkeypatch, {"w1": [_run("S1", "coder-1")]})
    _fake_exports(monkeypatch, {"S1": _export("S1", f"openrouter/{CODER_MODEL}", 5)})

    _ingest(conn, tmp_path)

    assert _mismatches(conn) == [{
        "profile": "coder-1", "expected": CODER_MODEL, "actual": f"openrouter/{CODER_MODEL}", "session_id": "S1",
    }]
    assert ledger.usage_today_for_provider(conn, "openrouter") == 5
    assert ledger.usage_today_for_provider(conn, "xkiro") == 0
    assert [(r["provider"], r["model"]) for r in _rows(conn)] == [("openrouter", f"openrouter/{CODER_MODEL}")]
    ingested = conn.execute("SELECT payload FROM events WHERE kind = 'usage_ingested'").fetchone()
    assert json.loads(ingested["payload"])["provider"] == "openrouter"


def test_a_model_only_one_other_provider_lists_is_counted_against_that_provider(conn, tmp_path, monkeypatch):
    # The coder profile's session reports the reviewer's model, which only OpenRouter lists.
    _seed_task(conn, "T1", "w1")
    _fake_cards(monkeypatch, {"w1": [_run("S1", "coder-1")]})
    _fake_exports(monkeypatch, {"S1": _export("S1", REVIEWER_MODEL, 6)})

    _ingest(conn, tmp_path)

    assert len(_mismatches(conn)) == 1
    assert ledger.usage_today_for_provider(conn, "openrouter") == 6
    assert ledger.usage_today_for_provider(conn, "xkiro") == 0


def test_a_model_two_other_providers_list_stays_with_the_profiles_provider(conn, tmp_path, monkeypatch):
    """No evidence which of the two answered, so nothing moves between quotas on a guess."""
    models = _with_models({"provider": "other", "model": REVIEWER_MODEL, "role_class": "reviewer_candidate",
                           "pinned": False})
    _seed_task(conn, "T1", "w1")
    _fake_cards(monkeypatch, {"w1": [_run("S1", "coder-1")]})
    _fake_exports(monkeypatch, {"S1": _export("S1", REVIEWER_MODEL, 6)})

    _ingest_with(conn, tmp_path, models)

    assert len(_mismatches(conn)) == 1
    assert ledger.usage_today_for_provider(conn, "xkiro") == 6
    assert ledger.usage_today_for_provider(conn, "openrouter") == 0


def test_a_model_the_profiles_own_provider_also_lists_stays_with_the_profiles_provider(conn, tmp_path, monkeypatch):
    # xkiro lists the lead's model too, so a coder session on it is a different model but not a different provider,
    # even though OpenRouter lists the same name.
    models = _with_models({"provider": "openrouter", "model": "qwen/qwen3.8-max:free", "role_class": "lead_candidate",
                           "pinned": False})
    _seed_task(conn, "T1", "w1")
    _fake_cards(monkeypatch, {"w1": [_run("S1", "coder-1")]})
    _fake_exports(monkeypatch, {"S1": _export("S1", "qwen/qwen3.8-max:free", 9)})

    _ingest_with(conn, tmp_path, models)

    assert len(_mismatches(conn)) == 1
    assert ledger.usage_today_for_provider(conn, "xkiro") == 9
    assert ledger.usage_today_for_provider(conn, "openrouter") == 0


def test_a_model_no_provider_lists_stays_with_the_profiles_provider(conn, tmp_path, monkeypatch):
    _seed_task(conn, "T1", "w1")
    _fake_cards(monkeypatch, {"w1": [_run("S1", "reviewer")]})
    _fake_exports(monkeypatch, {"S1": _export("S1", "brand/new-model:free", 2)})

    _ingest(conn, tmp_path)

    assert len(_mismatches(conn)) == 1
    assert ledger.usage_today_for_provider(conn, "openrouter") == 2


def test_a_provider_name_is_not_a_prefix_unless_it_is_followed_by_a_slash(conn, tmp_path, monkeypatch):
    """`openrouterish/x` starts with the word openrouter but names no provider, and is not the pinned model."""
    _seed_task(conn, "T1", "w1")
    _fake_cards(monkeypatch, {"w1": [_run("S1", "coder-1")]})
    _fake_exports(monkeypatch, {"S1": _export("S1", "openrouterish/some-model", 2)})

    _ingest(conn, tmp_path)

    assert len(_mismatches(conn)) == 1
    assert ledger.usage_today_for_provider(conn, "xkiro") == 2
    assert ledger.usage_today_for_provider(conn, "openrouter") == 0


def test_the_model_mismatch_event_waits_for_an_export_that_works(conn, tmp_path, monkeypatch):
    _seed_task(conn, "T1", "w1")
    _fake_cards(monkeypatch, {"w1": [_run("S1", "coder-1")]})
    exports = {"S1": None}
    _fake_exports(monkeypatch, exports)

    assert _ingest(conn, tmp_path) == []
    assert _mismatches(conn) == []

    exports["S1"] = _export("S1", "vendor/model-a:free", 3)

    assert _ingest(conn, tmp_path) == ["S1"]
    assert [m["session_id"] for m in _mismatches(conn)] == ["S1"]


def test_a_failing_mismatch_write_rolls_the_whole_session_back_and_it_is_retried(conn, tmp_path, monkeypatch):
    """The event goes in the same savepoint as the usage row and the ledger increment: a failure at the last write
    leaves none of the three, so the session is counted (and flagged) on the next call instead of half of it."""
    _seed_task(conn, "T1", "w1")
    _fake_cards(monkeypatch, {"w1": [_run("S1", "coder-1")]})
    _fake_exports(monkeypatch, {"S1": _export("S1", "vendor/model-a:free", 3)})
    real_record = events.record

    def flaky(_conn, kind, payload=None, **kwargs):
        if kind == "model_mismatch":
            raise sqlite3.OperationalError("database is locked")
        return real_record(_conn, kind, payload, **kwargs)

    monkeypatch.setattr(events, "record", flaky)
    with pytest.raises(sqlite3.OperationalError):
        _ingest(conn, tmp_path)

    assert _rows(conn) == []
    assert ledger.usage_today_for_provider(conn, "xkiro") == 0
    assert _count(conn, "events") == 0
    assert not conn.in_transaction

    monkeypatch.setattr(events, "record", real_record)

    assert _ingest(conn, tmp_path) == ["S1"]
    assert len(_mismatches(conn)) == 1
    assert ledger.usage_today_for_provider(conn, "xkiro") == 3


def test_a_model_mismatch_only_reads_and_records_it_never_touches_a_card(conn, tmp_path, monkeypatch):
    """Detection only: no card is blocked, reclaimed, commented on or failed. Any Hermes call other than the two
    reads this module makes fails the test."""
    _seed_task(conn, "T1", "w1")
    _fake_cards(monkeypatch, {"w1": [_run("S1", "coder-1")]})
    _fake_exports(monkeypatch, {"S1": _export("S1", "vendor/model-a:free", 3)})

    def boom(*args, **kwargs):
        raise AssertionError("a model mismatch must not change anything on the board")

    for name in dir(hermes):
        if name.startswith("kanban_") and name != "kanban_show":
            monkeypatch.setattr(hermes, name, boom)

    assert _ingest(conn, tmp_path) == ["S1"]
    assert len(_mismatches(conn)) == 1
