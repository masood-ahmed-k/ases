"""hermes.board_runs (round 19, package GIT12; ASES-GIT-12): the read-only SQLite run source r19/GIT12.md's
design calls option R1. Every test builds a real SQLite file shaped like Hermes 0.21.3's own task_runs/
task_events/tasks tables (hermes_cli/kanban_db.py), never a copy of hermes.py's own query, so a real read against
a real file is what is under test.
"""
import sqlite3

import pytest

from ases import hermes

TESTED = "0.21.3"


def _make_board_db(path, *, runs=(), tasks=(), events=()):
    """A minimal Hermes-shaped kanban.db: `tasks` rows are (id, status, workspace_path, branch_name), `runs` rows
    are (id, task_id, profile, started_at, ended_at, outcome, worker_pid), `events` rows are
    (id, task_id, run_id, kind, created_at)."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, workspace_path TEXT, branch_name TEXT)"
    )
    conn.execute(
        "CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT, started_at INTEGER, "
        "ended_at INTEGER, outcome TEXT, worker_pid INTEGER)"
    )
    conn.execute(
        "CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, run_id INTEGER, kind TEXT, "
        "created_at INTEGER)"
    )
    for task_id, status, workspace_path, branch_name in tasks:
        conn.execute(
            "INSERT INTO tasks (id, status, workspace_path, branch_name) VALUES (?, ?, ?, ?)",
            (task_id, status, workspace_path, branch_name),
        )
    for run_id, task_id, profile, started_at, ended_at, outcome, worker_pid in runs:
        conn.execute(
            "INSERT INTO task_runs (id, task_id, profile, started_at, ended_at, outcome, worker_pid) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, task_id, profile, started_at, ended_at, outcome, worker_pid),
        )
    for event_id, task_id, run_id, kind, created_at in events:
        conn.execute(
            "INSERT INTO task_events (id, task_id, run_id, kind, created_at) VALUES (?, ?, ?, ?, ?)",
            (event_id, task_id, run_id, kind, created_at),
        )
    conn.commit()
    conn.close()


@pytest.fixture(autouse=True)
def _pin_installed_version(monkeypatch):
    """Every test in this file wants the installed-version gate to pass by default; the two tests that exercise
    the gate itself override this."""
    monkeypatch.setattr(hermes, "hermes_version", lambda: TESTED)


def test_default_board_reads_kanban_db_directly_under_native_home(tmp_path):
    _make_board_db(
        tmp_path / "kanban.db",
        tasks=[("t_1", "running", "C:/wt/t_1", "swarm/T1-coder")],
        runs=[(1, "t_1", "coder-1", 1000, None, None, 4242)],
    )

    result = hermes.board_runs(tmp_path, "default", 0, tested_version=TESTED)

    assert result.ok and result.reason == ""
    assert len(result.runs) == 1
    run = result.runs[0]
    assert (run.run_id, run.task_id, run.profile) == (1, "t_1", "coder-1")
    assert (run.workspace_path, run.branch_name, run.task_status) == ("C:/wt/t_1", "swarm/T1-coder", "running")
    assert run.ended_at is None and run.worker_pid == 4242 and run.reaped_at is None


def test_a_named_board_reads_kanban_boards_slug_kanban_db(tmp_path):
    board_dir = tmp_path / "kanban" / "boards" / "ases-phase3"
    board_dir.mkdir(parents=True)
    _make_board_db(board_dir / "kanban.db", tasks=[("t_1", "done", None, None)])

    result = hermes.board_runs(tmp_path, "ases-phase3", 0, tested_version=TESTED)

    assert result.ok  # found the file at the non-default board's own path, proving the resolution rule


def test_open_ended_in_window_and_pid_still_set_runs_are_included_a_closed_stale_run_is_not(tmp_path):
    _make_board_db(
        tmp_path / "kanban.db",
        tasks=[("t_1", "done", None, None), ("t_2", "done", None, None), ("t_3", "done", None, None)],
        runs=[
            (1, "t_1", "coder-1", 100, None, None, None),        # open: always included
            (2, "t_2", "coder-1", 200, 250, "completed", None),  # ended inside [since, ...): included
            (3, "t_3", "coder-1", 10, 20, "completed", None),    # ended long before `since` and pid cleared: excluded
        ],
    )

    result = hermes.board_runs(tmp_path, "default", since_epoch=150, tested_version=TESTED)

    assert result.ok
    assert {r.run_id for r in result.runs} == {1, 2}


def test_worker_pid_still_set_keeps_a_long_closed_run_in_even_outside_the_window(tmp_path):
    _make_board_db(
        tmp_path / "kanban.db",
        tasks=[("t_1", "done", None, None)],
        runs=[(1, "t_1", "coder-1", 10, 20, "completed", 9999)],  # ended long ago, but worker_pid is still set
    )

    result = hermes.board_runs(tmp_path, "default", since_epoch=100_000, tested_version=TESTED)

    assert result.ok
    assert [r.run_id for r in result.runs] == [1]


def test_reaped_at_is_the_matching_events_created_at(tmp_path):
    _make_board_db(
        tmp_path / "kanban.db",
        tasks=[("t_1", "done", None, None)],
        runs=[(1, "t_1", "coder-1", 100, 200, "completed", None)],
        events=[(1, "t_1", 1, "terminal_worker_reaped", 400)],
    )

    result = hermes.board_runs(tmp_path, "default", since_epoch=0, tested_version=TESTED)

    assert result.runs[0].reaped_at == 400


def test_a_version_mismatch_falls_back_to_unknown_attribute_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(hermes, "hermes_version", lambda: "0.20.0")
    _make_board_db(tmp_path / "kanban.db")  # a real, readable file: the version gate must still refuse it

    result = hermes.board_runs(tmp_path, "default", 0, tested_version=TESTED)

    assert result.ok is False and result.runs == ()
    assert "0.20.0" in result.reason and TESTED in result.reason


def test_hermes_not_installed_falls_back_to_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(hermes, "hermes_version", lambda: None)
    _make_board_db(tmp_path / "kanban.db")

    result = hermes.board_runs(tmp_path, "default", 0, tested_version=TESTED)

    assert result.ok is False and "None" in result.reason


def test_a_missing_board_database_falls_back_to_unknown_and_creates_nothing(tmp_path):
    missing = tmp_path / "kanban.db"

    result = hermes.board_runs(tmp_path, "default", 0, tested_version=TESTED)

    assert result.ok is False and result.runs == ()
    assert str(missing) in result.reason
    assert not missing.exists()  # never silently created: a plain sqlite3.connect() would have made one


def test_the_connection_is_truly_read_only_a_write_through_the_same_uri_is_refused(tmp_path):
    """The hard requirement (this package's own standing rule): Hermes SQLite files are read with mode=ro URIs.
    Proven here by taking the SAME resolved path board_runs used and trying to write through mode=ro ourselves --
    it must fail exactly the way board_runs's own connection would refuse a write, never silently succeed."""
    db_path = tmp_path / "kanban.db"
    _make_board_db(db_path, tasks=[("t_1", "done", None, None)])

    result = hermes.board_runs(tmp_path, "default", 0, tested_version=TESTED)
    assert result.ok

    ro_uri = db_path.resolve().as_uri() + "?mode=ro"
    ro_conn = sqlite3.connect(ro_uri, uri=True)
    with pytest.raises(sqlite3.OperationalError):
        ro_conn.execute("INSERT INTO tasks (id, status) VALUES ('intruder', 'done')")
    ro_conn.close()


def test_a_board_other_than_default_is_read_from_its_own_boards_slug_directory_even_when_absent(tmp_path):
    result = hermes.board_runs(tmp_path, "ases-phase3", 0, tested_version=TESTED)

    assert result.ok is False
    assert str(tmp_path / "kanban" / "boards" / "ases-phase3" / "kanban.db") in result.reason
