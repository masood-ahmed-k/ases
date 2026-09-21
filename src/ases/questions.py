"""The human channel: swarm questions and swarm answer (section 19.5; ASES-REC-05, ASES-SEC-01).

Asking the user is a first-class state, not an exception. A worker, or the controller (section 19.1: a budget or
bound reached, a malformed plan or verdict, a merge that will not go through), stops its card and asks the
question, and this module is how a person finds those cards and answers them.

There is no question store. A question IS a card on the Hermes board that is `blocked` (or in Hermes's `triage`
lane after an unblock loop) and carries a signal nobody has answered yet. Answering it unblocks the card, so it
leaves the next listing by itself, and nothing here ever ages a question into a default answer (ASES-REC-05:
"Unanswered questions MUST NOT time out into guesses"). A card stays put until a person answers it.

Hermes 0.21.3 does not raise a question in one way only (read from kanban_db.py and kanban.py, 2026-09-21), so
open_question reads four signals:
  blocked       a `blocked` event whose payload has a reason: a worker's kanban_block, or ask_user on a card that was
                ready or running (the block is typed needs_input)
  gave_up       a `gave_up` event: the dispatcher's circuit breaker tripped after repeated failures. Hermes writes NO
                `blocked` event for it, so a lookup that only knew `blocked` never saw these cards
  block_loop    a `block_loop_detected` event: a second block of the same kind after an unblock sends the card to
                `triage`, where no `blocked` event is written either
  ases_comment  a comment by `ases` that starts "ASES QUESTION:": ask_user on a card Hermes will not block (block_task
                accepts only `running` and `ready`, so a merge card, created blocked, or a card in `todo` cannot be
                blocked, and the CLI fails AFTER it has added its comment)

  open_question    the newest unanswered signal of one card (pure: it only reads the card dict)
  ask_user         put a question to the person: block the card when Hermes allows it, else comment on it
  list_questions   the open questions of one plan, oldest first
  answer_question  add the answer as a card comment, then unblock the card
  format_questions the plain-ASCII text `swarm questions` prints

The outside calls are the hermes module (list, show, block, comment, unblock) and the events table. Nothing here
calls a provider.
"""
from __future__ import annotations

import dataclasses
import json
import sqlite3
import time
from collections.abc import Sequence

from . import events
from . import gates as gates_mod
from . import hermes as hermes_mod
from . import plan as plan_mod

# Every fix card is titled "<task key>: fix (round N)" (controller.process_merge_queue).
_FIX_MARKER = ": fix (round"

# The two lanes a question can sit in: `blocked`, and `triage`, where Hermes routes an unblock loop.
_QUESTION_STATUSES = ("blocked", "triage")

# The reason of the `blocked` event Hermes writes for a card that is CREATED blocked (kanban_db.create_task with
# initial_status="blocked", 0.21.3, read 2026-09-21): payload {"reason": "initial_status", "status": "blocked",
# "actor": ...}. The controller creates every merge card that way, to wait for its work card, and nobody asked
# anything, so that event is not a question. (Notes from an earlier probe say Hermes writes no event for it at all;
# both are handled, because the card then simply has no `blocked` event.)
CREATED_BLOCKED_REASON = "initial_status"

# The sources open_question can report, in the order they are counted and shown.
SOURCES = ("blocked", "gave_up", "block_loop", "ases_comment")

# What ask_user writes when Hermes will not block the card, and the only author open_question trusts for it: a
# comment that merely starts the same way but comes from a worker or a person is not ASES asking.
ASK_PREFIX = "ASES QUESTION:"
ASES_AUTHOR = "ases"

# A comment that starts like this says the question was dealt with: swarm answer writes ANSWER: before it unblocks,
# and the hermes CLI writes UNBLOCK: before it unblocks with a reason.
_ANSWER_PREFIXES = ("ANSWER:", "UNBLOCK:")

# The longest question ask_user posts. A question is agent-built text (a failed run's error, a merge conflict), and a
# card comment is not the place for a page of it.
QUESTION_LIMIT = 1500

# What ask_user returns.
ASKED_ALREADY = "already_asked"
ASKED_BLOCKED = "blocked"
ASKED_COMMENTED = "commented"

# How the header of `swarm questions` names a source. The default source, `blocked`, is not named.
_SOURCE_LABELS = {"gave_up": "gave up", "ases_comment": "asked by ASES", "block_loop": "unblock loop"}


class QuestionError(Exception):
    """An answer that cannot be delivered, with a message fit to show the person who typed it. It never carries
    the answer text: a refused answer may be exactly the one that held a secret."""


@dataclasses.dataclass(frozen=True)
class OpenQuestion:
    """What one card is asking right now (open_question). `reason` is the question text, redacted and escaped to
    ASCII, so it is safe to print and to put in a report; `asked_at` is the epoch seconds of the signal that raised
    it, 0 when Hermes gave no time; `source` is one of SOURCES."""
    reason: str
    asked_at: int
    source: str


@dataclasses.dataclass(frozen=True)
class Question:
    """One open question: a card and what it asks.

    card_kind is "work", "merge", "fix" or "other". task_key is None for a card that belongs to no plan task
    (only answer_question can return one: list_questions keeps the cards of one plan). asked_at is the epoch
    seconds of the signal that raised the question, 0 when Hermes gave no time. source is where the question came
    from (SOURCES): a worker's or ASES's block, a dispatcher that gave up, an unblock loop, or a comment ASES left
    on a card Hermes would not block."""
    card_id: str
    title: str
    task_key: str | None
    card_kind: str
    assignee: str | None
    question: str
    asked_at: int
    source: str = "blocked"


def _epoch(value) -> int:
    """`value` as whole epoch seconds, 0 when it is missing or not a number. Hermes hands timestamps over as ints
    and, in places, as numeric strings (a run's started_at is one), so both are read. An int is taken as it is: going
    through a float would round it once it is past 2**53, and two different times would compare equal."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError, OverflowError):
        return 0


def _ascii(text: str) -> str:
    """`text` made safe to print on a Windows console, which is cp1252 and crashes on an arrow, and safe to
    print at all: a non-ASCII character becomes a backslash escape, and a control character other than a tab
    or a newline (an ESC that starts a terminal sequence, a bell, a carriage return that overwrites the line)
    becomes visible x-escaped text. A card title and a worker's question are agent text, so neither may
    crash or steer the terminal."""
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


def _clean(text: str) -> str:
    """Question text as it is shown to a person: any secret-shaped value redacted (ASES-SEC-01), then escaped to
    ASCII. Doing it twice changes nothing, so text that has been through it once can safely go through again."""
    return _ascii(events.redact_text(text))


def _same(first: str, second: str) -> bool:
    """Whether two question texts are the same question. Compared after the same cleaning a listing applies and with
    runs of whitespace collapsed, so a question compares equal to itself as Hermes stored it."""
    return " ".join(_clean(first).split()) == " ".join(_clean(second).split())


# ---------------------------------------------------------------------------------------------
# open_question: what one card is asking
# ---------------------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _Signal:
    """One thing on a card that reads as a question: where it sits in time (`key`), when (`at`, epoch seconds), which
    of the SOURCES it is and the cleaned reason."""
    key: tuple[int, int, int]
    at: int
    source: str
    reason: str


def _key(epoch: int, comment: bool, position: int) -> tuple[int, int, int]:
    """Where an event or a comment sits in time, as something that compares. Events and comments come from two
    lists, so the ties are broken like this: at the same second a comment counts as newer than an event (an answer
    is written after the block it answers, and ASES's own comment after the failure it asks about), and within one
    list the later position is newer (Hermes lists both oldest first)."""
    return (epoch, 1 if comment else 0, position)


def _records(card: dict, name: str) -> list[tuple[int, dict]]:
    """(position, record) for the dict entries of card[name], skipping anything else (Hermes lists are trusted no
    further than that). The position is the index in the list, junk included, so it still orders the rest."""
    items = card.get(name)
    if not isinstance(items, list):
        return []
    return [(position, item) for position, item in enumerate(items) if isinstance(item, dict)]


def _payload(event: dict) -> dict:
    """An event's payload as a dict. Hermes hands it over as a dict or as a JSON string, and an event with nothing
    to say (an `unblocked` for a plain resume) has none."""
    payload = event.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, RecursionError):
            return {}
    return payload if isinstance(payload, dict) else {}


def _reason(value) -> str:
    """A question text out of a payload field or a comment body: stripped and cleaned, "" when it is not text or
    holds nothing but whitespace (a block with no reason asks nothing)."""
    if not isinstance(value, str) or not value.strip():
        return ""
    return _clean(value.strip())


def _failures(value) -> int | None:
    """The failure count of a `gave_up` payload, None when it is not a plain number."""
    if isinstance(value, bool):
        return None
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError, OverflowError):
        return None


def _gave_up_reason(payload: dict) -> str:
    """The question a `gave_up` event stands for. Hermes writes failures, effective_limit, error and trigger_outcome;
    the person needs the count and the last error: "gave up after 3 failure(s): <error>"."""
    count = _failures(payload.get("failures"))
    head = f"gave up after {count} failure(s)" if count is not None else "gave up after repeated failures"
    error = _reason(payload.get("error"))
    return f"{head}: {error}" if error else head


def _is_creation_block(event: dict) -> bool:
    """Whether a `blocked` event only records that the card was created blocked (CREATED_BLOCKED_REASON)."""
    reason = _payload(event).get("reason")
    return isinstance(reason, str) and reason.strip() == CREATED_BLOCKED_REASON


def _newest_event(
    records: list[tuple[int, dict]], kind: str, *, skip=None,
) -> tuple[tuple[int, int, int], dict] | None:
    """(key, event) of the newest event of `kind` that `skip` (a predicate on the event) does not exclude, None when
    there is none."""
    best = None
    for position, event in records:
        if event.get("kind") != kind or (skip is not None and skip(event)):
            continue
        key = _key(_epoch(event.get("created_at")), False, position)
        if best is None or key > best[0]:
            best = (key, event)
    return best


def _signals(card: dict) -> list[_Signal]:
    """Every signal on the card, answered or not (open_question decides which still count).

    Only the NEWEST event of each kind is looked at. For `blocked` that is what keeps an old, long-answered reason
    from coming back when the block that holds the card now carries none: an empty reason on the newest `blocked`
    event means the card asks nothing through that event, and reaching back to an older reason would show a question
    that was answered long ago. A `blocked` or `block_loop_detected` event counts only with a non-empty reason; a
    `gave_up` event always counts, because the count and the error are the question. The `blocked` event of a card
    created blocked (CREATED_BLOCKED_REASON) is not a signal at all: a merge card would otherwise read as a question
    from the moment it exists."""
    records = _records(card, "_events")
    found: list[_Signal] = []

    newest = _newest_event(records, "blocked", skip=_is_creation_block)
    if newest is not None:
        reason = _reason(_payload(newest[1]).get("reason"))
        if reason:
            found.append(_Signal(newest[0], newest[0][0], "blocked", reason))

    newest = _newest_event(records, "gave_up")
    if newest is not None:
        found.append(_Signal(newest[0], newest[0][0], "gave_up", _gave_up_reason(_payload(newest[1]))))

    newest = _newest_event(records, "block_loop_detected")
    if newest is not None:
        reason = _reason(_payload(newest[1]).get("reason"))
        if reason:
            found.append(_Signal(newest[0], newest[0][0], "block_loop", reason))

    latest = None
    for position, comment in _records(card, "_comments"):
        body = comment.get("body")
        if comment.get("author") != ASES_AUTHOR or not isinstance(body, str):
            continue
        text = body.lstrip()
        if not text.startswith(ASK_PREFIX):
            continue
        reason = _reason(text[len(ASK_PREFIX):])
        key = _key(_epoch(comment.get("created_at")), True, position)
        if reason and (latest is None or key > latest[0]):
            latest = (key, reason)
    if latest is not None:
        found.append(_Signal(latest[0], latest[0][0], "ases_comment", latest[1]))
    return found


def _answered_at(card: dict) -> tuple[int, int, int] | None:
    """Where the newest sign of an answer sits, None when the card has none: an `unblocked` event, or a comment that
    starts ANSWER: or UNBLOCK:. A signal older than this was answered."""
    latest = None
    for position, event in _records(card, "_events"):
        if event.get("kind") == "unblocked":
            key = _key(_epoch(event.get("created_at")), False, position)
            if latest is None or key > latest:
                latest = key
    for position, comment in _records(card, "_comments"):
        body = comment.get("body")
        if isinstance(body, str) and body.lstrip().startswith(_ANSWER_PREFIXES):
            key = _key(_epoch(comment.get("created_at")), True, position)
            if latest is None or key > latest:
                latest = key
    return latest


def open_question(card: dict) -> OpenQuestion | None:
    """ASES-REC-05: what a card is asking a person right now, or None. `card` is a hermes.kanban_show dict.

    None unless the card is `blocked` or in `triage`. Then the signals of _signals are compared and the NEWEST one
    wins (by created_at; at the same second a comment counts as newer than an event, and a later list position as
    newer than an earlier one), so a card that carries both a `gave_up` event and a `blocked` event shows the
    reason of whichever happened last. A signal older than the newest `unblocked` event, or than the newest comment
    that starts ANSWER: or UNBLOCK:, does not count: it was answered, and the card only looks blocked again because
    something newer blocked it without saying why (a merge card is created blocked and is not asking anything).

    This is the one rule for whether a card asks a question, so `swarm questions`, `swarm answer`, the recovery
    loop's "already asked" test and the report cannot drift apart. Pure: the dict is only read."""
    if not isinstance(card, dict) or card.get("status") not in _QUESTION_STATUSES:
        return None
    signals = _signals(card)
    if not signals:
        return None
    newest = max(signals, key=lambda signal: signal.key)
    answered = _answered_at(card)
    if answered is not None and newest.key < answered:
        return None
    return OpenQuestion(reason=newest.reason, asked_at=newest.at, source=newest.source)


# ---------------------------------------------------------------------------------------------
# ask_user: put a question to the person
# ---------------------------------------------------------------------------------------------


def _prepare(text) -> str:
    """The text ask_user posts: stripped, any secret-shaped value redacted (ASES-SEC-01: nothing secret-shaped in a
    card body or comment), a NUL dropped (it cannot travel in a command line) and cut to QUESTION_LIMIT characters. The
    cut comes after the redaction, so no half of a secret is left behind. An empty question is a caller's bug: a card
    blocked with no reason would sit in the lane asking nothing, which is the one thing this module exists to stop."""
    clean = events.redact_text(text.replace("\x00", "") if isinstance(text, str) else str(text or "")).strip()
    if not clean:
        raise ValueError("the question is empty: a card blocked with no reason asks nobody anything")
    return clean[:QUESTION_LIMIT]


def ask_user(
    board: str, card: dict, text: str, *, conn: sqlite3.Connection | None = None, author: str = ASES_AUTHOR,
) -> str:
    """ASES-REC-05: "A worker or the controller blocks the card with the question as the reason." Puts `text` to the
    person as a question on `card` (a hermes.kanban_show dict) and returns how it went:

      "already_asked"  an identical question is already open on the card (open_question): nothing is posted and no
                       event is written, so a pass that repeats every few seconds neither piles up comments nor
                       spams the log
      "blocked"        the card was `ready` or `running`, and `hermes kanban block --kind needs_input` took it: the
                       reason is the question, and it is the kind Hermes reserves for "a question for a human"
      "commented"      the question is a comment "ASES QUESTION: <text>" by `author`. Hermes's block_task accepts only
                       `running` and `ready`, so a card that is `blocked` (every merge card is created blocked), in
                       `triage`, `todo` or `scheduled` cannot be blocked: the CLI would add its "BLOCKED:" comment and
                       then exit 1. A `ready` or `running` card whose block Hermes refuses (it moved since `card` was
                       read) falls back to the same comment, so an accepted question is never lost to a race. Only
                       on a `blocked` or `triage` card is that comment a question anyone lists (open_question needs
                       one of the two): on `todo`, `scheduled`, `review` or `done` it is posted, but nothing holds
                       the card and `swarm questions` cannot see it, so do not rely on it there

    A second question of the same kind on a card that was unblocked once already sends it to `triage`, with a
    `block_loop_detected` event, and `hermes kanban block` still exits 0: the answer is still "blocked", and
    open_question finds it there. Nothing here unblocks anything, and nothing ever times a question out into a guess.

    `text` is redacted with events.redact_text and cut to QUESTION_LIMIT characters before anything is posted; a
    blank one raises ValueError. When `conn` is given a `question_asked` event records the card, how it was asked and
    the length, with the first 300 characters of the question. Only the default author `ases` is recognised by
    open_question, so a question posted under another name is one `swarm questions` cannot see and one ask_user
    would post again on the next pass: leave `author` alone unless you mean exactly that.

    A Hermes error from the block that is not a refusal (no hermes on PATH, a timeout) and any error from the
    comment propagate: the question was not asked, and the caller decides what a failed ask means."""
    card_id = card.get("id") if isinstance(card, dict) else None
    if not card_id:
        raise ValueError("card must be a hermes.kanban_show dict with an id")
    clean = _prepare(text)
    already = open_question(card)
    if already is not None and _same(already.reason, clean):
        return ASKED_ALREADY

    status = card.get("status")
    how = None
    if status in ("ready", "running"):
        try:
            hermes_mod.kanban_block(board, card_id, clean, kind="needs_input")
            how = ASKED_BLOCKED
        except hermes_mod.HermesCommandError:
            how = None     # refused: fall back to the comment below
    if how is None:
        hermes_mod.kanban_comment(board, card_id, f"{ASK_PREFIX} {clean}", author=author)
        how = ASKED_COMMENTED
    if conn is not None:
        events.record(conn, "question_asked", {
            "card_id": card_id, "via": how, "status": status, "chars": len(clean), "question": clean[:300],
        })
    return how


# ---------------------------------------------------------------------------------------------
# list_questions and answer_question
# ---------------------------------------------------------------------------------------------


def _parent_ids(card: dict) -> list[str]:
    """The ids of the card's parents, as kanban_show lists them (plain id strings)."""
    return [parent for parent in card.get("_parents") or [] if isinstance(parent, str) and parent]


def _card_index(
    conn: sqlite3.Connection, project: str | None,
) -> tuple[dict[str, tuple[str, str]], set[str], set[str]]:
    """The plan_tasks view the ownership rules need: (cards, foreign, keys).

    `cards` maps a card id to (task key, "work" or "merge") for the rows of `project`, or for every row when
    project is None (answering a card has no plan to scope by). `foreign` is the set of card ids held by the
    rows of OTHER projects. `keys` is the task keys of the rows in `cards`. The table is what is read, not the
    Plan object: process_merge_queue repoints work_card_id at each fix card, so only the row knows which card is
    the task's card now."""
    if project is None:
        rows = conn.execute("SELECT task_key, work_card_id, merge_card_id FROM plan_tasks").fetchall()
        other_rows: list = []
    else:
        rows = conn.execute(
            "SELECT task_key, work_card_id, merge_card_id FROM plan_tasks WHERE project = ?", (project,)
        ).fetchall()
        other_rows = conn.execute(
            "SELECT work_card_id, merge_card_id FROM plan_tasks WHERE project != ?", (project,)
        ).fetchall()
    cards: dict[str, tuple[str, str]] = {}
    for row in rows:
        for card_id, kind in ((row["work_card_id"], "work"), (row["merge_card_id"], "merge")):
            if card_id:
                cards[card_id] = (row["task_key"], kind)
    foreign = {
        card_id for row in other_rows for card_id in (row["work_card_id"], row["merge_card_id"]) if card_id
    }
    return cards, foreign, {task_key for task_key, _ in cards.values()}


def _owner(
    card_id: str, title: str, parents: list[str],
    cards: dict[str, tuple[str, str]], foreign: set[str], keys: set[str],
) -> tuple[str, str | None] | None:
    """(task key, kind by id) when the card belongs to the plan the index was built for, else None. The kind by
    id is "work" or "merge" when the card is a plan_tasks work or merge card, and None for a card found another
    way.

    In order: the card IS a work or merge card of the plan; one of its parents is one (a fix card hangs under the
    card it replaces, so a fix card not yet recorded in plan_tasks is still found); its title starts with
    "<task key>:" for a key of the plan (the title of a work card and of a fix card, and the longest key wins
    when one key is a prefix of another). A card claimed by another project's rows, by its own id or by a
    parent, is never given to the plan by the title rule: task keys collide across projects (two projects on
    one board both have a T1), so a bare key match would list a neighbour's card."""
    if card_id in cards:
        return cards[card_id]
    if card_id in foreign:
        return None
    for parent in parents:
        if parent in cards:
            return cards[parent][0], None
    if any(parent in foreign for parent in parents):
        return None
    matches = [key for key in keys if title.startswith(key + ":")]
    if matches:
        return max(matches, key=len), None
    return None


def _kind(kind_by_id: str | None, title: str) -> str:
    """The card kind shown to the user. A merge card is "merge". A card titled as a fix card is "fix" BEFORE the
    work-card check, because creating a fix card repoints plan_tasks.work_card_id at it (controller.
    process_merge_queue), so the live fix card IS the row's work card and would otherwise read "work"."""
    if kind_by_id == "merge":
        return "merge"
    if _FIX_MARKER in title:
        return "fix"
    if kind_by_id == "work":
        return "work"
    return "other"


def list_questions(board: str, plan: plan_mod.Plan, *, conn: sqlite3.Connection) -> list[Question]:
    """ASES-REC-05: "swarm questions lists open questions with their cards". Every blocked card, and every card in
    Hermes's triage lane, of this plan that carries a question nobody has answered yet, oldest first (asked_at, then
    card id).

    A question is whatever open_question finds on a card that is `blocked` or in `triage`: a `blocked` event with a
    reason (a worker, or ask_user, blocked it), a `gave_up` event (the dispatcher's circuit breaker tripped: no
    `blocked` event is written for that), a `block_loop_detected` event with a reason (a repeated block sent the card
    to `triage`) or an "ASES QUESTION:" comment (ask_user on a card Hermes will not block). The controller creates
    every merge card blocked, waiting for its work card, and that must not read as a question: Hermes records the
    creation as a `blocked` event with the reason "initial_status", which open_question ignores, so the card has no
    signal at all. A triage card with no signal is an agent-proposed card waiting for validation, not a question, and
    is not listed. Once a question is answered it leaves this list by itself: the card is unblocked, or carries the
    answer.

    A card belongs to the plan by the rules in _owner: its id or a parent's id is a work or merge card of a row
    in plan_tasks for plan.project, or its title starts with a task key of the plan and no other project claims
    it. Other projects' cards on the same board are left out (and are not even read).

    A card whose kanban_show fails (HermesCommandError: archived, or gone between the list and the show) is
    skipped and the rest are still listed, because one unreadable card must not hide every other question. The
    skip is not silent: a question_read_failed event records the card and Hermes's error. Any other failure,
    and a failure of either list, propagates: a Hermes that cannot answer at all must not look like an
    empty inbox."""
    cards, foreign, keys = _card_index(conn, plan.project)
    keys = keys | {task.key for task in plan.tasks}
    listed = [
        (lane, entry) for lane in _QUESTION_STATUSES for entry in hermes_mod.kanban_list(board, status=lane)
    ]
    found: list[Question] = []
    seen: set[str] = set()
    for lane, entry in listed:
        card_id = entry.get("id")
        if not card_id or card_id in seen or (card_id in foreign and card_id not in cards):
            continue
        seen.add(card_id)   # a card the list reports under both lanes (it moved meanwhile) is read once
        try:
            card = hermes_mod.kanban_show(board, card_id)
        except hermes_mod.HermesCommandError as exc:
            events.record(conn, "question_read_failed", {
                "card_id": card_id, "error": f"{type(exc).__name__}: {exc}"[:300],
            })
            continue
        status = card.get("status", lane)
        if status not in _QUESTION_STATUSES:
            continue  # answered (or otherwise moved on) between the list and the show
        if "status" not in card:
            card = {**card, "status": status}   # a show that names no status is trusted to be in its listed lane
        title = card.get("title") or entry.get("title") or ""
        owner = _owner(card_id, title, _parent_ids(card), cards, foreign, keys)
        asked = open_question(card)
        if owner is None or asked is None:
            continue
        task_key, kind_by_id = owner
        found.append(Question(
            card_id=card_id, title=title, task_key=task_key, card_kind=_kind(kind_by_id, title),
            assignee=card.get("assignee") or entry.get("assignee") or None,
            question=asked.reason, asked_at=asked.asked_at, source=asked.source,
        ))
    found.sort(key=lambda question: (question.asked_at, question.card_id))
    return found


def _secret_lines(text: str) -> list[int]:
    """The 1-based numbers of the lines of `text` that look like they hold a secret (ASES-SEC-01: scan before a
    card is written).

    gates.scan_for_secrets is a DIFF scanner (found 2026-09-19 while building this module): it reads only lines
    that start with "+", so raw text always scans clean, and each finding echoes up to 80 characters of the
    offending line, so a finding in an error message would print the secret it caught. Each line is therefore
    handed over as an added diff line, one call per line, and only the line NUMBER is kept."""
    return [
        number for number, line in enumerate(text.splitlines(), start=1)
        if gates_mod.scan_for_secrets("+" + line)
    ]


def _no_question(card_id: str, card: dict, status) -> str:
    """The refusal for a card that has no open question, saying why in the words the person can act on."""
    where = _ascii(card_id)
    if _signals(card):
        if status == "triage":
            why = (f"its question was already answered, but the card is still in triage: only `hermes kanban specify "
                   f"{where}` moves it on")
        else:
            why = (f"its question was already answered, but the card is still blocked: release it with "
                   f"`hermes kanban unblock {where}`")
    elif status == "triage":
        why = "it is in triage, but no unblock loop or question was recorded"
    else:
        why = "it is blocked, but no block reason was recorded"
    return f"card {where} has no open question ({why})"


def answer_question(
    board: str, card_id: str, text: str, *, conn: sqlite3.Connection, author: str = "user",
) -> Question:
    """ASES-REC-05: "swarm answer <card> "<text>" adds the answer as a card comment and unblocks it." Returns the
    Question that was answered (its reason is the one the answer replies to, and `source` says where it came from).

    The answer is refused, before Hermes is touched, when it is empty or blank, and when it looks like a secret
    (ASES-SEC-01: "scan card bodies ... before they are written"; the error names the line numbers, never the
    secret). The card is then read: it must be `blocked` or in `triage` and carry an open question (open_question),
    else there is no open question ("card X has no open question") and nothing is written. This is not scoped to a
    plan, so any card of the board can be answered by its id.

    The comment is posted FIRST ("ANSWER: <text>", by `author`) and the unblock second. In that order a failure
    can only leave a card that is still blocked with its answer already on it, which is safe. The other order could
    wake a worker with no answer to read, which is a guess in all but name (ASES-REC-05). A failed comment therefore
    never unblocks, and a failed unblock after a posted comment is not retried: HermesCommandError propagates from
    either, and so does one from the initial read, because a Hermes that failed is not the same fact as "no open
    question". The posted answer also marks the question answered, so a second `swarm answer` for that card says so
    and names `hermes kanban unblock` for the card that is still blocked.

    The same steps serve a `blocked` card whatever raised its question: a worker, a dispatcher that gave up (the
    unblock resets Hermes's failure counter, which is what lets the card try again), or ASES's own comment.

    A card in `triage` (an unblock loop) cannot be unblocked at all: Hermes lets a triage card leave that lane only
    through `hermes kanban specify <id>`, an auxiliary model call that ASES never makes on its own, or through a
    re-plan. The answer is still posted as a comment first, because it must not be lost, and then QuestionError says
    what is left to do. Nothing else is called.

    The question_answered event carries the card, the task key and the length of the answer, never the answer."""
    if not isinstance(text, str) or not text.strip():
        raise QuestionError("the answer is empty; nothing was posted")
    lines = _secret_lines(text)
    if lines:
        where = f"line {lines[0]}" if len(lines) == 1 else "lines " + ", ".join(str(n) for n in lines)
        raise QuestionError(
            f"answer refused: possible secret on {where} of the answer (ASES-SEC-01: a secret never goes into "
            "a card). Nothing was posted. Name where the value lives (an environment variable, a file path) "
            "instead of the value, and answer again."
        )

    card = hermes_mod.kanban_show(board, card_id)
    status = card.get("status")
    if status not in _QUESTION_STATUSES:
        raise QuestionError(
            f"card {_ascii(card_id)} has no open question (its status is {_ascii(str(status))}, not blocked)"
        )
    asked = open_question(card)
    if asked is None:
        raise QuestionError(_no_question(card_id, card, status))

    cards, foreign, keys = _card_index(conn, None)
    title = card.get("title") or ""
    owner = _owner(card_id, title, _parent_ids(card), cards, foreign, keys)
    task_key, kind_by_id = owner if owner is not None else (None, None)
    question = Question(
        card_id=card_id, title=title, task_key=task_key, card_kind=_kind(kind_by_id, title),
        assignee=card.get("assignee") or None, question=asked.reason, asked_at=asked.asked_at, source=asked.source,
    )

    hermes_mod.kanban_comment(board, card_id, "ANSWER: " + text.strip(), author=author)
    if status == "triage":
        loop = " (an unblock loop)" if asked.source == "block_loop" else ""
        raise QuestionError(
            f"card {_ascii(card_id)} is in Hermes's triage lane{loop}, so it cannot be unblocked. Your "
            f"answer was posted on the card as a comment and is not lost. A triage card can only leave that lane "
            f"through `hermes kanban specify {_ascii(card_id)}` (an auxiliary model call that ASES does not run on "
            "its own) or by re-planning the task."
        )
    hermes_mod.kanban_unblock(board, card_id, reason="answered by " + author)
    events.record(conn, "question_answered", {"card_id": card_id, "task_key": task_key, "chars": len(text)})
    return question


# ---------------------------------------------------------------------------------------------
# format_questions
# ---------------------------------------------------------------------------------------------


def _cell(value) -> str:
    """One display value on a single header line: runs of whitespace (line breaks included) collapse to one
    space, then the text is made ASCII."""
    return _ascii(" ".join(str(value).split()))


def _age(asked_at: int, now: float) -> str:
    """How long ago a question was asked, as the words that follow "asked": whole minutes under an hour, whole
    hours after that, "just now" under a minute, and "at an unknown time" when Hermes gave no time (asked_at 0),
    not an age counted from 1970. A clock that is behind the event reads as just now, never as a negative age."""
    if not asked_at:
        return "at an unknown time"
    minutes = max(0, int(now - asked_at)) // 60
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    return f"{minutes // 60} h ago"


def format_questions(questions: Sequence[Question], now: float | None = None) -> str:
    """The text `swarm questions` prints: ASCII only, one block per question, blocks separated by a blank line.

    A block is a numbered header line, the card id, task key, card kind, where the question came from when it is not
    the plain default ("gave up", "asked by ASES", "unblock loop") and how long ago it was asked (against `now`,
    epoch seconds, default the current time; a parameter so a test can fix it), then the card title, and under it
    the question text indented four spaces, one indented line per line of the question. With no questions the whole
    output is the single line "No open questions.".

    Everything printed goes through _ascii (backslash escapes for non-ASCII, visible escapes for control
    characters), because the title and the question are agent text and the Windows console is cp1252. Line
    breaks in the question are honoured, but a line break in the id, kind or title cannot break the header."""
    if not questions:
        return "No open questions."
    current = time.time() if now is None else now
    blocks = []
    for number, question in enumerate(questions, start=1):
        task = f"task {_cell(question.task_key)}" if question.task_key else "no task"
        label = "" if question.source in ("", "blocked") else _SOURCE_LABELS.get(question.source, question.source)
        where = f", {_cell(label)}" if label else ""
        header = (
            f"{number}. {_cell(question.card_id)} "
            f"({task}, {_cell(question.card_kind)}{where}, asked {_age(question.asked_at, current)})"
        )
        title = _cell(question.title)
        if title:
            header += f" - {title}"
        body = [("    " + _ascii(line)).rstrip() for line in question.question.splitlines()]
        blocks.append("\n".join([header, *body]))
    return "\n\n".join(blocks)
