"""Tamper check for Gate 1 (section 14.3: ASES-QG-02, ASES-QG-03; section 8.1: ASES-GIT-07; ASES-SEC-01).

Gate 1 runs a card's pinned commands in a clean checkout of the exact commit (ASES-QG-04), which proves the
tests PASS, not that the tests are still the tests. A worker under pressure to turn a red gate green has cheap
ways to do it that a passing run cannot see: delete the failing test, skip it, append `|| true` to the test
command, weaken an assertion until it cannot fail, or edit the runner configuration so the failing file is no
longer collected. This module reads the diff itself and reports each of those as a Finding, so Gate 1 can fail
with the right reason (test 22.12).

Two halves, kept apart on purpose. parse_diff and analyze_diff are pure functions of diff text and never
raise: malformed input gives an empty list or fewer findings, never an exception, so they are safe to run over
anything a worker produced. check_range is the only function here that runs git, and when git cannot answer (a
bad range, a missing repository, a timeout) it raises TamperCheckError instead of returning []: a gate that could
not run is never a silent pass, and what to do about it is the caller's decision.

These are heuristics over text. They flag for the controller to fail Gate 1 and for a reviewer to confirm, they
are not a proof that a test still tests what it did. Where a rule can misfire, the finding says why it fired and
the task's own touches (allow_paths) are the way to say "this change is intended".

Nothing in a Finding ever contains the text of a secret: a secret finding names the file and the line and says
"secret-shaped value", because the finding is sent to a card, a log and a model provider.
"""
from __future__ import annotations

import dataclasses
import fnmatch
import logging
import math
import pathlib
import re
import subprocess

from . import events as events_mod

_log = logging.getLogger(__name__)

# Every kind a Finding can carry. `line` is the line in the NEW file for an added line and in the OLD file for a
# removed one; None when the diff gave no line numbers (a bare +/- snippet) or the finding is about a whole file.
KINDS = (
    "test_file_deleted", "test_deleted", "skip_marker", "unconditional_pass", "assertion_weakened",
    "gate_config_changed", "generated_artifact", "secret_added", "large_file", "coverage_lowered",
)


class TamperCheckError(Exception):
    """git could not produce the diff (bad range, missing repository, timeout, git not installed). Raised by
    check_range only, so that a gate which could not run is never mistaken for a clean diff."""


@dataclasses.dataclass(frozen=True)
class Finding:
    """One thing the tamper check objects to. `path` is "" when the finding is about the whole change (a
    coverage drop). `blocks` is True for every finding the checks below produce: it exists so an informational
    finding can be represented without blocking Gate 1 (see blocking())."""
    kind: str
    path: str
    detail: str
    line: int | None = None
    blocks: bool = True


@dataclasses.dataclass(frozen=True)
class Hunk:
    """One @@ block. `header` is the text git prints after the closing @@ (the enclosing function or section,
    "" when git printed none). added is [(new line number or None, text)], removed is [(old line number or
    None, text)], context is the unchanged lines around them as [(new line number or None, text)]. Text has no
    leading +/-/space marker."""
    header: str
    old_start: int
    new_start: int
    added: list
    removed: list
    context: list


@dataclasses.dataclass(frozen=True)
class FileDiff:
    """One file's part of a diff. status is "A" (added), "M" (modified) or "D" (deleted). `path` is the new
    path (the old one for a deletion), old_path the old one (equal to path unless a rename was diffed).
    added_lines and removed_lines run over every hunk. A binary file has binary=True and no lines."""
    path: str
    old_path: str
    status: str
    added_lines: list
    removed_lines: list
    hunks: list
    binary: bool = False


# --- the diff parser --------------------------------------------------------------------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_HUNK_RE = re.compile(r"^@@+ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@+(?: (.*))?$")
_C_ESCAPES = {"a": 7, "b": 8, "f": 12, "n": 10, "r": 13, "t": 9, "v": 11, "\\": 92, '"': 34}
_OCTAL = "01234567"


def _coerce_text(diff_text: object) -> str:
    """The diff as text. None and non-text inputs give "", bytes are decoded as UTF-8 with replacement: this
    module never raises on what it is handed."""
    if isinstance(diff_text, str):
        return diff_text
    if isinstance(diff_text, (bytes, bytearray)):
        return bytes(diff_text).decode("utf-8", errors="replace")
    return ""


def _closing_quote(text: str) -> int:
    """Index of the quote that closes the C-quoted string starting at text[0], or -1."""
    i = 1
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == '"':
            return i
        i += 1
    return -1


def _unquote_c(text: str) -> str:
    """Undo git's C-style quoting of a path ("caf\\303\\251.py", "a\\tb"): a token wrapped in double quotes has its
    backslash escapes decoded (octal escapes are UTF-8 bytes). An unquoted token comes back unchanged."""
    if len(text) < 2 or text[0] != '"' or text[-1] != '"':
        return text
    body, out, i = text[1:-1], bytearray(), 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt in _C_ESCAPES:
                out.append(_C_ESCAPES[nxt])
                i += 2
                continue
            if nxt in _OCTAL:
                j = i + 1
                while j < len(body) and j < i + 4 and body[j] in _OCTAL:
                    j += 1
                out.append(int(body[i + 1:j], 8) & 0xFF)
                i = j
                continue
        out += ch.encode("utf-8", errors="replace")
        i += 1
    return out.decode("utf-8", errors="replace")


def _strip_side(path: str) -> str:
    """A path without git's a/ or b/ side prefix."""
    return path[2:] if path.startswith(("a/", "b/")) else path


def _header_path(spec: str) -> str | None:
    """The path a `--- ` or `+++ ` header line names, without its a/ or b/ prefix, or None for /dev/null. A
    path with a space is followed by a tab in these lines (the first raw tab ends the path: a path holding a
    tab is C-quoted, so it has none)."""
    spec = spec.split("\t", 1)[0]
    if spec == "/dev/null":
        return None
    return _strip_side(_unquote_c(spec))


def _split_diff_git(rest: str) -> tuple[str, str]:
    """(old path, new path) from the text after `diff --git `, as a fallback for a file with no ---/+++ lines
    (a binary file, an empty file, a mode change). The `+++ b/...` line is authoritative when there is one,
    because a path with a space makes this line ambiguous. Under --no-renames both sides are the same path, so
    the split that makes them equal is the right one."""
    if rest.startswith('"'):
        end = _closing_quote(rest)
        if end != -1:
            second = rest[end + 1:].lstrip(" ")
            return _strip_side(_unquote_c(rest[:end + 1])), _strip_side(_unquote_c(second))
    marks = [m.start() for m in re.finditer(" b/", rest)]
    for k in marks:
        left, right = rest[:k], rest[k + 1:]
        if left.startswith("a/") and left[2:] == right[2:]:
            return left[2:], right[2:]
    if marks:
        return _strip_side(rest[:marks[0]]), _strip_side(rest[marks[0] + 1:])
    left, _, right = rest.partition(" ")
    return _strip_side(left), _strip_side(right)


def _split_binary(line: str) -> tuple[str | None, str | None] | None:
    """(old path or None, new path or None) from `Binary files X and Y differ`, None when the line is not
    that shape. /dev/null on one side means an added or a deleted file."""
    body = line[len("Binary files "):-len(" differ")]
    for m in re.finditer(" and ", body):
        left, right = body[:m.start()], body[m.end():]
        if (left == "/dev/null" or left.startswith(("a/", '"a/'))) and (
                right == "/dev/null" or right.startswith(("b/", '"b/'))):
            return _header_path(left), _header_path(right)
    return None


class _FileBuilder:
    """Mutable accumulator for one file while parse_diff walks the lines."""

    def __init__(self, path: str = "", old_path: str = "") -> None:
        self.path, self.old_path, self.status = path, old_path, "M"
        self.added: list = []
        self.removed: list = []
        self.hunks: list = []
        self.binary = False
        self.saw_hunk = False
        self.saw_pair = False

    def freeze(self) -> FileDiff:
        path = self.path or self.old_path
        return FileDiff(
            path, self.old_path or path, self.status, self.added, self.removed,
            [h.freeze() for h in self.hunks], self.binary,
        )


class _HunkBuilder:
    def __init__(self, header: str, old_start: int, new_start: int) -> None:
        self.header, self.old_start, self.new_start = header, old_start, new_start
        self.added: list = []
        self.removed: list = []
        self.context: list = []

    def freeze(self) -> Hunk:
        return Hunk(self.header, self.old_start, self.new_start, self.added, self.removed, self.context)


def parse_diff(diff_text: str) -> list[FileDiff]:
    """A small unified-diff parser for `git diff --no-renames` output (ANSI colour is stripped, so a diff taken
    without --no-color still parses). Pure, and never raises: text it cannot make sense of gives fewer entries.

    The `+++ b/...` line is authoritative for a path (the `diff --git` line is only a fallback, it cannot be
    split on spaces). A file is A, M or D from the `new file mode` / `deleted file mode` lines and /dev/null
    headers; a binary file has an entry with no lines. Inside a hunk the header's line counts decide what is
    content and what is a header, so an added line whose text is `++ b/x` (which reads `+++ b/x`) is content,
    not a new file. Text with no headers at all, only +/- lines, becomes one entry with path "": the old
    marker checks in gates.detect_tamper were written against bare snippets like that."""
    text = _coerce_text(diff_text)
    if not text:
        return []
    lines = _ANSI_RE.sub("", text).split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # the newline that ends the last line, not a blank diff line

    files: list[FileDiff] = []
    cur: _FileBuilder | None = None
    hunk: _HunkBuilder | None = None
    old_left = new_left = 0
    old_no = new_no = 0

    def finish() -> None:
        nonlocal cur, hunk, old_left, new_left
        if cur is not None:
            files.append(cur.freeze())
        cur, hunk, old_left, new_left = None, None, 0, 0

    def add_line(builder: _FileBuilder, kind: str, number: int | None, body: str) -> None:
        nonlocal hunk
        if hunk is None:  # a bare +/- line with no @@ before it: an implicit hunk with no line numbers
            hunk = _HunkBuilder("", 0, 0)
            builder.hunks.append(hunk)
        entry = (number, body)
        if kind == "+":
            builder.added.append(entry)
            hunk.added.append(entry)
        elif kind == "-":
            builder.removed.append(entry)
            hunk.removed.append(entry)
        else:
            hunk.context.append(entry)

    i, n = 0, len(lines)
    while i < n:
        raw = lines[i]
        i += 1
        line = raw[:-1] if raw.endswith("\r") else raw

        if cur is not None and hunk is not None and (old_left > 0 or new_left > 0):
            tag, body = line[:1], line[1:]
            if (tag == " " or line == "") and old_left > 0 and new_left > 0:
                add_line(cur, " ", new_no, body)
                old_left, new_left, old_no, new_no = old_left - 1, new_left - 1, old_no + 1, new_no + 1
                continue
            if tag == "-" and old_left > 0:
                add_line(cur, "-", old_no, body)
                old_left, old_no = old_left - 1, old_no + 1
                continue
            if tag == "+" and new_left > 0:
                add_line(cur, "+", new_no, body)
                new_left, new_no = new_left - 1, new_no + 1
                continue
            if tag == "\\":  # "\ No newline at end of file"
                continue
            old_left = new_left = 0  # the counts were wrong: leave the counted region and read this line normally

        if line.startswith("diff --git "):
            finish()
            old, new = _split_diff_git(line[len("diff --git "):])
            cur = _FileBuilder(new, old)
            continue
        if line.startswith("--- ") and i < n and lines[i].startswith("+++ "):
            old_spec, new_spec = line[4:], lines[i][4:].rstrip("\r")
            i += 1
            if cur is None or cur.saw_hunk or cur.saw_pair:
                finish()  # a second file in a diff that has no `diff --git` lines
                cur = _FileBuilder()
            cur.saw_pair = True
            old, new = _header_path(old_spec), _header_path(new_spec)
            if old is None:
                cur.status, cur.path = "A", new or cur.path
            elif new is None:
                cur.status, cur.path, cur.old_path = "D", old, old
            else:
                cur.path, cur.old_path = new, old
            hunk = None
            continue
        if line.startswith("@@"):
            m = _HUNK_RE.match(line)
            if m is None:
                continue
            if cur is None:
                cur = _FileBuilder()
            cur.saw_hunk = True
            old_no, new_no = int(m.group(1)), int(m.group(3))
            old_left = 1 if m.group(2) is None else int(m.group(2))
            new_left = 1 if m.group(4) is None else int(m.group(4))
            hunk = _HunkBuilder((m.group(5) or "").strip(), old_no, new_no)
            cur.hunks.append(hunk)
            continue
        if cur is not None:
            if line.startswith("new file mode"):
                cur.status = "A"
                continue
            if line.startswith("deleted file mode"):
                cur.status = "D"
                continue
            if line.startswith("rename from ") or line.startswith("copy from "):
                cur.old_path = _unquote_c(line.split(" from ", 1)[1])
                continue
            if line.startswith("rename to ") or line.startswith("copy to "):
                cur.path = _unquote_c(line.split(" to ", 1)[1])
                if line.startswith("copy to "):
                    cur.status = "A"
                continue
            if line.startswith("GIT binary patch"):
                cur.binary = True
                continue
        if line.startswith("Binary files ") and line.endswith(" differ"):
            if cur is None:
                cur = _FileBuilder()
            cur.binary = True
            sides = _split_binary(line)
            if sides is not None:
                old, new = sides
                if old is None:
                    cur.status, cur.path = "A", new or cur.path
                elif new is None:
                    cur.status, cur.path, cur.old_path = "D", old, old
                else:
                    cur.path, cur.old_path = new, old
            continue
        if line[:1] in ("+", "-"):
            if cur is None:
                cur = _FileBuilder()  # a bare snippet: path ""
            add_line(cur, line[0], None, line[1:])  # outside a counted hunk there is no reliable line number
            continue
        # anything else (index lines, mode lines, prose, blank lines outside a hunk) carries no content
    finish()
    return files


# --- path classification and glob matching ----------------------------------------------------------------

_DOC_EXTENSIONS = (".md", ".markdown", ".rst", ".txt", ".adoc")
_CODE_EXTENSIONS = frozenset({
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".rb", ".go", ".java", ".kt", ".rs", ".php", ".cs",
    ".c", ".cc", ".cpp", ".h", ".hpp", ".swift", ".scala", ".sh", ".bash", ".ps1", ".lua", ".ex", ".exs",
    ".clj", ".dart", ".pl", ".r",
})
_TEST_DIRS = frozenset({"tests", "test", "__tests__", "spec"})
_TEST_NAME_PATTERNS = (
    "test_*.py", "*_test.py", "*_test.go", "*_spec.rb", "*Test.java", "*Tests.java",
    "*.test.js", "*.test.jsx", "*.test.ts", "*.test.tsx", "*.test.mjs", "*.test.cjs",
    "*.spec.js", "*.spec.jsx", "*.spec.ts", "*.spec.tsx", "*.spec.mjs", "*.spec.cjs",
)
_SCRIPT_EXTENSIONS = frozenset({".sh", ".bash", ".zsh", ".ksh", ".ps1", ".bat", ".cmd", ".mk", ".yml", ".yaml"})
_SCRIPT_NAMES = frozenset({
    "makefile", "gnumakefile", "jenkinsfile", "dockerfile", "package.json", "tox.ini", "justfile",
})
_CI_DIRS = (".github/", ".circleci/", ".buildkite/", ".gitlab/")


def _norm(path: str) -> str:
    """A path or glob with forward slashes and no leading ./ (a plan may say ./src/a.py where git says
    src/a.py); the same normalisation plan._normalize_glob applies."""
    return path.replace("\\", "/").removeprefix("./")


def _extension(name: str) -> str:
    dot = name.rfind(".")
    return name[dot:].lower() if dot > 0 else ""


def _is_doc(path: str) -> bool:
    """Prose files: a skip marker or `|| true` inside a README is documentation, not a skipped test."""
    return path.lower().endswith(_DOC_EXTENSIONS)


def _is_test_path(path: str) -> bool:
    """Looks like a test file: a test-shaped file name, or a source file inside a tests/test/__tests__/spec
    directory. The directory rule needs a code extension so that `spec/requirements.yaml` (a specification
    document, not a spec file) and `tests/fixtures/data.json` are not read as tests."""
    if not path:
        return False
    parts = path.split("/")
    name = parts[-1]
    if any(fnmatch.fnmatchcase(name, pattern) for pattern in _TEST_NAME_PATTERNS):
        return True
    if any(part.lower() in _TEST_DIRS for part in parts[:-1]):
        return _extension(name) in _CODE_EXTENSIONS
    return False


def _is_script_or_ci(path: str) -> bool:
    """A file whose lines are shell or CI commands, where `exit 0`, `set +e` and `; true` mean "make the step
    pass". A bare snippet with no path is treated as one: unknown is read strictly."""
    if not path:
        return True
    lower = path.lower()
    name = lower.rsplit("/", 1)[-1]
    return (
        name in _SCRIPT_NAMES or name.startswith(("dockerfile.", "jenkinsfile"))
        or _extension(name) in _SCRIPT_EXTENSIONS
        or lower.startswith(_CI_DIRS) or any(f"/{d}" in lower for d in _CI_DIRS)
    )


def _globs(value: object) -> tuple[str, ...]:
    """A tuple of normalised, non-empty glob strings from whatever the caller passed: a list, a tuple, a
    generator, None, or a bare string (one glob, not one glob per character)."""
    if value is None:
        return ()
    if isinstance(value, str):
        value = (value,)
    try:
        return tuple(_norm(g.strip()) for g in value if isinstance(g, str) and g.strip())
    except TypeError:
        return ()


def _glob_match(path: str, glob: str) -> bool:
    """The same matching integrity.paths_outside_touches uses for a task's touches: fnmatch on forward-slash
    paths, so `*` and `**` both cross directories. Sharing it means the scope check and the tamper check can
    never disagree about what a task's touches cover."""
    return path == glob or fnmatch.fnmatch(path, glob)


def _matches(path: str, globs: tuple[str, ...]) -> bool:
    return bool(path) and any(_glob_match(path, glob) for glob in globs)


def _ascii(text: str) -> str:
    """text with every control and non-ASCII character backslash-escaped: the Windows console is cp1252 and
    crashes on an arrow or an accented letter in a card title or a path."""
    out = []
    for ch in text:
        code = ord(ch)
        if 32 <= code < 127:
            out.append(ch)
        elif code < 256:
            out.append(f"\\x{code:02x}")
        elif code < 65536:
            out.append(f"\\u{code:04x}")
        else:
            out.append(f"\\U{code:08x}")
    return "".join(out)


# --- the marker tables --------------------------------------------------------------------------------------

def _compile_table(rows: tuple) -> tuple:
    """Turn (label, key, pattern, ignore case, script only) rows into (label, key, compiled, script only). `key`
    is a lowercase literal that any line the pattern matches must contain, and a line is tested against the
    pattern only when its lowercased text holds the key. Most added lines match nothing and a diff can add
    tens of thousands of them: the substring test is what keeps a 200,000-line diff to seconds instead of
    minutes. A wrong key would silently switch its row off, so the tests give every row a positive case."""
    return tuple(
        (label, key, re.compile(f"(?i:{pattern})" if ignore else pattern), script_only)
        for label, key, pattern, ignore, script_only in rows
    )


# (label, key, pattern, ignore case, only in script and CI files). The label is what a finding reports.
_SKIP_ROWS = (
    ("pytest.mark.skip", "pytest.mark.skip", r"pytest\.mark\.skip", True, False),
    ("pytest.skip(", "pytest.skip(", r"pytest\.skip\(", True, False),
    ("pytest.mark.xfail", "pytest.mark.xfail", r"pytest\.mark\.xfail", True, False),
    ("xfail(", "xfail(", r"\bxfail\(", True, False),
    ("unittest.skip", "unittest.skip", r"unittest\.skip", True, False),
    ("@skip", "@skip", r"@skip", True, False),
    ("skipif(", "skipif(", r"skipif\(", True, False),
    ("skipUnless", "skipunless", r"skipUnless", True, False),
    ("it.skip(", "it.skip(", r"\bit\.skip\(", True, False),
    ("test.skip(", "test.skip(", r"\btest\.skip\(", True, False),
    ("describe.skip(", "describe.skip(", r"\bdescribe\.skip\(", True, False),
    ("xit(", "xit(", r"(?<![\w.])xit\(", True, False),
    ("xdescribe(", "xdescribe(", r"(?<![\w.])xdescribe\(", True, False),
    ("xtest(", "xtest(", r"(?<![\w.])xtest\(", True, False),
    ("t.Skip(", "t.skip", r"\bt\.Skip(?:f|Now)?\(", True, False),
    ("#[ignore]", "#[ignore", r"#\[ignore\b", True, False),
    ("@Ignore", "@ignore", r"@Ignore\b", True, False),
    ("@Disabled", "@disabled", r"@Disabled\b", True, False),
    # .only( is only a marker after a test-runner word: Django's queryset.only('a') is not one.
    (".only(", ".only(", r"\b(?:it|test|describe|context|suite)\.only\(", True, False),
    # fit( is only a marker as a bare call: model.fit(x, y) is machine learning, not a focused test.
    ("fit(", "fit(", r"(?<![\w.])fit\(", True, False),
    ("fdescribe(", "fdescribe(", r"(?<![\w.])fdescribe\(", True, False),
    ("--deselect", "--deselect", r"--deselect\b", True, False),
    ('-k "not', "-k", r"""(?<![\w-])-k\s+["']not\b""", True, False),
    ("# noqa: test", "noqa", r"#\s*noqa:\s*test\b", True, False),
)
_SKIP_TABLE = _compile_table(_SKIP_ROWS)

_UNCONDITIONAL_ROWS = (
    ("|| true", "||", r"\|\|\s*true\b", True, False),
    ("|| :", "||", r"\|\|\s*:(?=\s*(?:$|[;&|)#]))", True, False),
    ("|| exit 0", "||", r"\|\|\s*exit\s+0\b", True, False),
    ("--passWithNoTests", "--passwithnotests", r"--passWithNoTests\b", True, False),
    ("continue-on-error: true", "continue-on-error", r"\bcontinue-on-error:\s*[\"']?true\b", True, False),
    ("allow_failure: true", "allow_failure", r"\ballow_failure:\s*[\"']?true\b", True, False),
    ("if: false", "if:", r"\bif:\s*(?:false|\$\{\{\s*false\s*\}\})\s*(?:#.*)?$", True, False),
    ("assert True", "assert", r"^\s*assert\s+(?:True|1|not\s+False)\s*(?:,.*)?(?:#.*)?$", False, False),
    ("assert x == x", "assert", r"^\s*assert\s+([\w.]+)\s*==\s*\1\s*(?:,.*)?(?:#.*)?$", False, False),
    ("assert(true)", "assert(", r"\bassert\(\s*(?:true|1)\s*\)", False, False),
    ("assertTrue(True)", "asserttrue(", r"\bassertTrue\(\s*(?:True|1)\s*\)", False, False),
    ("expect(true).toBe(true)", "expect(",
     r"\bexpect\(\s*(true|false|\d+)\s*\)\s*\.\s*(?:toBe|toEqual|toStrictEqual)\(\s*\1\s*\)", False, False),
    ("pass  # test", "pass", r"^\s*pass\s*#\s*test\b", True, False),
    # These three read as "make the step pass" only in a script or a CI file (Python has no `exit 0`).
    ("; true", ";", r";\s*true\s*;?\s*(?:#.*)?$", True, True),
    ("set +e", "set", r"\bset\s+\+[a-z]*e[a-z]*\b|\bset\s+\+o\s+errexit\b", True, True),
    ("exit 0", "exit", r"(?:^|[\s;&|(])exit\s+(?:/b\s+)?0\s*;?\s*(?:#.*)?$", True, True),
)
_UNCOND_TABLE = _compile_table(_UNCONDITIONAL_ROWS)


def _first_marker(text: str, table: tuple, script_like: bool) -> str | None:
    """The label of the first row of `table` that matches `text`, or None. `script_like` includes the rows that
    only make sense in a script or a CI file. Rows are tried in table order, so the most specific comes first."""
    lowered = text.lower()
    for label, key, pattern, script_only in table:
        if key in lowered and (script_like or not script_only) and pattern.search(text):
            return label
    return None


# Assertion lines, for the weakening check.
_ASSERTION_RE = re.compile(
    r"(?<![\w.])assert\b"                      # assert x, assert(x), assert.equal(...)
    r"|\bself\.assert\w*\("                    # unittest
    r"|\bassert[A-Z]\w*\s*\("                  # JUnit style assertEquals(
    r"|\bexpect\("                             # jest, chai, vitest
    r"|\.should\b"                             # should.js, chai
    r"|\brequire\.\w+\("                       # testify require.Equal(
    r"|\bassert_(?:eq|ne)!|\bassert!"          # Rust
    r"|\bt\.(?:Error|Errorf|Fatal|Fatalf)\("   # Go
)
_STRONG_RE = re.compile(
    r"==|assertEqual|assertEquals|assertDictEqual|assertListEqual|toBe\(|toEqual\(|toStrictEqual\("
    r"|assert_eq!|\.Equal\(",
)
_WEAK_RE = re.compile(
    r"\bis\s+not\s+None\b|\bis\s+not\s+False\b|>=\s*0\b|!=\s*None\b|!==?\s*(?:null|undefined)\b"
    r"|\btoBeDefined\b|\btoBeTruthy\b|\.not\.toBe(?:Null|Undefined)\b|\bassertIsNotNone\b"
    r"""|^\s*assert\s+(?:not\s+)?[\w.\[\]'"]+(?:\([^=<>!]*\))?\s*(?:,.*)?$"""
)
_COMMENT_PREFIXES = ("#", "//", "/*", "*", "--")
# Every alternative of _ASSERTION_RE contains one of these (lowercase) literals: the cheap test that keeps the
# check off the vast majority of changed lines.
_ASSERTION_KEYS = ("assert", "expect(", ".should", "require.", "t.error", "t.fatal")


def _is_assertion(text: str) -> bool:
    lowered = text.lower()
    if not any(key in lowered for key in _ASSERTION_KEYS):
        return False
    stripped = text.strip()
    return bool(stripped) and not stripped.startswith(_COMMENT_PREFIXES) and bool(_ASSERTION_RE.search(text))


# Test definitions, for the deleted-test check.
_PY_TEST_RE = re.compile(r"^\s*(?:async\s+)?def\s+(test\w*)\s*\(")
_GO_TEST_RE = re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?(Test\w*)\s*\(")
_JAVA_TEST_RE = re.compile(r"^\s*(?:(?:public|protected|private|static)\s+)*void\s+(test\w*)\s*\(")
_JS_TEST_RE = re.compile(r"""^\s*(?:it|test)(?:\.each\([^)]*\)|\.\w+)?\s*\(\s*(["'`])(.*?)\1""")
_RSPEC_RE = re.compile(r"""^\s*it\s+(["'])(.*?)\1""")
_ATTR_TEST_RE = re.compile(r"^\s*(?:@(?:Test|ParameterizedTest|RepeatedTest)\b|#\[(?:tokio::)?test\b)")
_JAVA_METHOD_RE = re.compile(
    r"^\s*(?:(?:public|protected|private|static|final|synchronized)\s+)*[\w<>\[\],.?]+\s+(\w+)\s*\("
)
_RUST_FN_RE = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+(\w+)")


def _test_definitions(lines: list) -> list[tuple[int | None, str]]:
    """(line number, test name) for each test definition in `lines` ([(number, text)], in order). A `@Test` or
    `#[test]` attribute is followed by the method it marks, which names the test; an attribute with no method
    in the next few lines gets the name "" (the annotation went away and the method may have stayed, which
    silently stops the test running)."""
    found: list[tuple[int | None, str]] = []
    pending: tuple[int | None, str] | None = None  # (attribute line number, "java" or "rust")
    for number, text in lines:
        if pending is not None:
            close = pending[0] is None or number is None or number - pending[0] <= 3
            method = (_RUST_FN_RE if pending[1] == "rust" else _JAVA_METHOD_RE).match(text) if close else None
            if method:
                found.append((pending[0], method.group(1)))
                pending = None
                continue
            if close and (not text.strip() or text.lstrip().startswith(("@", "#["))):
                continue  # stacked attributes or a blank line before the method
            found.append((pending[0], ""))
            pending = None
        if _ATTR_TEST_RE.match(text):
            pending = (number, "rust" if text.lstrip().startswith("#[") else "java")
            continue
        for regex in (_PY_TEST_RE, _GO_TEST_RE, _JAVA_TEST_RE):
            match = regex.match(text)
            if match:
                found.append((number, match.group(1)))
                break
        else:
            match = _JS_TEST_RE.match(text) or _RSPEC_RE.match(text)
            if match:
                found.append((number, match.group(2)))
    if pending is not None:
        found.append((pending[0], ""))
    return found


# --- secrets ---------------------------------------------------------------------------------------------------

# Shapes events.py's redaction does not cover. The provider-key shapes (sk-, ghp_, xox...) stay in events.py, the
# one place that defines them, and secret_hint asks it first.
_EXTRA_SECRET_SHAPES = (
    ("private key block", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")),
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("payment provider live key", re.compile(r"\b[rs]k_live_[A-Za-z0-9]{16,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
)
_SECRET_FILE_SUFFIXES = (".pem", ".key", ".p12", ".pfx")
_SECRET_FILE_PREFIXES = ("id_rsa", "id_ed25519")
_ENV_EXAMPLES = frozenset({".env.example", ".env.sample"})


def secret_hint(text: str) -> str | None:
    """ASES-SEC-01: a short label for the kind of secret `text` looks like ("provider token", "private key
    block"), or None. The label never contains any of the matched text: it goes into a finding, and a finding
    goes to a card, a log and a model provider. Reuses events.py's redaction, so there is one definition of a
    provider key, and adds the few shapes a diff can carry that an event payload never does."""
    try:
        if events_mod.redact({"line": text})["line"] != text:
            return "provider token"
    except Exception:  # redaction is a safety net; a failure in it must not hide the other patterns
        pass
    for label, pattern in _EXTRA_SECRET_SHAPES:
        if pattern.search(text):
            return label
    return None


def _secret_file_marker(path: str) -> str | None:
    """The part of a file NAME that makes it a secret file (`.env`, `.pem`, `id_rsa`), or None. `.env.example`
    and `.env.sample` are templates and are not secret files."""
    lower = path.lower().rsplit("/", 1)[-1]
    if lower == ".env" or (lower.startswith(".env.") and lower not in _ENV_EXAMPLES):
        return ".env"
    for suffix in _SECRET_FILE_SUFFIXES:
        if lower.endswith(suffix):
            return suffix
    for prefix in _SECRET_FILE_PREFIXES:
        if lower.startswith(prefix):
            return prefix
    return None


def is_secret_file(path: str) -> bool:
    """ASES-GIT-07 / ASES-SEC-01: is this file name one that holds secrets (.env, .env.*, *.pem, *.key,
    id_rsa*, id_ed25519*, *.p12, *.pfx)? A name check only: it never looks at content."""
    return _secret_file_marker(_norm(path or "")) is not None


# --- the per-file analysis ---------------------------------------------------------------------------------

_ARTIFACT_DIRS = frozenset({
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules", "dist", "build", ".venv",
    "venv", "htmlcov", "target",
})
_ARTIFACT_SUFFIXES = (".pyc", ".pyo", ".log", ".sqlite", ".sqlite3", ".db")
_ARTIFACT_NAMES = frozenset({".ds_store", "coverage.xml", ".coverage"})

_CONFIG_NAMES = frozenset({
    ".gitlab-ci.yml", "azure-pipelines.yml", "jenkinsfile", "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml",
    "conftest.py", ".coveragerc", "noxfile.py", "makefile", ".pre-commit-config.yaml",
})
_CONFIG_NAME_PATTERNS = ("jest.config.*", "vitest.config.*", "karma.conf.*")
_CI_PATH_RE = re.compile(r"(?:^|/)(?:\.github/workflows|\.circleci)/")

# ASES-QG-02 (section 14.3, plan.py's Gate 0): the same names and CI directories _config_reason treats as gate,
# CI or test-runner configuration, exported as glob patterns so Gate 0's touches check shares this one list
# instead of keeping a second copy that could drift from it. The two directory patterns mirror _CI_PATH_RE
# above; the rest is _CONFIG_NAMES and _CONFIG_NAME_PATTERNS verbatim. package.json and Cargo.toml are left out
# on purpose: _config_reason only counts them as gate configuration when a hunk touches their test-related keys
# or sections, a diff-time judgement Gate 0 cannot make from a path glob alone, before any diff exists.
GATE_CONFIG_PATTERNS: tuple[str, ...] = (
    (".github/workflows/**", ".circleci/**") + tuple(sorted(_CONFIG_NAMES)) + _CONFIG_NAME_PATTERNS
)
_PACKAGE_KEY_RE = re.compile(r'"(?:scripts|jest|test|pretest|posttest|test:[^"]*)"')
_CARGO_SECTION_RE = re.compile(r"^\s*\[(?:profile|lints)\b")


def _clip(text: str, limit: int = 60) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _artifact_reason(path: str) -> tuple[str, str] | None:
    """(why, the text an allow glob must contain to name the file explicitly) when an ADDED file at `path` is
    a generated artifact or holds secrets, else None."""
    parts = path.split("/")
    for part in parts[:-1]:
        lower = part.lower()
        if lower in _ARTIFACT_DIRS:
            return f"file inside the generated directory {part}/", lower
        if lower.endswith(".egg-info"):
            return "file inside an .egg-info directory", ".egg-info"
    secret = _secret_file_marker(path)
    if secret is not None:
        return "file whose name marks it as holding secrets", secret
    name = parts[-1].lower()
    for suffix in _ARTIFACT_SUFFIXES:
        if name.endswith(suffix):
            return f"generated or local-state file ({suffix})", suffix
    if name in _ARTIFACT_NAMES:
        return "generated file", name
    return None


def _hunks_match(fd: FileDiff, regex: re.Pattern) -> bool:
    """Does any line a hunk shows (changed or context) or any hunk header match `regex`? The context lines
    matter: a script added to the `scripts` block of package.json changes lines that do not say `scripts`."""
    for hunk in fd.hunks:
        if regex.search(hunk.header):
            return True
        for lines in (hunk.added, hunk.removed, hunk.context):
            if any(regex.search(text) for _, text in lines):
                return True
    return False


def _config_reason(path: str, fd: FileDiff) -> str | None:
    """ASES-QG-02: why a change to `path` counts as a change to gate, CI or test-runner configuration, or None.
    package.json and Cargo.toml hold much that is not test configuration, so they count only when a hunk
    touches the test-related keys or sections."""
    name = path.rsplit("/", 1)[-1].lower()
    if _CI_PATH_RE.search(path):
        return "CI configuration"
    if name in _CONFIG_NAMES or any(fnmatch.fnmatchcase(name, pattern) for pattern in _CONFIG_NAME_PATTERNS):
        return "gate, CI or test-runner configuration"
    if name == "package.json" and _hunks_match(fd, _PACKAGE_KEY_RE):
        return "package.json scripts or test-runner settings"
    if name == "cargo.toml" and _hunks_match(fd, _CARGO_SECTION_RE):
        return "Cargo.toml profile or lint settings"
    return None


def _deleted_tests(fd: FileDiff, path: str) -> list[Finding]:
    """ASES-QG-03: removed test definitions in a modified test file. A removal is fine when its name is still
    among the file's added lines (the signature line changed, or the test moved within the file) or when an
    added test definition with a new name stands in for it (a rename shows as delete plus add). Each added
    definition can stand in for one removal only, so deleting three tests and adding one is still two
    deletions, and a removal with no test definition added in the file at all is always one."""
    removed = _test_definitions(fd.removed_lines)
    if not removed:
        return []
    added_text = "\n".join(text for _, text in fd.added_lines)
    removed_names = {name for _, name in removed}
    spare = sum(1 for _, name in _test_definitions(fd.added_lines) if name not in removed_names)
    findings = []
    for number, name in removed:
        if name and re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", added_text):
            continue
        if spare > 0:
            spare -= 1
            continue
        if name:
            detail = f"test removed: {_clip(name, 80)} (no test definition was added in this file to take its place)"
        else:
            detail = "test attribute removed (@Test or #[test]): the test no longer runs"
        findings.append(Finding("test_deleted", path, detail, number))
    return findings


def _weakened_assertions(fd: FileDiff, path: str, already_flagged: set) -> list[Finding]:
    """ASES-QG-03: per hunk, fewer assertion lines added than removed, and a removed equality assertion
    replaced by a weaker one (is not None, >= 0, a bare truthiness test). Commented-out lines are not
    assertions on either side, so commenting an assertion out counts as removing it. A line already reported
    as an unconditional pass (`assert True`) is not reported a second time under a less specific kind."""
    findings = []
    for hunk in fd.hunks:
        removed = [(n, t) for n, t in hunk.removed if _is_assertion(t)]
        added = [(n, t) for n, t in hunk.added if _is_assertion(t)]
        if len(removed) > len(added):
            where = f" near {_clip(hunk.header, 40)}" if hunk.header else ""
            findings.append(Finding(
                "assertion_weakened", path,
                f"{len(removed)} assertion line(s) removed and only {len(added)} added in one hunk{where}",
                removed[0][0],
            ))
        for (_, old_text), (new_number, new_text) in zip(removed, added):
            if (new_number, new_text) in already_flagged:
                continue
            if _STRONG_RE.search(old_text) and _WEAK_RE.search(new_text) and not _STRONG_RE.search(new_text):
                findings.append(Finding(
                    "assertion_weakened", path, f"assertion weakened: {_clip(old_text)} -> {_clip(new_text)}",
                    new_number,
                ))
    return findings


def _file_findings(fd: FileDiff, allow: tuple[str, ...], config_paths: tuple[str, ...]) -> list[Finding]:
    """Every finding for one file of a diff. `allow` is the task's touches and `config_paths` the paths its gate
    profile commands name, both already normalised. See analyze_diff for the rules."""
    path = _norm(fd.path or "")
    anonymous = path == ""
    doc = _is_doc(path)
    is_test = _is_test_path(path)
    script_like = _is_script_or_ci(path)
    allowed = _matches(path, allow)
    found: list[Finding] = []

    if fd.status == "D" and is_test:
        found.append(Finding("test_file_deleted", path, "test file deleted"))
    elif fd.status == "M" and (is_test or anonymous or path.endswith(".rs")):
        found.extend(_deleted_tests(fd, path))

    unconditional: set = set()
    for number, text in fd.added_lines:
        if not doc:
            marker = _first_marker(text, _SKIP_TABLE, False)
            if marker:
                found.append(Finding("skip_marker", path, f"skip marker added: {marker}", number))
            marker = _first_marker(text, _UNCOND_TABLE, script_like)
            if marker:
                found.append(Finding("unconditional_pass", path, f"unconditional pass added: {marker}", number))
                unconditional.add((number, text))
        hint = secret_hint(text)
        if hint:
            found.append(Finding("secret_added", path, f"secret-shaped value added ({hint})", number))

    # A path an allow glob matches is the task's own territory: its assertions are its own to change.
    if fd.status == "M" and not doc and not allowed:
        found.extend(_weakened_assertions(fd, path, unconditional))

    if path and not allowed:
        reason = _config_reason(path, fd)
        if reason is None and any(_glob_match(path, glob) for glob in config_paths):
            reason = "a path an approved gate profile command names"
        if reason is not None:
            found.append(Finding(
                "gate_config_changed", path,
                f"{reason} changed ({fd.status}) and no plan task allows it (ASES-QG-02)",
            ))

    if path and fd.status == "A":
        artifact = _artifact_reason(path)
        if artifact is not None:
            why, marker = artifact
            # An allow glob has to NAME the artifact (dist/**, *.db), not merely cover it: src/** covers
            # src/pkg/__pycache__/x.pyc, and a worker's stray cache directory is exactly what this rule is for.
            if not any(marker in glob.lower() and _glob_match(path, glob) for glob in allow):
                found.append(Finding("generated_artifact", path, f"{why} added (ASES-GIT-07)"))
    return found


def _analyze_files(files: list[FileDiff], allow: tuple[str, ...], config_paths: tuple[str, ...]) -> list[Finding]:
    findings: list[Finding] = []
    for fd in files:
        try:
            findings.extend(_file_findings(fd, allow, config_paths))
        except Exception:  # the contract is "never raises"; a bug here must not turn into a crash in the gate
            _log.exception("tamper analysis failed for %r", getattr(fd, "path", "?"))
    return findings


def analyze_diff(diff_text: str, *, allow_paths=(), gate_config_paths=()) -> list[Finding]:
    """ASES-QG-03 and ASES-QG-02 over the text of a diff. Pure, and never raises: malformed input gives an empty
    list or fewer findings.

    ASES-QG-03 (section 14.3): "The tamper check fails Gate 1 when a diff deletes or skips existing tests, adds
    unconditional passes such as || true, weakens assertions in files it did not need to touch, or lowers
    coverage of the changed area beyond the configured tolerance." The first three are found here as
    test_file_deleted, test_deleted, skip_marker, unconditional_pass and assertion_weakened (coverage is the
    numeric coverage_check below). ASES-QG-02: "A diff that changes gate configuration, CI scripts or test
    runner settings needs an explicit plan task that allows it": gate_config_changed. ASES-GIT-07 and
    ASES-SEC-01: generated_artifact and secret_added.

    `allow_paths` are the globs of the task's own touches, matched exactly as the touches check matches them. A
    changed path an allow glob covers is exempt from gate_config_changed and from assertion_weakened (it is the
    task's own territory) and from generated_artifact only when the glob names the artifact explicitly. It is
    never exempt from skip_marker, test_deleted, unconditional_pass, test_file_deleted or secret_added: no plan
    task can allow a worker to skip a test. `gate_config_paths` are extra paths whose change is gate
    configuration, typically the files an approved gate profile's commands name."""
    try:
        files = parse_diff(diff_text)
        return _analyze_files(files, _globs(allow_paths), _globs(gate_config_paths))
    except Exception:  # pragma: no cover - parse_diff and _analyze_files already contain their own failures
        _log.exception("tamper analysis failed")
        return []


# --- reading a range out of git -----------------------------------------------------------------------------

_GIT = ("git", "--no-optional-locks", "-c", "core.quotepath=false")


def _run_git(repo: pathlib.Path, args: list[str], timeout: float | None, *, stdin: bytes | None = None) -> bytes:
    """Run one read-only git command and return its stdout as bytes. Anything but a clean answer (git missing,
    a timeout, a non-zero exit) is a TamperCheckError, never an empty result: an empty diff and a failed diff
    must not look alike. --no-optional-locks so the check can never take index.lock from a real git operation."""
    try:
        proc = subprocess.run([*_GIT, "-C", str(repo), *args], capture_output=True, timeout=timeout, input=stdin)
    except subprocess.TimeoutExpired as exc:
        raise TamperCheckError(f"git {args[0]} timed out after {timeout}s") from exc
    except (OSError, ValueError) as exc:  # git not installed, a repo path that cannot be used
        raise TamperCheckError(f"git could not be run: {_ascii(str(exc))}") from exc
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()
        why = _ascii(stderr[0][:200]) if stderr else f"exit {proc.returncode}"
        raise TamperCheckError(f"git {args[0]} failed: {why}")
    return proc.stdout


def _check_rev(name: str, value: object) -> str:
    """A revision that is safe to put in a git argument: a value that starts with '-' would be read as an
    option (--output=... makes git write a file), and whitespace or a NUL cannot be part of a revision."""
    if not isinstance(value, str) or not value or value.startswith("-") or any(c in value for c in " \t\r\n\0"):
        raise TamperCheckError(f"unusable {name} revision: {_ascii(repr(value))[:80]}")
    return value


def _parse_name_status(raw: bytes) -> list[tuple[str, str]]:
    """[(status, path)] from `git diff --name-status -z`: NUL-separated status and path, paths verbatim. Status
    is A, M or D; a type change and anything else read as M."""
    tokens = raw.decode("utf-8", errors="replace").split("\0")
    entries = []
    for i in range(0, len(tokens) - 1, 2):
        letter, path = tokens[i].strip(), tokens[i + 1]
        if letter and path:
            entries.append((letter[0] if letter[0] in "AD" else "M", path))
    return entries


def _reconcile_status(files: list[FileDiff], statuses: list[tuple[str, str]]) -> list[FileDiff]:
    """The name-status listing is authoritative for which files changed and how: it corrects a status the
    patch text did not show (an empty file, a mode change) and adds an entry for any file the patch parser did
    not produce, so the file-level checks (a deleted test file, an added .env) still see it."""
    by_path = dict((path, status) for status, path in statuses)
    result, seen = [], set()
    for fd in files:
        status = by_path.get(fd.path)
        result.append(dataclasses.replace(fd, status=status) if status and status != fd.status else fd)
        seen.add(fd.path)
    result.extend(FileDiff(path, path, status, [], [], [], False) for status, path in statuses if path not in seen)
    return result


def _large_files(repo: pathlib.Path, head: str, paths: list[str], limit: int, timeout: float | None) -> list[Finding]:
    """large_file findings for `paths` at `head`: one `git cat-file --batch-check` for all of them. A failure
    (an object git cannot size) skips the size check for those files rather than failing the whole check."""
    paths = [p for p in paths if "\n" not in p and "\r" not in p]
    if not paths:
        return []
    payload = "".join(f"{head}:{p}\n" for p in paths).encode("utf-8", errors="replace")
    try:
        out = _run_git(repo, ["cat-file", "--batch-check=%(objecttype) %(objectsize)"], timeout, stdin=payload)
    except TamperCheckError:
        return []
    findings = []
    for path, line in zip(paths, out.decode("utf-8", errors="replace").splitlines()):
        parts = line.split()
        if len(parts) == 2 and parts[0] == "blob" and parts[1].isdigit() and int(parts[1]) > limit:
            findings.append(Finding("large_file", path, f"file is {int(parts[1])} bytes, over the {limit} byte limit"))
    return findings


def check_range(
    repo: pathlib.Path, base: str, head: str, *, allow_paths=(), gate_config_paths=(),
    max_file_bytes: int | None = 1_000_000, timeout: float | None = 60,
) -> list[Finding]:
    """The tamper check for a branch: analyze what `head` changed since its merge base with `base`. This is the
    diff Gate 1 has to judge (ASES-QG-02, ASES-QG-03, ASES-GIT-07, ASES-SEC-01).

    The range is `base...head`, three dots: the changes on `head` since the merge base, which is what
    review._check_scope computes (merge-base, then base..head), so this and the touches check look at the same
    change even if the integration branch has moved on. `base` may be the integration branch or an
    already-computed merge-base SHA; a merge-base is its own merge-base with head, so both give the same diff.
    --no-renames, as the touches check does, so a rename reads as a deletion plus an addition and the removed
    path is never hidden. The patch comes with the a/ b/ prefixes and quoting forced, so a user's git config
    (diff.noprefix, diff.mnemonicPrefix, core.quotepath, an external diff driver) cannot change what is parsed.

    Adds large_file findings for added or modified files over `max_file_bytes` (None turns that off).

    Raises TamperCheckError when git cannot answer (an unresolvable revision, a missing repository, a timeout,
    git not installed): a gate that could not run is never a silent pass, and what to do about it (send the
    card back, retry, halt) is the caller's decision. Everything else, including a diff full of findings,
    is a returned list; an empty list means the diff is clean."""
    base, head = _check_rev("base", base), _check_rev("head", head)
    span = f"{base}...{head}"
    patch = _run_git(repo, [
        "diff", "--no-renames", "--no-color", "--no-ext-diff", "--no-textconv", "-U3",
        "--src-prefix=a/", "--dst-prefix=b/", span, "--",
    ], timeout)
    names = _run_git(repo, ["diff", "--name-status", "--no-renames", "-z", span, "--"], timeout)
    statuses = _parse_name_status(names)

    files = _reconcile_status(parse_diff(patch.decode("utf-8", errors="replace")), statuses)
    findings = _analyze_files(files, _globs(allow_paths), _globs(gate_config_paths))
    if max_file_bytes:
        findings.extend(_large_files(repo, head, [p for s, p in statuses if s in ("A", "M")], max_file_bytes, timeout))
    return findings


# --- reporting -------------------------------------------------------------------------------------------------

_LINE_CAP = 240    # characters per finding line
_TOTAL_CAP = 3000  # characters for the whole report


def format_finding(finding: Finding) -> str:
    """One finding as one ASCII line: `kind path:line: detail` (`kind: detail` when it has no path). Anything
    that is not printable ASCII is backslash-escaped, and the line is clipped, because the text comes from file
    names and diffs and goes to a Windows console, a card comment and a log."""
    if finding.path:
        where = finding.path if finding.line is None else f"{finding.path}:{finding.line}"
        text = f"{finding.kind} {where}: {finding.detail}"
    else:
        text = f"{finding.kind}: {finding.detail}"
    text = _ascii(text)
    return text if len(text) <= _LINE_CAP else text[:_LINE_CAP - 3] + "..."


def format_findings(findings, *, limit: int = 20) -> str:
    """The findings as text for a card comment or a log: ASCII only, one line each (format_finding), at most
    `limit` of them and about 3000 characters in all, then "... and N more" for the rest. "" for no findings."""
    items = list(findings or [])
    if not items:
        return ""
    try:
        cap = max(int(limit), 0)
    except (TypeError, ValueError):
        cap = 20
    lines, used = [], 0
    for finding in items[:cap]:
        line = format_finding(finding)
        if used + len(line) + 1 > _TOTAL_CAP - 40:  # leave room for the "... and N more" line
            break
        lines.append(line)
        used += len(line) + 1
    if len(items) > len(lines):
        lines.append(f"... and {len(items) - len(lines)} more")
    return "\n".join(lines)


def blocking(findings) -> list[Finding]:
    """The findings that fail Gate 1: all of them (ASES-QG-03 lists no finding the gate may ignore) except an
    informational one that marks itself `blocks=False`. The coverage clause never produces one: coverage_check
    returns a finding only for a drop beyond the tolerance, and that finding blocks."""
    return [f for f in findings or [] if f.blocks]


def coverage_check(before: float | None, after: float | None, tolerance_points: float = 1.0) -> Finding | None:
    """ASES-QG-03's coverage clause ("lowers coverage of the changed area beyond the configured tolerance"), as
    a pure numeric comparison of two percentages. None when the drop is inside the tolerance (a drop of exactly
    `tolerance_points` is inside it), when coverage rose, and when either number is None or not a finite
    number: no measurement is no verdict, and the caller decides whether a missing measurement matters."""
    try:
        was, now, tolerance = float(before), float(after), float(tolerance_points)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(was) and math.isfinite(now)):
        return None
    tolerance = max(tolerance, 0.0)
    drop = was - now
    if drop <= tolerance + 1e-9:
        return None
    return Finding(
        "coverage_lowered", "",
        f"coverage fell from {was:.1f}% to {now:.1f}% ({drop:.1f} points, tolerance {tolerance:.1f})",
    )
