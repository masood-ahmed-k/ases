"""Round 9, package MERGEPK (ASES-ARC-03 / ASES-GIT-05): two projects that share ONE ASES database and reuse the
SAME task key ('T1') get separate merge_records rows, proven through a real controller.process_merge_queue pass
on ases.fakes.board.FakeHermes, not a raw SQL check. Before db.py's migration 8, merge_records.task_key was the
table's only primary key, so the second project's candidate build would upsert onto the SAME physical row as the
first project's, silently overwriting its candidate_sha, gate3_result, squash_commit, reverted and completed_at --
exactly the register's own words for this: "a separate, still-open risk for two projects reusing a task key"
(ASES-GIT-05). Migration 8 rebuilds the table with PRIMARY KEY (project, task_key), so the two rows are distinct.

Why a second `world_factory()` call cannot build this: `make_world` creates its OWN temp repository (`tmp_path /
"primary"`) and its OWN database file (`tmp_path / "ases.db"`) per call, all under the ONE `tmp_path` a test's
`world_factory` fixture is bound to, so a second call's `repo.mkdir()` collides with the first world's directory
before a database file even enters the picture. Even giving the second call a different `tmp_path` would not
share ONE database: `fake.install(monkeypatch)` replaces the `hermes` module's public functions in place (a
module-level monkeypatch, not per-instance), so a second FakeHermes.install() call would silently redirect the
FIRST world's board operations to the second board underneath it. Building this scenario genuinely needs either
a `conftest.py` change (out of this package's owned files) or, as done here, a second hand-built
`ases.config.ProjectConfig` and `ases.plan.Plan` driven through the FIRST world's own repository, database and
FakeHermes instance -- one shared board, one shared repo, one shared connection, exactly ASES-GIT-05's registered
risk.

The second project's task uses role 'tester', not 'coder': branch naming
(`controller.create_cards_from_plan`'s `branch=f"swarm/{key}-{task.role}"`) has no project scoping at all, so two
projects reusing task key 'T1' under the SAME role would ALSO collide on the git branch name 'swarm/T1-coder' --
a real, separate gap this package does not own (branch naming is not a merge_records read/write site) and
reports rather than works around in production code. A different role sidesteps it for this test only; the two
ASES-level identifiers the actual bug collides on -- plan.project and the task key -- are identical across both
projects, exactly as the risk describes.
"""
from __future__ import annotations

from ases import config as config_mod
from ases import controller
from ases import finalgates
from ases import guards
from ases import plan as plan_mod

POLL_SECONDS = 20


def test_two_projects_sharing_one_task_key_keep_separate_merge_records(world_factory, one_task_plan):
    # Project 1: the ordinary acceptance rig, unmodified -- project "acceptance", task T1, role coder, touches
    # a.py. Merged for real through world.run_until (create_cards_from_plan, run_pass, process_merge_queue).
    world = world_factory(plan_raw=one_task_plan)
    world.create_cards()
    world.run_until(lambda w: w.all_merge_cards_done())

    project1_row = world.conn.execute(
        "SELECT project, candidate_sha, gate3_result, squash_commit, reverted, completed_at "
        "FROM merge_records WHERE task_key = 'T1' AND project = 'acceptance'"
    ).fetchone()
    assert project1_row is not None
    assert project1_row["gate3_result"] == "pass" and not project1_row["reverted"] and project1_row["completed_at"]
    project1_squash = project1_row["squash_commit"]
    assert project1_squash  # a real squash commit, not the no-op shape

    # Project 2: a second, hand-built project on the SAME board, repository and database, same task key 'T1'.
    plan2_raw = {
        "project": "acceptance2",
        "integration_branch": "integration",
        "gate_profiles": {"trivial": ["echo ok"]},
        "tasks": [
            {"key": "T1", "title": "add z", "role": "tester", "depends_on": [], "touches": ["z.py"],
             "acceptance": ["z.py exists"], "gate_profile": "trivial", "estimated_requests": 5},
        ],
    }
    plan2 = plan_mod.parse_and_validate(
        plan2_raw, known_roles={"lead", "coder", "reviewer", "tester"}, max_cards=40,
    )
    project2 = config_mod.ProjectConfig(
        name=plan2.project, environment="native", data_class="public", workspace_root=world.tmp_path / "ws2",
        ases_home=world.tmp_path / "home2", board=world.board, integration_branch="integration",
        # 'tester' reuses the already-registered 'coder-1' persona (fw.touches_coder(), a generic file-writer):
        # no NEW worker needs registering, only a role name that does not collide with project 1's branch.
        roles={"lead": "lead", "coder": "coder-1", "reviewer": "reviewer", "tester": "coder-1"},
        concurrency={"max_in_progress": 3, "per_profile": 1, "hard_max": 6},
        budgets=dict(world.project.budgets), hermes_tested_version=world.project.hermes_tested_version,
        hermes_native_home=world.tmp_path / "hermes2",
    )
    guards.adopt_current_head(world.conn, plan2.project, world.repo)  # the baseline swarm run adopts before pass 1
    controller.create_cards_from_plan(world.board, "p_acceptance2", world.repo, plan2, project2, conn=world.conn)

    merge_card_id = world.conn.execute(
        "SELECT merge_card_id FROM plan_tasks WHERE project = ? AND task_key = 'T1'", (plan2.project,),
    ).fetchone()["merge_card_id"]

    for _ in range(40):
        controller.run_pass(world.board, world.repo, plan2, project2, world.models_config, conn=world.conn,
                            now=world.fake.now)
        if world.fake.card(merge_card_id)["status"] == "done":
            break
        world.fake.tick(POLL_SECONDS)
    else:
        raise AssertionError(f"project 2's merge card never reached done\n{world.fake.describe()}")

    # The actual claim this test exists to prove: BOTH projects' rows for task_key 'T1' survived, distinct and
    # each carrying its OWN squash commit -- before migration 8 the second project's candidate build would have
    # upserted onto project 1's row (task_key was the whole primary key), overwriting project1_squash entirely.
    rows = {
        r["project"]: dict(r) for r in world.conn.execute(
            "SELECT project, candidate_sha, gate3_result, squash_commit, reverted, completed_at "
            "FROM merge_records WHERE task_key = 'T1'"
        )
    }
    assert set(rows) == {"acceptance", "acceptance2"}
    assert rows["acceptance"]["squash_commit"] == project1_squash  # untouched by project 2's merge
    assert rows["acceptance"]["gate3_result"] == "pass" and not rows["acceptance"]["reverted"]
    assert rows["acceptance2"]["gate3_result"] == "pass" and not rows["acceptance2"]["reverted"]
    assert rows["acceptance2"]["squash_commit"] and rows["acceptance2"]["squash_commit"] != project1_squash
    assert (world.repo / "z.py").exists() and (world.repo / "a.py").exists()  # both projects' work really landed

    # Readers scoped by project (report.py, finalgates.py, reconcile.py, evalkit/codetasks.py) see only their own
    # project's row for this task key, never the other project's.
    summary1 = finalgates._task_summaries(world.conn, world.plan)
    assert next(t for t in summary1 if t["task_key"] == "T1")["squash_commit"] == project1_squash
