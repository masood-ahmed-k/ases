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
  false`) are new v1.2 requirements with no code yet -- they land with Phase 3/4.

## Running things

```
cd C:\Users\masoo\ases
.venv\Scripts\swarm.exe doctor
.venv\Scripts\swarm.exe models
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe spec\check_requirements.py --check
```
