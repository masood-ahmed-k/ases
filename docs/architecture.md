# ASES architecture notes (Phase 0/1)

This file tracks what's actually built, not the full design -- that's the blueprint
(`ASES_Swarm_Implementation_Blueprint_v1.2.docx`, Appendix F = `spec/requirements.yaml`). Read the
blueprint first; this is the "where did that requirement end up in code" index.

## Decisions in force

- **D1 = native Windows** (chosen 2026-09-18, not the blueprint's WSL2 default). ASES lives at
  `C:\Users\masoo\ases`, workspaces at `C:\Users\masoo\ases-workspaces`, both outside OneDrive.
  `config.py` refuses to load a config that points back under OneDrive.
- Hermes stays the existing native install at `%LOCALAPPDATA%\hermes` (v0.21.3, default profile,
  `glm-5.3-thinking:free` via UnoRouter). Nothing about that install was touched.
- ~~Provider posture: UnoRouter only until the user adds an OpenRouter key~~ -- stale, superseded by D3.
- **D2 = lead moved from UnoRouter/GLM to a paid OpenAI key, then to xKiro** (2026-09-19, the user's call
  after the GLM complexity-ceiling finding below: "if the GLM is not working then lets use GPT API").
  See the dated sections below for the model choices and the data-policy finding along the way.
- **D3 = UnoRouter dropped entirely, for every role** (2026-09-19, explicit user instruction: "dont use
  unorouter at all - its complete waste"). Both `unorouter`'s provider block and its two model rows
  (former `lead`/`coder`) were removed from `config/models.yaml` outright -- not demoted to `pinned:
  false` like the `_retired`/`_unfunded` rows elsewhere in this file, since this is a closed decision,
  not a "revive later" one. `lead` and `coder-1` are both on xKiro now; `reviewer` was never on
  UnoRouter (it's on OpenRouter, a different, unrelated service despite the similar name). UnoRouter's
  exact former config and the real problems it caused are preserved in git history and this file's
  dated sections below, not reproduced in the live config.

## Module map (section 9.1 of the blueprint)

| Module | What it actually does today | Requirement IDs |
|---|---|---|
| `db.py` | SQLite connection + schema (`requests_ledger`, `model_registry`, `events`) | - |
| `events.py` | Structured event log, redacts credential-shaped keys/values before write | ASES-SEC-01 (partial) |
| `config.py` | Loads/validates `config/swarm.yaml` + `config/models.yaml`; rejects OneDrive paths | ASES-ENV-01, -03 |
| `models.py` | Model registry: declared context length, tool-calling, smoke-test state | ASES-MOD-01, -02, -04 |
| `ledger.py` | Persisted per-provider/model/UTC-day request counts; budget-aware `can_afford()` | ASES-CAP-02, -03, -05 |
| `hermes.py` | The only module that shells out to `hermes`; version + doctor + gateway status | ASES-ARC-04 (partial) |
| `doctor.py` | Every `swarm doctor` check (acceptance test 22.1), honest PASS/WARN/FAIL/PENDING | ASES-CAP-01, -MOD-05 |
| `cli.py` | argparse entry point (`swarm doctor`, `swarm models`); everything else stubbed | - |
| `fakes/provider.py` | Scripted fake OpenAI-compatible HTTP server for tests, stdlib only | ASES-TST-01 |
| `spec/check_requirements.py` | Extracts Appendix F from the live docx, diffs against `requirements.yaml` | ASES-DOC-02 |

## Module map additions (Phase 3)

| Module | What it does | Requirement IDs |
|---|---|---|
| `plan.py` | plan.json schema + Gate 0 (unique keys, no cycles, no dangling deps, criteria/touches/gate_profile present) | ASES-LED-01, -TSK-03 |
| `policy.py` | role -> Hermes profile resolution (config/swarm.yaml `roles:`), budget-gate wrapper over ledger.py | - |
| `gates.py` | runs a gate profile's commands in a throwaway worktree at an exact commit SHA; tamper heuristics | ASES-QG-01, -04 |
| `review.py` | re-runs Gate 1 when a card enters `review`, before trusting it; the reviewer's verdict IS the resulting Hermes status transition, nothing else to parse | ASES-REV-05 |
| `mergeq.py` | squash candidate on integration HEAD -> Gate 3 -> fast-forward; revert on a later failure | ASES-GIT-04, -05, -06 |
| `controller.py` | `create_cards_from_plan` (work+merge pairs, deps wired to MERGE cards per ASES-TSK-02), `run_pass` (one dispatch+review+merge iteration) | ASES-TSK-01, -02 |
| `integrity.py` | touches-path checking (`paths_outside_touches`), worktree before/after snapshots | ASES-GIT-12, -13 |
| `reconcile.py` | startup consistency checks: card IDs resolve, a done merge has a matching record, nothing reads done-but-reverted | ASES-REC-04 |
| `policy.check_data_class` | enforced in `cmd_approve` before Gate P; raises rather than returning a bool | ASES-PRV-01/02/03 |
| `gates.scan_for_secrets` | runs on every merge candidate's full diff before Gate 3; a planted secret blocks the merge | ASES-SEC-01 |
| `controller.process_budget_gate` | re-checks every ready card's affordability on *every* pass (not just once at Gate P) and parks it with `hermes kanban schedule` if the budget's since run out | ASES-CAP-03 |
| `hermes.kanban_schedule` / `kanban_unblock` | added for the parking flow above | - |
| `cli.py` additions | `swarm plan` (invokes `lead`), `swarm approve` (Gate 0 + Gate P publish + budget check + card creation), `swarm run` (bounded loop, reconciles on start), `swarm stop`/`resume` (kill switch) | - |
| `policy.estimate_calendar_minutes` | pacing estimate (not a budget decision) from a provider's rpm limit; `cmd_approve` prints it and then blocks on an interactive confirmation before publishing the plan or creating any card | ASES-CAP-04, ASES-REV-03 |

Real Hermes profiles `lead`/`coder-1`/`reviewer` created fresh (no `--clone-from`, per ASES-ROL-10),
toolsets restricted (reviewer has no terminal/code_execution/browser, kanban enabled for verdicts only),
models pinned per the Phase 2 report. Board `ases-phase3` + project `p_36370687` bound to
`C:\Users\masoo\ases-workspaces\test-repo-phase3` (throwaway, for acceptance test 22.2 only).

## Two more real bugs, same lesson

1. **`hermes kanban show`'s real JSON is nested** (`{"task": {...}, "parents": [...], "children": [...],
   "comments": [...], "events": [...], "runs": [...]}`), not the flat shape `list`/`create` return.
   Every caller (`review.py`, `reconcile.py`, `controller.py`) was written assuming flat access
   (`result["status"]`) -- and every unit test's fake `kanban_show` matched that same wrong
   assumption, so nothing caught it until `reconcile.check` crashed with a real `KeyError` against a
   real card. Fixed by unwrapping inside `hermes.kanban_show` itself, so no caller needed to change.
2. **The dispatcher is already live** (a multiplexed gateway the user runs for other purposes also
   polls this board) and had already tried dispatching T1 twice, failing both times on
   `git worktree add`: the deterministic branch name `swarm/T1-coder` collided with a *stale* worktree
   left over from an earlier, since-archived card on the misconfigured board (archiving a Hermes card
   does not clean up its worktree/branch -- that's `hermes worktree prune`'s job, by design). Cleaned
   up the stale worktree and branches, promoted T1 back to `ready`, then deliberately blocked it again
   pending real credentials so the live dispatcher doesn't burn through its failure-limit budget on
   auth errors before the real test can run.

## A real bug caught by actually running the system (not just unit tests)

`config/swarm.yaml`'s `project.board` was left at its Phase 0 placeholder value (`default`) after the
real `ases-phase3` board and `p_36370687` project were created. Every unit test used a fake/mocked
`hermes` module with an arbitrary board string, so nothing caught it -- the code was "correct" by every
test that existed. Only running `swarm approve` for real against the live board surfaced it: cards were
silently landing on the `default` board instead. Fixed (`board: ases-phase3`), stale rows cleared, and
re-verified: T1/T2 work+merge cards now land correctly, T2 sits in real `todo` status because it's
parented to T1's *merge* card (not work card, per ASES-TSK-02), and running `swarm approve` twice
returns identical card IDs and an identical Gate P commit SHA -- real idempotency, not just plumbing
that compiled. This is why Phase 3 kept alternating unit tests with real, free (no-LLM-call) CLI runs
against the actual board rather than trusting mocks alone for the deterministic layer.

## First real dispatch attempt (2026-09-18): two more real bugs

Credentials landed in `lead`/`coder-1`/`reviewer`'s `.env` files, T1 was unblocked, and `swarm run` was
let loose on the live board for the first time. It did not complete T1, for two genuinely new reasons --
neither hypothetical, both only visible once real requests actually went out:

1. **UnoRouter enforces an undocumented per-model Tokens-Per-Day cap, on top of the known
   `per_model_rpm: 1`.** `qwen3.8-27b:free`'s real error (from
   `hermes/kanban/boards/ases-phase3/logs/t_0e5d71a9.log`): `HTTP 429: Rate limit reached for model
   qwen/qwen3.8-27b in organization org_01kfs2s0g1ebgbrrq9w8yw48hz service tier on_demand on tokens per
   day (TPD): Limit 200000, Used 195327, Requested 22653`. The daily 200k-token bucket was already at
   ~97.7% used before this project's very first real coder-1 request -- i.e. it is shared at the
   `organization` level UnoRouter assigns this key to, not obviously reset per-API-key. Both real dispatch
   attempts (Hermes's own dispatcher, `run_id` 6 and 7 on the card) hit this immediately, exhausted their
   3 local retries in seconds, and the card fell back to `blocked`; `swarm run`'s remaining ~38 passes
   (of a 40-pass, 20-minute bound) correctly did nothing, since dispatch only acts on `ready` cards. Not
   a bug in ASES's own code -- `config/models.yaml` never claimed a daily cap for UnoRouter, so nothing
   here was wrong so much as incomplete. Left `config/models.yaml` with the observed number and left T1
   `blocked` rather than re-unblocking into a wall known to hold for ~2 more hours; ledger.py's budget
   gate has no concept of a *token* cap today (only request counts against a *daily* cap), so this isn't
   actually enforced yet -- tracked, not silently assumed fixed.
2. **`-z`/`--oneshot` grants no toolset by default; `cmd_plan` never passed one.** A real, isolated
   `hermes -p lead -z "..."` call (same recipe as the Phase 2 GLM fix: `reasoning_effort: low` +
   verify-don't-guess) reported its own terminal tool as unavailable -- correctly, not a hallucination:
   `hermes --help` confirms `-t/--toolsets` is required to grant any toolset to a oneshot invocation, and
   `cmd_plan`'s subprocess call never passed it. This meant `swarm plan` as coded could not actually
   inspect a repo or write `docs/ases/plan.json` -- it would either fail or, worse, invent one blind.
   Kanban-dispatched workers (`cmd_run`'s path, and the two real coder-1 attempts above) are unaffected;
   they run under the profile's full configured toolset regardless of oneshot's default. Fixed by adding
   `-t file,terminal` to `cmd_plan`'s subprocess call. Re-verified live: with `-t terminal` added back to
   the same isolated smoke prompt, lead actually called the tool and returned a real, correct answer
   ("Fri, Sep 18, 2026 9:51:54 PM"), confirming lead/GLM's credentials and tool-use recipe both work for
   real through the actual profile, independent of coder-1's TPD wall above. `swarm plan` itself was then
   re-run for real against a fresh scratch repo (not `test-repo-phase3`, to avoid disturbing the T1/T2
   state above) to confirm the fix end-to-end -- see the dated follow-up note below for that result.
3. **The re-run above hit its own real bug**: `cmd_plan`'s 600s subprocess timeout was too short for a
   real multi-turn planning call under UnoRouter's 1 req/min pace, and `TimeoutExpired` was never caught
   -- a bare Python traceback instead of a clean error. Raised to 1800s and handled cleanly, with
   whatever partial output existed printed for diagnosis rather than lost. While in this code, also
   noticed `create_cards_from_plan`/the fix-card path never passed `--max-runtime` to any worker-assigned
   card at all -- `budgets.card_runtime_minutes` was declared in config but wired nowhere, so a genuinely
   stuck worker had no ASES-side cap. Fixed both card-creation sites; unit-tested (default-45m and an
   explicit-value case, plus the merge card and fix card getting/not-getting one correctly).
4. **A capability limit, not a bug**: with the timeout fixed, the retry finished (no crash) but never
   called a real tool. It reasoned correctly about the task, then emitted a literal `<write_file>...
   </write_file>` block as plain response text instead of a real tool call, and `docs/ases/plan.json`
   was never written. This is the SAME `lead` profile, same recipe (`reasoning_effort: low` +
   verify-don't-guess), that had just, minutes earlier, correctly used the real terminal tool on a
   one-line factual prompt ("what does `date` print" -- verified against a real, correct answer). The
   difference is task complexity: a short factual ask vs. a multi-step plan conforming to a real JSON
   schema. `hermes logs --since` for the run shows no credential or tool-availability error at the time
   (`credential pool: no available entries` at 22:07:23 is an unrelated vision-auxiliary auto-detect
   note, not an auth failure -- the same run's earlier real tool call proves the credential itself
   works). This looks like a real ceiling on `glm-5.3-thinking:free`'s tool-call reliability under this
   harness once a task gets structurally complex, not a wrong flag or a missing credential. Flagged to
   the user rather than silently worked around, since GLM's reliability was explicitly called out as
   important earlier in this project.

## Lead moved off GLM, then to OpenAI via xKiro (2026-09-19)

The user's call after the finding above: "if the GLM is not working then lets use GPT API." Changes:

- **`config/models.yaml`**: `glm-5.3-thinking:free`'s row demoted (`role_class: lead_retired`,
  `pinned: false`, real finding kept in a comment, not deleted) so `policy.profile_provider()`'s
  exact-match lookup no longer resolves `lead` to it. New `openai` provider row (`type: openai_compatible`
  -- Hermes has no first-class `"openai"` provider name, it's configured the same way UnoRouter is: a
  named custom endpoint at `https://api.openai.com/v1`, confirmed against Hermes's own
  `cli-config.yaml.example`) and a new `gpt-5.6-terra` model row, `role_class: lead`, `pinned: true`.
- **Model choice**: `gpt-5.6-terra` over the flagship `gpt-6-astra` ($2/$12 per MTok vs $10/$50, per
  OpenAI's own API docs, checked 2026-09-19). Lead's real job here -- a 2-4 task `plan.json` for a
  throwaway test repo -- needs reliable tool-calling, not frontier-tier intelligence; OpenAI's own docs
  describe Terra as the cost-balanced pick for exactly that class of work. Not yet smoke-tested for
  real (needs the user's key) -- `swarm doctor` WARNs/PENDs on this by design until it is.
- **A genuine positive finding while researching this**: OpenAI's own current API data docs
  (`https://developers.openai.com/api/docs/guides/your-data`, checked 2026-09-19) state plainly: "data
  sent to the OpenAI API is not used to train or improve OpenAI models (unless you explicitly opt in to
  share data with us)" -- the default since 2023-03-01. That's `no_training`, one of
  `policy._SAFE_FOR_PRIVATE`'s markers (ASES-PRV-01/02) -- the first provider in this project whose
  declared policy actually qualifies. This does **not** by itself flip `config/swarm.yaml`'s
  `data_class` to `private`: this project still runs coder-1 on UnoRouter and reviewer on OpenRouter for
  the other two roles, and neither of those qualifies, so Gate P would correctly refuse the plan as
  currently composed. Noted here as a real, dated fact for whenever a future project's data-class
  decision actually depends on it -- not acted on beyond that.
- **Hermes profile mechanics**: `lead`'s `config.yaml` `model:`/`providers:` block repointed from
  unorouter/glm to a named custom provider -- structurally identical to how `unorouter` was already set
  up, just a different endpoint. GLM's `agent.reasoning_effort: low` override removed rather than
  carried over: that was an empirically-found GLM-specific workaround (see the Phase 2 report), and
  nothing confirms gpt-5.6-terra needs the same treatment or the same value, so it's left unset
  (provider/model default) until real testing says otherwise.
- **Then the user pivoted to xKiro** after sharing a real screenshot of its live model catalog: "lets
  use xkiro". xKiro is a router (like UnoRouter/OpenRouter, not a single vendor) -- one key, 111+ models
  across 16 providers, OpenAI/Anthropic-compatible, model IDs vendor-prefixed
  (`openai/gpt-5.6-terra`, confirmed against `https://docs.xkiro.com/`). Re-pointed `lead` at
  `https://api.xkiro.com/v1` / `key_env: XKIRO_API_KEY` instead. `config/models.yaml`'s provider row is
  correspondingly `xkiro`, not `openai` -- deliberately given a cautious `data_policy`
  (`router_ztr_upstream_varies`), not `no_training`: xKiro's own privacy policy
  (`https://xkiro.com/privacy`, checked 2026-09-19) states "Zero Data Retention for content: we do NOT
  persist your request or response bodies, nor request headers" -- real and specific, but it only covers
  xKiro's own handling, not what each of the 16 different upstream vendors does once a request reaches
  them, which is exactly the same reasoning gap that keeps UnoRouter off `_SAFE_FOR_PRIVATE`. The
  model row itself carries an informational (not enforced -- `policy.py` only reads the provider-level
  field) note that THIS specific route's ultimate upstream is OpenAI, whose own policy is separately
  confirmed `no_training` -- a real fact, just not one this codebase currently chains together
  automatically, and deliberately not promoted to the provider level since that would silently apply it
  to all 111 other models routed through the same key.
- **A real, price-discrepancy flag, unresolved**: xKiro's own listed price for `openai/gpt-5.6-terra`
  is $1/$6 per MTok; OpenAI's own pricing page lists $2/$12 for the same model name, checked the same
  day. Recorded, not explained -- could be a real xKiro discount, could be one source being stale.
- **Real key added, real smoke test run, one real blocker found**: with `XKIRO_API_KEY` in `lead/.env`,
  a oneshot call to `gpt-5.6-terra` failed with `HTTP 403: This premium model requires an active paid
  plan or real deposited balance. Subscribe to a plan or top up your wallet to use it -- promotional/
  bonus credits do not apply.` The key itself is genuinely good and the wiring is genuinely correct --
  proven by immediately re-running the identical prompt with `-m "qwen/qwen3.6-27b:free"` (same key,
  same profile, a free model on the same router) and getting a real, correct, tool-verified answer. So
  this isn't a config bug: xKiro's account behind this key has no funded balance or subscription, which
  premium (non-free) routed models require regardless of which upstream vendor they belong to. Switching
  `lead` to one of xKiro's free models instead would dodge the funding step but defeats the actual point
  of this whole move -- those free models are the same class of small open model (Qwen/GLM/MiniMax) that
  motivated leaving GLM in the first place, not a GPT-tier model. Flagged to the user rather than worked
  around. `swarm doctor`'s `smoke_test[xkiro/openai/gpt-5.6-terra]` stays `[PEND]` until a funded account
  lets a real dispatch actually complete.
- **A second key, same wallet-funding blocker, different model**: the user created a second xKiro key
  for `qwen/qwen3.8-max` ("i got this from xkiro"). Same real 403 pattern: the PAID tier
  (`qwen/qwen3.8-max`) needs "real deposited balance... billed from your wallet, not covered by a plan";
  the `:free` tier of the exact same model worked immediately (real terminal call, correct answer). So
  this is the same funding gap as gpt-5.6-terra, not a second, different problem -- neither of the two
  keys created so far has money behind it. `lead` is now running on `qwen/qwen3.8-max:free`
  (`role_class: lead`, `pinned: true`); `gpt-5.6-terra` and the paid `qwen3.8-max` are both kept in
  `config/models.yaml` as `role_class: lead_unfunded`, `pinned: false` -- ready to revive the moment
  either wallet actually has a balance, not deleted just because they're unusable today.
- **Answered, for real: yes.** `swarm plan` (the exact command that made GLM hallucinate a
  `<write_file>` tag) run against `qwen/qwen3.8-max:free` produced a real tool call, wrote a real
  `docs/ases/plan.json`, and replied "done" as instructed -- no crash, no fake tool-call text. The plan
  itself is genuinely well-formed, not just present: 2 tasks, correct `coder`/`reviewer` roles, T2
  correctly `depends_on: ["T1"]`, and a gate command (`test -f NOTES.txt && grep -q hello NOTES.txt`)
  that's actually *safer* than the `python -c "..."` one used earlier tonight (no risk of tripping
  Hermes's dangerous-command heuristic). Passed Gate 0 (`plan_mod.load_plan_file`) with no errors.
  Recorded as a real smoke-test pass in `model_registry` (`models.record_smoke_test`). First model in
  this whole project to clear the complex-structured-task bar on its first real attempt.

## A real bug found before it could actually cause damage: cross-project card contamination

With lead finally producing real plans, the obvious next step was to prove the *full* pipeline
(lead plans -> Gate P -> cards -> dispatch -> review -> merge) end to end for the first time, using a
fresh Hermes project (`hermes project create`) bound to the same `ases-phase3` board so it wouldn't
need a new one. Before actually running it, working through what `swarm run` would do exposed a real
correctness gap that would have fired the moment this ran while T1's own retries were still live on the
same board:

`controller.process_budget_gate` and `controller.process_review_lane` each list every "ready"/"review"
card on the *whole board* (`hermes_mod.kanban_list(board, status=...)`), then look up which task that
card belongs to by `work_card_id` alone -- with no check that the card's `project` column matches the
plan actually being processed. A Hermes board is explicitly designed to carry more than one project's
cards (`hermes project create --board <slug>` binds an existing board on purpose), and two unrelated
projects' plans routinely reuse the same generic task keys ("T1", "T2", ...) -- there's nothing stopping
it, and this project's own scratch plans have done exactly that all night. Given a colliding key, the
found row's `task_key` would resolve via `plan.task()` against *this* run's plan, silently substituting
a completely unrelated project's card into the wrong plan's budget check or Gate-1 re-check --
`process_budget_gate` could `kanban_schedule` (park) a card that has nothing to do with the plan being
processed, using another task's budget numbers entirely. `process_merge_queue` was already safe (it
queries `plan_tasks` by `(plan.project, key)` directly, never by listing the whole board), which is
exactly what made the asymmetry visible on inspection.

Fixed by scoping both lookups to `plan.project` (`WHERE work_card_id = ? AND project = ?`), matching
`process_merge_queue`'s existing pattern. Two regression tests added, each first confirmed to fail
against the unfixed code (temporarily reverted, verified `AssertionError: assert ['T1'] == []`, restored)
before trusting them: `test_process_budget_gate_ignores_another_projects_card_with_the_same_task_key`
and its `process_review_lane` counterpart, both building two real `Plan` objects with colliding "T1"
keys under different projects and confirming the second project's card is never touched by the first
project's run. The scratch Hermes project created for the aborted concurrent E2E attempt
(`ases-lead-e2e-test`) was archived, not left dangling, once the plan changed.

## coder-1 moved to xKiro too, and the repo got a GitHub remote (2026-09-19)

Two separate user decisions, same session:

- **"switch coder-1 to xkiro"**, after UnoRouter had spent the whole night being the source of
  essentially every real coder-1 problem (the daily token cap, the output-tokens-per-minute cap, the
  dangerous-command gate block, and recurring "service unavailable" errors that kept sending real T1
  dispatch attempts back to `blocked`) while xKiro had been completely reliable so far. This is a
  different kind of finding than GLM's: UnoRouter/qwen3.8-27b DID produce correct real work at least
  once (a real `hello.py`, correct reasoning about the merge-card dependency graph) -- it's a
  reliability problem, not a capability ceiling, so its row is `role_class: coder_retired`,
  `pinned: false` (demoted, not deleted, same convention as GLM's `lead_retired` row).
  `coder-1`'s Hermes profile repointed at `https://api.xkiro.com/v1` / `key_env: XKIRO_API_KEY`, model
  `qwen/qwen3-coder-plus:free` -- xKiro's own coding-specialized free tier, picked over reusing lead's
  `qwen3.8-max:free` on the theory that a model actually marketed for code generation suits the coder
  role better than a general flagship-reasoning one. Deliberately given its **own** xKiro key, separate
  from lead's, matching the UnoRouter-era per-profile key isolation habit -- not because xKiro is known
  to need it the way UnoRouter's `per_model_rpm: 1` did, just consistency. Not yet smoke-tested for real;
  `swarm doctor`'s `smoke_test[xkiro/qwen/qwen3-coder-plus:free]` stays `[PEND]` until the key lands.
- **"you can push and store in this repo"** -- `https://github.com/masood-ahmed-k/ases` (already
  created, empty, public). Before touching anything remote-facing: scanned the *entire* git history
  (`git log --all -p`, every commit, not just HEAD) and the full working tree for the project's own
  secret-value pattern (`events._SECRET_VALUE_PATTERN`) -- every hit was a synthetic fixture inside
  `test_events.py`/`test_gates.py` (`sk-or-v1-1234567890abcdefghij` and similar, used to test the
  *redactor itself*), never a real key; confirmed no `.env` file has ever existed inside this repo (real
  credentials have only ever lived in Hermes's own profile directories, entirely outside it) and
  `.gitignore` already excludes `.venv/`, `__pycache__/`, `*.db`, and `.env` defensively. Added `origin`,
  pushed the existing local history (through `61cf397`, the last commit made before the git identity
  went missing -- see below) as `master`, which GitHub's empty repo accepted cleanly with no conflicts.
  **This push is incomplete on purpose, not by oversight**: everything from the calendar-time/approval
  gate feature onward (the whole "First real dispatch attempt" era of this file, including today's
  fixes) is still sitting uncommitted locally, because the repo's local git identity (`git config
  user.name`/`user.email`) has been missing since partway through tonight and nothing here writes git
  config on the user's behalf. A second push is needed once the user restores it.

## Coder-1's first real progress, and two more real limits (2026-09-18 into 2026-09-19)

Once coder-1's TPD wall (above) cleared, retrying T1's dispatch several times over the next couple of
hours produced real forward motion, not just repeats of the same failure -- and two more previously
undocumented UnoRouter limits, found the same way as everything else tonight: by actually running it.

- **coder-1 did real, correct work.** One attempt wrote `hello.py` with `print("Hello, world!")` --
  correct against the task's acceptance criteria -- into the real worktree. A later attempt (14 real
  tool calls in one session: `kanban_show`, `read_file` on `plan.json`, `git show --stat`, more)
  reasoned *correctly* about ASES's own task graph on its own: "the child task t_980fcbd3 (merge)... is
  blocked waiting on this task... I should call kanban_complete (not request_review -- because a
  pre-created merge child task depends on my task)". That is the exact ASES-TSK-02 dependency structure,
  inferred correctly by the model from the card body text alone, not hinted at in the prompt.
- **A previously-undocumented Output-Tokens-Per-Minute (OTPM) cap.** A real 429 named it exactly:
  `Request too large for model qwen/qwen3.8-27b ... on output tokens per minute (OTPM): Limit 1000,
  Requested 2048`. Unlike the RPM/TPD limits, this one isn't timing-dependent -- Hermes's default
  max_tokens for this model (2048) permanently exceeds the 1000 ceiling, so it fails on *every* request
  that actually needs the full budget, not just under load. Checked Hermes's own source
  (`agent_init.py`'s per-model `custom provider` config handling, alongside how it already reads
  `context_length` the same way) and confirmed `providers.<name>.models."<model>".max_tokens` is a
  schema-recognized per-model override (`hermes config set` warns on an unrecognized key and did NOT
  warn on this path, unlike a first guess at `agent.max_tokens` which it correctly rejected). Set to 900
  in `coder-1`'s `config.yaml` (comfortably under 1000). Caveat: this was confirmed against the CLI's own
  schema validator, not by reading the exact runtime call site that applies it, given the size of
  Hermes's source tree -- re-verify against the next real dispatch's actual `Requested N` figure if this
  error recurs.
- **Hermes's terminal tool blocks the test plan's own gate command.** `python -c "print('gate ok')"`
  (this test plan's throwaway `trivial` gate profile) was refused by coder-1's terminal tool as
  "flagged as dangerous (script...)" -- a real, working coder blocked from running its own assigned
  gate check by Hermes's own safety heuristic on inline `python -c`. Not an ASES bug (ASES's own
  `gates.py` runs these commands directly via subprocess, never through an agent's terminal tool, so
  Gate 1/3 are unaffected) but a real gotcha for any plan that hands a gate command to a *worker* to
  self-check before completing. Changed the test plan's `trivial` profile to `echo gate ok`.
- **The TPD figure looks pooled and fluctuating, not a private monotonic counter.** Three separate 429s
  named different `Used` values at different times that don't line up with a single account steadily
  climbing to one fixed reset (195327, then -- after supposedly resetting -- 191667, then 195446):
  consistent with `qwen/qwen3.8-27b`'s free-tier TPD bucket being shared across UnoRouter's free users of
  this exact model in something closer to real time, not reserved per-key. Worth knowing before reading
  too much into any single "retry in Nh" figure as a private, predictable schedule.

Net effect: three genuine fixes applied (max_tokens, gate command, plus the earlier toolset/timeout/
max_runtime fixes), and the real dispatch retried again with all of them in place. See this file's
next dated entry (added once that run's outcome is known) for whether T1 actually reached `done`.

## A real finding, not a hypothetical

`policy.check_data_class` enforces section 21.2 for real: **neither UnoRouter nor OpenRouter's
currently declared data policy qualifies as safe for `data_class: private`** (both say upstream
providers or some endpoints may train on inputs). Gate P will correctly *refuse* to approve any plan
under `private` until a provider with a confirmed no-training/local policy is added. This project's own
`config/swarm.yaml` is set to `public` for exactly this reason -- it's throwaway test scaffold content,
not real code, so that's an honest, deliberate choice, not a workaround. A real future project MUST set
its own data class deliberately and will hit this same refusal under `private` until that gap is closed
(most likely by adding a provider with `data_collection: deny` + `zdr: true` verified, or a local model).

**Update, 2026-09-19**: that gap is now closed *for one role*. `openai` (added for `lead`, see the dated
section below) declares `data_policy: no_training`, sourced from OpenAI's own current docs -- it does
qualify. `config/swarm.yaml`'s `data_class` is still `public`, deliberately: this project's other two
roles (coder-1 on UnoRouter, reviewer on OpenRouter) still don't qualify, and `data_class` is a single
project-wide setting, not per-role, so flipping it now would just make Gate P refuse those two roles'
cards. A real future project that's actually private AND wants every role on a qualifying provider would
need all three roles on something like OpenAI (or another verified no-training/local provider), not just
one -- per-role data classes aren't a thing this architecture supports today.

## Known gaps (tracked, not hidden)

- `glm-5.3-thinking:free`'s context length is **not declared** in `config/models.yaml` -- native
  Hermes `config.yaml` doesn't set `model.context_length` either, so it's presumably auto-probed but
  never confirmed. `swarm doctor` WARNs on this by design. Confirm and set the real number before
  pinning this model in Phase 2.
- The blueprint's Appendix B illustrative config (v1.2) names the UnoRouter Hermes secret as
  `OPENAI_API_KEY`. The **actual** installed `config.yaml` on this machine says
  `key_env: HERMES_CUSTOM_UNOROUTER_API_KEY` -- that's what `config/models.yaml` uses here. Appendix
  B is explicitly illustrative ("map this to the installed Hermes configuration format instead of
  blindly pasting it"), so this isn't a bug in the blueprint, just a reminder that the illustrative
  name and the real one differ on this install. Since 2026-09-19, `OPENAI_API_KEY` is also the *real*
  env var for the actual `openai` provider (`lead`'s new home) -- a naming coincidence with Appendix B's
  illustrative UnoRouter name, not the same key or the same purpose. Don't confuse the two when reading
  older notes in this file.
- ~~No Hermes profiles exist yet~~ -- stale, fixed 2026-09-18: `lead`/`coder-1`/`reviewer` were created
  fresh (no `--clone-from`, ASES-ROL-10, `covered`) earlier in Phase 3; this bullet just never got
  removed when that happened. Left struck through instead of silently deleted so the drift is visible.
- ~~Gate P plan publication (ASES-ARC-09)... new v1.2 requirements with no code yet~~ -- also stale:
  `controller.publish_plan` exists and is wired into `cmd_approve` (ASES-ARC-09, `covered`).
  Pinned-worktree creation (ASES-GIT-16) is the one still genuinely open -- see below.
- **ASES-GIT-16 (`not_covered`)**: "ASES worktrees start from the exact local integration HEAD;
  worktree_sync is disabled or manual creation is used." `gates.py`/`mergeq.py`'s own throwaway
  worktrees already satisfy this (detached at an exact SHA). What's unverified is the WORK card's
  worktree, which Hermes itself creates on dispatch (`workspace: worktree` in `kanban_create`) -- whether
  Hermes's own `worktree_sync` project setting needs to be explicitly turned off requires checking real
  Hermes project/profile flags, not just ASES code. Deliberately not touched while T1's real dispatch
  (below) is using that exact worktree; revisit after this run concludes.
- The UnoRouter (`ases-reviewer`/GLM/Qwen keys, one each, GLM and Qwen scoped per-model) and OpenRouter
  credentials landed in the `lead`/`coder-1`/`reviewer` profiles' `.env` files on 2026-09-18. T1 was
  unblocked and `swarm run` is executing the real end-to-end loop (acceptance test 22.2) against the
  live `ases-phase3` board for the first time -- outcome not yet known as of this note; whatever it
  finds gets its own dated entry below, in this file's established style, once it lands.
- Gate 1/3 run directly on the host, not inside Docker (Phase 5 requirement, not built). Documented in
  `gates.py`'s own docstring so this isn't quietly assumed to be sandboxed.

## Running things

```
cd C:\Users\masoo\ases
.venv\Scripts\swarm.exe doctor
.venv\Scripts\swarm.exe models
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe spec\check_requirements.py --check
```
