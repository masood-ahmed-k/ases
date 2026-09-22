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


# ---------------------------------------------------------------------------------------------------------
# kanban_specify (round 7): the one real auxiliary-model call this file makes, only from triage.promote_card.
# Real shape confirmed by reading hermes_cli/kanban.py's _run_triage_sweep and hermes_cli/kanban_specify.py's
# specify_task, 2026-09-22: a single (non --all) call prints {"task_id", "ok", "reason", "new_title"} as one
# JSON line on stdout EITHER WAY, then exits 0 when ok is true and 1 when ok is false -- there is no zero-exit
# ok:false case for a single task_id.
# ---------------------------------------------------------------------------------------------------------


def _fake_run_with_kwargs(monkeypatch, returncode, stdout, stderr=""):
    """Installs hermes._run, returns a list that records (args, kwargs) for every call."""
    seen = []

    def fake_run(args, **kw):
        seen.append((args, kw))
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(hermes, "_run", fake_run)
    return seen


def test_kanban_specify_argv_shape_with_author_and_json(monkeypatch):
    seen = _fake_run_with_kwargs(
        monkeypatch, 0, json.dumps({"task_id": "t_1", "ok": True, "reason": "specified", "new_title": "Fix the thing"}),
    )

    hermes.kanban_specify("b", "t_1", author="lead")

    assert seen[0][0] == ["kanban", "--board", "b", "specify", "t_1", "--author", "lead", "--json"]


def test_kanban_specify_without_an_author_omits_the_flag(monkeypatch):
    seen = _fake_run_with_kwargs(
        monkeypatch, 0, json.dumps({"task_id": "t_1", "ok": True, "reason": "specified", "new_title": None}),
    )

    hermes.kanban_specify("b", "t_1")

    assert seen[0][0] == ["kanban", "--board", "b", "specify", "t_1", "--json"]


def test_kanban_specify_defaults_to_a_120_second_timeout_not_the_30_second_kanban_default(monkeypatch):
    seen = _fake_run_with_kwargs(
        monkeypatch, 0, json.dumps({"task_id": "t_1", "ok": True, "reason": "specified", "new_title": None}),
    )

    hermes.kanban_specify("b", "t_1")

    assert seen[0][1]["timeout"] == 120


def test_kanban_specify_an_explicit_timeout_is_forwarded(monkeypatch):
    seen = _fake_run_with_kwargs(
        monkeypatch, 0, json.dumps({"task_id": "t_1", "ok": True, "reason": "specified", "new_title": None}),
    )

    hermes.kanban_specify("b", "t_1", timeout=300)

    assert seen[0][1]["timeout"] == 300


def test_kanban_specify_a_zero_exit_with_ok_true_returns_the_reason_and_new_title(monkeypatch):
    _fake_run_with_kwargs(
        monkeypatch, 0,
        json.dumps({"task_id": "t_1", "ok": True, "reason": "specified", "new_title": "Add rate limiting"}),
    )

    result = hermes.kanban_specify("b", "t_1")

    assert result == hermes.SpecifyResult(ok=True, reason="specified", new_title="Add rate limiting")


def test_kanban_specify_a_nonzero_exit_with_ok_false_in_the_json_does_not_raise(monkeypatch):
    """The real single-task exit code for an ok=false outcome is 1, not 0 (read from _run_triage_sweep):
    kanban_specify must not treat this as a HermesCommandError, since it is Hermes's normal "could not make
    sense of this" outcome, not an infrastructure failure."""
    _fake_run_with_kwargs(
        monkeypatch, 1,
        json.dumps({"task_id": "t_1", "ok": False, "reason": "task is not in triage (status='todo')", "new_title": None}),
    )

    result = hermes.kanban_specify("b", "t_1")

    assert result == hermes.SpecifyResult(ok=False, reason="task is not in triage (status='todo')", new_title=None)


def test_kanban_specify_a_nonzero_exit_with_no_parseable_json_raises(monkeypatch):
    """A genuine failure (hermes crashed, bad board, argparse usage error): no {"task_id", "ok", ...} shape on
    stdout at all, so this must raise, unlike the ok=false case above."""
    _fake_run_with_kwargs(monkeypatch, 2, "", stderr="kanban: specify requires a task id or --all")

    with pytest.raises(hermes.HermesCommandError):
        hermes.kanban_specify("b", "t_1")


def test_kanban_specify_malformed_json_on_a_zero_exit_raises(monkeypatch):
    """Malformed JSON gets no special leniency here: json.loads is left to raise, same as every other
    _kanban_json caller in this file, and that failure surfaces as HermesCommandError."""
    _fake_run_with_kwargs(monkeypatch, 0, "not json")

    with pytest.raises(hermes.HermesCommandError):
        hermes.kanban_specify("b", "t_1")


def test_kanban_specify_json_missing_the_expected_keys_raises(monkeypatch):
    """Valid JSON that is not the {"task_id", "ok", "reason", "new_title"} shape (e.g. some unrelated object)
    must not be silently coerced into an ok=false result."""
    _fake_run_with_kwargs(monkeypatch, 1, json.dumps({"unexpected": "shape"}))

    with pytest.raises(hermes.HermesCommandError):
        hermes.kanban_specify("b", "t_1")
