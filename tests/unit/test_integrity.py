import subprocess

import pytest

from ases import integrity


def _git(*args, cwd):
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git("init", "-q", "-b", "integration", cwd=r)
    _git("config", "user.email", "t@t", cwd=r)
    _git("config", "user.name", "t", cwd=r)
    (r / "base.txt").write_text("base\n", encoding="utf-8")
    _git("add", "-A", cwd=r)
    _git("commit", "-q", "-m", "init", cwd=r)
    return r


def test_changed_paths_reports_new_and_modified_files(repo):
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("x\n", encoding="utf-8")
    (repo / "base.txt").write_text("changed\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "work", cwd=repo)
    sha = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()

    changed = integrity.changed_paths(repo, sha)
    assert set(changed) == {"src/a.py", "base.txt"}


@pytest.mark.parametrize("changed,touches,expected_outside", [
    (["src/a.py", "src/b.py"], ["src/*"], []),
    (["src/a.py", "README.md"], ["src/*"], ["README.md"]),
    (["a.py"], [], ["a.py"]),                       # no declared touches -> everything is out of scope
    (["src/a.py"], ["src/**", "docs/*"], []),
    ([], ["src/*"], []),
])
def test_paths_outside_touches(changed, touches, expected_outside):
    assert integrity.paths_outside_touches(changed, touches) == expected_outside


def test_snapshot_captures_head_and_dirty_paths(repo):
    (repo / "new.txt").write_text("x\n", encoding="utf-8")
    snap = integrity.snapshot(repo)
    assert snap.head == _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    assert "new.txt" in snap.dirty_paths


def test_diff_snapshots_flags_unexpected_dirty_file(repo):
    before = integrity.snapshot(repo)
    (repo / "surprise.txt").write_text("x\n", encoding="utf-8")
    after = integrity.snapshot(repo)

    findings = integrity.diff_snapshots(before, after)
    assert any("surprise.txt" in f for f in findings)


def test_diff_snapshots_flags_moved_head(repo):
    before = integrity.snapshot(repo)
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "extra commit", cwd=repo)
    after = integrity.snapshot(repo)

    findings = integrity.diff_snapshots(before, after)
    assert any("HEAD moved" in f for f in findings)


def test_diff_snapshots_clean_when_nothing_changed(repo):
    before = integrity.snapshot(repo)
    after = integrity.snapshot(repo)
    assert integrity.diff_snapshots(before, after) == []


# --- integrity.attribute (round 19, package GIT12; ASES-GIT-12, r19/GIT12.md's own unit-test list A) -----------

_NOW = 1_000_000.0
_I = (_NOW - 100.0, _NOW + 100.0)  # a baseline interval well clear of the tail/skew edge cases below


def _run(run_id, task_id, started_at, ended_at=None, worker_pid=None):
    return integrity.RunCandidate(run_id, task_id, started_at, ended_at, worker_pid)


def test_own_run_of_the_owner_card_is_own_the_implementer_alone():
    result = integrity.attribute([_run(1, "T1", _NOW - 50, _NOW - 10)], _I, owner_card="T1", now=_NOW)
    assert result == integrity.Attribution("own", ())


def test_own_a_reviewer_run_of_the_same_task_id_is_also_own():
    # design A5(b): a review run is a task_runs row of the SAME task id, so it counts exactly like the coder's.
    result = integrity.attribute([_run(7, "T1", _NOW - 20, None)], _I, owner_card="T1", now=_NOW)
    assert result == integrity.Attribution("own", ())


def test_unique_exactly_one_other_cards_run_names_it():
    result = integrity.attribute([_run(2, "T2", _NOW - 50, _NOW - 10)], _I, owner_card="T1", now=_NOW)
    assert result == integrity.Attribution("unique", (("T2", 2),))


def test_ambiguous_two_distinct_cards_are_both_listed():
    runs = [_run(2, "T2", _NOW - 50, _NOW - 10), _run(3, "T3", _NOW - 40, None)]
    result = integrity.attribute(runs, _I, owner_card="T1", now=_NOW)
    assert result.verdict == "ambiguous"
    assert set(result.candidates) == {("T2", 2), ("T3", 3)}


def test_none_when_no_run_overlaps_the_interval():
    long_gone = _run(9, "T9", _NOW - 10_000, _NOW - 9_000)
    result = integrity.attribute([long_gone], _I, owner_card="T1", now=_NOW)
    assert result == integrity.Attribution("none", ())


def test_no_owner_card_still_finds_unique_and_ambiguous_candidates():
    """A subject with no owner (a foreign worktree the classifier found no card for) still gets a real verdict:
    owner_card=None never matches any run's task_id, so nothing is ever "own" for it."""
    result = integrity.attribute([_run(2, "T2", _NOW - 50, _NOW - 10)], _I, owner_card=None, now=_NOW)
    assert result == integrity.Attribution("unique", (("T2", 2),))


def test_the_reap_tail_edge_184s_is_a_candidate_186s_is_not():
    i_begin = _NOW
    interval = (i_begin, i_begin + 10)
    inside = _run(1, "T2", started_at=i_begin - 400, ended_at=i_begin - 184)   # ends 184s before I.begin
    outside = _run(2, "T3", started_at=i_begin - 400, ended_at=i_begin - 186)  # ends 186s before I.begin

    assert integrity.attribute([inside], interval, owner_card=None, now=i_begin + 500).verdict == "unique"
    assert integrity.attribute([outside], interval, owner_card=None, now=i_begin + 500).verdict == "none"


def test_a_worker_pid_still_set_ten_minutes_after_ended_at_is_still_a_candidate():
    t0 = _NOW
    interval = (t0 + 700, t0 + 710)   # well past t0 + 10 (ended_at) + the default 185s tail
    later = t0 + 750                  # "now": about ten minutes after ended_at

    without_pid = _run(1, "T2", started_at=t0, ended_at=t0 + 10, worker_pid=None)
    with_pid = _run(2, "T3", started_at=t0, ended_at=t0 + 10, worker_pid=4242)

    # Without the pid, ended_at + tail closed the window long before `interval` even starts.
    assert integrity.attribute([without_pid], interval, owner_card=None, now=later).verdict == "none"
    # With it, Hermes has not confirmed the worker gone, so the window stays open through `now`.
    assert integrity.attribute([with_pid], interval, owner_card=None, now=later).verdict == "unique"


def test_an_open_run_with_no_ended_at_is_always_a_candidate():
    run = _run(1, "T2", started_at=_NOW - 50, ended_at=None)
    result = integrity.attribute([run], (_NOW + 10_000, _NOW + 10_001), owner_card=None, now=_NOW + 10_000)
    assert result.verdict == "unique"


def test_the_skew_boundary_is_plus_and_minus_two_seconds():
    i_end = _NOW
    interval = (i_end - 10, i_end)
    # started_at - skew lands EXACTLY on i_end (a run starting 2s after the interval closed, pulled back by the
    # skew pad): still a candidate. `tail` is pinned to a small value so only the SKEW boundary is under test.
    on_boundary = _run(1, "T2", started_at=i_end + 2, ended_at=i_end + 2)
    just_outside = _run(2, "T3", started_at=i_end + 2.001, ended_at=i_end + 2.001)

    assert integrity.attribute(
        [on_boundary], interval, owner_card=None, now=i_end + 300, tail=1.0,
    ).verdict == "unique"
    assert integrity.attribute(
        [just_outside], interval, owner_card=None, now=i_end + 300, tail=1.0,
    ).verdict == "none"
