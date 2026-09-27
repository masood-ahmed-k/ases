# Round 11 package MOD02: an under-declared model is rejected before any card starts (read `r10_rules.md` first)

Worktree `C:\Users\masoo\ases-wt\mod02`, branch `r11/mod02`, cut from master after round 10 (`c7e399c`). Every rule in
`r10_rules.md` applies unchanged (zero quota, Write/Edit only, no git stash, own `--basetemp`, `gitexec` for git). Baseline:
5757 passed, 2 skipped, 0 failed.

## Requirements (quoted from blueprint.txt)
- ASES-MOD-02 (Appendix F): "Every model used through a custom endpoint has a declared context length of at least 64K".
- Acceptance 22.4 (p406): "Register a model declared at 16K: the controller must reject it before any card starts. Register one
  with no declared context on a custom endpoint: rejected as unknown. Declare 64K or more and repeat: accepted."
- Hermes fact (table under p121): "Hermes rejects any model with less than 64,000 tokens of context ... Custom
  OpenAI-compatible endpoints often cannot report their context length, so Hermes relies on model.context_length, a
  per-model entry under custom_providers, or probing."
- p122: "At startup, query each provider's model list (/v1/models on OpenAI-compatible endpoints) and merge it with
  config/models.yaml, which carries the facts an endpoint does not report: context length, data policy, model family."

## Where things stand
`models.MINIMUM_CONTEXT_LENGTH = 64_000` and `ModelRecord.context_declared_and_sufficient` exist (`src/ases/models.py`), and
`swarm doctor` WARNs on an under-declared model (`doctor.py` around line 420). Nothing REJECTS one: `swarm approve` creates
cards and `swarm run` starts regardless. The register's MOD-02 note says exactly this.

## Build
1. ONE decision function in `models.py`: for a model and its provider entry, accepted, rejected as too small (declared below
   the minimum), or rejected as unknown (no declared context on a custom, OpenAI-compatible endpoint; read how
   `config/models.yaml` marks a provider's type). Decide what a native Hermes provider with no declared context means, justify
   it from the blueprint text above in the docstring (the blueprint's "rejected as unknown" is about custom endpoints), and be
   consistent everywhere.
2. Enforce it "before any card starts", at every door: `swarm approve` refuses before any card exists if a role the plan uses
   resolves to a rejected model (find how a role maps to its pinned model: `project.roles`, `config/models.yaml`'s
   `role_class`/`pinned`, `profiles.py`'s desired state); `swarm run`'s pre-flight refuses the same way (config can change after
   approval); and any place the controller picks a DIFFERENT model at run time (for example `recovery.next_model` switching model
   after capability failures) must never pick a rejected one. Refusal messages name the model, the declared value and the
   minimum, and exit non-zero like the other pre-flight refusals.
3. `swarm doctor`: a pinned model that the controller would reject is now a FAIL row (the run cannot start); an unpinned candidate
   stays a WARN. Keep the existing row names so nothing downstream breaks.
4. Tests on fakes: the decision function's cases; approve refuses with no card created; run pre-flight refuses; the model switch
   skips a rejected candidate; doctor FAIL vs WARN. And a NEW acceptance file `tests/acceptance/test_22_4_context.py` that plays
   p406's three steps exactly (16K rejected before any card, undeclared on a custom endpoint rejected as unknown, 64K accepted and
   cards created), in the style of the other 22.x files (read `tests/acceptance/conftest.py`, `world_factory` with its own
   `models_config`; do not edit conftest). Before/after proof: the 16K step lets cards be created on the old code.

## Files you own
`src/ases/models.py`, the approve and run pre-flight code in `src/ases/cli.py`, the model-switch code in `src/ases/recovery.py`
(and any other run-time model picker you find), the context rows in `src/ases/doctor.py`, and tests. Not `spec/requirements.yaml`
or `docs/` (the architect updates those).
