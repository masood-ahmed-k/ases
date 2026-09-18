import pytest

from ases import config


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


VALID_SWARM = """
project:
  name: ases
  environment: native
  data_class: private
  workspace_root: C:/Users/x/ases-workspaces
  ases_home: C:/Users/x/ases/data
  board: default
  integration_branch: integration
concurrency:
  max_in_progress: 3
  per_profile: 1
  hard_max: 6
budgets:
  attempts_per_card: 3
  review_rounds_per_task: 3
  fix_cards_per_task: 2
  replans_per_project: 2
  max_cards: 40
  card_runtime_minutes: 45
  daily_reserve_percent: 10
  review_reserve_requests: 20
hermes:
  tested_version: "0.21.3"
  native_home: "C:/Users/x/AppData/Local/hermes"
"""


def test_loads_valid_config(tmp_path):
    p = _write(tmp_path / "swarm.yaml", VALID_SWARM)
    cfg = config.load_swarm_config(p)
    assert cfg.environment == "native"
    assert cfg.data_class == "private"
    assert cfg.budgets["max_cards"] == 40


def test_rejects_bad_environment(tmp_path):
    p = _write(tmp_path / "swarm.yaml", VALID_SWARM.replace("environment: native", "environment: macos"))
    with pytest.raises(config.ConfigError, match="environment"):
        config.load_swarm_config(p)


def test_rejects_bad_data_class(tmp_path):
    p = _write(tmp_path / "swarm.yaml", VALID_SWARM.replace("data_class: private", "data_class: secret"))
    with pytest.raises(config.ConfigError, match="data_class"):
        config.load_swarm_config(p)


def test_rejects_onedrive_workspace_root(tmp_path):
    bad = VALID_SWARM.replace(
        "workspace_root: C:/Users/x/ases-workspaces",
        "workspace_root: C:/Users/x/OneDrive/Desktop/ases-workspaces",
    )
    p = _write(tmp_path / "swarm.yaml", bad)
    with pytest.raises(config.ConfigError, match="OneDrive"):
        config.load_swarm_config(p)


def test_rejects_onedrive_ases_home(tmp_path):
    bad = VALID_SWARM.replace(
        "ases_home: C:/Users/x/ases/data", "ases_home: C:/Users/x/OneDrive/Desktop/ases/data"
    )
    p = _write(tmp_path / "swarm.yaml", bad)
    with pytest.raises(config.ConfigError, match="OneDrive"):
        config.load_swarm_config(p)


def test_missing_file_raises(tmp_path):
    with pytest.raises(config.ConfigError):
        config.load_swarm_config(tmp_path / "nope.yaml")


def test_missing_key_raises(tmp_path):
    p = _write(tmp_path / "swarm.yaml", "project:\n  name: ases\n")
    with pytest.raises(config.ConfigError, match="environment"):
        config.load_swarm_config(p)


def test_load_models_config_requires_both_keys(tmp_path):
    p = _write(tmp_path / "models.yaml", "providers: {}\n")
    with pytest.raises(config.ConfigError):
        config.load_models_config(p)
