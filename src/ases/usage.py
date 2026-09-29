"""Real request usage for the ledger, and the review reserve (section 5.4, 9.2; ASES-CAP-02, ASES-CAP-03).

Round 19, package LEDGER, replaced the original design (a run's own worker_session_id metadata, counted once
per session) with a session-as-unit ingest: the ledger's unit is the Hermes SESSION, counted exactly once, plus
monotone top-ups up to its final api_call_count (r19 LEDGER.md). Three problems with the metadata-only design
drove this:

- Hermes stamps worker_session_id only on kanban_complete and kanban_request_review (hermes tools/kanban_tools.py,
  _stamp_worker_session_metadata). kanban_block, kanban_request_changes and a dispatcher crash/timeout/reclaim
  never call it, so those runs' sessions were invisible however honest their own api_call_count was.
- A run's ended_at is the hand-off, not the end of the session: a worker that keeps calling after its run ends
  (or is reaped later) had its early count locked in forever, permanently undercounting it.
- The Gate P critic's oneshot calls, and a session already counted for a different card by mistake, were never
  addressed by a design that only ever looked at one run's own metadata.

The fix keeps discovering sessions by metadata first (still the cheapest, most direct signal), then falls back to
a WINDOW match against the profile's own Hermes session store (the session's first user prompt names the card,
hermes_cli/kanban_db_dispatch.py:2463 "work kanban task <card_id>"; its start falls in the run's window), then to
a text-parsed session list for a worker that was killed hard and never got a hand-off at all. A session is
attributed to a run for reporting and lineage (ASES-REC-02), but attribution never decides whether it is counted:
usage_ingested.session_id stays the primary key, so a mapping ambiguity can never double count. Only Hermes's own
local session store is read here (hermes.kanban_show, hermes.session_usage, hermes.kanban_sessions,
hermes.kanban_session_ids); nothing in this module calls a provider (ASES-MOD-05).
"""
from __future__ import annotations

import contextlib
import json
import re
import sqlite3
import time
from datetime import datetime, timezone

from . import config as ases_config
from . import events
from . import hermes as hermes_mod
from . import ledger
from . import plan as plan_mod
from . import policy

# The exact prompt every kanban worker's first user message is (kanban_db_dispatch.py:2463, `chat -q "work kanban
# task <task.id>"`). Matched with re.match (not fullmatch): a leading/trailing blank in the prompt Hermes stores is
# tolerated, a trailing word is not.
KANBAN_PROMPT = re.compile(r"^\s*work\s+kanban\s+task\s+(t_[0-9a-f]+)\s*$")

# r19 LEDGER.md section C: Hermes reaps a worker that outlives its run TERMINAL_WORKER_REAP_GRACE_SECONDS (120s)
# after the run ends, so an open run with a counted session is given SETTLE_AFTER (600s) past its own ended_at
# before a stable count is trusted as final. MISSING_AFTER (900s) is how long a spawned run is allowed to show no
# session at all before the list fallback runs and, failing that, the run is declared 'no_session'.
# SETTLE_HARD_STOP (24h) is the backstop that settles ANY row, stable or not, so a row can never be watched
# forever.
SETTLE_AFTER = 600
MISSING_AFTER = 900
SETTLE_HARD_STOP = 86400

# How long a stable count must have been unchanged, checked twice, before it counts toward SETTLE_AFTER's
# "the count equals last_check_count recorded at least 120 s earlier" rule.
_STABILITY_WINDOW = 120


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


def _strip_provider(model: str, provider: str) -> str:
    """`model` without a leading `<provider>/`, compared without regard to case. A router can report the model
    it served with its own name in front (`xkiro/qwen/qwen3-coder-plus:free`) or without it, and both spell the
    same model, so ASES-RTE-01's comparison has to look past that prefix."""
    prefix = f"{provider}/"
    return model[len(prefix):] if model.lower().startswith(prefix.lower()) else model


def _provider_actually_hit(actual: str, pinned: policy.ProfileProvider, models_config: dict) -> str:
    """The provider a session that ran on an UNEXPECTED model most likely hit, else the profile's own provider.
    Only the model string is known here (hermes.session_usage does not say which provider answered), so the
    provider is "determined" in two ways and no more: the model starts with `<provider>/` naming a configured
    provider, or exactly one configured provider lists that model in models_config and the profile's own
    provider does not. Anything else (an unknown model, a model two providers list, one the profile's own
    provider lists too) stays with the profile's provider: a guess would move requests between two quotas on
    no evidence."""
    lowered = actual.lower()
    for name in models_config.get("providers") or {}:
        if lowered.startswith(f"{name}/".lower()):
            return name
    hosts = {
        row["provider"] for row in models_config.get("models") or []
        if isinstance(row, dict) and row.get("provider") and row.get("model")
        and _strip_provider(row["model"], row["provider"]) == _strip_provider(actual, row["provider"])
    }
    if pinned.provider in hosts or len(hosts) != 1:
        return pinned.provider
    return next(iter(hosts))


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


def _epoch(value) -> int | None:
    """`value` (an int, a float, or a numeric string, the three shapes a Hermes timestamp field arrives in
    across this codebase's fixtures and the real CLI) as whole epoch seconds, or None when it is missing or not
    numeric. Never raises."""
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _prompt_card_id(first_prompt) -> str | None:
    """The card id `first_prompt` names, via KANBAN_PROMPT, or None (not a string, or not that exact prompt)."""
    if not isinstance(first_prompt, str):
        return None
    match = KANBAN_PROMPT.match(first_prompt)
    return match.group(1) if match else None


def _as_utc(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, timezone.utc)


def _parse_utc(text) -> float | None:
    """A `datetime('now')`-shaped or ISO UTC timestamp (with or without a "T", with or without fractional
    seconds) as epoch seconds, for a legacy usage_ingested row's own `ingested_at` (step 7: "legacy rows have no
    run_ended_at: use ingested_at in its place"). None when `text` is empty or neither shape parses."""
    if not text:
        return None
    cleaned = str(text).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(cleaned[:26], fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def _id_timestamp(session_id: str) -> float | None:
    """The moment embedded in a Hermes session id (`YYYYMMDD_HHMMSS_hex6`, hermes_state_ids.py's new_session_id),
    read as LOCAL time exactly as Hermes itself stamps it, or None when the leading 15 characters do not parse
    (an id shape this code does not recognise is never used to pre-filter: see `_close_runs_without_sessions`)."""
    try:
        naive = datetime.strptime(session_id[:15], "%Y%m%d_%H%M%S")
    except ValueError:
        return None
    return naive.timestamp()


def _charge_time(usage: dict, now: float) -> float:
    """Step 7 of the proposed design: which moment a new row or a top-up is charged to. The session's own
    ended_at once it has one (so a backfill of an old session lands on the day IT ran, not the day it was
    ingested); otherwise `now`, unless the session's last_activity_at is more than an hour old (a session
    nobody has touched in a while is charged to when it was last seen, not to whenever this pass happens to
    run); never later than `now` itself."""
    ended_at = usage.get("ended_at")
    if ended_at not in (None, ""):
        return min(float(ended_at), now)
    last_activity_at = usage.get("last_activity_at")
    if last_activity_at not in (None, "") and (now - float(last_activity_at)) > 3600:
        return min(float(last_activity_at), now)
    return now


# ---------------------------------------------------------------------------------------------
# Step 1: the card's worker runs (W), and the usage_runs bookkeeping row for each.
# ---------------------------------------------------------------------------------------------


def _spawned_run_ids(card: dict) -> set:
    return {
        event["run_id"] for event in (card.get("_events") or [])
        if isinstance(event, dict) and event.get("kind") == "spawned" and event.get("run_id") is not None
    }


def _worker_runs(
    card: dict, project: ases_config.ProjectConfig, models_config: dict,
) -> list[tuple[dict, policy.ProfileProvider]]:
    """Step 1's W: this card's runs that have a profile mapped to a pinned provider, have ended, and have a
    `spawned` event. A run without one (a `scheduled` card with no worker at all, a `spawn_failed` attempt
    booked before any process started, a run the controller made itself) never expects a session and is never
    watched by this module."""
    spawned = _spawned_run_ids(card)
    out = []
    for run in card.get("_runs") or []:
        pinned = provider_for_profile(run.get("profile"), project.roles, models_config)
        if pinned is None or not _has_ended(run) or run.get("id") not in spawned:
            continue
        out.append((run, pinned))
    return out


def _ensure_usage_runs(
    conn: sqlite3.Connection, board: str, card_id: str, worker_runs: list[tuple[dict, policy.ProfileProvider]],
    plan_project: str | None, task_key: str | None,
) -> None:
    """INSERT OR IGNORE one usage_runs row, state 'open', for every run in W: additive and idempotent, exactly
    like a schema migration, so calling this again for a run already known is a no-op."""
    for run, _pinned in worker_runs:
        started, ended = _epoch(run.get("started_at")), _epoch(run.get("ended_at"))
        if started is None or ended is None:
            continue    # an unparseable timestamp is never expected from real Hermes; skip rather than guess
        conn.execute(
            "INSERT OR IGNORE INTO usage_runs (board, run_id, card_id, profile, project, task_key, "
            "run_started_at, run_ended_at, state, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', datetime('now'))",
            (board, run.get("id"), card_id, run.get("profile"), plan_project, task_key, started, ended),
        )


def _maybe_close_run(conn: sqlite3.Connection, board: str, run_id) -> None:
    """An open run with at least one counted session, none of them unsettled, becomes 'closed' (step 4). A run
    with zero counted sessions is never touched here (that is `_close_runs_without_sessions`'s job): COUNT(*)
    guards against closing a run this query would otherwise see as vacuously "nothing unsettled"."""
    row = conn.execute(
        "SELECT COUNT(*) AS total, SUM(CASE WHEN settled = 0 THEN 1 ELSE 0 END) AS unsettled "
        "FROM usage_ingested WHERE board = ? AND run_id = ?", (board, run_id),
    ).fetchone()
    if row["total"] and not row["unsettled"]:
        conn.execute(
            "UPDATE usage_runs SET state = 'closed', updated_at = datetime('now') "
            "WHERE board = ? AND run_id = ? AND state = 'open'", (board, run_id),
        )


def _maybe_record_ambiguity(conn: sqlite3.Connection, board: str, card_id: str, run_id, profile: str, project) -> None:
    """usage_mapping_ambiguous: this run now has more than one NON-LINEAGE counted session (a lineage session
    sharing its parent's run is expected, never ambiguous). Each such session is still counted once, which is
    correct (every one is a real, distinct worker); this event only flags that the run itself cannot be
    credited to a single session for reporting."""
    rows = conn.execute(
        "SELECT session_id, requests, mapped_by FROM usage_ingested WHERE board = ? AND run_id = ? "
        "ORDER BY session_id", (board, run_id),
    ).fetchall()
    non_lineage = [r for r in rows if r["mapped_by"] != "lineage"]
    if len(non_lineage) > 1:
        events.record(conn, "usage_mapping_ambiguous", {
            "board": board, "card_id": card_id, "run_id": run_id, "profile": profile,
            "session_ids": [r["session_id"] for r in non_lineage],
            "requests": sum(r["requests"] for r in non_lineage),
        }, project=project)


def _record_orphan_once(
    conn: sqlite3.Connection, session_id: str, card_id: str, event_kind: str, payload: dict, *, project,
) -> None:
    """The exactly-once key for a session this pass will never count: usage_orphans' primary key is session_id
    alone, so the first card to claim an already-claimed or unattributable session is the only one whose event
    is ever recorded for it."""
    with _atomic(conn):
        cur = conn.execute(
            "INSERT OR IGNORE INTO usage_orphans (session_id, card_id, recorded_at) VALUES (?, ?, datetime('now'))",
            (session_id, card_id),
        )
        if cur.rowcount:
            events.record(conn, event_kind, payload, project=project)


# ---------------------------------------------------------------------------------------------
# The shared counting core: one usage_ingested row, its ledger increment, and its events, in one savepoint.
# ---------------------------------------------------------------------------------------------


def _count_session(
    conn: sqlite3.Connection, *, board: str, run_id, card_id: str, profile: str, pinned: policy.ProfileProvider,
    usage: dict, models_config: dict, mapped_by: str, attribution: tuple, now: float,
    settled_override: int | None = None, extra_event_fields: dict | None = None,
) -> str:
    """Count one session (`usage`, a hermes.session_usage/kanban_sessions summary dict) into the ledger, however
    it was found. Shared by the metadata, window and list paths so ASES-RTE-01's model-mismatch detection (a
    session that ran on a model other than the one pinned for its profile records ONE model_mismatch event and
    is still counted; detection only, nothing is failed or stopped) is identical for all three. The ambiguity
    check (a run that ends up with two counted non-lineage sessions records usage_mapping_ambiguous) runs on every
    path too: it is a property of the run, not of whichever path found the second session (round 19 review
    finding: across two passes the metadata path could add the second session with no event). Returns the
    session id."""
    session_id = usage["id"]
    requests = int(usage.get("api_call_count") or 0)
    reported = usage.get("model")
    model = reported or pinned.model
    provider = pinned.provider
    mismatch = None
    if isinstance(reported, str) and reported.strip() and (
        _strip_provider(reported.strip(), pinned.provider) != _strip_provider(pinned.model.strip(), pinned.provider)
    ):
        provider = _provider_actually_hit(reported.strip(), pinned, models_config)
        mismatch = {"profile": profile, "expected": pinned.model, "actual": reported, "session_id": session_id}
    ended = usage.get("ended_at") not in (None, "")
    settled = settled_override if settled_override is not None else (1 if ended else 0)
    charge = _charge_time(usage, now)
    billing_provider = usage.get("billing_provider") or None

    with _atomic(conn):
        conn.execute(
            "INSERT INTO usage_ingested (session_id, profile, provider, model, requests, input_tokens, "
            "output_tokens, ingested_at, project, task_key, card_id, board, run_id, mapped_by, settled, "
            "last_check_at, last_check_count, billing_provider) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, profile, provider, model, requests, int(usage.get("input_tokens") or 0),
             int(usage.get("output_tokens") or 0), *attribution, board, run_id, mapped_by, settled,
             int(now), requests, billing_provider),
        )
        if requests > 0:
            ledger.record_usage(conn, provider, model, requests, now=_as_utc(charge))
        payload = {
            "session_id": session_id, "profile": profile, "provider": provider, "model": model,
            "requests": requests,
        }
        if extra_event_fields:
            payload.update(extra_event_fields)
        events.record(conn, "usage_ingested", payload, project=attribution[0])
        if mismatch is not None:
            events.record(conn, "model_mismatch", mismatch, project=attribution[0])
        if run_id is not None:
            _maybe_close_run(conn, board, run_id)
            _maybe_record_ambiguity(conn, board, card_id, run_id, profile, attribution[0])
    return session_id


# ---------------------------------------------------------------------------------------------
# Step 2: the metadata path.
# ---------------------------------------------------------------------------------------------


def _ingest_metadata_path(
    conn: sqlite3.Connection, board: str, card_id: str, worker_runs: list[tuple[dict, policy.ProfileProvider]],
    models_config: dict, attribution: tuple, now: float,
) -> list[str]:
    ingested = []
    for run, pinned in worker_runs:
        session_id = _worker_session_id(run)
        if session_id is None:
            continue
        if conn.execute("SELECT 1 FROM usage_ingested WHERE session_id = ?", (session_id,)).fetchone():
            continue
        profile = run.get("profile")
        usage = hermes_mod.session_usage(profile, session_id)
        if usage is None:
            continue  # export failed: record nothing, so the next call tries this session again
        ingested.append(_count_session(
            conn, board=board, run_id=run.get("id"), card_id=card_id, profile=profile, pinned=pinned,
            usage=usage, models_config=models_config, mapped_by="metadata", attribution=attribution, now=now,
        ))
    return ingested


# ---------------------------------------------------------------------------------------------
# Step 3: the window path.
# ---------------------------------------------------------------------------------------------


def _window_candidates(conn: sqlite3.Connection, card_id: str, sessions: list[dict]):
    """The sessions this card's window path keeps, per step 3: named by KANBAN_PROMPT, or linked by
    parent_session_id to a session already attributed to this card (a compression child). Yields
    (session, mapped_by, lineage_run_id), lineage_run_id only set for the second kind."""
    for session in sessions:
        if _prompt_card_id(session.get("first_prompt")) == card_id:
            yield session, "window", None
            continue
        parent_id = session.get("parent_session_id")
        if parent_id:
            parent = conn.execute(
                "SELECT run_id FROM usage_ingested WHERE session_id = ? AND card_id = ?", (parent_id, card_id),
            ).fetchone()
            if parent is not None:
                yield session, "lineage", parent["run_id"]


def _attribute_and_count_window_session(
    conn: sqlite3.Connection, *, board: str, card_id: str, profile: str, pinned: policy.ProfileProvider,
    session: dict, mapped_by: str, lineage_run_id, models_config: dict, attribution: tuple, now: float,
) -> str | None:
    session_id = session["id"]
    existing = conn.execute(
        "SELECT card_id, run_id FROM usage_ingested WHERE session_id = ?", (session_id,),
    ).fetchone()
    if existing is not None:
        if existing["card_id"] == card_id:
            return None    # already counted for this card: never re-fetched, never re-counted
        _record_orphan_once(conn, session_id, card_id, "usage_session_conflict", {
            "session_id": session_id, "counted_card": existing["card_id"], "claiming_card": card_id,
            "run_id": existing["run_id"],
        }, project=attribution[0])
        return None

    late_start = False
    if mapped_by == "lineage":
        run_id = lineage_run_id
        parent_run = conn.execute(
            "SELECT run_ended_at FROM usage_runs WHERE board = ? AND run_id = ?", (board, run_id),
        ).fetchone()
        if parent_run is not None:
            late_start = session["started_at"] >= parent_run["run_ended_at"]
    else:
        run = conn.execute(
            "SELECT run_id, run_ended_at FROM usage_runs WHERE board = ? AND card_id = ? AND profile = ? "
            "AND run_started_at <= ? ORDER BY run_started_at DESC LIMIT 1",
            (board, card_id, profile, session["started_at"]),
        ).fetchone()
        if run is None:
            _record_orphan_once(conn, session_id, card_id, "usage_session_unattributed", {
                "session_id": session_id, "card_id": card_id, "profile": profile,
                "started_at": session["started_at"],
            }, project=attribution[0])
            return None
        run_id = run["run_id"]
        late_start = session["started_at"] >= run["run_ended_at"]

    return _count_session(
        conn, board=board, run_id=run_id, card_id=card_id, profile=profile, pinned=pinned, usage=session,
        models_config=models_config, mapped_by=mapped_by, attribution=attribution, now=now,
        extra_event_fields={"late_start": True} if late_start else None,
    )


def _ingest_window_path(
    conn: sqlite3.Connection, board: str, card_id: str, project: ases_config.ProjectConfig, models_config: dict,
    attribution: tuple, now: float,
) -> list[str]:
    profiles = [
        row["profile"] for row in conn.execute(
            "SELECT DISTINCT profile FROM usage_runs WHERE board = ? AND card_id = ? AND state = 'open'",
            (board, card_id),
        ).fetchall()
    ]
    ingested = []
    for profile in profiles:
        lo = conn.execute(
            "SELECT MIN(run_started_at) AS lo FROM usage_runs WHERE board = ? AND card_id = ? AND profile = ? "
            "AND state = 'open'", (board, card_id, profile),
        ).fetchone()["lo"]
        if lo is None:
            continue
        sessions = hermes_mod.kanban_sessions(profile, int(lo))
        if sessions is None:
            continue    # unknown: skip this profile for this pass with no writes
        pinned = provider_for_profile(profile, project.roles, models_config)
        if pinned is None:
            continue
        for session, mapped_by, lineage_run_id in _window_candidates(conn, card_id, sessions):
            counted = _attribute_and_count_window_session(
                conn, board=board, card_id=card_id, profile=profile, pinned=pinned, session=session,
                mapped_by=mapped_by, lineage_run_id=lineage_run_id, models_config=models_config,
                attribution=attribution, now=now,
            )
            if counted is not None:
                ingested.append(counted)
    return ingested


# ---------------------------------------------------------------------------------------------
# Step 4: closing runs (the list fallback, and declaring a spawned run's session missing).
# ---------------------------------------------------------------------------------------------


def _close_runs_without_sessions(
    conn: sqlite3.Connection, board: str, card_id: str, project: ases_config.ProjectConfig, models_config: dict,
    attribution: tuple, now: float,
) -> list[str]:
    open_empty = conn.execute(
        "SELECT run_id, profile, run_started_at, run_ended_at FROM usage_runs "
        "WHERE board = ? AND card_id = ? AND state = 'open' AND NOT EXISTS ("
        "SELECT 1 FROM usage_ingested ui WHERE ui.board = usage_runs.board AND ui.run_id = usage_runs.run_id)",
        (board, card_id),
    ).fetchall()
    ingested = []
    for run in open_empty:
        if now < run["run_ended_at"] + MISSING_AFTER:
            continue    # not yet time: give the metadata/window paths more passes first
        pinned = provider_for_profile(run["profile"], project.roles, models_config)
        if pinned is None:
            continue
        found = None
        uncertain = False
        ids = hermes_mod.kanban_session_ids(run["profile"])
        if ids is None:
            continue    # unknown: skip this profile for this pass with no writes, retry next call (a real
                        # failure is never a genuine "nothing found": it must not close out the run)
        for session_id in ids:
            if conn.execute("SELECT 1 FROM usage_ingested WHERE session_id = ?", (session_id,)).fetchone():
                continue
            timestamp = _id_timestamp(session_id)
            if timestamp is not None and not (
                run["run_started_at"] - 60 <= timestamp <= run["run_ended_at"] + 60
            ):
                continue    # a parseable id outside the window is not this run's: an unparseable one is
                            # never pre-filtered, and falls through to the real check below instead
            candidate = hermes_mod.session_usage(run["profile"], session_id)
            if candidate is None:
                uncertain = True    # unknown, the same contract as the ids-is-None guard above: this id might
                                    # still be the run's real match, so it is never treated as "not a match"
                continue
            if _prompt_card_id(candidate.get("first_prompt")) != card_id:
                continue
            started_at = candidate.get("started_at")
            if started_at is None or not (
                run["run_started_at"] - 60 <= started_at <= run["run_ended_at"] + 60
            ):
                continue
            found = _count_session(
                conn, board=board, run_id=run["run_id"], card_id=card_id, profile=run["profile"],
                pinned=pinned, usage=candidate, models_config=models_config, mapped_by="list",
                attribution=attribution, now=now, settled_override=0,
            )
            break
        if found is not None:
            ingested.append(found)
            continue
        if uncertain:
            continue    # a candidate's session_usage call failed transiently: this pass cannot yet rule out
                        # every id, so the run stays 'open' and is retried later, never declared 'no_session'
        with _atomic(conn):
            cur = conn.execute(
                "UPDATE usage_runs SET state = 'no_session', updated_at = datetime('now') "
                "WHERE board = ? AND run_id = ? AND state = 'open'", (board, run["run_id"]),
            )
            if cur.rowcount:
                events.record(conn, "usage_session_missing", {
                    "board": board, "card_id": card_id, "run_id": run["run_id"], "profile": run["profile"],
                    "run_started_at": run["run_started_at"], "run_ended_at": run["run_ended_at"],
                }, project=attribution[0])
    return ingested


# ---------------------------------------------------------------------------------------------
# Public entry points.
# ---------------------------------------------------------------------------------------------


def ingest_run_usage(
    board: str, plan: plan_mod.Plan, project: ases_config.ProjectConfig, models_config: dict, *, conn,
    now: float | None = None,
) -> list[str]:
    """Count the requests of every finished worker session of this plan's tasks into the ledger, once per
    session (ASES-CAP-03: the budget gate can only be as honest as the ledger it reads).

    For each plan task this reads the task's CURRENT work card (plan_tasks.work_card_id, scoped to plan.project:
    process_merge_queue repoints it at a fix card, and two projects on one board reuse task keys) and runs
    ingest_card_usage on it (metadata path, window path, and closing runs still open with no session). A card
    whose export or window discovery fails for now is silently retried on the next call; the other cards carry
    on. `now` is this call's own injected moment in epoch seconds (None means the real wall clock): see
    `_charge_time`.

    Returns the session ids ingested by this call, in the order they were found."""
    now = time.time() if now is None else float(now)
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
            plan_project=plan.project, task_key=task.key, now=now,
        ))
    return ingested


def ingest_card_usage(
    board: str, card_id: str, project: ases_config.ProjectConfig, models_config: dict, *, conn,
    plan_project: str | None = None, task_key: str | None = None, now: float | None = None,
) -> list[str]:
    """ingest_run_usage for ONE card, by id (also called directly by process_merge_queue for the card it is
    about to replace with a fix card, before plan_tasks.work_card_id is repointed: after that, ingest_run_usage
    never looks at the outgoing card again, and any run of its still open at that moment is finished later by
    resolve_open_runs).

    Steps (r19 LEDGER.md PROPOSED DESIGN section C): 1. read the card and pick its worker runs (W: a profile
    pinned to a provider, ended, with a `spawned` event), recording a usage_runs row for each; 2. the metadata
    path (a run whose metadata names a worker_session_id not yet counted); 3. the window path (every profile
    with a still-open run on this card: match the profile's own ended Hermes sessions by first-user-prompt or
    compression lineage); 4. close runs: a run with a counted, settled session becomes 'closed', and a run with
    none, once MISSING_AFTER has passed, tries the killed-worker list fallback before being declared
    'no_session'. `now` is this call's own injected moment in epoch seconds (None means the real wall clock)."""
    now = time.time() if now is None else float(now)
    card = hermes_mod.kanban_show(board, card_id)
    worker_runs = _worker_runs(card, project, models_config)
    _ensure_usage_runs(conn, board, card_id, worker_runs, plan_project, task_key)

    attribution = (plan_project, task_key, card_id)
    ingested = _ingest_metadata_path(conn, board, card_id, worker_runs, models_config, attribution, now)
    ingested += _ingest_window_path(conn, board, card_id, project, models_config, attribution, now)
    ingested += _close_runs_without_sessions(conn, board, card_id, project, models_config, attribution, now)
    return ingested


def resolve_open_runs(
    board: str, project: ases_config.ProjectConfig, models_config: dict, *, conn, now: float | None = None,
) -> list[str]:
    """Step 6: re-run the window path and closing runs (steps 3 and 4) for every usage_runs row still 'open' on
    a card that plan_tasks no longer points its task at (a card process_merge_queue replaced with a fix card
    after this module's own outgoing-card ingest already ran, or one replaced by any other path that never
    called it). ingest_run_usage only ever reads a task's CURRENT card, so without this an outgoing card's
    still-open runs would never be looked at again. Returns the session ids counted by this call."""
    now = time.time() if now is None else float(now)
    rows = conn.execute(
        "SELECT DISTINCT card_id, project, task_key FROM usage_runs WHERE board = ? AND state = 'open'", (board,),
    ).fetchall()
    touched: list[str] = []
    for row in rows:
        card_id, plan_project, task_key = row["card_id"], row["project"], row["task_key"]
        if plan_project is not None and task_key is not None:
            current = conn.execute(
                "SELECT work_card_id FROM plan_tasks WHERE project = ? AND task_key = ?",
                (plan_project, task_key),
            ).fetchone()
            if current is not None and current["work_card_id"] == card_id:
                continue    # still the plan's current card: ingest_run_usage/ingest_card_usage already cover it
        attribution = (plan_project, task_key, card_id)
        touched += _ingest_window_path(conn, board, card_id, project, models_config, attribution, now)
        touched += _close_runs_without_sessions(conn, board, card_id, project, models_config, attribution, now)
    return touched


def settle_open_sessions(
    project: ases_config.ProjectConfig, models_config: dict, *, conn, now: float | None = None,
) -> list[str]:
    """Step 5: every usage_ingested row with settled=0 (regardless of plan_tasks, so an outgoing fix-card's
    sessions and a pre-migration legacy row are both included) is re-exported; a growing count is a monotone
    top-up to the ledger (never a decrease: ledger.record_usage refuses n<=0 anyway, and a count that went down
    records one usage_count_regressed event instead). A row settles (settled=1) once its session has ended, or
    its run ended SETTLE_AFTER ago and its count has been stable for at least `_STABILITY_WINDOW` seconds
    (a killed worker's session never ends), or SETTLE_HARD_STOP has passed regardless (which also records
    usage_settle_timeout, so a row is never watched forever). A legacy row (no run_started_at/run_ended_at of
    its own) uses its own ingested_at in run_ended_at's place. Returns the session ids that settled this call."""
    now = time.time() if now is None else float(now)
    rows = conn.execute(
        "SELECT session_id, profile, provider, model, requests, board, run_id, project, task_key, card_id, "
        "ingested_at, last_check_at, last_check_count FROM usage_ingested WHERE settled = 0"
    ).fetchall()
    newly_settled: list[str] = []
    for row in rows:
        usage = hermes_mod.session_usage(row["profile"], row["session_id"])
        if usage is None:
            continue    # unknown: ask again later, never treated as zero and never treated as settled

        run_ended_at = None
        if row["board"] is not None and row["run_id"] is not None:
            run = conn.execute(
                "SELECT run_ended_at FROM usage_runs WHERE board = ? AND run_id = ?", (row["board"], row["run_id"]),
            ).fetchone()
            if run is not None:
                run_ended_at = run["run_ended_at"]
        if run_ended_at is None:
            run_ended_at = _parse_utc(row["ingested_at"])

        observed = int(usage.get("api_call_count") or 0)
        delta = observed - row["requests"]
        stable = (
            row["last_check_count"] is not None and row["last_check_count"] == observed
            and row["last_check_at"] is not None and (now - row["last_check_at"]) >= _STABILITY_WINDOW
        )
        ended = usage.get("ended_at") not in (None, "")
        hard_stop = run_ended_at is not None and now >= run_ended_at + SETTLE_HARD_STOP
        settled_now = ended or hard_stop or (
            run_ended_at is not None and now >= run_ended_at + SETTLE_AFTER and stable
        )

        with _atomic(conn):
            if delta > 0:
                charge = _charge_time(usage, now)
                ledger.record_usage(conn, row["provider"], row["model"], delta, now=_as_utc(charge))
                conn.execute(
                    "UPDATE usage_ingested SET requests = ?, input_tokens = ?, output_tokens = ? "
                    "WHERE session_id = ?",
                    (observed, int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0),
                     row["session_id"]),
                )
                events.record(conn, "usage_topped_up", {
                    "session_id": row["session_id"], "run_id": row["run_id"], "added": delta, "total": observed,
                    "utc_date": _as_utc(charge).strftime("%Y-%m-%d"),
                }, project=row["project"])
            elif delta < 0:
                events.record(conn, "usage_count_regressed", {
                    "session_id": row["session_id"], "run_id": row["run_id"], "was": row["requests"],
                    "now": observed,
                }, project=row["project"])
            conn.execute(
                "UPDATE usage_ingested SET last_check_at = ?, last_check_count = ? WHERE session_id = ?",
                (int(now), observed, row["session_id"]),
            )
            if settled_now:
                conn.execute("UPDATE usage_ingested SET settled = 1 WHERE session_id = ?", (row["session_id"],))
                if row["board"] is not None and row["run_id"] is not None:
                    _maybe_close_run(conn, row["board"], row["run_id"])
                if hard_stop:
                    events.record(conn, "usage_settle_timeout", {
                        "session_id": row["session_id"], "run_id": row["run_id"],
                    }, project=row["project"])
                newly_settled.append(row["session_id"])
    return newly_settled


def lineage_requests(conn: sqlite3.Connection, plan_project: str, task_key: str) -> int:
    """Total model requests counted so far across a plan task's whole lineage (the original card and every fix
    card, ASES-REC-02), from the usage_ingested rows attributed to it."""
    row = conn.execute(
        "SELECT COALESCE(SUM(requests), 0) AS total FROM usage_ingested WHERE project = ? AND task_key = ?",
        (plan_project, task_key),
    ).fetchone()
    return int(row["total"])


def reviewer_run_identities(conn: sqlite3.Connection, board: str, run_id) -> list[tuple[str | None, str | None]] | None:
    """The (model, provider) pairs the ledger's own session attribution has recorded for one run, read back
    from usage_ingested (round 19, package MERGEGUARD: the merge queue's ASES-ROL-05 belt-and-braces check at
    merge, controller.process_merge_queue, fix D3 of REVIEWER.md design (a) "Belt and braces at merge"). A
    per-card model_override/provider_override is lane-blind (r19 REVIEWER.md finding F3): a switch pinned for a
    coder's replacement card can leak into that same card's own reviewer run, so the merge queue must judge the
    completing reviewer run's ACTUAL model/provider, never the profile name alone.

    None when no usage_ingested row for this run_id exists yet: the session has not been ingested (this
    module's metadata/window/list paths each run on their own schedule, see the module docstring), which is
    UNKNOWN, never assumed independent -- the caller must wait, not proceed.

    Otherwise one (model, provider) pair per usage_ingested row this run_id has (ordinarily one; more only in
    the rare mapping-ambiguity case _maybe_record_ambiguity already flags separately). `provider` prefers the
    row's own `billing_provider` (carried straight off Hermes's session export, hermes.py:448-461: the provider
    actually billed for the session) over usage_ingested's own `provider` column (this module's own attribution,
    _provider_actually_hit's best guess, used elsewhere only to flag a model_mismatch event) -- falling back to
    it only when a row has no billing_provider at all (that column is never NULL itself: _count_session always
    fills it, either from the pinned profile or the guessed host)."""
    rows = conn.execute(
        "SELECT model, provider, billing_provider FROM usage_ingested WHERE board = ? AND run_id = ?",
        (board, run_id),
    ).fetchall()
    if not rows:
        return None
    pairs = [(row["model"], row["billing_provider"] or row["provider"]) for row in rows]
    if any(not model or not provider for model, provider in pairs):
        # A row with no model (or no provider) cannot show the family and provider are the Lead's or not: that is
        # UNKNOWN, exactly like no row at all, never "independent" (round 19 architect fix of the reviewer's
        # MERGEGUARD finding: _reviewer_run_not_independent would otherwise read a NULL model as a different family).
        return None
    return pairs


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
