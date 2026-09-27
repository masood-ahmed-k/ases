import dataclasses
import random

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


# ---------------------------------------------------------------------------------------------
# ASES-GIT-08: tasks with overlapping touches and no dependency path are serialized by Gate 0.
# ---------------------------------------------------------------------------------------------

def _task(key, touches=(), depends_on=()):
    return plan_mod.PlanTask(
        key=key, title=key, role="coder", depends_on=tuple(depends_on), touches=tuple(touches),
        acceptance=("done",), gate_profile="trivial", estimated_requests=1,
    )


def _raw_task(key, touches, depends_on=()):
    return {"key": key, "title": key, "role": "coder", "depends_on": list(depends_on),
            "touches": list(touches), "acceptance": ["done"], "gate_profile": "trivial",
            "estimated_requests": 1}


def _raw_plan(*raw_tasks):
    return {**VALID, "tasks": list(raw_tasks)}


def _deps(tasks):
    return {t.key: t.depends_on for t in tasks}


def _pairs(links):
    return [(link.later, link.earlier) for link in links]


@pytest.mark.parametrize("a, b, expected", [
    pytest.param(("src/*",), ("src/a.py",), True, id="glob-and-a-path-it-matches"),
    pytest.param(("*.py",), ("src/x.py",), True, id="empty-prefix-same-suffix"),
    pytest.param(("src/a/*",), ("src/*",), True, id="nested-dirs"),
    pytest.param(("src/*",), ("*.py",), True, id="each-glob-fixes-one-end"),
    pytest.param(("src/a*.py",), ("src/*b.py",), True, id="neither-matches-the-other-as-a-path"),
    pytest.param(("src/a?c*",), ("src/ab*",), True, id="prefix-stops-at-the-first-wildcard"),
    pytest.param(("src/*/a.py",), ("src/b/*.py",), True, id="wildcard-in-the-middle"),
    pytest.param(("src/a.py",), ("src/a.py",), True, id="identical-literal"),
    pytest.param(("src/*.py",), ("src/*.py",), True, id="identical-glob"),
    pytest.param(("src/[ab].py",), ("src/[ab].py",), True, id="identical-class-glob"),
    pytest.param(("src/a[bc]",), ("src/a*c",), True, id="class-at-the-end-is-not-a-literal-suffix"),
    pytest.param(("./src/a.py",), ("src/a.py",), True, id="leading-dot-slash-is-ignored"),
    pytest.param(("src\\a.py",), ("src/a.py",), True, id="backslashes-are-slashes"),
    pytest.param(("docs/",), ("docs/*",), True, id="plain-dir-is-a-literal-a-glob-can-match"),
    pytest.param(("a.py", "src/*"), ("b.py", "src/x.py"), True, id="any-glob-pair-counts"),
    pytest.param(("src/*.py",), ("src/*.md",), False, id="same-dir-different-extension"),
    pytest.param(("a.py",), ("b.py",), False, id="different-literals"),
    pytest.param(("src/*",), ("tests/*",), False, id="disjoint-dirs"),
    pytest.param(("src/a.py",), ("tests/*",), False, id="literal-and-glob-in-another-dir"),
    pytest.param(("src/a/*",), ("src/b/*",), False, id="sibling-dirs"),
    pytest.param(("src/*/a.py",), ("src/*/b.py",), False, id="same-prefix-different-endings"),
    pytest.param(("*.py",), ("*.md",), False, id="empty-prefixes-different-extensions"),
    pytest.param(("docs/",), ("docs/a.md",), False, id="plain-dir-is-a-literal-not-a-subtree"),
    pytest.param(("SRC/*",), ("src/a.py",), False, id="case-sensitive"),
    pytest.param((), ("src/*",), False, id="empty-touches-vs-glob"),
    pytest.param((), (), False, id="empty-touches-vs-empty-touches"),
    pytest.param(("a.py", "src/*.py"), ("b.py", "src/*.md"), False, id="no-glob-pair-overlaps"),
])
def test_touches_overlap_table(a, b, expected):
    assert plan_mod.touches_overlap(a, b) is expected
    assert plan_mod.touches_overlap(b, a) is expected  # overlap is symmetric


def test_independent_overlapping_tasks_are_serialized_in_plan_order():
    tasks, links = plan_mod.serialize_overlapping_tasks([_task("T1", ["src/*"]), _task("T2", ["src/a.py"])])
    assert _deps(tasks) == {"T1": (), "T2": ("T1",)}
    assert len(links) == 1
    assert (links[0].later, links[0].earlier) == ("T2", "T1")
    assert "'src/*'" in links[0].reason and "'src/a.py'" in links[0].reason  # names the globs that collide


def test_priority_is_position_in_the_plan_not_the_key():
    tasks, links = plan_mod.serialize_overlapping_tasks([_task("B", ["src/*"]), _task("A", ["src/*"])])
    assert _deps(tasks) == {"B": (), "A": ("B",)}
    assert _pairs(links) == [("A", "B")]


def test_added_dependency_goes_after_the_existing_ones_and_nothing_else_changes():
    t0, t1 = _task("T0", ["README.md"]), _task("T1", ["src/*"])
    t2 = _task("T2", ["src/a.py"], ["T0"])
    tasks, links = plan_mod.serialize_overlapping_tasks((t0, t1, t2))
    assert isinstance(tasks, tuple) and isinstance(links, tuple)
    assert tasks == (t0, t1, dataclasses.replace(t2, depends_on=("T0", "T1")))
    assert _pairs(links) == [("T2", "T1")]


def test_a_direct_dependency_already_orders_the_pair():
    t1, t2 = _task("T1", ["src/*"]), _task("T2", ["src/a.py"], ["T1"])
    tasks, links = plan_mod.serialize_overlapping_tasks([t1, t2])
    assert tasks == (t1, t2)  # no second, duplicate T1 in T2's depends_on
    assert links == ()


def test_a_transitive_dependency_path_already_orders_the_pair():
    # T4 -> T3 -> T2 -> T1: T1 and T4 overlap, but the chain already runs T1 first.
    t1, t2 = _task("T1", ["src/*"]), _task("T2", [], ["T1"])
    t3, t4 = _task("T3", [], ["T2"]), _task("T4", ["src/a.py"], ["T3"])
    tasks, links = plan_mod.serialize_overlapping_tasks([t1, t2, t3, t4])
    assert tasks == (t1, t2, t3, t4)
    assert links == ()


def test_a_dependency_from_the_earlier_task_to_the_later_one_wins_over_plan_order():
    # T1 is listed first (higher priority) but depends on T2, so T2 has to go first. Linking T2 after T1
    # as well would close a cycle.
    t1, t2 = _task("T1", ["src/*"], ["T2"]), _task("T2", ["src/a.py"])
    tasks, links = plan_mod.serialize_overlapping_tasks([t1, t2])
    assert tasks == (t1, t2)
    assert links == ()


def test_a_transitive_path_from_the_earlier_task_to_the_later_one_wins_over_plan_order():
    t1, t2, t3 = _task("T1", ["src/*"], ["T3"]), _task("T2", ["src/a.py"]), _task("T3", [], ["T2"])
    tasks, links = plan_mod.serialize_overlapping_tasks([t1, t2, t3])
    assert tasks == (t1, t2, t3)
    assert links == ()


def test_links_added_earlier_in_the_pass_count_as_dependency_paths():
    # T1 depends on T2 (listed later) and all three overlap. T3 is linked after T1, and that link plus
    # T1 -> T2 already orders T3 after T2, so T3 gets no second link for T2.
    t1, t2, t3 = _task("T1", ["src/*"], ["T2"]), _task("T2", ["src/*"]), _task("T3", ["src/*"])
    tasks, links = plan_mod.serialize_overlapping_tasks([t1, t2, t3])
    assert _deps(tasks) == {"T1": ("T2",), "T2": (), "T3": ("T1",)}
    assert _pairs(links) == [("T3", "T1")]


def test_disjoint_touches_are_not_serialized():
    given = [_task("T1", ["src/*.py"]), _task("T2", ["docs/*.md"]), _task("T3", ["README.md"])]
    tasks, links = plan_mod.serialize_overlapping_tasks(given)
    assert tasks == tuple(given)
    assert links == ()


def test_tasks_with_empty_touches_are_never_serialized():
    # A reviewer changes no files, so it cannot conflict with anything, even next to a task touching "*".
    given = [_task("T1", ["*"]), _task("T2"), _task("T3")]
    tasks, links = plan_mod.serialize_overlapping_tasks(given)
    assert tasks == tuple(given)
    assert links == ()


def test_three_overlapping_tasks_become_a_chain():
    raw = _raw_plan(_raw_task("T1", ["src/*"]), _raw_task("T2", ["src/a.py"]), _raw_task("T3", ["src/*.py"]))
    p = plan_mod.parse_and_validate(raw, known_roles=ROLES, max_cards=40)
    assert _deps(p.tasks) == {"T1": (), "T2": ("T1",), "T3": ("T1", "T2")}
    assert set(_pairs(p.serialization_links)) == {("T2", "T1"), ("T3", "T1"), ("T3", "T2")}
    assert plan_mod.topological_order(p) == ["T1", "T2", "T3"]


def test_serialization_is_idempotent():
    given = [_task("T1", ["src/*"]), _task("T2", ["src/a.py"]), _task("T3", ["src/*.py"], ["T1"]), _task("T4")]
    once, links = plan_mod.serialize_overlapping_tasks(given)
    assert links  # something was added, so the second pass below is a real test
    again, more_links = plan_mod.serialize_overlapping_tasks(once)
    assert again == once
    assert more_links == ()


_GLOB_POOL = ["src/*", "src/a.py", "src/*.py", "src/*.md", "docs/*", "docs/a.md", "*.py", "README.md", "lib/x/*"]


def _closure(tasks):
    """key -> every key it depends on, directly or not. Computed by repeated widening, not by the search
    plan.py uses, so it can check that search."""
    reach = {t.key: set(t.depends_on) for t in tasks}
    while True:
        grown = False
        for deps in reach.values():
            for dep in list(deps):
                extra = reach[dep] - deps
                if extra:
                    deps |= extra
                    grown = True
        if not grown:
            return reach


def test_serialization_invariants_hold_on_random_plans():
    """Whatever the plan: no cycle appears, every overlapping pair ends up ordered one way or the other,
    existing dependencies keep their order with the new ones appended, links join only overlapping pairs
    the raw plan left unordered (later-listed after earlier-listed), and a second pass adds nothing."""
    rng = random.Random(20260919)
    plans_with_links = 0
    for _ in range(300):
        n = rng.randint(2, 7)
        keys = [f"T{i}" for i in range(1, n + 1)]
        rank = rng.sample(range(n), n)  # a task depends only on lower-ranked ones, so the raw plan is acyclic
        raw = [
            _task(key, rng.sample(_GLOB_POOL, rng.randint(0, 2)),
                  [k for j, k in enumerate(keys) if rank[j] < rank[i] and rng.random() < 0.3])
            for i, key in enumerate(keys)
        ]
        out, links = plan_mod.serialize_overlapping_tasks(raw)
        plans_with_links += bool(links)

        assert [t.key for t in out] == keys
        before, after = _closure(raw), _closure(out)
        assert all(key not in after[key] for key in after)  # still acyclic
        for i, a in enumerate(raw):
            for b in raw[i + 1:]:
                if plan_mod.touches_overlap(a.touches, b.touches):
                    assert b.key in after[a.key] or a.key in after[b.key]
        for link in links:
            earlier, later = raw[keys.index(link.earlier)], raw[keys.index(link.later)]
            assert keys.index(link.earlier) < keys.index(link.later)
            assert plan_mod.touches_overlap(earlier.touches, later.touches)
            assert link.earlier not in before[link.later] and link.later not in before[link.earlier]
        for original, serialized in zip(raw, out):
            added = tuple(link.earlier for link in links if link.later == original.key)
            assert serialized.depends_on == original.depends_on + added
            assert len(set(serialized.depends_on)) == len(serialized.depends_on)
            assert dataclasses.replace(serialized, depends_on=original.depends_on) == original
        assert plan_mod.serialize_overlapping_tasks(out) == (out, ())
    assert plans_with_links > 100  # the loop exercised serialization, not just plans that needed none


def test_parse_and_validate_serializes_overlapping_tasks():
    raw = _raw_plan(_raw_task("T1", ["./src/a.py"]), _raw_task("T2", ["src/*"]))
    p = plan_mod.parse_and_validate(raw, known_roles=ROLES, max_cards=40)
    assert p.task("T2").depends_on == ("T1",)
    assert p.task("T1").depends_on == ()
    assert p.task("T1").touches == ("./src/a.py",)  # stored as written: touches enforcement reads it
    assert _pairs(p.serialization_links) == [("T2", "T1")]
    assert plan_mod.topological_order(p) == ["T1", "T2"]


def test_plan_without_overlap_has_no_serialization_links():
    p = plan_mod.parse_and_validate(VALID, known_roles=ROLES, max_cards=40)
    assert p.serialization_links == ()
    assert p.task("T2").depends_on == ("T1",)  # T2 only has the dependency the Lead wrote


def test_two_tasks_already_ordered_through_a_third_get_no_link():
    # T1 and T3 overlap, but T3 -> T2 -> T1 already orders them, so Gate 0 adds nothing.
    raw = _raw_plan(_raw_task("T1", ["src/*"]), _raw_task("T2", [], ["T1"]), _raw_task("T3", ["src/a.py"], ["T2"]))
    p = plan_mod.parse_and_validate(raw, known_roles=ROLES, max_cards=40)
    assert _deps(p.tasks) == {"T1": (), "T2": ("T1",), "T3": ("T2",)}
    assert p.serialization_links == ()


def test_load_plan_file_serializes_overlapping_tasks(tmp_path):
    import json
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(_raw_plan(_raw_task("T1", ["src/*"]), _raw_task("T2", ["src/a.py"]))), encoding="utf-8")
    parsed = plan_mod.load_plan_file(path, known_roles=ROLES, max_cards=40)
    assert parsed.task("T2").depends_on == ("T1",)
    assert _pairs(parsed.serialization_links) == [("T2", "T1")]


def test_plan_can_still_be_built_without_serialization_links():
    p = plan_mod.Plan(project="t", integration_branch="integration", gate_profiles={"trivial": []},
                      tasks=(_task("T1"),))
    assert p.serialization_links == ()


def test_serialization_link_is_frozen():
    link = plan_mod.SerializationLink(later="T2", earlier="T1", reason="touches overlap")
    with pytest.raises(dataclasses.FrozenInstanceError):
        link.later = "T3"


def test_dangling_dependency_is_still_the_reported_error_when_touches_overlap():
    bad = _raw_plan(_raw_task("T1", ["src/*"]), _raw_task("T2", ["src/a.py"], ["T99"]))
    with pytest.raises(plan_mod.PlanError) as exc_info:
        plan_mod.parse_and_validate(bad, known_roles=ROLES, max_cards=40)
    assert exc_info.value.errors == ["task 'T2' depends_on unknown task 'T99'"]


@pytest.mark.parametrize("bad", [
    pytest.param(_raw_plan(_raw_task("T1", ["src/*"]), _raw_task("T2", ["src/a.py"], ["T99"])),
                 id="dangling-dependency"),
    pytest.param(_raw_plan(_raw_task("T1", ["src/*"], ["T2"]), _raw_task("T2", ["src/a.py"], ["T1"])),
                 id="cycle"),
    pytest.param(_raw_plan(_raw_task("T1", ["src/*"]), {**_raw_task("T2", ["src/a.py"]), "acceptance": []}),
                 id="missing-acceptance"),
])
def test_serialization_is_not_applied_to_a_plan_that_fails_validation(monkeypatch, bad):
    calls = []
    real = plan_mod.serialize_overlapping_tasks
    monkeypatch.setattr(plan_mod, "serialize_overlapping_tasks", lambda tasks: calls.append(tasks) or real(tasks))
    with pytest.raises(plan_mod.PlanError):
        plan_mod.parse_and_validate(bad, known_roles=ROLES, max_cards=40)
    assert calls == []


@pytest.mark.parametrize("touches", [[5], ["src/*", None], [["src/a.py"]]])
def test_touches_entries_must_be_strings(touches):
    """Serialization compares touches as globs, so a non-string is a Gate 0 error, not a crash inside it."""
    bad = _raw_plan({**_raw_task("T1", []), "touches": touches})
    with pytest.raises(plan_mod.PlanError, match="path glob strings"):
        plan_mod.parse_and_validate(bad, known_roles=ROLES, max_cards=40)


@pytest.mark.parametrize("bad", [[], "echo ok", [""], ["  "], [1], None])
def test_a_gate_profile_without_real_commands_fails_gate_0(bad):
    """An empty profile is vacuously green (run_gate over [] passes and records a pass), so it would wave every
    diff through Gate 1 and Gate 3 unchecked."""
    raw = {
        "project": "p", "integration_branch": "integration", "gate_profiles": {"tests": bad},
        "tasks": [{"key": "T1", "title": "t", "role": "coder", "depends_on": [], "touches": ["a.py"],
                   "acceptance": ["x"], "gate_profile": "tests", "estimated_requests": 5}],
    }

    with pytest.raises(plan_mod.PlanError) as info:
        plan_mod.parse_and_validate(raw, known_roles={"coder"}, max_cards=40)

    assert any("gate_profiles.tests" in e for e in info.value.errors)


# ---------------------------------------------------------------------------------------------
# ASES-QG-02 (section 14.3): a touches glob broad enough to also cover gate/CI configuration is rejected at
# Gate 0 unless the task marks allow_gate_config_changes. Found by the MR and FG builders in round 5: a task
# whose touches was a wildcard glob silently exempted gate-config paths too from tamper.gate_config_changed.
# ---------------------------------------------------------------------------------------------


def test_bare_star_touches_covering_gate_config_is_rejected():
    raw = _raw_plan(_raw_task("T1", ["*"]))
    with pytest.raises(plan_mod.PlanError) as info:
        plan_mod.parse_and_validate(raw, known_roles=ROLES, max_cards=40)
    assert any("gate/CI configuration" in e and "T1" in e and "'*'" in e for e in info.value.errors)


def test_bare_double_star_touches_covering_gate_config_is_rejected():
    raw = _raw_plan(_raw_task("T1", ["**"]))
    with pytest.raises(plan_mod.PlanError, match="gate/CI configuration"):
        plan_mod.parse_and_validate(raw, known_roles=ROLES, max_cards=40)


def test_bare_star_touches_is_accepted_with_allow_gate_config_changes():
    raw = _raw_plan({**_raw_task("T1", ["*"]), "allow_gate_config_changes": True})
    p = plan_mod.parse_and_validate(raw, known_roles=ROLES, max_cards=40)
    assert p.task("T1").allow_gate_config_changes is True
    assert p.task("T1").touches == ("*",)


def test_narrow_explicit_touches_on_a_gate_config_file_needs_no_marker():
    raw = _raw_plan(_raw_task("T1", ["pytest.ini"]))
    p = plan_mod.parse_and_validate(raw, known_roles=ROLES, max_cards=40)
    assert p.task("T1").touches == ("pytest.ini",)
    assert p.task("T1").allow_gate_config_changes is False


def test_wildcard_touches_that_does_not_reach_gate_config_is_unaffected():
    raw = _raw_plan(_raw_task("T1", ["docs/*.md"]))
    p = plan_mod.parse_and_validate(raw, known_roles=ROLES, max_cards=40)
    assert p.task("T1").touches == ("docs/*.md",)


def test_src_star_star_touches_does_not_reach_a_root_level_pytest_ini():
    """pytest.ini lives at the repo root in this project, not under src/, so src/** cannot match it: only an
    ACTUAL overlap is rejected, not every broad-looking glob (ASES-QG-02's own boundary example)."""
    raw = _raw_plan(_raw_task("T1", ["src/**"]))
    p = plan_mod.parse_and_validate(raw, known_roles=ROLES, max_cards=40)
    assert p.task("T1").touches == ("src/**",)


def test_only_the_offending_touches_entry_is_named_in_the_error():
    raw = _raw_plan(_raw_task("T1", ["pytest.ini", "docs/*.md", "*"]))
    with pytest.raises(plan_mod.PlanError) as info:
        plan_mod.parse_and_validate(raw, known_roles=ROLES, max_cards=40)
    hits = [e for e in info.value.errors if "gate/CI configuration" in e]
    assert len(hits) == 1
    assert "'*'" in hits[0] and "'pytest.ini'" not in hits[0] and "'docs/*.md'" not in hits[0]


def test_allow_gate_config_changes_must_be_a_bool():
    raw = _raw_plan({**_raw_task("T1", ["*"]), "allow_gate_config_changes": "yes"})
    with pytest.raises(plan_mod.PlanError, match="allow_gate_config_changes must be true or false"):
        plan_mod.parse_and_validate(raw, known_roles=ROLES, max_cards=40)


def test_allow_gate_config_changes_defaults_to_false():
    p = plan_mod.parse_and_validate(VALID, known_roles=ROLES, max_cards=40)
    assert all(t.allow_gate_config_changes is False for t in p.tasks)


def test_existing_plan_with_no_allow_gate_config_changes_field_parses_unchanged():
    """VALID has no allow_gate_config_changes and no gate4_allowlist anywhere: the shape of a plan.json written
    before either field existed. It must still parse exactly as it did before this change."""
    p = plan_mod.parse_and_validate(VALID, known_roles=ROLES, max_cards=40)
    assert len(p.tasks) == 2
    assert p.task("T2").depends_on == ("T1",)
    assert p.gate4_allowlist == ()


# ---------------------------------------------------------------------------------------------
# ASES-TSK-04 (section 18.2): the optional plan-level gate4_allowlist field.
# ---------------------------------------------------------------------------------------------


def test_gate4_allowlist_defaults_to_an_empty_tuple():
    p = plan_mod.parse_and_validate(VALID, known_roles=ROLES, max_cards=40)
    assert p.gate4_allowlist == () and isinstance(p.gate4_allowlist, tuple)


def test_gate4_allowlist_round_trips():
    raw = {**VALID, "gate4_allowlist": ["tests/**", "docs/**"]}
    p = plan_mod.parse_and_validate(raw, known_roles=ROLES, max_cards=40)
    assert p.gate4_allowlist == ("tests/**", "docs/**")


def test_gate4_allowlist_must_be_an_array_of_strings():
    raw = {**VALID, "gate4_allowlist": ["tests/**", 5]}
    with pytest.raises(plan_mod.PlanError, match="gate4_allowlist must be an array"):
        plan_mod.parse_and_validate(raw, known_roles=ROLES, max_cards=40)


def test_gate4_allowlist_wrong_type_is_rejected():
    raw = {**VALID, "gate4_allowlist": "tests/**"}
    with pytest.raises(plan_mod.PlanError, match="gate4_allowlist must be an array"):
        plan_mod.parse_and_validate(raw, known_roles=ROLES, max_cards=40)


# ---------------------------------------------------------------------------------------------
# ASES-QG-05 (round 7, part C): the Tester is an ordinary plan role, exactly like coder and reviewer, once a
# project maps a tester: profile in config/swarm.yaml. Gate 0 needs no special-casing for it -- cli.py already
# builds known_roles=set(project.roles) from that config, so this is a confirming test, not new production
# code (plan.py's role validation was not touched).
# ---------------------------------------------------------------------------------------------


def test_role_tester_validates_cleanly_when_the_project_maps_one():
    raw = _raw_plan({**_raw_task("T1", ["tests/**"]), "role": "tester"})
    p = plan_mod.parse_and_validate(raw, known_roles=ROLES | {"tester"}, max_cards=40)
    assert p.task("T1").role == "tester"


def test_role_tester_is_rejected_like_any_other_unmapped_role():
    raw = _raw_plan({**_raw_task("T1", ["tests/**"]), "role": "tester"})
    with pytest.raises(plan_mod.PlanError, match="unknown role 'tester'"):
        plan_mod.parse_and_validate(raw, known_roles=ROLES, max_cards=40)  # ROLES has no "tester"


# ---------------------------------------------------------------------------------------------
# ASES-GIT-11 (section 8.3, round 7 part B): a greenfield scaffold task, and whether Gate 0's own
# touches-overlap serialization (ASES-GIT-08) alone guarantees "parallel work starts only after the scaffold
# is merged".
# ---------------------------------------------------------------------------------------------


def test_touches_overlap_serialization_alone_does_not_order_a_task_after_the_scaffold():
    """The real gap this round found: a scaffold task's touches names root config/tooling files (pyproject.toml,
    package.json, .gitattributes, AGENTS.md), and an ordinary implementation task's touches names source files
    under src/ -- these do not literally overlap (ASES-GIT-08's own conservative glob comparison, see
    test_touches_overlap_table), so serialize_overlapping_tasks adds NO dependency between them. Without an
    explicit depends_on from the Lead, the scaffold task and the later task are free to run in parallel, which
    is exactly what ASES-GIT-11 ("parallel work starts only after the scaffold is merged") forbids. The
    guarantee rests entirely on the Lead writing an explicit depends_on to the scaffold task's key (see cli.py's
    cmd_plan prompt guidance), never on Gate 0's touches-overlap mechanism alone."""
    scaffold = _task("scaffold", ["pyproject.toml", "package.json", ".gitattributes", "AGENTS.md"])
    feature = _task("T1", ["src/feature_x.py"])  # no depends_on written by the Lead, and no touches overlap

    tasks, links = plan_mod.serialize_overlapping_tasks([scaffold, feature])

    assert _deps(tasks) == {"scaffold": (), "T1": ()}  # NOT ordered: this is the gap
    assert links == ()


def test_explicit_depends_on_the_scaffold_task_is_what_actually_orders_parallel_work_after_it():
    """The mechanism that DOES provide the ASES-GIT-11 guarantee: an explicit depends_on from every other task
    to the scaffold task's key, exactly like any other plan dependency, survives Gate 0 unchanged and puts the
    scaffold strictly before every dependent task in topological_order -- with no help needed from touches
    overlap (serialization_links stays empty)."""
    raw = _raw_plan(
        _raw_task("scaffold", ["pyproject.toml", "package.json", ".gitattributes", "AGENTS.md"]),
        _raw_task("T1", ["src/feature_x.py"], depends_on=["scaffold"]),
        _raw_task("T2", ["src/feature_y.py"], depends_on=["scaffold"]),
    )
    p = plan_mod.parse_and_validate(raw, known_roles=ROLES, max_cards=40)

    assert p.task("T1").depends_on == ("scaffold",)
    assert p.task("T2").depends_on == ("scaffold",)
    order = plan_mod.topological_order(p)
    assert order.index("scaffold") < order.index("T1")
    assert order.index("scaffold") < order.index("T2")
    assert p.serialization_links == ()  # from the explicit depends_on, not from a touches overlap


# ---------------------------------------------------------------------------------------------
# ASES-SEC-05, ASES-SEC-07 (round 9): a task-scoped network exception. A reason is required whenever the
# exception is granted, and plan.sandbox_network_exceptions folds it into the gate_profiles pin.
# ---------------------------------------------------------------------------------------------


def test_sandbox_network_defaults_to_false_and_empty_reason():
    p = plan_mod.parse_and_validate(VALID, known_roles=ROLES, max_cards=40)
    assert all(t.sandbox_network is False and t.sandbox_network_reason == "" for t in p.tasks)


def test_sandbox_network_true_with_a_reason_is_accepted():
    raw = _raw_plan({**_raw_task("T1", ["a.py"]),
                      "sandbox_network": True, "sandbox_network_reason": "installs a package during setup"})
    p = plan_mod.parse_and_validate(raw, known_roles=ROLES, max_cards=40)
    assert p.task("T1").sandbox_network is True
    assert p.task("T1").sandbox_network_reason == "installs a package during setup"


def test_sandbox_network_true_without_a_reason_is_rejected():
    raw = _raw_plan({**_raw_task("T1", ["a.py"]), "sandbox_network": True})
    with pytest.raises(plan_mod.PlanError, match="sandbox_network_reason is empty"):
        plan_mod.parse_and_validate(raw, known_roles=ROLES, max_cards=40)


def test_sandbox_network_true_with_a_blank_reason_is_rejected():
    raw = _raw_plan({**_raw_task("T1", ["a.py"]), "sandbox_network": True, "sandbox_network_reason": "   "})
    with pytest.raises(plan_mod.PlanError, match="sandbox_network_reason is empty"):
        plan_mod.parse_and_validate(raw, known_roles=ROLES, max_cards=40)


def test_sandbox_network_must_be_a_bool():
    raw = _raw_plan({**_raw_task("T1", ["a.py"]), "sandbox_network": "yes"})
    with pytest.raises(plan_mod.PlanError, match="sandbox_network must be true or false"):
        plan_mod.parse_and_validate(raw, known_roles=ROLES, max_cards=40)


def test_sandbox_network_reason_must_be_a_string():
    raw = _raw_plan({**_raw_task("T1", ["a.py"]), "sandbox_network": True, "sandbox_network_reason": 5})
    with pytest.raises(plan_mod.PlanError, match="sandbox_network_reason must be a string"):
        plan_mod.parse_and_validate(raw, known_roles=ROLES, max_cards=40)


def test_sandbox_network_false_needs_no_reason():
    """A reason is only required alongside the exception itself: a task that never asks for network access
    (the default) is not forced to explain why it does not need one."""
    raw = _raw_plan(_raw_task("T1", ["a.py"]))  # neither key given
    p = plan_mod.parse_and_validate(raw, known_roles=ROLES, max_cards=40)
    assert p.task("T1").sandbox_network is False


def test_existing_plan_with_no_sandbox_network_field_parses_unchanged():
    p = plan_mod.parse_and_validate(VALID, known_roles=ROLES, max_cards=40)
    assert all(t.sandbox_network is False and t.sandbox_network_reason == "" for t in p.tasks)


def test_sandbox_network_exceptions_lists_only_tasks_that_carry_one():
    raw = _raw_plan(
        {**_raw_task("T1", ["a.py"]), "sandbox_network": True, "sandbox_network_reason": "needs pypi"},
        _raw_task("T2", ["b.py"], depends_on=["T1"]),
    )
    p = plan_mod.parse_and_validate(raw, known_roles=ROLES, max_cards=40)

    assert plan_mod.sandbox_network_exceptions(p) == {"T1": [True, "needs pypi"]}


def test_sandbox_network_exceptions_empty_when_no_task_has_one():
    p = plan_mod.parse_and_validate(VALID, known_roles=ROLES, max_cards=40)
    assert plan_mod.sandbox_network_exceptions(p) == {}


def test_serialize_overlapping_tasks_preserves_the_sandbox_network_fields():
    """dataclasses.replace inside serialize_overlapping_tasks must not drop a field it does not itself set."""
    t1 = dataclasses.replace(_task("T1", ["a.py"]), sandbox_network=True, sandbox_network_reason="needs pypi")
    t2 = _task("T2", ["a.py"])  # touches overlap with T1, so Gate 0 serializes them

    tasks, links = plan_mod.serialize_overlapping_tasks([t1, t2])

    by_key = {t.key: t for t in tasks}
    assert by_key["T1"].sandbox_network is True
    assert by_key["T1"].sandbox_network_reason == "needs pypi"
    assert by_key["T2"].sandbox_network is False
    assert links  # confirms the overlap was actually serialized, so this is not a vacuous check
