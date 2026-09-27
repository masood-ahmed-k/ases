"""Model registry and capability cache (section 9.1: models.py; section 5).

Mirrors Hermes's own floor: MINIMUM_CONTEXT_LENGTH = 64_000, taken directly from the installed Hermes
v0.21.3 source (agent/model_metadata.py). A model that hasn't been confirmed at or above that floor,
or hasn't had a real tool-calling smoke test recorded, is not fit for an agent role yet (ASES-MOD-02,
ASES-MOD-04) -- this module tracks that state, it does not decide policy on top of it (policy.py does).
"""
from __future__ import annotations

import dataclasses
import sqlite3
from datetime import datetime, timezone

MINIMUM_CONTEXT_LENGTH = 64_000

# config/models.yaml's providers.<name>.type value for a router with no built-in Hermes knowledge of its
# own: Hermes talks to it purely as an OpenAI-compatible HTTP endpoint (xKiro today). Any other declared
# type (openrouter, hermes_provider) -- or none at all -- is a provider Hermes already has native knowledge
# of, or at least did not itself name here as "custom, OpenAI-compatible" (ASES-MOD-02).
_CUSTOM_OPENAI_COMPATIBLE_TYPE = "openai_compatible"


@dataclasses.dataclass(frozen=True)
class ModelContextDecision:
    """The verdict of classify_model_context: `status` is one of "accepted", "rejected_too_small" (a
    declared context_length under MINIMUM_CONTEXT_LENGTH) or "rejected_unknown" (no declared context_length
    on a custom OpenAI-compatible endpoint). `reason` is a human-readable sentence naming the declared value
    and the minimum, fit to print verbatim in a refusal message."""
    status: str
    reason: str

    @property
    def accepted(self) -> bool:
        return self.status == "accepted"


def classify_model_context(context_length: int | None, provider_type: str | None) -> ModelContextDecision:
    """ASES-MOD-02 / acceptance 22.4: the one decision of whether a model is fit to use, from its declared
    context_length and its provider's config/models.yaml `type`. Three outcomes:

    - accepted: context_length is declared and >= MINIMUM_CONTEXT_LENGTH, OR context_length is undeclared
      but the provider is not a custom OpenAI-compatible endpoint (see below).
    - rejected_too_small: context_length IS declared, but below MINIMUM_CONTEXT_LENGTH. This is an explicit,
      known fact about the model and is never excused by provider type (acceptance 22.4 step 1: "Register a
      model declared at 16K: the controller must reject it before any card starts").
    - rejected_unknown: context_length is undeclared (None) AND the provider's type is
      "openai_compatible" -- a custom, OpenAI-compatible endpoint (xKiro today; see config/models.yaml's own
      comment on providers.xkiro.type). This is the ONLY case that gets "rejected as unknown": the Hermes
      fact table (blueprint p121) says plainly "Custom OpenAI-compatible endpoints often cannot report their
      context length, so Hermes relies on model.context_length, a per-model entry under custom_providers, or
      probing" -- i.e. the blueprint's "unknown" language is specifically about custom endpoints, not about
      providers in general.

    What about a NATIVE Hermes provider (config/models.yaml type "openrouter" or "hermes_provider", or no
    type declared at all) with no declared context_length? It is accepted, not rejected as unknown. Hermes
    ships with, or actively probes, its own knowledge of these providers' models (p121's "or probing" is
    exactly this path) and already enforces its own 64K floor there; ASES declaring context_length for them
    is a nice-to-have cross-check (still WARNed on by swarm doctor, see doctor._check_model_registry), not
    the load-bearing fact it is for a custom endpoint Hermes cannot introspect. Treating an undeclared
    native-provider model the same as an undeclared custom-endpoint model would also be inconsistent with
    the plain reading of p121 above, which draws the line at "custom OpenAI-compatible endpoints" by name.

    context_length is treated as "declared" only when it is a real int (never a bool: bool is an int
    subclass in Python, and a config/models.yaml typo that yields True/False is not a context length worth
    trusting). Anything else not-None -- a stray string from a config typo, for instance -- is treated the
    same as a plain undeclared value rather than raising, so a malformed config/models.yaml cannot crash the
    doctor or the approve/run pre-flight; it degrades to the same WARN/rejected-unknown handling undeclared
    gets, never a silent false PASS."""
    if isinstance(context_length, int) and not isinstance(context_length, bool):
        if context_length >= MINIMUM_CONTEXT_LENGTH:
            return ModelContextDecision(
                "accepted", f"declared context {context_length} >= {MINIMUM_CONTEXT_LENGTH}"
            )
        return ModelContextDecision(
            "rejected_too_small",
            f"declared context {context_length} is below the {MINIMUM_CONTEXT_LENGTH} floor",
        )
    if provider_type == _CUSTOM_OPENAI_COMPATIBLE_TYPE:
        return ModelContextDecision(
            "rejected_unknown",
            f"no declared context_length in config/models.yaml, on a custom OpenAI-compatible endpoint "
            f"(provider type {provider_type!r}); Hermes cannot report its own context length there (p121), "
            f"so this model is rejected as unknown until config/models.yaml declares a context_length "
            f">= {MINIMUM_CONTEXT_LENGTH}",
        )
    return ModelContextDecision(
        "accepted",
        f"no declared context_length, but provider type {provider_type!r} is not a custom OpenAI-compatible "
        f"endpoint -- p121's 'rejected as unknown' language is specifically about those, so this is treated "
        f"as accepted on the strength of Hermes's own native provider knowledge or probing",
    )


def classify_declared_model(models_config: dict, provider: str, model: str) -> ModelContextDecision:
    """classify_model_context for a (provider, model) pair, read straight from a raw config/models.yaml dict
    (not the database): looks up the model's declared context_length and its provider's declared type. A
    (provider, model) with no matching row classifies as an undeclared model on that provider (context_length
    None); a provider with no entry at all classifies as provider_type None, the same as one that declares no
    type."""
    entry = next(
        (m for m in models_config.get("models") or ()
         if m.get("provider") == provider and m.get("model") == model),
        None,
    ) or {}
    provider_type = ((models_config.get("providers") or {}).get(provider) or {}).get("type")
    return classify_model_context(entry.get("context_length"), provider_type)


@dataclasses.dataclass(frozen=True)
class ModelRecord:
    provider: str
    model: str
    context_length: int | None
    tool_calling: bool | None
    role_class: str | None
    data_policy: str | None
    pinned: bool
    smoke_test_at: str | None
    smoke_test_result: str | None
    smoke_test_detail: str | None

    @property
    def context_declared_and_sufficient(self) -> bool:
        return self.context_length is not None and self.context_length >= MINIMUM_CONTEXT_LENGTH

    @property
    def smoke_tested(self) -> bool:
        return self.smoke_test_result == "pass"


def sync_from_config(conn: sqlite3.Connection, models_config: dict) -> None:
    """Upsert config/models.yaml's declared models into model_registry, then drop any row for a
    (provider, model) pair no longer declared anywhere in config.

    Declared fields (context_length, tool_calling, role_class, data_policy, pinned) are refreshed from
    config every time -- config is the source of truth for those. Smoke-test columns are left alone if
    the row already exists, so re-running this never erases a previously recorded smoke test.

    The delete step is real, not defensive: renaming/retiring a model (e.g. lead moving from
    openai/gpt-5.6-terra to xkiro/openai/gpt-5.6-terra) used to leave the old row behind forever, so
    `swarm models`/`swarm doctor` kept showing two "pinned, role=lead" rows -- caught by actually running
    `swarm models` after a real provider swap, not by a unit test (a fake config that always matches
    what's asserted has no way to exercise "a row that used to be there isn't anymore").
    """
    providers = models_config.get("providers", {})
    declared: set[tuple[str, str]] = set()
    for entry in models_config.get("models", []):
        provider = entry["provider"]
        model = entry["model"]
        declared.add((provider, model))
        data_policy = entry.get("data_policy") or providers.get(provider, {}).get("data_policy")
        conn.execute(
            """
            INSERT INTO model_registry (provider, model, context_length, tool_calling, role_class,
                                         data_policy, pinned)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider, model) DO UPDATE SET
                context_length = excluded.context_length,
                tool_calling   = excluded.tool_calling,
                role_class     = excluded.role_class,
                data_policy    = excluded.data_policy,
                pinned         = excluded.pinned
            """,
            (
                provider,
                model,
                entry.get("context_length"),
                entry.get("tool_calling"),
                entry.get("role_class"),
                data_policy,
                1 if entry.get("pinned") else 0,
            ),
        )

    existing = {(r["provider"], r["model"]) for r in conn.execute("SELECT provider, model FROM model_registry")}
    for provider, model in existing - declared:
        conn.execute("DELETE FROM model_registry WHERE provider = ? AND model = ?", (provider, model))


def list_models(conn: sqlite3.Connection) -> list[ModelRecord]:
    rows = conn.execute(
        "SELECT provider, model, context_length, tool_calling, role_class, data_policy, pinned, "
        "smoke_test_at, smoke_test_result, smoke_test_detail FROM model_registry ORDER BY provider, model"
    ).fetchall()
    return [
        ModelRecord(
            provider=r["provider"],
            model=r["model"],
            context_length=r["context_length"],
            tool_calling=None if r["tool_calling"] is None else bool(r["tool_calling"]),
            role_class=r["role_class"],
            data_policy=r["data_policy"],
            pinned=bool(r["pinned"]),
            smoke_test_at=r["smoke_test_at"],
            smoke_test_result=r["smoke_test_result"],
            smoke_test_detail=r["smoke_test_detail"],
        )
        for r in rows
    ]


def record_smoke_test(
    conn: sqlite3.Connection, provider: str, model: str, result: str, detail: str = ""
) -> None:
    if result not in ("pass", "fail"):
        raise ValueError("result must be 'pass' or 'fail'")
    conn.execute(
        "UPDATE model_registry SET smoke_test_at = ?, smoke_test_result = ?, smoke_test_detail = ? "
        "WHERE provider = ? AND model = ?",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"), result, detail, provider, model),
    )
    if conn.execute("SELECT changes()").fetchone()[0] == 0:
        raise KeyError(f"no such model in registry: {provider}/{model} (sync_from_config first)")
