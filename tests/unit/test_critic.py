"""Gate P plan critique (ASES-REV-01, ASES-REV-02, ASES-REV-03, ASES-LED-01). No real subprocess, no network: the
reviewer call is a fake `invoke`, the database is a temp SQLite file."""
import dataclasses
import hashlib
import json
import os
import pathlib
import re
import subprocess

import pytest

from ases import critic, db, events, hermes

_DROP = object()
SECRET = "sk-abcdefghijklmnop1234"


def verdict_dict(**overrides):
    """A valid PASS verdict as a dict; pass _DROP to remove a key."""
    base = {
        "review_status": "PASS", "summary": "Small, testable and correctly ordered.",
        "architecture_issues": [], "missing_cases": [], "security_issues": [], "test_gaps": [],
        "gate_tampering_suspected": False, "required_changes": [],
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not _DROP}


def verdict_text(**overrides):
    return json.dumps(verdict_dict(**overrides))


def changes_required(**overrides):
    return verdict_text(**{"review_status": "CHANGES_REQUIRED", "required_changes": ["add a scaffold task"], **overrides})


class FakeInvoke:
    """A stand-in for the hermes call: each reply is a stdout string (exit 0) or a (code, out, err) tuple. Running
    out of replies raises, so a call the code should not have made fails the test instead of passing quietly."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, profile, prompt, timeout):
        self.calls.append((profile, prompt, timeout))
        reply = self.replies.pop(0)
        return reply if isinstance(reply, tuple) else (0, reply, "")


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "ases.db")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "docs" / "ases").mkdir(parents=True)
    return root


@pytest.fixture
def plan_file(repo):
    path = repo / "docs" / "ases" / "plan.json"
    path.write_text('{"project": "demo", "tasks": []}\n', encoding="utf-8")
    return path


# --- plan_hash --------------------------------------------------------------------------------------------------


def test_plan_hash_is_sha256_of_the_bytes_with_lf_line_endings(tmp_path):
    path = tmp_path / "plan.json"
    path.write_bytes(b'{\r\n  "a": 1\r\n}\r\n')
    assert critic.plan_hash(path) == hashlib.sha256(b'{\n  "a": 1\n}\n').hexdigest()


def test_plan_hash_is_the_same_for_crlf_lf_and_cr_files(tmp_path):
    hashes = set()
    for name, eol in (("crlf", b"\r\n"), ("lf", b"\n"), ("cr", b"\r")):
        path = tmp_path / name
        path.write_bytes(b"{" + eol + b'"a": 1' + eol + b"}" + eol)
        hashes.add(critic.plan_hash(path))
    assert len(hashes) == 1


def test_plan_hash_is_stable_and_accepts_a_str_path(tmp_path):
    path = tmp_path / "plan.json"
    path.write_text("{}\n", encoding="utf-8")
    assert critic.plan_hash(path) == critic.plan_hash(str(path))
    assert re.fullmatch(r"[0-9a-f]{64}", critic.plan_hash(path))


def test_plan_hash_differs_when_the_content_differs(tmp_path):
    a, b, c = (tmp_path / n for n in "abc")
    a.write_text('{"n": 1}\n', encoding="utf-8")
    b.write_text('{"n": 2}\n', encoding="utf-8")
    c.write_text('{"n":  1}\n', encoding="utf-8")  # only inner whitespace differs: still a different plan file
    assert len({critic.plan_hash(a), critic.plan_hash(b), critic.plan_hash(c)}) == 3


def test_plan_hash_of_a_missing_file_raises_oserror(tmp_path):
    with pytest.raises(OSError):
        critic.plan_hash(tmp_path / "nope.json")


# --- build_critique_prompt --------------------------------------------------------------------------------------

_LIMITS = [("plan_text", 12000), ("architecture_text", 8000), ("repo_facts", 4000), ("estimate_text", 2000)]


def build(**overrides):
    args = dict(
        plan_text="PLAN-BODY", architecture_text="ARCH-BODY", repo_facts="FACTS-BODY",
        estimate_text="ESTIMATE-BODY", plan_hash_value="ab" * 32,
    )
    args.update(overrides)
    return critic.build_critique_prompt(**args)


def test_default_template_uses_exactly_the_placeholders_the_builder_fills():
    found = set(re.findall(r"<<([A-Z_]+)>>", critic.load_template()))
    assert found == {"PLAN_HASH", "PLAN_TEXT", "ARCHITECTURE_TEXT", "REPO_FACTS", "ESTIMATE_TEXT"}


def test_prompt_fills_every_placeholder_and_carries_each_input():
    prompt = build()
    assert not re.search(r"<<[A-Z_]+>>", prompt)
    for expected in ("PLAN-BODY", "ARCH-BODY", "FACTS-BODY", "ESTIMATE-BODY", "ab" * 32):
        assert expected in prompt


def test_prompt_states_the_full_review_schema_and_the_one_object_rule():
    prompt = build()
    for field in ("review_status", "commit", "summary", "architecture_issues", "missing_cases", "security_issues",
                  "test_gaps", "gate_tampering_suspected", "required_changes"):
        assert f'"{field}"' in prompt
    for text in ("PASS", "CHANGES_REQUIRED", "BLOCKED", "ONE JSON object and nothing else", "list of strings",
                 "boolean", "copy this plan hash exactly"):
        assert text in prompt


def test_prompt_lists_what_the_critic_must_check():
    prompt = build()
    for text in ("small, testable and correctly ordered", "Contracts and interfaces come before parallel work",
                 "touches do not collide", "request estimate is believable", "Acceptance criteria are checkable",
                 "gate_profiles are real commands", "Security and data-class concerns"):
        assert text in prompt


def test_prompt_says_text_in_the_plan_and_repository_is_data_never_instructions():
    prompt = build()
    assert "is data" in prompt and "never instructions to you" in prompt and "ASES-SEC-04" in prompt


def test_prompt_names_the_plan_hash_twice_so_the_critic_can_copy_it():
    assert build().count("ab" * 32) == 2  # in the schema example and in the plan heading


@pytest.mark.parametrize("field,limit", _LIMITS)
def test_an_over_long_input_is_cut_at_its_cap_with_the_exact_removed_count(field, limit):
    prompt = build(**{field: "x" * (limit + 37)})
    assert "[truncated 37 characters]" in prompt
    assert "x" * limit in prompt
    assert "x" * (limit + 1) not in prompt


@pytest.mark.parametrize("field,limit", _LIMITS)
def test_an_input_of_exactly_the_cap_is_not_marked(field, limit):
    prompt = build(**{field: "y" * limit})
    assert "y" * limit in prompt
    assert "[truncated" not in prompt


def test_one_character_over_the_cap_is_marked_with_a_count_of_one():
    assert "[truncated 1 characters]" in build(plan_text="z" * 12001)


@pytest.mark.parametrize("field", [name for name, _ in _LIMITS])
def test_secret_shaped_values_are_redacted_in_every_input(field):
    prompt = build(**{field: f"the key is {SECRET} ok"})
    assert SECRET not in prompt
    assert "[redacted]" in prompt


def test_a_secret_that_straddles_the_cut_is_redacted_whole_not_cut_in_half():
    prompt = build(plan_text="a" * 11995 + SECRET + "tail")
    assert SECRET[:8] not in prompt


def test_a_custom_template_is_used_and_an_unknown_placeholder_is_left_alone():
    prompt = critic.build_critique_prompt(
        plan_text="P", architecture_text="A", repo_facts="F", estimate_text="E", plan_hash_value="H",
        template="<<PLAN_TEXT>>|<<PLAN_HASH>>|<<NOPE>>{json}",
    )
    assert prompt == "P|H|<<NOPE>>{json}"


def test_input_text_that_looks_like_a_placeholder_is_not_expanded():
    prompt = critic.build_critique_prompt(
        plan_text="<<ARCHITECTURE_TEXT>>", architecture_text="REAL-ARCH", repo_facts="F", estimate_text="E",
        plan_hash_value="H", template="<<PLAN_TEXT>>|<<ARCHITECTURE_TEXT>>",
    )
    assert prompt == "<<ARCHITECTURE_TEXT>>|REAL-ARCH"


def test_a_none_input_becomes_empty_text():
    prompt = critic.build_critique_prompt(
        plan_text=None, architecture_text="A", repo_facts="F", estimate_text="E", plan_hash_value="H",
        template="[<<PLAN_TEXT>>]",
    )
    assert prompt == "[]"


def test_a_prompt_with_every_input_at_its_cap_stays_well_under_the_windows_command_line_limit():
    prompt = build(**{name: "q" * limit for name, limit in _LIMITS})
    assert len(prompt) < 31000  # the whole prompt is one argv entry; default_invoke enforces the exact limit


# --- parse_critique ---------------------------------------------------------------------------------------------

_LIST_FIELDS = ["architecture_issues", "missing_cases", "security_issues", "test_gaps", "required_changes"]


def test_clean_json_is_valid_and_every_field_is_read():
    text = verdict_text(
        review_status="CHANGES_REQUIRED", summary="Needs a scaffold.", architecture_issues=["a1"],
        missing_cases=["m1", "m2"], security_issues=["s1"], test_gaps=["t1"], gate_tampering_suspected=True,
        required_changes=["c1"], commit="abc123",
    )
    c = critic.parse_critique(text)
    assert c.valid and c.problems == ()
    assert (c.status, c.summary) == ("CHANGES_REQUIRED", "Needs a scaffold.")
    assert (c.architecture_issues, c.missing_cases, c.security_issues, c.test_gaps) == (["a1"], ["m1", "m2"], ["s1"], ["t1"])
    assert c.gate_tampering_suspected is True and c.required_changes == ["c1"] and c.plan_hash == "abc123"


def test_absent_optional_fields_default_to_empty_false_and_no_hash():
    c = critic.parse_critique('{"review_status": "PASS", "summary": "fine"}')
    assert c.valid
    assert (c.architecture_issues, c.missing_cases, c.security_issues, c.test_gaps, c.required_changes) == ([], [], [], [], [])
    assert c.gate_tampering_suspected is False and c.plan_hash is None


def test_extra_keys_are_tolerated():
    assert critic.parse_critique(verdict_text(reviewer_notes="x", confidence=0.4)).valid


def test_json_wrapped_in_prose_is_found():
    c = critic.parse_critique("Sure, here is my verdict.\n\n" + verdict_text() + "\n\nLet me know if you need more.")
    assert c.valid and c.status == "PASS"


@pytest.mark.parametrize("fence", ["```json\n{body}\n```", "```\n{body}\n```", "Verdict:\n```JSON\n{body}\n```\nThanks."])
def test_json_in_a_code_fence_is_found(fence):
    c = critic.parse_critique(fence.replace("{body}", verdict_text(summary="fenced")))
    assert c.valid and c.summary == "fenced"


def test_braces_and_quotes_inside_strings_do_not_end_the_object_early():
    summary = 'has {"a": "}" } and an escaped \\" quote and { an unbalanced brace'
    c = critic.parse_critique(verdict_text(summary=summary, required_changes=["fix } and {"]) + " trailing }")
    assert c.valid and c.summary == summary and c.required_changes == ["fix } and {"]


def test_a_brace_in_the_prose_before_the_object_is_passed_over():
    c = critic.parse_critique("Use the format {like this} and {\"not\": json,} first. " + verdict_text(summary="found"))
    assert c.valid and c.summary == "found"


def test_with_two_objects_the_first_wins():
    text = verdict_text(review_status="BLOCKED", summary="first") + "\n" + verdict_text(summary="second")
    c = critic.parse_critique(text)
    assert c.status == "BLOCKED" and c.summary == "first"


@pytest.mark.parametrize("raw,expected", [
    ("PASS", "PASS"), ("pass", "PASS"), ("Pass", "PASS"), ("  pass  ", "PASS"),
    ("CHANGES_REQUIRED", "CHANGES_REQUIRED"), ("changes_required", "CHANGES_REQUIRED"),
    ("Changes_Required", "CHANGES_REQUIRED"), ("BLOCKED", "BLOCKED"), ("blocked", "BLOCKED"),
])
def test_every_status_is_accepted_in_any_case_and_normalised_to_upper_case(raw, expected):
    c = critic.parse_critique(verdict_text(review_status=raw, required_changes=["x"]))
    assert c.valid and c.status == expected


@pytest.mark.parametrize("bad", ["MAYBE", "", "APPROVED", "CHANGES REQUIRED", 1, None, True, ["PASS"], {"a": 1}])
def test_an_unknown_or_non_string_status_is_a_problem(bad):
    c = critic.parse_critique(verdict_text(review_status=bad))
    assert not c.valid and c.status is None
    assert any(p.startswith("review_status is ") and "expected PASS, CHANGES_REQUIRED or BLOCKED" in p for p in c.problems)


def test_a_missing_status_is_a_problem():
    c = critic.parse_critique(verdict_text(review_status=_DROP))
    assert not c.valid and c.status is None and "review_status is missing" in c.problems[0]


@pytest.mark.parametrize("changes", [[], [""], ["   "], _DROP])
def test_changes_required_without_a_change_is_a_problem(changes):
    c = critic.parse_critique(verdict_text(review_status="CHANGES_REQUIRED", required_changes=changes))
    assert not c.valid and c.status == "CHANGES_REQUIRED"
    assert any("a change request with no change" in p for p in c.problems)


@pytest.mark.parametrize("status", ["PASS", "BLOCKED"])
def test_pass_and_blocked_need_no_required_changes(status):
    assert critic.parse_critique(verdict_text(review_status=status, required_changes=[])).valid


def test_a_missing_summary_is_a_problem():
    c = critic.parse_critique(verdict_text(summary=_DROP))
    assert not c.valid and "summary is missing" in c.problems


@pytest.mark.parametrize("bad,part", [("", "empty"), ("   ", "empty"), (5, "not a string"), (None, "not a string"), (["x"], "not a string")])
def test_an_empty_or_non_string_summary_is_a_problem(bad, part):
    c = critic.parse_critique(verdict_text(summary=bad))
    assert not c.valid and any(p.startswith("summary is") and part in p for p in c.problems)


@pytest.mark.parametrize("field", _LIST_FIELDS)
@pytest.mark.parametrize("bad", ["a string", 5, None, {"a": "b"}, True])
def test_a_value_that_is_not_a_list_is_a_problem_for_each_list_field(field, bad):
    c = critic.parse_critique(verdict_text(**{field: bad}))
    assert not c.valid and any(p.startswith(f"{field} is not a list") for p in c.problems)
    assert getattr(c, field) == []


@pytest.mark.parametrize("field", _LIST_FIELDS)
def test_a_non_string_list_item_is_a_problem_naming_its_index(field):
    c = critic.parse_critique(verdict_text(**{field: ["ok", 7]}))
    assert not c.valid and any(f"{field}[1] is not a string (got int)" in p for p in c.problems)
    assert getattr(c, field) == ["ok"]


@pytest.mark.parametrize("bad", ["true", 1, 0, None, "no", []])
def test_a_non_bool_tamper_flag_is_a_problem(bad):
    c = critic.parse_critique(verdict_text(gate_tampering_suspected=bad))
    assert not c.valid and c.gate_tampering_suspected is False
    assert any("gate_tampering_suspected is not a bool" in p for p in c.problems)


@pytest.mark.parametrize("bad", [123, None, ["abc"], True])
def test_a_non_string_commit_is_a_problem(bad):
    c = critic.parse_critique(verdict_text(commit=bad))
    assert not c.valid and c.plan_hash is None and any("commit is not a string" in p for p in c.problems)


def test_the_commit_is_the_plan_hash_and_a_blank_one_counts_as_absent():
    assert critic.parse_critique(verdict_text(commit="  abc  ")).plan_hash == "abc"
    blank = critic.parse_critique(verdict_text(commit="   "))
    assert blank.valid and blank.plan_hash is None


@pytest.mark.parametrize("text", ["", "   \n\t", "I think the plan is fine.", "no braces here at all", "[1, 2, 3]"])
def test_text_with_no_json_object_is_invalid_and_never_raises(text):
    c = critic.parse_critique(text)
    assert not c.valid and c.status is None and c.problems == ("the reply contains no JSON object",)


def test_broken_json_reports_the_parse_error():
    c = critic.parse_critique('{"review_status": "PASS", "summary": }')
    assert not c.valid and len(c.problems) == 1
    assert c.problems[0].startswith("the reply contains no valid JSON object (first parse error: ")


@pytest.mark.parametrize("value", [None, 5, b"{}", ["x"], {"a": 1}])
def test_a_reply_that_is_not_text_is_invalid(value):
    c = critic.parse_critique(value)
    assert not c.valid and "not text" in c.problems[0]


def test_an_object_that_is_not_a_verdict_lists_what_is_missing():
    c = critic.parse_critique('{"foo": 1}')
    assert not c.valid
    assert "review_status is missing, expected PASS, CHANGES_REQUIRED or BLOCKED" in c.problems
    assert "summary is missing" in c.problems


_PATHOLOGICAL = {
    "open_brackets": lambda: "[" * 50000,
    "open_braces": lambda: "{" * 50000,
    "nested_objects": lambda: '{"a":' * 50000,
    "nested_array_in_object": lambda: '{"a": ' + "[" * 50000,
    "brackets_then_object": lambda: "[" * 50000 + verdict_text(summary=_DROP),
}


@pytest.mark.parametrize("name", sorted(_PATHOLOGICAL))  # by name: a 50000 character param id overflows Windows' env limit
def test_pathologically_nested_input_never_raises_and_is_invalid(name):
    c = critic.parse_critique(_PATHOLOGICAL[name]())
    assert c.valid is False and c.problems


def test_every_problem_is_ascii_even_when_the_reply_is_not():
    c = critic.parse_critique('{"review_status": "caf\u00e9 \u2192 ok", "summary": "x"}')
    assert not c.valid and c.problems and all(p.isascii() for p in c.problems)


def test_valid_is_true_exactly_when_there_are_no_problems():
    for text in (verdict_text(), verdict_text(summary=""), "garbage", changes_required(), verdict_text(commit=3)):
        c = critic.parse_critique(text)
        assert c.valid == (c.problems == ())


def test_the_status_stays_readable_when_only_another_field_is_malformed():
    c = critic.parse_critique(verdict_text(summary=_DROP))
    assert not c.valid and c.status == "PASS"


def test_the_critique_dataclasses_are_frozen():
    c = critic.parse_critique(verdict_text())
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.valid = False
    with pytest.raises(dataclasses.FrozenInstanceError):
        critic.CritiqueRound(1, c).round = 2


# --- run_critique -----------------------------------------------------------------------------------------------


def run(repo, plan_file, invoke, **kwargs):
    kwargs.setdefault("estimate_text", "budget: 20 requests, about 5 minutes")
    return critic.run_critique(repo=repo, plan_path=plan_file, invoke=invoke, **kwargs)


def test_a_valid_first_reply_is_returned_after_one_call(repo, plan_file):
    fake = FakeInvoke(verdict_text())
    c = run(repo, plan_file, fake)
    assert c.valid and c.status == "PASS" and len(fake.calls) == 1


def test_the_call_gets_the_profile_the_timeout_and_a_prompt_with_the_plan_the_estimate_and_the_hash(repo, plan_file):
    fake = FakeInvoke(verdict_text())
    run(repo, plan_file, fake, profile="reviewer-x", timeout=77, estimate_text="EST-LINE")
    profile, prompt, timeout = fake.calls[0]
    assert (profile, timeout) == ("reviewer-x", 77)
    assert plan_file.read_text(encoding="utf-8") in prompt and "EST-LINE" in prompt
    assert critic.plan_hash(plan_file) in prompt


def test_the_defaults_are_the_reviewer_profile_and_a_900_second_timeout(repo, plan_file):
    fake = FakeInvoke(verdict_text())
    run(repo, plan_file, fake)
    assert (fake.calls[0][0], fake.calls[0][2]) == ("reviewer", 900)


def test_a_valid_verdict_is_bound_to_the_hash_of_the_plan_that_was_sent(repo, plan_file):
    c = run(repo, plan_file, FakeInvoke(verdict_text()))  # the critic quoted no commit
    assert c.plan_hash == critic.plan_hash(plan_file)


def test_a_quoted_hash_may_be_upper_case_padded_or_a_prefix_of_twelve_or_more_characters(repo, plan_file):
    expected = critic.plan_hash(plan_file)
    for quoted in (expected.upper(), expected[:12], expected[:40].upper(), f"  {expected}  "):
        c = run(repo, plan_file, FakeInvoke(verdict_text(commit=quoted)))
        assert c.valid and c.plan_hash == expected


def test_a_quoted_hash_shorter_than_twelve_characters_is_a_different_plan(repo, plan_file):
    reply = verdict_text(commit=critic.plan_hash(plan_file)[:11])
    c = run(repo, plan_file, FakeInvoke(reply, reply))
    assert not c.valid and "the critic reviewed a different plan" in c.problems[0]


def test_a_verdict_for_a_different_plan_is_invalid_after_the_one_repair(repo, plan_file):
    wrong = verdict_text(commit="0" * 64)
    fake = FakeInvoke(wrong, wrong)
    c = run(repo, plan_file, fake)
    assert not c.valid and len(fake.calls) == 2
    assert "the critic reviewed a different plan" in c.problems[0] and critic.plan_hash(plan_file) in c.problems[0]


def test_a_wrong_hash_can_be_corrected_on_the_repair_call(repo, plan_file):
    expected = critic.plan_hash(plan_file)
    fake = FakeInvoke(verdict_text(commit="f" * 64), verdict_text(commit=expected))
    c = run(repo, plan_file, fake)
    assert c.valid and len(fake.calls) == 2
    assert "the critic reviewed a different plan" in fake.calls[1][1]


def test_an_invalid_reply_gets_one_repair_call_that_quotes_the_exact_problems(repo, plan_file):
    bad = '{"review_status": "PASS"}'
    fake = FakeInvoke(bad, verdict_text(summary="repaired"))
    c = run(repo, plan_file, fake)
    assert c.valid and c.summary == "repaired" and len(fake.calls) == 2
    first_prompt, repair_prompt = fake.calls[0][1], fake.calls[1][1]
    assert repair_prompt.startswith(first_prompt) and len(repair_prompt) > len(first_prompt)
    problems = critic.parse_critique(bad).problems
    assert problems  # the summary is missing
    for problem in problems:
        assert f"- {problem}" in repair_prompt
    assert "reply again with only the corrected JSON object" in repair_prompt
    assert (fake.calls[1][0], fake.calls[1][2]) == ("reviewer", 900)


def test_a_reply_with_no_json_at_all_is_repaired_the_same_way(repo, plan_file):
    fake = FakeInvoke("I approve this plan!", verdict_text())
    assert run(repo, plan_file, fake).valid
    assert "- the reply contains no JSON object" in fake.calls[1][1]


def test_two_invalid_replies_return_the_second_invalid_critique_after_exactly_two_calls(repo, plan_file):
    fake = FakeInvoke("garbage", '{"review_status": "PASS"}')
    c = run(repo, plan_file, fake)
    assert not c.valid and len(fake.calls) == 2
    assert c.problems == ("summary is missing",)  # the second reply's problems, not the first's


def test_a_reply_in_a_fence_is_accepted_end_to_end(repo, plan_file):
    assert run(repo, plan_file, FakeInvoke("```json\n" + verdict_text() + "\n```")).valid


def test_a_nonzero_exit_is_an_invalid_critique_with_the_trimmed_stderr_and_no_repair_call(repo, plan_file):
    fake = FakeInvoke((3, "", "  provider unavailable: 503  \n"))
    c = run(repo, plan_file, fake)
    assert not c.valid and c.status is None and c.problems == ("provider unavailable: 503",)
    assert len(fake.calls) == 1


def test_a_nonzero_exit_without_stderr_falls_back_to_stdout_and_then_to_a_sentence(repo, plan_file):
    assert run(repo, plan_file, FakeInvoke((2, "the stdout tail", ""))).problems == ("the stdout tail",)
    assert run(repo, plan_file, FakeInvoke((2, "", " \n"))).problems == ("the reviewer call exited 2 with no output",)


def test_a_long_stderr_keeps_its_tail_and_is_redacted_and_ascii(repo, plan_file):
    err = "x" * 2000 + " END-MARKER " + SECRET + " caf\u00e9"
    (problem,) = run(repo, plan_file, FakeInvoke((1, "", err))).problems
    assert len(problem) <= 500 and problem.startswith("...") and "END-MARKER" in problem
    assert SECRET not in problem and problem.isascii()


def test_a_failed_repair_call_keeps_the_first_problems_and_adds_the_failure(repo, plan_file):
    fake = FakeInvoke("garbage", (1, "", "boom"))
    c = run(repo, plan_file, fake)
    assert not c.valid and len(fake.calls) == 2
    assert c.problems == ("the reply contains no JSON object", "the repair call failed: boom")


def test_a_missing_plan_file_is_an_invalid_critique_and_the_critic_is_never_called(repo):
    fake = FakeInvoke()
    c = critic.run_critique(repo=repo, plan_path=repo / "docs" / "ases" / "gone.json", estimate_text="e", invoke=fake)
    assert not c.valid and c.problems[0].startswith("cannot read the plan file gone.json") and fake.calls == []


def test_a_missing_architecture_file_is_fine_and_the_critic_is_told_without_the_absolute_path(repo, plan_file):
    fake = FakeInvoke(verdict_text())
    assert run(repo, plan_file, fake).valid
    prompt = fake.calls[0][1]
    assert "no architecture file was found at docs/ases/architecture.md" in prompt
    assert str(repo) not in prompt


def test_the_default_architecture_file_is_docs_ases_architecture_md(repo, plan_file):
    (repo / "docs" / "ases" / "architecture.md").write_text("ARCH-FROM-DEFAULT", encoding="utf-8")
    fake = FakeInvoke(verdict_text())
    run(repo, plan_file, fake)
    assert "ARCH-FROM-DEFAULT" in fake.calls[0][1] and "no architecture file" not in fake.calls[0][1]


def test_an_explicit_architecture_path_wins_and_a_missing_one_outside_the_repo_shows_only_its_name(repo, plan_file, tmp_path):
    (repo / "docs" / "ases" / "architecture.md").write_text("ARCH-FROM-DEFAULT", encoding="utf-8")
    other = tmp_path / "elsewhere.md"
    other.write_text("ARCH-ELSEWHERE", encoding="utf-8")
    fake = FakeInvoke(verdict_text(), verdict_text())
    run(repo, plan_file, fake, architecture_path=other)
    assert "ARCH-ELSEWHERE" in fake.calls[0][1] and "ARCH-FROM-DEFAULT" not in fake.calls[0][1]
    run(repo, plan_file, fake, architecture_path=tmp_path / "absent.md")
    assert "no architecture file was found at absent.md" in fake.calls[1][1] and str(tmp_path) not in fake.calls[1][1]


def test_supplied_repo_facts_replace_the_gathered_ones_and_gathered_ones_are_the_default(repo, plan_file):
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    fake = FakeInvoke(verdict_text(), verdict_text())
    run(repo, plan_file, fake)
    assert "top-level entries: " in fake.calls[0][1] and "src/" in fake.calls[0][1]
    run(repo, plan_file, fake, repo_facts="CUSTOM-FACTS")
    assert "CUSTOM-FACTS" in fake.calls[1][1] and "top-level entries" not in fake.calls[1][1]


def test_a_secret_in_the_plan_never_reaches_the_reviewer(repo, plan_file):
    plan_file.write_text(f'{{"note": "{SECRET}"}}\n', encoding="utf-8")
    fake = FakeInvoke(verdict_text())
    assert run(repo, plan_file, fake).valid
    assert SECRET not in fake.calls[0][1] and "[redacted]" in fake.calls[0][1]


def test_a_crlf_plan_is_sent_with_lf_line_endings_and_hashes_like_its_lf_twin(repo, plan_file):
    plan_file.write_bytes(b'{\r\n  "project": "demo"\r\n}\r\n')
    fake = FakeInvoke(verdict_text())
    c = run(repo, plan_file, fake)
    assert "\r" not in fake.calls[0][1]
    assert c.plan_hash == hashlib.sha256(b'{\n  "project": "demo"\n}\n').hexdigest()


def test_plan_bytes_that_are_not_utf8_do_not_raise(repo, plan_file):
    plan_file.write_bytes(b"\xff\xfe{}")
    fake = FakeInvoke(verdict_text())
    assert run(repo, plan_file, fake).valid and len(fake.calls) == 1


def test_a_missing_template_file_is_an_invalid_critique_and_the_critic_is_never_called(repo, plan_file, tmp_path, monkeypatch):
    monkeypatch.setattr(critic, "_TEMPLATE_PATH", tmp_path / "absent-template.md")
    fake = FakeInvoke()
    c = run(repo, plan_file, fake)
    assert not c.valid and c.problems[0].startswith("cannot load the critic prompt template") and fake.calls == []


def test_a_custom_template_is_passed_through(repo, plan_file):
    fake = FakeInvoke(verdict_text())
    run(repo, plan_file, fake, template="CUSTOM <<PLAN_HASH>>")
    assert fake.calls[0][1] == f"CUSTOM {critic.plan_hash(plan_file)}"


# --- gather_repo_facts ------------------------------------------------------------------------------------------


def _touch(root, *names):
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n", encoding="utf-8")


def test_repo_facts_for_an_empty_repository_say_so(tmp_path):
    root = tmp_path / "empty"
    _touch(root, "docs/ases/plan.json")
    facts = critic.gather_repo_facts(root)
    assert "repository: empty" in facts and "files outside docs/ases/: 0" in facts
    assert "planning files under docs/ases/: 1" in facts and "a scaffold task should come first" in facts


def test_repo_facts_describe_a_populated_repository(tmp_path):
    root = tmp_path / "app"
    _touch(root, "src/a.py", "src/b.py", "tests/test_a.py", "README.md", "pyproject.toml", "docs/ases/plan.json")
    (root / ".git").mkdir()
    facts = critic.gather_repo_facts(root)
    assert "git repository: yes" in facts and "files outside docs/ases/: 5" in facts
    assert "file types: .py x 3, .md x 1, .toml x 1" in facts
    assert "top-level entries: README.md, docs/, pyproject.toml, src/, tests/" in facts
    assert "project files present: pyproject.toml, README.md" in facts
    assert "scaffold" not in facts


def test_repo_facts_skip_vcs_cache_and_dependency_directories(tmp_path):
    root = tmp_path / "app"
    _touch(root, "real.py", "node_modules/x.js", ".git/config", "__pycache__/a.pyc", ".venv/lib.py")
    facts = critic.gather_repo_facts(root)
    assert "files outside docs/ases/: 1" in facts
    for name in ("node_modules", "__pycache__", ".venv"):
        assert name not in facts


def test_repo_facts_count_credential_looking_files_but_never_list_them(tmp_path):
    root = tmp_path / "app"
    _touch(root, ".env", "id_rsa", "server.pem", "api.key", "notes.txt")
    facts = critic.gather_repo_facts(root)
    assert "credential-looking files: 4 (names withheld)" in facts and "notes.txt" in facts
    for name in (".env", "id_rsa", "server.pem", "api.key"):
        assert name not in facts.replace("credential-looking", "")


def test_repo_facts_stop_scanning_at_the_limit_and_say_so(tmp_path):
    root = tmp_path / "app"
    _touch(root, *[f"f{i}.py" for i in range(10)])
    facts = critic.gather_repo_facts(root, max_files=4)
    assert "files outside docs/ases/: 4 (scan stopped at the limit)" in facts


def test_repo_facts_list_at_most_max_listed_top_level_entries(tmp_path):
    root = tmp_path / "app"
    _touch(root, *[f"f{i}.py" for i in range(6)])
    assert "top-level entries: f0.py, f1.py ... and 4 more" in critic.gather_repo_facts(root, max_listed=2)


def test_repo_facts_are_deterministic_ascii_and_do_not_leak_the_absolute_path(tmp_path):
    root = tmp_path / "app"
    _touch(root, "caf\u00e9.py", "b.py")
    facts = critic.gather_repo_facts(root)
    assert facts == critic.gather_repo_facts(root) and facts.isascii() and str(tmp_path) not in facts


def test_repo_facts_for_a_missing_directory_say_so(tmp_path):
    assert "does not exist" in critic.gather_repo_facts(tmp_path / "nope")


def test_repo_facts_survive_a_directory_that_cannot_be_listed(tmp_path, monkeypatch):
    root = tmp_path / "app"
    _touch(root, "a.py")

    def denied(self):
        raise PermissionError("denied")

    monkeypatch.setattr(pathlib.Path, "iterdir", denied)
    facts = critic.gather_repo_facts(root)
    assert "repository: app" in facts and "files outside docs/ases/: 1" in facts and "top-level entries" not in facts


# --- default_invoke (subprocess.run is monkeypatched: nothing real is started) -----------------------------------


class _Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


@pytest.fixture
def hermes_run(monkeypatch):
    """Patch hermes_path and subprocess.run; the returned dict records the call."""
    seen = {"calls": 0}

    def fake_run(argv, **kwargs):
        seen.update(argv=argv, kwargs=kwargs, calls=seen["calls"] + 1)
        return seen.get("result") or _Completed(0, "OUT", "ERR")

    monkeypatch.setattr(hermes, "hermes_path", lambda: "/bin/hermes")
    monkeypatch.setattr(subprocess, "run", fake_run)
    return seen


def test_default_invoke_runs_hermes_with_the_profile_and_prompt_and_no_toolsets(hermes_run):
    assert critic.default_invoke("reviewer", "PROMPT", 42) == (0, "OUT", "ERR")
    assert hermes_run["argv"] == ["/bin/hermes", "-p", "reviewer", "-z", "PROMPT"]
    assert "-t" not in hermes_run["argv"] and "--toolsets" not in hermes_run["argv"]
    kwargs = hermes_run["kwargs"]
    assert kwargs["timeout"] == 42 and kwargs["encoding"] == "utf-8" and kwargs["errors"] == "replace"
    assert kwargs["capture_output"] is True and kwargs["text"] is True


def test_default_invoke_starts_hermes_with_a_credential_scrubbed_environment(hermes_run, monkeypatch):
    """ASES-CFG-05 (blueprint 10.2): a provider key exported into the shell that runs `swarm critique` must not
    reach the reviewer's hermes process, while PATH (and SYSTEMROOT on Windows) still must. Nothing else about the
    call changes."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-adversarial-12345")
    monkeypatch.setenv("ASES_HARMLESS_SETTING", "kept")

    assert critic.default_invoke("reviewer", "p", 5) == (0, "OUT", "ERR")

    env = hermes_run["kwargs"].get("env")
    assert env is not None, "default_invoke passed no env=, so the reviewer inherits the whole parent environment"
    assert "OPENROUTER_API_KEY" not in {name.upper() for name in env}
    assert "sk-test-adversarial-12345" not in env.values()
    assert env["ASES_HARMLESS_SETTING"] == "kept" and env["PATH"] == os.environ["PATH"]
    if os.name == "nt":
        assert env["SYSTEMROOT"] == os.environ["SYSTEMROOT"]
    assert {k: v for k, v in hermes_run["kwargs"].items() if k != "env"} == {
        "capture_output": True, "text": True, "timeout": 5, "encoding": "utf-8", "errors": "replace",
    }


def test_default_invoke_turns_none_output_into_empty_strings(hermes_run):
    hermes_run["result"] = _Completed(0, None, None)
    assert critic.default_invoke("reviewer", "p", 5) == (0, "", "")


def test_default_invoke_passes_a_nonzero_exit_code_through(hermes_run):
    hermes_run["result"] = _Completed(7, "", "bad")
    assert critic.default_invoke("reviewer", "p", 5) == (7, "", "bad")


def test_default_invoke_maps_a_timeout_to_a_nonzero_code_and_never_raises(monkeypatch):
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 5)

    monkeypatch.setattr(hermes, "hermes_path", lambda: "/bin/hermes")
    monkeypatch.setattr(subprocess, "run", fake_run)
    code, out, err = critic.default_invoke("reviewer", "p", 5)
    assert code == -1 and out == "" and err == "the reviewer did not answer within 5s"


def test_default_invoke_maps_a_missing_hermes_and_an_os_error(monkeypatch):
    def missing():
        raise hermes.HermesNotFound("`hermes` is not on PATH")

    monkeypatch.setattr(hermes, "hermes_path", missing)
    assert critic.default_invoke("reviewer", "p", 5) == (-1, "", "`hermes` is not on PATH")

    def broken(argv, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(hermes, "hermes_path", lambda: "/bin/hermes")
    monkeypatch.setattr(subprocess, "run", broken)
    assert critic.default_invoke("reviewer", "p", 5) == (-1, "", "hermes could not be run: boom")


def test_default_invoke_refuses_a_prompt_too_long_for_a_windows_command_line(hermes_run, monkeypatch):
    monkeypatch.setattr(critic, "_IS_WINDOWS", True)
    code, out, err = critic.default_invoke("reviewer", "p" * 40000, 5)
    assert code == -1 and out == "" and "over the Windows limit" in err and "shorten docs/ases/plan.json" in err
    assert hermes_run["calls"] == 0


def test_default_invoke_counts_quote_escaping_toward_the_windows_limit(hermes_run, monkeypatch):
    monkeypatch.setattr(critic, "_IS_WINDOWS", True)
    prompt = '"' * 20000  # 20000 characters, but every quote becomes two on the command line
    assert critic.default_invoke("reviewer", prompt, 5)[0] == -1 and hermes_run["calls"] == 0


def test_default_invoke_lets_a_normal_prompt_through_on_windows_and_a_long_one_elsewhere(hermes_run, monkeypatch):
    monkeypatch.setattr(critic, "_IS_WINDOWS", True)
    assert critic.default_invoke("reviewer", "x" * 1000, 5)[0] == 0
    monkeypatch.setattr(critic, "_IS_WINDOWS", False)
    assert critic.default_invoke("reviewer", "x" * 50000, 5)[0] == 0 and hermes_run["calls"] == 2


# --- recording, counting and looking up verdicts ----------------------------------------------------------------


def crit(status="PASS", *, valid=True, plan_hash="h1", summary="s", **fields):
    return critic.PlanCritique(valid=valid, status=status, summary=summary, plan_hash=plan_hash, **fields)


def stored(conn):
    return [json.loads(r["payload"]) for r in conn.execute("SELECT payload FROM events WHERE kind = 'plan_critique' ORDER BY id")]


def test_record_critique_stores_one_plan_critique_event_with_the_documented_payload(conn):
    c = crit(
        "CHANGES_REQUIRED", plan_hash="H", summary="sum", architecture_issues=["a"], missing_cases=["m"],
        security_issues=["s"], test_gaps=["t"], gate_tampering_suspected=True, required_changes=["r1", "r2"],
    )
    assert critic.record_critique(conn, "proj", 2, c) is None
    assert conn.execute("SELECT kind FROM events").fetchone()["kind"] == "plan_critique"
    assert stored(conn) == [{
        "project": "proj", "round": 2, "status": "CHANGES_REQUIRED", "valid": True, "plan_hash": "H",
        "summary": "sum", "architecture_issues": ["a"], "missing_cases": ["m"], "security_issues": ["s"],
        "test_gaps": ["t"], "gate_tampering_suspected": True, "required_changes": ["r1", "r2"], "problems": [],
    }]


def test_record_critique_redacts_secret_shaped_values(conn):
    c = crit("CHANGES_REQUIRED", summary=f"leaked {SECRET}", required_changes=[f"rotate {SECRET}"],
             security_issues=[SECRET], problems=(f"bad {SECRET}",))
    critic.record_critique(conn, "proj", 1, c)
    raw = conn.execute("SELECT payload FROM events").fetchone()["payload"]
    assert SECRET not in raw and "[redacted]" in raw


def test_an_invalid_critique_is_recorded_too_with_its_problems(conn):
    critic.record_critique(conn, "proj", 1, critic.PlanCritique(valid=False, problems=("boom",)))
    (payload,) = stored(conn)
    assert payload["valid"] is False and payload["problems"] == ["boom"]
    assert payload["status"] is None and payload["plan_hash"] is None


def test_rounds_used_is_zero_before_anything_is_recorded(conn):
    assert critic.critique_rounds_used(conn, "p") == 0


def test_rounds_used_counts_only_valid_changes_required_critiques_of_this_project(conn):
    critic.record_critique(conn, "p", 1, crit("CHANGES_REQUIRED", plan_hash="a"))
    critic.record_critique(conn, "p", 2, crit("CHANGES_REQUIRED", plan_hash="b"))
    critic.record_critique(conn, "p", 3, crit("PASS", plan_hash="c"))
    critic.record_critique(conn, "p", 3, crit("BLOCKED", plan_hash="c"))
    critic.record_critique(conn, "p", 3, crit("CHANGES_REQUIRED", valid=False, plan_hash="c"))  # malformed: nothing went back
    critic.record_critique(conn, "other", 1, crit("CHANGES_REQUIRED"))
    events.record(conn, "not_a_critique", {"project": "p", "status": "CHANGES_REQUIRED", "valid": True})
    assert critic.critique_rounds_used(conn, "p") == 2
    assert critic.critique_rounds_used(conn, "other") == 1


def test_rounds_used_grows_by_one_per_recorded_change_request(conn):
    for n in range(4):
        assert critic.critique_rounds_used(conn, "p") == n
        critic.record_critique(conn, "p", n + 1, crit("CHANGES_REQUIRED", plan_hash=f"h{n}"))


def test_latest_critique_is_none_when_nothing_matches(conn):
    assert critic.latest_critique(conn, "p", "h1") is None
    critic.record_critique(conn, "p", 1, crit(plan_hash="h1"))
    assert critic.latest_critique(conn, "p", "h2") is None
    assert critic.latest_critique(conn, "q", "h1") is None


def test_latest_critique_returns_the_newest_payload_for_that_hash(conn):
    critic.record_critique(conn, "p", 1, crit("PASS", summary="first"))
    critic.record_critique(conn, "p", 2, crit("CHANGES_REQUIRED", summary="second", required_changes=["x"]))
    assert critic.latest_critique(conn, "p", "h1")["summary"] == "second"
    critic.record_critique(conn, "p", 3, crit("PASS", summary="third"))
    latest = critic.latest_critique(conn, "p", "h1")
    assert latest["summary"] == "third" and latest["status"] == "PASS" and latest["round"] == 3


def test_latest_critique_ignores_other_hashes_and_other_projects(conn):
    critic.record_critique(conn, "p", 1, crit(plan_hash="h1", summary="A"))
    critic.record_critique(conn, "p", 2, crit(plan_hash="h2", summary="B"))
    critic.record_critique(conn, "q", 1, crit(plan_hash="h1", summary="C"))
    assert critic.latest_critique(conn, "p", "h1")["summary"] == "A"
    assert critic.latest_critique(conn, "p", "h2")["summary"] == "B"
    assert critic.latest_critique(conn, "q", "h1")["summary"] == "C"


def test_latest_critique_matches_the_hash_exactly_not_by_case_or_prefix(conn):
    critic.record_critique(conn, "p", 1, crit(plan_hash="abcdef0123456789"))
    assert critic.latest_critique(conn, "p", "ABCDEF0123456789") is None
    assert critic.latest_critique(conn, "p", "abcdef012345") is None
    assert critic.latest_critique(conn, "p", "abcdef0123456789") is not None


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_a_blank_hash_matches_nothing_even_when_an_event_never_learned_its_hash(conn, blank):
    critic.record_critique(conn, "p", 1, crit("PASS", plan_hash=None))
    assert critic.latest_critique(conn, "p", blank) is None
    assert critic.is_plan_approved_by_critic(conn, "p", blank) is False


def test_rows_whose_payload_is_not_a_json_object_are_skipped(conn):
    for payload in ("not json", "[1, 2]", "42", "null"):
        conn.execute("INSERT INTO events (ts, kind, payload) VALUES ('t', 'plan_critique', ?)", (payload,))
    critic.record_critique(conn, "p", 1, crit("CHANGES_REQUIRED"))
    assert critic.critique_rounds_used(conn, "p") == 1
    assert critic.latest_critique(conn, "p", "h1")["status"] == "CHANGES_REQUIRED"


# --- is_plan_approved_by_critic ---------------------------------------------------------------------------------


def test_a_valid_pass_for_the_exact_hash_approves_and_nothing_else_does(conn):
    assert critic.is_plan_approved_by_critic(conn, "p", "h1") is False  # no critique yet
    critic.record_critique(conn, "p", 1, crit("PASS", plan_hash="h1"))
    assert critic.is_plan_approved_by_critic(conn, "p", "h1") is True
    assert critic.is_plan_approved_by_critic(conn, "p", "h2") is False  # an edited plan has another hash
    assert critic.is_plan_approved_by_critic(conn, "q", "h1") is False  # another project


@pytest.mark.parametrize("status,valid", [("CHANGES_REQUIRED", True), ("BLOCKED", True), (None, False), ("PASS", False)])
def test_only_a_valid_pass_approves(conn, status, valid):
    kwargs = {"required_changes": ["x"]} if status == "CHANGES_REQUIRED" else {}
    critic.record_critique(conn, "p", 1, crit(status, valid=valid, **kwargs))
    assert critic.is_plan_approved_by_critic(conn, "p", "h1") is False


def test_a_later_non_pass_or_malformed_critique_of_the_same_plan_revokes_an_earlier_pass(conn):
    critic.record_critique(conn, "p", 1, crit("PASS"))
    critic.record_critique(conn, "p", 2, crit("CHANGES_REQUIRED", required_changes=["x"]))
    assert critic.is_plan_approved_by_critic(conn, "p", "h1") is False
    critic.record_critique(conn, "p", 3, crit("PASS"))
    assert critic.is_plan_approved_by_critic(conn, "p", "h1") is True
    critic.record_critique(conn, "p", 4, crit("PASS", valid=False))
    assert critic.is_plan_approved_by_critic(conn, "p", "h1") is False


def test_a_pass_for_an_earlier_draft_does_not_approve_the_rewrite(conn):
    critic.record_critique(conn, "p", 1, crit("PASS", plan_hash="draft-1"))
    critic.record_critique(conn, "p", 2, crit("CHANGES_REQUIRED", plan_hash="draft-2", required_changes=["x"]))
    assert critic.is_plan_approved_by_critic(conn, "p", "draft-2") is False
    assert critic.is_plan_approved_by_critic(conn, "p", "draft-1") is True  # that exact plan did pass


# --- next_step --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("status,valid,used,expected", [
    ("PASS", True, 0, "approve"), ("PASS", True, 2, "approve"), ("PASS", True, 9, "approve"),
    ("CHANGES_REQUIRED", True, 0, "replan"), ("CHANGES_REQUIRED", True, 1, "replan"),
    ("CHANGES_REQUIRED", True, 2, "ask_user"), ("CHANGES_REQUIRED", True, 3, "ask_user"),
    ("BLOCKED", True, 0, "ask_user"), ("BLOCKED", True, 5, "ask_user"),
    ("PASS", False, 0, "ask_user"), ("CHANGES_REQUIRED", False, 0, "ask_user"), (None, False, 0, "ask_user"),
    ("MAYBE", True, 0, "ask_user"), (None, True, 0, "ask_user"),
])
def test_next_step_table(status, valid, used, expected):
    assert critic.next_step(crit(status, valid=valid), used) == expected


def test_next_step_honours_max_rounds_and_defaults_to_two():
    c = crit("CHANGES_REQUIRED")
    assert critic.next_step(c, 0, max_rounds=0) == "ask_user"
    assert critic.next_step(c, 2, max_rounds=3) == "replan"
    assert critic.next_step(c, 3, max_rounds=3) == "ask_user"
    assert critic.next_step(c, 1) == "replan" and critic.next_step(c, 2) == "ask_user"


def test_the_step_names_are_the_documented_strings():
    assert (critic.APPROVE, critic.REPLAN, critic.ASK_USER) == ("approve", "replan", "ask_user")


# --- lead_feedback_prompt ---------------------------------------------------------------------------------------


def feedback(critique=None, **kwargs):
    kwargs.setdefault("request", "Build a small CLI")
    kwargs.setdefault("plan_path", "/abs/repo/docs/ases/plan.json")
    return critic.lead_feedback_prompt(
        critique or crit("CHANGES_REQUIRED", summary="Too big.", required_changes=["split T1", "add a scaffold task"]),
        **kwargs,
    )


def test_lead_feedback_names_the_request_the_path_the_summary_and_numbers_the_changes():
    text = feedback()
    assert "Build a small CLI" in text and "/abs/repo/docs/ases/plan.json" in text and "Too big." in text
    assert "1. split T1" in text and "2. add a scaffold task" in text
    assert "rewrite the plan file; do not argue; keep it small" in text


def test_lead_feedback_includes_the_other_findings_only_when_there_are_some():
    plain = feedback()
    assert "Architecture issues" not in plain and "Security issues" not in plain
    full = feedback(crit(
        "CHANGES_REQUIRED", required_changes=["x"], architecture_issues=["arch-1"], missing_cases=["miss-1"],
        security_issues=["sec-1"], test_gaps=["gap-1"],
    ))
    for expected in ("Architecture issues:", "- arch-1", "Missing cases:", "- miss-1", "Security issues:", "- sec-1",
                     "Test gaps:", "- gap-1"):
        assert expected in full


def test_lead_feedback_is_ascii_even_when_every_input_is_not():
    text = feedback(
        crit("CHANGES_REQUIRED", summary="caf\u00e9 \u2192 done", required_changes=["r\u00e9sum\u00e9"]),
        request="d\u00e9ployer \u2192 prod", plan_path="C:/d\u00e9p/plan.json",
    )
    assert text.isascii() and "caf\\xe9" in text


def test_lead_feedback_redacts_secret_shaped_values_everywhere():
    text = feedback(
        crit("CHANGES_REQUIRED", summary=f"s {SECRET}", required_changes=[f"c {SECRET}"], security_issues=[SECRET]),
        request=f"r {SECRET}", plan_path=f"/x/{SECRET}/plan.json",
    )
    assert SECRET not in text and text.count("[redacted]") == 5


def test_lead_feedback_is_bounded_and_the_cut_is_marked():
    c = crit("CHANGES_REQUIRED", summary="s" * 5000, required_changes=["c" * 5000] * 40)
    text = feedback(c, request="q" * 9000)
    assert "(and 25 more not shown here)" in text and "[truncated" in text
    assert "c" * 601 not in text and "s" * 1501 not in text and "q" * 2001 not in text
    assert len(text) < 15 * 700 + 6000


def test_lead_feedback_skips_blank_changes_and_points_at_the_summary_when_none_are_left():
    assert "1. real" in feedback(crit("CHANGES_REQUIRED", required_changes=["", "  ", "real"]))
    assert "the reviewer listed none in detail; work from the summary" in feedback(crit("CHANGES_REQUIRED", required_changes=[""]))


def test_lead_feedback_accepts_a_path_object():
    path = pathlib.PurePosixPath("/abs/repo/docs/ases/plan.json")  # same text on every platform
    assert "use this exact absolute path, do not rely on any working directory): /abs/repo/docs/ases/plan.json" in feedback(plan_path=path)


# --- the whole loop ---------------------------------------------------------------------------------------------


def test_section_22_14_two_change_requests_go_back_to_the_lead_then_the_user_decides_and_nothing_is_approved(conn, repo, plan_file):
    fake = FakeInvoke(changes_required(), changes_required(), changes_required())
    steps = []
    for round_no in (1, 2, 3):
        used = critic.critique_rounds_used(conn, "demo")
        c = run(repo, plan_file, fake)
        assert c.valid and c.status == "CHANGES_REQUIRED"
        steps.append(critic.next_step(c, used))
        critic.record_critique(conn, "demo", round_no, c)
    assert steps == ["replan", "replan", "ask_user"]
    assert critic.critique_rounds_used(conn, "demo") == 3
    assert critic.is_plan_approved_by_critic(conn, "demo", critic.plan_hash(plan_file)) is False


def test_a_rewritten_plan_needs_its_own_pass_before_it_can_be_approved(conn, repo, plan_file):
    fake = FakeInvoke(changes_required(), verdict_text())
    first_hash = critic.plan_hash(plan_file)
    c1 = run(repo, plan_file, fake)
    assert critic.next_step(c1, critic.critique_rounds_used(conn, "demo")) == "replan"
    critic.record_critique(conn, "demo", 1, c1)

    plan_file.write_text('{"project": "demo", "tasks": [{"key": "T1"}]}\n', encoding="utf-8")  # the Lead rewrote it
    second_hash = critic.plan_hash(plan_file)
    assert second_hash != first_hash
    c2 = run(repo, plan_file, fake)
    assert critic.next_step(c2, critic.critique_rounds_used(conn, "demo")) == "approve"
    critic.record_critique(conn, "demo", 2, c2)

    assert critic.is_plan_approved_by_critic(conn, "demo", second_hash) is True
    assert critic.is_plan_approved_by_critic(conn, "demo", first_hash) is False


def test_the_critique_round_dataclass_holds_the_round_and_the_critique():
    c = crit()
    r = critic.CritiqueRound(round=2, critique=c)
    assert (r.round, r.critique) == (2, c)


@pytest.mark.parametrize("relative", ["src/ases/critic.py", "prompts/critic.md", "tests/unit/test_critic.py"])
def test_the_files_of_this_package_hold_no_em_dash_section_sign_or_other_non_ascii(relative):
    text = (pathlib.Path(__file__).resolve().parents[2] / relative).read_text(encoding="utf-8")
    assert "\u2014" not in text and "\u00a7" not in text and text.isascii()
