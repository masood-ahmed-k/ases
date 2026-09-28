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
roles:
  lead: lead
  coder: coder-1
  reviewer: reviewer
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


def test_load_models_config_returns_the_parsed_file_and_refuses_a_missing_one(tmp_path):
    p = _write(tmp_path / "models.yaml", "providers:\n  a: {}\nmodels: []\n")

    assert config.load_models_config(p) == {"providers": {"a": {}}, "models": []}
    with pytest.raises(config.ConfigError, match="not found"):
        config.load_models_config(tmp_path / "nope.yaml")


# --- providers.<name>.data_policy_verified_at / data_policy_source (ASES-PRV-04) ----------------


def test_data_policy_verification_fields_parse_when_present(tmp_path):
    p = _write(tmp_path / "models.yaml", (
        "providers:\n"
        "  a:\n"
        "    data_policy: no_training\n"
        "    data_policy_verified_at: \"2026-09-19\"\n"
        "    data_policy_source: \"https://example.com/privacy\"\n"
        "models: []\n"
    ))

    raw = config.load_models_config(p)

    assert raw["providers"]["a"]["data_policy_verified_at"] == "2026-09-19"
    assert raw["providers"]["a"]["data_policy_source"] == "https://example.com/privacy"


def test_data_policy_verification_fields_are_absent_safe(tmp_path):
    """No data_policy_verified_at/data_policy_source at all: fine to load (a public-class project never
    needs them; policy.check_data_class is what enforces them for private/confidential)."""
    p = _write(tmp_path / "models.yaml", "providers:\n  a:\n    data_policy: unknown\nmodels: []\n")

    raw = config.load_models_config(p)

    assert raw["providers"]["a"].get("data_policy_verified_at") is None
    assert raw["providers"]["a"].get("data_policy_source") is None


def test_a_malformed_verification_date_is_a_config_error_naming_the_provider(tmp_path):
    p = _write(tmp_path / "models.yaml", (
        "providers:\n  a:\n    data_policy: no_training\n    data_policy_verified_at: not-a-date\nmodels: []\n"
    ))

    with pytest.raises(config.ConfigError, match="providers.a.data_policy_verified_at"):
        config.load_models_config(p)


def test_an_unquoted_yaml_date_is_refused_not_silently_accepted(tmp_path):
    """An unquoted YAML date scalar parses as a datetime.date object, not a string: refused with a message
    that tells the author to quote it, rather than silently doing the wrong thing."""
    p = _write(tmp_path / "models.yaml", (
        "providers:\n  a:\n    data_policy: no_training\n    data_policy_verified_at: 2026-09-19\nmodels: []\n"
    ))

    with pytest.raises(config.ConfigError, match="providers.a.data_policy_verified_at"):
        config.load_models_config(p)


def test_a_non_string_verification_source_is_a_config_error(tmp_path):
    p = _write(tmp_path / "models.yaml", (
        "providers:\n  a:\n    data_policy: no_training\n    data_policy_source: 12345\nmodels: []\n"
    ))

    with pytest.raises(config.ConfigError, match="providers.a.data_policy_source"):
        config.load_models_config(p)


# --- providers.<name>.source (ASES-VER-01) -------------------------------------------------------


def test_verification_source_field_parses_when_present(tmp_path):
    p = _write(tmp_path / "models.yaml", (
        "providers:\n"
        "  a:\n"
        "    verified_on: \"2026-09-19\"\n"
        "    source: \"https://example.com/limits\"\n"
        "models: []\n"
    ))

    raw = config.load_models_config(p)

    assert raw["providers"]["a"]["source"] == "https://example.com/limits"


def test_verification_source_field_is_absent_safe(tmp_path):
    """No source at all: fine to load -- not every provider's numbers have a known page yet (xkiro's
    limits are genuinely unpublished), and swarm doctor WARNs on the missing source rather than
    refusing to load."""
    p = _write(tmp_path / "models.yaml", "providers:\n  a:\n    verified_on: \"2026-09-19\"\nmodels: []\n")

    raw = config.load_models_config(p)

    assert raw["providers"]["a"].get("source") is None


def test_a_non_string_verification_source_field_is_a_config_error(tmp_path):
    p = _write(tmp_path / "models.yaml", (
        "providers:\n  a:\n    verified_on: \"2026-09-19\"\n    source: 12345\nmodels: []\n"
    ))

    with pytest.raises(config.ConfigError, match="providers.a.source"):
        config.load_models_config(p)


def test_the_shipped_models_yaml_still_loads_and_documents_the_new_fields_as_comments(tmp_path):
    import pathlib

    path = pathlib.Path(__file__).resolve().parents[2] / "config" / "models.yaml"
    text = path.read_text(encoding="utf-8")
    raw = config.load_models_config(path)

    # documented as comments, not live values (a real verification date is a human decision, not this file's)
    assert "data_policy_verified_at" in text and "data_policy_source" in text
    for provider in raw["providers"].values():
        assert provider.get("data_policy_verified_at") is None
    assert text.isascii()


def test_the_shipped_models_yaml_openrouter_source_is_a_real_appendix_e_url(tmp_path):
    import pathlib

    path = pathlib.Path(__file__).resolve().parents[2] / "config" / "models.yaml"
    raw = config.load_models_config(path)

    assert raw["providers"]["openrouter"]["source"] == "https://openrouter.ai/docs/api-reference/limits"
    # xkiro and opencode_free publish no numeric limits; their sources (checked 2026-09-28, see each provider's
    # comment) are the providers' own pages saying so, and `limits` stays {} rather than a number read off a
    # promotional page.
    assert raw["providers"]["xkiro"]["source"] == "https://docs.xkiro.com/api/rate-limits/"
    assert raw["providers"]["xkiro"]["limits"] == {}
    assert raw["providers"]["opencode_free"]["source"] == "https://opencode.ai/docs/zen/"
    assert raw["providers"]["opencode_free"]["limits"] == {}


def test_db_path_is_ases_db_under_ases_home(tmp_path):
    cfg = config.load_swarm_config(_write(tmp_path / "swarm.yaml", VALID_SWARM))

    assert config.db_path(cfg) == cfg.ases_home / "ases.db"


# --- the sandbox: and retention: blocks (ASES-SEC-03, ASES-OBS-02) ------------------------------------


def _load(tmp_path, extra=""):
    return config.load_swarm_config(_write(tmp_path / "swarm.yaml", VALID_SWARM + extra))


def test_sandbox_and_retention_take_safe_defaults_when_the_file_has_neither(tmp_path):
    cfg = _load(tmp_path)

    assert cfg.sandbox == config.DEFAULT_SANDBOX
    assert cfg.sandbox_enabled is False  # Docker is off until the user starts it
    assert cfg.retention == {"logs_days": 30, "reports_days": 90}
    assert (cfg.logs_days, cfg.reports_days) == (30, 90)


def test_the_defaults_are_copies_so_one_config_cannot_change_another(tmp_path):
    first = _load(tmp_path)
    first.sandbox["forward_env"].append("HOME_DIR")
    first.retention["logs_days"] = 1

    second = _load(tmp_path)

    assert second.sandbox["forward_env"] == []
    assert config.DEFAULT_SANDBOX["forward_env"] == []
    assert second.retention["logs_days"] == 30
    assert config.DEFAULT_RETENTION["logs_days"] == 30


def test_empty_blocks_mean_the_defaults(tmp_path):
    cfg = _load(tmp_path, "sandbox:\nretention:\n")

    assert cfg.sandbox == config.DEFAULT_SANDBOX
    assert cfg.retention == config.DEFAULT_RETENTION


def test_a_sandbox_block_is_read_and_its_optional_keys_are_carried_through(tmp_path):
    cfg = _load(tmp_path, "sandbox:\n  enabled: true\n  image: python:3.11\n  cpu: 1\n  memory_mb: 2048\n")

    assert cfg.sandbox_enabled is True
    assert cfg.sandbox["image"] == "python:3.11"
    assert (cfg.sandbox["cpu"], cfg.sandbox["memory_mb"]) == (1, 2048)
    assert cfg.sandbox["mount"] == "worktree_only"  # a key the file did not name keeps its default


def test_the_sandbox_policy_config_is_the_document_form_without_the_enabled_switch(tmp_path):
    from ases import sandbox

    cfg = _load(tmp_path, "sandbox:\n  enabled: true\n  image: python:3.11\n")

    document = cfg.sandbox_policy_config()

    assert "enabled" not in document["sandbox"]
    assert document["sandbox"]["terminal_backend"] == "docker"
    assert sandbox.SandboxPolicy.from_config(document).image == "python:3.11"


@pytest.mark.parametrize("block, message", [
    ("sandbox: yes\n", "sandbox must be a mapping"),
    ("sandbox:\n  enabled: 'true'\n", "sandbox.enabled must be true or false"),
    ("sandbox:\n  enabled: 1\n", "sandbox.enabled must be true or false"),
    ("sandbox:\n  terminal_backend: local\n", "docker"),
    ("sandbox:\n  network_default: true\n", "network_default"),
    ("sandbox:\n  mount: everything\n", "worktree_only"),
    ("sandbox:\n  network_exceptions: anything\n", "explicit_allowlist"),
    ("sandbox:\n  enabledd: true\n", "unknown sandbox key"),
    ("sandbox:\n  forward_env: [API_KEY]\n", "credential"),
    ("sandbox:\n  cpu: 0\n", "cpu"),
])
def test_a_bad_sandbox_block_is_refused_not_ignored(tmp_path, block, message):
    with pytest.raises(config.ConfigError, match=message):
        _load(tmp_path, block)


@pytest.mark.parametrize("block, message", [
    ("retention: 30\n", "retention must be a mapping"),
    ("retention:\n  logs_day: 30\n", "unknown retention key"),
    ("retention:\n  logs_days: 0\n", "logs_days"),
    ("retention:\n  logs_days: -5\n", "logs_days"),
    ("retention:\n  reports_days: true\n", "reports_days"),
    ("retention:\n  reports_days: '90'\n", "reports_days"),
    ("retention:\n  reports_days: 1.5\n", "reports_days"),
])
def test_a_bad_retention_block_is_refused_not_ignored(tmp_path, block, message):
    with pytest.raises(config.ConfigError, match=message):
        _load(tmp_path, block)


def test_a_partial_retention_block_keeps_the_other_default(tmp_path):
    cfg = _load(tmp_path, "retention:\n  logs_days: 7\n")

    assert (cfg.logs_days, cfg.reports_days) == (7, 90)


def test_a_project_config_built_by_hand_still_gets_both_blocks():
    """The tests of other modules build ProjectConfig with the twelve original fields; the new two must default."""
    cfg = config.ProjectConfig(
        name="x", environment="native", data_class="public", workspace_root="w", ases_home="h", board="b",
        integration_branch="integration", roles={}, concurrency={}, budgets={}, hermes_tested_version="0.21.3",
        hermes_native_home="n",
    )

    assert cfg.sandbox_enabled is False
    assert cfg.retention == config.DEFAULT_RETENTION


def test_the_shipped_swarm_yaml_documents_both_blocks_and_keeps_the_sandbox_off():
    import pathlib

    path = pathlib.Path(__file__).resolve().parents[2] / "config" / "swarm.yaml"
    text = path.read_text(encoding="utf-8")

    cfg = config.load_swarm_config(path)

    assert cfg.sandbox_enabled is False
    assert cfg.sandbox["terminal_backend"] == "docker" and cfg.sandbox["mount"] == "worktree_only"
    assert cfg.sandbox["network_default"] is False and cfg.sandbox["forward_env"] == []
    assert (cfg.logs_days, cfg.reports_days) == (30, 90)
    assert "\nsandbox:\n" in text and "\nretention:\n" in text
    assert text.isascii()


def test_the_shipped_swarm_yaml_names_a_pinned_sandbox_image():
    """SANDBOXIMG (round 15): config/swarm.yaml sandbox.image names ases-sandbox:py311-1, built from
    docker/sandbox/Dockerfile, and that name is a PINNED reference (sandbox._unpinned_reason has nothing to say
    about it: a tag other than latest, same rule doctor's sandbox_image_configured check applies), so `swarm
    doctor`'s image check is meaningful rather than silently accepting a moving target."""
    import pathlib

    from ases import sandbox as sandbox_mod

    path = pathlib.Path(__file__).resolve().parents[2] / "config" / "swarm.yaml"
    cfg = config.load_swarm_config(path)

    assert cfg.sandbox["image"] == "ases-sandbox:py311-1"
    policy = sandbox_mod.SandboxPolicy.from_config(cfg.sandbox_policy_config())
    assert policy.image == "ases-sandbox:py311-1"
    assert sandbox_mod._unpinned_reason(policy.image) is None
    assert sandbox_mod._image_problems({"docker_image": policy.image}) == []
