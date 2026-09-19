"""kanban_show's real JSON shape is nested ({"task": {...}, "parents": [...], ...}), not flat --
confirmed against a live card on 2026-09-18. Every caller (review.py, reconcile.py, controller.py)
was written assuming a flat dict; the unwrap belongs in hermes.py so none of them had to change."""
import json
import subprocess

import pytest

from ases import hermes


def test_kanban_show_unwraps_the_real_nested_shape(monkeypatch):
    real_response = {
        "task": {"id": "t_1", "status": "ready", "branch_name": "swarm/T1-coder", "assignee": "coder-1"},
        "parents": ["t_0"],
        "children": ["t_2"],
        "comments": [],
        "events": [{"kind": "created"}],
        "runs": [],
    }

    def fake_run(args, **kw):
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(real_response), stderr="")

    monkeypatch.setattr(hermes, "_run", fake_run)
    result = hermes.kanban_show("b", "t_1")

    assert result["status"] == "ready"          # flat access, as every caller expects
    assert result["branch_name"] == "swarm/T1-coder"
    assert result["_parents"] == ["t_0"]
    assert result["_children"] == ["t_2"]


def test_kanban_reopen_review_is_the_controllers_send_back_with_the_reason_as_one_argument(monkeypatch):
    seen = []

    def fake_run(args, **kw):
        seen.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="Reopened t_1", stderr="")

    monkeypatch.setattr(hermes, "_run", fake_run)

    hermes.kanban_reopen_review("b", "t_1", "-x looks like an option\nsecond line")

    # `--reason=<text>` as ONE argument: a reason that starts with a dash must never be read as an option.
    assert seen == [["kanban", "--board", "b", "reopen-review", "t_1", "--reason=-x looks like an option\nsecond line"]]


def test_kanban_reopen_review_raises_when_hermes_refuses(monkeypatch):
    def fake_run(args, **kw):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="cannot reopen t_1 (not in review?)")

    monkeypatch.setattr(hermes, "_run", fake_run)

    with pytest.raises(hermes.HermesCommandError):
        hermes.kanban_reopen_review("b", "t_1", "anything")
