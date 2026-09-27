# Round 9 package GATESANDBOX: gates can run inside the Docker sandbox, with task-scoped network exceptions (read `r9_rules.md`)

Worktree: `C:\Users\masoo\ases-wt\gatesandbox`, branch `r9/gatesandbox`, cut AFTER round 8 (so `gates.py` already has round 8's
scrubbed environment). Tier 1 items 2 and 3.

Coordination note: package GITHARDEN is adding `src/ases/gitexec.py` (one hardened git argv prefix and environment) on its own
branch at the same time, and deliberately leaves `gates.py` to you. Every git subprocess you add or change in `gates.py` (the
checkout, clone, fetch, worktree remove) must go through ONE small local helper in `gates.py` that starts git with
`env=procenv.scrubbed_environ()`, so the architect can switch that one helper to `gitexec` at merge time with a one-line change.

## Requirements (quoted from blueprint.txt)
- ASES-QG-04 (p279): "Gates run in a clean checkout of the exact commit inside the sandbox, never in the worker's live directory, so
  leftover files cannot turn a red build green."
- ASES-SEC-03 (p385): "From Phase 5 every worker profile MUST use the Docker terminal backend with only its worktree mounted, no
  forwarded environment, CPU, memory and PID limits, and the container running as the host user."
- ASES-SEC-02 (p377): "Deny agent reads of .env*, key files, ~/.ssh, cloud credential folders and browser profiles through the
  sandbox mount list, not through a prompt."
- ASES-SEC-05 (p389): "Give containers only the network access the task needs: package registries during install steps, nothing
  else by default."
- ASES-SEC-07 (Appendix F): "Docker worker network is disabled by default; network exceptions are explicit and task-scoped".
- ASES-SEC-06 (p390): "Keep provider keys out of the sandbox."

## Where things stand
`gates.run_gate` takes a `runner` hook (see its docstring), and `src/ases/sandbox.py` has `docker_run_argv`, `default_runner`,
`docker_available`, `image_present` and the policy checks. But no production caller of `run_gate` passes a runner: Gate 1
(`review.py`), Gate 3 (`mergeq.py`), the post-merge Gate 3 re-run (`controller.py`) and Gates 4/5 (`finalgates.py`, which takes a
`runner` parameter: find who calls it and whether anyone passes one) all run on the host. The register (ASES-SEC-03) also records an
open design problem: a git worktree's `.git` is a FILE pointing back into the host repository, so git fails inside a container that
mounts only the checkout. ASES-SEC-05's note: "Task-scoped exceptions are not modelled in the plan schema."

## Build
1. **One resolution point.** A single function decides which runner a gate uses for a project (and task), from configuration. Every
   gate caller goes through it, so no caller can forget. Configuration: an explicit opt-in switch in the project config
   (`config/swarm.yaml`, loaded by `config.py`), default OFF, with the sandbox image and limits. Default off because Docker has never
   run for real on this machine (that is a separate, user-approved step); with it off, behaviour is exactly today's (the host runner
   with round 8's scrubbed environment).
2. **The sandbox gate runner.** Built on `sandbox.docker_run_argv` and an injectable process runner (tests never start Docker):
   only the gate checkout mounted, `--network none` unless the task has an exception, no inherited environment and no `--env-file`
   (round 8's credential scrub is irrelevant here because nothing is forwarded at all), the existing CPU/memory/PID limits, the host
   user where the platform supports it. Output handling and timeouts must match `_run_commands`' contract (same `(passed, output)`
   shape, stop at the first failing command, a timeout is a red result with a clear line).
3. **Infrastructure failure is not a red gate.** When the switch is on and Docker is unavailable or the image is missing, the runner
   raises; `run_gate` already tears down and writes no row. Check what EACH caller does with that exception today and make it a
   clean, visible infrastructure failure (an event, and the card or merge held, not failed and not merged). Never fall back to the
   host silently.
4. **A checkout git can use inside the container.** In sandbox mode only, the gate checkout must be self-contained: a `.git` that
   is a real directory with no path back to the host repository (no gitdir pointer, no `objects/info/alternates`), at the exact SHA,
   and nothing inside the container able to write the host repository's objects or refs (so no hardlinked object files either).
   Evaluate at least `git clone --no-hardlinks --no-checkout` then a detached checkout of the SHA, and a `file://` fetch of the one
   commit; pick one, justify it in the docstring (speed on a large repository versus safety), and test it without Docker (inspect
   the checkout's `.git`, confirm `rev-parse HEAD` equals the SHA, confirm no alternates file and no hardlink to a host object).
   Host mode keeps today's `git worktree add`.
5. **Task-scoped network exceptions.** A plan task may carry an explicit network exception (for example `sandbox_network: true` plus
   a required non-empty `sandbox_network_reason`; choose names consistent with the plan's existing fields). Gate 0 (`plan.py`)
   validates it; it is part of what the critic and the user see at Gate P (it lives in the published plan); and it must be covered
   by the same pinning that protects `gate_profiles` from worker edits (find `gates.hash_gate_profiles`,
   `controller.pin_gate_profiles` and `verify_gate_pin`, and extend what is hashed so flipping the flag after approval is detected).
   The sandbox gate runner gives network only to that task's own gate runs (its Gate 1, and its Gate 3 candidate); Gates 4/5 and
   every other task stay `--network none`. Worker containers are configured per Hermes PROFILE, not per card, so this package cannot
   make worker network task-scoped: say so in the docstrings and your report, do not attempt it.
6. `swarm doctor`: if the switch is on, report whether Docker is available and the image present (read-only checks through the
   existing `sandbox.docker_available` / `image_present` with the injectable runner; never start a container in a test).

## Tests (fakes only, never Docker)
The argv the sandbox runner builds (mount list, network flag default and with an exception, no environment forwarding, limits);
every gate caller routes through the resolution function (monkeypatch it and assert each of Gate 1, Gate 3, the post-merge re-run
and Gates 4/5 used it); switch off equals today's behaviour (existing tests stay green unchanged); Docker unavailable is an
infrastructure failure at each caller, never a red gate and never a silent host run; the self-contained checkout; Gate 0 accepts a
well-formed exception and rejects a malformed one; the pin detects a flipped exception. Before/after proof for the caller wiring:
show a caller test failing against the old call site and passing against the new one.

## Files you own
`src/ases/sandbox.py`, `src/ases/gates.py` (resolution function and checkout mode only; round 8 just changed `_run_commands`, keep
that), `src/ases/plan.py` (the new task field), `src/ases/config.py` and `config/swarm.yaml` (the switch), the gate call sites in
`src/ases/review.py`, `src/ases/mergeq.py`, `src/ases/controller.py`, `src/ases/finalgates.py` (only the lines that call `run_gate`
or the Gate 4/5 entry points, plus their exception handling), the pin functions, `src/ases/doctor.py` (one check), and the matching
tests. Other packages edit other parts of `review.py`, `mergeq.py`, `controller.py`, `plan.py` and `doctor.py` on their own branches:
touch only what this package needs.
