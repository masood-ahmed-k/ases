"""The kill switch: swarm stop and swarm resume (section 19.6; ASES-REC-06).

"hermes pause alone is not enough, because it never kills work in flight" (section 19.6). A stop therefore does
six things, in this order, and the whole call keeps a deadline (30 seconds, the number in the requirement):

  a. sets the stop flag (project_state.status = 'stopped'), so the polling loop and the merge queue, which call
     stop_requested() between steps, see the stop before anything else happens;
  b. hermes pause, so nothing new is dispatched;
  c. finds the cards of THIS plan that are running, and the cards in review that have a live reviewer run;
  d. reclaims each of them: Hermes drops the claim as an operator action, instead of noticing a dead worker
     later and counting a crash against the card (section 19.1, "Worker crash or stale claim");
  e. terminates each card's worker process tree, after proving the process really is that card's worker;
  f. stops the Docker sandboxes whose name or labels carry one of the plan's card ids.

Killing is the half that can do damage, so it has rules. A process is terminated only when ALL of these hold: its
pid was recorded by Hermes on a live run of a card of this plan; the pid is still alive; its command line, read
just before, names that card id; and it is neither this process nor its parent. The user may have unrelated Hermes
chat sessions, the gateway and the dispatcher running, and terminating any of them is a failure of this module. A
process whose command line cannot be read is not terminated. Every pid that was left alone is reported, with the
reason, in StopReport.unverified.

The worker pids are read in step c, BEFORE the reclaim in step d: reclaiming ends the run, and a run that has
ended no longer proves that the pid is a worker of that card.

Windows: os.kill(pid, 0) does not probe a process there, it TERMINATES it, so os.kill is never called on that
platform. Liveness uses OpenProcess and GetExitCodeProcess, termination uses `taskkill /PID n /T /F`. Every outside
effect (Hermes, processes, Docker, the clock) is a parameter of stop_all, so a test never touches a real one.

The stop flag lives in project_state (schema in db.py). request_stop, clear_stop and stop_requested are single
statements of their own, so this module needs nothing from bounds.py and the polling loop can call
stop_requested() cheaply.

"Keep the kill switch working at all times" (section 21.3): stop_all and resume_all never raise, every failure
becomes an entry in the StopReport or a reason in resume_all's answer. (Two documented exceptions sit outside
them: write_stop_report raises OSError if the directory cannot be written, and stop_requested lets a database
error through so the caller decides what an unreadable flag means.) To keep the 30 seconds honest, stop_all runs
each injected call on a short-lived daemon thread and abandons one that does not answer in time: an injected
callable must therefore not use the caller's sqlite connection, which belongs to the caller's thread.

Not done here: a merge or gate step already running inside a `swarm run` process is not interrupted. The flag
stops the loop and the merge queue between steps; that wiring lives in the polling loop.
"""
from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from . import events
from . import hermes as hermes_mod

STOPPED = "stopped"
DEADLINE_SECONDS = 30  # ASES-REC-06: "within 30 seconds"

# The Hermes calls (pause, list, show, reclaim) may use at most this share of the deadline, so a slow or hung
# Hermes cannot starve the process kills and the container stops, which need nothing from it.
_HERMES_SHARE = 2 / 3
# No single outside call may use more than this share of the deadline.
_CALL_SHARE = 1 / 3
_MAX_PARALLEL = 8  # outside calls in flight at once (each Hermes or Docker command is a process of its own)
_KILL_SETTLE_SECONDS = 3.0  # how long to wait for a terminated pid to disappear
_POLL_SECONDS = 0.2
_HELPER_TIMEOUT = 8  # seconds for taskkill, powershell, docker ps, pgrep, ps
_STOP_TIMEOUT = 15  # docker stop -t 5 waits up to 5 seconds before it kills, plus the CLI's own time
_MIN_PID = 5  # pids 0 to 4 are "none", the idle process, the system: never a worker
_MAX_PID = 2 ** 32
_SIGTERM = int(signal.SIGTERM)
_SIGKILL = int(getattr(signal, "SIGKILL", 9))  # POSIX only ever sends it; Windows has no SIGKILL
_BRANCH_PREFIX = "swarm/"  # the branch prefix of every ASES work and fix card (controller.create_cards_from_plan)

_WHY_SELF = "pid is this process or its parent"
_WHY_UNREADABLE = "command line could not be read"
_WHY_OTHER_PROCESS = "command line does not contain the card id"
_NO_TIME = "the time limit was reached"


# ---------------------------------------------------------------------------------------------
# Text that ends up in a report: one line, ASCII only, secret patterns redacted.
# ---------------------------------------------------------------------------------------------


def _safe(text: Any, limit: int = 300) -> str:
    """Text for the report: a single line, ASCII only (the Windows console is cp1252 and crashes on anything
    else), known secret patterns redacted (ASES-SEC-01), and capped. Exception text from Hermes, a card id or a
    container name may all end up on a terminal or in stop-<time>.json."""
    clean = events.redact({"text": str(text)})["text"]
    clean = " ".join(clean.split())
    return clean.encode("ascii", "backslashreplace").decode("ascii")[:limit]


def _err(exc: BaseException) -> str:
    return _safe(f"{type(exc).__name__}: {exc}")


def _cid(card_id: Any) -> str:
    return _safe(card_id, 120)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------------------------
# The stop flag
# ---------------------------------------------------------------------------------------------


def request_stop(conn: sqlite3.Connection, project: str, reason: str | None = None) -> None:
    """Set the stop flag: project_state.status becomes 'stopped' and stop_reason records why. One upsert, so it is
    atomic and idempotent: a second call leaves one row, keeps started_at, deadline_at and replans as they were,
    and the latest reason wins. The reason is stored ASCII-only, because `swarm status` may print it."""
    conn.execute(
        "INSERT INTO project_state (project, status, stop_reason, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(project) DO UPDATE SET status = excluded.status, stop_reason = excluded.stop_reason, "
        "updated_at = excluded.updated_at",
        (project, STOPPED, _safe(reason or "swarm stop", 500), _utc_now()),
    )


def stop_requested(conn: sqlite3.Connection, project: str) -> bool:
    """True only while the KILL SWITCH holds the project stopped (status 'stopped'). Only that one status counts:
    a project bounds.py merely paused is not a kill-switch stop, and one that has finished is not stopped either.
    A database error is not swallowed: the caller decides what an unreadable flag means.

    Round 6 fix (found alongside recovery.Bounds's duplicate): this is deliberately NARROWER than
    bounds.stop_requested(conn, project), which is True for 'stopped' OR 'paused' and answers a different
    question, "should new work happen right now" (ASES-REC-06, ASES-CTL-01) - that is what the polling loop and
    `swarm run`'s between-pass check actually call. This function is kept, under its existing name, for the kill
    switch's OWN narrower question, "did the kill switch specifically stop this project": stop_all uses it right
    here to confirm request_stop's own write landed (see _stop_steps, step a), and code outside this module
    (test_cli_commands.py, which this package does not own) already and correctly depends on exactly this
    distinction to assert what `swarm stop` and `swarm resume` did, as opposed to what a separately reached bound
    did. Renaming or deleting it would break that external, deliberate usage for no behavioural gain, so round 6
    keeps both functions and both names: use bounds.stop_requested for "should the loop do new work", use this
    one for "did the kill switch specifically stop it"."""
    row = conn.execute("SELECT status FROM project_state WHERE project = ?", (project,)).fetchone()
    return row is not None and row[0] == STOPPED


def clear_stop(conn: sqlite3.Connection, project: str) -> bool:
    """Lift the stop flag. Only a 'stopped' project changes: a paused, running, planning or finished one is left
    exactly as it is, so a stray `swarm resume` can never un-finish a project. The project goes back to 'running'
    if it had been started (started_at is set) and to 'planning' if it had not, so that bounds.start_project can
    still record its start. One statement; returns True when a stop was actually cleared."""
    cursor = conn.execute(
        "UPDATE project_state SET status = CASE WHEN started_at IS NULL THEN 'planning' ELSE 'running' END, "
        "stop_reason = NULL, updated_at = ? WHERE project = ? AND status = ?",
        (_utc_now(), project, STOPPED),
    )
    return cursor.rowcount > 0


# ---------------------------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------------------------


@dataclasses.dataclass
class StopReport:
    """What one swarm stop did, JSON-serialisable through to_dict(). Every failure is an entry here and nothing
    is raised. `unverified` lists the worker pids that were NOT terminated and why: a pid whose command line
    could not be read or did not name the card, this process or its parent, or a pid the time limit ran out on.
    `within_deadline` is False when the total time exceeded the deadline or when any step had to be skipped or
    abandoned because time ran out; `notes` says which."""
    started_at: str = ""
    finished_at: str = ""
    seconds: float = 0.0
    paused: bool = False
    reclaimed: list[str] = dataclasses.field(default_factory=list)
    reclaim_errors: list[dict] = dataclasses.field(default_factory=list)  # {card_id, error}
    killed: list[dict] = dataclasses.field(default_factory=list)  # {card_id, pid}
    unverified: list[dict] = dataclasses.field(default_factory=list)  # {card_id, pid, why}
    containers_stopped: list[str] = dataclasses.field(default_factory=list)
    flag_set: bool = False
    within_deadline: bool = True
    notes: list[str] = dataclasses.field(default_factory=list)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def _file_stamp(started_at: str) -> str:
    """20260919T203045Z from the report's ISO start time (the current time when it does not parse). No colons:
    this becomes a Windows file name."""
    try:
        moment = datetime.fromisoformat(started_at)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        moment = datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def write_stop_report(report: StopReport, directory: str | pathlib.Path) -> pathlib.Path:
    """Section 19.6: "write a stop report". Writes stop-<UTC timestamp>.json (UTF-8, indented) into `directory`,
    creating it, and returns the path. A second stop in the same second gets a -2, -3 suffix instead of
    overwriting the first. Raises OSError when the directory cannot be written: the stop itself is already done
    by then, so a caller prints the report and carries on."""
    directory = pathlib.Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = _file_stamp(report.started_at)
    text = json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n"
    attempt = 1
    while True:
        name = f"stop-{stamp}.json" if attempt == 1 else f"stop-{stamp}-{attempt}.json"
        path = directory / name
        try:
            with open(path, "x", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
        except FileExistsError:
            attempt += 1
            continue
        return path


# ---------------------------------------------------------------------------------------------
# Outside effects: processes and Docker. Each one never raises and carries a short timeout.
# ---------------------------------------------------------------------------------------------


def _run(args: list[str], timeout: float) -> tuple[int, str]:
    """The one place this module starts a process: (exit code, stdout as text). Raises what subprocess raises
    (OSError when the program is missing, TimeoutExpired); callers catch it. A test replaces this function."""
    result = subprocess.run(args, capture_output=True, timeout=timeout)
    return result.returncode, result.stdout.decode("utf-8", errors="replace")


_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5


class _Win32:
    """The three kernel32 calls pid_alive needs, behind an object so a test can hand in a fake. The argument and
    return types are set explicitly: ctypes assumes a C int for a return value, and a 64-bit process handle does
    not fit in one."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        self._ctypes = ctypes
        self._wintypes = wintypes
        self._kernel32 = kernel32

    def open(self, pid: int):
        return self._kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)

    def last_error(self) -> int:
        return self._ctypes.get_last_error()

    def exit_code(self, handle) -> int | None:
        code = self._wintypes.DWORD()
        if not self._kernel32.GetExitCodeProcess(handle, self._ctypes.byref(code)):
            return None
        return code.value

    def close(self, handle) -> None:
        self._kernel32.CloseHandle(handle)


def _windows_pid_alive(pid: int, api) -> bool:
    """Alive when OpenProcess succeeds and the exit code is STILL_ACTIVE (259). A process that exists but will
    not let us query it (access denied, for example an elevated one) counts as alive, and so does a process whose
    exit code cannot be read: when unsure, say alive, so the kill switch never reports a process gone that may
    not be."""
    handle = api.open(pid)
    if not handle:
        return api.last_error() == _ERROR_ACCESS_DENIED
    try:
        code = api.exit_code(handle)
    finally:
        api.close(handle)
    return code is None or code == _STILL_ACTIVE


def _posix_pid_alive(pid: int, send: Callable[[int, int], Any]) -> bool:
    """Signal 0 probes without signalling. `send` is os.kill, passed in by pid_alive on POSIX only: it is never a
    default here, so this function cannot reach os.kill on Windows by accident. pid must be positive: os.kill
    with 0 or -1 addresses a whole process group or every process."""
    if pid <= 0:
        return False
    try:
        send(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def pid_alive(pid: int) -> bool:
    """Is this pid a live process? Never raises: an invalid pid is not alive, and any failure to find out says
    alive (see _windows_pid_alive). On Windows this uses ctypes, never os.kill."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        if sys.platform == "win32":
            return _windows_pid_alive(pid, _Win32())
        return _posix_pid_alive(pid, os.kill)
    except Exception:  # noqa: BLE001 - never raises: unsure means alive
        return True


def _windows_terminate_tree(pid: int) -> bool:
    """`taskkill /PID n /T /F`: the process and every descendant, forcibly. Exit code 0 means it worked."""
    code, _ = _run(["taskkill", "/PID", str(pid), "/T", "/F"], _HELPER_TIMEOUT)
    return code == 0


def _posix_children(pid: int) -> list[int]:
    try:
        code, out = _run(["pgrep", "-P", str(pid)], _HELPER_TIMEOUT)
    except Exception:  # noqa: BLE001 - no pgrep, or it hung: no children known
        return []
    if code != 0:
        return []
    return [int(word) for word in out.split() if word.isdigit()]


def _descendants(pid: int, children: Callable[[int], list[int]], limit: int = 256) -> list[int]:
    """`pid` and every descendant, breadth first (parents before children), each once, at most `limit` of them."""
    found = [pid]
    seen = {pid}
    index = 0
    while index < len(found) and len(found) < limit:
        for child in children(found[index]):
            if child > 0 and child not in seen:
                seen.add(child)
                found.append(child)
        index += 1
    return found


def _posix_terminate_tree(
    pid: int, *, send: Callable[[int, int], Any], children: Callable[[int], list[int]],
    alive: Callable[[int], bool], sleep: Callable[[float], Any], grace: float = 2.0,
) -> bool:
    """SIGTERM the process and every descendant (leaves first), wait up to `grace` seconds, SIGKILL whatever is
    left, and report whether the root is gone. The tree is collected BEFORE anything is signalled, because a dead
    parent hands its children to init and they could no longer be found. Descendants are walked instead of
    signalling a process group, since a group can also hold the dispatcher that started the worker. `send` is
    os.kill, passed in by terminate_tree on POSIX only."""
    if pid <= 0:
        return False
    tree = _descendants(pid, children)

    def signal_survivors(number: int) -> None:
        # Only pids that are still alive are signalled: a pid that has exited may already belong to some
        # unrelated process.
        for target in reversed(tree):
            if alive(target):
                try:
                    send(target, number)
                except OSError:
                    pass

    signal_survivors(_SIGTERM)
    waited = 0.0
    while waited < grace and any(alive(target) for target in tree):
        sleep(0.1)
        waited += 0.1
    if any(alive(target) for target in tree):
        signal_survivors(_SIGKILL)
        sleep(0.1)
    return not alive(pid)


def terminate_tree(pid: int) -> bool:
    """Terminate a worker and everything it started: True when the process is gone. Never raises. Windows uses
    `taskkill /PID n /T /F` and never os.kill; a pid below _MIN_PID is refused."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid < _MIN_PID:
        return False
    try:
        if sys.platform == "win32":
            return _windows_terminate_tree(pid)
        return _posix_terminate_tree(
            pid, send=os.kill, children=_posix_children, alive=pid_alive, sleep=time.sleep,
        )
    except Exception:  # noqa: BLE001 - never raises
        return False


def _windows_command_line(pid: int) -> str | None:
    """The command line through CIM (wmic is gone from current Windows). The pid is an int, so nothing can be
    injected into the query."""
    script = f"(Get-CimInstance Win32_Process -Filter 'ProcessId = {int(pid)}').CommandLine"
    code, out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], _HELPER_TIMEOUT)
    return out if code == 0 else None


def _read_proc_cmdline(pid: int) -> bytes | None:
    try:
        return pathlib.Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None


def _posix_command_line(pid: int, read_proc: Callable[[int], bytes | None] = _read_proc_cmdline) -> str | None:
    raw = read_proc(pid)
    if raw:
        return raw.replace(b"\0", b" ").decode("utf-8", errors="replace")
    code, out = _run(["ps", "-o", "args=", "-p", str(pid)], _HELPER_TIMEOUT)
    return out if code == 0 else None


def process_command_line(pid: int) -> str | None:
    """The command line of a live process, or None when it cannot be read (no such process, no permission, the
    tool missing or too slow). Never raises. The kill safety rule depends on it: a pid whose command line cannot
    be read is never terminated."""
    try:
        pid = int(pid)
        if pid <= 0:
            return None
        line = _windows_command_line(pid) if sys.platform == "win32" else _posix_command_line(pid)
    except Exception:  # noqa: BLE001 - never raises
        return None
    line = (line or "").strip()
    return line or None


def _mentions(text: str | None, card_id: str) -> bool:
    """Does `text` name this card id? The id must not touch a letter or digit on either side, so card t_1 is not
    found inside a command line that belongs to card t_12."""
    if not text or not card_id:
        return False
    pattern = r"(?<![A-Za-z0-9])" + re.escape(card_id) + r"(?![A-Za-z0-9])"
    return re.search(pattern, text) is not None


def default_list_containers(card_ids: list[str]) -> list[str]:
    """Names of the running Docker containers whose name or labels name one of `card_ids` (the sandbox of a
    worker, section 21.3). `docker ps` lists every container the user has, so the filter here is what keeps an
    unrelated one safe. Returns [] and never raises when Docker is absent or its daemon is down.

    CONTAINERS (round 17): confirmed against Hermes 0.21.3's real, installed container labels
    (scripts/hermes_container_labels_check.py; ases.containers's module docstring has the full empirical
    finding, file:line). Every container Hermes creates for a worker carries hermes-agent=1
    (tools/environments/docker.py:585 in the installed source), so `docker ps` is now filtered to that label
    SERVER-SIDE before the name/label text match below ever runs: a container that is not Hermes's own can
    never match, whatever its name or its OTHER labels happen to say (Docker Desktop's own
    "desktop.docker.io/..." labels among them) -- closing exactly the false-positive risk the text match alone
    could not rule out. This does NOT make the id match itself reliable: under ASES's own current profile
    configuration a kanban-dispatched worker's container label hermes-task-id is always the literal string
    "default", never the real card id (ases.containers's module docstring explains why, with file:line), so
    this function will not find a real worker's container by card id today. ases.containers.
    find_orphan_containers finds this project's orphaned containers a different way, by Hermes profile, since
    that label IS the real active profile name."""
    ids = [str(card_id) for card_id in (card_ids or []) if card_id]
    if not ids:
        return []
    try:
        code, out = _run(
            ["docker", "ps", "--filter", "label=hermes-agent=1", "--format", "{{.Names}}|{{.Labels}}"],
            _HELPER_TIMEOUT,
        )
    except Exception:  # noqa: BLE001 - no docker, or it hung: nothing to stop
        return []
    if code != 0:
        return []
    names: list[str] = []
    for line in out.splitlines():
        name, _, labels = line.partition("|")
        name = name.strip()
        if name and name not in names and any(_mentions(name, i) or _mentions(labels, i) for i in ids):
            names.append(name)
    return names


def default_list_profile_containers(profiles: list[str]) -> list[str]:
    """Names of the running containers Hermes created (label hermes-agent=1, matched by Docker itself) whose
    hermes-profile label is one of `profiles`. This, not default_list_containers, is what finds a real worker's
    sandbox: under ASES's own profile config a dispatched worker's container never carries its card id (its
    hermes-task-id label is the literal "default"; ases.containers's module docstring has the evidence, file:line
    in Hermes 0.21.3), while hermes-profile is the worker's real profile. Returns [] and never raises when Docker
    is absent or its daemon is down."""
    wanted = {str(profile) for profile in (profiles or []) if profile}
    if not wanted:
        return []
    from . import containers as containers_mod  # imported here: ases.containers imports this module
    try:
        found = containers_mod.default_list_hermes_containers()
    except Exception:  # noqa: BLE001 - no docker, or it hung: nothing to stop
        return []
    return list(dict.fromkeys(name for name, profile in found if name and profile in wanted))


def default_stop_container(name: str) -> bool:
    """`docker stop -t 5 <name>`: True when Docker stopped it. Never raises. A name that starts with a dash is
    refused so it can never be read as an option."""
    name = str(name or "").strip()
    if not name or name.startswith("-"):
        return False
    try:
        code, _ = _run(["docker", "stop", "-t", "5", name], _STOP_TIMEOUT)
    except Exception:  # noqa: BLE001 - never raises
        return False
    return code == 0


# ---------------------------------------------------------------------------------------------
# swarm stop
# ---------------------------------------------------------------------------------------------


@dataclasses.dataclass
class _Outcome:
    """What one bounded outside call did: ok with a value, or not ok with a short ASCII reason."""
    ok: bool
    value: Any = None
    error: str = ""


@dataclasses.dataclass
class _Target:
    """One card the stop acts on. `pids` were read before the reclaim (see the module docstring)."""
    card_id: str
    status: str
    pids: list[int]


@dataclasses.dataclass
class _Hooks:
    """The outside effects of one stop: everything stop_all can be told to fake."""
    pause: Callable[..., Any]
    kanban_list: Callable[..., Any]
    kanban_show: Callable[..., Any]
    reclaim: Callable[..., Any]
    killer: Callable[[int], bool]
    alive: Callable[[int], bool]
    command_line: Callable[[int], str | None]
    list_containers: Callable[[list[str]], list[str]]
    list_profile_containers: Callable[[list[str]], list[str]]
    stop_container: Callable[[str], bool]


def _start(fn: Callable[..., Any], args: tuple, kwargs: dict) -> tuple[threading.Thread, dict]:
    """Run fn(*args, **kwargs) on a daemon thread; its value or exception lands in the returned box. Daemon, so a
    call that never returns cannot keep the process alive after swarm stop has finished."""
    box: dict = {}

    def target() -> None:
        try:
            box["value"] = fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - handed to the caller, never lost on the thread
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True, name="ases-killswitch-call")
    thread.start()
    return thread, box


def _outcome(timed_out: bool, box: dict, budget: float) -> _Outcome:
    """The result of a call that was given `budget` seconds. `timed_out` is read once by the caller: the box may
    only be read when the thread has finished."""
    if timed_out:
        return _Outcome(False, error=f"no answer within {budget:.1f}s")
    if "error" in box:
        return _Outcome(False, error=_err(box["error"]))
    return _Outcome(True, value=box.get("value"))


class _Stop:
    """One swarm stop in progress: the clock, the deadlines and the report being filled in.

    Every outside call goes through call() or call_all(), which run it on a helper thread and give up on it when
    its time is spent. That is what keeps the 30 seconds honest: Hermes' own commands carry timeouts of 20 to 30
    seconds and take no timeout argument (hermes.kanban_list, kanban_show and kanban_reclaim), so one hung
    command would use the whole budget on its own."""

    def __init__(self, report: StopReport, deadline_seconds: float, clock: Callable[[], float],
                 sleep: Callable[[float], Any]) -> None:
        self.report = report
        self.clock = clock
        self.sleep = sleep
        self.start = clock()
        self.deadline_at = self.start + deadline_seconds
        self.hermes_until = self.start + deadline_seconds * _HERMES_SHARE
        self.call_cap = deadline_seconds * _CALL_SHARE
        self.cut_short = False

    def note(self, text: str) -> None:
        self.report.notes.append(_safe(text))

    def left(self, until: float) -> float:
        return until - self.clock()

    def call(self, until: float, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> _Outcome:
        """fn(*args, **kwargs) with at most min(time left until `until`, the per-call cap) seconds. Time that has
        run out (before the call or during it) marks the whole stop as cut short."""
        budget = min(self.left(until), self.call_cap)
        if budget <= 0:
            self.cut_short = True
            return _Outcome(False, error=_NO_TIME)
        thread, box = _start(fn, args, kwargs)
        thread.join(budget)
        timed_out = thread.is_alive()
        if timed_out:
            self.cut_short = True
        return _outcome(timed_out, box, budget)

    def call_all(self, until: float, fn: Callable[[Any], Any], items: list) -> list[tuple[Any, _Outcome]]:
        """fn(item) for every item, up to _MAX_PARALLEL at once (so N container stops cost one, not N, without a
        crowded board starting dozens of processes), under one shared budget. Returns (item, outcome) in item
        order; an item whose turn came after the budget was spent gets the no-time outcome."""
        budget = min(self.left(until), self.call_cap)
        if budget <= 0:
            self.cut_short = True
            return [(item, _Outcome(False, error=_NO_TIME)) for item in items]
        end = time.monotonic() + budget
        results: list[tuple[Any, _Outcome]] = []
        for offset in range(0, len(items), _MAX_PARALLEL):
            batch = items[offset:offset + _MAX_PARALLEL]
            if time.monotonic() >= end:
                self.cut_short = True
                results.extend((item, _Outcome(False, error=_NO_TIME)) for item in batch)
                continue
            started = [(item, *_start(fn, (item,), {})) for item in batch]
            for item, thread, box in started:
                thread.join(max(end - time.monotonic(), 0.0))
                timed_out = thread.is_alive()
                if timed_out:
                    self.cut_short = True
                results.append((item, _outcome(timed_out, box, budget)))
        return results


def _dicts(value: Any) -> list[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, (list, tuple)) else []


def _ids(entries: Any) -> set[str]:
    """Card ids out of a Hermes parents list, which holds ids (or, defensively, dicts that carry one)."""
    found: set[str] = set()
    for entry in entries if isinstance(entries, (list, tuple)) else []:
        if isinstance(entry, dict):
            entry = entry.get("id") or entry.get("task_id") or entry.get("parent_id")
        if entry not in (None, ""):
            found.add(str(entry))
    return found


def _as_pid(value: Any) -> int | None:
    """A worker pid out of a Hermes field, or None. Hermes hands numbers over as ints or digit strings; anything
    else is not a pid, and neither is one below _MIN_PID (0 means none recorded, the lowest ids are the system)."""
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        return None
    try:
        pid = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return pid if _MIN_PID <= pid < _MAX_PID else None


def _plan_card_ids(conn: sqlite3.Connection, project: str) -> set[str]:
    """Every card id the plan_tasks rows of this project name: the current work cards (a fix card once the
    controller has repointed the row at it) and the merge cards. This set is what keeps other projects' cards on
    the same board out of the stop."""
    ids: set[str] = set()
    rows = conn.execute("SELECT work_card_id, merge_card_id FROM plan_tasks WHERE project = ?", (project,))
    for row in rows.fetchall():
        for card_id in (row[0], row[1]):
            if card_id:
                ids.add(str(card_id))
    return ids


def _may_be_ours(card_id: str, listed: dict, plan_ids: set[str]) -> bool:
    """Is this listed card worth a `hermes kanban show`? A card plan_tasks names is. Any other card is only when it
    might be a fix card, and its listing already rules that out when it shows a branch that is not ASES's, so an
    unrelated card on the same board costs no call at all."""
    if card_id in plan_ids:
        return True
    branch = listed.get("branch_name")
    return branch is None or str(branch).startswith(_BRANCH_PREFIX)


def _inspect_card(
    stop: _Stop, plan_ids: set[str], fix_ids: list[str], card_id: str, status: str, listed: dict, read: _Outcome,
) -> _Target | None:
    """The target for one listed card, or None when it is not this plan's or has nothing live to stop. `read` is
    the outcome of `hermes kanban show` for it.

    A card is this plan's when plan_tasks names it, or when it is a fix card: it has a card of this plan as a
    parent AND an ASES branch (swarm/...). The parent alone would also match a card the user made by hand that
    depends on an ASES card, and stopping that one would be the failure this module must never commit."""
    shown = read.value if read.ok and isinstance(read.value, dict) else None
    if shown is None:
        stop.note(f"could not read card {_cid(card_id)}: {read.error or 'unexpected answer'}")
    if card_id not in plan_ids:
        if shown is None or not (_ids(shown.get("_parents")) & plan_ids):
            return None
        if not str(shown.get("branch_name") or "").startswith(_BRANCH_PREFIX):
            return None
        fix_ids.append(card_id)

    # A run is live while it has no ended_at. A card can hold an older live run as well as its latest one, so
    # every live run's pid is a candidate, not only the last.
    live_runs = [run for run in _dicts((shown or {}).get("_runs")) if run.get("ended_at") in (None, "")]
    pids = [pid for pid in (_as_pid(run.get("worker_pid")) for run in live_runs) if pid is not None]
    if status == "running":
        # The card's own worker_pid field (in the show and in the listing) covers a run that has no pid of its
        # own yet, and a show that failed.
        for source in (shown, listed):
            pid = _as_pid(source.get("worker_pid")) if source else None
            if pid is not None:
                pids.append(pid)
    elif not live_runs:
        return None  # a card waiting in review with no reviewer on it: nothing is running
    return _Target(card_id, status, list(dict.fromkeys(pids)))


def _find_targets(
    stop: _Stop, hooks: _Hooks, board: str, plan_ids: set[str],
) -> tuple[list[_Target], list[str]]:
    """Step c: the cards to stop, and the ids of fix cards found on the way (the container search needs them).

    The two listings (running, review) are independent reads and go out together, and so do the `show` calls for
    the cards that need one: each Hermes command is a fresh process on Windows, so N sequential calls would cost
    N startups out of the 30 seconds. The reclaims that follow are writes and stay one at a time."""
    targets: list[_Target] = []
    fix_ids: list[str] = []
    if not plan_ids:
        stop.note("no cards are recorded for this plan, so there is nothing to reclaim or terminate")
        return targets, fix_ids
    listed: list[tuple[str, dict]] = []
    listings = stop.call_all(
        stop.hermes_until, lambda status: hooks.kanban_list(board, status=status), ["running", "review"])
    for status, listing in listings:
        if not listing.ok:
            stop.note(f"could not list {status} cards: {listing.error}")
            continue
        listed.extend((status, card) for card in _dicts(listing.value))
    candidates: dict[str, tuple[str, dict]] = {}
    for status, card in listed:
        card_id = str(card.get("id") or "")
        if card_id and card_id not in candidates and _may_be_ours(card_id, card, plan_ids):
            candidates[card_id] = (status, card)
    if not candidates:
        return targets, fix_ids
    reads = dict(stop.call_all(
        stop.hermes_until, lambda card_id: hooks.kanban_show(board, card_id), list(candidates)))
    for card_id, (status, card) in candidates.items():
        target = _inspect_card(stop, plan_ids, fix_ids, card_id, status, card, reads[card_id])
        if target is not None:
            targets.append(target)
    return targets, fix_ids


def _reclaim_all(stop: _Stop, hooks: _Hooks, board: str, targets: list[_Target], reason: str) -> None:
    """Step d. One card failing does not stop the others, and a card that could not be reached in time is
    recorded as an error like any other, not dropped."""
    for target in targets:
        outcome = stop.call(stop.hermes_until, hooks.reclaim, board, target.card_id, reason=reason)
        if outcome.ok:
            stop.report.reclaimed.append(_cid(target.card_id))
        else:
            stop.report.reclaim_errors.append({"card_id": _cid(target.card_id), "error": outcome.error})


def _alive(hooks: _Hooks, pid: int) -> bool:
    try:
        return bool(hooks.alive(pid))
    except Exception:  # noqa: BLE001 - unsure means alive, as in pid_alive
        return True


def _kill_workers(stop: _Stop, hooks: _Hooks, targets: list[_Target]) -> None:
    """Step e: terminate each worker that passes every safety rule, then wait briefly for the pids to go."""
    report = stop.report
    protected = {os.getpid(), os.getppid()}
    work = [(target.card_id, pid) for target in targets for pid in target.pids]
    for target in targets:
        if not target.pids:
            stop.note(f"card {_cid(target.card_id)}: no live worker pid is recorded, so nothing was terminated")

    def leave_alone(card_id: str, pid: int, why: str) -> None:
        report.unverified.append({"card_id": _cid(card_id), "pid": pid, "why": why})

    pending: list[tuple[str, int]] = []
    for index, (card_id, pid) in enumerate(work):
        if stop.left(stop.deadline_at) <= 0:
            stop.cut_short = True
            for late_card, late_pid in work[index:]:
                leave_alone(late_card, late_pid, f"skipped: {_NO_TIME}")
            break
        if pid in protected:
            leave_alone(card_id, pid, _WHY_SELF)
            continue
        if not _alive(hooks, pid):
            stop.note(f"card {_cid(card_id)}: worker pid {pid} had already exited")
            continue
        line = stop.call(stop.deadline_at, hooks.command_line, pid)
        text = line.value if line.ok and isinstance(line.value, str) else ""
        if not text.strip():
            leave_alone(card_id, pid, _WHY_UNREADABLE)
            continue
        if not _mentions(text, card_id):
            leave_alone(card_id, pid, _WHY_OTHER_PROCESS)
            continue
        kill = stop.call(stop.deadline_at, hooks.killer, pid)
        if kill.ok and kill.value:
            report.killed.append({"card_id": _cid(card_id), "pid": pid})
            pending.append((card_id, pid))
        elif not _alive(hooks, pid):
            stop.note(f"card {_cid(card_id)}: worker pid {pid} exited on its own before it was terminated")
        else:
            detail = f" ({kill.error})" if kill.error else ""
            stop.note(f"card {_cid(card_id)}: could not terminate worker pid {pid}{detail}")

    # taskkill returns before the process object is gone, so poll for a moment. At least one check always runs.
    settle_until = min(stop.deadline_at, stop.clock() + _KILL_SETTLE_SECONDS)
    while pending:
        pending = [(card_id, pid) for card_id, pid in pending if _alive(hooks, pid)]
        if not pending:
            break
        if stop.clock() >= settle_until:
            if stop.clock() >= stop.deadline_at:
                stop.cut_short = True
            break
        stop.sleep(_POLL_SECONDS)
    for card_id, pid in pending:
        stop.note(f"card {_cid(card_id)}: worker pid {pid} is still alive after it was terminated")


def _listed_containers(stop: _Stop, fn: Callable[[list[str]], list[str]], keys: list[str], what: str) -> list[str]:
    """One container listing inside the stop's time box: the names it returned, or [] with a note."""
    listing = stop.call(stop.deadline_at, fn, list(keys))
    if not listing.ok:
        stop.note(f"could not list Docker containers {what}: {listing.error}")
        return []
    try:
        return [str(name) for name in (listing.value or []) if name]
    except TypeError:
        stop.note(f"could not read the container list {what}: unexpected answer")
        return []


def _stop_containers(stop: _Stop, hooks: _Hooks, card_ids: list[str], profiles: list[str]) -> None:
    """Step f. Two listings, stopped together: the sandboxes whose name or labels name one of this plan's card ids
    (its plan_tasks cards and any fix cards found), and every running Hermes container of this project's own
    profiles (`profiles`). The second is the one that finds a real worker's sandbox (round 17: Hermes labels it
    with the worker's profile, never its card id; see default_list_profile_containers). Both listings only ever
    name containers carrying hermes-agent=1. Stopping by profile is right here, unlike the per-pass orphan sweep
    (ases.containers), which spares a profile with live work: swarm stop stops the whole system, so live work is
    exactly what must stop. Profile names are unique per machine across ASES projects (a hard constraint, see
    ases.containers's module docstring; swarm doctor's profile_isolation row checks it)."""
    names: list[str] = []
    if card_ids:
        names += _listed_containers(stop, hooks.list_containers, card_ids, "by card id")
    if profiles:
        names += _listed_containers(stop, hooks.list_profile_containers, profiles, "by Hermes profile")
    names = list(dict.fromkeys(names))
    if not names:
        return
    for name, outcome in stop.call_all(stop.deadline_at, hooks.stop_container, names):
        if outcome.ok and outcome.value:
            stop.report.containers_stopped.append(_safe(name, 200))
        else:
            detail = f": {outcome.error}" if outcome.error else ""
            stop.note(f"could not stop container {_safe(name, 200)}{detail}")


def _guarded(stop: _Stop, what: str, fn: Callable[..., Any], *args: Any) -> Any:
    """Run one step so that a bug in it costs that step, not the steps after it."""
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001 - the kill switch reports, it never raises
        stop.note(f"{what} failed unexpectedly: {_err(exc)}")
        return None


def _stop_steps(
    stop: _Stop, hooks: _Hooks, board: str, plan: Any, conn: sqlite3.Connection, reason: str, profiles: list[str],
) -> None:
    report = stop.report
    project = plan.project

    # a. The flag first, so the polling loop and the merge queue see the stop before anything else happens. It
    # is read back: a write that "succeeded" but did not stick is worth knowing about.
    try:
        request_stop(conn, project, reason)
        report.flag_set = stop_requested(conn, project)
        if not report.flag_set:
            stop.note("the stop flag was written but reads back as not set")
    except Exception as exc:  # noqa: BLE001 - a locked database must not stop the rest of the stop
        stop.note(f"could not set the stop flag: {_err(exc)}")

    # b. Nothing new is dispatched. A failure is recorded and the rest still runs: killing does not depend on it.
    paused = stop.call(stop.hermes_until, hooks.pause, reason=reason)
    report.paused = paused.ok
    if not paused.ok:
        stop.note(f"hermes pause failed: {paused.error}")

    # c. What is running, and whose it is.
    plan_ids = _guarded(stop, "reading the plan's cards", _plan_card_ids, conn, project) or set()
    found = _guarded(stop, "finding running cards", _find_targets, stop, hooks, board, plan_ids)
    targets, fix_ids = found if found else ([], [])

    # d. Release the claims, before anything is killed.
    _guarded(stop, "reclaiming cards", _reclaim_all, stop, hooks, board, targets, reason)

    # e. Terminate the workers that pass the safety rules.
    _guarded(stop, "terminating workers", _kill_workers, stop, hooks, targets)

    # f. Stop the sandboxes of this plan's cards and of this project's Hermes profiles.
    _guarded(stop, "stopping containers", _stop_containers, stop, hooks, sorted(plan_ids | set(fix_ids)), profiles)


def stop_all(
    board: str, plan: Any, *, conn: sqlite3.Connection, deadline_seconds: float = DEADLINE_SECONDS,
    reason: str = "swarm stop", pause=None, kanban_list=None, kanban_show=None, reclaim=None, killer=None,
    alive=None, command_line=None, list_containers=None, stop_container=None, now=None, sleep=None,
    profiles=None, list_profile_containers=None,
) -> StopReport:
    """ASES-REC-06, section 19.6: "swarm stop MUST stop the whole system within 30 seconds: hermes pause to stop
    new dispatch, reclaim every running card, terminate worker process trees and sandboxes, stop the merge queue
    between steps, and write a stop report. hermes pause alone is not enough, because it never kills work in
    flight." Section 22.13: "With three cards running, swarm stop must leave no worker process, container or
    merge step running after 30 seconds."

    Performs, in this order: the stop flag, hermes pause, listing the plan's running cards (and review cards with
    a live run), reclaiming each, terminating each worker that passes the safety rules in the module docstring,
    stopping the plan's containers. The merge queue is stopped between steps by the flag (stop_requested); the
    architect wires the polling loop and the merge queue to read it. A merge step already in flight finishes
    before the queue looks at the flag again. Nothing else is written.

    Only cards of `plan` (plan_tasks rows for plan.project, plus fix cards whose parent is one of them) are
    touched, so unrelated cards on the same board are left alone. `profiles` names this project's own Hermes
    profiles (project.roles.values()); every running Hermes container of one of them is stopped in step f,
    since that is the only way to find a real worker's sandbox (see _stop_containers). None or empty skips that
    listing. The time box: no outside call gets more than
    a third of `deadline_seconds`, the Hermes calls together get at most two thirds, and a step whose time has run
    out is skipped and recorded, with the flag already set. `now` is the monotonic clock and `sleep` the sleeper;
    both, and every outside effect, are parameters so a test never touches a real process or Docker. Left as
    None, each defaults to the real function, looked up when the stop runs (so monkeypatching the hermes module
    works). Never raises: every failure is an entry in the returned StopReport."""
    clock = now if now is not None else time.monotonic
    hooks = _Hooks(
        pause=pause if pause is not None else hermes_mod.pause,
        kanban_list=kanban_list if kanban_list is not None else hermes_mod.kanban_list,
        kanban_show=kanban_show if kanban_show is not None else hermes_mod.kanban_show,
        reclaim=reclaim if reclaim is not None else hermes_mod.kanban_reclaim,
        killer=killer if killer is not None else terminate_tree,
        alive=alive if alive is not None else pid_alive,
        command_line=command_line if command_line is not None else process_command_line,
        list_containers=list_containers if list_containers is not None else default_list_containers,
        list_profile_containers=(
            list_profile_containers if list_profile_containers is not None else default_list_profile_containers
        ),
        stop_container=stop_container if stop_container is not None else default_stop_container,
    )
    profile_names = sorted({str(profile) for profile in (profiles or []) if profile})
    try:
        limit = max(float(deadline_seconds), 0.0)
    except (TypeError, ValueError):
        limit = float(DEADLINE_SECONDS)
    report = StopReport(started_at=_utc_now())
    stop = _Stop(report, limit, clock, sleep if sleep is not None else time.sleep)
    try:
        _stop_steps(stop, hooks, board, plan, conn, reason, profile_names)
    except Exception as exc:  # noqa: BLE001 - the kill switch reports, it never raises
        stop.note(f"swarm stop hit an unexpected error and may be incomplete: {_err(exc)}")
    elapsed = max(clock() - stop.start, 0.0)
    report.finished_at = _utc_now()
    report.seconds = round(elapsed, 3)
    report.within_deadline = not stop.cut_short and elapsed <= limit
    return report


# ---------------------------------------------------------------------------------------------
# swarm resume
# ---------------------------------------------------------------------------------------------


def _describe_blocked(blocked: list) -> str:
    """A short ASCII summary of what reconcile refused to repair: task key and kind, first five."""
    parts = []
    for item in blocked[:5]:
        if isinstance(item, dict):
            task, kind = item.get("task_key"), item.get("kind")
        else:
            task, kind = getattr(item, "task_key", None), getattr(item, "kind", None)
        parts.append(" ".join(str(word) for word in (task, kind) if word) or str(item))
    if len(blocked) > 5:
        parts.append(f"and {len(blocked) - 5} more")
    return _safe("; ".join(parts))


def resume_all(
    board: str, plan: Any, *, conn: sqlite3.Connection, resume=None, reconcile: Callable[[], Any] | None = None,
) -> dict:
    """Section 19.6: "swarm resume reverses it after reconcile-on-start". Runs `reconcile` first (a zero-argument
    callable the caller supplies: it may return None or an object with a `blocked` list, such as reconcile's
    report). If it raises, or reports anything blocked, nothing is resumed and the stop flag stays set: the state
    is not safe to continue from and a person has to look. Otherwise it calls hermes resume and only then clears
    the stop flag, so a resume that fails leaves the system consistently stopped. Returns {"resumed": True}, or
    {"resumed": False, "reason": ...}; nothing is raised. Passing no `reconcile` skips that check. `board` is not
    used yet: it is here so the two entry points take the same arguments."""
    if reconcile is not None:
        try:
            findings = reconcile()
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            return {"resumed": False, "reason": f"reconcile-on-start failed, the stop flag stays set: {_err(exc)}"}
        blocked = findings.get("blocked") if isinstance(findings, dict) else getattr(findings, "blocked", None)
        try:
            blocked = list(blocked or [])
        except TypeError:
            blocked = [blocked]
        if blocked:
            return {
                "resumed": False,
                "reason": f"reconcile-on-start found {len(blocked)} item(s) that need a person, "
                          f"the stop flag stays set: {_describe_blocked(blocked)}",
            }
    lift = resume if resume is not None else hermes_mod.resume
    try:
        lift()
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        return {"resumed": False, "reason": f"hermes resume failed, the stop flag stays set: {_err(exc)}"}
    try:
        clear_stop(conn, plan.project)
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        return {"resumed": False, "reason": f"hermes resumed but the stop flag could not be cleared: {_err(exc)}"}
    return {"resumed": True}
