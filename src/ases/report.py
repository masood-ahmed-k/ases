"""Project report: swarm status, swarm report and the optional local page (section 15; section 16, phase 8).

ASES-OBS-01: the Hermes dashboard already shows the board, runs, worker logs and per-card model choices, so none
of that is rebuilt here. This module adds the seven panels of section 15.2 (Project, Budget, Cards, Quality,
Health, Events, Models) as plain data (build_report) and three ways to read them: a compact terminal summary
(render_status), the full terminal report (render_text) and one self-contained HTML page (render_html), which
write_report saves next to a JSON copy. Nothing is served and nothing is uploaded: the page is a static file with
no script and no external resource, opened from disk.

Read-only by construction: build_report only SELECTs from the ASES database and asks Hermes to show or list
cards, so producing a report can never move a card, touch the ledger or add an event.

ASES-OBS-02 and ASES-SEC-01 (never a secret in a report): every event payload is redacted as it is read, the
whole report goes through events.redact once more before build_report returns it, and again inside each renderer
and write_report (a report a caller edited by hand is not trusted either). events.redact replaces the VALUE of
any key whose name contains key, token, secret, password, credential or authorization, so this module must never
call one of its own fields by such a name: token counts are tok_in and tok_out for exactly that reason
(test_report_keys_survive_redaction pins it).
"""
from __future__ import annotations

import html
import json
import pathlib
import sqlite3
from datetime import datetime, timedelta, timezone

from . import config as ases_config
from . import events as events_mod
from . import hermes as hermes_mod
from . import ledger
from . import models as models_mod
from . import plan as plan_mod
from . import questions as questions_mod

# Section 15: where the board, runs and worker logs already are (ASES does not rebuild them).
HERMES_DASHBOARD_URL = "http://127.0.0.1:9119"

# Gates 4 and 5 run on the integration HEAD, not on one plan task, so their gate_runs rows carry this
# pseudo task key (bounds.record_final_gate). The Quality panel lists them next to the per-task gate runs.
FINAL_GATE_KEY = "__final__"

# The event kinds the Health panel reads: what the controller records when the run itself is in trouble or has
# had to spend a bounded resource (controller.run_pass, process_budget_gate, process_merge_queue), plus four kinds
# round 5 added that the panel had not picked up yet (round 6 fix): model_mismatch (usage.py, ASES-RTE-01, a
# session that ran on a different model than its profile is pinned to), should_stop_error (mergeq._stop_requested,
# the caller's own kill-switch check raised), and tamper_check_error / tamper_blocked (review.py, ASES-QG-03, the
# tamper check could not run, or found something that blocks a merge). The Quality panel's findings query also
# lists the two tamper kinds individually (its "contains tamper" catch-all is a deliberately general net, kept
# that way on purpose: see _quality_panel below); a health-panel COUNT and a quality-panel per-event LISTING are
# different things and both are kept.
HEALTH_KINDS = (
    "pass_error", "usage_ingest_error", "merge_failed", "merge_race_retrying", "card_parked_for_budget",
    "integrity_violation", "fix_card_created", "fix_card_budget_exhausted", "model_mismatch", "should_stop_error",
    "tamper_check_error", "tamper_blocked",
)

_HEALTH_WINDOW = 200     # at most this many health events are read; counts are over the events read
_HEALTH_RECENT = 10      # how many of them are kept, newest first, for the "recent" list
_GATE_RUN_LIMIT = 20
_VERDICT_LIMIT = 20
_FINDING_LIMIT = 20
_PARKED_WINDOW = 200     # how many card_parked_for_budget events are searched for a card's latest reason
_QUESTION_CHARS = 1000   # a block reason is agent text and can be any size
_MESSAGE_CHARS = 300
_TEXT_CELL_CHARS = 120   # a table cell in the terminal report is cut here (the JSON and the page keep it all)
_STATUS_LINE_CHARS = 150

# Section 9.3 table 17: the defaults a bound falls back to when config/swarm.yaml's budgets: does not name it.
_BOUND_DEFAULTS = {
    "max_cards": 40, "fix_cards_per_task": 2, "review_rounds_per_task": 3, "replans_per_project": 2,
    "attempts_per_card": 3,
}
_PROJECT_BOUNDS = ("cards in plan", "re-plans", "wall clock minutes")

# Lifecycle order for status counts: progress first, then what is waiting, then the odd cases.
_STATUS_ORDER = (
    "done", "review", "running", "ready", "todo", "scheduled", "blocked", "triage", "archived",
    "unknown", "not created",
)


# ---------------------------------------------------------------------------------------------
# Small helpers: time, text, events.
# ---------------------------------------------------------------------------------------------


def _utc(value: datetime | None) -> datetime:
    """`value` as an aware UTC datetime. None means now, and a naive value is taken to be UTC (every timestamp
    ASES writes is)."""
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _parse_ts(text) -> datetime | None:
    """A stored timestamp as an aware UTC datetime, or None when it is missing or unreadable. The database holds
    both `2026-09-19T10:00:00+00:00` (Python) and `2026-09-19 10:00:00` (SQLite's datetime('now')); both are UTC."""
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        return _utc(datetime.fromisoformat(text.strip()))
    except ValueError:
        return None


def _clip(text: str, limit: int | None) -> str:
    if limit is None or len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _short(sha) -> str | None:
    """A commit SHA shortened to 10 characters for display; None and "" stay None."""
    return sha[:10] if sha else None


def _load_payload(raw) -> dict:
    """An events row's payload as a redacted dict. events.record only ever writes a JSON object, but a row written
    by hand or damaged must still be readable: text that is not JSON is kept as {"text": ...}, and JSON that is
    not an object as {"value": ...}, instead of raising."""
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, RecursionError):
        payload = {"text": str(raw)}
    if not isinstance(payload, dict):
        payload = {"value": payload}
    return events_mod.redact(payload)


_MESSAGE_FIELDS = ("message", "detail", "error", "reason", "problems")


def _event_message(payload: dict) -> str:
    """One line saying what an event was about, from its (already redacted) payload: the plan task it names, then
    the first of message, detail, error, reason or problems that holds anything, else the rest of the payload as
    compact JSON. Runs of whitespace collapse, so a multi-line git error stays one line."""
    task = payload.get("task_key")
    body = None
    for name in _MESSAGE_FIELDS:
        value = payload.get(name)
        if value not in (None, "", [], {}):
            body = "; ".join(str(item) for item in value) if isinstance(value, list) else str(value)
            break
    if body is None:
        rest = {key: value for key, value in payload.items() if key != "task_key"}
        body = json.dumps(rest, sort_keys=True, separators=(",", ":")) if rest else ""
    text = f"{task}: {body}" if task and body else str(task or body)
    return _clip(" ".join(text.split()), _MESSAGE_CHARS)


def _read_events(conn: sqlite3.Connection, where: str, params: tuple, limit: int) -> list[dict]:
    """The newest `limit` events matching `where` (a fixed SQL fragment written in this module, never user text),
    newest first, each as {"ts", "kind", "payload"} with the payload already redacted. Ordered by ts and then id:
    ts has one-second resolution, so the id breaks a tie in the order the rows were written."""
    if limit <= 0:
        return []
    rows = conn.execute(
        f"SELECT ts, kind, payload FROM events {where} ORDER BY ts DESC, id DESC LIMIT ?", (*params, limit),
    ).fetchall()
    return [{"ts": row["ts"], "kind": row["kind"], "payload": _load_payload(row["payload"])} for row in rows]


def _status_rank(status: str) -> tuple[int, str]:
    if status in _STATUS_ORDER:
        return _STATUS_ORDER.index(status), ""
    return len(_STATUS_ORDER), status


def _count(statuses: list[str]) -> dict[str, int]:
    """How many of each status, in lifecycle order (a status Hermes adds later sorts last, alphabetically), so the
    JSON and the text read the same from one run to the next."""
    return {status: statuses.count(status) for status in sorted(set(statuses), key=_status_rank)}


def _bound(name: str, used: int | None, limit: int | None) -> dict:
    return {"name": name, "used": used, "limit": limit}


# ---------------------------------------------------------------------------------------------
# build_report and its seven panels.
# ---------------------------------------------------------------------------------------------


def build_report(
    board: str, plan: plan_mod.Plan, project: ases_config.ProjectConfig, models_config: dict,
    conn: sqlite3.Connection, *, now: datetime | None = None, event_limit: int = 40,
) -> dict:
    """ASES-OBS-01, section 15.2: the project report as plain, JSON-serialisable data, one key per panel
    (project, budget, cards, quality, health, events, models) after `generated_at`.

    `now` is the report's clock (generated_at, the wall-clock bound, the day used for "requests today by model"
    and the next reset); None means the real time. The provider totals come from ledger.usage_today_for_provider,
    which always reads the real UTC day, so in production the two are the same day.

    Cards are read from Hermes (kanban_show for each task's current work card and its merge card, kanban_list for
    the parked ones) and everything else from the ASES database. A card Hermes cannot show is reported with status
    "unknown" and the rest of the report is unaffected; the same goes for the parked list. Nothing is written.

    Every string in the result has been through events.redact (ASES-SEC-01, ASES-OBS-02)."""
    moment = _utc(now)
    task_rows = _task_rows(conn, plan.project)
    report = {
        "generated_at": _iso(moment),
        "project": _project_panel(conn, board, plan, project, task_rows, moment),
        "budget": _budget_panel(board, conn, project, models_config, task_rows, moment),
        "cards": _cards_panel(board, plan, task_rows),
        "quality": _quality_panel(conn, plan),
        "health": _health_panel(conn),
        "events": _read_events(conn, "", (), event_limit),
        "models": _models_panel(conn),
    }
    return events_mod.redact(report)


def _task_rows(conn: sqlite3.Connection, plan_project: str) -> dict:
    """plan_tasks for this plan, by task key. Scoped to plan.project: two projects on one board reuse task keys."""
    rows = conn.execute(
        "SELECT task_key, work_card_id, merge_card_id, fix_cards FROM plan_tasks WHERE project = ?",
        (plan_project,),
    ).fetchall()
    return {row["task_key"]: row for row in rows}


def _limit(budgets: dict, key: str) -> int | None:
    return budgets.get(key, _BOUND_DEFAULTS.get(key))


def _wall_clock(state, budgets: dict, now: datetime) -> dict:
    """The project wall-clock bound in whole minutes (section 9.3: "Set at Gate P"). used is the time since
    started_at, and limit is the time from started_at to deadline_at, or budgets.project_wall_clock_minutes when
    the project has no deadline (the deadline wins, as in bounds.evaluate_bounds). Both are None ("not set")
    until the project has a start time. A project that is finished or stopped stops the clock at
    project_state.updated_at, so a report read days later does not show a run as over its deadline for the time
    it sat idle."""
    started = _parse_ts(state["started_at"]) if state is not None else None
    deadline = _parse_ts(state["deadline_at"]) if state is not None else None
    used = limit = None
    if started is not None:
        end = now
        if state["status"] in ("finished", "stopped"):
            stopped = _parse_ts(state["updated_at"])
            if stopped is not None and started <= stopped < now:
                end = stopped
        used = max(int((end - started).total_seconds() // 60), 0)
        if deadline is not None:
            limit = int((deadline - started).total_seconds() // 60)
        else:
            limit = budgets.get("project_wall_clock_minutes")
    return _bound("wall clock minutes", used, limit)


def _project_panel(
    conn: sqlite3.Connection, board: str, plan: plan_mod.Plan, project: ases_config.ProjectConfig,
    task_rows: dict, now: datetime,
) -> dict:
    """Section 15.2 Project: what this project is, its phase, and how much of each bound is used.

    `bounds` lists {name, used, limit} for every bound the database can answer, and no others: the plan's size
    against max_cards, re-plans against replans_per_project, the wall clock, then for each plan task its fix
    cards (plan_tasks.fix_cards), review rounds, capability failures (attempts, against attempts_per_card) and
    infrastructure failures (no bound is defined for those, so their limit is None). A task with no lineage row
    has used nothing, so it reads 0. A key missing from budgets falls back to section 9.3's default."""
    state = conn.execute(
        "SELECT status, stop_reason, started_at, deadline_at, replans, updated_at FROM project_state "
        "WHERE project = ?", (plan.project,),
    ).fetchone()
    lineage = {
        row["task_key"]: row for row in conn.execute(
            "SELECT task_key, review_rounds, capability_failures, infra_failures FROM lineage WHERE project = ?",
            (plan.project,),
        )
    }
    budgets = project.budgets
    bounds = [
        _bound("cards in plan", len(plan.tasks), _limit(budgets, "max_cards")),
        _bound("re-plans", state["replans"] if state is not None else 0, _limit(budgets, "replans_per_project")),
        _wall_clock(state, budgets, now),
    ]
    for task in plan.tasks:
        row = task_rows.get(task.key)
        counters = lineage.get(task.key)
        bounds += [
            _bound(f"fix cards {task.key}", row["fix_cards"] if row is not None else 0,
                   _limit(budgets, "fix_cards_per_task")),
            _bound(f"review rounds {task.key}", counters["review_rounds"] if counters is not None else 0,
                   _limit(budgets, "review_rounds_per_task")),
            _bound(f"capability failures {task.key}",
                   counters["capability_failures"] if counters is not None else 0,
                   _limit(budgets, "attempts_per_card")),
            _bound(f"infra failures {task.key}", counters["infra_failures"] if counters is not None else 0, None),
        ]
    return {
        "name": project.name,
        "plan_project": plan.project,
        "board": board,
        "integration_branch": plan.integration_branch,
        "data_class": project.data_class,
        "status": state["status"] if state is not None else None,
        "stop_reason": state["stop_reason"] if state is not None else None,
        "started_at": state["started_at"] if state is not None else None,
        "deadline_at": state["deadline_at"] if state is not None else None,
        "bounds": bounds,
    }


def _role_of(profile: str, roles: dict) -> str | None:
    """The first role the roles: map (role -> profile) gives to this profile, None for a profile no role uses."""
    for role, mapped in roles.items():
        if mapped == profile:
            return role
    return None


def _parked_cards(board: str, conn: sqlite3.Connection, task_rows: dict) -> tuple[list[dict], str | None]:
    """(cards, error): the plan's cards Hermes lists as `scheduled`, which is how the controller parks a card it
    cannot afford (process_budget_gate), each with the reason and time of the LATEST card_parked_for_budget event
    for its task. Only cards of this plan are kept: the board is shared with other projects. A failing list gives
    ([], the error) so the report says it could not tell, rather than that nothing is parked."""
    owners = {}
    for key, row in task_rows.items():
        for card_id in (row["work_card_id"], row["merge_card_id"]):
            if card_id:
                owners[card_id] = key
    try:
        scheduled = hermes_mod.kanban_list(board, status="scheduled")
    except Exception as exc:  # noqa: BLE001 - deliberately broad: a Hermes that cannot answer must not sink the report
        return [], _clip(f"{type(exc).__name__}: {exc}", 200)
    latest: dict[str, dict] = {}
    for event in _read_events(conn, "WHERE kind = ?", ("card_parked_for_budget",), _PARKED_WINDOW):
        key = event["payload"].get("task_key")
        if isinstance(key, str):
            latest.setdefault(key, event)   # newest first, so the first one seen is the latest
    parked = []
    for card in scheduled:
        key = owners.get(card.get("id"))
        if key is None:
            continue
        event = latest.get(key)
        parked.append({
            "task_key": key,
            "card_id": card["id"],
            "title": card.get("title"),
            "reason": event["payload"].get("reason") if event is not None else None,
            "parked_at": event["ts"] if event is not None else None,
        })
    return parked, None


def _budget_panel(
    board: str, conn: sqlite3.Connection, project: ases_config.ProjectConfig, models_config: dict,
    task_rows: dict, now: datetime,
) -> dict:
    """Section 15.2 Budget, and section 15.1's "requests per provider, model, role and UTC day, against the
    budget": for each provider in models_config the daily limit, the requests used today, what remains and the
    reserve held back (budgets.daily_reserve_percent of the limit, the same arithmetic ledger.can_afford uses),
    then the cards parked for budget and the requests ingested today per provider, model and profile.

    A provider with no known daily cap has limit, remaining and reserve None: the report never invents a number
    for it. `next_reset` is the next UTC midnight, when the ledger's day rolls over."""
    providers = models_config.get("providers", {})
    reserve_percent = project.budgets.get("daily_reserve_percent", 0)
    rows = []
    for name, entry in providers.items():
        limit = ledger.daily_limit(providers, name)
        rows.append({
            "provider": name,
            "limit": limit,
            "used": ledger.usage_today_for_provider(conn, name),
            "remaining": ledger.remaining_today(conn, providers, name),
            "reserve": None if limit is None else int(limit * reserve_percent / 100),
            "status": entry.get("status"),
        })
    day = now.strftime("%Y-%m-%d")
    by_model = [
        {
            "provider": row["provider"], "model": row["model"], "role": _role_of(row["profile"], project.roles),
            "profile": row["profile"], "requests": row["requests"], "sessions": row["sessions"],
            "tok_in": row["tok_in"], "tok_out": row["tok_out"],
        }
        for row in conn.execute(
            "SELECT provider, model, profile, SUM(requests) AS requests, COUNT(*) AS sessions, "
            "SUM(input_tokens) AS tok_in, SUM(output_tokens) AS tok_out FROM usage_ingested "
            "WHERE date(ingested_at) = ? GROUP BY provider, model, profile "
            "ORDER BY requests DESC, provider, model, profile", (day,),
        )
    ]
    parked, parked_error = _parked_cards(board, conn, task_rows)
    return {
        "day": day,
        "next_reset": _iso(now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)),
        "reserve_percent": reserve_percent,
        "providers": rows,
        "parked": parked,
        "parked_error": parked_error,
        "by_model": by_model,
    }


def _card_view(board: str, card_id: str | None) -> tuple[dict, dict | None]:
    """(summary, the card as Hermes returned it) for one card id. A card with no id yet reads "not created". A
    card Hermes cannot show (any failure at all: a gone card, a Hermes that is not running, a timeout, an answer
    that is not a card) reads "unknown", with the error text, and is not fatal: one card must not sink the
    report. The card comes back None whenever there is nothing to read from it."""
    if not card_id:
        return {"id": None, "status": "not created", "assignee": None, "title": None, "error": None}, None
    try:
        card = hermes_mod.kanban_show(board, card_id)
        summary = {
            "id": card_id, "status": str(card.get("status") or "unknown"), "assignee": card.get("assignee"),
            "title": card.get("title"), "error": None,
        }
    except Exception as exc:  # noqa: BLE001 - deliberately broad: see above
        return {
            "id": card_id, "status": "unknown", "assignee": None, "title": None,
            "error": _clip(f"{type(exc).__name__}: {exc}", 200),
        }, None
    return summary, card


def _cards_panel(board: str, plan: plan_mod.Plan, task_rows: dict) -> dict:
    """Section 15.2 Cards: for every plan task its CURRENT work card (plan_tasks.work_card_id, which follows a fix
    card once one is opened) and its merge card, with status and assignee from Hermes, then the status counts,
    the open questions and the merge queue. `counts` is over the work cards, the cards that carry the task;
    merge cards are created blocked and only wait, so counting them would read as a wall of blocked cards, and
    they are summarised by `merge_queue` (done over total) instead.

    Open questions are the blocked and triage cards for which questions.open_question finds a question: the one
    rule that `swarm questions`, `swarm answer` and the recovery loop use, so this report cannot drift from them. A
    card blocked with a reason, one the dispatcher gave up on, one an unblock loop sent to triage and one ASES left
    a question on all count, and `questions_by_source` says how many of each (only the sources that occur). They are
    counted over both kinds of card, because a merge card the controller blocked on purpose (fix-card budget spent)
    is a question for the user. (This report only looks at each task's current work and merge card, so a stray card
    that `swarm questions` finds through a parent link or its title is not counted here.)"""
    tasks, questions, sources = [], [], []
    for task in plan.tasks:
        row = task_rows.get(task.key)
        views = {}
        for kind in ("work", "merge"):
            summary, card = _card_view(board, row[f"{kind}_card_id"] if row is not None else None)
            views[kind] = summary
            asked = questions_mod.open_question(card) if card is not None else None
            if asked is not None:
                questions.append({
                    "card_id": summary["id"], "task_key": task.key, "kind": kind,
                    "question": _clip(asked.reason, _QUESTION_CHARS),
                })
                sources.append(asked.source)
        tasks.append({
            "task_key": task.key,
            "title": views["work"]["title"] or task.title,
            "role": task.role,
            "fix_cards": row["fix_cards"] if row is not None else 0,
            "work": views["work"],
            "merge": views["merge"],
        })
    merge_statuses = [item["merge"]["status"] for item in tasks]
    order = {name: number for number, name in enumerate(questions_mod.SOURCES)}
    return {
        "board": board,
        "dashboard": HERMES_DASHBOARD_URL,
        "tasks": tasks,
        "counts": _count([item["work"]["status"] for item in tasks]),
        "merge_queue": {
            "done": merge_statuses.count("done"), "total": len(tasks), "counts": _count(merge_statuses),
        },
        "open_questions": len(questions),
        "questions": questions,
        "questions_by_source": {
            source: sources.count(source)
            for source in sorted(set(sources), key=lambda name: (order.get(name, len(order)), name))
        },
    }


def _tamper_flag(metadata) -> bool:
    """Did the reviewer flag gate tampering (gate_tampering_suspected, ASES-REV-06) in this stored verdict?"""
    try:
        parsed = json.loads(metadata) if metadata else None
    except (ValueError, RecursionError):
        return False
    return isinstance(parsed, dict) and parsed.get("gate_tampering_suspected") is True


def _quality_panel(conn: sqlite3.Connection, plan: plan_mod.Plan) -> dict:
    """Section 15.2 Quality (ASES-QG-01, ASES-REV-06): gate results per task and commit, the reviewer's verdicts,
    the merge records, and the findings the controller recorded as events (a merge refused for want of a review,
    a moved or dirty primary checkout, anything with tamper in its kind).

    gate_runs and merge_records have no project column, so they are scoped by task key (this plan's keys and
    the final-gate key). Verdicts are scoped by project. Gate detail text is deliberately left out: it is command
    output, the likeliest place for a secret, and it stays in gate_runs for whoever needs it."""
    keys = [task.key for task in plan.tasks]
    scope = [*keys, FINAL_GATE_KEY]
    marks = ",".join("?" * len(scope))
    gate_runs = [
        {"task_key": row["task_key"], "gate": row["gate"], "commit_sha": _short(row["commit_sha"]),
         "result": row["result"], "ran_at": row["ran_at"]}
        for row in conn.execute(
            f"SELECT task_key, gate, commit_sha, result, ran_at FROM gate_runs WHERE task_key IN ({marks}) "
            "ORDER BY ran_at DESC, id DESC LIMIT ?", (*scope, _GATE_RUN_LIMIT),
        )
    ]
    verdicts = [
        {"task_key": row["task_key"], "commit_sha": _short(row["commit_sha"]), "card_id": row["card_id"],
         "outcome": row["outcome"], "reviewer_profile": row["reviewer_profile"],
         "tamper_suspected": _tamper_flag(row["metadata"]), "recorded_at": row["recorded_at"]}
        for row in conn.execute(
            "SELECT task_key, commit_sha, card_id, outcome, reviewer_profile, metadata, recorded_at "
            "FROM review_verdicts WHERE project = ? ORDER BY recorded_at DESC LIMIT ?",
            (plan.project, _VERDICT_LIMIT),
        )
    ]
    records = {
        row["task_key"]: row for row in conn.execute(
            f"SELECT task_key, candidate_sha, gate3_result, squash_commit, reverted, completed_at "
            f"FROM merge_records WHERE task_key IN ({marks})", scope,
        )
    }
    merges = [
        {"task_key": key, "squash_commit": _short(records[key]["squash_commit"]),
         "candidate_sha": _short(records[key]["candidate_sha"]), "gate3_result": records[key]["gate3_result"],
         "reverted": bool(records[key]["reverted"]), "completed_at": records[key]["completed_at"]}
        for key in scope if key in records
    ]
    # Round 6 fix: tamper_check_error and tamper_blocked (review.py, ASES-QG-03) are now also counted in
    # HEALTH_KINDS above. They are deliberately still matched here too by the "contains tamper" catch-all: this
    # net is meant to be general (it also nets any future or ad hoc tamper-shaped kind, not only these two named
    # ones), and a quality finding is a per-event, audited listing while the health count is an aggregate for
    # operational monitoring. The two panels are allowed to show the same event for different readers.
    findings = [
        {"ts": event["ts"], "kind": event["kind"],
         "task_key": event["payload"]["task_key"] if isinstance(event["payload"].get("task_key"), str) else None,
         "message": _event_message(event["payload"])}
        for event in _read_events(
            conn,
            "WHERE kind = 'integrity_violation' OR kind GLOB 'merge_refused_*' "
            "OR instr(lower(kind), 'tamper') > 0", (), _FINDING_LIMIT,
        )
    ]
    return {"gate_runs": gate_runs, "review_verdicts": verdicts, "merge_records": merges, "findings": findings}


def _health_panel(conn: sqlite3.Connection) -> dict:
    """Section 15.2 Health, from what the controller itself recorded (HEALTH_KINDS): for each kind the count over
    the events read, the time of the newest one and its message, and the most recent events across all kinds.
    Provider health "from real traffic" (429s, 5xx, timeouts) is not collected anywhere yet, and this panel does
    not pretend otherwise: `note` says so."""
    marks = ",".join("?" * len(HEALTH_KINDS))
    rows = _read_events(conn, f"WHERE kind IN ({marks})", HEALTH_KINDS, _HEALTH_WINDOW)
    counts: dict[str, int] = {}
    newest: dict[str, dict] = {}
    for row in rows:
        counts[row["kind"]] = counts.get(row["kind"], 0) + 1
        newest.setdefault(row["kind"], row)   # newest first, so the first one seen is the newest
    return {
        "window": _HEALTH_WINDOW,
        "read": len(rows),
        "kinds": [
            {
                "kind": kind, "count": counts.get(kind, 0),
                "newest_at": newest[kind]["ts"] if kind in newest else None,
                "newest_message": _event_message(newest[kind]["payload"]) if kind in newest else None,
            }
            for kind in HEALTH_KINDS
        ],
        "recent": [
            {"ts": row["ts"], "kind": row["kind"], "message": _event_message(row["payload"])}
            for row in rows[:_HEALTH_RECENT]
        ],
        "note": "Controller events only. Provider health from real traffic (429, 5xx, timeouts) is not "
                "collected yet.",
    }


def _models_panel(conn: sqlite3.Connection) -> list[dict]:
    """Section 15.2 Models: the capability cache as models.py keeps it (declared context, tool calling, the last
    smoke test), pinned models first and each group in provider, model order."""
    records = sorted(models_mod.list_models(conn), key=lambda m: (not m.pinned, m.provider, m.model))
    return [
        {
            "provider": m.provider, "model": m.model, "role_class": m.role_class, "pinned": m.pinned,
            "context_length": m.context_length, "context_ok": m.context_declared_and_sufficient,
            "tool_calling": m.tool_calling, "data_policy": m.data_policy,
            "smoke_test_result": m.smoke_test_result, "smoke_test_at": m.smoke_test_at,
        }
        for m in records
    ]


# ---------------------------------------------------------------------------------------------
# Text: escaping, and the compact status.
# ---------------------------------------------------------------------------------------------


def _printable(text: str) -> str:
    """`text` with every control character, DEL, C1 control and lone surrogate written out as a backslash escape
    (newline, return and tab as \\n, \\r and \\t). A card title and an event message are agent text: one ESC could
    start a terminal sequence, one newline could break a table row, and a lone surrogate cannot be written as
    UTF-8 at all. Everything else, non-ASCII included, is kept."""
    named = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}
    out = []
    for char in text:
        code = ord(char)
        if char in named:
            out.append(named[char])
        elif code < 0x20 or 0x7F <= code <= 0x9F or 0xD800 <= code <= 0xDFFF:
            out.append(f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}")
        else:
            out.append(char)
    return "".join(out)


def _ascii(value) -> str:
    """`value` as text a Windows console can print (the console is cp1252 and crashes on an arrow): control
    characters escaped as in _printable and every non-ASCII character written as a backslash escape (\\xe9,
    \\u2192, \\U0001f600), never dropped. Already-escaped text passes through unchanged."""
    return _printable(str(value)).encode("ascii", "backslashreplace").decode("ascii")


def _finish(lines: list[str]) -> str:
    """Join intended lines into the final text, escaping each one first so that no value can add a line of its own."""
    return "\n".join(_ascii(line) for line in lines)


def _ratio(used, limit) -> str:
    """"12/240", "12 (no limit set)" for a counter nothing bounds, or "not set" for a bound with no data at all."""
    if limit is None:
        return f"{used} (no limit set)" if used is not None else "not set"
    return f"{used}/{limit}"


def _bound_text(bound: dict) -> str:
    return f"{bound['name']} {_ratio(bound['used'], bound['limit'])}"


def _status_summary(cards: dict) -> str:
    """"3 done, 1 running, 1 blocked (1 question)": the work cards' status counts, with the open questions
    attached to the blocked count, or to the triage count when no work card is blocked (a question can sit on a card
    an unblock loop sent to Hermes's triage lane). Questions can also sit on a merge card the controller blocked,
    and then no work card need be blocked or in triage, so they are named on their own in that case."""
    questions = cards["open_questions"]
    label = f"{questions} question" + ("" if questions == 1 else "s")
    holder = "blocked" if "blocked" in cards["counts"] else "triage"
    parts = []
    attached = False
    for status, number in cards["counts"].items():
        text = f"{number} {status}"
        if status == holder and questions:
            text += f" ({label})"
            attached = True
        parts.append(text)
    if questions and not attached:
        parts.append(f"{label} on merge cards")
    return ", ".join(parts) or "no cards"


def _provider_text(row: dict) -> str:
    """"openrouter 37/50 used (13 left, reserve 5)", or "xkiro 12 used (no known cap)" for a provider whose
    daily limit is not known."""
    if row["limit"] is None:
        text = f"{row['provider']} {row['used']} used (no known cap)"
    else:
        text = (f"{row['provider']} {row['used']}/{row['limit']} used "
                f"({row['remaining']} left, reserve {row['reserve']})")
    return f"{text}, status {row['status']}" if row["status"] else text


def render_status(report: dict) -> str:
    """ASES-OBS-01: the compact, one-screen text for `swarm status`, one line per panel summary: the project and
    its bounds, a line per provider ("openrouter 37/50 used"), the parked cards, the cards by status with the
    open questions ("3 done, 1 running, 1 blocked (1 question)") and the merge queue, the last gate result, and
    the last 5 health events. ASCII only: anything non-ASCII from a card title or event text is backslash-escaped
    (a Windows console crashes on it), and every value is redacted first (ASES-SEC-01)."""
    report = events_mod.redact(report)
    project, budget, cards = report["project"], report["budget"], report["cards"]
    quality, health = report["quality"], report["health"]
    lines = [
        f"ASES status, generated {report['generated_at']} (UTC)",
        f"Project: {project['name']} (plan {project['plan_project']}), board {project['board']}, "
        f"branch {project['integration_branch']}, data class {project['data_class']}, "
        f"status {project['status'] or 'not recorded'}",
        "Bounds: " + ", ".join(
            _bound_text(bound) for bound in project["bounds"]
            if bound["name"] in _PROJECT_BOUNDS or (bound["used"] or 0) > 0
        ),
    ]
    lines += [f"Budget: {_provider_text(row)}" for row in budget["providers"]]
    if not budget["providers"]:
        lines.append("Budget: no providers in the models config")
    if budget["parked_error"] is not None:
        lines.append(f"Parked: unavailable ({budget['parked_error']})")
    elif budget["parked"]:
        lines.append(f"Parked: {len(budget['parked'])} card(s) waiting for budget")
        for card in budget["parked"]:
            label = " ".join(part for part in (card["task_key"], card["title"]) if part)
            lines.append(f"  {label}: {_clip(str(card['reason'] or 'no reason recorded'), _STATUS_LINE_CHARS)}")
    else:
        lines.append("Parked: none")
    queue = cards["merge_queue"]
    lines.append(f"Cards: {_status_summary(cards)}; merge queue {queue['done']}/{queue['total']} done")
    if quality["gate_runs"]:
        run = quality["gate_runs"][0]
        text = f"last gate run {run['task_key']} {run['gate']} {run['result']} at {run['ran_at']}"
        text += f" (commit {run['commit_sha']})" if run["commit_sha"] else ""
    else:
        text = "no gate runs recorded"
    if quality["findings"]:
        text += f"; {len(quality['findings'])} recent finding(s) (refusals, integrity, tamper)"
    lines.append(f"Quality: {text}")
    if health["recent"]:
        lines.append("Health (last 5):")
        lines += [
            f"  {event['ts']} {event['kind']}: {_clip(event['message'], _STATUS_LINE_CHARS)}"
            for event in health["recent"][:5]
        ]
    else:
        lines.append("Health: no events of interest")
    return _finish(lines)


# ---------------------------------------------------------------------------------------------
# The full report: one content layout (_sections), two formatters (text and HTML).
# ---------------------------------------------------------------------------------------------
#
# _sections turns a report into display-ready blocks, all values already strings, so the terminal report and
# the page can never disagree about what a panel says. A block is one of:
#   ("facts", [(label, value), ...])
#   ("table", heading, [header, ...], [[cell, ...], ...], wide)     wide=True: never cut a cell in the terminal
#   ("note", text)


def _dash(value) -> str:
    return "-" if value is None or value == "" else str(value)


def _yes(value) -> str:
    return "-" if value is None else ("yes" if value else "no")


def _table(heading: str, headers: list[str], rows: list[list[str]], *, wide: bool = False, empty: str = "none"):
    if not rows:
        return ("note", f"{heading}: {empty}")
    return ("table", heading, headers, rows, wide)


def _project_blocks(project: dict) -> list[tuple]:
    facts = [
        ("Name", project["name"]), ("Plan project", project["plan_project"]), ("Board", project["board"]),
        ("Integration branch", project["integration_branch"]), ("Data class", project["data_class"]),
        ("Status", project["status"] or "not recorded"), ("Started", _dash(project["started_at"])),
        ("Deadline", _dash(project["deadline_at"])), ("Stop reason", _dash(project["stop_reason"])),
    ]
    rows = [
        [bound["name"], _dash(bound["used"]), "not set" if bound["limit"] is None else str(bound["limit"])]
        for bound in project["bounds"]
    ]
    return [("facts", facts), _table("Bounds", ["Bound", "Used", "Limit"], rows)]


def _budget_blocks(budget: dict) -> list[tuple]:
    facts = [
        ("UTC day", budget["day"]), ("Next reset", budget["next_reset"]),
        ("Daily reserve", f"{budget['reserve_percent']} percent of each capped provider's limit"),
    ]
    providers = [
        [row["provider"], _dash(row["limit"]), str(row["used"]),
         "no known cap" if row["remaining"] is None else str(row["remaining"]), _dash(row["reserve"]),
         _dash(row["status"])]
        for row in budget["providers"]
    ]
    blocks = [
        ("facts", facts),
        _table("Requests today per provider", ["Provider", "Limit", "Used", "Remaining", "Reserve", "Status"],
               providers, empty="no providers in the models config"),
    ]
    if budget["parked_error"] is not None:
        blocks.append(("note", f"Parked cards: unavailable ({budget['parked_error']})"))
    else:
        blocks.append(_table(
            "Parked cards (waiting for budget)", ["Task", "Card", "Title", "Reason", "Since"],
            [[card["task_key"], card["card_id"], _dash(card["title"]), _dash(card["reason"]),
              _dash(card["parked_at"])] for card in budget["parked"]],
        ))
    blocks.append(_table(
        "Requests today by model and role",
        ["Provider", "Model", "Role", "Profile", "Requests", "Sessions", "Tok in", "Tok out"],
        [[row["provider"], row["model"], _dash(row["role"]), row["profile"], str(row["requests"]),
          str(row["sessions"]), _dash(row["tok_in"]), _dash(row["tok_out"])] for row in budget["by_model"]],
        empty="none recorded today",
    ))
    return blocks


def _cards_blocks(cards: dict) -> list[tuple]:
    queue = cards["merge_queue"]
    facts = [
        ("Board", cards["board"]),
        ("Hermes dashboard", f"{cards['dashboard']} (the board, runs, worker logs and per-card models)"),
        ("Work cards", _status_summary(cards)),
        ("Merge queue", f"{queue['done']}/{queue['total']} merge cards done"),
    ]
    rows = [
        [item["task_key"], item["title"], item["role"], _dash(item["work"]["id"]), item["work"]["status"],
         _dash(item["work"]["assignee"]), _dash(item["merge"]["id"]), item["merge"]["status"],
         str(item["fix_cards"])]
        for item in cards["tasks"]
    ]
    blocks = [
        ("facts", facts),
        _table("Plan tasks", ["Task", "Title", "Role", "Work card", "Status", "Assignee", "Merge card",
                              "Merge status", "Fix cards"], rows),
        _table("Open questions (swarm questions lists them, swarm answer replies)",
               ["Card", "Task", "Kind", "Question"],
               [[q["card_id"], q["task_key"], q["kind"], q["question"]] for q in cards["questions"]],
               wide=True),
    ]
    unreadable = [
        [item[kind]["id"], item["task_key"], kind, item[kind]["error"]]
        for item in cards["tasks"] for kind in ("work", "merge") if item[kind]["error"]
    ]
    if unreadable:
        blocks.append(_table("Cards Hermes could not show", ["Card", "Task", "Kind", "Error"], unreadable))
    return blocks


def _quality_blocks(quality: dict) -> list[tuple]:
    return [
        _table("Gate runs (newest first)", ["Time", "Task", "Gate", "Commit", "Result"],
               [[run["ran_at"], run["task_key"], run["gate"], _dash(run["commit_sha"]), run["result"]]
                for run in quality["gate_runs"]], empty="none recorded"),
        _table("Review verdicts (newest first)",
               ["Time", "Task", "Commit", "Outcome", "Reviewer", "Card", "Tamper suspected"],
               [[v["recorded_at"], v["task_key"], _dash(v["commit_sha"]), v["outcome"], v["reviewer_profile"],
                 v["card_id"], _yes(v["tamper_suspected"])] for v in quality["review_verdicts"]],
               empty="none recorded"),
        _table("Merge records", ["Task", "Squash commit", "Candidate", "Gate 3", "Reverted", "Completed"],
               [[m["task_key"], _dash(m["squash_commit"]), _dash(m["candidate_sha"]), _dash(m["gate3_result"]),
                 _yes(m["reverted"]), _dash(m["completed_at"])] for m in quality["merge_records"]],
               empty="none recorded"),
        _table("Findings (merge refusals, integrity, tamper; newest first)", ["Time", "Kind", "Task", "Message"],
               [[f["ts"], f["kind"], _dash(f["task_key"]), f["message"]] for f in quality["findings"]],
               empty="none recorded"),
    ]


def _health_blocks(health: dict) -> list[tuple]:
    return [
        ("note", health["note"]),
        ("note", f"Counts are over the {health['read']} newest health event(s) read (at most {health['window']})."),
        _table("Health events by kind", ["Kind", "Count", "Newest", "Message"],
               [[k["kind"], str(k["count"]), _dash(k["newest_at"]), _dash(k["newest_message"])]
                for k in health["kinds"]]),
        _table("Recent health events", ["Time", "Kind", "Message"],
               [[e["ts"], e["kind"], e["message"]] for e in health["recent"]]),
    ]


def _events_blocks(events: list[dict]) -> list[tuple]:
    return [_table(
        "Newest events first, secrets redacted", ["Time", "Kind", "Payload"],
        [[e["ts"], e["kind"], json.dumps(e["payload"], sort_keys=True, separators=(",", ":"))] for e in events],
        empty="no events recorded",
    )]


def _models_blocks(models: list[dict]) -> list[tuple]:
    return [_table(
        "Model registry (pinned first)",
        ["Provider", "Model", "Role", "Pinned", "Context", "Context ok", "Tools", "Smoke test", "Tested",
         "Data policy"],
        [[m["provider"], m["model"], _dash(m["role_class"]), _yes(m["pinned"]), _dash(m["context_length"]),
          _yes(m["context_ok"]), _yes(m["tool_calling"]), _dash(m["smoke_test_result"]),
          _dash(m["smoke_test_at"]), _dash(m["data_policy"])] for m in models],
        empty="none in the registry (swarm models syncs it from config/models.yaml)",
    )]


def _sections(report: dict) -> list[tuple[str, list[tuple]]]:
    return [
        ("Project", _project_blocks(report["project"])),
        ("Budget", _budget_blocks(report["budget"])),
        ("Cards", _cards_blocks(report["cards"])),
        ("Quality", _quality_blocks(report["quality"])),
        ("Health", _health_blocks(report["health"])),
        ("Events", _events_blocks(report["events"])),
        ("Models", _models_blocks(report["models"])),
    ]


def _text_block(block: tuple) -> list[str]:
    kind = block[0]
    if kind == "note":
        return [_ascii(block[1])]
    if kind == "facts":
        pairs = [(_ascii(label), _ascii(value)) for label, value in block[1]]
        width = max(len(label) for label, _ in pairs)
        return [f"{label.ljust(width)}  {value}" for label, value in pairs]
    _, heading, headers, rows, wide = block
    limit = None if wide else _TEXT_CELL_CHARS
    head = [_ascii(header) for header in headers]
    body = [[_clip(_ascii(cell), limit) for cell in row] for row in rows]
    widths = [max([len(head[i])] + [len(row[i]) for row in body]) for i in range(len(head))]

    def line(cells: list[str]) -> str:
        return "  ".join(cell.ljust(width) for cell, width in zip(cells, widths)).rstrip()

    return [_ascii(heading), line(head), "  ".join("-" * width for width in widths), *(line(row) for row in body)]


def render_text(report: dict) -> str:
    """ASES-OBS-01: the full report for `swarm report`, every panel under its own heading with aligned tables.
    ASCII only: anything non-ASCII from a card title or event text is backslash-escaped, never dropped, and a
    control character is written out so it cannot steer the terminal. A very long cell is cut with "..." (the JSON
    and the page keep it whole); open questions are never cut. Every value is redacted first (ASES-SEC-01)."""
    report = events_mod.redact(report)
    lines = ["ASES project report", f"Generated {report['generated_at']} (UTC)", ""]
    for title, blocks in _sections(report):
        lines += [f"== {title} ==", ""]
        for block in blocks:
            lines += [*_text_block(block), ""]
    return _finish(lines[:-1])


# ---------------------------------------------------------------------------------------------
# The local page.
# ---------------------------------------------------------------------------------------------

_STYLE = """\
:root { color-scheme: light dark; }
body {
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; margin: 0 auto; max-width: 78rem; padding: 1rem;
}
.banner {
  background: #fff3cd; color: #4d3b00; border: 1px solid #d9b84a; border-radius: 4px; padding: .6rem .8rem;
}
h1 { font-size: 1.4rem; }
h2 { font-size: 1.15rem; border-bottom: 1px solid #8886; padding-bottom: .2rem; margin-top: 2rem; }
h3 { font-size: 1rem; margin: 1.2rem 0 .3rem; }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td {
  border: 1px solid #8886; padding: .25rem .5rem; text-align: left; vertical-align: top; overflow-wrap: anywhere;
}
th { background: #8882; }
dl { display: grid; grid-template-columns: max-content 1fr; gap: .2rem 1rem; }
dt { font-weight: 600; }
dd { margin: 0; overflow-wrap: anywhere; }
.note { color: #666a; font-style: italic; }
"""


def _h(value) -> str:
    """`value` as HTML text: control characters written out, then &, <, > and both quotes escaped. Every value on
    the page goes through here, so a card title of <script>alert(1)</script> shows as text and runs as nothing."""
    return html.escape(_printable(str(value)))


def _html_block(block: tuple) -> str:
    kind = block[0]
    if kind == "note":
        return f'<p class="note">{_h(block[1])}</p>'
    if kind == "facts":
        return "<dl>" + "".join(f"<dt>{_h(label)}</dt><dd>{_h(value)}</dd>" for label, value in block[1]) + "</dl>"
    _, heading, headers, rows, _wide = block
    head = "".join(f"<th>{_h(header)}</th>" for header in headers)
    body = "".join("<tr>" + "".join(f"<td>{_h(cell)}</td>" for cell in row) + "</tr>" for row in rows)
    return (f"<h3>{_h(heading)}</h3>\n<div class=\"scroll\"><table><thead><tr>{head}</tr></thead>"
            f"<tbody>{body}</tbody></table></div>")


def render_html(report: dict) -> str:
    """Section 16 phase 8: the optional local page, one self-contained file. Inline CSS, no script, no external
    resource, no link (the dashboard address is text), and a Content-Security-Policy that lets the browser enforce
    the same. It is bound to nothing: no server, no port, the user opens the file. Every value is HTML-escaped
    (a card title with <script> in it appears inert) and redacted first (ASES-SEC-01, ASES-OBS-02). The banner
    says what the file holds, because it can leave the machine as easily as any other file."""
    report = events_mod.redact(report)
    banner = (f"Local report: generated {report['generated_at']}, not served, contains source-code paths and "
              "card text, keep it on this machine")
    body = [f'<p class="banner">{_h(banner)}</p>', f"<h1>ASES project report: {_h(report['project']['name'])}</h1>"]
    for title, blocks in _sections(report):
        body += [f'<section id="{title.lower()}">', f"<h2>{_h(title)}</h2>", *(_html_block(b) for b in blocks),
                 "</section>"]
    head = [
        "<!DOCTYPE html>", '<html lang="en">', "<head>", '<meta charset="utf-8">',
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'\">",
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>ASES project report</title>", f"<style>\n{_STYLE}</style>", "</head>", "<body>",
    ]
    return "\n".join([*head, *body, "</body>", "</html>"]) + "\n"


def write_report(report: dict, directory: str | pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """Write report.html and report.json into `directory` (created if missing) and return both paths, the page
    first. UTF-8 with plain newlines on every platform. The JSON is pure ASCII (non-ASCII text is \\u escaped, so
    a lone surrogate in an agent's title cannot make the write fail) and loads back to exactly `report`. Both are
    written from a freshly redacted copy: the report is redacted again here, not trusted (ASES-SEC-01)."""
    directory = pathlib.Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    safe = events_mod.redact(report)
    page = directory / "report.html"
    data = directory / "report.json"
    page.write_text(render_html(safe), encoding="utf-8", newline="\n")
    data.write_text(json.dumps(safe, indent=2) + "\n", encoding="utf-8", newline="\n")
    return page, data
