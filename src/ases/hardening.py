"""Hardening (blueprint section 16, phase 9): cleanup of worktrees and branches, log retention, database vacuum.

Phase 9 reads "Cleanup of worktrees and branches (hermes worktree prune), database migrations, log retention,
documentation, runbook." The migrations live in db.py; this module is the code half of the rest. ASES-OBS-02 (section
15.2): "Transcripts and logs stay local under a retention setting", default 30 days. ASES-ARC-03: every ASES record is
keyed by the card ID and the commit SHA, so what may be cleaned is decided from the board, git AND the ASES database
together (section 19.4), never from one of them alone.

Why ASES needs its own cleanup on top of `hermes worktree prune`. Hermes removes a finished card's worktree only when
the tree is clean AND every commit is reachable from a remote-tracking ref, and `hermes worktree prune` never touches
the kanban trees (`.worktrees/t_...`) at all. ASES has no remote and merges by SQUASH (mergeq.py), so its work branches
are never ancestors of the integration branch, are never "pushed", and their worktrees and `swarm/*` branches
would pile up for ever. A leftover candidate worktree from a merge that was killed (`ases-merge-*` in the system temp
directory, registered in git) is another thing only ASES knows how to recognise.

The rules that make every function here safe to run by hand:
  - DRY RUN BY DEFAULT. Nothing is removed unless apply=True, and a dry run and an apply decide exactly the same
    things (the dry run plans, apply then does what the plan says).
  - NEVER RAISES. Each function returns a report, and a failure becomes a line in it. One failure never stops the rest.
  - FAIL CLOSED. Whatever cannot be checked (a card Hermes will not show, a git that will not answer) is SKIPPED and
    REPORTED, never assumed safe. A card that is running, in review, ready, blocked or scheduled owns its worktree and
    branch, and nothing here touches them.
  - EVERYTHING INJECTABLE: the two Hermes reads, the clock and the temp directory, so no test needs a real board.
  - ASCII OUTPUT: every string in a report is safe for the Windows console (cp1252).
Every removal is an event `hardening_removed` (ASES-SEC-01: redacted like every event), with the commit a deleted branch
pointed at, so a branch removed by mistake can be put back with `git branch <name> <sha>` while git still has the commit.
"""
from __future__ import annotations

import dataclasses
import os
import pathlib
import sqlite3
import stat
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone

from . import events as events_mod
from . import guards as guards_mod
from . import hermes as hermes_mod
from . import intents as intents_mod

_GIT_TIMEOUT = 60        # seconds per read-only git call, the same as the merge queue's
_REMOVE_TIMEOUT = 300    # a worktree removal deletes a whole tree, which can be slow on Windows

# A card in one of these states has finished its work. Every other state (triage, todo, scheduled, ready, running,
# blocked, review, or one Hermes adds later) is treated as still owning its worktree and branch.
_FINISHED = frozenset({"done", "archived"})

KIND_STALE = "stale_worktree"          # registered in git, directory gone (git worktree prune would remove it)
KIND_CANDIDATE = "candidate_worktree"  # a merge-queue candidate worktree left behind (ases-merge-* in the temp dir)
KIND_CARD = "card_worktree"            # the worktree of a finished card, under <repo>/.worktrees/<card id>
KIND_BRANCH = "branch"                 # a local swarm/* or merge/* branch

_CANDIDATE_PREFIX = "ases-merge-"
_BRANCH_PREFIXES = ("swarm/", "merge/")
_DB_FILE_NAMES = frozenset({"ases.db", "ases.db-wal", "ases.db-shm"})


# ---------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------


def _ascii(text: object, limit: int = 300) -> str:
    """text made safe to print on a cp1252 console (anything that is not ASCII becomes a backslash escape), on one
    line and capped, so a card title, a path or a git message can go into any report."""
    flat = " ".join(str(text).split())
    return flat[:limit].encode("ascii", "backslashreplace").decode("ascii")


def _aware(moment: datetime | None) -> datetime:
    """`moment` as an aware UTC datetime; None is now, and a naive datetime is taken as UTC."""
    if moment is None:
        return datetime.now(timezone.utc)
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)


def _key(path: str | os.PathLike) -> str:
    """A path in the one spelling used to compare paths: links and Windows short names resolved, separators unified
    and, on Windows, case folded (git prints forward slashes, Python backslashes, and the two differ in case)."""
    text = os.fspath(path)
    try:
        text = os.path.realpath(text)
    except (OSError, ValueError):
        text = os.path.normpath(text)
    return os.path.normcase(text)


def _inside(path: str | os.PathLike, root_key: str) -> bool:
    """Is `path` the directory `root_key` (already a _key) or inside it?"""
    where = _key(path)
    return where == root_key or where.startswith(root_key.rstrip(os.sep) + os.sep)


def _git(repo: str | os.PathLike, args: list[str], *, timeout: int = _GIT_TIMEOUT, read_only: bool = True) -> tuple[int, str, str]:
    """(exit code, stdout, stderr) of one git command run in `repo`. A read-only call passes --no-optional-locks, so
    it can never take index.lock from a git operation that is really running. The child's working directory is the
    repository, not wherever the operator stands: on Windows a directory that is some process's current directory
    cannot be deleted. A git that cannot be started or does not answer in time is exit code -1, so nothing raises."""
    argv = ["git", *(["--no-optional-locks"] if read_only else []), "-C", str(repo), *args]
    try:
        result = subprocess.run(argv, capture_output=True, timeout=timeout, cwd=str(repo))
    except subprocess.TimeoutExpired:
        return -1, "", f"git {args[0]} timed out after {timeout}s"
    except OSError as exc:
        return -1, "", f"git could not be run: {exc}"
    return (
        result.returncode,
        result.stdout.decode("utf-8", errors="replace"),
        result.stderr.decode("utf-8", errors="replace"),
    )


def _first_line(text: str) -> str:
    lines = text.strip().splitlines()
    return _ascii(lines[0], 200) if lines else ""


def _fmt_bytes(size: int) -> str:
    """A byte count for a person: 512 B, 1.5 KB, 3.2 MB, 1.1 GB."""
    if size < 1024:
        return f"{int(size)} B"
    value = float(size)
    for unit in ("KB", "MB", "GB"):
        value /= 1024
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
    return f"{size} B"


def _tasks_for_branch(branch: str, task_keys) -> list[str]:
    """EVERY plan task key a branch name could belong to (usually none or one), sorted. ASES names a work branch
    `swarm/<task key>-<role>` (a fix branch `swarm/<key>-fix<n>`, a retry `swarm/<key>-retry<n>`), and the blueprint's
    merge branch is `merge/<key>`. The key is a free-form string, so a name matches a key it equals or continues with a
    dash: with tasks T1 and T1-a, swarm/T1-a-coder matches BOTH."""
    for prefix in _BRANCH_PREFIXES:
        if branch.startswith(prefix):
            rest = branch[len(prefix):]
            break
    else:
        return []
    return sorted(key for key in task_keys if rest == key or rest.startswith(key + "-"))


def _task_for_branch(branch: str, task_keys) -> str | None:
    """The plan task a branch belongs to, or None when there is none OR when it is ambiguous. Deciding a branch's fate
    from the wrong task could delete a live branch, so two candidate keys (T1 and T1-a for swarm/T1-a-coder) fail closed:
    the branch is left alone and the report says why."""
    matches = _tasks_for_branch(branch, task_keys)
    return matches[0] if len(matches) == 1 else None


# ---------------------------------------------------------------------------------------------
# clean: worktrees and branches
# ---------------------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class CleanItem:
    """One worktree or branch and why it is (or is not) being removed. kind is one of the KIND_* constants (or
    "error" for a problem that is not one item). name is the path or branch name, ASCII-escaped."""
    kind: str
    name: str
    reason: str


@dataclasses.dataclass
class CleanReport:
    """What one clean() found. candidates: everything that is safe to remove, in a dry run and in an apply alike (each
    with the reason). removed: the candidates that were actually removed (apply only). skipped: looked at and left,
    with the reason it was left. errors: what went wrong (a removal git refused, a board that could not be read); a
    failure of one item never hides the others."""
    repo: str
    integration_branch: str
    apply: bool
    candidates: list[CleanItem] = dataclasses.field(default_factory=list)
    removed: list[CleanItem] = dataclasses.field(default_factory=list)
    skipped: list[CleanItem] = dataclasses.field(default_factory=list)
    errors: list[CleanItem] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class _Planned:
    item: CleanItem
    info: guards_mod.WorktreeInfo
    mode: str  # "stale" | "candidate" | "card"


class _Cleaner:
    """One clean() run. A class only so the many facts it gathers (cards, plan, worktrees) are not threaded through
    every function; nothing about it is public."""

    def __init__(
        self, repo, integration_branch, *, board, conn, plan_project, apply, kanban_show, kanban_list, temp_root,
        now, candidate_min_age_minutes, card_worktrees,
    ):
        self.repo = pathlib.Path(repo)
        self.integration = integration_branch
        self.board = board
        self.conn = conn
        self.project = plan_project
        self.apply = bool(apply)
        # Resolved at call time, not in the signature, so a test that patches hermes.kanban_show cannot reach a real board.
        self.show = kanban_show or hermes_mod.kanban_show
        self.list = kanban_list or hermes_mod.kanban_list
        self.temp_root = pathlib.Path(temp_root) if temp_root is not None else pathlib.Path(tempfile.gettempdir())
        self.now = _aware(now)
        self.min_age = timedelta(minutes=candidate_min_age_minutes)
        self.card_worktrees = bool(card_worktrees)
        self.report = CleanReport(repo=_ascii(self.repo), integration_branch=_ascii(integration_branch), apply=self.apply)
        self.worktrees: list[guards_mod.WorktreeInfo] = []
        self._cards: dict[str, tuple[dict | None, str]] = {}
        self._tasks: dict[str, tuple[str | None, str | None]] = {}
        self._finished_cache: dict[str, tuple[bool, str]] = {}
        self._active_paths: dict[str, tuple[str, str]] = {}
        self._active_branches: dict[str, tuple[str, str]] = {}
        self._list_error = ""
        self._open_builds: list[dict] = []

    # -- reporting ----------------------------------------------------------------------------

    def _add(self, bucket: list, kind: str, name: object, reason: str) -> None:
        bucket.append(CleanItem(kind, _ascii(name), _ascii(reason, 400)))

    def _skip(self, kind: str, name: object, reason: str) -> None:
        self._add(self.report.skipped, kind, name, reason)

    def _error(self, kind: str, name: object, reason: str) -> None:
        self._add(self.report.errors, kind, name, reason)

    def _record_removal(self, item: CleanItem, **extra) -> None:
        """The audit trail: one hardening_removed event per removal. A failure to write it is an error line, not a
        reason to stop (the removal already happened)."""
        try:
            events_mod.record(self.conn, "hardening_removed", {
                "project": self.project, "kind": item.kind, "name": item.name, "reason": item.reason, **extra,
            })
        except Exception as exc:  # noqa: BLE001 - reporting, never raising
            self._error("event", item.name, f"removed, but the hardening_removed event could not be written: {exc}")

    def _guarded(self, what: str, step, default=None):
        """step()'s result, or `default` after recording the failure: one broken phase must not hide the others."""
        try:
            return step()
        except Exception as exc:  # noqa: BLE001
            self._error("error", what, f"{type(exc).__name__}: {exc}")
            return default

    # -- the run ------------------------------------------------------------------------------

    def run(self) -> CleanReport:
        if self.conn is None:
            self._error("error", "database", "no database connection was given: merge records and plan cards cannot be checked")
            return self.report
        if not self._preconditions():
            return self.report
        self._guarded("reading the board", self._read_board)
        self._guarded("reading the plan", self._read_plan)
        planned: list[_Planned] = []
        self._guarded("looking at the worktrees", lambda: self._plan_worktrees(planned))
        remaining = self._remove_worktrees(planned)
        if remaining is not None:
            self._guarded("looking at the branches", lambda: self._branches(remaining))
        return self.report

    def _preconditions(self) -> bool:
        code, _out, err = _git(self.repo, ["rev-parse", "--git-dir"])
        if code != 0:
            self._error("error", self.repo, f"not a git repository, or git failed: {_first_line(err)}")
            return False
        code, out, _err = _git(self.repo, ["rev-parse", "--verify", "-q", f"refs/heads/{self.integration}"])
        if code != 0 or not out.strip():
            self._error("error", self.integration,
                        f"the integration branch does not exist in {self.repo}, so nothing can be judged merged")
            return False
        self.worktrees = guards_mod.list_worktrees(self.repo)
        if not self.worktrees:
            self._error("error", self.repo, "git could not list the worktrees, so nothing is safe to remove")
            return False
        return True

    # -- what the board and the plan say --------------------------------------------------------

    def _read_board(self) -> None:
        """Every card that is not finished, by workspace path and branch. They are protected whatever else is decided,
        which is what covers a fix or retry card that is no longer in plan_tasks. If the list cannot be read, or an
        entry in it cannot be understood, nothing that depends on it may be removed (fail closed): _list_error says so
        to every check that needs it, and the half-built maps are thrown away."""
        active_paths: dict[str, tuple[str, str]] = {}
        active_branches: dict[str, tuple[str, str]] = {}
        try:
            for card in self.list(self.board) or []:
                status = str(card.get("status") or "")
                if status in _FINISHED:
                    continue
                card_id = str(card.get("id") or "?")
                if card.get("workspace_path"):
                    active_paths[_key(card["workspace_path"])] = (card_id, status)
                if card.get("branch_name"):
                    active_branches[str(card["branch_name"])] = (card_id, status)
        except Exception as exc:  # noqa: BLE001
            self._list_error = _ascii(f"{type(exc).__name__}: {exc}", 160)
            self._error("error", f"board {self.board}",
                        f"could not list the cards ({self._list_error}); no worktree or branch is removed without it")
            return
        self._active_paths, self._active_branches = active_paths, active_branches

    def _read_plan(self) -> None:
        rows = self.conn.execute(
            "SELECT task_key, work_card_id, merge_card_id FROM plan_tasks WHERE project = ?", (self.project,),
        ).fetchall()
        self._tasks = {row[0]: (row[1], row[2]) for row in rows}
        self._open_builds = [
            item for item in intents_mod.open_intents(self.conn, self.project)
            if item["kind"] in (intents_mod.KIND_BUILD_CANDIDATE, intents_mod.KIND_FAST_FORWARD)
        ]

    def _card(self, card_id: str) -> tuple[dict | None, str]:
        """(card, "") or (None, why it could not be read). Asked once per card."""
        if card_id not in self._cards:
            try:
                card = self.show(self.board, card_id)
                self._cards[card_id] = (card, "") if isinstance(card, dict) else (None, "Hermes gave an unexpected answer")
            except Exception as exc:  # noqa: BLE001
                self._cards[card_id] = (None, _ascii(f"{type(exc).__name__}: {exc}", 160))
        return self._cards[card_id]

    def _task_finished(self, key: str) -> tuple[bool, str]:
        """Is the plan task finished: its CURRENT work card and its merge card both done or archived? A card that cannot
        be read makes the answer no, with the reason. (The work card is whichever card plan_tasks points at now: a fix
        or retry card replaces the original there, so an old card that was replaced does not decide.)"""
        if key not in self._finished_cache:
            work_id, merge_id = self._tasks[key]
            answer: tuple[bool, str] = (True, "")
            for label, card_id in (("work", work_id), ("merge", merge_id)):
                if not card_id:
                    answer = (False, f"task {key} has no {label} card recorded")
                    break
                card, why = self._card(card_id)
                if card is None:
                    answer = (False, f"card {card_id} ({label} card of task {key}) could not be read: {why}")
                    break
                status = str(card.get("status") or "")
                if status not in _FINISHED:
                    answer = (False, f"{label} card {card_id} of task {key} is {status or 'in an unknown state'}")
                    break
            self._finished_cache[key] = answer
        return self._finished_cache[key]

    def _card_id_of_worktree(self, path: pathlib.Path) -> str | None:
        """Hermes puts a project card's worktree at <repo>/.worktrees/<card id>. Anything else is not a card worktree
        as far as ASES can tell, and is left alone."""
        primary = self.worktrees[0].path
        if path.parent.name == ".worktrees" and _key(path.parent.parent) == _key(primary):
            return path.name
        return None

    # -- the worktrees ---------------------------------------------------------------------------

    def _plan_worktrees(self, planned: list[_Planned]) -> None:
        """Decide every non-primary worktree, appending the ones to remove to `planned`. Each is decided on its own, so
        one that cannot be judged is an error line and the others are still looked at."""
        for info in self.worktrees[1:]:  # the primary checkout is never a candidate
            if info.bare:
                continue
            if _inside(self.repo, _key(info.path)):
                # `swarm clean` was pointed at a linked worktree (or a folder inside one): that directory is this
                # command's own working directory, so it is never removed from under it.
                continue
            try:
                if info.prunable:
                    decision, kind, mode = self._decide_stale(info), KIND_STALE, "stale"
                elif self._is_candidate_worktree(info.path):
                    decision, kind, mode = self._decide_candidate(info), KIND_CANDIDATE, "candidate"
                elif self.card_worktrees and self._card_id_of_worktree(info.path) is not None:
                    decision, kind, mode = self._decide_card_worktree(info), KIND_CARD, "card"
                else:
                    continue  # not ours: a worktree somebody made by hand is none of clean's business
            except Exception as exc:  # noqa: BLE001
                self._error("error", info.path, f"could not be judged, left alone: {type(exc).__name__}: {exc}")
                continue
            ok, reason = decision
            if not ok:
                self._skip(kind, info.path, reason)
                continue
            item = CleanItem(kind, _ascii(info.path), _ascii(reason, 400))
            self.report.candidates.append(item)
            planned.append(_Planned(item, info, mode))

    def _is_candidate_worktree(self, path: pathlib.Path) -> bool:
        """A merge-queue candidate: under the temp directory, in a directory whose name starts with ases-merge-
        (mergeq creates <temp>/ases-merge-XXXX/candidate)."""
        try:
            relative = pathlib.Path(_key(path)).relative_to(_key(self.temp_root))
        except ValueError:
            return False
        return bool(relative.parts) and relative.parts[0].startswith(_CANDIDATE_PREFIX)

    def _protected_reason(self, info: guards_mod.WorktreeInfo) -> str:
        """Why an active card owns this worktree (or why that cannot be ruled out), or "" when none does."""
        if self._list_error:
            return f"the cards could not be listed ({self._list_error}), so it cannot be ruled out that a card uses it"
        owner = self._active_paths.get(_key(info.path))
        if owner is None and info.branch:
            owner = self._active_branches.get(info.branch)
        if owner is not None:
            return f"card {owner[0]} is {owner[1]}"
        card_id = self._card_id_of_worktree(info.path)
        if card_id is not None:
            card, why = self._card(card_id)
            if card is None:
                return f"card {card_id} could not be read ({why}), so it cannot be ruled out that it is active"
            status = str(card.get("status") or "")
            if status not in _FINISHED:
                return f"card {card_id} is {status or 'in an unknown state'}"
        return ""

    def _decide_stale(self, info: guards_mod.WorktreeInfo) -> tuple[bool, str]:
        protected = self._protected_reason(info)
        if protected:
            return False, f"registered but its directory is gone, left alone: {protected}"
        return True, "registered in git but its directory is gone (what git worktree prune removes)"

    def _decide_candidate(self, info: guards_mod.WorktreeInfo) -> tuple[bool, str]:
        """A leftover merge candidate is removable when its directory is old enough that no merge can still be using
        it, no candidate build or fast-forward is open in the intents table, and its merge record is completed or
        absent (an unfinished record is reconcile's: the merge queue redoes it and overwrites the record)."""
        if info.locked:
            return False, "locked (git worktree unlock it first)"
        if self._open_builds:
            first = self._open_builds[0]
            return False, (f"a {first['kind']} intent for {first['key']} is still open: run `swarm run` once so "
                           f"reconcile settles it, then clean again")
        newest = 0.0
        for probe in (info.path, info.path.parent):
            try:
                newest = max(newest, probe.stat().st_mtime)
            except OSError:
                pass
        if newest:
            age = self.now - datetime.fromtimestamp(newest, timezone.utc)
            if age < self.min_age:
                return False, (f"only {int(age.total_seconds() // 60)} minute(s) old (a merge may still be running "
                               f"in it; leftovers are removed after {int(self.min_age.total_seconds() // 60)})")
        row = None
        if info.head:
            row = self.conn.execute(
                "SELECT task_key, completed_at FROM merge_records WHERE candidate_sha = ? "
                "ORDER BY (completed_at IS NULL) DESC LIMIT 1", (info.head,),
            ).fetchone()
        if row is None:
            return True, "leftover merge candidate worktree, no merge record names its commit"
        if row[1]:
            return True, f"leftover merge candidate worktree, the merge record of {row[0]} is completed"
        return False, (f"the merge record of {row[0]} names this candidate and is not completed: reconcile owns it, "
                       f"the merge queue will redo it")

    def _decide_card_worktree(self, info: guards_mod.WorktreeInfo) -> tuple[bool, str]:
        """The worktree of a finished card. Requires: the card done or archived and its workspace recorded as this path;
        its plan task finished (work and merge card done or archived); a clean tree (git's own guard refuses a dirty
        one at removal, this only makes the dry run say so first); and HEAD on a branch, or on commits some branch holds,
        so removing the tree cannot lose a commit."""
        if info.locked:
            return False, "locked (git worktree unlock it first)"
        card_id = self._card_id_of_worktree(info.path) or "?"
        protected = self._protected_reason(info)
        if protected:
            return False, protected
        card, why = self._card(card_id)
        if card is None:  # _protected_reason already refuses an unreadable card; this only keeps the type honest
            return False, f"card {card_id} could not be read ({why})"
        recorded = card.get("workspace_path")
        if recorded and _key(recorded) != _key(info.path):
            return False, f"card {card_id} records a different workspace ({_ascii(recorded)})"
        branch = info.branch or str(card.get("branch_name") or "")
        key = None
        for task_key, (work_id, merge_id) in self._tasks.items():
            if card_id in (work_id, merge_id):
                key = task_key
                break
        if key is None and branch:
            key = _task_for_branch(branch, self._tasks)
        if key is None:
            return False, f"card {card_id} is not a card of plan {self.project}"
        finished, why = self._task_finished(key)
        if not finished:
            return False, why
        code, out, err = _git(info.path, ["status", "--porcelain", "--untracked-files=normal"])
        if code != 0:
            return False, f"git could not read its status: {_first_line(err)}"
        if out.strip():
            return False, "has uncommitted or untracked files (look at them, then git worktree remove --force by hand)"
        if info.detached or not info.branch:
            code, out, _err = _git(self.repo, ["for-each-ref", "--contains", info.head, "--format=%(refname)", "refs/heads"])
            if code != 0 or not out.strip():
                return False, "detached HEAD on commits no local branch holds: removing it would lose them"
        return True, f"card {card_id} is done or archived, task {key} is finished, the tree is clean"

    def _remove_worktrees(self, planned: list[_Planned]) -> list[guards_mod.WorktreeInfo] | None:
        """Do what the plan says (apply only) and return the worktrees that stay open, for the branch pass. A dry run
        returns the list without the ones it planned to remove, so the branch pass judges the world the apply would
        leave. None means the world could not be re-read after removals (the branch pass is then skipped)."""
        if not self.apply:
            gone = {_key(p.info.path) for p in planned}
            return [w for w in self.worktrees if _key(w.path) not in gone]
        removed_any = False
        for plan in planned:
            if self._guarded(f"removing {plan.item.name}", lambda p=plan: self._remove_one(p), default=False):
                removed_any = True
        if not removed_any:
            return self.worktrees
        fresh = guards_mod.list_worktrees(self.repo)
        if not fresh:
            self._error("error", self.repo, "git could not list the worktrees after removing some: branches are left alone")
            return None
        return fresh

    def _remove_one(self, plan: _Planned) -> bool:
        """Remove one planned worktree; True when it is gone. A stale registration has no directory to be dirty, and a
        merge candidate is throwaway by design (a crashed build leaves staged squash content in it), so both are forced.
        A card worktree is NOT: git refuses a dirty one, which is the guard."""
        path = plan.info.path
        args = ["worktree", "remove", *(["--force"] if plan.mode in ("stale", "candidate") else []), str(path)]
        code, _out, err = _git(self.repo, args, timeout=_REMOVE_TIMEOUT, read_only=False)
        if code != 0:
            self._error(plan.item.kind, path, f"git refused to remove it: {_first_line(err)}")
            return False
        if plan.mode == "candidate":
            root = path.parent
            if root.name.startswith(_CANDIDATE_PREFIX) and _key(root.parent) == _key(self.temp_root):
                try:
                    root.rmdir()  # the empty ases-merge-XXXX directory mkdtemp made; a non-empty one stays
                except OSError:
                    pass
        self.report.removed.append(plan.item)
        self._record_removal(plan.item)
        return True

    # -- the branches ----------------------------------------------------------------------------

    def _local_branches(self) -> list[tuple[str, str]]:
        """(name, tip sha) of every local swarm/* and merge/* branch, by name."""
        code, out, err = _git(self.repo, ["for-each-ref", "--format=%(objectname) %(refname)", "refs/heads/swarm", "refs/heads/merge"])
        if code != 0:
            self._error("error", "branches", f"git could not list the branches: {_first_line(err)}")
            return []
        found = []
        for line in out.splitlines():
            sha, _, ref = line.strip().partition(" ")
            if sha and ref.startswith("refs/heads/"):
                found.append((ref[len("refs/heads/"):], sha))
        return sorted(found)

    def _paths(self, a: str, b: str) -> set[str] | None:
        code, out, _err = _git(self.repo, ["diff", "--name-only", "-z", "--no-renames", a, b])
        return None if code != 0 else {p for p in out.split("\0") if p}

    def _squash_proof(self, tip: str, key: str) -> tuple[bool, str]:
        """ASES merges by squash, so a merged work branch is NOT an ancestor of the integration branch and
        `git branch --merged` never lists it. The proof used instead, all of it required: the task's merge record is
        completed, not reverted, and names a squash commit S; S is in the integration branch; and every path the branch
        changed (against its merge base with S) has the same content in the branch as in S. Compared with S and not with
        today's tip, so a later task that edits the same file does not make an old, merged branch look unmerged. If
        this holds, the branch's content is preserved in S, and deleting the branch loses only its private history."""
        ahead = _git(self.repo, ["rev-list", "--count", f"refs/heads/{self.integration}..{tip}"])[1].strip() or "?"
        row = self.conn.execute(
            "SELECT squash_commit, completed_at, reverted FROM merge_records WHERE task_key = ? "
            "AND (project IS NULL OR project = ?)", (key, self.project),
        ).fetchone()
        if row is None or not row[1]:
            return False, f"not merged: {ahead} commit(s) not in {self.integration} and no completed merge record for task {key}"
        if row[2]:
            return False, f"the merge of task {key} was reverted: the branch may be the only copy of that work"
        squash = row[0]
        if not squash:
            return False, f"not merged: {ahead} commit(s) not in {self.integration} and the merge record of task {key} names no squash commit"
        code, _out, _err = _git(self.repo, ["merge-base", "--is-ancestor", squash, f"refs/heads/{self.integration}"])
        if code != 0:
            return False, f"the squash commit {squash[:12]} of task {key} is not in {self.integration}"
        code, out, _err = _git(self.repo, ["merge-base", tip, squash])
        base = out.strip()
        if code != 0 or not base:
            return False, f"git found no merge base between the branch and the squash commit {squash[:12]}"
        changed, differing = self._paths(base, tip), self._paths(squash, tip)
        if changed is None or differing is None:
            return False, "git could not compare the branch with the squash commit"
        overlap = sorted(changed & differing)
        if overlap:
            return False, (f"the branch changed {len(overlap)} path(s) whose content differs from the squash commit "
                           f"{squash[:12]} (for example {_ascii(overlap[0], 80)}): it has work that was not merged")
        return True, f"squash-merged by ASES as {squash[:12]} (in {self.integration}), all {len(changed)} changed path(s) match it"

    def _branches(self, open_worktrees: list[guards_mod.WorktreeInfo]) -> None:
        checked_out: dict[str, str] = {}
        primary_key = _key(self.worktrees[0].path)
        for info in open_worktrees:
            if info.branch:
                where = "the primary checkout" if _key(info.path) == primary_key else f"worktree {info.path}"
                checked_out.setdefault(info.branch, where)
        for name, tip in self._local_branches():
            self._guarded(f"branch {name}", lambda n=name, t=tip: self._consider_branch(n, t, checked_out))

    def _consider_branch(self, name: str, tip: str, checked_out: dict[str, str]) -> None:
        if name == self.integration:
            return  # never, even if somebody named it swarm/...
        if name in checked_out:
            return self._skip(KIND_BRANCH, name, f"checked out in {checked_out[name]}")
        matches = _tasks_for_branch(name, self._tasks)
        if len(matches) > 1:
            return self._skip(KIND_BRANCH, name, f"ambiguous: the name fits tasks {', '.join(matches)} of plan {self.project}")
        key = matches[0] if matches else None
        if key is None:
            return self._skip(KIND_BRANCH, name, f"no task of plan {self.project} owns this branch")
        if self._list_error:
            return self._skip(KIND_BRANCH, name, f"the cards could not be listed ({self._list_error})")
        if name in self._active_branches:
            card_id, status = self._active_branches[name]
            return self._skip(KIND_BRANCH, name, f"card {card_id} is {status}")
        finished, why = self._task_finished(key)
        if not finished:
            return self._skip(KIND_BRANCH, name, why)
        code, _out, err = _git(self.repo, ["merge-base", "--is-ancestor", tip, f"refs/heads/{self.integration}"])
        if code == 0:
            mode, detail = "merged", f"fully merged into {self.integration} (git branch --merged)"
        elif code == 1:
            ok, detail = self._squash_proof(tip, key)
            if not ok:
                return self._skip(KIND_BRANCH, name, detail)
            mode = "squash"
        else:
            return self._skip(KIND_BRANCH, name, f"git could not tell whether it is merged: {_first_line(err)}")
        item = CleanItem(KIND_BRANCH, _ascii(name), _ascii(f"{detail}; task {key} is finished", 400))
        self.report.candidates.append(item)
        if not self.apply:
            return
        # Re-read the tip: a branch that moved since it was judged is not the branch that was judged.
        code, out, _err = _git(self.repo, ["rev-parse", "--verify", "-q", f"refs/heads/{name}"])
        if code != 0 or out.strip() != tip:
            return self._error(KIND_BRANCH, name, "moved or vanished while cleaning, left alone")
        # -d refuses anything git does not consider merged; only a branch proven by the squash record is forced (-D).
        code, _out, err = _git(self.repo, ["branch", "-d" if mode == "merged" else "-D", name], read_only=False)
        if code != 0:
            return self._error(KIND_BRANCH, name, f"git refused to delete it: {_first_line(err)}")
        self.report.removed.append(item)
        self._record_removal(item, sha=tip)


def clean(
    repo, integration_branch: str, *, board: str, conn, plan_project: str, apply: bool = False, kanban_show=None,
    kanban_list=None, temp_root=None, now: datetime | None = None, candidate_min_age_minutes: int = 60,
    card_worktrees: bool = True,
) -> CleanReport:
    """Find, and with apply=True remove, what a finished project leaves behind in the repository. Dry run by default.

    Three things, each removed only when it is provably safe (phase 9: "Cleanup of worktrees and branches"):
      (a) worktrees: registrations whose directory is gone (`git worktree prune` candidates); leftover merge-queue
          candidate worktrees, `ases-merge-*` under the system temp directory (or `temp_root`), old enough that no
          merge can be using them, with no open candidate-build intent, whose merge record is completed or absent;
          and, unless card_worktrees is False, the worktree of a FINISHED card (`<repo>/.worktrees/<card id>`) whose
          task is finished and whose tree is clean. NEVER the worktree of a card that is running, in review, ready,
          blocked or scheduled (or in any state that is not done or archived), NEVER a locked one, and NEVER one
          whose card cannot be read.
      (b) local branches `swarm/*` and `merge/*` (a fix or retry card's branch is one of them, ASES-GIT-09) of a task
          whose work and merge card are both done or archived, and which are fully merged into the integration branch
          (`git branch --merged`) OR squash-merged by ASES (see _squash_proof: ASES merges by squash, so the first test
          alone would never match a merged work branch). NEVER the integration branch, the checked-out branch, a
          branch with an open worktree (a worktree removed in this same run counts as gone), a branch whose card is
          active, or one of no task of `plan_project`.
    Every candidate has a reason; every removal is a `hardening_removed` event (a deleted branch's event carries its
    tip sha); one failure never stops the rest; nothing here raises. `kanban_show(board, card_id)` and
    `kanban_list(board)` default to the hermes module's, resolved at call time. candidate_min_age_minutes is how old a
    leftover candidate must be (a merge in progress is a young directory)."""
    try:
        return _Cleaner(
            repo, integration_branch, board=board, conn=conn, plan_project=plan_project, apply=apply,
            kanban_show=kanban_show, kanban_list=kanban_list, temp_root=temp_root, now=now,
            candidate_min_age_minutes=candidate_min_age_minutes, card_worktrees=card_worktrees,
        ).run()
    except Exception as exc:  # noqa: BLE001 - the contract is a report, never an exception
        report = CleanReport(repo=_ascii(repo), integration_branch=_ascii(integration_branch), apply=bool(apply))
        report.errors.append(CleanItem("error", "clean", _ascii(f"unexpected failure: {type(exc).__name__}: {exc}")))
        return report


def format_clean_report(report: CleanReport) -> str:
    """The clean report as plain ASCII text for the terminal."""
    mode = "APPLIED" if report.apply else "DRY RUN"
    lines = [f"swarm clean ({mode}): repo {report.repo}, integration branch {report.integration_branch}"]

    def section(title: str, items: list[CleanItem]) -> None:
        lines.append("")
        lines.append(f"{title} ({len(items)}):")
        if not items:
            lines.append("  (none)")
        for item in items:
            lines.append(f"  [{item.kind}] {item.name} - {item.reason}")

    section("Would remove" if not report.apply else "Candidates", report.candidates)
    if report.apply:
        section("Removed", report.removed)
    section("Left alone", report.skipped)
    section("Errors", report.errors)
    lines.append("")
    if report.apply:
        lines.append(f"Removed {len(report.removed)} of {len(report.candidates)} candidate(s); {len(report.errors)} error(s).")
    elif report.candidates:
        lines.append(f"Dry run: nothing was removed. Run again with --apply to remove the {len(report.candidates)} candidate(s) above.")
    else:
        lines.append("Dry run: nothing to remove.")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------------------------
# retention: files under the ASES home
# ---------------------------------------------------------------------------------------------

DEFAULT_KEEP_LATEST = 3
_RETENTION_DIRS = ("logs", "reports", "stops", "evals")
_BACKUP_PREFIX = "ases.db.bak-"
KIND_BACKUPS = "backups"


@dataclasses.dataclass(frozen=True)
class RetentionItem:
    """One file, or one directory of files that belong together (a report is a page and a data file), with its size in
    bytes, its age in days (from the newest file in it) and why it is listed."""
    kind: str
    path: str
    size: int
    age_days: float
    reason: str


@dataclasses.dataclass
class RetentionReport:
    """What one retention() found. candidates: entries older than `days` that are not among the newest keep_latest of
    their kind. removed: the ones actually removed (apply only). kept: entries old enough to go that were kept only
    because they are among the newest keep_latest of their kind. skipped: what was not looked at (a symbolic link, a
    directory that resolves outside its own kind directory, an unreadable directory). errors: what failed. bytes_freed is what
    the removed files weighed. `refused` is the reason nothing was done at all (days below 1), else ""."""
    ases_home: str
    days: int
    keep_latest: int
    apply: bool
    candidates: list[RetentionItem] = dataclasses.field(default_factory=list)
    removed: list[RetentionItem] = dataclasses.field(default_factory=list)
    kept: list[RetentionItem] = dataclasses.field(default_factory=list)
    skipped: list[RetentionItem] = dataclasses.field(default_factory=list)
    errors: list[str] = dataclasses.field(default_factory=list)
    bytes_freed: int = 0
    refused: str = ""

    @property
    def bytes_candidate(self) -> int:
        return sum(item.size for item in self.candidates)


@dataclasses.dataclass
class _Entry:
    kind: str
    path: pathlib.Path
    files: list[pathlib.Path]
    size: int
    newest: float


def _scan_kind(base: pathlib.Path, kind: str, skipped: list[RetentionItem]) -> list[_Entry]:
    """The entries of one kind: every file directly in `base` is its own entry, and the files directly in any
    directory below it are ONE entry (a report directory holds a page and a data file, and half a report is worse than
    none). Symbolic links are never followed, and neither is a directory that resolves outside `base` itself (a
    Windows junction is not a symlink to Python, its resolved path is what shows it): a link from reports/ to a
    sibling directory of the ASES home would otherwise be read as report data."""
    base_key = _key(base)
    entries: list[_Entry] = []
    stack = [base]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as scan:
                listing = list(scan)
        except OSError as exc:
            skipped.append(RetentionItem(kind, _ascii(directory), 0, 0.0, f"could not be read: {_ascii(exc, 80)}"))
            continue
        files: list[tuple[pathlib.Path, int, float]] = []
        for entry in listing:
            try:
                if entry.is_symlink():
                    skipped.append(RetentionItem(kind, _ascii(entry.path), 0, 0.0, "symbolic link, not followed"))
                elif entry.is_dir(follow_symlinks=False):
                    if _inside(entry.path, base_key):
                        stack.append(pathlib.Path(entry.path))
                    else:
                        skipped.append(RetentionItem(kind, _ascii(entry.path), 0, 0.0, f"resolves outside {kind}/, not followed"))
                elif entry.is_file(follow_symlinks=False):
                    if entry.name in _DB_FILE_NAMES:
                        skipped.append(RetentionItem(kind, _ascii(entry.path), 0, 0.0, "the ASES database is never removed"))
                        continue
                    info = entry.stat(follow_symlinks=False)
                    files.append((pathlib.Path(entry.path), info.st_size, info.st_mtime))
            except OSError as exc:
                skipped.append(RetentionItem(kind, _ascii(entry.path), 0, 0.0, f"could not be read: {_ascii(exc, 80)}"))
        if not files:
            continue
        if directory == base:
            entries.extend(_Entry(kind, path, [path], size, mtime) for path, size, mtime in files)
        else:
            entries.append(_Entry(kind, directory, [f[0] for f in files], sum(f[1] for f in files), max(f[2] for f in files)))
    return entries


def _unlink(path: pathlib.Path) -> None:
    """Remove one file, clearing the read-only flag first when Windows refuses (a read-only file cannot be deleted)."""
    try:
        path.unlink()
    except PermissionError:
        os.chmod(path, stat.S_IWRITE)
        path.unlink()


def _remove_entry(entry: _Entry, base: pathlib.Path) -> tuple[int, list[str]]:
    """Delete the entry's files (each re-checked, at the moment of deletion, to be a plain file that still lives inside
    `base`) and any directory that empties, up to but never including `base`. Returns (bytes freed, problems)."""
    base_key = _key(base)
    freed, problems = 0, []
    for path in entry.files:
        try:
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode) or not _inside(path.parent, base_key):
                continue
            _unlink(path)
            freed += info.st_size
        except FileNotFoundError:
            continue
        except OSError as exc:
            problems.append(f"{_ascii(path)}: {_ascii(exc, 100)}")
    directory = entry.path if entry.path.is_dir() and _key(entry.path) != base_key else entry.path.parent
    while _key(directory) != base_key and _inside(directory, base_key):
        try:
            directory.rmdir()  # succeeds only when it is empty
        except OSError:
            break
        directory = directory.parent
    return freed, problems


def retention(
    ases_home, days: int, *, apply: bool = False, now: datetime | None = None, keep_latest: int = DEFAULT_KEEP_LATEST,
) -> RetentionReport:
    """ASES-OBS-02: "Transcripts and logs stay local under a retention setting" (default 30 days, blueprint section
    15.2). Under <ases_home>, remove what is older than `days` in logs/, reports/, stops/ and evals/, and the old
    `ases.db.bak-*` backups db.connect makes before a migration. Dry run by default.

    The newest `keep_latest` entries of each kind are kept whatever their age (a project that has not run for two
    months still has its last report). Age is the modification time of the newest file in an entry. `ases.db` itself is
    never touched (only names starting `ases.db.bak-` are backups, and a file called ases.db, -wal or -shm is skipped
    wherever it is). A symbolic link, or a directory (a Windows junction is one) that resolves outside the kind
    directory it was found in, is skipped and reported, not followed, so nothing outside logs/, reports/, stops/ and
    evals/ is ever read as data. A `days` below 1 is refused (nothing is done and the report says so): a typo such as 0
    must not wipe the reports. Never raises."""
    when = _aware(now)
    report = RetentionReport(ases_home=_ascii(ases_home), days=days, keep_latest=keep_latest, apply=bool(apply))
    try:
        if isinstance(days, bool) or not isinstance(days, (int, float)) or days < 1:
            report.refused = f"days must be at least 1, got {_ascii(days, 40)}: nothing was done"
            return report
        keep = max(0, int(keep_latest))
        report.keep_latest = keep
        home = pathlib.Path(ases_home)
        home_key = _key(home)
        if not home.is_dir():
            report.errors.append(f"the ASES home {_ascii(home)} is not a directory")
            return report
        plan: list[tuple[RetentionItem, _Entry, pathlib.Path]] = []
        for kind in _RETENTION_DIRS:
            base = home / kind
            if not base.exists() and not base.is_symlink():
                continue
            if _key(base) != os.path.normcase(os.path.join(home_key, kind)):
                report.skipped.append(RetentionItem(kind, _ascii(base), 0, 0.0, "a link to somewhere else, not followed"))
                continue
            _plan_kind(report, plan, _scan_kind(base, kind, report.skipped), when, keep, days, base)
        _plan_backups(report, plan, home, when, keep, days)
        if report.apply:
            for item, entry, base in plan:
                freed, problems = _remove_entry(entry, base)
                report.bytes_freed += freed
                report.errors.extend(problems)
                if not problems:
                    report.removed.append(item)
    except Exception as exc:  # noqa: BLE001 - the contract is a report, never an exception
        report.errors.append(_ascii(f"unexpected failure: {type(exc).__name__}: {exc}"))
    return report


def _item(entry: _Entry, when: datetime, reason: str) -> RetentionItem:
    age = max(0.0, (when.timestamp() - entry.newest) / 86400.0)
    return RetentionItem(entry.kind, _ascii(entry.path), entry.size, round(age, 1), reason)


def _plan_kind(report: RetentionReport, plan: list, entries: list[_Entry], when: datetime, keep: int, days, base: pathlib.Path) -> None:
    """Sort one kind's entries newest first: the first `keep` are protected, and of the rest those older than `days`
    become candidates (and are added to `plan` with the directory they must not be removed above)."""
    entries.sort(key=lambda e: (e.newest, str(e.path)), reverse=True)
    limit = when.timestamp() - days * 86400.0
    for position, entry in enumerate(entries):
        if entry.newest >= limit:
            continue
        if position < keep:
            report.kept.append(_item(entry, when, f"older than {days} day(s) but one of the newest {keep} of its kind, kept"))
        else:
            item = _item(entry, when, f"older than {days} day(s)")
            report.candidates.append(item)
            plan.append((item, entry, base))


def _plan_backups(report: RetentionReport, plan: list, home: pathlib.Path, when: datetime, keep: int, days) -> None:
    """The `ases.db.bak-*` backups directly in the ASES home. A partial copy (`.tmp`) is not counted as one of the
    newest but is removed when old, like any other stale file."""
    entries: list[_Entry] = []
    partials: list[_Entry] = []
    try:
        with os.scandir(home) as scan:
            listing = list(scan)
    except OSError as exc:
        report.errors.append(f"could not read {_ascii(home)}: {_ascii(exc, 80)}")
        return
    for entry in listing:
        try:
            if entry.name.startswith(_BACKUP_PREFIX) and entry.is_file(follow_symlinks=False) and not entry.is_symlink():
                info = entry.stat(follow_symlinks=False)
                item = _Entry(KIND_BACKUPS, pathlib.Path(entry.path), [pathlib.Path(entry.path)], info.st_size, info.st_mtime)
                (partials if entry.name.endswith(".tmp") else entries).append(item)
        except OSError as exc:
            report.skipped.append(RetentionItem(KIND_BACKUPS, _ascii(entry.path), 0, 0.0, f"could not be read: {_ascii(exc, 80)}"))
    _plan_kind(report, plan, entries, when, keep, days, home)
    _plan_kind(report, plan, partials, when, 0, days, home)


def format_retention_report(report: RetentionReport) -> str:
    """The retention report as plain ASCII text for the terminal."""
    if report.refused:
        return f"swarm retention: REFUSED. {report.refused}\n"
    mode = "APPLIED" if report.apply else "DRY RUN"
    lines = [f"swarm retention ({mode}): {report.ases_home}, remove older than {report.days} day(s), keep the newest {report.keep_latest} of each kind"]
    kinds = [*_RETENTION_DIRS, KIND_BACKUPS]
    shown = report.removed if report.apply else report.candidates
    for kind in kinds:
        of_kind = [item for item in shown if item.kind == kind]
        if of_kind:
            lines.append(f"  {kind}: {len(of_kind)} entr{'y' if len(of_kind) == 1 else 'ies'}, {_fmt_bytes(sum(i.size for i in of_kind))}")
            lines.extend(f"    {item.path} ({item.age_days} days, {_fmt_bytes(item.size)})" for item in of_kind)
    if not shown:
        lines.append("  nothing is old enough to remove")
    if report.kept:
        lines.append(f"  kept because they are among the newest: {len(report.kept)}")
    for item in report.skipped:
        lines.append(f"  skipped {item.path}: {item.reason}")
    for problem in report.errors:
        lines.append(f"  error: {problem}")
    lines.append("")
    if report.apply:
        lines.append(f"Removed {len(report.removed)} entr{'y' if len(report.removed) == 1 else 'ies'}, freed {_fmt_bytes(report.bytes_freed)}.")
    else:
        lines.append(f"Dry run: nothing was removed. {len(report.candidates)} entr{'y' if len(report.candidates) == 1 else 'ies'} "
                     f"({_fmt_bytes(report.bytes_candidate)}) would be. Run again with --apply to remove them.")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------------------------
# events and vacuum: the database
# ---------------------------------------------------------------------------------------------


def retention_events(conn: sqlite3.Connection, days: int, *, apply: bool = False, now: datetime | None = None) -> int:
    """Prune `events` rows older than `days`, and return how many rows are (dry run) or were (apply) older. A SEPARATE
    function, off unless asked for by name, because the event log is the audit trail (ASES-OBS-01): retention() never
    touches it. It is also STATE the controller reads (plan_critique verdicts and rounds, the reason of a pause, the
    path of the last release report), so prune it only while no project is being planned, approved or run. The age is
    read from the row's own timestamp (`datetime(ts)`, which understands both the ISO form with
    an offset that events.record writes and SQLite's plain form), so a row whose timestamp cannot be read is never
    deleted. Applying records one `events_pruned` event, so the trail shows that it was pruned. A `days` below 1 or a
    database error returns 0 and deletes nothing. Never raises."""
    try:
        if isinstance(days, bool) or not isinstance(days, (int, float)) or days < 1:
            return 0
        cutoff = (_aware(now) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        count = conn.execute("SELECT COUNT(*) FROM events WHERE datetime(ts) < datetime(?)", (cutoff,)).fetchone()[0]
        if apply and count:
            conn.execute("DELETE FROM events WHERE datetime(ts) < datetime(?)", (cutoff,))
            events_mod.record(conn, "events_pruned", {"days": days, "rows": count})
        return int(count)
    except Exception:  # noqa: BLE001
        return 0


def vacuum(conn: sqlite3.Connection) -> tuple[int, int]:
    """VACUUM the database and return (bytes before, bytes after) of the database file, best effort: pruned events and
    old rows leave free pages that only a VACUUM gives back. The write-ahead log is folded into the file before each
    measurement so the two numbers compare like with like. An in-memory database, a locked one or any error returns
    what could be measured (both numbers equal when the VACUUM did not run). Never raises."""
    path: str | None = None
    try:
        row = next((r for r in conn.execute("PRAGMA database_list") if r[1] == "main"), None)
        path = row[2] if row is not None and row[2] else None
    except Exception:  # noqa: BLE001
        path = None

    def size() -> int:
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        except Exception:  # noqa: BLE001
            pass
        try:
            return os.path.getsize(path) if path else 0
        except OSError:
            return 0

    before = size()
    try:
        conn.execute("VACUUM")
    except Exception:  # noqa: BLE001
        return before, before
    return before, size()
