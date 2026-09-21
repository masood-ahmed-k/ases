# Package CL: the command line, version 2 (every command of blueprint section 9.1 that has a module behind it)

Files you own: `src/ases/cli.py`, `src/ases/doctor.py`, `src/ases/config.py`, `config/swarm.yaml` (small additions only, listed
below), `tests/unit/test_cli_run.py`, `tests/unit/test_doctor.py`, `tests/unit/test_config.py`, and NEW `tests/unit/test_cli_commands.py`.
Nothing else. Read `r2_rules.md`, `r5_rules.md` and `r5_contracts.md` first. Other builders this round own the modules you call:
questions.py and recovery.py (QF), controller.py (CT), mergeq.py and review.py (MR), finalgates.py, profiles.py, evals.py,
hardening.py. Import the ones that may not exist yet LAZILY inside the command that needs them, and test the command with a stub module
injected through `monkeypatch.setitem(sys.modules, ...)` or by patching the lazy-import helper you write (`_lazy("finalgates")`).

## Source of truth (blueprint.txt [p194] table 15, [p196] to [p201], [p349] to [p358], [p253] to [p269], section 22.2, 22.13, 22.14)
- Section 9.1: `cli.py` commands are `init, doctor, run, plan, approve, status, questions, answer, stop, resume, models, eval, report`.
- ASES-REC-05 (`swarm questions`, `swarm answer <card> "<text>"`), ASES-REC-06 (`swarm stop` within 30 seconds, `swarm resume` "after
  reconcile-on-start"), ASES-REC-04 (reconcile on start repairs what is safe and blocks the rest), ASES-OBS-01 (`swarm status`,
  `swarm report`), ASES-REV-02/03 (the critic, then "the user approves the plan, the request budget and the expected calendar time
  with swarm approve. No implementation card exists before that approval."), ASES-CTL-01 (project wall-clock "set at Gate P").
- Existing exit codes of `swarm run` must not change meaning: 0 finished, 1 iteration bound reached or a refusal, 2 five failed passes
  in a row, 3 primary-checkout guard. New: 4 the project is stopped or paused (say why), 5 reconcile found something it could not repair.

## Shared plumbing (write once, use everywhere)
- `_load_plan(repo, project)` (the Gate 0 load already done in cmd_run and cmd_approve), `_open_conn(project)`, `_lazy(name)` (import a
  sibling module on first use, so a missing optional module gives a one-line error, not a traceback), `_ascii(text)` (every line printed
  to the console goes through it: the Windows console is cp1252), `_reports_dir(project, kind)` =
  `<ases_home>/<kind>/<project name>/<UTC timestamp>` (never inside the repository).
- Every command that needs the plan takes `--repo`. `answer`, `stop` and `resume` do not require it: `answer` needs only the board;
  `stop` and `resume` accept `--repo` and, when it is missing, act on EVERY project found in `plan_tasks` (a small stand-in object with
  a `.project` attribute is all `killswitch.stop_all` needs; the kill switch must work "at all times", ASES-SEC section 21.3).

## Commands
1. `swarm questions --repo R`: `questions.list_questions` then `questions.format_questions`. Exit 0 (also when there are none).
2. `swarm answer <card> "<text>" [--author NAME]`: `questions.answer_question`; print the question that was answered and "answered";
   a `QuestionError` prints its message to stderr and exits 1 (never echo the answer text back).
3. `swarm status --repo R`: `report.build_report` then `report.render_status`. `swarm report --repo R [--out DIR] [--html]`:
   `report.render_text` to the console, and with `--out` (default `_reports_dir(project, "reports")`) `report.write_report`; print the
   two paths. Both are read-only.
4. `swarm critique --repo R --request "<original project request>" [--auto-replan] [--profile reviewer] [--timeout 900]`: computes
   `critic.plan_hash`, gathers the estimate text the approve screen shows (reuse the budget and calendar lines: factor them out of
   `cmd_approve` into `_estimate_lines(plan, project, models_config, conn)` and use the same function in both), calls
   `critic.run_critique`, records it with `critic.record_critique` (round number = `critic.critique_rounds_used + 1`), prints the
   status, summary and required changes. `critic.next_step` decides what happens next: `approve` prints "PASS: swarm approve may run";
   `replan` with `--auto-replan` runs the Lead again (`hermes -p lead -z <critic.lead_feedback_prompt(...)> -t file,terminal`, the same
   call shape as `cmd_plan`, factor that call out into `_run_lead(repo, prompt)` and share it), re-validates the plan (Gate 0) and
   critiques again, at most `budgets.replans_per_project` rounds in total; without `--auto-replan` it prints the feedback prompt and
   stops; `ask_user` prints why the user must decide. Exit 0 only for PASS, 1 otherwise.
5. `swarm approve`: after Gate 0 and the budget checks, require `critic.is_plan_approved_by_critic(conn, plan.project,
   critic.plan_hash(plan_path))` unless `--skip-critic` is given (then record a `critic_skipped` event and say so on the approval
   screen); show the critic's latest summary on the approval screen (ASES-REV-03); add `--deadline-minutes N` which stores the
   project wall-clock (`bounds.set_deadline`, "set at Gate P") and shows it on the screen; the create-cards call is unchanged.
   A `--yes` run still requires the critic (or `--skip-critic`).
6. `swarm run`: at start, in this order: Gate 0 load; gate pin check (as today); primary-checkout guard (as today, exit 3);
   `bounds.start_project(conn, plan.project)` (a `StateError` for a stopped or finished project prints why and exits 4; a `paused`
   project also exits 4 unless the operator ran `swarm resume`); REAL reconcile: `reconcile.reconcile(board, repo, plan, conn=conn,
   apply=True)`, print each repair and each blocked finding (ASCII), and when `report.blocked` is non-empty exit 5 unless
   `--ignore-reconcile` is given (print loudly that it was overridden). Then the loop as today with these additions: honour
   `summary["stopped"]` (print the reason, exit 4), print `warnings`, print `recovery` decisions and `unparked` when non-empty, and
   `summary["final"]` ("finished" ends the loop with exit 0; "gate_failed" prints the question and exits 4). Ctrl-C prints one line and
   exits 130 without a traceback. The line printed each pass keeps its current shape and gains the new counters only when non-zero.
7. `swarm stop [--repo R] [--reason TEXT]`: `killswitch.stop_all(...)` for the plan (or the stand-in), print a compact summary (paused,
   reclaimed count, killed, unverified, containers stopped, seconds, within_deadline), write the report with
   `killswitch.write_stop_report(report, _reports_dir(project, "stops"))` and print its path. Exit 0 when `within_deadline`, 1 otherwise.
   It must never raise: wrap the whole command and print a one-line error with exit 1.
8. `swarm resume [--repo R] [--extend-minutes N]`: `killswitch.resume_all(board, plan, conn=conn, reconcile=<callable>)` where the callable
   runs `reconcile.reconcile(..., apply=True)` and returns the report (blocked findings keep the system stopped and are printed). With
   `--extend-minutes` first call `bounds.set_deadline` to now + N minutes. After a successful resume, if `project_state.status` is
   `paused` set it back to `running` through `bounds.set_status`. Print what happened.
9. `swarm init [--apply] [--yes] [--global] [--sandbox] [--include-inactive] [--reuse-credentials-from PROFILE]`: `profiles.plan_init`
   prints the change list (one line each); without `--apply` that is all (dry run, exit 0, nothing written). With `--apply` it requires
   `--yes` (else refuse with an explanation and exit 1), calls `profiles.apply_init(..., confirmed=True)`, and prints the result. `--global`
   maps to `include_global` (the kanban limits, ASES-ARC-08) and is described in the help text as touching the user's whole Hermes.
10. `swarm eval ...`: everything after `eval` is passed to `evals.main(argv)` (use `argparse.REMAINDER`); the exit code is returned.
11. `swarm clean [--apply] --repo R` and `swarm retention [--days N] [--apply]`: `hardening.clean(...)` / `hardening.retention(...)` with
    their `format_*` functions; dry run by default. Read the hardening work order for the exact signatures.
12. `swarm doctor`: keep every existing check and add rows from `profiles.verify_state` (each problem one WARN row, none is a FAIL
    because a machine without the new profiles is still usable), and `sandbox.doctor_checks(policy, profile_dirs)` (rows are WARN when the
    sandbox is not enabled: the sandbox is off by default until the user starts Docker; a FAIL only when the config says it is enabled and
    a check fails). Replace the placeholder Docker check in `doctor.py` accordingly. `config/swarm.yaml` gains a documented
    `sandbox:` block (`enabled: false`, the Appendix B keys) and a `retention:` block (`logs_days: 30`, `reports_days: 90`); `config.py`
    validates both with safe defaults when absent, and `ProjectConfig` exposes them.
13. Remove from `_NOT_BUILT_YET` every command you built; the set may end up empty (delete it and `cmd_not_built_yet` if so).

## Tests (`tests/unit/test_cli_commands.py` for the new commands; update `test_cli_run.py` and `test_doctor.py`)
Every command through `cli.main([...])` with the modules faked: questions and answer (including the QuestionError path and that the
text is not echoed), status and report (files written outside the repo), critique (PASS, CHANGES_REQUIRED without and with
`--auto-replan`, the round limit, the plan-hash binding, exit codes), approve requiring the critic and `--skip-critic`, `--yes` still
requiring it, `--deadline-minutes`, run (start_project refusal exit 4, reconcile repairs printed, reconcile blocked exit 5 and the
override, stopped summary exit 4, final "finished" exit 0, "gate_failed" exit 4, Ctrl-C exit 130, the old exit codes 0 1 2 3 unchanged),
stop (never raises, exit codes, report path, no `--repo` acting on every project), resume (reconcile blocked keeps stopped, extend,
paused back to running), init (dry run writes nothing, apply without `--yes` refused, apply calls with `confirmed=True`), eval and clean
and retention dispatch, doctor rows. Everything printed is ASCII (assert it for a title with an accented letter). The existing tests
that patch `ases.reconcile.check` are updated to patch `reconcile.reconcile`.
