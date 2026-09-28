from datetime import datetime, timezone

from ases import db, ledger

LIMITS = {
    "openrouter": {"limits": {"per_day_default": 50, "per_day_after_credits": 1000}, "credits_purchased": False},
    "unorouter": {"limits": {"per_model_rpm": 1}},  # no daily cap published
}


def test_record_and_read_usage(tmp_path):
    conn = db.connect(tmp_path / "ases.db")
    ledger.record_usage(conn, "openrouter", "free-model", n=5)
    ledger.record_usage(conn, "openrouter", "free-model", n=3)
    assert ledger.usage_today(conn, "openrouter", "free-model") == 8


def test_usage_sums_across_models_for_provider_cap(tmp_path):
    conn = db.connect(tmp_path / "ases.db")
    ledger.record_usage(conn, "openrouter", "model-a", n=10)
    ledger.record_usage(conn, "openrouter", "model-b", n=15)
    assert ledger.usage_today_for_provider(conn, "openrouter") == 25


def test_daily_limit_before_and_after_credits():
    assert ledger.daily_limit(LIMITS, "openrouter") == 50
    purchased = {"openrouter": {**LIMITS["openrouter"], "credits_purchased": True}}
    assert ledger.daily_limit(purchased, "openrouter") == 1000


def test_daily_limit_none_for_uncapped_provider():
    assert ledger.daily_limit(LIMITS, "unorouter") is None


def test_can_afford_true_when_well_within_budget(tmp_path):
    conn = db.connect(tmp_path / "ases.db")
    result = ledger.can_afford(conn, LIMITS, "openrouter", 10, reserve_percent=10, extra_reserve=5)
    assert result.can_afford is True
    assert result.limit_today == 50


def test_can_afford_false_when_short(tmp_path):
    conn = db.connect(tmp_path / "ases.db")
    ledger.record_usage(conn, "openrouter", "m", n=40)
    result = ledger.can_afford(conn, LIMITS, "openrouter", 10, reserve_percent=10, extra_reserve=5)
    # 50 - 40 used = 10 remaining; minus 5 reserve minus 5 (10% of 50) = 0 usable < 10 requested
    assert result.can_afford is False
    assert "needs 10" in result.reason


def test_can_afford_unbounded_provider_always_true(tmp_path):
    conn = db.connect(tmp_path / "ases.db")
    ledger.record_usage(conn, "unorouter", "glm", n=10_000)
    result = ledger.can_afford(conn, LIMITS, "unorouter", 999999)
    assert result.can_afford is True
    assert result.remaining_today is None


def test_uses_after_credits_limit_once_flag_flipped(tmp_path):
    conn = db.connect(tmp_path / "ases.db")
    ledger.record_usage(conn, "openrouter", "m", n=100)
    purchased = {"openrouter": {**LIMITS["openrouter"], "credits_purchased": True}}
    result = ledger.can_afford(conn, purchased, "openrouter", 10)
    assert result.can_afford is True
    assert result.limit_today == 1000


def test_can_afford_reserves_10_percent_by_default_when_reserve_percent_is_omitted(tmp_path):
    """ASES-CAP-03: can_afford's own reserve_percent default is ledger.DEFAULT_DAILY_RESERVE_PERCENT (10), the
    same number every other reader of a missing daily_reserve_percent falls back to. A caller that means no
    reserve at all must say reserve_percent=0 explicitly (test_can_afford_true_when_well_within_budget and
    test_can_afford_false_when_short already cover an explicit reserve)."""
    conn = db.connect(tmp_path / "ases.db")
    assert ledger.DEFAULT_DAILY_RESERVE_PERCENT == 10
    omitted = ledger.can_afford(conn, LIMITS, "openrouter", 46)          # 50 - 10% (5) = 45 usable, needs 46
    explicit = ledger.can_afford(conn, LIMITS, "openrouter", 46, reserve_percent=10)
    assert omitted == explicit
    assert omitted.can_afford is False
    zero_reserve = ledger.can_afford(conn, LIMITS, "openrouter", 46, reserve_percent=0)
    assert zero_reserve.can_afford is True  # an explicit 0 still means 0, never the default


# --- round 14, package CLOCK: the injectable clock (ASES-CAP-03's daily-reset arithmetic, section 9.3). ---


def test_today_defaults_to_the_real_utc_day_with_no_now_given():
    """No behaviour change: with no `now` and no default_now override, `_today()` still reads the real wall
    clock, exactly as it did before this module had an injectable clock at all."""
    assert ledger._today() == datetime.now(timezone.utc).strftime("%Y-%m-%d")


def test_explicit_now_scopes_every_read_and_write_to_that_calendar_day(tmp_path):
    """Every public function takes its own `now`: a call with an explicit `now` reads and writes that UTC day,
    never the real wall clock, with no monkeypatching at all."""
    conn = db.connect(tmp_path / "ases.db")
    day_one = datetime(2026, 3, 1, 10, 0, tzinfo=timezone.utc)
    day_two = datetime(2026, 3, 2, 10, 0, tzinfo=timezone.utc)

    ledger.record_usage(conn, "openrouter", "m", n=9, now=day_one)
    assert ledger.usage_today(conn, "openrouter", "m", now=day_one) == 9
    assert ledger.usage_today_for_provider(conn, "openrouter", now=day_one) == 9
    # A different day's read sees none of it: the row is scoped by day, not carried forward.
    assert ledger.usage_today(conn, "openrouter", "m", now=day_two) == 0
    assert ledger.usage_today_for_provider(conn, "openrouter", now=day_two) == 0

    remaining = ledger.remaining_today(conn, LIMITS, "openrouter", now=day_one)
    assert remaining == 50 - 9
    afford = ledger.can_afford(conn, LIMITS, "openrouter", 5, reserve_percent=0, now=day_one)
    assert afford.can_afford is True and afford.remaining_today == 41


def test_utc_day_boundary_usage_counts_against_the_day_it_happened_on(tmp_path):
    """ASES-CAP-03's daily-reset arithmetic at the actual boundary: usage recorded at 23:59:59 UTC counts
    against that calendar day, and one second later, at 00:00:00 the next UTC day, the ledger has reset (a
    fresh day, nothing carried over), exactly the case the register's note on ASES-CAP-03 said could only be
    tested by monkeypatching ledger._today before this round."""
    conn = db.connect(tmp_path / "ases.db")
    end_of_day = datetime(2026, 3, 1, 23, 59, 59, tzinfo=timezone.utc)
    start_of_next_day = datetime(2026, 3, 2, 0, 0, 0, tzinfo=timezone.utc)

    ledger.record_usage(conn, "openrouter", "m", n=30, now=end_of_day)
    assert ledger.usage_today(conn, "openrouter", "m", now=end_of_day) == 30
    assert ledger.usage_today_for_provider(conn, "openrouter", now=end_of_day) == 30
    assert ledger.remaining_today(conn, LIMITS, "openrouter", now=end_of_day) == 50 - 30

    # One second later: a new UTC day, a fresh ledger row, the cap fully restored.
    assert ledger.usage_today(conn, "openrouter", "m", now=start_of_next_day) == 0
    assert ledger.usage_today_for_provider(conn, "openrouter", now=start_of_next_day) == 0
    assert ledger.remaining_today(conn, LIMITS, "openrouter", now=start_of_next_day) == 50

    # Usage recorded on the new day never touches the old day's row.
    ledger.record_usage(conn, "openrouter", "m", n=12, now=start_of_next_day)
    assert ledger.usage_today(conn, "openrouter", "m", now=start_of_next_day) == 12
    assert ledger.usage_today(conn, "openrouter", "m", now=end_of_day) == 30


def test_today_treats_a_naive_now_as_already_utc():
    """A naive `now` (no tzinfo) is assumed to already be UTC, the same convention report._utc documents for
    every other clock parameter in this codebase, so a caller cannot accidentally shift the day by feeding in
    a timezone-naive moment."""
    naive = datetime(2026, 4, 10, 23, 0)
    aware = datetime(2026, 4, 10, 23, 0, tzinfo=timezone.utc)
    assert ledger._today(naive) == ledger._today(aware) == "2026-04-10"


def test_default_now_is_the_fallback_used_when_a_call_gives_no_now_of_its_own(tmp_path, monkeypatch):
    """`default_now` (a public, documented attribute, not a private function) is the one seam a caller with no
    `now` of its own falls back to. Replacing it points every subsequent call at a simulated day, exactly what
    tests/acceptance/test_22_9_quota.py now does instead of monkeypatching ledger._today directly."""
    conn = db.connect(tmp_path / "ases.db")
    fixed = datetime(2026, 5, 1, 8, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(ledger, "default_now", lambda: fixed)

    ledger.record_usage(conn, "openrouter", "m", n=4)          # no now= given: falls back to default_now()
    assert ledger.usage_today(conn, "openrouter", "m") == 4
    assert ledger.usage_today(conn, "openrouter", "m", now=fixed) == 4

    # An explicit `now` on a single call still wins over the patched default.
    other_day = datetime(2026, 5, 2, 8, 0, tzinfo=timezone.utc)
    assert ledger.usage_today(conn, "openrouter", "m", now=other_day) == 0
