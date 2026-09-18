"""The only module that talks to Hermes (section 9.1: hermes.py; ASES-ARC-04).

Everything else in ASES that needs to know what Hermes is doing goes through here. Phase 1 only needs
read-only status calls (version, doctor, gateway status) -- card creation, kanban queries and worker
dispatch are wired up in Phase 3. Text parsing is used only where Hermes has no --json output for a
given command (confirmed by running --help first, per the blueprint's own instruction to Claude Code);
where JSON exists, later phases must prefer it.
"""
from __future__ import annotations

import dataclasses
import re
import shutil
import subprocess

_VERSION_RE = re.compile(r"Hermes Agent v(\d+\.\d+\.\d+)")


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
