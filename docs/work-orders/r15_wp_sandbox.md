# Round 15: the Docker sandbox for real (stage B) and git inside a worker's sandbox (read `r10_rules.md` first)

The owner said "do all" on 2026-09-28 to the list that included stage B (Docker for real) and the open decision on how git
works inside a worker's Docker sandbox. So this round MAY use the real Docker on this machine (daemon 29.7.2 running): pull a
base image, build an image, start containers. It still may NOT call a real Hermes worker, a model provider, or change the
owner's real Hermes profiles (a `swarm init --sandbox` DRY RUN is fine; `--apply` is the architect's step afterwards). Every
other rule in `r10_rules.md` applies. Two packages, each in its own worktree `C:\Users\masoo\ases-wt\<name>` on branch
`r15/<name>`. Testing budget: targeted files and the real Docker checks while working, ONE full suite at the end.

Requirements (quoted from blueprint.txt): ASES-SEC-03 (p385): "From Phase 5 every worker profile MUST use the Docker terminal
backend with only its worktree mounted, no forwarded environment, CPU, memory and PID limits, and the container running as the
host user." ASES-SEC-02 (p377): "Deny agent reads of .env*, key files, ~/.ssh, cloud credential folders and browser profiles
through the sandbox mount list, not through a prompt." ASES-SEC-05 (p389): "Give containers only the network access the task
needs". ASES-QG-04 (p279): "Gates run in a clean checkout of the exact commit inside the sandbox". Acceptance 22.10 (p418):
"env inside a worker terminal shows no provider key; reading .env from inside the sandbox fails".

## SANDBOXIMG (`sandboximg`): a pinned sandbox image, and the real probes

1. `docker/sandbox/Dockerfile` (new): a small image for gates and workers: an official Python 3.11 slim base pinned by
   `@sha256` digest (pull it, read the digest with `docker inspect`, write it into the Dockerfile), git, and a pinned pytest,
   nothing else. Decide the container user (the blueprint wants the host user; `docker_run_as_host_user` does nothing on native
   Windows per the register) and justify it in a comment. Build it as `ases-sandbox:py311-1` (a pinned tag, not `latest`) and
   record the image ID.
2. `config/swarm.yaml` `sandbox:` gets `image: ases-sandbox:py311-1`. Keep `enabled: false` (the architect enables it after
   the worker git design is proven). `swarm doctor`'s image check must now pass.
3. Run the real probes against real containers and record their output in your report: `sandbox.key_visibility_test` with a
   planted fake provider key in the controller's environment (it must not be visible inside), and `sandbox.exfiltration_probe`
   (the network must be blocked). Then a real sandboxed gate: `gates.run_gate` through `gates.resolve_runner` for a project
   config with the sandbox on, against the real test repository `C:\Users\masoo\ases-workspaces\test-repo-phase3` at its
   current integration HEAD, commands `python -m pytest -q`: it must pass in the container; a deliberately failing command must
   come back red; and `.env` planted in the checkout must not be readable inside (SEC-02). Put these as a script under
   `scripts/` (for example `scripts/sandbox_live_check.py`) that a person can re-run, clearly marked as using real Docker, and
   NOT collected by pytest.
Files you own: `docker/sandbox/Dockerfile`, `config/swarm.yaml` (the image line), `scripts/sandbox_live_check.py`, small fixes in
`src/ases/sandbox.py` or `src/ases/gates.py` only if the real run exposes a bug (reproduce it in a unit test first), and tests.

## WORKERGIT (`workergit`): how git works inside a worker's sandbox

The problem: Hermes cuts a card's worktree with `git worktree add` from the board repository, so the worktree's `.git` is a FILE
holding an absolute Windows path back into `<repo>/.git/worktrees/<id>`; a Linux container that mounts only the worktree cannot
resolve it, so a coder cannot commit. The architect's proposed design (verify it, then build it, or argue for a better one with
evidence):
- Set `worktree.useRelativePaths=true` in the project repository (git 2.48+; this machine has 2.54) so new worktrees record
  RELATIVE gitdir paths. ASES sets it where it prepares a project repository (read `controller.ensure_repo_bootstrapped` and
  what `swarm init`/`doctor` check), and `swarm doctor` reports when it is off.
- Mount the repository's `.git` into the container READ-ONLY at the relative position the worktree expects, with writable
  sub-mounts only for what a commit needs (objects, refs, logs, the worktrees admin directory), so a worker can commit but
  cannot plant hooks or change config (the attack round 9's GITHARDEN defends against on the host). Say exactly what stays
  writable and what a worker could still do with it (for example move another branch ref), and which existing ASES check would
  catch that (`guards.check_primary_checkout`, the base-commit check, the merge queue's own checks).
Do:
1. Read the installed Hermes 0.21.3 docker terminal backend (read-only, under
   `C:\Users\masoo\AppData\Local\hermes\hermes-agent`): where it mounts the worktree inside the container, how
   `terminal.docker_volumes` are interpreted, the working directory, the user. Quote file:line. The design must match what
   Hermes actually does.
2. Prove it with REAL Docker on a throwaway repository under your `--basetemp` (never the real test repository): create a repo
   with `worktree.useRelativePaths=true`, add a worktree the way Hermes does, start a container with exactly the mounts the
   terminal block will give (the `ases-sandbox:py311-1` image if SANDBOXIMG has built it, otherwise any small official image
   with git), and show inside the container: `git status`, `git add`, `git commit` succeed on the card's branch; the commit is
   visible from the host; writing `.git/hooks/pre-commit` or `.git/config` fails; `env` shows no planted key. Put this in
   `scripts/workergit_live_check.py` (real Docker, not collected by pytest).
3. Build it: the terminal block `sandbox.terminal_block` / `swarm init --sandbox` produces (the volumes), the policy check
   (`mount_problems` and friends) accepting exactly these mounts and still refusing everything else, the repo setting, the doctor
   row. Unit tests for all of it (no Docker in unit tests). Show the `swarm init --sandbox` DRY RUN output in your report.
Files you own: `src/ases/sandbox.py` (terminal block and policy), `src/ases/profiles.py` (only where it builds the terminal
block), `src/ases/controller.py` (`ensure_repo_bootstrapped` only), one check in `src/ases/doctor.py`,
`scripts/workergit_live_check.py`, and tests. SANDBOXIMG may also touch `sandbox.py` for a bug fix: keep hunks small.
