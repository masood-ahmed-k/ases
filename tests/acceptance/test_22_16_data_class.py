"""Acceptance 22.16: data class (blueprint.txt [p429]/[p430], table 32, section 21.2).

"Starting without a declared data class must fail. With class confidential and only remote providers
configured, the run must refuse to start. With class private, cards must never be routed to a provider
marked as training on inputs, even when every other provider is exhausted: they park instead."

spec/requirements.yaml (read before writing this file, per the project's own "check the requirements source"
rule):
  ASES-PRV-01 (covered): "A data class per project is enforced before any other routing rule." Its note:
    policy.check_data_class is called for every task's resolved provider in cmd_approve, before Gate P
    publication.
  ASES-PRV-02 (covered): "The default class is private; no run without a declared class." Its note is more
    precise than the requirement text: there IS no default at all, not even private -- config.py requires
    project.data_class to be one of public/private/confidential explicitly, always. Test 1 below tests what
    the note says is actually built (no run without a declared class), not a silent default to private that
    does not exist.
  ASES-PRV-03 (covered): "The controller never relaxes the data class to keep work flowing." Its note:
    check_data_class raises rather than returning a bool, so there is no silent-downgrade code path.

Where the note and the requirement's own summary text differ (PRV-02's "default is private" versus "no
default at all"), the note is the more specific, closer-to-the-code statement, and this file follows it,
reporting the difference rather than quietly picking one (per the project's own instruction that the source
wins but a disagreement is reported).
"""
from __future__ import annotations

import dataclasses
import json
import pathlib

import pytest
import yaml

from ases import cli as cli_mod
from ases import config as config_mod
from ases import controller as controller_mod
from ases import db
from ases import events
from ases import plan as plan_mod
from ases import policy as policy_mod

KNOWN_ROLES = {"lead", "coder", "reviewer"}


def _project(tmp_path: pathlib.Path, *, data_class: str) -> config_mod.ProjectConfig:
    return config_mod.ProjectConfig(
        name="demo-16", environment="native", data_class=data_class, workspace_root=tmp_path / "ws",
        ases_home=tmp_path / "home", board="ases-test", integration_branch="integration",
        roles={"lead": "lead", "coder": "coder-1", "reviewer": "reviewer"},
        concurrency={"max_in_progress": 3, "per_profile": 1, "hard_max": 6},
        budgets={
            "attempts_per_card": 3, "review_rounds_per_task": 3, "fix_cards_per_task": 2,
            "replans_per_project": 2, "max_cards": 40, "card_runtime_minutes": 45,
            "daily_reserve_percent": 10, "review_reserve_requests": 20,
        },
        hermes_tested_version="0.21.3", hermes_native_home=tmp_path / "hermes",
    )


# ---------------------------------------------------------------------------------------------
# 1. Starting without a declared data class must fail (ASES-PRV-02).
# ---------------------------------------------------------------------------------------------


def test_22_16_a_project_config_cannot_be_built_at_all_without_a_declared_data_class(tmp_path):
    """data_class is a required field of config.ProjectConfig with no default (it comes before the two
    fields that DO have defaults, sandbox and retention): the dataclass itself refuses to be constructed
    without one, which is the strongest possible version of "no run without a declared class". This IS the
    pass, exactly as r6_wp_ac_g.md anticipates for a field with no default: nothing downstream can ever see a
    ProjectConfig with a missing or silently-defaulted data_class, because Python will not build one."""
    with pytest.raises(TypeError):
        config_mod.ProjectConfig(
            name="x", environment="native", workspace_root=tmp_path / "ws", ases_home=tmp_path / "home",
            board="b", integration_branch="integration", roles={}, concurrency={}, budgets={},
            hermes_tested_version="0.21.3", hermes_native_home=tmp_path / "hermes",
        )  # data_class= is omitted entirely


def test_22_16_swarm_run_refuses_before_anything_is_dispatched_when_swarm_yaml_has_no_data_class(tmp_path):
    """cli._run_loop (cmd_run's real body) calls config.load_swarm_config as its very first line, before
    opening the database, loading the plan, or touching hermes/the board in any way. A config/swarm.yaml with
    no project.data_class key fails right there, with ConfigError, so "swarm run" is refused before anything
    is dispatched, exactly as this test's name says and exactly what the work order asks to be shown
    (read cli.cmd_run's real pre-flight checks: _load_project() is the first thing _run_loop calls, before
    _open_conn, _load_plan or verify_gate_pin)."""
    swarm_yaml = tmp_path / "swarm.yaml"
    swarm_yaml.write_text(yaml.safe_dump({
        "project": {
            "name": "x", "environment": "native",
            # data_class deliberately left undeclared
            "workspace_root": str(tmp_path / "ws"), "ases_home": str(tmp_path / "home"),
            "board": "b", "integration_branch": "integration",
        },
        "roles": {}, "concurrency": {}, "budgets": {},
        "hermes": {"tested_version": "0.21.3", "native_home": str(tmp_path / "hermes")},
    }), encoding="utf-8")

    with pytest.raises(config_mod.ConfigError, match="data_class"):
        config_mod.load_swarm_config(swarm_yaml)


# ---------------------------------------------------------------------------------------------
# 2. confidential with only remote providers configured must refuse to start (ASES-PRV-01).
# ---------------------------------------------------------------------------------------------


CONFIDENTIAL_PLAN = {
    "project": "demo-16-confidential", "integration_branch": "integration",
    "gate_profiles": {"trivial": ["echo ok"]},
    "tasks": [
        {"key": "T1", "title": "write code", "role": "coder", "depends_on": [], "touches": ["a.py"],
         "acceptance": ["a.py exists"], "gate_profile": "trivial", "estimated_requests": 3},
        {"key": "T2", "title": "review it", "role": "reviewer", "depends_on": ["T1"], "touches": [],
         "acceptance": ["reviewed"], "gate_profile": "trivial", "estimated_requests": 3},
    ],
}


def test_22_16_confidential_with_only_remote_providers_refuses_at_gate_p_before_any_card_exists(tmp_path):
    """ASES-PRV-01: "A data class per project is enforced before any other routing rule". With
    data_class=confidential and no provider declaring itself local_only (policy._SAFE_FOR_CONFIDENTIAL), Gate
    P must refuse. cli._estimate_lines is the real function cmd_approve calls for this (this test calls the
    pure function directly, per r6_wp_ac_g.md, rather than driving the CLI command): before this plan's own
    per-task loop even starts, before publish_plan or create_cards_from_plan would ever run.

    Round 19 (package STOPGATES, STOPDOC.md/PROVIDERS.md item 2): _estimate_lines now checks the lead and
    reviewer roles' own providers FIRST, ahead of the per-task loop, since a committing task's diff always
    ends up with the reviewer whether or not "reviewer" also happens to be a plan task's own role (T2 here
    happens to name it, but that is no longer why this is checked). So it is the reviewer's remote-b that
    stops the estimate now, not the coder's remote-a the per-task loop would have reached next -- either one
    alone is still enough to refuse the whole plan, which is the point this test proves; the second half below
    confirms both are independently unsafe regardless of which one a caller happens to see first."""
    plan = plan_mod.parse_and_validate(CONFIDENTIAL_PLAN, known_roles=KNOWN_ROLES, max_cards=40)
    project = _project(tmp_path, data_class="confidential")
    models_config = {
        "providers": {
            "remote-a": {"limits": {}},  # no data_policy declared at all: policy.py treats that as "unknown"
            "remote-b": {"data_policy": "trains_on_input", "limits": {}},
        },
        "models": [
            {"provider": "remote-a", "model": "m-coder", "role_class": "coder", "pinned": True},
            {"provider": "remote-b", "model": "m-reviewer", "role_class": "reviewer", "pinned": True},
        ],
    }
    conn = db.connect(tmp_path / "ases.db")

    estimate = cli_mod._estimate_lines(plan, project, models_config, conn)

    assert estimate.policy_violation is not None
    assert "confidential" in estimate.policy_violation and "remote-b" in estimate.policy_violation
    assert not estimate.budget_lines and not estimate.calendar_lines  # the estimate stops, nothing to budget

    # ASES-PRV-01 says this is enforced for EVERY role's provider, not just the one _estimate_lines happens to
    # reach first (it stops at the first violation, by design: there is nothing to budget for a plan that
    # cannot run at all). Calling policy.check_data_class directly, the way Gate P's own per-task loop in
    # cli.cmd_approve does (reproduced here since cli.py is read-only for this package), confirms EACH role's
    # provider is refused on its own, not only the first one encountered.
    for role, provider, declared_policy in (
        ("coder", "remote-a", None), ("reviewer", "remote-b", "trains_on_input"),
    ):
        with pytest.raises(policy_mod.DataPolicyViolation):
            policy_mod.check_data_class("confidential", provider, declared_policy)


# ---------------------------------------------------------------------------------------------
# 3. private must never route to a training-on-inputs provider (ASES-PRV-01/03, section 21.2, table 32).
# ---------------------------------------------------------------------------------------------


PRIVATE_PLAN = {
    "project": "demo-16-private", "integration_branch": "integration",
    "gate_profiles": {"trivial": ["echo ok"]},
    "tasks": [
        {"key": "T1", "title": "add a", "role": "coder", "depends_on": [], "touches": ["a.py"],
         "acceptance": ["a.py defines add(x, y) returning x + y"], "gate_profile": "trivial",
         "estimated_requests": 5},
    ],
}

_TRAINS_ON_INPUT_MODELS_CONFIG = {
    "providers": {"trains-on-input": {"data_policy": "trains_on_input", "limits": {}}},
    "models": [{"provider": "trains-on-input", "model": "m-coder", "role_class": "coder", "pinned": True}],
}


def test_22_16_private_refuses_a_training_on_inputs_provider_at_gate_p(tmp_path):
    """Section 21.2/table 32: a provider whose declared data_policy is not in policy._SAFE_FOR_PRIVATE
    ({no_training, local_only, zero_data_retention}) is refused for data_class=private, at Gate P, before any
    card is created. This is the ONE real, always-enforced guarantee (see the finding below): it is what
    actually stands between a private-class project and a training-on-inputs provider today."""
    plan = plan_mod.parse_and_validate(PRIVATE_PLAN, known_roles=KNOWN_ROLES, max_cards=40)
    project = _project(tmp_path, data_class="private")
    conn = db.connect(tmp_path / "ases.db")

    estimate = cli_mod._estimate_lines(plan, project, _TRAINS_ON_INPUT_MODELS_CONFIG, conn)

    assert estimate.policy_violation is not None
    assert "trains-on-input" in estimate.policy_violation and "private" in estimate.policy_violation


def test_22_16_the_per_pass_budget_gate_now_parks_a_card_whose_provider_turned_data_class_unsafe(world_factory):
    """Round 6 found a real gap here (see the round 6 section of builder-findings.md, ASES-PRV-01): the per-pass
    budget gate never re-checked a card's provider against the project's data class, only Gate P did, once, at
    approval time. Round 7's package FIXES closed it: controller._affordable_now now calls
    policy.check_data_class FIRST (mirroring Gate P's own order, "enforced before any other routing rule"),
    parking with a "data class:" reason on violation, through the SAME process_budget_gate/process_unpark path
    an ordinary budget shortfall uses -- except a data-class park is deliberately excluded from _PARK_PREFIXES,
    so process_unpark can never auto-resume it (ASES-PRV-03: the controller must never relax the data class to
    keep work flowing). This test asserts the FIXED behavior in place of the gap round 6 documented."""
    world = world_factory(plan_raw=PRIVATE_PLAN, models_config=dict(_TRAINS_ON_INPUT_MODELS_CONFIG))
    # As if this project had been approved under a data class that later needed tightening to private, or
    # Gate P's one-time check had been bypassed (--skip-critic skips only the critic, never this check, so in
    # the real system this specific combination cannot arise post-approval today; the point here is what the
    # per-pass gate does once a card whose provider is unsafe is already on the board).
    world.project = dataclasses.replace(world.project, data_class="private")
    t1 = world.create_cards()["T1"]
    assert world.fake.card(t1.work_card_id)["status"] == "ready"

    parked = controller_mod.process_budget_gate(
        world.board, world.plan, world.models_config, conn=world.conn, budgets=world.project.budgets,
        project=world.project,
    )

    assert parked == ["T1"]  # process_budget_gate now finds and acts on the data-class violation
    assert world.fake.card(t1.work_card_id)["status"] == "scheduled"
    park_events = [e for e in events.recent(world.conn, limit=200) if e["kind"] == "card_parked_for_budget"]
    assert park_events and json.loads(park_events[0]["payload"])["reason"].startswith("data class:")

    # ASES-PRV-03: never auto-unparked, even once the ordinary budget side of the same check would allow it.
    unparked = controller_mod.process_unpark(
        world.board, world.plan, world.models_config, conn=world.conn, budgets=world.project.budgets,
        project=world.project,
    )
    assert unparked == []
    assert world.fake.card(t1.work_card_id)["status"] == "scheduled"  # left exactly where it was
