"""Doctor logic tested with hermes.py mocked out -- no subprocess, no real Hermes needed. The real-Hermes
path is covered separately by tests/integration/test_doctor_real_hermes.py."""
import dataclasses
import os
import pathlib
import shutil
import subprocess
import sys
import types

import pytest

from ases import config, db, doctor, events, hermes, models, sandbox

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
        self.residual_risks = []


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
    stub.residual_risks = lambda: list(state.residual_risks)
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


def _context_report(tmp_path, monkeypatch, models_config):
    _stub_hermes(monkeypatch)
    project = _project(tmp_path)
    conn = db.connect(config.db_path(project))
    models.sync_from_config(conn, models_config)
    report = doctor.run(project, models_config, conn)
    return report, {c.name: c for c in report.checks}


def test_model_context_check_fails_a_pinned_model_rejected_as_unknown_on_a_custom_endpoint(tmp_path, monkeypatch):
    """ASES-MOD-02, acceptance 22.4: a PINNED model on a custom OpenAI-compatible endpoint (config/models.yaml
    providers.<name>.type: openai_compatible) with no declared context_length is exactly what
    models.classify_model_context rejects as unknown. Since it is pinned, cli._first_model_rejection would
    itself refuse it at swarm approve/run pre-flight -- so swarm doctor must FAIL here, not merely warn: the
    row should honestly say the run cannot start, the same row name (context_length[...]) as before."""
    models_config = {
        "providers": {"xkiro": {"type": "openai_compatible", "data_policy": "unknown"}},
        "models": [
            {"provider": "xkiro", "model": "some/undeclared-model", "context_length": None,
             "tool_calling": True, "role_class": "lead", "pinned": True},
        ],
    }
    report, by_name = _context_report(tmp_path, monkeypatch, models_config)
    row = by_name["context_length[xkiro/some/undeclared-model]"]
    assert row.status == "fail"
    assert "rejected unknown" in row.detail
    assert "ASES-MOD-02" in row.requirement_ids
    assert report.ok is False


def test_model_context_check_fails_a_pinned_model_with_a_declared_too_small_context(tmp_path, monkeypatch):
    """Acceptance 22.4 step 1 ("Register a model declared at 16K: the controller must reject it before any
    card starts") reflected in swarm doctor: an explicit, too-small declared context_length on a PINNED model
    is a FAIL regardless of provider type (models.classify_model_context never excuses this one on type)."""
    models_config = {
        "providers": {"openrouter": {"type": "openrouter", "data_policy": "some_free_endpoints_train"}},
        "models": [
            {"provider": "openrouter", "model": "too-small-model", "context_length": 16_000,
             "tool_calling": True, "role_class": "reviewer", "pinned": True},
        ],
    }
    report, by_name = _context_report(tmp_path, monkeypatch, models_config)
    row = by_name["context_length[openrouter/too-small-model]"]
    assert row.status == "fail"
    assert "rejected too small" in row.detail
    assert report.ok is False


def test_model_context_check_warns_not_fails_an_unpinned_candidate_rejected_as_unknown(tmp_path, monkeypatch):
    """The same rejected-as-unknown model, UNPINNED (a candidate, never actually resolved for a role today):
    the work order is explicit that "an unpinned candidate stays a WARN" -- it does not block anything from
    starting today, so FAILing it here would be a false alarm swarm doctor must not raise."""
    models_config = {
        "providers": {"xkiro": {"type": "openai_compatible", "data_policy": "unknown"}},
        "models": [
            {"provider": "xkiro", "model": "some/undeclared-candidate", "context_length": None,
             "tool_calling": True, "role_class": "coder_candidate", "pinned": False},
        ],
    }
    report, by_name = _context_report(tmp_path, monkeypatch, models_config)
    row = by_name["context_length[xkiro/some/undeclared-candidate]"]
    assert row.status == "warn"


def test_model_context_check_passes_undeclared_context_on_a_native_hermes_provider(tmp_path, monkeypatch):
    """A native Hermes provider (type openrouter or hermes_provider, or no type declared) with no declared
    context_length is ACCEPTED by models.classify_model_context (see its docstring: Hermes's own knowledge or
    probing, not the custom-endpoint gap the blueprint's 'rejected as unknown' text is about) -- so it is
    never a FAIL, pinned or not, even though the context is still undeclared. It stays a WARN, same as before
    this round, because the number itself is still worth confirming (ASES-MOD-02's own comment in
    config/models.yaml: "swarm doctor WARNs on every such row until a smoke test or an explicit number here
    confirms the Hermes floor")."""
    models_config = {
        "providers": {"opencode_free": {"type": "hermes_provider", "provider_id": "opencode-free"}},
        "models": [
            {"provider": "opencode_free", "model": "some/native-model", "context_length": None,
             "tool_calling": True, "role_class": "lead", "pinned": True},
        ],
    }
    _, by_name = _context_report(tmp_path, monkeypatch, models_config)
    row = by_name["context_length[opencode_free/some/native-model]"]
    assert row.status == "warn"  # never a FAIL: classify_model_context accepts this model


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


# --- ASES-CAP-01/ASES-VER-01: the limits table shows a source URL, and WARNs when one is missing ------------


def test_limits_table_passes_and_shows_the_source_when_every_provider_has_one():
    models_config = {"providers": {
        "a": {"limits": {"rpm": 20}, "verified_on": "2026-09-17", "source": "https://example.com/limits"},
    }}

    check = doctor._check_limits_table(models_config)

    assert check.status == "pass"
    assert "https://example.com/limits" in check.detail and "2026-09-17" in check.detail
    assert check.requirement_ids == ("ASES-CAP-01", "ASES-VER-01")


def test_limits_table_warns_and_names_the_provider_with_no_source():
    models_config = {"providers": {
        "a": {"limits": {"rpm": 20}, "verified_on": "2026-09-17", "source": "https://example.com/limits"},
        "b": {"limits": {}, "verified_on": "2026-09-19"},   # no source
    }}

    check = doctor._check_limits_table(models_config)

    assert check.status == "warn"
    assert "b" in check.detail and "no source URL on record" in check.detail
    assert "ASES-VER-01" in check.requirement_ids


def test_limits_table_is_wired_into_the_report_and_warns_on_a_real_missing_source(tmp_path, monkeypatch):
    _stub_hermes(monkeypatch)
    project = _project(tmp_path)
    conn = db.connect(config.db_path(project))
    models_config = {**MODELS_CONFIG, "providers": {"unorouter": {"verified_on": "2026-09-17"}}}
    models.sync_from_config(conn, models_config)

    report = doctor.run(project, models_config, conn)

    by_name = {c.name: c for c in report.checks}
    assert by_name["limits_displayed"].status == "warn"
    assert "unorouter" in by_name["limits_displayed"].detail


# --- p213/ASES-CFG-04/ASES-CFG-05: a provider key set in this process's own environment -----------------------


def test_provider_keys_not_exported_passes_when_no_key_env_is_set(monkeypatch):
    monkeypatch.delenv("SOME_PROVIDER_API_KEY", raising=False)
    models_config = {"providers": {"a": {"key_env": "SOME_PROVIDER_API_KEY"}}}

    check = doctor._check_provider_keys_not_exported(models_config)

    assert check.status == "pass"
    assert check.requirement_ids == ("ASES-CFG-04", "ASES-CFG-05")


def test_provider_keys_not_exported_warns_and_names_the_variable(monkeypatch):
    monkeypatch.setenv("SOME_PROVIDER_API_KEY", "sk-totally-secret-value")
    models_config = {"providers": {"a": {"key_env": "SOME_PROVIDER_API_KEY"}}}

    check = doctor._check_provider_keys_not_exported(models_config)

    assert check.status == "warn"
    assert "SOME_PROVIDER_API_KEY" in check.detail
    assert "sk-totally-secret-value" not in check.detail   # the value is never printed
    assert "Never export provider keys in the shell that launches the gateway or the controller" in check.detail


def test_provider_keys_not_exported_ignores_an_empty_value(monkeypatch):
    monkeypatch.setenv("SOME_PROVIDER_API_KEY", "")
    models_config = {"providers": {"a": {"key_env": "SOME_PROVIDER_API_KEY"}}}

    check = doctor._check_provider_keys_not_exported(models_config)

    assert check.status == "pass"


def test_provider_keys_not_exported_ignores_anonymous_providers(monkeypatch):
    models_config = {"providers": {"opencode_free": {"key_env": None}}}

    check = doctor._check_provider_keys_not_exported(models_config)

    assert check.status == "pass"


def test_provider_keys_not_exported_lists_other_credential_shaped_vars_as_info_only(monkeypatch):
    monkeypatch.delenv("SOME_PROVIDER_API_KEY", raising=False)
    monkeypatch.setenv("SOME_OTHER_TOKEN", "value-does-not-matter")
    models_config = {"providers": {"a": {"key_env": "SOME_PROVIDER_API_KEY"}}}

    check = doctor._check_provider_keys_not_exported(models_config)

    assert check.status == "pass"   # an unrelated credential-shaped var never warns by itself
    assert "SOME_OTHER_TOKEN" in check.detail
    assert "value-does-not-matter" not in check.detail


def test_provider_keys_not_exported_does_not_double_list_a_warned_key_env(monkeypatch):
    monkeypatch.setenv("SOME_PROVIDER_API_KEY", "secret")
    models_config = {"providers": {"a": {"key_env": "SOME_PROVIDER_API_KEY"}}}

    check = doctor._check_provider_keys_not_exported(models_config)

    assert check.status == "warn"
    assert check.detail.count("SOME_PROVIDER_API_KEY") == 1


def test_provider_keys_not_exported_never_leaks_a_value(monkeypatch):
    monkeypatch.setenv("SOME_PROVIDER_API_KEY", "sk-totally-secret-value")
    monkeypatch.setenv("SOME_OTHER_SECRET", "another-secret-value")
    models_config = {"providers": {"a": {"key_env": "SOME_PROVIDER_API_KEY"}}}

    check = doctor._check_provider_keys_not_exported(models_config)
    no_secrets = doctor._check_no_secrets_in_output(check.detail)

    assert no_secrets.status == "pass"


def test_provider_keys_not_exported_is_wired_into_the_report(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-should-not-be-set-here")
    _stub_hermes(monkeypatch)
    project = _project(tmp_path)
    conn = db.connect(config.db_path(project))
    models_config = {**MODELS_CONFIG, "providers": {
        **MODELS_CONFIG["providers"], "openrouter": {"key_env": "OPENROUTER_API_KEY"},
    }}
    models.sync_from_config(conn, models_config)

    report = doctor.run(project, models_config, conn)

    by_name = {c.name: c for c in report.checks}
    assert by_name["provider_keys_not_exported"].status == "warn"
    assert "OPENROUTER_API_KEY" in by_name["provider_keys_not_exported"].detail
    assert "sk-should-not-be-set-here" not in by_name["provider_keys_not_exported"].detail
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


def test_worktree_sync_drift_is_a_profile_state_warn_row_citing_ases_git_16(tmp_path, monkeypatch, world):
    """ASES-GIT-16: profiles.verify_state reports a profile whose worktree_sync is not pinned to false as one
    more profile_state problem, and that row must cite ASES-GIT-16 like every other requirement_ids row does
    (test_the_profile_rows_cite_the_requirements_they_check checks the other three IDs on the same family of
    rows)."""
    world.verify_problems = [
        "profile coder-1 has worktree_sync on (Hermes default): worktrees would branch from a fetched remote "
        "tip (ASES-GIT-16)",
    ]

    _, report, by_name = _run_healthy(tmp_path, monkeypatch)

    assert report.ok is True  # a WARN row, never a FAIL
    assert by_name["profile_state[1]"].status == "warn"
    assert "worktree_sync" in by_name["profile_state[1]"].detail
    assert "ASES-GIT-16" in by_name["profile_state[1]"].requirement_ids


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


# --- residual risks (ASES-ROL-05): profiles.residual_risks() as INFO rows, never WARN or FAIL -------------------


def test_residual_risks_are_info_rows_named_and_cited_in_order(tmp_path, monkeypatch, world):
    world.residual_risks = ["Reviewer file access: keeps write tools its prompt forbids it to use.",
                            "Kanban toolset: appended to every dispatcher-spawned worker regardless of profile."]

    _, report, by_name = _run_healthy(tmp_path, monkeypatch)

    assert by_name["residual_risk[1]"].status == "info"
    assert by_name["residual_risk[1]"].detail == world.residual_risks[0]
    assert by_name["residual_risk[2]"].status == "info"
    assert by_name["residual_risk[2]"].detail == world.residual_risks[1]
    assert by_name["residual_risk[1]"].requirement_ids == ("ASES-ROL-05",)
    assert report.ok is True  # never WARN or FAIL: they are known, accepted limits


def test_no_residual_risk_rows_when_the_module_reports_none(tmp_path, monkeypatch, world):
    world.residual_risks = []

    _, _, by_name = _run_healthy(tmp_path, monkeypatch)

    assert not any(name.startswith("residual_risk") for name in by_name)


def test_residual_risks_row_is_a_warn_when_reading_them_raises_not_a_crash(tmp_path, monkeypatch, world):
    def boom():
        raise RuntimeError("RESIDUAL_RISKS is not iterable")

    world.profiles.residual_risks = boom

    _, report, by_name = _run_healthy(tmp_path, monkeypatch)

    assert by_name["residual_risks"].status == "warn"
    assert "RuntimeError" in by_name["residual_risks"].detail
    assert report.ok is True


def test_no_residual_risk_rows_when_the_profiles_module_is_missing(tmp_path, monkeypatch, world):
    monkeypatch.setitem(sys.modules, "ases.profiles", None)

    _, report, by_name = _run_healthy(tmp_path, monkeypatch)

    assert not any(name.startswith("residual_risk") for name in by_name)
    assert report.ok is True


def test_no_residual_risk_rows_when_an_older_profiles_build_has_no_such_function(tmp_path, monkeypatch, world):
    del world.profiles.residual_risks  # a build from before this function existed

    _, report, by_name = _run_healthy(tmp_path, monkeypatch)

    assert not any(name.startswith("residual_risk") for name in by_name)
    assert report.ok is True


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


@needs_profiles
def test_the_real_residual_risks_are_wired_in_as_info_rows(tmp_path, monkeypatch, world):
    """The real profiles.RESIDUAL_RISKS, through the real profiles.residual_risks(), land as one INFO row each,
    in order, and never fail the report."""
    monkeypatch.setitem(sys.modules, "ases.profiles", real_profiles)

    _, report, by_name = _run_healthy(tmp_path, monkeypatch)

    rows = [name for name in by_name if name.startswith("residual_risk[")]
    assert len(rows) == len(real_profiles.RESIDUAL_RISKS)
    assert all(by_name[name].status == "info" for name in rows)
    assert [by_name[name].detail for name in rows] == list(real_profiles.RESIDUAL_RISKS)
    assert report.ok is True


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


# --- log_all_ref_updates (round 10, package BASECHECK; ASES-GIT-01, ASES-GIT-16) --------------------------------

def _git_repo(tmp_path, *, log_all_ref_updates=None):
    """A real git repository (not the ASES checkout doctor's other checks inspect), with core.logAllRefUpdates
    left at its non-bare default (True), explicitly set (True/False), or explicitly unset again."""
    repo = tmp_path / "target-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "integration"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo), check=True)
    if log_all_ref_updates is True:
        subprocess.run(["git", "config", "core.logAllRefUpdates", "true"], cwd=str(repo), check=True)
    elif log_all_ref_updates is False:
        subprocess.run(["git", "config", "core.logAllRefUpdates", "false"], cwd=str(repo), check=True)
    elif log_all_ref_updates == "unset":
        subprocess.run(["git", "config", "--unset", "core.logAllRefUpdates"], cwd=str(repo), check=True)
    return repo


def test_log_all_ref_updates_is_pending_without_a_repository_path():
    check = doctor._check_log_all_ref_updates(None)

    assert check.status == "pending"
    assert check.requirement_ids == ("ASES-GIT-01", "ASES-GIT-16")


def test_log_all_ref_updates_pending_detail_does_not_claim_cmd_doctor_has_no_repo_flag():
    """Finding 9 (round 12, RUNSTART): cmd_doctor has accepted --repo since round 10 (cli.py wires args.repo
    through to doctor.run's own `repo` keyword, which is exactly what this check consumes), so the "pending"
    detail must not tell the operator otherwise."""
    check = doctor._check_log_all_ref_updates(None)

    assert "cmd_doctor has no --repo" not in check.detail
    assert "not yet given the project repository" not in check.detail


def test_log_all_ref_updates_docstring_does_not_claim_cmd_doctor_has_no_repo_flag():
    """Same finding, the other stale text: a maintainer reading this check's own docstring must not be told
    cmd_doctor has no --repo flag (it has had one since round 10) or that no round 10 package added it."""
    doc = doctor._check_log_all_ref_updates.__doc__ or ""

    assert "cmd_doctor has no" not in doc
    assert "no round 10 package adds it" not in doc


def test_log_all_ref_updates_passes_when_explicitly_true(tmp_path):
    repo = _git_repo(tmp_path, log_all_ref_updates=True)

    check = doctor._check_log_all_ref_updates(repo)

    assert check.status == "pass" and "true" in check.detail


def test_log_all_ref_updates_passes_when_unset_the_non_bare_default(tmp_path):
    """git itself defaults core.logAllRefUpdates to true for a non-bare repository (and `git init` writes it
    explicitly), so an unset value must read as healthy too, not as a WARN nothing actually set."""
    repo = _git_repo(tmp_path, log_all_ref_updates="unset")

    check = doctor._check_log_all_ref_updates(repo)

    assert check.status == "pass" and "unset" in check.detail


def test_log_all_ref_updates_warns_when_explicitly_false(tmp_path):
    repo = _git_repo(tmp_path, log_all_ref_updates=False)

    check = doctor._check_log_all_ref_updates(repo)

    assert check.status == "warn"
    assert "core.logAllRefUpdates" in check.detail and str(repo) in check.detail
    assert check.requirement_ids == ("ASES-GIT-01", "ASES-GIT-16")


def test_log_all_ref_updates_is_pending_through_run_by_default(tmp_path, monkeypatch):
    _, _report, checks = _run(tmp_path, monkeypatch)

    assert checks["log_all_ref_updates"].status == "pending"


def test_log_all_ref_updates_is_wired_into_run_when_a_repo_is_given(tmp_path, monkeypatch):
    _stub_hermes(monkeypatch)
    project = _project(tmp_path)
    conn = db.connect(config.db_path(project))
    models.sync_from_config(conn, MODELS_CONFIG)
    repo = _git_repo(tmp_path, log_all_ref_updates=False)

    report = doctor.run(project, MODELS_CONFIG, conn, repo=repo)

    checks = {c.name: c for c in report.checks}
    assert checks["log_all_ref_updates"].status == "warn"


# --- round 15 (WORKERGIT): worktree_relative_paths, ASES-SEC-03 ---------------------------------------------------


def _worktree_repo(tmp_path, *, use_relative_paths=None):
    """A real git repository, `worktree.useRelativePaths` left at its (false) default, explicitly set true/false,
    or explicitly unset again -- same shape as _git_repo above, for the WORKERGIT check."""
    repo = tmp_path / "target-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "integration"], cwd=str(repo), check=True)
    if use_relative_paths is True:
        subprocess.run(["git", "config", "worktree.useRelativePaths", "true"], cwd=str(repo), check=True)
    elif use_relative_paths is False:
        subprocess.run(["git", "config", "worktree.useRelativePaths", "false"], cwd=str(repo), check=True)
    elif use_relative_paths == "unset":
        subprocess.run(["git", "config", "--unset", "worktree.useRelativePaths"], cwd=str(repo))  # ok if never set
    return repo


def test_worktree_relative_paths_is_pending_without_a_repository_path():
    check = doctor._check_worktree_relative_paths(None, False)

    assert check.status == "pending"
    assert check.requirement_ids == ("ASES-SEC-03", "ASES-CFG-04")


def test_worktree_relative_paths_passes_when_explicitly_true(tmp_path):
    repo = _worktree_repo(tmp_path, use_relative_paths=True)

    check = doctor._check_worktree_relative_paths(repo, False)

    assert check.status == "pass" and "true" in check.detail and str(repo) in check.detail


@pytest.mark.parametrize("spelling", ["yes", "on", "1", "TRUE", "Yes"])
def test_worktree_relative_paths_passes_for_every_git_true_spelling(tmp_path, spelling):
    """git-config(1) treats true/yes/on/1 (any case) as boolean-true; controller.ensure_repo_bootstrapped only
    ever writes the literal "true", but a human hand-editing the config is free to use any of these, and this
    check must not misreport a healthy repository as unset/false just because the spelling differs."""
    repo = tmp_path / "target-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "integration"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "worktree.useRelativePaths", spelling], cwd=str(repo), check=True)

    check = doctor._check_worktree_relative_paths(repo, True)

    assert check.status == "pass", check.detail


@pytest.mark.parametrize("sandbox_enabled,expected", [(False, "warn"), (True, "fail")])
def test_worktree_relative_paths_is_unhealthy_when_unset_the_false_default(tmp_path, sandbox_enabled, expected):
    """Opposite polarity from core.logAllRefUpdates: worktree.useRelativePaths defaults to FALSE, so unset must
    read the same as an explicit false, not as healthy. Severity only reaches FAIL once the sandbox is actually
    on -- that is the point a worker's own commit is demonstrably broken by this."""
    repo = _worktree_repo(tmp_path, use_relative_paths="unset")

    check = doctor._check_worktree_relative_paths(repo, sandbox_enabled)

    assert check.status == expected
    assert "worktree.useRelativePaths" in check.detail and str(repo) in check.detail


@pytest.mark.parametrize("sandbox_enabled,expected", [(False, "warn"), (True, "fail")])
def test_worktree_relative_paths_is_unhealthy_when_explicitly_false(tmp_path, sandbox_enabled, expected):
    repo = _worktree_repo(tmp_path, use_relative_paths=False)

    check = doctor._check_worktree_relative_paths(repo, sandbox_enabled)

    assert check.status == expected
    assert check.requirement_ids == ("ASES-SEC-03", "ASES-CFG-04")


def test_worktree_relative_paths_is_pending_through_run_by_default(tmp_path, monkeypatch):
    _, _report, checks = _run(tmp_path, monkeypatch)

    assert checks["worktree_relative_paths"].status == "pending"


def test_worktree_relative_paths_is_wired_into_run_when_a_repo_is_given(tmp_path, monkeypatch):
    _stub_hermes(monkeypatch)
    project = _project(tmp_path)
    conn = db.connect(config.db_path(project))
    models.sync_from_config(conn, MODELS_CONFIG)
    repo = _worktree_repo(tmp_path, use_relative_paths=False)

    report = doctor.run(project, MODELS_CONFIG, conn, repo=repo)

    checks = {c.name: c for c in report.checks}
    assert checks["worktree_relative_paths"].status == "warn"  # sandbox not enabled in this project config


def test_worktree_relative_paths_fails_through_run_once_the_sandbox_is_enabled(tmp_path, monkeypatch, world):
    """world (autouse) fakes sandbox.doctor_checks and ases.profiles so this reaches the FAIL branch without a
    real Docker daemon or real profiles on disk."""
    _stub_hermes(monkeypatch)
    project = _project(tmp_path, ENABLED)
    conn = db.connect(config.db_path(project))
    models.sync_from_config(conn, MODELS_CONFIG)
    repo = _worktree_repo(tmp_path, use_relative_paths=False)

    report = doctor.run(project, MODELS_CONFIG, conn, repo=repo)

    checks = {c.name: c for c in report.checks}
    assert checks["worktree_relative_paths"].status == "fail"
    assert report.ok is False


# --- round 13 (TIDY): leaked_worktrees, ASES-GIT-12 --------------------------------------------------------------


def _committed_repo(tmp_path):
    """A real git repository with one commit, so `git worktree add` has something to check out (unlike
    `_git_repo` above, which the log_all_ref_updates checks never need to add a worktree to)."""
    repo = tmp_path / "leak-target-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "integration"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo), check=True)
    (repo / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(repo), check=True)
    return repo


def test_leaked_worktrees_passes_when_no_events_are_recorded(tmp_path):
    project = _project(tmp_path)
    conn = db.connect(config.db_path(project))

    check = doctor._check_leaked_worktrees(project, conn, None)

    assert check.status == "pass"
    assert "no gate/merge worktree-leak events" in check.detail
    assert check.requirement_ids == ("ASES-GIT-12",)


def test_leaked_worktrees_warns_when_the_recorded_directory_still_exists(tmp_path):
    project = _project(tmp_path)
    conn = db.connect(config.db_path(project))
    leaked_dir = tmp_path / "leaked-gate-wt"
    leaked_dir.mkdir()
    events.record(
        conn, "gate_worktree_leak",
        {"task_key": "T1", "gate": "gate1", "path": str(leaked_dir), "git_exit_code": 128}, project=project.name,
    )

    check = doctor._check_leaked_worktrees(project, conn, None)

    assert check.status == "warn"
    assert str(leaked_dir) in check.detail
    assert "directory still on disk" in check.detail
    assert "git worktree prune" in check.detail
    assert check.requirement_ids == ("ASES-GIT-12",)


def test_leaked_worktrees_passes_when_the_recorded_path_was_already_cleaned_up(tmp_path):
    project = _project(tmp_path)
    conn = db.connect(config.db_path(project))
    gone = tmp_path / "already-gone"  # never created
    events.record(
        conn, "merge_worktree_leak", {"task_key": "T2", "path": str(gone), "git_exit_code": 128},
        project=project.name,
    )

    check = doctor._check_leaked_worktrees(project, conn, None)

    assert check.status == "pass"
    assert "already cleaned up" in check.detail


def test_leaked_worktrees_is_project_scoped(tmp_path):
    """A leak recorded for a different project must not turn up in this project's report (events.PROJECT_SCOPE_SQL,
    the one project-scope filter every reader of the events table uses)."""
    project = _project(tmp_path)
    conn = db.connect(config.db_path(project))
    leaked_dir = tmp_path / "someone-elses-leak"
    leaked_dir.mkdir()
    events.record(
        conn, "gate_worktree_leak",
        {"task_key": "T3", "gate": "gate1", "path": str(leaked_dir), "git_exit_code": 128},
        project="a-different-project",
    )

    check = doctor._check_leaked_worktrees(project, conn, None)

    assert check.status == "pass"


def test_leaked_worktrees_dedupes_the_same_path_reported_by_both_a_gate_and_a_merge_leak(tmp_path):
    project = _project(tmp_path)
    conn = db.connect(config.db_path(project))
    leaked_dir = tmp_path / "leaked-both"
    leaked_dir.mkdir()
    events.record(
        conn, "gate_worktree_leak",
        {"task_key": "T4", "gate": "gate1", "path": str(leaked_dir), "git_exit_code": 128}, project=project.name,
    )
    events.record(
        conn, "merge_worktree_leak", {"task_key": "T4", "path": str(leaked_dir), "git_exit_code": 128},
        project=project.name,
    )

    check = doctor._check_leaked_worktrees(project, conn, None)

    assert check.status == "warn"
    assert "1 leaked gate/merge worktree(s)" in check.detail


def test_leaked_worktrees_never_deletes_anything(tmp_path):
    """Read-only, like every other doctor check: the leaked directory (and whatever a hung gate command left
    inside it) must still be there afterwards."""
    project = _project(tmp_path)
    conn = db.connect(config.db_path(project))
    leaked_dir = tmp_path / "leaked-readonly"
    leaked_dir.mkdir()
    (leaked_dir / "marker.txt").write_text("still here", encoding="utf-8")
    events.record(
        conn, "gate_worktree_leak",
        {"task_key": "T5", "gate": "gate1", "path": str(leaked_dir), "git_exit_code": 128}, project=project.name,
    )

    doctor._check_leaked_worktrees(project, conn, None)

    assert leaked_dir.exists()
    assert (leaked_dir / "marker.txt").read_text(encoding="utf-8") == "still here"


def test_leaked_worktrees_notes_it_could_not_cross_check_git_without_a_repo(tmp_path):
    project = _project(tmp_path)
    conn = db.connect(config.db_path(project))
    leaked_dir = tmp_path / "leaked-no-repo"
    leaked_dir.mkdir()
    events.record(
        conn, "gate_worktree_leak",
        {"task_key": "T6", "gate": "gate1", "path": str(leaked_dir), "git_exit_code": 128}, project=project.name,
    )

    check = doctor._check_leaked_worktrees(project, conn, None)

    assert check.status == "warn"
    assert "not given --repo" in check.detail


def test_leaked_worktrees_warns_when_git_worktree_list_still_registers_it_though_the_directory_is_gone(tmp_path):
    """Round 12's own finding is the mirror image of this: `git worktree remove --force` can deregister a
    worktree even while failing to delete its directory. This proves the OTHER stale state is caught too -- a
    directory deleted by hand, without ever running `git worktree remove`, leaves git's own registration
    behind, and `git worktree list` (not just disk existence) is exactly what catches that."""
    project = _project(tmp_path)
    conn = db.connect(config.db_path(project))
    repo = _committed_repo(tmp_path)
    wt = tmp_path / "stale-registered-wt"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "--detach", str(wt), "integration"],
        capture_output=True, text=True, check=True,
    )
    shutil.rmtree(wt)  # deleted by hand, WITHOUT `git worktree remove`: git still has it registered
    events.record(
        conn, "gate_worktree_leak",
        {"task_key": "T7", "gate": "gate1", "path": str(wt), "git_exit_code": 0}, project=project.name,
    )

    check = doctor._check_leaked_worktrees(project, conn, repo)

    assert check.status == "warn"
    assert "still registered in `git worktree list`" in check.detail


def test_leaked_worktrees_matches_a_registered_path_of_different_case_on_windows(tmp_path):
    """Windows' filesystem is case-insensitive, but a leak event's recorded path (str(tmp_root), whatever case
    tempfile/pathlib happened to return) and git's own report of the very same directory are not guaranteed to
    agree on case: this must still count as the same directory, not two different ones."""
    if os.name != "nt":
        pytest.skip("case-insensitive path matching is a Windows-only concern")
    project = _project(tmp_path)
    conn = db.connect(config.db_path(project))
    repo = _committed_repo(tmp_path)
    wt = tmp_path / "CaseSensitiveWT"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "--detach", str(wt), "integration"],
        capture_output=True, text=True, check=True,
    )
    shutil.rmtree(wt)  # gone from disk, only git's own registration is left to find it by
    differently_cased_path = str(wt).replace("CaseSensitiveWT", "casesensitivewt")
    events.record(
        conn, "gate_worktree_leak",
        {"task_key": "T8", "gate": "gate1", "path": differently_cased_path, "git_exit_code": 0},
        project=project.name,
    )

    check = doctor._check_leaked_worktrees(project, conn, repo)

    assert check.status == "warn"
    assert "still registered in `git worktree list`" in check.detail


def test_leaked_worktrees_is_wired_into_run(tmp_path, monkeypatch):
    _, _report, checks = _run(tmp_path, monkeypatch)

    assert checks["leaked_worktrees"].status == "pass"
