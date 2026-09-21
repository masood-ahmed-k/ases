# Work orders for the phase-completion build (2026-09-19 to 2026-09-22)

These are the instructions each builder agent was given while the remaining blueprint phases were built ahead of their real
testing. They are kept so a later session can see exactly which requirement IDs each module was built against, re-dispatch a
package that needs redoing, or write the next ones in the same style. What each builder reported back (what it built, every
deviation, everything it noticed in code it did not own) is in `builder-findings.md`.

Read order for a package: the shared rules (`r2_rules.md`, then `r5_rules.md` for rounds 5 and later, then `r5_contracts.md`), then
the package file.

| File | Package | Status |
| ---- | ------- | ------ |
| `r2_rules.md`, `r5_rules.md`, `r5_contracts.md` | shared rules, round 5 addendum (real Hermes facts), interface contracts | in use |
| `r2_wp_questions.md` | Q: questions and answers (ASES-REC-05) | built, then reworked by QF |
| `r2_wp_report.md` | S: status and report (ASES-OBS-01) | built, wired |
| `r2_wp_recovery.md` | R: failure classification, retries, lineage (ASES-REC-01/02) | built, wired |
| `r2_wp_bounds.md` | B: global bounds, project state (ASES-CTL-01) | built, wired |
| `r2_wp_critic.md` | C: Gate P plan critique (ASES-REV-01/02) | built, wired |
| `r2_wp_reconcile.md` | Rc: reconcile repairs and intents (ASES-REC-03/04) | built, wired |
| `r2_wp_killswitch.md` | K: the kill switch (ASES-REC-06) | built, wired |
| `r3_wp_sandbox.md` | SB: Docker sandbox policy (ASES-SEC-02/03/05/06/07, CFG-04) | built, not applied |
| `r3_wp_tamper.md` | TM: tamper check, secret and artifact checks (ASES-QG-03, GIT-07) | built, wired |
| `r3_wp_leases.md` | LS: resource leases, other-worktree snapshots (ASES-GIT-14, GIT-12) | built, wired |
| `r3_wp_finalgates.md` | FG: Gates 4 and 5, release report (ASES-TSK-04) | built, wired |
| `r4_wp_profiles.md` | PF: `swarm init`, role prompts (ASES-ROL-*, ARC-08) | built, dry run only |
| `r5_wp_qf.md` | QF: questions, escalation and recovery match how Hermes really blocks | built |
| `r5_wp_controller.md` | CT: the controller loop, version 2 | built |
| `r5_wp_mergeq.md` | MR: merge queue, review lane, usage, gates wiring | built |
| `r5_wp_cli.md` | CL: the command line, version 2 | built |
| `r5_wp_evals.md` | EV: the evaluation harness (phase 7) | built, never run against a model |
| `r5_wp_fakes.md` | FK: fake Hermes board, scripted worker, fake provider, acceptance rig | built |
| `r5_wp_hardening.md` | HD: migrations, worktree and branch cleanup, retention, runbook | built |

Not yet written as work orders: the acceptance scenarios 22.3, 22.5, 22.7 to 22.16 on the rig (`tests/acceptance/` has the rig and
two demonstrations), the project-scoping sweep (a working `project` value in `gate_runs`, `merge_records` and `events`, now that
migration 7 has made room), plan-time validation of touches (Gate 0), a Gate 4 allowlist, wiring the post-merge revert, and the
triage lane (ASES-LED-03).

Paths inside the older files that point at a session scratchpad (the blueprint text extract, for example) are stale: the extract
lives at `C:\Users\masoo\ases-workspaces\tools\blueprint.txt`, next to the helper scripts used during the build (`nemo.py` to call
nemotron, `regtool.py` to rewrite register rows, the seeded-bug scripts `mutate4.py` and `mutate5.py`).
