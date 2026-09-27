import dataclasses
import json
import pathlib
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from ases import db, events, gates, guards, hermes, intents, mergeq, reconcile


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


def test_merge_task_records_an_event_when_the_candidate_directory_cannot_be_removed(repo, tmp_path, monkeypatch):
    """Finding 11 (round 12): merge_task's finally block used to discard shutil.rmtree's outcome completely
    (ignore_errors=True, and the git worktree-remove exit code was never even captured), so a Windows file lock
    on the throwaway candidate directory left it on disk with nothing anywhere saying so. Simulated
    deterministically here (a mocked rmtree that leaves the directory in place -- the same observable end state
    a real lock produces), matching test_gates.py's own counterpart for run_gate.

    `shutil` is one module object shared by every importer, so patching `mergeq.shutil.rmtree` also reaches
    gates.run_gate's own cleanup for its nested Gate 3 checkout (merge_task runs Gate 3 inside the candidate
    directory, which is itself a git repo as far as gates.run_gate is concerned): both leaks are real and both
    get cleaned up for real at the end, but only the "ases-merge-" one is this test's own concern."""
    _make_work_branch(repo, "swarm/leak", "new.txt", "x\n")
    conn = db.connect(tmp_path / "ases.db")
    real_rmtree = mergeq.shutil.rmtree
    seen_paths = []

    def fake_rmtree(path, ignore_errors=False):
        seen_paths.append(pathlib.Path(path))

    monkeypatch.setattr(mergeq.shutil, "rmtree", fake_rmtree)

    try:
        outcome = mergeq.merge_task(
            repo, "integration", "swarm/leak", "T-leak", ["echo ok"], conn=conn, project="p1",
        )

        assert outcome.merged is True
        merge_paths = [p for p in seen_paths if "ases-merge-" in p.name]
        assert len(merge_paths) == 1
        rows = [
            json.loads(e["payload"]) for e in events.recent(conn, limit=50) if e["kind"] == "merge_worktree_leak"
        ]
        assert len(rows) == 1
        assert rows[0] == {"task_key": "T-leak", "path": str(merge_paths[0]), "git_exit_code": 0}
    finally:
        for path in seen_paths:  # actually clean up everything the fake rmtree left behind
            real_rmtree(path, ignore_errors=True)


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


# --- round 9, package GITHARDEN: gitexec hardens every git call this module makes -----------------------------

def test_a_planted_post_checkout_hook_does_not_run_during_a_candidate_worktree_add(repo, tmp_path, monkeypatch):
    """Item 1 of the round 8 sweep: _build_candidate's first git call (mergeq._git's `worktree add --detach`)
    used to run with the repository's own (shared) .git/hooks on the path -- a worker who can write there
    (extensions.worktreeConfig is off by default, so every linked worktree shares one .git/hooks) got a
    post-checkout hook run with the controller's own environment on every merge attempt.

    An empty (`_branch_at_tip`) branch with allow_empty=True is used so merge_task takes the no-op path and
    never reaches Gate 3 (gates.run_gate does its OWN separate worktree add, out of this package's files and
    already covered by round 8's own environment scrub, which would otherwise also trip the same hook and make
    this test unable to tell the two apart). `_branch_at_tip` is a bare `git branch`, no checkout, so the only
    checkout _build_candidate's own worktree add performs is the one under test.

    Before/after in one test: the SAME hook, with gitexec.GIT weakened back to a bare "git" (no hooksPath
    override), DOES fire on the very next worktree add, proving the clean result above is the override's doing,
    not a hook that was never wired up."""
    marker = tmp_path / "hook_ran.txt"
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    (hooks_dir / "post-checkout").write_text(f'#!/bin/sh\necho ran > "{marker.as_posix()}"\n', encoding="utf-8")
    (hooks_dir / "post-checkout").chmod(0o755)
    _branch_at_tip(repo, "swarm/hookcheck")
    conn = db.connect(tmp_path / "ases.db")

    outcome = mergeq.merge_task(repo, "integration", "swarm/hookcheck", "T-HOOK", ["echo ok"], conn=conn, allow_empty=True)

    assert outcome.merged is True and outcome.gate3_result == "skipped"  # confirms Gate 3 (gates.py) never ran
    assert not marker.exists(), "a planted post-checkout hook ran during the candidate worktree add"

    monkeypatch.setattr(mergeq.gitexec, "GIT", ("git",))
    _branch_at_tip(repo, "swarm/hookcheck2")
    mergeq.merge_task(repo, "integration", "swarm/hookcheck2", "T-HOOK-2", ["echo ok"], conn=conn, allow_empty=True)
    assert marker.exists(), "fixture problem: the hook should fire once gitexec.GIT is weakened to plain git"


def test_a_planted_textconv_driver_does_not_hide_a_secret_from_the_gate_3_scan(repo, tmp_path):
    """The most important test in package GITHARDEN (round 9): before this package, mergeq's Gate 3 diff (the
    `git diff <base>..<candidate>` in _build_candidate, read by gates.scan_for_secrets) had no
    --no-ext-diff/--no-textconv, unlike tamper.py's equivalent diff. A worker-committed .gitattributes naming a
    textconv driver defined in the repo's own config could make the scanner see innocent, redacted text while the
    real blob still carried the secret -- exactly what this test plants."""
    redact_script = tmp_path / "redact.py"
    redact_script.write_text("import sys\nsys.stdout.write('REDACTED\\n')\n", encoding="utf-8")
    _git_ok("config", "diff.hider.textconv", f'"{sys.executable}" "{redact_script}"', cwd=repo)
    (repo / ".gitattributes").write_text("config.py diff=hider\n", encoding="utf-8")
    _git_ok("add", ".gitattributes", cwd=repo)
    _git_ok("commit", "-q", "-m", "gitattributes", cwd=repo)

    _git_ok("checkout", "-q", "-b", "swarm/textconv-secret", cwd=repo)
    (repo / "config.py").write_text(f"API_KEY = '{SECRET}'\n", encoding="utf-8")
    _git_ok("add", "-A", cwd=repo)
    _git_ok("commit", "-q", "-m", "oops", cwd=repo)
    _git_ok("checkout", "-q", "integration", cwd=repo)
    before = _git_ok("rev-parse", "integration", cwd=repo).stdout.strip()

    # Proof the textconv driver really does hide the secret from a diff that skips --no-textconv: this is the
    # OLD call's shape (mergeq._git's argv before this package, just without gitexec's hooksPath/fsmonitor -c's,
    # which are not what is under test here).
    plain_diff = subprocess.run(
        ["git", "-C", str(repo), "diff", f"{before}..swarm/textconv-secret"], capture_output=True, text=True,
    ).stdout
    assert SECRET not in plain_diff, "fixture problem: the textconv driver should hide the secret without the flag"

    outcome = mergeq.merge_task(repo, "integration", "swarm/textconv-secret", "T-TEXTCONV", ["echo ok"])

    assert outcome.merged is False
    assert "secret scan failed" in outcome.detail
    after = _git_ok("rev-parse", "integration", cwd=repo).stdout.strip()
    assert after == before


def test_revert_merge_creates_a_new_commit(repo):
    _make_work_branch(repo, "swarm/t6", "new.txt", "x\n")
    outcome = mergeq.merge_task(repo, "integration", "swarm/t6", "T6", ["echo ok"])
    before = _git_ok("rev-parse", "integration", cwd=repo).stdout.strip()

    result = mergeq.revert_merge(repo, outcome.squash_commit)

    assert result.ok is True and result.aborted is False
    after = _git_ok("rev-parse", "integration", cwd=repo).stdout.strip()
    assert after != before
    assert result.commit_sha == after  # the new revert commit is the tip now
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


def test_expected_head_merges_exactly_the_checked_commit(repo, tmp_path):
    """The time-of-check gap: the caller gate-ran ONE commit, so that SHA is what gets squashed."""
    _make_work_branch(repo, "swarm/X1", "new.txt", "hello\n")
    checked = _git_ok("rev-parse", "swarm/X1", cwd=repo).stdout.strip()
    conn = db.connect(tmp_path / "ases.db")

    outcome = mergeq.merge_task(repo, "integration", "swarm/X1", "X1", ["echo ok"], conn=conn, expected_head=checked)

    assert outcome.merged is True
    assert (repo / "new.txt").read_text(encoding="utf-8") == "hello\n"


def test_expected_head_refuses_when_the_branch_moved_after_the_checks(repo, tmp_path):
    _make_work_branch(repo, "swarm/X2", "new.txt", "hello\n")
    checked = _git_ok("rev-parse", "swarm/X2", cwd=repo).stdout.strip()
    _git_ok("checkout", "-q", "swarm/X2", cwd=repo)  # a late commit lands on the branch after the checks
    (repo / "late.txt").write_text("unchecked\n", encoding="utf-8")
    _git_ok("add", "-A", cwd=repo)
    _git_ok("commit", "-q", "-m", "late", cwd=repo)
    _git_ok("checkout", "-q", "integration", cwd=repo)
    conn = db.connect(tmp_path / "ases.db")
    before = _tip(repo)

    outcome = mergeq.merge_task(repo, "integration", "swarm/X2", "X2", ["echo ok"], conn=conn, expected_head=checked)

    assert outcome.merged is False
    assert "moved after the pre-merge checks" in outcome.detail and checked[:12] in outcome.detail
    assert _tip(repo) == before and not (repo / "late.txt").exists()
    assert _merge_row(conn, "X2") is None


def test_expected_head_for_an_unresolvable_branch_is_refused(repo, tmp_path):
    conn = db.connect(tmp_path / "ases.db")

    outcome = mergeq.merge_task(repo, "integration", "swarm/nope", "X3", ["echo ok"], conn=conn, expected_head="a" * 40)

    assert outcome.merged is False and "unresolvable" in outcome.detail


def test_without_expected_head_the_branch_name_is_used_as_before(repo, tmp_path):
    _make_work_branch(repo, "swarm/X4", "new.txt", "hello\n")
    conn = db.connect(tmp_path / "ases.db")

    assert mergeq.merge_task(repo, "integration", "swarm/X4", "X4", ["echo ok"], conn=conn).merged is True


def test_the_squash_is_taken_from_the_checked_sha_even_if_the_branch_moves_after_the_head_comparison(
    repo, tmp_path, monkeypatch,
):
    """Belt and braces for the last window: even if the branch moves between the head comparison and the squash,
    the squash comes from the checked SHA, so the late commit cannot ride in. The comparison is faked to see the
    checked head, as it would in that window."""
    _make_work_branch(repo, "swarm/X5", "new.txt", "hello\n")
    checked = _git_ok("rev-parse", "swarm/X5", cwd=repo).stdout.strip()
    _git_ok("checkout", "-q", "swarm/X5", cwd=repo)
    (repo / "late.txt").write_text("unchecked\n", encoding="utf-8")
    _git_ok("add", "-A", cwd=repo)
    _git_ok("commit", "-q", "-m", "late", cwd=repo)
    _git_ok("checkout", "-q", "integration", cwd=repo)
    real_resolve = mergeq._resolve
    monkeypatch.setattr(mergeq, "_resolve", lambda r, rev: checked if rev == "swarm/X5" else real_resolve(r, rev))
    conn = db.connect(tmp_path / "ases.db")

    outcome = mergeq.merge_task(repo, "integration", "swarm/X5", "X5", ["echo ok"], conn=conn, expected_head=checked)

    assert outcome.merged is True
    assert (repo / "new.txt").exists() and not (repo / "late.txt").exists()


# ---------------------------------------------------------------------------------------------
# Round 5. The kill switch between steps (ASES-REC-06), intent records around the steps (ASES-REC-03/04), a new
# candidate starting the merge_records row over (ASES-REC-04), and a secret-free detail (ASES-SEC-01). Real git
# throughout; the fakes are gates.run_gate where a test needs to see inside the window around it, and _git where a
# test needs a git command to fail or to leak.
# ---------------------------------------------------------------------------------------------

SECRET = "sk-or-v1-PLANTEDVALUE0123456789abcd"


def _intent_rows(conn):
    return [dict(r) for r in conn.execute(
        "SELECT project, kind, key, detail, completed_at FROM intents ORDER BY id")]


def _open_intent_kinds(conn):
    return [r["kind"] for r in conn.execute("SELECT kind FROM intents WHERE completed_at IS NULL ORDER BY id")]


def _payloads(conn, kind):
    return [json.loads(r["payload"]) for r in conn.execute(
        "SELECT payload FROM events WHERE kind = ? ORDER BY id", (kind,))]


def _worktree_count(repo):
    return len([ln for ln in _git_ok("worktree", "list", cwd=repo).stdout.splitlines() if ln.strip()])


def _seed_reverted_row(conn, task_key):
    """A merge_records row as an earlier, completed and then reverted merge of the task leaves it."""
    conn.execute(
        "INSERT INTO merge_records (task_key, candidate_sha, gate3_result, squash_commit, reverted, completed_at) "
        "VALUES (?, 'old-candidate', 'pass', 'old-squash', 1, '2026-01-01T00:00:00+00:00')", (task_key,))
    return dict(_merge_row(conn, task_key))


class _Polls:
    """A should_stop that answers from a script (False once the script runs out) and counts how often it was asked."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), 0

    def __call__(self):
        answer = self.answers[self.calls] if self.calls < len(self.answers) else False
        self.calls += 1
        return answer


def _forbid_gate3(monkeypatch):
    attempts = []

    def boom(*args, **kwargs):
        attempts.append(args)
        raise AssertionError("Gate 3 must not run here")

    monkeypatch.setattr(gates, "run_gate", boom)
    return attempts


def _spy_mkdtemp(monkeypatch):
    made = []
    real_mkdtemp = tempfile.mkdtemp

    def spy(*args, **kwargs):
        made.append(pathlib.Path(real_mkdtemp(*args, **kwargs)))
        return str(made[-1])

    monkeypatch.setattr(mergeq.tempfile, "mkdtemp", spy)
    return made


# --- MergeOutcome ---------------------------------------------------------------------------------------------


def test_merge_outcome_gains_a_last_field_stopped_that_defaults_to_false():
    names = [f.name for f in dataclasses.fields(mergeq.MergeOutcome)]
    assert names == ["merged", "candidate_sha", "squash_commit", "gate3_result", "detail", "integration_moved",
                     "stopped"]
    plain = mergeq.MergeOutcome(False, None, None, None, "refused")
    assert plain.stopped is False and plain.integration_moved is False
    with pytest.raises(dataclasses.FrozenInstanceError):
        plain.stopped = True


def test_no_ordinary_outcome_is_marked_stopped(repo, tmp_path):
    _make_work_branch(repo, "swarm/N1", "new.txt", "hello\n")
    conn = db.connect(tmp_path / "ases.db")

    merged = mergeq.merge_task(repo, "integration", "swarm/N1", "N1", ["echo ok"], conn=conn, should_stop=lambda: False)
    refused = mergeq.merge_task(repo, "integration", "swarm/N1", "N1", ["echo ok"], conn=conn)  # already merged

    assert merged.merged is True and merged.stopped is False
    assert refused.merged is False and refused.stopped is False


# --- the stop checkpoints (ASES-REC-06) ----------------------------------------------------------------------


def test_a_stop_before_the_candidate_is_built_builds_nothing(repo, tmp_path, monkeypatch):
    _make_work_branch(repo, "swarm/S1", "new.txt", "hello\n")
    conn = db.connect(tmp_path / "ases.db")
    tip = _tip(repo)
    calls = []
    real_git = mergeq._git

    def spy_git(args, cwd, timeout=60):
        calls.append(list(args))
        return real_git(args, cwd, timeout)

    monkeypatch.setattr(mergeq, "_git", spy_git)
    made = _spy_mkdtemp(monkeypatch)
    attempts = _forbid_gate3(monkeypatch)
    polls = _Polls(True)

    outcome = mergeq.merge_task(
        repo, "integration", "swarm/S1", "S1", ["echo ok"], conn=conn, project="p1", should_stop=polls,
    )

    assert outcome == mergeq.MergeOutcome(
        False, None, None, None, "stopped by the kill switch before building the candidate", stopped=True,
    )
    assert polls.calls == 1
    assert [c for c in calls if c[0] == "worktree"] == [] and made == [] and attempts == []
    assert _tip(repo) == tip and _worktree_count(repo) == 1
    assert _merge_row(conn, "S1") is None
    assert _intent_rows(conn) == []  # nothing had started, so there is nothing to record


def test_a_stop_before_gate3_removes_the_candidate_and_leaves_the_record_as_it_was(repo, tmp_path, monkeypatch):
    _make_work_branch(repo, "swarm/S2", "new.txt", "hello\n")
    conn = db.connect(tmp_path / "ases.db")
    seeded = _seed_reverted_row(conn, "S2")
    tip = _tip(repo)
    made = _spy_mkdtemp(monkeypatch)
    attempts = _forbid_gate3(monkeypatch)
    polls = _Polls(False, True)

    outcome = mergeq.merge_task(
        repo, "integration", "swarm/S2", "S2", ["echo ok"], conn=conn, project="p1", should_stop=polls,
    )

    assert outcome == mergeq.MergeOutcome(False, None, None, None, "stopped by the kill switch before Gate 3", stopped=True)
    assert polls.calls == 2 and attempts == []
    assert _tip(repo) == tip and not (repo / "new.txt").exists()
    assert made and all(not path.exists() for path in made)  # the candidate's temp directory is gone...
    assert _worktree_count(repo) == 1                        # ...and so is its worktree registration
    assert dict(_merge_row(conn, "S2")) == seeded            # not one column of the earlier row changed
    # The stop is a clean end: the build step is closed, so reconcile has nothing to look at.
    assert [(r["kind"], bool(r["completed_at"])) for r in _intent_rows(conn)] == [("build_candidate", True)]


@pytest.mark.parametrize("seed", [True, False], ids=["existing-row", "no-row"])
def test_a_stop_before_the_fast_forward_discards_a_gated_candidate_and_writes_no_row(repo, tmp_path, seed):
    """Gate 3 ran and was green, so the temptation is to record it. It is not recorded: the row is only written
    after the last checkpoint, so a stop here leaves no half row (a pass with no squash commit) behind."""
    _make_work_branch(repo, "swarm/S3", "new.txt", "hello\n")
    conn = db.connect(tmp_path / "ases.db")
    seeded = _seed_reverted_row(conn, "S3") if seed else None
    tip = _tip(repo)
    polls = _Polls(False, False, True)

    outcome = mergeq.merge_task(
        repo, "integration", "swarm/S3", "S3", ["echo ok"], conn=conn, project="p1", should_stop=polls,
    )

    assert outcome == mergeq.MergeOutcome(
        False, None, None, None, "stopped by the kill switch before the fast-forward", stopped=True,
    )
    assert polls.calls == 3
    assert _tip(repo) == tip and not (repo / "new.txt").exists() and _worktree_count(repo) == 1
    row = _merge_row(conn, "S3")
    assert (dict(row) if row is not None else None) == seeded
    # Gate 3 really ran (its own record, in gate_runs, is the gate's and stays), and only the build step was opened.
    assert conn.execute("SELECT gate, result FROM gate_runs WHERE task_key = 'S3'").fetchall()[0]["result"] == "pass"
    assert [(r["kind"], bool(r["completed_at"])) for r in _intent_rows(conn)] == [("build_candidate", True)]


def test_the_checkpoints_are_polled_before_the_candidate_before_gate3_and_before_the_fast_forward(
    repo, tmp_path, monkeypatch,
):
    _make_work_branch(repo, "swarm/S4", "new.txt", "hello\n")
    conn = db.connect(tmp_path / "ases.db")
    log = []
    real_git = mergeq._git

    def spy_git(args, cwd, timeout=60):
        if args[:2] == ["worktree", "add"]:
            log.append("candidate")
        if args[:2] == ["merge", "--ff-only"]:
            log.append("fast-forward")
        return real_git(args, cwd, timeout)

    def fake_gate(candidate, sha, name, commands, **kwargs):
        log.append("gate3")
        return gates.GateResult(name, sha, True, "green")

    def poll():
        log.append("poll")
        return False

    monkeypatch.setattr(mergeq, "_git", spy_git)
    monkeypatch.setattr(gates, "run_gate", fake_gate)

    outcome = mergeq.merge_task(repo, "integration", "swarm/S4", "S4", ["echo ok"], conn=conn, should_stop=poll)

    assert outcome.merged is True and outcome.stopped is False
    assert log == ["poll", "candidate", "poll", "gate3", "poll", "fast-forward"]


def test_a_red_gate3_is_not_asked_about_the_fast_forward_and_still_writes_its_record(repo, tmp_path):
    """There is no fast-forward left to stop after a red gate, so the third checkpoint is never reached, and the
    failure is what it always was: merged False, not stopped, with the fail recorded."""
    _make_work_branch(repo, "swarm/S5", "new.txt", "hello\n")
    conn = db.connect(tmp_path / "ases.db")
    polls = _Polls(False, False, True)

    outcome = mergeq.merge_task(repo, "integration", "swarm/S5", "S5", ["exit 1"], conn=conn, should_stop=polls)

    assert outcome.merged is False and outcome.stopped is False and outcome.gate3_result == "fail"
    assert polls.calls == 2
    assert _merge_row(conn, "S5")["gate3_result"] == "fail"


@pytest.mark.parametrize("answer", [True, 1, "yes"])
def test_any_true_ish_answer_from_should_stop_is_a_stop(repo, tmp_path, answer):
    _make_work_branch(repo, "swarm/S6", "new.txt", "hello\n")
    conn = db.connect(tmp_path / "ases.db")

    outcome = mergeq.merge_task(repo, "integration", "swarm/S6", "S6", ["echo ok"], conn=conn,
                                should_stop=lambda: answer)

    assert outcome.stopped is True and outcome.merged is False


@pytest.mark.parametrize("answer", [False, 0, None, ""])
def test_any_false_ish_answer_from_should_stop_lets_the_merge_go_ahead(repo, tmp_path, answer):
    _make_work_branch(repo, "swarm/S7", "new.txt", "hello\n")
    conn = db.connect(tmp_path / "ases.db")

    outcome = mergeq.merge_task(repo, "integration", "swarm/S7", "S7", ["echo ok"], conn=conn,
                                should_stop=lambda: answer)

    assert outcome.merged is True and outcome.stopped is False


def test_a_should_stop_that_raises_is_treated_as_false_and_recorded_as_an_event(repo, tmp_path):
    _make_work_branch(repo, "swarm/S8", "new.txt", "hello\n")
    conn = db.connect(tmp_path / "ases.db")

    def broken():
        raise RuntimeError("kill switch state unreadable")

    outcome = mergeq.merge_task(repo, "integration", "swarm/S8", "S8", ["echo ok"], conn=conn, should_stop=broken)

    assert outcome.merged is True and outcome.stopped is False  # a broken reader does not block merging
    events = _payloads(conn, "should_stop_error")
    assert [e["step"] for e in events] == ["building the candidate", "Gate 3", "the fast-forward"]
    assert all(e["task_key"] == "S8" for e in events)
    assert all(e["error"] == "RuntimeError: kill switch state unreadable" for e in events)


def test_a_should_stop_that_raises_records_its_project(repo, tmp_path):
    """events.py package, round 9: every _stop_requested call site is inside merge_task, which already has
    `project` in scope, so should_stop_error carries it too instead of leaving the column NULL."""
    _make_work_branch(repo, "swarm/S8B", "new.txt", "hello\n")
    conn = db.connect(tmp_path / "ases.db")

    def broken():
        raise RuntimeError("kill switch state unreadable")

    mergeq.merge_task(
        repo, "integration", "swarm/S8B", "S8B", ["echo ok"], conn=conn, should_stop=broken, project="proj-c",
    )

    rows = conn.execute("SELECT project FROM events WHERE kind = 'should_stop_error'").fetchall()
    assert rows and all(r["project"] == "proj-c" for r in rows)


def test_a_should_stop_that_raises_with_no_connection_still_lets_the_merge_go_ahead(repo):
    _make_work_branch(repo, "swarm/S9", "new.txt", "hello\n")

    def broken():
        raise RuntimeError("boom")

    assert mergeq.merge_task(repo, "integration", "swarm/S9", "S9", ["echo ok"], should_stop=broken).merged is True


def test_the_stop_wins_over_a_refusal_the_merge_would_otherwise_report(repo, tmp_path):
    """A halted project must get "stopped", not a failure that would open a fix card: here the primary checkout
    is on the wrong branch, which is a refusal when nothing is asking for a stop."""
    _make_work_branch(repo, "swarm/S10", "new.txt", "hello\n")
    _git_ok("checkout", "-q", "-b", "elsewhere", cwd=repo)
    conn = db.connect(tmp_path / "ases.db")

    stopped = mergeq.merge_task(repo, "integration", "swarm/S10", "S10", ["echo ok"], conn=conn,
                                should_stop=lambda: True)
    refused = mergeq.merge_task(repo, "integration", "swarm/S10", "S10", ["echo ok"], conn=conn)

    assert stopped.stopped is True and "refuses" not in stopped.detail
    assert refused.stopped is False and "refuses" in refused.detail


# --- intent records around the steps (ASES-REC-03/04) ----------------------------------------------------------


def test_a_merge_with_a_project_writes_an_intent_around_the_build_and_gate3_and_another_around_the_fast_forward(
    repo, tmp_path, monkeypatch,
):
    _make_work_branch(repo, "swarm/I1", "new.txt", "hello\n")
    conn = db.connect(tmp_path / "ases.db")
    seen = {}
    real_git = mergeq._git

    def fake_gate(candidate, sha, name, commands, **kwargs):
        seen["at_gate3"] = _open_intent_kinds(conn)
        return gates.GateResult(name, sha, True, "green")

    def spy_git(args, cwd, timeout=60):
        if args[:2] == ["merge", "--ff-only"]:
            seen["at_fast_forward"] = _open_intent_kinds(conn)
        return real_git(args, cwd, timeout)

    monkeypatch.setattr(gates, "run_gate", fake_gate)
    monkeypatch.setattr(mergeq, "_git", spy_git)

    outcome = mergeq.merge_task(repo, "integration", "swarm/I1", "I1", ["echo ok"], conn=conn, project="p1")

    assert outcome.merged is True
    # Open while the step runs, and only that step: the build's intent is closed before the fast-forward opens its own.
    assert seen == {"at_gate3": ["build_candidate"], "at_fast_forward": ["fast_forward"]}
    rows = _intent_rows(conn)
    assert [(r["project"], r["kind"], r["key"]) for r in rows] == [
        ("p1", "build_candidate", "I1"), ("p1", "fast_forward", "I1"),
    ]
    assert all(r["completed_at"] for r in rows)
    assert "swarm/I1" in rows[0]["detail"] and outcome.candidate_sha in rows[1]["detail"]
    assert intents.open_intents(conn, "p1") == []


def test_no_intent_is_written_without_a_project(repo, tmp_path):
    _make_work_branch(repo, "swarm/I2", "new.txt", "hello\n")
    conn = db.connect(tmp_path / "ases.db")

    assert mergeq.merge_task(repo, "integration", "swarm/I2", "I2", ["echo ok"], conn=conn).merged is True

    assert _intent_rows(conn) == []


def test_a_project_without_a_connection_merges_and_has_nowhere_to_write_an_intent(repo):
    _make_work_branch(repo, "swarm/I3", "new.txt", "hello\n")

    assert mergeq.merge_task(repo, "integration", "swarm/I3", "I3", ["echo ok"], project="p1").merged is True


def test_a_gate3_that_raises_leaves_the_build_intent_open_and_still_cleans_up(repo, tmp_path, monkeypatch):
    _make_work_branch(repo, "swarm/I4", "new.txt", "hello\n")
    conn = db.connect(tmp_path / "ases.db")
    tip = _tip(repo)
    made = _spy_mkdtemp(monkeypatch)

    def exploding(*args, **kwargs):
        raise RuntimeError("docker daemon is not running")

    monkeypatch.setattr(gates, "run_gate", exploding)

    with pytest.raises(RuntimeError, match="docker daemon"):
        mergeq.merge_task(repo, "integration", "swarm/I4", "I4", ["echo ok"], conn=conn, project="p1")

    # The step died half way, which is exactly what an open intent is for: reconcile reads it back by kind and key.
    open_now = intents.open_intents(conn, "p1")
    assert [(i["kind"], i["key"]) for i in open_now] == [("build_candidate", "I4")]
    assert _tip(repo) == tip and _merge_row(conn, "I4") is None
    assert made and all(not path.exists() for path in made) and _worktree_count(repo) == 1


def test_a_fast_forward_that_raises_leaves_its_intent_open_and_the_build_intent_completed(repo, tmp_path, monkeypatch):
    _make_work_branch(repo, "swarm/I5", "new.txt", "hello\n")
    conn = db.connect(tmp_path / "ases.db")
    real_git = mergeq._git

    def dying_git(args, cwd, timeout=60):
        if args[:2] == ["merge", "--ff-only"]:
            raise subprocess.TimeoutExpired(args, timeout)
        return real_git(args, cwd, timeout)

    monkeypatch.setattr(mergeq, "_git", dying_git)

    with pytest.raises(subprocess.TimeoutExpired):
        mergeq.merge_task(repo, "integration", "swarm/I5", "I5", ["echo ok"], conn=conn, project="p1")

    assert [(r["kind"], bool(r["completed_at"])) for r in _intent_rows(conn)] == [
        ("build_candidate", True), ("fast_forward", False),
    ]
    row = _merge_row(conn, "I5")  # the gated candidate is on record, never completed: the shape reconcile redoes
    assert row["gate3_result"] == "pass" and row["squash_commit"] is None and row["completed_at"] is None


@pytest.mark.parametrize("scenario", ["red-gate", "conflict", "empty-diff", "refused-fast-forward"])
def test_a_step_that_ends_in_a_refusal_has_finished_so_its_intents_are_completed(repo, tmp_path, monkeypatch, scenario):
    """Only an exception or a crash leaves an intent open. A refusal is a clean end of the step: the state is
    consistent and the caller has the outcome, so nothing is left for reconcile-on-start to look at."""
    conn = db.connect(tmp_path / "ases.db")
    gate = ["echo ok"]
    if scenario == "red-gate":
        _make_work_branch(repo, "swarm/I6", "new.txt", "hello\n")
        gate = ["exit 1"]
    elif scenario == "conflict":
        _git_ok("checkout", "-q", "-b", "swarm/I6", cwd=repo)
        (repo / "base.txt").write_text("branch version\n", encoding="utf-8")
        _git_ok("commit", "-aqm", "conflicting edit", cwd=repo)
        _git_ok("checkout", "-q", "integration", cwd=repo)
        (repo / "base.txt").write_text("integration version\n", encoding="utf-8")
        _git_ok("commit", "-aqm", "diverge", cwd=repo)
    elif scenario == "empty-diff":
        _branch_at_tip(repo, "swarm/I6")
    else:
        _make_work_branch(repo, "swarm/I6", "new.txt", "hello\n")
        monkeypatch.setattr(gates, "run_gate", _racing_gate(repo))

    outcome = mergeq.merge_task(repo, "integration", "swarm/I6", "I6", gate, conn=conn, project="p1")

    assert outcome.merged is False
    rows = _intent_rows(conn)
    assert rows and all(r["completed_at"] for r in rows)
    assert intents.open_intents(conn, "p1") == []
    if scenario == "refused-fast-forward":
        assert [r["kind"] for r in rows] == ["build_candidate", "fast_forward"]
    else:
        assert [r["kind"] for r in rows] == ["build_candidate"]


def test_a_recorded_no_op_is_inside_the_build_intent_and_completes_it(repo, tmp_path):
    _branch_at_tip(repo, "swarm/I7")
    conn = db.connect(tmp_path / "ases.db")

    outcome = mergeq.merge_task(repo, "integration", "swarm/I7", "I7", ["echo ok"], conn=conn, project="p1",
                                allow_empty=True)

    assert outcome.merged is True and outcome.squash_commit is None
    assert [(r["kind"], bool(r["completed_at"])) for r in _intent_rows(conn)] == [("build_candidate", True)]


def test_the_early_refusals_open_no_intent(repo, tmp_path):
    """A wrong checkout and a moved branch are refused before anything is built, so there is no step to record."""
    _make_work_branch(repo, "swarm/I8", "new.txt", "hello\n")
    conn = db.connect(tmp_path / "ases.db")

    moved = mergeq.merge_task(repo, "integration", "swarm/I8", "I8", ["echo ok"], conn=conn, project="p1",
                              expected_head="0" * 40)
    _git_ok("checkout", "-q", "-b", "elsewhere", cwd=repo)
    wrong = mergeq.merge_task(repo, "integration", "swarm/I8", "I8", ["echo ok"], conn=conn, project="p1")

    assert moved.merged is False and wrong.merged is False
    assert _intent_rows(conn) == []


# --- revert_merge and its intent --------------------------------------------------------------------------------


def test_revert_merge_writes_a_revert_intent_around_git_revert_and_completes_it(repo, tmp_path, monkeypatch):
    _make_work_branch(repo, "swarm/V1", "new.txt", "x\n")
    conn = db.connect(tmp_path / "ases.db")
    merged = mergeq.merge_task(repo, "integration", "swarm/V1", "V1", ["echo ok"], conn=conn)
    seen = {}
    real_git = mergeq._git

    def spy_git(args, cwd, timeout=60):
        if args[0] == "revert":
            seen["open"] = _open_intent_kinds(conn)
        return real_git(args, cwd, timeout)

    monkeypatch.setattr(mergeq, "_git", spy_git)

    result = mergeq.revert_merge(repo, merged.squash_commit, conn=conn, task_key="V1", project="p1")

    assert result.ok is True and seen["open"] == ["revert"]
    rows = _intent_rows(conn)
    assert [(r["project"], r["kind"], r["key"]) for r in rows] == [("p1", "revert", "V1")]
    assert rows[0]["completed_at"] and merged.squash_commit in rows[0]["detail"]
    assert _merge_row(conn, "V1")["reverted"] == 1
    assert not (repo / "new.txt").exists()


def test_revert_merge_leaves_its_intent_open_when_git_raises(repo, tmp_path, monkeypatch):
    _make_work_branch(repo, "swarm/V2", "new.txt", "x\n")
    conn = db.connect(tmp_path / "ases.db")
    merged = mergeq.merge_task(repo, "integration", "swarm/V2", "V2", ["echo ok"], conn=conn)
    real_git = mergeq._git

    def dying_git(args, cwd, timeout=60):
        if args[0] == "revert":
            raise subprocess.TimeoutExpired(args, timeout)
        return real_git(args, cwd, timeout)

    monkeypatch.setattr(mergeq, "_git", dying_git)

    with pytest.raises(subprocess.TimeoutExpired):
        mergeq.revert_merge(repo, merged.squash_commit, conn=conn, task_key="V2", project="p1")

    open_now = intents.open_intents(conn, "p1")
    assert [(i["kind"], i["key"]) for i in open_now] == [("revert", "V2")]
    assert _merge_row(conn, "V2")["reverted"] == 0  # the record update never ran: reconcile settles it from git


def test_revert_merge_writes_no_intent_without_a_project_or_a_connection(repo, tmp_path):
    _make_work_branch(repo, "swarm/V3", "a.txt", "x\n")
    _make_work_branch(repo, "swarm/V4", "b.txt", "y\n")
    conn = db.connect(tmp_path / "ases.db")
    first = mergeq.merge_task(repo, "integration", "swarm/V3", "V3", ["echo ok"], conn=conn)
    second = mergeq.merge_task(repo, "integration", "swarm/V4", "V4", ["echo ok"], conn=conn)

    assert mergeq.revert_merge(repo, second.squash_commit, conn=conn, task_key="V4").ok is True   # a connection only
    assert mergeq.revert_merge(repo, first.squash_commit, project="p1").ok is True                # a project only

    assert _intent_rows(conn) == []
    assert _merge_row(conn, "V4")["reverted"] == 1 and _merge_row(conn, "V3")["reverted"] == 0


# --- round 6: revert_merge's fixed return value, and project-scoping (schema v7) ---------------------------------


def test_revert_merge_marks_reverted_only_when_git_revert_actually_succeeds(repo, tmp_path):
    """The MR builder's finding: the old bool return let a failed git revert still mark reverted=1. Reverting the
    commit that ADDED new.txt, after a later commit has modified that same file, is a real conflict (modify/delete):
    git revert cannot apply it, and neither should the database record a revert that never happened."""
    _make_work_branch(repo, "swarm/G1", "new.txt", "v1\n")
    conn = db.connect(tmp_path / "ases.db")
    first = mergeq.merge_task(repo, "integration", "swarm/G1", "G1", ["echo ok"], conn=conn)
    (repo / "new.txt").write_text("v2, a later unrelated change\n", encoding="utf-8")
    _git_ok("commit", "-aqm", "a later change to the same file", cwd=repo)
    before = _git_ok("rev-parse", "integration", cwd=repo).stdout.strip()

    result = mergeq.revert_merge(repo, first.squash_commit, conn=conn, task_key="G1")

    assert result.ok is False and result.commit_sha is None
    assert result.aborted is True  # git revert --abort recovered the checkout
    after = _git_ok("rev-parse", "integration", cwd=repo).stdout.strip()
    assert after == before  # the abort left HEAD exactly where it was, not mid-revert
    assert _git_ok("status", "--porcelain", cwd=repo).stdout == ""  # clean, no conflict markers left behind
    assert _merge_row(conn, "G1")["reverted"] == 0  # the fix: never set on a failed revert


def test_revert_merge_with_a_mismatched_project_does_not_touch_another_projects_row(repo, tmp_path):
    """The NULL-tolerant scoping (schema v7, the smaller, safer fix mergeq.py's module docstring explains in
    place of a merge_records primary-key migration): a revert call under the WRONG project must not silently
    flip reverted=1 on a row a different project's more recent candidate build has since stamped."""
    _make_work_branch(repo, "swarm/S1", "new.txt", "x\n")
    conn = db.connect(tmp_path / "ases.db")
    merged = mergeq.merge_task(repo, "integration", "swarm/S1", "S1", ["echo ok"], conn=conn, project="p1")

    result = mergeq.revert_merge(repo, merged.squash_commit, conn=conn, task_key="S1", project="p2")

    assert result.ok is True  # git itself reverted the commit...
    row = _merge_row(conn, "S1")
    assert row["reverted"] == 0 and row["project"] == "p1"  # ...but p1's row was never touched


def test_revert_merge_still_matches_a_legacy_null_project_row(repo, tmp_path):
    """A row with no project (written before schema v7, or by a caller that has not been updated to pass one)
    still counts: never orphan history."""
    _make_work_branch(repo, "swarm/S2", "new.txt", "x\n")
    conn = db.connect(tmp_path / "ases.db")
    merged = mergeq.merge_task(repo, "integration", "swarm/S2", "S2", ["echo ok"], conn=conn)  # no project

    result = mergeq.revert_merge(repo, merged.squash_commit, conn=conn, task_key="S2", project="p1")

    assert result.ok is True and _merge_row(conn, "S2")["reverted"] == 1


def test_merge_task_stamps_the_project_column_on_merge_records_and_gate_runs(repo, tmp_path):
    _make_work_branch(repo, "swarm/S3", "new.txt", "x\n")
    conn = db.connect(tmp_path / "ases.db")

    mergeq.merge_task(repo, "integration", "swarm/S3", "S3", ["echo ok"], conn=conn, project="p9")

    assert _merge_row(conn, "S3")["project"] == "p9"
    gate_row = conn.execute(
        "SELECT project FROM gate_runs WHERE task_key = 'S3' AND gate = 'gate3'"
    ).fetchone()
    assert gate_row["project"] == "p9"


def test_merge_task_with_no_project_leaves_the_project_column_null_as_before(repo, tmp_path):
    _make_work_branch(repo, "swarm/S5", "new.txt", "x\n")
    conn = db.connect(tmp_path / "ases.db")

    mergeq.merge_task(repo, "integration", "swarm/S5", "S5", ["echo ok"], conn=conn)

    assert _merge_row(conn, "S5")["project"] is None


def test_post_merge_check_mechanism_end_to_end_on_a_real_repo(repo, tmp_path):
    """The mechanism controller.process_merge_queue's post-merge check and revert are built from (ASES-GIT-05,
    section 8.1: "the queue reverts the squash commit, records it"), exercised directly with real git and no
    controller involved: a task's merge (A2) lands, a second, unrelated commit (standing in for a second task's
    merge) breaks a shared invariant A2's own gate also checks, the post-merge re-check (a fresh gates.run_gate
    call on the new tip) goes red, and revert_merge repairs the branch cleanly because the two changes touch
    different files and so do not conflict (see the conflicting case above for --abort)."""
    (repo / "config.txt").write_text("enabled\n", encoding="utf-8")
    _git_ok("add", "-A", cwd=repo)
    _git_ok("commit", "-q", "-m", "project config", cwd=repo)
    gate_cmd = ["grep -q safe guarded.txt", "grep -q enabled config.txt"]
    conn = db.connect(tmp_path / "ases.db")

    _make_work_branch(repo, "swarm/A2", "guarded.txt", "safe\n")
    task_a = mergeq.merge_task(repo, "integration", "swarm/A2", "A2", gate_cmd, conn=conn, project="p1")
    assert task_a.merged is True and task_a.gate3_result == "pass"

    # A second, unrelated task's merge lands directly (standing in for process_merge_queue merging a DIFFERENT
    # task next): it never touches guarded.txt, A2's own file, but breaks the shared invariant A2's gate also
    # checks -- exactly "a project-level regression another task's merge introduced ... on a file THIS task's
    # touches never named".
    (repo / "config.txt").write_text("disabled\n", encoding="utf-8")
    _git_ok("commit", "-aqm", "task B: disable config (unrelated to A2's own file)", cwd=repo)
    new_head = _git_ok("rev-parse", "integration", cwd=repo).stdout.strip()

    postcheck = gates.run_gate(repo, new_head, "gate3-postmerge", gate_cmd, conn=conn, task_key="A2", project="p1")
    assert postcheck.passed is False  # what A2's own Gate 3 would have caught, now broken underneath it

    result = mergeq.revert_merge(repo, task_a.squash_commit, conn=conn, task_key="A2", project="p1")

    assert result.ok is True and result.aborted is False  # different files: a clean, non-conflicting revert
    assert _merge_row(conn, "A2")["reverted"] == 1
    assert not (repo / "guarded.txt").exists()             # A2's own contribution is gone
    assert (repo / "config.txt").read_text(encoding="utf-8") == "disabled\n"  # task B's change is left alone


# --- a new candidate starts the merge_records row over (ASES-REC-04) --------------------------------------------


def test_a_new_candidate_after_a_revert_resets_reverted_squash_commit_and_completed_at(repo, tmp_path):
    _make_work_branch(repo, "swarm/F1", "new.txt", "v1\n")
    conn = db.connect(tmp_path / "ases.db")
    first = mergeq.merge_task(repo, "integration", "swarm/F1", "F1", ["echo ok"], conn=conn)
    assert first.merged is True
    assert mergeq.revert_merge(repo, first.squash_commit, conn=conn, task_key="F1").ok is True
    reverted = _merge_row(conn, "F1")
    assert (reverted["reverted"], reverted["squash_commit"]) == (1, first.squash_commit) and reverted["completed_at"]

    _make_work_branch(repo, "swarm/F1-fix1", "new.txt", "v2\n")  # the fix card's branch, cut from the reverted tip
    second = mergeq.merge_task(repo, "integration", "swarm/F1-fix1", "F1", ["echo ok"], conn=conn)

    assert second.merged is True and second.squash_commit != first.squash_commit
    row = _merge_row(conn, "F1")
    assert row["reverted"] == 0                                # the bug: this stayed 1 forever
    assert (row["candidate_sha"], row["squash_commit"], row["gate3_result"]) == (
        second.candidate_sha, second.squash_commit, "pass")
    _assert_utc_seconds_timestamp(row["completed_at"])
    assert (repo / "new.txt").read_text(encoding="utf-8") == "v2\n"


def test_a_re_merged_task_no_longer_reads_as_done_but_reverted_to_reconcile(repo, tmp_path, monkeypatch):
    """The symptom the reconcile builder found: reconcile.check() reported done_but_reverted for a healthy merge."""
    _make_work_branch(repo, "swarm/F2", "new.txt", "v1\n")
    conn = db.connect(tmp_path / "ases.db")
    conn.execute(
        "INSERT INTO plan_tasks (project, task_key, work_card_id, merge_card_id, role, created_at) "
        "VALUES ('p1', 'F2', 'w_F2', 'm_F2', 'coder', datetime('now'))")
    monkeypatch.setattr(hermes, "kanban_show", lambda board, card_id: {"id": card_id, "status": "done"})
    first = mergeq.merge_task(repo, "integration", "swarm/F2", "F2", ["echo ok"], conn=conn)
    mergeq.revert_merge(repo, first.squash_commit, conn=conn, task_key="F2")
    # Control: with the row as the revert left it and the card done, reconcile does see the problem.
    assert [f.kind for f in reconcile.check("b", "p1", conn=conn)] == ["done_but_reverted"]

    _make_work_branch(repo, "swarm/F2-fix1", "new.txt", "v2\n")
    assert mergeq.merge_task(repo, "integration", "swarm/F2-fix1", "F2", ["echo ok"], conn=conn).merged is True

    assert reconcile.check("b", "p1", conn=conn) == []


def test_a_red_gate3_on_a_new_candidate_also_starts_the_row_over(repo, tmp_path):
    _make_work_branch(repo, "swarm/F3", "new.txt", "v1\n")
    conn = db.connect(tmp_path / "ases.db")
    first = mergeq.merge_task(repo, "integration", "swarm/F3", "F3", ["echo ok"], conn=conn)
    mergeq.revert_merge(repo, first.squash_commit, conn=conn, task_key="F3")
    _make_work_branch(repo, "swarm/F3-fix1", "new.txt", "v2\n")

    outcome = mergeq.merge_task(repo, "integration", "swarm/F3-fix1", "F3", ["exit 1"], conn=conn)

    assert outcome.merged is False and outcome.gate3_result == "fail"
    row = _merge_row(conn, "F3")
    assert (row["gate3_result"], row["candidate_sha"]) == ("fail", outcome.candidate_sha)
    assert (row["reverted"], row["squash_commit"], row["completed_at"]) == (0, None, None)


def test_a_recorded_no_op_also_starts_the_row_over(repo, tmp_path):
    _branch_at_tip(repo, "swarm/F4")
    conn = db.connect(tmp_path / "ases.db")
    _seed_reverted_row(conn, "F4")

    outcome = mergeq.merge_task(repo, "integration", "swarm/F4", "F4", ["echo ok"], conn=conn, allow_empty=True)

    assert outcome.merged is True and outcome.squash_commit is None
    row = _merge_row(conn, "F4")
    assert (row["reverted"], row["squash_commit"], row["gate3_result"]) == (0, None, "skipped")
    _assert_utc_seconds_timestamp(row["completed_at"])


def test_a_call_that_ends_before_a_new_candidate_exists_leaves_a_completed_row_untouched(repo, tmp_path):
    """Only a new candidate build resets the row. Every call below ends before one exists (or, for the secret, before
    Gate 3), and the row of the task's earlier, completed, NOT reverted merge must come out of each unchanged."""
    _make_work_branch(repo, "swarm/U1", "new.txt", "hello\n")
    conn = db.connect(tmp_path / "ases.db")
    first = mergeq.merge_task(repo, "integration", "swarm/U1", "U1", ["echo ok"], conn=conn)
    assert first.merged is True
    before = dict(_merge_row(conn, "U1"))
    assert before["reverted"] == 0 and before["squash_commit"] and before["completed_at"]

    root = _git_ok("rev-list", "--max-parents=0", "HEAD", cwd=repo).stdout.strip()
    _git_ok("checkout", "-q", "-b", "swarm/U1-conflict", root, cwd=repo)
    (repo / "new.txt").write_text("a different new.txt\n", encoding="utf-8")
    _git_ok("add", "-A", cwd=repo)
    _git_ok("commit", "-q", "-m", "conflicting add", cwd=repo)
    _git_ok("checkout", "-q", "integration", cwd=repo)
    _make_work_branch(repo, "swarm/U1-secret", "config.py", f"API_KEY = '{SECRET}'\n")

    calls = {
        "already merged (nothing to commit)": lambda: mergeq.merge_task(
            repo, "integration", "swarm/U1", "U1", ["echo ok"], conn=conn),
        "a conflict": lambda: mergeq.merge_task(
            repo, "integration", "swarm/U1-conflict", "U1", ["echo ok"], conn=conn),
        "a planted secret": lambda: mergeq.merge_task(
            repo, "integration", "swarm/U1-secret", "U1", ["echo ok"], conn=conn),
        "the branch moved after the checks": lambda: mergeq.merge_task(
            repo, "integration", "swarm/U1", "U1", ["echo ok"], conn=conn, expected_head="0" * 40),
        "a stop before the candidate": lambda: mergeq.merge_task(
            repo, "integration", "swarm/U1", "U1", ["echo ok"], conn=conn, should_stop=lambda: True),
    }
    for label, call in calls.items():
        outcome = call()
        assert outcome.merged is False, label
        assert dict(_merge_row(conn, "U1")) == before, label

    _git_ok("checkout", "-q", "-b", "elsewhere", cwd=repo)  # and a primary checkout that is on the wrong branch
    assert mergeq.merge_task(repo, "integration", "swarm/U1", "U1", ["echo ok"], conn=conn).merged is False
    assert dict(_merge_row(conn, "U1")) == before


# --- a secret-free detail (ASES-SEC-01) -------------------------------------------------------------------------


def test_a_merge_outcome_redacts_secret_shaped_text_in_its_detail_when_it_is_built():
    outcome = mergeq.MergeOutcome(False, None, None, None, f"gate said: key={SECRET} and more")

    assert SECRET not in outcome.detail and "PLANTEDVALUE" not in outcome.detail
    assert outcome.detail == "gate said: key=[redacted] and more"
    clean = mergeq.MergeOutcome(True, "a" * 40, "a" * 40, "pass", "merged")
    assert clean.detail == "merged"
    assert mergeq.MergeOutcome(False, None, None, None, None).detail is None  # not text: nothing to redact, no crash


def test_a_failed_commit_never_carries_a_secret_from_gits_output_into_the_outcome(repo, tmp_path, monkeypatch):
    _make_work_branch(repo, "swarm/R1", "new.txt", "hello\n")
    conn = db.connect(tmp_path / "ases.db")
    real_git = mergeq._git

    def leaky_git(args, cwd, timeout=60):
        if args[0] == "commit":
            return subprocess.CompletedProcess(args, 1, stdout="", stderr=f"pre-commit hook: token {SECRET}\n")
        return real_git(args, cwd, timeout)

    monkeypatch.setattr(mergeq, "_git", leaky_git)

    outcome = mergeq.merge_task(repo, "integration", "swarm/R1", "R1", ["echo ok"], conn=conn)

    assert outcome.merged is False and outcome.detail.startswith("commit failed:")
    assert SECRET not in outcome.detail and "[redacted]" in outcome.detail


def test_a_red_gate3_never_carries_a_secret_from_the_gate_output_into_the_outcome(repo, tmp_path, monkeypatch):
    _make_work_branch(repo, "swarm/R2", "new.txt", "hello\n")
    conn = db.connect(tmp_path / "ases.db")
    monkeypatch.setattr(
        gates, "run_gate",
        lambda candidate, sha, name, commands, **kw: gates.GateResult(name, sha, False, f"FAILED env dump {SECRET}"),
    )

    outcome = mergeq.merge_task(repo, "integration", "swarm/R2", "R2", ["echo ok"], conn=conn)

    assert outcome.merged is False and outcome.gate3_result == "fail"
    assert outcome.detail == "FAILED env dump [redacted]"


def test_a_refused_fast_forward_never_carries_a_secret_from_gits_stderr_into_the_outcome(repo, tmp_path, monkeypatch):
    _make_work_branch(repo, "swarm/R3", "new.txt", "hello\n")
    conn = db.connect(tmp_path / "ases.db")
    real_git = mergeq._git

    def leaky_git(args, cwd, timeout=60):
        if args[:2] == ["merge", "--ff-only"]:
            return subprocess.CompletedProcess(args, 128, stdout="", stderr=f"fatal: remote said {SECRET}\n")
        return real_git(args, cwd, timeout)

    monkeypatch.setattr(mergeq, "_git", leaky_git)

    outcome = mergeq.merge_task(repo, "integration", "swarm/R3", "R3", ["echo ok"], conn=conn)

    assert outcome.merged is False and outcome.gate3_result == "pass"
    assert SECRET not in outcome.detail and "remote said [redacted]" in outcome.detail


def test_a_real_gate3_that_prints_a_secret_is_redacted_in_the_outcome_and_in_gate_runs(repo, tmp_path):
    """End to end with no fakes: the real gate runner (which redacts what it stores and returns) under merge_task."""
    _make_work_branch(repo, "swarm/R4", "new.txt", "hello\n")
    conn = db.connect(tmp_path / "ases.db")

    outcome = mergeq.merge_task(repo, "integration", "swarm/R4", "R4", [f"echo leaked {SECRET}", "exit 1"], conn=conn)

    assert outcome.merged is False and outcome.gate3_result == "fail"
    stored = conn.execute("SELECT detail FROM gate_runs WHERE task_key = 'R4'").fetchone()["detail"]
    assert SECRET not in outcome.detail and SECRET not in stored
    assert "[redacted]" in outcome.detail and "[redacted]" in stored


# --- round 9 (ASES-QG-04, ASES-SEC-03/05/07): the Gate 3 candidate routes through gates.resolve_runner ----------


class _FakeProjectConfig:
    def __init__(self, enabled=True):
        self.sandbox_enabled = enabled

    def sandbox_policy_config(self):
        return {"sandbox": {"image": "registry.example/toolchain:1.0"}}


class _FakeTask:
    def __init__(self, sandbox_network=False, sandbox_network_reason=""):
        self.sandbox_network = sandbox_network
        self.sandbox_network_reason = sandbox_network_reason


def test_merge_task_passes_project_config_and_task_to_resolve_runner(repo, tmp_path, monkeypatch):
    _make_work_branch(repo, "swarm/PC1", "new.txt", "hello\n")
    conn = db.connect(tmp_path / "ases.db")
    calls = []
    real = gates.resolve_runner

    def spy(project_config, task=None):
        calls.append((project_config, task))
        return real(project_config, task)

    monkeypatch.setattr(gates, "resolve_runner", spy)
    project_config, task = _FakeProjectConfig(enabled=False), _FakeTask()

    mergeq.merge_task(
        repo, "integration", "swarm/PC1", "PC1", ["echo gate3-ok"], conn=conn,
        project_config=project_config, task=task,
    )

    assert calls == [(project_config, task)]


def test_merge_task_with_no_project_config_merges_exactly_as_before(repo, tmp_path):
    """Default None/None: today's behaviour, unchanged."""
    _make_work_branch(repo, "swarm/PC2", "new.txt", "hello\n")
    conn = db.connect(tmp_path / "ases.db")

    outcome = mergeq.merge_task(repo, "integration", "swarm/PC2", "PC2", ["echo gate3-ok"], conn=conn)

    assert outcome.merged is True and outcome.gate3_result == "pass"


def test_merge_task_when_sandbox_enabled_gives_run_gate_the_sandbox_runner_and_self_contained_checkout(
    repo, tmp_path, monkeypatch,
):
    _make_work_branch(repo, "swarm/PC3", "new.txt", "hello\n")
    conn = db.connect(tmp_path / "ases.db")
    seen = {}

    def fake_run_gate(repo_arg, sha, gate_name, commands, *, conn, task_key, project=None, runner=None,
                       self_contained_checkout=False, **kwargs):
        seen["runner"] = runner
        seen["self_contained_checkout"] = self_contained_checkout
        return gates.GateResult(gate_name, sha, True, "ok")

    monkeypatch.setattr(gates, "run_gate", fake_run_gate)

    mergeq.merge_task(
        repo, "integration", "swarm/PC3", "PC3", ["echo gate3-ok"], conn=conn,
        project_config=_FakeProjectConfig(enabled=True), task=_FakeTask(),
    )

    assert seen["self_contained_checkout"] is True
    assert callable(seen["runner"])


def test_merge_task_with_the_sandbox_off_calls_run_gate_with_todays_exact_keywords(repo, tmp_path, monkeypatch):
    """A run_gate stand-in with a fixed signature (no **kwargs, no self_contained_checkout) must keep working:
    the sandbox kwargs are added to the call only when the sandbox is actually enabled."""
    _make_work_branch(repo, "swarm/PC4", "new.txt", "hello\n")
    conn = db.connect(tmp_path / "ases.db")

    def fixed_signature_run_gate(repo_arg, sha, gate_name, commands, *, conn=None, task_key="", project=None,
                                  timeout_per_command=120, runner=None):
        return gates.GateResult(gate_name, sha, True, "ok")

    monkeypatch.setattr(gates, "run_gate", fixed_signature_run_gate)

    outcome = mergeq.merge_task(
        repo, "integration", "swarm/PC4", "PC4", ["echo gate3-ok"], conn=conn,
        project_config=_FakeProjectConfig(enabled=False), task=_FakeTask(),
    )

    assert outcome.merged is True


# --- the base-commit check (round 10, package BASECHECK; ASES-GIT-01, ASES-GIT-16) ------------------------------


def test_merge_task_merges_normally_when_the_branch_base_is_a_written_head(repo, tmp_path):
    base = _git_ok("rev-parse", "integration", cwd=repo).stdout.strip()
    _make_work_branch(repo, "swarm/B1", "new.txt", "hello\n")
    conn = db.connect(tmp_path / "ases.db")
    guards.set_expected_head(conn, "p1", base)

    outcome = mergeq.merge_task(repo, "integration", "swarm/B1", "B1", ["echo ok"], conn=conn, project="p1")

    assert outcome.merged is True and outcome.gate3_result == "pass"


def test_merge_task_refuses_when_the_branch_base_is_not_a_written_head(repo, tmp_path):
    """The planted-base case: a branch cut from a commit ASES never wrote, the way a remote-tip sync would."""
    _make_work_branch(repo, "swarm/B2", "new.txt", "hello\n")
    conn = db.connect(tmp_path / "ases.db")
    guards.set_expected_head(conn, "p1", "f" * 40)  # a head ASES wrote, but not the one B2 was cut from
    before = _git_ok("rev-parse", "integration", cwd=repo).stdout.strip()

    outcome = mergeq.merge_task(repo, "integration", "swarm/B2", "B2", ["echo ok"], conn=conn, project="p1")

    assert outcome.merged is False
    assert "ASES-GIT-01" in outcome.detail and "swarm/B2" in outcome.detail
    assert _git_ok("rev-parse", "integration", cwd=repo).stdout.strip() == before  # untouched
    assert _worktree_count(repo) == 1  # nothing was built: no candidate worktree left behind
    assert _merge_row(conn, "B2") is None  # refused before any merge_records row was ever written
    payload = json.loads(conn.execute(
        "SELECT payload FROM events WHERE kind = 'merge_refused_bad_base'"
    ).fetchone()["payload"])
    assert payload["branch"] == "swarm/B2" and payload["reason"]


def test_merge_task_skips_the_base_check_when_no_head_has_been_written_yet(repo, tmp_path):
    """A project that has never called guards.adopt_current_head/set_expected_head (written_heads empty) reads
    as "not started", never "started and forgot everything" -- the same reading check_primary_checkout already
    gives an expected_head of None."""
    _make_work_branch(repo, "swarm/B3", "new.txt", "hello\n")
    conn = db.connect(tmp_path / "ases.db")

    outcome = mergeq.merge_task(repo, "integration", "swarm/B3", "B3", ["echo ok"], conn=conn, project="p1")

    assert outcome.merged is True


def test_merge_task_skips_the_base_check_without_a_project_or_a_connection(repo, tmp_path):
    _make_work_branch(repo, "swarm/B4", "new.txt", "hello\n")
    conn = db.connect(tmp_path / "ases.db")
    guards.set_expected_head(conn, "p1", "f" * 40)  # would refuse B4/B5 if this project/conn pair were actually used

    without_project = mergeq.merge_task(repo, "integration", "swarm/B4", "B4", ["echo ok"], conn=conn)
    _make_work_branch(repo, "swarm/B5", "new.txt", "hello again\n")
    without_conn = mergeq.merge_task(repo, "integration", "swarm/B5", "B5", ["echo ok"], project="p1")

    assert without_project.merged is True
    assert without_conn.merged is True


def test_merge_task_before_this_check_existed_the_planted_base_test_would_have_merged(repo, tmp_path, monkeypatch):
    """Before/after proof (the work order's own words): with the base check disabled, the exact branch the test
    above refuses is merged instead, proving the refusal is this check's own doing."""
    _make_work_branch(repo, "swarm/B6", "new.txt", "hello\n")
    conn = db.connect(tmp_path / "ases.db")
    guards.set_expected_head(conn, "p1", "f" * 40)
    monkeypatch.setattr(mergeq, "_check_base", lambda *a, **kw: None)  # the pre-round-10 behaviour

    outcome = mergeq.merge_task(repo, "integration", "swarm/B6", "B6", ["echo ok"], conn=conn, project="p1")

    assert outcome.merged is True
