"""Worker sandbox orphan sweep (package CONTAINERS, round 17; section 19.4 p353, ASES-REC-04, ASES-REC-06,
ASES-SEC-03).

"Orphan worker processes from a previous controller session are found by card ID and terminated before
new work starts" (blueprint p353). That sentence is true of PROCESSES: killswitch.terminate_tree finds a
worker's pid by reading its command line, which really does carry the dispatched card id (kanban_db_dispatch
invokes the worker CLI with the task on its own command line). It is NOT true of the Docker CONTAINER a
worker's terminal runs inside, and this module exists because nobody had checked that before round 17.

THE EMPIRICAL FINDING (confirmed against real Docker and Hermes 0.21.3's own installed source; the check is
scripts/hermes_container_labels_check.py, run with Hermes's own venv Python, and passed after this finding
was accounted for):

A container Hermes creates for a worker's Docker terminal carries exactly these labels (installed Hermes
0.21.3, tools/environments/docker.py:582-588):

    hermes-agent: "1"                     -- constant, every Hermes-created container has it
    hermes-task-id: <_resolve_container_task_id(task_id)>
    hermes-profile: <the ACTIVE Hermes profile name, docker.py:104-111 _get_active_profile_name>
    hermes-egress: <the egress posture label, docker_egress.py:16 _EGRESS_LABEL_KEY>

and its NAME is `f"hermes-{uuid.uuid4().hex[:8]}"` (docker.py:784), random and unrelated to the task id.

hermes-task-id is NOT the real card/task id here. tools/terminal_tool.py:400-444 _resolve_container_task_id
maps the raw task_id argument to a cache/label key in this order: an isolation override (RL/benchmark only,
never set by kanban dispatch); the raw id, but ONLY when `scope.session_isolated` -- docker AND
`container_persistent: false` (terminal_tool.py:369-375), which ASES's OWN terminal_block() (sandbox.py) never
sets, so it stays Hermes's own default "true" and this branch never taken by an ASES worker; then a
gateway/WebUI session key (terminal_tool.py:106-111 `_current_session_key`, a ContextVar the gateway/WebUI
sets, with an os.environ fallback for "CLI/cron/tests") -- kanban_db_dispatch.py spawns each worker as a
plain OS subprocess (subprocess.Popen, kanban_db_dispatch.py:2634, `sys.executable -m hermes_cli.main`,
kanban_db_dispatch.py:2237) with no HERMES_SESSION_KEY in its environment (confirmed by reading the whole
function that builds that subprocess's env: TERMINAL_CWD and HERMES_KANBAN_BRANCH are the only two task-shaped
values it sets, kanban_db_dispatch.py:2590-2592) -- so `session_key` is empty and the LAST branch
(terminal_tool.py:439-440) fires: `if not session_key: return "default"`. Confirmed empirically: the label was
literally `"default"`, never the fake task id `"t_deadbeef"` the check script passed in.

So: the real card/task id never reaches a container's label OR its name, under ASES's own profile config,
for a CLI-dispatched worker (which is what kanban dispatch always is). The docstring on sandbox.py already
half-knew this -- "they carry the label hermes-agent=1 and the profile in hermes-profile" never once claims
a usable task-id label -- this module is what makes that fact explicit, verified, and acted on.

THE DESIGN THIS LEADS TO: since a container cannot be tied to one card, it is tied to one Hermes PROFILE
instead (hermes-profile really is the active profile, e.g. "coder-1": _get_active_profile_name reads it from
Hermes's own profile-activation state, which kanban dispatch does set correctly per worker -- unlike the task
id, nothing here collapses it). A running container is an ORPHAN when:

  1. it carries hermes-agent=1 (never touch anything without this: an unrelated user container never matches,
     whatever name or other label it happens to carry -- Docker's own `--filter label=...` does the matching
     server-side, so this is not a text search that could be fooled by a coincidental substring);
  2. its hermes-profile is one of THIS project's own configured profiles (project.roles.values()) -- a
     profile this project does not use is never touched. This is a match by NAME alone: a profile of another
     ASES project on the same machine that happens to use the SAME name is not, and cannot be, told apart
     from this project's own (see the hard constraint below);
  3. that profile has NO card in status "running" on the board right now (hermes.kanban_list(board,
     status="running"), the same read leases.sweep_finished already trusts). A profile WITH a running card is
     left alone in full: since every container of that profile looks identical (same labels, random name),
     there is no way to tell an old orphan lingering next to today's live one apart from it, so the safe rule
     is to touch NOTHING for a profile that is doing live work, exactly as ASES-REC-06's own kill switch
     already refuses to act on anything it cannot verify (killswitch.py's module docstring, "A process whose
     command line cannot be read is not terminated").

If the running-cards read itself fails, nothing is swept this pass (mirrors leases.sweep_finished: "Sweeping
on a partial view of the board is worse than not sweeping"). Docker being absent or its daemon down is a
plain no-op, never a failure (default_list_hermes_containers returns [] exactly like
killswitch.default_list_containers already does).

HARD CONSTRAINT, NOT A SUGGESTION (round 17 fix round 2, reviewer finding on the first pass of this
package): Hermes profile names MUST be unique across every ASES project run on the same machine. Rule 2
above only ever compares a NAME (project.roles.values()) against a container's hermes-profile label, because
that is all a real Hermes container's labels carry -- hermes-agent, hermes-task-id, hermes-profile,
hermes-egress, confirmed empirically (scripts/hermes_container_labels_check.py) and no more: nothing here,
or anywhere in Hermes's own container-create path, tags a container with a project id. This project's own
config/swarm.yaml (and docs/operations.md's own reference table) both use "lead", "coder-1", "reviewer" as
the ordinary role names, which makes a same-name collision between two independently configured ASES
projects an easy accident, not an exotic one. If a second project on this machine configures one of the
same names and genuinely has a card running under it, THIS project's sweep still only ever reads ITS OWN
board (default_running_profiles has no way to read another project's board), sees that name as idle, and
stops the other project's live container. Nothing at the container-label level can close this gap: it is a
deployment constraint, not a bug this module can fix by itself.

`swarm doctor`'s profile_isolation row (doctor.py) is the mitigation this project can actually offer: a
best-effort, read-only WARN whenever it can find another local project's config/swarm.yaml (one level under
this project's own workspace_root or ases_home parent directory, the common layout) declaring one of this
project's own profile names under a DIFFERENT project name. It is not a guarantee -- a second project laid
out somewhere that scan does not reach is a collision `swarm doctor` cannot see -- so the real fix is
operational: give every project's roles distinct Hermes profile names, and do not lean on this module to
catch a collision it is structurally unable to detect.

REAPING: stop only, never `docker rm`, and this is a considered choice, not an oversight. Hermes's own
`reap_orphan_containers` (tools/environments/docker.py:125-164) already removes a stale hermes-agent=1
container of the SAME profile once it is Exited AND older than 2 x TERMINAL_LIFETIME_SECONDS (default 300s,
so 600s: docker.py:146-153) -- and it runs automatically, once per new worker process, every time that
profile's Docker terminal is next built (tools/terminal_tool_backends.py:133-138 `_build_docker_env` calls
`_maybe_reap_docker_orphans` before creating its OWN new container). Stopping an orphan here turns it Exited,
which is exactly what makes Hermes's own janitor pick it up next time that profile dispatches again; adding
our own `docker rm` on top would be an extra, unnecessary irreversible step (ASES's own kill switch keeps the
same "stop, do not remove" shape: killswitch.default_stop_container only ever calls `docker stop`). A profile
that never dispatches again keeps its stopped container around forever either way -- that is exactly what
`swarm doctor`'s orphan_containers row (doctor.py) is for: a visible, named, read-only nudge for a person to
`docker rm` it by hand if they want it gone sooner.
"""
from __future__ import annotations

import dataclasses
import subprocess
from collections.abc import Callable
from typing import Any

from . import config as ases_config
from . import events
from . import hermes as hermes_mod
from . import killswitch

_DOCKER = "docker"
_LIST_TIMEOUT = 20  # docker ps of a label filter is always fast; matches sandbox.py's own _INFO_TIMEOUT


def _run(args: list[str], timeout: float) -> tuple[int, str]:
    """The one place this module starts a process: (exit code, stdout as text). Raises what subprocess raises;
    callers catch it. A test replaces this function. Mirrors killswitch._run and hermes.py's own _run: each
    module that starts a process keeps its own copy rather than reaching into another module's private name."""
    result = subprocess.run(args, capture_output=True, timeout=timeout)
    return result.returncode, result.stdout.decode("utf-8", errors="replace")


def default_list_hermes_containers() -> list[tuple[str, str]]:
    """[(container name, hermes-profile label value), ...] for every RUNNING container Docker itself says
    carries the label hermes-agent=1 (server-side `--filter label=hermes-agent=1`, never a text search: see
    the module docstring for why that matters). The profile value can be "" when the label is somehow absent
    despite the filter (defensive only). [] and never raises when Docker is absent or its daemon is down,
    exactly like killswitch.default_list_containers. Calls the module-level `_run` by name (not a default
    argument bound at def time), so a test's `monkeypatch.setattr(containers, "_run", ...)` is seen."""
    try:
        code, out = _run(
            [_DOCKER, "ps", "--filter", "label=hermes-agent=1", "--format", '{{.Names}}|{{.Label "hermes-profile"}}'],
            _LIST_TIMEOUT,
        )
    except Exception:  # noqa: BLE001 - no docker, or it hung: nothing found
        return []
    if code != 0:
        return []
    found: list[tuple[str, str]] = []
    for line in out.splitlines():
        name, _, profile = line.partition("|")
        name = name.strip()
        if name:
            found.append((name, profile.strip()))
    return found


def default_running_profiles(board: str, *, kanban_list: Callable[..., Any] | None = None) -> set[str] | None:
    """The hermes-profile (Hermes calls it the assignee) of every card in status "running" on `board` right
    now, or None when the listing itself failed. None must never be read as "nothing is running": the caller
    treats it as "unknown, sweep nothing this pass", the same rule leases.sweep_finished applies to its own
    board read."""
    kanban_list = kanban_list if kanban_list is not None else hermes_mod.kanban_list
    try:
        cards = kanban_list(board, status="running")
    except Exception:  # noqa: BLE001 - unknown: the caller must not guess
        return None
    profiles: set[str] = set()
    for card in cards if isinstance(cards, (list, tuple)) else []:
        if isinstance(card, dict):
            assignee = card.get("assignee")
            if assignee:
                profiles.add(str(assignee))
    return profiles


def find_orphan_containers(
    board: str, project: ases_config.ProjectConfig, *,
    list_containers: Callable[[], list[tuple[str, str]]] | None = None,
    kanban_list: Callable[..., Any] | None = None,
) -> list[tuple[str, str]] | None:
    """[(container name, profile), ...] for a RUNNING hermes-agent=1 container whose profile is one of
    `project`'s own (project.roles.values()) and currently has no card running on `board`. None when the
    board's running cards could not be read (see default_running_profiles): callers must sweep nothing then.
    [] is the ordinary "nothing found" answer -- no hermes-agent=1 containers at all, or every one found
    belongs to a profile with live work or to no profile of this project."""
    list_containers = list_containers if list_containers is not None else default_list_hermes_containers
    project_profiles = {str(name) for name in (getattr(project, "roles", None) or {}).values() if name}
    if not project_profiles:
        return []
    try:
        containers = list_containers()
    except Exception:  # noqa: BLE001 - never raises: nothing found this pass
        return []
    if not containers:
        return []
    running = default_running_profiles(board, kanban_list=kanban_list)
    if running is None:
        return None
    return [
        (name, profile) for name, profile in containers
        if profile in project_profiles and profile not in running
    ]


@dataclasses.dataclass
class OrphanSweepReport:
    """What one orphan sweep did. `stopped` names the containers Docker confirmed it stopped; every failure
    (a listing that could not run, a stop that did not confirm) is one line in `notes`, never raised."""
    stopped: list[str] = dataclasses.field(default_factory=list)
    notes: list[str] = dataclasses.field(default_factory=list)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def sweep_orphan_containers(
    board: str, project: ases_config.ProjectConfig, conn, *,
    list_containers: Callable[[], list[tuple[str, str]]] | None = None,
    stop_container: Callable[[str], bool] | None = None,
    kanban_list: Callable[..., Any] | None = None,
) -> OrphanSweepReport:
    """p353 (section 19.4): stop this project's own orphaned worker sandboxes (find_orphan_containers) before
    new work starts, and on the per-pass sweep. A no-op, never a failure, while sandbox.enabled is false (no
    Docker calls happen at all: cheap to call unconditionally every pass) or Docker is unreachable. Never
    touches a container whose profile has a card running right now, and never a container outside this
    project's own profiles (find_orphan_containers's own rules). `stop_container` defaults to
    killswitch.default_stop_container (`docker stop -t 5`; never `docker rm`, see the module docstring).
    Records one events.py row per container, project-scoped (events.PROJECT_SQL), so a person can see what
    was reclaimed without watching the run live."""
    report = OrphanSweepReport()
    if not bool(getattr(project, "sandbox_enabled", False)):
        return report
    orphans = find_orphan_containers(board, project, list_containers=list_containers, kanban_list=kanban_list)
    if orphans is None:
        report.notes.append("could not list running cards on the board, so nothing was swept this pass")
        return report
    if not orphans:
        return report
    stop_container = stop_container if stop_container is not None else killswitch.default_stop_container
    project_name = getattr(project, "name", None)
    for name, profile in orphans:
        try:
            stopped = bool(stop_container(name))
        except Exception as exc:  # noqa: BLE001 - one container must never stop the sweep of the others
            report.notes.append(f"docker stop raised for container {name}: {type(exc).__name__}: {exc}"[:300])
            events.record(
                conn, "orphan_container_stop_failed",
                {"container": name, "profile": profile, "error": f"{type(exc).__name__}: {exc}"[:300]},
                project=project_name,
            )
            continue
        if stopped:
            report.stopped.append(name)
            events.record(
                conn, "orphan_container_stopped", {"container": name, "profile": profile}, project=project_name,
            )
        else:
            report.notes.append(f"docker stop did not confirm success for container {name} (profile {profile})")
            events.record(
                conn, "orphan_container_stop_failed", {"container": name, "profile": profile}, project=project_name,
            )
    return report
