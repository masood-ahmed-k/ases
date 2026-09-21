import contextlib
import json
import os
import pathlib
import shutil
import sqlite3
import stat
import subprocess
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

from ases import db, events, hardening, hermes, intents

PROJECT = "p"
FAR_FUTURE = datetime.now(timezone.utc) + timedelta(days=2)  # every directory made by a test is "old" at this time


def _git(*args, cwd, check=True):
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if check:
        assert result.returncode == 0, f"git {' '.join(args)}: {result.stderr}"
    return result.stdout.strip()


def _norm(path):
    return os.path.normcase(os.path.realpath(str(path)))


class World:
    """A real repository (branch `integration`), an ASES database, a temp directory for merge candidates, and a fake
    Hermes board: cards live in `self.cards`, and `show` / `lst` are what gets injected into clean()."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.repo = tmp_path / "repo"
        self.repo.mkdir()
        _git("init", "-q", "-b", "integration", cwd=self.repo)
        _git("config", "user.email", "t@t", cwd=self.repo)
        _git("config", "user.name", "t", cwd=self.repo)
        _git("config", "core.autocrlf", "false", cwd=self.repo)
        self.write("base.txt", "base\n")
        self.commit("base")
        self.conn = db.connect(tmp_path / "ases.db")
        self.temp_root = tmp_path / "tmp"
        self.temp_root.mkdir()
        self.cards = {}
        self.list_error = None
        self.show_errors = set()
        self.calls = []

    # -- git ---------------------------------------------------------------------------------
    def git(self, *args, cwd=None, check=True):
        return _git(*args, cwd=cwd or self.repo, check=check)

    def write(self, rel, text="x\n", cwd=None):
        path = pathlib.Path(cwd or self.repo) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")

    def commit(self, message, cwd=None):
        self.git("add", "-A", cwd=cwd)
        self.git("commit", "-q", "-m", message, cwd=cwd)
        return self.git("rev-parse", "HEAD", cwd=cwd)

    def branch(self, name, files=None):
        """A branch off the integration tip, with one commit adding `files` ({path: text}) when given."""
        self.git("branch", name)
        if files:
            self.git("checkout", "-q", name)
            for rel, text in files.items():
                self.write(rel, text)
            self.commit(f"work on {name}")
            self.git("checkout", "-q", "integration")

    def squash_merge(self, branch, key, *, record=True, message=None):
        """What mergeq does to the integration branch: a squash commit, and (record=True) the completed merge record."""
        self.git("merge", "--squash", branch)
        sha = self.commit(message or f"{key}: merge {branch}")
        if record:
            self.conn.execute(
                "INSERT OR REPLACE INTO merge_records (task_key, candidate_sha, gate3_result, squash_commit, reverted, "
                "completed_at) VALUES (?, ?, 'pass', ?, 0, '2026-09-21T10:00:00')", (key, sha, sha))
        return sha

    def worktree(self, path, branch=None, *, detach=False):
        args = ["worktree", "add", "-q"]
        if detach:
            args.append("--detach")
        args.append(str(path))
        if branch:
            args.append(branch)
        self.git(*args)
        return pathlib.Path(path)

    def branches(self):
        return set(self.git("for-each-ref", "--format=%(refname:short)", "refs/heads").split())

    def worktree_paths(self):
        out = self.git("worktree", "list", "--porcelain")
        return {_norm(line[len("worktree "):]) for line in out.splitlines() if line.startswith("worktree ")}

    # -- the plan and the board ------------------------------------------------------------------
    def task(self, key, *, work="done", merge="done", work_branch=None, work_path=None):
        w, m = f"w_{key}", f"m_{key}"
        self.conn.execute(
            "INSERT INTO plan_tasks (project, task_key, work_card_id, merge_card_id, role, created_at) "
            "VALUES (?, ?, ?, ?, 'coder', datetime('now'))", (PROJECT, key, w, m))
        self.cards[w] = {"id": w, "status": work, "branch_name": work_branch or f"swarm/{key}-coder",
                         "workspace_path": str(work_path) if work_path else None}
        self.cards[m] = {"id": m, "status": merge, "branch_name": None, "workspace_path": None}

    def show(self, board, card_id):
        self.calls.append(("show", card_id))
        if card_id in self.show_errors or card_id not in self.cards:
            raise hermes.HermesCommandError(["kanban", "show", card_id], 1, "no such card")
        return dict(self.cards[card_id])

    def lst(self, board, *, status=None, assignee=None):
        self.calls.append(("list", status))
        if self.list_error:
            raise self.list_error
        return [dict(c) for c in self.cards.values() if c["status"] != "archived"]

    # -- the call under test ---------------------------------------------------------------------
    def clean(self, **kwargs):
        kwargs.setdefault("kanban_show", self.show)
        kwargs.setdefault("kanban_list", self.lst)
        kwargs.setdefault("temp_root", self.temp_root)
        return hardening.clean(self.repo, "integration", board="b", conn=self.conn, plan_project=PROJECT, **kwargs)

    def removal_events(self):
        rows = self.conn.execute("SELECT payload FROM events WHERE kind = 'hardening_removed' ORDER BY id").fetchall()
        return [json.loads(r[0]) for r in rows]

    def snapshot(self):
        """Everything a dry run must leave exactly as it was."""
        return (self.branches(), self.worktree_paths(),
                self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                sorted(p.name for p in self.temp_root.iterdir()))


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.conn.close()


def _kinds(items):
    return {(i.kind, i.name) for i in items}


def _names(items):
    return {i.name for i in items}


# ============================================================================================
# branches
# ============================================================================================

def _finished_squash_task(world, key, filename=None):
    """A task whose work branch was squash-merged the way ASES merges, with both cards done."""
    world.task(key)
    world.branch(f"swarm/{key}-coder", {filename or f"{key}.txt": f"{key}\n"})
    return world.squash_merge(f"swarm/{key}-coder", key)


def test_a_squash_merged_branch_of_a_finished_task_is_a_candidate_with_a_reason(world):
    _finished_squash_task(world, "T1")

    report = world.clean()

    assert [(c.kind, c.name) for c in report.candidates] == [("branch", "swarm/T1-coder")]
    assert "squash-merged by ASES" in report.candidates[0].reason and "task T1 is finished" in report.candidates[0].reason
    assert report.errors == [] and report.removed == []


def test_a_branch_that_is_an_ancestor_of_the_integration_branch_is_a_candidate(world):
    world.task("T1")
    world.branch("swarm/T1-coder", {"a.txt": "a\n"})
    world.git("merge", "--no-ff", "-q", "-m", "merge", "swarm/T1-coder")  # a plain merge: the branch IS an ancestor

    report = world.clean()

    assert _names(report.candidates) == {"swarm/T1-coder"}
    assert "fully merged into integration (git branch --merged)" in report.candidates[0].reason


def test_dry_run_changes_nothing_and_apply_removes_exactly_what_the_dry_run_listed(world):
    _finished_squash_task(world, "T1")
    world.task("T2")
    world.branch("swarm/T2-coder")  # no commit of its own: an ancestor
    world.task("T3")
    world.branch("swarm/T3-coder", {"t3.txt": "t3\n"})  # a commit the integration branch does not have
    before = world.snapshot()

    dry = world.clean()

    assert world.snapshot() == before  # not a branch, not a worktree, not an event
    assert dry.removed == [] and _names(dry.candidates) == {"swarm/T1-coder", "swarm/T2-coder"}
    assert "swarm/T3-coder" in _names(dry.skipped)
    applied = world.clean(apply=True)

    assert _kinds(applied.removed) == _kinds(dry.candidates)
    assert world.branches() == {"integration", "swarm/T3-coder"}
    assert applied.errors == []


def test_every_skipped_branch_says_why(world):
    world.task("T3")
    world.branch("swarm/T3-coder", {"t3.txt": "t3\n"})

    report = world.clean()

    [item] = report.skipped
    assert item.name == "swarm/T3-coder" and "not merged" in item.reason and "1 commit(s)" in item.reason


def test_an_unmerged_branch_with_a_squash_record_but_new_work_after_the_merge_is_kept(world):
    _finished_squash_task(world, "T1")
    world.git("checkout", "-q", "swarm/T1-coder")
    world.write("T1.txt", "more work after the merge\n")
    world.commit("late commit")
    world.git("checkout", "-q", "integration")

    report = world.clean(apply=True)

    assert report.candidates == [] and "swarm/T1-coder" in world.branches()
    assert "not merged" in report.skipped[0].reason or "differs" in report.skipped[0].reason
    assert "T1.txt" in report.skipped[0].reason


def test_a_later_task_editing_the_same_file_does_not_make_an_old_merged_branch_look_unmerged(world):
    # T1 creates app.py and is squash-merged; T2 (which depends on T1) edits app.py and is merged too. The integration
    # branch now differs from swarm/T1-coder in app.py, but T1's own squash commit does not.
    _finished_squash_task(world, "T1", "app.py")
    world.task("T2")
    world.branch("swarm/T2-coder", {"app.py": "T1\nT2 edit\n"})
    world.squash_merge("swarm/T2-coder", "T2")

    report = world.clean()

    assert _names(report.candidates) == {"swarm/T1-coder", "swarm/T2-coder"}


def test_the_squash_proof_holds_for_a_rename_and_a_deletion_that_were_merged(world):
    world.write("old_name.txt", "content\n")
    world.write("doomed.txt", "goes away\n")
    world.commit("more files")
    world.task("T1")
    world.git("branch", "swarm/T1-coder")
    world.git("checkout", "-q", "swarm/T1-coder")
    world.git("mv", "old_name.txt", "new_name.txt")  # a rename: with --no-renames it is a delete plus an add
    world.git("rm", "-q", "doomed.txt")
    world.commit("rename and delete")
    world.git("checkout", "-q", "integration")
    world.squash_merge("swarm/T1-coder", "T1")

    report = world.clean(apply=True)

    assert _names(report.removed) == {"swarm/T1-coder"} and "swarm/T1-coder" not in world.branches()
    assert not (world.repo / "doomed.txt").exists() and (world.repo / "new_name.txt").exists()


def test_a_deletion_the_squash_commit_does_not_have_keeps_the_branch(world):
    world.write("doomed.txt", "goes away\n")
    world.commit("a file")
    world.task("T1")
    world.git("branch", "swarm/T1-coder")
    world.git("checkout", "-q", "swarm/T1-coder")
    world.git("rm", "-q", "doomed.txt")
    world.commit("delete it")
    world.git("checkout", "-q", "integration")
    world.write("other.txt", "unrelated merge\n")
    other = world.commit("some other merge")  # the recorded squash commit is NOT this branch's: it kept doomed.txt
    world.conn.execute(
        "INSERT INTO merge_records (task_key, candidate_sha, gate3_result, squash_commit, reverted, completed_at) "
        "VALUES ('T1', ?, 'pass', ?, 0, '2026-09-21T10:00:00')", (other, other))

    report = world.clean(apply=True)

    assert report.removed == [] and "swarm/T1-coder" in world.branches()
    assert "doomed.txt" in report.skipped[0].reason


def test_a_merge_commit_inside_the_branch_does_not_hide_or_invent_changes(world):
    world.task("T1")
    world.git("branch", "swarm/T1-coder")
    world.write("i1.txt", "another task merged first\n")
    world.commit("I1: another task")
    world.git("checkout", "-q", "swarm/T1-coder")
    world.write("pre.txt", "the branch's own first commit\n")
    world.commit("pre")  # so the histories have diverged and the next merge is a real merge commit
    world.git("merge", "-q", "--no-edit", "integration")  # the worker merged the integration branch into its branch
    assert len(world.git("rev-list", "--parents", "-n", "1", "HEAD").split()) == 3  # a commit with two parents
    world.write("t1.txt", "t1 work\n")
    world.commit("t1 work")
    world.git("checkout", "-q", "integration")
    world.squash_merge("swarm/T1-coder", "T1")

    report = world.clean(apply=True)

    assert _names(report.removed) == {"swarm/T1-coder"}


def test_a_fix_branch_that_superseded_the_original_does_not_prove_the_original_merged(world):
    # T1's first attempt (swarm/T1-coder) never merged; its fix card's branch (swarm/T1-fix1) did, with different work.
    world.task("T1")
    world.branch("swarm/T1-coder", {"a.py": "first attempt\n"})
    world.branch("swarm/T1-fix1", {"a.py": "the fix\n"})
    world.squash_merge("swarm/T1-fix1", "T1")

    report = world.clean(apply=True)

    assert _names(report.removed) == {"swarm/T1-fix1"}  # the branch the record is about
    assert "swarm/T1-coder" in world.branches()  # the first attempt holds work that is in no merge
    assert "a.py" in {s.name: s.reason for s in report.skipped}["swarm/T1-coder"]


def test_a_squash_record_that_is_missing_incomplete_or_reverted_keeps_the_branch(world):
    _finished_squash_task(world, "T1")
    world.task("T2")
    world.branch("swarm/T2-coder", {"t2.txt": "t2\n"})
    world.squash_merge("swarm/T2-coder", "T2", record=False)  # merged in git, but no ASES record at all
    world.task("T3")
    world.branch("swarm/T3-coder", {"t3.txt": "t3\n"})
    world.squash_merge("swarm/T3-coder", "T3")
    world.conn.execute("UPDATE merge_records SET completed_at = NULL WHERE task_key = 'T3'")  # not completed
    world.task("T4")
    world.branch("swarm/T4-coder", {"t4.txt": "t4\n"})
    world.squash_merge("swarm/T4-coder", "T4")
    world.conn.execute("UPDATE merge_records SET reverted = 1 WHERE task_key = 'T4'")
    world.conn.execute("UPDATE merge_records SET reverted = 1 WHERE task_key = 'T1'")

    report = world.clean(apply=True)

    assert report.removed == []
    assert world.branches() >= {"swarm/T1-coder", "swarm/T2-coder", "swarm/T3-coder", "swarm/T4-coder"}
    reasons = {i.name: i.reason for i in report.skipped}
    assert "no completed merge record" in reasons["swarm/T2-coder"]
    assert "no completed merge record" in reasons["swarm/T3-coder"]
    assert "reverted" in reasons["swarm/T4-coder"] and "reverted" in reasons["swarm/T1-coder"]


def test_a_squash_commit_that_is_not_in_the_integration_branch_does_not_prove_a_merge(world):
    world.task("T1")
    world.branch("swarm/T1-coder", {"t1.txt": "t1\n"})
    orphan = world.git("commit-tree", "HEAD^{tree}", "-m", "elsewhere")  # a commit no branch holds
    world.conn.execute(
        "INSERT INTO merge_records (task_key, candidate_sha, gate3_result, squash_commit, reverted, completed_at) "
        "VALUES ('T1', ?, 'pass', ?, 0, '2026-09-21T10:00:00')", (orphan, orphan))

    report = world.clean(apply=True)

    assert report.removed == [] and "swarm/T1-coder" in world.branches()
    assert "is not in integration" in report.skipped[0].reason


def test_a_merge_branch_of_the_blueprint_naming_is_cleaned_like_a_work_branch(world):
    world.task("T5")
    world.branch("merge/T5")  # the blueprint's throwaway merge branch, left at the integration tip

    report = world.clean(apply=True)

    assert _names(report.removed) == {"merge/T5"} and "merge/T5" not in world.branches()


def test_the_integration_branch_and_branches_outside_swarm_and_merge_are_never_touched(world):
    world.task("T1")
    world.branch("swarm/T1-coder")
    world.git("branch", "feature/x")
    world.git("branch", "swarmish")
    world.git("branch", "release")

    world.clean(apply=True)

    assert world.branches() == {"integration", "feature/x", "swarmish", "release"}


def test_the_integration_branch_is_never_deleted_even_when_its_name_looks_like_a_task_branch(world):
    # An integration branch called merge/main, a task called main, the task finished, and the primary checkout on ANOTHER
    # branch (so being checked out does not protect it): only the guard on the integration branch stands in the way.
    world.git("branch", "-m", "integration", "merge/main")
    world.git("checkout", "-q", "-b", "elsewhere")
    world.task("main")

    report = hardening.clean(world.repo, "merge/main", board="b", conn=world.conn, plan_project=PROJECT, apply=True,
                             kanban_show=world.show, kanban_list=world.lst, temp_root=world.temp_root)

    assert "merge/main" in world.branches()
    assert "merge/main" not in _names(report.candidates) and "merge/main" not in _names(report.removed)


def test_a_branch_of_no_task_of_the_plan_is_skipped_with_that_reason(world):
    world.branch("swarm/ZZ-coder")

    report = world.clean(apply=True)

    assert report.removed == [] and "swarm/ZZ-coder" in world.branches()
    assert "no task of plan p owns this branch" in report.skipped[0].reason


def test_a_task_with_an_unfinished_card_keeps_its_branches(world):
    world.task("T1", work="running")
    world.branch("swarm/T1-coder")
    world.task("T2", work="done", merge="blocked")
    world.branch("swarm/T2-coder")
    world.task("T3", work="review", merge="blocked")
    world.branch("swarm/T3-coder")
    world.task("T4", work="ready", merge="blocked")
    world.branch("swarm/T4-coder")
    world.task("T5", work="scheduled", merge="blocked")
    world.branch("swarm/T5-coder")

    report = world.clean(apply=True)

    assert report.removed == []
    assert world.branches() >= {f"swarm/T{i}-coder" for i in range(1, 6)}
    reasons = {i.name: i.reason for i in report.skipped}
    assert "is running" in reasons["swarm/T1-coder"] and "is blocked" in reasons["swarm/T2-coder"]
    assert "is review" in reasons["swarm/T3-coder"] and "is ready" in reasons["swarm/T4-coder"]
    assert "is scheduled" in reasons["swarm/T5-coder"]


def test_a_task_whose_work_card_is_done_and_merge_card_archived_is_finished(world):
    world.task("T1", work="done", merge="archived")
    world.branch("swarm/T1-coder")

    assert _names(world.clean().candidates) == {"swarm/T1-coder"}


def test_a_status_that_is_not_done_or_archived_never_counts_as_finished(world):
    world.task("T1", work="triage")
    world.branch("swarm/T1-coder")
    world.task("T2", work="some_new_status")
    world.branch("swarm/T2-coder")

    report = world.clean()

    assert report.candidates == []
    assert "is triage" in {i.name: i.reason for i in report.skipped}["swarm/T1-coder"]
    assert "some_new_status" in {i.name: i.reason for i in report.skipped}["swarm/T2-coder"]


def test_a_card_that_cannot_be_read_is_skipped_and_reported_and_the_others_still_go(world):
    world.task("T1")
    world.branch("swarm/T1-coder")
    world.task("T2")
    world.branch("swarm/T2-coder")
    world.show_errors.add("m_T1")

    report = world.clean(apply=True)

    assert _names(report.removed) == {"swarm/T2-coder"}
    assert "swarm/T1-coder" in world.branches()
    [item] = [i for i in report.skipped if i.name == "swarm/T1-coder"]
    assert "m_T1" in item.reason and "could not be read" in item.reason


def test_a_task_with_no_card_recorded_is_not_finished(world):
    world.task("T1")
    world.conn.execute("UPDATE plan_tasks SET merge_card_id = NULL WHERE task_key = 'T1'")
    world.branch("swarm/T1-coder")

    report = world.clean()

    assert report.candidates == [] and "has no merge card recorded" in report.skipped[0].reason


def test_a_listed_active_card_that_names_the_branch_protects_it(world):
    # a retry or hand-made card that plan_tasks does not track, on a branch of a task that is otherwise finished
    world.task("T1")
    world.branch("swarm/T1-coder")
    world.cards["t_other"] = {"id": "t_other", "status": "running", "branch_name": "swarm/T1-coder", "workspace_path": None}

    report = world.clean(apply=True)

    assert report.removed == [] and "swarm/T1-coder" in world.branches()
    assert "card t_other is running" in report.skipped[0].reason


def test_when_the_card_list_cannot_be_read_nothing_is_removed_and_the_error_is_reported(world):
    _finished_squash_task(world, "T1")
    world.list_error = hermes.HermesCommandError(["kanban", "list"], 1, "hermes is down")

    report = world.clean(apply=True)

    assert report.removed == [] and report.candidates == []
    assert "swarm/T1-coder" in world.branches()
    assert any("could not list the cards" in e.reason for e in report.errors)
    assert any("cards could not be listed" in s.reason for s in report.skipped)


def test_the_checked_out_branch_is_never_removed_even_when_merged_and_finished(world):
    world.task("T1")
    world.branch("swarm/T1-coder")
    world.git("checkout", "-q", "swarm/T1-coder")  # the primary checkout is ON it

    report = world.clean(apply=True)

    assert report.removed == [] and "swarm/T1-coder" in world.branches()
    assert "checked out in the primary checkout" in report.skipped[0].reason


def test_a_branch_with_an_open_worktree_is_kept(world):
    world.task("T1")
    world.branch("swarm/T1-coder")
    other = world.worktree(world.tmp / "somebody-elses-worktree", "swarm/T1-coder")

    report = world.clean(apply=True)

    assert report.removed == [] and "swarm/T1-coder" in world.branches() and other.is_dir()
    assert "checked out in worktree" in report.skipped[0].reason


def test_a_branch_that_moves_between_the_check_and_the_delete_is_left_alone(world, monkeypatch):
    world.task("T1")
    world.branch("swarm/T1-coder")
    real = hardening._git

    def moving(repo, args, **kwargs):
        if args[:3] == ["rev-parse", "--verify", "-q"] and args[3] == "refs/heads/swarm/T1-coder":
            tree = real(repo, ["rev-parse", "HEAD^{tree}"])[1].strip()
            tip = real(repo, ["rev-parse", "swarm/T1-coder"])[1].strip()
            new = real(repo, ["commit-tree", tree, "-p", tip, "-m", "a worker committed just now"], read_only=False)[1].strip()
            real(repo, ["update-ref", "refs/heads/swarm/T1-coder", new], read_only=False)
        return real(repo, args, **kwargs)

    monkeypatch.setattr(hardening, "_git", moving)

    report = world.clean(apply=True)

    assert report.removed == [] and "swarm/T1-coder" in world.branches()
    assert any("moved or vanished" in e.reason for e in report.errors)


def test_a_failed_deletion_is_an_error_and_the_rest_still_go(world, monkeypatch):
    world.task("T1")
    world.branch("swarm/T1-coder")
    world.task("T2")
    world.branch("swarm/T2-coder")
    real = hardening._git

    def refusing(repo, args, **kwargs):
        if args[:1] == ["branch"] and args[-1] == "swarm/T1-coder":
            return 1, "", "error: cannot delete branch 'swarm/T1-coder': pretend refusal"
        return real(repo, args, **kwargs)

    monkeypatch.setattr(hardening, "_git", refusing)

    report = world.clean(apply=True)

    assert _names(report.removed) == {"swarm/T2-coder"}
    [error] = report.errors
    assert error.name == "swarm/T1-coder" and "pretend refusal" in error.reason
    assert "swarm/T1-coder" in world.branches()


def test_task_key_mapping_is_exact_and_a_name_that_fits_two_tasks_maps_to_none():
    keys = ["T1", "T1-a", "T10"]

    assert hardening._task_for_branch("swarm/T1-coder", keys) == "T1"
    assert hardening._task_for_branch("swarm/T10-coder", keys) == "T10"
    assert hardening._task_for_branch("swarm/T1-fix2", keys) == "T1"
    assert hardening._task_for_branch("merge/T10", keys) == "T10"
    assert hardening._task_for_branch("swarm/T2-coder", keys) is None
    assert hardening._task_for_branch("swarm/T", keys) is None
    assert hardening._task_for_branch("feature/T1-x", keys) is None
    assert hardening._task_for_branch("swarm/T1", keys) == "T1"
    # swarm/T1-a-coder is task T1's branch for a role called "a-coder", or task T1-a's branch: it cannot be told, so
    # neither is assumed (deciding a live branch's fate from the wrong task could delete it)
    assert hardening._tasks_for_branch("swarm/T1-a-coder", keys) == ["T1", "T1-a"]
    assert hardening._task_for_branch("swarm/T1-a-coder", keys) is None
    assert hardening._task_for_branch("swarm/T1-a-coder", ["T1-a", "T10"]) == "T1-a"
    assert hardening._tasks_for_branch("feature/x", keys) == []


def test_a_branch_that_fits_two_task_keys_is_left_alone_and_says_so(world):
    world.task("T1")
    world.branch("swarm/T1-coder")
    world.task("T1-a")
    world.branch("swarm/T1-a-coder")

    report = world.clean(apply=True)

    assert _names(report.removed) == {"swarm/T1-coder"}  # the unambiguous one goes
    assert "swarm/T1-a-coder" in world.branches()
    [item] = [s for s in report.skipped if s.name == "swarm/T1-a-coder"]
    assert "ambiguous" in item.reason and "T1, T1-a" in item.reason


def test_clean_never_removes_the_worktree_it_was_pointed_at(world):
    path = _card_worktree(world, "T1")
    inside = path / "sub"
    inside.mkdir()

    for where in (path, inside):
        for apply in (False, True):
            report = hardening.clean(where, "integration", board="b", conn=world.conn, plan_project=PROJECT, apply=apply,
                                     kanban_show=world.show, kanban_list=world.lst, temp_root=world.temp_root)
            # not even PLANNED (on Windows the OS would refuse the removal of a working directory anyway, on other
            # systems it would not, so what is asserted is the plan and the absence of a refused attempt)
            assert "card_worktree" not in {c.kind for c in report.candidates}
            assert report.errors == [] and "card_worktree" not in {r.kind for r in report.removed}
            assert path.is_dir() and "swarm/T1-coder" in world.branches()


def test_every_removal_is_an_event_and_a_dry_run_records_none(world):
    _finished_squash_task(world, "T1")
    world.task("T2")
    world.branch("swarm/T2-coder")
    tip = world.git("rev-parse", "swarm/T1-coder")

    world.clean()
    assert world.removal_events() == []
    world.clean(apply=True)

    payloads = {p["name"]: p for p in world.removal_events()}
    assert set(payloads) == {"swarm/T1-coder", "swarm/T2-coder"}
    assert payloads["swarm/T1-coder"]["kind"] == "branch" and payloads["swarm/T1-coder"]["sha"] == tip
    assert payloads["swarm/T1-coder"]["project"] == PROJECT and payloads["swarm/T1-coder"]["reason"]


def test_a_deleted_branch_can_be_restored_from_the_sha_in_its_event(world):
    _finished_squash_task(world, "T1")
    tip = world.git("rev-parse", "swarm/T1-coder")
    world.clean(apply=True)
    assert "swarm/T1-coder" not in world.branches()

    recorded = [p for p in world.removal_events() if p["name"] == "swarm/T1-coder"][0]["sha"]
    world.git("branch", "swarm/T1-coder", recorded)

    assert world.git("rev-parse", "swarm/T1-coder") == tip


# ============================================================================================
# worktrees
# ============================================================================================

def test_a_registered_worktree_whose_directory_is_gone_is_pruned(world):
    gone = world.worktree(world.tmp / "wt-gone", detach=True)
    shutil.rmtree(gone)
    assert _norm(gone) in world.worktree_paths()

    dry = world.clean()
    assert [(c.kind, _norm(c.name)) for c in dry.candidates] == [("stale_worktree", _norm(gone))]
    assert "directory is gone" in dry.candidates[0].reason
    assert _norm(gone) in world.worktree_paths()  # a dry run left the registration

    applied = world.clean(apply=True)

    assert len(applied.removed) == 1 and applied.errors == []
    assert _norm(gone) not in world.worktree_paths()
    assert [p["kind"] for p in world.removal_events()] == ["stale_worktree"]


def test_a_stale_registration_of_an_active_card_is_left_alone(world):
    world.task("T1", work="running")
    path = world.repo / ".worktrees" / "w_T1"
    world.branch("swarm/T1-coder")
    world.worktree(path, "swarm/T1-coder")
    world.cards["w_T1"]["workspace_path"] = str(path)
    shutil.rmtree(path)

    report = world.clean(apply=True)

    assert report.removed == [] and _norm(path) in world.worktree_paths()
    assert "card w_T1 is running" in report.skipped[0].reason


def test_a_locked_worktree_whose_directory_is_gone_is_never_pruned(world):
    gone = world.worktree(world.tmp / "wt-locked", detach=True)
    world.git("worktree", "lock", str(gone))
    shutil.rmtree(gone)

    report = world.clean(apply=True)

    assert report.removed == [] and _norm(gone) in world.worktree_paths()


def test_a_worktree_somebody_made_by_hand_is_none_of_cleans_business(world):
    hand = world.worktree(world.tmp / "my-own-worktree", detach=True)

    report = world.clean(apply=True)

    assert report.removed == [] and report.candidates == [] and hand.is_dir()


def _leftover_candidate(world, name="ases-merge-abc123"):
    root = world.temp_root / name
    path = world.worktree(root / "candidate", detach=True)
    return root, path


def test_a_leftover_merge_candidate_worktree_is_removed_with_its_temp_directory(world):
    root, path = _leftover_candidate(world)

    dry = world.clean(now=FAR_FUTURE)
    assert [c.kind for c in dry.candidates] == ["candidate_worktree"] and path.is_dir()
    assert "no merge record" in dry.candidates[0].reason

    applied = world.clean(now=FAR_FUTURE, apply=True)

    assert len(applied.removed) == 1 and applied.errors == []
    assert not path.exists() and not root.exists()  # the empty ases-merge-XXXX directory goes too
    assert _norm(path) not in world.worktree_paths()


def test_a_dirty_leftover_candidate_is_removed_because_a_killed_build_leaves_staged_work_in_it(world):
    root, path = _leftover_candidate(world)
    world.write("half_built.txt", "staged by `git merge --squash` when the build was killed\n", cwd=path)
    world.git("add", "half_built.txt", cwd=path)
    world.write("untracked.txt", "left by a gate command\n", cwd=path)

    report = world.clean(now=FAR_FUTURE, apply=True)

    assert len(report.removed) == 1 and report.errors == []
    assert not path.exists() and not root.exists()


def test_a_candidate_whose_merge_record_is_completed_is_removed_and_one_whose_record_is_not_is_left(world):
    _root_a, done_path = _leftover_candidate(world, "ases-merge-done")
    done_sha = world.git("rev-parse", "HEAD", cwd=done_path)
    world.conn.execute("INSERT INTO merge_records (task_key, candidate_sha, gate3_result, squash_commit, reverted, "
                       "completed_at) VALUES ('T1', ?, 'pass', ?, 0, '2026-09-21T10:00:00')", (done_sha, done_sha))
    _root_b, open_path = _leftover_candidate(world, "ases-merge-open")
    world.write("staged.txt", "candidate work\n", cwd=open_path)
    open_sha = world.commit("candidate commit", cwd=open_path)
    world.conn.execute("INSERT INTO merge_records (task_key, candidate_sha, gate3_result, squash_commit, reverted, "
                       "completed_at) VALUES ('T2', ?, 'pass', NULL, 0, NULL)", (open_sha,))

    report = world.clean(now=FAR_FUTURE, apply=True)

    assert [_norm(i.name) for i in report.removed] == [_norm(done_path)]
    assert open_path.is_dir()  # reconcile owns an unfinished candidate
    [skipped] = [s for s in report.skipped if _norm(s.name) == _norm(open_path)]
    assert "T2" in skipped.reason and "not completed" in skipped.reason and "reconcile" in skipped.reason


def test_a_candidate_that_is_too_young_is_left_alone_because_a_merge_may_be_using_it(world):
    _root, path = _leftover_candidate(world)

    report = world.clean(apply=True)  # the real clock: the directory is seconds old

    assert report.removed == [] and path.is_dir()
    assert "minute(s) old" in report.skipped[0].reason
    assert world.clean(now=FAR_FUTURE, candidate_min_age_minutes=60).candidates  # ... and old once time has passed


def test_the_minimum_age_of_a_candidate_is_a_parameter(world):
    _root, path = _leftover_candidate(world)
    later = datetime.now(timezone.utc) + timedelta(minutes=10)

    assert world.clean(now=later, candidate_min_age_minutes=60).candidates == []
    assert len(world.clean(now=later, candidate_min_age_minutes=5).candidates) == 1


def test_an_open_candidate_build_intent_blocks_removal_of_candidate_worktrees(world):
    _root, path = _leftover_candidate(world)
    intents.begin(world.conn, PROJECT, intents.KIND_BUILD_CANDIDATE, "T1")

    report = world.clean(now=FAR_FUTURE, apply=True)

    assert report.removed == [] and path.is_dir()
    assert "build_candidate intent for T1 is still open" in report.skipped[0].reason
    assert "reconcile" in report.skipped[0].reason


def test_an_open_fast_forward_intent_also_blocks_it_but_an_unrelated_intent_does_not(world):
    _root, path = _leftover_candidate(world)
    intents.begin(world.conn, PROJECT, intents.KIND_CREATE_CARDS, PROJECT)
    assert len(world.clean(now=FAR_FUTURE).candidates) == 1
    intents.begin(world.conn, PROJECT, intents.KIND_FAST_FORWARD, "T2")

    assert world.clean(now=FAR_FUTURE).candidates == []


def test_a_worktree_in_the_temp_directory_without_the_ases_merge_name_is_not_touched(world):
    other = world.worktree(world.temp_root / "somebody-else" / "wt", detach=True)

    report = world.clean(now=FAR_FUTURE, apply=True)

    assert report.removed == [] and other.is_dir()


def test_a_worktree_named_ases_merge_outside_the_temp_directory_is_not_touched(world):
    elsewhere = world.worktree(world.tmp / "ases-merge-xyz" / "candidate", detach=True)

    report = world.clean(now=FAR_FUTURE, apply=True)

    assert report.removed == [] and elsewhere.is_dir()


def test_the_temp_directory_itself_is_a_parameter_and_defaults_to_the_system_one(world, monkeypatch):
    root, path = _leftover_candidate(world)
    monkeypatch.setattr(hardening.tempfile, "gettempdir", lambda: str(world.temp_root))

    report = world.clean(now=FAR_FUTURE, temp_root=None)

    assert len(report.candidates) == 1


def _card_worktree(world, key, *, status="done", merge="done", dirty=False):
    """A worktree where Hermes puts a project card's: <repo>/.worktrees/<card id>, on the task's branch."""
    world.task(key, work=status, merge=merge)
    world.branch(f"swarm/{key}-coder")
    path = world.repo / ".worktrees" / f"w_{key}"
    world.worktree(path, f"swarm/{key}-coder")
    world.cards[f"w_{key}"]["workspace_path"] = str(path)
    if dirty:
        world.write("scratch.txt", "not committed\n", cwd=path)
    return path


def test_the_worktree_of_a_finished_card_and_then_its_branch_are_removed_in_one_run(world):
    path = _card_worktree(world, "T1")

    dry = world.clean()
    assert {c.kind for c in dry.candidates} == {"card_worktree", "branch"}  # the branch is judged as if the tree were gone
    assert path.is_dir() and "swarm/T1-coder" in world.branches()

    applied = world.clean(apply=True)

    assert {r.kind for r in applied.removed} == {"card_worktree", "branch"} and applied.errors == []
    assert not path.exists() and "swarm/T1-coder" not in world.branches()
    assert _norm(path) not in world.worktree_paths()


def test_a_dirty_finished_card_worktree_is_kept_and_so_is_its_branch(world):
    path = _card_worktree(world, "T1", dirty=True)

    report = world.clean(apply=True)

    assert report.removed == [] and path.is_dir() and "swarm/T1-coder" in world.branches()
    reasons = " | ".join(s.reason for s in report.skipped)
    assert "uncommitted or untracked" in reasons and "checked out in worktree" in reasons


def test_a_worktree_with_a_modified_tracked_file_is_kept(world):
    path = _card_worktree(world, "T1")
    world.write("base.txt", "edited by the worker, never committed\n", cwd=path)

    assert world.clean(apply=True).removed == []
    assert path.is_dir()


@pytest.mark.parametrize("status", ["running", "review", "ready", "blocked", "scheduled", "todo", "triage"])
def test_the_worktree_of_a_card_that_is_not_finished_is_never_removed(world, status):
    path = _card_worktree(world, "T1", status=status, merge="blocked")

    report = world.clean(apply=True)

    assert report.removed == [] and path.is_dir() and "swarm/T1-coder" in world.branches()


def test_a_finished_card_whose_task_is_not_finished_keeps_its_worktree(world):
    path = _card_worktree(world, "T1", status="done", merge="blocked")  # the work is done, the merge has not happened

    report = world.clean(apply=True)

    assert report.removed == [] and path.is_dir()
    assert "merge card m_T1 of task T1 is blocked" in " | ".join(s.reason for s in report.skipped)


def test_a_card_worktree_whose_card_cannot_be_read_is_kept(world):
    path = _card_worktree(world, "T1")
    world.show_errors.add("w_T1")

    report = world.clean(apply=True)

    assert report.removed == [] and path.is_dir()
    assert "could not be read" in " | ".join(s.reason for s in report.skipped)


def test_a_card_that_records_a_different_workspace_path_keeps_the_worktree(world):
    path = _card_worktree(world, "T1")
    world.cards["w_T1"]["workspace_path"] = str(world.tmp / "somewhere-else")

    assert world.clean(apply=True).removed == []
    assert path.is_dir()


def test_a_card_worktree_of_no_task_of_the_plan_is_kept(world):
    world.cards["w_X"] = {"id": "w_X", "status": "done", "branch_name": "swarm/X-coder", "workspace_path": None}
    world.branch("swarm/X-coder")
    path = world.repo / ".worktrees" / "w_X"
    world.worktree(path, "swarm/X-coder")

    report = world.clean(apply=True)

    assert report.removed == [] and path.is_dir()
    assert "is not a card of plan p" in " | ".join(s.reason for s in report.skipped)


def test_card_worktrees_can_be_switched_off_and_their_branches_then_stay_too(world):
    path = _card_worktree(world, "T1")

    report = world.clean(apply=True, card_worktrees=False)

    assert report.removed == [] and path.is_dir() and "swarm/T1-coder" in world.branches()


def test_a_locked_card_worktree_is_kept(world):
    path = _card_worktree(world, "T1")
    world.git("worktree", "lock", str(path))

    report = world.clean(apply=True)

    assert report.removed == [] and path.is_dir()
    # the exact phrase: the word "locked" alone would also match the temp path, which is named after this test
    assert "locked (git worktree unlock it first)" in " | ".join(s.reason for s in report.skipped)
    assert report.errors == []  # it was refused by ASES, not by git after a removal was attempted


def test_a_detached_card_worktree_on_commits_no_branch_holds_is_kept_but_one_on_a_branch_tip_goes(world):
    world.task("T1")
    path = world.repo / ".worktrees" / "w_T1"
    world.worktree(path, detach=True)
    world.cards["w_T1"]["workspace_path"] = str(path)
    world.write("lost.txt", "commit only this worktree has\n", cwd=path)
    world.commit("lonely commit", cwd=path)
    world.git("branch", "swarm/T1-coder", "integration")

    keep = world.clean(apply=True)
    assert "card_worktree" not in {r.kind for r in keep.removed} and path.is_dir()  # (the task's own branch may go)
    assert "no local branch holds" in " | ".join(s.reason for s in keep.skipped)

    world.git("branch", "-f", "saved", "HEAD", cwd=path)  # now a branch holds the commit
    assert "card_worktree" in {r.kind for r in world.clean(apply=True).removed} and not path.exists()


def test_a_git_refusal_on_a_card_worktree_is_an_error_line_and_the_rest_continue(world, monkeypatch):
    first = _card_worktree(world, "T1")
    second = _card_worktree(world, "T2")
    real = hardening._git

    def refusing(repo, args, **kwargs):
        if args[:2] == ["worktree", "remove"] and _norm(args[-1]) == _norm(first):
            return 128, "", "fatal: pretend a process holds this directory"
        return real(repo, args, **kwargs)

    monkeypatch.setattr(hardening, "_git", refusing)

    report = world.clean(apply=True)

    assert not second.exists() and first.is_dir()
    assert any("pretend a process holds" in e.reason for e in report.errors)
    assert "swarm/T2-coder" not in world.branches() and "swarm/T1-coder" in world.branches()


# ============================================================================================
# the contract: injection, never raising, ASCII
# ============================================================================================

def test_the_hermes_reads_default_to_the_hermes_module_resolved_at_call_time(world, monkeypatch):
    world.task("T1")
    world.branch("swarm/T1-coder")
    monkeypatch.setattr(hermes, "kanban_show", world.show)
    monkeypatch.setattr(hermes, "kanban_list", world.lst)

    report = hardening.clean(world.repo, "integration", board="b", conn=world.conn, plan_project=PROJECT,
                             temp_root=world.temp_root)

    assert _names(report.candidates) == {"swarm/T1-coder"}
    assert ("list", None) in world.calls and ("show", "w_T1") in world.calls


def test_a_directory_that_is_not_a_repository_gives_an_error_report_not_an_exception(world, tmp_path):
    empty = tmp_path / "not-a-repo"
    empty.mkdir()

    report = hardening.clean(empty, "integration", board="b", conn=world.conn, plan_project=PROJECT,
                             kanban_show=world.show, kanban_list=world.lst)

    assert report.candidates == [] and len(report.errors) == 1
    assert "not a git repository" in report.errors[0].reason


def test_a_missing_directory_gives_an_error_report(world, tmp_path):
    report = hardening.clean(tmp_path / "nope", "integration", board="b", conn=world.conn, plan_project=PROJECT,
                             kanban_show=world.show, kanban_list=world.lst)

    assert report.errors and report.candidates == []


def test_a_missing_integration_branch_gives_an_error_and_removes_nothing(world):
    world.task("T1")
    world.branch("swarm/T1-coder")

    report = hardening.clean(world.repo, "no-such-branch", board="b", conn=world.conn, plan_project=PROJECT,
                             apply=True, kanban_show=world.show, kanban_list=world.lst)

    assert report.removed == [] and "swarm/T1-coder" in world.branches()
    assert "integration branch does not exist" in report.errors[0].reason


def test_no_database_connection_gives_an_error_and_removes_nothing(world):
    world.task("T1")
    world.branch("swarm/T1-coder")

    report = hardening.clean(world.repo, "integration", board="b", conn=None, plan_project=PROJECT, apply=True,
                             kanban_show=world.show, kanban_list=world.lst)

    assert report.removed == [] and report.errors and "swarm/T1-coder" in world.branches()


def test_a_git_that_cannot_be_run_gives_an_error_report(world, monkeypatch):
    def broken(*args, **kwargs):
        raise FileNotFoundError("git is not installed")

    monkeypatch.setattr(hardening.subprocess, "run", broken)

    report = world.clean(apply=True)

    assert report.removed == [] and any("git could not be run" in e.reason or "not a git repository" in e.reason
                                        for e in report.errors)


def test_an_unexpected_exception_inside_a_phase_becomes_an_error_and_the_report_still_returns(world, monkeypatch):
    world.task("T1")
    world.branch("swarm/T1-coder")
    monkeypatch.setattr(hardening._Cleaner, "_local_branches", lambda self: (_ for _ in ()).throw(RuntimeError("boom")))

    report = world.clean(apply=True)

    assert report.removed == [] and any("boom" in e.reason for e in report.errors)


def test_an_unexpected_exception_anywhere_never_escapes_clean(world, monkeypatch):
    monkeypatch.setattr(hardening._Cleaner, "run", lambda self: (_ for _ in ()).throw(ValueError("surprise")))

    report = world.clean()

    assert isinstance(report, hardening.CleanReport) and "surprise" in report.errors[0].reason


def test_a_failing_event_write_is_an_error_line_but_the_removal_stands(world, monkeypatch):
    world.task("T1")
    world.branch("swarm/T1-coder")
    monkeypatch.setattr(hardening.events_mod, "record", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))

    report = world.clean(apply=True)

    assert _names(report.removed) == {"swarm/T1-coder"} and "swarm/T1-coder" not in world.branches()
    assert any("event could not be written" in e.reason for e in report.errors)


def test_the_report_is_ascii_even_for_accented_names_and_titles(world):
    accent = chr(0xE9)
    world.task("T1")
    world.branch(f"swarm/T1-caf{accent}")
    world.branch("swarm/T2-coder")
    world.cards["w_T1"]["status"] = "running"
    hard = world.worktree(world.tmp / f"wt-{accent}-gone", detach=True)
    shutil.rmtree(hard)

    report = world.clean()
    text = hardening.format_clean_report(report)

    assert text.isascii()
    for item in [*report.candidates, *report.skipped, *report.errors]:
        assert item.name.isascii() and item.reason.isascii()
    assert "\\xe9" in text  # escaped, not dropped


def test_the_formatted_report_lists_candidates_skips_errors_and_says_what_a_dry_run_did(world):
    _finished_squash_task(world, "T1")
    world.task("T3")
    world.branch("swarm/T3-coder", {"t3.txt": "t3\n"})

    dry_text = hardening.format_clean_report(world.clean())
    applied_text = hardening.format_clean_report(world.clean(apply=True))

    assert "DRY RUN" in dry_text and "Would remove (1)" in dry_text and "[branch] swarm/T1-coder" in dry_text
    assert "Left alone (1)" in dry_text and "swarm/T3-coder" in dry_text and "--apply" in dry_text
    assert "nothing was removed" in dry_text
    assert "APPLIED" in applied_text and "Removed (1)" in applied_text and "Removed 1 of 1 candidate(s)" in applied_text


def test_a_run_with_nothing_to_do_says_so(world):
    text = hardening.format_clean_report(world.clean())

    assert "Would remove (0)" in text and "(none)" in text and "nothing to remove" in text


# ============================================================================================
# retention
# ============================================================================================

DAY = 86400.0


def _make(path, size=10, age_days=0.0, now=None):
    """A file of `size` bytes whose modification time is `age_days` before `now`."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    moment = (now or datetime.now(timezone.utc)).timestamp() - age_days * DAY
    os.utime(path, (moment, moment))
    return path


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "data"
    h.mkdir()
    return h


def test_retention_dry_run_removes_nothing_and_apply_removes_only_the_old_files_beyond_the_newest_kept(home):
    old = [_make(home / "logs" / f"old{i}.log", 100, age_days=40 + i) for i in range(5)]
    new = [_make(home / "logs" / f"new{i}.log", 100, age_days=i) for i in range(2)]
    dry = hardening.retention(home, 30, keep_latest=1)

    assert all(p.exists() for p in old + new)
    # five old files; the single newest of the kind (new0) is protected by keep_latest but is not old anyway
    assert len(dry.candidates) == 5 and dry.removed == [] and dry.bytes_candidate == 500 and dry.bytes_freed == 0

    applied = hardening.retention(home, 30, keep_latest=1, apply=True)

    assert not any(p.exists() for p in old) and all(p.exists() for p in new)
    assert len(applied.removed) == 5 and applied.bytes_freed == 500 and applied.errors == []


def test_keep_latest_keeps_the_newest_entries_of_each_kind_whatever_their_age(home):
    logs = [_make(home / "logs" / f"l{i}.log", 10, age_days=100 + i) for i in range(5)]  # l0 is the newest
    reports = [_make(home / "reports" / "proj" / f"r{i}" / "report.json", 10, age_days=200 + i) for i in range(4)]

    report = hardening.retention(home, 30, keep_latest=3, apply=True)

    assert [p.exists() for p in logs] == [True, True, True, False, False]
    assert [p.exists() for p in reports] == [True, True, True, False]
    assert len(report.kept) == 6 and len(report.removed) == 3


def test_keep_latest_zero_removes_every_old_entry_and_the_default_is_three(home):
    files = [_make(home / "stops" / f"s{i}.json", 10, age_days=90 + i) for i in range(4)]

    dry = hardening.retention(home, 30)
    assert dry.keep_latest == 3 and len(dry.candidates) == 1  # 4 old, the newest 3 kept
    hardening.retention(home, 30, keep_latest=0, apply=True)

    assert not any(p.exists() for p in files)


def test_each_of_the_four_directories_is_covered_and_others_are_ignored(home):
    old = {kind: _make(home / kind / "f.txt", 10, age_days=99) for kind in ("logs", "reports", "stops", "evals")}
    ignored = [_make(home / "other" / "f.txt", 10, age_days=99), _make(home / "notes.txt", 10, age_days=99),
               _make(home / "workspaces" / "f.txt", 10, age_days=99)]

    report = hardening.retention(home, 30, keep_latest=0, apply=True)

    assert not any(p.exists() for p in old.values()) and all(p.exists() for p in ignored)
    assert {r.kind for r in report.removed} == {"logs", "reports", "stops", "evals"}


def test_a_report_directory_is_one_entry_aged_by_its_newest_file_and_empty_parents_are_removed(home):
    mixed = home / "reports" / "proj" / "20260101T000000Z"
    _make(mixed / "index.html", 10, age_days=90)
    _make(mixed / "report.json", 10, age_days=5)  # one fresh file keeps the whole report
    both_old = home / "reports" / "proj" / "20250101T000000Z"
    _make(both_old / "index.html", 10, age_days=91)
    _make(both_old / "report.json", 10, age_days=91)

    report = hardening.retention(home, 30, keep_latest=0, apply=True)

    assert (mixed / "index.html").exists() and (mixed / "report.json").exists()
    assert not both_old.exists() and (home / "reports" / "proj").exists()
    assert len(report.removed) == 1 and report.removed[0].size == 20


def test_emptied_project_directories_go_but_the_kind_directory_stays(home):
    _make(home / "stops" / "proj" / "20250101T000000Z" / "stop.json", 10, age_days=99)

    hardening.retention(home, 30, keep_latest=0, apply=True)

    assert (home / "stops").is_dir() and not (home / "stops" / "proj").exists()


def test_the_database_and_its_sidecars_are_never_touched_however_old_and_wherever_they_are(home):
    files = [_make(home / name, 50, age_days=500) for name in ("ases.db", "ases.db-wal", "ases.db-shm")]
    hidden = _make(home / "logs" / "ases.db", 50, age_days=500)

    report = hardening.retention(home, 1, keep_latest=0, apply=True)

    assert all(p.exists() for p in files) and hidden.exists()
    assert any("database is never removed" in s.reason for s in report.skipped)


def test_old_database_backups_are_removed_but_the_newest_are_kept_and_other_names_are_left(home):
    backups = [_make(home / f"ases.db.bak-v5-2025010{i}T000000Z", 30, age_days=100 - i) for i in range(1, 7)]
    partial = _make(home / "ases.db.bak-v5-20250101T000000Z.123.tmp", 30, age_days=100)
    others = [_make(home / "ases.db.backup-by-hand", 30, age_days=100), _make(home / "other.db.bak-v1-2025", 30, age_days=100)]

    report = hardening.retention(home, 30, keep_latest=3, apply=True)

    assert [p.exists() for p in backups] == [False, False, False, True, True, True]  # the three newest stay
    assert not partial.exists()  # a stale partial copy is removed, and was not counted among the newest
    assert all(p.exists() for p in others)
    assert {r.kind for r in report.removed} == {"backups"}


def test_a_fresh_partial_backup_is_not_counted_as_one_of_the_newest(home):
    good = [_make(home / f"ases.db.bak-v5-2025010{i}T000000Z", 10, age_days=100 - i) for i in range(1, 4)]
    _make(home / "ases.db.bak-v6-20260921T000000Z.9.tmp", 10, age_days=0)

    hardening.retention(home, 30, keep_latest=3, apply=True)

    assert all(p.exists() for p in good)  # had the partial counted, one good backup would have been pushed out


@pytest.mark.parametrize("days", [0, -1, -30, 0.5, True, False, None, "7"])
def test_a_days_value_below_one_is_refused_and_nothing_is_touched(home, days):
    old = _make(home / "logs" / "old.log", 10, age_days=999)

    report = hardening.retention(home, days, keep_latest=0, apply=True)

    assert report.refused and "at least 1" in report.refused
    assert report.candidates == [] and report.removed == [] and old.exists()
    assert "REFUSED" in hardening.format_retention_report(report)


def test_one_day_is_the_smallest_accepted_value(home):
    old = _make(home / "logs" / "old.log", 10, age_days=2)

    report = hardening.retention(home, 1, keep_latest=0, apply=True)

    assert report.refused == "" and not old.exists()


def test_the_age_is_measured_from_the_injected_time(home):
    fresh = _make(home / "logs" / "fresh.log", 10, age_days=0)
    later = datetime.now(timezone.utc) + timedelta(days=45)

    assert hardening.retention(home, 30, keep_latest=0).candidates == []
    report = hardening.retention(home, 30, keep_latest=0, now=later, apply=True)

    assert not fresh.exists() and 44 < report.removed[0].age_days < 46


def test_a_file_exactly_at_the_limit_is_not_removed_and_one_just_past_it_is(home):
    now = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)
    at_limit = _make(home / "logs" / "at.log", 10, age_days=30, now=now)
    past = _make(home / "logs" / "past.log", 10, age_days=30.01, now=now)

    hardening.retention(home, 30, keep_latest=0, now=now, apply=True)

    assert at_limit.exists() and not past.exists()


def test_a_missing_home_or_missing_directories_are_not_errors_but_a_home_that_is_a_file_is(tmp_path):
    empty = tmp_path / "empty-home"
    empty.mkdir()
    assert hardening.retention(empty, 30).errors == []
    a_file = tmp_path / "file"
    a_file.write_text("x", encoding="utf-8")

    report = hardening.retention(a_file, 30, apply=True)

    assert report.errors and "not a directory" in report.errors[0] and report.removed == []
    assert hardening.retention(tmp_path / "does-not-exist", 30).errors


def _link_dir(link, target):
    """A directory link at `link` pointing to `target`: a real symbolic link where this account may make one, else (on
    Windows, where that needs a privilege) a directory junction, which Python 3.11 does not report as a symlink. The
    code under test must refuse both."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return "symlink"
    except (OSError, NotImplementedError):
        pass
    if sys.platform == "win32":
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
        if made.returncode == 0:
            return "junction"
    pytest.skip("cannot create a directory link here")


def test_a_symlink_entry_is_reported_and_never_followed_even_where_this_account_cannot_make_one(home, monkeypatch):
    # Emulated with a stand-in directory listing, because creating a real symlink needs a privilege many Windows
    # accounts lack. The worst case is used: an entry that says it is a symlink AND (as some platforms do) a directory.
    base = home / "logs"
    base.mkdir()
    link = types.SimpleNamespace(
        name="link", path=str(base / "link"), is_symlink=lambda: True,
        is_dir=lambda follow_symlinks=True: True, is_file=lambda follow_symlinks=True: False,
    )
    real_scandir = os.scandir

    def listing(path):
        if os.path.normcase(str(path)) == os.path.normcase(str(base)):
            return contextlib.nullcontext(iter([link]))
        return real_scandir(path)

    monkeypatch.setattr(hardening.os, "scandir", listing)
    skipped = []

    entries = hardening._scan_kind(base, "logs", skipped)

    assert entries == []
    assert [(s.path, s.reason) for s in skipped] == [(str(base / "link"), "symbolic link, not followed")]


def test_a_link_inside_a_kind_directory_is_not_followed_or_removed(home, tmp_path):
    outside = tmp_path / "outside"
    victim = _make(outside / "precious.log", 10, age_days=999)
    (home / "logs").mkdir()
    _link_dir(home / "logs" / "link", outside)
    _make(home / "logs" / "old.log", 10, age_days=99)

    report = hardening.retention(home, 30, keep_latest=0, apply=True)

    assert victim.exists() and outside.exists()
    assert any(s.path.endswith("link") and ("symbolic link" in s.reason or "resolves outside" in s.reason)
               for s in report.skipped)
    assert not (home / "logs" / "old.log").exists()  # the real old file still goes


def test_a_linked_kind_directory_is_skipped_as_a_whole(home, tmp_path):
    outside = tmp_path / "outside-logs"
    victim = _make(outside / "precious.log", 10, age_days=999)
    _link_dir(home / "logs", outside)

    report = hardening.retention(home, 1, keep_latest=0, apply=True)

    assert victim.exists() and report.removed == []
    assert any("not followed" in s.reason for s in report.skipped)


def test_a_link_that_points_at_the_home_itself_does_not_expose_the_database(home):
    _make(home / "ases.db", 10, age_days=999)
    _link_dir(home / "logs", home)

    report = hardening.retention(home, 1, keep_latest=0, apply=True)

    assert (home / "ases.db").exists() and report.removed == []


def test_a_link_to_a_directory_inside_the_home_but_elsewhere_is_not_followed_either(home):
    inside = home / "workspaces"
    victim = _make(inside / "keep.txt", 10, age_days=999)
    (home / "reports").mkdir()
    _link_dir(home / "reports" / "sneaky", inside)

    report = hardening.retention(home, 1, keep_latest=0, apply=True)

    assert victim.exists() and report.removed == []


@pytest.mark.skipif(sys.platform != "win32", reason="the read-only flag blocks deletion only on Windows")
def test_a_read_only_file_is_still_removed(home):
    target = _make(home / "logs" / "ro.log", 10, age_days=99)
    os.chmod(target, stat.S_IREAD)

    report = hardening.retention(home, 30, keep_latest=0, apply=True)

    assert not target.exists() and report.errors == []


def test_retention_never_raises_even_when_the_directory_cannot_be_read(home, monkeypatch):
    _make(home / "logs" / "old.log", 10, age_days=99)

    def broken(path):
        raise PermissionError("denied")

    monkeypatch.setattr(hardening.os, "scandir", broken)

    report = hardening.retention(home, 30, keep_latest=0, apply=True)

    assert isinstance(report, hardening.RetentionReport)
    assert report.removed == [] and report.errors and report.skipped  # each unreadable place is reported, none raised


def test_a_file_that_cannot_be_deleted_is_an_error_line_and_the_rest_are_removed(home, monkeypatch):
    stuck = _make(home / "logs" / "a-stuck.log", 10, age_days=99)
    free = _make(home / "logs" / "b-free.log", 10, age_days=98)
    real = hardening._unlink

    def picky(path):
        if path.name == "a-stuck.log":
            raise PermissionError("in use by another process")
        real(path)

    monkeypatch.setattr(hardening, "_unlink", picky)

    report = hardening.retention(home, 30, keep_latest=0, apply=True)

    assert stuck.exists() and not free.exists()
    assert len(report.errors) == 1 and "in use" in report.errors[0]
    assert [r.path for r in report.removed] == [str(free)] and report.bytes_freed == 10


def test_retention_report_is_ascii_for_accented_file_names(home):
    accent = chr(0xE9)
    _make(home / "logs" / f"caf{accent}.log", 2048, age_days=99)
    _make(home / "reports" / f"projet-{accent}" / "20250101T000000Z" / "report.json", 5 * 1024 * 1024, age_days=99)

    dry = hardening.format_retention_report(hardening.retention(home, 30, keep_latest=0))
    applied = hardening.format_retention_report(hardening.retention(home, 30, keep_latest=0, apply=True))

    assert dry.isascii() and applied.isascii()
    assert "\\xe9" in dry and "DRY RUN" in dry and "--apply" in dry
    assert "APPLIED" in applied and "freed 5.0 MB" in applied


def test_the_formatted_retention_report_summarises_kinds_kept_and_skipped(home):
    _make(home / "logs" / "a.log", 100, age_days=99)
    _make(home / "logs" / "b.log", 100, age_days=98)
    _make(home / "evals" / "e1" / "r.json", 100, age_days=99)

    text = hardening.format_retention_report(hardening.retention(home, 30, keep_latest=1))

    assert "keep the newest 1 of each kind" in text
    assert "  logs: 1 entry, 100 B" in text  # a.log goes; b.log is the newest of its kind
    assert "  evals:" not in text  # the only evals entry is the newest of its kind, so nothing of it is listed
    assert "kept because they are among the newest: 2" in text


def test_nothing_old_enough_is_reported_as_such(home):
    _make(home / "logs" / "fresh.log", 10, age_days=1)

    text = hardening.format_retention_report(hardening.retention(home, 30))

    assert "nothing is old enough to remove" in text and "0 entries" in text


# ============================================================================================
# events, vacuum
# ============================================================================================

def _event_at(conn, ts, kind="old_thing"):
    conn.execute("INSERT INTO events (ts, kind, payload) VALUES (?, ?, '{}')", (ts, kind))


def test_retention_of_files_never_touches_the_event_log(home, tmp_path):
    conn = db.connect(home / "ases.db")
    _event_at(conn, "2020-01-01T00:00:00+00:00")

    hardening.retention(home, 1, keep_latest=0, apply=True)

    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    conn.close()


def test_event_pruning_is_off_unless_asked_and_a_dry_run_only_counts(tmp_path):
    conn = db.connect(tmp_path / "ases.db")
    _event_at(conn, "2020-01-01T00:00:00+00:00")
    _event_at(conn, "2020-06-01 10:00:00")  # SQLite's plain form
    events.record(conn, "fresh")
    total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    counted = hardening.retention_events(conn, 30)

    assert counted == 2 and conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == total
    assert hardening.retention_events(conn, 30, apply=False) == 2
    conn.close()


def test_event_pruning_deletes_old_rows_only_and_records_that_it_did(tmp_path):
    conn = db.connect(tmp_path / "ases.db")
    _event_at(conn, "2020-01-01T00:00:00+00:00")
    _event_at(conn, "2020-06-01 10:00:00")
    events.record(conn, "fresh")

    pruned = hardening.retention_events(conn, 30, apply=True)

    assert pruned == 2
    kinds = [r[0] for r in conn.execute("SELECT kind FROM events ORDER BY id")]
    assert kinds == ["fresh", "events_pruned"]
    payload = json.loads(conn.execute("SELECT payload FROM events WHERE kind = 'events_pruned'").fetchone()[0])
    assert payload == {"days": 30, "rows": 2}
    conn.close()


def test_event_pruning_uses_the_injected_time_and_leaves_unreadable_timestamps_alone(tmp_path):
    conn = db.connect(tmp_path / "ases.db")
    _event_at(conn, "2026-09-21T12:00:00+00:00")
    _event_at(conn, "not a timestamp")
    now = datetime(2026, 12, 1, tzinfo=timezone.utc)

    assert hardening.retention_events(conn, 30, now=now) == 1
    assert hardening.retention_events(conn, 30, now=now, apply=True) == 1
    assert [r[0] for r in conn.execute("SELECT ts FROM events WHERE kind = 'old_thing' ORDER BY id")] == ["not a timestamp"]
    conn.close()


def test_event_pruning_keeps_a_row_exactly_at_the_limit(tmp_path):
    conn = db.connect(tmp_path / "ases.db")
    now = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)
    _event_at(conn, "2026-08-22T12:00:00+00:00")  # exactly 30 days before
    _event_at(conn, "2026-08-22T11:59:59+00:00")  # a second older

    assert hardening.retention_events(conn, 30, now=now, apply=True) == 1
    conn.close()


@pytest.mark.parametrize("days", [0, -5, 0.5, True, None])
def test_event_pruning_refuses_a_days_value_below_one(tmp_path, days):
    conn = db.connect(tmp_path / "ases.db")
    _event_at(conn, "2020-01-01T00:00:00+00:00")

    assert hardening.retention_events(conn, days, apply=True) == 0
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    conn.close()


def test_event_pruning_never_raises_on_a_closed_connection(tmp_path):
    conn = db.connect(tmp_path / "ases.db")
    conn.close()

    assert hardening.retention_events(conn, 30, apply=True) == 0


def test_vacuum_returns_the_file_size_before_and_after_and_shrinks_a_bloated_database(tmp_path):
    conn = db.connect(tmp_path / "ases.db")
    for i in range(2000):
        conn.execute("INSERT INTO events (ts, kind, payload) VALUES ('2020-01-01T00:00:00+00:00', 'bulk', ?)", ("x" * 400,))
    conn.execute("DELETE FROM events")

    before, after = hardening.vacuum(conn)

    assert isinstance(before, int) and isinstance(after, int)
    assert before > 100_000 and after < before
    assert os.path.getsize(tmp_path / "ases.db") == after
    conn.close()


def test_vacuum_on_an_in_memory_database_and_a_closed_connection_returns_numbers_without_raising():
    memory = sqlite3.connect(":memory:")
    assert hardening.vacuum(memory) == (0, 0)
    memory.close()
    assert hardening.vacuum(memory) == (0, 0)


def test_vacuum_inside_a_transaction_reports_no_change_instead_of_raising(tmp_path):
    conn = db.connect(tmp_path / "ases.db")
    conn.execute("BEGIN")

    before, after = hardening.vacuum(conn)

    assert before == after
    conn.execute("ROLLBACK")
    conn.close()


# ============================================================================================
# the source files themselves
# ============================================================================================

def test_the_files_of_this_package_hold_no_non_ascii_character_no_em_dash_and_no_section_sign():
    root = pathlib.Path(__file__).resolve().parents[2]
    files = [root / "src" / "ases" / "db.py", root / "src" / "ases" / "hardening.py", pathlib.Path(__file__),
             root / "tests" / "unit" / "test_db.py", root / "docs" / "runbook.md", root / "docs" / "operations.md"]
    banned = {chr(0x2014): "em dash", chr(0xA7): "section sign"}

    for path in files:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for index, ch in enumerate(text):
            assert ord(ch) < 128, f"{path.name}: non-ASCII character {ch!r} ({banned.get(ch, 'other')}) near {text[max(0, index - 30):index + 10]!r}"
