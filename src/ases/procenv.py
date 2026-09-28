"""The environment ASES starts a subprocess with (ASES-CFG-05; ASES-SEC-01 for model-written code).

Blueprint 10.2: "Never export provider keys in the shell that launches the gateway or the controller". Nothing stops
a person doing it anyway, so every process ASES starts on Hermes's behalf (hermes.py's `_run`, which every hermes
subcommand goes through, and the evaluation harness's one-shot call in evals.py), every model-written program
evalkit.codeeval runs, and (round 8, ASES-CFG-04/ASES-CFG-05) every gate command gates.py's host runner starts and
every git subprocess run_gate's own worktree checkout starts, gets a copy of the current environment with the
credential-shaped variables removed. This is the project's ONE definition of "credential-shaped", so the callers
never drift apart.

Its own module, and not a public function of hermes.py, on purpose: hermes.py's public names are the Hermes call
surface, which ases.fakes.board.FakeHermes replaces wholesale in tests (and test_fakes checks that every public
function there has a fake), and an environment scrub is not a Hermes call. Free of ASES imports, so the lowest
module that starts a process can import it.

`_EXEMPT_EXACT_NAMES` (round 8, architect decision): a small, named, documented set of exact variable names kept
regardless of matching the credential-shaped pattern. `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL` and `GIT_AUTHOR_DATE`
match only because "AUTHOR" contains "auth"; they carry no secret and grant no capability, and a gate command that
makes a commit (common in projects that test git tooling) can need them. This is deliberately NOT the place for
`SSH_AUTH_SOCK` or `XAUTHORITY`: both are capability-bearing (an ssh-agent socket lets model-written code
authenticate as the operator), so scrubbing them is correct and neither gets an exemption. `SESSIONNAME` is left
as is too: it matches on "session" and stays dropped, exactly as before this exemption existed. The exemption
applies to every caller of `scrubbed_environ()`.

`kill_process_tree` (round 13, package TIDY): the one "stop a process and everything it started" implementation,
which gates.py's and evals.py's own `_kill_process_tree` both delegate to. Round 12's GATEINFRA added gates.py's
copy as its own function because that package did not own evals.py, which already had the older one; this module
is the natural home for the merge, since it is already "how ASES starts a subprocess" and, like the rest of this
file, imports nothing else from ASES, so both callers can import it with no cycle.
"""
from __future__ import annotations

import os
import re
import subprocess

_CREDENTIAL_ENV = re.compile(r"(key|token|secret|passw|credential|auth|cookie|session)", re.IGNORECASE)

# Exact names (compared case-insensitively, same as _CREDENTIAL_ENV) exempt from the pattern above even though
# they match it. See the module docstring for why each one is here, and why SSH_AUTH_SOCK/XAUTHORITY are not.
_EXEMPT_EXACT_NAMES = frozenset({"GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE"})


def scrubbed_environ() -> dict[str, str]:
    """A copy of the current environment without any variable whose name looks like a credential (key, token,
    secret, passw, credential, auth, cookie, session; case does not matter), except the exact names in
    _EXEMPT_EXACT_NAMES (round 8: GIT_AUTHOR_NAME/EMAIL/DATE, which match the pattern only on "auth" and carry no
    secret; see the module docstring). Everything else (PATH, SYSTEMROOT, the profile and temp directories,
    Python's own settings) is kept exactly as it is, so a subprocess launches as it did before; a caller adds what
    it needs on top of the copy."""
    return {
        name: value for name, value in os.environ.items()
        if name.upper() in _EXEMPT_EXACT_NAMES or not _CREDENTIAL_ENV.search(name)
    }


def kill_process_tree(pid: int, *, is_windows: bool, process_group: bool = False) -> None:
    """Stop `pid` and everything it started, best effort, never raises: the one implementation gates.py's and
    evals.py's own `_kill_process_tree` both delegate to (round 13, TIDY; each keeps its own thin wrapper, so a
    test can still flip that module's own `_IS_WINDOWS` and see it here, which is why `is_windows` is a required
    argument rather than this module reading `os.name` for itself).

    Windows (`is_windows=True`, both callers, always the same): `Popen.kill()`/`terminate()` (TerminateProcess)
    only ends the ONE process a caller holds a handle to -- gates.py's `_run_commands` runs each command with
    `shell=True`, so its `pid` is only the cmd.exe wrapper, and evals.py's `_run_process` starts a Hermes launcher
    that spawns the real model-call process -- never a grandchild that inherited the same stdout/stderr pipe
    handles. `taskkill /PID <pid> /T /F` ends the whole tree instead, so both callers use it here and
    `process_group` makes no difference on this branch.

    POSIX: the two callers start their subprocess differently, and each needs the POSIX behaviour it already had
    before this round, kept exactly:
      - gates.py's `_run_commands` starts its host command with `start_new_session=True`, which makes the new
        session's process group id equal to the child's own pid, so `os.killpg(pid, ...)` ends that whole group
        (the shell and everything it spawned). Pass `process_group=True` for this caller.
      - evals.py's `_run_process` starts the Hermes launcher in the caller's own session (no
        `start_new_session`), so its pid is not, in general, a process group leader: `os.killpg` there could
        raise or reach the wrong group. `os.kill(pid, ...)` on the one pid is what this caller has always done,
        and is what `process_group=False` (the default) keeps doing.

    Every caller takes this (or its own thin wrapper around it) as an injectable argument, so a test never has to
    touch a real process to prove the branch; a real child-plus-grandchild is proven once, directly, in
    test_procenv.py."""
    try:
        if is_windows:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=30)
        elif process_group:
            import signal

            os.killpg(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        else:
            import signal

            os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    except (OSError, subprocess.SubprocessError):
        pass
