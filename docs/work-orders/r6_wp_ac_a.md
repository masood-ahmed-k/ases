# Package AC-A: acceptance 22.3 (failure and fallback) and 22.9 (quota exhaustion)

Files you own: `tests/acceptance/test_22_3_failure.py` (new), `tests/acceptance/test_22_9_quota.py` (new). Nothing else. You may NOT
edit any file under `src/`, and may NOT edit `tests/acceptance/conftest.py` (use `world_factory` with your own `plan_raw`/`budgets`/
`models_config` for anything the default `world` fixture does not give you). Read `r2_rules.md`, `r5_rules.md`, `r6_rules.md` FIRST --
`r6_rules.md` describes the fake rig you will use and the hard zero-quota rule. Then read `tests/acceptance/test_scenarios_demo.py` in
full: copy its style (docstrings that quote the exact blueprint sentence and requirement IDs, the assertions on `fake.calls`/
`fake.events`/`git`).

## 22.3, the failure test (blueprint.txt around `[p403]`/`[p404]`)
"Script the fake provider to return 429 with Retry-After, then 500s, then 401 for one model. The run must wait, resume, fall back
along the configured chain only, record the provider and model actually used, and mark the credential unhealthy, without losing or
duplicating cards."
This scenario is really testing `recovery.py`'s classification and decisions end to end through `controller.process_recovery`, driven
by a worker's `Crash`/`agent_fail` outcome whose error text matches each failure kind (read `recovery.classify_run`'s real regex rules
first: quote the exact substrings it looks for, e.g. "429", "rate limit", "500", "401", "invalid api key"). Build it as ONE card that
fails three times in sequence, each with a different classification, using `fw.crasher` or a hand-built `ScriptedWorker` with `Crash`
steps (read both in `worker.py`) whose `error` text you choose to classify as rate_limit, then infrastructure, then auth:
1. Rate limit (429, Retry-After): the run fails with rate-limit-shaped text. Per `recovery.decide`, this is `action="none"` (Hermes
   retries on its own) -- assert NO fresh-attempt card is created and the SAME card eventually succeeds on a later dispatch (give it a
   worker that fails once with this text then succeeds, via `fw.crasher(times=1, then=fw.good_coder(...))` or equivalent -- read the
   real persona signatures).
2. Infrastructure (500-shaped text): per `decide`, this is `action="resume"` after a backoff -- assert the SAME card resumes (its
   `worker_pid`/run count reflects a resume, not a new card) once the backoff has elapsed (advance the fake clock past it with
   `fake.tick`), and that a card recorded as resumed is not duplicated (still one work card for this task throughout).
3. Auth (401-shaped text): per `decide`, this is `action="mark_credential_unhealthy"` -- assert the card becomes a QUESTION
   (`questions.list_questions` finds it, or `fake.card(id)["status"] == "blocked"` with the right reason) and a
   `credential_unhealthy` event was recorded (`fake.events(card_id)` or `events.recent` on `world.conn`, whichever actually carries it --
   check `recovery.py`'s real event kind name first).
"Fall back along the configured chain": with two candidate models declared for the coder role class in `world.models_config` (mirror
the shape `recovery.next_model`'s tests use, read `test_recovery.py` for the exact `models_config["models"]` row shape needed for a
role_class + `_candidate` pair), assert that a SECOND capability-shaped failure (not one of the three above; use a fourth failure
whose text has no recognisable shape, i.e. `classify_run` returns `unknown`, OR a capability-shaped one per `recovery.py`'s own rules,
read them) results in a fresh-attempt card, and that a THIRD (or second capability, per the real threshold) results in a switch-model
card pinned to the candidate model (`fake.card(new_id)["model_override"]` or however the fake records a `kanban_set_model` call --
check `fake.calls`). Never a model outside the two declared. Assert "the provider and model actually used are recorded" via
`usage.py`'s ingestion (a `fake.set_session_usage` call feeding a specific provider/model back, then confirm the ledger or an event
reflects it -- read `usage.ingest_run_usage`/`ingest_card_usage`'s real signatures and what they write). "Without losing or duplicating
cards": assert `len(fake.cards())` stays exactly what the plan implies at the end (count work cards, merge cards, and however many
fresh-attempt/fix cards this scenario legitimately created, no more).

## 22.9, the quota exhaustion test (blueprint.txt around `[p415]`/`[p416]`)
"Set the daily budget to 30 requests. The controller must park the cards it cannot afford, show the reset time, stay idle without
thrashing or probing the provider, and resume after the simulated reset with all state intact. It must never suggest or perform a
purchase."
Build a `world_factory` world with a `models_config` whose provider has a real daily limit (read `policy.check_budget`/`ledger`'s real
shapes for what a capped provider's config looks like, and how "today" is computed -- likely UTC date, so you may need to inject `now`
consistently between the fake clock and whatever the ledger reads; check `ledger.py`'s real function signatures for an injectable
`now`/date parameter before assuming one exists, and if it does not, say so in your report rather than writing a flaky test) and a
budget low enough (or a plan whose `estimated_requests` is high enough) that at least one card cannot be afforded. Then:
1. `controller.run_pass` (via `world.one_pass()` or `run_until`) parks the unaffordable card (`hermes.kanban_schedule`, i.e.
   `fake.card(id)["status"] == "scheduled"`), and the parked reason mentions the budget/reset (read `process_budget_gate`'s real
   parking-reason text).
2. Run several more passes (`fake.tick` between them, no real sleep): the card stays parked, no repeated identical `card_parked_for_
   budget` events pile up meaninglessly (read what "thrash" would look like: the real code should park once and leave it, not re-park
   every pass with a new event -- if it DOES re-emit an event every single pass, that may be existing, acceptable behavior; assert
   whatever the real code actually does, do not assert an aspiration the code does not meet, and note in your report if you think this
   is worth a future fix), and "never probes the provider" means no HTTP call happens at all in this whole test (nothing here starts
   `ases.fakes.provider`, which is itself the proof: if the code tried to reach a real network address the test would hang or error,
   not silently pass, so this assertion is implicit in the test using ONLY the fake board).
3. Simulate the reset: either advance the fake clock/injected `now` past UTC midnight (if `ledger.py` supports an injectable date) or,
   if it reads the real wall clock with no override, monkeypatch whatever `ledger.py` actually calls for "today" (read it first; do not
   guess) so the budget check now succeeds. Then confirm `process_unpark` (package CORE built this in round 5, read its real behavior in
   `controller.py`) returns the card to `ready` and it proceeds normally to completion, with every other piece of state (the OTHER
   cards' progress, the plan, the database) intact across the simulated reset.
4. "Never suggest or perform a purchase": there is no purchase mechanism anywhere in ASES to call, so assert this by absence -- no
   event, comment, or card body anywhere in this scenario contains the word "purchase", "buy", "credit card", or similar (scan
   `fake.snapshot()` for it), which is a real, checkable assertion even though it will obviously pass; it documents the guarantee.

## Report back
The usual report, plus: exactly which `models_config` shape and injectable `now`/date mechanism you used for the daily-budget reset (so
future scenarios reuse the same pattern), and whether `ledger.py` truly supports an injectable "today" or you had to monkeypatch it.
