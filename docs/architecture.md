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
- Provider posture: UnoRouter only until the user adds an OpenRouter key (their call, tracked in
  `config/models.yaml`'s `credits_purchased` / provider list, not hardcoded anywhere).

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
| `cli.py` additions | `swarm plan` (invokes `lead`), `swarm approve` (Gate 0 + Gate P publish + budget check + card creation), `swarm run` (bounded loop, reconciles on start), `swarm stop`/`resume` (kill switch) | - |

Real Hermes profiles `lead`/`coder-1`/`reviewer` created fresh (no `--clone-from`, per ASES-ROL-10),
toolsets restricted (reviewer has no terminal/code_execution/browser, kanban enabled for verdicts only),
models pinned per the Phase 2 report. Board `ases-phase3` + project `p_36370687` bound to
`C:\Users\masoo\ases-workspaces\test-repo-phase3` (throwaway, for acceptance test 22.2 only).

## A real finding, not a hypothetical

`policy.check_data_class` enforces section 21.2 for real: **neither UnoRouter nor OpenRouter's
currently declared data policy qualifies as safe for `data_class: private`** (both say upstream
providers or some endpoints may train on inputs). Gate P will correctly *refuse* to approve any plan
under `private` until a provider with a confirmed no-training/local policy is added. This project's own
`config/swarm.yaml` is set to `public` for exactly this reason -- it's throwaway test scaffold content,
not real code, so that's an honest, deliberate choice, not a workaround. A real future project MUST set
its own data class deliberately and will hit this same refusal under `private` until that gap is closed
(most likely by adding a provider with `data_collection: deny` + `zdr: true` verified, or a local model).

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
  name and the real one differ on this install.
- No Hermes profiles (`lead`/`coder`/`reviewer`) exist yet. Per the blueprint's phase table that's
  Phase 3 work, and per v1.2's revision, they should be created **fresh** by default (no
  `--clone-from`) rather than cloned, with any deliberate clone's memory/credentials reviewed before
  use (ASES-ROL-10).
- Gate P plan publication (ASES-ARC-09) and pinned-worktree creation (ASES-GIT-16, `worktree_sync:
  false`) are new v1.2 requirements with no code yet.
- The real end-to-end run (acceptance test 22.2) hasn't executed yet -- waiting on the UnoRouter/
  OpenRouter credentials being copied into the `lead`/`coder-1`/`reviewer` profiles' own `.env` files
  (they don't inherit the default profile's, by design). Everything upstream of real dispatch (plan
  schema, card creation, gate running, merge queue) is unit-tested against real git repos and passes;
  only the live multi-agent dispatch is unproven so far.
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
