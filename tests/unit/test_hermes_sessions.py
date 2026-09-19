"""hermes.session_usage: what one Hermes worker session cost (ASES-CAP-03).

Checked against real Hermes 0.21.3 on 2026-09-19: `hermes -p <profile> sessions export --session-id <id>
--format jsonl --redact -` prints ONE JSON object per session on one line, and that object also carries the
whole conversation under "messages". `hermes._run` is faked, so no real hermes runs here."""
import json
import subprocess

import pytest

from ases import hermes

SESSION_ID = "20260919_173835_d33d16"


def _export(session_id=SESSION_ID, **overrides):
    """One line as `sessions export` prints it: bookkeeping fields, then the whole conversation."""
    session = {
        "id": session_id,
        "source": "kanban",
        "profile_name": "reviewer",
        "model": "cohere/north-mini-code:free",
        "started_at": 1789832318.2723622,
        "ended_at": 1789832491.6628356,
        "end_reason": "completed",
        "message_count": 79,
        "tool_call_count": 40,
        "api_call_count": 37,
        "input_tokens": 933438,
        "output_tokens": 13726,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 14256,
        "estimated_cost_usd": 0.0,
        "system_prompt": "You are the reviewer. " * 200,
        "messages": [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"message {i} " * 50} for i in range(79)
        ],
    }
    session.update(overrides)
    return json.dumps(session)


def _fake_run(monkeypatch, *, stdout="", stderr="", returncode=0):
    """Fakes hermes._run and returns the list of (args, kwargs) it was called with."""
    seen = []

    def fake_run(args, **kw):
        seen.append((args, kw))
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(hermes, "_run", fake_run)
    return seen


def test_session_usage_reads_only_the_ledger_fields_and_ignores_the_messages(monkeypatch):
    line = _export()
    assert len(line) > 40_000      # a real export is big: the conversation must not come back with the numbers
    _fake_run(monkeypatch, stdout=line + "\n")

    result = hermes.session_usage("reviewer", SESSION_ID)

    assert result == {
        "id": SESSION_ID, "model": "cohere/north-mini-code:free", "api_call_count": 37,
        "input_tokens": 933438, "output_tokens": 13726,
    }


def test_session_usage_runs_the_documented_export_for_that_profile_and_session(monkeypatch):
    seen = _fake_run(monkeypatch, stdout=_export())

    hermes.session_usage("reviewer", SESSION_ID)

    assert [args for args, _ in seen] == [[
        "-p", "reviewer", "sessions", "export", "--session-id", SESSION_ID, "--format", "jsonl", "--redact", "-",
    ]]


def test_session_usage_passes_its_timeout_to_the_command(monkeypatch):
    seen = _fake_run(monkeypatch, stdout=_export())

    hermes.session_usage("reviewer", SESSION_ID)
    hermes.session_usage("reviewer", SESSION_ID, timeout=5)

    assert [kw["timeout"] for _, kw in seen] == [60, 5]


def test_session_usage_is_none_when_the_command_exits_non_zero(monkeypatch):
    # Even with a well-formed line on stdout: a command that failed is a failed export.
    _fake_run(monkeypatch, stdout=_export(), stderr="no such profile", returncode=1)

    assert hermes.session_usage("reviewer", SESSION_ID) is None


@pytest.mark.parametrize("stdout", ["", "\n", "  \n\n  \n"])
def test_session_usage_is_none_when_nothing_is_printed(monkeypatch, stdout):
    _fake_run(monkeypatch, stdout=stdout)

    assert hermes.session_usage("reviewer", SESSION_ID) is None


@pytest.mark.parametrize("stdout", [
    "not json at all",
    "{broken",
    "[1, 2, 3]",
    '"a string"',
    "null",
    "42",
    json.dumps({"api_call_count": 37}),             # JSON, but no id
    json.dumps({"id": None, "api_call_count": 37}),
])
def test_session_usage_is_none_when_no_line_is_a_parseable_session(monkeypatch, stdout):
    _fake_run(monkeypatch, stdout=stdout + "\n")

    assert hermes.session_usage("reviewer", SESSION_ID) is None


def test_session_usage_is_none_for_a_line_about_a_different_session(monkeypatch):
    _fake_run(monkeypatch, stdout=_export("20260919_000000_aaaaaa") + "\n")

    assert hermes.session_usage("reviewer", SESSION_ID) is None


def test_session_usage_is_none_when_the_command_times_out(monkeypatch):
    def fake_run(args, **kw):
        raise subprocess.TimeoutExpired(cmd=["hermes", *args], timeout=kw["timeout"])

    monkeypatch.setattr(hermes, "_run", fake_run)

    assert hermes.session_usage("reviewer", SESSION_ID, timeout=1) is None


@pytest.mark.parametrize("error", [
    hermes.HermesNotFound("`hermes` is not on PATH"),
    FileNotFoundError("hermes"),
    PermissionError("access denied"),
])
def test_session_usage_is_none_when_hermes_cannot_be_started(monkeypatch, error):
    def fake_run(args, **kw):
        raise error

    monkeypatch.setattr(hermes, "_run", fake_run)

    assert hermes.session_usage("reviewer", SESSION_ID) is None


def test_session_usage_finds_its_own_line_among_other_output(monkeypatch):
    stdout = "\n".join([
        "hermes: exporting 1 session",
        _export("20260919_000000_aaaaaa", api_call_count=5),
        "",
        "{broken",
        _export(SESSION_ID, api_call_count=3),
        _export("20260919_000000_bbbbbb", api_call_count=9),
    ])
    _fake_run(monkeypatch, stdout=stdout + "\n")

    result = hermes.session_usage("reviewer", SESSION_ID)

    assert result["id"] == SESSION_ID
    assert result["api_call_count"] == 3


@pytest.mark.parametrize("session", [
    {"id": SESSION_ID},
    {"id": SESSION_ID, "model": None, "api_call_count": None, "input_tokens": None, "output_tokens": None},
])
def test_session_usage_defaults_a_missing_number_to_zero_and_a_missing_model_to_empty(monkeypatch, session):
    _fake_run(monkeypatch, stdout=json.dumps(session) + "\n")

    assert hermes.session_usage("reviewer", SESSION_ID) == {
        "id": SESSION_ID, "model": "", "api_call_count": 0, "input_tokens": 0, "output_tokens": 0,
    }


@pytest.mark.parametrize("raw, expected", [
    ("37", 37),
    ("37.0", 37),
    ("7.9", 7),
    ('"12"', 12),
    ("0", 0),
    ('"abc"', 0),
    ("[1]", 0),
    ('{"a": 1}', 0),
    ("-5", 0),
    ("NaN", 0),
    ("Infinity", 0),
    ("null", 0),
])
def test_session_usage_returns_every_number_as_a_non_negative_int(monkeypatch, raw, expected):
    line = ('{"id": "%s", "api_call_count": %s, "input_tokens": %s, "output_tokens": %s}'
            % (SESSION_ID, raw, raw, raw))
    _fake_run(monkeypatch, stdout=line + "\n")

    result = hermes.session_usage("reviewer", SESSION_ID)

    for field in ("api_call_count", "input_tokens", "output_tokens"):
        assert result[field] == expected
        assert type(result[field]) is int
