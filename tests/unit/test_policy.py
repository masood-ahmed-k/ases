import pytest

from ases import policy


def test_public_allows_anything():
    policy.check_data_class("public", "openrouter", "some_free_endpoints_train")
    policy.check_data_class("public", "unorouter", "unknown")
    policy.check_data_class("public", "x", None)  # no exception = pass


@pytest.mark.parametrize("data_policy", ["no_training", "local_only", "zero_data_retention"])
def test_private_allows_safe_policies_once_verified(data_policy):
    policy.check_data_class("private", "some_provider", data_policy, verified_at="2026-09-19")


@pytest.mark.parametrize("data_policy", [
    "some_free_endpoints_train", "forwards_to_upstreams_that_may_train", "unknown", None,
])
def test_private_rejects_unsafe_policies(data_policy):
    with pytest.raises(policy.DataPolicyViolation, match="private"):
        policy.check_data_class("private", "openrouter", data_policy, verified_at="2026-09-19")


def test_confidential_only_allows_local_once_verified():
    policy.check_data_class("confidential", "local_ollama", "local_only", verified_at="2026-09-19")
    with pytest.raises(policy.DataPolicyViolation, match="confidential"):
        policy.check_data_class("confidential", "openrouter", "no_training", verified_at="2026-09-19")


# --- ASES-PRV-04: private/confidential also need an explicitly recorded verification date -----------------


@pytest.mark.parametrize("data_class, data_policy", [
    ("private", "no_training"), ("private", "local_only"), ("private", "zero_data_retention"),
    ("confidential", "local_only"),  # the only policy in policy._SAFE_FOR_CONFIDENTIAL
])
def test_a_compatible_policy_with_no_verification_date_is_still_refused(data_class, data_policy):
    """ASES-PRV-04: 'private/confidential projects require an EXPLICITLY VERIFIED provider data policy'.
    A policy string that would otherwise qualify is not enough on its own for the two stricter classes; the
    default (verified_at omitted) must still raise, naming the missing field, not the unrelated policy
    string (which is not the problem here)."""
    with pytest.raises(policy.DataPolicyViolation, match="no recorded verification date") as caught:
        policy.check_data_class(data_class, "some_provider", data_policy)
    assert "some_provider" in str(caught.value)
    assert "data_policy_verified_at" in str(caught.value)


@pytest.mark.parametrize("data_class", ["private", "confidential"])
def test_a_verification_date_of_empty_string_is_treated_as_missing(data_class):
    data_policy = "local_only"  # safe for both private and confidential
    with pytest.raises(policy.DataPolicyViolation, match="no recorded verification date"):
        policy.check_data_class(data_class, "some_provider", data_policy, verified_at="")


def test_public_never_needs_a_verification_date():
    """ASES-PRV-04 only tightens private/confidential; public is unaffected, with or without the keyword."""
    policy.check_data_class("public", "openrouter", "some_free_endpoints_train")
    policy.check_data_class("public", "openrouter", "some_free_endpoints_train", verified_at=None)


def test_an_unsafe_policy_is_still_refused_for_its_own_reason_even_with_a_verification_date():
    """The policy-string check runs first: a provider that is not confirmed safe at all is refused for that
    reason, not silently waved through just because someone recorded a verification date for it."""
    with pytest.raises(policy.DataPolicyViolation, match="not confirmed safe") as caught:
        policy.check_data_class("private", "openrouter", "some_free_endpoints_train", verified_at="2026-09-19")
    assert "no recorded verification date" not in str(caught.value)


def test_unknown_data_class_raises():
    with pytest.raises(policy.DataPolicyViolation, match="unknown data_class"):
        policy.check_data_class("super-secret", "x", "no_training")


def test_current_real_providers_are_not_yet_safe_for_private():
    """Documents the actual current state (2026-09-18), not a hypothetical: neither working
    provider qualifies for private data yet. If this test ever starts failing because someone
    "fixed" it by loosening _SAFE_FOR_PRIVATE without a real policy change, that's the bug."""
    with pytest.raises(policy.DataPolicyViolation):
        policy.check_data_class("private", "unorouter", "forwards_to_upstreams_that_may_train")
    with pytest.raises(policy.DataPolicyViolation):
        policy.check_data_class("private", "openrouter", "some_free_endpoints_train")


def test_resolve_assignee_unknown_role_raises():
    with pytest.raises(policy.UnknownRoleError):
        policy.resolve_assignee("wizard", {"coder": "coder-1"})


def test_resolve_assignee_known_role():
    assert policy.resolve_assignee("coder", {"coder": "coder-1"}) == "coder-1"


def test_calendar_time_per_model_rpm_uses_only_that_models_requests():
    """UnoRouter paces each model independently -- a second model's volume must not inflate the
    estimate for this one."""
    limits = {"unorouter": {"limits": {"per_model_rpm": 1}}}
    minutes = policy.estimate_calendar_minutes(
        limits, "unorouter", requests_for_model=5, requests_for_provider=30,
    )
    assert minutes == 5.0


def test_calendar_time_account_rpm_uses_full_provider_total():
    """OpenRouter's rpm is account-wide, so it paces against every model's requests combined."""
    limits = {"openrouter": {"limits": {"rpm": 20}}}
    minutes = policy.estimate_calendar_minutes(
        limits, "openrouter", requests_for_model=5, requests_for_provider=25,
    )
    assert minutes == 1.25


def test_calendar_time_none_when_provider_declares_no_rate_limit():
    limits = {"opencode_free": {"limits": {}}}
    assert policy.estimate_calendar_minutes(
        limits, "opencode_free", requests_for_model=5, requests_for_provider=5,
    ) is None


def test_calendar_time_none_when_rpm_is_zero():
    limits = {"unorouter": {"limits": {"per_model_rpm": 0}}}
    assert policy.estimate_calendar_minutes(
        limits, "unorouter", requests_for_model=5, requests_for_provider=5,
    ) is None
