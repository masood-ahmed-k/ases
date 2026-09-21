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


def _capture(monkeypatch, stdout=""):
    seen = []

    def fake_run(args, **kw):
        seen.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(hermes, "_run", fake_run)
    return seen


def test_kanban_show_also_returns_events_comments_and_latest_summary(monkeypatch):
    real = {
        "task": {"id": "t_1", "status": "blocked"},
        "parents": [], "children": [], "runs": [],
        "comments": [{"author": "default", "body": "BLOCKED: which database?", "created_at": 1}],
        "events": [{"kind": "blocked", "payload": {"reason": "which database?"}, "created_at": 1, "run_id": 1}],
        "latest_summary": "s",
    }
    monkeypatch.setattr(hermes, "_run", lambda args, **kw: subprocess.CompletedProcess(
        args, 0, stdout=json.dumps(real), stderr=""))

    card = hermes.kanban_show("b", "t_1")

    assert card["_events"][0]["payload"]["reason"] == "which database?"
    assert card["_comments"][0]["body"] == "BLOCKED: which database?"
    assert card["_latest_summary"] == "s"


def test_kanban_show_defaults_events_and_comments_to_empty_lists(monkeypatch):
    monkeypatch.setattr(hermes, "_run", lambda args, **kw: subprocess.CompletedProcess(
        args, 0, stdout=json.dumps({"task": {"id": "t_1"}}), stderr=""))

    card = hermes.kanban_show("b", "t_1")

    assert card["_events"] == [] and card["_comments"] == [] and card["_latest_summary"] is None


def test_kanban_comment_passes_the_text_after_a_double_dash_so_a_leading_dash_is_not_an_option(monkeypatch):
    seen = _capture(monkeypatch)

    hermes.kanban_comment("b", "t_1", "-x looks like an option", author="asestest")

    assert seen == [["kanban", "--board", "b", "comment", "--author", "asestest", "t_1", "--", "-x looks like an option"]]


def test_kanban_comment_without_an_author_leaves_the_default(monkeypatch):
    seen = _capture(monkeypatch)

    hermes.kanban_comment("b", "t_1", "hello")

    assert seen == [["kanban", "--board", "b", "comment", "t_1", "--", "hello"]]


def test_kanban_unblock_with_a_reason_uses_the_single_argument_form(monkeypatch):
    seen = _capture(monkeypatch)

    hermes.kanban_unblock("b", "t_1", "-use sqlite")
    hermes.kanban_unblock("b", "t_2")

    assert seen == [
        ["kanban", "--board", "b", "unblock", "t_1", "--reason=-use sqlite"],
        ["kanban", "--board", "b", "unblock", "t_2"],
    ]


def test_kanban_promote_passes_the_reason_after_a_double_dash(monkeypatch):
    seen = _capture(monkeypatch)

    hermes.kanban_promote("b", "t_1", "validated by the controller")
    hermes.kanban_promote("b", "t_2")

    assert seen == [
        ["kanban", "--board", "b", "promote", "t_1", "--", "validated by the controller"],
        ["kanban", "--board", "b", "promote", "t_2"],
    ]


def test_kanban_archive_archives_only_and_never_purges(monkeypatch):
    seen = _capture(monkeypatch)

    hermes.kanban_archive("b", ["t_1", "t_2"])
    hermes.kanban_archive("b", [])

    assert seen == [["kanban", "--board", "b", "archive", "t_1", "t_2"]]  # nothing called for the empty list
    assert not any("--rm" in call for call in seen)


def test_kanban_set_model_pins_or_clears(monkeypatch):
    seen = _capture(monkeypatch)

    hermes.kanban_set_model("b", "t_1", "minimax/minimax-m3:free", provider="xkiro")
    hermes.kanban_set_model("b", "t_2", None)

    assert seen == [
        ["kanban", "--board", "b", "set-model", "--provider", "xkiro", "t_1", "minimax/minimax-m3:free"],
        ["kanban", "--board", "b", "set-model", "t_2", "none"],
    ]


def test_kanban_block_puts_the_kind_before_the_card_id_and_the_reason_after_a_double_dash(monkeypatch):
    """Hermes's argparse rejects `block <id> --kind K -- <reason>`; the accepted order is options first."""
    seen = _capture(monkeypatch)
    hermes.kanban_block("b", "t_1", "should we use A or B?", kind="needs_input")
    hermes.kanban_block("b", "t_2", "-x looks like a flag")
    assert seen == [
        ["kanban", "--board", "b", "block", "--kind", "needs_input", "t_1", "--", "should we use A or B?"],
        ["kanban", "--board", "b", "block", "t_2", "--", "-x looks like a flag"],
    ]
