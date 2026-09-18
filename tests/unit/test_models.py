import pytest

from ases import db, models

CONFIG = {
    "providers": {
        "unorouter": {"data_policy": "forwards_to_upstreams_that_may_train"},
        "openrouter": {"data_policy": "some_free_endpoints_train"},
    },
    "models": [
        {
            "provider": "unorouter", "model": "glm-5.3-thinking:free",
            "context_length": None, "tool_calling": None, "role_class": "lead", "pinned": True,
        },
        {
            "provider": "openrouter", "model": "some/reviewer-model",
            "context_length": 128000, "tool_calling": True, "role_class": "reviewer", "pinned": True,
        },
    ],
}


def _conn(tmp_path):
    conn = db.connect(tmp_path / "ases.db")
    models.sync_from_config(conn, CONFIG)
    return conn


def test_sync_populates_registry(tmp_path):
    conn = _conn(tmp_path)
    records = models.list_models(conn)
    assert {(m.provider, m.model) for m in records} == {
        ("unorouter", "glm-5.3-thinking:free"), ("openrouter", "some/reviewer-model")
    }


def test_context_sufficiency(tmp_path):
    conn = _conn(tmp_path)
    by_model = {m.model: m for m in models.list_models(conn)}
    assert by_model["glm-5.3-thinking:free"].context_declared_and_sufficient is False
    assert by_model["some/reviewer-model"].context_declared_and_sufficient is True


def test_smoke_test_round_trip(tmp_path):
    conn = _conn(tmp_path)
    models.record_smoke_test(conn, "openrouter", "some/reviewer-model", "pass", "tool call ok")
    by_model = {m.model: m for m in models.list_models(conn)}
    assert by_model["some/reviewer-model"].smoke_tested is True
    assert by_model["glm-5.3-thinking:free"].smoke_tested is False


def test_smoke_test_unknown_model_raises(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(KeyError):
        models.record_smoke_test(conn, "openrouter", "does/not-exist", "pass")


def test_smoke_test_bad_result_raises(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(ValueError):
        models.record_smoke_test(conn, "openrouter", "some/reviewer-model", "maybe")


def test_resync_preserves_smoke_test(tmp_path):
    conn = _conn(tmp_path)
    models.record_smoke_test(conn, "openrouter", "some/reviewer-model", "pass", "ok")
    models.sync_from_config(conn, CONFIG)  # re-running config sync must not wipe the smoke test
    by_model = {m.model: m for m in models.list_models(conn)}
    assert by_model["some/reviewer-model"].smoke_tested is True
