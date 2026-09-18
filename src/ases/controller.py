"""The main orchestration loop (section 9.1 calls this the responsibility of cli.py's `run`; kept as
its own module so the loop body is testable without going through argv).

Never claims or spawns a card itself (ASES-ARC-02) -- it creates cards, reads board state, and performs
the deterministic steps agents aren't trusted with: card creation from an approved plan, the Gate 1
re-check before trusting a review, and the merge queue. Dispatch itself is Hermes's own
`kanban dispatch` / gateway.
"""
from __future__ import annotations

import dataclasses
import pathlib
import subprocess
import time

from . import config as ases_config
from . import db as ases_db
from . import events
from . import gates as gates_mod
from . import hermes as hermes_mod
from . import mergeq
from . import plan as plan_mod
from . import policy
from . import review as review_mod


@dataclasses.dataclass(frozen=True)
class CardPair:
    task_key: str
    work_card_id: str
    merge_card_id: str


def publish_plan(repo: pathlib.Path, integration_branch: str) -> str:
    """ASES-ARC-09 (v1.2): commit docs/ases/ to the integration branch and return that commit's SHA,
    BEFORE any implementation card is created. A worktree cut after this point sees the approved plan;
    one cut before it wouldn't. The repo's checked-out branch must already be integration_branch --
    Phase 3's `swarm plan` writes straight into the primary checkout, so this is a plain commit, not a
    merge from a separate planning worktree (a real multi-worktree planning flow is a later refinement)."""
    current = subprocess.run(
        ["git", "-C", str(repo), "branch", "--show-current"], capture_output=True, text=True,
    ).stdout.strip()
    if current != integration_branch:
        raise RuntimeError(
            f"publish_plan expected {repo} to be on {integration_branch!r}, found {current!r}"
        )
    subprocess.run(["git", "-C", str(repo), "add", "docs/ases"], check=True, capture_output=True)
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain", "--", "docs/ases"],
        capture_output=True, text=True,
    ).stdout
    if not status.strip():
        # Nothing staged -- plan.json was already committed (e.g. a re-run of `swarm approve`).
        return subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True,
        ).stdout.strip()
    commit = subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "ASES: publish approved plan (Gate P)"],
        capture_output=True, text=True,
    )
    if commit.returncode != 0:
        raise RuntimeError(f"could not commit the approved plan: {commit.stdout}{commit.stderr}")
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True,
    ).stdout.strip()


def create_cards_from_plan(
    board: str, project_id: str, repo_path: pathlib.Path, plan: plan_mod.Plan, project: ases_config.ProjectConfig,
    *, conn,
) -> list[CardPair]:
    """ASES-LED-02/ASES-TSK-01/02: one work + one merge card per task, idempotent by plan key, work
    cards depend on their prerequisites' MERGE cards (not work cards) so a dependent never starts
    before its parent is actually merged."""
    pairs: dict[str, CardPair] = {}
    order = plan_mod.topological_order(plan)

    for key in order:
        task = plan.task(key)
        assignee = policy.resolve_assignee(task.role, project.roles)
        parent_merge_ids = [pairs[dep].merge_card_id for dep in task.depends_on]

        work = hermes_mod.kanban_create(
            board, f"{key}: {task.title}", assignee=assignee, workspace="worktree",
            branch=f"swarm/{key}-{task.role}", project=project_id,
            body=_work_card_body(task), parent=parent_merge_ids or None,
            idempotency_key=f"ases-work-{plan.project}-{key}",
        )
        merge = hermes_mod.kanban_create(
            board, f"{key}: merge", workspace="scratch", project=project_id,
            body=f"Merge candidate for {key}. Controller-owned; never assigned to an agent.",
            parent=[work["id"]], initial_status="blocked",
            idempotency_key=f"ases-merge-{plan.project}-{key}",
        )
        pairs[key] = CardPair(key, work["id"], merge["id"])
        conn.execute(
            "INSERT INTO plan_tasks (project, task_key, work_card_id, merge_card_id, role, touches, "
            "gate_profile, estimated_requests, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(project, task_key) DO UPDATE SET work_card_id=excluded.work_card_id, "
            "merge_card_id=excluded.merge_card_id",
            (plan.project, key, work["id"], merge["id"], task.role,
             __import__("json").dumps(list(task.touches)), task.gate_profile, task.estimated_requests),
        )
        events.record(conn, "cards_created", {"task_key": key, "work": work["id"], "merge": merge["id"]})

    return list(pairs.values())


def _work_card_body(task: plan_mod.PlanTask) -> str:
    lines = [f"Role: {task.role}", "Acceptance criteria:"]
    lines += [f"- {a}" for a in task.acceptance]
    if task.touches:
        lines.append("Touches: " + ", ".join(task.touches))
    lines.append(f"Gate profile: {task.gate_profile}")
    return "\n".join(lines)


def process_review_lane(
    board: str, repo: pathlib.Path, plan: plan_mod.Plan, *, conn,
) -> list[str]:
    """One pass: for every work card currently in 'review', re-run Gate 1 (ASES-REV-05). Returns the
    task keys that were sent back this pass."""
    sent_back = []
    for card in hermes_mod.kanban_list(board, status="review"):
        row = conn.execute(
            "SELECT task_key FROM plan_tasks WHERE work_card_id = ?", (card["id"],)
        ).fetchone()
        if row is None:
            continue
        task_key = row["task_key"]
        task = plan.task(task_key)
        gate_cmds = plan.gate_profiles.get(task.gate_profile, [])
        branch = card.get("branch_name") or f"swarm/{task_key}-{task.role}"
        ok = review_mod.gate_before_review(board, card["id"], repo, branch, gate_cmds, conn=conn, task_key=task_key)
        if not ok:
            sent_back.append(task_key)
            events.record(conn, "gate1_recheck_failed", {"task_key": task_key})
    return sent_back


def process_merge_queue(
    board: str, repo: pathlib.Path, plan: plan_mod.Plan, project: ases_config.ProjectConfig, *, conn,
) -> list[str]:
    """One pass: for every DONE work card whose merge card is still blocked/ready, run the merge.
    Serialized -- one merge_task call at a time, in task order, matching ASES-GIT-04.

    On conflict or a red Gate 3 (ASES-GIT-09, ASES-REC-01/02): open a fix card for the same role,
    fresh worktree, the failure bundle attached, and link it as an EXTRA parent of the merge card --
    never send the two original cards back to argue with each other. Bounded by
    budgets.fix_cards_per_task; past that, escalate by blocking the merge card for the user instead
    of creating another fix card."""
    merged = []
    fix_limit = project.budgets.get("fix_cards_per_task", 2)

    for key in plan_mod.topological_order(plan):
        row = conn.execute(
            "SELECT work_card_id, merge_card_id, fix_cards FROM plan_tasks WHERE project = ? AND task_key = ?",
            (plan.project, key),
        ).fetchone()
        if row is None:
            continue
        work_card = hermes_mod.kanban_show(board, row["work_card_id"])
        merge_card = hermes_mod.kanban_show(board, row["merge_card_id"])
        if work_card["status"] != "done" or merge_card["status"] not in ("blocked", "ready", "todo"):
            continue

        task = plan.task(key)
        gate_cmds = plan.gate_profiles.get(task.gate_profile, [])
        branch = work_card.get("branch_name") or f"swarm/{key}-{task.role}"
        outcome = mergeq.merge_task(repo, plan.integration_branch, branch, key, gate_cmds, conn=conn)

        if outcome.merged:
            hermes_mod.kanban_complete(
                board, row["merge_card_id"],
                result=f"merged {outcome.squash_commit}",
                metadata={"squash_commit": outcome.squash_commit},
            )
            merged.append(key)
            events.record(conn, "merged", {"task_key": key, "sha": outcome.squash_commit})
            continue

        events.record(conn, "merge_failed", {"task_key": key, "detail": outcome.detail[:500]})
        if row["fix_cards"] >= fix_limit:
            hermes_mod.kanban_block(
                board, row["merge_card_id"],
                f"fix-card budget ({fix_limit}) exhausted for {key}; needs a human decision. "
                f"Last failure: {outcome.detail[:500]}",
            )
            events.record(conn, "fix_card_budget_exhausted", {"task_key": key})
            continue

        assignee = policy.resolve_assignee(task.role, project.roles)
        fix_branch = f"swarm/{key}-fix{row['fix_cards'] + 1}"
        fix_card = hermes_mod.kanban_create(
            board, f"{key}: fix (round {row['fix_cards'] + 1})", assignee=assignee,
            workspace="worktree", branch=fix_branch, project=project.name,
            body=(f"Merge attempt for {key} failed. Fix in a fresh worktree.\n\n"
                  f"Failure detail:\n{outcome.detail[:1500]}"),
            parent=[row["work_card_id"]],
            idempotency_key=f"ases-fix-{plan.project}-{key}-{row['fix_cards'] + 1}",
        )
        hermes_mod.kanban_link(board, fix_card["id"], row["merge_card_id"])
        conn.execute(
            "UPDATE plan_tasks SET fix_cards = fix_cards + 1 WHERE project = ? AND task_key = ?",
            (plan.project, key),
        )
        events.record(conn, "fix_card_created", {"task_key": key, "fix_card_id": fix_card["id"]})
    return merged


def all_merge_cards_done(board: str, plan: plan_mod.Plan, *, conn) -> bool:
    for key in [t.key for t in plan.tasks]:
        row = conn.execute(
            "SELECT merge_card_id FROM plan_tasks WHERE project = ? AND task_key = ?",
            (plan.project, key),
        ).fetchone()
        if row is None:
            return False
        if hermes_mod.kanban_show(board, row["merge_card_id"])["status"] != "done":
            return False
    return True


def run_pass(board: str, repo: pathlib.Path, plan: plan_mod.Plan, project: ases_config.ProjectConfig, *, conn) -> dict:
    """One full controller iteration: dispatch, review-lane policing, merge queue.
    Returns a small summary dict for logging -- this is what a bounded `swarm run` loop calls
    repeatedly (section 9.2's pseudocode), sleeping between calls to respect provider pacing."""
    dispatch_result = hermes_mod.kanban_dispatch(board)
    sent_back = process_review_lane(board, repo, plan, conn=conn)
    merged = process_merge_queue(board, repo, plan, project, conn=conn)
    finished = all_merge_cards_done(board, plan, conn=conn)
    return {"dispatch": dispatch_result, "sent_back": sent_back, "merged": merged, "finished": finished}
