"""reviewcontract.py: the pure reviewer-contract classifier (round 19, package REVIEWLADDER; ASES-ROL-05,
ASES-ROL-06, ASES-REV-05). Every case here is what R2 (src/ases/hooks/reviewer_evidence_guard.py) and
controller.process_reviewer_contract both rely on classifying identically; see tests/unit/test_hooks.py for the
hook scripts' own subprocess-level tests, which use the SAME reasons as this file for exactly that reason."""
import re

from ases import reviewcontract as rc

# Plausible reconstructions of the reviewer's real block/changes-request text on t_4ae270eb (research report
# finding F1: runs 33, 39, 40), since the verbatim board text was not itself reproduced in the report.
RUN_39_40_TEXT = (
    "I am unable to run the test suite myself in this environment, so I cannot verify these changes work. "
    "Please provide test evidence or execution output showing the tests pass before I can give a verdict."
)
RUN_33_TEXT = (
    "I do not have terminal access to execute pytest, so I cannot confirm the fix works as claimed. "
    "Provide the test execution output and I will review it."
)


def test_denied_tools_and_matcher_are_the_six_tools_in_a_fixed_order():
    assert rc.DENIED_TOOLS == (
        "write_file", "patch", "skill_manage", "kanban_request_review", "kanban_unblock", "kanban_link",
    )
    assert rc.DENY_MATCHER == "(?:write_file|patch|skill_manage|kanban_request_review|kanban_unblock|kanban_link)"
    compiled = re.compile(rc.DENY_MATCHER)
    assert all(compiled.fullmatch(tool) for tool in rc.DENIED_TOOLS)
    assert not compiled.fullmatch("kanban_complete") and not compiled.fullmatch("write_filex")


def test_evidence_tools_and_matcher_are_the_two_verdict_tools_a_reviewer_can_misuse():
    assert rc.EVIDENCE_TOOLS == ("kanban_block", "kanban_request_changes")
    compiled = re.compile(rc.EVIDENCE_MATCHER)
    assert all(compiled.fullmatch(tool) for tool in rc.EVIDENCE_TOOLS)
    assert not compiled.fullmatch("kanban_complete")


def test_deny_message_names_the_tool_and_the_request_review_case_matches_the_design_verbatim():
    assert rc.deny_message("kanban_request_review") == (
        "ASES: a reviewer never hands off for review. Give your verdict with kanban_complete, "
        "kanban_request_changes or kanban_block."
    )
    for tool in ("kanban_unblock", "kanban_link"):
        message = rc.deny_message(tool)
        assert "controller's job" in message and "kanban_complete" in message
    for tool in ("write_file", "patch", "skill_manage"):
        message = rc.deny_message(tool)
        assert tool in message and "ASES-ROL-05" in message
    assert "this tool" in rc.deny_message("") and "this tool" in rc.deny_message(None)
    assert "unknown_tool" in rc.deny_message("unknown_tool")


def test_evidence_guard_message_matches_the_design_verbatim():
    assert rc.EVIDENCE_GUARD_MESSAGE == (
        "ASES: running tests is the controller's job. The card's comment headed 'ASES gate record' is the "
        "evidence. Do not block or request changes for test evidence. Judge the code and give PASS or "
        "CHANGES_REQUIRED with concrete code findings. If you have a genuine requirements or design question, "
        "ask only that question."
    )


# ---------------------------------------------------------------------------------------------------------------
# classify_evidence_text (B1, B2 of the research report's test plan)
# ---------------------------------------------------------------------------------------------------------------


def test_classify_evidence_text_true_for_the_real_shape_of_runs_33_39_and_40():
    assert rc.classify_evidence_text(RUN_39_40_TEXT) is True
    assert rc.classify_evidence_text(RUN_33_TEXT) is True


def test_classify_evidence_text_false_for_a_genuine_question():
    assert rc.classify_evidence_text("Should slugify keep underscores? The spec says letters or digits.") is False


def test_classify_evidence_text_false_for_evidence_plus_a_genuine_question():
    """The veto is whole-text: ANY sentence that is really a design question turns the whole reason back into a
    genuine question, even when an earlier sentence alone would have matched EVIDENCE."""
    mixed = RUN_39_40_TEXT + " Should I also check the error message wording, or is that out of scope?"
    assert rc.classify_evidence_text(mixed) is False


def test_classify_evidence_text_false_for_an_ordinary_coder_block():
    assert rc.classify_evidence_text(
        "I am blocked: the API key for the payments provider is not in my .env. Which sandbox key should I use?"
    ) is False


def test_classify_evidence_text_false_for_a_changes_requested_with_a_concrete_code_finding():
    assert rc.classify_evidence_text(
        "CHANGES REQUESTED: the tests you added do not cover the empty-list branch in slugify(); please add one "
        "and confirm the edge case at line 42 is handled."
    ) is False


def test_classify_evidence_text_requires_both_ask_and_test_or_tool():
    # ASK alone, about something that is not tests or a terminal: not evidence-stalling.
    assert rc.classify_evidence_text(
        "I cannot verify the deployment configuration is correct without production access."
    ) is False
    # TEST alone, no claimed inability: a reviewer merely citing the tests while judging code.
    assert rc.classify_evidence_text("PASS: the tests already cover this branch and the diff looks correct.") is False


def test_classify_evidence_text_the_tool_alternative_to_test_also_counts():
    assert rc.classify_evidence_text(
        "I cannot run this in a terminal to confirm it behaves as claimed. Please provide execution output."
    ) is True


def test_classify_evidence_text_veto_phrases_win_even_without_a_question_mark():
    text = RUN_39_40_TEXT + " Please clarify whether this is intended before I continue."
    assert rc.classify_evidence_text(text) is False


def test_classify_evidence_text_handles_none_and_blank():
    assert rc.classify_evidence_text(None) is False
    assert rc.classify_evidence_text("") is False
    assert rc.classify_evidence_text("   ") is False


# ---------------------------------------------------------------------------------------------------------------
# classify_reviewer_run
# ---------------------------------------------------------------------------------------------------------------


def test_classify_reviewer_run_protocol_for_review_requested_by_a_reviewer_profile():
    run = {"id": 31, "profile": "reviewer", "outcome": "review_requested"}
    stop = rc.classify_reviewer_run(run, frozenset({"reviewer"}))
    assert stop is not None and stop.kind == rc.KIND_PROTOCOL and stop.run_id == 31 and stop.profile == "reviewer"


def test_classify_reviewer_run_evidence_for_blocked_and_for_changes_requested():
    blocked = {"id": 39, "profile": "reviewer", "outcome": "blocked", "summary": RUN_39_40_TEXT}
    stop = rc.classify_reviewer_run(blocked, frozenset({"reviewer"}))
    assert stop is not None and stop.kind == rc.KIND_EVIDENCE and stop.run_id == 39

    changes = {"id": 33, "profile": "reviewer", "outcome": "changes_requested", "summary": RUN_33_TEXT}
    stop2 = rc.classify_reviewer_run(changes, frozenset({"reviewer"}))
    assert stop2 is not None and stop2.kind == rc.KIND_EVIDENCE and stop2.run_id == 33


def test_classify_reviewer_run_reads_error_when_summary_is_absent():
    run = {"id": 40, "profile": "reviewer", "outcome": "blocked", "summary": None, "error": RUN_39_40_TEXT}
    stop = rc.classify_reviewer_run(run, frozenset({"reviewer"}))
    assert stop is not None and stop.kind == rc.KIND_EVIDENCE


def test_classify_reviewer_run_none_for_a_profile_not_in_reviewer_profiles():
    run = {"id": 1, "profile": "coder-1", "outcome": "blocked", "summary": RUN_39_40_TEXT}
    assert rc.classify_reviewer_run(run, frozenset({"reviewer"})) is None
    assert rc.classify_reviewer_run(run, frozenset()) is None


def test_classify_reviewer_run_none_for_a_reviewer_pass_or_a_genuine_question_or_a_real_finding():
    completed = {"id": 5, "profile": "reviewer", "outcome": "completed",
                 "metadata": {"review_outcome": "approved"}}
    assert rc.classify_reviewer_run(completed, frozenset({"reviewer"})) is None

    genuine = {"id": 6, "profile": "reviewer", "outcome": "blocked",
               "summary": "Should slugify keep underscores? The spec says letters or digits."}
    assert rc.classify_reviewer_run(genuine, frozenset({"reviewer"})) is None

    real_finding = {"id": 7, "profile": "reviewer", "outcome": "changes_requested",
                     "summary": "CHANGES REQUESTED: the empty-list branch is untested; please add a case."}
    assert rc.classify_reviewer_run(real_finding, frozenset({"reviewer"})) is None


def test_classify_reviewer_run_none_for_an_unclassified_type_or_a_still_running_run():
    assert rc.classify_reviewer_run("not a dict", frozenset({"reviewer"})) is None
    assert rc.classify_reviewer_run({"id": 8, "profile": "reviewer", "outcome": None}, frozenset({"reviewer"})) is None
    assert rc.classify_reviewer_run(
        {"id": 9, "profile": "reviewer", "outcome": "scheduled"}, frozenset({"reviewer"}),
    ) is None


# ---------------------------------------------------------------------------------------------------------------
# Event kind names: distinct, and stable strings a caller (recovery.py's D5 exclusion; tests) can rely on.
# ---------------------------------------------------------------------------------------------------------------


def test_event_kind_constants_are_distinct_strings():
    names = {rc.EVENT_DECISION, rc.EVENT_FAILURE, rc.EVENT_ANSWERED, rc.EVENT_PROVENANCE_BROKEN}
    assert len(names) == 4 and all(isinstance(n, str) and n for n in names)
    assert rc.EVENT_DECISION == "reviewer_contract_decision"
    assert rc.EVENT_FAILURE == "reviewer_contract_failure"
    assert rc.EVENT_ANSWERED == "reviewer_evidence_answered"
    assert rc.EVENT_PROVENANCE_BROKEN == "provenance_broken"
