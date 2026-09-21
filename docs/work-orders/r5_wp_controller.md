# Package CT: the controller loop, version 2 (wire the phase 4 and 5 modules into `run_pass`)

Files you own: `src/ases/controller.py`, `tests/unit/test_controller.py`. Nothing else (you MAY add a new test file
`tests/unit/test_controller_loop.py` for the new steps if `test_controller.py` gets too large). Read `r2_rules.md`, `r5_rules.md` and
`r5_contracts.md` first. Other builders this round own: questions.py, recovery.py, report.py (QF); mergeq.py, review.py, usage.py,
gates.py (MR); cli.py, doctor.py, config.py (CLI); finalgates.py, profiles.py, evals.py, hardening.py, db.py, fakes/. Build against
the contracts in `r5_contracts.md`; test your calls into those modules with monkeypatches (use `raising=False` when a function may not
exist yet) and import `finalgates` lazily inside the function that uses it.

## The source of truth: blueprint section 9.2 and 9.3 (blueprint.txt lines around [p196] to [p201])
The loop, verbatim in spirit: `reconcile_on_start()`; `while not project_finished(): ingest usage; for each card that entered review:
re-run Gate 1 or request changes; for each card in triage: validate the follow-up; for each ready or todo card: pin the model and
reserve budget or park; for each card parked past its reset: unblock; process the next ready merge card; classify and act on failed
runs; check the active worktrees; if bounds are reached or the user stopped: pause and report`.
- ASES-CTL-01: "A project is finished when every merge card is done, Gates 4 and 5 are green on the integration HEAD, and the release
  report is written. It is stopped, not finished, when any global bound is reached."
- ASES-REC-06: "swarm stop ... stop the merge queue between steps". ASES-REC-01/02: classify failures, lineage budgets, escalation.
- ASES-REC-03/04: intent records before and completion records after every multi-step action.
- ASES-CAP-03: park what cannot be afforded; "Park cards until the reset" (table 17); the loop's `parked_past_reset -> unblock`.
- ASES-GIT-12 (idle worktrees), ASES-GIT-14 (`.env.ases`), ASES-TSK-04 (Gates 4 and 5 are controller lifecycle operations).
- Deliberately NOT built here (say so in the docstring and the report): the triage lane (ASES-LED-03, agent-proposed follow-up
  cards) and per-card model pinning (`policy.pin_model`; the profile config pins the model and recovery pins one only when it switches).

## 1. `create_cards_from_plan`
Pass `max_retries=project.budgets.get("attempts_per_card", 3)` to the work card create (Hermes gives up after 2 by default, the
blueprint says 3, and a mismatch stalls a card silently). Wrap the whole creation loop in `intents.intent(conn, plan.project,
intents.KIND_CREATE_CARDS, plan.project)` so a crash mid-way leaves an open intent for reconcile. Nothing else changes.

## 2. `process_merge_queue` (keep its signature; `unreviewed` and `models_config` stay optional)
1. Skip any task whose MERGE card has an open question (`questions.open_question(merge_card)` is not None): while the human has not
   answered, the queue must not re-run the merge and must not re-ask. (Today a merge card blocked for an exhausted fix budget is
   re-processed on every pass and the real Hermes refuses to block it again, which would raise on every pass.)
2. Everywhere the controller asks the user (the fix-card budget exhausted, and any new escalation you add) go through
   `questions.ask_user(board, card, text, conn=conn)` with the card fetched by `kanban_show`; never call `hermes.kanban_block` directly
   on a merge card. The text ends with a question ("How should this be resolved?") and contains the failure detail, redacted.
3. The fix card body: pass `outcome.detail` through `events.redact_text` before it is written (command output can contain a secret),
   and give the fix card `max_retries=project.budgets.get("attempts_per_card", 3)`.
4. Call `mergeq.merge_task(..., project=plan.project, should_stop=<callable>)` where the callable returns True when the project is
   halted (section 4 below). When the outcome has `stopped=True`: record `merge_stopped`, stop the whole queue for this pass, do NOT
   treat it as a failure (no fix card, no budget spent, no event `merge_failed`). Also check the halt flag before starting each task.
5. A pre-merge `BranchCheck` of kind `tamper_check_error`: record `tamper_check_error` and `continue` (retry next pass, never a
   failure and never a fix card). Kind `tamper` takes the ordinary failure path (fix card, bounded) with the findings as the detail.
6. Wrap each `kanban_complete` of a merge card in `intents.intent(conn, plan.project, intents.KIND_COMPLETE_MERGE_CARD, key)`.

## 3. New steps (each a module-level function with its own tests; each returns plain data for the summary)
- `process_recovery(board, repo, plan, project, models_config, *, conn, now=None) -> list[dict]`:
  (a) `recovery.refresh_review_rounds(board, plan, conn=conn)`; (b) `decisions = recovery.process_failures(board, plan, project,
  models_config, conn=conn, now=now)`; (c) for each decision whose action is `fresh_attempt` or `switch_model` call
  `_start_fresh_attempt`; for `replan` call `_request_replan`; (d) lineage budgets: for every plan task whose CURRENT work card is not
  done or archived, load its lineage, ask `recovery.exhausted(lineage, recovery.Bounds.from_budgets(project.budgets))`, and when a
  budget is spent apply `recovery.escalation(...)` (read its real signature): re-plan once (`_request_replan`) or ask the user
  (`ask_user`), deduplicated by `ask_user` returning "already_asked". Returns `[{"task_key", "action", "kind"}, ...]`.
- `_start_fresh_attempt(board, repo, plan, project, models_config, task, old_card_id, decision, *, conn) -> str | None`: the
  replacement card for a failed attempt (blueprint 19.2: "a fresh worktree at the current integration HEAD, attach the failure
  bundle"). Create a new work card with: title `"<key>: retry <n>"`, the same assignee and role, `workspace="worktree"`, branch
  `swarm/<key>-retry<n>`, the project id of the old card, body = `_work_card_body(task, reviewer_profile)` + a blank line + the text of
  `recovery.failure_bundle(old_card, criteria=task.acceptance, diff_text=<git diff of the old branch against the integration branch,
  empty when the branch does not exist>, gate_output=<detail of the latest gate_runs row for this task, redacted>,
  reviewer_findings=<the last three comments of the old card that start "CHANGES REQUESTED:" or "ANSWER:">)`, `parent` = the OLD
  card's own parents (its dependency merge cards, from `old_card["_parents"]`: read the real shape in `hermes.kanban_show`), and
  `max_retries` / `max_runtime` like the original. `<n>` = the number of `retry_card_created` events already recorded for this task
  plus one, and the idempotency key is `ases-retry-<project>-<key>-<n>`, so a crash between the create and the bookkeeping is safe to
  repeat. Then, in this order: link the new card as an extra parent of the task's merge card (`kanban_link(new, merge)`); ingest the
  old card's usage (`usage.ingest_card_usage`, guarded like the fix path); repoint `plan_tasks.work_card_id` at the new card in one
  UPDATE; archive the OLD card (`kanban_archive([old])`) so it stops showing as a blocked question; record `retry_card_created`. When
  `decision.action == "switch_model"` also pin the model on the new card: `hermes.kanban_set_model(board, new_id, decision.model,
  provider=decision.provider)`. Never raises: a failing Hermes call records `retry_card_error` and returns None (the next pass sees the
  same decision again because the run is not yet counted as handled: read `recovery.process_failures` to see how it de-duplicates and
  make sure a failure HERE does not lose the decision; if it does, say so in your report).
- `_request_replan(board, plan, project, task, card_id, decision, *, conn)`: `recovery.bump(conn, plan.project, task.key, "replans")`,
  `bounds.add_replan(conn, plan.project)`, then `ask_user` with a question that names the task, the failure kind and the last error
  (redacted, short) and says what the user can do: `swarm answer <card> "<guidance>"` retries the same card with the guidance. Record
  `replan_requested`. (The Lead re-plan call itself is NOT automated in this round: the docstring says so.)
- `process_unpark(board, plan, models_config, *, conn, budgets, project=None) -> list[str]`: for every `scheduled` card of this plan
  (`hermes.kanban_list(board, status="scheduled")`, scoped by `plan_tasks` like `process_budget_gate`), read the reason of its latest
  `scheduled` event from `kanban_show(...)["_events"]`; only cards whose reason starts with `budget:` or `review budget` were parked
  by this controller (never touch a card someone else scheduled). Recompute affordability with the SAME code `process_budget_gate`
  uses (factor a shared helper `_affordable_now(...) -> tuple[bool, str]` out of it) and, when it is affordable now, call
  `hermes.kanban_unblock(board, id, reason="budget available again")` and record `card_unparked`. Returns the task keys unparked.
- `process_bounds(board, repo, plan, project, models_config, *, conn, now=None) -> tuple[bool, str | None]`: `bounds.evaluate_bounds`,
  `bounds.stop_reasons`; when any project-stopping bound is breached call `pause_and_report` and return `(True, reason)`.
- `pause_and_report(board, repo, plan, project, models_config, reason, *, conn) -> str`: `hermes.pause(reason)`, then
  `bounds.set_status(conn, plan.project, "paused", reason)`, then `report.build_report` and `report.write_report` into
  `<ases_home>/reports/<project name>/<UTC timestamp>-paused/`; returns the directory. Every step is individually guarded (an event,
  never an exception), because it runs exactly when things are going wrong.
- `process_provision(board, plan, *, conn) -> list[str]`: `leases.provision_running_cards(board, conn, plan)` and
  `leases.sweep_finished(board, conn, plan, live_statuses=("running", "ready", "review", "scheduled", "todo", "blocked"))` (a blocked
  card keeps its lease while it waits for an answer).
- `process_idle_worktrees(board, repo, plan, *, conn) -> list[str]`: the running plan cards' `workspace_path` values go to
  `guards.check_idle_worktrees(conn, plan.project, repo, running_paths)`; the problems come back as WARNINGS (an event
  `idle_worktree_changed` each, never a halt: the first version has known false positives).
- `process_finalize(board, repo, plan, project, models_config, *, conn, now=None) -> str | None`: only when every merge card is done
  (`all_merge_cards_done`) and `bounds.get_state` is not already `finished`; lazy-import `finalgates` and call `finalgates.finalize(...)`;
  on status `gate_failed` call `pause_and_report` with `finalgates.final_gate_question(outcome)` as the reason; returns the status.

## 4. The halt check
`_halted(conn, project_name) -> tuple[bool, str | None]`: True when `bounds.get_state(conn, project_name)` has status `stopped` or
`paused` (the reason comes from `stop_reason`). One helper used by `run_pass`, the merge queue and `merge_task(should_stop=...)`.

## 5. `run_pass` version 2 (signature gains `now=None`; the order matters)
0. `_halted`: when halted return the summary with `stopped=True` and `stop_reason` and do NOTHING else (no guard, no dispatch, no merge).
1. Primary-checkout guard (as today; still the early return with `integrity`).
2. `process_idle_worktrees` (warnings).
3. Usage ingest (as today).
4. `process_recovery`.
5. `process_bounds`; when it stops the project return the summary with `stopped=True`.
6. `process_budget_gate`, then `process_unpark`.
7. `process_review_lane` (as today; the tamper check lives inside `review.py`).
8. `hermes.kanban_dispatch(board)`, then `process_provision`.
9. `process_merge_queue` (halt check inside).
10. `process_finalize`.
11. Return the summary of `r5_contracts.md`; `finished` is True only when `bounds.get_state` says `finished` (or `process_finalize` returned "finished").
Exceptions: steps 2, 3, 4, 5, 6 (unpark), 8 (provision) and 10 are NOT safety-critical for the pass: an exception in any of them is
recorded as a `pass_step_error` event naming the step and the pass continues (a stale ledger or a failed lease must not stop the merge
queue). An exception in the guard, the budget gate, the review lane, dispatch or the merge queue propagates as today.

## 6. Tests
Keep every existing test passing (update fakes where a new step now runs: give the old run_pass tests an autouse fixture that stubs
the new steps to no-ops, and test the new steps on their own). New tests: `create_cards_from_plan` passes `max_retries` and opens
and completes a create-cards intent; the merge queue skipping a merge card with an open question, using `ask_user` for the exhausted
budget, redacting the fix card body (plant a secret-shaped value in the detail), passing `max_retries` to the fix card, `stopped`
outcome handling, `tamper_check_error` retry without failure, `tamper` taking the fix path, the complete intent; `process_recovery`
(refresh, failures, a fresh attempt building the exact card, idempotency key stability, repoint, archive, link, usage ingest, the
model pin for `switch_model`, a failing hermes call, a replan asking the user and bumping counters, lineage-budget escalation
deduplicated); `process_unpark` (a budget-parked card unparked when affordable, one not parked by us untouched, still unaffordable
stays parked); `process_bounds` and `pause_and_report` (pause call, status set, report files written, each step guarded); provisioning
and sweeping; idle worktrees as warnings; finalize (not ready, finished, gate_failed pauses); `_halted`; the ORDER of steps in
`run_pass` (record the call order with monkeypatched steps); halted short-circuit; exception isolation per step; the summary always
has every key of the contract.

## Report back
The usual report, plus the answer to: does `recovery.process_failures` lose a decision when `_start_fresh_attempt` fails?
