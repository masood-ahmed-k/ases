"""SANDBOXIMG (round 15): the real Docker probes, not fakes.

USES REAL DOCKER. Not collected by pytest (it lives under scripts/, not tests/, and pytest's own testpaths is
["tests"]). Run it by hand, from anywhere, with:

    C:/Users/masoo/ases/.venv/Scripts/python.exe scripts/sandbox_live_check.py

It proves, against real containers on this machine:

1. sandbox.key_visibility_test (ASES-CFG-04, ASES-SEC-02, ASES-SEC-06, acceptance 22.10): a fake provider key
   planted in the controller's own environment, and a planted .env in a throwaway worktree, must not be visible
   inside the sandbox.
2. sandbox.exfiltration_probe (ASES-SEC-05, ASES-SEC-07, acceptance 22.11): the sandbox network must be blocked.
3. A real sandboxed gate, gates.run_gate through gates.resolve_runner (ASES-QG-04 p279: "Gates run in a clean
   checkout of the exact commit inside the sandbox"), against a LOCAL CLONE of the real test repository:
     - `python -m pytest -q` passes inside the container.
     - a deliberately failing command comes back red.
     - a .env committed in the checkout is not readable inside (ASES-SEC-02, acceptance 22.10's "reading .env
       from inside the sandbox fails").

Safety: a real swarm run can be using C:\\Users\\masoo\\ases-workspaces\\test-repo-phase3 at the same time this
script runs. Nothing here ever passes that path to a git command that writes to it (checkout, worktree add,
commit) or to run_gate as a repo_path (whose own checkout step, in host mode, would register a worktree in its
.git/worktrees). The one git operation against it is a single `git clone --no-hardlinks`, which only READS the
source repository's objects and refs and writes nothing there, into a fresh directory under this script's own
work directory (under the round's pytest basetemp, C:/Users/masoo/ases-wt/_pytest/sandboximg, by default). Every
later step, including run_gate's own nested self-contained checkout, clones from THAT local clone, never from
the real repository again.

Nothing here calls a real Hermes worker or a real model provider: this exercises only ASES's own sandbox module
and gate runner against containers this script starts directly.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import stat
import subprocess
import sys
import time

_SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ases import config as config_mod  # noqa: E402
from ases import gates as gates_mod  # noqa: E402
from ases import gitexec as gitexec_mod  # noqa: E402
from ases import sandbox as sandbox_mod  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
# The real repository this check reads from, and NEVER writes to (see the module docstring).
TEST_REPO_SOURCE = pathlib.Path(r"C:\Users\masoo\ases-workspaces\test-repo-phase3")
# This round's own pytest basetemp: a directory that is this package's alone, cleared at the start of a pytest
# run but otherwise left for a person to inspect after this script runs.
BASETEMP = pathlib.Path(r"C:\Users\masoo\ases-wt\_pytest\sandboximg")
WORKDIR = BASETEMP / "sandboximg-live-check"

# Deliberately not shaped like a real provider key (no sk-/ghp_/... prefix events.redact_text would recognise),
# so a leak is visible verbatim in whatever this script prints, never silently turned into "[redacted]" first.
PLANTED_ENV_SECRET = "ASES-SANDBOXIMG-LIVECHECK-DO-NOT-LEAK-390210"
PLANTED_ENV_KEY_NAME = "FAKE_PROVIDER_API_KEY"

_FAILURES: list[str] = []


def _ok(label: str, detail: str = "") -> None:
    print(f"[PASS] {label}" + (f" - {detail}" if detail else ""))


def _fail(label: str, detail: str) -> None:
    print(f"[FAIL] {label} - {detail}")
    _FAILURES.append(f"{label}: {detail}")


def _scrub_planted_secret(text: str) -> str:
    """A finding's own text must never carry the planted secret's value, even a fake one (the project's own
    events.redact_text follows the same rule for real provider keys): a caller that wants to report WHERE the
    secret showed up prints this, never the raw text."""
    return text.replace(PLANTED_ENV_SECRET, "[planted secret]")


def _clear_readonly_and_retry(func, path, exc_info) -> None:
    """shutil.rmtree's onerror hook (three positional arguments, exactly this shape, is the stdlib's own
    contract). git marks every loose object file read-only (mode 0444) on every platform, including inside a
    fresh `git clone` this script makes of its own local clone (step3c_env_masked's throwaway commit adds more
    of them); Windows honours that bit for delete, unlike POSIX, so shutil.rmtree's plain os.unlink/os.rmdir
    raises PermissionError on every one of them -- deterministically, not as a timing race, and
    ignore_errors=True used to swallow this silently, leaving the directory behind for the NEXT run of this
    script to trip over as a confusing `git clone` "already exists" failure. Clearing the read-only bit and
    retrying the exact call that failed (func is os.unlink or os.rmdir) is the standard fix."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _rmtree_retry(path: pathlib.Path, *, attempts: int = 5, delay: float = 1.0) -> None:
    """Clears a directory that may contain read-only git objects (see _clear_readonly_and_retry), plus a short
    retry loop for the separate, genuinely transient case of a file Docker's own bind-mount teardown has not
    yet released on Windows right after a `docker run --rm` exits. That second case is the one shutil.rmtree
    itself, not just its onerror hook, can raise for: _clear_readonly_and_retry's own single retry (chmod then
    re-run the failed op once) still fails while the file is genuinely locked, and shutil.rmtree's documented
    contract is to propagate whatever an onerror hook raises. shutil.rmtree is therefore called inside a
    try/except here, not just wrapped by the loop: catching only at the loop level (with no try/except around
    the call itself) would let that propagation escape on attempt 1, before any sleep(delay) ever ran, which is
    exactly the bug a prior version of this function had. If the directory still exists after every attempt,
    this raises so the real cause is reported plainly instead of failing somewhere else, later, in a confusing
    shape (git clone's own "destination path ... already exists")."""
    if not path.exists():
        return
    last_exc: OSError | None = None
    for attempt in range(attempts):
        try:
            shutil.rmtree(path, onerror=_clear_readonly_and_retry)
        except OSError as exc:
            last_exc = exc
        else:
            if not path.exists():
                return
        if attempt + 1 < attempts:
            time.sleep(delay)
    if path.exists():
        detail = f": {last_exc}" if last_exc is not None else ""
        raise OSError(f"could not remove {path} after {attempts} attempts (a file inside it may still be locked){detail}")


def _run_git(args: list[str], *, cwd: pathlib.Path) -> subprocess.CompletedProcess:
    argv = [*gitexec_mod.GIT, "-C", str(cwd), *args]
    return subprocess.run(argv, capture_output=True, text=True, timeout=120, env=gitexec_mod.git_env())


def _load_policy() -> sandbox_mod.SandboxPolicy:
    project = config_mod.load_swarm_config(REPO_ROOT / "config" / "swarm.yaml")
    policy = sandbox_mod.SandboxPolicy.from_config(project.sandbox_policy_config())
    print(f"policy.image = {policy.image!r} (from config/swarm.yaml, sandbox.enabled = {project.sandbox_enabled})")
    return policy


class _SandboxEnabledProject:
    """Duck-typed project_config for gates.resolve_runner (see its own docstring): this live check builds its own
    stand-in with the sandbox switched ON, carrying the same policy (same pinned image, same limits), so it
    proves the sandboxed path whatever config/swarm.yaml's own sandbox.enabled says (true since 2026-09-28)."""

    def __init__(self, policy_config: dict) -> None:
        self.sandbox_enabled = True
        self._policy_config = policy_config

    def sandbox_policy_config(self) -> dict:
        return self._policy_config


def step0_docker_available() -> bool:
    ok, why = sandbox_mod.docker_available()
    if ok:
        _ok("docker_available", why)
    else:
        _fail("docker_available", why)
    return ok


def step1_key_visibility(policy: sandbox_mod.SandboxPolicy, workdir: pathlib.Path) -> None:
    kv_dir = workdir / "key-visibility"
    kv_dir.mkdir(parents=True, exist_ok=True)
    (kv_dir / ".env").write_text(f"{PLANTED_ENV_KEY_NAME}={PLANTED_ENV_SECRET}\n", encoding="utf-8")
    empty_file = workdir / "empty"
    empty_file.touch()
    result = sandbox_mod.key_visibility_test(policy, kv_dir, [PLANTED_ENV_SECRET], empty_file=empty_file)
    if result.passed:
        _ok("key_visibility_test", "planted secret was not visible in env or in .env inside the sandbox")
    else:
        _fail("key_visibility_test", "; ".join(result.findings))


def step2_exfiltration(policy: sandbox_mod.SandboxPolicy, workdir: pathlib.Path) -> None:
    net_dir = workdir / "exfiltration"
    net_dir.mkdir(parents=True, exist_ok=True)
    result = sandbox_mod.exfiltration_probe(policy, net_dir)
    if result.passed:
        _ok("exfiltration_probe", "the network call failed from inside the sandbox, as required")
    else:
        _fail("exfiltration_probe", "; ".join(result.findings))


def step3_clone_test_repo(workdir: pathlib.Path) -> pathlib.Path | None:
    if not TEST_REPO_SOURCE.is_dir():
        _fail("clone test-repo-phase3", f"source not found: {TEST_REPO_SOURCE}")
        return None
    clone_dir = workdir / "test-repo-phase3-clone"
    try:
        _rmtree_retry(clone_dir)
    except OSError as exc:
        _fail("clone test-repo-phase3", str(exc))
        return None
    # The ONE git operation against the real repository: a clone only reads its objects and refs, and writes
    # nothing there (see the module docstring). --no-hardlinks: this clone's objects never share an inode with
    # the source repository's, so nothing later can reach back into it through a shared file.
    clone = _run_git(["clone", "--no-hardlinks", str(TEST_REPO_SOURCE), str(clone_dir)], cwd=workdir)
    if clone.returncode != 0:
        _fail("clone test-repo-phase3", f"exit {clone.returncode}: {clone.stdout}{clone.stderr}")
        return None
    head = _run_git(["rev-parse", "HEAD"], cwd=clone_dir)
    if head.returncode != 0:
        _fail("clone test-repo-phase3", f"rev-parse HEAD failed: {head.stderr}")
        return None
    sha = head.stdout.strip()
    _ok("clone test-repo-phase3", f"local clone at {clone_dir}, HEAD {sha}")
    return clone_dir


def step3a_pytest_passes(runner_info: gates_mod.GateRunner, clone_dir: pathlib.Path) -> None:
    head = _run_git(["rev-parse", "HEAD"], cwd=clone_dir).stdout.strip()
    result = gates_mod.run_gate(
        clone_dir, head, "sandboximg_live_pytest", ["python -m pytest -q"],
        runner=runner_info.runner, self_contained_checkout=runner_info.self_contained,
    )
    if result.passed:
        _ok("sandboxed gate: python -m pytest -q", result.detail.splitlines()[-1] if result.detail else "")
    else:
        _fail("sandboxed gate: python -m pytest -q", result.detail)


def step3b_failing_command_is_red(runner_info: gates_mod.GateRunner, clone_dir: pathlib.Path) -> None:
    head = _run_git(["rev-parse", "HEAD"], cwd=clone_dir).stdout.strip()
    result = gates_mod.run_gate(
        clone_dir, head, "sandboximg_live_fail", ["python -c \"import sys; sys.exit(1)\""],
        runner=runner_info.runner, self_contained_checkout=runner_info.self_contained,
    )
    if not result.passed:
        _ok("sandboxed gate: deliberately failing command comes back red", result.detail.splitlines()[-1])
    else:
        _fail("sandboxed gate: deliberately failing command", "the gate passed but should have failed")


def step3c_env_masked(runner_info: gates_mod.GateRunner, clone_dir: pathlib.Path) -> None:
    (clone_dir / ".env").write_text(f"{PLANTED_ENV_KEY_NAME}={PLANTED_ENV_SECRET}\n", encoding="utf-8")
    add = _run_git(["add", ".env"], cwd=clone_dir)
    if add.returncode != 0:
        _fail("sandboxed gate: .env masked", f"git add failed: {add.stderr}")
        return
    commit = _run_git(
        ["-c", "user.name=ASES sandbox live check", "-c", "user.email=ases-sandbox-live-check@example.invalid",
         "commit", "-m", "sandboximg live check: planted .env"],
        cwd=clone_dir,
    )
    if commit.returncode != 0:
        _fail("sandboxed gate: .env masked", f"git commit failed: {commit.stdout}{commit.stderr}")
        return
    head = _run_git(["rev-parse", "HEAD"], cwd=clone_dir).stdout.strip()
    result = gates_mod.run_gate(
        clone_dir, head, "sandboximg_live_env", ["cat .env", "wc -c < .env"],
        runner=runner_info.runner, self_contained_checkout=runner_info.self_contained,
    )
    if PLANTED_ENV_SECRET in result.detail:
        _fail(
            "sandboxed gate: .env masked",
            f"the planted secret was readable: {_scrub_planted_secret(result.detail)}",
        )
        return
    if not result.passed:
        _fail("sandboxed gate: .env masked", f"the gate itself failed unexpectedly: {result.detail}")
        return
    _ok("sandboxed gate: committed .env is not readable inside (SEC-02)", result.detail.replace("\n", " | "))


def main() -> int:
    print("SANDBOXIMG live check: real Docker, real containers, no real Hermes, no real model provider.")
    print(f"work directory: {WORKDIR}")
    try:
        _rmtree_retry(WORKDIR)
    except OSError as exc:
        print(f"\n[FAIL] could not clear the work directory before starting: {exc}")
        return 1
    WORKDIR.mkdir(parents=True, exist_ok=True)

    if not step0_docker_available():
        print("\nDocker is not reachable: stopping here, nothing below can run for real.")
        return 1

    policy = _load_policy()
    if not sandbox_mod.image_present(policy.image):
        _fail("image present", f"{policy.image} is not present locally; build it first (see docker/sandbox/Dockerfile)")
        print("\nSummary: FAIL (see above)")
        return 1
    _ok("image present", policy.image)

    step1_key_visibility(policy, WORKDIR)
    step2_exfiltration(policy, WORKDIR)

    clone_dir = step3_clone_test_repo(WORKDIR)
    if clone_dir is not None:
        runner_info = gates_mod.resolve_runner(_SandboxEnabledProject(
            {"sandbox": {k: v for k, v in {
                "terminal_backend": "docker", "network_default": False, "mount": "worktree_only",
                "forward_env": list(policy.forward_env), "network_exceptions": "explicit_allowlist",
                "image": policy.image, "cpu": policy.cpu, "memory_mb": policy.memory_mb,
                "pids_limit": policy.pids_limit, "extra_deny": list(policy.extra_deny),
            }.items()}}
        ))
        step3a_pytest_passes(runner_info, clone_dir)
        step3b_failing_command_is_red(runner_info, clone_dir)
        step3c_env_masked(runner_info, clone_dir)

    print()
    if _FAILURES:
        print(f"Summary: FAIL ({len(_FAILURES)} finding(s))")
        for item in _FAILURES:
            print(f" - {item}")
        return 1
    print("Summary: PASS (all real-Docker probes succeeded)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
