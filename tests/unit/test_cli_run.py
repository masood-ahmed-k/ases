"""swarm run's polling loop: one bad pass must not end an unattended run, a repeating one must.

Before 2026-09-19 an exception out of run_pass (a hermes CLI timeout, HermesCommandError, a locked
database) propagated straight out of cmd_run and killed the process with a traceback, leaving the board
half-driven for whoever was not watching. The passes are idempotent, so the next one simply retries."""
import argparse
import types

import pytest

from ases import cli, db, events, guards, reconcile


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """cmd_run with everything outside the loop stubbed, so only the loop's own behaviour is under test."""
    db_file = tmp_path / "ases.db"
    project = types.SimpleNamespace(board="b", roles={"coder": "coder-1", "reviewer": "reviewer"}, budgets={})
    plan = types.SimpleNamespace(
        project="p", gate_profiles={}, integration_branch="integration", serialization_links=(), tasks=(),
    )
    monkeypatch.setattr(cli, "_load_project", lambda: project)
    monkeypatch.setattr(cli.ases_config, "db_path", lambda p: db_file)
    monkeypatch.setattr(cli.plan_mod, "load_plan_file", lambda *a, **kw: plan)
    monkeypatch.setattr(cli.controller_mod, "verify_gate_pin", lambda *a, **kw: None)
    # swarm run reconciles for real on start (ASES-REC-04); a clean report keeps these tests about the loop only.
    monkeypatch.setattr(reconcile, "reconcile", lambda *a, **kw: reconcile.ReconcileReport())
    monkeypatch.setattr(cli, "_load_models_config", lambda: {})
    monkeypatch.setattr(cli.guards_mod, "check_primary_checkout",
                        lambda *a, **kw: guards.GuardResult(True, (), "abc", "integration"))
    monkeypatch.setattr(cli.guards_mod, "adopt_current_head", lambda conn, project, repo: "abc")
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    args = argparse.Namespace(repo=str(tmp_path), max_iterations=20, sleep_seconds=0, ignore_reconcile=False)
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


def test_usage_sessions_are_shown_on_the_pass_line_only_when_there_are_some(wired, monkeypatch, capsys):
    _scripted_run_pass(monkeypatch, [dict(_NOT_DONE, usage_sessions=3), dict(_NOT_DONE, usage_sessions=0), _DONE])

    assert cli.cmd_run(wired.args) == 0

    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("[pass")]
    assert "usage_sessions=3" in lines[0]
    assert "usage_sessions" not in lines[1]


def test_run_refuses_to_start_on_a_primary_checkout_ases_cannot_trust(wired, monkeypatch, capsys):
    """ASES-GIT-12: a dirty or wrong-branch primary checkout stops swarm run before the first pass."""
    monkeypatch.setattr(cli.guards_mod, "check_primary_checkout", lambda *a, **kw: guards.GuardResult(
        False, ("primary checkout is dirty: ?? 'stray.txt'",), "abc", "integration"))
    calls = _scripted_run_pass(monkeypatch, [_DONE])

    assert cli.cmd_run(wired.args) == 3

    assert calls == []
    err = capsys.readouterr().err
    assert "REFUSED (ASES-GIT-12)" in err and "stray.txt" in err


def test_run_refuses_before_any_pass_when_a_pinned_role_model_is_rejected(wired, monkeypatch, capsys):
    """ASES-MOD-02, acceptance 22.4: swarm run's own pre-flight refuses the same way swarm approve does, since
    config/models.yaml can change between approve and run. Here the coder role's pinned model is on a custom
    OpenAI-compatible endpoint with no declared context_length -- rejected as unknown -- and the refusal must
    come before the first controller.run_pass call, exit non-zero like the other pre-flight refusals above."""
    task = types.SimpleNamespace(key="T1", role="coder", sandbox_network=False, allow_gate_config_changes=False)
    plan = types.SimpleNamespace(
        project="p", gate_profiles={}, integration_branch="integration", serialization_links=(), tasks=(task,),
    )
    monkeypatch.setattr(cli.plan_mod, "load_plan_file", lambda *a, **kw: plan)
    monkeypatch.setattr(cli, "_load_models_config", lambda: {
        "providers": {"xkiro": {"type": "openai_compatible"}},
        "models": [{"provider": "xkiro", "model": "undeclared-model", "role_class": "coder", "pinned": True}],
    })
    calls = _scripted_run_pass(monkeypatch, [_DONE])  # never reached if the refusal works

    assert cli.cmd_run(wired.args) == 1

    assert calls == []
    err = capsys.readouterr().err
    assert "swarm run REFUSED (ASES-MOD-02)" in err
    assert "coder" in err and "xkiro/undeclared-model" in err and "rejected unknown" in err


def test_run_adopts_the_checkouts_head_only_after_the_guard_passes(wired, monkeypatch):
    adopted = []
    monkeypatch.setattr(cli.guards_mod, "adopt_current_head", lambda conn, project, repo: adopted.append(project) or "abc")
    _scripted_run_pass(monkeypatch, [_DONE])

    assert cli.cmd_run(wired.args) == 0

    assert adopted == ["p"]


def test_run_does_not_adopt_a_head_it_refused(wired, monkeypatch):
    adopted = []
    monkeypatch.setattr(cli.guards_mod, "check_primary_checkout", lambda *a, **kw: guards.GuardResult(
        False, ("wrong branch",), "abc", "other"))
    monkeypatch.setattr(cli.guards_mod, "adopt_current_head", lambda conn, project, repo: adopted.append(project) or "abc")
    _scripted_run_pass(monkeypatch, [_DONE])

    assert cli.cmd_run(wired.args) == 3

    assert adopted == []


def test_run_halts_with_exit_code_3_and_names_the_problems_on_a_security_event(wired, monkeypatch, capsys):
    summary = dict(_NOT_DONE, integrity=["primary checkout is dirty: ?? 'stray.txt'"])
    calls = _scripted_run_pass(monkeypatch, [summary, _DONE])

    assert cli.cmd_run(wired.args) == 3

    assert len(calls) == 1  # halted on the first violation, did not poll on
    err = capsys.readouterr().err
    assert "SECURITY EVENT" in err and "stray.txt" in err


def test_serialization_lines_tell_the_user_what_gate_0_added():
    link = types.SimpleNamespace(later="T2", earlier="T1", reason="touches overlap: src/* and src/a.py")
    plan = types.SimpleNamespace(serialization_links=(link,))

    assert cli.serialization_lines(plan) == ["  Gate 0 serialized T2 after T1: touches overlap: src/* and src/a.py"]
    assert cli.serialization_lines(types.SimpleNamespace(serialization_links=())) == []
