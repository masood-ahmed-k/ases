"""An in-memory fake of the Hermes 0.21.3 Kanban board and dispatcher (ASES-TST-01, ASES-TST-02).

Blueprint section 14.4 (ASES-TST-01): "The controller has its own test suite that never touches a real provider ... a
throwaway Hermes board (--board ases-test) and temporary Git repositories provide the rest. Free quota is far too
small to debug a controller against live providers." Section 22.0 adds "a scripted fake worker that performs chosen file
edits", and ASES-TST-02 says the acceptance tests "run against the fake provider and a test board ... so they cost no
quota and are repeatable". This module is the board half of that rig. It is a single-threaded-in-effect, deterministic
simulation of the parts of Hermes that ASES uses, so a test can drive the REAL controller (controller.run_pass, review,
mergeq, recovery, reconcile, killswitch, questions) against it without a Hermes install, a gateway, an LLM or a network.

Why it mirrors Hermes so closely: the modules under test were first written against a simplified picture of Hermes, and
reading the real source showed the picture was wrong in ways no mock could reveal (a block on an already-blocked card
fails AFTER leaving a comment, a second same-kind block goes to triage, a dispatcher give-up writes no `blocked` event,
merge cards are only promoted when their block is not sticky ...). A fake that follows the real rules is what lets an
acceptance test find that kind of defect before a live run does. Every rule below was read from hermes_cli/kanban_db.py,
kanban_db_dispatch.py, kanban_db_workspace.py, kanban_db_graph.py, kanban.py and tools/kanban_tools.py (commit c661785f87,
2026-09-18) and is named in the comment or docstring next to it.

What is modelled: statuses, creation (idempotent by key, parents, todo versus ready, blocked-at-birth), links and
promotion, claims and runs, the circuit breaker (consecutive_failures, max_retries, gave_up), typed blocks and the unblock
loop breaker, review hand-offs and requested changes, schedule, promote, archive, reclaim, comments, the dispatcher (one
board, max_in_progress, a per-profile cap, the review lane, the respawn guard, crash, protocol-violation, timeout and
stale-claim detection), real git worktrees under <primary>/.worktrees/<card id>, and a fake clock. What is not modelled:
processes (a worker is a Python callable run synchronously inside kanban_dispatch, or later from tick() when it sleeps),
goal mode, attachments and artifacts, notify subscriptions, projects.db (a project id is stored as given), multiple
boards, secret redaction inside hand-offs, and the systemic-crash shortcut.

Two Hermes facts worth knowing, both read from the source and both different from what the controller assumed:
  * `hermes pause` is honoured only by the GATEWAY's embedded dispatcher loop (gateway/kanban_watchers.py, through
    _kanban_dispatch_allowed). The CLI `hermes kanban dispatch`, which ASES's hermes.kanban_dispatch runs, ignores it.
    So here kanban_dispatch keeps spawning while paused unless `cli_dispatch_honors_pause` is set, and tick() (the
    gateway) stops entirely while paused. A controller that relies on `pause` to stop its own dispatch is wrong.
  * A card created with initial_status="blocked" (every merge card) gets a `blocked` event with reason "initial_status"
    (kanban_db.create_task), which makes its block sticky (recompute_ready never promotes it) and makes it look like a
    question to any code that reads `blocked` events. docs/work-orders/r2_rules.md says the opposite, from a live probe.
    `initial_block_event=False` reproduces that claim, so either can be tested.

Fake worker processes get PIDs from 2,100,000,000 upwards, far above any real PID, so a real probe or kill can never
hit a real process. Nothing here shells out to hermes: install() also replaces hermes._run with a function that raises.
"""
from __future__ import annotations

import collections
import contextlib
import copy
import dataclasses
import functools
import inspect
import json
import os
import pathlib
import re
import subprocess
import threading
import time

from .. import hermes as _hermes

# ---------------------------------------------------------------------------------------------
# Constants, all read from Hermes 0.21.3 (see the module docstring for the files)
# ---------------------------------------------------------------------------------------------

VALID_STATUSES = ("triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done", "archived")
VALID_BLOCK_KINDS = ("dependency", "needs_input", "capability", "transient")
VALID_INITIAL_STATUSES = ("running", "blocked")
BLOCK_RECURRENCE_LIMIT = 2                      # kanban_db.BLOCK_RECURRENCE_LIMIT
DEFAULT_FAILURE_LIMIT = 2                       # kanban_db_dispatch.DEFAULT_FAILURE_LIMIT ("--max-retries N trips on the Nth failure")
PROTOCOL_VIOLATION_FAILURE_LIMIT = 3            # kanban_db_dispatch._PROTOCOL_VIOLATION_FAILURE_LIMIT
DEFAULT_CLAIM_TTL_SECONDS = 15 * 60             # kanban_db.DEFAULT_CLAIM_TTL_SECONDS
CLAIM_HEARTBEAT_MAX_STALE_SECONDS = 60 * 60     # kanban_db.DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS
DEFAULT_CRASH_GRACE_SECONDS = 30                # kanban_db.DEFAULT_CRASH_GRACE_SECONDS
DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS = 300       # kanban_db_dispatch.DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS
TERMINAL_WORKER_REAP_GRACE_SECONDS = 120        # kanban_db_dispatch.TERMINAL_WORKER_REAP_GRACE_SECONDS
RESPAWN_GUARD_SUCCESS_WINDOW = 3600             # kanban_db_dispatch._RESPAWN_GUARD_SUCCESS_WINDOW
RATE_LIMIT_EXIT_CODE = 75                       # kanban_db.KANBAN_RATE_LIMIT_EXIT_CODE (BSD EX_TEMPFAIL)
FAKE_PID_BASE = 2_100_000_000                   # above every real PID on Windows and Linux

# hermes_cli/kanban_output._TASK_DICT_FIELDS and _SHOW_RUN_FIELDS: exactly what `kanban show --json` prints. Note what
# is NOT in the task dict: consecutive_failures, worker_pid, claim_lock, max_runtime_seconds, block_kind.
TASK_FIELDS = (
    "id", "title", "body", "assignee", "status", "priority", "tenant", "workspace_kind", "workspace_path",
    "branch_name", "project_id", "created_by", "created_at", "started_at", "completed_at", "result", "skills",
    "max_retries", "model_override", "provider_override", "session_id", "workflow_template_id",
    "current_step_key", "completion_contract", "last_failure_error",
)
RUN_FIELDS = (
    "id", "profile", "step_key", "status", "outcome", "summary", "error", "metadata", "worker_pid", "started_at",
    "ended_at",
)

# kanban_db_dispatch._RESPAWN_BLOCKER_RE: a last failure that reads like a quota or auth wall is never respawned.
_RESPAWN_BLOCKER_RE = re.compile(
    r"\b(quota|rate[\s_\-]?limit|429|403|auth\w*|unauthorized|forbidden|billing|subscription|"
    r"access[\s_]denied|permission[\s_]denied|invalid[\s_]api[\s_]key)\b",
    re.IGNORECASE,
)

# kanban_db_dispatch._PROTOCOL_VIOLATION_ERROR with the em dash written as a hyphen (ASCII only in this repo).
_PROTOCOL_VIOLATION_ERROR = (
    "worker exited cleanly (rc=0) without calling kanban_complete or kanban_block - protocol violation. "
    "If the prior run already did the work, verify it and report the result via kanban_complete; a run that ends "
    "without a terminal kanban call counts as failed no matter what it did."
)

_REVIEW_APPROVED_NOTE = "Review approved without additional evidence."   # kanban_db._REVIEW_APPROVED_NOTE

_UNSET = object()


class AgentToolError(Exception):
    """A worker's kanban tool call that Hermes would have refused (the tool returns a structured error to the model).
    Raised by the agent_* methods so a scripted worker that does something illegal fails its test loudly instead of
    being silently ignored."""


class _Refused(Exception):
    """The `hermes kanban` CLI would have printed `output` to stderr and exited with `code`. Turned into a
    hermes.HermesCommandError by FakeHermes._cli, which is exactly what hermes._kanban raises for a non-zero exit."""

    def __init__(self, output: str, code: int = 1) -> None:
        super().__init__(output)
        self.output = output if output.endswith("\n") else output + "\n"
        self.code = code


class _LiveClaim(Exception):
    """kanban_db.LiveClaimError: the card is running under a live worker and the caller neither owns the run nor forced."""


class _WorkerFailed(Exception):
    """A scripted worker raised. Inside the fake it travels wrapped, so that the CLI-style error conversion in
    FakeHermes._cli (which turns a ValueError or RuntimeError into a HermesCommandError, as kanban_command does for the
    database layer) never swallows a bug in a test's worker script; the public API unwraps it and re-raises the original."""

    def __init__(self, original: Exception) -> None:
        super().__init__(repr(original))
        self.original = original


Call = collections.namedtuple("Call", "name args kwargs")
Call.__doc__ = "One controller-side call, as made: the hermes function name, its positional and its keyword arguments."


def _ascii(text) -> str:
    """`text` as ASCII (backslash escapes), for anything a person reads in a terminal."""
    return str(text).encode("ascii", "backslashreplace").decode("ascii")


def _first_line(text, limit: int) -> str:
    """kanban_db._first_line: the first line of `text`, stripped, capped at `limit` characters ("" when empty)."""
    lines = (text or "").strip().splitlines()
    return lines[0][:limit] if lines else ""


def _jsonify(obj):
    """kanban_db._json_or_null and back: a falsy payload is stored as NULL (None here), anything else round-trips
    through JSON, so a tuple becomes a list, a key becomes a string and a value that is not JSON raises TypeError."""
    return json.loads(json.dumps(obj, ensure_ascii=False)) if obj else None


def _canonical(assignee):
    """kanban_db._canonical_assignee: profile names are lower case, `default` matches case-insensitively, blank is refused."""
    if assignee is None:
        return None
    stripped = str(assignee).strip()
    if not stripped:
        raise ValueError("profile name cannot be empty")
    return "default" if stripped.casefold() == "default" else stripped.lower()


def _parse_duration(value):
    """kanban._parse_duration: `30s`, `5m`, `2h`, `1d` or a bare number of seconds; None for empty input."""
    if value is None or value == "":
        return None
    text = str(value).strip().lower()
    try:
        return int(text)
    except ValueError:
        pass
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if not (text and text[-1] in units):
        raise ValueError(f"malformed duration {value!r} (expected 30s, 5m, 2h, 1d, or a number)")
    try:
        number = float(text[:-1])
    except ValueError as exc:
        raise ValueError(f"malformed duration {value!r}") from exc
    return int(number * units[text[-1]])


def _parse_workspace(value):
    """kanban._parse_workspace_flag: `scratch`, `worktree`, `worktree:<path>`, `dir:<path>` -> (kind, path or None)."""
    if not value:
        return None, None
    text = value.strip()
    if text in ("scratch", "worktree"):
        return text, None
    for prefix, kind in (("dir:", "dir"), ("worktree:", "worktree")):
        if text.startswith(prefix):
            path = text[len(prefix):].strip()
            if not path:
                raise _Refused(f"kanban: --workspace {prefix} requires a path after the colon", 2)
            return kind, os.path.expanduser(path)
    raise _Refused(
        f"kanban: unknown --workspace value {value!r}: use scratch, worktree, worktree:<path>, or dir:<path>", 2)


def _parse_branch(value):
    """kanban._parse_branch_flag: a non-empty name without whitespace that does not start with a dash."""
    if value is None:
        return None
    branch = value.strip()
    if not branch:
        raise _Refused("kanban: --branch requires a non-empty name", 2)
    if branch.startswith("-"):
        raise _Refused("kanban: --branch must not start with '-'", 2)
    if any(ch.isspace() for ch in branch):
        raise _Refused("kanban: --branch must not contain whitespace", 2)
    return branch


def _git(cwd, *args, timeout: int = 60):
    """`git -C cwd args`, never raising on a non-zero exit (the caller reads returncode)."""
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=timeout,
        encoding="utf-8", errors="replace",
    )


# ---------------------------------------------------------------------------------------------
# Internal records
# ---------------------------------------------------------------------------------------------


@dataclasses.dataclass
class _Task:
    id: str
    seq: int
    title: str
    body: str | None
    assignee: str | None
    status: str
    created_by: str | None
    created_at: int
    workspace_kind: str = "scratch"
    workspace_path: str | None = None
    branch_name: str | None = None
    project_id: str | None = None
    tenant: str | None = None
    priority: int = 0
    started_at: int | None = None
    completed_at: int | None = None
    result: str | None = None
    skills: list | None = None
    max_retries: int | None = None
    model_override: str | None = None
    provider_override: str | None = None
    session_id: str | None = None
    workflow_template_id: str | None = None
    current_step_key: str | None = None
    completion_contract: str | None = None
    last_failure_error: str | None = None
    # not in `kanban show --json`, but real columns that drive behaviour
    idempotency_key: str | None = None
    consecutive_failures: int = 0
    worker_pid: int | None = None
    max_runtime_seconds: int | None = None
    last_heartbeat_at: int | None = None
    current_run_id: int | None = None
    claim_lock: str | None = None
    claim_expires: int | None = None
    block_kind: str | None = None
    block_recurrences: int = 0


@dataclasses.dataclass
class _Run:
    id: int
    task_id: str
    profile: str | None
    step_key: str | None
    status: str
    started_at: int
    claim_lock: str | None = None
    claim_expires: int | None = None
    worker_pid: int | None = None
    max_runtime_seconds: int | None = None
    last_heartbeat_at: int | None = None
    ended_at: int | None = None
    outcome: str | None = None
    summary: str | None = None
    metadata: dict | None = None
    error: str | None = None


@dataclasses.dataclass
class _Event:
    id: int
    task_id: str
    run_id: int | None
    kind: str
    payload: dict | None
    created_at: int


@dataclasses.dataclass
class _Comment:
    id: int
    task_id: str
    author: str
    body: str
    created_at: int


@dataclasses.dataclass
class _Proc:
    """A fake worker process. `alive` is what the dispatcher's PID probe would say; `hung` keeps it alive after its
    function returned (a worker stuck in a call that never ends); an exit_kind of clean_exit, nonzero_exit, signaled
    or rate_limited is what the reaped exit status would classify as."""
    run_id: int
    task_id: str
    pid: int
    alive: bool = True
    hung: bool = False
    exit_kind: str | None = None
    exit_code: int | None = None


@dataclasses.dataclass
class _Deferred:
    at: int
    seq: int
    task_id: str
    run_id: int
    fn: object


@dataclasses.dataclass
class _Armed:
    name: str
    card_id: str | None
    error: object
    times: int


@dataclasses.dataclass
class _SpawnFailure:
    profile: str | None
    card_id: str | None
    error: str
    times: int


def _is_factory(obj) -> bool:
    """A worker factory takes the card and returns a worker; a worker takes (fake, card, run, workspace_path). The
    `is_factory` attribute settles it when present, else a callable with exactly one positional parameter is a factory."""
    marker = getattr(obj, "is_factory", None)
    if marker is not None:
        return bool(marker)
    try:
        params = [
            p for p in inspect.signature(obj).parameters.values()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) and p.default is p.empty
        ]
    except (TypeError, ValueError):
        return False
    return len(params) == 1


def _controller_call(fn):
    """Mark `fn` as one of the functions the controller calls on the hermes module: log it in `calls`, fire an armed
    failure (fail_next) BEFORE any effect, refuse a board other than the fake's, and serialise on the fake's lock."""
    signature = inspect.signature(fn)
    has_board = "board" in signature.parameters

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            self.calls.append(Call(fn.__name__, args, dict(kwargs)))
            bound = signature.bind(self, *args, **kwargs)
            self._maybe_fail(fn.__name__, bound.arguments)
            if has_board:
                self._check_board(bound.arguments["board"], fn.__name__)
            try:
                return fn(self, *args, **kwargs)
            except _WorkerFailed as failed:
                raise failed.original from None

    return wrapper


def _agent_call(fn):
    """Mark `fn` as a worker-side kanban tool call: serialised on the fake's lock, not logged in `calls` (that log is
    the controller's), and a refusal from the state machine surfaces as AgentToolError."""

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            try:
                return fn(self, *args, **kwargs)
            except _Refused as exc:
                raise AgentToolError(exc.output.strip()) from None
            except _LiveClaim as exc:
                raise AgentToolError(str(exc)) from None

    return wrapper


class FakeHermes:
    """One fake Hermes board, its dispatcher and a set of fake worker profiles.

        fake = FakeHermes(repo, board="ases-test", integration_branch="integration").install(monkeypatch)
        fake.register_worker("coder-1", worker)      # see ases.fakes.worker
        card = hermes.kanban_create("ases-test", "T1: scaffold", assignee="coder-1", workspace="worktree",
                                    branch="swarm/T1-coder")
        hermes.kanban_dispatch("ases-test")           # claims the card, cuts a real worktree, runs the worker
        fake.card(card["id"])["status"]               # -> "review"

    `install` replaces every public function of the hermes module with the fake's, so the real controller, review,
    mergeq, recovery, reconcile, killswitch and questions modules run against this board unchanged. The controller-side
    methods (the ones with the same names and signatures as hermes.py) are logged in `calls`. The `agent_*` methods are
    what a worker's kanban tools do; a scripted worker (ases.fakes.worker) calls them.

    Time is `now` (epoch seconds), and it only moves when a test calls tick() (or run_until in the acceptance rig does).
    Every event, comment and run timestamp uses it, so a whole scenario is deterministic and instant. Ids look like
    Hermes's (t_ and eight hex digits) but count up from a fixed start, so two runs of a test see the same ids.

    Settings a test may change (all plain attributes):
      max_in_progress (3, kanban.max_in_progress) and max_in_progress_per_profile (1, kanban.max_in_progress_per_profile),
      claim_ttl_seconds, crash_grace_seconds, rate_limit_cooldown_seconds, failure_limit, review_dispatch,
      paused and cli_dispatch_honors_pause, gateway_dispatch (tick() also spawns, like the gateway's own dispatcher),
      initial_block_event, default_author ("default", what a controller-side comment without --author is signed with),
      version, gateway_running, doctor, default_session_requests.
    """

    def __init__(
        self, repo: pathlib.Path | str | None = None, *, board: str = "ases-test",
        integration_branch: str = "integration", now: int | None = None,
        scratch_root: pathlib.Path | str | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self.repo = pathlib.Path(repo) if repo is not None else None
        self.board = board
        self.integration_branch = integration_branch
        self.scratch_root = pathlib.Path(scratch_root) if scratch_root is not None else None
        # Starts at the real time so card timestamps look current next to ASES's own; only tick() moves it.
        self.now = int(time.time()) if now is None else int(now)

        self.max_in_progress: int | None = 3
        self.max_in_progress_per_profile: int | None = 1
        self.claim_ttl_seconds = DEFAULT_CLAIM_TTL_SECONDS
        self.crash_grace_seconds = DEFAULT_CRASH_GRACE_SECONDS
        self.rate_limit_cooldown_seconds = DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS
        self.failure_limit = DEFAULT_FAILURE_LIMIT
        self.review_dispatch = True
        self.paused = False
        self.pause_reason: str | None = None
        self.cli_dispatch_honors_pause = False
        self.gateway_dispatch = False
        self.initial_block_event = True
        self.default_author = "default"
        self.hermes_path_value: str | None = None
        self.version: str | None = "0.21.3"
        self.gateway_running = True
        self.gateway_output = "Gateway is running (FakeHermes)"
        self.doctor = _hermes.DoctorResult(True, 0, "fake hermes doctor: every check passed", (), ())
        self.default_session_requests = 1
        self.extra_profiles: set[str] = set()
        self.calls: list[Call] = []

        self._tasks: dict[str, _Task] = {}
        self._links: list[tuple[str, str]] = []
        self._events: dict[str, list[_Event]] = {}
        self._comments: dict[str, list[_Comment]] = {}
        self._runs: dict[str, list[_Run]] = {}
        self._run_by_id: dict[int, _Run] = {}
        self._workers: dict[str, object] = {}
        self._procs: dict[int, _Proc] = {}
        self._deferred: list[_Deferred] = []
        self._armed: list[_Armed] = []
        self._spawn_failures: list[_SpawnFailure] = []
        self._session_usage: dict[str, dict | None] = {}
        self._issued_sessions: set[str] = set()
        self._task_seq = 0
        self._run_seq = 0
        self._event_seq = 0
        self._comment_seq = 0
        self._pid_seq = 0
        self._claim_seq = 0
        self._deferred_seq = 0

    # ---------------------------------------------------------------------------------------
    # Installing the fake, and the inspection helpers a test asserts with
    # ---------------------------------------------------------------------------------------

    def install(self, monkeypatch) -> "FakeHermes":
        """Replace every public function of the hermes module with this fake's (pytest's monkeypatch undoes it), and
        make hermes._run raise, so nothing in the process can start a real `hermes`. A public function the fake does not
        have is an AttributeError at once: a wrapper added to hermes.py later must be added here before any test runs."""
        for name, function in inspect.getmembers(_hermes, inspect.isfunction):
            if name.startswith("_") or function.__module__ != _hermes.__name__:
                continue
            replacement = getattr(self, name, None)
            if replacement is None:
                raise AttributeError(f"FakeHermes has no {name}(): add it (same signature as hermes.{name}) to ases.fakes.board")
            monkeypatch.setattr(_hermes, name, replacement)

        def forbidden(*args, **kwargs):
            raise AssertionError("hermes._run called while FakeHermes is installed: nothing may start a real hermes")

        monkeypatch.setattr(_hermes, "_run", forbidden)
        return self

    def card(self, card_id: str) -> dict:
        """The card exactly as hermes.kanban_show returns it (flat task dict plus _children, _parents, _runs, _events,
        _comments, _latest_summary), as a copy. KeyError for an unknown id."""
        with self._lock:
            if card_id not in self._tasks:
                raise KeyError(card_id)
            return self._show(card_id)

    def cards(self, status: str | None = None) -> list[dict]:
        """Every card, archived ones too, in creation order, as flat task dicts (what kanban_list returns per card)."""
        with self._lock:
            return [
                self._task_dict(t) for t in self._tasks.values() if status is None or t.status == status
            ]

    def events(self, card_id: str, kind: str | None = None) -> list[dict]:
        """The card's events ({kind, payload, created_at, run_id}), oldest first, optionally only one kind."""
        with self._lock:
            return [
                self._event_dict(e) for e in self._events[card_id] if kind is None or e.kind == kind
            ]

    def comments(self, card_id: str) -> list[dict]:
        """The card's comments ({author, body, created_at}), oldest first."""
        with self._lock:
            return [self._comment_dict(c) for c in self._comments[card_id]]

    def runs(self, card_id: str) -> list[dict]:
        """The card's runs, oldest first, in the shape kanban_show gives them."""
        with self._lock:
            return [self._run_dict(r) for r in self._runs[card_id]]

    def worktree(self, card_id: str) -> pathlib.Path | None:
        """The card's worktree (or scratch directory) once it has been dispatched, else None."""
        with self._lock:
            path = self._tasks[card_id].workspace_path
            return pathlib.Path(path) if path else None

    def live_workers(self) -> list[dict]:
        """Every fake worker process that is alive: {card_id, run_id, pid, orphan}. `orphan` is True when the card no
        longer runs that run (it was blocked, completed, reclaimed or archived underneath a worker that lived on), which
        is what an "no orphan workers" assertion (acceptance 22.7, 22.13) looks for."""
        with self._lock:
            live = []
            for proc in self._procs.values():
                if not proc.alive:
                    continue
                task = self._tasks[proc.task_id]
                live.append({
                    "card_id": proc.task_id, "run_id": proc.run_id, "pid": proc.pid,
                    "orphan": not (task.status == "running" and task.current_run_id == proc.run_id),
                })
            return live

    @staticmethod
    def session_id_for(card_id: str, run_id: int) -> str:
        """The worker_session_id a worker's kanban tool stamps into its run metadata (usage.py reads it)."""
        return f"sess_{card_id}_{run_id}"

    def snapshot(self) -> dict:
        """The whole board as plain data, deep-copied, so `fake.snapshot() == before` proves a call changed nothing (an
        idempotent card creation, a repeated show). Includes the columns Hermes keeps but does not show (failure counter,
        claim lock, block kind), the worker processes and the clock; excludes `calls`."""
        with self._lock:
            return copy.deepcopy({
                "now": self.now, "paused": self.paused, "pause_reason": self.pause_reason,
                "tasks": {tid: dataclasses.asdict(t) for tid, t in self._tasks.items()},
                "links": list(self._links),
                "events": {tid: [dataclasses.asdict(e) for e in evs] for tid, evs in self._events.items()},
                "comments": {tid: [dataclasses.asdict(c) for c in cs] for tid, cs in self._comments.items()},
                "runs": {tid: [dataclasses.asdict(r) for r in rs] for tid, rs in self._runs.items()},
                "procs": {rid: dataclasses.asdict(p) for rid, p in self._procs.items()},
                "deferred": [(d.at, d.task_id, d.run_id) for d in self._deferred],
                "sessions": sorted(self._issued_sessions),
                "counters": [
                    self._task_seq, self._run_seq, self._event_seq, self._comment_seq, self._pid_seq,
                    self._claim_seq,
                ],
            })

    def describe(self) -> str:
        """A short ASCII table of the board, for the failure message of a scenario that did not converge."""
        with self._lock:
            lines = [f"FakeHermes board={self.board} now={self.now} paused={self.paused}"]
            for t in self._tasks.values():
                lines.append(
                    f"  {t.id} {t.status:9s} {(t.assignee or '-'):10s} failures={t.consecutive_failures} "
                    f"runs={len(self._runs[t.id])} {_ascii(t.title)}"
                )
            return "\n".join(lines)

    # ---------------------------------------------------------------------------------------
    # Failure injection, the clock, and the worker registry
    # ---------------------------------------------------------------------------------------

    def fail_next(
        self, name: str, *, card_id: str | None = None, error: Exception | None = None, times: int = 1,
    ) -> None:
        """Make the next `times` calls of hermes function `name` raise `error` (default a HermesCommandError) BEFORE they
        change anything. With `card_id`, only a call that names that card fails; the others pass through."""
        function = getattr(_hermes, name, None)
        if not (inspect.isfunction(function) and not name.startswith("_")):
            raise ValueError(f"{name!r} is not a public function of the hermes module")
        with self._lock:
            self._armed.append(_Armed(name, card_id, error, int(times)))

    def fail_spawn(
        self, *, profile: str | None = None, card_id: str | None = None,
        error: str = "spawn failed: fake spawn failure", times: int = 1,
    ) -> None:
        """Make the next `times` dispatches of a matching card (any card when both filters are None) fail at spawn, after
        the claim and the workspace: Hermes books a spawn_failed run and counts a failure (the circuit breaker)."""
        with self._lock:
            self._spawn_failures.append(
                _SpawnFailure(_canonical(profile) if profile else None, card_id, error, int(times)))

    def tick(self, seconds: int = 1) -> None:
        """Advance the clock by `seconds`, running everything the gateway's dispatcher would have done meanwhile: sleeping
        workers wake in time order (a worker that would run past its max_runtime is left to be timed out), then the
        reclaim phase runs (stale claims, dead workers, timeouts, promotion of cards whose parents are done), and, with
        `gateway_dispatch`, one spawn phase. While `paused` the gateway does nothing but the clock moves."""
        with self._lock:
            try:
                self._advance_to(self.now + int(seconds))
            except _WorkerFailed as failed:
                raise failed.original from None
            if self.paused:
                return
            result = self._empty_dispatch_result()
            self._reclaim_phase(result)
            if self.gateway_dispatch:
                self._spawn_phase(result, dry_run=False, max_spawn=None)

    def register_worker(self, profile: str, worker) -> None:
        """Say what a profile's workers do. `worker` is a callable (fake, card, run, workspace_path) -> None, or a
        factory taking the card and returning such a callable (ases.fakes.worker.by_task_key builds one). Registering a
        profile is also what makes it exist: a card assigned to any other profile is skipped as non-spawnable."""
        with self._lock:
            self._workers[_canonical(profile)] = worker

    def defer(self, card_id: str, run_id: int, seconds: int, fn) -> None:
        """Run `fn()` when the clock reaches now + seconds, if the run is still the card's live run by then. This is how
        a worker that sleeps (a slow coder) keeps its card `running` across controller passes and finishes later."""
        with self._lock:
            self._deferred_seq += 1
            self._deferred.append(_Deferred(self.now + int(seconds), self._deferred_seq, card_id, run_id, fn))

    def kill_worker(self, card_id: str, *, exit_code: int | None = 137, signal: int | None = None) -> None:
        """Kill the card's worker process from outside (SIGKILL, an out-of-memory kill, the host rebooting). The card stays
        `running` with a dead PID until the next reclaim phase (a dispatch or a tick) books a crash, exactly as Hermes does.
        `signal` makes it "killed by signal N", and exit code 75 (EX_TEMPFAIL) is the quota wall a rate-limited worker
        leaves with: booked as a `rate_limited` run and never counted as a failure."""
        with self._lock:
            task = self._tasks[card_id]
            proc = self._procs.get(task.current_run_id) if task.current_run_id else None
            if proc is None:
                raise KeyError(f"{card_id} has no worker process to kill")
            proc.alive = False
            proc.hung = False
            self._drop_deferred(proc.run_id)
            if signal is not None:
                proc.exit_kind, proc.exit_code = "signaled", int(signal)
            elif exit_code == RATE_LIMIT_EXIT_CODE:
                proc.exit_kind, proc.exit_code = "rate_limited", RATE_LIMIT_EXIT_CODE  # EX_TEMPFAIL: a provider quota wall
            else:
                proc.exit_kind, proc.exit_code = "nonzero_exit", int(exit_code if exit_code is not None else 1)

    def set_session_usage(
        self, session_id: str, *, api_call_count: int = 1, input_tokens: int = 1000, output_tokens: int = 200,
        model: str = "",
    ) -> None:
        """What `hermes sessions export` reports for one worker session (session_usage returns it)."""
        with self._lock:
            self._session_usage[session_id] = {
                "id": session_id, "model": model, "api_call_count": api_call_count,
                "input_tokens": input_tokens, "output_tokens": output_tokens,
            }

    def set_session_usage_unknown(self, session_id: str) -> None:
        """Make session_usage return None for this session (the export failed): usage.py must record nothing for it."""
        with self._lock:
            self._session_usage[session_id] = None

    # ---------------------------------------------------------------------------------------
    # Plumbing: the board check, armed failures, CLI-style refusals
    # ---------------------------------------------------------------------------------------

    def _check_board(self, board: str, name: str) -> None:
        """hermes_cli.kanban.kanban_command: a board that does not exist is an error. Stricter than Hermes for the
        `default` board, which always exists there: a controller that quietly used it (a config left at its placeholder,
        as happened once for real) would put cards where nobody looks, so here it fails loudly."""
        if board != self.board:
            raise _hermes.HermesCommandError(
                ["kanban", "--board", str(board), name.removeprefix("kanban_")], 1,
                f"kanban: board {board!r} does not exist. Create it with `hermes kanban boards create {board}`.\n"
                f"(FakeHermes has only the board {self.board!r}; real Hermes would have quietly used the default board.)\n",
            )

    def _maybe_fail(self, name: str, arguments: dict) -> None:
        for armed in list(self._armed):
            if armed.name != name:
                continue
            if armed.card_id is not None:
                named = {arguments.get(key) for key in ("card_id", "parent_id", "child_id")}
                if armed.card_id not in named and armed.card_id not in (arguments.get("card_ids") or []):
                    continue
            armed.times -= 1
            if armed.times <= 0:
                self._armed.remove(armed)
            if armed.error is not None:
                raise armed.error
            raise _hermes.HermesCommandError(
                ["kanban", "--board", self.board, name.removeprefix("kanban_")], 1,
                "injected failure (FakeHermes.fail_next)\n")

    @contextlib.contextmanager
    def _cli(self, board: str, *argv: str):
        """Run the body as the `hermes kanban <argv>` command would: a _Refused is the command's own stderr and exit code,
        and a ValueError, RuntimeError or PermissionError is kanban_command's catch-all ("kanban: <message>", exit 1). Both
        surface as the HermesCommandError that hermes._kanban raises for a non-zero exit."""
        try:
            yield
        except _Refused as exc:
            raise _hermes.HermesCommandError(["kanban", "--board", board, *argv], exc.code, exc.output) from None
        except (ValueError, RuntimeError, PermissionError) as exc:
            raise _hermes.HermesCommandError(["kanban", "--board", board, *argv], 1, f"kanban: {exc}\n") from None

    # ---------------------------------------------------------------------------------------
    # Views: what `kanban show --json` and `kanban list --json` print
    # ---------------------------------------------------------------------------------------

    def _task_dict(self, task: _Task) -> dict:
        view = {name: getattr(task, name) for name in TASK_FIELDS}
        view["skills"] = list(task.skills) if task.skills else []
        return copy.deepcopy(view)

    def _run_dict(self, run: _Run) -> dict:
        return copy.deepcopy({name: getattr(run, name) for name in RUN_FIELDS})

    @staticmethod
    def _event_dict(event: _Event) -> dict:
        return {
            "kind": event.kind, "payload": copy.deepcopy(event.payload), "created_at": event.created_at,
            "run_id": event.run_id,
        }

    @staticmethod
    def _comment_dict(comment: _Comment) -> dict:
        return {"author": comment.author, "body": comment.body, "created_at": comment.created_at}

    def _show(self, card_id: str) -> dict:
        """hermes.kanban_show's result: the flat task dict plus _children, _parents (ids, sorted), _runs, _events,
        _comments and _latest_summary. Reading a card never promotes anything (only list and dispatch do)."""
        view = self._task_dict(self._tasks[card_id])
        view["_children"] = sorted(self._children_of(card_id))
        view["_parents"] = sorted(self._parents_of(card_id))
        view["_runs"] = [self._run_dict(r) for r in self._runs[card_id]]
        view["_events"] = [self._event_dict(e) for e in self._events[card_id]]
        view["_comments"] = [self._comment_dict(c) for c in self._comments[card_id]]
        view["_latest_summary"] = self._latest_summary(card_id)
        return view

    def _latest_summary(self, card_id: str) -> str | None:
        """kanban_db.latest_summary: the newest non-empty run summary (by end time, else start time, then id)."""
        best = None
        for run in self._runs[card_id]:
            if run.summary:
                key = (run.ended_at if run.ended_at is not None else run.started_at, run.id)
                if best is None or key >= best[0]:
                    best = (key, run.summary)
        return best[1] if best else None

    # ---------------------------------------------------------------------------------------
    # Records: events, comments, runs (kanban_db._append_event, add_comment, _end_run, ...)
    # ---------------------------------------------------------------------------------------

    def _event(self, task_id: str, kind: str, payload: dict | None = None, run_id: int | None = None) -> None:
        self._event_seq += 1
        self._events[task_id].append(_Event(self._event_seq, task_id, run_id, kind, _jsonify(payload), self.now))

    def _latest_event(self, task_id: str, kind: str, run_id: int | None = None) -> _Event | None:
        for event in reversed(self._events[task_id]):
            if event.kind == kind and (run_id is None or event.run_id == run_id):
                return event
        return None

    def _add_comment(self, task_id: str, author: str, body: str) -> None:
        """kanban_db.add_comment: the body and author are stripped and must not be blank, an unknown card is a
        ValueError, and every comment also writes a `commented` event carrying the author and the raw length."""
        if not body or not body.strip():
            raise ValueError("comment body is required")
        if not author or not author.strip():
            raise ValueError("comment author is required")
        if task_id not in self._tasks:
            raise ValueError(f"unknown task {task_id}")
        self._comment_seq += 1
        self._comments[task_id].append(_Comment(self._comment_seq, task_id, author.strip(), body.strip(), self.now))
        self._event(task_id, "commented", {"author": author, "len": len(body)})

    def _new_run(self, task: _Task, lock: str, expires: int) -> _Run:
        self._run_seq += 1
        run = _Run(
            id=self._run_seq, task_id=task.id, profile=task.assignee, step_key=task.current_step_key,
            status="running", started_at=self.now, claim_lock=lock, claim_expires=expires,
            max_runtime_seconds=task.max_runtime_seconds,
        )
        self._runs[task.id].append(run)
        self._run_by_id[run.id] = run
        task.current_run_id = run.id
        return run

    def _end_run(
        self, task: _Task, *, outcome: str, status: str | None = None, summary: str | None = None,
        error: str | None = None, metadata: dict | None = None,
    ) -> int | None:
        """kanban_db._end_run: close the active run (status defaults to the outcome) and clear current_run_id; None when no
        run was active. worker_pid and claim_lock stay on the closed run, as Hermes keeps them as evidence."""
        run_id = task.current_run_id
        if run_id is None:
            return None
        run = self._run_by_id[run_id]
        if run.ended_at is None:
            run.status = status or outcome
            run.outcome = outcome
            run.summary = summary
            run.error = error
            run.metadata = _jsonify(metadata)
            run.ended_at = self.now
            run.claim_expires = None
        task.current_run_id = None
        return run_id

    def _synthesize_run(
        self, task: _Task, *, outcome: str, summary: str | None = None, error: str | None = None,
        metadata: dict | None = None, profile=_UNSET,
    ) -> int:
        """kanban_db._synthesize_ended_run: a zero-duration closed run for a terminal transition on a card that was never
        claimed (a merge card, a card completed by hand), so the hand-off fields survive. Its profile is the card's
        assignee unless the caller names the actor, so a merge card's synthesized run has profile None."""
        self._run_seq += 1
        run = _Run(
            id=self._run_seq, task_id=task.id, profile=task.assignee if profile is _UNSET else profile,
            step_key=task.current_step_key, status=outcome, started_at=self.now, ended_at=self.now, outcome=outcome,
            summary=summary, error=error, metadata=_jsonify(metadata),
        )
        self._runs[task.id].append(run)
        self._run_by_id[run.id] = run
        return run.id

    def _end_or_synthesize(
        self, task: _Task, *, outcome: str, status: str, summary: str | None = None, metadata: dict | None = None,
        synthesize: bool, profile=_UNSET,
    ) -> int | None:
        run_id = self._end_run(task, outcome=outcome, status=status, summary=summary, metadata=metadata)
        if run_id is None and synthesize:
            run_id = self._synthesize_run(task, outcome=outcome, summary=summary, metadata=metadata, profile=profile)
        return run_id

    # ---------------------------------------------------------------------------------------
    # The graph: links, dependency gating, promotion (kanban_db.link_tasks, recompute_ready, ...)
    # ---------------------------------------------------------------------------------------

    def _parents_of(self, card_id: str) -> list[str]:
        return [p for p, c in self._links if c == card_id]

    def _children_of(self, card_id: str) -> list[str]:
        return [c for p, c in self._links if p == card_id]

    def _parents_satisfied(self, card_id: str) -> bool:
        """kanban_db._parents_satisfied: every parent is done or archived (an archived parent counts as satisfied)."""
        return all(self._tasks[p].status in ("done", "archived") for p in self._parents_of(card_id))

    def _would_cycle(self, parent_id: str, child_id: str) -> bool:
        seen: set[str] = set()
        stack = [child_id]
        while stack:
            node = stack.pop()
            if node == parent_id:
                return True
            if node in seen:
                continue
            seen.add(node)
            stack.extend(self._children_of(node))
        return False

    def _link_tasks(self, parent_id: str, child_id: str) -> bool:
        """kanban_db.link_tasks. A `ready` child whose new parent is not yet done or archived is demoted to `todo` with a
        dependency_wait event (a blocked child, such as a merge card, is left alone), and a `linked` event is written on
        the child every time, even when the link already existed. Returns whether the child was demoted."""
        if parent_id == child_id:
            raise ValueError("a task cannot depend on itself")
        missing = [i for i in (parent_id, child_id) if i not in self._tasks]
        if missing:
            raise ValueError(f"unknown task(s): {', '.join(missing)}")
        if self._would_cycle(parent_id, child_id):
            raise ValueError(f"linking {parent_id} -> {child_id} would create a cycle")
        if (parent_id, child_id) not in self._links:
            self._links.append((parent_id, child_id))
        gated = False
        if self._tasks[parent_id].status not in ("done", "archived"):
            child = self._tasks[child_id]
            if child.status == "ready":
                child.status = "todo"
                gated = True
                self._event(child_id, "dependency_wait",
                            {"reason": "parent_not_done", "demoted": True, "parent": parent_id})
        self._event(child_id, "linked", {"parent": parent_id, "child": child_id})
        return gated

    def _has_sticky_block(self, card_id: str) -> bool:
        """kanban_db._has_sticky_block: the newest `blocked` or `unblocked` event is `blocked`, an explicit block that only
        an unblock may leave. A gave_up trip writes neither, so it is not sticky (the failure limit holds it instead)."""
        for event in reversed(self._events[card_id]):
            if event.kind in ("blocked", "unblocked"):
                return event.kind == "blocked"
        return False

    def _resume_status_from_events(self, card_id: str) -> str:
        """kanban_db._resume_status_from_events: `review` when the newest lifecycle event says the card left from review."""
        kinds = (
            "blocked", "block_loop_detected", "dependency_wait", "gave_up", "unblocked", "changes_requested",
            "review_reopened", "status", "reclaimed", "stale", "timed_out", "crashed", "spawn_failed", "rate_limited",
        )
        for event in reversed(self._events[card_id]):
            if event.kind in kinds:
                payload = event.payload or {}
                for key in ("resume_status", "retry_status", "source_status"):
                    if payload.get(key) == "review":
                        return "review"
                return "ready"
        return "ready"

    def _retry_status_for_run(self, card_id: str, run_id: int | None = None) -> str:
        """kanban_db._retry_status_for_run: `review` when the run was claimed from review, else `ready`, so a crash,
        timeout or reclaim can never turn a reviewer run into an implementation run."""
        if run_id is None:
            run_id = self._tasks[card_id].current_run_id
        if run_id is None:
            return "ready"
        event = self._latest_event(card_id, "claimed", run_id)
        payload = (event.payload if event else None) or {}
        return "review" if payload.get("source_status") == "review" else "ready"

    def _recompute_ready(self, failure_limit: int | None = None) -> int:
        """kanban_db.recompute_ready: promote `todo` and `blocked` cards whose parents are all done or archived. A blocked
        card is skipped when its block is sticky, or when its failure counter reached its limit (else the breaker could
        never trip). The promoted card lands where it left from (`review` or `ready`) and gets a `promoted` event."""
        limit = DEFAULT_FAILURE_LIMIT if failure_limit is None else failure_limit
        promoted = 0
        for task in list(self._tasks.values()):
            if task.status not in ("todo", "blocked"):
                continue
            if task.status == "blocked" and self._has_sticky_block(task.id):
                continue
            if not self._parents_satisfied(task.id):
                continue
            resume = self._resume_status_from_events(task.id)
            if task.status == "blocked":
                effective = int(task.max_retries) if task.max_retries is not None else int(limit)
                if task.consecutive_failures >= effective:
                    continue
            task.status = resume
            self._event(task.id, "promoted", {"status": resume} if resume != "ready" else None)
            promoted += 1
        return promoted

    # ---------------------------------------------------------------------------------------
    # Transitions (kanban_db.block_task, request_review, complete_task, ...)
    # ---------------------------------------------------------------------------------------

    @staticmethod
    def _route_block(kind, reason, source_status, *, prev_kind, prev_recurrences):
        """kanban_db._route_block: `dependency` waits in `todo`; any other kind (None, a generic block, included) counts
        unblock-loop recurrences (the stored kind equal to the incoming one means blocked, unblocked, blocked again for the
        same cause) and at BLOCK_RECURRENCE_LIMIT the card goes to `triage` with a block_loop_detected event instead of
        `blocked`. Returns (new status, event kind, event payload)."""
        payload = {"reason": reason, "kind": kind, "source_status": source_status}
        if kind == "dependency":
            return "todo", "dependency_wait", payload
        recurrences = prev_recurrences + 1 if prev_kind == kind else 1
        payload = {"reason": reason, "kind": kind, "recurrences": recurrences, "source_status": source_status}
        if recurrences >= BLOCK_RECURRENCE_LIMIT:
            payload["limit"] = BLOCK_RECURRENCE_LIMIT
            return "triage", "block_loop_detected", payload
        return "blocked", "blocked", payload

    def _block_task(self, card_id: str, *, reason, kind, expected_run_id) -> bool:
        """kanban_db.block_task: only a `running` or `ready` card can be blocked (anything else, an already blocked merge card
        or a `todo` card included, returns False). Closes the active run as `blocked` (or writes one when there is a reason and
        no run), and records the routed event."""
        if kind is not None and kind not in VALID_BLOCK_KINDS:
            raise ValueError(f"block kind must be one of {sorted(VALID_BLOCK_KINDS)} or None")
        task = self._tasks.get(card_id)
        if task is None:
            return False
        source_status = self._retry_status_for_run(card_id) if task.status == "running" else "ready"
        new_status, event_kind, payload = self._route_block(
            kind, reason, source_status, prev_kind=task.block_kind, prev_recurrences=task.block_recurrences)
        if task.status not in ("running", "ready"):
            return False
        if expected_run_id is not None and task.current_run_id != expected_run_id:
            return False
        task.status = new_status
        task.claim_lock = None
        task.claim_expires = None
        task.worker_pid = None
        task.block_kind = kind
        if kind != "dependency":
            task.block_recurrences = payload["recurrences"]
        run_id = self._end_or_synthesize(
            task, outcome="blocked", status="blocked", summary=reason, synthesize=bool(reason))
        self._event(card_id, event_kind, payload, run_id)
        return True

    def _claim_is_live(self, task: _Task) -> bool:
        """kanban_db._claim_is_live: a running card whose worker process exists. Completing or handing it off without
        proof of ownership would close the run of a process that is still executing."""
        proc = self._procs.get(task.current_run_id) if task.current_run_id is not None else None
        return bool(
            task.status == "running" and task.claim_lock is not None and task.worker_pid
            and proc is not None and proc.alive
        )

    def _prior_reviewer(self, card_id: str):
        """kanban_db._prior_reviewer: the reviewer recorded by the latest changes_requested run's event. None is a first
        review (no such run); False is a run whose event has no usable reviewer."""
        changes_run = None
        for run in reversed(self._runs[card_id]):
            if run.outcome == "changes_requested":
                changes_run = run
                break
        if changes_run is None:
            return None
        event = self._latest_event(card_id, "changes_requested", changes_run.id)
        reviewer = (event.payload or {}).get("reviewer") if event else None
        return reviewer if isinstance(reviewer, str) and reviewer.strip() else False

    def _request_review(self, card_id: str, *, summary, metadata, reviewer, expected_run_id, force):
        """kanban_db.request_review: `running` or `ready` to `review`, reassigned to `reviewer` when one is named (with no
        reviewer named the card stays with its implementer, who would then review its own work; a re-review defaults to
        the reviewer of the latest changes_requested). The run ends `review_requested` with the summary and metadata, and
        a `review_requested` event names the implementer and reviewer so requested changes can route back. Returns
        (ok, reason)."""
        task = self._tasks.get(card_id)
        if task is None:
            return False, "task not found"
        if not self._parents_satisfied(card_id):
            return False, "parent dependencies are not satisfied"
        if expected_run_id is None and not force and self._claim_is_live(task):
            return False, (
                "task is running under a live claim; pass expected_run_id (worker ownership) or force=True "
                "(explicit operator override) instead of clearing the live run's claim")
        implementer = task.assignee
        if reviewer is None:
            reviewer = self._prior_reviewer(card_id)
            if reviewer is False:
                return False, (
                    "re-review has no durable reviewer provenance (the latest changes_requested event is missing or "
                    "malformed); pass reviewer= explicitly")
        reviewer = _canonical(reviewer)
        if task.status not in ("running", "ready") or (
                expected_run_id is not None and task.current_run_id != expected_run_id):
            return False, "task is not in running/ready (or expected_run_id did not match the current run)"
        task.status = "review"
        task.claim_lock = None
        task.claim_expires = None
        task.worker_pid = None
        if reviewer is not None:
            task.assignee = reviewer
        run_id = self._end_or_synthesize(
            task, outcome="review_requested", status="review", summary=summary, metadata=metadata,
            synthesize=bool(summary or metadata), profile=implementer)
        self._event(card_id, "review_requested", {
            "summary": _first_line(summary, 400) or None, "implementer": implementer, "reviewer": reviewer,
        }, run_id)
        return True, None

    def _request_changes(self, card_id: str, *, reason: str, expected_run_id):
        """kanban_db.request_changes: close an ACTIVE reviewer run (a `running` card claimed from review) and hand the card
        back to the implementer named by the latest review_requested event, `ready` or `todo` by its parents. The run ends
        `changes_requested` with the reason as its summary (Hermes stores no metadata on it). A card that only sits in
        `review` is refused, which is why the controller's own send-back is reopen-review. Returns (ok, implementer or
        reason)."""
        reason = (reason or "").strip()
        if not reason:
            return False, "reason is required"
        task = self._tasks.get(card_id)
        if task is None:
            return False, "task not found"
        if task.status != "running" or task.current_run_id is None:
            return False, "task is not in an active review run"
        if expected_run_id is not None and int(task.current_run_id) != int(expected_run_id):
            return False, "run_id mismatch"
        claimed = self._latest_event(card_id, "claimed", task.current_run_id)
        if ((claimed.payload if claimed else None) or {}).get("source_status") != "review":
            return False, "active run was not claimed from review"
        requested = self._latest_event(card_id, "review_requested")
        if requested is None:
            return False, "no prior review_requested event"
        implementer = (requested.payload or {}).get("implementer")
        if not (isinstance(implementer, str) and implementer.strip()):
            return False, "review handoff has no valid implementer provenance"
        reviewer = _canonical(task.assignee) if task.assignee and task.assignee.strip() else None
        new_status = "ready" if self._parents_satisfied(card_id) else "todo"
        task.status = new_status
        task.assignee = implementer
        task.claim_lock = None
        task.claim_expires = None
        task.worker_pid = None
        run_id = self._end_run(task, outcome="changes_requested", status=new_status, summary=reason)
        self._event(card_id, "changes_requested", {
            "reason": reason, "implementer": implementer, "reviewer": reviewer, "status": new_status,
        }, run_id)
        return True, implementer

    def _complete_task(self, card_id: str, *, result, summary, metadata, expected_run_id, force) -> bool:
        """kanban_db.complete_task: `running`, `ready`, `blocked` or `review` to `done`. Refused (False) while a parent is
        unfinished. A `running` card under a live worker needs the caller to own the run (expected_run_id) or force, else
        _LiveClaim. The closing run carries the summary (default: the result) and the metadata; with no active run one is
        synthesized (a merge card's, with profile None). Success wipes the failure counter, promotes children, and the
        `completed` event carries the summary's first line."""
        task = self._tasks.get(card_id)
        if task is None or not self._parents_satisfied(card_id):
            return False
        handoff_summary = summary if summary is not None else result
        prior_status = task.status
        if expected_run_id is None and not force and self._claim_is_live(task):
            raise _LiveClaim(
                f"{card_id} is running under a live worker claim; pass expected_run_id (worker ownership) or "
                "force=True (explicit operator override) instead of closing the live run")
        if task.status not in ("running", "ready", "blocked", "review"):
            return False
        if expected_run_id is not None and task.current_run_id != expected_run_id:
            return False
        task.status = "done"
        task.result = result
        task.completed_at = self.now
        task.claim_lock = None
        task.claim_expires = None
        task.worker_pid = None
        task.block_kind = None
        task.block_recurrences = 0
        run_id = self._end_run(task, outcome="completed", status="done", summary=handoff_summary, metadata=metadata)
        if run_id is None and (summary or metadata or result or prior_status == "review"):
            synth_summary, synth_metadata = handoff_summary, metadata
            if prior_status == "review" and not synth_summary and not synth_metadata:
                synth_summary = _REVIEW_APPROVED_NOTE
                synth_metadata = {"source_status": "review", "approval": "manual"}
            run_id = self._synthesize_run(task, outcome="completed", summary=synth_summary, metadata=synth_metadata)
        event_summary = handoff_summary
        if prior_status == "review" and not event_summary:
            event_summary = _REVIEW_APPROVED_NOTE
        self._event(card_id, "completed", {
            "result_len": len(result) if result else 0, "summary": _first_line(event_summary, 400) or None,
        }, run_id)
        self._clear_failure_counter(task)
        self._recompute_ready()
        return True

    def _promote_task(self, card_id: str, *, actor: str, reason):
        """kanban_db.promote_task: `todo` or `blocked` to `ready`, refused while a parent is unfinished (an archived
        parent is finished). Returns (ok, error text)."""
        task = self._tasks.get(card_id)
        if task is None:
            return False, f"task {card_id} not found"
        if task.status not in ("todo", "blocked"):
            return False, f"task {card_id} is {task.status!r}; promote only applies to 'todo' or 'blocked'"
        unsatisfied = [p for p in self._parents_of(card_id) if self._tasks[p].status not in ("done", "archived")]
        if unsatisfied:
            return False, (
                f"unsatisfied parent dependencies: {', '.join(unsatisfied)} (the ready -> running claim re-checks "
                f"parents, so promotion cannot bypass them; complete the parents or drop the link with "
                f"`hermes kanban unlink <parent_id> {card_id}`)")
        task.status = "ready"
        self._event(card_id, "promoted_manual", {"actor": actor, "reason": reason})
        return True, None

    def _reclaim_dangling_run(self, task: _Task, statuses, note: str) -> None:
        """kanban_db._reclaim_dangling_run: close a leaked open run before a status flip (no-op normally)."""
        if task.status in statuses and task.current_run_id:
            run = self._run_by_id[task.current_run_id]
            if run.ended_at is None:
                run.status = "reclaimed"
                run.outcome = "reclaimed"
                run.summary = run.summary or note
                run.ended_at = self.now
                run.claim_lock = None
                run.claim_expires = None
                run.worker_pid = None

    def _unblock_task(self, card_id: str) -> bool:
        """kanban_db.unblock_task: `blocked` or `scheduled` to its resumable phase (`ready`, `todo` while a parent is
        unfinished, or `review` when the card left from review). Resets the failure counter (a deliberate unblock is a
        fresh retry budget) but NOT block_kind or block_recurrences, so a same-kind re-block still reads as a loop. The
        `unblocked` event has a payload only when the landing status is not plain `ready`."""
        task = self._tasks.get(card_id)
        if task is None or task.status not in ("blocked", "scheduled"):
            return False
        resume_status = self._resume_status_from_events(card_id) if task.status == "blocked" else "ready"
        self._reclaim_dangling_run(task, ("blocked", "scheduled"), "invariant recovery on unblock")
        landing = "ready" if self._parents_satisfied(card_id) else "todo"
        new_status = "review" if landing == "ready" and resume_status == "review" else landing
        task.status = new_status
        task.current_run_id = None
        task.consecutive_failures = 0
        task.last_failure_error = None
        self._event(
            card_id, "unblocked",
            {"status": new_status, "resume_status": resume_status}
            if (new_status != "ready" or resume_status != "ready") else None)
        return True

    def _reopen_review(self, card_id: str) -> bool:
        """kanban_db.reopen_review_task: `review` to `ready` or `todo`, restoring the implementer from the latest
        review_requested event. Preserves the failure counter (a review is not a success signal)."""
        task = self._tasks.get(card_id)
        if task is None or task.status != "review":
            return False
        self._reclaim_dangling_run(task, ("review",), "invariant recovery on review reopen")
        new_status = "ready" if self._parents_satisfied(card_id) else "todo"
        requested = self._latest_event(card_id, "review_requested")
        implementer = (requested.payload or {}).get("implementer") if requested else None
        implementer = implementer if isinstance(implementer, str) and implementer.strip() else None
        task.status = new_status
        task.current_run_id = None
        task.claim_lock = None
        task.claim_expires = None
        task.worker_pid = None
        if implementer:
            task.assignee = implementer
        payload = {"status": new_status}
        if implementer:
            payload["implementer"] = implementer
        self._event(card_id, "review_reopened", payload if payload != {"status": "ready"} else None)
        return True

    def _archive_task(self, card_id: str) -> bool:
        """kanban_db.archive_task: any status but `archived` to `archived`. An active run is closed as reclaimed and its
        worker is terminated (an `archive_worker_termination` event); archived parents then no longer block children."""
        task = self._tasks.get(card_id)
        if task is None or task.status == "archived":
            return False
        was_running = task.status == "running"
        proc = self._procs.get(task.current_run_id) if was_running and task.current_run_id else None
        task.status = "archived"
        task.claim_lock = None
        task.claim_expires = None
        task.worker_pid = None
        run_id = self._end_run(
            task, outcome="reclaimed", status="reclaimed", summary="task archived with run still active")
        self._event(card_id, "archived", None, run_id)
        if was_running:
            self._event(card_id, "archive_worker_termination", self._terminate(proc), run_id)
        self._recompute_ready()
        return True

    def _schedule_task(self, card_id: str, *, reason, expected_run_id) -> bool:
        """kanban_db.schedule_task: `todo`, `ready`, `running` or `blocked` to `scheduled` (waiting on time, not
        dispatchable) until an unblock re-gates it. A reason is written into a synthesized run when none was active."""
        task = self._tasks.get(card_id)
        if task is None or task.status not in ("todo", "ready", "running", "blocked"):
            return False
        if expected_run_id is not None and task.current_run_id != expected_run_id:
            return False
        task.status = "scheduled"
        task.claim_lock = None
        task.claim_expires = None
        task.worker_pid = None
        run_id = self._end_or_synthesize(
            task, outcome="scheduled", status="scheduled", summary=reason, synthesize=bool(reason))
        self._event(card_id, "scheduled", {"reason": reason}, run_id)
        return True

    def _terminate(self, proc: _Proc | None) -> dict:
        """kanban_db_dispatch._terminate_reclaimed_worker: SIGTERM the worker (here: mark it dead) and report it."""
        info = {
            "prev_pid": None, "host_local": False, "termination_attempted": False, "terminated": False,
            "sigkill": False,
        }
        if proc is None:
            return info
        info.update(prev_pid=proc.pid, host_local=True, termination_attempted=True, terminated=True)
        proc.alive = False
        proc.hung = False
        if proc.exit_kind is None:
            proc.exit_kind, proc.exit_code = "signaled", 15
        self._drop_deferred(proc.run_id)
        return info

    def _record_reclaim(self, task: _Task, termination: dict, *, error: str, payload: dict) -> int | None:
        run_id = self._end_run(task, outcome="reclaimed", status="reclaimed", error=error, metadata=termination)
        payload.update(termination)
        self._event(task.id, "reclaimed", payload, run_id)
        return run_id

    def _reclaim_task(self, card_id: str, *, reason) -> bool:
        """kanban_db.reclaim_task: an operator reclaim regardless of the TTL. Terminates the worker, releases the claim,
        returns the card to where the run came from (`ready`, or `review` for a reviewer run), closes the run as
        `reclaimed` with error "manual_reclaim: <reason>", and resets the failure counter. False for a card that is not
        running and holds no claim."""
        task = self._tasks.get(card_id)
        if task is None or (task.status != "running" and task.claim_lock is None):
            return False
        prev_lock = task.claim_lock
        proc = self._procs.get(task.current_run_id) if task.current_run_id is not None else None
        termination = self._terminate(proc if (task.worker_pid and prev_lock) else None)
        retry_status = self._retry_status_for_run(card_id)
        if task.status not in ("running", "ready", "blocked"):
            return False
        task.status = retry_status
        task.claim_lock = None
        task.claim_expires = None
        task.worker_pid = None
        self._record_reclaim(
            task, termination,
            error=f"manual_reclaim: {reason}" if reason else f"manual_reclaim lock={prev_lock}",
            payload={"manual": True, "reason": reason, "prev_lock": prev_lock, "retry_status": retry_status})
        self._clear_failure_counter(task)
        return True

    @staticmethod
    def _clear_failure_counter(task: _Task) -> None:
        task.consecutive_failures = 0
        task.last_failure_error = None

    def _set_model_override(self, card_id: str, model, provider) -> bool:
        """kanban_db.set_model_override: pin (or, with model None, clear) a card's model and provider. A provider needs a
        model. Allowed while running (it applies on the next dispatch); refused on an archived card."""
        model = (model or "").strip() or None
        provider = (provider or "").strip() or None
        if provider and not model:
            raise ValueError("provider_override requires a model_override")
        task = self._tasks.get(card_id)
        if task is None:
            return False
        if task.status == "archived":
            raise RuntimeError(f"cannot set model override on archived task {card_id}")
        task.model_override = model
        task.provider_override = provider
        self._event(card_id, "model_override_set", {"model": model, "provider": provider})
        return True

    def _record_task_failure(
        self, task: _Task, error: str, *, outcome: str, failure_limit: int | None = None, force_trip: bool = False,
        release_claim: bool = False, end_run: bool = False, event_payload_extra: dict | None = None,
    ) -> bool:
        """kanban_db_dispatch._record_task_failure: every non-success funnels through here. The failure counter goes up by
        one; at max_retries (else the dispatcher's limit, default 2, "--max-retries N trips on the Nth failure") the card
        goes to `blocked` with a `gave_up` event carrying failures, effective_limit, limit_source, error, trigger_outcome and
        retry_status, and NO `blocked` event. `release_claim` and `end_run` are the spawn path (the card is still running
        with an open run: restore its source phase, close the run); both False is the crash and timeout path (the caller
        already restored the phase and closed the run, only the counter moves). Returns True when the card was blocked."""
        if failure_limit is None:
            failure_limit = DEFAULT_FAILURE_LIMIT
        error = error[:500]
        retry_status = (
            self._retry_status_for_run(task.id, task.current_run_id) if release_claim
            else ("review" if task.status == "review" else "ready")
        )
        failures = task.consecutive_failures + 1
        if task.max_retries is not None:
            effective_limit, limit_source = int(task.max_retries), "task"
        else:
            effective_limit, limit_source = int(failure_limit), "dispatcher"
        if not (force_trip or failures >= effective_limit):
            if release_claim:
                if task.status == "running":
                    task.status = retry_status
                    task.claim_lock = None
                    task.claim_expires = None
                    task.worker_pid = None
                    task.consecutive_failures = failures
                    task.last_failure_error = error
            else:
                task.consecutive_failures = failures
                task.last_failure_error = error
            if end_run:
                run_id = self._end_run(
                    task, outcome=outcome, status=outcome, error=error,
                    metadata={"failures": failures, "retry_status": retry_status})
                self._event(
                    task.id, outcome, {"error": error, "failures": failures, "retry_status": retry_status}, run_id)
            return False
        if task.status in ("running", "ready", "review"):
            task.status = "blocked"
            if release_claim:
                task.claim_lock = None
                task.claim_expires = None
                task.worker_pid = None
            task.consecutive_failures = failures
            task.last_failure_error = error
        payload = {
            "failures": failures, "effective_limit": effective_limit, "limit_source": limit_source, "error": error,
            "trigger_outcome": outcome, "retry_status": retry_status,
        }
        run_id = None
        if end_run:
            run_id = self._end_run(
                task, outcome="gave_up", status="gave_up", error=error,
                metadata={
                    "failures": failures, "trigger_outcome": outcome, "effective_limit": effective_limit,
                    "limit_source": limit_source, "retry_status": retry_status,
                })
        if event_payload_extra:
            payload.update(event_payload_extra)
        self._event(task.id, "gave_up", payload, run_id)
        return True

    # ---------------------------------------------------------------------------------------
    # The dispatcher (kanban_db_dispatch.dispatch_once and its reclaim phase)
    # ---------------------------------------------------------------------------------------

    @staticmethod
    def _empty_dispatch_result() -> dict:
        """The keys `hermes kanban dispatch --json` prints (kanban_ops._cmd_dispatch), all always present."""
        return {
            "reclaimed": 0, "crashed": [], "timed_out": [], "stale": [], "auto_blocked": [], "promoted": 0,
            "reaped_terminal_workers": [], "spawned": [], "skipped_unassigned": [], "skipped_nonspawnable": [],
            "skipped_per_profile_capped": [], "auto_assigned_default": [], "respawn_guarded": [], "rate_limited": [],
            "skipped_locked": False, "memory_pressure": None,
        }

    def _profile_exists(self, name: str | None) -> bool:
        return bool(name) and (name in self._workers or name in self.extra_profiles)

    def _next_pid(self) -> int:
        self._pid_seq += 1
        return FAKE_PID_BASE + self._pid_seq

    def _has_deferred(self, run_id: int) -> bool:
        return any(d.run_id == run_id for d in self._deferred)

    def _drop_deferred(self, run_id: int) -> None:
        self._deferred = [d for d in self._deferred if d.run_id != run_id]

    def _settle(self, proc: _Proc) -> None:
        """A worker function (or a woken continuation) returned. It is still alive when it is hung or has a later wake-up
        pending; otherwise its process exited with code 0. A card still `running` under a process that exited cleanly is
        Hermes's protocol violation, booked at the next reclaim phase."""
        if proc.hung or self._has_deferred(proc.run_id):
            return
        proc.alive = False
        if proc.exit_kind is None:
            proc.exit_kind, proc.exit_code = "clean_exit", 0

    def _advance_to(self, target: int) -> None:
        """Move the clock to `target`, waking sleeping workers in time order on the way. A worker whose wake-up is at or
        after its run's deadline (started_at + max_runtime) is not woken: it was killed by the timeout first, and the
        reclaim phase that follows books that."""
        while True:
            due = [d for d in self._deferred if d.at <= target]
            if not due:
                break
            item = min(due, key=lambda d: (d.at, d.seq))
            self._deferred.remove(item)
            task = self._tasks[item.task_id]
            run = self._run_by_id.get(item.run_id)
            proc = self._procs.get(item.run_id)
            if proc is None or not proc.alive or task.status != "running" or task.current_run_id != item.run_id:
                continue
            if run is not None and run.max_runtime_seconds is not None and item.at >= run.started_at + run.max_runtime_seconds:
                continue
            self.now = max(self.now, item.at)
            try:
                item.fn()
            except Exception as exc:
                proc.alive = False
                proc.exit_kind, proc.exit_code = "nonzero_exit", 1
                raise _WorkerFailed(exc) from None
            except BaseException:
                proc.alive = False
                proc.exit_kind, proc.exit_code = "nonzero_exit", 1
                raise
            self._settle(proc)
        self.now = max(self.now, target)

    def _reclaim_phase(self, result: dict) -> None:
        """kanban_db_dispatch._run_reclaim_phase, in its order: workers that outlived their closed run, stale claims, dead
        workers, timeouts, then promotion. (Orphan reconciliation and the no-heartbeat sweep are off by default in Hermes
        and not modelled.)"""
        result["reaped_terminal_workers"] = self._reap_terminal_workers()
        result["reclaimed"] = self._release_stale_claims()
        self._detect_crashed_workers(result)
        result["timed_out"] = self._enforce_max_runtime()
        result["promoted"] = self._recompute_ready(failure_limit=self.failure_limit)

    def _reap_terminal_workers(self) -> list[str]:
        """kanban_db_dispatch.reap_terminal_workers: a worker still alive TERMINAL_WORKER_REAP_GRACE_SECONDS after its run
        was closed (the controller blocked or completed the card underneath it) is terminated."""
        reaped: list[str] = []
        for run in list(self._run_by_id.values()):
            if run.ended_at is None or run.ended_at > self.now - TERMINAL_WORKER_REAP_GRACE_SECONDS:
                continue
            proc = self._procs.get(run.id)
            if proc is None or not proc.alive:
                continue
            termination = self._terminate(proc)
            self._event(run.task_id, "terminal_worker_reaped",
                        {"pid": proc.pid, "worker_started_at": f"fake|{run.id}", **termination}, run.id)
            reaped.append(run.task_id)
        return reaped

    def _release_stale_claims(self) -> int:
        """kanban_db.release_stale_claims: a `running` card whose claim expired. A live worker keeps its claim (extended,
        `claim_extended` event) unless its heartbeat is over an hour old; otherwise the claim is reclaimed, the run closes as
        `reclaimed` with error "stale_lock=<lock>", and the reclaim counts as a failure (a claim that expires without a
        worker ever spawning would otherwise loop for ever)."""
        reclaimed = 0
        for task in list(self._tasks.values()):
            if task.status != "running" or task.claim_expires is None or task.claim_expires >= self.now:
                continue
            proc = self._procs.get(task.current_run_id) if task.current_run_id is not None else None
            heartbeat = task.last_heartbeat_at
            heartbeat_stale = heartbeat is not None and (self.now - heartbeat) > CLAIM_HEARTBEAT_MAX_STALE_SECONDS
            if task.worker_pid and proc is not None and proc.alive and not heartbeat_stale:
                expires_was = task.claim_expires
                task.claim_expires = self.now + self.claim_ttl_seconds
                run = self._run_by_id.get(task.current_run_id)
                if run is not None:
                    run.claim_expires = task.claim_expires
                self._event(task.id, "claim_extended", {
                    "reason": "pid_alive", "worker_pid": task.worker_pid, "claim_lock": task.claim_lock,
                    "claim_expires_was": expires_was, "claim_expires_now": task.claim_expires,
                    "last_heartbeat_at": heartbeat,
                }, task.current_run_id)
                continue
            termination = self._terminate(proc if (task.worker_pid and task.claim_lock) else None)
            lock, pid, expires = task.claim_lock, task.worker_pid, task.claim_expires
            retry_status = self._retry_status_for_run(task.id)
            task.status = retry_status
            task.claim_lock = None
            task.claim_expires = None
            task.worker_pid = None
            self._record_reclaim(task, termination, error=f"stale_lock={lock}", payload={
                "stale_lock": lock, "worker_pid": pid, "claim_expires": expires, "last_heartbeat_at": heartbeat,
                "now": self.now, "host_local": True, "heartbeat_stale": bool(heartbeat_stale),
                "retry_status": retry_status,
            })
            reclaimed += 1
            self._record_task_failure(
                task, f"stale_lock={lock}", outcome="reclaimed", failure_limit=self.failure_limit,
                event_payload_extra={"worker_pid": pid, "retry_status": retry_status})
        return reclaimed

    def _detect_crashed_workers(self, result: dict) -> None:
        """kanban_db_dispatch.detect_crashed_workers: a `running` card whose worker process is dead, 30 seconds (the
        crash grace) after the card first started. The exit status decides the booking: exit 0 is a protocol violation (a
        clean exit without kanban_complete or kanban_block, event `protocol_violation`), exit 75 is a quota wall (run
        outcome `rate_limited`, no failure counted), anything else a `crashed` run whose error reads "pid N exited with
        code C" or "pid N killed by signal S". The card goes back to where the run came from."""
        details = []
        for task in list(self._tasks.values()):
            if task.status != "running" or task.worker_pid is None:
                continue
            if task.started_at is not None and self.now - task.started_at < self.crash_grace_seconds:
                continue
            proc = self._procs.get(task.current_run_id) if task.current_run_id is not None else None
            if proc is None or proc.alive:
                continue
            pid, lock = proc.pid, task.claim_lock
            kind, code = proc.exit_kind or "unknown", proc.exit_code
            protocol = rate = False
            if kind == "clean_exit":
                protocol = True
                error_text, event_kind = _PROTOCOL_VIOLATION_ERROR, "protocol_violation"
                payload = {"pid": pid, "claimer": lock, "exit_code": code, "protocol_violation": True}
            elif kind == "rate_limited":
                rate = True
                error_text = f"pid {pid} exited rate-limited (quota wall) - requeued without counting a failure"
                event_kind = "rate_limited"
                payload = {"pid": pid, "claimer": lock, "exit_code": code}
            else:
                if kind == "nonzero_exit":
                    error_text = f"pid {pid} exited with code {code}"
                elif kind == "signaled":
                    error_text = f"pid {pid} killed by signal {code}"
                else:
                    error_text = f"pid {pid} not alive"
                event_kind = "crashed"
                payload = {"pid": pid, "claimer": lock}
                if code is not None and kind != "unknown":
                    payload["exit_kind"] = kind
                    payload["exit_code"] = code
            retry_status = self._retry_status_for_run(task.id)
            payload["retry_status"] = retry_status
            task.status = retry_status
            task.claim_lock = None
            task.claim_expires = None
            task.worker_pid = None
            outcome = "rate_limited" if rate else "crashed"
            run_id = self._end_run(task, outcome=outcome, status=outcome, error=error_text, metadata=dict(payload))
            self._event(task.id, event_kind, payload, run_id)
            if rate or protocol:
                task.last_failure_error = error_text[:500]
            if rate:
                result["rate_limited"].append(task.id)
            else:
                result["crashed"].append(task.id)
                details.append((task.id, pid, lock, protocol, error_text))
        if details:
            result["auto_blocked"].extend(self._account_crashes(details))

    def _protocol_violation_streak(self, card_id: str) -> int:
        """kanban_db_dispatch._protocol_violation_streak: the trailing run of clean-exit protocol violations among the
        card's closed runs (a rate_limited run is neutral, anything else breaks the streak)."""
        streak = 0
        for run in reversed(self._runs[card_id]):
            if run.ended_at is None:
                continue
            if run.outcome == "rate_limited":
                continue
            if run.outcome == "crashed" and (
                    (run.metadata or {}).get("protocol_violation") or "protocol violation" in (run.error or "")):
                streak += 1
                continue
            break
        return streak

    def _account_crashes(self, details: list) -> list[str]:
        """kanban_db_dispatch._account_crashes: count each crash against the breaker. A protocol violation has its own
        bounded streak (3, or the card's max_retries) that neither consumes nor extends the failure counter."""
        auto_blocked: list[str] = []
        for card_id, pid, claimer, protocol, error_text in details:
            task = self._tasks[card_id]
            if protocol:
                streak = self._protocol_violation_streak(card_id)
                limit = int(task.max_retries) if task.max_retries is not None else PROTOCOL_VIOLATION_FAILURE_LIMIT
                if streak < limit:
                    continue
                tripped = self._record_task_failure(
                    task, error_text, outcome="crashed", failure_limit=limit, force_trip=True,
                    event_payload_extra={
                        "pid": pid, "claimer": claimer, "protocol_violations": streak,
                        "protocol_violation_limit": limit,
                    })
            else:
                tripped = self._record_task_failure(
                    task, error_text, outcome="crashed", event_payload_extra={"pid": pid, "claimer": claimer})
            if tripped:
                auto_blocked.append(card_id)
        return auto_blocked

    def _enforce_max_runtime(self) -> list[str]:
        """kanban_db_dispatch.enforce_max_runtime: a `running` card whose active run has lasted its max_runtime. The worker
        is terminated, the run closes as `timed_out` with error "elapsed Ns > limit Ms", the card returns to where the run
        came from, and the timeout counts as a failure (a trip goes to `blocked` with `gave_up` on top)."""
        timed_out: list[str] = []
        for task in list(self._tasks.values()):
            if task.status != "running" or task.max_runtime_seconds is None or task.worker_pid is None:
                continue
            run = self._run_by_id.get(task.current_run_id)
            started = run.started_at if run is not None else task.started_at
            if started is None:
                continue
            elapsed = self.now - started
            limit = int(task.max_runtime_seconds)
            if elapsed < limit:
                continue
            pid = task.worker_pid
            self._terminate(self._procs.get(task.current_run_id))
            error = f"elapsed {int(elapsed)}s > limit {limit}s"
            retry_status = self._retry_status_for_run(task.id)
            task.status = retry_status
            task.claim_lock = None
            task.claim_expires = None
            task.worker_pid = None
            task.last_heartbeat_at = None
            payload = {
                "pid": pid, "elapsed_seconds": int(elapsed), "limit_seconds": limit, "sigkill": False,
                "retry_status": retry_status,
            }
            run_id = self._end_run(task, outcome="timed_out", status="timed_out", error=error, metadata=payload)
            self._event(task.id, "timed_out", payload, run_id)
            timed_out.append(task.id)
            self._record_task_failure(
                task, error, outcome="timed_out",
                event_payload_extra={"pid": pid, "sigkill": False, "retry_status": retry_status})
        return timed_out

    def _respawn_guard(self, task: _Task, lane: str) -> str | None:
        """kanban_db_dispatch.check_respawn_guard, in its order: `rate_limit_cooldown` (the latest closed run was a quota
        wall, for 5 minutes), `blocker_auth` (the last failure reads like a quota or auth error, so a retry cannot help),
        and, for the ready lane only, `recent_success` (a completed run within the hour with nothing re-queuing the card
        since). The active_pr rule (a GitHub PR URL in a comment) is not modelled."""
        latest = None
        for run in self._runs[task.id]:
            if run.ended_at is not None and (latest is None or run.ended_at >= latest.ended_at):
                latest = run
        if latest is not None and latest.outcome == "rate_limited":
            if self.rate_limit_cooldown_seconds <= 0:
                return None
            if self.now - latest.ended_at < self.rate_limit_cooldown_seconds:
                return "rate_limit_cooldown"
            return None
        if task.last_failure_error and _RESPAWN_BLOCKER_RE.search(task.last_failure_error):
            return "blocker_auth"
        if lane == "review":
            return None
        cutoff = self.now - RESPAWN_GUARD_SUCCESS_WINDOW
        completed = [
            r.ended_at for r in self._runs[task.id]
            if r.outcome == "completed" and r.ended_at is not None and r.ended_at >= cutoff
        ]
        if completed:
            completed_at = max(completed)
            requeued = any(
                e.kind in ("status", "promoted", "unblocked", "reclaimed") and e.created_at >= completed_at
                for e in self._events[task.id]
            )
            if not requeued:
                return "recent_success"
        return None

    def _claim(self, task: _Task, lane: str) -> _Run | None:
        """kanban_db.claim_task and claim_review_task: `ready` (or, for the review lane, `review`) to `running` with a
        claim lock and expiry, and a new run for the assignee's profile. Parents are re-checked first: a card whose parent
        is not done is demoted to `todo` (claim_rejected, or dependency_wait from review) and not spawned."""
        if not self._parents_satisfied(task.id):
            if lane == "review":
                if task.status == "review" and task.claim_lock is None:
                    task.status = "todo"
                    self._event(task.id, "dependency_wait", {"reason": "parent_reopened", "source_status": "review"})
            else:
                if task.status == "ready":
                    task.status = "todo"
                self._event(task.id, "claim_rejected", {"reason": "parents_not_done"})
            return None
        source = "review" if lane == "review" else "ready"
        if lane != "review":
            self._reclaim_dangling_run(task, ("ready",), "invariant recovery on re-claim")
        if task.status != source or task.claim_lock is not None:
            return None
        self._claim_seq += 1
        lock = f"fake-host:{self._claim_seq}"
        expires = self.now + self.claim_ttl_seconds
        task.status = "running"
        task.claim_lock = lock
        task.claim_expires = expires
        if task.started_at is None:
            task.started_at = self.now
        run = self._new_run(task, lock, expires)
        extra = {"source_status": "review"} if lane == "review" else {}
        self._event(task.id, "claimed", {"lock": lock, "expires": expires, "run_id": run.id, **extra}, run.id)
        return run

    @staticmethod
    def _git_common_dir(path) -> pathlib.Path | None:
        result = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return pathlib.Path(result.stdout.strip()).resolve()

    def _ensure_git_worktree(self, target: pathlib.Path, branch: str) -> None:
        """kanban_db_workspace._ensure_git_worktree with real git. An existing checkout of this repository is reused (a
        retried card keeps its worktree). Otherwise `git worktree add`: onto the branch when it already exists, else
        `-b <branch>` cut from the integration branch, whose tip is the primary checkout's HEAD while the checkout is
        where the controller keeps it (Hermes cuts from HEAD)."""
        common = self._git_common_dir(self.repo)
        if target.exists() and common is not None and self._git_common_dir(target) == common:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        exists = _git(self.repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0
        if exists:
            args = ["worktree", "add", str(target), branch]
        else:
            args = ["worktree", "add", "-b", branch, str(target), self.integration_branch]
        result = _git(self.repo, *args)
        if result.returncode != 0:
            raise RuntimeError(
                f"git worktree add failed for {target} on branch {branch}: {(result.stderr or result.stdout).strip()}")

    def _resolve_workspace(self, task: _Task) -> tuple[pathlib.Path | None, str | None]:
        """kanban_db_workspace.resolve_workspace: (path, branch). A `worktree` card gets a real git worktree at
        <primary>/.worktrees/<card id> (or its own path) on its branch; a `dir` card its directory; a `scratch` card a
        directory under scratch_root when the fake has one, else no path."""
        kind = task.workspace_kind
        if kind == "worktree":
            if self.repo is None:
                raise ValueError(
                    f"task {task.id} has workspace_kind=worktree but this FakeHermes has no primary checkout (repo=)")
            branch = (task.branch_name or "").strip() or f"wt/{task.id}"
            target = pathlib.Path(task.workspace_path) if task.workspace_path else self.repo / ".worktrees" / task.id
            self._ensure_git_worktree(target, branch)
            return target, branch
        if kind == "dir":
            if not task.workspace_path:
                raise ValueError(f"task {task.id} has workspace_kind=dir but no workspace_path")
            path = pathlib.Path(task.workspace_path)
        elif task.workspace_path:
            path = pathlib.Path(task.workspace_path)
        elif self.scratch_root is not None:
            path = self.scratch_root / task.id
        else:
            return None, None
        path.mkdir(parents=True, exist_ok=True)
        return path, None

    def _check_spawn_failure(self, task: _Task) -> None:
        for armed in list(self._spawn_failures):
            if armed.profile is not None and armed.profile != task.assignee:
                continue
            if armed.card_id is not None and armed.card_id != task.id:
                continue
            armed.times -= 1
            if armed.times <= 0:
                self._spawn_failures.remove(armed)
            raise RuntimeError(armed.error)

    def _dispatch_lane_task(
        self, task: _Task, assignee: str, result: dict, *, lane: str, dry_run: bool, per_profile_running: dict,
    ) -> bool:
        """kanban_db_dispatch._dispatch_lane_task: guard, claim, resolve the workspace and spawn one row. Returns True when a
        spawn slot was used (real or dry-run); every skip is recorded on `result`. A workspace or spawn failure is a
        `spawn_failed` run and a counted failure; the worker itself then runs to its end, synchronously."""
        tid = task.id
        if not self._profile_exists(assignee):
            result["skipped_nonspawnable"].append(tid)
            return False
        cap = self.max_in_progress_per_profile
        cap = cap if isinstance(cap, int) and cap > 0 else None
        if cap is not None:
            current = per_profile_running.get(assignee, 0)
            if current >= cap:
                result["skipped_per_profile_capped"].append({"task_id": tid, "assignee": assignee, "current": current})
                return False
        guard = self._respawn_guard(task, lane)
        if guard is not None:
            result["respawn_guarded"].append({"task_id": tid, "reason": guard})
            if not dry_run:
                self._event(tid, "respawn_guarded", {"reason": guard})
            return False
        if dry_run:
            result["spawned"].append({"task_id": tid, "assignee": assignee, "workspace": ""})
            per_profile_running[assignee] = per_profile_running.get(assignee, 0) + 1
            return True
        run = self._claim(task, lane)
        if run is None:
            return False
        try:
            workspace, branch = self._resolve_workspace(task)
        except Exception as exc:  # noqa: BLE001 - Hermes books any workspace failure the same way
            if self._record_task_failure(
                    task, f"workspace: {exc}", outcome="spawn_failed", failure_limit=self.failure_limit,
                    release_claim=True, end_run=True):
                result["auto_blocked"].append(tid)
            return False
        if workspace is not None:
            task.workspace_path = str(workspace)
        if task.workspace_kind == "worktree":
            task.branch_name = branch or (task.branch_name or "").strip() or f"wt/{task.id}"
        try:
            self._check_spawn_failure(task)
        except Exception as exc:  # noqa: BLE001
            if self._record_task_failure(
                    task, str(exc), outcome="spawn_failed", failure_limit=self.failure_limit,
                    release_claim=True, end_run=True):
                result["auto_blocked"].append(tid)
            return False
        pid = self._next_pid()
        proc = _Proc(run_id=run.id, task_id=tid, pid=pid)
        self._procs[run.id] = proc
        task.worker_pid = pid
        run.worker_pid = pid
        self._event(tid, "spawned", {"pid": pid, "started_at": f"fake|{run.id}"}, run.id)
        result["spawned"].append({"task_id": tid, "assignee": task.assignee or "", "workspace": str(workspace or "")})
        per_profile_running[task.assignee] = per_profile_running.get(task.assignee, 0) + 1
        self._run_worker(task, run, proc, workspace)
        return True

    def _run_worker(self, task: _Task, run: _Run, proc: _Proc, workspace: pathlib.Path | None) -> None:
        """Run the profile's registered worker to its end (or to its first Sleep), synchronously. An exception from a
        worker script is a bug in the test, so it propagates, after the process is marked dead."""
        card = self._show(task.id)
        run_view = self._run_dict(run)
        worker = self._workers[task.assignee]
        try:
            if _is_factory(worker):
                worker = worker(card)
            worker(self, card, run_view, workspace)
        except Exception as exc:
            proc.alive = False
            proc.exit_kind, proc.exit_code = "nonzero_exit", 1
            raise _WorkerFailed(exc) from None
        except BaseException:
            proc.alive = False
            proc.exit_kind, proc.exit_code = "nonzero_exit", 1
            raise
        self._settle(proc)

    def _lane(self, status: str) -> list[_Task]:
        """kanban_db_dispatch._lane_rows: unclaimed cards of one status in dispatch order (priority, then creation)."""
        rows = [t for t in self._tasks.values() if t.status == status and t.claim_lock is None]
        return sorted(rows, key=lambda t: (-t.priority, t.seq))

    def _spawn_budget(self, max_spawn: int | None) -> tuple[bool, int | None]:
        """kanban_db_dispatch._tick_spawn_budget: `max_spawn` (the CLI's --max) and max_in_progress are LIVE concurrency
        caps, not per-tick budgets: the running cards count against them."""
        running = 0
        budget: int | None = None
        if max_spawn is not None or self.max_in_progress is not None:
            running = sum(1 for t in self._tasks.values() if t.status == "running")
        if max_spawn is not None:
            if running >= max_spawn:
                return False, None
            budget = max_spawn - running
        if self.max_in_progress is not None:
            if running >= self.max_in_progress:
                return False, None
            remaining = self.max_in_progress - running
            if budget is None or budget > remaining:
                budget = remaining
        return True, budget

    def _spawn_phase(self, result: dict, *, dry_run: bool, max_spawn: int | None) -> None:
        """The spawn half of kanban_db_dispatch._dispatch_once_locked: the ready lane first, then the review lane, sharing
        one budget, with one slot held back for the review lane when a spawnable review card waits. The rows are read once
        up front, so a card that a worker moves into `review` during this pass is reviewed on the NEXT pass."""
        may_spawn, budget = self._spawn_budget(max_spawn)
        if not may_spawn:
            return
        ready_rows = [(t.id, t.assignee) for t in self._lane("ready")]
        review_rows = [(t.id, t.assignee) for t in self._lane("review")] if self.review_dispatch else []
        ready_budget = budget
        if budget is not None and budget > 0 and any(a and self._profile_exists(a) for _, a in review_rows):
            ready_budget = max(budget - 1, 0)
        per_profile_running: dict[str, int] = {}
        for task in self._tasks.values():
            if task.status == "running" and task.assignee:
                per_profile_running[task.assignee] = per_profile_running.get(task.assignee, 0) + 1
        spawned = 0
        for tid, assignee in ready_rows:
            if ready_budget is not None and spawned >= ready_budget:
                break
            if not assignee:
                result["skipped_unassigned"].append(tid)
                continue
            if self._dispatch_lane_task(
                    self._tasks[tid], assignee, result, lane="ready", dry_run=dry_run,
                    per_profile_running=per_profile_running):
                spawned += 1
        for tid, assignee in review_rows:
            if budget is not None and spawned >= budget:
                break
            if not assignee:
                result["skipped_unassigned"].append(tid)
                continue
            if self._dispatch_lane_task(
                    self._tasks[tid], assignee, result, lane="review", dry_run=dry_run,
                    per_profile_running=per_profile_running):
                spawned += 1

    def _dispatch(self, *, dry_run: bool, max_spawn: int | None) -> dict:
        """One dispatcher tick (dispatch_once): the reclaim phase, then the spawn phase. The reclaim phase runs even in a
        dry run, as in Hermes, where only the spawns are simulated."""
        result = self._empty_dispatch_result()
        self._reclaim_phase(result)
        self._spawn_phase(result, dry_run=dry_run, max_spawn=max_spawn)
        return result

    # ---------------------------------------------------------------------------------------
    # The controller side: the same names and signatures as ases.hermes (install() puts these in its place).
    # The CLI argument lists in the error text are the ones hermes.py builds, so a failure reads like the real one.
    # ---------------------------------------------------------------------------------------

    @_controller_call
    def hermes_path(self) -> str:
        """No real hermes exists for the rig: HermesNotFound, which everything that shells out already handles. Set
        `hermes_path_value` to hand out a path anyway."""
        if self.hermes_path_value is None:
            raise _hermes.HermesNotFound("`hermes` is not on PATH (FakeHermes: the rig never runs a real hermes)")
        return self.hermes_path_value

    @_controller_call
    def hermes_version(self) -> str | None:
        return self.version

    @_controller_call
    def run_doctor(self, timeout: int = 60) -> _hermes.DoctorResult:
        return self.doctor

    @_controller_call
    def gateway_status(self, timeout: int = 20) -> _hermes.GatewayStatus:
        return _hermes.GatewayStatus(self.gateway_running, self.gateway_output)

    @_controller_call
    def kanban_init(self, board: str) -> None:
        """Idempotent, like `kanban init`: the fake's board exists from the start."""

    @_controller_call
    def kanban_create(
        self, board: str, title: str, *, assignee: str | None = None, parent: list[str] | None = None,
        workspace: str = "scratch", branch: str | None = None, project: str | None = None,
        body: str | None = None, idempotency_key: str | None = None, max_retries: int | None = None,
        max_runtime: str | None = None, initial_status: str | None = None,
    ) -> dict:
        """kanban_db.create_task through `kanban create`. Idempotent by `idempotency_key` (the newest non-archived card with
        the key comes back and nothing changes). Status: `blocked` for initial_status="blocked", `todo` when any parent is
        not `done` (an archived parent does not count as done at creation), else `ready`. A blocked-at-birth card gets a
        `blocked` event with reason "initial_status" (see the module docstring and `initial_block_event`)."""
        with self._cli(board, "create", title, "--workspace", workspace):
            kind, workspace_path = _parse_workspace(workspace)
            branch_name = _parse_branch(branch or None)  # hermes.kanban_create leaves out a falsy --branch, and --body
            body = body or None
            if branch_name and kind != "worktree":
                raise _Refused("kanban: --branch is only valid with --workspace worktree", 2)
            try:
                runtime = _parse_duration(max_runtime)
            except ValueError as exc:
                raise _Refused(f"kanban: --max-runtime: {exc}", 2) from None
            if max_retries is not None and max_retries < 1:
                raise _Refused(
                    f"kanban: --max-retries must be >= 1 (got {max_retries}); use 1 to trip on the first failure.", 2)
            assignee_c = _canonical(assignee) if assignee else None
            if not title or not title.strip():
                raise ValueError("title is required")
            initial = initial_status or "running"
            if initial not in VALID_INITIAL_STATUSES:
                raise ValueError(f"initial_status must be one of {sorted(VALID_INITIAL_STATUSES)}")
            kind = kind or "scratch"
            if idempotency_key:
                existing = [
                    t for t in self._tasks.values() if t.idempotency_key == idempotency_key and t.status != "archived"
                ]
                if existing:
                    return self._task_dict(existing[-1])
            parents = tuple(dict.fromkeys(p for p in (parent or []) if p))
            missing = [p for p in parents if p not in self._tasks]
            if missing:
                raise ValueError(f"unknown parent task(s): {', '.join(missing)}")
            if initial == "blocked":
                status = "blocked"
            elif any(self._tasks[p].status != "done" for p in parents):
                status = "todo"
            else:
                status = "ready"
            self._task_seq += 1
            task_id = "t_%08x" % (0x1000 + self._task_seq)
            task = _Task(
                id=task_id, seq=self._task_seq, title=title.strip(), body=body, assignee=assignee_c, status=status,
                created_by=self.default_author, created_at=self.now, workspace_kind=kind,
                workspace_path=workspace_path, branch_name=branch_name,
                project_id=(str(project).strip() or None) if project is not None else None,
                max_retries=max_retries, idempotency_key=idempotency_key, max_runtime_seconds=runtime,
            )
            self._tasks[task_id] = task
            self._events[task_id] = []
            self._comments[task_id] = []
            self._runs[task_id] = []
            for p in parents:
                self._links.append((p, task_id))
            self._event(task_id, "created", {
                "assignee": assignee_c, "status": status, "parents": list(parents), "creator_task_id": None,
                "tenant": None, "workspace_kind": kind, "workspace_path": workspace_path,
                "branch_name": branch_name, "project_id": task.project_id, "skills": None, "goal_mode": None,
                "model_override": None, "provider_override": None,
            })
            if status == "blocked" and self.initial_block_event:
                self._event(task_id, "blocked", {"reason": "initial_status", "status": "blocked",
                                                 "actor": self.default_author})
            if status == "todo":
                gating = [p for p in parents if self._tasks[p].status not in ("done", "archived")]
                if gating:
                    self._event(task_id, "dependency_wait", {"reason": "parent_not_done", "parent": gating[0]})
            return self._task_dict(task)

    @_controller_call
    def kanban_show(self, board: str, card_id: str) -> dict:
        with self._cli(board, "show", card_id, "--json"):
            if card_id not in self._tasks:
                raise _Refused(f"no such task: {card_id}")
            return self._show(card_id)

    @_controller_call
    def kanban_list(self, board: str, *, status: str | None = None, assignee: str | None = None) -> list[dict]:
        """`kanban list`, which runs recompute_ready first (so a card whose parents finished shows as ready even before a
        dispatch tick), hides archived cards unless asked for them by status, and orders by priority then creation."""
        status = status or None
        assignee = assignee or None
        args = ["list"]
        if status:
            args += ["--status", status]
        if assignee:
            args += ["--assignee", assignee]
        with self._cli(board, *args, "--json"):
            self._recompute_ready()
            if status is not None and status not in VALID_STATUSES:
                raise ValueError(f"status must be one of {sorted(VALID_STATUSES)}")
            wanted = _canonical(assignee) if assignee else None
            rows = []
            for task in sorted(self._tasks.values(), key=lambda t: (-t.priority, t.seq)):
                if wanted is not None and task.assignee != wanted:
                    continue
                if status is not None and task.status != status:
                    continue
                if status != "archived" and task.status == "archived":
                    continue
                rows.append(self._task_dict(task))
            return rows

    @_controller_call
    def kanban_link(self, board: str, parent_id: str, child_id: str) -> None:
        with self._cli(board, "link", parent_id, child_id):
            self._link_tasks(parent_id, child_id)

    @_controller_call
    def kanban_dispatch(self, board: str, *, dry_run: bool = False, max_spawns: int | None = None) -> dict:
        """`kanban dispatch --json`: one dispatcher tick, returning the dict the CLI prints (reclaimed, crashed, timed_out,
        stale, auto_blocked, promoted, reaped_terminal_workers, spawned [{task_id, assignee, workspace}],
        skipped_unassigned, skipped_nonspawnable, skipped_per_profile_capped, auto_assigned_default, respawn_guarded,
        rate_limited, skipped_locked, memory_pressure). It does NOT honour `pause` unless cli_dispatch_honors_pause is set:
        the CLI path has no pause check, only the gateway's loop does."""
        args = ["dispatch"]
        if dry_run:
            args += ["--dry-run"]
        if max_spawns is not None:
            args += ["--max", str(max_spawns)]
        with self._cli(board, *args, "--json"):
            if self.paused and self.cli_dispatch_honors_pause:
                return self._empty_dispatch_result()
            return self._dispatch(dry_run=dry_run, max_spawn=max_spawns)

    @_controller_call
    def kanban_request_changes(self, board: str, card_id: str, reason: str) -> None:
        with self._cli(board, "request-changes", card_id, reason):
            ok, detail = self._request_changes(card_id, reason=" ".join([reason]).strip(), expected_run_id=None)
            if not ok:
                raise _Refused(f"cannot request changes for {card_id}: {detail or 'invalid review state'}")

    @_controller_call
    def kanban_reopen_review(self, board: str, card_id: str, reason: str) -> None:
        """`kanban reopen-review`: the card goes back to its implementer first, and only then is the reason written as a
        "CHANGES REQUESTED: <reason>" comment (the wrapper's docstring says the comment comes first; the CLI does it after)."""
        with self._cli(board, "reopen-review", card_id, f"--reason={reason}"):
            cleaned = reason.strip() or None
            if not self._reopen_review(card_id):
                raise _Refused(f"cannot reopen {card_id} (not in review?)")
            if cleaned:
                self._add_comment(card_id, self.default_author, f"CHANGES REQUESTED: {cleaned}")

    @_controller_call
    def kanban_complete(
        self, board: str, card_id: str, *, result: str | None = None, metadata: dict | None = None,
    ) -> None:
        args = ["complete", card_id]
        if result:
            args += ["--result", result]
        if metadata is not None:
            args += ["--metadata", json.dumps(metadata)]
        with self._cli(board, *args):
            parsed = json.loads(json.dumps(metadata)) if metadata is not None else None
            if parsed is not None and not isinstance(parsed, dict):
                raise _Refused("kanban: --metadata: must be a JSON object", 2)
            try:
                ok = self._complete_task(
                    card_id, result=result or None, summary=None, metadata=parsed, expected_run_id=None, force=False)
            except _LiveClaim:
                raise _Refused(
                    f"cannot complete {card_id}: a live worker is running it. Wait for the worker, `hermes kanban "
                    f"reclaim {card_id}` to release it, or re-run with --force to close its run and complete anyway."
                ) from None
            if not ok:
                raise _Refused(f"cannot complete {card_id} (unknown id or terminal state)")

    @_controller_call
    def kanban_block(self, board: str, card_id: str, reason: str, *, kind: str | None = None) -> None:
        """`kanban block`: the "BLOCKED: <reason>" comment (by default_author) is written FIRST, then the block is tried. Only
        a `running` or `ready` card can be blocked, so on any other card (an already blocked merge card, a `todo` card) this
        raises AFTER leaving the comment. A second block of the same kind after an unblock routes the card to `triage`
        with a block_loop_detected event (limit 2)."""
        args = ["block"]
        if kind:
            args += ["--kind", kind]
        with self._cli(board, *args, card_id, "--", reason):
            if kind and kind not in VALID_BLOCK_KINDS:
                raise _Refused(
                    f"kanban block: error: argument --kind: invalid choice: {kind!r} (choose from "
                    f"{', '.join(repr(k) for k in sorted(VALID_BLOCK_KINDS))})", 2)
            cleaned = reason.strip() or None
            if cleaned:
                self._add_comment(card_id, self.default_author, f"BLOCKED: {cleaned}")
            if not self._block_task(card_id, reason=cleaned, kind=kind or None, expected_run_id=None):
                raise _Refused(f"cannot block {card_id}")

    @_controller_call
    def kanban_schedule(self, board: str, card_id: str, reason: str) -> None:
        with self._cli(board, "schedule", card_id, reason):
            cleaned = reason.strip() or None
            if cleaned:
                self._add_comment(card_id, self.default_author, f"SCHEDULED: {cleaned}")
            if not self._schedule_task(card_id, reason=cleaned, expected_run_id=None):
                raise _Refused(f"cannot schedule {card_id}")

    @_controller_call
    def kanban_unblock(self, board: str, card_id: str, reason: str | None = None) -> None:
        """`kanban unblock`: with a reason, an "UNBLOCK: <reason>" comment first, then the unblock (which resets the failure
        counter). Only a `blocked` or `scheduled` card can be unblocked; a `triage` card cannot."""
        args = ["unblock", card_id]
        if reason:
            args += [f"--reason={reason}"]
        with self._cli(board, *args):
            cleaned = (reason.strip() or None) if reason is not None else None
            if cleaned:
                self._add_comment(card_id, self.default_author, f"UNBLOCK: {cleaned}")
            if not self._unblock_task(card_id):
                raise _Refused(f"cannot unblock {card_id} (not blocked/scheduled?)")

    @_controller_call
    def kanban_comment(self, board: str, card_id: str, text: str, *, author: str | None = None) -> None:
        args = ["comment"]
        if author:
            args += ["--author", author]
        with self._cli(board, *args, card_id, "--", text):
            self._add_comment(card_id, author or self.default_author, " ".join([text]).strip())

    @_controller_call
    def kanban_promote(self, board: str, card_id: str, reason: str | None = None) -> None:
        args = ["promote", card_id]
        if reason:
            args += ["--", reason]
        with self._cli(board, *args):
            ok, error = self._promote_task(
                card_id, actor=self.default_author, reason=(reason.strip() or None) if reason else None)
            if not ok:
                raise _Refused(f"cannot promote {card_id}: {error}")

    @_controller_call
    def kanban_archive(self, board: str, card_ids: list[str]) -> None:
        if not card_ids:
            return
        with self._cli(board, "archive", *card_ids):
            done, failed = [], []
            for card_id in card_ids:
                if self._archive_task(card_id):
                    done.append(f"Archived {card_id}")
                else:
                    failed.append(f"cannot archive {card_id}")
            if failed:
                raise _Refused("\n".join([*done, *failed]))

    @_controller_call
    def kanban_set_model(self, board: str, card_id: str, model: str | None, *, provider: str | None = None) -> None:
        args = ["set-model"]
        if provider and model:
            args += ["--provider", provider]
        with self._cli(board, *args, card_id, model or "none"):
            cleared = model is None or model.lower() in ("none", "-", "null", "")
            try:
                ok = self._set_model_override(
                    card_id, None if cleared else model, provider if (provider and model) else None)
            except (ValueError, RuntimeError) as exc:
                raise _Refused(f"kanban: {exc}", 2) from None
            if not ok:
                raise _Refused(f"no such task: {card_id}")

    @_controller_call
    def kanban_reclaim(self, board: str, card_id: str, *, reason: str | None = None) -> None:
        args = ["reclaim", card_id]
        if reason:
            args += ["--reason", reason]
        with self._cli(board, *args):
            if not self._reclaim_task(card_id, reason=reason or None):
                raise _Refused(f"cannot reclaim {card_id} (not running or unknown id)")

    @_controller_call
    def pause(self, reason: str | None = None, timeout: int = 20) -> None:
        """`hermes pause`: sets the flag the GATEWAY's dispatcher honours (tick() then does nothing). Running workers finish."""
        self.paused = True
        self.pause_reason = reason

    @_controller_call
    def resume(self, timeout: int = 20) -> None:
        self.paused = False
        self.pause_reason = None

    @_controller_call
    def session_usage(self, profile: str, session_id: str, timeout: int = 60) -> dict | None:
        """What `hermes sessions export` reports for a worker session: the numbers set with set_session_usage, else
        default_session_requests calls for a session a worker of this fake really had, else None (unknown)."""
        if session_id in self._session_usage:
            usage = self._session_usage[session_id]
            return copy.deepcopy(usage) if usage is not None else None
        if session_id in self._issued_sessions:
            return {
                "id": session_id, "model": "", "api_call_count": self.default_session_requests,
                "input_tokens": 1000, "output_tokens": 200,
            }
        return None

    # ---------------------------------------------------------------------------------------
    # The worker side: what a dispatched worker's kanban tools do (tools/kanban_tools.py). A worker names its own
    # run with run_id, like HERMES_KANBAN_RUN_ID, so a worker whose run was reclaimed cannot act on its successor's.
    # ---------------------------------------------------------------------------------------

    def _need(self, card_id: str) -> _Task:
        task = self._tasks.get(card_id)
        if task is None:
            raise AgentToolError(f"task {card_id} not found")
        return task

    def _expected_run(self, task: _Task, run_id: int | None) -> int | None:
        return run_id if run_id is not None else task.current_run_id

    def _stamp(self, task: _Task, run_id: int | None, metadata: dict | None) -> dict | None:
        """kanban_tools._stamp_worker_session_metadata: the worker's session id goes into the metadata it hands off, which
        is what usage.py finds to count the session's requests."""
        rid = self._expected_run(task, run_id)
        if rid is None:
            return metadata
        session = self.session_id_for(task.id, rid)
        self._issued_sessions.add(session)
        return {**(metadata or {}), "worker_session_id": session}

    @_agent_call
    def agent_request_review(
        self, card_id: str, summary: str | None = None, metadata: dict | None = None, reviewer: str | None = None,
        *, run_id: int | None = None,
    ) -> None:
        """kanban_request_review: hand the card off for review. The summary is required; `reviewer` names the profile that
        takes the card (without it the card stays with the implementer)."""
        task = self._need(card_id)
        if not summary or not str(summary).strip():
            raise AgentToolError(
                "summary is required - describe what was implemented and how it was verified so the reviewer has context")
        if metadata is not None and not isinstance(metadata, dict):
            raise AgentToolError(f"metadata must be an object/dict, got {type(metadata).__name__}")
        if reviewer and not self._profile_exists(_canonical(reviewer)):
            raise AgentToolError(
                f"reviewer profile {reviewer!r} is not installed. Installed profiles: "
                f"{', '.join(sorted(set(self._workers) | self.extra_profiles))}")
        stamped = self._stamp(task, run_id, metadata)
        ok, why = self._request_review(
            card_id, summary=summary, metadata=stamped, reviewer=reviewer or None,
            expected_run_id=self._expected_run(task, run_id), force=False)
        if not ok:
            raise AgentToolError(f"could not request review for {card_id}: {why or 'unknown id or not in running/ready'}")

    @_agent_call
    def agent_complete(
        self, card_id: str, summary: str | None = None, metadata: dict | None = None, *, result: str | None = None,
        run_id: int | None = None,
    ) -> None:
        """kanban_complete: finish the card with a structured hand-off (a reviewer's verdict rides here). At least one of
        summary and result is required; the worker's session id is added to the metadata."""
        task = self._need(card_id)
        if not (summary or result):
            raise AgentToolError("provide at least one of: summary (preferred), result")
        if metadata is not None and not isinstance(metadata, dict):
            raise AgentToolError(f"metadata must be an object/dict, got {type(metadata).__name__}")
        stamped = self._stamp(task, run_id, metadata)
        if not self._complete_task(
                card_id, result=result, summary=summary, metadata=stamped,
                expected_run_id=self._expected_run(task, run_id), force=False):
            raise AgentToolError(f"could not complete {card_id} (unknown id, stale run, or already terminal)")

    @_agent_call
    def agent_block(
        self, card_id: str, reason: str, kind: str | None = None, *, run_id: int | None = None,
    ) -> None:
        """kanban_block: block the card with a reason a human will read. Unlike `hermes kanban block` this writes no
        "BLOCKED:" comment, only the run and the `blocked` event (or, from the second same-kind block, triage)."""
        task = self._need(card_id)
        if not reason or not str(reason).strip():
            raise AgentToolError("reason is required - explain what input you need")
        if kind is not None and kind not in VALID_BLOCK_KINDS:
            raise AgentToolError(f"kind must be one of {sorted(VALID_BLOCK_KINDS)} (or omit it)")
        if not self._block_task(
                card_id, reason=reason, kind=kind, expected_run_id=self._expected_run(task, run_id)):
            raise AgentToolError(f"could not block {card_id} (unknown id or not in running/ready)")

    @_agent_call
    def agent_request_changes(self, card_id: str, reason: str, *, run_id: int | None = None) -> None:
        """kanban_request_changes: a reviewer returns the card it is reviewing to its implementer. Takes a reason and no
        metadata (Hermes has none for it)."""
        task = self._need(card_id)
        ok, detail = self._request_changes(
            card_id, reason=reason, expected_run_id=self._expected_run(task, run_id))
        if not ok:
            raise AgentToolError(f"could not request changes for {card_id}: {detail or 'invalid review state'}")

    @_agent_call
    def agent_comment(self, card_id: str, text: str, *, author: str = "worker") -> None:
        """kanban_comment: the author is the worker's own profile, never an argument the model controls."""
        self._need(card_id)
        try:
            self._add_comment(card_id, author, text)
        except ValueError as exc:
            raise AgentToolError(str(exc)) from None

    @_agent_call
    def agent_heartbeat(self, card_id: str, note: str | None = None, *, run_id: int | None = None) -> None:
        """kanban_heartbeat: extend the claim and record a heartbeat event on the live run."""
        task = self._need(card_id)
        rid = self._expected_run(task, run_id)
        if task.status != "running" or task.current_run_id is None or (rid is not None and rid != task.current_run_id):
            raise AgentToolError(f"could not heartbeat {card_id} (unknown id or not running)")
        task.claim_expires = self.now + self.claim_ttl_seconds
        task.last_heartbeat_at = self.now
        run = self._run_by_id[task.current_run_id]
        run.claim_expires = task.claim_expires
        run.last_heartbeat_at = self.now
        self._event(card_id, "heartbeat", {"note": note} if note else None, task.current_run_id)

    @_agent_call
    def agent_hang(self, card_id: str, *, run_id: int | None = None) -> None:
        """The worker gets stuck in a call that never returns: the process stays alive, the card stays `running`, and only a
        timeout, a reclaim or a kill ends it."""
        task = self._need(card_id)
        rid = self._expected_run(task, run_id)
        proc = self._procs.get(rid) if rid is not None else None
        if proc is None or not proc.alive:
            raise AgentToolError(f"{card_id} has no live worker process to hang")
        proc.hung = True

    @_agent_call
    def agent_fail(
        self, card_id: str, error: str | None = None, outcome: str = "crashed", *, run_id: int | None = None,
        exit_code: int | None = None,
    ) -> None:
        """The worker's process dies before any terminal call, and Hermes books it on the spot (a real dispatcher notices at
        its next tick). `outcome` is `crashed` (run error = `error`, else "pid N exited with code C"), `timed_out` ("elapsed
        Ns > limit Ms"), `spawn_failed` (the card returns to its source phase at once) or `rate_limited` (a quota wall: the
        card is re-queued and NO failure is counted). Every outcome but the last increments consecutive_failures and trips
        `gave_up` and `blocked` at max_retries (default 2)."""
        task = self._need(card_id)
        if outcome not in ("crashed", "timed_out", "spawn_failed", "rate_limited"):
            raise ValueError(f"unknown failure outcome {outcome!r}")
        rid = self._expected_run(task, run_id)
        if task.status != "running" or rid is None or task.current_run_id != rid:
            raise AgentToolError(f"{card_id} has no live run {rid} to fail")
        proc = self._procs.get(rid)
        pid = proc.pid if proc is not None else task.worker_pid
        lock = task.claim_lock
        run = self._run_by_id[rid]
        if proc is not None:
            proc.alive = False
            proc.hung = False
            self._drop_deferred(rid)
        if outcome == "spawn_failed":
            if proc is not None:
                proc.exit_kind, proc.exit_code = "nonzero_exit", 1
            self._record_task_failure(
                task, error or "spawn failed", outcome="spawn_failed", failure_limit=self.failure_limit,
                release_claim=True, end_run=True)
            return
        retry_status = self._retry_status_for_run(card_id)
        task.status = retry_status
        task.claim_lock = None
        task.claim_expires = None
        task.worker_pid = None
        if outcome == "crashed":
            code = exit_code if exit_code is not None else 1
            if proc is not None:
                proc.exit_kind, proc.exit_code = "nonzero_exit", code
            text = error or f"pid {pid} exited with code {code}"
            payload = {"pid": pid, "claimer": lock, "retry_status": retry_status}
            if exit_code is not None:
                payload.update(exit_kind="nonzero_exit", exit_code=exit_code)
            run_id_ = self._end_run(task, outcome="crashed", status="crashed", error=text, metadata=dict(payload))
            self._event(card_id, "crashed", payload, run_id_)
            self._record_task_failure(
                task, text, outcome="crashed", event_payload_extra={"pid": pid, "claimer": lock})
        elif outcome == "timed_out":
            if proc is not None:
                proc.exit_kind, proc.exit_code = "signaled", 15
            elapsed = self.now - run.started_at
            limit = int(task.max_runtime_seconds or 0)
            text = error or f"elapsed {int(elapsed)}s > limit {limit}s"
            task.last_heartbeat_at = None
            payload = {
                "pid": pid, "elapsed_seconds": int(elapsed), "limit_seconds": limit, "sigkill": False,
                "retry_status": retry_status,
            }
            run_id_ = self._end_run(task, outcome="timed_out", status="timed_out", error=text, metadata=payload)
            self._event(card_id, "timed_out", payload, run_id_)
            self._record_task_failure(
                task, text, outcome="timed_out",
                event_payload_extra={"pid": pid, "sigkill": False, "retry_status": retry_status})
        else:
            if proc is not None:
                proc.exit_kind, proc.exit_code = "rate_limited", RATE_LIMIT_EXIT_CODE
            text = error or f"pid {pid} exited rate-limited (quota wall) - requeued without counting a failure"
            payload = {"pid": pid, "claimer": lock, "exit_code": RATE_LIMIT_EXIT_CODE, "retry_status": retry_status}
            run_id_ = self._end_run(
                task, outcome="rate_limited", status="rate_limited", error=text, metadata=dict(payload))
            self._event(card_id, "rate_limited", payload, run_id_)
            task.last_failure_error = text[:500]
