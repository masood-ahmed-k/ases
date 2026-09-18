"""Workspace integrity: touches-path enforcement and worktree snapshots (section 9.1: integrity.py;
ASES-GIT-12, -13).

Two checks: (1) does a diff stay inside the paths a card declared it would touch, and (2) did a
worker's run change anything outside its own worktree. (2) needs a real snapshot taken before and after
a real dispatched run, so it's only exercised end-to-end (Phase 3's live test); this module provides
the comparison primitive and is fully unit-testable on its own.
"""
from __future__ import annotations

import dataclasses
import fnmatch
import pathlib
import subprocess


def changed_paths(repo: pathlib.Path, commit_sha: str) -> list[str]:
    """Paths touched by commit_sha relative to its first parent (or, if it has none, the paths in
    that initial commit)."""
    result = subprocess.run(
        ["git", "-C", str(repo), "diff-tree", "--no-commit-id", "--name-only", "-r", commit_sha],
        capture_output=True, text=True,
    )
    return [ln for ln in result.stdout.splitlines() if ln.strip()]


def paths_outside_touches(changed: list[str], touches: list[str]) -> list[str]:
    """ASES-GIT-13: which changed paths fall outside every declared glob. An empty touches list
    means the task declared no path changes at all -- everything changed is then out of scope."""
    if not touches:
        return list(changed)
    return [p for p in changed if not any(fnmatch.fnmatch(p, glob) for glob in touches)]


@dataclasses.dataclass(frozen=True)
class WorktreeSnapshot:
    path: pathlib.Path
    head: str
    dirty_paths: tuple[str, ...]


def snapshot(path: pathlib.Path) -> WorktreeSnapshot:
    head = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "-C", str(path), "status", "--porcelain"], capture_output=True, text=True,
    ).stdout
    dirty = tuple(ln[3:] for ln in status.splitlines() if ln.strip())
    return WorktreeSnapshot(path, head, dirty)


def diff_snapshots(before: WorktreeSnapshot, after: WorktreeSnapshot) -> list[str]:
    """ASES-GIT-12: what changed in a worktree ASES did not expect to touch. Returns a description
    per unexpected change; empty means the worktree matches what was expected."""
    findings = []
    if before.head != after.head:
        findings.append(f"HEAD moved: {before.head} -> {after.head}")
    newly_dirty = set(after.dirty_paths) - set(before.dirty_paths)
    for p in sorted(newly_dirty):
        findings.append(f"unexpected working-tree change: {p}")
    return findings
