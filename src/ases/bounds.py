"""Global bounds, project state and the definition of finished (section 9.3, ASES-CTL-01).

"A project is finished when every merge card is done, Gates 4 and 5 are green on the integration HEAD, and the
release report is written. It is stopped, not finished, when any global bound is reached." This module holds the
three pieces that sentence needs and nothing else:

  * Bounds and evaluate_bounds: Table 17 as configuration, and a measurement of every bound against the database
    (and, for the per-card wall clock only, the board). It measures and never acts. What to do on reaching a bound
    is the "On reaching it" column of the table, carried on each BoundStatus for the caller. Only two bounds stop a
    whole project (stop_reasons); the rest escalate one task and belong to recovery.py, and the card wall clock is
    enforced by Hermes itself (--max-runtime), so its status here is for reporting and for the attempt count.
  * The project_state row (get_state, start_project, set_status, ...): the one place the polling loop, the kill
    switch (ASES-REC-06, section 19.6) and the report agree on whether a project is planning, running, paused,
    stopped or finished. Where a stop written by another process could race a write of ours (start_project,
    finish_project) the check is part of the write statement, so a stop is never overwritten by a stale read.
  * The finish test (is_finished, finish_project) and the place the final Gate 4 and Gate 5 results and the release
    report are recorded. ASES-TSK-04 makes those gates controller lifecycle operations, built elsewhere; this
    module is only where their results are written down and consulted.

Nothing here calls a provider or starts a process. hermes.kanban_show is the only outside call.
"""
from __future__ import annotations

import dataclasses
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone

from . import events
from . import gates as gates_mod
from . import hermes as hermes_mod
from . import ledger
from . import plan as plan_mod

# ---------------------------------------------------------------------------------------------
# The bounds (section 9.3, Table 17)
# ---------------------------------------------------------------------------------------------

# The names a BoundStatus carries. Each is the config/swarm.yaml budget key of its bound wherever one exists, so a
# status can be traced back to the setting that produced its limit.
ATTEMPTS_PER_CARD = "attempts_per_card"
REVIEW_ROUNDS_PER_TASK = "review_rounds_per_task"
FIX_CARDS_PER_TASK = "fix_cards_per_task"
REPLANS_PER_PROJECT = "replans_per_project"
CARDS_PER_PROJECT = "max_cards"
PROVIDER_REQUESTS_PER_DAY = "provider_requests_per_day"
CARD_WALL_CLOCK = "card_runtime_minutes"
PROJECT_WALL_CLOCK = "project_wall_clock_minutes"

# Table 17, column "On reaching it", verbatim.
_ON_REACH = {
    ATTEMPTS_PER_CARD: "The card blocks; classify and escalate",
    REVIEW_ROUNDS_PER_TASK: "Escalate to the Lead, then to the user",
    FIX_CARDS_PER_TASK: "Escalate to the user",
    REPLANS_PER_PROJECT: "User decision",
    CARDS_PER_PROJECT: "Gate 0 rejects larger plans",
    PROVIDER_REQUESTS_PER_DAY: "Park cards until the reset",
    CARD_WALL_CLOCK: "Hermes terminates and re-queues the card; counts as an attempt",
    PROJECT_WALL_CLOCK: "Pause and report",
}

# The bounds that stop a whole project rather than escalate one task: Table 17 says "User decision" for re-plans and
# "Pause and report" for the project wall clock. Every other row is a task or card level response.
STOP_BOUNDS = frozenset({REPLANS_PER_PROJECT, PROJECT_WALL_CLOCK})

_COUNT_FIELDS = (
    "attempts_per_card", "review_rounds_per_task", "fix_cards_per_task", "replans_per_project", "max_cards",
    "card_runtime_minutes", "daily_reserve_percent",
)


def _bound_int(name: str, value) -> int:
    """One budget value as an int, or ValueError naming the key. A bool is refused although it is an int in
    Python: `attempts_per_card: true` in YAML would otherwise quietly mean one attempt. A negative value is a
    typo and never a meaning ("always breached"), so it is refused at load time instead of stopping the first
    project it touches."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"budgets.{name} must be an integer, got {ascii(value)}")
    if value < 0:
        raise ValueError(f"budgets.{name} must not be negative, got {value}")
    return value


@dataclasses.dataclass(frozen=True)
class Bounds:
    """The global bounds of section 9.3 (ASES-CTL-01: "Bounds are configuration with these defaults").

    The first four are lineage and project counters (attempts per card, review rounds per plan task, fix cards per
    plan task, re-plans per project), max_cards is Gate 0's ceiling on tasks in a plan, card_runtime_minutes is
    the per-card wall clock (--max-runtime), and daily_reserve_percent is the share of a provider's daily cap held
    back (ASES-CAP-03). project_wall_clock_minutes has no default because the table says "Set at Gate P": None
    means no configured limit, and a deadline recorded in project_state still applies."""
    attempts_per_card: int = 3
    review_rounds_per_task: int = 3
    fix_cards_per_task: int = 2
    replans_per_project: int = 2
    max_cards: int = 40
    card_runtime_minutes: int = 45
    daily_reserve_percent: int = 10
    project_wall_clock_minutes: int | None = None

    @classmethod
    def from_budgets(cls, budgets: Mapping | None) -> Bounds:
        """Bounds from the `budgets:` mapping of config/swarm.yaml (ProjectConfig.budgets). Unknown keys are
        ignored (review_reserve_requests lives in the same mapping and is not a global bound), a missing key takes
        its default, and a present key that is not an integer raises ValueError naming that key. This is the
        parse-and-validate entry point for configuration; the constructor itself trusts its caller.

        Also refused: a negative number, and a daily_reserve_percent above 100 (a reserve larger than the whole
        cap would leave every provider permanently over its limit)."""
        if budgets is None:
            budgets = {}
        if not isinstance(budgets, Mapping):
            raise ValueError(f"budgets must be a mapping, got {type(budgets).__name__}")
        values: dict = {}
        for name in _COUNT_FIELDS:
            if name in budgets:
                values[name] = _bound_int(name, budgets[name])
        if values.get("daily_reserve_percent", 0) > 100:
            raise ValueError(
                f"budgets.daily_reserve_percent must be at most 100, got {values['daily_reserve_percent']}"
            )
        wall_clock = budgets.get("project_wall_clock_minutes")
        if wall_clock is not None:
            values["project_wall_clock_minutes"] = _bound_int("project_wall_clock_minutes", wall_clock)
        return cls(**values)


# ---------------------------------------------------------------------------------------------
# Time helpers. Every timestamp ASES writes is UTC ISO 8601 in whole seconds, and every time-dependent function takes
# an injectable `now`, so no test depends on the wall clock.
# ---------------------------------------------------------------------------------------------


def _utc(moment: datetime | None = None) -> datetime:
    """`moment` as an aware UTC datetime, the current time when None. A naive datetime is read as UTC, so a test
    that injects datetime(2026, 9, 19, 12, 0) means noon UTC and not noon in the machine's own zone."""
    if moment is None:
        return datetime.now(timezone.utc)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _iso(moment: datetime) -> str:
    """The one timestamp format ASES writes: 2026-09-19T12:00:00+00:00."""
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_time(value) -> datetime | None:
    """A timestamp as an aware UTC datetime, or None when it cannot be read. Two shapes occur: ASES's own rows are
    ISO 8601 text, and Hermes hands run times over as epoch seconds, as a float, an int or a numeric string
    (probed 2026-09-19: "1789832318" on a run, 1789832318.27 on a session). Nothing raises: a value that is neither
    is unknown, and every caller treats unknown as "cannot measure", never as zero."""
    if isinstance(value, datetime):
        return _utc(value)
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    if isinstance(value, str):
        text = value.strip()
        try:
            seconds = float(text)
        except ValueError:
            try:
                return _utc(datetime.fromisoformat(text))
            except ValueError:
                return None
    else:
        seconds = float(value)
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):  # nan, inf, or a year the platform cannot represent
        return None


def _minutes(span: timedelta) -> float:
    """A span in minutes, never negative: a start time slightly in the future (clock skew between Hermes and this
    process) is zero elapsed, not a negative amount used."""
    return max(span.total_seconds(), 0.0) / 60.0


# ---------------------------------------------------------------------------------------------
# Project state: the project_state row (planning | running | paused | stopped | finished)
# ---------------------------------------------------------------------------------------------

STATUSES = ("planning", "running", "paused", "stopped", "finished")


class StateError(Exception):
    """A project_state change that is not allowed from the current status: a stopped or finished project is not
    restarted by start_project."""


def get_state(conn: sqlite3.Connection, project: str) -> dict | None:
    """The project_state row as a dict (project, started_at, deadline_at, replans, status, stop_reason,
    updated_at), or None when the project has none yet."""
    row = conn.execute(
        "SELECT project, started_at, deadline_at, replans, status, stop_reason, updated_at "
        "FROM project_state WHERE project = ?",
        (project,),
    ).fetchone()
    return None if row is None else dict(row)


def _deadline_after(moment: datetime, minutes) -> str | None:
    """The ISO deadline `minutes` after `moment`, None when no deadline was asked for. Checked before anything is
    written, so a bad value never leaves a half-made row behind."""
    if minutes is None:
        return None
    if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes <= 0:
        raise ValueError(f"deadline_minutes must be a positive integer, got {ascii(minutes)}")
    return _iso(moment + timedelta(minutes=minutes))


def start_project(
    conn: sqlite3.Connection, project: str, *, deadline_minutes: int | None = None, now: datetime | None = None,
) -> dict:
    """ASES-CTL-01: mark a project running and start its wall clock. Returns the resulting state.

    A project with no row gets one, status running, started_at now, and a deadline `deadline_minutes` from now
    when that is given (the wall-clock bound "Set at Gate P"). A project that already has a row keeps its
    started_at and any deadline it has: a restart after a pause must not reset the clock or quietly extend the
    limit. Only gaps are filled (a row a re-plan or set_deadline made before the project started has no started_at
    yet, and a deadline asked for now is set when there is none), and a planning or paused project becomes running.

    A stopped or finished project is NOT restarted by this call: it raises StateError. A stop is the kill switch
    (ASES-REC-06) or a reached bound, and a finished project is done; both need a deliberate resume through
    set_status, never a `swarm run` that happens to call this. The refusal is part of the write statement, so a
    stop that lands between our read and our write still wins."""
    moment = _utc(now)
    stamp = _iso(moment)
    deadline_at = _deadline_after(moment, deadline_minutes)
    created = conn.execute(
        "INSERT INTO project_state (project, started_at, deadline_at, replans, status, updated_at) "
        "VALUES (?, ?, ?, 0, 'running', ?) ON CONFLICT(project) DO NOTHING",
        (project, stamp, deadline_at, stamp),
    ).rowcount == 1
    if created:
        return get_state(conn, project)

    state = get_state(conn, project)
    if state["status"] in ("stopped", "finished"):
        raise StateError(f"project {ascii(project)} is {state['status']} and is not restarted by start_project")
    if state["status"] == "running" and state["started_at"] is not None and (
        state["deadline_at"] is not None or deadline_at is None
    ):
        return state  # nothing to start and no gap to fill: leave the row, updated_at included, exactly as it is
    changed = conn.execute(
        "UPDATE project_state SET status = 'running', started_at = COALESCE(started_at, ?), "
        "deadline_at = COALESCE(deadline_at, ?), updated_at = ? "
        "WHERE project = ? AND status IN ('planning', 'running', 'paused')",
        (stamp, deadline_at, stamp, project),
    ).rowcount
    if changed == 0:  # a stop or a finish landed after the read above
        raise StateError(
            f"project {ascii(project)} is {get_state(conn, project)['status']} and is not restarted by start_project"
        )
    return get_state(conn, project)


def set_deadline(
    conn: sqlite3.Connection, project: str, deadline_at_iso: str, *, now: datetime | None = None,
) -> None:
    """Record the project wall-clock deadline (Table 17: "Project wall-clock: Set at Gate P"), replacing any
    earlier one: extending it is the user's decision when the bound stops a project, and this is how it is made.
    Creates the row (status planning, no started_at) when the project has none, since Gate P comes before the
    project starts. The deadline is stored as UTC ISO seconds whatever offset it was given with (a timestamp with
    no offset is read as UTC); text that is not a timestamp raises ValueError and writes nothing."""
    deadline = _parse_time(deadline_at_iso)
    if deadline is None:
        raise ValueError(f"deadline must be an ISO 8601 timestamp, got {ascii(deadline_at_iso)}")
    conn.execute(
        "INSERT INTO project_state (project, deadline_at, replans, status, updated_at) "
        "VALUES (?, ?, 0, 'planning', ?) "
        "ON CONFLICT(project) DO UPDATE SET deadline_at = excluded.deadline_at, updated_at = excluded.updated_at",
        (project, _iso(deadline), _iso(_utc(now))),
    )


def set_status(
    conn: sqlite3.Connection, project: str, status: str, reason: str | None = None, *, now: datetime | None = None,
) -> None:
    """Set a project's status (section 19.6: the polling loop and the kill switch coordinate through it).
    `status` must be one of planning, running, paused, stopped, finished, else ValueError and nothing is written.
    Creates the row when the project has none.

    `reason` is stored as stop_reason for `stopped` AND `paused`, the two statuses ASES-CTL-01 leaves a project in
    when it is not finished (Table 17: a reached bound "stops, not finishes"; pause_and_report uses `paused` for
    the same case with running work left alone) and that the report needs an explanation for. Any other status
    (planning, running, finished) clears it, so a project that runs again never shows the reason of an old stop or
    pause. started_at, the deadline and the re-plan count are not touched.

    Before this fix, a paused project's reason was dropped here and kept only in a `project_paused` event
    (controller.pause_and_report / controller._pause_reason); this is the fix for that known gap."""
    if status not in STATUSES:
        raise ValueError(f"project status must be one of {', '.join(STATUSES)}, got {ascii(status)}")
    stop_reason = str(reason) if (status in ("stopped", "paused") and reason is not None) else None
    conn.execute(
        "INSERT INTO project_state (project, replans, status, stop_reason, updated_at) VALUES (?, 0, ?, ?, ?) "
        "ON CONFLICT(project) DO UPDATE SET status = excluded.status, stop_reason = excluded.stop_reason, "
        "updated_at = excluded.updated_at",
        (project, status, stop_reason, _iso(_utc(now))),
    )


def add_replan(conn: sqlite3.Connection, project: str, *, now: datetime | None = None) -> int:
    """Count one re-plan against the project (Table 17: "Re-plans per project 2") and return the new total.
    Creates the row (status planning) when the project has none. One statement with RETURNING (SQLite 3.35 or
    newer), so the number returned is the one this call wrote even if another process re-plans at the same time."""
    rows = conn.execute(
        "INSERT INTO project_state (project, replans, status, updated_at) VALUES (?, 1, 'planning', ?) "
        "ON CONFLICT(project) DO UPDATE SET replans = replans + 1, updated_at = excluded.updated_at "
        "RETURNING replans",
        (project, _iso(_utc(now))),
    ).fetchall()
    return int(rows[0]["replans"])


def stop_requested(conn: sqlite3.Connection, project: str) -> bool:
    """ASES-REC-06 (section 19.6): True when the project is stopped or paused, and the polling loop and the merge
    queue must do no new work. A project with no row, or one that is planning, running or finished, is not asking
    for a stop."""
    row = conn.execute("SELECT status FROM project_state WHERE project = ?", (project,)).fetchone()
    return row is not None and row["status"] in ("stopped", "paused")


# ---------------------------------------------------------------------------------------------
# Measuring the bounds
# ---------------------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class BoundStatus:
    """One bound measured against one subject. `subject` is a plan task key, a provider name, a card id, or
    "project", whichever the bound counts against. `used` and `limit` are whole numbers for the counters and the
    request bounds, and minutes (fractional) for the two wall clocks. `breached` is used >= limit: a bound is
    reached when its budget is spent, not only when it is exceeded. `on_reach` is the blueprint's response, the
    "On reaching it" text of Table 17 word for word."""
    name: str
    subject: str
    used: int | float
    limit: int | float
    breached: bool
    on_reach: str


def _status(name: str, subject: str, used: int | float, limit: int | float) -> BoundStatus:
    return BoundStatus(name, subject, used, limit, used >= limit, _ON_REACH[name])


def _show(board: str, card_id: str) -> Mapping | None:
    """hermes.kanban_show, or None when it fails for any reason. Deliberately broad: a card that cannot be read
    (Hermes down, a card that vanished, a timeout, a malformed answer) is a card this module cannot vouch for,
    and both callers turn that into the safe answer, "not measured" for a bound and "not done" for the finish
    test. A bound check that raised would take the whole polling pass down with it."""
    try:
        card = hermes_mod.kanban_show(board, card_id)
    except Exception:  # noqa: BLE001 - see the docstring
        return None
    return card if isinstance(card, Mapping) else None


def _latest_run_start(card: Mapping) -> datetime | None:
    """When the card's latest run started, from its `_runs` list (hermes.kanban_show), or None when no run has a
    readable start. The latest is the one that started last, whatever the order the list came in."""
    starts = []
    for run in card.get("_runs") or []:
        if isinstance(run, Mapping):
            started = _parse_time(run.get("started_at"))
            if started is not None:
                starts.append(started)
    return max(starts, default=None)


def _provider_statuses(conn: sqlite3.Connection, models_config: Mapping | None, bounds: Bounds) -> list[BoundStatus]:
    """Requests used today against each capped provider's daily limit minus the reserve (Table 17: "the limit
    in section 5.3 minus a 10 percent reserve", ASES-CAP-03). The reserve is taken the way ledger.can_afford takes
    it, rounded down to whole requests, so 50 a day with 10 percent held back is 45 and 25 a day is 23. A provider
    with no known daily cap (limits: {}) has nothing to measure against and gets no status."""
    providers = (models_config or {}).get("providers") or {}
    statuses = []
    for provider in providers:
        cap = ledger.daily_limit(providers, provider)
        if cap is None:
            continue
        limit = cap - cap * bounds.daily_reserve_percent // 100
        statuses.append(_status(
            PROVIDER_REQUESTS_PER_DAY, provider, ledger.usage_today_for_provider(conn, provider), limit,
        ))
    return statuses


def _card_wall_clock_statuses(
    board: str, plan: plan_mod.Plan, work_cards: Mapping[str, str], bounds: Bounds, moment: datetime,
) -> list[BoundStatus]:
    """Minutes each running work card has been going since its latest run started, against card_runtime_minutes
    (Table 17: "Wall-clock per card"). Only a task's CURRENT work card is looked at (the original, or the latest fix
    card, plan_tasks.work_card_id), only when Hermes reports it `running`, and a card that cannot be read or has no
    readable run start is skipped: it is not measured, which is not the same as not breached."""
    statuses = []
    for task in plan.tasks:
        card_id = work_cards.get(task.key)
        if not card_id:
            continue
        card = _show(board, card_id)
        if card is None or card.get("status") != "running":
            continue
        started = _latest_run_start(card)
        if started is None:
            continue
        statuses.append(_status(CARD_WALL_CLOCK, card_id, _minutes(moment - started), bounds.card_runtime_minutes))
    return statuses


def _project_wall_clock_status(state: Mapping | None, bounds: Bounds, moment: datetime) -> BoundStatus | None:
    """Minutes since the project started against its wall-clock limit (Table 17: "Project wall-clock: Set at
    Gate P"), or None when there is nothing to measure: the project has not started, or has no limit. A deadline
    recorded in project_state wins over the configured project_wall_clock_minutes: it is the per-project decision
    (made at Gate P, or a user's extension after a stop), and the configured number is only the default for a
    project that has none. The limit is the length of the window from started_at to the deadline, so the bound is
    reached exactly when the deadline is."""
    if state is None:
        return None
    started = _parse_time(state["started_at"])
    if started is None:
        return None
    deadline = _parse_time(state["deadline_at"])
    if deadline is not None:
        limit = _minutes(deadline - started)
    elif bounds.project_wall_clock_minutes is not None:
        limit = bounds.project_wall_clock_minutes
    else:
        return None
    return _status(PROJECT_WALL_CLOCK, "project", _minutes(moment - started), limit)


def evaluate_bounds(
    board: str, plan: plan_mod.Plan, bounds: Bounds, models_config: Mapping | None, *,
    conn: sqlite3.Connection, now: datetime | None = None,
) -> list[BoundStatus]:
    """ASES-CTL-01: measure every global bound of section 9.3 and return one BoundStatus per bound and subject,
    breached or not, in the order of Table 17. Nothing is acted on here; a caller asks stop_reasons which of the
    breached ones stop the project and hands the rest to recovery.

      * attempts per card and review rounds per plan task: the task's lineage counters (capability_failures and
        review_rounds), one status per plan task, a task with no lineage row counting 0 (ASES-REC-02: the counters
        are per task, because every fix card starts a new card-level counter);
      * fix cards per plan task: plan_tasks.fix_cards, 0 for a task with no row;
      * re-plans per project: project_state.replans, 0 with no row;
      * cards per project: the number of plan TASKS (a task is two cards, but Gate 0 counts tasks) against max_cards;
      * requests per provider per day: today's ledger total against the daily limit minus the reserve, for each
        provider that has a daily cap;
      * wall clock per card: each task's current work card that Hermes reports running, by hermes.kanban_show
        (the only board call; one that fails is skipped);
      * project wall clock: minutes since project_state.started_at against the deadline row or the configured
        minutes, no status when neither exists.

    `now` (a datetime, naive read as UTC) makes the two wall clocks testable; the daily request total always
    comes from the ledger's own UTC date."""
    moment = _utc(now)
    keys = [task.key for task in plan.tasks]
    lineage = {
        row["task_key"]: row for row in conn.execute(
            "SELECT task_key, review_rounds, capability_failures FROM lineage WHERE project = ?", (plan.project,),
        )
    }
    spent = {
        row["task_key"]: row for row in conn.execute(
            "SELECT task_key, work_card_id, fix_cards FROM plan_tasks WHERE project = ?", (plan.project,),
        )
    }
    state = get_state(conn, plan.project)

    statuses = [
        _status(ATTEMPTS_PER_CARD, key, lineage[key]["capability_failures"] if key in lineage else 0,
                bounds.attempts_per_card)
        for key in keys
    ]
    statuses += [
        _status(REVIEW_ROUNDS_PER_TASK, key, lineage[key]["review_rounds"] if key in lineage else 0,
                bounds.review_rounds_per_task)
        for key in keys
    ]
    statuses += [
        _status(FIX_CARDS_PER_TASK, key, spent[key]["fix_cards"] if key in spent else 0, bounds.fix_cards_per_task)
        for key in keys
    ]
    statuses.append(_status(
        REPLANS_PER_PROJECT, "project", state["replans"] if state else 0, bounds.replans_per_project,
    ))
    statuses.append(_status(CARDS_PER_PROJECT, "project", len(plan.tasks), bounds.max_cards))
    statuses += _provider_statuses(conn, models_config, bounds)
    statuses += _card_wall_clock_statuses(
        board, plan, {key: row["work_card_id"] for key, row in spent.items()}, bounds, moment,
    )
    wall_clock = _project_wall_clock_status(state, bounds, moment)
    if wall_clock is not None:
        statuses.append(wall_clock)
    return statuses


def stop_reasons(statuses: list[BoundStatus]) -> list[BoundStatus]:
    """ASES-CTL-01: the breached bounds that STOP a project, in the order they came. Table 17 gives two of them a
    whole-project response, "User decision" for re-plans per project and "Pause and report" for the project wall
    clock. Every other breach (attempts, review rounds, fix cards, provider requests, cards per project, the card
    wall clock) escalates or parks one task, and is not a reason to stop everything."""
    return [status for status in statuses if status.breached and status.name in STOP_BOUNDS]


# ---------------------------------------------------------------------------------------------
# Finished: merge cards done, Gates 4 and 5 green on the integration HEAD, release report written
# ---------------------------------------------------------------------------------------------

# The final gates are not tied to a plan task, and gate_runs (schema v5, not edited here) has no project column, so
# they live in gate_runs under this task_key. gates.run_gate(..., conn=conn, task_key=FINAL_TASK_KEY) writes exactly
# such a row, so the gate runner needs no special case.
FINAL_TASK_KEY = "__final__"
FINAL_GATES = ("gate4", "gate5")
RELEASE_REPORT_EVENT = "release_report_written"


def _outcome(result) -> str:
    """'pass' or 'fail' (the two values gate_runs.result holds) from what a caller has to hand: those words in
    any case, or a bool such as GateResult.passed."""
    if isinstance(result, bool):
        return "pass" if result else "fail"
    if isinstance(result, str) and result.strip().lower() in ("pass", "fail"):
        return result.strip().lower()
    raise ValueError(f"gate result must be 'pass' or 'fail' (or a bool), got {ascii(result)}")


def record_final_gate(
    conn: sqlite3.Connection, project: str, gate: str, commit_sha: str, result, *,
    detail: str = "", now: datetime | None = None,
) -> None:
    """ASES-TSK-04, ASES-CTL-01: record the result of a final gate, "gate4" (security) or "gate5" (smoke), on an
    exact commit. Anything else raises ValueError, as does a blank commit or a result that is not pass or fail.

    The row is the one gates.run_gate would insert (task_key, gate, commit_sha, result, detail, ran_at, project),
    under the task_key "__final__". `detail` is passed through events.redact first: Gate 4 is a secrets scan, and
    its findings quote the lines they found (ASES-SEC-01: secrets never reach logs or reports).

    Schema v7 added a project column to gate_runs (this function predates it, and used to record the project only
    in the `final_gate_recorded` event below, next to a row that could not otherwise carry it). It is written here
    too now, the same way gates.run_gate writes it, so a reader that scopes by project (final_gates_green,
    gates.last_gate_result) has real data to filter on; the event is kept as well, since another reader may still
    depend on it and it costs nothing to keep."""
    if gate not in FINAL_GATES:
        raise ValueError(f"a final gate is one of {', '.join(FINAL_GATES)}, got {ascii(gate)}")
    sha = (commit_sha or "").strip()
    if not sha:
        raise ValueError("a final gate result needs the commit SHA it ran on")
    outcome = _outcome(result)
    conn.execute(
        "INSERT INTO gate_runs (task_key, gate, commit_sha, result, detail, ran_at, project) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (FINAL_TASK_KEY, gate, sha, outcome, events.redact({"detail": detail or ""})["detail"],
         _iso(_utc(now)), project),
    )
    events.record(conn, "final_gate_recorded", {"project": project, "gate": gate, "commit_sha": sha, "result": outcome})


def final_gates_green(
    conn: sqlite3.Connection, integration_head: str | None, *, project: str | None = None,
) -> bool:
    """ASES-CTL-01: True only when Gate 4 AND Gate 5 both passed on exactly `integration_head`. A pass on an older
    commit does not count (the integration branch has moved since, so that evidence is about different code), a
    fail does not count, and when a gate ran more than once on the commit the latest row wins (the same rule as
    gates.last_gate_result), so a red re-run cancels an earlier green. Only rows under the "__final__" task_key are
    read: a task's own gate rows never stand in for a final gate. No head means nothing is green.

    `project` is a new, keyword-only, optional filter (schema v7): omitted (the old, still positional, two-argument
    call shape every existing caller and test uses), it scans every project's "__final__" rows exactly as before --
    two projects sharing this database is a real case (found by the bounds and MR builders) that this function used
    to get wrong silently. Given, it is passed on to gates.last_gate_result, which matches that project's own rows
    OR a legacy row with no project at all, never a different project's. is_finished/finish_project pass plan.project
    here; finalgates.py (which this round does not own or edit) still calls this with just (conn, head) and keeps
    working unchanged."""
    sha = (integration_head or "").strip()
    if not sha:
        return False
    return all(
        gates_mod.last_gate_result(conn, FINAL_TASK_KEY, gate, sha, project=project) == "pass" for gate in FINAL_GATES
    )


def mark_release_report(conn: sqlite3.Connection, project: str, path) -> None:
    """ASES-CTL-01: note that the release report for `project` is written, by recording its path in a
    `release_report_written` event. Call it after the file is on disk: it does not look at the file, because the
    path may be relative to a checkout this module never sees. A blank path raises ValueError, since it can name
    no report."""
    text = "" if path is None else str(path).strip()  # str(None) would record the text "None" as a path
    if not text:
        raise ValueError("the release report path must not be empty")
    events.record(conn, RELEASE_REPORT_EVENT, {"project": project, "path": text})


def release_report_written(conn: sqlite3.Connection, project: str) -> bool:
    """ASES-CTL-01: True once mark_release_report has recorded a report for this project."""
    row = conn.execute(
        f"SELECT 1 FROM events WHERE kind = ? AND {events.PROJECT_SCOPE_SQL} LIMIT 1",
        (RELEASE_REPORT_EVENT, project),
    ).fetchone()
    return row is not None


def _merge_cards_done(board: str, plan: plan_mod.Plan, conn: sqlite3.Connection) -> bool:
    """True when every plan task has a merge card and Hermes reports each one `done`. Stops at the first that is
    not, so a plan that is nowhere near done costs one board call, not one per task."""
    merge_cards = {
        row["task_key"]: row["merge_card_id"] for row in conn.execute(
            "SELECT task_key, merge_card_id FROM plan_tasks WHERE project = ?", (plan.project,),
        )
    }
    for task in plan.tasks:
        card_id = merge_cards.get(task.key)
        if not card_id:
            return False
        card = _show(board, card_id)
        if card is None or card.get("status") != "done":
            return False
    return True


def is_finished(board: str, plan: plan_mod.Plan, integration_head: str | None, *, conn: sqlite3.Connection) -> bool:
    """ASES-CTL-01 (section 9.3): "A project is finished when every merge card is done, Gates 4 and 5 are green on
    the integration HEAD, and the release report is written." All three must hold, and the two that need only the
    database are checked first, so the board is asked nothing while the gates are red or the report is missing.

    Fail closed: a merge card that cannot be read, or a plan task that has no merge card, means the project is not
    finished. So is a plan with no tasks (Gate 0 refuses one, and a project that merged nothing has finished
    nothing). `integration_head` is the exact commit the final gates must have passed on."""
    if not plan.tasks:
        return False
    if not final_gates_green(conn, integration_head, project=plan.project):
        return False
    if not release_report_written(conn, plan.project):
        return False
    return _merge_cards_done(board, plan, conn)


def finish_project(
    board: str, plan: plan_mod.Plan, integration_head: str | None, *, conn: sqlite3.Connection,
    now: datetime | None = None,
) -> bool:
    """ASES-CTL-01: set the project's status to `finished` when is_finished says it is. Returns True only when
    THIS call made the change, so it flips the status once: a project that is already finished, or one that is not
    finished yet, returns False and writes nothing, and calling it again is harmless. A caller that wants "is it
    finished" as a state reads get_state(...)["status"].

    A `stopped` or `paused` project is never flipped, and the board is not even asked: the kill switch (ASES-REC-06)
    and a reached bound (stopped, not finished) outrank a finish that a loop pass raced to. The guard is part of
    the write statement, so a stop that lands while is_finished is still reading the board wins. A project with
    no row is created as finished."""
    state = get_state(conn, plan.project)
    if state is not None and state["status"] in ("finished", "paused", "stopped"):
        return False
    if not is_finished(board, plan, integration_head, conn=conn):
        return False
    return conn.execute(
        "INSERT INTO project_state (project, replans, status, updated_at) VALUES (?, 0, 'finished', ?) "
        "ON CONFLICT(project) DO UPDATE SET status = 'finished', stop_reason = NULL, "
        "updated_at = excluded.updated_at "
        "WHERE project_state.status NOT IN ('paused', 'stopped', 'finished')",
        (plan.project, _iso(_utc(now))),
    ).rowcount == 1
