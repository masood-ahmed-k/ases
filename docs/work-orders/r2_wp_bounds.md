# Package B: global bounds, project state, and the definition of finished

Files you own: `src/ases/bounds.py` (new), `tests/unit/test_bounds.py` (new). Nothing else. (Other packages this round own
recovery.py, reconcile.py, killswitch.py, questions.py, report.py, critic.py, intents.py: do not import them.)

## Requirements (quote the ids; read blueprint.txt around them)
- ASES-CTL-01, section 9.3: "A project is finished when every merge card is done, Gates 4 and 5 are green on the integration
  HEAD, and the release report is written. It is stopped, not finished, when any global bound is reached. Bounds are
  configuration with these defaults." Table 17 (search `Bound | Default | On reaching it`): attempts per card 3 (the card
  blocks; classify and escalate); review rounds per plan task 3 (escalate to the Lead, then to the user); fix cards per plan
  task 2 (escalate to the user); re-plans per project 2 (user decision); cards per project 40 (Gate 0 rejects larger plans);
  requests per provider per day (the limit minus a 10 percent reserve; park cards until the reset); wall-clock per card 45
  minutes (Hermes terminates and re-queues; counts as an attempt); project wall-clock (set at Gate P; pause and report).
- ASES-TSK-04, section 18.2: final integration security and smoke gates (Gates 4 and 5) are controller lifecycle
  operations. Another package (later) builds the gates themselves; you only provide the place where their results are
  recorded and consulted.
- ASES-REC-06 / section 19.6 (kill switch) and 19.5 use `project_state.status`: `planning`, `running`, `paused`, `stopped`,
  `finished`; the polling loop and the kill switch coordinate through it.

## Build `bounds.py`
1. `Bounds` frozen dataclass: attempts_per_card=3, review_rounds_per_task=3, fix_cards_per_task=2, replans_per_project=2,
   max_cards=40, card_runtime_minutes=45, daily_reserve_percent=10, project_wall_clock_minutes (int or None) and
   `Bounds.from_budgets(budgets: dict)` (unknown keys ignored, missing keys default, non-int values raise ValueError naming
   the key).
2. Project state helpers over the `project_state` table (schema in src/ases/db.py, do not edit it):
   `get_state(conn, project) -> dict | None`; `start_project(conn, project, *, deadline_minutes=None, now=None)` creates
   the row with status `running` and started_at now (if it already exists: leave started_at and any deadline alone, only
   set status running when it was planning/paused; a `stopped` or `finished` project is NOT restarted by this call, it
   raises `StateError`); `set_deadline(conn, project, deadline_at_iso)`; `set_status(conn, project, status, reason=None)`
   validating the status against the five values (ValueError otherwise) and recording stop_reason for `stopped`;
   `add_replan(conn, project) -> int` incrementing and returning project_state.replans (creates the row if missing);
   `stop_requested(conn, project) -> bool` (status is `stopped` or `paused`); all timestamps UTC isoformat seconds; an
   injectable `now` (datetime) for tests. `StateError(Exception)`.
3. `BoundStatus` frozen dataclass: name, subject (a task key, provider, card id, or "project"), used, limit, breached (bool),
   on_reach (the blueprint's response text, verbatim from the table).
4. `evaluate_bounds(board, plan, bounds, models_config, *, conn, now=None) -> list[BoundStatus]` computing, from the
   database and (only for the per-card wall clock) the board: cards per project (len(plan.tasks) counting both cards per
   task? NO: the bound is plan tasks, "Cards per project 40, Gate 0 rejects larger plans", so use len(plan.tasks) vs
   max_cards); fix cards per task (plan_tasks.fix_cards) vs fix_cards_per_task; review rounds per task and attempts per
   task (capability_failures) from the `lineage` table (missing row = 0); re-plans (project_state.replans) vs
   replans_per_project; requests per provider today (ledger.usage_today_for_provider) vs the limit minus daily_reserve_percent
   for each provider in models_config["providers"] that has a limit (skip providers with no cap); project wall clock: elapsed
   since project_state.started_at vs project_wall_clock_minutes or the deadline_at row (if neither, no status); per-card
   wall clock: for every plan task's current work card that is `running`, elapsed since its latest run's started_at vs
   card_runtime_minutes (use hermes.kanban_show; a failing show is skipped). `breached` means used >= limit.
5. `stop_reasons(statuses) -> list[BoundStatus]`: the breached statuses that STOP a project rather than escalate a task:
   project wall clock and re-plans (per the table: "Pause and report" and "User decision"). The others are escalations for the
   recovery package and are not stop reasons.
6. Finish gate: `record_final_gate(conn, project, gate, commit_sha, result)` writing a `final_gates` row for gate in
   ("gate4", "gate5") -- to avoid touching db.py store these in the existing `gate_runs` table with task_key "__final__" and
   the gate name, via a small helper that inserts exactly what gates.run_gate would insert (look at src/ases/gates.py for the
   columns), and `final_gates_green(conn, integration_head) -> bool` true only when there is a passing gate4 AND gate5 row
   for that exact commit. `mark_release_report(conn, project, path)` recording the report path as an event kind
   `release_report_written` and `release_report_written(conn, project) -> bool`. `is_finished(board, plan, integration_head,
   *, conn) -> bool`: every plan task's merge card is `done` (hermes.kanban_show) AND final_gates_green AND
   release_report_written; and `finish_project(...)` that sets status `finished` when is_finished is true and returns whether it
   did.

## Tests (`tests/unit/test_bounds.py`; temp DB; monkeypatch hermes.kanban_show)
from_budgets defaults, overrides, bad value; state transitions (start, restart refused when stopped/finished, deadline kept,
set_status validation, stop_reason stored only for stopped, add_replan from no row, stop_requested for each status);
evaluate_bounds: each bound below, at and above its limit (breached false/true/true), providers without a cap skipped, the
daily reserve arithmetic (limit 50, reserve 10 percent -> limit 45), a missing lineage row, a card running longer than
card_runtime_minutes, wall clock from started_at with an injected now and from a deadline; stop_reasons picks only the two
stopping bounds; final gates: green needs both gates on the exact commit, a pass for an older commit does not count, a fail
row does not count; is_finished false until all three conditions hold, true when they do; finish_project flips the status once
and is idempotent.
