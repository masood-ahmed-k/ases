"""The reviewer pre_tool_call hook scripts (round 19, package REVIEWLADDER; ASES-ROL-05, ASES-ROL-06, ASES-REV-05),
run exactly the way Hermes's shell-hook bridge runs them: as a subprocess, JSON on stdin, exit code and stderr
read back (agent/shell_hooks.py:350-381; see src/ases/hooks/deny_tool.py's own module docstring for the exact
contract this asserts against). No provider call, no Hermes: these are plain subprocess round-trips against the
scripts on disk, using PYTHONPATH from the SAME interpreter running pytest (never the real Hermes install)."""
import json
import pathlib
import subprocess
import sys

from ases import reviewcontract as rc

HOOKS_DIR = pathlib.Path(__file__).resolve().parents[2] / "src" / "ases" / "hooks"
DENY_TOOL = HOOKS_DIR / "deny_tool.py"
EVIDENCE_GUARD = HOOKS_DIR / "reviewer_evidence_guard.py"

EVIDENCE_REASON = (
    "I cannot run the test suite myself to verify these changes work. Please provide test evidence or "
    "execution output showing the tests pass."
)
GENUINE_QUESTION_REASON = "Should slugify keep underscores? The spec says letters or digits."


def _run(script: pathlib.Path, payload) -> subprocess.CompletedProcess:
    stdin = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run(
        [sys.executable, str(script)], input=stdin, capture_output=True, text=True, timeout=30,
    )


def test_hooks_dir_has_exactly_the_two_scripts_profiles_py_names():
    assert DENY_TOOL.is_file() and EVIDENCE_GUARD.is_file()


# ---------------------------------------------------------------------------------------------------------------
# R1 deny_tool.py: always exits 2 (fail-closed), for every denied tool AND for garbage input.
# ---------------------------------------------------------------------------------------------------------------


def test_deny_tool_exits_2_for_every_denied_tool_with_that_tools_own_message():
    for tool in rc.DENIED_TOOLS:
        result = _run(DENY_TOOL, {"hook_event_name": "pre_tool_call", "tool_name": tool, "tool_input": {}})
        assert result.returncode == 2, (tool, result.stdout, result.stderr)
        assert result.stdout.strip() == ""  # message on stderr only, per shell_hooks.py's own reading order
        assert result.stderr.strip() == rc.deny_message(tool)


def test_deny_tool_request_review_message_matches_the_design_verbatim():
    result = _run(DENY_TOOL, {"hook_event_name": "pre_tool_call", "tool_name": "kanban_request_review"})
    assert result.returncode == 2
    assert result.stderr.strip() == (
        "ASES: a reviewer never hands off for review. Give your verdict with kanban_complete, "
        "kanban_request_changes or kanban_block."
    )


def test_deny_tool_exits_2_even_on_unparseable_stdin_or_a_missing_tool_name():
    for bad_stdin in ("not json at all", "", "null", "[1, 2, 3]"):
        result = _run(DENY_TOOL, bad_stdin)
        assert result.returncode == 2, bad_stdin
        assert result.stderr.strip() != ""


# ---------------------------------------------------------------------------------------------------------------
# R2 reviewer_evidence_guard.py: exits 2 only on a confirmed EVIDENCE classification; fails open otherwise.
# ---------------------------------------------------------------------------------------------------------------


def test_evidence_guard_exits_2_with_the_design_message_on_evidence():
    for tool in rc.EVIDENCE_TOOLS:
        result = _run(EVIDENCE_GUARD, {
            "hook_event_name": "pre_tool_call", "tool_name": tool, "tool_input": {"reason": EVIDENCE_REASON},
        })
        assert result.returncode == 2, (tool, result.stdout, result.stderr)
        assert result.stdout.strip() == ""
        assert result.stderr.strip() == rc.EVIDENCE_GUARD_MESSAGE


def test_evidence_guard_exits_0_on_a_genuine_question():
    result = _run(EVIDENCE_GUARD, {
        "hook_event_name": "pre_tool_call", "tool_name": "kanban_block",
        "tool_input": {"reason": GENUINE_QUESTION_REASON},
    })
    assert result.returncode == 0
    assert result.stdout.strip() == "" and result.stderr.strip() == ""


def test_evidence_guard_fails_open_on_missing_reason_or_unparseable_stdin():
    no_reason = _run(EVIDENCE_GUARD, {"hook_event_name": "pre_tool_call", "tool_name": "kanban_block",
                                       "tool_input": {}})
    assert no_reason.returncode == 0

    no_tool_input = _run(EVIDENCE_GUARD, {"hook_event_name": "pre_tool_call", "tool_name": "kanban_block"})
    assert no_tool_input.returncode == 0

    for bad_stdin in ("not json at all", "", "null", "{{{"):
        result = _run(EVIDENCE_GUARD, bad_stdin)
        assert result.returncode == 0, bad_stdin


def test_evidence_guard_and_the_pure_classifier_agree_on_a_batch_of_reasons():
    """The whole point of sharing reviewcontract.classify_evidence_text: R2 and controller.process_reviewer_
    contract can never quietly disagree about what counts as evidence-stalling."""
    reasons = [
        EVIDENCE_REASON,
        GENUINE_QUESTION_REASON,
        "I am blocked: which sandbox API key should I use for the payments provider?",
        "CHANGES REQUESTED: the empty-list branch is untested; please add a case for it.",
        "I do not have terminal access to execute pytest, so I cannot confirm the fix works. "
        "Provide the test execution output and I will review it.",
    ]
    for reason in reasons:
        result = _run(EVIDENCE_GUARD, {
            "hook_event_name": "pre_tool_call", "tool_name": "kanban_block", "tool_input": {"reason": reason},
        })
        expected_block = rc.classify_evidence_text(reason)
        assert (result.returncode == 2) == expected_block, (reason, result.returncode)
