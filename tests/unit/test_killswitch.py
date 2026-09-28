"""killswitch.py: swarm stop and swarm resume (ASES-REC-06, section 19.6; acceptance test 22.13).

Every outside effect is a fake: no Hermes, no process, no Docker. On Windows os.kill TERMINATES the process it is
pointed at (even with signal 0), so an autouse fixture replaces it, and each real default of the module, with a
guard that records the attempt; a test that reaches one fails at teardown even if the module swallowed the error."""
import ast
import copy
import inspect
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import types

import pytest

from ases import bounds, db, hermes, killswitch, plan as plan_mod

PROJECT = "p1"
PLAN = plan_mod.Plan(project=PROJECT, integration_branch="integration", gate_profiles={}, tasks=())

# The real implementations, kept before the autouse fixture replaces them with guards.
REAL = types.SimpleNamespace(
    pid_alive=killswitch.pid_alive,
    terminate_tree=killswitch.terminate_tree,
    process_command_line=killswitch.process_command_line,
    default_list_containers=killswitch.default_list_containers,
    default_list_profile_containers=killswitch.default_list_profile_containers,
    default_stop_container=killswitch.default_stop_container,
    _run=killswitch._run,
    _Win32=killswitch._Win32,
    _read_proc_cmdline=killswitch._read_proc_cmdline,
)

REACHED: list[str] = []
ON_WINDOWS = pytest.mark.skipif(sys.platform != "win32", reason="the Windows branch of a helper")


def _refuse(name):
    def refused(*args, **kwargs):
        REACHED.append(name)
        raise AssertionError(f"{name} reached the real thing inside a unit test")
    return refused


@pytest.fixture(autouse=True)
def no_real_effects(monkeypatch):
    REACHED.clear()
    for name in ("pid_alive", "terminate_tree", "process_command_line", "default_list_containers",
                 "default_list_profile_containers", "default_stop_container", "_run", "_Win32"):
        monkeypatch.setattr(killswitch, name, _refuse(f"killswitch.{name}"))
    for name in ("pause", "resume", "kanban_list", "kanban_show", "kanban_reclaim"):
        monkeypatch.setattr(hermes, name, _refuse(f"hermes.{name}"))

    def refuse_os_kill(pid, sig):
        REACHED.append("os.kill")
        raise PermissionError("os.kill is blocked in unit tests")

    monkeypatch.setattr(os, "kill", refuse_os_kill)
    yield
    assert REACHED == [], f"a unit test reached a real effect: {REACHED}"


# ---------------------------------------------------------------------------------------------
# The fake world: a Hermes board, a process table, Docker, a clock, and a log of every call
# ---------------------------------------------------------------------------------------------


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


OMIT = object()


def live_run(pid):
    return {"id": 1, "profile": "coder-1", "status": "running", "outcome": None, "summary": None, "error": None,
            "metadata": None, "started_at": 1, "ended_at": None, "worker_pid": pid}


def ended_run(pid):
    return {**live_run(pid), "status": "done", "outcome": "completed", "ended_at": 2}


class World:
    def __init__(self, db_path):
        self.db_path = db_path
        self.log = []
        self.clock = Clock()
        self.cards = {}
        self.cmdlines = {}
        self.live = set()
        self.stubborn = set()  # the killer says yes but the process stays alive
        self.kill_fails = set()
        self.kill_raises = set()
        self.reclaim_fails = set()
        self.pause_error = None
        self.containers = []
        self.profile_containers = []  # what a listing by Hermes profile finds (round 17)
        self.container_stop_fails = set()
        self.container_stop_raises = set()
        self.on_pause = None
        self.on_reclaim = None

    # -- building the scene
    def card(self, card_id, status="running", *, pid=None, branch="swarm/T1-coder", parents=(), runs=None,
             task_pid=None, listed_branch=None):
        if runs is None:
            runs = [live_run(pid)] if pid is not None else []
        self.cards[card_id] = {
            "shown": {"id": card_id, "status": status, "branch_name": branch, "worker_pid": task_pid,
                      "_parents": list(parents), "_children": [], "_runs": runs},
            "listed_branch": branch if listed_branch is None else listed_branch,
        }

    def worker(self, card_id, pid, cmdline=None):
        """`pid` is a live process whose command line is a Hermes worker for `card_id`."""
        self.live.add(pid)
        self.cmdlines[pid] = cmdline if cmdline is not None else (
            f'python -m hermes_cli.main -p coder-1 --cli x chat -q "work kanban task {card_id}"')

    # -- the fakes, one per outside effect of stop_all
    def flag_visible(self):
        """Is the stop flag visible from ANOTHER connection (what the polling loop would see)?"""
        other = sqlite3.connect(str(self.db_path))
        try:
            row = other.execute("SELECT status FROM project_state WHERE project = ?", (PROJECT,)).fetchone()
        finally:
            other.close()
        return row is not None and row[0] == "stopped"

    def pause(self, reason=None):
        self.log.append(("pause", self.flag_visible()))
        if self.on_pause:
            self.on_pause()
        if self.pause_error:
            raise self.pause_error

    def kanban_list(self, board, status=None, assignee=None):
        self.log.append(("list", status))
        listed = []
        for card_id, entry in self.cards.items():
            if entry["shown"]["status"] != status:
                continue
            card = {"id": card_id, "status": status, "worker_pid": entry["shown"]["worker_pid"]}
            if entry["listed_branch"] is not OMIT:
                card["branch_name"] = entry["listed_branch"]
            listed.append(card)
        return listed

    def kanban_show(self, board, card_id):
        self.log.append(("show", card_id))
        if card_id not in self.cards:
            raise hermes.HermesCommandError(["kanban", "show", card_id], 1, "no such card")
        return copy.deepcopy(self.cards[card_id]["shown"])

    def reclaim(self, board, card_id, reason=None):
        """Like Hermes: the card goes back to ready and its live run ends."""
        self.log.append(("reclaim", card_id))
        if self.on_reclaim:
            self.on_reclaim()
        if card_id in self.reclaim_fails:
            raise hermes.HermesCommandError(["kanban", "reclaim", card_id], 1, "task is not running")
        shown = self.cards[card_id]["shown"]
        shown["status"] = "ready"
        for run in shown["_runs"]:
            if run.get("ended_at") is None:
                run["ended_at"] = 99

    def alive(self, pid):
        return pid in self.live

    def command_line(self, pid):
        self.log.append(("cmdline", pid))
        return self.cmdlines.get(pid)

    def killer(self, pid):
        self.log.append(("kill", pid))
        if pid in self.kill_raises:
            raise RuntimeError("taskkill exploded")
        if pid in self.kill_fails:
            return False
        if pid not in self.stubborn:
            self.live.discard(pid)
        return True

    def list_containers(self, card_ids):
        self.log.append(("containers", tuple(sorted(card_ids))))
        return list(self.containers)

    def list_profile_containers(self, profiles):
        self.log.append(("profile_containers", tuple(sorted(profiles))))
        return list(self.profile_containers)

    def stop_container(self, name):
        self.log.append(("stop_container", name))
        if name in self.container_stop_raises:
            raise RuntimeError("docker exploded")
        if name in self.container_stop_fails:
            return False
        if name in self.containers:
            self.containers.remove(name)
        if name in self.profile_containers:
            self.profile_containers.remove(name)
        return True

    def hooks(self):
        return dict(
            pause=self.pause, kanban_list=self.kanban_list, kanban_show=self.kanban_show, reclaim=self.reclaim,
            killer=self.killer, alive=self.alive, command_line=self.command_line,
            list_containers=self.list_containers, stop_container=self.stop_container,
            list_profile_containers=self.list_profile_containers,
            now=self.clock.now, sleep=self.clock.sleep,
        )

    def kinds(self):
        return [entry[0] for entry in self.log]


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "ases.db")


@pytest.fixture
def world(tmp_path, conn):
    return World(tmp_path / "ases.db")


def seed(conn, key, work, merge, project=PROJECT):
    conn.execute(
        "INSERT INTO plan_tasks (project, task_key, work_card_id, merge_card_id, role, created_at) "
        "VALUES (?, ?, ?, ?, 'coder', datetime('now'))",
        (project, key, work, merge),
    )


def run_stop(world, conn, **overrides):
    return killswitch.stop_all("b", PLAN, conn=conn, **{**world.hooks(), **overrides})


def two_running(world, conn):
    seed(conn, "T1", "w1", "m1")
    seed(conn, "T2", "w2", "m2")
    for card_id, pid in (("w1", 4101), ("w2", 4102)):
        world.card(card_id, pid=pid)
        world.worker(card_id, pid)


def state_row(conn, project=PROJECT):
    return conn.execute("SELECT * FROM project_state WHERE project = ?", (project,)).fetchone()


# ---------------------------------------------------------------------------------------------
# The stop flag
# ---------------------------------------------------------------------------------------------


def test_the_flag_is_off_until_request_stop_and_on_after(conn):
    assert killswitch.stop_requested(conn, PROJECT) is False

    killswitch.request_stop(conn, PROJECT, "operator asked")

    assert killswitch.stop_requested(conn, PROJECT) is True
    row = state_row(conn)
    assert (row["status"], row["stop_reason"]) == ("stopped", "operator asked")


def test_request_stop_twice_is_one_row_and_the_latest_reason_wins(conn):
    killswitch.request_stop(conn, PROJECT, "first")
    killswitch.request_stop(conn, PROJECT, "second")

    assert conn.execute("SELECT COUNT(*) AS n FROM project_state").fetchone()["n"] == 1
    assert killswitch.stop_requested(conn, PROJECT) is True
    assert state_row(conn)["stop_reason"] == "second"


def test_request_stop_keeps_started_at_deadline_and_replans_of_an_existing_row(conn):
    conn.execute(
        "INSERT INTO project_state (project, started_at, deadline_at, replans, status, updated_at) "
        "VALUES (?, '2026-09-19T10:00:00+00:00', '2026-09-19T18:00:00+00:00', 2, 'running', 'x')",
        (PROJECT,),
    )

    killswitch.request_stop(conn, PROJECT, "stop")

    row = state_row(conn)
    assert (row["started_at"], row["deadline_at"], row["replans"]) == (
        "2026-09-19T10:00:00+00:00", "2026-09-19T18:00:00+00:00", 2)
    assert row["status"] == "stopped"
    assert row["updated_at"] != "x"


@pytest.mark.parametrize("status", ["planning", "running", "paused", "finished"])
def test_only_the_stopped_status_counts_as_a_stop(conn, status):
    conn.execute("INSERT INTO project_state (project, status, updated_at) VALUES (?, ?, 'x')", (PROJECT, status))

    assert killswitch.stop_requested(conn, PROJECT) is False


def test_the_flag_is_per_project(conn):
    killswitch.request_stop(conn, "p1", "stop")

    assert killswitch.stop_requested(conn, "p1") is True
    assert killswitch.stop_requested(conn, "p2") is False


def test_clear_stop_returns_a_started_project_to_running(conn):
    conn.execute(
        "INSERT INTO project_state (project, started_at, status, stop_reason, updated_at) "
        "VALUES (?, '2026-09-19T10:00:00+00:00', 'stopped', 'why', 'x')", (PROJECT,))

    assert killswitch.clear_stop(conn, PROJECT) is True

    row = state_row(conn)
    assert (row["status"], row["stop_reason"], row["started_at"]) == ("running", None, "2026-09-19T10:00:00+00:00")
    assert row["updated_at"] != "x"
    assert killswitch.stop_requested(conn, PROJECT) is False


def test_clear_stop_returns_a_never_started_project_to_planning(conn):
    killswitch.request_stop(conn, PROJECT, "stop before the run began")

    assert killswitch.clear_stop(conn, PROJECT) is True

    assert state_row(conn)["status"] == "planning"


@pytest.mark.parametrize("status", ["planning", "running", "paused", "finished"])
def test_clear_stop_leaves_every_other_status_alone(conn, status):
    conn.execute("INSERT INTO project_state (project, status, updated_at) VALUES (?, ?, 'x')", (PROJECT, status))

    assert killswitch.clear_stop(conn, PROJECT) is False

    assert state_row(conn)["status"] == status


def test_clear_stop_without_a_row_changes_nothing(conn):
    assert killswitch.clear_stop(conn, PROJECT) is False
    assert state_row(conn) is None


def test_stop_requested_is_deliberately_narrower_than_bounds_stop_requested(conn):
    """Round 6 fix: killswitch.stop_requested and bounds.stop_requested were found to answer two different
    questions under confusingly similar names. This one is kept, under its existing name, for "did the kill
    switch specifically stop this project"; bounds.stop_requested is the general "should new work happen right
    now" (true for stopped OR paused). A project a bound merely paused must read False here even though
    bounds.stop_requested reads True for it: that is the distinction test_cli_commands.py already depends on."""
    bounds.start_project(conn, PROJECT)
    bounds.set_status(conn, PROJECT, "paused", "project wall clock reached")

    assert killswitch.stop_requested(conn, PROJECT) is False
    assert bounds.stop_requested(conn, PROJECT) is True

    killswitch.request_stop(conn, PROJECT, "swarm stop")

    assert killswitch.stop_requested(conn, PROJECT) is True
    assert bounds.stop_requested(conn, PROJECT) is True  # both true once the kill switch itself has stopped it


def test_request_stop_clear_stop_round_trip_can_repeat(conn):
    for _ in range(2):
        killswitch.request_stop(conn, PROJECT, "stop")
        assert killswitch.stop_requested(conn, PROJECT) is True
        assert killswitch.clear_stop(conn, PROJECT) is True
        assert killswitch.stop_requested(conn, PROJECT) is False
        assert killswitch.clear_stop(conn, PROJECT) is False  # nothing left to clear


def test_the_stop_reason_is_stored_ascii_only_capped_and_redacted(conn):
    killswitch.request_stop(conn, PROJECT, "stop \u2014 now sk-abcdefghijklmnopqrst " + "x" * 700)

    reason = state_row(conn)["stop_reason"]

    assert reason.isascii()
    assert len(reason) <= 500
    assert "sk-abcdefghijklmnopqrst" not in reason
    assert "\\u2014" in reason


def test_a_blank_reason_is_stored_as_swarm_stop(conn):
    killswitch.request_stop(conn, PROJECT, "")
    assert state_row(conn)["stop_reason"] == "swarm stop"


# ---------------------------------------------------------------------------------------------
# stop_all: the order, and the scope
# ---------------------------------------------------------------------------------------------


def test_the_stop_runs_flag_then_pause_then_list_then_reclaim_then_kill_then_containers(world, conn, monkeypatch):
    two_running(world, conn)
    world.containers = ["sandbox-w1-1"]
    real_request_stop = killswitch.request_stop

    def logged_request_stop(*args, **kwargs):
        world.log.append(("flag",))
        return real_request_stop(*args, **kwargs)

    monkeypatch.setattr(killswitch, "request_stop", logged_request_stop)

    report = run_stop(world, conn)

    assert world.kinds() == [
        "flag", "pause", "list", "list", "show", "show", "reclaim", "reclaim",
        "cmdline", "kill", "cmdline", "kill", "containers", "stop_container",
    ]
    assert ("pause", True) in world.log  # already visible to another connection when hermes pause ran
    assert report.flag_set is True and report.paused is True
    assert report.reclaimed == ["w1", "w2"]
    assert report.killed == [{"card_id": "w1", "pid": 4101}, {"card_id": "w2", "pid": 4102}]
    assert report.unverified == []
    assert report.containers_stopped == ["sandbox-w1-1"]
    assert report.within_deadline is True
    assert report.notes == []
    assert world.live == set()


def test_the_worker_pids_are_read_before_the_reclaim_ends_the_runs(world, conn):
    """Hermes ends a card's live run when it is reclaimed, so a pid looked up afterwards would find no live run.
    The fake reclaim ends the run exactly like that; the worker must still be found and terminated."""
    seed(conn, "T1", "w1", "m1")
    world.card("w1", pid=4101)
    world.worker("w1", 4101)

    report = run_stop(world, conn)

    assert world.cards["w1"]["shown"]["_runs"][0]["ended_at"] == 99
    assert report.killed == [{"card_id": "w1", "pid": 4101}]


def test_a_failing_pause_is_recorded_and_the_rest_of_the_stop_still_runs(world, conn):
    two_running(world, conn)
    world.pause_error = hermes.HermesCommandError(["pause"], 1, "hermes is busy")

    report = run_stop(world, conn)

    assert report.paused is False
    assert any("hermes pause failed" in note and "hermes is busy" in note for note in report.notes)
    assert report.flag_set is True
    assert report.reclaimed == ["w1", "w2"]
    assert [entry["pid"] for entry in report.killed] == [4101, 4102]


def test_only_cards_of_this_plan_are_reclaimed_and_killed(world, conn):
    seed(conn, "T1", "w1", "m1")
    seed(conn, "T1", "o1", "om1", project="other")  # another plan of ASES on the same board
    world.card("w1", pid=4101)
    world.worker("w1", 4101)
    world.card("o1", pid=4107, branch="swarm/T1-coder")
    world.worker("o1", 4107)
    world.card("x9", pid=4109, branch="swarm/Z1-coder")  # an ASES-looking card no plan of ours owns
    world.worker("x9", 4109)
    world.card("x8", pid=4108, branch="feature/thing")  # a card of the user's own
    world.worker("x8", 4108)

    report = run_stop(world, conn)

    assert report.reclaimed == ["w1"]
    assert report.killed == [{"card_id": "w1", "pid": 4101}]
    assert world.live == {4107, 4108, 4109}
    for other in ("o1", "x9", "x8"):
        assert ("reclaim", other) not in world.log
    assert not any(entry[0] in ("cmdline", "kill") and entry[1] in (4107, 4108, 4109) for entry in world.log)
    assert ("show", "x8") not in world.log  # its listing already shows a branch that is not ASES's
    assert report.unverified == []


def test_a_fix_card_is_stopped_through_its_plan_card_parent(world, conn):
    seed(conn, "T1", "w1", "m1")
    world.card("w1", status="done")
    world.card("f1", pid=4110, branch="swarm/T1-fix1", parents=["w1"])  # created, not yet in plan_tasks
    world.worker("f1", 4110)

    report = run_stop(world, conn)

    assert report.reclaimed == ["f1"]
    assert report.killed == [{"card_id": "f1", "pid": 4110}]
    assert ("containers", ("f1", "m1", "w1")) in world.log  # the fix card's sandboxes are searched for too


def test_a_card_with_a_plan_parent_but_no_ases_branch_is_not_a_fix_card(world, conn):
    seed(conn, "T1", "w1", "m1")
    world.card("u1", pid=4111, branch="hand-made", parents=["w1"], listed_branch=OMIT)
    world.worker("u1", 4111)

    report = run_stop(world, conn)

    assert ("show", "u1") in world.log  # the listing did not say, so it was looked at
    assert report.reclaimed == [] and report.killed == [] and report.unverified == []
    assert world.live == {4111}


def test_a_review_card_with_a_live_reviewer_run_is_stopped_and_an_idle_one_is_not(world, conn):
    seed(conn, "T1", "w1", "m1")
    seed(conn, "T2", "w2", "m2")
    world.card("w1", status="review", runs=[ended_run(4101), live_run(4120)])  # coder done, reviewer running
    world.worker("w1", 4101)
    world.worker("w1", 4120)
    world.card("w2", status="review", runs=[ended_run(4102)])  # waiting for a reviewer
    world.worker("w2", 4102)

    report = run_stop(world, conn)

    assert report.reclaimed == ["w1"]
    assert report.killed == [{"card_id": "w1", "pid": 4120}]
    assert world.live == {4101, 4102}  # the finished coder run's pid and the idle card's pid are never touched
    assert not any(entry[0] in ("cmdline", "kill") and entry[1] in (4101, 4102) for entry in world.log)
    assert ("reclaim", "w2") not in world.log


def test_a_card_that_is_neither_running_nor_in_review_is_left_alone(world, conn):
    seed(conn, "T1", "w1", "m1")
    world.card("w1", status="blocked", pid=4101)
    world.worker("w1", 4101)

    report = run_stop(world, conn)

    assert report.reclaimed == [] and report.killed == []
    assert world.live == {4101}


def test_with_no_cards_recorded_for_the_plan_nothing_is_listed_or_touched(world, conn):
    world.card("w1", pid=4101)
    world.worker("w1", 4101)

    report = run_stop(world, conn)

    assert report.flag_set is True and report.paused is True
    assert world.kinds() == ["pause"]
    assert any("no cards are recorded" in note for note in report.notes)
    assert world.live == {4101}


# ---------------------------------------------------------------------------------------------
# stop_all: the safety rules for killing
# ---------------------------------------------------------------------------------------------


def test_a_process_whose_command_line_lacks_the_card_id_is_not_killed_and_is_reported(world, conn):
    seed(conn, "T1", "w1", "m1")
    world.card("w1", pid=4101)
    world.worker("w1", 4101, cmdline="C:\\Python311\\python.exe -m http.server 8000")

    report = run_stop(world, conn)

    assert report.killed == []
    assert report.unverified == [
        {"card_id": "w1", "pid": 4101, "why": "command line does not contain the card id"}]
    assert world.live == {4101}
    assert ("kill", 4101) not in world.log
    assert report.reclaimed == ["w1"]  # the claim is still released


@pytest.mark.parametrize("how", ["none", "blank", "raises"])
def test_an_unreadable_command_line_means_the_process_is_not_killed(world, conn, how):
    seed(conn, "T1", "w1", "m1")
    world.card("w1", pid=4101)
    world.worker("w1", 4101)
    overrides = {}
    if how == "none":
        world.cmdlines[4101] = None
    elif how == "blank":
        world.cmdlines[4101] = "   "
    else:
        def unreadable(pid):
            raise OSError("access is denied")
        overrides["command_line"] = unreadable

    report = run_stop(world, conn, **overrides)

    assert report.killed == []
    assert report.unverified == [{"card_id": "w1", "pid": 4101, "why": "command line could not be read"}]
    assert world.live == {4101}
    assert ("kill", 4101) not in world.log


@pytest.mark.parametrize("pid", [os.getpid(), os.getppid()])
def test_the_current_process_and_its_parent_are_never_killed_even_when_everything_matches(world, conn, pid):
    if pid < 5:
        pytest.skip("this process has no ordinary parent to test with")
    seed(conn, "T1", "w1", "m1")
    world.card("w1", pid=pid)
    world.worker("w1", pid)  # alive, and its command line names the card

    report = run_stop(world, conn)

    assert report.killed == []
    assert report.unverified == [{"card_id": "w1", "pid": pid, "why": "pid is this process or its parent"}]
    assert ("kill", pid) not in world.log and ("cmdline", pid) not in world.log
    assert world.live == {pid}


def test_a_card_id_that_is_a_prefix_of_another_id_does_not_match(world, conn):
    seed(conn, "T1", "t_1", "m1")
    world.card("t_1", pid=4101)
    world.worker("t_1", 4101, cmdline='python -m hermes_cli.main chat -q "work kanban task t_12"')

    report = run_stop(world, conn)

    assert report.killed == []
    assert report.unverified[0]["why"] == "command line does not contain the card id"
    assert world.live == {4101}


def test_a_card_id_followed_by_punctuation_is_found(world, conn):
    seed(conn, "T1", "t_1", "m1")
    world.card("t_1", pid=4101)
    world.worker("t_1", 4101, cmdline='python -m hermes_cli.main chat -q "work kanban task t_1"')

    report = run_stop(world, conn)

    assert report.killed == [{"card_id": "t_1", "pid": 4101}]


@pytest.mark.parametrize("text, card_id, expected", [
    ('chat -q "work kanban task t_1"', "t_1", True),
    ("work kanban task t_12", "t_1", False),
    ("xt_1", "t_1", False),
    ("--task=t_1;", "t_1", True),
    ("C:\\repo\\.worktrees\\t_1\\src", "t_1", True),
    ("t_1", "t_1", True),
    ("", "t_1", False),
    (None, "t_1", False),
    ("anything", "", False),
    ("a.b (c)", "a.b", True),  # regex characters in an id are literal
    ("axb", "a.b", False),
])
def test_a_card_id_matches_only_when_it_stands_alone(text, card_id, expected):
    assert killswitch._mentions(text, card_id) is expected


def test_a_worker_that_has_already_exited_is_noted_not_killed_and_not_unverified(world, conn):
    seed(conn, "T1", "w1", "m1")
    world.card("w1", pid=4101)  # nothing is alive at that pid

    report = run_stop(world, conn)

    assert report.killed == [] and report.unverified == []
    assert any("4101" in note and "already exited" in note for note in report.notes)
    assert ("kill", 4101) not in world.log and ("cmdline", 4101) not in world.log
    assert report.reclaimed == ["w1"]


@pytest.mark.parametrize("bad", [0, 4, 1, -7, None, "", "abc", True, 4.5, 2 ** 40])
def test_a_value_that_is_not_a_worker_pid_is_ignored(world, conn, bad):
    seed(conn, "T1", "w1", "m1")
    world.card("w1", runs=[live_run(bad)], task_pid=bad)

    report = run_stop(world, conn)

    assert report.killed == [] and report.unverified == []
    assert any("no live worker pid is recorded" in note for note in report.notes)
    assert report.reclaimed == ["w1"]


@pytest.mark.parametrize("pid, accepted", [(5, True), (4, False), (2 ** 32 - 1, True), (2 ** 32, False)])
def test_the_range_of_pids_that_can_be_a_worker(world, conn, pid, accepted):
    seed(conn, "T1", "w1", "m1")
    world.card("w1", pid=pid)
    world.worker("w1", pid)

    report = run_stop(world, conn)

    if accepted:
        assert report.killed == [{"card_id": "w1", "pid": pid}]
    else:
        assert report.killed == [] and world.live == {pid}
        assert any("no live worker pid is recorded" in note for note in report.notes)


def test_a_review_card_that_cannot_be_read_is_skipped_with_a_note(world, conn):
    seed(conn, "T1", "w1", "m1")
    world.card("w1", status="review", runs=[live_run(4120)])
    world.worker("w1", 4120)

    def failing_show(board, card_id):
        raise hermes.HermesCommandError(["kanban", "show", card_id], 1, "database is locked")

    report = run_stop(world, conn, kanban_show=failing_show)

    assert report.reclaimed == [] and report.killed == []  # whether it has a live run is unknown
    assert any("could not read card w1" in note and "database is locked" in note for note in report.notes)
    assert world.live == {4120}


def test_a_pid_that_hermes_sends_as_a_digit_string_is_accepted(world, conn):
    seed(conn, "T1", "w1", "m1")
    world.card("w1", runs=[live_run("4101")])
    world.worker("w1", 4101)

    report = run_stop(world, conn)

    assert report.killed == [{"card_id": "w1", "pid": 4101}]


def test_the_cards_own_worker_pid_is_used_when_no_run_carries_one(world, conn):
    seed(conn, "T1", "w1", "m1")
    world.card("w1", runs=[live_run(None)], task_pid=4101)
    world.worker("w1", 4101)

    report = run_stop(world, conn)

    assert report.killed == [{"card_id": "w1", "pid": 4101}]


def test_a_failed_show_falls_back_to_the_pid_in_the_listing(world, conn):
    seed(conn, "T1", "w1", "m1")
    world.card("w1", task_pid=4101)
    world.worker("w1", 4101)

    def failing_show(board, card_id):
        raise hermes.HermesCommandError(["kanban", "show", card_id], 1, "database is locked")

    report = run_stop(world, conn, kanban_show=failing_show)

    assert any("could not read card w1" in note and "database is locked" in note for note in report.notes)
    assert report.killed == [{"card_id": "w1", "pid": 4101}]


def test_every_live_run_of_a_card_is_a_candidate_and_a_pid_is_only_handled_once(world, conn):
    seed(conn, "T1", "w1", "m1")
    world.card("w1", runs=[live_run(4101), live_run(4102), ended_run(4103)], task_pid=4101)
    for pid in (4101, 4102, 4103):
        world.worker("w1", pid)

    report = run_stop(world, conn)

    assert report.killed == [{"card_id": "w1", "pid": 4101}, {"card_id": "w1", "pid": 4102}]
    assert world.live == {4103}  # the run that had already ended is not a worker to stop
    assert world.kinds().count("kill") == 2


def test_a_reclaim_failure_on_one_card_does_not_stop_the_others_or_spare_the_worker(world, conn):
    two_running(world, conn)
    world.reclaim_fails = {"w1"}

    report = run_stop(world, conn)

    assert report.reclaimed == ["w2"]
    assert len(report.reclaim_errors) == 1
    assert report.reclaim_errors[0]["card_id"] == "w1"
    assert "task is not running" in report.reclaim_errors[0]["error"]
    assert [entry["pid"] for entry in report.killed] == [4101, 4102]


# ---------------------------------------------------------------------------------------------
# stop_all: what happens after the kill
# ---------------------------------------------------------------------------------------------


def test_a_pid_that_is_still_alive_after_the_kill_is_noted_and_the_wait_is_bounded(world, conn):
    seed(conn, "T1", "w1", "m1")
    world.card("w1", pid=4101)
    world.worker("w1", 4101)
    world.stubborn = {4101}

    report = run_stop(world, conn)

    assert report.killed == [{"card_id": "w1", "pid": 4101}]  # the killer said yes
    assert any("4101" in note and "still alive" in note for note in report.notes)
    assert world.clock.t == pytest.approx(killswitch._KILL_SETTLE_SECONDS, abs=0.3)
    assert report.within_deadline is True


def test_a_killer_that_fails_on_a_live_process_is_noted_and_not_listed_as_killed(world, conn):
    seed(conn, "T1", "w1", "m1")
    world.card("w1", pid=4101)
    world.worker("w1", 4101)
    world.kill_fails = {4101}

    report = run_stop(world, conn)

    assert report.killed == []
    assert any("could not terminate worker pid 4101" in note for note in report.notes)
    assert world.live == {4101}


def test_a_killer_that_raises_is_noted_and_the_stop_carries_on(world, conn):
    two_running(world, conn)
    world.kill_raises = {4101}

    report = run_stop(world, conn)

    assert any("could not terminate worker pid 4101" in note and "taskkill exploded" in note
               for note in report.notes)
    assert report.killed == [{"card_id": "w2", "pid": 4102}]


def test_a_killer_that_says_no_for_a_process_that_just_exited_is_not_an_error(world, conn):
    seed(conn, "T1", "w1", "m1")
    world.card("w1", pid=4101)
    world.worker("w1", 4101)

    def killer(pid):  # the process ended by itself between the check and the kill
        world.live.discard(pid)
        return False

    report = run_stop(world, conn, killer=killer)

    assert report.killed == []
    assert any("exited on its own" in note for note in report.notes)
    assert not any("could not terminate" in note for note in report.notes)


def test_an_alive_check_that_raises_counts_as_alive(world, conn):
    seed(conn, "T1", "w1", "m1")
    world.card("w1", pid=4101)
    world.worker("w1", 4101)

    def flaky_alive(pid):
        raise OSError("cannot tell")

    report = run_stop(world, conn, alive=flaky_alive)

    # unsure means alive: the worker was still verified and terminated, and the wait then notes it as alive
    assert report.killed == [{"card_id": "w1", "pid": 4101}]
    assert any("still alive" in note for note in report.notes)


# ---------------------------------------------------------------------------------------------
# stop_all: containers
# ---------------------------------------------------------------------------------------------


def test_the_containers_of_this_plan_are_looked_up_by_card_id_and_stopped(world, conn):
    two_running(world, conn)
    world.containers = ["sbx-w1", "sbx-w2"]

    report = run_stop(world, conn)

    assert ("containers", ("m1", "m2", "w1", "w2")) in world.log
    assert sorted(report.containers_stopped) == ["sbx-w1", "sbx-w2"]
    assert sorted(entry[1] for entry in world.log if entry[0] == "stop_container") == ["sbx-w1", "sbx-w2"]
    assert world.containers == []


def test_a_container_that_will_not_stop_is_recorded_and_the_others_still_stop(world, conn):
    seed(conn, "T1", "w1", "m1")
    world.containers = ["a", "b", "c"]
    world.container_stop_fails = {"b"}
    world.container_stop_raises = {"c"}

    report = run_stop(world, conn)

    assert report.containers_stopped == ["a"]
    assert any("could not stop container b" in note for note in report.notes)
    assert any("could not stop container c" in note and "docker exploded" in note for note in report.notes)


def test_containers_are_stopped_even_when_no_card_is_running(world, conn):
    seed(conn, "T1", "w1", "m1")
    world.card("w1", status="done")
    world.containers = ["sbx-w1"]

    report = run_stop(world, conn)

    assert report.containers_stopped == ["sbx-w1"]


def test_a_container_list_failure_is_a_note_and_nothing_is_stopped(world, conn):
    seed(conn, "T1", "w1", "m1")

    def broken(card_ids):
        raise OSError("docker daemon is down")

    report = run_stop(world, conn, list_containers=broken)

    assert report.containers_stopped == []
    assert any("could not list Docker containers" in note and "daemon is down" in note for note in report.notes)


def test_duplicate_container_names_are_stopped_once(world, conn):
    seed(conn, "T1", "w1", "m1")
    world.containers = ["sbx", "sbx"]

    report = run_stop(world, conn)

    assert report.containers_stopped == ["sbx"]
    assert world.kinds().count("stop_container") == 1


def test_a_real_workers_sandbox_is_found_by_its_hermes_profile_and_stopped(world, conn):
    """Round 17 (ASES-REC-06, p357 "terminate worker process trees and sandboxes"): a real worker's container is
    labelled hermes-task-id="default", never its card id, so the card-id listing finds nothing. The listing by this
    project's Hermes profiles must find it and swarm stop must stop it, even though its card is running."""
    two_running(world, conn)
    world.containers = []  # what the card-id match finds for a real Hermes container: nothing
    world.profile_containers = ["hermes-1a2b3c4d"]

    report = run_stop(world, conn, profiles=["reviewer", "coder-1", "lead", "coder-1"])

    assert ("profile_containers", ("coder-1", "lead", "reviewer")) in world.log
    assert report.containers_stopped == ["hermes-1a2b3c4d"]
    assert world.profile_containers == []


def test_a_container_found_by_card_id_and_by_profile_is_stopped_once(world, conn):
    seed(conn, "T1", "w1", "m1")
    world.containers = ["sbx"]
    world.profile_containers = ["sbx", "hermes-9f"]

    report = run_stop(world, conn, profiles=["coder-1"])

    assert sorted(report.containers_stopped) == ["hermes-9f", "sbx"]
    assert world.kinds().count("stop_container") == 2


def test_without_profiles_the_profile_listing_never_runs(world, conn):
    seed(conn, "T1", "w1", "m1")
    world.profile_containers = ["hermes-1a2b3c4d"]

    report = run_stop(world, conn)
    run_stop(world, conn, profiles=[])

    assert "profile_containers" not in world.kinds()
    assert report.containers_stopped == []


def test_a_profile_listing_failure_is_a_note_and_the_card_id_containers_still_stop(world, conn):
    seed(conn, "T1", "w1", "m1")
    world.containers = ["sbx-w1"]

    def broken(profiles):
        raise OSError("docker daemon is down")

    report = run_stop(world, conn, profiles=["coder-1"], list_profile_containers=broken)

    assert report.containers_stopped == ["sbx-w1"]
    assert any("by Hermes profile" in note and "daemon is down" in note for note in report.notes)


def test_the_profile_listing_keeps_only_the_given_profiles_of_hermes_containers(monkeypatch):
    """default_list_profile_containers reads ases.containers's hermes-agent=1 listing (Docker's own label filter)
    and keeps only this project's profiles: another profile's container, or one with no profile label, is never
    returned, so swarm stop never touches it."""
    from ases import containers

    monkeypatch.setattr(killswitch, "default_list_profile_containers", REAL.default_list_profile_containers)
    monkeypatch.setattr(containers, "default_list_hermes_containers", lambda: [
        ("hermes-aaaa", "coder-1"), ("hermes-bbbb", "someone-elses-profile"), ("hermes-cccc", ""),
        ("hermes-dddd", "reviewer"), ("hermes-aaaa", "coder-1"),
    ])

    assert killswitch.default_list_profile_containers(["coder-1", "reviewer"]) == ["hermes-aaaa", "hermes-dddd"]
    assert killswitch.default_list_profile_containers([]) == []

    def no_docker():
        raise FileNotFoundError("docker")

    monkeypatch.setattr(containers, "default_list_hermes_containers", no_docker)
    assert killswitch.default_list_profile_containers(["coder-1"]) == []


# ---------------------------------------------------------------------------------------------
# stop_all: the deadline
# ---------------------------------------------------------------------------------------------


def test_a_clock_that_jumps_past_the_deadline_skips_the_later_steps_but_the_flag_is_already_set(world, conn):
    two_running(world, conn)
    world.containers = ["sbx-w1"]
    world.on_pause = lambda: setattr(world.clock, "t", 100.0)

    report = run_stop(world, conn)

    assert report.within_deadline is False
    assert report.flag_set is True and killswitch.stop_requested(conn, PROJECT) is True
    assert world.kinds() == ["pause"]  # no list, reclaim, kill or container call was made
    assert report.reclaimed == [] and report.killed == [] and report.containers_stopped == []
    assert world.live == {4101, 4102}
    assert report.seconds >= 100


def test_slow_hermes_calls_cannot_starve_the_kills(world, conn):
    seed(conn, "T1", "w1", "m1")
    seed(conn, "T2", "w2", "m2")
    seed(conn, "T3", "w3", "m3")
    for card_id, pid in (("w1", 4101), ("w2", 4102), ("w3", 4103)):
        world.card(card_id, pid=pid)
        world.worker(card_id, pid)
    world.containers = ["sbx-w3"]
    world.on_reclaim = lambda: setattr(world.clock, "t", world.clock.t + 12)  # each reclaim takes 12 seconds

    report = run_stop(world, conn)  # 30 seconds in all, and Hermes may use two thirds of it (20)

    assert report.reclaimed == ["w1", "w2"]  # the third reclaim found the Hermes budget spent
    assert report.reclaim_errors == [{"card_id": "w3", "error": "the time limit was reached"}]
    assert [entry["pid"] for entry in report.killed] == [4101, 4102, 4103]  # and the kills still ran
    assert report.containers_stopped == ["sbx-w3"]
    assert report.within_deadline is False  # a step was cut short, and the report says so
    assert world.live == set()


def test_pids_not_handled_before_the_deadline_are_listed_as_unverified_and_left_alone(world, conn):
    two_running(world, conn)
    world.on_reclaim = lambda: setattr(world.clock, "t", world.clock.t + 40)

    report = run_stop(world, conn)

    assert report.killed == []
    assert report.unverified == [
        {"card_id": "w1", "pid": 4101, "why": "skipped: the time limit was reached"},
        {"card_id": "w2", "pid": 4102, "why": "skipped: the time limit was reached"},
    ]
    assert world.live == {4101, 4102}
    assert report.within_deadline is False
    assert report.flag_set is True


@pytest.mark.parametrize("seconds_per_reclaim, reclaimed, within", [
    (8, ["w1", "w2", "w3"], True),  # the third reclaim starts at 16 seconds, inside Hermes's 20
    (10.5, ["w1", "w2"], False),  # it would start at 21 seconds: skipped
])
def test_the_hermes_calls_may_use_two_thirds_of_the_deadline(world, conn, seconds_per_reclaim, reclaimed, within):
    for n in (1, 2, 3):
        seed(conn, f"T{n}", f"w{n}", f"m{n}")
        world.card(f"w{n}", pid=4100 + n)
        world.worker(f"w{n}", 4100 + n)
    world.on_reclaim = lambda: setattr(world.clock, "t", world.clock.t + seconds_per_reclaim)

    report = run_stop(world, conn)

    assert report.reclaimed == reclaimed
    assert len(report.killed) == 3  # whatever happened to the reclaims, every worker was terminated
    assert report.within_deadline is within


def test_a_stop_that_finishes_late_is_not_within_the_deadline_even_though_no_step_was_skipped(world, conn):
    seed(conn, "T1", "w1", "m1")
    world.containers = ["sbx"]

    def slow_stop(name):
        result = world.stop_container(name)
        world.clock.t += 100  # the very last step ends far past the deadline, and nothing comes after it
        return result

    report = run_stop(world, conn, stop_container=slow_stop)

    assert report.containers_stopped == ["sbx"]
    assert report.within_deadline is False
    assert report.seconds >= 100


def test_a_zero_deadline_still_sets_the_flag_and_skips_everything_else(world, conn):
    two_running(world, conn)

    report = run_stop(world, conn, deadline_seconds=0)

    assert report.flag_set is True
    assert report.within_deadline is False
    assert world.kinds() == []


def test_a_hung_reclaim_is_abandoned_and_the_kill_still_happens_within_the_deadline(world, conn):
    seed(conn, "T1", "w1", "m1")
    world.card("w1", pid=4101)
    world.worker("w1", 4101)
    release = threading.Event()

    def hung_reclaim(board, card_id, reason=None):
        release.wait(10)

    started = time.monotonic()
    try:
        report = run_stop(world, conn, deadline_seconds=3, reclaim=hung_reclaim, now=time.monotonic,
                          sleep=lambda seconds: None)
    finally:
        release.set()
    elapsed = time.monotonic() - started

    assert elapsed < 6  # it did not wait the ten seconds
    assert report.reclaimed == []
    assert report.reclaim_errors == [{"card_id": "w1", "error": "no answer within 1.0s"}]  # a third of 3
    assert report.killed == [{"card_id": "w1", "pid": 4101}]
    assert report.within_deadline is False


def test_a_hung_listing_is_abandoned_and_the_containers_are_still_stopped(world, conn):
    seed(conn, "T1", "w1", "m1")
    world.containers = ["sbx-w1"]
    release = threading.Event()

    def hung_list(board, status=None, assignee=None):
        release.wait(10)
        return []

    try:
        report = run_stop(world, conn, deadline_seconds=3, kanban_list=hung_list, now=time.monotonic,
                          sleep=lambda seconds: None)
    finally:
        release.set()

    assert sum("no answer within" in note for note in report.notes) == 2  # the running and the review listing
    assert report.containers_stopped == ["sbx-w1"]
    assert report.within_deadline is False


# ---------------------------------------------------------------------------------------------
# stop_all: it never raises, writes only the flag, and resolves its defaults late
# ---------------------------------------------------------------------------------------------


def test_stop_all_never_raises_and_reports_what_failed(world, conn):
    conn.close()  # the flag cannot be written and the plan's cards cannot be read

    def boom(*args, **kwargs):
        raise RuntimeError("everything is on fire")

    report = run_stop(world, conn, pause=boom, kanban_list=boom, list_containers=boom)

    assert isinstance(report, killswitch.StopReport)
    assert report.flag_set is False and report.paused is False
    assert any("could not set the stop flag" in note for note in report.notes)
    assert any("hermes pause failed" in note and "everything is on fire" in note for note in report.notes)
    assert any("reading the plan's cards failed" in note for note in report.notes)


def test_stop_all_survives_a_plan_it_cannot_read(world, conn):
    report = killswitch.stop_all("b", None, conn=conn, **world.hooks())

    assert isinstance(report, killswitch.StopReport)
    assert any("unexpected error" in note for note in report.notes)
    assert world.log == []


def test_the_two_listings_and_the_shows_go_out_together(world, conn):
    """Each Hermes command is a fresh process, so N in a row would cost N startups out of the 30 seconds. A
    sequential implementation would sit at the barrier until it broke, and the stop would report it."""
    two_running(world, conn)
    listings = threading.Barrier(2, timeout=3)
    shows = threading.Barrier(2, timeout=3)

    def kanban_list(board, status=None, assignee=None):
        listings.wait()
        return world.kanban_list(board, status=status)

    def kanban_show(board, card_id):
        shows.wait()
        return world.kanban_show(board, card_id)

    report = run_stop(world, conn, kanban_list=kanban_list, kanban_show=kanban_show)

    assert report.notes == []
    assert [entry["pid"] for entry in report.killed] == [4101, 4102]


def test_no_more_than_the_parallel_limit_of_outside_calls_run_at_once(world, conn, monkeypatch):
    seed(conn, "T1", "w1", "m1")
    world.containers = [f"sbx-{n}" for n in range(7)]
    monkeypatch.setattr(killswitch, "_MAX_PARALLEL", 3)
    lock = threading.Lock()
    running = {"now": 0, "peak": 0}

    def stop_container(name):
        with lock:
            running["now"] += 1
            running["peak"] = max(running["peak"], running["now"])
        time.sleep(0.05)
        with lock:
            running["now"] -= 1
        return True

    report = run_stop(world, conn, stop_container=stop_container)

    assert len(report.containers_stopped) == 7  # every container, in order
    assert report.containers_stopped == [f"sbx-{n}" for n in range(7)]
    assert running["peak"] == 3  # in batches of three, and they did overlap


def test_containers_whose_turn_comes_after_the_budget_are_reported_not_dropped(world, conn, monkeypatch):
    seed(conn, "T1", "w1", "m1")
    world.containers = ["a", "b", "c", "d"]
    monkeypatch.setattr(killswitch, "_MAX_PARALLEL", 2)

    def slow_stop(name):
        time.sleep(0.4)  # the first batch uses up the whole budget (0.3 seconds: a third of 0.9)
        return True

    report = run_stop(world, conn, deadline_seconds=0.9, stop_container=slow_stop, now=time.monotonic,
                      sleep=lambda seconds: None)

    assert report.within_deadline is False
    stopped = set(report.containers_stopped)
    missed = [n for n in report.notes if n.startswith("could not stop container")]
    assert len(missed) >= 2 and not stopped & {"c", "d"}  # the second batch was never started, and says so
    assert any("could not stop container c: the time limit was reached" in n for n in missed)


def test_a_flag_that_does_not_read_back_is_reported_and_the_stop_carries_on(world, conn, monkeypatch):
    two_running(world, conn)
    monkeypatch.setattr(killswitch, "stop_requested", lambda conn, project: False)

    report = run_stop(world, conn)

    assert report.flag_set is False
    assert any("reads back as not set" in note for note in report.notes)
    assert [entry["pid"] for entry in report.killed] == [4101, 4102]


@pytest.mark.parametrize("deadline, expected_limit", [("abc", 30), (None, 30), (-5, 0)])
def test_a_deadline_that_is_not_a_positive_number_falls_back_or_clamps(world, conn, deadline, expected_limit):
    two_running(world, conn)

    report = run_stop(world, conn, deadline_seconds=deadline)

    if expected_limit == 30:
        assert report.within_deadline is True and len(report.killed) == 2  # the default thirty seconds
    else:
        assert report.within_deadline is False and report.killed == []  # a zero deadline skips everything
        assert report.flag_set is True


@pytest.mark.parametrize("answer", [None, "oops", {"id": "w1"}, 7, [None, "w1", 3, ["w1"]]])
def test_a_listing_that_is_not_a_list_of_cards_is_ignored(world, conn, answer):
    two_running(world, conn)

    report = run_stop(world, conn, kanban_list=lambda board, status=None, assignee=None: answer)

    assert report.reclaimed == [] and report.killed == []
    assert world.live == {4101, 4102}


def test_entries_that_are_not_cards_are_skipped_but_the_real_cards_are_still_stopped(world, conn):
    two_running(world, conn)

    def mixed(board, status=None, assignee=None):
        real = world.kanban_list(board, status=status)
        return [None, "junk", 5, {"no": "id"}, *real] if status == "running" else []

    report = run_stop(world, conn, kanban_list=mixed)

    assert [entry["pid"] for entry in report.killed] == [4101, 4102]


def test_parents_that_hermes_sends_as_objects_are_understood(world, conn):
    seed(conn, "T1", "w1", "m1")
    world.card("w1", status="done")
    world.card("f1", pid=4110, branch="swarm/T1-fix1", parents=[{"id": "w1", "title": "T1: scaffold"}])
    world.worker("f1", 4110)

    report = run_stop(world, conn)

    assert report.killed == [{"card_id": "f1", "pid": 4110}]


def test_a_container_list_that_is_not_a_list_is_a_note_not_a_crash(world, conn):
    seed(conn, "T1", "w1", "m1")

    report = run_stop(world, conn, list_containers=lambda card_ids: 5)

    assert report.containers_stopped == []
    assert any("could not read the container list" in note for note in report.notes)


def test_a_bug_in_finding_the_cards_still_lets_the_containers_be_stopped(world, conn, monkeypatch):
    seed(conn, "T1", "w1", "m1")
    world.containers = ["sbx-w1"]

    def buggy(*args, **kwargs):
        raise ValueError("bug in finding the cards")

    monkeypatch.setattr(killswitch, "_find_targets", buggy)

    report = run_stop(world, conn)

    assert any("finding running cards failed unexpectedly" in note for note in report.notes)
    assert ("containers", ("m1", "w1")) in world.log
    assert report.containers_stopped == ["sbx-w1"]


def test_a_step_that_hits_a_bug_does_not_cost_the_steps_after_it(world, conn, monkeypatch):
    two_running(world, conn)
    world.containers = ["sbx-w1"]

    def buggy(*args, **kwargs):
        raise ValueError("bug in the reclaim step")

    monkeypatch.setattr(killswitch, "_reclaim_all", buggy)

    report = run_stop(world, conn)

    assert any("reclaiming cards failed unexpectedly" in note for note in report.notes)
    assert [entry["pid"] for entry in report.killed] == [4101, 4102]
    assert report.containers_stopped == ["sbx-w1"]


def test_stop_all_writes_only_the_stop_flag(world, conn):
    two_running(world, conn)
    world.containers = ["sbx-w1"]
    tables = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]

    def counts():
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}

    before = counts()
    run_stop(world, conn)
    after = counts()

    assert {t for t in tables if before[t] != after[t]} == {"project_state"}
    assert after["events"] == 0


def test_the_hermes_and_module_functions_are_looked_up_when_the_stop_runs(world, conn, monkeypatch):
    seed(conn, "T1", "w1", "m1")
    world.card("w1", pid=4101)
    world.worker("w1", 4101)
    world.containers = ["sbx-w1"]
    for name, fake in (("pause", world.pause), ("kanban_list", world.kanban_list),
                       ("kanban_show", world.kanban_show), ("kanban_reclaim", world.reclaim)):
        monkeypatch.setattr(hermes, name, fake)
    for name, fake in (("terminate_tree", world.killer), ("pid_alive", world.alive),
                       ("process_command_line", world.command_line),
                       ("default_list_containers", world.list_containers),
                       ("default_stop_container", world.stop_container)):
        monkeypatch.setattr(killswitch, name, fake)

    report = killswitch.stop_all("b", PLAN, conn=conn, now=world.clock.now, sleep=world.clock.sleep)

    assert report.paused is True
    assert report.reclaimed == ["w1"]
    assert report.killed == [{"card_id": "w1", "pid": 4101}]
    assert report.containers_stopped == ["sbx-w1"]


def test_the_deadline_defaults_to_the_thirty_seconds_of_the_requirement():
    assert killswitch.DEADLINE_SECONDS == 30
    assert inspect.signature(killswitch.stop_all).parameters["deadline_seconds"].default == 30


def test_a_second_stop_finds_nothing_left_to_do(world, conn):
    two_running(world, conn)
    world.containers = ["sbx-w1"]
    run_stop(world, conn)
    world.log.clear()

    report = run_stop(world, conn)

    assert report.flag_set is True and report.paused is True
    assert report.reclaimed == [] and report.killed == [] and report.containers_stopped == []
    assert report.notes == []
    assert "reclaim" not in world.kinds() and "kill" not in world.kinds()


# ---------------------------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------------------------


def test_the_report_is_json_serialisable_and_has_every_field(world, conn):
    two_running(world, conn)

    report = run_stop(world, conn)
    data = report.to_dict()

    json.dumps(data)
    assert set(data) == {
        "started_at", "finished_at", "seconds", "paused", "reclaimed", "reclaim_errors", "killed", "unverified",
        "containers_stopped", "flag_set", "within_deadline", "notes",
    }
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00", data["started_at"])
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00", data["finished_at"])
    assert data["seconds"] == 0.0  # the fake clock never moved


def test_an_empty_report_has_safe_defaults():
    data = killswitch.StopReport().to_dict()

    assert data["reclaimed"] == [] and data["killed"] == [] and data["notes"] == []
    assert data["paused"] is False and data["flag_set"] is False and data["within_deadline"] is True


def test_every_note_and_error_is_ascii_and_secrets_are_redacted(world, conn):
    seed(conn, "T1", "w1", "m1")
    world.card("w1", pid=4101)
    world.worker("w1", 4101)
    world.pause_error = RuntimeError("pause failed \u2014 token sk-abcdefghijklmnopqrstuv caf\u00e9")
    world.reclaim_fails = {"w1"}

    report = run_stop(world, conn)
    text = json.dumps(report.to_dict(), ensure_ascii=False)

    assert text.isascii()
    assert "sk-abcdefghijklmnopqrstuv" not in text
    note = next(n for n in report.notes if n.startswith("hermes pause failed"))
    assert "\\u2014" in note and "caf\\xe9" in note  # escaped, not dropped


def test_a_command_line_never_reaches_the_report(world, conn):
    seed(conn, "T1", "w1", "m1")
    world.card("w1", pid=4101)
    world.worker("w1", 4101, cmdline="python -m hermes_cli.main --api-key SUPERSECRETVALUE work kanban task w2")

    report = run_stop(world, conn)

    assert report.unverified[0]["why"] == "command line does not contain the card id"
    assert "SUPERSECRETVALUE" not in json.dumps(report.to_dict())


def test_a_note_is_capped():
    stop = killswitch._Stop(killswitch.StopReport(), 30, time.monotonic, time.sleep)

    stop.note("x" * 1000)

    assert stop.report.notes == ["x" * 300]


def test_notes_are_single_lines():
    stop = killswitch._Stop(killswitch.StopReport(), 30, time.monotonic, time.sleep)

    stop.note("first line\nsecond line\r\n   third\tline")

    assert stop.report.notes == ["first line second line third line"]


# ---------------------------------------------------------------------------------------------
# write_stop_report
# ---------------------------------------------------------------------------------------------


def _report(**kwargs):
    fields = dict(started_at="2026-09-19T20:30:45+00:00", finished_at="2026-09-19T20:30:52+00:00", seconds=7.0,
                  paused=True, reclaimed=["w1"], flag_set=True, notes=["a note"])
    return killswitch.StopReport(**{**fields, **kwargs})


def test_the_report_file_is_named_by_the_start_time_and_holds_the_report(tmp_path):
    report = _report()

    path = killswitch.write_stop_report(report, tmp_path / "reports" / "stop")

    assert path == tmp_path / "reports" / "stop" / "stop-20260919T203045Z.json"
    assert json.loads(path.read_text(encoding="utf-8")) == report.to_dict()
    assert path.read_text(encoding="utf-8").endswith("\n")


def test_a_second_report_in_the_same_second_does_not_overwrite_the_first(tmp_path):
    first = killswitch.write_stop_report(_report(notes=["one"]), tmp_path)
    second = killswitch.write_stop_report(_report(notes=["two"]), tmp_path)
    third = killswitch.write_stop_report(_report(notes=["three"]), tmp_path)

    assert [p.name for p in (first, second, third)] == [
        "stop-20260919T203045Z.json", "stop-20260919T203045Z-2.json", "stop-20260919T203045Z-3.json"]
    assert json.loads(first.read_text(encoding="utf-8"))["notes"] == ["one"]
    assert json.loads(second.read_text(encoding="utf-8"))["notes"] == ["two"]


def test_a_start_time_in_another_offset_is_named_in_utc(tmp_path):
    path = killswitch.write_stop_report(_report(started_at="2026-09-19T22:30:45+02:00"), tmp_path)

    assert path.name == "stop-20260919T203045Z.json"


def test_a_start_time_without_an_offset_is_taken_as_utc(tmp_path):
    path = killswitch.write_stop_report(_report(started_at="2026-09-19T20:30:45"), tmp_path)

    assert path.name == "stop-20260919T203045Z.json"


def test_a_start_time_that_does_not_parse_still_gets_a_file_name(tmp_path):
    path = killswitch.write_stop_report(_report(started_at="not a time"), tmp_path)

    assert re.fullmatch(r"stop-\d{8}T\d{6}Z\.json", path.name)


def test_the_report_file_is_utf8(tmp_path):
    path = killswitch.write_stop_report(_report(notes=["caf\u00e9"]), tmp_path)

    assert "caf\u00e9" in path.read_bytes().decode("utf-8")


def test_a_real_stop_report_round_trips_through_the_file(world, conn, tmp_path):
    two_running(world, conn)
    report = run_stop(world, conn)

    path = killswitch.write_stop_report(report, tmp_path / "out")

    assert json.loads(path.read_text(encoding="utf-8")) == report.to_dict()


# ---------------------------------------------------------------------------------------------
# resume_all
# ---------------------------------------------------------------------------------------------


def test_resume_with_a_clean_reconcile_reconciles_then_resumes_then_clears_the_flag(conn):
    killswitch.request_stop(conn, PROJECT, "test")
    order = []

    def reconcile():
        order.append(("reconcile", killswitch.stop_requested(conn, PROJECT)))
        return types.SimpleNamespace(findings=[], blocked=[])

    def resume():
        order.append(("resume", killswitch.stop_requested(conn, PROJECT)))

    result = killswitch.resume_all("b", PLAN, conn=conn, resume=resume, reconcile=reconcile)

    assert result == {"resumed": True}
    assert order == [("reconcile", True), ("resume", True)]  # the flag is the last thing to be cleared
    assert killswitch.stop_requested(conn, PROJECT) is False


def test_resume_with_blocked_findings_keeps_the_flag_and_does_not_resume(conn):
    killswitch.request_stop(conn, PROJECT, "test")
    resumed = []
    blocked = [types.SimpleNamespace(task_key="T1", kind="missing_card", detail="w1 no longer resolves")]

    result = killswitch.resume_all(
        "b", PLAN, conn=conn, resume=lambda: resumed.append(1),
        reconcile=lambda: types.SimpleNamespace(findings=blocked, blocked=blocked))

    assert result["resumed"] is False
    assert "T1 missing_card" in result["reason"] and "stop flag stays set" in result["reason"]
    assert set(result) == {"resumed", "reason"}
    assert resumed == []
    assert killswitch.stop_requested(conn, PROJECT) is True


def test_resume_summarises_many_blocked_findings(conn):
    killswitch.request_stop(conn, PROJECT, "test")
    blocked = [types.SimpleNamespace(task_key=f"T{i}", kind="k", detail="d") for i in range(8)]

    result = killswitch.resume_all("b", PLAN, conn=conn, resume=lambda: None,
                                   reconcile=lambda: types.SimpleNamespace(blocked=blocked))

    assert result["resumed"] is False
    assert "8 item(s)" in result["reason"] and "and 3 more" in result["reason"] and "T4 k" in result["reason"]
    assert "T5" not in result["reason"]


def test_a_blocked_list_of_dicts_also_blocks(conn):
    killswitch.request_stop(conn, PROJECT, "test")

    result = killswitch.resume_all(
        "b", PLAN, conn=conn, resume=lambda: None,
        reconcile=lambda: {"blocked": [{"task_key": "T2", "kind": "open_intent"}]})

    assert result["resumed"] is False and "T2 open_intent" in result["reason"]
    assert killswitch.stop_requested(conn, PROJECT) is True


def test_resume_with_a_raising_reconcile_keeps_the_flag_and_does_not_resume(conn):
    killswitch.request_stop(conn, PROJECT, "test")
    resumed = []

    def reconcile():
        raise RuntimeError("cannot read the board")

    result = killswitch.resume_all("b", PLAN, conn=conn, resume=lambda: resumed.append(1), reconcile=reconcile)

    assert result["resumed"] is False
    assert "reconcile-on-start failed" in result["reason"] and "cannot read the board" in result["reason"]
    assert resumed == []
    assert killswitch.stop_requested(conn, PROJECT) is True


def test_resume_with_a_raising_resume_keeps_the_flag(conn):
    killswitch.request_stop(conn, PROJECT, "test")

    def resume():
        raise hermes.HermesCommandError(["resume"], 1, "gateway not answering")

    result = killswitch.resume_all("b", PLAN, conn=conn, resume=resume, reconcile=lambda: None)

    assert result["resumed"] is False
    assert "hermes resume failed" in result["reason"] and "gateway not answering" in result["reason"]
    assert killswitch.stop_requested(conn, PROJECT) is True


def test_resume_without_a_reconcile_just_resumes_and_clears_the_flag(conn):
    killswitch.request_stop(conn, PROJECT, "test")
    resumed = []

    result = killswitch.resume_all("b", PLAN, conn=conn, resume=lambda: resumed.append(1))

    assert result == {"resumed": True}
    assert resumed == [1]
    assert killswitch.stop_requested(conn, PROJECT) is False


@pytest.mark.parametrize("findings", [
    None, object(), types.SimpleNamespace(blocked=[]), types.SimpleNamespace(blocked=None), {"blocked": []}, {},
])
def test_a_reconcile_that_reports_nothing_blocked_lets_the_resume_through(conn, findings):
    killswitch.request_stop(conn, PROJECT, "test")

    result = killswitch.resume_all("b", PLAN, conn=conn, resume=lambda: None, reconcile=lambda: findings)

    assert result == {"resumed": True}


def test_resume_says_so_when_hermes_resumed_but_the_flag_could_not_be_cleared(conn, monkeypatch):
    def broken(conn, project):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(killswitch, "clear_stop", broken)

    result = killswitch.resume_all("b", PLAN, conn=conn, resume=lambda: None)

    assert result["resumed"] is False
    assert "hermes resumed but the stop flag could not be cleared" in result["reason"]


def test_resume_looks_up_hermes_resume_when_it_runs(conn, monkeypatch):
    called = []
    monkeypatch.setattr(hermes, "resume", lambda: called.append(1))

    result = killswitch.resume_all("b", PLAN, conn=conn)

    assert result == {"resumed": True} and called == [1]


def test_resume_reasons_are_ascii(conn):
    def reconcile():
        raise RuntimeError("bad \u2014 board")

    result = killswitch.resume_all("b", PLAN, conn=conn, reconcile=reconcile)

    assert result["reason"].isascii()


def test_three_running_cards_are_all_stopped_and_resume_continues(world, conn):
    """Acceptance test 22.13: with three cards running, swarm stop leaves no worker process and no container
    running, and swarm resume continues correctly."""
    for n in (1, 2, 3):
        seed(conn, f"T{n}", f"w{n}", f"m{n}")
        world.card(f"w{n}", pid=4100 + n)
        world.worker(f"w{n}", 4100 + n)
    world.containers = ["sbx-w1-web", "sbx-w2-web", "sbx-w3-web"]

    report = run_stop(world, conn)

    assert world.live == set() and world.containers == []
    assert report.within_deadline and report.flag_set and report.paused
    assert len(report.killed) == 3 and len(report.containers_stopped) == 3
    assert killswitch.stop_requested(conn, PROJECT) is True

    resumed = []
    result = killswitch.resume_all("b", PLAN, conn=conn, resume=lambda: resumed.append(1),
                                   reconcile=lambda: types.SimpleNamespace(blocked=[]))

    assert result == {"resumed": True} and resumed == [1]
    assert killswitch.stop_requested(conn, PROJECT) is False
    # the reclaimed cards are back to ready, so the dispatcher can pick them up again
    assert {world.cards[f"w{n}"]["shown"]["status"] for n in (1, 2, 3)} == {"ready"}


# ---------------------------------------------------------------------------------------------
# The real helpers, driven through fakes (never a real process, container or Docker)
# ---------------------------------------------------------------------------------------------


class FakeWin32:
    def __init__(self, handle=1, exit_code=259, last_error=0):
        self.handle, self._exit_code, self._last_error = handle, exit_code, last_error
        self.opened, self.closed = [], []

    def open(self, pid):
        self.opened.append(pid)
        return self.handle

    def last_error(self):
        return self._last_error

    def exit_code(self, handle):
        return self._exit_code

    def close(self, handle):
        self.closed.append(handle)


@pytest.mark.parametrize("exit_code, expected", [(259, True), (0, False), (1, False), (3221225786, False)])
def test_windows_liveness_is_alive_only_while_the_exit_code_is_still_active(exit_code, expected):
    assert killswitch._windows_pid_alive(4242, FakeWin32(exit_code=exit_code)) is expected


@pytest.mark.parametrize("last_error, expected", [(5, True), (87, False), (0, False)])
def test_windows_liveness_treats_access_denied_as_alive_and_other_open_failures_as_gone(last_error, expected):
    api = FakeWin32(handle=0, last_error=last_error)

    assert killswitch._windows_pid_alive(4242, api) is expected
    assert api.closed == []  # there was no handle to close


def test_windows_liveness_says_alive_when_the_exit_code_cannot_be_read_and_closes_the_handle():
    api = FakeWin32(handle=77, exit_code=None)

    assert killswitch._windows_pid_alive(4242, api) is True
    assert api.closed == [77]


def test_posix_liveness_maps_the_results_of_signal_zero():
    sent = []

    def gone(pid, sig):
        sent.append((pid, sig))
        raise ProcessLookupError

    def denied(pid, sig):
        raise PermissionError

    assert killswitch._posix_pid_alive(4242, gone) is False
    assert sent == [(4242, 0)]  # signal 0: a probe, nothing is delivered
    assert killswitch._posix_pid_alive(4242, denied) is True
    assert killswitch._posix_pid_alive(4242, lambda pid, sig: None) is True


@pytest.mark.parametrize("pid", [0, -1, -4242])
def test_posix_liveness_never_signals_a_group_or_every_process(pid):
    """os.kill(0, ...) addresses the caller's process group and os.kill(-1, ...) every process."""
    def must_not_be_called(pid, sig):
        raise AssertionError("signalled")

    assert killswitch._posix_pid_alive(pid, must_not_be_called) is False


def test_posix_terminate_signals_leaves_first_then_kills_only_the_survivors():
    tree = {100: [200, 300], 200: [400], 300: [], 400: []}
    alive = {100, 200, 300, 400}
    sent = []

    def send(pid, sig):
        sent.append((pid, sig))
        if sig == 15 and pid == 200:
            return  # 200 ignores SIGTERM
        alive.discard(pid)

    ok = killswitch._posix_terminate_tree(
        100, send=send, children=lambda p: tree.get(p, []), alive=lambda p: p in alive, sleep=lambda s: None)

    assert ok is True
    assert sent == [(400, 15), (300, 15), (200, 15), (100, 15), (200, 9)]  # SIGKILL only for the survivor


def test_posix_terminate_reports_failure_when_the_root_survives():
    def send(pid, sig):
        pass  # nothing ever dies

    ok = killswitch._posix_terminate_tree(
        100, send=send, children=lambda p: [], alive=lambda p: True, sleep=lambda s: None)

    assert ok is False


def test_posix_terminate_does_not_signal_a_pid_that_already_exited():
    sent = []
    ok = killswitch._posix_terminate_tree(
        100, send=lambda pid, sig: sent.append((pid, sig)), children=lambda p: [], alive=lambda p: False,
        sleep=lambda s: None)

    assert ok is True and sent == []


@pytest.mark.parametrize("pid", [0, -1])
def test_posix_terminate_refuses_a_non_positive_pid(pid):
    sent = []

    assert killswitch._posix_terminate_tree(
        pid, send=lambda p, s: sent.append(p), children=lambda p: [], alive=lambda p: True,
        sleep=lambda s: None) is False
    assert sent == []


def test_descendants_come_parents_first_without_repeats_and_are_capped():
    tree = {1: [2, 3], 2: [4, 1], 3: [4], 4: [5], 5: []}

    assert killswitch._descendants(1, lambda p: tree.get(p, [])) == [1, 2, 3, 4, 5]
    assert killswitch._descendants(1, lambda p: [p + 1], limit=5) == [1, 2, 3, 4, 5]


def test_posix_children_reads_pgrep_and_shrugs_off_failures(monkeypatch):
    seen = []
    monkeypatch.setattr(killswitch, "_run", lambda args, timeout: seen.append(args) or (0, "201\n202\nnoise\n"))
    assert killswitch._posix_children(200) == [201, 202]
    assert seen == [["pgrep", "-P", "200"]]

    monkeypatch.setattr(killswitch, "_run", lambda args, timeout: (1, ""))
    assert killswitch._posix_children(200) == []

    def broken(args, timeout):
        raise FileNotFoundError("pgrep")

    monkeypatch.setattr(killswitch, "_run", broken)
    assert killswitch._posix_children(200) == []


def test_posix_command_line_prefers_proc_and_falls_back_to_ps(monkeypatch):
    assert killswitch._posix_command_line(
        4242, read_proc=lambda pid: b"python\0-m\0hermes_cli.main\0t_1\0").strip() == "python -m hermes_cli.main t_1"

    seen = []
    monkeypatch.setattr(killswitch, "_run", lambda args, timeout: seen.append(args) or (0, "ps output t_1\n"))
    assert killswitch._posix_command_line(4242, read_proc=lambda pid: None).strip() == "ps output t_1"
    assert seen == [["ps", "-o", "args=", "-p", "4242"]]


@ON_WINDOWS
def test_pid_alive_on_windows_uses_the_win32_api_and_never_os_kill(monkeypatch):
    monkeypatch.setattr(killswitch, "_Win32", lambda: FakeWin32(exit_code=259))
    assert REAL.pid_alive(4242) is True

    monkeypatch.setattr(killswitch, "_Win32", lambda: FakeWin32(handle=0, last_error=87))
    assert REAL.pid_alive(4242) is False
    assert REACHED == []  # the autouse fixture records any os.kill


@ON_WINDOWS
def test_pid_alive_says_alive_when_it_cannot_find_out(monkeypatch):
    def broken():
        raise OSError("kernel32 is not available")

    monkeypatch.setattr(killswitch, "_Win32", broken)

    assert REAL.pid_alive(4242) is True


@pytest.mark.parametrize("bad", [0, -5, "abc", None])
def test_pid_alive_says_an_invalid_pid_is_not_alive_without_touching_anything(bad):
    assert REAL.pid_alive(bad) is False


@ON_WINDOWS
def test_terminate_tree_on_windows_runs_taskkill_for_the_tree_and_never_os_kill(monkeypatch):
    seen = []
    monkeypatch.setattr(killswitch, "_run", lambda args, timeout: seen.append(args) or (0, ""))

    assert REAL.terminate_tree(4242) is True
    assert seen == [["taskkill", "/PID", "4242", "/T", "/F"]]

    monkeypatch.setattr(killswitch, "_run", lambda args, timeout: (128, ""))
    assert REAL.terminate_tree(4242) is False  # taskkill: the process was not found


@ON_WINDOWS
@pytest.mark.parametrize("error", [subprocess.TimeoutExpired("taskkill", 8), FileNotFoundError("taskkill"),
                                   RuntimeError("anything")])
def test_terminate_tree_never_raises(monkeypatch, error):
    def broken(args, timeout):
        raise error

    monkeypatch.setattr(killswitch, "_run", broken)

    assert REAL.terminate_tree(4242) is False


@pytest.mark.parametrize("bad", [0, 1, 4, -1, "abc", None])
def test_terminate_tree_refuses_a_pid_below_the_lowest_worker_pid(bad):
    assert REAL.terminate_tree(bad) is False  # and _run is the fixture's guard: reaching it would fail the test


@ON_WINDOWS
def test_process_command_line_on_windows_asks_cim_for_that_pid(monkeypatch):
    seen = []
    monkeypatch.setattr(killswitch, "_run",
                        lambda args, timeout: seen.append(args) or (0, 'python.exe -m x "task t_1"\r\n'))

    assert REAL.process_command_line(4242) == 'python.exe -m x "task t_1"'
    assert seen[0][0] == "powershell"
    assert "Win32_Process" in seen[0][-1] and "ProcessId = 4242" in seen[0][-1]


@ON_WINDOWS
@pytest.mark.parametrize("answer", [(1, "x"), (0, ""), (0, "   \r\n")])
def test_process_command_line_is_none_when_there_is_nothing_to_read(monkeypatch, answer):
    monkeypatch.setattr(killswitch, "_run", lambda args, timeout: answer)

    assert REAL.process_command_line(4242) is None


@ON_WINDOWS
def test_process_command_line_never_raises(monkeypatch):
    def broken(args, timeout):
        raise subprocess.TimeoutExpired("powershell", 8)

    monkeypatch.setattr(killswitch, "_run", broken)

    assert REAL.process_command_line(4242) is None


@pytest.mark.parametrize("bad", [0, -3, "abc", None])
def test_process_command_line_is_none_for_an_invalid_pid(bad):
    assert REAL.process_command_line(bad) is None


def test_run_captures_output_with_a_timeout_and_decodes_it_leniently(monkeypatch):
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"], seen["kwargs"] = args, kwargs
        return subprocess.CompletedProcess(args, 3, stdout=b"caf\xc3\xa9 \xff end", stderr=b"ignored")

    monkeypatch.setattr(killswitch.subprocess, "run", fake_run)

    assert REAL._run(["tool", "arg"], 7) == (3, "caf\u00e9 \ufffd end")
    assert seen["args"] == ["tool", "arg"]
    assert seen["kwargs"] == {"capture_output": True, "timeout": 7}  # no shell, output captured, a timeout


@ON_WINDOWS
def test_the_win32_wrapper_declares_pointer_sized_handles():
    """ctypes assumes a C int for a return value: without restype a 64-bit process handle would be truncated."""
    from ctypes import wintypes

    api = REAL._Win32()

    assert api._kernel32.OpenProcess.restype is wintypes.HANDLE
    assert api._kernel32.OpenProcess.argtypes[0] is wintypes.DWORD
    assert api._kernel32.GetExitCodeProcess.argtypes[0] is wintypes.HANDLE
    assert api._kernel32.CloseHandle.argtypes == [wintypes.HANDLE]


def test_reading_a_proc_command_line_for_a_pid_that_does_not_exist_gives_none():
    assert REAL._read_proc_cmdline(2 ** 31 - 5) is None


def test_default_list_containers_matches_names_and_labels_by_card_id_only(monkeypatch):
    seen = []
    listing = "\n".join([
        "sbx-t_1-web-1|com.docker.compose.project=sbx",
        "unrelated-db|com.example.owner=t_1,other=x",
        "my-postgres|a=b",
        "foo-t_12-1|x=y",
        "another-t_2-app|x=y",
        "",
    ])
    monkeypatch.setattr(killswitch, "_run", lambda args, timeout: seen.append(args) or (0, listing))

    names = REAL.default_list_containers(["t_1", "t_2"])

    assert names == ["sbx-t_1-web-1", "unrelated-db", "another-t_2-app"]  # not my-postgres, not t_12
    # CONTAINERS (round 17): docker ps is now filtered server-side to label=hermes-agent=1 first (killswitch.
    # default_list_containers's own docstring has the empirical finding), so an unrelated container can never
    # match even if this fake's own listing text (unrealistically) puts a card id in one of its labels.
    assert seen == [["docker", "ps", "--filter", "label=hermes-agent=1", "--format", "{{.Names}}|{{.Labels}}"]]


def test_default_list_containers_matches_the_real_hermes_label_format(monkeypatch):
    """CONTAINERS (round 17): the exact label text confirmed against installed Hermes 0.21.3 by
    scripts/hermes_container_labels_check.py (docker ps --format {{.Labels}}, Docker's own alphabetical key
    order): "hermes-agent=1,hermes-egress=off,hermes-profile=coder-1,hermes-task-id=default". A real,
    CLI-dispatched worker's hermes-task-id is always literally "default" (never the real card id: see
    ases.containers's module docstring for why), so this also proves the match logic still works if that
    label ever DID carry the real id (a hypothetical here, not today's reality), on top of the real,
    unmodified label string for the part that is real today."""
    seen = []
    real_shape = "hermes-a1b2c3d4|hermes-agent=1,hermes-egress=off,hermes-profile=coder-1,hermes-task-id=default"
    monkeypatch.setattr(killswitch, "_run", lambda args, timeout: seen.append(args) or (0, real_shape))

    assert REAL.default_list_containers(["default"]) == ["hermes-a1b2c3d4"]  # matches when asked for it directly

    hypothetical = "hermes-a1b2c3d4|hermes-agent=1,hermes-egress=off,hermes-profile=coder-1,hermes-task-id=t_1"
    monkeypatch.setattr(killswitch, "_run", lambda args, timeout: seen.append(args) or (0, hypothetical))

    assert REAL.default_list_containers(["t_1"]) == ["hermes-a1b2c3d4"]
    assert REAL.default_list_containers(["t_2"]) == []  # a different card id never matches


def test_default_list_containers_returns_nothing_without_card_ids_and_never_calls_docker():
    assert REAL.default_list_containers([]) == []
    assert REAL.default_list_containers(["", None]) == []  # _run is the fixture's guard


@pytest.mark.parametrize("failure", [
    FileNotFoundError("docker"), subprocess.TimeoutExpired("docker", 8), OSError("daemon"), RuntimeError("x"),
])
def test_default_list_containers_returns_nothing_when_docker_is_absent_or_broken(monkeypatch, failure):
    def broken(args, timeout):
        raise failure

    monkeypatch.setattr(killswitch, "_run", broken)

    assert REAL.default_list_containers(["t_1"]) == []


def test_default_list_containers_returns_nothing_when_docker_exits_non_zero(monkeypatch):
    monkeypatch.setattr(killswitch, "_run", lambda args, timeout: (1, "t_1|x=y"))

    assert REAL.default_list_containers(["t_1"]) == []


def test_default_stop_container_runs_docker_stop_with_a_five_second_grace(monkeypatch):
    seen = []
    monkeypatch.setattr(killswitch, "_run", lambda args, timeout: seen.append(args) or (0, ""))

    assert REAL.default_stop_container("sbx-t_1-web-1") is True
    assert seen == [["docker", "stop", "-t", "5", "sbx-t_1-web-1"]]

    monkeypatch.setattr(killswitch, "_run", lambda args, timeout: (1, ""))
    assert REAL.default_stop_container("sbx-t_1-web-1") is False


@pytest.mark.parametrize("failure", [FileNotFoundError("docker"), subprocess.TimeoutExpired("docker", 15)])
def test_default_stop_container_never_raises(monkeypatch, failure):
    def broken(args, timeout):
        raise failure

    monkeypatch.setattr(killswitch, "_run", broken)

    assert REAL.default_stop_container("sbx") is False


@pytest.mark.parametrize("name", ["", "   ", "-f", "--all", None])
def test_default_stop_container_refuses_a_name_that_could_be_an_option(name):
    assert REAL.default_stop_container(name) is False  # and _run is the fixture's guard


def test_os_kill_appears_in_the_code_only_as_an_argument_to_the_posix_helpers():
    """os.kill terminates a process on Windows, whatever the signal. In the module's code it may appear exactly
    twice, each time handed to a POSIX-only helper (never called, never a default argument), and in each of the
    two public functions the Windows branch is decided before that point."""
    tree = ast.parse(open(killswitch.__file__, encoding="utf-8").read())
    parent = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    uses = [node for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr == "kill"
            and isinstance(node.value, ast.Name) and node.value.id == "os"]

    assert len(uses) == 2
    shapes = set()
    for use in uses:
        holder = parent[use]
        call = parent[holder] if isinstance(holder, ast.keyword) else holder  # send=os.kill sits in a keyword
        assert isinstance(call, ast.Call)
        shapes.add((call.func.id, isinstance(holder, ast.keyword)))
    assert shapes == {("_posix_pid_alive", False), ("_posix_terminate_tree", True)}
    for function in (REAL.pid_alive, REAL.terminate_tree):  # killswitch.* are the fixture's guards here
        body = ast.parse(inspect.getsource(function))
        platform_checks = [node.lineno for node in ast.walk(body)
                           if isinstance(node, ast.Compare) and "sys.platform" in ast.unparse(node)]
        kill_uses = [node.lineno for node in ast.walk(body)
                     if isinstance(node, ast.Attribute) and node.attr == "kill"]
        assert platform_checks and kill_uses and min(platform_checks) < min(kill_uses)


def test_the_source_has_no_em_dash_or_section_sign():
    for path in (killswitch.__file__, __file__):
        text = open(path, encoding="utf-8").read()
        assert "\u2014" not in text and "\u00a7" not in text, path
