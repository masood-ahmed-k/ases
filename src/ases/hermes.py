"""The only module that talks to Hermes (section 9.1: hermes.py; ASES-ARC-04).

Everything else in ASES that needs to know what Hermes is doing goes through here. Phase 1 only needs
read-only status calls (version, doctor, gateway status) -- card creation, kanban queries and worker
dispatch are wired up in Phase 3. Text parsing is used only where Hermes has no --json output for a
given command (confirmed by running --help first, per the blueprint's own instruction to Claude Code);
where JSON exists, later phases must prefer it.
"""
from __future__ import annotations

import dataclasses
import json
import re
import shutil
import subprocess

_VERSION_RE = re.compile(r"Hermes Agent v(\d+\.\d+\.\d+)")


class HermesCommandError(Exception):
    """A `hermes` subcommand exited non-zero. Carries stdout+stderr for the caller to inspect."""

    def __init__(self, args: list[str], returncode: int, output: str):
        self.args = args
        self.returncode = returncode
        self.output = output
        super().__init__(f"hermes {' '.join(args)} exited {returncode}: {output[:500]}")


class HermesNotFound(Exception):
    pass


def hermes_path() -> str:
    path = shutil.which("hermes")
    if not path:
        raise HermesNotFound("`hermes` is not on PATH")
    return path


def _run(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        [hermes_path(), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )


def hermes_version() -> str | None:
    """Returns e.g. '0.21.3', or None if hermes isn't on PATH or the output didn't parse."""
    try:
        result = _run(["--version"], timeout=20)
    except (HermesNotFound, subprocess.TimeoutExpired, FileNotFoundError):
        return None
    match = _VERSION_RE.search(result.stdout)
    return match.group(1) if match else None


@dataclasses.dataclass(frozen=True)
class DoctorResult:
    ok: bool
    exit_code: int | None
    raw_output: str
    warning_lines: tuple[str, ...]
    error_lines: tuple[str, ...]


def run_doctor(timeout: int = 60) -> DoctorResult:
    """Shells out to `hermes doctor`. Hermes has no --json for this command (checked via --help), so we
    fall back to its own pass/fail convention: exit code 0 means healthy. Lines are also scanned for the
    warning (warn) and error (x) glyphs Hermes prints, purely as extra detail for a human reading the
    ASES doctor report -- the exit code is what decides ok, not glyph-counting.
    """
    try:
        result = _run(["doctor"], timeout=timeout)
    except HermesNotFound:
        return DoctorResult(False, None, "hermes not found on PATH", (), ("hermes not found on PATH",))
    except subprocess.TimeoutExpired:
        return DoctorResult(False, None, "hermes doctor timed out", (), ("hermes doctor timed out",))

    raw = result.stdout + result.stderr
    warnings = tuple(line.strip() for line in raw.splitlines() if line.lstrip().startswith(("⚠", "!")))
    errors = tuple(line.strip() for line in raw.splitlines() if line.lstrip().startswith(("✗", "x", "X")))
    return DoctorResult(result.returncode == 0, result.returncode, raw, warnings, errors)


@dataclasses.dataclass(frozen=True)
class GatewayStatus:
    running: bool
    raw_output: str


def gateway_status(timeout: int = 20) -> GatewayStatus:
    try:
        result = _run(["gateway", "status"], timeout=timeout)
    except (HermesNotFound, subprocess.TimeoutExpired):
        return GatewayStatus(False, "hermes gateway status unavailable")
    raw = result.stdout + result.stderr
    running = "not running" not in raw.lower() and "✗" not in raw
    return GatewayStatus(running, raw)


# ---------------------------------------------------------------------------------------------
# Kanban (Phase 3). Every one of these prefers --json (ASES-ARC-04); the two commands that don't
# support it (dispatch's dry-run summary, and plain status changes) fall back to exit-code +
# stdout text, documented per function.
# ---------------------------------------------------------------------------------------------


def _kanban(board: str, args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    result = _run(["kanban", "--board", board, *args], timeout=timeout)
    if result.returncode != 0:
        raise HermesCommandError(["kanban", "--board", board, *args], result.returncode,
                                  result.stdout + result.stderr)
    return result


def _kanban_json(board: str, args: list[str], timeout: int = 30):
    result = _kanban(board, [*args, "--json"], timeout=timeout)
    return json.loads(result.stdout)


def kanban_init(board: str) -> None:
    _kanban(board, ["init"])


def kanban_create(
    board: str, title: str, *, assignee: str | None = None, parent: list[str] | None = None,
    workspace: str = "scratch", branch: str | None = None, project: str | None = None,
    body: str | None = None, idempotency_key: str | None = None, max_retries: int | None = None,
    max_runtime: str | None = None, initial_status: str | None = None,
) -> dict:
    args = ["create", title, "--workspace", workspace]
    if assignee:
        args += ["--assignee", assignee]
    for p in parent or []:
        args += ["--parent", p]
    if branch:
        args += ["--branch", branch]
    if project:
        args += ["--project", project]
    if body:
        args += ["--body", body]
    if idempotency_key:
        args += ["--idempotency-key", idempotency_key]
    if max_retries is not None:
        args += ["--max-retries", str(max_retries)]
    if max_runtime:
        args += ["--max-runtime", max_runtime]
    if initial_status:
        args += ["--initial-status", initial_status]
    return _kanban_json(board, args)


def kanban_show(board: str, card_id: str) -> dict:
    """Returns the flat task dict (same shape as list/create's entries). The raw CLI response wraps
    it as {"task": {...}, "parents": [...], "children": [...], "comments": [...], "events": [...],
    "runs": [...]} -- real shape, confirmed against a live card, not the flat shape this wrongly
    assumed at first (every module that calls kanban_show()["status"] depends on this unwrap)."""
    raw = _kanban_json(board, ["show", card_id])
    task = dict(raw["task"])
    task["_children"] = raw.get("children", [])
    task["_parents"] = raw.get("parents", [])
    task["_runs"] = raw.get("runs", [])
    return task


def kanban_list(board: str, *, status: str | None = None, assignee: str | None = None) -> list[dict]:
    args = ["list"]
    if status:
        args += ["--status", status]
    if assignee:
        args += ["--assignee", assignee]
    return _kanban_json(board, args)


def kanban_link(board: str, parent_id: str, child_id: str) -> None:
    _kanban(board, ["link", parent_id, child_id])


def kanban_dispatch(board: str, *, dry_run: bool = False, max_spawns: int | None = None) -> dict:
    args = ["dispatch"]
    if dry_run:
        args += ["--dry-run"]
    if max_spawns is not None:
        args += ["--max", str(max_spawns)]
    return _kanban_json(board, args)


def kanban_request_changes(board: str, card_id: str, reason: str) -> None:
    _kanban(board, ["request-changes", card_id, reason])


def kanban_complete(board: str, card_id: str, *, result: str | None = None, metadata: dict | None = None) -> None:
    args = ["complete", card_id]
    if result:
        args += ["--result", result]
    if metadata is not None:
        args += ["--metadata", json.dumps(metadata)]
    _kanban(board, args)


def kanban_block(board: str, card_id: str, reason: str) -> None:
    _kanban(board, ["block", card_id, reason])


def kanban_reclaim(board: str, card_id: str, *, reason: str | None = None) -> None:
    args = ["reclaim", card_id]
    if reason:
        args += ["--reason", reason]
    _kanban(board, args)


def pause(reason: str | None = None, timeout: int = 20) -> None:
    """ASES-REC-06: halts NEW dispatch/cron/gateway turns. Never kills work already in flight --
    that's Hermes's own documented behavior for `hermes pause`, not a gap here."""
    args = ["pause"]
    if reason:
        args += ["--reason", reason]
    result = _run(args, timeout=timeout)
    if result.returncode != 0:
        raise HermesCommandError(args, result.returncode, result.stdout + result.stderr)


def resume(timeout: int = 20) -> None:
    result = _run(["resume"], timeout=timeout)
    if result.returncode != 0:
        raise HermesCommandError(["resume"], result.returncode, result.stdout + result.stderr)
