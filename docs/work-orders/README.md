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
| (no separate work order; a direct architect audit) | audit of the 3 rows wave 2 left not_covered: ASES-ARC-01, ASES-DOC-03, ASES-CFG-05 | done |
| (no separate work order; two build-and-verify pairs dispatched from the audit's CFG-05 finding) | credential-scrubbed environment for every real hermes launch (ASES-CFG-05) | done, independently verified twice |

Wave 1 (four packages) landed as commit `e6bcf5b`; wave 2 (ROLES2 plus the two verification/fix agents dispatched alongside it)
landed as `4e9d2fe` and left three rows not_covered (ASES-ARC-01, ASES-DOC-03, ASES-CFG-05); an audit closed ARC-01 by design,
found DOC-03 honestly partial, and found a concrete CFG-05 gap that two more build-and-verify pairs then fixed (`dc14773` and the
CFG-05 commit that follows it). See `builder-findings.md` for every report, including two architect fixes in wave 1
(`FakeHermes.kanban_specify`, a `recovery.next_model` regression), the independent review in wave 2 that corrected its own site
count (five hardcoded-role sites in controller.py, not six as first estimated), and the CFG-05 pairs' shared finding of a
separate, deferred gap in `gates.py` (same family as ASES-SEC-01/03, not fixed under this scope).

Paths inside the older files that point at a session scratchpad (the blueprint text extract, for example) are stale: the extract
lives at `C:\Users\masoo\ases-workspaces\tools\blueprint.txt`, next to the helper scripts used during the build (`nemo.py` to call
nemotron, `regtool.py` to rewrite register rows, the seeded-bug scripts `mutate4.py` and `mutate5.py`).

## Round 8 (2026-09-27): housekeeping, then the gate-command environment

The user asked for "the housekeeping items first, then close the gates.py gap", built by Sonnet agents, "we will test properly
later". One workflow, zero quota.

| File | Package | Status |
| ---- | ------- | ------ |
| `r8_rules.md` | shared rules addendum: the round 8 suite command, no bare `git stash` in a shared tree | in use |
| `r8_wp_housekeeping.md` | HK-PATH: the drift check's blueprint path and an `ASES_BLUEPRINT_DOCX` override (ASES-DOC-02); HK-GAPS: the "Known gaps" list re-checked against the code | done, independently verified |
| `r8_wp_gateenv.md` | GATEENV: gate commands and the gate checkout run with a credential-scrubbed environment (ASES-CFG-04/05) | done, independently reviewed and live-verified |
| (no separate work order; a read-only sweep dispatched alongside GATEENV) | the controller's other git calls: hooks, fsmonitor, textconv in worker-writable repositories | done; became round 9's GITHARDEN |

## Round 9 (2026-09-27): Tier 2, then every Tier 1 item, many agents at once

The user asked for "tier 2 first, then tier 1 items - dispatch multiple agents to do the work faster". Each package had its own
git worktree under `C:\Users\masoo\ases-wt\` and branch `r9/<package>`; Tier 2 landed first.

| File | Package | Status |
| ---- | ------- | ------ |
| `r9_rules.md` | shared rules: one worktree and one pytest `--basetemp` per package, no `git stash` at all, imports from the worktree | in use |
| `r9_wp_tier2.md` | T2A: register notes re-checked against the code (ASES-DOC-01/02); T2B: `worktree_sync` (ASES-GIT-16) | done, independently verified |
| `r9_wp_githarden.md` | GITHARDEN: `gitexec.py`, one hardened way the controller runs git (ASES-CFG-04, SEC-01, SEC-04) | done, independently verified |
| `r9_wp_gatesandbox.md` | GATESANDBOX: gates in the Docker sandbox, task-scoped network (ASES-QG-04, SEC-03, SEC-05, SEC-07) | done, independently verified; pin wiring finished by the architect |
| `r9_wp_mergepk.md` | MERGEPK: `merge_records` keyed by project and task, schema v8 (ASES-ARC-03, GIT-05) | done, independently verified |
| `r9_wp_small.md` | CIPIN (ASES-QG-02), IDLEWT (GIT-12), PAUSEREASON (CTL-01), DOCTOR (VER-01, p213), CAPDOC (CAP-06) | done, independently verified |
| `r9_wp_eventsproj.md` | EVENTSPROJ: events carry their project (ASES-ARC-03, OBS-01) | done, independently verified; two report panels scoped by the architect |

## Round 10 (2026-09-27): the base-commit check, and what round 9 surfaced

| File | Package | Status |
| ---- | ------- | ------ |
| `r10_rules.md` | shared rules: Write/Edit only (a heredoc eats backslashes), no `/tmp` for Windows Python, use `gitexec` and `events.PROJECT_SCOPE_SQL` | in use |
| `r10_wp_basecheck.md` | BASECHECK: a work card's base commit is verified, detection plus merge refusal (ASES-GIT-01, GIT-16) | done, independently verified; `swarm doctor --repo` wired by the architect |
| `r10_wp_small.md` | GATEPIN (ASES-QG-02), BUDGETFIX (CAP-03, CTL-01), CALLERS (MOD-04, ROL-05) | done, independently verified |

## Round 11 and round 12 (2026-09-28): MOD02, stage A, the audit and its fixes

The owner asked for autonomous work overnight ("keep doing things back to back", short test cycles, pushing allowed).

| File | Package | Status |
| ---- | ------- | ------ |
| `r11_wp_mod02.md` | MOD02: an under-declared model is rejected before any card starts (ASES-MOD-02, acceptance 22.4) | done, independently verified |
| (no work order; architect-run) | stage A: real doctor and `swarm init` dry run, `docs/stage-a-2026-09-28.md` | done; applying `swarm init` waits for the owner |
| `r12_audit_findings.md` | a read-only adversarial audit of rounds 8-11: 12 findings confirmed, 1 refuted | done |
| `r12_wp_fixes.md` | GATEINFRA (findings 0, 10, 11, 1), DATAFIX (4, 5, 6, 7), RUNSTART (2, 8, 3, 9) | done, independently reviewed |

## Rounds 13 and 14 (2026-09-28): tidy-ups, a second audit of the new code, and its fixes

| File | Package | Status |
| ---- | ------- | ------ |
| `r13_wp_tidy.md` | TIDY: one process-tree kill helper; doctor reports leaked gate worktrees (ASES-GIT-12) | done, independently reviewed |
| `r14_audit2_findings.md` | a second read-only audit of what rounds 11 and 12 changed: 3 findings confirmed, 0 refuted | done |
| `r14_wp.md` | RUNSTART2 (the three findings: ASES-REC-04, MOD-02, GIT-16), CLOCK (an injectable ledger clock, ASES-CAP-03) | done, independently reviewed |
