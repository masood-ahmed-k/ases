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
