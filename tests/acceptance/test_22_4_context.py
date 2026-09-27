"""Acceptance 22.4: an under-declared model is rejected before any card starts (blueprint.txt [p405]/[p406]).

[p405] ## 22.4 Context test
[p406] "Register a model declared at 16K: the controller must reject it before any card starts. Register one
with no declared context on a custom endpoint: rejected as unknown. Declare 64K or more and repeat: accepted."

spec/requirements.yaml (read before writing this file, per the project's own "check the requirements source"
rule):
  ASES-MOD-02 (in_progress before this package; verified_by 22.4): "Every model used through a custom endpoint
    has a declared context length of at least 64K before first use." The register's own note (2026-09-27,
    ROUND 9 hygiene) says exactly what was missing: "the controller does not yet enforce the context-length
    rejection 22.4 describes, only the classification primitive is unit-tested in test_models.py" -- this
    package (MOD02) is what closes that gap, and this file is the acceptance-level test the note says did not
    exist yet. The architect updates the register itself; this package does not touch spec/requirements.yaml.

Hermes fact (blueprint.txt, table under [p121]): "Hermes rejects any model with less than 64,000 tokens of
context ... Custom OpenAI-compatible endpoints often cannot report their context length, so Hermes relies on
model.context_length, a per-model entry under custom_providers, or probing." config/models.yaml marks that
custom-endpoint kind with providers.<name>.type: openai_compatible (see its own xkiro block and comment).

cli._estimate_lines is the real function swarm approve calls before controller.publish_plan or
controller.create_cards_from_plan ever run (this file calls it directly, the same way
tests/acceptance/test_22_16_data_class.py does for the data-policy check, per r6_wp_ac_g.md's "call the pure
function directly" pattern -- cli.py is a file this package DOES own, unlike test_22_16's package, but the
same direct-call style keeps this test about the decision, not argv/console plumbing already covered by
tests/unit/test_cli_commands.py and tests/unit/test_cli_run.py).

Before/after proof (see this package's report for the full transcript): reverting src/ases/cli.py to the
`git show HEAD:src/ases/cli.py` version (before this package's fix) and re-running
test_22_4_a_model_declared_at_16k_is_rejected_before_any_card_starts against it fails -- estimate.model_rejected
does not exist as a field on the old Estimate at all (AttributeError), and the OLD cli._estimate_lines happily
returns budget/calendar lines for the 16K model with no refusal of any kind, exactly as the work order's "swarm
approve creates cards and swarm run starts regardless" description says. Restoring the fixed file makes it pass
again with a clean `git diff --stat` showing only the intended files changed.
"""
from __future__ import annotations

from ases import cli as cli_mod

PLAN_22_4 = {
    "project": "demo-22-4",
    "integration_branch": "integration",
    "gate_profiles": {"trivial": ["echo ok"]},
    "tasks": [
        {"key": "T1", "title": "write code", "role": "coder", "depends_on": [], "touches": ["a.py"],
         "acceptance": ["a.py defines add(x, y) returning x + y"], "gate_profile": "trivial",
         "estimated_requests": 3},
    ],
}


def _models_config(context_length: int | None, *, provider_type: str = "openai_compatible") -> dict:
    """One provider, one pinned coder model, everything else minimal: what p406's three steps vary is only
    context_length (and, for the "custom endpoint" wording, the provider's declared type)."""
    return {
        "providers": {"custom": {"type": provider_type, "limits": {}, "data_policy": "unknown"}},
        "models": [{
            "provider": "custom", "model": "m-coder", "context_length": context_length,
            "tool_calling": True, "role_class": "coder", "pinned": True,
        }],
    }


# ---------------------------------------------------------------------------------------------
# Step 1: a model declared at 16K -- the controller must reject it before any card starts.
# ---------------------------------------------------------------------------------------------


def test_22_4_a_model_declared_at_16k_is_rejected_before_any_card_starts(world_factory):
    world = world_factory(plan_raw=PLAN_22_4, models_config=_models_config(16_000))

    estimate = cli_mod._estimate_lines(world.plan, world.project, world.models_config, world.conn)

    assert estimate.model_rejected is not None
    assert "coder" in estimate.model_rejected and "custom/m-coder" in estimate.model_rejected
    assert "rejected too small" in estimate.model_rejected and "16000" in estimate.model_rejected
    # Nothing was budgeted, and no card was ever asked for: this test never calls world.create_cards() at
    # all, the same way swarm approve's real cmd_approve returns 1 before publish_plan or
    # create_cards_from_plan are reached once estimate.model_rejected is set.
    assert estimate.budget_lines == () and estimate.calendar_lines == ()
    assert world.pairs == {}
    assert "Gate P would REFUSE this plan (context length, ASES-MOD-02)" in estimate.text()


# ---------------------------------------------------------------------------------------------
# Step 2: no declared context on a custom endpoint -- rejected as unknown.
# ---------------------------------------------------------------------------------------------


def test_22_4_an_undeclared_model_on_a_custom_endpoint_is_rejected_as_unknown(world_factory):
    world = world_factory(plan_raw=PLAN_22_4, models_config=_models_config(None, provider_type="openai_compatible"))

    estimate = cli_mod._estimate_lines(world.plan, world.project, world.models_config, world.conn)

    assert estimate.model_rejected is not None
    assert "rejected unknown" in estimate.model_rejected
    assert "openai_compatible" in estimate.model_rejected
    assert world.pairs == {}


def test_22_4_the_same_undeclared_model_on_a_native_hermes_provider_is_not_rejected_as_unknown(world_factory):
    """Not one of p406's three named steps, but the necessary contrast that makes step 2 mean what it says: the
    blueprint's "rejected as unknown" is specifically about a CUSTOM endpoint (p121), so the same undeclared
    context on a native Hermes provider (type openrouter or hermes_provider) must not be refused the same way
    -- models.classify_model_context's own documented reading of p121, exercised here at the approve layer."""
    world = world_factory(plan_raw=PLAN_22_4, models_config=_models_config(None, provider_type="hermes_provider"))

    estimate = cli_mod._estimate_lines(world.plan, world.project, world.models_config, world.conn)

    assert estimate.model_rejected is None


# ---------------------------------------------------------------------------------------------
# Step 3: declare 64K or more and repeat -- accepted, and cards ARE created.
# ---------------------------------------------------------------------------------------------


def test_22_4_a_model_declared_at_64k_or_more_is_accepted_and_cards_are_created(world_factory):
    world = world_factory(plan_raw=PLAN_22_4, models_config=_models_config(64_000))

    estimate = cli_mod._estimate_lines(world.plan, world.project, world.models_config, world.conn)
    assert estimate.model_rejected is None

    pairs = world.create_cards()  # the real controller.create_cards_from_plan, as swarm approve calls it next

    assert set(pairs) == {"T1"}
