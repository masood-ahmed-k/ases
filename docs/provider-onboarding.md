# Adding a provider: a repeatable checklist (ASES-CAP-06)

The blueprint, section 5.4, p136 (quoted here because the register's summary trims it): "Capacity SHOULD
come from provider diversity: use additional free-tier providers that the user is legitimately entitled to
use, such as native Hermes providers or a compatible endpoint. Do not assume that extra API keys for the
same account increase a provider quota. Each addition goes through discovery, smoke test, data-policy check
and evaluation. [ASES-CAP-06]"

`spec/requirements.yaml`'s ASES-CAP-06 row (status `in_progress`) says exactly what was missing before this
file existed: "there's no repeatable artifact (checklist/template) that would make discovery+evaluation hold
for the *next* provider addition -- only a one-time real instance exists so far." This document is that
artifact. It is a checklist, not a new explanation of the four steps -- each one below names the real ASES
command, config field or check that already does the work, so following it is mechanical. The one real
instance it is distilled from is `docs/architecture.md`'s dated log: search it for "Lead moved off GLM,
then to OpenAI via xKiro", "coder-1 moved to xKiro too", and "Coding candidates evaluated" for the worked
example of every step below actually happening once.

## Before you start: the same-account-keys warning

Blueprint p209 (ASES-CFG-02): "Prefer one key per provider and several providers. Key pools add no capacity
on the three configured providers: OpenRouter limits per account, UnoRouter per user, and OpenCode Free has
no key." (UnoRouter was dropped entirely on 2026-09-19, an explicit user decision recorded in
`docs/architecture.md`'s D3 entry and in `config/models.yaml`'s own comment at the top of `providers:`;
today's three configured providers are `openrouter`, `xkiro` and `opencode_free`, but the underlying rule --
one account's key pool never multiplies that account's quota -- applies to whichever providers are actually
configured.)

Blueprint p210 (ASES-CFG-03): "Do not create or rotate accounts to get around provider limits or abuse
controls."

Both are account-management rules about what a human does outside ASES, which ASES cannot see or enforce
directly (both rows say so in `spec/requirements.yaml`). The one thing ASES CAN check from its own config is
`swarm doctor`'s `key_pooling` row (`src/ases/doctor.py`'s `_check_key_pooling`, ASES-CFG-02/ASES-CFG-03):
it WARNs, never fails, when two DIFFERENT `providers.<name>.key_env` entries in `config/models.yaml` name
the same environment variable -- a real, if imperfect, proxy for "the same account under two provider
names." One provider used by several profiles is normal and is not what it flags. Run `swarm doctor` after
adding a provider and read that row; it is the only automated check this checklist has for the warning
above; the account-creation half is enforced only by a person reading this document and choosing not to.

## Step 1: Discovery

Find the provider's own current documentation, never a summary of it: the API base URL (or, for a provider
Hermes already knows natively, its Hermes provider id), the auth scheme, published rate limits (requests
per minute, a daily cap, any per-model pacing), and the exact model catalog with model IDs spelled the way
the provider spells them (a router's IDs are usually vendor-prefixed, e.g. xKiro's
`openai/gpt-5.6-terra`, confirmed against `https://docs.xkiro.com/`, not OpenAI's own bare name).

Record what you found in `config/models.yaml`'s `providers.<name>` block:

- `type`: `openrouter`, `openai_compatible` or `hermes_provider` (`docs/operations.md` section 7.2).
- `base_url` (`openai_compatible`) or `provider_id` (`hermes_provider`; read by
  `src/ases/profiles.py`'s `_provider_identity`, e.g. `opencode_free`'s `provider_id: opencode-free`).
- `key_env`: the NAME of the environment variable the key will live in (in the Hermes profile's own
  `.env`, never in this repository -- ASES-CFG-01). `null` only for a provider served anonymously.
- `limits`: `rpm` or `per_model_rpm` (read by `policy.estimate_calendar_minutes` and `evals.py` for
  calendar-time pacing, ASES-CAP-04), and `per_day` or the `per_day_default`/`per_day_after_credits` pair
  (read by `ledger.py`'s affordability check, ASES-CAP-02/03). Leave `limits: {}` rather than guess when
  nothing is published; that means "never parked for lack of a known cap," which is the honest default.
- `quota_endpoint`: an API path the provider exposes to read remaining quota, or `null`. (Grep note: this
  field and `require_parameters` are read by nothing in `src/ases/` today -- they are documentation the
  same way a code comment is, not enforced. Fill them in anyway; they are exactly what the next person
  doing this checklist needs.)
- `verified_on`: the ISO date (quoted) you actually read the page, per ASES-VER-01 (blueprint p128):
  "These numbers were verified from current provider documentation on 18 September 2026. They can change.
  swarm doctor MUST display the value it is using, the source URL and the checked date." `swarm doctor`'s
  `limits_displayed` row shows the date today; there is no separate `source` config field yet (`verified_on`
  is the only one `config.py`/`doctor.py` actually read), so write the source URL as a plain YAML comment
  next to `verified_on`, the same way every existing provider block in this file already does -- do not
  invent a field name for it.

## Step 2: Smoke test

Blueprint p125 (ASES-MOD-04): "Before first use, run one smoke test per model through the real Hermes path:
a tiny tool-calling task with a structured result. Record the result and the latency."

Dispatch one small real tool-calling task on the new model, through the Hermes profile that will use it,
and confirm it actually made a real tool call (`docs/architecture.md`'s dated entries are the pattern to
follow, e.g. its record of a real terminal tool call on the real profile, recorded as a pass). Then record it:
`ases.models.record_smoke_test(conn, provider, model, "pass"|"fail", detail)` (`src/ases/models.py`,
unit-tested in `tests/unit/test_models.py`). Grep note, stated plainly because this checklist promises to
verify every command it names: `record_smoke_test` has no caller anywhere in `src/`, so there is no `swarm`
subcommand that runs or records this step for you today -- open the project's own database the way
`cli.py`'s `cmd_models` does (`_load_project` then `_open_conn`) and call it by hand, or from a short
script. This is a real, current gap worth a follow-up (a `swarm smoke-test` command), not something this
docs-only package can fix.

Where the result then shows up, both grep-verified:

- `swarm models` -- the `smoke=` column (`cli.py`'s `cmd_models`).
- `swarm doctor` -- one `smoke_test[provider/model]` row per declared model, and one
  `context_length[provider/model]` row that WARNs until `context_length` is set and at least
  `models.MINIMUM_CONTEXT_LENGTH` (64,000, Hermes's own floor, ASES-MOD-02) -- `src/ases/doctor.py`'s
  `_check_model_registry`.

## Step 3: Data-policy check

Blueprint section 21.2 (ASES-PRV-04, quoted in `src/ases/policy.py` and in `config/models.yaml`'s own
header comment): "private/confidential projects require an EXPLICITLY VERIFIED provider data policy."

Read the provider's own privacy or data-retention page (never assume, never copy another provider's
answer) and set `providers.<name>.data_policy` in `config/models.yaml`. `policy.check_data_class`
(`src/ases/policy.py`) only treats `no_training`, `local_only` or `zero_data_retention` as safe for a
`private` project's provider, and only `local_only` as safe for `confidential`; anything else -- including
a descriptive label like `some_free_endpoints_train` or `router_ztr_upstream_varies` (xKiro's real one: its
own zero-retention promise covers xKiro's own handling, not the 16 different upstream vendors it routes to,
which is why it is not marked safe for private) -- correctly refuses the stricter data classes. `swarm
doctor`'s `key_pooling` check is unrelated to this; the enforcement point is `check_data_class` itself,
called from `cli.py`'s `cmd_approve` (Gate P) and from `controller.process_budget_gate`.

If `config/swarm.yaml`'s `project.data_class` is `private` or `confidential` and this provider will be
pinned to a role that project uses, also set `data_policy_verified_at` (the ISO date, quoted, you actually
read the policy) and `data_policy_source` (a URL or short note). Both are validated for shape at load time
(`config.load_models_config`) but `check_data_class` is what actually refuses a provider with a compatible
`data_policy` string and no `data_policy_verified_at` -- however safe the string looks, it is treated as
unverified. For today's `public` project neither field is required, but there is no reason not to record
them the moment you have actually checked the page; it is free now and saves the work later if the project's
data class is ever tightened.

## Step 4: Evaluation

The same blueprint sentence as above names this the fourth step. It is the evaluation harness,
`src/ases/evals.py` and `src/ases/evalkit/`:

- `swarm eval list` -- the tasks E1 to E11 and what one run of each costs (`src/ases/evalkit/tasks.py`;
  `PHASE2_IDS` there is the short `E1, E9, E10` list this project's Phase 2 actually runs).
- `swarm eval run --tasks E1,E9,E10 --candidates <provider>/<model> [--profile PROFILE]` -- a dry run by
  default: it prints the plan and the cost and calls nothing.
- Add `--spend-quota` only once the user has actually approved spending real provider requests on this run
  -- `docs/operations.md` already flags `swarm eval ...` as "Needs your approval: it spends real provider
  requests," and this is a real, user-approved spend (ASES-DOC-04), not a default action.
- `swarm eval report <run_dir>` reads a finished run's results; `swarm eval compare <candidate_run>
  <pinned_run> [--tolerance F]` checks the new candidate against whatever is currently pinned for the role
  (exit code 2 on a regression) before deciding to actually switch.

Treat one run as evidence, not a verdict -- `docs/architecture.md`'s own comparison of three coder
candidates on one easy task says it plainly: "One run of an easy task cannot rank models." Run more than
one task, and prefer a harder one, before pinning a role to the result.

## After the four steps: promoting the result

The requirement's four steps stop at "evaluate it." Actually putting a provider to use needs one more,
real step this checklist should not skip:

1. In `config/models.yaml`, change the winning row's `role_class` to the real role (`lead`, `coder` or
   `reviewer`, not a `_candidate`/`_unfunded` label) and set `pinned: true` on that one row for the role;
   unpin (`pinned: false`) whatever row used to hold it, keeping its history in a comment rather than
   deleting it (see how every retired row in this file already does this).
2. Run `swarm init --apply --yes [--reuse-credentials-from OLD_PROFILE]` so the role's Hermes profile is
   actually repointed at the new provider. This is real, grep-verified machinery, not a manual edit:
   `src/ases/profiles.py` reads the newly-pinned row via `policy.profile_provider` (ASES-MOD-06 in its own
   comments) and diffs/writes the profile's `model.provider`, and for an `openai_compatible` router also
   `providers.<name>.base_url` and `providers.<name>.key_env`. `--reuse-credentials-from` copies only the
   one environment variable the OLD profile already had for that role, never any other credential.
3. `swarm doctor` again, to confirm `smoke_test[...]`, `context_length[...]` and `key_pooling` all read the
   way you expect before dispatching real work against the new provider.

## The stop condition (ASES-DOC-04)

`CLAUDE.md`'s restatement of blueprint section 16, verbatim: Claude Code MUST stop and ask the user before
any action that (1) spends money, including a one-time credit purchase; (2) deletes user data; (3)
overwrites an existing repository or Hermes configuration; (4) downloads and installs new software; (5)
sends code from a private project to a provider outside its allow-list; (6) needs a secret that was not
already configured.

A provider addition usually touches three of the six directly: category 6 the moment a brand-new API key
is needed; category 3 the moment `swarm init --apply --yes` is actually run (step 2 above rewrites a real
Hermes profile file on disk); and category 1 if the new provider is used past a free tier or
`credits_purchased` is ever flipped to `true` (ASES-CAP-05). None of the four discovery/smoke-test/
data-policy/evaluation steps above need that approval by themselves -- reading documentation, running a
free smoke test, reading a privacy page and a dry-run `swarm eval` all stay inside what can be done without
asking first -- but do not chain straight from "evaluation looked good" into "promote the result" without
stopping at the boundary above.

And, restated because it is the one this checklist exists to guard against: account creation or rotation to
get around a provider's limits or abuse controls (ASES-CFG-03) is never something ASES does, and never
something to do on the user's behalf even when asked to move faster -- the user creates the account, the
user hands over the key.

## Copy-paste `config/models.yaml` stub, every field commented

```yaml
providers:
  <name>:                           # short, lowercase key; models[].provider below must match it exactly
    type: openai_compatible         # "openrouter" (the built-in OpenRouter service) | "openai_compatible"
                                     # (any OpenAI/Anthropic-compatible endpoint, including a router such as
                                     # xKiro) | "hermes_provider" (a provider Hermes already calls natively,
                                     # e.g. opencode_free) -- docs/operations.md section 7.2
    base_url: https://api.<name>.com/v1   # openai_compatible only
    # provider_id: <name>-native    # hermes_provider only; the id Hermes's own provider registry uses
    key_env: <NAME>_API_KEY         # NAME of the env var holding the key (kept in the Hermes profile's own
                                     # .env, never here -- ASES-CFG-01); null for a provider served with no
                                     # key at all
    limits:                         # {} (leave empty) means "no known cap yet" -- never guess one
      rpm: 20                       # requests per minute, account-wide, if published
      # per_model_rpm: 1            # use instead of rpm only if the provider paces per model, not per
                                     # account (policy.estimate_calendar_minutes prefers this key)
      per_day: 50                   # a flat daily request cap, if published
      # per_day_default: 50         # use this pair instead of per_day when the cap changes after a paid
      # per_day_after_credits: 1000 # credit purchase (see credits_purchased below); ledger.py picks
                                     # whichever of the two applies
    credits_purchased: false        # flip to true ONLY after the user has actually bought credits
                                     # (ASES-CAP-05); this is a stop-condition category-1 action
    quota_endpoint: /api/v1/key     # an API path this provider exposes to read remaining quota, or null --
                                     # documentation today, read by no code in src/ases/
    require_parameters: true        # OpenRouter-specific switch; also documentation only today
    data_policy: some_free_endpoints_train   # what step 3 above actually found; never no_training or
                                     # zero_data_retention unless the provider's OWN policy page says so
    # data_policy_verified_at: "2026-09-19"  # ISO date, quoted -- set only once a human has actually read
    # data_policy_source: "<url or note>"    # the page named here (ASES-PRV-04/ASES-VER-01). Leave both
                                     # commented out until that has really happened: policy.check_data_class
                                     # refuses private/confidential for a provider with a compatible
                                     # data_policy but no data_policy_verified_at, however good it looks.
    verified_on: "2026-09-19"       # ISO date, quoted, you last checked base_url/limits/data_policy against
                                     # the provider's own current pages (ASES-VER-01) -- write the source URL
                                     # as a plain comment next to whichever line it supports
    # status: blocked                # optional free-text note, read by no code -- see opencode_free's real
                                     # use of this in this same file for the pattern

models:
  - provider: <name>                 # must match a providers.<key> above exactly
    model: "<vendor>/<model-id>"     # exactly as the provider's own live catalog spells it -- never assumed
    context_length: 128000           # the provider's declared context; null means undeclared, and swarm
                                      # doctor WARNs on every model below 64000 (models.MINIMUM_CONTEXT_LENGTH,
                                      # ASES-MOD-02) until this is set and confirmed
    tool_calling: true                # true only once step 2's real tool call has actually worked
    role_class: coder_candidate       # lead | coder | reviewer once pinned to a role; otherwise a
                                       # *_candidate or *_unfunded label so it is visible without being live
    pinned: false                     # true for exactly one row per role: the one actually in use
    # data_policy: no_training        # only if this ONE model's policy differs from its provider's default
                                       # above (e.g. one specific routed model, checked end-to-end, is
                                       # safer or less safe than the provider-level default)
```
