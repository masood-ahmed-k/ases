# Round 10 smaller packages (read `r10_rules.md` first)

Three independent packages, each in its own worktree `C:\Users\masoo\ases-wt\<name>` on branch `r10/<name>`.

## GATEPIN (`gatepin`): pin the gate-config marker, ASES-QG-02

Requirement (p277): "Gate commands come from the approved plan's gate profiles and are pinned in controller config with a hash. A
diff that changes gate configuration, CI scripts or test runner settings needs an explicit plan task that allows it. [ASES-QG-02]"

Round 9 made a task's `allow_gate_config_changes` marker the ONLY thing that lets a diff change gate/CI/test-runner configuration
(CIPIN), and folded a task's `sandbox_network` exception into the gate-profile pin (GATESANDBOX: `plan.sandbox_network_exceptions`,
`gates.hash_gate_profiles`, `controller.pin_gate_profiles`/`verify_gate_pin`, both wired from `cli.py`'s `swarm approve` and
`swarm run` pre-flight). The marker is NOT pinned, so a `plan.json` edited after approval to set it goes unnoticed.
1. Fold every task's `allow_gate_config_changes` into the same pin. Prefer one generalised shape over a second parallel argument
   (for example one "pinned per-task fields" mapping), but keep the hash of a plan that sets neither field byte-identical to
   today's, so existing pins stay valid: prove that with a test. Wire it through every place that pins or verifies (grep for
   both functions; `cli.py` has two).
2. A test that `swarm run` refuses a plan whose marker was flipped after approval (model it on round 9's
   `test_run_refuses_when_a_tasks_sandbox_network_exception_changed_after_approval` in `tests/unit/test_cli_commands.py`), with a
   before/after proof.
3. The register notes that acceptance 22.12 never exercises `gate_config_changed` end to end. Add a NEW acceptance file (read
   `tests/acceptance/test_22_12_tampering.py` and `conftest.py` first; do not edit either) where a task whose touches include a
   gate-config path changes it: without the marker Gate 0 refuses the plan; with the marker the change passes Gate 1; and a
   task that changes a gate-config file inside its touches but whose plan was approved without the marker is caught.
Files you own: `src/ases/plan.py`, `src/ases/gates.py` (the hash function only), the pin functions in `src/ases/controller.py`, the
two pin call sites in `src/ases/cli.py`, their tests, the new acceptance file.

## BUDGETFIX (`budgetfix`): one reserve default, and a paused project's clock, ASES-CAP-03 and ASES-CTL-01

Requirements: ASES-CAP-03 (p133): "A card MUST NOT become ready unless the remaining budget covers its estimated cost plus a review
reserve. Otherwise the controller parks it with hermes kanban schedule until the reset time." Bounds table (section 9.3, p342):
"Requests per provider per day | The limit in section 5.3 minus a 10 percent reserve | Park cards until the reset", and
"Project wall-clock | Set at Gate P | Pause and report".
1. The daily reserve: `bounds.Bounds.daily_reserve_percent` defaults to 10 and `config/swarm.yaml` sets 10, but
   `policy.py` (around line 107) and `report.py` (around line 382) fall back to 0 when a project's budgets omit the key, so the
   budget gate, the Gate P estimate and the report can disagree about how much quota is usable. Make ONE definition of the default
   (the blueprint's 10) and use it everywhere the reserve is read (grep `daily_reserve_percent` and `reserve_percent` across
   `src/ases`, including `ledger.py` and `usage.py`). Test that a budgets block without the key reserves 10 percent in every
   reader, and that an explicit 0 still means 0.
2. The paused project's clock: `report._wall_clock` freezes the clock at `project_state.updated_at` for `finished`/`stopped` but
   keeps counting against now for `paused`. First establish what the wall-clock BOUND itself does (bounds.py: how `deadline_at`
   is set at start, whether a pause stops it, what `swarm resume` does with a deadline that has already passed: if a project
   paused by its wall-clock bound re-pauses on the very next pass after resume, with no way to extend the deadline, that is a
   real bug). Then make the report agree with the bound's own arithmetic, and if resume after a wall-clock pause is broken, fix
   it with the smallest honest change (for example `swarm resume` refusing with a clear message unless a new wall-clock limit is
   given). Quote what you found in the report with file:line. Tests for each behaviour.
Files you own: `src/ases/bounds.py`, `src/ases/policy.py`, `src/ases/report.py` (the reserve and wall-clock code only),
`src/ases/ledger.py`/`usage.py` only if they read the default, the `swarm resume` code in `src/ases/cli.py` only if item 2
needs it, and tests.

## CALLERS (`callers`): the smoke test and the residual risks get their callers, ASES-MOD-04 and ASES-ROL-05

Requirements: ASES-MOD-04 (p125): "Before first use, run one smoke test per model through the real Hermes path: a tiny
tool-calling task with a structured result. Record the result and the latency." ASES-ROL-05 (register): "The Reviewer is
independent: different model family and provider, no shared project memory", which profiles.RESIDUAL_RISKS documents Hermes
cannot fully enforce (the reviewer keeps the combined `file` toolset).
1. `models.record_smoke_test` has no production caller: today a smoke test can only be recorded by hand. Add a `swarm` command
   (fit it into `cmd_models` or a sibling; read `cli.py`) that runs ONE tiny tool-calling task with a structured result through
   the real Hermes path for a named provider/model, measures the latency, validates the structured result, and records pass/fail
   plus detail via `record_smoke_test`. Because it spends real quota it must refuse to run without an explicit
   `--spend-quota` flag, exactly like `swarm eval run --spend-quota` (read how evals.py gates that, and reuse its one-shot call
   and its scrubbed environment rather than writing a second subprocess path). Tests replace the call with a fake: pass,
   malformed result, timeout, refused without the flag. Never run it for real.
2. `profiles.residual_risks()` has no caller; its own docstring says it is for `swarm init` and `swarm doctor` "to print next to
   the plan". Print it in `swarm init`'s output and as INFO rows (never WARN or FAIL: they are known, accepted limits) in
   `swarm doctor`. Tests on the output.
Files you own: `src/ases/cli.py` (the new command and `swarm init`'s output only), `src/ases/models.py` if the recording needs a
small change, `src/ases/evals.py` only to expose an existing one-shot call for reuse, `src/ases/doctor.py` (the INFO rows only),
and tests. BASECHECK also adds one doctor check on its own branch.
