"""Failure classification, the two kinds of retry, and lineage budgets (section 19.1 to 19.3; ASES-REC-01,
ASES-REC-02).

Hermes already retries a card by itself: a crashed, timed-out or reclaimed run is re-queued, and its circuit
breaker gives up (the card goes `blocked` and a `gave_up` event is written) after a few consecutive failures.
What Hermes cannot do is say what a failure MEANS. An infrastructure failure says nothing about the model or the
task and deserves a plain resume; a capability failure means the attempt itself was wrong and deserves a fresh
attempt, then a different model. Hermes also counts attempts per CARD, and every fix card starts a new counter,
so a task could loop for ever one card at a time. This module adds the two missing pieces: classify_run() and
decide() turn a failed run into the response the blueprint prescribes, and the lineage counters (per plan
task, across the original card and every card it spawned) bound the whole thing.

The classification table is grounded in what Hermes 0.21.3 really writes (hermes_cli/kanban_db.py and
kanban_db_dispatch.py, read 2026-09-19). The run `outcome` values it uses:
  completed, review_requested, changes_requested, blocked, scheduled   a run that ended normally
  rate_limited   the worker exited 75 (EX_TEMPFAIL): a provider quota wall, never counted as a failure, the card
                 is re-queued after a cooldown
  timed_out      --max-runtime elapsed ("elapsed Ns > limit Ms") or the worker ran out of iterations
  stale          no heartbeat (not counted as a failure by Hermes)
  crashed        the worker died: "pid N exited with code C", "pid N killed by signal S", "pid N not alive", or
                 a clean exit without a terminal kanban call (metadata.protocol_violation, "protocol violation")
  reclaimed      the claim TTL ran out ("stale_lock=..."), or an operator reclaimed the card
  spawn_failed   the workspace or the worker could not be started
  gave_up        the breaker tripped on the spawn path; metadata.trigger_outcome names what tripped it
Other provider errors reach us as text inside `error` (Hermes appends the worker's last output to a crash), so
after the outcome the text rules below decide.

Counting convention, used everywhere here: the lineage counters INCLUDE the failure being decided. The first
capability failure is decided with capability_failures == 1, and attempts_per_card (3) means three attempts,
not four. process_failures() therefore counts in memory first, decides, applies, and only then writes the
counters and the decision record in one savepoint, so a Hermes call that fails leaves the database untouched
and the same failure is simply decided again on the next pass.

Nothing here calls a provider or starts a process. The only outside calls are the hermes.kanban_* wrappers and
questions.ask_user, and creating the replacement card of a fresh attempt or of a model switch, or asking the Lead
for a re-plan, is the controller's job.

Asking the user goes through questions.ask_user and never through hermes.kanban_block directly. The cards this
module acts on are `blocked` already (Hermes's circuit breaker gave up on them and wrote a `gave_up` event, and no
`blocked` event), and Hermes's block_task accepts only a `running` or `ready` card: a block on a blocked card exits
1 after it has left its comment behind (checked against the 0.21.3 source, 2026-09-21), so the question goes on the
card as a comment instead, which `swarm questions` finds, and a question already open comes back as
"already_asked" and is not put twice.

Round 6 fix: `recovery.Bounds` used to be its own 4-field dataclass with a lenient `from_budgets` (a bad value fell
back to the default). It is now an alias of `bounds.Bounds` (bounds.py's own 8-field, strictly-parsed dataclass,
the single source of truth for section 9.3's bounds), kept for backward compatibility; prefer importing
`bounds.Bounds` directly in new code.
"""
from __future__ import annotations

import contextlib
import dataclasses
import enum
import json
import math
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timezone

from . import bounds as bounds_mod
from . import config as ases_config
from . import events
from . import hermes as hermes_mod
from . import models as models_mod
from . import plan as plan_mod
from . import policy
from . import questions as questions_mod
from . import usage as usage_mod


# ---------------------------------------------------------------------------------------------
# Failure classification (section 19.1)
# ---------------------------------------------------------------------------------------------


class FailureKind(enum.StrEnum):
    """What a failed run says about the world. A str enum, so a member equals its lower-case value
    (FailureKind.RATE_LIMIT == "rate_limit") and serialises into an event payload as plain text.

    NONE            the run ended normally (completed, review_requested, changes_requested, scheduled) or with a
                    deliberate block: a worker asking the user a question is not a failure
    RATE_LIMIT      a short provider throttle (429, Retry-After): Hermes waits and retries, no model rotation
    QUOTA           a daily or per-day allowance is used up: park the card until the reset
    INFRASTRUCTURE  a 5xx, a timeout, a dropped connection, a dead worker: says nothing about the model or task
    AUTH            401 or 403: the credential is bad, and retrying would only loop
    POLICY          the provider refused on data policy or has no endpoint: never relax the data class
    CONTEXT         the request does not fit the model (context window, request too large)
    TOOL_CALLING    malformed or invalid tool calls: the model must be rejected for agent roles
    CAPABILITY      the attempt itself was wrong (for example a worker that ends without a terminal kanban call)
    RUNTIME         the run exceeded its runtime or iteration budget: counts as an attempt, like a capability failure
    UNKNOWN         a failed run that says nothing recognisable
    """

    NONE = "none"
    RATE_LIMIT = "rate_limit"
    QUOTA = "quota"
    INFRASTRUCTURE = "infrastructure"
    AUTH = "auth"
    POLICY = "policy"
    CONTEXT = "context"
    TOOL_CALLING = "tool_calling"
    CAPABILITY = "capability"
    RUNTIME = "runtime"
    UNKNOWN = "unknown"


# Run outcomes that are not failures. "done", "review" and "running" are run STATUSES: they are only consulted when
# a run has no outcome yet (an open run), which is never a failure either.
_ENDED_NORMALLY = frozenset({
    "completed", "review_requested", "changes_requested", "blocked", "scheduled", "done", "review", "running",
})

# What an outcome means when the error text names nothing more specific. crashed and gave_up are absent on
# purpose: a crash with no text at all is the one thing this module cannot classify, and gave_up borrows the
# outcome of the failure that tripped the breaker (metadata.trigger_outcome).
_OUTCOME_DEFAULT = {
    "spawn_failed": FailureKind.INFRASTRUCTURE,   # the worker never started: nothing about the model or task
    "stale": FailureKind.INFRASTRUCTURE,          # no heartbeat: "worker crash or stale claim" (19.1)
    "reclaimed": FailureKind.INFRASTRUCTURE,      # the claim expired under a live or dead worker (19.1)
}

# A 4xx or 5xx status is only a status when it is not a quantity: "Limit 500, Used 500" is a quota message.
_NOT_A_QUANTITY = r"(?<!limit )(?<!used )(?<!requested )(?<!max )"

# Text rules, most specific first; the first rule that matches wins. The order is deliberate. Phrases that name one
# exact problem (policy, context, quota) come before the bare HTTP codes, because one provider message often carries
# several: UnoRouter's "HTTP 429: Rate limit reached ... on tokens per day (TPD)" contains both 429 and "rate limit"
# but is a DAILY quota that a short wait will not cure. The stop-the-line kinds (auth, tool calling) come before the
# retryable infrastructure kinds, because retrying an auth failure is the loop 19.1 forbids, while escalating a
# transient one only costs a question. Hermes's own crash bookkeeping is the weakest signal and comes last.
_TEXT_RULES: tuple[tuple[FailureKind, re.Pattern[str]], ...] = tuple(
    (kind, re.compile(pattern, re.IGNORECASE))
    for kind, pattern in (
        # Hermes: enforce_max_runtime writes "elapsed 2700s > limit 2700s"; a worker that spends its iteration budget
        # writes "Iteration budget exhausted (90/90)". 19.1: "Worker exceeds its runtime ... counts as an attempt".
        (FailureKind.RUNTIME, r"elapsed \d+s? *> *limit|iteration budget exhausted|max[-_ ]?runtime"),
        # 19.1 "Data policy mismatch, 404 no endpoints". OpenRouter answers 404 "No endpoints found matching your data
        # policy", and its routing parameter is called data_collection.
        (FailureKind.POLICY, r"no endpoints found|data[-_ ]?polic(?:y|ies)|data_collection"),
        # 19.1 "Context too small". OpenAI: "maximum context length", code context_length_exceeded. UnoRouter's real
        # OTPM error is "Request too large for model ... Requested 2048": a request the model cannot take, which a
        # retry cannot fix.
        (FailureKind.CONTEXT, r"context[-_ ]?length|maximum context|context[-_ ]?window|request too large"
                              r"|prompt is too long"),
        # 19.1 "Daily quota exhausted". OpenAI's 429 says "You exceeded your current quota"; OpenRouter's free tier
        # says "free-models-per-day"; UnoRouter's says "tokens per day (TPD)".
        (FailureKind.QUOTA, r"quota|daily|exceeded your current|per[-_ ]?day|\btpd\b"),
        # 19.1 "Rate limit 429": the status, the phrase, or a Retry-After header.
        (FailureKind.RATE_LIMIT, _NOT_A_QUANTITY + r"\b429\b|rate[-_ ]?limit|retry[-_ ]?after|too many requests"),
        # 19.1 "Auth 401 or 403". xKiro's real 403 says "This premium model requires an active paid plan".
        (FailureKind.AUTH, _NOT_A_QUANTITY + r"\b40[13]\b|invalid[-_ ]?api[-_ ]?key|authentication|unauthorized"
                                             r"|forbidden"),
        # 19.1 "Tool calling broken". tool_use is Anthropic's name for a tool call block.
        (FailureKind.TOOL_CALLING, r"tool[-_ ]?calls?|tool_use|malformed function|invalid tool"),
        # 19.1 "Server 5xx, timeout".
        (FailureKind.INFRASTRUCTURE, _NOT_A_QUANTITY + r"\b50[0234]\b|time[-_ ]?out|timed out"
                                                       r"|connection (?:reset|error|refused|aborted|closed)"
                                                       r"|service unavailable|temporarily unavailable|bad gateway"
                                                       r"|internal server error|overloaded"),
        # Hermes marks a worker that exited 0 without calling kanban_complete or kanban_block as a protocol violation.
        # The model did not do what the hand-off requires: the attempt was wrong, which 19.2 calls a capability failure.
        (FailureKind.CAPABILITY, r"protocol violation"),
        # 19.1 "Worker crash or stale claim": Hermes's own words for a dead worker or an expired claim. An
        # infrastructure failure by the blueprint, and only recognised when nothing sharper matched above (the crash
        # text often carries the provider error the worker died of).
        (FailureKind.INFRASTRUCTURE, r"exited with code|killed by signal|not alive|stale_lock="),
    )
)


def _word(value) -> str:
    return str(value).strip().lower() if value is not None else ""


def _text(value) -> str:
    return str(value) if value is not None else ""


def _metadata(value) -> dict:
    """A run's metadata as a dict. Hermes hands it over as a dict or as a JSON string, and either may be absent."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):
            return {}
    return value if isinstance(value, dict) else {}


def _kind_from_text(text: str) -> FailureKind | None:
    for kind, pattern in _TEXT_RULES:
        if pattern.search(text):
            return kind
    return None


def classify_run(run: dict) -> FailureKind:
    """What one run of a card's `_runs` means (ASES-REC-01, blueprint 19.1). Pure: the dict is only read.

    The outcome comes first, because it is Hermes's own verdict: a run that ended normally is NONE whatever its
    text says (a worker that blocks itself to ask the user about a 429 is asking a question, not failing),
    `rate_limited` is RATE_LIMIT and `timed_out` is RUNTIME even when the error text mentions something else.
    Every other outcome fails for a reason the outcome does not name, so the error text is read next (then the
    summary), through the ordered rules above. Only when the text says nothing does the outcome give a default:
    spawn_failed, stale and reclaimed are infrastructure, and gave_up takes the outcome of the failure that
    tripped the breaker (metadata.trigger_outcome). A crashed or gave_up run with no recognisable text at all is
    UNKNOWN, as is a run with no outcome and no text: this function never guesses capability from silence."""
    if not isinstance(run, dict):
        return FailureKind.UNKNOWN
    outcome = _word(run.get("outcome")) or _word(run.get("status"))
    if outcome in _ENDED_NORMALLY:
        return FailureKind.NONE
    if outcome == "rate_limited":
        return FailureKind.RATE_LIMIT
    if outcome == "timed_out":
        return FailureKind.RUNTIME

    metadata = _metadata(run.get("metadata"))
    texts = [_text(run.get("error")), _text(run.get("summary"))]
    if metadata.get("protocol_violation"):
        texts.append("protocol violation")     # Hermes's durable marker, for a run whose text was trimmed
    for text in texts:
        kind = _kind_from_text(text)
        if kind is not None:
            return kind

    if outcome == "gave_up":
        outcome = _word(metadata.get("trigger_outcome"))
        if outcome == "timed_out":
            return FailureKind.RUNTIME
    return _OUTCOME_DEFAULT.get(outcome, FailureKind.UNKNOWN)


# ---------------------------------------------------------------------------------------------
# Lineage counters and bounds (section 19.3, 9.3)
# ---------------------------------------------------------------------------------------------


@dataclasses.dataclass
class Lineage:
    """Everything counted for one plan task across the original card and every fix card it spawned (ASES-REC-02).
    Counters are per TASK, because every fix card starts a new card-level counter. `requests` is informational:
    the blueprint counts it (19.3) but section 9.3 sets no bound for it, so exhausted() ignores it."""
    project: str
    task_key: str
    review_rounds: int = 0
    capability_failures: int = 0
    infra_failures: int = 0
    replans: int = 0
    fix_cards: int = 0
    requests: int = 0


# The counters a caller may increment. A whitelist, because the column name is put into the SQL text.
_BUMP_FIELDS = ("review_rounds", "capability_failures", "infra_failures", "replans")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_lineage(conn: sqlite3.Connection, project: str, task_key: str) -> Lineage:
    """The lineage of one plan task (ASES-REC-02). A task with no lineage row has all-zero counters. fix_cards
    comes from plan_tasks (process_merge_queue bumps it when it opens a fix card), and requests is summed from the
    usage_ingested rows attributed to the task (usage.lineage_requests)."""
    row = conn.execute(
        "SELECT review_rounds, capability_failures, infra_failures, replans FROM lineage "
        "WHERE project = ? AND task_key = ?", (project, task_key),
    ).fetchone()
    task = conn.execute(
        "SELECT fix_cards FROM plan_tasks WHERE project = ? AND task_key = ?", (project, task_key),
    ).fetchone()
    return Lineage(
        project=project, task_key=task_key,
        review_rounds=int(row["review_rounds"]) if row else 0,
        capability_failures=int(row["capability_failures"]) if row else 0,
        infra_failures=int(row["infra_failures"]) if row else 0,
        replans=int(row["replans"]) if row else 0,
        fix_cards=int(task["fix_cards"]) if task else 0,
        requests=usage_mod.lineage_requests(conn, project, task_key),
    )


def bump(conn: sqlite3.Connection, project: str, task_key: str, field: str, n: int = 1) -> None:
    """Add n to one lineage counter (ASES-REC-02), creating the row when the task has none, and stamp updated_at
    (UTC, seconds). The field is checked against a whitelist and anything else is a ValueError: it is spliced into
    the SQL. n must be positive, as in ledger.record_usage: a budget only ever goes up."""
    if field not in _BUMP_FIELDS:
        raise ValueError(f"cannot bump {field!r}: expected one of {list(_BUMP_FIELDS)}")
    if n < 1:
        raise ValueError("n must be positive")
    conn.execute(
        f"INSERT INTO lineage (project, task_key, {field}, updated_at) VALUES (?, ?, ?, ?) "
        f"ON CONFLICT(project, task_key) DO UPDATE SET {field} = {field} + excluded.{field}, "
        "updated_at = excluded.updated_at",
        (project, task_key, n, _utc_now()),
    )


# What Hermes writes when a review sends a card back: the reviewer's request-changes, and the controller's own
# reopen-review after a red Gate 1 re-check. Each one is a review round.
_REVIEW_EVENT_KINDS = frozenset({"changes_requested", "review_reopened"})

# What a hermes.kanban_* wrapper can raise: a non-zero exit, no hermes on PATH, a hung command, or output that is not
# JSON. One of these on one card must never stop the pass for the other cards.
_HERMES_ERRORS = (
    hermes_mod.HermesCommandError, hermes_mod.HermesNotFound, subprocess.TimeoutExpired, OSError, ValueError,
)


def refresh_review_rounds(board: str, plan: plan_mod.Plan, *, conn: sqlite3.Connection) -> dict[str, int]:
    """Count review rounds per plan task from the board (ASES-REC-02: "review rounds ... across the original card
    and everything it spawned"). Returns {task_key: rounds added by this call} for every task whose current work
    card could be read, 0 for a task with nothing new.

    A round is a `changes_requested` or `review_reopened` event on the task's CURRENT work card
    (plan_tasks.work_card_id, which process_merge_queue repoints at each fix card). The lineage row remembers which
    card those events were counted for (seen_card) and how many (seen_events): only the difference is added, so
    the call is idempotent, and when a fix card has taken over (seen_card differs) its own events count from zero
    while the rounds already counted stay in review_rounds. A card that cannot be read is skipped, with a
    `recovery_error` event, and the other tasks carry on."""
    added: dict[str, int] = {}
    for task in plan.tasks:
        row = conn.execute(
            "SELECT work_card_id FROM plan_tasks WHERE project = ? AND task_key = ?", (plan.project, task.key),
        ).fetchone()
        if row is None or not row["work_card_id"]:
            continue
        card_id = row["work_card_id"]
        try:
            card = hermes_mod.kanban_show(board, card_id)
        except _HERMES_ERRORS as exc:
            _record_error_once(conn, plan.project, task.key, card_id, "refresh_review_rounds", exc)
            continue
        count = sum(
            1 for event in card.get("_events") or []
            if isinstance(event, dict) and event.get("kind") in _REVIEW_EVENT_KINDS
        )
        stored = conn.execute(
            "SELECT seen_card, seen_events FROM lineage WHERE project = ? AND task_key = ?",
            (plan.project, task.key),
        ).fetchone()
        seen_card = stored["seen_card"] if stored else None
        seen_events = int(stored["seen_events"]) if stored else 0
        if seen_card != card_id:
            seen_events = 0            # a fix card took over: its events are counted from zero
        delta = max(count - seen_events, 0)
        new_seen = max(count, seen_events)
        changed = delta > 0 or (
            stored is not None and (stored["seen_card"] != card_id or int(stored["seen_events"]) != new_seen)
        )
        if changed or (stored is None and count > 0):
            conn.execute(
                "INSERT INTO lineage (project, task_key, review_rounds, seen_card, seen_events, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(project, task_key) DO UPDATE SET "
                "review_rounds = review_rounds + excluded.review_rounds, seen_card = excluded.seen_card, "
                "seen_events = excluded.seen_events, updated_at = excluded.updated_at",
                (plan.project, task.key, delta, card_id, new_seen, _utc_now()),
            )
        added[task.key] = delta
    return added


# recovery.Bounds is now an alias of bounds.Bounds, not a second, independently-defined class (that was the bug:
# this module used to keep its own 4-field Bounds with a lenient from_budgets, while bounds.py had an 8-field
# Bounds with a strict one, for the same section 9.3 concept). bounds.Bounds is a superset (8 fields vs. 4) with
# the same names and defaults for the 4 this module reads, so exhausted() and decide() are unaffected by the
# extra fields, and the strict parser now wins: a present budget value that is a bool, negative, or not already
# an int now raises ValueError instead of silently falling back to the default (bounds.Bounds.from_budgets's
# docstring explains why: a typo should stop the project at load time, not quietly mean "no bound"). A genuinely
# MISSING key still takes the section 9.3 default exactly as before.
Bounds = bounds_mod.Bounds


def exhausted(lineage: Lineage, bounds: bounds_mod.Bounds) -> str | None:
    """The name of the FIRST lineage budget that has run out, or None (ASES-REC-02: "When a lineage budget runs
    out, the Lead may re-plan that task once ... After that the controller blocks the task with a question for
    the user"). Checked in the order review_rounds, fix_cards, attempts:
      "review_rounds"  review_rounds >= bounds.review_rounds_per_task
      "fix_cards"      fix_cards >= bounds.fix_cards_per_task
      "attempts"       capability_failures >= bounds.attempts_per_card
    Infrastructure failures are not in it: they say nothing about the task, and have their own cap in decide()."""
    if lineage.review_rounds >= bounds.review_rounds_per_task:
        return "review_rounds"
    if lineage.fix_cards >= bounds.fix_cards_per_task:
        return "fix_cards"
    if lineage.capability_failures >= bounds.attempts_per_card:
        return "attempts"
    return None


# ---------------------------------------------------------------------------------------------
# Decisions (section 19.1 to 19.3)
# ---------------------------------------------------------------------------------------------

ACTION_NONE = "none"                                        # nothing to do (Hermes's own retry, or not a failure)
ACTION_RESUME = "resume"                                    # unblock: same card, same worktree, same model
ACTION_FRESH_ATTEMPT = "fresh_attempt"                      # a NEW card from a fresh worktree, failure bundle attached
ACTION_SWITCH_MODEL = "switch_model"                        # a NEW card from a fresh worktree, on the next model
ACTION_PARK = "park"                                        # schedule the card until the provider's reset
ACTION_REPLAN = "replan"                                    # the Lead may re-plan this task once
ACTION_BLOCK_FOR_USER = "block_for_user"                    # a question for the user
ACTION_MARK_CREDENTIAL_UNHEALTHY = "mark_credential_unhealthy"   # auth: record it, block for the user, never loop
ACTIONS = frozenset({
    ACTION_NONE, ACTION_RESUME, ACTION_FRESH_ATTEMPT, ACTION_SWITCH_MODEL, ACTION_PARK, ACTION_REPLAN,
    ACTION_BLOCK_FOR_USER, ACTION_MARK_CREDENTIAL_UNHEALTHY,
})

INFRA_BACKOFF_BASE_SECONDS = 30
INFRA_BACKOFF_CAP_SECONDS = 900

# Round 7 (ASES-REC-01, bug 3): how long _recover_task waits, after a `ready` card's latest run ended, before
# treating an auth- or quota-shaped failure on it as needing recovery (see _recover_task's own docstring for
# why a ready card is looked at at all). Hermes's own respawn guard for these two kinds
# (kanban_db_dispatch.check_respawn_guard's `blocker_auth` rule, read from the installed 0.21.3 source: once
# task.last_failure_error matches an auth/quota-shaped regex, the guard holds the card in `ready` with NO
# timer of its own) never lifts on its own, so this is not a wait for anything Hermes will eventually do.
# It is ASES's own margin against a run that only just ended, on the chance Hermes's dispatcher has not even
# had one tick yet to apply the guard. Reuses INFRA_BACKOFF_BASE_SECONDS's own 30 s rather than inventing a
# new number: real Hermes's DEFAULT_CRASH_GRACE_SECONDS (kanban_db.py) independently agrees at 30 s for the
# related "has this worker really gone quiet" judgment, so 30 s is at least as patient as Hermes's own
# thresholds for a comparable question.
READY_RESPAWN_SETTLE_SECONDS = INFRA_BACKOFF_BASE_SECONDS


@dataclasses.dataclass(frozen=True)
class Decision:
    """What to do about one failed run. `reason` is a sentence a person can read; for block_for_user and
    mark_credential_unhealthy it is a question, because it becomes the reason the card is blocked with.
    `model` and `provider` are set for switch_model and mark_credential_unhealthy by process_failures (decide() has
    no model list). The last four fields are filled in by process_failures so that a list of decisions says which
    card each one is about: without them the controller could not tell which task a fresh_attempt or a replan
    belongs to. `run_id` is the failed run this decision answers.

    Who does what for the three decisions process_failures only RETURNS: for fresh_attempt the controller creates the
    replacement card (fresh worktree at the current integration HEAD, failure_bundle() attached) and repoints
    plan_tasks.work_card_id at it; switch_model is the same, and the controller also pins `model` and `provider` on the
    replacement card (ASES-REC-01, 19.2: a capability failure restarts from a fresh worktree, and the second one also
    changes model, so the switch is never applied to the failed card in place); for replan the controller asks the Lead
    once and calls bump(conn, project, task_key, "replans") when it does, which is what stops the next decide()
    offering a second re-plan."""
    action: str
    reason: str
    backoff_seconds: int = 0
    model: str | None = None
    provider: str | None = None
    task_key: str | None = None
    card_id: str | None = None
    run_id: int | str | None = None
    failure_kind: FailureKind | None = None

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise ValueError(f"unknown action {self.action!r}: expected one of {sorted(ACTIONS)}")


def backoff_seconds(infra_failures: int) -> int:
    """The wait before resuming after an infrastructure failure (ASES-REC-01: "resume ... after a backoff"): 30 s,
    doubling per PRIOR infrastructure failure, capped at 900 s. `infra_failures` counts the failure being decided
    (see the module docstring), so the first failure waits 30 s, the second 60 s, the third 120 s."""
    prior = min(max(infra_failures - 1, 0), 10)      # the exponent is capped so a corrupt counter cannot overflow
    return min(INFRA_BACKOFF_BASE_SECONDS * 2 ** prior, INFRA_BACKOFF_CAP_SECONDS)


_BUDGET_LABELS = {
    "review_rounds": "review-round",
    "fix_cards": "fix-card",
    "attempts": "attempt",
}


def escalation(lineage: Lineage, bounds: bounds_mod.Bounds) -> Decision | None:
    """The response when a lineage budget has run out, or None while none has (ASES-REC-02, section 9.3 "Escalate to
    the Lead where allowed, then a blocked card for the user"): a `replan` while the task has not been re-planned
    yet (the Lead may re-plan it ONCE, with the full failure bundle), a `block_for_user` question after that.
    decide() uses it for capability and runtime failures; the controller can call it after refresh_review_rounds()
    to escalate a task whose review rounds or fix cards are spent, which no failed run announces."""
    spent = exhausted(lineage, bounds)
    if spent is None:
        return None
    detail = {
        "review_rounds": f"{lineage.review_rounds} review rounds, limit {bounds.review_rounds_per_task}",
        "fix_cards": f"{lineage.fix_cards} fix cards, limit {bounds.fix_cards_per_task}",
        "attempts": f"{lineage.capability_failures} failed attempts, limit {bounds.attempts_per_card}",
    }[spent]
    label = _BUDGET_LABELS[spent]
    if lineage.replans == 0:
        return Decision(ACTION_REPLAN, (
            f"The {label} budget of this task is spent ({detail}). The Lead may re-plan the task once, with the "
            "full failure bundle."
        ))
    return Decision(ACTION_BLOCK_FOR_USER, (
        f"The {label} budget of this task is spent again ({detail}) after its one re-plan. Should ASES change the "
        "acceptance criteria, take a different approach, or stop this task?"
    ))


def decide(
    kind: FailureKind | str, lineage: Lineage, bounds: bounds_mod.Bounds, *,
    provider_reset_text: str = "the next UTC midnight", consecutive_unknown: int = 0,
) -> Decision:
    """The response to one failure of `kind` (ASES-REC-01, ASES-REC-02, blueprint 19.1, 19.2, 19.3, 11.1). Pure.
    `lineage` counts the failure being decided (module docstring); `consecutive_unknown` is how many runs in a row
    were UNKNOWN, which only the caller can see (process_failures reads it off the card's runs).

    RATE_LIMIT      none: Hermes waits for Retry-After and retries, the ledger records it, no model rotation
    QUOTA           park: schedule the card until `provider_reset_text`
    INFRASTRUCTURE  resume in the same worktree with the same model after backoff_seconds(), until infra_failures
                    reaches attempts_per_card, then block_for_user
    AUTH            mark_credential_unhealthy: record it, block for the user, never loop
    POLICY          block_for_user: the data class is never relaxed automatically (ASES-PRV-01)
    CONTEXT         block_for_user: the model must be rejected for agent roles, and a verified 64K model chosen
    TOOL_CALLING    block_for_user: the model must be rejected for agent roles
    CAPABILITY,     when a lineage budget is spent (escalation): replan while lineage.replans == 0, else
    RUNTIME         block_for_user. Otherwise fresh_attempt on the first capability failure and switch_model from
                    the second on (both start from a fresh worktree; the second also takes the next model). A
                    runtime overrun counts as a capability failure ("counts as an attempt")
    UNKNOWN         none until attempts_per_card runs in a row were unknown ("Hermes's own retry runs first"), then
                    block_for_user
    NONE            none"""
    kind = FailureKind(kind)
    if kind is FailureKind.NONE:
        return Decision(ACTION_NONE, "The run ended normally, so there is nothing to recover.")
    if kind is FailureKind.RATE_LIMIT:
        return Decision(ACTION_NONE, (
            "Provider rate limit (429): Hermes waits for Retry-After and retries, the ledger records the requests, "
            "and the model is not rotated."
        ))
    if kind is FailureKind.QUOTA:
        return Decision(ACTION_PARK, (
            f"The provider's daily quota is used up. Park the card until {provider_reset_text}, when the quota "
            "resets: retrying sooner only burns requests."
        ))
    if kind is FailureKind.INFRASTRUCTURE:
        failures = lineage.infra_failures
        if failures >= bounds.attempts_per_card:
            return Decision(ACTION_BLOCK_FOR_USER, (
                f"This task has hit {failures} infrastructure failures (the limit is {bounds.attempts_per_card}): "
                "the provider or the worker keeps failing, and retrying blindly would only burn requests. Should "
                "ASES try again later, move the task to another provider, or stop it?"
            ))
        wait = backoff_seconds(failures)
        return Decision(ACTION_RESUME, (
            f"Infrastructure failure {max(failures, 1)} of {bounds.attempts_per_card}. It says nothing about the "
            f"model or the task, so resume in the same worktree with the same model after a {wait} s backoff."
        ), backoff_seconds=wait)
    if kind is FailureKind.AUTH:
        return Decision(ACTION_MARK_CREDENTIAL_UNHEALTHY, (
            "The provider rejected the credential (401 or 403). ASES marks it unhealthy and will not retry with it. "
            "Can you check or replace the key for this provider, or should the task move to another provider?"
        ))
    if kind is FailureKind.POLICY:
        return Decision(ACTION_BLOCK_FOR_USER, (
            "The provider refused the request on a data policy, or has no endpoint that matches it. ASES never "
            "relaxes the data class by itself. Should this task run on a different provider that the project's "
            "data class allows?"
        ))
    if kind is FailureKind.CONTEXT:
        return Decision(ACTION_BLOCK_FOR_USER, (
            "The request does not fit the model (context window or request size), so the model must be rejected "
            "for agent roles. Should it be replaced by a model with a verified context of at least 64K?"
        ))
    if kind is FailureKind.TOOL_CALLING:
        return Decision(ACTION_BLOCK_FOR_USER, (
            "The model produced malformed or invalid tool calls, so it must be rejected for agent roles. Should it "
            "be replaced by a model whose tool-calling smoke test passed?"
        ))
    if kind in (FailureKind.CAPABILITY, FailureKind.RUNTIME):
        spent = escalation(lineage, bounds)
        if spent is not None:
            return spent
        attempt = max(lineage.capability_failures, 1)
        what = ("ran out of its runtime or iteration budget" if kind is FailureKind.RUNTIME
                else "was wrong (a capability failure)")
        if attempt == 1:
            return Decision(ACTION_FRESH_ATTEMPT, (
                f"The attempt {what}. Start the next one from a fresh worktree at the current integration HEAD, "
                f"with the failure bundle attached (failed attempt 1 of {bounds.attempts_per_card})."
            ))
        return Decision(ACTION_SWITCH_MODEL, (
            f"The attempt {what} again (failed attempt {attempt} of {bounds.attempts_per_card}). Start the next one "
            "from a fresh worktree at the current integration HEAD, with the failure bundle attached, on the next "
            "model for its role class."
        ))
    # UNKNOWN: a failed run that says nothing recognisable.
    if consecutive_unknown >= bounds.attempts_per_card:
        return Decision(ACTION_BLOCK_FOR_USER, (
            f"{consecutive_unknown} runs in a row failed in a way ASES cannot classify. Should ASES retry the "
            "task, change it, or stop it?"
        ))
    return Decision(ACTION_NONE, (
        f"A failure ASES cannot classify ({consecutive_unknown} of {bounds.attempts_per_card} in a row): Hermes's "
        "own retry runs first, and ASES escalates only if it keeps happening."
    ))


# ---------------------------------------------------------------------------------------------
# The next model of a role class (ASES-REC-01, blueprint 11.1)
# ---------------------------------------------------------------------------------------------


def _smoke_failed(row: dict) -> bool:
    """A model row records a failed smoke test as smoke_test_result (the model_registry column) or smoke_test. The
    rows in config/models.yaml carry neither, and a missing result is acceptable."""
    return "fail" in (_word(row.get("smoke_test_result")), _word(row.get("smoke_test")))


def _unfit_for_agent_role(row: dict) -> bool:
    """A row that says outright it cannot serve an agent role: tool calling declared false, or a declared context
    under the 64K floor (ASES-MOD-02, ASES-MOD-04). A row that declares neither is not judged here, that is the
    doctor's job, so a minimal row still counts."""
    if row.get("tool_calling") is False:
        return True
    context = row.get("context_length")
    return isinstance(context, int) and not isinstance(context, bool) and context < models_mod.MINIMUM_CONTEXT_LENGTH


def next_model(
    models_config: dict, role_class: str, current_provider: str | None, current_model: str | None, *,
    unhealthy: set[tuple[str, str]] | None = None, data_class: str | None = None,
) -> tuple[str, str] | None:
    """The model to switch a card to after its second capability failure, as (provider, model), or None when the
    role class has no other usable candidate (ASES-REC-01: "on the second capability failure switch to the next
    model for that role class"; 11.1 "with kanban set-model").

    Candidates are the rows of models_config["models"] whose role_class is the pinned class or `<class>_candidate`
    (coder and coder_candidate). They are ordered pinned first, then in file order, and the first one that is not
    the current model and is still usable is returned. A row is skipped when its smoke test failed, when it says it
    cannot serve an agent role (tool calling false, declared context under 64K), when its provider is unhealthy,
    or, if `data_class` is given, when its provider's declared data policy does not clear that data class (the data
    class is enforced before any routing rule and never relaxed to keep work flowing: ASES-PRV-01).

    `unhealthy` holds (provider, model) pairs to skip; (provider, "*") skips every model of that provider, which is
    what a bad credential means (see unhealthy_credentials)."""
    unhealthy = unhealthy or set()
    providers = models_config.get("providers") or {}
    wanted = {role_class, f"{role_class}_candidate"}
    rows = [m for m in models_config.get("models") or [] if m.get("role_class") in wanted]
    ordered = [m for m in rows if m.get("pinned")] + [m for m in rows if not m.get("pinned")]
    for row in ordered:
        provider, model = row.get("provider"), row.get("model")
        if not provider or not model or (provider, model) == (current_provider, current_model):
            continue
        if (provider, model) in unhealthy or (provider, "*") in unhealthy:
            continue
        if _smoke_failed(row) or _unfit_for_agent_role(row):
            continue
        if data_class is not None:
            declared = row.get("data_policy") or (providers.get(provider) or {}).get("data_policy")
            # ASES-PRV-04 (round 7, package POLICY): check_data_class now requires an explicit, non-empty
            # verified_at for private/confidential, not just a compatible policy string -- found broken here by
            # POLICY itself (a file it does not own): without this, EVERY candidate was treated as a violation
            # for those two data classes, so next_model silently returned None instead of a real switch target.
            verified_at = row.get("data_policy_verified_at") or (providers.get(provider) or {}).get(
                "data_policy_verified_at")
            try:
                policy.check_data_class(data_class, provider, declared, verified_at=verified_at)
            except policy.DataPolicyViolation:
                continue
        return provider, model
    return None


def unhealthy_credentials(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    """The providers whose credential ASES has marked unhealthy, as {(provider, "*")}, ready to hand to
    next_model(unhealthy=...). Read from the event log: a `credential_unhealthy` event marks the provider, and a
    later `credential_restored` event for the same provider clears it (record one, with the provider in its
    payload, once the user has fixed the key). Without a way out, one 401 would exclude a provider for ever."""
    rows = conn.execute(
        "SELECT kind, payload FROM events WHERE kind IN ('credential_unhealthy', 'credential_restored') ORDER BY id"
    ).fetchall()
    marked: set[str] = set()
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (ValueError, RecursionError):
            continue
        provider = payload.get("provider") if isinstance(payload, dict) else None
        if not isinstance(provider, str) or not provider:
            continue
        if row["kind"] == "credential_unhealthy":
            marked.add(provider)
        else:
            marked.discard(provider)
    return {(provider, "*") for provider in marked}


# ---------------------------------------------------------------------------------------------
# The failure bundle (ASES-REC-01, ASES-REC-02)
# ---------------------------------------------------------------------------------------------

_DIFF_LIMIT = 6000
_GATE_LIMIT = 3000
_FINDINGS_LIMIT = 3000
_FAILED_LIMIT = 2000      # per field of the failed run: Hermes caps a run's error at 500, a summary is free text


def _scrub(text) -> str:
    """Text safe to put on a card or in a terminal: any secret-shaped value redacted with the same pattern every
    event payload goes through (events._SECRET_VALUE_PATTERN), and anything that is not ASCII escaped, since the
    Windows console is cp1252 and crashes on an arrow."""
    redacted = events.redact({"t": _text(text)})["t"]
    return redacted.encode("ascii", "backslashreplace").decode("ascii")


def _clip(text: str, limit: int) -> str:
    """Cut to `limit` characters and say so, so a reader knows the section is not the whole thing."""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n[truncated: {len(text) - limit} more characters omitted]"


def _latest_run(card: dict) -> tuple[int, dict | None]:
    """(index, run) of the last run in card["_runs"] that has an outcome, or (-1, None). Runs are in start order
    (Hermes lists them ORDER BY started_at, id), and a run with no outcome is still open."""
    runs = card.get("_runs") or []
    for index in range(len(runs) - 1, -1, -1):
        run = runs[index]
        if isinstance(run, dict) and _word(run.get("outcome")):
            return index, run
    return -1, None


def _last_failed_run(card: dict) -> dict | None:
    """The last run that failed, else the last run that ended at all, else None."""
    runs = card.get("_runs") or []
    for run in reversed(runs):
        if isinstance(run, dict) and _word(run.get("outcome")) and classify_run(run) is not FailureKind.NONE:
            return run
    return _latest_run(card)[1]


def failure_bundle(
    card: dict, *, criteria, diff_text: str = "", gate_output: str = "", reviewer_findings: str = "",
) -> str:
    """The text attached to the next attempt of a task (ASES-REC-01: "attach the failure bundle (criteria, diff,
    gate output, reviewer findings) to the card"; ASES-REC-02: a re-plan gets "the full failure bundle").

    Five sections, always all present ("(none provided)" when empty): Acceptance criteria (a list of strings or
    one string), What failed (the last failed run of `card`: its outcome, error and summary), Diff so far (cut to
    6000 characters), Gate output (3000) and Reviewer findings (3000), each cut with a visible marker. Every piece
    of text goes through the event log's secret redaction first, so a key that leaked into a diff or an error is
    never copied onto a card, and the whole bundle is ASCII."""
    if isinstance(criteria, str):
        criteria_text = _scrub(criteria).strip()
    else:
        criteria_text = "\n".join(f"- {_scrub(item).strip()}" for item in criteria or [])
    run = _last_failed_run(card)
    if run is None:
        failed = "No run of this card has ended yet."
    else:
        failed = "\n".join([
            f"Run: {_scrub(run.get('id'))} (profile {_scrub(run.get('profile'))})",
            f"Outcome: {_scrub(run.get('outcome'))}",
            f"Error: {_clip(_scrub(run.get('error')), _FAILED_LIMIT)}",
            f"Summary: {_clip(_scrub(run.get('summary')), _FAILED_LIMIT)}",
        ])
    sections = [
        ("Acceptance criteria", criteria_text),
        ("What failed", failed),
        ("Diff so far", _clip(_scrub(diff_text), _DIFF_LIMIT)),
        ("Gate output", _clip(_scrub(gate_output), _GATE_LIMIT)),
        ("Reviewer findings", _clip(_scrub(reviewer_findings), _FINDINGS_LIMIT)),
    ]
    parts = [f"Failure bundle for card {_scrub(card.get('id'))}"]
    for title, body in sections:
        parts.append(f"## {title}\n{body.strip() or '(none provided)'}")
    return "\n\n".join(parts) + "\n"


# ---------------------------------------------------------------------------------------------
# The loop step (ASES-REC-01, ASES-REC-02)
# ---------------------------------------------------------------------------------------------

# Which lineage counter a failure of each kind spends. Only these two are counted per failure: a rate limit, a
# quota park or an escalation to the user is not an attempt. A runtime overrun "counts as an attempt" (19.1).
_COUNTER_FOR_KIND = {
    FailureKind.INFRASTRUCTURE: "infra_failures",
    FailureKind.CAPABILITY: "capability_failures",
    FailureKind.RUNTIME: "capability_failures",
}


def _epoch(value) -> float | None:
    """Epoch seconds out of what Hermes and callers hand over: a number, a numeric string ("1789832491": Hermes's
    run timestamps arrive as strings on some paths), an ISO-8601 string or a datetime (naive means UTC). None when
    it is none of these, or not finite."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    if isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            number = float(text)
        except ValueError:
            try:
                return _epoch(datetime.fromisoformat(text.replace("Z", "+00:00")))
            except ValueError:
                return None
        return number if math.isfinite(number) else None
    return None


def _squash(value) -> str:
    """Whitespace collapsed, so one line of a failed run's error reads as one line."""
    return " ".join(_text(value).split())


def _already_decided(conn: sqlite3.Connection, project: str, task_key: str, card_id: str, run_id) -> bool:
    """True when a `recovery_decision` event already answers this exact failed run. This is what makes each
    failure count once, however many passes see the same blocked card."""
    return conn.execute(
        "SELECT 1 FROM events WHERE kind = 'recovery_decision' "
        "AND json_extract(payload, '$.project') = ? AND json_extract(payload, '$.task_key') = ? "
        "AND json_extract(payload, '$.card_id') = ? AND json_extract(payload, '$.run_id') = ? LIMIT 1",
        (project, task_key, card_id, run_id),
    ).fetchone() is not None


def _record_error_once(
    conn: sqlite3.Connection, project: str, task_key: str, card_id: str, action: str, exc: BaseException,
) -> None:
    """Record a failed hermes call as a `recovery_error` event, once per (task, card, action, message). A pass
    repeats every few seconds, and a card that keeps failing the same way would otherwise add the same event to the
    log on every pass (the same reasoning as controller._refuse_once)."""
    message = events.redact({"e": f"{type(exc).__name__}: {exc}"[:300]})["e"]
    seen = conn.execute(
        "SELECT 1 FROM events WHERE kind = 'recovery_error' AND json_extract(payload, '$.project') = ? "
        "AND json_extract(payload, '$.task_key') = ? AND json_extract(payload, '$.card_id') = ? "
        "AND json_extract(payload, '$.action') = ? AND json_extract(payload, '$.error') = ? LIMIT 1",
        (project, task_key, card_id, action, message),
    ).fetchone()
    if seen is None:
        events.record(conn, "recovery_error", {
            "project": project, "task_key": task_key, "card_id": card_id, "action": action, "error": message,
        })


@contextlib.contextmanager
def _atomic(conn: sqlite3.Connection):
    """One SAVEPOINT around a group of writes, so they land together or not at all. Unlike BEGIN a savepoint nests,
    so this is safe inside a transaction or savepoint the controller already holds."""
    conn.execute("SAVEPOINT recovery_record")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK TO recovery_record")
        conn.execute("RELEASE recovery_record")
        raise
    conn.execute("RELEASE recovery_record")


def _unknown_streak(card: dict) -> int:
    """How many of the card's most recent runs in a row were UNKNOWN. A rate-limited run is skipped, as Hermes does
    (a quota wall says nothing about the task); any other kind ends the streak."""
    streak = 0
    for run in reversed(card.get("_runs") or []):
        if not isinstance(run, dict) or not _word(run.get("outcome")):
            continue
        kind = classify_run(run)
        if kind is FailureKind.UNKNOWN:
            streak += 1
        elif kind is not FailureKind.RATE_LIMIT:
            break
    return streak


def _snippet(text, limit: int = 200) -> str:
    """One line of a failed run's error for a question, redacted, ASCII and short."""
    line = _squash(_scrub(text))
    return line if len(line) <= limit else line[:limit] + "..."


def _question(task_key: str, decision: Decision, run: dict) -> str:
    """The reason a card is blocked with: which task, what the last error said, then the question itself (it is
    last, so the reason reads as a question and ends in one)."""
    error = _snippet(run.get("error") or run.get("summary"))
    lead = f"{task_key}, last error: {error}. " if error else f"{task_key}: "
    return lead + decision.reason


def _provider_of(models_config: dict, model: str) -> str | None:
    for row in models_config.get("models") or []:
        if row.get("model") == model:
            return row.get("provider")
    return None


def _current_model(
    card: dict, run: dict, task: plan_mod.PlanTask, project: ases_config.ProjectConfig, models_config: dict,
) -> tuple[str | None, str | None]:
    """(provider, model) the failed run most likely ran on: the card's own override when one was set (a previous
    switch), else what the run's profile is pinned to, else what the task's role is pinned to. (None, None) when
    nothing says."""
    override = card.get("model_override")
    if isinstance(override, str) and override.strip():
        return card.get("provider_override") or _provider_of(models_config, override), override
    pinned = usage_mod.provider_for_profile(run.get("profile"), project.roles, models_config)
    if pinned is None:
        pinned = policy.profile_provider(task.role, models_config)
    return (pinned.provider, pinned.model) if pinned else (None, None)


def _project_replans(conn: sqlite3.Connection, project: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(replans), 0) AS total FROM lineage WHERE project = ?", (project,),
    ).fetchone()
    return int(row["total"])


def _call(conn, project, task_key, card_id, action, fn, *args, **kwargs) -> bool:
    """Make one hermes call. False, after recording a `recovery_error`, when it failed: the caller then does
    nothing more for this task this pass, and the next pass decides the same failure again."""
    try:
        fn(*args, **kwargs)
    except _HERMES_ERRORS as exc:
        _record_error_once(conn, project, task_key, card_id, action, exc)
        return False
    return True


def _switch_target(conn, project, task_key, card_id, run_id) -> tuple[str, str] | None:
    """The (provider, model) an earlier pass already chose to switch this failed run's card to, or None."""
    row = conn.execute(
        "SELECT payload FROM events WHERE kind = 'recovery_switch_target' "
        "AND json_extract(payload, '$.project') = ? AND json_extract(payload, '$.task_key') = ? "
        "AND json_extract(payload, '$.card_id') = ? AND json_extract(payload, '$.run_id') = ? "
        "ORDER BY id DESC LIMIT 1",
        (project, task_key, card_id, run_id),
    ).fetchone()
    if row is None:
        return None
    payload = _metadata(row["payload"])
    provider, model = payload.get("provider"), payload.get("model")
    return (provider, model) if isinstance(provider, str) and provider and isinstance(model, str) and model else None


def _adjust(
    decision: Decision, *, conn, plan, project, models_config, bounds, task, card, run, card_id, run_id,
) -> Decision:
    """Fill in what decide() cannot know: the model to switch to, the provider whose credential failed, and the
    project-wide re-plan cap."""
    if decision.action == ACTION_REPLAN and _project_replans(conn, plan.project) >= bounds.replans_per_project:
        return Decision(ACTION_BLOCK_FOR_USER, (
            f"This task's budget is spent, but the project has already used its {bounds.replans_per_project} "
            "re-plans (section 9.3: a user decision). Should ASES change the task itself, take a different "
            "approach, or stop it?"
        ))
    if decision.action not in (ACTION_MARK_CREDENTIAL_UNHEALTHY, ACTION_SWITCH_MODEL):
        return decision
    provider, model = _current_model(card, run, task, project, models_config)
    if decision.action == ACTION_MARK_CREDENTIAL_UNHEALTHY:
        return dataclasses.replace(decision, provider=provider, model=model)
    # A target an earlier pass chose for this failed run but did not get to record the decision for is reused, not
    # chosen again: by now the controller may have pinned it on a card, and "the next model after the current one"
    # would then flip straight back to the model that failed.
    target = _switch_target(conn, plan.project, task.key, card_id, run_id) or next_model(
        models_config, task.role, provider, model, unhealthy=unhealthy_credentials(conn),
        data_class=project.data_class,
    )
    if target is None:
        return Decision(ACTION_FRESH_ATTEMPT, (
            f"The attempt failed a second time, but no other usable model is configured for the "
            f"'{task.role}' role class, so start another fresh attempt on the same model."
        ))
    return dataclasses.replace(decision, provider=target[0], model=target[1], reason=(
        f"{decision.reason} Next model: {target[0]}/{target[1]}."
    ))


def _recover_task(
    board, plan, project, models_config, task, card_id, card, bounds, now_ts, conn,
) -> Decision | None:
    """process_failures for one task: None when there is nothing to do, or nothing was done this pass.

    Normally only acts on a `blocked` card. Round 7 (ASES-REC-01, bug 3) widens this for exactly two failure
    kinds: Hermes's own dispatcher respawn guard (kanban_db_dispatch.check_respawn_guard's `blocker_auth` rule)
    can hold a card whose last failure reads like an auth or quota wall in `ready` FOREVER instead of ever
    blocking it (confirmed against the installed 0.21.3 source: that rule has no expiry of its own), so such a
    card used to never reach this function at all, however many passes polled it -- this module's own docstring
    and process_failures's used to say so outright. AUTH and QUOTA (read classify_run's own table) are the ONLY
    two kinds widened past `blocked` here: anything else on a `ready` card (infrastructure, capability, a
    runtime overrun, ...) is Hermes's own ordinary retry in progress, and reacting to it here would be exactly
    the false positive this fix must avoid causing. A settle window since the failed run ended
    (READY_RESPAWN_SETTLE_SECONDS) keeps this from racing a run that only just finished, in case Hermes's own
    dispatcher has not even had one tick yet to apply blocker_auth. Once the widened gate lets a `ready` card
    through, it takes the exact same classification/decision/apply path below that a `blocked` card always has:
    there is nothing else here that needs to know which status let it in."""
    status = card.get("status")
    index, run = _latest_run(card)
    if run is None:
        return None
    kind = classify_run(run)
    if kind is FailureKind.NONE:
        return None       # includes a worker's deliberate question: the latest run's outcome is `blocked`
    if status != "blocked":
        if status != "ready" or kind not in (FailureKind.AUTH, FailureKind.QUOTA):
            return None
        ended = _epoch(run.get("ended_at"))
        if ended is None or now_ts < ended + READY_RESPAWN_SETTLE_SECONDS:
            return None   # no end time to judge by, or still inside the settle window: wait for a later pass
    run_id = run.get("id") if run.get("id") is not None else f"#{index}"
    if _already_decided(conn, plan.project, task.key, card_id, run_id):
        return None

    # Count this failure in memory, decide on the counted lineage, and write nothing until the decision is applied.
    lineage = load_lineage(conn, plan.project, task.key)
    counter = _COUNTER_FOR_KIND.get(kind)
    if counter is not None:
        lineage = dataclasses.replace(lineage, **{counter: getattr(lineage, counter) + 1})
    decision = decide(
        kind, lineage, bounds,
        consecutive_unknown=_unknown_streak(card) if kind is FailureKind.UNKNOWN else 0,
    )
    decision = _adjust(
        decision, conn=conn, plan=plan, project=project, models_config=models_config, bounds=bounds,
        task=task, card=card, run=run, card_id=card_id, run_id=run_id,
    )
    decision = dataclasses.replace(
        decision, task_key=task.key, card_id=card_id, run_id=run_id, failure_kind=kind,
    )

    def call(action: str, fn, *args, **kwargs) -> bool:
        return _call(conn, plan.project, task.key, card_id, action, fn, *args, **kwargs)

    applied = True
    action = decision.action
    if action == ACTION_RESUME:
        ended = _epoch(run.get("ended_at"))
        if ended is not None and now_ts < ended + decision.backoff_seconds:
            return None   # the backoff is still running: nothing this pass, the caller polls again
        if not call(action, hermes_mod.kanban_unblock, board, card_id):
            return None
    elif action == ACTION_PARK:
        if not call(action, hermes_mod.kanban_schedule, board, card_id, decision.reason):
            return None
    elif action in (ACTION_BLOCK_FOR_USER, ACTION_MARK_CREDENTIAL_UNHEALTHY):
        # ask_user, never kanban_block: this card is already `blocked`, which Hermes refuses to block again (after
        # leaving its comment), so the question is a comment that `swarm questions` finds. "already_asked" means the
        # same question is still open on the card: converged, the same as having asked it just now, and it writes
        # nothing. (The call is wrapped because `call` takes its own `conn`, which is not ask_user's.)
        question = _question(task.key, decision, run)
        if not call(action, lambda: questions_mod.ask_user(board, card, question, conn=conn)):
            return None
    elif action == ACTION_SWITCH_MODEL:
        # Not applied in place (ASES-REC-01, 19.2): the controller starts a fresh attempt on this model. What is
        # remembered is the choice, before anything else is written, so that a decision made again after a failed
        # write below takes the same target instead of choosing from a card whose model has moved since.
        if _switch_target(conn, plan.project, task.key, card_id, run_id) is None:
            events.record(conn, "recovery_switch_target", {
                "project": plan.project, "task_key": task.key, "card_id": card_id, "run_id": run_id,
                "provider": decision.provider, "model": decision.model,
            })
        applied = False
    else:
        applied = False   # none, fresh_attempt, switch_model, replan: nothing for this module to apply

    with _atomic(conn):
        if action == ACTION_MARK_CREDENTIAL_UNHEALTHY and decision.provider:
            events.record(conn, "credential_unhealthy", {
                "project": plan.project, "task_key": task.key, "card_id": card_id, "run_id": run_id,
                "provider": decision.provider, "model": decision.model,
            })
        events.record(conn, "recovery_decision", {
            "project": plan.project, "task_key": task.key, "card_id": card_id, "run_id": run_id,
            "kind": kind.value, "action": action, "reason": decision.reason,
            "backoff_seconds": decision.backoff_seconds, "model": decision.model, "provider": decision.provider,
            "applied": applied,
        })
        if counter is not None:
            bump(conn, plan.project, task.key, counter)
    return decision


def process_failures(
    board: str, plan: plan_mod.Plan, project: ases_config.ProjectConfig, models_config: dict, *,
    conn: sqlite3.Connection, now: float | int | datetime | None = None,
) -> list[Decision]:
    """The loop step of section 9.2 (`recovery.classify_and_act`): decide and apply the response to every plan task
    whose card Hermes gave up on (ASES-REC-01, ASES-REC-02). Returns the decisions made this pass, each carrying
    its task_key, card_id and run_id.

    For each plan task it reads the CURRENT work card (plan_tasks.work_card_id) and acts only when the card is
    `blocked` and the last run of it that has an outcome is a failure. A card blocked by a worker's deliberate
    question (that run's outcome is `blocked`) is left alone: unanswered questions never time out into guesses
    (ASES-REC-05). The failed run is classified, counted in memory (capability and runtime failures towards
    capability_failures, infrastructure failures towards infra_failures), decided on the counted lineage with the
    bounds of project.budgets, and applied through the hermes wrappers:
      resume                   kanban_unblock, but only once `now` (epoch seconds, default the clock) is past the
                               failed run's ended_at plus the backoff; before that nothing is done or recorded
      park                     kanban_schedule
      block_for_user           questions.ask_user with the question: a comment on the blocked card, and nothing at
                               all when the same question is still open ("already_asked")
      mark_credential_unhealthy  questions.ask_user with the question, and a `credential_unhealthy` event
      fresh_attempt,           returned, NOT applied: creating the replacement card (and, for switch_model, pinning
      switch_model, replan     `model` and `provider` on it) and asking the Lead are the controller's job (see
                               Decision). A capability failure restarts from a fresh worktree and the second one
                               also switches model (ASES-REC-01, 19.2), so a switch is never set in place on the
                               failed card
    Each decision is recorded once as a `recovery_decision` event (project, task_key, card_id, run_id, kind, action,
    reason, applied), and a run that already has one is skipped, so a failure is counted once however many passes
    see the card. The event and the counter bump are written in one savepoint AFTER the hermes calls succeed, so a
    hermes call that fails (non-zero exit, no hermes, a hang) leaves the counters untouched: it is recorded once as
    a `recovery_error` event (which is only for a failure ask_user could not recover from), the task is decided
    again on the next pass, and no other task is affected. `recovery_switch_target` remembers the model a switch
    chose, so that a decision that has to be made again does not choose again.

    Not covered here: a review-round or fix-card budget that is spent is not a failed run. After
    refresh_review_rounds(), call escalation(load_lineage(...), Bounds.from_budgets(...)) for that. A repeated
    block that sends a card to `triage` is not seen either. A card Hermes's dispatcher respawn guard holds in
    `ready` IS now seen (round 7, bug 3), but only for an auth- or quota-shaped last failure, and only once a
    settle window has passed since that run ended: see _recover_task's own docstring for exactly which two
    kinds and why the window exists."""
    now_ts = time.time() if now is None else _epoch(now)
    if now_ts is None:
        raise ValueError(f"now must be epoch seconds or a datetime, got {now!r}")
    bounds = bounds_mod.Bounds.from_budgets(project.budgets)
    decisions: list[Decision] = []
    for task in plan.tasks:
        row = conn.execute(
            "SELECT work_card_id FROM plan_tasks WHERE project = ? AND task_key = ?", (plan.project, task.key),
        ).fetchone()
        if row is None or not row["work_card_id"]:
            continue
        card_id = row["work_card_id"]
        try:
            card = hermes_mod.kanban_show(board, card_id)
        except _HERMES_ERRORS as exc:
            _record_error_once(conn, plan.project, task.key, card_id, "show", exc)
            continue
        decision = _recover_task(board, plan, project, models_config, task, card_id, card, bounds, now_ts, conn)
        if decision is not None:
            decisions.append(decision)
    return decisions
