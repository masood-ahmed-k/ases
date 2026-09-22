"""Acceptance 22.3: failure and fallback (blueprint.txt [p403]/[p404]; ASES-REC-01, ASES-REC-02, ASES-CAP-03,
ASES-RTE-01).

"Script the fake provider to return 429 with Retry-After, then 500s, then 401 for one model. The run must wait,
resume, fall back along the configured chain only, record the provider and model actually used, and mark the
credential unhealthy, without losing or duplicating cards."

Package AC-A drives this through the REAL controller.process_recovery against ases.fakes.board.FakeHermes only: no
ases.fakes.provider HTTP server is ever started (nothing here reads what was sent to a model, only what a worker's
failed run says happened), and nothing calls a real Hermes or a real provider. The failure TEXT is what
recovery.classify_run reads (recovery.py's own ordered regex rules: "\\b429\\b|rate...limit|retry...after" for a
rate limit, "\\b50[0234]\\b|...|internal server error" for infrastructure, "\\b40[13]\\b|invalid...api...key|
unauthorized" for auth, "protocol violation" for a capability failure), and Hermes's OWN circuit breaker (real fact,
board.py's docstring: a card's max_retries is pinned at creation to budgets.attempts_per_card, the blueprint's
default of 3, "Hermes gives up after 2 by default and the blueprint says 3") decides when a card actually becomes
`blocked` and is handed to controller.process_recovery: one failure under that limit is Hermes retrying the SAME
card on its own, which is why recovery never even looks at a card that is not `blocked` (recovery._recover_task:
"if card.get('status') != 'blocked': return None").

One card (T1, the one-task plan) is walked through all four classifications in sequence, across however many
replacement cards a capability failure and a model switch create for it (ASES-REC-01, 19.2: those two restart from a
fresh card, never in place), so "without losing or duplicating cards" can be checked by counting the board at the
end: the merge card is created once and never again, and each fresh attempt archives the card it replaces.
"""
from __future__ import annotations

import json

from ases import events, questions, recovery
from ases.fakes import worker as fw

# Two providers, kept apart on purpose: the coder's pinned provider is the one the auth failure (phase 3) marks
# unhealthy, and neither declared candidate is on it, so the later switch-model choice (phase 4) is never affected
# by that unrelated credential mark. That is what "fall back along the CONFIGURED chain only" means here: the
# fallback must be the model_config's own next_model() rule, not an accident of which provider got poisoned first.
CODER_PROVIDER = "fake"
CODER_MODEL = "fake-coder"
CANDIDATE_PROVIDER = "fake-candidates"
CANDIDATE_1 = "fake-coder-candidate-1"
CANDIDATE_2 = "fake-coder-candidate-2"
REVIEWER_PROVIDER = "fake"
REVIEWER_MODEL = "fake-reviewer"

# Mirrors the models_config["models"] shape test_recovery.py's own MODELS constant uses for a role_class + _candidate
# pair (two coder_candidate rows, so a switch can be shown to pick from the declared set and never invent a model).
MODELS_CONFIG = {
    "providers": {CODER_PROVIDER: {"limits": {}}, CANDIDATE_PROVIDER: {"limits": {}}},
    "models": [
        {"provider": CODER_PROVIDER, "model": CODER_MODEL, "role_class": "coder", "pinned": True},
        {"provider": CANDIDATE_PROVIDER, "model": CANDIDATE_1, "role_class": "coder_candidate", "pinned": False},
        {"provider": CANDIDATE_PROVIDER, "model": CANDIDATE_2, "role_class": "coder_candidate", "pinned": False},
        {"provider": REVIEWER_PROVIDER, "model": REVIEWER_MODEL, "role_class": "reviewer", "pinned": True},
    ],
}

# Error text chosen to match exactly one rule of recovery._TEXT_RULES (read from recovery.py, 2026-09-22), and
# deliberately NOT to match FakeHermes's own _RESPAWN_BLOCKER_RE (board.py: "a last failure that reads like a
# quota or auth wall is never respawned" -- it matches on 429, 403, "rate limit", "auth...", "unauthorized",
# "forbidden", "invalid api key"). A text that satisfies classify_run's rate-limit or auth rule with words the
# respawn guard also recognises would leave the card stuck in 'ready', respawn-guarded, before Hermes's own
# breaker (three CONSECUTIVE failures) ever had the chance to trip it: "too many requests" (no "429", no "rate
# limit") and a bare "401" (the guard's list has 403, not 401) clear recovery's rules without tripping the guard.
RATE_LIMIT_TEXT = "Too many requests right now, please slow down"
INFRA_TEXT = "HTTP 500 Internal Server Error"
AUTH_TEXT = "request failed with status 401"
CAPABILITY_TEXT = "worker exited cleanly without a terminal kanban call - protocol violation detected"

A_PY = "def add(x, y):\n    return x + y\n"


class _CrashThenSucceed:
    """A coder worker for ONE plan task that crashes through `script` ((outcome, error) pairs, in order) across
    however many cards the task spawns (a fresh attempt or a switch replaces the card with a new one, so the count
    is kept on the worker instance, not per card id, unlike fakes.worker.sequence), then runs `then` for every
    dispatch once the script is exhausted. Registered through fw.by_task_key so the SAME instance answers every
    generation of T1's card."""

    def __init__(self, script, then):
        self.script = list(script)
        self.then = then
        self.calls = 0

    def __call__(self, fake, card, run, workspace_path) -> None:
        if self.calls < len(self.script):
            outcome, error = self.script[self.calls]
            self.calls += 1
            fake.agent_fail(card["id"], error, outcome, run_id=run["id"])
        else:
            self.then(fake, card, run, workspace_path)


def _payloads(conn, kind: str) -> list[dict]:
    """The payloads of every ASES-level event of `kind`, oldest first (events.recent is newest-first and stores
    payload as a JSON string, not the fake board's own per-card _events)."""
    rows = [e for e in events.recent(conn, limit=500) if e["kind"] == kind]
    rows.reverse()
    return [json.loads(row["payload"]) for row in rows]


def test_22_3_rate_limit_waits_infra_resumes_auth_marks_unhealthy_then_falls_back_along_the_chain(
    world_factory, one_task_plan, run_until, git,
):
    """Blueprint 22.3 ([p403]/[p404]), the whole arc on one plan task. ASES-REC-01, ASES-CAP-03, ASES-RTE-01."""
    world = world_factory(plan_raw=one_task_plan, models_config=MODELS_CONFIG)
    fake = world.fake
    conn = world.conn

    # rate_limit(1), infrastructure(2) -- the second infrastructure crash is the third CONSECUTIVE failure of this
    # card, which is what trips Hermes's own breaker (max_retries pinned to budgets.attempts_per_card, 3 by
    # default) and hands the card to recovery for the first time.
    # auth(3) -- a fresh trip (the resume below reset the counter), the third of which trips again.
    # capability(6) -- two more trips of three, the second of which is the SECOND capability failure of this TASK's
    # whole lineage (the counter is per task, not per card: ASES-REC-02), which is recovery's real switch-model
    # threshold (test_recovery.py: "the first capability failure is a fresh attempt and the second switches model").
    script = [
        ("crashed", RATE_LIMIT_TEXT),
        ("crashed", INFRA_TEXT), ("crashed", INFRA_TEXT),
        ("crashed", AUTH_TEXT), ("crashed", AUTH_TEXT), ("crashed", AUTH_TEXT),
        ("crashed", CAPABILITY_TEXT), ("crashed", CAPABILITY_TEXT), ("crashed", CAPABILITY_TEXT),
        ("crashed", CAPABILITY_TEXT), ("crashed", CAPABILITY_TEXT), ("crashed", CAPABILITY_TEXT),
    ]
    session = {}

    def _finish(fake, card, run, workspace_path) -> None:
        # ASES-RTE-01: the session that finally finishes this run reports having run on the switched-to candidate
        # model, which is "the provider and model actually used" -- set before the hand-off, so usage.py's later
        # ingest (it reads hermes.session_usage by session id) finds it.
        session["id"] = fake.session_id_for(card["id"], run["id"])
        fake.set_session_usage(session["id"], model=CANDIDATE_1, api_call_count=3)
        fw.good_coder({"a.py": A_PY}, "add a.py")(fake, card, run, workspace_path)

    worker = _CrashThenSucceed(script, _finish)
    fake.register_worker("coder-1", fw.by_task_key({"T1": worker}))

    t1 = world.create_cards()["T1"]
    work = t1.work_card_id

    # --- 1. Rate limit (429, Retry-After): action=none, "Hermes waits for Retry-After and retries" (decide()'s own
    # words). One failure is under the breaker's limit of 3, so the card is never blocked and recovery never even
    # looks at it: no fresh-attempt card, no recovery_decision event, the SAME card.
    run_until(world, lambda w: len(w.card(work)["_runs"]) >= 1)
    first = world.card(work)["_runs"][-1]
    assert first["outcome"] == "crashed" and RATE_LIMIT_TEXT in (first["error"] or "")
    assert recovery.classify_run(first) == recovery.FailureKind.RATE_LIMIT
    assert world.card(work)["status"] == "ready"
    assert len(fake.cards()) == 2   # just T1's work and merge card
    assert _payloads(conn, "recovery_decision") == []

    # --- 2. Infrastructure (500-shaped): the third consecutive failure (this one, after the rate limit and one
    # infrastructure crash) trips Hermes's breaker, and recovery classifies THIS run (the newest), decides resume.
    run_until(world, lambda w: w.card(work)["status"] == "blocked" and len(w.card(work)["_runs"]) >= 3)
    tripped = world.card(work)
    last_run = tripped["_runs"][-1]
    assert last_run["outcome"] == "crashed" and INFRA_TEXT in last_run["error"]
    assert recovery.classify_run(last_run) == recovery.FailureKind.INFRASTRUCTURE
    card_events = [e["kind"] for e in tripped["_events"]]
    assert card_events.count("gave_up") == 1 and "blocked" not in card_events   # a gave_up writes no blocked event

    # Per decide(): action=resume, in the SAME worktree with the SAME model, after a backoff. run_until's own
    # polling (it ticks the fake clock between passes) is what lets the backoff actually elapse.
    run_until(world, lambda w: w.card(work)["status"] == "ready" and len(w.card(work)["_runs"]) >= 4)
    resumes = _payloads(conn, "recovery_decision")
    assert [d["action"] for d in resumes] == ["resume"]
    assert resumes[0]["kind"] == "infrastructure" and resumes[0]["card_id"] == work
    assert len(fake.cards()) == 2 and world.work_card_id("T1") == work   # the SAME card, not duplicated

    # --- 3. Auth (401-shaped): three more consecutive failures (the counter reset when the card resumed) trip the
    # breaker again; per decide() this is mark_credential_unhealthy, never a retry loop.
    run_until(world, lambda w: w.card(work)["status"] == "blocked" and len(w.card(work)["_runs"]) >= 6)
    auth_run = world.card(work)["_runs"][-1]
    assert auth_run["outcome"] == "crashed" and AUTH_TEXT in auth_run["error"]
    assert recovery.classify_run(auth_run) == recovery.FailureKind.AUTH

    run_until(world, lambda w: bool(_payloads(conn, "credential_unhealthy")))
    (unhealthy,) = _payloads(conn, "credential_unhealthy")
    assert (unhealthy["provider"], unhealthy["model"]) == (CODER_PROVIDER, CODER_MODEL)
    assert recovery.unhealthy_credentials(conn) == {(CODER_PROVIDER, "*")}
    decisions = _payloads(conn, "recovery_decision")
    assert decisions[-1]["action"] == "mark_credential_unhealthy" and decisions[-1]["kind"] == "auth"
    # Hermes refuses a block on an already-blocked card (r6_rules.md): the question is a comment, never a re-block.
    assert world.card(work)["status"] == "blocked"
    asked = [q for q in questions.list_questions(world.board, world.plan, conn=conn) if q.card_id == work]
    # ask_user's comment ("ASES QUESTION: ...") is newer than the gave_up event it followed, so it is the signal
    # open_question reports (questions.py: the newest signal wins); Hermes never gets a real "BLOCKED:" comment or
    # a second block call for this card (it was already blocked, which Hermes refuses).
    assert len(asked) == 1 and asked[0].source == "ases_comment"
    assert "credential" in asked[0].question and asked[0].question.endswith("?")

    # A person deals with the credential (rotates the key) and tells ASES to try again.
    questions.answer_question(world.board, work, "rotated the key, please retry", conn=conn)
    assert world.card(work)["status"] == "ready"

    # --- 4. "Fall back along the configured chain only": the first capability failure (three more consecutive
    # crashes) is a fresh attempt on a NEW card; the second (three more, on that new card: the counter is per TASK)
    # switches to the first declared candidate, never a model outside the two configured.
    run_until(world, lambda w: len(w.fake.cards()) >= 3, max_passes=60)
    fresh_id = world.work_card_id("T1")
    assert fresh_id != work and "retry" in world.card(fresh_id)["title"]
    assert world.card(work)["status"] == "archived"           # the failed card is archived, not left as a question
    fresh_decisions = [d for d in _payloads(conn, "recovery_decision") if d["kind"] == "capability"]
    assert fresh_decisions[0]["action"] == "fresh_attempt"

    run_until(world, lambda w: len(w.fake.cards()) >= 4, max_passes=60)
    switched_id = world.work_card_id("T1")
    assert switched_id not in (work, fresh_id)
    assert world.card(fresh_id)["status"] == "archived"
    switched = world.card(switched_id)
    assert (switched["model_override"], switched["provider_override"]) == (CANDIDATE_1, CANDIDATE_PROVIDER)
    assert switched["model_override"] in (CANDIDATE_1, CANDIDATE_2)   # never a model outside the two declared
    capability_decisions = [d for d in _payloads(conn, "recovery_decision") if d["kind"] == "capability"]
    assert [d["action"] for d in capability_decisions] == ["fresh_attempt", "switch_model"]
    assert capability_decisions[1]["model"] == CANDIDATE_1 and capability_decisions[1]["provider"] == CANDIDATE_PROVIDER

    # The switched card finally succeeds: it merges, and the model and provider the session actually reported are
    # what is recorded (ASES-RTE-01), not the originally pinned model.
    run_until(world, lambda w: w.all_merge_cards_done(), max_passes=60)
    assert git(world, "show", "integration:a.py") == A_PY.strip()
    usage_row = conn.execute(
        "SELECT provider, model, requests, task_key FROM usage_ingested WHERE session_id = ?", (session["id"],),
    ).fetchone()
    assert usage_row is not None
    assert (usage_row["model"], usage_row["requests"], usage_row["task_key"]) == (CANDIDATE_1, 3, "T1")
    assert usage_row["provider"] == CANDIDATE_PROVIDER   # the provider actually hit, not the originally pinned one
    mismatches = _payloads(conn, "model_mismatch")
    assert any(m["actual"] == CANDIDATE_1 and m["expected"] == CODER_MODEL for m in mismatches)

    # "Without losing or duplicating cards": exactly the merge card, the two archived generations and the one that
    # finally succeeded -- nothing extra, nothing missing.
    assert len(fake.cards()) == 4
    assert {c["status"] for c in fake.cards() if c["id"] in (work, fresh_id)} == {"archived"}
    assert world.card(switched_id)["status"] == "done" and world.card(t1.merge_card_id)["status"] == "done"
