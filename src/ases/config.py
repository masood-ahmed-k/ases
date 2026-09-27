"""Load and validate ASES's own configuration (section 9.1: config.py).

Two files: config/swarm.yaml (project settings, concurrency, budgets) and config/models.yaml (provider
limits and model declarations, section 5.3/5.1). Both are plain YAML plus this module's validation --
no environment-variable overlay yet (Phase 1 doesn't need one; add it when a real deployment does).

Two optional blocks of swarm.yaml have safe defaults when they are absent, so a config written before they
existed still loads: `sandbox:` (blueprint Appendix B, ASES-SEC-03: OFF until the user has Docker running and
says so) and `retention:` (ASES-OBS-02: "Transcripts and logs stay local under a retention setting", default
30 days). A typo in either block is an error, never a silent default: a retention key that is ignored keeps
source code on disk for longer than the user asked, and a sandbox key that is ignored weakens a boundary.
"""
from __future__ import annotations

import copy
import dataclasses
import datetime
import pathlib

import yaml

from . import sandbox as sandbox_mod

VALID_ENVIRONMENTS = {"native", "wsl2"}
VALID_DATA_CLASSES = {"public", "private", "confidential"}

# The sandbox: block as ProjectConfig exposes it. `enabled` is ASES's own switch (the blueprint's Appendix B has
# no such key): while it is false `swarm doctor` reports the sandbox checks as warnings and `swarm init` leaves
# the workers' terminal blocks alone, because Docker is not running yet. The other five are the Appendix B keys.
# image, cpu, memory_mb, pids_limit and extra_deny are optional and are carried through only when the file names
# them (sandbox.SandboxPolicy owns their meaning and their validation).
DEFAULT_SANDBOX = {
    "enabled": False,
    "terminal_backend": "docker",
    "network_default": False,
    "mount": "worktree_only",
    "forward_env": [],
    "network_exceptions": "explicit_allowlist",
}

# ASES-OBS-02: how many days ASES keeps what it writes under ases_home. Logs (and the worker transcripts that
# contain source code) are short lived; reports and stop reports are the audit trail, so they live longer.
DEFAULT_RETENTION = {"logs_days": 30, "reports_days": 90}


class ConfigError(Exception):
    """A swarm.yaml or models.yaml value is missing, malformed, or fails validation."""


@dataclasses.dataclass(frozen=True)
class ProjectConfig:
    """The settings of config/swarm.yaml. `sandbox` and `retention` come last and have defaults, so every place
    that builds a ProjectConfig by hand (the tests do) keeps working without naming them."""
    name: str
    environment: str
    data_class: str
    workspace_root: pathlib.Path
    ases_home: pathlib.Path
    board: str
    integration_branch: str
    roles: dict
    concurrency: dict
    budgets: dict
    hermes_tested_version: str
    hermes_native_home: pathlib.Path
    sandbox: dict = dataclasses.field(default_factory=lambda: copy.deepcopy(DEFAULT_SANDBOX))
    retention: dict = dataclasses.field(default_factory=lambda: dict(DEFAULT_RETENTION))

    @property
    def sandbox_enabled(self) -> bool:
        """True only when config/swarm.yaml says `sandbox: enabled: true`."""
        return bool(self.sandbox.get("enabled", False))

    @property
    def logs_days(self) -> int:
        return int(self.retention.get("logs_days", DEFAULT_RETENTION["logs_days"]))

    @property
    def reports_days(self) -> int:
        return int(self.retention.get("reports_days", DEFAULT_RETENTION["reports_days"]))

    def sandbox_policy_config(self) -> dict:
        """The sandbox block as sandbox.SandboxPolicy.from_config reads it: the document form
        {"sandbox": {...}} without `enabled`, which is ASES's switch and not a policy key."""
        block = {key: copy.deepcopy(value) for key, value in self.sandbox.items() if key != "enabled"}
        return {"sandbox": block}


def _require(d: dict, path: str):
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise ConfigError(f"config/swarm.yaml is missing required key: {path}")
        cur = cur[part]
    return cur


def _parse_sandbox(raw: dict) -> dict:
    """The `sandbox:` block with the defaults filled in, or ConfigError. Absent (or an empty `sandbox:`) means the
    safe defaults: the sandbox is not enabled. The Appendix B keys are validated by sandbox.SandboxPolicy, the one
    place that knows the rules (a backend other than docker, a mount other than worktree_only and
    network_default: true are all refused there), so the two cannot drift; only `enabled` is checked here. The block
    is handed over in its document form, {"sandbox": {...}}, because that is the form in which SandboxPolicy
    rejects an unknown key instead of quietly ignoring it."""
    block = raw.get("sandbox")
    if block is None:
        return copy.deepcopy(DEFAULT_SANDBOX)
    if not isinstance(block, dict):
        raise ConfigError("config/swarm.yaml: sandbox must be a mapping")
    settings = copy.deepcopy(block)
    enabled = settings.pop("enabled", DEFAULT_SANDBOX["enabled"])
    if not isinstance(enabled, bool):
        raise ConfigError(f"config/swarm.yaml: sandbox.enabled must be true or false, got {enabled!r}")
    try:
        sandbox_mod.SandboxPolicy.from_config({"sandbox": settings})
    except sandbox_mod.SandboxConfigError as exc:
        raise ConfigError(f"config/swarm.yaml: sandbox: {exc}") from exc
    merged = copy.deepcopy(DEFAULT_SANDBOX)
    merged.update(settings)
    merged["enabled"] = enabled
    return merged


def _parse_retention(raw: dict) -> dict:
    """The `retention:` block with the defaults filled in, or ConfigError. Each value is a whole number of days,
    at least 1: zero or a negative number would mean "delete everything", and a bool is refused although it is an
    int in Python (`logs_days: true` would quietly mean one day)."""
    block = raw.get("retention")
    if block is None:
        return dict(DEFAULT_RETENTION)
    if not isinstance(block, dict):
        raise ConfigError("config/swarm.yaml: retention must be a mapping")
    unknown = sorted(str(key) for key in block if key not in DEFAULT_RETENTION)
    if unknown:
        raise ConfigError(
            f"config/swarm.yaml: unknown retention key(s) {unknown}; the keys are {sorted(DEFAULT_RETENTION)}"
        )
    settings = dict(DEFAULT_RETENTION)
    for key in DEFAULT_RETENTION:
        if key not in block:
            continue
        value = block[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ConfigError(
                f"config/swarm.yaml: retention.{key} must be a whole number of days, 1 or more, got {value!r}"
            )
        settings[key] = value
    return settings


def load_swarm_config(path: str | pathlib.Path) -> ProjectConfig:
    path = pathlib.Path(path)
    if not path.exists():
        raise ConfigError(f"swarm config not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    environment = _require(raw, "project.environment")
    if environment not in VALID_ENVIRONMENTS:
        raise ConfigError(f"project.environment must be one of {sorted(VALID_ENVIRONMENTS)}, got {environment!r}")

    data_class = _require(raw, "project.data_class")
    if data_class not in VALID_DATA_CLASSES:
        raise ConfigError(f"project.data_class must be one of {sorted(VALID_DATA_CLASSES)}, got {data_class!r}")

    workspace_root = pathlib.Path(_require(raw, "project.workspace_root"))
    if "onedrive" in str(workspace_root).lower():
        # This is exactly the failure mode section 17.2 / ASES-ENV-03 warns about: cloud file sync
        # fighting with SQLite and Git. Refuse rather than silently corrupt state later.
        raise ConfigError(
            f"project.workspace_root ({workspace_root}) is under a OneDrive-looking path. "
            "Repositories, worktrees and databases must not live under OneDrive (ASES-ENV-03)."
        )

    ases_home = pathlib.Path(_require(raw, "project.ases_home"))
    if "onedrive" in str(ases_home).lower():
        raise ConfigError(f"project.ases_home ({ases_home}) is under a OneDrive-looking path (ASES-ENV-03).")

    return ProjectConfig(
        name=_require(raw, "project.name"),
        environment=environment,
        data_class=data_class,
        workspace_root=workspace_root,
        ases_home=ases_home,
        board=_require(raw, "project.board"),
        integration_branch=_require(raw, "project.integration_branch"),
        roles=_require(raw, "roles"),
        concurrency=_require(raw, "concurrency"),
        budgets=_require(raw, "budgets"),
        hermes_tested_version=_require(raw, "hermes.tested_version"),
        hermes_native_home=pathlib.Path(_require(raw, "hermes.native_home")),
        sandbox=_parse_sandbox(raw),
        retention=_parse_retention(raw),
    )


def _validate_data_policy_verification_fields(providers: dict) -> None:
    """ASES-PRV-04 / ASES-VER-01's convention (a source and a verification date, never invented): two
    OPTIONAL fields per provider entry, `data_policy_verified_at` (an ISO date string: WHEN a human checked
    the provider's `data_policy`) and `data_policy_source` (a short string: a URL or a note saying WHO/WHERE
    that check came from). Neither is required (a public-class project never needs them, and policy.py's
    check_data_class is where private/confidential actually enforce `data_policy_verified_at`'s presence);
    this only validates the SHAPE of whichever of the two is present, so a typo is caught at load time
    rather than silently accepted and only noticed when a private-class approval is refused for a reason
    that does not mention the real problem."""
    if not isinstance(providers, dict):
        return
    for name, entry in providers.items():
        if not isinstance(entry, dict):
            continue
        verified_at = entry.get("data_policy_verified_at")
        if verified_at is not None:
            if not isinstance(verified_at, str):
                raise ConfigError(
                    f"config/models.yaml: providers.{name}.data_policy_verified_at must be an ISO date "
                    f"string (quote it, e.g. \"2026-09-19\"), got {verified_at!r}"
                )
            try:
                datetime.date.fromisoformat(verified_at)
            except ValueError as exc:
                raise ConfigError(
                    f"config/models.yaml: providers.{name}.data_policy_verified_at is not a valid ISO "
                    f"date ({verified_at!r}): {exc}"
                ) from exc
        source = entry.get("data_policy_source")
        if source is not None and not isinstance(source, str):
            raise ConfigError(
                f"config/models.yaml: providers.{name}.data_policy_source must be a string (a URL or a "
                f"short note), got {source!r}"
            )


def _validate_verification_source_field(providers: dict) -> None:
    """ASES-VER-01 (section 5.3, Appendix E): "swarm doctor MUST display the value it is using, the source
    URL and the checked date." `verified_on` already carries the date; `source` is the optional companion
    field for the page the value came from -- a URL from the blueprint's Appendix E, or one already written
    in this file's own comments, never invented. Not every provider has one yet (some numbers are genuinely
    unpublished, e.g. xkiro's), so it is optional and swarm doctor WARNs rather than fails when it is
    missing; this only validates the SHAPE of whichever is present, same convention as
    data_policy_source above."""
    if not isinstance(providers, dict):
        return
    for name, entry in providers.items():
        if not isinstance(entry, dict):
            continue
        source = entry.get("source")
        if source is not None and not isinstance(source, str):
            raise ConfigError(
                f"config/models.yaml: providers.{name}.source must be a string (a URL from the blueprint's "
                f"Appendix E or already written in this file's comments), got {source!r}"
            )


def load_models_config(path: str | pathlib.Path) -> dict:
    path = pathlib.Path(path)
    if not path.exists():
        raise ConfigError(f"models config not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if "providers" not in raw or "models" not in raw:
        raise ConfigError("config/models.yaml must have top-level 'providers' and 'models' keys")
    _validate_data_policy_verification_fields(raw["providers"])
    _validate_verification_source_field(raw["providers"])
    return raw


def db_path(project: ProjectConfig) -> pathlib.Path:
    return project.ases_home / "ases.db"
