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
