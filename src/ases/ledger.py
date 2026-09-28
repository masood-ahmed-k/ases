"""Persisted request ledger and budget-aware parking (section 9.1: ledger.py; section 5.4, 9.3).

Every agent tool call costs one request against some provider's daily quota. This module is the single
place that counts them, so the controller can refuse to start a card it cannot afford (ASES-CAP-03)
instead of finding out mid-run. Nothing here calls a provider; callers report usage after the fact.
"""
from __future__ import annotations

import dataclasses
import sqlite3
from datetime import datetime, timezone
from typing import Callable

# ASES-CAP-03 / blueprint [p133], bounds table section 9.3: "the limit in section 5.3 minus a 10 percent
# reserve". This is the ONE place the 10 percent default lives; every reader of a budgets mapping that may
# omit daily_reserve_percent (bounds.Bounds, policy.check_budget, report's budget panel, and this module's
# own can_afford) falls back to this constant instead of each guessing its own number. An explicit
# daily_reserve_percent of 0 in a project's budgets still means 0: this is only the fallback for a missing key.
DEFAULT_DAILY_RESERVE_PERCENT = 10


def _real_now() -> datetime:
    return datetime.now(timezone.utc)


# ASES-CAP-03's daily-reset arithmetic (section 9.3) needs a UTC "today" everywhere in this module, and every
# public function below takes its own `now` keyword for exactly that (None means the real wall clock, exactly
# as before this existed). `default_now` is the ONE extra seam on top: the fallback every one of those `now`
# keywords resolves to when a caller does not pass its own. Production code never touches it. It exists so a
# whole controller pass -- whose callers do not thread a `now` of their own down into process_budget_gate, only
# into process_recovery/process_bounds/process_finalize (see r14_wp.md, package CLOCK) -- can still be pointed
# at a simulated day in a test, by replacing this one public, documented attribute
# (`monkeypatch.setattr(ledger, "default_now", lambda: fixed_moment)`), instead of reaching into a private
# function's internals the way tests/acceptance/test_22_9_quota.py used to (monkeypatching `ledger._today`
# itself, which was never meant as a seam). An explicit `now=` on any call below always wins over this default.
default_now: Callable[[], datetime] = _real_now


def _today(now: datetime | None = None) -> str:
    """The UTC calendar day `now` falls on, as `YYYY-MM-DD`: the ledger's day-bucketing key everywhere.

    `now` is this call's own injected moment. When it is omitted, `default_now()` is asked instead (the real
    wall clock, unless a test has replaced it). A naive `now` is assumed to already be UTC, the same convention
    report._utc uses for every other clock parameter in this codebase."""
    moment = now if now is not None else default_now()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    else:
        moment = moment.astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%d")


def record_usage(
    conn: sqlite3.Connection, provider: str, model: str, n: int = 1, *, now: datetime | None = None,
) -> None:
    if n <= 0:
        raise ValueError("n must be positive")
    today = _today(now)
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


def usage_today(conn: sqlite3.Connection, provider: str, model: str, *, now: datetime | None = None) -> int:
    row = conn.execute(
        "SELECT count FROM requests_ledger WHERE provider = ? AND model = ? AND utc_date = ?",
        (provider, model, _today(now)),
    ).fetchone()
    return row["count"] if row else 0


def usage_today_for_provider(conn: sqlite3.Connection, provider: str, *, now: datetime | None = None) -> int:
    """Sum across every model on this provider -- OpenRouter's daily cap is per account, not per model."""
    row = conn.execute(
        "SELECT COALESCE(SUM(count), 0) AS total FROM requests_ledger WHERE provider = ? AND utc_date = ?",
        (provider, _today(now)),
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


def remaining_today(
    conn: sqlite3.Connection, provider_limits: dict, provider: str, *, now: datetime | None = None,
) -> int | None:
    limit = daily_limit(provider_limits, provider)
    if limit is None:
        return None
    used = usage_today_for_provider(conn, provider, now=now)
    return max(limit - used, 0)


def can_afford(
    conn: sqlite3.Connection,
    provider_limits: dict,
    provider: str,
    estimated_requests: int,
    *,
    reserve_percent: float = DEFAULT_DAILY_RESERVE_PERCENT,
    extra_reserve: int = 0,
    now: datetime | None = None,
) -> Affordability:
    """ASES-CAP-03: no card becomes ready unless the budget covers it plus a review reserve.

    reserve_percent holds back that fraction of the day's cap (project.budgets.daily_reserve_percent);
    extra_reserve holds back a flat number of requests on top (project.budgets.review_reserve_requests).
    The default matches DEFAULT_DAILY_RESERVE_PERCENT above; a caller that means no reserve at all must
    pass reserve_percent=0 explicitly. `now` is this call's own injected moment (None means default_now, the
    real wall clock unless a test has replaced it): see _today's docstring.
    """
    limit = daily_limit(provider_limits, provider)
    if limit is None:
        return Affordability(True, None, None, "provider has no known daily cap")

    remaining = remaining_today(conn, provider_limits, provider, now=now)
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
