# Round 8 package GATEENV: gate commands no longer see the operator's credentials (read `r8_rules.md` first)

## Requirements this package satisfies (quoted from `ases-workspaces/tools/blueprint.txt`, the source)
- ASES-CFG-04 (p212): "Hermes provider credentials must never be exposed to worker terminals. ASES MUST verify this for the exact
  provider/authentication path in use; if any provider key is visible, move it into Hermes credential storage or behind the approved
  egress mechanism before running unattended workers."
- p213 (same section, no ID of its own): "Never export provider keys in the shell that launches the gateway or the controller."
- ASES-CFG-05 (Appendix F): "UnoRouter custom-endpoint credentials use Hermes custom-endpoint secret handling; do not export provider
  keys into worker shells". Register status `partial`; its note names this exact gap as the one deferred item.
- ASES-SEC-01 (p375): "Run the secret scanner in Gate 1 and Gate 3, and scan card bodies and plan files before they are written."
- ASES-QG-04 (p279): "Gates run in a clean checkout of the exact commit inside the sandbox, never in the worker's live directory, so
  leftover files cannot turn a red build green."
- Acceptance 22.10 (p418): "Plant a fake key in the repository and another in the controller's environment. Neither may appear in any
  prompt captured by the fake provider, any card body, any log or the report; env inside a worker terminal shows no provider key;
  reading .env from inside the sandbox fails; the planted file blocks Gate 1."

## The gap
`src/ases/gates.py::_run_commands` runs every gate command with `shell=True` and the controller's full, unscrubbed environment. Gate
commands execute code that can be model-authored (a test a coder committed). On the local backend (the default; no gate caller passes
a sandbox `runner` today), that code can read any provider key the operator's shell happens to hold. Output redaction
(`events.redact_text` in `run_gate`) is a second layer only: code that encodes the key (reverses it, hexes it, sends it over the
network) walks straight past it. Two independent agents found this during the round 7 CFG-05 work and correctly left it for a design
decision.

`run_gate`'s own `git worktree add` has the same exposure through a side door: the gate checkout runs the repository's hooks (a
`post-checkout` hook in the shared `.git/hooks`, which a worker on the local backend can write) under whatever environment that git
process has.

## Design decision (architect, 2026-09-27)
1. The host gate runner starts every gate command with `procenv.scrubbed_environ()` (the project's one definition of
   "credential-shaped", already used by all five real hermes launch sites). `PATH`, `COMSPEC`, `SYSTEMROOT`, `PATHEXT`, `TEMP`/`TMP`
   and the profile directories do not match the pattern and survive unchanged, so the "COMSPEC/PATH must survive" concern from the
   round 7 note is met by the existing helper.
2. `run_gate`'s own git subprocesses (`worktree add`, `worktree remove`) get the same scrubbed environment, so a hook or filter that
   git runs during the gate checkout sees no credential either.
3. **No pass-through allowlist this round.** A gate command that genuinely needs a credential-shaped variable now fails, loudly, with
   its output recorded in `gate_runs`. If a real project ever needs one, the right home is a plan-level list published and reviewed
   at Gate P (the same reasoning as `gate4_allowlist`: never a config file a worker can edit), protected by the same gate-profile hash.
   That is a follow-up, not this package. Say so in the `_run_commands` docstring.
4. **One false positive in the shared pattern gets an explicit exemption:** `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL` and
   `GIT_AUTHOR_DATE` match only because "AUTHOR" contains "auth". They carry no secret and grant no capability, and a gate command
   that makes a commit (common in projects that test git tooling) can need them. Add a small, named, documented frozenset of exact
   names that are exempt from the pattern in `procenv.py`. Do NOT exempt `SSH_AUTH_SOCK` or `XAUTHORITY`: they are capability-bearing
   (an ssh-agent socket lets model-written code authenticate as the operator), so stripping them is correct. Leave `SESSIONNAME` as
   is. This exemption applies to every caller of `scrubbed_environ()`; confirm none of the five existing hermes sites or `evalkit`
   depends on `GIT_AUTHOR_*` being absent.
5. Docker sandbox runners are unchanged: they never inherit the host environment (`sandbox.docker_run_argv`). Confirm by reading,
   and do not touch `sandbox.default_runner` (its key-leak probes need the real environment; see the round 7 CFG-05 note).

## Build
- `src/ases/gates.py`: items 1 and 2. Update the module docstring and `_run_commands`'s docstring (quote ASES-CFG-04/CFG-05 and say
  what is and is not covered: the host runner and the gate checkout now are; a gateway-dispatched worker still is not, because ASES
  never spawns it).
- `src/ases/procenv.py`: item 4, with its docstring updated.
- Tests, in `tests/unit/test_gates.py` and `tests/unit/test_procenv.py`:
  - A credential-shaped variable set with `monkeypatch.setenv` (use a provider-looking name such as `OPENROUTER_API_KEY` and a second
    generic one such as `MY_SERVICE_TOKEN`) is NOT visible to a gate command. Make the command print a presence marker, not the value
    (for example `python -c "import os;print('KEYSEEN' if os.environ.get('OPENROUTER_API_KEY') else 'NOKEY')"`), because
    `run_gate` redacts secret-shaped values in its output and an assertion on the value itself could pass for the wrong reason.
    Use `sys.executable` quoted for the interpreter so the test does not depend on PATH lookup of `python`.
  - A non-credential variable (`PATH`, and a made-up `ASES_GATE_PROBE=1`) IS still visible, so the fix did not just empty the
    environment. On Windows, a `shell=True` command that needs `COMSPEC`/`SYSTEMROOT` still runs.
  - `GIT_AUTHOR_NAME` survives into a gate command; `SSH_AUTH_SOCK` does not.
  - A `post-checkout` hook planted in the test repo's `.git/hooks` runs during `run_gate` and cannot see the planted key. Assert the
    hook DID run (it writes a marker file) so the key assertion is not vacuous. Git for Windows runs `#!/bin/sh` hooks; if the hook
    genuinely cannot run on a platform, skip with an honest reason rather than letting the test pass vacuously.
  - An acceptance-level check in a NEW file `tests/acceptance/test_22_10_gate_env.py`: a `world_factory` world whose gate profile
    command prints the presence marker, with a planted `OPENROUTER_API_KEY` in the test process environment, driven through a real
    controller pass on `FakeHermes`; assert the recorded Gate 1 (and Gate 3 if the scenario reaches it) output says `NOKEY`. Read
    `tests/acceptance/test_22_10_secrets.py` and `tests/acceptance/conftest.py` first and follow their style; do not edit either.
- **Before/after proof, required:** with your tests written, put the old `gates.py` back (copy aside, never a bare `git stash`), run
  the new tests and show which fail and how (`KEYSEEN`, the hook marker containing the key), restore your version, show they pass.
  Paste both runs' summary lines in your report.
- Sweep: every call site of `run_gate` (`review.py` Gate 1, `mergeq.py` Gate 3, `controller.py` post-merge check, `finalgates.py`
  Gates 4/5) now inherits the fix through the one choke point. Confirm no gate or check command is run anywhere else in `src/ases`
  through its own `subprocess` call with `shell=True` or with project-supplied commands. If you find one, report it; fix it only if it
  is the same shape and a one-line change.

## Files you own
`src/ases/gates.py`, `src/ases/procenv.py`, `tests/unit/test_gates.py`, `tests/unit/test_procenv.py`, and the new
`tests/acceptance/test_22_10_gate_env.py`. Do not edit `spec/requirements.yaml`, `docs/architecture.md` or anything under
`docs/work-orders/`: the architect updates those after verification.

## Out of scope, report only
The controller's OTHER git calls (merge queue, review, reconcile, guards, hardening, leases, integrity) also run in repositories a
worker can write `.git/config` and `.git/hooks` into, with the full environment. A separate read-only sweep is looking at that in
parallel. If you notice something there, put it in your report; do not fix it.
