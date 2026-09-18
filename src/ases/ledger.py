"""Persisted request ledger and budget-aware parking (section 9.1: ledger.py; section 5.4, 9.3).

Every agent tool call costs one request against some provider's daily quota. This module is the single
place that counts them, so the controller can refuse to start a card it cannot afford (ASES-CAP-03)
instead of finding out mid-run. Nothing here calls a provider; callers report usage after the fact.
"""
from __future__ import annotations

import dataclasses
import sqlite3
from datetime import datetime, timezone


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def record_usage(conn: sqlite3.Connection, provider: str, model: str, n: int = 1) -> None:
    if n <= 0:
        raise ValueError("n must be positive")
    today = _today()
    conn.execute(
        """
        INSERT INTO requests_ledger (provider, model, utc_date, count, updated_at)
        VALUES (?, ?, ?, ?, datetime('now'))
        ON CONFLICT(provider, model, utc_date) DO UPDATE SET
            count = count + excluded.count,
            updated_at = datetime('now')
        """,
        (provider, model, today, n),
    )


def usage_today(conn: sqlite3.Connection, provider: str, model: str) -> int:
    row = conn.execute(
        "SELECT count FROM requests_ledger WHERE provider = ? AND model = ? AND utc_date = ?",
        (provider, model, _today()),
    ).fetchone()
    return row["count"] if row else 0


def usage_today_for_provider(conn: sqlite3.Connection, provider: str) -> int:
    """Sum across every model on this provider -- OpenRouter's daily cap is per account, not per model."""
    row = conn.execute(
        "SELECT COALESCE(SUM(count), 0) AS total FROM requests_ledger WHERE provider = ? AND utc_date = ?",
        (provider, _today()),
    ).fetchone()
    return row["total"]


def daily_limit(provider_limits: dict, provider: str) -> int | None:
    """None means unlimited/unspecified (e.g. UnoRouter, which publishes no daily cap)."""
    entry = provider_limits.get(provider, {})
    limits = entry.get("limits", {})
    if "per_day_after_credits" in limits and "per_day_default" in limits:
        return limits["per_day_after_credits"] if entry.get("credits_purchased") else limits["per_day_default"]
    return limits.get("per_day")


@dataclasses.dataclass(frozen=True)
class Affordability:
    can_afford: bool
    remaining_today: int | None    # None = no known daily cap for this provider
    limit_today: int | None
    reason: str


def remaining_today(conn: sqlite3.Connection, provider_limits: dict, provider: str) -> int | None:
    limit = daily_limit(provider_limits, provider)
    if limit is None:
        return None
    used = usage_today_for_provider(conn, provider)
    return max(limit - used, 0)


def can_afford(
    conn: sqlite3.Connection,
    provider_limits: dict,
    provider: str,
    estimated_requests: int,
    *,
    reserve_percent: float = 0.0,
    extra_reserve: int = 0,
) -> Affordability:
    """ASES-CAP-03: no card becomes ready unless the budget covers it plus a review reserve.

    reserve_percent holds back that fraction of the day's cap (project.budgets.daily_reserve_percent);
    extra_reserve holds back a flat number of requests on top (project.budgets.review_reserve_requests).
    """
    limit = daily_limit(provider_limits, provider)
    if limit is None:
        return Affordability(True, None, None, "provider has no known daily cap")

    remaining = remaining_today(conn, provider_limits, provider)
    usable = remaining - extra_reserve - int(limit * reserve_percent / 100)
    if usable >= estimated_requests:
        return Affordability(True, remaining, limit, "within budget")
    return Affordability(
        False,
        remaining,
        limit,
        f"needs {estimated_requests}, only {usable} usable today after the {extra_reserve}-request "
        f"review reserve and {reserve_percent:.0f}% daily reserve ({remaining} remaining of {limit})",
    )
