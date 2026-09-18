import subprocess

import pytest

from ases import db, gates


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


def test_gate_worktree_cleaned_up(repo):
    gates.run_gate(repo, _head_sha(repo), "gate1", ["echo x"])
    result = subprocess.run(["git", "-C", str(repo), "worktree", "list"], capture_output=True, text=True)
    assert "wt" not in result.stdout


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
