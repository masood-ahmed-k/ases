"""The evaluation tasks that run code: E4 Debugging, E5 Refactor and E6 Test design, plus the E8 Long-horizon descriptor
(blueprint Appendix D.1).

E4 and E5 grade a model's patch by applying it to a temp copy of the fixture and running the ORIGINAL test suite there
(edits to test files are dropped: evalkit/codeeval.py). E6 grades the model's tests by running them against the
reference module and against seeded mutants of it, the idea of benchmarks/allocate/eval_arm.py: a test suite is only
as good as the bugs it catches. E8 needs the whole swarm, so here it is a descriptor plus a scorer that reads a finished
project; the runner refuses to run it standalone (evals.run_eval).
"""
from __future__ import annotations

import ast
import json
import pathlib
import re
import sqlite3
import subprocess
import sys
import tempfile
import zipfile

from .. import gitexec
from . import codeeval, text
from .model import KIND_REPO, KIND_SWARM, EvalTask, InvokeResult, Score
from .texttasks import CALL_REQUESTS

_PREAMBLE = (
    "This is an automated evaluation of your written answer, not a conversation: nobody will reply to a question, "
    "so answer directly and completely. Do not use any tools.\n\n"
)


def _listing(files: dict[str, str]) -> str:
    return "\n".join(f"--- {path} ---\n{body}" for path, body in files.items())


# ---- E4 Debugging: a failing test, a patch, and the test passing afterwards -----------------------------------------

E4_FILES: dict[str, str] = {
    "textkit/__init__.py": '"""Small sequence helpers."""\n',
    "textkit/chunks.py": (
        '"""Helpers for splitting sequences."""\n\n\n'
        "def chunk(items, size):\n"
        '    """Split `items` into consecutive lists of at most `size` items.\n\n'
        "    The last list may be shorter than `size`. `size` must be a positive integer.\n"
        '    """\n'
        "    if size <= 0:\n"
        '        raise ValueError("size must be positive")\n'
        "    return [list(items[i:i + size]) for i in range(0, len(items) - size + 1, size)]\n"
    ),
    "tests/test_chunks.py": (
        "import pytest\n\n"
        "from textkit.chunks import chunk\n\n\n"
        "def test_exact_multiple():\n"
        "    assert chunk([1, 2, 3, 4], 2) == [[1, 2], [3, 4]]\n\n\n"
        "def test_last_chunk_may_be_shorter():\n"
        "    assert chunk([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]\n\n\n"
        "def test_empty_input():\n"
        "    assert chunk([], 3) == []\n\n\n"
        "def test_rejects_non_positive_size():\n"
        "    with pytest.raises(ValueError):\n"
        "        chunk([1, 2, 3], 0)\n"
    ),
}
E4_TARGET = "textkit/chunks.py"
# The fixed source, for the tests of the scorer (a known-good answer): the one line that differs from E4_FILES.
E4_FIXED_LINE = "    return [list(items[i:i + size]) for i in range(0, len(items), size)]"
E4_BUGGY_LINE = "    return [list(items[i:i + size]) for i in range(0, len(items) - size + 1, size)]"


def _e4_fixture(workdir: pathlib.Path) -> dict:
    codeeval.write_tree(workdir, E4_FILES)
    baseline = codeeval.run_pytest(workdir, ["tests"], timeout=60)
    return {
        "target": E4_TARGET, "files": dict(E4_FILES), "failure_output": text.ascii_safe(baseline.tail),
        "baseline_passed": baseline.passed, "baseline_failed": baseline.failed,
    }


def _e4_prompt(fixture: dict) -> str:
    return (
        _PREAMBLE
        + "A Python package has a failing test. Find the root cause and fix the package code, not the tests.\n\n"
        + _listing(fixture["files"])
        + "\n\nOutput of `python -m pytest -q tests`:\n" + fixture["failure_output"]
        + "\n\nReply with EITHER a unified diff (with the --- and +++ header lines and @@ hunks) OR the complete "
        f"corrected contents of {fixture['target']}, in one fenced code block. A short explanation before it is "
        "fine. Do not change the tests: a change to a test file is thrown away."
    )


def _last_line(block: str) -> str:
    lines = block.strip().splitlines()
    return lines[-1] if lines else ""


def _apply_and_run(
    workdir: pathlib.Path, output: str, target: str,
) -> tuple[codeeval.ApplyResult, codeeval.PytestResult, str]:
    """Apply the reply to a temp copy of `workdir` and run its tests/ suite there: (what was applied, the pytest run,
    the patched target file's text or '' when it cannot be read)."""
    with tempfile.TemporaryDirectory(prefix="ases-eval-") as tmp:
        copy = pathlib.Path(tmp) / "repo"
        codeeval.copy_tree(workdir, copy)
        applied = codeeval.apply_answer(output, copy, default_target=target)
        run = codeeval.run_pytest(copy, ["tests"], timeout=60)
        path = copy / target
        source = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    return applied, run, source


def _apply_findings(applied: codeeval.ApplyResult) -> dict:
    return {
        "patch_method": applied.method, "files_changed": len(applied.written),
        "protected_edits_ignored": len(applied.ignored), "patch_problems": "; ".join(applied.problems),
    }


def _e4_score(fixture: dict, workdir: pathlib.Path, output: str, result: InvokeResult) -> Score:
    applied, run, _ = _apply_and_run(workdir, output, fixture["target"])
    findings = _apply_findings(applied)
    findings["tests_failing_after"] = run.failed + run.errors
    return Score(
        success=bool(applied.written) and run.ok and run.failed == 0 and run.errors == 0,
        tests_passed=run.passed, tests_total=run.total, findings=findings,
        notes="; ".join(applied.problems) or _last_line(run.tail),
    )


# ---- E5 Refactor: change the code, keep the suite green ---------------------------------------------------------------

E5_FILES: dict[str, str] = {
    "billing/__init__.py": "",
    "billing/invoice.py": (
        '"""Invoice totals for the shop."""\n\n'
        "TAX_RATE = 0.2\n\n\n"
        "def summarize_orders(orders):\n"
        '    """Return the subtotal, tax, total and line count for a list of order dicts.\n\n'
        '    Each order has "qty" (int) and "unit_price" (float). A negative quantity raises ValueError.\n'
        '    """\n'
        "    lines = 0\n"
        "    subtotal = 0.0\n"
        "    for order in orders:\n"
        '        if order["qty"] < 0:\n'
        '            raise ValueError("negative quantity")\n'
        '        subtotal += order["qty"] * order["unit_price"]\n'
        "        lines += 1\n"
        "    tax = round(subtotal * TAX_RATE, 2)\n"
        "    return {\n"
        '        "subtotal": round(subtotal, 2),\n'
        '        "tax": tax,\n'
        '        "total": round(subtotal + tax, 2),\n'
        '        "lines": lines,\n'
        "    }\n"
    ),
    "tests/test_invoice.py": (
        "import pytest\n\n"
        "from billing.invoice import summarize_orders\n\n\n"
        "def test_single_line():\n"
        '    got = summarize_orders([{"qty": 2, "unit_price": 5.0}])\n'
        '    assert got == {"subtotal": 10.0, "tax": 2.0, "total": 12.0, "lines": 1}\n\n\n'
        "def test_several_lines():\n"
        '    got = summarize_orders([{"qty": 1, "unit_price": 2.5}, {"qty": 4, "unit_price": 1.25}])\n'
        '    assert got == {"subtotal": 7.5, "tax": 1.5, "total": 9.0, "lines": 2}\n\n\n'
        "def test_empty_list():\n"
        '    assert summarize_orders([]) == {"subtotal": 0.0, "tax": 0.0, "total": 0.0, "lines": 0}\n\n\n'
        "def test_negative_quantity_is_rejected():\n"
        "    with pytest.raises(ValueError):\n"
        '        summarize_orders([{"qty": -1, "unit_price": 1.0}])\n\n\n'
        "def test_rounding_to_cents():\n"
        '    got = summarize_orders([{"qty": 3, "unit_price": 0.1}])\n'
        '    assert got == {"subtotal": 0.3, "tax": 0.06, "total": 0.36, "lines": 1}\n\n\n'
        "def test_zero_quantity_still_counts_as_a_line():\n"
        '    got = summarize_orders([{"qty": 0, "unit_price": 9.99}])\n'
        '    assert got["lines"] == 1 and got["total"] == 0.0\n'
    ),
}
E5_TARGET = "billing/invoice.py"


def check_e5_structure(source: str) -> dict[str, bool]:
    """The structural half of E5, read from the AST of the refactored module: the per-order logic is a module-level
    function `line_total(order)` that `summarize_orders` actually calls, the constant TAX_RATE is now SALES_TAX_RATE
    (assigned, and the old name appears nowhere), and the public function keeps its name and its one parameter."""
    keys = ("parses", "function_extracted", "extracted_function_used", "constant_renamed", "public_api_kept")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {key: False for key in keys}
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    extracted = functions.get("line_total")
    summarize = functions.get("summarize_orders")
    assigned = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            assigned.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            assigned.add(node.target.id)
    return {
        "parses": True,
        "function_extracted": extracted is not None
        and len(extracted.args.posonlyargs) + len(extracted.args.args) == 1,
        "extracted_function_used": summarize is not None and any(
            isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "line_total"
            for node in ast.walk(summarize)
        ),
        "constant_renamed": "SALES_TAX_RATE" in assigned
        and not any(isinstance(node, ast.Name) and node.id == "TAX_RATE" for node in ast.walk(tree)),
        "public_api_kept": summarize is not None
        and [a.arg for a in summarize.args.args] == ["orders"] and not summarize.args.posonlyargs,
    }


def _e5_fixture(workdir: pathlib.Path) -> dict:
    codeeval.write_tree(workdir, E5_FILES)
    return {"target": E5_TARGET, "files": dict(E5_FILES)}


def _e5_prompt(fixture: dict) -> str:
    return (
        _PREAMBLE
        + "Refactor this package. Behaviour must not change and every existing test must still pass.\n\n"
        + _listing(fixture["files"])
        + "\n\nThe refactoring:\n"
        "1. Extract the per-order logic of summarize_orders (the negative quantity check and the quantity times unit "
        "price) into a module-level function `line_total(order)` that returns the total of one order, and make "
        "summarize_orders call it.\n"
        "2. Rename the constant TAX_RATE to SALES_TAX_RATE everywhere it is used.\n\n"
        f"Reply with EITHER a unified diff (with --- and +++ header lines and @@ hunks) OR the complete new contents "
        f"of {fixture['target']}, in one fenced code block. Do not change the tests."
    )


def _e5_score(fixture: dict, workdir: pathlib.Path, output: str, result: InvokeResult) -> Score:
    applied, run, source = _apply_and_run(workdir, output, fixture["target"])
    structure = check_e5_structure(source) if source else check_e5_structure("this is not python(")
    findings = _apply_findings(applied)
    findings.update(structure)
    findings["tests_failing_after"] = run.failed + run.errors
    suite_green = run.ok and run.failed == 0 and run.errors == 0
    unmet = [name for name, held in structure.items() if not held]
    notes = "structural checks not met: " + ", ".join(unmet) if unmet else _last_line(run.tail)
    return Score(
        success=suite_green and not unmet,
        tests_passed=run.passed, tests_total=run.total, findings=findings, notes=notes,
    )


# ---- E6 Test design: tests are judged by the bugs they catch ---------------------------------------------------------

E6_MODULE = '''"""Interval helpers."""


def merge_intervals(intervals):
    """Merge overlapping or touching (start, end) intervals and return them sorted, as tuples.

    An interval whose start is greater than its end raises ValueError. The list passed in is never modified.
    """
    checked = []
    for start, end in intervals:
        if start > end:
            raise ValueError("interval start must not exceed its end")
        checked.append((start, end))
    checked.sort()
    merged = []
    for start, end in checked:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def total_covered(intervals):
    """Total length covered by the union of the intervals: overlaps count once, gaps not at all."""
    return sum(end - start for start, end in merge_intervals(intervals))
'''

# (id, what the bug is, exact text in E6_MODULE, replacement). Each `old` occurs exactly once in E6_MODULE (build_e6_mutants
# asserts it), and every mutant changes behaviour that a reasonable test can observe (the tests of this module run a
# reference test file against all of them). The style of benchmarks/allocate/eval_arm.py.
E6_MUTANTS: tuple[tuple[str, str, str, str], ...] = (
    ("M01", "touching intervals are not merged",
     "        if merged and start <= merged[-1][1]:\n", "        if merged and start < merged[-1][1]:\n"),
    ("M02", "the input is not sorted first", "    checked.sort()\n", ""),
    ("M03", "a contained interval shrinks the merged one", "max(merged[-1][1], end)", "end"),
    ("M04", "a start greater than its end is accepted",
     '        if start > end:\n            raise ValueError("interval start must not exceed its end")\n', ""),
    ("M05", "an empty input returns None", "    checked = []\n", "    if not intervals:\n        return None\n    checked = []\n"),
    ("M06", "merged intervals are lists, not tuples", "            merged.append((start, end))\n",
     "            merged.append([start, end])\n"),
    ("M07", "the caller's list is sorted in place", "    checked.sort()\n",
     "    intervals.sort()\n    checked = list(intervals)\n"),
    ("M08", "total_covered counts the gaps too",
     "    return sum(end - start for start, end in merge_intervals(intervals))\n",
     "    merged = merge_intervals(intervals)\n    return merged[-1][1] - merged[0][0] if merged else 0\n"),
    ("M09", "total_covered counts one unit too many per interval",
     "sum(end - start for start, end in merge_intervals(intervals))",
     "sum(end - start + 1 for start, end in merge_intervals(intervals))"),
    ("M10", "a zero-length interval is rejected", "        if start > end:\n", "        if start >= end:\n"),
)
E6_MODULE_FILE = "intervals.py"
E6_TEST_FILE = "test_generated.py"
E6_MIN_KILLED = 7  # of the 10 mutants
# Tests that read or rewrite the module's own source could 'kill' every mutant by comparing text, so they are refused.
_E6_FORBIDDEN = re.compile(r"getsource|__file__|read_text|write_text|\bopen\s*\(|__code__")


def build_e6_mutants() -> dict[str, str]:
    """{mutant id: the module source with that one bug}. Raises AssertionError when a mutation no longer applies
    exactly once (the module and the table drifted apart), which the tests of this module would catch first."""
    mutants = {}
    for mutant_id, _what, old, new in E6_MUTANTS:
        assert E6_MODULE.count(old) == 1, (mutant_id, E6_MODULE.count(old))
        mutants[mutant_id] = E6_MODULE.replace(old, new)
    return mutants


def extract_test_code(output: str) -> str | None:
    """The test file in a reply: every Python code block (tagged python, py, python3 or pytest, or untagged with a test
    function in it) joined in order; a reply with no fences at all is taken whole when it has a test function; else None."""
    blocks = text.extract_code_blocks(output)
    chosen = [
        b.body for b in blocks
        if b.body.strip() and (b.lang in ("python", "py", "python3", "pytest") or (b.lang == "" and "def test" in b.body))
    ]
    if chosen:
        return "\n\n".join(chosen) + "\n"
    if not blocks and "def test" in output:
        return output.rstrip("\n") + "\n"
    return None


def _e6_fixture(workdir: pathlib.Path) -> dict:
    return {
        "module_name": "intervals", "module": E6_MODULE, "mutants": build_e6_mutants(),
        "mutant_labels": {m[0]: m[1] for m in E6_MUTANTS},
    }


def _e6_prompt(fixture: dict) -> str:
    return (
        _PREAMBLE
        + "Here is a small Python module, `intervals.py`:\n\n```python\n" + fixture["module"] + "```\n\n"
        "Write a pytest test file for it. The tests will be run against the module and then against several "
        "deliberately broken versions of it, and are judged by how many of the broken versions they catch, so think "
        "about edge cases and every behaviour the docstrings promise, not just the typical case.\n"
        "Import it with `from intervals import merge_intervals, total_covered`. Use only pytest and the standard "
        "library, and never read or modify the module's file. Reply with one fenced Python code block holding the "
        "complete test file."
    )


def _e6_score(fixture: dict, workdir: pathlib.Path, output: str, result: InvokeResult) -> Score:
    mutants: dict[str, str] = fixture["mutants"]
    findings: dict = {"mutants_total": len(mutants), "mutants_needed": E6_MIN_KILLED, "mutants_killed": 0,
                      "tests_found": False, "passes_on_reference": False, "survivors": ""}
    code = extract_test_code(output)
    if code is None:
        return Score(False, findings=findings, notes="the reply holds no test code")
    findings["tests_found"] = True
    if _E6_FORBIDDEN.search(code):
        return Score(False, findings=findings, notes="the tests read or rewrite the module's source, which is not allowed")
    with tempfile.TemporaryDirectory(prefix="ases-eval-") as tmp:
        base = pathlib.Path(tmp)
        reference = base / "reference"
        codeeval.write_tree(reference, {E6_MODULE_FILE: fixture["module"], E6_TEST_FILE: code})
        ref_run = codeeval.run_pytest(reference, [E6_TEST_FILE], timeout=60)
        if not ref_run.ok:
            return Score(
                False, tests_passed=ref_run.passed, tests_total=ref_run.total, findings=findings,
                notes="the tests do not pass on the reference module: " + (ref_run.tail.splitlines() or [""])[-1],
            )
        findings["passes_on_reference"] = True
        jobs = []
        for mutant_id, source in mutants.items():
            directory = base / mutant_id
            codeeval.write_tree(directory, {E6_MODULE_FILE: source, E6_TEST_FILE: code})
            jobs.append((directory, [E6_TEST_FILE]))
        runs = codeeval.run_pytest_many(jobs, timeout=30, stop_first=True)
    survivors = [mid for mid, run in zip(mutants, runs, strict=True) if run.returncode == 0]
    killed = len(mutants) - len(survivors)
    findings["mutants_killed"] = killed
    findings["survivors"] = ",".join(survivors)
    return Score(
        success=killed >= E6_MIN_KILLED, tests_passed=ref_run.passed, tests_total=ref_run.total, findings=findings,
        notes=f"the tests caught {killed} of {len(mutants)} seeded bugs (need {E6_MIN_KILLED})",
    )


# ---- E8 Long-horizon task: a descriptor, scored from a finished swarm project ---------------------------------------

E8_BRANCH = "integration"
E8_REQUEST = (
    "Build a small command-line todo manager in Python 3 using only the standard library. Create the package `todo` "
    "with `todo/storage.py` (load and save the list of items as JSON in the file named by the environment variable "
    "TODO_FILE), `todo/core.py` (add an item, list the items, mark an item done; ids are 1, 2, 3 in order of creation), "
    "and `todo/cli.py` plus `todo/__main__.py` so that `python -m todo add TEXT`, `python -m todo list` and "
    "`python -m todo done ID` work. `add` prints `added <id>`. `list` prints one line per item, `<id> [ ] <text>` for an "
    "open item and `<id> [x] <text>` for a done one. `done` prints `done <id>`, and exits with status 1 and a message on "
    "stderr for an id that does not exist. Every module has pytest tests in `tests/`. Do not edit README.md."
)
E8_REQUIRED_FILES = ("todo/storage.py", "todo/core.py", "todo/cli.py", "todo/__main__.py")
E8_MIN_OWN_TESTS = 3
E8_REFUSAL = (
    "E8 (Long-horizon task) needs the whole swarm and cannot run standalone: there is no one-shot model call to score. "
    "To evaluate it, build the throwaway repository with the task's build_fixture (evalkit/codetasks.py, E8_REQUEST is "
    "the request), run `swarm plan --repo <repo> --request \"<request>\"`, then `swarm approve` and `swarm run` on it, "
    "and score the finished project with evals.score_swarm_project(conn, project, repo)."
)


def _git(repo: pathlib.Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    """Routed through gitexec (round 9, GITHARDEN): _e8_fixture's own init/add/commit run before any model has
    touched `repo`, but _export_branch's `git archive` runs against it AFTER a swarm build (E8), i.e. against a
    repository a model's worker may have written into -- the same worker-controlled-repo case gitexec exists for,
    so every call this helper makes gets the same hardened prefix and scrubbed environment, not just that one."""
    return subprocess.run(
        [*gitexec.GIT, "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, env=gitexec.git_env(),
    )


def _e8_fixture(workdir: pathlib.Path) -> dict:
    """The throwaway repository the swarm is pointed at: a README, a .gitignore and one commit on the integration
    branch. Git is told who the committer is with -c, so no git configuration is written anywhere."""
    codeeval.write_tree(workdir, {
        "README.md": "# todo\n\nA throwaway repository for the E8 evaluation.\n", ".gitignore": "__pycache__/\n*.pyc\n",
    })
    for args in (
        ("init", "-q", "-b", E8_BRANCH), ("add", "-A"),
        ("-c", "user.name=ases-eval", "-c", "user.email=eval@example.invalid", "commit", "-q", "-m", "initial commit"),
    ):
        proc = _git(workdir, *args)
        if proc.returncode != 0:
            raise RuntimeError(f"git {args[0]} failed while building the E8 repository: {proc.stderr.strip()[:200]}")
    return {"repo": str(workdir), "integration_branch": E8_BRANCH, "request": E8_REQUEST, "project": None,
            "db_path": None}


def _e8_prompt(fixture: dict) -> str:
    return fixture["request"]


def _export_branch(repo: pathlib.Path, branch: str, dest: pathlib.Path) -> str | None:
    """Export `branch` of `repo` into the empty directory `dest`; None on success, else why not."""
    archive = dest.parent / (dest.name + ".zip")
    try:
        proc = _git(repo, "archive", "--format=zip", "-o", str(archive), branch, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"git archive could not run: {exc}"
    if proc.returncode != 0:
        return f"git archive {branch} failed: {proc.stderr.strip()[:200]}"
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(dest)
    return None


def _run_todo(tree: pathlib.Path, state: pathlib.Path, *args: str) -> subprocess.CompletedProcess | None:
    env = codeeval.scrubbed_env({"TODO_FILE": str(state), "PYTHONPATH": str(tree)})
    try:
        return subprocess.run(
            [sys.executable, "-m", "todo", *args], cwd=str(tree), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30, env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def run_e8_checks(tree: pathlib.Path) -> list[tuple[str, bool]]:
    """The acceptance checks of E8, in order, against an exported copy of the integration branch. They are the task's
    hidden tests: the swarm was told the behaviour in E8_REQUEST but never sees these commands."""
    state = tree.parent / (tree.name + "-todo.json")
    results: list[tuple[str, bool]] = []

    def listing() -> list[str]:
        proc = _run_todo(tree, state, "list")
        if proc is None or proc.returncode != 0:
            return []
        return [line.strip() for line in proc.stdout.splitlines() if line.strip()]

    results.append(("files_exist", all((tree / rel).is_file() for rel in E8_REQUIRED_FILES)))
    first = _run_todo(tree, state, "add", "buy milk")
    results.append(("add_first", first is not None and first.returncode == 0 and "added 1" in first.stdout))
    second = _run_todo(tree, state, "add", "write tests")
    results.append(("add_second", second is not None and second.returncode == 0 and "added 2" in second.stdout))
    results.append(("list_open", listing() == ["1 [ ] buy milk", "2 [ ] write tests"]))
    done = _run_todo(tree, state, "done", "1")
    results.append(("done", done is not None and done.returncode == 0 and "done 1" in done.stdout))
    results.append(("list_after_done", listing()[:1] == ["1 [x] buy milk"]))
    missing = _run_todo(tree, state, "done", "99")
    results.append(("done_unknown_id", missing is not None and missing.returncode == 1))
    suite = codeeval.run_pytest(tree, ["tests"], timeout=120) if (tree / "tests").is_dir() else None
    results.append(("own_tests_pass", suite is not None and suite.ok and suite.passed >= E8_MIN_OWN_TESTS))
    return results


def score_swarm_project(
    conn: sqlite3.Connection, project: str, repo: pathlib.Path, integration_branch: str = E8_BRANCH,
) -> Score:
    """E8's scorer: read a FINISHED swarm project (blueprint D.1: 'task completion and review count'). Every plan task of
    `project` must have a completed, not reverted, merge record; the integration branch of `repo` is exported and put
    through run_e8_checks; success is both. The counts are kept apart, as Appendix D.2 asks: tasks merged, reverts,
    review rounds (the controller's lineage counter, else the CHANGES_REQUIRED verdicts), model requests counted for
    the project, and the human answers given (question_answered events for its tasks). Reads the database only.

    merge_records' primary key is (project, task_key) (schema v8, round 9), so the read below is scoped to
    `project` the same NULL-tolerant way gates.last_gate_result reads gate_runs: this project's own rows, or a
    legacy row with no project recorded, never a different project's row for one of this project's task keys."""
    keys = [r["task_key"] for r in conn.execute(
        "SELECT task_key FROM plan_tasks WHERE project = ? ORDER BY task_key", (project,))]
    if not keys:
        return Score(False, findings={"tasks_total": 0}, notes=f"no plan tasks are recorded for project {text.ascii_safe(project)}")
    marks = ",".join("?" for _ in keys)
    # round 12, finding 6: a legacy project=NULL row and this project's own row can coexist for one task_key
    # (db.py's v8 migration leaves an ambiguous task_key NULL on purpose), so the raw row list can hold two
    # entries for the same task_key. Summing over `merges` directly (the old code) double-counted that task_key,
    # which could make `merged` exceed `len(keys)` and turn a genuinely fully-merged project into a false "not
    # all tasks merged" failure. Folded into a dict keyed by task_key instead (the pattern report._quality_panel
    # and finalgates._task_summaries already use): ORDER BY (project IS NULL) DESC visits the legacy NULL row
    # first for a given task_key, so this project's own row -- visited second -- is the one left in the dict.
    merges = conn.execute(
        f"SELECT task_key, completed_at, reverted FROM merge_records "
        f"WHERE task_key IN ({marks}) AND (project IS NULL OR project = ?) "
        f"ORDER BY (project IS NULL) DESC", (*keys, project)).fetchall()
    records = {m["task_key"]: m for m in merges}
    merged = sum(1 for m in records.values() if m["completed_at"] and not m["reverted"])
    reverted = sum(1 for m in records.values() if m["reverted"])
    lineage = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(review_rounds), 0) AS rounds FROM lineage WHERE project = ?", (project,)).fetchone()
    if lineage["n"]:
        review_changes = int(lineage["rounds"])
    else:
        review_changes = int(conn.execute(
            "SELECT COUNT(*) FROM review_verdicts WHERE project = ? AND outcome = 'CHANGES_REQUIRED'", (project,)).fetchone()[0])
    requests = int(conn.execute(
        "SELECT COALESCE(SUM(requests), 0) FROM usage_ingested WHERE project = ?", (project,)).fetchone()[0])
    interventions = 0
    # question_answered carries no project (questions.answer_question is "not scoped to a plan"; events.py package,
    # round 9), so this is already correctly scoped the only way it can be: read every row, then keep only the
    # ones whose own task_key is one of THIS project's (`keys`, from plan_tasks, above). A COALESCE(project, ...)
    # filter would be wrong here, not merely unhelpful: every question_answered row's project column and payload
    # are NULL, so it would silently drop every intervention instead of counting them.
    for row in conn.execute("SELECT payload FROM events WHERE kind = 'question_answered'"):
        try:
            payload = json.loads(row["payload"])
        except ValueError:
            continue
        if isinstance(payload, dict) and payload.get("task_key") in keys:
            interventions += 1
    state = conn.execute("SELECT status FROM project_state WHERE project = ?", (project,)).fetchone()

    checks: list[tuple[str, bool]] = []
    problem = ""
    with tempfile.TemporaryDirectory(prefix="ases-eval-") as tmp:
        tree = pathlib.Path(tmp) / "tree"
        tree.mkdir()
        problem = _export_branch(pathlib.Path(repo), integration_branch, tree) or ""
        if not problem:
            checks = run_e8_checks(tree)
    passed = sum(1 for _, ok in checks if ok)
    findings: dict = {
        "tasks_total": len(keys), "tasks_merged": merged, "tasks_reverted": reverted,
        "review_changes_required": review_changes, "requests": requests, "human_interventions": interventions,
        "project_status": state["status"] if state else "unknown", "checks_passed": passed, "checks_total": len(checks),
    }
    findings.update({f"check_{name}": ok for name, ok in checks})
    all_merged = merged == len(keys)
    return Score(
        success=all_merged and bool(checks) and passed == len(checks),
        tests_passed=passed, tests_total=len(checks) or None, findings=findings,
        notes=problem or f"{merged} of {len(keys)} tasks merged, {passed} of {len(checks)} acceptance checks passed",
    )


def _e8_score(fixture: dict, workdir: pathlib.Path, output: str, result: InvokeResult) -> Score:
    db_path, project = fixture.get("db_path"), fixture.get("project")
    if not db_path or not project or not pathlib.Path(db_path).is_file():
        return Score(False, notes="E8 is scored from a finished swarm project: set fixture['db_path'] (the ASES database) "
                                  "and fixture['project'] (the plan's project slug), then score again")
    from .. import db as ases_db  # imported here: a scorer that is never asked to score E8 should not open a database
    conn = ases_db.connect(db_path)
    try:
        return score_swarm_project(conn, project, pathlib.Path(fixture.get("repo") or workdir),
                                   fixture.get("integration_branch", E8_BRANCH))
    finally:
        conn.close()


# ---- the task objects ---------------------------------------------------------------------------------------------

E4 = EvalTask(
    id="E4", title="Debugging", kind=KIND_REPO, build_fixture=_e4_fixture, build_prompt=_e4_prompt, score=_e4_score,
    est_requests=CALL_REQUESTS, what="find the root cause of a failing test and fix it (graded by the original suite)",
)
E5 = EvalTask(
    id="E5", title="Refactor", kind=KIND_REPO, build_fixture=_e5_fixture, build_prompt=_e5_prompt, score=_e5_score,
    est_requests=CALL_REQUESTS, what="extract a function and rename a constant without breaking the suite",
)
E6 = EvalTask(
    id="E6", title="Test design", kind=KIND_REPO, build_fixture=_e6_fixture, build_prompt=_e6_prompt, score=_e6_score,
    est_requests=CALL_REQUESTS, what="write tests that pass on a module and catch most of its seeded bugs",
)
# Blueprint 5.4: a small coding task 25 to 60 requests, a review 8 to 20, the Lead's planning pass 30 to 80, plus about 15 percent
# for auxiliary calls. For this two to three card project that is roughly 100 to 280 requests; 200 is the middle.
E8 = EvalTask(
    id="E8", title="Long-horizon task", kind=KIND_SWARM, build_fixture=_e8_fixture, build_prompt=_e8_prompt,
    score=_e8_score, est_requests=200,
    what="keep a multi-step task coherent through the whole swarm (needs swarm run)",
)
