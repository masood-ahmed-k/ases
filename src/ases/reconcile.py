"""Reconcile-on-start (section 9.1 folds this into recovery.py's job; kept as its own module here
since it's independently testable): compare the Hermes board, Git and the ASES DB before doing
anything else on a fresh controller start (ASES-REC-04, ASES-ARC-03).

check() is the read-only half: card IDs still resolve, a merge marked done has a matching merge_records row,
nothing reads done-but-reverted. reconcile() is the whole of section 19.4: it runs those checks per task, compares
Git and the intent records too, REPAIRS what is safe and BLOCKS the rest with an explanation, and terminates orphan
worker processes found by card ID. "Safe" means the repair only makes the database or the board say what Git
already proves (a squash commit carrying "Merge card: <id>" is on the integration branch, so that merge happened)
or undoes nothing that cannot be redone (reclaiming a card whose worker is gone). Whatever would need a guess is
left alone and reported in `blocked`, and `swarm resume` refuses to continue while any of it remains.

Windows trap: os.kill(pid, 0) does NOT probe a process there, it TERMINATES it. This module never calls os.kill on
Windows, and every process helper (liveness, command line, termination) is injectable so tests touch no process.
"""
from __future__ import annotations

import ctypes
import dataclasses
import functools
import os
import pathlib
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

from . import events
from . import hermes as hermes_mod
from . import intents as intents_mod


@dataclasses.dataclass(frozen=True)
class Inconsistency:
    task_key: str
    kind: str
    detail: str


@dataclasses.dataclass(frozen=True)
class Repair:
    """One thing reconcile fixed (applied True) or, with apply=False, WOULD fix (applied False). A repair of kind
    "candidate_discarded" is informational and is always applied False: nothing is changed, the merge queue redoes
    the candidate."""
    task_key: str
    kind: str
    detail: str
    applied: bool


@dataclasses.dataclass(frozen=True)
class ReconcileReport:
    """What one reconcile() saw. `findings` is every inconsistency detected (including the ones that were
    repaired), `repairs` what was or would be done about them, and `blocked` the findings that were NOT safely
    repairable and need a person: a subset of `findings`. `clean` means nothing was detected at all."""
    findings: list[Inconsistency] = dataclasses.field(default_factory=list)
    repairs: list[Repair] = dataclasses.field(default_factory=list)
    blocked: list[Inconsistency] = dataclasses.field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.findings


def _fetch_cards(board: str, row) -> dict:
    """The work and merge card of one plan_tasks row, keyed "work" / "merge" (a label with no card id is left out).
    A card that no longer resolves (Hermes exits non-zero) maps to None; any other failure propagates."""
    cards: dict = {}
    for label, card_id in (("work", row["work_card_id"]), ("merge", row["merge_card_id"])):
        if not card_id:
            continue
        try:
            cards[label] = hermes_mod.kanban_show(board, card_id)
        except hermes_mod.HermesCommandError:
            cards[label] = None
    return cards


def _check_cards(row, cards: dict, conn) -> list[Inconsistency]:
    """The per-task body of check(), on cards already fetched (reconcile() fetches each card once and shares them
    with its repair steps). A merge card that is done needs a completed merge_records row, and a done merge that
    is recorded as reverted was rolled back after its card was completed."""
    findings: list[Inconsistency] = []
    key = row["task_key"]
    for label, card_id in (("work", row["work_card_id"]), ("merge", row["merge_card_id"])):
        if not card_id:
            continue
        card = cards.get(label)
        if card is None:
            findings.append(Inconsistency(key, "missing_card", f"{label} card {card_id} no longer resolves"))
            continue

        if label == "merge" and card["status"] == "done":
            mr = conn.execute(
                "SELECT completed_at, reverted FROM merge_records WHERE task_key = ?", (key,)
            ).fetchone()
            if mr is None or not mr["completed_at"]:
                findings.append(Inconsistency(
                    key, "merge_done_without_record",
                    f"merge card {card_id} is done but merge_records has no completed_at",
                ))
            elif mr["reverted"]:
                findings.append(Inconsistency(
                    key, "done_but_reverted",
                    f"merge card {card_id} reads done but merge_records.reverted=1 -- the "
                    "integration branch was rolled back after this card was completed",
                ))
    return findings


def check(board: str, project: str, *, conn) -> list[Inconsistency]:
    rows = conn.execute(
        "SELECT task_key, work_card_id, merge_card_id FROM plan_tasks WHERE project = ?", (project,)
    ).fetchall()
    findings: list[Inconsistency] = []
    for row in rows:
        findings.extend(_check_cards(row, _fetch_cards(board, row), conn))
    return findings


# ---------------------------------------------------------------------------------------------
# Process helpers. Both liveness and termination are the DEFAULTS of reconcile()'s `alive` and
# `killer` parameters, so a test injects fakes and never touches a real process.
# ---------------------------------------------------------------------------------------------

_STILL_ACTIVE = 259                        # GetExitCodeProcess: the process has not exited
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_INVALID_PARAMETER = 87              # OpenProcess for a pid that does not exist
# Pids 0 to 4 are the idle process, System and (on POSIX) init and kernel threads: never a worker, never ours to kill.
_LOWEST_WORKER_PID = 5


def _is_windows() -> bool:
    return sys.platform == "win32"


def _as_pid(value) -> int | None:
    """A positive int for anything int-like (Hermes may hand a pid over as an int or a numeric string), else None.
    A bool is not a pid, and zero or a negative number would address a process GROUP on POSIX."""
    if isinstance(value, bool):
        return None
    try:
        pid = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return pid if pid > 0 else None


class _WinApi:
    """The three kernel32 calls pid_alive needs, behind a small interface so the liveness LOGIC can be tested
    without ctypes. A private WinDLL (not the shared ctypes.windll) so setting argtypes cannot disturb other code,
    and use_last_error so the error code read after a failed OpenProcess is the right one."""

    def __init__(self) -> None:
        from ctypes import wintypes  # imported here: only meaningful on Windows
        self._wintypes = wintypes
        self._k = ctypes.WinDLL("kernel32", use_last_error=True)
        self._k.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        self._k.OpenProcess.restype = wintypes.HANDLE
        self._k.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        self._k.GetExitCodeProcess.restype = wintypes.BOOL
        self._k.CloseHandle.argtypes = (wintypes.HANDLE,)
        self._k.CloseHandle.restype = wintypes.BOOL

    def open_process(self, pid: int):
        """(handle, 0) on success, (None, the Windows error code) when the process cannot be opened."""
        handle = self._k.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        return (handle, 0) if handle else (None, ctypes.get_last_error())

    def exit_code(self, handle) -> int | None:
        code = self._wintypes.DWORD()
        if not self._k.GetExitCodeProcess(handle, ctypes.byref(code)):
            return None
        return int(code.value)

    def close(self, handle) -> None:
        self._k.CloseHandle(handle)


_win_api_instance: _WinApi | None = None


def _win_api() -> _WinApi:
    global _win_api_instance
    if _win_api_instance is None:
        _win_api_instance = _WinApi()
    return _win_api_instance


def _pid_alive_windows(pid: int, api=None) -> bool:
    """OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION) then GetExitCodeProcess: exit code 259 (STILL_ACTIVE) means
    running. When the answer is unclear this errs towards ALIVE, because the two mistakes are not equal: calling a
    live worker dead would get its card reclaimed underneath it, while calling a dead one alive only leaves a card
    for Hermes's own stale-claim reclaim. Only error 87 (no such pid) proves the process is gone; an access-denied
    error means it exists."""
    api = api or _win_api()
    handle, error = api.open_process(pid)
    if handle is None:
        return error != _ERROR_INVALID_PARAMETER
    try:
        code = api.exit_code(handle)
        return code is None or code == _STILL_ACTIVE
    finally:
        api.close(handle)


def _pid_alive_posix(pid: int, kill=None) -> bool:
    """os.kill(pid, 0) sends nothing on POSIX, it only checks that the pid can be signalled. `kill` is injectable
    so the Windows trap cannot bite a test that exercises this branch on Windows."""
    kill = kill or os.kill
    try:
        kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # the process exists, it just belongs to someone else
    return True


def pid_alive(pid: int) -> bool:
    """Is a process with this pid running right now? Windows: ctypes OpenProcess and GetExitCodeProcess, never
    os.kill (the trap in the module docstring). POSIX: os.kill(pid, 0). A pid that is not a positive int is not
    alive; this is the default of reconcile()'s `alive` parameter."""
    checked = _as_pid(pid)
    if checked is None:
        return False
    return _pid_alive_windows(checked) if _is_windows() else _pid_alive_posix(checked)


def _terminate_windows(pid: int, run=None, alive=None) -> bool:
    """`taskkill /PID n /T /F`: /T takes the process tree with it (a worker's shell, compilers, test runners), /F
    forces it. A non-zero exit is still a success when the process is gone by the time we look (it exited on its
    own between the liveness check and the kill)."""
    run = run or subprocess.run
    alive = alive or pid_alive
    try:
        result = run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True,
                     errors="replace", timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 or not alive(pid)


def _terminate_posix(pid: int, kill=None, killpg=None, getpgid=None, sleep=time.sleep, alive=None,
                     grace_seconds: float = 5.0) -> bool:
    """SIGTERM the worker's process GROUP, wait up to grace_seconds for it to go, then SIGKILL. Signalling the
    group takes the worker's children too, but a group is only ever signalled when it is NOT our own: a worker
    started without its own group shares the controller's, and killpg would then kill the controller as well, so
    that case falls back to signalling the single pid."""
    kill = kill or os.kill
    killpg = killpg or getattr(os, "killpg", None)
    getpgid = getpgid or getattr(os, "getpgid", None)
    alive = alive or (lambda p: _pid_alive_posix(p, kill))
    sigterm = signal.SIGTERM
    sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)

    def send(sig) -> None:
        group = None
        if killpg is not None and getpgid is not None:
            try:
                group, own = getpgid(pid), getpgid(0)
            except OSError:
                group, own = None, None
            if group == own:
                group = None
        if group is not None:
            killpg(group, sig)
        else:
            kill(pid, sig)

    try:
        send(sigterm)
    except ProcessLookupError:
        return True       # already gone
    except OSError:
        return False
    for _ in range(max(int(grace_seconds / 0.25), 1)):
        if not alive(pid):
            return True
        sleep(0.25)
    try:
        send(sigkill)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    sleep(0.25)
    return not alive(pid)


def terminate_tree(pid: int) -> bool:
    """Terminate a process and everything it started; True when it is gone. Windows: `taskkill /PID n /T /F`.
    POSIX: SIGTERM then SIGKILL on the process group. Refuses (returns False) for a pid that is not a plausible
    worker, and for this process and its parent: taking the controller down with its own orphan sweep would be
    worse than the orphan. This is the default of reconcile()'s `killer` parameter."""
    checked = _as_pid(pid)
    if checked is None or checked < _LOWEST_WORKER_PID or checked in (os.getpid(), os.getppid()):
        return False
    return _terminate_windows(checked) if _is_windows() else _terminate_posix(checked)


def _command_line_windows(pid: int, run=None) -> str | None:
    """The process's command line through PowerShell's CIM cmdlet (wmic is gone from current Windows 11). The pid
    is an int formatted into the script, never text from a card. None when it cannot be read."""
    run = run or subprocess.run
    script = ("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
              f"(Get-CimInstance -ClassName Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine")
    try:
        result = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                     capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip() or None


def _command_line_posix(pid: int, proc_root: str = "/proc", run=None) -> str | None:
    """/proc/<pid>/cmdline (NUL separated) when procfs exists, else `ps -o args= -p <pid>`. None when unreadable."""
    try:
        raw = pathlib.Path(proc_root, str(pid), "cmdline").read_bytes()
    except OSError:
        raw = b""
    text = raw.replace(b"\0", b" ").decode("utf-8", errors="replace").strip()
    if text:
        return text
    run = run or subprocess.run
    try:
        result = run(["ps", "-o", "args=", "-p", str(pid)], capture_output=True, text=True,
                     errors="replace", timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip() or None


def process_command_line(pid: int) -> str | None:
    """The command line of a live process, or None when it cannot be read (no such process, no permission, no
    tool). Used only by the orphan-worker check, where None means "do not kill": an unreadable process is never
    assumed to be ours. This is the default of reconcile()'s `command_line` parameter."""
    checked = _as_pid(pid)
    if checked is None:
        return None
    return _command_line_windows(checked) if _is_windows() else _command_line_posix(checked)


def worker_pid(card: dict) -> int | None:
    """The pid of the card's LIVE run: the last entry of card["_runs"] whose ended_at is empty and whose
    worker_pid is int-like, else the card's own worker_pid field when its status is running, else None. A run
    that has ended names a process that is no longer the card's worker, so it is never returned."""
    for run in reversed(card.get("_runs") or []):
        if isinstance(run, dict) and not run.get("ended_at"):
            pid = _as_pid(run.get("worker_pid"))
            if pid is not None:
                return pid
    if card.get("status") == "running":
        return _as_pid(card.get("worker_pid"))
    return None


# ---------------------------------------------------------------------------------------------
# Git, read-only. A git that fails RAISES (_GitError): "git could not be asked" must never be read as "git has no
# such commit", because the repairs below write a record or complete a card on the strength of what git says.
# ---------------------------------------------------------------------------------------------

_GIT_TIMEOUT = 60                          # seconds per git call, the same as the merge queue's
_COMPLETABLE = ("blocked", "ready", "todo")  # the merge card states process_merge_queue itself completes from
_RECLAIM_REASON = "worker process gone (reconcile-on-start)"
_NOOP_RESULT = "no changes to merge (review-only task)"
_PROJECT_KEY = "*"                         # task_key of a finding that belongs to no single task


class _GitError(RuntimeError):
    """A git call reconcile depends on failed, or answered in a way it cannot use."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ascii(text) -> str:
    """Anything a person reads in a terminal must survive the Windows console (cp1252): card ids are ASCII but a
    workspace path, a command line or an error message need not be, so non-ASCII is escaped, never dropped."""
    return str(text).encode("ascii", "backslashreplace").decode("ascii")


def _first_line(text: str) -> str:
    lines = (text or "").strip().splitlines()
    return lines[0][:200] if lines else ""


def _git(repo, args: list[str]) -> tuple[int, str, str]:
    """One read-only git command: (exit code, stdout, stderr), decoded as UTF-8. --no-optional-locks so a query
    cannot take index.lock away from a real git operation. A git that cannot be started or times out comes back
    as exit code -1 with the reason in stderr, so callers deal in one failure shape."""
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(repo), *args], capture_output=True, timeout=_GIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return -1, "", f"git {args[0]} timed out after {_GIT_TIMEOUT}s"
    except OSError as exc:
        return -1, "", f"git could not be run: {exc}"
    return (result.returncode, result.stdout.decode("utf-8", errors="replace"),
            result.stderr.decode("utf-8", errors="replace"))


def _landed_commit(repo, branch: str, merge_card_id: str) -> str | None:
    """The newest commit on `branch` whose message has the line "Merge card: <id>", or None. The controller writes
    that line into every squash commit (ASES-GIT-06), which is how a merge is recognised in git after a crash.
    --grep only narrows the search: the match is then made on the WHOLE line, because one card id can be a prefix of
    another (t_1 and t_12), and --fixed-strings keeps an id from being read as a regular expression. The trailing
    "--" keeps a file named like the branch from making the revision ambiguous."""
    line = f"Merge card: {merge_card_id}"
    code, out, err = _git(repo, ["log", branch, "--fixed-strings", f"--grep={line}",
                                 "--format=%H%x1f%B%x1e", "--"])
    if code != 0:
        raise _GitError(f"git log {branch} failed: {_first_line(err)}")
    for record in out.split("\x1e"):
        sha, _, body = record.strip().partition("\x1f")
        if sha and any(text.strip() == line for text in body.splitlines()):
            return sha
    return None


def _is_ancestor(repo, sha: str, branch: str) -> bool:
    """`git merge-base --is-ancestor`: exit 0 is yes, 1 is no, anything else is git failing to answer."""
    code, _out, err = _git(repo, ["merge-base", "--is-ancestor", sha, branch])
    if code in (0, 1):
        return code == 0
    raise _GitError(f"git merge-base --is-ancestor {sha[:12]} {branch} failed: {_first_line(err)}")


def _revert_commit(repo, branch: str, sha: str) -> str | None:
    """The commit on `branch` that reverts `sha`, or None. git's own revert message says "This reverts commit
    <full sha>.", which is what mergeq.revert_merge produces."""
    code, out, err = _git(repo, ["log", branch, "--fixed-strings", f"--grep=This reverts commit {sha}",
                                 "--format=%H", "--"])
    if code != 0:
        raise _GitError(f"git log {branch} failed: {_first_line(err)}")
    found = out.split()
    return found[0] if found else None


def _worktree_paths(porcelain: str) -> list[str]:
    """The paths in `git worktree list --porcelain` (one "worktree <path>" line per worktree)."""
    return [line[len("worktree "):] for line in porcelain.splitlines() if line.startswith("worktree ")]


def _mentions(command_line: str, card_id: str) -> bool:
    """Does the command line name this card? A whole-token match, so t_1 is not found in a line that only names
    t_12: an orphan sweep that errs, errs towards leaving the process alone."""
    if not card_id:
        return False
    pattern = rf"(?<![A-Za-z0-9_-]){re.escape(card_id)}(?![A-Za-z0-9_-])"
    return re.search(pattern, command_line) is not None


class _Pass:
    """The state of ONE reconcile() run. Everything a step finds goes through note() and everything it changes goes
    through do(), which is the only place a repair is applied, so apply=False is enforced in one spot: with it
    nothing is written, no hermes call mutates a card and no process is killed."""

    def __init__(self, board, repo, plan, conn, apply, alive, killer, command_line):
        self.board, self.repo, self.plan, self.conn = board, repo, plan, conn
        self.project, self.branch = plan.project, plan.integration_branch
        self.apply, self.alive, self.killer, self.command_line = apply, alive, killer, command_line
        self.findings: list[Inconsistency] = []
        self.repairs: list[Repair] = []
        self.blocked: list[Inconsistency] = []
        self.task_blocked: dict[str, list[Inconsistency]] = {}   # what is still unresolved, per task key
        self.task_repaired: dict[str, list[str]] = {}            # what was (or would be) repaired, per task key
        self.rows: dict = {}                                     # task key -> plan_tasks row
        self.cards: dict = {}                                    # card id -> card dict, None when it does not resolve
        self.card_task: dict[str, str] = {}                      # card id -> task key
        self.reclaimed: set[str] = set()                         # running cards this pass reclaims (or would)
        self.reverted_keys: set[str] = set()                     # tasks whose revert git has, recorded now or not
        self.open_intents: list[dict] = []

    # -- reporting ---------------------------------------------------------------------------

    def add(self, finding: Inconsistency, *, blocked: bool = False) -> Inconsistency:
        self.findings.append(finding)
        if blocked:
            self._block(finding)
        return finding

    def note(self, task: str, kind: str, detail: str, *, blocked: bool = False) -> Inconsistency:
        return self.add(Inconsistency(task, kind, _ascii(detail)), blocked=blocked)

    def _block(self, finding: Inconsistency) -> None:
        self.blocked.append(finding)
        self.task_blocked.setdefault(finding.task_key, []).append(finding)

    def escalate(self, finding: Inconsistency, why: str) -> None:
        """A finding already in the report turns out NOT to be safely repairable: replace it, in place, with a copy
        that says why, and count it as blocked (blocked stays a subset of findings)."""
        blocked = dataclasses.replace(finding, detail=_ascii(f"{finding.detail}; {why}"))
        self.findings[self.findings.index(finding)] = blocked
        self._block(blocked)

    def do(self, task: str, kind: str, detail: str, action) -> bool:
        """Apply one repair, or with apply=False only report that it would be. An applied repair is logged once, as
        a reconcile_repair event, AFTER its action succeeded. An action that returns False (a kill that did not
        take) is not a repair: nothing is recorded and False is returned. An action that raises propagates to the
        task's handler, which reports a reconcile_error."""
        detail = _ascii(detail)
        if self.apply:
            if action() is False:
                return False
            events.record(self.conn, "reconcile_repair", {"task_key": task, "kind": kind, "detail": detail})
        self.repairs.append(Repair(task, kind, detail, self.apply))
        self.task_repaired.setdefault(task, []).append(kind)
        return True

    # -- the write actions (each one statement, like mergeq's own) ----------------------------

    def _insert_recovered(self, key: str, sha: str) -> None:
        self.conn.execute(
            "INSERT INTO merge_records (task_key, candidate_sha, gate3_result, squash_commit, reverted, "
            "completed_at) VALUES (?, ?, 'recovered', ?, 0, ?)",
            (key, sha, sha, _now()),
        )

    def _finish_record(self, key: str, sha: str) -> None:
        self.conn.execute(
            "UPDATE merge_records SET squash_commit = ?, completed_at = ? WHERE task_key = ? AND completed_at IS NULL",
            (sha, _now(), key),
        )

    def _write_noop(self, key: str) -> None:
        self.conn.execute(
            "INSERT INTO merge_records (task_key, candidate_sha, gate3_result, squash_commit, reverted, "
            "completed_at) VALUES (?, NULL, 'skipped', NULL, 0, ?) "
            "ON CONFLICT(task_key) DO UPDATE SET candidate_sha=NULL, gate3_result='skipped', squash_commit=NULL, "
            "completed_at=excluded.completed_at",
            (key, _now()),
        )

    def _mark_reverted(self, key: str) -> None:
        self.conn.execute("UPDATE merge_records SET reverted = 1 WHERE task_key = ?", (key,))

    def _complete_card(self, merge_id: str, sha: str | None) -> None:
        """Complete the merge card the way process_merge_queue does, marked as recovered."""
        if sha:
            hermes_mod.kanban_complete(
                self.board, merge_id, result=f"merged {sha} (recovered)",
                metadata={"squash_commit": sha, "recovered": True},
            )
        else:
            hermes_mod.kanban_complete(
                self.board, merge_id, result=_NOOP_RESULT,
                metadata={"squash_commit": None, "no_op": True, "recovered": True},
            )

    # -- per task ----------------------------------------------------------------------------

    def run_task(self, row) -> None:
        """Every step for one plan task. One failing hermes or git call is caught HERE and becomes a
        reconcile_error finding, so it cannot stop the other tasks (ASES-REC-04). Repairs made before the failure
        stay in the report: they happened."""
        key = row["task_key"]
        try:
            cards = _fetch_cards(self.board, row)
            for label, card in cards.items():
                card_id = row[f"{label}_card_id"]
                self.cards[card_id] = card
                self.card_task[card_id] = key
            deferred = self._checks(row, cards)
            self._revert(row)   # before _merge: a revert git already has must be settled before the merge is judged
            self._merge(row, cards, deferred)
            self._workers(row, cards)
        except Exception as exc:  # noqa: BLE001 - deliberately broad: see the docstring
            self.note(key, "reconcile_error", f"{type(exc).__name__}: {exc}"[:300], blocked=True)

    def _checks(self, row, cards: dict) -> Inconsistency | None:
        """check()'s findings for this task. missing_card and done_but_reverted are always blocked: nothing here can
        decide them. merge_done_without_record is reported and returned, for _merge to repair or escalate."""
        deferred = None
        for finding in _check_cards(row, cards, self.conn):
            if finding.kind == "merge_done_without_record":
                deferred = self.add(finding)
            else:
                self.add(finding, blocked=True)
        return deferred

    def _merge_record(self, key: str):
        return self.conn.execute(
            "SELECT candidate_sha, gate3_result, squash_commit, reverted, completed_at "
            "FROM merge_records WHERE task_key = ?", (key,),
        ).fetchone()

    def _landed(self, merge_id: str, mr) -> str | None:
        """The commit that proves this merge card's fast-forward happened: the recorded squash commit when it is on
        the integration branch, else the commit carrying "Merge card: <id>", else None."""
        recorded = mr["squash_commit"] if mr is not None else None
        if recorded and _is_ancestor(self.repo, recorded, self.branch):
            return recorded
        return _landed_commit(self.repo, self.branch, merge_id)

    def _reverted_reason(self, sha: str) -> str | None:
        """Why a landed commit must NOT be recovered as a live merge: git shows it was reverted. None otherwise."""
        revert = _revert_commit(self.repo, self.branch, sha)
        if revert is None:
            return None
        return (f"commit {sha[:12]} was reverted on '{self.branch}' by {revert[:12]}, so it is not recovered as "
                f"a live merge; a person must decide")

    def _merge(self, row, cards: dict, deferred: Inconsistency | None) -> None:
        """ASES-REC-04: a merge card and its merge_records row must agree with each other and with git."""
        merge = cards.get("merge")
        if merge is None:
            return  # missing_card has already blocked it
        key, status = row["task_key"], merge.get("status")
        mr = self._merge_record(key)
        complete = mr is not None and bool(mr["completed_at"])
        if status == "done":
            if not complete:
                self._done_without_record(row, deferred, mr)
        elif (mr is not None and mr["reverted"]) or key in self.reverted_keys:
            return  # rolled back and waiting for its fix card: consistent
        elif complete:
            self._record_without_done_card(row, merge, mr)
        else:
            self._unfinished(row, cards, merge, mr)

    def _done_without_record(self, row, deferred: Inconsistency | None, mr) -> None:
        """A merge card that is done with no completed merge_records row. If git carries the card's squash commit
        the merge happened, so the record is written (or finished) from git. A review-only task legitimately
        merged nothing, so its no-op record is written. Anything else is a card that says merged while git has no
        trace of it: blocked."""
        key, merge_id = row["task_key"], row["merge_card_id"]
        finding = deferred or self.note(
            key, "merge_done_without_record", f"merge card {merge_id} is done but merge_records has no completed_at")
        sha = self._landed(merge_id, mr)
        if sha:
            why = self._reverted_reason(sha)
            if why:
                self.escalate(finding, why)
            elif mr is None:
                self.do(key, "merge_record_recovered",
                        f"write the merge record for {key} from git: {sha} carries 'Merge card: {merge_id}'",
                        functools.partial(self._insert_recovered, key, sha))
            else:
                self.do(key, "merge_record_recovered",
                        f"finish the merge record for {key} from git: squash commit {sha}",
                        functools.partial(self._finish_record, key, sha))
        elif row["role"] != "coder":
            self.do(key, "merge_record_noop",
                    f"record the no-op merge of review-only task {key} (merge card {merge_id} is done, git has "
                    f"no commit for it)", functools.partial(self._write_noop, key))
        else:
            self.escalate(finding, f"git has no commit carrying 'Merge card: {merge_id}' on '{self.branch}', so the "
                                   f"card says merged but nothing landed; a person must decide whether to re-merge "
                                   f"or reopen the card")

    def _record_without_done_card(self, row, merge: dict, mr) -> None:
        """The crash between the fast-forward and the merge-card completion, record side: merge_records says the
        merge finished and the card is not done. The card is completed from the record. Refused (blocked) when the
        recorded commit is not on the integration branch, was reverted, or the card is somewhere reconcile does not
        complete cards from."""
        key, merge_id, status = row["task_key"], row["merge_card_id"], merge.get("status")
        finding = self.note(key, "merge_record_without_done_card",
                            f"merge_records says {key} finished merging but merge card {merge_id} is {status}")
        if status not in _COMPLETABLE:
            self.escalate(finding, f"a card in '{status}' is not one reconcile completes (only "
                                   f"{', '.join(_COMPLETABLE)}); a person must look")
            return
        sha = mr["squash_commit"]
        if sha:
            if not _is_ancestor(self.repo, sha, self.branch):
                self.escalate(finding, f"squash commit {sha[:12]} is not on '{self.branch}', so the merge did not "
                                       f"survive; a person must decide")
                return
            why = self._reverted_reason(sha)
            if why:
                self.escalate(finding, why)
                return
            self.do(key, "merge_card_completed", f"complete merge card {merge_id} as merged {sha} (recovered)",
                    functools.partial(self._complete_card, merge_id, sha))
        elif mr["gate3_result"] == "skipped":
            self.do(key, "merge_card_completed", f"complete merge card {merge_id} as a recorded no-op (recovered)",
                    functools.partial(self._complete_card, merge_id, None))
        else:
            self.escalate(finding, "the record is complete but names no squash commit and is not a recorded no-op")

    def _unfinished(self, row, cards: dict, merge: dict, mr) -> None:
        """A merge that is not done and whose record is missing or was never completed. If git carries the card's
        squash commit the fast-forward DID happen (the crash was after it): finish the record and complete the card.
        If not, a candidate that never landed is left for the merge queue to redo and reported as informational.
        With no record at all, nothing can have landed before the work card was done, so git is not even asked."""
        key, merge_id, status = row["task_key"], row["merge_card_id"], merge.get("status")
        if mr is None:
            work = cards.get("work")
            if work is None or work.get("status") != "done":
                return
        sha = self._landed(merge_id, mr)
        if sha is None:
            if mr is not None:
                self.repairs.append(Repair(key, "candidate_discarded", _ascii(
                    f"candidate {(mr['candidate_sha'] or 'unknown')[:12]} for {key} (gate3 "
                    f"{mr['gate3_result'] or 'unknown'}) never landed on '{self.branch}'; the row is left and the "
                    f"merge queue redoes it"), False))
            return
        what = "no merge record exists" if mr is None else "the merge record was never completed"
        finding = self.note(key, "merge_unfinished",
                            f"{sha} carries 'Merge card: {merge_id}' on '{self.branch}', so the fast-forward "
                            f"happened, but {what} and merge card {merge_id} is {status}")
        if status not in _COMPLETABLE:
            self.escalate(finding, f"a card in '{status}' is not one reconcile completes (only "
                                   f"{', '.join(_COMPLETABLE)}); a person must look")
            return
        why = self._reverted_reason(sha)
        if why:
            self.escalate(finding, why)
            return
        if mr is None:
            self.do(key, "merge_record_recovered", f"write the merge record for {key} from git: squash commit {sha}",
                    functools.partial(self._insert_recovered, key, sha))
        else:
            self.do(key, "merge_record_recovered", f"finish the merge record for {key}: squash commit {sha}",
                    functools.partial(self._finish_record, key, sha))
        self.do(key, "merge_card_completed", f"complete merge card {merge_id} as merged {sha} (recovered)",
                functools.partial(self._complete_card, merge_id, sha))

    def _revert(self, row) -> None:
        """An open revert intent for this task means the crash was around `git revert`. Git is the truth: a revert
        commit for the recorded squash commit means the record is behind and is brought up to date; no such commit
        means the merge still stands while something believed it was being undone, which only a person can settle."""
        key = row["task_key"]
        if not any(i["kind"] == intents_mod.KIND_REVERT and i["key"] == key for i in self.open_intents):
            return
        mr = self._merge_record(key)
        if mr is None or not mr["squash_commit"]:
            self.note(key, "revert_unfinished", f"a revert was started for {key} but there is no recorded squash "
                                                f"commit to say what was being reverted", blocked=True)
            return
        if mr["reverted"]:
            return
        sha = mr["squash_commit"]
        revert = _revert_commit(self.repo, self.branch, sha)
        if revert is None:
            self.note(key, "revert_unfinished",
                      f"a revert of {sha[:12]} was started but '{self.branch}' has no revert commit: the merge still "
                      f"stands; a person must decide whether to revert it", blocked=True)
            return
        self.note(key, "revert_unrecorded", f"git has revert {revert[:12]} of {sha[:12]} but "
                                            f"merge_records.reverted is 0")
        self.do(key, "revert_recorded", f"mark {key} reverted: git has revert {revert[:12]} of {sha[:12]}",
                functools.partial(self._mark_reverted, key))
        self.reverted_keys.add(key)

    def _workers(self, row, cards: dict) -> None:
        """Worker processes of this task's cards: a running card is checked against its live worker, any other card
        against a worker left over from an earlier session."""
        key = row["task_key"]
        for label in ("work", "merge"):
            card = cards.get(label)
            if card is None:
                continue
            card_id = row[f"{label}_card_id"]
            if card.get("status") == "running":
                self._running(key, card_id, card)
            else:
                self._orphan(key, card_id, card)

    def _running(self, key: str, card_id: str, card: dict) -> None:
        """A card that reads running needs a worker. Its live run's pid not being alive means the worker died with
        the previous session, so the card is reclaimed (Hermes then re-queues it). A live worker is never touched.
        A running card that records no pid at all is blocked: there is no way to tell. Its workspace must also exist
        on disk (a card without a worktree), unless the card is being reclaimed anyway."""
        pid = worker_pid(card)
        if pid is None:
            self.note(key, "running_without_pid",
                      f"card {card_id} is running but no live run records a worker pid, so reconcile cannot tell "
                      f"whether its worker is alive", blocked=True)
        elif not self.alive(pid):
            self.note(key, "worker_gone", f"card {card_id} is running but its worker (pid {pid}) is gone")
            self.reclaimed.add(card_id)
            self.do(key, "worker_gone_reclaimed", f"reclaim card {card_id}: worker pid {pid} is gone",
                    functools.partial(hermes_mod.kanban_reclaim, self.board, card_id, reason=_RECLAIM_REASON))
            return
        path = card.get("workspace_path")
        if path and not pathlib.Path(str(path)).exists():
            self.note(key, "missing_worktree",
                      f"card {card_id} is running but its workspace {path} does not exist on disk", blocked=True)

    def _orphan(self, key: str, card_id: str, card: dict) -> None:
        """Section 19.4: "Orphan worker processes from a previous controller session are found by card ID and
        terminated before new work starts." A card that is NOT running whose latest run's worker is still alive and
        whose command line NAMES THIS CARD is terminated. Never a process whose command line does not name the card
        (the user may have unrelated Hermes sessions open), never one whose command line cannot be read, never this
        process or its parent. A card in `review` whose latest run is still open is left alone too: a reviewer may
        legitimately hold it."""
        runs = card.get("_runs") or []
        latest = runs[-1] if runs and isinstance(runs[-1], dict) else None
        if latest is None:
            return
        pid = _as_pid(latest.get("worker_pid"))
        if pid is None or pid in (os.getpid(), os.getppid()):
            return
        status = card.get("status")
        if status == "review" and not latest.get("ended_at"):
            return
        if not self.alive(pid):
            return
        command = self.command_line(pid)
        if not command or not _mentions(command, card_id):
            return
        finding = self.note(key, "orphan_worker", f"card {card_id} is {status} but a worker from an earlier session "
                                                  f"(pid {pid}) is still running")
        if not self.do(key, "orphan_worker_terminated",
                       f"terminate orphan worker pid {pid} of card {card_id} (its command line names the card)",
                       functools.partial(self.killer, pid)):
            self.escalate(finding, f"pid {pid} could not be terminated; a person must stop it")

    # -- across the plan ---------------------------------------------------------------------

    def worktrees(self) -> None:
        """Worktrees without cards. `git worktree list --porcelain`: a worktree under .worktrees/ whose directory
        name is a card of this plan that is archived or no longer resolves is reported as orphan_worktree. It is NOT
        removed (cleanup is a separate hardening step) and NOT blocked: it stops nothing. (Cards without worktrees,
        the other direction, are checked per running card in _running.)"""
        code, out, err = _git(self.repo, ["worktree", "list", "--porcelain"])
        if code != 0:
            self.note(_PROJECT_KEY, "reconcile_error", f"git worktree list failed: {_first_line(err)}", blocked=True)
            return
        for path in _worktree_paths(out):
            where = pathlib.PurePath(path)
            if where.parent.name != ".worktrees" or where.name not in self.cards:
                continue
            card = self.cards[where.name]
            if card is None or card.get("status") == "archived":
                state = "no longer resolves" if card is None else "is archived"
                self.note(self.card_task[where.name], "orphan_worktree",
                          f"worktree {path} belongs to card {where.name}, which {state}; left in place (cleanup is a "
                          f"separate step)")

    def _intent_state(self, item: dict, plan_keys: list[str]) -> tuple[bool, str]:
        """Has reconcile settled the state this open intent was guarding? Its key is a plan task key, or anything
        else for an action that spans the plan (then every task counts). Resolved when the task ended this run with
        nothing blocked, whether it was consistent all along or was repaired. Card creation is judged by whether the
        plan's tasks each have both cards recorded: reconcile creates nothing, so an unfinished creation stays
        open and blocks, with the instruction to re-run it (it is idempotent, ASES-REC-03)."""
        key = item["key"]
        scope = [key] if key in self.rows else [*self.rows, _PROJECT_KEY]
        if item["kind"] == intents_mod.KIND_CREATE_CARDS:
            # Judged on the cards alone: an unrelated merge problem in one of these tasks does not make the creation
            # unfinished.
            wanted = [key] if key in plan_keys else plan_keys
            missing = [k for k in wanted if k not in self.rows
                       or not (self.rows[k]["work_card_id"] and self.rows[k]["merge_card_id"])]
            if missing:
                return False, (f"card creation did not finish: no complete plan_tasks row for {', '.join(missing)}; "
                               f"re-run swarm approve (card creation is idempotent, ASES-REC-03)")
            gone = [k for k in wanted if any(f.kind == "missing_card" for f in self.task_blocked.get(k, []))]
            if gone:
                return False, (f"card creation did not finish: a card of {', '.join(gone)} no longer resolves on "
                               f"the board; re-run swarm approve (card creation is idempotent, ASES-REC-03)")
            return True, "every task of the plan has both cards"
        stuck = sorted({f.kind for k in scope for f in self.task_blocked.get(k, [])})
        if stuck:
            return False, f"the task still has unresolved findings: {', '.join(stuck)}"
        fixed = sorted({repair_kind for k in scope for repair_kind in self.task_repaired.get(k, [])})
        return True, f"repaired {', '.join(fixed)}" if fixed else "state consistent"

    def resolve_intents(self) -> None:
        """Open intents: an action that started and never wrote its completion record. One whose task reconcile has
        settled is closed with mark_recovered (and the closing is itself logged as a repair); one it could not settle
        is reported as a blocked open_intent finding and stays open."""
        plan_keys = [t.key for t in self.plan.tasks]
        for item in self.open_intents:
            key = item["key"] or _PROJECT_KEY
            try:
                settled, text = self._intent_state(item, plan_keys)
                label = f"intent #{item['id']} ({item['kind']} {key}, started {item['started_at']})"
                if settled:
                    self.do(key, "intent_recovered", f"{label}: {text}",
                            functools.partial(intents_mod.mark_recovered, self.conn, item["id"],
                                              f"reconcile-on-start: {text}"))
                else:
                    self.note(key, "open_intent", f"{label} is still open: {text}", blocked=True)
            except Exception as exc:  # noqa: BLE001 - one bad intent must not hide the others
                self.note(key, "reconcile_error", f"intent #{item['id']}: {type(exc).__name__}: {exc}"[:300],
                          blocked=True)

    def execute(self) -> ReconcileReport:
        try:
            self.open_intents = intents_mod.open_intents(self.conn, self.project)
        except Exception as exc:  # noqa: BLE001 - reported below, the rest of the run still has value
            self.note(_PROJECT_KEY, "reconcile_error",
                      f"could not read the intent records: {type(exc).__name__}: {exc}"[:300], blocked=True)
        rows = self.conn.execute(
            "SELECT task_key, work_card_id, merge_card_id, role FROM plan_tasks WHERE project = ? ORDER BY rowid",
            (self.project,),
        ).fetchall()
        self.rows = {row["task_key"]: row for row in rows}
        for row in rows:
            self.run_task(row)
        self.worktrees()
        self.resolve_intents()
        return ReconcileReport(self.findings, self.repairs, self.blocked)


def reconcile(
    board: str, repo, plan, *, conn, apply: bool = True, alive=pid_alive, killer=terminate_tree,
    command_line=process_command_line,
) -> ReconcileReport:
    """ASES-REC-04: "On start the controller compares the board, Git and its database: merge cards that are done
    without a merge record, merge records without a done card, candidates without a verdict, worktrees without cards,
    cards without worktrees, running cards whose worker is gone. It repairs what is safe and blocks the rest with an
    explanation." Also ASES-REC-03 (section 19.4: card creation is idempotent, so a half-finished creation is
    repeated, not repaired here: an open create_cards intent blocks until it has been) and the section 19.4 line
    "Orphan worker processes from a previous controller session are found by card ID and terminated before new work
    starts". Section 22.7 is the acceptance test: after a kill during a running card, during a candidate build, or
    between the fast-forward and the merge-card completion, a restart leaves no duplicate card, no orphan worker and
    no half-merged state, and every repair is logged.

    Per plan task (the plan_tasks rows of plan.project), in this order:
      a. check()'s findings: a card that no longer resolves, a done merge with no completed record, a done merge
         recorded as reverted. missing_card and done_but_reverted are blocked.
      b. merge card done without a completed record: the record is written from git when the integration branch has a
         commit carrying "Merge card: <id>"; a review-only task gets its no-op record; otherwise blocked.
      c. merge record without a done card (the crash between the fast-forward and the card completion): the card is
         completed as "merged <sha> (recovered)", or as the no-op text for a no-op record.
      d. merge record never completed: if the squash commit is on the integration branch the record is finished and
         the card completed; if not, the candidate never landed, which is reported as a "candidate_discarded" repair
         that is never applied (the merge queue redoes it). With no record at all but a landed commit and a done work
         card, the record is written from git and the card completed (the ASES database was lost).
      f. running card whose worker pid is not alive: reclaimed. No pid at all: blocked. A live worker is not touched.
      g. card that is not running whose latest run's worker is alive AND whose command line names the card:
         terminated with `killer`. A command line that does not name the card, or cannot be read, is never killed.
      h. worktrees without cards (orphan_worktree, reported, not removed, not blocked) and running cards without a
         worktree (missing_worktree, blocked).
    Then e: every open intent of the project whose task reconcile settled is closed with intents.mark_recovered; the
    rest are reported as blocked "open_intent" findings.

    With apply=False nothing changes (no write, no card mutation, no kill): the report says what WOULD be repaired,
    with applied False. Every applied repair is logged once as a reconcile_repair event, and a run is safe to repeat:
    a second run after applying finds nothing new to repair. One failing hermes or git call is a "reconcile_error"
    finding for that task and the other tasks carry on. `alive`, `killer` and `command_line` default to the real
    process helpers of this module and are what tests replace."""
    return _Pass(board, repo, plan, conn, apply, alive, killer, command_line).execute()
