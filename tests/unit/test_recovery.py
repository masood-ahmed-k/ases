"""recovery.py: failure classification, the two kinds of retry, and lineage budgets (ASES-REC-01, ASES-REC-02).

The hermes.kanban_* wrappers are replaced by an in-memory board and the database is a temp sqlite file, so nothing
here touches a real board, a real Hermes or a provider. The board mimics the real Hermes behaviours this module
depends on (probed against the installed 0.21.3 source): a block on a card that is already blocked is refused after
its "BLOCKED: <reason>" comment has landed, and an unblock adds an `unblocked` event. Recovery only ever acts on a
`blocked` card, so it asks the user through questions.ask_user, which comments instead of blocking: the question a
test looks for is the text of an "ASES QUESTION:" comment, and a `block` call is a failure of the test."""
import copy
import dataclasses
import json
import types
from datetime import datetime, timezone

import pytest

from ases import bounds, config, db, events, hermes, plan as plan_mod, questions, recovery
from ases.recovery import Bounds, Decision, FailureKind, Lineage

ROLES = {"lead": "lead", "coder": "coder-1", "reviewer": "reviewer"}

CODER_MODEL = "qwen/qwen3-coder-plus:free"
CANDIDATE_1 = "minimax/minimax-m3:free"
CANDIDATE_2 = "minimax/minimax-m2.5:free"
REVIEWER_MODEL = "cohere/north-mini-code:free"

# Shaped like config/models.yaml: a pinned coder with two candidates behind it on the same router, and a reviewer
# on OpenRouter. The lead has a candidate row of a different class, which must never be offered to a coder.
MODELS = {
    "providers": {
        "xkiro": {"limits": {}, "data_policy": "router_ztr_upstream_varies"},
        "openrouter": {"limits": {"per_day_default": 50}, "data_policy": "some_free_endpoints_train"},
    },
    "models": [
        {"provider": "xkiro", "model": "qwen/qwen3.8-max:free", "role_class": "lead", "pinned": True},
        {"provider": "xkiro", "model": CANDIDATE_1, "role_class": "coder_candidate", "pinned": False},
        {"provider": "xkiro", "model": CODER_MODEL, "role_class": "coder", "pinned": True},
        {"provider": "xkiro", "model": CANDIDATE_2, "role_class": "coder_candidate", "pinned": False},
        {"provider": "openrouter", "model": REVIEWER_MODEL, "role_class": "reviewer", "pinned": True},
    ],
}

PLAN = plan_mod.parse_and_validate({
    "project": "p1",
    "integration_branch": "integration",
    "gate_profiles": {"trivial": ["echo ok"]},
    "tasks": [
        {"key": "T1", "title": "scaffold", "role": "coder", "depends_on": [], "touches": ["a.py"],
         "acceptance": ["exists"], "gate_profile": "trivial", "estimated_requests": 10},
        {"key": "T2", "title": "more code", "role": "coder", "depends_on": [], "touches": ["b.py"],
         "acceptance": ["exists"], "gate_profile": "trivial", "estimated_requests": 10},
        {"key": "T3", "title": "review it", "role": "reviewer", "depends_on": [], "touches": [],
         "acceptance": ["reviewed"], "gate_profile": "trivial", "estimated_requests": 5},
    ],
}, known_roles=set(ROLES), max_cards=40)

ENDED = 1_000_000     # when the failed runs below ended, in epoch seconds

# Real strings, so the rules are checked against what providers and Hermes actually write.
CRASH_TEXT = "pid 4242 exited with code 1"
POLICY_TEXT = "HTTP 404: No endpoints found matching your data policy (Free model training)"
QUOTA_TEXT = "HTTP 429: Rate limit reached for model qwen/qwen3.8-27b on tokens per day (TPD): Limit 200000, Used 195327"
AUTH_TEXT = "HTTP 403: This premium model requires an active paid plan or real deposited balance."
PROTOCOL_TEXT = ("worker exited cleanly (rc=0) without calling kanban_complete or kanban_block - protocol "
                 "violation.")

# Non-ASCII inputs are built from code points, so this file itself stays pure ASCII (and never holds an em dash).
ARROW, E_ACUTE, DASH = chr(0x2192), chr(0xE9), chr(0x2014)
CURLY_OPEN, CURLY_CLOSE, CHECK, CROSS = chr(0x201C), chr(0x201D), chr(0x2713), chr(0x2717)
ESCAPED_ARROW = chr(92) + "u2192"          # what a backslash escape of ARROW looks like in the output
ESCAPED_E_ACUTE = chr(92) + "xe9"


@pytest.fixture(autouse=True)
def _no_real_hermes(monkeypatch):
    """Every hermes call a test makes must be one it faked. hermes._run is what every wrapper ends in, so an
    unfaked call fails loudly here instead of reaching the real `hermes` on this machine and its real board."""
    def refuse(*args, **kwargs):
        raise AssertionError(f"a test tried to run a real hermes command: {args!r}")
    monkeypatch.setattr(hermes, "_run", refuse)


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "ases.db")


class FakeBoard:
    """The hermes wrappers recovery.py calls, over a dict of card id -> card. Every mutation is recorded in
    `mutations` as a tuple, in call order. `fail[(wrapper, card id)]` makes that call raise. Unblock, schedule and
    set-model change the card the way Hermes does, so a second pass sees what a real second pass would."""

    def __init__(self, monkeypatch):
        self.cards: dict[str, dict] = {}
        self.mutations: list[tuple] = []
        self.fail: dict[tuple[str, str], Exception] = {}
        self.status_follows = True      # False: a card stays `blocked` whatever is done to it
        self.block_mode = "accept"      # "refuse": block adds its comment, then fails, as Hermes does on a blocked card
        self.clock = 100                # what a comment written now is stamped with: after every event a test seeds
        monkeypatch.setattr(hermes, "kanban_show", self.show)
        monkeypatch.setattr(hermes, "kanban_unblock", self.unblock)
        monkeypatch.setattr(hermes, "kanban_schedule", self.schedule)
        monkeypatch.setattr(hermes, "kanban_block", self.block)
        monkeypatch.setattr(hermes, "kanban_comment", self.comment)
        monkeypatch.setattr(hermes, "kanban_set_model", self.set_model)

    def _maybe_fail(self, name, card_id):
        exc = self.fail.get((name, card_id))
        if exc is not None:
            raise exc

    def show(self, board, card_id):
        self._maybe_fail("show", card_id)
        if card_id not in self.cards:
            raise hermes.HermesCommandError(["kanban", "show", card_id], 1, "no such task")
        return copy.deepcopy(self.cards[card_id])

    def unblock(self, board, card_id, reason=None):
        self._maybe_fail("unblock", card_id)
        self.mutations.append(("unblock", card_id))
        card = self.cards[card_id]
        if self.status_follows:
            card["status"] = "ready"
        self.clock += 1
        card["_events"].append({"kind": "unblocked", "payload": None, "created_at": self.clock, "run_id": None})

    def schedule(self, board, card_id, reason):
        self._maybe_fail("schedule", card_id)
        self.mutations.append(("schedule", card_id, reason))
        if self.status_follows:
            self.cards[card_id]["status"] = "scheduled"

    def block(self, board, card_id, reason, *, kind=None):
        card = self.cards[card_id]
        self.mutations.append(("block", card_id, reason, kind))
        card["_comments"].append({"author": "default", "body": f"BLOCKED: {reason}", "created_at": 2})
        self._maybe_fail("block", card_id)
        if self.block_mode == "refuse":
            raise hermes.HermesCommandError(["kanban", "block", card_id], 1, f"cannot block {card_id}")
        card["_events"].append({"kind": "blocked", "payload": {"reason": reason}, "created_at": 2, "run_id": None})

    def comment(self, board, card_id, text, *, author=None):
        self._maybe_fail("comment", card_id)
        self.mutations.append(("comment", card_id, text, author))
        self.clock += 1
        self.cards[card_id]["_comments"].append(
            {"author": author or "default", "body": text, "created_at": self.clock},
        )

    def set_model(self, board, card_id, model, *, provider=None):
        self._maybe_fail("set_model", card_id)
        self.mutations.append(("set_model", card_id, model, provider))
        self.cards[card_id]["model_override"] = model
        self.cards[card_id]["provider_override"] = provider


@pytest.fixture
def board(monkeypatch):
    return FakeBoard(monkeypatch)


def _run(run_id, outcome="crashed", error=None, summary=None, *, profile="coder-1", ended_at=ENDED, metadata=None):
    """One entry of a card's runs list, as hermes.kanban_show returns it under "_runs"."""
    started_at = ended_at - 60 if isinstance(ended_at, int) else None
    return {"id": run_id, "profile": profile, "status": outcome, "outcome": outcome, "summary": summary,
            "error": error, "metadata": metadata, "started_at": started_at, "ended_at": ended_at,
            "worker_pid": 4242}


def _card(card_id, runs, status="blocked", **extra):
    return {"id": card_id, "status": status, "title": card_id, "_runs": runs, "_events": [], "_comments": [],
            "_children": [], "_parents": [], "_latest_summary": None, **extra}


def _seed_task(conn, key, work_card_id, project="p1", fix_cards=0):
    conn.execute(
        "INSERT INTO plan_tasks (project, task_key, work_card_id, merge_card_id, role, fix_cards, created_at) "
        "VALUES (?, ?, ?, ?, 'coder', ?, datetime('now'))",
        (project, key, work_card_id, f"m_{key}", fix_cards),
    )


def _project(tmp_path, budgets=None, data_class="public"):
    return config.ProjectConfig(
        name="ases", environment="native", data_class=data_class, workspace_root=tmp_path / "ws",
        ases_home=tmp_path / "home", board="b", integration_branch="integration", roles=ROLES,
        concurrency={}, budgets={} if budgets is None else budgets, hermes_tested_version="0.21.3",
        hermes_native_home=tmp_path / "hermes",
    )


def _events_of(conn, kind):
    rows = conn.execute("SELECT payload FROM events WHERE kind = ? ORDER BY id", (kind,)).fetchall()
    return [json.loads(r["payload"]) for r in rows]


def _lineage_row(conn, key, project="p1"):
    row = conn.execute("SELECT * FROM lineage WHERE project = ? AND task_key = ?", (project, key)).fetchone()
    return dict(row) if row else None


def _pass(conn, tmp_path, *, now=ENDED + 10_000, budgets=None, data_class="public", plan=PLAN, models=MODELS):
    return recovery.process_failures(
        "b", plan, _project(tmp_path, budgets, data_class), models, conn=conn, now=now,
    )


def _lineage(**counters):
    return Lineage("p1", "T1", **counters)


ASK = questions.ASK_PREFIX + " "     # "ASES QUESTION: ", what ask_user writes in front of the question


def _asked(board):
    """The questions put to the person, in order: the text of every ASES QUESTION comment the pass wrote."""
    return [m[2][len(ASK):] for m in board.mutations if m[0] == "comment" and m[2].startswith(ASK)]


def _gave_up(failures=3, error="boom", at=50):
    """The `gave_up` event Hermes's dispatcher writes when its circuit breaker trips, and NO `blocked` event."""
    return {"kind": "gave_up", "payload": {"failures": failures, "effective_limit": 3, "error": error,
                                           "trigger_outcome": "crashed"}, "created_at": at, "run_id": None}


# ---------------------------------------------------------------------------------------------
# classify_run
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("error", [
    "HTTP 429 Too Many Requests",
    "Error code: 429",
    "Rate limit exceeded, please slow down",
    "Retry-After: 30",
    "rate_limit_error: requests are being throttled",
])
def test_a_429_or_a_rate_limit_or_retry_after_is_a_rate_limit(error):
    assert recovery.classify_run({"outcome": "crashed", "error": error}) is FailureKind.RATE_LIMIT


@pytest.mark.parametrize("error", [
    "You exceeded your current quota, please check your plan and billing details",
    "Daily quota exhausted",
    "Rate limit exceeded: free-models-per-day. Add 10 credits to unlock 1000 free model requests per day",
    QUOTA_TEXT,
])
def test_a_quota_message_is_a_quota_even_when_it_also_says_429_or_rate_limit(error):
    """The real UnoRouter error (QUOTA_TEXT) contains both 429 and "rate limit" but is a DAILY bucket: a short
    wait will not cure it, so it must not be read as a plain rate limit that Hermes will retry."""
    assert recovery.classify_run({"outcome": "crashed", "error": error}) is FailureKind.QUOTA


@pytest.mark.parametrize("error", [
    "HTTP 503 Service Unavailable",
    "502 Bad Gateway",
    "HTTP 504",
    "HTTP 500 Internal Server Error",
    "Read timed out. (read timeout=60)",
    "Request timeout",
    "Connection reset by peer",
    "Connection error.",
    "The service is temporarily unavailable",
    "Overloaded",
])
def test_5xx_timeouts_and_dropped_connections_are_infrastructure(error):
    assert recovery.classify_run({"outcome": "crashed", "error": error}) is FailureKind.INFRASTRUCTURE


@pytest.mark.parametrize("error", [
    "HTTP 401 Unauthorized",
    "HTTP 403 Forbidden",
    "Invalid API key provided",
    "invalid_api_key",
    "Authentication failed",
    AUTH_TEXT,
])
def test_401_403_and_bad_key_messages_are_auth(error):
    assert recovery.classify_run({"outcome": "crashed", "error": error}) is FailureKind.AUTH


@pytest.mark.parametrize("error", [
    POLICY_TEXT,
    "No endpoints found for this model",
    "provider preference data_collection=deny excludes every endpoint",
    "blocked by your data policy settings",
])
def test_a_data_policy_mismatch_or_missing_endpoint_is_policy(error):
    assert recovery.classify_run({"outcome": "crashed", "error": error}) is FailureKind.POLICY


@pytest.mark.parametrize("error", [
    "This model's maximum context length is 8192 tokens",
    "context window exceeded",
    "context_length_exceeded",
    "HTTP 429: Request too large for model qwen/qwen3.8-27b on output tokens per minute (OTPM): Requested 2048",
    "prompt is too long: 250000 tokens",
])
def test_a_request_the_model_cannot_hold_is_context(error):
    assert recovery.classify_run({"outcome": "crashed", "error": error}) is FailureKind.CONTEXT


@pytest.mark.parametrize("error", [
    "malformed function call from the model",
    "Invalid tool call arguments",
    "tool_use ids were found without tool_result blocks",
    "the model returned an invalid tool name",
])
def test_broken_tool_calling_is_tool_calling(error):
    assert recovery.classify_run({"outcome": "crashed", "error": error}) is FailureKind.TOOL_CALLING


@pytest.mark.parametrize("run", [
    {"outcome": "timed_out"},
    {"outcome": "timed_out", "error": "elapsed 2700s > limit 2700s"},
    {"outcome": "timed_out", "error": "Iteration budget exhausted (90/90) - task could not complete"},
    {"outcome": "gave_up", "error": "elapsed 2700s > limit 2700s"},
    {"outcome": "gave_up", "error": "", "metadata": {"trigger_outcome": "timed_out"}},
    {"outcome": "gave_up", "error": "", "metadata": '{"trigger_outcome": "timed_out"}'},
])
def test_a_run_that_exceeded_its_runtime_is_runtime(run):
    assert recovery.classify_run(run) is FailureKind.RUNTIME


@pytest.mark.parametrize("run", [
    {"outcome": "crashed", "error": PROTOCOL_TEXT},
    {"outcome": "crashed", "error": "", "metadata": {"protocol_violation": True}},
    {"outcome": "crashed", "error": "", "metadata": '{"protocol_violation": true}'},
])
def test_a_worker_that_ends_without_a_terminal_kanban_call_is_a_capability_failure(run):
    assert recovery.classify_run(run) is FailureKind.CAPABILITY


@pytest.mark.parametrize("outcome", ["completed", "review_requested", "changes_requested", "blocked", "scheduled"])
def test_a_run_that_ended_normally_is_none_whatever_its_text_says(outcome):
    """A worker that blocks itself to ask the user about a 429 is asking a question, not failing."""
    assert recovery.classify_run({"outcome": outcome, "error": "HTTP 429", "summary": "401 quota tool call"}) \
        is FailureKind.NONE


@pytest.mark.parametrize("run", [
    {},
    {"outcome": "crashed"},
    {"outcome": "crashed", "error": "", "summary": ""},
    {"outcome": "crashed", "error": None, "summary": None},
    {"outcome": "gave_up", "error": "", "metadata": {}},
    {"outcome": "gave_up", "error": "something odd happened", "metadata": {"trigger_outcome": "crashed"}},
    {"outcome": "some_future_outcome", "error": "no idea"},
    {"outcome": None, "status": None},
])
def test_a_failed_run_with_nothing_recognisable_is_unknown(run):
    assert recovery.classify_run(run) is FailureKind.UNKNOWN


def test_something_that_is_not_a_run_is_unknown():
    assert recovery.classify_run(None) is FailureKind.UNKNOWN
    assert recovery.classify_run("crashed") is FailureKind.UNKNOWN


@pytest.mark.parametrize("metadata", ["not json at all", "[1, 2, 3]", "null", 42, ["trigger_outcome"], b"\xff"])
def test_metadata_that_is_not_a_json_object_is_ignored(metadata):
    assert recovery.classify_run({"outcome": "gave_up", "error": "", "metadata": metadata}) is FailureKind.UNKNOWN
    assert recovery.classify_run({"outcome": "crashed", "error": "HTTP 503", "metadata": metadata}) \
        is FailureKind.INFRASTRUCTURE


@pytest.mark.parametrize("run, expected", [
    ({"outcome": "timed_out", "error": "HTTP 401 Unauthorized"}, FailureKind.RUNTIME),
    ({"outcome": "rate_limited", "error": "HTTP 500 Internal Server Error"}, FailureKind.RATE_LIMIT),
    ({"outcome": "rate_limited", "error": ""}, FailureKind.RATE_LIMIT),
    ({"outcome": "completed", "error": "HTTP 503"}, FailureKind.NONE),
    ({"outcome": "blocked", "summary": "quota exceeded"}, FailureKind.NONE),
])
def test_the_outcome_is_read_before_the_text(run, expected):
    assert recovery.classify_run(run) is expected


@pytest.mark.parametrize("outcome", ["spawn_failed", "stale", "reclaimed"])
def test_spawn_failed_stale_and_reclaimed_are_infrastructure_even_with_no_text(outcome):
    assert recovery.classify_run({"outcome": outcome}) is FailureKind.INFRASTRUCTURE
    assert recovery.classify_run({"outcome": outcome, "error": "stale_lock=host"}) is FailureKind.INFRASTRUCTURE


def test_a_spawn_failed_run_still_reads_its_text_first():
    assert recovery.classify_run({"outcome": "spawn_failed", "error": "HTTP 401 Unauthorized"}) is FailureKind.AUTH


@pytest.mark.parametrize("error", [
    "pid 4242 exited with code 1",
    "pid 4242 killed by signal 9",
    "pid 4242 not alive",
    "stale_lock=host:4242:abcd",
])
def test_hermes_own_crash_bookkeeping_is_infrastructure(error):
    """19.1: "Worker crash or stale claim" is an infrastructure failure. Hermes always says which of these it saw."""
    assert recovery.classify_run({"outcome": "crashed", "error": error}) is FailureKind.INFRASTRUCTURE


def test_a_gave_up_run_takes_the_outcome_of_the_failure_that_tripped_the_breaker():
    spawn = {"outcome": "gave_up", "error": "workspace: git worktree add failed",
             "metadata": {"trigger_outcome": "spawn_failed"}}
    assert recovery.classify_run(spawn) is FailureKind.INFRASTRUCTURE


def test_a_crash_that_carries_a_provider_error_is_classified_by_that_error():
    """Hermes appends the worker's last output to a crash, and that is where the provider's error shows up."""
    run = {"outcome": "crashed",
           "error": "pid 4242 exited with code 1 Worker's last output: 'Error code: 401 - Unauthorized'"}
    assert recovery.classify_run(run) is FailureKind.AUTH


@pytest.mark.parametrize("error, expected", [
    ("HTTP 503 SERVICE UNAVAILABLE", FailureKind.INFRASTRUCTURE),
    ("RATE LIMIT EXCEEDED", FailureKind.RATE_LIMIT),
    ("INVALID API KEY", FailureKind.AUTH),
    ("NO ENDPOINTS FOUND", FailureKind.POLICY),
    ("Maximum Context Length", FailureKind.CONTEXT),
    ("Tool Call failed", FailureKind.TOOL_CALLING),
    ("DAILY QUOTA", FailureKind.QUOTA),
])
def test_text_is_matched_case_insensitively(error, expected):
    assert recovery.classify_run({"outcome": "crashed", "error": error}) is expected


@pytest.mark.parametrize("outcome, expected", [
    ("TIMED_OUT", FailureKind.RUNTIME), ("Rate_Limited", FailureKind.RATE_LIMIT), ("  Completed ", FailureKind.NONE),
])
def test_the_outcome_is_matched_case_insensitively(outcome, expected):
    assert recovery.classify_run({"outcome": outcome}) is expected


@pytest.mark.parametrize("run, expected", [
    ({"status": "done"}, FailureKind.NONE),
    ({"status": "running", "outcome": None}, FailureKind.NONE),       # an open run is not a failure
    ({"status": "review", "outcome": ""}, FailureKind.NONE),
    ({"status": "timed_out"}, FailureKind.RUNTIME),
    ({"status": "rate_limited", "outcome": None}, FailureKind.RATE_LIMIT),
    ({"status": "crashed", "outcome": None, "error": "HTTP 401"}, FailureKind.AUTH),
    ({"status": "done", "outcome": "timed_out"}, FailureKind.RUNTIME),   # the outcome wins over the status
])
def test_the_status_stands_in_for_a_missing_outcome(run, expected):
    assert recovery.classify_run(run) is expected


@pytest.mark.parametrize("error", ["Limit 500 requests reached, Used 500", "Requested 401 tokens", "max 403"])
def test_a_status_code_that_is_really_a_quantity_is_not_a_status(error):
    assert recovery.classify_run({"outcome": "crashed", "error": error}) is FailureKind.UNKNOWN


@pytest.mark.parametrize("error, expected", [
    ("HTTP 503 from the auth service: unauthorized", FailureKind.AUTH),        # never retry an auth failure
    ("HTTP 429 quota exceeded", FailureKind.QUOTA),
    ("HTTP 500: invalid tool call format", FailureKind.TOOL_CALLING),
    ("pid 4242 exited with code 1 after HTTP 503 Service Unavailable", FailureKind.INFRASTRUCTURE),
    ("HTTP 404 no endpoints found (data policy), HTTP 503", FailureKind.POLICY),
])
def test_the_more_specific_kind_wins_when_a_message_carries_several(error, expected):
    assert recovery.classify_run({"outcome": "crashed", "error": error}) is expected


def test_the_error_is_read_before_the_summary_and_the_summary_is_the_fallback():
    assert recovery.classify_run(
        {"outcome": "crashed", "error": "HTTP 503", "summary": "HTTP 401 Unauthorized"},
    ) is FailureKind.INFRASTRUCTURE
    assert recovery.classify_run(
        {"outcome": "crashed", "error": "", "summary": "HTTP 401 Unauthorized"},
    ) is FailureKind.AUTH


def test_classify_run_never_changes_the_run_it_was_given():
    run = {"id": 3, "outcome": "gave_up", "error": "boom 503", "summary": None,
           "metadata": {"trigger_outcome": "crashed", "nested": {"a": [1, 2]}}}
    before = copy.deepcopy(run)
    recovery.classify_run(run)
    assert run == before


def test_failure_kind_is_a_string_enum():
    assert FailureKind.RATE_LIMIT == "rate_limit"
    assert FailureKind("infrastructure") is FailureKind.INFRASTRUCTURE
    assert {kind.value for kind in FailureKind} == {
        "none", "rate_limit", "quota", "infrastructure", "auth", "policy", "context", "tool_calling",
        "capability", "runtime", "unknown",
    }


# ---------------------------------------------------------------------------------------------
# Lineage: load, bump, refresh_review_rounds
# ---------------------------------------------------------------------------------------------


def test_load_lineage_of_a_task_with_no_rows_is_all_zero(conn):
    assert recovery.load_lineage(conn, "p1", "T1") == Lineage("p1", "T1", 0, 0, 0, 0, 0, 0)


def test_load_lineage_reads_the_lineage_row_the_fix_cards_and_the_requests(conn):
    _seed_task(conn, "T1", "w1", fix_cards=2)
    conn.execute(
        "INSERT INTO lineage (project, task_key, review_rounds, capability_failures, infra_failures, replans, "
        "updated_at) VALUES ('p1', 'T1', 3, 2, 1, 1, 'now')"
    )
    for session, task, requests in (("s1", "T1", 12), ("s2", "T1", 30), ("s3", "T2", 99)):
        conn.execute(
            "INSERT INTO usage_ingested (session_id, profile, provider, model, requests, ingested_at, project, "
            "task_key, card_id) VALUES (?, 'coder-1', 'xkiro', 'm', ?, 'now', 'p1', ?, 'w')",
            (session, requests, task),
        )

    assert recovery.load_lineage(conn, "p1", "T1") == Lineage(
        "p1", "T1", review_rounds=3, capability_failures=2, infra_failures=1, replans=1, fix_cards=2, requests=42,
    )


def test_load_lineage_is_scoped_to_its_project(conn):
    _seed_task(conn, "T1", "w1", project="p1", fix_cards=1)
    _seed_task(conn, "T1", "w9", project="p2", fix_cards=2)
    conn.execute("INSERT INTO lineage (project, task_key, replans, updated_at) VALUES ('p2', 'T1', 1, 'now')")
    assert recovery.load_lineage(conn, "p1", "T1").fix_cards == 1
    assert recovery.load_lineage(conn, "p1", "T1").replans == 0
    assert recovery.load_lineage(conn, "p2", "T1").replans == 1


@pytest.mark.parametrize("field", ["review_rounds", "capability_failures", "infra_failures", "replans"])
def test_bump_creates_the_row_and_adds_to_the_one_field(conn, field):
    recovery.bump(conn, "p1", "T1", field)
    row = _lineage_row(conn, "T1")
    assert {name: row[name] for name in
            ("review_rounds", "capability_failures", "infra_failures", "replans")} == {
        "review_rounds": 0, "capability_failures": 0, "infra_failures": 0, "replans": 0, field: 1}

    recovery.bump(conn, "p1", "T1", field, 2)
    assert _lineage_row(conn, "T1")[field] == 3


def test_bump_stamps_updated_at_in_utc_seconds(conn):
    recovery.bump(conn, "p1", "T1", "replans")
    stamp = datetime.fromisoformat(_lineage_row(conn, "T1")["updated_at"])
    assert stamp.tzinfo is not None and stamp.utcoffset().total_seconds() == 0
    assert stamp.microsecond == 0
    assert abs((datetime.now(timezone.utc) - stamp).total_seconds()) < 60


def test_bump_leaves_the_review_bookkeeping_and_other_tasks_alone(conn):
    conn.execute(
        "INSERT INTO lineage (project, task_key, review_rounds, seen_card, seen_events, updated_at) "
        "VALUES ('p1', 'T1', 1, 'w1', 4, 'then')"
    )
    recovery.bump(conn, "p1", "T2", "infra_failures")
    recovery.bump(conn, "p1", "T1", "infra_failures")
    row = _lineage_row(conn, "T1")
    assert (row["review_rounds"], row["seen_card"], row["seen_events"], row["infra_failures"]) == (1, "w1", 4, 1)
    assert _lineage_row(conn, "T2")["infra_failures"] == 1
    assert _lineage_row(conn, "T1", project="p2") is None


@pytest.mark.parametrize("field", ["fix_cards", "requests", "seen_events", "project", "", "replans; DROP TABLE lineage"])
def test_bump_refuses_a_field_that_is_not_a_lineage_counter(conn, field):
    with pytest.raises(ValueError):
        recovery.bump(conn, "p1", "T1", field)
    assert _lineage_row(conn, "T1") is None


@pytest.mark.parametrize("n", [0, -1])
def test_bump_refuses_a_non_positive_amount(conn, n):
    with pytest.raises(ValueError):
        recovery.bump(conn, "p1", "T1", "replans", n)
    assert _lineage_row(conn, "T1") is None


def _review_events(*kinds):
    return [{"kind": kind, "payload": None, "created_at": i, "run_id": None} for i, kind in enumerate(kinds)]


def test_refresh_review_rounds_counts_changes_requested_and_review_reopened_only(conn, board):
    _seed_task(conn, "T1", "w1")
    board.cards["w1"] = _card("w1", [], status="review", _events=_review_events(
        "spawned", "review_requested", "changes_requested", "commented", "review_reopened", "blocked", "unblocked",
    ))

    assert recovery.refresh_review_rounds("b", PLAN, conn=conn) == {"T1": 2}

    row = _lineage_row(conn, "T1")
    assert (row["review_rounds"], row["seen_card"], row["seen_events"]) == (2, "w1", 2)
    assert recovery.load_lineage(conn, "p1", "T1").review_rounds == 2


def test_refresh_review_rounds_is_idempotent_and_only_adds_what_is_new(conn, board):
    _seed_task(conn, "T1", "w1")
    board.cards["w1"] = _card("w1", [], status="review", _events=_review_events("changes_requested"))
    assert recovery.refresh_review_rounds("b", PLAN, conn=conn) == {"T1": 1}
    assert recovery.refresh_review_rounds("b", PLAN, conn=conn) == {"T1": 0}
    assert recovery.load_lineage(conn, "p1", "T1").review_rounds == 1

    board.cards["w1"]["_events"] += _review_events("review_reopened", "changes_requested")
    assert recovery.refresh_review_rounds("b", PLAN, conn=conn) == {"T1": 2}
    assert recovery.refresh_review_rounds("b", PLAN, conn=conn) == {"T1": 0}
    assert recovery.load_lineage(conn, "p1", "T1").review_rounds == 3


def test_refresh_review_rounds_counts_a_fix_card_from_zero_and_keeps_the_rounds_already_counted(conn, board):
    _seed_task(conn, "T1", "w1")
    board.cards["w1"] = _card("w1", [], status="blocked", _events=_review_events("changes_requested", "changes_requested"))
    board.cards["fix1"] = _card("fix1", [], status="review", _events=_review_events("changes_requested"))
    assert recovery.refresh_review_rounds("b", PLAN, conn=conn) == {"T1": 2}

    # process_merge_queue opens a fix card and repoints the task at it.
    conn.execute("UPDATE plan_tasks SET work_card_id = 'fix1', fix_cards = 1 WHERE task_key = 'T1'")
    assert recovery.refresh_review_rounds("b", PLAN, conn=conn) == {"T1": 1}
    row = _lineage_row(conn, "T1")
    assert (row["review_rounds"], row["seen_card"], row["seen_events"]) == (3, "fix1", 1)
    assert recovery.refresh_review_rounds("b", PLAN, conn=conn) == {"T1": 0}

    # A second fix card with no review yet adds nothing but takes over the bookkeeping.
    board.cards["fix2"] = _card("fix2", [], status="running")
    conn.execute("UPDATE plan_tasks SET work_card_id = 'fix2' WHERE task_key = 'T1'")
    assert recovery.refresh_review_rounds("b", PLAN, conn=conn) == {"T1": 0}
    row = _lineage_row(conn, "T1")
    assert (row["review_rounds"], row["seen_card"], row["seen_events"]) == (3, "fix2", 0)
    board.cards["fix2"]["_events"] = _review_events("changes_requested")
    assert recovery.refresh_review_rounds("b", PLAN, conn=conn) == {"T1": 1}
    assert _lineage_row(conn, "T1")["review_rounds"] == 4


def test_refresh_review_rounds_never_adds_a_negative_amount(conn, board):
    _seed_task(conn, "T1", "w1")
    board.cards["w1"] = _card("w1", [], _events=_review_events("changes_requested", "changes_requested"))
    recovery.refresh_review_rounds("b", PLAN, conn=conn)

    board.cards["w1"]["_events"] = _review_events("changes_requested")      # fewer than were counted
    assert recovery.refresh_review_rounds("b", PLAN, conn=conn) == {"T1": 0}
    row = _lineage_row(conn, "T1")
    assert (row["review_rounds"], row["seen_events"]) == (2, 2)

    board.cards["w1"]["_events"] = _review_events("changes_requested", "changes_requested", "review_reopened")
    assert recovery.refresh_review_rounds("b", PLAN, conn=conn) == {"T1": 1}


def test_refresh_review_rounds_reports_every_task_it_read_and_writes_nothing_for_a_quiet_one(conn, board):
    _seed_task(conn, "T1", "w1")
    _seed_task(conn, "T2", "w2")
    board.cards["w1"] = _card("w1", [], _events=_review_events("changes_requested"))
    board.cards["w2"] = _card("w2", [])
    assert recovery.refresh_review_rounds("b", PLAN, conn=conn) == {"T1": 1, "T2": 0}
    assert _lineage_row(conn, "T2") is None      # no row, and so no updated_at churn, for a task with nothing new


def test_refresh_review_rounds_is_scoped_to_the_plans_project(conn, board):
    _seed_task(conn, "T1", "w1", project="p1")
    _seed_task(conn, "T1", "other", project="p2")
    conn.execute("INSERT INTO lineage (project, task_key, review_rounds, seen_card, seen_events, updated_at) "
                 "VALUES ('p2', 'T1', 5, 'other', 5, 'then')")
    board.cards["w1"] = _card("w1", [], _events=_review_events("changes_requested"))
    recovery.refresh_review_rounds("b", PLAN, conn=conn)
    assert _lineage_row(conn, "T1")["review_rounds"] == 1
    assert _lineage_row(conn, "T1", project="p2")["review_rounds"] == 5


def test_refresh_review_rounds_skips_a_task_with_no_card_and_survives_an_unreadable_one(conn, board):
    _seed_task(conn, "T1", "w1")
    _seed_task(conn, "T2", "w2")
    conn.execute("INSERT INTO plan_tasks (project, task_key, work_card_id, merge_card_id, role, created_at) "
                 "VALUES ('p1', 'T3', NULL, NULL, 'reviewer', datetime('now'))")
    board.cards["w1"] = _card("w1", [], _events=_review_events("changes_requested"))
    board.cards["w2"] = _card("w2", [], _events=_review_events("changes_requested"))
    board.fail[("show", "w1")] = hermes.HermesCommandError(["kanban", "show", "w1"], 1, "database is locked")

    assert recovery.refresh_review_rounds("b", PLAN, conn=conn) == {"T2": 1}      # T1 unreadable, T3 has no card

    errors = _events_of(conn, "recovery_error")
    assert [(e["task_key"], e["card_id"], e["action"]) for e in errors] == [("T1", "w1", "refresh_review_rounds")]
    assert "database is locked" in errors[0]["error"]
    recovery.refresh_review_rounds("b", PLAN, conn=conn)
    assert len(_events_of(conn, "recovery_error")) == 1      # the same failure is not logged again on the next pass


# ---------------------------------------------------------------------------------------------
# Bounds and exhausted
# ---------------------------------------------------------------------------------------------


def test_recovery_bounds_is_an_alias_of_bounds_bounds():
    """Round 6 fix: there used to be two independently-defined Bounds classes (recovery's own 4-field one, lenient,
    and bounds.py's 8-field one, strict) for the same section 9.3 concept. Now there is exactly one class."""
    assert recovery.Bounds is bounds.Bounds
    assert Bounds is bounds.Bounds


def test_bounds_default_to_the_blueprint_section_9_3_values():
    assert Bounds() == Bounds(attempts_per_card=3, review_rounds_per_task=3, fix_cards_per_task=2,
                              replans_per_project=2)
    assert Bounds.from_budgets({}) == Bounds()
    assert Bounds.from_budgets(None) == Bounds()


def test_bounds_read_the_budget_keys_and_ignore_the_others():
    budgets = {"attempts_per_card": 5, "review_rounds_per_task": 4, "fix_cards_per_task": 1,
               "replans_per_project": 7, "max_cards": 40, "card_runtime_minutes": 45}
    assert Bounds.from_budgets(budgets) == Bounds(5, 4, 1, 7)


def test_bounds_now_also_carries_the_fields_only_bounds_py_used_to_have():
    """Since recovery.Bounds is bounds.Bounds (not a 4-field lookalike any more), a budgets dict that sets one of
    the other four section 9.3 fields is honoured too, not silently dropped."""
    full = Bounds.from_budgets({"max_cards": 99, "card_runtime_minutes": 12, "daily_reserve_percent": 25,
                                "project_wall_clock_minutes": 480})
    assert (full.max_cards, full.card_runtime_minutes, full.daily_reserve_percent,
            full.project_wall_clock_minutes) == (99, 12, 25, 480)
    # exhausted() and decide() only ever read the 4 fields recovery.py always had, so this is safe either way.
    assert recovery.exhausted(_lineage(capability_failures=3), full) == "attempts"


def test_bounds_take_the_default_for_a_missing_key():
    assert Bounds.from_budgets({"attempts_per_card": 6}) == Bounds(attempts_per_card=6)
    assert Bounds.from_budgets({"replans_per_project": 4}) == Bounds(replans_per_project=4)


def test_bounds_from_budgets_is_strict_now_a_present_bad_value_raises_instead_of_defaulting():
    """Before round 6, recovery.Bounds.from_budgets had its OWN lenient parser: a present value that was not a
    usable int silently fell back to the default (None, a non-numeric string, a numeric string it coerced with
    int()). Now that recovery.Bounds is bounds.Bounds, the same inputs raise ValueError instead: bounds.Bounds's
    own from_budgets treats a bad value as a config typo that must stop the project at load time, not a value to
    quietly ignore (see bounds.py's _bound_int docstring). This is the one behaviour difference Problem 1 asked to
    pin: the strict behaviour wins, because nothing in this file's own tests or in process_failures depended on
    the old silent-default-on-bad-value behaviour for a real reason (only this test asserted it, and it asserted
    it as coincidence of the old implementation, not as a requirement)."""
    with pytest.raises(ValueError):
        Bounds.from_budgets({"attempts_per_card": None})           # used to default to 3
    with pytest.raises(ValueError):
        Bounds.from_budgets({"fix_cards_per_task": "two"})          # used to default to 2
    with pytest.raises(ValueError):
        Bounds.from_budgets({"replans_per_project": "4"})           # used to be coerced by int() to 4
    with pytest.raises(ValueError):
        Bounds.from_budgets({"attempts_per_card": True})            # a bool is refused although bool is an int
    with pytest.raises(ValueError):
        Bounds.from_budgets({"attempts_per_card": -1})               # a negative value is refused too


@pytest.mark.parametrize("counters, expected", [
    ({}, None),
    ({"review_rounds": 2}, None),
    ({"review_rounds": 3}, "review_rounds"),
    ({"review_rounds": 9}, "review_rounds"),
    ({"fix_cards": 1}, None),
    ({"fix_cards": 2}, "fix_cards"),
    ({"capability_failures": 2}, None),
    ({"capability_failures": 3}, "attempts"),
    ({"infra_failures": 50}, None),      # infrastructure failures are not a lineage budget
    ({"requests": 100_000}, None),       # requests are counted but section 9.3 sets no bound for them
    ({"replans": 9}, None),
    ({"review_rounds": 3, "fix_cards": 2, "capability_failures": 3}, "review_rounds"),
    ({"fix_cards": 2, "capability_failures": 3}, "fix_cards"),
])
def test_exhausted_names_the_first_budget_that_has_run_out(counters, expected):
    assert recovery.exhausted(_lineage(**counters), Bounds()) == expected


def test_exhausted_uses_the_configured_bounds():
    bounds = Bounds(attempts_per_card=5, review_rounds_per_task=1, fix_cards_per_task=4)
    assert recovery.exhausted(_lineage(capability_failures=4), bounds) is None
    assert recovery.exhausted(_lineage(capability_failures=5), bounds) == "attempts"
    assert recovery.exhausted(_lineage(review_rounds=1), bounds) == "review_rounds"
    assert recovery.exhausted(_lineage(fix_cards=3), bounds) is None
    assert recovery.exhausted(_lineage(fix_cards=4), bounds) == "fix_cards"


# ---------------------------------------------------------------------------------------------
# decide and escalation
# ---------------------------------------------------------------------------------------------


def _decide(kind, **counters):
    return recovery.decide(kind, _lineage(**counters), Bounds())


def test_a_rate_limit_is_left_to_hermes():
    decision = _decide(FailureKind.RATE_LIMIT)
    assert decision.action == "none"
    assert "Retry-After" in decision.reason and "not rotated" in decision.reason


def test_a_quota_failure_parks_the_card_until_the_reset():
    decision = _decide(FailureKind.QUOTA)
    assert decision.action == "park"
    assert "the next UTC midnight" in decision.reason
    custom = recovery.decide(FailureKind.QUOTA, _lineage(), Bounds(), provider_reset_text="00:00 UTC on 2026-09-20")
    assert "00:00 UTC on 2026-09-20" in custom.reason and "midnight" not in custom.reason


@pytest.mark.parametrize("failures, backoff", [(0, 30), (1, 30), (2, 60)])
def test_an_infrastructure_failure_resumes_after_a_backoff_that_doubles(failures, backoff):
    decision = _decide(FailureKind.INFRASTRUCTURE, infra_failures=failures)
    assert decision.action == "resume"
    assert decision.backoff_seconds == backoff
    assert f"{backoff} s backoff" in decision.reason
    assert decision.model is None and decision.provider is None


def test_the_backoff_doubles_per_prior_failure_and_stops_at_900_seconds():
    assert [recovery.backoff_seconds(n) for n in range(1, 10)] == [30, 60, 120, 240, 480, 900, 900, 900, 900]
    assert recovery.backoff_seconds(10 ** 9) == 900        # a corrupt counter cannot overflow the shift
    bounds = Bounds(attempts_per_card=50)
    got = [recovery.decide(FailureKind.INFRASTRUCTURE, _lineage(infra_failures=n), bounds).backoff_seconds
           for n in range(1, 9)]
    assert got == [30, 60, 120, 240, 480, 900, 900, 900]


def test_infrastructure_failures_escalate_to_the_user_once_attempts_per_card_is_reached():
    assert _decide(FailureKind.INFRASTRUCTURE, infra_failures=2).action == "resume"
    spent = _decide(FailureKind.INFRASTRUCTURE, infra_failures=3)
    assert spent.action == "block_for_user" and spent.backoff_seconds == 0
    assert spent.reason.endswith("?")
    assert recovery.decide(FailureKind.INFRASTRUCTURE, _lineage(infra_failures=3),
                           Bounds(attempts_per_card=4)).action == "resume"


def test_infrastructure_failures_do_not_care_about_the_other_lineage_budgets():
    """An infrastructure failure says nothing about the task: a spent review budget must not turn it into a re-plan."""
    decision = _decide(FailureKind.INFRASTRUCTURE, infra_failures=1, review_rounds=3, fix_cards=2)
    assert decision.action == "resume"


def test_an_auth_failure_marks_the_credential_unhealthy_and_asks_the_user():
    decision = _decide(FailureKind.AUTH)
    assert decision.action == "mark_credential_unhealthy"
    assert "credential" in decision.reason and decision.reason.endswith("?")


def test_a_policy_failure_blocks_for_the_user_and_never_relaxes_the_data_class():
    decision = _decide(FailureKind.POLICY)
    assert decision.action == "block_for_user"
    assert "never relaxes the data class" in decision.reason and decision.reason.endswith("?")


@pytest.mark.parametrize("kind", [FailureKind.CONTEXT, FailureKind.TOOL_CALLING])
def test_a_context_or_tool_calling_failure_says_the_model_must_be_rejected_for_agent_roles(kind):
    decision = _decide(kind)
    assert decision.action == "block_for_user"
    assert "rejected for agent roles" in decision.reason and decision.reason.endswith("?")


@pytest.mark.parametrize("kind", [FailureKind.CAPABILITY, FailureKind.RUNTIME])
def test_the_first_capability_failure_is_a_fresh_attempt_and_the_second_switches_model(kind):
    first = _decide(kind, capability_failures=1)
    assert first.action == "fresh_attempt"
    assert "fresh worktree" in first.reason and "failure bundle" in first.reason
    second = _decide(kind, capability_failures=2)
    assert second.action == "switch_model"
    assert "next model" in second.reason
    assert "fresh worktree" in second.reason and "failure bundle" in second.reason   # the switch restarts too (19.2)
    assert first.model is None and second.model is None       # decide() has no model list: process_failures fills it in


def test_a_capability_failure_that_was_not_counted_yet_is_treated_as_the_first():
    assert _decide(FailureKind.CAPABILITY).action == "fresh_attempt"


def test_a_runtime_overrun_is_described_as_one_and_counts_like_a_capability_failure():
    assert "runtime" in _decide(FailureKind.RUNTIME, capability_failures=1).reason
    assert "runtime" not in _decide(FailureKind.CAPABILITY, capability_failures=1).reason


@pytest.mark.parametrize("kind", [FailureKind.CAPABILITY, FailureKind.RUNTIME])
def test_the_third_capability_failure_exhausts_the_attempts_and_replans_once_then_blocks(kind):
    replan = _decide(kind, capability_failures=3)
    assert replan.action == "replan" and "re-plan the task once" in replan.reason
    block = _decide(kind, capability_failures=3, replans=1)
    assert block.action == "block_for_user" and block.reason.endswith("?")
    assert _decide(kind, capability_failures=7, replans=2).action == "block_for_user"


@pytest.mark.parametrize("counters", [{"review_rounds": 3}, {"fix_cards": 2}])
def test_any_spent_lineage_budget_escalates_even_on_the_first_capability_failure(counters):
    assert _decide(FailureKind.CAPABILITY, capability_failures=1, **counters).action == "replan"
    assert _decide(FailureKind.CAPABILITY, capability_failures=1, replans=1, **counters).action == "block_for_user"


def test_escalation_is_none_until_a_budget_runs_out_then_replan_then_a_question():
    assert recovery.escalation(_lineage(review_rounds=2, fix_cards=1, capability_failures=2), Bounds()) is None
    replan = recovery.escalation(_lineage(review_rounds=3), Bounds())
    assert replan.action == "replan" and "review-round" in replan.reason
    block = recovery.escalation(_lineage(fix_cards=2, replans=1), Bounds())
    assert block.action == "block_for_user" and "fix-card" in block.reason and block.reason.endswith("?")
    assert "attempt" in recovery.escalation(_lineage(capability_failures=3), Bounds()).reason


@pytest.mark.parametrize("unknowns, action", [(0, "none"), (1, "none"), (2, "none"), (3, "block_for_user"),
                                              (9, "block_for_user")])
def test_an_unknown_failure_waits_for_attempts_per_card_unknowns_in_a_row(unknowns, action):
    decision = recovery.decide(FailureKind.UNKNOWN, _lineage(), Bounds(), consecutive_unknown=unknowns)
    assert decision.action == action
    assert (decision.reason.endswith("?")) == (action == "block_for_user")


def test_the_unknown_patience_follows_the_configured_attempts_per_card():
    assert recovery.decide(FailureKind.UNKNOWN, _lineage(), Bounds(attempts_per_card=2),
                           consecutive_unknown=2).action == "block_for_user"
    assert recovery.decide(FailureKind.UNKNOWN, _lineage(), Bounds(attempts_per_card=5),
                           consecutive_unknown=4).action == "none"


def test_a_run_that_ended_normally_needs_no_recovery():
    assert _decide(FailureKind.NONE).action == "none"


def test_decide_accepts_the_kind_as_a_plain_string_and_refuses_an_invented_one():
    assert recovery.decide("quota", _lineage(), Bounds()).action == "park"
    with pytest.raises(ValueError):
        recovery.decide("banana", _lineage(), Bounds())


def test_every_decision_carries_a_readable_ascii_reason_and_the_questions_end_in_one():
    questions = {"block_for_user", "mark_credential_unhealthy"}
    for kind in FailureKind:
        for counters in ({}, {"capability_failures": 1}, {"capability_failures": 2}, {"capability_failures": 3},
                         {"infra_failures": 1}, {"infra_failures": 3}):
            decision = _decide(kind, **counters)
            assert decision.reason.strip() and decision.reason.isascii(), (kind, counters)
            assert decision.action in recovery.ACTIONS
            if decision.action in questions:
                assert decision.reason.endswith("?"), (kind, counters)


def test_decide_does_not_change_its_inputs():
    lineage, bounds = _lineage(capability_failures=2, infra_failures=1), Bounds()
    recovery.decide(FailureKind.CAPABILITY, lineage, bounds)
    assert lineage == _lineage(capability_failures=2, infra_failures=1) and bounds == Bounds()


def test_a_decision_is_frozen_and_refuses_an_unknown_action():
    decision = Decision("resume", "because", backoff_seconds=30)
    assert (decision.model, decision.provider, decision.task_key, decision.card_id) == (None, None, None, None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.action = "none"
    with pytest.raises(ValueError):
        Decision("restart_the_world", "no")
    assert {"none", "resume", "fresh_attempt", "switch_model", "park", "replan", "block_for_user",
            "mark_credential_unhealthy"} == set(recovery.ACTIONS)


# ---------------------------------------------------------------------------------------------
# next_model and unhealthy_credentials
# ---------------------------------------------------------------------------------------------


def test_next_model_picks_the_first_other_candidate_of_the_class_in_file_order():
    assert recovery.next_model(MODELS, "coder", "xkiro", CODER_MODEL) == ("xkiro", CANDIDATE_1)


def test_next_model_offers_the_pinned_model_first_even_when_it_comes_later_in_the_file():
    # CANDIDATE_1 is listed before the pinned coder row, but the pinned one is ordered first.
    assert recovery.next_model(MODELS, "coder", "xkiro", CANDIDATE_1) == ("xkiro", CODER_MODEL)
    assert recovery.next_model(MODELS, "coder", "xkiro", "some/unlisted-model") == ("xkiro", CODER_MODEL)
    assert recovery.next_model(MODELS, "coder", None, None) == ("xkiro", CODER_MODEL)


def test_next_model_never_returns_the_current_model_and_matches_provider_and_model_together():
    models = {"models": [
        {"provider": "a", "model": "m1", "role_class": "coder", "pinned": True},
        {"provider": "b", "model": "m1", "role_class": "coder_candidate"},
    ]}
    assert recovery.next_model(models, "coder", "a", "m1") == ("b", "m1")     # same model name, other provider
    assert recovery.next_model(models, "coder", "b", "m1") == ("a", "m1")


def test_next_model_takes_the_class_and_its_candidates_and_nothing_else():
    assert recovery.next_model(MODELS, "reviewer", "openrouter", REVIEWER_MODEL) is None
    lead = recovery.next_model(MODELS, "lead", None, None)
    assert lead == ("xkiro", "qwen/qwen3.8-max:free")
    models = {"models": [
        {"provider": "x", "model": "unfunded", "role_class": "lead_unfunded", "pinned": True},
        {"provider": "x", "model": "retired", "role_class": "coder_retired", "pinned": True},
        {"provider": "x", "model": "reviewer-model", "role_class": "reviewer", "pinned": True},
    ]}
    assert recovery.next_model(models, "coder", None, None) is None
    assert recovery.next_model(models, "lead", None, None) is None


def test_next_model_is_none_when_there_is_no_other_candidate():
    only = {"models": [{"provider": "x", "model": "m", "role_class": "coder", "pinned": True}]}
    assert recovery.next_model(only, "coder", "x", "m") is None
    assert recovery.next_model({"models": []}, "coder", None, None) is None
    assert recovery.next_model({}, "coder", None, None) is None


def test_next_model_ignores_a_row_that_names_no_provider_or_no_model():
    models = {"models": [
        {"model": "no-provider", "role_class": "coder", "pinned": True},
        {"provider": "x", "role_class": "coder_candidate"},
        {"provider": "x", "model": "fine", "role_class": "coder_candidate"},
    ]}
    assert recovery.next_model(models, "coder", None, None) == ("x", "fine")


def test_next_model_skips_an_unhealthy_model_and_an_unhealthy_provider():
    assert recovery.next_model(MODELS, "coder", "xkiro", CODER_MODEL, unhealthy={("xkiro", CANDIDATE_1)}) \
        == ("xkiro", CANDIDATE_2)
    assert recovery.next_model(MODELS, "coder", "xkiro", CODER_MODEL, unhealthy={("xkiro", "*")}) is None
    assert recovery.next_model(MODELS, "coder", "xkiro", CODER_MODEL, unhealthy={("openrouter", "*")}) \
        == ("xkiro", CANDIDATE_1)
    assert recovery.next_model(MODELS, "coder", "xkiro", CODER_MODEL, unhealthy=set()) == ("xkiro", CANDIDATE_1)


def test_next_model_skips_a_model_whose_smoke_test_failed_but_accepts_one_with_no_result():
    models = {"models": [
        {"provider": "x", "model": "current", "role_class": "coder", "pinned": True},
        {"provider": "x", "model": "failed", "role_class": "coder_candidate", "smoke_test_result": "fail"},
        {"provider": "x", "model": "failed-too", "role_class": "coder_candidate", "smoke_test": "FAIL"},
        {"provider": "x", "model": "passed", "role_class": "coder_candidate", "smoke_test_result": "pass"},
    ]}
    assert recovery.next_model(models, "coder", "x", "current") == ("x", "passed")
    models["models"][3]["smoke_test_result"] = None
    assert recovery.next_model(models, "coder", "x", "current") == ("x", "passed")


def test_next_model_skips_a_model_that_says_it_cannot_serve_an_agent_role():
    models = {"models": [
        {"provider": "x", "model": "current", "role_class": "coder", "pinned": True},
        {"provider": "x", "model": "no-tools", "role_class": "coder_candidate", "tool_calling": False},
        {"provider": "x", "model": "tiny", "role_class": "coder_candidate", "context_length": 16_000},
        {"provider": "x", "model": "ok", "role_class": "coder_candidate", "tool_calling": True,
         "context_length": 64_000},
    ]}
    assert recovery.next_model(models, "coder", "x", "current") == ("x", "ok")


def test_next_model_never_leaves_the_data_class_when_it_is_given_one():
    assert recovery.next_model(MODELS, "coder", "xkiro", CODER_MODEL, data_class="public") == ("xkiro", CANDIDATE_1)
    # xkiro's declared policy (router_ztr_upstream_varies) does not clear "private": nothing to switch to.
    assert recovery.next_model(MODELS, "coder", "xkiro", CODER_MODEL, data_class="private") is None
    models = {"providers": {"x": {"data_policy": "unknown"}}, "models": [
        {"provider": "x", "model": "current", "role_class": "coder", "pinned": True},
        {"provider": "x", "model": "cleared", "role_class": "coder_candidate", "data_policy": "no_training",
         "data_policy_verified_at": "2026-09-22"},
    ]}
    assert recovery.next_model(models, "coder", "x", "current", data_class="private") == ("x", "cleared")
    assert recovery.next_model(models, "coder", "x", "current", data_class="confidential") is None
    assert recovery.next_model(models, "coder", "x", "current", data_class="not-a-class") is None

    # ASES-PRV-04 (round 7): a compatible policy string alone is no longer enough for private/confidential --
    # without a recorded data_policy_verified_at, next_model must treat the candidate as unsafe, same as an
    # incompatible policy, never silently skip the verification requirement.
    unverified = {"providers": {"x": {"data_policy": "unknown"}}, "models": [
        {"provider": "x", "model": "current", "role_class": "coder", "pinned": True},
        {"provider": "x", "model": "cleared", "role_class": "coder_candidate", "data_policy": "no_training"},
    ]}
    assert recovery.next_model(unverified, "coder", "x", "current", data_class="private") is None


def test_unhealthy_credentials_reads_the_event_log_and_a_restore_clears_a_provider(conn):
    assert recovery.unhealthy_credentials(conn) == set()
    events.record(conn, "credential_unhealthy", {"provider": "xkiro", "model": CODER_MODEL})
    events.record(conn, "credential_unhealthy", {"provider": "openrouter"})
    events.record(conn, "credential_unhealthy", {"model": "no-provider"})            # nothing to mark
    events.record(conn, "recovery_decision", {"provider": "ignored"})                # other events are not read
    assert recovery.unhealthy_credentials(conn) == {("xkiro", "*"), ("openrouter", "*")}

    events.record(conn, "credential_restored", {"provider": "xkiro"})
    assert recovery.unhealthy_credentials(conn) == {("openrouter", "*")}
    events.record(conn, "credential_unhealthy", {"provider": "xkiro"})               # a later failure marks it again
    assert recovery.unhealthy_credentials(conn) == {("xkiro", "*"), ("openrouter", "*")}


def test_unhealthy_credentials_ignores_a_payload_that_is_not_json(conn):
    conn.execute("INSERT INTO events (ts, kind, payload) VALUES ('t', 'credential_unhealthy', 'not json')")
    conn.execute("INSERT INTO events (ts, kind, payload) VALUES ('t', 'credential_unhealthy', '[1, 2]')")
    assert recovery.unhealthy_credentials(conn) == set()


# ---------------------------------------------------------------------------------------------
# failure_bundle
# ---------------------------------------------------------------------------------------------

SECTIONS = ["Acceptance criteria", "What failed", "Diff so far", "Gate output", "Reviewer findings"]


def _section(bundle, title):
    """The text of one section of a bundle."""
    parts = bundle.split(f"## {title}\n", 1)[1]
    return parts.split("\n\n## ", 1)[0]


def test_the_bundle_has_all_five_sections_in_order_and_says_when_one_is_empty():
    card = _card("w1", [_run(3, "crashed", CRASH_TEXT)])
    bundle = recovery.failure_bundle(card, criteria=["it exists", "it is tested"])
    positions = [bundle.index(f"## {title}") for title in SECTIONS]
    assert positions == sorted(positions)
    assert bundle.startswith("Failure bundle for card w1")
    assert _section(bundle, "Acceptance criteria") == "- it exists\n- it is tested"
    for title in ("Diff so far", "Gate output", "Reviewer findings"):
        assert _section(bundle, title).strip() == "(none provided)"


def test_the_bundle_carries_each_input_in_its_own_section():
    bundle = recovery.failure_bundle(
        _card("w1", [_run(3, "crashed", CRASH_TEXT)]), criteria="one criterion as a string",
        diff_text="+ added line", gate_output="FAILED test_x", reviewer_findings="missing a test",
    )
    assert _section(bundle, "Acceptance criteria") == "one criterion as a string"
    assert _section(bundle, "Diff so far").strip() == "+ added line"
    assert _section(bundle, "Gate output").strip() == "FAILED test_x"
    assert _section(bundle, "Reviewer findings").strip() == "missing a test"


def test_what_failed_names_the_last_failed_run_not_the_last_run():
    card = _card("w1", [
        _run(1, "crashed", "old crash", summary="old summary"),
        _run(2, "timed_out", "elapsed 60s > limit 60s"),
        _run(3, "changes_requested", summary="please add a test"),
    ])
    failed = _section(recovery.failure_bundle(card, criteria=[]), "What failed")
    assert "Run: 2 (profile coder-1)" in failed
    assert "Outcome: timed_out" in failed and "elapsed 60s > limit 60s" in failed
    assert "old crash" not in failed and "please add a test" not in failed


def test_what_failed_falls_back_to_the_last_ended_run_and_then_to_a_plain_sentence():
    ended = _card("w1", [_run(1, "completed", summary="all done")])
    assert "Outcome: completed" in _section(recovery.failure_bundle(ended, criteria=[]), "What failed")
    empty = _section(recovery.failure_bundle(_card("w1", []), criteria=[]), "What failed")
    assert empty == "No run of this card has ended yet."
    open_run = _card("w1", [_run(1, None, None)])
    assert _section(recovery.failure_bundle(open_run, criteria=[]), "What failed") \
        == "No run of this card has ended yet."


@pytest.mark.parametrize("title, argument, limit", [
    ("Diff so far", "diff_text", 6000), ("Gate output", "gate_output", 3000),
    ("Reviewer findings", "reviewer_findings", 3000),
])
def test_a_long_section_is_cut_at_its_limit_with_a_marker(title, argument, limit):
    card = _card("w1", [_run(1, "crashed", CRASH_TEXT)])
    text = "a" * limit + "b" * 250
    bundle = recovery.failure_bundle(card, criteria=[], **{argument: text})
    body = _section(bundle, title)
    assert body.startswith("a" * limit + "\n[truncated: 250 more characters omitted]")
    assert "b" * 10 not in body

    exact = recovery.failure_bundle(card, criteria=[], **{argument: "a" * limit})
    assert "truncated" not in exact and "a" * limit in exact
    over_by_one = recovery.failure_bundle(card, criteria=[], **{argument: "a" * (limit + 1)})
    assert "[truncated: 1 more characters omitted]" in over_by_one


def test_a_secret_shaped_value_is_never_copied_into_the_bundle():
    secret, token = "sk-abcdefghij0123456789", "ghp_abcdefghij0123456789"
    card = _card("w1", [_run(1, "crashed", f"Invalid API key {secret}", summary=f"used {token}")])
    bundle = recovery.failure_bundle(
        card, criteria=[f"never print {secret}"], diff_text=f"+KEY = '{secret}'", gate_output=f"env {token}",
        reviewer_findings=f"leaked {secret} in the diff",
    )
    assert secret not in bundle and token not in bundle
    assert bundle.count("[redacted]") >= 6


def test_the_bundle_is_ascii_even_when_its_inputs_are_not():
    card = _card("w1", [_run(1, "crashed", f"ran into {ARROW} an arrow", summary=f"caf{E_ACUTE}")])
    bundle = recovery.failure_bundle(
        card, criteria=[f"show {CURLY_OPEN}quotes{CURLY_CLOSE}"], diff_text=f"+ {CHECK} done",
        gate_output=f"{CROSS} failed", reviewer_findings=f"{DASH} no",
    )
    assert bundle.isascii()
    assert ESCAPED_ARROW in bundle and f"caf{ESCAPED_E_ACUTE}" in bundle
    assert chr(0x2014) not in bundle and ARROW not in bundle


def test_the_bundle_does_not_change_the_card_it_describes():
    card = _card("w1", [_run(1, "crashed", CRASH_TEXT)])
    before = copy.deepcopy(card)
    recovery.failure_bundle(card, criteria=("a",), diff_text="d")
    assert card == before


def test_a_very_long_run_error_is_cut_too():
    card = _card("w1", [_run(1, "crashed", "x" * 5000)])
    failed = _section(recovery.failure_bundle(card, criteria=[]), "What failed")
    assert "[truncated: 3000 more characters omitted]" in failed


# ---------------------------------------------------------------------------------------------
# process_failures
# ---------------------------------------------------------------------------------------------


def test_a_crashed_card_is_resumed_only_once_the_backoff_has_passed(conn, board, tmp_path):
    _seed_task(conn, "T1", "w1")
    board.cards["w1"] = _card("w1", [_run(7, "crashed", CRASH_TEXT)])

    assert _pass(conn, tmp_path, now=ENDED + 29) == []                # 30 s backoff for the first infra failure
    assert board.mutations == []
    assert _events_of(conn, "recovery_decision") == []
    assert _lineage_row(conn, "T1") is None                          # nothing counted while it waits

    decisions = _pass(conn, tmp_path, now=ENDED + 30)                 # the whole backoff has elapsed
    assert [d.action for d in decisions] == ["resume"]
    decision = decisions[0]
    assert (decision.task_key, decision.card_id, decision.run_id) == ("T1", "w1", 7)
    assert decision.failure_kind is FailureKind.INFRASTRUCTURE and decision.backoff_seconds == 30
    assert board.mutations == [("unblock", "w1")]
    assert _lineage_row(conn, "T1")["infra_failures"] == 1
    (record,) = _events_of(conn, "recovery_decision")
    assert (record["action"], record["backoff_seconds"], record["applied"], record["kind"]) == \
        ("resume", 30, True, "infrastructure")


def test_the_backoff_doubles_with_each_infrastructure_failure_of_the_task(conn, board, tmp_path):
    _seed_task(conn, "T1", "w1")
    recovery.bump(conn, "p1", "T1", "infra_failures")               # one failure was already counted
    board.cards["w1"] = _card("w1", [_run(7, "crashed", CRASH_TEXT)])

    assert _pass(conn, tmp_path, now=ENDED + 59) == []
    assert [d.backoff_seconds for d in _pass(conn, tmp_path, now=ENDED + 60)] == [60]
    assert _lineage_row(conn, "T1")["infra_failures"] == 2


def test_a_run_whose_end_time_is_missing_or_a_string_still_resumes(conn, board, tmp_path):
    _seed_task(conn, "T1", "w1")
    _seed_task(conn, "T2", "w2")
    board.cards["w1"] = _card("w1", [_run(7, "crashed", CRASH_TEXT, ended_at=str(ENDED))])
    board.cards["w2"] = _card("w2", [_run(8, "crashed", CRASH_TEXT, ended_at=None)])

    # T2 has no end time, so there is nothing to wait from. T1's end time arrives as a string and is still honoured.
    assert [d.task_key for d in _pass(conn, tmp_path, now=ENDED + 29)] == ["T2"]
    assert board.mutations == [("unblock", "w2")]
    assert [d.task_key for d in _pass(conn, tmp_path, now=ENDED + 30)] == ["T1"]


def test_now_may_be_a_datetime_or_an_iso_string_and_defaults_to_the_clock(conn, board, tmp_path, monkeypatch):
    _seed_task(conn, "T1", "w1")
    board.cards["w1"] = _card("w1", [_run(7, "crashed", CRASH_TEXT)])
    early = datetime.fromtimestamp(ENDED + 5, tz=timezone.utc)
    assert _pass(conn, tmp_path, now=early) == []
    assert _pass(conn, tmp_path, now=early.isoformat()) == []
    clock = types.SimpleNamespace(time=lambda: ENDED + 5)             # only this module's clock, not the real one
    monkeypatch.setattr(recovery, "time", clock)
    assert _pass(conn, tmp_path, now=None) == []
    clock.time = lambda: ENDED + 500
    assert [d.action for d in _pass(conn, tmp_path, now=None)] == ["resume"]


def test_a_naive_datetime_now_is_taken_as_utc(conn, board, tmp_path):
    _seed_task(conn, "T1", "w1")
    board.cards["w1"] = _card("w1", [_run(7, "crashed", CRASH_TEXT)])
    early = datetime.fromtimestamp(ENDED + 5, tz=timezone.utc).replace(tzinfo=None)
    late = datetime.fromtimestamp(ENDED + 40, tz=timezone.utc).replace(tzinfo=None)
    assert _pass(conn, tmp_path, now=early) == []
    assert [d.action for d in _pass(conn, tmp_path, now=late)] == ["resume"]


@pytest.mark.parametrize("now", ["tomorrow", "", "nan", "inf", True, float("nan"), float("inf"), [1], {}])
def test_a_now_that_is_not_a_time_is_refused(conn, tmp_path, now):
    with pytest.raises(ValueError):
        _pass(conn, tmp_path, now=now)


def test_infrastructure_failures_escalate_to_a_question_at_attempts_per_card(conn, board, tmp_path):
    _seed_task(conn, "T1", "w1")
    conn.execute("INSERT INTO lineage (project, task_key, infra_failures, updated_at) VALUES ('p1', 'T1', 2, 'then')")
    board.cards["w1"] = _card("w1", [_run(7, "crashed", CRASH_TEXT)])

    decisions = _pass(conn, tmp_path, now=ENDED)                      # no waiting: this is a question, not a resume
    assert [d.action for d in decisions] == ["block_for_user"]
    assert [(m[0], m[1], m[3]) for m in board.mutations] == [("comment", "w1", "ases")]
    (reason,) = _asked(board)
    assert reason.startswith("T1, last error: pid 4242 exited with code 1.") and reason.endswith("?")
    assert _lineage_row(conn, "T1")["infra_failures"] == 3


def test_a_quota_failure_parks_the_card_and_counts_nothing(conn, board, tmp_path):
    _seed_task(conn, "T1", "w1")
    board.cards["w1"] = _card("w1", [_run(7, "crashed", QUOTA_TEXT)])

    decisions = _pass(conn, tmp_path)

    assert [d.action for d in decisions] == ["park"]
    ((verb, card_id, reason),) = board.mutations
    assert (verb, card_id) == ("schedule", "w1") and "the next UTC midnight" in reason
    assert _lineage_row(conn, "T1") is None
    (record,) = _events_of(conn, "recovery_decision")
    assert (record["action"], record["applied"], record["kind"]) == ("park", True, "quota")


def test_a_policy_failure_asks_the_user_on_the_card_and_counts_nothing(conn, board, tmp_path):
    _seed_task(conn, "T1", "w1")
    board.cards["w1"] = _card("w1", [_run(7, "crashed", POLICY_TEXT)])

    decisions = _pass(conn, tmp_path)

    assert [d.action for d in decisions] == ["block_for_user"]
    assert [(m[0], m[1]) for m in board.mutations] == [("comment", "w1")]
    (reason,) = _asked(board)
    assert reason.startswith("T1, last error: HTTP 404: No endpoints found") and reason.endswith("?")
    assert "never relaxes the data class" in reason
    assert reason.isascii() and "\n" not in reason
    assert _lineage_row(conn, "T1") is None


def test_a_card_that_already_carries_the_question_is_not_asked_again(conn, board, tmp_path):
    _seed_task(conn, "T1", "w1")
    board.cards["w1"] = _card("w1", [_run(7, "crashed", POLICY_TEXT)])
    _pass(conn, tmp_path)
    (question,) = _asked(board)

    def rerun(**card_changes):
        """Decide the same failure again on a fresh database, against a card carrying `card_changes`."""
        board.mutations.clear()
        conn.execute("DELETE FROM events")
        conn.execute("DELETE FROM lineage")
        board.cards["w1"] = _card("w1", [_run(7, "crashed", POLICY_TEXT)], **card_changes)
        return _pass(conn, tmp_path)

    blocked_event = {"kind": "blocked", "payload": {"reason": question}, "created_at": 5, "run_id": None}
    posted = {"author": "ases", "body": f"ASES QUESTION: {question}", "created_at": 5}
    for changes in ({"_events": [blocked_event]},
                    {"_events": [{**blocked_event, "payload": json.dumps({"reason": question})}]},
                    {"_comments": [posted]},
                    {"_comments": [{**posted, "body": f"ASES QUESTION:   {question}\n"}]}):
        assert [d.action for d in rerun(**changes)] == ["block_for_user"]
        assert board.mutations == []                                # not asked again
        assert [e["action"] for e in _events_of(conn, "recovery_decision")] == ["block_for_user"]   # but decided
        assert _events_of(conn, "question_asked") == []             # and nothing is logged about a question not asked

    # A different question on the card does not count as this one. Neither do the same words from somebody else, or
    # a "BLOCKED:" comment that a refused block left behind: that is not the question ASES puts to the person.
    other = {"kind": "blocked", "payload": {"reason": "something else?"}, "created_at": 5, "run_id": None}
    for changes in ({"_events": [other]},
                    {"_comments": [{**posted, "author": "coder-1"}]},
                    {"_comments": [{"author": "default", "body": f"BLOCKED: {question}", "created_at": 5}]}):
        assert [d.action for d in rerun(**changes)] == ["block_for_user"]
        assert _asked(board) == [question]


def test_events_and_comments_that_are_not_records_do_not_hide_a_question(conn, board, tmp_path):
    _seed_task(conn, "T1", "w1")
    board.cards["w1"] = _card(
        "w1", [_run(7, "crashed", POLICY_TEXT)],
        _events=["garbage", None, {"kind": "blocked", "payload": "not json", "created_at": 1, "run_id": None}],
        _comments=["garbage", None, {"author": "default", "body": None, "created_at": 2}],
    )
    assert [d.action for d in _pass(conn, tmp_path)] == ["block_for_user"]
    assert [m[0] for m in board.mutations] == ["comment"]


def test_an_answered_question_that_comes_back_is_asked_again(conn, board, tmp_path):
    _seed_task(conn, "T1", "w1")
    board.cards["w1"] = _card("w1", [_run(7, "crashed", POLICY_TEXT)])
    _pass(conn, tmp_path)
    (question,) = _asked(board)
    blocked = {"kind": "blocked", "payload": {"reason": question}, "created_at": 5, "run_id": None}
    unblocked = {"kind": "unblocked", "payload": None, "created_at": 6, "run_id": None}
    posted = {"author": "ases", "body": f"ASES QUESTION: {question}", "created_at": 5}

    for cards in ({"_events": [blocked, unblocked]},
                  {"_comments": [posted, {"author": "user", "body": "ANSWER: use another provider", "created_at": 6}]},
                  {"_comments": [posted, {"author": "default", "body": "UNBLOCK: use another provider",
                                          "created_at": 6}]}):
        board.mutations.clear()
        conn.execute("DELETE FROM events")
        board.cards["w1"] = _card("w1", [_run(8, "crashed", POLICY_TEXT)], **cards)
        assert [d.action for d in _pass(conn, tmp_path)] == ["block_for_user"]
        assert _asked(board) == [question]


def test_a_blocked_card_is_never_blocked_again_the_question_is_a_comment_and_the_pass_converges(
    conn, board, tmp_path,
):
    """Checked against the installed Hermes: block_task accepts only a running or ready card, so `block` on a card that
    is already blocked adds its "BLOCKED: <reason>" comment and then exits 1. Recovery only ever acts on a blocked
    card, so it must not call block at all: the question is an "ASES QUESTION:" comment, the first pass already
    records its decision, and no pass ever piles up comments or errors."""
    board.block_mode = "refuse"          # a call to block would fail here, as it does on the real thing
    _seed_task(conn, "T1", "w1")
    board.cards["w1"] = _card("w1", [_run(7, "crashed", POLICY_TEXT)])

    decisions = _pass(conn, tmp_path)

    assert [d.action for d in decisions] == ["block_for_user"]
    assert [(m[0], m[1], m[3]) for m in board.mutations] == [("comment", "w1", "ases")]
    assert _events_of(conn, "recovery_error") == []
    assert len(board.cards["w1"]["_comments"]) == 1

    assert _pass(conn, tmp_path) == [] and _pass(conn, tmp_path) == []      # the run is settled
    assert [m[0] for m in board.mutations] == ["comment"]
    assert len(_events_of(conn, "recovery_decision")) == 1


@pytest.mark.parametrize("text, action", [
    (POLICY_TEXT, "block_for_user"), (AUTH_TEXT, "mark_credential_unhealthy"),
    ("HTTP 400: maximum context length is 32768 tokens", "block_for_user"),
    ("model produced an invalid tool call", "block_for_user"),
])
def test_recovery_never_calls_kanban_block_and_asks_through_ask_user(conn, board, tmp_path, monkeypatch, text, action):
    """Whatever the question is about, the pass reaches the person through questions.ask_user (a comment on the
    blocked card), so a `block` call, which Hermes refuses on a blocked card, never happens."""
    def forbidden(*args, **kwargs):
        raise AssertionError("recovery called hermes.kanban_block")

    monkeypatch.setattr(hermes, "kanban_block", forbidden)
    seen = []
    real_ask = questions.ask_user
    monkeypatch.setattr(questions, "ask_user", lambda *a, **k: seen.append((a, k)) or real_ask(*a, **k))
    _seed_task(conn, "T1", "w1")
    board.cards["w1"] = _card("w1", [_run(7, "crashed", text)])

    assert [d.action for d in _pass(conn, tmp_path)] == [action]

    ((args, kwargs),) = seen
    assert args[0] == "b" and args[1]["id"] == "w1" and args[2].endswith("?") and kwargs == {"conn": conn}
    assert [m[0] for m in board.mutations] == ["comment"]


def test_a_card_the_dispatcher_gave_up_on_is_asked_about_and_then_found_by_swarm_questions(conn, board, tmp_path):
    """The whole point of the change: Hermes writes a `gave_up` event and no `blocked` event for this card, and the
    question recovery puts on it must be one `swarm questions` and `swarm answer` can see and act on."""
    _seed_task(conn, "T1", "w1")
    board.cards["w1"] = _card("w1", [_run(7, "crashed", POLICY_TEXT)], _events=[_gave_up(3, "no endpoints")])
    assert questions.open_question(board.cards["w1"]).source == "gave_up"          # what a person would see before

    assert [d.action for d in _pass(conn, tmp_path)] == ["block_for_user"]

    (question,) = _asked(board)
    asked = questions.open_question(board.cards["w1"])
    assert (asked.source, asked.reason) == ("ases_comment", question)               # ASES's question is the newest
    assert "never relaxes the data class" in asked.reason

    assert _pass(conn, tmp_path) == []                                              # nothing more on later passes
    assert _asked(board) == [question]

    answered = questions.answer_question("b", "w1", "Use the paid provider.", conn=conn)   # and it can be answered
    assert (answered.source, answered.question) == ("ases_comment", question)
    assert board.cards["w1"]["status"] == "ready"
    assert questions.open_question(board.cards["w1"]) is None


def test_ask_user_failing_is_a_recovery_error_that_is_retried_and_counts_nothing(conn, board, tmp_path):
    """recovery_error is only for a failure ask_user could not recover from: here even the comment is refused."""
    _seed_task(conn, "T1", "w1")
    board.cards["w1"] = _card("w1", [_run(7, "crashed", POLICY_TEXT)])
    board.fail[("comment", "w1")] = hermes.HermesCommandError(["kanban", "comment", "w1"], 1, "database is locked")

    assert _pass(conn, tmp_path) == []
    (error,) = _events_of(conn, "recovery_error")
    assert (error["task_key"], error["card_id"], error["action"]) == ("T1", "w1", "block_for_user")
    assert "database is locked" in error["error"]
    assert _events_of(conn, "recovery_decision") == [] and _events_of(conn, "question_asked") == []

    assert _pass(conn, tmp_path) == [] and len(_events_of(conn, "recovery_error")) == 1     # said once, retried

    del board.fail[("comment", "w1")]
    assert [d.action for d in _pass(conn, tmp_path)] == ["block_for_user"]
    assert len(_asked(board)) == 1
    (asked_event,) = _events_of(conn, "question_asked")
    assert (asked_event["card_id"], asked_event["via"]) == ("w1", "commented")


def test_an_auth_failure_is_recorded_asked_to_the_user_and_the_provider_is_marked_unhealthy(conn, board, tmp_path):
    _seed_task(conn, "T1", "w1")
    board.cards["w1"] = _card("w1", [_run(7, "crashed", AUTH_TEXT)])

    decisions = _pass(conn, tmp_path)

    assert [d.action for d in decisions] == ["mark_credential_unhealthy"]
    assert (decisions[0].provider, decisions[0].model) == ("xkiro", CODER_MODEL)
    assert [(m[0], m[1]) for m in board.mutations] == [("comment", "w1")]
    (reason,) = _asked(board)
    assert reason.endswith("?") and "credential" in reason
    (unhealthy,) = _events_of(conn, "credential_unhealthy")
    assert (unhealthy["task_key"], unhealthy["provider"], unhealthy["model"], unhealthy["run_id"]) == \
        ("T1", "xkiro", CODER_MODEL, 7)
    assert recovery.unhealthy_credentials(conn) == {("xkiro", "*")}
    assert [e["action"] for e in _events_of(conn, "recovery_decision")] == ["mark_credential_unhealthy"]
    assert _lineage_row(conn, "T1") is None


def test_the_credential_is_only_marked_when_the_question_went_through(conn, board, tmp_path):
    _seed_task(conn, "T1", "w1")
    board.cards["w1"] = _card("w1", [_run(7, "crashed", AUTH_TEXT)])
    board.fail[("comment", "w1")] = hermes.HermesCommandError(["kanban", "comment", "w1"], 1, "database is locked")
    assert _pass(conn, tmp_path) == []
    assert recovery.unhealthy_credentials(conn) == set() and _events_of(conn, "credential_unhealthy") == []

    del board.fail[("comment", "w1")]
    assert [d.action for d in _pass(conn, tmp_path)] == ["mark_credential_unhealthy"]
    assert recovery.unhealthy_credentials(conn) == {("xkiro", "*")}


def test_the_auth_failure_of_a_switched_card_names_the_model_it_was_switched_to(conn, board, tmp_path):
    _seed_task(conn, "T1", "w1")
    _seed_task(conn, "T2", "w2")
    # The provider comes from the card's override when it has one, and from the model list when it has only a model.
    board.cards["w1"] = _card("w1", [_run(7, "crashed", AUTH_TEXT)], model_override=CANDIDATE_1)
    board.cards["w2"] = _card("w2", [_run(8, "crashed", AUTH_TEXT)], model_override="m", provider_override="elsewhere")
    decisions = _pass(conn, tmp_path)
    assert [(d.task_key, d.provider, d.model) for d in decisions] == [
        ("T1", "xkiro", CANDIDATE_1), ("T2", "elsewhere", "m"),
    ]
    assert recovery.unhealthy_credentials(conn) == {("xkiro", "*"), ("elsewhere", "*")}


def test_the_failing_provider_is_found_from_the_role_when_the_runs_profile_maps_to_no_role(conn, board, tmp_path):
    _seed_task(conn, "T1", "w1")
    board.cards["w1"] = _card("w1", [_run(7, "crashed", AUTH_TEXT, profile="a-profile-no-role-maps-to")])
    decisions = _pass(conn, tmp_path)
    assert (decisions[0].provider, decisions[0].model) == ("xkiro", CODER_MODEL)   # T1 is a coder task


def test_an_auth_failure_on_an_unknown_provider_still_asks_but_marks_nothing(conn, board, tmp_path):
    _seed_task(conn, "T1", "w1")
    board.cards["w1"] = _card("w1", [_run(7, "crashed", AUTH_TEXT)], model_override="mystery/model")
    decisions = _pass(conn, tmp_path)
    assert [(d.action, d.provider, d.model) for d in decisions] == \
        [("mark_credential_unhealthy", None, "mystery/model")]
    assert [m[0] for m in board.mutations] == ["comment"]
    assert _events_of(conn, "credential_unhealthy") == [] and recovery.unhealthy_credentials(conn) == set()
    assert [e["action"] for e in _events_of(conn, "recovery_decision")] == ["mark_credential_unhealthy"]


def test_the_second_capability_failure_returns_a_switch_model_decision_and_applies_nothing(conn, board, tmp_path):
    """ASES-REC-01 (19.2): a capability failure restarts from a fresh worktree and the second one also takes the next
    model. So the switch is decided here, with the model and the provider filled in, and left to the controller to
    do on the replacement card: the failed card is neither given the model in place nor unblocked."""
    _seed_task(conn, "T1", "w1")
    recovery.bump(conn, "p1", "T1", "capability_failures")            # the first one was fresh_attempt earlier
    board.cards["w1"] = _card("w1", [_run(7, "crashed", PROTOCOL_TEXT)])

    decisions = _pass(conn, tmp_path)

    assert [d.action for d in decisions] == ["switch_model"]
    assert (decisions[0].provider, decisions[0].model) == ("xkiro", CANDIDATE_1)
    assert (decisions[0].task_key, decisions[0].card_id, decisions[0].run_id) == ("T1", "w1", 7)
    assert f"xkiro/{CANDIDATE_1}" in decisions[0].reason and "fresh worktree" in decisions[0].reason
    assert board.mutations == []                                       # no set-model, no unblock, no comment
    assert "model_override" not in board.cards["w1"] and board.cards["w1"]["status"] == "blocked"
    assert _lineage_row(conn, "T1")["capability_failures"] == 2
    (record,) = _events_of(conn, "recovery_decision")
    assert (record["action"], record["model"], record["provider"], record["applied"]) == \
        ("switch_model", CANDIDATE_1, "xkiro", False)
    (target,) = _events_of(conn, "recovery_switch_target")             # the decided switch is remembered
    assert (target["task_key"], target["card_id"], target["run_id"], target["provider"], target["model"]) == \
        ("T1", "w1", 7, "xkiro", CANDIDATE_1)
    assert _pass(conn, tmp_path) == []                                 # decided once: later passes return nothing


def test_a_switch_moves_on_from_the_model_the_card_already_runs_on(conn, board, tmp_path):
    _seed_task(conn, "T1", "w1")
    recovery.bump(conn, "p1", "T1", "capability_failures")
    board.cards["w1"] = _card("w1", [_run(7, "crashed", PROTOCOL_TEXT)], model_override=CANDIDATE_1,
                              provider_override="xkiro")
    decisions = _pass(conn, tmp_path)
    assert decisions[0].model == CODER_MODEL                          # pinned first, and not the current one


def test_a_switch_with_no_other_usable_model_starts_another_fresh_attempt_instead(conn, board, tmp_path):
    _seed_task(conn, "T3", "w3")
    recovery.bump(conn, "p1", "T3", "capability_failures")
    board.cards["w3"] = _card("w3", [_run(7, "crashed", PROTOCOL_TEXT, profile="reviewer")])      # one reviewer model

    decisions = _pass(conn, tmp_path)

    assert [d.action for d in decisions] == ["fresh_attempt"]
    assert "no other usable model" in decisions[0].reason and "'reviewer'" in decisions[0].reason
    assert board.mutations == []
    assert _lineage_row(conn, "T3")["capability_failures"] == 2


def test_a_switch_skips_a_provider_whose_credential_was_marked_unhealthy(conn, board, tmp_path):
    _seed_task(conn, "T1", "w1")
    recovery.bump(conn, "p1", "T1", "capability_failures")
    events.record(conn, "credential_unhealthy", {"provider": "xkiro"})
    board.cards["w1"] = _card("w1", [_run(7, "crashed", PROTOCOL_TEXT)])
    decisions = _pass(conn, tmp_path)
    assert [d.action for d in decisions] == ["fresh_attempt"] and board.mutations == []


def test_a_switch_never_leaves_the_projects_data_class(conn, board, tmp_path):
    _seed_task(conn, "T1", "w1")
    recovery.bump(conn, "p1", "T1", "capability_failures")
    board.cards["w1"] = _card("w1", [_run(7, "crashed", PROTOCOL_TEXT)])
    decisions = _pass(conn, tmp_path, data_class="private")
    assert [d.action for d in decisions] == ["fresh_attempt"] and board.mutations == []


def test_the_first_capability_failure_is_returned_as_a_fresh_attempt_and_nothing_is_applied(conn, board, tmp_path):
    _seed_task(conn, "T1", "w1")
    board.cards["w1"] = _card("w1", [_run(7, "crashed", PROTOCOL_TEXT)])

    decisions = _pass(conn, tmp_path)

    assert [d.action for d in decisions] == ["fresh_attempt"]
    assert (decisions[0].task_key, decisions[0].card_id, decisions[0].run_id) == ("T1", "w1", 7)
    assert board.mutations == []
    assert _lineage_row(conn, "T1")["capability_failures"] == 1
    (record,) = _events_of(conn, "recovery_decision")
    assert (record["action"], record["applied"], record["kind"]) == ("fresh_attempt", False, "capability")


def test_a_runtime_overrun_counts_as_a_capability_failure(conn, board, tmp_path):
    _seed_task(conn, "T1", "w1")
    board.cards["w1"] = _card("w1", [_run(7, "timed_out", "elapsed 2700s > limit 2700s")])
    decisions = _pass(conn, tmp_path)
    assert [(d.action, d.failure_kind) for d in decisions] == [("fresh_attempt", FailureKind.RUNTIME)]
    assert _lineage_row(conn, "T1")["capability_failures"] == 1
    assert _lineage_row(conn, "T1")["infra_failures"] == 0


def test_an_exhausted_budget_replans_once_then_asks_the_user(conn, board, tmp_path):
    _seed_task(conn, "T1", "w1")
    for _ in range(2):
        recovery.bump(conn, "p1", "T1", "capability_failures")
    board.cards["w1"] = _card("w1", [_run(7, "crashed", PROTOCOL_TEXT)], status="blocked")

    replan = _pass(conn, tmp_path)
    assert [d.action for d in replan] == ["replan"]
    assert board.mutations == []                                       # asking the Lead is the controller's job
    assert _lineage_row(conn, "T1")["capability_failures"] == 3
    assert _lineage_row(conn, "T1")["replans"] == 0                    # ... and so is spending the re-plan

    recovery.bump(conn, "p1", "T1", "replans")                          # what the controller does when it re-plans
    board.cards["w1"]["_runs"].append(_run(8, "crashed", PROTOCOL_TEXT))
    asked = _pass(conn, tmp_path)
    assert [d.action for d in asked] == ["block_for_user"]
    assert [m[0] for m in board.mutations] == ["comment"] and _asked(board)[0].endswith("?")
    assert _lineage_row(conn, "T1")["capability_failures"] == 4


def test_a_spent_review_or_fix_budget_escalates_the_next_capability_failure(conn, board, tmp_path):
    _seed_task(conn, "T1", "w1", fix_cards=2)
    board.cards["w1"] = _card("w1", [_run(7, "crashed", PROTOCOL_TEXT)])
    assert [d.action for d in _pass(conn, tmp_path)] == ["replan"]     # first failure, but fix_cards is at its limit


def test_the_projects_replan_budget_turns_a_replan_into_a_question(conn, board, tmp_path):
    _seed_task(conn, "T1", "w1")
    _seed_task(conn, "T2", "w2")
    for _ in range(3):
        recovery.bump(conn, "p1", "T1", "capability_failures")
    conn.execute("INSERT INTO lineage (project, task_key, replans, updated_at) VALUES ('p1', 'T2', 1, 'then')")
    board.cards["w1"] = _card("w1", [_run(7, "crashed", PROTOCOL_TEXT)])

    # T1 has never been re-planned, but the project's only re-plan was spent on T2.
    decisions = _pass(conn, tmp_path, budgets={"replans_per_project": 1})
    assert [d.action for d in decisions] == ["block_for_user"]
    assert "re-plans" in decisions[0].reason and decisions[0].reason.endswith("?")
    assert [m[0] for m in board.mutations] == ["comment"]

    # With room left in the project's budget the same failure is a re-plan.
    board.mutations.clear()
    conn.execute("DELETE FROM events")
    board.cards["w1"]["_runs"].append(_run(8, "crashed", PROTOCOL_TEXT))
    assert [d.action for d in _pass(conn, tmp_path, budgets={"replans_per_project": 2})] == ["replan"]


def test_a_worker_asking_a_question_is_left_alone(conn, board, tmp_path):
    _seed_task(conn, "T1", "w1")
    board.cards["w1"] = _card("w1", [_run(1, "blocked", summary="Which database should I use?")])
    assert _pass(conn, tmp_path) == []
    assert board.mutations == [] and _events_of(conn, "recovery_decision") == []

    # Even when earlier attempts had failed: the question is the latest thing that happened.
    board.cards["w1"]["_runs"].insert(0, _run(0, "crashed", CRASH_TEXT))
    assert _pass(conn, tmp_path) == []
    assert board.mutations == [] and _lineage_row(conn, "T1") is None


def test_a_failure_after_an_answered_question_is_recovered(conn, board, tmp_path):
    _seed_task(conn, "T1", "w1")
    board.cards["w1"] = _card("w1", [_run(1, "blocked", summary="Which database?"), _run(2, "crashed", CRASH_TEXT)])
    assert [d.run_id for d in _pass(conn, tmp_path)] == [2]


@pytest.mark.parametrize("status", ["ready", "running", "review", "scheduled", "todo", "done", "triage", "archived"])
def test_only_a_blocked_card_is_recovered(conn, board, tmp_path, status):
    _seed_task(conn, "T1", "w1")
    board.cards["w1"] = _card("w1", [_run(7, "crashed", CRASH_TEXT)], status=status)
    assert _pass(conn, tmp_path) == [] and board.mutations == []


def test_a_blocked_card_whose_latest_run_ended_normally_is_left_alone(conn, board, tmp_path):
    _seed_task(conn, "T1", "w1")
    for outcome in ("completed", "review_requested", "changes_requested", "scheduled"):
        board.cards["w1"] = _card("w1", [_run(1, "crashed", CRASH_TEXT), _run(2, outcome, summary="fine")])
        assert _pass(conn, tmp_path) == []
    assert board.mutations == []


def test_the_latest_run_that_has_an_outcome_decides_and_an_open_run_is_skipped(conn, board, tmp_path):
    _seed_task(conn, "T1", "w1")
    board.cards["w1"] = _card("w1", [_run(5, "crashed", QUOTA_TEXT), _run(6, None, None, ended_at=None)])
    decisions = _pass(conn, tmp_path)
    assert [(d.action, d.run_id) for d in decisions] == [("park", 5)]


def test_a_card_with_no_runs_is_left_alone(conn, board, tmp_path):
    _seed_task(conn, "T1", "w1")
    board.cards["w1"] = _card("w1", [])
    assert _pass(conn, tmp_path) == [] and _events_of(conn, "recovery_decision") == []


def test_the_current_work_card_is_the_one_read_not_the_original(conn, board, tmp_path):
    _seed_task(conn, "T1", "fix1", fix_cards=1)      # process_merge_queue repointed the task at its fix card
    board.cards["w1"] = _card("w1", [_run(3, "crashed", QUOTA_TEXT)])                    # the old, still-blocked card
    board.cards["fix1"] = _card("fix1", [_run(9, "crashed", POLICY_TEXT)])
    decisions = _pass(conn, tmp_path)
    assert [(d.card_id, d.run_id, d.action) for d in decisions] == [("fix1", 9, "block_for_user")]


# ---------------------------------------------------------------------------------------------
# _recover_task: a `ready` card after an auth- or quota-shaped failure (round 7, ASES-REC-01, bug 3)
# ---------------------------------------------------------------------------------------------
# AC-A's round 6 finding, and process_failures's own old docstring, said plainly that a card Hermes's
# dispatcher respawn guard holds in `ready` (kanban_db_dispatch.check_respawn_guard's blocker_auth rule,
# which never expires on its own) never reached recovery at all, however many passes polled it. _pass's
# default `now` (ENDED + 10_000) is already comfortably past READY_RESPAWN_SETTLE_SECONDS (30 s), so every
# test below except the settle-window one itself is well past it.


def test_a_ready_card_with_a_stale_auth_failure_is_recovered_like_a_blocked_one(conn, board, tmp_path):
    _seed_task(conn, "T1", "w1")
    board.cards["w1"] = _card("w1", [_run(7, "crashed", AUTH_TEXT)], status="ready")

    decisions = _pass(conn, tmp_path)

    assert [d.action for d in decisions] == ["mark_credential_unhealthy"]
    assert (decisions[0].provider, decisions[0].model) == ("xkiro", CODER_MODEL)
    # questions.ask_user's OWN logic (not this module's), unchanged by this fix: a `ready` card, unlike a
    # `blocked` merge card, is not already blocked, so real Hermes accepts `kanban block --kind needs_input`
    # for it, which is a MORE useful outcome than the comment-only fallback a blocked card needs (a real,
    # actionable block a person sees, not just a comment).
    assert [(m[0], m[1], m[3]) for m in board.mutations] == [("block", "w1", "needs_input")]
    (unhealthy,) = _events_of(conn, "credential_unhealthy")
    assert (unhealthy["task_key"], unhealthy["provider"]) == ("T1", "xkiro")
    assert recovery.unhealthy_credentials(conn) == {("xkiro", "*")}


def test_a_ready_card_with_a_stale_quota_failure_is_recovered_like_a_blocked_one(conn, board, tmp_path):
    _seed_task(conn, "T1", "w1")
    board.cards["w1"] = _card("w1", [_run(7, "crashed", QUOTA_TEXT)], status="ready")

    decisions = _pass(conn, tmp_path)

    assert [d.action for d in decisions] == ["park"]
    assert [m[:2] for m in board.mutations] == [("schedule", "w1")]


@pytest.mark.parametrize("text", [CRASH_TEXT, PROTOCOL_TEXT])
def test_a_ready_card_with_an_infra_or_capability_failure_is_not_touched(conn, board, tmp_path, text):
    """Only AUTH and QUOTA are widened past `blocked` (see _recover_task's own docstring): an
    infrastructure- or capability-shaped failure on a `ready` card is Hermes's own ordinary retry in
    progress (CRASH_TEXT classifies as infrastructure, PROTOCOL_TEXT as capability), and reacting to either
    here would be exactly the false positive this fix must avoid."""
    _seed_task(conn, "T1", "w1")
    board.cards["w1"] = _card("w1", [_run(7, "crashed", text)], status="ready")

    assert _pass(conn, tmp_path) == []
    assert board.mutations == []
    assert _lineage_row(conn, "T1") is None   # not even counted: the gate returns before any counter is touched


def test_a_ready_cards_auth_failure_still_within_the_settle_window_is_not_touched_yet(conn, board, tmp_path):
    _seed_task(conn, "T1", "w1")
    board.cards["w1"] = _card("w1", [_run(7, "crashed", AUTH_TEXT)], status="ready")

    assert _pass(conn, tmp_path, now=ENDED + recovery.READY_RESPAWN_SETTLE_SECONDS - 1) == []
    assert board.mutations == []

    # The moment the settle window has fully elapsed, the very same failure is recovered.
    decisions = _pass(conn, tmp_path, now=ENDED + recovery.READY_RESPAWN_SETTLE_SECONDS)
    assert [d.action for d in decisions] == ["mark_credential_unhealthy"]


def test_a_ready_card_with_no_run_at_all_is_unaffected(conn, board, tmp_path):
    _seed_task(conn, "T1", "w1")
    board.cards["w1"] = _card("w1", [], status="ready")
    assert _pass(conn, tmp_path) == [] and board.mutations == []


def test_a_ready_card_whose_latest_run_succeeded_is_unaffected(conn, board, tmp_path):
    _seed_task(conn, "T1", "w1")
    board.cards["w1"] = _card("w1", [_run(7, "completed", summary="all done")], status="ready")
    assert _pass(conn, tmp_path) == [] and board.mutations == []


def test_tasks_with_no_card_yet_are_skipped_and_other_projects_are_not_touched(conn, board, tmp_path):
    conn.execute("INSERT INTO plan_tasks (project, task_key, work_card_id, merge_card_id, role, created_at) "
                 "VALUES ('p1', 'T1', NULL, NULL, 'coder', datetime('now'))")
    _seed_task(conn, "T2", "other", project="other-project")           # same key space, different project
    board.cards["other"] = _card("other", [_run(3, "crashed", QUOTA_TEXT)])
    assert _pass(conn, tmp_path) == [] and board.mutations == []


def test_each_failure_is_counted_once_however_many_passes_see_the_card(conn, board, tmp_path):
    board.status_follows = False
    _seed_task(conn, "T1", "w1")
    board.cards["w1"] = _card("w1", [_run(7, "crashed", PROTOCOL_TEXT)])

    assert [d.action for d in _pass(conn, tmp_path)] == ["fresh_attempt"]
    assert _pass(conn, tmp_path) == []
    assert _pass(conn, tmp_path) == []
    assert _lineage_row(conn, "T1")["capability_failures"] == 1
    assert len(_events_of(conn, "recovery_decision")) == 1

    # A NEW failed run is a new failure, and is decided on the lineage as it now stands.
    board.cards["w1"]["_runs"].append(_run(8, "crashed", PROTOCOL_TEXT))
    assert [d.action for d in _pass(conn, tmp_path)] == ["switch_model"]
    assert _lineage_row(conn, "T1")["capability_failures"] == 2
    assert _pass(conn, tmp_path) == []
    assert _lineage_row(conn, "T1")["capability_failures"] == 2


def test_a_run_is_identified_by_its_card_as_well_as_its_id(conn, board, tmp_path):
    """Run ids from different cards can collide in a fake board; the record is keyed by card and run together."""
    board.status_follows = False
    _seed_task(conn, "T1", "w1")
    board.cards["w1"] = _card("w1", [_run(1, "crashed", PROTOCOL_TEXT)])
    board.cards["fix1"] = _card("fix1", [_run(1, "crashed", PROTOCOL_TEXT)])
    assert [d.card_id for d in _pass(conn, tmp_path)] == ["w1"]
    conn.execute("UPDATE plan_tasks SET work_card_id = 'fix1' WHERE task_key = 'T1'")
    assert [d.card_id for d in _pass(conn, tmp_path)] == ["fix1"]
    assert _lineage_row(conn, "T1")["capability_failures"] == 2


def test_a_run_with_no_id_is_still_counted_once(conn, board, tmp_path):
    board.status_follows = False
    _seed_task(conn, "T1", "w1")
    run = _run(None, "crashed", PROTOCOL_TEXT)
    board.cards["w1"] = _card("w1", [run])
    assert [d.run_id for d in _pass(conn, tmp_path)] == ["#0"]
    assert _pass(conn, tmp_path) == []
    assert _lineage_row(conn, "T1")["capability_failures"] == 1


def test_one_failing_hermes_call_does_not_stop_the_other_tasks(conn, board, tmp_path):
    for key, card in (("T1", "w1"), ("T2", "w2")):
        _seed_task(conn, key, card)
        board.cards[card] = _card(card, [_run(7 if key == "T1" else 8, "crashed", QUOTA_TEXT)])
    board.fail[("schedule", "w1")] = hermes.HermesCommandError(["kanban", "schedule", "w1"], 1, "database is locked")

    decisions = _pass(conn, tmp_path)

    assert [(d.task_key, d.action) for d in decisions] == [("T2", "park")]
    assert [m[:2] for m in board.mutations] == [("schedule", "w2")]
    (error,) = _events_of(conn, "recovery_error")
    assert (error["task_key"], error["card_id"], error["action"]) == ("T1", "w1", "park")
    assert "database is locked" in error["error"]
    assert [e["task_key"] for e in _events_of(conn, "recovery_decision")] == ["T2"]

    # T1 is retried, and the same failure is not written to the log a second time.
    assert _pass(conn, tmp_path) == []
    assert len(_events_of(conn, "recovery_error")) == 1
    del board.fail[("schedule", "w1")]
    assert [(d.task_key, d.action) for d in _pass(conn, tmp_path)] == [("T1", "park")]


@pytest.mark.parametrize("error", [
    hermes.HermesNotFound("`hermes` is not on PATH"),
    hermes.HermesCommandError(["kanban", "show", "w1"], 1, "boom"),
    ValueError("Expecting value: line 1 column 1 (char 0)"),
    OSError("The handle is invalid"),
])
def test_a_card_that_cannot_be_read_is_recorded_and_the_others_carry_on(conn, board, tmp_path, error):
    _seed_task(conn, "T1", "w1")
    _seed_task(conn, "T2", "w2")
    board.cards["w1"] = _card("w1", [_run(7, "crashed", QUOTA_TEXT)])
    board.cards["w2"] = _card("w2", [_run(8, "crashed", QUOTA_TEXT)])
    board.fail[("show", "w1")] = error

    assert [d.task_key for d in _pass(conn, tmp_path)] == ["T2"]

    (recorded,) = _events_of(conn, "recovery_error")
    assert (recorded["task_key"], recorded["action"]) == ("T1", "show")


def test_a_switch_decision_that_could_not_be_recorded_is_made_again_on_the_same_model(
    conn, board, tmp_path, monkeypatch,
):
    """The target is remembered before the decision is written. When the write fails the decision is made again on
    the next pass, and by then the model may be pinned on the card: choosing "the next model after the current one"
    again would return to the model that just failed twice. The remembered target is reused instead."""
    _seed_task(conn, "T1", "w1")
    recovery.bump(conn, "p1", "T1", "capability_failures")
    board.cards["w1"] = _card("w1", [_run(7, "crashed", PROTOCOL_TEXT)])
    real_bump = recovery.bump

    def broken_bump(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(recovery, "bump", broken_bump)
    with pytest.raises(RuntimeError):
        _pass(conn, tmp_path)
    assert _events_of(conn, "recovery_decision") == [] and _lineage_row(conn, "T1")["capability_failures"] == 1
    (target,) = _events_of(conn, "recovery_switch_target")
    assert (target["task_key"], target["card_id"], target["run_id"], target["model"]) == ("T1", "w1", 7, CANDIDATE_1)

    board.cards["w1"]["model_override"], board.cards["w1"]["provider_override"] = CANDIDATE_1, "xkiro"
    monkeypatch.setattr(recovery, "bump", real_bump)
    decisions = _pass(conn, tmp_path)
    assert [(d.action, d.provider, d.model) for d in decisions] == [("switch_model", "xkiro", CANDIDATE_1)]
    assert len(_events_of(conn, "recovery_switch_target")) == 1                           # remembered once
    assert _lineage_row(conn, "T1")["capability_failures"] == 2
    assert board.mutations == []


def test_a_remembered_switch_target_belongs_to_one_failed_run_only(conn, board, tmp_path):
    budgets = {"attempts_per_card": 6}
    _seed_task(conn, "T1", "w1")
    recovery.bump(conn, "p1", "T1", "capability_failures")
    board.cards["w1"] = _card("w1", [_run(7, "crashed", PROTOCOL_TEXT)])
    assert [d.model for d in _pass(conn, tmp_path, budgets=budgets)] == [CANDIDATE_1]

    # The controller pinned that model on the card, and the card failed again on it. That is a NEW run, so the next
    # model is chosen afresh from the one the card now runs on, not taken from the target remembered for run 7.
    board.cards["w1"]["model_override"], board.cards["w1"]["provider_override"] = CANDIDATE_1, "xkiro"
    board.cards["w1"]["_runs"].append(_run(8, "crashed", PROTOCOL_TEXT))
    assert [d.model for d in _pass(conn, tmp_path, budgets=budgets)] == [CODER_MODEL]
    assert [(t["run_id"], t["model"]) for t in _events_of(conn, "recovery_switch_target")] == [
        (7, CANDIDATE_1), (8, CODER_MODEL),
    ]
    assert board.mutations == []


def test_a_failed_unblock_after_a_resume_is_retried_without_counting_twice(conn, board, tmp_path):
    _seed_task(conn, "T1", "w1")
    board.cards["w1"] = _card("w1", [_run(7, "crashed", CRASH_TEXT)])
    board.fail[("unblock", "w1")] = hermes.HermesCommandError(["kanban", "unblock", "w1"], 1, "locked")
    assert _pass(conn, tmp_path, now=ENDED + 60) == []
    assert _lineage_row(conn, "T1") is None
    del board.fail[("unblock", "w1")]
    assert [d.action for d in _pass(conn, tmp_path, now=ENDED + 60)] == ["resume"]
    assert _lineage_row(conn, "T1")["infra_failures"] == 1


def test_unknown_failures_are_left_to_hermes_until_attempts_per_card_of_them_in_a_row(conn, board, tmp_path):
    _seed_task(conn, "T1", "w1")
    two = [_run(1, "crashed", ""), _run(2, "gave_up", "")]
    board.cards["w1"] = _card("w1", two)

    decisions = _pass(conn, tmp_path)
    assert [(d.action, d.failure_kind) for d in decisions] == [("none", FailureKind.UNKNOWN)]
    assert board.mutations == [] and _lineage_row(conn, "T1") is None
    assert _pass(conn, tmp_path) == []                                 # recorded once, not returned again

    board.cards["w1"]["_runs"].append(_run(3, "crashed", ""))          # the third unknown failure in a row
    decisions = _pass(conn, tmp_path)
    assert [d.action for d in decisions] == ["block_for_user"]
    assert [m[0] for m in board.mutations] == ["comment"]


def test_the_unknown_streak_is_broken_by_a_failure_of_any_other_kind(conn, board, tmp_path):
    _seed_task(conn, "T1", "w1")
    board.cards["w1"] = _card("w1", [
        _run(1, "crashed", ""), _run(2, "crashed", ""),
        _run(3, "crashed", CRASH_TEXT),                                # an infrastructure failure ends the streak
        _run(4, "crashed", ""),
    ])
    decisions = _pass(conn, tmp_path)
    assert [(d.action, d.failure_kind) for d in decisions] == [("none", FailureKind.UNKNOWN)]   # a streak of one
    assert board.mutations == []


def test_the_unknown_streak_skips_an_open_run(conn, board, tmp_path):
    _seed_task(conn, "T1", "w1")
    board.cards["w1"] = _card("w1", [
        _run(1, "crashed", ""), _run(2, "crashed", ""), _run(3, "crashed", ""), _run(4, None, None, ended_at=None),
    ])
    assert [(d.action, d.run_id) for d in _pass(conn, tmp_path)] == [("block_for_user", 3)]


def test_the_unknown_streak_counts_through_rate_limited_runs(conn, board, tmp_path):
    """A rate-limited run says nothing about the task, so Hermes skips it when it counts a streak, and so do we."""
    _seed_task(conn, "T1", "w1")
    board.cards["w1"] = _card("w1", [
        _run(1, "crashed", ""), _run(2, "rate_limited", ""), _run(3, "crashed", ""),
        _run(4, "rate_limited", ""), _run(5, "crashed", ""),
    ])
    assert [d.action for d in _pass(conn, tmp_path)] == ["block_for_user"]          # three unknowns in a row
    assert [m[0] for m in board.mutations] == ["comment"]


def test_a_latest_run_that_was_rate_limited_is_a_rate_limit_not_an_unknown_failure(conn, board, tmp_path):
    _seed_task(conn, "T1", "w1")
    board.cards["w1"] = _card("w1", [_run(1, "crashed", ""), _run(2, "crashed", ""), _run(3, "rate_limited", "429")])
    assert [d.failure_kind for d in _pass(conn, tmp_path)] == [FailureKind.RATE_LIMIT]
    assert board.mutations == []


def test_a_rate_limit_is_recorded_and_left_to_hermes(conn, board, tmp_path):
    _seed_task(conn, "T1", "w1")
    board.cards["w1"] = _card("w1", [_run(7, "rate_limited", "quota wall, requeued")])
    decisions = _pass(conn, tmp_path)
    assert [(d.action, d.failure_kind) for d in decisions] == [("none", FailureKind.RATE_LIMIT)]
    assert board.mutations == [] and _lineage_row(conn, "T1") is None
    (record,) = _events_of(conn, "recovery_decision")
    assert (record["action"], record["applied"], record["kind"]) == ("none", False, "rate_limit")


def test_the_bounds_come_from_the_projects_budgets(conn, board, tmp_path):
    _seed_task(conn, "T1", "w1")
    recovery.bump(conn, "p1", "T1", "infra_failures")
    board.cards["w1"] = _card("w1", [_run(7, "crashed", CRASH_TEXT)])
    # attempts_per_card 2: the second infrastructure failure is the last one allowed before a question.
    assert [d.action for d in _pass(conn, tmp_path, budgets={"attempts_per_card": 2})] == ["block_for_user"]


def test_every_recorded_decision_carries_the_task_the_run_the_kind_and_the_reason(conn, board, tmp_path):
    _seed_task(conn, "T1", "w1")
    board.cards["w1"] = _card("w1", [_run(7, "crashed", POLICY_TEXT)])
    _pass(conn, tmp_path)
    (record,) = _events_of(conn, "recovery_decision")
    assert record["project"] == "p1" and record["task_key"] == "T1"        # task_key survives redaction
    assert (record["card_id"], record["run_id"], record["kind"], record["action"]) == \
        ("w1", 7, "policy", "block_for_user")
    assert "never relaxes the data class" in record["reason"]
    assert (record["backoff_seconds"], record["applied"]) == (0, True)


def test_a_secret_in_a_failed_runs_error_never_reaches_a_question_or_the_log(conn, board, tmp_path):
    secret = "sk-abcdefghij0123456789"
    _seed_task(conn, "T1", "w1")
    board.cards["w1"] = _card("w1", [_run(7, "crashed", f"HTTP 401: Invalid API key {secret} for account")])
    _pass(conn, tmp_path)
    (reason,) = _asked(board)
    assert secret not in reason and "[redacted]" in reason
    assert not any(secret in str(part) for mutation in board.mutations for part in mutation)   # nor in any comment
    everything = json.dumps([dict(r) for r in conn.execute("SELECT kind, payload FROM events")])
    assert secret not in everything


def test_a_question_is_ascii_one_line_and_short_even_for_a_hostile_error(conn, board, tmp_path):
    _seed_task(conn, "T1", "w1")
    error = f"HTTP 404 No endpoints found {ARROW} line one\nline two\t" + "z" * 900
    board.cards["w1"] = _card("w1", [_run(7, "crashed", error)])
    _pass(conn, tmp_path)
    (reason,) = _asked(board)
    assert reason.isascii() and "\n" not in reason and "\t" not in reason
    assert ESCAPED_ARROW in reason and len(reason) < 700 and reason.endswith("?")


def test_process_failures_does_not_write_to_the_card_or_the_plan(conn, board, tmp_path):
    _seed_task(conn, "T1", "w1")
    board.cards["w1"] = _card("w1", [_run(7, "crashed", PROTOCOL_TEXT)])
    plan_before = copy.deepcopy(PLAN)
    _pass(conn, tmp_path)
    assert PLAN == plan_before
    row = conn.execute("SELECT work_card_id, fix_cards FROM plan_tasks WHERE task_key = 'T1'").fetchone()
    assert (row["work_card_id"], row["fix_cards"]) == ("w1", 0)


def test_the_savepoint_is_released_so_the_writes_are_committed(conn, board, tmp_path):
    _seed_task(conn, "T1", "w1")
    board.cards["w1"] = _card("w1", [_run(7, "crashed", PROTOCOL_TEXT)])
    _pass(conn, tmp_path)
    assert not conn.in_transaction


def test_a_failure_while_recording_rolls_the_whole_record_back(conn, board, tmp_path, monkeypatch):
    """The decision event and the counter bump land together or not at all: a failed bump leaves no half record that
    would make the next pass skip a failure that was never counted."""
    _seed_task(conn, "T1", "w1")
    board.cards["w1"] = _card("w1", [_run(7, "crashed", PROTOCOL_TEXT)])

    real_bump = recovery.bump

    def broken_bump(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(recovery, "bump", broken_bump)
    with pytest.raises(RuntimeError):
        _pass(conn, tmp_path)
    assert _events_of(conn, "recovery_decision") == [] and _lineage_row(conn, "T1") is None
    assert not conn.in_transaction

    monkeypatch.setattr(recovery, "bump", real_bump)
    assert [d.action for d in _pass(conn, tmp_path)] == ["fresh_attempt"]
    assert _lineage_row(conn, "T1")["capability_failures"] == 1
