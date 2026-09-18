"""Merge queue (section 9.1: mergeq.py; section 8.1, ASES-GIT-04/05/06).

One merge at a time. Squash the work branch onto the integration HEAD in a throwaway worktree, run
Gate 3 on that exact candidate commit, fast-forward the real integration branch only if green, then the
merge card is done. A post-merge failure reverts the squash commit rather than leaving a red branch.

The controller is the only thing that ever writes to the integration branch (ASES-GIT-02) -- this
module is where that happens; nothing else in ASES calls `git merge` or `git push` on it.
"""
from __future__ import annotations

import dataclasses
import pathlib
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone

from . import gates as gates_mod


class MergeConflict(Exception):
    pass


@dataclasses.dataclass(frozen=True)
class MergeOutcome:
    merged: bool
    candidate_sha: str | None
    squash_commit: str | None
    gate3_result: str | None
    detail: str


def _git(args: list[str], cwd: pathlib.Path, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=timeout)


def _head_sha(repo: pathlib.Path) -> str:
    return _git(["rev-parse", "HEAD"], repo).stdout.strip()


def merge_task(
    repo: pathlib.Path, integration_branch: str, work_branch: str, task_key: str,
    gate3_commands: list[str], *, conn=None, commit_message: str | None = None,
) -> MergeOutcome:
    """ASES-GIT-04: squash candidate on integration HEAD -> Gate 3 -> fast-forward -> done.
    A merge conflict or a red Gate 3 leaves the integration branch untouched and returns merged=False;
    the caller (review.py / the controller loop) is responsible for opening a fix card."""
    tmp_root = pathlib.Path(tempfile.mkdtemp(prefix="ases-merge-"))
    candidate = tmp_root / "candidate"
    try:
        base_sha = _head_sha(repo)
        add = _git(["worktree", "add", "--detach", str(candidate), integration_branch], repo)
        if add.returncode != 0:
            return MergeOutcome(False, None, None, None, f"could not create candidate worktree: {add.stderr}")

        squash = _git(["merge", "--squash", work_branch], candidate)
        if squash.returncode != 0:
            _git(["merge", "--abort"], candidate)
            return MergeOutcome(False, None, None, None, f"merge conflict: {squash.stdout}{squash.stderr}")

        msg = commit_message or f"{task_key}: merge {work_branch}"
        commit = _git(["commit", "-q", "-m", msg], candidate)
        if commit.returncode != 0:
            return MergeOutcome(False, None, None, None, f"nothing to commit: {commit.stdout}{commit.stderr}")

        candidate_sha = _head_sha(candidate)
        gate_result = gates_mod.run_gate(
            candidate, candidate_sha, "gate3", gate3_commands, conn=conn, task_key=task_key,
        )
        if conn is not None:
            conn.execute(
                "INSERT INTO merge_records (task_key, candidate_sha, gate3_result, squash_commit, "
                "reverted, completed_at) VALUES (?, ?, ?, NULL, 0, NULL) "
                "ON CONFLICT(task_key) DO UPDATE SET candidate_sha=excluded.candidate_sha, "
                "gate3_result=excluded.gate3_result",
                (task_key, candidate_sha, "pass" if gate_result.passed else "fail"),
            )
        if not gate_result.passed:
            return MergeOutcome(False, candidate_sha, None, "fail", gate_result.detail)

        ff = _git(["merge", "--ff-only", candidate_sha], repo)
        if ff.returncode != 0:
            # The integration branch moved under us between base_sha and now (concurrent merge that
            # shouldn't happen if the queue is truly serialized, but fail safe rather than force).
            return MergeOutcome(False, candidate_sha, None, "pass",
                                 f"fast-forward refused, integration branch moved: {ff.stderr}")

        if conn is not None:
            conn.execute(
                "UPDATE merge_records SET squash_commit = ?, completed_at = ? WHERE task_key = ?",
                (candidate_sha, datetime.now(timezone.utc).isoformat(timespec="seconds"), task_key),
            )
        return MergeOutcome(True, candidate_sha, candidate_sha, "pass", "merged")
    finally:
        _git(["worktree", "remove", "--force", str(candidate)], repo)
        shutil.rmtree(tmp_root, ignore_errors=True)


def revert_merge(repo: pathlib.Path, squash_commit: str, *, conn=None, task_key: str = "") -> bool:
    """ASES-GIT-05: a post-merge failure reverts the squash commit rather than leaving the
    integration branch red."""
    result = _git(["revert", "--no-edit", squash_commit], repo)
    ok = result.returncode == 0
    if conn is not None and task_key:
        conn.execute("UPDATE merge_records SET reverted = 1 WHERE task_key = ?", (task_key,))
    return ok
