# Package AC-G: acceptance 22.14 (plan rejection), 22.15 (idempotent re-run), 22.16 (data class)

Files you own: `tests/acceptance/test_22_14_plan_rejection.py` (new), `tests/acceptance/test_22_15_idempotent.py` (new),
`tests/acceptance/test_22_16_data_class.py` (new). Nothing else. You may NOT edit any file under `src/`, and may NOT edit
`tests/acceptance/conftest.py`. Read `r2_rules.md`, `r5_rules.md`, `r6_rules.md` FIRST, then `tests/acceptance/test_scenarios_demo.py`
in full and copy its style. Also read `src/ases/critic.py` (its real `run_critique`/`next_step`/`is_plan_approved_by_critic` signatures
and its OWN test file `test_critic.py` for the exact fake-`invoke` pattern -- your critic scenario uses the SAME fake-invoke mechanism,
not a real Hermes call) and `src/ases/plan.py` (Gate 0's real error shapes) before writing 22.14.

## 22.14, the plan rejection test (blueprint.txt around `[p425]`/`[p426]`)
"A plan with a cycle, a missing criterion or a missing touches entry must fail Gate 0 with exact errors. The critic then returns
CHANGES_REQUIRED twice and the user rejects the plan. The approval screen shows the request budget and the calendar estimate. No
implementation card may exist at any point."
This scenario does NOT need `FakeHermes`/`world` for its Gate 0 half at all (Gate 0 is pure plan validation, no board involved) -- use
`plan.parse_and_validate` (or `load_plan_file`, read the real entry point) directly on three separate malformed plan dicts (a cycle: T1
depends_on T2, T2 depends_on T1; a task missing `acceptance`; a task missing `touches`) and assert each raises the plan module's own
error type with the RIGHT error text (read `plan.py`'s real exception/error-list shape; `PlanError.errors` per earlier context, confirm
it still matches). Then build ONE valid, publishable plan and drive the critic half with a FAKE `invoke` (copy `test_critic.py`'s
pattern exactly: a callable returning `(returncode, stdout, stderr)`) scripted to return CHANGES_REQUIRED twice in a row: call
`critic.run_critique(...)` (or whatever composes it with round-tracking; read `critic.py`'s real functions -- `record_critique`,
`critique_rounds_used`, `next_step`) twice, asserting `next_step` returns `"replan"` after the first (rounds_used < max_rounds) and
`"ask_user"` after the second (rounds_used >= max_rounds, i.e. the user must now decide). Then simulate "the user rejects the plan" by
simply NOT calling `controller.create_cards_from_plan` (there is no "reject" action to call -- rejection is the ABSENCE of approval),
and assert the final state: `critic.is_plan_approved_by_critic(conn, project, plan_hash)` is False, and (using a bare `FakeHermes` with
no cards created at all, or simply asserting nothing was ever created since `create_cards_from_plan` was never called) NO implementation
card exists anywhere. "The approval screen shows the request budget and the calendar estimate": this is `cli._estimate_lines`'s job
(round 5, package CL) -- you do not own `cli.py`, but you MAY call `cli._estimate_lines` directly in your test (it is a pure function
over a plan/project/models_config/conn, read its real signature) and assert it returns non-empty budget and calendar lines for this
plan, proving the data the approval screen needs is computable; do not try to test the CLI's actual printed output (that is
`test_cli_commands.py`'s job).

## 22.15, the idempotent re-run test (blueprint.txt around `[p427]`/`[p428]`)
"Run card creation twice from the same approved plan, and once more after deleting the ASES database. The board must contain each
card exactly once. A card proposed by an agent must stay in triage until it is validated."
Use `world`/`create_cards` from `conftest.py`. Call `world.create_cards()` TWICE in a row (same plan, same conn) and assert
`len(world.fake.cards())` is unchanged after the second call (idempotency keys, per `create_cards_from_plan`'s docstring) -- and that
the SAME card ids come back both times (`pairs` dict equal). Then simulate "once more after deleting the ASES database": close
`world.conn`, delete the sqlite file at `world.db_path` (and its `-wal`/`-shm` siblings if present -- read what `db.connect` leaves
behind), reconnect with `db.connect(world.db_path)` (a FRESH ASES database, but the SAME Hermes board, since only the ASES-side
database was deleted, not the fake board), assign it back onto `world.conn` (or build a new `World`-shaped call directly, whichever is
cleaner given `World` is a dataclass you may mutate in your own test, you do not own `conftest.py` but nothing stops you from
constructing values of its dataclass), and call `create_cards_from_plan` a THIRD time. Assert the board STILL has each card exactly
once (Hermes's own `idempotency_key` is the real source of truth here, not ASES's database -- this is the whole point of the test: the
blueprint deliberately deletes the ASES DB to prove idempotency does not secretly depend on it). Check whether `plan_tasks` rows in the
fresh database get correctly repopulated from the (unchanged) board state by this third call, or whether a gap exists here (if
`create_cards_from_plan` only WRITES `plan_tasks`, and does not also RECOVER `plan_tasks` rows from existing board cards when its own
row is missing, this third call might create the ASES-side bookkeeping fresh while correctly leaving the board alone -- assert whatever
the real code actually does, and if you find `plan_tasks` after the third call does NOT correctly reflect the (unchanged) work/merge
card ids for a task whose original `plan_tasks` row was in the deleted database, that is a genuine gap: report it clearly, do not paper
over it with a weaker assertion). "A card proposed by an agent must stay in triage until it is validated": if package LED's
`triage.py` exists by the time you write this (check `src/ases/triage.py`; if it is not there yet, or you are running before/without
it, SKIP this one sub-assertion with a clear `pytest.mark.skip(reason="waiting on package LED's triage.py")` rather than inventing your
own triage mechanism) -- create a card directly in triage (`hermes.kanban_create(..., initial_status="triage")`, matching whatever
package LED's report said about how a real proposed card actually lands there) and assert it is NOT promoted or archived by any number
of ordinary `run_pass` calls (nothing in the controller loop touches triage cards on its own), only by an explicit
`triage.promote_card`/`archive_card` call.

## 22.16, the data class test (blueprint.txt around `[p429]`/`[p430]`, table 32, section 21.2)
"Starting without a declared data class must fail. With class confidential and only remote providers configured, the run must refuse
to start. With class private, cards must never be routed to a provider marked as training on inputs, even when every other provider is
exhausted: they park instead."
Read `config.py`'s real `ProjectConfig`/`load_swarm_config` (does an undeclared `data_class` actually fail, or does it silently default
-- read `ASES-PRV-02`'s current status/note in `spec/requirements.yaml` first to know what is already built vs. not, quote it) and
`policy.py`'s real `check_data_class`/`DataPolicyViolation`. Build THREE `world_factory` worlds (or three plain calls to the relevant
functions without needing a full `world` at all, whichever the real functions need):
1. A `ProjectConfig` built with no `data_class` (or an explicitly empty one, matching whatever "undeclared" means in the real
   dataclass -- it may not even be constructible without one if it is a required field with no default; if so, that IS the pass,
   document it as such rather than forcing an artificial construction) -- assert `swarm run`'s own refusal (read `cli.cmd_run`'s real
   pre-flight checks, you do not own `cli.py` but MAY call its checking functions directly if they are separable, or reproduce the
   check via `config.py`'s own validation) rejects it before anything is dispatched.
2. `data_class="confidential"` with a `models_config["providers"]` containing ONLY remote (non-local) providers -- assert
   `policy.check_data_class` (called the way `cmd_approve`/Gate P calls it, read the real call site in `cli.py` for the exact
   arguments even though you cannot edit that file) raises/refuses for EVERY role's provider, so the plan cannot be approved (you may
   reproduce Gate P's OWN loop over `plan.tasks` calling `policy.check_data_class` per task, matching `cli.cmd_approve`'s real logic,
   since you only need to CALL the policy function, not the CLI command).
3. `data_class="private"` with a `models_config["providers"]` where the coder's PRIMARY provider is marked as training-on-inputs
   (read `policy.py`'s real "safe for private" allow-list / `_SAFE_FOR_PRIVATE`-style check to know the exact config shape that marks
   a provider unsafe) and a SECOND provider that is safe but has no daily budget left (its ledger usage already at its cap -- seed
   `requests_ledger` rows directly in `world.conn`, matching how `test_controller.py`'s budget tests seed usage). Drive
   `controller.process_budget_gate` (via `world.one_pass()`) and assert the coder's card is PARKED (`scheduled`, per the budget gate's
   real parking mechanism), NEVER dispatched to the training-on-inputs provider even though it is the only one with room left. If
   `process_budget_gate`/`policy.py` do not actually have logic to refuse a data-class-unsafe provider AT DISPATCH TIME (as opposed to
   only at Gate P, once, when the plan is approved) -- i.e. if the current code would happily dispatch to whatever provider `roles`
   names regardless of data class once past Gate P -- that is worth knowing precisely: read the real code path before asserting
   anything, and if the guarantee is ONLY enforced at Gate P (one-time, at plan approval) and not re-checked every pass, say so plainly
   in your report as a finding, and write the test against what the code ACTUALLY does (Gate P refusal), not an aspirational per-pass
   re-check that may not exist.

## Report back
The usual report, plus: for 22.15's third call (after deleting the database), exactly what you found about whether `plan_tasks` rows
are correctly recovered or not; for 22.16, exactly which layer (Gate P one-time check vs. a genuine per-pass re-check) actually
enforces the data-class guarantee today, quoting the real function and line.
