"""Doctor logic tested with hermes.py mocked out -- no subprocess, no real Hermes needed. The real-Hermes
path is covered separately by tests/integration/test_doctor_real_hermes.py."""
from ases import config, db, doctor, hermes, models

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


def _project(tmp_path):
    p = tmp_path / "swarm.yaml"
    # as_posix(): Windows tmp_path backslashes inside a double-quoted YAML scalar are escape sequences,
    # not path separators -- forward slashes side-step that (same reason config/swarm.yaml uses them).
    p.write_text(
        SWARM_YAML.format(ws=(tmp_path / "ws").as_posix(), home=(tmp_path / "home").as_posix()),
        encoding="utf-8",
    )
    return config.load_swarm_config(p)


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
