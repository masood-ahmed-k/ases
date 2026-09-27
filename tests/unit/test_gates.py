import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

import pytest

from ases import db, events, gates, gitexec, tamper

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
    """Round 12 (finding 0): a checkout that fails raises GateCheckoutError instead of returning
    GateResult(passed=False, ...) -- it is never a red gate. See
    test_run_gate_checkout_infrastructure_failure_raises_not_a_red_gate below for the finding's own
    reproduction (a pre-occupied worktree path, unrelated to the commit)."""
    with pytest.raises(gates.GateCheckoutError, match="could not create gate worktree"):
        gates.run_gate(repo, "0000000000000000000000000000000000000000", "gate1", ["echo x"])


def test_run_gate_checkout_infrastructure_failure_raises_not_a_red_gate(repo, tmp_path, monkeypatch):
    """Finding 0's own reproduction method (r12_audit_findings.md): pre-occupy the exact path run_gate's own
    worktree checkout targets -- simulating a stale leftover directory, an AV lock, or a worktree-limit
    collision, something with nothing to do with the commit or the gate commands -- and confirm run_gate raises
    GateCheckoutError instead of returning GateResult(passed=False, ...). Before this fix that GateResult was
    indistinguishable from a real failing gate command: mergeq.py spent real fix-card budget on it, and
    controller.py's post-merge re-check reverted an already-landed, correct merge because of it (see
    test_controller.py's own reproductions of those two call sites)."""
    tmp_root = tmp_path / "fixed-tmp-root"
    tmp_root.mkdir()
    (tmp_root / "wt").write_bytes(b"")  # pre-occupies the path `git worktree add` targets (a FILE, not an
    # already-existing empty directory, which git accepts): unrelated to the commit
    monkeypatch.setattr(gates.tempfile, "mkdtemp", lambda prefix="": str(tmp_root))

    with pytest.raises(gates.GateCheckoutError) as excinfo:
        gates.run_gate(repo, _head_sha(repo), "gate1", ["echo hi"])

    detail = str(excinfo.value)
    assert "could not create gate worktree" in detail
    assert "already exists" in detail
    assert not tmp_root.exists()  # still torn down, exactly like a runner that raises


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
    result = subprocess.run(["git", "-C", str(repo), "worktree", "list", "--porcelain"], capture_output=True, text=True)
    # Only the primary checkout is left. Counting entries, not searching for "wt" in the text: a temp directory
    # whose path happens to contain "wt" (pytest --basetemp under a folder named ases-wt, say) made the old
    # substring check fail.
    assert [ln for ln in result.stdout.splitlines() if ln.startswith("worktree ")] == [f"worktree {repo.as_posix()}"]


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


def test_run_gate_records_an_event_when_the_worktree_cannot_be_removed(repo, tmp_path, monkeypatch):
    """Finding 11 (round 12): run_gate's finally block used to discard the teardown outcome completely (the git
    exit code was never checked, and shutil.rmtree ran with ignore_errors=True), so a Windows file lock left a
    throwaway worktree on disk with nothing anywhere saying so. Simulated deterministically here (a mocked
    rmtree that leaves the directory in place -- the same observable end state a real lock produces) rather
    than a real, timing-dependent lock; test_mergeq.py's counterpart does the same for merge_task."""
    conn = db.connect(tmp_path / "ases.db")
    seen_paths = []
    real_rmtree = shutil.rmtree

    def fake_rmtree(path, ignore_errors=False):
        seen_paths.append(pathlib.Path(path))
        # does not actually remove anything: simulates a lock that survives the finally block's own attempt

    monkeypatch.setattr(gates.shutil, "rmtree", fake_rmtree)

    result = gates.run_gate(repo, _head_sha(repo), "gate1", ["echo ok"], conn=conn, task_key="T-leak")

    assert result.passed is True  # the leak-reporting must never affect the gate's own outcome
    assert len(seen_paths) == 1
    rows = [json.loads(e["payload"]) for e in events.recent(conn, limit=50) if e["kind"] == "gate_worktree_leak"]
    assert len(rows) == 1
    assert rows[0] == {
        "task_key": "T-leak", "gate": "gate1", "path": str(seen_paths[0]), "git_exit_code": 0,
    }
    real_rmtree(seen_paths[0], ignore_errors=True)  # actually clean up what the fake left behind


def test_run_gate_reports_no_leak_when_conn_is_none(repo, monkeypatch):
    """The leak event needs somewhere to write: a caller that gave no conn (most of finalgates.py, for example)
    gets exactly the pre-round-12 behaviour, no event, never an exception from the reporting itself."""
    real_rmtree = shutil.rmtree
    seen_paths = []

    def fake_rmtree(path, ignore_errors=False):
        seen_paths.append(pathlib.Path(path))

    monkeypatch.setattr(gates.shutil, "rmtree", fake_rmtree)

    result = gates.run_gate(repo, _head_sha(repo), "gate1", ["echo ok"])  # conn defaults to None

    assert result.passed is True
    real_rmtree(seen_paths[0], ignore_errors=True)


def test_run_gate_with_a_runner_never_calls_it_for_a_bad_commit(repo, monkeypatch):
    _forbid_local_runner(monkeypatch)
    runner = FakeRunner()

    with pytest.raises(gates.GateCheckoutError, match="could not create gate worktree"):
        gates.run_gate(repo, "0000000000000000000000000000000000000000", "gate1", ["x"], runner=runner)

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
    the raised GateCheckoutError's own message (round 12, finding 0: a checkout failure raises rather than
    returning a GateResult, but ASES-SEC-01's redaction still happens before the exception is built)."""
    with pytest.raises(gates.GateCheckoutError) as excinfo:
        gates.run_gate(repo, PLANTED, "gate1", ["echo x"])

    detail = str(excinfo.value)
    assert "could not create gate worktree" in detail
    assert PLANTED not in detail and "[redacted]" in detail


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


# --- round 8, ASES-CFG-04/ASES-CFG-05: the host runner scrubs credential-shaped env vars ------------------------
#
# Every probe below prints a PRESENCE marker (KEYSEEN/NOKEY), never the value: run_gate redacts secret-shaped
# VALUES in its output (see the ASES-SEC-01 tests above), so asserting on the value itself could pass for the
# wrong reason (redaction hiding it, not the scrub actually removing it from the subprocess's environment).
# sys.executable is quoted so the probe does not depend on a PATH lookup of "python".


def _probe(*names: str) -> str:
    """A python -c command that prints one SEEN/UNSEEN word per name in `names`, in order, space separated."""
    body = ", ".join(
        f"'{name.upper()}SEEN' if os.environ.get('{name}') else 'NO{name.upper()}'" for name in names
    )
    return f'"{sys.executable}" -c "import os; print({body})"'


def _last_line(detail: str) -> str:
    """The probe's own printed line, never the echoed `$ <cmd>` line above it (which contains the SEEN/NOxxx
    words as literal source text, so checking the whole `detail` for them would pass or fail for the wrong
    reason)."""
    return detail.strip().splitlines()[-1]


def test_run_gate_hides_credential_shaped_env_vars_from_a_gate_command(repo, monkeypatch):
    """A gate command can run model-authored code (a test a coder committed), so it must not be able to read a
    provider key -- or any other credential-shaped variable -- that the operator's shell happens to hold."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-shouldnotbevisible0123456789")
    monkeypatch.setenv("MY_SERVICE_TOKEN", "also-should-not-be-visible")

    result = gates.run_gate(repo, _head_sha(repo), "gate1", [_probe("OPENROUTER_API_KEY", "MY_SERVICE_TOKEN")])

    assert result.passed is True
    assert _last_line(result.detail) == "NOOPENROUTER_API_KEY NOMY_SERVICE_TOKEN"


def test_run_gate_still_shows_non_credential_env_vars(repo, monkeypatch):
    """The fix scrubs credential-shaped names only: PATH (needed to find an interpreter under shell=True) and an
    ordinary, made-up project variable are still visible, so this did not just empty the environment."""
    monkeypatch.setenv("ASES_GATE_PROBE", "1")

    result = gates.run_gate(repo, _head_sha(repo), "gate1", [_probe("PATH", "ASES_GATE_PROBE")])

    assert result.passed is True
    assert _last_line(result.detail) == "PATHSEEN ASES_GATE_PROBESEEN"


@pytest.mark.skipif(os.name != "nt", reason="COMSPEC/SYSTEMROOT only matter to a shell=True command on Windows")
def test_run_gate_shell_command_still_runs_on_windows_with_the_scrubbed_environment(repo):
    """A shell=True command needs COMSPEC (which .exe interprets the command line) and Windows programs need
    SYSTEMROOT to start at all; scrubbed_environ keeps both because neither name is credential-shaped."""
    result = gates.run_gate(repo, _head_sha(repo), "gate1", ["echo hello-from-shell"])

    assert result.passed is True and "hello-from-shell" in result.detail


def test_run_gate_keeps_git_author_identity_but_drops_ssh_auth_sock(repo, monkeypatch):
    """Round 8 exemption (procenv._EXEMPT_EXACT_NAMES): GIT_AUTHOR_NAME survives into a gate command (a gate
    command that makes a commit can need it) even though "AUTHOR" contains "auth"; SSH_AUTH_SOCK, which is
    capability-bearing, does not."""
    monkeypatch.setenv("GIT_AUTHOR_NAME", "A U Thor")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    probe = (
        f'"{sys.executable}" -c '
        '"import os; print(os.environ.get(\'GIT_AUTHOR_NAME\', \'MISSING\'), '
        '\'SOCKSEEN\' if os.environ.get(\'SSH_AUTH_SOCK\') else \'NOSOCK\')"'
    )

    result = gates.run_gate(repo, _head_sha(repo), "gate1", [probe])

    assert result.passed is True
    assert _last_line(result.detail) == "A U Thor NOSOCK"


def _posix_shell_available() -> bool:
    return shutil.which("sh") is not None or shutil.which("bash") is not None


def _write_post_checkout_hook(repo, marker_path) -> None:
    """A post-checkout hook that writes KEYSEEN/NOKEY to `marker_path` depending on whether OPENROUTER_API_KEY is
    visible to it. Git for Windows runs a `#!/bin/sh` hook through its bundled sh.exe."""
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / "post-checkout"
    marker = str(marker_path).replace("\\", "/")
    hook.write_text(
        "#!/bin/sh\n"
        "if [ -n \"$OPENROUTER_API_KEY\" ]; then\n"
        f"  echo KEYSEEN > \"{marker}\"\n"
        "else\n"
        f"  echo NOKEY > \"{marker}\"\n"
        "fi\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)


def test_run_gate_worktree_checkout_does_not_run_a_planted_hook(repo, tmp_path, monkeypatch):
    """ASES-CFG-04, the side door named in r8_wp_gateenv.md: run_gate's own `git worktree add` used to run the
    repository's hooks, under whatever environment that git process had (round 8 scrubbed that environment, and
    this test checked the hook saw no key). Since round 9 the checkout goes through gitexec.GIT, which points
    core.hooksPath at an empty directory, so the planted hook does not run at all. Not vacuous: the same hook is
    first shown to fire under a plain `git worktree add` on the same repository."""
    marker = tmp_path / "hook-marker.txt"
    _write_post_checkout_hook(repo, marker)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-PLANTEDHOOKVALUE0123456789")

    control = tmp_path / "control-wt"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(control), _head_sha(repo)],
                   capture_output=True, text=True, check=True)
    subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(control)],
                   capture_output=True, text=True, check=True)
    if not marker.exists():
        if _posix_shell_available():
            pytest.fail("the post-checkout hook did not run under plain git even though a POSIX shell is available")
        pytest.skip("no sh or bash on PATH to run a #!/bin/sh post-checkout hook on this machine")
    marker.unlink()

    gates.run_gate(repo, _head_sha(repo), "gate1", ["echo x"])

    assert not marker.exists()


# --- finding 10 (round 12): the timeout must bound real wall-clock, not just the text that comes back ----------


def test_run_commands_kills_a_hung_command_within_the_timeout_on_real_windows(tmp_path):
    """The finding's own reproduction method (r12_audit_findings.md): a command that spawns a long-lived child
    (here, shell=True's own cmd.exe/sh wrapper around a sleeping python process is the "child") must be dead,
    and _run_commands must have returned, within the timeout plus a small margin -- not the ~6x-the-timeout
    real elapsed time the bug produced (subprocess.run's Windows TimeoutExpired handling kills only the
    cmd.exe wrapper, then drains its pipes with a second, UNTIMED communicate() that blocks until the real
    command, a grandchild that inherited the same pipe handles, exits on its own)."""
    marker = tmp_path / "still-running.txt"
    cmd = (
        f'"{sys.executable}" -c '
        f'"import time; open(r\'{marker}\', \'w\').close(); time.sleep(8)"'
    )

    started = time.monotonic()
    passed, output = gates._run_commands(tmp_path, [cmd], timeout=1)
    elapsed = time.monotonic() - started

    assert passed is False
    assert "[TIMEOUT after 1s]" in output
    assert marker.exists()  # the command really started (not a false pass from never launching at all)
    assert elapsed < 6, f"_run_commands took {elapsed:.1f}s to return for a 1s timeout: the tree was not killed"


def test_run_commands_stops_at_the_first_timeout_and_never_runs_a_later_command(tmp_path):
    cmd = f'"{sys.executable}" -c "import time; time.sleep(8)"'

    passed, output = gates._run_commands(tmp_path, [cmd, "echo never"], timeout=1)

    assert passed is False
    assert "[TIMEOUT after 1s]" in output
    assert "never" not in output


class _FakeTimingOutProcess:
    """An injectable stand-in for subprocess.Popen: communicate() times out once, then returns partial output,
    exactly like evals.py's own test of _run_process's equivalent branch."""

    pid = 4242

    def __init__(self):
        self.calls = 0

    def communicate(self, timeout=None):
        self.calls += 1
        if self.calls == 1:
            raise subprocess.TimeoutExpired("cmd", timeout)
        return "partial stdout", "partial stderr"


def test_run_commands_kills_the_whole_tree_on_timeout_then_drains_and_reports_it(tmp_path):
    """The branch-level proof (fast, deterministic, no real process) that mirrors evals.py's own
    test_run_process_stops_the_whole_process_tree_on_a_timeout_and_never_raises: the real-process test above
    proves this actually works on Windows; this one proves _run_commands' own logic without a real hang."""
    killed = []

    passed, output = gates._run_commands(
        tmp_path, ["some command"], timeout=9,
        popen=lambda *a, **k: _FakeTimingOutProcess(), kill_tree=killed.append,
    )

    assert killed == [4242]  # the whole tree, not just this Popen's own handle
    assert passed is False
    assert "partial stdout" in output and "partial stderr" in output
    assert "[TIMEOUT after 9s]" in output


def test_run_commands_a_command_that_finishes_in_time_still_works_with_the_new_popen_shape(tmp_path):
    passed, output = gates._run_commands(tmp_path, ["echo hello-from-popen"], timeout=30)

    assert passed is True
    assert "hello-from-popen" in output


def test_run_commands_stops_at_the_first_non_zero_exit_with_the_new_popen_shape(tmp_path):
    passed, output = gates._run_commands(tmp_path, ["echo first", "exit 1", "echo never"], timeout=30)

    assert passed is False
    assert "first" in output and "[exit 1]" in output and "never" not in output


def test_kill_process_tree_uses_taskkill_on_windows_and_never_killpg(monkeypatch):
    """os.killpg does not exist on Windows at all (raising=False lets the attribute be set anyway, for this
    fake), so this also proves the Windows branch never even reaches for it."""
    calls = []
    monkeypatch.setattr(gates, "_IS_WINDOWS", True)
    monkeypatch.setattr(gates.subprocess, "run", lambda argv, **kwargs: calls.append(argv))
    monkeypatch.setattr(
        gates.os, "killpg", lambda *a: pytest.fail("killpg ends a process GROUP on POSIX only"), raising=False,
    )

    gates._kill_process_tree(4242)

    assert calls == [["taskkill", "/PID", "4242", "/T", "/F"]]


def test_kill_process_tree_never_raises_when_the_process_is_already_gone(monkeypatch):
    monkeypatch.setattr(gates, "_IS_WINDOWS", True)

    def boom(argv, **kwargs):
        raise subprocess.SubprocessError("no such process")

    monkeypatch.setattr(gates.subprocess, "run", boom)

    gates._kill_process_tree(4242)  # must not raise


# --- _git: the one local git-launcher, running git through gitexec since the round 9 merge


def test_git_helper_builds_a_dash_c_argv_and_scrubs_the_environment(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["env"] = kwargs.get("env")
        seen["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(gates.subprocess, "run", fake_run)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-SHOULDNEVERSURVIVE0123456789")

    gates._git(["status"], cwd="C:/some/repo", timeout=42)

    assert seen["argv"] == [*gitexec.GIT, "-C", "C:/some/repo", "status"]
    assert seen["timeout"] == 42
    assert "OPENROUTER_API_KEY" not in seen["env"]


def test_git_helper_without_cwd_has_no_dash_c(monkeypatch):
    seen = {}
    monkeypatch.setattr(gates.subprocess, "run", lambda argv, **kw: seen.update(argv=argv) or
                         subprocess.CompletedProcess(argv, 0, "", ""))

    gates._git(["clone", "a", "b"], timeout=42)

    assert seen["argv"] == [*gitexec.GIT, "clone", "a", "b"]


# --- resolve_runner (round 9, ASES-QG-04, ASES-SEC-03/05/07) ---------------------------------------------------


class _FakeProjectConfig:
    """A duck-typed stand-in for ases.config.ProjectConfig: resolve_runner reads only these two members."""

    def __init__(self, *, enabled=True, image="registry.example/toolchain:1.0"):
        self.sandbox_enabled = enabled
        self._image = image

    def sandbox_policy_config(self):
        return {"sandbox": {"image": self._image}}


class _FakeTask:
    """A duck-typed stand-in for ases.plan.PlanTask: resolve_runner reads only these two members."""

    def __init__(self, sandbox_network=False, sandbox_network_reason=""):
        self.sandbox_network = sandbox_network
        self.sandbox_network_reason = sandbox_network_reason


def test_resolve_runner_off_with_no_project_config():
    choice = gates.resolve_runner(None)
    assert choice == gates.GateRunner(None, False)


def test_resolve_runner_off_when_sandbox_enabled_is_false():
    choice = gates.resolve_runner(_FakeProjectConfig(enabled=False))
    assert choice == gates.GateRunner(None, False)


def test_resolve_runner_on_returns_a_callable_sandbox_runner_and_self_contained_true():
    choice = gates.resolve_runner(_FakeProjectConfig(enabled=True))
    assert choice.self_contained is True
    assert callable(choice.runner)


def test_resolve_runner_builds_the_policy_from_the_projects_own_sandbox_config(monkeypatch):
    seen = []

    def fake_command_runner(policy, *, network=False, **kwargs):
        seen.append((policy.image, network))
        return lambda *a: (True, "")

    monkeypatch.setattr(gates.sandbox_mod, "sandbox_command_runner", fake_command_runner)

    gates.resolve_runner(_FakeProjectConfig(image="registry.example/special:9.9"))

    assert seen == [("registry.example/special:9.9", False)]


def test_resolve_runner_grants_network_only_to_a_task_with_an_explicit_non_empty_reason(monkeypatch):
    seen = []
    monkeypatch.setattr(
        gates.sandbox_mod, "sandbox_command_runner",
        lambda policy, *, network=False, **kwargs: seen.append(network) or (lambda *a: (True, "")),
    )

    gates.resolve_runner(_FakeProjectConfig(), task=None)
    gates.resolve_runner(_FakeProjectConfig(), task=_FakeTask(False, ""))
    gates.resolve_runner(_FakeProjectConfig(), task=_FakeTask(False, "installs a package"))  # flag off: ignored
    gates.resolve_runner(_FakeProjectConfig(), task=_FakeTask(True, ""))  # no reason: no exception
    gates.resolve_runner(_FakeProjectConfig(), task=_FakeTask(True, "   "))  # blank reason: no exception
    gates.resolve_runner(_FakeProjectConfig(), task=_FakeTask(True, "installs a package"))

    assert seen == [False, False, False, False, False, True]


def test_resolve_runner_never_grants_network_when_the_switch_is_off():
    """Gates 4/5 and every other caller that passes no task, or a project with the sandbox off, always gets
    GateRunner(None, False): there is no runner at all to carry a network flag."""
    choice = gates.resolve_runner(_FakeProjectConfig(enabled=False), task=_FakeTask(True, "needs the registry"))
    assert choice == gates.GateRunner(None, False)


# --- self-contained checkout (round 9, ASES-QG-04, ASES-SEC-03): sandbox mode's own checkout, no Docker needed --


def test_self_contained_checkout_is_a_real_directory_at_the_exact_sha_with_no_alternates(repo, tmp_path):
    target = tmp_path / "checkout"
    sha = _head_sha(repo)

    result = gates._self_contained_checkout(repo, sha, target)

    assert result.returncode == 0
    assert (target / ".git").is_dir()  # never a FILE pointing back at the host repo, unlike a linked worktree
    head = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "HEAD"], capture_output=True, text=True,
    ).stdout.strip()
    assert head == sha
    assert (target / "ok.py").is_file()  # the commit's tree was actually checked out, not left bare
    assert not (target / ".git" / "objects" / "info" / "alternates").exists()


def test_self_contained_checkout_never_shares_a_host_object_file(repo, tmp_path):
    """--no-hardlinks: writing through the clone's own object file must never reach the host repository's copy.
    A real hardlink would make this mutation visible on both sides."""
    target = tmp_path / "checkout"
    gates._self_contained_checkout(repo, _head_sha(repo), target)

    checked = False
    for shard in (target / ".git" / "objects").iterdir():
        if shard.name in ("pack", "info") or not shard.is_dir():
            continue
        for obj in shard.iterdir():
            source_obj = repo / ".git" / "objects" / shard.name / obj.name
            assert source_obj.is_file(), "the clone has an object the source repository does not"
            before = source_obj.read_bytes()
            with open(obj, "ab") as fh:
                fh.write(b"\x00not-shared")
            assert source_obj.read_bytes() == before, "the clone's object and the host's are the same file (hardlinked)"
            checked = True
            break
        if checked:
            break
    assert checked, "expected at least one loose object to compare"


def test_self_contained_checkout_does_not_share_the_host_repositorys_hooks(repo, tmp_path):
    """Unlike run_gate's own worktree-add checkout (test_run_gate_worktree_checkout_hook_cannot_see_a_planted_key
    above), a clone's .git/hooks holds only git's own .sample files: a hook planted in the host repository never
    runs at all here, not even without a credential to see."""
    marker = tmp_path / "marker.txt"
    _write_post_checkout_hook(repo, marker)
    target = tmp_path / "checkout"

    result = gates._self_contained_checkout(repo, _head_sha(repo), target)

    assert result.returncode == 0
    assert not marker.exists()


def test_self_contained_checkout_a_bad_commit_fails_cleanly(repo, tmp_path):
    target = tmp_path / "checkout"

    result = gates._self_contained_checkout(repo, "0" * 40, target)

    assert result.returncode != 0


# --- run_gate(self_contained_checkout=True) (round 9) ------------------------------------------------------------


def test_run_gate_self_contained_checkout_gives_the_runner_a_standalone_clone(repo, monkeypatch):
    _forbid_local_runner(monkeypatch)
    seen = {}

    def runner(worktree, commands, timeout):
        # Captured DURING the call: run_gate tears the checkout down again as soon as this returns.
        seen["is_git_dir"] = (worktree / ".git").is_dir()
        seen["commands"] = list(commands)
        return True, "ran in the sandbox"

    result = gates.run_gate(
        repo, _head_sha(repo), "gate1", ["pytest -q"], runner=runner, self_contained_checkout=True,
    )

    assert (result.passed, result.detail) == (True, "ran in the sandbox")
    assert seen == {"is_git_dir": True, "commands": ["pytest -q"]}


def test_run_gate_self_contained_checkout_cleans_up_afterward(repo, monkeypatch):
    _forbid_local_runner(monkeypatch)
    runner = FakeRunner()

    gates.run_gate(repo, _head_sha(repo), "gate1", ["x"], runner=runner, self_contained_checkout=True)

    worktree = runner.calls[0][0]
    assert not worktree.exists() and not worktree.parent.exists()
    # unlike the worktree mode, nothing was ever registered with the source repository to unregister
    listing = subprocess.run(["git", "-C", str(repo), "worktree", "list"], capture_output=True, text=True).stdout
    assert len(listing.strip().splitlines()) == 1


def test_run_gate_self_contained_checkout_bad_commit_never_calls_the_runner(repo, monkeypatch):
    _forbid_local_runner(monkeypatch)
    runner = FakeRunner()

    with pytest.raises(gates.GateCheckoutError, match="could not create gate checkout"):
        gates.run_gate(repo, "0" * 40, "gate1", ["x"], runner=runner, self_contained_checkout=True)

    assert runner.calls == []


def test_run_gate_self_contained_checkout_false_is_still_todays_worktree_message(repo):
    """The host-mode failure text must stay exactly what it was before this round, since an existing caller or
    test may match on it. Round 12 (finding 0): it is now the message of a raised GateCheckoutError, not a
    returned GateResult."""
    with pytest.raises(gates.GateCheckoutError, match="could not create gate worktree"):
        gates.run_gate(repo, "0" * 40, "gate1", ["x"])


# --- hash_gate_profiles with pinned_task_fields (round 9 ASES-SEC-05/07; round 10 ASES-QG-02, GATEPIN) ------------


def test_hash_gate_profiles_with_no_pinned_fields_matches_the_hash_before_this_parameter_existed():
    profiles = {"default": ["pytest -q"]}
    assert gates.hash_gate_profiles(profiles) == gates.hash_gate_profiles(profiles, None) \
        == gates.hash_gate_profiles(profiles, {})


def test_hash_gate_profiles_changes_when_a_network_exception_is_added_or_edited():
    profiles = {"default": ["pytest -q"]}
    plain = gates.hash_gate_profiles(profiles)
    with_exception = gates.hash_gate_profiles(profiles, {"T1": {"sandbox_network": [True, "installs a package"]}})
    different_reason = gates.hash_gate_profiles(profiles, {"T1": {"sandbox_network": [True, "a different reason"]}})

    assert plain != with_exception != different_reason
    assert len({plain, with_exception, different_reason}) == 3
    # deterministic and order-independent, same as the base hash
    assert gates.hash_gate_profiles(
        profiles, {"T1": {"sandbox_network": [True, "installs a package"]}}) == with_exception


def test_hash_gate_profiles_changes_when_the_allow_gate_config_changes_marker_is_added():
    """ASES-QG-02 (round 10, GATEPIN): the marker folds into the same pin the network exception does, by the
    same pinned_task_fields mapping -- gates.py itself does not know or care which per-task field it is."""
    profiles = {"default": ["pytest -q"]}
    plain = gates.hash_gate_profiles(profiles)
    with_marker = gates.hash_gate_profiles(profiles, {"T1": {"allow_gate_config_changes": True}})

    assert plain != with_marker
    # deterministic
    assert gates.hash_gate_profiles(profiles, {"T1": {"allow_gate_config_changes": True}}) == with_marker


def test_hash_gate_profiles_a_task_with_both_pinned_fields_differs_from_either_alone():
    """A task can carry both a network exception and the gate-config marker at once; the hash must tell that
    state apart from having only one of the two, so flipping either one alone is still caught."""
    profiles = {"default": ["pytest -q"]}
    network_only = gates.hash_gate_profiles(profiles, {"T1": {"sandbox_network": [True, "needs pypi"]}})
    marker_only = gates.hash_gate_profiles(profiles, {"T1": {"allow_gate_config_changes": True}})
    both = gates.hash_gate_profiles(
        profiles, {"T1": {"sandbox_network": [True, "needs pypi"], "allow_gate_config_changes": True}})

    assert len({network_only, marker_only, both}) == 3


def test_hash_gate_profiles_pinned_task_fields_key_order_does_not_matter():
    """sort_keys=True (see the docstring) applies to the whole payload, not just gate_profiles: a task's own
    field order, and the order tasks appear in the mapping, must not change the hash either."""
    profiles = {"default": ["pytest -q"]}
    t1_fields_forward = {"sandbox_network": [True, "needs pypi"], "allow_gate_config_changes": True}
    t1_fields_reversed = {"allow_gate_config_changes": True, "sandbox_network": [True, "needs pypi"]}
    t2_fields = {"allow_gate_config_changes": True}
    a = gates.hash_gate_profiles(profiles, {"T1": t1_fields_forward, "T2": t2_fields})
    b = gates.hash_gate_profiles(profiles, {"T2": t2_fields, "T1": t1_fields_reversed})
    assert a == b
