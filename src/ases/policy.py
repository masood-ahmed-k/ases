"""Role-to-provider/model policy (section 9.1: policy.py; section 11).

Phase 3 scope is deliberately small: resolve a plan task's role to the Hermes profile that plays it
(config/swarm.yaml's roles: map), and check the request budget before letting a card go ready. Model
selection itself is already fixed per-profile in each profile's own config.yaml (Phase 2's pins) --
this module does not re-pick models, it enforces the budget gate in front of dispatch.
"""
from __future__ import annotations

import dataclasses

from . import ledger


class UnknownRoleError(Exception):
    pass


def resolve_assignee(role: str, roles_map: dict) -> str:
    if role not in roles_map:
        raise UnknownRoleError(f"no profile mapped for role '{role}' in config/swarm.yaml roles:")
    return roles_map[role]


@dataclasses.dataclass(frozen=True)
class ProfileProvider:
    """Which provider/model a profile is pinned to, for budget checks (from config/models.yaml)."""
    provider: str
    model: str


def profile_provider(role: str, models_config: dict) -> ProfileProvider | None:
    """Best-effort lookup: find the pinned model whose role_class matches this plan role."""
    for m in models_config.get("models", []):
        if m.get("role_class") == role and m.get("pinned"):
            return ProfileProvider(m["provider"], m["model"])
    return None


# A provider is safe for a data class only if its declared policy says so explicitly (section 21.2).
# Absence of a safe marker means "not confirmed safe", not "assumed safe" -- ASES-PRV-01/03: the
# controller never relaxes this to keep work flowing. Neither UnoRouter nor OpenRouter's current
# declared policies qualify for "private" (both say upstream/some endpoints may train); that's a
# real, current fact, not a bug in this check -- see docs/architecture.md's known-gaps section.
_SAFE_FOR_PRIVATE = frozenset({"no_training", "local_only", "zero_data_retention"})
_SAFE_FOR_CONFIDENTIAL = frozenset({"local_only"})


class DataPolicyViolation(Exception):
    pass


def check_data_class(data_class: str, provider: str, provider_data_policy: str | None) -> None:
    """ASES-PRV-01/02: enforced before any other routing rule. Raises DataPolicyViolation with a
    clear reason rather than returning a bool -- a silently-ignored False here is exactly the failure
    mode ASES-PRV-03 exists to prevent."""
    policy = (provider_data_policy or "unknown").lower()
    if data_class == "public":
        return
    if data_class == "private":
        if policy not in _SAFE_FOR_PRIVATE:
            raise DataPolicyViolation(
                f"provider '{provider}' (data_policy={policy!r}) is not confirmed safe for "
                f"data_class=private; needs one of {sorted(_SAFE_FOR_PRIVATE)}, or use a local model"
            )
        return
    if data_class == "confidential":
        if policy not in _SAFE_FOR_CONFIDENTIAL:
            raise DataPolicyViolation(
                f"provider '{provider}' (data_policy={policy!r}) is not approved for "
                f"data_class=confidential; needs {sorted(_SAFE_FOR_CONFIDENTIAL)} or explicit "
                f"written user approval for this project"
            )
        return
    raise DataPolicyViolation(f"unknown data_class {data_class!r}")


def check_budget(
    conn, provider_limits: dict, provider: str, estimated_requests: int, *, budgets: dict
) -> ledger.Affordability:
    return ledger.can_afford(
        conn, provider_limits, provider, estimated_requests,
        reserve_percent=budgets.get("daily_reserve_percent", 0),
        extra_reserve=budgets.get("review_reserve_requests", 0),
    )


def estimate_calendar_minutes(
    provider_limits: dict, provider: str, *, requests_for_model: int, requests_for_provider: int,
) -> float | None:
    """ASES-CAP-04/ASES-REV-03's 'calendar time': how long a plan will take to actually run against a
    provider's own pace limit. This is pacing, never a budget decision -- check_budget/can_afford above
    is what refuses a plan; this only estimates a wait. Returns None if the provider declares no rate
    limit to pace against.

    UnoRouter publishes per_model_rpm: each pinned model is paced independently, so the estimate uses
    only the requests going to that one model. OpenRouter publishes a single account-wide rpm shared by
    every model on it, so the estimate uses the provider's full request total instead.
    """
    limits = provider_limits.get(provider, {}).get("limits", {})
    if "per_model_rpm" in limits:
        rpm = limits["per_model_rpm"]
        return requests_for_model / rpm if rpm > 0 else None
    if "rpm" in limits:
        rpm = limits["rpm"]
        return requests_for_provider / rpm if rpm > 0 else None
    return None
