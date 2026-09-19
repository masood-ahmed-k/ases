import subprocess

import pytest

from ases import db, gates, mergeq


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


# ---------------------------------------------------------------------------------------------
# A refused fast-forward is only a race if the integration tip really moved (2026-09-19 fix), and the
# primary checkout must be ON the integration branch at all. Real git throughout; the one thing faked is
# gates.run_gate, so a commit can be landed inside the window between the candidate and the fast-forward.
# ---------------------------------------------------------------------------------------------

def _racing_gate(repo):
    """A stand-in for gates.run_gate that reports a pass AND, as a side effect, commits a new file onto the
    integration branch in the primary checkout: the deterministic way to put a commit between the
    candidate being built and the fast-forward."""
    def fake(candidate, candidate_sha, gate_name, commands, **kwargs):
        (repo / "injected.txt").write_text("landed while the candidate was being gated\n", encoding="utf-8")
        _git_ok("add", "-A", cwd=repo)
        _git_ok("commit", "-q", "-m", "injected mid-merge", cwd=repo)
        return gates.GateResult(gate_name, candidate_sha, True, "green")
    return fake


def test_ff_refusal_after_the_integration_branch_really_moved_is_a_verified_race(repo, tmp_path, monkeypatch):
    """A REAL race, not a scripted one. merge_task must refuse the fast-forward (never force it), keep
    gate3_result "pass", and say from git that the branch moved: the one outcome the controller retries
    for free."""
    _make_work_branch(repo, "swarm/race", "new.txt", "hello\n")
    conn = db.connect(tmp_path / "ases.db")
    old_tip = _git_ok("rev-parse", "integration", cwd=repo).stdout.strip()
    monkeypatch.setattr(gates, "run_gate", _racing_gate(repo))

    outcome = mergeq.merge_task(repo, "integration", "swarm/race", "T8", ["echo ok"], conn=conn)

    new_tip = _git_ok("rev-parse", "integration", cwd=repo).stdout.strip()
    assert outcome.merged is False
    assert outcome.gate3_result == "pass"
    assert outcome.integration_moved is True
    # Integration holds the injected commit, exactly one on top of where it was, and NOT the candidate.
    assert new_tip != old_tip
    assert _git_ok("rev-parse", "integration^", cwd=repo).stdout.strip() == old_tip
    assert _git_ok("log", "-1", "--format=%s", "integration", cwd=repo).stdout.strip() == "injected mid-merge"
    assert (repo / "injected.txt").exists() and not (repo / "new.txt").exists()
    assert _git("merge-base", "--is-ancestor", outcome.candidate_sha, "integration", cwd=repo).returncode == 1
    # The detail names both tips, so a human reading the merge_failed event or fix card can see the move.
    assert "moved" in outcome.detail and old_tip[:7] in outcome.detail and new_tip[:7] in outcome.detail
    row = conn.execute("SELECT * FROM merge_records WHERE task_key='T8'").fetchone()
    assert row["gate3_result"] == "pass" and row["squash_commit"] is None


def test_ff_refusal_by_a_dirty_primary_checkout_is_not_reported_as_a_race(repo, tmp_path):
    """git also refuses `merge --ff-only` while the integration tip has NOT moved, e.g. when the primary
    checkout holds an uncommitted edit to a file the merge changes. That is not a race and must not look
    like one: integration_moved False, and a detail that says so, followed by git's own text."""
    _make_work_branch(repo, "swarm/dirty", "base.txt", "branch version\n")
    conn = db.connect(tmp_path / "ases.db")
    tip = _git_ok("rev-parse", "integration", cwd=repo).stdout.strip()
    (repo / "base.txt").write_text("uncommitted local edit\n", encoding="utf-8")

    outcome = mergeq.merge_task(repo, "integration", "swarm/dirty", "T9", ["echo ok"], conn=conn)

    assert outcome.merged is False
    # Gate 3 was green on a real candidate, so it was the fast-forward itself that git refused...
    assert outcome.gate3_result == "pass" and outcome.candidate_sha is not None
    # ...with the tip exactly where the candidate was built, so it was not a race.
    assert outcome.integration_moved is False
    assert "did NOT move" in outcome.detail and tip[:7] in outcome.detail
    assert "not a race" in outcome.detail
    assert "base.txt" in outcome.detail  # git's own stderr names the file it refused to overwrite
    assert _git_ok("rev-parse", "integration", cwd=repo).stdout.strip() == tip
    assert (repo / "base.txt").read_text(encoding="utf-8") == "uncommitted local edit\n"  # the edit survived
    row = conn.execute("SELECT * FROM merge_records WHERE task_key='T9'").fetchone()
    assert row["gate3_result"] == "pass" and row["squash_commit"] is None


def test_ff_refusal_git_cannot_confirm_is_treated_as_not_moved(repo, monkeypatch):
    """A free retry is a privilege granted only with evidence. Here the race is real, but looking up the
    integration tip fails the way a bare `git rev-parse` fails (it echoes its argument on stdout AND exits
    non-zero), so there is no evidence and the outcome must say NOT moved."""
    _make_work_branch(repo, "swarm/blind", "new.txt", "hello\n")
    real_git = mergeq._git

    def blind_git(args, cwd, timeout=60):
        if args[0] == "rev-parse" and args[-1] == "integration":
            return subprocess.CompletedProcess(args, 128, stdout="integration\n", stderr="fatal: bad revision\n")
        return real_git(args, cwd, timeout)

    monkeypatch.setattr(mergeq, "_git", blind_git)
    monkeypatch.setattr(gates, "run_gate", _racing_gate(repo))

    outcome = mergeq.merge_task(repo, "integration", "swarm/blind", "T10", ["echo ok"])

    assert outcome.merged is False and outcome.gate3_result == "pass"
    assert outcome.integration_moved is False
    assert "could not confirm" in outcome.detail


@pytest.mark.parametrize("move_head, expected_where", [
    pytest.param(["checkout", "-q", "-b", "elsewhere"], "branch 'elsewhere'", id="other-branch"),
    pytest.param(["checkout", "-q", "--detach"], "a detached HEAD", id="detached-head"),
])
def test_merge_refuses_a_primary_checkout_that_is_not_on_the_integration_branch(
    repo, monkeypatch, move_head, expected_where,
):
    """`git merge --ff-only` advances whatever branch is checked out. With the primary checkout on a branch
    whose tip equals integration's (built here), it would fast-forward THAT branch and merge_task would
    report merged=True while integration never moved. It must refuse up front, before any worktree
    exists, and change nothing."""
    _make_work_branch(repo, "swarm/wb", "new.txt", "hello\n")
    _git_ok(*move_head, cwd=repo)  # HEAD is at integration's tip but no longer ON the integration branch
    tip = _git_ok("rev-parse", "integration", cwd=repo).stdout.strip()
    git_calls = []
    real_git = mergeq._git

    def spy_git(args, cwd, timeout=60):
        git_calls.append(list(args))
        return real_git(args, cwd, timeout)

    monkeypatch.setattr(mergeq, "_git", spy_git)

    outcome = mergeq.merge_task(repo, "integration", "swarm/wb", "T11", ["echo ok"])

    assert outcome.merged is False
    assert outcome.integration_moved is False
    assert outcome.candidate_sha is None and outcome.gate3_result is None
    assert expected_where in outcome.detail and "'integration'" in outcome.detail
    assert "refuses" in outcome.detail
    assert [c for c in git_calls if c[0] == "worktree"] == []  # refused before any worktree was created
    worktrees = [ln for ln in _git_ok("worktree", "list", cwd=repo).stdout.splitlines() if ln.strip()]
    assert len(worktrees) == 1
    assert _git_ok("rev-parse", "integration", cwd=repo).stdout.strip() == tip  # integration untouched
    assert _git_ok("rev-parse", "HEAD", cwd=repo).stdout.strip() == tip  # and so is whatever HEAD sits on
    assert not (repo / "new.txt").exists()
