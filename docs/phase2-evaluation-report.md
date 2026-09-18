# Phase 2 evaluation report

Date: 2026-09-18. Exit criterion (blueprint section 16): "Evaluation report accepted by the user."
Evals are E1 (requirements -> assumptions), E9 (tool use), E10 (review -> seeded bug), from Appendix D.
Raw transcripts and `--usage-file` output are under `C:\Users\masoo\ases-workspaces\_eval\`.

## Headline finding

**The currently-configured Lead model, `glm-5.3-thinking:free` via UnoRouter, fails the E9 tool-use
smoke test.** Confirmed twice, cleanly: `tool_call_count: 0` in both session transcripts (sessions
`20260918_150057_55158c` and `20260918_152259_c9aac4`), the second run with an explicit absolute path
and an instruction not to rely on cwd. Both times it "thought" for 2-4 minutes, produced nothing or an
empty turn, then fabricated an answer ("0") with no tool ever invoked. Per ASES-MOD-04, a model that
fails its smoke test is not fit to pin. **Recommendation: do not pin glm-5.3-thinking:free for Lead.**

A same-provider, same-endpoint alternative works cleanly: **`qwen3.8-27b:free` via UnoRouter passes all
three evals** and needs no new setup (no new key, no new provider). One quirk: it 400s with the default
reasoning effort ("invalid Qwen3.8 reasoning_effort") and needs `--reasoning medium` pinned explicitly.

## Results

| Model | Role | E1 | E9 (tool use) | E10 (bug review) | Recommendation |
|---|---|---|---|---|---|
| `glm-5.3-thinking:free` (UnoRouter) | Lead (current config) | not run | **FAIL** x2 (tool_call_count=0) | not run | Do not pin |
| `qwen3.8-27b:free` (UnoRouter) | Lead (proposed) | Pass -- thorough, security-conscious plan | **Pass** -- correct answer, 2 api_calls | Pass -- exact bug, worked example, scoped when it manifests | **Pin this** |
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

## Requested decision

Accept `qwen3.8-27b:free` (UnoRouter, `--reasoning medium`) as Lead and `cohere/north-mini-code:free`
(OpenRouter) as Reviewer, superseding the glm-5.3-thinking:free default? If yes, `config/models.yaml`
gets updated and both get marked `pinned: true`, closing out Phase 2.
