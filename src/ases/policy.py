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


def check_budget(
    conn, provider_limits: dict, provider: str, estimated_requests: int, *, budgets: dict
) -> ledger.Affordability:
    return ledger.can_afford(
        conn, provider_limits, provider, estimated_requests,
        reserve_percent=budgets.get("daily_reserve_percent", 0),
        extra_reserve=budgets.get("review_reserve_requests", 0),
    )
