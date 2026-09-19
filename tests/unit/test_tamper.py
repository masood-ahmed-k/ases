"""Tests for ases.tamper: the diff parser, every finding kind in both directions, the real-git range check
(including the exact sequence of test 22.12), and the reporting helpers. The look-alike cases matter as much as
the positive ones: a tamper check that cries wolf gets its allowances widened until it checks nothing."""
import random
import subprocess

import pytest

from ases import tamper
from ases.tamper import Finding

# Non-ASCII test data is built with chr() so that this file stays pure ASCII.
E_ACUTE, ARROW, CJK, EMOJI = chr(0xE9), chr(0x2192), chr(0x4E2D), chr(0x1F600)


# --- helpers ----------------------------------------------------------------------------------------------------

def file_diff(path, body, *, status="M", header=""):
    """A `git diff --no-renames` block for one file. `body` is the hunk's lines, each already prefixed with
    ' ', '-' or '+'; the hunk header counts are worked out from it so the parser sees a well-formed diff."""
    old = sum(1 for line in body if line[:1] in (" ", "-"))
    new = sum(1 for line in body if line[:1] in (" ", "+"))
    lines = [f"diff --git a/{path} b/{path}"]
    if status == "A":
        lines += ["new file mode 100644", "index 0000000..1111111", "--- /dev/null", f"+++ b/{path}"]
        span = f"-0,0 +1,{new}"
    elif status == "D":
        lines += ["deleted file mode 100644", "index 1111111..0000000", f"--- a/{path}", "+++ /dev/null"]
        span = f"-1,{old} +0,0"
    else:
        lines += ["index 1111111..2222222 100644", f"--- a/{path}", f"+++ b/{path}"]
        span = f"-1,{old} +1,{new}"
    lines.append(f"@@ {span} @@" + (f" {header}" if header else ""))
    lines += list(body)
    return "\n".join(lines) + "\n"


def kinds(findings):
    return [f.kind for f in findings]


def only(findings, kind):
    return [f for f in findings if f.kind == kind]


# --- parse_diff ---------------------------------------------------------------------------------------------------

def test_parse_modified_file_keeps_line_numbers_and_hunk_context():
    diff = file_diff("src/a.py", [" keep", "-old", "+new", "+extra", " tail"], header="def run():")

    (fd,) = tamper.parse_diff(diff)

    assert (fd.path, fd.old_path, fd.status, fd.binary) == ("src/a.py", "src/a.py", "M", False)
    assert fd.removed_lines == [(2, "old")]
    assert fd.added_lines == [(2, "new"), (3, "extra")]
    assert [h.header for h in fd.hunks] == ["def run():"]
    assert fd.hunks[0].context == [(1, "keep"), (4, "tail")]
    assert fd.hunks[0].added == fd.added_lines


def test_parse_added_file_is_status_a_with_numbered_lines():
    (fd,) = tamper.parse_diff(file_diff("new.txt", ["+one", "+two"], status="A"))

    assert (fd.path, fd.status) == ("new.txt", "A")
    assert fd.added_lines == [(1, "one"), (2, "two")]
    assert fd.removed_lines == []


def test_parse_deleted_file_is_status_d_and_keeps_the_old_path():
    (fd,) = tamper.parse_diff(file_diff("gone.py", ["-x", "-y"], status="D"))

    assert (fd.path, fd.old_path, fd.status) == ("gone.py", "gone.py", "D")
    assert fd.removed_lines == [(1, "x"), (2, "y")]


def test_parse_binary_files_have_an_entry_and_no_lines():
    diff = (
        "diff --git a/img.png b/img.png\nnew file mode 100644\nindex 0000000..abc\n"
        "Binary files /dev/null and b/img.png differ\n"
        "diff --git a/old.bin b/old.bin\ndeleted file mode 100644\nindex abc..0000000\n"
        "Binary files a/old.bin and /dev/null differ\n"
        "diff --git a/both.bin b/both.bin\nindex 1..2 100644\nBinary files a/both.bin and b/both.bin differ\n"
    )

    added, deleted, changed = tamper.parse_diff(diff)

    assert (added.path, added.status, added.binary) == ("img.png", "A", True)
    assert (deleted.path, deleted.status, deleted.binary) == ("old.bin", "D", True)
    assert (changed.path, changed.status, changed.binary) == ("both.bin", "M", True)
    assert not (added.added_lines or added.removed_lines or added.hunks)


def test_parse_path_with_spaces_uses_the_plus_line_not_the_diff_git_line():
    diff = (
        "diff --git a/my dir/my file.py b/my dir/my file.py\nindex 1..2 100644\n"
        "--- a/my dir/my file.py\t\n+++ b/my dir/my file.py\t\n@@ -1 +1 @@\n-a\n+b\n"
    )

    (fd,) = tamper.parse_diff(diff)

    assert fd.path == "my dir/my file.py"
    assert fd.added_lines == [(1, "b")]


def test_parse_path_with_spaces_and_no_hunks_falls_back_to_the_symmetric_split():
    diff = "diff --git a/dir b/x y.txt b/dir b/x y.txt\nnew file mode 100644\nindex 0000000..e69de29\n"

    (fd,) = tamper.parse_diff(diff)

    assert (fd.path, fd.status) == ("dir b/x y.txt", "A")


def test_parse_quoted_paths_are_unescaped():
    diff = (
        'diff --git "a/caf\\303\\251 \\"x\\".py" "b/caf\\303\\251 \\"x\\".py"\nnew file mode 100644\n'
        '--- /dev/null\n+++ "b/caf\\303\\251 \\"x\\".py"\n@@ -0,0 +1 @@\n+hi\n'
    )

    (fd,) = tamper.parse_diff(diff)

    assert fd.path == f'caf{E_ACUTE} "x".py'
    assert fd.status == "A"


def test_parse_diff_with_no_trailing_newline_and_the_no_newline_marker():
    diff = "diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1 +1 @@\n-x\n\\ No newline at end of file\n+y"

    (fd,) = tamper.parse_diff(diff)

    assert fd.removed_lines == [(1, "x")]
    assert fd.added_lines == [(1, "y")]


@pytest.mark.parametrize("text", ["", "\n", "   \n", None, 12, [], "not a diff at all\njust prose\n"])
def test_parse_empty_and_non_diff_input_gives_no_entries(text):
    assert tamper.parse_diff(text) == []


def test_parse_strips_ansi_colour():
    diff = (
        "\x1b[1mdiff --git a/a.py b/a.py\x1b[m\n\x1b[1m--- a/a.py\x1b[m\n\x1b[1m+++ b/a.py\x1b[m\n"
        "\x1b[36m@@ -1 +1 @@\x1b[m\n\x1b[31m-old\x1b[m\n\x1b[32m+new\x1b[m\n"
    )

    (fd,) = tamper.parse_diff(diff)

    assert fd.path == "a.py"
    assert fd.removed_lines == [(1, "old")]
    assert fd.added_lines == [(1, "new")]


def test_parse_reads_an_added_line_that_looks_like_a_header_as_content():
    # The added line's text is "++ b/.env", so the diff line reads "+++ b/.env". The hunk's counts say it is content.
    diff = file_diff("notes.txt", [" a", "+++ b/.env", " b"])

    (fd,) = tamper.parse_diff(diff)

    assert [fd.path for fd in tamper.parse_diff(diff)] == ["notes.txt"]
    assert fd.added_lines == [(2, "++ b/.env")]


def test_parse_several_files_in_one_diff():
    diff = file_diff("a.py", ["-x", "+y"]) + file_diff("b.py", ["+z"], status="A") + file_diff("c.py", ["-w"], status="D")

    assert [(f.path, f.status) for f in tamper.parse_diff(diff)] == [("a.py", "M"), ("b.py", "A"), ("c.py", "D")]


def test_parse_headers_without_diff_git_lines_start_a_new_file_at_each_pair():
    diff = "--- a/one.py\n+++ b/one.py\n@@ -1 +1 @@\n-a\n+b\n--- a/two.py\n+++ b/two.py\n@@ -1 +1 @@\n-c\n+d\n"

    assert [f.path for f in tamper.parse_diff(diff)] == ["one.py", "two.py"]


def test_parse_bare_plus_minus_snippet_is_one_entry_with_no_path():
    (fd,) = tamper.parse_diff("-    def test_x():\n+    def test_y():\n")

    assert fd.path == ""
    assert fd.removed_lines == [(None, "    def test_x():")]
    assert fd.added_lines == [(None, "    def test_y():")]


def test_parse_accepts_bytes_and_crlf():
    (fd,) = tamper.parse_diff(file_diff("a.py", ["+x"], status="A").replace("\n", "\r\n").encode("utf-8"))

    assert fd.added_lines == [(1, "x")]


# --- test_file_deleted ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "tests/test_a.py", "pkg/test_b.py", "pkg/b_test.py", "svc/user_test.go", "web/app.test.js", "web/app.test.ts",
    "web/App.test.tsx", "web/app.spec.js", "web/app.spec.ts", "src/main/UserTest.java", "spec/user_spec.rb",
    "tests/helpers.py", "test/support.js", "web/__tests__/render.js", "tests/conftest.py",
])
def test_deleting_a_test_file_is_flagged(path):
    findings = tamper.analyze_diff(file_diff(path, ["-x = 1"], status="D"))

    (hit,) = only(findings, "test_file_deleted")
    assert hit.path == path


@pytest.mark.parametrize("path", [
    "src/app.py", "README.md", "tests/fixtures/data.json", "spec/requirements.yaml", "docs/testing.md",
    "src/contest.py", "latest.py",
])
def test_deleting_a_file_that_only_looks_test_shaped_is_not_flagged(path):
    assert only(tamper.analyze_diff(file_diff(path, ["-x = 1"], status="D")), "test_file_deleted") == []


def test_adding_or_editing_a_test_file_is_not_a_deletion():
    diff = file_diff("tests/test_a.py", ["+def test_a():", "+    assert f() == 1"], status="A")
    assert only(tamper.analyze_diff(diff), "test_file_deleted") == []


# --- test_deleted --------------------------------------------------------------------------------------------

def test_removing_a_python_test_with_nothing_added_names_the_test():
    diff = file_diff("tests/test_a.py", ["-def test_login():", "-    assert login() == 1", " ", " def test_other():"])

    (hit,) = only(tamper.analyze_diff(diff), "test_deleted")

    assert "test_login" in hit.detail
    assert (hit.path, hit.line) == ("tests/test_a.py", 1)


def test_removing_an_async_python_test_is_flagged():
    diff = file_diff("tests/test_a.py", ["-async def test_fetch():", "-    await go()"])
    assert "test_fetch" in only(tamper.analyze_diff(diff), "test_deleted")[0].detail


def test_a_test_renamed_is_not_a_deleted_test():
    diff = file_diff("tests/test_a.py", ["-def test_old_name():", "+def test_new_name():", "     assert f() == 1"])
    assert only(tamper.analyze_diff(diff), "test_deleted") == []


def test_a_test_whose_signature_line_changed_is_not_a_deleted_test():
    diff = file_diff("tests/test_a.py", ["-def test_x(self):", "+def test_x(self, tmp_path):", "     assert f() == 1"])
    assert only(tamper.analyze_diff(diff), "test_deleted") == []


def test_each_added_definition_stands_in_for_one_removal_only():
    diff = file_diff("tests/test_a.py", [
        "-def test_one():", "-    pass", "-def test_two():", "-    pass", "-def test_three():", "-    pass",
        "+def test_brand_new():", "+    pass",
    ])

    hits = only(tamper.analyze_diff(diff), "test_deleted")

    assert len(hits) == 2


@pytest.mark.parametrize("path,removed,name", [
    ("web/app.test.js", ['-it("renders the header", () => {', "-  expect(x).toBe(1);", "-});"], "renders the header"),
    ("web/app.test.ts", ["-test('adds numbers', () => {", "-});"], "adds numbers"),
    ("web/app.spec.ts", ['-  it.each([1, 2])("handles %i", (n) => {', "-  });"], "handles %i"),
    ("svc/user_test.go", ["-func TestParse(t *testing.T) {", "-\tt.Error(\"x\")", "-}"], "TestParse"),
    ("svc/user_test.go", ["-func (s *Suite) TestLogin(t *testing.T) {", "-}"], "TestLogin"),
    ("src/main/UserTest.java", ["-    @Test", "-    public void testCreate() {", "-    }"], "testCreate"),
    ("src/main/UserTest.java", ["-    public void testDelete() {", "-    }"], "testDelete"),
    ("tests/it.rs", ["-#[test]", "-fn parses_input() {", "-}"], "parses_input"),
    ("spec/user_spec.rb", ['-  it "validates the email" do', "-  end"], "validates the email"),
])
def test_removed_test_definitions_in_other_languages_are_found(path, removed, name):
    hits = only(tamper.analyze_diff(file_diff(path, removed)), "test_deleted")

    assert len(hits) == 1, hits
    assert name in hits[0].detail


def test_a_removed_test_attribute_alone_is_flagged_because_the_test_stops_running():
    diff = file_diff("src/main/UserTest.java", ["-    @Test", "     public void testCreate() {", "     }"])

    (hit,) = only(tamper.analyze_diff(diff), "test_deleted")

    assert "attribute" in hit.detail


def test_inline_rust_unit_tests_are_found_in_a_source_file():
    diff = file_diff("src/lib.rs", ["-    #[test]", "-    fn adds() {", "-        assert_eq!(add(1, 2), 3);", "-    }"])
    assert len(only(tamper.analyze_diff(diff), "test_deleted")) == 1


def test_a_function_named_test_something_in_a_source_file_is_not_a_deleted_test():
    diff = file_diff("src/db.py", ["-    def test_connection(self):", "-        return self.ping()"])
    assert only(tamper.analyze_diff(diff), "test_deleted") == []


def test_removing_a_non_test_function_from_a_test_file_is_not_a_deleted_test():
    diff = file_diff("tests/test_a.py", ["-def helper():", "-    return 1"])
    assert only(tamper.analyze_diff(diff), "test_deleted") == []


def test_test_deleted_is_not_exempted_by_allow_paths():
    diff = file_diff("tests/test_a.py", ["-def test_login():", "-    pass"])
    assert len(only(tamper.analyze_diff(diff, allow_paths=["tests/*"]), "test_deleted")) == 1


# --- skip_marker ---------------------------------------------------------------------------------------------

SKIP_POSITIVES = [
    ('@pytest.mark.skip(reason="flaky")', "pytest.mark.skip"),
    ("    pytest.skip('not now')", "pytest.skip("),
    ("@pytest.mark.xfail(strict=False)", "pytest.mark.xfail"),
    ('@unittest.skip("later")', "unittest.skip"),
    ("@skip", "@skip"),
    ('@pytest.mark.skipif(sys.platform == "win32", reason="x")', "pytest.mark.skip"),
    ("@unittest.skipUnless(HAS_X, 'x')", "unittest.skip"),
    ("it.skip('does x', () => {})", "it.skip("),
    ("test.skip('does x', () => {})", "test.skip("),
    ("describe.skip('suite', () => {})", "describe.skip("),
    ("xit('does x', () => {})", "xit("),
    ("xdescribe('suite', () => {})", "xdescribe("),
    ("xtest('does x', () => {})", "xtest("),
    ("    t.Skip(\"needs network\")", "t.Skip("),
    ("#[ignore]", "#[ignore]"),
    ("    @Ignore", "@Ignore"),
    ("    @Disabled(\"broken\")", "@Disabled"),
    ("it.only('does x', () => {})", ".only("),
    ("describe.only('suite', () => {})", ".only("),
    ("fit('does x', () => {})", "fit("),
    ("fdescribe('suite', () => {})", "fdescribe("),
    ("pytest -q --deselect tests/test_a.py::test_x", "--deselect"),
    ('pytest -q -k "not slow"', '-k "not'),
    ("@PYTEST.MARK.SKIP", "pytest.mark.skip"),
    ("run_tests()  # noqa: test", "# noqa: test"),
    ('@mark.xfail(reason="later")', "xfail("),                    # imported mark, so no pytest.mark prefix
    ('@mark.skipif(sys.platform == "win32")', "skipif("),
    ('decorator = skipUnless(HAS_X, "x")', "skipUnless"),
    ("    pytest.xfail('known bug')", "xfail("),
]


@pytest.mark.parametrize("line,marker", SKIP_POSITIVES)
def test_skip_markers_added_are_flagged(line, marker):
    hits = only(tamper.analyze_diff(file_diff("tests/test_a.py", ["+" + line])), "skip_marker")

    assert len(hits) == 1, hits
    assert marker in hits[0].detail
    assert hits[0].line == 1


@pytest.mark.parametrize("line", [
    "# skip this step if the cache is warm",
    "@pytest.mark.parametrize('x', [1, 2])",
    "model.fit(X_train, y_train)",
    "curve_fit(func, x, y)",
    "rows = Model.objects.only('id')",
    "benefit(total)",
    "def skipped_count(items): return 0",
    "x = 'kit(' + suffix",
    "from unittest import mock",
    "importorskip_helper = 1",
    "-k = 3",
])
def test_lookalikes_are_not_skip_markers(line):
    assert only(tamper.analyze_diff(file_diff("tests/test_a.py", ["+" + line])), "skip_marker") == []


def test_a_skip_marker_removed_is_not_a_finding():
    diff = file_diff("tests/test_a.py", ["-@pytest.mark.skip", " def test_x():"])
    assert only(tamper.analyze_diff(diff), "skip_marker") == []


def test_a_skip_marker_in_prose_documentation_is_not_a_finding():
    diff = file_diff("docs/testing.md", ["+Use `@pytest.mark.skip` sparingly and never `|| true`."])
    assert only(tamper.analyze_diff(diff), "skip_marker") == []
    assert only(tamper.analyze_diff(diff), "unconditional_pass") == []


def test_skip_marker_is_not_exempted_by_allow_paths():
    diff = file_diff("tests/test_a.py", ["+@pytest.mark.skip"])
    assert len(only(tamper.analyze_diff(diff, allow_paths=["tests/*", "tests/test_a.py"]), "skip_marker")) == 1


# --- unconditional_pass --------------------------------------------------------------------------------------

UNCONDITIONAL_POSITIVES = [
    ("run_tests.sh", "pytest -q || true", "|| true"),
    ("run_tests.sh", "pytest -q || TRUE", "|| true"),
    ("Makefile", "\tpytest || :", "|| :"),
    ("run_tests.sh", "pytest -q; true", "; true"),
    ("run_tests.sh", "set +e", "set +e"),
    ("run_tests.sh", "set +euo pipefail", "set +e"),
    ("run_tests.sh", "pytest -q; exit 0", "exit 0"),
    ("run_tests.sh", "exit 0", "exit 0"),
    ("ci.bat", "exit /b 0", "exit 0"),
    ("package.json", '    "test": "jest --passWithNoTests",', "--passWithNoTests"),
    (".github/workflows/ci.yml", "        continue-on-error: true", "continue-on-error: true"),
    (".gitlab-ci.yml", "  allow_failure: true", "allow_failure: true"),
    (".github/workflows/ci.yml", "    if: false", "if: false"),
    ("tests/test_a.py", "    assert True", "assert True"),
    ("tests/test_a.py", "    assert 1", "assert True"),
    ("tests/test_a.py", "    assert not False", "assert True"),
    ("tests/test_a.py", "        self.assertTrue(True)", "assertTrue(True)"),
    ("web/a.test.js", "  expect(true).toBe(true);", "expect(true).toBe(true)"),
    ("tests/test_a.py", "    assert 1 == 1", "assert x == x"),
    ("tests/test_a.py", "    pass  # test", "pass  # test"),
    ("run_tests.sh", "pytest || exit 0", "|| exit 0"),
    ("tests/test_a.py", 'subprocess.run("pytest || true", shell=True)', "|| true"),
    (".github/workflows/ci.yml", "    if: ${{ false }}", "if: false"),
    ("web/a.test.js", "  assert(true);", "assert(true)"),
]


@pytest.mark.parametrize("path,line,marker", UNCONDITIONAL_POSITIVES)
def test_unconditional_passes_added_are_flagged(path, line, marker):
    hits = only(tamper.analyze_diff(file_diff(path, ["+" + line])), "unconditional_pass")

    assert len(hits) == 1, hits
    assert marker in hits[0].detail
    assert (hits[0].path, hits[0].line) == (path, 1)


def test_every_marker_row_is_reachable_so_no_row_is_silently_switched_off():
    """Each row has a substring key that gates its regex. A wrong key would make its row match nothing and no
    other test would notice, so demand that the positive cases above reach every label of both tables."""
    skip_hit = {tamper._first_marker(line, tamper._SKIP_TABLE, False) for line, _ in SKIP_POSITIVES}
    uncond_hit = {
        tamper._first_marker(line, tamper._UNCOND_TABLE, tamper._is_script_or_ci(path))
        for path, line, _ in UNCONDITIONAL_POSITIVES
    }

    assert {row[0] for row in tamper._SKIP_TABLE} <= skip_hit
    assert {row[0] for row in tamper._UNCOND_TABLE} <= uncond_hit
    # a key is compared with the LOWERCASED line, so an upper-case or empty key could never (or always) hit
    for table in (tamper._SKIP_TABLE, tamper._UNCOND_TABLE):
        assert all(key and key == key.lower() for _, key, _, _ in table)


@pytest.mark.parametrize("path,line", [
    ("tests/test_a.py", "    assert x == 1"),
    ("tests/test_a.py", "    assert result"),
    ("tests/test_a.py", "    assert 1 == 10"),
    ("tests/test_a.py", "    assert value == other"),
    ("tests/test_a.py", "    pass"),
    ("web/a.test.js", "  expect(1).toBe(2);"),
    ("run_tests.sh", "echo true"),
    ("run_tests.sh", "set -e"),
    ("run_tests.sh", "set -euo pipefail"),
    ("run_tests.sh", 'echo "exit 0 is success"'),
    ("run_tests.sh", "true_value=1"),
    ("run_tests.sh", "pytest -q --maxfail=1"),
    ("src/app.py", "    sys.exit(0)"),
    ("src/app.py", "exit 0"),
    ("src/app.py", "value = compute(); true"),
    (".github/workflows/ci.yml", "    if: github.ref == 'refs/heads/main'"),
    (".github/workflows/ci.yml", "        continue-on-error: false"),
])
def test_lookalikes_are_not_unconditional_passes(path, line):
    assert only(tamper.analyze_diff(file_diff(path, ["+" + line])), "unconditional_pass") == []


def test_an_unconditional_pass_removed_is_not_a_finding():
    assert only(tamper.analyze_diff(file_diff("run_tests.sh", ["-pytest -q || true", "+pytest -q"])), "unconditional_pass") == []


def test_unconditional_pass_is_not_exempted_by_allow_paths():
    diff = file_diff("run_tests.sh", ["+pytest -q || true"])
    assert len(only(tamper.analyze_diff(diff, allow_paths=["run_tests.sh", "*"]), "unconditional_pass")) == 1


def test_a_line_is_reported_once_under_the_most_specific_kind():
    # `assert f() == 1` became `assert True`: that is an unconditional pass, not also a "weakened assertion".
    diff = file_diff("tests/test_a.py", ["-    assert f() == 1", "+    assert True"])

    assert kinds(tamper.analyze_diff(diff)) == ["unconditional_pass"]


# --- assertion_weakened --------------------------------------------------------------------------------------

def test_fewer_assertions_added_than_removed_reports_the_counts():
    diff = file_diff("tests/test_a.py", [
        "-    assert a() == 1", "-    assert b() == 2", "+    assert a() == 1", " ", " def test_next():",
    ], header="def test_a():")

    (hit,) = only(tamper.analyze_diff(diff), "assertion_weakened")

    assert "2 assertion line(s) removed" in hit.detail and "only 1 added" in hit.detail
    assert hit.line == 1


@pytest.mark.parametrize("old,new", [
    ("    assert result == 5", "    assert result is not None"),
    ("    assert result == 5", "    assert result is not False"),
    ("    assert count == 3", "    assert count >= 0"),
    ("    assert value == 'x'", "    assert value != None"),
    ("    assert total == 10", "    assert total"),
    ("    self.assertEqual(a, 1)", "    self.assertIsNotNone(a)"),
    ("    expect(x).toBe(5);", "    expect(x).toBeDefined();"),
    ("    expect(x).toEqual(5);", "    expect(x).toBeTruthy();"),
])
def test_an_equality_assertion_replaced_by_a_weaker_one_is_flagged_with_both_lines(old, new):
    (hit,) = only(tamper.analyze_diff(file_diff("tests/test_a.py", ["-" + old, "+" + new])), "assertion_weakened")

    assert old.strip()[:20] in hit.detail
    assert new.strip()[:20] in hit.detail
    assert hit.line == 1


def test_long_assertion_lines_are_truncated_in_the_finding():
    long_old = "    assert compute_something_expensive(argument_one, argument_two) == expected_value_here_" + "x" * 80
    (hit,) = only(tamper.analyze_diff(file_diff("tests/test_a.py", ["-" + long_old, "+    assert y is not None"])),
                  "assertion_weakened")

    assert len(hit.detail) < 200


@pytest.mark.parametrize("body", [
    ["-    assert a == 1", "+    assert a == 2"],                                  # still an equality
    ["-    assert a == 1", "+    assert a == 1", "+    assert b == 2"],            # more added than removed
    ["+    assert a == 1"],                                                         # only additions
    ["-    # assert a == 1"],                                                        # a comment was removed
    ["-    x = compute()", "+    y = compute()"],                                  # no assertions at all
    ["-    assert a == 1", "+    assert a == 1 and b is not None"],                 # not weaker
])
def test_changes_that_do_not_weaken_assertions_are_not_flagged(body):
    assert only(tamper.analyze_diff(file_diff("tests/test_a.py", body)), "assertion_weakened") == []


def test_commenting_an_assertion_out_counts_as_removing_it():
    diff = file_diff("tests/test_a.py", ["-    assert f() == 1", "+    # assert f() == 1"])
    assert len(only(tamper.analyze_diff(diff), "assertion_weakened")) == 1


def test_assertions_are_counted_per_hunk_not_across_the_file():
    diff = (
        "diff --git a/tests/test_a.py b/tests/test_a.py\n--- a/tests/test_a.py\n+++ b/tests/test_a.py\n"
        "@@ -1,2 +1,1 @@ def test_one():\n-    assert f() == 1\n     x = 1\n"
        "@@ -10,1 +9,2 @@ def test_two():\n     y = 2\n+    assert g() == 2\n"
    )

    (hit,) = only(tamper.analyze_diff(diff), "assertion_weakened")

    assert hit.line == 1
    assert "test_one" in hit.detail


def test_a_path_matched_by_allow_paths_is_exempt_from_assertion_weakened_but_not_from_the_rest():
    body = ["-    assert f() == 1", "+    assert f() is not None", "+    @pytest.mark.skip"]
    diff = file_diff("tests/test_a.py", body)

    assert len(only(tamper.analyze_diff(diff), "assertion_weakened")) == 1
    exempt = tamper.analyze_diff(diff, allow_paths=["tests/test_a.py"])
    assert only(exempt, "assertion_weakened") == []
    assert len(only(exempt, "skip_marker")) == 1


def test_a_source_file_the_task_did_not_need_to_touch_is_checked_too():
    diff = file_diff("src/util.py", ["-    assert x > 0", "     return x"])

    assert len(only(tamper.analyze_diff(diff), "assertion_weakened")) == 1
    assert only(tamper.analyze_diff(diff, allow_paths=["src/*"]), "assertion_weakened") == []


def test_assertion_weakened_needs_a_modified_file():
    added = file_diff("tests/test_a.py", ["+    assert a == 1"], status="A")
    deleted = file_diff("tests/test_a.py", ["-    assert a == 1"], status="D")

    assert only(tamper.analyze_diff(added), "assertion_weakened") == []
    assert only(tamper.analyze_diff(deleted), "assertion_weakened") == []
    assert kinds(tamper.analyze_diff(deleted)) == ["test_file_deleted"]


# --- gate_config_changed -------------------------------------------------------------------------------------

CHANGE = ["-a = 1", "+a = 2"]


@pytest.mark.parametrize("path", [
    ".github/workflows/ci.yml", ".github/workflows/sub/deploy.yaml", ".gitlab-ci.yml", "azure-pipelines.yml",
    "Jenkinsfile", ".circleci/config.yml", "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", "conftest.py",
    "tests/unit/conftest.py", ".coveragerc", "noxfile.py", "Makefile", "jest.config.js", "vitest.config.ts",
    "karma.conf.js", ".pre-commit-config.yaml", "packages/api/pyproject.toml",
])
def test_changing_gate_ci_or_runner_configuration_is_flagged(path):
    (hit,) = only(tamper.analyze_diff(file_diff(path, CHANGE)), "gate_config_changed")

    assert hit.path == path
    assert "ASES-QG-02" in hit.detail


@pytest.mark.parametrize("status", ["A", "D"])
def test_adding_or_deleting_a_config_file_is_a_change_too(status):
    body = ["+[pytest]"] if status == "A" else ["-[pytest]"]
    assert len(only(tamper.analyze_diff(file_diff("pytest.ini", body, status=status)), "gate_config_changed")) == 1


@pytest.mark.parametrize("path", [
    "go.mod", "README.md", "src/config.py", "requirements.txt", "Cargo.lock", "package-lock.json", "docs/tox.md",
    "src/conftest_helpers.py", "tests/test_pytest_ini.py", "src/makefile_parser.py",
])
def test_ordinary_files_are_not_gate_configuration(path):
    assert only(tamper.analyze_diff(file_diff(path, CHANGE)), "gate_config_changed") == []


def test_a_config_path_the_task_allows_is_not_flagged():
    diff = file_diff("pyproject.toml", CHANGE)

    assert only(tamper.analyze_diff(diff, allow_paths=["pyproject.toml"]), "gate_config_changed") == []
    assert only(tamper.analyze_diff(diff, allow_paths=["*.toml"]), "gate_config_changed") == []
    assert len(only(tamper.analyze_diff(diff, allow_paths=["src/**"]), "gate_config_changed")) == 1
    assert len(only(tamper.analyze_diff(diff, allow_paths=[]), "gate_config_changed")) == 1


def test_allowing_one_config_file_does_not_allow_another():
    diff = file_diff("pyproject.toml", CHANGE) + file_diff("tox.ini", CHANGE)

    hits = only(tamper.analyze_diff(diff, allow_paths=["tox.ini"]), "gate_config_changed")

    assert [h.path for h in hits] == ["pyproject.toml"]


def test_package_json_counts_only_when_a_test_related_key_is_touched():
    scripts = file_diff("package.json", ['     "scripts": {', '-    "test": "jest",', '+    "test": "jest --silent",', "     }"])
    added_script = file_diff("package.json", ['   "scripts": {', '+    "lint": "eslint ."', "   },"])
    jest_block = file_diff("package.json", ['   "jest": {', '-    "bail": 1', '+    "bail": 0', "   }"])
    dependency = file_diff("package.json", ['   "dependencies": {', '-    "left-pad": "1.0.0",', '+    "left-pad": "1.1.0",', "   }"])

    assert len(only(tamper.analyze_diff(scripts), "gate_config_changed")) == 1
    assert len(only(tamper.analyze_diff(added_script), "gate_config_changed")) == 1
    assert len(only(tamper.analyze_diff(jest_block), "gate_config_changed")) == 1
    assert only(tamper.analyze_diff(dependency), "gate_config_changed") == []


def test_cargo_toml_counts_only_when_a_profile_or_lints_section_is_touched():
    profile = file_diff("Cargo.toml", [" [profile.release]", "-opt-level = 3", "+opt-level = 0"])
    lints = file_diff("Cargo.toml", [" [lints.rust]", '-unsafe_code = "forbid"', '+unsafe_code = "allow"'])
    dependency = file_diff("Cargo.toml", [" [dependencies]", '-serde = "1.0.1"', '+serde = "1.0.2"'])

    assert len(only(tamper.analyze_diff(profile), "gate_config_changed")) == 1
    assert len(only(tamper.analyze_diff(lints), "gate_config_changed")) == 1
    assert only(tamper.analyze_diff(dependency), "gate_config_changed") == []


def test_paths_named_by_the_gate_profile_commands_count_as_gate_configuration():
    diff = file_diff("scripts/check.sh", CHANGE)

    assert only(tamper.analyze_diff(diff), "gate_config_changed") == []
    assert len(only(tamper.analyze_diff(diff, gate_config_paths=["scripts/check.sh"]), "gate_config_changed")) == 1
    assert len(only(tamper.analyze_diff(diff, gate_config_paths=["scripts/*.sh"]), "gate_config_changed")) == 1
    assert only(tamper.analyze_diff(diff, gate_config_paths=["scripts/other.sh"]), "gate_config_changed") == []
    assert only(tamper.analyze_diff(diff, gate_config_paths=["scripts/check.sh"], allow_paths=["scripts/check.sh"]),
                "gate_config_changed") == []


def test_allow_globs_match_exactly_like_the_touches_check():
    from ases import integrity

    paths = ["pyproject.toml", "packages/api/pyproject.toml", "tests/unit/conftest.py", ".github/workflows/ci.yml"]
    globs = ["*", "**", "*.toml", "packages/*", "packages/**", "tests/*", "tests/**", ".github/*", ".github/**/*.yml",
             "pyproject.toml", "conftest.py", "src/*"]
    for path in paths:
        for glob in globs:
            hits = only(tamper.analyze_diff(file_diff(path, CHANGE), allow_paths=[glob]), "gate_config_changed")
            in_scope = integrity.paths_outside_touches([path], [glob]) == []
            assert (hits == []) == in_scope, (path, glob)


def test_allow_paths_accepts_a_bare_string_none_and_windows_style_globs():
    diff = file_diff("pyproject.toml", CHANGE)

    assert only(tamper.analyze_diff(diff, allow_paths="pyproject.toml"), "gate_config_changed") == []
    assert only(tamper.analyze_diff(diff, allow_paths="./pyproject.toml"), "gate_config_changed") == []
    assert only(tamper.analyze_diff(diff, allow_paths=iter([".\\pyproject.toml"])), "gate_config_changed") == []
    assert len(only(tamper.analyze_diff(diff, allow_paths=None), "gate_config_changed")) == 1
    assert len(only(tamper.analyze_diff(diff, allow_paths=[None, 3, ""]), "gate_config_changed")) == 1
    assert len(only(tamper.analyze_diff(diff, allow_paths=7), "gate_config_changed")) == 1


# --- generated_artifact --------------------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "src/__pycache__/a.cpython-311.pyc", ".pytest_cache/v/cache/lastfailed", ".mypy_cache/3.11/x.json",
    ".ruff_cache/0.1/x", "node_modules/left-pad/index.js", "dist/bundle.js", "build/lib/a.py", ".venv/bin/python",
    "venv/pyvenv.cfg", "htmlcov/index.html", "target/debug/app", "ases.egg-info/PKG-INFO", "a.pyc", "b.pyo",
    "run.log", "logs/app.log", ".DS_Store", "coverage.xml", ".coverage", "data.sqlite", "data.sqlite3", "local.db",
    ".env", ".env.local", ".env.production", "certs/server.pem", "secrets/api.key", "id_rsa", "id_rsa.pub",
    "keys/id_ed25519", "cert.p12", "cert.pfx",
])
def test_adding_a_generated_artifact_or_secret_file_is_flagged(path):
    (hit,) = only(tamper.analyze_diff(file_diff(path, ["+x"], status="A")), "generated_artifact")

    assert hit.path == path
    assert "ASES-GIT-07" in hit.detail


@pytest.mark.parametrize("path", [
    ".env.example", ".env.sample", "src/envelope.py", "src/builder/a.py", "distribution/a.py", "src/keyboard.py",
    "monkey.py", "src/db.py", "README.md", "docs/build-notes.md", "src/logging.py", "tests/fixtures/a.json",
])
def test_lookalikes_are_not_generated_artifacts(path):
    assert only(tamper.analyze_diff(file_diff(path, ["+x"], status="A")), "generated_artifact") == []


def test_only_added_files_are_generated_artifacts():
    assert only(tamper.analyze_diff(file_diff("dist/bundle.js", CHANGE)), "generated_artifact") == []
    assert only(tamper.analyze_diff(file_diff("dist/bundle.js", ["-x"], status="D")), "generated_artifact") == []


def test_an_allow_glob_must_name_the_artifact_to_exempt_it():
    cache = file_diff("src/pkg/__pycache__/a.pyc", ["+x"], status="A")
    dist = file_diff("dist/bundle.js", ["+x"], status="A")
    db = file_diff("fixtures/local.db", ["+x"], status="A")
    env = file_diff(".env", ["+X=1"], status="A")

    assert only(tamper.analyze_diff(dist, allow_paths=["dist/**"]), "generated_artifact") == []
    assert only(tamper.analyze_diff(dist, allow_paths=["dist/bundle.js"]), "generated_artifact") == []
    assert only(tamper.analyze_diff(db, allow_paths=["*.db"]), "generated_artifact") == []
    assert only(tamper.analyze_diff(db, allow_paths=["fixtures/local.db"]), "generated_artifact") == []
    # covering is not naming: these globs match the path but say nothing about it being an artifact
    assert len(only(tamper.analyze_diff(cache, allow_paths=["src/**"]), "generated_artifact")) == 1
    assert len(only(tamper.analyze_diff(dist, allow_paths=["**"]), "generated_artifact")) == 1
    assert len(only(tamper.analyze_diff(env, allow_paths=["*"]), "generated_artifact")) == 1
    assert len(only(tamper.analyze_diff(db, allow_paths=["fixtures/*"]), "generated_artifact")) == 1


# --- secret_added --------------------------------------------------------------------------------------------

PLANTED = "sk-or-v1-PLANTEDVALUE0123456789abcd"


def test_a_planted_secret_is_found_and_never_appears_in_any_finding_text():
    diff = file_diff("config.py", [f"+API_KEY = '{PLANTED}'"], status="A")

    findings = tamper.analyze_diff(diff)

    (hit,) = only(findings, "secret_added")
    assert (hit.path, hit.line) == ("config.py", 1)
    assert "secret-shaped value" in hit.detail
    for text in (hit.detail, hit.path, repr(hit), tamper.format_finding(hit), tamper.format_findings(findings)):
        assert PLANTED not in text
        assert "PLANTEDVALUE" not in text


@pytest.mark.parametrize("secret", [
    "ghp_" + "abcdefghij1234567890",
    "glpat-" + "abcdefghij1234567890",
    "xoxb-" + "1234567890abcdef",
    "-----BEGIN RSA PRIVATE KEY-----",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "-----BEGIN PRIVATE KEY-----",
    "AKIA" + "IOSFODNN7EXAMPLE",
    "github_pat_" + "11ABCDEFG0abcdefghij_klmnopqrstuv",
    "sk_live_" + "abcdefghij1234567890",
    "AIza" + "SyA1234567890abcdefghijklmnopqrstuv",
])
def test_each_secret_shape_is_found_without_echoing_it(secret):
    findings = tamper.analyze_diff(file_diff("notes.txt", [f"+value = {secret}"], status="A"))

    (hit,) = only(findings, "secret_added")
    assert secret not in tamper.format_findings(findings)
    assert secret not in hit.detail


@pytest.mark.parametrize("line", [
    "task-manager-configuration-value-long", "risk-assessment-framework-document", "desk-lamp-assembly-instructions",
    "key = os.environ['OPENROUTER_API_KEY']", "AKIA is a prefix", "-----BEGIN CERTIFICATE-----",
])
def test_lookalikes_are_not_secrets(line):
    assert only(tamper.analyze_diff(file_diff("a.py", ["+" + line])), "secret_added") == []


def test_a_secret_on_a_removed_line_is_not_a_finding():
    assert only(tamper.analyze_diff(file_diff("a.py", [f"-key = '{PLANTED}'", "+key = None"])), "secret_added") == []


def test_a_plus_plus_plus_header_is_not_an_added_line():
    diff = file_diff(f"{PLANTED}.txt", ["+harmless"], status="A")
    assert only(tamper.analyze_diff(diff), "secret_added") == []


def test_secrets_are_flagged_in_documentation_too_and_no_allow_glob_exempts_them():
    diff = file_diff("docs/setup.md", [f"+export KEY={PLANTED}"])
    assert len(only(tamper.analyze_diff(diff, allow_paths=["docs/*", "*"]), "secret_added")) == 1


# --- analyze_diff never raises -------------------------------------------------------------------------------

_FRAGMENTS = [
    "diff --git a/x b/x", "diff --git ", 'diff --git "a/x', "--- a/x", "+++ b/x", "--- /dev/null", "+++ /dev/null",
    "@@ -1,2 +1,2 @@", "@@ -1 +1 @@ ctx", "@@", "@@@ -1 -1 +1 @@@", "@@ -a,b +c,d @@", "new file mode 100644",
    "deleted file mode 100644", "Binary files a/x and b/x differ", "Binary files", "GIT binary patch", "+", "-", " ",
    "\\ No newline at end of file", "+    assert True", "-def test_x():", "+@pytest.mark.skip", "\x00\x01\x02",
    E_ACUTE + CJK + EMOJI, '"a/\\303', "+" + "x" * 5000, "index 1..2", "rename from a", "rename to b",
    "similarity index 90%", "\r", "\t", "", "+++ b/.env", "--- a/tests/test_a.py", "+sk-or-v1-abcdefghijklmnop",
]


def test_analyze_diff_never_raises_on_malformed_input():
    for seed in range(300):
        rng = random.Random(seed)
        text = "\n".join(rng.choice(_FRAGMENTS) for _ in range(rng.randint(0, 30)))
        findings = tamper.analyze_diff(text, allow_paths=["tests/*"], gate_config_paths=["x"])
        assert isinstance(findings, list) and all(isinstance(f, Finding) for f in findings)
        assert all(isinstance(fd, tamper.FileDiff) for fd in tamper.parse_diff(text))
        assert isinstance(tamper.format_findings(findings), str)


@pytest.mark.parametrize("value", [None, 5, [], {}, b"", b"\xff\xfe binary \x00", "\ud800 lone surrogate", object()])
def test_analyze_diff_tolerates_non_text_input(value):
    assert tamper.analyze_diff(value) == []


# --- check_range on real git repositories --------------------------------------------------------------------

def git(repo, *args, check=True):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8")
    if check:
        assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def write(repo, rel, text, *, binary=False):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if binary:
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8", newline="\n")


def commit(repo, message="change"):
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


TEST_FILE = (
    "import pytest\nfrom calc import add\n\n\n"
    "def test_add():\n    assert add(1, 2) == 3\n\n\n"
    "def test_add_negative():\n    assert add(-1, -2) == -4\n"
)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "integration")
    git(r, "config", "user.email", "t@t")
    git(r, "config", "user.name", "t")
    git(r, "config", "core.autocrlf", "false")
    write(r, "calc.py", "def add(a, b):\n    return a + b\n")
    write(r, "tests/test_calc.py", TEST_FILE)  # test_add_negative is the failing test: -1 + -2 is not -4
    write(r, "run_tests.sh", "#!/bin/sh\npytest -q\n")
    write(r, "pytest.ini", "[pytest]\ntestpaths = tests\n")
    commit(r, "init")
    return r


def attempt(repo, name, edit):
    """A worker's attempt on its own branch off integration: apply `edit(repo)`, commit, return the branch name."""
    git(repo, "checkout", "-q", "-b", name, "integration")
    edit(repo)
    commit(repo, name)
    git(repo, "checkout", "-q", "integration")
    return name


def delete_failing_test(r):
    write(r, "tests/test_calc.py", TEST_FILE.split("\n\n\ndef test_add_negative")[0] + "\n")


def add_skip_marker(r):
    write(r, "tests/test_calc.py", TEST_FILE.replace("def test_add_negative", "@pytest.mark.skip\ndef test_add_negative"))


def append_or_true(r):
    write(r, "run_tests.sh", "#!/bin/sh\npytest -q || true\n")


def edit_the_runner_config(r):
    write(r, "pytest.ini", "[pytest]\ntestpaths = tests/passing\n")


def test_22_12_deleting_the_failing_test_fails_gate_1_with_test_deleted(repo):
    branch = attempt(repo, "a1", delete_failing_test)

    findings = tamper.check_range(repo, "integration", branch)

    (hit,) = only(findings, "test_deleted")
    assert "test_add_negative" in hit.detail
    assert hit.path == "tests/test_calc.py"
    assert tamper.blocking(findings)


def test_22_12_deleting_the_whole_test_file_fails_with_test_file_deleted(repo):
    branch = attempt(repo, "a1b", lambda r: (r / "tests" / "test_calc.py").unlink())

    (hit,) = only(tamper.check_range(repo, "integration", branch), "test_file_deleted")

    assert hit.path == "tests/test_calc.py"


def test_22_12_adding_a_skip_marker_fails_gate_1_with_skip_marker(repo):
    branch = attempt(repo, "a2", add_skip_marker)

    findings = tamper.check_range(repo, "integration", branch)

    (hit,) = findings
    assert (hit.kind, hit.path, hit.line) == ("skip_marker", "tests/test_calc.py", 9)
    assert "pytest.mark.skip" in hit.detail


def test_22_12_appending_or_true_to_the_test_command_fails_with_unconditional_pass(repo):
    branch = attempt(repo, "a3", append_or_true)

    (hit,) = tamper.check_range(repo, "integration", branch)

    assert (hit.kind, hit.path, hit.line) == ("unconditional_pass", "run_tests.sh", 2)
    assert "|| true" in hit.detail


def test_22_12_editing_the_runner_config_fails_with_gate_config_changed(repo):
    branch = attempt(repo, "a4", edit_the_runner_config)

    (hit,) = tamper.check_range(repo, "integration", branch)

    assert (hit.kind, hit.path) == ("gate_config_changed", "pytest.ini")


def test_22_12_the_whole_sequence_on_one_branch_reports_each_attempt(repo):
    git(repo, "checkout", "-q", "-b", "swarm/T1", "integration")
    delete_failing_test(repo)
    commit(repo, "delete the failing test")
    write(repo, "tests/test_calc.py", (repo / "tests" / "test_calc.py").read_text(encoding="utf-8").replace(
        "def test_add():", "@pytest.mark.skip\ndef test_add():"))
    commit(repo, "skip the other one")
    append_or_true(repo)
    commit(repo, "or true")
    edit_the_runner_config(repo)
    commit(repo, "edit the config")

    findings = tamper.check_range(repo, "integration", "swarm/T1")

    assert {"test_deleted", "skip_marker", "unconditional_pass", "gate_config_changed"} <= set(kinds(findings))
    assert {f.path for f in findings} == {"tests/test_calc.py", "run_tests.sh", "pytest.ini"}


def test_a_task_allowed_to_change_its_config_only_gets_the_other_findings(repo):
    git(repo, "checkout", "-q", "-b", "swarm/T1", "integration")
    edit_the_runner_config(repo)
    append_or_true(repo)
    commit(repo)

    findings = tamper.check_range(repo, "integration", "swarm/T1", allow_paths=["pytest.ini"])

    assert kinds(findings) == ["unconditional_pass"]


def test_a_clean_diff_returns_no_findings(repo):
    def honest_work(r):
        write(r, "calc.py", "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n")
        write(r, "tests/test_sub.py", "from calc import sub\n\n\ndef test_sub():\n    assert sub(3, 1) == 2\n")

    branch = attempt(repo, "clean", honest_work)

    assert tamper.check_range(repo, "integration", branch) == []
    assert tamper.check_range(repo, "integration", "integration") == []


def test_the_range_is_taken_from_the_merge_base_so_a_moved_integration_branch_is_not_blamed(repo):
    branch = attempt(repo, "work", lambda r: write(r, "calc.py", "def add(a, b):\n    return a + b  # ok\n"))
    (repo / "tests" / "test_calc.py").unlink()  # someone else's commit on integration, after the branch was cut
    commit(repo, "integration moves on")

    assert tamper.check_range(repo, "integration", branch) == []
    merge_base = git(repo, "merge-base", "integration", branch)
    assert tamper.check_range(repo, merge_base, branch) == []
    # the two-dot range WOULD blame the branch for it, which is the mistake three dots avoids
    assert "test_calc.py" in git(repo, "diff", "--name-only", f"integration..{branch}")


def test_a_rename_reads_as_a_deletion_plus_an_addition(repo):
    branch = attempt(repo, "mv", lambda r: git(r, "mv", "tests/test_calc.py", "tests/test_calc2.py"))

    findings = tamper.check_range(repo, "integration", branch)

    assert [f.path for f in only(findings, "test_file_deleted")] == ["tests/test_calc.py"]


def test_the_result_does_not_depend_on_the_users_git_diff_configuration(repo):
    for key, value in (("diff.noprefix", "true"), ("diff.mnemonicPrefix", "true"), ("color.ui", "always"),
                       ("core.quotepath", "true"), ("diff.renames", "true")):
        git(repo, "config", key, value)
    write(repo, f"tests/test_caf{E_ACUTE} x.py", "def test_c():\n    assert 1 == 2\n")
    commit(repo, "add a test with an odd name")
    branch = attempt(repo, "odd", lambda r: (r / "tests" / f"test_caf{E_ACUTE} x.py").unlink())

    findings = tamper.check_range(repo, "integration", branch)

    assert [(f.kind, f.path) for f in findings] == [("test_file_deleted", f"tests/test_caf{E_ACUTE} x.py")]


def test_added_files_with_spaces_no_content_or_binary_content_are_all_seen(repo):
    def add_files(r):
        write(r, "my dir/.env", "")  # empty: the diff has no hunk and no ---/+++ lines
        write(r, "assets/blob.bin", b"\x00\x01\x02\x03" * 10, binary=True)
        write(r, "dist/bundle.bin", b"\x00\xff" * 10, binary=True)
        write(r, "notes/a b.txt", "fine\n")

    branch = attempt(repo, "files", add_files)

    findings = tamper.check_range(repo, "integration", branch)

    assert sorted((f.kind, f.path) for f in findings) == [
        ("generated_artifact", "dist/bundle.bin"), ("generated_artifact", "my dir/.env"),
    ]


def test_a_planted_secret_in_a_commit_is_found_and_never_echoed(repo):
    branch = attempt(repo, "leak", lambda r: write(r, "config.py", f"# settings\nAPI_KEY = '{PLANTED}'\n"))

    findings = tamper.check_range(repo, "integration", branch)

    (hit,) = findings
    assert (hit.kind, hit.path, hit.line) == ("secret_added", "config.py", 2)
    assert PLANTED not in tamper.format_findings(findings)


def test_large_files_are_flagged_by_size_and_only_when_added_or_modified(repo):
    def add_big(r):
        write(r, "data/big.txt", "x" * 5000 + "\n")
        write(r, "calc.py", "def add(a, b):\n    return a + b\n" + "# padding\n" * 300)
        write(r, "data/small.txt", "small\n")

    branch = attempt(repo, "big", add_big)

    findings = tamper.check_range(repo, "integration", branch, max_file_bytes=1000)

    assert sorted((f.kind, f.path) for f in findings) == [("large_file", "calc.py"), ("large_file", "data/big.txt")]
    assert "5001" in only(findings, "large_file")[1].detail
    assert tamper.check_range(repo, "integration", branch) == []  # the default limit is 1,000,000 bytes
    assert tamper.check_range(repo, "integration", branch, max_file_bytes=None) == []


def test_a_bad_range_raises_instead_of_returning_a_clean_result(repo, tmp_path):
    with pytest.raises(tamper.TamperCheckError):
        tamper.check_range(repo, "integration", "no-such-branch")
    with pytest.raises(tamper.TamperCheckError):
        tamper.check_range(repo, "no-such-branch", "integration")
    with pytest.raises(tamper.TamperCheckError):
        tamper.check_range(repo, "integration", "HEAD~99")
    with pytest.raises(tamper.TamperCheckError):
        tamper.check_range(tmp_path / "not-a-repository", "integration", "HEAD")
    with pytest.raises(tamper.TamperCheckError):
        tamper.check_range(tmp_path / "does" / "not" / "exist", "integration", "HEAD")


@pytest.mark.parametrize("bad", [None, "", "  ", "a b", "a\nb", "--output=leak.txt", "-p", 5])
def test_an_unusable_revision_is_refused_before_git_runs(repo, bad):
    with pytest.raises(tamper.TamperCheckError):
        tamper.check_range(repo, bad, "integration")
    with pytest.raises(tamper.TamperCheckError):
        tamper.check_range(repo, "integration", bad)
    assert not (repo / "leak.txt").exists()


def test_git_missing_or_hanging_raises_tamper_check_error(repo, monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(tamper.subprocess, "run", missing)
    with pytest.raises(tamper.TamperCheckError, match="could not be run"):
        tamper.check_range(repo, "integration", "integration")

    def hangs(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(tamper.subprocess, "run", hangs)
    with pytest.raises(tamper.TamperCheckError, match="timed out"):
        tamper.check_range(repo, "integration", "integration", timeout=1)


def test_check_range_never_modifies_the_repository(repo):
    branch = attempt(repo, "work", append_or_true)
    before = git(repo, "status", "--porcelain=v1", "--untracked-files=all") + git(repo, "rev-parse", "HEAD", branch)

    tamper.check_range(repo, "integration", branch)

    after = git(repo, "status", "--porcelain=v1", "--untracked-files=all") + git(repo, "rev-parse", "HEAD", branch)
    assert before == after


# --- format_findings -----------------------------------------------------------------------------------------

def test_format_finding_is_one_line_per_finding_with_the_documented_shape():
    assert tamper.format_finding(Finding("skip_marker", "tests/a.py", "skip marker added: @skip", 12)) == \
        "skip_marker tests/a.py:12: skip marker added: @skip"
    assert tamper.format_finding(Finding("large_file", "big.bin", "too big")) == "large_file big.bin: too big"
    assert tamper.format_finding(Finding("coverage_lowered", "", "fell", 3)) == "coverage_lowered: fell"


def test_format_findings_is_ascii_only_and_keeps_one_line_per_finding():
    findings = [
        Finding("test_file_deleted", f"tests/test_caf{E_ACUTE}.py", f"gone {ARROW} elsewhere {EMOJI}"),
        Finding("skip_marker", "a.py", "line one\nline two\ttabbed\rcarriage", 4),
    ]

    text = tamper.format_findings(findings)

    assert text.isascii()
    assert len(text.splitlines()) == 2
    assert "caf\\xe9" in text and "\\u2192" in text and "\\U0001f600" in text
    assert "\\x0a" in text and "\\x09" in text and "\\x0d" in text  # control characters cannot break the line
    text.encode("cp1252")  # what the Windows console does; must not raise


def test_format_findings_caps_the_count_and_says_how_many_more():
    findings = [Finding("skip_marker", f"t{i}.py", "x", i) for i in range(30)]

    default = tamper.format_findings(findings).splitlines()
    assert len(default) == 21 and default[-1] == "... and 10 more"
    assert tamper.format_findings(findings, limit=3).splitlines()[-1] == "... and 27 more"
    assert len(tamper.format_findings(findings, limit=3).splitlines()) == 4
    assert tamper.format_findings(findings, limit=0) == "... and 30 more"
    assert "more" not in tamper.format_findings(findings, limit=30)
    assert "more" not in tamper.format_findings(findings, limit=500)


def test_format_findings_is_never_much_longer_than_3000_characters():
    long_detail = "d" * 400
    many = [Finding("skip_marker", "p" * 100 + f"{i}.py", long_detail, i) for i in range(1000)]

    text = tamper.format_findings(many, limit=1000)

    assert len(text) <= 3000
    assert text.splitlines()[-1].startswith("... and ") and text.splitlines()[-1].endswith(" more")
    assert all(len(line) <= 240 for line in text.splitlines())
    assert len(tamper.format_findings([Finding("skip_marker", "a.py", "x" * 5000)])) <= 240


def test_format_findings_of_nothing_is_empty():
    assert tamper.format_findings([]) == ""
    assert tamper.format_findings(None) == ""


# --- coverage_check and blocking -----------------------------------------------------------------------------

@pytest.mark.parametrize("before,after,tolerance", [
    (85.0, 84.5, 1.0),   # inside the tolerance
    (85.0, 84.0, 1.0),   # exactly the tolerance is still inside it
    (85.0, 90.0, 1.0),   # coverage rose
    (85.0, 85.0, 0.0),
    (90.0, 80.0, 10.0),
    (85.3, 84.3, 1.0),   # float noise must not push an exact 1.0 point drop over the line
    (85.0, 85.0, -3.0),  # a negative tolerance is read as zero
])
def test_coverage_inside_the_tolerance_is_not_a_finding(before, after, tolerance):
    assert tamper.coverage_check(before, after, tolerance) is None


def test_coverage_dropping_beyond_the_tolerance_is_a_blocking_finding():
    finding = tamper.coverage_check(85.0, 83.9)

    assert finding is not None
    assert (finding.kind, finding.path) == ("coverage_lowered", "")
    assert "85.0" in finding.detail and "83.9" in finding.detail and "1.1 points" in finding.detail
    assert tamper.blocking([finding]) == [finding]
    assert tamper.coverage_check(90.0, 79.0, 10.0) is not None
    assert tamper.coverage_check(80.0, 79.99, 0.0) is not None
    assert tamper.coverage_check(85.0, 80.0, -3.0) is not None


@pytest.mark.parametrize("before,after", [
    (None, 80.0), (80.0, None), (None, None), (float("nan"), 80.0), (80.0, float("inf")), ("n/a", 80.0), ([], 1),
])
def test_coverage_without_two_usable_numbers_is_no_verdict(before, after):
    assert tamper.coverage_check(before, after) is None


def test_blocking_keeps_every_finding_except_one_marked_informational():
    skip = Finding("skip_marker", "a.py", "x", 1)
    cov = tamper.coverage_check(90.0, 50.0)
    info = Finding("coverage_lowered", "", "within tolerance", None, blocks=False)

    assert tamper.blocking([skip, cov, info]) == [skip, cov]
    assert tamper.blocking([]) == []
    assert tamper.blocking(None) == []
    assert len(tamper.KINDS) == 10 and len(set(tamper.KINDS)) == 10
