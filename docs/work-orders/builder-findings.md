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
