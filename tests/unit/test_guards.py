import re
import subprocess
from datetime import datetime, timedelta

import pytest

from ases import db, guards


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
