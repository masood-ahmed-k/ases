# Builder reports: what each package delivered, where it deviated, and what it noticed

Collected as each builder finished (2026-09-19). "Noticed" items are things the builder saw in code it did not own and did not
fix; they are the input to the wiring step. Nothing here has been run against a real Hermes yet.

## Package Q: questions (`src/ases/questions.py`, `tests/unit/test_questions.py`), done

Built: `QuestionError`, `Question`, `list_questions(board, plan, *, conn)`, `answer_question(board, card_id, text, *, conn,
author="user")`, `format_questions(questions, now=None)`. 98 test cases.

Deviations from the work order (all deliberate):
1. The secret scan feeds each answer line to `gates.scan_for_secrets` as an added diff line (`"+" + line`), because that function only
   reads lines starting with `+` and echoes the matched line; only line numbers are reported.
2. Card kind is decided merge, then fix (title contains `: fix (round`), then work, because `process_merge_queue` repoints
   `work_card_id` at the fix card and the literal order would label every live fix card "work".
3. A card that another project's `plan_tasks` rows hold, by its own id or by a parent, is never claimed by the title rule.
4. An unreadable card is skipped and a `question_read_failed` event is recorded so the skip is not silent.
5. After `kanban_show`, a card that is no longer blocked is skipped.
6. Only the latest `blocked` event counts: if it has an empty reason the card is skipped even when an older block had one.
7. A failing `kanban_show` inside `answer_question` propagates as `HermesCommandError`; the task key is looked up across all
   `plan_tasks` rows because `answer_question` has no plan.
8. `format_questions` also escapes control characters, since agent text could otherwise steer the terminal.

Noticed:
- `mergeq.merge_task` puts raw secret-scanner findings into the merge failure detail, and `process_merge_queue` writes
  `outcome.detail[:1500]` into the fix card body. The event copy is redacted, the card body copy is not (ASES-SEC-01). To fix in wiring.
- `process_merge_queue` handles merge cards in status `blocked` on every pass, so a merge card blocked for an exhausted fix budget is
  re-blocked each pass, which resets its age and undoes an answer while the merge still fails. To check and fix in wiring.
- Unknown: what event Hermes writes when its OWN dispatcher parks a card in `blocked`. If it is not a `blocked` event with a reason,
  those cards will not be listed and `swarm answer` will refuse them. Needs one live probe.
- `report._open_question` re-implements the same latest-block rule; a shared helper would stop the two drifting.
- The Write tool decodes `\uXXXX` escapes into real characters; scan every new file for accidental non-ASCII.

## Package B: bounds and project state (`src/ases/bounds.py`, `tests/unit/test_bounds.py`), done

Built: `Bounds` (8 fields, strict `from_budgets`), `StateError`, `STATUSES`, `get_state`, `start_project`, `set_deadline`,
`set_status`, `add_replan`, `stop_requested` (stopped or paused), `BoundStatus`, `evaluate_bounds`, `stop_reasons` (only the
re-plan and project wall-clock bounds stop a project), `record_final_gate`, `final_gates_green`, `mark_release_report`,
`release_report_written`, `is_finished`, `finish_project`. 160 test functions (349 cases).

Judgment calls:
- `finish_project` returns True only when THIS call flipped the status; it never overrides `stopped` or `paused` (the guard is inside
  the write statement).
- A `deadline_at` row wins over `project_wall_clock_minutes`; no `started_at` means no wall-clock status.
- `from_budgets` also rejects bools, negatives and a reserve above 100.
- `record_final_gate` redacts its `detail` (Gate 4 findings quote lines).
- `evaluate_bounds` keeps counting the wall clock for a stopped or finished project: skip evaluation in the loop once stopped.
- A failing `kanban_show` counts as "not measured" (bounds) or "not done" (`is_finished`).

Noticed (to settle in wiring):
- Two `Bounds` classes exist (`recovery.Bounds`, 4 fields, lenient; `bounds.Bounds`, 8 fields, strict): unify.
- Two `stop_requested` functions exist (`killswitch`: only `stopped`; `bounds`: stopped or paused): pick one meaning.
- `gate_runs` has no project column, so final-gate rows are keyed by commit SHA only; two projects cloned from one repo would share them.
- `gates.run_gate` stores raw command output in `gate_runs.detail` without redaction (`record_final_gate` does redact).
- `controller.all_merge_cards_done` is the old, weaker finish test; `is_finished` and `finish_project` replace it.
- `evaluate_bounds` costs one `kanban_show` per task per pass (up to 40 subprocess calls).
- `ledger._today()` reads the real clock, so `now` does not reach the provider-request bound.
- `plan_tasks.attempts` and `plan_tasks.review_rounds` are never written; the lineage table is the source of truth.

## Package C: plan critic (`src/ases/critic.py`, `prompts/critic.md`, `tests/unit/test_critic.py`), done

Built: `PlanCritique`, `CritiqueRound`, `plan_hash`, `gather_repo_facts` (extra: repository facts for the prompt from the filesystem
only), `load_template`, `build_critique_prompt` (single-pass `<<NAME>>` placeholders, redaction then truncation with a
`[truncated N characters]` marker), `parse_critique` (never raises, including 50000-deep nesting), `default_invoke` (never raises,
refuses a prompt over 32000 characters on Windows), `run_critique` (one repair call for an invalid reply, none for a nonzero exit),
`record_critique`, `critique_rounds_used`, `latest_critique`, `is_plan_approved_by_critic`, `next_step`, `lead_feedback_prompt`.
122 test functions (225 cases), including the section 22.14 scenario.

Deviations:
- One-shot reviewer call, not a critique CARD (blueprint 13.1 says the controller "creates a critique card"): only `run_critique` and
  `default_invoke` assume one-shot, so a card path could reuse the rest.
- A quoted `commit` may be case-insensitive or a prefix of at least 12 characters; every accepted verdict is returned with the full
  computed plan hash, even when the critic omitted `commit`.
- Events carry `valid`; `is_plan_approved_by_critic` and `critique_rounds_used` require it (a malformed critique can still parse as PASS).
- `estimate_text` is capped at 2000 characters (not in the work order). `null` for a list field is a problem.

Noticed (to settle in wiring):
- `gate_tampering_suspected: true` on a PASS is ignored by `next_step`; consider showing it on the approval screen or routing to `ask_user`.
- `next_step` needs `critique_rounds_used` read BEFORE `record_critique` for the current round.
- Wire `max_rounds` from `budgets.replans_per_project` (default 2). The register note for ASES-REV-02 says there is no DB column for
  re-plans; `project_state.replans` exists since schema v5.
- `swarm approve` must require `is_plan_approved_by_critic(conn, project, plan_hash(plan_path))`; a rewritten plan changes the hash and
  voids the earlier PASS. Pass the reviewer profile name as `profile`.
- The Lead's re-plan call also passes its prompt as one argv entry, so the same 32K Windows command-line limit applies and the
  architect's own wrapper around `lead_feedback_prompt` is not capped yet.
- pytest trap: a parametrized test with a 50000-character string overflows Windows' 32767-character environment limit through the test id.

## Package K: kill switch (`src/ases/killswitch.py`, `tests/unit/test_killswitch.py`), done

Built: `StopReport` (+ `to_dict`), `request_stop`, `stop_requested` (only `stopped`), `clear_stop`, `stop_all` (flag first, pause, list
this plan's running and review-with-live-run cards, reclaim, kill verified workers, stop the plan's containers; never raises; writes
nothing but the flag), `write_stop_report` (`stop-<UTC>.json`, numeric suffix instead of overwriting), `resume_all`, and the real
helpers `pid_alive`, `terminate_tree`, `process_command_line`, `default_list_containers`, `default_stop_container`. 130 test functions
(204 cases); an autouse fixture fails any test that reaches a real process, Docker, Hermes or `os.kill`.

Checked for real, outside the suite, against two throwaway processes: `pid_alive`, `process_command_line`, `terminate_tree` (the
grandchild died with the worker, a decoy without the card id survived, the test process survived). NOT measured: the 30 second limit
against a real Hermes (a stop with 3 running cards makes about 6 sequential Hermes calls), anything against a real board or Docker, and
the Linux code paths (fakes only).

Deviations:
- Parameter defaults are `None`, resolved at call time, so a CLI test that monkeypatches `hermes.pause` cannot hit the real Hermes.
- `resume_all` order: reconcile, then `resume()`, then clear the flag; a failed resume leaves the system consistently stopped.
- A fix card is a card whose parent is a plan card AND whose branch starts with `swarm/`.
- Worker pids are read BEFORE the reclaim (reclaiming ends the run).
- Time box: each outside call on a daemon thread gets at most a third of the deadline; Hermes calls together at most two thirds;
  `within_deadline` is False when any step was cut short; pids not reached are listed as unverified, not killed.
- The Docker filter matches names OR labels (`{{.Names}}|{{.Labels}}`); listings and per-card `show` calls run concurrently (cap 8).
- `stop_all` has an extra optional `reason`; it only reads `plan.project`, so the CLI can pass a stand-in when the plan file is missing.

Noticed (to settle in wiring):
- `bounds.stop_requested` is True for stopped OR paused; `killswitch.stop_requested` only for stopped. Pick one deliberately.
- Nothing interrupts a merge or gate step already running inside `swarm run`; the flag only stops it BETWEEN steps. Test 22.13
  ("no merge step running after 30 seconds") needs the gate runner to check the flag or be killable.
- Injected callables run on helper threads: they must not use the caller's sqlite connection.
- `swarm stop` run from inside a worker's own shell would have that worker as grandparent and `taskkill /T` would end the caller.
- The register note for ASES-REC-06, the `cmd_stop`/`cmd_resume` docstrings and `docs/architecture.md` still say the kill switch does
  NOT kill workers; stale once the CLI is rewired.
- `request_stop` on a `finished` project turns it into `stopped` (heals after a resume via the finish check).
- The module is about 1000 lines, mostly docstrings: trim later if wanted.

## Package R: recovery (`src/ases/recovery.py`, `tests/unit/test_recovery.py`), done

Built: `FailureKind` (string enum), `classify_run` (pure; outcome first, then ordered text rules), `Lineage`/`load_lineage`/`bump`,
`refresh_review_rounds` (idempotent, resets when a fix card takes over), `Bounds`/`from_budgets`/`exhausted`, `Decision`/`decide`/
`ACTION_*` (the whole 19.1 to 19.3 table), `escalation`, `backoff_seconds` (30 s doubling, cap 900 s), `next_model`,
`unhealthy_credentials`, `failure_bundle` (five sections, truncated, redacted, ASCII), `process_failures`. 148 test functions
(282 cases), 100 percent line coverage by the stdlib `trace` module (not a mutation tool).

Hermes outcome strings really found in `kanban_db.py` / `kanban_db_dispatch.py`: `completed`, `review_requested`, `changes_requested`,
`blocked`, `scheduled`, `reclaimed`, `timed_out`, `stale`, `crashed`, `rate_limited`, `gave_up`, `spawn_failed`.

Deviations and interpretations:
- Blueprint over work order on crashes: Hermes's own crash text ("exited with code", "killed by signal", "not alive", "stale_lock=") is
  infrastructure (19.1); a crashed or gave_up run with NO text is unknown; "exited cleanly without a terminal kanban call" is capability.
- Quota, policy and context text is checked BEFORE a bare 429 (the real xKiro/UnoRouter "429: ... tokens per day (TPD)" is a daily quota).
- `decide` expects a lineage that ALREADY includes the failure being decided: bump first, then decide.
- `Decision` carries extra fields (`task_key`, `card_id`, `run_id`, `failure_kind`); `decide` takes a `consecutive_unknown` keyword.
- `switch_model` is applied in place on the same card; a fresh worktree only comes via `fresh_attempt` (the controller applies it).
- The project-wide re-plan cap is `SUM(lineage.replans)`; the controller must call `bump(..., "replans")` when it really re-plans.
- A `recovery_switch_target` event remembers a chosen model so a failed unblock cannot flip back to the model that just failed.
- Not built: a policy failure does not mark the provider unavailable for later switches; the quota "fallback chain" is not modelled;
  requests are loaded into `Lineage` but not bounded (no requests bound exists in 9.3).

Noticed (to settle in wiring):
- **`create_cards_from_plan` never passes `max_retries`**, so Hermes trips at its default of 2, not the blueprint's 3. An unknown or
  rate-limit-classified failure on a blocked card then shows a streak of 2 (below `attempts_per_card`), `decide` returns `none`, and
  nothing ever unblocks it: a silent stall. Pass `max_retries=attempts_per_card` when creating cards.
- Real Hermes refuses `block` on an already-blocked card (`block_task` accepts only `running` or `ready`), but the CLI adds the
  "BLOCKED: <reason>" comment first and then exits 1; `process_failures` treats the comment as "already asked" so it converges (read
  from source, not run).
- Cards `process_failures` never sees: a `ready` card whose `last_failure_error` looks like quota or auth is held by Hermes's respawn
  guard (`blocker_auth`) forever; a repeated same-kind block routes the card to `triage`.
- `unblock` resets Hermes's `consecutive_failures` to 0, so each `resume` buys a fresh Hermes retry budget.
- Crash window: `fresh_attempt` and `replan` decisions are returned once; if the controller dies before acting they are not returned
  again (recorded as decided, `applied=false`). An outer savepoint (writes nest) would cover it.
- `events.redact` blanks any payload KEY containing "credential", "key" or "token": avoid such keys in event payloads.

## Package LS: resource leases and worktree guard (`src/ases/leases.py`, `src/ases/guards.py`, `tests/unit/test_leases.py`, `tests/unit/test_guards.py`), done

Built: `leases.py` with `CardEnv`, `LeaseError`, `ResourceBusy`, `is_port_free`, `allocate_card_env` (idempotent, lowest free
`port-block:<n>`, race-safe through the partial unique index, derived compose project, database name and temp dir capped at 63
characters), `release_card_resources`, `acquire_singleton`, `release_singleton`, `holders`, `sweep`, `write_env_file`,
`provision_running_cards`, `sweep_finished`; and in `guards.py` `WorktreeInfo`, `list_worktrees`, `snapshot_worktree`,
`check_idle_worktrees`, `refresh_snapshots` (the existing primary-checkout code is unchanged). 159 new test functions (214 items),
including the section 22.5 property (three cards, three port blocks and compose projects, a fourth stays queued).

Deviations:
- Injectable defaults are `None` resolved at call time (a bound-at-import default would let a monkeypatched `hermes.kanban_list` silently
  reach the real Hermes). Extra keywords: `sweep_finished(live_statuses=, now=)`, `release_singleton(now=)`,
  `snapshot_worktree(ignore_prefixes=)`, `refresh_snapshots(ignore_prefixes=)`.
- On a git failure `check_idle_worktrees` returns ONE problem and keeps every stored baseline.
- `snapshot_after_stop` (named in the work order, never defined) is implicit: a running card's snapshot row is deleted, so the first
  idle look after it stops takes a new baseline.
- The real-symlink test cannot run on this account (simulated instead, one skip).
- NOT verified: the `workspace_path` field name of a card dict; confirm against one live card.

Noticed (to settle in wiring):
- The exclude file is REPO-WIDE: in a linked worktree `git rev-parse --git-path info/exclude` returns the shared
  `<primary>/.git/info/exclude`, so the `.env.ases` line applies to every worktree and is the one file written outside the worktree and
  temp dir (contradicts the work order's "never writes outside the worktree except the temp dir"; harmless but worth knowing).
- `sweep_finished` releases a BLOCKED card's lease by default; on resume the card keeps an old `.env.ases` whose port block may have
  gone to another card. Pass `live_statuses` with `"blocked"` added.
- A card re-dispatched into the same worktree (a review send-back) whose run finishes between two polls looks like a change in an idle
  worktree: the controller must include such worktrees in `running_paths` or call `refresh_snapshots` after a dispatch. A reviewer that
  runs tests and leaves untracked files behaves the same way.
- Port blocks are per project: two projects sharing one machine are both handed block 0; only the bind probe of the first port guards a
  real collision.
- `provision_running_cards` uses `plan` only for the project name (it reads `plan_tasks` by project).
- `test_guards.py` went from about 4 s to about 35 s (real git worktrees on Windows).

## Package S: status and report (`src/ases/report.py`, `tests/unit/test_report.py`), done

Built: `build_report` (seven panels: project, budget, cards, quality, health, events, models; read-only, JSON-serialisable, redacted),
`render_status`, `render_text`, `render_html` (self-contained: no script, no link, no external resource, banner, CSP meta, everything
escaped), `write_report`, and the constants `HEALTH_KINDS`, `FINAL_GATE_KEY`, `HERMES_DASHBOARD_URL`. 96 test functions (181 cases);
every statement executed by the tests (stdlib `trace`). Smoke-tested against a COPY of the real `data/ases.db` with Hermes faked as
unreachable: rendered cleanly, original untouched, copy deleted.

Decisions:
- `cards.counts` covers work cards only; merge cards are reported as `merge_queue` (done over total). Open questions are counted over
  both kinds, following `questions._latest_block` so status and `swarm questions` agree.
- Bounds follow `bounds.evaluate_bounds` (capability failures limited by `attempts_per_card`, infra failures unlimited, a `deadline_at`
  row wins, the wall clock freezes at `updated_at` for a finished or stopped project).
- A missing `daily_reserve_percent` reads 0 here and in `policy.check_budget`, but `bounds.Bounds` defaults it to 10: they disagree when
  the key is absent.
- Gate runs are scoped by the plan's task keys plus `__final__`; gate output text is never included.
- Extra fields: provider `status`, `next_reset`, `day`, `parked_error`, `by_model` token sums, `cards.questions`, `health.note`,
  verdict `tamper_suspected`, `context_ok` on models.

Noticed (to settle in wiring):
- `events.redact` wipes the value of any key containing "token" (so token counts are named `tok_in` and `tok_out`).
- `gate_runs`, `merge_records` and `events` have no project column: two projects reusing keys like `T1` in one database would mix.
- `swarm status` costs two `kanban_show` subprocess calls per task, each with a 30 s timeout and no circuit breaker: a wedged Hermes
  could stall it for minutes.
- Nothing records a tamper event today (`gates.detect_tamper` had no caller); the tamper package now provides one to wire.
- `cli._NOT_BUILT_YET` still lists `status` and `report`. Wiring: `print(render_status(build_report(project.board, plan, project,
  models_config, conn)))`; for `swarm report`, `render_text` plus `write_report` into a directory OUTSIDE the repo.
- `gate1_recheck_failed`, `question_read_failed` and `recovery_error` are arguably health signals too (only the eight in the work order
  are listed).
- Old `cards_created` rows in the real database show `task_key` as `[redacted]` (they predate the `events.redact` fix).
- `spec/requirements.yaml` ASES-OBS-01 and the not-built list need updating once wired.

## Package Rc: reconcile repairs and intents (`src/ases/intents.py`, `src/ases/reconcile.py`, `tests/unit/test_intents.py`, `tests/unit/test_reconcile.py`), done

Built: `intents.py` (`KIND_*` constants for the six section 19.4 kinds, `begin`, `complete`, `open_intents`, `intent` context manager,
`mark_recovered`; details redacted). `reconcile.py` extended: `Repair`, `ReconcileReport` (findings, repairs, blocked, `clean`),
`pid_alive` (ctypes on Windows, never `os.kill` there; errs towards "alive"), `terminate_tree` (refuses pids below 5, this process and its
parent, never the controller's own process group), `process_command_line`, `worker_pid`, and `reconcile(board, repo, plan, *, conn,
apply=True, alive=, killer=, command_line=)` doing steps a to h plus open intents; idempotent; every applied repair logged once as a
`reconcile_repair` event; `apply=False` writes nothing. `check()` and `Inconsistency` unchanged (the 7 original tests pass). 185 new
test cases (170 in `test_reconcile.py`, 15 in `test_intents.py`), including the three crash points of section 22.7 and the real
`mergeq.merge_task` writer. The builder ran 8 hand mutations against a scratch copy; each broke a real test. Manually confirmed the
Windows command-line helper reads this process's own command line in 0.4 s.

Deviations:
- `reconcile()` does not call `check()`; it runs the same per-task body that `check()` now shares, so cards are fetched once and one
  failing call cannot abort other tasks' findings.
- The orphan sweep skips a `review` card whose latest run is still open (a reviewer worker may legitimately hold it); the command line
  must name the card as a whole token (`t_1` does not match `t_12`).
- Step c completes a merge card only from `blocked`, `ready` or `todo` (the states `process_merge_queue` itself completes from).
- Recovering a commit that git shows was reverted is blocked; step d also covers "no record, work card done, commit landed" (database lost).
- Extra kinds: `intent_recovered`, `revert_recorded`, `revert_unfinished` (blocked), and an open `create_cards` intent closes only when
  every task has both cards, else it is blocked with "re-run swarm approve (idempotent)".
- `candidate_discarded` is a repair only (never blocks); `orphan_worktree` is a finding but not blocked; `missing_worktree` and
  `reconcile_error` are blocked. A finding tied to no task has task key `"*"`.
- Finding kinds added: `merge_record_without_done_card`, `merge_unfinished`, `worker_gone`, `running_without_pid`, `orphan_worker`,
  `revert_unfinished`, `revert_unrecorded`. Repair kinds: `merge_record_recovered`, `merge_record_noop`, `merge_card_completed`,
  `worker_gone_reclaimed`, `orphan_worker_terminated`, `intent_recovered`, `revert_recorded`.

Noticed (to settle in wiring):
- `tests/unit/test_cli_run.py` line 24 patches `ases.reconcile.check`; once `cmd_run` calls `reconcile.reconcile` that patch no longer
  covers it and the real hermes would be called: patch `reconcile.reconcile` instead.
- Nothing writes intent records yet: the controller and `mergeq` never call `begin` or `complete`, so reconcile sees no open intents
  until they are wired (create cards, run gate, build candidate, fast-forward, complete merge card, revert).
- `merge_records` has no project column (keyed by `task_key` only).
- LATENT BUG in `mergeq.merge_task`: the candidate upsert never resets `reverted`, `squash_commit` or `completed_at`, so after a revert,
  a fix card and a second merge, `reverted` stays 1 and `check()` would report `done_but_reverted` forever (`revert_merge` is not called
  from the controller yet, so it has not bitten).
- A SIGKILL during a candidate build leaves the throwaway candidate worktree (`ases-merge-*` in the system temp dir) registered in git;
  reconcile ignores it, so `git worktree prune` is needed later (hardening).
- Only worker pids are checked for liveness, not names: a recycled pid on a running card looks alive (the safe direction; Hermes's own
  stale-claim reclaim covers it).
- `killswitch.resume_all` refuses to resume while `report.blocked` is non-empty, so what reconcile puts in `blocked` directly controls
  resume.

## Package SB: worker sandbox policy (`src/ases/sandbox.py`, `tests/unit/test_sandbox.py`), done

Built: `SandboxPolicy` (frozen, validated, `from_config` reads the Appendix B `sandbox:` block) and `SandboxConfigError`;
`terminal_block`, `check_terminal_block`, `check_profile_config`, `load_profile_config`; `sensitive_host_paths`,
`sensitive_name_patterns`, `is_sensitive_path`, `mount_problems`; `sensitive_files_in`, `mask_args`; `docker_run_argv`
(`--network none`, `--pull never`, `--mount type=bind` everywhere, only the worktree and masks mounted, no inherited env);
`docker_available`, `image_present`, `default_runner`, `pull_command` (returns argv only, never pulls), `KeyVisibilityResult`,
`key_visibility_test`, `exfiltration_probe`, `doctor_checks`; extras `looks_like_credential`, `host_user_spec`,
`remove_container_command`. 227 test functions (674 cases), 100 percent line coverage; one skip (symlinks not creatable on this
account). Docker was never started and nothing was pulled; Hermes was only read. Nemotron reviewers returned 403, so the builder
reviewed adversarially itself and closed real bypasses (Windows spellings that hid `~/.ssh`: trailing dot or space, `\?\` prefix, admin
shares such as `\localhost\C$`; `--user 00`; `re.match` accepting a trailing newline; a vacuous pass when the planted `.env` was
"no such file"; mounting the Docker socket).

Two deviations from blueprint table 33, both from reading the Hermes 0.21.3 source (read these first):
1. `terminal_block` OMITS `cwd: /workspace`. The kanban dispatcher pins `TERMINAL_CWD` to the worktree, then the worker's CLI overwrites
   it with the profile's `terminal.cwd`; Hermes then looks for a host directory named `workspace` (`C:\workspace`), so the worktree is
   NOT mounted. Verified by running Hermes's real functions in memory. With no `cwd` the worktree is mounted and the container still starts
   in `/workspace`. `check_terminal_block` therefore flags an explicit `cwd`. Test:
   `test_table_33s_cwd_would_defeat_the_worktree_mount_on_the_installed_hermes` (skips if Hermes is absent).
2. `terminal_block` ADDS `docker_persist_across_processes: false` and the checker requires it: Hermes keeps one container per profile and
   reattaches by labels only, never comparing mounts, so without it card 2's worker would see card 1's worktree.
   `container_persistent: false` cannot replace it (Hermes then mounts an empty tmpfs).

Table 33 keys: all exist in 0.21.3 (`docker_network` is real although missing from `cli-config.yaml.example`). There is NO PID-limit key:
Hermes hard-codes `--pids-limit 256`, and only when its cgroup probe passes.

Extra checks beyond the work order: `docker_extra_args` allow-list, `docker_env`, `env_passthrough`, `credential_files`,
`docker_volumes` containing `:/workspace`, `docker_shared_container_key`, `docker_snap_compat`, and engine sockets and system paths as
volumes. `from_config` refuses `network_default: true`.

Noticed (design questions for the Phase 5 real test):
- The real profiles have NO `terminal:` block today (local backend).
- `docker_run_as_host_user` does nothing on native Windows (Hermes needs `os.getuid`): the container runs as the image default (usually
  root) with SETUID/SETGID added back, so ASES-SEC-03's "host user" is not met there by config alone.
- Hermes silently drops ALL CPU, memory and PID limits when its own probe container fails to start (for example the image is not pulled);
  `doctor_checks` therefore carries a pull command for a human to run.
- Workers killed by a timeout or the kill switch leave a RUNNING container (Hermes only reaps exited ones; labels `hermes-agent=1`,
  `hermes-profile`); nothing removes them yet (`killswitch` stops containers whose name or labels contain a plan card id: check that the
  labels match).
- BIG ONE: a `git worktree` checkout's `.git` is a FILE pointing at a host path outside the mount (confirmed with a throwaway repo), so
  `git` fails inside a worktree-only sandbox: both gates that call git and workers told to "Commit your work". Options: run gates on a
  `git archive` export; have the controller commit on the worker's behalf; or mount the shared git dir (which lets a worker touch the
  integration branch refs, violating ASES-GIT-02). Decide before enabling the sandbox.
- `key_visibility_test` only exercises containers `docker_run_argv` builds, not Hermes-managed worker containers (a skill-declared env
  var could still reach a worker; only `docker exec <worker> env` would show it).
- Masking `secrets.*` blanks a source file like `secrets.py` and `*.pem`/`*.key` test fixtures (the blueprint's own patterns).
- `swarm.yaml` has no `sandbox:` block and `doctor.py` still has a placeholder check for it.
- The persistent per-profile `/root` bind mount still carries state between cards.
- Hermes's kanban worker prompt tells workers to `cd $HERMES_KANBAN_WORKSPACE`, a host path or unset inside the container.

## Package TM: tamper check and gate runner hook (`src/ases/tamper.py`, `src/ases/gates.py`, `tests/unit/test_tamper.py`, `tests/unit/test_gates.py`), done

Built: `tamper.py` with `Finding` (kind, path, detail, line, `blocks`), `KINDS` (ten kinds), `TamperCheckError`, `FileDiff`/`Hunk`,
`parse_diff` (hunk counts decide content versus header, so an added line reading `+++ b/.env` stays content), `analyze_diff`
(pure, never raises), `check_range` (`base...head`, raises `TamperCheckError` on any git failure, refuses a revision starting with `-`,
forces config, prefix and colour flags so a user's git config cannot change the parse, adds `large_file`), `format_findings`,
`format_finding`, `blocking`, `coverage_check`, `secret_hint`, `is_secret_file`. In `gates.py`: `run_gate(..., runner=None)` (a runner
replaces `_run_commands`; checkout, cleanup and the `gate_runs` row unchanged), `detect_tamper` delegates to `tamper.analyze_diff`,
`scan_for_secrets` also reports added secret-named files, never echoing a value. 98 + 17 new test functions (332 + 32 items), including
the exact section 22.12 sequence on real temp git repos and a fuzz test for "never raises". A read-only run of `check_range` over 20
recent ASES commits found no false positives for the structural kinds (the 16 hits were fake tokens in the scanner's own tests and
marker strings in the old `detect_tamper`).

Judgment calls:
- Glob semantics follow the code (`fnmatch`, where `*` and `**` are equivalent, as in `integrity.paths_outside_touches` and
  `plan._globs_overlap`), not the work order's "`*` does not cross directories", so the scope check and the tamper check cannot disagree.
  One function, `_glob_match`, if stricter matching is wanted.
- A `generated_artifact` exemption must NAME the artifact (`dist/**`, `*.db`); `src/**` or `**` do not exempt `__pycache__`.
- `assertion_weakened` applies to every file not matched by `allow_paths`, test or not, and only to modified files.
- Each added test definition can stand in for one removal (a rename): deleting three tests and adding one is still two findings.
- `check_range` uses `--no-renames`, so a moved test file reads as `test_file_deleted` (no `allow_paths` entry can exempt it).
- Extras: five more secret shapes (private key header, AWS key id, GitHub fine-grained token, live payment keys, Google API key);
  doc files (`.md`, `.rst`, `.txt`) exempt from skip and unconditional-pass markers; `tests/` and `spec/` count as test paths only for
  source-code extensions.
- `run_gate` with a raising runner: the exception propagates, cleanup still runs, NO `gate_runs` row is written (the caller decides what an
  infrastructure failure means).

Noticed (to settle in wiring):
- NOTHING calls `tamper.check_range` yet: Gate 1 has no secret scan or tamper check until it is wired (ASES-SEC-01 wants Gate 1 as
  well as Gate 3). Wire it into `review.gate_before_review` and `review.check_branch_for_merge`.
- `questions.py` has a stale comment saying `scan_for_secrets` echoes 80 characters of the line (it no longer does; the call still works).
- `events._SECRET_VALUE_PATTERN` only knows `sk-`, so Stripe-style `sk_live_...` keys were never redacted from events (the tamper
  package's extra shapes cover diffs, not the event redactor).
- ASES's own past commits would trip this check if run through the swarm (fake tokens in tests, marker strings in code): the seeded
  fixtures need allow globs or a per-repo baseline if ASES ever builds itself.
- `spec/requirements.yaml` rows for QG-02, QG-03 and GIT-07 still need updating once wired.

# Round 5 (2026-09-21 and 2026-09-22): wiring, fake rig, evals, hardening, final gates, profiles

Work orders: `r5_rules.md`, `r5_contracts.md`, `r5_wp_*.md`, plus `r3_wp_finalgates.md` and `r4_wp_profiles.md`.

## Package QF: questions and escalation fix (`questions.py`, `recovery.py`, `report.py` and their tests), done

Built: `OpenQuestion` and `open_question(card)` (four sources: `blocked`, `gave_up`, `block_loop`, `ases_comment`; newest signal wins, a
comment beats an event on a tie; a later `unblocked` event or a later `ANSWER:` or `UNBLOCK:` comment means it was answered; pure;
redacted and ASCII-escaped) and `ask_user(board, card, text, *, conn=None, author="ases")` returning `already_asked`, `blocked` or
`commented` (`ready` and `running` go through `kanban_block(kind="needs_input")`, every other status and a refused block through a
`ASES QUESTION:` comment). `list_questions` reads both the `blocked` and `triage` lanes; `answer_question` on a triage card posts the
`ANSWER:` comment first and then raises `QuestionError` naming `hermes kanban specify <id>`. `recovery.process_failures` now uses
`ask_user` for `block_for_user` and `mark_credential_unhealthy`, and returns `switch_model` UNAPPLIED (model and provider filled in, like
`fresh_attempt`) with `recovery_switch_target` still recorded. `report.py` uses `open_question` and adds `cards.questions_by_source`.
Tests in the three files went from 561 to 754. The builder fuzzed `open_question` and `ask_user` 20,000 cases each.

Deviations:
- `open_question` ignores a `blocked` event whose reason is `initial_status` (Hermes writes that event for every card created blocked,
  so every merge card would otherwise be listed). The controller builder had a wrapper `controller._open_question` for the same thing;
  it is now redundant but harmless.
- `list_questions` lists a triage card with ANY open signal, including an `ASES QUESTION:` comment (else `ask_user` on a triage card
  would create a question nobody sees).
- `ask_user` raises `ValueError` for a blank question or a card with no id. The `gave_up` reason reads `gave up after N failure(s): <error>`.
- An answer whose unblock failed now reads as answered, so `swarm answer` refuses a second try (it names `hermes kanban unblock <id>`).
- No `question_answered` event on the triage path.

Noticed:
- Hermes keeps `block_recurrences` across an unblock and only `complete_task` clears it: a worker that uses an untyped `kanban_block`
  gets ONE answered question per card, the second lands in triage where `swarm answer` can only comment. (The profile prompts tell
  workers to use `--kind needs_input`, which does not avoid it: same kind repeats.)
- `ask_user` on a `todo`, `scheduled`, `review` or `done` card posts a comment but holds nothing, and `open_question` cannot see it.
- Blocking a `running` card releases the claim in the database but does not stop the live worker process.
- `list_comments` sorts by `created_at` only, so same-second comments have no guaranteed order (ties broken by list position).
- `hermes kanban unblock` on a triage card writes its `UNBLOCK:` comment BEFORE failing, so that question then reads as answered while
  the card stays in triage.
- `kanban_block` callers outside `questions.ask_user`: none in `src/`, apart from a worker prompt line at `controller.py:275`.

## Package MR: merge queue, review lane, usage and gates wiring (`mergeq.py`, `review.py`, `usage.py`, `gates.py` and their tests), done

Built: `MergeOutcome.stopped` and `merge_task(..., project=None, should_stop=None)` (polled before the candidate, before Gate 3 and
before the fast-forward; the `merge_records` row is written only after the last poll, so a stop leaves it untouched; a raising
`should_stop` counts as False and records `should_stop_error`); `build_candidate`, `fast_forward` and `revert` intents; the candidate
upsert now resets `reverted`, `squash_commit` and `completed_at` (the reconcile builder's `done_but_reverted` bug); `MergeOutcome.detail`
redacted whenever an outcome is built. `review.py`: `BranchCheck` kinds `tamper` and `tamper_check_error`, public `gate_config_paths`
(one `git cat-file --batch-check`, blobs only), `tamper.check_range` wired into `check_branch` AND `check_branch_for_merge` (after the
scope and binding checks, before any Gate 1 record is trusted, same merge-base as the scope check); `gate_before_review` sends a tamper
result back like a red Gate 1 and does NOT send back a `tamper_check_error` (returns True; the merge-time check is authoritative and
fails closed). `usage.py`: one `model_mismatch` event per session (detection only, written in the same savepoint as the usage row).
`gates.run_gate` redacts command output before it is stored and returned. 92 new test functions (133 items); the four files went from
324 to 457 tests.

Deviations:
- Provider attribution in `usage.py`: `hermes.session_usage` does not say which provider answered, so "determined" means a leading
  `<provider>/` naming a configured provider, or exactly one other configured provider listing that model; else the profile's provider.
- `check_branch_for_merge` does not call `check_branch`, so the tamper check was wired into both; `_check_scope` now returns
  `(BranchCheck, base)`.
- An extra `tamper_blocked` event from `gate_before_review` (the send-back is only a card comment and `report.py` shows events whose
  kind contains "tamper").
- The first `should_stop` poll comes before the wrong-checkout and `expected_head` refusals, so a halted project gets "stopped" and not
  a failure that would open a fix card. No `run_gate` intent (review's signatures carry no project).
- `tests/unit/test_usage.py` had CRLF line endings; normalised to LF as `.gitattributes` says.

Noticed:
- IMPORTANT: gate-config and assertion-weakening findings NEVER fire through the review wiring. The scope check runs first, so any path
  that reaches the tamper check is already inside the task's touches, which tamper treats as allowed. Verified by experiment: a
  `pytest.ini` edit is `out_of_scope` with touches `src/*` and `ok` with touches `*`, so a wildcard touches glob silently allows config
  edits. ASES-QG-02 is enforced by the scope check alone. To make the tamper rule matter: pass only non-wildcard touches as
  `allow_paths`, or reject wildcard touches (and touches naming gate config) at plan time (Gate 0).
- `revert_merge` marks `reverted = 1` even when `git revert` fails and never aborts a conflicted revert (the primary checkout can be
  left mid-revert). Nothing calls it (ASES-GIT-05 still partial). One-condition fix: `if ok and ...`.
- `report.HEALTH_KINDS` lacks `model_mismatch`, `should_stop_error`, `tamper_check_error`, `tamper_blocked`.
- `process_review_lane` re-runs `gate_before_review` on every pass for cards in review: a persistent tamper-check failure records one
  `tamper_check_error` event per pass (the controller dedupes its own, review does not).
- Concurrent full-suite runs by several builders share pytest's temp base directory and delete each other's dirs (spurious
  `FileNotFoundError` failures).
- Register rows to update once wired: ASES-QG-02, QG-03, GIT-07, REC-03, REC-04, REC-06, RTE-01, SEC-01. `gates.py` has an unused
  import (`ases_db`).

## Package FG: Gates 4 and 5 and the release report (`src/ases/finalgates.py`, `tests/unit/test_finalgates.py`), done

Built: `TreeFinding`, `severity`/`blocking`/`advisory` (only `injection_pattern` is advisory and `skipped` an info note; everything
else blocks), `format_finding`, `scan_text`, `scan_tree(repo, ref, ...)` (reads git objects with `ls-tree -z -l` plus `cat-file --batch`,
never raises, a git failure is one `scan_error`), `GateOutcome`, `FinalizeResult`, `run_gate4` (built-in scan first; a blocking finding
fails the gate WITHOUT running the plan's commands; then the plan's `gate4` profile; one combined row), `run_gate5` (the plan's `gate5`
profile, else every distinct task command in first-seen order; nothing to run fails), `final_gate_question`, `release_summary`,
`write_release_report` (`release.md` in ASCII plus `report.html`/`report.json` beside it; a failing project report is noted in
`release.md`, which is still written), `finalize` (steps a to f, the four events and the intents, idempotent). 177 test functions
(294 cases).

Deviations:
- `run_gate` is called with `conn=None` and the ONE row is always written by `bounds.record_final_gate` (only way it can be the combined
  scan-plus-commands result and cover paths where no command runs).
- Hooks (`scan`, `run_gate`, `run4`, `run5`, `build`, `is_finished`) default to None and resolve the real function at call time.
- `finalize` beyond the spec: returns "finished" at once for a finished project; `not_ready` when the project is stopped or paused (checked
  before each gate and before the report) or when the integration branch moved while the gates ran; a gate that raises returns `error`,
  records no gate row and closes its intent with "aborted"; a failed report write is retried without re-running green gates.
- Gate 5's fallback excludes the `gate4` profile. `tamper.py` keeps its artifact and secret-name lists private, so a subset is mirrored.
- Injection rules: JS `child_process.exec` and `innerHTML` need a `${`; comment lines skipped; lines read to 2000 characters; added
  `pickle.load(` and `+` concatenation. Not built: no Hermes audit or verify tool is called (the plan's commands are the mechanism);
  no false-positive allowlist.

Noticed:
- IMPORTANT: Gate 4 FAILS on ASES's own repository (37 blocking `secret_in_tree` hits on fake `sk-` keys in tests and docs, 8 advisory
  hits). Any project with sample keys in tests or docs fails Gate 4 with no override, and a repo that tracks `dist/` or `build/` fails
  permanently. Gate 1's tamper check lets a task's touches allow such a path; Gate 4 has no equivalent (needs an allowlist in the plan).
- `gates.run_gate` reports a failed worktree creation as a RED gate, not an infrastructure error: for a final gate that becomes
  `gate_failed` and a pause instead of a retry.
- `bounds.set_status(..., "paused", reason)` DROPS the reason (only `stopped` keeps it): the controller's gate-failure question survives
  only in the `hermes.pause` call; the CLI needs it recorded somewhere (an event).
- Two `stop_requested` again (`bounds` counts paused, `killswitch` does not); FG used bounds'.
- The `intents` key is the project name (per the `intents.py` convention), so reconcile's label for a crashed gate does not say which
  gate (the gate is in the intent's detail). Question counts in the release summary are database-wide (only `recovery_decision` carries a
  project field). `release_summary` reads every task's work card through `bounds.evaluate_bounds`.
- Gate 0 does not reserve the plan gate profile names `gate4` and `gate5`, so a task could use one as its focused gate.
- The release-report folder timestamp is `20260921T120000Z` (from killswitch); the CLI's `_reports_dir` may use another format: check.
- `spec/requirements.yaml` still lists ASES-TSK-04 as `not_covered`.

## Package CL: command line version 2 (`cli.py`, `doctor.py`, `config.py`, `config/swarm.yaml` and their tests), done

Built: `cli.py` rewritten with shared plumbing (`_lazy`, `_ascii`, `_reports_dir` with no colons in the timestamp and a numeric suffix on
a same-second collision, `_run_lead`/`_LeadResult` factored out of `cmd_plan`, `_estimate_lines` shared by `approve` and `critique`) and
every command of section 9.1: `questions`, `answer` (never echoes the answer), `status`, `report`, `critique` (Gate P, `--auto-replan`),
`approve` (requires a critic PASS bound to the plan hash, or `--skip-critic` recorded as `critic_skipped`; shows the critic summary;
`--deadline-minutes`), `run` (real reconcile at start: exit 5 or `--ignore-reconcile`; exit 4 stopped, paused or finished; Ctrl-C gives
130), `stop`, `resume` (`--extend-minutes`), `init`, `eval`, `clean`, `retention`, `doctor`, `models`, `plan`. `_NOT_BUILT_YET` and
`cmd_not_built_yet` are deleted. `doctor.py`: sandbox and profile-state rows (WARN while the sandbox is disabled, FAIL only when enabled
and a check fails; the Docker placeholder is gone). `config.py`: `sandbox` and `retention` blocks validated through
`SandboxPolicy.from_config`, unknown keys are errors. `config/swarm.yaml` gains documented `sandbox:` (`enabled: false`) and
`retention:` (30 and 90). 325 tests in the four files (27 before), coverage of `cli.py` and `config.py` measured with `sys.settrace`.

Deviations and extras:
- `swarm report` writes the page and JSON only with `--html` or `--out` (`--out` inside the repo is refused before any work).
- `approve` re-hashes the plan after the y/N answer and refuses if it changed (an edit made while the prompt waits would otherwise be
  published unreviewed); `run` checks the stop or pause flag before each pass (a `swarm stop` from another terminal is honoured whichever
  controller is behind `run_pass`); `run` with an invalid plan is a clean exit 1.
- `--deadline-minutes` stores now + N minutes only after the user approves (a later `swarm run` gets a shorter window).
- `swarm retention` defaults to the LONGER of `logs_days` and `reports_days` (`hardening.retention` takes one `days`).
- `swarm resume` without `--repo` cannot reconcile (says so; the next `swarm run` reconciles); with a plan that fails Gate 0 it refuses.
- `critique --auto-replan` does not feed a failed Gate 0 back to the Lead (prints the errors, exit 1).
- The nemotron reviewers returned 403: the builder reviewed adversarially itself and found the approve hole and two smaller gaps.

Noticed:
- `killswitch.within_deadline` stays True when `hermes pause` failed, so a stop that left dispatch running can report success.
- `bounds.set_status` keeps a reason only for `stopped`; the controller keeps a pause reason in a `project_paused` event.
- Wiring `critic.critique_rounds_used` into `project_state.replans` would stop a project at once (`evaluate_bounds` uses `used >= limit`
  and re-plans are a stop bound): NOT wired.
- `profiles.plan_init` returns warning-only rows even for a converged home (`swarm init` counts only `actionable` rows).
- `profiles.apply_init`'s default runner calls the real `hermes profile create`: no test may reach it through the CLI.
- `swarm stop` with no known projects writes a `stopped` `project_state` row for the configured project name ("ases").
- `reconcile.reconcile` needs a plan with `.tasks` and `.integration_branch` (an old test stand-in reported blocked, exit 5).
- `bounds.start_project` runs before reconcile, so a run refused with exit 5 has already started the wall clock.

## Package PF: profile scaffolding and role prompts (`src/ases/profiles.py`, `prompts/*.md`, `tests/unit/test_profiles.py`), done

Built: eleven prompts (lead, coder, reviewer, tester, architect, backend, frontend, database, devops, security, debugger; ASCII, each
under 4000 characters, the last line is the data-not-instructions sentence; `critic.md` untouched); `profiles.py` with `desired_profiles`
(lead, coder-1 and reviewer active; coder-2, coder-3 and tester defined but inactive), `ProfileSpec`, `RoleDef`, `ROLE_TABLE`,
`KNOWN_TOOLSETS`, `REVIEWER_FORBIDDEN_TOOLSETS`, `read_prompt`, `render_soul` (header, prompt, fixed footer), `current_state`
(read-only; only checks that `.env` exists), `Change`, `pending()`, `format_changes()`, `plan_init`, `apply_init` (refuses without
`confirmed=True`, backs up every file it changes, reads each write back), `verify_state`, `residual_risks()`, `ProfileError`. 153 test
functions (226 cases); every Hermes home is a tmp dir and an autouse fixture repoints `HERMES_HOME`, `LOCALAPPDATA` and `Path.home()`.

Both extra requirements were built: worker prompts block with `kind needs_input` and one precise question (and explain the triage
lane), request review with `reviewer=` and metadata naming the full `commit_sha`; the reviewer and security prompts say the verdict
metadata names the full commit sha.

Decisions (the user may want to change these):
- The Lead has NO `terminal` toolset (ASES-ROL-06: only implementation roles run commands; the blueprint wins over the work order).
  `swarm plan` passes `-t file,terminal` itself, so nothing breaks today; add it to `_LEAD_TOOLSETS` to give the Lead a terminal.
- Memory is off for the Lead as well as workers (the Lead plans across projects).
- `plan_init` returns WARNING rows for what ASES will not fix itself (sandbox off, "needs credentials from the user", unmanaged
  terminal keys, unset explicit kanban values); `Change.actionable` and `pending(plan)` separate them (the CLI already counts only
  actionable rows).
- With `include_global` only, the desired global state goes beyond the work order: `failure_limit` from `budgets.attempts_per_card`,
  `dispatch_interval_seconds`, `review_dispatch` restored to true, `auto_decompose` false, `auto_promote_children` false, and a
  `default_assignee` cleared.
- An xKiro-type provider is matched by its endpoint; when missing it is written as a named `providers:` entry (base_url, key_env) plus
  `model.provider`; an api_key is never written.
- The argv is exactly as specified, so there is no `--no-alias`: Hermes will write a wrapper script into `~/.local/bin`.
- NOT verified against the user's real profile configs (the rules kept the builder out of them): the first real `swarm init` dry run is
  the first contact.

Residual risk (kept visible in `RESIDUAL_RISKS`): Hermes 0.21.3 has one combined `file` toolset (read, write, patch, search) and no
read-only one, and `agent.disabled_toolsets` removes only whole toolsets, so the Reviewer keeps write tools that its prompt forbids
(it has no terminal, so it cannot commit; the controller believes only its own gate records). `verify_state` does not repeat it, so the
doctor stays quiet; the CLI would have to print `profiles.residual_risks()`.

Behaviours that may surprise: `kanban_request_changes` and `kanban_block` take no metadata in Hermes 0.21.3, so the reviewer prompt puts
the section 13.3 structure in the reason text for CHANGES_REQUIRED and BLOCKED (only PASS via `kanban_complete` carries it in run
metadata). Review-only cards (plan role reviewer) have no commit to name: the reviewer and security prompts say to follow the card's own
"How to finish" steps. Keys found in the Hermes source: `memory.memory_enabled`, `memory.user_profile_enabled`, `memory.provider`, the
`memory` toolset, and the top-level `worktree_sync` (read only by `hermes -w`).

Noticed:
- `kanban.auto_decompose` defaults to TRUE: the gateway dispatcher decomposes triage cards with an auxiliary model when one is configured
  (triage cards appear when Hermes detects a block loop) and children auto-promote by default. That contradicts "ASES never runs
  specify on its own"; it is in the opt-in global desired state.
- `controller._finish_instructions` treats every role other than "coder" as a review-only card: a task with role tester (or backend etc.
  once mapped) would get the wrong finishing text and its commit would never go to review. Needs a tester branch before the tester is enabled.
- `worktree_sync` is NOT read by the kanban dispatcher: it always runs `git worktree add -b <branch> <path> HEAD` from the board repo,
  so the controller must still verify each card's base commit.
- `hermes profile create` (fresh) seeds the launch profile's model block and, for a custom provider, that provider's entry (which may
  hold an inline key), writes a default SOUL.md and a placeholder `.env`, and seeds the bundled skills.
- Hermes appends the kanban lifecycle tools to EVERY dispatcher-spawned worker regardless of the profile's toolset list.
- Hermes writes `config.yaml` with `IndentDumper` and `allow_unicode=True`; ASES mirrors the layout but comments are lost (the backup
  keeps the original bytes). `session_search` stays on the Reviewer as specified but arguably cuts against ASES-ROL-05 and ROL-07.

## Package CT: controller loop version 2 (`controller.py`, `tests/unit/test_controller.py`, `tests/unit/test_controller_loop.py`), done

Built: `create_cards_from_plan` (passes `max_retries` from `attempts_per_card`, inside a create-cards intent, and keeps the replacement
card of a task that has had a fresh attempt on a re-approve); `process_merge_queue` (skips a task whose merge card has an open question,
asks through `ask_user` and never calls `kanban_block` itself, redacts the fix-card body, events and question, gives fix cards
`max_retries`, handles `stopped` outcomes and `tamper_check_error`, wraps merge-card completion in an intent); the new steps
`process_recovery`, `_start_fresh_attempt`, `_request_replan`, `process_unpark` (with `_affordable_now` shared with the budget gate),
`process_bounds`, `pause_and_report`, `process_provision`, `process_idle_worktrees`, `process_finalize`, `_halted`; and `run_pass`
version 2 (order, per-step exception isolation and the summary contract of `r5_contracts.md`). 133 new test functions (153 items) in
`test_controller_loop.py`; the 118 tests in `test_controller.py` keep their names (an autouse fixture makes the new steps inert). The
builder also drove the REAL controller against the FK fake Hermes with scratch scenarios (not in the repo): capability failure to
fresh attempt to merged and finished, second failure to model switch, the ask/wait/answer/ask-again loop on a merge card, and a
wall-clock pause with the report all worked.

Answer to the work order's question: YES, `recovery.process_failures` loses a decision when `_start_fresh_attempt` fails (it records the
`recovery_decision` event and bumps the counter before the controller acts and never returns that run again). The builder added a
controller-side redrive built from those events: `_pending_decisions` finds `fresh_attempt`, `switch_model` and `replan` decisions with
no completion marker and `_redrive` carries them out on the next pass, dropping one if the card was resumed, answered or has a newer
run. Tested with injected failures of create, link and archive: exactly one retry card, counted once.

Deviations:
- In `_start_fresh_attempt` the repoint together with `retry_card_created` is the LAST step (after the archive), the model pin comes right
  after the create; the order in the work order was not crash-safe.
- The lineage escalation step acts on review rounds only, once per round count, and skips running cards or cards with another open
  question; fix cards escalate in the merge queue at the failure that would need a third fix card (table 17); attempts are decided by
  `process_failures`.
- Re-approve guard (Hermes ignores archived cards when it looks up an idempotency key, so a re-approve would have made a duplicate original).
- The expected primary HEAD is set BEFORE completing a merge card, so a Hermes error while completing cannot look like an intruder.
- Idle worktrees use the workspaces of ALL running cards on the board (avoids false positives). The budget-park event carries the same
  reason text that goes to Hermes.

Noticed:
- Hermes 0.21.3 writes a `blocked` event with reason `initial_status` on every card created blocked (contradicts "no blocked event" in
  `r2_rules.md`; FK's fake models it; QF's `open_question` now ignores it, and the controller has its own redundant guard).
- BUG in the FK fake: `FakeHermes.fail_next` rejects every name once the fake is installed (it checks `inspect.isfunction` on bound
  methods); the builder armed faults directly in scratch runs.
- `gate_runs` has no project column: two projects that reuse a task key share the "latest gate output" lookup in the failure bundle.
- Hermes call volume: each pass now makes roughly five `kanban_show` calls per task (recovery refresh, recovery failures, bounds wall
  clock, merge queue twice); a per-pass card cache would cut it.
- A project paused by the replan bound pauses AGAIN on the next pass after `swarm resume` unless the user raises `replans_per_project`
  (the counter is not reset).
- An answer that lands in the same second as the controller's next block reads as already answered under QF's tie rule (one extra
  merge attempt and one extra question).

## Package FK: acceptance rig (`src/ases/fakes/board.py`, `worker.py`, `provider.py`, `tests/unit/test_fakes.py`, `tests/acceptance/`), done

Built: `FakeHermes(repo=None, *, board="ases-test", integration_branch="integration", now=None, scratch_root=None)`, an in-memory
simulation of Hermes 0.21.3 ported from the real source (`kanban_db.py`, `kanban_db_dispatch.py`, `kanban_db_workspace.py`, `kanban.py`,
`tools/kanban_tools.py`): every public function of `hermes.py` with the same signature (a test enforces parity), the worker side
(`agent_request_review`, `agent_complete`, `agent_block`, `agent_request_changes`, `agent_comment`, `agent_heartbeat`, `agent_fail`,
`agent_hang`), test helpers (`install`, `card`, `cards`, `events`, `comments`, `runs`, `worktree`, `live_workers`, `snapshot`, `describe`,
`calls`, `fail_next`, `fail_spawn`, `tick`, `register_worker`, `defer`, `kill_worker`, `set_session_usage`); dispatch creates REAL git
worktrees at `<primary>/.worktrees/<card id>`; fake worker pids start at 2,100,000,000 so a probe or kill can never hit a real process.
`worker.py`: steps `Write`, `Delete`, `Commit`, `Untracked`, `RequestReview`, `Complete`, `Block`, `Crash`, `Timeout`, `Comment`,
`Heartbeat` (+ `Append`, `Modify`, `RequestChanges`, `Sleep`, `Do`; `Sleep` keeps a card running across passes, which 22.5, 22.7 and 22.13
need), personas `good_coder`, `slow_coder`, `wrong_coder`, `tampering_coder` (five kinds, checked against the real tamper and review
code), `questioner`, `crasher`, `touches_coder`, `reviewer_pass`, `reviewer_changes`, `reviewer_wrong_commit`, `by_task_key`, `sequence`.
`provider.py` extended (existing API and tests untouched): `delay_seconds`, `drop_connection`, `tool_call_response`, `slow_response`,
`rate_limit`, `unauthorized`, `malformed_json`, a request log with credentials redacted, `assert_never_received` (fails by secret index
and length, never printing the value), `hermes_endpoint_config`. `tests/acceptance/`: `conftest.py` (`World`, `make_world`, fixtures
`world`, `world_factory`, `one_task_plan`, `create_cards`, `run_until`, `git`) and `test_scenarios_demo.py` with four scenarios (22.2
merge order and one squash commit per task; a worker question listed, answered and unblocked, with a controller restart; waiting merge
cards are not questions; 22.6 changes once and only the corrected commit merges). 162 tests in the rig (144 unit functions, 158 cases,
4 acceptance). No xfail was needed. The builder also ran a merge conflict, fix card and final gates through the real controller from the
scratchpad: it worked.

Deviations:
- `pause` does NOT stop `kanban_dispatch` by default: in the Hermes source only the gateway loop honours `hermes pause`; the CLI
  `kanban dispatch` that `run_pass` calls does not (`gateway/kanban_watchers.py`, `_kanban_dispatch_allowed`). `tick()` stops while paused.
  `cli_dispatch_honors_pause = True` gives the other reading.
- `request_changes` carries no metadata in Hermes 0.21.3, so `reviewer_changes` puts the verdict in the reason text.
- A card created blocked gets a `blocked` event with reason `initial_status` by default (real Hermes does; `r2_rules.md` was wrong);
  `initial_block_event = False` gives the r2 reading.
- Worktrees are cut from the integration branch NAME (real Hermes cuts from `HEAD`: identical while the primary stays on integration).
- Not modelled: goal mode, attachments, `projects.db`, multiple boards, hand-off secret redaction, the respawn guard's PR-URL rule, the
  systemic-crash shortcut, orphan reconciliation, the no-heartbeat sweep. Stricter than Hermes: a board other than the fake's raises.

Noticed:
- A worker that exits without a terminal kanban call trips its give-up and the card is promoted straight back to `ready` in the same
  tick (`consecutive_failures` stays 1 and `recompute_ready` only holds a blocked card whose counter reached its limit): code that waits
  for `blocked` after such a `gave_up` will not see it (pinned by a test).
- `swarm stop` cannot stop `swarm run`'s own dispatch (`hermes pause` does not affect the CLI dispatch): `run_pass` must check the flag
  itself (the CL and CT packages now do).
- A reviewer that completes a card with a CHANGES_REQUIRED verdict dead-ends it (only `kanban_complete` carries metadata): the card goes
  `done`, `process_merge_queue` refuses it once (`merge_refused_invalid_verdict`) and nothing sends it back. Read, not run.
- `hermes.kanban_reopen_review`'s docstring is inaccurate (the CLI writes the reason AFTER the reopen succeeds).
- `HermesCommandError.__init__` overwrites `self.args` with the argv and `super().__init__` resets it to `(message,)`.
- `killswitch._inspect_card` reads `card["worker_pid"]` as a fallback; real task dicts have no such field (only runs do).
- `FakeHermes.fail_next` rejects every name once the fake is installed (found by the CT builder).
- An untracked `data/ases.db.bak-v3-20260921T224427Z` appeared: see the entry "the real database was migrated" below.

## Package EV: evaluation harness (`src/ases/evals.py`, `src/ases/evalkit/`, `tests/unit/test_evals.py`), done

Built: `evals.py` (1,398 lines: `RunRecord` with every Appendix D.2 metric as its own field, `Estimate`, `RunSummary`, `Regression`,
`RoleValue`, `candidate_from_config` and `load_candidates` (an unknown `provider/model` label is an error that lists what is declared),
`estimate`, `calendar_minutes`, `check_budget` (refuses when the day's quota cannot cover the run), `default_invoke` (the ONLY function
that calls a model: `hermes -p P -z PROMPT -m M --provider X [-t tools] --usage-file F`, never raises), `run_eval` (dry run unless
`spend=True`; each run in its own temp directory; raw output redacted; results flushed per run; one failing run never stops the rest),
`load_run`, `render_report` (four tables per candidate by task, no combined score, ASCII), `compare`, `role_value`,
`is_dynamic_router`, `recommend`, `main` with `list`, `run`, `report`, `compare` and an extra `role-value`; exit codes 0 ok, 1 usage or
refusal, 2 regression) and `evalkit/` (keyword scoring, fence and JSON extraction, a tolerant diff applier that drops edits to tests and
pytest config, pytest in a scrubbed environment, tasks E1 to E10 with deterministic scorers, E8 as a swarm descriptor plus
`score_swarm_project`, E11 as a comparison descriptor). 198 tests (E6's reference tests kill all 10 seeded mutants, a weak file kills none).
Nothing calls a model; nothing has run against a real provider.

Deviations:
- Request counting: a real Phase 2 usage file shows a plain one-shot call is 1 main call plus 1 auxiliary title-generation call (2 in
  all) and Hermes's `oneshot.py` bills on `total_including_auxiliary`, so the usage report comes first and `session_usage` (main loop
  only) is a last resort. Estimates: 2 requests per text task, E9 4, E3 13, E8 200. A report with no counts and no session means Hermes
  failed before its agent ran (0 requests).
- Extras: a per-run budget re-check before each run (status `stopped`, exit 1), `--profile`, `--candidate`, `--pinned`, the
  `role-value` subcommand, test-only keyword arguments on `main`.
- E9 is JSON tool-call emulation as specified (it does not reproduce Phase 2's real Hermes tool-call test). Model-written code (E4 to
  E6) runs with the user's rights in a temp copy with credential-like environment variables removed; E3 gives the model Hermes's `file`
  toolset, which can write (`list` and the dry-run plan say so).
- `--provider` gets the ASES provider label; real usage files report `"provider": "custom"` for custom endpoints, so `--provider xkiro`
  may be refused by Hermes (it would fail before any request, at no cost; a `hermes_provider` key in `models.yaml` fixes it).

Noticed:
- Hermes keeps auxiliary calls (title generation, compression, vision) in separate `session_model_usage` rows while
  `sessions.api_call_count` is the main loop only. `usage.py` counts worker sessions from the latter, so the ledger probably UNDER-COUNTS
  worker sessions: check with a real worker session.
- `merge_records` has no `project` column, so `score_swarm_project` scopes by the project's task keys.
- `config.py` now imports `sandbox` at import time (`evals.py` imports config lazily).

## The real database was migrated (an accident, harmless so far)

While wiring-checking, the EV builder ran `python -m ases.cli eval run` (a dry run) against the REAL config. The new auto-migrating
`db.connect` upgraded the real `data/ases.db` from schema v3 to v7 (2026-09-21 22:44 UTC) and wrote a backup,
`data/ases.db.bak-v3-20260921T224427Z`. Checked read-only on 2026-09-21: `PRAGMA integrity_check` is `ok` on both files; row counts are
identical (plan_tasks 5, gate_runs 4, merge_records 3, events 15); the migration rows are 1 to 7; no eval or ledger rows were written.
The database had last been opened on 2026-09-19 (schema 3), so the v4 to v7 steps ran for the first time. Nothing else touches the real
path (the suite uses temp files; `test_cli_commands.py` only loads the real CONFIG files). `.gitignore` now also ignores
`data/*.db.bak-*`. Restoring the backup is a copy of that file over `data/ases.db` (with no ASES process running), but keeping the
upgraded database is what the next real run would do anyway.

## Package HD: hardening and migrations (`src/ases/db.py`, `src/ases/hardening.py`, `tests/unit/test_db.py`, `tests/unit/test_hardening.py`, `docs/operations.md`, `docs/runbook.md`), done

Built: `db.py` with `MigrationError`, `Migration`, `MIGRATIONS` (7 numbered migrations; `SCHEMA_VERSION` derived; `BACKUPS_KEPT = 5`),
`latest_version`, `current_version`, `pending`, `backup_path` and a rewritten `connect` (each migration in its own `BEGIN IMMEDIATE`
transaction that also records its `schema_migrations` row; the version is re-read under the write lock so two processes upgrading at
once do not double-apply; a whole-file backup by the SQLite backup API before touching an existing non-empty database, pruned to 5; a
NEWER database is refused untouched; a failed migration or backup raises `MigrationError` with the connection closed; a migration is
never applied without its backup; version 7 only makes room: a nullable `project` column on `gate_runs`, `merge_records` and `events`
plus two indexes). `hardening.py`: `clean` (dry run by default: `git worktree` cleanup of stale, leftover `ases-merge-*` candidate and
finished-card worktrees, and deletion of `swarm/*` and `merge/*` branches that are proven merged), `retention` (files under `logs`,
`reports`, `stops`, `evals` and old `ases.db.bak-*`), `retention_events`, `vacuum` and the `format_*` functions; every removal writes a
`hardening_removed` event. Docs: `docs/operations.md` and `docs/runbook.md` (16 symptom, cause, action sections). 178 new tests.
Self-review and seeded-bug testing (60 mutants against a scratch copy, 59 killed, 1 equivalent) because the nemotron reviewers returned 403.

Facts and deviations:
- The real `data/ases.db` was at schema 3 (rows 1 to 3), not 5 as the work order assumed, so the v1 to v3 upgrade paths are needed on
  real data. The builder did not open the real database: it read the backup read-only and upgraded a temp copy (every row identical,
  integrity ok, no foreign-key violations). See "The real database was migrated" above.
- v1 is the schema-1 baseline (each object is defined once, in the migration that introduced it); a test proves a fresh database and
  one upgraded from each of versions 1 to 6 have identical shape. v5 re-ensures the three `usage_ingested` attribution columns.
- `git branch --merged` alone would clean nothing (ASES merges by squash): a branch is also deletable when its merge record is
  completed, not reverted, names a squash commit that is in the integration branch, and every path the branch changed has the same
  content in that commit. Squash-proven branches go with `git branch -D`, merged ones with `-d`. Dry run against the real test repo (temp
  database, fake board): `swarm/G1-coder` proven, `swarm/G2-reviewer` merged, dirty `A1` worktree protected, unmerged `T1` kept.
- `clean` also removes the worktree of a finished card (`<repo>/.worktrees/<card id>`, clean tree, task finished; switch off with
  `card_worktrees=False`), leftover candidate worktrees must be at least an hour old with no open build or fast-forward intent, a branch
  name that fits two task keys is skipped, it never removes the worktree it is pointed at, and it fails closed if the card list cannot
  be read. A report directory is one retention entry aged by its newest file.
- BUG found and fixed: two processes upgrading in the same second collided on the backup name and the second `os.replace` failed with
  "Access denied" on Windows (caught by the suite's own four-process test; `_backup` now accepts an existing finished backup).

Noticed:
- `cli.py` never catches `db.MigrationError`: a database from a newer ASES, a failed migration or a failed backup surfaces as a
  traceback from any command (fixed by the architect the same day: `main()` now prints one line and exits 1).
- `cmd_answer`'s advice text says a second `swarm answer` can retry an answer whose unblock failed; `questions.answer_question`
  refuses the second call and names `hermes kanban unblock <id>` (the runbook follows `questions.py`; fixed by the architect).
- `events` is STATE, not just a log (critic verdicts, pause reasons and the release-report path are read from it), so `retention_events`
  is dangerous while a project is live; no command calls it and `retention()` never touches events.
- `merge_records` is keyed by `task_key` alone; the new `project` column exists but nothing writes it yet (`clean` uses
  `project IS NULL OR project = ?`). `docs/architecture.md` around line 674 still describes `_ensure_columns`.
- `hermes worktree prune` skips `t_*` kanban trees by design (`_KANBAN_RE` in `worktree_gc.py`), so ASES needs its own cleanup.
- This account cannot create symlinks (link-safety tests fall back to Windows junctions).

# Round 6 (2026-09-22): acceptance scenarios, project scoping, touches validation, triage, consolidation (all zero quota)

Work orders: `r6_rules.md`, `r6_wp_*.md`. Every package was told never to call a real Hermes or a real model provider.

## Package AC-F: acceptance 22.11 injection (`tests/acceptance/test_22_11_injection.py`), done

Built three tests: sandbox policy is network-deny-by-default for a profile built the real way (`config.ProjectConfig.
sandbox_policy_config()` -> `SandboxPolicy.from_config`, the same call doctor.py/cli.py make); a worker that writes outside its
worktree (the one thing the rig can simulate of "the model echoes the injection as a tool call") trips `guards.check_primary_checkout`
end to end through the real `controller.run_pass`, records `integrity_violation`, halts the pass, and the integration branch never
moves again; the negative baseline (injection text present, never acted on) causes no violation and a normal merge. Full suite:
5279 passed, 2 skipped, 1 failed (the one failure was `test_killswitch.py`'s live AST scan colliding with another builder mid-edit;
passed alone on re-run).

Correction to the work order's own speculation: it worried `ScriptedWorker` might have no way to simulate an out-of-worktree write.
It does: `WorkerContext.path()` accepts an absolute path "so a worker can deliberately write OUTSIDE its worktree (the prompt-injection
and integrity scenarios need exactly that)" - built for exactly this. So three of the blueprint's four clauses (nothing outside the
worktree, the integration branch untouched, a security event recorded) are proven fully END TO END, not reduced.

Only clause 1 ("the sandbox must block the network call") stays at the policy level, as `r6_rules.md` anticipated: FakeHermes/
ScriptedWorker never shells out and Docker never starts in this suite by hard constraint, so nothing here can make a real network
attempt. The builder's judgment: don't spend a future round adding a `worker.py` step to fake a "curl attempt" (it would still not be
a real network call, just decoration around the same policy-level proof); closing that gap for real needs a separate, explicitly
Docker-permitted test phase, deliberately out of scope for this whole suite.

Noticed: `guards.check_idle_worktrees` is warning-only with documented false positives (ASES-GIT-12's own register note); the primary-
checkout guard (`check_primary_checkout`) is the half that actually halts and records the security event, and the test says so.

## Package FIX: consolidation (`recovery.py`, `killswitch.py`, `report.py` and their tests), done

Built: `recovery.Bounds` is now an ALIAS of `bounds.Bounds` (not a second class), so `exhausted`/`escalation`/`decide` read the same
8-field, frozen, strictly-parsed dataclass `controller.py` already uses; `report.HEALTH_KINDS` extended from 8 to 12 (adds
`model_mismatch`, `should_stop_error`, `tamper_check_error`, `tamper_blocked`), with the two tamper kinds deliberately double-listed
(counted in health AND still individually shown in the quality panel's "contains tamper" catch-all, which the builder confirmed is a
deliberately general net, not tied only to these two kinds: it is tested against a fictional kind that matches nothing in `src/`).
694 tests in the three files; full suite 5353 passed, 2 skipped, 11 failed (all in `mergeq.py`/`triage.py`, owned by CORE/LED and being
edited concurrently; confirmed transient by an isolated re-run of just those 5 files: 134 passed, 0 failed).

DEVIATION from the work order, deliberate: `killswitch.stop_requested` was KEPT with its original name and "stopped only" meaning,
not renamed or merged into `bounds.stop_requested`. Reason: `tests/unit/test_cli_commands.py` (owned by package CL, not FIX, and
protected by the standing rule against editing another package's file) calls `killswitch.stop_requested` 8 times, correctly and
deliberately distinguishing it from a paused state elsewhere in the same file via `bounds.get_state(...)["status"]`. Renaming or
merging it would have broken that file. The docstring now cross-references `bounds.stop_requested` explicitly so the distinction is
documented, not just implicit. Independent confirmation: `tests/acceptance/test_22_13_kill_switch.py` (package AC-B, built
concurrently) reached the same conclusion on its own, in its own comments.

`Bounds.from_budgets` strictness: the old `recovery.Bounds.from_budgets` was lenient (a present bad value silently fell back to the
default); `bounds.Bounds.from_budgets` is strict (raises `ValueError`). The builder picked strict, and verified this introduces NO new
production failure mode: `controller.py` already calls the strict parser on the same raw `project.budgets` dict earlier in every pass
(via `bounds.evaluate_bounds`), so anything that would newly raise inside `recovery.py` would already have raised there first.

Noticed (to settle later):
- `controller.py` calls NEITHER `stop_requested` function by name. Its `_halted()` reimplements the identical "stopped or paused"
  check a THIRD time inline (`bounds_mod.get_state(...).status not in ("stopped", "paused")`), then threads it through
  `mergeq._stop_requested` (a private, generically-named helper). CORE could simplify `_halted` to call `bounds.stop_requested`
  directly; not fixed here (not FIX's file).
- `bounds.Bounds` is frozen; the old `recovery.Bounds` was not. Nothing mutates one today, so this is safe, but flagged for anyone who
  might later try an in-place mutation.

## Package LED: the triage lane (`src/ases/triage.py`, `tests/unit/test_triage.py`), done

Built: `TriageError`, `TriageCard`, `Decision` (PROMOTE/ARCHIVE), `ValidationResult`, `list_triage_cards`, `validate`, `promote_card`,
`archive_card`, `record_decision`, `format_triage`. 44 tests. Full suite 5336 passed, 42 failed (all in `test_controller.py`,
`test_controller_loop.py`, `test_mergeq.py`, owned by CORE and mid-edit at the time; none touch `triage.py`), 2 skipped, up from the
5277 baseline. No `propose_card` helper was needed (see the real-Hermes finding below, which confirms the work order's own guess).

Real Hermes finding (read-only, from the installed source, never run): a plain task WORKER can call `kanban_create` itself as a
structured tool call (`kanban_create` is NOT in `_ORCHESTRATOR_TOOLS`, the only two tools hidden from workers), so Appendix C.2's
"propose a follow-up card" is a literal tool call the worker makes, not a comment or a different tool name. `kanban_create` does NOT
default new cards to triage: its schema's `initial_status` enum is only `["running", "blocked"]` (triage is not even a legal value
there; `VALID_INITIAL_STATUSES = {"running", "blocked"}`), and a card lands in triage only when the worker's own call passes a
SEPARATE boolean, `triage=true`. `created_by` is exposed on the flat task dict; `creator_task_id` (a cleaner "which task's run created
this" signal) is NOT, which is why `raised_by_task` needs the same id/parent/title-prefix heuristic `questions.py`/`leases.py` use.

Deviations:
- `promote_card`/`validate` gained optional `plan`/`known_roles` kwargs beyond the listed signature, since `validate` needs them.
- `list_triage_cards` is NOT a literal copy of `questions._owner`'s ownership rule: a card claimed by NEITHER this project NOR another
  (no title prefix, no parent link) is still LISTED with `raised_by_task=None`, rather than dropped, because a proposal (unlike a
  question) is real work on the board that must be validated even with no clean attribution; only a card POSITIVELY claimed by another
  project is excluded. Flagged as a deliberate deviation from the literal instruction.
- The lineage charge field is a STOPGAP: `fix_cards` (the blueprint's closest conceptual match) is not in `recovery.bump`'s whitelist
  and is bumped directly by `mergeq.process_merge_queue`, unreachable from this package. Of the four reachable fields, `infra_failures`
  was chosen because `recovery.exhausted`/`escalation` explicitly exclude it from ever triggering a replan or a model switch, giving it
  the narrowest blast radius, but it is still not semantically correct. A new `proposed_cards` column on the `lineage` table is the
  right long-term fix (not built: LED does not own `db.py`).

Noticed: ASES's own `hermes.py::kanban_create` wrapper has NO `triage` parameter at all (only `initial_status`), so if ASES itself
ever needs to land a card in triage (as opposed to a worker's own tool call, which is the LED-03 path), that wrapper and the CLI's
`create` subcommand both need extending first.

## Package AC-B: acceptance 22.5 (parallel) and 22.13 (kill switch) (`tests/acceptance/test_22_5_parallel.py`, `test_22_13_kill_switch.py`), done

Built: one scenario per file, three cards running at once with distinct worktrees/branches/profiles/port-blocks and a fourth queued
by `max_in_progress` (22.5); `killswitch.stop_all`/`resume_all` driven against three live cards with fake `alive`/`killer`/
`command_line` built from `fake.live_workers()` (never a real process), plus a focused test that a pid not answering alive is reported
unverified and never killed (22.13). 3 tests total. Full suite: 5336 passed, 2 skipped, 42 failed, ALL in `test_controller.py`,
`test_controller_loop.py`, `test_mergeq.py` (CORE was mid-edit); a 60s-later re-run of just those three files dropped it to 5 different
failures, still zero in AC-B's own files, confirming a moving target from concurrent editing, not AC-B's bug.

Deviation: `conftest.py`'s `world_factory`/`make_world` have a roles map fixed to one profile per role
(`policy.resolve_assignee` is a strict one-role-one-profile lookup), but 22.5/22.13 need THREE distinct coder profiles. Since AC-B
cannot edit `conftest.py`, it built each scenario's own `World` by repeating `make_world`'s steps with a wider roles map, rather than
using the shared fixture. Suggests a `roles=`/`known_roles=` override on `make_world`/`world_factory` would help future multi-profile
scenarios (not added: out of scope for this package). Also: `killswitch.default_list_containers`/`default_stop_container` swallow ANY
exception by design, so a raising guard is silently eaten there; the builder used a call-tracking list instead of `pytest.raises`.

Noticed: `leases.py`'s module-level `LIVE_STATUSES = ("running","ready","review","scheduled")` OMITS "todo"/"blocked", while
`controller.py`'s own `_LEASE_LIVE_STATUSES` (always passed explicitly to `sweep_finished`) includes both -- two similarly-named
tuples that disagree; worth confirming the module-level one isn't stale dead code or a real bug.

Confirmed signatures (matched the work order's assumptions): `killswitch.stop_all(board, plan, *, conn, deadline_seconds=30,
reason=..., pause=None, kanban_list=None, kanban_show=None, reclaim=None, killer=None, alive=None, command_line=None,
list_containers=None, stop_container=None, now=None, sleep=None)`; `resume_all(board, plan, *, conn, resume=None, reconcile=None)`;
`reconcile.reconcile(board, repo, plan, *, conn, apply=True, alive=pid_alive, killer=terminate_tree,
command_line=process_command_line)`.

## Package AC-C: acceptance 22.7 (crash recovery, all three points) (`tests/acceptance/test_22_7_crash_recovery.py`), done

Built: three tests, one per crash point, each with its own `world_factory()` world (never shared). Full suite: 5342 passed, 2 skipped,
42 failed, all in `test_controller.py`/`test_controller_loop.py`/`test_mergeq.py` (CORE mid-edit); an isolated re-run of just those
three files gave 358 passed, 0 failed, confirming the collision was transient.

Crash simulations, each confirmed faithful to what a real SIGKILL would leave behind:
- Point 1 (during a running card): `fake.kill_worker(card_id)`, no monkeypatch at all -- runs zero ASES code, just leaves "running
  card, dead pid" on the board. Confirmed reconcile's OWN `worker_gone`/`worker_gone_reclaimed` repair fires (not Hermes's own
  dispatch-time crash detection, since nothing ticks between the kill and the reconcile call).
- Point 2 (during a candidate build): monkeypatched `gates.run_gate` ONLY for `gate_name == "gate3"`, delegating every other gate call
  (notably Gate 1 re-checks, which call the identical function) to the real implementation. Lands exactly in the
  row-written-before-Gate-3-returns gap `test_reconcile.py`'s own `test_crash_b` already proves in isolation, now through the real
  controller with a real Gate 3 running for every other call.
- Point 3 (between the fast-forward and the merge-card completion): monkeypatched `hermes.kanban_complete` itself (already pointed at
  `FakeHermes.kanban_complete` by `install()`) to raise the FIRST time it is called for the one merge card, then delegate normally.
  Lands exactly in the git-write/db-write-vs-board-write gap. Reconcile produces TWO repairs in order at this point
  (`merge_card_completed` then `intent_recovered`, since `process_merge_queue`'s own completion intent is also left open), matching
  `test_reconcile.py`'s shape; the test asserts both.

CONFIRMED BUG (independently found twice now: the round 5 CT builder hit the same thing): `FakeHermes.fail_next` cannot be armed
after `install()` for ANY test built on the standard `world`/`world_factory` fixtures, because its validation
(`inspect.isfunction(getattr(hermes, name))`) rejects a name once `install()` has already replaced it with a bound method -- which is
unconditionally true for every world, since `make_world()` always calls `install()` before returning. This blocks every acceptance
package from using `fail_next` as documented; the crash-point-3 monkeypatch above is the work order's own suggested fallback, used
because the documented mechanism does not work. Worth a fix in `fakes/board.py` (accept `inspect.ismethod` too, or check against the
fake's own method instead of the hermes module's current attribute) -- not built here, AC-C does not own that file.

Noticed: `spec/requirements.yaml`'s ASES-ARC-03 note says "reconcile-on-start (crash recovery) not built yet", which has drifted from
the source: `reconcile.py` is fully built with its own extensive unit-level crash-recovery suite, now also proven end to end by this
package. Per "the source wins, report the disagreement", not corrected here (out of scope for this package).

## Package TV: touches validation and Gate 4 allowlist (`tamper.py`, `plan.py`, `finalgates.py`, `config/swarm.yaml`), done

Built: `tamper.GATE_CONFIG_PATTERNS` (public export, built from the same private constants `_config_reason` already uses, so Gate 0
and the diff-time check share one list); `plan.py` gains `PlanTask.allow_gate_config_changes: bool = False` and
`Plan.gate4_allowlist: tuple[str, ...] = ()` (both default such that an existing plan.json parses unchanged), `_is_literal_glob`,
`_gate_config_violation`, and Gate 0 now REJECTS a touches entry that is a WILDCARD and overlaps a gate-config pattern unless the task
sets `allow_gate_config_changes: true` (a narrow literal entry, e.g. exactly `"pytest.ini"`, is never rejected, marker or not);
`finalgates.py` gains `ALLOWLIST_PREFIX = "allowed_"`, `severity()` treats any `allowed_`-prefixed kind as info (never blocking),
`_apply_allowlist` rewrites only currently-blocking findings whose path matches (never `scan_error`, never an already-advisory
finding), `run_gate4(..., allow_paths=())` (backward compatible default), `finalize()` passes `plan.gate4_allowlist` through only when
non-empty. `config.py` was NOT touched (confirmed not needed: the allowlist lives on the plan, not the project config).
`config/swarm.yaml` gained a documented comment block only, no live key. 30 new tests across three files; full suite 5354 passed, 2
skipped, 37 failed, all in CORE's `controller.py` files and two acceptance tests mid-edit; an isolated re-run of just those four files
gave 282 passed, 0 failed, confirming the collision was transient.

Ran Gate 4 against ASES's OWN repository (read-only, as the work order asked): 60 blocking `secret_in_tree` findings at HEAD
8ae7372, all sample/fake keys, all inside `tests/` or `docs/`. With `allow_paths=["tests/**", "docs/**"]` all 60 are excused and Gate 4
now PASSES. The 20 files it had to allowlist: `docs/architecture.md`, `docs/work-orders/r2_wp_questions.md`,
`docs/work-orders/r2_wp_report.md`, and 17 files under `tests/unit/` (bounds, cli_commands, critic, evals, events, fakes, finalgates,
gates, killswitch, mergeq, profiles, questions, recovery, report, review, sandbox, tamper).

Deviation: the work order said the marker is "the task's `gate_profile` OR a new explicit boolean"; the builder found no elaboration
of a `gate_profile`-based exemption anywhere in the blueprint or the work order's own detailed spec, and built only the boolean.
Flagged in case a `gate_profile`-based exemption was intended elsewhere.

Noticed:
- `tamper._config_reason` treats `package.json`/`Cargo.toml` as gate-config only by CONTENT (a diff hunk touching their test-related
  keys), not by path, so `GATE_CONFIG_PATTERNS` deliberately excludes them: the new Gate-0 check cannot pre-empt a broad touches over
  `package.json`, but the existing diff-time `gate_config_changed` finding still catches it at Gate 1, unaffected.
- `spec/requirements.yaml`'s existing ASES-QG-02 note flags a DIFFERENT, still-open gap (hashing/pinning a fixed list of CI/config
  files at approve time): this round's Gate-0 fix is complementary (plan-time touches-breadth), not a substitute; the register still
  needs a note reflecting this round's work.
- `cli.py`'s Lead-prompt text should mention the new `allow_gate_config_changes` task field and `gate4_allowlist` plan field (not
  built: TV does not own `cli.py`).

## Package AC-A: acceptance 22.3 (failure/fallback) and 22.9 (quota exhaustion) (`tests/acceptance/test_22_3_failure.py`, `test_22_9_quota.py`), done

Built: one card walked through all four failure classifications end to end via `controller.process_recovery` (rate-limit: Hermes
retries alone; infrastructure: three trips, resumed after backoff; auth: three trips, marks the credential unhealthy as a QUESTION,
answered; capability: a fresh-attempt card then a switch-model card pinned to a declared candidate, which succeeds), ending with
exactly 4 cards (no loss or duplication) and the ingested usage row naming the model/provider actually used (22.3); a 30-request daily
cap with 10 already spent parks an over-budget task (`scheduled`, `budget:`-prefixed reason) while a cheaper task finishes normally,
several idle passes show exactly one `card_parked_for_budget` event (no thrashing, the fake provider never started), the report shows
the reset time and the parked card, a snapshot scan finds no purchase language anywhere, and after `ledger._today` is monkeypatched
past midnight the card unparks and completes with the other task's state unchanged (22.9). 5 tests. Full suite 5361 passed, 2 skipped,
35 failed, all in CORE's `controller.py` files mid-edit; an isolated re-run one minute later gave 279 passed, 0 failed.

Report-back: `ledger.py` has NO injectable "today" (`ledger._today()` always calls `datetime.now(timezone.utc)` directly; `run_pass`'s
own injected `now` is never read by it); the builder monkeypatched `ledger._today` itself. Budget shape used: one capped provider
(`{"limits": {"per_day": 30}}`), one uncapped, `daily_reserve_percent`/`review_reserve_requests` zeroed so the arithmetic is just
cap-minus-used; usage seeded directly via `ledger.record_usage` rather than through a real worker session, for exactness.

Noticed (both worth checking):
1. IMPORTANT: a card that crashes ONCE with genuinely auth- or quota-shaped error text (short of tripping the 3-strike breaker) can
   get PERMANENTLY STUCK in `ready`, invisible to `recovery.py`. `FakeHermes._respawn_guard` (mirroring real Hermes) refuses to
   redispatch ANY card whose `last_failure_error` matches an auth/quota-shaped regex (429, 403, `auth\w*`, "unauthorized", "invalid api
   key"...), regardless of whether the breaker has actually tripped, and `recovery.process_failures` only ever examines `blocked`
   cards -- so such a card never becomes a question, never gets marked credential-unhealthy, nothing. The builder had to choose error
   text carefully (avoiding "429"/"rate limit") to keep the test working. This is a real gap between what the blueprint promises
   (401/429 always classified and acted on) and what a worker crashing with realistic provider error text would experience against
   real Hermes.
2. `usage.py`'s `_ingest_run` resolves the "pinned" model purely from the profile's ROLE (`provider_for_profile`), never from
   `card.get("model_override")`, while `recovery._current_model` (used for the NEXT switch decision) DOES check it -- the two modules
   read the override inconsistently. It still works today (a mismatch is detected and the real reported model is recorded), but worth
   confirming that inconsistency is intentional.

## Package AC-E: acceptance 22.10 (secret leak) and 22.12 (gate tampering) (`tests/acceptance/test_22_10_secrets.py`, `test_22_12_tampering.py`), done

Built: five 22.12 tests, one per real `fw.TAMPER_KINDS` value (`delete_test`, `skip_marker`, `or_true`, `outside_paths`, `untracked`),
each sent back before any reviewer runs, with the RIGHT finding kind asserted; three 22.10 tests (a secret-shaped value in a committed
file blocks Gate 1 and never leaks across events/gate_runs.detail/review_verdicts.metadata/card bodies/comments/both report renderings;
a key in the controller's own environment never leaks anywhere either; a whole file named like a secret, e.g. `id_rsa`, blocks Gate 1).
8 tests. Full suite 5350 passed, 2 skipped, 41 failed, all in CORE's `controller.py` files mid-edit; an isolated re-run gave 358
passed, 0 failed.

Deviations: the work order's guessed kind name "out_of_scope" is not real, the actual value is `outside_paths`; for the whole-file
secret case touches had to be `["a.py", "id_*"]` not `["a.py", "id_rsa"]` (naming the file literally would EXEMPT it from the
`generated_artifact` finding, since tamper.py only exempts a glob that names the artifact explicitly); had to move off a bare `"*"`
touches glob because TV's plan-time validation (landed concurrently) now refuses one broad enough to cover gate/CI config without
`allow_gate_config_changes: true`; each kind's worker is `fw.sequence(tampering_coder(kind), fw.questioner(...))` not the persona
alone, since the review lane's send-back and dispatch's redispatch happen in the SAME `run_pass` call and `tampering_coder`'s steps
are not safe to repeat on the same branch. The fake provider was never started (nothing here makes a model call).

CONFIRMED (independently reproduced, matching the register's own ASES-QG-02/QG-03 note and the MR builder's round 5 finding):
because `review.check_branch` runs the scope check BEFORE the tamper check, `gate_config_changed` and `assertion_weakened` can never
fire through this wiring -- anything that reaches tamper.py is already inside touches, hence "allowed". The `outside_paths` test is
caught purely by the scope check, confirming this a second, independent way.

Finding/event kinds the real code actually produces (table for future readers): delete_test/skip_marker/or_true -> `tamper_blocked` +
`gate1_recheck_failed`, detail kind `test_file_deleted`/`skip_marker`/`unconditional_pass`; outside_paths -> scope check only (
`gate1_recheck_failed`, BranchCheck kind `out_of_scope`, no `tamper_blocked`); untracked -> Gate 1 itself runs and is genuinely red (
`gate1_recheck_failed`, BranchCheck kind `gate1_red`, a real `gate_runs` fail row) -- the blueprint's prose reads as if a
missing-file check catches this kind, but the real mechanism is simply that the diff has no tamper finding and the seeded test was
never actually fixed. A whole secret-named file added is `generated_artifact`, NEVER `secret_added`: `secret_added` only fires for a
secret-shaped VALUE on an added line, via `tamper.check_range` (what the review lane and merge queue call); `gates.scan_for_secrets`
(which WOULD call a whole secret file `secret_added`) is a separate helper only reached from `mergeq.merge_task` at Gate 3, never from
`review.check_branch`.

## Package AC-G: acceptance 22.14 (plan rejection), 22.15 (idempotent re-run), 22.16 (data class) (three new files under `tests/acceptance/`), done

Built: Gate 0 rejects a cycle/missing-acceptance/missing-touches plan with exact errors, no FakeHermes needed; a 3-round critic loop
(replan, replan, ask_user -- `next_step`'s real bound needs a THIRD CHANGES_REQUIRED to cross `max_rounds=2`, not two) ending in
rejection with no card ever created (22.14); card creation idempotent twice and once more after the database is deleted, a fix card's
staleness after DB deletion documented as a real gap (below), a triage card untouched by ordinary `run_pass` calls, listed/validated/
archived for real once LED's `triage.py` landed mid-session, and a NEW bug found in `triage.promote_card` (below) (22.15); a
`ProjectConfig` cannot be built at all without a data class, `swarm run` refuses before dispatch with none declared, confidential with
only remote providers refuses at Gate P, private refuses a training-on-inputs provider at Gate P, and the per-pass budget gate has NO
data-class awareness -- a genuine, empirically confirmed gap (22.16). 14 tests. Full suite 5350 passed, 2 skipped, 41 failed, all in
CORE's `controller.py`/`mergeq.py` files mid-edit (the machine was heavily contended: ~39 python processes from concurrent builders,
19.5 minutes for this run); none of AC-G's own 14 tests failed, each file also passed standalone.

Two real, not-yet-fixed problems found:
1. NEW BUG in `triage.py` (package LED): `promote_card` calls `hermes.kanban_promote` UNCONDITIONALLY, including under `force=True`
   (force only skips `validate()`, not the Hermes call). Both `FakeHermes.kanban_promote` and, per the same source reading, REAL
   Hermes only promote from `todo`/`blocked`, never `triage`. So `promote_card` on a genuinely triage-status card ALWAYS raises
   `HermesCommandError` ("... is 'triage'; promote only applies to 'todo' or 'blocked'") -- it can never actually do its job.
   `archive_card` is unaffected. Not fixed (not AC-G's file); needs a follow-up in `triage.py` (likely: promote a triage card via
   `unblock`/`specify`-adjacent mechanics, or accept that "promote" for a triage card means something Hermes-side this function does
   not yet call).
2. `create_cards_from_plan`'s fix-card protection (the `CASE WHEN plan_tasks.fix_cards > 0 THEN plan_tasks.work_card_id ELSE
   excluded.work_card_id END` clause in its upsert) only fires when a `plan_tasks` row ALREADY EXISTS to read `fix_cards` from.
   Deleting the ASES database deletes that row, so a THIRD `create_cards_from_plan` call after a DB delete reverts `work_card_id` to
   the ORIGINAL, superseded work card and resets `fix_cards` to 0, even though a fix card is still the task's real current card on the
   (unchanged) board. The board stays internally consistent (no duplicate card), but ASES's own bookkeeping silently goes stale. Test
   reproduces the exact fix-card state `process_merge_queue` leaves and pins this precisely.

Confirmed empirically (not assumed): the data-class guarantee is enforced ONLY by `cli._estimate_lines` (calling
`policy.check_data_class` per task), invoked once at plan-approval time by `cmd_approve`/`cmd_critique`. `controller.process_budget_gate`
calls only `policy.check_budget` (a grep for `next_model|data_class` in `controller.py` returns nothing from that function); the only
other call site is `recovery.next_model` -> `recovery._adjust` -> `process_failures`, which DOES run every pass but only on the
switch-model branch, reached only after a card's SECOND capability failure -- never on ordinary dispatch of a healthy ready card. So
"they park instead" (private class, provider exhausted) is a ONE-TIME Gate P check today, not a per-pass re-check; a card whose
provider becomes or was always unsafe is never parked for that reason once past approval.

## Package CORE: project-scoped gate/merge records, the post-merge revert trigger, the CHANGES_REQUIRED dead end (`gates.py`, `bounds.py`, `mergeq.py`, `controller.py` and their tests), done -- the last of the ten round 6 packages

Built: `gates.run_gate(..., project=None)` (stores it; NULL when omitted, unchanged) and `last_gate_result(..., project=None)`
(NULL-tolerant scoping); `bounds.record_final_gate` now also stamps `gate_runs.project`; `bounds.final_gates_green(conn, head, *,
project=None)` (keyword-only, so every existing positional call keeps working) and `bounds.is_finished` now passes `project=
plan.project` internally, fixing the real bug (two projects sharing a database could read each other's final-gate rows);
`mergeq.RevertOutcome` (new: ok, commit_sha, detail, aborted) replaces `revert_merge`'s old bare bool, `reverted=1` written only on a
REAL success, a failed revert runs `git revert --abort` as best effort; `merge_task` threads `project` through to Gate 3 and to
`merge_records` writes; `controller.process_merge_queue` re-runs Gate 3 as `"gate3-postmerge"` on the new HEAD after every real
(non-no-op) coder merge (ASES-GIT-05): green is unchanged, red-but-revert-succeeds takes the ordinary fix-card/escalate path (factored
into a new `_handle_merge_failure` helper both failure paths now share), red-and-revert-also-fails records `integrity_violation` and
halts the run via a new `integrity` out-param threaded through `run_pass`; and the CHANGES_REQUIRED/BLOCKED dead end is fixed: a
well-formed non-PASS verdict on a `done` card now calls `kanban_reopen_review` + a `reviewer_completed_with_changes_requested` event
instead of being refused forever. 27 new tests plus one narrowed/renamed. Full suite: 5416 passed, 2 skipped, 0 FAILED (822s) -- clean,
all ten round 6 packages now integrate. Also explicitly re-ran `test_finalgates.py` (green) and `test_reconcile.py` (170 passed) after
the change, as its own work order asked.

IMPORTANT, answered the work order's own question: CORE did NOT touch `db.py` (the hard rule), but concluded the NULL-tolerant
approach, while sufficient for `gate_runs` (an append-only log, filtering genuinely disambiguates), is NOT a complete fix for
`merge_records`. `merge_records.task_key` is the table's ONLY primary key; SQLite's `ON CONFLICT` dispatches off the DECLARED
constraint, not a value passed at call time, so an upsert's conflict target cannot be made NULL-tolerant the way a SELECT's WHERE can.
Two projects reusing the same task key will have the SECOND project's candidate-build upsert land directly on the FIRST project's row,
overwriting candidate_sha/gate3_result/squash_commit/reverted/completed_at -- real data loss, not just an ambiguous read. What CORE
built (project-stamped writes, NULL-tolerant WHERE-scoped plain UPDATEs in `_fast_forward`/`revert_merge`) turns that specific
corruption into a safe no-op instead (a mismatched-project UPDATE matches zero rows) and lets `reconcile.py`'s existing
`merge_done_without_record` check surface the resulting inconsistency -- a real improvement, but explicitly not a fix for the upsert
collision itself.

**PROPOSED MIGRATION for a future round (architect decision, not built)**: change `merge_records`' PRIMARY KEY from `task_key` to
`(project, task_key)`, `project` NOT NULL going forward, backfilled from `plan_tasks` (which already has `(project, task_key)` as its
own primary key, so a join can supply the correct value for every existing row; a sentinel for anything unresolvable). Every
`INSERT ... ON CONFLICT(task_key)` in `mergeq.py` becomes `ON CONFLICT(project, task_key)`. This is a COORDINATED change, not a solo
fix: `finalgates.py`, `report.py`, `reconcile.py`, `hardening.py`, and `evalkit/codetasks.py` all currently read `merge_records` by
`task_key` alone (several via `.fetchone()`), and once `task_key` stops being unique on its own, every one of them needs a `project`
filter added too, or they read (or crash on) the wrong row the moment two projects genuinely share a task key.

Deviations: `Verdict` has no `summary` field (the work order's suggested `reason=verdict.summary` does not exist); used
`completed_run.get("summary")` instead. `review.py`'s Gate 1 call to `run_gate` was NOT given `project=` (confirmed by reading it: not
yet project-scoped, deliberately left for a later round). `events.record` was not touched (nothing in this change made it newly
urgent). nemotron returned 403 again; the builder did a manual self-review instead of a second-opinion pass.

Noticed: `hardening.py` (a different round 5 package) ALREADY reads `merge_records` with the identical NULL-tolerant
`(project IS NULL OR project = ?)` pattern CORE independently used -- cross-confirms this is the converged convention for this round.
The post-merge check is gated on `task.role == "coder"` exactly as instructed; if a non-coder branch ever carried a real diff (not
supposed to happen: review-only roles have no file-write tools by design) it would skip the post-merge re-check entirely.

## Architect fix: FakeHermes.fail_next after install() (`src/ases/fakes/board.py`, `tests/unit/test_fakes.py`), done

Applied directly (small, mechanical, well-understood after two independent reports). Root cause: `fail_next` validated a name
against `inspect.isfunction(getattr(_hermes, name))`, reading the HERMES MODULE's CURRENT attribute; once `install(monkeypatch)`
replaces `hermes.<name>` with a bound method of the fake, `inspect.isfunction` on it is always False, so `fail_next` rejected EVERY
name for any test built on `world`/`world_factory` (which always call `install()`). Fix: a module-level `_HERMES_PUBLIC_NAMES`
frozenset, captured once at import time (before any test's monkeypatching can happen), and `fail_next` now validates against that
frozen set instead of a live lookup. One regression test added (`test_fail_next_can_still_be_armed_after_install`), also checking
that a real method of the fake that is NOT a hermes-module function (`card`) is still correctly rejected. `test_fakes.py`: 159 passed.
`tests/acceptance/test_scenarios_demo.py`: 4 passed (unaffected). Full merged-tree suite run separately to confirm no wider impact.

# Round 7 (2026-09-22): the specify wiring, three real bugs, docs/policy scaffolding, plus wave 2 queued

Work orders: `r7_rules.md`, `r7_wp_*.md`. User decisions this round: "option A" for triage.promote_card (ASES may call Hermes's own
`specify`, only from there), "fix all the bugs", "build all" of the never-built register items.

## Package AC-D: acceptance 22.8 (merge conflict) (`tests/acceptance/test_22_8_merge_conflict.py`), done

Built: Gate 0 serializing overlapping touches with no explicit depends_on (confirming ASES-GIT-08's real behavior for this exact
shape); a real git-level conflict (two tasks with genuinely disjoint touches, one scripted with `fw.Do` to reset its branch to a
stale base before committing, honestly simulating a race Gate 0's static check cannot see) getting a fix card as an extra PARENT of
the merge card (confirmed `kanban_link(board, parent_id, child_id)` makes the fix card the parent, the merge card the child -- the
opposite of what the builder first assumed) and merging cleanly afterward; a seeded post-merge failure caught by CORE's round 6
`gate3-postmerge` re-check, reverted for real (`mergeq.revert_merge`), a fix card opened, and the integration branch proven green at
every OTHER commit by re-running the real gate command against each one in `git log` (not assumed). 3 tests, deterministic across 12
runs.

Hardest part solved: pre-merge and post-merge Gate 3 for one task's OWN merge run the identical commands against the literal same
commit SHA, so no git-content construction can force a genuine pre-pass/post-fail split for a single task's own merge. What
genuinely changes between the two calls is one SQLite column (`merge_records.squash_commit`, NULL then set, exactly at the
fast-forward); the seeded gate command reads that column from the real ASES database and only starts checking file content once it
is set, modelling "Gate 3 was green a moment before a regression became visible" honestly, with the seeded-bad commit explicitly
proven to fail the full-history green-walk (not a vacuous check).

CONFIRMED BLOCKING GAP (found while AC-D was setting up, before it could run anything): `FakeHermes.install()` now fails for EVERY
acceptance test, old and new alike, with `AttributeError: FakeHermes has no kanban_specify(): add it ... to ases.fakes.board`.
Package SPECIFY (running in parallel this round) added `hermes.kanban_specify`, but its work order never mentioned
`src/ases/fakes/board.py`, so nothing in round 7 is assigned to add the matching fake method. **This must be fixed (a
`kanban_specify` method on `FakeHermes`) before any acceptance test, existing or new, can pass again.** AC-D worked around it with a
scratch-only, never-shipped monkeypatch to verify its own logic, and left `board.py` untouched per the standing rule (report a
needed change in someone else's file, never make it).

Noticed: the blueprint's own text (ASES-GIT-09) describes a reconciliation card for a NEUTRAL profile with BOTH conflicting cards as
parents for a true two-sided conflict; the real `_handle_merge_failure` only ever builds the simpler single-parent fix-card path.
Not news: `spec/requirements.yaml`'s own ASES-GIT-09 row is already `partial` and already names this gap.

## Package SPECIFY: kanban_specify wiring, Option A (`src/ases/hermes.py`, `src/ases/triage.py`), done

Built: `hermes.SpecifyResult` (ok, reason, new_title) and `hermes.kanban_specify(board, card_id, *, author=None, timeout=120)`
(120s default timeout, deliberately does NOT reuse `_kanban`/`_kanban_json` since a Hermes-side "no" from the auxiliary model is a
NORMAL outcome on exit code 1 with parseable JSON, not a failure to raise on -- only an unparseable result or an unexpected exit
code raises `HermesCommandError`); `triage.promote_card` now calls `kanban_specify` instead of the always-broken `kanban_promote`,
gains an `author` parameter, records the promote decision only on `ok=True`, raises `TriageError` with Hermes's own reason on
`ok=False` and records nothing, and `force=True` is now documented as bypassing only ASES's own `validate()`, never Hermes's
auxiliary-model judgment. 68 new/changed tests in the two owned test files, both fully passing.

Confirmed via the real Hermes source, with exact citations: `ok:false` is a NONZERO (1) CLI exit code with the JSON still on stdout,
not a zero-exit JSON field (`kanban.py:1221-1222`); the exact field names (`task_id`, `ok`, `reason`, `new_title`,
`kanban_output.py:81-82`); the real failure reason strings Hermes emits (`kanban_specify.py`: "unknown task id", "task is not in
triage", "auxiliary client unavailable", "LLM error: <type>", "LLM returned an empty response", "LLM response missing title and
body", "task moved out of triage before promotion" -- a race); and that NO CLI path exists for a manual title/body that skips the
auxiliary model (`_triage_sweep_args` exposes only `task_id`, `--all`, `--tenant`, `--author`, `--json`) -- ASES's use is inherently
auto/auxiliary-LLM only, confirming the user's decision was the only real option.

Correction: the work order assumed the stale "ASES never calls specify" sentence was in `r2_rules.md`'s "What already exists"
section; it is actually in `r5_rules.md:35`. SPECIFY added the authorized correction to `r2_rules.md` anyway (the first file every
builder reads) but could not touch `r5_rules.md` (outside its file list) -- the architect should fix that sentence directly.

CONFIRMED the blocking gap AC-D found independently: adding `hermes.kanban_specify` makes `FakeHermes.install()` raise
`AttributeError` for every test that installs it (`board.py`'s own design: it fails loudly for any hermes.py function it has no
matching method for), breaking 22 acceptance tests plus 3 `test_fakes.py` tests that assert full coverage. SPECIFY sketched the fix
(a `kanban_specify` method on `FakeHermes`, modeled on `kanban_promote`/`_promote_task`, returning `ok=False` rather than raising
`_Refused` for a normal decline) but did not apply it, since `fakes/board.py` is outside its file list. **Architect must fix this
before anything else runs the acceptance suite.**

Two failures in the full-suite run were confirmed NOT SPECIFY's: `test_policy.py` (4, a concurrent edit by package POLICY mid-run,
confirmed by the renamed test functions already on disk) and `test_recovery.py` (1, owned by FIXES, not investigated per the
standing rule).

## Architect fix: FakeHermes.kanban_specify (`src/ases/fakes/board.py`), done

Applied directly (small, well-specified by two independent builders: AC-D found the blocking gap, SPECIFY sketched the fix).
`FakeHermes.kanban_specify(board, card_id, *, author=None, timeout=120) -> hermes.SpecifyResult`: an unknown card or one not in
`triage` returns `ok=False` with the real Hermes reason string, WITHOUT raising (matching `hermes.kanban_specify`'s own contract
that a decline is a normal result, not a `_Refused`/`HermesCommandError`-shaped failure -- the one wrapper this round that does NOT
go through `_cli`'s exception machinery for its "no" case); a structurally valid triage card always succeeds (no real auxiliary
model to consult), moving `triage` -> `todo` and recording a `specified` event. `test_fakes.py`: 159 passed (confirms
`test_every_public_hermes_function_has_a_fake_with_the_same_signature` now covers it). Acceptance suite re-run in progress to
confirm the 22 previously-broken tests are fixed.

## Architect follow-up: kanban_specify fix confirmed against the acceptance suite, one stale test updated

Re-ran `tests/acceptance/` after the `FakeHermes.kanban_specify` fix: 39 passed, 2 failed (down from the 22+9 broken by the gap).
Both remaining failures are EXPECTED, not new bugs: they are round 6's own acceptance tests that were written to explicitly
DOCUMENT the two bugs round 7 is fixing.
- `test_22_15_finding_triage_promote_card_does_not_work_on_a_genuinely_triage_status_card` documented the exact
  `triage.promote_card` bug SPECIFY just fixed (Option A). Since the fix already landed in the working tree, this test's premise is
  now wrong. UPDATED directly (renamed to `test_22_15_promote_card_now_works_on_a_genuinely_triage_status_card`, asserts the FIXED
  behavior: the card lands in `todo`, per real Hermes's own `kanban_specify` mechanism, not `ready`) and confirmed passing.
- `test_22_16_the_per_pass_budget_gate_has_no_data_class_awareness_this_is_a_genuine_gap` documents the per-pass data-class gap
  package FIXES is fixing (bug 2). Left UNCHANGED for now, since FIXES has not landed yet: once it does, this test needs the same
  treatment (its `assert parked == []` will need to become `assert parked == ['T1']` with the right reason, since the gap it
  documents will no longer exist). This is architect follow-up work, not any single round 7 package's job, since the file belongs
  to round 6's (already committed) AC-G package.

## Package FIXES: three real controller/recovery bugs (`controller.py`, `recovery.py` and their tests), done

Built: `controller._board_current_work_card(board, project_id, plan, key, *, conn)` (asks the BOARD, not the ASES database, which
card is a task's real current one, by reading the merge card's `_parents` lineage and each member's OWN `created_at` field -- see
the critical finding below on why `created_at` and not id/list order); `create_cards_from_plan` uses it whenever the local
`plan_tasks` row is missing or names no card (bug 1: fix and retry cards are no longer forgotten or duplicated after a database
delete; it also recovers the `fix_cards` budget counter from board lineage, beyond the literal ask, since it's the same read).
`_affordable_now` gains an optional `project` parameter and checks `policy.check_data_class` BEFORE the budget check, parking with
a `"data class: ..."` reason on violation, deliberately kept OUT of `_PARK_PREFIXES` so `process_unpark` can never auto-resume a
data-class park (bug 2: pinned by a dedicated test). `recovery._recover_task`'s gate widened: a `ready` card whose latest run is
AUTH- or QUOTA-classified, past a 30-second settle window (`recovery.READY_RESPAWN_SETTLE_SECONDS`, reusing
`INFRA_BACKOFF_BASE_SECONDS`), is now recovered exactly like a blocked one; every other failure kind on a `ready` card is left
alone (bug 3). Each owned file run standalone: 128/128, 161/161, 296/297 (585/586; the one failure is POLICY's concurrent edit to
`policy.check_data_class`, confirmed via `git stash` isolation to have zero of FIXES's own changes present, not investigated
further per the standing rule).

CRITICAL FINDING for anyone building board-native recovery logic in the future: real Hermes card ids are RANDOM
(`"t_" + secrets.token_hex(4)`), not sequential, and `kanban_show`'s `_parents` comes back alphabetically by id, which only
happens to equal creation order on the FAKE board (whose ids ARE sequential). A board-native "which card is current" reader that
trusted id or parent-list order would pass every test against the fake and silently pick the WRONG card on a real board. FIXES's
own `_board_current_work_card` deliberately uses each candidate's own `created_at` field instead, precisely to avoid this trap.

Bug-specific answers: bug 1's board signal is the merge card's parent lineage, disambiguated by `created_at`; bug 2 never
auto-unparks a data-class violation (separate prefix, deliberately excluded); bug 3's 30-second settle window reuses the existing
infra-backoff base, and real Hermes's own `DEFAULT_CRASH_GRACE_SECONDS` independently agrees at 30s for a comparable judgment.

Noticed, not fixed (both are stale-test-needs-updating items in round 6's AC-G file, `tests/acceptance/test_22_15_idempotent.py`,
which no round 7 package owns):
1. `test_22_15_a_fix_card_is_forgotten_after_the_database_is_deleted` builds its fix card with `parent=[t1.work_card_id]` but never
   calls the `kanban_link(fix_card, merge_card)` step `_handle_merge_failure` always performs in reality -- the exact signal the
   fix reads. Because the fixture is missing that link, the test still passes (falls back to the only lineage member) but no
   longer genuinely exercises the fixed scenario. Needs the missing `kanban_link` call added to actually verify the fix.
2. `test_22_16_the_per_pass_budget_gate_has_no_data_class_awareness_this_is_a_genuine_gap` (already flagged by the architect
   above) needs updating now that bug 2 is fixed: `parked == []` should become `parked == ['T1']` with the `data class:` prefix.

## Architect follow-up: the 22.16 data-class test updated now that FIXES's bug 2 has landed

`test_22_16_the_per_pass_budget_gate_has_no_data_class_awareness_this_is_a_genuine_gap` documented exactly the gap FIXES just
closed. Updated directly (renamed to `..._now_parks_a_card_whose_provider_turned_data_class_unsafe`, asserts the FIXED behavior:
`parked == ["T1"]`, the card lands `scheduled` with a `"data class:"`-prefixed reason event, and a follow-up `process_unpark` call
confirms it is NEVER auto-resumed, per ASES-PRV-03). Both this file and `test_22_15_idempotent.py` now pass in full.

## Package POLICY: docs scaffolding, data-policy verification, key-pool doctor checks (`cli.py`, `policy.py`, `config.py`, `doctor.py`), done

Built: `cli.cmd_plan`'s Lead prompt now asks for `docs/ases/contracts/`, `docs/ases/decisions/`, `AGENTS.md` (ASES-GIT-15);
`cli._scaffolding_warnings(repo)` (WARN, never refuse, for each missing/empty path) printed by `cmd_approve` before the y/N prompt;
`policy.check_data_class(..., *, verified_at=None)` now requires an explicit, non-empty `verified_at` for `private`/`confidential`
(a compatible policy string alone is no longer enough -- ASES-PRV-04's "explicitly verified"), `public` unaffected;
`config._validate_data_policy_verification_fields` (the two new optional provider fields, `data_policy_verified_at` an ISO date,
`data_policy_source` a string, validated only when present); `doctor._check_key_pooling` (WARN when two DIFFERENT providers share a
`key_env`, never flags one provider used by multiple profiles, never prints a secret value). `config/models.yaml` and
`docs/operations.md` documented. 26 new/changed tests across four files, all passing standalone. Full suite (after the transient
FIXES/SPECIFY collision cleared): 756 passed in the affected modules, 1 reproducible failure (below), confirmed to be a real
consequence of this package's own change, correctly left for the architect since it needed `recovery.py` (owned by FIXES, already
finished by the time this was found).

CONFIRMED FACT (read from the installed Hermes source, with citations): Hermes DOES auto-load `AGENTS.md` from the working
directory at session start (`agent/prompt_builder.py:1696`, `build_context_files_prompt()`; called from
`agent/system_prompt.py:640-651`), exactly as the blueprint claims -- but it is FIRST-MATCH-WINS against `.hermes.md`/`HERMES.md`/
`AGENTS.override.md`: if a project ever has one of those, its `AGENTS.md` is silently NOT loaded. Worth knowing for later.
`key_env` (not `hermes_secret_ref`/`secret_ref`) is the real field name for "which environment variable holds this provider's key".

CONFIRMED REGRESSION, not fixed by POLICY (needed `recovery.py`, owned by FIXES, already finished): `recovery.py`'s `next_model`
calls `policy.check_data_class(data_class, provider, declared)` WITHOUT `verified_at`, so once POLICY's stricter check landed, EVERY
candidate model is now treated as a violation for `private`/`confidential` projects, even a fully compatible one --
`next_model` silently returns `None` instead of a real switch target, breaking
`test_recovery.py::test_next_model_never_leaves_the_data_class_when_it_is_given_one`. Fix: thread `verified_at=
(providers.get(provider) or {}).get("data_policy_verified_at")` into that call, mirroring the existing `declared =
row.get("data_policy") or (providers.get(provider) or {}).get("data_policy")` fallback immediately above it. THIS IS A REAL BUG
THAT MUST BE FIXED before committing round 7.

Noticed, not fixed (out of scope for this package): `cli._estimate_lines` only reads the PROVIDER-level `data_policy`/
`data_policy_verified_at`, never a per-model override (`models[].data_policy` can override the provider's policy per
`docs/operations.md`); a separate, pre-existing gap.

## Architect fix: recovery.next_model's ASES-PRV-04 regression (`src/ases/recovery.py`, `tests/unit/test_recovery.py`)

Applied POLICY's own prescribed fix directly: `next_model` now threads `verified_at=(row or provider's own)
data_policy_verified_at` into its `check_data_class` call, mirroring the existing `declared` policy-string fallback immediately
above it. Updated `test_next_model_never_leaves_the_data_class_when_it_is_given_one`'s fixture to add a
`data_policy_verified_at` field where the test expects a switch to succeed, and added one new assertion proving the reverse: a
policy-compatible but UNVERIFIED candidate is correctly treated as unsafe, not silently allowed. `test_recovery.py`: 297 passed.

## Package ROLES2 (round 7 wave 2): greenfield bootstrap and the Tester role's hardcoded-role bug

Read the REAL current `controller.py` (1939 lines) and `cli.py` (1619 lines) fresh off HEAD `a98b95e` rather than trusting the
work order's snapshot, per its own warning, and confirmed the working tree was clean before starting.

Built `controller.ensure_repo_bootstrapped(repo, integration_branch, *, conn=None) -> bool` (ASES-GIT-10): detects a truly empty
repository (no `.git`, or `.git` with zero commits), creates the integration branch and one commit carrying whatever is already
on disk plus a `.gitignore`/README if missing, with a per-invocation `-c user.name=... -c user.email=...` identity, never a
persistent git config. Returns `True` only when it created something; never touches a repository with real history, whatever
branch it is on. Records `repo_bootstrapped`/`repo_bootstrap_error` events when given a `conn`. Wired into `cli.cmd_plan`,
immediately after `_open_conn`, before the Lead is ever invoked: the earliest real touch-point, and the only one that avoids
double-wiring, since `cmd_approve`'s `publish_plan` already refuses a repository not on the integration branch and so needs the
bootstrap to have already happened.

Found and fixed the bug the investigation surfaced (ASES-QG-05, ASES-ROL-09): `controller.py` hardcoded `role == "coder"` (or its
negation) in what was thought to be six places to mean "the only role that produces a real commit", silently mistreating a
Tester role's card the moment a project enabled one. Added `_COMMITTING_ROLES = frozenset({"coder", "tester"})` and rewrote the
five real sites (`_finish_instructions`, the review-reserve budget check, `process_merge_queue`'s verdict-validation gate,
`mergeq.merge_task`'s `allow_empty=`, the post-merge Gate 3 recheck) to check membership instead of equality. Empirically proved
the old bug was worse than "a tester's card fails to merge": `mergeq.merge_task` runs Gate 3 unconditionally on any real diff
regardless of role, so under the old code a tester's real commit would still have merged, just with the reviewer-verdict check,
the Gate 1 recheck and the post-merge Gate 3 recheck all silently skipped -- unreviewed, unchecked work merging silently, not
work failing to merge. Reverted the five sites, confirmed 7 tester-focused tests fail against the old code and pass against the
fix, then restored and reconfirmed byte-identical.

Confirmed `plan.py` needs NO change: Gate 0 already accepts a `tester` role automatically via `known_roles=set(project.roles)`;
added a confirming test instead of touching the module. Extended `cmd_plan`'s prompt to offer `role: "tester"` (gated on
`"tester" in project.roles`) and to instruct the Lead explicitly and forcefully to write a `depends_on` pointing parallel work at
a scaffold task, after proving with a new test that Gate 0's touches-overlap serialization does NOT by itself guarantee this
(ASES-GIT-11): a scaffold task's touches essentially never literally overlaps an ordinary task's touches, so the "parallel work
starts only after the scaffold is merged" guarantee rests entirely on the Lead's own `depends_on`.

+17 tests in `test_controller.py` (plus a fix to `_setup_one_task`'s `known_roles=set(ROLES)`, which silently ignored its own
`roles=` parameter), +4 in `test_cli_commands.py`, +4 in `test_plan.py`. Baseline (HEAD, before any edit): 5478 passed, 2 skipped.
Final full suite at report time: 5501 passed, 2 skipped, 0 failed (18m32s).

Found, not fixed (not this package's file, flagged for the architect): `src/ases/reconcile.py:651` has the same-species bug,
`elif row["role"] != "coder":`, in `_done_without_record` used during reconcile-on-start.

## Architect follow-up: two agents dispatched in parallel on ROLES2's own findings

Rather than fix and verify ROLES2's own diff solo, two agents ran concurrently: one fixed the `reconcile.py` bug ROLES2 flagged
as out of scope, one independently re-verified ROLES2's diff and swept the rest of the repository for anything missed.

**Reconcile fix** (`src/ases/reconcile.py`, `tests/unit/test_reconcile.py`): confirmed no circular import risk (`controller.py`
imports bounds/config/db/events/gates/guards/hermes/intents/leases/mergeq/plan/policy/questions/recovery/report/review/usage,
none of which import `reconcile`; `reconcile.py` itself only imports events/hermes/intents), so `reconcile.py` now does
`from . import controller as controller_mod` and reuses `controller_mod._COMMITTING_ROLES` directly rather than duplicating the
frozenset. Line 651's `elif row["role"] != "coder":` became `elif row["role"] not in controller_mod._COMMITTING_ROLES:`, keeping
the escalate branch as the correct complement. Confirmed by grep this was the file's only occurrence of the bug pattern. New test
`test_b_a_tester_merge_card_that_says_done_with_no_commit_is_blocked_and_nothing_is_written`, proven with the same before/after
technique: reverted, the new test failed (`report.blocked == []` instead of `['merge_done_without_record']`, the tester card
silently no-op'd); restored, it and the two pre-existing coder/reviewer tests all passed. Full suite: 5505 passed, 2 skipped,
0 failed (21m55s).

**Independent verification**: re-derived (not just re-read) `ensure_repo_bootstrapped`'s safety properties directly from the
code -- confirmed it checks for an existing commit via `git rev-parse -q --verify HEAD` and returns before any write when one
exists, and that its only identity-bearing git call is scoped with `-c user.name=... -c user.email=...` rather than a global
config, with a new test asserting `"[user]" not in (repo/".git"/"config").read_text()`. Corrected ROLES2's own site count: only
five hardcoded-role comparisons exist in `controller.py`, not six -- one of the six originally listed was inside a docstring, not
code. Swept `mergeq.py`, `review.py`, `evals.py`/`evalkit/`, `recovery.py`, `plan.py`, `gates.py`, `finalgates.py`, `tamper.py`,
`config.py`, `critic.py`, `doctor.py`, `hardening.py`, `profiles.py`, `usage.py` for the same pattern: none found (the only other
`"coder"` occurrences are `evals.py`'s unrelated `ROLE_TASKS` eval-class mapping and `profiles.py`'s coder-1/2/3 profile naming).
Checked every acceptance and unit test touching `cmd_plan`, `tester`, or empty-repo bootstrapping for staleness against the new
behavior: found none needing a fix. Independent full suite run: 5505 passed, 2 skipped, 0 failed (21m45s), matching the reconcile
agent's own count.

Architect's own final clean run (no concurrent editors) before committing: see the commit message for the exact number.

## ASES-CFG-05 close-out: an audit sweep, a doc-ordering fix, and two build-and-verify pairs on real hermes launches

An inspection audit of the three remaining not_covered rows (ASES-ARC-01, ASES-DOC-03, ASES-CFG-05) found ARC-01 satisfied by
design (closed same day, no code, see the register note for the row-by-row citations against blueprint table 2), DOC-03 honestly
partial (build order compressed across rounds, no exit test run for real yet, by disclosed policy), and one mechanical defect in
this very file's sibling, `docs/architecture.md`: a 2026-09-18/19 section had drifted to sit after three much later round
sections, because new sections were repeatedly inserted before a fixed anchor without checking that anchor's own place in time.
Fixed directly the same day: the misplaced 64-line block now sits right after the section it actually continues from, confirmed
by a line-count-preserving diff (64 inserted, 64 deleted, same total).

For CFG-05's live half, the audit also surfaced a concrete, previously-unlisted gap: `hermes.py`'s `_run` and `evals.py`'s
`_run_process` (the one function that makes a real, live one-shot hermes call for an evaluation) both launched the real hermes
CLI with the parent's full, unfiltered environment. Two build-and-independently-verify pairs closed this, run back to back:

**Pair 1** (`hermes.py`, `evals.py`, `evalkit/codeeval.py`): the builder's first attempt defined the scrub as a new public
function of `hermes.py` itself and broke 50 tests, because `ases.fakes.board.FakeHermes.install` replaces every public function
DEFINED in that module and `test_fakes.py` requires a fake with the same signature for each one; an environment scrub is not a
Hermes call and must never be faked. Corrected to a new module, `src/ases/procenv.py` (stdlib-only, zero ASES imports, so the
lowest module that starts a process can use it), re-exported from `hermes.py` (a re-export keeps `__module__ == "ases.procenv"`,
invisible to the fake-completeness check). `evals.py` and `evalkit/codeeval.py` (which used to carry its own, near-identical
credential regex) both now build on the same one definition. Discriminating proof: reverted, both new tests fail with the
credential leaking through; restored, both pass. Independent verifier ran its own adversarial test (three credential-shaped
names, both real and timeout code paths) and confirmed no leak, no missed site, and a clean independent full suite:
5515 passed, 2 skipped, 0 failed.

**Pair 2** (`cli.py`, `critic.py`, `profiles.py`, `sandbox.py`): closed the three remaining real hermes-launch sites the first
pair's builder had spotted and flagged but left out of scope (a spawned follow-up suggestion the user started): the `swarm plan`
Lead call, the `swarm critique` reviewer call, and the real `hermes profile create` call. The first two got the same one-keyword
`env=hermes_mod.scrubbed_environ()` addition. `profiles.py`'s case needed a real design decision: `sandbox.default_runner` (the
injectable runner `profiles.py` calls through) is also the instrument of the Docker sandbox's own key-leak probes
(`key_visibility_test`, `exfiltration_probe`), which need to see a REAL, unscrubbed environment to detect a regression in
`docker_run_argv`'s own credential filtering -- scrubbing `default_runner` globally would have blinded the very probes that exist
to catch this class of bug. Fixed instead with a new `_hermes_runner` wrapper that composes a scrubbed call on top of
`default_runner`'s new optional `env=` parameter (default `None`, meaning "inherit, exactly as before," so every other caller of
`default_runner` is untouched), made the default for `profiles.py`'s real init path. A hard constraint that ruled out a third
option (growing the runner `Callable` contract to accept `env`): `test_profiles.py` asserts the module never contains the string
`subprocess`, and every injected fake runner in both test files takes exactly `(argv, timeout)`. Discriminating proof for all
three sites via `git stash push -- <file>` / `git stash pop`, each showing the exact old failure and the fix restoring a pass.
Independent verifier ran its own adversarial test through `critic.run_critique` from the top of its real call path (not just
`default_invoke` directly), covering the reviewer's repair-call branch too, and confirmed no fourth unscrubbed site exists: all
five `hermes_path()` call sites in `src/ases` (hermes.py, evals.py, cli.py, critic.py, profiles.py) are now covered.

Both independent verifiers, working from different agents on different days' worth of investigation, converged on the exact same
out-of-scope finding: `gates.py`'s `_run_commands` still runs the project's own gate commands with `shell=True` and the
operator's full, unscrubbed environment, so a model-authored test committed to the repo could read a key from the controller's
own shell during a gate run. Same family as ASES-SEC-01/03. Neither patched it (a real design decision -- COMSPEC/PATH must
survive, and sandbox runners already bypass this path -- not a two-line fix), correctly reporting rather than patching under
time pressure, per instruction. Also flagged, not a bug: the shared credential pattern also strips `SSH_AUTH_SOCK`/`XAUTHORITY`
on POSIX and `SESSIONNAME` on Windows from every scrubbed launch, which would affect an ssh-agent-backed git push from a Lead's
terminal tool; a human decision for later, not touched.

Architect's own final clean run on the fully combined tree (no concurrent editors) before committing: 5512 passed, 2 skipped,
0 failed.

## Round 8 (2026-09-27): housekeeping, then gate commands stop seeing the operator's credentials

Work orders: `r8_rules.md`, `r8_wp_housekeeping.md`, `r8_wp_gateenv.md`. One workflow: HK-PATH and HK-GAPS in parallel (Sonnet builders, HK-PATH checked by a Haiku checklist, HK-GAPS by an independent Sonnet review, one fix round, and a re-review), then GATEENV (Sonnet build, independent Sonnet review with a nemotron second opinion, Haiku live verification last) with a read-only Sonnet sweep of the sibling git-hook exposure alongside. Every report below is verbatim.

### HK-PATH builder

HK-PATH (round 8 housekeeping) - builder report

Scope: spec/check_requirements.py and its test file only, per r8_wp_housekeeping.md and r8_rules.md. Did not touch HK-GAPS (docs/architecture.md), which is another agent's package.

What was wrong: spec/check_requirements.py --check (no flags) failed with "blueprint docx not found: C:\Users\masoo\OneDrive\Desktop\ASES_Swarm_Implementation_Blueprint_v1.2.docx". The docx had moved to C:\Users\masoo\OneDrive\Desktop\AISES\ASES_Swarm_Implementation_Blueprint_v1.2.docx and DEFAULT_DOCX still pointed at the old location.

What I changed in spec/check_requirements.py:
1. DEFAULT_DOCX now points at the new AISES subfolder path.
2. Added an ASES_BLUEPRINT_DOCX environment variable and a new resolve_docx_path(cli_docx) helper implementing precedence: --docx flag, then the env var, then DEFAULT_DOCX. main() now computes docx_path once via resolve_docx_path(args.docx) and passes it to both cmd_check and cmd_regenerate. The --docx argparse default was changed from DEFAULT_DOCX to None so resolve_docx_path can tell an explicit flag apart from nothing given.
3. The not-found error raised by extract_appendix_f now names the path it tried and tells the reader to use --docx PATH or set ASES_BLUEPRINT_DOCX; exit code on this path is still 2 (unchanged control flow, only the message text and args.docx replaced by the resolved docx_path).
4. Documented the precedence order and the env var usage in the module docstring.

Tests added: tests/unit/test_check_requirements.py (new file; none existed before - grepped first and confirmed). The module has no __init__.py and spec/ is not on pythonpath (only src/ is, per pyproject.toml), so the test file loads spec/check_requirements.py directly from its file path via importlib.util.spec_from_file_location rather than importing it by dotted name. 10 tests: DEFAULT_DOCX value, resolve_docx_path under all three precedence combinations plus an empty-env-var edge case, extract_appendix_f's not-found message contents, and three end-to-end main() invocations (cli flag, env-var fallback, cli overriding env var) all using tmp_path files that are intentionally never created, so no real docx is read anywhere in the suite - exactly the "exercise path resolution, not extraction" instruction in the work order.

Verification:
- pytest -q --tb=short tests/unit/test_check_requirements.py through the quiet wrapper: 10 passed, run twice (immediately after writing, and again after the before/after proof), both green in about 0.5s wall / 0.12-0.13s test time.
- python spec/check_requirements.py --check with no flags: "OK: 103 requirement IDs in sync between ASES_Swarm_Implementation_Blueprint_v1.2.docx and requirements.yaml" (exit 0), matching the work order's required outcome exactly.
- Before/after proof done the safe way the rules require (no bare git stash, since other agents may have uncommitted edits in this tree): copied my fixed spec/check_requirements.py aside to the session scratchpad, restored the old committed version with "git show HEAD:spec/check_requirements.py > spec/check_requirements.py", reran the check and got the original failure message verbatim (exit 2), then copied the fixed file back. git diff --stat and git status --porcelain were byte-identical before and after this sequence (only spec/check_requirements.py modified; tests/unit/test_check_requirements.py and the pre-existing untracked r8_*.md work-order files untouched), confirming nothing else in the shared tree was disturbed.
- Grepped the repo (excluding .venv) and C:\Users\masoo\ases-workspaces\tools for any other live reference to the stale full path (OneDrive\Desktop\ASES_Swarm... without the AISES subfolder). Found only two: the historical description inside docs/work-orders/r8_wp_housekeeping.md (a work order, not code, not mine to touch, and correctly describing the bug that was observed) and the line in spec/check_requirements.py itself, which I fixed. CLAUDE.md, docs/architecture.md, and spec/requirements.yaml only reference the blueprint's filename (no directory path), so they were not stale and needed no change. No matches at all under ases-workspaces/tools.
- Confirmed no source under src/ases imports or shells out to check_requirements.py (grepped src/, no hits), so this is a self-contained fix with no other callers to update.
- Scanned both files I touched for the banned em dash and section sign characters: none found.

Hard rules honored: never called a real Hermes, a real model provider, or Docker (this package never touches Hermes at all); never ran git commit or git push; never ran a bare git stash (used a plain file copy plus git show HEAD:<path> instead, restricted to the one file I own); ran only my own test file and the no-flag drift check, not the full suite, per the work order's instruction that this change is isolated.

Nothing found but not fixed for this package. (HK-GAPS's docs/architecture.md drift is out of scope and owned by another agent.)

### HK-PATH verifier (Haiku checklist)

All 6 checklist tests pass individually, but one critical deliverable issue: The test file tests/unit/test_check_requirements.py exists and all 10 tests pass, but the file is NOT committed to git (git status shows ?? - untracked). It sits outside version control at C:\Users\masoo\ases\tests\unit\test_check_requirements.py.

Per the build spec, the test file should be added (implying committed). Untracked test files mean the deliverable is incomplete - CI and other developers cannot access this code. Other test files in tests/unit/ are tracked normally in git; this one standing out as untracked indicates an oversight in the build completion.

The implementation is correct: DEFAULT_DOCX points at the new AISES path, env var override works, CLI flag precedence is correct, error messages name the path and explain both override mechanisms, exit code is non-zero. All code passes its tests and has no non-ASCII characters.

Minor note: The actual output includes filenames in the OK message ("OK: 103 requirement IDs in sync between ASES_Swarm_Implementation_Blueprint_v1.2.docx and requirements.yaml") rather than the minimalist format the spec showed ("OK: 103 requirement IDs in sync"), but this is supplementary helpful detail that does not violate the requirement.

Recommendation: FIX-FIRST. The test file must be added to git (either commit it or remove it). Otherwise the deliverable is incomplete and CI/other users cannot run these critical tests.

### HK-GAPS builder

## HK-GAPS build report (docs/architecture.md, "Known gaps" and "Running things" sections only)

Repo: C:\Users\masoo\ases, branch master. No commits made, nothing pushed, no bare git stash used, no real Hermes/model provider/Docker calls made. `swarm doctor` was never run (it shells out to a real hermes.exe on PATH on this machine); I verified its command syntax by reading src/ases/doctor.py and src/ases/cli.py instead.

### What I did

Read docs/work-orders/r8_rules.md and docs/work-orders/r8_wp_housekeeping.md first, as instructed. Then, for every bullet in "Known gaps (tracked, not hidden)", checked the claim against src/ases/ (via Grep/Read, and code-review-graph's query/semantic-search tools where they saved a lookup -- the graph itself was not used as the final source of truth for anything load-bearing, per the work order's own warning that it predates recent commits), against spec/requirements.yaml's status/note for every register ID the bullet names or implies, and, where relevant, against a real installed Hermes config file. Also checked every command in "## Running things" actually works on this machine.

### Table: every original bullet, classification, evidence, what changed

| # | Original bullet (paraphrased) | Classification | Evidence (file:line or register ID + status) | What I changed |
|---|---|---|---|---|
| 1 | `glm-5.3-thinking:free`'s context length not declared in config/models.yaml; confirm before pinning in Phase 2 | **Stale** | config/models.yaml has no `glm` model row at all (grep confirms only a historical comment); `swarm models` output shows lead pinned to `xkiro/qwen/qwen3.8-max:free`, `context=1050000 smoke=pass pinned`; register `ASES-MOD-02` (`in_progress`) still describes the old gap, itself drifted | Struck through with a 2026-09-27 dated note explaining the model row is gone entirely, not just demoted, and naming the model that replaced it |
| 2 | Blueprint Appendix B names the UnoRouter secret `OPENAI_API_KEY`; the real installed config.yaml uses `HERMES_CUSTOM_UNOROUTER_API_KEY`, which is "what config/models.yaml uses here"; since 2026-09-19 `OPENAI_API_KEY` is also `lead`'s new home on the `openai` provider | **Partly true** | The core naming-difference observation is still true: the machine's default installed Hermes config.yaml (`%LOCALAPPDATA%\hermes\config.yaml`) still shows `key_env: HERMES_CUSTOM_UNOROUTER_API_KEY`. But config/models.yaml has zero `unorouter` or `openai` provider rows left (grep confirms), so "that's what config/models.yaml uses here" and "`lead`'s new home [is `openai`]" are both stale; `lead` is on `xkiro` today | Rewrote in place: kept the still-true naming-coincidence point, flagged the two stale sub-claims, stated what config/models.yaml actually uses today |
| 3 | ~~"No Hermes profiles exist yet"~~ (already struck through) | **Still true** (as a correctly-struck bullet) | register `ASES-ROL-10` (`covered`): "all three profiles created fresh" | None |
| 4 | ~~"Gate P plan publication (ASES-ARC-09)..."~~ (already struck through) | **Still true** (as a correctly-struck bullet) | register `ASES-ARC-09` (`covered`) | None |
| 5 | `ASES-GIT-16` (`partial`): worktree pinned to exact local integration HEAD; whether `worktree_sync` needs to be turned off explicitly for a repo with a remote is "still open" | **Partly true** | `src/ases/profiles.py:901-905` (`_config_rows`, cites ASES-GIT-16, sets `worktree_sync: false` on every profile) and `:1530-1533` (`_check_profile`, flags it as a problem if left on), both built in Round 5, after the run this bullet describes; register `ASES-GIT-16` still `partial` (real guard case -- a repo whose remote tip differs from local HEAD -- still never exercised) | Rewrote: the "is it turned off explicitly" design question is now answered (yes, by code); the genuinely-still-open part (never tested against a real divergent remote) is kept, restated precisely |
| 6 | "Gaps the first real run exposed": reviewer has `write_file`/`patch` (ASES-ROL-05 partial); reviewer can't see gate records, only trusts the coder's claim; integrity snapshots (ASES-GIT-12) not wired; Docker sandbox (ASES-SEC-03) is Phase 5 | **Partly true** | Reviewer tool scope: register `ASES-ROL-05` (`partial`), unchanged, still true. Gate-record trust: `src/ases/review.py:62` (`gate_before_review`) and `:121` (`check_branch_for_merge`) now independently re-verify Gate 1, seen firing for real 2026-09-19 (register `ASES-REV-05`, `partial`). Integrity snapshots: `src/ases/guards.py:117` (`check_primary_checkout`, wired and acceptance-proven 2026-09-22) vs `:373` (`check_idle_worktrees`, still only WARNs, documented false-positive gap) (register `ASES-GIT-12`, `partial`) | Rewrote all three sub-claims: kept the reviewer-tools claim, corrected the "only trusts the coder's claim" claim (controller now independently re-checks), corrected "not wired" (primary-checkout half is wired; the bullet's own pip-install example is still uncaught by either half, only Docker sandbox would catch it) |
| 7 | Credentials as of 2026-09-19: `lead`/`coder-1` on xKiro, `reviewer` on OpenRouter, UnoRouter removed entirely | **Still true** | config/models.yaml: coder still pinned `xkiro/qwen/qwen3-coder-plus:free`; reviewer still pinned `openrouter/cohere/north-mini-code:free`; no `unorouter` row anywhere | None |
| 8 | Gate 1/3 run directly on the host, not inside Docker (Phase 5 requirement, not built) | **Partly true** | `src/ases/gates.py:58` (`run_gate` has an injectable `runner` param, citing ASES-QG-04/ASES-SEC-03); `src/ases/finalgates.py:649` and `:709` (Gate 4/5 already forward `runner`); but `src/ases/review.py:382` (Gate 1), `src/ases/mergeq.py:254` (Gate 3), `src/ases/controller.py:1011` (Gate 3 postmerge) and `src/ases/controller.py:1941` (`finalize` call) all pass no `runner`, so it defaults to `None` everywhere; register `ASES-SEC-03` (`in_progress`): "the controller's own gate runs do not use docker_run_argv yet" | Rewrote: the practical conclusion (host-only, not sandboxed) is unchanged and still true for every gate, but the "(Phase 5 requirement, not built)" framing was wrong -- the sandbox plumbing IS built, just not wired into any real call site; cited exactly which four call sites still need a runner |
| new | (none previously) | **New bullet added** | register `ASES-REV-01` (`partial`): "Plan critique (the critic role in Gate P) is not built." (quoted verbatim) | Added a new bullet: this is a genuine, explicitly-open register gap directly tied to two things this section already discusses (Gate P, and the Reviewer's first real run), so a reader of "Known gaps" would reasonably expect to find it and it was missing |

### "## Running things" section

- The bare `.venv\Scripts\python.exe -m pytest -q` line was wrong on this machine: `tests/integration/test_doctor_real_hermes.py` only self-skips via `pytestmark = pytest.mark.skipif(shutil.which("hermes") is None, ...)`, and a real `hermes.exe` is on this machine's PATH (per r8_rules.md), so a bare run would call the real Hermes for real, which the round's hard rule forbids. Fixed by adding `--ignore=tests/integration/test_doctor_real_hermes.py`, exactly as r8_rules.md's own required test command does. Proved with `--collect-only`: 5527 tests collected without the ignore flag, 5524 with it (the 3 real-hermes tests are exactly what gets excluded).
- `.venv\Scripts\python.exe spec\check_requirements.py --check` was left exactly as written (no `--docx` flag): HK-PATH (the parallel package) already appears to have landed its default-path fix in this shared working tree, and running the command directly confirms it: "OK: 103 requirement IDs in sync". Per the work order, I wrote/kept the command as it will work once HK-PATH lands, which it already does here.
- `swarm doctor` and `swarm models` left unchanged (still the correct current CLI subcommands, confirmed by reading `src/ases/cli.py`'s `add_parser` calls). `swarm models` was actually run (safe, makes no real-Hermes call) and its output is what grounded bullet 1's "stale" classification above. `swarm doctor` was NOT run, per the hard rule; `src/ases/doctor.py` confirms it really does call `hermes_mod.hermes_version()`, `run_doctor()` and `gateway_status()`, i.e. the real Hermes binary, so the ban is correct and the doc line needs no change beyond what's already there.
- Added one short dated paragraph under the code block explaining both of the above, for a reader who wasn't here.

### Verification run

Full suite (this machine's exact required command, through the output compressor): 5522 passed, 2 skipped, 0 failed, in 762.03s (12:42). Round-8 baseline was 5,512 passed, 2 skipped, 0 failed -- count went up (other parallel work already in this shared tree), never down, exit code 0.

### Style check

Scanned the entire file (not just the sections I own) for em dash (U+2014) and section-sign (U+00A7) characters before and after editing: zero occurrences either time. Nothing to report there.

### Scope discipline

Touched only the "## Known gaps (tracked, not hidden)" and "## Running things" sections of docs/architecture.md. Did not touch spec/check_requirements.py or tests/unit/test_check_requirements.py (HK-PATH's files, changed in parallel by another agent -- confirmed via `git status` that only that agent's edits touch those paths). Did not edit any of the dated historical round sections earlier in the file, per instructions, even where they're now superseded.

### HK-GAPS independent review

Repo: C:\Users\masoo\ases, branch master. No files edited (review only), no commits, no push, no bare `git stash`, no real Hermes/model-provider/Docker calls made. Read r8_rules.md and r8_wp_housekeeping.md (HK-GAPS spec) first, as instructed.

Verified `git -C C:\Users\masoo\ases diff docs/architecture.md` directly and checked every bullet in the new "Known gaps" section against src/ases/*.py (via Read/Grep, exact line numbers), spec/requirements.yaml's status/note fields (loaded via Python+yaml), config/models.yaml, and the installed Hermes config at %LOCALAPPDATA%\hermes\config.yaml.

(a) Scope: the diff has exactly two hunks, one at original line 1143 (the "Known gaps" section, which starts at line 1144) and one at original line 1188 (the "Running things" section, which starts at line 1225, the last section in the file). Nothing above line 1143 changed. Confirmed clean.

(b) Bullet-by-bullet fact-check against code and register (every ID cited was pulled fresh from spec/requirements.yaml, not taken on faith):
- glm-5.3-thinking:free bullet (struck through, ASES-MOD-02 in_progress): confirmed no `glm` model row exists in config/models.yaml (grep), confirmed `xkiro/qwen/qwen3.8-max:free` is the pinned lead with context_length 1050000 by actually running `swarm models` myself (output: `xkiro/qwen/qwen3.8-max:free role=lead context=1050000 smoke=pass pinned`, matching the doc's quoted line exactly), and confirmed ASES-MOD-02's register status is `in_progress` with a note that still describes the old glm gap. Accurate.
- UnoRouter secret naming bullet: confirmed the installed %LOCALAPPDATA%\hermes\config.yaml still has `key_env: HERMES_CUSTOM_UNOROUTER_API_KEY`; confirmed config/models.yaml has zero `provider: unorouter` and zero `provider: openai` rows (grep of every `provider:` line shows only xkiro and openrouter); confirmed lead's key_env is `XKIRO_API_KEY` under the xkiro provider block; cross-checked against blueprint.txt Appendix B, which does name `OPENAI_API_KEY` for the UnoRouter secret, and against the "Lead moved off GLM, then to OpenAI via xKiro" section (line 220) which corroborates the demote-then-remove history. Accurate.
- Two already-struck-through bullets (Hermes profiles, Gate P publication): unchanged by this diff; register status for ASES-ROL-10 and ASES-ARC-09 both `covered`, matching. Correct to leave untouched.
- ASES-GIT-16 (worktree_sync) bullet: confirmed `profiles._config_rows` (src/ases/profiles.py:901-905) and `profiles._check_profile` (src/ases/profiles.py:1530-1533) exist exactly as cited and cite ASES-GIT-16 by name; register status is `partial` with the exact "never exercised against a real divergent remote" gap the bullet describes. Accurate.
- "Gaps the first real run exposed" bullet: verified ASES-ROL-05 (partial, unchanged), verified `review.gate_before_review` at review.py:62 and `review.check_branch_for_merge` at review.py:121 exist as cited, verified `guards.check_primary_checkout` (guards.py:117, wired into controller.py:2014, runs every pass) and `guards.check_idle_worktrees` (guards.py:373, called from controller.py:1912, confirmed WARN-only via controller.py:1913-1914) exactly as described. One overstatement found here -- see blocking finding.
- Credentials bullet: unchanged; confirmed coder still pinned to xkiro/qwen/qwen3-coder-plus:free and reviewer to openrouter/cohere/north-mini-code:free, no unorouter row. Accurate, correctly left untouched.
- Gate 1/3 Docker bullet (fully rewritten): confirmed `gates.run_gate`'s `runner` parameter at gates.py:58 with the exact `(ASES-QG-04, ASES-SEC-03)` citation in its own docstring; confirmed `run_gate4`/`run_gate5` in finalgates.py (lines 649, 709) forward `runner`; confirmed all four cited call sites (review.py:382, mergeq.py:254, controller.py:1011, controller.py:1941) call run_gate/finalize without a runner argument, defaulting to None; confirmed ASES-SEC-03 register status `in_progress` with the exact quoted note "the controller's own gate runs do not use docker_run_argv yet"; confirmed sandbox.py:18's docstring quote. Accurate (two trivial non-blocking nits noted below).
- New ASES-REV-01 bullet: confirmed register status `partial` and the quoted sentence "Plan critique (the critic role in Gate P) is not built." is a verbatim, correct quote of the register note. Accurate, and it is a genuine gap the section would be expected to carry, so adding it was correct per the work order's rule 3.

(c) The one genuinely stale bullet (glm) was struck through with `~~...~~` plus a dated 2026-09-27 note, matching the file's established convention, not silently deleted. Partly-true bullets were rewritten in place without strikethrough, which is correct per the work order (strikethrough is only required for stale bullets).

(d) Running things: confirmed a real hermes.exe is on PATH (`where hermes` resolved it) and tests/integration/test_doctor_real_hermes.py only self-skips via `shutil.which("hermes") is None`, so the added `--ignore=...` flag is required, exactly as the doc now says. I ran `pytest --collect-only` with and without the ignore flag myself: 5527 collected without it, 5524 with it, a difference of exactly 3 (the real-Hermes tests), matching the build report's numbers. I ran `spec/check_requirements.py --check` with no flags myself: it printed "OK: 103 requirement IDs in sync" (HK-PATH's fix has landed in this shared tree, confirmed via grep of DEFAULT_DOCX in spec/check_requirements.py). I read (did not run) src/ases/doctor.py and confirmed it calls `hermes_mod.hermes_version()`, `run_doctor()` and `gateway_status()` -- real Hermes calls -- so leaving `swarm doctor` in the doc and not running it is correct.

(e) Scanned both the diff's added lines and the entire current docs/architecture.md file programmatically for U+2014 (em dash) and U+00A7 (section sign): zero occurrences in either.

One blocking finding: the rewritten "Gaps the first real run exposed" bullet claims `gate_before_review` "independently re-runs Gate 1 itself before the reviewer ever sees the card" as an unqualified fact. This directly contradicts the docstring of `check_branch_for_merge` (cited two lines later in the same bullet), which says Hermes's gateway dispatcher "can claim a review card and start the reviewer before gate_before_review has seen it" -- and the register note for ASES-REV-05 (also cited in the same bullet) confirms this race is a real, occurring behavior ("when Hermes's dispatcher wins the race the re-check happens at merge time instead of when the card enters review"), not merely a hypothetical. The bullet acknowledges one caveat (the send-back path has never fired for real) but omits this different, code-documented ordering caveat while asserting the ordering as settled fact.

Two non-blocking style/precision nits: (1) "finalgates.gate4"/"finalgates.gate5" should be `run_gate4`/`run_gate5` (line numbers and behavior are correct, only the name is off); (2) a docstring quote from sandbox.py has a capitalization mismatch that doesn't affect substance.

Could not get a nemotron second opinion: all three nemotron MCP tools returned 403 Authorization failed for this session (an auth problem with that server, unrelated to this task). All verification above was done by direct inspection of source, the register, and by running the document's own listed safe commands.

### HK-GAPS fix

## HK-GAPS review-finding fix (docs/architecture.md)

Scope: one blocking finding on sub-package HK-GAPS, file docs/architecture.md, "Known gaps" section only (per r8_wp_housekeeping.md, files owned are docs/architecture.md's Known gaps and Running things sections). No other file was touched.

### Finding re-checked

Claim under review: the "Gaps the first real run exposed" bullet (around line 1191) said, as an unqualified fact, that `review.gate_before_review` (src/ases/review.py:62) "independently re-runs Gate 1 itself before the reviewer ever sees the card."

I re-checked this against the code and the register myself rather than taking the reviewer's word:

- src/ases/review.py:121-129, the docstring of `check_branch_for_merge` (the very function cited two lines later in the same bullet), says verbatim: "Hermes's own gateway dispatcher can claim a review card and start the reviewer before gate_before_review has seen it, so a card can reach the merge queue with the scope check and the Gate 1 record never having happened."
- spec/requirements.yaml, ASES-REV-05 (status: partial), says verbatim: "...and when Hermes's dispatcher wins the race the re-check happens at merge time instead of when the card enters review."

Both the code's own docstring and the register row the bullet itself cites describe this ordering race as a real, occurring behavior, not a hypothetical. The bullet's unqualified "before the reviewer ever sees the card" therefore overstated what the code guarantees. The reviewer's finding is CONFIRMED.

### Fix applied

Edited the bullet (docs/architecture.md, "Known gaps" section) to:
- Say `gate_before_review` re-runs Gate 1 "when a card enters review" (not an absolute "before the reviewer ever sees the card").
- Add the qualifier, sourced from the function's own docstring, that Hermes's gateway dispatcher can sometimes start the reviewer first.
- State explicitly that when that race is lost, `check_branch_for_merge` is what actually catches it at merge time -- preserving the bullet's true, substantive point (two independent checks together mean the controller no longer merely trusts the coder's claim) without asserting a strict ordering guarantee the code does not always hold.
- Extend the `ASES-REV-05` parenthetical to also note the register's own caveat about the dispatcher-wins-the-race case resolving at merge time, not at review entry.

No other clause in the bullet, and no other section of the file, was touched. I did not touch spec/check_requirements.py, spec/requirements.yaml, or the other work-order/test files that appeared as untracked/modified in `git status` -- those are other agents' concurrent, disjoint HK-PATH and round-9 work, not part of this HK-GAPS finding.

### Verification

- Read src/ases/review.py:1-150 (module docstring, `gate_before_review`, `check_branch`, `check_branch_for_merge`) directly to confirm the finding rather than trusting the reviewer's quoted evidence blindly.
- Read the ASES-REV-05 row in spec/requirements.yaml directly (lines 791-803) to confirm the register note's exact wording.
- Re-read the edited paragraph in context (docs/architecture.md lines 1186-1203) to confirm it reads coherently and does not contradict the rest of the bullet.
- Scanned the whole file for em dash (U+2014) and section sign (U+00A7) characters: zero hits, so the housekeeping character rule still holds.
- Confirmed via `git status --porcelain` that only docs/architecture.md was modified by me; the other modified/untracked files (spec/check_requirements.py, spec/requirements.yaml, tests/unit/test_check_requirements.py, various docs/work-orders/*.md) belong to the parallel HK-PATH package and other round work, not to this task.
- No test suite covers this file's prose (grepped tests/ for "architecture.md": the only hits are in test_critic.py and test_profiles.py, which reference an unrelated fixture path `docs/ases/architecture.md`, not the repo's own top-level docs/architecture.md), so no test run applies to this documentation-only fix. Did not run the full pytest suite since this change touches no code path.

### Anything found but not fixed

Nothing else in scope was found wrong. The rest of the "Gaps the first real run exposed" bullet, and the rest of the Known gaps / Running things sections, were left as-is since no other finding was given for this sub-package in this pass.

### HK-GAPS re-review

Verdict: PASS. The single blocking finding is resolved and the fix introduces no new wrong claim.

What I checked directly (not taking the fixer's word for it):

1. Code: read src/ases/review.py lines 1-150 myself.
   - `gate_before_review` is at line 62 (confirmed).
   - `check_branch_for_merge` is at line 121 (confirmed), and its docstring says verbatim: "Hermes's own gateway dispatcher can claim a review card and start the reviewer before gate_before_review has seen it, so a card can reach the merge queue with the scope check and the Gate 1 record never having happened," and later: "otherwise there is no green record at all (for example the review-lane re-check was skipped by that race), so Gate 1 runs now on the head."

2. Register: read spec/requirements.yaml lines 785-800 myself. ASES-REV-05 is `status: partial`, and its note ends verbatim: "...when Hermes's dispatcher wins the race the re-check happens at merge time instead of when the card enters review."

3. Current doc text (docs/architecture.md lines 1187-1198, read directly): the bullet now says `gate_before_review` "independently re-runs Gate 1 when a card enters review, though its own docstring says Hermes's gateway dispatcher can sometimes claim the card and start the reviewer before gate_before_review has run; when that race is lost, `review.check_branch_for_merge` ... is what actually catches it, re-checking scope and Gate 1 again, independently, at merge time," and the `ASES-REV-05` parenthetical now adds "the register note also records that when the dispatcher wins the race, the re-check happens at merge time rather than at review entry."

This tracks the code and the register exactly: the absolute "before the reviewer ever sees the card" claim is gone, replaced by "when a card enters review" plus an explicit, sourced caveat about the race and which check catches the lost-race case. The one wording nuance -- the docstring says "before gate_before_review has seen it" and the fix paraphrases it as "before gate_before_review has run" -- is a faithful, non-material paraphrase (gate_before_review "sees" a card precisely by running its checks on it), not a new inaccuracy. Line citations (review.py:62, review.py:121) are both correct.

Scope and hygiene checks: confirmed via `git status --porcelain` that only docs/architecture.md is modified in this agent's area of concern (the other modified/untracked files -- spec/check_requirements.py, spec/requirements.yaml, tests/unit/test_check_requirements.py, docs/work-orders/*.md -- belong to concurrent HK-PATH/round-9 work, matching the fixer's account). Independently re-counted em dash (U+2014) and section-sign (U+00A7) characters in the whole file via a small Python check: zero of each, consistent with the project's character-ban rule.

One non-blocking observation for a later pass (not part of this finding, not touched by this fix): the very next, untouched sentence in the same bullet -- "Both paths were seen firing for real on the 2026-09-19 run" -- sits a bit uneasily next to the register note's "Since then the merge queue re-checks independently" (implying the merge-time-recheck-from-scratch capability was built after 2026-09-19) and the register's own silence on whether the dispatcher-actually-won-the-race case occurred that day. This may be a pre-existing, separate overclaim worth a future look, but it predates and is unrelated to the specific finding I was asked to re-review, so it is not a blocking item here.

nemotron: attempted an independent second-opinion pass via mcp__nemotron__run_nemotron_super (pasting the docstring, register note, and fixed bullet text verbatim and asking for an independent RESOLVED/NOT-RESOLVED verdict), but the call failed with a 403 PermissionDeniedError ("Authorization failed") -- the nemotron endpoint is not authorized in this environment/session. Proceeded with my own direct source verification instead (code + register + doc, read firsthand, not from the fixer's quotes).

Files involved: C:\Users\masoo\ases\docs\architecture.md (re-reviewed, not edited), C:\Users\masoo\ases\src\ases\review.py (read for verification), C:\Users\masoo\ases\spec\requirements.yaml (read for verification). No files were edited by me, per instructions.

### GATEENV builder

## Package GATEENV (round 8): gate commands no longer see the operator's credentials

### What changed

**src/ases/gates.py**
- `_run_commands` (the default host `runner` for `run_gate`) now starts every command with `procenv.scrubbed_environ()` instead of the controller's own environment. A gate command can run model-authored code (a test a coder committed), so it must not be able to read a provider key the operator's shell happens to hold.
- `run_gate`'s own `git worktree add`/`worktree remove` subprocess calls also now run with `procenv.scrubbed_environ()`, closing the side door named in the work order: a `post-checkout` hook in the shared `.git/hooks` (writable by a worker on the local backend) that runs during the gate checkout now sees no credential either.
- Module docstring and `_run_commands`'s docstring updated to quote ASES-CFG-04/ASES-CFG-05 and state coverage precisely: the host runner and the gate checkout are covered; a gateway-dispatched worker's own shell is not, because ASES never spawns one; Docker sandbox runners are unaffected (they never inherited the host environment); no pass-through allowlist exists this round (documented as a follow-up, per the architect's decision).
- Added `from . import procenv as procenv_mod` import.

**src/ases/procenv.py**
- Added `_EXEMPT_EXACT_NAMES = frozenset({"GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE"})`, matched case-insensitively (`name.upper() in _EXEMPT_EXACT_NAMES`), and `scrubbed_environ()` now keeps a variable if its exact name is in that set even when it matches `_CREDENTIAL_ENV` (all three match only because "AUTHOR" contains "auth"). `SSH_AUTH_SOCK` and `XAUTHORITY` deliberately get no exemption (capability-bearing: an ssh-agent socket lets model-written code authenticate as the operator); `SESSIONNAME` is left as is (still dropped via the pre-existing "session" match).
- Module and function docstrings updated to explain the exemption and name every caller it now reaches (gates.py's host runner and its git worktree subprocesses, plus the five pre-existing hermes launch sites and evalkit).

**tests/unit/test_procenv.py** -- 5 new tests: the exemption fires for GIT_AUTHOR_NAME/EMAIL/DATE, is case-insensitive, does NOT extend to SSH_AUTH_SOCK/XAUTHORITY, leaves SESSIONNAME dropped, and is an exact-name match (not a substring match, e.g. `GIT_AUTHOR_NAME_EXTRA` still gets dropped).

**tests/unit/test_gates.py** -- 5 new tests, every one using a presence-marker probe (KEYSEEN/NOKEY), never asserting on the secret value itself (since `run_gate` redacts secret-shaped values in its output, so a value-based assertion could pass for the wrong reason):
- a credential-shaped var (`OPENROUTER_API_KEY`) and a generic one (`MY_SERVICE_TOKEN`) are invisible to a gate command;
- `PATH` and a made-up `ASES_GATE_PROBE` are still visible (the fix didn't just empty the environment);
- a `shell=True` command still runs on Windows (COMSPEC/SYSTEMROOT survive);
- `GIT_AUTHOR_NAME` survives into a gate command, `SSH_AUTH_SOCK` does not;
- a planted `post-checkout` hook in the test repo's `.git/hooks` runs during `run_gate` (proven non-vacuous by its own marker file) and cannot see a planted `OPENROUTER_API_KEY`; the test fails loudly (rather than skipping silently) if a POSIX shell is available but the hook somehow didn't run, and only skips with an honest reason if neither `sh` nor `bash` is on PATH.

**tests/acceptance/test_22_10_gate_env.py (new file)** -- one acceptance-level test, following test_22_10_secrets.py's and conftest.py's style without editing either: a `world_factory` world whose only gate command is the presence probe, with `OPENROUTER_API_KEY` planted in the TEST PROCESS's own environment, driven through a real controller pass (create_cards_from_plan, run_pass) on FakeHermes to `all_merge_cards_done()`. Asserts every `gate1` and `gate3*` row in `gate_runs` says `NOKEY` (the scenario reaches `gate1`, `gate3`, and `gate3-postmerge`, confirmed by inspection during development), and that the planted key literal never appears in any `gate_runs.detail`.

### Before/after proof (required)
Copied the new `gates.py` aside, restored the pre-round-8 version from git (`git show HEAD:src/ases/gates.py > src/ases/gates.py` -- never a bare `git stash`, since other agents have uncommitted edits in the same tree), ran the new tests, then restored the fix. Full output is in `before_after`. Summary: against the OLD code, 3 of the 5 new unit tests fail (`KEYSEEN`/`SOCKSEEN`/the hook's own marker file containing `KEYSEEN`) and the new acceptance test fails (`KEYSEEN`); against the restored fix, all pass. `git diff --stat` confirmed the tree was byte-identical to what I'd left it before and after the swap.

### Sweep (WP requirement)
Every call site of `run_gate` -- `review.py` Gate 1 (`_run_gate1`), `mergeq.py` Gate 3, `controller.py`'s post-merge `gate3-postmerge` check, and `finalgates.py`'s Gate 4/Gate 5 -- goes through the same function, so all four now inherit the fix through the one choke point; I read each call site to confirm none passes its own environment. Ran their full unit-test files (test_review.py, test_mergeq.py, test_controller.py, test_finalgates.py: 787 passed) to confirm no regression. Grepped all of `src/ases` for `shell=True`: the only other hit is `evalkit/texttasks.py:390`, a deliberately-vulnerable Flask fixture route (`ping = subprocess.run("ping -c 1 " + host, shell=True, ...)`) used as sample content for `finalgates.py`'s own injection-heuristic scanner tests, not a gate or check command ASES runs -- reported, not fixed, since it isn't the same shape as this gap.

### Confirmed unaffected (design decision items)
- `sandbox.py`'s `docker_run_argv` never inherits the host environment (grepped: no `os.environ`/`scrubbed_environ` reference in it) and I did not touch `sandbox.default_runner` (its key-leak probes still use the real environment, per the round 7 CFG-05 note).
- None of the five real hermes launch sites (`hermes.py::_run`, `evals.py`, `evalkit/codeeval.py`, `cli.py`, `critic.py`, plus `profiles.py`'s use of `hermes_mod.scrubbed_environ()` for its sandbox default runner) or `evalkit` depend on `GIT_AUTHOR_*` being absent: grepped for `GIT_AUTHOR`/`SSH_AUTH_SOCK`/`XAUTHORITY` across `src/ases` (no other references), and ran their full unit-test files (test_hermes_env.py, test_profiles.py, test_critic.py, test_cli_commands.py, test_evals.py: 915 passed).
- Out of scope, per the work order: the controller's other git calls (merge queue, review, reconcile, guards, hardening, leases, integrity) still run with the full environment; a separate sweep covers that in parallel, so I only confirmed (via grep) none of them uses `shell=True`, and did not otherwise touch them.

### Characters and files
Scanned every file I wrote or edited for em dash and section sign characters: none found. Files touched: `src/ases/gates.py`, `src/ases/procenv.py`, `tests/unit/test_gates.py`, `tests/unit/test_procenv.py`, and the new `tests/acceptance/test_22_10_gate_env.py` -- exactly the files the work order names as mine. I did not touch `spec/requirements.yaml`, `docs/architecture.md`, or anything under `docs/work-orders/`.

### Full suite (once, at the end, per r8_rules.md)
`python C:/Users/masoo/.claude/scripts/quiet.py -l pytest -- .venv/Scripts/python.exe -m pytest -q --tb=line --ignore=tests/integration/test_doctor_real_hermes.py`
Result: **5533 passed, 2 skipped in 969.99s (0:16:09)**. Round-8 baseline was 5,512 passed, 2 skipped, 0 failed -- it did not go down, and 0 failed. Never called a real Hermes, a real model provider, or Docker at any point (everything ran on `FakeHermes` or plain unit fakes); never committed or pushed; never ran a bare `git stash`.

See `open_issues` for the handful of judgment calls and out-of-scope findings worth the architect's attention.

### GATEENV independent review 1 (verdict: pass)

Verdict: PASS. Reviewed package GATEENV (round 8) without editing any file, per C:\Users\masoo\ases\docs\work-orders\r8_wp_gateenv.md and r8_rules.md.

What I checked and found:

1. Design decision items 1-5, verified against the actual diff (git diff -- src/ases/gates.py src/ases/procenv.py tests/unit/test_gates.py tests/unit/test_procenv.py) and the old code (via `git show HEAD:...`, never touching the working tree):
   - Item 1: `_run_commands` now does `env = procenv_mod.scrubbed_environ()` and passes it to every `subprocess.run(cmd, shell=True, ...)`. Old code had no `env=` kwarg at all (confirmed by reading `git show HEAD:src/ases/gates.py`), so it inherited the full environment - this is a real behavior change, not cosmetic.
   - Item 2: both the `git worktree add` and `git worktree remove` subprocess calls in `run_gate` now also pass `env=procenv_mod.scrubbed_environ()`. Confirmed old code passed no env there either.
   - Item 3: no allowlist mechanism was added anywhere; docstrings in both files state this and describe the follow-up. Confirmed by reading the full diff.
   - Item 4: `procenv._EXEMPT_EXACT_NAMES = frozenset({"GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE"})`, checked via `name.upper() in _EXEMPT_EXACT_NAMES` (a set-membership check, so it is exact-name only, never a substring/prefix match) `or not _CREDENTIAL_ENV.search(name)`. This is case-insensitive by construction and does not touch `SSH_AUTH_SOCK` or `XAUTHORITY` (neither is in the frozenset, so both still fall through to the regex and get dropped, as tests confirm).
   - Item 5: read `sandbox.py` directly - `docker_run_argv`'s docstring and body confirm "Environment: ONLY the variables in env... The parent's environment is never forwarded", and `default_runner` (untouched by this diff) still defaults to inheriting the real environment when no `env` is passed, exactly as the round-7 note says its key-leak probes require. `sandbox.py` does not appear anywhere in the reviewed diff.

2. Read `tests/acceptance/test_22_10_gate_env.py` in full and the new tests in `test_gates.py`/`test_procenv.py`. Every credential-presence test uses a SEEN/NOxxx presence marker, never asserts on the secret value (correctly avoiding a false pass from `run_gate`'s own output redaction). The `post-checkout` hook test asserts the hook actually ran (via its own marker file) before checking the marker's content, so it is not vacuous, and fails loudly rather than skipping silently unless neither `sh` nor `bash` is on PATH - I read `tests/unit/test_gates.py`'s `repo` fixture and confirmed it is a real `git init` repo with a genuine `.git/hooks` directory, so planting a hook there is legitimate.

3. Checked each new test would fail against the pre-round-8 code for the right reason: since the old `_run_commands` and the old `git worktree add/remove` calls had no `env=` argument at all, they inherited the full test-process environment, meaning `OPENROUTER_API_KEY`/`MY_SERVICE_TOKEN`/`SSH_AUTH_SOCK`/the planted hook's `OPENROUTER_API_KEY` would all have been visible, producing `KEYSEEN`/`SOCKSEEN`/a hook marker of `KEYSEEN` - exactly the 3-of-5 unit-test and 1 acceptance-test failures the builder's before/after proof reports. (I did not personally reconstruct the old file and re-run it, since I was told not to edit any file; I verified this analytically from the `git show HEAD` content, which is sufficient to confirm the claim.) The other 2 new unit tests (non-credential vars still visible; a shell=True command still runs on Windows) are sanity/coverage checks that would already pass on the old code too - this is expected and matches the builder's own report, not a defect.

4. Swept `src/ases` myself for other subprocess call sites of the same shape and for other callers of `procenv.scrubbed_environ()`:
   - `grep -rn "shell=True" src/ases` -> only `gates.py:73` (now fixed) and `evalkit/texttasks.py:390`, which I read directly and confirmed is a deliberately-vulnerable Flask fixture route (`ping = subprocess.run("ping -c 1 " + host, shell=True, ...)`) used as sample content for `finalgates.py`'s own injection-heuristic scanner tests, not a gate/check command ASES runs.
   - `grep -rn "scrubbed_environ" src` -> `hermes.py`, `critic.py`, `cli.py`, `evals.py`, `evalkit/codeeval.py`, `profiles.py` (via `hermes_mod.scrubbed_environ()`, which is a direct re-export of `procenv.scrubbed_environ`), and the new `gates.py` sites. This matches the report's "five hermes launch sites plus evalkit plus profiles" enumeration.
   - `grep -rn "GIT_AUTHOR\|SSH_AUTH_SOCK\|XAUTHORITY" src/ases tests/unit/test_hermes_env.py tests/unit/test_profiles.py tests/unit/test_critic.py tests/unit/test_cli_commands.py tests/unit/test_evals.py` -> no hits outside `procenv.py` itself, confirming none of those callers depends on `GIT_AUTHOR_*` being absent.
   - `grep -n "run_gate("` across `src/ases` -> exactly the four call sites named in the work order (`review.py::_run_gate1`, `mergeq.py` Gate 3, `controller.py`'s post-merge check, `finalgates.py`'s Gate 4 and Gate 5), and I read each call site directly: none passes its own `env` or its own runner that would bypass the scrub; all either use the default `_run_commands` or forward a `runner` parameter meant for the (already-unaffected) sandbox path.

5. Ran the targeted tests via the quiet wrapper (absolute python path was required; the bare relative `.venv/Scripts/python.exe` failed under the wrapper on this machine):
   `python C:/Users/masoo/.claude/scripts/quiet.py -l pytest_gateenv -- C:/Users/masoo/ases/.venv/Scripts/python.exe -m pytest -q --tb=short tests/unit/test_gates.py tests/unit/test_procenv.py tests/acceptance/test_22_10_gate_env.py tests/acceptance/test_22_10_secrets.py`
   Result: **80 passed** in 26.6s.
   I also ran the broader regression set the builder's report claims to have covered (test_review.py, test_mergeq.py, test_controller.py, test_finalgates.py, test_hermes_env.py, test_profiles.py, test_critic.py, test_cli_commands.py, test_evals.py) to independently confirm no regression from the exemption or the scrubbing: **1702 passed** in 406s, 0 failed.

6. Got a second opinion from nemotron (see the `nemotron` field for the 403-then-fallback path). It found zero defects after an extensive (if repetitive) self-directed trace through the regex, the exemption's exact/case-insensitive matching, the shell=True/env interaction on Windows, and the acceptance test's assertions, and confirmed the tests are not vacuous. I treated this as one input and independently verified everything myself rather than taking its word for it.

I also noticed (git status) that docs/architecture.md, spec/check_requirements.py and spec/requirements.yaml are modified in the working tree even though the work order says GATEENV must not touch them. I diffed those three files and confirmed the changes are about the blueprint docx path (matching r8_rules.md's mention of a separate package, HK-PATH) and contain no GATEENV/ASES-CFG-04/05 content - this is another in-flight package's uncommitted work in the same tree, not a violation by this builder. I did not edit anything.

No blocking defects found. Files reviewed (read-only): src/ases/gates.py, src/ases/procenv.py, src/ases/sandbox.py, tests/unit/test_gates.py, tests/unit/test_procenv.py, tests/acceptance/test_22_10_gate_env.py, plus review.py/mergeq.py/controller.py/finalgates.py/evalkit/texttasks.py/evalkit/codetasks.py/fakes/worker.py/fakes/board.py for the sweep.

### GATEENV live verification (Haiku)

FIX-FIRST: Windows environment misconfiguration blocks test verification. Step 2 shows 5 test failures (expected 0) due to subprocess.run() inability to locate python command in PATH, which is an environmental issue not a code bug. However, this prevents proper verification of the credential scrubbing feature tests. Code quality checks show: Step 5 (requirements) PASS; Step 6 (ASCII) PASS; Step 3 (before/after) PASS; subset tests including new procenv tests (6/6) and project scoping tests PASS. Step 4 (full suite) still running after 600s timeout, cannot verify. Root cause: Windows PATH configuration prevents subprocess discovery of python executable. Recommend: fix Windows PATH to include python before retest, or configure subprocess calls to use full python.exe path. Code implementation appears correct based on review and subset test passes.

### Sibling sweep: the controller's other git calls (read-only)

Outcome: the sibling exposure GATEENV's work order flags as "out of scope, report only" is real and already reachable by unmodified production code, proved empirically twice against the real ASES helper functions (not a hypothetical).

Proof 1: mergeq._git(['worktree','add','--detach',...]) against a throwaway repo with a planted post-checkout hook leaked a planted OPENROUTER_API_KEY-shaped env var into the hook.
Proof 2: guards._git(['status','--porcelain',...]) against a repo with core.fsmonitor set to a script leaked the same env var, with git's own stdout/stderr completely silent about it.
Proof 3: plain 'git config' run from a linked worktree wrote into the shared .git/config (extensions.worktreeConfig off by default), confirming the worker-plant path the other two proofs depend on.

High severity: mergeq.py (worktree add/merge/commit/revert), mergeq.py:344's own secret-scan diff (missing --no-ext-diff/--no-textconv that the sibling call in tamper.py:1018 already has), and every status-calling _git in guards.py/hardening.py/reconcile.py/integrity.py.
Medium: controller.py's bootstrap/publish_plan/_branch_diff, hardening.py's branch -d/-D.
Info: filter/merge driver names can't be pre-empted by one flag (env-scrub is the real backstop there); Hermes's own worktree-creation subprocess and a worker's direct edits to its own account (~/.gitconfig, PATH binaries) are outside anything src/ases can reach.

Fix proposed: one hardened git helper (scrubbed env, -c core.hooksPath=<empty dir>, -c core.fsmonitor=false, --no-ext-diff/--no-textconv on diff-capable subcommands, GIT_CONFIG_NOSYSTEM=1) used by every site above. Note: this dispatch is read-only per its own instructions and my tool grant (no edit tools); the "build and fix" part of the relayed request is for the build-track agents, not this audit.

Checked and clean / lower risk: review.py (only rev-parse, merge-base, cat-file --batch, and 'diff --name-only' between two commits, no working-tree interaction, no content diff); leases.py (only 'rev-parse --git-path', pure plumbing); doctor.py and cli.py's git calls (config --get, --version, no repo content); killswitch.py (no git at all, only taskkill/pgrep/ps/docker); finalgates.py and tamper.py's cat-file/ls-tree calls (plumbing, no filter/hook path); tamper.py's own content diff at line 1018 (already passes --no-ext-diff --no-textconv, a good precedent to copy).

### Architect notes

- HK-PATH's checklist came back `all_pass: false` with every individual check passing: the verifier's only objection was that
  the new test file is untracked, which is by design (builders never commit; the architect does). Not a defect.
- GATEENV's Haiku pass reported 5 targeted failures ("subprocess cannot find python") and a full-suite timeout. Re-run by the
  architect in Git Bash: the 5 are pre-existing tests that run a bare `python ok.py` as a gate command and so depend on `python`
  being on the caller's PATH (the verifier's shell did not have it); none is a round 8 test and all pass here. The timeout was the
  tool's 600 s limit, not the suite.
- One real, pre-existing test fragility found by the architect's re-run: `test_gate_worktree_cleaned_up` asserted that the text
  "wt" does not appear in `git worktree list`, so it failed whenever pytest's temp directory path contained "wt" (this round's
  `--basetemp` sits under a folder named `ases-wt`). Rewritten to compare the actual worktree entries (`--porcelain`, exactly the
  primary checkout left). Every round 9 worktree will see the old failure as a pre-existing baseline failure until this merges.
- The nemotron MCP tools still return 403. The GATEENV reviewer found a working fallback: `tools/nemo.py` run with the nemotron
  MCP server's own venv interpreter (`C:\Users\masoo\.claude\mcp-servers\nemotron\venv\Scripts\python.exe nemo.py super
  < task.txt`), since neither the system Python nor the ASES venv has the `mcp` package.
- The sweep's findings became round 9 package GITHARDEN (`r9_wp_githarden.md`), with one deliberate change to its proposal: no
  `GIT_CONFIG_NOSYSTEM`, because the system config is not worker-writable and on Windows carries `core.autocrlf`.
- Architect's full-suite run on the combined round 8 tree: 5533 passed, 2 skipped, 0 failed (657 s, clean tree apart from the in-progress round 9 register edits, which no test depends on).

