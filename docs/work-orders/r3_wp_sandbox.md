# Package SB: the worker sandbox (Docker terminal backend policy, key visibility, mount denial, network default)

Files you own: `src/ases/sandbox.py` (new), `tests/unit/test_sandbox.py` (new). Nothing else. Read `r2_rules.md` first (the shared
rules apply to this round too; ignore its "baseline 669" number, the suite baseline is whatever it shows before you start and must
never go down; the modules other agents own this round are gates.py, tamper.py, leases.py, guards.py, finalgates.py).

## Requirements (read blueprint.txt lines around [p371] to [p393], the table 33 and table 37 rows, and 22.10, 22.11)
- ASES-SEC-03 (section 21.3): "From Phase 5 every worker profile MUST use the Docker terminal backend with only its worktree mounted, no
  forwarded environment, CPU, memory and PID limits, and the container running as the host user."
- ASES-SEC-02 (21.1): "Deny agent reads of .env*, key files, ~/.ssh, cloud credential folders and browser profiles through the sandbox
  mount list, not through a prompt."
- ASES-SEC-05 and ASES-SEC-07: "Sandbox network access is disabled by default and only enabled through explicit task-scoped exceptions"
  ("package registries during install steps, nothing else by default").
- ASES-SEC-06 and ASES-CFG-04: "No provider key is visible inside a worker terminal"; "Keep provider keys out of the sandbox."
- ASES-QG-04: gates run "in a clean checkout of the exact commit inside the sandbox". (gates.py is another agent's file this round; you
  only provide the argv builder it will call.)
- Test 22.10 (secret leak) and 22.11 (prompt injection) describe what the sandbox must guarantee; 22.11: "The sandbox must block the
  network call, nothing outside the worktree may change".
- Table 33 is the exact `terminal:` block for a worker profile:
  `backend: docker, cwd: /workspace, docker_image: <pinned>, docker_mount_cwd_to_workspace: true, docker_run_as_host_user: true,
  docker_forward_env: [], docker_network: false, container_cpu: 2, container_memory: 4096`.

## Ground it in the real Hermes (read-only; never edit the Hermes install, its profiles or its config)
Hermes 0.21.3 is installed at `C:\Users\masoo\AppData\Local\hermes\hermes-agent` (source) and the user's profiles are under
`C:\Users\masoo\AppData\Local\hermes\profiles\<name>\config.yaml`. Read `cli-config.yaml.example` (the `terminal:` section) and the
Docker environment implementation (search the source for `docker_forward_env`, `docker_mount_cwd_to_workspace`,
`container_memory`, `docker_run_as_host_user`, `docker_volumes`, `docker_network`, `container_pids`) to find out which of those keys REALLY
exist in 0.21.3 and what each one does. Your `terminal_block` must emit only keys that exist; list every key from table 33 that does NOT
exist (for example a PID limit) in your report, and if a limit is missing from Hermes, say so instead of inventing a key. Also read one
profile's current `config.yaml` (do not print any secret in your report) to learn the real shape (is `terminal:` top level?).

## Build `sandbox.py`
1. `SandboxPolicy` frozen dataclass: image (str), cpu (float, default 2), memory_mb (int, default 4096), pids_limit (int, default 512),
   network (bool, default False), forward_env (tuple[str, ...], default ()), extra_deny (tuple[str, ...], default ()), plus a
   `from_config(cfg: dict) -> SandboxPolicy` reading the `sandbox:` block of Appendix B (`terminal_backend`, `network_default`, `mount`,
   `forward_env`, `network_exceptions`) with safe defaults; unknown or malformed values raise `SandboxConfigError`.
2. `SandboxConfigError(Exception)`.
3. `terminal_block(policy) -> dict`: the `terminal:` block for a worker profile config (only keys that exist in Hermes 0.21.3).
4. `check_terminal_block(terminal: dict, policy) -> list[str]`: problems (empty list = compliant): backend not docker, cwd not
   /workspace, `docker_mount_cwd_to_workspace` not true, `docker_run_as_host_user` not true, forward_env not empty or containing any
   name that looks like a credential (`*KEY*`, `*TOKEN*`, `*SECRET*`, `*PASSWORD*`), network true without the policy allowing it, no
   CPU or memory limit, image missing, or an unpinned image (no tag, or the tag `latest`; a `@sha256:` digest counts as pinned), and any
   `docker_volumes` entry that mounts a sensitive host path (see 5) or the whole home directory or a drive root. Each problem is one short
   sentence naming the key. `check_profile_config(profile_config: dict, policy) -> list[str]` applies it to a loaded profile config
   (`profile_config.get("terminal")`; a missing block is a problem: it means the local backend). `load_profile_config(profile_dir) ->
   dict` reads `config.yaml` with `yaml.safe_load` (return {} when missing).
5. Sensitive paths: `sensitive_host_paths(home) -> list[pathlib.Path]` (`~/.ssh`, `~/.aws`, `~/.azure`, `~/.config/gcloud`, `~/.kube`,
   `~/.docker/config.json`, `~/.gnupg`, `~/.npmrc`, `~/.pypirc`, `~/.netrc`, `~/.git-credentials`, the browser profile folders under
   `AppData/Local/Google/Chrome/User Data`, `AppData/Local/Microsoft/Edge/User Data`, `AppData/Roaming/Mozilla/Firefox`, and on POSIX
   `~/.config/google-chrome`, `~/.mozilla`) and `sensitive_name_patterns()` (`.env`, `.env.*`, `*.pem`, `*.key`, `id_rsa*`, `id_ed25519*`,
   `*.p12`, `*.pfx`, `credentials.json`, `secrets.*`); `is_sensitive_path(path, home) -> bool`; `mount_problems(mounts, worktree,
   home) -> list[str]`: a mount list (host paths) is acceptable only when every host path is the worktree itself or inside it; anything
   else (the home directory, a drive root, a sensitive path, a parent of the worktree, a parent of a sensitive path) is a problem.
   Pure path logic (`pathlib.PurePath` semantics, case-insensitive compare for Windows drive paths); no filesystem access needed.
6. `sensitive_files_in(worktree) -> list[pathlib.Path]`: files inside the worktree matching the name patterns (walk without following
   symlinks, skip `.git`), used to mask them; `mask_args(worktree, empty_file) -> list[str]`: the `--mount type=bind,source=<empty
   file>,target=/workspace/<relative path>,readonly` arguments that overlay each of those files with an empty file so a committed
   `.env` cannot be read (test 22.10: "reading .env from inside the sandbox fails").
7. `docker_run_argv(policy, worktree, command, *, container_name=None, user=None, env=None, network=None, extra_mounts=(), empty_file=None)
   -> list[str]`: the argv for `docker run --rm` used for the controller's own sandboxed gate runs: `--network none` unless `network`
   (default: policy.network) is true, `--cpus`, `--memory`, `--pids-limit`, `--security-opt no-new-privileges`, `--cap-drop ALL`,
   `--read-only` NOT required (workers must write /workspace and tmp), `-v <worktree>:/workspace`, `-w /workspace`, `--user` only when
   given, `--name` when given, ONLY the env vars passed in `env` and only when their names pass the credential check (a credential-shaped
   name raises `SandboxConfigError`; never forward the parent's environment, and never use `--env-file`), then the image and
   `sh -lc <command>`. Mount arguments are built from the worktree and masks only; any other mount must pass `mount_problems`
   (raise `SandboxConfigError` otherwise). The path of a Windows worktree is converted for Docker Desktop (`C:\a\b` becomes `/c/a/b`
   is NOT needed: Docker Desktop accepts `C:\a\b` as the source of `-v`; use `--mount type=bind,source=...,target=...` everywhere so
   a colon in a Windows path never confuses the parser).
8. Runner and probes (all injectable, all never raising, all with a timeout): `docker_available(runner=...) -> tuple[bool, str]` (docker
   CLI on PATH and `docker info` succeeds; the string says why not), `image_present(image, runner=...) -> bool` (`docker image inspect`),
   `default_runner(argv, timeout) -> subprocess.CompletedProcess-like` (never raises TimeoutExpired: returns a nonzero code and a message).
   NEVER pull an image or start Docker: pulling or starting is a stop-condition action that needs the user; provide
   `pull_command(image) -> list[str]` that only RETURNS the argv for a human to approve.
9. `KeyVisibilityResult` (passed bool, findings list[str]) and `key_visibility_test(policy, worktree, secrets, *, runner=default_runner,
   empty_file) -> KeyVisibilityResult`: runs `env` and `cat /workspace/.env` (and `ls /root/.ssh /home` style probes are NOT needed)
   inside the sandbox through `docker_run_argv` and passes only when none of the given secret values (the caller passes planted values
   and the values of the controller's provider key environment variables) appears in either output, and reading a planted `.env` fails or
   is empty. The findings never contain a secret value (say which secret index and where it appeared). If Docker is unavailable it
   returns passed=False with a finding saying the test could not run (never a silent pass).
10. `exfiltration_probe(policy, worktree, *, runner=default_runner) -> KeyVisibilityResult`: runs a command that tries a network call from
    inside (`wget -q -T 3 -O- http://example.com` style, or `getent hosts example.com`) and passes only when it FAILS, proving the network is
    off (test 22.11: "The sandbox must block the network call"). Same rules.
11. `doctor_checks(policy, profile_dirs, *, runner=default_runner, image=None) -> list[tuple[str, bool, str]]`: (name, ok, detail) rows
    for `swarm doctor`: docker available, image present locally (only when an image is named), and one row per profile dir whose terminal
    block is compliant. No secrets in the detail strings.

## Tests (`tests/unit/test_sandbox.py`; inject a fake runner everywhere; no Docker, no network, no real Hermes writes)
policy defaults and `from_config` (valid, malformed, unknown key); `terminal_block` keys (all present, only real keys); every
`check_terminal_block` problem in both directions (compliant block returns []), unpinned image variants (no tag, latest, tag, digest),
credential-shaped forward_env names, network true, volumes mounting `~/.ssh` or the home directory; `is_sensitive_path` and
`mount_problems` (worktree ok, subfolder ok, home rejected, drive root rejected, parent of the worktree rejected, case differences on a
Windows-style path, `..` segments normalised); `sensitive_files_in` on a tmp_path tree (nested `.env`, `.env.local`, a `.git` dir skipped,
a symlink not followed where the platform allows); `mask_args` shape; `docker_run_argv`: network none by default, `--network bridge` only
when asked, limits present, no `--env-file`, no inherited env, a credential-shaped env name raises, a foreign mount raises,
`--mount type=bind` syntax with a Windows-style path; `docker_available` / `image_present` with fake runners (missing docker, daemon
down, timeout); `pull_command` returns argv and never executes anything (assert the runner was not called); `key_visibility_test` passes
when the fake runner's `env` output lacks the secrets, fails (findings without the secret text) when it contains one, fails when Docker is
unavailable; `exfiltration_probe` both ways; `doctor_checks` rows. Add one test that reads the REAL Hermes example config file when it
exists (skip otherwise) and asserts every key `terminal_block` emits appears in it.
