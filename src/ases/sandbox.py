"""Worker sandbox policy: the Docker terminal block, the key and mount checks, and the docker argv builder
(section 21.3; ASES-SEC-02, ASES-SEC-03, ASES-SEC-05, ASES-SEC-06, ASES-SEC-07, ASES-CFG-04, ASES-QG-04).

Safety comes from the mount list and the container flags, never from a prompt (section 21.1). This module holds
everything ASES itself decides about that boundary, as pure functions plus a few probes whose runner is
injectable, so the tests never need Docker:

- terminal_block / check_terminal_block: the `terminal:` block a worker profile must carry (table 33, with the
  two differences explained below) and the checker `swarm doctor` runs over a profile's config.yaml. A profile
  with no terminal block runs on the local backend, on the user's own account with no isolation, so a missing
  block is reported as a problem.
- the sensitive-path model (~/.ssh, cloud credentials, browser profiles, .env and key files), used to refuse a
  mount and to mask a committed .env with an empty file (test 22.10: reading .env from inside the sandbox fails).
- docker_run_argv: the `docker run` argv for the controller's own sandboxed gate runs (ASES-QG-04). It is built
  from the worktree and the masks only and refuses anything else.
- sandbox_command_runner (round 9): wraps docker_run_argv as the `runner` hook gates.run_gate takes, one `docker
  run` per gate command, with the task-scoped network exception (ASES-SEC-05, ASES-SEC-07) as its one parameter.
  gates.resolve_runner is the single place that decides whether a gate call gets one of these or the host runner.
- key_visibility_test / exfiltration_probe: tests 22.10 and 22.11 as runnable probes.

Nothing here starts Docker or pulls an image: both are stop-condition actions that need the user (section 16).
pull_command only RETURNS the argv for a human to approve, and every docker run built here says --pull never.
All path checks are lexical (no filesystem access), so callers pass absolute, already-resolved paths: a junction
or an 8.3 short name is not seen through.

What Hermes 0.21.3 really does, read from its source on 2026-09-19 (cli.py, tools/environments/docker.py,
tools/terminal_tool.py, hermes_cli/config_defaults.py, hermes_cli/config.py). Items 4 and 5 are why terminal_block
differs from a literal reading of table 33; items 2 and 3 are gaps that config alone cannot close; none of it is
guessed:

1. Every key in table 33 exists. docker_network is real (default true; false becomes --network=none) but is missing
   from cli-config.yaml.example, so "confirm against the example config" alone would wrongly say it does not exist.
   The keys Hermes honours from config.yaml are the ones in TERMINAL_CONFIG_ENV_MAP (hermes_cli/config.py).
2. There is NO PID-limit key. Hermes hard-codes --pids-limit 256 and passes it, like --cpus and --memory, only when
   a probe container with those flags starts (_cgroup_limits_available); if the probe fails (for example an image
   that is not pulled yet) all three limits are silently dropped with a log line. SandboxPolicy.pids_limit therefore
   governs ASES's own docker runs only. The image must be pulled by a human before workers run (doctor_checks).
3. docker_run_as_host_user needs os.getuid, which does not exist on native Windows: Hermes then starts the
   container as the image's default user (usually root), adds SETUID and SETGID back, and logs a warning.
   ASES-SEC-03's "host user" is not met there by config alone.
4. cwd. Table 33 says cwd: /workspace, but that key stops the worktree being mounted. The kanban dispatcher pins the
   worktree as TERMINAL_CWD in the worker's environment, the worker's CLI then force-exports the profile's
   terminal.cwd over it (cli.py _mirror_config_to_env), and docker_mount_cwd_to_workspace mounts whatever
   TERMINAL_CWD names as a HOST directory. With cwd: /workspace that is C:\\workspace (or /workspace), which does
   not exist, so the worker gets an empty workspace; if such a directory did exist, THAT would be mounted.
   Verified by running the real functions from the 0.21.3 source in memory. With no cwd key the pin survives and
   Hermes itself starts the container in /workspace. terminal_block therefore has no cwd and the checker refuses
   one (a placeholder such as '.' is ignored by Hermes for docker and is accepted).
5. Hermes keeps ONE long-lived container per profile and re-attaches to it by label (task id, profile, egress
   posture) in every later process; the bind mounts are fixed when it is created and are never compared. With table
   33 as written, the second card's worker would land in the first card's container and see the first card's
   worktree at /workspace. docker_persist_across_processes: false (a real key) stops the reuse, so terminal_block
   adds it. container_persistent: false cannot replace it: Hermes then refuses the process-global TERMINAL_CWD as a
   mount source and gives the worker an empty tmpfs. The price of not reusing a container is that a worker killed
   by a timeout or the kill switch leaves its container running (Hermes only reaps Exited ones). Nothing removes
   those yet; they carry the label hermes-agent=1 and the profile in hermes-profile.
6. docker_extra_args is appended verbatim after Hermes's own flags, so it can undo any of them (a later -v, --memory
   or --network wins, and a bare word replaces the image). docker_env, env_passthrough and credential_files are
   other ways for a secret or a host file to reach the container, and a docker_volumes entry containing ':/workspace'
   makes Hermes skip the worktree mount. The checker covers all of these.

WORKERGIT (round 15, docs/work-orders/r15_wp_sandbox.md): how a worker commits inside its own dispatched worktree.

The problem: Hermes's Kanban dispatcher cuts a card's worktree with `git worktree add <repo>/.worktrees/<task_id>
<start-point>` from the board repository (hermes_cli/kanban_db_workspace.py:422 _ensure_git_worktree, :443
_anchored_worktree's `target = repo_root / ".worktrees" / task_id`, read-only, under
C:\\Users\\masoo\\AppData\\Local\\hermes\\hermes-agent). A linked worktree's own `.git` is a FILE naming the main
repository's `.git/worktrees/<task_id>` admin directory; by default that name is an ABSOLUTE host path, which does
not exist inside a Linux container that mounts only the worktree (item 4 above -- `docker_mount_cwd_to_workspace`
mounts the worktree itself at /workspace, nothing else), so a worker's own `git` fails there today ("not a git
repository"), the open design question ASES-SEC-03's own register note names.

The fix, verified against a real Docker container (scripts/workergit_live_check.py) on a throwaway repository under
a --basetemp, never the real test repository:
1. `worktree.useRelativePaths=true` (git 2.48+; controller.ensure_repo_bootstrapped sets it for a repository ASES
   itself creates from empty, doctor.py's worktree_relative_paths check reports when an existing one has it off)
   makes `git worktree add` record a RELATIVE gitdir: for the canonical `<repo>/.worktrees/<task_id>` layout,
   empirically `gitdir: ../../.git/worktrees/<task_id>` inside the worktree's own `.git` file, and `commondir:
   ../..` inside the admin directory. A relative '..' cannot climb above a container's filesystem root (POSIX
   clamps it there), so from /workspace (the mounted worktree, itself two directories below the repository root)
   that pointer resolves to exactly /.git/worktrees/<task_id> -- which is where mounting the repository's own
   `.git` at /.git puts it.
2. Hermes's own docker_volumes are `-v host:container[:ro]` strings Hermes reads back out of the profile's config
   (hermes_cli/config.py TERMINAL_CONFIG_ENV_MAP; tools/environments/docker.py DockerEnvironment._mount_args
   passes each straight to `-v`, skipping only an entry with no colon), and, critically, Hermes's own config
   loader expands `${VAR}` / `${env:VAR}` in EVERY string value, mounts included, before mirroring config.yaml
   into the environment variables the terminal actually launches with (installed Hermes 0.21.3, cli.py:488-490
   "Expand ${ENV_VAR} references before bridging to env vars", `defaults = _expand_env_vars(defaults)`, run
   BEFORE cli.py:498's `_mirror_config_to_env`). The kanban dispatcher pins the dispatched worktree's own absolute
   host path into TERMINAL_CWD before the worker process starts (kanban_db_dispatch.py:2590 `env["TERMINAL_CWD"]
   = workspace`), and terminal_block() carries no `cwd` key (item 4 above), so that pin survives untouched into
   this expansion. A docker_volumes entry of the literal string "${env:TERMINAL_CWD}/../../.git:/.git:ro"
   therefore expands, at each worker's own startup, to THAT task's own repository .git -- one static profile
   config.yaml, correct for every project and every card, with no per-task or per-project rewrite (confirmed
   empirically: Docker itself normalises a host bind-mount source containing '..', mixed slash styles included,
   before the daemon ever sees it). git_worktree_volumes() below is exactly these five entries.
3. Hermes never compares nested docker_volumes mount targets against each other (each is its own bind mount;
   confirmed empirically, in either arg order), so the five are, together with the worktree at /workspace:
     - .git -> /.git, READ-ONLY: config, hooks, HEAD, every OTHER branch's history.
     - .git/objects -> /.git/objects, writable: the blobs/trees/commits a commit creates.
     - .git/refs -> /.git/refs, writable: the branch ref(s) a commit moves.
     - .git/logs -> /.git/logs, writable: the reflog entry a commit appends.
     - .git/worktrees -> /.git/worktrees, writable: every dispatched card's own per-worktree HEAD/index/ORIG_HEAD
       admin state (Hermes's own static, per-ROLE config.yaml has no per-task variable this design could use to
       name only THIS card's own admin subdirectory -- TERMINAL_CWD is the only per-task value Hermes exports,
       and config-string expansion substitutes a whole `${env:VAR}` value verbatim, never a substring of it -- so
       the whole directory is what a static config can express; see the WORKERGIT report for the alternative
       considered and rejected: rewriting config.yaml per dispatch, which would race two cards on the same role).
   Proven with a real container (scripts/workergit_live_check.py): `git status`, `git add`, `git commit` succeed
   on the card's own branch and the commit is visible from the host afterwards; writing `/.git/hooks/pre-commit`
   or editing `/.git/config` fails (Read-only file system); no planted host key is visible in `env` (docker_volumes
   never forwards environment; that is docker_env/docker_forward_env's job, unrelated to these five).

   What a worker COULD still do with objects/refs/logs/worktrees writable, deliberately not narrower than that
   (also proven live): move ANOTHER branch's ref with `git update-ref refs/heads/<other> <sha>` (refs/ is not
   scoped to the worker's own branch), or, in principle, tamper with a DIFFERENT concurrently-dispatched card's
   own worktree admin state under .git/worktrees/<other-task-id> (the whole directory is mounted, not one
   subdirectory, per point 3 above), or overwrite the bytes of an EXISTING loose object under .git/objects with
   corrupt data (objects/ is the one shared, single store every worktree and every branch reads from -- this is
   inherent to how git worktrees share one repository, not something a narrower mount could avoid while still
   letting a worker commit). Every one of these is caught downstream, not prevented at the mount: moving the
   INTEGRATION branch's own ref is caught immediately by guards.check_primary_checkout (it compares the primary
   checkout's live HEAD, which reads that same shared ref, against the expected_head the controller already knew
   -- proven live: `git update-ref refs/heads/main <sha>` from inside the sandbox is immediately visible as
   `git rev-parse HEAD` in the primary checkout); a tampered CARD branch is caught by the base-commit check
   (guards.check_card_base / _branch_created_from, reading that branch's own reflog) the next time its card is
   evaluated, and by the merge queue's own Gate 1 / Gate 3 re-checks (mergeq.py) before anything from a tampered
   branch could ever be merged, whatever the tampering; a corrupted loose object is caught the next time anything
   reads it, because git verifies a loose object's hash against its own content on read and refuses it with an
   explicit error ("corrupt loose object") rather than returning the corrupt bytes silently -- plausibly during
   one of those same downstream gate/checkout reads. None of the writable five reaches `.git/config` or
   `.git/hooks` (both stay under the read-only base /.git mount), so a worker cannot plant a hook or change repo
   config that way.
"""
from __future__ import annotations

import dataclasses
import fnmatch
import math
import ntpath
import os
import pathlib
import posixpath
import re
import subprocess
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, NamedTuple

import yaml

from . import events as events_mod
from . import procenv as procenv_mod

WORKSPACE = "/workspace"
_DOCKER = "docker"
# Hermes drops these for a non-local backend instead of exporting them as TERMINAL_CWD (cli.py _CWD_PLACEHOLDERS).
_CWD_PLACEHOLDERS = (".", "auto", "cwd")

# Exit codes default_runner reports when the command never produced one of its own (shell conventions), so a
# caller deals in one failure shape and nothing raises.
RC_TIMEOUT = 124
RC_ERROR = 126
RC_NOT_FOUND = 127

# Each mask is one --mount argument of roughly 150 characters, and a Windows command line tops out near 32,000
# characters. More sensitive files than this is refused instead of silently leaving some unmasked.
MAX_MASKS = 64

_INFO_TIMEOUT = 20
_RUN_TIMEOUT = 120

# A name that looks like a credential never crosses into the sandbox (ASES-SEC-06, ASES-CFG-04).
_CREDENTIAL_TOKENS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")
# These four are always used with fullmatch: `$` would accept a trailing newline ("python:3\n", "CI\n").
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# The first character rules out a leading '-', which docker would read as a flag.
_IMAGE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@-]*")
_DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}")
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


class SandboxConfigError(Exception):
    """A sandbox: value is unknown or malformed, or a docker command would break the sandbox rules."""


class SandboxInfrastructureError(Exception):
    """Round 9 (ASES-QG-04, ASES-SEC-03): the sandbox switch is on but a gate run could not even start -- Docker
    is unreachable, the pinned image is not present locally, or the docker argv could not be built. Raised by
    the runner sandbox_command_runner hands to gates.run_gate, and deliberately NOT a SandboxConfigError: this is
    an infrastructure failure, never a red gate, and a caller must catch this exact type to record a clean, visible
    infrastructure event and hold the card or merge, never fail the task and never fall back to the host silently
    (see gates.resolve_runner and each gate caller's own exception handling)."""


def looks_like_credential(name: str) -> bool:
    """ASES-SEC-06 / ASES-CFG-04: does an environment variable NAME look like it holds a credential? Deliberately
    broad (MONKEY matches KEY): a false positive here costs one renamed variable, a false negative puts a
    provider key into a worker's terminal."""
    upper = str(name).upper()
    return any(token in upper for token in _CREDENTIAL_TOKENS)


def _ascii(text: object) -> str:
    """Anything a person reads in a terminal must survive a cp1252 console, and a path, an image name or a
    stderr line can carry any character."""
    return str(text).encode("ascii", "backslashreplace").decode("ascii")


def _short(value: object, limit: int = 80) -> str:
    text = _ascii(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _first_line(text: str, limit: int = 160) -> str:
    for line in str(text).splitlines():
        if line.strip():
            return _short(line.strip(), limit)
    return ""


def _text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value if isinstance(value, str) else ""


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _plain_number(value: float) -> int | float:
    """2.0 becomes 2, so the emitted block reads container_cpu: 2 exactly as table 33 does."""
    return int(value) if float(value).is_integer() else float(value)


# ---------------------------------------------------------------------------------------------------------------
# The policy
# ---------------------------------------------------------------------------------------------------------------

_BLOCK_KEYS = frozenset({
    "terminal_backend", "network_default", "mount", "forward_env", "network_exceptions",
    # Not in Appendix B, which has no way to name the image or the limits, but the dataclass needs them:
    "image", "cpu", "memory_mb", "pids_limit", "extra_deny",
})


def _as_str_tuple(field: str, value: object) -> tuple[str, ...]:
    """A list or tuple of strings. Not any iterable: a dict would pass as its keys and a bare string as characters."""
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise SandboxConfigError(f"{field} must be a list of strings")
    return tuple(value)


@dataclasses.dataclass(frozen=True)
class SandboxPolicy:
    """What a worker sandbox may do (ASES-SEC-03, -05, -07). Validated on construction, so a value that would
    quietly switch a limit off (cpu 0 means unlimited to docker) cannot exist.

    network is the permission for one run: from_config never sets it (a global "network on" is not an explicit
    task-scoped exception, ASES-SEC-05), so a task-scoped exception is dataclasses.replace(policy, network=True).
    forward_env names variables the policy lets through; a credential-shaped name is refused even there.
    extra_deny holds extra FILE-NAME patterns (like *.kdbx) masked in addition to sensitive_name_patterns().
    """
    image: str
    cpu: float = 2
    memory_mb: int = 4096
    pids_limit: int = 512
    network: bool = False
    forward_env: tuple[str, ...] = ()
    extra_deny: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.image, str):
            raise SandboxConfigError("image must be a string")
        if not _is_number(self.cpu) or self.cpu <= 0:
            raise SandboxConfigError("cpu must be a positive number")
        for name in ("memory_mb", "pids_limit"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise SandboxConfigError(f"{name} must be a positive integer")
        if not isinstance(self.network, bool):
            raise SandboxConfigError("network must be true or false")
        forward = _as_str_tuple("forward_env", self.forward_env)
        for name in forward:
            if not _ENV_NAME_RE.fullmatch(name):
                raise SandboxConfigError(f"forward_env entry {_short(name)!r} is not a variable name")
            if looks_like_credential(name):
                raise SandboxConfigError(
                    f"forward_env entry {name} looks like a credential and can never enter the sandbox"
                )
        deny = _as_str_tuple("extra_deny", self.extra_deny)
        for pattern in deny:
            if not pattern.strip() or "/" in pattern or "\\" in pattern:
                raise SandboxConfigError(
                    f"extra_deny entry {_short(pattern)!r} must be a file-name pattern, not a path"
                )
        object.__setattr__(self, "forward_env", forward)
        object.__setattr__(self, "extra_deny", deny)

    @classmethod
    def from_config(cls, cfg: dict) -> "SandboxPolicy":
        """Read the `sandbox:` block of Appendix B (table 37): terminal_backend, network_default, mount,
        forward_env, network_exceptions, plus the optional image, cpu, memory_mb, pids_limit and extra_deny.

        cfg is the parsed config document (the block is cfg["sandbox"]) or the block itself. No block at all gives
        the safe defaults. Anything unknown or malformed raises SandboxConfigError, and so do the values that
        would loosen the sandbox: a backend other than docker, a mount other than worktree_only, and
        network_default true (ASES-SEC-05 and -07: network access is default-deny and opened only by an explicit
        task-scoped exception, never by a global default)."""
        if not isinstance(cfg, dict):
            raise SandboxConfigError("the sandbox config must be a mapping")
        if "sandbox" in cfg:
            block = cfg["sandbox"]
        elif any(key in cfg for key in _BLOCK_KEYS):
            block = cfg
        else:
            block = {}
        if block is None:
            block = {}
        if not isinstance(block, dict):
            raise SandboxConfigError("sandbox: must be a mapping")
        unknown = sorted(str(key) for key in set(block) - _BLOCK_KEYS)
        if unknown:
            raise SandboxConfigError(f"unknown sandbox key(s): {', '.join(_short(k) for k in unknown)}")
        if block.get("terminal_backend", "docker") != "docker":
            raise SandboxConfigError("sandbox.terminal_backend must be 'docker' (ASES-SEC-03)")
        network_default = block.get("network_default", False)
        if not isinstance(network_default, bool):
            raise SandboxConfigError("sandbox.network_default must be true or false")
        if network_default:
            raise SandboxConfigError(
                "sandbox.network_default must be false: network access is only opened by an explicit "
                "task-scoped exception (ASES-SEC-05, ASES-SEC-07)"
            )
        if block.get("mount", "worktree_only") != "worktree_only":
            raise SandboxConfigError("sandbox.mount must be 'worktree_only' (ASES-SEC-03)")
        if block.get("network_exceptions", "explicit_allowlist") != "explicit_allowlist":
            raise SandboxConfigError("sandbox.network_exceptions must be 'explicit_allowlist' (ASES-SEC-05)")
        kwargs: dict[str, Any] = {"image": block.get("image", "")}
        for key in ("cpu", "memory_mb", "pids_limit", "forward_env", "extra_deny"):
            if key in block:
                kwargs[key] = block[key]
        return cls(**kwargs)


# WORKERGIT (module docstring): the docker_volumes entries a worker's Docker terminal needs to commit inside its
# own dispatched linked worktree. `${env:TERMINAL_CWD}` is Hermes's own config-string expansion of the dispatched
# worktree's absolute host path (see the module docstring for why this is correct per-task and per-project with
# no rewrite); the literal suffix walks up from a canonical <repo>/.worktrees/<task_id> worktree to <repo>/.git.
# A tuple, not a function of any argument: nothing here is specific to one repository or one card.
_GIT_WORKTREE_CWD_REF = "${env:TERMINAL_CWD}"
_GIT_WORKTREE_UP = "/../../.git"
GIT_WORKTREE_VOLUMES: tuple[str, ...] = (
    f"{_GIT_WORKTREE_CWD_REF}{_GIT_WORKTREE_UP}:/.git:ro",
    f"{_GIT_WORKTREE_CWD_REF}{_GIT_WORKTREE_UP}/objects:/.git/objects",
    f"{_GIT_WORKTREE_CWD_REF}{_GIT_WORKTREE_UP}/refs:/.git/refs",
    f"{_GIT_WORKTREE_CWD_REF}{_GIT_WORKTREE_UP}/logs:/.git/logs",
    f"{_GIT_WORKTREE_CWD_REF}{_GIT_WORKTREE_UP}/worktrees:/.git/worktrees",
)


def git_worktree_volumes() -> tuple[str, ...]:
    """WORKERGIT: the five `docker_volumes` entries every Docker-backend worker profile needs so its own `git`
    can commit inside the linked worktree Hermes dispatches it into (module docstring, WORKERGIT section, for the
    full mechanism and what stays writable). A plain accessor for GIT_WORKTREE_VOLUMES so callers never need the
    module-level constant's name; check_terminal_block uses the same constant to verify a profile carries them."""
    return GIT_WORKTREE_VOLUMES


def terminal_block(policy: SandboxPolicy) -> dict:
    """ASES-SEC-03, ASES-SEC-05, ASES-CFG-04: the `terminal:` block for a worker profile's config.yaml (table 33).

    Every key exists in Hermes 0.21.3. Two differences from the table, both from reading the Hermes source (see
    the module docstring, items 4 and 5): no `cwd: /workspace`, because that key stops the worktree being
    mounted (Hermes starts the container in /workspace by itself), and an added
    docker_persist_across_processes: false, which is what makes "only its worktree mounted" true across cards
    (otherwise Hermes re-attaches to the previous card's container and its old /workspace mount). There is no
    PID-limit key to emit: Hermes applies its own fixed --pids-limit 256.

    WORKERGIT: docker_volumes carries git_worktree_volumes(), unconditionally -- every profile this block is
    written into is a worker Hermes dispatches into a linked worktree (ASES-SEC-03's own "only its worktree
    mounted" already assumes that), so there is no profile that would want the Docker backend without also being
    able to commit inside it. A caller with extra mounts of its own merges them in afterward, same as any other
    key here (see profiles.py's _terminal_rows, which reports rather than deletes a key ASES does not manage)."""
    return {
        "backend": "docker",
        "docker_image": policy.image,
        "docker_mount_cwd_to_workspace": True,
        "docker_run_as_host_user": True,
        "docker_forward_env": list(policy.forward_env),
        "docker_network": bool(policy.network),
        "container_cpu": _plain_number(policy.cpu),
        "container_memory": policy.memory_mb,
        "docker_persist_across_processes": False,
        "docker_volumes": list(GIT_WORKTREE_VOLUMES),
    }


# ---------------------------------------------------------------------------------------------------------------
# Paths: the sensitive-path model. Lexical only, no filesystem access.
# ---------------------------------------------------------------------------------------------------------------

# Relative to the home directory. The Windows browser profiles and the POSIX ones are both always listed: a path
# of the other platform never matches, and a fixed list keeps the result independent of where this runs.
# .hermes and AppData/Local/hermes hold every profile's auth.json and .env (provider keys); .claude and the two
# GitHub CLI folders hold tokens too.
_SENSITIVE_HOME_RELATIVE = (
    ".ssh", ".aws", ".azure", ".config/gcloud", ".kube", ".docker/config.json", ".gnupg",
    ".npmrc", ".pypirc", ".netrc", ".git-credentials",
    "AppData/Local/Google/Chrome/User Data", "AppData/Local/Microsoft/Edge/User Data",
    "AppData/Roaming/Mozilla/Firefox", ".config/google-chrome", ".mozilla",
    ".hermes", "AppData/Local/hermes", ".claude", ".config/gh", "AppData/Roaming/GitHub CLI",
)

# .envrc is covered by the blueprint's ".env*". Note secrets.* also matches a source file such as secrets.py, which
# a mask would blank inside the sandbox; the patterns are the blueprint's, the collateral is documented, not hidden.
_SENSITIVE_NAME_PATTERNS = (
    ".env", ".env.*", ".envrc", "*.pem", "*.key", "id_rsa*", "id_ed25519*", "id_ecdsa*", "id_dsa*",
    "*.p12", "*.pfx", "credentials.json", "secrets.*",
)

_DRIVE_RE = re.compile(r"^[A-Za-z]:")
# Git Bash spells C:\Users as /c/Users, WSL as /mnt/c/Users and Cygwin as /cygdrive/c/Users; all name C:\Users.
_MSYS_DRIVE_RE = re.compile(r"^/(?:mnt/|cygdrive/)?([A-Za-z])(?:/(.*))?$")
# The Win32 device prefix, in either slash style: \\?\C:\x is C:\x and \\?\UNC\server\share is \\server\share.
_DEVICE_PREFIX_RE = re.compile(r"^[\\/]{2}[?.][\\/]")
_ADMIN_SHARE_RE = re.compile(r"[A-Za-z]\$")
# //host/C$/x with forward slashes: only this shape is read as a Windows share, because //x/y is an ordinary POSIX path.
_FORWARD_ADMIN_UNC_RE = re.compile(r"^//[^/]+/[A-Za-z]\$(/|$)")


class _Canon(NamedTuple):
    """A path reduced to comparable parts. flavour is 'nt' (drive letter, UNC or backslashes) or 'posix'."""
    flavour: str
    drive: str
    anchored: bool
    parts: tuple[str, ...]


def _canon(path: object, *, admin_shares: bool = False) -> _Canon:
    """Normalise a path lexically: '.' and '..' collapsed (so '~/x/../.ssh' is seen as '~/.ssh'), both slash styles
    accepted, MSYS, WSL and Cygwin drive spellings and the Win32 device prefix mapped to a plain drive path. '..'
    cannot climb above the root. Windows also ignores trailing dots and spaces in a name and reads 'name:stream'
    as the name itself, so '.ssh.' and '.ssh::$INDEX_ALLOCATION' are '.ssh'.

    admin_shares maps \\\\host\\C$\\x to C:\\x, whatever the host. The DENY rules ask for it (\\\\localhost\\C$ is the
    local C: drive, so it must not hide ~/.ssh); the ALLOW rule does not, so a share can never be mistaken for
    the worktree."""
    text = (os.fspath(path) if isinstance(path, os.PathLike) else str(path)).strip()
    if _DEVICE_PREFIX_RE.match(text):
        text = text[4:]
        if text[:4].upper() in ("UNC\\", "UNC/"):
            text = "\\\\" + text[4:]
    if _FORWARD_ADMIN_UNC_RE.match(text):
        text = text.replace("/", "\\")
    msys = _MSYS_DRIVE_RE.match(text)
    if msys:
        text = f"{msys.group(1).upper()}:\\{msys.group(2) or ''}"
    if _DRIVE_RE.match(text) or text.startswith("\\\\") or "\\" in text:
        norm = ntpath.normpath(text)
        drive, rest = ntpath.splitdrive(norm)
        names = (part.split(":", 1)[0].rstrip(". ") for part in re.split(r"[\\/]+", rest))
        parts = tuple(name for name in names if name)
        # A UNC share (\\server\share) is a root in its own right even with nothing after it.
        anchored = rest.startswith(("\\", "/")) or drive.startswith("\\\\")
        if admin_shares and drive.startswith("\\\\") and _ADMIN_SHARE_RE.fullmatch(re.split(r"[\\/]", drive)[-1]):
            drive = re.split(r"[\\/]", drive)[-1][0] + ":"
        return _Canon("nt", drive.lower(), anchored, parts)
    norm = posixpath.normpath(text) if text else "."
    return _Canon("posix", "", norm.startswith("/"), tuple(part for part in norm.split("/") if part))


def _is_absolute(canon: _Canon) -> bool:
    return canon.anchored and (canon.flavour == "posix" or bool(canon.drive))


def _within(child: _Canon, parent: _Canon, *, fold: bool = False) -> bool:
    """child is parent or lies below it. Windows-style paths always compare case-insensitively; a POSIX-style path
    only when fold is set, because the allow rule (inside the worktree) must stay strict on a case-sensitive
    filesystem while the deny rules (a sensitive path) should be as broad as possible."""
    if (child.flavour, child.drive, child.anchored) != (parent.flavour, parent.drive, parent.anchored):
        return False
    count = len(parent.parts)
    if len(child.parts) < count:
        return False
    if fold or child.flavour == "nt":
        return all(a.lower() == b.lower() for a, b in zip(child.parts[:count], parent.parts))
    return child.parts[:count] == parent.parts


# Locations that are never a legitimate worker mount, wherever they are. A container that can reach the engine's
# socket can start another container with any host path mounted (host root in all but name), and the system
# directories hold host credentials and devices. POSIX spellings: on a Windows host they name the Docker VM, which
# holds the same socket. (/dev/shm as a worktree is refused too; use /tmp.)
_SYSTEM_PATHS = (
    "/var/run/docker.sock", "/run/docker.sock", "/var/run/podman", "/run/podman", "/var/lib/docker",
    "/etc", "/root", "/proc", "/sys", "/dev", "/boot",
)
_ENGINE_PIPE_NAMES = ("docker_engine", "dockerdesktoplinuxengine")
_SYSTEM_CANON = tuple((shown, _canon(shown)) for shown in _SYSTEM_PATHS)


def _as_pure(path: object) -> pathlib.PurePath:
    if isinstance(path, pathlib.PurePath):
        return path
    text = str(path)
    if _DRIVE_RE.match(text) or text.startswith("\\\\") or "\\" in text:
        return pathlib.PureWindowsPath(text)
    return pathlib.PurePosixPath(text)


def sensitive_host_paths(home: object) -> list[pathlib.PurePath]:
    """ASES-SEC-02: host locations an agent must never read through the sandbox: SSH keys, cloud credential
    folders, container and package-registry credentials, browser profiles and the Hermes and Claude homes. A
    pathlib.Path home gives Paths back, a string gives pure paths of the matching flavour."""
    base = _as_pure(home)
    if not _is_absolute(_canon(base)):
        raise SandboxConfigError("home must be an absolute path")
    return [base.joinpath(*relative.split("/")) for relative in _SENSITIVE_HOME_RELATIVE]


def sensitive_name_patterns() -> tuple[str, ...]:
    """ASES-SEC-02: file-name patterns for .env files and key files, matched case-insensitively on the last name."""
    return _SENSITIVE_NAME_PATTERNS


def _name_pattern_hit(name: str, patterns: Iterable[str]) -> str | None:
    lowered = name.lower()
    for pattern in patterns:
        if fnmatch.fnmatchcase(lowered, pattern.lower()):
            return pattern
    return None


class _Home(NamedTuple):
    canon: _Canon
    sensitive: tuple[tuple[str, _Canon], ...]


def _home_context(home: object) -> _Home:
    base = _as_pure(home)
    canon = _canon(base)
    if not _is_absolute(canon):
        raise SandboxConfigError("home must be an absolute path")
    return _Home(canon, tuple(
        (f"~/{relative}", _canon(base.joinpath(*relative.split("/")))) for relative in _SENSITIVE_HOME_RELATIVE
    ))


def _sensitive_reason(canon: _Canon, home: _Home, patterns: Iterable[str]) -> str | None:
    for shown, sensitive in home.sensitive:
        if _within(canon, sensitive, fold=True):
            return f"is or lies inside {shown}"
    if canon.parts:
        hit = _name_pattern_hit(canon.parts[-1], patterns)
        if hit:
            return f"has a name that marks it sensitive ({hit})"
    return None


def is_sensitive_path(path: object, home: object, *, extra_patterns: Iterable[str] = ()) -> bool:
    """ASES-SEC-02: is path one of sensitive_host_paths(home), inside one, or named like a secret file? Pure path
    logic: '..' is normalised first, so ~/proj/../.ssh/id_rsa is caught."""
    patterns = (*_SENSITIVE_NAME_PATTERNS, *extra_patterns)
    return _sensitive_reason(_canon(path, admin_shares=True), _home_context(home), patterns) is not None


def _mount_hazard(canon: _Canon, home: _Home) -> str | None:
    """Why this host path must never be a mount, whatever the worktree is: not absolute, a drive root, the home
    directory or a parent of it, a sensitive path, or a parent of a sensitive path (which would hand the agent the
    sensitive path underneath)."""
    if not _is_absolute(canon):
        return "is not an absolute path"
    if not canon.parts:
        return "is a drive root"
    if _within(canon, home.canon, fold=True) and _within(home.canon, canon, fold=True):
        return "is the home directory"
    if _within(home.canon, canon, fold=True):
        return "is a parent of the home directory"
    reason = _sensitive_reason(canon, home, _SENSITIVE_NAME_PATTERNS)
    if reason:
        return reason
    last = canon.parts[-1].lower()
    if last.endswith(".sock") or last in _ENGINE_PIPE_NAMES:
        return "is a container-engine socket"
    for shown, system in _SYSTEM_CANON:
        # Not for a system path that holds the home itself (root's home is /root): the home rules above and the
        # sensitive list already guard what matters there, and a root-run controller keeps its worktrees in it.
        if _within(canon, system, fold=True) and not _within(home.canon, system, fold=True):
            return f"is or lies inside {shown}, which is never a worker mount"
    for shown, sensitive in (*home.sensitive, *_SYSTEM_CANON):
        if _within(sensitive, canon, fold=True):
            return f"is a parent of {shown}"
    return None


def _as_list(items: object) -> list:
    """A bare string or path is one item, not a sequence of characters."""
    if isinstance(items, (str, os.PathLike)):
        return [items]
    return list(items) if isinstance(items, Iterable) else [items]


def mount_problems(mounts: Iterable[object], worktree: object, home: object) -> list[str]:
    """ASES-SEC-02, ASES-SEC-03: is this mount list (host paths) acceptable? Only the worktree itself, or a path
    inside it, is. The home directory, a drive root, a sensitive path, a parent of the worktree and a parent of a
    sensitive path are each reported (first matching reason per mount). An empty list means acceptable.

    Lexical: '..' is collapsed, Windows-style paths compare case-insensitively, POSIX-style ones strictly."""
    home_ctx = _home_context(home)
    tree = _canon(worktree)
    if not _is_absolute(tree):
        return [_ascii(f"worktree {_short(worktree)} is not an absolute path")]
    problems: list[str] = []
    for raw in _as_list(mounts):
        canon = _canon(raw)
        shown = _short(raw)
        hazard = _mount_hazard(_canon(raw, admin_shares=True), home_ctx)
        if hazard:
            problems.append(f"mount {shown} {hazard}")
        elif _within(tree, canon) and not _within(canon, tree):
            problems.append(f"mount {shown} is a parent of the worktree")
        elif not _within(canon, tree):
            problems.append(f"mount {shown} is outside the worktree {_short(worktree)}")
    return [_ascii(problem) for problem in problems]


# ---------------------------------------------------------------------------------------------------------------
# Masks: overlay committed secret files with an empty file (test 22.10)
# ---------------------------------------------------------------------------------------------------------------


def sensitive_files_in(worktree: object, *, extra_patterns: Iterable[str] = ()) -> list[pathlib.Path]:
    """ASES-SEC-02: the files inside the worktree whose name matches sensitive_name_patterns() (plus
    extra_patterns), sorted, for mask_args. The walk does not follow symlinks and skips every .git directory. A
    symlink named like a secret is not returned: inside the container it can only point at a path in the mounted
    worktree (nothing else is mounted), where a real secret file is masked in its own right. A directory that
    cannot be listed raises, because a silently skipped folder is a secret left unmasked."""
    root = pathlib.Path(worktree)
    patterns = (*_SENSITIVE_NAME_PATTERNS, *extra_patterns)

    def unreadable(error: OSError) -> None:
        raise SandboxConfigError(f"cannot scan {_short(error.filename)} for sensitive files: {_short(error.strerror)}")

    found: list[pathlib.Path] = []
    for dirpath, dirnames, filenames in os.walk(str(root), onerror=unreadable, followlinks=False):
        dirnames[:] = [name for name in dirnames if name != ".git"]
        for name in filenames:
            if _name_pattern_hit(name, patterns) is None:
                continue
            path = pathlib.Path(dirpath) / name
            if not path.is_symlink():
                found.append(path)
    return sorted(found)


def _csv_field(value: str) -> str:
    """docker parses --mount as one CSV record, so a comma or a quote inside a value must be quoted."""
    if any(ch in value for ch in "\0\r\n"):
        raise SandboxConfigError("a mount path contains a control character")
    if any(ch in value for ch in ',"'):
        return '"' + value.replace('"', '""') + '"'
    return value


def _bind_spec(source: str, target: str, *, readonly: bool) -> str:
    """One --mount value. --mount (not -v) everywhere, so the colon in C:\\a\\b never confuses the parser, and a
    missing source is an error instead of a silently created empty directory."""
    fields = ["type=bind", f"source={source}", f"target={target}"] + (["readonly"] if readonly else [])
    return ",".join(_csv_field(field) for field in fields)


def _source_text(path: object) -> str:
    text = str(path)
    stripped = text.rstrip("\\/")
    return stripped if stripped and not stripped.endswith(":") else text


def mask_args(
    worktree: object, empty_file: object, *, extra_patterns: Iterable[str] = (), limit: int = MAX_MASKS,
) -> list[str]:
    """ASES-SEC-02, test 22.10 ("reading .env from inside the sandbox fails"): the `--mount type=bind,
    source=<empty file>,target=/workspace/<relative path>,readonly` arguments that lay an empty file over every
    sensitive file in the worktree, so a committed .env cannot be read through the mount.

    empty_file must exist and be empty: a mask source with content would publish it in place of every secret.
    More than `limit` sensitive files raises rather than truncating (see MAX_MASKS)."""
    empty = pathlib.Path(empty_file)
    if not empty.is_file():
        raise SandboxConfigError(f"the mask source {_short(empty)} does not exist")
    if empty.stat().st_size != 0:
        raise SandboxConfigError(f"the mask source {_short(empty)} is not empty")
    files = sensitive_files_in(worktree, extra_patterns=extra_patterns)
    if len(files) > limit:
        raise SandboxConfigError(
            f"{len(files)} files with sensitive names are in the worktree (limit {limit}): "
            "remove them before a sandboxed run instead of leaving some unmasked"
        )
    root = pathlib.Path(worktree)
    args: list[str] = []
    for path in files:
        target = f"{WORKSPACE}/{path.relative_to(root).as_posix()}"
        args += ["--mount", _bind_spec(str(empty), target, readonly=True)]
    return args


# ---------------------------------------------------------------------------------------------------------------
# The terminal block checker
# ---------------------------------------------------------------------------------------------------------------


def _unpinned_reason(image: str) -> str | None:
    """None when the image is pinned (a tag other than latest, or a full @sha256 digest), else why it is not."""
    digest = _DIGEST_RE.search(image)
    if digest and digest.end() == len(image):
        return None
    if "@" in image:
        return "its digest is not a full sha256"
    name = image.rsplit("/", 1)[-1]
    if ":" not in name or not name.rsplit(":", 1)[1]:
        return "it has no tag"
    if name.rsplit(":", 1)[1].lower() == "latest":
        return "its tag is 'latest'"
    return None


def _image_problems(terminal: dict) -> list[str]:
    image = terminal.get("docker_image")
    if not isinstance(image, str) or not image.strip():
        return ["docker_image is missing"]
    image = image.strip()
    if not _IMAGE_RE.fullmatch(image):
        return [f"docker_image {_short(image)!r} is not a valid image reference"]
    reason = _unpinned_reason(image)
    if reason:
        return [f"docker_image {_short(image)!r} is not pinned ({reason}; use a fixed tag or an @sha256 digest)"]
    return []


def _limit_problems(terminal: dict, policy: SandboxPolicy) -> list[str]:
    problems = []
    for key, limit, label in (
        ("container_cpu", policy.cpu, "CPU"), ("container_memory", policy.memory_mb, "memory"),
    ):
        value = terminal.get(key)
        if value is None:
            problems.append(f"{key} is not set (no explicit {label} limit)")
        elif not _is_number(value) or value <= 0:
            problems.append(f"{key} is {_short(value)}, not a positive number (no {label} limit)")
        elif value > limit:
            problems.append(f"{key} {value} exceeds the policy limit {_plain_number(limit)}")
    return problems


def _name_list_problems(key: str, value: object, policy: SandboxPolicy) -> list[str]:
    """docker_forward_env and env_passthrough: names of host variables that would reach the container."""
    if not isinstance(value, list):
        return [f"{key} is not a list"]
    problems = []
    for item in value:
        if not isinstance(item, str):
            problems.append(f"{key} has an entry that is not a string")
        elif looks_like_credential(item):
            problems.append(f"{key} forwards credential-shaped name {_short(item)}")
        elif item not in policy.forward_env:
            problems.append(f"{key} forwards {_short(item)}, which the policy does not allow")
    return problems


def _env_channel_problems(terminal: dict, policy: SandboxPolicy) -> list[str]:
    problems = []
    if "docker_forward_env" in terminal:
        problems += _name_list_problems("docker_forward_env", terminal["docker_forward_env"], policy)
    if "env_passthrough" in terminal:
        problems += _name_list_problems("env_passthrough", terminal["env_passthrough"], policy)
    if "docker_env" in terminal:
        docker_env = terminal["docker_env"]
        if not isinstance(docker_env, dict):
            problems.append("docker_env is not a mapping")
        else:
            for name, value in docker_env.items():
                if looks_like_credential(name):
                    problems.append(f"docker_env sets credential-shaped name {_short(name)}")
                elif isinstance(value, str) and events_mod.redact({"value": value})["value"] != value:
                    problems.append(f"docker_env value for {_short(name)} looks like a secret")
    files = terminal.get("credential_files")
    if files is not None and not isinstance(files, list):
        problems.append("credential_files is not a list")
    elif files:
        problems.append("credential_files would mount host credential files into the sandbox")
    return problems


def _split_volume(spec: str) -> tuple[str, str, str]:
    """(host, container, options) of one docker_volumes entry; host is '' when it has no colon (Hermes skips it).
    A leading drive letter keeps its own colon, so C:\\data:/data:ro splits as C:\\data, /data, ro."""
    match = re.match(r"^((?:[\\/]{2}[?.][\\/])?[A-Za-z]:[\\/][^:]*|[^:]*)(?::(.*))?$", spec.strip())
    if not match or match.group(2) is None:
        return "", "", ""
    container, _, options = match.group(2).partition(":")
    return match.group(1), container, options


def _expand_home(host: str, home: str) -> str:
    return home + host[1:] if host == "~" or host.startswith(("~/", "~\\")) else host


def _path_like(host: str) -> bool:
    """A named docker volume is not a host path; anything that spells a location is."""
    return host.startswith(("/", "~", "./", "../", ".\\", "..\\", "\\\\")) or bool(_DRIVE_RE.match(host))


_TEMPLATE_RE = re.compile(r"\$\{[^}]*\}")
_TEMPLATE_MARKER = "~"


def _templated_candidate_hosts(entry: str) -> list[str]:
    """WORKERGIT: entry may reference one or more Hermes `${env:VAR}` templates ANYWHERE in its host text, not
    only as a leading prefix -- Hermes's own config-string expansion substitutes every `${env:VAR}` occurrence in
    a string value wherever it sits (module docstring), so `/data/${env:USER}/.ssh:/x:ro` is exactly as real a
    shape as GIT_WORKTREE_VOLUMES' own leading `${env:TERMINAL_CWD}/...`. Two things break for any templated
    shape without this: `_split_volume` reads left to right, so a ':' embedded inside `${env:VAR}` is mistaken
    for the host/container separator and the real host text is lost; and `_path_like` never recognises a string
    starting with '$' as a path at all. Together, a templated entry previously reached NO sensitive-path check
    below, whatever host location it named, while the identical location spelled literally is caught (see
    SSH_SPELLINGS-style tests).

    Hermes resolves `${env:VAR}` from whatever environment the caller merging the mount in supplies, which this
    static config check cannot predict (config.py's `_env_ref_lookup` places no allow-list on the variable
    name), so the safe reading is the conservative one: replace EVERY `${...}` occurrence in entry with the
    marker '~' first (this alone fixes the colon-confusion above, since none of the embedded colons survive),
    then split the result exactly like an ordinary template-free entry to recover its real host text -- now a
    path with a literal '~' standing in for each unresolved template, wherever it sits.

    A sensitive segment can sit BETWEEN two templates just as easily as after the last one (an entry with
    multiple `${...}` references, e.g. `/home/${env:USER}/.ssh/${env:TMP}/scratch:/mnt`, only reaches `.ssh`
    once its first template resolves; the second one is irrelevant to that) -- so this returns one candidate
    PER marker occurrence in the host, each built by treating that one occurrence as if it were the home
    directory (the same conservative anchor a bare literal '~/...' mount already gets) and keeping everything
    from there on, including any further '~' markers or literal text: `_mount_hazard` below then judges each
    candidate independently, and the entry is flagged if ANY of them is hazardous. Whatever comes BEFORE a given
    marker occurrence is dropped for THAT candidate only (a different candidate, anchored at an earlier or later
    marker, still covers it) -- not a loss, since the point is "does this occurrence, or anything after it,
    reach a sensitive location", not "what canonical path does the whole templated entry resolve to" (unknowable
    without the real environment).

    Returns [] when entry has no `${...}` at all (the caller's own literal-path handling covers that case) or
    the host side collapses to something falsy or with no marker in it (`_split_volume` still could not make
    sense of it even once every embedded colon is gone -- not a well-formed volumes entry)."""
    if not _TEMPLATE_RE.search(entry):
        return []
    collapsed = _TEMPLATE_RE.sub(_TEMPLATE_MARKER, entry)
    host, _container, _options = _split_volume(collapsed)
    if not host or _TEMPLATE_MARKER not in host:
        return []
    candidates = []
    start = 0
    while True:
        index = host.find(_TEMPLATE_MARKER, start)
        if index == -1:
            break
        suffix = host[index + 1:]
        if suffix == "" or suffix.startswith(("/", "\\")):
            candidates.append(_TEMPLATE_MARKER + suffix)
        start = index + 1
    return candidates


def _volume_problems(volumes: object, home: object) -> list[str]:
    if not isinstance(volumes, list):
        return ["docker_volumes is not a list"]
    home_ctx = _home_context(home)
    problems = []
    for entry in volumes:
        if not isinstance(entry, str):
            problems.append("docker_volumes has an entry that is not a string")
            continue
        if ":" not in entry:
            continue  # Hermes skips an entry with no colon
        host, _container, _options = _split_volume(entry)
        shown = _short(entry)
        for templated in _templated_candidate_hosts(entry):
            hazard = _mount_hazard(_canon(_expand_home(templated, str(home)), admin_shares=True), home_ctx)
            if hazard:
                problems.append(f"docker_volumes entry {shown} {hazard}")
        # Hermes tests the substring ':/workspace' anywhere in a volume and then skips the worktree mount.
        if ":/workspace" in entry:
            problems.append(f"docker_volumes entry {shown} targets /workspace, so Hermes skips the worktree mount")
        if host and _path_like(host):
            hazard = _mount_hazard(_canon(_expand_home(host, str(home)), admin_shares=True), home_ctx)
            if hazard:
                problems.append(f"docker_volumes entry {shown} {hazard}")
    return problems


_GIT_PATH_SEGMENT_RE = re.compile(r"(?:^|[\\/])\.git(?:[\\/:]|$)", re.IGNORECASE)


def _mentions_git_dir(entry: str) -> bool:
    """WORKERGIT: does entry (a whole docker_volumes string, host or container side, templated or literal) name
    a `.git` path component anywhere? A `.git` segment can end in a slash, at the end of the string, or at the
    ':' that separates a docker_volumes entry's host/container/options fields (so a bare ".../.git:/.git:ro"
    matches on both sides). Used only to flag an UNEXPECTED entry that reaches into git internals: the five
    GIT_WORKTREE_VOLUMES entries are matched by exact string first (_git_worktree_volume_problems) and never
    reach this at all."""
    return bool(_GIT_PATH_SEGMENT_RE.search(entry))


def _git_worktree_volume_problems(volumes: object) -> list[str]:
    """WORKERGIT (ASES-SEC-03; module docstring, WORKERGIT section): docker_volumes must carry every one of
    git_worktree_volumes(), verbatim -- a worker's own `git status`/`add`/`commit` inside its dispatched worktree
    needs all five -- and nothing else may reach into `.git` at all: an extra entry that duplicates one of the
    five with different flags (the base mount missing ':ro', for example) or reaches a path the five
    deliberately leave read-only (.git/config, .git/hooks) is refused, whether it is one of Hermes's literal
    shapes or a ${env:...} template of its own. Unconditional (called on every terminal block, docker_volumes
    key present or not): every profile this checker is asked about is a Docker-backend worker Hermes dispatches
    into a linked worktree, so there is no profile that legitimately has none of the five.

    Silent (returns []) when volumes is not a list: _volume_problems already reports "docker_volumes is not a
    list" in that case, and there is nothing more useful to say about a value that is not even the right shape."""
    if not isinstance(volumes, list):
        return []
    present = [entry for entry in volumes if isinstance(entry, str)]
    problems = [
        f"docker_volumes is missing the git mount {expected!r} (WORKERGIT: a worker could not commit inside "
        "its dispatched worktree without it)"
        for expected in GIT_WORKTREE_VOLUMES if expected not in present
    ]
    problems += [
        f"docker_volumes entry {_short(entry)!r} reaches into .git outside the WORKERGIT git mounts, which is "
        "not allowed (it could widen what a worker can read or write there)"
        for entry in present if entry not in GIT_WORKTREE_VOLUMES and _mentions_git_dir(entry)
    ]
    return problems


def _ok_user(value: str) -> bool:
    """Not root. docker reads the part before ':' as a name or a number, and any spelling of zero ('0', '00',
    '+0') is uid 0."""
    user = value.split(":", 1)[0].strip().lower()
    if not user or user == "root":
        return False
    return not (re.fullmatch(r"[+-]?[0-9]+", user) and int(user) == 0)


def _ok_pids(value: str, policy: SandboxPolicy) -> bool:
    """A positive integer no higher than the policy: 0 and -1 mean unlimited to docker."""
    return value.isascii() and value.isdigit() and 0 < int(value) <= policy.pids_limit


# What may follow docker_extra_args. The table is an allow-list on purpose: the value checks are what stop
# --pids-limit from meaning unlimited or --user from meaning root, and --cpus, --memory, -v, --mount, -e,
# --env-file, --privileged, --cap-add, --network, --pid, --device and --security-opt are all absent.
_EXTRA_FLAGS: dict[str, tuple[bool, Callable[[str, SandboxPolicy], bool] | None]] = {
    "--pids-limit": (True, _ok_pids),
    "--shm-size": (True, lambda v, p: re.fullmatch(r"\d+[bkmgBKMG]?", v) is not None),
    "--ulimit": (True, lambda v, p: re.fullmatch(r"[a-z]+=\d+(:\d+)?", v) is not None),
    "--hostname": (True, lambda v, p: _NAME_RE.fullmatch(v) is not None),
    "--label": (True, lambda v, p: re.fullmatch(r"[A-Za-z0-9_.-]+(=.*)?", v) is not None),
    "--user": (True, lambda v, p: _ok_user(v)),
    "-u": (True, lambda v, p: _ok_user(v)),
    "--init": (False, None),
}


def _extra_args_problems(args: object, policy: SandboxPolicy) -> list[str]:
    """docker_extra_args goes to `docker run` verbatim and last, so it can undo the sandbox; see _EXTRA_FLAGS."""
    if not isinstance(args, list):
        return ["docker_extra_args is not a list"]
    problems = []
    index = 0
    while index < len(args):
        token = args[index]
        index += 1
        if not isinstance(token, str):
            problems.append("docker_extra_args has an entry that is not a string")
            continue
        if not token.startswith("-"):
            problems.append(
                f"docker_extra_args has the bare word {_short(token)!r} where a flag is expected "
                "(it could replace the image)"
            )
            continue
        flag, equals, inline = token.partition("=")
        spec = _EXTRA_FLAGS.get(flag)
        if spec is None:
            problems.append(f"docker_extra_args has {_short(flag)}, which is not on the allow-list")
            if not equals and index < len(args) and isinstance(args[index], str) and not args[index].startswith("-"):
                index += 1  # its value, so it is not also reported as a bare word
            continue
        takes_value, check = spec
        if not takes_value:
            if equals:
                problems.append(f"docker_extra_args gives {flag} a value it does not take")
            continue
        if equals:
            value = inline
        elif index < len(args) and isinstance(args[index], str):
            value = args[index]
            index += 1
        else:
            problems.append(f"docker_extra_args has {flag} without a value")
            continue
        if check is not None and not check(value, policy):
            problems.append(
                f"docker_extra_args gives {flag} the value {_short(value)!r}, which the policy does not allow"
            )
    return problems


def check_terminal_block(terminal: object, policy: SandboxPolicy, *, home: object = None) -> list[str]:
    """ASES-SEC-03, ASES-SEC-02, ASES-SEC-05, ASES-SEC-06, ASES-CFG-04: what is wrong with a worker profile's
    `terminal:` block? Empty means compliant. Each problem is one short sentence naming the key. A block whose
    backend is not docker returns that single problem, because none of the docker keys apply to it.

    Differences from a literal reading of table 33: a cwd key is a problem (module docstring, item 4) and
    docker_persist_across_processes must be false (item 5). It also refuses the ways a config can undo the
    sandbox (item 6): docker_extra_args outside a small allow-list, credential names or secret-shaped values in
    docker_env, env_passthrough or docker_forward_env, credential_files, a docker_volumes entry that reaches a
    sensitive path, the home directory, a drive root, a system path, a container-engine socket or ':/workspace',
    a shared container key, and snap compatibility (it drops no-new-privileges).

    home is the user's home directory for the volume check (default: the current user's)."""
    if not isinstance(terminal, dict):
        return ["terminal block is missing or not a mapping: the profile would use the local backend"]
    backend = terminal.get("backend")
    if backend != "docker":
        if backend is None:
            return ["backend is missing (Hermes then uses the local backend)"]
        return [_ascii(f"backend is {_short(backend)!r}, not 'docker'")]
    if home is None:
        try:
            home = pathlib.Path.home()
        except (RuntimeError, KeyError) as exc:
            raise SandboxConfigError("cannot determine the home directory; pass home=") from exc

    problems: list[str] = []
    if "cwd" in terminal and terminal["cwd"] not in _CWD_PLACEHOLDERS:
        problems.append(
            f"cwd is {_short(terminal['cwd'])!r}: Hermes exports it over the worktree the dispatcher pinned, "
            "so the worktree would not be mounted (leave cwd out, the container starts in /workspace)"
        )
    if terminal.get("docker_mount_cwd_to_workspace") is not True:
        problems.append("docker_mount_cwd_to_workspace is not true (the worktree would not be mounted at /workspace)")
    if terminal.get("docker_run_as_host_user") is not True:
        problems.append("docker_run_as_host_user is not true (the container would run as the image's default user)")
    if terminal.get("docker_persist_across_processes") is not False:
        problems.append(
            "docker_persist_across_processes is not false (Hermes would reuse one container per profile, "
            "keeping the first card's worktree mounted)"
        )
    problems += _env_channel_problems(terminal, policy)
    network = terminal.get("docker_network")
    if network is None:
        if not policy.network:
            problems.append("docker_network is not set (Hermes defaults to true, which allows network access)")
    elif not isinstance(network, bool):
        problems.append("docker_network is not true or false")
    elif network and not policy.network:
        problems.append("docker_network is true but the policy does not allow network access")
    problems += _limit_problems(terminal, policy)
    problems += _image_problems(terminal)
    if "docker_volumes" in terminal:
        problems += _volume_problems(terminal["docker_volumes"], home)
    problems += _git_worktree_volume_problems(terminal.get("docker_volumes", []))
    if "docker_extra_args" in terminal:
        problems += _extra_args_problems(terminal["docker_extra_args"], policy)
    if terminal.get("docker_shared_container_key"):
        problems.append("docker_shared_container_key is set (profiles would share one container)")
    if terminal.get("docker_snap_compat") is True:
        problems.append("docker_snap_compat is true (it drops no-new-privileges)")
    return [_ascii(problem) for problem in problems]


def check_profile_config(profile_config: object, policy: SandboxPolicy, *, home: object = None) -> list[str]:
    """ASES-SEC-03: check_terminal_block over a loaded profile config.yaml. terminal: sits at the top level of a
    Hermes config; a profile without it (every profile today) uses the local backend, which is a problem."""
    terminal = profile_config.get("terminal") if isinstance(profile_config, dict) else None
    if terminal is None:
        return ["terminal block is missing: the profile runs on the local backend with no sandbox"]
    return check_terminal_block(terminal, policy, home=home)


def load_profile_config(profile_dir: object) -> dict:
    """The profile's config.yaml as a dict ({} when the file is missing or empty). Anything unreadable or not a
    mapping raises SandboxConfigError; the message carries the exception type and line, never a snippet, because
    a config file holds provider keys and a YAML error would quote the line."""
    path = pathlib.Path(profile_dir) / "config.yaml"
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        raise SandboxConfigError(f"cannot read {_short(path)}: {type(exc).__name__}") from exc
    try:
        data = yaml.safe_load(text)
    except (yaml.YAMLError, ValueError) as exc:  # ValueError: a scalar such as the date 2001-13-45
        mark = getattr(exc, "problem_mark", None)
        where = f" near line {mark.line + 1}" if mark is not None else ""
        raise SandboxConfigError(f"{_short(path)} is not valid YAML{where}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise SandboxConfigError(f"{_short(path)} is not a mapping")
    return data


# ---------------------------------------------------------------------------------------------------------------
# docker run for the controller's own sandboxed runs
# ---------------------------------------------------------------------------------------------------------------


def _fmt_cpu(cpu: float) -> str:
    return str(_plain_number(cpu))


def _check_image(image: str) -> None:
    if not isinstance(image, str) or not _IMAGE_RE.fullmatch(image):
        raise SandboxConfigError(f"the sandbox image {_short(image)!r} is missing or not a valid image reference")


def docker_run_argv(
    policy: SandboxPolicy, worktree: object, command: str, *, container_name: str | None = None,
    user: str | None = None, env: dict[str, str] | None = None, network: bool | None = None,
    extra_mounts: Iterable[object] = (), empty_file: object = None, home: object = None,
) -> list[str]:
    """ASES-QG-04, ASES-SEC-02, ASES-SEC-03, ASES-SEC-05, ASES-SEC-06: the argv for one `docker run --rm` of
    `sh -lc <command>` with only the worktree mounted, for the controller's own gate runs.

    --network none unless network (default: policy.network) is true, then bridge; --cpus, --memory and
    --pids-limit from the policy; --security-opt no-new-privileges and --cap-drop ALL; --pull never, so a missing
    image is an error and never a download. Not --read-only: workers write /workspace and tmp. --user and --name
    only when given (see host_user_spec for the host user; keep the name to kill a container the runner timed out
    on, see remove_container_command).

    Mounts use --mount type=bind everywhere. They are the worktree at /workspace, one empty-file mask per
    sensitive file when empty_file is given (callers that run agent-written code MUST pass it; the exfiltration
    probe reads nothing and passes none), and extra_mounts. An extra mount must pass mount_problems, so it lies
    inside the worktree; it is bound read-only over the same relative path, which is what lets a caller freeze
    gate configuration, and the worktree itself as an extra mount makes the whole run read-only. The worktree
    itself must not be the home directory, a drive root, a sensitive path or a system path.

    Environment: ONLY the variables in env, and only when their names are plain and not credential-shaped. The
    parent's environment is never forwarded and --env-file is never used (ASES-SEC-06).

    Limit of "only the worktree is mounted": a `git worktree add` checkout has a .git FILE that names the main
    repository's .git directory by its host path (gitdir: C:/.../repo/.git/worktrees/wt). That path does not exist
    inside the container, so any command that calls git fails there. A gate that needs git should run on an export
    of the commit (git archive) instead of a linked worktree."""
    _check_image(policy.image)
    if not isinstance(command, str):
        raise SandboxConfigError("the sandbox command must be a string")
    if home is None:
        try:
            home = pathlib.Path.home()
        except (RuntimeError, KeyError) as exc:
            raise SandboxConfigError("cannot determine the home directory; pass home=") from exc
    tree = _canon(worktree)
    if not _is_absolute(tree):
        raise SandboxConfigError(f"the worktree {_short(worktree)} is not an absolute path")
    extras = _as_list(extra_mounts)
    problems = mount_problems([worktree], worktree, home) + mount_problems(extras, worktree, home)
    if problems:
        raise SandboxConfigError("; ".join(problems))
    if container_name is not None and not _NAME_RE.fullmatch(str(container_name)):
        raise SandboxConfigError(f"the container name {_short(container_name)!r} is not a plain name")
    net_on = policy.network if network is None else bool(network)

    argv = [_DOCKER, "run", "--rm", "--pull", "never"]
    if container_name is not None:
        argv += ["--name", str(container_name)]
    argv += [
        "--network", "bridge" if net_on else "none",
        "--cpus", _fmt_cpu(policy.cpu), "--memory", f"{policy.memory_mb}m", "--pids-limit", str(policy.pids_limit),
        "--security-opt", "no-new-privileges", "--cap-drop", "ALL",
    ]
    if user is not None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+(:[A-Za-z0-9_.-]+)?", str(user)):
            raise SandboxConfigError(f"the container user {_short(user)!r} is not a name or uid, or name:group")
        argv += ["--user", str(user)]
    # docker refuses two mounts at one target ("Duplicate mount point"), so the worktree given as an extra mount
    # makes the primary mount read-only instead, and a path given twice is mounted once.
    overlays: dict[tuple[str, ...], object] = {}
    for extra in extras:
        relative = _canon(extra).parts[len(tree.parts):]
        overlays.setdefault(tuple(part.lower() for part in relative) if tree.flavour == "nt" else relative, extra)
    argv += ["--mount", _bind_spec(_source_text(worktree), WORKSPACE, readonly=() in overlays)]
    if empty_file is not None:
        argv += mask_args(worktree, empty_file, extra_patterns=policy.extra_deny)
    for key, extra in overlays.items():
        if key:
            relative = "/".join(_canon(extra).parts[len(tree.parts):])
            argv += ["--mount", _bind_spec(_source_text(extra), f"{WORKSPACE}/{relative}", readonly=True)]
    argv += ["-w", WORKSPACE]
    for name, value in (env or {}).items():
        if not isinstance(name, str) or not _ENV_NAME_RE.fullmatch(name):
            raise SandboxConfigError(f"the environment name {_short(name)!r} is not a plain variable name")
        if looks_like_credential(name):
            raise SandboxConfigError(
                f"the environment name {name} looks like a credential and cannot enter the sandbox"
            )
        if "\0" in str(value):
            raise SandboxConfigError(f"the value of {name} contains a NUL character")
        argv += ["-e", f"{name}={value}"]
    argv += [policy.image, "sh", "-lc", command]
    return argv


def host_user_spec(*, getuid: Callable[[], int] | None = None, getgid: Callable[[], int] | None = None) -> str | None:
    """ASES-SEC-03: '<uid>:<gid>' of the host user for docker_run_argv(user=...), or None where the platform has no
    POSIX ids (native Windows), in which case the container runs as the image's default user."""
    getuid = getuid if getuid is not None else getattr(os, "getuid", None)
    getgid = getgid if getgid is not None else getattr(os, "getgid", None)
    if getuid is None or getgid is None:
        return None
    try:
        return f"{getuid()}:{getgid()}"
    except Exception:
        return None


def pull_command(image: str) -> list[str]:
    """Pulling an image is a stop-condition action (a download, section 16): this only RETURNS the argv for a human
    to approve and run. Nothing in this module executes it."""
    _check_image(image)
    return [_DOCKER, "pull", image]


def remove_container_command(name: str) -> list[str]:
    """The argv that force-removes a named container. `subprocess` kills the docker CLI on a timeout but not the
    container it started, so a caller that named its container (docker_run_argv container_name=) runs this after
    a timeout. Like pull_command it only returns the argv."""
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise SandboxConfigError(f"the container name {_short(name)!r} is not a plain name")
    return [_DOCKER, "rm", "-f", name]


# ---------------------------------------------------------------------------------------------------------------
# Runner and probes: injectable, never raising, always with a timeout
# ---------------------------------------------------------------------------------------------------------------


def default_runner(
    argv: Sequence[str], timeout: float, *, env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run argv with a timeout and never raise: a command that is missing, cannot start or does not finish comes
    back as a CompletedProcess with RC_NOT_FOUND, RC_ERROR or RC_TIMEOUT and the reason in stderr. stdin is closed,
    so nothing can wait for input. Killing a timed-out docker CLI does not stop its container (see
    remove_container_command).

    `env` is the environment the command starts with; None (the default) inherits the parent's whole
    environment, as before. The docker probes here rely on being ABLE to do that: the key visibility test must
    see a credential that a mis-built `docker run` would forward, so this helper never scrubs on its own, and
    must stay callable with env=None for that reason (docker_available/image_present's own default `runner`
    parameter is this function, unchanged, for a caller such as doctor.py's sandbox checks that wants the real
    environment). A caller that starts hermes passes hermes.scrubbed_environ() (ASES-CFG-05); round 12's
    sandbox_command_runner (finding 1) passes its own env via `_gate_docker_runner` below, its default
    `process_runner`, rather than calling this with env=None the way it used to."""
    argv = list(argv)
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL, env=env,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(argv, RC_TIMEOUT, _text(exc.stdout), f"timed out after {timeout}s")
    except FileNotFoundError:
        return subprocess.CompletedProcess(argv, RC_NOT_FOUND, "", f"command not found: {_short(argv[0])}")
    except OSError as exc:
        return subprocess.CompletedProcess(argv, RC_ERROR, "", f"could not run {_short(argv[0])}: {_short(exc)}")


# Round 12 (finding 1): none of these match procenv's credential-shaped pattern (key, token, secret, passw,
# credential, auth, cookie, session), so procenv.scrubbed_environ() alone leaves them untouched -- an operator
# override of any of them would otherwise silently redirect the gate path's docker CLI calls to a different
# daemon/context than the mount and resource checks in this module were built for.
_DOCKER_CONTEXT_ENV_NAMES = ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH")


def _gate_docker_env() -> dict[str, str]:
    """The environment the gate path's own docker-launching subprocesses start with (round 12, finding 1,
    ASES-CFG-04/ASES-CFG-05): procenv.scrubbed_environ() (credential-shaped names dropped, everything else --
    PATH, SYSTEMROOT, the profile and temp directories -- kept) with _DOCKER_CONTEXT_ENV_NAMES also dropped."""
    env = dict(procenv_mod.scrubbed_environ())
    for name in _DOCKER_CONTEXT_ENV_NAMES:
        env.pop(name, None)
    return env


def _gate_docker_runner(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess:
    """Round 12 (finding 1, ASES-CFG-04/ASES-CFG-05): sandbox_command_runner's default `process_runner`,
    replacing a bare `default_runner` (which passes env=None, inheriting the controller's WHOLE environment --
    every credential-shaped variable and any DOCKER_HOST/DOCKER_CONTEXT/DOCKER_TLS_VERIFY/DOCKER_CERT_PATH
    override -- into the docker CLI process that drives every one of the controller's own sandboxed gate runs,
    the ONE gap this module's docstring's list of scrubbed callers never actually closed). Same pattern as
    profiles.py's `_hermes_runner`: wrap `default_runner` with an explicit, scrubbed environment instead of
    changing `default_runner` itself, which key_visibility_test and exfiltration_probe still call directly with
    env=None on purpose (see default_runner's own docstring) -- their whole point is to see what a real,
    unscrubbed environment would leak through a mis-built `docker run`, and scrubbing default_runner globally
    would have blinded exactly the probes meant to catch that class of bug."""
    return default_runner(argv, timeout, env=_gate_docker_env())


class _Result(NamedTuple):
    returncode: int
    stdout: str
    stderr: str


def _safe_run(runner: Callable, argv: Sequence[str], timeout: float) -> _Result:
    """Call an injected runner and reduce whatever it returns to one shape. A runner that raises or returns
    something without an exit code is a failed run, not an exception: the doctor must never crash on a probe."""
    try:
        result = runner(list(argv), timeout)
    except Exception as exc:
        return _Result(RC_ERROR, "", f"the runner raised {type(exc).__name__}")
    code = getattr(result, "returncode", None)
    if isinstance(code, bool) or not isinstance(code, int):
        return _Result(RC_ERROR, "", "the runner returned no exit code")
    return _Result(code, _text(getattr(result, "stdout", "")), _text(getattr(result, "stderr", "")))


def docker_available(runner: Callable = default_runner) -> tuple[bool, str]:
    """(ok, why): the docker CLI is on PATH and `docker info` reaches a daemon. This only asks; it never starts
    Docker Desktop (a stop-condition action). The string says why not, in one ASCII line."""
    result = _safe_run(runner, [_DOCKER, "info", "--format", "{{.ServerVersion}}"], _INFO_TIMEOUT)
    if result.returncode == 0:
        return True, f"docker daemon reachable (server {_short(result.stdout.strip() or 'version unknown', 40)})"
    if result.returncode == RC_NOT_FOUND:
        return False, "docker CLI not found on PATH"
    if result.returncode == RC_TIMEOUT:
        return False, f"docker info timed out after {_INFO_TIMEOUT}s (is Docker Desktop still starting?)"
    reason = _first_line(result.stderr) or f"exit {result.returncode}"
    return False, f"docker daemon not reachable: {reason}"


def image_present(image: str, runner: Callable = default_runner) -> bool:
    """Is the image already in the local store (`docker image inspect`)? Never pulls. False on any failure,
    including an image name that is not a valid reference."""
    if not isinstance(image, str) or not _IMAGE_RE.fullmatch(image):
        return False
    result = _safe_run(runner, [_DOCKER, "image", "inspect", "--format", "{{.Id}}", image], _INFO_TIMEOUT)
    return result.returncode == 0


def sandbox_command_runner(
    policy: SandboxPolicy, *, network: bool = False, process_runner: Callable = _gate_docker_runner,
    home: object = None,
) -> Callable[[pathlib.Path, list[str], int], tuple[bool, str]]:
    """Round 9 (ASES-QG-04, ASES-SEC-02, ASES-SEC-03, ASES-SEC-05, ASES-SEC-06, ASES-SEC-07): build the `runner`
    gates.run_gate calls instead of its own host runner (_run_commands), so a gate's commands each execute inside
    a container built by docker_run_argv over the gate's own checkout, never on the host.

    Same contract as gates._run_commands: the returned runner(worktree, commands, timeout) -> (passed, output)
    stops at the first non-zero command, and a timeout is a red result ending in a "[TIMEOUT after Ns]" line --
    so the controller's gate_runs row means the same thing whichever runner produced it.

    `network` is the ONE task-scoped permission this runner ever grants (ASES-SEC-05, ASES-SEC-07): False (the
    default) unless the caller (gates.resolve_runner) resolved an explicit, Gate-0-approved exception for this
    one gate call. Every command of every call gets only the worktree mounted (plus a mask over each sensitive
    file inside it, since a gate command runs committed, model-authored code and docker_run_argv's own docstring
    says callers like this one MUST mask), no inherited environment and no --env-file (ASES-SEC-06, the
    CONTAINER's own environment -- docker_run_argv never forwards one), the policy's CPU/memory/PID limits, and
    the host user where the platform has one (host_user_spec; None on native Windows, where the container then
    runs as the image's own user, same as docker_run_argv's own default).

    `process_runner` (round 12, finding 1) defaults to `_gate_docker_runner`, not a bare `default_runner`: the
    docker CLI process itself (docker info/image inspect/run -- distinct from the container's own environment
    the paragraph above covers) used to inherit the controller's WHOLE environment (env=None), including any
    credential-shaped variable and any DOCKER_HOST/DOCKER_CONTEXT/DOCKER_TLS_VERIFY/DOCKER_CERT_PATH override
    that happened to be set. `_gate_docker_runner` scrubs both (see its own docstring). A test that wants the
    old, unscrubbed default_runner passes its own `process_runner`, exactly as every test here already does.

    Before running anything, this checks docker_available() and, when the policy names an image, image_present():
    either missing raises SandboxInfrastructureError instead of attempting a `docker run` that would fail with an
    unhelpful "docker: command not found" or "no such image" and be misread as an ordinary red gate. A
    SandboxConfigError while building one command's argv (a worktree that fails mount_problems, a stray NUL byte)
    is raised the same way: none of these are the code under test failing, so none of them may become a red gate
    (see SandboxInfrastructureError's own docstring -- callers must catch it specifically)."""

    def run(worktree: pathlib.Path, commands: list[str], timeout: int) -> tuple[bool, str]:
        ok, why = docker_available(process_runner)
        if not ok:
            raise SandboxInfrastructureError(f"the sandbox is enabled but {why}")
        if policy.image and not image_present(policy.image, process_runner):
            try:
                pull = " ".join(pull_command(policy.image))
            except SandboxConfigError:
                pull = "(the image name is not a valid reference)"
            raise SandboxInfrastructureError(
                f"the sandbox is enabled but image {policy.image} is not present locally; a human runs: {pull}"
            )
        user = host_user_spec()
        lines: list[str] = []
        with tempfile.TemporaryDirectory(prefix="ases-sandbox-mask-") as mask_dir:
            empty_file = pathlib.Path(mask_dir) / "empty"
            empty_file.touch()
            for cmd in commands:
                try:
                    argv = docker_run_argv(
                        policy, worktree, cmd, user=user, network=network, empty_file=empty_file, home=home,
                    )
                except SandboxConfigError as exc:
                    raise SandboxInfrastructureError(f"could not build the sandboxed command: {exc}") from exc
                result = _safe_run(process_runner, argv, timeout)
                lines.append(f"$ {cmd}\n{result.stdout}{result.stderr}".rstrip())
                if result.returncode == RC_TIMEOUT:
                    lines.append(f"[TIMEOUT after {timeout}s]")
                    return False, "\n".join(lines)
                if result.returncode != 0:
                    lines.append(f"[exit {result.returncode}]")
                    return False, "\n".join(lines)
        return True, "\n".join(lines)

    return run


@dataclasses.dataclass(frozen=True)
class KeyVisibilityResult:
    """passed is True exactly when findings is empty. A finding names a secret by its index in the list the caller
    passed and says where it appeared; it never contains the value itself."""
    passed: bool
    findings: list[str] = dataclasses.field(default_factory=list)


def _failed(*findings: str) -> KeyVisibilityResult:
    return KeyVisibilityResult(False, [_ascii(finding) for finding in findings])


def _scrub(text: str, secrets: Sequence[tuple[int, str]]) -> str:
    """Replace every secret value in text with its index, so a stderr excerpt can be quoted in a finding."""
    for index, value in sorted(secrets, key=lambda item: -len(item[1])):
        text = text.replace(value, f"[secret #{index}]")
    return text


def key_visibility_test(
    policy: SandboxPolicy, worktree: object, secrets: Sequence[str | None], *,
    runner: Callable = default_runner, empty_file: object, home: object = None,
) -> KeyVisibilityResult:
    """ASES-CFG-04, ASES-SEC-06, ASES-SEC-02, test 22.10: run `env` and `cat /workspace/.env` inside the sandbox
    (through docker_run_argv, masks included) and pass only when none of the secret values appears in either
    output and the planted .env is unreadable or empty. The caller passes the planted values and the values of the
    controller's own provider-key variables, and plants a .env in the worktree first.

    Never a silent pass: Docker unavailable, no secret to look for, no .env in the worktree, a docker run that
    itself fails, a .env that is "no such file" inside the container (a working mask leaves an empty file, so this
    means nothing was tested), or a runner that raises each return passed=False. Empty or None secrets are
    skipped (an empty string would match every output). Never raises.

    Scope: this exercises the containers docker_run_argv builds (the controller's gate runs). It does not look
    inside a Hermes-managed worker container, whose environment comes from the profile's terminal block; that is
    what check_terminal_block guards, and a live check there is `docker exec <worker> env`."""
    ok, why = docker_available(runner)
    if not ok:
        return _failed(f"the test could not run: {why}")
    values = [(index, value) for index, value in enumerate(secrets or ()) if isinstance(value, str) and value]
    blockers = []
    if not values:
        blockers.append("the test could not run: no secret values were supplied to look for")
    try:
        planted = (pathlib.Path(worktree) / ".env").is_file()
    except (OSError, TypeError, ValueError):
        planted = False
    if not planted:
        blockers.append("the test could not run: the worktree has no .env to test the mask against (plant one first)")
    if blockers:
        return _failed(*blockers)
    try:
        env_argv = docker_run_argv(policy, worktree, "env", empty_file=empty_file, home=home)
        cat_argv = docker_run_argv(policy, worktree, f"cat {WORKSPACE}/.env", empty_file=empty_file, home=home)
    except SandboxConfigError as exc:
        return _failed(f"the test could not run: {exc}")

    findings = []
    env_run = _safe_run(runner, env_argv, _RUN_TIMEOUT)
    if env_run.returncode != 0:
        findings.append(
            f"env could not run inside the sandbox (exit {env_run.returncode}): "
            f"{_scrub(_first_line(env_run.stderr), values)}"
        )
    else:
        for index, value in values:
            if value in env_run.stdout or value in env_run.stderr:
                findings.append(f"secret #{index} appeared in the output of env inside the sandbox")

    cat_run = _safe_run(runner, cat_argv, _RUN_TIMEOUT)
    if cat_run.returncode not in (0, 1):
        # 0 and 1 are cat's own (read, or failed to read). Anything else means docker never ran cat, and a
        # failed run must not be mistaken for a mask that worked.
        findings.append(
            f"cat of {WORKSPACE}/.env could not run inside the sandbox (exit {cat_run.returncode}): "
            f"{_scrub(_first_line(cat_run.stderr), values)}"
        )
    else:
        if cat_run.returncode == 0 and cat_run.stdout.strip():
            findings.append(f"{WORKSPACE}/.env was readable inside the sandbox ({len(cat_run.stdout)} characters)")
        elif cat_run.returncode == 1 and "no such file" in cat_run.stderr.lower():
            # A working mask leaves an EMPTY file, so "no such file" means the planted .env was never visible
            # in the container (worktree not mounted, wrong path): nothing was tested.
            findings.append(
                f"the planted {WORKSPACE}/.env is not visible inside the sandbox, so the mask proved nothing "
                "(is the worktree mounted?)"
            )
        for index, value in values:
            if value in cat_run.stdout or value in cat_run.stderr:
                findings.append(f"secret #{index} appeared in the output of cat {WORKSPACE}/.env")
    return KeyVisibilityResult(not findings, [_ascii(finding) for finding in findings])


# One sh script, because an image may carry any of these tools and only sh is certain. No -q and -sS instead of -s:
# the probe needs the error text to tell "the network is off" from "the tool did not work".
_EXFIL_RC_NO_CLIENT = 99
_EXFIL_COMMAND = (
    "if command -v wget >/dev/null 2>&1; then wget -T 3 -O /dev/null http://example.com; "
    "elif command -v curl >/dev/null 2>&1; then curl -sS -m 3 -o /dev/null http://example.com; "
    "elif command -v python3 >/dev/null 2>&1; then "
    "python3 -c \"import socket; socket.create_connection(('example.com', 80), 3)\"; "
    "else echo 'no network client in the image' >&2; exit 99; fi"
)
# Phrases a client prints when it cannot reach the network. Deliberately not the bare words "timeout" or "resolve":
# a client that rejects an option prints its usage text ("-T SEC  Network read timeout"), and that must not read as
# a blocked network.
_NETWORK_FAILURE_MARKERS = (
    "could not resolve", "unable to resolve", "cannot resolve", "name resolution", "name or service not known",
    "nodename nor servname", "bad address", "unreachable", "no route to host", "refused", "timed out",
    "network is down", "could not connect", "failed to connect",
)


def exfiltration_probe(
    policy: SandboxPolicy, worktree: object, *, runner: Callable = default_runner, home: object = None,
) -> KeyVisibilityResult:
    """ASES-SEC-05, ASES-SEC-07, test 22.11 ("the sandbox must block the network call"): from inside the sandbox,
    with the default-deny network (--network none whatever policy.network says), try to fetch a public page. Passes
    only when the attempt fails with a network error.

    Never a silent pass: a client that succeeds fails the probe; an image with no wget, curl or python3, a failure
    that is not a network error, a docker that could not run, and an unavailable Docker each return passed=False,
    because none of them proves the network is off. On a host that is itself offline a bridge network would fail
    too, so run it with the host online. Never raises."""
    ok, why = docker_available(runner)
    if not ok:
        return _failed(f"the probe could not run: {why}")
    try:
        argv = docker_run_argv(policy, worktree, _EXFIL_COMMAND, network=False, home=home)
    except SandboxConfigError as exc:
        return _failed(f"the probe could not run: {exc}")
    result = _safe_run(runner, argv, _RUN_TIMEOUT)
    detail = _first_line(result.stderr) or _first_line(result.stdout)
    if result.returncode == 0:
        return _failed("the network call succeeded from inside the sandbox: the network is not disabled")
    if result.returncode == _EXFIL_RC_NO_CLIENT:
        return _failed("the image has no wget, curl or python3, so the probe proved nothing about the network")
    if result.returncode in (RC_TIMEOUT, RC_ERROR, RC_NOT_FOUND, 125):
        return _failed(f"the probe could not run inside the sandbox (exit {result.returncode}): {detail}")
    combined = (result.stdout + " " + result.stderr).lower()
    if any(marker in combined for marker in _NETWORK_FAILURE_MARKERS):
        return KeyVisibilityResult(True, [])
    return _failed(
        f"the probe failed (exit {result.returncode}) but not with a network error, so it proves nothing: {detail}"
    )


def doctor_checks(
    policy: SandboxPolicy, profile_dirs: Iterable[object], *, runner: Callable = default_runner,
    image: str | None = None, home: object = None,
) -> list[tuple[str, bool, str]]:
    """ASES-SEC-03, ASES-CFG-04: (name, ok, detail) rows for `swarm doctor`.

    sandbox_docker: the docker CLI and daemon are reachable. sandbox_image: the image is in the local store, only
    when one is named (the image argument, else policy.image); a missing image is the reason Hermes silently drops
    every CPU, memory and PID limit (module docstring, item 2), and the row carries the pull command for a human
    to run. sandbox_profile[<name>]: that profile's terminal block is compliant. No secret can appear in a detail:
    only key names, image references and paths are used, all ASCII."""
    rows: list[tuple[str, bool, str]] = []
    docker_ok, why = docker_available(runner)
    rows.append(("sandbox_docker", docker_ok, _ascii(why)))
    named = policy.image if image is None else image
    if named:
        if not docker_ok:
            rows.append(("sandbox_image", False, _ascii(f"not checked: docker is unavailable ({named})")))
        elif image_present(named, runner):
            rows.append(("sandbox_image", True, _ascii(f"image {named} is present locally")))
        else:
            try:
                pull = " ".join(pull_command(named))
            except SandboxConfigError:
                pull = "(the image name is not a valid reference)"
            detail = f"image {named} is not present locally; a human runs: {pull}"
            rows.append(("sandbox_image", False, _ascii(detail)))
    for profile_dir in _as_list(profile_dirs):
        try:
            name = pathlib.Path(profile_dir).name or str(profile_dir)
        except TypeError:
            rows.append(("sandbox_profile[?]", False, "the profile entry is not a path"))
            continue
        label = f"sandbox_profile[{_ascii(name)}]"
        try:
            problems = check_profile_config(load_profile_config(profile_dir), policy, home=home)
        except SandboxConfigError as exc:
            rows.append((label, False, _ascii(exc)))
            continue
        if problems:
            rows.append((label, False, _ascii("; ".join(problems))))
        else:
            rows.append((label, True, "terminal block is compliant"))
    return rows
