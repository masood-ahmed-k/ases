"""Gates 4 and 5 as controller lifecycle operations, and the release report (section 14.1 table 24, section 18.2;
ASES-TSK-04, ASES-CTL-01).

ASES-TSK-04: "Final integration security and smoke gates are controller lifecycle operations, not a worker role or
task assignee." Section 18.2 says where they sit: "After T9, T10 and T11 are merged: controller runs Gate 4, then
Gate 5, then writes the final report." This module is that finalization step. finalize() is what the polling loop
calls once every merge card is done; it does nothing else than the three things the sentence names, in that order,
and it records each on the exact integration HEAD (ASES-QG-01: the controller believes only its own gate records).

  * Gate 4, security (table 24: "Secrets scan, dependency audit, auth checks, obvious injection paths"). A built-in
    scan (scan_tree) reads the tracked tree straight out of git objects: tracked secret files, tracked generated
    artifacts, secret-shaped values (the one pattern set of events.py, through tamper.secret_hint) and a small fixed
    list of injection heuristics. The plan's own `gate4` profile (pip-audit, npm audit, an auth test) runs on top
    of it through gates.run_gate, so the sandbox `runner` hook applies to it (ASES-QG-04, ASES-SEC-03). The scan
    executes nothing from the repository, so it needs no sandbox.
  * Gate 5, smoke (table 24: "Start the app, hit the health endpoint, run a representative user flow"). The plan's
    `gate5` profile, or, when the plan has none, every task gate profile again on the integration HEAD.
  * The release report (ASES-CTL-01: "A project is finished when every merge card is done, Gates 4 and 5 are green
    on the integration HEAD, and the release report is written"): release.md plus report.html and report.json,
    local files only (ASES-OBS-02), outside the repository so a report never dirties the primary checkout.

Not built here, on purpose: a dependency audit and an app smoke run are the plan's commands (the Lead writes them
into the `gate4` and `gate5` profiles), not code in this module. The built-in scan is a set of regular expressions
and heuristics, not a static analyser, and it finds provider-shaped secrets (a value of the shape events.py
recognises), never an arbitrary password.

Nothing here calls a provider, and no value found by the scan is ever copied into a finding, an event, a report or
a question (ASES-SEC-01, ASES-GIT-07): a finding names the file, the line and the KIND of thing it looks like.
"""
from __future__ import annotations

import collections
import dataclasses
import fnmatch
import pathlib
import re
import sqlite3
import subprocess
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone

from . import bounds
from . import events
from . import gates
from . import hermes as hermes_mod
from . import intents
from . import ledger
from . import report as report_mod
from . import tamper

GATE4 = "gate4"
GATE5 = "gate5"
GATE_LABELS = {GATE4: "Gate 4 (security)", GATE5: "Gate 5 (smoke)"}
# The pseudo task key the two final gates are recorded under (bounds.record_final_gate, report.FINAL_GATE_KEY).
FINAL_TASK_KEY = bounds.FINAL_TASK_KEY

# finalize() statuses. `not_ready` is "nothing to finalize yet, ask again later"; `error` is "something outside the
# gates went wrong (git, an infrastructure failure, the report), no gate result was recorded for it".
STATUS_FINISHED = "finished"
STATUS_GATE_FAILED = "gate_failed"
STATUS_NOT_READY = "not_ready"
STATUS_ERROR = "error"

# The intent kind of the release report (intents.py: the vocabulary is open, reconcile treats an unknown kind as an
# action that spans the whole plan). The gates use intents.KIND_RUN_GATE.
KIND_RELEASE_REPORT = "write_release_report"

# Findings of the tree scan.
KIND_SECRET_IN_TREE = "secret_in_tree"
KIND_SECRET_FILE = "secret_file_tracked"
KIND_ARTIFACT = "generated_artifact_tracked"
KIND_SCAN_ERROR = "scan_error"
KIND_INJECTION = "injection_pattern"
KIND_SKIPPED = "skipped"

SEVERITY_BLOCKING = "blocking"
SEVERITY_ADVISORY = "advisory"
SEVERITY_INFO = "info"
_ADVISORY_KINDS = frozenset({KIND_INJECTION})
_INFO_KINDS = frozenset({KIND_SKIPPED})

# ASES-TSK-04 (section 18.2): a finding the plan's gate4_allowlist excused (see _apply_allowlist). Every
# original blocking kind (secret_in_tree, secret_file_tracked, generated_artifact_tracked) gets this prefix
# rather than one fixed new kind, so the release report still says WHAT was found, only that it was allowed.
# scan_error is never rewritten this way (see _apply_allowlist), so no "allowed_scan_error" kind exists.
ALLOWLIST_PREFIX = "allowed_"

# "scan the text of every tracked text file up to 1 MB": a file of exactly this size is scanned, one byte more is not.
MAX_SCAN_BYTES = 1_000_000
_BINARY_PROBE_BYTES = 8000      # git's own rule for "binary": a NUL byte in the first 8000 bytes
_BATCH_BYTES = 32 * 1024 * 1024  # blobs read from one `git cat-file --batch` call, so a big tree never sits in memory
_LINE_RULE_CHARS = 2000         # the injection heuristics look at the first 2000 characters of a line (minified code)
_DETAIL_CAP = 20_000            # characters of one gate record: command output can be enormous
_LIST_LIMIT = 50                # findings listed in the release summary
_QUESTION_CAP = 1500

_BACKSLASH = chr(92)


# ---------------------------------------------------------------------------------------------
# Small text helpers: everything a person reads is ASCII, single-line where it is a cell, and redacted.
# ---------------------------------------------------------------------------------------------


def _ascii(value) -> str:
    """`value` as text a Windows console can print (cp1252 crashes on an arrow or an accented letter in a path):
    every control and non-ASCII character becomes a backslash escape, never dropped, and a line break becomes a
    space so a cell stays one line. Already ASCII text passes through unchanged."""
    text = "" if value is None else str(value)
    out = []
    for char in text:
        code = ord(char)
        if char in "\r\n\t":
            out.append(" ")
        elif 32 <= code < 127:
            out.append(char)
        elif code < 256:
            out.append(f"{_BACKSLASH}x{code:02x}")
        elif code < 65536:
            out.append(f"{_BACKSLASH}u{code:04x}")
        else:
            out.append(f"{_BACKSLASH}U{code:08x}")
    return "".join(out)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(limit - 3, 0)] + "..."


def _safe(value, limit: int = 300) -> str:
    """One short ASCII line with secret-shaped values redacted (ASES-SEC-01): for exception text, a git error, a
    path. The redaction runs first, so a value is replaced whole before the text is cut."""
    return _clip(_ascii(events.redact_text(str(value))), limit)


def _err(exc: BaseException) -> str:
    return _safe(f"{type(exc).__name__}: {exc}")


def _cap_detail(text: str, limit: int = _DETAIL_CAP) -> str:
    """A gate record's text cut to `limit` characters, keeping the start and (more of) the end: the end of a
    command's output is where its failure is."""
    if len(text) <= limit:
        return text
    head = limit // 5
    tail = limit - head
    return f"{text[:head]}\n... [{len(text) - limit} characters omitted] ...\n{text[-tail:]}"


def _moment(now) -> datetime:
    """`now` as an aware UTC datetime: None is the current time, a naive datetime is read as UTC (every timestamp
    ASES writes is UTC), a number is epoch seconds."""
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, datetime):
        return now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
    if isinstance(now, (int, float)) and not isinstance(now, bool):
        return datetime.fromtimestamp(now, tz=timezone.utc)
    raise ValueError(f"now must be a datetime, epoch seconds or None, got {_safe(repr(now), 80)}")


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


# ---------------------------------------------------------------------------------------------
# Findings of the tree scan
# ---------------------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class TreeFinding:
    """One thing the Gate 4 tree scan reports. `path` is the file it is about (the generated directory, for a
    tracked generated tree; "" for a finding about the scan itself), `line` is the 1-based line in that file (None
    for a whole-file finding). `detail` says what KIND of thing was found and never contains the matched text: a
    finding goes to a card, a log, a report and a model provider (ASES-SEC-01)."""
    kind: str
    path: str
    line: int | None
    detail: str


def severity(finding_or_kind) -> str:
    """"blocking", "advisory" or "info" for a finding (or a kind). Only secret_in_tree, secret_file_tracked,
    generated_artifact_tracked and scan_error are named blocking by the work order, but the rule is fail closed:
    an injection_pattern is advisory, a `skipped` note is information, and ANY other kind blocks, so a kind nobody
    classified can never let a gate pass by being unknown. A kind carrying ALLOWLIST_PREFIX (ASES-TSK-04: a
    finding the plan's gate4_allowlist excused) is information: a human already reviewed and excused it, so it
    is listed for audit but never fails the gate."""
    kind = finding_or_kind if isinstance(finding_or_kind, str) else getattr(finding_or_kind, "kind", "")
    if kind.startswith(ALLOWLIST_PREFIX):
        return SEVERITY_INFO
    if kind in _ADVISORY_KINDS:
        return SEVERITY_ADVISORY
    if kind in _INFO_KINDS:
        return SEVERITY_INFO
    return SEVERITY_BLOCKING


def blocking(findings: Iterable) -> list:
    """The findings that fail Gate 4 (ASES-TSK-04): secrets in the tree, tracked secret files, tracked generated
    artifacts, and a scan that could not run (a gate that could not run is never a silent pass)."""
    return [finding for finding in findings or () if severity(finding) == SEVERITY_BLOCKING]


def advisory(findings: Iterable) -> list:
    """The findings that are listed in the release report and do not fail the gate (the injection heuristics)."""
    return [finding for finding in findings or () if severity(finding) == SEVERITY_ADVISORY]


def format_finding(finding: TreeFinding) -> str:
    """One finding as one ASCII line, `kind path:line: detail`, through tamper.format_finding so a Gate 4 line
    reads exactly like a Gate 1 line. The path is redacted first: a file NAME can itself be secret-shaped."""
    return tamper.format_finding(tamper.Finding(
        finding.kind, events.redact_text(finding.path or ""), events.redact_text(finding.detail), finding.line,
    ))


def _finding_counts(findings: Iterable) -> dict:
    """{kind: how many}, in the order the kinds first appear. Counts only: a count carries no value."""
    return dict(collections.Counter(finding.kind for finding in findings))


# --- the file name checks -------------------------------------------------------------------------------------

# Generated artifacts (ASES-GIT-07: "Keep generated artifacts and secrets out of commits"). tamper.py keeps its own
# lists private, so these mirror its unambiguous ones. A name that is often a real source directory (target, out,
# bin) is left out on purpose: this finding blocks the gate, and a wrong block costs a human decision.
_ARTIFACT_DIRS = frozenset({
    "__pycache__", "node_modules", "dist", "build", ".venv", "venv", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "htmlcov",
})
_ARTIFACT_SUFFIXES = (".pyc", ".pyo")
_ARTIFACT_NAMES = frozenset({".ds_store"})
_SECRET_NAME_SUFFIXES = (".kdbx",)
_SECRET_NAMES = frozenset({"credentials.json"})


def _is_secret_file_name(path: str) -> bool:
    """A tracked file whose NAME says it holds secrets: everything tamper.is_secret_file knows (.env and .env.*
    except the .example and .sample templates, *.pem, *.key, id_rsa*, id_ed25519*, *.p12, *.pfx) plus *.kdbx (a
    password database) and credentials.json. A name check only: it never looks at content."""
    if tamper.is_secret_file(path):
        return True
    name = path.rsplit("/", 1)[-1].lower()
    return name in _SECRET_NAMES or name.endswith(_SECRET_NAME_SUFFIXES)


def _artifact_dir(path: str) -> str | None:
    """The path of the first generated directory (node_modules, __pycache__, dist, build, .venv, *.egg-info ...)
    that a tracked file sits inside, else None. Only directory components count, never the file's own name, so a
    script called `build` or `dist.py` is not an artifact."""
    parts = path.split("/")
    for index, part in enumerate(parts[:-1]):
        lower = part.lower()
        if lower in _ARTIFACT_DIRS or lower.endswith(".egg-info"):
            return "/".join(parts[: index + 1])
    return None


def _is_artifact_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    return name.endswith(_ARTIFACT_SUFFIXES) or name in _ARTIFACT_NAMES


# --- the injection heuristics ---------------------------------------------------------------------------------
#
# "Obvious injection paths" (table 24) as a SMALL FIXED list of regular expressions, one line at a time. They are
# heuristics: they read a line, not a program, so a call split over several lines is missed and a harmless line that
# has the shape is reported. That is why every one is ADVISORY (listed in the release report, never failing the gate).
# A rule is tried only when one of its `keys` (literals that every match must contain) is in the line, which keeps a
# scan of a big tree fast, and it never fires when its `unless` pattern matches the line too.

# A command or query string that is BUILT rather than written: an f-string, "..." % x, "..." + x or "...".format(x).
_BUILT = r"""(?:(?:[rR][fF]|[fF][rR]?)["']|["'][^"']*["']\s*(?:%|\+|\.format\())"""

_Rule = collections.namedtuple("_Rule", "label keys regex unless")

_PY_RULES = (
    _Rule("subprocess call with shell=True and a built command", ("subprocess",),
          re.compile(r"\bsubprocess\.\w+\((?=[^\n]*\bshell\s*=\s*True\b)\s*" + _BUILT), None),
    _Rule("os.system call with a built command", ("os.system",),
          re.compile(r"\bos\.system\(\s*" + _BUILT), None),
    # A literal argument is not the problem: eval("1+1") and exec("import x") name their code in the source. The
    # lookbehind keeps model.eval(), session.exec(query) and ast.literal_eval(text) out.
    _Rule("eval or exec of a non-literal", ("eval(", "exec("),
          re.compile(r"""(?<![\w.])(?:eval|exec)\(\s*(?!["'\d)\s])"""), None),
    _Rule("pickle load of data", ("pickle.load",), re.compile(r"\bpickle\.loads?\("), None),
    _Rule("yaml.load without SafeLoader", ("yaml.load",), re.compile(r"\byaml\.load\("), re.compile(r"SafeLoader")),
    _Rule("SQL built by string formatting passed to execute", ("execute",),
          re.compile(r"\.execute(?:many|script)?\(\s*" + _BUILT), None),
)

# In JavaScript and TypeScript a template literal WITH an interpolation is the injectable shape; a template literal
# with none is a plain string and is not reported.
_JS_RULES = (
    _Rule("eval call", ("eval(",), re.compile(r"(?<![\w.$])eval\("), None),
    _Rule("new Function from a string", ("Function",), re.compile(r"\bnew\s+Function\s*\("), None),
    _Rule("child_process exec with a template literal", ("exec",),
          re.compile(r"\bexec(?:Sync)?\(\s*`[^`]*\$\{"), None),
    _Rule("innerHTML assigned a template literal", ("innerHTML",),
          re.compile(r"\binnerHTML\s*\+?=\s*`[^`]*\$\{"), None),
)

_PY_EXTENSIONS = (".py", ".pyw")
_JS_EXTENSIONS = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx")


def _rules_for(path: str) -> tuple[tuple, tuple[str, ...]]:
    """(rules, comment prefixes) for a file, chosen by its extension; no rules for anything else."""
    lower = path.lower()
    if lower.endswith(_PY_EXTENSIONS):
        return _PY_RULES, ("#",)
    if lower.endswith(_JS_EXTENSIONS):
        return _JS_RULES, ("//", "/*", "*")
    return (), ()


def _injection_label(line: str, rules: tuple, comments: tuple[str, ...]) -> str | None:
    """The label of the first rule that fires on `line`, or None. A line that is only a comment is skipped."""
    stripped = line.lstrip()
    if not stripped or stripped.startswith(comments):
        return None
    line = line[:_LINE_RULE_CHARS]
    for rule in rules:
        if (any(key in line for key in rule.keys) and rule.regex.search(line)
                and (rule.unless is None or not rule.unless.search(line))):
            return rule.label
    return None


def scan_text(path: str, text: str) -> list[TreeFinding]:
    """The findings for the text of ONE tracked file: a secret-shaped value on a line (blocking, kind
    secret_in_tree, from tamper.secret_hint, which is events.py's pattern set, the one place that defines what a
    provider key looks like) and, for Python and JavaScript files, the injection heuristics (advisory, kind
    injection_pattern). Lines are split on the newline only, so a number is the line an editor shows. The text of
    a matched line is never put into a finding, only its number and the kind of thing it looks like."""
    findings: list[TreeFinding] = []
    # One pass over the whole text first: nearly every file holds no secret, and then the per-line pass is skipped.
    has_secret = tamper.secret_hint(text) is not None
    rules, comments = _rules_for(path)
    if not has_secret and not rules:
        return findings
    for number, raw in enumerate(text.split("\n"), start=1):
        line = raw[:-1] if raw.endswith("\r") else raw
        if has_secret:
            hint = tamper.secret_hint(line)
            if hint is not None:
                findings.append(TreeFinding(KIND_SECRET_IN_TREE, path, number, f"secret-shaped value ({hint})"))
        if rules:
            label = _injection_label(line, rules, comments)
            if label is not None:
                findings.append(TreeFinding(KIND_INJECTION, path, number, f"{label} (heuristic)"))
    return findings


# --- reading the tracked tree out of git ----------------------------------------------------------------------

_GIT = ("git", "--no-optional-locks", "-c", "core.quotepath=false")


class _GitFailure(Exception):
    """git could not answer (a missing repository, an unknown ref, a timeout, git not installed). Turned into a
    `scan_error` finding by scan_tree, so a scan that could not run is never mistaken for a clean tree."""


def _git(repo: pathlib.Path, args: list[str], timeout: float, *, stdin: bytes | None = None) -> bytes:
    """One read-only git command, its stdout as bytes. --no-optional-locks so the scan can never take a lock away
    from the merge queue. Anything but a clean answer is a _GitFailure carrying one redacted line."""
    kwargs = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
    try:
        proc = subprocess.run([*_GIT, "-C", str(repo), *args], capture_output=True, timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired as exc:
        raise _GitFailure(f"git {args[0]} timed out after {timeout}s") from exc
    except (OSError, ValueError) as exc:
        raise _GitFailure(f"git could not be run: {_safe(exc, 160)}") from exc
    if proc.returncode != 0:
        lines = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()
        raise _GitFailure(f"git {args[0]} failed: " + (_safe(lines[0], 200) if lines else f"exit {proc.returncode}"))
    return proc.stdout


def _check_ref(ref) -> str:
    """A revision that is safe to hand to git: a value starting with '-' would be read as an option, and
    whitespace or a NUL cannot be part of a revision."""
    if not isinstance(ref, str) or not ref or ref.startswith("-") or any(c in ref for c in " \t\r\n\0"):
        raise _GitFailure(f"unusable revision {_safe(repr(ref), 80)}")
    return ref


def _parse_listing(raw: bytes) -> list[tuple[str, str, str, int | None, str]]:
    """[(mode, type, object id, size or None, path)] from `git ls-tree -r -z -l`: NUL-terminated entries, each
    `<mode> <type> <object> <size, padded>` TAB `<path>`. A submodule is type `commit` with size `-`. The path is
    git's own bytes decoded as UTF-8 (an undecodable byte becomes U+FFFD: the path is only ever displayed)."""
    entries = []
    for chunk in raw.split(b"\0"):
        if not chunk:
            continue
        meta, _, name = chunk.partition(b"\t")
        fields = meta.decode("ascii", errors="replace").split()
        if len(fields) != 4:
            raise _GitFailure("unexpected output from git ls-tree")
        mode, kind, oid, size = fields
        entries.append((
            mode, kind, oid, int(size) if size.isdigit() else None, name.decode("utf-8", errors="replace"),
        ))
    return entries


def _read_blobs(repo: pathlib.Path, oids: list[str], timeout: float) -> dict[str, bytes]:
    """{object id: content} for the blobs `oids`, from ONE `git cat-file --batch` call. The records come back as
    `<oid> <type> <size>` LF content LF; an object git does not have is `<oid> missing` and is left out."""
    out = _git(repo, ["cat-file", "--batch"], timeout, stdin="".join(f"{oid}\n" for oid in oids).encode("ascii"))
    blobs: dict[str, bytes] = {}
    position = 0
    while position < len(out):
        end = out.find(b"\n", position)
        if end < 0:
            break
        header = out[position:end].split()
        position = end + 1
        if len(header) == 2 and header[1] == b"missing":
            continue
        if len(header) != 3 or not header[2].isdigit():
            raise _GitFailure("unexpected output from git cat-file")
        size = int(header[2])
        blobs[header[0].decode("ascii", errors="replace")] = out[position: position + size]
        position += size + 1
    return blobs


def _scan_tree(repo: pathlib.Path, ref, timeout: float, max_file_bytes: int) -> list[TreeFinding]:
    rev = _check_ref(ref)
    entries = _parse_listing(_git(repo, ["ls-tree", "-r", "-z", "-l", rev], timeout))
    findings: list[TreeFinding] = []
    artifact_files: dict[str, int] = {}     # generated directory -> tracked files inside it
    sizes: dict[str, int] = {}              # object id -> size, for the blobs whose text is scanned
    paths_of: dict[str, list[str]] = {}     # object id -> the tracked paths that hold it

    for mode, kind, oid, size, path in entries:
        safe_path = events.redact_text(path)
        if _is_secret_file_name(path):
            findings.append(TreeFinding(
                KIND_SECRET_FILE, safe_path, None, "a tracked file whose name marks it as holding secrets",
            ))
        directory = _artifact_dir(path)
        if directory is not None:
            artifact_files[directory] = artifact_files.get(directory, 0) + 1
            continue                          # a tracked generated tree is one finding, and its text is not read
        if _is_artifact_file(path):
            findings.append(TreeFinding(KIND_ARTIFACT, safe_path, None, "a tracked generated or local-state file"))
            continue
        if kind != "blob" or mode == "120000" or size is None or size == 0:
            continue                          # a submodule, a symbolic link (its content is a path) or an empty file
        if size > max_file_bytes:
            findings.append(TreeFinding(
                KIND_SKIPPED, safe_path, None, f"not scanned: {size} bytes is over the {max_file_bytes} byte limit",
            ))
            continue
        sizes[oid] = size
        paths_of.setdefault(oid, []).append(path)

    for directory, count in artifact_files.items():
        findings.append(TreeFinding(
            KIND_ARTIFACT, events.redact_text(directory), None,
            f"{count} tracked file(s) inside a generated directory",
        ))

    for oids in _batches(sizes):
        blobs = _read_blobs(repo, oids, timeout)
        for oid in oids:
            data = blobs.get(oid)
            if data is None:
                for path in paths_of[oid]:
                    findings.append(TreeFinding(
                        KIND_SCAN_ERROR, events.redact_text(path), None,
                        "the file's content could not be read from git",
                    ))
                continue
            if b"\0" in data[:_BINARY_PROBE_BYTES]:
                continue                      # binary: there is no text to scan
            text = data.decode("utf-8", errors="replace")
            for path in paths_of[oid]:
                findings.extend(
                    dataclasses.replace(finding, path=events.redact_text(finding.path))
                    for finding in scan_text(path, text)
                )
    findings.sort(key=lambda f: (f.path, 0 if f.line is None else f.line, f.kind))
    return findings


def _batches(sizes: Mapping[str, int]) -> Iterable[list[str]]:
    """The object ids grouped so that no group holds more than _BATCH_BYTES of blob content (one group at least)."""
    group: list[str] = []
    total = 0
    for oid, size in sizes.items():
        if group and total + size > _BATCH_BYTES:
            yield group
            group, total = [], 0
        group.append(oid)
        total += size
    if group:
        yield group


def scan_tree(
    repo, ref, *, timeout: float = 120, max_file_bytes: int = MAX_SCAN_BYTES,
) -> list[TreeFinding]:
    """Gate 4's built-in scan (table 24: "Secrets scan ... obvious injection paths"; ASES-SEC-01, ASES-GIT-07):
    the tracked tree at `ref` read out of git objects (`git ls-tree -r -z -l`, then `git cat-file --batch`, so no
    checkout and no process per file), never a worker's live directory.

    Reports, each with a path and, where it has one, a line, and NEVER the matched text:
      secret_file_tracked        a tracked file named .env or .env.* (not .env.example, .env.sample), *.pem, *.key,
                                 id_rsa*, id_ed25519*, *.p12, *.pfx, *.kdbx or credentials.json     (blocking)
      generated_artifact_tracked a tracked __pycache__, node_modules, dist, build, .venv, venv, *.egg-info ... tree
                                 (one finding per tree, with a count) or a stray *.pyc, *.pyo, .DS_Store  (blocking)
      secret_in_tree             a line of a tracked text file with a secret-shaped value in it        (blocking)
      injection_pattern          the fixed heuristics of scan_text                                      (advisory)
      skipped                    a file over `max_file_bytes` (default 1 MB), which is not scanned        (a note)
      scan_error                 git could not answer, or an object could not be read: the scan did not
                                 run, which is never a clean tree                                        (blocking)
    A binary file (a NUL byte in its first 8000 bytes) is skipped without a note; a generated tree's files, a
    symbolic link and a submodule are not read. `timeout` is the limit of each git call. This function never
    raises: whatever goes wrong becomes one `scan_error` finding. Findings come sorted by path and line."""
    try:
        return _scan_tree(pathlib.Path(repo), ref, timeout, max_file_bytes)
    except _GitFailure as exc:
        return [TreeFinding(KIND_SCAN_ERROR, "", None, _safe(exc))]
    except Exception as exc:  # noqa: BLE001 - the contract is "never raises": a bug must fail the gate, not crash it
        return [TreeFinding(KIND_SCAN_ERROR, "", None, _err(exc))]


# ---------------------------------------------------------------------------------------------
# The gates
# ---------------------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class GateOutcome:
    """The result of one final gate on one commit. `detail` is the recorded text (scan summary and command output,
    redacted and capped); `findings` are the TreeFindings of Gate 4 (empty for Gate 5); `notes` are plain lines the
    release report shows, such as "no gate4 profile in the plan: built-in scan only"."""
    gate: str
    commit_sha: str
    passed: bool
    detail: str
    findings: tuple = ()
    notes: tuple = ()


@dataclasses.dataclass(frozen=True)
class FinalizeResult:
    """What finalize() did. `status` is finished, gate_failed, not_ready or error; `gate4` and `gate5` are the
    outcomes that exist (a gate that was not reached is None); `report_path` is release.md once it is written;
    `reason` is one plain line saying why, for a log, a card or the console."""
    status: str
    gate4: GateOutcome | None = None
    gate5: GateOutcome | None = None
    report_path: pathlib.Path | None = None
    reason: str = ""


def _profile_commands(plan, name: str) -> list[str]:
    """The commands of the plan's gate profile `name` (blank and non-text entries dropped), [] when it has none."""
    profiles = getattr(plan, "gate_profiles", None)
    commands = profiles.get(name) if isinstance(profiles, Mapping) else None
    if not isinstance(commands, (list, tuple)):
        return []
    return [command for command in commands if isinstance(command, str) and command.strip()]


def _every_task_command(plan) -> list[str]:
    """Every DISTINCT command of every task gate profile of the plan, in the order it first appears (profiles in
    plan order, commands in list order). The `gate4` profile is not a task profile and is left out: the security
    audit already ran as Gate 4, and re-running it would make a smoke gate out of a dependency audit."""
    profiles = getattr(plan, "gate_profiles", None)
    if not isinstance(profiles, Mapping):
        return []
    seen: set[str] = set()
    ordered: list[str] = []
    for name in profiles:
        if name in (GATE4, GATE5):
            continue
        for command in _profile_commands(plan, name):
            if command.strip() not in seen:
                seen.add(command.strip())
                ordered.append(command)
    return ordered


def _finding_lines(findings: list, limit: int = 30) -> list[str]:
    lines = [f"  {format_finding(finding)}" for finding in findings[:limit]]
    if len(findings) > limit:
        lines.append(f"  ... and {len(findings) - limit} more")
    return lines


def _record(conn, plan, gate: str, head: str, passed: bool, parts: list[str], findings, notes) -> GateOutcome:
    """The one place a final gate result is written: ONE row in gate_runs under the "__final__" task key, through
    bounds.record_final_gate (which redacts the detail and adds the `final_gate_recorded` event). The plan's commands
    run through run_gate WITHOUT a connection, so gates.run_gate writes no row of its own: with both there would be
    two rows for one gate run, and a row that holds only the command output and not the scan. The row is the
    COMBINED result. Returns the outcome with the same (redacted, capped) detail."""
    detail = _cap_detail(events.redact_text("\n".join(part for part in parts if part)))
    bounds.record_final_gate(conn, plan.project, gate, head, passed, detail=detail)
    return GateOutcome(gate, head, passed, detail, tuple(findings), tuple(notes))


def _run_scan(scan: Callable, repo, head: str) -> list:
    """The scan's findings, or one blocking scan_error when a (possibly injected) scan raised."""
    try:
        return list(scan(repo, head))
    except Exception as exc:  # noqa: BLE001 - a scan that crashed is a scan that did not run
        return [TreeFinding(KIND_SCAN_ERROR, "", None, f"the tree scan raised {_err(exc)}")]


def _path_matches_any(path: str, globs: tuple) -> bool:
    """Same glob semantics as everywhere else in ASES (tamper._glob_match, plan.py's touches check): fnmatch on
    forward-slash paths, plus an exact-string fast path, so * and ** both cross directories."""
    return bool(path) and any(path == glob or fnmatch.fnmatch(path, glob) for glob in globs)


def _apply_allowlist(findings: list, allow_paths) -> list:
    """ASES-TSK-04 (section 18.2): `findings` with every currently BLOCKING one whose `path` an `allow_paths`
    glob covers turned into its ALLOWLIST_PREFIX kind (see severity()): still in the list for a human to audit,
    never blocking. Only a finding severity() already calls blocking is rewritten: an advisory injection_pattern
    or an informational skipped note under the same path is left exactly as it is, because there was nothing on
    it to excuse. scan_error is never allowlisted either way (a scan that could not run is not a content
    decision a plan can excuse, and its path is often "" anyway, which no glob can name). Findings that do not
    match are returned unchanged, in place, so callers keep one list either way."""
    globs = tuple(g for g in (allow_paths or ()) if isinstance(g, str) and g)
    if not globs:
        return findings
    rewritten = []
    for finding in findings:
        if (finding.kind != KIND_SCAN_ERROR and severity(finding) == SEVERITY_BLOCKING
                and finding.path and _path_matches_any(finding.path, globs)):
            rewritten.append(dataclasses.replace(
                finding, kind=f"{ALLOWLIST_PREFIX}{finding.kind}",
                detail=f"{finding.detail} (allowlisted by the plan's gate4_allowlist)",
            ))
        else:
            rewritten.append(finding)
    return rewritten


def run_gate4(
    repo, plan, conn, head: str, *, runner=None, scan=None, run_gate=None, timeout_per_command: int = 300,
    allow_paths=(),
) -> GateOutcome:
    """Gate 4, security, on the integration HEAD `head` (table 24; ASES-TSK-04, ASES-QG-01, ASES-QG-04, ASES-SEC-01).

    The built-in scan comes first (`scan`, default scan_tree). Blocking findings fail the gate WITHOUT running the
    plan's commands, and the detail says so: a tree with a tracked secret is not made better by an audit tool. When
    the scan is clean, the plan's `gate4` profile (a dependency audit such as pip-audit or npm audit, an auth test)
    runs on `head` in a clean throwaway worktree through `run_gate` (default gates.run_gate), so `runner` (the
    sandbox hook) applies to it. A plan with no gate4 profile is decided by the scan alone and says so in a note
    ("no gate4 profile in the plan: built-in scan only"): a note, not a failure. The advisory findings (the
    injection heuristics) are kept in the outcome for the release report and never fail the gate.

    ONE combined row is recorded, always through bounds.record_final_gate (see _record): the result is the scan
    AND the commands. A `scan` that raises is a scan that did not run, so it is a blocking scan_error and the gate
    is red (fail closed). A `run_gate` that raises is NOT caught: an infrastructure failure (Docker down, git
    failing) is not a red gate, so it propagates and no row is written (finalize turns it into status "error").

    `allow_paths` (ASES-TSK-04, section 18.2: normally `plan.gate4_allowlist`) are path globs whose findings are
    dropped from the blocking set before it is decided: a known, reviewed exception such as a sample key in
    tests/ or docs/. An allowlisted finding is never silently invisible, it is still in the returned outcome's
    findings and in the detail, renamed to its ALLOWLIST_PREFIX kind (see _apply_allowlist and severity()), so
    the release report shows exactly what was excused. Defaults to `()`: a caller that does not pass it sees
    exactly today's behaviour."""
    scan = scan or scan_tree
    run_gate = run_gate or gates.run_gate
    findings = _apply_allowlist(_run_scan(scan, repo, head), allow_paths)
    blockers = blocking(findings)
    notes: list[str] = []
    skipped = [f for f in findings if f.kind == KIND_SKIPPED]
    if skipped:
        notes.append(f"{len(skipped)} file(s) over the scan size limit were not scanned")
    counts = _finding_counts(findings)
    parts = [
        "built-in scan of the tracked tree: "
        f"{len(blockers)} blocking finding(s), {len(advisory(findings))} advisory, {len(skipped)} file(s) not scanned"
        + (f" ({', '.join(f'{kind} {count}' for kind, count in counts.items())})" if counts else ""),
        *_finding_lines(blockers),
    ]
    commands = _profile_commands(plan, GATE4)

    if blockers:
        notes.append("the built-in scan found blocking findings: the plan's gate4 commands were not run")
        parts.append("the plan's gate4 commands were NOT run because the built-in scan failed")
        passed = False
    elif commands:
        result = run_gate(
            repo, head, GATE4, commands, conn=None, task_key=FINAL_TASK_KEY,
            timeout_per_command=timeout_per_command, runner=runner,
        )
        passed = bool(result.passed)
        parts += [f"the plan's gate4 commands: {'pass' if passed else 'fail'}", str(result.detail)]
    else:
        notes.append("no gate4 profile in the plan: built-in scan only")
        parts.append("no gate4 profile in the plan: built-in scan only")
        passed = True
    return _record(conn, plan, GATE4, head, passed, parts, findings, notes)


def run_gate5(
    repo, plan, conn, head: str, *, runner=None, run_gate=None, timeout_per_command: int = 300,
) -> GateOutcome:
    """Gate 5, smoke, on the integration HEAD `head` (table 24: "Start the app, hit the health endpoint, run a
    representative user flow"; ASES-TSK-04, ASES-QG-01, ASES-QG-04).

    The plan's `gate5` profile runs when it has one: the Lead writes the command that starts the app and probes
    its health endpoint, and this module does not guess one. With no gate5 profile the smoke fallback is every
    DISTINCT command of every task gate profile, in the order they first appear, run on the integration HEAD (for a
    library that is the full test suite), with the note "no gate5 profile in the plan: ran every task gate profile
    on the integration HEAD". The `gate4` profile is not part of the fallback (see _every_task_command). A plan
    with nothing to run FAILS the gate with a clear message: nothing to run is not a pass. The commands run through
    `run_gate` (default gates.run_gate) so `runner`, the sandbox hook, applies. ONE row is recorded through
    bounds.record_final_gate; an exception from `run_gate` propagates and records nothing."""
    run_gate = run_gate or gates.run_gate
    notes: list[str] = []
    commands = _profile_commands(plan, GATE5)
    if commands:
        parts = [f"the plan's gate5 profile: {len(commands)} command(s)"]
    else:
        commands = _every_task_command(plan)
        if not commands:
            message = ("the plan has no gate5 profile and no task gate profile, so there is nothing to run: "
                       "a smoke gate with nothing to run is not a pass")
            return _record(conn, plan, GATE5, head, False, [message], (), [message])
        note = "no gate5 profile in the plan: ran every task gate profile on the integration HEAD"
        notes.append(note)
        parts = [note, f"{len(commands)} distinct command(s)"]
    result = run_gate(
        repo, head, GATE5, commands, conn=None, task_key=FINAL_TASK_KEY,
        timeout_per_command=timeout_per_command, runner=runner,
    )
    passed = bool(result.passed)
    parts += [f"the commands: {'pass' if passed else 'fail'}", str(result.detail)]
    return _record(conn, plan, GATE5, head, passed, parts, (), notes)


def final_gate_question(outcome) -> str:
    """The question a failed final gate puts to the human (the controller pauses the project with it): plain
    English, ASCII, at most ~1500 characters, naming the gate and the commit, the first few blocking findings (a
    kind, a file and a line, never a value) and what the person can do next, ending in a question.

    `outcome` is the failing GateOutcome; a FinalizeResult is accepted too and its first failing gate is used. A
    gate that failed on its plan commands has no findings, so the last lines of its recorded output are shown
    instead, redacted. The whole text goes through events.redact_text last, so nothing secret-shaped is in it."""
    target = _failing_outcome(outcome)
    if target is None:
        return _safe_question(
            "A final gate did not pass, so the project was not finished. How should this be resolved?"
        )
    # getattr with defaults: the controller's tests hand over stand-ins, and a question must still be asked.
    gate = str(getattr(target, "gate", "") or "")
    label = GATE_LABELS.get(gate, f"Final gate {gate}".rstrip())
    sha = _ascii(getattr(target, "commit_sha", "") or "")[:10]
    findings = getattr(target, "findings", None) or ()
    notes = getattr(target, "notes", None) or ()
    blockers = blocking(findings)
    where = f" at commit {sha}" if sha else ""
    lines = [f"{label} failed on the integration branch{where}, so the project was not finished."]
    if blockers:
        lines.append(f"{len(blockers)} blocking finding(s), the first {min(len(blockers), 5)}:")
        lines += [f"  {format_finding(finding)}" for finding in blockers[:5]]
        if len(blockers) > 5:
            lines.append(f"  ... and {len(blockers) - 5} more (they are in the gate record)")
        if any(finding.kind == KIND_SCAN_ERROR for finding in blockers):
            lines.append("A scan_error means the scan itself could not run (git or the repository is unhealthy), "
                         "not that a secret was found.")
    else:
        for note in list(notes)[:3]:
            lines.append(_clip(_safe(note), 200))
        tail = [line.strip() for line in str(getattr(target, "detail", "") or "").splitlines() if line.strip()][-4:]
        if tail:
            lines.append("The last lines of its output:")
            lines += [f"  {_safe(line, 160)}" for line in tail]
    lines += [
        "Next: fix the cause on the integration branch (remove or rotate a flagged secret and delete a flagged "
        "file, or fix the code or the plan's gate command that failed) and commit it there, then run `swarm resume`: "
        "the final gates run again on the new commit. A gate profile of the plan changes only through a new "
        "`swarm approve`.",
        "How should this be resolved?",
    ]
    return _safe_question("\n".join(lines))


def _safe_question(text: str) -> str:
    """The question text as ASCII with secret-shaped values redacted, cut to _QUESTION_CAP characters (the end, the
    question itself, is always kept)."""
    clean = events.redact_text(text)
    clean = "\n".join(_ascii(line) for line in clean.split("\n"))
    if len(clean) <= _QUESTION_CAP:
        return clean
    last = clean.rsplit("\n", 1)[-1]
    return clean[: _QUESTION_CAP - len(last) - 5].rstrip() + "\n...\n" + last


def _failing_outcome(outcome) -> GateOutcome | None:
    """The GateOutcome that failed: `outcome` itself when it did not pass, or the first failing gate of a
    FinalizeResult. None when nothing failed."""
    if hasattr(outcome, "passed"):
        return None if outcome.passed else outcome
    for name in ("gate4", "gate5"):
        candidate = getattr(outcome, name, None)
        if candidate is not None and not candidate.passed:
            return candidate
    return None


# ---------------------------------------------------------------------------------------------
# The release summary and the release report
# ---------------------------------------------------------------------------------------------


def _gate_summary(outcome: GateOutcome | None) -> dict:
    """One gate as plain data for the release report: status, notes, the count of findings of each kind, and the
    findings themselves (kind, file, line, what kind of thing: blocking ones first, then the advisory ones) up to
    _LIST_LIMIT. Never the recorded output, which is command text and can hold anything, and never a value."""
    if outcome is None:
        return {"status": "not run", "commit_sha": None, "notes": [], "finding_counts": {}, "blocking": 0,
                "advisory": 0, "findings": [], "findings_omitted": 0}
    rank = {SEVERITY_BLOCKING: 0, SEVERITY_ADVISORY: 1, SEVERITY_INFO: 2}
    ordered = sorted(outcome.findings, key=lambda finding: rank[severity(finding)])
    return {
        "status": "pass" if outcome.passed else "fail",
        "commit_sha": outcome.commit_sha,
        "notes": [str(note) for note in outcome.notes],
        "finding_counts": _finding_counts(outcome.findings),
        "blocking": len(blocking(outcome.findings)),
        "advisory": len(advisory(outcome.findings)),
        "findings": [
            {"kind": finding.kind, "path": finding.path, "line": finding.line, "detail": finding.detail}
            for finding in ordered[:_LIST_LIMIT]
        ],
        "findings_omitted": max(len(ordered) - _LIST_LIMIT, 0),
    }


def _task_summaries(conn: sqlite3.Connection, plan) -> list[dict]:
    """One entry per plan task: task key, title, role, work card id (the task's CURRENT work card, which follows a
    fix or retry card), merge card id, the squash commit and the Gate 3 result from merge_records (None for a task
    with no record; a review-only task's merge has no squash commit). merge_records has no project column, so it is
    read by task key."""
    rows = {
        row["task_key"]: row for row in conn.execute(
            "SELECT task_key, work_card_id, merge_card_id FROM plan_tasks WHERE project = ?", (plan.project,),
        )
    }
    keys = [task.key for task in plan.tasks]
    records: dict = {}
    if keys:
        marks = ",".join("?" * len(keys))
        records = {
            row["task_key"]: row for row in conn.execute(
                f"SELECT task_key, squash_commit, gate3_result FROM merge_records WHERE task_key IN ({marks})", keys,
            )
        }
    summaries = []
    for task in plan.tasks:
        row, record = rows.get(task.key), records.get(task.key)
        summaries.append({
            "task_key": task.key, "title": task.title, "role": task.role,
            "work_card_id": row["work_card_id"] if row is not None else None,
            "merge_card_id": row["merge_card_id"] if row is not None else None,
            "squash_commit": record["squash_commit"] if record is not None else None,
            "gate3_result": record["gate3_result"] if record is not None else None,
        })
    return summaries


def _request_summary(conn: sqlite3.Connection, plan, models_config, report: dict | None, moment: datetime) -> dict:
    """The request budget used, per provider: the day's limit, what was used today and what is left (from the
    report's Budget panel when a report was built, which is what `swarm report` shows; else recomputed from the
    ledger the same way), and this project's total from usage_ingested (requests and worker sessions). The ledger
    itself counts per day and per provider, not per project, so the project total is what says what THIS project
    cost."""
    budget = report.get("budget") if isinstance(report, Mapping) else None
    rows: list[dict] = []
    if isinstance(budget, Mapping) and isinstance(budget.get("providers"), list):
        day = budget.get("day")
        for entry in budget["providers"]:
            rows.append({"provider": entry.get("provider"), "limit": entry.get("limit"),
                         "used_today": entry.get("used"), "remaining": entry.get("remaining")})
    else:
        day = moment.strftime("%Y-%m-%d")
        providers = (models_config or {}).get("providers") or {}
        for name in providers:
            rows.append({"provider": name, "limit": ledger.daily_limit(providers, name),
                         "used_today": ledger.usage_today_for_provider(conn, name),
                         "remaining": ledger.remaining_today(conn, providers, name)})
    totals = {
        row["provider"]: (row["requests"], row["sessions"]) for row in conn.execute(
            "SELECT provider, COALESCE(SUM(requests), 0) AS requests, COUNT(*) AS sessions FROM usage_ingested "
            "WHERE project = ? GROUP BY provider ORDER BY provider", (plan.project,),
        )
    }
    known = {row["provider"] for row in rows}
    rows += [{"provider": name, "limit": None, "used_today": None, "remaining": None} for name in totals
             if name not in known]
    for row in rows:
        requests, sessions = totals.get(row["provider"], (0, 0))
        row["project_total"], row["sessions"] = requests, sessions
    return {"day": day, "providers": rows}


def _bounds_reached(board: str, plan, project, models_config, conn, moment: datetime) -> list[dict]:
    """The bounds of section 9.3 (table 17) whose budget is spent (used >= limit), measured by bounds.evaluate_bounds:
    a fix-card budget that ran out and a human resolved, a provider's daily cap, a plan that has exactly max_cards
    tasks. A reached bound is information for the report, not an error."""
    statuses = bounds.evaluate_bounds(
        board, plan, bounds.Bounds.from_budgets(getattr(project, "budgets", None)), models_config, conn=conn,
        now=moment,
    )
    return [
        {"name": status.name, "subject": status.subject,
         "used": round(status.used, 1) if isinstance(status.used, float) else status.used,
         "limit": round(status.limit, 1) if isinstance(status.limit, float) else status.limit,
         "on_reach": status.on_reach}
        for status in statuses if status.breached
    ]


def _count_events(conn: sqlite3.Connection, kind: str, project: str | None = None) -> int:
    """How many events of `kind`, and of `project` when one is given (only payloads that carry a project field can
    be scoped: recovery_decision does, question_answered and reconcile_repair do not, so those count the database)."""
    if project is None:
        row = conn.execute("SELECT COUNT(*) AS n FROM events WHERE kind = ?", (kind,)).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE kind = ? AND json_extract(payload, '$.project') = ?",
            (kind, project),
        ).fetchone()
    return int(row["n"])


def release_summary(
    board: str, plan, project, models_config, conn: sqlite3.Connection, head: str, *,
    gate4: GateOutcome | None, gate5: GateOutcome | None, now=None, report: dict | None = None,
) -> dict:
    """The release report as plain, JSON-serialisable data (ASES-CTL-01, ASES-OBS-01): the project name, the
    integration branch, the head SHA and generated_at; per plan task its key, title, role, work and merge card,
    squash commit and Gate 3 result; the two final gates (status, notes, the count of findings of each kind and the
    findings themselves as file and line, never a value); the requests used per provider; the bounds reached; the
    questions asked by ASES (`question_asked` events) and answered (`question_answered` events); the re-plans used;
    and how many `recovery_decision` and `reconcile_repair` events there were.

    `report` is the project report finalize() already built (report.build_report): its Budget panel is the source of
    the daily numbers when it is given, so the summary and the report beside it agree, and the board is not asked a
    second time. Without one the daily numbers are recomputed from the ledger. A section that cannot be measured
    (the bounds, when the configuration does not parse) is left empty and named in `notes`: the report still gets
    written. Everything goes through events.redact before it is returned (ASES-SEC-01, ASES-OBS-02)."""
    moment = _moment(now)
    notes: list[str] = []
    try:
        reached = _bounds_reached(board, plan, project, models_config, conn, moment)
    except Exception as exc:  # noqa: BLE001 - one section that cannot be measured must not stop the release report
        reached = []
        notes.append(f"the bounds could not be measured: {_err(exc)}")
    state = bounds.get_state(conn, plan.project)
    summary = {
        "project": getattr(project, "name", plan.project),
        "plan_project": plan.project,
        "board": board,
        "integration_branch": plan.integration_branch,
        "head": head,
        "generated_at": _iso(moment),
        "tasks": _task_summaries(conn, plan),
        "gates": {GATE4: _gate_summary(gate4), GATE5: _gate_summary(gate5)},
        "requests": _request_summary(conn, plan, models_config, report, moment),
        "bounds_reached": reached,
        "questions": {
            "asked": _count_events(conn, "question_asked"), "answered": _count_events(conn, "question_answered"),
        },
        "replans": state["replans"] if state is not None else 0,
        "recovery_decisions": _count_events(conn, "recovery_decision", plan.project),
        "reconcile_repairs": _count_events(conn, "reconcile_repair"),
        "notes": notes,
    }
    return events.redact(summary)


def _pairs(rows: list[tuple]) -> list[str]:
    """Aligned `label  value` lines."""
    width = max((len(str(label)) for label, _ in rows), default=0)
    return [f"{str(label).ljust(width)}  {value}" for label, value in rows]


def _table(headers: list[str], rows: list[list]) -> list[str]:
    """A plain aligned table; every cell is ASCII-escaped first so a column width is a real width."""
    head = [_ascii(header) for header in headers]
    body = [[_ascii("-" if cell is None or cell == "" else cell) for cell in row] for row in rows]
    widths = [max([len(head[i])] + [len(row[i]) for row in body]) for i in range(len(head))]

    def line(cells: list[str]) -> str:
        return "  ".join(cell.ljust(width) for cell, width in zip(cells, widths)).rstrip()

    return [line(head), "  ".join("-" * width for width in widths), *(line(row) for row in body)]


def _render_release(summary: Mapping, files_note: str) -> str:
    """release.md: plain text, ASCII only, one heading per section. Every value comes out of `summary` with .get,
    so a summary written by hand (or by an older version) still renders."""
    gates_data = summary.get("gates") or {}
    lines = ["ASES release report", "===================", ""]
    lines += _pairs([
        ("Project", summary.get("project")), ("Plan project", summary.get("plan_project")),
        ("Board", summary.get("board")), ("Integration branch", summary.get("integration_branch")),
        ("Integration HEAD", summary.get("head")), ("Generated", f"{summary.get('generated_at')} (UTC)"),
    ])
    statuses = [(gates_data.get(name) or {}).get("status") for name in (GATE4, GATE5)]
    lines += ["", "Result", "------"]
    if statuses == ["pass", "pass"]:
        lines.append("RELEASED: Gate 4 and Gate 5 are green on the integration HEAD above.")
    else:
        lines.append("NOT RELEASED: a final gate did not pass or did not run.")
    for name in (GATE4, GATE5):
        data = gates_data.get(name) or {}
        lines.append(f"{GATE_LABELS[name]}: {str(data.get('status', 'not run')).upper()}")
        lines += [f"  note: {note}" for note in data.get("notes") or []]
        counts = data.get("finding_counts") or {}
        if counts:
            lines.append("  findings by kind: " + ", ".join(f"{kind} {count}" for kind, count in counts.items()))
    listed = [f for name in (GATE4, GATE5) for f in (gates_data.get(name) or {}).get("findings") or []]
    lines += ["", "Gate 4 findings (the injection patterns are heuristics and never fail the gate)",
              "-----------------------------------------------------------------------------"]
    if listed:
        for finding in listed:
            where = finding.get("path") or ""
            if where and finding.get("line") is not None:
                where = f"{where}:{finding['line']}"
            head = f"{finding.get('kind')} {where}" if where else str(finding.get("kind"))
            lines.append(f"{head}: {finding.get('detail')}")
        omitted = sum((gates_data.get(name) or {}).get("findings_omitted") or 0 for name in (GATE4, GATE5))
        if omitted:
            lines.append(f"... and {omitted} more (the counts above are complete)")
    else:
        lines.append("none")

    lines += ["", "Plan tasks", "----------"]
    tasks = summary.get("tasks") or []
    lines += _table(
        ["Task", "Title", "Role", "Work card", "Merge card", "Squash commit", "Gate 3"],
        [[t.get("task_key"), t.get("title"), t.get("role"), t.get("work_card_id"), t.get("merge_card_id"),
          (t.get("squash_commit") or "")[:10] or None, t.get("gate3_result")] for t in tasks],
    ) if tasks else ["none"]

    lines += ["", "Requests", "--------"]
    requests = summary.get("requests") or {}
    providers = requests.get("providers") or []
    lines.append(f"UTC day of the daily columns: {requests.get('day')}")
    lines += _table(
        ["Provider", "Daily limit", "Used today", "Remaining today", "This project", "Sessions"],
        [[p.get("provider"), p.get("limit"), p.get("used_today"), p.get("remaining"), p.get("project_total"),
          p.get("sessions")] for p in providers],
    ) if providers else ["no providers"]

    lines += ["", "Bounds reached", "--------------"]
    reached = summary.get("bounds_reached") or []
    lines += _table(
        ["Bound", "Subject", "Used", "Limit", "On reaching it"],
        [[b.get("name"), b.get("subject"), b.get("used"), b.get("limit"), b.get("on_reach")] for b in reached],
    ) if reached else ["none"]

    questions = summary.get("questions") or {}
    lines += ["", "Questions and recovery", "----------------------"]
    lines += _pairs([
        ("Questions asked by ASES", questions.get("asked")), ("Questions answered", questions.get("answered")),
        ("Re-plans used", summary.get("replans")), ("Recovery decisions", summary.get("recovery_decisions")),
        ("Reconcile repairs", summary.get("reconcile_repairs")),
    ])
    notes = summary.get("notes") or []
    if notes:
        lines += ["", "Notes", "-----", *[f"- {note}" for note in notes]]
    lines += ["", "Files", "-----", files_note]
    return "\n".join(_ascii(line) for line in lines) + "\n"


def write_release_report(summary: Mapping, report: dict | None, directory) -> pathlib.Path:
    """ASES-CTL-01, ASES-OBS-02: write the release report into `directory` (created, with its parents) and return the
    path of release.md. release.md is plain text, ASCII only, one heading per section, written as UTF-8 with plain
    newlines. The project report goes beside it through report.write_report, so report.html and report.json sit in
    the same directory; with no `report` that call is skipped and release.md says so. A project report that cannot
    be written (report.write_report raised) does not stop the release report: release.md names the failure and is
    still written. Local files only, nothing is served or uploaded. The summary is redacted again here, as the
    report renderers do: a summary a caller edited by hand is not trusted either (ASES-SEC-01)."""
    directory = pathlib.Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    safe = events.redact(dict(summary))
    if report is None:
        files_note = "report.html and report.json were not written: no project report was available."
    else:
        try:
            report_mod.write_report(report, directory)
            files_note = ("report.html and report.json in this directory are the project report as it stood when "
                          "this release report was written.")
        except Exception as exc:  # noqa: BLE001 - the release report is the deliverable, its attachments are not
            files_note = f"report.html and report.json could not be written: {_err(exc)}"
    path = directory / "release.md"
    path.write_text(_render_release(safe, files_note), encoding="utf-8", newline="\n")
    return path


# ---------------------------------------------------------------------------------------------
# finalize: the lifecycle step
# ---------------------------------------------------------------------------------------------


def _integration_head(repo, branch: str) -> tuple[str | None, str]:
    """(the full SHA the integration branch points at, "") or (None, why not). `refs/heads/<branch>` so a tag of the
    same name cannot win, and --verify -q with the exit code (a bare `git rev-parse <bad-ref>` echoes the bad
    argument on stdout while failing)."""
    if not isinstance(branch, str) or not branch or branch.startswith("-"):
        return None, f"unusable integration branch name {_safe(repr(branch), 80)}"
    try:
        proc = subprocess.run(
            [*_GIT, "-C", str(repo), "rev-parse", "--verify", "-q", f"refs/heads/{branch}^{{commit}}"],
            capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return None, f"git could not read the integration branch: {_err(exc)}"
    sha = proc.stdout.strip()
    if proc.returncode != 0 or not sha:
        detail = _safe(proc.stderr.strip().splitlines()[0], 160) if proc.stderr.strip() else f"exit {proc.returncode}"
        return None, f"cannot resolve the integration branch {_safe(branch, 80)} to a commit ({detail})"
    return sha, ""


def _merge_cards_not_done(board: str, plan, conn: sqlite3.Connection) -> str | None:
    """Why the project is not ready to finalize, or None when every plan task has a merge card that Hermes reports
    `done` (the semantics of controller.all_merge_cards_done, not imported: controller.py imports this module).
    Fail closed: a task with no merge card, a card that cannot be read and an empty plan are all "not ready"."""
    if not plan.tasks:
        return "the plan has no tasks, so there is nothing to release"
    merge_cards = {
        row["task_key"]: row["merge_card_id"] for row in conn.execute(
            "SELECT task_key, merge_card_id FROM plan_tasks WHERE project = ?", (plan.project,),
        )
    }
    for task in plan.tasks:
        card_id = merge_cards.get(task.key)
        if not card_id:
            return f"task {_safe(task.key, 80)} has no merge card yet"
        try:
            card = hermes_mod.kanban_show(board, card_id)
        except Exception as exc:  # noqa: BLE001 - a card that cannot be read is a card that is not known to be done
            return f"merge card {_safe(card_id, 80)} of task {_safe(task.key, 80)} could not be read: {_err(exc)}"
        status = card.get("status") if isinstance(card, Mapping) else None
        if status != "done":
            return (f"merge card {_safe(card_id, 80)} of task {_safe(task.key, 80)} is "
                    f"{_safe(status or 'unreadable', 40)}, not done")
    return None


def _green_gate(conn: sqlite3.Connection, gate: str, head: str) -> GateOutcome | None:
    """The outcome of a gate that already PASSED on exactly `head` (the latest row for that commit wins, the same
    rule as bounds.final_gates_green), rebuilt from its gate_runs row; None when it did not."""
    if gates.last_gate_result(conn, FINAL_TASK_KEY, gate, head) != "pass":
        return None
    row = conn.execute(
        "SELECT detail FROM gate_runs WHERE task_key = ? AND gate = ? AND commit_sha = ? ORDER BY id DESC LIMIT 1",
        (FINAL_TASK_KEY, gate, head),
    ).fetchone()
    return GateOutcome(
        gate, head, True, (row["detail"] or "") if row is not None else "",
        (), ("a green result for this exact commit was already recorded: not run again",),
    )


def _reports_root(project, repo) -> pathlib.Path:
    """Where release reports live: `<ases_home>/reports`, or `<repo>/../ases-reports` when the project has no
    ases_home, and always OUTSIDE the repository: a report written into the primary checkout would show up as an
    untracked file and trip the integrity guard (ASES-GIT-12). An ases_home that turns out to be inside the
    repository (a misconfiguration) gets the fallback too."""
    repo_path = pathlib.Path(repo)
    fallback = repo_path.resolve().parent / "ases-reports"
    home = getattr(project, "ases_home", None)
    if not home:
        return fallback
    root = pathlib.Path(home) / "reports"
    try:
        root.resolve().relative_to(repo_path.resolve())
    except (ValueError, OSError):
        return root
    return fallback


def _report_directory(project, repo, moment: datetime, plan) -> pathlib.Path:
    """`<reports root>/<project name>/<UTC timestamp>` for a report written now: the name reduced to characters that
    are safe in a directory name on every platform, the timestamp with no colon (it is a Windows file name), and a
    -2, -3 ... suffix when that directory already exists, so a second report never overwrites the first."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", str(getattr(project, "name", None) or plan.project)).strip("._")[:80]
    base = _reports_root(project, repo) / (name or "project")
    stamp = moment.strftime("%Y%m%dT%H%M%SZ")
    candidate, attempt = base / stamp, 1
    while candidate.exists():
        attempt += 1
        candidate = base / f"{stamp}-{attempt}"
    return candidate


def _last_report_path(conn: sqlite3.Connection, project: str) -> pathlib.Path | None:
    row = conn.execute(
        "SELECT json_extract(payload, '$.path') AS path FROM events WHERE kind = ? "
        "AND json_extract(payload, '$.project') = ? ORDER BY id DESC LIMIT 1",
        (bounds.RELEASE_REPORT_EVENT, project),
    ).fetchone()
    return pathlib.Path(row["path"]) if row is not None and row["path"] else None


def _stopped_reason(conn: sqlite3.Connection, plan) -> str | None:
    if bounds.stop_requested(conn, plan.project):
        state = bounds.get_state(conn, plan.project) or {}
        return f"the project is {state.get('status', 'stopped')}: the final gates run when it is resumed"
    return None


def _run_final_gate(conn, plan, gate: str, head: str, run: Callable, call_args: tuple, call_kwargs: dict):
    """One final gate inside its intent record and its events (ASES-REC-04: an intent is written BEFORE acting and a
    completion AFTER, so a crash in between is visible to reconcile). Returns (outcome, None), or (None, why) when
    the gate could not run at all: that is an infrastructure failure, not a red gate, so no gate row is written, a
    `final_gate_error` event says what happened and the intent is closed with the error (the crash the intent
    exists for is a dead process, and this is a handled exception). A BaseException such as Ctrl-C leaves the intent
    open, as intents.intent does."""
    intent_id = intents.begin(conn, plan.project, intents.KIND_RUN_GATE, plan.project, f"{gate} on {head}")
    events.record(conn, "final_gate_started", {"project": plan.project, "gate": gate, "commit_sha": head})
    try:
        outcome = run(*call_args, **call_kwargs)
        summary = _outcome_event(plan.project, outcome)
    except Exception as exc:  # noqa: BLE001 - see the docstring
        events.record(conn, "final_gate_error", {
            "project": plan.project, "gate": gate, "commit_sha": head, "error": _err(exc),
        })
        intents.complete(conn, intent_id, detail=f"aborted: {_err(exc)}")
        return None, _err(exc)
    events.record(conn, "final_gate_result", summary)
    intents.complete(conn, intent_id, detail=f"{gate} {'pass' if outcome.passed else 'fail'} on {head}")
    return outcome, None


def _outcome_event(project: str, outcome: GateOutcome) -> dict:
    return {
        "project": project, "gate": outcome.gate, "commit_sha": outcome.commit_sha, "passed": bool(outcome.passed),
        "blocking": len(blocking(outcome.findings)), "advisory": len(advisory(outcome.findings)),
        "finding_counts": _finding_counts(outcome.findings),
        "notes": [_safe(note, 200) for note in outcome.notes][:10],
    }


def finalize(
    board: str, repo, plan, project, models_config, conn: sqlite3.Connection, *, now=None, run4=None, run5=None,
    build=None, is_finished=None, summarize=None, write=None, runner=None, timeout_per_command: int = 300,
) -> FinalizeResult:
    """The single lifecycle step the controller calls once every merge card is done (ASES-TSK-04: "After T9, T10 and
    T11 are merged: controller runs Gate 4, then Gate 5, then writes the final report"; ASES-CTL-01: finished means
    every merge card done, Gates 4 and 5 green on the integration HEAD, and the release report written).

    In order:
      a. a project that is already `finished` returns "finished" and runs nothing; a project that is stopped or
         paused returns "not_ready" (ASES-REC-06: no new work while the kill switch or a bound holds it; the same
         check runs again before each gate and before the report, so a stop lands between steps, never inside one);
      b. a merge card that is not done (or an empty plan) returns "not_ready" with the card in the reason;
      c. the integration branch HEAD is read with git; a failure returns "error";
      d. when bounds.is_finished already holds for that exact HEAD (`is_finished`), nothing runs, the project is
         marked finished if that had not happened yet, and the result is "finished": calling this again after a
         success is harmless;
      e. a final gate already green for that exact HEAD is skipped (a green Gate 4 with no Gate 5 runs only Gate 5),
         Gate 4 runs, and only when it passed does Gate 5 (`run4`, `run5`: the failing one is recorded and the
         result is "gate_failed" with both outcomes that exist). A gate that could not run at all (an exception:
         Docker down, git failing) is "error", not a red gate, and records no gate row;
      f. the branch must still point at that HEAD (if it moved during the gates the result is "not_ready": the next
         call runs the gates on the new HEAD), then the release report is written into
         `<project.ases_home>/reports/<project name>/<UTC timestamp>/` (`summarize`, `write`; the project report is
         built with `build`, and one that cannot be built only costs report.html and report.json) and recorded with
         bounds.mark_release_report; a report that cannot be written is "error";
      g. bounds.finish_project marks the project finished, and the result is "finished".
    Every step records an event (final_gate_started, final_gate_result, release_report_written by
    bounds.mark_release_report, project_finished) and each gate and the report run inside an intent record
    (intents.KIND_RUN_GATE, KIND_RELEASE_REPORT), so a crash half way is visible to reconcile.

    The collaborators are late-bound parameters (None means the real one) so a test injects failures and the sandbox
    `runner` reaches the plan's commands. `now` (a datetime, naive read as UTC) sets the report's clock and folder.

    `plan.gate4_allowlist` (ASES-TSK-04), when the plan sets one, is passed to `run4` as `allow_paths` so Gate 4
    drops those findings from its blocking set (see run_gate4); a plan with none calls `run4` exactly as before
    this field existed, so a `run4` stand-in that predates it keeps working unchanged."""
    run4 = run4 or run_gate4
    run5 = run5 or run_gate5
    build = build or report_mod.build_report
    is_finished = is_finished or bounds.is_finished
    summarize = summarize or release_summary
    write = write or write_release_report
    moment = _moment(now)
    repo = pathlib.Path(repo)

    state = bounds.get_state(conn, plan.project)
    if state is not None and state["status"] == "finished":
        return FinalizeResult(STATUS_FINISHED, None, None, _last_report_path(conn, plan.project),
                              "the project is already finished")
    stopped = _stopped_reason(conn, plan)
    if stopped is not None:
        return FinalizeResult(STATUS_NOT_READY, reason=stopped)
    not_done = _merge_cards_not_done(board, plan, conn)
    if not_done is not None:
        return FinalizeResult(STATUS_NOT_READY, reason=not_done)
    head, why = _integration_head(repo, plan.integration_branch)
    if head is None:
        return FinalizeResult(STATUS_ERROR, reason=why)

    if is_finished(board, plan, head, conn=conn):
        if bounds.finish_project(board, plan, head, conn=conn, now=moment):
            events.record(conn, "project_finished", {"project": plan.project, "commit_sha": head, "report_path": ""})
        return FinalizeResult(
            STATUS_FINISHED, _green_gate(conn, GATE4, head), _green_gate(conn, GATE5, head),
            _last_report_path(conn, plan.project),
            f"Gates 4 and 5 are already green on {head[:10]} and the release report is written",
        )

    outcomes: dict[str, GateOutcome] = {}
    for gate, run in ((GATE4, run4), (GATE5, run5)):
        skipped = _green_gate(conn, gate, head)
        if skipped is not None:
            outcomes[gate] = skipped
            continue
        stopped = _stopped_reason(conn, plan)
        if stopped is not None:
            return FinalizeResult(STATUS_NOT_READY, outcomes.get(GATE4), outcomes.get(GATE5), None, stopped)
        gate_kwargs = {"runner": runner, "timeout_per_command": timeout_per_command}
        if gate == GATE4:
            # ASES-TSK-04: the plan-author decision (Gate P reviewed), passed only when it is not empty, so a
            # run4 stand-in written before this field existed (elsewhere in the suite, or a caller's own) keeps
            # working unchanged: only a plan that actually sets gate4_allowlist sees the new keyword at all.
            allow_paths = getattr(plan, "gate4_allowlist", None) or ()
            if allow_paths:
                gate_kwargs["allow_paths"] = allow_paths
        outcome, error = _run_final_gate(
            conn, plan, gate, head, run, (repo, plan, conn, head), gate_kwargs,
        )
        if outcome is None:
            return FinalizeResult(
                STATUS_ERROR, outcomes.get(GATE4), outcomes.get(GATE5), None,
                f"{GATE_LABELS[gate]} could not run on {head[:10]}: {error}",
            )
        outcomes[gate] = outcome
        if not outcome.passed:
            blockers = len(blocking(outcome.findings))
            why = f"{GATE_LABELS[gate]} failed on {head[:10]}"
            if blockers:
                why += f": {blockers} blocking finding(s)"
            return FinalizeResult(STATUS_GATE_FAILED, outcomes.get(GATE4), outcomes.get(GATE5), None, why)
    gate4, gate5 = outcomes[GATE4], outcomes[GATE5]

    latest, why = _integration_head(repo, plan.integration_branch)
    if latest is None:
        return FinalizeResult(STATUS_ERROR, gate4, gate5, None, why)
    if latest != head:
        return FinalizeResult(
            STATUS_NOT_READY, gate4, gate5, None,
            f"the integration branch moved from {head[:10]} to {latest[:10]} while the final gates ran: they run "
            "again on the new HEAD",
        )
    stopped = _stopped_reason(conn, plan)
    if stopped is not None:
        return FinalizeResult(STATUS_NOT_READY, gate4, gate5, None, stopped)

    intent_id = intents.begin(
        conn, plan.project, KIND_RELEASE_REPORT, plan.project, f"release report for {head}",
    )
    try:
        report = None
        notes: list[str] = []
        try:
            report = build(board, plan, project, models_config, conn, now=moment)
        except Exception as exc:  # noqa: BLE001 - the attachments of the release report must not stop it
            notes.append(
                f"the project report could not be built, so report.html and report.json are missing: {_err(exc)}"
            )
        summary = summarize(
            board, plan, project, models_config, conn, head, gate4=gate4, gate5=gate5, now=moment, report=report,
        )
        if notes:
            summary = events.redact({**summary, "notes": [*summary.get("notes", []), *notes]})
        path = write(summary, report, _report_directory(project, repo, moment, plan))
        bounds.mark_release_report(conn, plan.project, path)
    except Exception as exc:  # noqa: BLE001 - reported as status "error", with the intent closed on the error
        events.record(conn, "release_report_error", {"project": plan.project, "commit_sha": head, "error": _err(exc)})
        intents.complete(conn, intent_id, detail=f"aborted: {_err(exc)}")
        return FinalizeResult(
            STATUS_ERROR, gate4, gate5, None, f"the release report could not be written: {_err(exc)}",
        )
    intents.complete(conn, intent_id, detail=f"release report written to {path}")

    if bounds.finish_project(board, plan, head, conn=conn, now=moment):
        events.record(
            conn, "project_finished", {"project": plan.project, "commit_sha": head, "report_path": str(path)},
        )
        return FinalizeResult(
            STATUS_FINISHED, gate4, gate5, path,
            f"Gates 4 and 5 are green on {head[:10]} and the release report is written",
        )
    state = bounds.get_state(conn, plan.project)
    if state is not None and state["status"] == "finished":      # another process finished it first
        return FinalizeResult(STATUS_FINISHED, gate4, gate5, path, "the project was finished by another process")
    if bounds.stop_requested(conn, plan.project):
        return FinalizeResult(
            STATUS_NOT_READY, gate4, gate5, path,
            "both gates are green and the release report is written, but the project was stopped or paused before it "
            "could be marked finished",
        )
    return FinalizeResult(
        STATUS_ERROR, gate4, gate5, path,
        "both gates are green and the release report is written, but bounds.finish_project refused to finish the "
        f"project (status {(state or {}).get('status', 'unknown')})",
    )
