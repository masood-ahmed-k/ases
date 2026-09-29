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


# ---------------------------------------------------------------------------------------------
# classify_model_context (ASES-MOD-02, acceptance 22.4): the one decision function.
# ---------------------------------------------------------------------------------------------


def test_classify_accepts_a_declared_sufficient_context_on_any_provider_type():
    for provider_type in (None, "openrouter", "hermes_provider", "openai_compatible", "something_new"):
        decision = models.classify_model_context(64_000, provider_type)
        assert decision.accepted is True
        assert decision.status == "accepted"
        assert "64000" in decision.reason or "64,000" in decision.reason


def test_classify_rejects_a_declared_context_below_the_floor_regardless_of_provider_type():
    """Acceptance 22.4 step 1: "Register a model declared at 16K: the controller must reject it before any
    card starts." An explicit, declared-too-small context_length is never excused by provider type -- unlike
    an undeclared one, it is a known fact about the model, not a gap in what config/models.yaml says."""
    for provider_type in (None, "openrouter", "hermes_provider", "openai_compatible"):
        decision = models.classify_model_context(16_000, provider_type)
        assert decision.accepted is False
        assert decision.status == "rejected_too_small"
        assert "16000" in decision.reason


def test_classify_rejects_undeclared_context_as_unknown_only_on_a_custom_openai_compatible_endpoint():
    """Acceptance 22.4 step 2: "Register one with no declared context on a custom endpoint: rejected as
    unknown." Blueprint p121: "Custom OpenAI-compatible endpoints often cannot report their context length,
    so Hermes relies on model.context_length ... or probing" -- config/models.yaml marks that endpoint kind
    with providers.<name>.type: openai_compatible (see config/models.yaml's own xkiro block)."""
    decision = models.classify_model_context(None, "openai_compatible")
    assert decision.accepted is False
    assert decision.status == "rejected_unknown"
    assert "openai_compatible" in decision.reason


@pytest.mark.parametrize("provider_type", [None, "openrouter", "hermes_provider"])
def test_classify_accepts_undeclared_context_on_a_native_hermes_provider(provider_type):
    """A native Hermes provider (openrouter, hermes_provider, or no declared type at all -- i.e. not the
    custom-endpoint case the blueprint's 'rejected as unknown' language names) is trusted to Hermes's own
    model knowledge or probing when config/models.yaml declares no context_length of its own. This is the
    module's documented, deliberate reading of p121 (see classify_model_context's docstring): 'rejected as
    unknown' is about custom endpoints, not providers in general."""
    decision = models.classify_model_context(None, provider_type)
    assert decision.accepted is True
    assert decision.status == "accepted"


def test_classify_treats_a_non_int_context_length_as_undeclared_rather_than_raising():
    """A malformed config/models.yaml value (a bool -- bool is an int subclass in Python -- or a stray
    string from a typo) must never crash the doctor or the approve/run pre-flight: classify_model_context
    degrades it to the same handling a genuinely undeclared context_length gets, never a silent PASS and
    never a TypeError from comparing it to MINIMUM_CONTEXT_LENGTH."""
    for bad in (True, False, "64000", "not a number", []):
        accepted_case = models.classify_model_context(bad, "hermes_provider")
        assert accepted_case.accepted is True  # same as None on a native provider

        rejected_case = models.classify_model_context(bad, "openai_compatible")
        assert rejected_case.status == "rejected_unknown"  # same as None on a custom endpoint


def test_classify_declared_model_looks_up_context_and_provider_type_from_a_raw_config_dict():
    models_config = {
        "providers": {
            "xkiro": {"type": "openai_compatible"},
            "openrouter": {"type": "openrouter"},
        },
        "models": [
            {"provider": "xkiro", "model": "undeclared-model", "role_class": "coder", "pinned": True},
            {"provider": "openrouter", "model": "big-model", "context_length": 200_000,
             "role_class": "reviewer", "pinned": True},
        ],
    }
    assert models.classify_declared_model(models_config, "xkiro", "undeclared-model").status == "rejected_unknown"
    assert models.classify_declared_model(models_config, "openrouter", "big-model").accepted is True
    # A (provider, model) that config/models.yaml does not declare at all: context_length None, provider type
    # looked up from whatever the providers block does or does not say about that provider.
    unknown = models.classify_declared_model(models_config, "xkiro", "never-declared")
    assert unknown.status == "rejected_unknown"  # xkiro is still type openai_compatible
    assert models.classify_declared_model({"providers": {}, "models": []}, "nobody", "nothing").accepted is True


def test_resync_hides_but_never_deletes_a_smoke_tested_model_no_longer_declared(tmp_path):
    """STOPDOC.md item 7 (round 19, package STOPGATES, ASES-DOC-04 stop condition category 2 'deletes user
    data'): a real recorded smoke test must survive a model briefly dropping out of config/models.yaml (a
    provider swap, a typo, a rebase mid-edit), even though the model is correctly hidden from list_models the
    whole time it is undeclared (the same visible behaviour test_resync_drops_rows_no_longer_declared proves)."""
    conn = _conn(tmp_path)
    models.record_smoke_test(conn, "unorouter", "glm-5.3-thinking:free", "pass", "tool call ok")
    dropped = {"providers": CONFIG["providers"], "models": [CONFIG["models"][1]]}  # the lead row is gone

    models.sync_from_config(conn, dropped)

    assert ("unorouter", "glm-5.3-thinking:free") not in {(m.provider, m.model) for m in models.list_models(conn)}
    row = conn.execute(
        "SELECT smoke_test_result, declared FROM model_registry WHERE provider = ? AND model = ?",
        ("unorouter", "glm-5.3-thinking:free"),
    ).fetchone()
    assert row is not None, "the row must survive, not be deleted"
    assert row["smoke_test_result"] == "pass" and row["declared"] == 0

    models.sync_from_config(conn, CONFIG)  # the model comes back into config...
    by_model = {m.model: m for m in models.list_models(conn)}
    assert by_model["glm-5.3-thinking:free"].smoke_tested is True  # ...and its old smoke test is still there


def test_resync_drops_rows_no_longer_declared(tmp_path):
    """A model swapped out of config.yaml (e.g. a role moving to a different provider) must not
    linger in the registry forever as a phantom pinned row -- real bug, found by actually running
    `swarm models` after a real provider swap for lead."""
    conn = _conn(tmp_path)
    swapped = {
        "providers": CONFIG["providers"],
        "models": [
            {
                "provider": "unorouter", "model": "a-new-lead-model",
                "context_length": None, "tool_calling": None, "role_class": "lead", "pinned": True,
            },
            CONFIG["models"][1],  # the reviewer row is unchanged
        ],
    }
    models.sync_from_config(conn, swapped)
    provider_model_pairs = {(m.provider, m.model) for m in models.list_models(conn)}
    assert ("unorouter", "glm-5.3-thinking:free") not in provider_model_pairs
    assert provider_model_pairs == {
        ("unorouter", "a-new-lead-model"), ("openrouter", "some/reviewer-model")
    }
