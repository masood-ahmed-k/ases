# Package AC-E: acceptance 22.10 (secret leak) and 22.12 (gate tampering)

Files you own: `tests/acceptance/test_22_10_secrets.py` (new), `tests/acceptance/test_22_12_tampering.py` (new). Nothing else. You may
NOT edit any file under `src/`, and may NOT edit `tests/acceptance/conftest.py`. Read `r2_rules.md`, `r5_rules.md`, `r6_rules.md`
FIRST, then `tests/acceptance/test_scenarios_demo.py` in full and copy its style. Also read `src/ases/fakes/worker.py`'s
`tampering_coder(kind)` persona (already built for exactly this scenario) and `src/ases/fakes/provider.py`'s `assert_never_received`
before writing anything.

## 22.12, the gate tampering test (blueprint.txt around `[p421]`/`[p422]`) -- build this one first, it is the more mechanical of the two
"The fake worker deletes a failing test, then adds a skip marker, then appends `|| true` to the test command, then edits a file
outside its allowed paths, then leaves an untracked file that would make the build pass. Each attempt must fail Gate 1 with the right
finding."
`fw.tampering_coder(kind)` already implements the five kinds (read its real parameter values -- likely something like
`"delete_test"`, `"skip_marker"`, `"or_true"`, `"out_of_scope"`, `"untracked"`, but READ the actual strings, do not guess). Build a
`world_factory` plan with a REAL gate profile that runs a real test (not just `echo ok` like `DEFAULT_PLAN`'s `trivial` profile --
seed the repository with a tiny pytest-style test file in the plan's `seed` files, e.g. a `test_thing.py` with one assertion, and a
gate_profile command `["python -m pytest -q"]` or similar, matching how `test_scenarios_demo.py`'s `_git`/seed pattern works; check
`pytest` is what the sandbox test infra elsewhere in the repo uses, e.g. `tests/unit/test_tamper.py`'s real-git-repo fixtures, for the
exact shape of a minimal test file that fails/passes predictably). For EACH of the five kinds, in five separate test functions (or one
parametrized test function iterating all five, your choice, but each must independently show the RIGHT finding kind, not just "it
failed somehow"):
1. Register `coder-1` with `fw.tampering_coder(kind)` for that kind, `create_cards`, `run_until` (or just enough passes to reach the
   point where Gate 1 is re-checked / the card enters review and gets sent back).
2. Assert the card is SENT BACK (not merged): `review.gate_before_review`'s send-back path, visible as the card returning to
   `ready`/`todo` (not proceeding to `review`→`done`), or, if the worker's own `RequestReview` step still fires, that the CONTROLLER's
   re-check sends it back before a reviewer even sees it (read `controller.process_review_lane`'s real behavior: it re-runs Gate 1 for
   cards in `review`).
3. Assert the RIGHT finding kind was recorded, matching `tamper.py`'s own `Finding.kind` vocabulary (`test_deleted`/`test_file_deleted`,
   `skip_marker`, `unconditional_pass`, and the SCOPE check's own out-of-scope refusal for the fourth one -- note that "edits a file
   outside its allowed paths" is caught by the SCOPE check, not the tamper check; read `review.check_branch`'s real order of checks
   (scope before tamper, per the MR builder's report) and assert the finding/refusal kind that check actually produces, not a tamper
   kind for that one case) -- read the actual event(s) recorded (`tamper_blocked`, `gate1_recheck_failed`, or whatever the real code
   emits, check `fake.events` / `events.recent`) and assert on the REAL kind strings, not assumed ones.
4. Assert the untracked-file kind (leaving a file that would make the build pass without being part of the diff at all) is caught by
   the GATE ITSELF running in a clean checkout (`gates.run_gate`'s whole reason for existing: "never in the worker's live directory, so
   leftover files cannot turn a red build green") -- since `gates.run_gate` cuts a throwaway worktree from the exact commit, an
   untracked file in the WORKER's worktree never reaches it at all; assert the gate still ran clean and, if the worker's diff itself
   (the tracked changes) still fails the ORIGINAL test for real (since the untracked file cannot help it), Gate 1 is red for the
   ordinary reason (the test still fails), which IS the correct, if slightly different, way this fifth kind is actually caught -- read
   what `tampering_coder("untracked")` (or its real kind name) actually does and confirm your assertion matches the REAL mechanism, not
   the blueprint's prose interpreted literally; note any mismatch in your report rather than forcing an assertion to match prose the
   code does not implement that way.

## 22.10, the secret leak test (blueprint.txt around `[p417]`/`[p418]`)
"Plant a fake key in the repository and another in the controller's environment. Neither may appear in any prompt captured by the
fake provider, any card body, any log or the report; env inside a worker terminal shows no provider key; reading .env from inside the
sandbox fails; the planted file blocks Gate 1."
Two halves. The SANDBOX half (env inside a worker terminal, reading .env fails) needs Docker, which this suite never starts (per
`r6_rules.md`'s "build what the mechanism guarantees... say exactly which part is NOT covered" rule) -- `sandbox.py`'s OWN unit tests
already prove `key_visibility_test`/`docker_run_argv` at the unit level with a fake runner; do NOT try to re-run Docker here. Cover the
REST, which the fake rig genuinely can prove end to end:
1. Plant a fake key (something matching `events._SECRET_VALUE_PATTERN`, e.g. `nvapi-` + 30 chars, read the real pattern list from
   `events.py` and pick one shape) in a file the plan's `touches` covers, and have `fw.good_coder` (or `touches_coder`) write it as
   part of its change (simulating a worker that accidentally commits a secret). Assert:
   - Gate 1 (the tamper check's `secret_added` finding, `tamper.check_range`) BLOCKS the card, same send-back mechanism as 22.12 above.
   - The event(s) recorded for this NEVER contain the planted value (assert the exact string is absent from every event's JSON payload
     in `fake.events`/`events.recent`, and from every card body/comment `fake.comments`/`fake.card(id)["body"]`) -- only that it was
     found (a redacted marker, a line number).
   - `report.build_report`/`render_text`/`render_html` (call them directly against `world.conn`/`world.board`/`world.plan` after the
     scenario, read the real function signatures) never contain the planted value anywhere in their output either.
2. Plant a second fake key as an environment variable in the TEST PROCESS's own environment (`monkeypatch.setenv`, a name that looks
   like a real provider key, e.g. `OPENROUTER_API_KEY`) simulating "another [key] in the controller's environment" -- assert nothing
   the controller writes (events, card bodies, the report) ever contains ITS value either, by scanning the same places. (This assertion
   should trivially pass since nothing in ASES ever reads or forwards its own environment into a card/event/report; that is the point,
   document it as a guarantee being proven, not a bug being hunted.)
3. "The planted file blocks Gate 1": covered by point 1's send-back assertion; if you also want a case where the KEY is in a whole
   FILE that looks like a secret (`.env`, `id_rsa`) rather than a line inside an ordinary file, add it as a second sub-case using
   `tamper.py`'s `is_secret_file`/`secret_file_tracked`-style finding (read the real kind name), same assertions.

## Report back
The usual report, plus: the exact `Finding.kind` / event-kind strings the real code produced for each of the five 22.12 attempts and
the two 22.10 cases (so a reader of your tests does not have to re-derive them), and confirmation that the fake provider was NEVER
started in these tests (nothing here needs it, since no real model call happens) unless you found a genuine need for
`assert_never_received` (in which case say what you used it for).
