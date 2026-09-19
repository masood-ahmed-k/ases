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
