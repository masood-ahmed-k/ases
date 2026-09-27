"""Gate runner (section 9.1: gates.py; section 14, ASES-QG-01/02/03/04).

Runs a gate profile's pinned commands against an exact commit SHA in a clean checkout, never in a
worker's live worktree -- so leftover files can't turn a red build green (ASES-QG-04). "Clean checkout"
here means either a fresh git worktree at that commit (host mode) or a standalone clone with no path back to
the host repository (sandbox mode, self_contained_checkout=True; see run_gate and _self_contained_checkout).
Where the commands run is the `runner` hook of run_gate: by default they run locally (_run_commands), and
sandbox.sandbox_command_runner executes the same commands inside a container instead (ASES-SEC-03).

Round 9: resolve_runner is the ONE place that decides, for a project (and a task's own network exception),
whether a gate call gets the host runner or a sandbox one -- every production caller (review.py's two branch
checks, mergeq.py's Gate 3 candidate, controller.py's post-merge Gate 3 re-run, finalgates.py's Gates 4/5) goes
through it, so the switch (config/swarm.yaml `sandbox: enabled:`, default OFF) actually reaches every gate, not
just the ones a caller remembered to wire. With the switch off, or no project_config given, behavior is exactly
what it was before this round: the host runner, in a linked worktree.

ASES-CFG-04 (p212): "Hermes provider credentials must never be exposed to worker terminals ... if any provider
key is visible, move it into Hermes credential storage or behind the approved egress mechanism before running
unattended workers." ASES-CFG-05 (Appendix F): "do not export provider keys into worker shells." Round 8: the
host runner (_run_commands) and run_gate's own `git worktree add`/`worktree remove` now start every subprocess
with procenv.scrubbed_environ() instead of the controller's full environment, so a gate command -- which can run
model-authored code, such as a test a coder committed -- cannot read a provider key the operator's shell happens
to hold, and neither can a git hook (a `post-checkout` hook in the shared .git/hooks, which a worker on the local
backend can write) that runs during the worktree checkout. Round 9 goes further for the checkout: every git call
here goes through _git, which uses gitexec.GIT and gitexec.git_env(), so repository hooks and core.fsmonitor do
not run at all and anything git still runs (a filter driver) sees the same scrubbed environment. This covers the
HOST runner and the gate checkout. It
does NOT cover a gateway-dispatched worker's own shell: ASES never spawns one, so there is nothing here to scrub.
Docker sandbox runners (sandbox.docker_run_argv) are unchanged: they never inherit the host environment at all.
No pass-through allowlist exists this round: a gate command that genuinely needs a credential-shaped variable now
fails, loudly, with its output recorded in gate_runs; a plan-level allowlist published and reviewed at Gate P is
a follow-up, not built here.

The controller believes only these records, never a worker's self-report (ASES-QG-01, section 14.2).

The text checks over a diff (the tamper check, the secret scan) live in tamper.py; detect_tamper and
scan_for_secrets here are their string-returning entry points for callers that want messages, not Findings.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import pathlib
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from datetime import datetime, timezone

from . import db as ases_db
from . import events as events_mod
from . import gitexec as gitexec_mod
from . import procenv as procenv_mod
from . import sandbox as sandbox_mod
from . import tamper as tamper_mod


@dataclasses.dataclass(frozen=True)
class GateResult:
    gate: str
    commit_sha: str
    passed: bool
    detail: str


@dataclasses.dataclass(frozen=True)
class GateRunner:
    """Round 9 (ASES-QG-04, ASES-SEC-03): what resolve_runner decided for one gate call. `runner` is the hook
    run_gate takes (None means its own default host runner, _run_commands); `self_contained` says whether the
    checkout must also be self-contained (see run_gate's `self_contained_checkout`), because the commands are
    about to run inside a container that mounts only the checkout, never a linked worktree whose .git points
    back at the host repository. The two always travel together: resolve_runner never hands back a sandbox
    runner with self_contained False, or vice versa."""
    runner: Callable[[pathlib.Path, list[str], int], tuple[bool, str]] | None
    self_contained: bool


def resolve_runner(project_config, task=None) -> GateRunner:
    """Round 9: the ONE place that decides which runner a gate call uses for a project and, for the task-scoped
    network exception, a task (ASES-QG-04, ASES-SEC-03, ASES-SEC-05, ASES-SEC-07). Every gate caller -- Gate 1's
    two branch checks (review.py), the Gate 3 candidate and the post-merge Gate 3 re-run (mergeq.py,
    controller.py) and Gates 4/5 (finalgates.py) -- goes through this, so no caller can forget to and none can
    silently disagree about whether the sandbox is on.

    `project_config` is an ases.config.ProjectConfig, or anything shaped like one: a `sandbox_enabled` property
    and a `sandbox_policy_config()` method. Duck-typed on purpose, so this module needs no import of config.py
    and a test can hand it a bare stand-in. None, or sandbox_enabled False, returns GateRunner(None, False):
    today's behavior exactly, the host runner in a linked worktree -- the switch's default (config/swarm.yaml
    `sandbox: enabled: false`) is OFF, so this is what every project sees until a human turns it on.

    With the sandbox enabled, `task` (an ases.plan.PlanTask, or anything with `sandbox_network` and
    `sandbox_network_reason`, or None) is the ONLY source of the network exception: network is granted for this
    one gate call only when task.sandbox_network is true and names a non-empty reason. Gates 4/5 and the
    post-merge Gate 3 re-run are called with no task (or the caller simply passes none), so they always run with
    --network none; only the task's own Gate 1 (review.py) and Gate 3 candidate (mergeq.py) calls can carry an
    approved exception through, exactly as the round 9 work order specifies. Worker containers are configured per
    Hermes PROFILE, not per card or task (see sandbox.py's module docstring, item 5): this function has no say
    over a worker's own network access, only over the controller's own gate runs."""
    if project_config is None or not getattr(project_config, "sandbox_enabled", False):
        return GateRunner(None, False)
    policy = sandbox_mod.SandboxPolicy.from_config(project_config.sandbox_policy_config())
    network = bool(
        task is not None
        and getattr(task, "sandbox_network", False)
        and str(getattr(task, "sandbox_network_reason", "") or "").strip()
    )
    return GateRunner(sandbox_mod.sandbox_command_runner(policy, network=network), True)


def _run_commands(cwd: pathlib.Path, commands: list[str], timeout: int) -> tuple[bool, str]:
    """Runs `commands` on the host, in order, stopping at the first non-zero exit: the default `runner` for
    run_gate, used whenever no sandbox runner is given.

    ASES-CFG-04/ASES-CFG-05: every command starts with procenv.scrubbed_environ(), a credential-scrubbed copy of
    the controller's environment, never the controller's own os.environ -- a command here can run model-authored
    code (a test a coder committed), so it must not be able to read a provider key the operator's shell happens to
    hold. PATH, COMSPEC, SYSTEMROOT, PATHEXT, TEMP/TMP and the profile directories do not match the
    credential-shaped pattern and survive unchanged, so a shell=True command still finds its interpreter and
    Windows programs still start. This is the HOST runner only: a sandbox runner (sandbox.py) never inherits the
    host environment at all, and a gateway-dispatched worker's own shell is out of scope, since ASES never spawns
    one. No pass-through allowlist: a command that needs a credential-shaped variable fails here, loudly, and that
    failure is recorded like any other red gate (see the module docstring)."""
    env = procenv_mod.scrubbed_environ()
    lines = []
    for cmd in commands:
        try:
            result = subprocess.run(
                cmd, shell=True, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace", env=env,
            )
        except subprocess.TimeoutExpired:
            lines.append(f"$ {cmd}\n[TIMEOUT after {timeout}s]")
            return False, "\n".join(lines)
        lines.append(f"$ {cmd}\n{result.stdout}{result.stderr}".rstrip())
        if result.returncode != 0:
            lines.append(f"[exit {result.returncode}]")
            return False, "\n".join(lines)
    return True, "\n".join(lines)


def _git(args: list[str], *, cwd: pathlib.Path | str | None = None, timeout: int) -> subprocess.CompletedProcess:
    """The ONE local helper every git subprocess gates.py itself starts (the checkout, the clone, the detached
    checkout, worktree remove) goes through. Round 9: it runs git through gitexec (GITHARDEN), the controller's one
    hardened way to run git: gitexec.GIT (repository hooks and core.fsmonitor disabled) with gitexec.git_env() (the
    procenv credential scrub, ASES-CFG-04/ASES-CFG-05, plus no terminal prompt), so a `post-checkout` hook the
    checkout would otherwise trigger (host mode: a hook in the shared .git/hooks, which a worker on the local
    backend can write) does not run, and a filter driver that still does sees no credential. `cwd` is git's own
    `-C <path>`, not the subprocess's working directory, so a caller never needs an absolute vs. relative path
    distinction."""
    argv = [*gitexec_mod.GIT, *(["-C", str(cwd)] if cwd is not None else []), *args]
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=gitexec_mod.git_env())


def _worktree_checkout(
    repo_path: pathlib.Path, commit_sha: str, target: pathlib.Path,
) -> subprocess.CompletedProcess:
    """Host mode (today's behavior, unchanged): a linked worktree, `git worktree add --detach`. Its .git is a
    FILE naming the host repository's .git/worktrees/<name> directory, which is fine on the host -- every gate
    command shares repo_path's own git installation and history -- but does not exist inside a container that
    mounts only the checkout (see _self_contained_checkout)."""
    return _git(["worktree", "add", "--detach", str(target), commit_sha], cwd=repo_path, timeout=60)


def _worktree_teardown(repo_path: pathlib.Path, target: pathlib.Path) -> None:
    _git(["worktree", "remove", "--force", str(target)], cwd=repo_path, timeout=60)


def _self_contained_checkout(
    repo_path: pathlib.Path, commit_sha: str, target: pathlib.Path,
) -> subprocess.CompletedProcess:
    """Round 9, sandbox mode (ASES-QG-04, ASES-SEC-03): a checkout with NO path back to repo_path, for a gate
    that runs inside a container mounting only this checkout. `git worktree add`'s .git is a FILE (gitdir:
    <repo>/.git/worktrees/<name>) that does not exist inside such a container, so every git command in there
    would fail (docker_run_argv's own docstring names this limit).

    This clones instead: `git clone --no-hardlinks --no-checkout` makes a real `.git` DIRECTORY with its own copy
    of every object (no shared inode with the host's, and no objects/info/alternates file, since neither
    --shared nor --reference is used), then a detached checkout of the exact commit. Nothing inside the container
    can then write repo_path's own objects or refs, because it has no path to them at all -- and, as a side
    effect, a fresh clone's .git/hooks holds only git's own .sample files, never the host repository's real hooks
    (a linked worktree shares those with the main repository; a clone does not), so there is no post-checkout
    hook to worry about here either.

    Chosen over a `file://` fetch of the single commit (evaluated too, and faster on a large repository) for the
    simplicity of getting exactly right and verifying without Docker: test_gates.py inspects the checkout's
    .git, confirms `rev-parse HEAD` equals commit_sha, and confirms no alternates file and no hardlink to a host
    object. Safety over speed while the sandbox mode is new; a repository large enough for the full clone to
    matter can swap this for a shallow fetch later without changing run_gate's own contract."""
    clone = _git(["clone", "--no-hardlinks", "--no-checkout", str(repo_path), str(target)], timeout=120)
    if clone.returncode != 0:
        return clone
    return _git(["checkout", "--detach", commit_sha], cwd=target, timeout=60)


def _self_contained_teardown(repo_path: pathlib.Path, target: pathlib.Path) -> None:
    """A clone registers nothing in repo_path/.git/worktrees, so there is nothing to unregister: run_gate's own
    shutil.rmtree(tmp_root) removes the directory. Kept as its own function, matching _worktree_teardown, so
    run_gate never has to know which mode tore itself down."""


def run_gate(
    repo_path: pathlib.Path, commit_sha: str, gate_name: str, commands: list[str], *,
    conn=None, task_key: str = "", project: str | None = None, timeout_per_command: int = 120,
    runner: Callable[[pathlib.Path, list[str], int], tuple[bool, str]] | None = None,
    self_contained_checkout: bool = False,
) -> GateResult:
    """Checks out commit_sha into a throwaway checkout, runs commands there, tears it down.

    If conn is given, records the result in gate_runs (task_key, gate, commit_sha, result, detail, project).
    `project` (schema v7) scopes the row to the project that ran it, so two projects sharing this database, or
    reusing a task key, do not share gate history: see last_gate_result for how a reader tells them apart. Left
    at the default None, the row's project column is NULL, exactly as every row written before this column
    existed -- old behavior, unchanged.

    `runner` (ASES-QG-04, ASES-SEC-03) replaces the local command runner: a callable
    runner(worktree, commands, timeout_per_command) -> (passed, output). This is how the sandbox module runs
    the same commands inside a container instead of on the host. The checkout, the cleanup and the
    gate_runs row are the same whichever runner ran the commands, so the controller's gate record means the
    same thing either way. A runner that raises is not caught: the checkout is still torn down, no row is
    written, and the caller decides what an infrastructure failure means (it is not a red gate). Callers get
    both `runner` and `self_contained_checkout` from the ONE resolution point, gates.resolve_runner: they always
    travel together (see GateRunner).

    `self_contained_checkout` (round 9, ASES-QG-04, ASES-SEC-03) picks how the checkout is made. False (the
    default: today's behavior, unchanged) is `git worktree add --detach` (_worktree_checkout), a linked worktree
    whose .git is a FILE pointing back at repo_path -- fine on the host, where a gate command shares repo_path's
    own git installation. True (set only alongside a sandbox runner) is a standalone clone with no path back to
    repo_path at all (_self_contained_checkout), because a container that mounts only the checkout has no way to
    resolve that pointer, and nothing inside it may be able to write repo_path's own objects or refs anyway.

    ASES-CFG-04/ASES-CFG-05: the git subprocesses below go through _git, so they run with gitexec.git_env() (the
    round 8 credential scrub, never the controller's environment) and gitexec.GIT, which since round 9 stops a
    `post-checkout` hook the checkout would trigger from running at all (host mode: a hook in the shared
    .git/hooks, which a worker on the local backend can write; sandbox mode has no shared hooks at all, see
    _self_contained_checkout).

    ASES-SEC-01 ("nothing secret-shaped in stored gate output or in text handed to a card"): the command output
    is redacted ONCE here (events.redact_text), before it is stored in gate_runs.detail and before it is returned
    in GateResult.detail. Command output is the likeliest place for a secret (a failing test that prints its
    environment, a tool that echoes a token), and every consumer copies GateResult.detail somewhere else: a card
    comment, a fix-card body, a merge outcome. Redacting at the source means no consumer can forget to.
    """
    run_commands = runner if runner is not None else _run_commands
    checkout = _self_contained_checkout if self_contained_checkout else _worktree_checkout
    teardown = _self_contained_teardown if self_contained_checkout else _worktree_teardown
    kind = "checkout" if self_contained_checkout else "worktree"
    tmp_root = pathlib.Path(tempfile.mkdtemp(prefix="ases-gate-"))
    worktree = tmp_root / "wt"
    try:
        add = checkout(repo_path, commit_sha, worktree)
        if add.returncode != 0:
            passed, output = False, f"could not create gate {kind}: {add.stdout}{add.stderr}"
        else:
            passed, output = run_commands(worktree, commands, timeout_per_command)
    finally:
        teardown(repo_path, worktree)
        shutil.rmtree(tmp_root, ignore_errors=True)

    # A runner is expected to hand back text; anything else is passed through untouched rather than crashing a
    # gate that already ran.
    detail = events_mod.redact_text(output) if isinstance(output, str) else output
    result = GateResult(gate_name, commit_sha, passed, detail)

    if conn is not None:
        conn.execute(
            "INSERT INTO gate_runs (task_key, gate, commit_sha, result, detail, ran_at, project) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_key, gate_name, commit_sha, "pass" if result.passed else "fail", result.detail,
             datetime.now(timezone.utc).isoformat(timespec="seconds"), project),
        )
    return result


def last_gate_result(conn, task_key: str, gate: str, commit_sha: str, *, project: str | None = None) -> str | None:
    """The most recent gate_runs.result for this task_key, gate and commit_sha, or None with no row.

    `project`, when given, scopes the match to rows this project wrote OR a row with no project at all (a NULL
    column: written before schema v7 added it, or by a caller that did not pass one to run_gate). A NULL row
    still counts so history from before this change, or from a caller that has not been updated yet, is never
    orphaned; it is only a DIFFERENT project's row, stamped with a project that is neither this one nor NULL,
    that is excluded. Left at the default None (the old call shape), nothing is filtered by project at all --
    every caller's behavior is exactly what it was before this parameter existed."""
    if project is None:
        row = conn.execute(
            "SELECT result FROM gate_runs WHERE task_key = ? AND gate = ? AND commit_sha = ? "
            "ORDER BY id DESC LIMIT 1",
            (task_key, gate, commit_sha),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT result FROM gate_runs WHERE task_key = ? AND gate = ? AND commit_sha = ? "
            "AND (project IS NULL OR project = ?) ORDER BY id DESC LIMIT 1",
            (task_key, gate, commit_sha, project),
        ).fetchone()
    return row["result"] if row else None


def scan_for_secrets(diff_text: str) -> list[str]:
    """ASES-SEC-01 / ASES-GIT-07: secret scan (Gates 1 and 3). Reads only the ADDED lines of the diff, through
    tamper.secret_hint (events.py's pattern set, which is the one place that defines what a provider key
    looks like, plus the few shapes a diff can carry that an event payload never does), and reports an added
    FILE whose name marks it as holding secrets (.env, *.pem, id_rsa) even when its content matches nothing.

    A finding names the file and the line and says what kind of secret it looks like; it NEVER contains the
    matched text. It used to echo the first 80 characters of the offending line, so the message that reported
    a leaked key printed the key into a card comment and a log. A bare "+text" line, as questions.py hands
    over one at a time, is scanned as an added line of a file with no name."""
    findings = []
    for fd in tamper_mod.parse_diff(diff_text):
        if fd.status == "A" and tamper_mod.is_secret_file(fd.path):
            findings.append(tamper_mod.format_finding(tamper_mod.Finding(
                "secret_added", fd.path, "possible secret added: a file whose name marks it as holding secrets",
            )))
        for number, text in fd.added_lines:
            hint = tamper_mod.secret_hint(text)
            if hint:
                findings.append(tamper_mod.format_finding(tamper_mod.Finding(
                    "secret_added", fd.path, f"possible secret added: secret-shaped value ({hint})", number,
                )))
    return findings


def detect_tamper(diff_text: str) -> list[str]:
    """ASES-QG-03 over the text of a diff, as one string per finding (kind, path and line, then why). Delegates
    to tamper.analyze_diff, which does the real work and returns Findings; the old cheap markers (a removed
    test function, @pytest.mark.skip, `|| true`, xfail) are all covered by it. Flags for the controller and a
    reviewer to confirm, it does not prove a test still tests what it did. With no allow_paths given, nothing
    is treated as the task's own territory: use tamper.analyze_diff or tamper.check_range for that."""
    return [tamper_mod.format_finding(finding) for finding in tamper_mod.analyze_diff(diff_text)]


def hash_gate_profiles(
    gate_profiles: dict[str, list[str]], sandbox_network_exceptions: dict | None = None,
) -> str:
    """ASES-QG-02: a stable content hash of a plan's gate profiles, pinned in controller config (see
    controller.pin_gate_profiles / verify_gate_pin) so a later diff that quietly edits gate
    configuration, CI scripts, or test-runner settings is caught instead of trusted.

    `sort_keys=True` makes profile-NAME order irrelevant; command order within a profile's own list is
    preserved -- reordering commands still changes the hash. That's deliberately stricter than a set
    comparison would be, since any textual change to what a profile actually does should force
    re-approval.

    `sandbox_network_exceptions` (round 9, ASES-SEC-05, ASES-SEC-07): the plan's task-scoped network exceptions
    (see plan.sandbox_network_exceptions), folded into the same pin so flipping one after approval -- without a
    fresh `swarm approve` -- is caught exactly like an edited gate command, instead of quietly granting network
    access to a sandboxed gate run that Gate P never saw. Left at the default None, or given an empty mapping,
    the hash is EXACTLY what it was before this parameter existed: every project with no network exception --
    which is every project pinned before this round -- keeps validating against its existing pin unchanged."""
    payload = (
        {"gate_profiles": gate_profiles, "sandbox_network_exceptions": sandbox_network_exceptions}
        if sandbox_network_exceptions else gate_profiles
    )
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
