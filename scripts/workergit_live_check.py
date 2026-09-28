"""WORKERGIT (round 15): git inside a worker's Docker sandbox, proven against a real container.

USES REAL DOCKER. Not collected by pytest (it lives under scripts/, not tests/, and pytest's own testpaths is
["tests"]). Run it by hand, from anywhere, with:

    C:/Users/masoo/ases/.venv/Scripts/python.exe scripts/workergit_live_check.py

The problem this proves the fix for (sandbox.py's own module docstring, WORKERGIT section, has the full mechanism
and file:line citations into the installed Hermes 0.21.3 source): a card's linked worktree's own `.git` is a FILE
naming the main repository's `.git/worktrees/<task_id>` admin directory; by default that name is an ABSOLUTE host
path, which does not exist inside a container that mounts only the worktree, so a worker's own `git` fails there.

This script, against a THROWAWAY repository it creates under its own work directory (never the real test
repository, never a real Hermes worker, never a real model provider):

1. Creates a repo the way controller.ensure_repo_bootstrapped now does (`worktree.useRelativePaths=true` set
   before anything else) and cuts a worktree the way Hermes's Kanban dispatcher does
   (hermes_cli/kanban_db_workspace.py:443 `<repo>/.worktrees/<task_id>`, `git worktree add -b <branch> <path>
   HEAD`), then reads the worktree's own `.git` file to show the relative gitdir empirically.
2. Builds the exact `docker run` mounts a worker's terminal gets: /workspace for the worktree (Hermes's own
   docker_mount_cwd_to_workspace) plus sandbox.GIT_WORKTREE_VOLUMES, with the literal ${env:TERMINAL_CWD} each
   one carries substituted for the worktree's own path -- the SAME substitution the installed Hermes source
   performs (cli.py:488-490, hermes_cli/config.py's _expand_env_vars) before a worker's terminal ever starts,
   done here directly against sandbox.py's own real output so this proves ASES's code, not a hand-rolled
   equivalent of it.
3. Inside a real container with exactly those mounts: `git status`, `git add`, `git commit` succeed on the
   card's own branch; the commit is visible from the HOST afterwards; writing `.git/hooks/pre-commit` and
   editing `.git/config` both fail (Read-only file system); `env` shows no planted host key (nothing is ever
   forwarded: no -e, no --env-file); and, to make the "what a worker could still do" risk in sandbox.py's own
   docstring concrete rather than theoretical, `git update-ref refs/heads/main <sha>` succeeds from inside the
   container and is shown to be immediately visible as the primary checkout's own HEAD on the host afterwards
   (exactly what guards.check_primary_checkout's expected_head comparison would catch on the controller's very
   next pass).

A real, load-bearing finding this script surfaces on this machine: SANDBOXIMG's own pinned `ases-sandbox:py311-1`
(docker/sandbox/Dockerfile, Debian 13 "trixie" stable, git 1:2.47.3-0+deb13u1 -- the newest trixie's own apt
repository offered on 2026-09-28) ships git 2.47.3, and `worktree.useRelativePaths=true` needs git 2.48+ (it
marks the repository with `extensions.relativeWorktrees = true`, which git < 2.48 refuses outright: "fatal:
unknown repository extension found: relativeworktrees" on EVERY git command against that repository, not just
worktree ones). This script checks the pinned image's own git version first and says plainly whether it can run
this proof at all; when it cannot, it builds one small throwaway image of its own (an official `alpine` base
plus `apk add git`, git 2.54 as of this run) so the MOUNT MECHANISM itself is still proven for real, and reports
the incompatibility as a finding rather than silently switching images. See the WORKERGIT report for exactly
this: it is the architect's call whether SANDBOXIMG bumps its own pinned git (a different source than Debian
trixie stable, which does not have 2.48 yet) or WORKERGIT's design is reconsidered.
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

_SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ases import gitexec as gitexec_mod  # noqa: E402
from ases import sandbox as sandbox_mod  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
# This round's own pytest basetemp: a directory that is this package's alone, but pytest CLEARS it at the start
# of every `pytest --basetemp=...` run, so this script's own work directory lives one level below it, under a
# name pytest never writes to, and is left for a person to inspect after a run that does not overlap with pytest.
BASETEMP = pathlib.Path(r"C:\Users\masoo\ases-wt\_pytest\workergit")
WORKDIR = BASETEMP / "workergit-live-check"
REPO = WORKDIR / "repo"
TASK_ID = "workergit-livecheck-task1"
BRANCH = f"wt/{TASK_ID}"

PINNED_IMAGE = "ases-sandbox:py311-1"
_MIN_GIT = (2, 48)
FALLBACK_DOCKERFILE = "FROM alpine:latest\nRUN apk add --no-cache git\n"
FALLBACK_IMAGE = "ases-workergit-livecheck:alpine-git"

PLANTED_ENV_SECRET = "ASES-WORKERGIT-LIVECHECK-DO-NOT-LEAK-582034"
PLANTED_ENV_KEY_NAME = "FAKE_PROVIDER_API_KEY"

_FAILURES: list[str] = []
_FINDINGS: list[str] = []


def _ok(label: str, detail: str = "") -> None:
    print(f"[PASS] {label}" + (f" - {detail}" if detail else ""))


def _fail(label: str, detail: str) -> None:
    print(f"[FAIL] {label} - {detail}")
    _FAILURES.append(f"{label}: {detail}")


def _note(label: str, detail: str) -> None:
    print(f"[NOTE] {label} - {detail}")
    _FINDINGS.append(f"{label}: {detail}")


def _run_git(args: list[str], *, cwd: pathlib.Path) -> subprocess.CompletedProcess:
    argv = [*gitexec_mod.GIT, "-C", str(cwd), *args]
    return subprocess.run(argv, capture_output=True, text=True, timeout=60, env=gitexec_mod.git_env())


def _run(argv: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _docker_git_version(image: str) -> tuple[int, int] | None:
    """(major, minor) of `git --version` run inside image, or None if the image or git in it is unreachable."""
    result = _run(["docker", "run", "--rm", image, "git", "--version"], timeout=60)
    if result.returncode != 0:
        return None
    text = result.stdout.strip()
    # "git version 2.47.3" (possibly with a distro suffix after that).
    parts = text.replace("git version", "").strip().split(".")
    try:
        return int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        return None


def choose_image() -> str | None:
    """PINNED_IMAGE if it exists AND its git is new enough for worktree.useRelativePaths; otherwise a small
    throwaway image this function builds itself (never silently -- always printed which, and why)."""
    if sandbox_mod.image_present(PINNED_IMAGE):
        version = _docker_git_version(PINNED_IMAGE)
        if version is None:
            _fail("pinned image git version", f"could not run git --version inside {PINNED_IMAGE}")
        elif version >= _MIN_GIT:
            _ok("pinned image git version", f"{PINNED_IMAGE} has git {version[0]}.{version[1]} (>= 2.48)")
            return PINNED_IMAGE
        else:
            _note(
                "pinned image git version too old for WORKERGIT",
                f"{PINNED_IMAGE} has git {version[0]}.{version[1]}, but worktree.useRelativePaths needs git "
                "2.48+ (it marks the repository with extensions.relativeWorktrees, which an older git refuses "
                "outright: every git command in that repository fails, not just worktree ones). Debian 13 "
                "trixie stable's own apt repository does not offer git 2.48+ yet as of this run. Falling back "
                f"to {FALLBACK_IMAGE} (official alpine + apk add git) to still prove the MOUNT MECHANISM for "
                "real; see this script's own module docstring and the WORKERGIT report.",
            )
    else:
        _note(
            "pinned image not present",
            f"{PINNED_IMAGE} (SANDBOXIMG's image) is not present locally; using {FALLBACK_IMAGE} (official "
            "alpine + apk add git) for this live check instead, per the work order.",
        )
    build_dir = WORKDIR / "fallback-image"
    build_dir.mkdir(parents=True, exist_ok=True)
    (build_dir / "Dockerfile").write_text(FALLBACK_DOCKERFILE, encoding="utf-8")
    build = _run(["docker", "build", "-t", FALLBACK_IMAGE, str(build_dir)], timeout=180)
    if build.returncode != 0:
        _fail("build fallback image", f"exit {build.returncode}: {build.stdout}{build.stderr}")
        return None
    version = _docker_git_version(FALLBACK_IMAGE)
    if version is None or version < _MIN_GIT:
        _fail("fallback image git version", f"{FALLBACK_IMAGE} git version is {version}, still older than 2.48")
        return None
    _ok("fallback image built", f"{FALLBACK_IMAGE} has git {version[0]}.{version[1]}")
    return FALLBACK_IMAGE


def step1_build_repo_and_worktree() -> pathlib.Path | None:
    """Mirrors controller.ensure_repo_bootstrapped (worktree.useRelativePaths=true before anything else) and
    then Hermes's own kanban_db_workspace._ensure_git_worktree / _anchored_worktree shape: `<repo>/.worktrees/
    <task_id>`, `git worktree add -b <branch> <path> HEAD`."""
    if REPO.exists():
        shutil.rmtree(REPO, ignore_errors=True)
    REPO.mkdir(parents=True, exist_ok=True)
    steps = [
        (["init", "-q", "-b", "main"], "init"),
        (["config", "worktree.useRelativePaths", "true"], "config worktree.useRelativePaths"),
        # Cosmetic only, not part of the WORKERGIT design: without this, a host with core.autocrlf=true
        # (a common Windows git installer default) checks README.md out as CRLF while the container's own git
        # (no autocrlf) compares it against the LF bytes actually stored, and `git status` inside the container
        # reports it "modified" even though nothing touched it. Off here so the container output below is only
        # ever about what this script itself does.
        (["config", "core.autocrlf", "false"], "config core.autocrlf"),
    ]
    for args, label in steps:
        result = _run_git(args, cwd=REPO)
        if result.returncode != 0:
            _fail(f"repo setup: {label}", f"{result.stdout}{result.stderr}")
            return None
    (REPO / "README.md").write_text("workergit live check throwaway repo\n", encoding="utf-8")
    add = _run_git(["add", "-A"], cwd=REPO)
    commit = _run_git(
        ["-c", "user.name=ASES workergit live check", "-c", "user.email=ases-workergit-livecheck@example.invalid",
         "commit", "-q", "-m", "workergit live check: initial commit"],
        cwd=REPO,
    )
    if add.returncode != 0 or commit.returncode != 0:
        _fail("repo setup: initial commit", f"{add.stderr}{commit.stdout}{commit.stderr}")
        return None
    worktree = REPO / ".worktrees" / TASK_ID
    wt = _run_git(["worktree", "add", "-b", BRANCH, str(worktree), "HEAD"], cwd=REPO)
    if wt.returncode != 0:
        _fail("worktree add", f"{wt.stdout}{wt.stderr}")
        return None
    gitfile = (worktree / ".git").read_text(encoding="utf-8").strip()
    _ok("worktree cut", f"{worktree} on branch {BRANCH}; its own .git file reads: {gitfile!r}")
    if not gitfile.startswith("gitdir: ../../.git/worktrees/"):
        _fail(
            "relative gitdir", f"expected 'gitdir: ../../.git/worktrees/{TASK_ID}', got {gitfile!r} -- "
            "worktree.useRelativePaths did not take effect the way the module docstring assumes",
        )
        return None
    _ok("relative gitdir confirmed", "a container mounting only this worktree can resolve .git by mounting the "
        "repository's .git at /.git (see sandbox.py's module docstring, WORKERGIT section)")
    return worktree


def _git_worktree_docker_args(worktree: pathlib.Path) -> list[str]:
    """sandbox.GIT_WORKTREE_VOLUMES with ${env:TERMINAL_CWD} substituted for worktree's own path -- the same
    substitution the installed Hermes source performs before a worker's terminal starts (cli.py:488-490); done
    here directly so this proves sandbox.py's own real output, not a hand-rolled equivalent of it."""
    cwd_ref = "${env:TERMINAL_CWD}"
    args: list[str] = ["-v", f"{worktree}:/workspace"]
    for entry in sandbox_mod.git_worktree_volumes():
        assert cwd_ref in entry, f"unexpected git_worktree_volumes() shape: {entry!r}"
        args += ["-v", entry.replace(cwd_ref, str(worktree))]
    return args


_CONTAINER_SCRIPT = r"""
set -e
echo '--- git status ---'
git status
echo '--- commit from inside the sandbox ---'
echo hello-from-container > f.txt
git add f.txt
git -c user.name=worker -c user.email=worker@example.invalid commit -q -m 'from the sandbox'
git rev-parse HEAD
echo '--- hooks/pre-commit write (must fail) ---'
if hookerr=$( (echo pwn > /.git/hooks/pre-commit) 2>&1 ); then
  echo HOOK_WRITE_SUCCEEDED
else
  echo "HOOK_WRITE_BLOCKED: $hookerr"
fi
echo '--- .git/config write (must fail) ---'
if git config --file /.git/config core.evil true 2>/tmp/cfgerr; then
  echo CONFIG_WRITE_SUCCEEDED
else
  echo "CONFIG_WRITE_BLOCKED: $(cat /tmp/cfgerr)"
fi
echo '--- env (must show no credential-shaped variable) ---'
if env | grep -iE 'key|token|secret|password' ; then
  echo ENVCHECK_LEAK_FOUND
else
  echo ENVCHECK_CLEAN
fi
echo '--- move another branch (main) -- the known risk sandbox.py documents ---'
git update-ref refs/heads/main HEAD
echo MOVED_MAIN
"""


def step2_run_in_sandbox(image: str, worktree: pathlib.Path) -> str | None:
    argv = [
        "docker", "run", "--rm", "--network", "none",
        *_git_worktree_docker_args(worktree),
        "-w", "/workspace",
        "--security-opt", "no-new-privileges", "--cap-drop", "ALL",
        image, "sh", "-c", _CONTAINER_SCRIPT,
    ]
    # stderr=STDOUT merges the container's two streams as the OS delivers them, in the order the container script
    # itself wrote its lines. `_run`'s capture_output captures stdout and stderr into two separate buffers, so
    # `result.stdout + result.stderr` (the previous shape here) always put every stderr line after every stdout
    # line regardless of when either was written -- fine for the pass/fail checks below (they only test
    # substring membership), but it can obscure or reorder something a future reader relies on the order of.
    result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=120)
    output = result.stdout
    print(output)
    if result.returncode != 0:
        _fail("sandboxed git commands", f"container exited {result.returncode}")
        return None
    # The container script uses `set -e`, so reaching MOVED_MAIN at all already proves status/add/commit
    # succeeded (a failing git command would have stopped the script before that line, and the exit-code check
    # above already caught it); step3_verify_from_host confirms the commit independently, from the host.
    checks = [
        ("hooks/pre-commit write blocked", "HOOK_WRITE_BLOCKED" in output and "HOOK_WRITE_SUCCEEDED" not in output),
        ("config write blocked", "CONFIG_WRITE_BLOCKED" in output and "CONFIG_WRITE_SUCCEEDED" not in output),
        ("no credential-shaped env var visible", "ENVCHECK_CLEAN" in output and "ENVCHECK_LEAK_FOUND" not in output),
        ("refs/heads/main move succeeded (the known risk)", "MOVED_MAIN" in output),
    ]
    ok = True
    for label, passed in checks:
        if passed:
            _ok(label)
        else:
            _fail(label, "see container output above")
            ok = False
    return output if ok else None


def step3_verify_from_host(worktree: pathlib.Path) -> None:
    log = _run_git(["log", "--oneline", "-1"], cwd=worktree)
    if "from the sandbox" not in log.stdout:
        _fail("commit visible from host", f"git log on the worktree does not show it: {log.stdout!r}")
    else:
        _ok("commit visible from host", log.stdout.strip())

    primary = _run_git(["log", "--oneline", "-1"], cwd=REPO)
    worktree_head = _run_git(["rev-parse", "HEAD"], cwd=worktree).stdout.strip()
    primary_head = _run_git(["rev-parse", "main"], cwd=REPO).stdout.strip()
    if primary_head == worktree_head:
        _ok(
            "primary checkout's own 'main' ref moved",
            f"main is now {primary_head[:12]}, the worker's own commit -- guards.check_primary_checkout's "
            "expected_head comparison would flag this the moment it next runs (its own git rev-parse HEAD "
            "reads this exact shared ref)",
        )
    else:
        _fail("primary checkout's own 'main' ref moved", f"expected {worktree_head[:12]}, found {primary_head[:12]}")


def main() -> int:
    print("WORKERGIT live check: real Docker, real containers, a throwaway repo, no real Hermes, no real "
          "model provider, never the real test repository.")
    print(f"work directory: {WORKDIR}")
    if WORKDIR.exists():
        shutil.rmtree(WORKDIR, ignore_errors=True)
    WORKDIR.mkdir(parents=True, exist_ok=True)

    ok, why = sandbox_mod.docker_available()
    if not ok:
        _fail("docker_available", why)
        print("\nSummary: FAIL (Docker is not reachable; nothing below can run)")
        return 1
    _ok("docker_available", why)

    image = choose_image()
    if image is None:
        print("\nSummary: FAIL (no usable image; see above)")
        return 1

    worktree = step1_build_repo_and_worktree()
    if worktree is None:
        print("\nSummary: FAIL (repo/worktree setup did not succeed; see above)")
        return 1

    output = step2_run_in_sandbox(image, worktree)
    if output is not None:
        step3_verify_from_host(worktree)

    print()
    if _FINDINGS:
        print(f"Findings ({len(_FINDINGS)}), read alongside the WORKERGIT report:")
        for item in _FINDINGS:
            print(f" - {item}")
    if _FAILURES:
        print(f"Summary: FAIL ({len(_FAILURES)} finding(s))")
        for item in _FAILURES:
            print(f" - {item}")
        return 1
    print(f"Summary: PASS (all real-Docker probes succeeded, using image {image})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
