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
    before its parent is actually merged.

    Safe to re-run on an in-flight plan (2026-09-19 fix): once a task has spent a fix card,
    process_merge_queue has repointed plan_tasks.work_card_id at it, and a re-approve (also how gate
    configuration is changed, ASES-QG-02) gets the ORIGINAL work card back from the idempotent create
    below. The upsert used to reset the column to that original card, forgetting the live fix card, so
    it now leaves work_card_id alone for any task with fix_cards > 0."""
    pairs: dict[str, CardPair] = {}
    order = plan_mod.topological_order(plan)
    reviewer_profile = _reviewer_profile(project)

    for key in order:
        task = plan.task(key)
        assignee = policy.resolve_assignee(task.role, project.roles)
        parent_merge_ids = [pairs[dep].merge_card_id for dep in task.depends_on]

        work = hermes_mod.kanban_create(
            board, f"{key}: {task.title}", assignee=assignee, workspace="worktree",
            branch=f"swarm/{key}-{task.role}", project=project_id,
            body=_work_card_body(task, reviewer_profile), parent=parent_merge_ids or None,
            idempotency_key=f"ases-work-{plan.project}-{key}",
            max_runtime=f"{project.budgets.get('card_runtime_minutes', 45)}m",
        )
        merge = hermes_mod.kanban_create(
            board, f"{key}: merge", workspace="scratch", project=project_id,
            body=f"Merge candidate for {key}. Controller-owned; never assigned to an agent.",
            parent=[work["id"]], initial_status="blocked",
            idempotency_key=f"ases-merge-{plan.project}-{key}",
        )
        pairs[key] = CardPair(key, work["id"], merge["id"])
        # work_card_id is the task's CURRENT card: process_merge_queue repoints it at each fix card it
        # opens and bumps fix_cards in the same statement. A re-approve must not undo that, and it gets
        # the ORIGINAL work card id back from the idempotent create above, so the incoming id is only
        # taken while no fix card has been spent for this task (2026-09-19 fix).
        conn.execute(
            "INSERT INTO plan_tasks (project, task_key, work_card_id, merge_card_id, role, touches, "
            "gate_profile, estimated_requests, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(project, task_key) DO UPDATE SET "
            "work_card_id=CASE WHEN plan_tasks.fix_cards > 0 THEN plan_tasks.work_card_id "
            "ELSE excluded.work_card_id END, "
            "merge_card_id=excluded.merge_card_id",
            (plan.project, key, work["id"], merge["id"], task.role,
             __import__("json").dumps(list(task.touches)), task.gate_profile, task.estimated_requests),
        )
        events.record(conn, "cards_created", {"task_key": key, "work": work["id"], "merge": merge["id"]})

    return list(pairs.values())


def _scope_lines(task: plan_mod.PlanTask) -> list[str]:
    """The lines that scope a task for its worker: the paths it may change and the gate profile it must
    pass. On every work card body, and on every fix card body (the hand-off steps refer to both, and a
    fix card used to carry neither)."""
    lines = []
    if task.touches:
        lines.append("Touches: " + ", ".join(task.touches))
    lines.append(f"Gate profile: {task.gate_profile}")
    return lines


def _work_card_body(task: plan_mod.PlanTask, reviewer_profile: str = "reviewer") -> str:
    lines = [f"Role: {task.role}", "Acceptance criteria:"]
    lines += [f"- {a}" for a in task.acceptance]
    lines += _scope_lines(task)
    lines += ["", *_finish_instructions(task.role, reviewer_profile)]
    return "\n".join(lines)


def _reviewer_profile(project: ases_config.ProjectConfig) -> str:
    """The Hermes profile a coder must name as reviewer= when it calls kanban_request_review. Hermes only
    reassigns the card when reviewer= is given (there is no default reviewer), so without it the card stays
    with the implementer, who would then review its own work. Resolved like every other role, from the
    roles: map in config/swarm.yaml."""
    try:
        return policy.resolve_assignee("reviewer", project.roles)
    except policy.UnknownRoleError:
        return "reviewer"  # a missing mapping is doctor's job to flag, not the card body's


def _completing_profile(work_card: dict) -> str | None:
    """The Hermes profile that ran the work card's latest COMPLETED run, or None when no run completed it.
    Hermes keeps one run per attempt with the profile that ran it and its outcome (`_runs`, see
    hermes.kanban_show): a card the reviewer approved ends with a reviewer run whose outcome is "completed",
    while a card its implementer finished itself ends with the implementer's."""
    completed = [run for run in work_card.get("_runs", []) if run.get("outcome") == "completed"]
    return completed[-1].get("profile") if completed else None


def _refuse_unreviewed(
    conn, task_key: str, work_card: dict, completed_by: str | None, reviewer_profile: str,
) -> None:
    """Record, once per work card, that the merge queue refused it because the reviewer profile did not
    complete it. Once, not on every poll: a pass repeats every few seconds and would otherwise add an
    identical event each time for as long as the card sits there."""
    seen = conn.execute(
        "SELECT 1 FROM events WHERE kind = 'merge_refused_unreviewed' "
        "AND json_extract(payload, '$.card_id') = ? LIMIT 1", (work_card["id"],),
    ).fetchone()
    if seen is None:
        events.record(conn, "merge_refused_unreviewed", {
            "task_key": task_key, "card_id": work_card["id"], "completed_by": completed_by,
            "needs_completion_by": reviewer_profile,
        })


def _finish_instructions(role: str, reviewer_profile: str = "reviewer") -> list[str]:
    """How a worker hands its card off (ASES-REV-04: a review request carries handoff evidence). Appended to
    every work card body and every fix card body. A coder commits and asks for review, and must NOT complete
    its own card: that would let the merge queue run before anyone independent had looked at the diff. Any
    other role (a reviewer) has no commit to hand off: it completes with its verdict, and the merge card
    for its task completes as a recorded no-op."""
    if role == "coder":
        return [
            "How to finish (ASES):",
            "1. Make the change only inside your worktree and only on the paths listed under Touches.",
            "2. Run the commands of your gate profile and fix what they report. Never edit tests, gate settings "
            "or CI files to make a check pass.",
            "3. Commit your work on this card's branch (git add, then git commit). Uncommitted work is not merged.",
            f"4. Hand off with kanban_request_review, and always pass reviewer=\"{reviewer_profile}\" so the "
            "independent reviewer profile takes the card (without it the card stays assigned to you and you "
            "would review your own work). Give a one or two sentence summary, plus metadata with "
            "changed_files, the verification commands you ran, residual_risk and the commit SHA. Do NOT call "
            "kanban_complete on this card. The reviewer completes it after approving, and only then does the "
            "merge queue run.",
        ]
    return [
        "How to finish (ASES):",
        "1. Read the acceptance criteria above and inspect the code in your worktree. You review; you do not "
        "edit product files.",
        "2. Give your verdict with kanban_complete: the summary starts with PASS or FAIL, and the metadata "
        "carries verdict, findings and criteria_checked.",
        "3. If you need a human decision, call kanban_block with one precise question.",
        "4. This card has no commit to merge; its merge card completes as a recorded no-op.",
    ]


class GateConfigTamperedError(RuntimeError):
    pass


def pin_gate_profiles(conn, project: str, gate_profiles: dict) -> str:
    """ASES-QG-02: pin this project's approved gate profiles by content hash at `swarm approve` time,
    so `swarm run` can refuse to trust a diff that quietly changed gate configuration, CI scripts, or
    test-runner settings. Called only after publish_plan succeeds (see cmd_approve) -- a plan refused
    earlier by a data-policy or budget gate must never reach this and get pinned.

    Re-approving (a fresh `swarm approve`) intentionally moves the pin to whatever is approved now --
    that's the sanctioned way to change gate configuration, not a bypass of it."""
    digest = gates_mod.hash_gate_profiles(gate_profiles)
    conn.execute(
        "INSERT INTO gate_pins (project, gate_profiles_hash, pinned_at) VALUES (?, ?, datetime('now')) "
        "ON CONFLICT(project) DO UPDATE SET gate_profiles_hash=excluded.gate_profiles_hash, "
        "pinned_at=excluded.pinned_at",
        (project, digest),
    )
    return digest


def verify_gate_pin(conn, project: str, gate_profiles: dict) -> None:
    """ASES-QG-02: refuse to proceed if the plan's gate profiles no longer match what was pinned at
    this project's last `swarm approve`. No pin row means this project has never been through the
    pinning path yet -- nothing to verify against, so this is a silent no-op rather than a false
    positive on a first-ever approve."""
    row = conn.execute(
        "SELECT gate_profiles_hash FROM gate_pins WHERE project = ?",
        (project,),
    ).fetchone()
    if row is None:
        return
    current = gates_mod.hash_gate_profiles(gate_profiles)
    if row["gate_profiles_hash"] != current:
        raise GateConfigTamperedError(
            f"gate configuration for project {project!r} no longer matches the pin recorded at the "
            f"last `swarm approve` (ASES-QG-02): gate commands, CI scripts, or test-runner settings "
            f"changed without an explicit approved plan task allowing it. Re-run `swarm approve` if "
            f"this change is intentional."
        )


def process_budget_gate(
    board: str, plan: plan_mod.Plan, models_config: dict, *, conn, budgets: dict,
) -> list[str]:
    """ASES-CAP-03, ongoing case: a card affordable at Gate P time may not be affordable anymore by
    the time it's actually about to run (other real-world usage, a quota reset that hasn't happened
    yet). Checked every pass, not just once at approval -- parks with `hermes kanban schedule` rather
    than failing outright; a scheduled card is picked back up once the reset time genuinely arrives
    (the reset itself is a human/cron action in Phase 3; auto-unblock-on-reset is a later refinement).

    The plan_tasks lookup is scoped to `plan.project` (2026-09-19 fix): a board can carry more than one
    project's cards at once (Hermes projects share a board on purpose), and two different projects'
    plans commonly reuse the same task keys ("T1", "T2", ...). Before this fix, a "ready" card from a
    DIFFERENT project with a colliding key would resolve via `plan.task()` against the wrong plan's
    task definition -- caught while about to run a second, unrelated real project on the same board as
    an in-flight one, before it actually happened, not after."""
    parked = []
    for card in hermes_mod.kanban_list(board, status="ready"):
        row = conn.execute(
            "SELECT task_key FROM plan_tasks WHERE work_card_id = ? AND project = ?",
            (card["id"], plan.project),
        ).fetchone()
        if row is None:
            continue
        task = plan.task(row["task_key"])
        pp = policy.profile_provider(task.role, models_config)
        if pp is None:
            continue
        afford = policy.check_budget(
            conn, models_config["providers"], pp.provider, task.estimated_requests, budgets=budgets,
        )
        if not afford.can_afford:
            hermes_mod.kanban_schedule(board, card["id"], f"budget: {afford.reason}")
            parked.append(task.key)
            events.record(conn, "card_parked_for_budget", {"task_key": task.key, "reason": afford.reason})
    return parked


def process_review_lane(
    board: str, repo: pathlib.Path, plan: plan_mod.Plan, *, conn,
) -> list[str]:
    """One pass: for every work card currently in 'review', re-run Gate 1 (ASES-REV-05). Returns the
    task keys that were sent back this pass.

    Scoped to `plan.project`, same reasoning as `process_budget_gate` above -- a "review" card from a
    different project sharing this board must not be matched against this run's plan by task key alone.

    A fix card counts as its task's work card here once `process_merge_queue` has repointed
    plan_tasks.work_card_id at it, so its diff is policed like any other card's."""
    sent_back = []
    for card in hermes_mod.kanban_list(board, status="review"):
        row = conn.execute(
            "SELECT task_key FROM plan_tasks WHERE work_card_id = ? AND project = ?",
            (card["id"], plan.project),
        ).fetchone()
        if row is None:
            continue
        task_key = row["task_key"]
        task = plan.task(task_key)
        gate_cmds = plan.gate_profiles.get(task.gate_profile, [])
        branch = card.get("branch_name") or f"swarm/{task_key}-{task.role}"
        ok = review_mod.gate_before_review(
            board, card["id"], repo, branch, plan.integration_branch, gate_cmds, list(task.touches),
            conn=conn, task_key=task_key,
        )
        if not ok:
            sent_back.append(task_key)
            events.record(conn, "gate1_recheck_failed", {"task_key": task_key})
    return sent_back


def process_merge_queue(
    board: str, repo: pathlib.Path, plan: plan_mod.Plan, project: ases_config.ProjectConfig, *, conn,
    unreviewed: list[str] | None = None,
) -> list[str]:
    """One pass: for every DONE work card whose merge card is still blocked/ready, run the merge.
    Serialized -- one merge_task call at a time, in task order, matching ASES-GIT-04.

    "Done" alone is not approval (ASES-GIT-03, 2026-09-19): a work card only merges if its latest completed
    run belongs to the reviewer profile. The first real coder run finished its own card with kanban_complete
    instead of asking for review, and this queue used to merge anything that read `done`. A card completed
    by anyone else, or by no run at all, is refused: nothing is merged, no fix card is opened, a
    `merge_refused_unreviewed` event is recorded once per card, and the task key is appended to `unreviewed`
    when the caller passes a list, so the polling loop can show it. This checks WHO completed the card, not
    WHICH commit they reviewed: binding the verdict to the exact commit SHA is ASES-REV-06 and is not built.

    On conflict or a red Gate 3 (ASES-GIT-09, ASES-REC-01/02): open a fix card for the same role,
    fresh worktree, the failure bundle attached, and link it as an EXTRA parent of the merge card --
    never send the two original cards back to argue with each other. Bounded by
    budgets.fix_cards_per_task; past that, escalate by blocking the merge card for the user instead
    of creating another fix card.

    Creating a fix card also repoints plan_tasks.work_card_id at it (2026-09-19 fix), so "the work
    card" below, and in process_review_lane and process_budget_gate, always means the task's CURRENT
    card: the original, or the latest fix card. Before this, a fix card was created and then forgotten.
    The merge queue kept re-deriving the original, still-broken branch from work_card_id, so it never
    saw the fix card's output and re-merged the broken branch on every poll while the fix was still in
    flight (burning the fix budget before the fix could even run), and process_review_lane's
    work_card_id lookup could never find the fix card, so Gate 1 never policed its diff. The fix card
    is still parented to the card it replaces (fix2 to fix1 to the original), and is created under
    that card's real Hermes project_id: `project.name` is config/swarm.yaml's own ASES-internal label,
    not a Hermes project id.

    A merge whose fast-forward was refused because the integration branch VERIFIABLY moved underneath
    the candidate (`outcome.integration_moved`, which mergeq.merge_task sets only after comparing the
    candidate's parent with the branch tip in git) is a benign race, not a failure: it gets a free retry
    on the next pass, with no fix card, no fix budget spent, and the merge card left exactly as it was.
    A fast-forward refused with the tip NOT moved (a dirty or wrong-branch primary checkout, an
    index.lock) is not a race and would only be refused again, so it takes the ordinary failure path
    below (2026-09-19 fix): a merge_failed event carrying git's own text, a fix card bounded by
    fix_cards_per_task, then a block for a human. Keying the free retry on gate3_result == "pass" alone
    would have retried those silently on every poll.

    A review-only task (any role other than "coder") has no commit to merge: its branch adds nothing to the
    integration tip. Those are merged with allow_empty, which makes mergeq.merge_task record a no-op instead
    of failing (merged=True, squash_commit None). The merge card is completed with result "no changes to
    merge (review-only task)" and metadata no_op=True, the "merged" event carries no_op=True, and it is never
    a failure: no fix card, no fix budget. A coder's empty branch is still the failure it always was.
    Every squash commit carries the work card and merge card ids in its message (ASES-GIT-06)."""
    merged = []
    fix_limit = project.budgets.get("fix_cards_per_task", 2)
    reviewer_profile = _reviewer_profile(project)

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

        completed_by = _completing_profile(work_card)
        if completed_by != reviewer_profile:
            _refuse_unreviewed(conn, key, work_card, completed_by, reviewer_profile)
            if unreviewed is not None:
                unreviewed.append(key)
            continue

        task = plan.task(key)
        gate_cmds = plan.gate_profiles.get(task.gate_profile, [])
        branch = work_card.get("branch_name") or f"swarm/{key}-{task.role}"
        # ASES-GIT-06: one squash commit per plan task, with the card IDs in its message. work_card is the
        # task's CURRENT card (the original, or the latest fix card), so a merge of a fix card's branch names
        # the fix card that produced it.
        commit_message = (
            f"{key}: {task.title}\n\nWork card: {work_card['id']}\nMerge card: {row['merge_card_id']}\n"
            f"Branch: {branch}\nControlled by ASES (one squash commit per plan task)."
        )
        # Only a coder is expected to commit. Any other role (a reviewer) legitimately leaves an empty diff,
        # which merge_task then records as a no-op rather than failing on "nothing to commit".
        outcome = mergeq.merge_task(
            repo, plan.integration_branch, branch, key, gate_cmds, conn=conn,
            commit_message=commit_message, allow_empty=(task.role != "coder"),
        )

        if outcome.merged and outcome.squash_commit is None:
            # The recorded no-op (an empty diff on a review-only task): nothing was committed or gated and
            # nothing is wrong, so it is never a failure. No fix card, no fix budget. The merge card still
            # completes, which is what releases the tasks that depend on it (ASES-TSK-02).
            hermes_mod.kanban_complete(
                board, row["merge_card_id"],
                result="no changes to merge (review-only task)",
                metadata={"squash_commit": None, "no_op": True},
            )
            merged.append(key)
            events.record(conn, "merged", {"task_key": key, "sha": None, "no_op": True})
            continue
        if outcome.merged:
            hermes_mod.kanban_complete(
                board, row["merge_card_id"],
                result=f"merged {outcome.squash_commit}",
                metadata={"squash_commit": outcome.squash_commit},
            )
            merged.append(key)
            events.record(conn, "merged", {"task_key": key, "sha": outcome.squash_commit})
            continue
        elif outcome.integration_moved:
            # Only the fast-forward was refused, and mergeq verified in git that the integration branch
            # moved underneath the candidate (its parent is no longer the branch tip), so Gate 3's verdict
            # on that exact candidate still stands. Nothing is wrong with the branch, so there is nothing
            # for a fix card to fix: leave the merge card exactly as it is and let the next poll retry for
            # free (2026-09-19 fix -- this used to cost a real coder turn and one unit of fix-card budget).
            # Keyed on integration_moved, NOT on gate3_result == "pass": git also refuses a fast-forward
            # with the tip unmoved (dirty or wrong-branch primary checkout, index.lock), and treating that
            # as a race retried it silently on every poll instead of letting it surface below.
            events.record(conn, "merge_race_retrying", {"task_key": key, "detail": outcome.detail[:500]})
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
        # project= is the real Hermes project id, read off the card being fixed (`work_card`, fetched
        # above and not yet repointed), never `project.name` (2026-09-19 fix). parent= is likewise the
        # card being replaced, so this is created BEFORE work_card_id is repointed below.
        fix_card = hermes_mod.kanban_create(
            board, f"{key}: fix (round {row['fix_cards'] + 1})", assignee=assignee,
            workspace="worktree", branch=fix_branch, project=work_card.get("project_id"),
            body=(f"Merge attempt for {key} failed. Fix in a fresh worktree.\n\n"
                  f"Failure detail:\n{outcome.detail[:1500]}\n\n"
                  + "\n".join([*_scope_lines(task), "", *_finish_instructions(task.role, reviewer_profile)])),
            parent=[row["work_card_id"]],
            idempotency_key=f"ases-fix-{plan.project}-{key}-{row['fix_cards'] + 1}",
            max_runtime=f"{project.budgets.get('card_runtime_minutes', 45)}m",
        )
        hermes_mod.kanban_link(board, fix_card["id"], row["merge_card_id"])
        # Spend the fix budget and repoint work_card_id in ONE statement: the next pass then waits for
        # the fix card to reach done, merges the fix card's branch, and the review lane can find it.
        conn.execute(
            "UPDATE plan_tasks SET fix_cards = fix_cards + 1, work_card_id = ? "
            "WHERE project = ? AND task_key = ?",
            (fix_card["id"], plan.project, key),
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


def run_pass(
    board: str, repo: pathlib.Path, plan: plan_mod.Plan, project: ases_config.ProjectConfig,
    models_config: dict, *, conn,
) -> dict:
    """One full controller iteration: budget gate, review-lane policing, dispatch, merge queue.
    Returns a small summary dict for logging -- this is what a bounded `swarm run` loop calls
    repeatedly (section 9.2's pseudocode), sleeping between calls to respect provider pacing.

    Review-lane policing runs BEFORE dispatch (2026-09-19), as in the blueprint's loop, which re-runs Gate 1
    for cards that entered review first. `kanban_dispatch` also claims cards waiting in `review` and spawns
    their reviewer, and the Gate 1 re-check only acts on cards still sitting in `review`, so with the old
    order (dispatch first) it was skipped for every card that dispatch reached first. Not closed: Hermes's
    own gateway dispatcher can still claim a review card between two of these passes; Gate 3 at merge time is
    the backstop for that."""
    unreviewed: list[str] = []
    parked = process_budget_gate(board, plan, models_config, conn=conn, budgets=project.budgets)
    sent_back = process_review_lane(board, repo, plan, conn=conn)
    dispatch_result = hermes_mod.kanban_dispatch(board)
    merged = process_merge_queue(board, repo, plan, project, conn=conn, unreviewed=unreviewed)
    finished = all_merge_cards_done(board, plan, conn=conn)
    return {"parked": parked, "dispatch": dispatch_result, "sent_back": sent_back, "merged": merged,
            "unreviewed": unreviewed, "finished": finished}
