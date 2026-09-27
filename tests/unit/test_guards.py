import hashlib
import os
import pathlib
import re
import shutil
import subprocess
from datetime import datetime, timedelta

import pytest

from ases import db, gitexec, guards


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
    (r / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    _git("add", "-A", cwd=r)
    _git("commit", "-q", "-m", "init", cwd=r)
    return r


def _head(repo):
    return _git("rev-parse", "HEAD", cwd=repo).stdout.strip()


def _commit_new_file(repo, name="more.txt"):
    (repo / name).write_text("more\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", f"add {name}", cwd=repo)
    return _head(repo)


def _write(repo, rel, text="x\n"):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


# --- a clean checkout ----------------------------------------------------------------------------

def test_clean_checkout_on_the_integration_branch_is_ok(repo):
    result = guards.check_primary_checkout(repo, "integration")

    assert result.ok is True
    assert result.problems == ()
    assert result.branch == "integration"
    assert result.head == _head(repo)


# --- uncommitted changes: every kind of path git lists ------------------------------------------

def test_modified_tracked_file_is_a_problem(repo):
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")

    result = guards.check_primary_checkout(repo, "integration")

    assert result.ok is False
    assert result.problems == ("primary checkout is dirty: M 'tracked.txt'",)


def test_staged_new_file_is_a_problem(repo):
    _write(repo, "staged.txt")
    _git("add", "staged.txt", cwd=repo)

    result = guards.check_primary_checkout(repo, "integration")

    assert result.ok is False
    assert result.problems == ("primary checkout is dirty: A 'staged.txt'",)


def test_deleted_tracked_file_is_a_problem(repo):
    (repo / "tracked.txt").unlink()

    result = guards.check_primary_checkout(repo, "integration")

    assert result.ok is False
    assert result.problems == ("primary checkout is dirty: D 'tracked.txt'",)


def test_untracked_file_is_a_problem(repo):
    _write(repo, "stray.txt")

    result = guards.check_primary_checkout(repo, "integration")

    assert result.ok is False
    assert result.problems == ("primary checkout is dirty: ?? 'stray.txt'",)


def test_untracked_directory_is_reported_once_by_name(repo):
    _write(repo, "newdir/a.txt")
    _write(repo, "newdir/b.txt")

    result = guards.check_primary_checkout(repo, "integration")

    assert result.problems == ("primary checkout is dirty: ?? 'newdir/'",)


def test_rename_is_one_problem_naming_both_paths(repo):
    _git("config", "status.renames", "true", cwd=repo)  # do not depend on a global diff.renames=false
    _git("mv", "tracked.txt", "renamed.txt", cwd=repo)

    result = guards.check_primary_checkout(repo, "integration")

    # `status -z` follows a rename entry with the source path as a separate field: it must not become a second problem
    assert result.problems == ("primary checkout is dirty: R 'renamed.txt' (from 'tracked.txt')",)


def test_untracked_files_are_reported_even_if_git_config_hides_them(repo):
    _git("config", "status.showUntrackedFiles", "no", cwd=repo)
    _write(repo, "stray.txt")

    result = guards.check_primary_checkout(repo, "integration")

    assert result.problems == ("primary checkout is dirty: ?? 'stray.txt'",)


# --- paths that need care: spaces, non-ASCII, quoting ---------------------------------------------

def test_filename_with_a_space_is_named_exactly_without_git_quoting(repo):
    _write(repo, "a b.txt")

    result = guards.check_primary_checkout(repo, "integration")

    # plain `git status --porcelain` prints this path as "a b.txt" with the double quotes included
    assert result.problems == ("primary checkout is dirty: ?? 'a b.txt'",)


def test_non_ascii_filename_is_decoded_as_utf8_and_reported_ascii_safe(repo):
    _write(repo, "caf\u00e9 menu.txt")

    result = guards.check_primary_checkout(repo, "integration")

    # ascii() escapes the e-acute, so a problem string can be printed to any console: a mis-decoded name
    # (the locale code page instead of UTF-8) would show up here as two escaped bytes instead of one
    assert result.problems == ("primary checkout is dirty: ?? 'caf\\xe9 menu.txt'",)


# --- what is deliberately not a problem ---------------------------------------------------------

def test_untracked_file_under_worktrees_is_ignored(repo):
    _write(repo, ".worktrees/wt1/notes.txt")

    result = guards.check_primary_checkout(repo, "integration")

    assert result.ok is True, result.problems


def test_a_real_worker_worktree_under_worktrees_does_not_trip_the_guard(repo):
    before = _head(repo)
    wt = repo / ".worktrees" / "t1"
    _git("worktree", "add", "-q", "-b", "swarm/t1", str(wt), cwd=repo)
    _write(wt, "work.txt")
    _git("add", "-A", cwd=wt)
    _git("commit", "-q", "-m", "worker commit", cwd=wt)
    _write(wt, "scratch.txt", "an uncommitted change in the worker's own worktree\n")

    result = guards.check_primary_checkout(repo, "integration", before)

    assert result.ok is True, result.problems
    assert result.head == before


def test_a_file_git_ignores_never_appears(repo):
    _write(repo, ".gitignore", "ignored.log\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "ignore a log", cwd=repo)
    _write(repo, "ignored.log", "noise\n")

    result = guards.check_primary_checkout(repo, "integration")

    assert result.ok is True, result.problems


# --- the ignore prefix is a directory prefix ---------------------------------------------------

def test_ignore_prefix_matches_only_a_directory_at_the_start_of_the_path(repo):
    _write(repo, "sub/keep.txt")
    _git("add", "sub", cwd=repo)
    _git("commit", "-q", "-m", "sub", cwd=repo)
    _write(repo, ".worktrees-old/f.txt")  # shares the characters of '.worktrees' but is another directory
    _write(repo, "sub/.worktrees/x.txt")  # contains '.worktrees/' but does not start with it

    result = guards.check_primary_checkout(repo, "integration")

    assert sorted(result.problems) == [
        "primary checkout is dirty: ?? '.worktrees-old/'",
        "primary checkout is dirty: ?? 'sub/.worktrees/'",
    ]


@pytest.mark.parametrize("prefixes", [
    ("scratch/",), ("scratch",), ("\\scratch\\",), ("nope/", "scratch/"), "scratch/",
])
def test_custom_ignore_prefixes_replace_the_default(repo, prefixes):
    _write(repo, "scratch/f.txt")
    _write(repo, ".worktrees/f.txt")

    result = guards.check_primary_checkout(repo, "integration", ignore_prefixes=prefixes)

    assert result.problems == ("primary checkout is dirty: ?? '.worktrees/'",)


@pytest.mark.parametrize("prefixes", [(), ("",), ("/",), ""])
def test_an_empty_ignore_prefix_ignores_nothing(repo, prefixes):
    _write(repo, "stray.txt")

    result = guards.check_primary_checkout(repo, "integration", ignore_prefixes=prefixes)

    assert result.problems == ("primary checkout is dirty: ?? 'stray.txt'",)


def test_tracked_file_moved_into_an_ignored_prefix_is_still_reported(repo):
    _git("config", "status.renames", "true", cwd=repo)  # do not depend on a global diff.renames=false
    (repo / ".worktrees").mkdir()
    _git("mv", "tracked.txt", ".worktrees/tracked.txt", cwd=repo)

    result = guards.check_primary_checkout(repo, "integration")

    assert result.problems == ("primary checkout is dirty: R '.worktrees/tracked.txt' (from 'tracked.txt')",)


# --- branch and HEAD ---------------------------------------------------------------------------

def test_wrong_branch_is_a_problem(repo):
    _git("checkout", "-q", "-b", "other", cwd=repo)

    result = guards.check_primary_checkout(repo, "integration")

    assert result.ok is False
    assert result.problems == ("primary checkout is on branch 'other', expected 'integration'",)
    assert result.branch == "other"
    assert result.head == _head(repo)


def test_detached_head_is_a_problem(repo):
    _git("checkout", "-q", "--detach", cwd=repo)

    result = guards.check_primary_checkout(repo, "integration")

    assert result.ok is False
    assert result.problems == ("primary checkout has a detached HEAD, expected branch 'integration'",)
    assert result.branch == ""
    assert result.head == _head(repo)


def test_moved_head_is_a_problem_naming_both_shas_shortened_to_12(repo):
    old = _head(repo)
    new = _commit_new_file(repo)
    assert old != new

    result = guards.check_primary_checkout(repo, "integration", old)

    assert result.ok is False
    assert result.problems == (f"primary checkout HEAD moved: expected {old[:12]}, found {new[:12]}",)
    assert result.head == new
    assert len(old[:12]) == 12 and old[:12] != old


def test_head_equal_to_expected_head_is_ok(repo):
    _commit_new_file(repo)

    result = guards.check_primary_checkout(repo, "integration", _head(repo))

    assert result.ok is True, result.problems


def test_head_comparison_is_skipped_when_no_expected_head_is_given(repo):
    _commit_new_file(repo)

    assert guards.check_primary_checkout(repo, "integration").ok is True
    assert guards.check_primary_checkout(repo, "integration", None).ok is True


def test_several_problems_at_once_are_all_reported(repo):
    old = _head(repo)
    _git("checkout", "-q", "-b", "other", cwd=repo)
    new = _commit_new_file(repo)
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    _write(repo, "stray.txt")

    result = guards.check_primary_checkout(repo, "integration", old)

    assert result.ok is False
    assert result.problems == (
        "primary checkout is on branch 'other', expected 'integration'",
        f"primary checkout HEAD moved: expected {old[:12]}, found {new[:12]}",
        "primary checkout is dirty: M 'tracked.txt'",
        "primary checkout is dirty: ?? 'stray.txt'",
    )


# --- git itself failing is a problem, never an exception and never a clean bill -----------------

def test_a_directory_that_is_not_a_git_repository_is_a_problem_not_an_exception(tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))  # never discover a repository above the temp dir

    result = guards.check_primary_checkout(plain, "integration", "abc123")

    assert result.ok is False
    assert len(result.problems) == 1
    assert result.problems[0].startswith(f"cannot inspect the primary checkout at {plain}: ")
    assert (result.head, result.branch) == ("", "")


def test_a_missing_directory_is_a_problem_not_an_exception(tmp_path):
    missing = tmp_path / "missing"

    result = guards.check_primary_checkout(missing, "integration")

    assert result.ok is False
    assert len(result.problems) == 1
    assert result.problems[0].startswith(f"cannot inspect the primary checkout at {missing}: ")
    assert (result.head, result.branch) == ("", "")


@pytest.mark.parametrize("error,expected", [
    (FileNotFoundError("git"), "git could not be run"),
    (subprocess.TimeoutExpired("git", 60), "timed out"),
])
def test_git_that_cannot_run_or_times_out_is_a_problem_not_an_exception(repo, monkeypatch, error, expected):
    def fail(cmd, **kwargs):
        raise error

    monkeypatch.setattr(guards.subprocess, "run", fail)

    result = guards.check_primary_checkout(repo, "integration")

    assert result.ok is False
    assert len(result.problems) == 1
    assert expected in result.problems[0]
    assert (result.head, result.branch) == ("", "")


def test_a_failing_git_status_is_a_problem_not_a_clean_bill(repo, monkeypatch):
    head = _head(repo)
    real_run = subprocess.run

    def run(cmd, **kwargs):
        if "status" in cmd:
            return subprocess.CompletedProcess(cmd, 128, b"", b"fatal: index file corrupt\n")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(guards.subprocess, "run", run)

    result = guards.check_primary_checkout(repo, "integration")

    assert result.ok is False
    assert result.problems == (f"git status failed in the primary checkout at {repo}: fatal: index file corrupt",)
    assert (result.head, result.branch) == (head, "integration")


def test_a_repository_without_commits_is_a_problem(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    _git("init", "-q", "-b", "integration", cwd=empty)

    result = guards.check_primary_checkout(empty, "integration", "abc123")

    assert result.ok is False
    assert result.problems == (
        f"cannot read HEAD of the primary checkout at {empty}: HEAD does not resolve to a commit",
    )
    assert (result.head, result.branch) == ("", "integration")


def test_git_is_only_used_read_only_and_always_time_limited(repo, monkeypatch):
    calls = []
    real_run = subprocess.run

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(guards.subprocess, "run", run)

    guards.check_primary_checkout(repo, "integration")

    assert calls
    for cmd, kwargs in calls:
        assert cmd[0] == "git" and "--no-optional-locks" in cmd  # never takes index.lock from a real git operation
        assert cmd[cmd.index("-C") + 2] in {"symbolic-ref", "rev-parse", "status"}
        assert kwargs["timeout"] > 0


# --- round 9, package GITHARDEN: a planted fsmonitor hook does not fire on check_primary_checkout's status ----

def test_a_planted_fsmonitor_hook_does_not_run_during_check_primary_checkouts_status(repo, tmp_path, monkeypatch):
    """Item 3 of the round 8 sweep: guards._git's `status` call (check_primary_checkout's uncommitted-change
    check) used to run with whatever core.fsmonitor a worker wrote into the shared .git/config, and a planted
    fsmonitor hook fires on every status with nothing in git's own output to show it -- --no-optional-locks does
    not affect it, only -c core.fsmonitor=false does.

    Before/after in one test: the SAME hook, with gitexec.GIT weakened back to a bare "git" (no fsmonitor
    override), DOES fire on the very next status call, proving the clean result above is the override's doing."""
    marker = tmp_path / "fsmonitor_ran.txt"
    # core.fsmonitor's value is a single executable path, not a shell command line (unlike diff/filter drivers,
    # which run through `sh -c`): a shebang script is the plain way to plant one. It replies with nothing on
    # stdout, which tells git "nothing is known to have changed", keeping `status` accurate. Kept OUTSIDE the
    # repository (tmp_path, not repo) so it never shows up as an untracked file in the status being checked.
    script = tmp_path / "fsmon-hook.sh"
    script.write_text(f'#!/bin/sh\necho ran >> "{marker.as_posix()}"\n', encoding="utf-8", newline="\n")
    script.chmod(0o755)
    # Forward slashes: an unquoted git-config value treats a backslash as a (mostly unrecognised) C-style
    # escape and silently drops it, corrupting a native Windows path -- as_posix() (still "C:/Users/...", not a
    # /c/... mount path) is what stays intact through git config's own value parser.
    _git("config", "core.fsmonitor", script.as_posix(), cwd=repo)

    result = guards.check_primary_checkout(repo, "integration")

    assert result.ok is True
    assert not marker.exists(), "a planted fsmonitor hook ran during check_primary_checkout's status call"

    monkeypatch.setattr(gitexec, "GIT", ("git",))
    guards.check_primary_checkout(repo, "integration")
    assert marker.exists(), "fixture problem: the fsmonitor hook should fire once gitexec.GIT is weakened to plain git"


# --- expected head in the database ---------------------------------------------------------------

def test_expected_head_is_none_until_one_is_set(tmp_path):
    conn = db.connect(tmp_path / "ases.db")

    assert guards.expected_head(conn, "proj") is None


def test_set_expected_head_round_trips_per_project(tmp_path):
    conn = db.connect(tmp_path / "ases.db")

    guards.set_expected_head(conn, "proj-a", "a" * 40)
    guards.set_expected_head(conn, "proj-b", "b" * 40)

    assert guards.expected_head(conn, "proj-a") == "a" * 40
    assert guards.expected_head(conn, "proj-b") == "b" * 40
    assert guards.expected_head(conn, "proj-c") is None


def test_set_expected_head_is_an_idempotent_upsert(tmp_path):
    conn = db.connect(tmp_path / "ases.db")

    guards.set_expected_head(conn, "proj", "a" * 40)
    guards.set_expected_head(conn, "proj", "a" * 40)
    assert conn.execute("SELECT COUNT(*) FROM integrity_state").fetchone()[0] == 1
    assert guards.expected_head(conn, "proj") == "a" * 40

    guards.set_expected_head(conn, "proj", "b" * 40)
    assert conn.execute("SELECT COUNT(*) FROM integrity_state").fetchone()[0] == 1
    assert guards.expected_head(conn, "proj") == "b" * 40


def test_set_expected_head_replaces_the_row_and_refreshes_updated_at(tmp_path):
    conn = db.connect(tmp_path / "ases.db")
    long_ago = "2000-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO integrity_state (project, expected_head, updated_at) VALUES ('proj', 'old', ?)", (long_ago,),
    )

    guards.set_expected_head(conn, "proj", "new")

    row = conn.execute("SELECT expected_head, updated_at FROM integrity_state WHERE project='proj'").fetchone()
    assert row["expected_head"] == "new"
    assert row["updated_at"] != long_ago


def test_set_expected_head_stamps_utc_isoformat_to_the_second(tmp_path):
    conn = db.connect(tmp_path / "ases.db")

    guards.set_expected_head(conn, "proj", "a" * 40)

    stamp = conn.execute("SELECT updated_at FROM integrity_state WHERE project='proj'").fetchone()["updated_at"]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00", stamp), stamp
    assert datetime.fromisoformat(stamp).utcoffset() == timedelta(0)


@pytest.mark.parametrize("blank", ["", "   ", "\n"])
def test_set_expected_head_refuses_a_blank_sha(tmp_path, blank):
    conn = db.connect(tmp_path / "ases.db")

    with pytest.raises(ValueError):
        guards.set_expected_head(conn, "proj", blank)

    assert guards.expected_head(conn, "proj") is None


def test_set_expected_head_strips_surrounding_whitespace(tmp_path):
    conn = db.connect(tmp_path / "ases.db")

    guards.set_expected_head(conn, "proj", "  " + "a" * 40 + "\n")

    assert guards.expected_head(conn, "proj") == "a" * 40


def test_adopt_current_head_records_and_returns_the_primary_checkout_head(repo, tmp_path):
    conn = db.connect(tmp_path / "ases.db")

    adopted = guards.adopt_current_head(conn, "proj", repo)

    assert adopted == _head(repo)
    assert guards.expected_head(conn, "proj") == adopted
    assert guards.check_primary_checkout(repo, "integration", guards.expected_head(conn, "proj")).ok is True


def test_adopt_current_head_follows_a_head_that_moved(repo, tmp_path):
    conn = db.connect(tmp_path / "ases.db")
    first = guards.adopt_current_head(conn, "proj", repo)
    second_head = _commit_new_file(repo)

    second = guards.adopt_current_head(conn, "proj", repo)

    assert second == second_head != first
    assert guards.expected_head(conn, "proj") == second_head
    assert conn.execute("SELECT COUNT(*) FROM integrity_state").fetchone()[0] == 1


def test_adopt_current_head_refuses_an_unreadable_head_and_records_nothing(tmp_path):
    conn = db.connect(tmp_path / "ases.db")
    missing = tmp_path / "missing"

    with pytest.raises(RuntimeError, match="cannot read HEAD of the primary checkout"):
        guards.adopt_current_head(conn, "proj", missing)

    assert guards.expected_head(conn, "proj") is None


# --- the other worktrees (ASES-GIT-12) -----------------------------------------------------------

@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "ases.db")


def _worktree(repo, name, *, detach=False):
    """A real linked worktree at <repo>/.worktrees/<name>, where Hermes puts them."""
    wt = repo / ".worktrees" / name
    if detach:
        _git("worktree", "add", "-q", "--detach", str(wt), cwd=repo)
    else:
        _git("worktree", "add", "-q", "-b", f"swarm/{name}", str(wt), cwd=repo)
    return wt


def _key(path):
    """A path the way worktree_snapshots stores it: resolved, separators unified, case folded on Windows."""
    return os.path.normcase(os.path.realpath(str(path)))


def _snapshot_rows(conn, project="proj"):
    """Confirmed baselines only: a worktree's grace and pending markers are separate rows, keyed by the worktree's
    own key plus a NUL suffix, and are not part of what a caller normally means by "the stored snapshot"."""
    cur = conn.execute("SELECT path, head, status_hash FROM worktree_snapshots WHERE project = ?", (project,))
    return {
        row["path"]: (row["head"], row["status_hash"])
        for row in cur.fetchall() if not row["path"].endswith(("\x00grace", "\x00pending"))
    }


def _listed(repo, name):
    """The path git lists for the worktree called `name`, spelled the way a problem message spells it."""
    return str(next(w.path for w in guards.list_worktrees(repo) if w.path.name == name))


def _idle(conn, repo, running=(), project="proj", **kwargs):
    return guards.check_idle_worktrees(conn, project, repo, set(running), **kwargs)


def _changed(repo, name, detail):
    return f"worktree {_listed(repo, name)} changed while no card was running in it: {detail}"


# list_worktrees

def test_list_worktrees_reports_the_primary_checkout_first_then_each_linked_worktree(repo):
    wt = _worktree(repo, "t1")

    found = guards.list_worktrees(repo)

    assert [_key(w.path) for w in found] == [_key(repo), _key(wt)]
    primary, linked = found
    assert (primary.branch, primary.head) == ("integration", _head(repo))
    assert (linked.branch, linked.head) == ("swarm/t1", _head(repo))
    for info in found:
        assert (info.detached, info.bare, info.locked, info.prunable) == (False, False, False, False)


def test_list_worktrees_reports_a_detached_worktree(repo):
    wt = _worktree(repo, "d1", detach=True)

    linked = guards.list_worktrees(repo)[1]

    assert _key(linked.path) == _key(wt)
    assert (linked.branch, linked.detached, linked.head) == (None, True, _head(repo))


def test_list_worktrees_reports_locked_and_prunable_worktrees(repo):
    locked = _worktree(repo, "locked1")
    _git("worktree", "lock", "--reason", "in use", str(locked), cwd=repo)
    gone = _worktree(repo, "gone1")
    shutil.rmtree(gone)  # removed behind git's back

    by_name = {w.path.name: w for w in guards.list_worktrees(repo)}

    assert (by_name["locked1"].locked, by_name["locked1"].prunable) == (True, False)
    assert (by_name["gone1"].locked, by_name["gone1"].prunable) == (False, True)


def test_list_worktrees_reports_a_bare_repository(tmp_path):
    bare = tmp_path / "bare.git"
    _git("init", "-q", "--bare", str(bare), cwd=tmp_path)

    found = guards.list_worktrees(bare)

    assert len(found) == 1
    assert (found[0].bare, found[0].head, found[0].branch, found[0].detached) == (True, "", None, False)


def test_list_worktrees_reads_a_path_with_a_space_and_a_non_ascii_character_exactly(repo):
    name = "caf\u00e9 dir"
    wt = _worktree(repo, name, detach=True)

    found = guards.list_worktrees(repo)

    assert found[1].path.name == name
    assert _key(found[1].path) == _key(wt)


def test_list_worktrees_is_empty_when_git_fails_and_never_raises(tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))  # never discover a repository above the temp dir

    assert guards.list_worktrees(plain) == []
    assert guards.list_worktrees(tmp_path / "missing") == []


@pytest.mark.parametrize("error", [FileNotFoundError("git"), subprocess.TimeoutExpired("git", 60)])
def test_list_worktrees_survives_a_git_that_cannot_run(repo, monkeypatch, error):
    def fail(cmd, **kwargs):
        raise error

    monkeypatch.setattr(guards.subprocess, "run", fail)

    assert guards.list_worktrees(repo) == []


def test_list_worktrees_falls_back_to_the_plain_format_for_a_git_without_z(repo, monkeypatch):
    _worktree(repo, "t1")
    real_run = subprocess.run
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if "-z" in cmd:  # what a git older than 2.36 answers
            return subprocess.CompletedProcess(cmd, 129, b"", b"error: unknown switch `z'\nusage: git worktree list\n")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(guards.subprocess, "run", run)

    found = guards.list_worktrees(repo)

    assert [w.branch for w in found] == ["integration", "swarm/t1"]
    assert len(calls) == 2 and "-z" in calls[0] and "-z" not in calls[1]


def test_a_git_error_that_is_not_a_usage_error_is_not_retried(repo, monkeypatch):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 128, b"", b"fatal: boom\n")

    monkeypatch.setattr(guards.subprocess, "run", run)

    assert guards.list_worktrees(repo) == []
    assert len(calls) == 1


_SHA_A, _SHA_B = "a" * 40, "b" * 40
_LISTING = (
    f"worktree /a\nHEAD {_SHA_A}\nbranch refs/heads/main\n\n"
    f"worktree /b\nHEAD {_SHA_B}\ndetached\nlocked because reasons\nfuture-key value\n\n"
    "worktree /c\nprunable gitdir file points to non-existent location\nbranch refs/tags/odd\n\n"
    "HEAD orphan-record-without-a-worktree-line\n\n"
    "worktree /d\nbare"  # the last record has no blank line after it
)
_PARSED = [
    (pathlib.Path("/a"), _SHA_A, "main", False, False, False, False),
    (pathlib.Path("/b"), _SHA_B, None, True, False, True, False),
    (pathlib.Path("/c"), "", "refs/tags/odd", False, False, False, True),
    (pathlib.Path("/d"), "", None, False, True, False, False),
]


@pytest.mark.parametrize("text,terminator", [
    (_LISTING, "\n"),
    (_LISTING.replace("\n", "\r\n"), "\n"),
    (_LISTING.replace("\n", "\0"), "\0"),
])
def test_the_listing_parser_reads_flags_and_skips_what_it_does_not_know_in_all_three_layouts(text, terminator):
    found = guards._parse_worktrees(text, terminator)

    assert [(w.path, w.head, w.branch, w.detached, w.bare, w.locked, w.prunable) for w in found] == _PARSED


def test_the_listing_parser_keeps_spaces_inside_a_path():
    found = guards._parse_worktrees("worktree /with space/dir name\nHEAD " + _SHA_A + "\ndetached\n", "\n")

    assert [w.path for w in found] == [pathlib.Path("/with space/dir name")]


def test_worktree_info_is_frozen_and_defaults_its_flags_to_false():
    info = guards.WorktreeInfo(pathlib.Path("/x"), "abc", None)

    assert (info.detached, info.bare, info.locked, info.prunable) == (False, False, False, False)
    with pytest.raises(Exception):
        info.head = "other"


# snapshot_worktree

def test_snapshot_worktree_is_the_head_and_the_hash_of_the_porcelain_status(repo):
    wt = _worktree(repo, "t1")
    clean = guards.snapshot_worktree(wt)
    _write(wt, "new.txt")

    head, digest = guards.snapshot_worktree(wt)

    raw = subprocess.run(
        ["git", "-C", str(wt), "status", "--porcelain", "-z", "--untracked-files=normal"], capture_output=True,
    ).stdout
    assert raw == b"?? new.txt\0"
    assert head == _head(wt) == clean[0]
    assert digest == hashlib.sha256(raw).hexdigest()
    assert clean[1] == hashlib.sha256(b"").hexdigest()


def test_snapshot_worktree_moves_with_head_and_with_status_and_with_nothing_else(repo):
    wt = _worktree(repo, "t1")
    base = guards.snapshot_worktree(wt)
    assert guards.snapshot_worktree(wt) == base

    _write(wt, "a.txt")
    dirty = guards.snapshot_worktree(wt)
    assert dirty[0] == base[0] and dirty[1] != base[1]

    _git("add", "-A", cwd=wt)
    _git("commit", "-q", "-m", "a", cwd=wt)
    committed = guards.snapshot_worktree(wt)
    assert committed[0] != base[0] and committed[1] == base[1]  # clean again: the same status hash as before


def test_snapshot_worktree_accepts_a_string_path(repo):
    wt = _worktree(repo, "t1")

    assert guards.snapshot_worktree(str(wt)) == guards.snapshot_worktree(wt)


def test_snapshot_worktree_is_empty_when_it_cannot_be_read(tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    empty = tmp_path / "empty"
    empty.mkdir()
    _git("init", "-q", "-b", "integration", cwd=empty)

    assert guards.snapshot_worktree(plain) == ("", "")
    assert guards.snapshot_worktree(tmp_path / "missing") == ("", "")
    assert guards.snapshot_worktree(empty) == ("", "")  # no commits: no HEAD


def test_snapshot_worktree_is_empty_when_git_status_fails(repo, monkeypatch):
    real_run = subprocess.run

    def run(cmd, **kwargs):
        if "status" in cmd:
            return subprocess.CompletedProcess(cmd, 128, b"", b"fatal: index file corrupt\n")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(guards.subprocess, "run", run)

    assert guards.snapshot_worktree(repo) == ("", "")


@pytest.mark.parametrize("error", [FileNotFoundError("git"), subprocess.TimeoutExpired("git", 60)])
def test_snapshot_worktree_survives_a_git_that_cannot_run(repo, monkeypatch, error):
    def fail(cmd, **kwargs):
        raise error

    monkeypatch.setattr(guards.subprocess, "run", fail)

    assert guards.snapshot_worktree(repo) == ("", "")


def test_snapshot_worktree_leaves_out_status_paths_under_an_ignore_prefix(repo):
    wt = _worktree(repo, "t1")
    clean = guards.snapshot_worktree(wt, ignore_prefixes=("scratch/",))

    _write(wt, "scratch/f.txt")
    assert guards.snapshot_worktree(wt, ignore_prefixes=("scratch/",)) == clean
    assert guards.snapshot_worktree(wt, ignore_prefixes=("scratch",)) == clean  # the same prefix, spelled without a slash
    assert guards.snapshot_worktree(wt)[1] != clean[1]  # without the prefix the file counts

    _write(wt, "other.txt")
    assert guards.snapshot_worktree(wt, ignore_prefixes=("scratch/",))[1] != clean[1]  # an entry outside it does


def test_an_ignore_prefix_does_not_swallow_a_directory_that_merely_starts_with_the_same_letters(repo):
    wt = _worktree(repo, "t1")
    clean = guards.snapshot_worktree(wt, ignore_prefixes=("scratch",))

    _write(wt, "scratch-old/f.txt")

    assert guards.snapshot_worktree(wt, ignore_prefixes=("scratch",))[1] != clean[1]


def test_worktree_git_calls_are_read_only_and_time_limited(conn, repo, monkeypatch):
    wt = _worktree(repo, "t1")
    real_run = subprocess.run
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(guards.subprocess, "run", run)

    guards.list_worktrees(repo)
    guards.snapshot_worktree(wt)
    _idle(conn, repo)
    guards.refresh_snapshots(conn, "proj", repo, set())

    assert calls
    for cmd, kwargs in calls:
        assert cmd[0] == "git" and "--no-optional-locks" in cmd  # never takes index.lock from a real git operation
        assert cmd[cmd.index("-C") + 2] in {"worktree", "rev-parse", "status"}
        assert kwargs["timeout"] > 0


# check_idle_worktrees

def test_first_sight_of_an_idle_worktree_records_its_snapshot_and_reports_nothing(conn, repo):
    wt = _worktree(repo, "t1")

    assert _idle(conn, repo) == []

    assert _snapshot_rows(conn) == {_key(wt): (_head(wt), hashlib.sha256(b"").hexdigest())}


def test_the_snapshot_row_is_stamped_in_utc_to_the_second(conn, repo):
    _worktree(repo, "t1")
    _idle(conn, repo)

    stamp = conn.execute("SELECT taken_at FROM worktree_snapshots").fetchone()["taken_at"]

    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00", stamp), stamp
    assert datetime.fromisoformat(stamp).utcoffset() == timedelta(0)


def test_an_untouched_idle_worktree_stays_quiet_pass_after_pass(conn, repo):
    wt = _worktree(repo, "t1")

    for _ in range(3):
        assert _idle(conn, repo) == []

    assert list(_snapshot_rows(conn)) == [_key(wt)]


def test_a_commit_in_an_idle_worktree_is_reported_once(conn, repo):
    wt = _worktree(repo, "t1")
    _idle(conn, repo)
    old = _head(wt)
    new = _commit_new_file(wt)

    problems = _idle(conn, repo)

    assert problems == [_changed(repo, "t1", f"HEAD {old[:12]}..{new[:12]}")]
    assert len(old[:12]) == 12 and old[:12] != old
    assert _idle(conn, repo) == []  # reported once, not on every pass from now on
    assert _snapshot_rows(conn)[_key(wt)][0] == new


def test_an_uncommitted_change_in_an_idle_worktree_is_reported_once(conn, repo):
    wt = _worktree(repo, "t1")
    _idle(conn, repo)
    _write(wt, "stray.txt")

    assert _idle(conn, repo) == [_changed(repo, "t1", "status changed")]
    assert _idle(conn, repo) == []

    _write(wt, "another.txt")  # the next change is a new report
    assert _idle(conn, repo) == [_changed(repo, "t1", "status changed")]


def test_an_edit_to_a_tracked_file_is_a_status_change_too(conn, repo):
    wt = _worktree(repo, "t1")
    _idle(conn, repo)
    (wt / "tracked.txt").write_text("edited\n", encoding="utf-8")

    assert _idle(conn, repo) == [_changed(repo, "t1", "status changed")]


def test_a_moved_head_and_a_changed_status_together_are_one_problem(conn, repo):
    wt = _worktree(repo, "t1")
    _idle(conn, repo)
    old = _head(wt)
    new = _commit_new_file(wt)
    _write(wt, "left-over.txt")

    assert _idle(conn, repo) == [_changed(repo, "t1", f"HEAD {old[:12]}..{new[:12]}; status changed")]


def test_each_idle_worktree_is_judged_on_its_own(conn, repo):
    a, b, c = _worktree(repo, "a"), _worktree(repo, "b"), _worktree(repo, "c")
    _idle(conn, repo)
    _write(b, "stray.txt")

    assert _idle(conn, repo) == [_changed(repo, "b", "status changed")]
    assert set(_snapshot_rows(conn)) == {_key(a), _key(b), _key(c)}


def test_a_running_cards_worktree_is_skipped_and_its_baseline_is_dropped_until_it_stops(conn, repo):
    wt = _worktree(repo, "t1")
    _idle(conn, repo)
    assert _snapshot_rows(conn)
    _commit_new_file(wt, "own-work.txt")  # the running card's own worker commits

    assert _idle(conn, repo, running=[str(wt)]) == []
    assert _snapshot_rows(conn) == {}

    assert _idle(conn, repo) == []  # the card stopped: the first idle look is the new baseline, nothing is reported
    assert list(_snapshot_rows(conn)) == [_key(wt)]
    _commit_new_file(wt, "later.txt")  # a change after that is caught again
    assert _idle(conn, repo) == []  # this fresh, just-vacated baseline still carries its one grace pass
    assert len(_idle(conn, repo)) == 1  # never explained: confirmed on the pass after that


# --- the re-dispatch race: a worktree just vacated by a running card earns one grace pass -------------------------

def test_a_worktree_no_card_has_ever_left_gets_no_grace_and_is_reported_on_its_very_first_divergence(conn, repo):
    """The baseline for a worktree check_idle_worktrees has never seen owned carries no grace pass, so it is
    judged exactly as before this round: immediately, on the very first pass that finds it changed."""
    wt = _worktree(repo, "t1")
    _idle(conn, repo)  # a genuinely first sight: wt was never in running_paths before this
    old = _head(wt)
    new = _commit_new_file(wt)

    assert _idle(conn, repo) == [_changed(repo, "t1", f"HEAD {old[:12]}..{new[:12]}")]


def test_a_worktree_that_becomes_owned_again_explains_a_change_seen_right_after_it_was_vacated(conn, repo):
    """ASES-GIT-12's own documented false positive (the register's ROUND 6 note on ASES-GIT-12; r9_wp_small.md's
    IDLEWT): "a card re-dispatched into its worktree between two polls" can leave the worktree looking idle and
    changed for exactly one pass, before the board catches up and shows it running again. The pass right after a
    running card leaves its worktree spends that worktree's one grace pass on its first divergence rather than
    reporting it, so if the worktree is owned again by the next pass, the change is explained, never reported."""
    wt = _worktree(repo, "t1")
    _idle(conn, repo)
    _commit_new_file(wt, "own-work.txt")
    assert _idle(conn, repo, running=[str(wt)]) == []  # the card runs and commits
    assert _idle(conn, repo) == []  # vacated: a fresh baseline, one grace pass earned
    _commit_new_file(wt, "re-dispatched.txt")  # a re-dispatch starts writing before the board reflects it

    assert _idle(conn, repo) == []  # looks idle for this one pass: held back, not blamed
    assert _idle(conn, repo, running=[str(wt)]) == []  # the board now shows it running again: explained
    assert _idle(conn, repo) == []  # vacated once more, from a clean baseline: still nothing to report
    assert _snapshot_rows(conn)[_key(wt)][0] == _head(wt)


def test_a_post_vacate_change_that_is_never_explained_is_still_reported(conn, repo):
    """The grace pass only delays a genuine, unexplained change by one pass: it never lets one through for good."""
    wt = _worktree(repo, "t1")
    _idle(conn, repo)
    _commit_new_file(wt, "own-work.txt")
    assert _idle(conn, repo, running=[str(wt)]) == []
    assert _idle(conn, repo) == []  # vacated: a fresh baseline, one grace pass earned
    old = _head(wt)
    new = _commit_new_file(wt, "intruder.txt")  # nobody re-dispatches: this is never explained

    assert _idle(conn, repo) == []  # held back for its one grace pass
    assert _idle(conn, repo) == [_changed(repo, "t1", f"HEAD {old[:12]}..{new[:12]}")]  # confirmed the pass after


def test_a_change_that_keeps_moving_between_two_idle_looks_is_still_confirmed(conn, repo):
    """Confirming a held-back change does not require the worktree to have settled: only that it is still idle
    and still different from the ORIGINAL baseline on the pass after it was first noticed."""
    wt = _worktree(repo, "t1")
    _idle(conn, repo)
    _commit_new_file(wt, "own-work.txt")
    assert _idle(conn, repo, running=[str(wt)]) == []
    assert _idle(conn, repo) == []  # vacated: a fresh baseline, one grace pass earned
    old = _head(wt)
    _write(wt, "first.txt")

    assert _idle(conn, repo) == []  # held back
    new = _commit_new_file(wt, "second.txt")  # the worktree keeps changing before the next pass

    assert _idle(conn, repo) == [_changed(repo, "t1", f"HEAD {old[:12]}..{new[:12]}")]


def test_a_change_that_fully_reverts_before_the_next_pass_is_never_reported(conn, repo):
    wt = _worktree(repo, "t1")
    _idle(conn, repo)
    _commit_new_file(wt, "own-work.txt")
    assert _idle(conn, repo, running=[str(wt)]) == []
    assert _idle(conn, repo) == []  # vacated: a fresh baseline, one grace pass earned
    _write(wt, "stray.txt")

    assert _idle(conn, repo) == []  # held back
    (wt / "stray.txt").unlink()  # removed again before the next pass: back to the confirmed baseline exactly

    assert _idle(conn, repo) == []
    assert _idle(conn, repo) == []  # and the held-back sighting does not linger either


def test_the_grace_pass_expires_after_one_full_quiet_pass(conn, repo):
    """A worktree that proves quiet for one whole pass after being vacated is judged immediately from then on,
    the same as a worktree no card has ever left: the grace pass is not renewed pass after pass."""
    wt = _worktree(repo, "t1")
    _idle(conn, repo)
    _commit_new_file(wt, "own-work.txt")
    assert _idle(conn, repo, running=[str(wt)]) == []  # a grace pass is earned for the next time it is idle
    assert _idle(conn, repo) == []  # vacated: fresh baseline, grace pass still unspent
    assert _idle(conn, repo) == []  # a full quiet pass: the grace pass is spent, unused
    old = _head(wt)
    new = _commit_new_file(wt, "later.txt")

    assert _idle(conn, repo) == [_changed(repo, "t1", f"HEAD {old[:12]}..{new[:12]}")]  # reported immediately


def test_a_running_worktree_is_skipped_even_when_it_has_no_baseline_yet(conn, repo):
    wt = _worktree(repo, "t1")

    assert _idle(conn, repo, running=[wt]) == []

    assert _snapshot_rows(conn) == {}


def test_running_paths_match_whatever_separator_case_or_type_hermes_uses(conn, repo):
    wt = _worktree(repo, "t1")
    spellings = [str(wt), wt.as_posix(), wt]
    if os.name == "nt":
        spellings += [str(wt).upper(), wt.as_posix().lower()]

    for index, spelling in enumerate(spellings):
        _idle(conn, repo)  # baseline (a first sight, because the previous pass dropped the row)
        _commit_new_file(wt, f"f{index}.txt")

        assert _idle(conn, repo, running=[spelling]) == [], spelling


def test_a_lone_running_path_string_is_one_path_not_a_sequence_of_characters(conn, repo):
    wt = _worktree(repo, "t1")
    _idle(conn, repo)
    _commit_new_file(wt)

    assert guards.check_idle_worktrees(conn, "proj", repo, str(wt)) == []
    _idle(conn, repo)
    _commit_new_file(wt, "again.txt")
    assert guards.check_idle_worktrees(conn, "proj", repo, wt) == []  # a lone Path likewise


def test_empty_running_paths_are_ignored_rather_than_read_as_the_current_directory(conn, repo, monkeypatch):
    wt = _worktree(repo, "t1")
    monkeypatch.chdir(wt)  # an empty path must not resolve to here and hide this worktree
    _idle(conn, repo)
    _commit_new_file(wt)

    assert len(guards.check_idle_worktrees(conn, "proj", repo, ["", None])) == 1


def test_the_primary_checkout_is_never_snapshotted_or_reported_here(conn, repo):
    wt = _worktree(repo, "t1")
    _write(repo, "stray.txt")  # a dirty primary checkout: check_primary_checkout's business
    _commit_new_file(repo, "more.txt")  # and a moved one

    assert _idle(conn, repo) == []

    assert set(_snapshot_rows(conn)) == {_key(wt)}


def test_the_repo_argument_is_skipped_even_when_it_is_a_linked_worktree(conn, repo):
    a, b = _worktree(repo, "a"), _worktree(repo, "b")

    assert _idle(conn, a) == []

    assert set(_snapshot_rows(conn)) == {_key(b)}


def test_a_bare_repository_has_no_primary_worktree_to_skip_wrongly(conn, tmp_path, repo):
    bare = tmp_path / "bare.git"
    _git("clone", "-q", "--bare", str(repo), str(bare), cwd=tmp_path)
    _git("config", "user.email", "t@t", cwd=bare)  # a clone does not carry the fixture's local identity over
    _git("config", "user.name", "t", cwd=bare)
    wt = tmp_path / "linked"
    _git("worktree", "add", "-q", "-b", "b1", str(wt), "integration", cwd=bare)

    assert _idle(conn, bare) == []
    _commit_new_file(wt)

    assert len(_idle(conn, bare)) == 1
    assert set(_snapshot_rows(conn)) == {_key(wt)}


def test_a_worktree_that_disappears_drops_its_row_without_a_problem(conn, repo):
    a, b = _worktree(repo, "a"), _worktree(repo, "b")
    _idle(conn, repo)
    assert set(_snapshot_rows(conn)) == {_key(a), _key(b)}

    shutil.rmtree(b)  # gone behind git's back: git lists it as prunable
    assert _idle(conn, repo) == []
    assert set(_snapshot_rows(conn)) == {_key(a)}

    _git("worktree", "remove", "--force", str(a), cwd=repo)  # and one removed properly
    assert _idle(conn, repo) == []
    assert _snapshot_rows(conn) == {}


def test_a_stale_row_for_a_worktree_git_no_longer_lists_is_dropped(conn, repo):
    conn.execute(
        "INSERT INTO worktree_snapshots (project, path, head, status_hash, taken_at) VALUES ('proj', 'old', 'h', 's', 't')"
    )

    assert _idle(conn, repo) == []

    assert _snapshot_rows(conn) == {}


def test_a_worktree_that_appears_later_is_a_first_sight_not_a_problem(conn, repo):
    _worktree(repo, "a")
    _idle(conn, repo)
    b = _worktree(repo, "b")

    assert _idle(conn, repo) == []

    assert _key(b) in _snapshot_rows(conn)


def test_git_that_cannot_list_the_worktrees_is_one_problem_and_every_baseline_is_kept(conn, repo, monkeypatch):
    wt = _worktree(repo, "t1")
    _idle(conn, repo)
    before = _snapshot_rows(conn)
    old = _head(wt)
    new = _commit_new_file(wt)  # a change made while git is failing
    real_run = subprocess.run

    def run(cmd, **kwargs):
        if "worktree" in cmd:
            return subprocess.CompletedProcess(cmd, 128, b"", b"fatal: index file corrupt\n")
        return real_run(cmd, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(guards.subprocess, "run", run)
        problems = _idle(conn, repo)

    assert problems == [f"cannot list the worktrees of {repo}: fatal: index file corrupt"]
    assert _snapshot_rows(conn) == before
    assert _idle(conn, repo) == [_changed(repo, "t1", f"HEAD {old[:12]}..{new[:12]}")]  # git is back: still caught


@pytest.mark.parametrize("error", [FileNotFoundError("git"), subprocess.TimeoutExpired("git", 60)])
def test_a_git_that_cannot_run_is_a_problem_not_an_exception(conn, repo, monkeypatch, error):
    def fail(cmd, **kwargs):
        raise error

    monkeypatch.setattr(guards.subprocess, "run", fail)

    problems = _idle(conn, repo)

    assert len(problems) == 1
    assert problems[0].startswith(f"cannot list the worktrees of {repo}: ")


def test_a_directory_that_is_not_a_repository_is_a_problem_not_an_exception(conn, tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))

    problems = _idle(conn, plain)

    assert len(problems) == 1 and problems[0].startswith(f"cannot list the worktrees of {plain}: ")


def test_a_worktree_that_exists_but_cannot_be_read_is_a_problem_and_keeps_its_baseline(conn, repo, monkeypatch):
    wt = _worktree(repo, "t1")
    _idle(conn, repo)
    before = _snapshot_rows(conn)
    monkeypatch.setattr(guards, "snapshot_worktree", lambda path, **kwargs: ("", ""))

    problems = _idle(conn, repo)

    assert problems == [
        f"worktree {_listed(repo, 't1')} could not be inspected: git could not read its HEAD or its status"
    ]
    assert _snapshot_rows(conn) == before


def test_problem_messages_are_ascii_so_a_console_can_print_them(conn, repo):
    name = "caf\u00e9"
    wt = _worktree(repo, name, detach=True)
    _idle(conn, repo)
    _commit_new_file(wt)

    problems = _idle(conn, repo)

    assert len(problems) == 1
    problems[0].encode("ascii")  # would raise on a stray non-ASCII character
    assert "caf\\xe9" in problems[0]


def test_snapshots_are_kept_per_project(conn, repo):
    wt = _worktree(repo, "t1")
    _idle(conn, repo, project="p1")
    _commit_new_file(wt)

    assert _idle(conn, repo, project="p2") == []  # a first sight for p2
    assert len(_idle(conn, repo, project="p1")) == 1  # p1 has its own baseline and sees the change
    assert set(_snapshot_rows(conn, "p1")) == {_key(wt)} and set(_snapshot_rows(conn, "p2")) == {_key(wt)}


def test_ignore_prefixes_hide_changes_under_them_from_the_idle_check(conn, repo):
    wt = _worktree(repo, "t1")
    _idle(conn, repo, ignore_prefixes=(".cache/",))
    _write(wt, ".cache/blob.bin")

    assert _idle(conn, repo, ignore_prefixes=(".cache/",)) == []

    _write(wt, "real-change.txt")
    assert _idle(conn, repo, ignore_prefixes=(".cache/",)) == [_changed(repo, "t1", "status changed")]


def test_check_idle_worktrees_accepts_a_string_repo(conn, repo):
    _worktree(repo, "t1")

    assert guards.check_idle_worktrees(conn, "proj", str(repo), set()) == []
    assert len(_snapshot_rows(conn)) == 1


# refresh_snapshots

def test_refresh_snapshots_makes_a_change_ases_made_itself_quiet(conn, repo):
    wt = _worktree(repo, "t1")
    _idle(conn, repo)
    _commit_new_file(wt)  # ASES itself repointed the worktree

    assert guards.refresh_snapshots(conn, "proj", repo, set()) == 1

    assert _idle(conn, repo) == []


def test_refresh_snapshots_after_a_reported_change_keeps_the_next_pass_quiet_too(conn, repo):
    wt = _worktree(repo, "t1")
    _idle(conn, repo)
    _commit_new_file(wt, "first.txt")
    assert len(_idle(conn, repo)) == 1  # reported
    _commit_new_file(wt, "second.txt")  # and now ASES makes another change on purpose

    guards.refresh_snapshots(conn, "proj", repo, set())

    assert _idle(conn, repo) == []


def test_refresh_snapshots_returns_how_many_it_stored_and_skips_running_worktrees(conn, repo):
    a, b, c = _worktree(repo, "a"), _worktree(repo, "b"), _worktree(repo, "c")

    assert guards.refresh_snapshots(conn, "proj", repo, {str(b)}) == 2

    assert set(_snapshot_rows(conn)) == {_key(a), _key(c)}  # the running worktree has no baseline
    assert guards.refresh_snapshots(conn, "proj", repo, set()) == 3  # every idle worktree is stored again


def test_refresh_snapshots_takes_a_first_snapshot_of_a_worktree_never_seen(conn, repo):
    wt = _worktree(repo, "t1")
    _write(wt, "already-there.txt")

    assert guards.refresh_snapshots(conn, "proj", repo, set()) == 1

    assert _snapshot_rows(conn) == {_key(wt): guards.snapshot_worktree(wt)}
    assert _idle(conn, repo) == []


def test_refresh_snapshots_skips_the_primary_checkout_and_worktrees_it_cannot_use(conn, repo, monkeypatch):
    a, gone = _worktree(repo, "a"), _worktree(repo, "gone")
    shutil.rmtree(gone)

    assert guards.refresh_snapshots(conn, "proj", repo, set()) == 1

    assert set(_snapshot_rows(conn)) == {_key(a)}
    monkeypatch.setattr(guards, "snapshot_worktree", lambda path, **kwargs: ("", ""))
    assert guards.refresh_snapshots(conn, "proj", repo, set()) == 0  # unreadable: nothing stored, nothing counted


def test_refresh_snapshots_never_raises_and_stores_nothing_when_git_fails(conn, repo, monkeypatch):
    _worktree(repo, "t1")

    def fail(cmd, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(guards.subprocess, "run", fail)

    assert guards.refresh_snapshots(conn, "proj", repo, set()) == 0
    assert _snapshot_rows(conn) == {}


def test_refresh_snapshots_uses_the_same_ignore_prefixes_as_the_check(conn, repo):
    wt = _worktree(repo, "t1")
    _write(wt, ".cache/blob.bin")

    guards.refresh_snapshots(conn, "proj", repo, set(), ignore_prefixes=(".cache/",))

    assert _idle(conn, repo, ignore_prefixes=(".cache/",)) == []
