"""CONTAINERS (round 17): ases.containers.sweep_orphan_containers proven against REAL Docker containers
carrying Hermes's real container labels.

RUN WITH THE PROJECT'S OWN PYTHON, from anywhere:

    C:/Users/masoo/ases/.venv/Scripts/python.exe scripts/orphan_sweep_live_check.py

USES REAL DOCKER. Not collected by pytest (testpaths is ["tests"]; this lives under scripts/).

What this proves, that the fake-hook unit tests (tests/unit/test_containers.py) cannot: that
default_list_hermes_containers's `docker ps --filter label=hermes-agent=1 --format
{{.Names}}|{{.Label "hermes-profile"}}` and killswitch.default_stop_container's `docker stop -t 5` really do
what ases.containers assumes against a REAL docker daemon, on containers that carry the exact label shape
confirmed real by scripts/hermes_container_labels_check.py (hermes-agent=1, hermes-task-id, hermes-profile,
hermes-egress) -- built directly with `docker run --label ...` here rather than through Hermes's own code
again, since that shape is already proven; this script's own job is ases.containers, not Hermes.

Two containers, two fake profiles of one fake project, a fake kanban_list (no real Hermes board needed: the
orphan sweep only ever reads hermes.kanban_list's answer, which is exactly what "a fake board" means here):
  - profile CODER_PROFILE: no card of this fake project is running under it -> its container is an orphan,
    swept (stopped) by sweep_orphan_containers.
  - profile REVIEWER_PROFILE: a card IS running under it right now -> its container must be left alone,
    proven still running afterward.

Real sqlite (ases.db.connect) so the orphan_container_stopped event is a real row, read back and checked.
Cleans up both containers (stop + rm) whatever happens, and is safe to run twice in a row (each run creates
its own two containers by a random suffix, never touches another run's).
"""
from __future__ import annotations

import pathlib
import subprocess
import sys
import uuid

_SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ases import config as config_mod  # noqa: E402
from ases import containers as containers_mod  # noqa: E402
from ases import db as db_mod  # noqa: E402
from ases import events as events_mod  # noqa: E402
from ases import killswitch as killswitch_mod  # noqa: E402
from ases import sandbox as sandbox_mod  # noqa: E402

BASETEMP = pathlib.Path(r"C:\Users\masoo\ases-wt\_pytest\containers-build")
WORKDIR = BASETEMP / "orphan-sweep-live-check"
IMAGE = "alpine:latest"

RUN_ID = uuid.uuid4().hex[:8]
PROJECT_NAME = f"orphan-livecheck-{RUN_ID}"
CODER_PROFILE = f"coder-livecheck-{RUN_ID}"
REVIEWER_PROFILE = f"reviewer-livecheck-{RUN_ID}"
OTHER_PROFILE = f"not-this-project-livecheck-{RUN_ID}"
BOARD = f"b-{RUN_ID}"

_FAILURES: list[str] = []
_RESULTS: list[tuple[str, bool]] = []


def _ok(label: str, detail: str = "") -> None:
    print(f"[PASS] {label}" + (f" - {detail}" if detail else ""))
    _RESULTS.append((label, True))


def _fail(label: str, detail: str) -> None:
    print(f"[FAIL] {label} - {detail}")
    _FAILURES.append(f"{label}: {detail}")
    _RESULTS.append((label, False))


def _docker(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def _make_container(profile: str, task_label: str = "default") -> str | None:
    """`docker run -d` with exactly the four labels scripts/hermes_container_labels_check.py confirmed real
    (tools/environments/docker.py:584-588 in the installed Hermes source), for `profile`. Returns the
    container id, or None on failure."""
    name = f"hermes-{uuid.uuid4().hex[:8]}"
    result = _docker(
        "run", "-d", "--name", name,
        "--label", "hermes-agent=1",
        "--label", f"hermes-task-id={task_label}",
        "--label", f"hermes-profile={profile}",
        "--label", "hermes-egress=off",
        IMAGE, "sleep", "300",
        timeout=60,
    )
    if result.returncode != 0:
        _fail(f"create container for profile {profile}", result.stdout + result.stderr)
        return None
    container_id = result.stdout.strip()
    _ok(f"created container for profile {profile}", f"{name} ({container_id[:12]})")
    return container_id


def _is_running(container_id: str) -> bool | None:
    result = _docker("inspect", "--format", "{{.State.Running}}", container_id)
    if result.returncode != 0:
        return None
    return result.stdout.strip() == "true"


def _name(container_id: str) -> str:
    result = _docker("inspect", "--format", "{{.Name}}", container_id)
    return result.stdout.strip().lstrip("/") if result.returncode == 0 else ""


def _remove(container_id: str | None) -> None:
    if container_id:
        _docker("rm", "-f", container_id, timeout=20)


def _fake_kanban_list(board: str, *, status: str | None = None):
    assert board == BOARD
    assert status == "running"
    # Only REVIEWER_PROFILE has a card running right now; CODER_PROFILE has none (its card is "done").
    return [{"id": "t_running_card", "assignee": REVIEWER_PROFILE, "status": "running"}]


def main() -> int:
    print(f"orphan_sweep_live_check: run {RUN_ID}")

    ok, why = sandbox_mod.docker_available()
    if not ok:
        _fail("docker_available", why)
        print("\nSummary: FAIL (Docker is not reachable; nothing below can run)")
        return 1
    _ok("docker_available", why)

    WORKDIR.mkdir(parents=True, exist_ok=True)
    db_path = WORKDIR / f"ases-{RUN_ID}.db"
    if db_path.exists():
        db_path.unlink()
    conn = db_mod.connect(db_path)

    project = config_mod.ProjectConfig(
        name=PROJECT_NAME, environment="native", data_class="public",
        workspace_root=WORKDIR / "ws", ases_home=WORKDIR / "home", board=BOARD, integration_branch="integration",
        roles={"lead": "lead", "coder": CODER_PROFILE, "reviewer": REVIEWER_PROFILE},
        concurrency={}, budgets={}, hermes_tested_version="0.21.3", hermes_native_home=WORKDIR / "hermes",
        sandbox={"enabled": True},
    )

    orphan_id = _make_container(CODER_PROFILE)
    running_id = _make_container(REVIEWER_PROFILE)
    if not orphan_id or not running_id:
        _remove(orphan_id)
        _remove(running_id)
        print("\nSummary: FAIL (container setup did not succeed; see above)")
        return 1

    try:
        before_orphan = _is_running(orphan_id)
        before_running = _is_running(running_id)
        if before_orphan is True and before_running is True:
            _ok("both containers are Up before the sweep", "")
        else:
            _fail("both containers are Up before the sweep", f"orphan Running={before_orphan} running Running={before_running}")

        report = containers_mod.sweep_orphan_containers(BOARD, project, conn, kanban_list=_fake_kanban_list)
        print(f"OrphanSweepReport: stopped={report.stopped} notes={report.notes}")

        orphan_name_seen = any(name.startswith("hermes-") for name in report.stopped)
        if len(report.stopped) == 1 and orphan_name_seen:
            _ok("sweep report names exactly one stopped container", report.stopped[0])
        else:
            _fail("sweep report names exactly one stopped container", f"stopped={report.stopped} notes={report.notes}")

        after_orphan = _is_running(orphan_id)
        after_running = _is_running(running_id)
        if after_orphan is False:
            _ok("the orphan container (no card running under its profile) is now stopped", "")
        else:
            _fail("the orphan container (no card running under its profile) is now stopped", f"State.Running={after_orphan}")
        if after_running is True:
            _ok("the running-card's container was left alone (still Up)", "")
        else:
            _fail("the running-card's container was left alone (still Up)", f"State.Running={after_running}")

        rows = [dict(r) for r in conn.execute(
            "SELECT kind, payload, project FROM events WHERE kind = 'orphan_container_stopped'",
        ).fetchall()]
        import json
        matched = [r for r in rows if json.loads(r["payload"]).get("profile") == CODER_PROFILE]
        if len(matched) == 1 and matched[0]["project"] == PROJECT_NAME:
            _ok("an orphan_container_stopped event was recorded, project-scoped", str(matched[0]))
        else:
            _fail("an orphan_container_stopped event was recorded, project-scoped", f"rows={rows}")

        # A second sweep right after must be a no-op: the orphan is already stopped, so default_list_hermes_
        # containers (RUNNING only) no longer lists it, and the running one is still protected.
        second = containers_mod.sweep_orphan_containers(BOARD, project, conn, kanban_list=_fake_kanban_list)
        if second.stopped == []:
            _ok("a second sweep right after is a no-op (nothing left running to stop)", "")
        else:
            _fail("a second sweep right after is a no-op", f"stopped={second.stopped}")

        # swarm stop, step f (ASES-REC-06, p357 "terminate worker process trees and sandboxes"; architect addition,
        # round 17). The running card's container, which the sweep rightly spared, is exactly what swarm stop must
        # stop. The card-id listing cannot find it (hermes-task-id is "default"); the listing by this project's
        # profiles can, and never lists another profile's container.
        other_id = _make_container(OTHER_PROFILE)
        try:
            by_card = killswitch_mod.default_list_containers(["t_running_card"])
            if by_card == []:
                _ok("swarm stop's card-id listing finds no real-labelled container (why the profile listing exists)")
            else:
                _fail("swarm stop's card-id listing finds no real-labelled container", f"found {by_card}")
            by_profile = killswitch_mod.default_list_profile_containers([CODER_PROFILE, REVIEWER_PROFILE, "lead"])
            running_name = _name(running_id)
            if by_profile == [running_name]:
                _ok("swarm stop's profile listing finds the running card's container and nothing else", running_name)
            else:
                _fail("swarm stop's profile listing finds the running card's container and nothing else",
                      f"listed={by_profile} expected=[{running_name}]")
            stopped = [name for name in by_profile if killswitch_mod.default_stop_container(name)]
            if stopped == by_profile and _is_running(running_id) is False:
                _ok("swarm stop's container step stopped the running card's sandbox", ", ".join(stopped))
            else:
                _fail("swarm stop's container step stopped the running card's sandbox",
                      f"stopped={stopped} State.Running={_is_running(running_id)}")
            if other_id and _is_running(other_id) is True:
                _ok("another profile's container was never touched (still Up)", "")
            else:
                _fail("another profile's container was never touched", f"State.Running={_is_running(other_id or '')}")
        finally:
            _remove(other_id)
    finally:
        _remove(orphan_id)
        _remove(running_id)
        still_there = [cid for cid in (orphan_id, running_id) if _is_running(cid) is not None]
        if not still_there:
            _ok("both containers removed at the end", "")
        else:
            _fail("both containers removed at the end", f"still present: {still_there}")

    print("\n--- PASS/FAIL table ---")
    for label, passed in _RESULTS:
        print(f"{'PASS' if passed else 'FAIL'}: {label}")
    if _FAILURES:
        print(f"\nSummary: FAIL ({len(_FAILURES)} finding(s))")
        return 1
    print("\nSummary: PASS (the orphan sweep stopped the idle profile's real container and left the "
          "running profile's alone; swarm stop's container step then stopped that one, and never another "
          "profile's)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
