# ASES operations guide

How to run a project with the ASES swarm controller, day to day. This guide describes the code as it is: every command,
file name, key and exit code below was read from the source (`src/ases/cli.py` and the modules it calls). When a
failure needs a diagnosis, go to `docs/runbook.md` (symptom, cause, action). The design and the reasons are in
`docs/architecture.md`.

Contents: 1 the parts, 2 a normal project, 3 command reference, 4 what `swarm run` does, 5 exit codes, 6 files and
directories, 7 configuration, 8 the request ledger and parking, 9 the database, 10 retention and cleanup.

## 1. The parts

| Part | What it is | Where it lives |
| --- | --- | --- |
| Hermes Agent | The board (Kanban), the profiles, the dispatcher and the workers. ASES only talks to it through `src/ases/hermes.py`. The tested version is pinned in `config/swarm.yaml` (`hermes.tested_version`, now 0.21.3). | `hermes.native_home` (on this machine `C:/Users/masoo/AppData/Local/hermes`) |
| The controller | The `swarm` command: policy, gates, the merge queue, recovery, the kill switch, reports. It does not know how to run an agent. | this repository, `src/ases/` |
| The target repository | The code being built. Its primary checkout stays on the integration branch (`project.integration_branch`, `integration`) and is never edited by an agent. Only the merge queue writes to that branch. | `project.workspace_root` |
| Worker worktrees | One git worktree per work card, made by Hermes, on a branch `swarm/<task key>-<role>`. | `<repo>/.worktrees/<card id>` |
| The ASES database | SQLite, WAL mode: the request ledger, plan tasks, gate runs, merge records, events, lineage counters, intents, leases. | `<ases_home>/ases.db` |

Words used everywhere:

- Plan: `docs/ases/plan.json` in the target repository. One plan task becomes a work card and a merge card.
- Gate 0: the plan is validated (roles, keys, touches, acceptance criteria, gate profiles, `max_cards`).
- Gate P: the independent reviewer critiques the plan (`swarm critique`), then you approve it (`swarm approve`). No
  implementation card exists before that.
- Gate 1: the controller re-runs a card's own gate commands on the exact commit that entered review, after a scope check
  and a tamper check (deleted or skipped tests, unconditional passes, gate configuration edits, secrets).
- Gate 3: the same commands on the merge candidate, a squash of the work branch on the integration tip, in a throwaway
  worktree. Green means the integration branch is fast-forwarded to exactly that commit.
- Gates 4 and 5: security scan and smoke run on the integration HEAD once every merge card is done. The project is
  finished when both are green and the release report is written.
- Question: a card that is blocked (or in Hermes's triage lane) with a reason nobody has answered. There is no separate
  store; `swarm questions` reads the board.
- Parking: a card that cannot be afforded today is scheduled (parked) until the provider quota resets.

Everything ASES prints is plain ASCII, because the Windows console is cp1252. Anything else (an accented letter in a card
title) is shown as a backslash escape.

## 2. A normal project, in order

Run every command from `C:\Users\masoo\ases` (or with `swarm` on your path). `--repo` is the target repository.

1. `swarm doctor` - check the environment before anything else (section 3). Fix every `[FAIL]` row.
2. `swarm init` - a dry run that lists what the Hermes profiles need. Read the list. When it is what you want,
   `swarm init --apply --yes` writes it. **Needs your approval:** this changes your real Hermes profiles under
   `hermes.native_home` (a backup is taken of each file it changes). `--global` also changes the kanban limits of your whole
   Hermes configuration, which every Hermes project shares. `--sandbox` (or `sandbox.enabled: true`) needs Docker.
3. `swarm plan --repo R --request "what to build"` - the Lead profile writes `docs/ases/plan.json` into the repository.
   The file is not committed yet.
4. `swarm critique --repo R --request "what to build"` - Gate 0, then the reviewer critiques the plan. Exit 0 only for a
   PASS. `--auto-replan` lets the command send CHANGES_REQUIRED back to the Lead itself, at most
   `budgets.replans_per_project` times in all. The verdict is bound to the hash of the plan file: editing the plan
   afterwards voids it.
5. `swarm approve --repo R --project-id P` - Gate 0, the request budget and calendar estimate, the critic PASS for this exact
   file, then you are asked `Proceed? [y/N]`. On yes it commits `docs/ases/` to the integration branch
   (`ASES: publish approved plan (Gate P)`), pins the gate profiles, and creates one work and one merge card per task.
   `--yes` skips only the question, never the critic. `--skip-critic` approves without a critic PASS and records a
   `critic_skipped` event. `--deadline-minutes N` sets the project wall clock. Re-running it is safe (card creation is
   idempotent by plan key); it is also how you re-pin gate profiles after changing them on purpose.
   `P` is the Hermes project id (`hermes project list`).
6. `swarm run --repo R` - the controller loop (section 4). Leave it running. It ends with one of the exit codes of
   section 5; re-run it to continue.
7. While it runs, from another terminal: `swarm status --repo R` (one screen), `swarm report --repo R` (full text),
   `swarm questions --repo R` and `swarm answer <card> "<text>"` when a card needs a person.
8. `swarm stop` if anything looks wrong (section 3), `swarm resume` when it is safe again.
9. When the run exits 0: read the release report (section 6), then `swarm clean --repo R` (a dry run) and, if the list is
   right, `swarm clean --repo R --apply`. Now and then `swarm retention` (a dry run) and `swarm retention --apply`.

## 3. Command reference

`swarm <command> --help` shows every option. Exit codes other than those of `swarm run` are 0 for success and 1 for a refusal
or failure, unless a row says otherwise. A configuration error (`config/swarm.yaml` or `config/models.yaml`) is exit code 2
from any command; a Hermes command that fails is a one-line message and exit code 1; Ctrl-C is 130.

| Command | What it does | Reads / writes |
| --- | --- | --- |
| `swarm doctor` | Runs the environment checks and prints one row each: `[PASS]`, `[WARN]`, `[FAIL]` or `[PEND]`. Exit 1 only when a row is a FAIL. Rows cover: the environment decision, not under OneDrive, git long paths and `.gitattributes` (native Windows), Python and git versions, Hermes version against the pin (a mismatch is a WARN), `hermes doctor`, the gateway dispatcher, the Hermes profile state (warnings only), the sandbox (warnings until it is enabled), the model registry, role profiles, reviewer diversity, key pooling (ASES-CFG-02/03, warning only). | writes the model registry rows |
| `swarm models` | Lists the model registry: declared context length, smoke test result, pinned or not. | reads `config/models.yaml`, the database |
| `swarm init [--apply] [--yes] [--global] [--sandbox] [--include-inactive] [--reuse-credentials-from PROFILE]` | Brings the Hermes profiles to the state ASES needs. Dry run unless `--apply`; `--apply` needs `--yes`. Never reads or prints a credential; `--reuse-credentials-from` copies only the one variable named by the provider's `key_env`. | Hermes profiles under `hermes.native_home` (backups next to each changed file) |
| `swarm plan --repo R --request T` | Asks the Lead profile to write `docs/ases/plan.json`. Slow on a free provider (30 minute timeout). | `R/docs/ases/plan.json` |
| `swarm critique --repo R --request T [--auto-replan] [--profile P] [--timeout S]` | Gate 0 and the plan critique. Exit 0 only for PASS. | database (critique rounds) |
| `swarm approve --repo R --project-id P [--yes] [--skip-critic] [--deadline-minutes N]` | Gate P (section 2, step 5). | a commit in `R`, gate pin, plan tasks, Hermes cards |
| `swarm run --repo R [--max-iterations N] [--sleep-seconds S] [--ignore-reconcile]` | The loop (section 4). Defaults: 30 passes, 20 seconds apart. | everything |
| `swarm questions --repo R` | Lists the open questions with their cards. Exit 0, also when there are none. | reads only |
| `swarm answer CARD "TEXT" [--author NAME]` | Adds the answer as a card comment (author `user` by default) and unblocks the card. The text is never printed back. Needs no repository. | the card |
| `swarm status --repo R` | One-screen status. Read only. | reads only |
| `swarm report --repo R [--html] [--out DIR]` | The full report on the terminal. With `--html` or `--out` also writes the page and its JSON copy. `--out` inside the repository is refused. | `report.html`, `report.json` (section 6) |
| `swarm stop [--repo R] [--reason TEXT]` | The kill switch, within 30 seconds: sets the stop flag, `hermes pause`, reclaims running cards, terminates verified worker process trees, stops sandbox containers, writes a stop report. Without `--repo` it stops every project found in the database. Exit 0 when everything stopped in time, 1 otherwise. A merge or gate step already running inside a `swarm run` process is not interrupted: the flag stops the loop and the merge queue between steps. | `stop-<UTC>.json` (section 6), project state |
| `swarm resume [--repo R] [--extend-minutes N]` | Lifts a stop or a pause. With `--repo` it runs reconcile-on-start first and stays stopped if anything is blocked. `--extend-minutes` moves the project deadline to N minutes from now (the way out of a wall-clock pause). A paused project becomes running again. | project state |
| `swarm eval ...` | Everything after `eval` goes to the evaluation harness (`list`, `run`, `report`, `compare`). It is a dry run unless you pass `--spend-quota`. **Needs your approval:** it spends real provider requests. | `<ases_home>/evals` |
| `swarm clean --repo R [--apply]` | Finds, and with `--apply` removes, leftover worktrees and merged branches (section 10). Dry run by default. Exit 1 when it reported errors. | git worktrees and branches, `hardening_removed` events |
| `swarm retention [--days N] [--apply]` | Removes old logs, reports, stop reports, evaluation results and database backups (section 10). Dry run by default. Without `--days` it uses the longer of `retention.logs_days` and `retention.reports_days`. Exit 1 when refused (days below 1) or on errors. | files under `<ases_home>` |

## 4. What `swarm run` does

At start, in this order. Each refusal names its exit code (section 5).

1. Gate 0 on the plan file (exit 1 when it fails, every error printed).
2. The gate pin: the gate profiles must still match the hash pinned at the last `swarm approve` (exit 1 otherwise, ASES-QG-02).
3. The primary-checkout guard: on the integration branch, clean, no stray files (exit 3 otherwise). Then the current
   HEAD is adopted as the one ASES expects.
4. The project state: a `stopped` or `finished` project, or a `paused` one, is refused (exit 4, the message says why).
   Otherwise the project wall clock starts.
5. Reconcile-on-start (ASES-REC-04): the board, git and the database are compared. What is safe is repaired and printed as
   `[RECONCILE] repaired ...`; what would need a guess is printed as `[RECONCILE] BLOCKED ...` and ends the run with exit 5
   unless you pass `--ignore-reconcile` (printed loudly, and to be used only when you understand each blocked line).

Then, each pass (one line is printed per pass: `[pass N] parked=... merged=... sent_back=... finished=...`, with further
counters only when they are not zero):

| Step | What happens |
| --- | --- |
| 0 | A stopped or paused project ends the pass at once. |
| 1 | The primary-checkout guard again. A violation halts the run with exit 3 before anything is dispatched or merged. |
| 2 | Idle worktrees: a worktree no running card owns whose HEAD or status changed is reported as a WARNING. |
| 3 | Usage: every finished worker session is counted once into the request ledger (section 8). |
| 4 | Recovery: failed runs are classified (rate limit, quota, infrastructure, auth, policy, context, tool calling, capability, runtime), retried, a model is switched, or the user is asked; lineage budgets are enforced. |
| 5 | Bounds: reaching the project wall clock or the re-plan limit pauses the project and writes a paused report (exit 4). |
| 6 | The budget gate parks cards the provider cannot afford today; cards whose budget is available again are unparked. |
| 7 | Review-lane policing: Gate 1 is re-run on every card that entered review; a red result sends it straight back. |
| 8 | `hermes kanban dispatch`, then per-card resources (ports, database names, `.env.ases`) are provisioned. |
| 9 | The merge queue, one merge at a time: candidate, Gate 3, fast-forward, merge card done. |
| 10 | When every merge card is done: Gate 4, Gate 5 and the release report. |

The idle-worktree check, the usage ingest, recovery, bounds, unparking, provisioning and the final gates are not safety
critical: when one raises it is recorded as a `pass_step_error` event (and a warning) and the pass carries on, so a stale ledger
never stops the merge queue. An exception in the guard, the budget gate, the review lane, dispatch or the merge queue ends the
pass: it is retried after the sleep, and five failed passes in a row end the run with exit 2.

## 5. Exit codes of `swarm run`

| Code | Meaning | What to do |
| --- | --- | --- |
| 0 | Finished: every merge card done, Gates 4 and 5 green, release report written. | Read the release report. |
| 1 | The iteration bound was reached without finishing (not a failure), or the run was refused before it started: Gate 0 failed, or the gate configuration changed since the last `swarm approve`. | Re-run; or fix the plan; or re-run `swarm approve` if the gate change was intended. |
| 2 | Five failed passes in a row (a Hermes timeout, a locked database, a bug). | Read the `[pass N] ERROR` lines, fix the cause, re-run. |
| 3 | The primary checkout is not in a state ASES can trust (wrong branch, uncommitted changes, HEAD moved). | `docs/runbook.md`, "Primary checkout violation". |
| 4 | The project is stopped or paused, or a final gate failed. The message says why. | `swarm status`, then `swarm resume`. |
| 5 | Reconcile-on-start found something it could not repair. | `docs/runbook.md`, "Reconcile block". |
| 130 | Ctrl-C. Nothing is lost: the state is on the board and in git. | Re-run. |

## 6. Files and directories ASES writes

`<ases_home>` is `project.ases_home` (on this machine `C:/Users/masoo/ases/data`). It must not be under OneDrive.
Reports and stop reports are always written outside the repository: a file inside it would dirty the primary checkout and
trip the guard.

| Path | What | Removed by |
| --- | --- | --- |
| `<ases_home>/ases.db`, `ases.db-wal`, `ases.db-shm` | The ASES database (SQLite, WAL mode). The two sidecar files exist while a process has it open. | never |
| `<ases_home>/ases.db.bak-v<from>-<UTC>` | A copy made before a schema migration (section 9). The newest 5 are kept. | `swarm retention` (older than the window, keeping the newest 3) |
| `<ases_home>/reports/<project>/<UTC>/report.html`, `report.json` | `swarm report --html`. A `-2`, `-3` suffix when the name is taken. | `swarm retention` |
| `<ases_home>/reports/<project>/<UTC>-paused/` | The report the controller writes when it pauses a project (bound reached, final gate failed). | `swarm retention` |
| `<ases_home>/reports/<project>/<UTC>/release.md` (+ `report.html`, `report.json`) | The release report written when the final gates are green. | `swarm retention` |
| `<ases_home>/stops/<project>/<UTC>/stop-<UTC>.json` | The stop report of `swarm stop`. | `swarm retention` |
| `<ases_home>/evals/eval-<UTC>/` | Evaluation results (`results.jsonl` and the redacted raw output). | `swarm retention` |
| `<ases_home>/logs/` | Reserved: retention covers it, nothing writes there yet. | `swarm retention` |
| `<repo>/docs/ases/plan.json` | The plan. Committed to the integration branch by `swarm approve`. | never |
| `<repo>/.worktrees/<card id>` | A worker's worktree (Hermes). Stays after the card is done until `swarm clean --apply`. | `swarm clean` |
| `<repo>/.worktrees/<card id>/.env.ases` | The card's ports, compose project name, database names and temp directory. Git-ignored through the repository's `info/exclude`. | with the worktree |
| `<system temp>/ases-merge-<random>/candidate` | The throwaway merge candidate worktree. Removed by the merge queue itself; a kill leaves it behind. | `swarm clean` |
| `<hermes native home>/profiles/<name>/` | Hermes profiles. `swarm init --apply --yes` writes config and SOUL files here, with a backup next to each. | you |

Every event ASES records (`events` table) is redacted for secret shapes before it is written. Transcripts and worker logs
that Hermes keeps are Hermes's, in its own home; they stay on the local disk and ASES never uploads them.

## 7. Configuration

Two files in `config/`. Both are read on every command, so an edit takes effect at the next command; a running
`swarm run` reads them once at start.

### 7.1 `config/swarm.yaml`

| Key | Meaning | Now |
| --- | --- | --- |
| `project.name` | Names the report directories and the project in reports. | `ases` |
| `project.environment` | `native` or `wsl2`. | `native` |
| `project.data_class` | `public`, `private` or `confidential`. Gate P refuses a plan whose providers do not fit (private needs `no_training`, `local_only` or `zero_data_retention`; confidential needs `local_only`). | `public` |
| `project.workspace_root` | Where repositories and worktrees live. Refused under OneDrive. | `C:/Users/masoo/ases-workspaces` |
| `project.ases_home` | Where the database and reports live. Refused under OneDrive. | `C:/Users/masoo/ases/data` |
| `project.board` | The Hermes kanban board. | `ases-phase3` |
| `project.integration_branch` | The branch only the merge queue writes. | `integration` |
| `roles` | Plan task role to Hermes profile (`lead`, `coder`, `reviewer`). | `lead`, `coder-1`, `reviewer` |
| `concurrency.max_in_progress` | Cards in progress at once. `swarm init --apply --yes --global` writes it to Hermes `kanban.max_in_progress` (a setting of your whole Hermes, shared by every Hermes project). | 3 |
| `concurrency.per_profile` | Per profile; written to Hermes `kanban.max_in_progress_per_profile` the same way. | 1 |
| `concurrency.hard_max` | The most `max_in_progress` may ever be; a larger value is refused. | 6 |
| `budgets.attempts_per_card` | Attempts per card; also passed to Hermes as the card's `--max-retries`. | 3 |
| `budgets.review_rounds_per_task` | Review rounds per task before escalating. | 3 |
| `budgets.fix_cards_per_task` | Fix cards per task before the merge card asks you. | 2 |
| `budgets.replans_per_project` | Re-plans per project; reaching it pauses the project. Also the critique round limit. | 2 |
| `budgets.max_cards` | Gate 0 rejects a plan with more tasks. | 40 |
| `budgets.card_runtime_minutes` | Passed to Hermes as `--max-runtime`. | 45 |
| `budgets.daily_reserve_percent` | Share of a provider's daily cap never planned to be used (section 8). | 10 |
| `budgets.review_reserve_requests` | Requests held back for the review pass (section 8). | 20 |
| `hermes.tested_version` | The Hermes version ASES was last checked against; `swarm doctor` warns on a mismatch. | `0.21.3` |
| `hermes.native_home` | The Hermes home, where the profiles live. | `C:/Users/masoo/AppData/Local/hermes` |
| `sandbox.enabled` | Worker sandbox switch. On since 2026-09-28 (round 16, `docs/stage-b-2026-09-28.md`): controller gates and the `coder-1` worker profile run through Docker on the pinned image; no real Hermes worker has run inside Docker yet. Other keys: `terminal_backend` (docker), `network_default` (false), `mount` (`worktree_only`), `forward_env`, `network_exceptions`; optional `image`, `cpu`, `memory_mb`, `pids_limit`, `extra_deny`. A key that is not known is an error. | `true` |
| `retention.logs_days` | Days to keep logs. Whole number, 1 or more. | 30 |
| `retention.reports_days` | Days to keep reports. Whole number, 1 or more. | 90 |

### 7.2 `config/models.yaml`

`providers:` one entry per provider, `models:` one entry per model.

| Key | Meaning |
| --- | --- |
| `providers.<name>.type` | `openrouter`, `openai_compatible` or `hermes_provider`. |
| `providers.<name>.base_url` | The API base URL (openai_compatible). |
| `providers.<name>.key_env` | The NAME of the environment variable that holds the key. ASES never holds the key: it is in the Hermes profile's `.env`. |
| `providers.<name>.limits` | `rpm`, `per_model_rpm` (pacing), `per_day`, or `per_day_default` with `per_day_after_credits` (the daily request cap). Empty means no known cap. |
| `providers.<name>.credits_purchased` | Selects `per_day_after_credits`. Flip it only after you have actually bought credits. **Needs your approval** (spends money). |
| `providers.<name>.data_policy` | Compared with `project.data_class`. |
| `providers.<name>.data_policy_verified_at`, `data_policy_source` | Optional. ASES-PRV-04: a `private`/`confidential` project needs more than a compatible `data_policy` string -- it needs an explicitly verified one. `data_policy_verified_at` is the ISO date (quoted) a human actually checked `data_policy`, `data_policy_source` is a URL or short note saying where. Absent is fine for a `public` project; `policy.check_data_class` refuses `private`/`confidential` for a provider with a compatible policy but no `data_policy_verified_at`. |
| `providers.<name>.status`, `verified_on`, `quota_endpoint`, `require_parameters` | Notes and provider switches. |
| `models[].provider`, `models[].model` | The pair, as the provider spells the model. |
| `models[].context_length` | Declared context. `null` means undeclared: `swarm doctor` warns until it is at least 64,000. |
| `models[].tool_calling` | `true` once a real tool call has worked. |
| `models[].role_class` | `lead`, `coder`, `reviewer`, or a candidate or unfunded label. |
| `models[].pinned` | Whether this row is the model in use for its role. |
| `models[].data_policy` | Overrides the provider's policy for this model. |

The numbers in this file were read from the providers' own pages and change. Re-verify them before relying on them for
capacity planning, and update `verified_on` when you do.

**Key pooling (ASES-CFG-02/03, section 10.2).** Prefer one key per provider, and prefer several distinct, legitimate
providers over trying to stretch one further. A pool of keys on the SAME real-world account does not create extra
quota on the free paths this project uses (each provider's limits are per account, not per key), so pooling keys is
wasted complexity, not more capacity. Never create or rotate an account to get around a limit or an abuse control --
that is a violation of the provider's own terms, not a supported way to run ASES. Both rules are about your
account-management behavior, which ASES cannot see or enforce directly; what it CAN check from its own config is the
one signal visible there: two DIFFERENT provider entries in `config/models.yaml` pointing at the same `key_env`. That
is a real, if imperfect, proxy for "the same account under two provider names", so `swarm doctor` WARNs on it (the
`key_pooling` row) without ever failing the run over it -- one provider used by several profiles is normal and is not
what this flags.

**Adding a provider.** `docs/provider-onboarding.md` is the checklist (ASES-CAP-06): discovery, smoke test, data-policy
check, evaluation, each tied to the real command or field that supports it, plus a copy-paste `config/models.yaml` stub.

## 8. The request ledger and parking

Every agent tool call costs one request against some provider's daily quota. ASES counts them itself so it can refuse to
start a card it cannot afford, instead of finding out half way.

- Counting. Each pass, every finished Hermes worker session is read once (`hermes sessions export`) and its API call count is
  added to `requests_ledger` under the day's UTC date, per provider and model. A session is remembered in
  `usage_ingested`, so it is never counted twice. The daily cap of a provider is per provider (all its models together).
- Affordability. For a provider with a known daily cap: `usable = remaining - review_reserve_requests -
  floor(cap * daily_reserve_percent / 100)`. A card may run when `usable` covers its task's `estimated_requests`. A provider
  with no known cap is never parked. A coder card is also parked when the reviewer's provider cannot afford the review
  reserve.
- Parking. A card that cannot be afforded is scheduled with `hermes kanban schedule`, with a reason that starts
  `budget:` or `review budget on <provider>:` and says the numbers (`needs N, only M usable today after ...`). Nothing else
  is ever parked by ASES, and ASES never unparks a card someone else scheduled.
- Reset. The ledger is keyed by UTC date, so the count starts again at 00:00 UTC. Each pass the controller recomputes
  affordability for parked cards and unparks the ones that fit again (comment `budget available again`, event
  `card_unparked`). There is nothing to do by hand.
- Where to look. `swarm status` and `swarm report` show the budget per provider. `swarm approve` prints the same numbers before
  you commit to a plan, and refuses a plan that cannot be afforded today.

## 9. The database

- One file, `<ases_home>/ases.db`, WAL mode, foreign keys on. Tables: `requests_ledger`, `model_registry`, `events`,
  `plan_tasks`, `gate_runs`, `merge_records`, `gate_pins`, `usage_ingested`, `review_verdicts`, `integrity_state`,
  `lineage`, `project_state`, `intents`, `resource_leases`, `worktree_snapshots`, and `schema_migrations`.
- Migrations. `src/ases/db.py` holds a numbered list. Opening the database (every command does) applies the ones it is missing,
  each in its own transaction, and records `(version, applied_at)` in `schema_migrations`. The current version is 7. A failing
  migration is rolled back and `MigrationError` names its number.
- Backups. Before the first pending migration touches a database that already holds data, the file is copied (with the SQLite
  backup API, safe while the database is in use) to `ases.db.bak-v<from>-<UTC timestamp>` in the same directory. The newest 5 are
  kept. These are the only automatic backups: there is no periodic one. They are whole copies of the database, so keep them out
  of git: the repository `.gitignore` must list `data/*.db.bak-*` (when this was written it listed only `data/*.db`,
  `data/*.db-wal` and `data/*.db-shm`, so the backups showed up as untracked files).
- A database from a NEWER ASES (a version above the highest this code knows) is refused with a clear error and is not touched.
  Never open it with an older ASES; upgrade ASES instead.
- To take a backup by hand while nothing is running `swarm run` (PowerShell, from the repository):

  ```
  python -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()" C:/Users/masoo/ases/data/ases.db C:/Users/masoo/ases/data/ases.db.manual-backup
  ```

- Restoring from a backup is in `docs/runbook.md`.

## 10. Retention and cleanup

Both commands are dry runs unless you add `--apply`, print ASCII, and never raise: a problem is a line in the report.

`swarm clean --repo R` removes, only when it can prove each one is safe:

- worktree registrations whose directory is gone (what `git worktree prune` removes);
- leftover merge-candidate worktrees (`ases-merge-*` in the system temp directory) that are at least an hour old, with no
  open candidate-build or fast-forward intent, whose merge record is completed or absent;
- the worktree of a FINISHED card under `<repo>/.worktrees/<card id>` (card done or archived, its task's work and merge
  cards both done or archived, clean tree, HEAD on a branch);
- local `swarm/*` and `merge/*` branches of a finished task that are either fully merged into the integration branch
  (`git branch --merged`) or squash-merged by ASES (the task's merge record is completed, not reverted, names a squash commit
  that is on the integration branch, and every path the branch changed has the same content in that commit).

It never touches: the integration branch, the checked-out branch, a branch with an open worktree, a locked worktree, the
worktree it was pointed at with `--repo`, the worktree or branch of a card that is running, in review, ready, blocked,
scheduled, todo or triage, a branch whose name fits two task keys of the plan (`swarm/T1-a-coder` with tasks `T1` and `T1-a`),
a branch of no task of the plan, and anything whose card cannot be read (each is skipped and listed with its reason). Every removal is a `hardening_removed` event; a deleted branch's event carries the
commit it pointed at, so it can be put back with `git branch <name> <sha>` while git still has the commit. Hermes has its own
`hermes worktree list` and `hermes worktree prune`; the latter never touches kanban worktrees, which is why ASES has this.

`swarm retention` removes files older than the window from `logs/`, `reports/`, `stops/`, `evals/` (a report directory is one
unit, aged by its newest file) and old `ases.db.bak-*` backups. The newest 3 entries of each kind are kept whatever their
age. It never touches `ases.db`, never follows a symbolic link or junction out of the directory it is scanning, and refuses a
window below 1 day. It does not touch the `events` table: the event log is the audit trail, and it is also state (plan critique
verdicts and rounds, the reason a project was paused, the path of the release report, open questions are read from it). Pruning it
is a separate, explicit function (`hardening.retention_events`, with `hardening.vacuum` afterwards) that no command calls. Use it
from a Python prompt only when no project is being planned, approved or run, because it also removes those records.
