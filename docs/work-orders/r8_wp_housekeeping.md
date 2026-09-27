# Round 8 package HOUSEKEEPING: two small, independent fixes (read `r8_rules.md` first)

The user asked for these first, before the gate-environment work. They are separate sub-packages with disjoint files, dispatched in
parallel.

## HK-PATH: the register drift check cannot find the blueprint

Requirement (blueprint p90, quoted exactly): "the ASES repository MUST carry the register as spec/requirements.yaml with a check that
fails when the file and Appendix F drift apart. [ASES-DOC-01] [ASES-DOC-02]"

Observed 2026-09-27: `spec/check_requirements.py --check` with no flags fails with
`blueprint docx not found: C:\Users\masoo\OneDrive\Desktop\ASES_Swarm_Implementation_Blueprint_v1.2.docx`. The docx moved to
`C:\Users\masoo\OneDrive\Desktop\AISES\ASES_Swarm_Implementation_Blueprint_v1.2.docx`. With `--docx <new path>` the check passes
(103 IDs in sync). A drift check that fails for a reason unrelated to drift is worse than useless: people learn to ignore it.

Build:
1. Point `DEFAULT_DOCX` at the new location.
2. Add an override so the next move does not break it again: an `ASES_BLUEPRINT_DOCX` environment variable, used when set and
   `--docx` is not given. Precedence: `--docx`, then the variable, then the default. Document it in the module docstring.
3. When the file is not found, the error must name the path it tried AND say how to point the check elsewhere (`--docx` or the
   variable). Keep the exit code non-zero.
4. Tests for the precedence and the not-found message (find the existing tests for this script first with Grep; if there are none,
   add `tests/unit/test_check_requirements.py`). Tests must not need the real docx: exercise path resolution, not extraction.
5. Run `spec/check_requirements.py --check` with no flags and confirm `OK: 103 requirement IDs in sync`.
6. Grep the repo (excluding `.venv`) and `C:\Users\masoo\ases-workspaces\tools` for any other reference to the old path and fix it.

Files you own: `spec/check_requirements.py`, its test file. Nothing else.

## HK-GAPS: the "Known gaps" list in docs/architecture.md has drifted

`docs/architecture.md` ends with a "## Known gaps (tracked, not hidden)" section (currently around line 1144), then "## Running
things". Some bullets there are stale. Example the architect spotted but did NOT verify: "Gate 1/3 run directly on the host, not
inside Docker (Phase 5 requirement, not built)". The sandbox module has since been built (`src/ases/sandbox.py`) and `gates.run_gate`
takes a `runner` hook, but the register row ASES-SEC-03 says "the controller's own gate runs do not use docker_run_argv yet", and no
caller of `run_gate` in `review.py`, `mergeq.py` or `controller.py` passes a runner. So that bullet may be half-true, not stale.
Decide from the code, not from this paragraph.

Build:
1. For EVERY bullet in that section, check its claim against the current code (`src/ases/`), the register (`spec/requirements.yaml`
   status and note of every ID the bullet names or implies) and the blueprint text. Classify each bullet: still true, partly true
   (say which part changed), or stale.
2. Fix the section in the file's own established style: a stale bullet is struck through with `~~...~~` plus a one-line dated note
   saying what superseded it (the file already does this: "Left struck through instead of silently deleted so the drift is
   visible"); a partly true bullet is rewritten to say exactly what is true today, with the register ID; a still-true bullet stays.
   Date new notes 2026-09-27.
3. Add a bullet for any genuine open gap the register records as `partial` that a reader of this section would expect to find and
   that is missing, but only when the register note states it as open. Do not invent gaps.
4. Also check the "## Running things" block: every command in it must still work as written on this machine (the full suite needs
   `--ignore=tests/integration/test_doctor_real_hermes.py`; the drift check needs the docx path fix from HK-PATH, which lands in
   parallel, so write the command as it will work once HK-PATH lands). Do not run `swarm doctor` or anything that calls real Hermes:
   `swarm doctor` DOES shell out to `hermes`, so check that command by reading `doctor.py`, not by running it.
5. Do not touch any other section of `docs/architecture.md`. The dated round sections above are a historical log: they stay as
   written even where later work superseded them.
6. Scan the file for em dash and section sign characters when done (the file must have none; if older sections have some, report
   the line numbers, do not edit those sections).

Files you own: `docs/architecture.md`, the "Known gaps" and "Running things" sections only.
