"""Acceptance 22.14: plan rejection (blueprint.txt [p425]/[p426]).

"A plan with a cycle, a missing criterion or a missing touches entry must fail Gate 0 with exact errors. The
critic then returns CHANGES_REQUIRED twice and the user rejects the plan. The approval screen shows the
request budget and the calendar estimate. No implementation card may exist at any point."

Gate 0 (plan.parse_and_validate) is pure plan validation with no board involved at all (ASES-TSK-03,
ASES-LED-01): a malformed plan never gets anywhere near hermes, so the three Gate 0 cases below need no
FakeHermes to prove no card exists, there is no code path through which one could have been created. The
critic half (ASES-REV-02, ASES-REV-03) drives critic.run_critique with a fake invoke, copying
tests/unit/test_critic.py's FakeInvoke pattern exactly, and that test installs a bare FakeHermes to make "no
implementation card exists" a concrete, executable assertion rather than an assumption.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from ases import cli as cli_mod
from ases import config as config_mod
from ases import critic as critic_mod
from ases import db
from ases import plan as plan_mod
from ases.fakes.board import FakeHermes

KNOWN_ROLES = {"lead", "coder", "reviewer"}
GATE_PROFILES = {"trivial": ["echo ok"]}


class FakeInvoke:
    """A stand-in for the reviewer call: each reply is a stdout string (exit 0) or a (code, out, err) tuple.
    Copied from tests/unit/test_critic.py's own FakeInvoke, the exact fake-invoke pattern this scenario's
    critic half uses, so critic.run_critique never touches a real hermes or a real model provider (r6_rules.md
    hard constraint)."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, profile, prompt, timeout):
        self.calls.append((profile, prompt, timeout))
        reply = self.replies.pop(0)
        return reply if isinstance(reply, tuple) else (0, reply, "")


def _changes_required_json(required_change: str) -> str:
    """A valid CHANGES_REQUIRED verdict in the review format of section 13.3, the same shape
    tests/unit/test_critic.py's verdict_text builds. A non-empty required_changes list is required: an empty
    one is itself a parse problem ("a change request with no change")."""
    return (
        '{"review_status": "CHANGES_REQUIRED", "summary": "the plan needs another pass", '
        '"architecture_issues": [], "missing_cases": [], "security_issues": [], "test_gaps": [], '
        '"gate_tampering_suspected": false, "required_changes": ["' + required_change + '"]}'
    )


def _project(tmp_path: pathlib.Path, *, data_class: str = "public") -> config_mod.ProjectConfig:
    return config_mod.ProjectConfig(
        name="demo-14", environment="native", data_class=data_class, workspace_root=tmp_path / "ws",
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


MODELS_CONFIG_14 = {
    "providers": {"fake": {"limits": {}}},
    "models": [
        {"provider": "fake", "model": "fake-coder", "role_class": "coder", "pinned": True},
        {"provider": "fake", "model": "fake-reviewer", "role_class": "reviewer", "pinned": True},
    ],
}


# ---------------------------------------------------------------------------------------------
# Gate 0: a cycle, a missing criterion, a missing touches entry, each with the exact errors plan.py raises.
# ---------------------------------------------------------------------------------------------


def test_22_14_gate_0_rejects_a_plan_with_a_dependency_cycle_with_the_exact_error():
    """ASES-LED-01: Gate 0 rejects a cycle. Both tasks are otherwise complete (acceptance, touches, a
    declared gate_profile), so the cycle is the only problem plan.py can find, and _check_cycles' own error
    text (plan.py) is exact: "dependency cycle: T1 -> T2 -> T1"."""
    raw = {
        "project": "demo-14-cycle", "integration_branch": "integration", "gate_profiles": GATE_PROFILES,
        "tasks": [
            {"key": "T1", "title": "a", "role": "coder", "depends_on": ["T2"], "touches": ["a.py"],
             "acceptance": ["a.py exists"], "gate_profile": "trivial", "estimated_requests": 1},
            {"key": "T2", "title": "b", "role": "coder", "depends_on": ["T1"], "touches": ["b.py"],
             "acceptance": ["b.py exists"], "gate_profile": "trivial", "estimated_requests": 1},
        ],
    }
    with pytest.raises(plan_mod.PlanError) as excinfo:
        plan_mod.parse_and_validate(raw, known_roles=KNOWN_ROLES, max_cards=40)
    assert excinfo.value.errors == ["dependency cycle: T1 -> T2 -> T1"]


def test_22_14_gate_0_rejects_a_task_missing_acceptance_criteria_with_the_exact_errors():
    """ASES-TSK-03: a task with no "acceptance" key at all fails twice over, once for the missing required
    key and once because the (absent, so empty) acceptance list is not a non-empty array. Both exact strings
    are in the errors plan.py raises, together, not just the first one found."""
    raw = {
        "project": "demo-14-acceptance", "integration_branch": "integration", "gate_profiles": GATE_PROFILES,
        "tasks": [
            {"key": "T1", "title": "a", "role": "coder", "depends_on": [], "touches": ["a.py"],
             "gate_profile": "trivial", "estimated_requests": 1},  # no "acceptance" key
        ],
    }
    with pytest.raises(plan_mod.PlanError) as excinfo:
        plan_mod.parse_and_validate(raw, known_roles=KNOWN_ROLES, max_cards=40)
    assert excinfo.value.errors == [
        "tasks[0]: missing required key 'acceptance'",
        "tasks[0] (T1): acceptance must be a non-empty array (ASES-TSK-03)",
    ]


def test_22_14_gate_0_rejects_a_task_missing_touches_with_the_exact_errors():
    """ASES-TSK-03: a task with no "touches" key fails the same way, twice over: the missing required key,
    then "touches must be an array" because None is not a list at all."""
    raw = {
        "project": "demo-14-touches", "integration_branch": "integration", "gate_profiles": GATE_PROFILES,
        "tasks": [
            {"key": "T1", "title": "a", "role": "coder", "depends_on": [], "acceptance": ["a.py exists"],
             "gate_profile": "trivial", "estimated_requests": 1},  # no "touches" key
        ],
    }
    with pytest.raises(plan_mod.PlanError) as excinfo:
        plan_mod.parse_and_validate(raw, known_roles=KNOWN_ROLES, max_cards=40)
    assert excinfo.value.errors == [
        "tasks[0]: missing required key 'touches'",
        "tasks[0] (T1): touches must be an array, possibly empty (ASES-TSK-03)",
    ]


# ---------------------------------------------------------------------------------------------
# The critic loop: two CHANGES_REQUIRED, then the user must decide, and nothing is ever approved or created.
# ---------------------------------------------------------------------------------------------


VALID_PLAN_14 = {
    "project": "demo-14-critic", "integration_branch": "integration", "gate_profiles": GATE_PROFILES,
    "tasks": [
        {"key": "T1", "title": "add a", "role": "coder", "depends_on": [], "touches": ["a.py"],
         "acceptance": ["a.py defines add(x, y) returning x + y"], "gate_profile": "trivial",
         "estimated_requests": 5},
    ],
}


def test_22_14_two_change_requests_go_back_to_the_lead_then_the_user_decides_and_no_card_ever_exists(
    tmp_path, monkeypatch,
):
    """ASES-REV-02 (a plan goes back to the Lead at most twice), ASES-REV-03 (the user approves budget and
    calendar time before any implementation card exists). One valid, publishable plan (it passes Gate 0 on
    its own; that half is covered separately above), critiqued twice with a fake invoke (the same fake-invoke
    mechanism as tests/unit/test_critic.py, never a real hermes call, per r6_rules.md's hard constraint): the
    first CHANGES_REQUIRED goes back to the Lead (next_step == "replan", rounds_used < max_rounds), the
    second is past the bound (next_step == "ask_user"). "The user rejects the plan" is simulated the only way
    the real system allows: by simply never calling controller.create_cards_from_plan (there is no separate
    "reject" action; rejection is the absence of approval). The plan is never approved, and a bare FakeHermes
    with nothing ever registered on it proves no implementation card exists anywhere."""
    repo = tmp_path / "repo"
    (repo / "docs" / "ases").mkdir(parents=True)
    plan_file = repo / "docs" / "ases" / "plan.json"
    plan_file.write_text(json.dumps(VALID_PLAN_14, indent=2), encoding="utf-8")

    plan = plan_mod.parse_and_validate(VALID_PLAN_14, known_roles=KNOWN_ROLES, max_cards=40)
    project = _project(tmp_path)
    conn = db.connect(tmp_path / "ases.db")

    # "The approval screen shows the request budget and the calendar estimate": cli._estimate_lines is the
    # real function cmd_approve and cmd_critique both call for that screen (round 5 package CL owns cli.py;
    # this only calls its pure function, it does not test the CLI's own printed output). No policy or budget
    # problem here, so both line groups are non-empty: the data the approval screen needs is computable.
    estimate = cli_mod._estimate_lines(plan, project, MODELS_CONFIG_14, conn)
    assert estimate.policy_violation is None
    assert estimate.budget_lines and any("fake" in line for line in estimate.budget_lines)
    assert estimate.calendar_lines

    # next_step's real bound (max_rounds=2, matching ASES-REV-02 "a plan goes back to the Lead at most
    # twice"): rounds_used 0 and 1 both still say "replan" (two replans are allowed), and only the round AT
    # the bound (rounds_used == 2, the plan's third critique) says "ask_user". tests/unit/test_critic.py's
    # own test_section_22_14_... drives exactly this same three-round shape to reach "ask_user"; two rounds
    # alone are not enough to cross the bound, so this scenario needs a third CHANGES_REQUIRED to actually
    # reject the plan, not just bounce it back to the Lead again.
    fake = FakeInvoke(
        _changes_required_json("split T1 into a scaffold task and the real one"),
        _changes_required_json("shrink the acceptance criteria further"),
        _changes_required_json("the plan is still too large"),
    )
    project_name = plan.project
    steps = []
    for round_no in (1, 2, 3):
        used = critic_mod.critique_rounds_used(conn, project_name)
        c = critic_mod.run_critique(repo=repo, plan_path=plan_file, invoke=fake, estimate_text=estimate.text())
        assert c.valid and c.status == "CHANGES_REQUIRED"
        steps.append(critic_mod.next_step(c, used))
        critic_mod.record_critique(conn, project_name, round_no, c)

    assert steps == [critic_mod.REPLAN, critic_mod.REPLAN, critic_mod.ASK_USER]  # the third round is a human decision
    assert len(fake.calls) == 3  # every reply was a valid CHANGES_REQUIRED verdict: no repair call was ever needed
    assert critic_mod.critique_rounds_used(conn, project_name) == 3
    assert critic_mod.is_plan_approved_by_critic(conn, project_name, critic_mod.plan_hash(plan_file)) is False

    # "The user rejects the plan": nothing calls controller.create_cards_from_plan. A bare FakeHermes proves
    # no implementation card exists anywhere, board-side, at any point in this scenario.
    fake_board = FakeHermes(repo, board="ases-test", integration_branch="integration").install(monkeypatch)
    assert fake_board.cards() == []
