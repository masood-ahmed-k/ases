"""Gate runner (section 9.1: gates.py; section 14, ASES-QG-01/02/03/04).

Runs a gate profile's pinned commands against an exact commit SHA in a clean checkout, never in a
worker's live worktree -- so leftover files can't turn a red build green (ASES-QG-04). "Clean checkout"
here means a fresh git worktree at that commit. Where the commands run is the `runner` hook of run_gate:
by default they run locally (_run_commands), and the sandbox module supplies a runner that executes the
same commands inside a container (ASES-SEC-03). Without one, the commands run on the host: a known gap,
not hidden.

The controller believes only these records, never a worker's self-report (ASES-QG-01, section 14.2).

The text checks over a diff (the tamper check, the secret scan) live in tamper.py; detect_tamper and
scan_for_secrets here are their string-returning entry points for callers that want messages, not Findings.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import pathlib
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from datetime import datetime, timezone

from . import db as ases_db
from . import events as events_mod
from . import tamper as tamper_mod


@dataclasses.dataclass(frozen=True)
class GateResult:
    gate: str
    commit_sha: str
    passed: bool
    detail: str


def _run_commands(cwd: pathlib.Path, commands: list[str], timeout: int) -> tuple[bool, str]:
    lines = []
    for cmd in commands:
        try:
            result = subprocess.run(
                cmd, shell=True, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
            )
        except subprocess.TimeoutExpired:
            lines.append(f"$ {cmd}\n[TIMEOUT after {timeout}s]")
            return False, "\n".join(lines)
        lines.append(f"$ {cmd}\n{result.stdout}{result.stderr}".rstrip())
        if result.returncode != 0:
            lines.append(f"[exit {result.returncode}]")
            return False, "\n".join(lines)
    return True, "\n".join(lines)


def run_gate(
    repo_path: pathlib.Path, commit_sha: str, gate_name: str, commands: list[str], *,
    conn=None, task_key: str = "", project: str | None = None, timeout_per_command: int = 120,
    runner: Callable[[pathlib.Path, list[str], int], tuple[bool, str]] | None = None,
) -> GateResult:
    """Checks out commit_sha into a throwaway worktree, runs commands there, tears it down.

    If conn is given, records the result in gate_runs (task_key, gate, commit_sha, result, detail, project).
    `project` (schema v7) scopes the row to the project that ran it, so two projects sharing this database, or
    reusing a task key, do not share gate history: see last_gate_result for how a reader tells them apart. Left
    at the default None, the row's project column is NULL, exactly as every row written before this column
    existed -- old behavior, unchanged.

    `runner` (ASES-QG-04, ASES-SEC-03) replaces the local command runner: a callable
    runner(worktree, commands, timeout_per_command) -> (passed, output). This is how the sandbox module runs
    the same commands inside a container instead of on the host. The checkout, the cleanup and the
    gate_runs row are the same whichever runner ran the commands, so the controller's gate record means the
    same thing either way. A runner that raises is not caught: the worktree is still torn down, no row is
    written, and the caller decides what an infrastructure failure means (it is not a red gate).

    ASES-SEC-01 ("nothing secret-shaped in stored gate output or in text handed to a card"): the command output
    is redacted ONCE here (events.redact_text), before it is stored in gate_runs.detail and before it is returned
    in GateResult.detail. Command output is the likeliest place for a secret (a failing test that prints its
    environment, a tool that echoes a token), and every consumer copies GateResult.detail somewhere else: a card
    comment, a fix-card body, a merge outcome. Redacting at the source means no consumer can forget to.
    """
    run_commands = runner if runner is not None else _run_commands
    tmp_root = pathlib.Path(tempfile.mkdtemp(prefix="ases-gate-"))
    worktree = tmp_root / "wt"
    try:
        add = subprocess.run(
            ["git", "-C", str(repo_path), "worktree", "add", "--detach", str(worktree), commit_sha],
            capture_output=True, text=True, timeout=60,
        )
        if add.returncode != 0:
            passed, output = False, f"could not create gate worktree: {add.stdout}{add.stderr}"
        else:
            passed, output = run_commands(worktree, commands, timeout_per_command)
    finally:
        subprocess.run(
            ["git", "-C", str(repo_path), "worktree", "remove", "--force", str(worktree)],
            capture_output=True, text=True, timeout=60,
        )
        shutil.rmtree(tmp_root, ignore_errors=True)

    # A runner is expected to hand back text; anything else is passed through untouched rather than crashing a
    # gate that already ran.
    detail = events_mod.redact_text(output) if isinstance(output, str) else output
    result = GateResult(gate_name, commit_sha, passed, detail)

    if conn is not None:
        conn.execute(
            "INSERT INTO gate_runs (task_key, gate, commit_sha, result, detail, ran_at, project) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_key, gate_name, commit_sha, "pass" if result.passed else "fail", result.detail,
             datetime.now(timezone.utc).isoformat(timespec="seconds"), project),
        )
    return result


def last_gate_result(conn, task_key: str, gate: str, commit_sha: str, *, project: str | None = None) -> str | None:
    """The most recent gate_runs.result for this task_key, gate and commit_sha, or None with no row.

    `project`, when given, scopes the match to rows this project wrote OR a row with no project at all (a NULL
    column: written before schema v7 added it, or by a caller that did not pass one to run_gate). A NULL row
    still counts so history from before this change, or from a caller that has not been updated yet, is never
    orphaned; it is only a DIFFERENT project's row, stamped with a project that is neither this one nor NULL,
    that is excluded. Left at the default None (the old call shape), nothing is filtered by project at all --
    every caller's behavior is exactly what it was before this parameter existed."""
    if project is None:
        row = conn.execute(
            "SELECT result FROM gate_runs WHERE task_key = ? AND gate = ? AND commit_sha = ? "
            "ORDER BY id DESC LIMIT 1",
            (task_key, gate, commit_sha),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT result FROM gate_runs WHERE task_key = ? AND gate = ? AND commit_sha = ? "
            "AND (project IS NULL OR project = ?) ORDER BY id DESC LIMIT 1",
            (task_key, gate, commit_sha, project),
        ).fetchone()
    return row["result"] if row else None


def scan_for_secrets(diff_text: str) -> list[str]:
    """ASES-SEC-01 / ASES-GIT-07: secret scan (Gates 1 and 3). Reads only the ADDED lines of the diff, through
    tamper.secret_hint (events.py's pattern set, which is the one place that defines what a provider key
    looks like, plus the few shapes a diff can carry that an event payload never does), and reports an added
    FILE whose name marks it as holding secrets (.env, *.pem, id_rsa) even when its content matches nothing.

    A finding names the file and the line and says what kind of secret it looks like; it NEVER contains the
    matched text. It used to echo the first 80 characters of the offending line, so the message that reported
    a leaked key printed the key into a card comment and a log. A bare "+text" line, as questions.py hands
    over one at a time, is scanned as an added line of a file with no name."""
    findings = []
    for fd in tamper_mod.parse_diff(diff_text):
        if fd.status == "A" and tamper_mod.is_secret_file(fd.path):
            findings.append(tamper_mod.format_finding(tamper_mod.Finding(
                "secret_added", fd.path, "possible secret added: a file whose name marks it as holding secrets",
            )))
        for number, text in fd.added_lines:
            hint = tamper_mod.secret_hint(text)
            if hint:
                findings.append(tamper_mod.format_finding(tamper_mod.Finding(
                    "secret_added", fd.path, f"possible secret added: secret-shaped value ({hint})", number,
                )))
    return findings


def detect_tamper(diff_text: str) -> list[str]:
    """ASES-QG-03 over the text of a diff, as one string per finding (kind, path and line, then why). Delegates
    to tamper.analyze_diff, which does the real work and returns Findings; the old cheap markers (a removed
    test function, @pytest.mark.skip, `|| true`, xfail) are all covered by it. Flags for the controller and a
    reviewer to confirm, it does not prove a test still tests what it did. With no allow_paths given, nothing
    is treated as the task's own territory: use tamper.analyze_diff or tamper.check_range for that."""
    return [tamper_mod.format_finding(finding) for finding in tamper_mod.analyze_diff(diff_text)]


def hash_gate_profiles(gate_profiles: dict[str, list[str]]) -> str:
    """ASES-QG-02: a stable content hash of a plan's gate profiles, pinned in controller config (see
    controller.pin_gate_profiles / verify_gate_pin) so a later diff that quietly edits gate
    configuration, CI scripts, or test-runner settings is caught instead of trusted.

    `sort_keys=True` makes profile-NAME order irrelevant; command order within a profile's own list is
    preserved -- reordering commands still changes the hash. That's deliberately stricter than a set
    comparison would be, since any textual change to what a profile actually does should force
    re-approval."""
    encoded = json.dumps(gate_profiles, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
