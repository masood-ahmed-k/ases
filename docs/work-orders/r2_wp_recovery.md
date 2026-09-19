# Package R: failure classification, the two kinds of retry, lineage budgets

Files you own: `src/ases/recovery.py` (new), `tests/unit/test_recovery.py` (new). Nothing else. (Other packages in this round
own reconcile.py, bounds.py, killswitch.py, questions.py, report.py, critic.py and intents.py: do not import them, they may
not exist yet. You may import hermes, events, ledger, policy, plan, config, usage, gates.)

You may READ the installed Hermes source (read-only, never edit it, never run its commands that change state):
`C:\Users\masoo\AppData\Local\hermes\hermes-agent\hermes_cli\kanban_db.py` and `kanban_db_dispatch.py` show which run
`outcome` strings exist and how the dispatcher retries (search for `outcome=` and `failure_limit`), and
`agent\error_classifier.py` or similar files may show how provider errors are named. Ground your classification table in
what Hermes really writes, and list in your report every outcome string you found.

## Requirements (quote the ids; read blueprint.txt around them)
- ASES-REC-01, section 19.2: "An infrastructure failure says nothing about the model or the task: resume in the same
  worktree with the same model after a backoff. A capability failure means the attempt itself was wrong: start the next
  attempt from a fresh worktree at the current integration HEAD, attach the failure bundle (criteria, diff, gate output,
  reviewer findings) to the card, and on the second capability failure switch to the next model for that role class."
- ASES-REC-02, section 19.3: "Retry counters on single cards are not enough, because every fix card starts a new counter.
  ASES counts per plan task: review rounds, fix cards and total requests across the original card and everything it
  spawned. When a lineage budget runs out, the Lead may re-plan that task once with the full failure bundle. After that the
  controller blocks the task with a question for the user."
- Section 19.1, the whole failure table (blueprint.txt table 31): the detection and response for rate limit, daily quota,
  data policy mismatch, 5xx and timeout, auth 401/403, context too small, tool calling broken, worker crash or stale claim,
  runtime exceeded, worker edits outside its worktree, diff outside allowed paths, gates red, gate tampering, reviewer
  rejects, malformed plan or verdict, merge conflict or red Gate 3, post-merge failure, controller crash, budget or bound
  reached. Implement the rows that concern a worker RUN's outcome; the merge-queue and integrity rows are already handled
  elsewhere.
- Section 9.3 bounds table: attempts per card 3, review rounds per plan task 3, fix cards per plan task 2, re-plans per
  project 2 (config keys attempts_per_card, review_rounds_per_task, fix_cards_per_task, replans_per_project in
  `project.budgets`). "Escalate to the Lead where allowed, then a blocked card for the user."

## Build `recovery.py`
1. `FailureKind` (an Enum or a set of string constants, your choice, documented): `none` (a run that ended normally: completed,
   review_requested, changes_requested, or a deliberate block for a question), `rate_limit`, `quota`, `infrastructure`,
   `auth`, `policy`, `context`, `tool_calling`, `capability`, `runtime`, `unknown`.
2. `classify_run(run: dict) -> FailureKind`: from one run dict of `card["_runs"]` (fields: outcome, status, summary, error,
   metadata). Use the `outcome` first (Hermes's own vocabulary, for example spawn_failed, crashed, gave_up, timed_out or
   whatever you find in the source), then regexes over `error` and `summary` (case-insensitive): 429 or "rate limit" or
   "retry-after" -> rate_limit; "quota", "daily", "exceeded your current" -> quota; 500/502/503/504, "timeout",
   "timed out", "connection reset", "connection error", "service unavailable", "temporarily unavailable" -> infrastructure;
   401/403, "invalid api key", "authentication", "unauthorized", "forbidden" -> auth; "no endpoints found", "data policy",
   "data_collection" -> policy; "context length", "maximum context", "context window" -> context; "tool call", "tool_use",
   "malformed function", "invalid tool" -> tool_calling; the run outcome for a run that exceeded its runtime -> runtime;
   a gave_up or a crashed run with no recognisable text -> capability only when the crash count says so (see decide), else
   unknown. Document each pattern with the Hermes behaviour or blueprint row that motivates it. The function is pure.
3. `Lineage` dataclass (project, task_key, review_rounds, capability_failures, infra_failures, replans, fix_cards,
   requests) and `load_lineage(conn, project, task_key) -> Lineage` reading the `lineage` table (missing row = zeros),
   `plan_tasks.fix_cards`, and `usage.lineage_requests(conn, project, task_key)` (see src/ases/usage.py). `bump(conn,
   project, task_key, field, n=1)` upserts the lineage row incrementing one of review_rounds, capability_failures,
   infra_failures, replans (whitelist the field name; refuse anything else with ValueError) and sets updated_at (UTC
   isoformat seconds).
4. `refresh_review_rounds(board, plan, *, conn) -> dict[str, int]`: for each plan task read its CURRENT work card
   (plan_tasks.work_card_id), count the review events on it (`card["_events"]` entries whose kind is `changes_requested` or
   `review_reopened`), and compare with the lineage row's `seen_card`/`seen_events`: when `seen_card` differs from the
   current card id (a fix card took over) reset `seen_events` to 0 and set `seen_card`; add the difference
   (count - seen_events, never negative) to review_rounds and store the new seen_events. Returns {task_key: rounds added}.
   Idempotent: calling it twice adds nothing the second time.
5. `Bounds` dataclass with attempts_per_card, review_rounds_per_task, fix_cards_per_task, replans_per_project (ints) and
   `Bounds.from_budgets(budgets: dict)` using the defaults above for missing keys.
6. `exhausted(lineage: Lineage, bounds: Bounds) -> str | None`: the name of the FIRST lineage budget that has run out
   ("review_rounds" when review_rounds >= bounds.review_rounds_per_task, "fix_cards" when fix_cards >= fix_cards_per_task,
   "attempts" when capability_failures + infra_failures >= attempts_per_card * 2 is NOT the rule: use
   capability_failures >= attempts_per_card), else None.
7. `Decision` frozen dataclass (action, reason, backoff_seconds=0, model=None, provider=None) with `action` one of: `none`,
   `resume` (unblock the same card so Hermes re-runs it in the same worktree with the same model), `fresh_attempt` (new
   card from a fresh worktree with the failure bundle), `switch_model` (a fresh attempt pinned to the next model of the
   role class), `park` (schedule until the provider's reset), `replan` (the Lead may re-plan this task once), `block_for_user`
   (a question for the user), `mark_credential_unhealthy` (auth: record it, block for the user, never loop).
   `decide(kind, lineage, bounds, *, provider_reset_text="the next UTC midnight") -> Decision` implementing 19.1/19.2/19.3:
   rate_limit -> none (Hermes waits and retries; the ledger records it); quota -> park; infrastructure -> resume with an
   exponential backoff (30 s doubling per prior infra failure, capped at 900 s) until infra_failures >= attempts_per_card,
   then block_for_user; auth -> mark_credential_unhealthy; policy -> block_for_user (never relax the data class); context
   and tool_calling -> block_for_user with a reason saying the model must be rejected for agent roles; runtime and
   capability -> fresh_attempt on the first capability failure, switch_model on the second, and once `exhausted()` names a
   budget: replan when lineage.replans == 0, else block_for_user with a question; unknown -> block_for_user only after
   attempts_per_card consecutive unknowns, otherwise none (Hermes's own retry runs first). Every Decision carries a reason
   sentence a human can read.
8. `next_model(models_config, role_class, current_provider, current_model) -> tuple[str, str] | None`: the next
   candidate for a role class from `models_config["models"]`: rows whose role_class matches the pinned class OR
   `<class>_candidate` (for example coder and coder_candidate), a smoke test result not "fail" (rows carry no smoke result
   in the yaml; treat missing as acceptable), ordered pinned first then file order, skipping the current one, and skipping
   the ones the same provider has been marked unhealthy for (accept an optional `unhealthy: set[tuple[str,str]]`
   parameter). None when there is no other candidate.
9. `failure_bundle(card, *, criteria, diff_text="", gate_output="", reviewer_findings="") -> str`: the text attached to the
   next attempt: sections "Acceptance criteria", "What failed" (last failing run: outcome, error, summary, all passed
   through `events.redact`), "Diff so far" (truncate to 6000 chars with a marker), "Gate output" (truncate 3000),
   "Reviewer findings" (truncate 3000). ASCII-safe. Never include a secret-shaped value (redact with the same patterns
   `events._SECRET_VALUE_PATTERN` uses; use `events.redact({"t": text})["t"]`).
10. `process_failures(board, plan, project, models_config, *, conn, now=None) -> list[Decision]`: the loop step. For each
    plan task whose current work card is `blocked` and whose latest run ended with a failure classification other than
    `none` (look at the last run in `_runs` with an `outcome`; skip cards blocked with a `blocked` event that is a
    deliberate question from a worker, i.e. the latest run's outcome is `blocked` with a non-empty reason and no failed run
    after it), decide, and APPLY the decision through the hermes wrappers: resume -> `hermes.kanban_unblock` but only
    when `now` is past the last failed run's ended_at plus backoff_seconds (otherwise do nothing this pass; the caller
    polls again); park -> `hermes.kanban_schedule(board, card_id, reason)`; block_for_user -> `hermes.kanban_block(board,
    card_id, reason)` (reason phrased as a question) unless the card is already blocked with that same reason;
    mark_credential_unhealthy -> record the event and block_for_user; switch_model -> `hermes.kanban_set_model(board,
    card_id, model, provider=provider)` then `kanban_unblock`; fresh_attempt and replan are returned as decisions WITHOUT
    being applied (creating a replacement card and asking the Lead are the controller's job; the architect wires them).
    Each decision applied is recorded once with `events.record(conn, "recovery_decision", {...})` (task key, kind, action,
    reason) and each failure counted once: keep the failed run id in the event and do not re-count a run whose id already
    appears in a previous recovery_decision event for this task (query the events table). Bump capability_failures or
    infra_failures accordingly. One failing hermes call must not stop the other tasks (catch HermesCommandError, record a
    `recovery_error` event, continue).

## Tests (`tests/unit/test_recovery.py`; monkeypatch hermes; temp DB with plan_tasks rows)
classify_run: one test per FailureKind with realistic error strings, outcome-first precedence, unknown for empty text,
case-insensitivity, purity (input dict not mutated). Lineage: load with no rows, bump each field, refuse a bad field name,
refresh_review_rounds counting, idempotence, and the reset when the current card changes. exhausted: each budget and the
default bounds. decide: the full table (each kind, first vs second capability failure, backoff doubling and the 900 s cap,
infra escalation at attempts_per_card, exhausted lineage with replans 0 then 1, unknown patience). next_model: ordering,
skipping the current and unhealthy ones, the candidate class, None. failure_bundle: sections present, truncation markers,
redaction of a secret-shaped value, ASCII-safe. process_failures: resume only after the backoff (use `now`), park applies
schedule, block applies block with a question-shaped reason and does not re-block an already blocked identical reason,
switch_model applies set-model then unblock, fresh_attempt/replan returned but nothing applied, a deliberate worker
question is left alone, the same failed run is never counted twice across two calls, one failing hermes call does not stop
the others, counters bumped in the lineage table.
