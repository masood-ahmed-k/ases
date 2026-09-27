# Round 9 Tier 2: make the register honest, pin worktree_sync (read `r9_rules.md` first)

T2A works in the primary checkout `C:\Users\masoo\ases` (register only). T2B works in its own worktree
`C:\Users\masoo\ases-wt\t2b` on branch `r9/t2b`. They run in parallel with disjoint files.

## T2A REGHYGIENE: stale register notes and two covered candidates

Requirement (blueprint p90): "the ASES repository MUST carry the register as spec/requirements.yaml with a check that fails when the
file and Appendix F drift apart. [ASES-DOC-01] [ASES-DOC-02]". The register is the project's single source of truth for build
status, so a note that contradicts the code is a defect in it.

Known stale notes (spotted by the architect on 2026-09-27; verify each against the code before changing it):
- ASES-ARC-02: "credentials not yet in the lead/coder-1/reviewer profiles"; the first real end-to-end run happened on 2026-09-19.
- ASES-ARC-03: "reconcile-on-start (crash recovery) not built yet"; `src/ases/reconcile.py` exists (see ASES-REC-04).
- ASES-GIT-01: "the real Hermes-created worktree hasn't been exercised yet pending credentials"; ASES-GIT-16's note records it being
  observed on the 2026-09-19 run.
- ASES-GIT-07 and ASES-CTL-01: "Gate 4 fails on any repository that tracks sample keys ... no allowlist"; `plan.gate4_allowlist`
  exists since round 6 (see ASES-TSK-04).
- ASES-TST-01: "22.8 (merge conflict) remains to be written"; `tests/acceptance/test_22_8_merge_conflict.py` exists.
- ASES-REV-01: "Plan critique (the critic role in Gate P) is not built"; `src/ases/critic.py` is Gate P plan critique.
- ASES-REV-03: refers to a real dispatch "in flight" on 2026-09-19.
- ASES-MOD-02: names `glm-5.3-thinking:free` as undeclared; `config/models.yaml` may have retired that model (check `_retired`).

Do:
1. For each row above, read the current code and the rest of the register and decide what is true today. Prepend a new dated note
   segment (`ROUND 9 register hygiene (2026-09-27, zero quota): ...`) that states the current truth with evidence (file names,
   test names), keeping every older note text intact after `Earlier note:` exactly as the register already does. Change `status` only
   where the evidence clearly supports it, and say why in the note.
2. ASES-TST-01 ("The controller has its own test suite that never touches a real provider ...", p282) and ASES-TST-02 ("Tests 22.1
   to 22.16 run against the fake provider and a test board unless stated otherwise, so they cost no quota and are repeatable", p396)
   may now be `covered`: check which of 22.1 to 22.16 exist under `tests/acceptance/` (22.1 and 22.17 are documented as needing a
   real provider; check what the blueprint itself says about 22.1 and 22.4), that the suite has no network or real Hermes dependency
   (the one `tests/integration/test_doctor_real_hermes.py` exception: read it and say exactly what it is), and decide. Be honest: if
   a scenario is split (22.11's real network block), say whether that blocks `covered` under the requirement's own words.
3. Then sweep EVERY other `partial` and `in_progress` row's newest note for claims that later rows or the code contradict, and fix
   them the same way. List every row you changed, and every row you checked and left alone, in your report.
4. Use `C:\Users\masoo\ases-workspaces\tools\regtool.py` if it helps (read its docstring first); otherwise edit the YAML with
   Edit, preserving the file's quoting style. When done: the YAML must still load, and
   `spec/check_requirements.py --check` must print `OK: 103 requirement IDs in sync`. Report the status counts before and after.

Files you own: `spec/requirements.yaml` only. Do not touch ASES-CFG-05 or ASES-GIT-16 (other work updates those this round).

## T2B WTSYNC: pin worktree_sync (ASES-GIT-16)

Requirement (blueprint p169, quoted exactly): "Current Hermes can sync a worktree from the freshly fetched remote tip by default;
ASES requires the worktree base to be the exact local integration HEAD. Set worktree_sync: false for ASES-managed worktrees, or have
the controller create the worktree manually from the pinned integration HEAD. Phase 3 MUST verify the actual base commit before a
worker starts. [ASES-GIT-01] [ASES-GIT-16]"

Register (partial): the 2026-09-19 run saw worktrees cut at the exact integration tip, but the test repo had no remote, so Hermes's
remote-sync default was never exercised, and "whether worktree_sync must be turned off explicitly is unverified".

Do:
1. Read the installed Hermes source (allowed, zero quota; it is under `C:\Users\masoo\AppData\Local\hermes\hermes-agent`; do not run
   it) and find exactly how Hermes 0.21.3 decides a new worktree's base: the config key name and where it lives (global
   config.yaml, profile config, per-board, per-task), its default, and what it does with and without a remote. Quote file:line.
2. Make ASES pin it explicitly: add the setting to the desired state ASES manages (find where `profiles.py` / `swarm init` builds
   Hermes config, the same place the kanban limits for ASES-ARC-08 live) so it is applied with everything else ASES pins, and to
   whatever `swarm doctor` or `profiles.verify_state` compares, so a drifted value is reported. Nothing is applied to the real Hermes
   (`swarm init` stays a dry run by default).
3. Check whether ASES already verifies the actual base commit before a worker starts ("Phase 3 MUST verify the actual base commit
   before a worker starts"): find that check if it exists and say where; if it does not, report it, do not build it here.
4. Tests for the new desired-state entry and the drift report, on fakes only. Run your test files and then the full suite once
   (the worktree command in `r9_rules.md`, from `C:\Users\masoo\ases-wt\t2b`, with `--basetemp=C:/Users/masoo/ases-wt/_pytest/t2b`).

Files you own: the module(s) that build and verify the Hermes desired state (expected: `src/ases/profiles.py`, possibly
`src/ases/doctor.py` for the report line) and their unit tests. Not `spec/requirements.yaml` (the architect updates ASES-GIT-16).
