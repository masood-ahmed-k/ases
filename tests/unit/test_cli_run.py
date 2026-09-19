"""swarm run's polling loop: one bad pass must not end an unattended run, a repeating one must.

Before 2026-09-19 an exception out of run_pass (a hermes CLI timeout, HermesCommandError, a locked
database) propagated straight out of cmd_run and killed the process with a traceback, leaving the board
half-driven for whoever was not watching. The passes are idempotent, so the next one simply retries."""
import argparse
import types

import pytest

from ases import cli, db, events


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """cmd_run with everything outside the loop stubbed, so only the loop's own behaviour is under test."""
    db_file = tmp_path / "ases.db"
    project = types.SimpleNamespace(board="b", roles={"coder": "coder-1", "reviewer": "reviewer"}, budgets={})
    plan = types.SimpleNamespace(project="p", gate_profiles={})
    monkeypatch.setattr(cli, "_load_project", lambda: project)
    monkeypatch.setattr(cli.ases_config, "db_path", lambda p: db_file)
    monkeypatch.setattr(cli.plan_mod, "load_plan_file", lambda *a, **kw: plan)
    monkeypatch.setattr(cli.controller_mod, "verify_gate_pin", lambda *a, **kw: None)
    monkeypatch.setattr("ases.reconcile.check", lambda *a, **kw: [])
    monkeypatch.setattr(cli, "_load_models_config", lambda: {})
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    args = argparse.Namespace(repo=str(tmp_path), max_iterations=20, sleep_seconds=0)
    return types.SimpleNamespace(args=args, db_file=db_file)


def _scripted_run_pass(monkeypatch, script):
    """run_pass returns or raises the next scripted item on every call; returns the list of calls made."""
    calls = []
    items = iter(script)

    def fake(*args, **kwargs):
        calls.append(1)
        item = next(items)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(cli.controller_mod, "run_pass", fake)
    return calls


_DONE = {"parked": [], "merged": ["T1"], "sent_back": [], "finished": True}
_NOT_DONE = {"parked": [], "merged": [], "sent_back": [], "finished": False}


def _pass_errors(db_file):
    conn = db.connect(db_file)
    return [e for e in events.recent(conn, limit=100) if e["kind"] == "pass_error"]


def test_an_isolated_failed_pass_does_not_end_the_run(wired, monkeypatch, capsys):
    calls = _scripted_run_pass(monkeypatch, [RuntimeError("hermes kanban list timed out"), _DONE])

    assert cli.cmd_run(wired.args) == 0

    assert len(calls) == 2
    assert "all merge cards done" in capsys.readouterr().out
    errors = _pass_errors(wired.db_file)
    assert len(errors) == 1
    assert "hermes kanban list timed out" in errors[0]["payload"]


def test_a_repeating_failure_stops_the_run_with_exit_code_2(wired, monkeypatch, capsys):
    calls = _scripted_run_pass(monkeypatch, [RuntimeError("boom")] * 10)

    assert cli.cmd_run(wired.args) == 2

    assert len(calls) == cli._MAX_CONSECUTIVE_PASS_ERRORS
    assert "stopping after 5 failed passes in a row" in capsys.readouterr().err
    assert len(_pass_errors(wired.db_file)) == cli._MAX_CONSECUTIVE_PASS_ERRORS


def test_the_failure_counter_resets_after_a_good_pass(wired, monkeypatch):
    boom = RuntimeError("boom")
    n = cli._MAX_CONSECUTIVE_PASS_ERRORS - 1
    calls = _scripted_run_pass(monkeypatch, [boom] * n + [_NOT_DONE] + [boom] * n + [_DONE])

    assert cli.cmd_run(wired.args) == 0  # two bursts of n failures never add up to the limit

    assert len(calls) == 2 * n + 2


def test_unreviewed_tasks_are_shown_on_the_pass_line(wired, monkeypatch, capsys):
    summary = dict(_NOT_DONE, unreviewed=["T1"])
    _scripted_run_pass(monkeypatch, [summary, _DONE])

    assert cli.cmd_run(wired.args) == 0

    assert "unreviewed=['T1']" in capsys.readouterr().out
