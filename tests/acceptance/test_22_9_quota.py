"""Acceptance 22.9: quota exhaustion (blueprint.txt [p415]/[p416]; ASES-CAP-03).

"Set the daily budget to 30 requests. The controller must park the cards it cannot afford, show the reset time,
stay idle without thrashing or probing the provider, and resume after the simulated reset with all state intact.
It must never suggest or perform a purchase."

This is the PROACTIVE half of ASES-CAP-03 (controller.process_budget_gate / _affordable_now), not recovery.py's
reactive QUOTA classification (that is a worker's run failing with quota-shaped text after it started, package
AC-A's 22.3 territory): here a card is never even dispatched because the ledger already shows it cannot be
afforded. "Never probes the provider" is implicit throughout: this file never starts ases.fakes.provider (the fake
HTTP server), so a network call anywhere in the code under test would hang or error the whole suite, not silently
pass.

ledger.py now has an injectable clock (round 14, package CLOCK): every public function that reads "today" takes
its own `now` keyword (record_usage, usage_today, usage_today_for_provider, remaining_today, can_afford), and
`ledger.default_now` is the one public, documented fallback they resolve to when a call gives no `now` of its
own. run_pass's own `now` (which the World/FakeHermes clock supplies) still is not threaded into
process_budget_gate/process_unpark (only into process_recovery/process_bounds/process_finalize; that wiring is
controller.py's, outside this package's files), so "simulate the reset" here still means pointing ledger's clock
at a fixed day for the whole pass loop -- but now that is `monkeypatch.setattr(ledger, "default_now", ...)`, a
public attribute meant to be replaced this way, never `ledger._today` itself, which is a private implementation
detail and no longer touched by this file at all.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from ases import events, ledger, report
from ases.fakes import worker as fw

CAPPED_PROVIDER = "fake-capped"
CAPPED_MODEL = "fake-coder"
OTHER_PROVIDER = "fake"
REVIEWER_MODEL = "fake-reviewer"

MODELS_CONFIG = {
    "providers": {
        CAPPED_PROVIDER: {"limits": {"per_day": 30}},   # "Set the daily budget to 30 requests"
        OTHER_PROVIDER: {"limits": {}},                  # the reviewer's provider: no cap, so only the coder's
    },                                                    # budget is what parks a card in this test
    "models": [
        {"provider": CAPPED_PROVIDER, "model": CAPPED_MODEL, "role_class": "coder", "pinned": True},
        {"provider": OTHER_PROVIDER, "model": REVIEWER_MODEL, "role_class": "reviewer", "pinned": True},
    ],
}

# No reserve held back, so the arithmetic is exactly the daily cap minus what the ledger shows used today
# (policy.check_budget / ledger.can_afford): simpler to reason about than the project defaults' 10%/20-request
# reserve, and this test is about the park/unpark mechanism, not the reserve sizing.
BUDGETS = {"daily_reserve_percent": 0, "review_reserve_requests": 0}

# T1 needs more than is left after PRE_USED is already spent today (30 - 10 = 20 usable < 25 needed): parked.
# T2 needs far less, and is independent of T1 (no depends_on), so it is free to finish while T1 sits parked --
# "the OTHER cards' progress" the package asks to see stay intact across the whole park/unpark cycle.
PRE_USED = 10
QUOTA_PLAN = {
    "project": "acceptance",
    "integration_branch": "integration",
    "gate_profiles": {"trivial": ["echo ok"]},
    "tasks": [
        {"key": "T1", "title": "add a", "role": "coder", "depends_on": [], "touches": ["a.py"],
         "acceptance": ["a.py defines add(x, y) returning x + y"], "gate_profile": "trivial",
         "estimated_requests": 25},
        {"key": "T2", "title": "add b", "role": "coder", "depends_on": [], "touches": ["b.py"],
         "acceptance": ["b.py defines sub(x, y) returning x - y"], "gate_profile": "trivial",
         "estimated_requests": 5},
    ],
}


def _payloads(conn, kind: str) -> list[dict]:
    """The payloads of every ASES-level event of `kind`, oldest first."""
    rows = [e for e in events.recent(conn, limit=500) if e["kind"] == kind]
    rows.reverse()
    return [json.loads(row["payload"]) for row in rows]


def test_22_9_a_card_over_the_daily_budget_is_parked_shown_and_resumes_after_the_reset(
    world_factory, run_until, git, monkeypatch,
):
    """Blueprint 22.9 ([p415]/[p416]). ASES-CAP-03."""
    # day_two is the simulated reset: the next UTC day, a fresh ledger row, nothing carried over from day_one.
    day_one_moment = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
    day_two_moment = datetime(2026, 1, 16, 12, 0, tzinfo=timezone.utc)
    # ledger.default_now (public, documented, meant to be replaced this way) is the fallback every ledger call
    # below resolves to when it is not given its own `now` -- this is what points the WHOLE pass loop (which
    # never threads its own `now` into process_budget_gate) at a simulated day, without touching ledger._today.
    monkeypatch.setattr(ledger, "default_now", lambda: day_one_moment)

    world = world_factory(plan_raw=QUOTA_PLAN, budgets=BUDGETS, models_config=MODELS_CONFIG)
    conn = world.conn
    # Some of today's 30 requests are already spent (another card's session, ingested on an earlier pass in a real
    # run): recorded straight into the ledger, the one thing process_budget_gate actually reads.
    ledger.record_usage(conn, CAPPED_PROVIDER, CAPPED_MODEL, PRE_USED)

    pairs = world.create_cards()
    t1, t2 = pairs["T1"], pairs["T2"]

    # --- park: the first pass parks T1 (25 needed, only 20 usable) and leaves T2 alone (5 needed, affordable).
    summary = world.one_pass()
    assert summary["parked"] == ["T1"]
    assert world.card(t1.work_card_id)["status"] == "scheduled"
    parked_events = _payloads(conn, "card_parked_for_budget")
    assert len(parked_events) == 1 and parked_events[0]["task_key"] == "T1"
    assert parked_events[0]["reason"].startswith("budget:") and "25" in parked_events[0]["reason"]

    # --- T2 is unaffected and runs to completion while T1 sits parked.
    run_until(world, lambda w: w.card(t2.merge_card_id)["status"] == "done")
    assert git(world, "show", "integration:b.py").strip() != ""

    # --- stay idle without thrashing: several more passes (ticking the fake clock, never the real one) while T1
    # remains parked. process_budget_gate only ever looks at 'ready' cards, so a scheduled card literally cannot be
    # re-parked by it; process_unpark looks at it every pass but writes nothing while it is still unaffordable, so
    # the one park event from above stays the only one (asserting what the real code does, not an aspiration it
    # does not meet, per r6_wp_ac_a.md: this is the actual mechanism, not a guess).
    for _ in range(5):
        world.fake.tick(20)
        world.one_pass()
    assert world.card(t1.work_card_id)["status"] == "scheduled"
    assert len(_payloads(conn, "card_parked_for_budget")) == 1
    assert _payloads(conn, "card_unparked") == []
    # "Never probes the provider": nothing here ever started ases.fakes.provider, so there is no HTTP server this
    # code could have reached even if it tried -- the absence of that fixture IS the proof.

    # --- show the reset time: the report the person would read names it explicitly. `now` is the report's own
    # clock (report.py: "the day used for ... the next reset"), passed explicitly here so it reads day_one
    # regardless of the real wall clock; report.py now hands this same `now` to ledger.usage_today_for_provider
    # too (round 14, package CLOCK), so the report's own budget numbers agree with it as well, not just the date.
    rep = report.build_report(
        world.board, world.plan, world.project, world.models_config, conn, now=day_one_moment,
    )
    assert rep["budget"]["next_reset"] == "2026-01-16T00:00:00+00:00"
    parked_in_report = [row for row in rep["budget"]["parked"] if row["task_key"] == "T1"]
    assert len(parked_in_report) == 1 and parked_in_report[0]["card_id"] == t1.work_card_id
    assert parked_in_report[0]["reason"].startswith("budget:")

    # --- never suggest or perform a purchase: there is no purchase mechanism in ASES to call, so this is checked
    # by absence across the whole board and event log, which is a real, checkable guarantee even though it will
    # obviously pass (r6_wp_ac_a.md's own framing for this exact assertion).
    blob = json.dumps(world.fake.snapshot(), default=str).lower()
    for word in ("purchase", "buy credits", "credit card", "upgrade your plan"):
        assert word not in blob

    # --- simulate the reset: the next UTC day, a fresh ledger row (nothing carried over from day_one's usage).
    monkeypatch.setattr(ledger, "default_now", lambda: day_two_moment)
    assert ledger.usage_today_for_provider(conn, CAPPED_PROVIDER) == 0   # the day rolled over, as the ledger sees it

    run_until(world, lambda w: w.card(t1.work_card_id)["status"] != "scheduled")
    assert _payloads(conn, "card_unparked")[-1]["task_key"] == "T1"

    # --- resumes with all state intact: T1 proceeds normally to completion, and T2's earlier progress (the plan,
    # the database, its own merged commit) was never touched by any of this.
    run_until(world, lambda w: w.all_merge_cards_done())
    assert git(world, "show", "integration:a.py").strip() != ""
    assert world.card(t2.merge_card_id)["status"] == "done"
    still_t2 = conn.execute(
        "SELECT work_card_id, merge_card_id FROM plan_tasks WHERE project = ? AND task_key = 'T2'",
        (world.plan.project,),
    ).fetchone()
    assert (still_t2["work_card_id"], still_t2["merge_card_id"]) == (t2.work_card_id, t2.merge_card_id)
    assert conn.execute(
        "SELECT COUNT(*) FROM merge_records WHERE task_key = 'T2'"
    ).fetchone()[0] == 1   # T2 was merged exactly once, not re-merged by anything the reset touched


def test_22_9_process_budget_gate_never_parks_a_card_that_is_not_ready(world_factory, monkeypatch):
    """A narrower check of the same mechanism: process_budget_gate only ever inspects the 'ready' lane
    (hermes.kanban_list(board, status="ready")), so a card that is already scheduled, blocked or running is never
    a candidate for a SECOND park event, which is the real reason 22.9's idle loop above never thrashes."""
    monkeypatch.setattr(ledger, "default_now", lambda: datetime(2026, 2, 1, 12, 0, tzinfo=timezone.utc))
    world = world_factory(
        plan_raw={**QUOTA_PLAN, "tasks": QUOTA_PLAN["tasks"][:1]}, budgets=BUDGETS, models_config=MODELS_CONFIG,
    )
    conn = world.conn
    ledger.record_usage(conn, CAPPED_PROVIDER, CAPPED_MODEL, PRE_USED)
    t1 = world.create_cards()["T1"]

    world.one_pass()
    assert world.card(t1.work_card_id)["status"] == "scheduled"

    for _ in range(4):
        world.fake.tick(20)
        world.one_pass()
    assert len(_payloads(conn, "card_parked_for_budget")) == 1
