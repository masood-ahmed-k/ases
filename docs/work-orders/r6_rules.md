# Round 6 addendum to the shared rules (read `r2_rules.md`, then `r5_rules.md`, then this, then your package file)

Everything in `r2_rules.md` and `r5_rules.md` still applies (files you own, no commits, ASCII output, no em dash or section sign, the
Windows `os.kill` trap, the Write-tool `\uXXXX` decoding trap, write regex files with Write/Edit never a shell heredoc, tests through
the compressor, real Hermes facts). This addendum:

## The hard constraint this round: NEVER call a real provider or a real Hermes
The user said, in these words, "no need to test and burn the tokens from xkiro". Nothing you do may spend real provider quota or
touch the user's real Hermes installation, in ANY form:
- Never run `hermes` for real (no subprocess call to the real CLI, no import of the real Hermes package at runtime).
- Never run `swarm plan`, `swarm run`, `swarm critique`, or `swarm eval run --spend-quota` against real config.
- Every scenario you write uses `ases.fakes.board.FakeHermes` (installed over the `hermes` module with `monkeypatch`) and, where a
  scenario is about the model's OWN output, `ases.fakes.provider` (a local HTTP server, no network egress). Read both modules before
  you write anything: their docstrings and public methods are listed below.
- If a scenario in the blueprint genuinely cannot be closed-loop tested without a real provider or Docker (this happens: prompt
  injection through a real sandbox network block is one), build what the mechanism guarantees at the unit or policy level, say in the
  test's docstring exactly which part is NOT covered and why, and do not fake a pass. A `pytest.mark.skip(reason=...)` with the honest
  reason is correct there; never invent a shortcut that makes the assertion vacuous.
- `tests/unit/` already had fakes for gates, git and the database; nothing there changes. This round adds to `tests/acceptance/` only,
  unless your package file says otherwise.

## The acceptance rig (read the real files, this is a summary, not a substitute)
- `src/ases/fakes/board.py`: `FakeHermes(repo, *, board="ases-test", integration_branch="integration", now=None)`. Every public
  function of `hermes.py` exists on it with the same signature (`install(monkeypatch)` enforces this at import time). Controller-side
  calls are logged in `fake.calls`. Inspection: `card(id)`, `cards(status=None)`, `events(id, kind=None)`, `comments(id)`, `runs(id)`,
  `worktree(id)`, `live_workers()` (orphan detection for 22.7/22.13), `snapshot()`, `describe()`. Settings (plain attributes):
  `max_in_progress`, `max_in_progress_per_profile`, `claim_ttl_seconds`, `crash_grace_seconds`, `rate_limit_cooldown_seconds`,
  `failure_limit`, `review_dispatch`, `paused`/`cli_dispatch_honors_pause` (pause does NOT stop `kanban_dispatch` by default, matching
  real Hermes: only the gateway loop honours it), `gateway_dispatch`, `initial_block_event`, `default_author`. Failure injection:
  `fail_next(name, *, card_id=None, error=None, times=1)`, `fail_spawn(*, profile=None, card_id=None, error=..., times=1)`,
  `kill_worker(card_id, *, exit_code=137, signal=None)` (an out-of-band kill: the card stays `running` with a dead pid until the next
  reclaim). Time: `tick(seconds)` advances the clock, wakes sleeping workers, runs the reclaim phase, and (with `gateway_dispatch`)
  spawns. `register_worker(profile, worker_or_factory)`. Fake worker pids are far outside any real range, so a kill can never touch a
  real process.
- `src/ases/fakes/worker.py`: steps `Write`, `Append`, `Modify`, `Delete`, `Commit`, `Untracked`, `RequestReview`, `Complete`,
  `RequestChanges`, `Block`, `Crash`, `Timeout`, `Comment`, `Heartbeat`, `Sleep` (keeps a card `running` across passes until `tick`
  reaches it: needed for 22.5, 22.7, 22.13), `Do`. `ScriptedWorker(steps)`, `WorkerFactory`, `by_task_key({...}, default=None)`,
  `sequence(*workers)`, `write_files`, `write_and_commit`. Personas: `good_coder(files, message)`, `slow_coder`, `wrong_coder`,
  `tampering_coder(kind)` (the five section 22.12 kinds), `questioner(reason, *, then=None, kind="needs_input")`, `crasher(...)`,
  `touches_coder()` (writes whatever files the card's touches name), `reviewer_pass(...)`, `reviewer_changes(required, ...)`,
  `reviewer_wrong_commit(...)`.
- `src/ases/fakes/provider.py`: a scripted OpenAI-compatible HTTP server on localhost. `ScriptedResponse`, `delay_seconds`,
  `drop_connection`, `tool_call_response`, `slow_response`, `rate_limit(retry_after)`, `unauthorized()`, `malformed_json()`, a request
  log with credentials redacted, `assert_never_received(secrets)`, `hermes_endpoint_config()`. You need this only for a scenario that
  reads what was SENT to a model (22.10's "may not appear in any prompt captured by the fake provider"); most scenarios never start it.
- `tests/acceptance/conftest.py`: fixtures `world` (a fresh `World`: temp primary repo on `integration`, the two-task `DEFAULT_PLAN`
  published exactly as `swarm approve` publishes it, a temp ASES database, a `FakeHermes` installed, default personas for lead,
  coder-1 and reviewer), `world_factory` (build another `World` with your own `plan_raw`/`seed`/`budgets`/`models_config`),
  `one_task_plan` (a copy of the one-task plan), `create_cards`, `run_until`, `git`. `World` methods: `git(*args)`, `create_cards()`,
  `one_pass()`, `run_until(predicate, max_passes=40, step=20)`, `restart_controller()` (closes and reopens the database connection:
  this is what "stop the controller and confirm state survives" means here), `card(id)`, `work_card_id(task_key)` (the task's CURRENT
  work card, a fix card once one exists), `all_merge_cards_done()`. Read `tests/acceptance/test_scenarios_demo.py` for two full,
  working examples in this exact style before you write your own: it shows the assertions a real reviewer would expect (squash commit
  messages, reflog, ancestor checks, run metadata).

## Package boundaries this round
- Packages named `AC-*` own ONLY new files under `tests/acceptance/`. They must not edit any file under `src/`, and must not edit
  `tests/acceptance/conftest.py` (if you need a new fixture or plan shape, build it locally in your own file, or use `world_factory`
  with your own `plan_raw`; note in your report if `conftest.py` genuinely needs a new shared fixture, do not add it yourself).
- Packages named `CORE`, `TV`, `LED`, `FIX` own the `src/` files their package file lists, plus the matching `tests/unit/` files. They
  do not touch `tests/acceptance/`.
- The suite baseline is whatever it shows before you start (about 5,277 passed, 2 skipped at the time these orders were written); it
  must never go down. Full-suite runs take 6 to 8 minutes now: run your own new/changed test files while you work, and the full suite
  once near the end. If several agents run the full suite at once, a transient `FileNotFoundError` under pytest's shared temp base
  directory is another agent's run colliding with yours, not your bug (re-run before deciding).
- Quote requirement IDs from `C:\Users\masoo\ases-workspaces\tools\blueprint.txt` (grep for the id, read the paragraphs around it) and
  from `spec/requirements.yaml` (read your rows; the note says what is and is not done). Where they disagree, the blueprint wins.
