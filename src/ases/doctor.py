"""`swarm doctor` check logic (acceptance test 22.1).

Kept separate from cli.py so the checks are unit-testable without going through argv. Each check
returns a DoctorCheck with an honest status: PASS/WARN/FAIL for things Phase 1 can actually verify,
PENDING for things that legitimately don't exist until a later phase (role profiles, the gateway
dispatcher, the Docker sandbox) -- a PENDING check is not a hidden failure, it's a true statement about
where the project is in the phase plan (section 16), and it must say so rather than pretend to be green.

Overall status is FAIL only if any single check is FAIL; WARN and PENDING never fail the run on their
own, matching the exit-code convention `hermes doctor` itself uses (0 = healthy enough to proceed).
"""
from __future__ import annotations

import dataclasses
import pathlib
import subprocess
import sys

from . import config as ases_config
from . import hermes as hermes_mod
from . import models as models_mod

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
            ["git", "-C", str(_repo_root()), "config", "--get", "core.longpaths"],
            capture_output=True, text=True, timeout=10,
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
        result = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=10)
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


def _check_docker_sandbox() -> DoctorCheck:
    return DoctorCheck(
        "docker_sandbox", "pending",
        "Docker terminal backend is a Phase 5 requirement (ASES-SEC-03), not checked before then",
        ("ASES-SEC-03",),
    )


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
        _check_docker_sandbox(),
        *_check_model_registry(conn),
        _check_role_profiles(project),
        _check_reviewer_diversity(project),
        _check_limits_table(models_config),
    ]
    # The secrets check needs to see everything decided above it, so it runs last, over the detail text
    # of every other check plus the raw hermes doctor output already folded into hermes_doctor's detail.
    so_far = "\n".join(f"{c.name} {c.status} {c.detail}" for c in checks)
    checks.append(_check_no_secrets_in_output(so_far))
    return DoctorReport(tuple(checks))
