"""Merge queue (section 9.1: mergeq.py; section 8.1, ASES-GIT-04/05/06).

One merge at a time. Squash the work branch onto the integration HEAD in a throwaway worktree, run
Gate 3 on that exact candidate commit, fast-forward the real integration branch only if green, then the
merge card is done. A post-merge failure reverts the squash commit rather than leaving a red branch.

The controller is the only thing that ever writes to the integration branch (ASES-GIT-02) -- this
module is where that happens; nothing else in ASES calls `git merge` or `git push` on it.
"""
from __future__ import annotations

import dataclasses
import pathlib
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone

from . import gates as gates_mod


class MergeConflict(Exception):
    pass


@dataclasses.dataclass(frozen=True)
class MergeOutcome:
    """The result of one merge_task call. Three shapes matter to a caller: a real merge (merged=True with
    squash_commit set), a refusal (merged=False), and a recorded no-op (merged=True with squash_commit None
    and gate3_result "skipped"). The no-op means the branch added nothing to the integration branch and the
    caller passed allow_empty: no commit was made, nothing was gated, and the integration branch did not move."""
    merged: bool
    candidate_sha: str | None
    squash_commit: str | None
    gate3_result: str | None
    detail: str
    # True ONLY when merge_task verified in git that the integration branch moved underneath the candidate
    # between building it and the fast-forward: a genuine race, the one refusal the controller may retry for
    # free (2026-09-19 fix). Every other outcome, including every other refused fast-forward, leaves it False.
    integration_moved: bool = False


def _git(args: list[str], cwd: pathlib.Path, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=timeout)


def _head_sha(repo: pathlib.Path) -> str:
    return _git(["rev-parse", "HEAD"], repo).stdout.strip()


def _resolve(repo: pathlib.Path, rev: str) -> str:
    """The full SHA `rev` names in `repo`, or "" when git can't resolve it. Checks the exit code and uses
    --verify because a bare `git rev-parse <bad-rev>` echoes the bad argument on stdout while failing, so
    "non-empty output" alone would mistake a failed lookup for an answer."""
    result = _git(["rev-parse", "--verify", "-q", rev], repo)
    return result.stdout.strip() if result.returncode == 0 else ""


def merge_task(
    repo: pathlib.Path, integration_branch: str, work_branch: str, task_key: str,
    gate3_commands: list[str], *, conn=None, commit_message: str | None = None, allow_empty: bool = False,
    expected_head: str | None = None,
) -> MergeOutcome:
    """ASES-GIT-04: squash candidate on integration HEAD -> Gate 3 -> fast-forward -> done.
    A merge conflict or a red Gate 3 leaves the integration branch untouched and returns merged=False;
    the caller (review.py / the controller loop) is responsible for opening a fix card.

    The primary checkout must be ON integration_branch itself (2026-09-19 fix): `git merge --ff-only`
    advances whatever branch is checked out, so from any other branch (or a detached HEAD) it could
    fast-forward THAT branch, report merged=True, and leave integration where it was. That is refused up
    front, before any worktree exists.

    A refused fast-forward returns gate3_result="pass" with `integration_moved` decided from git, not
    assumed: True only if the integration tip is no longer the candidate's parent (a real race, safe for
    the caller to retry for free). git also refuses a fast-forward while the tip has NOT moved (a dirty or
    wrong-branch primary checkout, an index.lock); that is integration_moved=False and a real failure.

    An empty diff (2026-09-19): `git merge --squash` exits 0 with nothing staged when the branch adds
    nothing to the integration tip (a review-only task never commits, so its branch sits at or behind the
    tip), and `git commit` then fails. Emptiness is read from the staged index, not inferred from that
    commit failing. With allow_empty=False (the default) it stays the failure it always was: merged=False,
    detail starting "nothing to commit". With allow_empty=True it is a RECORDED NO-OP instead: no commit, no
    secret scan and no Gate 3 (there is no candidate diff), the integration branch is not touched, and the
    merge_records row is completed with squash_commit NULL and gate3_result "skipped". The outcome is
    merged=True with squash_commit None, which is how a caller tells a no-op from a real merge. When the
    squash does stage changes allow_empty changes nothing.

    `expected_head` closes the time-of-check gap (2026-09-19): the caller checked and gate-ran ONE commit of the
    work branch (review.check_branch_for_merge), and a commit pushed after that must not ride in unchecked. With
    it given, the branch must still be at exactly that commit, else the merge is refused (nothing is built);
    and the squash is taken from that SHA itself, not from the branch name, so there is no window at all."""
    # Before mkdtemp and before any worktree, so refusing here leaves nothing behind to clean up.
    head = _git(["symbolic-ref", "--short", "-q", "HEAD"], repo)
    current_branch = head.stdout.strip() if head.returncode == 0 else ""
    if current_branch != integration_branch:
        if head.returncode == 0:
            where = f"branch '{current_branch}'"
        elif head.returncode == 1:  # `symbolic-ref -q` exits 1, silently, on a detached HEAD
            where = "a detached HEAD (no branch)"
        else:
            where = f"an unreadable HEAD ({head.stderr.strip()})"
        return MergeOutcome(
            False, None, None, None,
            f"primary checkout {repo} is on {where}, not the integration branch '{integration_branch}': "
            f"ASES refuses to merge into a checkout that isn't on it (git merge --ff-only would advance "
            f"whatever branch is checked out, not '{integration_branch}')",
        )

    squash_ref = work_branch
    if expected_head is not None:
        now = _resolve(repo, work_branch)
        if now != expected_head:
            return MergeOutcome(
                False, None, None, None,
                f"branch {work_branch} moved after the pre-merge checks (checked {expected_head[:12]}, now "
                f"{now[:12] or 'unresolvable'}): a later commit voids the review and the gate record "
                f"(ASES-GIT-03), so nothing was merged",
            )
        squash_ref = expected_head

    tmp_root = pathlib.Path(tempfile.mkdtemp(prefix="ases-merge-"))
    candidate = tmp_root / "candidate"
    try:
        base_sha = _head_sha(repo)
        add = _git(["worktree", "add", "--detach", str(candidate), integration_branch], repo)
        if add.returncode != 0:
            return MergeOutcome(False, None, None, None, f"could not create candidate worktree: {add.stderr}")

        squash = _git(["merge", "--squash", squash_ref], candidate)
        if squash.returncode != 0:
            _git(["merge", "--abort"], candidate)
            return MergeOutcome(False, None, None, None, f"merge conflict: {squash.stdout}{squash.stderr}")

        # Empty or not is decided from the index (2026-09-19). --quiet implies --exit-code: 0 means nothing
        # is staged, 1 means something is, and any other code is git failing to answer, which is reported
        # rather than guessed at (guessing "empty" would silently record a merge that never happened).
        staged = _git(["diff", "--cached", "--quiet"], candidate)
        if staged.returncode not in (0, 1):
            return MergeOutcome(
                False, None, None, None,
                f"could not tell whether squashing {work_branch} staged any changes (git diff --cached "
                f"--quiet exited {staged.returncode}): {staged.stdout}{staged.stderr}",
            )
        if staged.returncode == 0:
            if not allow_empty:
                # Refused here instead of by attempting the commit: git's own text for that failure is only
                # "Not currently on any branch. nothing to commit", which says nothing about why.
                return MergeOutcome(
                    False, None, None, None,
                    f"nothing to commit: squashing {work_branch} onto {integration_branch} staged no changes "
                    f"(an empty diff: the branch has no commit that adds anything to the integration tip)",
                )
            if conn is not None:
                # Same upsert idiom as the Gate 3 record below, but complete in this one write: there is no
                # fast-forward left to wait for, and reconcile (ASES-REC-04) wants completed_at on a done merge
                # card. candidate_sha is the integration tip the empty squash was built on. A retry rewrites
                # every column the no-op owns, so running it twice leaves one identical row.
                conn.execute(
                    "INSERT INTO merge_records (task_key, candidate_sha, gate3_result, squash_commit, "
                    "reverted, completed_at) VALUES (?, ?, ?, NULL, 0, ?) "
                    "ON CONFLICT(task_key) DO UPDATE SET candidate_sha=excluded.candidate_sha, "
                    "gate3_result=excluded.gate3_result, squash_commit=NULL, "
                    "completed_at=excluded.completed_at",
                    (task_key, base_sha, "skipped", datetime.now(timezone.utc).isoformat(timespec="seconds")),
                )
            return MergeOutcome(True, None, None, "skipped", "no changes to merge (review-only task)")

        msg = commit_message or f"{task_key}: merge {work_branch}"
        commit = _git(["commit", "-q", "-m", msg], candidate)
        if commit.returncode != 0:
            # Emptiness was decided from the index above, so a commit that fails NOW had staged changes and was
            # refused for another reason (no usable git identity, a hook). It used to be labelled "nothing to
            # commit" here, which sent whoever read the failure looking for an empty diff that was not there.
            return MergeOutcome(False, None, None, None, f"commit failed: {commit.stdout}{commit.stderr}")

        candidate_sha = _head_sha(candidate)

        diff = _git(["diff", f"{base_sha}..{candidate_sha}"], candidate).stdout
        secret_findings = gates_mod.scan_for_secrets(diff)
        if secret_findings:
            return MergeOutcome(False, candidate_sha, None, None,
                                 "secret scan failed (ASES-SEC-01): " + "; ".join(secret_findings))

        gate_result = gates_mod.run_gate(
            candidate, candidate_sha, "gate3", gate3_commands, conn=conn, task_key=task_key,
        )
        if conn is not None:
            conn.execute(
                "INSERT INTO merge_records (task_key, candidate_sha, gate3_result, squash_commit, "
                "reverted, completed_at) VALUES (?, ?, ?, NULL, 0, NULL) "
                "ON CONFLICT(task_key) DO UPDATE SET candidate_sha=excluded.candidate_sha, "
                "gate3_result=excluded.gate3_result",
                (task_key, candidate_sha, "pass" if gate_result.passed else "fail"),
            )
        if not gate_result.passed:
            return MergeOutcome(False, candidate_sha, None, "fail", gate_result.detail)

        ff = _git(["merge", "--ff-only", candidate_sha], repo)
        if ff.returncode != 0:
            # Fail safe rather than force. But "refused" alone does not mean the tip moved (2026-09-19 fix):
            # git also refuses --ff-only over a dirty primary checkout whose uncommitted changes the merge
            # would overwrite, or an index.lock, with the tip exactly where it was. The candidate was
            # squashed onto the integration tip as of worktree creation, so its parent IS that tip; the
            # branch moved iff it is somewhere else now. Only that evidence earns the free retry, so if
            # either lookup fails the answer is "not moved".
            parent = _resolve(repo, f"{candidate_sha}^")
            tip_now = _resolve(repo, integration_branch)
            moved = bool(parent) and bool(tip_now) and tip_now != parent
            if moved:
                detail = (f"fast-forward refused, integration branch moved ('{integration_branch}' was "
                          f"{parent[:12]} when the candidate was built, is now {tip_now[:12]}): {ff.stderr}")
            elif parent and tip_now:
                detail = (f"fast-forward refused although the integration branch did NOT move (still "
                          f"{tip_now[:12]}); this is not a race, a dirty or wrong-branch primary checkout "
                          f"is the likely cause: {ff.stderr}")
            else:
                detail = (f"fast-forward refused and git could not confirm whether the integration branch "
                          f"moved (treated as NOT moved, so no free retry); a dirty or wrong-branch primary "
                          f"checkout is a likely cause: {ff.stderr}")
            return MergeOutcome(False, candidate_sha, None, "pass", detail, integration_moved=moved)

        if conn is not None:
            conn.execute(
                "UPDATE merge_records SET squash_commit = ?, completed_at = ? WHERE task_key = ?",
                (candidate_sha, datetime.now(timezone.utc).isoformat(timespec="seconds"), task_key),
            )
        return MergeOutcome(True, candidate_sha, candidate_sha, "pass", "merged")
    finally:
        _git(["worktree", "remove", "--force", str(candidate)], repo)
        shutil.rmtree(tmp_root, ignore_errors=True)


def revert_merge(repo: pathlib.Path, squash_commit: str, *, conn=None, task_key: str = "") -> bool:
    """ASES-GIT-05: a post-merge failure reverts the squash commit rather than leaving the
    integration branch red."""
    result = _git(["revert", "--no-edit", squash_commit], repo)
    ok = result.returncode == 0
    if conn is not None and task_key:
        conn.execute("UPDATE merge_records SET reverted = 1 WHERE task_key = ?", (task_key,))
    return ok
