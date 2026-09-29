"""R1 (pre_tool_call shell hook): vetoes write_file, patch, skill_manage, kanban_request_review, kanban_unblock
and kanban_link for a reviewer profile (blueprint ASES-ROL-05, ASES-ROL-06; design (a)/(b),
C:/Users/masoo/ases-wt/_research/r19/REVIEWER.md).

Runs as a Hermes shell hook (agent/shell_hooks.py): a NEW subprocess Hermes spawns for every matching
pre_tool_call, under WHATEVER Python the profile's config.yaml `hooks:` command names, with stdin
{hook_event_name, tool_name, tool_input, session_id, cwd, profile, extra} (shell_hooks.py:1-5, :79-94) and no
guarantee that the ASES package is importable the ordinary way. profiles.py writes the command with
sys.executable (the interpreter that ran `swarm init`), which is normally the ASES venv's own python -- but
this script never RELIES on that: `_ases_src()` below resolves the `src` directory from this file's own path on
disk, not from sys.path, PYTHONPATH or an active venv, so the import still works even if the command is ever
changed to a bare `python` resolved from PATH, or Hermes is moved to a machine where no venv is active for it.

Contract (verified against Hermes 0.21.3 source, agent/shell_hooks.py, read 2026-09-29, HEAD c661785f872b):
  - exit code 2 blocks the tool call (BLOCK_EXIT_CODE, shell_hooks.py:42, :365-371); the message is read from
    stdout JSON {"action": "block", "message": ...} if present, ELSE stderr trimmed to 400 chars (:369) -- this
    script writes only to stderr and prints nothing on stdout.
  - fail_closed: true (this hook's own config.yaml entry, profiles.py) turns a hook that could not even be
    spawned, or that timed out, into a block too (:346-361) -- but NEVER a hook that exits normally with EMPTY
    stdout (:372-381 only forces a block on fail_closed when stdout is non-empty and not parseable JSON): a
    normal exit with nothing printed reads as "no verdict", i.e. ALLOWED, whatever fail_closed says. That is
    exactly why `main()` below has no early return and no bare `raise`: every path ends in `_block()`, and any
    exception becomes a block with a generic message instead of a silent, un-blocked crash.
  - the matcher (agent/shell_hooks.py:124, reviewcontract.DENY_MATCHER) is `re.fullmatch` against the bare tool
    name, so this hook is ONLY ever invoked for a name in reviewcontract.DENIED_TOOLS. There is deliberately no
    "should I allow this?" branch: if this script runs at all, the tool is one the reviewer must never call, so
    it always blocks. Reading tool_name back out of stdin only makes the message name the actual tool; it never
    decides whether to block.
"""
from __future__ import annotations

import json
import pathlib
import sys


def _ases_src() -> str:
    """The `src` directory that holds the `ases` package: src/ases/hooks/deny_tool.py -> parents[2] is `src`.
    Computed from THIS FILE's own location every time, never cached and never read from an environment
    variable, so a copy of the ASES checkout at a different path still resolves correctly."""
    return str(pathlib.Path(__file__).resolve().parents[2])


def _tool_name() -> str:
    """The tool_name stdin named, or "" if stdin cannot be read or parsed at all. Never raises."""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return ""
    return str(payload.get("tool_name") or "") if isinstance(payload, dict) else ""


def _block(message: str) -> int:
    sys.stderr.write(message + "\n")
    return 2


def main() -> int:
    tool_name = _tool_name()
    try:
        sys.path.insert(0, _ases_src())
        from ases import reviewcontract
        message = reviewcontract.deny_message(tool_name)
    except Exception:
        # The import or the message lookup failed (a moved package, a syntax error in a future edit to
        # reviewcontract.py): still block, with a message that depends on nothing that just failed. See the
        # module docstring for why this script must never exit any other way.
        message = "ASES-ROL-05/ASES-ROL-06: this tool is not available to the reviewer."
    return _block(message)


if __name__ == "__main__":
    sys.exit(main())
