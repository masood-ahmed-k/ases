"""evals.py and evalkit/: the evaluation harness (blueprint Appendix D, phase 7).

No test here calls a model or a provider. Every model call goes through a fake `invoke`; hermes.session_usage and
hermes.hermes_path are monkeypatched where default_invoke is exercised; the databases are temp sqlite files; E4, E5, E6
and E8 run real pytest and real git in temp directories, because what those scorers do IS running a suite and reading a
repository (they are kept small so the file stays fast).
"""
import json
import os
import pathlib
import subprocess
import types

import pytest

from ases import db, evals, events, hermes, ledger
from ases.evalkit import codeeval, codetasks, tasks as tasks_mod, text, texttasks
from ases.evalkit.model import EvalError, EvalRefused, InvokeResult, Score

TASKS = tasks_mod.TASKS

# ---- known answers: what a competent model would say, and what a lazy one would ----------------------------------------

E1_GOOD = """Assumptions
1. Users are customers who order, shop staff who prepare orders, and an admin who manages shops and prices.
2. A customer can cancel or change an order up to 24 hours before pickup; after that it is fixed, and refunds follow the shop policy.
3. Payment is taken online by card through a payment provider (Stripe); nothing is paid at pickup.
4. Reminders go by email and SMS, one 24 hours before pickup and one 2 hours before.
5. Fast means pages load in under 2 seconds and the system handles 50 concurrent users at peak times.
6. Secure means HTTPS everywhere, hashed passwords and personal data handled under GDPR.
7. Works on phones means a responsive web app in current iOS and Android browsers, not a native app.
8. Shops see a dashboard of upcoming orders grouped by pickup time slot.
9. Grow means up to 20 shops and 500 orders a day within two years; the design should scale horizontally.
Open questions
- Are allergens tracked?
"""
E1_BAD = "I would build a website where customers order cakes. Assumptions: it will be fast."
E1_ECHO = (
    "1. Customers order cakes online.\n2. It is fast.\n3. It is secure.\n4. It works on phones.\n"
    "5. Shops see what is coming up.\n6. Reminders help.\n7. It can grow with us.\nThese are my assumptions.\n"
)

E2_GOOD = """Components
1. Intake API - receives events. `POST /events -> 202 {message_id}`
2. Queue (durable broker) - buffers messages. `enqueue(message) -> None`, `dequeue() -> message`
3. Template renderer - `render(template_id, context) -> text`
4. Email adapter and SMS adapter - `send(channel, address, text) -> DeliveryResult`
5. Retry scheduler - retries with exponential backoff: `schedule_retry(message_id, attempt) -> datetime`
6. Dead-letter store - `park(message_id, reason) -> None`
7. Audit log - `record_attempt(message_id, channel, outcome) -> None`
8. Status API - `GET /messages/{id}/status -> {state, attempts}`
9. Idempotency store - `seen(event_id) -> bool` so a duplicate event is dropped
Flow: intake -> dedupe -> queue -> render -> deliver -> audit.
"""
E2_BAD = "Use microservices with a message queue and a database. Keep it simple and scalable."

E3_GOOD = """Q1: INVENTORY_DB, read in inventory/config.py (database_path).
Q2: 37 (MAX_ITEMS_PER_ORDER in config.py).
Q3: CatalogNotFoundError, defined in storage.py.
Q4: bulk_discount in pricing.py; it starts at 25 units.
Q5: the report sub-command; items with a quantity of 4 or less are low stock.
Q6: cli.py imports config, pricing and storage.
"""

E7_GOOD = """- get_user: SQL injection, the name is formatted into the query; use parameterized queries.
- register: MD5 password hashing is weak and unsalted; use bcrypt.
- download: path traversal via the file parameter; validate the path.
- ping: command injection through shell=True with the host parameter.
- restore: insecure deserialization, pickle.loads on request data.
- db(): hard-coded database password in source.
"""

E9_CALL_OK = json.dumps(
    {"tool": "create_ticket", "arguments": {"title": "Payment page returns 500", "priority": "high", "assignee": "Maria"}}
)
E9_CALL_URGENT = E9_CALL_OK.replace('"high"', '"urgent"')

E10_GOOD = (
    "review_status: CHANGES_REQUIRED\nfindings:\n"
    "- file: shop/pricing.py | defect: off-by-one | why: the tiers start at 10 and at 50 units but the code uses > so "
    "exactly 10 or 50 units get no discount.\n"
)

E4_GOOD_DIFF = """The loop stops too early. Fix:
```diff
--- a/textkit/chunks.py
+++ b/textkit/chunks.py
@@ -10,4 +10,4 @@
     if size <= 0:
         raise ValueError("size must be positive")
-    return [list(items[i:i + size]) for i in range(0, len(items) - size + 1, size)]
+    return [list(items[i:i + size]) for i in range(0, len(items), size)]
```
"""
E4_GOOD_FILE = (
    "textkit/chunks.py\n```python\n"
    + codetasks.E4_FILES["textkit/chunks.py"].replace(codetasks.E4_BUGGY_LINE, codetasks.E4_FIXED_LINE)
    + "```\n"
)

E5_GOOD = '''"""Invoice totals for the shop."""

SALES_TAX_RATE = 0.2


def line_total(order):
    if order["qty"] < 0:
        raise ValueError("negative quantity")
    return order["qty"] * order["unit_price"]


def summarize_orders(orders):
    """Return the subtotal, tax, total and line count for a list of order dicts."""
    lines = 0
    subtotal = 0.0
    for order in orders:
        subtotal += line_total(order)
        lines += 1
    tax = round(subtotal * SALES_TAX_RATE, 2)
    return {
        "subtotal": round(subtotal, 2),
        "tax": tax,
        "total": round(subtotal + tax, 2),
        "lines": lines,
    }
'''

# A test file that passes on the reference and kills every one of the ten seeded mutants.
E6_GOLD = '''import pytest
from intervals import merge_intervals, total_covered


def test_empty_input_gives_empty_list():
    assert merge_intervals([]) == []


def test_single_interval_is_returned_as_tuple():
    assert merge_intervals([(1, 3)]) == [(1, 3)]


def test_disjoint_intervals_are_kept_apart():
    assert merge_intervals([(1, 2), (4, 5)]) == [(1, 2), (4, 5)]


def test_overlapping_intervals_merge():
    assert merge_intervals([(1, 4), (3, 6)]) == [(1, 6)]


def test_touching_intervals_merge():
    assert merge_intervals([(1, 2), (2, 3)]) == [(1, 3)]


def test_unsorted_input_is_sorted_first():
    assert merge_intervals([(5, 6), (1, 2), (2, 4)]) == [(1, 4), (5, 6)]


def test_contained_interval_does_not_shrink_the_result():
    assert merge_intervals([(1, 10), (2, 3)]) == [(1, 10)]


def test_start_after_end_is_rejected():
    with pytest.raises(ValueError):
        merge_intervals([(3, 1)])


def test_zero_length_interval_is_valid():
    assert merge_intervals([(2, 2)]) == [(2, 2)]


def test_input_list_is_not_modified():
    data = [(5, 6), (1, 2)]
    merge_intervals(data)
    assert data == [(5, 6), (1, 2)]


def test_total_covered_counts_union_length():
    assert total_covered([(1, 3), (2, 5), (8, 9)]) == 5


def test_total_covered_of_nothing_is_zero():
    assert total_covered([]) == 0


def test_total_covered_ignores_gaps():
    assert total_covered([(1, 2), (5, 6)]) == 2
'''
E6_WEAK = "```python\nfrom intervals import merge_intervals\n\n\ndef test_basic():\n    assert merge_intervals([(1, 3), (2, 6)]) == [(1, 6)]\n```"

# One known-good reply per standalone task, keyed by a phrase only that task's prompt contains.
GOOD_REPLIES = {
    "requirements analyst": E1_GOOD,
    "You are the architect": E2_GOOD,
    "exact absolute path": E3_GOOD,
    "has a failing test": E4_GOOD_DIFF,
    "Refactor this package": "billing/invoice.py\n```python\n" + E5_GOOD + "```",
    "Write a pytest test file": "```python\n" + E6_GOLD + "```",
    "You are a security reviewer": E7_GOOD,
    "You can call the tools below": E9_CALL_OK,
    "independent reviewer of a change": E10_GOOD,
}

R = InvokeResult(0, "", "", 1.0, 1, 10, 10)  # a stand-in invoke result for calling a scorer directly


def _work(tmp_path, name="work"):
    path = tmp_path / name
    path.mkdir()
    return path


# =====================================================================================================================
# evalkit/text.py
# =====================================================================================================================


def test_normalize_folds_case_accents_and_punctuation():
    accented = "SQL-injection in `get_user`, caf" + chr(0xE9) + " **XSS**!"
    assert text.normalize(accented) == " sql injection in get user cafe xss "
    assert text.normalize("") == "  "


def test_ascii_safe_escapes_non_ascii_and_clip_marks_the_cut():
    assert text.ascii_safe("a" + chr(0x2192) + "b") == "a\\u2192b"
    assert text.clip("abcdefghij", 6) == "abc..."
    assert text.clip("abc", 6) == "abc"


def test_topic_hits_match_any_pattern_and_ignore_list_numbering():
    topics = {"count": (r"\b\d+ shops\b",), "sec": (r"\bbcrypt\b", r"\bargon2\b")}
    assert text.topic_hits("We use Argon2.", topics) == {"count": False, "sec": True}
    # the '5' of '5. Shops see...' is a list number, not a count of shops
    assert text.topic_hits("5. Shops see what is coming up.", topics)["count"] is False
    assert text.topic_hits("We plan for 5 shops.", topics)["count"] is True


def test_matches_all_and_any_use_the_lower_cased_text_with_underscores_kept():
    assert text.matches_all("Use BULK_DISCOUNT in pricing.py", [r"bulk_discount", r"pricing\.py"]) is True
    assert text.matches_all("Use BULK_DISCOUNT", [r"bulk_discount", r"pricing\.py"]) is False
    assert text.matches_any("nothing", [r"a", r"th"]) is True


def test_list_item_count_counts_bullets_and_numbers_only():
    assert text.list_item_count("1. a\n2) b\n- c\n* d\n+ e\n  text\n---\n**bold**\nplain") == 5


def test_split_blocks_keeps_nested_items_with_their_parent_and_splits_siblings():
    reply = "review_status: X\nfindings:\n- shop/pricing.py\n  - off-by-one here\n- shop/cart.py fine\n\nA paragraph"
    blocks = text.split_blocks(reply)
    assert blocks[0] == "review_status: X\nfindings:"
    assert "shop/pricing.py" in blocks[1] and "off-by-one here" in blocks[1]
    assert blocks[2] == "- shop/cart.py fine"
    assert blocks[3] == "A paragraph"


def test_extract_code_blocks_handles_tildes_nesting_hints_and_an_unclosed_block():
    reply = (
        "Some text\nFile: chunks.py\n```python\nx = 1\n```\n"
        "~~~~\nfoo\n```\nstill inside\n~~~~\n```diff\n--- a/x\n+++ b/x\n"
    )
    first, second, third = text.extract_code_blocks(reply)
    assert (first.lang, first.body) == ("python", "x = 1")
    assert "chunks.py" in first.hint
    assert second.lang == "" and second.body == "foo\n```\nstill inside"
    assert third.lang == "diff" and third.body.startswith("--- a/x")  # the reply was cut off: the block runs to the end


def test_extract_json_objects_tolerates_prose_fences_and_broken_braces():
    reply = 'Sure! {"tool": "x", "arguments": {"a": 1}} and {"b": 2}. Trailing {oops'
    assert text.extract_json_objects(reply) == [{"tool": "x", "arguments": {"a": 1}}, {"b": 2}]
    assert text.extract_json_objects("no json here") == []


def test_is_bare_json_accepts_one_object_alone_or_in_one_fence_only():
    assert text.is_bare_json('{"a": 1}') is True
    assert text.is_bare_json('```json\n{"a": 1}\n```') is True
    assert text.is_bare_json('Here: {"a": 1}') is False
    assert text.is_bare_json('{"a": 1} {"b": 2}') is False
    assert text.is_bare_json('```json\n{"a": 1}\n```\nand more') is False


def test_split_numbered_answers_reads_labels_bullets_bold_and_continuations():
    reply = "Intro\nQ1: foo bar\ncont\n**Q2**: baz\n- q3) qux\nQ2. again"
    assert text.split_numbered_answers(reply) == {1: "foo bar\ncont", 2: "baz\nagain", 3: "qux"}
    assert text.split_numbered_answers("no labels at all") == {}


# =====================================================================================================================
# evalkit/codeeval.py: applying a model's answer, and running its code safely
# =====================================================================================================================

ORIGINAL = (
    '"""Doc."""\n\n\ndef chunk(items, size):\n    if size <= 0:\n        raise ValueError("size must be positive")\n'
    "    return [items[i:i + size] for i in range(0, len(items) - size + 1, size)]\n"
)


def test_apply_unified_diff_locates_the_hunk_by_content_not_by_line_number():
    diff = (
        "--- a/x.py\n+++ b/x.py\n@@ -40,4 +40,4 @@\n def chunk(items, size):\n     if size <= 0:\n"
        '         raise ValueError("size must be positive")\n'
        "-    return [items[i:i + size] for i in range(0, len(items) - size + 1, size)]\n"
        "+    return [items[i:i + size] for i in range(0, len(items), size)]\n"
    )
    new, why = codeeval.apply_unified_diff(ORIGINAL, diff)
    assert why is None
    assert "range(0, len(items), size)]" in new and "- size + 1" not in new and new.endswith("\n")


def test_apply_unified_diff_forgives_a_blank_context_line_that_lost_its_space():
    diff = (
        '--- a/x.py\n+++ b/x.py\n@@ -1,5 +1,5 @@\n """Doc."""\n\n\n-def chunk(items, size):\n'
        "+def chunk(items, size=2):\n     if size <= 0:\n"
    )
    new, why = codeeval.apply_unified_diff(ORIGINAL, diff)
    assert why is None and "def chunk(items, size=2):" in new


def test_apply_unified_diff_forgives_lost_indentation_as_a_last_resort():
    diff = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-if size <= 0:\n+    if size < 0:\n"
    new, why = codeeval.apply_unified_diff(ORIGINAL, diff)
    assert why is None and "    if size < 0:\n" in new


def test_apply_unified_diff_reports_a_hunk_that_matches_nothing():
    diff = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-this line is not in the file\n+something\n"
    assert codeeval.apply_unified_diff(ORIGINAL, diff) == (None, "hunk 1 does not match the file")
    assert codeeval.apply_unified_diff(ORIGINAL, "no diff at all")[1] == "the diff has no hunks"


def test_apply_unified_diff_applies_several_hunks_in_order():
    original = "a\nb\nc\nd\ne\nf\n"
    diff = "--- a/x\n+++ b/x\n@@ -1,2 +1,2 @@\n-a\n+A\n b\n@@ -5,2 +5,2 @@\n e\n-f\n+F\n"
    assert codeeval.apply_unified_diff(original, diff) == ("A\nb\nc\nd\ne\nF\n", None)


def _tree(tmp_path):
    root = tmp_path / "repo"
    codeeval.write_tree(root, {
        "textkit/chunks.py": ORIGINAL, "tests/test_chunks.py": "def test_x():\n    pass\n", "conftest.py": "x = 1\n",
    })
    return root


def test_apply_answer_drops_edits_to_tests_and_pytest_configuration(tmp_path):
    root = _tree(tmp_path)
    reply = (
        "```diff\n--- a/textkit/chunks.py\n+++ b/textkit/chunks.py\n@@ -1 +1 @@\n-\"\"\"Doc.\"\"\"\n+\"\"\"New doc.\"\"\"\n"
        "--- a/tests/test_chunks.py\n+++ b/tests/test_chunks.py\n@@ -1 +1 @@\n-def test_x():\n+def test_y():\n"
        "--- a/conftest.py\n+++ b/conftest.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n```"
    )
    applied = codeeval.apply_answer(reply, root)
    assert applied.written == ("textkit/chunks.py",)
    assert sorted(applied.ignored) == ["conftest.py", "tests/test_chunks.py"]
    assert (root / "tests" / "test_chunks.py").read_text() == "def test_x():\n    pass\n"
    assert (root / "conftest.py").read_text() == "x = 1\n"


@pytest.mark.parametrize("path", ["../evil.py", "/etc/evil.py", "C:/evil.py", "a/../../evil.py", "x:y.py"])
def test_apply_answer_refuses_paths_that_climb_out_or_are_absolute(tmp_path, path):
    root = _tree(tmp_path)
    reply = f"```diff\n--- a/{path}\n+++ b/{path}\n@@ -0,0 +1 @@\n+boom\n```"
    applied = codeeval.apply_answer(reply, root)
    assert applied.written == () and applied.problems
    assert not (tmp_path / "evil.py").exists()


def test_apply_answer_is_all_or_nothing_when_one_file_does_not_apply(tmp_path):
    root = _tree(tmp_path)
    codeeval.write_tree(root, {"textkit/other.py": "y = 1\n"})
    reply = (
        "```diff\n--- a/textkit/chunks.py\n+++ b/textkit/chunks.py\n@@ -1 +1 @@\n-\"\"\"Doc.\"\"\"\n+\"\"\"New.\"\"\"\n"
        "--- a/textkit/other.py\n+++ b/textkit/other.py\n@@ -1 +1 @@\n-nothing like this\n+z = 2\n```"
    )
    applied = codeeval.apply_answer(reply, root)
    assert applied.written == () and "textkit/other.py" in applied.problems[0]
    assert (root / "textkit" / "chunks.py").read_text() == ORIGINAL


def test_apply_answer_creates_a_new_file_and_refuses_a_deletion(tmp_path):
    root = _tree(tmp_path)
    created = codeeval.apply_answer("```diff\n--- /dev/null\n+++ b/textkit/new.py\n@@ -0,0 +1,2 @@\n+a = 1\n+b = 2\n```", root)
    assert created.written == ("textkit/new.py",)
    assert (root / "textkit" / "new.py").read_text() == "a = 1\nb = 2\n"
    deleted = codeeval.apply_answer("```diff\n--- a/textkit/chunks.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n```", root)
    assert deleted.written == () and "deletes" in deleted.problems[0]


def test_apply_answer_replacement_file_named_by_a_hint_a_comment_or_the_default(tmp_path):
    root = _tree(tmp_path)
    hinted = codeeval.apply_answer("Here is chunks.py:\n```python\nx = 1\n```", root)
    assert hinted.method == "replacement" and hinted.written == ("textkit/chunks.py",)
    assert (root / "textkit" / "chunks.py").read_text() == "x = 1\n"
    commented = codeeval.apply_answer("```python\n# textkit/other.py\ny = 2\n```", root)
    assert commented.written == ("textkit/other.py",)
    default = codeeval.apply_answer("```python\nz = 3\n```", root, default_target="textkit/chunks.py")
    assert default.written == ("textkit/chunks.py",)
    assert (root / "textkit" / "chunks.py").read_text() == "z = 3\n"


def test_apply_answer_replacement_needs_to_know_which_file_it_replaces(tmp_path):
    root = _tree(tmp_path)
    ambiguous = codeeval.apply_answer("```python\na = 1\n```\n```python\nb = 2\n```", root, default_target="textkit/chunks.py")
    assert ambiguous.written == () and "which file" in ambiguous.problems[0]
    no_target = codeeval.apply_answer("```python\na = 1\n```", root)
    assert no_target.written == () and no_target.problems
    nothing = codeeval.apply_answer("I would fix the loop bound.", root)
    assert nothing.method == "none" and nothing.problems


def test_apply_answer_replacement_of_a_protected_file_is_ignored(tmp_path):
    root = _tree(tmp_path)
    applied = codeeval.apply_answer("tests/test_chunks.py\n```python\ndef test_x():\n    assert True\n```", root)
    assert applied.written == () and applied.ignored == ("tests/test_chunks.py",)
    assert (root / "tests" / "test_chunks.py").read_text() == "def test_x():\n    pass\n"


def test_apply_answer_takes_a_bare_unfenced_diff_too(tmp_path):
    root = _tree(tmp_path)
    reply = 'Fix below.\n--- a/textkit/chunks.py\n+++ b/textkit/chunks.py\n@@ -1 +1 @@\n-"""Doc."""\n+"""Bare."""\n'
    assert codeeval.apply_answer(reply, root).written == ("textkit/chunks.py",)


def test_safe_relpath_accepts_plain_relative_paths_only():
    assert codeeval.safe_relpath("a\\b\\c.py") == "a/b/c.py"
    assert codeeval.safe_relpath("./a/b.py") == "a/b.py"
    assert codeeval.safe_relpath("a/b.py\t2024-01-01") == "a/b.py"
    for bad in ("", "/dev/null", "/etc/x", "C:/x.py", "../x.py", "a/../x.py", "a//b.py", "a/b.py:stream", "a/b?.py"):
        assert codeeval.safe_relpath(bad) is None


def test_is_protected_covers_tests_and_pytest_configuration():
    assert all(codeeval.is_protected(p) for p in (
        "tests/x.py", "test/x.py", "pkg/test_a.py", "pkg/a_test.py", "conftest.py", "pkg/conftest.py", "pytest.ini",
        "pyproject.toml", "sitecustomize.py",
    ))
    assert not any(codeeval.is_protected(p) for p in ("pkg/a.py", "textkit/chunks.py", "contest.py"))


def test_parse_pytest_summary_reads_the_last_summary_line():
    assert codeeval.parse_pytest_summary("F.\n1 failed, 3 passed in 0.05s") == (3, 1, 0)
    assert codeeval.parse_pytest_summary("4 passed in 0.02s (0:00:00)") == (4, 0, 0)
    assert codeeval.parse_pytest_summary("1 error in 0.10s") == (0, 0, 1)
    assert codeeval.parse_pytest_summary("no tests ran in 0.01s") == (0, 0, 0)
    assert codeeval.parse_pytest_summary("garbage") == (0, 0, 0)


def test_scrubbed_env_removes_credentials_and_pins_pytest_behaviour(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "value-one")
    monkeypatch.setenv("MY_TOKEN", "value-two")
    monkeypatch.setenv("DB_PASSWORD", "value-three")
    monkeypatch.setenv("PYTHONPATH", "/somewhere")
    monkeypatch.setenv("PYTEST_ADDOPTS", "-x")
    monkeypatch.setenv("HARMLESS_SETTING", "kept")
    env = codeeval.scrubbed_env({"EXTRA": "1"})
    assert not {"OPENROUTER_API_KEY", "MY_TOKEN", "DB_PASSWORD", "PYTHONPATH", "PYTEST_ADDOPTS"} & set(env)
    assert env["HARMLESS_SETTING"] == "kept" and env["EXTRA"] == "1"
    assert env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1" and env["PYTHONDONTWRITEBYTECODE"] == "1"


def test_run_pytest_counts_results_and_never_raises_on_a_timeout(tmp_path):
    codeeval.write_tree(tmp_path, {
        "test_a.py": "def test_ok():\n    pass\n\n\ndef test_bad():\n    assert 1 == 2\n",
        "test_slow.py": "import time\n\n\ndef test_slow():\n    time.sleep(30)\n",
    })
    run = codeeval.run_pytest(tmp_path, ["test_a.py"])
    assert (run.passed, run.failed, run.errors, run.total) == (1, 1, 0, 2)
    assert run.returncode == 1 and not run.ok and not run.timed_out
    slow = codeeval.run_pytest(tmp_path, ["test_slow.py"], timeout=2)
    assert slow.timed_out and slow.returncode is None and not slow.ok


def test_run_pytest_many_keeps_the_order_of_its_jobs(tmp_path):
    jobs = []
    for i, body in enumerate(("assert True", "assert False", "assert True")):
        directory = tmp_path / f"d{i}"
        codeeval.write_tree(directory, {"test_x.py": f"def test_x():\n    {body}\n"})
        jobs.append((directory, ["test_x.py"]))
    assert [r.returncode for r in codeeval.run_pytest_many(jobs, workers=2)] == [0, 1, 0]


# =====================================================================================================================
# the task registry
# =====================================================================================================================


def test_the_registry_holds_e1_to_e11_with_the_blueprint_kinds():
    assert tuple(TASKS) == tuple(f"E{i}" for i in range(1, 12))
    kinds = {t.id: t.kind for t in TASKS.values()}
    assert kinds["E8"] == "swarm" and kinds["E11"] == "comparison"
    assert all(kinds[i] in ("text", "repo") for i in ("E1", "E2", "E3", "E4", "E5", "E6", "E7", "E9", "E10"))
    assert tasks_mod.STANDALONE_IDS == ("E1", "E2", "E3", "E4", "E5", "E6", "E7", "E9", "E10")
    assert tasks_mod.PHASE2_IDS == ("E1", "E9", "E10")  # Appendix D: Phase 2 runs only E1, E9 and E10
    assert all(t.est_requests >= 0 and t.title and t.what for t in TASKS.values())
    assert TASKS["E11"].est_requests == 0
    # a one-shot call is the main call plus Hermes's title-generation call (2 requests, measured in the Phase 2 usage
    # files); E9 allows one corrected retry, and every call is its own session
    assert texttasks.CALL_REQUESTS == 2 and TASKS["E1"].est_requests == 2 and TASKS["E9"].est_requests == 4
    assert all(TASKS[i].est_requests == 2 for i in ("E2", "E4", "E5", "E6", "E7", "E10"))
    assert TASKS["E3"].est_requests > TASKS["E1"].est_requests  # reading a repository takes tool turns
    assert evals.TASKS is TASKS


def test_parse_task_ids_reads_lists_all_and_rejects_unknown_ids():
    assert [t.id for t in tasks_mod.parse_task_ids("E1, e9,E10,E1")] == ["E1", "E9", "E10"]
    assert [t.id for t in tasks_mod.parse_task_ids("all")] == list(tasks_mod.STANDALONE_IDS)
    assert [t.id for t in tasks_mod.parse_task_ids("E10,all")][0] == "E10"
    with pytest.raises(EvalError, match="unknown task 'E12'"):
        tasks_mod.parse_task_ids("E1,E12")
    with pytest.raises(EvalError, match="no tasks"):
        tasks_mod.parse_task_ids(" , ")


def test_every_standalone_prompt_is_ascii_short_and_free_of_secret_shaped_text(tmp_path):
    for i, task_id in enumerate(tasks_mod.STANDALONE_IDS):
        task = TASKS[task_id]
        prompt = task.build_prompt(task.build_fixture(_work(tmp_path, f"w{i}")))
        assert prompt.isascii(), task_id
        assert 200 < len(prompt) < 20000, task_id  # one command line argument: Windows tops out near 32K
        assert events.redact_text(prompt) == prompt, task_id


def test_a_task_that_cannot_run_standalone_says_how_to_run_it():
    assert tasks_mod.refusal_for(TASKS["E1"]) is None
    e8 = tasks_mod.refusal_for(TASKS["E8"])
    assert "swarm plan" in e8 and "swarm approve" in e8 and "swarm run" in e8 and "throwaway repository" in e8
    e11 = tasks_mod.refusal_for(TASKS["E11"])
    assert "role-value" in e11 and "two finished runs" in e11
    e11_score = TASKS["E11"].score({}, pathlib.Path("."), "", R)
    assert not e11_score.success and "role-value" in e11_score.notes  # a comparison has no scorer of its own


# =====================================================================================================================
# the scorers that read text: a known-good answer passes, known-bad ones do not (none of them calls a model)
# =====================================================================================================================


def _score(task_id, output, tmp_path, result=R, name="w"):
    task = TASKS[task_id]
    workdir = _work(tmp_path, name)
    return task.score(task.build_fixture(workdir), workdir, output, result)


def _assert_findings_survive_redaction(score):
    """A finding whose key looks like a credential ('key', 'token', 'secret') would be blanked by the event redactor
    before it reached results.jsonl."""
    stored = events.redact(score.to_dict())
    assert stored["findings"] == score.to_dict()["findings"]
    assert "[redacted]" not in json.dumps(stored)


def test_e1_accepts_explicit_assumptions_that_cover_the_ambiguities(tmp_path):
    good = _score("E1", E1_GOOD, tmp_path)
    assert good.success and good.findings["topics_covered"] == 9 and good.findings["missing_topics"] == ""
    _assert_findings_survive_redaction(good)


def test_e1_rejects_a_thin_answer_and_an_echo_of_the_request(tmp_path):
    for i, reply in enumerate((E1_BAD, E1_ECHO, "")):
        score = _score("E1", reply, tmp_path, name=f"w{i}")
        assert not score.success and score.findings["topics_covered"] <= 1


def test_e1_thresholds_are_enforced_on_topics_items_and_the_word_assumption(tmp_path, monkeypatch):
    monkeypatch.setattr(texttasks, "E1_MIN_TOPICS", 9)
    assert _score("E1", E1_GOOD, tmp_path, name="a").success
    missing_growth = "\n".join(line for line in E1_GOOD.splitlines() if "Grow means" not in line)
    assert not _score("E1", missing_growth, tmp_path, name="b").success
    monkeypatch.setattr(texttasks, "E1_MIN_TOPICS", 7)
    monkeypatch.setattr(texttasks, "E1_MIN_ITEMS", 11)
    assert not _score("E1", E1_GOOD, tmp_path, name="c").success  # 10 list items
    monkeypatch.setattr(texttasks, "E1_MIN_ITEMS", 6)
    no_word = E1_GOOD.replace("Assumptions", "Decisions")
    assert not _score("E1", no_word, tmp_path, name="d").findings["states_assumptions"]
    assert not _score("E1", no_word, tmp_path, name="e").success


def test_e2_accepts_named_components_with_interfaces_and_rejects_hand_waving(tmp_path):
    good = _score("E2", E2_GOOD, tmp_path)
    assert good.success and good.findings["components_named"] == 9 and good.findings["interface_lines"] >= 6
    _assert_findings_survive_redaction(good)
    bad = _score("E2", E2_BAD, tmp_path, name="b")
    assert not bad.success and bad.findings["components_named"] <= 2
    # components named but no interface stated is not an architecture
    no_interfaces = "\n".join(
        line.split(" - ")[0] for line in E2_GOOD.splitlines() if not line.startswith(("Components", "Flow"))
    ) + "\nIntake, queue, template renderer, email adapter, SMS adapter, retry backoff, dead letter, audit log, status, idempotency."
    score = _score("E2", no_interfaces, tmp_path, name="c")
    assert score.findings["components_named"] >= 7 and score.findings["interface_lines"] < 6 and not score.success


def test_e3_builds_a_real_repository_whose_code_holds_every_expected_fact(tmp_path):
    task = TASKS["E3"]
    workdir = _work(tmp_path)
    fixture = task.build_fixture(workdir)
    assert fixture["root"] == str(workdir) and len(fixture["questions"]) == 6
    assert (workdir / "inventory" / "cli.py").is_file() and (workdir / "README.md").is_file()
    config = (workdir / "inventory" / "config.py").read_text()
    assert 'INVENTORY_DB' in config and "MAX_ITEMS_PER_ORDER = 37" in config and "LOW_STOCK_THRESHOLD = 4" in config
    assert "BULK_THRESHOLD = 25" in (workdir / "inventory" / "pricing.py").read_text()
    assert "class CatalogNotFoundError" in (workdir / "inventory" / "storage.py").read_text()
    prompt = task.build_prompt(fixture)
    assert str(workdir) in prompt and "Q6:" in prompt and "file tools" in prompt
    assert task.tools == ("file",)  # a repository is explored with the file toolset


def test_e3_scores_the_fraction_of_answers_found_under_their_labels(tmp_path):
    good = _score("E3", E3_GOOD, tmp_path)
    assert good.success and (good.tests_passed, good.tests_total) == (6, 6)
    _assert_findings_survive_redaction(good)
    five = _score("E3", E3_GOOD.replace("37", "36"), tmp_path, name="b")
    assert five.success and five.findings["missing_answers"] == "Q2"  # 5 of 6 is 83 percent, above the 80 percent bar
    four = _score("E3", E3_GOOD.replace("37", "36").replace("25 units", "20 units"), tmp_path, name="c")
    assert not four.success and four.tests_passed == 4
    for i, reply in enumerate((
        "The variable is INVENTORY_DB in config.py, the limit is 37, CatalogNotFoundError in storage.py, bulk_discount "
        "in pricing.py from 25, report at 4, config pricing storage.",  # every fact, but under no label
        "",
    )):
        assert not _score("E3", reply, tmp_path, name=f"d{i}").success


def test_e3_an_answer_must_sit_under_its_own_label(tmp_path):
    shuffled = E3_GOOD.replace("Q2:", "Q9:")  # the answer to Q2 is now under a label that is not a question
    score = _score("E3", shuffled, tmp_path)
    assert "Q2" in score.findings["missing_answers"]


def test_e7_needs_the_seeded_vulnerabilities_named_and_no_shotgun(tmp_path):
    good = _score("E7", E7_GOOD, tmp_path)
    assert good.success and good.findings["seeded_named"] == 6 and good.findings["absent_classes_claimed"] == 0
    _assert_findings_survive_redaction(good)
    five = _score("E7", E7_GOOD.replace("- db(): hard-coded database password in source.\n", ""), tmp_path, name="b")
    assert five.success and five.findings["named_hardcoded_credential"] is False
    four = _score("E7", "\n".join(E7_GOOD.splitlines()[:4]), tmp_path, name="c")
    assert not four.success and four.findings["seeded_named"] == 4
    assert not _score("E7", "The code looks fine but consider adding logging.", tmp_path, name="d").success


def test_e7_naming_more_absent_classes_than_allowed_is_a_shotgun_answer(tmp_path):
    two = _score("E7", E7_GOOD + "- XSS everywhere\n- CSRF on all forms\n", tmp_path, name="a")
    assert two.success and two.findings["absent_classes_claimed"] == 2  # up to two are tolerated
    three = _score("E7", E7_GOOD + "- XSS everywhere\n- CSRF on all forms\n- XXE in parsing\n", tmp_path, name="b")
    assert not three.success and three.findings["seeded_named"] == 6 and three.findings["absent_classes_claimed"] == 3


def test_e7_sample_really_contains_each_seeded_weakness():
    code = texttasks.E7_CODE
    for needle in ("% name", "hashlib.md5", "os.path.join(UPLOAD_DIR", "shell=True", "pickle.loads", 'DB_PASSWORD = "'):
        assert needle in code
    compile(code, "sample", "exec")  # the sample is syntactically valid Python (it is compiled, never run)


def test_e9_tool_schema_check_names_every_kind_of_malformed_call():
    check = texttasks.check_tool_call
    assert check(json.loads(E9_CALL_OK)) == []
    assert any("priority" in e and "high" in e for e in check(json.loads(E9_CALL_URGENT)))
    assert check("x") == ["the reply must be a JSON object"]
    assert any("unknown tool" in e for e in check({"tool": "delete_everything", "arguments": {}}))
    assert any('"arguments" field' in e for e in check({"tool": "get_weather"}))
    assert any("unexpected field" in e for e in check({"tool": "get_weather", "arguments": {}, "extra": 1}))
    assert any("unknown argument 'colour'" in e for e in check(
        {"tool": "get_weather", "arguments": {"city": "Oslo", "unit": "celsius", "colour": "red"}}))
    assert any("missing required argument 'unit'" in e for e in check({"tool": "get_weather", "arguments": {"city": "Oslo"}}))
    assert any("integer" in e for e in check({"tool": "search_docs", "arguments": {"query": "x", "limit": True}}))
    assert any("between 1 and 20" in e for e in check({"tool": "search_docs", "arguments": {"query": "x", "limit": 21}}))
    assert any("number" in e for e in check(
        {"tool": "convert_currency", "arguments": {"amount": "5", "from_currency": "USD", "to_currency": "EUR"}}))
    assert any("3-letter" in e for e in check(
        {"tool": "convert_currency", "arguments": {"amount": 5, "from_currency": "usd", "to_currency": "EUR"}}))
    assert any("non-empty string" in e for e in check({"tool": "search_docs", "arguments": {"query": "  "}}))


def test_e9_scores_the_trace_with_at_most_one_corrected_retry(tmp_path):
    counter = iter(range(1000))

    def score(*attempts):
        result = InvokeResult(0, attempts[-1], "", 2.0, len(attempts), 10, 10, attempts=tuple(attempts))
        return _score("E9", attempts[-1], tmp_path, result=result, name=f"w{next(counter)}")

    first = score(E9_CALL_OK)
    assert first.success and first.findings["first_try_correct"] and first.findings["retries_used"] == 0
    _assert_findings_survive_redaction(first)
    recovered = score(E9_CALL_URGENT, E9_CALL_OK)
    assert recovered.success and recovered.findings["retries_used"] == 1 and not recovered.findings["first_try_valid"]
    assert not score(E9_CALL_URGENT, E9_CALL_URGENT).success  # still rejected after the retry
    assert not score(E9_CALL_URGENT, E9_CALL_URGENT, E9_CALL_OK).success  # a second retry is not allowed
    wrong = score(E9_CALL_OK.replace('"high"', '"low"'))  # accepted by the tool but not what the user asked for
    assert not wrong.success and wrong.findings["final_valid"] and not wrong.findings["final_correct"]
    assert not score("I will create the ticket now.").success
    chatty = score("Sure:\n```json\n" + E9_CALL_OK + "\n```")
    assert chatty.success and chatty.findings["strict_json_only"] is False  # still a call, but not 'nothing else'
    fenced = score("```json\n" + E9_CALL_OK + "\n```")
    assert fenced.success and fenced.findings["strict_json_only"] is True


def test_e9_a_second_json_object_in_the_reply_is_an_error(tmp_path):
    two = E9_CALL_OK + "\n" + E9_CALL_OK
    assert not _score("E9", two, tmp_path).findings["first_try_valid"]


def test_e9_retry_prompt_quotes_the_tool_errors_only_when_the_call_was_rejected():
    task = TASKS["E9"]
    fixture = task.build_fixture(pathlib.Path("."))
    assert task.retry_prompt(fixture, E9_CALL_OK) is None  # accepted: scored, not retried
    assert task.retry_prompt(fixture, E9_CALL_OK.replace('"high"', '"low"')) is None  # accepted but wrong: not retried
    prompt = task.retry_prompt(fixture, E9_CALL_URGENT)
    assert "error: argument 'priority' must be one of low, medium, high (got 'urgent')" in prompt
    assert "You can call the tools below" in prompt and "one corrected JSON tool call" in prompt
    assert task.max_retries == 1
    assert "no JSON object" in task.retry_prompt(fixture, "I cannot do that.")


def test_e10_the_diff_is_valid_and_carries_the_seeded_boundary_bug():
    diff = texttasks.build_e10_diff()
    assert "diff --git a/shop/pricing.py b/shop/pricing.py" in diff and "--- /dev/null" in diff
    assert "+    if quantity > 50:" in diff and "+    elif quantity > 10:" in diff
    assert "10 percent off from 10 units" in diff  # the documented tiers the strict comparisons contradict
    # each file's part of the diff, applied to its 'before' text, gives its 'after' text: it is a valid unified diff
    sections = {s.split(" ", 1)[0][2:]: s.split("\n", 1)[1] for s in diff.split("diff --git ")[1:]}
    assert set(sections) == set(texttasks.E10_FILES)
    for path, (before, after) in texttasks.E10_FILES.items():
        patched, why = codeeval.apply_unified_diff(before, sections[path])
        assert why is None and patched == after, path


def test_e10_needs_the_file_and_the_defect_class_in_one_finding_and_no_approval(tmp_path):
    good = _score("E10", E10_GOOD, tmp_path)
    assert good.success and good.findings["file_and_defect_together"] and not good.findings["approved"]
    _assert_findings_survive_redaction(good)
    approved = _score("E10", E10_GOOD.replace("CHANGES_REQUIRED", "PASS"), tmp_path, name="b")
    assert not approved.success and approved.findings["approved"]
    wrong_file = "review_status: CHANGES_REQUIRED\nfindings:\n- file: shop/cart.py | defect: off-by-one | why: boundary\n"
    assert not _score("E10", wrong_file, tmp_path, name="c").success
    vague = "review_status: CHANGES_REQUIRED\nfindings:\n- file: shop/pricing.py | defect: naming | why: use better names\n"
    vague_score = _score("E10", vague, tmp_path, name="d")
    assert vague_score.findings["file_named"] and not vague_score.findings["defect_named"] and not vague_score.success
    apart = "review_status: CHANGES_REQUIRED\n- shop/pricing.py needs another look\n\n- unrelated: an off-by-one somewhere\n"
    apart_score = _score("E10", apart, tmp_path, name="e")
    assert apart_score.findings["file_named"] and apart_score.findings["defect_named"]
    assert not apart_score.findings["file_and_defect_together"] and not apart_score.success
    nested = "review_status: CHANGES_REQUIRED\nfindings:\n- shop/pricing.py\n  - the comparison should be >= 10\n"
    assert _score("E10", nested, tmp_path, name="f").success  # a nested bullet stays with its parent finding


def test_e10_reads_the_verdict_however_the_reviewer_formats_it(tmp_path):
    finding = '{"file": "shop/pricing.py", "defect": "off-by-one", "why": "tiers start at 10"}'
    for i, (status, expected) in enumerate((('"review_status": "PASS"', True), ('"review_status": "CHANGES_REQUIRED"', False),
                                            ("**review_status:** PASS", True), ("review_status = `PASS`", True),
                                            ("Review_Status: pass", True), ("no status line", False))):
        score = _score("E10", f"{{{status}, \"findings\": [{finding}]}}" if status.startswith('"') else f"{status}\n- {finding}",
                       tmp_path, name=f"w{i}")
        assert score.findings["approved"] is expected, status
        assert score.success is (not expected), status
    changed_mind = "review_status: PASS\n...on reflection:\nreview_status: CHANGES_REQUIRED\n- shop/pricing.py | off-by-one"
    assert _score("E10", changed_mind, tmp_path, name="last").success  # the last verdict stands


# =====================================================================================================================
# the scorers that run code: E4, E5 and E6 (real pytest in temp copies)
# =====================================================================================================================


def test_e4_fixture_has_exactly_one_failing_test_and_says_so_in_the_prompt(tmp_path):
    task = TASKS["E4"]
    workdir = _work(tmp_path)
    fixture = task.build_fixture(workdir)
    assert (fixture["baseline_passed"], fixture["baseline_failed"]) == (3, 1)
    prompt = task.build_prompt(fixture)
    assert "test_last_chunk_may_be_shorter" in prompt and "textkit/chunks.py" in prompt and "unified diff" in prompt
    assert fixture["failure_output"].isascii()


def test_e4_passes_a_diff_or_a_replacement_file_that_makes_the_original_suite_green(tmp_path):
    for i, answer in enumerate((E4_GOOD_DIFF, E4_GOOD_FILE)):
        score = _score("E4", answer, tmp_path, name=f"w{i}")
        assert score.success and (score.tests_passed, score.tests_total) == (4, 4), answer[:30]
        _assert_findings_survive_redaction(score)


def test_e4_rejects_prose_a_rewritten_test_and_a_wrong_fix(tmp_path):
    prose = _score("E4", "I think the bug is an off by one in the range.", tmp_path, name="a")
    assert not prose.success and prose.findings["patch_method"] == "none" and prose.tests_passed == 3
    edited_test = (
        "```diff\n--- a/tests/test_chunks.py\n+++ b/tests/test_chunks.py\n@@ -8,3 +8,3 @@\n"
        " def test_last_chunk_may_be_shorter():\n-    assert chunk([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]\n"
        "+    assert chunk([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4]]\n```"
    )
    cheat = _score("E4", edited_test, tmp_path, name="b")
    assert not cheat.success and cheat.findings["protected_edits_ignored"] == 1 and cheat.tests_passed == 3
    broken = "textkit/chunks.py\n```python\ndef chunk(items, size):\n    return []\n```"
    wrong = _score("E4", broken, tmp_path, name="c")
    assert not wrong.success and wrong.findings["tests_failing_after"] >= 1


def test_e5_passes_a_refactor_that_extracts_renames_and_keeps_the_suite_green(tmp_path):
    score = _score("E5", "billing/invoice.py\n```python\n" + E5_GOOD + "```", tmp_path)
    assert score.success and (score.tests_passed, score.tests_total) == (6, 6)
    assert score.findings["function_extracted"] and score.findings["constant_renamed"]
    _assert_findings_survive_redaction(score)


def test_e5_needs_the_structure_as_well_as_a_green_suite(tmp_path):
    unchanged = _score("E5", "I would extract the function.", tmp_path, name="a")
    assert unchanged.tests_passed == 6 and not unchanged.success  # green, but nothing was refactored
    no_rename = _score("E5", "billing/invoice.py\n```python\n" + E5_GOOD.replace("SALES_TAX_RATE", "TAX_RATE") + "```",
                       tmp_path, name="b")
    assert not no_rename.success and not no_rename.findings["constant_renamed"] and no_rename.findings["function_extracted"]
    behaviour = E5_GOOD.replace("subtotal += line_total(order)", "subtotal += line_total(order) + 1")
    changed = _score("E5", "billing/invoice.py\n```python\n" + behaviour + "```", tmp_path, name="c")
    assert not changed.success and changed.tests_passed < 6 and changed.findings["extracted_function_used"]


@pytest.mark.parametrize("edit, key", [
    (lambda s: s.replace("def line_total(order):", "def line_total(order, extra):"), "function_extracted"),
    (lambda s: s.replace("subtotal += line_total(order)", 'subtotal += order["qty"] * order["unit_price"]'),
     "extracted_function_used"),
    (lambda s: s.replace("SALES_TAX_RATE = 0.2", "SALES_TAX_RATE = 0.2\nTAX_RATE = 0.2"), "constant_renamed"),
    (lambda s: s.replace("def summarize_orders(orders):", "def summarize_orders(rows):").replace("in orders", "in rows"),
     "public_api_kept"),
    (lambda s: s + "def broken(:\n", "parses"),
])
def test_e5_structure_check_names_each_way_a_refactor_can_fall_short(edit, key):
    assert all(codetasks.check_e5_structure(E5_GOOD).values())
    result = codetasks.check_e5_structure(edit(E5_GOOD))
    assert result[key] is False


def test_e6_every_seeded_mutant_applies_once_differs_and_still_compiles():
    mutants = codetasks.build_e6_mutants()
    assert len(mutants) == 10 == len(codetasks.E6_MUTANTS)
    for mutant_id, source in mutants.items():
        assert source != codetasks.E6_MODULE, mutant_id
        compile(source, mutant_id, "exec")
    assert len({source for source in mutants.values()}) == 10  # no two mutants are the same program


def test_e6_a_reference_test_file_kills_every_mutant_and_a_weak_one_kills_none(tmp_path):
    good = _score("E6", "```python\n" + E6_GOLD + "```", tmp_path, name="a")
    assert good.success and good.findings["mutants_killed"] == 10 and good.findings["survivors"] == ""
    assert (good.tests_passed, good.tests_total) == (13, 13) and good.findings["passes_on_reference"]
    _assert_findings_survive_redaction(good)
    weak = _score("E6", E6_WEAK, tmp_path, name="b")
    assert not weak.success and weak.findings["passes_on_reference"] and weak.findings["mutants_killed"] == 0
    assert weak.findings["survivors"].count("M") == 10


def test_e6_the_kill_threshold_is_at_least_n_of_m(tmp_path, monkeypatch):
    first_four = "```python\n" + E6_GOLD.split("def test_unsorted")[0].rstrip() + "\n```"  # empty, single, disjoint, overlap, touching
    score = _score("E6", first_four, tmp_path, name="a")
    killed = score.findings["mutants_killed"]
    assert 0 < killed < 10 and score.findings["passes_on_reference"]
    monkeypatch.setattr(codetasks, "E6_MIN_KILLED", killed)
    assert _score("E6", first_four, tmp_path, name="b").success
    monkeypatch.setattr(codetasks, "E6_MIN_KILLED", killed + 1)
    assert not _score("E6", first_four, tmp_path, name="c").success


def test_e6_tests_that_fail_on_the_reference_or_read_its_source_or_do_not_exist_score_nothing(tmp_path):
    wrong = "```python\nfrom intervals import merge_intervals\n\n\ndef test_bad():\n    assert merge_intervals([(1, 3), (2, 6)]) == [(1, 5)]\n```"
    score = _score("E6", wrong, tmp_path, name="a")
    assert not score.success and not score.findings["passes_on_reference"] and score.findings["mutants_killed"] == 0
    assert "reference" in score.notes
    cheat = "```python\nimport inspect, intervals\n\n\ndef test_x():\n    assert 'max(' in inspect.getsource(intervals)\n```"
    cheated = _score("E6", cheat, tmp_path, name="b")
    assert not cheated.success and "source" in cheated.notes and not cheated.findings["passes_on_reference"]
    none = _score("E6", "I would test the empty list.", tmp_path, name="c")
    assert not none.success and not none.findings["tests_found"]
    skipped = "```python\nimport pytest\n\n\n@pytest.mark.skip\ndef test_x():\n    assert False\n```"
    assert not _score("E6", skipped, tmp_path, name="d").success  # nothing passed, so nothing was proven


def test_extract_test_code_joins_python_blocks_and_takes_a_bare_test_file():
    joined = codetasks.extract_test_code("```python\nimport pytest\n```\ntext\n```python\ndef test_a():\n    pass\n```")
    assert joined.count("import pytest") == 1 and "def test_a" in joined
    assert codetasks.extract_test_code("```bash\npytest -q\n```") is None
    assert "def test_x" in codetasks.extract_test_code("def test_x():\n    assert True\n")
    assert codetasks.extract_test_code("```\ndef test_y():\n    pass\n```").startswith("def test_y")
    assert codetasks.extract_test_code("no tests here") is None


# =====================================================================================================================
# E8: a descriptor, scored from a finished swarm project
# =====================================================================================================================

TODO_REFERENCE = {
    "todo/__init__.py": '"""Todo manager."""\n',
    "todo/storage.py": (
        "import json\nimport os\n\n\ndef load():\n    try:\n"
        '        with open(os.environ["TODO_FILE"], encoding="utf-8") as handle:\n            return json.load(handle)\n'
        "    except FileNotFoundError:\n        return []\n\n\ndef save(items):\n"
        '    with open(os.environ["TODO_FILE"], "w", encoding="utf-8") as handle:\n        json.dump(items, handle)\n'
    ),
    "todo/core.py": (
        "from . import storage\n\n\ndef add(text):\n    items = storage.load()\n"
        '    item = {"id": len(items) + 1, "text": text, "done": False}\n    items.append(item)\n'
        '    storage.save(items)\n    return item["id"]\n\n\ndef list_items():\n    return storage.load()\n\n\n'
        "def mark_done(item_id):\n    items = storage.load()\n    for item in items:\n"
        '        if item["id"] == item_id:\n            item["done"] = True\n            storage.save(items)\n'
        "            return True\n    return False\n"
    ),
    "todo/cli.py": (
        "import sys\n\nfrom . import core\n\n\ndef main(argv=None):\n"
        "    argv = list(sys.argv[1:] if argv is None else argv)\n    if not argv:\n"
        '        print("usage: todo add TEXT | list | done ID", file=sys.stderr)\n        return 2\n'
        "    command, rest = argv[0], argv[1:]\n"
        '    if command == "add":\n        print(f"added {core.add(\' \'.join(rest))}")\n        return 0\n'
        '    if command == "list":\n        for item in core.list_items():\n'
        '            mark = "x" if item["done"] else " "\n'
        "            print(f\"{item['id']} [{mark}] {item['text']}\")\n        return 0\n"
        '    if command == "done":\n        try:\n            item_id = int(rest[0])\n'
        "        except (IndexError, ValueError):\n"
        '            print("done needs a numeric id", file=sys.stderr)\n            return 2\n'
        '        if core.mark_done(item_id):\n            print(f"done {item_id}")\n            return 0\n'
        '        print(f"no such item: {item_id}", file=sys.stderr)\n        return 1\n'
        '    print(f"unknown command: {command}", file=sys.stderr)\n    return 2\n'
    ),
    "todo/__main__.py": "import sys\n\nfrom .cli import main\n\nsys.exit(main())\n",
    "tests/test_todo.py": (
        "import pytest\n\nfrom todo import core\n\n\n@pytest.fixture(autouse=True)\n"
        'def state(tmp_path, monkeypatch):\n    monkeypatch.setenv("TODO_FILE", str(tmp_path / "todo.json"))\n\n\n'
        'def test_add_assigns_sequential_ids():\n    assert core.add("a") == 1\n    assert core.add("b") == 2\n\n\n'
        'def test_mark_done():\n    core.add("a")\n    assert core.mark_done(1) is True\n'
        '    assert core.list_items()[0]["done"] is True\n\n\n'
        "def test_mark_done_unknown_id():\n    assert core.mark_done(7) is False\n"
    ),
}


def _git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _finished_swarm(tmp_path, files=None):
    """A repository whose integration branch holds `files` (the reference todo project by default), and a database with
    the rows a finished two-task swarm project would leave."""
    task = TASKS["E8"]
    repo = _work(tmp_path, "repo")
    fixture = task.build_fixture(repo)
    codeeval.write_tree(repo, TODO_REFERENCE if files is None else files)
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "swarm work")
    conn = db.connect(tmp_path / "swarm.db")
    for key in ("T1", "T2"):
        conn.execute(
            "INSERT INTO plan_tasks (project, task_key, work_card_id, merge_card_id, role, created_at) "
            "VALUES ('p1', ?, 'w', 'm', 'coder', datetime('now'))", (key,))
        conn.execute(
            "INSERT INTO merge_records (task_key, squash_commit, gate3_result, reverted, completed_at) "
            "VALUES (?, 'abc', 'pass', 0, datetime('now'))", (key,))
    return fixture, repo, conn


def test_e8_fixture_is_a_throwaway_git_repository_on_the_integration_branch(tmp_path):
    fixture, repo, _conn = _finished_swarm(tmp_path, files={"x.txt": "x\n"})
    assert fixture["integration_branch"] == "integration" and fixture["repo"] == str(repo)
    assert _git(repo, "log", "--format=%s", "integration").splitlines()[-1] == "initial commit"
    assert "todo" in TASKS["E8"].build_prompt(fixture) and "TODO_FILE" in TASKS["E8"].build_prompt(fixture)
    assert TASKS["E8"].kind == "swarm" and TASKS["E8"].est_requests >= 100


def test_e8_scores_a_finished_project_from_its_merge_records_review_counts_and_acceptance_checks(tmp_path):
    _fixture, repo, conn = _finished_swarm(tmp_path)
    conn.execute("INSERT INTO lineage (project, task_key, review_rounds, updated_at) VALUES ('p1', 'T1', 2, datetime('now'))")
    conn.execute(
        "INSERT INTO usage_ingested (session_id, profile, provider, model, requests, input_tokens, output_tokens, "
        "ingested_at, project, task_key, card_id) VALUES ('s1', 'coder-1', 'xkiro', 'm', 30, 1, 1, datetime('now'), "
        "'p1', 'T1', 'w')")
    conn.execute("INSERT INTO events (ts, kind, payload) VALUES (datetime('now'), 'question_answered', "
                 "'{\"card_id\": \"w\", \"task_key\": \"T1\", \"chars\": 5}')")
    conn.execute("INSERT INTO events (ts, kind, payload) VALUES (datetime('now'), 'question_answered', "
                 "'{\"card_id\": \"z\", \"task_key\": \"OTHER\", \"chars\": 5}')")
    conn.execute("INSERT INTO project_state (project, status, updated_at) VALUES ('p1', 'finished', datetime('now'))")
    score = evals.score_swarm_project(conn, "p1", repo)
    assert score.success and (score.tests_passed, score.tests_total) == (8, 8)
    assert score.findings["tasks_total"] == 2 and score.findings["tasks_merged"] == 2 and score.findings["tasks_reverted"] == 0
    assert score.findings["review_changes_required"] == 2 and score.findings["requests"] == 30
    assert score.findings["human_interventions"] == 1 and score.findings["project_status"] == "finished"
    assert all(score.findings[f"check_{name}"] for name in (
        "files_exist", "add_first", "add_second", "list_open", "done", "list_after_done", "done_unknown_id",
        "own_tests_pass"))
    _assert_findings_survive_redaction(score)


def test_e8_an_unmerged_or_reverted_task_or_an_unknown_project_is_not_success(tmp_path):
    _fixture, repo, conn = _finished_swarm(tmp_path)
    conn.execute("UPDATE merge_records SET completed_at = NULL WHERE task_key = 'T2'")
    unmerged = evals.score_swarm_project(conn, "p1", repo)
    assert not unmerged.success and unmerged.findings["tasks_merged"] == 1 and unmerged.tests_passed == 8
    conn.execute("UPDATE merge_records SET completed_at = datetime('now'), reverted = 1 WHERE task_key = 'T2'")
    reverted = evals.score_swarm_project(conn, "p1", repo)
    assert not reverted.success and reverted.findings["tasks_reverted"] == 1
    missing = evals.score_swarm_project(conn, "nope", repo)
    assert not missing.success and "no plan tasks" in missing.notes


def test_e8_review_changes_fall_back_to_the_changes_required_verdicts_without_lineage_rows(tmp_path):
    _fixture, repo, conn = _finished_swarm(tmp_path)
    for sha, outcome in (("a1", "CHANGES_REQUIRED"), ("a2", "CHANGES_REQUIRED"), ("a3", "PASS")):
        conn.execute(
            "INSERT INTO review_verdicts (project, task_key, commit_sha, card_id, outcome, reviewer_profile, recorded_at) "
            "VALUES ('p1', 'T1', ?, 'w', ?, 'reviewer', datetime('now'))", (sha, outcome))
    assert evals.score_swarm_project(conn, "p1", repo).findings["review_changes_required"] == 2


def test_e8_acceptance_checks_fail_on_code_that_does_not_do_what_the_request_said(tmp_path):
    broken = dict(TODO_REFERENCE)
    broken["todo/cli.py"] = TODO_REFERENCE["todo/cli.py"].replace("return 1\n    print(f\"unknown", "return 0\n    print(f\"unknown")
    _fixture, repo, conn = _finished_swarm(tmp_path, files=broken)
    score = evals.score_swarm_project(conn, "p1", repo)
    assert not score.success and score.findings["check_done_unknown_id"] is False and score.findings["check_add_first"]
    assert score.tests_passed == 7 and score.tests_total == 8


def test_e8_a_project_that_shipped_no_tests_fails_the_own_tests_check(tmp_path):
    no_tests = {k: v for k, v in TODO_REFERENCE.items() if not k.startswith("tests/")}
    _fixture, repo, conn = _finished_swarm(tmp_path, files=no_tests)
    score = evals.score_swarm_project(conn, "p1", repo)
    assert not score.success and score.findings["check_own_tests_pass"] is False and score.findings["check_add_first"]


def test_e8_a_missing_branch_is_reported_not_raised(tmp_path):
    _fixture, repo, conn = _finished_swarm(tmp_path)
    score = evals.score_swarm_project(conn, "p1", repo, "no-such-branch")
    assert not score.success and "git archive" in score.notes and score.tests_total is None


def test_e8_the_task_scorer_reads_the_database_named_in_the_fixture(tmp_path):
    fixture, repo, conn = _finished_swarm(tmp_path)
    task = TASKS["E8"]
    without = task.score(fixture, repo, "", R)
    assert not without.success and "finished swarm project" in without.notes
    conn.close()
    with_db = task.score(dict(fixture, db_path=str(tmp_path / "swarm.db"), project="p1"), repo, "", R)
    assert with_db.success and with_db.findings["tasks_merged"] == 2
    assert not task.score(dict(fixture, db_path=str(tmp_path / "gone.db"), project="p1"), repo, "", R).success


# =====================================================================================================================
# helpers for the harness tests: a model that is never called, small tasks, records, configuration files
# =====================================================================================================================

MODELS = {
    "providers": {
        "openrouter": {"limits": {"rpm": 20, "per_day_default": 50, "per_day_after_credits": 1000}, "credits_purchased": False},
        "xkiro": {"limits": {}},
        "slow": {"limits": {"per_model_rpm": 2}},
    },
    "models": [
        {"provider": "xkiro", "model": "qwen/qwen3-coder-plus:free", "role_class": "coder", "pinned": True},
        {"provider": "xkiro", "model": "minimax/minimax-m3:free", "role_class": "coder_candidate", "pinned": False},
        {"provider": "xkiro", "model": "qwen/qwen3.8-max:free", "role_class": "lead", "pinned": True},
        {"provider": "xkiro", "model": "openai/gpt-5.6-terra", "role_class": "lead_unfunded", "pinned": False},
        {"provider": "openrouter", "model": "cohere/north-mini-code:free", "role_class": "reviewer", "pinned": True},
        {"provider": "openrouter", "model": "openrouter/free", "role_class": "coder", "pinned": False},
        {"provider": "openrouter", "model": "some/model", "role_class": "coder", "pinned": False, "router": True},
        {"provider": "slow", "model": "m1", "role_class": "coder", "pinned": False},
        {"provider": "slow", "model": "m2", "role_class": "coder", "pinned": False},
        {"provider": "xkiro", "model": "noclass/model", "pinned": False},
    ],
}
ROLES = {"lead": "lead", "coder": "coder-1", "reviewer": "reviewer"}
BUDGETS = {"daily_reserve_percent": 10, "review_reserve_requests": 20}


def cand(label, role_class="coder", profile="coder-1"):
    provider, model = label.split("/", 1)
    return evals.Candidate(provider, model, role_class, profile, label)


CODER = cand("xkiro/qwen/qwen3-coder-plus:free")
MINIMAX = cand("xkiro/minimax/minimax-m3:free", "coder_candidate")
REVIEWER = cand("openrouter/cohere/north-mini-code:free", "reviewer", "reviewer")


class FakeInvoke:
    """A model that is never called. `replies` maps a phrase of the prompt to the stdout to return (a callable
    (candidate, prompt) -> str is allowed); `raises` maps a phrase of the prompt or a candidate label to an exception."""

    def __init__(self, replies=None, *, requests=1, latency=1.0, tokens=(100, 20), returncode=0, stderr="", raises=None,
                 served=None):
        self.replies = GOOD_REPLIES if replies is None else replies
        self.requests, self.latency, self.tokens = requests, latency, tokens
        self.returncode, self.stderr, self.raises, self.served = returncode, stderr, raises or {}, served
        self.calls = []

    def __call__(self, candidate, prompt, workdir, timeout):
        self.calls.append(types.SimpleNamespace(
            candidate=candidate, prompt=prompt, workdir=pathlib.Path(workdir), timeout=timeout,
            marker_present=(pathlib.Path(workdir) / "marker.txt").exists(),
        ))
        for key, exc in self.raises.items():
            if key in prompt or key == candidate.label:
                raise exc
        reply = next((v for k, v in self.replies.items() if k in prompt), "")
        if callable(reply):
            reply = reply(candidate, prompt)
        return InvokeResult(self.returncode, reply, self.stderr, self.latency, self.requests, self.tokens[0],
                            self.tokens[1], served_model=self.served)


def make_task(task_id="T1", *, score=None, est=1, tools=(), retry=None, max_retries=0, kind="text"):
    """A tiny evaluation task: the fixture drops a marker file, the prompt is 'prompt for <id>', and by default the run
    succeeds when the model answers 'ok'."""
    def build_fixture(workdir):
        (workdir / "marker.txt").write_text("x")
        return {"id": task_id}

    def default_score(fixture, workdir, output, result):
        return Score(success=output == "ok", findings={"attempts": len(result.attempts)})

    return evals.EvalTask(
        id=task_id, title=f"Task {task_id}", kind=kind, build_fixture=build_fixture,
        build_prompt=lambda fixture: f"prompt for {task_id}", score=score or default_score, est_requests=est,
        tools=tools, max_retries=max_retries, retry_prompt=retry, timeout_seconds=77, what="a test task",
    )


OK = {"prompt for": "ok"}


def rec(task="E1", cand="a/x", *, success=True, requests=1, latency=1.0, tin=10, tout=5, retries=0, fallbacks=0,
        changes=0, human=0, tests=None, findings=None, error="", run_id="r1", notes=""):
    passed, total = tests if tests else (None, None)
    return evals.RunRecord(
        task_id=task, candidate=cand, run_id=run_id, started_at="2026-09-21T00:00:00+00:00", latency_seconds=latency,
        requests=requests, input_tokens=tin, output_tokens=tout, retries=retries, fallbacks=fallbacks,
        review_changes_required=changes, human_interventions=human, score=Score(success, passed, total, findings or {}, notes),
        raw_output_path=None, error=error,
    )


class FakeClock:
    def __init__(self, start=1_800_000_000.0):
        self.t = start
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


def run(tmp_path, tasks, candidates, invoke, **kwargs):
    kwargs.setdefault("spend", True)
    kwargs.setdefault("sleep", lambda s: None)
    return evals.run_eval(tasks, candidates, invoke=invoke, workroot=tmp_path / "work", out_dir=tmp_path / "out", **kwargs)


def all_text(directory):
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(pathlib.Path(directory).rglob("*")) if p.is_file())


# =====================================================================================================================
# candidates: looked up in config/models.yaml by provider/model label
# =====================================================================================================================


def test_candidate_from_config_gives_the_profile_the_role_map_assigns():
    coder = evals.candidate_from_config(MODELS, "xkiro/qwen/qwen3-coder-plus:free", roles=ROLES)
    assert (coder.provider, coder.model, coder.role_class, coder.profile) == (
        "xkiro", "qwen/qwen3-coder-plus:free", "coder", "coder-1")
    assert coder.label == "xkiro/qwen/qwen3-coder-plus:free"
    assert evals.candidate_from_config(MODELS, "xkiro/minimax/minimax-m3:free", roles=ROLES).profile == "coder-1"
    assert evals.candidate_from_config(MODELS, "xkiro/openai/gpt-5.6-terra", roles=ROLES).profile == "lead"
    assert evals.candidate_from_config(MODELS, "openrouter/cohere/north-mini-code:free", roles=ROLES).profile == "reviewer"


def test_candidate_from_config_profile_override_and_the_error_cases():
    assert evals.candidate_from_config(MODELS, "xkiro/noclass/model", profile="tester").profile == "tester"
    with pytest.raises(EvalError, match="no Hermes profile is known.*--profile"):
        evals.candidate_from_config(MODELS, "xkiro/noclass/model", roles=ROLES)
    with pytest.raises(EvalError, match="no Hermes profile"):
        evals.candidate_from_config(MODELS, "xkiro/qwen/qwen3-coder-plus:free", roles={})
    with pytest.raises(EvalError) as unknown:  # an unknown label is an error that lists what is declared, never a guess
        evals.candidate_from_config(MODELS, "xkiro/qwen/qwen3-coder-plus", roles=ROLES)
    assert "unknown candidate" in str(unknown.value) and "xkiro/qwen/qwen3-coder-plus:free" in str(unknown.value)
    duplicated = {"models": [MODELS["models"][0], MODELS["models"][0]]}
    with pytest.raises(EvalError, match="more than once"):
        evals.candidate_from_config(duplicated, "xkiro/qwen/qwen3-coder-plus:free", roles=ROLES)


def test_load_candidates_keeps_order_drops_repeats_and_needs_at_least_one():
    labels = ["xkiro/minimax/minimax-m3:free", " xkiro/qwen/qwen3-coder-plus:free", "xkiro/minimax/minimax-m3:free", ""]
    assert [c.label for c in evals.load_candidates(MODELS, labels, roles=ROLES)] == [
        "xkiro/minimax/minimax-m3:free", "xkiro/qwen/qwen3-coder-plus:free"]
    with pytest.raises(EvalError, match="no candidates"):
        evals.load_candidates(MODELS, [" ", ""], roles=ROLES)


def test_every_model_in_the_real_models_yaml_resolves_to_a_candidate():
    from ases import config as ases_config

    real = ases_config.load_models_config(pathlib.Path(__file__).resolve().parents[2] / "config" / "models.yaml")
    labels = [f"{m['provider']}/{m['model']}" for m in real["models"]]
    assert labels and len(set(labels)) == len(labels)
    for label in labels:
        found = evals.candidate_from_config(real, label, profile="any")
        assert found.label == label and label == f"{found.provider}/{found.model}"


# =====================================================================================================================
# what a run costs, and whether the day's quota can afford it
# =====================================================================================================================


def test_estimate_counts_requests_per_provider_candidate_and_task():
    e = evals.estimate([TASKS["E1"], TASKS["E3"], TASKS["E9"]], [CODER, REVIEWER])  # 2 + 13 + 4 requests per candidate
    assert (e.runs, e.total_requests) == (6, 38)
    assert e.per_task == {"E1": 4, "E3": 26, "E9": 8}
    assert e.per_candidate == {CODER.label: 19, REVIEWER.label: 19}
    assert e.per_provider == {"xkiro": 19, "openrouter": 19}
    same = evals.estimate([TASKS["E1"]], [CODER, MINIMAX])  # two models on one provider share its quota
    assert same.per_provider == {"xkiro": 4} and same.per_candidate == {CODER.label: 2, MINIMAX.label: 2}
    assert evals.estimate([], [CODER]).total_requests == 0


def test_calendar_minutes_paces_per_model_or_per_provider_as_the_limit_is_declared():
    slow1, slow2 = cand("slow/m1"), cand("slow/m2")
    e = evals.estimate([make_task(est=10)], [slow1, slow2, REVIEWER, CODER])
    minutes = evals.calendar_minutes(e, [slow1, slow2, REVIEWER, CODER], MODELS["providers"])
    assert minutes["slow"] == pytest.approx(5.0)  # 10 requests on one model at 2 a minute: each model paces alone
    assert minutes["openrouter"] == pytest.approx(0.5)  # 10 requests at 20 a minute, account wide
    assert minutes["xkiro"] is None  # no declared limit


def test_check_budget_refuses_what_the_days_quota_cannot_afford(tmp_path):
    conn = db.connect(tmp_path / "ases.db")
    e = evals.estimate([make_task(est=40)], [REVIEWER])
    assert evals.check_budget(conn, MODELS, e) == []  # 50 free requests a day cover 40 with no reserve
    problems = evals.check_budget(conn, MODELS, e, budgets=BUDGETS)  # 20 held for reviews and 10 percent daily reserve
    assert len(problems) == 1 and problems[0].startswith("provider openrouter:") and "needs 40" in problems[0]
    ledger.record_usage(conn, "openrouter", "another/model", 20)  # the cap is per account, not per model
    assert evals.check_budget(conn, MODELS, e)[0].startswith("provider openrouter:")


def test_check_budget_passes_a_provider_with_no_known_cap_and_skips_zero_requests(tmp_path):
    conn = db.connect(tmp_path / "ases.db")
    ledger.record_usage(conn, "xkiro", "m", 10_000)
    assert evals.check_budget(conn, MODELS, evals.estimate([make_task(est=5000)], [CODER]), budgets=BUDGETS) == []
    assert evals.check_budget(conn, MODELS, evals.estimate([make_task(est=0)], [REVIEWER]), budgets=BUDGETS) == []


# =====================================================================================================================
# run_eval: the stop condition, the runner, the files
# =====================================================================================================================


def test_a_dry_run_calls_nothing_writes_nothing_and_says_what_it_would_do(tmp_path):
    fake = FakeInvoke()
    tasks = [TASKS["E1"], TASKS["E9"], TASKS["E10"]]
    summary = run(tmp_path, tasks, [CODER, REVIEWER], fake, spend=False)
    assert fake.calls == []  # ASES-DOC-04: evaluation spends real quota, so nothing is called without spend=True
    assert not (tmp_path / "out").exists() and not (tmp_path / "work").exists()
    assert (summary.spent, summary.status, summary.run_id, summary.run_dir) == (False, "dry_run", None, None)
    assert summary.planned[0] == ("E1", CODER.label) and len(summary.planned) == 6
    assert summary.estimate.total_requests == 16 and summary.refusals == () and summary.records == ()  # (2 + 4 + 2) x 2


def test_the_default_is_a_dry_run(tmp_path):
    fake = FakeInvoke()
    summary = evals.run_eval([TASKS["E1"]], [CODER], invoke=fake, workroot=tmp_path / "w", out_dir=tmp_path / "o")
    assert fake.calls == [] and not summary.spent


def test_a_dry_run_reports_why_a_real_run_would_be_refused(tmp_path):
    conn = db.connect(tmp_path / "ases.db")
    summary = run(tmp_path, [TASKS["E8"], make_task(est=45)], [REVIEWER], FakeInvoke(), spend=False, conn=conn,
                  models_config=MODELS, budgets=BUDGETS)
    assert len(summary.refusals) == 2
    assert "swarm run" in summary.refusals[0] and summary.refusals[1].startswith("provider openrouter:")


def test_spending_refuses_a_task_that_cannot_run_standalone_before_calling_anything(tmp_path):
    fake = FakeInvoke()
    for task in (TASKS["E8"], TASKS["E11"]):
        with pytest.raises(EvalRefused) as refused:
            run(tmp_path, [TASKS["E1"], task], [CODER], fake)
        assert tasks_mod.refusal_for(task) in str(refused.value)
    assert fake.calls == [] and not (tmp_path / "out").exists()


def test_spending_refuses_a_run_the_quota_cannot_afford_before_calling_anything(tmp_path):
    conn = db.connect(tmp_path / "ases.db")
    fake = FakeInvoke()
    with pytest.raises(EvalRefused, match="provider openrouter"):
        run(tmp_path, [make_task(est=45)], [REVIEWER], fake, conn=conn, models_config=MODELS, budgets=BUDGETS)
    assert fake.calls == [] and not (tmp_path / "out").exists()
    assert ledger.usage_today_for_provider(conn, "openrouter") == 0  # a refusal costs nothing


def test_spending_writes_results_summary_and_the_raw_outputs(tmp_path):
    fake = FakeInvoke(OK, tokens=(120, 30), latency=2.5)
    tasks = [make_task("T1"), make_task("T2")]
    summary = run(tmp_path, tasks, [CODER, MINIMAX], fake)
    assert summary.spent and summary.status == "complete" and len(summary.records) == 4 and fake.calls
    run_dir = summary.run_dir
    assert run_dir.parent == tmp_path / "out" and run_dir.name == summary.run_id and run_dir.name.startswith("eval-")
    lines = (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4
    first = json.loads(lines[0])
    assert first["task_id"] == "T1" and first["candidate"] == CODER.label and first["score"]["success"] is True
    assert (first["requests"], first["input_tokens"], first["output_tokens"], first["latency_seconds"]) == (1, 120, 30, 2.5)
    assert first["raw_output_path"] == "raw/T1-xkiro_qwen_qwen3-coder-plus_free.txt"
    assert (run_dir / first["raw_output_path"]).read_text(encoding="utf-8") == "ok"
    assert sorted(p.name for p in (run_dir / "raw").iterdir()) == [
        "T1-xkiro_minimax_minimax-m3_free.txt", "T1-xkiro_qwen_qwen3-coder-plus_free.txt",
        "T2-xkiro_minimax_minimax-m3_free.txt", "T2-xkiro_qwen_qwen3-coder-plus_free.txt"]
    summary_file = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary_file["status"] == "complete" and summary_file["tasks"] == ["T1", "T2"]
    assert [c["label"] for c in summary_file["candidates"]] == [CODER.label, MINIMAX.label]
    assert len(summary_file["results"]) == 4 and summary_file["estimate"]["total_requests"] == 4
    assert "never merged into one number" in summary_file["note"]
    assert evals.load_run(run_dir) == list(summary.records)  # what was written is what is read back


def test_a_secret_shaped_value_in_a_model_reply_never_reaches_the_disk(tmp_path):
    secret_key, bearer = "sk-abcdefghijklmnop123456", "Bearer abcdefghijklmnopqrstuvwxyz0123456789"
    fake = FakeInvoke({"prompt for T1": f"answer {secret_key} and more", "prompt for T2": "ok"},
                      stderr=f"warning {bearer}")
    leaky = make_task("T1", score=lambda f, w, output, r: Score(True, findings={"echo": output}, notes=output))
    summary = run(tmp_path, [leaky, make_task("T2")], [CODER], fake)
    text_on_disk = all_text(summary.run_dir)
    assert secret_key not in text_on_disk and "abcdefghijklmnopqrstuvwxyz0123456789" not in text_on_disk
    assert "[redacted]" in (summary.run_dir / "raw" / "T1-xkiro_qwen_qwen3-coder-plus_free.txt").read_text(encoding="utf-8")
    assert "[redacted]" in (summary.run_dir / "results.jsonl").read_text(encoding="utf-8")
    assert "=== stderr ===" in (summary.run_dir / "raw" / "T2-xkiro_qwen_qwen3-coder-plus_free.txt").read_text(encoding="utf-8")


def test_results_are_ascii_even_when_the_model_answers_in_other_characters(tmp_path):
    fake = FakeInvoke({"prompt for": "caf" + chr(0xE9) + " " + chr(0x2192) + " ok"})
    summary = run(tmp_path, [make_task()], [CODER], fake)
    assert (summary.run_dir / "results.jsonl").read_text(encoding="utf-8").isascii()
    assert (summary.run_dir / "summary.json").read_text(encoding="utf-8").isascii()
    raw = (summary.run_dir / summary.records[0].raw_output_path).read_text(encoding="utf-8")
    assert chr(0xE9) in raw  # the raw file keeps what the model said; only the readable outputs are ASCII


def test_one_failing_run_does_not_stop_the_rest(tmp_path):
    a, b, c = cand("xkiro/qwen/qwen3-coder-plus:free"), cand("xkiro/minimax/minimax-m3:free"), cand("slow/m1")
    fake = FakeInvoke(OK, raises={b.label: RuntimeError("boom sk-abcdefghijklmnop123456")})

    def exploding_score(fixture, workdir, output, result):
        raise ValueError("the scorer broke")

    tasks = [make_task("T1"), make_task("T2", score=exploding_score)]
    summary = run(tmp_path, tasks, [a, b, c], fake)
    assert len(summary.records) == 6 and len(fake.calls) == 6  # every (task, candidate) was tried
    by = {(r.task_id, r.candidate): r for r in summary.records}
    assert by[("T1", a.label)].score.success and by[("T1", c.label)].score.success
    failed = by[("T1", b.label)]
    assert not failed.score.success and failed.error.startswith("RuntimeError: boom") and "sk-abcdefghijklmnop123456" not in failed.error
    assert by[("T2", a.label)].error.startswith("ValueError: the scorer broke") and not by[("T2", a.label)].score.success
    assert "sk-abcdefghijklmnop123456" not in all_text(summary.run_dir)
    assert summary.status == "complete"


def test_a_call_that_exits_non_zero_is_a_failed_run_and_is_not_scored(tmp_path):
    scored = []
    task = make_task(score=lambda f, w, output, r: scored.append(output) or Score(True))
    fake = FakeInvoke({"prompt for": "partial answer"}, returncode=2, stderr="provider said no sk-abcdefghijklmnop123456")
    summary = run(tmp_path, [task], [CODER], fake)
    record = summary.records[0]
    assert scored == [] and not record.score.success
    assert record.error.startswith("the model call failed (exit 2)") and "sk-abcdefghijklmnop123456" not in record.error
    assert record.requests == 1  # the call was made, so it is counted


def test_the_ledger_records_the_requests_of_every_call_even_when_the_scorer_breaks(tmp_path):
    conn = db.connect(tmp_path / "ases.db")

    def broken(fixture, workdir, output, result):
        raise RuntimeError("no")

    fake = FakeInvoke(OK, requests=3)
    summary = run(tmp_path, [make_task("T1"), make_task("T2", score=broken)], [CODER, REVIEWER], fake, conn=conn)
    assert ledger.usage_today(conn, "xkiro", "qwen/qwen3-coder-plus:free") == 6  # two runs of three requests
    assert ledger.usage_today(conn, "openrouter", "cohere/north-mini-code:free") == 6
    assert sum(r.requests for r in summary.records) == 12
    kinds = [json.loads(e["payload"]) for e in conn.execute("SELECT payload FROM events WHERE kind = 'eval_run'")]
    assert len(kinds) == 4 and {k["task"] for k in kinds} == {"T1", "T2"} and kinds[0]["run_id"] == summary.run_id


def test_a_call_that_reports_no_requests_records_nothing_in_the_ledger(tmp_path):
    conn = db.connect(tmp_path / "ases.db")
    run(tmp_path, [make_task()], [CODER], FakeInvoke(OK, requests=0), conn=conn)
    assert ledger.usage_today(conn, "xkiro", "qwen/qwen3-coder-plus:free") == 0


def test_every_run_rechecks_the_quota_and_the_evaluation_stops_when_it_can_no_longer_be_afforded(tmp_path):
    """ASES-CAP-03 per run: the estimate said 40 of 50, but each call really cost 15, so the fourth run is not started."""
    conn = db.connect(tmp_path / "ases.db")
    fake = FakeInvoke(OK, requests=15)
    summary = run(tmp_path, [make_task(f"T{i}", est=10) for i in range(1, 5)], [REVIEWER], fake, conn=conn,
                  models_config=MODELS)  # 40 estimated of 50 free requests a day: allowed at the start
    assert len(fake.calls) == 3 and len(summary.records) == 3
    assert summary.status == "stopped" and summary.warnings[-1].startswith(f"stopped before T4 on {REVIEWER.label}: needs 10")
    assert ledger.usage_today_for_provider(conn, "openrouter") == 45
    assert len((summary.run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines()) == 3
    assert json.loads((summary.run_dir / "summary.json").read_text(encoding="utf-8"))["status"] == "stopped"
    # a run that was never started is a MISSING result, not a failed one, so compare() calls the run incomplete
    kept = evals.load_run(summary.run_dir)
    assert {r.task_id for r in kept} == {"T1", "T2", "T3"} and all(r.score.success for r in kept)
    pinned = [rec(f"T{i}", "p/m", requests=15, latency=1.0) for i in range(1, 5)]
    assert [(r.task, r.metric) for r in evals.compare(kept, pinned)] == [("T4", "coverage")]


def test_the_per_run_quota_check_needs_a_ledger_and_the_provider_limits_and_skips_free_tasks(tmp_path):
    fake = FakeInvoke(OK, requests=500)  # spends far more than any daily cap, but nothing is being checked
    summary = run(tmp_path, [make_task("T1"), make_task("T2")], [REVIEWER], fake)
    assert summary.status == "complete" and len(fake.calls) == 2
    conn = db.connect(tmp_path / "ases.db")
    free = run(tmp_path / "b", [make_task("T1", est=0), make_task("T2", est=0)], [REVIEWER], FakeInvoke(OK, requests=500),
               conn=conn, models_config=MODELS)
    assert free.status == "complete" and len(free.records) == 2  # a task that is estimated to cost nothing is not gated


def test_a_task_that_allows_a_retry_gets_a_second_call_only_when_its_answer_is_rejected(tmp_path):
    conn = db.connect(tmp_path / "ases.db")
    tokens = FakeInvoke({"You can call the tools below": lambda c, p: E9_CALL_OK if "rejected it" in p else E9_CALL_URGENT},
                        tokens=(100, 10))
    summary = run(tmp_path, [TASKS["E9"]], [CODER], tokens, conn=conn)
    record = summary.records[0]
    assert len(tokens.calls) == 2 and "error: argument 'priority'" in tokens.calls[1].prompt
    assert (record.retries, record.requests, record.input_tokens, record.output_tokens) == (1, 2, 200, 20)
    assert record.score.success and record.score.findings["retries_used"] == 1
    assert ledger.usage_today(conn, "xkiro", "qwen/qwen3-coder-plus:free") == 2
    raw = (summary.run_dir / record.raw_output_path).read_text(encoding="utf-8")
    assert "=== attempt 1 of 2 ===" in raw and "=== attempt 2 of 2 ===" in raw
    first_time = FakeInvoke({"You can call the tools below": E9_CALL_OK})
    once = run(tmp_path / "again", [TASKS["E9"]], [CODER], first_time)
    assert len(first_time.calls) == 1 and once.records[0].retries == 0


def test_a_retry_is_never_attempted_after_a_failed_call_or_past_the_limit(tmp_path):
    failing = FakeInvoke({"You can call the tools below": E9_CALL_URGENT}, returncode=1)
    run(tmp_path, [TASKS["E9"]], [CODER], failing)
    assert len(failing.calls) == 1  # an infrastructure failure is not a model error to correct
    stubborn = FakeInvoke({"You can call the tools below": E9_CALL_URGENT})
    summary = run(tmp_path / "second", [TASKS["E9"]], [CODER], stubborn)
    assert len(stubborn.calls) == 2 and summary.records[0].retries == 1 and not summary.records[0].score.success


def test_the_toolsets_of_a_task_reach_only_an_invoke_that_asks_for_them(tmp_path):
    seen = []

    def wants_tools(candidate, prompt, workdir, timeout, tools=()):
        seen.append((prompt, tools, timeout))
        return InvokeResult(0, "ok", "", 1.0, 1, 1, 1)

    run(tmp_path, [make_task("T1", tools=("file",)), make_task("T2")], [CODER], wants_tools)
    assert seen == [("prompt for T1", ("file",), 77), ("prompt for T2", (), 77)]
    plain = FakeInvoke(OK)  # four parameters only: called with exactly four
    run(tmp_path / "second", [make_task("T1", tools=("file",))], [CODER], plain)
    assert len(plain.calls) == 1
    star = []
    run(tmp_path / "third", [make_task("T1", tools=("file", "terminal"))], [CODER],
        lambda *args, **kwargs: star.append(kwargs) or InvokeResult(0, "ok", "", 1.0, 1, 1, 1))
    assert star == [{"tools": ("file", "terminal")}]


def test_every_run_gets_its_own_fresh_fixture_directory_that_is_removed_afterwards(tmp_path):
    fake = FakeInvoke(OK)
    run(tmp_path, [make_task("T1"), make_task("T2")], [CODER, MINIMAX], fake)
    dirs = [c.workdir for c in fake.calls]
    assert len(set(dirs)) == 4 and all(d.parent == tmp_path / "work" for d in dirs)
    assert all(c.marker_present for c in fake.calls)  # the fixture was built before the call
    assert list((tmp_path / "work").iterdir()) == []  # and cleaned up after it


def test_the_provider_rate_limit_is_respected_between_calls(tmp_path):
    clock = FakeClock()
    slow1, slow2 = cand("slow/m1"), cand("slow/m2")
    run(tmp_path, [make_task("T1"), make_task("T2")], [slow1, slow2], FakeInvoke(OK), models_config=MODELS, now=clock,
        sleep=clock.sleep)
    assert clock.sleeps == [pytest.approx(30.0)]  # per-model limit of 2 a minute: only m1's second call has to wait
    clock2 = FakeClock()
    run(tmp_path / "b", [make_task("T1"), make_task("T2")], [MINIMAX, cand("xkiro/qwen/qwen3.8-max:free", "lead")],
        FakeInvoke(OK), models_config=MODELS, now=clock2, sleep=clock2.sleep)
    assert clock2.sleeps == []  # no declared limit: no waiting
    clock3 = FakeClock()
    other = cand("openrouter/openrouter/free")
    run(tmp_path / "c", [make_task("T1"), make_task("T2")], [REVIEWER, other], FakeInvoke(OK), models_config=MODELS,
        now=clock3, sleep=clock3.sleep)
    assert clock3.sleeps == [pytest.approx(3.0)] * 3  # 20 a minute for the whole account: every call waits for the last


def test_a_slow_call_does_not_wait_twice(tmp_path):
    clock = FakeClock()
    slow = cand("slow/m1")

    def takes_forty_seconds(candidate, prompt, workdir, timeout):
        clock.t += 40.0  # longer than the 30 second gap
        return InvokeResult(0, "ok", "", 40.0, 1, 1, 1)

    run(tmp_path, [make_task("T1"), make_task("T2")], [slow], takes_forty_seconds, models_config=MODELS, now=clock,
        sleep=clock.sleep)
    assert clock.sleeps == []


def test_run_ids_do_not_collide_within_a_second(tmp_path):
    fixed = 1_800_000_000
    first = run(tmp_path, [make_task()], [CODER], FakeInvoke(OK), now=fixed)
    second = run(tmp_path, [make_task()], [CODER], FakeInvoke(OK), now=fixed)
    assert first.run_id != second.run_id and second.run_id == first.run_id + "-2"
    assert first.run_dir.is_dir() and second.run_dir.is_dir()


def test_a_different_model_answering_counts_as_a_fallback(tmp_path):
    other = run(tmp_path, [make_task()], [CODER], FakeInvoke(OK, served="openai/gpt-4o")).records[0]
    assert (other.fallbacks, other.served_model) == (1, "openai/gpt-4o")
    for i, served in enumerate(("qwen/qwen3-coder-plus:free", "xkiro/qwen/qwen3-coder-plus", "QWEN/qwen3-coder-plus", None)):
        same = run(tmp_path / f"same{i}", [make_task()], [CODER], FakeInvoke(OK, served=served)).records[0]
        assert same.fallbacks == 0, served  # the same model spelled with a provider prefix, a tag or other case


def test_findings_can_carry_review_and_human_counts_into_the_record(tmp_path):
    task = make_task(score=lambda f, w, o, r: Score(True, findings={"review_changes_required": 3, "human_interventions": "2"}))
    record = run(tmp_path, [task], [CODER], FakeInvoke(OK)).records[0]
    assert (record.review_changes_required, record.human_interventions) == (3, 2)
    plain = run(tmp_path / "b", [make_task()], [CODER], FakeInvoke(OK)).records[0]
    assert (plain.review_changes_required, plain.human_interventions) == (0, 0)


def test_an_interrupted_evaluation_keeps_what_it_finished(tmp_path):
    fake = FakeInvoke(OK, raises={"prompt for T2": KeyboardInterrupt()})
    with pytest.raises(KeyboardInterrupt):
        run(tmp_path, [make_task("T1"), make_task("T2"), make_task("T3")], [CODER], fake)
    (run_dir,) = list((tmp_path / "out").iterdir())
    assert len((run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    assert json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))["status"] == "interrupted"
    assert list((tmp_path / "work").iterdir()) == []


def test_every_standalone_task_can_run_end_to_end_and_its_findings_survive_redaction(tmp_path):
    """The real tasks, the runner and the writer together: nothing is blanked by the redactor, nothing is lost."""
    fake = FakeInvoke()
    summary = run(tmp_path, [TASKS[i] for i in tasks_mod.STANDALONE_IDS], [CODER], fake)
    assert [r.task_id for r in summary.records] == list(tasks_mod.STANDALONE_IDS)
    assert all(r.score.success for r in summary.records), [(r.task_id, r.error, r.score.notes) for r in summary.records if not r.score.success]
    on_disk = (summary.run_dir / "results.jsonl").read_text(encoding="utf-8")
    assert "[redacted]" not in on_disk
    assert evals.load_run(summary.run_dir) == list(summary.records)


# =====================================================================================================================
# default_invoke: the argv it builds and the accounting it reads (never a real call)
# =====================================================================================================================


@pytest.fixture
def hermes_stub(monkeypatch):
    monkeypatch.setattr(hermes, "hermes_path", lambda: "hermes")
    sessions = {}

    def session_usage(profile, session_id, timeout=60):
        sessions.setdefault("asked", []).append((profile, session_id))
        return sessions.get("answer")

    monkeypatch.setattr(hermes, "session_usage", session_usage)
    return sessions


def _fake_process(report=None, *, returncode=0, stdout="the answer", stderr="", timed_out=False, started=True, seen=None):
    def runner(argv, cwd, timeout):
        if seen is not None:
            seen.update(argv=list(argv), cwd=cwd, timeout=timeout)
        if report is not None:
            pathlib.Path(argv[argv.index("--usage-file") + 1]).write_text(json.dumps(report), encoding="utf-8")
        return evals._Process(returncode, stdout, stderr, timed_out, started)

    return runner


def test_default_invoke_builds_the_documented_one_shot_command(hermes_stub, tmp_path):
    seen = {}
    result = evals.default_invoke(CODER, "the prompt", tmp_path, 321, _run=_fake_process(seen=seen))
    argv = seen["argv"]
    assert argv[:10] == ["hermes", "-p", "coder-1", "-z", "the prompt", "-m", "qwen/qwen3-coder-plus:free", "--provider",
                         "xkiro", "--usage-file"]
    assert len(argv) == 11 and "-t" not in argv  # a text task passes no toolset
    assert seen["cwd"] == tmp_path and seen["timeout"] == 321
    assert not pathlib.Path(argv[10]).parent.exists()  # the usage file's temp directory is removed afterwards
    assert result.stdout == "the answer" and result.returncode == 0
    with_tools = {}
    evals.default_invoke(CODER, "p", tmp_path, 5, tools=("file", "terminal"), _run=_fake_process(seen=with_tools))
    assert with_tools["argv"][-2:] == ["-t", "file,terminal"]


# The shape of a real Hermes 0.21.3 --usage-file report, from the Phase 2 evaluation (a plain one-shot text call): one
# main API call plus one auxiliary title-generation call, so the provider's quota saw TWO requests.
REAL_USAGE_REPORT = {
    "estimated_cost_usd": 0.0, "cost_status": "unknown", "input_tokens": 18959, "output_tokens": 143, "total_tokens": 19102,
    "api_calls": 1, "model": "glm-5.3-thinking:free", "provider": "custom", "session_id": "20260918_175152_baba3e",
    "completed": True, "partial": False, "interrupted": False, "failed": False,
    "auxiliary": {"api_calls": 1, "input_tokens": 375, "output_tokens": 536, "total_tokens": 911,
                  "by_task": {"title_generation": {"api_calls": 1, "input_tokens": 375, "output_tokens": 536}}},
    "total_including_auxiliary": {"estimated_cost_usd": 0.0, "total_tokens": 20013, "api_calls": 2},
}


def test_default_invoke_counts_what_the_quota_counts_including_the_auxiliary_calls(hermes_stub, tmp_path):
    real = evals.default_invoke(CODER, "p", tmp_path, 5, _run=_fake_process(REAL_USAGE_REPORT))
    assert real.requests == 2  # the main call and the title generation: 'api_calls' alone would under-count the quota
    assert (real.input_tokens, real.output_tokens) == (18959 + 375, 143 + 536)
    assert real.served_model == "glm-5.3-thinking:free" and real.session_id == "20260918_175152_baba3e"
    assert "asked" not in hermes_stub  # the report has the numbers: no second hermes process is started
    main_only = {k: v for k, v in REAL_USAGE_REPORT.items() if k not in ("auxiliary", "total_including_auxiliary")}
    plain = evals.default_invoke(CODER, "p", tmp_path, 5, _run=_fake_process(main_only))
    assert (plain.requests, plain.input_tokens, plain.output_tokens) == (1, 18959, 143)


def test_default_invoke_asks_the_session_export_only_when_the_report_names_a_session_but_has_no_counts(hermes_stub, tmp_path):
    hermes_stub["answer"] = {"id": "S1", "model": "exported/model", "api_call_count": 7, "input_tokens": 700, "output_tokens": 70}
    exported = evals.default_invoke(CODER, "p", tmp_path, 5, _run=_fake_process({"session_id": "S1"}, returncode=2))
    assert (exported.requests, exported.input_tokens, exported.output_tokens) == (7, 700, 70)
    assert exported.served_model == "exported/model" and hermes_stub["asked"] == [("coder-1", "S1")]
    hermes_stub["answer"] = None  # the export failed too: a session was opened, so count one request
    assert evals.default_invoke(CODER, "p", tmp_path, 5, _run=_fake_process({"session_id": "S1"})).requests == 1


def test_default_invoke_counts_nothing_for_a_run_hermes_says_never_reached_its_agent(hermes_stub, tmp_path):
    early = {"failed": True, "failure": "provider not found", "api_calls": None, "session_id": None, "model": None}
    result = evals.default_invoke(CODER, "p", tmp_path, 5, _run=_fake_process(early, returncode=1))
    assert (result.requests, result.input_tokens, result.session_id, result.served_model) == (0, None, None, None)


def test_default_invoke_without_a_report_counts_one_request_only_for_a_call_that_started(hermes_stub, tmp_path):
    unknown = evals.default_invoke(CODER, "p", tmp_path, 5, _run=_fake_process(None))  # started, and Hermes wrote no report
    assert (unknown.requests, unknown.input_tokens, unknown.output_tokens, unknown.session_id) == (1, None, None, None)
    gone = evals.default_invoke(CODER, "p", tmp_path, 5, _run=_fake_process(returncode=-1, started=False, stderr="no such file"))
    assert gone.requests == 0 and gone.returncode == -1
    hermes_stub["answer"] = {"id": "S1", "model": "m", "api_call_count": 9, "input_tokens": 1, "output_tokens": 1}
    killed = evals.default_invoke(CODER, "p", tmp_path, 5, _run=_fake_process({"session_id": "S1"}, returncode=-1, timed_out=True))
    assert killed.timed_out and killed.requests == 1 and "asked" not in hermes_stub  # no export is asked for after a kill


def test_default_invoke_never_raises_when_hermes_is_missing(monkeypatch, tmp_path):
    def missing():
        raise hermes.HermesNotFound("`hermes` is not on PATH")

    monkeypatch.setattr(hermes, "hermes_path", missing)
    result = evals.default_invoke(CODER, "p", tmp_path, 5)
    assert (result.returncode, result.requests, result.stdout) == (-1, 0, "") and "not on PATH" in result.stderr


def test_default_invoke_refuses_a_prompt_too_long_for_a_windows_command_line(hermes_stub, monkeypatch, tmp_path):
    monkeypatch.setattr(evals, "_IS_WINDOWS", True)
    called = []
    result = evals.default_invoke(CODER, "x" * 40_000, tmp_path, 5, _run=lambda *a: called.append(a))
    assert called == [] and result.returncode == -1 and result.requests == 0 and "Windows limit" in result.stderr


def test_run_process_stops_the_whole_process_tree_on_a_timeout_and_never_raises(tmp_path):
    class Proc:
        pid = 4242

        def __init__(self):
            self.calls = 0

        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("hermes", timeout)
            return ("partial output", "some stderr")

    killed = []
    result = evals._run_process(["hermes"], tmp_path, 9, popen=lambda *a, **k: Proc(), kill_tree=killed.append)
    assert killed == [4242]  # the launcher AND its child: taskkill /T on Windows, never os.kill there
    assert (result.returncode, result.timed_out, result.started, result.stdout) == (-1, True, True, "partial output")
    assert "did not finish within 9s" in result.stderr and "some stderr" in result.stderr


def test_run_process_reports_a_command_that_cannot_start_and_a_normal_run(tmp_path):
    def cannot_start(*args, **kwargs):
        raise FileNotFoundError("no such file")

    started = evals._run_process(["hermes"], tmp_path, 9, popen=cannot_start)
    assert (started.returncode, started.started, started.timed_out) == (-1, False, False)

    class Done:
        pid = 1
        returncode = 0

        def communicate(self, timeout=None):
            return ("out", None)

    done = evals._run_process(["hermes"], tmp_path, 9, popen=lambda *a, **k: Done())
    assert (done.returncode, done.stdout, done.stderr, done.started) == (0, "out", "", True)


def test_run_process_starts_hermes_with_a_credential_scrubbed_environment(monkeypatch, tmp_path):
    """ASES-CFG-05 (blueprint 10.2): a provider key exported into the shell that runs `swarm eval` must not reach the
    hermes process; everything else, and the UTF-8 settings this function adds, must. No other argument changes."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "value-one")
    monkeypatch.setenv("MY_TOKEN", "value-two")
    monkeypatch.setenv("HARMLESS_SETTING", "kept")
    seen = {}

    class Done:
        pid = 1
        returncode = 0

        def communicate(self, timeout=None):
            return ("out", "")

    def popen(argv, **kwargs):
        seen.update(argv=list(argv), kwargs=kwargs)
        return Done()

    result = evals._run_process(["hermes", "-z", "p"], tmp_path, 9, popen=popen)
    assert result.returncode == 0 and seen["argv"] == ["hermes", "-z", "p"]
    env = seen["kwargs"]["env"]
    assert not {"OPENROUTER_API_KEY", "MY_TOKEN"} & set(env)
    assert "value-one" not in env.values() and "value-two" not in env.values()
    assert env["HARMLESS_SETTING"] == "kept" and env["PATH"] == os.environ["PATH"]
    assert env["PYTHONIOENCODING"] == "utf-8" and env["PYTHONUTF8"] == "1"
    assert {k: v for k, v in seen["kwargs"].items() if k != "env"} == {
        "cwd": str(tmp_path), "stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
        "text": True, "encoding": "utf-8", "errors": "replace",
    }


def test_the_process_killer_uses_taskkill_on_windows_and_never_os_kill(monkeypatch):
    calls = []
    monkeypatch.setattr(evals, "_IS_WINDOWS", True)
    monkeypatch.setattr(evals.subprocess, "run", lambda argv, **kwargs: calls.append(argv))
    monkeypatch.setattr(evals.os, "kill", lambda *a: pytest.fail("os.kill terminates a process on Windows"))
    evals._kill_process_tree(4242)
    assert calls == [["taskkill", "/PID", "4242", "/T", "/F"]]


# =====================================================================================================================
# reading a run back and the report
# =====================================================================================================================


def test_run_record_round_trips_through_its_dict_and_tolerates_missing_optional_fields():
    original = rec("E4", "a/x", tests=(3, 4), findings={"n": 1, "ok": True, "s": "t"}, retries=1, fallbacks=1, changes=2,
                   human=3, error="e", notes="n")
    assert evals.RunRecord.from_dict(json.loads(json.dumps(original.to_dict()))) == original
    minimal = evals.RunRecord.from_dict({"task_id": "E1", "candidate": "a/x"})
    assert (minimal.requests, minimal.input_tokens, minimal.error, minimal.score.success) == (0, None, "", False)


def test_load_run_says_what_is_wrong_instead_of_raising_a_traceback(tmp_path):
    with pytest.raises(EvalError, match="no results.jsonl"):
        evals.load_run(tmp_path / "missing")
    (tmp_path / "results.jsonl").write_text('{"task_id": "E1", "candidate": "a/x"}\nnot json\n', encoding="utf-8")
    with pytest.raises(EvalError, match="line 2 is not valid JSON"):
        evals.load_run(tmp_path)
    (tmp_path / "results.jsonl").write_text('{"task_id": "E1"}\n', encoding="utf-8")
    with pytest.raises(EvalError, match="line 1 is not a run record"):
        evals.load_run(tmp_path)
    (tmp_path / "results.jsonl").write_text('\n{"task_id": "E1", "candidate": "a/x", "surprise": 1}\n\n', encoding="utf-8")
    assert [r.task_id for r in evals.load_run(tmp_path)] == ["E1"]  # blank lines and unknown fields are fine
    (tmp_path / "results.jsonl").write_text("", encoding="utf-8")
    assert evals.load_run(tmp_path) == []


def test_the_report_has_one_table_per_metric_family_per_task_and_candidate(tmp_path):
    records = [
        rec("E1", "a/x", tests=(3, 4), requests=2, tin=100, tout=50, latency=3.0, retries=1),
        rec("E1", "b/y", success=False, requests=5, tin=None, tout=None, latency=9.5, fallbacks=1, changes=2, human=1),
        rec("E10", "a/x", latency=1.0),
        rec("E1", "a/x", requests=3, tin=10, tout=5, latency=5.0),
    ]
    report = evals.render_report(records)
    headings = [line for line in report.splitlines() if line.startswith("## ")]
    assert headings[:4] == ["## Outcome: task success and tests passed", "## Cost: model requests and tokens",
                            "## Time: latency per run",
                            "## Reliability: retries, fallbacks, review changes required, human interventions"]
    assert "| Task | a/x | b/y |" in report
    assert "| E1 Requirements | 2/2 pass (3/4 tests) | FAIL |" in report  # two runs of a/x on E1, one passed with tests 3/4
    assert "| E10 Review | pass | - |" in report  # b/y did not run E10
    assert "| E1 Requirements | 5 req, 110 in, 55 out | 5 req, n/a in, n/a out |" in report
    assert "| E1 Requirements | 4.0s | 9.5s |" in report  # mean latency of the two a/x runs
    assert "| E1 Requirements | 1 retries, 0 fallbacks, 0 changes, 0 human | 0 retries, 1 fallbacks, 2 changes, 1 human |" in report


def test_the_report_never_combines_the_measurements_into_one_score(tmp_path):
    report = evals.render_report([rec("E1", "a/x"), rec("E1", "b/y", success=False)]).lower()
    for word in ("score", "overall", "rating", "rank", "winner", "best"):
        assert word not in report
    assert "never merged into one number" in report


def test_the_report_is_ascii_whatever_the_names_and_errors_contain():
    odd = "caf" + chr(0xE9) + "/mod" + chr(0x2192) + "el"
    records = [rec("E1", odd, error="failed " + chr(0x2192) + " twice", notes="n" + chr(0xE9),
                   findings={"k": "v" + chr(0x2018)})]
    report = evals.render_report(records)
    assert report.isascii() and "\\u2192" in report and "caf\\xe9" in report


def test_a_pipe_in_a_candidate_name_cannot_break_the_markdown_tables():
    report = evals.render_report([rec("E1", "we|ird/x"), rec("E10", "we|ird/x")])
    table_lines = [line for line in report.splitlines() if line.startswith("|")]
    assert table_lines and all(line.count("|") == 3 for line in table_lines)  # Task | one candidate: three separators


def test_the_report_lists_findings_notes_and_errors_and_handles_no_runs():
    report = evals.render_report([rec("E4", "a/x", success=False, findings={"missing": "M02,M07", "count": 3},
                                      notes="two survived", error="boom")])
    assert "- E4 a/x: FAIL (count=3, missing=M02,M07)" in report
    assert "  note: two survived" in report and "  error: boom" in report
    assert evals.render_report([]) == "# Evaluation report\n\nNo runs were recorded.\n"


# =====================================================================================================================
# compare: the regression check before a pinned model changes
# =====================================================================================================================


def _pinned():
    return [rec("E1", "pinned/m", requests=10, latency=10.0), rec("E9", "pinned/m", requests=2, latency=4.0)]


def _candidate(**overrides):
    base = {"E1": dict(requests=10, latency=10.0), "E9": dict(requests=2, latency=4.0)}
    for task, changes in overrides.items():
        base[task].update(changes)
    return [rec(task, "cand/m", **values) for task, values in base.items()]


def test_compare_passes_an_equal_run_and_a_better_one():
    assert evals.compare(_candidate(), _pinned()) == []
    better = _candidate(E1=dict(requests=4, latency=2.0), E9=dict(requests=1, latency=1.0))
    assert evals.compare(better, _pinned()) == []
    was_failing = [rec("E1", "pinned/m", success=False, requests=10)]
    assert evals.compare([rec("E1", "cand/m", success=True, requests=10)], was_failing) == []  # an improvement


def test_compare_flags_a_success_regression_naming_task_metric_and_both_values():
    regressions = evals.compare(_candidate(E9=dict(success=False)), _pinned())
    assert len(regressions) == 1
    (found,) = regressions
    assert (found.task, found.metric, found.pinned_value, found.candidate_value) == ("E9", "success", 1.0, 0.0)
    assert "less often" in found.detail


def test_compare_flags_cost_and_latency_growth_beyond_the_tolerance_only():
    at_limit = _candidate(E1=dict(requests=12.5, latency=12.5))  # exactly 25 percent more: allowed
    assert evals.compare(at_limit, _pinned()) == []
    over = evals.compare(_candidate(E1=dict(requests=13, latency=13.0)), _pinned())
    assert [(r.task, r.metric, r.pinned_value, r.candidate_value) for r in over] == [
        ("E1", "requests", 10.0, 13.0), ("E1", "latency_seconds", 10.0, 13.0)]
    only_latency = evals.compare(_candidate(E9=dict(latency=6.0)), _pinned())
    assert [(r.task, r.metric) for r in only_latency] == [("E9", "latency_seconds")]
    assert evals.compare(_candidate(E1=dict(requests=13)), _pinned(), tolerance=0.5) == []
    assert evals.compare(_candidate(E1=dict(requests=11)), _pinned(), tolerance=0.0)[0].metric == "requests"


def test_compare_flags_a_task_the_candidate_never_ran_and_ignores_extra_ones():
    partial = [r for r in _candidate() if r.task_id == "E1"]
    (missing,) = evals.compare(partial, _pinned())
    assert (missing.task, missing.metric, missing.candidate_value) == ("E9", "coverage", "missing")
    extra = _candidate() + [rec("E5", "cand/m")]
    assert evals.compare(extra, _pinned()) == []


def test_compare_uses_success_rates_when_a_task_was_run_more_than_once():
    pinned = [rec("E1", "p/m"), rec("E1", "p/m")]
    candidate = [rec("E1", "c/m"), rec("E1", "c/m", success=False)]
    (found,) = evals.compare(candidate, pinned)
    assert (found.metric, found.pinned_value, found.candidate_value) == ("success", 1.0, 0.5)
    assert evals.compare(pinned, candidate) == []


def test_compare_rejects_a_negative_tolerance():
    with pytest.raises(EvalError, match="tolerance"):
        evals.compare(_candidate(), _pinned(), tolerance=-0.1)


# =====================================================================================================================
# role_value (E11) and recommend
# =====================================================================================================================


def test_role_value_compares_success_rate_and_requests_per_merged_task_over_shared_tasks():
    with_role = [rec("E1", "a/x", requests=2), rec("E2", "a/x", success=False, requests=4), rec("E3", "a/x")]
    without = [rec("E1", "b/y", success=False, requests=3), rec("E2", "b/y", success=False, requests=6)]
    value = evals.role_value(with_role, without)
    assert value.tasks_compared == ("E1", "E2") and (value.runs_with, value.runs_without) == (2, 2)
    assert (value.success_rate_with, value.success_rate_without, value.success_rate_delta) == (0.5, 0.0, 0.5)
    assert value.requests_per_merged_with == 6.0  # 6 requests, one merged task
    assert value.requests_per_merged_without is None and value.requests_per_merged_delta is None  # nothing merged


def test_role_value_with_both_sides_succeeding_gives_plain_numbers():
    with_role = [rec("E1", "a/x", requests=2), rec("E2", "a/x", requests=4)]
    without = [rec("E1", "b/y", requests=5), rec("E2", "b/y", success=False, requests=7)]
    value = evals.role_value(with_role, without)
    assert (value.success_rate_with, value.success_rate_without) == (1.0, 0.5)
    assert (value.requests_per_merged_with, value.requests_per_merged_without) == (3.0, 12.0)
    assert value.requests_per_merged_delta == -9.0
    empty = evals.role_value([rec("E1", "a/x")], [rec("E2", "b/y")])
    assert empty.tasks_compared == () and empty.success_rate_with is None and empty.success_rate_delta is None


def _candidates_models():
    return {"models": MODELS["models"] + [{"provider": "openrouter", "model": "openrouter/auto", "role_class": "coder"}]}


def test_recommend_picks_by_success_then_cost_and_reports_both_side_by_side():
    records = []
    for label, wins, requests in (("a/one", 2, 2), ("b/two", 2, 5), ("c/three", 1, 1)):
        for i, task in enumerate(("E1", "E2")):
            records.append(rec(task, label, success=i < wins, requests=requests))
    lines = evals.recommend(records, None)
    lead = next(line for line in lines if line.startswith("lead role"))
    assert "consider a/one (2 of 2 runs succeeded, 2.0 requests per success)" in lead
    assert "Others: b/two 2 of 2; c/three 1 of 2." in lead and "only 2 run(s)" in lead
    assert lines[-1] == evals.PINNING_SENTENCE and "your decision" in lines[-1]


def test_recommend_only_compares_candidates_that_ran_every_task_of_the_role():
    partial = [rec("E1", "a/one"), rec("E1", "b/two"), rec("E2", "b/two")]
    lead = next(line for line in evals.recommend(partial, None) if line.startswith("lead role"))
    assert "consider b/two" in lead and "a/one" not in lead
    none_complete = evals.recommend([rec("E1", "a/one")], None)
    assert "no candidate ran all of those tasks" in none_complete[0]
    nothing_worked = evals.recommend([rec("E1", "a/one", success=False), rec("E2", "a/one", success=False)], None)
    assert "no eligible candidate succeeded" in nothing_worked[0]


def test_recommend_never_proposes_a_dynamic_router_for_a_protected_role():
    """ASES-MOD-06: Lead, Reviewer and Debugger are never a dynamic free router; a worker may be."""
    router = "openrouter/openrouter/free"
    for role_tasks, role in ((("E1", "E2"), "lead"), (("E10", "E7"), "reviewer"), (("E4", "E3"), "debugger")):
        records = [rec(t, router, requests=1) for t in role_tasks] + [rec(t, "xkiro/plain", requests=9) for t in role_tasks]
        lines = [l for l in evals.recommend(records, _candidates_models()) if l.startswith(role + " role")]
        chosen = next(l for l in lines if "consider" in l)
        assert "consider xkiro/plain" in chosen, role  # the router is cheaper and equally good, and still not offered
        passed_over = [l for l in lines if "not eligible" in l]
        assert passed_over and router in passed_over[0] and "ASES-MOD-06" in passed_over[0]
        assert router not in chosen.split("Others:")[-1]
    worker = [rec(t, router, requests=1) for t in ("E4", "E5", "E6", "E9")] + [
        rec(t, "xkiro/plain", requests=9) for t in ("E4", "E5", "E6", "E9")]
    coder = next(l for l in evals.recommend(worker, _candidates_models()) if l.startswith("coder role"))
    assert "consider openrouter/openrouter/free" in coder  # a router may be a worker
    only_router = [rec("E1", router), rec("E2", router)]
    lines = evals.recommend(only_router, _candidates_models())
    assert any("no eligible candidate" in l for l in lines) and not any("consider" in l for l in lines)


def test_is_dynamic_router_reads_the_config_flag_or_the_shape_of_the_id():
    models = _candidates_models()
    assert evals.is_dynamic_router(models, "openrouter/openrouter/free")
    assert evals.is_dynamic_router(models, "openrouter/openrouter/auto")
    assert evals.is_dynamic_router(models, "openrouter/some/model")  # flagged router: true in the config row
    assert not evals.is_dynamic_router(models, "openrouter/cohere/north-mini-code:free")  # a :free tag is not a router
    assert evals.is_dynamic_router(None, "x/free") and not evals.is_dynamic_router(None, "x/qwen3:free")


def test_recommend_always_ends_with_the_pinning_sentence_even_with_nothing_to_say():
    assert evals.recommend([], None) == ["There are no runs for any role class to recommend from.", evals.PINNING_SENTENCE]
    assert all(line.isascii() for line in evals.recommend([rec("E1", "a/" + chr(0xE9))], None))


# =====================================================================================================================
# main: the swarm eval command line, exit codes 0 ok, 1 usage or refusal, 2 regression
# =====================================================================================================================


def _swarm_yaml(tmp_path):
    return f"""
project:
  name: ases
  environment: native
  data_class: public
  workspace_root: "{(tmp_path / 'ws').as_posix()}"
  ases_home: "{(tmp_path / 'home').as_posix()}"
  board: test-board
  integration_branch: integration
roles:
  lead: lead
  coder: coder-1
  reviewer: reviewer
concurrency:
  max_in_progress: 3
  per_profile: 1
  hard_max: 6
budgets:
  attempts_per_card: 3
  review_rounds_per_task: 3
  fix_cards_per_task: 2
  replans_per_project: 2
  max_cards: 40
  card_runtime_minutes: 45
  daily_reserve_percent: 10
  review_reserve_requests: 20
hermes:
  tested_version: "0.21.3"
  native_home: "{(tmp_path / 'hermes').as_posix()}"
"""


@pytest.fixture
def cfg(tmp_path, capsys):
    """The real config loaders reading small YAML files, and a call() that runs main with them and returns
    (exit code, stdout, stderr), checking that nothing printed is anything but ASCII."""
    import yaml

    models = tmp_path / "models.yaml"
    models.write_text(yaml.safe_dump(MODELS), encoding="utf-8")
    swarm = tmp_path / "swarm.yaml"
    swarm.write_text(_swarm_yaml(tmp_path), encoding="utf-8")

    def call(argv, **kwargs):
        kwargs.setdefault("models_path", models)
        kwargs.setdefault("swarm_path", swarm)
        kwargs.setdefault("workroot", tmp_path / "work")
        kwargs.setdefault("sleep", lambda s: None)
        code = evals.main(argv, **kwargs)
        out, err = capsys.readouterr()
        assert out.isascii() and err.isascii()  # the Windows console is cp1252
        return code, out, err

    return types.SimpleNamespace(call=call, home=tmp_path / "home", tmp=tmp_path, models=models, swarm=swarm)


def _write_run(directory, records):
    directory.mkdir(parents=True)
    (directory / "results.jsonl").write_text(
        "".join(json.dumps(r.to_dict()) + "\n" for r in records), encoding="utf-8")
    return directory


def test_list_prints_every_task_the_request_estimates_and_the_phase_2_short_list(capsys):
    code = evals.main(["list"])  # exactly how the swarm command calls it: one positional argument
    out = capsys.readouterr().out
    assert code == 0 and out.isascii()
    for task in TASKS.values():
        assert task.id in out and task.title in out
    assert "Phase 2 of the roadmap runs only E1, E9, E10" in out and "--spend-quota" in out


def test_usage_errors_exit_with_1_never_with_argparses_2(cfg):
    for argv in ([], ["bogus"], ["run"], ["run", "--tasks", "E1"], ["run", "--candidates", "a/b"], ["report"],
                 ["compare", "only-one"], ["compare", "a", "b", "--tolerance", "abc"]):
        code, out, err = cfg.call(argv)
        assert code == 1, argv
        assert err  # something on stderr says what was wrong
    assert "swarm eval:" in cfg.call(["bogus"])[2]


def test_help_exits_zero(cfg):
    code, out, _ = cfg.call(["--help"])
    assert code == 0 and "real provider quota" in " ".join(out.split())  # argparse wraps its help text
    for name in ("list", "run", "report", "compare", "role-value"):
        assert name in out
    code, out, _ = cfg.call(["run", "--help"])
    assert code == 0 and "--spend-quota" in out


def test_run_without_spend_quota_is_a_dry_run_that_prints_the_plan_and_calls_nothing(cfg):
    fake = FakeInvoke()
    code, out, err = cfg.call(["run", "--tasks", "E1,E9,E10", "--candidates", f"{CODER.label},{REVIEWER.label}"], invoke=fake)
    assert code == 0 and err == "" and fake.calls == []
    assert "a dry run: nothing was called and no quota was spent" in out
    assert "estimated requests: 16 in total" in out and "provider xkiro: 8" in out and "provider openrouter: 8" in out
    assert "about 0.4 minutes" in out and "profile coder-1" in out and "profile reviewer" in out
    assert "every provider can afford its share today" in out and "--spend-quota" in out
    assert not (cfg.home / "evals").exists()


def test_run_with_spend_quota_calls_the_model_and_keeps_the_results_under_ases_home(cfg):
    fake = FakeInvoke()
    code, out, err = cfg.call(["run", "--tasks", "E1,E10", "--candidates", CODER.label, "--spend-quota"], invoke=fake)
    assert code == 0 and err == "" and len(fake.calls) == 2
    (run_dir,) = list((cfg.home / "evals").iterdir())
    assert len(evals.load_run(run_dir)) == 2 and (run_dir / "summary.json").is_file()
    assert "PASS" in out and "No combined score is computed" in out and str(run_dir) in out
    conn = db.connect(cfg.home / "ases.db")
    assert ledger.usage_today(conn, "xkiro", "qwen/qwen3-coder-plus:free") == 2  # the real requests reached the ledger


def test_run_that_the_quota_cuts_short_exits_1_and_says_where_it_stopped(cfg):
    fake = FakeInvoke(requests=40)  # each call costs far more than the estimate of 2
    code, out, err = cfg.call(["run", "--tasks", "E1,E10", "--candidates", REVIEWER.label, "--spend-quota"], invoke=fake)
    assert code == 1 and len(fake.calls) == 1 and err == ""
    assert "(stopped)" in out and "warning: stopped before E10 on " + REVIEWER.label in out


def test_run_out_overrides_where_the_results_go(cfg):
    target = cfg.tmp / "elsewhere"
    code, *_ = cfg.call(["run", "--tasks", "E1", "--candidates", CODER.label, "--spend-quota", "--out", str(target)],
                        invoke=FakeInvoke())
    assert code == 0 and len(list(target.iterdir())) == 1 and not (cfg.home / "evals").exists()


def test_run_that_cannot_write_its_results_is_an_error_with_exit_1_not_a_traceback(cfg):
    blocker = cfg.tmp / "a-file-not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    fake = FakeInvoke()
    code, out, err = cfg.call(
        ["run", "--tasks", "E1", "--candidates", CODER.label, "--spend-quota", "--out", str(blocker / "results")], invoke=fake)
    assert code == 1 and "a file operation failed" in err
    assert fake.calls == []  # nothing was called: the results directory is made before any model is


def test_run_names_the_profile_from_the_role_map_or_the_profile_flag(cfg):
    fake = FakeInvoke()
    cfg.call(["run", "--tasks", "E1", "--candidates", MINIMAX.label, "--spend-quota"], invoke=fake)
    assert fake.calls[0].candidate.profile == "coder-1"  # coder_candidate is played by the coder profile
    cfg.call(["run", "--tasks", "E1", "--candidates", MINIMAX.label, "--spend-quota", "--profile", "tester"], invoke=fake)
    assert fake.calls[1].candidate.profile == "tester"


def test_run_with_an_unknown_task_or_candidate_is_an_error_not_a_guess(cfg):
    fake = FakeInvoke()
    code, out, err = cfg.call(["run", "--tasks", "E1", "--candidates", "xkiro/nope"], invoke=fake)
    assert code == 1 and out == "" and "unknown candidate 'xkiro/nope'" in err and CODER.label in err
    code, out, err = cfg.call(["run", "--tasks", "E1,E12", "--candidates", CODER.label], invoke=fake)
    assert code == 1 and "unknown task 'E12'" in err
    assert fake.calls == []


def test_run_of_a_task_that_needs_the_swarm_says_how_and_never_calls_the_model(cfg):
    fake = FakeInvoke()
    dry = cfg.call(["run", "--tasks", "E1,E8", "--candidates", CODER.label], invoke=fake)
    assert dry[0] == 1 and "refused:" in dry[1] and "swarm run" in dry[1]  # a dry run shows the refusal too
    real = cfg.call(["run", "--tasks", "E8", "--candidates", CODER.label, "--spend-quota"], invoke=fake)
    assert real[0] == 1 and "swarm approve" in real[2] and real[1] == ""
    assert cfg.call(["run", "--tasks", "E11", "--candidates", CODER.label, "--spend-quota"], invoke=fake)[0] == 1
    assert fake.calls == []


def test_run_that_the_days_quota_cannot_afford_is_refused_before_it_starts(cfg):
    conn = db.connect(cfg.home / "ases.db")
    ledger.record_usage(conn, "openrouter", "cohere/north-mini-code:free", 45)  # 50 free requests a day, 45 already used
    conn.close()
    fake = FakeInvoke()
    argv = ["run", "--tasks", "E1,E9,E10", "--candidates", REVIEWER.label]
    dry = cfg.call(argv, invoke=fake)
    assert dry[0] == 1 and "refused:" in dry[1] and "provider openrouter" in dry[1]
    real = cfg.call([*argv, "--spend-quota"], invoke=fake)
    assert real[0] == 1 and "provider openrouter" in real[2] and real[1] == ""
    assert fake.calls == [] and not (cfg.home / "evals").exists()


def test_run_needs_a_readable_configuration_and_says_which_file_is_broken(cfg):
    argv = ["run", "--tasks", "E1", "--candidates", CODER.label]
    code, _, err = cfg.call(argv, swarm_path=cfg.tmp / "missing-swarm.yaml")
    assert code == 1 and "cannot read config/swarm.yaml" in err
    code, _, err = cfg.call(argv, models_path=cfg.tmp / "missing-models.yaml")
    assert code == 1 and "cannot read config/models.yaml" in err
    (cfg.tmp / "broken.yaml").write_text("providers: [unclosed", encoding="utf-8")
    assert cfg.call(argv, models_path=cfg.tmp / "broken.yaml")[0] == 1


def test_report_prints_the_tables_and_the_recommendations_and_reads_only(cfg):
    fake = FakeInvoke()
    cfg.call(["run", "--tasks", "E1,E2", "--candidates", f"{CODER.label},{REVIEWER.label}", "--spend-quota"], invoke=fake)
    (run_dir,) = list((cfg.home / "evals").iterdir())
    before = sorted(p.name for p in run_dir.rglob("*"))
    code, out, err = cfg.call(["report", str(run_dir)])
    assert code == 0 and err == ""
    assert "## Outcome" in out and "## Recommendations" in out and evals.PINNING_SENTENCE in out
    assert "| E1 Requirements | pass | pass |" in out and "lead role (judged on E1, E2): consider" in out
    assert sorted(p.name for p in run_dir.rglob("*")) == before  # a report changes nothing
    code, _, err = cfg.call(["report", str(cfg.tmp / "nowhere")])
    assert code == 1 and "no results.jsonl" in err


def test_report_still_works_when_the_models_config_cannot_be_read(cfg):
    run_dir = _write_run(cfg.tmp / "r", [rec("E1", "a/x"), rec("E2", "a/x")])
    code, out, _ = cfg.call(["report", str(run_dir)], models_path=cfg.tmp / "gone.yaml")
    assert code == 0 and "## Recommendations" in out and evals.PINNING_SENTENCE in out


def test_compare_exits_0_for_an_equal_run_and_2_for_a_regression(cfg):
    pinned = _write_run(cfg.tmp / "pinned", [rec("E1", "p/m", requests=10, latency=10.0), rec("E9", "p/m")])
    same = _write_run(cfg.tmp / "same", [rec("E1", "c/m", requests=10, latency=10.0), rec("E9", "c/m")])
    code, out, _ = cfg.call(["compare", str(same), str(pinned)])
    assert code == 0 and "No regression" in out
    worse = _write_run(cfg.tmp / "worse", [rec("E1", "c/m", success=False, requests=10, latency=10.0), rec("E9", "c/m")])
    code, out, err = cfg.call(["compare", str(worse), str(pinned)])
    assert code == 2 and err == ""
    assert "1 regression(s)" in out and "E1: success: pinned 1.0, candidate 0.0" in out


def test_compare_tolerance_flag_widens_or_narrows_the_cost_allowance(cfg):
    pinned = _write_run(cfg.tmp / "pinned", [rec("E1", "p/m", requests=10)])
    costly = _write_run(cfg.tmp / "costly", [rec("E1", "c/m", requests=14)])
    assert cfg.call(["compare", str(costly), str(pinned)])[0] == 2  # 40 percent more, over the default 25 percent
    assert cfg.call(["compare", str(costly), str(pinned), "--tolerance", "0.5"])[0] == 0
    assert cfg.call(["compare", str(costly), str(pinned), "--tolerance", "-1"])[0] == 1  # a negative tolerance is refused


def test_compare_needs_to_know_which_candidate_of_a_multi_candidate_run_to_use(cfg):
    pinned = _write_run(cfg.tmp / "pinned", [rec("E1", "p/m")])
    both = _write_run(cfg.tmp / "both", [rec("E1", "c/one"), rec("E1", "c/two", success=False)])
    code, _, err = cfg.call(["compare", str(both), str(pinned)])
    assert code == 1 and "holds 2 candidates" in err and "--candidate" in err
    assert cfg.call(["compare", str(both), str(pinned), "--candidate", "c/one"])[0] == 0
    assert cfg.call(["compare", str(both), str(pinned), "--candidate", "c/two"])[0] == 2
    code, _, err = cfg.call(["compare", str(both), str(pinned), "--candidate", "c/none"])
    assert code == 1 and "no candidate 'c/none'" in err
    assert cfg.call(["compare", str(pinned), str(both), "--pinned", "c/one"])[0] == 0
    assert cfg.call(["compare", str(pinned), str(both)])[0] == 1  # the pinned side is ambiguous too
    assert cfg.call(["compare", str(cfg.tmp / "nowhere"), str(pinned)])[0] == 1


def test_role_value_prints_plain_numbers_for_two_runs(cfg):
    with_role = _write_run(cfg.tmp / "with", [rec("E1", "a/x", requests=2), rec("E2", "a/x", requests=4)])
    without = _write_run(cfg.tmp / "without", [rec("E1", "b/y", requests=5), rec("E2", "b/y", success=False, requests=7)])
    code, out, _ = cfg.call(["role-value", str(with_role), str(without)])
    assert code == 0 and "Role value over 2 shared task(s): E1, E2" in out
    assert "with the role:    2 runs, success rate 1.00, requests per merged task 3.00" in out
    assert "without the role: 2 runs, success rate 0.50, requests per merged task 12.00" in out
    assert "your decision" in out
    assert cfg.call(["role-value", str(with_role), str(cfg.tmp / "nowhere")])[0] == 1


def test_no_subcommand_ever_prints_a_non_ascii_character_even_for_hostile_names(cfg):
    odd = "caf" + chr(0xE9) + "/mod" + chr(0x2192) + "el"
    run_dir = _write_run(cfg.tmp / "odd", [rec("E1", odd, error="x" + chr(0x2192), notes="y" + chr(0xE9))])
    for argv in (["report", str(run_dir)], ["role-value", str(run_dir), str(run_dir)], ["compare", str(run_dir), str(run_dir)]):
        assert cfg.call(argv)[0] == 0  # cfg.call asserts everything printed is ASCII
    code, _, err = cfg.call(["run", "--tasks", "E1", "--candidates", odd])
    assert code == 1 and "unknown candidate" in err


def test_main_is_the_function_the_swarm_command_lazily_imports():
    import inspect

    assert callable(evals.main) and list(inspect.signature(evals.main).parameters)[0] == "argv"
    required = [p for p in inspect.signature(evals.main).parameters.values()
                if p.default is inspect.Parameter.empty]
    assert [p.name for p in required] == ["argv"]  # the test hooks are all optional keyword arguments


# =====================================================================================================================
# precision: each keyword pattern, boundary values and error paths pinned one by one
# =====================================================================================================================

E1_SAMPLES = {
    "users_and_roles": "Shop staff and an admin use it.",
    "order_changes": "Customers may cancel before pickup.",
    "payment": "Payment is taken by card at checkout.",
    "reminders": "Reminders go out by SMS.",
    "performance": "Pages load in under two seconds.",
    "security_privacy": "Passwords are hashed and traffic uses HTTPS.",
    "devices": "A responsive web app for iOS and Android browsers.",
    "shop_view": "Shops get a dashboard of upcoming orders.",
    "growth": "The design must scale to more shops.",
}
E2_SAMPLES = {
    "event_intake": "An intake API receives events.",
    "queue": "A durable queue buffers messages.",
    "template_renderer": "A template renderer builds the text.",
    "channel_adapters": "An email adapter and an SMS adapter deliver it.",
    "retry_scheduler": "Failures are retried with backoff.",
    "dead_letter_store": "A dead-letter store keeps what failed.",
    "audit_log": "Every attempt goes to the audit log.",
    "status_query": "Operators call the status API.",
    "deduplication": "Idempotency keys drop duplicates.",
}
E7_SEEDED_SAMPLES = {
    "sql_injection": "SQL injection in get_user.",
    "path_traversal": "Path traversal in download.",
    "command_injection": "Command injection via shell=True.",
    "hardcoded_credential": "A hard-coded database password.",
    "weak_password_hash": "MD5 is a weak hash.",
    "insecure_deserialization": "Unsafe pickle deserialization.",
}
E7_ABSENT_SAMPLES = {"xss": "XSS", "csrf": "CSRF", "xxe": "XXE", "ssrf": "SSRF", "open_redirect": "an open redirect"}


@pytest.mark.parametrize("topics, samples", [
    (texttasks.E1_TOPICS, E1_SAMPLES), (texttasks.E2_COMPONENTS, E2_SAMPLES), (texttasks.E7_SEEDED, E7_SEEDED_SAMPLES),
    (texttasks.E7_ABSENT, E7_ABSENT_SAMPLES),
])
def test_every_keyword_topic_is_triggered_by_its_own_sample_and_by_nothing_neutral(topics, samples):
    assert set(samples) == set(topics)  # a topic added without a sample here is caught
    for name, sentence in samples.items():
        assert text.topic_hits(sentence, topics)[name] is True, name
    assert not any(text.topic_hits("We will build something nice and keep it tidy.", topics).values())


def test_a_hard_coded_path_or_a_plain_hash_mention_is_not_a_seeded_vulnerability():
    hits = text.topic_hits("The upload path is hard-coded, and a hash table would help.", texttasks.E7_SEEDED)
    assert not hits["hardcoded_credential"] and not hits["weak_password_hash"]
    assert text.topic_hits("The password is hardcoded in the source.", texttasks.E7_SEEDED)["hardcoded_credential"]


@pytest.mark.parametrize("line, is_interface", [
    ("POST /events -> 202 {message_id}", True), ("GET /messages/{id}/status", True), ("render(template_id, context)", True),
    ("intake -> queue", True), ("a => b", True), ("Queue (durable broker) buffers messages", False),
    ("A plain sentence about the design.", False), ("The user posts events to /events", False),
])
def test_e2_interface_lines_are_verbs_with_paths_calls_or_arrows(line, is_interface):
    assert bool(texttasks._INTERFACE_LINE.search(line)) is is_interface


def test_e3_a_question_needs_every_one_of_its_facts(tmp_path):
    facts = {1: ("INVENTORY_DB", "config.py"), 2: ("37", "config.py"), 3: ("CatalogNotFoundError", "storage.py"),
             4: ("bulk_discount", "pricing.py", "25"), 5: ("report", "4"), 6: ("config", "pricing", "storage")}
    every = "\n".join(f"Q{n}: {' '.join(parts)}" for n, parts in facts.items())
    assert _score("E3", every, tmp_path, name="all").tests_passed == 6
    counter = 0
    for n, parts in facts.items():
        for missing in parts:
            counter += 1
            reduced = "\n".join(
                f"Q{k}: {' '.join(p for p in ps if not (k == n and p == missing))}" for k, ps in facts.items())
            score = _score("E3", reduced, tmp_path, name=f"d{counter}")
            assert score.tests_passed == 5 and f"Q{n}" in score.findings["missing_answers"], (n, missing)


def test_e3_facts_are_matched_as_whole_tokens_not_as_substrings(tmp_path):
    sloppy = E3_GOOD.replace("37", "137").replace("25 units", "2500 units").replace("4 or less", "4.5 or less")
    score = _score("E3", sloppy, tmp_path)
    assert score.findings["missing_answers"] == "Q2,Q4,Q5"
    misspelled = _score("E3", E3_GOOD.replace("inventory/config.py", "inventory/myconfig.py"), tmp_path, name="b")
    assert "Q1" in misspelled.findings["missing_answers"]  # 'myconfig.py' is not 'config.py'


def test_e9_boundary_values_of_the_tool_schema():
    check = texttasks.check_tool_call

    def docs(limit):
        return check({"tool": "search_docs", "arguments": {"query": "x", "limit": limit}})

    assert docs(1) == [] and docs(20) == [] and docs(0) and docs(21) and docs(-5)
    assert check({"tool": "search_docs", "arguments": {"query": "x"}}) == []  # the limit is optional
    for good in (5, 5.5, 0, -3):
        assert check({"tool": "convert_currency", "arguments": {"amount": good, "from_currency": "USD", "to_currency": "EUR"}}) == []
    for bad in (True, "5", None, [5]):
        assert check({"tool": "convert_currency", "arguments": {"amount": bad, "from_currency": "USD", "to_currency": "EUR"}})
    for bad_code in ("US", "USDX", "usd", "U5D", 5):
        assert check({"tool": "convert_currency", "arguments": {"amount": 1, "from_currency": bad_code, "to_currency": "EUR"}})
    assert check({"tool": "get_weather", "arguments": {"city": "Oslo", "unit": "kelvin"}})  # not in the enum
    assert check({"tool": "get_weather", "arguments": {"city": "", "unit": "celsius"}})  # an empty string is not a city
    assert check({"tool": ["get_weather"], "arguments": {}})  # the tool name must be a string


def test_e10_scores_a_review_that_has_no_status_line():
    assert TASKS["E10"].score({}, pathlib.Path("."), "- shop/pricing.py: an off-by-one", R).success


def test_diff_hunks_land_on_the_occurrence_nearest_the_hinted_line():
    original = "a\nprint(1)\nb\nprint(1)\nc\n"
    second = codeeval.apply_unified_diff(original, "--- a/x\n+++ b/x\n@@ -4,1 +4,1 @@\n-print(1)\n+print(2)\n")
    assert second == ("a\nprint(1)\nb\nprint(2)\nc\n", None)
    first = codeeval.apply_unified_diff(original, "--- a/x\n+++ b/x\n@@ -2,1 +2,1 @@\n-print(1)\n+print(2)\n")
    assert first == ("a\nprint(2)\nb\nprint(1)\nc\n", None)
    unhinted = codeeval.apply_unified_diff(original, "--- a/x\n+++ b/x\n@@ -0,0 +0,0 @@\n-print(1)\n+print(2)\n")
    assert unhinted == ("a\nprint(2)\nb\nprint(1)\nc\n", None)  # no usable hint: the first occurrence


def test_a_pure_insertion_hunk_goes_where_its_header_says():
    original = "a\nb\nc\n"
    assert codeeval.apply_unified_diff(original, "--- a/x\n+++ b/x\n@@ -1,0 +2,2 @@\n+X\n+Y\n") == ("a\nX\nY\nb\nc\n", None)
    assert codeeval.apply_unified_diff(original, "--- a/x\n+++ b/x\n@@ -99,0 +99,1 @@\n+END\n") == ("a\nb\nc\nEND\n", None)


def test_parse_unified_diff_reads_git_style_multi_file_diffs_and_skips_markers():
    diff = (
        "diff --git a/one.py b/one.py\nindex 111..222 100644\n--- a/one.py\n+++ b/one.py\n@@ -1 +1 @@\n-a\n+b\n"
        "\\ No newline at end of file\n"
        "diff --git a/two.py b/two.py\nnew file mode 100644\n--- /dev/null\n+++ b/two.py\n@@ -0,0 +1,2 @@\n+x\n+y\n"
    )
    first, second = codeeval.parse_unified_diff(diff)
    assert (first.old_path, first.new_path) == ("a/one.py", "b/one.py") and first.hunks[0].lines == [("-", "a"), ("+", "b")]
    assert (second.old_path, second.new_path) == ("/dev/null", "b/two.py") and second.hunks[0].lines == [("+", "x"), ("+", "y")]


def test_a_removed_line_that_looks_like_a_file_header_stays_a_removed_line():
    diff = "--- a/x.sql\n+++ b/x.sql\n@@ -1,2 +1,1 @@\n--- a comment\n keep\n"
    (patch,) = codeeval.parse_unified_diff(diff)
    assert patch.hunks[0].lines == [("-", "-- a comment"), (" ", "keep")]


def test_run_pytest_stop_first_stops_at_the_first_failure(tmp_path):
    codeeval.write_tree(tmp_path, {"test_x.py": "def test_a():\n    assert False\n\n\ndef test_b():\n    assert False\n"})
    assert codeeval.run_pytest(tmp_path).failed == 2
    assert codeeval.run_pytest(tmp_path, stop_first=True).failed == 1


def test_write_tree_and_copy_tree_keep_line_endings_and_skip_bytecode(tmp_path):
    src = tmp_path / "src"
    codeeval.write_tree(src, {"a/b.txt": "x\ny\n", "c.py": "z = 1\n"})
    assert (src / "a" / "b.txt").read_bytes() == b"x\ny\n"  # LF on every platform
    (src / "__pycache__").mkdir()
    (src / "__pycache__" / "c.cpython-311.pyc").write_bytes(b"0")
    (src / ".pytest_cache").mkdir()
    codeeval.copy_tree(src, tmp_path / "dest")
    assert sorted(p.name for p in (tmp_path / "dest").iterdir()) == ["a", "c.py"]


def test_score_and_model_types_keep_scalar_findings_and_tolerate_odd_input():
    score = Score(True, 1, 2, {"n": 1, "ok": True, "f": 1.5, "s": "t", "l": [1, 2]}, "note")
    stored = score.to_dict()
    assert stored["findings"] == {"n": 1, "ok": True, "f": 1.5, "s": "t", "l": "[1, 2]"}
    assert Score.from_dict({"success": 1, "findings": "not a dict"}) == Score(True)
    assert Score.from_dict({}) == Score(False)
    assert evals._int_or_none(True) is None and evals._int_or_none("5") == 5 and evals._int_or_none(None) is None
    assert evals._int_or_none("x") is None and evals._int_or_none(float("inf")) is None and evals._int_or_none(3.9) == 3


def test_clock_accepts_none_a_callable_a_datetime_or_a_number():
    import datetime as dt

    assert evals._clock(None) is evals.time.time
    fixed = dt.datetime(2026, 9, 21, 12, 0, 0, tzinfo=dt.timezone.utc)
    assert evals._clock(fixed)() == fixed.timestamp()
    assert evals._clock(1234)() == 1234.0
    ticking = iter([1.0, 2.0])
    assert evals._clock(lambda: next(ticking))() == 1.0


def test_the_process_killer_on_other_platforms_signals_the_pid_and_never_raises(monkeypatch):
    sent = []
    monkeypatch.setattr(evals, "_IS_WINDOWS", False)
    monkeypatch.setattr(evals.os, "kill", lambda pid, sig: sent.append(pid))
    evals._kill_process_tree(77)
    assert sent == [77]

    def already_gone(pid, sig):
        raise ProcessLookupError()

    monkeypatch.setattr(evals.os, "kill", already_gone)
    evals._kill_process_tree(77)  # the process ended by itself: not an error
    monkeypatch.setattr(evals, "_IS_WINDOWS", True)

    def taskkill_missing(*args, **kwargs):
        raise FileNotFoundError("taskkill")

    monkeypatch.setattr(evals.subprocess, "run", taskkill_missing)
    evals._kill_process_tree(77)  # nor is a missing taskkill


def test_check_budget_skips_a_provider_that_is_not_asked_for_anything_even_when_its_quota_is_gone(tmp_path):
    conn = db.connect(tmp_path / "ases.db")
    ledger.record_usage(conn, "openrouter", "m", 50)  # the whole daily cap
    assert evals.check_budget(conn, MODELS, evals.estimate([make_task(est=0)], [REVIEWER]), budgets=BUDGETS) == []
    assert evals.check_budget(conn, MODELS, evals.estimate([make_task(est=1)], [REVIEWER]))[0].startswith("provider openrouter")


def test_two_candidates_whose_names_map_to_one_file_name_keep_separate_raw_files(tmp_path):
    a, b = cand("xkiro/team:a"), cand("xkiro/team_a")  # both read xkiro_team_a once made file safe
    summary = run(tmp_path, [make_task()], [a, b], FakeInvoke(OK))
    paths = [r.raw_output_path for r in summary.records]
    assert paths == ["raw/T1-xkiro_team_a.txt", "raw/T1-xkiro_team_a-2.txt"]
    assert all((summary.run_dir / p).is_file() for p in paths)


def test_unknown_token_counts_stay_unknown_instead_of_becoming_zero(tmp_path):
    record = run(tmp_path, [make_task()], [CODER], FakeInvoke(OK, tokens=(None, None))).records[0]
    assert (record.input_tokens, record.output_tokens) == (None, None)
    half = run(tmp_path / "b", [make_task("T1"), make_task("T2")], [CODER], FakeInvoke(OK, tokens=(None, 7))).records[0]
    assert (half.input_tokens, half.output_tokens) == (None, 7)


def test_a_fixture_with_a_read_only_file_is_still_removed(tmp_path):
    import dataclasses
    import os
    import stat

    def read_only_fixture(workdir):
        path = workdir / "locked.txt"
        path.write_text("x")
        os.chmod(path, stat.S_IREAD)
        (workdir / "marker.txt").write_text("x")
        return {}

    task = dataclasses.replace(make_task(), build_fixture=read_only_fixture)
    run(tmp_path, [task], [CODER], FakeInvoke(OK))
    assert list((tmp_path / "work").iterdir()) == []


def test_a_failed_call_with_nothing_on_stderr_reports_what_it_printed(tmp_path):
    record = run(tmp_path, [make_task()], [CODER], FakeInvoke({"prompt for": "provider overloaded"}, returncode=1)).records[0]
    assert "(exit 1): provider overloaded" in record.error


def test_a_ledger_that_cannot_be_written_becomes_a_warning_not_a_crash(tmp_path):
    import sqlite3

    class LockedDatabase:
        def execute(self, *args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

    summary = run(tmp_path, [make_task("T1"), make_task("T2")], [CODER], FakeInvoke(OK, requests=2), conn=LockedDatabase())
    assert summary.status == "complete" and len(summary.records) == 2 and all(r.score.success for r in summary.records)
    assert sum("could not record 2 request(s)" in w for w in summary.warnings) == 2
    assert sum("could not record the eval_run event" in w for w in summary.warnings) == 2
    assert "database is locked" in summary.warnings[0]


def test_the_list_and_the_plan_warn_that_some_tasks_run_model_written_code(cfg):
    listing = cfg.call(["list"])[1]
    assert "run code a model wrote" in listing and "your own user rights" in listing
    with_code = cfg.call(["run", "--tasks", "E1,E4", "--candidates", CODER.label])[1]
    assert "note:       E4, E5 and E6 run code a model wrote" in with_code
    with_files = cfg.call(["run", "--tasks", "E3", "--candidates", CODER.label])[1]
    assert "file tools (which can write)" in with_files
    without = cfg.call(["run", "--tasks", "E1,E10", "--candidates", CODER.label])[1]
    assert "note:" not in without  # a plain text task needs no warning


def test_the_plan_says_when_the_budget_could_not_be_checked():
    import dataclasses

    summary = evals.run_eval([TASKS["E1"]], [CODER], invoke=FakeInvoke(), workroot=pathlib.Path("w"), out_dir=pathlib.Path("o"))
    plan = evals._format_plan(summary, [TASKS["E1"]], [CODER], MODELS, budget_checked=False)
    assert "budget:     not checked" in plan and "--spend-quota" in plan
    refused = dataclasses.replace(summary, refusals=("nope",))
    shown = evals._format_plan(refused, [TASKS["E1"]], [CODER], MODELS, budget_checked=True)
    assert "refused:" in shown and "- nope" in shown and "--spend-quota" not in shown


def test_run_without_a_usable_ledger_still_plans_but_refuses_to_spend(cfg):
    blocker = cfg.tmp / "a-file"
    blocker.write_text("x", encoding="utf-8")
    bad_db = blocker / "ases.db"  # its parent is a file, so the database cannot be created
    fake = FakeInvoke()
    argv = ["run", "--tasks", "E1", "--candidates", CODER.label]
    code, out, _ = cfg.call(argv, invoke=fake, db_path=bad_db)
    assert code == 0 and "not checked (no request ledger was available)" in out  # a dry run needs no ledger
    code, out, err = cfg.call([*argv, "--spend-quota"], invoke=fake, db_path=bad_db)
    assert code == 1 and "cannot open the request ledger" in err and fake.calls == []  # a real run must record its usage


def test_recommend_breaks_ties_by_label_and_drops_the_small_sample_warning_with_enough_runs():
    tied = [rec(t, label, requests=2) for label in ("b/two", "a/one") for t in ("E1", "E2")]
    lead = next(line for line in evals.recommend(tied, None) if line.startswith("lead role"))
    assert "consider a/one" in lead and "Others: b/two 2 of 2." in lead
    assert next(line for line in evals.recommend(list(reversed(tied)), None) if line.startswith("lead role")) == lead
    many = [rec(t, "a/one", requests=1) for _ in range(2) for t in ("E4", "E5", "E6", "E9")]  # 8 runs for the coder role
    coder = next(line for line in evals.recommend(many, None) if line.startswith("coder role"))
    assert "8 of 8 runs succeeded" in coder and "only" not in coder
    mixed = [rec("E4", "a/one", success=False), rec("E5", "a/one", success=False), rec("E6", "a/one"), rec("E9", "a/one")]
    assert "2 of 4 runs succeeded" in next(l for l in evals.recommend(mixed, None) if l.startswith("coder role"))


def test_check_e5_structure_accepts_an_annotated_constant_and_positional_only_arguments():
    annotated = E5_GOOD.replace("SALES_TAX_RATE = 0.2", "SALES_TAX_RATE: float = 0.2")
    assert all(codetasks.check_e5_structure(annotated).values())
    posonly = E5_GOOD.replace("def line_total(order):", "def line_total(order, /):")
    assert all(codetasks.check_e5_structure(posonly).values())
    keyword_only = E5_GOOD.replace("def line_total(order):", "def line_total(*, order):")
    assert codetasks.check_e5_structure(keyword_only)["function_extracted"] is False  # not called with one positional
