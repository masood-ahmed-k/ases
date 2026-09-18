"""Gate runner (section 9.1: gates.py; section 14, ASES-QG-01/02/04).

Runs a gate profile's pinned commands against an exact commit SHA in a clean checkout, never in a
worker's live worktree -- so leftover files can't turn a red build green (ASES-QG-04). "Clean checkout"
here means a fresh git worktree at that commit, not a Docker sandbox: full sandbox isolation is a
Phase 5 requirement (ASES-SEC-03) that doesn't exist yet. Documented as a known gap, not hidden.

The controller believes only these records, never a worker's self-report (ASES-QG-01, section 14.2).
"""
from __future__ import annotations

import dataclasses
import pathlib
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone

from . import db as ases_db


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
    conn=None, task_key: str = "", timeout_per_command: int = 120,
) -> GateResult:
    """Checks out commit_sha into a throwaway worktree, runs commands there, tears it down.

    If conn is given, records the result in gate_runs (task_key, gate, commit_sha, result, detail).
    """
    tmp_root = pathlib.Path(tempfile.mkdtemp(prefix="ases-gate-"))
    worktree = tmp_root / "wt"
    try:
        add = subprocess.run(
            ["git", "-C", str(repo_path), "worktree", "add", "--detach", str(worktree), commit_sha],
            capture_output=True, text=True, timeout=60,
        )
        if add.returncode != 0:
            result = GateResult(gate_name, commit_sha, False,
                                 f"could not create gate worktree: {add.stdout}{add.stderr}")
        else:
            passed, detail = _run_commands(worktree, commands, timeout_per_command)
            result = GateResult(gate_name, commit_sha, passed, detail)
    finally:
        subprocess.run(
            ["git", "-C", str(repo_path), "worktree", "remove", "--force", str(worktree)],
            capture_output=True, text=True, timeout=60,
        )
        shutil.rmtree(tmp_root, ignore_errors=True)

    if conn is not None:
        conn.execute(
            "INSERT INTO gate_runs (task_key, gate, commit_sha, result, detail, ran_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (task_key, gate_name, commit_sha, "pass" if result.passed else "fail", result.detail,
             datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
    return result


def last_gate_result(conn, task_key: str, gate: str, commit_sha: str) -> str | None:
    row = conn.execute(
        "SELECT result FROM gate_runs WHERE task_key = ? AND gate = ? AND commit_sha = ? "
        "ORDER BY id DESC LIMIT 1",
        (task_key, gate, commit_sha),
    ).fetchone()
    return row["result"] if row else None


def detect_tamper(diff_text: str) -> list[str]:
    """Cheap heuristics for ASES-QG-03: deleted/skipped tests, unconditional passes, weakened
    assertions. Not a substitute for a real diff-aware checker; flags for a human/reviewer to confirm."""
    findings = []
    lowered = diff_text.lower()
    if "-    def test_" in diff_text or "-def test_" in diff_text:
        findings.append("a test function appears to have been deleted")
    for marker in ("@pytest.mark.skip", "skipif(true", "|| true", "# noqa: test", "xfail(strict=false"):
        if marker in lowered:
            findings.append(f"suspicious marker found: {marker!r}")
    return findings
