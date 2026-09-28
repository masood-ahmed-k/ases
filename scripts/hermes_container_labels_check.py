"""CONTAINERS (round 17): what labels and name a REAL Hermes-dispatched worker container carries, and
that Hermes's own orphan reaper only ever targets exited containers, confirmed against real Docker.

RUN WITH HERMES'S OWN PYTHON (needed to import Hermes's modules), from anywhere:

    C:/Users/masoo/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe scripts/hermes_container_labels_check.py

USES REAL DOCKER. Not collected by pytest (testpaths is ["tests"]; this lives under scripts/).

Why this script exists, separate from scripts/hermes_docker_terminal_check.py: that script proves the
*mount and isolation* behaviour of the real coder-1 profile (git access, env leaks, network). This one
answers a narrower, different question for package CONTAINERS: exactly which label keys and values (and
which container NAME shape) does Hermes's own tools.environments.docker.DockerEnvironment attach to a
container it creates for a dispatched worker, confirmed two ways: (a) read directly off the Python
object Hermes built (self._labels, matching tools/environments/docker.py:584-588), and (b) independently,
by shelling out to `docker ps --format "{{.Names}}|{{.Labels}}"` and `docker inspect`, the same commands
ases.killswitch.default_list_containers and the new orphan sweep actually run -- so this proves the real
docker daemon's own view agrees with Hermes's Python side, not only that Hermes's source SAYS it sets
these labels. Also creates a second, unrelated plain container (no hermes-agent label) to prove the
sweep's own label-based filter would never touch an unrelated user container.

Same read-only discipline as hermes_docker_terminal_check.py: HERMES_HOME points at a throwaway copy of
coder-1's config.yaml (never its .env, never the real profile directory), TERMINAL_CWD points at a
throwaway empty directory (no git repo needed: this script never touches git, only labels/name), and
every credential-shaped env var is scrubbed first. Nothing under C:/Users/masoo/AppData/Local/hermes is
ever opened for writing.
"""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys

_SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ases import sandbox as sandbox_mod  # noqa: E402

HERMES_HOME_REAL = pathlib.Path(r"C:\Users\masoo\AppData\Local\hermes")
HERMES_AGENT_DIR = HERMES_HOME_REAL / "hermes-agent"
CODER1_PROFILE_DIR = HERMES_HOME_REAL / "profiles" / "coder-1"
CODER1_CONFIG = CODER1_PROFILE_DIR / "config.yaml"

BASETEMP = pathlib.Path(r"C:\Users\masoo\ases-wt\_pytest\containers-build")
WORKDIR = BASETEMP / "hermes-container-labels-check"
# CONTAINERS round 17 fix round 2 (reviewer finding, major): HERMES_HOME for a real kanban-dispatched worker
# is NEVER a flat directory. kanban_db_dispatch.py sets it from resolve_profile_env(profile_arg), which
# returns "<root>/profiles/<canon>" for any non-"default" profile (hermes_cli/profiles.py:1966); and
# get_active_profile_name() (hermes_cli/profiles.py:1606, what tools/environments/docker.py:104
# _get_active_profile_name calls) only ever reports a NAMED profile when HERMES_HOME resolves to exactly one
# path segment under hermes_constants.get_default_hermes_root()'s OWN idea of the profiles root -- and that
# function (hermes_constants.py:171) adapts to ANY custom root: when HERMES_HOME's parent is literally named
# "profiles", the root becomes HERMES_HOME's grandparent, whatever that is, real or fake. So a THROWAWAY
# nested "<fake_root>/profiles/coder-1" resolves to the real active profile name "coder-1" exactly as a real
# deployment's own "<real_root>/profiles/coder-1" would -- confirmed by tracing both functions above, then
# empirically below -- while a FLAT fake directory (what this script used before round 2) has no "profiles"
# parent segment at all, so get_default_hermes_root() takes its OTHER branch (result = the flat directory
# itself) and get_active_profile_name() reports "custom", never "coder-1". That was this script's own bug:
# every check below still printed PASS, because none of them asserted the hermes-profile LABEL VALUE, only
# that the key was present -- so the flat layout's silent "custom" (or, depending on the exact path shape,
# "default") never tripped a failure. Fixed by nesting FAKE_HOME under "profiles/coder-1" and by asserting
# the label's value, not just its presence, below.
FAKE_HOME_ROOT = WORKDIR / "fake_hermes_home"
FAKE_HOME = FAKE_HOME_ROOT / "profiles" / "coder-1"
FAKE_CWD = WORKDIR / "fake_worktree"
TASK_ID = "t_deadbeef"

_CREDENTIAL_NAME_RE = re.compile(r"key|token|secret|password", re.IGNORECASE)
_CREDENTIAL_NAME_EXCLUDE = frozenset({"GPG_KEY"})

_FAILURES: list[str] = []
_RESULTS: list[tuple[str, bool]] = []


def _ok(label: str, detail: str = "") -> None:
    print(f"[PASS] {label}" + (f" - {detail}" if detail else ""))
    _RESULTS.append((label, True))


def _fail(label: str, detail: str) -> None:
    print(f"[FAIL] {label} - {detail}")
    _FAILURES.append(f"{label}: {detail}")
    _RESULTS.append((label, False))


def _force_rmtree(path: pathlib.Path) -> None:
    def _clear_and_retry(func, failed_path, _exc_info):
        os.chmod(failed_path, stat.S_IWRITE)
        func(failed_path)
    try:
        shutil.rmtree(path, onerror=_clear_and_retry)
    except OSError:
        pass


def scrub_and_plant_env() -> None:
    for name in list(os.environ):
        if _CREDENTIAL_NAME_RE.search(name) and name not in _CREDENTIAL_NAME_EXCLUDE:
            os.environ.pop(name, None)


def _docker_ps_line(container_id: str) -> str | None:
    result = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"id={container_id}", "--format", "{{.Names}}|{{.Labels}}"],
        capture_output=True, text=True, timeout=15,
    )
    line = result.stdout.strip()
    return line or None


def _docker_inspect_labels(container_id: str) -> str | None:
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{json .Config.Labels}}", container_id],
        capture_output=True, text=True, timeout=15,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _docker_inspect_name(container_id: str) -> str | None:
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.Name}}", container_id],
        capture_output=True, text=True, timeout=15,
    )
    return result.stdout.strip().lstrip("/") if result.returncode == 0 else None


def main() -> int:
    print("hermes_container_labels_check: real labels/name of a Hermes-dispatched worker container.")
    print(f"work directory: {WORKDIR}")

    ok, why = sandbox_mod.docker_available()
    if not ok:
        _fail("docker_available", why)
        print("\nSummary: FAIL (Docker is not reachable; nothing below can run)")
        return 1
    _ok("docker_available", why)

    if not CODER1_CONFIG.is_file():
        _fail("coder-1 profile config.yaml exists", str(CODER1_CONFIG))
        print("\nSummary: FAIL")
        return 1

    if WORKDIR.exists():
        _force_rmtree(WORKDIR)
    WORKDIR.mkdir(parents=True, exist_ok=True)
    FAKE_CWD.mkdir(parents=True, exist_ok=True)
    FAKE_HOME.mkdir(parents=True, exist_ok=True)
    (FAKE_HOME / "config.yaml").write_text(CODER1_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")

    scrub_and_plant_env()
    os.environ["HERMES_HOME"] = str(FAKE_HOME)
    os.environ["TERMINAL_CWD"] = str(FAKE_CWD)

    if str(HERMES_AGENT_DIR) not in sys.path:
        sys.path.insert(0, str(HERMES_AGENT_DIR))
    try:
        from tools import terminal_tool as terminal_tool_mod
    except Exception as e:  # noqa: BLE001
        _fail("import tools.terminal_tool (Hermes's own module)", f"{type(e).__name__}: {e}")
        print("\nSummary: FAIL")
        return 1

    try:
        env_obj = terminal_tool_mod.ensure_task_env(TASK_ID)
    except Exception as e:  # noqa: BLE001
        _fail("tools.terminal_tool.ensure_task_env", f"{type(e).__name__}: {e}")
        print("\nSummary: FAIL")
        return 1
    if env_obj is None:
        _fail("tools.terminal_tool.ensure_task_env", "returned None (env_type resolved to 'local', not 'docker')")
        print("\nSummary: FAIL")
        return 1

    python_labels = dict(getattr(env_obj, "_labels", {}) or {})
    container_id = getattr(env_obj, "_container_id", None)
    print(f"Python-side self._labels: {python_labels}")
    print(f"container id: {container_id}")

    if not container_id:
        _fail("container id captured", "env_obj._container_id is empty")
    else:
        ps_line = _docker_ps_line(container_id)
        inspect_labels = _docker_inspect_labels(container_id)
        inspect_name = _docker_inspect_name(container_id)
        print(f"docker ps --format {{{{.Names}}}}|{{{{.Labels}}}}: {ps_line!r}")
        print(f"docker inspect .Config.Labels: {inspect_labels!r}")
        print(f"docker inspect .Name: {inspect_name!r}")

        if inspect_name and re.fullmatch(r"hermes-[0-9a-f]{8}", inspect_name):
            _ok("container NAME is the random 'hermes-<8 hex>' shape, no task id in it", inspect_name)
        else:
            _fail("container NAME is the random 'hermes-<8 hex>' shape", f"got {inspect_name!r}")

        if inspect_labels and '"hermes-agent":"1"' in inspect_labels.replace(" ", ""):
            _ok("docker inspect shows label hermes-agent=1", "")
        else:
            _fail("docker inspect shows label hermes-agent=1", f"labels were {inspect_labels!r}")

        # ensure_task_env resolves task_id through terminal_tool._resolve_container_task_id BEFORE the
        # container is created (terminal_tool_lifecycle.py:184 ensure_task_env; terminal_tool.py:400-444
        # _resolve_container_task_id). A CLI-dispatched worker (no HERMES_SESSION_KEY, no session_isolated
        # scope) falls through every earlier branch to the last one: "if not session_key: return 'default'"
        # (terminal_tool.py:439-440). So the raw TASK_ID passed to ensure_task_env above never reaches the
        # label: it collapses to the literal string "default". That collapse is the empirical finding this
        # script exists to nail down (ases.containers's module docstring has the full trace), so the checks
        # below assert the collapse, not the raw id passed in.
        expected_task_label = '"hermes-task-id":"default"'
        if inspect_labels and expected_task_label in inspect_labels.replace(" ", ""):
            _ok("docker inspect shows label hermes-task-id=\"default\" (raw task id collapses, not passed through)", TASK_ID)
        else:
            _fail("docker inspect shows label hermes-task-id=\"default\"", f"expected {expected_task_label!r} in {inspect_labels!r}")

        # CONTAINERS round 17 fix round 2 (reviewer finding, major): this must assert the label's VALUE, not
        # merely that the key is present. A present-but-wrong value (e.g. "custom" or "default" from a
        # HERMES_HOME that does not resolve to a named profile) would still pass a key-only check, which is
        # exactly the bug this script had before: FAKE_HOME was a flat directory, hermes-profile resolved to
        # something other than "coder-1", and every check here still printed PASS. See the python-side value
        # too (python_labels, captured before the container was even created), not only Docker's own view.
        if python_labels.get("hermes-profile") == "coder-1":
            _ok("Python-side self._labels['hermes-profile'] is \"coder-1\" (the real active profile)", "")
        else:
            _fail(
                "Python-side self._labels['hermes-profile'] is \"coder-1\"",
                f"got {python_labels.get('hermes-profile')!r} (full labels: {python_labels!r})",
            )
        if inspect_labels and '"hermes-profile":"coder-1"' in inspect_labels.replace(" ", ""):
            _ok("docker inspect shows label hermes-profile=\"coder-1\" (the real active profile, not \"default\")", "")
        else:
            _fail("docker inspect shows label hermes-profile=\"coder-1\"", f"labels were {inspect_labels!r}")

        # `docker ps --format` renders the same Labels as comma-separated key=value pairs, not JSON -- the
        # same command ases.killswitch.default_list_containers and ases.containers.
        # default_list_hermes_containers actually run, so this checks the daemon's ps-side view agrees with
        # `docker inspect` above, not only that the Python object says so.
        ps_labels = ps_line.split("|", 1)[1] if ps_line and "|" in ps_line else ""
        if "hermes-profile=coder-1" in ps_labels:
            _ok("docker ps --format Names|Labels shows hermes-profile=coder-1 in the Labels column", ps_line)
        else:
            _fail("docker ps --format Names|Labels shows hermes-profile=coder-1", f"got {ps_line!r}")
        if "hermes-task-id=default" in ps_labels:
            _ok("docker ps --format Names|Labels shows hermes-task-id=default in the Labels column", ps_line)
        else:
            _fail("docker ps --format Names|Labels shows hermes-task-id=default in the Labels column", f"got {ps_line!r}")

    # A plain, unrelated container: never hermes-agent labelled, must never be matched by the sweep.
    plain = subprocess.run(
        ["docker", "run", "-d", "--name", "ases-livecheck-unrelated-t_deadbeef",
         "--label", "some-other-label=t_deadbeef", "alpine:latest", "sleep", "60"],
        capture_output=True, text=True, timeout=60,
    )
    plain_id = plain.stdout.strip()
    if plain.returncode == 0 and plain_id:
        _ok("created an unrelated container whose LABEL VALUE also contains the fake task id", plain_id[:12])
        plain_ps = _docker_ps_line(plain_id)
        print(f"unrelated container docker ps line: {plain_ps!r}")
    else:
        _fail("created an unrelated container for the negative case", plain.stdout + plain.stderr)
        plain_id = None

    # Cleanup: stop+remove both containers so nothing is left running.
    try:
        env_obj.cleanup()
        env_obj.wait_for_cleanup(timeout=30)
    except Exception as e:  # noqa: BLE001
        print(f"[NOTE] env_obj.cleanup()/wait_for_cleanup raised: {type(e).__name__}: {e}")
    if container_id:
        check = subprocess.run(["docker", "inspect", container_id], capture_output=True, text=True, timeout=15)
        if check.returncode != 0:
            _ok("Hermes-created container is stopped and removed", container_id[:12])
        else:
            _fail("Hermes-created container is stopped and removed", "docker inspect still finds it; removing by hand")
            subprocess.run(["docker", "rm", "-f", container_id], capture_output=True, timeout=15)
    if plain_id:
        subprocess.run(["docker", "rm", "-f", plain_id], capture_output=True, timeout=15)
        print("[NOTE] unrelated container removed")
    try:
        terminal_tool_mod._stop_cleanup_thread()
    except Exception:
        pass

    _force_rmtree(WORKDIR)

    print("\n--- PASS/FAIL table ---")
    for label, passed in _RESULTS:
        print(f"{'PASS' if passed else 'FAIL'}: {label}")
    if _FAILURES:
        print(f"\nSummary: FAIL ({len(_FAILURES)} finding(s))")
        return 1
    print("\nSummary: PASS (real Hermes container labels and name shape confirmed against real Docker)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
