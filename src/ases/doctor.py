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
import pathlib
import subprocess
import sys

from . import config as ases_config
from . import gitexec
from . import hermes as hermes_mod
from . import models as models_mod
from . import sandbox as sandbox_mod

Status = str  # "pass" | "warn" | "fail" | "pending"


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
_PROFILE_IDS = ("ASES-ROL-02", "ASES-ROL-07", "ASES-ARC-08")
# The roles that are not workers when profiles.desired_profiles cannot say: the lead plans and the reviewer only
# has the Kanban verdict tools and read access (ASES-ROL-05), so neither runs a worker's shell.
_NON_WORKER_ROLES = ("lead", "reviewer")


def _takes(func, name: str) -> bool:
    """Does `func` take a keyword argument called `name` (or any, through **kwargs)? False when its signature cannot
    be read."""
    try:
        parameters = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False
    return name in parameters or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())


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
        DoctorCheck(f"profile_state[{number}]", "warn", f"{problem} (`swarm init` shows the fix)", _PROFILE_IDS)
        for number, problem in enumerate(problems, start=1)
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


def _check_model_registry(conn) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    records = models_mod.list_models(conn)
    if not records:
        return [DoctorCheck("model_registry", "fail", "no models declared in config/models.yaml", ("ASES-MOD-02",))]
    for m in records:
        label = f"{m.provider}/{m.model}"
        if m.context_declared_and_sufficient:
            checks.append(DoctorCheck(
                f"context_length[{label}]", "pass",
                f"declared context {m.context_length} >= {models_mod.MINIMUM_CONTEXT_LENGTH}",
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


def _check_limits_table(models_config: dict) -> DoctorCheck:
    lines = []
    for name, p in models_config.get("providers", {}).items():
        limits = p.get("limits", {})
        lines.append(f"{name}: {limits or '(no published cap)'} [verified {p.get('verified_on', '?')}]")
    return DoctorCheck("limits_displayed", "pass", "; ".join(lines), ("ASES-CAP-01",))


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


def run(project: ases_config.ProjectConfig, models_config: dict, conn) -> DoctorReport:
    profiles_mod, profiles_unavailable = _load_profiles_module()
    checks: list[DoctorCheck] = [
        _check_environment_decision(project),
        _check_not_under_onedrive(project),
        _check_git_longpaths(project),
        _check_gitattributes(project),
        _check_python_version(),
        _check_git_version(),
        _check_hermes_version(project),
        _check_hermes_doctor(),
        _check_gateway_dispatcher(),
        *_check_sandbox(project, models_config, profiles_mod),
        *_check_model_registry(conn),
        _check_role_profiles(project),
        _check_reviewer_diversity(project),
        *([profiles_unavailable] if profiles_unavailable is not None
          else _check_profile_state(project, models_config, profiles_mod)),
        _check_limits_table(models_config),
        _check_key_pooling(models_config),
    ]
    # The secrets check needs to see everything decided above it, so it runs last, over the detail text
    # of every other check plus the raw hermes doctor output already folded into hermes_doctor's detail.
    so_far = "\n".join(f"{c.name} {c.status} {c.detail}" for c in checks)
    checks.append(_check_no_secrets_in_output(so_far))
    return DoctorReport(tuple(checks))
