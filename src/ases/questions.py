"""The human channel: swarm questions and swarm answer (section 19.5; ASES-REC-05, ASES-SEC-01).

Asking the user is a first-class state, not an exception. A worker, or the controller (section 19.1: a budget or
bound reached, a malformed plan or verdict, a merge that will not go through), blocks its card with the question
as the block reason, and this module is how a person finds those cards and answers them.

There is no question store. A question IS a blocked card on the Hermes board: answering it unblocks the card, so
it leaves the next listing by itself, and nothing here ever ages a question into a default answer (ASES-REC-05:
"Unanswered questions MUST NOT time out into guesses"). A card stays blocked until a person answers it.

  list_questions     the blocked cards of one plan that carry a block reason nobody has answered yet
  answer_question    add the answer as a card comment, then unblock the card
  format_questions   the plain-ASCII text `swarm questions` prints

The outside calls are the hermes module (list, show, comment, unblock) and the events table. Nothing here calls
a provider.
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


class QuestionError(Exception):
    """An answer that cannot be delivered, with a message fit to show the person who typed it. It never carries
    the answer text: a refused answer may be exactly the one that held a secret."""


@dataclasses.dataclass(frozen=True)
class Question:
    """One open question: a blocked card and the reason it was blocked with.

    card_kind is "work", "merge", "fix" or "other". task_key is None for a card that belongs to no plan task
    (only answer_question can return one: list_questions keeps the cards of one plan). asked_at is the epoch
    seconds of the `blocked` event that raised the question, 0 when Hermes gave no time."""
    card_id: str
    title: str
    task_key: str | None
    card_kind: str
    assignee: str | None
    question: str
    asked_at: int


def _epoch(value) -> int:
    """`value` as whole epoch seconds, 0 when it is missing or not a number. Hermes hands timestamps over as ints
    and, in places, as numeric strings (a run's started_at is one), so both are read."""
    if isinstance(value, bool):
        return 0
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


def _latest_block(card: dict) -> tuple[str, int] | None:
    """(reason, asked_at) from the card's LATEST `blocked` event, or None when there is no such event or it
    carries no reason.

    "Latest" is by created_at, and among events with the same time by position in the list, so a card blocked
    twice (answered once, blocked again) reports the newest question, not the one already answered. Only the
    newest event is looked at: an empty reason on it means the block that holds the card now asks nothing, and
    reaching back to an older reason would show a question that was answered long ago. Hermes hands a payload
    over as a dict or as a JSON string, so both are read."""
    latest = None
    for position, event in enumerate(card.get("_events") or []):
        if not isinstance(event, dict) or event.get("kind") != "blocked":
            continue
        stamp = (_epoch(event.get("created_at")), position)
        if latest is None or stamp >= latest[0]:
            latest = (stamp, event)
    if latest is None:
        return None
    (asked_at, _position), event = latest
    payload = event.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = None
    reason = payload.get("reason") if isinstance(payload, dict) else None
    if not isinstance(reason, str) or not reason.strip():
        return None
    return reason.strip(), asked_at


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
    """ASES-REC-05: "swarm questions lists open questions with their cards". Every blocked card of this plan that
    carries a block reason nobody has answered yet, oldest first (asked_at, then card id).

    A question is a card that is `blocked` AND whose latest `blocked` event has a reason: Hermes writes that
    event when a worker (kanban_block) or the controller blocks a card, and the reason is the question. A card
    blocked with no such event asks nothing. The controller creates every merge card blocked, waiting for its
    work card, and that must not read as a question, while a merge card the controller blocked on purpose (its
    fix-card budget is spent, section 19.1) does carry a reason: the controller is asking the user. Once a
    question is answered the card is unblocked, so it leaves this list by itself.

    A card belongs to the plan by the rules in _owner: its id or a parent's id is a work or merge card of a row
    in plan_tasks for plan.project, or its title starts with a task key of the plan and no other project claims
    it. Other projects' cards on the same board are left out (and are not even read).

    A card whose kanban_show fails (HermesCommandError: archived, or gone between the list and the show) is
    skipped and the rest are still listed, because one unreadable card must not hide every other question. The
    skip is not silent: a question_read_failed event records the card and Hermes's error. Any other failure,
    and a failure of the list itself, propagates: a Hermes that cannot answer at all must not look like an
    empty inbox."""
    cards, foreign, keys = _card_index(conn, plan.project)
    keys = keys | {task.key for task in plan.tasks}
    found: list[Question] = []
    for entry in hermes_mod.kanban_list(board, status="blocked"):
        card_id = entry.get("id")
        if not card_id or (card_id in foreign and card_id not in cards):
            continue
        try:
            card = hermes_mod.kanban_show(board, card_id)
        except hermes_mod.HermesCommandError as exc:
            events.record(conn, "question_read_failed", {
                "card_id": card_id, "error": f"{type(exc).__name__}: {exc}"[:300],
            })
            continue
        if card.get("status", "blocked") != "blocked":
            continue  # answered (or otherwise moved on) between the list and the show
        title = card.get("title") or entry.get("title") or ""
        owner = _owner(card_id, title, _parent_ids(card), cards, foreign, keys)
        block = _latest_block(card)
        if owner is None or block is None:
            continue
        task_key, kind_by_id = owner
        found.append(Question(
            card_id=card_id, title=title, task_key=task_key, card_kind=_kind(kind_by_id, title),
            assignee=card.get("assignee") or entry.get("assignee") or None,
            question=block[0], asked_at=block[1],
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


def answer_question(
    board: str, card_id: str, text: str, *, conn: sqlite3.Connection, author: str = "user",
) -> Question:
    """ASES-REC-05: "swarm answer <card> "<text>" adds the answer as a card comment and unblocks it." Returns the
    Question that was answered (its reason is the one the answer replies to).

    The answer is refused, before Hermes is touched, when it is empty or blank, and when it looks like a secret
    (ASES-SEC-01: "scan card bodies ... before they are written"; the error names the line numbers, never the
    secret). The card is then read: it must be `blocked` with a block reason, else there is no open question
    ("card X has no open question") and nothing is written. This is not scoped to a plan, so any blocked card
    of the board can be answered by its id.

    The comment is posted FIRST ("ANSWER: <text>", by `author`) and the unblock second. In that order a failure
    can only leave a card that is still blocked with its answer already on it, which is safe and can be sent
    again. The other order could wake a worker with no answer to read, which is a guess in all but name
    (ASES-REC-05). A failed comment therefore never unblocks, and a failed unblock after a posted comment is
    not retried: HermesCommandError propagates from either, and so does one from the initial read, because a
    Hermes that failed is not the same fact as "no open question".

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
    if status != "blocked":
        raise QuestionError(
            f"card {_ascii(card_id)} has no open question (its status is {_ascii(str(status))}, not blocked)"
        )
    block = _latest_block(card)
    if block is None:
        raise QuestionError(
            f"card {_ascii(card_id)} has no open question (it is blocked, but no block reason was recorded)"
        )

    cards, foreign, keys = _card_index(conn, None)
    title = card.get("title") or ""
    owner = _owner(card_id, title, _parent_ids(card), cards, foreign, keys)
    task_key, kind_by_id = owner if owner is not None else (None, None)
    question = Question(
        card_id=card_id, title=title, task_key=task_key, card_kind=_kind(kind_by_id, title),
        assignee=card.get("assignee") or None, question=block[0], asked_at=block[1],
    )

    hermes_mod.kanban_comment(board, card_id, "ANSWER: " + text.strip(), author=author)
    hermes_mod.kanban_unblock(board, card_id, reason="answered by " + author)
    events.record(conn, "question_answered", {"card_id": card_id, "task_key": task_key, "chars": len(text)})
    return question


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

    A block is a numbered header line, the card id, task key, card kind and how long ago it was asked (against
    `now`, epoch seconds, default the current time; a parameter so a test can fix it), then the card title, and
    under it the question text indented four spaces, one indented line per line of the question. With no
    questions the whole output is the single line "No open questions.".

    Everything printed goes through _ascii (backslash escapes for non-ASCII, visible escapes for control
    characters), because the title and the question are agent text and the Windows console is cp1252. Line
    breaks in the question are honoured, but a line break in the id, kind or title cannot break the header."""
    if not questions:
        return "No open questions."
    current = time.time() if now is None else now
    blocks = []
    for number, question in enumerate(questions, start=1):
        task = f"task {_cell(question.task_key)}" if question.task_key else "no task"
        header = (
            f"{number}. {_cell(question.card_id)} "
            f"({task}, {_cell(question.card_kind)}, asked {_age(question.asked_at, current)})"
        )
        title = _cell(question.title)
        if title:
            header += f" - {title}"
        body = [("    " + _ascii(line)).rstrip() for line in question.question.splitlines()]
        blocks.append("\n".join([header, *body]))
    return "\n\n".join(blocks)
