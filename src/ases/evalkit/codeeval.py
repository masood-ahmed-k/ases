"""Applying a model's answer to a temp copy of a fixture and running pytest there (tasks E4, E5 and E6).

Two things about running model output. First, it is code from a model, so it runs only in a throwaway copy, with
the credential-shaped environment variables removed and pytest's plugin autoload switched off (which also makes a
run three times faster and independent of whatever plugins the user has installed). It still runs with the user's
privileges: an evaluation is only as safe as the model being evaluated, and that is said in the report of the
package. Second, a model's patch is rarely byte perfect, so the diff applier locates each hunk by its content
rather than by its line numbers and forgives lost trailing spaces, exactly like a person applying it by hand.

Edits to test files and to pytest configuration are ignored (see is_protected): a fix that works by changing the
test is not a fix, and E4 and E5 grade against the original suite.
"""
from __future__ import annotations

import concurrent.futures
import dataclasses
import pathlib
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence

from .. import procenv
from .text import CodeBlock, extract_code_blocks


def scrubbed_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment a model-written test or program runs in: the current one without any variable whose name
    looks like a credential (ASES-SEC-01: a generated test must not be able to read an API key from the
    environment; procenv.scrubbed_environ is the one definition of which names count), without PYTHONPATH and
    pytest's option variables, with pytest plugin autoload off, no bytecode files, and UTF-8 output."""
    env = procenv.scrubbed_environ()
    for name in ("PYTHONPATH", "PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONSTARTUP"):
        env.pop(name, None)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if extra:
        env.update(extra)
    return env


@dataclasses.dataclass(frozen=True)
class PytestResult:
    """One pytest run: `returncode` is None when it timed out or could not start. `tail` is the last lines of
    its output, for a person debugging a score."""

    returncode: int | None
    passed: int
    failed: int
    errors: int
    timed_out: bool
    tail: str

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.errors

    @property
    def ok(self) -> bool:
        """Exit code 0 with at least one test passed (an empty or fully skipped suite proves nothing)."""
        return self.returncode == 0 and self.passed > 0


_SUMMARY_TOKEN = re.compile(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected)")


def parse_pytest_summary(text: str) -> tuple[int, int, int]:
    """(passed, failed, errors) from the last pytest summary line, such as '1 failed, 3 passed in 0.05s'. (0, 0, 0)
    when there is none ('no tests ran', or a crash before pytest printed one)."""
    for line in reversed(text.splitlines()):
        if not re.search(r"\bin [\d.]+s\b", line):
            continue
        found = _SUMMARY_TOKEN.findall(line)
        if not found:
            continue
        passed = failed = errors = 0
        for number, word in found:
            if word == "passed":
                passed += int(number)
            elif word == "failed":
                failed += int(number)
            elif word.startswith("error"):
                errors += int(number)
        return passed, failed, errors
    return 0, 0, 0


def _tail(text: str, lines: int = 30, chars: int = 3000) -> str:
    kept = "\n".join(text.strip().splitlines()[-lines:])
    return kept[-chars:]


def run_pytest(
    directory: pathlib.Path, args: Sequence[str] = (), *, timeout: int = 60, stop_first: bool = False,
) -> PytestResult:
    """`python -m pytest -q` in `directory` (through sys.executable, so the same interpreter as the harness) with
    the scrubbed environment. Never raises: a timeout or an OS error comes back as returncode None. `-p
    no:cacheprovider` keeps the copy free of a .pytest_cache; `-x` (stop_first) saves time when only 'does any
    test fail' matters, as when a mutant is being killed."""
    argv = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=short"]
    if stop_first:
        argv.append("-x")
    argv += list(args)
    try:
        proc = subprocess.run(
            argv, cwd=str(directory), capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=scrubbed_env(),
        )
    except subprocess.TimeoutExpired:
        return PytestResult(None, 0, 0, 0, True, f"pytest did not finish within {timeout}s")
    except OSError as exc:
        return PytestResult(None, 0, 0, 0, False, f"pytest could not be started: {exc}")
    out = (proc.stdout or "") + (proc.stderr or "")
    passed, failed, errors = parse_pytest_summary(out)
    return PytestResult(proc.returncode, passed, failed, errors, False, _tail(out))


def run_pytest_many(
    jobs: Sequence[tuple[pathlib.Path, Sequence[str]]], *, timeout: int = 60, stop_first: bool = True,
    workers: int = 4,
) -> list[PytestResult]:
    """run_pytest over several directories at once (each pytest run is its own process, so threads are enough),
    results in the order of `jobs`. Used to run one test file against every seeded mutant of a module."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(lambda job: run_pytest(job[0], job[1], timeout=timeout, stop_first=stop_first), jobs))


def write_tree(root: pathlib.Path, files: Mapping[str, str]) -> None:
    """Write {relative posix path: text} under `root` as UTF-8 with LF line endings, making directories as needed."""
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")


def copy_tree(src: pathlib.Path, dest: pathlib.Path) -> None:
    """A copy of `src` at `dest` (which must not exist yet), leaving out bytecode and pytest caches."""
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"))


# ---- which files a model may change ------------------------------------------------------------------------

PROTECTED_NAMES = frozenset({
    "conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", "sitecustomize.py", "usercustomize.py",
})


def is_protected(relpath: str) -> bool:
    """True for a file an answer may not change: anything under tests/ or test/, any test_*.py or *_test.py, and
    the files that configure pytest or Python's startup. Changing one could make a failing suite pass without
    fixing anything (a conftest.py that skips everything, a test rewritten to expect the bug)."""
    parts = relpath.split("/")
    name = parts[-1]
    return (
        parts[0] in ("tests", "test") or name.startswith("test_") or name.endswith("_test.py")
        or name in PROTECTED_NAMES
    )


def safe_relpath(raw: str) -> str | None:
    """A repository-relative posix path taken from a diff header or a hint, or None when it is absent, absolute,
    has a drive letter, climbs with '..', or holds a character no Windows file name can hold. A model must not be
    able to write outside the temp copy."""
    text = str(raw).strip().strip("\"'").replace("\\", "/")
    text = text.split("\t")[0].strip()
    if not text or text == "/dev/null":
        return None
    while text.startswith("./"):
        text = text[2:]
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        return None
    if any(ord(ch) < 32 or ch in '<>|?*":' for ch in text):
        return None
    if any(part in ("", ".", "..") for part in text.split("/")):
        return None
    return text


# ---- unified diffs ------------------------------------------------------------------------------------------

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_DIFF_SIGNATURE = re.compile(r"^--- .+\n\+\+\+ .+", re.MULTILINE)


@dataclasses.dataclass
class _Hunk:
    old_start: int
    lines: list[tuple[str, str]]  # (tag, text): tag is ' ' (context), '-' (removed) or '+' (added)


@dataclasses.dataclass
class _FilePatch:
    old_path: str
    new_path: str
    hunks: list[_Hunk]


def _header_path(raw: str) -> str:
    return raw.split("\t")[0].strip().strip("\"'")


def _diff_lines(text: str) -> list[str]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def parse_unified_diff(text: str) -> list[_FilePatch]:
    """The file patches of a unified diff (git style or plain). Tolerant the way a model's diff has to be read: a
    context line that lost its leading space, or is empty, is a context line; the '\\ No newline' marker and git's
    extended header lines are skipped."""
    lines = _diff_lines(text)
    patches: list[_FilePatch] = []
    current: _FilePatch | None = None
    hunk: _Hunk | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            hunk = None
            i += 1
            continue
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            current = _FilePatch(_header_path(line[4:]), _header_path(lines[i + 1][4:]), [])
            patches.append(current)
            hunk = None
            i += 2
            continue
        header = _HUNK_HEADER.match(line)
        if header and current is not None:
            hunk = _Hunk(int(header.group(1)), [])
            current.hunks.append(hunk)
            i += 1
            continue
        if hunk is not None and not line.startswith("\\"):
            if line[:1] in ("+", "-"):
                hunk.lines.append((line[0], line[1:]))
            elif line.startswith(" "):
                hunk.lines.append((" ", line[1:]))
            else:
                hunk.lines.append((" ", line))
        i += 1
    return patches


def _find_block(lines: list[str], old: list[str], hint: int, start: int) -> int | None:
    """Where `old` occurs in `lines`: the match nearest the hinted line, looked for from `start` on first (hunks
    come in file order) and anywhere second, exactly first, then ignoring trailing spaces, then ignoring
    indentation as well. None when it occurs nowhere."""
    size = len(old)
    for normal in (lambda s: s, lambda s: s.rstrip(), lambda s: s.strip()):
        wanted = [normal(s) for s in old]
        for begin in (start, 0) if start > 0 else (0,):
            hits = [
                i for i in range(begin, len(lines) - size + 1) if [normal(s) for s in lines[i:i + size]] == wanted
            ]
            if hits:
                return min(hits, key=lambda i: (abs(i - hint), i)) if hint >= 0 else hits[0]
    return None


def _apply_hunks(original: str, hunks: list[_Hunk]) -> tuple[str | None, str | None]:
    """(new text, None) or (None, why). The file always ends with a newline afterwards."""
    lines = original.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    cursor = 0
    for number, hunk in enumerate(hunks, start=1):
        old = [text for tag, text in hunk.lines if tag in (" ", "-")]
        new = [text for tag, text in hunk.lines if tag in (" ", "+")]
        if old:
            position = _find_block(lines, old, hunk.old_start - 1, cursor)
            if position is None:
                return None, f"hunk {number} does not match the file"
        else:
            position = min(max(hunk.old_start, 0), len(lines))
        lines[position:position + len(old)] = new
        cursor = position + len(new)
    return "\n".join(lines) + "\n", None


def apply_unified_diff(original: str, diff: str) -> tuple[str | None, str | None]:
    """Apply the FIRST file patch of `diff` to the text `original`: (new text, None), or (None, why) when a hunk
    does not match. Use apply_answer to apply a whole reply to a directory."""
    patches = parse_unified_diff(diff)
    if not patches or not patches[0].hunks:
        return None, "the diff has no hunks"
    return _apply_hunks(original, patches[0].hunks)


# ---- applying a whole reply to a directory ------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ApplyResult:
    """What apply_answer did. `method` is 'diff', 'replacement' or 'none' (nothing usable in the reply). `written`
    lists the files changed, `ignored` the protected files the reply also tried to change (dropped), and
    `problems` why nothing was applied (a patch is applied all or nothing)."""

    method: str
    written: tuple[str, ...]
    ignored: tuple[str, ...]
    problems: tuple[str, ...]


def _find_diff(output: str, blocks: list[CodeBlock]) -> str | None:
    for block in blocks:
        if _DIFF_SIGNATURE.search(block.body) and "@@" in block.body:
            return block.body
    if not blocks:
        text = output.replace("\r\n", "\n")
        found = _DIFF_SIGNATURE.search(text)
        if found and "@@" in text:
            return text[found.start():]
    return None


def _diff_target(patch: _FilePatch, root: pathlib.Path) -> str | None:
    """The repository path a file patch changes: the header path as written when that file exists, else without
    git's a/ or b/ prefix (a file that is being created exists under neither name)."""
    raw = patch.new_path if patch.new_path != "/dev/null" else patch.old_path
    stripped = raw[2:] if raw[:2] in ("a/", "b/") else raw
    for candidate in (raw, stripped):
        rel = safe_relpath(candidate)
        if rel is not None and (root / rel).is_file():
            return rel
    return safe_relpath(stripped)


def _apply_diff_answer(diff_text: str, root: pathlib.Path) -> ApplyResult:
    patches = parse_unified_diff(diff_text)
    if not patches:
        return ApplyResult("diff", (), (), ("the diff has no file headers",))
    new_texts: dict[str, str] = {}
    ignored: list[str] = []
    problems: list[str] = []
    for patch in patches:
        rel = _diff_target(patch, root)
        if rel is None:
            problems.append(f"the diff names a path that is not allowed: {ascii(patch.new_path)}")
            continue
        if is_protected(rel):
            ignored.append(rel)
            continue
        if patch.new_path == "/dev/null":
            problems.append(f"the diff deletes {rel}, which is not allowed")
            continue
        path = root / rel
        if patch.old_path != "/dev/null" and not path.is_file():
            problems.append(f"{rel} does not exist in the repository")
            continue
        if not patch.hunks:
            problems.append(f"{rel}: the patch has no hunks")
            continue
        original = path.read_text(encoding="utf-8") if path.is_file() else ""
        text, why = _apply_hunks(original, patch.hunks)
        if why is not None:
            problems.append(f"{rel}: {why}")
            continue
        new_texts[rel] = text
    if problems:
        return ApplyResult("diff", (), tuple(ignored), tuple(problems))
    write_tree(root, new_texts)
    return ApplyResult("diff", tuple(sorted(new_texts)), tuple(ignored), ())


_PY_PATH = re.compile(r"(?<![\w./\\-])((?:[\w.-]+[/\\])*[\w.-]+\.py)(?!\w)")


def _hinted_target(block: CodeBlock, root: pathlib.Path) -> str | None:
    """The file a code block claims to replace, from a file name in the lines above its fence or in a first-line
    comment. A partial path is matched to the one existing file that ends with it; a name that matches nothing is
    a new file at that path."""
    first = block.body.split("\n", 1)[0] if block.body.lstrip().startswith("#") else ""
    found = _PY_PATH.findall(block.hint + "\n" + first)
    if not found:
        return None
    rel = safe_relpath(found[-1])
    if rel is None:
        return None
    if (root / rel).is_file():
        return rel
    hits = [
        p.relative_to(root).as_posix() for p in root.rglob("*.py")
        if p.relative_to(root).as_posix().endswith("/" + rel)
    ]
    return hits[0] if len(hits) == 1 else rel


def _apply_replacement_answer(
    blocks: list[CodeBlock], root: pathlib.Path, default_target: str | None,
) -> ApplyResult:
    candidates = [b for b in blocks if b.lang in ("", "python", "py", "python3") and b.body.strip()]
    if not candidates:
        return ApplyResult("none", (), (), ("the reply has no unified diff and no code block",))
    assignments: dict[str, str] = {}
    unhinted: list[CodeBlock] = []
    for block in candidates:
        target = _hinted_target(block, root)
        if target is None:
            unhinted.append(block)
        else:
            assignments[target] = block.body
    problems: list[str] = []
    if unhinted and not assignments:
        if default_target is not None and len(unhinted) == 1:
            assignments[default_target] = unhinted[0].body
        else:
            problems.append("the reply has code blocks but does not say which file each one replaces")
    if problems:
        return ApplyResult("replacement", (), (), tuple(problems))
    ignored = sorted(rel for rel in assignments if is_protected(rel))
    written = {rel: body.rstrip("\n") + "\n" for rel, body in assignments.items() if not is_protected(rel)}
    write_tree(root, written)
    return ApplyResult("replacement", tuple(sorted(written)), tuple(ignored), ())


def apply_answer(output: str, root: pathlib.Path, *, default_target: str | None = None) -> ApplyResult:
    """Turn a model's reply into file changes under `root` (a temp copy of the fixture): a unified diff when the
    reply holds one (in a fence or bare), otherwise the complete file in a code block. A block that names its file
    (a path in the line above the fence, or a first-line comment) replaces that file; one that names none replaces
    `default_target`, but only when it is the sole block. Edits to protected files are dropped and listed."""
    blocks = extract_code_blocks(output)
    diff_text = _find_diff(output, blocks)
    if diff_text is not None:
        return _apply_diff_answer(diff_text, root)
    return _apply_replacement_answer(blocks, root, default_target)
