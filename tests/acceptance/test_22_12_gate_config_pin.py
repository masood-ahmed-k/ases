"""Acceptance 22.12 continued: the gate-config marker itself is pinned (blueprint p277, ASES-QG-02, round 10 GATEPIN).

spec/requirements.yaml's own note on ASES-QG-02 says round 9's CIPIN fix (only a task's allow_gate_config_changes
marker, never touches on its own, exempts a diff from tamper.py's gate_config_changed finding) leaves acceptance
22.12 never exercising gate_config_changed end to end: none of test_22_12_tampering.py's five attempts touches a
gate-config path at all. This file closes that gap, and covers the round 10 half CIPIN did not: the marker itself
was not pinned at `swarm approve` time, so a plan.json edited after approval to flip it went unnoticed. GATEPIN
closes it (plan.pinned_task_fields, controller.pin_gate_profiles / verify_gate_pin).

Three scenarios, matching the GATEPIN work order exactly:
  1. A task whose touches name a gate-config path, without the marker, is refused at Gate 0 (plan.py) -- no card
     is ever created.
  2. The same task WITH the marker: the real diff touches that gate-config file, and the real controller, tamper
     check and gate run let it through to done -- gate_config_changed exercised end to end and correctly silent.
  3. A plan approved with the marker false (so its touches never reach a gate-config path, and Gate 0 has nothing
     to object to) is caught when re-parsed after plan.json is edited, post-approval, to add both the gate-config
     touches and the marker together (Gate 0 alone would refuse the first without the second, so both must be
     added at once to pass a fresh parse): verify_gate_pin -- the exact function `swarm run`'s pre-flight calls,
     before a single card would be dispatched from the edited plan -- raises GateConfigTamperedError.

conftest.py's world_factory does not itself call pin_gate_profiles / verify_gate_pin (only cli.cmd_approve and
cli.cmd_run do, see test_cli_commands.py's `swarm run` startup tests for that layer); scenario 3 here calls the
real controller.pin_gate_profiles / verify_gate_pin functions directly against the world's own conn and plan,
exactly as those CLI call sites do, rather than going through cli.main.
"""
import pytest

from ases import controller, events
from ases import plan as plan_mod
from ases.fakes import worker as fw

SEED = {
    # A genuine bug, not a literal "assert False": fixing it must not itself look like tamper.py's own
    # unconditional_pass marker (a bare "assert True"/"assert 1"/"assert not False" -- see _UNCONDITIONAL_ROWS
    # in tamper.py), which would trip a DIFFERENT finding than the one this file means to exercise.
    "tests/test_feature.py": 'def test_feature():\n    assert 1 + 1 == 3, "not implemented yet"\n',
}


def _plan(*, touches, allow_gate_config_changes):
    task = {
        "key": "T1", "title": "implement the feature", "role": "coder", "depends_on": [],
        "touches": list(touches), "acceptance": ["tests/test_feature.py passes"],
        "gate_profile": "real", "estimated_requests": 5,
    }
    if allow_gate_config_changes:
        task["allow_gate_config_changes"] = True
    return {
        "project": "acceptance-gate-config-pin",
        "integration_branch": "integration",
        "gate_profiles": {"real": ["python -m pytest -q tests/test_feature.py"]},
        "tasks": [task],
    }


def test_22_12_touches_naming_a_gate_config_path_is_refused_at_gate_0_without_the_marker(world_factory):
    """ASES-QG-02 (section 14.3, plan.py): a task whose touches literally names pytest.ini -- a gate-config path
    (tamper.GATE_CONFIG_PATTERNS) -- fails Gate 0 unless it also carries allow_gate_config_changes. No card is
    ever created: world_factory's own Gate 0 call (plan.parse_and_validate, inside make_world) is what refuses
    it, before a FakeHermes board or repository state a scenario could dispatch to even exists."""
    bad = _plan(touches=["tests/test_feature.py", "pytest.ini"], allow_gate_config_changes=False)

    with pytest.raises(plan_mod.PlanError, match="allow_gate_config_changes"):
        world_factory(plan_raw=bad, seed=SEED)


def test_22_12_the_same_change_with_the_marker_reaches_done_and_never_trips_gate_config_changed(world_factory):
    """The marker this time: the coder's real diff edits pytest.ini (inside its declared touches) alongside
    fixing the feature test. tamper.analyze_diff reads allow_gate_config_changes=True as "an explicit plan task
    that allows it" (ASES-QG-02), so gate_config_changed never fires, Gate 1's real pytest run goes green on the
    now-fixed test, the reviewer's PASS merges it, and the card reaches done -- the exact end-to-end path the
    register noted as never exercised."""
    plan = _plan(touches=["tests/test_feature.py", "pytest.ini"], allow_gate_config_changes=True)
    world = world_factory(plan_raw=plan, seed=SEED)
    world.fake.register_worker("coder-1", fw.ScriptedWorker([
        fw.Modify("tests/test_feature.py", lambda text: text.replace("1 + 1 == 3", "1 + 1 == 2")),
        fw.Append("pytest.ini", "[pytest]\naddopts = -q\n"),
        fw.Commit("fix the feature and tune pytest.ini"),
        fw.RequestReview("done: the feature works and pytest.ini is tuned", None, "reviewer"),
    ]))
    pair = world.create_cards()["T1"]
    work = pair.work_card_id

    world.run_until(lambda w: w.all_merge_cards_done())

    recorded = events.recent(world.conn, limit=500)
    assert "tamper_blocked" not in {e["kind"] for e in recorded}
    gate_run = world.conn.execute(
        "SELECT result FROM gate_runs WHERE task_key = 'T1' ORDER BY id DESC LIMIT 1").fetchone()
    assert gate_run["result"] == "pass"
    assert world.card(work)["status"] == "done"


def test_22_12_a_plan_approved_without_the_marker_is_caught_if_edited_afterward_to_add_it(world_factory):
    """GATEPIN's own scenario: at `swarm approve` time the task's touches do not reach a gate-config path, so
    the marker is false and Gate 0 passes it -- pin_gate_profiles pins that. If docs/ases/plan.json is then
    edited, after approval, to add pytest.ini to the task's touches AND set allow_gate_config_changes (both
    together: Gate 0 alone would refuse the first without the second), the edited plan re-parses cleanly on its
    own -- Gate 0 has nothing to object to. Only the pin, re-checked with the plan's CURRENT pinned_task_fields,
    catches that the approved plan never covered this: verify_gate_pin (the exact function `swarm run`'s
    pre-flight calls before a single card is dispatched) raises GateConfigTamperedError. This is precisely what
    the round 10 work order describes: "The marker is NOT pinned, so a plan.json edited after approval to set it
    goes unnoticed" -- proven here noticed."""
    approved_raw = _plan(touches=["tests/test_feature.py"], allow_gate_config_changes=False)
    world = world_factory(plan_raw=approved_raw, seed=SEED)
    controller.pin_gate_profiles(
        world.conn, world.plan.project, world.plan.gate_profiles, plan_mod.pinned_task_fields(world.plan),
    )
    # Sanity: verifying the plan exactly as approved is not tampering.
    controller.verify_gate_pin(
        world.conn, world.plan.project, world.plan.gate_profiles, plan_mod.pinned_task_fields(world.plan),
    )

    edited_raw = _plan(touches=["tests/test_feature.py", "pytest.ini"], allow_gate_config_changes=True)
    edited = plan_mod.parse_and_validate(
        edited_raw, known_roles=set(world.project.roles), max_cards=world.project.budgets["max_cards"])

    with pytest.raises(controller.GateConfigTamperedError, match="ASES-QG-02"):
        controller.verify_gate_pin(
            world.conn, edited.project, edited.gate_profiles, plan_mod.pinned_task_fields(edited),
        )
