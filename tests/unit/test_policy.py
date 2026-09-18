import pytest

from ases import policy


def test_public_allows_anything():
    policy.check_data_class("public", "openrouter", "some_free_endpoints_train")
    policy.check_data_class("public", "unorouter", "unknown")
    policy.check_data_class("public", "x", None)  # no exception = pass


@pytest.mark.parametrize("data_policy", ["no_training", "local_only", "zero_data_retention"])
def test_private_allows_safe_policies(data_policy):
    policy.check_data_class("private", "some_provider", data_policy)


@pytest.mark.parametrize("data_policy", [
    "some_free_endpoints_train", "forwards_to_upstreams_that_may_train", "unknown", None,
])
def test_private_rejects_unsafe_policies(data_policy):
    with pytest.raises(policy.DataPolicyViolation, match="private"):
        policy.check_data_class("private", "openrouter", data_policy)


def test_confidential_only_allows_local():
    policy.check_data_class("confidential", "local_ollama", "local_only")
    with pytest.raises(policy.DataPolicyViolation, match="confidential"):
        policy.check_data_class("confidential", "openrouter", "no_training")


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
