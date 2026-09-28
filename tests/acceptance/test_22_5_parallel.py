"""Acceptance 22.5, the parallel test (blueprint [p407]/[p408]; package AC-B).

"Three independent cards at once: separate worktrees, branches and profiles, separate port blocks and compose
project names, no cross-worktree changes in the integrity snapshots, and three sequential merges. A fourth card
stays queued because of kanban.max_in_progress." ASES-GIT-01, ASES-GIT-04, ASES-GIT-12, ASES-GIT-14.

Drives the REAL controller (controller.run_pass, guards, leases, mergeq) against ases.fakes.board.FakeHermes,
with real git worktrees and scripted workers, the same rig test_22_2_end_to_end.py uses.

tests/acceptance/conftest.py's world_factory cannot build this scenario's plan: make_world's roles map is fixed
to {"coder": "coder-1"} and ases.policy.resolve_assignee(role, roles_map) is a strict one role, one profile
lookup, so every task of role "coder" would resolve to the SAME single profile no matter how many extra
profiles are registered on the fake board. This module therefore builds its own World the same way
conftest.make_world does (git init, publish the plan, parse it, open the database, install a FakeHermes), with
a roles map of three coder profiles, one per task, and two of the four tasks sharing a role (and so a profile)
on purpose. Nothing here calls a real Hermes, a real model provider, or Docker.
"""
from __future__ import annotations

import json
import subprocess

from ases import config, controller, db, events, guards
from ases import plan as plan_mod
from ases.fakes import worker as fw
from ases.fakes.board import FakeHermes

from tests.acceptance.conftest import World

BOARD = "ases-test"

# Four independent coder tasks (no depends_on, non-overlapping touches, so Gate 0 does not serialize them) across
# three Hermes profiles: T1 and T4 share role "coder1" (profile coder-1), T2 is "coder2" (coder-2), T3 is
# "coder3" (coder-3). max_in_progress_per_profile is 1 by default, so T1 and T4 can never run at the same time.
FOUR_TASK_PLAN = {
    "project": "acceptance-parallel",
    "integration_branch": "integration",
    "gate_profiles": {"trivial": ["echo ok"]},
    "tasks": [
        {"key": "T1", "title": "write t1", "role": "coder1", "depends_on": [], "touches": ["t1.py"],
         "acceptance": ["t1.py exists"], "gate_profile": "trivial", "estimated_requests": 5},
        {"key": "T2", "title": "write t2", "role": "coder2", "depends_on": [], "touches": ["t2.py"],
         "acceptance": ["t2.py exists"], "gate_profile": "trivial", "estimated_requests": 5},
        {"key": "T3", "title": "write t3", "role": "coder3", "depends_on": [], "touches": ["t3.py"],
         "acceptance": ["t3.py exists"], "gate_profile": "trivial", "estimated_requests": 5},
        {"key": "T4", "title": "write t4", "role": "coder1", "depends_on": [], "touches": ["t4.py"],
         "acceptance": ["t4.py exists"], "gate_profile": "trivial", "estimated_requests": 5},
    ],
}
ROLES = {"lead": "lead", "coder1": "coder-1", "coder2": "coder-2", "coder3": "coder-3", "reviewer": "reviewer"}
BUDGETS = {
    "attempts_per_card": 3, "review_rounds_per_task": 3, "fix_cards_per_task": 2, "replans_per_project": 2,
    "max_cards": 40, "card_runtime_minutes": 45, "daily_reserve_percent": 10, "review_reserve_requests": 20,
}
# No pinned model for any of these role names, so policy.profile_provider(task.role, ...) is None for every task
# and the budget gate has nothing to check (controller._affordable_now: "a role with no pinned provider has
# nothing to check and is affordable"): irrelevant to what 22.5 tests, kept out of the way on purpose.
MODELS_CONFIG = {"providers": {}, "models": []}

SLEEP_SECONDS = 60  # fake-clock seconds each coder "works" before committing: long enough to observe it running


def _git(cwd, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert result.returncode == 0, f"git {' '.join(args)} failed in {cwd}: {result.stderr.strip()}"
    return result.stdout.strip()


def _parallel_world(tmp_path, monkeypatch) -> World:
    """A World for FOUR_TASK_PLAN and its three-profile roles map: the same steps
    tests/acceptance/conftest.py's make_world takes for the default plan, with a roles map make_world cannot
    produce (see the module docstring)."""
    repo = tmp_path / "primary"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "integration")
    for key, value in (("user.name", "ASES acceptance"), ("user.email", "acceptance@example.invalid"),
                       ("commit.gpgsign", "false"), ("core.autocrlf", "false")):
        _git(repo, "config", key, value)
    (repo / "README.md").write_text("seeded repository\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")

    (repo / "docs" / "ases").mkdir(parents=True)
    (repo / "docs" / "ases" / "plan.json").write_text(json.dumps(FOUR_TASK_PLAN, indent=2), encoding="utf-8")
    plan_sha = controller.publish_plan(repo, "integration")

    plan = plan_mod.parse_and_validate(FOUR_TASK_PLAN, known_roles=set(ROLES), max_cards=40)
    db_path = tmp_path / "ases.db"
    conn = db.connect(db_path)
    project = config.ProjectConfig(
        name=plan.project, environment="native", data_class="public", workspace_root=tmp_path / "ws",
        ases_home=tmp_path / "home", board=BOARD, integration_branch="integration", roles=dict(ROLES),
        concurrency={"max_in_progress": 3, "per_profile": 1, "hard_max": 6},
        budgets=dict(BUDGETS), hermes_tested_version="0.21.3", hermes_native_home=tmp_path / "hermes",
    )
    fake = FakeHermes(repo, board=BOARD, integration_branch="integration")
    fake.install(monkeypatch)
    fake.register_worker("lead", fw.ScriptedWorker([fw.Complete("nothing for the lead to do")]))
    fake.register_worker("reviewer", fw.reviewer_pass())
    guards.adopt_current_head(conn, plan.project, repo)
    return World(
        tmp_path=tmp_path, repo=repo, db_path=db_path, conn=conn, plan_raw=FOUR_TASK_PLAN, plan=plan,
        project=project, models_config=json.loads(json.dumps(MODELS_CONFIG)), fake=fake, plan_sha=plan_sha,
        board=BOARD,
    )


def _commit_subjects(git, world) -> list[str]:
    return git(world, "log", "--format=%s", "integration").splitlines()


def _refs_moved_only_by_fast_forward(git, world, moves: int) -> None:
    """The newest `moves` reflog entries of the integration branch are the merge queue's fast-forwards, and
    nothing else moved the branch after the plan was published (same check as test_22_2_end_to_end.py)."""
    entries = git(world, "reflog", "show", "integration", "--format=%gs").splitlines()
    assert len(entries) == moves + 2, entries  # the seeded commit, the plan commit, then one fast-forward per merge
    assert all(entry.startswith("merge ") and entry.endswith("Fast-forward") for entry in entries[:moves]), entries


def _env_file(worktree) -> dict[str, str]:
    """Parse a worktree's .env.ases (leases.py: `KEY=value` lines, a value quoted only when it needs to be)."""
    values: dict[str, str] = {}
    for line in (worktree / ".env.ases").read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        values[key] = value.strip().strip("'\"")
    return values


def test_22_5_three_cards_run_in_parallel_a_fourth_is_queued(tmp_path, monkeypatch, create_cards, run_until, git):
    """Blueprint 22.5: three of the four independent cards run at once, each with its own worktree, branch,
    profile, port block and compose project name (ASES-GIT-01, ASES-GIT-14); the fourth is queued because its
    profile (coder-1, shared with the first) is already busy. No cross-worktree changes are seen in the
    integrity snapshots while they run (ASES-GIT-12), and all four eventually land on integration as one
    fast-forward each, one merge at a time (ASES-GIT-04)."""
    world = _parallel_world(tmp_path, monkeypatch)
    fake = world.fake
    fake.register_worker("coder-1", fw.by_task_key({
        "T1": fw.slow_coder({"t1.py": "x = 1\n"}, "write t1", seconds=SLEEP_SECONDS),
        "T4": fw.slow_coder({"t4.py": "x = 4\n"}, "write t4", seconds=SLEEP_SECONDS),
    }))
    fake.register_worker("coder-2", fw.slow_coder({"t2.py": "x = 2\n"}, "write t2", seconds=SLEEP_SECONDS))
    fake.register_worker("coder-3", fw.slow_coder({"t3.py": "x = 3\n"}, "write t3", seconds=SLEEP_SECONDS))

    pairs = create_cards(world)
    assert {pair.task_key for pair in pairs.values()} == {"T1", "T2", "T3", "T4"}
    assert all(fake.card(pair.work_card_id)["status"] == "ready" for pair in pairs.values())  # no depends_on

    # One pass: kanban_dispatch claims up to max_in_progress (3, the fake's default) cards. Exactly three of the
    # four run at once; the fourth stays queued (kanban.max_in_progress: [p408]).
    world.one_pass()
    statuses = {key: fake.card(pairs[key].work_card_id)["status"] for key in ("T1", "T2", "T3", "T4")}
    running_keys = sorted(key for key, status in statuses.items() if status == "running")
    queued_keys = sorted(key for key, status in statuses.items() if status != "running")
    assert len(running_keys) == 3 and len(queued_keys) == 1, statuses
    assert statuses[queued_keys[0]] != "running", statuses

    # Each running card has its OWN worktree, branch and profile (ASES-GIT-01).
    running_cards = {key: fake.card(pairs[key].work_card_id) for key in running_keys}
    worktrees = {key: fake.worktree(pairs[key].work_card_id) for key in running_keys}
    assert all(path is not None and path.is_dir() for path in worktrees.values()), worktrees
    assert len({str(path) for path in worktrees.values()}) == 3, worktrees
    assert all(".worktrees" in path.parts for path in worktrees.values()), worktrees
    branches = {key: running_cards[key]["branch_name"] for key in running_keys}
    assert len(set(branches.values())) == 3, branches
    profiles = {key: running_cards[key]["assignee"] for key in running_keys}
    assert len(set(profiles.values())) == 3, profiles
    assert set(profiles.values()) <= {"coder-1", "coder-2", "coder-3"}

    # leases.py gave each running card its own port block and compose project name (ASES-GIT-14). "best effort
    # by nature" (leases.provision_running_cards): run one more pass if the file has not landed yet.
    for _ in range(3):
        if all((worktrees[key] / ".env.ases").exists() for key in running_keys):
            break
        world.one_pass()
    envs = {key: _env_file(worktrees[key]) for key in running_keys}
    port_bases = {values["ASES_PORT_BASE"] for values in envs.values()}
    compose_names = {values["COMPOSE_PROJECT_NAME"] for values in envs.values()}
    assert len(port_bases) == 3, envs
    assert len(compose_names) == 3, envs

    # No cross-worktree changes: while these three ran (and once they are done), guards.check_idle_worktrees
    # (run every pass by controller.process_idle_worktrees) reported nothing about their worktrees, since they
    # were tracked as running throughout and never looked idle in between (ASES-GIT-12).
    summaries = run_until(world, lambda w: w.all_merge_cards_done(), max_passes=80, step=SLEEP_SECONDS)
    assert summaries[-1]["finished"] is True
    kinds = {row["kind"] for row in events.recent(world.conn, limit=1000)}
    assert "idle_worktree_changed" not in kinds, [
        row for row in events.recent(world.conn, limit=1000) if row["kind"] == "idle_worktree_changed"]

    # Four sequential merges (mergeq's own serialization, controller.process_merge_queue: "Serialized -- one
    # merge_task call at a time"): the integration branch moved by exactly four fast-forwards, in some order
    # (nothing depends on anything else here), and every task's file is on it.
    assert [fake.card(pair.merge_card_id)["status"] for pair in pairs.values()] == ["done"] * 4
    assert [fake.card(pair.work_card_id)["status"] for pair in pairs.values()] == ["done"] * 4
    _refs_moved_only_by_fast_forward(git, world, moves=4)
    tree = set(git(world, "ls-tree", "-r", "--name-only", "integration").splitlines())
    assert {"t1.py", "t2.py", "t3.py", "t4.py"} <= tree, tree
    subjects = _commit_subjects(git, world)
    assert sorted(s for s in subjects if s.startswith(("T1:", "T2:", "T3:", "T4:"))) == [
        "T1: write t1", "T2: write t2", "T3: write t3", "T4: write t4"]
