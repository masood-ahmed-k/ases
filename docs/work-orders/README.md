# Work orders for the phase-completion build (2026-09-19)

These are the instructions each builder agent was given (or is ready to be given) while the remaining blueprint phases
were built ahead of their real testing. They are kept so a later session can see exactly which requirement IDs each
module was built against, re-dispatch a package that needs redoing, or dispatch the ones not yet started.

| File | Package | Status when parked |
| ---- | ------- | ------------------ |
| `r2_rules.md` | shared rules for every package | in use |
| `r2_wp_questions.md` | Q: `swarm questions` / `swarm answer` (ASES-REC-05) | dispatched |
| `r2_wp_report.md` | S: `swarm status` / `swarm report` (ASES-OBS-01) | dispatched |
| `r2_wp_recovery.md` | R: failure classification, retries, lineage (ASES-REC-01/02) | dispatched |
| `r2_wp_bounds.md` | B: global bounds, project state, finish definition (ASES-CTL-01, TSK-04) | dispatched |
| `r2_wp_critic.md` | C: Gate P plan critique (ASES-REV-01/02) | dispatched |
| `r2_wp_reconcile.md` | Rc: reconcile-on-start repairs, intents (ASES-REC-03/04) | dispatched |
| `r2_wp_killswitch.md` | K: the kill switch (ASES-REC-06) | dispatched |
| `r3_wp_sandbox.md` | SB: Docker sandbox policy, key visibility (ASES-SEC-02/03/05/06/07, CFG-04) | dispatched |
| `r3_wp_tamper.md` | TM: tamper check, artifact and secret checks (ASES-QG-03, GIT-07) | dispatched |
| `r3_wp_leases.md` | LS: resource leases, other-worktree snapshots (ASES-GIT-14, GIT-12) | dispatched |
| `r3_wp_finalgates.md` | FG: Gates 4 and 5, release report (ASES-TSK-04) | written, NOT dispatched |
| `r4_wp_profiles.md` | PF: `swarm init`, role prompts, desired Hermes config (ASES-ROL-*, ARC-08) | written, NOT dispatched |

Not yet written as work orders (see `docs/architecture.md`, section "Building the remaining phases"): the evaluation harness
(Phase 7, Appendix D, `evals.py`), the in-memory fake Hermes board plus scripted fake worker and fake provider with the
acceptance scenarios 22.2 to 22.16 (ASES-TST-01/02), and hardening (Phase 9: worktree and branch cleanup, real database
migrations, log retention, runbook).

Paths inside these files that point at a session scratchpad (the blueprint text extract, for example) are stale: the
extract now lives at `C:\Users\masoo\ases-workspaces\tools\blueprint.txt`. The helper scripts used during the build
(`nemo.py` to call nemotron, `regtool.py` to rewrite register rows, the seeded-bug scripts `mutate4.py` and `mutate5.py`)
are next to it in that folder.
