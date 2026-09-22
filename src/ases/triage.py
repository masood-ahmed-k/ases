"""Agent-proposed cards land in triage and are validated before promotion (section 12.4; ASES-LED-03).

ASES-LED-03: "Agents may propose follow-up work during the project with the Hermes kanban_create tool. Such
cards MUST land in triage. The controller validates them like plan tasks, charges them to the lineage budget
of the task that raised them, and promotes or archives them."

What the real worker-side mechanism is (read from the installed Hermes 0.21.3 source, 2026-09-21, read-only,
never run): a dispatcher-spawned WORKER can call `kanban_create` directly, as an in-process structured tool
call, not only an orchestrator. `tools/kanban_tools.py` registers it under the "kanban" toolset with
`_check_kanban_mode` (`_visible(to_env_worker=True)`), and `kanban_create` is NOT in `_ORCHESTRATOR_TOOLS =
frozenset({"kanban_list", "kanban_unblock"})` (the two tools hidden from task workers), so a plain
dispatcher-spawned task worker sees and can call it. That answers Appendix C.2's "propose a follow-up card
instead of doing it": it is a literal `kanban_create` tool call the worker makes itself, not a comment or a
different tool name, and ASES does not have to create the card on the worker's behalf.

kanban_create does NOT default to landing in triage. `tools/kanban_tools_schemas.py`'s KANBAN_CREATE_SCHEMA
gives the tool an `initial_status` enum of only `["running", "blocked"]` (never "triage"), and separately a
plain `triage: bool` parameter ("If true, task lands in 'triage' instead of 'todo'"). The actual routing is
`hermes_cli/kanban_db_graph.py:initial_task_state` (read in full):

    if initial_status == "blocked": return "blocked", tenant
    if triage: return "triage", tenant
    if any(row["status"] != "done" for row in rows.values()): return "todo", tenant
    return "ready", tenant

`initial_status` is checked ONLY for the literal string "blocked"; there is no branch for "triage" at all, and
`hermes_cli/kanban_db.py:104` sets `VALID_INITIAL_STATUSES = {"running", "blocked"}`, so `initial_status="triage"`
would not even be a legal value to pass to `create_task`. The ONLY way a card lands in triage is the worker
explicitly passing `triage=true` on its own `kanban_create` call. This means "propose a follow-up card" is an
opt-in convention the worker's prompt must ask for (Appendix C.2, not this module's job), and this module needs
no `propose_card` helper: the worker creates the card itself, in triage, and this module only discovers,
validates and adjudicates what is already sitting there.

There is no triage-proposal store. A proposal IS a card on the Hermes board that is in Hermes's `triage` status
and carries no open question (questions.open_question returns None for it): a `triage` card that DOES carry a
`block_loop_detected` signal (or any other open-question signal) is a recovery state of an EXISTING card
(questions.py's job), not new work an agent proposed, and this module leaves it alone.

  list_triage_cards  the agent-proposed cards of one plan, oldest first
  validate           the lightest shape check a proposal must pass before a human/controller accepts it
  promote_card        hermes.kanban_promote, gated by validate unless force=True, then record_decision
  archive_card         hermes.kanban_archive, then record_decision
  record_decision      the triage_decision event, and the lineage charge to the task that raised the card
  format_triage         the plain-ASCII text `swarm triage` prints

The outside calls are the hermes module (list, show, promote, archive), questions.open_question (to tell a
proposal apart from a question), recovery.bump (the lineage charge) and the events table. Nothing here calls a
provider, and nothing here calls `hermes kanban specify` or `hermes kanban decompose` (r2_rules.md: "ASES never
runs specify/decompose on its own").
"""
from __future__ import annotations

import dataclasses
import enum
import json
import sqlite3
import time
from collections.abc import Sequence

from . import events
from . import hermes as hermes_mod
from . import plan as plan_mod
from . import questions as questions_mod
from . import recovery as recovery_mod

# The lightest a proposal's body may be and still be "long enough to act on" (validate() problem 5 of the
# package file). Deliberately low: a plain English proposal is valid as long as a human can read it, and this
# only catches an effectively blank body ("x", a single word), not a short-but-clear one.
MIN_BODY_LENGTH = 8


class TriageError(Exception):
    """A triage card could not be promoted or archived, with a message fit to show the person who asked for it."""


@dataclasses.dataclass(frozen=True)
class TriageCard:
    """One card an agent proposed, sitting in Hermes's triage lane, not yet validated.

    proposed_by is the card's `created_by` (the profile of the run whose kanban_create call made it), or None
    when Hermes did not report one. raised_by_task is the plan task whose card's run proposed this one, found
    the same ownership rule questions.py and leases.py use (a plan_tasks row's own id, a parent's id, or a
    title prefix, read via _plan_scope/_raised_by_task_key below): None when nothing in this plan claims it,
    which is expected and not an error, since a freshly proposed card need not yet carry either signal.
    created_at is epoch seconds, 0 when Hermes gave no time."""
    card_id: str
    title: str
    body: str
    proposed_by: str | None
    raised_by_task: str | None
    created_at: int


class Decision(enum.StrEnum):
    """What record_decision writes for one triage card: promote or archive."""
    PROMOTE = "promote"
    ARCHIVE = "archive"


@dataclasses.dataclass(frozen=True)
class ValidationResult:
    """What validate() found: ok is True exactly when problems is empty. Every problem is one ASCII sentence a
    human can read as-is (no traceback, no raised exception)."""
    ok: bool
    problems: tuple[str, ...]


# ---------------------------------------------------------------------------------------------
# Small helpers (ascii/epoch, matched to questions.py's style; kept local since they are questions.py's
# private helpers and this module does not own that file)
# ---------------------------------------------------------------------------------------------


def _ascii(text: str) -> str:
    """`text` made safe for a Windows console (cp1252): non-ASCII becomes a backslash escape, a control
    character other than tab/newline becomes visible x-escaped text. A card title and a worker's proposal are
    agent text, so neither may crash or steer the terminal."""
    out = []
    for char in text:
        code = ord(char)
        if char in "\t\n" or 32 <= code < 127:
            out.append(char)
        elif code < 128:
            out.append(f"\\x{code:02x}")
        else:
            out.append(char.encode("ascii", "backslashreplace").decode("ascii"))
    return "".join(out)


def _cell(value) -> str:
    """One display value on a single header line: whitespace runs collapse to one space, then ascii-escaped."""
    return _ascii(" ".join(str(value).split()))


def _epoch(value) -> int:
    """`value` as whole epoch seconds, 0 when missing or not a number (Hermes hands timestamps over as ints and,
    in places, numeric strings). An int is taken as it is: going through a float would round it once past 2**53."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError, OverflowError):
        return 0


def _age(created_at: int, now: float) -> str:
    """How long ago a card was proposed, as the words that follow "proposed": whole minutes under an hour,
    whole hours after that, "just now" under a minute, "at an unknown time" when Hermes gave no time."""
    if not created_at:
        return "at an unknown time"
    minutes = max(0, int(now - created_at)) // 60
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    return f"{minutes // 60} h ago"


# ---------------------------------------------------------------------------------------------
# list_triage_cards: what is proposed and unvalidated
# ---------------------------------------------------------------------------------------------


def _plan_scope(conn: sqlite3.Connection, project: str) -> tuple[dict[str, str], set[str], set[str]]:
    """(mine, foreign, keys) out of plan_tasks: the same view questions._card_index builds, scoped to
    `project`. mine maps a work or merge card id of this project's rows to its task key; foreign is every such
    id held by another project's rows (never even read further); keys is the task keys of this project's rows."""
    rows = conn.execute(
        "SELECT task_key, work_card_id, merge_card_id FROM plan_tasks WHERE project = ?", (project,),
    ).fetchall()
    other_rows = conn.execute(
        "SELECT work_card_id, merge_card_id FROM plan_tasks WHERE project != ?", (project,),
    ).fetchall()
    mine: dict[str, str] = {}
    for row in rows:
        for card_id in (row["work_card_id"], row["merge_card_id"]):
            if card_id:
                mine[card_id] = row["task_key"]
    foreign = {
        card_id for row in other_rows for card_id in (row["work_card_id"], row["merge_card_id"]) if card_id
    }
    return mine, foreign, {row["task_key"] for row in rows}


def _raised_by_task_key(card_id: str, title: str, parents: list[str], mine: dict[str, str], keys: set[str]) -> str | None:
    """The task key of THIS plan whose card's run proposed `card_id`: its own id, then a parent's id, then a
    title prefix (longest key wins when one key is a prefix of another), matching questions._owner's rule. None
    when nothing in this plan's plan_tasks claims it -- expected for a fresh proposal that names neither."""
    if card_id in mine:
        return mine[card_id]
    for parent in parents:
        if parent in mine:
            return mine[parent]
    matches = [key for key in keys if title.startswith(key + ":")]
    return max(matches, key=len) if matches else None


def _parent_ids(card: dict) -> list[str]:
    return [parent for parent in card.get("_parents") or [] if isinstance(parent, str) and parent]


def list_triage_cards(board: str, plan: plan_mod.Plan, *, conn: sqlite3.Connection) -> list[TriageCard]:
    """ASES-LED-03, section 12.4: the agent-proposed cards of this plan waiting for a human/controller decision,
    oldest first (created_at, then card id).

    A triage-status card counts as a proposal when questions.open_question finds NO open question on it: that
    excludes a card Hermes routed to triage through its own unblock-loop detection (a `block_loop_detected`
    signal) and any other open-question signal, which is a recovery state of an existing card that questions.py
    already owns, not new work an agent proposed (test 22.15's exact case: "A card proposed by an agent must
    stay in triage until it is validated").

    Ownership is resolved the same way _raised_by_task_key/_plan_scope work out (the ownership rule
    questions._owner uses): a card whose own id, or a parent's id, another project's plan_tasks rows claim is
    never given to this plan (NOT by a title-prefix match alone: cross-project task-key collisions are real).
    Unlike questions.list_questions, a card that matches NEITHER this plan NOR another project's rows is still
    listed here, with raised_by_task=None: a question is meaningless without a task to attach it to, but a
    proposal is still real, freshly-created work sitting on OUR board that section 12.4 says the controller must
    validate and promote or archive, and a fresh kanban_create call has no obligation to title itself with a
    task-key prefix or link parents=[self] (see the module docstring's account of the real mechanism); silently
    dropping it here would leave it in triage forever with nobody ever looking at it again.

    A card whose kanban_show fails (HermesCommandError) is skipped, and the rest are still listed: a
    `triage_read_failed` event records the card and Hermes's error, the same pattern list_questions uses for
    `question_read_failed`. A failure of the list itself propagates."""
    mine, foreign, keys = _plan_scope(conn, plan.project)
    keys = keys | {task.key for task in plan.tasks}
    found: list[TriageCard] = []
    for entry in hermes_mod.kanban_list(board, status="triage"):
        card_id = entry.get("id")
        if not card_id or (card_id in foreign):
            continue
        try:
            card = hermes_mod.kanban_show(board, card_id)
        except hermes_mod.HermesCommandError as exc:
            events.record(conn, "triage_read_failed", {
                "card_id": card_id, "error": f"{type(exc).__name__}: {exc}"[:300],
            })
            continue
        status = card.get("status", "triage")
        if status != "triage":
            continue  # answered/moved on (or promoted/archived already) between the list and the show
        if "status" not in card:
            card = {**card, "status": status}
        if questions_mod.open_question(card) is not None:
            continue  # a question (an unblock loop or otherwise), not a proposal: questions.py owns it
        parents = _parent_ids(card)
        if any(parent in foreign for parent in parents):
            continue  # another project's plan_tasks rows claim a parent of this card: not ours to touch
        title = card.get("title") or entry.get("title") or ""
        raised_by_task = _raised_by_task_key(card_id, title, parents, mine, keys)
        found.append(TriageCard(
            card_id=card_id, title=title, body=card.get("body") or "",
            proposed_by=card.get("created_by") or None, raised_by_task=raised_by_task,
            created_at=_epoch(card.get("created_at")),
        ))
    found.sort(key=lambda card: (card.created_at, card.card_id))
    return found


# ---------------------------------------------------------------------------------------------
# validate: the lightest shape check a proposal must pass
# ---------------------------------------------------------------------------------------------


def _structured_problems(body_text: str, plan: plan_mod.Plan | None, known_roles) -> list[str]:
    """Problems from a body that LOOKS like an attempted JSON object (starts with '{'): malformed JSON is
    reported (a broken machine-generated proposal should not silently read as prose), and when it parses as an
    object its role/gate_profile/touches/acceptance are checked the way Gate 0 (plan.parse_and_validate) checks
    a plan task, wherever `plan`/`known_roles` are given and the proposal names one. A body with no such hint,
    or JSON that is not an object, is plain English and reports nothing: this is deliberately lighter than Gate
    0, a proposal is something for a human to accept or reject, not itself a ready-to-run task."""
    if not body_text.startswith("{"):
        return []
    try:
        hint = json.loads(body_text)
    except (ValueError, RecursionError) as exc:
        return [f"the card body looks like structured JSON but does not parse: {exc}"]
    if not isinstance(hint, dict):
        return []
    problems: list[str] = []
    role = hint.get("role")
    if known_roles is not None and isinstance(role, str) and role and role not in known_roles:
        problems.append(f"the proposal names role '{role}', which is not one of the known roles: {sorted(known_roles)}")
    gate_profile = hint.get("gate_profile")
    if (plan is not None and isinstance(gate_profile, str) and gate_profile
            and gate_profile not in plan.gate_profiles):
        problems.append(
            f"the proposal names gate_profile '{gate_profile}', which is not declared in this plan's gate_profiles"
        )
    touches = hint.get("touches")
    if touches is not None and (not isinstance(touches, list) or not all(isinstance(g, str) for g in touches)):
        problems.append("the proposal's touches must be an array of path glob strings (ASES-TSK-03)")
    acceptance = hint.get("acceptance")
    if acceptance is not None and (not isinstance(acceptance, list) or not acceptance):
        problems.append("the proposal's acceptance must be a non-empty array (ASES-TSK-03)")
    return problems


def validate(
    board: str, card_id: str, *, conn: sqlite3.Connection, plan: plan_mod.Plan | None = None, known_roles=None,
) -> ValidationResult:
    """ASES-LED-03: "The controller validates them like plan tasks." At minimum: a non-empty title, and a body
    long enough to act on (not blank, at least MIN_BODY_LENGTH characters). When the body carries a structured
    hint (an attempted JSON object), its role/gate_profile/touches/acceptance are checked against the SAME shape
    Gate 0 (plan.parse_and_validate) requires of a plan task, using `plan`/`known_roles` when given -- a
    structured hint is never required, a plain English proposal a human can read is valid on its own, and
    `plan`/`known_roles` are optional so a caller with no plan context on hand still gets the core checks.

    Never raises on malformed card content (a missing title, a non-string body, unparseable JSON in it): every
    problem is one ASCII sentence, and ok is True exactly when there are none. A Hermes failure reading the card
    (HermesCommandError or another hermes-module error) is a different kind of failure and propagates, the same
    as questions.answer_question's single-card read."""
    card = hermes_mod.kanban_show(board, card_id)
    problems: list[str] = []
    title = card.get("title")
    if not isinstance(title, str) or not title.strip():
        problems.append("the card has no title")
    body = card.get("body")
    body_text = body.strip() if isinstance(body, str) else ""
    if not body_text:
        problems.append("the card body is blank: a proposal needs a description a human can act on")
    elif len(body_text) < MIN_BODY_LENGTH:
        problems.append(f"the card body is only {len(body_text)} character(s) long: too short to act on")
    else:
        problems.extend(_structured_problems(body_text, plan, known_roles))
    return ValidationResult(ok=not problems, problems=tuple(problems))


# ---------------------------------------------------------------------------------------------
# record_decision, promote_card, archive_card
# ---------------------------------------------------------------------------------------------

# recovery.bump's whitelist (_BUMP_FIELDS) is ("review_rounds", "capability_failures", "infra_failures",
# "replans"). "fix_cards" is NOT in it: fix_cards lives on plan_tasks and is bumped directly by
# mergeq.process_merge_queue, not through recovery.bump, so it is not actually reachable here even though a
# fix card is the closest EXISTING concept to "extra work this task spawned". None of the four bump-able
# fields is a semantically correct match for "a task's run proposed a follow-up card" either. infra_failures is
# chosen as the least-damaging stopgap: unlike the other three, it is explicitly excluded from
# recovery.exhausted()/escalation() ("Infrastructure failures are not in it: they say nothing about the task,
# and have their own cap in decide()"), so bumping it here can never falsely trigger a replan or eat into the
# project-wide replans_per_project budget (bumping "replans" would), and it never feeds
# recovery.next_model()'s switch-model choice (bumping "capability_failures" would corrupt the very next real
# capability failure's fresh_attempt/switch_model decision, and "review_rounds" would corrupt review-round
# exhaustion for a healthy task that simply proposed follow-up work). Its only side effect is nudging the
# backoff/threshold of this task's OWN next real infrastructure failure (recovery.decide()'s INFRASTRUCTURE
# branch), the narrowest blast radius on offer. See the report for why a new `proposed_cards` column on the
# lineage table (db.py, not owned by this package) is the correct fix for a later round.
_LINEAGE_CHARGE_FIELD = "infra_failures"


def record_decision(
    conn: sqlite3.Connection, project: str, card_id: str, decision: Decision, *,
    reason: str | None = None, raised_by_task: str | None = None,
) -> None:
    """ASES-LED-03: records the decision as a `triage_decision` event (card_id, decision, reason, raised_by_task;
    events.record redacts any secret-shaped value in `reason` before it is written), and, when raised_by_task is
    given, "charges [the card] to the lineage budget of the task that raised them" by bumping
    _LINEAGE_CHARGE_FIELD (see its comment for which field and why) through recovery.bump (a public function of
    a module this package does not own). A task with no raised_by_task given is never touched: an unattributable
    proposal (see list_triage_cards) has no task to charge."""
    decision = Decision(decision)
    events.record(conn, "triage_decision", {
        "card_id": card_id, "decision": str(decision), "reason": reason, "raised_by_task": raised_by_task,
    })
    if raised_by_task:
        recovery_mod.bump(conn, project, raised_by_task, _LINEAGE_CHARGE_FIELD)


def promote_card(
    board: str, card_id: str, *, conn: sqlite3.Connection, project: str, raised_by_task: str | None = None,
    reason: str | None = None, force: bool = False,
    plan: plan_mod.Plan | None = None, known_roles=None,
) -> None:
    """ASES-LED-03: promotes a validated proposal out of triage. Refuses with TriageError, before Hermes is
    touched, when validate() reports a problem, unless `force=True` (a human override: `swarm triage promote`
    is expected to offer it, the same shape as answer_question's own refusals). `plan`/`known_roles` are
    optional and forwarded to validate() for its structured-hint checks (see validate(); the package's signature
    for this function did not list them, so they default to None: a caller with a plan on hand can pass them for
    the fuller check, and `force=True` needs neither).

    hermes.kanban_promote(board, card_id, reason=reason) runs first, then record_decision(..., PROMOTE, ...): a
    promote that fails leaves nothing recorded, and a failure of either propagates."""
    if not force:
        result = validate(board, card_id, conn=conn, plan=plan, known_roles=known_roles)
        if not result.ok:
            raise TriageError(
                f"card {card_id} refused: " + "; ".join(result.problems)
                + " (a human can override this with force=True)"
            )
    hermes_mod.kanban_promote(board, card_id, reason=reason)
    record_decision(conn, project, card_id, Decision.PROMOTE, reason=reason, raised_by_task=raised_by_task)


def archive_card(
    board: str, card_id: str, *, conn: sqlite3.Connection, project: str, raised_by_task: str | None = None,
    reason: str | None = None,
) -> None:
    """ASES-LED-03: archives a rejected proposal (hermes.kanban_archive, soft: Hermes keeps the card), then
    record_decision(..., ARCHIVE, ...). No validation gate: rejecting a bad proposal must always be possible."""
    hermes_mod.kanban_archive(board, [card_id])
    record_decision(conn, project, card_id, Decision.ARCHIVE, reason=reason, raised_by_task=raised_by_task)


# ---------------------------------------------------------------------------------------------
# format_triage
# ---------------------------------------------------------------------------------------------


def format_triage(cards: Sequence[TriageCard], now: float | None = None) -> str:
    """The text `swarm triage` prints: ASCII only, same look as questions.format_questions. One numbered block
    per card, oldest first, blocks separated by a blank line: a header line (card id, which task raised it or
    "no task found", who proposed it when known, how long ago), the title after a dash, and the body indented
    four spaces underneath, one indented line per line of the body. "No proposed cards awaiting validation." when
    `cards` is empty."""
    if not cards:
        return "No proposed cards awaiting validation."
    current = time.time() if now is None else now
    blocks = []
    for number, card in enumerate(cards, start=1):
        task = f"raised by task {_cell(card.raised_by_task)}" if card.raised_by_task else "no task found"
        proposer = f", proposed by {_cell(card.proposed_by)}" if card.proposed_by else ""
        header = f"{number}. {_cell(card.card_id)} ({task}{proposer}, {_age(card.created_at, current)})"
        title = _cell(card.title)
        if title:
            header += f" - {title}"
        body_lines = [("    " + _ascii(line)).rstrip() for line in card.body.splitlines()]
        blocks.append("\n".join([header, *body_lines]))
    return "\n\n".join(blocks)
