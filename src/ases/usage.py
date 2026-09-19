"""Real request usage for the ledger, and the review reserve (section 5.4, 9.2; ASES-CAP-03).

Until this module, nothing wrote real usage into the request ledger: ledger.record_usage() had no production
caller, so the budget gate read zero spend however much a worker had really burned (one review used 37 of
OpenRouter's 50 free requests a day and nothing warned). Hermes already has the numbers. Every finished worker
run on a card is in the card's runs list, a coder or reviewer run's metadata names its worker_session_id, and
`hermes sessions export` reports how many model API calls that session made, which is exactly what a
provider's daily quota counts. ingest_run_usage() turns those into ledger rows, once per session (the
usage_ingested table), and review_budget() is the review-reserve half of ASES-CAP-03.

The only outside calls are hermes.kanban_show and hermes.session_usage. Nothing here calls a provider.
"""
from __future__ import annotations

import contextlib
import json
import sqlite3

from . import config as ases_config
from . import events
from . import hermes as hermes_mod
from . import ledger
from . import plan as plan_mod
from . import policy


def provider_for_profile(
    profile: str | None, roles: dict, models_config: dict,
) -> policy.ProfileProvider | None:
    """The provider/model a Hermes profile is pinned to, found by reversing the roles: map (role -> profile)
    and asking policy.profile_provider for that role. None when the profile maps to no role, or its role has
    no pinned provider. A profile shared by several roles gets the first of them that has one. A run made by
    the controller itself has no profile at all, and must never match a role whose profile is unset."""
    if not profile:
        return None
    for role, mapped in roles.items():
        if mapped != profile:
            continue
        pinned = policy.profile_provider(role, models_config)
        if pinned is not None:
            return pinned
    return None


def _has_ended(run: dict) -> bool:
    return run.get("ended_at") not in (None, "")


def _worker_session_id(run: dict) -> str | None:
    """The run's worker_session_id, or None. Hermes hands metadata over as a dict or as a JSON string, and a
    run the controller made itself (a merge card) has none."""
    metadata = run.get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            return None
    if not isinstance(metadata, dict):
        return None
    session_id = metadata.get("worker_session_id")
    return session_id if isinstance(session_id, str) and session_id else None


@contextlib.contextmanager
def _atomic(conn: sqlite3.Connection):
    """One SAVEPOINT around a group of writes, so they land together or not at all. Unlike BEGIN, a savepoint
    nests, so this is safe whether or not the caller already has a transaction open."""
    conn.execute("SAVEPOINT usage_ingest")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK TO usage_ingest")
        conn.execute("RELEASE usage_ingest")
        raise
    conn.execute("RELEASE usage_ingest")


def _ingest_run(
    conn: sqlite3.Connection, run: dict, project: ases_config.ProjectConfig, models_config: dict,
    attribution: tuple[str | None, str | None, str | None] = (None, None, None),
) -> str | None:
    """Count one run's session into the ledger if it qualifies. Returns the session id when it was counted.
    `attribution` is (plan project, task key, card id): recorded with the row so requests can be summed per plan
    task lineage (ASES-REC-02); (None, None, None) when the caller does not know."""
    if not _has_ended(run):
        return None
    session_id = _worker_session_id(run)
    if session_id is None:
        return None
    if conn.execute("SELECT 1 FROM usage_ingested WHERE session_id = ?", (session_id,)).fetchone():
        return None
    profile = run.get("profile")
    pinned = provider_for_profile(profile, project.roles, models_config)
    if pinned is None:
        return None
    usage = hermes_mod.session_usage(profile, session_id)
    if usage is None:
        return None  # export failed: record nothing, so the next call tries this session again

    requests = int(usage.get("api_call_count") or 0)
    model = usage.get("model") or pinned.model
    with _atomic(conn):
        conn.execute(
            "INSERT INTO usage_ingested (session_id, profile, provider, model, requests, input_tokens, "
            "output_tokens, ingested_at, project, task_key, card_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, ?, ?)",
            (session_id, profile, pinned.provider, model, requests,
             int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0), *attribution),
        )
        if requests > 0:
            ledger.record_usage(conn, pinned.provider, model, requests)
        events.record(conn, "usage_ingested", {
            "session_id": session_id, "profile": profile, "provider": pinned.provider,
            "model": model, "requests": requests,
        })
    return session_id


def ingest_run_usage(
    board: str, plan: plan_mod.Plan, project: ases_config.ProjectConfig, models_config: dict, *, conn,
) -> list[str]:
    """Count the requests of every finished worker run of this plan into the ledger, once per session
    (ASES-CAP-03: the budget gate can only be as honest as the ledger it reads).

    For each plan task this reads the task's CURRENT work card (plan_tasks.work_card_id, scoped to
    plan.project: process_merge_queue repoints it at a fix card, and two projects on one board reuse task
    keys) and walks that card's runs. A run is counted when it has ended, its metadata names a
    worker_session_id, that session is not already in usage_ingested, and its profile maps to a role with a
    pinned provider. Runs the controller made itself (merge cards) have no profile and no session, and are
    never counted. The count is the session's api_call_count, under the model the session reports (else the
    pinned model).

    A session whose export fails (hermes.session_usage returns None) records nothing and is retried on the
    next call, and the other sessions carry on. A session that made no requests is still recorded, with 0, so
    it is not fetched again. The ledger increment, the usage_ingested row and the event are written in one
    savepoint, so a failure between them cannot leave a session counted but unmarked (double counted on the
    next call) or marked but uncounted (lost).

    Returns the session ids ingested by this call, in the order they were found."""
    ingested: list[str] = []
    for task in plan.tasks:
        row = conn.execute(
            "SELECT work_card_id FROM plan_tasks WHERE project = ? AND task_key = ?",
            (plan.project, task.key),
        ).fetchone()
        if row is None or not row["work_card_id"]:
            continue
        ingested.extend(ingest_card_usage(
            board, row["work_card_id"], project, models_config, conn=conn,
            plan_project=plan.project, task_key=task.key,
        ))
    return ingested


def ingest_card_usage(
    board: str, card_id: str, project: ases_config.ProjectConfig, models_config: dict, *, conn,
    plan_project: str | None = None, task_key: str | None = None,
) -> list[str]:
    """ingest_run_usage for ONE card, by id. process_merge_queue calls it for the card it is about to
    replace with a fix card: plan_tasks.work_card_id is repointed at the fix card in that same step, after
    which ingest_run_usage never looks at the outgoing card again, so a reviewer run that ended since the
    last pass would otherwise never be counted."""
    card = hermes_mod.kanban_show(board, card_id)
    ingested: list[str] = []
    for run in card.get("_runs") or []:
        session_id = _ingest_run(conn, run, project, models_config, (plan_project, task_key, card_id))
        if session_id is not None:
            ingested.append(session_id)
    return ingested


def lineage_requests(conn: sqlite3.Connection, plan_project: str, task_key: str) -> int:
    """Total model requests counted so far across a plan task's whole lineage (the original card and every fix
    card, ASES-REC-02), from the usage_ingested rows attributed to it."""
    row = conn.execute(
        "SELECT COALESCE(SUM(requests), 0) AS total FROM usage_ingested WHERE project = ? AND task_key = ?",
        (plan_project, task_key),
    ).fetchone()
    return int(row["total"])


def review_budget(
    conn: sqlite3.Connection, models_config: dict, project: ases_config.ProjectConfig,
) -> ledger.Affordability | None:
    """The review reserve of ASES-CAP-03: can the REVIEWER's provider still afford one review pass today?

    A coder card that finishes needs a review, and that review runs on the reviewer role's provider, which
    can be a different and much tighter provider than the coder's (on the current pins the coder is on a
    provider with no known daily cap and the reviewer is on OpenRouter's 50 requests a day). The card gate
    only asks about the card's own provider, so it cannot see this. The pass is sized by
    budgets.review_reserve_requests, and budgets.daily_reserve_percent is still held back on top, as
    everywhere else.

    The reserve is the amount being asked for here, so it is NOT also handed to check_budget as its own
    extra reserve: that would demand twice the reserve (20 requests asked for and 20 more held back). The
    question is whether that many requests are still usable after the daily reserve.

    Returns None when the reviewer role has no pinned provider (nothing to check). Otherwise the
    Affordability, and a provider with no known daily cap is always affordable."""
    pinned = policy.profile_provider("reviewer", models_config)
    if pinned is None:
        return None
    reserve = project.budgets.get("review_reserve_requests", 0)
    budgets = {**project.budgets, "review_reserve_requests": 0}
    return policy.check_budget(conn, models_config["providers"], pinned.provider, reserve, budgets=budgets)
