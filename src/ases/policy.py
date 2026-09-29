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


def check_data_class(
    data_class: str, provider: str, provider_data_policy: str | None, *, verified_at: str | None = None,
) -> None:
    """ASES-PRV-01/02: enforced before any other routing rule. Raises DataPolicyViolation with a
    clear reason rather than returning a bool -- a silently-ignored False here is exactly the failure
    mode ASES-PRV-03 exists to prevent.

    ASES-PRV-04 (section 21.2): "private/confidential projects require an EXPLICITLY VERIFIED provider
    data policy". A compatible policy string alone is no longer enough for the two stricter data classes:
    `verified_at` must also be given (config/models.yaml's `providers.<name>.data_policy_verified_at`,
    an ISO date string recording when a human actually checked the policy, ASES-VER-01's convention).
    `verified_at` is keyword-only and defaults to None so the signature stays backward compatible, but the
    behaviour is deliberately NOT relaxed for a caller that omits it: `public` never needs a verification
    date, but `private`/`confidential` raise DataPolicyViolation naming exactly what is missing, the same
    way an unsafe policy string does. A caller that has not been updated to read and pass
    data_policy_verified_at will therefore see every private/confidential candidate refused, even one with
    a compatible policy; that is correct (ASES-PRV-03: the data class is never relaxed to keep something
    working), not a bug in this function."""
    policy = (provider_data_policy or "unknown").lower()
    if data_class == "public":
        return
    if data_class == "private":
        if policy not in _SAFE_FOR_PRIVATE:
            raise DataPolicyViolation(
                f"provider '{provider}' (data_policy={policy!r}) is not confirmed safe for "
                f"data_class=private; needs one of {sorted(_SAFE_FOR_PRIVATE)}, or use a local model"
            )
        if not verified_at:
            raise DataPolicyViolation(
                f"provider '{provider}' has a compatible policy but no recorded verification date; "
                f"add data_policy_verified_at to its config/models.yaml entry"
            )
        return
    if data_class == "confidential":
        if policy not in _SAFE_FOR_CONFIDENTIAL:
            raise DataPolicyViolation(
                f"provider '{provider}' (data_policy={policy!r}) is not approved for "
                f"data_class=confidential; needs {sorted(_SAFE_FOR_CONFIDENTIAL)} or explicit "
                f"written user approval for this project"
            )
        if not verified_at:
            raise DataPolicyViolation(
                f"provider '{provider}' has a compatible policy but no recorded verification date; "
                f"add data_policy_verified_at to its config/models.yaml entry"
            )
        return
    raise DataPolicyViolation(f"unknown data_class {data_class!r}")


def effective_policy(
    models_config: dict, provider: str, model: str | None = None,
) -> tuple[str | None, str | None]:
    """(data_policy, data_policy_verified_at) resolved TOGETHER, as one pair, never mixed across levels
    (STOPDOC.md/PROVIDERS.md finding, round 19 package STOPGATES): a config/models.yaml model row may
    declare its OWN `data_policy`, distinct from its provider's declared policy (for example one endpoint
    of a router that does not train, while the provider as a whole does). That row-level claim is a
    DIFFERENT verified fact from the provider-level one, so it needs its OWN `data_policy_verified_at`;
    borrowing the provider's date (recovery.py used to do exactly this, at the row's own request.get(...)
    or provider.get(...) idiom) would let a human's verification of the PROVIDER's policy stand in for a
    verification of the ROW's different policy that never happened, which is exactly the silent relaxation
    ASES-PRV-03 forbids. So: when the row has its own data_policy, its own verified_at is returned even
    when that is None -- the caller then sees a compatible-but-unverified policy and check_data_class
    refuses it, rather than quietly reading the provider's date instead. Only a row with NO data_policy of
    its own falls back to the provider triple (policy AND verified_at together).

    `model` is optional so a provider-only call (no specific model in hand) still works exactly like the
    old provider-level lookups callers used to write out by hand."""
    if model is not None:
        for row in models_config.get("models", []) or ():
            if row.get("provider") == provider and row.get("model") == model and row.get("data_policy"):
                return row.get("data_policy"), row.get("data_policy_verified_at")
    provider_row = (models_config.get("providers") or {}).get(provider) or {}
    return provider_row.get("data_policy"), provider_row.get("data_policy_verified_at")


def check_roles(data_class: str, models_config: dict, roles: list[str]) -> None:
    """ASES-PRV-01/03 (STOPDOC.md topic A, item 2 / PROVIDERS.md finding 3): check_data_class used to be
    reachable only through the plan's own task roles (coder, tester), so `swarm plan` (the Lead reads the
    whole repository) and `swarm critique` (the Reviewer reads the plan) were never checked at all -- a
    private or confidential project could still hand its content to an unsafe provider through either one.
    This checks every named role's PINNED provider/model (policy.profile_provider), using effective_policy
    so a row-level policy is judged on its own verification, never the provider's (see that function).

    Raises the first violation, prefixed with the role and its resolved provider/model so a refusal names
    who was refused, not just why. A role with no pinned model (profile_provider returns None) has nothing
    to check and is skipped, same as every other data-class call site in this codebase."""
    for role in roles:
        pp = profile_provider(role, models_config)
        if pp is None:
            continue
        data_policy, verified_at = effective_policy(models_config, pp.provider, pp.model)
        try:
            check_data_class(data_class, pp.provider, data_policy, verified_at=verified_at)
        except DataPolicyViolation as exc:
            raise DataPolicyViolation(f"role {role!r} ({pp.provider}/{pp.model}): {exc}") from exc


def is_paid_model(models_config: dict, provider: str, model: str) -> bool:
    """STOPDOC.md topic A, item 8 (ASES-DOC-04, section 16 STOP CONDITION, category 1 'spends money'):
    blueprint.txt line 509 (table 27, section 16) requires Claude Code to "stop and ask the user before any
    action that spends money"; today nothing in config/models.yaml marks a model as billed, so a paid SKU
    is kept out only by a naming convention (":free") nobody enforces in code. An optional, explicit
    `paid: true` on a models[] row is the one new fact this checks for; absent or false means free."""
    for row in models_config.get("models", []) or ():
        if row.get("provider") == provider and row.get("model") == model:
            return bool(row.get("paid"))
    return False


class PaidModelViolation(Exception):
    pass


def check_roles_not_paid(models_config: dict, roles: list[str], *, allow_paid: bool) -> None:
    """ASES-DOC-04 (section 16 STOP CONDITION, fix round 1 on package STOPGATES): is_paid_model was reachable
    only through a plan's own task roles (cli._estimate_lines's per-task loop, and controller._affordable_now
    at dispatch time) -- both walk plan.tasks, and the Lead is never a plan.tasks role (it authors tasks, it
    is not one), so a paid, pinned Lead model fell through every one of the blueprint's three named enforcement
    points (Gate P, the budget gate, recovery.next_model) whenever `cli._run_lead` was called directly: swarm
    plan's one-shot call and swarm critique --auto-replan's re-plan call. Neither call site has a plan.tasks
    list to walk at all (swarm plan has no plan yet; a re-plan's whole point is that the old one is being
    rewritten), so this is role-level like check_roles, not task-level like the per-task loop -- and it is one
    shared function so a future direct-role call site cannot forget the check independently, the same reason
    check_roles itself exists instead of each call site re-deriving the data-class check by hand.

    `allow_paid` is the caller's already-read swarm.yaml budgets.allow_paid_models (default False): True skips
    every check below without even resolving a role's pinned model, matching is_paid_model's own callers.
    A role with no pinned model (profile_provider returns None) has nothing to check and is skipped, same as
    check_roles. Raises the first violation, prefixed with the role and its resolved provider/model so a
    refusal names who was refused, not just why (check_roles's own convention)."""
    if allow_paid:
        return
    for role in roles:
        pp = profile_provider(role, models_config)
        if pp is None:
            continue
        if is_paid_model(models_config, pp.provider, pp.model):
            raise PaidModelViolation(
                f"role {role!r} resolves to model {pp.provider}/{pp.model}, which is billed (models.yaml "
                f"paid: true); set budgets.allow_paid_models: true in config/swarm.yaml to allow it"
            )


def check_budget(
    conn, provider_limits: dict, provider: str, estimated_requests: int, *, budgets: dict
) -> ledger.Affordability:
    """ASES-CAP-03. A budgets mapping that omits daily_reserve_percent still reserves the blueprint's 10
    percent (ledger.DEFAULT_DAILY_RESERVE_PERCENT), the same fallback bounds.Bounds and report's budget
    panel use; an explicit daily_reserve_percent of 0 means 0, never the default."""
    return ledger.can_afford(
        conn, provider_limits, provider, estimated_requests,
        reserve_percent=budgets.get("daily_reserve_percent", ledger.DEFAULT_DAILY_RESERVE_PERCENT),
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
