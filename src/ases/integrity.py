"""Workspace integrity: touches-path enforcement and worktree snapshots (section 9.1: integrity.py;
ASES-GIT-12, -13).

Two checks: (1) does a diff stay inside the paths a card declared it would touch, and (2) did a
worker's run change anything outside its own worktree. (2) needs a real snapshot taken before and after
a real dispatched run, so it's only exercised end-to-end (Phase 3's live test); this module provides
the comparison primitive and is fully unit-testable on its own.

Round 19 (package GIT12, ASES-GIT-12, report-mode rollout): `attribute()` is the pure half of the
per-poll bracketing design in r19/GIT12.md ("Put the logic where blueprint table 15 puts it: integrity.py
... holds a pure attribute() with no git"). It decides which Hermes run(s), if any, could explain a
change guards.observe_worktrees found, from plain data alone: no git, no Hermes, no database. guards.py
collects the git state and hermes.board_runs supplies the candidate runs; this module only judges them.
"""
from __future__ import annotations

import dataclasses
import fnmatch
import pathlib
import subprocess
from collections.abc import Iterable

from . import gitexec

# ASES-GIT-12 design A2's own defaults (r19/GIT12.md): the clock-skew pad applied to a run's own effective
# window, and the reap tail (Hermes's TERMINAL_WORKER_REAP_GRACE_SECONDS=120, plus its 60s dispatch tick, plus a
# 5s margin) a run stays a candidate for after ended_at, because a worker can outlive the row that closed it.
DEFAULT_SKEW_SECONDS = 2.0
DEFAULT_REAP_TAIL_SECONDS = 185.0

# integrity.attribute's own verdicts (r19/GIT12.md design A5): "own" and "none" never carry a security_event;
# "unique" and "ambiguous" always do, whatever config/swarm.yaml's integrity.enforce_attribution says -- that flag
# only gates whether the finding is ACTED on, never whether it is recorded (architect decision, round 19).
ATTRIBUTION_OWN = "own"
ATTRIBUTION_UNIQUE = "unique"
ATTRIBUTION_AMBIGUOUS = "ambiguous"
ATTRIBUTION_NONE = "none"


def changed_paths(repo: pathlib.Path, commit_sha: str) -> list[str]:
    """Paths touched by commit_sha relative to its first parent (or, if it has none, the paths in
    that initial commit)."""
    result = subprocess.run(
        [*gitexec.GIT, "-C", str(repo), "diff-tree", "--no-commit-id", "--name-only", "-r", commit_sha],
        capture_output=True, text=True, env=gitexec.git_env(),
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
        [*gitexec.GIT, "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True, env=gitexec.git_env(),
    ).stdout.strip()
    status = subprocess.run(
        [*gitexec.GIT, "-C", str(path), "status", "--porcelain"], capture_output=True, text=True, env=gitexec.git_env(),
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


# ---------------------------------------------------------------------------------------------
# Per-poll bracketing attribution (round 19, package GIT12; ASES-GIT-12, report mode)
# ---------------------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class RunCandidate:
    """One Hermes run, reduced to exactly what attribute() needs to place it in time and attribute it to a card:
    a slice of hermes.RunWindow. `task_id` is the Hermes card id that owns the run (the reviewer's run carries
    the SAME task_id as the implementer's, which is how design A5(b) makes "the reviewer counts alike" fall out
    of plain equality, with no special case)."""
    run_id: int
    task_id: str
    started_at: float
    ended_at: float | None
    worker_pid: int | None


@dataclasses.dataclass(frozen=True)
class Attribution:
    """attribute()'s verdict for one changed subject (r19/GIT12.md design A5): `verdict` is one of the
    ATTRIBUTION_* constants above. `candidates` names every FOREIGN run found (never the owner's own), as
    (task_id, run_id) pairs, deduplicated and sorted; empty for "own" and "none"."""
    verdict: str
    candidates: tuple[tuple[str, int], ...]


def _effective_window(run: RunCandidate, *, now: float, skew: float, tail: float) -> tuple[float, float]:
    """A run's own window (design A2): begin is its claim time minus the skew pad; end is `now` while it is
    still open (ended_at is None) or while Hermes still holds worker_pid past ended_at + tail (a worker can
    outlive the row that closed its run -- kanban_db._end_run keeps worker_pid deliberately, and
    _reclaim_dangling_run clears it with no liveness proof, so a NULL pid is never treated as proof of death
    here; this function only ever sees what the caller read from Hermes at `now`), else ended_at + tail."""
    begin = run.started_at - skew
    if run.ended_at is None:
        return begin, now
    end = run.ended_at + tail
    if run.worker_pid is not None:
        end = max(end, now)
    return begin, end


def _overlaps(window: tuple[float, float], interval: tuple[float, float]) -> bool:
    (w_begin, w_end), (i_begin, i_end) = window, interval
    return w_begin <= i_end and w_end >= i_begin


def attribute(
    runs: Iterable[RunCandidate], interval: tuple[float, float], *, owner_card: str | None, now: float,
    skew: float = DEFAULT_SKEW_SECONDS, tail: float = DEFAULT_REAP_TAIL_SECONDS,
) -> Attribution:
    """ASES-GIT-12 (r19/GIT12.md design A1, A5): which run(s), if any, could have made a change to one subject
    (a worktree, in this round's scope) whose baseline interval is `interval` -- the caller (guards.py, which
    collects the git state) computes it as [baseline.confirmed_begin - skew, current.read_end + skew], the SAME
    skew this function pads every run's own window with, so the two are directly comparable.

    A run is a CANDIDATE exactly when its effective window (_effective_window) overlaps `interval` at all.
    "own": `owner_card` (the live card this subject belongs to, or None for a subject with no owner) has any
    candidate run at all -- the implementer and the reviewer count alike, because a review run is a task_runs
    row of the same task id (design A5(b)). Otherwise, among the candidates that are NOT the owner's own:
    "unique" when they all belong to exactly one other card, "ambiguous" when two or more distinct cards do,
    "none" when there are no candidates at all. Pure: no git, no Hermes, no database, so every one of these is
    just arithmetic over its arguments and is exercised with plain data, never a real repository or board."""
    candidates = [run for run in runs if _overlaps(_effective_window(run, now=now, skew=skew, tail=tail), interval)]
    if owner_card is not None and any(run.task_id == owner_card for run in candidates):
        return Attribution(ATTRIBUTION_OWN, ())
    foreign = sorted({(run.task_id, run.run_id) for run in candidates if run.task_id != owner_card})
    if not foreign:
        return Attribution(ATTRIBUTION_NONE, ())
    distinct_cards = {task_id for task_id, _ in foreign}
    if len(distinct_cards) == 1:
        return Attribution(ATTRIBUTION_UNIQUE, tuple(foreign))
    return Attribution(ATTRIBUTION_AMBIGUOUS, tuple(foreign))
