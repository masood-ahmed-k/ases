# Phase 2 evaluation report

Date: 2026-09-18. Exit criterion (blueprint section 16): "Evaluation report accepted by the user."
Evals are E1 (requirements -> assumptions), E9 (tool use), E10 (review -> seeded bug), from Appendix D.
Raw transcripts and `--usage-file` output are under `C:\Users\masoo\ases-workspaces\_eval\`.

## Headline finding (revised -- GLM fixed, see below)

The currently-configured Lead model, `glm-5.3-thinking:free` via UnoRouter, initially failed the E9
tool-use smoke test twice, cleanly: `tool_call_count: 0`, the model "thinking" for 2-4 minutes and then
fabricating an answer with no tool ever invoked. Per the user's request this was investigated further
rather than dropped. **Root cause found and fixed**: the model needs BOTH `reasoning_effort: low` AND an
explicit "you must verify with tools, never guess" instruction, or it narrates an intent to act
("I should use ls to...") and stops without ever calling anything. Isolation-tested: each change alone
still fails (sessions `20260918_174528_85c275` and `20260918_174922_472f95`); both together pass
reliably (session `20260918_174223_9af615` and the full E1/E9/E10 re-run below).
**Recommendation: pin glm-5.3-thinking:free for Lead, with this exact recipe encoded into its profile
config and SOUL.md -- not left as a CLI flag someone forgets.**

`qwen3.8-27b:free` via UnoRouter also passes all three evals cleanly with no special prompt needed
(just `--reasoning medium`, or it 400s). Kept in `config/models.yaml` as `role_class:
lead_alternative`, unpinned -- a documented fallback if GLM's recipe ever proves unreliable at scale.

## Results

| Model | Role | E1 | E9 (tool use) | E10 (bug review) | Recommendation |
|---|---|---|---|---|---|
| `glm-5.3-thinking:free` (UnoRouter), default settings | Lead, as originally configured | not run | **FAIL** x2 (tool_call_count=0) | not run | Needs the recipe below |
| `glm-5.3-thinking:free` (UnoRouter), `reasoning_effort: low` + verify-don't-guess instruction | Lead (proposed, final) | Pass -- clear assumptions + plan, session invalidation noted | **Pass** -- tool_call_count=1, correct answer, 2 api_calls | Pass -- exact bug, worked example, correct fix | **Pin this** |
| `qwen3.8-27b:free` (UnoRouter), `reasoning_effort: medium`, no special prompt | Lead alternative | Pass -- thorough, security-conscious plan | **Pass** -- correct answer, 2 api_calls | Pass -- exact bug, worked example, scoped when it manifests | Keep as documented fallback |
| `cohere/north-mini-code:free` (OpenRouter) | Reviewer (proposed) | Pass -- clear assumptions + plan | **Pass** -- correct answer, 2 api_calls | Pass -- exact bug, worked example | **Pin this** |

Reviewer diversity (ASES-ROL-05): Cohere North Mini Code is a different model family on a different
provider (OpenRouter) than Qwen/UnoRouter. Satisfied.

Cost, per `--usage-file`: each eval ran 27K-62K input tokens (Hermes's own tool schemas and system
prompt dominate a trivial task) at 2-4 api_calls. All three real models tested are free-tier ($0
estimated cost); OpenRouter's free daily cap (50/day, no credits purchased) is the real constraint, not
per-call cost. Total requests spent this session: 3 on UnoRouter (rate-limited ~1/min, hence the long
wall-clock time), 4 on OpenRouter.

## Known gaps before these are truly "pinned" (config/models.yaml)

- `qwen3.8-27b:free`'s context length is still undeclared (ASES-MOD-02) -- UnoRouter's `/v1/models`
  wasn't queried for it in this pass. Needs confirming >=65536 before Phase 3 relies on it.
- `qwen3.8-27b:free` needs `--reasoning medium` (or similar) pinned in its config; the default causes a
  hard 400 error. This must be encoded in `hermes.py`'s wrapper for Phase 3, not left as tribal
  knowledge.
- Neither candidate's OpenRouter/UnoRouter route has had its data policy independently verified beyond
  what's already in `config/models.yaml` (both still marked with the provider-level general policy, not
  a per-model check).

## Environment bugs found and worked around (matters for Phase 3's hermes.py)

1. **Working directory is not reliably inherited.** Neither a shell `cd` before invoking `hermes.exe`,
   nor Hermes's own `--in DIR` flag, reliably landed the terminal tool in the intended directory in this
   environment -- two different runs landed in two different *wrong* directories (the user's home, and
   an unrelated old working folder) before the fix was found. `TERMINAL_CWD` (the env var Hermes's own
   source names as the usual culprit, per `hermes_cli/main.py`) was confirmed empty, so it wasn't the
   cause here; the actual mechanism wasn't pinned down further given the cost of continuing to test it
   against real rate-limited quota. **Workaround that reliably worked: always give the model an explicit
   absolute path in the prompt/task text and tell it not to rely on cwd.** Phase 3's `hermes.py` wrapper
   and card-body templates MUST do this for every card -- never rely on `--in` or launch-directory alone
   for a worker to land in its worktree.
2. **`hermes auth add` (the interactive wizard) was unreliable.** Two attempts on two different profiles
   produced two different auth failures (`401 User not found`, then `401 Missing Authentication header`)
   before a credential actually worked. Editing the profile's `.env` file directly
   (`OPENROUTER_API_KEY=sk-or-...`) worked on the first try. **Recommendation: Phase 3's setup docs
   should prefer direct `.env` editing over the wizard**, and note that a valid OpenRouter key always
   starts with `sk-or-` (Hermes's own format validator) as a quick sanity check.
3. **Incident:** while checking that `.env` file, it was read with `cat` and the raw key value was
   printed into this session's transcript. The user was told immediately and advised to regenerate that
   key on OpenRouter's dashboard once testing is done. Going forward, file checks confirm
   presence/format only (e.g. `grep -c`), never dump full secret files.

## OpenCode Free: ruled out entirely

Tried `nemotron-3-ultra-free` via `opencode-free` for the Lead role (a strong candidate on paper: 1M
context, built for agent/coding orchestration). Got a hard, definitive server-side rejection:

> HTTP 403: "OpenCode's free tier can only be used from within OpenCode"

This is OpenCode Zen refusing any client that isn't their own product, independent of which model is
requested -- confirmed this is a blanket policy, not a per-model gate, so there's no value in retrying
other `opencode-free` models (`mimo-v2.5-free`, `ling-3.0-flash-fin-free`, `big-pickle`, etc.) expecting
a different outcome. **`opencode-free` is not currently usable through Hermes at all**, contradicting
blueprint section 5.3/17.4's assumption that it's just "keyless, no pool to build" -- keyless turned out
to also mean "blocked outside their own client." Marked `status: blocked` in `config/models.yaml`.

This leaves the two working providers (UnoRouter, OpenRouter) as the only real options for version 1.

## Decision: accepted, GLM pinned

`config/models.yaml` now pins `glm-5.3-thinking:free` (UnoRouter, `reasoning_effort: low` + the
verify-don't-guess instruction) as Lead and `cohere/north-mini-code:free` (OpenRouter) as Reviewer.
`qwen3.8-27b:free` stays in the registry, unpinned, as `role_class: lead_alternative`.

**Carries forward to Phase 3, not optional:** the `lead` profile's `config.yaml` must set
`agent.reasoning_effort: low`, and its `SOUL.md` must include the verify-don't-guess instruction
verbatim. Without both, this regresses to the original failure. `hermes.py`'s profile-creation helper
should assert both are present before letting a `lead`-role profile go live.
