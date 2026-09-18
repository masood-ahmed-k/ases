import pytest

from ases import plan as plan_mod

ROLES = {"lead", "coder", "reviewer"}

VALID = {
    "project": "test",
    "integration_branch": "integration",
    "gate_profiles": {"trivial": ["python -c \"pass\""]},
    "tasks": [
        {"key": "T1", "title": "Scaffold", "role": "coder", "depends_on": [],
         "touches": ["README.md"], "acceptance": ["file exists"], "gate_profile": "trivial",
         "estimated_requests": 20},
        {"key": "T2", "title": "Review scaffold", "role": "reviewer", "depends_on": ["T1"],
         "touches": [], "acceptance": ["reviewed"], "gate_profile": "trivial",
         "estimated_requests": 10},
    ],
}


def test_valid_plan_parses():
    p = plan_mod.parse_and_validate(VALID, known_roles=ROLES, max_cards=40)
    assert len(p.tasks) == 2
    assert p.task("T2").depends_on == ("T1",)


def test_missing_top_level_key():
    bad = {k: v for k, v in VALID.items() if k != "gate_profiles"}
    with pytest.raises(plan_mod.PlanError, match="gate_profiles"):
        plan_mod.parse_and_validate(bad, known_roles=ROLES, max_cards=40)


def test_duplicate_task_key():
    bad = {**VALID, "tasks": [VALID["tasks"][0], {**VALID["tasks"][0]}]}
    with pytest.raises(plan_mod.PlanError, match="duplicate"):
        plan_mod.parse_and_validate(bad, known_roles=ROLES, max_cards=40)


def test_dangling_dependency():
    bad_task = {**VALID["tasks"][1], "depends_on": ["T99"]}
    bad = {**VALID, "tasks": [VALID["tasks"][0], bad_task]}
    with pytest.raises(plan_mod.PlanError, match="unknown task 'T99'"):
        plan_mod.parse_and_validate(bad, known_roles=ROLES, max_cards=40)


def test_dependency_cycle():
    t1 = {**VALID["tasks"][0], "depends_on": ["T2"]}
    t2 = {**VALID["tasks"][1], "depends_on": ["T1"]}
    bad = {**VALID, "tasks": [t1, t2]}
    with pytest.raises(plan_mod.PlanError, match="cycle"):
        plan_mod.parse_and_validate(bad, known_roles=ROLES, max_cards=40)


def test_unknown_role():
    bad_task = {**VALID["tasks"][0], "role": "wizard"}
    bad = {**VALID, "tasks": [bad_task]}
    with pytest.raises(plan_mod.PlanError, match="unknown role"):
        plan_mod.parse_and_validate(bad, known_roles=ROLES, max_cards=40)


def test_missing_acceptance():
    bad_task = {**VALID["tasks"][0], "acceptance": []}
    bad = {**VALID, "tasks": [bad_task]}
    with pytest.raises(plan_mod.PlanError, match="acceptance"):
        plan_mod.parse_and_validate(bad, known_roles=ROLES, max_cards=40)


def test_gate_profile_not_declared():
    bad_task = {**VALID["tasks"][0], "gate_profile": "does-not-exist"}
    bad = {**VALID, "tasks": [bad_task]}
    with pytest.raises(plan_mod.PlanError, match="not declared"):
        plan_mod.parse_and_validate(bad, known_roles=ROLES, max_cards=40)


def test_too_many_cards():
    with pytest.raises(plan_mod.PlanError, match="max_cards"):
        plan_mod.parse_and_validate(VALID, known_roles=ROLES, max_cards=1)


def test_multiple_errors_reported_together():
    bad_task = {**VALID["tasks"][0], "role": "wizard", "acceptance": []}
    bad = {**VALID, "tasks": [bad_task]}
    with pytest.raises(plan_mod.PlanError) as exc_info:
        plan_mod.parse_and_validate(bad, known_roles=ROLES, max_cards=40)
    assert len(exc_info.value.errors) >= 2


def test_topological_order_respects_dependencies():
    p = plan_mod.parse_and_validate(VALID, known_roles=ROLES, max_cards=40)
    order = plan_mod.topological_order(p)
    assert order.index("T1") < order.index("T2")


def test_load_plan_file_missing(tmp_path):
    with pytest.raises(plan_mod.PlanError, match="not found"):
        plan_mod.load_plan_file(tmp_path / "nope.json", known_roles=ROLES, max_cards=40)


def test_load_plan_file_bad_json(tmp_path):
    p = tmp_path / "plan.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(plan_mod.PlanError, match="not valid JSON"):
        plan_mod.load_plan_file(p, known_roles=ROLES, max_cards=40)


def test_load_plan_file_valid(tmp_path):
    import json
    p = tmp_path / "plan.json"
    p.write_text(json.dumps(VALID), encoding="utf-8")
    parsed = plan_mod.load_plan_file(p, known_roles=ROLES, max_cards=40)
    assert parsed.project == "test"
