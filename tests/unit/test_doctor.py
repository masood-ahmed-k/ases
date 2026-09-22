"""Doctor logic tested with hermes.py mocked out -- no subprocess, no real Hermes needed. The real-Hermes
path is covered separately by tests/integration/test_doctor_real_hermes.py."""
import dataclasses
import pathlib
import sys
import types

import pytest

from ases import config, db, doctor, hermes, models, sandbox

try:  # another package's module: imported here, before the autouse fixture below swaps a stub into sys.modules
    from ases import profiles as real_profiles
except ImportError:
    real_profiles = None

SWARM_YAML = """
project:
  name: ases
  environment: native
  data_class: private
  workspace_root: {ws}
  ases_home: {home}
  board: default
  integration_branch: integration
roles: {{lead: lead, coder: coder-1, reviewer: reviewer}}
concurrency: {{max_in_progress: 3, per_profile: 1, hard_max: 6}}
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
  native_home: "{home}/hermes-home"
"""

MODELS_CONFIG = {
    "providers": {"unorouter": {"data_policy": "forwards_to_upstreams_that_may_train"}},
    "models": [
        {"provider": "unorouter", "model": "glm-5.3-thinking:free", "context_length": None,
         "tool_calling": None, "role_class": "lead", "pinned": True},
    ],
}


def _project(tmp_path, extra=""):
    p = tmp_path / "swarm.yaml"
    # as_posix(): Windows tmp_path backslashes inside a double-quoted YAML scalar are escape sequences,
    # not path separators -- forward slashes side-step that (same reason config/swarm.yaml uses them).
    p.write_text(
        SWARM_YAML.format(ws=(tmp_path / "ws").as_posix(), home=(tmp_path / "home").as_posix()) + extra,
        encoding="utf-8",
    )
    return config.load_swarm_config(p)


class _World:
    """What the new rows read from other modules, recorded so a test can say what the doctor asked them."""

    def __init__(self):
        self.sandbox_rows = [("sandbox_docker", False, "docker CLI not found on PATH")]
        self.sandbox_calls = []
        self.verify_problems = []
        self.verify_calls = []
        self.specs = []


@pytest.fixture(autouse=True)
def world(monkeypatch):
    """No test in this file may reach a real Docker daemon or the real profiles module: sandbox.doctor_checks
    and ases.profiles are replaced by fakes whose answers each test can set."""
    state = _World()

    def fake_checks(policy, profile_dirs, **kwargs):
        state.sandbox_calls.append((policy, [pathlib.Path(d) for d in profile_dirs], kwargs))
        return list(state.sandbox_rows)

    def fake_verify(project, models_config, hermes_home, prompts_dir, **kwargs):
        state.verify_calls.append((project, models_config, hermes_home, prompts_dir, kwargs))
        return list(state.verify_problems)

    monkeypatch.setattr(sandbox, "doctor_checks", fake_checks)
    stub = types.ModuleType("ases.profiles")
    stub.verify_state = fake_verify
    stub.desired_profiles = lambda project, models_config: list(state.specs)
    monkeypatch.setitem(sys.modules, "ases.profiles", stub)
    state.profiles = stub
    return state


def _stub_hermes(monkeypatch):
    monkeypatch.setattr(hermes, "hermes_version", lambda: "0.21.3")
    monkeypatch.setattr(hermes, "run_doctor", lambda **_: hermes.DoctorResult(True, 0, "", (), ()))
    monkeypatch.setattr(hermes, "gateway_status", lambda **_: hermes.GatewayStatus(False, "not running"))


def _run(tmp_path, monkeypatch, extra=""):
    _stub_hermes(monkeypatch)
    project = _project(tmp_path, extra)
    conn = db.connect(config.db_path(project))
    models.sync_from_config(conn, MODELS_CONFIG)
    report = doctor.run(project, MODELS_CONFIG, conn)
    return project, report, {c.name: c for c in report.checks}


def test_model_context_check_warns_when_undeclared(tmp_path, monkeypatch):
    monkeypatch.setattr(hermes, "hermes_version", lambda: "0.21.3")
    monkeypatch.setattr(hermes, "run_doctor", lambda **_: hermes.DoctorResult(True, 0, "", (), ()))
    monkeypatch.setattr(hermes, "gateway_status", lambda **_: hermes.GatewayStatus(False, "not running"))

    project = _project(tmp_path)
    conn = db.connect(config.db_path(project))
    models.sync_from_config(conn, MODELS_CONFIG)

    report = doctor.run(project, MODELS_CONFIG, conn)
    by_name = {c.name: c for c in report.checks}
    assert by_name["context_length[unorouter/glm-5.3-thinking:free]"].status == "warn"
    assert by_name["smoke_test[unorouter/glm-5.3-thinking:free]"].status == "pending"
    assert "ASES-MOD-02" in by_name["context_length[unorouter/glm-5.3-thinking:free]"].requirement_ids


def _make_profile(home: object, name: str, provider: str, model: str) -> None:
    import pathlib
    d = pathlib.Path(str(home)) / "profiles" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.yaml").write_text(f"model:\n  default: {model}\n  provider: {provider}\n", encoding="utf-8")


def test_report_is_healthy_when_only_warn_and_pending(tmp_path, monkeypatch):
    monkeypatch.setattr(hermes, "hermes_version", lambda: "0.21.3")
    monkeypatch.setattr(hermes, "run_doctor", lambda **_: hermes.DoctorResult(True, 0, "", (), ()))
    monkeypatch.setattr(hermes, "gateway_status", lambda **_: hermes.GatewayStatus(False, "not running"))

    project = _project(tmp_path)
    _make_profile(project.hermes_native_home, "lead", "unorouter", "glm-5.3-thinking:free")
    _make_profile(project.hermes_native_home, "coder-1", "unorouter", "qwen3.8-27b:free")
    _make_profile(project.hermes_native_home, "reviewer", "openrouter", "cohere/north-mini-code:free")
    conn = db.connect(config.db_path(project))
    models.sync_from_config(conn, MODELS_CONFIG)

    report = doctor.run(project, MODELS_CONFIG, conn)
    assert report.ok is True
    assert report.exit_code == 0
    assert any(c.status == "warn" for c in report.checks)   # the undeclared context length
    assert any(c.status == "pending" for c in report.checks)  # gateway not running, etc.


def test_role_profiles_fail_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(hermes, "hermes_version", lambda: "0.21.3")
    monkeypatch.setattr(hermes, "run_doctor", lambda **_: hermes.DoctorResult(True, 0, "", (), ()))
    monkeypatch.setattr(hermes, "gateway_status", lambda **_: hermes.GatewayStatus(False, ""))
    project = _project(tmp_path)
    conn = db.connect(config.db_path(project))
    models.sync_from_config(conn, MODELS_CONFIG)

    report = doctor.run(project, MODELS_CONFIG, conn)
    by_name = {c.name: c for c in report.checks}
    assert by_name["role_profiles"].status == "fail"
    assert report.ok is False


def test_reviewer_diversity_fails_when_same_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(hermes, "hermes_version", lambda: "0.21.3")
    monkeypatch.setattr(hermes, "run_doctor", lambda **_: hermes.DoctorResult(True, 0, "", (), ()))
    monkeypatch.setattr(hermes, "gateway_status", lambda **_: hermes.GatewayStatus(False, ""))
    project = _project(tmp_path)
    _make_profile(project.hermes_native_home, "lead", "unorouter", "glm-5.3-thinking:free")
    _make_profile(project.hermes_native_home, "coder-1", "unorouter", "qwen3.8-27b:free")
    _make_profile(project.hermes_native_home, "reviewer", "unorouter", "glm-5.3-flash-thinking:free")
    conn = db.connect(config.db_path(project))
    models.sync_from_config(conn, MODELS_CONFIG)

    report = doctor.run(project, MODELS_CONFIG, conn)
    by_name = {c.name: c for c in report.checks}
    assert by_name["reviewer_diversity"].status == "fail"


def test_reviewer_diversity_passes_when_different_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(hermes, "hermes_version", lambda: "0.21.3")
    monkeypatch.setattr(hermes, "run_doctor", lambda **_: hermes.DoctorResult(True, 0, "", (), ()))
    monkeypatch.setattr(hermes, "gateway_status", lambda **_: hermes.GatewayStatus(False, ""))
    project = _project(tmp_path)
    _make_profile(project.hermes_native_home, "lead", "unorouter", "glm-5.3-thinking:free")
    _make_profile(project.hermes_native_home, "coder-1", "unorouter", "qwen3.8-27b:free")
    _make_profile(project.hermes_native_home, "reviewer", "openrouter", "cohere/north-mini-code:free")
    conn = db.connect(config.db_path(project))
    models.sync_from_config(conn, MODELS_CONFIG)

    report = doctor.run(project, MODELS_CONFIG, conn)
    by_name = {c.name: c for c in report.checks}
    assert by_name["reviewer_diversity"].status == "pass"


def test_report_fails_when_hermes_doctor_is_unhealthy(tmp_path, monkeypatch):
    monkeypatch.setattr(hermes, "hermes_version", lambda: "0.21.3")
    monkeypatch.setattr(
        hermes, "run_doctor",
        lambda **_: hermes.DoctorResult(False, 1, "x config broken", (), ("x config broken",)),
    )
    monkeypatch.setattr(hermes, "gateway_status", lambda **_: hermes.GatewayStatus(False, "not running"))

    project = _project(tmp_path)
    conn = db.connect(config.db_path(project))
    models.sync_from_config(conn, MODELS_CONFIG)

    report = doctor.run(project, MODELS_CONFIG, conn)
    assert report.ok is False
    assert report.exit_code == 1


def test_report_fails_when_hermes_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr(hermes, "hermes_version", lambda: None)
    monkeypatch.setattr(hermes, "run_doctor", lambda **_: hermes.DoctorResult(False, None, "", (), ()))
    monkeypatch.setattr(hermes, "gateway_status", lambda **_: hermes.GatewayStatus(False, ""))

    project = _project(tmp_path)
    conn = db.connect(config.db_path(project))
    models.sync_from_config(conn, MODELS_CONFIG)

    report = doctor.run(project, MODELS_CONFIG, conn)
    by_name = {c.name: c for c in report.checks}
    assert by_name["hermes_version"].status == "fail"
    assert report.ok is False


def test_no_secrets_check_flags_a_leaked_value(monkeypatch):
    monkeypatch.setenv("SOME_TEST_API_KEY", "totally-secret-value-123")
    check = doctor._check_no_secrets_in_output("report says totally-secret-value-123 somewhere")
    assert check.status == "fail"


def test_no_secrets_check_passes_when_clean(monkeypatch):
    monkeypatch.setenv("SOME_TEST_API_KEY", "totally-secret-value-123")
    check = doctor._check_no_secrets_in_output("report contains nothing sensitive")
    assert check.status == "pass"


# --- ASES-CFG-02/CFG-03: same-account key pooling, detected as a shared key_env across providers -----------


def test_key_pooling_passes_when_no_two_providers_share_a_key_env():
    models_config = {
        "providers": {
            "a": {"key_env": "A_API_KEY"},
            "b": {"key_env": "B_API_KEY"},
            "anonymous": {"key_env": None},  # served with no key at all (opencode_free): never flagged
        },
    }

    check = doctor._check_key_pooling(models_config)

    assert check.status == "pass"
    assert check.requirement_ids == ("ASES-CFG-02", "ASES-CFG-03")


def test_key_pooling_warns_when_two_different_providers_share_a_key_env():
    models_config = {
        "providers": {
            "openrouter": {"key_env": "SHARED_KEY"},
            "some-other-router": {"key_env": "SHARED_KEY"},
            "unpooled": {"key_env": "UNPOOLED_API_KEY"},
        },
    }

    check = doctor._check_key_pooling(models_config)

    assert check.status == "warn"  # advisory only, never a FAIL: Inspection, not a machine-checkable fact
    assert "SHARED_KEY" in check.detail
    assert "openrouter" in check.detail and "some-other-router" in check.detail
    assert "unpooled" not in check.detail and "UNPOOLED_API_KEY" not in check.detail


def test_key_pooling_does_not_flag_one_provider_used_by_multiple_profiles():
    """A single provider is one config entry regardless of how many Hermes profiles draw on its key -- that
    is normal (a coder and a reviewer both on the same OpenRouter key) and is explicitly not what
    ASES-CFG-02/03 warns about; only a key_env shared ACROSS DIFFERENT provider entries counts."""
    models_config = {"providers": {"openrouter": {"key_env": "OPENROUTER_API_KEY"}}}

    check = doctor._check_key_pooling(models_config)

    assert check.status == "pass"


def test_key_pooling_never_prints_a_secret_value(monkeypatch):
    monkeypatch.setenv("SHARED_KEY", "totally-secret-value-123")
    models_config = {"providers": {
        "a": {"key_env": "SHARED_KEY"}, "b": {"key_env": "SHARED_KEY"},
    }}

    check = doctor._check_key_pooling(models_config)
    no_secrets = doctor._check_no_secrets_in_output(check.detail)

    assert check.status == "warn"
    assert "totally-secret-value-123" not in check.detail  # only the key_env NAME is shown, never its value
    assert no_secrets.status == "pass"


def test_key_pooling_is_wired_into_the_report_and_warns_on_a_real_pool(tmp_path, monkeypatch):
    _stub_hermes(monkeypatch)
    project = _project(tmp_path)
    conn = db.connect(config.db_path(project))
    pooled_models_config = {
        **MODELS_CONFIG,
        "providers": {
            **MODELS_CONFIG["providers"],
            "unorouter-2": {"key_env": "SHARED_KEY", "data_policy": "unknown"},
            "unorouter-3": {"key_env": "SHARED_KEY", "data_policy": "unknown"},
        },
    }
    models.sync_from_config(conn, pooled_models_config)

    report = doctor.run(project, pooled_models_config, conn)

    by_name = {c.name: c for c in report.checks}
    assert by_name["key_pooling"].status == "warn"
    assert "unorouter-2" in by_name["key_pooling"].detail and "unorouter-3" in by_name["key_pooling"].detail
    assert report.checks[-1].name == "no_secrets_in_output"  # the secrets check still runs last


# --- the rows other modules feed: profiles.verify_state and sandbox.doctor_checks ---------------------------


def _run_healthy(tmp_path, monkeypatch, extra=""):
    """doctor.run on a machine where the three role profiles exist with different providers for lead and
    reviewer, so nothing else in the report is a FAIL and a FAIL can only come from the rows under test."""
    _stub_hermes(monkeypatch)
    project = _project(tmp_path, extra)
    _make_profile(project.hermes_native_home, "lead", "unorouter", "glm-5.3-thinking:free")
    _make_profile(project.hermes_native_home, "coder-1", "unorouter", "qwen3.8-27b:free")
    _make_profile(project.hermes_native_home, "reviewer", "openrouter", "cohere/north-mini-code:free")
    conn = db.connect(config.db_path(project))
    models.sync_from_config(conn, MODELS_CONFIG)
    report = doctor.run(project, MODELS_CONFIG, conn)
    return project, report, {c.name: c for c in report.checks}


ENABLED = "sandbox:\n  enabled: true\n  image: python:3.11\n"


def test_each_profile_problem_is_one_warn_row_and_never_a_fail(tmp_path, monkeypatch, world):
    world.verify_problems = ["worker coder-1 still has the memory toolset", "reviewer has a terminal toolset"]

    _, report, by_name = _run_healthy(tmp_path, monkeypatch)

    assert by_name["profile_state[1]"].status == "warn"
    assert "memory toolset" in by_name["profile_state[1]"].detail
    assert by_name["profile_state[2]"].status == "warn"
    assert "terminal toolset" in by_name["profile_state[2]"].detail
    assert "profile_state" not in by_name  # the PASS row is only for a clean result
    assert report.ok is True  # a machine that has not had swarm init run on it is still usable


def test_no_profile_problems_is_one_pass_row(tmp_path, monkeypatch, world):
    _, report, by_name = _run_healthy(tmp_path, monkeypatch)

    assert by_name["profile_state"].status == "pass"
    assert not any(name.startswith("profile_state[") for name in by_name)


def test_the_profile_rows_cite_the_requirements_they_check(tmp_path, monkeypatch, world):
    world.verify_problems = ["a problem"]

    _, _, by_name = _run_healthy(tmp_path, monkeypatch)

    assert {"ASES-ROL-02", "ASES-ROL-07", "ASES-ARC-08"} <= set(by_name["profile_state[1]"].requirement_ids)


def test_verify_state_is_asked_about_the_hermes_home_the_prompts_and_the_sandbox_switch(tmp_path, monkeypatch, world):
    project, _, _ = _run_healthy(tmp_path, monkeypatch)

    (asked_project, models_config, hermes_home, prompts_dir, kwargs), = world.verify_calls
    assert asked_project is project and models_config is MODELS_CONFIG
    assert hermes_home == project.hermes_native_home
    assert prompts_dir == doctor._repo_root() / "prompts"
    assert kwargs == {"sandbox_enabled": False}


def test_verify_state_learns_that_the_sandbox_is_enabled_and_gets_the_policy_of_the_config(tmp_path, monkeypatch, world):
    _run_healthy(tmp_path, monkeypatch, ENABLED)

    kwargs = world.verify_calls[0][4]
    assert kwargs["sandbox_enabled"] is True
    assert isinstance(kwargs["policy"], sandbox.SandboxPolicy) and kwargs["policy"].image == "python:3.11"


def test_verify_state_is_not_given_a_policy_it_does_not_take(tmp_path, monkeypatch, world):
    seen = []

    def strict(project, models_config, hermes_home, prompts_dir, *, sandbox_enabled=False):
        seen.append(sandbox_enabled)
        return []

    world.profiles.verify_state = strict

    _, report, by_name = _run_healthy(tmp_path, monkeypatch, ENABLED)

    assert seen == [True] and by_name["profile_state"].status == "pass"


def test_verify_state_is_still_asked_when_the_sandbox_block_cannot_make_a_policy(tmp_path, monkeypatch, world):
    project = dataclasses.replace(_project(tmp_path), sandbox={"enabled": True, "network_default": True})
    _stub_hermes(monkeypatch)
    conn = db.connect(config.db_path(project))

    report = doctor.run(project, MODELS_CONFIG, conn)

    assert world.verify_calls[0][4] == {"sandbox_enabled": True}  # no policy: its own row (sandbox_config) says why
    assert {c.name: c for c in report.checks}["sandbox_config"].status == "fail"


def test_a_missing_profiles_module_is_a_pending_row_and_the_doctor_carries_on(tmp_path, monkeypatch, world):
    monkeypatch.setitem(sys.modules, "ases.profiles", None)  # `import ases.profiles` now raises ImportError

    _, report, by_name = _run_healthy(tmp_path, monkeypatch)

    assert by_name["profile_state"].status == "pending"
    assert "not part of this build" in by_name["profile_state"].detail
    assert report.ok is True
    assert "sandbox_enabled" in by_name  # the other new rows were not lost


def test_a_verify_state_that_raises_is_a_warn_row_not_a_crash(tmp_path, monkeypatch, world):
    def boom(*args, **kwargs):
        raise RuntimeError("config.yaml is not valid YAML")

    world.profiles.verify_state = boom

    _, report, by_name = _run_healthy(tmp_path, monkeypatch)

    assert by_name["profile_state"].status == "warn"
    assert "RuntimeError" in by_name["profile_state"].detail and "not valid YAML" in by_name["profile_state"].detail
    assert report.ok is True


def test_sandbox_rows_are_warnings_while_the_sandbox_is_not_enabled(tmp_path, monkeypatch, world):
    world.sandbox_rows = [
        ("sandbox_docker", False, "docker CLI not found on PATH"),
        ("sandbox_profile[coder-1]", True, "terminal block is compliant"),
    ]

    _, report, by_name = _run_healthy(tmp_path, monkeypatch)

    assert by_name["sandbox_enabled"].status == "pending"
    assert "not enabled" in by_name["sandbox_enabled"].detail
    assert by_name["sandbox_docker"].status == "warn"
    assert "docker CLI not found on PATH" in by_name["sandbox_docker"].detail
    assert "only a warning" in by_name["sandbox_docker"].detail
    assert by_name["sandbox_profile[coder-1]"].status == "pass"
    assert {"ASES-SEC-03", "ASES-CFG-04"} <= set(by_name["sandbox_docker"].requirement_ids)
    assert report.ok is True and report.exit_code == 0


def test_sandbox_rows_fail_once_the_config_says_the_sandbox_is_enabled(tmp_path, monkeypatch, world):
    world.sandbox_rows = [
        ("sandbox_docker", False, "docker daemon not reachable"),
        ("sandbox_profile[coder-1]", True, "terminal block is compliant"),
    ]

    _, report, by_name = _run_healthy(tmp_path, monkeypatch, ENABLED)

    assert by_name["sandbox_enabled"].status == "pass"
    assert by_name["sandbox_docker"].status == "fail"
    assert "only a warning" not in by_name["sandbox_docker"].detail
    assert by_name["sandbox_profile[coder-1]"].status == "pass"
    assert report.ok is False and report.exit_code == 1


def test_an_enabled_sandbox_with_every_check_green_is_healthy(tmp_path, monkeypatch, world):
    world.sandbox_rows = [("sandbox_docker", True, "docker daemon reachable")]

    _, report, by_name = _run_healthy(tmp_path, monkeypatch, ENABLED)

    assert by_name["sandbox_docker"].status == "pass"
    assert report.ok is True


def test_an_enabled_sandbox_with_no_image_fails_its_own_row(tmp_path, monkeypatch, world):
    world.sandbox_rows = []

    _, report, by_name = _run_healthy(tmp_path, monkeypatch, "sandbox:\n  enabled: true\n")

    assert by_name["sandbox_image_configured"].status == "fail"
    assert "no sandbox.image" in by_name["sandbox_image_configured"].detail
    assert report.ok is False


def test_the_docker_placeholder_row_is_gone(tmp_path, monkeypatch, world):
    _, _, by_name = _run_healthy(tmp_path, monkeypatch)

    assert "docker_sandbox" not in by_name
    assert not hasattr(doctor, "_check_docker_sandbox")


def test_the_sandbox_is_checked_against_the_policy_in_the_config_and_the_real_home(tmp_path, monkeypatch, world):
    _run_healthy(tmp_path, monkeypatch, ENABLED)

    (policy, profile_dirs, kwargs), = world.sandbox_calls
    assert isinstance(policy, sandbox.SandboxPolicy) and policy.image == "python:3.11"
    assert kwargs == {"home": pathlib.Path.home()}


def test_the_worker_profile_dirs_come_from_the_profiles_module(tmp_path, monkeypatch, world):
    world.specs = [
        types.SimpleNamespace(name="coder-1", worker=True, active=True, toolsets=("file", "terminal")),
        types.SimpleNamespace(name="coder-2", worker=True, active=False, toolsets=("file", "terminal")),  # not active
        types.SimpleNamespace(name="lead", worker=False, active=True, toolsets=("file",)),
        # The reviewer is a worker (the dispatcher spawns it) but has no terminal, so init gives it no terminal block.
        types.SimpleNamespace(name="reviewer", worker=True, active=True, toolsets=("file", "kanban")),
        types.SimpleNamespace(name="tester", worker=True, active=True),  # no toolsets listed: counts, as unknown
    ]

    project, _, _ = _run_healthy(tmp_path, monkeypatch)

    (_, profile_dirs, _), = world.sandbox_calls
    assert profile_dirs == [project.hermes_native_home / "profiles" / "coder-1",
                            project.hermes_native_home / "profiles" / "tester"]


def test_the_worker_profiles_fall_back_to_every_role_but_the_lead_and_the_reviewer(tmp_path, monkeypatch, world):
    monkeypatch.setitem(sys.modules, "ases.profiles", None)  # nothing to ask, so the role map decides

    project, _, _ = _run_healthy(tmp_path, monkeypatch)

    (_, profile_dirs, _), = world.sandbox_calls
    assert profile_dirs == [project.hermes_native_home / "profiles" / "coder-1"]


def test_a_profile_that_also_plays_the_reviewer_is_not_a_worker(tmp_path, monkeypatch, world):
    monkeypatch.setitem(sys.modules, "ases.profiles", None)
    project = dataclasses.replace(
        _project(tmp_path),
        roles={"lead": "lead", "coder": "coder-1", "reviewer": "reviewer", "security": "reviewer",
               "debugger": "coder-1"},
    )
    _stub_hermes(monkeypatch)
    conn = db.connect(config.db_path(project))
    doctor.run(project, MODELS_CONFIG, conn)

    (_, profile_dirs, _), = world.sandbox_calls
    assert [d.name for d in profile_dirs] == ["coder-1"]  # once: security is the reviewer, debugger is coder-1


def _boom(policy, profile_dirs, **kwargs):
    raise OSError("docker.exe is locked")


def test_a_sandbox_probe_that_raises_is_a_warn_row_while_the_sandbox_is_off(tmp_path, monkeypatch, world):
    monkeypatch.setattr(sandbox, "doctor_checks", _boom)

    _, report, by_name = _run_healthy(tmp_path, monkeypatch)

    assert by_name["sandbox_checks"].status == "warn" and "docker.exe is locked" in by_name["sandbox_checks"].detail
    assert report.ok is True


def test_a_sandbox_probe_that_raises_is_a_fail_row_once_the_sandbox_is_enabled(tmp_path, monkeypatch, world):
    monkeypatch.setattr(sandbox, "doctor_checks", _boom)

    _, report, by_name = _run_healthy(tmp_path, monkeypatch, ENABLED)

    assert by_name["sandbox_checks"].status == "fail"
    assert report.ok is False


def test_an_invalid_sandbox_block_on_a_hand_built_config_is_a_row(tmp_path, monkeypatch, world):
    _stub_hermes(monkeypatch)
    project = dataclasses.replace(_project(tmp_path), sandbox={"enabled": False, "network_default": True})
    conn = db.connect(config.db_path(project))

    report = doctor.run(project, MODELS_CONFIG, conn)

    row = {c.name: c for c in report.checks}["sandbox_config"]
    assert row.status == "warn" and "network_default" in row.detail
    assert world.sandbox_calls == []  # nothing was probed with a policy that could not be built


def test_the_secret_check_still_runs_last_over_the_new_rows(tmp_path, monkeypatch, world):
    world.verify_problems = ["a problem"]

    _, report, _ = _run_healthy(tmp_path, monkeypatch)

    assert report.checks[-1].name == "no_secrets_in_output"
    assert all(check.detail.isascii() for check in report.checks)


needs_profiles = pytest.mark.skipif(real_profiles is None, reason="ases.profiles is not built in this checkout yet")


@needs_profiles
def test_the_real_profiles_module_feeds_the_warn_rows_and_the_worker_dirs(tmp_path, monkeypatch, world):
    """The real profiles.verify_state and desired_profiles on a temp Hermes home (read-only): the profiles exist but
    were never set up by swarm init, so there are problems to report, all as warnings; and the sandbox is asked about
    exactly the profiles init would put a terminal block on."""
    monkeypatch.setitem(sys.modules, "ases.profiles", real_profiles)

    project, report, by_name = _run_healthy(tmp_path, monkeypatch)

    rows = [name for name in by_name if name.startswith("profile_state[")]
    assert rows and all(by_name[name].status == "warn" for name in rows)
    assert all(by_name[name].detail.isascii() for name in rows)
    assert report.ok is True
    (_, profile_dirs, _), = world.sandbox_calls
    assert profile_dirs == [project.hermes_native_home / "profiles" / "coder-1"]  # not the reviewer, not coder-2/3


def test_takes_finds_a_named_keyword_or_a_var_keyword_and_gives_up_on_a_signature_it_cannot_read():
    assert doctor._takes(lambda a, *, policy=None: 0, "policy")
    assert doctor._takes(lambda a, **kwargs: 0, "policy")
    assert not doctor._takes(lambda a, b=1: 0, "policy")
    assert not doctor._takes(int, "policy")  # a builtin type whose signature cannot be read


def test_a_profiles_module_that_fails_to_load_is_a_warn_row_not_a_crash(tmp_path, monkeypatch, world):
    def broken_import(name):
        raise SyntaxError("invalid syntax (profiles.py, line 12)")

    monkeypatch.setattr(doctor, "importlib", types.SimpleNamespace(import_module=broken_import))

    _, report, by_name = _run_healthy(tmp_path, monkeypatch)

    assert by_name["profile_state"].status == "warn"
    assert "failed to load" in by_name["profile_state"].detail and "SyntaxError" in by_name["profile_state"].detail
    assert report.ok is True and "sandbox_enabled" in by_name


def test_worker_profiles_fall_back_to_the_role_map_when_desired_profiles_raises(tmp_path, monkeypatch, world):
    def broken(project, models_config):
        raise RuntimeError("the role table is unreadable")

    world.profiles.desired_profiles = broken

    project, _, _ = _run_healthy(tmp_path, monkeypatch)

    (_, profile_dirs, _), = world.sandbox_calls
    assert profile_dirs == [project.hermes_native_home / "profiles" / "coder-1"]


def test_worker_profiles_fall_back_to_the_role_map_when_no_spec_qualifies(tmp_path, monkeypatch, world):
    world.specs = [types.SimpleNamespace(name="reviewer", worker=True, active=True, toolsets=("file", "kanban"))]

    project, _, _ = _run_healthy(tmp_path, monkeypatch)

    (_, profile_dirs, _), = world.sandbox_calls
    assert profile_dirs == [project.hermes_native_home / "profiles" / "coder-1"]  # the roles map still says coder
