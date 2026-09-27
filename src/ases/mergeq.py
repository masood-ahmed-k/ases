"""Merge queue (section 9.1: mergeq.py; section 8.1, ASES-GIT-04/05/06).

One merge at a time. Squash the work branch onto the integration HEAD in a throwaway worktree, run
Gate 3 on that exact candidate commit, fast-forward the real integration branch only if green, then the
merge card is done. A post-merge failure reverts the squash commit rather than leaving a red branch.

The controller is the only thing that ever writes to the integration branch (ASES-GIT-02) -- this
module is where that happens; nothing else in ASES calls `git merge` or `git push` on it.

Two things wrap those steps (round 5). The kill switch (ASES-REC-06: "stop the merge queue between steps") is a
`should_stop` callable polled between the steps, so a stop request ends the merge at a step boundary and never in
the middle of one. And when the caller names a project, the steps write intent records (ASES-REC-03/04: "Every
multi-step action writes an intent record before acting and a completion record after ... build a candidate,
fast-forward ... revert"), so a crash between the two leaves an open intent that reconcile-on-start can read.

Round 6, project-scoping merge_records (found by the reconcile and evals builders: two projects sharing this
database, or reusing a task key, share a merge_records row): schema v7 added a nullable `project` column here, the
same as gate_runs. Round 9 (package MERGEPK) finished the fix schema v7 left open: db.py's migration 8 rebuilds
merge_records with PRIMARY KEY (project, task_key), so two projects' candidates for the SAME task_key are two
DIFFERENT rows, never one overwriting the other.

`project` stays NULLABLE on purpose (a row with no project recorded still matches gates.last_gate_result's own
style of read -- see reconcile.py, report.py, finalgates.py and evalkit/codetasks.py, all updated in the same
change). What changed here is how a NULL-project write is done: SQLite indexes, and so `ON CONFLICT`, treat every
NULL as distinct from every other NULL, so `ON CONFLICT(project, task_key)` never dispatches for a NULL project
(two calls with project NULL and the same task_key would insert TWO rows, not update one). `_build_candidate`,
`_record_candidate` and `_fast_forward` use ON CONFLICT / a plain scoped UPDATE for a real project, exactly like a
single-column key used to, and fall back to an explicit UPDATE-then-INSERT-if-absent for a NULL project, so a
caller that still does not pass one keeps the pre-migration behavior exactly: one row per task_key, reset in
place by a new candidate build. `revert_merge`'s write is scoped the same way in both branches now (never a bare,
unscoped `WHERE task_key = ?`, which would have touched every project's row sharing that task_key)."""
from __future__ import annotations

import contextlib
import dataclasses
import pathlib
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from datetime import datetime, timezone

from . import events as events_mod
from . import gates as gates_mod
from . import gitexec
from . import guards as guards_mod
from . import intents as intents_mod

# The three places merge_task asks whether to stop, named for the "stopped by the kill switch before <step>" text.
STEP_CANDIDATE = "building the candidate"
STEP_GATE3 = "Gate 3"
STEP_FAST_FORWARD = "the fast-forward"


class MergeConflict(Exception):
    pass


@dataclasses.dataclass(frozen=True)
class MergeOutcome:
    """The result of one merge_task call. Three shapes matter to a caller: a real merge (merged=True with
    squash_commit set), a refusal (merged=False), and a recorded no-op (merged=True with squash_commit None
    and gate3_result "skipped"). The no-op means the branch added nothing to the integration branch and the
    caller passed allow_empty: no commit was made, nothing was gated, and the integration branch did not move.

    A fourth shape is a stop (`stopped=True`, merged=False): the caller's should_stop callable said stop at one of
    the three checkpoints, so nothing was merged and nothing failed. It is not a refusal: there is no branch
    problem to fix, so a caller must not open a fix card or spend budget for it.

    `detail` is redacted when the outcome is built (ASES-SEC-01): it carries git and gate output, and the
    controller copies it into events and fix-card bodies. Doing it here means no producer of an outcome can forget."""
    merged: bool
    candidate_sha: str | None
    squash_commit: str | None
    gate3_result: str | None
    detail: str
    # True ONLY when merge_task verified in git that the integration branch moved underneath the candidate
    # between building it and the fast-forward: a genuine race, the one refusal the controller may retry for
    # free (2026-09-19 fix). Every other outcome, including every other refused fast-forward, leaves it False.
    integration_moved: bool = False
    # True ONLY when should_stop asked for a halt at a checkpoint (ASES-REC-06). The candidate was discarded and no
    # merge_records row was written or changed for it; candidate_sha, squash_commit and gate3_result are None.
    stopped: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.detail, str):
            object.__setattr__(self, "detail", events_mod.redact_text(self.detail))


@dataclasses.dataclass(frozen=True)
class RevertOutcome:
    """The result of one revert_merge call (round 6: the MR builder found that the old bool return let a failed
    `git revert` still mark merge_records.reverted = 1, and never cleaned up a conflicted checkout).

    `ok` is True only when `git revert` itself exited 0 and a new revert commit exists; that is the one bit the
    old bare bool carried, kept under a name a caller checks explicitly (the same style as MergeOutcome.merged),
    never by truthiness. `commit_sha` is that new commit, None when there is none. `aborted` is True only when
    the revert failed AND `git revert --abort` was run and itself exited 0, so the primary checkout was left
    clean at the pre-revert commit; a caller for which even that is not good enough (the checkout could still be
    left mid-conflict) tells the two apart by `ok is False and aborted is False`. `detail` is git's own output or
    error text, always redacted (ASES-SEC-01: git can echo an environment or a remote URL)."""
    ok: bool
    commit_sha: str | None
    detail: str
    aborted: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.detail, str):
            object.__setattr__(self, "detail", events_mod.redact_text(self.detail))


def _git(args: list[str], cwd: pathlib.Path, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*gitexec.GIT, "-C", str(cwd), *args], capture_output=True, text=True, timeout=timeout, env=gitexec.git_env(),
    )


def _head_sha(repo: pathlib.Path) -> str:
    return _git(["rev-parse", "HEAD"], repo).stdout.strip()


def _resolve(repo: pathlib.Path, rev: str) -> str:
    """The full SHA `rev` names in `repo`, or "" when git can't resolve it. Checks the exit code and uses
    --verify because a bare `git rev-parse <bad-rev>` echoes the bad argument on stdout while failing, so
    "non-empty output" alone would mistake a failed lookup for an answer."""
    result = _git(["rev-parse", "--verify", "-q", rev], repo)
    return result.stdout.strip() if result.returncode == 0 else ""


def _intent(conn, project: str | None, kind: str, task_key: str, detail: str | None = None):
    """The intent record of ASES-REC-03/04 around one step: intents.intent (write before, complete only if the body
    finished, leave it open when the body raises) when the caller gave both a project and a connection, else a
    no-op, so a call without them (a unit test, a one-off merge) behaves exactly as it did before intents existed.
    A step that RETURNS, including a refusal, finished cleanly and completes its intent; only an exception or a
    crash leaves it open, which is what reconcile-on-start looks for."""
    if conn is None or project is None:
        return contextlib.nullcontext()
    return intents_mod.intent(conn, project, kind, task_key, detail)


def _stop_requested(
    should_stop: Callable[[], bool] | None, conn, task_key: str, step: str, project: str | None = None,
) -> bool:
    """Ask the caller whether to stop before `step` (ASES-REC-06). No callable means never stop. A callable that
    raises counts as "keep going" (a broken kill-switch reader must not be able to block merging) and is recorded
    as a should_stop_error event, so it is visible instead of silent."""
    if should_stop is None:
        return False
    try:
        return bool(should_stop())
    except Exception as exc:  # noqa: BLE001 - any failure of the caller's callable is treated the same way
        if conn is not None:
            # events.py package, round 9: every call site is inside merge_task, which already has `project` in
            # scope, so it is threaded straight through here instead of left NULL.
            events_mod.record(conn, "should_stop_error", {
                "task_key": task_key, "step": step, "error": f"{type(exc).__name__}: {exc}"[:300],
            }, project=project)
        return False


def _stopped(step: str) -> MergeOutcome:
    return MergeOutcome(False, None, None, None, f"stopped by the kill switch before {step}", stopped=True)


def _check_base(repo: pathlib.Path, work_branch: str, conn, project: str | None) -> MergeOutcome | None:
    """ASES-GIT-01, ASES-GIT-16 (blueprint p169's second sentence): refuse a merge before anything is built when
    `work_branch`'s own base commit is not one ASES itself wrote (guards.check_card_base against
    guards.written_heads). This is the merge queue's OWN, independent half of the base-commit check: the
    controller's per-pass detector (controller.process_card_base_checks) only looks at cards it currently sees
    Hermes report `running`, so a card whose worktree was cut from a bad base but that the detector missed for
    any reason (a race between two polls, a card that left `running` before a pass ever saw it) can still never
    have its branch actually land, because this runs on every merge_task call, not once per card.

    None (nothing to refuse) when there is nothing to check the base against: no `conn`/`project` given (the
    pre-round-10 call shape, and every caller that does not pass them), or this project has not adopted any head
    yet (guards.written_heads empty) -- the same "nothing recorded, skip the comparison" reading
    check_primary_checkout already gives an expected_head of None, never "everything fails". Otherwise a
    MergeOutcome refusal (merged=False) naming the branch and why, with a merge_refused_bad_base event, or None
    when the base verifies."""
    if conn is None or project is None:
        return None
    allowed = guards_mod.written_heads(conn, project)
    if not allowed:
        return None
    result = guards_mod.check_card_base(repo, work_branch, allowed)
    if result.ok:
        return None
    events_mod.record(conn, "merge_refused_bad_base", {
        "branch": work_branch, "base": result.base, "reason": result.reason,
    }, project=project)
    return MergeOutcome(
        False, None, None, None, f"branch {work_branch} refused (ASES-GIT-01, ASES-GIT-16): {result.reason}",
    )


def merge_task(
    repo: pathlib.Path, integration_branch: str, work_branch: str, task_key: str,
    gate3_commands: list[str], *, conn=None, commit_message: str | None = None, allow_empty: bool = False,
    expected_head: str | None = None, project: str | None = None,
    should_stop: Callable[[], bool] | None = None, project_config=None, task=None,
) -> MergeOutcome:
    """ASES-GIT-04: squash candidate on integration HEAD -> Gate 3 -> fast-forward -> done.
    A merge conflict or a red Gate 3 leaves the integration branch untouched and returns merged=False;
    the caller (review.py / the controller loop) is responsible for opening a fix card.

    The primary checkout must be ON integration_branch itself (2026-09-19 fix): `git merge --ff-only`
    advances whatever branch is checked out, so from any other branch (or a detached HEAD) it could
    fast-forward THAT branch, report merged=True, and leave integration where it was. That is refused up
    front, before any worktree exists.

    ASES-GIT-01, ASES-GIT-16 (round 10, package BASECHECK): refused up front too, right after that, when
    `work_branch`'s own base commit is not one ASES itself wrote (`_check_base`, against
    `guards.written_heads(conn, project)`) -- this project's own, independent enforcement of the base-commit
    check, so a card the controller's per-pass detector missed can still never have its branch land. Only
    checked when both `conn` and `project` are given and this project has adopted at least one head; see
    `_check_base`'s own docstring.

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
    and the squash is taken from that SHA itself, not from the branch name, so there is no window at all.

    `project_config`/`task` (round 9, ASES-QG-04, ASES-SEC-03, ASES-SEC-05, ASES-SEC-07): reach
    gates.resolve_runner for the Gate 3 candidate run only (never the fast-forward, which runs no commands).
    `project_config` is an ases.config.ProjectConfig (or a duck-typed stand-in, see resolve_runner); `task` is
    this task's own ases.plan.PlanTask, so its sandbox_network exception, if it carries one, applies to this one
    candidate run. Both default to None: today's behaviour, the host runner. A
    sandbox.SandboxInfrastructureError from the Gate 3 run is NOT caught here -- nothing has been built or
    merged yet at that point, so there is nothing to undo -- and propagates to the caller.

    ASES-REC-06 (`should_stop`): "stop the merge queue between steps". A zero-argument callable, polled before the
    candidate is built, before Gate 3 and before the fast-forward, in that order. When it says True the
    candidate worktree is removed, no merge_records row is written or changed (the row is only written once the
    last checkpoint has passed, so there is no half row to clean up), and the outcome is merged=False,
    stopped=True with detail "stopped by the kill switch before <step>". Gate 3, once started, is never
    interrupted: the stop takes effect at the next boundary. A callable that raises is treated as False and
    recorded as a should_stop_error event (a broken kill-switch reader must not be able to block merging).

    ASES-REC-03/04 (`project`): with a project name (and a connection) the candidate build plus Gate 3 run inside
    an intent of kind build_candidate and the fast-forward inside one of kind fast_forward, both keyed by the
    task. The intent is completed only when the step finished (an exception or a crash leaves it open for
    reconcile-on-start), and a step that ends in a refusal has finished. Without a project no intent is written.

    ASES-REC-04 (found by the reconcile builder, 2026-09-21): a NEW candidate for a task resets `reverted`,
    `squash_commit` and `completed_at` on its merge_records row. Before, a merge that was reverted, fixed and
    merged again kept reverted=1 forever, so reconcile.check() reported done_but_reverted for a healthy merge.
    Only a new candidate build resets the row: a call that ends before one exists (a wrong checkout, a moved
    branch, a conflict, an empty diff, a red secret scan) leaves whatever row the task already has untouched."""
    if _stop_requested(should_stop, conn, task_key, STEP_CANDIDATE, project):
        return _stopped(STEP_CANDIDATE)

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

    # ASES-GIT-01, ASES-GIT-16 (round 10, package BASECHECK): before mkdtemp and before any worktree, exactly
    # like the branch check just above, so refusing here leaves nothing behind to clean up either.
    refused = _check_base(repo, work_branch, conn, project)
    if refused is not None:
        return refused

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
        with _intent(conn, project, intents_mod.KIND_BUILD_CANDIDATE, task_key,
                     f"squash {work_branch} onto {integration_branch}"):
            candidate_sha, early = _build_candidate(
                repo, integration_branch, work_branch, squash_ref, task_key, candidate, commit_message,
                allow_empty, conn, project,
            )
            if early is not None:
                return early

            if _stop_requested(should_stop, conn, task_key, STEP_GATE3, project):
                return _stopped(STEP_GATE3)
            # The sandbox kwargs are added to the call ONLY when the sandbox is actually enabled (round 9): a
            # `run_gate` stand-in written before this round (a fixed signature, no **kwargs) keeps working
            # unchanged for every test that does not turn the sandbox on, exactly like `allow_paths` elsewhere.
            choice = gates_mod.resolve_runner(project_config, task)
            sandbox_kwargs = (
                {"runner": choice.runner, "self_contained_checkout": True} if choice.self_contained else {}
            )
            gate_result = gates_mod.run_gate(
                candidate, candidate_sha, "gate3", gate3_commands, conn=conn, task_key=task_key, project=project,
                **sandbox_kwargs,
            )
            # The last checkpoint comes BEFORE the merge_records row is written, so a stop here leaves the row
            # exactly as it was. A red gate never reaches the fast-forward, so it has nothing left to stop.
            if gate_result.passed and _stop_requested(should_stop, conn, task_key, STEP_FAST_FORWARD, project):
                return _stopped(STEP_FAST_FORWARD)
            if conn is not None:
                _record_candidate(conn, task_key, candidate_sha, gate_result.passed, project)
            if not gate_result.passed:
                return MergeOutcome(False, candidate_sha, None, "fail", gate_result.detail)

        with _intent(conn, project, intents_mod.KIND_FAST_FORWARD, task_key,
                     f"fast-forward {integration_branch} to {candidate_sha}"):
            return _fast_forward(repo, integration_branch, candidate_sha, task_key, conn, project)
    finally:
        _git(["worktree", "remove", "--force", str(candidate)], repo)
        shutil.rmtree(tmp_root, ignore_errors=True)


def _upsert_merge_record(
    conn, project: str | None, task_key: str, *, candidate_sha: str | None, gate3_result: str | None,
    squash_commit: str | None, reverted: int, completed_at: str | None,
) -> None:
    """INSERT a fresh merge_records row for a new candidate, or reset one that already exists for this
    (project, task_key) -- schema v8's primary key, db.py. Called by both `_build_candidate`'s no-op path and
    `_record_candidate`; the two always write the same five columns (candidate_sha, gate3_result, squash_commit,
    reverted, completed_at), only their values differ.

    With a real project, `ON CONFLICT(project, task_key)` dispatches exactly the way ON CONFLICT(task_key) used
    to when task_key alone was the whole key: a second candidate for the SAME (project, task_key) updates the one
    row in place, a different project's candidate for the same task_key is a DIFFERENT row entirely (the fix this
    migration exists for).

    With no project (an old call site, a unit test, or a caller not yet updated), ON CONFLICT cannot be used:
    SQLite indexes treat every NULL as distinct from every other NULL, so two INSERTs with project NULL and the
    same task_key never conflict -- proven empirically before this was written, not assumed. The row is found or
    created by hand instead: an UPDATE scoped to `project IS NULL`, and only when that touches nothing (there is
    no such row yet) does an INSERT follow. This is exactly the pre-migration behavior for a no-project caller:
    one row per task_key, reset in place by every new candidate build, never a second row accumulating."""
    if project is None:
        updated = conn.execute(
            "UPDATE merge_records SET candidate_sha = ?, gate3_result = ?, squash_commit = ?, reverted = ?, "
            "completed_at = ? WHERE task_key = ? AND project IS NULL",
            (candidate_sha, gate3_result, squash_commit, reverted, completed_at, task_key),
        )
        if updated.rowcount == 0:
            conn.execute(
                "INSERT INTO merge_records (project, task_key, candidate_sha, gate3_result, squash_commit, "
                "reverted, completed_at) VALUES (NULL, ?, ?, ?, ?, ?, ?)",
                (task_key, candidate_sha, gate3_result, squash_commit, reverted, completed_at),
            )
    else:
        conn.execute(
            "INSERT INTO merge_records (project, task_key, candidate_sha, gate3_result, squash_commit, reverted, "
            "completed_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(project, task_key) DO UPDATE SET candidate_sha = excluded.candidate_sha, "
            "gate3_result = excluded.gate3_result, squash_commit = excluded.squash_commit, "
            "reverted = excluded.reverted, completed_at = excluded.completed_at",
            (project, task_key, candidate_sha, gate3_result, squash_commit, reverted, completed_at),
        )


def _build_candidate(
    repo: pathlib.Path, integration_branch: str, work_branch: str, squash_ref: str, task_key: str,
    candidate: pathlib.Path, commit_message: str | None, allow_empty: bool, conn, project: str | None = None,
) -> tuple[str | None, MergeOutcome | None]:
    """Squash `squash_ref` onto the integration tip in the throwaway worktree `candidate`, commit it, and scan the
    diff for secrets. Returns (candidate_sha, None) when there is a candidate commit ready for Gate 3, or (None,
    outcome) when merge_task is already done: a refusal (no worktree, a conflict, an empty diff with allow_empty
    False, a failed commit, a secret) or the recorded no-op. Nothing here touches the real integration branch."""
    base_sha = _head_sha(repo)
    add = _git(["worktree", "add", "--detach", str(candidate), integration_branch], repo)
    if add.returncode != 0:
        return None, MergeOutcome(False, None, None, None, f"could not create candidate worktree: {add.stderr}")

    squash = _git(["merge", "--squash", squash_ref], candidate)
    if squash.returncode != 0:
        _git(["merge", "--abort"], candidate)
        return None, MergeOutcome(False, None, None, None, f"merge conflict: {squash.stdout}{squash.stderr}")

    # Empty or not is decided from the index (2026-09-19). --quiet implies --exit-code: 0 means nothing
    # is staged, 1 means something is, and any other code is git failing to answer, which is reported
    # rather than guessed at (guessing "empty" would silently record a merge that never happened).
    # --quiet means the answer is the exit code alone (0/1/failure), never rendered diff text, so a worker-
    # configured external-diff/textconv driver has nothing to hide here: gitexec.DIFF_SAFETY is left off this
    # call on purpose (it is added below, to the Gate 3 diff, which DOES read rendered text).
    staged = _git(["diff", "--cached", "--quiet"], candidate)
    if staged.returncode not in (0, 1):
        return None, MergeOutcome(
            False, None, None, None,
            f"could not tell whether squashing {work_branch} staged any changes (git diff --cached "
            f"--quiet exited {staged.returncode}): {staged.stdout}{staged.stderr}",
        )
    if staged.returncode == 0:
        if not allow_empty:
            # Refused here instead of by attempting the commit: git's own text for that failure is only
            # "Not currently on any branch. nothing to commit", which says nothing about why.
            return None, MergeOutcome(
                False, None, None, None,
                f"nothing to commit: squashing {work_branch} onto {integration_branch} staged no changes "
                f"(an empty diff: the branch has no commit that adds anything to the integration tip)",
            )
        if conn is not None:
            # Same upsert idiom as the Gate 3 record (_record_candidate), but complete in this one write: there is
            # no fast-forward left to wait for, and reconcile (ASES-REC-04) wants completed_at on a done merge
            # card. candidate_sha is the integration tip the empty squash was built on. A retry rewrites every
            # column the no-op owns, so running it twice leaves one identical row. reverted is one of them: this
            # is a new candidate build too, so it starts the row over (see merge_task's ASES-REC-04 note).
            #
            # project (schema v8, round 9): a real project's row is its OWN row now (PRIMARY KEY (project,
            # task_key)), never a different project's upsert target; see _upsert_merge_record for how a NULL
            # project is still reset in place without relying on ON CONFLICT (SQLite never treats two NULLs as a
            # conflict).
            _upsert_merge_record(
                conn, project, task_key, candidate_sha=base_sha, gate3_result="skipped", squash_commit=None,
                reverted=0, completed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
        return None, MergeOutcome(True, None, None, "skipped", "no changes to merge (review-only task)")

    msg = commit_message or f"{task_key}: merge {work_branch}"
    commit = _git(["commit", "-q", "-m", msg], candidate)
    if commit.returncode != 0:
        # Emptiness was decided from the index above, so a commit that fails NOW had staged changes and was
        # refused for another reason (no usable git identity, a hook). It used to be labelled "nothing to
        # commit" here, which sent whoever read the failure looking for an empty diff that was not there.
        return None, MergeOutcome(False, None, None, None, f"commit failed: {commit.stdout}{commit.stderr}")

    candidate_sha = _head_sha(candidate)

    diff = _git(["diff", *gitexec.DIFF_SAFETY, f"{base_sha}..{candidate_sha}"], candidate).stdout
    secret_findings = gates_mod.scan_for_secrets(diff)
    if secret_findings:
        return None, MergeOutcome(False, candidate_sha, None, None,
                                   "secret scan failed (ASES-SEC-01): " + "; ".join(secret_findings))
    return candidate_sha, None


def _record_candidate(conn, task_key: str, candidate_sha: str, passed: bool, project: str | None = None) -> None:
    """Write the merge_records row for a candidate that has been through Gate 3. A NEW candidate starts the row
    over: reverted, squash_commit, completed_at and project are reset, because they describe the previous merge of
    this task (a revert, a fix card and a second merge is the case that used to leave reverted=1 behind). The
    fast-forward fills squash_commit and completed_at in again once it lands."""
    _upsert_merge_record(
        conn, project, task_key, candidate_sha=candidate_sha, gate3_result="pass" if passed else "fail",
        squash_commit=None, reverted=0, completed_at=None,
    )


def _fast_forward(
    repo: pathlib.Path, integration_branch: str, candidate_sha: str, task_key: str, conn, project: str | None = None,
) -> MergeOutcome:
    """Advance the real integration branch to the gated candidate, or refuse without forcing anything, and
    complete the merge_records row when it lands."""
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
        # project is deliberately NOT touched here (the upsert moments ago already stamped it, this is a plain
        # UPDATE): it IS scoped by project, though (schema v8, round 9) -- merge_records' key is now (project,
        # task_key), so two projects can share a task_key as two different rows, and an unscoped `WHERE
        # task_key = ?` would complete BOTH of them instead of only the one _record_candidate/_build_candidate
        # just wrote in THIS merge_task call. A NULL project (an old caller) is matched by `project IS NULL`,
        # never left unscoped, for the same reason.
        if project is None:
            conn.execute(
                "UPDATE merge_records SET squash_commit = ?, completed_at = ? "
                "WHERE task_key = ? AND project IS NULL",
                (candidate_sha, datetime.now(timezone.utc).isoformat(timespec="seconds"), task_key),
            )
        else:
            conn.execute(
                "UPDATE merge_records SET squash_commit = ?, completed_at = ? WHERE task_key = ? AND project = ?",
                (candidate_sha, datetime.now(timezone.utc).isoformat(timespec="seconds"), task_key, project),
            )
    return MergeOutcome(True, candidate_sha, candidate_sha, "pass", "merged")


def revert_merge(
    repo: pathlib.Path, squash_commit: str, *, conn=None, task_key: str = "", project: str | None = None,
) -> RevertOutcome:
    """ASES-GIT-05: a post-merge failure reverts the squash commit rather than leaving the
    integration branch red. Returns a RevertOutcome; read RevertOutcome.ok, never the truthiness of the
    object itself (round 6: this used to return a bare bool, which the caller trusted even when it was
    wrong -- see the two fixes below).

    ASES-REC-03/04 (`project`): with a project name (and a connection) the revert runs inside an intent of kind
    revert keyed by `task_key`, the shape reconcile-on-start reads ("a revert was started for the task"). It is
    completed when this function returns and left open when it raises or the process dies, so a crash between
    `git revert` and the merge_records update is found and settled from git. `git revert --abort` (below) is run
    OUTSIDE that accounting on purpose: a best-effort cleanup attempt must never turn into a second half-written
    intent of its own.

    Round 6 (the MR builder's finding): this used to mark merge_records.reverted = 1 even when `git revert`
    itself failed, and never ran `git revert --abort` on a conflict, so a failed revert could leave the primary
    checkout mid-revert (conflict markers, an in-progress revert in .git) while the database quietly claimed the
    commit was reverted. Both are fixed: `reverted = 1` is written only when `git revert` actually exited 0, and
    any other exit runs `git revert --abort` as a best-effort cleanup (its own failure is swallowed, never
    raised: a cleanup attempt that itself blows up must not be worse than not attempting one) so the checkout is
    left clean at the pre-revert commit whenever git can manage it at all. `RevertOutcome.aborted` says whether
    that cleanup succeeded; `ok` is False either way; a caller that must tell "cleanly refused" from "still
    dirty" apart checks `aborted` too, matching the halt path controller.process_merge_queue wires this round.

    The `reverted` write is NULL-tolerant by project (the same scoping run_gate and last_gate_result use):
    with a real project it matches that project's own row OR a legacy row with no project recorded, never a
    DIFFERENT project's row that happens to share this task_key (schema v8, round 9: merge_records' primary key
    is now (project, task_key), so that row genuinely exists as its own, separate record rather than the SAME
    physical row a same-task_key upsert used to share). With no project given (the old call shape) the write is
    now scoped to `project IS NULL` rather than left completely unscoped: an unscoped `WHERE task_key = ?` would
    mark EVERY project's row for this task_key reverted, not just the one row this caller actually means."""
    with _intent(conn, project, intents_mod.KIND_REVERT, task_key, f"revert {squash_commit}"):
        result = _git(["revert", "--no-edit", squash_commit], repo)
        ok = result.returncode == 0
        aborted = False
        if ok:
            commit_sha = _head_sha(repo)
            detail = f"reverted {squash_commit}: {result.stdout}{result.stderr}".strip()
        else:
            commit_sha = None
            detail = f"git revert of {squash_commit} failed: {result.stdout}{result.stderr}"
            try:
                abort = _git(["revert", "--abort"], repo)
                aborted = abort.returncode == 0
                if not aborted:
                    detail += f" (git revert --abort also failed: {abort.stdout}{abort.stderr})"
            except Exception as exc:  # noqa: BLE001 - best effort: a cleanup attempt must never itself raise
                detail += f" (git revert --abort also raised: {type(exc).__name__}: {exc})"
        if ok and conn is not None and task_key:
            if project is None:
                conn.execute(
                    "UPDATE merge_records SET reverted = 1 WHERE task_key = ? AND project IS NULL", (task_key,),
                )
            else:
                conn.execute(
                    "UPDATE merge_records SET reverted = 1 WHERE task_key = ? AND (project IS NULL OR project = ?)",
                    (task_key, project),
                )
    return RevertOutcome(ok, commit_sha, detail, aborted=aborted)
