import copy
import json
import os
import shutil
import signal
import subprocess
import types

import pytest

from ases import db, hermes, intents, reconcile
from ases import plan as plan_mod


def _seed(conn, project="p1", task_key="T1", work="t_work", merge="t_merge"):
    conn.execute(
        "INSERT INTO plan_tasks (project, task_key, work_card_id, merge_card_id, role, touches, "
        "gate_profile, estimated_requests, created_at) VALUES (?, ?, ?, ?, 'coder', '[]', 'g', 10, "
        "datetime('now'))",
        (project, task_key, work, merge),
    )


def test_clean_state_has_no_findings(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "ases.db")
    _seed(conn)
    monkeypatch.setattr(hermes, "kanban_show", lambda b, cid: {"status": "ready"})

    assert reconcile.check("b", "p1", conn=conn) == []


def test_missing_card_is_flagged(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "ases.db")
    _seed(conn)

    def fake_show(b, cid):
        if cid == "t_work":
            raise hermes.HermesCommandError(["kanban", "show"], 1, "not found")
        return {"status": "ready"}

    monkeypatch.setattr(hermes, "kanban_show", fake_show)

    findings = reconcile.check("b", "p1", conn=conn)
    assert len(findings) == 1
    assert findings[0].kind == "missing_card"
    assert findings[0].task_key == "T1"


def test_done_merge_without_record_is_flagged(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "ases.db")
    _seed(conn)
    monkeypatch.setattr(hermes, "kanban_show", lambda b, cid: (
        {"status": "done"} if cid == "t_merge" else {"status": "done"}
    ))

    findings = reconcile.check("b", "p1", conn=conn)
    assert any(f.kind == "merge_done_without_record" for f in findings)


def test_done_merge_with_proper_record_is_clean(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "ases.db")
    _seed(conn)
    conn.execute(
        "INSERT INTO merge_records (task_key, candidate_sha, gate3_result, squash_commit, reverted, "
        "completed_at) VALUES ('T1', 'abc', 'pass', 'abc', 0, datetime('now'))"
    )
    monkeypatch.setattr(hermes, "kanban_show", lambda b, cid: (
        {"status": "done"} if cid == "t_merge" else {"status": "done"}
    ))

    assert reconcile.check("b", "p1", conn=conn) == []


def test_done_merge_with_a_no_op_record_is_clean(tmp_path, monkeypatch):
    """A review-only task's merge card completes as a recorded no-op: gate3_result "skipped", no squash
    commit, completed_at set (the row mergeq.merge_task writes for an empty diff). That is a finished
    merge, not a merge_done_without_record finding."""
    conn = db.connect(tmp_path / "ases.db")
    _seed(conn)
    conn.execute(
        "INSERT INTO merge_records (task_key, candidate_sha, gate3_result, squash_commit, reverted, "
        "completed_at) VALUES ('T1', 'abc', 'skipped', NULL, 0, datetime('now'))"
    )
    monkeypatch.setattr(hermes, "kanban_show", lambda b, cid: {"status": "done"})

    assert reconcile.check("b", "p1", conn=conn) == []


def test_done_but_reverted_is_flagged(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "ases.db")
    _seed(conn)
    conn.execute(
        "INSERT INTO merge_records (task_key, candidate_sha, gate3_result, squash_commit, reverted, "
        "completed_at) VALUES ('T1', 'abc', 'pass', 'abc', 1, datetime('now'))"
    )
    monkeypatch.setattr(hermes, "kanban_show", lambda b, cid: (
        {"status": "done"} if cid == "t_merge" else {"status": "done"}
    ))

    findings = reconcile.check("b", "p1", conn=conn)
    assert any(f.kind == "done_but_reverted" for f in findings)


def test_only_checks_the_given_project(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "ases.db")
    _seed(conn, project="p1", task_key="T1")
    _seed(conn, project="p2", task_key="T1", work="other_work", merge="other_merge")

    def fake_show(b, cid):
        if cid == "other_work":
            raise AssertionError("should not check project p2's cards")
        return {"status": "ready"}

    monkeypatch.setattr(hermes, "kanban_show", fake_show)
    assert reconcile.check("b", "p1", conn=conn) == []


# =============================================================================================
# Process helpers. Nothing below spawns or kills a real process: the platform switch, the kernel32
# calls, os.kill, taskkill and PowerShell are all replaced by fakes. The only real calls are the
# liveness probes of THIS process and of an impossible pid, which read and never signal.
# =============================================================================================


def _run(pid=None, *, ended=False, profile="coder-1", run_id=1):
    return {"id": run_id, "profile": profile, "status": "done" if ended else "running",
            "outcome": "completed" if ended else None, "summary": None, "error": None, "metadata": None,
            "started_at": 1, "ended_at": 2 if ended else None, "worker_pid": pid}


def test_worker_pid_is_the_last_live_run_with_a_pid():
    card = {"status": "running", "_runs": [_run(111, ended=True, run_id=1), _run(222, run_id=2), _run(333, run_id=3)]}
    assert reconcile.worker_pid(card) == 333


def test_worker_pid_skips_ended_runs_and_runs_without_a_pid():
    card = {"status": "running", "_runs": [_run(222, run_id=1), _run(None, run_id=2), _run(333, ended=True, run_id=3)]}
    assert reconcile.worker_pid(card) == 222


@pytest.mark.parametrize("raw,expected", [("4242", 4242), (4242, 4242), (4242.0, 4242)])
def test_worker_pid_accepts_int_like_values(raw, expected):
    assert reconcile.worker_pid({"status": "running", "_runs": [_run(raw)]}) == expected


@pytest.mark.parametrize("bad", [None, "", "abc", 0, -3, True, [], {}])
def test_worker_pid_ignores_values_that_are_not_a_pid(bad):
    assert reconcile.worker_pid({"status": "running", "_runs": [_run(bad)]}) is None


def test_worker_pid_falls_back_to_the_cards_own_field_only_while_running():
    assert reconcile.worker_pid({"status": "running", "worker_pid": 555, "_runs": []}) == 555
    assert reconcile.worker_pid({"status": "running", "worker_pid": "556"}) == 556
    assert reconcile.worker_pid({"status": "ready", "worker_pid": 555, "_runs": []}) is None
    assert reconcile.worker_pid({"status": "running", "worker_pid": None}) is None
    assert reconcile.worker_pid({"status": "running", "_runs": [_run(111, ended=True)]}) is None


def test_worker_pid_prefers_a_live_run_over_the_cards_own_field():
    assert reconcile.worker_pid({"status": "running", "worker_pid": 555, "_runs": [_run(777)]}) == 777


class FakeWinApi:
    """The three kernel32 calls, as reconcile._WinApi exposes them."""

    def __init__(self, handle, error, code):
        self.handle, self.error, self.code, self.closed = handle, error, code, []

    def open_process(self, pid):
        return (self.handle, 0) if self.handle else (None, self.error)

    def exit_code(self, handle):
        return self.code

    def close(self, handle):
        self.closed.append(handle)


@pytest.mark.parametrize("handle,error,code,alive", [
    (None, 87, None, False),   # ERROR_INVALID_PARAMETER: no such pid, the only proof of death without a handle
    (None, 5, None, True),     # access denied: the process exists
    (None, 0, None, True),     # any other failure: unclear, so alive
    (1234, 0, 259, True),      # STILL_ACTIVE
    (1234, 0, 0, False),       # exited
    (1234, 0, 1, False),
    (1234, 0, None, True),     # exit code unreadable: unclear, so alive
])
def test_windows_liveness_reads_the_exit_code_and_errs_towards_alive(handle, error, code, alive):
    api = FakeWinApi(handle, error, code)
    assert reconcile._pid_alive_windows(4242, api) is alive
    assert api.closed == ([handle] if handle else [])  # a handle that was opened is always closed


def test_pid_alive_sees_this_process_and_not_an_impossible_pid():
    """Real, but read-only: the current process is alive and pid 2**31-8 does not exist, on Windows through
    OpenProcess and on POSIX through os.kill(pid, 0), which sends no signal."""
    assert reconcile.pid_alive(os.getpid()) is True
    assert reconcile.pid_alive(2**31 - 8) is False


def test_pid_alive_picks_the_windows_or_the_posix_path_by_platform(monkeypatch):
    calls = []
    monkeypatch.setattr(reconcile, "_pid_alive_windows", lambda pid, api=None: calls.append(("win", pid)) or True)
    monkeypatch.setattr(reconcile, "_pid_alive_posix", lambda pid, kill=None: calls.append(("posix", pid)) or True)

    monkeypatch.setattr(reconcile, "_is_windows", lambda: True)
    assert reconcile.pid_alive(4242) is True
    monkeypatch.setattr(reconcile, "_is_windows", lambda: False)
    assert reconcile.pid_alive("4243") is True

    assert calls == [("win", 4242), ("posix", 4243)]  # Windows never reaches the os.kill path


@pytest.mark.parametrize("bad", [0, -5, None, "abc", True])
def test_pid_alive_is_false_for_anything_that_is_not_a_pid_without_asking_the_platform(monkeypatch, bad):
    def boom(*args, **kwargs):
        raise AssertionError("the platform must not be asked about a non-pid")

    monkeypatch.setattr(reconcile, "_pid_alive_windows", boom)
    monkeypatch.setattr(reconcile, "_pid_alive_posix", boom)
    assert reconcile.pid_alive(bad) is False


@pytest.mark.parametrize("error,expected", [(ProcessLookupError(), False), (PermissionError(), True), (None, True)])
def test_posix_liveness_sends_only_signal_zero(error, expected):
    sent = []

    def kill(pid, sig):
        sent.append((pid, sig))
        if error is not None:
            raise error

    assert reconcile._pid_alive_posix(4242, kill) is expected
    assert sent == [(4242, 0)]


@pytest.mark.parametrize("pid", [0, 1, 4, -7, None, True, "abc"])
def test_terminate_tree_refuses_a_pid_that_cannot_be_a_worker(monkeypatch, pid):
    def boom(*args, **kwargs):
        raise AssertionError("nothing may be terminated for an implausible pid")

    monkeypatch.setattr(reconcile, "_terminate_windows", boom)
    monkeypatch.setattr(reconcile, "_terminate_posix", boom)
    assert reconcile.terminate_tree(pid) is False


def test_terminate_tree_never_terminates_this_process_or_its_parent(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("the controller must not terminate itself or its parent")

    monkeypatch.setattr(reconcile, "_terminate_windows", boom)
    monkeypatch.setattr(reconcile, "_terminate_posix", boom)
    assert reconcile.terminate_tree(os.getpid()) is False
    assert reconcile.terminate_tree(os.getppid()) is False


def test_terminate_tree_picks_taskkill_on_windows_and_signals_on_posix(monkeypatch):
    calls = []
    monkeypatch.setattr(reconcile, "_terminate_windows", lambda pid: calls.append(("win", pid)) or True)
    monkeypatch.setattr(reconcile, "_terminate_posix", lambda pid: calls.append(("posix", pid)) or True)

    monkeypatch.setattr(reconcile, "_is_windows", lambda: True)
    assert reconcile.terminate_tree(987654) is True
    monkeypatch.setattr(reconcile, "_is_windows", lambda: False)
    assert reconcile.terminate_tree("987655") is True

    assert calls == [("win", 987654), ("posix", 987655)]


@pytest.mark.parametrize("returncode,still_alive,expected", [(0, True, True), (128, False, True), (1, True, False)])
def test_windows_termination_uses_taskkill_tree_and_force(returncode, still_alive, expected):
    seen = []

    def run(argv, **kwargs):
        seen.append(argv)
        return types.SimpleNamespace(returncode=returncode)

    assert reconcile._terminate_windows(4242, run=run, alive=lambda pid: still_alive) is expected
    assert seen == [["taskkill", "/PID", "4242", "/T", "/F"]]


@pytest.mark.parametrize("error", [OSError("no taskkill"), subprocess.TimeoutExpired("taskkill", 30)])
def test_windows_termination_that_cannot_run_is_a_failure(error):
    def run(argv, **kwargs):
        raise error

    assert reconcile._terminate_windows(4242, run=run, alive=lambda pid: True) is False


def _posix(pid=4242, *, group=4242, own=1, alive=lambda p: False, kill_error=None, sends=None):
    """Drive _terminate_posix with fakes for every os call; `sends` collects (target, pid or group, signal)."""
    sends = [] if sends is None else sends

    def kill(target, sig):
        sends.append(("pid", target, sig))
        if kill_error:
            raise kill_error

    def killpg(target, sig):
        sends.append(("group", target, sig))
        if kill_error:
            raise kill_error

    result = reconcile._terminate_posix(
        pid, kill=kill, killpg=killpg, getpgid=lambda p: own if p == 0 else group, sleep=lambda s: None, alive=alive,
    )
    return result, sends


def test_posix_termination_sends_sigterm_to_the_process_group_and_stops_once_it_is_gone():
    result, sends = _posix(group=4242, own=1)
    assert result is True
    assert sends == [("group", 4242, signal.SIGTERM)]


def test_posix_termination_never_signals_the_controllers_own_group():
    result, sends = _posix(group=50, own=50)
    assert result is True
    assert sends == [("pid", 4242, signal.SIGTERM)]  # the single pid, not the group the controller lives in


def test_posix_termination_escalates_to_sigkill_when_sigterm_is_not_enough():
    sends = []
    result, _ = _posix(alive=lambda p: len(sends) < 2, sends=sends)
    assert result is True
    assert len(sends) == 2 and sends[0][2] == signal.SIGTERM
    assert sends[1][2] == getattr(signal, "SIGKILL", signal.SIGTERM)


def test_posix_termination_that_does_not_take_reports_failure():
    result, sends = _posix(alive=lambda p: True)
    assert result is False
    assert len(sends) == 2  # SIGTERM, then SIGKILL, then it gave up


def test_posix_termination_of_a_process_that_is_already_gone_succeeds():
    result, sends = _posix(kill_error=ProcessLookupError())
    assert result is True and len(sends) == 1


def test_posix_termination_without_permission_fails():
    result, _ = _posix(kill_error=PermissionError())
    assert result is False


def test_windows_command_line_comes_from_powershell_cim_with_the_pid_as_an_int():
    seen = []

    def run(argv, **kwargs):
        seen.append(argv)
        return types.SimpleNamespace(
            returncode=0, stdout='  python -m hermes_cli.main chat -q "work kanban task t_1"  \n')

    line = reconcile._command_line_windows(4242, run=run)

    assert line == 'python -m hermes_cli.main chat -q "work kanban task t_1"'
    assert seen[0][0] == "powershell" and "-NoProfile" in seen[0]
    assert "ProcessId=4242" in seen[0][-1] and "Win32_Process" in seen[0][-1]


@pytest.mark.parametrize("result", [
    types.SimpleNamespace(returncode=1, stdout="whatever"),
    types.SimpleNamespace(returncode=0, stdout="   \n"),
    types.SimpleNamespace(returncode=0, stdout=None),
    OSError("no powershell"),
    subprocess.TimeoutExpired("powershell", 20),
])
def test_windows_command_line_is_none_when_it_cannot_be_read(result):
    def run(argv, **kwargs):
        if isinstance(result, Exception):
            raise result
        return result

    assert reconcile._command_line_windows(4242, run=run) is None


def test_posix_command_line_reads_proc_cmdline_and_joins_the_nul_separated_arguments(tmp_path):
    (tmp_path / "4242").mkdir()
    (tmp_path / "4242" / "cmdline").write_bytes(b"python\0-m\0hermes_cli.main\0work kanban task t_1\0")

    def no_ps(argv, **kwargs):
        raise AssertionError("ps is only the fallback")

    assert reconcile._command_line_posix(4242, proc_root=str(tmp_path), run=no_ps) == \
        "python -m hermes_cli.main work kanban task t_1"


@pytest.mark.parametrize("result,expected", [
    (types.SimpleNamespace(returncode=0, stdout="python worker t_1\n"), "python worker t_1"),
    (types.SimpleNamespace(returncode=1, stdout=""), None),
    (types.SimpleNamespace(returncode=0, stdout="\n"), None),
    (OSError("no ps"), None),
    (subprocess.TimeoutExpired("ps", 10), None),
])
def test_posix_command_line_falls_back_to_ps_when_there_is_no_procfs(tmp_path, result, expected):
    seen = []

    def run(argv, **kwargs):
        seen.append(argv)
        if isinstance(result, Exception):
            raise result
        return result

    assert reconcile._command_line_posix(4242, proc_root=str(tmp_path), run=run) == expected
    assert seen == [["ps", "-o", "args=", "-p", "4242"]]


def test_process_command_line_picks_the_platform_and_refuses_a_non_pid(monkeypatch):
    monkeypatch.setattr(reconcile, "_command_line_windows", lambda pid: f"win:{pid}")
    monkeypatch.setattr(reconcile, "_command_line_posix", lambda pid: f"posix:{pid}")

    monkeypatch.setattr(reconcile, "_is_windows", lambda: True)
    assert reconcile.process_command_line(4242) == "win:4242"
    monkeypatch.setattr(reconcile, "_is_windows", lambda: False)
    assert reconcile.process_command_line("4243") == "posix:4243"
    assert reconcile.process_command_line(0) is None
    assert reconcile.process_command_line(None) is None
    assert reconcile.process_command_line(True) is None


def test_the_report_and_its_parts_are_plain_values():
    finding = reconcile.Inconsistency("T1", "k", "d")
    repair = reconcile.Repair("T1", "k", "d", True)
    assert reconcile.ReconcileReport().clean is True
    assert reconcile.ReconcileReport([finding], [], []).clean is False
    assert reconcile.ReconcileReport([], [repair], []).clean is True  # a repair with no finding is not an inconsistency
    with pytest.raises(Exception):
        repair.applied = False


# =============================================================================================
# reconcile(): the board, git and the database compared and repaired. Real temp git repos with a
# branch named integration; real commits whose messages carry "Merge card: <id>" exactly as
# process_merge_queue writes them; a fake Hermes board and fake processes.
# =============================================================================================


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def _git_ok(*args, cwd):
    result = _git(*args, cwd=cwd)
    assert result.returncode == 0, result.stderr
    return result


@pytest.fixture(scope="module")
def _template_repo(tmp_path_factory):
    """One real git repository with a first commit on a branch named integration, built once per module: making a
    repository costs five git calls, and every scenario needs a fresh one."""
    r = tmp_path_factory.mktemp("template") / "repo"
    r.mkdir()
    _git_ok("init", "-q", "-b", "integration", cwd=r)
    _git_ok("config", "user.email", "t@t", cwd=r)
    _git_ok("config", "user.name", "t", cwd=r)
    (r / "base.txt").write_text("base\n", encoding="utf-8")
    _git_ok("add", "-A", cwd=r)
    _git_ok("commit", "-q", "-m", "init", cwd=r)
    return r


@pytest.fixture
def repo(tmp_path, _template_repo):
    r = tmp_path / "repo"
    shutil.copytree(_template_repo, r)
    return r


def _head(repo):
    return _git_ok("rev-parse", "HEAD", cwd=repo).stdout.strip()


def _land(repo, key="T1", merge="t_merge", work="t_work"):
    """A commit on integration shaped like the squash commit process_merge_queue writes (ASES-GIT-06): the card ids
    are in its message, which is how a merge is recognised in git after a crash. Returns its sha."""
    (repo / f"{key}.txt").write_text(f"{key}\n", encoding="utf-8")
    _git_ok("add", "-A", cwd=repo)
    message = (f"{key}: task\n\nWork card: {work}\nMerge card: {merge}\nBranch: swarm/{key}-coder\n"
               "Controlled by ASES (one squash commit per plan task).")
    _git_ok("commit", "-q", "-m", message, cwd=repo)
    return _head(repo)


def _side_commit(repo):
    """A commit that is on a side branch and NOT on integration. Returns its sha."""
    _git_ok("checkout", "-q", "-b", "side", cwd=repo)
    (repo / "side.txt").write_text("side\n", encoding="utf-8")
    _git_ok("add", "-A", cwd=repo)
    _git_ok("commit", "-q", "-m", "side work", cwd=repo)
    sha = _head(repo)
    _git_ok("checkout", "-q", "integration", cwd=repo)
    return sha


def _plan(*keys, project="p1"):
    tasks = tuple(plan_mod.PlanTask(k, f"task {k}", "coder", (), (), ("ok",), "g", 1) for k in keys)
    return plan_mod.Plan(project, "integration", {"g": ["echo ok"]}, tasks)


def _task(conn, key="T1", *, project="p1", role="coder", work="t_work", merge="t_merge"):
    conn.execute(
        "INSERT INTO plan_tasks (project, task_key, work_card_id, merge_card_id, role, touches, gate_profile, "
        "estimated_requests, created_at) VALUES (?, ?, ?, ?, ?, '[]', 'g', 10, datetime('now'))",
        (project, key, work, merge, role),
    )


def _record(conn, key="T1", *, candidate="abc", gate3="pass", squash=None, reverted=0,
            completed="2026-09-19T00:00:00+00:00"):
    conn.execute(
        "INSERT INTO merge_records (task_key, candidate_sha, gate3_result, squash_commit, reverted, completed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)", (key, candidate, gate3, squash, reverted, completed),
    )


def _card(card_id, status="ready", runs=(), **extra):
    return {"id": card_id, "status": status, "_runs": [dict(r) for r in runs], "_events": [], "_comments": [],
            "_children": [], "_parents": [], **extra}


def _merge_row(conn, key="T1"):
    return conn.execute("SELECT * FROM merge_records WHERE task_key = ?", (key,)).fetchone()


def _repair_events(conn):
    return [json.loads(r["payload"]) for r in
            conn.execute("SELECT payload FROM events WHERE kind = 'reconcile_repair' ORDER BY id")]


def _kinds(items):
    return [i.kind for i in items]


_FORBIDDEN_HERMES_CALLS = (
    "kanban_create", "kanban_link", "kanban_dispatch", "kanban_block", "kanban_schedule", "kanban_unblock",
    "kanban_comment", "kanban_promote", "kanban_archive", "kanban_set_model", "kanban_reopen_review",
    "kanban_request_changes", "pause", "resume",
)


class World:
    """A fake Hermes board plus fake processes. reconcile may only ever COMPLETE or RECLAIM a card: every other
    mutating hermes call lands in `forbidden` (and raises), and the make_world fixture fails the test when that list
    is not empty at the end, so a stray kanban_create can never be swallowed by reconcile's own error handling.
    That also proves the crash scenarios create no duplicate cards."""

    def __init__(self, monkeypatch, cards):
        self.cards = cards
        self.calls = []            # the completes and reclaims reconcile made, in order
        self.forbidden = []
        self.fail_show = {}        # card id -> exception raised by kanban_show
        self.fail_complete = {}    # card id -> exception raised by kanban_complete
        self.fail_reclaim = {}     # card id -> exception raised by kanban_reclaim
        self.alive_pids = set()
        self.commands = {}         # pid -> command line, None = unreadable
        self.killed = []
        self.kill_ok = True
        monkeypatch.setattr(hermes, "kanban_show", self.show)
        monkeypatch.setattr(hermes, "kanban_complete", self.complete)
        monkeypatch.setattr(hermes, "kanban_reclaim", self.reclaim)
        for name in _FORBIDDEN_HERMES_CALLS:
            monkeypatch.setattr(hermes, name, self._forbid(name))

    def _forbid(self, name):
        def call(*args, **kwargs):
            self.forbidden.append(name)
            raise AssertionError(f"reconcile must not call hermes.{name}")
        return call

    def show(self, board, card_id):
        if card_id in self.fail_show:
            raise self.fail_show[card_id]
        if card_id not in self.cards:
            raise hermes.HermesCommandError(["kanban", "show", card_id], 1, "task not found")
        return copy.deepcopy(self.cards[card_id])

    def complete(self, board, card_id, *, result=None, metadata=None):
        if card_id in self.fail_complete:
            raise self.fail_complete[card_id]
        self.calls.append(("complete", card_id, result, metadata))
        self.cards[card_id]["status"] = "done"

    def reclaim(self, board, card_id, *, reason=None):
        if card_id in self.fail_reclaim:
            raise self.fail_reclaim[card_id]
        self.calls.append(("reclaim", card_id, reason))
        card = self.cards[card_id]
        card["status"] = "ready"
        for run in card["_runs"]:
            if not run.get("ended_at"):
                run["ended_at"] = 99

    def alive(self, pid):
        return pid in self.alive_pids

    def command_line(self, pid):
        return self.commands.get(pid)

    def killer(self, pid):
        self.killed.append(pid)
        if self.kill_ok:
            self.alive_pids.discard(pid)
        return self.kill_ok

    def go(self, repo, conn, plan=None, **kwargs):
        return reconcile.reconcile("b", repo, plan or _plan("T1"), conn=conn, alive=self.alive,
                                   killer=self.killer, command_line=self.command_line, **kwargs)


@pytest.fixture
def make_world(monkeypatch):
    worlds = []

    def make(cards):
        world = World(monkeypatch, cards)
        worlds.append(world)
        return world

    yield make
    for world in worlds:
        assert world.forbidden == [], f"reconcile called a hermes mutation it must never make: {world.forbidden}"


@pytest.fixture
def env(tmp_path, repo, make_world):
    """conn + repo + a fake board. env.board(*cards) installs the board, env.task(...) seeds a plan task and
    env.go(...) runs reconcile against all of it."""
    conn = db.connect(tmp_path / "ases.db")
    ns = types.SimpleNamespace(conn=conn, repo=repo, world=None, tmp=tmp_path)

    def board(*cards):
        ns.world = make_world({c["id"]: c for c in cards})
        return ns.world

    ns.board = board
    ns.task = lambda key="T1", **kw: _task(conn, key, **kw)
    ns.go = lambda plan=None, **kw: ns.world.go(repo, conn, plan, **kw)
    return ns


# -- a. the checks check() already made --------------------------------------------------------------


def test_a_consistent_project_reports_nothing_and_changes_nothing(env):
    env.task()
    env.board(_card("t_work", "done"), _card("t_merge", "blocked"))
    before = env.conn.total_changes

    report = env.go()

    assert report.clean and report.findings == [] and report.repairs == [] and report.blocked == []
    assert env.world.calls == [] and env.conn.total_changes == before


def test_a_card_that_no_longer_resolves_stays_a_blocked_finding_and_other_tasks_carry_on(env):
    env.task("T1")
    env.task("T2", work="u_work", merge="u_merge")
    sha = _land(env.repo, "T2", merge="u_merge", work="u_work")
    _record(env.conn, "T2", candidate=sha, squash=sha)
    env.board(_card("u_work", "done"), _card("u_merge", "blocked"), _card("t_merge", "blocked"))  # t_work is gone

    report = env.go(_plan("T1", "T2"))

    assert [(f.task_key, f.kind) for f in report.blocked if f.kind == "missing_card"] == [("T1", "missing_card")]
    assert "t_work" in report.blocked[0].detail
    assert [(r.task_key, r.kind, r.applied) for r in report.repairs] == [("T2", "merge_card_completed", True)]


def test_a_done_merge_recorded_as_reverted_is_a_blocked_finding_and_is_not_touched(env):
    env.task()
    sha = _land(env.repo)
    _record(env.conn, candidate=sha, squash=sha, reverted=1)
    env.board(_card("t_work", "done"), _card("t_merge", "done"))

    report = env.go()

    assert _kinds(report.findings) == ["done_but_reverted"] and report.blocked == report.findings
    assert report.repairs == [] and env.world.calls == []


def test_a_reconcile_reports_the_same_check_findings_as_check_does(env):
    env.task()
    env.board(_card("t_work", "done"), _card("t_merge", "done"))  # done merge, no record, no commit, coder

    from_check = reconcile.check("b", "p1", conn=env.conn)
    report = env.go()

    assert _kinds(from_check) == _kinds(report.findings) == ["merge_done_without_record"]
    assert report.findings[0].detail.startswith(from_check[0].detail)  # the same finding, explained further


def test_only_the_given_projects_tasks_and_intents_are_reconciled(env):
    env.task("T1")
    env.task("T7", project="p2", work="other_work", merge="other_merge")
    _record(env.conn, "T7", squash="deadbeef" * 5)  # p2's business, never looked at
    intents.begin(env.conn, "p2", intents.KIND_FAST_FORWARD, "T7")
    env.board(_card("t_work", "done"), _card("t_merge", "blocked"))  # other_work / other_merge are not on the board

    report = env.go()

    assert report.clean and env.world.calls == []
    assert len(intents.open_intents(env.conn, "p2")) == 1  # p2's open intent is still open


# -- b. merge card done without a record ---------------------------------------------------------------


def test_b_a_done_merge_card_with_a_landed_commit_gets_its_record_written_from_git(env):
    env.task()
    sha = _land(env.repo)
    env.board(_card("t_work", "done"), _card("t_merge", "done"))

    report = env.go()

    row = _merge_row(env.conn)
    assert (row["candidate_sha"], row["squash_commit"], row["gate3_result"], row["reverted"]) == \
        (sha, sha, "recovered", 0)
    assert row["completed_at"]
    assert _kinds(report.findings) == ["merge_done_without_record"] and report.blocked == []
    assert [(r.task_key, r.kind, r.applied) for r in report.repairs] == [("T1", "merge_record_recovered", True)]
    assert env.world.calls == []  # the card was already done
    assert _repair_events(env.conn) == [
        {"task_key": "T1", "kind": "merge_record_recovered", "detail": report.repairs[0].detail}]
    assert env.go().clean  # a second run finds nothing


def test_b_a_review_only_task_with_no_commit_gets_the_no_op_record(env):
    env.task(role="reviewer")
    env.board(_card("t_work", "done"), _card("t_merge", "done"))

    report = env.go()

    row = _merge_row(env.conn)
    assert (row["gate3_result"], row["squash_commit"], row["reverted"]) == ("skipped", None, 0)
    assert row["completed_at"]
    assert report.blocked == [] and [r.kind for r in report.repairs] == ["merge_record_noop"]
    assert reconcile.check("b", "p1", conn=env.conn) == []  # the record is what check() accepts as a done no-op
    assert env.go().clean


def test_b_a_coder_merge_card_that_says_done_with_no_commit_is_blocked_and_nothing_is_written(env):
    env.task()
    env.board(_card("t_work", "done"), _card("t_merge", "done"))

    report = env.go()

    assert _kinds(report.blocked) == ["merge_done_without_record"] and report.blocked[0] in report.findings
    assert "Merge card: t_merge" in report.blocked[0].detail
    assert _merge_row(env.conn) is None and report.repairs == [] and _repair_events(env.conn) == []


def test_b_a_tester_merge_card_that_says_done_with_no_commit_is_blocked_and_nothing_is_written(env):
    """Regression for the controller.py _COMMITTING_ROLES bug class: a tester commits real work just like a
    coder does, so a tester's merge card that says done with no git commit must be escalated for a person to
    look at, never silently recorded as a legitimate no-op (that treatment is for review-only roles only)."""
    env.task(role="tester")
    env.board(_card("t_work", "done"), _card("t_merge", "done"))

    report = env.go()

    assert _kinds(report.blocked) == ["merge_done_without_record"] and report.blocked[0] in report.findings
    assert "Merge card: t_merge" in report.blocked[0].detail
    assert _merge_row(env.conn) is None and report.repairs == [] and _repair_events(env.conn) == []


def test_b_a_commit_for_a_card_whose_id_only_starts_with_ours_is_not_ours(env):
    env.task()
    _land(env.repo, "T9", merge="t_merge2", work="t_work2")  # t_merge is a PREFIX of t_merge2
    env.board(_card("t_work", "done"), _card("t_merge", "done"))

    report = env.go()

    assert _kinds(report.blocked) == ["merge_done_without_record"] and _merge_row(env.conn) is None


def test_b_a_landed_commit_that_git_shows_was_reverted_is_not_recovered_as_a_live_merge(env):
    env.task()
    sha = _land(env.repo)
    _git_ok("revert", "--no-edit", sha, cwd=env.repo)
    env.board(_card("t_work", "done"), _card("t_merge", "done"))

    report = env.go()

    assert _kinds(report.blocked) == ["merge_done_without_record"] and "reverted" in report.blocked[0].detail
    assert _merge_row(env.conn) is None and report.repairs == []


def test_b_an_unfinished_record_is_finished_from_git_keeping_what_the_merge_queue_wrote(env):
    env.task()
    sha = _land(env.repo)
    _record(env.conn, candidate=sha, gate3="pass", squash=None, completed=None)
    env.board(_card("t_work", "done"), _card("t_merge", "done"))

    report = env.go()

    row = _merge_row(env.conn)
    assert (row["candidate_sha"], row["gate3_result"], row["squash_commit"]) == (sha, "pass", sha)
    assert row["completed_at"]
    assert [r.kind for r in report.repairs] == ["merge_record_recovered"] and report.blocked == []


# -- c. merge record without a done card (the crash between the fast-forward and the card completion) --


def test_c_a_completed_record_with_the_card_not_done_completes_the_card(env):
    env.task()
    sha = _land(env.repo)
    _record(env.conn, candidate=sha, squash=sha)
    env.board(_card("t_work", "done"), _card("t_merge", "blocked"))

    report = env.go()

    assert env.world.calls == [
        ("complete", "t_merge", f"merged {sha} (recovered)", {"squash_commit": sha, "recovered": True})]
    assert _kinds(report.findings) == ["merge_record_without_done_card"] and report.blocked == []
    assert [(r.kind, r.applied) for r in report.repairs] == [("merge_card_completed", True)]
    assert len(_repair_events(env.conn)) == 1
    assert env.go().clean and len(env.world.calls) == 1  # the card is done now: nothing is completed twice


def test_c_a_no_op_record_completes_the_card_as_a_no_op(env):
    env.task(role="reviewer")
    _record(env.conn, candidate="base", gate3="skipped", squash=None)
    env.board(_card("t_work", "done"), _card("t_merge", "blocked"))

    report = env.go()

    assert env.world.calls == [("complete", "t_merge", "no changes to merge (review-only task)",
                                {"squash_commit": None, "no_op": True, "recovered": True})]
    assert report.blocked == [] and [r.kind for r in report.repairs] == ["merge_card_completed"]


def test_c_a_recorded_commit_that_is_not_on_the_integration_branch_is_blocked(env):
    env.task()
    sha = _side_commit(env.repo)
    _record(env.conn, candidate=sha, squash=sha)
    env.board(_card("t_work", "done"), _card("t_merge", "blocked"))

    report = env.go()

    assert _kinds(report.blocked) == ["merge_record_without_done_card"]
    assert "not on 'integration'" in report.blocked[0].detail
    assert env.world.calls == [] and report.repairs == []


def test_c_a_recorded_commit_that_was_reverted_is_blocked(env):
    env.task()
    sha = _land(env.repo)
    _git_ok("revert", "--no-edit", sha, cwd=env.repo)
    _record(env.conn, candidate=sha, squash=sha)
    env.board(_card("t_work", "done"), _card("t_merge", "blocked"))

    report = env.go()

    assert _kinds(report.blocked) == ["merge_record_without_done_card"] and env.world.calls == []


@pytest.mark.parametrize("status", ["review", "scheduled", "triage", "archived"])
def test_c_a_merge_card_in_a_state_reconcile_does_not_complete_from_is_blocked(env, status):
    env.task()
    sha = _land(env.repo)
    _record(env.conn, candidate=sha, squash=sha)
    env.board(_card("t_work", "done"), _card("t_merge", status))

    report = env.go()

    assert _kinds(report.blocked) == ["merge_record_without_done_card"] and env.world.calls == []


@pytest.mark.parametrize("status", ["blocked", "ready", "todo"])
def test_c_the_states_the_merge_queue_itself_completes_from_are_completed(env, status):
    env.task()
    sha = _land(env.repo)
    _record(env.conn, candidate=sha, squash=sha)
    env.board(_card("t_work", "done"), _card("t_merge", status))

    assert env.go().blocked == [] and [c[0] for c in env.world.calls] == ["complete"]


def test_c_a_reverted_record_with_the_card_not_done_is_consistent_and_left_alone(env):
    env.task()
    sha = _land(env.repo)
    _record(env.conn, candidate=sha, squash=sha, reverted=1)  # rolled back, the fix card is on its way
    env.board(_card("t_work", "todo"), _card("t_merge", "blocked"))

    report = env.go()

    assert report.clean and env.world.calls == []


def test_c_a_complete_record_that_names_no_commit_and_is_not_a_no_op_is_blocked(env):
    env.task()
    _record(env.conn, candidate="abc", gate3="pass", squash=None)
    env.board(_card("t_work", "done"), _card("t_merge", "blocked"))

    report = env.go()

    assert _kinds(report.blocked) == ["merge_record_without_done_card"] and env.world.calls == []


# -- d. a candidate without a verdict / an unfinished merge -------------------------------------------


def test_d_a_candidate_that_landed_gets_its_record_finished_and_its_card_completed(env):
    env.task()
    sha = _land(env.repo)
    _record(env.conn, candidate=sha, gate3="pass", squash=None, completed=None)
    env.board(_card("t_work", "done"), _card("t_merge", "blocked"))

    report = env.go()

    row = _merge_row(env.conn)
    assert (row["candidate_sha"], row["gate3_result"], row["squash_commit"]) == (sha, "pass", sha)
    assert row["completed_at"]
    assert env.world.calls == [
        ("complete", "t_merge", f"merged {sha} (recovered)", {"squash_commit": sha, "recovered": True})]
    assert _kinds(report.findings) == ["merge_unfinished"] and report.blocked == []
    assert [(r.kind, r.applied) for r in report.repairs] == [
        ("merge_record_recovered", True), ("merge_card_completed", True)]
    assert env.go().clean


def test_d_a_candidate_that_never_landed_is_left_for_the_merge_queue_and_is_informational(env):
    env.task()
    _record(env.conn, candidate="cafe" * 10, gate3="pass", squash=None, completed=None)
    env.board(_card("t_work", "done"), _card("t_merge", "blocked"))
    before = tuple(_merge_row(env.conn))

    report = env.go()

    assert report.clean and report.blocked == []  # informational: it is not an inconsistency and blocks nothing
    assert [(r.task_key, r.kind, r.applied) for r in report.repairs] == [("T1", "candidate_discarded", False)]
    assert tuple(_merge_row(env.conn)) == before  # the row is left as it was
    assert env.world.calls == [] and _repair_events(env.conn) == []


def test_d_a_landed_commit_with_neither_the_record_nor_the_card_completes_both(env):
    """The ASES database was lost (or the crash came before the record was ever written) but git proves the
    fast-forward: the record is written from git and the card completed."""
    env.task()
    sha = _land(env.repo)
    env.board(_card("t_work", "done"), _card("t_merge", "blocked"))

    report = env.go()

    row = _merge_row(env.conn)
    assert (row["candidate_sha"], row["squash_commit"], row["gate3_result"]) == (sha, sha, "recovered")
    assert [c[:2] for c in env.world.calls] == [("complete", "t_merge")]
    assert _kinds(report.findings) == ["merge_unfinished"] and report.blocked == []
    assert env.go().clean


def test_d_a_pending_merge_whose_work_card_is_not_done_never_asks_git_about_a_landing(env, monkeypatch):
    env.task()
    env.board(_card("t_work", "ready"), _card("t_merge", "blocked"))
    asked = []
    real = reconcile._git
    monkeypatch.setattr(reconcile, "_git", lambda repo, args: (asked.append(args[0]), real(repo, args))[1])

    report = env.go()

    assert report.clean and "log" not in asked and "merge-base" not in asked


def test_d_the_recorded_squash_commit_counts_as_landed_even_without_the_message_line(env):
    env.task()
    (env.repo / "plain.txt").write_text("plain\n", encoding="utf-8")
    _git_ok("add", "-A", cwd=env.repo)
    _git_ok("commit", "-q", "-m", "a squash commit with no card ids in it", cwd=env.repo)
    sha = _head(env.repo)
    _record(env.conn, candidate=sha, gate3="pass", squash=sha, completed=None)  # squash_commit set, completed_at not
    env.board(_card("t_work", "done"), _card("t_merge", "blocked"))

    report = env.go()

    assert _merge_row(env.conn)["completed_at"] and _merge_row(env.conn)["squash_commit"] == sha
    assert [r.kind for r in report.repairs] == ["merge_record_recovered", "merge_card_completed"]


def test_d_a_landed_candidate_that_was_reverted_is_blocked_not_recovered(env):
    env.task()
    sha = _land(env.repo)
    _git_ok("revert", "--no-edit", sha, cwd=env.repo)
    _record(env.conn, candidate=sha, gate3="pass", squash=None, completed=None)
    env.board(_card("t_work", "done"), _card("t_merge", "blocked"))

    report = env.go()

    assert _kinds(report.blocked) == ["merge_unfinished"] and env.world.calls == []
    assert _merge_row(env.conn)["completed_at"] is None


def test_d_a_landed_candidate_whose_card_is_somewhere_unexpected_is_blocked(env):
    env.task()
    sha = _land(env.repo)
    _record(env.conn, candidate=sha, gate3="pass", squash=None, completed=None)
    env.board(_card("t_work", "done"), _card("t_merge", "review"))

    report = env.go()

    assert _kinds(report.blocked) == ["merge_unfinished"] and env.world.calls == []
    assert _merge_row(env.conn)["completed_at"] is None


# -- e. open intents ------------------------------------------------------------------------------------


def _intent_row(conn, intent_id):
    return conn.execute("SELECT * FROM intents WHERE id = ?", (intent_id,)).fetchone()


def test_e_an_open_intent_of_a_consistent_task_is_closed_as_recovered(env):
    env.task()
    env.board(_card("t_work", "done"), _card("t_merge", "blocked"))
    iid = intents.begin(env.conn, "p1", intents.KIND_RUN_GATE, "T1", "gate3 on abc")

    report = env.go()

    row = _intent_row(env.conn, iid)
    assert row["completed_at"] and row["detail"] == "gate3 on abc | reconcile-on-start: state consistent"
    assert [(r.task_key, r.kind, r.applied) for r in report.repairs] == [("T1", "intent_recovered", True)]
    assert report.clean and report.blocked == []  # closing an intent is not an inconsistency
    assert [e["kind"] for e in _repair_events(env.conn)] == ["intent_recovered"]
    assert intents.open_intents(env.conn, "p1") == []


def test_e_an_intent_whose_task_was_repaired_is_recovered_and_says_what_was_repaired(env):
    env.task()
    sha = _land(env.repo)
    _record(env.conn, candidate=sha, squash=sha)
    env.board(_card("t_work", "done"), _card("t_merge", "blocked"))
    iid = intents.begin(env.conn, "p1", intents.KIND_FAST_FORWARD, "T1")

    report = env.go()

    assert [r.kind for r in report.repairs] == ["merge_card_completed", "intent_recovered"]
    assert "repaired merge_card_completed" in _intent_row(env.conn, iid)["detail"]
    assert _intent_row(env.conn, iid)["completed_at"]


def test_e_an_intent_whose_task_could_not_be_settled_stays_open_and_is_blocked(env):
    env.task()
    env.board(_card("t_work", "done"), _card("t_merge", "done"))  # done merge, no record, no commit: unrepairable
    iid = intents.begin(env.conn, "p1", intents.KIND_COMPLETE_MERGE_CARD, "T1")

    report = env.go()

    assert _kinds(report.blocked) == ["merge_done_without_record", "open_intent"]
    detail = report.blocked[1].detail
    assert f"#{iid}" in detail and "complete_merge_card" in detail and "merge_done_without_record" in detail
    assert _intent_row(env.conn, iid)["completed_at"] is None
    assert not any(r.kind == "intent_recovered" for r in report.repairs)


def test_e_an_unfinished_card_creation_stays_open_until_it_has_been_repeated(env):
    """Reconcile creates nothing (card creation is idempotent, ASES-REC-03: repeating it is the repair), so an
    open create_cards intent for a task with no complete plan_tasks row is blocked with that instruction."""
    env.task("T1")  # T2 is in the plan but its plan_tasks row was never written: the crash was mid-creation
    env.board(_card("t_work", "ready"), _card("t_merge", "blocked"))
    iid = intents.begin(env.conn, "p1", intents.KIND_CREATE_CARDS, "T2")

    report = env.go(_plan("T1", "T2"))

    assert _kinds(report.blocked) == ["open_intent"]
    assert "T2" in report.blocked[0].detail and "idempotent" in report.blocked[0].detail
    assert _intent_row(env.conn, iid)["completed_at"] is None

    env.task("T2", work="u_work", merge="u_merge")  # the creation is repeated and completes
    env.world.cards["u_work"] = _card("u_work", "ready")
    env.world.cards["u_merge"] = _card("u_merge", "blocked")
    again = env.go(_plan("T1", "T2"))

    assert again.blocked == [] and _intent_row(env.conn, iid)["completed_at"]


def test_e_a_project_level_intent_covers_every_task_of_the_plan(env):
    env.task("T1")
    env.task("T2", work="u_work", merge="u_merge")
    env.board(_card("t_work", "ready"), _card("t_merge", "blocked"), _card("u_work", "done"), _card("u_merge", "done"))
    creating = intents.begin(env.conn, "p1", intents.KIND_CREATE_CARDS, "p1")   # key: the project, not a task
    gating = intents.begin(env.conn, "p1", intents.KIND_RUN_GATE, "p1")

    report = env.go(_plan("T1", "T2"))  # T2 is a done merge with no record and no commit: blocked

    assert _intent_row(env.conn, creating)["completed_at"]  # both tasks have both cards
    assert _intent_row(env.conn, gating)["completed_at"] is None  # T2 is unresolved, so the whole plan is
    assert _kinds(report.blocked) == ["merge_done_without_record", "open_intent"]


def _reverted_world(env, *, reverted_flag):
    env.task()
    sha = _land(env.repo)
    _git_ok("revert", "--no-edit", sha, cwd=env.repo)
    revert_sha = _head(env.repo)
    _record(env.conn, candidate=sha, squash=sha, reverted=reverted_flag)
    env.board(_card("t_work", "todo"), _card("t_merge", "blocked"))  # the fix card is on its way
    return sha, revert_sha


def test_e_a_revert_that_is_in_git_but_was_never_recorded_is_recorded(env):
    sha, revert_sha = _reverted_world(env, reverted_flag=0)
    iid = intents.begin(env.conn, "p1", intents.KIND_REVERT, "T1", f"revert {sha[:12]}")

    report = env.go()

    assert _merge_row(env.conn)["reverted"] == 1
    assert _kinds(report.findings) == ["revert_unrecorded"] and report.blocked == []
    assert [r.kind for r in report.repairs] == ["revert_recorded", "intent_recovered"]
    assert revert_sha[:12] in report.repairs[0].detail
    assert _intent_row(env.conn, iid)["completed_at"]
    assert env.world.calls == []  # the merge step did not complete a card for a merge that was rolled back
    assert env.go().clean


def test_e_a_revert_that_was_already_recorded_just_closes_the_intent(env):
    _reverted_world(env, reverted_flag=1)
    iid = intents.begin(env.conn, "p1", intents.KIND_REVERT, "T1")

    report = env.go()

    assert report.clean and [r.kind for r in report.repairs] == ["intent_recovered"]
    assert _intent_row(env.conn, iid)["completed_at"]


def test_e_a_revert_that_never_reached_git_is_blocked_because_the_merge_still_stands(env):
    env.task()
    sha = _land(env.repo)
    _record(env.conn, candidate=sha, squash=sha)
    env.board(_card("t_work", "done"), _card("t_merge", "done"))
    iid = intents.begin(env.conn, "p1", intents.KIND_REVERT, "T1")

    report = env.go()

    assert _kinds(report.blocked) == ["revert_unfinished", "open_intent"]
    assert "still stands" in report.blocked[0].detail and sha[:12] in report.blocked[0].detail
    assert _merge_row(env.conn)["reverted"] == 0 and _intent_row(env.conn, iid)["completed_at"] is None


def test_e_a_revert_intent_with_no_merge_record_is_blocked(env):
    env.task()
    env.board(_card("t_work", "done"), _card("t_merge", "blocked"))
    intents.begin(env.conn, "p1", intents.KIND_REVERT, "T1")

    report = env.go()

    assert _kinds(report.blocked) == ["revert_unfinished", "open_intent"]


# -- f. running cards whose worker is gone --------------------------------------------------------------


def _running_work(env, *runs, **extra):
    workspace = env.tmp / "wt"
    workspace.mkdir(exist_ok=True)
    env.task()
    return env.board(_card("t_work", "running", runs=runs, workspace_path=str(workspace), **extra),
                     _card("t_merge", "blocked"))


def test_f_a_running_card_whose_worker_is_gone_is_reclaimed(env):
    _running_work(env, _run(4242))

    report = env.go()

    assert env.world.calls == [("reclaim", "t_work", "worker process gone (reconcile-on-start)")]
    assert _kinds(report.findings) == ["worker_gone"] and report.blocked == []
    assert [(r.task_key, r.kind, r.applied) for r in report.repairs] == [("T1", "worker_gone_reclaimed", True)]
    assert _repair_events(env.conn)[0]["kind"] == "worker_gone_reclaimed"
    assert env.go().clean and len(env.world.calls) == 1  # reclaimed once, it is ready now


def test_f_a_running_card_with_a_live_worker_is_not_touched(env):
    _running_work(env, _run(4242))
    env.world.alive_pids.add(4242)

    report = env.go()

    assert report.clean and env.world.calls == [] and env.world.killed == []


def test_f_a_running_card_that_records_no_pid_at_all_is_blocked_because_nobody_can_tell(env):
    _running_work(env, _run(None))

    report = env.go()

    assert _kinds(report.blocked) == ["running_without_pid"] and env.world.calls == []


def test_f_an_ended_run_does_not_count_as_the_cards_worker(env):
    _running_work(env, _run(4242, ended=True))  # the only run has ended, and no other pid is recorded
    env.world.alive_pids.add(4242)

    report = env.go()

    assert _kinds(report.blocked) == ["running_without_pid"] and env.world.killed == []


def test_f_the_cards_own_worker_pid_is_used_when_no_run_carries_one(env):
    _running_work(env, worker_pid=4242)

    report = env.go()

    assert [r.kind for r in report.repairs] == ["worker_gone_reclaimed"]


def test_f_a_failing_reclaim_is_an_error_for_that_task_and_no_repair(env):
    world = _running_work(env, _run(4242))
    world.fail_reclaim["t_work"] = hermes.HermesCommandError(["kanban", "reclaim"], 1, "refused")

    report = env.go()

    assert _kinds(report.findings) == ["worker_gone", "reconcile_error"]
    assert _kinds(report.blocked) == ["reconcile_error"] and report.repairs == []
    assert _repair_events(env.conn) == [] and world.calls == []


# -- g. orphan workers ----------------------------------------------------------------------------------

WORKER_COMMAND = 'python -m hermes_cli.main -p coder-1 --cli chat -q "work kanban task t_work"'


def _orphaned(env, status="done", *, runs=None, command=WORKER_COMMAND, pid=4242):
    env.task()
    world = env.board(_card("t_work", status, runs=[_run(pid, ended=True)] if runs is None else runs),
                      _card("t_merge", "blocked"))
    world.alive_pids.add(pid)
    world.commands[pid] = command
    return world


@pytest.mark.parametrize("status", ["done", "blocked", "ready", "todo", "review", "scheduled", "triage", "archived"])
def test_g_an_orphan_worker_of_a_card_that_is_not_running_is_terminated(env, status):
    _orphaned(env, status)

    report = env.go()

    assert env.world.killed == [4242]
    assert _kinds(report.findings) == ["orphan_worker"] and report.blocked == []
    assert [(r.task_key, r.kind, r.applied) for r in report.repairs] == [("T1", "orphan_worker_terminated", True)]
    assert [e["kind"] for e in _repair_events(env.conn)] == ["orphan_worker_terminated"]
    assert "t_work" in report.repairs[0].detail and "4242" in report.repairs[0].detail
    assert env.go().clean and env.world.killed == [4242]  # dead now: not killed twice


@pytest.mark.parametrize("command", [
    'python -m hermes_cli.main -p coder-1 --cli chat -q "work kanban task t_other"',       # another card
    'python -m hermes_cli.main -p coder-1 --cli chat -q "work kanban task t_work2"',       # an id that only starts with ours
    'python -m hermes_cli.main -p coder-1 --cli chat -q "work kanban task x_t_work"',      # an id that only ends with ours
    'python -m hermes_cli.main -p default --cli chat',                                     # an unrelated hermes session
    "",
])
def test_g_a_process_whose_command_line_does_not_name_the_card_is_never_terminated(env, command):
    _orphaned(env, command=command)

    report = env.go()

    assert env.world.killed == [] and report.clean and report.repairs == []


def test_g_a_process_whose_command_line_cannot_be_read_is_never_terminated(env):
    _orphaned(env, command=None)

    report = env.go()

    assert env.world.killed == [] and report.clean and report.repairs == []


def test_g_a_worker_that_is_already_dead_is_not_even_looked_at(env):
    world = _orphaned(env)
    world.alive_pids.clear()

    def boom(pid):
        raise AssertionError("the command line of a dead process must not be read")

    world.command_line = boom

    assert env.go().clean and world.killed == []


def test_g_only_the_latest_runs_worker_counts(env):
    _orphaned(env, runs=[_run(111, ended=True, run_id=1), _run(222, ended=True, run_id=2)], pid=111)
    env.world.alive_pids.add(111)   # the OLD run's process is alive and names the card; the latest run's is not
    env.world.commands[111] = WORKER_COMMAND

    assert env.go().clean and env.world.killed == []


def test_g_a_card_in_review_whose_latest_run_is_still_open_may_be_held_by_a_reviewer(env):
    _orphaned(env, "review", runs=[_run(4242, ended=False, profile="reviewer")])

    report = env.go()

    assert report.clean and env.world.killed == []


def test_g_a_worker_that_will_not_die_is_a_blocked_finding_not_a_repair(env):
    _orphaned(env)
    env.world.kill_ok = False

    report = env.go()

    assert env.world.killed == [4242]
    assert _kinds(report.blocked) == ["orphan_worker"] and "could not be terminated" in report.blocked[0].detail
    assert report.repairs == [] and _repair_events(env.conn) == []


@pytest.mark.parametrize("which", [os.getpid, os.getppid])
def test_g_the_controller_and_its_parent_are_never_terminated(env, which):
    _orphaned(env, pid=which(), runs=[_run(which(), ended=True)])

    report = env.go()

    assert env.world.killed == [] and report.clean


def test_g_a_running_cards_live_worker_is_not_an_orphan(env):
    _running_work(env, _run(4242))
    env.world.alive_pids.add(4242)
    env.world.commands[4242] = WORKER_COMMAND

    report = env.go()

    assert report.clean and env.world.killed == []


# -- h. worktrees without cards, cards without worktrees -------------------------------------------------


def _add_worktree(repo, name, *, under=".worktrees"):
    path = repo / under / name
    _git_ok("worktree", "add", "-q", "-b", f"swarm/{name}", str(path), "integration", cwd=repo)
    return path


def test_h_a_worktree_of_an_archived_card_is_reported_and_left_in_place(env):
    env.task()
    path = _add_worktree(env.repo, "t_work")
    env.board(_card("t_work", "archived"), _card("t_merge", "blocked"))

    report = env.go()

    assert [(f.task_key, f.kind) for f in report.findings] == [("T1", "orphan_worktree")]
    assert report.blocked == [] and report.repairs == []  # reported, not blocked, and never removed
    assert "t_work" in report.findings[0].detail and "archived" in report.findings[0].detail
    assert path.exists()


def test_h_a_worktree_of_a_card_that_no_longer_resolves_is_reported_too(env):
    env.task()
    _add_worktree(env.repo, "t_work")
    env.board(_card("t_merge", "blocked"))  # t_work is not on the board any more

    report = env.go()

    assert sorted(_kinds(report.findings)) == ["missing_card", "orphan_worktree"]
    assert _kinds(report.blocked) == ["missing_card"]  # the worktree is informational, the missing card is not


def test_h_a_worktree_of_a_live_card_is_fine(env):
    env.task()
    _add_worktree(env.repo, "t_work")
    env.board(_card("t_work", "ready"), _card("t_merge", "blocked"))

    assert env.go().clean


def test_h_worktrees_that_belong_to_no_card_of_this_plan_are_ignored(env):
    env.task()
    _add_worktree(env.repo, "t_stranger")                         # under .worktrees, but not a card of this plan
    _add_worktree(env.repo, "t_work", under="elsewhere")           # named like a card, but not under .worktrees
    env.board(_card("t_work", "archived"), _card("t_merge", "blocked"))

    assert env.go().clean


def test_h_a_running_card_whose_workspace_does_not_exist_is_blocked(env):
    _running_work(env, _run(4242))
    env.world.cards["t_work"]["workspace_path"] = str(env.tmp / "gone")
    env.world.alive_pids.add(4242)

    report = env.go()

    assert _kinds(report.blocked) == ["missing_worktree"] and "gone" in report.blocked[0].detail
    assert env.world.calls == [] and report.repairs == []


def test_h_a_running_card_with_its_workspace_or_with_none_recorded_is_fine(env):
    _running_work(env, _run(4242))
    env.world.alive_pids.add(4242)
    assert env.go().clean                                          # the workspace exists on disk
    del env.world.cards["t_work"]["workspace_path"]
    assert env.go().clean                                          # nothing recorded: nothing to check


def test_h_a_card_that_is_being_reclaimed_anyway_is_not_also_reported_as_missing_its_worktree(env):
    _running_work(env, _run(4242))
    env.world.cards["t_work"]["workspace_path"] = str(env.tmp / "gone")  # and its worker is dead

    report = env.go()

    assert _kinds(report.findings) == ["worker_gone"] and report.blocked == []


def test_e_a_card_recorded_for_the_plan_that_no_longer_resolves_keeps_the_creation_intent_open(env):
    env.task("T1")
    env.board(_card("t_merge", "blocked"))  # t_work was deleted from the board
    iid = intents.begin(env.conn, "p1", intents.KIND_CREATE_CARDS, "T1")

    report = env.go()

    assert _kinds(report.blocked) == ["missing_card", "open_intent"]
    assert "no longer resolves" in report.blocked[1].detail and "idempotent" in report.blocked[1].detail
    assert _intent_row(env.conn, iid)["completed_at"] is None


# =============================================================================================
# Section 22.7, the crash recovery test: "Kill the controller with SIGKILL during a running card, again during a
# candidate build, and again between the fast-forward and the merge-card completion. After each restart: no
# duplicate cards, no orphan workers, no half-merged state, the ledger is intact, and every repair is logged."
# Each scenario below is the state such a kill leaves behind, and what the restart's reconcile makes of it.
# =============================================================================================


def _seed_ledger(conn):
    conn.execute("INSERT INTO requests_ledger (provider, model, utc_date, count, updated_at) "
                 "VALUES ('prov', 'm', '2026-09-19', 17, '2026-09-19T00:00:00+00:00')")
    return _ledger(conn)


def _ledger(conn):
    return [tuple(r) for r in conn.execute("SELECT * FROM requests_ledger ORDER BY provider, model, utc_date")]


def _assert_restart_is_clean(env, report, ledger_before, *, plan=None, still_alive=frozenset()):
    """The five things 22.7 asks for after a restart."""
    world = env.world
    assert world.forbidden == []                                      # no duplicate cards: nothing was created
    assert world.alive_pids == set(still_alive)                       # no orphan workers (unrelated ones survive)
    assert reconcile.check("b", "p1", conn=env.conn) == []            # no half-merged state
    assert _ledger(env.conn) == ledger_before                         # the ledger is intact
    applied = [(r.task_key, r.kind) for r in report.repairs if r.applied]
    assert [(e["task_key"], e["kind"]) for e in _repair_events(env.conn)] == applied   # every repair is logged, once
    events_before, calls_before = len(_repair_events(env.conn)), len(world.calls)
    again = env.go(plan)
    assert again.clean and not any(r.applied for r in again.repairs)  # and a second restart has nothing left to do
    assert len(_repair_events(env.conn)) == events_before and len(world.calls) == calls_before


def test_crash_a_kill_during_a_running_card(env):
    env.task("T1")
    env.task("T2", work="u_work", merge="u_merge")
    workspace = env.tmp / "wt"
    workspace.mkdir()
    world = env.board(
        _card("t_work", "running", runs=[_run(4242)], workspace_path=str(workspace)), _card("t_merge", "blocked"),
        _card("u_work", "done", runs=[_run(5151, ended=True)]), _card("u_merge", "blocked"),
    )
    world.alive_pids.update({5151, 6161})          # 5151 is T2's orphan; 6161 is the user's own unrelated session
    world.commands[5151] = 'python -m hermes_cli.main -p coder-1 --cli chat -q "work kanban task u_work"'
    world.commands[6161] = 'python -m hermes_cli.main -p default --cli chat'
    ledger = _seed_ledger(env.conn)

    report = env.go(_plan("T1", "T2"))

    assert world.calls == [("reclaim", "t_work", "worker process gone (reconcile-on-start)")]  # T1's dead worker
    assert world.killed == [5151]                                                              # T2's orphan only
    assert [(r.task_key, r.kind) for r in report.repairs] == [
        ("T1", "worker_gone_reclaimed"), ("T2", "orphan_worker_terminated")]
    _assert_restart_is_clean(env, report, ledger, plan=_plan("T1", "T2"), still_alive={6161})


@pytest.mark.parametrize("row_written", [True, False])
def test_crash_b_kill_during_a_candidate_build(env, row_written):
    """Gate 3 writes the candidate row only after it ran, so a kill during the build leaves no row, only the open
    intent (row_written False); a kill after Gate 3 leaves a candidate row that never landed (True)."""
    env.task()
    if row_written:
        _record(env.conn, candidate="cafe" * 10, gate3="pass", squash=None, completed=None)
    env.board(_card("t_work", "done"), _card("t_merge", "blocked"))
    build = intents.begin(env.conn, "p1", intents.KIND_BUILD_CANDIDATE, "T1")
    gate = intents.begin(env.conn, "p1", intents.KIND_RUN_GATE, "T1")
    ledger = _seed_ledger(env.conn)
    row_before = tuple(_merge_row(env.conn)) if row_written else None
    head_before = _head(env.repo)

    report = env.go()

    expected = ([("candidate_discarded", False)] if row_written else []) + [("intent_recovered", True)] * 2
    assert [(r.kind, r.applied) for r in report.repairs] == expected
    assert report.clean and report.blocked == []
    assert (tuple(_merge_row(env.conn)) if row_written else None) == row_before   # the merge queue redoes it
    assert env.world.calls == [] and _head(env.repo) == head_before               # nothing was merged or completed
    assert all(_intent_row(env.conn, i)["completed_at"] for i in (build, gate))
    _assert_restart_is_clean(env, report, ledger)


@pytest.mark.parametrize("record", ["complete", "incomplete", "missing"])
def test_crash_c_kill_between_the_fast_forward_and_the_merge_card_completion(env, record):
    """The commit is on the integration branch. Depending on how far the merge queue got, the record is complete
    (killed just before complete_merge_card), never completed (killed just after the fast-forward), or absent."""
    env.task()
    sha = _land(env.repo)
    if record == "complete":
        _record(env.conn, candidate=sha, squash=sha)
    elif record == "incomplete":
        _record(env.conn, candidate=sha, gate3="pass", squash=None, completed=None)
    env.board(_card("t_work", "done"), _card("t_merge", "blocked"))
    forward = intents.begin(env.conn, "p1", intents.KIND_FAST_FORWARD, "T1")
    completing = intents.begin(env.conn, "p1", intents.KIND_COMPLETE_MERGE_CARD, "T1")
    ledger = _seed_ledger(env.conn)
    commits_before = _git_ok("rev-list", "--count", "integration", cwd=env.repo).stdout.strip()

    report = env.go()

    row = _merge_row(env.conn)
    assert row["squash_commit"] == sha and row["completed_at"] and row["reverted"] == 0
    assert env.world.calls == [
        ("complete", "t_merge", f"merged {sha} (recovered)", {"squash_commit": sha, "recovered": True})]
    assert report.blocked == []
    assert all(_intent_row(env.conn, i)["completed_at"] for i in (forward, completing))
    # no half-merged state and no second merge: integration still ends at that one commit and is clean
    assert _head(env.repo) == sha
    assert _git_ok("rev-list", "--count", "integration", cwd=env.repo).stdout.strip() == commits_before
    assert _git_ok("status", "--porcelain", cwd=env.repo).stdout.strip() == ""
    _assert_restart_is_clean(env, report, ledger)


# =============================================================================================
# apply=False, idempotence, failure isolation, ASCII safety, defaults
# =============================================================================================


def _messy(env):
    """Four tasks that each need one kind of repair, plus one open intent."""
    for key, work, merge in (("T1", "a_work", "a_merge"), ("T2", "b_work", "b_merge"),
                             ("T3", "c_work", "c_merge"), ("T4", "d_work", "d_merge")):
        env.task(key, work=work, merge=merge)
    sha1 = _land(env.repo, "T1", merge="a_merge", work="a_work")
    _record(env.conn, "T1", candidate=sha1, squash=sha1)                    # record, card not done
    _land(env.repo, "T4", merge="d_merge", work="d_work")                   # card done, no record, commit landed
    workspace = env.tmp / "wt"
    workspace.mkdir()
    world = env.board(
        _card("a_work", "done"), _card("a_merge", "blocked"),
        _card("b_work", "running", runs=[_run(4242)], workspace_path=str(workspace)), _card("b_merge", "blocked"),
        _card("c_work", "done", runs=[_run(5151, ended=True)]), _card("c_merge", "blocked"),
        _card("d_work", "done"), _card("d_merge", "done"),
    )
    world.alive_pids.add(5151)
    world.commands[5151] = 'python -m hermes_cli.main chat -q "work kanban task c_work"'
    intents.begin(env.conn, "p1", intents.KIND_FAST_FORWARD, "T1")
    return _plan("T1", "T2", "T3", "T4")


def _snapshot(conn):
    tables = ("merge_records", "intents", "events", "plan_tasks", "requests_ledger", "gate_runs")
    return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY rowid")] for t in tables}


EXPECTED_MESSY_REPAIRS = [
    ("T1", "merge_card_completed"), ("T2", "worker_gone_reclaimed"), ("T3", "orphan_worker_terminated"),
    ("T4", "merge_record_recovered"), ("T1", "intent_recovered"),
]


def test_apply_false_changes_nothing_and_reports_what_would_be_repaired(env):
    plan = _messy(env)
    before, changes = _snapshot(env.conn), env.conn.total_changes

    dry = env.go(plan, apply=False)

    assert env.world.calls == [] and env.world.killed == []                # no hermes mutation, no kill
    assert _snapshot(env.conn) == before and env.conn.total_changes == changes   # no DB write, not even an event
    assert [(r.task_key, r.kind) for r in dry.repairs] == EXPECTED_MESSY_REPAIRS
    assert not any(r.applied for r in dry.repairs) and dry.blocked == []
    assert len(intents.open_intents(env.conn, "p1")) == 1

    wet = env.go(plan)  # and a real run then does exactly what the dry run said it would

    assert [(r.task_key, r.kind) for r in wet.repairs] == EXPECTED_MESSY_REPAIRS and all(r.applied for r in wet.repairs)
    assert [(f.task_key, f.kind) for f in wet.findings] == [(f.task_key, f.kind) for f in dry.findings]
    assert env.world.killed == [5151] and [c[0] for c in env.world.calls] == ["complete", "reclaim"]


def test_a_second_run_after_applying_finds_nothing_new(env):
    plan = _messy(env)
    first = env.go(plan)
    events_after_first, calls_after_first = len(_repair_events(env.conn)), len(env.world.calls)
    snapshot = _snapshot(env.conn)

    second = env.go(plan)

    assert len(first.repairs) == len(EXPECTED_MESSY_REPAIRS) and events_after_first == len(EXPECTED_MESSY_REPAIRS)
    assert second.clean and second.repairs == [] and second.blocked == []
    assert len(_repair_events(env.conn)) == events_after_first and len(env.world.calls) == calls_after_first
    assert _snapshot(env.conn) == snapshot and env.world.killed == [5151]


def test_every_applied_repair_is_logged_exactly_once_with_task_kind_and_detail(env):
    plan = _messy(env)

    report = env.go(plan)

    assert _repair_events(env.conn) == [
        {"task_key": r.task_key, "kind": r.kind, "detail": r.detail} for r in report.repairs]


@pytest.mark.parametrize("error", [
    RuntimeError("boom"), subprocess.TimeoutExpired("hermes", 30), ValueError("not json"),
    hermes.HermesNotFound("hermes is not on PATH"),
])
def test_one_failing_hermes_call_does_not_stop_the_other_tasks(env, error):
    env.task("T1")
    env.task("T2", work="u_work", merge="u_merge")
    sha = _land(env.repo, "T2", merge="u_merge", work="u_work")
    _record(env.conn, "T2", candidate=sha, squash=sha)
    world = env.board(_card("t_work", "ready"), _card("t_merge", "blocked"),
                      _card("u_work", "done"), _card("u_merge", "blocked"))
    world.fail_show["t_work"] = error

    report = env.go(_plan("T1", "T2"))

    assert [(f.task_key, f.kind) for f in report.blocked] == [("T1", "reconcile_error")]
    assert type(error).__name__ in report.blocked[0].detail
    assert [(r.task_key, r.kind, r.applied) for r in report.repairs] == [("T2", "merge_card_completed", True)]
    assert report.blocked[0] in report.findings


def test_a_failing_card_completion_keeps_the_repair_that_already_happened_and_the_next_run_finishes(env):
    env.task("T1")
    env.task("T2", work="u_work", merge="u_merge")
    sha = _land(env.repo, "T1")
    sha2 = _land(env.repo, "T2", merge="u_merge", work="u_work")
    _record(env.conn, "T1", candidate=sha, gate3="pass", squash=None, completed=None)
    _record(env.conn, "T2", candidate=sha2, squash=sha2)
    world = env.board(_card("t_work", "done"), _card("t_merge", "blocked"),
                      _card("u_work", "done"), _card("u_merge", "blocked"))
    world.fail_complete["t_merge"] = hermes.HermesCommandError(["kanban", "complete"], 1, "refused")

    report = env.go(_plan("T1", "T2"))

    assert [(r.task_key, r.kind) for r in report.repairs] == [
        ("T1", "merge_record_recovered"), ("T2", "merge_card_completed")]   # T1's record write DID happen
    assert [(f.task_key, f.kind) for f in report.blocked] == [("T1", "reconcile_error")]
    assert _merge_row(env.conn, "T1")["completed_at"] and world.cards["u_merge"]["status"] == "done"

    del world.fail_complete["t_merge"]
    again = env.go(_plan("T1", "T2"))   # the record is complete and the card is not: the ordinary c repair

    assert [(r.task_key, r.kind) for r in again.repairs] == [("T1", "merge_card_completed")]
    assert again.blocked == [] and env.go(_plan("T1", "T2")).clean


def test_a_git_that_cannot_answer_is_an_error_for_the_tasks_that_need_it_and_not_a_guess(env):
    not_a_repo = env.tmp / "not-a-repo"
    not_a_repo.mkdir()
    env.task("T1")
    env.task("T2", work="u_work", merge="u_merge")
    workspace = env.tmp / "wt"
    workspace.mkdir()
    world = env.board(_card("t_work", "done"), _card("t_merge", "done"),      # needs git: is a commit there?
                      _card("u_work", "running", runs=[_run(4242)], workspace_path=str(workspace)),
                      _card("u_merge", "blocked"))                             # needs no git: worker is gone

    report = world.go(not_a_repo, env.conn, _plan("T1", "T2"))

    assert [(f.task_key, f.kind) for f in report.blocked] == [
        ("T1", "reconcile_error"), ("*", "reconcile_error")]                   # the task, and `git worktree list`
    assert _merge_row(env.conn) is None                                        # nothing written on a guess
    assert [(r.task_key, r.kind) for r in report.repairs] == [("T2", "worker_gone_reclaimed")]


def test_blocked_is_always_a_subset_of_findings_and_says_why(env):
    env.task("T1")
    env.task("T2", work="u_work", merge="u_merge")
    env.task("T3", work="v_work", merge="v_merge")
    _record(env.conn, "T3", squash="0" * 40, reverted=1)
    env.board(_card("t_merge", "blocked"),                                         # T1: t_work is missing
              _card("u_work", "running", runs=[_run(None)]), _card("u_merge", "blocked"),   # T2: no worker pid
              _card("v_work", "done"), _card("v_merge", "done"))                    # T3: done but reverted

    report = env.go(_plan("T1", "T2", "T3"))

    assert sorted(_kinds(report.blocked)) == ["done_but_reverted", "missing_card", "running_without_pid"]
    assert all(b in report.findings for b in report.blocked)
    assert not report.clean and report.repairs == []


def test_everything_a_person_reads_is_ascii(env):
    env.task("T1")
    env.task("T2", work="u_work", merge="u_merge")
    world = env.board(
        _card("t_work", "running", runs=[_run(4242)], workspace_path="C:/work/caf\u00e9"),
        _card("t_merge", "blocked"), _card("u_work", "ready"), _card("u_merge", "blocked"))
    world.alive_pids.add(4242)
    world.fail_show["u_work"] = RuntimeError("could not read caf\u00e9 \u2192 card")

    report = env.go(_plan("T1", "T2"))

    assert [(f.task_key, f.kind) for f in report.blocked] == [("T1", "missing_worktree"), ("T2", "reconcile_error")]
    assert "caf\\xe9" in report.blocked[0].detail
    assert "\\u2192" in report.blocked[1].detail
    for item in (*report.findings, *report.blocked):
        assert item.detail.isascii() and item.kind.isascii() and item.task_key.isascii()


def test_a_finding_about_no_single_task_is_keyed_to_the_project_marker(env):
    env.task()
    env.board(_card("t_work", "ready"), _card("t_merge", "blocked"))

    report = env.world.go(env.tmp / "missing-dir", env.conn)

    assert [(f.task_key, f.kind) for f in report.blocked] == [("*", "reconcile_error")]


def test_reconcile_defaults_to_the_real_process_helpers():
    import inspect

    params = inspect.signature(reconcile.reconcile).parameters
    assert params["alive"].default is reconcile.pid_alive
    assert params["killer"].default is reconcile.terminate_tree
    assert params["command_line"].default is reconcile.process_command_line
    assert params["apply"].default is True
    assert [p for p, v in params.items() if v.kind is inspect.Parameter.KEYWORD_ONLY] == [
        "conn", "apply", "alive", "killer", "command_line"]


# =============================================================================================
# The real writer. Every scenario above builds its commits and rows by hand; these use mergeq.merge_task itself, so
# what reconcile recognises is what the merge queue really writes (the squash commit's message, the merge_records row).
# =============================================================================================

CONTROLLER_MESSAGE = ("T1: task\n\nWork card: t_work\nMerge card: t_merge\nBranch: swarm/T1-coder\n"
                      "Controlled by ASES (one squash commit per plan task).")


@pytest.mark.parametrize("crash", ["before_card_completion", "before_record_update"])
def test_a_real_squash_merge_is_recovered_after_a_crash_following_the_fast_forward(env, crash):
    from ases import mergeq

    env.task()
    _git_ok("checkout", "-q", "-b", "swarm/T1-coder", cwd=env.repo)
    (env.repo / "feature.txt").write_text("feature\n", encoding="utf-8")
    _git_ok("add", "-A", cwd=env.repo)
    _git_ok("commit", "-q", "-m", "add feature", cwd=env.repo)
    _git_ok("checkout", "-q", "integration", cwd=env.repo)
    outcome = mergeq.merge_task(env.repo, "integration", "swarm/T1-coder", "T1", ["echo ok"], conn=env.conn,
                                commit_message=CONTROLLER_MESSAGE)
    assert outcome.merged and outcome.squash_commit
    sha = outcome.squash_commit
    if crash == "before_record_update":  # the kill came after the fast-forward and before the row was finished
        env.conn.execute("UPDATE merge_records SET squash_commit = NULL, completed_at = NULL WHERE task_key = 'T1'")
    env.board(_card("t_work", "done"), _card("t_merge", "blocked"))  # ... and before complete_merge_card

    report = env.go()

    row = _merge_row(env.conn)
    assert (row["candidate_sha"], row["squash_commit"], row["gate3_result"]) == (sha, sha, "pass")
    assert row["completed_at"] and report.blocked == []
    assert env.world.calls == [
        ("complete", "t_merge", f"merged {sha} (recovered)", {"squash_commit": sha, "recovered": True})]
    assert reconcile.check("b", "p1", conn=env.conn) == [] and env.go().clean


def test_a_real_no_op_merge_is_recovered_after_a_crash_before_the_card_was_completed(env):
    from ases import mergeq

    env.task(role="reviewer")
    _git_ok("branch", "swarm/T1-reviewer", "integration", cwd=env.repo)  # a review-only branch adds nothing
    outcome = mergeq.merge_task(env.repo, "integration", "swarm/T1-reviewer", "T1", ["echo ok"], conn=env.conn,
                                commit_message=CONTROLLER_MESSAGE, allow_empty=True)
    assert outcome.merged and outcome.squash_commit is None and outcome.gate3_result == "skipped"
    env.board(_card("t_work", "done"), _card("t_merge", "blocked"))

    report = env.go()

    assert env.world.calls == [("complete", "t_merge", "no changes to merge (review-only task)",
                                {"squash_commit": None, "no_op": True, "recovered": True})]
    assert report.blocked == [] and [r.kind for r in report.repairs] == ["merge_card_completed"]
    assert reconcile.check("b", "p1", conn=env.conn) == [] and env.go().clean
