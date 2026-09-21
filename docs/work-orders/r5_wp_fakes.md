# Package FK: the acceptance rig (an in-memory fake Hermes board, a scripted fake worker, a bigger fake provider)

Files you own: `src/ases/fakes/board.py` (new), `src/ases/fakes/worker.py` (new), `src/ases/fakes/provider.py` (extend, keep its
current API and tests), `tests/unit/test_fakes.py` (new), and the NEW directory `tests/acceptance/` (`__init__.py`, `conftest.py`,
`test_scenarios_demo.py`). Nothing else. Read `r2_rules.md`, `r5_rules.md` and `r5_contracts.md` first.

## Requirements (blueprint.txt [p396] to [p398] section 22.0 and [p282] section 14.4)
- ASES-TST-01 (14.4): "The controller has its own test suite that never touches a real provider: a fake OpenAI-compatible server returns
  scripted successes, 429 with Retry-After, 401, 500, timeouts and malformed JSON; a throwaway Hermes board (--board ases-test) and
  temporary Git repositories provide the rest. Free quota is far too small to debug a controller against live providers."
- ASES-TST-02 (22): "Tests 22.1 to 22.16 run against the fake provider and a test board unless stated otherwise, so they cost no quota and are
  repeatable." 22.0: "A fake OpenAI-compatible server with scripted responses, a --board ases-test board, throwaway Git repositories with seeded
  content, and a scripted fake worker that performs chosen file edits."
- The point of THIS package is to make acceptance scenarios 22.2, 22.3, 22.5, 22.6, 22.7, 22.8, 22.9, 22.10, 22.12, 22.13, 22.14, 22.15 and 22.16
  writable as fast, deterministic, zero-quota pytest tests that drive the REAL controller (`controller.run_pass`, `review`, `mergeq`,
  `recovery`, `reconcile`, `killswitch`, `questions`) against a fake Hermes. The scenarios themselves come in a later round; you build the
  rig and prove it with two demonstration scenarios.

## 1. `fakes/board.py`: `FakeHermes`
An in-memory, single-threaded, deterministic simulation of the parts of Hermes 0.21.3 that ASES uses, mirroring its REAL semantics. Read
`src/ases/hermes.py` (every public function is yours to implement), the tests that already build fake cards (`tests/unit/test_controller.py`,
`test_recovery.py`, `test_questions.py`, `test_report.py` show the dict shapes the modules expect), `usage.py` (which fields of a run it
reads to find the session id), and the Hermes source under `C:\Users\masoo\AppData\Local\hermes\hermes-agent\hermes_cli` (`kanban_db.py`,
`kanban_db_dispatch.py`, `kanban.py`; the facts in `r5_rules.md` came from there) to mirror the state machine.
- Statuses `triage, todo, scheduled, ready, running, blocked, review, done, archived`; `kanban_create` (idempotent by `idempotency_key`, `initial_status`,
  parents, `max_retries`, `max_runtime`, branch, workspace, project) with `todo` while a parent is unfinished else `ready`; links and the
  `recompute_ready` promotion (archived parents count as satisfied); `kanban_show` returning the flat task dict plus `_children`, `_parents`, `_runs`,
  `_events`, `_comments`, `_latest_summary` exactly as `hermes.kanban_show` does (runs: id, profile, status, outcome, summary, error, metadata,
  started_at, ended_at, worker_pid; events: kind, payload, created_at, run_id; comments: author, body, created_at); `kanban_list(status, assignee)`.
- Controller-side operations with the real rules: `kanban_block` (only from running or ready, else raise `hermes.HermesCommandError` AFTER adding the
  "BLOCKED: <reason>" comment; a `blocked` event with reason and kind; the second same-kind block after an unblock routes to `triage` with a
  `block_loop_detected` event, limit 2); `kanban_unblock` (blocked or scheduled to ready/todo/review, "UNBLOCK: <reason>" comment, `unblocked` event,
  resets `consecutive_failures`); `kanban_schedule` (with a `scheduled` event carrying `reason`); `kanban_promote`; `kanban_comment`; `kanban_complete`;
  `kanban_archive`; `kanban_reclaim`; `kanban_reopen_review` ("CHANGES REQUESTED: <reason>" comment, review to ready/todo); `kanban_request_changes`
  (only from an active review run); `kanban_set_model`; `kanban_link`; `kanban_init`; `pause`/`resume` (a `paused` flag that stops `kanban_dispatch`);
  `session_usage` (configurable per session id); `hermes_version`, `gateway_status`, `run_doctor` (healthy defaults, overridable).
- Agent-side operations (what a worker's kanban tools do), named `agent_request_review(card_id, summary=, metadata=, reviewer=)`, `agent_complete`,
  `agent_block(card_id, reason, kind=)`, `agent_comment`, `agent_heartbeat`, `agent_fail(card_id, error, outcome)` (crash, timeout, spawn failure:
  increments `consecutive_failures`, trips `gave_up` and `blocked` at `max_retries` or the default 2, writes the right run outcome and events).
- `kanban_dispatch(board, dry_run=False, max_spawns=None)`: in creation order, honouring `max_in_progress` (default 3, settable) and one card per
  profile: claim the card, open a run (profile = assignee, fake `worker_pid`), set `running`, create the worktree for `workspace="worktree"` with REAL git
  (`git worktree add -b <branch> <primary>/.worktrees/<card id> <integration branch>` in the primary checkout given to the constructor; the card's
  `workspace_path` and `branch_name` are set), then call the worker registered for that profile (below). A `review` card is dispatched to its
  reviewer profile. The return value has the same top-level shape the real `dispatch --json` prints (read what `controller.run_pass` and the tests
  store from it) with the spawned ids.
- A fake clock: `fake.now` (epoch seconds), `fake.tick(seconds)`; every event, comment and run timestamp uses it; a run past `max_runtime` is timed out by
  `tick`. Failure injection: `fake.fail_next("kanban_show", card_id=None, error=HermesCommandError(...))`, `fake.hang`-style not needed.
- `install(monkeypatch)` replaces every public function of the `hermes` module with the fake's (the two must have identical signatures: write a test that
  compares `inspect.signature` of every public function in `hermes.py` with the fake's, so a new wrapper added later fails loudly until the fake has it).
  Inspection helpers for assertions: `card(id)`, `cards(status=None)`, `events(id, kind=None)`, `comments(id)`, `runs(id)`, `snapshot()` (a plain, comparable
  copy of the whole board for idempotence tests), `calls` (a log of every controller-side call for order assertions).

## 2. `fakes/worker.py`: `ScriptedWorker` and personas
`ScriptedWorker(steps)` is a callable `(fake, card, run, workspace_path) -> None` that performs its steps in the card's REAL worktree and reports through the
fake's agent-side operations. Steps (small frozen dataclasses with builder helpers): `Write(path, text)`, `Delete(path)`, `Commit(message)`, `Untracked(path,
text)`, `RequestReview(summary, metadata=None, reviewer="reviewer")` (by default the metadata carries the hand-off `commit_sha` of HEAD, like the coder prompt
asks), `Complete(result, metadata)`, `Block(reason, kind="needs_input")`, `Crash(error, outcome="crashed")`, `Timeout()`, `Comment(text)`, `Heartbeat()`.
A worker may also be a plain function. Register with `fake.register_worker(profile, worker_or_factory)`; a factory receives the card and returns a worker so
one profile can behave differently per card (`by_task_key({"T1": ..., "T2": ...})` helper). Personas (functions returning a worker): `good_coder(files,
message)`, `slow_coder`, `wrong_coder` (violates an acceptance criterion), `tampering_coder(kind)` for the five section 22.12 attempts (delete a failing test,
add a skip marker, append `|| true` to a test command, edit a file outside the allowed paths, leave an untracked file), `questioner(reason)`,
`crasher(times, then=...)`, `reviewer_pass()` and `reviewer_changes(required)` (both write metadata in BOTH shapes the validator accepts: the blueprint
`review_status` shape and the Hermes skill `review_outcome` shape, with the reviewed commit named), and `reviewer_wrong_commit()`. Because the worker runs
inside `kanban_dispatch`, one `run_pass` advances every dispatched card synchronously: the scenarios are deterministic.

## 3. `fakes/provider.py` (extend; keep `ScriptedResponse`, the server and every existing test)
Add: `tool_call_response(name, arguments)` builders; a scripted slow response (`delay_seconds`) and a connection-drop response for timeouts; `rate_limit(retry_after)`,
`unauthorized()`, `server_error()`, `malformed_json()` conveniences; a `requests` log holding every request body received (headers with credentials redacted
in the log), and `assert_never_received(secrets)` to prove that no planted secret reached any prompt (section 22.10); `base_url` and a helper that returns a Hermes
custom-endpoint config fragment pointing at it (for later real-Hermes runs on an `ases-test` board; do not run Hermes).

## 4. `tests/acceptance/`
`conftest.py`: fixtures `world` (a temp primary repo on branch `integration` with an initial commit, `docs/ases/plan.json` written and committed, a temp ASES
database via `db.connect`, a programmatic `ProjectConfig` and `models_config` in the shape `tests/unit/test_controller.py` uses, a `FakeHermes` bound to the repo
and installed, personas registered for `lead`/`coder-1`/`reviewer`), `create_cards(world)` (calls the real `controller.create_cards_from_plan`),
`run_until(world, predicate, max_passes=40)` (calls the real `controller.run_pass`, advancing the fake clock, returns the summaries), and `git(world, *args)`.
`test_scenarios_demo.py` proves the rig with TWO scenarios against the CURRENT controller: (a) the section 22.2 core (two tasks, T2 depends on T1, a coder writes
files, the reviewer passes, both merge cards end `done`, one squash commit per task, the integration branch only moved by the merge queue, one card asks a
question that `questions.answer_question` unblocks); (b) the section 22.6 core (the reviewer returns changes once, the card goes back to its implementer, only the
corrected commit merges). If the current controller cannot pass one of them for a reason that is a genuine bug, do NOT bend the rig: mark that test
`xfail(strict=True, reason=...)` with the exact cause and describe it in your report.

## Tests (`tests/unit/test_fakes.py`)
The state machine for every rule above (each in both directions): the block rules (already blocked raises after the comment, second same-kind block goes to triage),
unblock resets the failure counter, `gave_up` at `max_retries`, idempotent create, promotion when parents finish and with archived parents, dispatch ordering
and the concurrency limit, real worktrees created from the integration branch, a scripted worker's edits landing in its own worktree only, crash outcomes, the
clock and timeouts, failure injection, `snapshot()` equality after a no-op, signature parity with `hermes.py`, the fake provider additions (tool call, delay, drop,
request log, `assert_never_received`).
