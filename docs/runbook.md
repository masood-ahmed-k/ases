# ASES runbook

What to do when something goes wrong. Find the symptom, confirm the cause, take the action. Every command, message, event
name and exit code below was read from the source. How the pieces normally work is in `docs/operations.md`.

Rules that hold everywhere:

- State lives on the Hermes board, in git and in `<ases_home>/ases.db`, not in the `swarm run` process. Stopping or losing
  the process loses nothing; running `swarm run` again starts with reconcile-on-start, which compares the three and repairs
  what is safe.
- Anything marked **Needs your approval** spends money, deletes data, or changes your Hermes installation or configuration.
  ASES does not do these by itself and neither should a script.
- Nothing is deleted without `--apply`. `swarm clean` and `swarm retention` are dry runs by default.
- Do not delete `ases.db-wal` or `ases.db-shm` by hand: they hold committed data until SQLite folds them into `ases.db`.

## 0. The first five minutes

Run these before you decide what is wrong (`R` is the target repository, `B` the board in `project.board`):

| Command | Tells you |
| --- | --- |
| `swarm status --repo R` | Project state (running, paused, stopped, finished) and its bounds, the budget per provider, the parked cards, the cards by state with open questions, the merge queue, the last gate run, recent findings and health events. |
| `swarm questions --repo R` | Every card waiting for a person, with its question. |
| `swarm doctor` | Environment problems (Hermes version, gateway, profiles, sandbox, model registry). |
| `git -C R status` and `git -C R worktree list` | Whether the primary checkout is clean and which worktrees exist. |
| `hermes kanban --board B list` | The board as Hermes sees it (`show <card>` for one card with its events and runs). |
| the last events (below) | What ASES itself did and why. |

The last 20 events, ASCII safe (PowerShell, from the repository):

```
python -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); [print(r[0], r[1], r[2][:200].encode('ascii','backslashreplace').decode()) for r in c.execute('SELECT ts, kind, payload FROM events ORDER BY id DESC LIMIT 20')]" C:/Users/masoo/ases/data/ases.db
```

## 1. A card is blocked for a question

| Symptom | Cause | Action |
| --- | --- | --- |
| `swarm questions` lists a card with a question; the card is `blocked` on the board. Other cards keep running. | A worker called `kanban_block` to ask you something, or the controller asked (a budget or bound reached, a malformed plan or verdict, a merge that will not go through). Source `blocked`. | Read the question, then `swarm answer CARD "your guidance"`. The answer is posted first as a comment (`ANSWER: ...`), then the card is unblocked (`UNBLOCK: answered by user`). Unanswered questions never time out into guesses: the card waits for you. |
| The question text starts `ASES QUESTION:` (a comment, not a block). | The controller asked about a card Hermes will not block (a merge card, which is created blocked, or a card in `todo`). Source `ases_comment`. | Same: `swarm answer CARD "..."`. |
| `swarm answer` prints an error from Hermes after the comment was posted, and the card is still blocked. | The comment went in and the unblock failed. The posted answer marks the question answered, so a second `swarm answer` is refused with `has no open question`, and the failed unblock is not retried by it. | Unblock it yourself: `hermes kanban --board B unblock CARD`. |
| `swarm answer` says `card X has no open question`. | The card is not blocked, or the question was already answered (an `ANSWER:` or `UNBLOCK:` comment, or an `unblocked` event, is newer than the question). | Nothing to do. `hermes kanban --board B show CARD` shows the events. |
| `swarm answer` says `answer refused: possible secret on line N`. | The answer contains a secret-shaped value. Nothing was posted. | Name where the value lives (an environment variable, a file path) instead of the value, and answer again. |

The text of your answer is never printed back by ASES. An answer becomes a comment on the card, which is Hermes's data.

## 2. A card gave up

| Symptom | Cause | Action |
| --- | --- | --- |
| `swarm questions` shows `gave up after N failure(s): <error>`. The card is `blocked`, and its events include `gave_up`. | Hermes's circuit breaker tripped after `attempts_per_card` consecutive failures (ASES passes it as the card's `--max-retries`, default 3). Hermes writes no `blocked` event for this, which is why it is a separate source. | Read the error. What it means decides the action (next rows). |
| Error mentions 401, 403, "invalid key", "unauthorized". | Auth failure: the credential is bad. Retrying would only loop, so ASES does not. | Rotate or fix the key (section 14), then `swarm answer CARD "key fixed"`. |
| Error mentions a daily or per-day limit, "quota", "tokens per day". | Provider quota. A short throttle (429 with Retry-After) is waited out by Hermes and never reaches you; this is the daily kind. | Section 9. |
| Error mentions the data policy or "no endpoint". | The provider refused on data policy. ASES never relaxes `project.data_class`. | Pick another provider or model in `config/models.yaml`; do not lower the data class to make it go. |
| Error mentions context length or "request too large". | The model's window is too small for this task. | Split the task, or pin a model with a bigger declared `context_length`. |
| Error mentions malformed tool calls. | The model cannot drive tools. | Reject the model for agent roles (`pinned: false`, choose another). |
| Timeouts, 5xx, "pid N exited", "not alive". | Infrastructure: says nothing about the model or the task. ASES resumes these by itself first. | If it gave up anyway, check the provider status and Hermes (`swarm doctor`), then answer to retry. |
| The card is in `triage`, not `blocked`, and the question says the block repeated. | Hermes routes a second block of the same kind (after an unblock) to `triage` (`block_loop_detected`). A `triage` card cannot be unblocked: it leaves triage only through `hermes kanban specify CARD`, which makes an auxiliary model call. | `swarm answer` still posts your answer on the card (it is not lost) and then says what is left. ASES never runs `specify` by itself. **Needs your approval** (it spends requests): decide whether to run it, or re-plan the task. |

After you fix the cause, `swarm answer CARD "what changed"` unblocks the card and Hermes resets its failure counter. Above
Hermes's limit ASES keeps its own per-task counters (`attempts_per_card`, `review_rounds_per_task`, `fix_cards_per_task`,
`replans_per_project`); a spent counter turns into a question for you, never into another silent retry.

## 3. A merge card is blocked for the fix-card budget

| Symptom | Cause | Action |
| --- | --- | --- |
| The merge card of a task is blocked, the question begins `Fix-card budget (N) exhausted for T1: the merge keeps failing and ASES will not open another fix card. Last failure: ...` and ends `How should this be resolved?`. Event `fix_card_budget_exhausted`. | The merge (candidate build or Gate 3) failed, ASES opened `budgets.fix_cards_per_task` fix cards (`T1: fix (round N)`, branches `swarm/T1-fixN`), and it still fails. ASES stops spending. | Read the last failure in the question (it is redacted and capped at 500 characters) and in `swarm report`. Decide, then either row below. |
| The failure is a real conflict or a red test that needs judgement. | The task is wrong or too big. | Fix the plan: `swarm stop`, edit `docs/ases/plan.json`, `swarm critique`, `swarm approve` (re-pins the gate profiles), `swarm resume`, `swarm run`. |
| You want one more attempt. | The budget is a setting. | Raise `budgets.fix_cards_per_task` by one in `config/swarm.yaml` (ASES config, not Hermes config), stop and start `swarm run` (it reads the file once, at start), then `swarm answer MERGE_CARD "one more round: what the fix must do differently"`. The answer unblocks the merge card and the queue retries. |

While the question is open the merge queue skips the task: it does not re-run the merge and does not ask again. Answering
without changing anything makes the queue retry once and ask again if it fails again.

## 4. A red Gate 1 or Gate 3

| Symptom | Cause | Action |
| --- | --- | --- |
| A pass line shows `sent_back=['T1']`; event `gate1_recheck_failed`; the card returns to its worker with the gate output (the end of the output, where the failure summary is) as the reason. | Gate 1: the controller re-ran the pinned commands on the exact commit that entered review and they failed, or the change is outside the task's `touches`, or the branch cannot be resolved. It is a failed attempt, not a review round; no reviewer turn is spent. | Usually nothing: the worker fixes it. If it keeps failing, the attempts budget will block the card (section 2). |
| Gate 1 is red on a fresh checkout for every task. | The gate command itself is broken (a missing tool, a wrong path). | Fix the gate profile in the plan, run `swarm approve` again to re-pin it. Without re-approval `swarm run` refuses to start with exit code 1 (ASES-QG-02). |
| Event `merge_failed`, then a card `T1: fix (round N)` appears with the failure detail. | Gate 3 (or the squash) failed on the merge candidate: a conflict with the integration branch, or a red gate on the combined result. The integration branch was not touched. | Wait for the fix card. After `fix_cards_per_task` rounds, section 3. |
| Event `merge_race_retrying`. | Only the fast-forward was refused because the integration branch moved underneath the candidate (verified in git). Gate 3's verdict still stands. | Nothing: it is retried on the next pass at no cost. |
| The merge is refused with "branch moved after the pre-merge checks", a `stale_review` or `unbound_review` reason. | A commit was added to the work branch after it was reviewed and gated, or the approval names no commit (ASES-GIT-03). | The card must be reviewed and gated again on its new head; nothing to do by hand. |
| The merge is refused because the primary checkout is on another branch or detached. | `git merge --ff-only` would move whatever is checked out, so ASES refuses. | Section 6. |

## 5. A tamper finding

| Symptom | Cause | Action |
| --- | --- | --- |
| The card is sent back with lines naming a kind of finding; event `tamper_blocked`. | Gate 1 reads the diff itself. Every finding blocks unless it marks itself informational. Kinds: `test_file_deleted`, `test_deleted`, `skip_marker`, `unconditional_pass` (for example an "or true" appended to a test command so it cannot fail), `assertion_weakened`, `gate_config_changed`, `generated_artifact`, `secret_added` (names the file and line, never the value), `large_file`, `coverage_lowered`. | If the worker really removed or skipped a test to get green, the card going back is the right outcome: leave it. |
| The finding is a false positive: a config or assertion change the task is meant to make. | The rules for gate configuration and assertions do not fire on paths inside the task's own `touches`. No `touches` can allow a skipped or deleted test, an unconditional pass or a secret. | Widen the task's `touches` in the plan (a plan change: `swarm critique`, `swarm approve`), or have the worker restore the file. |
| `secret_added`. | A secret-shaped value is in the diff. | Treat the value as leaked: rotate it (section 14). Remove it from the branch history before it can merge. |
| Event `tamper_check_error`; the card is NOT sent back. | git could not produce the diff (bad range, timeout). A check that could not run says nothing about the card, and the merge-time check repeats it and fails closed. | Check git health in the repository and let the next pass retry. |

## 6. Primary checkout violation (exit code 3)

| Symptom | Cause | Action |
| --- | --- | --- |
| `swarm run REFUSED (ASES-GIT-12): the primary checkout is not in a state ASES can trust`, then up to 20 lines. Exit 3. | At start the primary checkout must be on the integration branch and clean. Lines: `primary checkout is on branch X, expected Y`, `has a detached HEAD`, `is dirty: M 'path'` (also `??` for untracked, `A`, `D`, `R`), `cannot read HEAD`, `git status failed`, `cannot inspect the primary checkout`. | `git -C R status` and `git -C R log --oneline -5`. Put the checkout on the integration branch and clean it: commit what you meant to keep, discard what you did not (**discarding files deletes data: your decision**). Run `swarm run` again. |
| The only dirty path is `docs/ases/plan.json` (`??`). | `swarm plan` wrote the plan and it has not been committed. | `swarm approve` commits it. |
| Mid-run: `[pass N] SECURITY EVENT: the primary checkout changed outside the controller`, then the problems. Event `integrity_violation`. Exit 3. | Something other than the merge queue changed the primary checkout while workers were running. A worker (the reviewer has file-write tools) may have been handed the primary checkout's path, or you edited it. Also `primary checkout HEAD moved: expected A, found B`. | Find out what wrote there before anything else: `git -C R diff`, `git -C R log`. Restore the checkout, then run again. At start `swarm run` adopts the current HEAD as the expected one, so a commit you made on purpose is accepted after the fact and later merges build on it. |
| Worker worktrees do not count as dirt. | Hermes keeps them under `R/.worktrees/`, and the guard ignores that prefix. | Nothing to do. |

## 7. Reconcile block (exit code 5)

`swarm run` prints every reconcile line as `[RECONCILE] found|repaired|note|BLOCKED <task> <kind>: <detail>` and ends with
exit 5 when anything is BLOCKED. `--ignore-reconcile` starts anyway; it prints a warning and the blocked items are NOT fixed,
so work may be dispatched on top of them. Use it only when you understand each blocked line. `swarm resume --repo R` also
stays stopped while any of these remain.

| Kind | Meaning | Repaired by reconcile? | Action when blocked |
| --- | --- | --- | --- |
| `merge_done_without_record` | A merge card is done but `merge_records` has no completed record. | Yes, when the integration branch has a commit carrying `Merge card: <id>` (or the task is review-only). | Otherwise look at `git log` on the integration branch: did the merge happen? |
| `merge_record_without_done_card` | Crash between the fast-forward and completing the card. | Yes: the card is completed as `merged <sha> (recovered)`. | None. |
| `merge_unfinished` | A merge is not done and its record was never completed. | Yes when git shows the squash commit landed. Blocked when the card is in a state reconcile does not complete (it completes only `blocked`, `ready`, `todo`) or when the commit was reverted. | A person must look: `hermes kanban --board B show CARD` and `git log`. |
| `candidate_discarded` (note) | A candidate never landed. | Informational; nothing is changed. | None: the merge queue redoes it. |
| `done_but_reverted` | A merge card reads done but the merge was reverted afterwards (`merge_records.reverted = 1`). | No. | Decide whether to reopen the work (a new fix card) or accept the revert. |
| `revert_unfinished`, `revert_unrecorded` | A revert was started but not finished or not recorded. | No. | Compare `git log` with the record; complete or undo the revert by hand, then run again. |
| `missing_card` | A work or merge card of the plan no longer resolves on the board. | No. | If it was archived by mistake, restore it in Hermes. If it was removed, `swarm approve --repo R --project-id P` again: card creation is idempotent by plan key and records the new ids. |
| `worker_gone` | A `running` card whose worker process is not alive. | Yes: the card is reclaimed. | None. |
| `running_without_pid` | A `running` card that records no worker pid. | No. | `hermes kanban --board B reclaim CARD`, then run again. |
| `missing_worktree` | A `running` card whose worktree directory is gone. | No. | Reclaim the card the same way; Hermes makes a fresh worktree on the next dispatch. |
| `orphan_worker` | A live process whose command line names a card that is not running. | Yes: terminated (never when the command line cannot be read or does not name the card). | If it could not be terminated, section 12. |
| `orphan_worktree` (note) | A worktree under `.worktrees/` of a card that is archived or gone. | Reported, not removed, not blocked. | `swarm clean` (section 13). |
| `open_intent` | An action (`create_cards`, `run_gate`, `build_candidate`, `fast_forward`, `complete_merge_card`, `revert`) was started and never completed, and its task is not settled. | Closed automatically once the task is consistent. Otherwise blocked. | For `create_cards`: re-run `swarm approve` (it is idempotent). For the others: fix the task's other findings; the intent then closes. |
| `reconcile_error` | One git or Hermes call failed for a task; the other tasks carried on. | No. | Check Hermes (`swarm doctor`) and git, then run again. |

## 8. A stopped or paused project (exit code 4)

| Symptom | Cause | Action |
| --- | --- | --- |
| `swarm run REFUSED: project P is stopped (<reason>)` or `[pass N] STOPPED: <reason>`. | `swarm stop` set the stop flag (`project_state.status = stopped`, with `--reason`). | `swarm status` to confirm; when it is safe, `swarm resume --repo R`. It reconciles first and refuses (`NOT resumed: <reason>`) while anything is blocked (section 7). |
| `swarm run REFUSED: project P is paused (a bound was reached or a final gate failed)`. A directory `<ases_home>/reports/<project>/<UTC>-paused/` was written; event `project_paused` has the reason. | The controller paused the project: the project wall clock passed, or `replans_per_project` was reached, or a final gate failed. | Read the paused report. Wall clock: `swarm resume --repo R --extend-minutes N`. Re-plans: decide (section 3), then `swarm resume`. |
| `[pass N] FINAL GATE FAILED: the project is paused, not finished.` Exit 4. | Gate 4 (security) or Gate 5 (smoke) failed on the integration HEAD. The question and the first lines of the gate detail are printed. | Fix the cause on the integration branch (or re-plan), `swarm resume --repo R`, then `swarm run`: the final gates run again. |
| `swarm resume` prints a WARNING that the project deadline has already passed. | The wall clock ran out; the next pass would pause again. | `swarm resume --extend-minutes N`. |
| After `swarm stop`: `paused=NO` or `flag=NO`, or `within_deadline=NO`. Exit 1. | `hermes pause` failed (new cards may still be dispatched), or the flag could not be written (a running `swarm run` will not see the stop). | Read the notes under the summary and the `stop-<UTC>.json` report. Fix Hermes, run `swarm stop` again. A merge or gate step already running inside `swarm run` is not interrupted; the loop halts between steps. |
| Hermes dispatch is stopped although the project runs. | Someone ran `hermes pause`. | `swarm resume` (it also calls `hermes resume`). |

## 9. Provider quota exhausted

| Symptom | Cause | Action |
| --- | --- | --- |
| `[pass N] parked=['T2']`; cards `scheduled` whose reason starts `budget: needs N, only M usable today after the R-request review reserve and P% daily reserve (X remaining of Y)`. | The ledger says the provider's daily cap, less the two reserves, cannot cover the task's `estimated_requests` (`budgets.review_reserve_requests`, `budgets.daily_reserve_percent`). | Wait for the reset (section 10). Nothing else is needed: the card runs when it fits. |
| The reason says `review budget on <provider>: ...`. | A coder card is parked because the reviewer's provider cannot afford the review pass its finished work will need. | Same. |
| `swarm approve` says `Gate P REFUSED: cannot afford this plan today`. | The estimate for the whole plan exceeds today's usable requests. | Shrink the plan, or wait for the reset, or use another provider. |
| Cards fail with a daily-quota error even though the ledger says there is room. | The ledger only counts what ASES has seen (finished sessions). Other users of the same key, or a provider that counts differently, are invisible to it. | Compare with the provider's own dashboard (`quota_endpoint` for OpenRouter). Lower the cap in `config/models.yaml` `limits` until the two agree. |
| You want more requests today. | The free tier is small (for OpenRouter 50 a day without credits, 1000 with). | **Needs your approval** (spends money): buy credits yourself, then set `credits_purchased: true` in `config/models.yaml`. ASES never buys anything. |
| A daily reserve is too strict for a small provider. | The reserves are settings. | Lower `daily_reserve_percent` or `review_reserve_requests` in `config/swarm.yaml` only knowingly: the reserve is what keeps the review pass affordable. |

A short throttle (HTTP 429 with Retry-After) is a different thing: Hermes waits and retries, ASES does not park for it and it
does not count as a failure.

## 10. A quota reset

| Symptom | Cause | Action |
| --- | --- | --- |
| It is a new UTC day and parked cards are still `scheduled`. | Parked cards are released by the controller, and only while `swarm run` is running: each pass recomputes affordability and unparks what fits. | Start `swarm run --repo R` (exit 1 after the iteration bound only means re-run it). The pass prints `unparked=[...]`; each card gets the comment `UNBLOCK: budget available again` and an event `card_unparked`. |
| A card stays `scheduled` although the ledger has room. | Its latest `scheduled` reason does not start with `budget:` or `review budget`, so someone else scheduled it and ASES leaves it alone by design. | `hermes kanban --board B show CARD` for the reason. If you scheduled it, release it yourself: `hermes kanban --board B unblock CARD`. |
| The provider resets at a different hour. | The ledger is keyed by UTC date and resets at 00:00 UTC; providers use their own clocks. | If the provider refuses before 00:00 UTC, the card fails with a quota error and is parked; if the provider has reset earlier, ASES still waits for 00:00 UTC. Adjust the estimate in `config/models.yaml` `limits` if that matters. |

## 11. The controller crashed

| Symptom | Cause | Action |
| --- | --- | --- |
| The `swarm run` window was closed, the machine lost power, or the process died. Cards may be `running` with dead workers; a merge candidate may be half built; an intent may be open. | Nothing is lost: every multi-step action writes an intent before and a completion after, and the state is on the board, in git and in the database. SQLite recovers its own write-ahead log on the next open. | `swarm run --repo R`. Reconcile-on-start prints what it repaired (`[RECONCILE] repaired ...`); a `candidate_discarded` note is normal (the merge queue redoes it). Blocked lines: section 7. |
| A worker keeps running after the controller died. | Orphan worker. | Section 12. |
| A directory `ases-merge-...` is left in the temp directory and registered in git. | The kill hit a candidate build. | Section 13. |
| `swarm run` ends with exit 2. | Five failed passes in a row (a Hermes timeout, a locked database, a bug). Each is recorded as a `pass_error` event and printed. | Read the `[pass N] ERROR` lines, fix the cause (often Hermes), run again. The plan and cards are untouched. |

## 12. Orphan workers

| Symptom | Cause | Action |
| --- | --- | --- |
| A process `hermes_cli.main -p coder-1 --cli ... chat -q "work kanban task <card id>"` is alive for a card that is not `running`, or `swarm stop` prints `left alone: card X pid N: <why>`. | A previous controller session died, or a worker outlived its claim. | `swarm run` finds workers by card id in the command line and terminates them at start. `swarm stop` terminates verified worker process trees. |
| ASES left one alone. | It never kills a process whose command line cannot be read or does not name the card: a recycled process id must not take an innocent process with it. | Check the command line yourself. When it is a worker of that card: `taskkill /PID N /T /F` (`/T` takes its children with it). Never send signals to a process id from a script on Windows: signal 0 terminates there instead of probing. |
| Sandbox containers remain after a stop. | Killed workers leave a running container. | `swarm stop` stops those whose name or labels carry a plan card id. Check `docker ps` yourself. |

To find worker processes yourself (PowerShell):

```
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*work kanban task*' } | Select-Object ProcessId, CommandLine
```

## 13. Leftover worktrees and branches

| Symptom | Cause | Action |
| --- | --- | --- |
| `git worktree list` shows `<temp>/ases-merge-XXXX/candidate`. | A kill during a candidate build; the merge queue removes its own candidate on every normal exit. | Run `swarm run` once first (reconcile settles the open intent), then `swarm clean --repo R`, then `--apply`. It waits until the directory is an hour old and its merge record is completed or absent. |
| `.worktrees/t_...` directories for done cards pile up. | Hermes removes a finished card's worktree only when every commit is on a remote-tracking ref, and `hermes worktree prune` never touches kanban worktrees. There is no remote here. | `swarm clean --repo R --apply` removes the worktree of a finished card whose task is finished and whose tree is clean. |
| `swarm/*` branches remain after the merges. | ASES merges by squash, so a merged work branch is not an ancestor of the integration branch and `git branch --merged` never lists it. | `swarm clean` also proves the merge from the merge record (the squash commit is on the integration branch and the branch's content matches it). A branch it cannot prove is listed under "Left alone" with the reason. |
| `swarm clean` lists a worktree under "Left alone: has uncommitted or untracked files". | Git refuses to remove a dirty worktree, and so does ASES. | Look at it (`git -C PATH status`). If it is junk: `git worktree remove --force PATH` (**deletes data: your decision**). |
| `git worktree list` shows `prunable`. | Someone deleted a worktree directory by hand. | `swarm clean --apply` (or `git worktree prune`). A missing worktree of an active card is left alone and listed. |
| A branch was deleted and you want it back. | `swarm clean --apply` records every removal as a `hardening_removed` event, with the commit a branch pointed at (`sha`). | Find the commit with the command below, then `git branch NAME SHA`. It works while git still holds the commit (until git's own garbage collection). |
| Hermes's own view. | | `hermes worktree list` and `hermes worktree prune --dry-run` (its own dry run) show its side. |

The removals recorded so far (PowerShell, from the repository):

```
python -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); [print(r[0]) for r in c.execute('SELECT payload FROM events WHERE kind=?', ('hardening_removed',))]" C:/Users/masoo/ases/data/ases.db
```

## 14. Rotating a provider key

Keys are never in ASES. Each provider entry in `config/models.yaml` names an environment variable (`key_env`, for example
`OPENROUTER_API_KEY`, `XKIRO_API_KEY`); the value lives in the Hermes profile's `.env` under `<hermes native home>/profiles/<profile>/.env`.
ASES never reads, writes or prints `.env` or `auth.json`. The one exception is `swarm init --apply --yes
--reuse-credentials-from PROFILE`, which copies the one variable named by `key_env` into a new profile and prints only the name.

| Step | Action |
| --- | --- |
| 1 | Create the new key at the provider. |
| 2 | **Needs your approval** (it is your Hermes configuration): put the new value into the `.env` of every profile that uses that provider, replacing the old one. |
| 3 | Restart what holds the old value: the Hermes gateway and any running worker. `swarm stop`, then start again, is the safe way. |
| 4 | `swarm doctor`, then `swarm models`. A profile that still has the old key fails with auth errors (section 2, the 401 row). |
| 5 | Revoke the old key at the provider. |
| Leak | If a key appeared in a diff, a card or a log, rotate it at once. ASES redacts secret-shaped values (`sk-`, `nvapi-`, AWS, Google, Bearer, JWT, PEM headers, and more) before it stores an event or a report, and Gate 1 blocks `secret_added`, but redaction is a safety net, not a guarantee. |

## 15. Upgrading Hermes

The tested version is pinned in `config/swarm.yaml` (`hermes.tested_version`, now 0.21.3). `swarm doctor` prints a WARN row when
the installed version differs: "Re-verify the CLI surface before relying on it (Hermes changes fast)".

| Symptom | Cause | Action |
| --- | --- | --- |
| `hermes_version` WARN. | Hermes was upgraded (or downgraded). | Do not assume it still works. Re-verify the surfaces below. **Needs your approval:** upgrading your Hermes installation is your decision, and so is changing the pin afterwards. |

What ASES depends on, and so what to re-verify (read Hermes's `hermes_cli/kanban_db.py`, `kanban_db_dispatch.py`,
`kanban.py`, or probe a throwaway board; the fakes used by the tests mirror 0.21.3, so a green test suite proves ASES still
matches its own fake, not that Hermes still behaves like it):

- `hermes --version` output parses; `hermes doctor`; the gateway dispatcher.
- `hermes kanban --board B ... --json`: the shape of `show` (`task`, `parents`, `children`, `comments`, `events`, `runs`,
  `latest_summary`), `list`, `create` (`--workspace`, `--branch`, `--project`, `--body`, `--idempotency-key`, `--max-retries`,
  `--max-runtime`, `--initial-status`, `--parent`), `dispatch`, `complete --result --metadata`.
- The card state machine: `block` accepts only a `running` or `ready` card (and the CLI has already added its comment when it
  refuses); `block --kind K <id> -- <reason>` (the option must come before the id); a second block of the same kind after an
  unblock goes to `triage` (`block_loop_detected`); the circuit breaker (`--max-retries N` trips on the Nth failure) writes a
  `gave_up` event and no `blocked` event; `unblock`, `schedule`, `promote`, `archive` (soft), `reclaim`, `reopen-review`,
  `request-changes`, `set-model`, `link`, `comment` (with `--` before the text).
- The events and comments ASES parses: `blocked`, `gave_up`, `block_loop_detected`, `unblocked`, `scheduled` payloads; the
  `BLOCKED:` and `UNBLOCK:` comments.
- `hermes pause` and `hermes resume`; `hermes -p PROFILE sessions export --session-id ID --format jsonl --redact -` (the usage
  the ledger counts).
- Worktree layout `<repo>/.worktrees/<card id>` and the worker command line
  `hermes_cli.main -p <profile> --cli ... chat -q "work kanban task <card id>"` (orphan-worker detection depends on it).
- The profile layout `swarm init` writes: `profiles/<name>/config.yaml`, `SOUL.md`, `.env`, and the kanban limits in the global
  config.
- `kanban.review_dispatch` (the reviewer's verdict is the card transition).

Procedure: read the release notes and the diff of the files above; run `python -m pytest`; run `swarm doctor`; run `swarm init`
(a dry run) and read the change list; make a throwaway board and run one small plan end to end before a real project. When it
holds, set `hermes.tested_version` to the new version.

## 16. Recovering the ASES database from a backup

The database is `<ases_home>/ases.db` (with `ases.db-wal` and `ases.db-shm` while open). The only automatic backups are the
copies made just before a schema migration: `ases.db.bak-v<from>-<UTC timestamp>` (the newest 5 are kept). Take a manual backup
before anything risky (`docs/operations.md`, section 9).

| Symptom | Cause | Action |
| --- | --- | --- |
| `MigrationError: ... is at schema version N, newer than the newest this ASES knows`. Any command. | The database was written by a newer ASES. It was refused and not touched. | Do not open it with this code. Upgrade ASES; or, to go back on purpose, restore an older backup (below). |
| `MigrationError: migration N (...) failed and was rolled back, the database stays at version V`. | A migration failed; nothing was applied for it and the file is as it was (a backup was made first). | Read the cause in the message, fix it (disk space, a locked file, a bug), run again. Report a bug with the message. |
| `MigrationError: could not back up ...; nothing was changed`. | The copy before a migration could not be written (disk full, permissions). | Free disk space or fix the directory, run again. A migration is never applied without its backup. |
| `database disk image is malformed`, `file is not a database`, or the file was deleted or overwritten. | Corruption, or a mistake. | Restore below. |

Restore, with nothing running (`swarm stop` first, and make sure no `swarm run` process is left):

1. Keep what is there: copy `ases.db`, `ases.db-wal` and `ases.db-shm` somewhere safe (do not delete them yet).
2. Pick the newest good backup: `ases.db.bak-v<from>-<UTC>` in `<ases_home>`. Its name says the schema version it holds (`v5` is
   before the upgrade to 6 or later). A backup is one whole file, in rollback mode, with no sidecars; check it with
   `python -c "import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute('PRAGMA integrity_check').fetchone())" PATH` (it must
   print `('ok',)`).
3. **Deleting the damaged files is your decision.** Replace `ases.db` with a copy of the backup, and remove the old `ases.db-wal`
   and `ases.db-shm` (they belong to the damaged file).
4. `swarm run --repo R`. Opening the database migrates it forward again if it is behind (after taking a new backup of it), then
   reconcile-on-start compares it with the board and git.

What a restore loses: everything recorded after the backup was made. Reconcile repairs what git and the board can prove (a merge
record is rebuilt from the squash commit that carries `Merge card: <id>`; a merge card that is done gets its record; a running
card whose worker is gone is reclaimed). What it cannot: events (the audit trail, including plan critique verdicts and the
reason a project was paused), gate runs and review verdicts, lineage counters, leases, the project deadline and status. Worker
sessions that finished after the backup are counted again from Hermes's own session records, and counted on the day they
are re-counted, so today's ledger can show more used than really was; that is the safe direction. If the critic verdict is gone,
run `swarm critique` again, or `swarm approve --skip-critic` (recorded as `critic_skipped`). Re-running `swarm approve` is
idempotent for cards and for the plan commit.

With no usable backup: `swarm approve --repo R --project-id P` rebuilds the plan tasks (idempotent by plan key) and reconcile
rebuilds merge records from git, but today's request counts are unknown: check the provider's dashboard before running, and
lower the caps in `config/models.yaml` until they agree.
