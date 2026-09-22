import subprocess

import pytest

from ases import db, gates, tamper

# Non-ASCII test data is built with chr() so that this file stays pure ASCII.
E_ACUTE = chr(0xE9)


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git("init", "-q", "-b", "integration", cwd=r)
    _git("config", "user.email", "t@t", cwd=r)
    _git("config", "user.name", "t", cwd=r)
    (r / "ok.py").write_text("print('hi')\n", encoding="utf-8")
    _git("add", "-A", cwd=r)
    _git("commit", "-q", "-m", "init", cwd=r)
    return r


def _head_sha(repo):
    result = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True)
    return result.stdout.strip()


def test_run_gate_pass(repo):
    result = gates.run_gate(repo, _head_sha(repo), "gate1", ["python ok.py"])
    assert result.passed is True
    assert "hi" in result.detail


def test_run_gate_fail(repo):
    result = gates.run_gate(repo, _head_sha(repo), "gate1", ["python -c \"import sys; sys.exit(1)\""])
    assert result.passed is False


def test_run_gate_stops_at_first_failing_command(repo):
    result = gates.run_gate(repo, _head_sha(repo), "gate1", ["echo first", "exit 1", "echo never"])
    assert result.passed is False
    assert "never" not in result.detail


def test_run_gate_bad_commit(repo):
    result = gates.run_gate(repo, "0000000000000000000000000000000000000000", "gate1", ["echo x"])
    assert result.passed is False
    assert "could not create gate worktree" in result.detail


def test_run_gate_records_to_db(repo, tmp_path):
    conn = db.connect(tmp_path / "ases.db")
    sha = _head_sha(repo)
    gates.run_gate(repo, sha, "gate1", ["python ok.py"], conn=conn, task_key="T1")
    assert gates.last_gate_result(conn, "T1", "gate1", sha) == "pass"


def test_last_gate_result_none_when_absent(tmp_path):
    conn = db.connect(tmp_path / "ases.db")
    assert gates.last_gate_result(conn, "T1", "gate1", "deadbeef") is None


# --- schema v7: gate_runs.project (round 6) ----------------------------------------------------------------------


def test_run_gate_with_no_project_stores_a_null_project_column(repo, tmp_path):
    """Keep the old behavior exactly when project is not given: a NULL project column, matching every row
    written before this column existed."""
    conn = db.connect(tmp_path / "ases.db")
    sha = _head_sha(repo)

    gates.run_gate(repo, sha, "gate1", ["python ok.py"], conn=conn, task_key="T1")

    row = conn.execute("SELECT project FROM gate_runs WHERE task_key = 'T1'").fetchone()
    assert row["project"] is None


def test_run_gate_stores_the_project_it_is_given(repo, tmp_path):
    conn = db.connect(tmp_path / "ases.db")
    sha = _head_sha(repo)

    gates.run_gate(repo, sha, "gate1", ["python ok.py"], conn=conn, task_key="T1", project="p1")

    row = conn.execute("SELECT project FROM gate_runs WHERE task_key = 'T1'").fetchone()
    assert row["project"] == "p1"


def test_last_gate_result_with_no_project_ignores_the_column_entirely(repo, tmp_path):
    """The old, still-default call shape: every row for this task_key/gate/commit_sha counts, whatever project
    (or none) wrote it -- exactly the query this function ran before schema v7 added the column."""
    conn = db.connect(tmp_path / "ases.db")
    sha = _head_sha(repo)
    gates.run_gate(repo, sha, "gate1", ["python ok.py"], conn=conn, task_key="T1", project="someone-elses-project")

    assert gates.last_gate_result(conn, "T1", "gate1", sha) == "pass"


def test_last_gate_result_scoped_to_a_project_matches_its_own_rows_and_null_legacy_rows(repo, tmp_path):
    conn = db.connect(tmp_path / "ases.db")
    sha = _head_sha(repo)
    gates.run_gate(repo, sha, "gate1", ["python ok.py"], conn=conn, task_key="T1", project="p1")

    assert gates.last_gate_result(conn, "T1", "gate1", sha, project="p1") == "pass"

    conn.execute("DELETE FROM gate_runs")
    gates.run_gate(repo, sha, "gate1", ["python ok.py"], conn=conn, task_key="T1")  # no project: a legacy row

    assert gates.last_gate_result(conn, "T1", "gate1", sha, project="p1") == "pass"  # NULL still counts


def test_last_gate_result_scoped_to_a_project_never_matches_a_different_projects_row(repo, tmp_path):
    """The bug two projects sharing a database (or reusing a task key) used to hit: the same task_key and commit
    under a DIFFERENT project must not be read as this project's own gate history."""
    conn = db.connect(tmp_path / "ases.db")
    sha = _head_sha(repo)
    gates.run_gate(repo, sha, "gate1", ["exit 1"], conn=conn, task_key="T1", project="p2")  # p2's row: fail

    assert gates.last_gate_result(conn, "T1", "gate1", sha, project="p1") is None  # not p1's history
    assert gates.last_gate_result(conn, "T1", "gate1", sha, project="p2") == "fail"  # but is p2's


def test_last_gate_result_scoped_to_a_project_the_latest_row_still_wins(repo, tmp_path):
    conn = db.connect(tmp_path / "ases.db")
    sha = _head_sha(repo)
    gates.run_gate(repo, sha, "gate1", ["exit 1"], conn=conn, task_key="T1", project="p1")
    gates.run_gate(repo, sha, "gate1", ["python ok.py"], conn=conn, task_key="T1", project="p1")

    assert gates.last_gate_result(conn, "T1", "gate1", sha, project="p1") == "pass"


def test_gate_worktree_cleaned_up(repo):
    gates.run_gate(repo, _head_sha(repo), "gate1", ["echo x"])
    result = subprocess.run(["git", "-C", str(repo), "worktree", "list"], capture_output=True, text=True)
    assert "wt" not in result.stdout


@pytest.mark.parametrize("diff,should_flag", [
    ("+api_key = 'sk-or-v1-1234567890abcdefghij'\n", True),
    ("+token = \"ghp_1234567890abcdefghij\"\n", True),
    ("+GITHUB_TOKEN=ghp_abcdefghij1234567890\n", True),
    ("+print('hello world')\n", False),
    ("-api_key = 'sk-or-v1-1234567890abcdefghij'\n", False),  # removed, not added
    ("+x = 1\n+y = 2\n", False),
])
def test_scan_for_secrets(diff, should_flag):
    findings = gates.scan_for_secrets(diff)
    assert bool(findings) == should_flag


@pytest.mark.parametrize("diff,expected_hit", [
    ("-    def test_something():\n-        assert True\n", "deleted"),
    ("+    @pytest.mark.skip\n+    def test_x(): pass\n", "skip"),
    ("+        run() || true\n", "|| true"),
    ("+    assert x == 1\n", None),
])
def test_detect_tamper(diff, expected_hit):
    findings = gates.detect_tamper(diff)
    if expected_hit:
        assert any(expected_hit in f for f in findings)
    else:
        assert findings == []


@pytest.mark.parametrize("a,b,expect_equal", [
    # identical content -> identical hash
    ({"fast": ["pytest -q"], "slow": ["pytest", "flake8"]},
     {"fast": ["pytest -q"], "slow": ["pytest", "flake8"]}, True),
    # changing a command's text changes the hash
    ({"default": ["pytest -q"]}, {"default": ["pytest -q -x"]}, False),
    # profile-KEY order doesn't matter (sort_keys=True)
    ({"fast": ["pytest -q"], "slow": ["pytest"]}, {"slow": ["pytest"], "fast": ["pytest -q"]}, True),
    # command ORDER within one profile's list DOES matter
    ({"default": ["a", "b"]}, {"default": ["b", "a"]}, False),
])
def test_hash_gate_profiles(a, b, expect_equal):
    assert (gates.hash_gate_profiles(a) == gates.hash_gate_profiles(b)) == expect_equal


# --- detect_tamper delegates to tamper.py (ASES-QG-03) --------------------------------------------------------

@pytest.mark.parametrize("line", [
    "@pytest.mark.skip",
    "@pytest.mark.skipif(True, reason='x')",
    "@pytest.mark.xfail(strict=False)",
    "run_all_tests || true",
    "x = compute()  # noqa: test",
])
def test_detect_tamper_still_flags_the_old_cheap_markers(line):
    findings = gates.detect_tamper(f"+{line}\n")

    assert findings and all(isinstance(f, str) for f in findings)


def test_detect_tamper_still_flags_a_removed_test_function_at_any_indent():
    assert gates.detect_tamper("-def test_a():\n-    pass\n")
    assert gates.detect_tamper("-    def test_a(self):\n-        pass\n")
    assert gates.detect_tamper("+    assert x == 1\n") == []


def test_detect_tamper_delegates_to_the_tamper_module_and_renders_one_string_per_finding(monkeypatch):
    seen = []

    def fake(diff_text, **kwargs):
        seen.append(diff_text)
        return [tamper.Finding("skip_marker", "a.py", "skip marker added: @skip", 3),
                tamper.Finding("large_file", "big.bin", "too big")]

    monkeypatch.setattr(tamper, "analyze_diff", fake)

    assert gates.detect_tamper("some diff") == [
        "skip_marker a.py:3: skip marker added: @skip", "large_file big.bin: too big",
    ]
    assert seen == ["some diff"]


def test_detect_tamper_reports_a_real_diff_with_paths_and_stays_ascii():
    diff = (
        f"diff --git a/tests/test_caf{E_ACUTE}.py b/tests/test_caf{E_ACUTE}.py\n--- a/tests/test_caf{E_ACUTE}.py\n"
        f"+++ b/tests/test_caf{E_ACUTE}.py\n@@ -1,2 +1,2 @@\n-def test_x():\n+@pytest.mark.skip\n+def test_x():\n     pass\n"
    )

    findings = gates.detect_tamper(diff)

    assert any("skip_marker" in f and "test_caf\\xe9.py:1" in f for f in findings)
    assert all(f.isascii() for f in findings)


# --- scan_for_secrets: added secret files, and never echoing a value ------------------------------------------

def _added_file_diff(path, line="DEBUG=1"):
    return (
        f"diff --git a/{path} b/{path}\nnew file mode 100644\nindex 0000000..1111111\n--- /dev/null\n"
        f"+++ b/{path}\n@@ -0,0 +1 @@\n+{line}\n"
    )


@pytest.mark.parametrize("path", [".env", "config/.env", ".env.local", "certs/server.pem", "id_rsa", "keys/id_ed25519",
                                  "secrets/api.key", "cert.p12"])
def test_scan_for_secrets_flags_an_added_secret_file_by_name_alone(path):
    findings = gates.scan_for_secrets(_added_file_diff(path))

    assert len(findings) == 1
    assert path in findings[0] and "DEBUG=1" not in findings[0]


@pytest.mark.parametrize("path", [".env.example", ".env.sample", "src/envelope.py", "docs/keys.md", "keyboard.py"])
def test_scan_for_secrets_does_not_flag_look_alike_file_names(path):
    assert gates.scan_for_secrets(_added_file_diff(path)) == []


def test_scan_for_secrets_ignores_a_deleted_or_merely_modified_secret_file():
    deleted = ("diff --git a/.env b/.env\ndeleted file mode 100644\nindex 1111111..0000000\n--- a/.env\n"
               "+++ /dev/null\n@@ -1 +0,0 @@\n-DEBUG=1\n")
    modified = ("diff --git a/.env b/.env\nindex 1..2 100644\n--- a/.env\n+++ b/.env\n@@ -1 +1 @@\n-DEBUG=1\n+DEBUG=0\n")

    assert gates.scan_for_secrets(deleted) == []
    assert gates.scan_for_secrets(modified) == []


def test_scan_for_secrets_never_echoes_the_secret_it_found():
    planted = "sk-or-v1-PLANTEDVALUE0123456789abcd"
    diff = _added_file_diff("config.py", f"API_KEY = '{planted}'")

    findings = gates.scan_for_secrets(diff)

    assert len(findings) == 1
    assert "config.py:1" in findings[0]
    assert planted not in findings[0] and "PLANTEDVALUE" not in findings[0]
    # the way questions.py uses it: one bare added line at a time, with no file name
    bare = gates.scan_for_secrets("+" + f"token = {planted}")
    assert len(bare) == 1 and planted not in bare[0]
    assert gates.scan_for_secrets("+" + "nothing to see here") == []


def test_scan_for_secrets_finds_a_private_key_block_and_an_aws_key_id():
    """Both shapes used to be found only by tamper.py's extra patterns; events.py now redacts them too (2026-09-21),
    so the label may come from either place. What matters is one finding each and no echo of the value."""
    aws_id = "AKIA" + "IOSFODNN7EXAMPLE"
    private_key = gates.scan_for_secrets(_added_file_diff("notes.txt", "-----BEGIN RSA PRIVATE KEY-----"))
    aws = gates.scan_for_secrets(_added_file_diff("notes.txt", f"id = {aws_id}"))

    assert len(private_key) == 1 and "notes.txt:1" in private_key[0]
    assert len(aws) == 1 and "notes.txt:1" in aws[0] and aws_id not in aws[0]


def test_scan_for_secrets_output_is_ascii_even_for_an_odd_file_name():
    findings = gates.scan_for_secrets(_added_file_diff(f"caf{E_ACUTE}/.env"))

    assert findings and all(f.isascii() for f in findings)


# --- run_gate(runner=...) ---------------------------------------------------------------------------------------

class FakeRunner:
    """A stand-in for the sandbox's runner: records what run_gate handed it and looks at the checkout."""

    def __init__(self, passed=True, detail="fake runner output"):
        self.passed, self.detail = passed, detail
        self.calls = []
        self.checkout_files = None

    def __call__(self, worktree, commands, timeout):
        self.calls.append((worktree, list(commands), timeout))
        self.checkout_files = sorted(p.name for p in worktree.iterdir() if p.name != ".git")
        return self.passed, self.detail


def _forbid_local_runner(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("_run_commands must not run when a runner is given")

    monkeypatch.setattr(gates, "_run_commands", boom)


def test_run_gate_uses_the_runner_instead_of_the_local_command_runner(repo, monkeypatch):
    _forbid_local_runner(monkeypatch)
    runner = FakeRunner(passed=True, detail="ran in a container")

    result = gates.run_gate(repo, _head_sha(repo), "gate1", ["pytest -q", "ruff check"], runner=runner,
                            timeout_per_command=77)

    assert (result.passed, result.detail, result.gate) == (True, "ran in a container", "gate1")
    assert result.commit_sha == _head_sha(repo)
    (worktree, commands, timeout), = runner.calls
    assert commands == ["pytest -q", "ruff check"]
    assert timeout == 77
    assert runner.checkout_files == ["ok.py"]  # the runner got a real checkout of the exact commit


def test_run_gate_passes_a_failing_runner_result_through_as_a_red_gate(repo, monkeypatch):
    _forbid_local_runner(monkeypatch)

    result = gates.run_gate(repo, _head_sha(repo), "gate1", ["x"], runner=FakeRunner(passed=False, detail="1 failed"))

    assert (result.passed, result.detail) == (False, "1 failed")


def test_run_gate_with_a_runner_still_records_the_gate_runs_row(repo, tmp_path, monkeypatch):
    _forbid_local_runner(monkeypatch)
    conn = db.connect(tmp_path / "ases.db")
    sha = _head_sha(repo)

    gates.run_gate(repo, sha, "gate1", ["x"], conn=conn, task_key="T1", runner=FakeRunner(True, "green in docker"))
    gates.run_gate(repo, sha, "gate3", ["x"], conn=conn, task_key="T1", runner=FakeRunner(False, "red in docker"))

    rows = [dict(r) for r in conn.execute("SELECT * FROM gate_runs ORDER BY id")]
    assert [(r["task_key"], r["gate"], r["commit_sha"], r["result"], r["detail"]) for r in rows] == [
        ("T1", "gate1", sha, "pass", "green in docker"), ("T1", "gate3", sha, "fail", "red in docker"),
    ]
    assert all(r["ran_at"] for r in rows)
    assert gates.last_gate_result(conn, "T1", "gate1", sha) == "pass"


def test_run_gate_with_a_runner_cleans_the_worktree_up(repo, monkeypatch):
    _forbid_local_runner(monkeypatch)
    runner = FakeRunner()

    gates.run_gate(repo, _head_sha(repo), "gate1", ["x"], runner=runner)

    worktree = runner.calls[0][0]
    assert not worktree.exists() and not worktree.parent.exists()
    listing = subprocess.run(["git", "-C", str(repo), "worktree", "list"], capture_output=True, text=True).stdout
    assert len(listing.strip().splitlines()) == 1


def test_run_gate_with_a_runner_never_calls_it_for_a_bad_commit(repo, monkeypatch):
    _forbid_local_runner(monkeypatch)
    runner = FakeRunner()

    result = gates.run_gate(repo, "0000000000000000000000000000000000000000", "gate1", ["x"], runner=runner)

    assert result.passed is False and "could not create gate worktree" in result.detail
    assert runner.calls == []


def test_a_runner_that_raises_still_cleans_up_and_writes_no_row(repo, tmp_path):
    conn = db.connect(tmp_path / "ases.db")
    seen = []

    def exploding(worktree, commands, timeout):
        seen.append(worktree)
        raise RuntimeError("docker daemon is not running")

    with pytest.raises(RuntimeError, match="docker daemon"):
        gates.run_gate(repo, _head_sha(repo), "gate1", ["x"], conn=conn, task_key="T1", runner=exploding)

    assert not seen[0].exists()
    assert conn.execute("SELECT COUNT(*) FROM gate_runs").fetchone()[0] == 0
    listing = subprocess.run(["git", "-C", str(repo), "worktree", "list"], capture_output=True, text=True).stdout
    assert len(listing.strip().splitlines()) == 1


def test_run_gate_without_a_runner_still_uses_the_local_command_runner(repo, monkeypatch):
    calls = []

    def spy(cwd, commands, timeout):
        calls.append((cwd, list(commands), timeout))
        return True, "from the local runner"

    monkeypatch.setattr(gates, "_run_commands", spy)

    result = gates.run_gate(repo, _head_sha(repo), "gate1", ["echo x"])
    explicit_none = gates.run_gate(repo, _head_sha(repo), "gate1", ["echo x"], runner=None)

    assert result.detail == explicit_none.detail == "from the local runner"
    assert [(c[1], c[2]) for c in calls] == [(["echo x"], 120), (["echo x"], 120)]


# --- ASES-SEC-01: the command output is redacted once, before it is stored and before it is returned -----------

PLANTED = "sk-or-v1-PLANTEDVALUE0123456789abcd"


def test_run_gate_redacts_a_secret_in_the_output_it_returns(repo):
    result = gates.run_gate(repo, _head_sha(repo), "gate1", [f"echo token={PLANTED}"])

    assert result.passed is True
    assert PLANTED not in result.detail and "PLANTEDVALUE" not in result.detail
    assert "[redacted]" in result.detail
    assert "token=" in result.detail  # only the secret went: the rest of the output is intact


def test_run_gate_stores_the_redacted_output_and_stores_what_it_returns(repo, tmp_path):
    """A red gate, because a failing test that prints its environment is the usual way a secret gets into gate
    output. The gate_runs row and the returned GateResult must carry the same, redacted, text."""
    conn = db.connect(tmp_path / "ases.db")

    result = gates.run_gate(
        repo, _head_sha(repo), "gate1", [f"echo leaked {PLANTED}", "exit 1"], conn=conn, task_key="T1",
    )

    row = conn.execute("SELECT result, detail FROM gate_runs WHERE task_key = 'T1'").fetchone()
    assert result.passed is False and row["result"] == "fail"
    assert PLANTED not in row["detail"] and "PLANTEDVALUE" not in row["detail"]
    assert "[redacted]" in row["detail"] and "[exit 1]" in row["detail"]
    assert row["detail"] == result.detail


def test_run_gate_redacts_what_a_runner_hands_back(repo, tmp_path, monkeypatch):
    _forbid_local_runner(monkeypatch)
    conn = db.connect(tmp_path / "ases.db")

    result = gates.run_gate(
        repo, _head_sha(repo), "gate3", ["x"], conn=conn, task_key="T2",
        runner=FakeRunner(passed=False, detail=f"container env: OPENAI_API_KEY={PLANTED}"),
    )

    stored = conn.execute("SELECT detail FROM gate_runs WHERE task_key = 'T2'").fetchone()["detail"]
    assert PLANTED not in result.detail and PLANTED not in stored
    assert result.detail == stored == "container env: OPENAI_API_KEY=[redacted]"


def test_run_gate_redacts_the_worktree_error_too(repo):
    """git echoes the bad ref back in its error, so a value shaped like a key in the commit argument would land in
    the detail of a gate that never ran."""
    result = gates.run_gate(repo, PLANTED, "gate1", ["echo x"])

    assert result.passed is False and "could not create gate worktree" in result.detail
    assert PLANTED not in result.detail and "[redacted]" in result.detail


def test_run_gate_leaves_output_with_no_secret_exactly_as_it_was(repo, monkeypatch):
    _forbid_local_runner(monkeypatch)
    text = "$ pytest -q\n3 passed in 0.12s\ncommit 0123456789abcdef0123456789abcdef01234567 ok"

    result = gates.run_gate(repo, _head_sha(repo), "gate1", ["x"], runner=FakeRunner(True, text))

    assert result.detail == text


def test_a_runner_result_that_is_not_text_is_passed_through_instead_of_crashing_the_gate(repo, tmp_path, monkeypatch):
    """The runner contract is (passed, output-as-text). A runner that returns something else must not turn a gate
    that already ran into an exception: there is nothing to redact, so it is left as it is."""
    _forbid_local_runner(monkeypatch)
    conn = db.connect(tmp_path / "ases.db")

    result = gates.run_gate(
        repo, _head_sha(repo), "gate1", ["x"], conn=conn, task_key="T3", runner=lambda tree, cmds, timeout: (True, None),
    )

    assert result.passed is True and result.detail is None
    assert conn.execute("SELECT detail FROM gate_runs WHERE task_key = 'T3'").fetchone()["detail"] is None
