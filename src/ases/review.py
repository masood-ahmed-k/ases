"""Review-lane policing (section 9.1: review.py; section 13.2, ASES-REV-05/06).

When a card enters `review`, the controller re-runs Gate 1 itself on the branch head before trusting
anything the worker claimed (ASES-REV-05) -- a red result sends the card straight back, no reviewer
turn spent on it. If Gate 1 is green, Hermes's own dispatcher spawns the reviewer profile
(`kanban.review_dispatch: true`, the default) and the reviewer's verdict IS the resulting card
transition: `kanban_complete` -> done (PASS), `kanban_request_changes` -> back to ready
(CHANGES_REQUIRED), `kanban_block` -> blocked (BLOCKED). The run metadata that rides on that tool call is
the structured review (ASES-REV-06): validate_verdict checks it against the schema and record_verdict
stores it by commit SHA, so a verdict belongs to the one commit it was given for (ASES-GIT-03).

The review-lane path cannot be the only line of defence (ASES-GIT-03, ASES-GIT-13). Hermes's own gateway
dispatcher can claim a review card and start the reviewer before the controller has looked at it, so a
card can reach the merge queue with the scope check and the Gate 1 record never having happened.
check_branch_for_merge is the merge queue's own, independent check: it repeats the resolution, merge-base
and touches checks and then believes only the controller's own gate_runs records (ASES-QG-01), never the
review lane having seen the card.

The controller's own send-back is `reopen-review` (hermes.kanban_reopen_review), NOT `request-changes`:
request-changes is the reviewer's verdict and Hermes rejects it (exit 1, "task is not in an active review
run") on a card that is merely sitting in `review`, which is where this module finds it (2026-09-19 fix,
verified against real Hermes; every earlier test mocked the wrapper, so none could see it).
"""
from __future__ import annotations

import dataclasses
import json
import pathlib
import re
import subprocess
from datetime import datetime, timezone

from . import events as events_mod
from . import gates as gates_mod
from . import hermes as hermes_mod
from . import integrity


@dataclasses.dataclass(frozen=True)
class BranchCheck:
    """The result of check_branch / check_branch_for_merge. `kind` is one of "ok", "unresolvable_branch",
    "no_merge_base", "out_of_scope", "gate1_red", "stale_review", "unbound_review". For a failure `detail` is the reason text
    (exactly what gate_before_review sends to Hermes); for "ok" it is a short note. `head` is the commit the
    check ran against, or "" when the branch could not be resolved."""

    ok: bool
    kind: str
    detail: str
    head: str


def gate_before_review(
    board: str, card_id: str, repo: pathlib.Path, branch: str, integration_branch: str,
    gate1_commands: list[str], touches: list[str], *, conn, task_key: str,
) -> bool:
    """ASES-REV-05 (Gate 1 re-check) + ASES-GIT-13 (touches-path check). Returns True if both pass
    (card stays in review for the reviewer), False if it sent the card back (a failed attempt, not a
    review round). A thin wrapper: check_branch makes the decision, this owns the send-back to Hermes.

    integration_branch is the plan's configured integration branch (the same value mergeq.merge_task
    and the rest of ASES already receive as a parameter); the touches check diffs `branch` against its
    merge-base with it. That used to be a hardcoded "integration" literal (2026-09-19 fix), which only
    worked because config/swarm.yaml's integration_branch happens to be spelled exactly that. Under any
    other name the merge-base lookup failed, `base` came back empty, and the check silently fell back
    to inspecting only the branch's LAST commit -- so an out-of-scope path in any earlier commit was
    never seen. It now fails closed instead: no merge-base means the card is sent back, not guessed at."""
    result = check_branch(
        repo, branch, integration_branch, gate1_commands, touches, conn=conn, task_key=task_key,
    )
    if not result.ok:
        hermes_mod.kanban_reopen_review(board, card_id, result.detail)
        return False
    return True


def check_branch(
    repo: pathlib.Path, branch: str, integration_branch: str, gate1_commands: list[str],
    touches: list[str], *, conn, task_key: str,
) -> BranchCheck:
    """The decision behind gate_before_review, with no Hermes call: resolve the branch head, find its
    merge-base with the integration branch, hold the diff to the card's touches, then re-run Gate 1 on
    the head (which records a gate_runs row). Gate 1 always runs when the scope check passes; the merge
    queue uses check_branch_for_merge instead, which reuses a record the controller already holds."""
    scope = _check_scope(repo, branch, integration_branch, touches)
    if not scope.ok:
        return scope
    return _run_gate1(repo, scope.head, gate1_commands, conn=conn, task_key=task_key)


def check_branch_for_merge(
    repo: pathlib.Path, branch: str, integration_branch: str, gate1_commands: list[str],
    touches: list[str], *, conn, task_key: str, require_binding: bool = False,
    reviewed_commit: str | None = None,
) -> BranchCheck:
    """The merge queue's own branch check (ASES-GIT-03, ASES-GIT-13, ASES-QG-01). It does not rely on the
    review lane: Hermes's own gateway dispatcher can claim a review card and start the reviewer before
    gate_before_review has seen it, so a card can reach the merge queue with the scope check and the Gate 1
    record never having happened. This repeats the resolution, merge-base and touches checks (a scope
    violation blocks even when the review lane never saw the card), then believes only the controller's own
    Gate 1 records for this task, never anything the worker or the reviewer said:

    - a green record for this exact head is reused and Gate 1 is not run again;
    - otherwise a green record for a DIFFERENT commit means the branch moved after Gate 1, and so after the
      review that Gate 1 gated: "stale_review", and nothing is run, because a later commit voids both the
      review and the gate record (ASES-GIT-03);
    - otherwise there is no green record at all (for example the review-lane re-check was skipped by that
      race), so Gate 1 runs now on the head and records its row: green is ok, red is "gate1_red".

    Only result 'pass' rows count, so a red record for the head is never read as green. `head` on the
    result is the commit that was checked: merge that SHA rather than re-resolving the branch name, or a
    commit pushed in between would ride in unchecked.

    With `require_binding` (the merge queue always passes it) the reviewer's approval must be tied to a commit
    (ASES-GIT-03: "a reviewer PASS for the same commit SHA. Any later commit voids both"), and `reviewed_commit`
    is that commit as far as the controller can tell: what the reviewer's verdict quoted, else what the coder's
    hand-off named (a nemotron review found the gap: the Hermes review skill's verdict shape has no commit
    field, so without this a commit added after the approval, with no Gate 1 record yet, would merge under
    the old approval). None means neither names one and the approval cannot be bound: "unbound_review".
    A head that does not start with the reviewed commit means a later commit exists: "stale_review". Both are
    decided before anything is run. A prefix compare, case-insensitive, because a quoted SHA may be short."""
    scope = _check_scope(repo, branch, integration_branch, touches)
    if not scope.ok:
        return scope
    head = scope.head

    if require_binding:
        if not reviewed_commit:
            return BranchCheck(
                False, "unbound_review",
                "neither the reviewer's verdict nor the coder's hand-off names the commit that was reviewed, so "
                f"the approval cannot be bound to a commit and {head} cannot be shown to be the one that was "
                "reviewed (ASES-GIT-03). It needs a review that names its commit.",
                head,
            )
        if not head.lower().startswith(reviewed_commit.lower()):
            return BranchCheck(
                False, "stale_review",
                f"the branch head {head} is not the reviewed commit {reviewed_commit}: a later commit exists, and "
                "a later commit voids the review and the gate record (ASES-GIT-03), so it needs its own Gate 1 "
                "run and a reviewer PASS of its own before it can merge.",
                head,
            )

    same_head = conn.execute(
        "SELECT 1 FROM gate_runs WHERE task_key = ? AND gate = 'gate1' AND result = 'pass' "
        "AND commit_sha = ? LIMIT 1",
        (task_key, head),
    ).fetchone()
    if same_head:
        return BranchCheck(True, "ok", f"Gate 1 green for {head} (existing controller record, not re-run)", head)

    earlier = conn.execute(
        "SELECT commit_sha FROM gate_runs WHERE task_key = ? AND gate = 'gate1' AND result = 'pass' "
        "AND commit_sha != ? ORDER BY id DESC LIMIT 1",
        (task_key, head),
    ).fetchone()
    if earlier:
        return BranchCheck(
            False, "stale_review",
            f"the branch head moved after review: Gate 1 was green for {earlier[0]}, head is {head}. "
            f"A later commit voids the review and the gate record, so {head} needs its own Gate 1 run "
            "and a reviewer PASS of its own before it can merge.",
            head,
        )

    return _run_gate1(repo, head, gate1_commands, conn=conn, task_key=task_key)


def _check_scope(
    repo: pathlib.Path, branch: str, integration_branch: str, touches: list[str],
) -> BranchCheck:
    """The part of a branch check that needs no gate run: resolve the head, find the merge-base with the
    integration branch, and hold the diff to the declared touches (ASES-GIT-13). Returns ok=True with the
    resolved head, or the first failure. check_branch and check_branch_for_merge both call this, so the
    review lane and the merge queue can never disagree about what counts as a scope violation."""
    # --verify -q and the exit code, not bare stdout: `git rev-parse <bad-ref>` echoes the bad argument on
    # stdout while failing, so a missing branch used to come back as a non-empty "head" and sail past the
    # guard below (found 2026-09-19 by a test that asked for a branch that does not exist).
    resolved = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "-q", branch], capture_output=True, text=True,
    )
    head = resolved.stdout.strip() if resolved.returncode == 0 else ""
    if not head:
        return BranchCheck(False, "unresolvable_branch", f"could not resolve branch {branch}", "")

    # The merge-base is taken from the resolved head, not the branch name, so it and the diff below are
    # computed from the same commit even if the branch moves while this runs.
    base = subprocess.run(
        ["git", "-C", str(repo), "merge-base", integration_branch, head], capture_output=True, text=True,
    ).stdout.strip()
    if not base:
        # Fail closed. Falling back to inspecting only the branch's LAST commit (what this used to do)
        # lets an out-of-scope path in any earlier commit through, and it is exactly what an unrelated
        # history or a missing integration branch would trigger without anyone noticing.
        return BranchCheck(
            False, "no_merge_base",
            f"could not compute a merge-base between '{integration_branch}' and '{branch}', so the "
            f"touches check cannot be trusted",
            head,
        )
    changed = _changed_since(repo, base, head)
    out_of_scope = integrity.paths_outside_touches(changed, touches)
    if out_of_scope:
        return BranchCheck(
            False, "out_of_scope",
            f"diff touches paths outside the card's declared touches ({touches}): {out_of_scope}. "
            "Either the task needs widening or these changes need to come out.",
            head,
        )
    return BranchCheck(True, "ok", "", head)


def _run_gate1(
    repo: pathlib.Path, head: str, gate1_commands: list[str], *, conn, task_key: str,
) -> BranchCheck:
    """Run Gate 1 on `head` through the gate runner (which records the result in gate_runs) and translate
    it. The red reason text is what gate_before_review has always sent to Hermes for a failed re-check."""
    result = gates_mod.run_gate(repo, head, "gate1", gate1_commands, conn=conn, task_key=task_key)
    if not result.passed:
        return BranchCheck(
            False, "gate1_red", f"Gate 1 failed on the controller's re-check:\n{_evidence(result.detail)}", head,
        )
    return BranchCheck(True, "ok", f"Gate 1 green for {head}", head)


def _evidence(detail: str, limit: int = 1500) -> str:
    """Gate output trimmed to `limit` characters keeping BOTH ends. It used to keep only the first 1500, so a
    long red run lost the failure summary and the [exit N] marker, which are at the tail (2026-09-19)."""
    if len(detail) <= limit:
        return detail
    marker = "\n...[trimmed]...\n"
    head = limit // 3
    return detail[:head] + marker + detail[-(limit - head - len(marker)):]


def _changed_since(repo: pathlib.Path, base: str, head: str) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), "diff", "--name-only", "--no-renames", f"{base}..{head}"],
        capture_output=True, text=True,
    )
    # --no-renames (2026-09-19): with rename detection `git mv SECRETS.md src/a.py` lists only src/a.py, so the
    # removed out-of-scope path was never seen by the touches check. Both sides of a rename must be in scope.
    return [ln for ln in result.stdout.splitlines() if ln.strip()]


def card_status(board: str, card_id: str) -> str:
    return hermes_mod.kanban_show(board, card_id)["status"]


REVIEW_OUTCOME_STATUSES = frozenset({"done", "ready", "blocked", "review", "running"})


# --- the reviewer's verdict (ASES-REV-06, ASES-GIT-03) ---------------------------------------------------

_OUTCOMES = ("PASS", "CHANGES_REQUIRED", "BLOCKED")
# The optional list-valued keys of both accepted shapes: the blueprint's (architecture_issues ...
# required_changes) and the Hermes review skill's (reviewer_checks).
_LIST_FIELDS = (
    "architecture_issues", "missing_cases", "security_issues", "test_gaps", "required_changes",
    "reviewer_checks",
)
_COMMIT_PATTERN = re.compile(r"[0-9a-fA-F]{7,40}")


@dataclasses.dataclass(frozen=True)
class Verdict:
    """A reviewer's structured review after validation (ASES-REV-06). `valid` means well formed and nothing
    more: a CHANGES_REQUIRED or BLOCKED verdict is valid, and the caller decides what to do with `outcome`.
    `outcome` is normalised to "PASS", "CHANGES_REQUIRED" or "BLOCKED", and is None when the outcome keys
    are missing, unreadable or contradict each other. `commit` is the SHA the reviewer says it reviewed
    (7 to 40 hex characters), or None when it did not say or wrote something malformed (which is also a
    problem). `tamper_suspected` is the reviewer's gate_tampering_suspected flag. Check `valid` before
    acting on any other field: `outcome` stays readable when only another field is malformed."""

    valid: bool
    outcome: str | None
    problems: tuple[str, ...]
    commit: str | None
    tamper_suspected: bool


def _show(value: object, limit: int = 60) -> str:
    """A value for a problem sentence, clipped: it comes from an agent and can be any size."""
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _unreadable(problem: str) -> Verdict:
    return Verdict(valid=False, outcome=None, problems=(problem,), commit=None, tamper_suspected=False)


def validate_verdict(metadata: object) -> Verdict:
    """ASES-REV-06: check a reviewer's run metadata against the review schema. `metadata` may be a dict,
    a JSON string (it sometimes reaches the controller as text) or None.

    Two shapes are accepted and mean the same thing. The blueprint's own schema (section 13.3):
    review_status ("PASS", "CHANGES_REQUIRED" or "BLOCKED"), commit, summary, architecture_issues,
    missing_cases, security_issues, test_gaps, gate_tampering_suspected, required_changes. And the shape the
    reviewers we actually run emit, from Hermes's own review skill: review_outcome "approved" (case
    sensitive, normalised to "PASS") and reviewer_checks. When both outcome keys are present they must
    agree; a PASS next to anything but "approved" is a contradiction, so the outcome is None rather than
    whichever key was read first.

    Optional keys are checked only when present: commit must be a string of 7 to 40 hex characters (a
    reviewer without a terminal may not know it, so leaving it out is fine, but a JSON null is not); the
    list-valued keys must be lists; gate_tampering_suspected must be a bool. Extra keys are tolerated.
    Every problem is a short sentence in `problems`; the verdict is valid only when there are none."""
    if isinstance(metadata, str):
        if metadata.strip():
            try:
                metadata = json.loads(metadata)
            except (ValueError, RecursionError):
                # RecursionError is not a ValueError: a string of a few thousand "[" would otherwise
                # raise straight out of the polling loop. The text comes from an agent.
                return _unreadable("metadata is not a JSON object (it does not parse as JSON)")
        else:
            metadata = None
    if metadata is None:
        return _unreadable("metadata is missing")
    if not isinstance(metadata, dict):
        return _unreadable(f"metadata is not a JSON object (got {type(metadata).__name__})")

    problems: list[str] = []

    outcomes: list[str | None] = []  # the normalised outcome of each outcome key present, None if unusable
    if "review_status" in metadata:
        value = metadata["review_status"]
        usable = isinstance(value, str) and value in _OUTCOMES
        outcomes.append(value if usable else None)
        if not usable:
            problems.append(f"review_status is {_show(value)}, expected PASS, CHANGES_REQUIRED or BLOCKED")
    if "review_outcome" in metadata:
        value = metadata["review_outcome"]
        usable = isinstance(value, str) and value == "approved"
        outcomes.append("PASS" if usable else None)
        if not usable:
            problems.append(f"review_outcome is {_show(value)}, expected 'approved'")
    if not outcomes:
        problems.append("neither review_status nor review_outcome is present")
    elif len(set(outcomes)) > 1:
        problems.append(
            f"review_status {_show(metadata['review_status'])} and review_outcome "
            f"{_show(metadata['review_outcome'])} disagree"
        )
    outcome = outcomes[0] if outcomes and len(set(outcomes)) == 1 else None

    commit = None
    if "commit" in metadata:
        value = metadata["commit"]
        if isinstance(value, str) and _COMMIT_PATTERN.fullmatch(value):
            commit = value
        else:
            problems.append(f"commit is not 7 to 40 hex characters (got {_show(value)})")

    for name in _LIST_FIELDS:
        if name in metadata and not isinstance(metadata[name], list):
            problems.append(f"{name} is not a list (got {type(metadata[name]).__name__})")

    tamper_suspected = False
    if "gate_tampering_suspected" in metadata:
        value = metadata["gate_tampering_suspected"]
        if isinstance(value, bool):
            tamper_suspected = value
        else:
            problems.append(f"gate_tampering_suspected is not a bool (got {type(value).__name__})")

    return Verdict(
        valid=not problems, outcome=outcome, problems=tuple(problems), commit=commit,
        tamper_suspected=tamper_suspected,
    )


def verdict_matches_head(verdict: Verdict, head: str) -> bool:
    """ASES-GIT-03: a reviewer's PASS counts for one commit. True when the reviewer did not name one
    (verdict.commit is None: the controller binds the verdict to the head it observed, and there is
    nothing to contradict), or when the commit it named is a prefix of `head` (a reviewer-quoted short SHA
    must be a prefix of the full head, compared without regard to hex case). False otherwise. Ask this only
    of a valid verdict: a malformed commit is reported as a problem and comes back as None."""
    if verdict.commit is None:
        return True
    return head.lower().startswith(verdict.commit.lower())


def record_verdict(
    conn, project: str, task_key: str, commit_sha: str, card_id: str, outcome: str,
    reviewer_profile: str, metadata: dict | str | None,
) -> None:
    """ASES-REV-06 + ASES-GIT-03: store a reviewer's verdict by (project, task_key, commit_sha), so it can
    only ever be read back for the commit it was given for. `outcome` is the normalised outcome from
    validate_verdict ("PASS", "CHANGES_REQUIRED" or "BLOCKED"). `metadata` (a dict, a JSON string or None)
    is stored as JSON after events.redact, so a secret-shaped value never reaches the table. An upsert:
    recording the same verdict again leaves one row, and a later verdict for the same commit replaces the
    earlier one."""
    conn.execute(
        "INSERT INTO review_verdicts (project, task_key, commit_sha, card_id, outcome, reviewer_profile, "
        "metadata, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(project, task_key, commit_sha) DO UPDATE SET card_id=excluded.card_id, "
        "outcome=excluded.outcome, reviewer_profile=excluded.reviewer_profile, "
        "metadata=excluded.metadata, recorded_at=excluded.recorded_at",
        (project, task_key, commit_sha, card_id, outcome, reviewer_profile, _redacted_json(metadata),
         datetime.now(timezone.utc).isoformat(timespec="seconds")),
    )


def _redacted_json(metadata: dict | str | None) -> str | None:
    """`metadata` as redacted JSON text for the review_verdicts table; None stays NULL. A JSON string is
    parsed first so events.redact sees its keys and values (a credential-named key is replaced wholesale);
    text that is not JSON is kept as a string, which redact still scans for secret shapes."""
    value = metadata
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):
            pass
    if value is None:
        return None
    return json.dumps(events_mod.redact({"metadata": value})["metadata"], default=str)
