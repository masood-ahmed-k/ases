"""Text helpers for the evaluation scorers: normalising, keyword topic scoring, and pulling code fences, JSON objects
and numbered answers out of a model's reply.

Everything here is a heuristic over free text and says so. A keyword scorer can be gamed by an answer that stuffs
keywords and can miss a right answer worded in an unexpected way; each task documents its own thresholds, and the
tests pin what every scorer accepts and rejects. The scorers never call a model (blueprint Appendix D, ASES-DOC-04).
"""
from __future__ import annotations

import dataclasses
import functools
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence


def ascii_safe(text: object) -> str:
    """`text` with every non-ASCII character written as an escape. Anything a person reads in a Windows console
    (cp1252) goes through this, because one stray arrow from a card title or a model reply raises an exception."""
    return str(text).encode("ascii", "backslashreplace").decode("ascii")


def clip(text: object, limit: int) -> str:
    """`text` cut to at most `limit` characters, ending in '...' when it was cut."""
    value = str(text)
    return value if len(value) <= limit else value[: max(limit - 3, 0)] + "..."


def normalize(text: str) -> str:
    """Lower case, accents dropped, every run of characters that are not letters or digits turned into ONE space,
    with a space at each end. A keyword pattern written against this form can anchor on word edges with \\b and
    ignores punctuation, hyphens and markdown: 'SQL-injection', 'sql injection' and '**SQL injection**' all read
    'sql injection'. Non-ASCII letters that have no ASCII form are dropped."""
    folded = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode("ascii")
    return " " + re.sub(r"[^a-z0-9]+", " ", folded.lower()).strip() + " "


@functools.lru_cache(maxsize=None)
def _compiled(pattern: str) -> re.Pattern:
    return re.compile(pattern)


_LIST_MARKER = re.compile(r"^[ \t]*(?:[-*+]|\d{1,3}[.)])[ \t]+", re.M)


def topic_hits(text: str, topics: Mapping[str, Sequence[str]]) -> dict[str, bool]:
    """For each topic, whether ANY of its regex patterns matches the normalised text. The patterns are written
    against normalize()'s output (lower case, single spaces, no punctuation). Bullet and item numbers are removed
    first, or the '5' of '5. Shops see...' would read as '5 shops' and satisfy a pattern for a count of shops."""
    norm = normalize(_LIST_MARKER.sub("", text))
    return {name: any(_compiled(p).search(norm) for p in patterns) for name, patterns in topics.items()}


def matches_all(text: str, patterns: Sequence[str]) -> bool:
    """True when EVERY regex in `patterns` matches the lower-cased text (not normalised: identifiers such as
    bulk_discount and file names such as pricing.py keep their underscores and dots this way)."""
    low = str(text).lower()
    return all(_compiled(p).search(low) for p in patterns)


def matches_any(text: str, patterns: Sequence[str]) -> bool:
    """True when at least one regex in `patterns` matches the lower-cased text (see matches_all)."""
    low = str(text).lower()
    return any(_compiled(p).search(low) for p in patterns)


_LIST_ITEM = re.compile(r"^[ \t]*(?:[-*+]|\d{1,3}[.)])[ \t]+\S", re.M)


def list_item_count(text: str) -> int:
    """How many lines start a bullet ('-', '*', '+') or a numbered item ('1.' or '1)')."""
    return len(_LIST_ITEM.findall(text))


def _lines(text: str) -> list[str]:
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


_BLOCK_START = re.compile(r"^[ \t]*(?:[-*+][ \t]+|\d{1,3}[.)][ \t]+|#{1,6}[ \t]+)")
_HEADING = re.compile(r"^[ \t]*#{1,6}[ \t]+")


def split_blocks(text: str) -> list[str]:
    """The reply cut into blocks that each hold one thought: paragraphs (separated by blank lines), and each bullet
    or numbered item together with its indented continuation and any deeper nested items. A scorer that needs two
    facts in one finding (a file AND a defect class) looks for both inside one block, so a reply that names every
    file in one line and every defect in another does not count as naming a defect in a file."""
    blocks: list[list[str]] = []
    current: list[str] = []
    base = 0
    for line in _lines(text):
        if not line.strip():
            if current:
                blocks.append(current)
                current = []
            continue
        starts = bool(_BLOCK_START.match(line))
        if current and starts and (_HEADING.match(line) or _indent(line) <= base):
            blocks.append(current)
            current = []
        if not current:
            base = _indent(line)
        current.append(line)
    if current:
        blocks.append(current)
    return ["\n".join(b) for b in blocks]


@dataclasses.dataclass(frozen=True)
class CodeBlock:
    """One fenced code block: its language tag (lower case, possibly empty), its body, and `hint`, the up to three
    non-empty lines just above the opening fence (where a model usually names the file the block belongs to)."""

    lang: str
    body: str
    hint: str


_FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})[ \t]*([A-Za-z0-9_+.#-]*)[^\n]*$")


def extract_code_blocks(text: str) -> list[CodeBlock]:
    """Every fenced block of the reply, in order. A fence closes on a line of the same character that is at least
    as long as the opener; a block still open at the end of the text (a reply cut off mid-block) runs to the end
    instead of being lost."""
    lines = _lines(text)
    blocks: list[CodeBlock] = []
    i = 0
    while i < len(lines):
        match = _FENCE.match(lines[i])
        if not match:
            i += 1
            continue
        fence, lang = match.group(1), match.group(2).lower()
        closer = re.compile(r"^[ \t]*" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}[ \t]*$")
        j = i + 1
        while j < len(lines) and not closer.match(lines[j]):
            j += 1
        hint = "\n".join(line for line in lines[max(0, i - 3):i] if line.strip())
        blocks.append(CodeBlock(lang, "\n".join(lines[i + 1:j]), hint))
        i = j + 1
    return blocks


def extract_json_objects(text: str) -> list[dict]:
    """Every top-level JSON object in the text, in order, found by trying to decode from each opening brace. Prose
    around an object, a code fence and a second object are all tolerated; an object nested inside another is not
    returned separately."""
    decoder = json.JSONDecoder()
    found: list[dict] = []
    i = 0
    while True:
        start = text.find("{", i)
        if start < 0:
            return found
        try:
            obj, end = decoder.raw_decode(text, start)
        except ValueError:
            i = start + 1
            continue
        if isinstance(obj, dict):
            found.append(obj)
            i = end
        else:
            i = start + 1


def is_bare_json(text: str) -> bool:
    """True when the whole reply is ONE JSON object, alone or inside a single code fence: the strict reading of
    'reply with exactly one JSON object and nothing else'."""
    body = text.strip()
    if body.startswith(("```", "~~~")):
        blocks = extract_code_blocks(body)
        if len(blocks) != 1 or not body.endswith(("```", "~~~")):
            return False
        body = blocks[0].body.strip()
    try:
        return isinstance(json.loads(body), dict)
    except ValueError:
        return False


def split_numbered_answers(text: str, prefix: str = "Q") -> dict[int, str]:
    """{n: answer text} for a reply that starts each answer with 'Q1:', 'Q2.', '**Q3**:' and so on (case
    insensitive, an optional bullet in front). Text after a marker up to the next marker belongs to it. A number
    that appears twice has its parts joined. A reply with no such markers gives {}."""
    marker = re.compile(
        r"^[ \t]*(?:[-*+][ \t]*)?(?:\*\*|__)?[ \t]*" + re.escape(prefix)
        + r"[ \t]*(\d{1,2})[ \t]*(?:\*\*|__)?[ \t]*[:.)-][ \t]*(.*)$",
        re.IGNORECASE,
    )
    answers: dict[int, list[str]] = {}
    current: int | None = None
    for line in _lines(text):
        match = marker.match(line)
        if match:
            current = int(match.group(1))
            answers.setdefault(current, []).append(match.group(2))
        elif current is not None:
            answers[current].append(line)
    return {n: "\n".join(parts).strip() for n, parts in answers.items()}
