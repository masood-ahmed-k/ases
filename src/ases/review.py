"""Review-lane policing (section 9.1: review.py; section 13.2, ASES-REV-05/06).

When a card enters `review`, the controller re-runs Gate 1 itself on the branch head before trusting
anything the worker claimed (ASES-REV-05) -- a red result sends the card straight back, no reviewer
turn spent on it. If Gate 1 is green, Hermes's own dispatcher spawns the reviewer profile
(`kanban.review_dispatch: true`, the default) and the reviewer's verdict IS the resulting card
transition: `kanban_complete` -> done (PASS), `kanban_request_changes` -> back to ready
(CHANGES_REQUIRED), `kanban_block` -> blocked (BLOCKED). There is no separate verdict payload to parse
here -- the state machine itself is the verdict (ASES-REV-06's schema lives in the run metadata for
audit, not as something this module has to interpret to know what happened).
"""
from __future__ import annotations

import pathlib

from . import gates as gates_mod
from . import hermes as hermes_mod


def gate_before_review(
    board: str, card_id: str, repo: pathlib.Path, branch: str, gate1_commands: list[str], *,
    conn, task_key: str,
) -> bool:
    """ASES-REV-05. Returns True if Gate 1 passed (card stays in review for the reviewer),
    False if it sent the card back with the failure output (a failed attempt, not a review round)."""
    import subprocess

    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", branch], capture_output=True, text=True,
    ).stdout.strip()
    if not head:
        hermes_mod.kanban_request_changes(board, card_id, f"could not resolve branch {branch}")
        return False

    result = gates_mod.run_gate(repo, head, "gate1", gate1_commands, conn=conn, task_key=task_key)
    if not result.passed:
        hermes_mod.kanban_request_changes(
            board, card_id, f"Gate 1 failed on the controller's re-check:\n{result.detail[:1500]}"
        )
        return False
    return True


def card_status(board: str, card_id: str) -> str:
    return hermes_mod.kanban_show(board, card_id)["status"]


REVIEW_OUTCOME_STATUSES = frozenset({"done", "ready", "blocked", "review", "running"})
