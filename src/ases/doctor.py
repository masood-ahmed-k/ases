"""`swarm doctor` check logic (acceptance test 22.1).

Kept separate from cli.py so the checks are unit-testable without going through argv. Each check
returns a DoctorCheck with an honest status: PASS/WARN/FAIL for things Phase 1 can actually verify,
PENDING for things that legitimately don't exist until a later phase (the gateway dispatcher, a
sandbox the user has not enabled) -- a PENDING check is not a hidden failure, it's a true statement
about where the project is in the phase plan (section 16), and it must say so rather than pretend to be green.

Overall status is FAIL only if any single check is FAIL; WARN and PENDING never fail the run on their
own, matching the exit-code convention `hermes doctor` itself uses (0 = healthy enough to proceed).

Two families of rows come from other modules and are deliberately soft:
  * the profile state (profiles.verify_state): each problem is one WARN row and never a FAIL, because a
    machine that has not had `swarm init --apply` run on it is still usable;
  * the sandbox (sandbox.doctor_checks): WARN rows while config/swarm.yaml says `sandbox: enabled: false`
    (Docker is off until the user starts it, ASES-SEC-03), and FAIL only once the config says it is enabled
    and a check fails, because then the workers really are meant to run inside it.
"""
from __future__ import annotations

import dataclasses
import importlib
import inspect
import json
import os
import pathlib
import re
import subprocess
import sys

from . import config as ases_config
from . import containers as containers_mod
from . import events as events_mod
from . import gitexec
from . import hermes as hermes_mod
from . import models as models_mod
from . import procenv as procenv_mod
from . import sandbox as sandbox_mod

Status = str  # "pass" | "warn" | "fail" | "pending" | "info"


@dataclasses.dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: Status
    detail: str
    requirement_ids: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class DoctorReport:
    checks: tuple[DoctorCheck, ...]

    @property
    def ok(self) -> bool:
        return not any(c.status == "fail" for c in self.checks)

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 1


def _repo_root() -> pathlib.Path:
    # src/ases/doctor.py -> src/ases -> src -> repo root
    return pathlib.Path(__file__).resolve().parents[2]


def _check_environment_decision(project: ases_config.ProjectConfig) -> DoctorCheck:
    return DoctorCheck(
        "environment_decision",
        "pass",
        f"decision D1 = {project.environment} (config/swarm.yaml project.environment)",
        ("ASES-ENV-01",),
    )


def _check_not_under_onedrive(project: ases_config.ProjectConfig) -> DoctorCheck:
    # config.py already refuses to load a config whose workspace_root/ases_home are under OneDrive, so
    # reaching this point means those two are clean. Also check the repo root itself, which config.py
    # doesn't know about.
    root = _repo_root()
    if "onedrive" in str(root).lower():
        return DoctorCheck(
            "not_under_onedrive", "fail", f"ASES repo root is under OneDrive: {root}", ("ASES-ENV-03",)
        )
    return DoctorCheck(
        "not_under_onedrive",
        "pass",
        f"repo root ({root}), workspace_root, and ases_home are all outside OneDrive",
        ("ASES-ENV-03",),
    )


def _check_git_longpaths(project: ases_config.ProjectConfig) -> DoctorCheck:
    if project.environment != "native":
        return DoctorCheck("git_longpaths", "pending", "only checked on native Windows", ())
    try:
        result = subprocess.run(
            [*gitexec.GIT, "-C", str(_repo_root()), "config", "--get", "core.longpaths"],
            capture_output=True, text=True, timeout=10, env=gitexec.git_env(),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return DoctorCheck("git_longpaths", "fail", f"could not run git: {exc}", ("ASES-ENV-01",))
    value = result.stdout.strip().lower()
    if value == "true":
        return DoctorCheck("git_longpaths", "pass", "git core.longpaths=true", ("ASES-ENV-01",))
    return DoctorCheck(
        "git_longpaths", "warn",
        f"git core.longpaths is {value or 'unset'}, expected true on native Windows (section 17.2)",
        ("ASES-ENV-01",),
    )


def _check_log_all_ref_updates(repo: pathlib.Path | None) -> DoctorCheck:
    """[ASES-GIT-01] [ASES-GIT-16] (blueprint p169's second sentence: "Phase 3 MUST verify the actual base commit
    before a worker starts"). guards.check_card_base reads a work card branch's base commit from its OWN reflog's
    "branch: Created from ..." entry (see guards.py's module-level comment on that section for why: it is the
    only durable record git keeps of where a branch began), which only exists when `core.logAllRefUpdates` is on
    -- for the PROJECT repository the swarm dispatches cards into, not this ASES checkout, which is what every
    other check in this module inspects (`repo` is a separate parameter for exactly that reason).

    `git init` has written `core.logAllRefUpdates = true` into a fresh non-bare repository's own config since
    long before this Hermes version, so an EXPLICIT "true" and an UNSET value are both healthy (a non-bare
    repository's own default is true either way); only an explicit "false" is a real WARN, since that repository
    would then be unable to answer a base-commit check at all, and guards.check_card_base fails a card's base
    closed (blocks it) whenever the signal is missing, so a real remote-tip sync would go undetected right along
    with every legitimate card.

    `repo` is None when the caller has not been given the project repository's path: `cmd_doctor`'s `--repo` is
    optional (added round 10; cli.py threads `args.repo` through to this function's own `repo` keyword), so an
    invocation without it still runs every other check, but this one stays "pending" rather than silently
    checking the wrong repository or claiming a pass it cannot back up."""
    if repo is None:
        return DoctorCheck(
            "log_all_ref_updates", "pending",
            "not checked: swarm doctor was not given the project repository's path this time (pass --repo). "
            "The base-commit check (guards.check_card_base) depends on core.logAllRefUpdates being on "
            "there, not in this ASES checkout.",
            ("ASES-GIT-01", "ASES-GIT-16"),
        )
    try:
        result = subprocess.run(
            [*gitexec.GIT, "-C", str(repo), "config", "--get", "core.logAllRefUpdates"],
            capture_output=True, text=True, timeout=10, env=gitexec.git_env(),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return DoctorCheck(
            "log_all_ref_updates", "warn", f"could not read core.logAllRefUpdates in {repo}: {exc}",
            ("ASES-GIT-01", "ASES-GIT-16"),
        )
    value = result.stdout.strip().lower()
    if value in ("true", ""):
        why = "core.logAllRefUpdates=true" if value == "true" else "core.logAllRefUpdates is unset (default true for a non-bare repository)"
        return DoctorCheck(
            "log_all_ref_updates", "pass", f"{why} in {repo}: the base-commit check can read branch reflogs",
            ("ASES-GIT-01", "ASES-GIT-16"),
        )
    return DoctorCheck(
        "log_all_ref_updates", "warn",
        f"core.logAllRefUpdates={value!r} in {repo}, expected true (or unset): the base-commit check "
        "(guards.check_card_base) reads a branch's creation commit from its reflog and fails a card's base "
        "closed, blocking it, whenever that signal is missing -- so with this off, a real remote-tip sync would "
        "go undetected right along with every legitimate card",
        ("ASES-GIT-01", "ASES-GIT-16"),
    )


# Git's own boolean-true spellings for a config value (git-config(1)): case folded by the .lower() below.
_GIT_TRUE_SPELLINGS = ("true", "yes", "on", "1")


def _check_worktree_relative_paths(repo: pathlib.Path | None, sandbox_enabled: bool) -> DoctorCheck:
    """WORKERGIT (round 15, docs/work-orders/r15_wp_sandbox.md; ASES-SEC-03): a worker's Docker terminal backend
    can only commit inside the linked worktree Hermes dispatches it into when the PROJECT repository's own
    `worktree.useRelativePaths` is true (git 2.48+): `git worktree add` then records the new worktree's own
    `.git` gitdir pointer as a RELATIVE path, the only kind that can resolve once just the worktree is mounted
    into the container (sandbox.py's module docstring has the full mechanism; scripts/workergit_live_check.py
    proves it against a real container). controller.ensure_repo_bootstrapped sets this for a repository ASES
    itself creates from empty; an EXISTING repository (the common case) is never touched there, so this is what
    tells a person it still needs `git config worktree.useRelativePaths true` run once, by hand, in that
    repository.

    Same `repo` is None handling as _check_log_all_ref_updates: without --repo there is nothing to read. Unlike
    that check, "unset" here is NOT healthy: worktree.useRelativePaths defaults to false (the opposite polarity
    of core.logAllRefUpdates), so unset and an explicit "false" report the same way. A healthy value is any of
    git's own boolean-true spellings (_GIT_TRUE_SPELLINGS: "true", "yes", "on", "1"), not only the literal "true"
    controller.ensure_repo_bootstrapped writes -- a human hand-editing the config is free to use any of them, and
    git itself treats them identically. Severity: a WARN always (matching every other repository-shape check in
    this module), raised to a FAIL only once `sandbox_enabled` is true -- that is the point a worker's own `git
    commit` inside the container is demonstrably broken by this, not a theoretical future problem, the same
    "pending vs FAIL only once the sandbox is really on"
    reasoning `_check_sandbox` already applies elsewhere in this module."""
    if repo is None:
        return DoctorCheck(
            "worktree_relative_paths", "pending",
            "not checked: swarm doctor was not given the project repository's path this time (pass --repo). "
            "A worker's Docker terminal backend needs worktree.useRelativePaths=true there to commit inside "
            "its dispatched worktree (WORKERGIT).",
            _SANDBOX_IDS,
        )
    try:
        result = subprocess.run(
            [*gitexec.GIT, "-C", str(repo), "config", "--get", "worktree.useRelativePaths"],
            capture_output=True, text=True, timeout=10, env=gitexec.git_env(),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return DoctorCheck(
            "worktree_relative_paths", "warn", f"could not read worktree.useRelativePaths in {repo}: {exc}",
            _SANDBOX_IDS,
        )
    value = result.stdout.strip().lower()
    if value in _GIT_TRUE_SPELLINGS:
        return DoctorCheck(
            "worktree_relative_paths", "pass",
            f"worktree.useRelativePaths={value} in {repo}: a dispatched worker can commit inside its own linked "
            "worktree",
            _SANDBOX_IDS,
        )
    status = "fail" if sandbox_enabled else "warn"
    why = f"worktree.useRelativePaths={value}" if value else "worktree.useRelativePaths is unset (default false)"
    return DoctorCheck(
        "worktree_relative_paths", status,
        f"{why} in {repo}: run `git config worktree.useRelativePaths true` there once, by hand, or a worker's "
        "own `git commit` inside its dispatched worktree fails (WORKERGIT)"
        + (" -- the sandbox is enabled, so this blocks real work today" if sandbox_enabled else ""),
        _SANDBOX_IDS,
    )


_WORKTREE_LEAK_KINDS = ("gate_worktree_leak", "merge_worktree_leak")
_WORKTREE_LEAK_IDS = ("ASES-GIT-12",)


def _leaked_worktree_events(conn, project_name: str) -> list[dict]:
    """Every gate_worktree_leak/merge_worktree_leak event recorded for this project (gates.run_gate's
    `_report_worktree_leak_if_any`, mergeq.merge_task's `_report_candidate_leak_if_any`, round 12 finding 11):
    each one names a throwaway gate or merge worktree that `git worktree remove --force` deregistered but a
    Windows file lock kept from actually being deleted. Project-scoped through events.PROJECT_SCOPE_SQL, the one
    project-scope filter every reader of the events table uses (round 10 rules), so this sees exactly this
    project's own rows (plus legacy rows with no project recorded), never another project's."""
    placeholders = ", ".join("?" for _ in _WORKTREE_LEAK_KINDS)
    rows = conn.execute(
        f"SELECT kind, payload FROM events WHERE kind IN ({placeholders}) AND {events_mod.PROJECT_SCOPE_SQL} "
        "ORDER BY id",
        (*_WORKTREE_LEAK_KINDS, project_name),
    ).fetchall()
    found = []
    for kind, payload in rows:
        try:
            data = json.loads(payload)
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict) and data.get("path"):
            found.append({"kind": kind, **data})
    return found


def _norm_path(path: str) -> str:
    """Forward slashes, so a path compares equal whichever way it was spelled: `git worktree list --porcelain`
    always prints forward slashes, even on Windows (proven against a real repository while building this check),
    while the leak events store `str(tmp_root)`, native backslashes there. Neither side is wrong, they just do
    not compare equal as raw strings without this. Also lower-cased on Windows only: its filesystem is
    case-insensitive, so a leak event's own recorded path and git's own report of the same directory are not
    guaranteed to agree on case, and a caller here wants "the same directory", not "the same bytes". POSIX stays
    case-sensitive, matching its filesystem."""
    normalized = pathlib.PurePath(path).as_posix()
    return normalized.lower() if os.name == "nt" else normalized


def _registered_worktrees(repo: pathlib.Path) -> set[str] | None:
    """The paths `git worktree list` still has registered for the project repository, normalized with
    `_norm_path` so they compare equal to a leak event's own (natively-spelled) path, or None when the command
    could not be read at all (git missing, timeout, `repo` not a repository). None is "unknown", never "nothing
    registered": a caller must not let a git failure here make a real leak look cleaned up."""
    try:
        result = subprocess.run(
            [*gitexec.GIT, "-C", str(repo), "worktree", "list", "--porcelain"],
            capture_output=True, text=True, timeout=15, env=gitexec.git_env(),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return {
        _norm_path(line[len("worktree "):].strip())
        for line in result.stdout.splitlines() if line.startswith("worktree ")
    }


def _check_leaked_worktrees(project: ases_config.ProjectConfig, conn, repo: pathlib.Path | None) -> DoctorCheck:
    """Blueprint p185 (ASES-GIT-12) makes the controller responsible for what sits outside a worker's own
    worktree. Round 12 (finding 11) started RECORDING a leaked throwaway gate/merge worktree when `git worktree
    remove --force` deregisters it but a Windows file lock (a hung gate command that still has it as its cwd, or
    any other process with an open handle inside it) keeps the directory itself from being deleted -- but nothing
    ever surfaced those records anywhere a person would look. This is that surface.

    One WARN row per distinct leaked path that is STILL present (its directory still exists on disk, and/or `git
    worktree list` of the project repository still has it registered), never FAIL: a leftover throwaway worktree
    is disk and hygiene, a leaked test-run byproduct, not a broken gate or a security problem, and it must not
    turn `swarm doctor`'s overall status red. Read-only, like every other check in this module: this never
    deletes a directory or runs `git worktree prune` itself, it only names each path and the two commands
    (`git worktree prune`, then delete the directory if it remains) that clean it up.

    A path recorded once but no longer present anywhere (the operator already cleaned it up) drops out silently:
    only what is still actually leaked is worth a row. `repo` is optional, exactly like `_check_log_all_ref_updates`
    (cmd_doctor's `--repo`, round 10): without it, presence is judged from disk existence alone and the row says
    so, so it never claims a `git worktree list` cross-check it did not make."""
    leaks = _leaked_worktree_events(conn, project.name)
    if not leaks:
        return DoctorCheck(
            "leaked_worktrees", "pass", "no gate/merge worktree-leak events recorded for this project",
            _WORKTREE_LEAK_IDS,
        )
    registered = _registered_worktrees(repo) if repo is not None else None
    still_present: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for leak in leaks:
        path = str(leak["path"])
        if path in seen:
            continue
        seen.add(path)
        on_disk = pathlib.Path(path).exists()
        also_registered = registered is not None and _norm_path(path) in registered
        if not (on_disk or also_registered):
            continue
        reasons = []
        if on_disk:
            reasons.append("directory still on disk")
        if also_registered:
            reasons.append("still registered in `git worktree list`")
        still_present.append((leak.get("kind", "?"), path, ", ".join(reasons)))
    if not still_present:
        return DoctorCheck(
            "leaked_worktrees", "pass",
            f"{len(leaks)} worktree-leak event(s) recorded for this project, but none of the leaked path(s) are "
            "still present (already cleaned up)",
            _WORKTREE_LEAK_IDS,
        )
    detail = f"{len(still_present)} leaked gate/merge worktree(s) still present: " + "; ".join(
        f"{kind} at {path} ({why}) -- run `git worktree prune` in the project repository, then delete the "
        "directory if it remains"
        for kind, path, why in still_present
    )
    if repo is None:
        detail += (
            " (swarm doctor was not given --repo this time, so `git worktree list` was not cross-checked; "
            "presence above is from the recorded path's disk existence alone)"
        )
    return DoctorCheck("leaked_worktrees", "warn", detail, _WORKTREE_LEAK_IDS)


def _check_gitattributes(project: ases_config.ProjectConfig) -> DoctorCheck:
    if project.environment != "native":
        return DoctorCheck("gitattributes_eol", "pending", "only checked on native Windows", ())
    ga = _repo_root() / ".gitattributes"
    if not ga.exists():
        return DoctorCheck("gitattributes_eol", "fail", ".gitattributes is missing", ("ASES-ENV-01",))
    text = ga.read_text(encoding="utf-8")
    if "eol=lf" in text:
        return DoctorCheck("gitattributes_eol", "pass", ".gitattributes sets eol=lf", ("ASES-ENV-01",))
    return DoctorCheck("gitattributes_eol", "warn", ".gitattributes exists but has no eol=lf rule", ("ASES-ENV-01",))


def _check_python_version() -> DoctorCheck:
    ok = sys.version_info >= (3, 11)
    v = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    return DoctorCheck("python_version", "pass" if ok else "fail", f"running Python {v}", ())


def _check_git_version() -> DoctorCheck:
    try:
        result = subprocess.run(
            [*gitexec.GIT, "--version"], capture_output=True, text=True, timeout=10, env=gitexec.git_env(),
        )
        return DoctorCheck("git_version", "pass", result.stdout.strip(), ())
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return DoctorCheck("git_version", "fail", f"git not usable: {exc}", ())


def _check_hermes_version(project: ases_config.ProjectConfig) -> DoctorCheck:
    version = hermes_mod.hermes_version()
    if version is None:
        return DoctorCheck("hermes_version", "fail", "hermes not found on PATH or --version did not parse", ())
    if version == project.hermes_tested_version:
        return DoctorCheck("hermes_version", "pass", f"hermes {version} (matches the tested version)", ())
    return DoctorCheck(
        "hermes_version", "warn",
        f"hermes {version} is installed; ASES was last checked against {project.hermes_tested_version}. "
        "Re-verify the CLI surface before relying on it (Hermes changes fast).",
        (),
    )


def _check_hermes_doctor() -> DoctorCheck:
    result = hermes_mod.run_doctor()
    if result.exit_code is None:
        return DoctorCheck("hermes_doctor", "fail", result.raw_output.strip() or "hermes doctor did not run", ())
    if result.ok:
        detail = "hermes doctor exited 0 (healthy)"
        if result.warning_lines:
            detail += f"; {len(result.warning_lines)} warning line(s) in its own output (non-blocking)"
        return DoctorCheck("hermes_doctor", "pass", detail, ())
    return DoctorCheck(
        "hermes_doctor", "fail",
        f"hermes doctor exited {result.exit_code}: " + " | ".join(result.error_lines[:3]),
        (),
    )


def _check_gateway_dispatcher() -> DoctorCheck:
    status = hermes_mod.gateway_status()
    if status.running:
        return DoctorCheck("gateway_dispatcher", "pass", "hermes gateway is running", ("ASES-ARC-05", "ASES-ARC-07"))
    return DoctorCheck(
        "gateway_dispatcher", "pending",
        "gateway not running -- expected until Phase 3 starts dispatching real Kanban cards",
        ("ASES-ARC-05", "ASES-ARC-07"),
    )


_SANDBOX_IDS = ("ASES-SEC-03", "ASES-CFG-04")
# The orphan-container and profile-isolation rows (round 17): the sandbox itself, the reconcile-on-start
# sweep that stops orphans (p352-p353) and the kill switch that stops every sandbox (p357).
_CONTAINER_IDS = ("ASES-SEC-03", "ASES-REC-04", "ASES-REC-06")
_PROFILE_IDS = ("ASES-ROL-02", "ASES-ROL-07", "ASES-ARC-08")
# The roles that are not workers when profiles.desired_profiles cannot say: the lead plans and the reviewer only
# has the Kanban verdict tools and read access (ASES-ROL-05), so neither runs a worker's shell.
_NON_WORKER_ROLES = ("lead", "reviewer")
_LSP_IDS = ("ASES-DOC-04",)


def _takes(func, name: str) -> bool:
    """Does `func` take a keyword argument called `name` (or any, through **kwargs)? False when its signature cannot
    be read."""
    try:
        parameters = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False
    return name in parameters or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())


def _extract_requirement_ids(problem: str) -> tuple[str, ...]:
    """Add any requirement IDs from the problem message to the base profile IDs."""
    ids = tuple(dict.fromkeys(re.findall(r'ASES-[A-Z]+-\d+', problem))) + _PROFILE_IDS
    return tuple(dict.fromkeys(ids))


def _load_profiles_module() -> tuple[object | None, DoctorCheck | None]:
    """(the ases.profiles module, None), or (None, the row to show instead). The module belongs to another
    package and a machine may not have it yet, so a missing one is PENDING (a true statement about the build) and
    a broken one is a WARN; neither may crash the doctor."""
    try:
        return importlib.import_module(f"{__package__}.profiles"), None
    except ImportError as exc:
        return None, DoctorCheck(
            "profile_state", "pending",
            f"the profiles module is not part of this build yet, so the Hermes profile state was not verified ({exc})",
            _PROFILE_IDS,
        )
    except Exception as exc:  # noqa: BLE001 - a module that fails to load costs one row, not the doctor
        return None, DoctorCheck(
            "profile_state", "warn",
            f"the profiles module failed to load, so the Hermes profile state was not verified: "
            f"{type(exc).__name__}: {exc}",
            _PROFILE_IDS,
        )


def _check_profile_state(project: ases_config.ProjectConfig, models_config: dict, profiles_mod) -> list[DoctorCheck]:
    """ASES-ROL-02, ASES-ROL-07, ASES-ARC-08: one WARN row per problem profiles.verify_state reports (a worker
    with the memory toolset, a reviewer with a terminal, a missing SOUL.md, a model mismatch, missing kanban
    limits). Never a FAIL: a machine that has not had `swarm init --apply` run on it is still usable, and the
    hard requirements have their own rows (role_profiles, reviewer_diversity).

    With the sandbox enabled the terminal blocks are checked against the policy of config/swarm.yaml (its image and
    limits), when verify_state takes one; without it verify_state falls back to the image the profile names itself."""
    enabled = bool(getattr(project, "sandbox_enabled", False))
    kwargs: dict = {"sandbox_enabled": enabled}
    if enabled and _takes(profiles_mod.verify_state, "policy"):
        try:
            kwargs["policy"] = sandbox_mod.SandboxPolicy.from_config(project.sandbox_policy_config())
        except Exception:  # noqa: BLE001 - a bad sandbox block has its own row (sandbox_config)
            pass
    try:
        problems = profiles_mod.verify_state(
            project, models_config, project.hermes_native_home, _repo_root() / "prompts", **kwargs,
        )
    except Exception as exc:  # noqa: BLE001 - a doctor row never crashes the doctor
        return [DoctorCheck(
            "profile_state", "warn", f"could not verify the profile state: {type(exc).__name__}: {exc}", _PROFILE_IDS,
        )]
    problems = [str(problem) for problem in (problems or [])]
    if not problems:
        return [DoctorCheck(
            "profile_state", "pass", "the Hermes profiles are in the desired state (swarm init has nothing to change)",
            _PROFILE_IDS,
        )]
    return [
        DoctorCheck(
            f"profile_state[{number}]", "warn", f"{problem} (`swarm init` shows the fix)", _extract_requirement_ids(problem),
        )
        for number, problem in enumerate(problems, start=1)
    ]


def _check_residual_risks(profiles_mod: object | None) -> list[DoctorCheck]:
    """ASES-ROL-05: profiles.residual_risks() names the known, accepted limits of the profile hardening this
    build can do (its own docstring asks `swarm init` and `swarm doctor` to print them next to the plan, see
    profiles.RESIDUAL_RISKS -- for example the Reviewer keeping write tools its prompt forbids it to use, because
    Hermes has no read-only file toolset). Each one is an INFO row: never WARN or FAIL, because nothing here is
    unhealthy or unexpected -- it is a documented, accepted gap, and a doctor that hid it behind "HEALTHY" would
    be pretending the gap is closed. `profiles_mod` may be an older build with no such function (or the stub a
    test puts in sys.modules), so a missing attribute is simply no rows, not a warning."""
    fn = getattr(profiles_mod, "residual_risks", None)
    if fn is None:
        return []
    try:
        risks = [str(risk) for risk in fn()]
    except Exception as exc:  # noqa: BLE001 - a doctor row never crashes the doctor
        return [DoctorCheck(
            "residual_risks", "warn", f"could not read the residual risks: {type(exc).__name__}: {exc}",
            ("ASES-ROL-05",),
        )]
    return [
        DoctorCheck(f"residual_risk[{i}]", "info", risk, ("ASES-ROL-05",)) for i, risk in enumerate(risks, start=1)
    ]


def _worker_profile_names(project: ases_config.ProjectConfig, models_config: dict, profiles_mod) -> list[str]:
    """The profiles whose terminal must run inside the sandbox. profiles.desired_profiles is the source of truth:
    `swarm init` puts the Docker terminal block on an ACTIVE worker that has the terminal toolset (the reviewer is a
    worker too, since the dispatcher spawns it, but it has no terminal), and the doctor asks about exactly those, so
    the two cannot disagree and a green doctor is reachable. Without the module every role except the lead and the
    reviewer counts, and a profile that also plays the lead or the reviewer is left out."""
    if profiles_mod is not None:
        try:
            specs = profiles_mod.desired_profiles(project, models_config)
            names = [
                str(spec.name) for spec in specs
                if getattr(spec, "worker", False) and getattr(spec, "active", True)
                and "terminal" in (getattr(spec, "toolsets", None) or ("terminal",))
            ]
        except Exception:  # noqa: BLE001 - fall back to the role map below
            names = []
        if names:
            return list(dict.fromkeys(names))
    excluded = {project.roles.get(role) for role in _NON_WORKER_ROLES}
    names: list[str] = []
    for role, profile in project.roles.items():
        if role in _NON_WORKER_ROLES or profile in excluded or profile in names:
            continue
        names.append(profile)
    return names


def _check_sandbox(project: ases_config.ProjectConfig, models_config: dict, profiles_mod) -> list[DoctorCheck]:
    """ASES-SEC-03, ASES-CFG-04: `sandbox.doctor_checks` as rows (Docker reachable, the pinned image present, each
    worker profile's terminal block compliant). The sandbox is OFF by default until the user has Docker running,
    so while config/swarm.yaml says `enabled: false` a failing check is only a WARN and the first row says the
    sandbox is off; once it says `enabled: true` a failing check is a FAIL, because the workers are then meant to
    run inside it. The rows never contain a secret (sandbox.doctor_checks documents that)."""
    enabled = bool(getattr(project, "sandbox_enabled", False))
    bad = "fail" if enabled else "warn"
    if enabled:
        rows = [DoctorCheck(
            "sandbox_enabled", "pass", "sandbox: enabled: true in config/swarm.yaml; the rows below must be green",
            _SANDBOX_IDS,
        )]
    else:
        rows = [DoctorCheck(
            "sandbox_enabled", "pending",
            "sandbox is not enabled in config/swarm.yaml (Docker is off until the user starts it): workers run on "
            "the local backend, so ASES-SEC-03 is not met yet, and the sandbox rows below are warnings",
            _SANDBOX_IDS,
        )]
    try:
        policy = sandbox_mod.SandboxPolicy.from_config(project.sandbox_policy_config())
        profile_dirs = [
            project.hermes_native_home / "profiles" / name
            for name in _worker_profile_names(project, models_config, profiles_mod)
        ]
        results = sandbox_mod.doctor_checks(policy, profile_dirs, home=pathlib.Path.home())
    except sandbox_mod.SandboxConfigError as exc:
        return rows + [DoctorCheck("sandbox_config", bad, f"config/swarm.yaml sandbox: {exc}", _SANDBOX_IDS)]
    except Exception as exc:  # noqa: BLE001 - a probe never crashes the doctor
        return rows + [DoctorCheck(
            "sandbox_checks", bad, f"could not run the sandbox checks: {type(exc).__name__}: {exc}", _SANDBOX_IDS,
        )]
    if enabled and not policy.image:
        rows.append(DoctorCheck(
            "sandbox_image_configured", "fail",
            "sandbox.enabled is true but config/swarm.yaml names no sandbox.image: workers need a pinned image "
            "with the project toolchain (ASES-SEC-03)",
            _SANDBOX_IDS,
        ))
    for name, ok, detail in results:
        detail = str(detail)
        if not ok and not enabled:
            detail += " (sandbox not enabled, so only a warning)"
        rows.append(DoctorCheck(str(name), "pass" if ok else bad, detail, _SANDBOX_IDS))
    return rows


def _check_orphan_containers(project: ases_config.ProjectConfig) -> list[DoctorCheck]:
    """CONTAINERS (round 17), ASES-SEC-03: a read-only row listing this project's own orphaned worker
    sandboxes (ases.containers.find_orphan_containers: a RUNNING container labelled hermes-agent=1, whose
    Hermes profile is one of this project's own (project.roles.values()), with no card of this project
    currently running under that profile), names only, plus the command to reclaim them by hand. Shown only
    while sandbox.enabled is true (see that module's docstring for why a container cannot be tied to one
    card): no rows at all otherwise, matching config/swarm.yaml `sandbox: enabled: false` being the ordinary,
    unremarkable case. Docker unreachable is reported and is never a failure (nothing here ever stops a
    container: only ases.containers.sweep_orphan_containers, called elsewhere, does that)."""
    if not bool(getattr(project, "sandbox_enabled", False)):
        return []
    ok, why = sandbox_mod.docker_available()
    if not ok:
        return [DoctorCheck(
            "orphan_containers", "pending", f"could not check for orphaned worker containers: {why}", _CONTAINER_IDS,
        )]
    try:
        orphans = containers_mod.find_orphan_containers(project.board, project)
    except Exception as exc:  # noqa: BLE001 - a doctor row never crashes the doctor
        return [DoctorCheck(
            "orphan_containers", "warn",
            f"could not check for orphaned worker containers: {type(exc).__name__}: {exc}", _CONTAINER_IDS,
        )]
    if orphans is None:
        return [DoctorCheck(
            "orphan_containers", "pending",
            "could not list running cards on the board, so orphaned worker containers were not checked this run",
            _CONTAINER_IDS,
        )]
    if not orphans:
        return [DoctorCheck(
            "orphan_containers", "pass", "no orphaned worker containers found for this project's profiles",
            _CONTAINER_IDS,
        )]
    names = [name for name, _ in orphans]
    cleanup = "; ".join(f"docker stop -t 5 {name}" for name in names)
    return [DoctorCheck(
        "orphan_containers", "warn",
        f"{len(names)} orphaned worker container(s) (no card of this project is running under their profile): "
        f"{', '.join(names)}. Reclaim by hand with: {cleanup}",
        _CONTAINER_IDS,
    )]


def _sibling_swarm_configs(project: ases_config.ProjectConfig) -> list[pathlib.Path]:
    """Every OTHER project's config/swarm.yaml this can find one level under project.workspace_root's parent
    directory and one level under project.ases_home's parent directory (the two directories deduplicated
    when they are the same one) -- the layout this machine's own project actually has, a repository root
    holding config/swarm.yaml with workspace_root and ases_home configured as paths near it. Never recurses
    further than one level, and never raises: a parent that does not exist or that this process cannot read
    contributes nothing rather than failing the scan. Best-effort only, not a real project registry (ASES
    has none): see _check_profile_isolation, the only caller."""
    roots: list[pathlib.Path] = []
    seen_roots: set[str] = set()
    for base in (project.workspace_root, project.ases_home):
        try:
            parent = pathlib.Path(base).resolve().parent
        except OSError:
            continue
        key = str(parent)
        if key in seen_roots:
            continue
        seen_roots.add(key)
        roots.append(parent)
    found: list[pathlib.Path] = []
    seen_configs: set[str] = set()
    for root in roots:
        try:
            candidates = sorted(root.iterdir())
        except OSError:
            continue
        for candidate in candidates:
            try:
                if not candidate.is_dir():
                    continue
                swarm_path = candidate / "config" / "swarm.yaml"
                if not swarm_path.is_file():
                    continue
            except OSError:
                continue
            key = str(swarm_path)
            if key in seen_configs:
                continue
            seen_configs.add(key)
            found.append(swarm_path)
    return found


def _check_profile_isolation(project: ases_config.ProjectConfig) -> list[DoctorCheck]:
    """CONTAINERS (round 17 fix round 2), a reviewer finding on the first pass of this package:
    find_orphan_containers (the function _check_orphan_containers above also calls) matches "this project's
    own" Hermes profiles by NAME ALONE (project.roles.values()), because that is all a real container's
    labels ever carry (containers.py's own module docstring, "HARD CONSTRAINT, NOT A SUGGESTION"). Two ASES
    projects on this machine that configure the same profile name are not safe together once either has
    sandbox.enabled: project A's sweep only ever reads its OWN board, so it can see project B's live
    container as idle and stop it.

    This row is the best a single machine can verify without ASES having a real project registry (it has
    none): a WARN, never a FAIL (a false positive here costs nothing; a missed one costs a running
    container), for every OTHER project's config/swarm.yaml this can find (_sibling_swarm_configs) whose
    declared profile names overlap this project's own under a DIFFERENT project name. A sibling with the
    SAME project name is a worktree of this same project (every round-17 package worktree has its own
    config/swarm.yaml with name: ases) and is never a collision. Shown only while sandbox.enabled is true,
    matching _check_orphan_containers: the risk this warns about only exists once this project's own sweep
    can actually stop a container. Never raises: an unreadable or malformed sibling config is skipped, and
    an unexpected error in the scan itself is a WARN row, never a doctor crash."""
    if not bool(getattr(project, "sandbox_enabled", False)):
        return []
    try:
        project_profiles = {str(name) for name in (getattr(project, "roles", None) or {}).values() if name}
        collisions: list[str] = []
        checked = 0
        for swarm_path in _sibling_swarm_configs(project):
            try:
                other = ases_config.load_swarm_config(swarm_path)
            except Exception:  # noqa: BLE001 - not every config/swarm.yaml found this way even parses; skip it
                continue
            checked += 1
            if not other.name or other.name == project.name:
                continue
            other_profiles = {str(name) for name in (getattr(other, "roles", None) or {}).values() if name}
            shared = sorted(project_profiles & other_profiles)
            if shared:
                collisions.append(f"project \"{other.name}\" ({swarm_path}) also declares: {', '.join(shared)}")
    except Exception as exc:  # noqa: BLE001 - a doctor row never crashes the doctor
        return [DoctorCheck(
            "profile_isolation", "warn",
            f"could not check for a profile-name collision with another local project: {type(exc).__name__}: {exc}",
            _CONTAINER_IDS,
        )]
    if collisions:
        return [DoctorCheck(
            "profile_isolation", "warn",
            "this project's Hermes profile name(s) are also declared by another local project's config -- "
            "its orphan sweep and this one can each mistake the other's live container for its own idle one: "
            + "; ".join(collisions) + ". Give every project's roles distinct Hermes profile names.",
            _CONTAINER_IDS,
        )]
    return [DoctorCheck(
        "profile_isolation", "pass",
        f"no other local project's config/swarm.yaml (checked {checked}) declares one of this project's "
        f"{len(project_profiles)} Hermes profile name(s)",
        _CONTAINER_IDS,
    )]


def _check_model_registry(conn, models_config: dict) -> list[DoctorCheck]:
    """ASES-MOD-02, acceptance 22.4: one context_length[provider/model] row per registered model. PASS only
    when context_length is declared and sufficient. Otherwise: FAIL when the model is PINNED and
    models.classify_model_context rejects it -- the controller now refuses this at swarm approve/run
    pre-flight, so the run really cannot start (row name kept as `context_length[...]`, unchanged, so nothing
    downstream that keys off it breaks). Everything else -- an unpinned candidate the controller would
    reject, or a model classify_model_context accepts but whose context is still undeclared (a native
    provider with no declared context, trusted to Hermes's own knowledge/probing per that function's
    docstring) -- stays a WARN, exactly as before this round: informational, never blocking a start."""
    checks: list[DoctorCheck] = []
    records = models_mod.list_models(conn)
    if not records:
        return [DoctorCheck("model_registry", "fail", "no models declared in config/models.yaml", ("ASES-MOD-02",))]
    providers = models_config.get("providers") or {}
    for m in records:
        label = f"{m.provider}/{m.model}"
        if m.context_declared_and_sufficient:
            checks.append(DoctorCheck(
                f"context_length[{label}]", "pass",
                f"declared context {m.context_length} >= {models_mod.MINIMUM_CONTEXT_LENGTH}",
                ("ASES-MOD-02",),
            ))
        else:
            provider_type = (providers.get(m.provider) or {}).get("type")
            decision = models_mod.classify_model_context(m.context_length, provider_type)
            if m.pinned and not decision.accepted:
                checks.append(DoctorCheck(
                    f"context_length[{label}]", "fail",
                    f"pinned model {decision.status.replace('_', ' ')}: {decision.reason} -- the controller "
                    f"refuses this at swarm approve/run pre-flight, so the run cannot start (ASES-MOD-02, 22.4)",
                    ("ASES-MOD-02",),
                ))
            else:
                checks.append(DoctorCheck(
                    f"context_length[{label}]", "warn",
                    f"context length {'undeclared' if m.context_length is None else f'only {m.context_length}'} "
                    f"in config/models.yaml -- confirm >= {models_mod.MINIMUM_CONTEXT_LENGTH} before pinning this model",
                    ("ASES-MOD-02",),
                ))
        if m.smoke_tested:
            checks.append(DoctorCheck(
                f"smoke_test[{label}]", "pass", f"last smoke test passed at {m.smoke_test_at}", ("ASES-MOD-04",)
            ))
        else:
            checks.append(DoctorCheck(
                f"smoke_test[{label}]", "pending" if m.smoke_test_result is None else "warn",
                "no smoke test recorded yet -- run in Phase 2" if m.smoke_test_result is None
                else f"last smoke test FAILED at {m.smoke_test_at}: {m.smoke_test_detail}",
                ("ASES-MOD-04",),
            ))
    return checks


def _check_role_profiles(project: ases_config.ProjectConfig) -> DoctorCheck:
    profiles_root = project.hermes_native_home / "profiles"
    missing = []
    unconfigured = []
    for role, profile in project.roles.items():
        cfg = profiles_root / profile / "config.yaml"
        if not cfg.exists():
            missing.append(profile)
            continue
        text = cfg.read_text(encoding="utf-8")
        if "provider:" not in text or "default:" not in text:
            unconfigured.append(profile)
    if missing:
        return DoctorCheck("role_profiles", "fail", f"missing profiles: {missing}", ("ASES-ROL-02",))
    if unconfigured:
        return DoctorCheck("role_profiles", "warn", f"profiles with no model configured: {unconfigured}",
                            ("ASES-ROL-02",))
    return DoctorCheck(
        "role_profiles", "pass",
        f"all {len(project.roles)} role profiles exist and have a model configured: "
        f"{sorted(project.roles.values())}",
        ("ASES-ROL-02",),
    )


def _profile_model(project: ases_config.ProjectConfig, profile: str) -> tuple[str | None, str | None]:
    """(provider, model) as declared in that profile's config.yaml, or (None, None) if unreadable."""
    cfg_path = project.hermes_native_home / "profiles" / profile / "config.yaml"
    if not cfg_path.exists():
        return None, None
    provider = model = None
    for line in cfg_path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("provider:") and provider is None:
            provider = s.split(":", 1)[1].strip()
        elif s.startswith("default:") and model is None:
            model = s.split(":", 1)[1].strip()
    return provider, model


def _check_reviewer_diversity(project: ases_config.ProjectConfig) -> DoctorCheck:
    lead_profile = project.roles.get("lead")
    reviewer_profile = project.roles.get("reviewer")
    if not lead_profile or not reviewer_profile:
        return DoctorCheck("reviewer_diversity", "pending", "lead or reviewer role not mapped yet",
                            ("ASES-ROL-05",))
    lead_provider, lead_model = _profile_model(project, lead_profile)
    rev_provider, rev_model = _profile_model(project, reviewer_profile)
    if lead_provider is None or rev_provider is None:
        return DoctorCheck("reviewer_diversity", "warn", "could not read one or both profiles' config.yaml",
                            ("ASES-ROL-05",))
    if lead_provider == rev_provider:
        return DoctorCheck(
            "reviewer_diversity", "fail",
            f"lead ({lead_model} on {lead_provider}) and reviewer ({rev_model} on {rev_provider}) "
            "share the same provider -- ASES-ROL-05 wants a different provider, not just a different model",
            ("ASES-ROL-05",),
        )
    return DoctorCheck(
        "reviewer_diversity", "pass",
        f"lead={lead_model}@{lead_provider}, reviewer={rev_model}@{rev_provider} -- different providers",
        ("ASES-ROL-05",),
    )


def _lsp_install_summary(lsp_dir: pathlib.Path) -> str:
    """What a profile's lsp/ directory already holds, for a WARN row that names it without reading anything
    sensitive: package.json's own dependency names (the npm recipe Hermes's installer uses, agent/lsp/install.py),
    else the directory's own entry names. Never raises: an unreadable or malformed package.json falls back to the
    entry names."""
    try:
        entries = sorted(p.name for p in lsp_dir.iterdir())
    except OSError:
        return "(could not be listed)"
    package_json = lsp_dir / "package.json"
    if package_json.is_file():
        try:
            data = json.loads(package_json.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        deps = data.get("dependencies") if isinstance(data, dict) else None
        if isinstance(deps, dict) and deps:
            return ", ".join(sorted(str(k) for k in deps)) + " (from lsp/package.json)"
    return ", ".join(entries) if entries else "(empty)"


def _check_lsp_installed(project: ases_config.ProjectConfig) -> DoctorCheck:
    """ASES-DOC-04 (section 16 STOP CONDITION, category 4): Hermes's own lsp.install_strategy default ("auto")
    may already have put language-server files on disk for a profile before swarm init ever set
    install_strategy to "manual" (round 19, package PROFILEGUARDS; a real example: pyright auto-installed into
    coder-1 and reviewer, STOPDOC.md topic A item 1). Removing an already-installed server is a deletion this
    module never performs on its own: a WARN names what a profile's lsp/ directory already holds, so a person can
    decide whether to keep or remove it. A profile with no lsp/ directory, or an empty one, is silently fine."""
    profiles_root = project.hermes_native_home / "profiles"
    found: list[str] = []
    for profile in sorted({str(name) for name in (getattr(project, "roles", None) or {}).values() if name}):
        lsp_dir = profiles_root / profile / "lsp"
        try:
            populated = lsp_dir.is_dir() and any(lsp_dir.iterdir())
        except OSError:
            continue
        if populated:
            found.append(f"{profile}: {_lsp_install_summary(lsp_dir)}")
    if not found:
        return DoctorCheck(
            "lsp_install", "pass",
            "no ASES profile has a populated lsp/ directory (nothing Hermes auto-installed there)", _LSP_IDS,
        )
    return DoctorCheck(
        "lsp_install", "warn",
        "Hermes already installed language-server files (lsp.install_strategy defaults to 'auto'); swarm init "
        "never removes them, that stays a person's decision: " + "; ".join(found),
        _LSP_IDS,
    )


def _check_limits_table(models_config: dict) -> DoctorCheck:
    """ASES-CAP-01 / ASES-VER-01 (5.3, Appendix E): "swarm doctor MUST display the value it is using, the
    source URL and the checked date." The value and date were already shown; this also shows `source` (a
    URL from the blueprint's Appendix E, or one already written in config/models.yaml's own comments --
    config.py's _validate_verification_source_field is what keeps an invented one out). Not every provider
    has a source yet (some numbers, like xkiro's, are genuinely unpublished), so a missing one is a WARN,
    never a FAIL: the number itself may still be right, but nobody can re-check it without a page to check
    against."""
    lines = []
    missing_source = []
    for name, p in models_config.get("providers", {}).items():
        limits = p.get("limits", {})
        verified_on = p.get("verified_on", "?")
        source = p.get("source")
        if source:
            lines.append(f"{name}: {limits or '(no published cap)'} [verified {verified_on}, source {source}]")
        else:
            lines.append(f"{name}: {limits or '(no published cap)'} [verified {verified_on}, source unknown]")
            missing_source.append(name)
    detail = "; ".join(lines)
    if missing_source:
        detail += f" -- no source URL on record for: {', '.join(missing_source)} (ASES-VER-01)"
        return DoctorCheck("limits_displayed", "warn", detail, ("ASES-CAP-01", "ASES-VER-01"))
    return DoctorCheck("limits_displayed", "pass", detail, ("ASES-CAP-01", "ASES-VER-01"))


def _check_key_pooling(models_config: dict) -> DoctorCheck:
    """ASES-CFG-02/CFG-03 (section 10.2, verified_by: Inspection): both requirements are genuinely about the
    user's account-management behaviour, which ASES has no way to enforce (it cannot know whether two
    provider entries share a real-world account). What IS buildable from ASES's own config: two or more
    provider entries in config/models.yaml pointing at the same `key_env` (the NAME of the environment
    variable that holds the key, docs/operations.md section 7.2 -- ASES never holds the value) is a signal of
    exactly the "same-account key pool" anti-pattern the requirement warns about, because two DIFFERENT
    providers sharing one key are, in the case that matters, the same account under two names.

    A single provider used by several profiles (a coder and a reviewer both drawing on one OpenRouter key) is
    normal and is explicitly NOT what this warns about, so only a key_env shared ACROSS providers counts.
    Always a WARN, never a FAIL: two providers can legitimately share a key without being the pool this
    requirement means (Inspection, not a machine-checkable fact), and the row names only the key_env NAME
    (the same thing config/models.yaml itself shows), never a secret value -- it passes
    _check_no_secrets_in_output the same as every other row."""
    by_key_env: dict[str, list[str]] = {}
    for name, entry in (models_config.get("providers") or {}).items():
        if not isinstance(entry, dict):
            continue
        key_env = entry.get("key_env")
        if not key_env:  # None (served anonymously, e.g. opencode_free) or empty: nothing to pool
            continue
        by_key_env.setdefault(str(key_env), []).append(str(name))
    pooled = {key: sorted(names) for key, names in by_key_env.items() if len(names) > 1}
    if not pooled:
        return DoctorCheck(
            "key_pooling", "pass",
            "no two providers in config/models.yaml share the same key_env",
            ("ASES-CFG-02", "ASES-CFG-03"),
        )
    detail = "; ".join(f"{key}: {', '.join(names)}" for key, names in sorted(pooled.items()))
    return DoctorCheck(
        "key_pooling", "warn",
        f"provider(s) share a key_env in config/models.yaml, a same-account key-pool signal -- prefer one "
        f"key per provider and several distinct real providers instead, never a shared key across provider "
        f"names (ASES-CFG-02/CFG-03, docs/operations.md): {detail}",
        ("ASES-CFG-02", "ASES-CFG-03"),
    )


_KEY_EXPOSURE_IDS = ("ASES-CFG-04", "ASES-CFG-05")


def _check_provider_keys_not_exported(models_config: dict) -> DoctorCheck:
    """Blueprint p213: "Never export provider keys in the shell that launches the gateway or the
    controller." (ASES-CFG-04 and ASES-CFG-05 depend on it.)

    WARN, never FAIL: ASES-CFG-05 (procenv.scrubbed_environ) already strips every credential-shaped
    variable from any subprocess ASES itself starts on Hermes's behalf, so a key sitting in this
    process's own environment does not reach a worker launched through ASES today. But `swarm doctor`
    runs IN that same shell, and so would a gateway or controller a person starts by hand from it
    (exactly what p213 warns about) -- neither of those is scrubbed, so a key set here is a real, if
    not yet realised, exposure. Only the variable NAME is ever shown, never its value
    (_check_no_secrets_in_output double-checks that no row, this one included, leaks one).

    Also lists, as an informational note and never a WARN by itself, any OTHER credential-shaped
    variable present in the environment (procenv's own pattern -- key/token/secret/passw/credential/
    auth/cookie/session, case-insensitive -- so the definition of "credential-shaped" lives in exactly
    one place) that is not already one of the provider key_env names above, names only."""
    import os

    key_envs = sorted({
        str(entry["key_env"])
        for entry in (models_config.get("providers") or {}).values()
        if isinstance(entry, dict) and entry.get("key_env")
    })
    set_and_present = [name for name in key_envs if os.environ.get(name)]
    other_credential_shaped = sorted(
        name for name in os.environ
        if name not in key_envs and procenv_mod._CREDENTIAL_ENV.search(name)
    )
    if set_and_present:
        status = "warn"
        detail = (
            f"provider key_env variable(s) set in this process's own environment: {', '.join(set_and_present)} "
            "-- blueprint p213: \"Never export provider keys in the shell that launches the gateway or the "
            "controller.\" ASES-CFG-05 scrubs a worker ASES itself launches, but a gateway or controller "
            "started BY HAND from this same shell would inherit it; move it into Hermes credential storage "
            "or the approved egress mechanism instead."
        )
    else:
        status = "pass"
        detail = "no provider key_env variable from config/models.yaml is set in this process's environment"
    if other_credential_shaped:
        detail += f"; other credential-shaped variable(s) present (names only): {', '.join(other_credential_shaped)}"
    return DoctorCheck("provider_keys_not_exported", status, detail, _KEY_EXPOSURE_IDS)


def _check_no_secrets_in_output(report_text_so_far: str) -> DoctorCheck:
    import os
    secret_env_names = [
        n for n in os.environ
        if any(t in n.upper() for t in ("KEY", "TOKEN", "SECRET", "PASSWORD")) and os.environ[n]
    ]
    leaked = [n for n in secret_env_names if os.environ[n] in report_text_so_far]
    if leaked:
        return DoctorCheck("no_secrets_in_output", "fail", f"value of {leaked} appears in the report text!", ())
    return DoctorCheck(
        "no_secrets_in_output", "pass",
        f"checked {len(secret_env_names)} credential-shaped env var(s) against the report text: none leaked",
        (),
    )


def run(project: ases_config.ProjectConfig, models_config: dict, conn, *, repo: pathlib.Path | None = None) -> DoctorReport:
    """`repo` (round 10, package BASECHECK; round 15, package WORKERGIT): the project repository's path, for
    _check_log_all_ref_updates and _check_worktree_relative_paths. Keyword
    -only and optional so a caller that does not pass one (cmd_doctor without --repo; see that check's own
    docstring) keeps working unchanged, getting a "pending" row for it instead of a forced signature change."""
    profiles_mod, profiles_unavailable = _load_profiles_module()
    checks: list[DoctorCheck] = [
        _check_environment_decision(project),
        _check_not_under_onedrive(project),
        _check_git_longpaths(project),
        _check_log_all_ref_updates(repo),
        _check_worktree_relative_paths(repo, bool(getattr(project, "sandbox_enabled", False))),
        _check_leaked_worktrees(project, conn, repo),
        _check_gitattributes(project),
        _check_python_version(),
        _check_git_version(),
        _check_hermes_version(project),
        _check_hermes_doctor(),
        _check_gateway_dispatcher(),
        *_check_sandbox(project, models_config, profiles_mod),
        *_check_orphan_containers(project),
        *_check_profile_isolation(project),
        *_check_model_registry(conn, models_config),
        _check_role_profiles(project),
        _check_reviewer_diversity(project),
        _check_lsp_installed(project),
        *([profiles_unavailable] if profiles_unavailable is not None
          else _check_profile_state(project, models_config, profiles_mod)),
        *(_check_residual_risks(profiles_mod) if profiles_unavailable is None else []),
        _check_limits_table(models_config),
        _check_key_pooling(models_config),
        _check_provider_keys_not_exported(models_config),
    ]
    # The secrets check needs to see everything decided above it, so it runs last, over the detail text
    # of every other check plus the raw hermes doctor output already folded into hermes_doctor's detail.
    so_far = "\n".join(f"{c.name} {c.status} {c.detail}" for c in checks)
    checks.append(_check_no_secrets_in_output(so_far))
    return DoctorReport(tuple(checks))
