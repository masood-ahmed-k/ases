"""Review-lane policing (section 9.1: review.py; section 13.2, ASES-REV-05/06).

When a card enters `review`, the controller re-runs Gate 1 itself on the branch head before trusting
anything the worker claimed (ASES-REV-05) -- a red result sends the card straight back, no reviewer
turn spent on it. If Gate 1 is green, Hermes's own dispatcher spawns the reviewer profile
(`kanban.review_dispatch: true`, the default) and the reviewer's verdict IS the resulting card
transition: `kanban_complete` -> done (PASS), `kanban_request_changes` -> back to ready
(CHANGES_REQUIRED), `kanban_block` -> blocked (BLOCKED). There is no separate verdict payload to parse
here -- the state machine itself is the verdict (ASES-REV-06's schema lives in the run metadata for
audit, not as something this module has to interpret to know what happened).

The controller's own send-back is `reopen-review` (hermes.kanban_reopen_review), NOT `request-changes`:
request-changes is the reviewer's verdict and Hermes rejects it (exit 1, "task is not in an active review
run") on a card that is merely sitting in `review`, which is where this module finds it (2026-09-19 fix,
verified against real Hermes; every earlier test mocked the wrapper, so none could see it).
"""
from __future__ import annotations

import pathlib

from . import gates as gates_mod
from . import hermes as hermes_mod
from . import integrity


def gate_before_review(
    board: str, card_id: str, repo: pathlib.Path, branch: str, integration_branch: str,
    gate1_commands: list[str], touches: list[str], *, conn, task_key: str,
) -> bool:
    """ASES-REV-05 (Gate 1 re-check) + ASES-GIT-13 (touches-path check). Returns True if both pass
    (card stays in review for the reviewer), False if it sent the card back (a failed attempt, not a
    review round).

    integration_branch is the plan's configured integration branch (the same value mergeq.merge_task
    and the rest of ASES already receive as a parameter); the touches check diffs `branch` against its
    merge-base with it. That used to be a hardcoded "integration" literal (2026-09-19 fix), which only
    worked because config/swarm.yaml's integration_branch happens to be spelled exactly that. Under any
    other name the merge-base lookup failed, `base` came back empty, and the check silently fell back
    to inspecting only the branch's LAST commit -- so an out-of-scope path in any earlier commit was
    never seen. It now fails closed instead: no merge-base means the card is sent back, not guessed at."""
    import subprocess

    # --verify -q and the exit code, not bare stdout: `git rev-parse <bad-ref>` echoes the bad argument on
    # stdout while failing, so a missing branch used to come back as a non-empty "head" and sail past the
    # guard below (found 2026-09-19 by a test that asked for a branch that does not exist).
    resolved = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "-q", branch], capture_output=True, text=True,
    )
    head = resolved.stdout.strip() if resolved.returncode == 0 else ""
    if not head:
        hermes_mod.kanban_reopen_review(board, card_id, f"could not resolve branch {branch}")
        return False

    base = subprocess.run(
        ["git", "-C", str(repo), "merge-base", integration_branch, branch], capture_output=True, text=True,
    ).stdout.strip()
    if not base:
        # Fail closed. Falling back to inspecting only the branch's LAST commit (what this used to do)
        # lets an out-of-scope path in any earlier commit through, and it is exactly what an unrelated
        # history or a missing integration branch would trigger without anyone noticing.
        hermes_mod.kanban_reopen_review(
            board, card_id,
            f"could not compute a merge-base between '{integration_branch}' and '{branch}', so the "
            f"touches check cannot be trusted",
        )
        return False
    changed = _changed_since(repo, base, head)
    out_of_scope = integrity.paths_outside_touches(changed, touches)
    if out_of_scope:
        hermes_mod.kanban_reopen_review(
            board, card_id,
            f"diff touches paths outside the card's declared touches ({touches}): {out_of_scope}. "
            "Either the task needs widening or these changes need to come out.",
        )
        return False

    result = gates_mod.run_gate(repo, head, "gate1", gate1_commands, conn=conn, task_key=task_key)
    if not result.passed:
        hermes_mod.kanban_reopen_review(
            board, card_id, f"Gate 1 failed on the controller's re-check:\n{result.detail[:1500]}"
        )
        return False
    return True


def _changed_since(repo: pathlib.Path, base: str, head: str) -> list[str]:
    import subprocess
    result = subprocess.run(
        ["git", "-C", str(repo), "diff", "--name-only", f"{base}..{head}"],
        capture_output=True, text=True,
    )
    return [ln for ln in result.stdout.splitlines() if ln.strip()]


def card_status(board: str, card_id: str) -> str:
    return hermes_mod.kanban_show(board, card_id)["status"]


REVIEW_OUTCOME_STATUSES = frozenset({"done", "ready", "blocked", "review", "running"})
