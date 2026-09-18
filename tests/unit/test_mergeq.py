import subprocess

import pytest

from ases import db, mergeq


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def _git_ok(*args, cwd):
    result = _git(*args, cwd=cwd)
    assert result.returncode == 0, result.stderr
    return result


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git_ok("init", "-q", "-b", "integration", cwd=r)
    _git_ok("config", "user.email", "t@t", cwd=r)
    _git_ok("config", "user.name", "t", cwd=r)
    (r / "base.txt").write_text("base\n", encoding="utf-8")
    _git_ok("add", "-A", cwd=r)
    _git_ok("commit", "-q", "-m", "init", cwd=r)
    return r


def _make_work_branch(repo, branch, filename, content):
    _git_ok("checkout", "-q", "-b", branch, cwd=repo)
    (repo / filename).write_text(content, encoding="utf-8")
    _git_ok("add", "-A", cwd=repo)
    _git_ok("commit", "-q", "-m", f"add {filename}", cwd=repo)
    _git_ok("checkout", "-q", "integration", cwd=repo)


def test_successful_merge_fast_forwards_integration(repo, tmp_path):
    _make_work_branch(repo, "swarm/t1", "new.txt", "hello\n")
    conn = db.connect(tmp_path / "ases.db")
    before = _git_ok("rev-parse", "integration", cwd=repo).stdout.strip()

    outcome = mergeq.merge_task(repo, "integration", "swarm/t1", "T1", ["echo gate3-ok"], conn=conn)

    assert outcome.merged is True
    assert outcome.gate3_result == "pass"
    after = _git_ok("rev-parse", "integration", cwd=repo).stdout.strip()
    assert after != before
    assert (repo / "new.txt").exists()
    row = conn.execute("SELECT * FROM merge_records WHERE task_key='T1'").fetchone()
    assert row["squash_commit"] == outcome.squash_commit


def test_squash_produces_one_commit_not_the_branchs_history(repo, tmp_path):
    _git_ok("checkout", "-q", "-b", "swarm/t2", cwd=repo)
    for i in range(3):
        (repo / f"f{i}.txt").write_text(str(i), encoding="utf-8")
        _git_ok("add", "-A", cwd=repo)
        _git_ok("commit", "-q", "-m", f"step {i}", cwd=repo)
    _git_ok("checkout", "-q", "integration", cwd=repo)
    before_log = _git_ok("log", "--oneline", cwd=repo).stdout.strip().splitlines()

    mergeq.merge_task(repo, "integration", "swarm/t2", "T2", ["echo ok"])

    after_log = _git_ok("log", "--oneline", cwd=repo).stdout.strip().splitlines()
    assert len(after_log) == len(before_log) + 1  # one squash commit, not three


def test_failing_gate3_does_not_touch_integration(repo, tmp_path):
    _make_work_branch(repo, "swarm/t3", "new.txt", "hello\n")
    conn = db.connect(tmp_path / "ases.db")
    before = _git_ok("rev-parse", "integration", cwd=repo).stdout.strip()

    outcome = mergeq.merge_task(repo, "integration", "swarm/t3", "T3", ["exit 1"], conn=conn)

    assert outcome.merged is False
    assert outcome.gate3_result == "fail"
    after = _git_ok("rev-parse", "integration", cwd=repo).stdout.strip()
    assert after == before  # integration branch untouched
    row = conn.execute("SELECT * FROM merge_records WHERE task_key='T3'").fetchone()
    assert row["gate3_result"] == "fail"


def test_merge_conflict_leaves_integration_untouched(repo):
    # Two branches that both edit base.txt differently.
    _git_ok("checkout", "-q", "-b", "swarm/conflict", cwd=repo)
    (repo / "base.txt").write_text("branch version\n", encoding="utf-8")
    _git_ok("commit", "-aqm", "conflicting edit", cwd=repo)
    _git_ok("checkout", "-q", "integration", cwd=repo)
    (repo / "base.txt").write_text("integration version\n", encoding="utf-8")
    _git_ok("commit", "-aqm", "diverge", cwd=repo)
    before = _git_ok("rev-parse", "integration", cwd=repo).stdout.strip()

    outcome = mergeq.merge_task(repo, "integration", "swarm/conflict", "T4", ["echo ok"])

    assert outcome.merged is False
    assert "conflict" in outcome.detail.lower()
    after = _git_ok("rev-parse", "integration", cwd=repo).stdout.strip()
    assert after == before


def test_candidate_worktree_cleaned_up_after_merge(repo):
    _make_work_branch(repo, "swarm/t5", "new.txt", "x\n")
    mergeq.merge_task(repo, "integration", "swarm/t5", "T5", ["echo ok"])
    result = _git_ok("worktree", "list", cwd=repo)
    # Only the primary checkout should remain registered -- the throwaway candidate worktree
    # (a sibling "candidate" dir under a *different* tmp root) must be torn down, not just any
    # line containing the substring "candidate" (pytest's own tmp_path for *this* test function
    # contains that word, which false-positives a naive substring check on the primary repo line).
    lines = [ln for ln in result.stdout.strip().splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected only the primary checkout, got:\n{result.stdout}"


def test_merge_blocks_on_a_planted_secret(repo, tmp_path):
    _git_ok("checkout", "-q", "-b", "swarm/secret", cwd=repo)
    (repo / "config.py").write_text("API_KEY = 'sk-or-v1-1234567890abcdefghij'\n", encoding="utf-8")
    _git_ok("add", "-A", cwd=repo)
    _git_ok("commit", "-q", "-m", "oops", cwd=repo)
    _git_ok("checkout", "-q", "integration", cwd=repo)
    before = _git_ok("rev-parse", "integration", cwd=repo).stdout.strip()

    outcome = mergeq.merge_task(repo, "integration", "swarm/secret", "T7", ["echo ok"])

    assert outcome.merged is False
    assert "secret scan failed" in outcome.detail
    after = _git_ok("rev-parse", "integration", cwd=repo).stdout.strip()
    assert after == before


def test_revert_merge_creates_a_new_commit(repo):
    _make_work_branch(repo, "swarm/t6", "new.txt", "x\n")
    outcome = mergeq.merge_task(repo, "integration", "swarm/t6", "T6", ["echo ok"])
    before = _git_ok("rev-parse", "integration", cwd=repo).stdout.strip()

    ok = mergeq.revert_merge(repo, outcome.squash_commit)

    assert ok is True
    after = _git_ok("rev-parse", "integration", cwd=repo).stdout.strip()
    assert after != before
    assert not (repo / "new.txt").exists()  # revert undid the file addition
