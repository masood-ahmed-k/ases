"""Gate P plan critique: the independent Reviewer reads the plan before the user is asked to approve it
(section 13.1; ASES-REV-01, ASES-REV-02, ASES-REV-03, ASES-LED-01).

The critique is a one-shot call to the reviewer profile with NO toolsets (`hermes -p reviewer -z <prompt>`), the
same shape as cmd_plan's call to the Lead, and not a Kanban card. Nothing lands on the board before the user has
approved anything (ASES-REV-03), the critic can only answer in text and so cannot touch a file, and the controller
gets the reply directly. Everything here except run_critique and default_invoke is transport agnostic: a card
based path could reuse the parser, the event helpers and next_step unchanged.

The reply is never trusted as prose. It must contain ONE JSON object in the review format of section 13.3, which
parse_critique validates the way plan.py validates plan.json (ASES-LED-01). A malformed verdict gets one repair
request that quotes the exact problems, then the caller blocks for the user (section 19.1, "Malformed plan or
verdict"). A verdict belongs to exactly one plan: for a plan critique the `commit` field carries the sha256 of the
plan file (plan_hash), the controller binds every accepted verdict to the hash of the plan it actually sent, and
the events helpers store and look verdicts up by (project, plan hash), so `swarm approve` can require a PASS for
exactly the plan it is about to publish and nothing older.

CHANGES_REQUIRED goes back to the Lead at most twice (ASES-REV-02; re-plans per project are also a section 9.3
bound, ASES-CTL-01). Counting those rounds is critique_rounds_used, and next_step turns a verdict plus that count
into the one thing the controller does next.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import pathlib
import re
import sqlite3
import subprocess

from . import events as events_mod
from . import hermes as hermes_mod

# Per-input caps on what goes into the prompt (section 13.1 inputs). A prompt is one command-line argument, and
# a Windows command line tops out near 32K characters, so the inputs are bounded and default_invoke checks the
# total (a truncated plan is announced with a marker, so the critic knows it is judging a partial plan).
PLAN_LIMIT = 12000
ARCHITECTURE_LIMIT = 8000
REPO_FACTS_LIMIT = 4000
ESTIMATE_LIMIT = 2000

_TEMPLATE_PATH = pathlib.Path(__file__).resolve().parents[2] / "prompts" / "critic.md"
_PLACEHOLDER = re.compile(r"<<([A-Z_]+)>>")

STATUSES = ("PASS", "CHANGES_REQUIRED", "BLOCKED")
_ISSUE_LISTS = ("architecture_issues", "missing_cases", "security_issues", "test_gaps")
EVENT_KIND = "plan_critique"

# What the controller does next (next_step).
APPROVE = "approve"
REPLAN = "replan"
ASK_USER = "ask_user"

_IS_WINDOWS = os.name == "nt"  # a module attribute so tests can flip it without touching os.name
_WINDOWS_CMDLINE_LIMIT = 32000  # CreateProcess allows 32766 characters in all; keep a margin for the exe path
_MIN_HASH_PREFIX = 12  # a critic that quotes a shortened plan hash must still give at least this many characters


@dataclasses.dataclass(frozen=True)
class PlanCritique:
    """A plan critique after validation (section 13.3 review format). `valid` means well formed and nothing more:
    a CHANGES_REQUIRED or BLOCKED critique is valid, and next_step decides what to do with `status`. Check `valid`
    before acting on any other field: `status` stays readable when only another field is malformed, and an
    invalid critique never approves anything. `plan_hash` is the `commit` field the critic quoted (for a plan
    critique, the hash of the plan it reviewed), or None when it named none; run_critique replaces it with the
    hash of the plan it sent. `problems` holds one short ASCII sentence per violation, or the reason the call
    itself failed."""

    valid: bool
    status: str | None = None
    summary: str = ""
    architecture_issues: list[str] = dataclasses.field(default_factory=list)
    missing_cases: list[str] = dataclasses.field(default_factory=list)
    security_issues: list[str] = dataclasses.field(default_factory=list)
    test_gaps: list[str] = dataclasses.field(default_factory=list)
    gate_tampering_suspected: bool = False
    required_changes: list[str] = dataclasses.field(default_factory=list)
    plan_hash: str | None = None
    problems: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class CritiqueRound:
    """One critique and the round it was made in (1 for the first critique of a project)."""

    round: int
    critique: PlanCritique


def ascii_safe(text: object) -> str:
    """`text` with every non-ASCII character written as an escape. Critic text reaches a person through a Windows
    console (cp1252), where one stray arrow raises an exception, so anything printed or sent on goes through
    this."""
    return str(text).encode("ascii", "backslashreplace").decode("ascii")


def _show(value: object, limit: int = 60) -> str:
    """A value for a problem sentence: ASCII, and clipped, because it comes from a model and can be any size."""
    text = ascii(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _redact(text: object) -> str:
    """ASES-SEC-01: secret-shaped values never reach a prompt or an event. None becomes an empty string."""
    if text is None:
        return ""
    return events_mod.redact({"t": str(text)})["t"]


def _clip(text: str, limit: int) -> str:
    """`text` cut to `limit` characters plus an explicit marker naming how many were removed. Redaction runs
    first (see _prepare), so a secret that straddles the cut is redacted whole, not cut in half."""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n[truncated {len(text) - limit} characters]"


def _prepare(text: object, limit: int) -> str:
    return _clip(_redact(text), limit)


# --- the plan hash -----------------------------------------------------------------------------------------


def _hash_bytes(data: bytes) -> str:
    # CRLF and lone CR become LF first, so the same plan hashes the same on Windows and Linux (git autocrlf, an
    # editor that rewrites line endings). A raw CR cannot occur inside a JSON string, so this never merges two
    # different plans.
    return hashlib.sha256(data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")).hexdigest()


def plan_hash(plan_path: str | os.PathLike) -> str:
    """sha256 hex of the plan file with line endings normalised to LF: the identity of the plan a critique
    (`commit` in the review format) and an approval refer to. Raises OSError when the file cannot be read."""
    return _hash_bytes(pathlib.Path(plan_path).read_bytes())


# --- repository facts ---------------------------------------------------------------------------------------

_SKIP_DIRS = frozenset({
    ".git", ".worktrees", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".tox",
})
_MANIFESTS = (
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "package.json", "tsconfig.json", "go.mod",
    "Cargo.toml", "pom.xml", "build.gradle", "Makefile", "Dockerfile", "pytest.ini", "tox.ini", ".gitignore",
    "README.md",
)
# Names only ever counted, never listed (ASES-SEC-02 keeps agents away from credential files; a name is not a read,
# but there is no reason to send one to a provider either).
_SENSITIVE_NAME = re.compile(r"(^\.env)|\.(pem|key|p12|pfx)$|^id_(rsa|ed25519|ecdsa)", re.IGNORECASE)


def gather_repo_facts(repo: str | os.PathLike, *, max_files: int = 5000, max_listed: int = 40) -> str:
    """The repository facts of section 13.1, as short text: what the critic needs to judge "is the repository
    empty", "does this toolchain exist" and "do these touches make sense", and nothing more. Pure filesystem, no
    git and no subprocess, bounded by `max_files` (the scan stops there and says so), in a stable order so the
    same tree gives the same prompt. Only the directory NAME is given, never the absolute path; file names that
    look like credentials are counted, not listed."""
    root = pathlib.Path(repo)
    if not root.is_dir():
        return "the repository directory does not exist or is not a directory"
    source = planning = sensitive = scanned = 0
    by_ext: dict[str, int] = {}
    cut = False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        rel = pathlib.Path(dirpath).relative_to(root).as_posix()
        for name in sorted(filenames):
            if scanned >= max_files:
                cut = True
                break
            scanned += 1
            if _SENSITIVE_NAME.search(name):
                sensitive += 1
            elif (rel + "/").startswith("docs/ases/"):
                planning += 1
            else:
                source += 1
                ext = pathlib.PurePosixPath(name).suffix.lower() or "(none)"
                by_ext[ext] = by_ext.get(ext, 0) + 1
        if cut:
            break

    lines = [f"repository: {ascii_safe(root.name or 'repo')}", f"git repository: {'yes' if (root / '.git').exists() else 'no'}"]
    lines.append(f"files outside docs/ases/: {source}" + (" (scan stopped at the limit)" if cut else ""))
    lines.append(f"planning files under docs/ases/: {planning}")
    if not source:
        lines.append("the repository has no source files yet: treat it as empty, so a scaffold task should come first")
    top = sorted(by_ext.items(), key=lambda kv: (-kv[1], kv[0]))[:6]
    if top:
        lines.append("file types: " + ", ".join(f"{ext} x {n}" for ext, n in top))
    try:
        children = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError:  # an unreadable directory gives thin facts, not a crashed critique
        children = []
    entries = []
    for child in children:
        if child.name in _SKIP_DIRS or _SENSITIVE_NAME.search(child.name):
            continue
        entries.append(ascii_safe(child.name) + ("/" if child.is_dir() else ""))
    if entries:
        shown = ", ".join(entries[:max_listed])
        lines.append(f"top-level entries: {shown}" + (f" ... and {len(entries) - max_listed} more" if len(entries) > max_listed else ""))
    present = [m for m in _MANIFESTS if (root / m).is_file()]
    lines.append("project files present: " + (", ".join(present) if present else "none"))
    if sensitive:
        lines.append(f"credential-looking files: {sensitive} (names withheld)")
    return "\n".join(lines)


# --- the prompt ----------------------------------------------------------------------------------------------


def load_template() -> str:
    """prompts/critic.md, the versioned critic prompt (ASES-ROL-03). Raises OSError when it is missing."""
    return _TEMPLATE_PATH.read_text(encoding="utf-8")


def build_critique_prompt(
    *, plan_text: str, architecture_text: str, repo_facts: str, estimate_text: str, plan_hash_value: str,
    template: str | None = None,
) -> str:
    """ASES-REV-02 and ASES-SEC-01: fill the critic template with the section 13.1 inputs. Every input is
    redacted first (a secret-shaped value never goes to the reviewer's provider) and then cut to its cap with an
    explicit "[truncated N characters]" marker, N counting the characters of the redacted text that were dropped.
    Placeholders are `<<NAME>>`, filled in ONE pass, so a plan that happens to contain "<<PLAN_TEXT>>" cannot
    have it expanded, and the JSON braces of the schema need no escaping. An unknown placeholder in a custom
    template is left as it is."""
    body = template if template is not None else load_template()
    values = {
        "PLAN_HASH": str(plan_hash_value),
        "PLAN_TEXT": _prepare(plan_text, PLAN_LIMIT),
        "ARCHITECTURE_TEXT": _prepare(architecture_text, ARCHITECTURE_LIMIT),
        "REPO_FACTS": _prepare(repo_facts, REPO_FACTS_LIMIT),
        "ESTIMATE_TEXT": _prepare(estimate_text, ESTIMATE_LIMIT),
    }
    return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), body)


# --- reading the verdict --------------------------------------------------------------------------------------

_MAX_CANDIDATES = 100  # `{` positions tried before giving up: bounds the work on a pathological reply


def _invalid(problem: str) -> PlanCritique:
    return PlanCritique(valid=False, problems=(problem,))


def _first_object(text: str) -> tuple[dict | None, str]:
    """(the first JSON object in `text`, "") or (None, why there is none). Each `{` in turn is handed to the JSON
    decoder, which knows about strings and escapes, so a brace inside a string never confuses it, prose around
    the object (or a ```json fence) is simply skipped, and a `{` in the prose that is not JSON is passed over."""
    decoder = json.JSONDecoder()
    first_error = ""
    tried = 0
    pos = text.find("{")
    while pos != -1 and tried < _MAX_CANDIDATES:
        tried += 1
        try:
            value, _end = decoder.raw_decode(text, pos)
        except RecursionError:  # not a ValueError: a reply of a few thousand nested brackets lands here
            first_error = first_error or "it is nested too deeply"
        except ValueError as exc:
            first_error = first_error or str(exc)
        else:
            if isinstance(value, dict):
                return value, ""
        pos = text.find("{", pos + 1)
    if not tried:
        return None, "the reply contains no JSON object"
    return None, f"the reply contains no valid JSON object (first parse error: {ascii_safe(first_error)[:120]})"


def _type_name(value: object) -> str:
    return type(value).__name__


def parse_critique(text: object) -> PlanCritique:
    """ASES-LED-01 and section 19.1 ("Malformed plan or verdict"): find ONE JSON object in a reply and check it
    against the review format of section 13.3. Never raises: a reply that is not text, has no JSON in it or has
    JSON nested past the recursion limit comes back invalid with a problem, and so does every violation below.

    The object is the first balanced top-level `{...}` in the text, wrapped in prose or a fence or not. Then:
    review_status must be PASS, CHANGES_REQUIRED or BLOCKED (any case, normalised to upper case); summary a
    non-empty string; the four issue lists and required_changes lists of strings when present (absent means
    empty); gate_tampering_suspected a bool when present (absent means False); commit a string when present (it is
    the plan hash, and a blank one counts as absent). CHANGES_REQUIRED with no change listed is itself a problem:
    the Lead would be sent back with nothing to do. Extra keys are tolerated. `valid` is True only with no
    problems, and every problem is a short ASCII sentence."""
    try:
        return _parse(text)
    except RecursionError:
        return _invalid("the reply is nested too deeply to read")


def _parse(text: object) -> PlanCritique:
    if not isinstance(text, str):
        return _invalid(f"the reply is not text (got {_type_name(text)})")
    obj, why = _first_object(text)
    if obj is None:
        return _invalid(why)

    problems: list[str] = []
    status = None
    if "review_status" not in obj:
        problems.append("review_status is missing, expected PASS, CHANGES_REQUIRED or BLOCKED")
    else:
        raw = obj["review_status"]
        candidate = raw.strip().upper() if isinstance(raw, str) else None
        if candidate in STATUSES:
            status = candidate
        else:
            problems.append(f"review_status is {_show(raw)}, expected PASS, CHANGES_REQUIRED or BLOCKED")

    summary = ""
    if "summary" not in obj:
        problems.append("summary is missing")
    elif not isinstance(obj["summary"], str):
        problems.append(f"summary is not a string (got {_type_name(obj['summary'])})")
    elif not obj["summary"].strip():
        problems.append("summary is empty")
    else:
        summary = obj["summary"]

    lists: dict[str, list[str]] = {}
    for name in _ISSUE_LISTS + ("required_changes",):
        value = obj.get(name, [])
        if not isinstance(value, list):
            problems.append(f"{name} is not a list (got {_type_name(value)})")
            lists[name] = []
            continue
        bad = next((i for i, item in enumerate(value) if not isinstance(item, str)), None)
        if bad is not None:
            problems.append(f"{name}[{bad}] is not a string (got {_type_name(value[bad])})")
        lists[name] = [item for item in value if isinstance(item, str)]

    tampering = False
    if "gate_tampering_suspected" in obj:
        value = obj["gate_tampering_suspected"]
        if isinstance(value, bool):
            tampering = value
        else:
            problems.append(f"gate_tampering_suspected is not a bool (got {_type_name(value)})")

    quoted_hash = None
    if "commit" in obj:
        value = obj["commit"]
        if isinstance(value, str):
            quoted_hash = value.strip() or None
        else:
            problems.append(f"commit is not a string (got {_type_name(value)})")

    if status == "CHANGES_REQUIRED" and not any(change.strip() for change in lists["required_changes"]):
        problems.append("review_status is CHANGES_REQUIRED but required_changes is empty (a change request with no change)")

    return PlanCritique(
        valid=not problems, status=status, summary=summary,
        architecture_issues=lists["architecture_issues"], missing_cases=lists["missing_cases"],
        security_issues=lists["security_issues"], test_gaps=lists["test_gaps"],
        gate_tampering_suspected=tampering, required_changes=lists["required_changes"],
        plan_hash=quoted_hash, problems=tuple(problems),
    )


# --- calling the critic ---------------------------------------------------------------------------------------


def default_invoke(profile: str, prompt: str, timeout: int) -> tuple[int, str, str]:
    """(exit code, stdout, stderr) of `hermes -p <profile> -z <prompt>`, UTF-8 with errors="replace". Deliberately
    no `-t`: a oneshot call grants no toolset without it (docs/architecture.md), and the critique must not be able
    to touch a file. Never raises. A run that could not happen or did not finish (hermes missing, the OS refusing
    the command, the timeout, a prompt too long for a Windows command line) comes back as exit code -1 with the
    reason in stderr, the same one-shape convention guards.py uses for git, so run_critique deals in one failure
    shape."""
    try:
        argv = [hermes_mod.hermes_path(), "-p", profile, "-z", prompt]
    except hermes_mod.HermesNotFound as exc:
        return -1, "", str(exc)
    if _IS_WINDOWS:
        length = len(subprocess.list2cmdline(argv))
        if length > _WINDOWS_CMDLINE_LIMIT:
            return -1, "", (
                f"the critique prompt is {length} characters as a command line, over the Windows limit "
                f"(about {_WINDOWS_CMDLINE_LIMIT}); shorten docs/ases/plan.json or docs/ases/architecture.md"
            )
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
            env=hermes_mod.scrubbed_environ(),  # ASES-CFG-05: a provider key in the launching shell stops here
        )
    except subprocess.TimeoutExpired:
        return -1, "", f"the reviewer did not answer within {timeout}s"
    except OSError as exc:
        return -1, "", f"hermes could not be run: {exc}"
    return result.returncode, result.stdout or "", result.stderr or ""


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")


def _display_path(path: pathlib.Path, repo: pathlib.Path) -> str:
    """A path for the prompt: relative to the repository when it is inside it, so the user's directory names
    are not sent to the reviewer's provider."""
    try:
        return path.relative_to(repo).as_posix()
    except ValueError:
        return path.name


def _architecture_text(repo: pathlib.Path, architecture_path: str | os.PathLike | None) -> str:
    """The architecture file (section 12.2 puts it at docs/ases/architecture.md), or a note when there is none:
    a plan without an architecture file is still worth critiquing, and the critic should know it is missing."""
    path = pathlib.Path(architecture_path) if architecture_path is not None else repo / "docs" / "ases" / "architecture.md"
    try:
        return _decode(path.read_bytes())
    except OSError:
        return (
            f"(no architecture file was found at {ascii_safe(_display_path(path, repo))}; judge the plan without "
            "it, and say in summary if the missing architecture matters)"
        )


def _failure_problem(code: int, out: str, err: str) -> str:
    """The trimmed stderr of a failed call (its tail: a traceback ends with the reason), redacted and ASCII."""
    # Escape BEFORE trimming: a non-ASCII character grows when it is written as an escape, so trimming first
    # could leave the result over the bound.
    text = ascii_safe(_redact((err or "").strip() or (out or "").strip()))
    if not text:
        return f"the reviewer call exited {code} with no output"
    if len(text) > 500:
        text = "..." + text[-497:]
    return text


def _same_plan(quoted: str, expected: str) -> bool:
    """True when the critic's `commit` is the plan hash: equal without regard to case, or a prefix of at least 12
    characters (a model often shortens a 64 character hash; the same courtesy review.verdict_matches_head gives a
    quoted commit SHA)."""
    q = quoted.strip().lower()
    return q == expected or (len(q) >= _MIN_HASH_PREFIX and expected.startswith(q))


def _judge(reply: str, expected_hash: str) -> PlanCritique:
    """parse_critique plus the one check that needs the plan: a verdict that names a different plan is invalid.
    An accepted verdict is bound to `expected_hash` whether or not the critic quoted it (the controller knows
    which plan it sent), so events and approval always see the full hash."""
    critique = parse_critique(reply)
    if not critique.valid:
        return critique
    if critique.plan_hash is not None and not _same_plan(critique.plan_hash, expected_hash):
        return dataclasses.replace(critique, valid=False, problems=(
            f"the critic reviewed a different plan: commit {_show(critique.plan_hash)} does not match the plan "
            f"hash {expected_hash}",
        ))
    return dataclasses.replace(critique, plan_hash=expected_hash)


def _repair_prompt(prompt: str, problems: tuple[str, ...]) -> str:
    listed = "\n".join(f"- {problem}" for problem in problems)
    return (
        f"{prompt}\n\n## Your earlier reply was rejected\n"
        "An earlier reply to this same request could not be accepted, for these exact reasons:\n"
        f"{listed}\n"
        "Fix every one of them and reply again with only the corrected JSON object.\n"
    )


def run_critique(
    *, repo: str | os.PathLike, plan_path: str | os.PathLike, architecture_path: str | os.PathLike | None = None,
    estimate_text: str, invoke=default_invoke, template: str | None = None, timeout: int = 900,
    profile: str = "reviewer", repo_facts: str | None = None,
) -> PlanCritique:
    """ASES-REV-01 and ASES-REV-02 (Gate P, after Gate 0 passed): ask the independent Reviewer to critique the
    plan and return a validated PlanCritique. `invoke(profile, prompt, timeout) -> (exit code, stdout, stderr)`
    is the only thing that touches the outside world, so tests inject a fake.

    The plan, the architecture file (`architecture_path`, else docs/ases/architecture.md; a missing one is fine,
    the critic is told), the repository facts (gathered from `repo` unless `repo_facts` is given) and
    `estimate_text` (the budget and calendar estimate the user will see) go into the prompt. A call that exits
    non-zero returns an invalid critique whose problem is the trimmed stderr, with no repair call: the failure
    is the transport, not the reply. A reply that fails validation gets ONE repair call (the same prompt plus
    the exact problems and "reply again with only the corrected JSON object", section 19.1); a second failure
    is returned as it is, and the caller blocks for the user. A critique that quotes a different plan hash than
    the file sent is invalid ("the critic reviewed a different plan"), and every valid one comes back bound to
    the full hash of the plan that was sent.

    Never raises for a bad input file: an unreadable plan or template comes back as an invalid critique too, so
    the caller has one failure shape to block on."""
    root = pathlib.Path(repo)
    try:
        plan_bytes = pathlib.Path(plan_path).read_bytes()
    except OSError as exc:
        return _invalid(f"cannot read the plan file {ascii_safe(pathlib.Path(plan_path).name)}: {ascii_safe(exc)}")
    expected_hash = _hash_bytes(plan_bytes)
    try:
        prompt = build_critique_prompt(
            plan_text=_decode(plan_bytes), architecture_text=_architecture_text(root, architecture_path),
            repo_facts=repo_facts if repo_facts is not None else gather_repo_facts(root),
            estimate_text=estimate_text, plan_hash_value=expected_hash, template=template,
        )
    except OSError as exc:
        return _invalid(f"cannot load the critic prompt template: {ascii_safe(exc)}")

    code, out, err = invoke(profile, prompt, timeout)
    if code != 0:
        return _invalid(_failure_problem(code, out, err))
    critique = _judge(out, expected_hash)
    if critique.valid:
        return critique

    code, out, err = invoke(profile, _repair_prompt(prompt, critique.problems), timeout)
    if code != 0:
        return dataclasses.replace(
            critique, problems=critique.problems + (f"the repair call failed: {_failure_problem(code, out, err)}",),
        )
    return _judge(out, expected_hash)


# --- recording verdicts ---------------------------------------------------------------------------------------


def record_critique(conn: sqlite3.Connection, plan_project: str, round_no: int, critique: PlanCritique) -> None:
    """ASES-REV-02 (the bound needs a count) and ASES-REV-03 (approval needs a verdict): store a critique as a
    `plan_critique` event. The payload carries project, round, status, valid, plan_hash, summary, the four issue
    lists, gate_tampering_suspected, required_changes and problems, and goes through events.record, so a
    secret-shaped value is redacted before it touches the database. An invalid critique is recorded too (for the
    audit trail), with valid False: the readers below never count or approve one."""
    events_mod.record(conn, EVENT_KIND, {
        "project": plan_project, "round": round_no, "status": critique.status, "valid": critique.valid,
        "plan_hash": critique.plan_hash, "summary": critique.summary,
        "architecture_issues": list(critique.architecture_issues), "missing_cases": list(critique.missing_cases),
        "security_issues": list(critique.security_issues), "test_gaps": list(critique.test_gaps),
        "gate_tampering_suspected": critique.gate_tampering_suspected,
        "required_changes": list(critique.required_changes), "problems": list(critique.problems),
    })


def _payloads(conn: sqlite3.Connection, plan_project: str):
    """The payload of every plan_critique event of this project, newest first (by id: the timestamps only have
    second resolution). A row whose payload is not a JSON object is skipped."""
    for row in conn.execute("SELECT payload FROM events WHERE kind = ? ORDER BY id DESC", (EVENT_KIND,)):
        try:
            payload = json.loads(row[0])
        except (ValueError, RecursionError):
            continue
        if isinstance(payload, dict) and payload.get("project") == plan_project:
            yield payload


def critique_rounds_used(conn: sqlite3.Connection, plan_project: str) -> int:
    """ASES-REV-02: how many times this project's plan has already gone back to the Lead, that is the number of
    valid CHANGES_REQUIRED critiques recorded for it. Pass the result to next_step as `rounds_used` BEFORE
    recording the critique being judged. An invalid critique is not a round: nothing was sent back."""
    return sum(1 for p in _payloads(conn, plan_project) if p.get("valid") is True and p.get("status") == "CHANGES_REQUIRED")


def latest_critique(conn: sqlite3.Connection, plan_project: str, plan_hash_value: str) -> dict | None:
    """The payload of the newest plan_critique event of this project for exactly this plan hash, or None. A
    critique of any other plan (an earlier draft, a later rewrite) is never returned: a verdict belongs to one
    plan. None for a blank hash, so an event that never learned its plan hash cannot be matched by asking for
    nothing."""
    if not isinstance(plan_hash_value, str) or not plan_hash_value.strip():
        return None
    for payload in _payloads(conn, plan_project):
        if payload.get("plan_hash") == plan_hash_value:
            return payload
    return None


def is_plan_approved_by_critic(conn: sqlite3.Connection, plan_project: str, plan_hash_value: str) -> bool:
    """ASES-REV-03: True only when the NEWEST critique for exactly this plan hash is a valid PASS. This is what
    `swarm approve` requires before it will publish a plan: an edit after the PASS changes the hash and voids it,
    and a later critique of the same plan that is not a PASS (or is malformed) revokes it."""
    latest = latest_critique(conn, plan_project, plan_hash_value)
    return bool(latest) and latest.get("valid") is True and latest.get("status") == "PASS"


# --- what happens next -----------------------------------------------------------------------------------------


def next_step(critique: PlanCritique, rounds_used: int, *, max_rounds: int = 2) -> str:
    """ASES-REV-02: what the controller does with a critique. `rounds_used` counts the rounds BEFORE this one
    (critique_rounds_used, read before this critique is recorded). "approve" for PASS: the plan goes to the
    user. "replan" for CHANGES_REQUIRED while fewer than `max_rounds` rounds were used, so the plan goes back to
    the Lead at most twice by default. "ask_user" for everything else: CHANGES_REQUIRED past the limit, BLOCKED
    (a human decision is needed) and an invalid critique (the one repair request is already spent inside
    run_critique, and section 19.1 says to block for the user). Anything unrecognised is "ask_user" too: the
    safe answer is a person, never an approval."""
    if not critique.valid:
        return ASK_USER
    if critique.status == "PASS":
        return APPROVE
    if critique.status == "CHANGES_REQUIRED":
        return REPLAN if rounds_used < max_rounds else ASK_USER
    return ASK_USER


def _clean(text: object, limit: int) -> str:
    """Model or user text for a prompt the Lead reads: redacted, cut with a marker, ASCII only."""
    return ascii_safe(_clip(_redact(text), limit))


def lead_feedback_prompt(critique: PlanCritique, *, request: str, plan_path: str | os.PathLike) -> str:
    """The text the controller gives the Lead to re-plan after CHANGES_REQUIRED: the original request, the exact
    path to rewrite, the critic's summary, every required change as a numbered list, and the other findings for
    context. ASES-SEC-01 (secret-shaped values redacted) and ASCII only (it is logged and shown). Bounded: a
    critic that writes a wall of text cannot make the Lead's prompt unbounded, and the cut is marked."""
    lines = [
        "The independent reviewer critiqued your plan and requires changes before the user can be asked to approve it.",
        "",
        f"Project request: {_clean(request, 2000)}",
        f"Plan file to rewrite (use this exact absolute path, do not rely on any working directory): {_clean(plan_path, 500)}",
        "",
        f"Reviewer summary: {_clean(critique.summary, 1500)}",
        "",
    ]
    changes = [c for c in critique.required_changes if c.strip()]
    if changes:
        lines.append("Required changes:")
        lines += [f"{i}. {_clean(change, 600)}" for i, change in enumerate(changes[:15], 1)]
        if len(changes) > 15:
            lines.append(f"(and {len(changes) - 15} more not shown here)")
    else:
        lines.append("Required changes: the reviewer listed none in detail; work from the summary.")
    for label, items in (
        ("Architecture issues", critique.architecture_issues), ("Missing cases", critique.missing_cases),
        ("Security issues", critique.security_issues), ("Test gaps", critique.test_gaps),
    ):
        shown = [item for item in items if item.strip()]
        if shown:
            lines += ["", f"{label}:"] + [f"- {_clean(item, 400)}" for item in shown[:8]]
    lines += [
        "",
        "Your job now: rewrite the plan file; do not argue; keep it small. Keep the same top-level shape, and every "
        "task still needs a key, a role, depends_on, touches, acceptance criteria, a gate_profile and "
        "estimated_requests. After writing the file, reply with just the word done.",
    ]
    return "\n".join(lines)
