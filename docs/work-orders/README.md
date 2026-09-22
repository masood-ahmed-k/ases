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

## Round 6 (2026-09-22, zero quota only per the user's "no need to test and burn the tokens from xkiro")

| File | Package | Status |
| ---- | ------- | ------ |
| `r6_rules.md` | shared rules addendum: the hard zero-quota constraint, the fake rig summary | in use |
| `r6_wp_core.md` | CORE: project-scoped gate_runs/merge_records, the post-merge revert trigger (ASES-GIT-05), the CHANGES_REQUIRED dead end | dispatched |
| `r6_wp_tv.md` | TV: touches can no longer hide gate-config edits (ASES-QG-02), a Gate 4 allowlist | dispatched |
| `r6_wp_led.md` | LED: agent-proposed cards land in triage and are validated (ASES-LED-03) | dispatched |
| `r6_wp_fix.md` | FIX: one Bounds class, one stop_requested meaning, report.HEALTH_KINDS | dispatched |
| `r6_wp_ac_a.md` | AC-A: acceptance 22.3 (failure/fallback), 22.9 (quota exhaustion) | dispatched |
| `r6_wp_ac_b.md` | AC-B: acceptance 22.5 (parallel), 22.13 (kill switch) | dispatched |
| `r6_wp_ac_c.md` | AC-C: acceptance 22.7 (crash recovery, all three points) | dispatched |
| `r6_wp_ac_e.md` | AC-E: acceptance 22.10 (secret leak), 22.12 (gate tampering) | dispatched |
| `r6_wp_ac_f.md` | AC-F: acceptance 22.11 (prompt injection; honest about the Docker gap) | dispatched |
| `r6_wp_ac_g.md` | AC-G: acceptance 22.14 (plan rejection), 22.15 (idempotent re-run), 22.16 (data class) | done |

All ten round 6 packages finished; see `builder-findings.md` for every report. CORE's own full-suite run was clean (5416 passed, 2
skipped, 0 failed) before the other nine packages' changes were all merged together; a final merged-tree run confirms the whole set.

Not yet written: acceptance 22.8 (merge conflict; CORE's revert wiring now exists, so this can be dispatched as package AC-D), 22.4
(context test; likely already covered by `test_models.py`, worth confirming rather than rebuilding), the `merge_records` primary-key
migration CORE wrote up (needs `db.py` plus five other readers updated together), the `events.project` sweep (deliberately deferred
twice now), a fix for `triage.promote_card` (always fails on a genuinely triage-status card, found by AC-G), a fix for
`FakeHermes.fail_next` (cannot be armed after `install()`, found independently by two builders), a Lead-prompt mention of the new
`allow_gate_config_changes`/`gate4_allowlist` plan fields (TV's report asks for this), and `swarm triage` CLI commands plus a
`process_triage` controller step (LED's report asks for this).

## Round 7 (2026-09-22 to 2026-09-23, zero quota, one narrowly authorized real Hermes call)

The user was told what was left to build (the items in the paragraph above, plus the never-built register rows) and answered in
one message: "option A" (ASES may call Hermes's own `specify`, only from `triage.promote_card`), "fix all the bugs", "build all".

| File | Package | Status |
| ---- | ------- | ------ |
| `r7_rules.md` | shared rules addendum: the one authorized real Hermes call, and why it does not weaken the zero-quota rule | in use |
| `r7_wp_specify.md` | SPECIFY: `hermes.kanban_specify`, `triage.promote_card` now works (ASES-LED-03) | done |
| `r7_wp_fixes.md` | FIXES: stale fix/retry-card pointer, budget gate data-class check, a card stuck ready after an auth/quota failure (ASES-CAP-03, PRV-04, REC-01/03) | done |
| `r7_wp_policy.md` | POLICY: docs/ases scaffolding, data-policy verification, key-pool doctor checks (ASES-GIT-15, PRV-04, CFG-02/03) | done |
| (no separate work order; AC-D dispatched directly) | AC-D: acceptance 22.8, the sixteenth and last blueprint scenario | done |
| `r7_wp_wave2_roles.md` | ROLES2: greenfield repo bootstrapping, the Tester-role hardcoded-role bug (ASES-GIT-10/11, QG-05, ROL-09) | done, independently verified |
| (no separate work order; dispatched directly alongside ROLES2's verification) | the matching hardcoded-role bug in `reconcile.py` (ASES-REC-04) | done |

Wave 1 (four packages) landed as commit `a98b95e`; wave 2 (ROLES2 plus the two verification/fix agents dispatched alongside it)
closes out every remaining "not_covered" row from the user's "build all" instruction. See `builder-findings.md` for every report,
including two architect fixes in wave 1 (`FakeHermes.kanban_specify`, a `recovery.next_model` regression) and the independent
review this round that corrected wave 2's own site count (five hardcoded-role sites in controller.py, not six as first estimated)
and found no stale tests anywhere else in the repository.

Paths inside the older files that point at a session scratchpad (the blueprint text extract, for example) are stale: the extract
lives at `C:\Users\masoo\ases-workspaces\tools\blueprint.txt`, next to the helper scripts used during the build (`nemo.py` to call
nemotron, `regtool.py` to rewrite register rows, the seeded-bug scripts `mutate4.py` and `mutate5.py`).
