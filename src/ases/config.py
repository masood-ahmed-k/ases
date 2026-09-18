"""Load and validate ASES's own configuration (section 9.1: config.py).

Two files: config/swarm.yaml (project settings, concurrency, budgets) and config/models.yaml (provider
limits and model declarations, section 5.3/5.1). Both are plain YAML plus this module's validation --
no environment-variable overlay yet (Phase 1 doesn't need one; add it when a real deployment does).
"""
from __future__ import annotations

import dataclasses
import pathlib

import yaml

VALID_ENVIRONMENTS = {"native", "wsl2"}
VALID_DATA_CLASSES = {"public", "private", "confidential"}


class ConfigError(Exception):
    """A swarm.yaml or models.yaml value is missing, malformed, or fails validation."""


@dataclasses.dataclass(frozen=True)
class ProjectConfig:
    name: str
    environment: str
    data_class: str
    workspace_root: pathlib.Path
    ases_home: pathlib.Path
    board: str
    integration_branch: str
    concurrency: dict
    budgets: dict
    hermes_tested_version: str
    hermes_native_home: pathlib.Path


def _require(d: dict, path: str):
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise ConfigError(f"config/swarm.yaml is missing required key: {path}")
        cur = cur[part]
    return cur


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
        concurrency=_require(raw, "concurrency"),
        budgets=_require(raw, "budgets"),
        hermes_tested_version=_require(raw, "hermes.tested_version"),
        hermes_native_home=pathlib.Path(_require(raw, "hermes.native_home")),
    )


def load_models_config(path: str | pathlib.Path) -> dict:
    path = pathlib.Path(path)
    if not path.exists():
        raise ConfigError(f"models config not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if "providers" not in raw or "models" not in raw:
        raise ConfigError("config/models.yaml must have top-level 'providers' and 'models' keys")
    return raw


def db_path(project: ProjectConfig) -> pathlib.Path:
    return project.ases_home / "ases.db"
