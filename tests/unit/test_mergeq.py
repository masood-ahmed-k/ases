import pathlib
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone

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


# ---------------------------------------------------------------------------------------------
# An empty diff (2026-09-19). A review-only task never commits, so its branch adds nothing to the
# integration tip: `git merge --squash` exits 0 with nothing staged and `git commit` then fails. Without
# allow_empty that is still the failure it always was; with it, a recorded no-op. Real git throughout.
# ---------------------------------------------------------------------------------------------

def _tip(repo):
    return _git_ok("rev-parse", "integration", cwd=repo).stdout.strip()


def _merge_row(conn, task_key):
    return conn.execute("SELECT * FROM merge_records WHERE task_key = ?", (task_key,)).fetchone()


def _assert_utc_seconds_timestamp(value):
    """completed_at is UTC isoformat at second precision: the same shape on a real merge and on a no-op."""
    stamp = datetime.fromisoformat(value)
    assert stamp.utcoffset() == timedelta(0) and stamp.microsecond == 0
    assert abs(datetime.now(timezone.utc) - stamp) < timedelta(minutes=2)


def _branch_at_tip(repo, branch):
    _git_ok("branch", branch, cwd=repo)


def _branch_behind_tip(repo, branch):
    """Already merged: the branch is an ancestor of the integration tip. The usual shape of a reviewer's
    branch, cut before the earlier tasks landed."""
    _git_ok("branch", branch, cwd=repo)
    (repo / "landed.txt").write_text("landed after the branch was cut\n", encoding="utf-8")
    _git_ok("add", "-A", cwd=repo)
    _git_ok("commit", "-q", "-m", "landed after the branch was cut", cwd=repo)


def _branch_net_zero(repo, branch):
    """Ahead of the tip by two commits whose net effect is nothing (a file added, then removed again). Not
    an ancestor, so git does not say "Already up to date", yet the squash still stages nothing."""
    _make_work_branch(repo, branch, "scratch.txt", "x\n")
    _git_ok("checkout", "-q", branch, cwd=repo)
    _git_ok("rm", "-q", "scratch.txt", cwd=repo)
    _git_ok("commit", "-q", "-m", "remove it again", cwd=repo)
    _git_ok("checkout", "-q", "integration", cwd=repo)


EMPTY_BRANCH_SHAPES = [
    pytest.param(_branch_at_tip, id="at-the-tip"),
    pytest.param(_branch_behind_tip, id="already-merged"),
    pytest.param(_branch_net_zero, id="net-zero-diff"),
]


@pytest.mark.parametrize("make_empty_branch", EMPTY_BRANCH_SHAPES)
def test_empty_branch_with_allow_empty_is_a_recorded_no_op(repo, tmp_path, make_empty_branch):
    make_empty_branch(repo, "swarm/rev")
    conn = db.connect(tmp_path / "ases.db")
    tip = _tip(repo)
    history = _git_ok("log", "--format=%H", "integration", cwd=repo).stdout

    outcome = mergeq.merge_task(repo, "integration", "swarm/rev", "R1", ["echo ok"], conn=conn, allow_empty=True)

    # merged=True with no squash commit is the "no-op merged" shape.
    assert outcome == mergeq.MergeOutcome(True, None, None, "skipped", "no changes to merge (review-only task)")
    assert _tip(repo) == tip  # integration did not move...
    assert _git_ok("log", "--format=%H", "integration", cwd=repo).stdout == history  # ...and gained no commit
    assert _git_ok("status", "--porcelain", cwd=repo).stdout.strip() == ""  # the primary checkout is untouched
    row = _merge_row(conn, "R1")
    assert row["candidate_sha"] == tip  # the integration tip the empty squash was built on
    assert row["gate3_result"] == "skipped"
    assert row["squash_commit"] is None
    assert row["reverted"] == 0
    _assert_utc_seconds_timestamp(row["completed_at"])


def test_recorded_no_op_is_idempotent(repo, tmp_path):
    _branch_at_tip(repo, "swarm/rev")
    conn = db.connect(tmp_path / "ases.db")

    first = mergeq.merge_task(repo, "integration", "swarm/rev", "R2", ["echo ok"], conn=conn, allow_empty=True)
    row_after_first = dict(_merge_row(conn, "R2"))
    second = mergeq.merge_task(repo, "integration", "swarm/rev", "R2", ["echo ok"], conn=conn, allow_empty=True)

    assert first == second and second.merged is True
    assert conn.execute("SELECT COUNT(*) AS n FROM merge_records WHERE task_key = 'R2'").fetchone()["n"] == 1
    row = dict(_merge_row(conn, "R2"))
    assert {k: v for k, v in row.items() if k != "completed_at"} == \
        {k: v for k, v in row_after_first.items() if k != "completed_at"}
    _assert_utc_seconds_timestamp(row["completed_at"])


def test_no_op_overwrites_every_column_it_owns_on_an_existing_row(repo, tmp_path):
    """The ON CONFLICT half of the upsert, not just the insert: a row left behind by an earlier attempt at
    this task (a red Gate 3, never completed) must not keep any stale value once the no-op completes it."""
    _branch_at_tip(repo, "swarm/rev")
    conn = db.connect(tmp_path / "ases.db")
    conn.execute(
        "INSERT INTO merge_records (task_key, candidate_sha, gate3_result, squash_commit, reverted, "
        "completed_at) VALUES ('R3', 'stale-candidate', 'fail', 'stale-squash', 0, NULL)"
    )

    outcome = mergeq.merge_task(repo, "integration", "swarm/rev", "R3", ["echo ok"], conn=conn, allow_empty=True)

    assert outcome.merged is True
    row = _merge_row(conn, "R3")
    assert (row["candidate_sha"], row["gate3_result"], row["squash_commit"]) == (_tip(repo), "skipped", None)
    _assert_utc_seconds_timestamp(row["completed_at"])


@pytest.mark.parametrize("make_empty_branch", EMPTY_BRANCH_SHAPES)
def test_empty_branch_without_allow_empty_is_still_nothing_to_commit(repo, tmp_path, make_empty_branch):
    """The default keeps the failure an empty branch always was: merged=False, "nothing to commit", the
    integration branch untouched, and no merge record marked completed."""
    make_empty_branch(repo, "swarm/none")
    conn = db.connect(tmp_path / "ases.db")
    tip = _tip(repo)

    outcome = mergeq.merge_task(repo, "integration", "swarm/none", "R4", ["echo ok"], conn=conn)

    assert outcome.merged is False
    assert outcome.detail.startswith("nothing to commit:")
    assert (outcome.candidate_sha, outcome.squash_commit, outcome.gate3_result) == (None, None, None)
    assert outcome.integration_moved is False
    assert _tip(repo) == tip
    row = _merge_row(conn, "R4")
    assert row is None or not row["completed_at"]


def test_a_commit_that_fails_with_staged_changes_is_not_labelled_nothing_to_commit(repo, tmp_path, monkeypatch):
    """The changes ARE staged, so this is not an empty diff: a commit that git refuses for another reason (here
    signing is required and the signing program does not exist, the same shape as a rejecting hook or an
    unusable identity) fails the commit. It used to be reported as "nothing to commit"."""
    _make_work_branch(repo, "swarm/t9", "new.txt", "hello\n")
    conn = db.connect(tmp_path / "ases.db")
    before = _tip(repo)
    _git_ok("config", "commit.gpgsign", "true", cwd=repo)
    _git_ok("config", "gpg.program", "ases-test-no-such-signing-program", cwd=repo)

    outcome = mergeq.merge_task(repo, "integration", "swarm/t9", "T9", ["echo gate3-ok"], conn=conn)

    assert outcome.merged is False
    assert outcome.detail.startswith("commit failed:")
    assert "nothing to commit" not in outcome.detail
    assert _tip(repo) == before
    assert _merge_row(conn, "T9") is None


def test_non_empty_branch_with_allow_empty_is_a_normal_squash_merge(repo, tmp_path):
    """allow_empty only decides what an EMPTY squash means. A branch that adds something still gets the
    commit, the secret scan, Gate 3 and the fast-forward."""
    _make_work_branch(repo, "swarm/code", "new.txt", "hello\n")
    conn = db.connect(tmp_path / "ases.db")
    before = _tip(repo)

    outcome = mergeq.merge_task(repo, "integration", "swarm/code", "R5", ["echo ok"], conn=conn, allow_empty=True)

    after = _tip(repo)
    assert outcome.merged is True and outcome.gate3_result == "pass"
    assert outcome.squash_commit == after and outcome.candidate_sha == after
    assert after != before
    assert _git_ok("rev-parse", "integration^", cwd=repo).stdout.strip() == before  # exactly one new commit
    assert (repo / "new.txt").exists()
    row = _merge_row(conn, "R5")
    assert (row["candidate_sha"], row["gate3_result"], row["squash_commit"]) == (after, "pass", after)
    _assert_utc_seconds_timestamp(row["completed_at"])


def test_no_op_skips_gate3_and_the_secret_scan(repo, tmp_path, monkeypatch):
    """There is no candidate diff, so neither runs. The gate command here always fails, so a Gate 3 that
    ran would have turned the no-op into a failure; gate_runs is empty too, since run_gate records every
    run it makes."""
    _branch_at_tip(repo, "swarm/rev")
    conn = db.connect(tmp_path / "ases.db")
    scans = []
    monkeypatch.setattr(gates, "scan_for_secrets", lambda diff: scans.append(diff) or [])

    outcome = mergeq.merge_task(repo, "integration", "swarm/rev", "R6", ["exit 1"], conn=conn, allow_empty=True)

    assert outcome.merged is True and outcome.gate3_result == "skipped"
    assert scans == []
    assert conn.execute("SELECT COUNT(*) AS n FROM gate_runs WHERE task_key = 'R6'").fetchone()["n"] == 0
    # Control: the very same gate command DOES fail a real merge, so the assertions above prove something.
    _make_work_branch(repo, "swarm/code", "new.txt", "hello\n")
    control = mergeq.merge_task(repo, "integration", "swarm/code", "R7", ["exit 1"], conn=conn, allow_empty=True)
    assert control.merged is False and control.gate3_result == "fail"


@pytest.mark.parametrize("code", [128, 129])
def test_a_failed_emptiness_check_is_reported_not_guessed(repo, tmp_path, monkeypatch, code):
    """`git diff --cached --quiet` answers 0 (nothing staged) or 1 (something staged). Any other exit code is
    git failing to answer, and guessing either way is wrong: "empty" would record a merge that never
    happened, "not empty" would commit blind. Real branch is non-empty and allow_empty is on, so both wrong
    guesses would end in merged=True."""
    _make_work_branch(repo, "swarm/unsure", "new.txt", "hello\n")
    conn = db.connect(tmp_path / "ases.db")
    tip = _tip(repo)
    real_git = mergeq._git

    def unsure_git(args, cwd, timeout=60):
        if args[:2] == ["diff", "--cached"]:
            return subprocess.CompletedProcess(args, code, stdout="", stderr="fatal: could not read the index\n")
        return real_git(args, cwd, timeout)

    monkeypatch.setattr(mergeq, "_git", unsure_git)

    outcome = mergeq.merge_task(repo, "integration", "swarm/unsure", "R8", ["echo ok"], conn=conn, allow_empty=True)

    assert outcome.merged is False
    assert (outcome.candidate_sha, outcome.squash_commit, outcome.gate3_result) == (None, None, None)
    assert "could not tell" in outcome.detail and str(code) in outcome.detail
    assert "could not read the index" in outcome.detail  # git's own text is carried through
    assert _tip(repo) == tip
    assert _merge_row(conn, "R8") is None


def test_no_op_leaves_no_worktree_or_temp_dir_behind(repo, monkeypatch):
    """The early return still runs the cleanup in the finally block. Also runs with no db connection, which
    the no-op must tolerate like every other path does."""
    _branch_at_tip(repo, "swarm/rev")
    made = []
    real_mkdtemp = tempfile.mkdtemp

    def spy_mkdtemp(*args, **kwargs):
        made.append(pathlib.Path(real_mkdtemp(*args, **kwargs)))
        return str(made[-1])

    monkeypatch.setattr(mergeq.tempfile, "mkdtemp", spy_mkdtemp)

    outcome = mergeq.merge_task(repo, "integration", "swarm/rev", "R9", ["echo ok"], allow_empty=True)

    assert outcome.merged is True and outcome.squash_commit is None and outcome.gate3_result == "skipped"
    assert made and all(not path.exists() for path in made)  # the throwaway directory is gone
    lines = [ln for ln in _git_ok("worktree", "list", cwd=repo).stdout.splitlines() if ln.strip()]
    assert len(lines) == 1  # only the primary checkout is still registered
