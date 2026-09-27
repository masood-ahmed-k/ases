"""ases.gitexec: the shared argv prefix, scrubbed environment and diff-safety flags every controller git call
uses (round 9, package GITHARDEN). These are the empirical proofs behind gitexec.py's module docstring, each
against a real throwaway git repository (never Docker, never a real Hermes, never a real model provider): a
worker who can only write inside a repository (the shared .git/hooks, .git/config, a committed .gitattributes)
must not be able to make a controller git call run their code, hide content from a scanner, or read a
credential-shaped variable out of the controller's own environment.

The completeness scan (test_no_bare_git_subprocess_call_bypasses_gitexec_outside_fakes_and_gates) is the
project's proof that no OTHER git call site in src/ases regressed back to a bare `["git", ...]` argv; it is
written in the spirit of test_fakes.py's public-function/fake signature check.
"""
from __future__ import annotations

import ast
import pathlib
import subprocess
import sys

from ases import gitexec

_SRC_ROOT = pathlib.Path(__file__).resolve().parents[2] / "src" / "ases"

_SUBPROCESS_STARTERS = {"run", "Popen", "call", "check_output", "check_call"}


def _is_subprocess_call(node: ast.Call) -> bool:
    func = node.func
    return (
        isinstance(func, ast.Attribute) and func.attr in _SUBPROCESS_STARTERS
        and isinstance(func.value, ast.Name) and func.value.id == "subprocess"
    )


def _first_argv_element(node: ast.Call) -> ast.expr | None:
    """The first element of a subprocess call's argv, given as a positional list/tuple literal or an `args=`
    keyword one. None for anything else (a bare name, a concatenation): those are not what this scanner looks
    for, and are left alone rather than guessed at."""
    argv = node.args[0] if node.args else next((kw.value for kw in node.keywords if kw.arg == "args"), None)
    if isinstance(argv, (ast.List, ast.Tuple)) and argv.elts:
        return argv.elts[0]
    return None


def bare_git_call_sites(root: pathlib.Path) -> list[str]:
    """"path:line" (relative to `root`) of every subprocess call under it whose argv starts with the literal
    string "git", rather than something derived from gitexec.GIT (a starred `*gitexec.GIT`/`*_GIT`, a name, an
    attribute). Skips `<root>/fakes/` (test doubles standing in for a real Hermes or a real worker, never the
    controller) and `<root>/gates.py` (round 8 already scrubs its own environment; package GATESANDBOX is
    switching its git calls to gitexec on its own branch; the architect merges it). A literal "git" is exactly
    what an unrouted call looks like -- see gitexec.py's module docstring for why that matters."""
    hits = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if rel.parts[0] == "fakes" or rel == pathlib.Path("gates.py"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_subprocess_call(node):
                first = _first_argv_element(node)
                if isinstance(first, ast.Constant) and first.value == "git":
                    hits.append(f"{rel.as_posix()}:{node.lineno}")
    return hits


def test_no_bare_git_subprocess_call_bypasses_gitexec_outside_fakes_and_gates():
    hits = bare_git_call_sites(_SRC_ROOT)
    assert hits == [], f"subprocess call(s) start a literal 'git' argv, bypassing gitexec.GIT: {hits}"


def test_the_scanner_itself_catches_a_planted_bare_git_call(tmp_path):
    """Proves the scanner above is not vacuously green: planting exactly the kind of call round 9 removed makes
    it fail, naming the file and line."""
    (tmp_path / "planted.py").write_text(
        "import subprocess\n\n\ndef f(repo):\n    return subprocess.run([\"git\", \"-C\", repo, \"status\"])\n",
        encoding="utf-8",
    )
    assert bare_git_call_sites(tmp_path) == ["planted.py:5"]


def test_the_scanner_ignores_fakes_and_gates_even_with_a_bare_call(tmp_path):
    (tmp_path / "gates.py").write_text("import subprocess\nsubprocess.run([\"git\", \"status\"])\n", encoding="utf-8")
    fakes = tmp_path / "fakes"
    fakes.mkdir()
    (fakes / "board.py").write_text("import subprocess\nsubprocess.run([\"git\", \"status\"])\n", encoding="utf-8")

    assert bare_git_call_sites(tmp_path) == []


def test_a_call_routed_through_gitexec_is_not_flagged(tmp_path):
    (tmp_path / "routed.py").write_text(
        "import subprocess\nfrom ases import gitexec\n"
        "subprocess.run([*gitexec.GIT, \"-C\", \"x\", \"status\"], env=gitexec.git_env())\n",
        encoding="utf-8",
    )
    assert bare_git_call_sites(tmp_path) == []


# --- GIT, git_env, DIFF_SAFETY themselves --------------------------------------------------------------------

def test_GIT_starts_with_git_and_names_an_existing_empty_hooks_directory_plus_fsmonitor_off():
    assert gitexec.GIT[0] == "git"
    hooks_flags = [f for f in gitexec.GIT if f.startswith("core.hooksPath=")]
    assert len(hooks_flags) == 1
    hooks_dir = pathlib.Path(hooks_flags[0].split("=", 1)[1])
    assert hooks_dir.is_dir()
    assert list(hooks_dir.iterdir()) == []  # nothing in this codebase ever writes to it
    assert "core.fsmonitor=false" in gitexec.GIT


def test_GIT_is_the_same_tuple_object_every_time_one_hooks_directory_per_process():
    assert gitexec.GIT is gitexec.GIT  # re-import would be a separate process; within one, it never changes


def test_git_env_scrubs_credential_shaped_variables_but_keeps_the_git_author_exemption(monkeypatch):
    monkeypatch.setenv("X_FAKE_API_KEY", "dropped")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "kept")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "kept@example.invalid")
    monkeypatch.setenv("ASES_HARMLESS_SETTING", "kept too")

    env = gitexec.git_env()

    assert "X_FAKE_API_KEY" not in env
    assert env["GIT_AUTHOR_NAME"] == "kept"
    assert env["GIT_AUTHOR_EMAIL"] == "kept@example.invalid"
    assert env["ASES_HARMLESS_SETTING"] == "kept too"


def test_git_env_disables_the_terminal_prompt():
    assert gitexec.git_env()["GIT_TERMINAL_PROMPT"] == "0"


def test_DIFF_SAFETY_is_no_ext_diff_and_no_textconv():
    assert gitexec.DIFF_SAFETY == ("--no-ext-diff", "--no-textconv")


# --- the residual gitexec caps rather than closes (item 5 of the round 8 sweep) --------------------------------

def _git(args, cwd):
    result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result


def test_a_credential_shaped_variable_is_not_visible_to_a_filter_driver_that_still_runs(tmp_path, monkeypatch):
    """Item 5, the one thing gitexec cannot close by name: a filter driver's name is attacker-chosen
    (filter.<name>.clean/smudge), so no single -c pre-empts it, and it genuinely still runs. What gitexec caps is
    what the driver can SEE: git_env()'s scrub means a credential-shaped variable in the controller's own
    environment never reaches it.

    Before/after, in the same test: the identical filter, run with the controller's real (unscrubbed)
    environment -- exactly what a call with no env= at all used to do -- DOES read the credential, proving the
    first half's clean answer is the scrub's doing and not a fixture that never worked."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q", "-b", "integration"], repo)
    _git(["config", "user.email", "t@t"], repo)
    _git(["config", "user.name", "t"], repo)

    marker = tmp_path / "filter_saw.txt"
    script = tmp_path / "leak.py"
    script.write_text(
        "import os, pathlib, sys\n"
        f"pathlib.Path(r'{marker}').write_text(os.environ.get('X_FAKE_PROVIDER_KEY', '<absent>'))\n"
        "sys.stdout.write(sys.stdin.read())\n",
        encoding="utf-8",
    )
    filter_cmd = f'"{sys.executable}" "{script}"'
    _git(["config", "filter.leak.clean", filter_cmd], repo)
    _git(["config", "filter.leak.smudge", filter_cmd], repo)
    _git(["config", "filter.leak.required", "true"], repo)
    (repo / ".gitattributes").write_text("secret.txt filter=leak\n", encoding="utf-8")
    (repo / "secret.txt").write_text("hello\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "init"], repo)

    monkeypatch.setenv("X_FAKE_PROVIDER_KEY", "sk-should-not-leak-anywhere")

    (repo / "secret.txt").unlink()  # force the smudge filter to run again on checkout
    result = subprocess.run(
        [*gitexec.GIT, "-C", str(repo), "checkout", "-q", "--", "secret.txt"],
        capture_output=True, text=True, env=gitexec.git_env(),
    )
    assert result.returncode == 0, result.stderr
    assert marker.exists(), "the filter driver never ran at all -- a fixture problem, not what this test checks"
    assert marker.read_text() == "<absent>"

    (repo / "secret.txt").unlink()
    result2 = subprocess.run(  # no env= at all: the shape of an unrouted call, inheriting this process's env
        [*gitexec.GIT, "-C", str(repo), "checkout", "-q", "--", "secret.txt"], capture_output=True, text=True,
    )
    assert result2.returncode == 0, result2.stderr
    assert marker.read_text() == "sk-should-not-leak-anywhere"
