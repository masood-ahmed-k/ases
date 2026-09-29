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
| `gates.hash_gate_profiles` / `controller.pin_gate_profiles` / `verify_gate_pin` | pins a project's approved gate commands by content hash at `swarm approve`; `swarm run` refuses if they've since changed without a fresh approve | ASES-QG-02 (partial) |
| `controller.process_merge_queue` (fix-card lifecycle) | on a conflict or red Gate 3, opens a fix card and repoints `plan_tasks.work_card_id` at it (that column now means the task's *current* card: the original, or the latest fix card); a benign fast-forward race records `merge_race_retrying` and retries free; `review.gate_before_review` now takes `integration_branch` as a required parameter | ASES-GIT-09 (partial), ASES-GIT-13 |

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
  to need it the way UnoRouter's `per_model_rpm: 1` did, just consistency. (Smoke-tested for real the same
  day once that key landed: a real terminal tool call on the real profile, recorded as a pass in the
  registry. See "coder-1's first run on xKiro, and a MiniMax comparison" below for what it then did.)
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

## A multi-agent pass at the remaining register gaps (2026-09-19)

Git identity got fixed (the user ran the commands directly), so the backlog above was committed and
pushed for real (`6e4758f`). Separately, the user asked whether multi-agent orchestration would finish
"this project" faster; clarified to mean a genuinely working v1 of the whole pipeline, not a specific
document. Since the actual remaining blocker (coder-1's key) can't be sped up by more agents -- it's an
external, sequential dependency -- the agents were pointed at two things that COULD parallelize: the
`not_covered` requirement backlog, and a pre-flight adversarial review of pipeline code that has only
ever been exercised by mocked tests (see the next section).

Six parallel agents each read their actual blueprint section (not just the one-line requirements.yaml
summary) plus the current code, and reported honestly on what's really buildable:

- **ASES-CAP-06** (provider diversity): mostly already satisfied by tonight's real xKiro/UnoRouter work
  -- register moved from `not_covered` to `in_progress`, with the one real residual gap (same-account
  key-quota-sharing has no code check, needs a live provider API call) written into the note.
- **ASES-REV-02** (plan bounces to Lead at most twice): genuinely blocked on ASES-REV-01, the
  plan-critique mechanism itself, which doesn't exist as a call site or a data model yet. Real finding
  along the way: "Gate P" as the blueprint defines it (critique -> privacy check -> budget check ->
  approval -> publish) is 4 of 5 steps built under that name today; grepping "Gate P" in this codebase
  makes it look done, and it isn't quite. Left `not_covered`, note filled in with the real reason.
- **ASES-QG-02** (gate commands pinned by hash): genuinely buildable, and built (see the module map).
- **ASES-TST-02** (acceptance tests cost no quota): produced the most consequential finding of the
  batch -- the blueprint's own section 22.0 says tests 22.2 through 22.16 are specified to run against a
  *fake* provider; only 22.1, the Phase 2 evaluation, and 22.17 are meant to touch a real one. Tonight's
  real end-to-end dispatch against real coder-1/lead credentials has been standing in for a fake-provider
  harness that doesn't exist yet, not fulfilling the design as specified. Not unwound -- the real run is
  still genuinely valuable, proving things a fake never could -- but worth knowing precisely what it is
  and isn't. Also surfaced that `gates.detect_tamper` (ASES-QG-03) is written and unit-tested but has
  zero callers anywhere in the real pipeline, unlike `scan_for_secrets` which is wired into
  `mergeq.merge_task`; and that the secret-scan requirement row (section 8.1) is marked `not_covered`
  despite being directly tested (`test_gates.py::test_scan_for_secrets`,
  `test_mergeq.py::test_merge_blocks_on_a_planted_secret`) -- a stale status, not an ID-drift problem.
  Register left `not_covered` (the full 16-test closure is a substantial harness-building effort, not a
  small task); the coverage mapping and these two findings are recorded here rather than silently fixed.
- **ASES-DOC-04** ("the stop condition is honored"): resolved an ambiguity the task itself flagged --
  this is section 16's STOP CONDITION paragraph, a rule for whoever builds/operates ASES (originally
  Claude Code), not runtime software. No artifact stating it existed anywhere in this repo. Added
  `C:\Users\masoo\ases\CLAUDE.md` restating it verbatim so a future session without this conversation's
  memory still loads and honors it; register moved to `partial` (real but scattered evidence exists for
  2 of the rule's 6 categories, not a durable artifact until now).
- **ASES-GIT-16** (worktree pinning): declined to guess, correctly. This needs a real Hermes CLI/config
  investigation that was already deliberately deferred earlier tonight to avoid disturbing T1's live
  dispatch -- the agent independently reached the same conclusion already recorded in this file's
  "Known gaps" section and did not re-investigate past that point.

ASES-QG-02 was then built by a single dispatched agent, faithfully executing the investigation agent's
own detailed spec: `gates.hash_gate_profiles()`, a new `gate_pins` SQLite table (schema version 2 -> 3),
`controller.pin_gate_profiles()`/`verify_gate_pin()`, and the two call sites in `cli.py` (`cmd_approve`
pins right after a plan clears every refusal gate; `cmd_run` refuses with `REFUSED (ASES-QG-02)` if the
pin no longer matches). 9 new tests. The build agent reported 2 pre-existing test failures
(`test_run_gate_pass`, `test_run_gate_records_to_db`, a `'python' not recognized` error) as unrelated to
its change -- re-verified directly afterward in the main session's own shell: 147/147 pass cleanly.
That's a PATH difference in the agent's own sandboxed subprocess environment, not a real regression in
this codebase; recorded here so it isn't mistaken for one later.

## Pre-flight adversarial review of the never-yet-live pipeline (2026-09-19)

Lead works, but coder-1 is still waiting on its own key, so `review.py`, `process_review_lane`,
`mergeq.py` and `process_merge_queue` had only ever run under mocks and scripted git repos -- never
against a real Hermes-dispatched worker. Four review agents (merge queue and gates; review lane;
controller orchestration and idempotency; reconcile and integrity wiring) read the real code hunting for
the class of bug a fake can't show, and every finding then went to an independent verifier told to
refute it and to check for an existing test. **25 survived as real and untested** (24 distinct: two
agents independently found the hardcoded `"integration"` literal): 11 high, 11 medium, 3 low.

### Fixed the same day (7 findings, 6 distinct)

One build agent, then a line-by-line diff review and a full-suite run in the main session (160 passing).
The agent re-introduced each original bug into the source and confirmed the new tests go red, then
restored the source byte-for-byte:

- **The fix-card lifecycle was broken (3 findings).** A fix card was created and then forgotten:
  `process_merge_queue` kept re-deriving the original, still-broken branch from `plan_tasks.work_card_id`,
  so it never saw the fix card's output; it re-merged that broken branch on every poll while the fix card
  was still running (burning the fix budget before the fix could even start); and `process_review_lane`
  could never find the fix card (its id was never in `plan_tasks`), so Gate 1 never policed its diff.
  Fixed with one change: creating a fix card also repoints `plan_tasks.work_card_id` at it, so "the
  task's work card" now always means its *current* card. The next pass waits for the fix card to reach
  done, merges the fix branch, and the review and budget lanes can see it. Fix cards are still parented to
  the card they replace (fix2 to fix1 to the original).
- **Fix cards were created under the wrong Hermes project.** `project=project.name` passes
  `config/swarm.yaml`'s own ASES-internal label ("ases"), not a Hermes project id. Now read off the card
  being fixed (`work_card["project_id"]`).
- **A benign fast-forward race was treated as a real failure.** `mergeq.merge_task` already returned a
  distinct outcome (Gate 3 passed, fast-forward refused, integration moved) but the controller only ever
  branched on `outcome.merged`, so a race that needed a free retry cost a real coder turn and a unit of
  fix budget. Now records `merge_race_retrying` and retries next pass.
- **`review.py` hardcoded the branch name `"integration"`** in its merge-base call (found independently by
  two review agents). It only worked because `config/swarm.yaml` happens to spell it that way; under any
  other name the lookup failed and the touches check silently degraded to inspecting only the branch's
  last commit. `gate_before_review` now takes `integration_branch` as a required parameter (deliberately
  not defaulted to `"integration"`, which would just re-mask the same bug), tested against a repo whose
  branch is named `main-line`.

### Two decisions the build agent surfaced (implemented afterwards, same day, on the user's "go")

1. **The race branch could hide a persistent failure -- fixed.** `merge_task` returned the identical
   outcome for any `git merge --ff-only` refusal, not just a real race: a dirty primary checkout produced
   exactly that outcome although the integration tip never moved, and it retried silently every poll,
   bounded only by `--max-iterations`. Now `MergeOutcome` carries `integration_moved`, decided from git:
   after a refused fast-forward, `merge_task` compares the candidate's parent (the tip it was squashed
   onto) with the integration tip, and only a tip that verifiably moved earns the free retry. Both
   lookups use `rev-parse --verify -q` plus an exit-code check, because a bare `git rev-parse <bad-rev>`
   echoes its argument on stdout while failing; if either lookup fails the answer is "not moved". A
   refusal with the tip unmoved takes the ordinary failure path (`merge_failed` event with git's own text
   and a "did NOT move" explanation, a fix card, then a block). Adjacent and included: `merge_task` now
   refuses up front, before any worktree exists, to run when the primary checkout isn't on
   `integration_branch` (or is detached), because `--ff-only` advances whatever branch is checked out and
   from another branch whose tip happened to equal integration's it would have reported `merged=True`
   while integration never advanced. Known trade-offs: a dirty-checkout refusal still opens a fix card
   that no coder can fix (the dirty tree is the operator's), which is bounded by `fix_cards_per_task` and
   surfaces at the block, costing a coder turn or two first; and `symbolic-ref --short` prints
   `heads/integration` if a tag is named like the branch, so that pathological case fails closed with a
   confusing message rather than merging. Tested with real git (a genuine race, a genuinely dirty
   checkout, a wrong branch and a detached HEAD); each bug was re-introduced in the main session to
   confirm the tests go red.
2. **A re-approve reset the repoint -- fixed.** Re-running `create_cards_from_plan` upserted
   `work_card_id` back to the original card while `fix_cards` kept its count. The upsert is now
   `work_card_id = CASE WHEN plan_tasks.fix_cards > 0 THEN plan_tasks.work_card_id ELSE
   excluded.work_card_id END`, so a re-approve (also how gate configuration changes, ASES-QG-02) can't
   undo a live fix card. Cosmetic leftovers: the `swarm approve` printout and the `cards_created` event
   still show the original card id for such a task while the database holds the fix card. Same
   bug-reintroduction check as above.

### Behaviour changes from the repoint worth knowing

The review lane now holds fix cards to the *original* task's `touches` globs (a fix that must edit an
out-of-scope file bounces until the plan is widened); the budget gate now parks fix cards, which it
couldn't see before; a fix card stuck in a non-done state stalls its task silently like any regular work
card (before, it looped into budget exhaustion and then blocked for a human); both
`branch_name or f"swarm/{key}-{role}"` fallbacks only know the original naming, so a fix card that ever
lacked `branch_name` would send the queue back to the original broken branch; `plan_tasks` no longer
records the original work card id once a fix exists (it survives in the `cards_created` event and as the
fix card's Hermes parent); and if `integration_branch` doesn't resolve, `review.py` still falls back to
the last-commit-only check -- now reachable only through misconfiguration. New event kind:
`merge_race_retrying`.

### Still open (18 findings), by theme

- **Built and unit-tested, but never wired into the real pipeline.** `ledger.record_usage()` has no
  production caller, so the budget gate can never see real usage (ASES-CAP-03 downgraded to `partial`);
  `gates.detect_tamper` (ASES-QG-03) is never called; `integrity.snapshot`/`diff_snapshots` (ASES-GIT-12)
  are never called; `mergeq.revert_merge` (ASES-GIT-05, downgraded to `partial`) is never called.
- **Cross-project isolation, the same class as the `process_budget_gate`/`process_review_lane` fix but in
  other places.** `merge_records` and `gate_runs` are keyed on `task_key` alone with no project column
  (`mergeq` writes them, `reconcile` reads them), and `run_pass`'s `kanban_dispatch(board)` is board-wide
  with no project filter, so it can dispatch a sibling project's card.
- **Crash and robustness.** An unprotected window between the fast-forward landing in git and
  `kanban_complete` running: interrupted there, the branch has the commit but the Hermes card never
  reads done, `reconcile.check` has no check for git-ahead-of-board, and the next pass treats the
  already-merged task as a failure and opens a spurious fix card. No `TimeoutExpired` handling on
  `mergeq`'s git calls, and no timeout at all on `review.py`'s. No exception handling around
  `kanban_show`/`plan.task` in `process_review_lane`, `process_merge_queue` and `all_merge_cards_done`, so
  one bad card kills the whole polling loop. Worktree teardown ignores `git worktree remove`'s exit code
  and then force-deletes the directory, leaving orphaned registrations nothing prunes. (The old
  "nothing verifies the primary checkout is on `integration_branch`" gap is closed, see above.) The
  `fix_cards` read/create/increment sequence isn't atomic across two `swarm run` processes.
- **Minor.** Any nonzero `git merge --squash` is labelled "merge conflict:"; a re-approve leaves
  role/touches/gate_profile/estimated_requests stale (the upsert only refreshes card ids); Hermes's own
  live dispatcher can spawn the reviewer the moment a card enters `review`, before ASES's Gate-1 re-check,
  which contradicts `review.py`'s own docstring.

Method notes: an independent nemotron cross-check was attempted by several agents and returned 403 every
time (the known key problem), so every conclusion here rests on direct reading of source, tests, and
throwaway probes rather than a second model's opinion.

## coder-1's first run on xKiro, and a MiniMax comparison (2026-09-19)

**What the first xKiro run actually did (and a correction).** With coder-1's own xKiro key in place, the
smoke test passed (a real terminal tool call on the real profile) and T1 was unblocked. The new run
(run 20) was claimed and spawned at 07:42:47 and called `kanban_complete` at 07:43:51. It did not author
the work. `hello.py` was already committed on the task branch as `c23b684`, authored at 00:00:50 during
the earlier UnoRouter-era attempt (T1 had `gave_up` at 02:02:57 and runs 18 and 19 ended `crashed`). The
xKiro run found that commit, ran `python hello.py` (output `Hello, world!`, exit 0) and completed the card.
An earlier status message in this build described that as coder-1 having "written and committed"
`hello.py`; that was wrong, checked against the commit's author time and the card's event log, and is
corrected here and in `config/models.yaml`.

What it does show: `xkiro/qwen/qwen3-coder-plus:free` drives Hermes's kanban tools (`kanban_show`,
`kanban_complete`) and terminal tool correctly on a real card, on a key that had never run a real
dispatch before. What it does not show: this model writing and committing new work on a real card. That
is still untested on a real card, and there is a latent reason it might go wrong: T1's card body never
tells the worker to commit its changes, so the run only succeeded because a commit already existed. Watch
for that on the first card that starts from an empty branch.

**MiniMax, suggested by the user ("minimax could be better option when it comes to coding").** The user's
xKiro dashboard capture lists `minimax/minimax-m3:free` (1M context) and `minimax/minimax-m2.5:free` (204K).
No new key was needed: xKiro keys are not model-scoped (one key has served several different models
tonight). One identical real task was run once per model through coder-1's profile with `-m`: write a
`slugify()` function to a written spec and verify it with the terminal tool, then graded afterwards by a
hidden 10-case check the models never saw.

| model | hidden check | wall time |
| ------ | ------ | ------ |
| `qwen/qwen3-coder-plus:free` | 10/10 | 142 s |
| `minimax/minimax-m3:free` | 10/10 | 133 s |
| `minimax/minimax-m2.5:free` | 10/10 | 194 s |

Also noted: m2.5, asked to reply with just "done", answered with a summary instead. Verdict: MiniMax works
in this harness (real tool calls, correct code), and nothing here separates the three. One run of an easy
task cannot rank models, and this file does not claim MiniMax is better or worse. Both MiniMax models are
recorded in `config/models.yaml` as `role_class: coder_candidate`, `pinned: false`, and no role was
switched. Compare them on something harder (a fix card, a multi-file task) before changing a role.

## The review lane, checked against real Hermes before its first real run (2026-09-19)

The review lane had never run for real: the one real coder run so far (T1, above) finished its card directly
and never went through review. Before spending a real run on it, its Hermes semantics were read from the
installed source (v0.21.3) and probed on a scratch board (created, used and archived the same day). Every
unit test around it mocked the Hermes wrapper, so none of the following could have been seen by them.

- **The controller's send-back crashed the polling loop.** `review.gate_before_review` returned a card to its
  implementer with `hermes kanban request-changes`. That command is the REVIEWER's verdict and Hermes only
  accepts it on a card claimed in an active review run. On a card that merely sits in `review`, which is where
  `process_review_lane` finds cards, it prints "task is not in an active review run" and exits 1 (probed), which
  `hermes.py` raises as `HermesCommandError` straight out of `run_pass` and out of `swarm run`. The correct
  controller-side command is `reopen-review --reason`, which moves the card back to `ready`, restores the
  implementer and records the reason as a "CHANGES REQUESTED" comment (probed). Fixed:
  `hermes.kanban_reopen_review`, used at all three send-back sites, with a test that fails if
  `request-changes` is ever called from there again.
- **Hermes has no default reviewer.** `kanban_request_review` only reassigns the card when `reviewer=` is
  passed; otherwise it stays assigned to the implementer, and the dispatcher would spawn coder-1 to review its
  own work. The coder's persona file said "request review" without saying who. Work-card and fix-card bodies
  now tell the coder to hand off with `reviewer="<reviewer profile from the roles map>"`, to commit first, and
  not to call `kanban_complete`. (Only cards created from now on carry this: card creation is idempotent by
  key, so a re-approve hands back the original card with its original body.)
- **A coder can bypass review, and did.** On the first real run coder-1 called `kanban_complete` on T1 itself
  (a one-line script is what Hermes's own worker guidance calls "genuinely terminal"), and the merge queue
  treated any `done` card as approved. ASES-GIT-03 was marked `covered` on the strength of the Gate 1 re-check
  alone; the half that says "a reviewer PASS" was never enforced. The merge queue now requires the work card's
  latest completed run to belong to the reviewer profile and otherwise refuses to merge it, records a
  `merge_refused_unreviewed` event once per card, and shows it on the `swarm run` pass line. The register row is
  `partial`: the profile is checked, the binding of the verdict to the exact commit SHA (ASES-REV-06) is not.
- **The pass ran dispatch before policing the review lane.** `run_pass` called `kanban_dispatch` first, and
  that call also claims cards in `review` and spawns their reviewer, so the Gate 1 re-check (which only acts on
  cards still in `review`) was skipped for every card the same pass's dispatch got to first. The blueprint's
  loop runs the Gate 1 re-check first. Reordered. Residual race, not closed: Hermes's own gateway dispatcher
  (60 s tick) can still claim a review card between two ASES passes; Gate 3 at merge time is the backstop.
  Note the scope (touches) check lives only in the review-lane path, so a card that skips that path also skips
  it (ASES-GIT-13 stays `partial`).
- **Smaller findings from testing the send-back path.** `review.py` decided a branch did not exist by looking
  for empty output from `git rev-parse <branch>`, but git echoes the bad name on stdout while failing, so that
  guard was dead code (the same trap `mergeq._resolve` had already been fixed for). And when no merge-base
  could be computed the touches check silently fell back to inspecting only the last commit; it now fails
  closed. `mergeq` labelled any failed commit "nothing to commit", including one refused for a bad identity or
  a hook after changes were staged; it now says "commit failed". `events.record` redacted `task_key` in every
  event, because the credential pattern matches any field name containing "key", so the events table held
  `"task_key": "[redacted]"` for every merge, failure and fix card; the exact names `task_key` and
  `idempotency_key` are now exempt (a value that looks like a secret is still scrubbed).
- **`swarm run` died on the first failed pass.** An exception out of `run_pass` (a Hermes CLI timeout, a locked
  database) ended the whole run with a traceback. A failed pass is now recorded (`pass_error` event), reported
  and retried on the next poll, and the run stops with exit code 2 only after five failed passes in a row.
- **Review-only tasks and commit provenance.** A plan task with role `reviewer` (which the Lead is told it may
  write, and which Hermes's own review skill calls "ordinary implementation work with a review-oriented
  specification") leaves an empty diff: `git merge --squash` exits 0 with nothing staged and the commit then
  fails, which would have opened spurious fix cards. `merge_task(..., allow_empty=True)` now records that as a
  no-op merge (a `merge_records` row with `gate3_result` "skipped" and no squash commit; no secret scan, no
  Gate 3, the integration branch untouched) and the merge card completes with result "no changes to merge
  (review-only task)". A coder's empty branch is still a failure. Squash commit messages now carry the work
  card and merge card ids (ASES-GIT-06 says "with the card ID"; the register had it `covered` without that).
- **Not fixed, noted by the build agent.** A missing work branch is reported as "merge conflict: ... not
  something we can merge", and for a reviewer task whose branch was never created that still opens a fix card.
  Fix-card bodies carry the hand-off steps but no "Touches:" or "Gate profile:" line, which those steps refer to.
  A no-op merge counts in `run_pass`'s `merged` list. For a no-op the returned outcome has `candidate_sha` None
  while the `merge_records` row holds the integration tip.

## The first real end-to-end run: lead, coder, reviewer, merge (2026-09-19)

Project `greet-e2e` in the throwaway repo, board `ases-phase3`, run through the controller as it stood at
commit 4643c3a. Times are the machine's local time (UTC+2). Nothing below was mocked: real Hermes, real git,
real providers.

| time | what happened |
| ------ | ------ |
| earlier | `swarm plan`: the Lead (xKiro `qwen/qwen3.8-max:free`) wrote `docs/ases/plan.json` from a two-task request. It was valid on the first try and was not edited: project `greet-e2e`, G1 (coder, touches `greet.py` and `test_greet.py`), G2 (reviewer, depends on G1, no touches), one gate profile `tests` = `python -m pytest -q` |
| 16:51:56 | `swarm approve --yes`: Gate 0 passed, budgets fine, plan published to `integration` at 232e12e (ASES-ARC-09), gate profile pinned (ASES-QG-02), four cards created (G1 work `t_4fc6dc34` and merge `t_515c19fc`, G2 work `t_f62b1534` and merge `t_467fdd18`); the work-card body carried the new hand-off steps |
| 16:52 | `swarm run` pass 1 dispatched G1 to coder-1 (xKiro `qwen/qwen3-coder-plus:free`). Its worktree was cut at 232e12e, exactly the integration tip |
| 16:53:45 | coder-1 committed `12995fc` (`greet.py`, `test_greet.py`, both inside its touches), ran the gate itself (2 passed) and handed off with `kanban_request_review` naming `reviewer="reviewer"` and metadata: changed_files, verification_commands, gate_result, commit_sha, branch, residual_risk. 2 m 49 s, 21 tool calls |
| 16:54:11 | the controller re-ran Gate 1 on 12995fc in a clean worktree: pass (`gate_runs` id 1). The reviewer worker was spawned one second later |
| 16:54 to 16:57:47 | the reviewer (OpenRouter `cohere/north-mini-code:free`) read both files, tried to run the tests, could not, and approved with `kanban_complete` (`review_outcome: approved`). 5 API calls, 92,785 input tokens, 1,585 output tokens. Most of the wall time was Hermes starting up (about two minutes before its first model call), not reviewing |
| 16:57:56 | merge queue: work card completed by the `reviewer` profile, so eligible; squash candidate `676628f` built on the tip (message names both card ids), secret scan clean, Gate 3 pass (`gate_runs` id 2), fast-forward, `merge_records` completed, merge card completed |
| 16:58:28 | G2, unlocked by G1's merge card, went to the reviewer: 13 s, verdict PASS with structured metadata. Its branch `swarm/G2-reviewer` was cut at 676628f, the tip after G1's merge |
| 16:58:58 | G2's merge: an empty diff, recorded as a no-op (`gate3_result` "skipped", no squash commit); merge card completed "no changes to merge (review-only task)" |
| 16:59:05 | `swarm run`: "all merge cards done", exit 0, 15 passes, about seven minutes after approve. `reconcile.check`: no findings. Integration branch: exactly one new commit for the code task |

What the run showed beyond "it works":

- **The new hand-off text was followed to the letter**, and the fix that mattered most (naming the reviewer) is
  why the card reached a different profile at all. The coder also handled Hermes's post-hand-off "protocol
  violation" nudge correctly: it worked out from the run history that its run had ended and the reviewer's run was
  live, and declined to complete its own card.
- **The reviewer really is command-less, but not write-less.** Its `terminal` call failed with "Tool 'terminal'
  does not exist", which confirms the command-execution half of ASES-ROL-05. The tool list it was given also
  contains `write_file` and `patch`: Hermes toolsets are per group (`file` is reads and writes together) and this
  version has no per-tool deny, so "no product-file write" is not enforced by profile configuration. ROL-05 is
  now `partial`; the real fix is the Phase 5 sandbox with a read-only mount.
- **The reviewer vouched for something it could not check.** It listed `verified_gate_tests_passed` among its
  checks right after its own attempt to run the tests failed; it was relying on the coder's hand-off claim. It has
  no way to see the controller's gate records (they live in the ASES database, not on the card). Open item: post
  the controller's Gate 1 result to the card as a comment so a reviewer can cite evidence the controller produced.
- **The coder changed the machine outside its worktree.** `pytest` was missing from Hermes's own virtualenv, so
  it ran `pip install pytest` there (pytest 9.1.1 is now in `hermes-agent/venv`). It reported this honestly in
  `residual_risk`. Nothing in ASES would have noticed otherwise: the integrity snapshots (ASES-GIT-12) are still
  not wired into worker runs and the Docker sandbox (ASES-SEC-03) is Phase 5. It also means the worker's `python`
  and the gate's `python` (system Python 3.11) are different interpreters that both happened to pass.
- **Evidence for ASES-GIT-16:** the work-card worktree base was the exact integration tip both times. This repo
  has no remote, so the remote-sync behaviour the requirement guards against was not exercised; the register row
  is `partial`, not `covered`.
- **The `task_key` fix is visible in the database:** the run's events carry `"task_key": "G1"`, while the
  events from before the fix still read `"[redacted]"`.

Not exercised in a real run, so still unproven end to end: the send-back path (a red Gate 1 or an out-of-scope
diff), merge conflicts and fix cards, a red Gate 3, the reviewer requesting changes or blocking, the merge queue
refusing an unreviewed card (unit- and mutation-tested only; coder-1 followed its instructions), the race
between Hermes's gateway dispatcher and the Gate 1 re-check (this time ASES's own pass got there first), budget
parking, more than one task in flight, crash and resume, the kill switch, questions, and Gates 4 and 5. An
independent nemotron review of the approval logic was attempted and returned 403 (the known key problem), so
the new code rests on direct reading, 24 seeded-bug mutation checks (every one caught) and this run.

## Side by side: the swarm against one Sonnet agent on the same small task (2026-09-19)

Same request, word for word, to both (`benchmarks/allocate/`, with its README): a money-splitting function with
subtle rules, graded by a hidden 91-test suite that both arms never saw, plus 15 seeded bugs to measure how
strong each arm's own tests are. The grader was validated first (the reference passes 91 of 91, every seeded
bug is caught by the hidden suite). The swarm arm ran through the real pipeline with the Lead's plan unedited
(commit 179e1c1 on `integration`); the agent arm worked in a fresh repository with the same starting tree.

| | swarm (free models) | one Sonnet agent |
| ------ | ------ | ------ |
| hidden suite | 91 of 91 | 91 of 91 |
| own tests | 26 | 150 |
| seeded bugs caught by its own tests | 12 of 15 | 15 of 15 |
| wall clock | 9 min 36 s, plan to merge | 11 min 32 s |
| cost | free tiers | about 155,600 tokens |
| independent review | yes | no |

What it says, and what it does not:

- **Correctness was a tie at the ceiling.** Both solutions matched the spec on every hidden test, so this task
  cannot tell them apart on correctness. The Lead's plan kept every rule of the request (it dropped only the
  worked example and the formulas) and the free coder implemented them exactly.
- **The gap is in how hard each arm tried to break its own work.** The agent wrote 150 tests, including an
  independent exact oracle and exhaustive small-input sweeps, and ran its own mutation check; the swarm's coder
  wrote 26 example-style tests that miss three of the fifteen seeded bugs (a negative weight that is not
  rejected, ties broken by weight instead of by index, leftover cents handed out in index order).
- **The reviewer did not close that gap.** It approved, listing "all tests pass" although it has no command
  tool: that was the coder's claim repeated, and the coder's claim was slightly wrong (28 tests reported, 26
  present; the agent's own count was off too, 152 reported against 150). It also did not notice the thin tests.
  This is the same weakness as in the first real run and the clearest place to improve the swarm: give the
  reviewer the controller's gate evidence, and have the controller measure test strength itself (a mutation
  gate is what ASES-QG-03's tamper and weak-test intent points at).
- **A hard review can eat the reviewer's free quota.** This review took 37 API calls and 933,000 input tokens
  on OpenRouter's free tier, which allows about 50 requests a day. The usage ledger is not wired
  (ASES-CAP-03), so nothing warned about it.
- **What was not tested:** ambiguity in the request, changing existing code, more than one file, larger tasks,
  and any recovery path. One run per arm, so no variance. The agent got the full request while the swarm's coder
  got the Lead's eleven acceptance bullets, which is how the swarm works but is a difference between the arms.

## Building the remaining phases, round 1: the Phase 3 leftovers (2026-09-19, evening)

The user asked for every remaining phase to be BUILT first and tested for real afterwards. The method for each round:
requirement text quoted from the blueprint (extracted to a searchable text file so agents read the source, not a
summary), builder agents with exclusive files, then my own review, seeded-bug (mutation) checks against the
integration code, an independent nemotron review (the NVIDIA key works again), and a local commit. Round 1 took the
Phase 3 items that block parallel coders:

- **Schema v5** (`db.py`): `usage_ingested`, `review_verdicts`, `integrity_state`, `lineage`, `project_state`,
  `intents`, and a small `_ensure_columns` step because `CREATE TABLE IF NOT EXISTS` cannot add a column to a table an
  earlier version made. (Replaced on 2026-09-22 by numbered, backed-up migrations: see the round 5 section below.)
- **Real usage into the ledger** (ASES-CAP-03, `usage.py`, `hermes.session_usage`): every pass first counts each
  finished worker session once, attributed to its plan task; the outgoing card of a fix-card repoint is counted just
  before the repoint; ready coder cards are parked when the reviewer's provider cannot afford the review reserve.
- **Gate 0 serialization** (ASES-GIT-08, `plan.py`): overlapping touches with no dependency path become a chain in
  plan order; `swarm approve` shows what was serialized; Gate 0 now also rejects a gate profile with no commands
  (it would be vacuously green).
- **Merge-time checks** (ASES-GIT-03/13, REV-05/06, QG-01, `review.py`, `controller.process_merge_queue`,
  `mergeq.merge_task`): the merge queue no longer relies on the review lane having seen the card. It requires the
  reviewer profile's completion, a schema-valid PASS verdict (both the blueprint's shape and the shape Hermes's review
  skill actually emits), the approval bound to a commit, the scope check, and the controller's own green Gate 1 record
  for that exact head; the merge is then taken from the checked SHA and refused if the branch moved. Verdicts are
  stored by commit SHA.
- **Primary-checkout guard** (ASES-GIT-12, `guards.py`): checked at run start and every pass; a violation halts the
  run (exit code 3) before anything is dispatched or merged.
- **Fixed on the way:** renames hid a removed out-of-scope path from the touches check; the Gate 1 evidence sent to a
  worker kept the start of the output and lost the failure summary at the end; the Lead is now told how to write touches
  globs.

What the checks found. A first-time independent review by nemotron ultra of the merge-queue change found one real gap
that my own review and the builders' tests had missed: the Hermes review skill's verdict has no commit field, so an
approval could be tied to no commit, and a commit added after the approval with no Gate 1 record yet would merge under
it. The approval is now bound to the commit the verdict quotes, else to the commit the coder's hand-off named, and an
approval that names none is refused. 33 seeded-bug checks were run against the integration code: the first pass missed
two (the project not being handed to the budget gate, and the squash-by-SHA race window), both got tests, and every one
is now caught.

Builder findings worth keeping in view (not all fixed): `integrity.snapshot` fails open and mis-parses quoted paths (the
new guard does not use it); the reviewer's failure summary is now trimmed from the middle; `gate_runs` still has no
branch or gate-configuration hash, so after a fix card an old green row for the original branch can make an unchecked
head read as stale (it heals through Gate 1); `events.redact` does not redact a secret-shaped dictionary KEY; bare
directory names in touches are literals that match nothing.

## Building the remaining phases, rounds 2 and 3: ten modules exist, none is wired in yet (2026-09-19, night)

The user asked for everything to be built first and tested afterwards ("no need to do testing, first build everything"), then
to park the work for the day. Ten builder agents ran in parallel with exclusive files, one per package (the work orders they were
given are in `docs/work-orders/`, and what each reported back, including every deviation and everything it noticed, is in
`docs/work-orders/builder-findings.md`). Because testing was set aside, this round did NOT get round 1's seeded-bug pass or an
independent nemotron review: the only checks so far are each builder's own unit tests (the suite went from 669 to about 3,400),
one builder's hand-made mutations, and a builder's own adversarial read where the nemotron reviewers returned 403. Nothing here has
run against a real Hermes, a real board or a real container, and NONE of it is called from `controller.py` or `cli.py` yet.

Round 2 (blueprint phase 4):
- `questions.py`: `list_questions`, `answer_question` (the comment is posted before the unblock, the answer is secret-scanned and
  never written to the event), `format_questions` (ASES-REC-05).
- `report.py`: `build_report` (seven panels), `render_status`, `render_text`, a self-contained HTML page with no script and no
  external resource, `write_report` (ASES-OBS-01).
- `recovery.py`: `classify_run` over the outcome strings Hermes really writes (`completed`, `review_requested`,
  `changes_requested`, `blocked`, `scheduled`, `reclaimed`, `timed_out`, `stale`, `crashed`, `rate_limited`, `gave_up`,
  `spawn_failed`), `decide` for the whole failure table, lineage counters per plan task, `next_model`, `failure_bundle`,
  `process_failures` (ASES-REC-01, REC-02).
- `bounds.py`: the section 9.3 bounds, project state helpers (planning, running, paused, stopped, finished), `evaluate_bounds`,
  final-gate records, `is_finished`, `finish_project` (ASES-CTL-01).
- `critic.py` and `prompts/critic.md`: a one-shot reviewer call with no toolsets, strict verdict parsing, one repair call, at
  most two change requests, approval bound to the plan hash (ASES-REV-02).
- `reconcile.py` (extended) and `intents.py`: repairs for done-without-record, record-without-done-card, unfinished candidates,
  open intents, dead workers, orphan workers (found by card id in the process command line and never killed otherwise) and orphan
  worktrees, with the three crash points of section 22.7 as scenarios (ASES-REC-03, REC-04).
- `killswitch.py`: stop flag first, `hermes pause`, reclaim, kill verified worker process trees, stop the plan's containers,
  write a stop report, all time-boxed to 30 seconds; `resume_all` clears the flag only after reconcile (ASES-REC-06). Its process
  helpers were checked for real against two throwaway processes (a worker's grandchild died with it, a decoy without the card id
  survived).

Round 3 (blueprint phase 5), plus schema v6 (`resource_leases`, `worktree_snapshots`):
- `sandbox.py`: the Docker policy for worker profiles, a checker for a profile's terminal block, the mount and sensitive-path
  rules, `docker_run_argv` for the controller's own sandboxed gates, the key-visibility and no-network probes, doctor rows. Docker
  was never started and nothing was pulled (ASES-SEC-02, SEC-03, SEC-05, SEC-06, SEC-07, CFG-04).
- `tamper.py` and `gates.py`: a diff parser and checker for deleted or skipped tests, unconditional passes, weakened assertions,
  gate configuration and CI changes, generated artifacts, secrets and large files, with the exact section 22.12 sequence tested on
  real git repositories; `run_gate` takes an optional `runner` so the sandbox can execute the commands (ASES-QG-03, QG-02,
  GIT-07).
- `leases.py` and `guards.py`: per-card port blocks, compose project names, database names and temp directories, singleton locks,
  `.env.ases`, and snapshots of the worktrees no running card owns (ASES-GIT-14, GIT-12).

What the builders found by reading the real Hermes 0.21.3 source (each one is a fix or a decision waiting for the wiring):
- The Docker sandbox cannot use table 33 as written. An explicit `terminal.cwd: /workspace` makes Hermes look for a host
  directory called `workspace`, so the worktree is not mounted (the block leaves `cwd` out); `docker_persist_across_processes` must
  be false or card 2's worker sees card 1's worktree; `docker_run_as_host_user` does nothing on native Windows; Hermes silently
  drops all CPU, memory and PID limits when its probe container fails to start (an image that is not pulled); killed workers leave
  a running container; and a git worktree's `.git` is a FILE pointing at a host path outside the mount, so `git` fails inside a
  worktree-only sandbox (workers are told to commit, and gates call git). That last one needs a design decision before the sandbox
  is switched on: run gates on a `git archive` export and let the controller commit for the worker, or mount the shared git
  directory, which lets a worker touch the integration branch refs and breaks ASES-GIT-02.
- `create_cards_from_plan` never passes `max_retries`, so Hermes gives up after 2 attempts, not the blueprint's 3, and an unknown
  or rate-limit failure on a blocked card is then never unblocked by anything. Real Hermes also refuses `block` on a card that is
  already blocked (it adds the comment, then exits 1), and a `ready` card whose last failure looks like quota or auth is held by
  Hermes's respawn guard forever.
- `mergeq.merge_task` puts the raw secret-scanner findings into the merge failure detail, which `process_merge_queue` writes into
  the fix card body: a secret can reach a card body (ASES-SEC-01). The same function's candidate upsert never resets `reverted`,
  `squash_commit` or `completed_at`, so after a revert, a fix card and a second merge, `check()` would report
  `done_but_reverted` forever. And `process_merge_queue` re-blocks an already-blocked merge card on every pass, which resets its
  age and undoes an answer while the merge still fails.
- `gate_runs`, `merge_records` and `events` have no project column, so two projects that reuse a task key in one database mix
  their rows (the report and the final-gate check both depend on it).
- Two `Bounds` classes and two `stop_requested` functions now exist (`recovery` and `bounds`, `killswitch` and `bounds`) and
  disagree (four fields against eight; stopped against stopped-or-paused; a missing daily reserve reads 0 in one place and 10 in
  another). They must be unified when wired.
- The stop flag only takes effect between steps, so a merge or gate step already running is not interrupted; test 22.13 needs the
  gate runner to check the flag or be killable.
- `events._SECRET_VALUE_PATTERN` only knows the `sk-` shape, so a Stripe-style `sk_live_` key is never redacted from an event.

What is written down but not built: Gates 4 and 5 with the release report (`docs/work-orders/r3_wp_finalgates.md`) and profile
scaffolding with the role prompts (`docs/work-orders/r4_wp_profiles.md`). What is not yet written as a work order: the evaluation
harness (phase 7), the in-memory fake Hermes board, scripted fake worker and fake provider with acceptance scenarios 22.2 to 22.16
(ASES-TST-01, TST-02), and hardening (phase 9: worktree and branch cleanup, real migrations, log retention, the runbook).

The wiring plan (`run_pass` version 2): stop flag first; primary-checkout guard and idle-worktree guard; usage ingest; review-round
refresh and `process_failures` (the controller applies `fresh_attempt` and `replan`); bounds evaluation and stop reasons; budget
gate plus un-parking when the provider's window resets; review lane with the tamper and secret checks; dispatch; `.env.ases`
provisioning for running cards; merge queue with a stop check between steps and intent records around each multi-step action;
final gates and the release report once every merge card is done. New CLI commands: `questions`, `answer`, `status`, `report`,
`critique`, a real `stop` and `resume`, `init`; `approve` requires a critic PASS for the exact plan hash; `run` reconciles for real
at start.

## Building the remaining phases, round 5: the modules are wired in, plus the fake rig, evals, hardening, final gates and profiles (2026-09-21 to 2026-09-22)

The user said "lets continue development", and later to park the work once the agents finished. Nine builders ran in parallel with
exclusive files (work orders `docs/work-orders/r5_*.md`, plus `r3_wp_finalgates.md` and `r4_wp_profiles.md`; every builder's
deviations and findings are in `docs/work-orders/builder-findings.md`). As before, testing was set aside, so rounds 2 to 5 got no
seeded-bug pass and no independent nemotron review (ultra and super returned 403 to several builders; they reviewed their own work
adversarially, and the hardening builder ran 60 seeded-bug mutants on its own code: 59 killed, 1 equivalent). The suite now has
5,277 (2 skipped) tests passing. Nothing has run against a real Hermes, a real board or a real container.

Before dispatching, the design was checked against the real Hermes 0.21.3 source, which corrected the work orders: `block_task`
accepts only a `running` or `ready` card (blocking a `blocked` merge card or a `todo` card fails AFTER leaving a comment); a
dispatcher give-up is `blocked` with a `gave_up` event and no `blocked` event; a second block of the same kind after an unblock goes
to `triage` (`block_loop_detected`, limit 2); `unblock` also returns a `scheduled` card to `ready`; `--max-retries 3` allows two
retries; `hermes kanban block` needs `--kind` BEFORE the card id (argparse rejected the other order, checked offline). `hermes.py`
(`kanban_block(kind=)`) and `events.py` (nvapi-, Stripe, AWS, Google, Bearer, JWT and PEM shapes; `redact_text`; a number, boolean or
null under a credential-shaped key is kept) were changed by the architect. A shell heredoc turned `\b` into a backspace character and
silently broke every secret pattern until a test failed; regex files are written with the Write and Edit tools.

What each package delivered:
- **Controller loop v2** (`controller.py`): `run_pass` now runs, in order: the stop flag, the primary-checkout guard, idle
  worktrees (warnings), usage ingest, recovery (`process_recovery`: fresh-attempt and model-switch cards with the failure bundle,
  re-plan questions, lineage escalation, and a redrive of any decision the controller could not carry out, because
  `recovery.process_failures` counts a failure before the controller acts), bounds (a breached project bound pauses with a report),
  the budget gate and un-parking (a card parked for budget returns to `ready` through `hermes kanban unblock` when it is affordable
  again), the review lane, dispatch, `.env.ases` provisioning and lease sweeping, the merge queue (with a stop check between steps,
  `ask_user` for escalations, redacted card bodies, `max_retries` on every card it creates) and finalization (Gates 4 and 5, the
  release report). Steps that are not safety critical are isolated: an exception in one is an event, not a failed pass.
- **Merge queue and review lane** (`mergeq.py`, `review.py`, `usage.py`, `gates.py`): `merge_task` polls a stop callable before the
  candidate, Gate 3 and the fast-forward; writes build-candidate, fast-forward and revert intents; resets the merge record of a task
  that is merged again after a revert (the reconcile builder's `done_but_reverted` bug); redacts its detail. The tamper check now runs
  in the review lane and the merge-time check, `gate_config_paths` names the files a gate command uses, a check that could not run
  fails closed at merge time and never sends a card back. Usage ingest records a `model_mismatch` event; gate output is redacted
  before it is stored.
- **Questions and escalation** (`questions.py`, `recovery.py`, `report.py`): a question is now what Hermes really produces: a
  worker's block, a dispatcher give-up, the triage lane after a block loop, or an `ASES QUESTION:` comment on a card that cannot be
  blocked again. `ask_user` is the one way the controller asks; `switch_model` is applied on a fresh card, not in place.
- **Command line** (`cli.py`, `doctor.py`, `config.py`): `questions`, `answer`, `status`, `report`, `critique`, `approve` (needs a
  critic PASS bound to the plan hash, or `--skip-critic`), `run` (real reconcile at start, exit code 5 when something cannot be
  repaired, exit code 4 when stopped or paused), `stop`, `resume`, `init`, `eval`, `clean`, `retention`, `doctor`; every printed line
  is ASCII. `config/swarm.yaml` gains a disabled `sandbox:` block and a `retention:` block.
- **Migrations and hardening** (`db.py`, `hardening.py`, `docs/operations.md`, `docs/runbook.md`): numbered, backed-up migrations
  (a newer database is refused; two processes upgrading at once are safe); version 7 only adds a nullable `project` column to
  `gate_runs`, `merge_records` and `events`. `swarm clean` removes stale worktrees and squash-merged `swarm/*` branches, `swarm
  retention` prunes old logs, reports, stop reports, evaluation runs and database backups. Both are dry runs by default.
- **Final gates** (`finalgates.py`): Gate 4 (built-in tracked-tree scan plus the plan's `gate4` profile), Gate 5 (the plan's `gate5`
  profile, else every distinct task command), the release report, and `finalize`.
- **Profiles and prompts** (`profiles.py`, `prompts/`): the roster as data, eleven role prompts, and `swarm init` (dry run by
  default; `--apply --yes` writes with backups; the kanban limits are an opt-in `--global`).
- **Evaluation harness** (`evals.py`, `evalkit/`): tasks E1 to E11, a dry run unless `--spend-quota`, a per-run budget re-check, raw
  results kept locally and redacted, a regression check, no combined score.
- **Acceptance rig** (`fakes/board.py`, `fakes/worker.py`, `fakes/provider.py`, `tests/acceptance/`): an in-memory Hermes with the
  real block, unblock, triage-loop and give-up rules and real git worktrees, scripted workers and personas (including the five
  section 22.12 tampering attempts), a bigger fake provider, and two demonstration scenarios that drive the real controller.

Real problems the builders found (each is a fix or a decision, none is fixed yet unless noted):
- Through the review wiring the gate-configuration and assertion-weakening findings can never fire: the scope check runs first, so a
  path that reaches the tamper check is already allowed by the task's touches, and a wildcard touches glob silently allows config
  edits. Plan-time validation of touches (Gate 0) is needed.
- Gate 4 fails on ASES's own repository (37 fake keys in tests and docs) and on any repository that tracks `dist/` or `build/`; it
  has no allowlist.
- A reviewer that completes a card with CHANGES_REQUIRED dead-ends it: only `kanban_complete` can carry verdict metadata, so the
  card goes `done` and the merge queue refuses it once. `kanban_request_changes` and `kanban_block` take no metadata in 0.21.3, so
  the reviewer prompt puts the structure in the reason text.
- `hermes pause` does not stop the CLI dispatch that `run_pass` calls (only the gateway loop honours it), so the pass checks the
  stop flag itself.
- Hermes's `kanban.auto_decompose` defaults to true (it would decompose triage cards with an auxiliary model on its own), and its
  `worktree_sync` key is not read by the kanban dispatcher, which always cuts worktrees from `HEAD`.
- The Reviewer still has write tools (one combined `file` toolset), the Docker sandbox cannot run git inside a worktree-only mount
  (a worktree's `.git` is a file pointing outside it), `docker_run_as_host_user` does nothing on native Windows, and Hermes silently
  drops all container limits when its probe container fails to start.
- The request ledger probably under-counts worker sessions: Hermes keeps auxiliary calls in separate rows.
- `revert_merge` marks a merge reverted even when `git revert` fails, and nothing calls it (ASES-GIT-05 stays partial).
- `gate_runs`, `merge_records` and `events` still have no working project scope (the column exists since migration 7, nothing writes
  it); two projects that reuse a task key in one database collide in `merge_records`.
- `bounds.set_status(paused)` drops the reason, each pass makes about five `kanban_show` calls per task, and a project paused by the
  re-plan bound pauses again after `swarm resume` unless `replans_per_project` is raised.
- The real `data/ases.db` was upgraded from schema 3 to 7 once (a builder ran the real CLI as a wiring check): backed up first,
  integrity checked, every row identical, no data lost.

Still to do: acceptance scenarios 22.3, 22.5, 22.7 to 22.16 on the rig (the rig only has two demonstrations), the project-scoping
sweep, plan-time validation of touches, a Gate 4 allowlist, wiring the post-merge revert, the triage lane (ASES-LED-03), the
decisions that wait on the user (Hermes global configuration, Docker, reviewer capacity, private-code provider, coder-2 and coder-3),
and then testing at zero quota only: on 2026-09-22 the user said not to burn xKiro tokens on tests, so there are no real runs
unless they ask for one; the fake rig, seeded-bug checks and an independent review cover everything built since round 1.

## Round 6: ten of sixteen acceptance scenarios on the fake rig, project-scoped records, the post-merge revert, the triage lane (2026-09-22)

The user said "continue all work... you are free to deploy sonnets as many as u want", immediately after "no need to test and burn
the tokens from xkiro" -- so this round is bigger (ten builders at once, matching the earlier rounds' scale) but every builder was
told, in writing, never to call a real Hermes, a real model provider, or Docker. Everything here runs on the fake acceptance rig
(`ases.fakes.board.FakeHermes`, real git worktrees, no network) or plain unit fakes. Work orders: `docs/work-orders/r6_rules.md` +
`r6_wp_*.md`; every builder's report is in `docs/work-orders/builder-findings.md`. The final merged-tree suite: 5,416 passed, 2 skipped, 0 failed.

Four packages fixed real gaps in the controller itself:
- **CORE** (`gates.py`, `bounds.py`, `mergeq.py`, `controller.py`): `gate_runs` and `merge_records` are now project-scoped
  (schema v7's `project` column, added in round 5, finally gets a writer); the post-merge revert trigger is wired (ASES-GIT-05:
  after every real coder merge, Gate 3 re-runs on the new integration HEAD, and a red result reverts, records, and opens a fix
  card, or halts the run if the revert itself fails); and a reviewer that calls `kanban_complete` with a CHANGES_REQUIRED or
  BLOCKED verdict, instead of the proper Hermes verdict tools, no longer dead-ends the card forever -- it is reopened for its
  implementer instead. CORE also found that the NULL-tolerant scoping it used is not a complete fix for `merge_records`: SQLite's
  `ON CONFLICT` dispatches off the table's declared primary key, which is `task_key` alone, so two projects reusing a task key
  would still have their upserts collide and overwrite each other's row. It wrote up the exact migration this needs
  (`(project, task_key)` as the primary key) and which five other modules would need updating alongside it, for a future round.
- **TV** (`tamper.py`, `plan.py`, `finalgates.py`): Gate 0 now rejects a task whose `touches` is a wildcard glob broad enough to
  cover gate or CI configuration, unless the task explicitly sets `allow_gate_config_changes: true` -- closing the hole two
  independent builders confirmed this round (the scope check runs before the tamper check, so a broad touches glob silently
  exempted config edits from ever being caught). Gate 4 gained an allowlist (`plan.gate4_allowlist`), tested read-only against
  ASES's own repository: 60 blocking findings, all sample keys in `tests/` and `docs/`, all excused once allowlisted, and Gate 4
  now passes on ASES's own tree.
- **LED** (new `triage.py`): the triage lane (ASES-LED-03). Reading the real Hermes source settled an open question: a worker can
  already propose a card in triage itself (`kanban_create(triage=true)`), so ASES needs no card-proposal helper, only discovery,
  validation and promote/archive. A round 6 acceptance test then found a real bug in the same module: `promote_card` calls
  `kanban_promote` unconditionally, but Hermes can only promote a card from `todo` or `blocked`, never `triage`, so it currently
  always fails on the one card shape it exists to handle. Not yet fixed.
- **FIX** (`recovery.py`, `killswitch.py`, `report.py`): consolidated the two independently-defined `Bounds` classes into one
  (`recovery.Bounds` is now an alias of `bounds.Bounds`), and, after reading every real caller, deliberately KEPT the two
  differently-named `stop_requested` functions rather than merging them, because `tests/unit/test_cli_commands.py` (a file it does
  not own) correctly relies on them meaning different things. `report.HEALTH_KINDS` gained the four event kinds round 5 introduced
  but never added.

Six packages wrote ten of the blueprint's sixteen 22.x acceptance scenarios, all zero-quota, all driving the real controller against
`FakeHermes`:
- 22.3 (failure and fallback) and 22.9 (quota exhaustion): one card walked through every failure classification end to end, and a
  30-request daily cap correctly parks, shows its reset time, never thrashes, and resumes with state intact.
- 22.5 (parallel) and 22.13 (kill switch): three cards running at once with distinct worktrees, branches, profiles and lease-assigned
  ports, a fourth queued by `max_in_progress`; and `killswitch.stop_all`/`resume_all` driven against three live cards with safe fake
  process handles.
- 22.7 (crash recovery, all three named crash points): a running card, a candidate build, and the gap between the fast-forward and
  the merge-card completion, each faithfully simulated and each recovering with no duplicate cards, no orphan workers, no
  half-merged state.
- 22.10 (secret leak) and 22.12 (gate tampering): a planted secret-shaped value never appears anywhere the controller writes, and
  three of the five blueprint tampering attempts (deleted test, skip marker, `|| true`) each fail Gate 1 with the right finding
  kind through the real review lane.
- 22.11 (prompt injection): written honestly, since Docker never runs in this suite -- three of the blueprint's four clauses
  (nothing outside the worktree, the integration branch untouched, a security event recorded) are proven fully end to end using a
  worker step that writes outside its own worktree; the fourth (a real sandboxed network block) stays at the policy level on
  purpose, documented as such rather than faked.
- 22.14 (plan rejection), 22.15 (idempotent re-run), 22.16 (data class): a rejected plan never creates an implementation card;
  card creation survives being run twice and once more after the database is deleted (though a real gap was found here too, next
  paragraph); a data class must be declared, and an unsafe provider is refused at Gate P.

Real problems the round 6 builders found, none fixed yet unless noted:
- `create_cards_from_plan`'s fix-card protection only works while the `plan_tasks` row that names the current fix card still
  exists. Delete the ASES database and re-run card creation a third time, and it silently reverts `work_card_id` to the ORIGINAL,
  superseded work card -- the board stays consistent (no duplicate card), but ASES's own bookkeeping goes stale, pointing at a card
  that is no longer the task's real current one.
- The private-data-class guarantee ("cards never routed to a provider marked as training on inputs... they park instead") is
  enforced only ONCE, at Gate P. Confirmed empirically: `process_budget_gate` never calls `check_data_class`, and the only
  per-pass call site is inside the model-switch branch of recovery, reached only after a card's second capability failure. A
  provider that becomes unsafe after approval is never parked for that reason.
- A card that crashes once with genuinely auth- or quota-shaped error text, without tripping the retry breaker, can get stuck in
  `ready` forever: Hermes's own respawn guard refuses to redispatch it, and `recovery.process_failures` only ever looks at
  `blocked` cards, so it never becomes a question and is never marked credential-unhealthy.
- `FakeHermes.fail_next` could not be armed after `install()` -- two different builders, in two different rounds, independently hit
  the same bug in the acceptance rig itself and had to work around it. Fixed directly after this round landed (a module-level
  frozenset of the real hermes module's public names, captured at import time, replaces the old live, monkeypatch-able check).
- `triage.promote_card` (this round's own new code) always fails on a genuinely triage-status card, for the reason above.

What is still open after this round: 22.8 (merge conflict) was deliberately deferred, since it needed CORE's revert wiring to
exist first, which it now does -- its work order (`r6_wp_ac_d.md`) is written and ready to dispatch as a wave 2 package; 22.4 is
likely already covered by `test_models.py` but was not confirmed; 22.1 and 22.17 correctly stay out of scope, since they need a
real provider; the `merge_records` primary-key migration CORE wrote up; the `events.project` sweep, deliberately deferred again
this round (CORE's own read: `events.record` is called from roughly a hundred places, a full sweep is its own change); and a fix
for `triage.promote_card`, which needs a design decision first (does "promote" a triage card mean calling Hermes's own
`specify`/`decompose` auxiliary-model path, which `r2_rules.md` currently says ASES never runs on its own, or something else)
rather than a mechanical patch.

## Round 7, wave 1: the specify decision, three real bugs fixed, docs and policy scaffolding (2026-09-22)

The user asked what was left to build, was told about the pending design question on `triage.promote_card` and the register's
never-built items, and answered all three in one message: "option A" (ASES may call Hermes's own `specify`, only from
`promote_card`), "fix all the bugs", and "build all" of the never-built list. Four builders ran (work orders
`docs/work-orders/r7_rules.md` + `r7_wp_*.md`), still under the zero-quota rule for every test; one real Hermes call
(`kanban_specify`) is now authorized in PRODUCT code, for the first time since that rule was set, narrowly and only from that one
function. Two architect fixes landed directly (both small, both blocking, both found independently by two builders each): a gap in
`FakeHermes` (it had no `kanban_specify` method, so adding the real wrapper broke every acceptance test until the fake caught up),
and a regression the stricter data-policy check caused in `recovery.next_model` (it stopped passing the new required field, so it
silently treated every switch-model candidate as unsafe for a private/confidential project). Suite: 5,478 passed, 2 skipped, 0 failed.

- **SPECIFY** (`hermes.py`, `triage.py`): `hermes.kanban_specify`, confirmed against the real Hermes source down to the exact exit
  code convention (an `ok: false` decline is exit code 1 with JSON still on stdout, not a zero-exit JSON field) and every real
  failure reason string Hermes can return. `triage.promote_card` now actually works on a genuinely triage-status card, which it
  never could before this round.
- **FIXES** (`controller.py`, `recovery.py`): the fix/retry-card pointer no longer goes stale (or duplicates) after the ASES
  database is deleted and cards are recreated -- `create_cards_from_plan` now asks the BOARD directly when its own bookkeeping is
  silent, using the merge card's parent lineage disambiguated by each candidate's `created_at` (never by id or list order: real
  Hermes card ids are random, only the fake board's happen to be sequential, which the builder's report flags precisely for
  anyone building similar logic later). The per-pass budget gate now also checks the project's data class before dispatching a
  card, parking a violation with its own reason prefix that is deliberately never auto-unparked. A card stuck `ready` after an
  auth- or quota-shaped failure that never tripped Hermes's own retry breaker is no longer invisible to recovery.
- **POLICY** (`cli.py`, `policy.py`, `config.py`, `doctor.py`): the Lead is now asked to write `docs/ases/contracts/`,
  `docs/ases/decisions/` and `AGENTS.md` (confirmed, by reading the installed Hermes source, that it genuinely auto-loads
  `AGENTS.md` at session start -- with the caveat that it is first-match-wins against `.hermes.md`/`HERMES.md`); a compatible
  provider data-policy string is no longer enough on its own for a private/confidential project, an explicit, recorded
  verification date is now required; `swarm doctor` warns when two different providers share the same key (never when one
  provider serves several profiles).
- **AC-D** (`tests/acceptance/test_22_8_merge_conflict.py`): the sixteenth and last blueprint acceptance scenario. Proved Gate 0's
  overlap serialization, a real git-level merge conflict resolved by a fix card, and CORE's round 6 post-merge revert trigger,
  including a full re-run of the real gate command against every commit in the final history to prove the integration branch is
  green everywhere except the one seeded, verified-bad commit -- not assumed.

Every "not_covered" row on the register the user asked to build is now `in_progress` except the two greenfield-bootstrapping rows
(ASES-GIT-10/11), deliberately held for wave 2, since bootstrapping an empty repository needs `controller.py`/`cli.py`, which this
wave already had in flight. Wave 2 (`r7_wp_wave2_roles.md`, already written) also fixes a related bug the investigation surfaced:
six places in `controller.py` hardcode `role == "coder"`, which would silently mistreat a Tester role's card (no review, no merge,
treated as a review-only no-op) the moment a project actually enables one.

## Round 7, wave 2: greenfield bootstrapping and the Tester role's hardcoded-role bug (2026-09-23)

Wave 1 left two things for wave 2, both from the same investigation: the two greenfield-bootstrapping rows
(ASES-GIT-10/11), and a bug the investigation surfaced along the way, that controller.py hardcoded
`role == "coder"` (or its negation) in several places to mean "the only role that produces a real git commit",
which would silently mistreat a Tester role's card the moment a project actually enabled one. Package ROLES2
built both; two more agents then independently verified the work before it was committed, a heavier check than
usual because this touches the merge and gate machinery directly.

- **`controller.ensure_repo_bootstrapped(repo, integration_branch, *, conn=None)`** (ASES-GIT-10): detects a
  truly empty repository (no `.git`, or `.git` with zero commits), creates the integration branch and an initial
  commit carrying whatever is already on disk plus a `.gitignore`/README if missing, with a per-invocation git
  identity, never a persistent git config. Never touches a repository with real history, on any branch. Wired
  into `cli.cmd_plan`, before the Lead is ever invoked. Worth recording plainly: the blueprint's literal wording
  in section 8.3 assigns this to `swarm run` ("git worktree add needs at least one commit. swarm run MUST create
  an initial commit..."), but the actual implementation does it at plan time instead. This is a necessary
  adaptation, not a missed requirement: `cmd_approve`'s `publish_plan` already refuses to operate on a repository
  that is not on the integration branch, so for a genuinely empty repository the bootstrap has to happen no later
  than plan time, or approve would fail first on every greenfield project, before a single worktree is ever
  created. The requirement's own reasoning (worktree creation needs a commit) is satisfied either way.
- **`cli.cmd_plan`'s prompt** (ASES-GIT-11) now explicitly tells the Lead to plan a scaffold task first in an
  empty repository and to give parallel work an explicit `depends_on` pointing at it. A real limit was found and
  proven with a test, not assumed away: Gate 0's touches-overlap serialization does NOT by itself order parallel
  work after a scaffold task, since a scaffold task's touches (`pyproject.toml`, `package.json`, `AGENTS.md`)
  essentially never literally overlaps an ordinary task's touches. The "parallel work starts only after the
  scaffold is merged" guarantee rests entirely on the Lead itself writing the `depends_on`; this is a prompt-level
  instruction, not a code-enforced one, and the row stays `in_progress` for exactly that reason.
- **`_COMMITTING_ROLES = frozenset({"coder", "tester"})`** (ASES-QG-05, ASES-ROL-09): the new single source of
  truth for which roles produce a real commit, replacing five hardcoded `role == "coder"` comparisons in
  controller.py (`_finish_instructions`, the review-reserve budget check, the merge queue's verdict-validation
  gate, `allow_empty` for `mergeq.merge_task`, the post-merge Gate 3 recheck). One independent review agent
  corrected the original estimate of six sites to five: the sixth was a docstring, not code. Reverting the five
  sites and re-running the tester-focused tests showed the old bug was worse than "a tester's card fails to
  merge": Gate 3 runs unconditionally on any real diff regardless of role, so a tester's real commit would still
  have merged under the old code, just with the reviewer-verdict check, the Gate 1 recheck, and the post-merge
  Gate 3 recheck all silently skipped. Unreviewed, unchecked work merging silently, not work failing to merge.
- **The same bug, found and fixed independently in `reconcile.py`** (ASES-REC-04): `_done_without_record`
  hardcoded `role != "coder"` to mean "this role's merge card legitimately has no git commit", the same
  hardcoding just fixed in controller.py. A tester's merge card marked done with no landed commit behind it
  (crash, force-push, board/git desync) would be silently written as a no-op merge record on `swarm resume`
  instead of escalating for a person to look at. Fixed by reusing `controller._COMMITTING_ROLES` directly
  (confirmed no circular import between the two modules) rather than duplicating the frozenset.

Every one of the five fixes above, plus the reconcile.py fix, was proven with a before/after test: reverted,
shown to fail against the old code, restored, shown to pass. A second, independent agent then re-read the diff
line by line, re-derived the safety properties of `ensure_repo_bootstrapped` from the code itself rather than
trusting the builder's report, and swept the rest of the repository (`mergeq.py`, `review.py`, `evals.py`,
`recovery.py`, `plan.py`, `gates.py`, `finalgates.py`, `tamper.py`, and more) for any other hardcoded-role site
or stale test the fix might have left behind. It found none. Final suite: 5,505 passed, 2 skipped, 0 failed.

Every "not_covered" row on the register the user asked to build in round 7 is now `in_progress`. This closes out
the "build all" instruction that opened round 7: nothing on the register is still `not_covered`.

## Round 7, closing the audit: ASES-CFG-05, and a doc-ordering defect (2026-09-23)

The round 7 wave 2 audit above left three rows not_covered. ASES-ARC-01 turned out to be satisfied by design
(closed same day, by inspection, no code needed) and ASES-DOC-03 stays honestly partial (build order was
compressed across rounds and no exit test has run for real yet, by disclosed policy). The audit also found a
physical defect in this very file: a 2026-09-18/19 section had been sitting after three much later round
sections, because new sections were repeatedly inserted before a fixed anchor without ever checking that
anchor's own place in time. Fixed the same day: the misplaced block now sits where it actually continues from,
confirmed by a line-count-preserving diff.

ASES-CFG-05's live half ("do not export provider keys into worker shells") got a real fix. Two build-and-
independently-verify pairs ran back to back: the first added `src/ases/procenv.py` (a new, ASES-import-free
module; `scrubbed_environ()` could not be a public function of `hermes.py` itself, since `FakeHermes.install`
requires a fake for every public name defined there, and an environment scrub is not a Hermes call) and wired
it into `hermes.py`'s `_run` and `evals.py`'s `_run_process`. The second, working from a spawned follow-up
suggestion that named three more real launch sites, wired the same helper into `cli.py`'s Lead call and
`critic.py`'s reviewer call, and gave `profiles.py`'s real `hermes profile create` call a new `_hermes_runner`
wrapper rather than changing `sandbox.default_runner` itself: that runner also drives the Docker sandbox's own
key-leak probes, which need to see a real, unscrubbed environment to actually detect a regression, so scrubbing
it globally would have blinded the very probes meant to catch this class of bug. Every one of the five real
hermes-launch sites in `src/ases` is now covered; both verify passes independently confirmed no sixth site was
missed.

Two things stay open, honestly, not silently closed: a real Hermes-gateway-dispatched worker is spawned
entirely by Hermes's own gateway, a process ASES never touches, so no ASES-side fix can reach that path (this
matches ASES-ARC-01's own finding that ASES never spawns a worker itself); and `gates.py`'s own gate-command
runner still executes with the operator's full, unscrubbed environment, a separate, deferred design decision
(same family as ASES-SEC-01/03) two independent agents found and correctly declined to patch under this fix's
scope. Suite: 5,512 passed, 2 skipped, 0 failed.

## Round 8: housekeeping, then gate commands stop seeing the operator's credentials (2026-09-27)

Zero quota throughout; one workflow of Sonnet builders, independent Sonnet reviewers and a Haiku live-verification pass last. Two
housekeeping fixes first. The register drift check (ASES-DOC-02) had been failing for a reason unrelated to drift: the blueprint
docx moved into a `Desktop\AISES` folder. `spec/check_requirements.py` now defaults to the new path and takes an
`ASES_BLUEPRINT_DOCX` override (precedence: `--docx`, then the variable, then the default), with a not-found message that says
how to point it elsewhere. And the "Known gaps" list below was re-checked bullet by bullet against the code and the register:
stale bullets struck through with a dated note, partly true ones rewritten with their register IDs; one overstated claim the
first rewrite made (that Gate 1 always re-runs before the reviewer sees a card) was caught by the independent review and fixed.

Then the gap the CFG-05 close-out left open (ASES-CFG-04, ASES-CFG-05; blueprint p213, "Never export provider keys in the shell
that launches the gateway or the controller"). `gates._run_commands` now starts every gate command with
`procenv.scrubbed_environ()`, and `run_gate`'s own `git worktree add`/`remove` do too, so neither model-authored code under a gate
nor a `post-checkout` hook planted in the shared `.git/hooks` can read a provider key from the controller's shell. Design, decided
by the architect: no pass-through allowlist (a gate that needs a credential now fails, visibly; if a real project ever needs one,
it belongs on the plan, published at Gate P and covered by the gate-profile pin, like `gate4_allowlist`); `GIT_AUTHOR_NAME`,
`GIT_AUTHOR_EMAIL` and `GIT_AUTHOR_DATE` exempted by exact name in `procenv.py` (they match the pattern only because "AUTHOR"
contains "auth"); `SSH_AUTH_SOCK` and `XAUTHORITY` stay stripped because they are capability-bearing. Proven before and after:
against the old `gates.py` the new tests print `KEYSEEN`, and the planted hook records the key. A new acceptance test
(`tests/acceptance/test_22_10_gate_env.py`) drives it through a real controller pass on the fake board.

A read-only sweep alongside found the same exposure one level out, and proved it empirically against unmodified ASES helpers in
throwaway repositories: the controller's OTHER git calls (merge queue, guards, reconcile, hardening, integrity, controller) run
with the full environment in repositories a local-backend worker can write `.git/config` and `.git/hooks` into, so a planted
`post-checkout` hook or `core.fsmonitor` script captured planted keys; and the merge queue's Gate 3 secret-scan diff lacks the
`--no-ext-diff --no-textconv` that `tamper.py` already uses, so a worker-defined textconv driver could hide a secret from it. That
became round 9's GITHARDEN package. An architect re-run also fixed a pre-existing test fragility (`test_gate_worktree_cleaned_up`
searched `git worktree list` for the text "wt", so any temp path containing "wt" failed it). Suite: 5,533 passed, 2 skipped, 0 failed.

## Round 9: Tier 2 register honesty, then the Tier 1 gaps in parallel worktrees (2026-09-27)

The user asked for "tier 2 first, then tier 1 items", with many agents at once. Zero quota throughout. Every package was
built by a Sonnet agent in its own git worktree (`C:\Users\masoo\ases-wt\<package>`, its own pytest `--basetemp`, no
`git stash` anywhere, since the stash is shared by every worktree), reviewed by a second Sonnet agent with a nemotron second
opinion (the MCP tools still return 403; `tools/nemo.py` run with the nemotron server's own venv works), given a fix round
when the review found something, and live-verified by Haiku last. The architect reviewed each diff and merged the branches
one at a time, Tier 2 first.

Tier 2. A register-hygiene pass re-checked every partial and in_progress row against the code: ARC-02, ARC-03, ARC-04,
GIT-07, CFG-01 and TST-01 moved to covered on evidence, CTL-01 and TST-02 to partial with the exact gap named, and stale
notes (MOD-02, REV-01, REV-03) corrected; the architect set GIT-01 back to partial, because blueprint p169's "Phase 3 MUST
verify the actual base commit before a worker starts" is not built. `worktree_sync: false` turned out to have been pinned
since round 5; `swarm doctor` now names ASES-GIT-16 on a drifted value.

Tier 1, nine packages. GITHARDEN: `src/ases/gitexec.py` is the one way the controller runs git (repository hooks and
`core.fsmonitor` disabled, the credential scrub, `--no-ext-diff --no-textconv` on every diff whose text matters), closing
what round 8's sweep proved, including a textconv driver that could hide a secret from the Gate 3 scan. GATESANDBOX: every
gate caller goes through `gates.resolve_runner`, so gates can run in the Docker sandbox with a self-contained checkout and a
task-scoped, Gate-0-validated, pinned network exception (the architect wired `cli.py` to pass that exception into the pin,
the gap its builder reported); off by default until Docker runs for real. MERGEPK: `merge_records` keyed by
`(project, task_key)`, schema v8. EVENTSPROJ: events carry their project; every reader scopes through one definition,
`events.PROJECT_SCOPE_SQL` (the architect replaced twelve inline copies with it and scoped the two report panels the
builder had left open, the health panel having shown other projects' merge failures). CIPIN: a gate/CI config change
needs the plan task's explicit `allow_gate_config_changes` marker, and Gate 0 was tightened to match (reversing round 6's
literal-touches exemption). IDLEWT: the idle-worktree false positive fixed, still a warning. PAUSEREASON: a paused
project keeps its reason. DOCTOR: source URLs and an exported-provider-key warning. CAPDOC: `docs/provider-onboarding.md`.

Merging found real conflicts only where CIPIN, GATESANDBOX and EVENTSPROJ each added a parameter to the same Gate 1 call
sites; all three were kept. Register after round 9: 48 covered, 39 in_progress, 13 partial, 3 not_applicable, 0
not_covered. Suite: 5,668 passed, 2 skipped, 0 failed.

## Round 10: the base-commit check, and what round 9 surfaced (2026-09-27)

Same method as round 9 (one Sonnet builder per package in its own worktree, an independent Sonnet review with a nemotron
second opinion, fix rounds, Haiku live verification, the architect merging one branch at a time), zero quota. Four
packages. BASECHECK built the check blueprint p169 requires ("Phase 3 MUST verify the actual base commit before a worker
starts"): Hermes's Kanban dispatcher always cuts a card's worktree from the board repository's local HEAD and never reads
`worktree_sync`, so `guards.check_card_base` reads each work branch's creation commit from its reflog and requires it to be
an integration head ASES itself wrote or adopted (a new `integrity_heads` history, schema v9, so a card dispatched before a
later merge still passes). Hermes starts the worker, not ASES, so "before it starts" becomes: checked on the first pass that
sees it running and blocked for the user if wrong or unverifiable, and refused by the merge queue whatever happens, so a
wrong base can never land. Its nemotron review caught a real bug before merge: git translates reflog messages under a
non-English locale, so the check now forces `LC_ALL=C`. GATEPIN pinned the `allow_gate_config_changes` marker alongside the
network exception and added the 22.12 acceptance test for `gate_config_changed`. BUDGETFIX made 10 percent the one
daily-reserve default and found that a paused project's clock running on was correct, not a bug. CALLERS gave the smoke test
a command (refused without `--spend-quota`, fake-tested only) and printed the residual risks in `swarm init` and
`swarm doctor`. The architect wired `swarm doctor --repo` so the base check's prerequisite is actually checked. Branch names
were deliberately left alone (see Known gaps). The owner decided the same day that commits are authored by the owner alone,
with no co-author lines, and that the reviewer's write access is accepted until the sandbox closes it. Register after round
10: 50 covered, 40 in_progress, 10 partial, 3 not_applicable, 0 not_covered. Suite: 5,757 passed, 2 skipped, 0 failed.

## Round 11 and real-run stage A: under-declared models rejected; the first real doctor since round 7 (2026-09-28)

The owner went to sleep after asking for autonomous work ("keep doing things back to back", testing capped at about 30
minutes a cycle, pushing allowed). Round 11 was one package, MOD02, from a gap found while tabulating what was left:
blueprint 22.4 says the controller "must reject" a model declared below 64K "before any card starts", and only `swarm doctor`
ever warned. `models.classify_model_context` now decides (too small, or undeclared on a custom `openai_compatible` endpoint,
is rejected; a native Hermes provider with no declaration is not, because Hermes knows its models), and `swarm approve`,
`swarm run`'s pre-flight and `recovery.next_model` all honour it; `tests/acceptance/test_22_4_context.py` plays p406. It
passed its first review and live verification (5,778 passed).

Then stage A, zero provider quota (full record: `docs/stage-a-2026-09-28.md`). The v7-to-v9 migration was rehearsed on a copy
of the real database, the real database was backed up, and a real `swarm doctor --repo` against the Phase 3 test repository
came back HEALTHY, migrating the real database exactly as rehearsed. It showed the machine ready (Hermes 0.21.3, gateway
running, Docker reachable, reflogs on, every pinned model above the context floor, no provider key exported) and the real
Hermes profiles never brought to ASES's desired state (22 drift warnings: memory on, surplus toolsets, `worktree_sync` on,
kanban limits unset, `auto_decompose` true). `swarm init --global` was run as a dry run only: its 20 changes wait for the
owner's yes, because they rewrite the owner's real Hermes configuration.

Also that night: a read-only adversarial audit of everything rounds 8 to 11 built (five Sonnet finders on a snapshot, each
finding checked by an independent skeptic told to refute it) confirmed 12 real defects and refuted one; round 12 fixes them
(`docs/work-orders/r12_audit_findings.md`). Register after round 11: 51 covered, 39 in_progress, 10 partial, 3
not_applicable, 0 not_covered.

## Round 12: the audit's twelve findings fixed (2026-09-28)

Three packages from the verified audit findings, each builder reproducing every finding with a failing test before fixing
it, each passing its first independent review (with a nemotron second opinion). GATEINFRA: a gate checkout that fails for
infrastructure reasons now raises `gates.GateCheckoutError` instead of reading as a red gate, and every caller treats it
like `SandboxInfrastructureError`, so the post-merge re-run can no longer revert a correct, landed merge over a stale temp
directory; a gate command's timeout now kills the whole process tree (`taskkill /T /F`), because on real Windows
`subprocess.run(..., shell=True, timeout=...)` waited out a hung child (the audit measured 8 s for a 1 s timeout), which would
have wedged the merge queue; a gate or merge worktree that cannot be removed is recorded as an event; and the docker CLI on
the gate path no longer inherits the controller's credentials. DATAFIX: every `merge_records` write matches exactly one row
and every single-row read prefers the project's own row, closing the ways a legacy NULL-project row kept by schema v8 could be
reverted by another project's revert, collide on a UNIQUE key, or be double-counted. RUNSTART: a restarted `swarm run` no
longer adopts whatever HEAD it finds (it refuses a HEAD moved since the last recorded one unless `--allow-head-move` is
given, loudly); reconcile-on-start now runs before the project is marked running; the base-commit check re-checks a card
whose branch Hermes has not created yet instead of blocking it; and doctor's texts no longer deny `--repo`. Worth noting for
the method: every one of these twelve was invisible to the fake rig, which is why a skeptic-verified audit on real git, real
SQLite and real Windows subprocesses paid off. Suite: 5,814 passed, 2 skipped, 0 failed.

## Rounds 13 and 14: a second audit of the new code, and the last zero-quota items (2026-09-28)

Round 13 ran two things side by side. TIDY folded the two process-tree kill helpers (gates and evals) into one,
`procenv.kill_process_tree`, keeping each caller's exact behaviour, and gave `swarm doctor` a read-only warning for a gate or
merge worktree that was recorded as leaked and still exists. A second read-only audit looked only at what rounds 11 and 12
had just changed, on the principle that new code is where new bugs are: three findings confirmed, none refuted
(`docs/work-orders/r14_audit2_findings.md`). All three were in the run-start code round 12 had reordered: the MOD-02 model
pre-flight still ran after the wall clock started (the very bug round 12 fixed for reconcile, reintroduced for another
refusal), a pause landing during reconcile could be flipped back to running, and a card whose branch never appeared was
skipped silently forever. Round 14's RUNSTART2 fixed all three (every refusal before the clock starts; a re-read right before
starting; a bounded, visible grace of 5 passes, never a block), and CLOCK made the ledger's day injectable so the daily-reset
arithmetic is tested without monkeypatching a private function, with a test on the UTC midnight boundary. Both packages
passed their first review. With this, the zero-quota queue is empty: everything left needs the owner (applying `swarm init`,
Docker for real, a real run, source URLs, two design decisions). Suite: 5,833 passed, 2 skipped, 0 failed.

## Rounds 15 and 16 and stage B: the sandbox for real, then switched on (2026-09-28)

The owner said "do all" to a list that included stage B (Docker for real) and the open design question of how git
works inside a worker's Docker sandbox. Round 15 built it as two parallel packages, each independently reviewed
(`docs/work-orders/r15_wp_sandbox.md`): SANDBOXIMG (`docker/sandbox/Dockerfile`, `scripts/sandbox_live_check.py`,
commit 6954e88) proved the mount-list denial, the default-deny network and a sandboxed gate run against real
Docker; WORKERGIT (`scripts/workergit_live_check.py`, commit c80827f) proved a worker can commit inside its own
dispatched worktree, by setting `worktree.useRelativePaths=true` on a repository ASES creates and mounting the
repository's `.git` read-only with five writable sub-mounts (objects, refs, logs, worktrees), documented in full
in `sandbox.py`'s own module docstring. Running both checks for real found that the first image, `python:3.11-slim`
with git 2.47.3, could not open the relative worktrees WORKERGIT's design needs (git 2.48+ marks such a repository
with `extensions.relativeWorktrees`); the architect replaced it with `ases-sandbox:py311-2` (`python:3.11-alpine`
pinned by digest, git 2.54.0-r0, `safe.directory /workspace` against a "dubious ownership" refusal on the
Windows bind mount, commit 5aa88d9), and fixed two bugs the real runs exposed in the live checks themselves: a
read-only-safe `rmtree` for stale root-owned git objects, and an exclusion for the base image's own `GPG_KEY`
fingerprint from the credential-name check. The optional Hermes egress proxy stays not installed, an
owner-authorised decision (`ASES-SEC-06`). Full record, with the verbatim PASS lines: `docs/stage-b-2026-09-28.md`.
Round 15 full suite: 5,897 passed, 2 skipped.

Round 16 started from stage C's own finding (`docs/stage-c-2026-09-28.md`): the reviewer requested changes on a
card it could not itself verify, because it cannot run tests and could not see the controller's own gate result.
EVIDENCE (commit 0373fe7, independently reviewed with a nemotron second opinion) closes it: `review._post_gate_record`
posts the controller's Gate 1 result on the card as a comment headed "ASES gate record", redacted before
truncation and posted once per run; reviewer prompt version 2 tells the reviewer where that record is and that a
missing one means the gate has not run yet, never a reason by itself to request changes. Two more findings
surfaced while proving the sandbox: PROMPTVER (commit 1b5cb8b) fixed `profiles.prompt_version()`, which had
reported one global `PROMPT_VERSION` for every prompt regardless of a prompt's own version line, found in the
`swarm init` dry run before it could install the new reviewer prompt under a stale header; PACKEDREFS (commit
e7e2ce8) traced the harmless `packed-refs.lock` message a sandboxed commit prints (git's sequencer cleanup after
a successful commit, needing a lock in the read-only base `.git` mount) and told the coder prompt (version 2,
rule 5) to expect it. SANDBOX ON (commit 409dc86) then flipped `config/swarm.yaml`'s `sandbox.enabled` to `true`
for real: the test repository's five card worktrees were made relative (`git worktree repair --relative-paths`),
`swarm init --global --apply --yes` wrote the Docker terminal block into the real `coder-1` Hermes profile (12
changes, 0 failed, every changed file backed up), and `swarm doctor` came back `HEALTHY`. Full suite with round
16 merged: 5,912 passed, 2 skipped (685.9 s).

What this proves and what it does not: gates and the worker profile now run through Docker rather than the local
backend, and every check above ran against a real container. HERMESDOCKER (merge 5bd81bf) then drove Hermes's
own Docker terminal code with the real `coder-1` profile config at zero quota and found that Hermes runs every
command as `bash -c` while the Alpine image had no bash: every worker command would have failed with exit 127.
`ases-sandbox:py311-3` adds a pinned bash, and on it every HERMESDOCKER probe passes through Hermes's own
`execute` (details in `docs/stage-b-2026-09-28.md`). No real dispatched card has yet run inside Docker. S1 is parked for OpenRouter's daily quota (resets 00:00 UTC); finishing it is the
first real run with Docker workers and sandboxed gates, and decides whether `ASES-SEC-02`, `SEC-03`, `SEC-05`,
`SEC-06`, `SEC-07` and `ASES-CFG-04` can move to `covered`.

## Round 17: worker sandboxes found by profile, and the reduced scenarios made full (2026-09-29)

Tier 1 of the "what's left" list: zero quota, no owner decisions. Three packages in parallel worktrees, each built
by a Sonnet builder and independently reviewed (a Sonnet reviewer plus a nemotron second opinion) with up to two
fix rounds, run as one workflow.

- **CONTAINERS** (merge 161077e; ASES-REC-04, REC-06, SEC-03). The package set out to find worker sandboxes by
  card id (blueprint p353) and found that impossible: Hermes 0.21.3 labels a dispatched worker's container
  `hermes-agent=1`, `hermes-profile=<profile>`, `hermes-task-id="default"` (never the card id, because a kanban
  worker runs without a session key) and gives it a random name. `scripts/hermes_container_labels_check.py` proves
  it with Hermes's own Python against real Docker; `src/ases/containers.py`'s docstring has the file:line trail.
  So `ases.containers` finds sandboxes by PROFILE: a running Hermes container of one of this project's profiles,
  with no card running under that profile, is an orphan and is stopped in reconcile-on-start and in the per-pass
  provisioning step. A profile with live work is left alone. The reviewers' last open finding was the kill switch:
  `swarm stop`'s container step still matched card ids, so with the sandbox on it could never stop a real sandbox
  (p357). The architect finished it: step f now also stops every running Hermes container of this project's
  profiles, live work included, with a before/after proof (four new tests fail without it) and a real-Docker proof
  added to `scripts/orphan_sweep_live_check.py`. The price, stated as a hard constraint and checked by the new
  `profile_isolation` doctor row: Hermes profile names must be unique per machine across ASES projects, since a
  container carries no project id.
- **SCENARIOS** (merge 173641b; ASES-TST-02). 22.2 and 22.6 are full scenarios now (`test_22_2_end_to_end.py`,
  `test_22_6_review.py`): an empty repository through Gate 0, Gate P and the publish of architecture, contracts,
  decisions and plan.json before any card exists; the question through `swarm questions` and `swarm answer`; a
  commit after approval voiding it; repeated violation escalating at the lineage budget. The last two had never
  been tested at any level; each test was shown to fail with its guard removed. No product bug was found. TST-02
  stays partial (22.11's real-Docker network clause is unit-level only); its stale claim about 22.4 is corrected.
- **FAKEFIX** (merge 5e365dd; ASES-REC-03). The register said `FakeHermes.fail_next` could not be armed after
  `install()`; that was fixed in round 6 and the note never updated. The last workaround is gone and 22.15 gained
  a crash in the middle of card creation, repeated with no duplicate cards.
- Housekeeping: the merged round branches `r9` to `r14` and the images `py311-1` and `py311-2` removed.

Full suite after the merges: 5980 passed, 2 skipped. `swarm doctor` HEALTHY, both new container rows PASS.

## Round 18: what the S1 finish found for real, and two fixes (2026-09-29)

The S1 finish (the first real run with the sandbox on; `docs/stage-c-2026-09-28.md`, addendum) found three real problems the
fake rig had never exercised. Two are fixed; the third is the reviewer model itself.

- **UNPARK** (merge 3808612; ASES-CAP-03, REC-01). Recovery parks a card whose worker failed with quota text, but
  `process_unpark` only released the budget gate's own `budget:` parks, so a quota park was never released and S1 stayed
  parked through the reset. Now a card whose reason starts with `recovery.QUOTA_PARK_PREFIX` is released once
  `recovery.quota_reset_passed` says the UTC day of its park is over, and only when `_affordable_now` agrees; never on the
  same day. Proven for real the same night: `card_unparked` on the first pass.
- **REVIEWPATH** (merge 91fb430; ASES-REV-05, ROL-03). A released card kept the reviewer as its assignee, so the gateway
  dispatched the reviewer at once, bypassing the review lane: no Gate 1 re-check, no gate record. `process_unpark` now runs
  `review.record_gate1` (check plus posted record, no send-back) before releasing a card assigned to the reviewer. Reviewer
  prompt version 3 closes a loophole: v2 forbade requesting changes over tests the reviewer cannot run, so the model blocked
  instead; v3 forbids both and keeps BLOCKED for human decisions about requirements or design.
- **Not fixed by code: the reviewer model.** OpenRouter's `cohere/north-mini-code:free` blocked S1 again, word for word, under
  prompt v3 with a sandboxed PASS gate record on the card (Gate 1 ran in a real `py311-3` container: the first sandboxed gate
  on a real card). Hermes moved S1 to triage. Changing the reviewer model or provider is the owner's call.
- **Open, found by the same run: the request ledger undercounts.** Only runs that end through a normal hand-off carry a
  `worker_session_id`, and `usage.ingest_run_usage` counts only those; crashed, blocked and changes-requested runs are
  invisible, which is how the budget gate let S1 run into OpenRouter's quota on 2026-09-28 (ASES-CAP-03, RTE-01).

Both fixes went through a builder or the architect, an independent Sonnet reviewer and a nemotron second opinion, each with a
before/after proof. Full suite after both: 6002 passed, 2 skipped.

## Round 19: Tier 1 and Tier 3, research first (2026-09-29)

The owner's "Tier 1: do it; Tier 3: do all". Zero quota, no accounts created. Five read-only researchers first
(reports in `C:/Users/masoo/ases-wt/_research/r19/`: LEDGER, REVIEWER, GIT12, STOPDOC, PROVIDERS), then seven build packages in
two waves, each built by a Sonnet builder, independently reviewed with a nemotron second opinion, and fixed.

- **LEDGER** (ASES-CAP-02, CAP-03, RTE-01): the ledger's unit is the Hermes session, found through `hermes -p <profile>
  sessions export --source kanban` and matched to its run by the worker's first prompt and the run window; crashed, blocked
  and changes-requested runs are now counted, sessions are topped up to their final count, the Gate P critic is counted.
  Migration 10.
- **REVIEWLADDER** (ROL-05, ROL-06, REV-05, REC-05): the research found that in stage C the reviewer called
  `kanban_request_review` and Hermes made it the card's implementer. Reviewer profiles now carry `pre_tool_call` hooks that deny
  write tools and hand-offs in-run and bounce a stop that only asks for test evidence; the controller answers such a stop once
  with its Gate 1 record, otherwise asks the owner one question, never loops. Model switching is deferred to the owner's
  reviewer choice.
- **PROFILEGUARDS** (DOC-04, PRV-04): no LSP auto-install (Hermes had already npm-installed pyright into two profiles),
  `--no-alias`, no-data-collection routing for the OpenRouter reviewer.
- **STOPGATES** (DOC-04, PRV-01 to 04, ROL-06): the data class checked for the Lead, critic and reviewer; the Lead without a
  terminal; `.env` never committed by the bootstrap; smoke history and report files never silently destroyed; paid models
  refused unless allowed; no `swarm run` without the pinned image. Migration 11 (it collided with LEDGER's 10 at merge and was
  renumbered).
- **TESTSDOCS** (TST-02, DOC-03, PRV-04, CAP-06): 22.11's network clause against real Docker, so ASES-TST-02 is covered;
  `docs/phase-exit-plan.md`; verified data policies only; three reviewer candidates ready to onboard.
- **GIT12** (GIT-12): every change outside a worker's worktree is attributed to the runs that could have made it, including runs
  between two polls, read version-gated from Hermes's board database; report mode, enforcement behind
  `integrity.enforce_attribution` (off). `gate_runs` now carries its project on every path and every reader is scoped.
  Migration 12.
- **MERGEGUARD** (ROL-05, DOC-04): the merge queue refuses a card whose completing reviewer run actually used the Lead's
  provider or family; the reviewer's paid status re-checked at dispatch; doctor's LSP row covers every profile.

The architect fixed each package's last open finding with a failing-first test (REVIEWLADDER's transient-failure freeze,
LEDGER's cross-pass ambiguity, MERGEGUARD's missing-model case) and resolved the merges. Full suite: 6248 passed, 2 skipped.
Register: 60 covered, 32 in progress, 8 partial, 3 not applicable, 0 not covered.

## Known gaps (tracked, not hidden)

- ~~`glm-5.3-thinking:free`'s context length is not declared in `config/models.yaml`... Confirm and
  set the real number before pinning this model in Phase 2.~~ -- stale, 2026-09-27: `glm-5.3-thinking:free`
  is no longer a model row in `config/models.yaml` at all. It was demoted (`role_class: lead_retired`,
  `pinned: false`) the same day this bullet was written (see "Lead moved off GLM, then to OpenAI via
  xKiro" above) and has since been removed from the file entirely, not just demoted; only a historical
  comment names it now. The lead role is pinned today to `xkiro/qwen/qwen3.8-max:free`, whose
  `context_length: 1050000` is declared and smoke-tested (`swarm models` reports
  `context=1050000 smoke=pass pinned`). The register's `ASES-MOD-02` (`in_progress`) note still describes
  the old `glm` gap; that note has drifted from the code and is not fixed by this pass (HK-GAPS owns only
  this file, not `spec/requirements.yaml`).
- The blueprint's Appendix B illustrative config (v1.2) names the UnoRouter Hermes secret as
  `OPENAI_API_KEY`. This machine's default installed Hermes `config.yaml` (`%LOCALAPPDATA%\hermes\config.yaml`,
  the base install, not any ASES role profile) still shows `key_env: HERMES_CUSTOM_UNOROUTER_API_KEY` for
  its `unorouter` provider block, so the illustrative name and a real one still differ on this install,
  exactly as this note originally said. Partly true, updated 2026-09-27: the rest of the original note is
  now stale. `config/models.yaml` no longer uses `HERMES_CUSTOM_UNOROUTER_API_KEY`, or UnoRouter, at all --
  it was removed from the file entirely on 2026-09-19 (see "Lead moved off GLM, then to OpenAI via xKiro"
  above); no `unorouter` provider row remains there, only a historical comment. The claim that
  `OPENAI_API_KEY` was "`lead`'s new home" is stale too: `lead` moved again the same day, to `xkiro`
  (`key_env: XKIRO_API_KEY`), which is what `config/models.yaml` still pins `lead` to today
  (`qwen/qwen3.8-max:free`); no `openai` provider row exists in the file either any more. Nothing in
  `config/models.yaml` today shares Appendix B's illustrative `OPENAI_API_KEY` name.
- ~~No Hermes profiles exist yet~~ -- stale, fixed 2026-09-18: `lead`/`coder-1`/`reviewer` were created
  fresh (no `--clone-from`, ASES-ROL-10, `covered`) earlier in Phase 3; this bullet just never got
  removed when that happened. Left struck through instead of silently deleted so the drift is visible.
- ~~Gate P plan publication (ASES-ARC-09)... new v1.2 requirements with no code yet~~ -- also stale:
  `controller.publish_plan` exists and is wired into `cmd_approve` (ASES-ARC-09, `covered`).
  Pinned-worktree creation (ASES-GIT-16) is the one still genuinely open -- see below.
- **ASES-GIT-16 (`partial`)**: "ASES worktrees start from the exact local integration HEAD;
  worktree_sync is disabled or manual creation is used." `gates.py`/`mergeq.py`'s own throwaway
  worktrees already satisfy this (detached at an exact SHA). The WORK card's worktree, which Hermes itself
  creates on dispatch (`workspace: worktree`), was observed at the exact integration tip on both real
  dispatches of the 2026-09-19 run (232e12e, then 676628f after G1's merge). Partly true, updated
  2026-09-27: the bullet's own open question ("whether `worktree_sync` needs to be turned off explicitly...
  is still open") is now answered in code, built in Round 5 (after this run): `profiles._config_rows`
  (`src/ases/profiles.py:901-905`) sets `worktree_sync: false` on every profile whenever it isn't already,
  citing ASES-GIT-16 by name, and `profiles._check_profile` (`src/ases/profiles.py:1530-1533`) reports a
  live profile with it left on as a problem. What the register (`ASES-GIT-16`, still `partial`) correctly
  says remains open: this test repo still has no remote, so Hermes's default of syncing a worktree from a
  freshly fetched REMOTE tip that actually differs from local HEAD (the case the requirement guards
  against) has still never been exercised for real. Round 9 (2026-09-27): the second half of blueprint
  p169, "Phase 3 MUST verify the actual base commit before a worker starts", is not built anywhere in
  `src/ases`; ASES-GIT-01 was set back to `partial` for it. Round 10 (2026-09-27): built
  (`guards.check_card_base`, detection every pass plus a merge-queue refusal; see the round 10 section), so
  GIT-01 is covered again and GIT-16 is `in_progress`: the check has not yet run against a real dispatch.
- **Gaps the first real run exposed** (details in "The first real end-to-end run"), updated 2026-09-27
  against the current code: the reviewer profile still has `write_file` and `patch` (Hermes toolsets are
  per group, no per-tool deny; `ASES-ROL-05`, `partial`, unchanged). Partly true: the reviewer itself still
  has no direct read access to the controller's gate records, but the controller no longer merely trusts
  the coder's claim about them -- `review.gate_before_review` (`src/ases/review.py:62`) independently
  re-runs Gate 1 when a card enters review, though its own docstring says Hermes's gateway dispatcher can
  sometimes claim the card and start the reviewer before gate_before_review has run; when that race is
  lost, `review.check_branch_for_merge` (`src/ases/review.py:121`) is what actually catches it,
  re-checking scope and Gate 1 again, independently, at merge time. Both paths were seen firing for real
  on the 2026-09-19 run (`ASES-REV-05`, `partial`: the pass path is acceptance-proven, the send-back path
  has never fired for real, and the register note also records that when the dispatcher wins the race,
  the re-check happens at merge time rather than at review entry). Partly true: "integrity snapshots
  (ASES-GIT-12) are not wired" is now wrong
  for the primary checkout -- `guards.check_primary_checkout` (`src/ases/guards.py:117`) runs at the start
  of every controller pass and at run start, and halts the run on a violation, acceptance-proven 2026-09-22
  (`ASES-GIT-12`, `partial`). Still genuinely open: the other-worktree half
  (`guards.check_idle_worktrees`, `src/ases/guards.py:373`) only WARNs (round 9's IDLEWT fixed its documented
  re-dispatch false positive but kept it a warning, because a reviewer editing during review still looks the
  same to it), and neither half inspects anything outside a git worktree's own tracked state, so the bullet's own
  example -- `pip install` into Hermes's own venv -- would still go undetected today; only the Docker
  sandbox (`ASES-SEC-03`, `in_progress`, see below) would close that.
- Credentials, as of 2026-09-19: `lead` and `coder-1` are on xKiro (own key each), `reviewer` is on
  OpenRouter, and UnoRouter is removed entirely (an explicit decision). The first complete real end-to-end
  run (acceptance test 22.2's shape: two tasks, both merge cards done) finished on 2026-09-19 and is
  written up in "The first real end-to-end run" above, including what it did NOT exercise.
- Updated again after round 9 (2026-09-27): GATESANDBOX wired every gate caller (Gate 1 in both review paths,
  Gate 3, the post-merge re-run, Gates 4/5) through `gates.resolve_runner`, so gates CAN run in the Docker
  sandbox, with a self-contained checkout and task-scoped network. The switch (`sandbox: enabled` in
  `config/swarm.yaml`) stays off by default because Docker has never run for real on this machine, so every
  gate still runs on the host in practice, now with round 8's scrubbed environment and round 9's hardened git.
  Stale, corrected 2026-09-28 (round 16, commit 409dc86, "Sandbox on: workers and gates run in Docker"): the
  switch is no longer off by default. Round 15 (SANDBOXIMG and WORKERGIT, commits 6954e88 and c80827f; the
  architect's `ases-sandbox:py311-2` rebuild, commit 5aa88d9) proved the sandboxed gate, the key and `.env`
  checks, default-deny network, and a worker's own git commit against real Docker (`scripts/sandbox_live_check.py`,
  `scripts/workergit_live_check.py`; full record `docs/stage-b-2026-09-28.md`), and `config/swarm.yaml`'s
  `sandbox.enabled` is now `true`, applied to the `coder-1` profile's terminal block and to the controller's own
  gate calls by `swarm init --global --apply --yes` (12 changes, 0 failed) and confirmed `HEALTHY` by `swarm
  doctor --repo`. No real Hermes worker has yet run inside Docker: every PASS above is a standalone probe or a
  throwaway worktree, never an actual dispatched card, so this remains the S1 finish after the 00:00 UTC
  OpenRouter quota reset (see "Rounds 15 and 16 and stage B" above).
  One accepted limitation: with the sandbox on, a post-merge Gate 3 re-run that cannot start (Docker down)
  keeps the merge and records a `sandbox_infrastructure_error` event rather than reverting. The text below is
  the pre-round-9 state, kept for the record. Partly true, updated 2026-09-27 (was: "Gate 1/3 run directly on
  the host, not inside Docker, Phase 5 requirement, not built"): `gates.run_gate` (`src/ases/gates.py:58`) has
  since grown an injectable
  `runner` parameter (`ASES-QG-04`, `ASES-SEC-03`) that a sandboxed runner could plug into, and
  `finalgates.run_gate4`/`run_gate5` (`src/ases/finalgates.py:648`, `:708`) already forward it. But no real call
  site actually supplies one: `review.py`'s Gate 1 re-check (`src/ases/review.py:382`), `mergeq.py`'s
  pre-merge Gate 3 (`src/ases/mergeq.py:254`), `controller.py`'s post-merge Gate 3
  (`src/ases/controller.py:1011`) and `controller.process_finalize`'s own call into
  `finalgates.finalize` (`src/ases/controller.py:1941`) all call it with the default `runner=None`. So
  every gate that actually runs today -- 1, 3, 4 and 5 alike -- still runs directly on the host, matching
  the register's own `ASES-SEC-03` (`in_progress`) note: "the controller's own gate runs do not use
  docker_run_argv yet". `sandbox.py` (`src/ases/sandbox.py`) exists and builds a real, tested
  `docker run` argv (`docker_run_argv`), but by its own docstring "nothing here starts Docker or pulls an
  image" -- wiring a real runner into any of the four call sites above is the Phase 5 work still left.
- **ASES-REV-01 (`partial`)**, added 2026-09-27, corrected the same day by the architect: diff review by the
  independent Reviewer profile happened for real on 2026-09-19 and is covered above ("Credentials, as of
  2026-09-19"). Plan critique IS built: `src/ases/critic.py` is Gate P (a one-shot, toolless call to the
  reviewer profile behind `swarm critique`, its verdict validated as JSON, a malformed one repaired once then
  blocked for the user), unit-tested in `tests/unit/test_critic.py` and acceptance-tested at zero quota in
  `tests/acceptance/test_22_14_plan_rejection.py`. The register's note "Plan critique (the critic role in Gate
  P) is not built" predates it and is stale (round 9's register hygiene package corrects it). What is still
  open is the live half: the critic has never run against a real plan with a real reviewer model.
- ~~**The controller's other git calls** (round 8 sweep): every controller git subprocess ran with the full
  environment in a repository a local-backend worker can write hooks and config into, and the Gate 3
  secret-scan diff had no `--no-ext-diff --no-textconv`.~~ -- fixed in round 9 (GITHARDEN, 2026-09-27): every
  controller git call goes through `src/ases/gitexec.py` (hooks and `core.fsmonitor` disabled, the credential
  scrub, `DIFF_SAFETY` on diff text), and a completeness test fails on any new bare git call. Residual, by
  design: filter and merge drivers have worker-chosen names no single flag disables (they run, but see no
  credential), and a worker's own shell on the local backend runs as the operator's user; only the Docker
  sandbox closes that.
- ~~**Found in round 9, not fixed yet**: the unpinned `allow_gate_config_changes` marker; the `daily_reserve_percent`
  default read as 10 in one place and 0 in another; a paused project's wall clock; project-scoped branch names;
  `profiles.residual_risks()` and `models.record_smoke_test` with no caller.~~ -- resolved in round 10
  (2026-09-27): the marker is pinned (GATEPIN); one reserve default of 10 (BUDGETFIX); the paused clock was NOT a
  bug (the bound itself keeps counting and `swarm resume --extend-minutes` handles a passed deadline; the real
  mismatch was the report freezing a STOPPED project's clock, now fixed); the residual risks are shown and the smoke
  test has a command (CALLERS). Branch names were deliberately left as they are: the blueprint assigns them to
  Hermes as `swarm/<plan-key>-<slug>`, and each project has its own repository (its own `workspace_root`), so two
  projects can only collide on a branch by sharing a repository, which ASES does not support.
- **ASES-CFG-05 (`partial`)**: a Hermes-gateway-dispatched worker is spawned by Hermes's own gateway, a process
  ASES never touches, so no ASES-side scrub reaches it (ASES-ARC-01). If a provider key leaks there, it was
  exported into the shell that started the gateway, which blueprint p213 forbids.

## Running things

```
cd C:\Users\masoo\ases
.venv\Scripts\swarm.exe doctor
.venv\Scripts\swarm.exe models
.venv\Scripts\python.exe -m pytest -q --ignore=tests/integration/test_doctor_real_hermes.py
.venv\Scripts\python.exe spec\check_requirements.py --check
```

Updated 2026-09-27: the bare `pytest -q` line above used to be wrong on any machine with a real
`hermes.exe` on PATH. `tests/integration/test_doctor_real_hermes.py` only skips itself when
`shutil.which("hermes") is None`; on a machine where Hermes is actually installed, a bare `pytest -q`
run collects it and calls the real `hermes.hermes_version()` / `run_doctor()` / `gateway_status()` for
real, which every round of this project's rules forbids. The `--ignore` above is required whenever a
real `hermes` binary is on PATH; the test itself is exactly what `swarm doctor` above documents, so
running it here is redundant with `swarm doctor` anyway, never a loss of coverage. `check_requirements.py
--check` above needs no `--docx` flag: this file's "Known gaps" doesn't own `spec/check_requirements.py`,
but `--check` with no flags already passes on this machine (`OK: 103 requirement IDs in sync`) once the
default docx path points at its current location (package HK-PATH, landing in parallel).
