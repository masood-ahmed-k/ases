"""Acceptance 22.13, the kill switch test (blueprint [p423]/[p424]; package AC-B).

"With three cards running, swarm stop must leave no worker process, container or merge step running after 30
seconds, and swarm resume must continue correctly." ASES-REC-06.

Drives the REAL killswitch.stop_all / killswitch.resume_all / reconcile.reconcile and controller.run_pass
against ases.fakes.board.FakeHermes, with real git worktrees and scripted workers, the same rig
test_22_2_end_to_end.py uses. killswitch.stop_all takes hermes calls (pause, kanban_list, kanban_show, reclaim)
that default to the (now faked) hermes module once FakeHermes.install(monkeypatch) has run, so this test never
overrides them; it only overrides the process- and Docker-facing hooks (killer, alive, command_line,
list_containers, stop_container), which default to REAL OS/Docker calls and so must never be left un-faked
here (r6_rules.md's hard constraint: never touch a real process, and never call a real Docker).

Like test_22_5_parallel.py, this builds its own World instead of using tests/acceptance/conftest.py's
world_factory: three independent coder tasks need three DIFFERENT Hermes profiles, and
ases.policy.resolve_assignee(role, roles_map) is a strict one role, one profile lookup that make_world's fixed
roles map ({"coder": "coder-1"}) cannot produce. The two files intentionally repeat this small amount of setup
rather than import from each other.
"""
from __future__ import annotations

import json
import subprocess

from ases import bounds, config, controller, db, guards, killswitch, reconcile as reconcile_mod
from ases import plan as plan_mod
from ases.fakes import worker as fw
from ases.fakes.board import FakeHermes

from tests.acceptance.conftest import World

BOARD = "ases-test"

# Three independent coder tasks, one profile each: no depends_on, no shared profile, so all three can run at
# the same time with the fake's default max_in_progress (3) and max_in_progress_per_profile (1).
THREE_TASK_PLAN = {
    "project": "acceptance-killswitch",
    "integration_branch": "integration",
    "gate_profiles": {"trivial": ["echo ok"]},
    "tasks": [
        {"key": "T1", "title": "write t1", "role": "coder1", "depends_on": [], "touches": ["t1.py"],
         "acceptance": ["t1.py exists"], "gate_profile": "trivial", "estimated_requests": 5},
        {"key": "T2", "title": "write t2", "role": "coder2", "depends_on": [], "touches": ["t2.py"],
         "acceptance": ["t2.py exists"], "gate_profile": "trivial", "estimated_requests": 5},
        {"key": "T3", "title": "write t3", "role": "coder3", "depends_on": [], "touches": ["t3.py"],
         "acceptance": ["t3.py exists"], "gate_profile": "trivial", "estimated_requests": 5},
    ],
}
ROLES = {"lead": "lead", "coder1": "coder-1", "coder2": "coder-2", "coder3": "coder-3", "reviewer": "reviewer"}
BUDGETS = {
    "attempts_per_card": 3, "review_rounds_per_task": 3, "fix_cards_per_task": 2, "replans_per_project": 2,
    "max_cards": 40, "card_runtime_minutes": 45, "daily_reserve_percent": 10, "review_reserve_requests": 20,
}
MODELS_CONFIG = {"providers": {}, "models": []}  # no pinned provider for these role names: irrelevant here

SLEEP_SECONDS = 1000  # long enough that nothing finishes on its own before swarm stop reaches them


def _git(cwd, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert result.returncode == 0, f"git {' '.join(args)} failed in {cwd}: {result.stderr.strip()}"
    return result.stdout.strip()


def _killswitch_world(tmp_path, monkeypatch) -> World:
    """A World for THREE_TASK_PLAN and its three-profile roles map: the same steps
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
    (repo / "docs" / "ases" / "plan.json").write_text(json.dumps(THREE_TASK_PLAN, indent=2), encoding="utf-8")
    plan_sha = controller.publish_plan(repo, "integration")

    plan = plan_mod.parse_and_validate(THREE_TASK_PLAN, known_roles=set(ROLES), max_cards=40)
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
        tmp_path=tmp_path, repo=repo, db_path=db_path, conn=conn, plan_raw=THREE_TASK_PLAN, plan=plan,
        project=project, models_config=json.loads(json.dumps(MODELS_CONFIG)), fake=fake, plan_sha=plan_sha,
        board=BOARD,
    )


class _FakeProcessTable:
    """The three process- and Docker-facing hooks stop_all/reconcile take (killer/terminate_tree, alive/pid_alive,
    command_line/process_command_line), built from the FakeHermes's own live_workers(): every FakeHermes worker
    pid is deliberately far outside any real process id range (ases.fakes.board.FAKE_PID_BASE, 2.1 billion and
    up), a real pid_alive/terminate_tree would just report it as gone unverified, which is not what this
    scenario tests, so this fake reports each one alive, then dead once "killed", entirely in a plain Python
    dict and set: it never inspects, signals or shells out to a real OS process, matching r6_rules.md's hard
    constraint. Command lines are built in the same shape real Hermes worker command lines have (r2_rules.md):
    'hermes_cli.main ... chat -q "work kanban task <card id>"', which is what killswitch's own safety rule
    (a pid is only killed when its command line names the card) checks for."""

    def __init__(self, live: dict[int, str]) -> None:
        self.pid_to_card = dict(live)
        self.alive_pids = set(live)
        self.killed: list[int] = []

    def alive(self, pid) -> bool:
        return pid in self.alive_pids

    def killer(self, pid) -> bool:
        self.alive_pids.discard(pid)
        self.killed.append(pid)
        return True

    def command_line(self, pid) -> str | None:
        card_id = self.pid_to_card.get(pid)
        if card_id is None:
            return None
        return f'python -m hermes_cli.main -p x --cli x chat -q "work kanban task {card_id}"'


def _dispatch_three(world, create_cards):
    """Register three coder profiles, create the cards and run one pass: all three cards start running at once
    (same setup as 22.5's first half, deliberately repeated rather than imported, see the module docstring)."""
    fake = world.fake
    fake.register_worker("coder-1", fw.slow_coder({"t1.py": "x = 1\n"}, "write t1", seconds=SLEEP_SECONDS))
    fake.register_worker("coder-2", fw.slow_coder({"t2.py": "x = 2\n"}, "write t2", seconds=SLEEP_SECONDS))
    fake.register_worker("coder-3", fw.slow_coder({"t3.py": "x = 3\n"}, "write t3", seconds=SLEEP_SECONDS))
    pairs = create_cards(world)
    world.one_pass()
    work_ids = [pairs[key].work_card_id for key in ("T1", "T2", "T3")]
    assert all(world.fake.card(cid)["status"] == "running" for cid in work_ids), {
        cid: world.fake.card(cid)["status"] for cid in work_ids}
    return pairs, work_ids


def test_22_13_stop_reclaims_kills_and_stops_containers_then_resume_continues(
    tmp_path, monkeypatch, create_cards, run_until, git,
):
    """Blueprint 22.13: with three cards running, swarm stop leaves no worker process or container running
    within 30 seconds (ASES-REC-06), the merge queue does no further work while stopped, and swarm resume
    reconciles, resumes Hermes and lets the (now reclaimed) cards finish."""
    world = _killswitch_world(tmp_path, monkeypatch)
    fake = world.fake
    pairs, work_ids = _dispatch_three(world, create_cards)

    live = {entry["pid"]: entry["card_id"] for entry in fake.live_workers()}
    assert set(live.values()) == set(work_ids)  # exactly the three running cards have a live fake worker
    table = _FakeProcessTable({pid: card_id for pid, card_id in live.items()})

    # A real _run (the subprocess helper default_list_containers/default_stop_container shell out through) must
    # never be reached: this test always supplies its own list_containers/stop_container below, so nothing here
    # should ever touch it. default_list_containers/default_stop_container both catch any exception from a
    # broken or missing docker and return an empty/false result (killswitch.py: "never raises when Docker is
    # absent"), so a guard that only raises would be silently swallowed there; this one also records every
    # attempt, which the assertions below check directly.
    reached_run: list[list[str]] = []

    def _run_guard(args, timeout):
        reached_run.append(args)
        raise AssertionError(f"a real subprocess call was attempted: {args}")

    monkeypatch.setattr(killswitch, "_run", _run_guard)

    report = killswitch.stop_all(
        world.board, world.plan, conn=world.conn, killer=table.killer, alive=table.alive,
        command_line=table.command_line, list_containers=lambda card_ids: [], stop_container=lambda name: True,
    )

    assert report.flag_set is True
    assert report.paused is True
    assert sorted(report.reclaimed) == sorted(work_ids)
    assert sorted(entry["pid"] for entry in report.killed) == sorted(live)
    assert report.unverified == []
    assert report.within_deadline is True
    assert table.alive_pids == set()  # "no worker process running" after the stop
    assert reached_run == []  # the guard never fired: stop_all used this test's own container fakes

    # The stop flag is visible through bounds.stop_requested too (it reads the same project_state row killswitch
    # writes, and treats "stopped" as a halt, same as killswitch's own stop_requested).
    assert bounds.stop_requested(world.conn, world.plan.project) is True

    # "no ... merge step running": a further pass does nothing while stopped (run_pass's own contract,
    # r5_contracts.md: stopped/stop_reason are always present; every other key keeps the pass's initial, empty
    # default because run_pass returns before reaching dispatch or the merge queue).
    summary = world.one_pass()
    assert summary["stopped"] is True
    assert summary["stop_reason"]
    assert summary["dispatch"] == {} and summary["merged"] == [] and summary["provisioned"] == []

    # "no ... container running": nothing in this test suite ever starts Docker for real (list_containers and
    # stop_container above are this test's own fakes, never the real default_list_containers/
    # default_stop_container -- reached_run stayed empty through the whole stop_all call, which already proved
    # that). This separately proves those real defaults WOULD have shelled out to docker had stop_all's
    # container step ever fallen back to them: called directly, under the same guard, each one reaches _run
    # (and, by design, swallows the guard's exception and returns an empty/false result rather than raising).
    assert killswitch.default_list_containers(["t_1"]) == []
    assert reached_run and reached_run[-1][:2] == ["docker", "ps"], reached_run
    assert killswitch.default_stop_container("sbx-t_1") is False
    assert reached_run[-1][:2] == ["docker", "stop"], reached_run
    # The guard stays in place for the rest of the test (nothing below calls killswitch._run: reconcile,
    # resume_all and run_until never touch it), and pytest's monkeypatch fixture restores it, and everything
    # else this test patched, at teardown. Undoing it explicitly here would also undo fake.install(monkeypatch)
    # above (monkeypatch.undo() reverts every patch made through this fixture, not just the last one), putting
    # the real hermes module back mid-test.

    # swarm resume: reconcile-on-start finds a clean board (stop_all already reclaimed every card and killed
    # every worker, so nothing is left inconsistent), resumes hermes and clears the flag.
    def do_reconcile():
        return reconcile_mod.reconcile(
            world.board, world.repo, world.plan, conn=world.conn, apply=True,
            alive=table.alive, killer=table.killer, command_line=table.command_line,
        )

    result = killswitch.resume_all(world.board, world.plan, conn=world.conn, reconcile=do_reconcile)

    assert result == {"resumed": True}
    assert bounds.stop_requested(world.conn, world.plan.project) is False
    assert killswitch.stop_requested(world.conn, world.plan.project) is False

    # The reclaimed cards are back to an idle status (not running, not done), so the dispatcher picks them up
    # again from scratch, exactly like a real reclaimed card.
    reclaimed_statuses = {cid: fake.card(cid)["status"] for cid in work_ids}
    assert all(status not in ("running", "done") for status in reclaimed_statuses.values()), reclaimed_statuses

    summaries = run_until(world, lambda w: w.all_merge_cards_done(), max_passes=80, step=SLEEP_SECONDS)
    assert summaries[-1]["finished"] is True
    assert [fake.card(pair.merge_card_id)["status"] for pair in pairs.values()] == ["done"] * 3
    assert [fake.card(pair.work_card_id)["status"] for pair in pairs.values()] == ["done"] * 3
    tree = set(git(world, "ls-tree", "-r", "--name-only", "integration").splitlines())
    assert {"t1.py", "t2.py", "t3.py"} <= tree, tree


def test_22_13_a_pid_that_does_not_answer_alive_is_reported_unverified_not_killed(tmp_path, monkeypatch, create_cards):
    """Safety rule (killswitch.py's module docstring): a pid is terminated only when it is alive, its command
    line names the card, and it is not this process or its parent. A fake worker this test's own alive() says is
    already gone must not be "killed" and must not appear in StopReport.unverified either (it is simply not a
    candidate): this pins down the fake process table above against the real stop_all contract, independently of
    the fuller scenario."""
    world = _killswitch_world(tmp_path, monkeypatch)
    pairs, work_ids = _dispatch_three(world, create_cards)
    live = {entry["pid"]: entry["card_id"] for entry in world.fake.live_workers()}
    table = _FakeProcessTable(live)
    gone_pid = next(iter(live))
    table.alive_pids.discard(gone_pid)  # this one exited on its own before the stop ran

    report = killswitch.stop_all(
        world.board, world.plan, conn=world.conn, killer=table.killer, alive=table.alive,
        command_line=table.command_line, list_containers=lambda card_ids: [], stop_container=lambda name: True,
    )

    killed_pids = {entry["pid"] for entry in report.killed}
    assert gone_pid not in killed_pids
    assert gone_pid not in table.killed
    assert not any(entry["pid"] == gone_pid for entry in report.unverified)
    assert killed_pids == set(live) - {gone_pid}
