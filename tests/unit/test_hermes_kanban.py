"""kanban_show's real JSON shape is nested ({"task": {...}, "parents": [...], ...}), not flat --
confirmed against a live card on 2026-09-18. Every caller (review.py, reconcile.py, controller.py)
was written assuming a flat dict; the unwrap belongs in hermes.py so none of them had to change."""
import json
import subprocess

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
