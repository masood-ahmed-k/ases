"""The main orchestration loop (section 9.1 calls this the responsibility of cli.py's `run`; kept as
its own module so the loop body is testable without going through argv).

Never claims or spawns a card itself (ASES-ARC-02) -- it creates cards, reads board state, and performs
the deterministic steps agents aren't trusted with: card creation from an approved plan, the Gate 1
re-check before trusting a review, and the merge queue. Dispatch itself is Hermes's own
`kanban dispatch` / gateway.

Loop version 2 (round 5) wires the phase 4 and 5 modules into run_pass, in the order of blueprint section 9.2
(halt check, primary-checkout guard, idle worktrees, usage ingest, failure recovery, bounds, budget gate and unpark,
review lane, dispatch and provisioning, merge queue, final gates). Each of those steps is a module-level function
of its own, so it can be tested and replaced on its own.

Deliberately NOT built here: the triage lane (ASES-LED-03, cards agents propose land in triage and are validated
before promotion) and per-card model pinning (`policy.pin_model` in the pseudocode): the profile configuration pins
each role's model, and recovery pins one on a card only when it switches models after a second capability failure.
The Lead re-plan call of ASES-REC-02 is not automated either: a spent budget asks the user (see _request_replan).
"""
from __future__ import annotations

import contextlib
import dataclasses
import functools
import importlib
import json
import pathlib
import re
import subprocess
import time
from datetime import datetime, timezone

from . import bounds as bounds_mod
from . import config as ases_config
from . import db as ases_db
from . import events
from . import gates as gates_mod
from . import gitexec
from . import guards as guards_mod
from . import hermes as hermes_mod
from . import intents as intents_mod
from . import leases as leases_mod
from . import mergeq
from . import plan as plan_mod
from . import policy
from . import questions as questions_mod
from . import recovery as recovery_mod
from . import report as report_mod
from . import review as review_mod
from . import sandbox as sandbox_mod
from . import usage as usage_mod


@dataclasses.dataclass(frozen=True)
class CardPair:
    task_key: str
    work_card_id: str
    merge_card_id: str


_BOOTSTRAP_GITIGNORE = "__pycache__/\n*.pyc\n.venv/\nnode_modules/\n"


def _bootstrap_git(repo: pathlib.Path, args: list[str]) -> subprocess.CompletedProcess:
    """One git command against `repo`, for ensure_repo_bootstrapped only: output captured as text and never
    raising, even when git itself cannot be started (a synthetic CompletedProcess with returncode 1 comes back
    instead, the same shape a real failure has, so the caller reads .returncode uniformly either way). Every
    other git call in this module (publish_plan, _branch_diff) is a single invocation and stays inline; this one
    chains several in a row with the same error handling, which is what earns it a helper."""
    try:
        return subprocess.run(
            [*gitexec.GIT, "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, env=gitexec.git_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(args, 1, "", f"git could not be run: {exc}")


def _bootstrap_event(conn, kind: str, repo: pathlib.Path, integration_branch: str, **extra) -> None:
    """Record one of ensure_repo_bootstrapped's own events, only when the caller gave a database connection: a
    plain unit test of the git mechanics alone need not open one (the same optional-conn idiom
    _ingest_outgoing_usage uses for models_config)."""
    if conn is None:
        return
    events.record(conn, kind, {"repo": str(repo), "integration_branch": integration_branch, **extra})


def ensure_repo_bootstrapped(repo: pathlib.Path, integration_branch: str, *, conn=None) -> bool:
    """ASES-GIT-10 (section 8.3): "git worktree add needs at least one commit. swarm run MUST create an initial
    commit and the integration branch when the repository is empty." Called once, from cmd_plan: the earliest
    real touch-point on a brand new project's repository (before the Lead ever inspects it, long before `swarm
    run`'s own primary-checkout guard, ASES-GIT-12, would refuse an empty repo outright). Returns True when it
    had to create the branch or the commit, False when the repository already had at least one commit (left
    completely untouched) or when a step failed (see below): either way there was nothing new to build on.

    "Empty" is asked of git itself, the same question check_primary_checkout effectively answers later: no
    `.git` at all, or a `.git` whose HEAD names no commit yet (`git rev-parse --verify HEAD` fails, the ordinary
    state right after a bare `git init`). A repository that already has history is NEVER touched here, even if
    it is on the wrong branch: that stays publish_plan's own refusal, unchanged, exactly what ASES-GIT-10 asks
    for ("when the repository is empty"), not "when it happens to be on the wrong branch".

    Two different git calls put the repository on `integration_branch`, because they are not interchangeable on
    a genuinely fresh `.git`: `git init -b <name>` only sets the initial branch while `.git` does not exist yet
    (a re-init on an existing `.git` silently ignores --initial-branch, confirmed empirically: `git init -q -b
    x` on an existing unborn repo prints "warning: re-init: ignored --initial-branch=x" and leaves the branch
    alone); `git checkout -B <name>` is what moves an already-unborn HEAD onto that branch name instead (there
    is no commit yet to check out FROM, but checkout -B only ever touches the symbolic ref in that case, is
    idempotent whether or not the branch already exists or is already checked out, confirmed empirically, and
    is safe to repeat if an earlier bootstrap attempt got this far and failed on a later step).

    The commit carries whatever is already on disk (`git add -A`, not just the files below): a repository is
    "empty" here purely by git history, and any file already sitting in the working tree before this ran would
    otherwise show up as an untracked change the moment the primary-checkout guard (ASES-GIT-12) next looks at
    it. This function writes at minimum a .gitignore and a one-line README.md of its own, but never a
    pyproject.toml or similar (that is the plan's own scaffold task's job, ASES-GIT-11, part B) -- and only
    when a file of that name is not already there, so a repository that already had one before bootstrapping
    keeps it.

    Never writes git config (the standing hard rule): the commit's author and committer are given with a
    `-c user.name=... -c user.email=...` pair scoped to that one git invocation, the same throwaway-identity
    pattern already used elsewhere in this codebase for a repository with no identity of its own
    (evalkit/codetasks.py's E8 fixture, fakes/worker.py's _IDENTITY).

    Never raises for an ordinary git failure: a step that fails records a repo_bootstrap_error event (only when
    `conn` is given) naming which step and git's own text, and returns False, leaving the repository exactly as
    git left it for a person to look at. A repo_bootstrapped event, with the branch and the new commit SHA, is
    recorded only on the success path, once something was actually created, so the release report / operations
    log has a trace of it."""
    has_dot_git = (repo / ".git").exists()
    if has_dot_git:
        verify = _bootstrap_git(repo, ["rev-parse", "-q", "--verify", "HEAD"])
        if verify.returncode == 0:
            return False  # real history: never touched here, whatever branch it is on (publish_plan's refusal)
    else:
        try:
            repo.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            _bootstrap_event(conn, "repo_bootstrap_error", repo, integration_branch,
                              step="mkdir", detail=_clean(str(exc), 300))
            return False

    try:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        readme = repo / "README.md"
        if not readme.exists():
            readme.write_text(
                f"This repository was bootstrapped by ASES on {stamp} for this project (ASES-GIT-10, section "
                f"8.3): git needs at least one commit before `git worktree add` can give a card its own "
                f"workspace.\n",
                encoding="utf-8",
            )
        gitignore = repo / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(_BOOTSTRAP_GITIGNORE, encoding="utf-8")
    except OSError as exc:
        _bootstrap_event(conn, "repo_bootstrap_error", repo, integration_branch,
                          step="write_files", detail=_clean(str(exc), 300))
        return False

    if not has_dot_git:
        result, step = _bootstrap_git(repo, ["init", "-q", "-b", integration_branch]), "init"
    else:
        result, step = _bootstrap_git(repo, ["checkout", "-q", "-B", integration_branch]), "checkout"
    if result.returncode != 0:
        _bootstrap_event(conn, "repo_bootstrap_error", repo, integration_branch,
                          step=step, detail=_clean(f"{result.stdout}{result.stderr}", 300))
        return False

    add = _bootstrap_git(repo, ["add", "-A"])
    if add.returncode != 0:
        _bootstrap_event(conn, "repo_bootstrap_error", repo, integration_branch,
                          step="add", detail=_clean(f"{add.stdout}{add.stderr}", 300))
        return False

    commit = _bootstrap_git(repo, [
        "-c", "user.name=ASES bootstrap", "-c", "user.email=ases-bootstrap@example.invalid",
        "commit", "-q", "-m", "ASES: bootstrap an empty repository (ASES-GIT-10)",
    ])
    if commit.returncode != 0:
        _bootstrap_event(conn, "repo_bootstrap_error", repo, integration_branch,
                          step="commit", detail=_clean(f"{commit.stdout}{commit.stderr}", 300))
        return False

    sha = _bootstrap_git(repo, ["rev-parse", "HEAD"]).stdout.strip()
    _bootstrap_event(conn, "repo_bootstrapped", repo, integration_branch, commit=sha)
    return True


def publish_plan(repo: pathlib.Path, integration_branch: str) -> str:
    """ASES-ARC-09 (v1.2): commit docs/ases/ to the integration branch and return that commit's SHA,
    BEFORE any implementation card is created. A worktree cut after this point sees the approved plan;
    one cut before it wouldn't. The repo's checked-out branch must already be integration_branch --
    Phase 3's `swarm plan` writes straight into the primary checkout, so this is a plain commit, not a
    merge from a separate planning worktree (a real multi-worktree planning flow is a later refinement)."""
    current = subprocess.run(
        [*gitexec.GIT, "-C", str(repo), "branch", "--show-current"], capture_output=True, text=True,
        env=gitexec.git_env(),
    ).stdout.strip()
    if current != integration_branch:
        raise RuntimeError(
            f"publish_plan expected {repo} to be on {integration_branch!r}, found {current!r}"
        )
    subprocess.run(
        [*gitexec.GIT, "-C", str(repo), "add", "docs/ases"], check=True, capture_output=True, env=gitexec.git_env(),
    )
    status = subprocess.run(
        [*gitexec.GIT, "-C", str(repo), "status", "--porcelain", "--", "docs/ases"],
        capture_output=True, text=True, env=gitexec.git_env(),
    ).stdout
    if not status.strip():
        # Nothing staged -- plan.json was already committed (e.g. a re-run of `swarm approve`).
        return subprocess.run(
            [*gitexec.GIT, "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True,
            env=gitexec.git_env(),
        ).stdout.strip()
    commit = subprocess.run(
        [*gitexec.GIT, "-C", str(repo), "commit", "-q", "-m", "ASES: publish approved plan (Gate P)"],
        capture_output=True, text=True, env=gitexec.git_env(),
    )
    if commit.returncode != 0:
        raise RuntimeError(f"could not commit the approved plan: {commit.stdout}{commit.stderr}")
    return subprocess.run(
        [*gitexec.GIT, "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True,
        env=gitexec.git_env(),
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
    it now leaves work_card_id alone for any task with fix_cards > 0.

    ASES-REC-03 and ASES-REC-04 (blueprint 19.4: "Card creation uses idempotency keys" and "Every multi-step action
    writes an intent record before acting and a completion record after: create cards ..."): the whole loop runs
    inside a create_cards intent keyed by the project, so a crash half way leaves an OPEN intent for
    reconcile-on-start, which then asks for this call to be repeated (it is idempotent by the keys above).

    ASES-CTL-01 (Table 17, "Attempts per card 3 (--max-retries 3)"): the work card is created with --max-retries
    set to budgets.attempts_per_card. Hermes gives up after 2 by default and the blueprint says 3, and a mismatch
    stalls a card silently (it blocks with a `gave_up` event one attempt earlier than ASES counts).

    A task that has had a fresh attempt (a `retry_card_created` event) keeps the work card plan_tasks names on a
    re-approve, and its original work card is not created again: that card was archived, and Hermes does not find an
    archived card by its idempotency key, so the create would have made a duplicate.

    Round 7 (ASES-REC-03, bug 1): the guard above only works while the plan_tasks ROW ITSELF still exists, to
    read fix_cards / _has_retry_card from. Deleting the ASES database (test 22.15's own scenario: create twice,
    delete the database, create a third time) deletes that row too, so both local signals go silent even though a
    fix or retry card may still be the task's real current card on the (unchanged) board. Falling into the `else`
    branch there used to assume "no fix/retry card exists" and call kanban_create with the ORIGINAL idempotency
    key, which is wrong two different ways: for a task with a FIX card (never archived), Hermes's idempotency
    lookup still finds the original and plan_tasks silently reverts to the stale, superseded card; for a task with
    a RETRY card (the original was archived by _start_fresh_attempt), the lookup finds nothing and a genuine
    duplicate work card gets created. See `_board_current_work_card`'s own docstring for the board-native signal
    this now asks instead, whenever the local row is missing or names no card."""
    pairs: dict[str, CardPair] = {}
    order = plan_mod.topological_order(plan)
    reviewer_profile = _reviewer_profile(project)
    attempts = project.budgets.get("attempts_per_card", 3)

    with intents_mod.intent(conn, plan.project, intents_mod.KIND_CREATE_CARDS, plan.project):
        for key in order:
            task = plan.task(key)
            assignee = policy.resolve_assignee(task.role, project.roles)
            parent_merge_ids = [pairs[dep].merge_card_id for dep in task.depends_on]

            existing = conn.execute(
                "SELECT work_card_id FROM plan_tasks WHERE project = ? AND task_key = ?", (plan.project, key),
            ).fetchone()
            merge = None
            fix_cards_seed = 0
            if existing is not None and existing["work_card_id"] and _has_retry_card(conn, plan.project, key):
                # A fresh attempt (_start_fresh_attempt) ARCHIVED the original work card and pointed plan_tasks at its
                # replacement. Hermes ignores an archived card when it looks a key up, so creating "the original"
                # again would make a duplicate work card, and the upsert below would forget the replacement. The
                # task's current card is the one plan_tasks names, so that is what a re-approve keeps.
                work = {"id": existing["work_card_id"]}
            elif existing is not None and existing["work_card_id"]:
                # Unchanged fast path: the row exists and names a card, and the SQL upsert below's own CASE WHEN
                # already protects a fix card's repoint once a plan_tasks row exists to read fix_cards from, so an
                # ordinary idempotent create is safe here too.
                work = hermes_mod.kanban_create(
                    board, f"{key}: {task.title}", assignee=assignee, workspace="worktree",
                    branch=f"swarm/{key}-{task.role}", project=project_id,
                    body=_work_card_body(task, reviewer_profile), parent=parent_merge_ids or None,
                    idempotency_key=f"ases-work-{plan.project}-{key}", max_retries=attempts,
                    max_runtime=f"{project.budgets.get('card_runtime_minutes', 45)}m",
                )
            else:
                # The local signal is silent (row missing, or present but naming no card): ask the board instead
                # of assuming "no fix/retry card exists" (round 7 bug 1, see the docstring above).
                current_id, merge, fix_cards_seed = _board_current_work_card(board, project_id, plan, key, conn=conn)
                if current_id is None:
                    # Genuinely never created before: the merge card _board_current_work_card just made (or found
                    # empty) has no parent yet, so the original work card is made and linked in explicitly, the
                    # same end state the parent=[work["id"]] argument below gives the common case.
                    work = hermes_mod.kanban_create(
                        board, f"{key}: {task.title}", assignee=assignee, workspace="worktree",
                        branch=f"swarm/{key}-{task.role}", project=project_id,
                        body=_work_card_body(task, reviewer_profile), parent=parent_merge_ids or None,
                        idempotency_key=f"ases-work-{plan.project}-{key}", max_retries=attempts,
                        max_runtime=f"{project.budgets.get('card_runtime_minutes', 45)}m",
                    )
                    hermes_mod.kanban_link(board, work["id"], merge["id"])
                else:
                    work = {"id": current_id}
            if merge is None:
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
            # taken while no fix card has been spent for this task (2026-09-19 fix). fix_cards_seed is 0 for
            # every path except the board-derived one above, which seeds a freshly INSERTed row with the fix
            # count the board lineage actually shows, instead of silently resetting that budget counter to
            # zero (round 7 bug 1); it has no effect on the ON CONFLICT branch, which never touches fix_cards.
            conn.execute(
                "INSERT INTO plan_tasks (project, task_key, work_card_id, merge_card_id, role, touches, "
                "gate_profile, estimated_requests, fix_cards, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "datetime('now')) "
                "ON CONFLICT(project, task_key) DO UPDATE SET "
                "work_card_id=CASE WHEN plan_tasks.fix_cards > 0 THEN plan_tasks.work_card_id "
                "ELSE excluded.work_card_id END, "
                "merge_card_id=excluded.merge_card_id",
                (plan.project, key, work["id"], merge["id"], task.role,
                 json.dumps(list(task.touches)), task.gate_profile, task.estimated_requests, fix_cards_seed),
            )
            events.record(conn, "cards_created", {"task_key": key, "work": work["id"], "merge": merge["id"]})

    return list(pairs.values())


def _board_current_work_card(
    board: str, project_id: str, plan: plan_mod.Plan, key: str, *, conn,
) -> tuple[str | None, dict, int]:
    """ASES-REC-03 (round 7 bug 1): the board-native fallback create_cards_from_plan uses once the local
    plan_tasks signals for a task have gone silent (the row is missing, typically because the ASES database
    itself was deleted, or present but names no card). Returns (current_work_card_id, merge_card,
    fix_cards_seen).

    Every fix card (_handle_merge_failure) and retry card (_start_fresh_attempt) a task has ever had is linked
    as an EXTRA parent of the task's MERGE card, alongside the original work card (create_cards_from_plan links
    it there when the merge card is first made). The merge card's `_parents` is therefore the task's complete
    card lineage, and the merge card itself is NEVER archived (only a work/fix/retry card is, in
    _start_fresh_attempt), so its own idempotency key (ases-merge-<project>-<key>) is always safe to
    fetch-or-create here with no risk of ever making a duplicate merge card.

    This does NOT use `_parents`' order, or the card ids, to find "the newest" member: real Hermes ids are
    `t_` + 4 random bytes (hermes_cli/kanban_db.py's `_new_task_id`, confirmed against the installed 0.21.3
    source), and `kanban_show`'s `_parents` comes back `ORDER BY parent_id` (hermes_cli/kanban_db.py's
    `_linked_ids`), alphabetic by id, not by creation time -- FakeHermes's own sequential `t_%08x` ids happen
    to sort the same as they were created, which would make this look right against the fake while being wrong
    against a real board. What every card dict DOES carry, on both, is its own `created_at`
    (kanban_show/TASK_FIELDS): a fresh fix or retry card is always created strictly after the card it replaces,
    and an archived card is always superseded by (so older than) whatever replaced it, so the lineage member
    with the latest `created_at` is always the live one. A non-archived member is preferred when there is one
    (the normal case); the whole lineage being archived should never happen given how _start_fresh_attempt only
    ever archives the single card it is replacing, but the newest member overall is used rather than returning
    nothing if it somehow did.

    current_work_card_id is None when the merge card itself is genuinely new (nothing has ever been linked to
    it): the caller creates the original work card itself, the same as it always has, and links it in.
    fix_cards_seen is how many lineage members are titled like a fix card (create_cards_from_plan's own
    "<key>: fix (round N)"), so a freshly INSERTed plan_tasks row can seed its fix_cards counter to match the
    board's real history instead of silently resetting that budget to zero."""
    merge = hermes_mod.kanban_create(
        board, f"{key}: merge", workspace="scratch", project=project_id,
        body=f"Merge candidate for {key}. Controller-owned; never assigned to an agent.",
        initial_status="blocked", idempotency_key=f"ases-merge-{plan.project}-{key}",
    )
    lineage_ids = hermes_mod.kanban_show(board, merge["id"]).get("_parents") or []
    if not lineage_ids:
        return None, merge, 0

    fix_title = re.compile(rf"^{re.escape(key)}: fix \(round \d+\)$")
    fix_cards_seen = 0
    best_id = best_at = None
    newest_id = newest_at = None
    for card_id in lineage_ids:
        card = hermes_mod.kanban_show(board, card_id)
        if fix_title.match(card.get("title") or ""):
            fix_cards_seen += 1
        at = _number(card.get("created_at"))
        if newest_at is None or at >= newest_at:
            newest_id, newest_at = card_id, at
        if card.get("status") == "archived":
            continue
        if best_at is None or at >= best_at:
            best_id, best_at = card_id, at
    current_id = best_id if best_id is not None else newest_id
    return current_id, merge, fix_cards_seen


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


def _completing_run(work_card: dict) -> dict | None:
    """The work card's latest COMPLETED run, or None when no run completed it. Hermes keeps one run per attempt
    with the profile that ran it, its outcome and its metadata (`_runs`, see hermes.kanban_show): a card the
    reviewer approved ends with a reviewer run whose outcome is "completed", while a card its implementer
    finished itself ends with the implementer's."""
    completed = [run for run in work_card.get("_runs", []) if run.get("outcome") == "completed"]
    return completed[-1] if completed else None


def _completing_profile(work_card: dict) -> str | None:
    """The Hermes profile that ran the work card's latest COMPLETED run, or None when no run completed it."""
    run = _completing_run(work_card)
    return run.get("profile") if run else None


_COMMIT_SHA = re.compile(r"[0-9a-fA-F]{7,40}")


def _handoff_commit(work_card: dict) -> str | None:
    """The commit SHA the coder named in its latest review hand-off (a run whose outcome is "review_requested";
    its metadata carries `commit_sha`, which the work-card body asks for), or None. The reviewer's own verdict
    often omits the commit (the Hermes review skill's shape has no such field), so this is what the approval is
    bound to when the verdict does not say (ASES-GIT-03)."""
    handoffs = [run for run in work_card.get("_runs", []) if run.get("outcome") == "review_requested"]
    if not handoffs:
        return None
    metadata = handoffs[-1].get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (ValueError, RecursionError):
            return None
    if not isinstance(metadata, dict):
        return None
    for field in ("commit_sha", "commit"):
        value = metadata.get(field)
        if isinstance(value, str) and _COMMIT_SHA.fullmatch(value.strip()):
            return value.strip()
    return None


def _refuse_once(conn, kind: str, work_card: dict, payload: dict) -> None:
    """Record a merge refusal ONCE per work card and kind. Once, not on every poll: a pass repeats every few
    seconds and would otherwise add an identical event each time for as long as the card sits there."""
    seen = conn.execute(
        "SELECT 1 FROM events WHERE kind = ? AND json_extract(payload, '$.card_id') = ? LIMIT 1",
        (kind, work_card["id"]),
    ).fetchone()
    if seen is None:
        events.record(conn, kind, {"card_id": work_card["id"], **payload})


def _refuse_unreviewed(
    conn, task_key: str, work_card: dict, completed_by: str | None, reviewer_profile: str,
) -> None:
    """Record, once per work card, that the merge queue refused it because the reviewer profile did not
    complete it."""
    _refuse_once(conn, "merge_refused_unreviewed", work_card, {
        "task_key": task_key, "completed_by": completed_by, "needs_completion_by": reviewer_profile,
    })


# ASES-QG-05 (round 7, part C): every role whose card produces a real commit that must go through review and
# merge exactly like any other diff, not a no-op verdict-only card. Round 7 found six places in this module that
# hardcoded `role == "coder"` (or its negation) to mean "the only role that commits", which is correct for
# "reviewer" but was silently wrong for "tester": ASES-QG-05 says "The Tester writes acceptance tests from
# docs/ases/contracts/ ... and the implementation card SHOULD depend on them", and a test file that never
# actually lands on the integration branch is not much of a contract. profiles.py and plan.py already treat
# "tester" as an ordinary role (RoleDef, known_roles); this is the one place that decides which roles commit,
# and there was no other such place to extend (checked config.py and plan.py first).
_COMMITTING_ROLES = frozenset({"coder", "tester"})


def _finish_instructions(role: str, reviewer_profile: str = "reviewer") -> list[str]:
    """How a worker hands its card off (ASES-REV-04: a review request carries handoff evidence). Appended to
    every work card body and every fix card body. A coder or a tester (any role in `_COMMITTING_ROLES`) commits
    and asks for review, and must NOT complete its own card: that would let the merge queue run before anyone
    independent had looked at the diff. A tester's instructions are deliberately identical to a coder's (the
    hand-off steps -- worktree, gate profile, commit, request review -- are the same regardless of what kind of
    file was changed); ASES-QG-05's own guidance about writing tests from docs/ases/contracts/ belongs in the
    acceptance criteria the Lead writes on the card, not repeated here. Any role not in `_COMMITTING_ROLES`
    (reviewer today) has no commit to hand off: it completes with its verdict, and the merge card for its task
    completes as a recorded no-op."""
    if role in _COMMITTING_ROLES:
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


def pin_gate_profiles(
    conn, project: str, gate_profiles: dict, sandbox_network_exceptions: dict | None = None,
) -> str:
    """ASES-QG-02: pin this project's approved gate profiles by content hash at `swarm approve` time,
    so `swarm run` can refuse to trust a diff that quietly changed gate configuration, CI scripts, or
    test-runner settings. Called only after publish_plan succeeds (see cmd_approve) -- a plan refused
    earlier by a data-policy or budget gate must never reach this and get pinned.

    Re-approving (a fresh `swarm approve`) intentionally moves the pin to whatever is approved now --
    that's the sanctioned way to change gate configuration, not a bypass of it.

    `sandbox_network_exceptions` (round 9, ASES-SEC-05, ASES-SEC-07: plan.sandbox_network_exceptions) is folded
    into the same hash, so a task's network exception is pinned exactly like a gate command and re-approval is
    required to change either. Left at the default None (today's call shape), the pin is unaffected."""
    digest = gates_mod.hash_gate_profiles(gate_profiles, sandbox_network_exceptions)
    conn.execute(
        "INSERT INTO gate_pins (project, gate_profiles_hash, pinned_at) VALUES (?, ?, datetime('now')) "
        "ON CONFLICT(project) DO UPDATE SET gate_profiles_hash=excluded.gate_profiles_hash, "
        "pinned_at=excluded.pinned_at",
        (project, digest),
    )
    return digest


def verify_gate_pin(
    conn, project: str, gate_profiles: dict, sandbox_network_exceptions: dict | None = None,
) -> None:
    """ASES-QG-02: refuse to proceed if the plan's gate profiles no longer match what was pinned at
    this project's last `swarm approve`. No pin row means this project has never been through the
    pinning path yet -- nothing to verify against, so this is a silent no-op rather than a false
    positive on a first-ever approve.

    `sandbox_network_exceptions`: see pin_gate_profiles. Passing the plan's current
    plan.sandbox_network_exceptions here is what makes a task's network flag flipped after approval, without a
    fresh `swarm approve`, raise GateConfigTamperedError exactly like an edited gate command would."""
    row = conn.execute(
        "SELECT gate_profiles_hash FROM gate_pins WHERE project = ?",
        (project,),
    ).fetchone()
    if row is None:
        return
    current = gates_mod.hash_gate_profiles(gate_profiles, sandbox_network_exceptions)
    if row["gate_profiles_hash"] != current:
        raise GateConfigTamperedError(
            f"gate configuration for project {project!r} no longer matches the pin recorded at the "
            f"last `swarm approve` (ASES-QG-02): gate commands, CI scripts, test-runner settings, or a "
            f"task's sandbox network exception changed without an explicit approved plan task allowing "
            f"it. Re-run `swarm approve` if this change is intentional."
        )


def process_budget_gate(
    board: str, plan: plan_mod.Plan, models_config: dict, *, conn, budgets: dict,
    project: ases_config.ProjectConfig | None = None,
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
    an in-flight one, before it actually happened, not after.

    With `project` given it also applies the review half of ASES-CAP-03 (2026-09-19): a coder card that
    finishes needs a review pass on the REVIEWER's provider, which can be a much tighter one than the
    coder's (a provider with no known cap for the coder, OpenRouter's 50 a day for the reviewer). When that
    provider cannot afford the review reserve, ready coder cards are parked instead of started, since work
    that cannot be reviewed today would only sit in review.

    The decision itself is _affordable_now, which process_unpark asks again once a card is parked: the same code
    on both sides means a card is never parked by one rule and unparked by another."""
    parked = []
    review_afford = usage_mod.review_budget(conn, models_config, project) if project is not None else None
    for card in hermes_mod.kanban_list(board, status="ready"):
        row = conn.execute(
            "SELECT task_key FROM plan_tasks WHERE work_card_id = ? AND project = ?",
            (card["id"], plan.project),
        ).fetchone()
        if row is None:
            continue
        task = plan.task(row["task_key"])
        ok, reason = _affordable_now(conn, task, models_config, budgets, review_afford, project)
        if not ok:
            hermes_mod.kanban_schedule(board, card["id"], reason)
            parked.append(task.key)
            events.record(conn, "card_parked_for_budget", {"task_key": task.key, "reason": reason})
    return parked


# The reasons process_budget_gate parks a card with, which is how process_unpark knows a scheduled card is one of
# ours: a card someone else scheduled (a person, a cron job) must never be unblocked by the controller. A
# "data class:" park (ASES-PRV-01/03, round 7 bug 2) is DELIBERATELY not one of these, and must never be added
# here: ASES-PRV-03 says the controller must never relax the project's data class to keep work flowing, so a
# data-class park must never auto-resume the way a budget park does the moment some other provider's usage
# frees up. process_unpark's prefix check below already excludes it correctly, just by _affordable_now using a
# different prefix for it; keep it that way.
_PARK_PREFIXES = ("budget:", "review budget")


def _affordable_now(
    conn, task: plan_mod.PlanTask, models_config: dict, budgets: dict, review_afford=None,
    project: ases_config.ProjectConfig | None = None,
) -> tuple[bool, str]:
    """ASES-CAP-03: (True, "") when this task's card may run now, else (False, the reason it is parked with).

    Three questions, in this order: when `project` is given, is the task's resolved provider still safe for the
    project's declared data_class (ASES-PRV-01, round 7 bug 2, checked first, mirroring Gate P's own order,
    "enforced before any other routing rule"); can the task's own provider afford its estimated requests (after
    the daily reserve); and, for a task whose role is in `_COMMITTING_ROLES` (a coder or a tester) when
    `review_afford` is given, can the REVIEWER's provider afford the review pass its finished work will need
    (usage.review_budget) -- round 7 part C: a tester's card needs review exactly like a coder's, and the old
    `task.role == "coder"` check here would have let a tester card start even when nobody could afford to
    review it. A role with no pinned provider has nothing to check and is affordable. The reasons start with
    `budget:`, `review budget` or `data class:`, the first two of which are what _PARK_PREFIXES matches (see the
    comment above it for why the third is not).

    Round 7 (ASES-PRV-01, bug 2): cli.cmd_approve's Gate P check (_estimate_lines) only ever runs once, at
    plan-approval time, so a provider that becomes (or, on a mis-approved plan, always was) unsafe for the
    project's data_class was never re-checked once a card was actually about to run -- confirmed empirically by
    round 6's ASES-PRV-01 finding: process_budget_gate called only policy.check_budget, never check_data_class.
    When `project` is given this now asks policy.check_data_class the same way cmd_approve does, building
    provider_policies/provider_verified_at from models_config["providers"] inline (the same
    {name: p.get("data_policy")} / {name: p.get("data_policy_verified_at")} cmd_approve's own _estimate_lines
    builds; models_config is already a parameter here, so no new one is threaded through just for this). A
    DataPolicyViolation never raises past this function: it parks, exactly like an ordinary budget shortfall
    does, through the same (bool, reason) shape the caller already handles.

    Shared by process_budget_gate (park a ready card) and process_unpark (release a parked one): one rule, so the
    two can never disagree about the same card on the same ledger. `project` is the same optional parameter
    process_budget_gate already threads through for the review-budget half; process_unpark now passes it too, so
    a data-class park is recognised by neither as anything it should ever resume."""
    pp = policy.profile_provider(task.role, models_config)
    if pp is None:
        return True, ""
    if project is not None:
        providers = models_config.get("providers", {})
        provider_data_policy = (providers.get(pp.provider) or {}).get("data_policy")
        provider_verified_at = (providers.get(pp.provider) or {}).get("data_policy_verified_at")
        try:
            policy.check_data_class(
                project.data_class, pp.provider, provider_data_policy, verified_at=provider_verified_at,
            )
        except policy.DataPolicyViolation as exc:
            return False, f"data class: {exc}"
    afford = policy.check_budget(
        conn, models_config["providers"], pp.provider, task.estimated_requests, budgets=budgets,
    )
    if not afford.can_afford:
        return False, f"budget: {afford.reason}"
    if review_afford is not None and not review_afford.can_afford and task.role in _COMMITTING_ROLES:
        reviewer_pp = policy.profile_provider("reviewer", models_config)
        provider = reviewer_pp.provider if reviewer_pp else "the reviewer provider"
        return False, f"review budget on {provider}: {review_afford.reason}"
    return True, ""


def process_review_lane(
    board: str, repo: pathlib.Path, plan: plan_mod.Plan, project: ases_config.ProjectConfig | None = None,
    *, conn,
) -> list[str]:
    """One pass: for every work card currently in 'review', re-run Gate 1 (ASES-REV-05). Returns the
    task keys that were sent back this pass.

    Scoped to `plan.project`, same reasoning as `process_budget_gate` above -- a "review" card from a
    different project sharing this board must not be matched against this run's plan by task key alone.

    A fix card counts as its task's work card here once `process_merge_queue` has repointed
    plan_tasks.work_card_id at it, so its diff is policed like any other card's.

    `project` (round 9, ASES-QG-04, ASES-SEC-03; optional and None by default, so an older caller keeps working
    unchanged with the host runner) is handed to gates.resolve_runner through review.gate_before_review, along
    with the task itself for its own network exception (ASES-SEC-05, ASES-SEC-07). A gate call that cannot even
    start (sandbox.SandboxInfrastructureError: Docker down, the pinned image missing) is not a red gate: it is
    recorded once per card as a `sandbox_infrastructure_error` event and the card is left exactly where it is,
    to be re-checked next pass, never sent back and never silently run on the host instead."""
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
        try:
            ok = review_mod.gate_before_review(
                board, card["id"], repo, branch, plan.integration_branch, gate_cmds, list(task.touches),
                conn=conn, task_key=task_key, allow_gate_config_changes=task.allow_gate_config_changes,
                project_config=project, task=task,
            )
        except sandbox_mod.SandboxInfrastructureError as exc:
            _record_once(conn, "sandbox_infrastructure_error", {
                "task_key": task_key, "card_id": card["id"], "gate": "gate1", "error": _clean(str(exc), 300),
            }, match=("task_key", "gate"))
            continue
        if not ok:
            sent_back.append(task_key)
            events.record(conn, "gate1_recheck_failed", {"task_key": task_key})
    return sent_back


def _handle_merge_failure(
    board: str, plan: plan_mod.Plan, project: ases_config.ProjectConfig, *, conn, key: str, row, work_card: dict,
    task: plan_mod.PlanTask, reviewer_profile: str, models_config: dict | None, detail: str, fix_limit: int,
    attempts: int,
) -> None:
    """The failure path a merge takes once its detail text is known (round 6: factored out so an ordinary Gate 3
    or fast-forward failure and a post-merge revert, ASES-GIT-05, share one implementation instead of two). Past
    `fix_limit` the merge card is blocked for the user (through questions.ask_user, never hermes.kanban_block --
    every merge card is created blocked, and real Hermes refuses to block a card that already is); otherwise a
    fix card is opened for the same role in a fresh worktree, linked as an EXTRA parent of the merge card, and
    the task's CURRENT work card is repointed at it. The merge card itself is never completed here in either
    case: it stays open, for the fix or for the human."""
    if row["fix_cards"] >= fix_limit:
        asked = _ask_about_merge_card(
            board, row["merge_card_id"],
            f"Fix-card budget ({fix_limit}) exhausted for {key}: the merge keeps failing and ASES will not open "
            f"another fix card. Last failure: {detail[:500]}\nHow should this be resolved?",
            conn=conn,
        )
        events.record(conn, "fix_card_budget_exhausted", {"task_key": key, "asked": asked})
        return

    assignee = policy.resolve_assignee(task.role, project.roles)
    fix_branch = f"swarm/{key}-fix{row['fix_cards'] + 1}"
    # project= is the real Hermes project id, read off the card being fixed (`work_card`), never `project.name`
    # (2026-09-19 fix). parent= is likewise the card being replaced, so this must run BEFORE work_card_id is
    # repointed below.
    fix_card = hermes_mod.kanban_create(
        board, f"{key}: fix (round {row['fix_cards'] + 1})", assignee=assignee,
        workspace="worktree", branch=fix_branch, project=work_card.get("project_id"),
        body=(f"Merge attempt for {key} failed. Fix in a fresh worktree.\n\n"
              f"Failure detail:\n{detail[:1500]}\n\n"
              + "\n".join([*_scope_lines(task), "", *_finish_instructions(task.role, reviewer_profile)])),
        parent=[row["work_card_id"]],
        idempotency_key=f"ases-fix-{plan.project}-{key}-{row['fix_cards'] + 1}",
        max_retries=attempts, max_runtime=f"{project.budgets.get('card_runtime_minutes', 45)}m",
    )
    hermes_mod.kanban_link(board, fix_card["id"], row["merge_card_id"])
    # Count the outgoing card's finished runs NOW (see _ingest_outgoing_usage), before it is repointed.
    _ingest_outgoing_usage(board, row["work_card_id"], project, models_config, plan, key, conn)
    # Spend the fix budget and repoint work_card_id in ONE statement: the next pass then waits for
    # the fix card to reach done, merges the fix card's branch, and the review lane can find it.
    conn.execute(
        "UPDATE plan_tasks SET fix_cards = fix_cards + 1, work_card_id = ? "
        "WHERE project = ? AND task_key = ?",
        (fix_card["id"], plan.project, key),
    )
    events.record(conn, "fix_card_created", {"task_key": key, "fix_card_id": fix_card["id"]})


def process_merge_queue(
    board: str, repo: pathlib.Path, plan: plan_mod.Plan, project: ases_config.ProjectConfig, *, conn,
    unreviewed: list[str] | None = None, models_config: dict | None = None, integrity: list[str] | None = None,
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

    A review-only task (any role not in `_COMMITTING_ROLES`, reviewer today) has no commit to merge: its branch
    adds nothing to the integration tip. Those are merged with allow_empty, which makes mergeq.merge_task record
    a no-op instead of failing (merged=True, squash_commit None). The merge card is completed with result "no
    changes to merge (review-only task)" and metadata no_op=True, the "merged" event carries no_op=True, and it
    is never a failure: no fix card, no fix budget. A coder's or a tester's empty branch (any role IN
    `_COMMITTING_ROLES`, round 7 part C) is still the failure it always was. Every squash commit carries the
    work card and merge card ids in its message (ASES-GIT-06).

    Loop version 2 (round 5) adds these rules:
      * ASES-REC-06 ("stop the merge queue between steps"): the halt flag (_halted: a stopped or paused project) is
        read before each task, and merge_task gets `should_stop` and the project, so it polls the same flag before
        it builds the candidate, before Gate 3 and before the fast-forward. A merge that comes back `stopped` is not
        a failure: it is recorded as merge_stopped, the queue ends for this pass, and no fix card and no budget is
        spent.
      * ASES-REC-05: a task whose MERGE card has an open question is skipped. The human has not answered, so the
        queue neither re-runs the merge nor asks again (before this, a merge card blocked for an exhausted fix
        budget was re-processed on every pass, and real Hermes refuses to block a blocked card, which raised).
        Every question the controller puts to a person goes through questions.ask_user, never hermes.kanban_block
        on a merge card, because that call fails on a card that is already blocked and every merge card is.
      * ASES-SEC-01: the failure detail is redacted before it goes into a fix card body, an event or a question.
      * ASES-CTL-01 (Table 17): a fix card is created with --max-retries set to budgets.attempts_per_card.
      * ASES-REC-03/04: completing a merge card runs inside a complete_merge_card intent, and the expected primary
        HEAD is recorded BEFORE that (the fast-forward has already moved HEAD, so a Hermes error while completing the
        card must not leave the integrity guard expecting the old one and calling the controller's own merge a
        violation).
      * ASES-QG-03: a pre-merge check of kind `tamper_check_error` (git could not answer) is never a failure: it is
        recorded and the task is retried on the next pass. Kind `tamper` takes the ordinary failure path.

    Round 6 adds the post-merge check (ASES-GIT-05, section 8.1: "The integration branch MUST stay runnable. If a
    post-merge check fails, the queue reverts the squash commit, records it, blocks the merge card and opens a fix
    card."). Gate 3 above is a PRE-merge check: it can be green a moment before another task's merge lands on a
    shared file this task's own touches never named, making the candidate's promise stale by the time it actually
    reaches the tip. So immediately after a real (non-no-op) merge, for a coder task, Gate 3's own commands run
    ONE more time on the new integration HEAD, in a throwaway worktree, before the merge card is completed:

      * green (the common case): unchanged from before this round -- the merge card completes, `merged` is
        recorded, done.
      * red, and the revert succeeds: a `post_merge_reverted` event, then the SAME failure path an ordinary Gate 3
        or fast-forward failure takes (`_handle_merge_failure`: a `merge_failed` event, a fix card bounded by
        `fix_cards_per_task`, or the budget escalation past it). The merge card is NOT completed: it stays open
        for the fix. The branch is runnable again (the bad commit is gone), so the rest of the queue is not halted.
      * red, and the revert ITSELF fails (git refuses and `git revert --abort` cannot recover a clean checkout
        either): this is not safe to paper over with a fix card while the integration branch is in an unknown
        state, so it halts the run the way the primary-checkout guard does (ASES-GIT-12): an `integrity_violation`
        event, and, when the caller passes a list for `integrity` (run_pass does), the problem is appended to it
        and the queue stops for this pass. Callers that do not pass `integrity` see this exactly as an ordinary
        halt of the loop below (no further tasks processed this pass), which is why every existing caller of this
        function keeps working unchanged: `integrity` is new and optional, the same shape as `unreviewed`."""
    merged = []
    fix_limit = project.budgets.get("fix_cards_per_task", 2)
    attempts = project.budgets.get("attempts_per_card", 3)
    reviewer_profile = _reviewer_profile(project)

    def should_stop() -> bool:
        return _halted(conn, plan.project)[0]

    for key in plan_mod.topological_order(plan):
        halted, why = _halted(conn, plan.project)
        if halted:
            events.record(conn, "merge_queue_halted", {"project": plan.project, "reason": _clean(why, 200)})
            break
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
        if _open_question(merge_card) is not None:
            continue  # ASES-REC-05: asked and not answered yet, so neither merge again nor ask again

        completed_run = _completing_run(work_card)
        completed_by = completed_run.get("profile") if completed_run else None
        if completed_by != reviewer_profile:
            _refuse_unreviewed(conn, key, work_card, completed_by, reviewer_profile)
            if unreviewed is not None:
                unreviewed.append(key)
            continue

        task = plan.task(key)
        gate_cmds = plan.gate_profiles.get(task.gate_profile, [])
        branch = work_card.get("branch_name") or f"swarm/{key}-{task.role}"

        # Merge-time checks (2026-09-19), independent of whether the review lane ever saw this card: Hermes's own
        # gateway dispatcher can start the reviewer before the controller's Gate 1 re-check, so the merge queue,
        # the only writer to the integration branch (ASES-GIT-02), verifies for itself. Only a coder task has a
        # diff, a Gate 1 record and a schema-checked verdict; a reviewer-role task's card has no commit to check.
        pre_merge_outcome = None
        expected_head = None
        if task.role in _COMMITTING_ROLES:
            # ASES-REV-06: the verdict is validated against the schema, and it must be a PASS.
            verdict = review_mod.validate_verdict(completed_run.get("metadata"))
            if not verdict.valid:
                _refuse_once(conn, "merge_refused_invalid_verdict", work_card, {
                    "task_key": key, "outcome": verdict.outcome, "problems": list(verdict.problems)[:10],
                })
                if unreviewed is not None:
                    unreviewed.append(key)
                continue
            if verdict.outcome != "PASS":
                # Found by the FK builder: a reviewer that calls kanban_complete with a CHANGES_REQUIRED or
                # BLOCKED verdict in its metadata, instead of using kanban_request_changes/kanban_block, used to
                # fall into the branch above and be refused as an "invalid" verdict forever -- the card was
                # `done`, refused every poll, and never went back to its implementer. The verdict is well formed
                # here (validate_verdict already ruled out valid=False above): the reviewer answered, just not
                # with a pass, so this is not the unreviewed-refusal path at all. Treat it as if the reviewer had
                # called kanban_reopen_review instead of kanban_complete: back to the implementer, with the
                # reviewer's own words as the reason when it gave any (a run's "summary", not part of the
                # validated Verdict, which carries no free-text summary field of its own).
                reason = _clean(
                    completed_run.get("summary") or f"reviewer completed the card with a {verdict.outcome} verdict",
                    300,
                )
                hermes_mod.kanban_reopen_review(board, work_card["id"], reason=reason)
                events.record(conn, "reviewer_completed_with_changes_requested", {
                    "task_key": key, "card_id": work_card["id"], "outcome": verdict.outcome,
                })
                continue
            # ASES-GIT-03, ASES-GIT-13, ASES-REV-05, ASES-QG-01: scope, then the controller's OWN green Gate 1
            # record for this exact head. A red or stale result takes the ordinary failure path (fix card).
            try:
                check = review_mod.check_branch_for_merge(
                    repo, branch, plan.integration_branch, gate_cmds, list(task.touches), conn=conn, task_key=key,
                    require_binding=True, reviewed_commit=verdict.commit or _handoff_commit(work_card),
                    allow_gate_config_changes=task.allow_gate_config_changes,
                    project_config=project, task=task,
                )
            except sandbox_mod.SandboxInfrastructureError as exc:
                # Round 9 (ASES-QG-04, ASES-SEC-03): the sandbox is enabled but this task's Gate 1 re-check could
                # not even start (Docker down, the pinned image missing). Not a red gate and not a scope
                # violation: recorded once per card and retried next pass, exactly like a tamper_check_error.
                _record_once(conn, "sandbox_infrastructure_error", {
                    "task_key": key, "card_id": work_card["id"], "gate": "gate1", "error": _clean(str(exc), 300),
                }, match=("task_key", "gate"))
                continue
            if not check.ok and check.kind == "tamper_check_error":
                # git could not answer the tamper question, so nothing is known about the diff: not a failure of the
                # branch (no fix card, no fix budget), and not a pass either. The task is retried next pass. Recorded
                # once per card and message: a pass repeats every few seconds and a broken git would say the same
                # thing each time.
                _record_once(conn, "tamper_check_error", {
                    "task_key": key, "card_id": work_card["id"], "detail": _clean(check.detail, 300),
                }, match=("card_id", "detail"))
                continue
            if not check.ok:
                pre_merge_outcome = mergeq.MergeOutcome(
                    False, check.head or None, None, None, f"{check.kind}: {check.detail}",
                )
            elif not review_mod.verdict_matches_head(verdict, check.head):
                _refuse_once(conn, "merge_refused_verdict_commit_mismatch", work_card, {
                    "task_key": key, "reviewed_commit": verdict.commit, "branch_head": check.head,
                })
                if unreviewed is not None:
                    unreviewed.append(key)
                continue
            else:
                expected_head = check.head
                review_mod.record_verdict(
                    conn, plan.project, key, check.head, work_card["id"], verdict.outcome, reviewer_profile,
                    completed_run.get("metadata"),
                )
        # ASES-GIT-06: one squash commit per plan task, with the card IDs in its message. work_card is the
        # task's CURRENT card (the original, or the latest fix card), so a merge of a fix card's branch names
        # the fix card that produced it.
        commit_message = (
            f"{key}: {task.title}\n\nWork card: {work_card['id']}\nMerge card: {row['merge_card_id']}\n"
            f"Branch: {branch}\nControlled by ASES (one squash commit per plan task)."
        )
        # Only a role in _COMMITTING_ROLES (coder, tester) is expected to commit. Any other role (a reviewer)
        # legitimately leaves an empty diff, which merge_task then records as a no-op rather than failing on
        # "nothing to commit".
        try:
            outcome = pre_merge_outcome or mergeq.merge_task(
                repo, plan.integration_branch, branch, key, gate_cmds, conn=conn,
                commit_message=commit_message, allow_empty=(task.role not in _COMMITTING_ROLES),
                expected_head=expected_head, project=plan.project, should_stop=should_stop,
                project_config=project, task=task,
            )
        except sandbox_mod.SandboxInfrastructureError as exc:
            # Round 9 (ASES-QG-04, ASES-SEC-03): the sandbox is enabled but this task's Gate 3 candidate could
            # not even start (Docker down, the pinned image missing). Not a red gate: nothing was built and
            # nothing was merged, so there is nothing to revert either. Recorded once per card and retried next
            # pass, the same shape as the Gate 1 case above -- never fall back to the host silently.
            _record_once(conn, "sandbox_infrastructure_error", {
                "task_key": key, "card_id": row["merge_card_id"], "gate": "gate3", "error": _clean(str(exc), 300),
            }, match=("task_key", "gate"))
            continue

        if getattr(outcome, "stopped", False):
            # ASES-REC-06: the kill switch (or a reached bound) stopped the merge at one of its checkpoints. Nothing
            # was merged and nothing is wrong with the branch, so it is not a failure: no merge_failed event, no fix
            # card, no budget. The rest of the queue waits too, since the same flag stops every later task.
            events.record(conn, "merge_stopped", {"task_key": key, "detail": _clean(outcome.detail, 300)})
            break

        if outcome.merged and outcome.squash_commit is None:
            # The recorded no-op (an empty diff on a review-only task): nothing was committed or gated and
            # nothing is wrong, so it is never a failure. No fix card, no fix budget. The merge card still
            # completes, which is what releases the tasks that depend on it (ASES-TSK-02).
            with intents_mod.intent(conn, plan.project, intents_mod.KIND_COMPLETE_MERGE_CARD, key):
                hermes_mod.kanban_complete(
                    board, row["merge_card_id"],
                    result="no changes to merge (review-only task)",
                    metadata={"squash_commit": None, "no_op": True},
                )
            merged.append(key)
            events.record(conn, "merged", {"task_key": key, "sha": None, "no_op": True})
            continue
        if outcome.merged:
            # ASES-GIT-05 (section 8.1): re-check Gate 3 on the NEW integration HEAD before trusting the merge.
            # Only a task whose role is in _COMMITTING_ROLES has anything to re-check (a no-op merge,
            # squash_commit None, was already handled above and never reaches here). Deliberately redundant
            # with the pre-merge Gate 3 above: this catches a project-level regression another task's merge
            # introduced on the shared tip between this candidate's build and this fast-forward, on a file this
            # task's own touches never named.
            #
            # No task-scoped network exception here (round 9, ASES-SEC-05, ASES-SEC-07): resolve_runner is
            # called with no task, so this re-check always runs with --network none, even for a task whose Gate
            # 1 and Gate 3 candidate carried an exception. Only that task's OWN candidate build gets network.
            postcheck = None
            if task.role in _COMMITTING_ROLES:
                # The sandbox kwargs are added ONLY when the sandbox is actually enabled (round 9), exactly like
                # mergeq.merge_task's own Gate 3 call: a `run_gate` test stand-in with a fixed signature keeps
                # working unchanged for every test that does not turn the sandbox on.
                choice = gates_mod.resolve_runner(project)
                sandbox_kwargs = (
                    {"runner": choice.runner, "self_contained_checkout": True} if choice.self_contained else {}
                )
                try:
                    postcheck = gates_mod.run_gate(
                        repo, outcome.squash_commit, "gate3-postmerge", gate_cmds, conn=conn, task_key=key,
                        project=plan.project, **sandbox_kwargs,
                    )
                except sandbox_mod.SandboxInfrastructureError as exc:
                    # The fast-forward already landed on the integration branch: an infra failure here is NOT a
                    # red gate, so it must not trigger a revert (the reasoning below), and it must not leave the
                    # merge card open either -- a repeat pass would re-run merge_task on a work branch whose
                    # content the integration tip already has, which mergeq.merge_task then correctly refuses as
                    # "nothing to commit" (allow_empty is False for a committing role), opening a spurious fix
                    # card for a task that actually succeeded. So the merge is accepted as it stands, the
                    # re-verification is recorded as skipped this pass (never silently treated as green), and the
                    # completion path below runs exactly as it would with no postcheck at all.
                    events.record(conn, "sandbox_infrastructure_error", {
                        "task_key": key, "commit": outcome.squash_commit, "gate": "gate3-postmerge",
                        "error": _clean(str(exc), 300),
                    })

            if postcheck is not None and not postcheck.passed:
                post_detail = _clean(postcheck.detail, 500)
                events.record(conn, "post_merge_reverted", {
                    "task_key": key, "commit": outcome.squash_commit, "detail": post_detail,
                })
                revert = mergeq.revert_merge(repo, outcome.squash_commit, conn=conn, task_key=key, project=plan.project)
                if not revert.ok:
                    # The integration branch is now in an unknown state (still broken, or mid-conflict if even
                    # git revert --abort could not recover it): not safe to keep merging on top of, the same
                    # reasoning run_pass's primary-checkout guard already acts on (ASES-GIT-12).
                    problem = (
                        f"post-merge Gate 3 failed for {key} on {outcome.squash_commit[:12]} and the automatic "
                        f"revert could not repair the integration branch (aborted={revert.aborted}): "
                        f"{_clean(revert.detail, 300)}"
                    )
                    events.record(conn, "integrity_violation", {
                        "problems": [problem], "head": outcome.squash_commit, "branch": plan.integration_branch,
                    })
                    if integrity is not None:
                        integrity.append(problem)
                    break
                # The revert landed a new commit on the primary checkout itself: that, not the reverted merge, is
                # the HEAD the integrity guard must now expect (the same reasoning as the green case below).
                guards_mod.set_expected_head(conn, plan.project, revert.commit_sha)
                events.record(conn, "merge_failed", {"task_key": key, "detail": post_detail})
                _handle_merge_failure(
                    board, plan, project, conn=conn, key=key, row=row, work_card=work_card, task=task,
                    reviewer_profile=reviewer_profile, models_config=models_config, detail=post_detail,
                    fix_limit=fix_limit, attempts=attempts,
                )
                continue

            # The merge queue moved the primary checkout's HEAD itself, so that is the HEAD the integrity
            # guard must now expect (a move it did not make is the violation, ASES-GIT-12). Recorded BEFORE the
            # card is completed: the fast-forward has already happened, and a Hermes error below must not leave
            # the guard expecting the old HEAD and reporting the controller's own merge as an intruder.
            guards_mod.set_expected_head(conn, plan.project, outcome.squash_commit)
            with intents_mod.intent(conn, plan.project, intents_mod.KIND_COMPLETE_MERGE_CARD, key):
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

        # Command output can carry a secret (a gate prints its environment, git echoes a remote URL), and this text goes
        # into an event, a question and a card body: redacted once here, BEFORE it is cut, so a secret is never left
        # half-visible at a truncation point (ASES-SEC-01).
        detail = events.redact_text(outcome.detail or "")
        events.record(conn, "merge_failed", {"task_key": key, "detail": detail[:500]})
        _handle_merge_failure(
            board, plan, project, conn=conn, key=key, row=row, work_card=work_card, task=task,
            reviewer_profile=reviewer_profile, models_config=models_config, detail=detail,
            fix_limit=fix_limit, attempts=attempts,
        )
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


# ---------------------------------------------------------------------------------------------
# Loop version 2 (round 5). Everything below is a step of run_pass, or a small helper of one.
# ---------------------------------------------------------------------------------------------

# Hermes writes a `blocked` event with this reason when a card is CREATED blocked (kanban_db.create_task, read from the
# 0.21.3 source on 2026-09-21), which is how every merge card starts. It records where the card began, and nobody asked
# anything, so it must never read as an open question.
_CREATED_BLOCKED = "initial_status"

# Every status in which a card still holds its worktree and its leased resources. A BLOCKED card is in the list on
# purpose: it waits for an answer and then resumes in the same worktree with the same ports (leases.sweep_finished).
_LEASE_LIVE_STATUSES = ("running", "ready", "review", "scheduled", "todo", "blocked")

# The recovery actions the controller has to carry out itself (recovery.process_failures only decides them).
_CONTROLLER_ACTIONS = (
    recovery_mod.ACTION_FRESH_ATTEMPT, recovery_mod.ACTION_SWITCH_MODEL, recovery_mod.ACTION_REPLAN,
)

_RETRY_CREATE_TRIES = 3        # how many retry numbers _start_fresh_attempt tries before it gives up on a collision
_DIFF_READ_LIMIT = 200_000     # characters of a failed branch's diff read from git (the bundle keeps far fewer)


def _clean(text, limit: int = 300) -> str:
    """One line of agent, command or exception text made safe to put in an event, a question or a terminal: any
    secret-shaped value redacted first (ASES-SEC-01), whitespace collapsed, anything that is not ASCII escaped (the
    Windows console is cp1252 and crashes on an arrow), then cut to `limit` characters."""
    flat = " ".join(events.redact_text(str(text if text is not None else "")).split())
    return flat.encode("ascii", "backslashreplace").decode("ascii")[:limit]


def _event_payload(event) -> dict:
    """A payload as a dict, from a board event (Hermes hands it over as a dict or as a JSON string) or from the raw
    JSON text of a row of our own events table."""
    payload = event.get("payload") if isinstance(event, dict) else event
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, RecursionError):
            return {}
    return payload if isinstance(payload, dict) else {}


def _number(value) -> float:
    """`value` as a number for ordering (Hermes timestamps are ints, in places numeric strings), 0 when it is not."""
    if isinstance(value, bool):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _record_once(conn, kind: str, payload: dict, *, match: tuple[str, ...]) -> bool:
    """Record the event unless one of the same kind already carries the same values in the payload fields named by
    `match`; True when it was recorded. A pass repeats every few seconds, and a step that fails the same way each time
    would otherwise add an identical event per pass for as long as the fault lasts (the reasoning of _refuse_once).
    The field names are literals of this module (they are spliced into the SQL), and the values are compared after the
    redaction the event itself goes through, so a payload with a secret-shaped value finds its own earlier copy."""
    safe = events.redact(payload)
    clauses = "".join(f" AND json_extract(payload, '$.{name}') IS ?" for name in match)
    seen = conn.execute(
        f"SELECT 1 FROM events WHERE kind = ?{clauses} LIMIT 1", (kind, *(safe.get(name) for name in match)),
    ).fetchone()
    if seen is not None:
        return False
    events.record(conn, kind, payload)
    return True


@contextlib.contextmanager
def _atomic(conn):
    """One SAVEPOINT around a group of writes, so they land together or not at all. Unlike BEGIN a savepoint nests,
    so this is safe whether or not the caller already holds a transaction (the same helper recovery.py and usage.py
    have; the ASES connection is in autocommit mode)."""
    conn.execute("SAVEPOINT controller_atomic")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK TO controller_atomic")
        conn.execute("RELEASE controller_atomic")
        raise
    conn.execute("RELEASE controller_atomic")


def _as_datetime(now):
    """`now` as the datetime bounds.evaluate_bounds takes (naive means UTC): a datetime as it is, epoch seconds or an
    ISO 8601 string converted, None as None. The clock parameter of run_pass is handed on unchanged to the modules that
    accept several types (recovery), and converted only here, for the one that is strict."""
    if now is None or isinstance(now, datetime):
        return now
    if isinstance(now, bool):
        raise ValueError(f"now must be a datetime, epoch seconds or an ISO 8601 string, got {now!r}")
    if isinstance(now, (int, float)):
        return datetime.fromtimestamp(now, timezone.utc)
    if isinstance(now, str):
        return datetime.fromisoformat(now.strip().replace("Z", "+00:00"))
    raise ValueError(f"now must be a datetime, epoch seconds or an ISO 8601 string, got {now!r}")


def _pause_reason(conn, project_name: str) -> str | None:
    """Why a paused project is paused: the reason of the newest `project_paused` event (pause_and_report records it).
    bounds.set_status keeps a reason only for a `stopped` project, so this is where a paused one's is kept."""
    row = conn.execute(
        "SELECT payload FROM events WHERE kind = 'project_paused' AND json_extract(payload, '$.project') = ? "
        "ORDER BY id DESC LIMIT 1", (project_name,),
    ).fetchone()
    reason = _event_payload(row["payload"]).get("reason") if row else None
    return reason if isinstance(reason, str) and reason else None


def _halted(conn, project_name: str) -> tuple[bool, str | None]:
    """ASES-REC-06 and ASES-CTL-01: (True, why) when the project is `stopped` (the kill switch, or a reached bound
    that stops a project) or `paused` (pause_and_report), else (False, None). The one halt test of the loop: run_pass
    asks it before anything else, the merge queue before each task, and merge_task polls it (should_stop) at its
    checkpoints, so a stop request ends the work at the next step boundary. A project with no state row is not
    halted."""
    state = bounds_mod.get_state(conn, project_name)
    if state is None or state["status"] not in ("stopped", "paused"):
        return False, None
    return True, state["stop_reason"] or _pause_reason(conn, project_name) or f"the project is {state['status']}"


def _open_question(card: dict):
    """questions.open_question(card), except that a merge card's CREATION is not a question. Hermes records the
    `initial_status` block of a card created blocked as an ordinary `blocked` event with that word as its reason,
    and every merge card is created that way; read as a question it would make the merge queue skip every task for
    ever. (The r2 notes say Hermes writes no event for it, the 0.21.3 source says it does, so both are handled.)"""
    question = questions_mod.open_question(card)
    if (question is not None and getattr(question, "source", None) == "blocked"
            and str(getattr(question, "reason", "")).strip() == _CREATED_BLOCKED):
        return None
    return question


def _ask_about_merge_card(board: str, merge_card_id: str, text: str, *, conn) -> str:
    """Put a question to the person on a MERGE card, through questions.ask_user with the card as it is NOW (the merge
    just ran for minutes, and the card may have moved). Never hermes.kanban_block: a merge card is created blocked, and
    Hermes refuses to block a blocked card, after leaving its comment, with exit 1. Returns ask_user's answer."""
    return questions_mod.ask_user(board, hermes_mod.kanban_show(board, merge_card_id), text, conn=conn)


def _ingest_outgoing_usage(board, card_id, project, models_config, plan, task_key, conn) -> None:
    """Count the finished runs of a card that is about to be replaced NOW: once plan_tasks.work_card_id points at
    the replacement, the per-pass ingest never looks at this card again, and a reviewer run that ended since the last
    pass would go uncounted (found by the usage builder). Only with a models_config, and never raises: a stale
    ledger must not stop the replacement card being tracked."""
    if models_config is None:
        return
    try:
        usage_mod.ingest_card_usage(
            board, card_id, project, models_config, conn=conn, plan_project=plan.project, task_key=task_key,
        )
    except Exception as exc:  # noqa: BLE001 - see above
        events.record(conn, "usage_ingest_error", {"error": f"{type(exc).__name__}: {exc}"[:300]})


# --- recovery: failed runs, fresh attempts, re-plans and spent lineage budgets -----------------------------------


def _recovery_row(task_key, action, kind) -> dict:
    """One entry of process_recovery's answer (and of the summary's `recovery` list)."""
    return {"task_key": task_key, "action": action, "kind": str(kind) if kind else ""}


def process_recovery(
    board: str, repo: pathlib.Path, plan: plan_mod.Plan, project: ases_config.ProjectConfig, models_config: dict,
    *, conn, now=None,
) -> list[dict]:
    """ASES-REC-01 and ASES-REC-02 (blueprint 19.1 to 19.3; the loop's `recovery.classify_and_act(failed_runs)`).
    Returns [{"task_key", "action", "kind"}, ...] for what was decided or done this pass.

      a. recovery.refresh_review_rounds counts the review rounds the board shows, per plan task (idempotent).
      b. recovery.process_failures decides, and applies what it can (resume, park, ask the user), for every task
         whose current work card Hermes gave up on. It only RETURNS fresh_attempt, switch_model and replan.
      c. This function carries those three out. fresh_attempt and switch_model start a replacement card
         (_start_fresh_attempt: "start the next attempt from a fresh worktree at the current integration HEAD, attach
         the failure bundle", and "on the second capability failure switch to the next model", blueprint 19.2);
         replan asks the user (_request_replan).
      d. A decision that recovery made and this function did not finish is carried out again (_pending_decisions).
      e. A spent review-round budget, which no failed run announces, is escalated (_escalate_spent_budgets).

    Step d exists because recovery writes the `recovery_decision` event, and bumps the lineage counter, BEFORE the
    controller acts, and never returns a decision for a run that already has one. Without it a Hermes call that fails
    inside _start_fresh_attempt, or a crash between the two, would lose the decision for good: the failed card would
    sit blocked, never replaced, with the run counted as handled. The redo is driven by the events table itself, so
    it also survives a restart.

    Each part is idempotent, and a failing Hermes call is recorded (retry_card_error, replan_error) and retried on
    the next pass; it never stops the other tasks."""
    recovery_mod.refresh_review_rounds(board, plan, conn=conn)
    decisions = recovery_mod.process_failures(board, plan, project, models_config, conn=conn, now=now)
    rows = []
    attempted = set()
    for decision in decisions:
        attempted.add((decision.card_id, decision.run_id))
        _apply_decision(board, repo, plan, project, models_config, decision, conn=conn)
        rows.append(_recovery_row(decision.task_key, decision.action, decision.failure_kind))
    for decision in _pending_decisions(conn, plan, skip=attempted):
        if _redrive(board, repo, plan, project, models_config, decision, conn=conn):
            rows.append(_recovery_row(decision.task_key, decision.action, decision.failure_kind))
    rows.extend(_escalate_spent_budgets(board, plan, project, conn=conn))
    return rows


def _apply_decision(board, repo, plan, project, models_config, decision, *, conn) -> bool:
    """Carry out the controller's half of one recovery decision. True when it is done (or needs nothing from the
    controller), False when a call failed: the failure is recorded once and the decision is picked up again on the
    next pass (_pending_decisions). Never raises."""
    if decision.action not in _CONTROLLER_ACTIONS:
        return True
    try:
        task = plan.task(decision.task_key)
        if decision.action == recovery_mod.ACTION_REPLAN:
            return _request_replan(board, plan, project, task, decision.card_id, decision, conn=conn)
        return _start_fresh_attempt(
            board, repo, plan, project, models_config, task, decision.card_id, decision, conn=conn,
        ) is not None
    except Exception as exc:  # noqa: BLE001 - see the docstring: one task must never stop the others
        _record_once(conn, "recovery_action_error", {
            "project": plan.project, "task_key": decision.task_key, "card_id": decision.card_id,
            "action": decision.action, "error": _clean(f"{type(exc).__name__}: {exc}"),
        }, match=("project", "task_key", "card_id", "action", "error"))
        return False


def _latest_run_id(card: dict):
    """The id recovery gives the LAST run of `card` that has an outcome (`#<index>` for a run with no id), or None.
    The same rule as recovery._latest_run, so an id read back from a `recovery_decision` event compares equal."""
    runs = card.get("_runs") or []
    for index in range(len(runs) - 1, -1, -1):
        run = runs[index]
        if isinstance(run, dict) and str(run.get("outcome") or "").strip():
            return run.get("id") if run.get("id") is not None else f"#{index}"
    return None


def _pending_decisions(conn, plan: plan_mod.Plan, *, skip=frozenset()) -> list:
    """The `recovery_decision` events of this project that ask the controller to act (fresh_attempt, switch_model,
    replan) and were never carried out, as recovery.Decision objects, oldest first. `skip` is a set of (card id, run
    id) pairs to leave out (the ones this pass has just tried).

    A decision is carried out once a marker event names its card and run: `retry_card_created` or `retry_card_skipped`
    for a fresh attempt or a model switch (`old_card`), `replan_requested` or `replan_skipped` for a re-plan
    (`card_id`). A fresh attempt is also finished, whatever the events say, once plan_tasks no longer points at the card
    it was decided about (the repoint is its last step), which keeps the scan to the few cards still current."""
    current = {
        row["task_key"]: row["work_card_id"] for row in conn.execute(
            "SELECT task_key, work_card_id FROM plan_tasks WHERE project = ?", (plan.project,),
        )
    }
    rows = conn.execute(
        "SELECT payload FROM events WHERE kind = 'recovery_decision' AND json_extract(payload, '$.project') = ? "
        "AND json_extract(payload, '$.action') IN (?, ?, ?) ORDER BY id", (plan.project, *_CONTROLLER_ACTIONS),
    ).fetchall()
    pending = []
    for row in rows:
        payload = _event_payload(row["payload"])
        key, card_id, run_id, action = (
            payload.get("task_key"), payload.get("card_id"), payload.get("run_id"), payload.get("action"),
        )
        if payload.get("applied") or not key or not card_id or (card_id, run_id) in skip:
            continue
        replan = action == recovery_mod.ACTION_REPLAN
        if not replan and current.get(key) != card_id:
            continue
        markers, field = (
            (("replan_requested", "replan_skipped"), "card_id") if replan
            else (("retry_card_created", "retry_card_skipped"), "old_card")
        )
        done = conn.execute(
            f"SELECT 1 FROM events WHERE kind IN (?, ?) AND json_extract(payload, '$.project') = ? "
            f"AND json_extract(payload, '$.{field}') = ? AND json_extract(payload, '$.run_id') IS ? LIMIT 1",
            (*markers, plan.project, card_id, run_id),
        ).fetchone()
        if done is not None:
            continue
        try:
            kind = recovery_mod.FailureKind(payload.get("kind"))
        except ValueError:
            kind = None
        pending.append(recovery_mod.Decision(
            action, str(payload.get("reason") or ""), model=payload.get("model"), provider=payload.get("provider"),
            task_key=key, card_id=card_id, run_id=run_id, failure_kind=kind,
        ))
    return pending


def _redrive(board, repo, plan, project, models_config, decision, *, conn) -> bool:
    """Carry out a decision that an earlier pass left unfinished, after checking it still applies. Time has passed, and
    a person may have answered the card or resumed it: replacing a card that is running again would archive it under
    its worker (Hermes terminates the worker of an archived card). So the card must still be `blocked` (or, for a
    replacement whose archive step already ran, `archived`) and its latest ended run must still be the run that was
    decided about. If not, the decision is dropped with a `retry_card_skipped` or `replan_skipped` event, which is also
    what stops it being considered again. True when the decision was carried out."""
    replan = decision.action == recovery_mod.ACTION_REPLAN
    skipped, field = ("replan_skipped", "card_id") if replan else ("retry_card_skipped", "old_card")
    try:
        card = hermes_mod.kanban_show(board, decision.card_id)
    except Exception as exc:  # noqa: BLE001 - one unreadable card must not stop the others; it is tried again
        _record_once(conn, "recovery_action_error", {
            "project": plan.project, "task_key": decision.task_key, "card_id": decision.card_id,
            "action": decision.action, "error": _clean(f"{type(exc).__name__}: {exc}"),
        }, match=("project", "task_key", "card_id", "action", "error"))
        return False
    allowed = ("blocked",) if replan else ("blocked", "archived")
    status = card.get("status")
    if status not in allowed:
        reason = f"the card is {status}, not blocked: it was answered or resumed since the decision was made"
    elif _latest_run_id(card) != decision.run_id:
        reason = "the card has a newer run than the one the decision was made about"
    else:
        reason = None
    if reason is not None:
        events.record(conn, skipped, {
            "project": plan.project, "task_key": decision.task_key, field: decision.card_id,
            "run_id": decision.run_id, "reason": reason,
        })
        return False
    return _apply_decision(board, repo, plan, project, models_config, decision, conn=conn)


def _retry_count(conn, project: str, task_key: str) -> int:
    """How many replacement cards `retry_card_created` events say were made for this task."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE kind = 'retry_card_created' "
        "AND json_extract(payload, '$.project') = ? AND json_extract(payload, '$.task_key') = ?",
        (project, task_key),
    ).fetchone()
    return int(row["n"])


def _has_retry_card(conn, project: str, task_key: str) -> bool:
    return _retry_count(conn, project, task_key) > 0


def _next_retry_number(conn, project: str, task_key: str) -> int:
    """<n> of the next `retry <n>` card of a task: the number of `retry_card_created` events recorded for it, plus one.
    A decision that _redrive dropped (`retry_card_skipped`) counts too: the attempt it belonged to may have made a card
    before it was dropped, and the next decision must not adopt that stray card through its idempotency key."""
    skipped = conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE kind = 'retry_card_skipped' "
        "AND json_extract(payload, '$.project') = ? AND json_extract(payload, '$.task_key') = ?",
        (project, task_key),
    ).fetchone()
    return _retry_count(conn, project, task_key) + int(skipped["n"]) + 1


def _branch_diff(repo, integration_branch: str, old_card: dict, task: plan_mod.PlanTask) -> str:
    """What the failed attempt changed: `git diff <integration>...<branch>` (against the merge base, so it shows the
    attempt and not what integration gained since), or "" when the branch does not exist or git cannot answer. The
    branch is the card's own (branch_name), else the name create_cards_from_plan gives a work card. Never raises."""
    branch = old_card.get("branch_name") or f"swarm/{task.key}-{task.role}"
    try:
        exists = subprocess.run(
            [*gitexec.GIT, "-C", str(repo), "rev-parse", "--verify", "-q", f"refs/heads/{branch}"],
            capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace", env=gitexec.git_env(),
        )
        if exists.returncode != 0:
            return ""
        # ASES-SEC-01/ASES-SEC-04: this text is embedded verbatim into the next attempt's retry card
        # (recovery.failure_bundle), so it must be git's own diff, never a worker-configured external-diff or
        # textconv driver's rendering of it (gitexec.DIFF_SAFETY).
        diff = subprocess.run(
            [*gitexec.GIT, "-C", str(repo), "diff", *gitexec.DIFF_SAFETY, f"{integration_branch}...{branch}"],
            capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace", env=gitexec.git_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return diff.stdout[:_DIFF_READ_LIMIT] if diff.returncode == 0 else ""


def _last_gate_detail(conn, task_key: str) -> str:
    """The detail of the newest gate_runs row of this task (any gate), redacted; "" when it never ran a gate. The gate
    output is command output, so it can carry a secret. (gate_runs has no project column, so two projects that reuse a
    task key share this lookup: known, and only a hint on a card.)"""
    row = conn.execute(
        "SELECT detail FROM gate_runs WHERE task_key = ? ORDER BY id DESC LIMIT 1", (task_key,),
    ).fetchone()
    return events.redact_text(row["detail"] or "") if row else ""


def _findings_text(card: dict) -> str:
    """The reviewer findings for a failure bundle: the last three comments of the card that start `CHANGES REQUESTED:`
    (the controller's send-back, and the reviewer's) or `ANSWER:` (a person's guidance), oldest first."""
    picked = []
    for comment in card.get("_comments") or []:
        body = comment.get("body") if isinstance(comment, dict) else None
        if isinstance(body, str) and body.lstrip().startswith(("CHANGES REQUESTED:", "ANSWER:")):
            picked.append(body.strip())
    return "\n\n".join(picked[-3:])


def _start_fresh_attempt(
    board: str, repo: pathlib.Path, plan: plan_mod.Plan, project: ases_config.ProjectConfig, models_config: dict,
    task: plan_mod.PlanTask, old_card_id: str, decision, *, conn,
) -> str | None:
    """ASES-REC-01 (blueprint 19.2): "A capability failure means the attempt itself was wrong: start the next attempt
    from a fresh worktree at the current integration HEAD, attach the failure bundle (criteria, diff, gate output,
    reviewer findings) to the card, and on the second capability failure switch to the next model for that role
    class." Creates the replacement for the failed card `old_card_id` and returns its id, or None when a call failed.

    The new card is titled `<key>: retry <n>` (n is the number of retry_card_created events recorded for the task, plus
    one), on branch swarm/<key>-retry<n>, with the task's own role, assignee and worktree workspace, the failed
    card's Hermes project id, the failed card's own parents (its dependency merge cards) and the same --max-retries
    and --max-runtime as the original. Its body is the ordinary work-card body, a blank line, then
    recovery.failure_bundle for the failed card: the acceptance criteria, what failed, the diff of the old branch
    against integration, the newest gate output of the task and the last three reviewer or user comments, all redacted
    by the bundle. The idempotency key is ases-retry-<project>-<key>-<n>, so repeating a half-finished attempt returns
    the card that was already made instead of a second one. For a `switch_model` decision the decided model and provider
    are pinned on the new card at once (kanban set-model), so no worker starts on the model that failed.

    Then, in this order: the new card is linked as an extra parent of the task's merge card; the old card's usage is
    ingested (guarded like the fix path); the OLD card is archived, so it stops showing as a blocked question (Hermes
    terminates a running worker of an archived card, which is why _redrive checks the card first);
    plan_tasks.work_card_id is repointed at the new card in one UPDATE together with the `retry_card_created` event
    (one savepoint, so the two can never disagree). The repoint is last on
    purpose (a deviation from the order the work order lists): every step before it is safe to repeat, so a failure or a
    crash anywhere leaves plan_tasks pointing at the OLD card and the whole attempt is repeated, and the old card is
    already archived only in a run that had finished everything but the repoint. A repeat finds the card it made
    before through the idempotency key, whatever became of it meanwhile: Hermes may have dispatched it and even
    finished it while a later step kept failing, and that work is kept, not redone. A retry number whose key comes
    back with the OLD card itself is skipped, so the attempt can never archive the card it is about to use.

    Never raises: a failing call records retry_card_error (once per message) and returns None, and the decision is
    carried out again on the next pass (_pending_decisions), because recovery.process_failures has already counted the
    run and would never return this decision again."""
    key = task.key
    try:
        old_card = hermes_mod.kanban_show(board, old_card_id)
        merge_card_id = conn.execute(
            "SELECT merge_card_id FROM plan_tasks WHERE project = ? AND task_key = ?", (plan.project, key),
        ).fetchone()["merge_card_id"]
        assignee = policy.resolve_assignee(task.role, project.roles)
        body = _work_card_body(task, _reviewer_profile(project)) + "\n\n" + recovery_mod.failure_bundle(
            old_card, criteria=list(task.acceptance),
            diff_text=_branch_diff(repo, plan.integration_branch, old_card, task),
            gate_output=_last_gate_detail(conn, key), reviewer_findings=_findings_text(old_card),
        )
        parents = [p for p in old_card.get("_parents") or [] if isinstance(p, str) and p]
        number = _next_retry_number(conn, plan.project, key)
        new_id = None
        for _ in range(_RETRY_CREATE_TRIES):
            created = hermes_mod.kanban_create(
                board, f"{key}: retry {number}", assignee=assignee, workspace="worktree",
                branch=f"swarm/{key}-retry{number}", project=old_card.get("project_id"), body=body,
                parent=parents or None, idempotency_key=f"ases-retry-{plan.project}-{key}-{number}",
                max_retries=project.budgets.get("attempts_per_card", 3),
                max_runtime=f"{project.budgets.get('card_runtime_minutes', 45)}m",
            )
            if created.get("id") not in (None, old_card_id):
                new_id = created["id"]
                break
            number += 1
        if new_id is None:
            raise RuntimeError(f"no retry number of {key} gave a card other than an existing one")
        if decision.action == recovery_mod.ACTION_SWITCH_MODEL and decision.model:
            hermes_mod.kanban_set_model(board, new_id, decision.model, provider=decision.provider)
        hermes_mod.kanban_link(board, new_id, merge_card_id)
        _ingest_outgoing_usage(board, old_card_id, project, models_config, plan, key, conn)
        if old_card.get("status") != "archived":
            hermes_mod.kanban_archive(board, [old_card_id])
        with _atomic(conn):
            conn.execute(
                "UPDATE plan_tasks SET work_card_id = ? WHERE project = ? AND task_key = ?",
                (new_id, plan.project, key),
            )
            events.record(conn, "retry_card_created", {
                "project": plan.project, "task_key": key, "old_card": old_card_id, "new_card": new_id,
                "n": number, "run_id": decision.run_id, "action": decision.action, "model": decision.model,
                "provider": decision.provider, "idempotency_key": f"ases-retry-{plan.project}-{key}-{number}",
            })
        return new_id
    except Exception as exc:  # noqa: BLE001 - see the docstring: never raises, the next pass repeats it
        _record_once(conn, "retry_card_error", {
            "project": plan.project, "task_key": key, "old_card": old_card_id,
            "error": _clean(f"{type(exc).__name__}: {exc}"),
        }, match=("project", "task_key", "old_card", "error"))
        return None


def _last_error(card: dict) -> str:
    """The last error of a card, short and clean, for a question: the error (else the summary) of its newest FAILED
    run, else the newest `CHANGES REQUESTED:` comment (a spent review budget has no failed run), else ""."""
    for run in reversed(card.get("_runs") or []):
        if (isinstance(run, dict) and str(run.get("outcome") or "").strip()
                and recovery_mod.classify_run(run) is not recovery_mod.FailureKind.NONE):
            text = run.get("error") or run.get("summary")
            if isinstance(text, str) and text.strip():
                return _clean(text, 200)
    for comment in reversed(card.get("_comments") or []):
        body = comment.get("body") if isinstance(comment, dict) else None
        if isinstance(body, str) and body.lstrip().startswith("CHANGES REQUESTED:"):
            return _clean(body, 200)
    return ""


def _request_replan(
    board: str, plan: plan_mod.Plan, project: ases_config.ProjectConfig, task: plan_mod.PlanTask, card_id: str,
    decision, *, conn,
) -> bool:
    """ASES-REC-02 (blueprint 19.3): "When a lineage budget runs out, the Lead may re-plan that task once with the full
    failure bundle. After that the controller blocks the task with a question for the user." The Lead re-plan call
    itself is NOT automated in this round: what is done is the bookkeeping of one re-plan, and the question to the
    person. The question names the task, the failure kind and the last error (redacted, short), and says what the user
    can do: `swarm answer <card> "<guidance>"` retries the same card with the guidance.

    The order is the question first, then the counters: recovery.bump(replans), bounds.add_replan (Table 17: two
    re-plans per project, then a user decision) and the `replan_requested` event are written together in one
    savepoint once the question is out. A question that fails leaves nothing counted and is asked again, and a crash
    between the two finds the question already asked (ask_user answers "already_asked") and counts it then. The event
    also records the lineage counters at that moment, which is how _escalate_spent_budgets knows the spending it is
    looking at was already escalated. True when done; a failing call records replan_error (once per message) and
    returns False."""
    try:
        card = hermes_mod.kanban_show(board, card_id)
        lineage = recovery_mod.load_lineage(conn, plan.project, task.key)
        error = _last_error(card)
        kind = str(decision.failure_kind) if decision.failure_kind else "budget"
        text = (
            f"{task.key}: {kind} problem" + (f", last error: {error}" if error else "") + f". {decision.reason} "
            "The Lead re-plan is not automated in this version, so ASES cannot go on by itself. "
            f"How should this task proceed? To retry this same card with your guidance, run: "
            f"swarm answer {card_id} \"<guidance>\""
        )
        asked = questions_mod.ask_user(board, card, text, conn=conn)
    except Exception as exc:  # noqa: BLE001 - see the docstring
        _record_once(conn, "replan_error", {
            "project": plan.project, "task_key": task.key, "card_id": card_id,
            "error": _clean(f"{type(exc).__name__}: {exc}"),
        }, match=("project", "task_key", "card_id", "error"))
        return False
    with _atomic(conn):
        recovery_mod.bump(conn, plan.project, task.key, "replans")
        bounds_mod.add_replan(conn, plan.project)
        events.record(conn, "replan_requested", {
            "project": plan.project, "task_key": task.key, "card_id": card_id, "run_id": decision.run_id,
            "kind": kind, "asked": asked, "review_rounds": lineage.review_rounds, "fix_cards": lineage.fix_cards,
            "capability_failures": lineage.capability_failures,
        })
    return True


def _project_replans(conn, project: str) -> int:
    """Re-plans spent by the whole project: the sum of the per-task lineage counters (the number recovery's own
    _adjust compares with replans_per_project)."""
    row = conn.execute(
        "SELECT COALESCE(SUM(replans), 0) AS total FROM lineage WHERE project = ?", (project,),
    ).fetchone()
    return int(row["total"])


def _escalate_spent_budgets(board: str, plan: plan_mod.Plan, project: ases_config.ProjectConfig, *, conn) -> list[dict]:
    """ASES-REC-02 and Table 17 ("Review rounds per plan task 3: Escalate to the Lead, then to the user"): escalate a
    task whose review-round budget is spent, which no failed run announces. For each plan task whose CURRENT work card
    is not done, archived or running: recovery.exhausted names the budget, and when it is `review_rounds`
    recovery.escalation says what to do, a re-plan while the task has had none (_request_replan), else a question
    (questions.ask_user). Returns [{"task_key", "action", "kind"}, ...] for the escalations made this pass.

    Two other budgets are deliberately left to their own owners. Fix cards: Table 17 says "Escalate to the user"
    (there is no re-plan), and the merge queue does exactly that at the failure that would need one more fix card;
    acting here as soon as the count reaches the limit would ask about a second fix card that has not even run yet, and
    block it. Attempts: process_failures decides them from the failed run, the moment the run fails.

    A task is escalated once for each number of review rounds it has spent (the `replan_requested` and
    `lineage_escalated` events record it), which is what keeps a person's answer from being followed at once by the same
    question: the next escalation needs another round. A card with any open question is left alone (the person has
    something to answer first), and so is a running one (blocking it would cut its worker off mid-attempt)."""
    limits = recovery_mod.Bounds.from_budgets(project.budgets)
    rows = []
    for task in plan.tasks:
        row = conn.execute(
            "SELECT work_card_id FROM plan_tasks WHERE project = ? AND task_key = ?", (plan.project, task.key),
        ).fetchone()
        if row is None or not row["work_card_id"]:
            continue
        lineage = recovery_mod.load_lineage(conn, plan.project, task.key)
        if recovery_mod.exhausted(lineage, limits) != "review_rounds" or lineage.review_rounds < 1:
            continue
        seen = conn.execute(
            "SELECT 1 FROM events WHERE kind IN ('replan_requested', 'lineage_escalated') "
            "AND json_extract(payload, '$.project') = ? AND json_extract(payload, '$.task_key') = ? "
            "AND json_extract(payload, '$.review_rounds') = ? LIMIT 1",
            (plan.project, task.key, lineage.review_rounds),
        ).fetchone()
        if seen is not None:
            continue
        card_id = row["work_card_id"]
        try:
            card = hermes_mod.kanban_show(board, card_id)
            if card.get("status") in ("done", "archived", "running") or _open_question(card) is not None:
                continue
            decision = recovery_mod.escalation(lineage, limits)
            if decision is None:
                continue
            if (decision.action == recovery_mod.ACTION_REPLAN
                    and _project_replans(conn, plan.project) >= limits.replans_per_project):
                decision = recovery_mod.Decision(recovery_mod.ACTION_BLOCK_FOR_USER, (
                    f"The review-round budget of this task is spent, but the project has already used its "
                    f"{limits.replans_per_project} re-plans (section 9.3: a user decision). Should ASES change the "
                    "task itself, take a different approach, or stop it?"
                ))
            if decision.action == recovery_mod.ACTION_REPLAN:
                done = _request_replan(board, plan, project, task, card_id, decision, conn=conn)
            else:
                asked = questions_mod.ask_user(board, card, f"{task.key}: {decision.reason}", conn=conn)
                events.record(conn, "lineage_escalated", {
                    "project": plan.project, "task_key": task.key, "card_id": card_id, "asked": asked,
                    "review_rounds": lineage.review_rounds, "fix_cards": lineage.fix_cards,
                    "capability_failures": lineage.capability_failures,
                })
                done = True
        except Exception as exc:  # noqa: BLE001 - one task must never stop the others; it is tried again
            _record_once(conn, "recovery_action_error", {
                "project": plan.project, "task_key": task.key, "card_id": card_id, "action": "escalate",
                "error": _clean(f"{type(exc).__name__}: {exc}"),
            }, match=("project", "task_key", "card_id", "action", "error"))
            continue
        if done:
            rows.append(_recovery_row(task.key, decision.action, "review_rounds"))
    return rows


# --- parked cards ---------------------------------------------------------------------------------------------------


def _latest_scheduled_reason(card: dict) -> str | None:
    """The reason of the card's newest `scheduled` event (kanban_show's `_events`), or None. Newest by created_at, and
    among equal times by position in the list."""
    best = None
    for position, event in enumerate(card.get("_events") or []):
        if not isinstance(event, dict) or event.get("kind") != "scheduled":
            continue
        stamp = (_number(event.get("created_at")), position)
        if best is None or stamp >= best[0]:
            best = (stamp, event)
    if best is None:
        return None
    reason = _event_payload(best[1]).get("reason")
    return reason if isinstance(reason, str) else None


def process_unpark(
    board: str, plan: plan_mod.Plan, models_config: dict, *, conn, budgets: dict,
    project: ases_config.ProjectConfig | None = None,
) -> list[str]:
    """ASES-CAP-03 and Table 17 ("Requests per provider per day: Park cards until the reset"); the loop's
    `for card in board.parked_past_reset(): hermes.unblock(card)`. process_budget_gate parks a card it cannot afford
    with `hermes kanban schedule`, and nothing woke it again when the provider's day rolled over. This does: for
    every `scheduled` card of this plan (scoped by plan_tasks like the gate: a board carries several projects) whose
    latest `scheduled` event has a reason starting `budget:` or `review budget` (the reasons the gate writes, so
    it was parked by this controller: a card someone else scheduled is never touched), the affordability is
    recomputed with the SAME code the gate uses (_affordable_now), and a card that is affordable now is unblocked
    with the reason "budget available again" and recorded as `card_unparked`. Returns the task keys unparked.

    Round 7 (ASES-PRV-01/03, bug 2): a "data class:" park is DELIBERATELY excluded by the prefix check above (it
    is not one of _PARK_PREFIXES, see the comment on it), so a card _affordable_now parked for its provider's data
    policy is never even considered here, let alone unblocked: ASES-PRV-03 forbids relaxing the project's data
    class to keep work flowing, and this loop's whole point is picking work back up automatically. `project` is
    passed into _affordable_now below too (belt and braces, since a card that WAS parked for budget could in
    principle also now fail the data-class check if the project's declared data_class or the provider's policy
    changed underneath it), but the prefix check above is what actually keeps a data-class park untouched.

    A card that cannot be read or unblocked is recorded (unpark_error, once per message) and the rest carry on."""
    review_afford = usage_mod.review_budget(conn, models_config, project) if project is not None else None
    unparked = []
    for card in hermes_mod.kanban_list(board, status="scheduled"):
        row = conn.execute(
            "SELECT task_key FROM plan_tasks WHERE work_card_id = ? AND project = ?", (card["id"], plan.project),
        ).fetchone()
        if row is None:
            continue
        task = plan.task(row["task_key"])
        try:
            reason = _latest_scheduled_reason(hermes_mod.kanban_show(board, card["id"]))
            if reason is None or not reason.lstrip().startswith(_PARK_PREFIXES):
                continue
            ok, _ = _affordable_now(conn, task, models_config, budgets, review_afford, project)
            if not ok:
                continue
            hermes_mod.kanban_unblock(board, card["id"], reason="budget available again")
        except Exception as exc:  # noqa: BLE001 - one card must never stop the others
            _record_once(conn, "unpark_error", {
                "task_key": task.key, "card_id": card["id"], "error": _clean(f"{type(exc).__name__}: {exc}"),
            }, match=("card_id", "error"))
            continue
        unparked.append(task.key)
        events.record(conn, "card_unparked", {"task_key": task.key, "card_id": card["id"]})
    return unparked


# --- bounds, pause and report ---------------------------------------------------------------------------------------


def _num_text(value) -> str:
    """A bound's used or limit for a sentence: whole numbers as they are, a wall clock (float minutes) to 1 decimal."""
    if isinstance(value, float):
        text = f"{value:.1f}"
        return text[:-2] if text.endswith(".0") else text
    return str(value)


def process_bounds(
    board: str, repo: pathlib.Path, plan: plan_mod.Plan, project: ases_config.ProjectConfig, models_config: dict,
    *, conn, now=None,
) -> tuple[bool, str | None]:
    """ASES-CTL-01 (blueprint 9.3): "It is stopped, not finished, when any global bound is reached." Measures every
    bound (bounds.evaluate_bounds) and asks which breached ones stop a whole project (bounds.stop_reasons: the
    re-plans per project, "User decision", and the project wall clock, "Pause and report"; every other breach
    escalates one task and belongs to recovery). When one does, the project is paused and reported
    (pause_and_report) and (True, the reason) comes back; else (False, None). `now` is a datetime, epoch seconds or an
    ISO 8601 string. A malformed budgets: block raises ValueError (bounds.Bounds.from_budgets names the key)."""
    limits = bounds_mod.Bounds.from_budgets(project.budgets)
    statuses = bounds_mod.evaluate_bounds(board, plan, limits, models_config, conn=conn, now=_as_datetime(now))
    stops = bounds_mod.stop_reasons(statuses)
    if not stops:
        return False, None
    reason = "; ".join(
        f"{s.name} reached for {s.subject} ({_num_text(s.used)} of {_num_text(s.limit)}): {s.on_reach}" for s in stops
    )
    events.record(conn, "bounds_reached", {
        "project": plan.project,
        "bounds": [{"name": s.name, "subject": s.subject, "used": s.used, "limit": s.limit} for s in stops],
    })
    pause_and_report(board, repo, plan, project, models_config, reason, conn=conn)
    return True, reason


def _report_directory(project: ases_config.ProjectConfig, stamp: str) -> pathlib.Path:
    """<ases_home>/reports/<project name>/<UTC timestamp>-paused/ (a -2, -3 suffix when the name is taken), never
    inside the repository: a report must not dirty the primary checkout the integrity guard watches."""
    label = re.sub(r"[^A-Za-z0-9._-]+", "_", str(project.name)) or "project"
    base = pathlib.Path(project.ases_home) / "reports" / label
    directory = base / f"{stamp}-paused"
    number = 2
    while directory.exists():
        directory = base / f"{stamp}-paused-{number}"
        number += 1
    return directory


def pause_and_report(
    board: str, repo: pathlib.Path, plan: plan_mod.Plan, project: ases_config.ProjectConfig, models_config: dict,
    reason: str, *, conn,
) -> str:
    """ASES-REC-06 and ASES-CTL-01 (the loop's `if bounds_reached() or user_stop(): pause_and_report()`): stop new
    work, remember why, write the report. Returns the report directory (`<ases_home>/reports/<project name>/<UTC
    timestamp>-paused/`), or "" when it could not be worked out.

    In this order: `hermes pause` (nothing new is dispatched; running work is not touched, that is Hermes's own
    documented behaviour), then the project's status becomes `paused` (bounds.set_status, after which run_pass and the
    merge queue halt), then a `project_paused` event that keeps the reason (bounds.set_status stores a reason only for
    `stopped`), then report.build_report and report.write_report into the directory. The pause comes first because a
    Hermes that is not paused lets its gateway dispatcher keep starting workers while the controller believes it has
    stopped, and the opposite failure only costs one more pause on the next pass.

    Every step is guarded on its own and a failure is an event (pause_error, pause_state_error, pause_report_error),
    never an exception: this runs exactly when things are going wrong, and a report that cannot be written must not
    stop the pause. The reason is redacted and escaped to ASCII, because it is passed to a command line and printed."""
    why = _clean(reason, 500) or "a bound was reached"
    try:
        directory = _report_directory(project, datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    except Exception as exc:  # noqa: BLE001 - see the docstring
        directory = None
        events.record(conn, "pause_report_error", {"error": _clean(f"{type(exc).__name__}: {exc}")})
    try:
        hermes_mod.pause(why)
    except Exception as exc:  # noqa: BLE001 - see the docstring
        events.record(conn, "pause_error", {"error": _clean(f"{type(exc).__name__}: {exc}")})
    try:
        bounds_mod.set_status(conn, plan.project, "paused", why)
    except Exception as exc:  # noqa: BLE001 - see the docstring
        events.record(conn, "pause_state_error", {"error": _clean(f"{type(exc).__name__}: {exc}")})
    try:
        events.record(conn, "project_paused", {
            "project": plan.project, "reason": why, "report_dir": str(directory) if directory else "",
        })
    except Exception:  # noqa: BLE001 - a full disk is exactly the case this must survive
        pass
    if directory is not None:
        try:
            report_mod.write_report(report_mod.build_report(board, plan, project, models_config, conn), directory)
        except Exception as exc:  # noqa: BLE001 - see the docstring
            events.record(conn, "pause_report_error", {"error": _clean(f"{type(exc).__name__}: {exc}")})
    return str(directory) if directory else ""


# --- provisioning, idle worktrees, finish ----------------------------------------------------------------------------


def process_provision(board: str, plan: plan_mod.Plan, *, conn) -> list[str]:
    """ASES-GIT-14 (Table: "Each card gets its own port block, COMPOSE_PROJECT_NAME, database name or schema, and temp
    directory through environment variables written to .env.ases in its worktree"): give every running work card its
    env file (leases.provision_running_cards, best effort by nature: Hermes starts the worker in the same step as it
    creates the worktree, so the file arrives on the pass after) and release the leases of cards that are no longer
    live (leases.sweep_finished). A BLOCKED card counts as live, because it waits for an answer and then resumes in the
    same worktree with the same ports. Returns the ids of the cards provisioned this pass."""
    provisioned = leases_mod.provision_running_cards(board, conn, plan)
    try:
        leases_mod.sweep_finished(board, conn, plan, live_statuses=_LEASE_LIVE_STATUSES)
    except Exception as exc:  # noqa: BLE001 - a lease that is not swept now is swept next pass
        events.record(conn, "lease_sweep_error", {"error": _clean(f"{type(exc).__name__}: {exc}")})
    return list(provisioned)


def process_idle_worktrees(board: str, repo: pathlib.Path, plan: plan_mod.Plan, *, conn) -> list[str]:
    """ASES-GIT-12 (blueprint 8.4: "Before a worker starts and after it stops, the controller snapshots git status
    --porcelain and HEAD of the primary checkout and of every other active worktree. Any change outside the worker's own
    worktree fails the card and raises a security event."): guards.check_idle_worktrees reports a worktree that
    changed while no running card owned it. What comes back are WARNINGS, an `idle_worktree_changed` event each, and
    never a halt: this first version has known false positives (a reviewer legitimately works in a card's worktree
    while the card is in review, and a card re-dispatched between two polls looks idle in between), so it informs a
    person and does not stop the run. The paths that count as owned are those of every running card of the board, not
    only this plan's: another project on the same repository owns its worktrees too."""
    running = hermes_mod.kanban_list(board, status="running")
    paths = [card["workspace_path"] for card in running if card.get("workspace_path")]
    problems = list(guards_mod.check_idle_worktrees(conn, plan.project, repo, paths))
    for problem in problems:
        events.record(conn, "idle_worktree_changed", {"project": plan.project, "problem": _clean(problem, 500)})
    return problems


def _finalgates():
    """The final-gates module, imported on first use: it is built at the same time as this one, and only needed once
    every merge card is done."""
    return importlib.import_module(f"{__package__}.finalgates")


def process_finalize(
    board: str, repo: pathlib.Path, plan: plan_mod.Plan, project: ases_config.ProjectConfig, models_config: dict,
    *, conn, now=None,
) -> str | None:
    """ASES-TSK-04 and ASES-CTL-01: "Final integration security and smoke gates are controller lifecycle operations",
    and "A project is finished when every merge card is done, Gates 4 and 5 are green on the integration HEAD, and the
    release report is written." Only when every merge card is done (all_merge_cards_done), the project is not already
    finished and not halted (a stop that arrived mid-pass must not start the final gates), this calls
    finalgates.finalize and returns its status ("finished", "gate_failed", "not_ready" or "error"); None when it did
    not run. A `gate_failed` pauses the project (pause_and_report) with finalgates.final_gate_question(outcome) as the
    reason, so the person is asked what to do about the red gate and the loop stops until they answer."""
    state = bounds_mod.get_state(conn, plan.project)
    if state is not None and state["status"] == "finished":
        return None
    if _halted(conn, plan.project)[0] or not all_merge_cards_done(board, plan, conn=conn):
        return None
    finalgates = _finalgates()
    # Round 9 (ASES-QG-04, ASES-SEC-03): Gates 4 and 5 get no task, so they never carry a task's own network
    # exception through (ASES-SEC-05, ASES-SEC-07: "Gates 4/5 and every other task stay --network none").
    # `self_contained_checkout` is bound onto run_gate4/run_gate5 themselves via functools.partial, not passed
    # through finalize()'s own gate_kwargs, so a test's run4/run5 stand-in (a fixed signature, no **kwargs) is
    # never handed a keyword it does not know about. The sandbox kwargs (runner/run4/run5) are added to this call
    # ONLY when the sandbox is actually enabled, for the same reason: a finalize() stand-in (or the real one, with
    # its own defaults) sees exactly today's call shape otherwise. An infrastructure failure (Docker down, the
    # pinned image missing) is already handled by finalgates._run_final_gate, which turns any exception from
    # run4/run5 into status "error" with a final_gate_error event and no gate row written: not a red gate, and
    # nothing here needs to catch it again.
    choice = gates_mod.resolve_runner(project)
    sandbox_kwargs = {}
    if choice.self_contained:
        sandbox_kwargs["runner"] = choice.runner
        sandbox_kwargs["run4"] = functools.partial(finalgates.run_gate4, self_contained_checkout=True)
        sandbox_kwargs["run5"] = functools.partial(finalgates.run_gate5, self_contained_checkout=True)
    outcome = finalgates.finalize(
        board, repo, plan, project, models_config, conn, now=now, **sandbox_kwargs,
    )
    events.record(conn, "finalize_result", {
        "project": plan.project, "status": outcome.status, "reason": _clean(getattr(outcome, "reason", ""), 300),
    })
    if outcome.status == "gate_failed":
        pause_and_report(board, repo, plan, project, models_config, finalgates.final_gate_question(outcome), conn=conn)
    return outcome.status


# --- the pass -------------------------------------------------------------------------------------------------------


def _isolated(conn, summary: dict, step: str, fn, default):
    """Run one step of the pass that is NOT safety-critical, and turn an exception into a `pass_step_error` event naming
    the step plus a warning line in the summary, then carry on with `default`: a stale ledger, a failed lease or a
    final gate that cannot run must not stop the merge queue. KeyboardInterrupt and SystemExit are not caught."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - deliberately broad: see the docstring
        text = _clean(f"{type(exc).__name__}: {exc}")
        events.record(conn, "pass_step_error", {"step": step, "error": text})
        summary["warnings"].append(f"step {step} failed: {text}")
        return default


def run_pass(
    board: str, repo: pathlib.Path, plan: plan_mod.Plan, project: ases_config.ProjectConfig,
    models_config: dict, *, conn, now=None,
) -> dict:
    """One full controller iteration (loop version 2), in the order of blueprint section 9.2, which a bounded
    `swarm run` calls repeatedly, sleeping between calls to respect provider pacing. Returns a summary with every one
    of these keys, always: parked, dispatch, sent_back, merged, unreviewed, usage_sessions, integrity (a non-empty
    list halts the run), warnings (never halt), recovery, unparked, provisioned, stopped, stop_reason, final (None, or
    the status finalgates.finalize returned) and finished (True only when bounds says the project is finished).

      0. halted (a stopped or paused project): return at once, `stopped` True, doing nothing else
      1. the primary-checkout guard (ASES-GIT-12): a violation returns with `integrity` set
      2. idle worktrees (warnings)                    3. usage ingest into the ledger (ASES-CAP-03)
      4. failure recovery and spent budgets           5. bounds: a project-stopping bound pauses and returns
      6. the budget gate, then unpark                 7. review-lane policing (Gate 1 re-check)
      8. dispatch, then provisioning                  9. the merge queue (halt checks inside; ASES-GIT-05: a
                                                           post-merge revert that cannot repair the branch also
                                                           returns with `integrity` set)
     10. the final gates, once every merge card is done

    Steps 2 to 5, unpark, provisioning and the final gates are not safety-critical for the pass: an exception in one is
    recorded as a `pass_step_error` event naming the step (and a warning), and the pass goes on. An exception in the
    guard, the budget gate, the review lane, dispatch or the merge queue propagates: the caller counts it, and every
    step is idempotent, so the next pass simply retries.

    Review-lane policing runs BEFORE dispatch (2026-09-19), as in the blueprint's loop, which re-runs Gate 1
    for cards that entered review first. `kanban_dispatch` also claims cards waiting in `review` and spawns
    their reviewer, and the Gate 1 re-check only acts on cards still sitting in `review`, so with the old
    order (dispatch first) it was skipped for every card that dispatch reached first. Not closed: Hermes's
    own gateway dispatcher can still claim a review card between two of these passes; Gate 3 at merge time is
    the backstop for that."""
    summary = {
        "parked": [], "dispatch": {}, "sent_back": [], "merged": [], "unreviewed": [], "usage_sessions": 0,
        "integrity": [], "warnings": [], "recovery": [], "unparked": [], "provisioned": [], "stopped": False,
        "stop_reason": None, "final": None, "finished": False,
    }
    halted, why = _halted(conn, plan.project)
    if halted:
        summary["stopped"] = True
        summary["stop_reason"] = why
        return summary

    # The primary checkout first (ASES-GIT-12): it must still be on the integration branch, clean, and at the HEAD
    # ASES itself last wrote. Hermes, not ASES, spawns workers, so a snapshot around each spawn is not possible;
    # checking every pass bounds how long a stray write (the reviewer has file-write tools) can go unseen. A
    # violation stops the pass before anything is dispatched or merged: nothing is safe to build on until a human
    # has looked, and the polling loop reports it and halts (a deviation from "fails the card", because the writer
    # cannot be attributed to a card).
    guard = guards_mod.check_primary_checkout(
        repo, plan.integration_branch, guards_mod.expected_head(conn, plan.project),
    )
    if not guard.ok:
        problems = list(guard.problems)[:20]
        events.record(conn, "integrity_violation", {"problems": problems, "head": guard.head, "branch": guard.branch})
        summary["integrity"] = problems
        return summary

    summary["warnings"].extend(_isolated(
        conn, summary, "idle_worktrees", lambda: process_idle_worktrees(board, repo, plan, conn=conn), [],
    ))

    # Real usage into the ledger first (ASES-CAP-03), as the blueprint's loop does: the budget gate below is
    # only as honest as the ledger it reads. A failure here (one unreadable card, a Hermes hiccup) costs a
    # stale ledger for one pass, not the pass.
    def ingest() -> list:
        try:
            return usage_mod.ingest_run_usage(board, plan, project, models_config, conn=conn)
        except Exception as exc:  # noqa: BLE001 - recorded under its own name, then by _isolated as a step error
            events.record(conn, "usage_ingest_error", {"error": f"{type(exc).__name__}: {exc}"[:300]})
            raise

    summary["usage_sessions"] = len(_isolated(conn, summary, "usage", ingest, []))
    summary["recovery"] = _isolated(
        conn, summary, "recovery",
        lambda: process_recovery(board, repo, plan, project, models_config, conn=conn, now=now), [],
    )
    stopped, why = _isolated(
        conn, summary, "bounds",
        lambda: process_bounds(board, repo, plan, project, models_config, conn=conn, now=now), (False, None),
    )
    if stopped:
        summary["stopped"] = True
        summary["stop_reason"] = why
        return summary

    summary["parked"] = process_budget_gate(
        board, plan, models_config, conn=conn, budgets=project.budgets, project=project,
    )
    summary["unparked"] = _isolated(
        conn, summary, "unpark",
        lambda: process_unpark(board, plan, models_config, conn=conn, budgets=project.budgets, project=project), [],
    )
    summary["sent_back"] = process_review_lane(board, repo, plan, project, conn=conn)
    summary["dispatch"] = hermes_mod.kanban_dispatch(board)
    summary["provisioned"] = _isolated(
        conn, summary, "provision", lambda: process_provision(board, plan, conn=conn), [],
    )
    summary["merged"] = process_merge_queue(
        board, repo, plan, project, conn=conn, unreviewed=summary["unreviewed"], models_config=models_config,
        integrity=summary["integrity"],
    )
    if summary["integrity"]:
        # ASES-GIT-05: the merge queue's own post-merge check found the integration branch broken and could not
        # repair it with a revert. The same rule as the primary-checkout guard above: nothing later in the pass
        # (finalize, the finished check) is safe to run on top of a branch in that state.
        return summary
    final = _isolated(
        conn, summary, "finalize",
        lambda: process_finalize(board, repo, plan, project, models_config, conn=conn, now=now), None,
    )

    summary["final"] = final
    if final == "error":
        summary["warnings"].append("the final gates could not run: see the finalize_result event")
    state = bounds_mod.get_state(conn, plan.project)
    summary["finished"] = final == "finished" or (state is not None and state["status"] == "finished")
    # A stop or pause that landed during the pass (the kill switch, or a final gate that failed) is reported now, not
    # one pass late.
    halted, why = _halted(conn, plan.project)
    if halted:
        summary["stopped"] = True
        summary["stop_reason"] = why
    return summary
