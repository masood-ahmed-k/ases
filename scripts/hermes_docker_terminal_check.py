"""HERMESDOCKER (round 16): Hermes 0.21.3's OWN Docker terminal code, driven by the REAL coder-1
profile config ASES just applied, proven at zero model-provider quota.

RUN WITH HERMES'S OWN PYTHON (needed to import Hermes's modules), from anywhere:

    C:/Users/masoo/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe scripts/hermes_docker_terminal_check.py

USES REAL DOCKER. Not collected by pytest (testpaths is ["tests"]; this lives under scripts/).

What this proves that scripts/workergit_live_check.py did not: workergit_live_check.py builds its own
`docker run` argv by hand (sandbox.GIT_WORKTREE_VOLUMES substituted directly). Nobody had yet run
Hermes's OWN config-loading and DockerEnvironment-building code against the real coder-1 profile
config.yaml that `swarm init` wrote. That is the gap this script closes: config loading with
`${env:TERMINAL_CWD}` expansion, the terminal config bridged into env vars, tools/environments/docker.py's
DockerEnvironment and its mount/argument building, and Hermes's own execute() API, all exercised for real,
so a mounting problem on Hermes's side is found here instead of during the next real dispatch (50
requests/day quota).

HOW HERMES'S CONFIG-LOADING CODE IS DRIVEN, AND WHY NOT BY IMPORTING cli.py DIRECTLY:

cli.py's own `_mirror_config_to_env`/`load_cli_config` (cli.py:291-500, cited in sandbox.py's module
docstring) are the functions the real worker CLI process uses. But importing cli.py itself is not safe
here: cli.py:171-178 unconditionally calls `load_hermes_dotenv(hermes_home=_hermes_home, project_env=
Path(__file__).parent / '.env')` at MODULE level, and `Path(__file__).parent` is cli.py's OWN directory,
i.e. the real, read-only hermes-agent/.env -- independent of whatever HERMES_HOME is set to. That call
reaches `_sanitize_env_file_if_needed` (hermes_cli/env_loader.py:305-373), which CAN rewrite the file via
`tempfile.mkstemp` + `atomic_replace` (env_loader.py:353-365) when its BOM/NUL sanitize pass would change
anything. Reading the code, this is a real write path against a file under the install we must never
modify, and it fires regardless of HERMES_HOME. So cli.py is never imported here.

The alternative used instead is also Hermes's OWN code, not a hand-rolled equivalent: hermes_cli/config.py
defines `apply_terminal_config_to_env`, whose own docstring says it gives "the same bridge as the CLI
without importing cli.py" (hermes_cli/config.py, next to TERMINAL_CONFIG_ENV_MAP) -- built by Hermes's own
authors for exactly a caller like this one. It runs automatically, with no extra call needed here, the
moment `tools.terminal_tool.ensure_task_env()` is used: `_get_env_config()` calls `_ensure_terminal_env_bridged()`
(tools/terminal_tool.py:530-576), which calls `hermes_cli.config.apply_terminal_config_to_env()` the first
time a terminal environment is requested in the process -- so driving the REAL terminal tool also drives
the REAL config bridge, with no cli.py import. Confirmed by reading the code: `load_hermes_dotenv` is never
called anywhere the terminal_tool/hermes_cli.config/tools.environments import graph reaches (the only two
call sites in the whole hermes-agent tree are cli.py:178 itself, and tools/mcp_tool_config.py:351-352,
inside a function unrelated to and never imported by the terminal tool).

That bridge does still call `hermes_cli.config.ensure_hermes_home()` (hermes_cli/config.py:656-670,
`initialize_home(...)`), which creates a directory skeleton (mkdir + chmod) under whatever HERMES_HOME is
set to. That IS a write, so HERMES_HOME here is never the real coder-1 profile directory: this script
copies ONLY coder-1's config.yaml (never its .env, which holds a real provider key) into a throwaway
Hermes home under this script's own work directory, and points HERMES_HOME there. Nothing under
C:/Users/masoo/AppData/Local/hermes is opened for writing anywhere in this script (see the mtime/listing
check in main()); every read of a real Hermes path is a plain read_text/read_bytes/stat/iterdir call.

The provider key itself (coder-1's .env, XKIRO_API_KEY) is therefore never loaded into this process at all
-- proving that specific key never reaches the container would be vacuous. Instead: every credential-shaped
name already in the inherited environment is scrubbed, and one fake `ASES_PLANTED_SECRET_KEY` is planted,
before Hermes's own code runs. If Hermes's own docker_forward_env/docker_env handling ever forwarded
anything, this canary would show up inside the container; the real key would behave no differently, since
Hermes has no code path that treats one credential-shaped env var specially over another.

Cleanup: coder-1's config.yaml sets docker_persist_across_processes: false, so DockerEnvironment.cleanup()
(tools/environments/docker.py:1005) actually stops+removes the container (persist_across_processes=True
would just detach the handle and leave it running); wait_for_cleanup() blocks until that background
teardown thread finishes so this script's own container is gone before it exits.
"""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import time

_SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ases import config as config_mod  # noqa: E402
from ases import gitexec as gitexec_mod  # noqa: E402
from ases import sandbox as sandbox_mod  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# The real, read-only Hermes install. Every use of these four paths below is a read (stat, read_text,
# read_bytes, iterdir) or a sys.path insert (for importing Hermes's modules); never open(...'w'...),
# write_text, write_bytes, shutil.copy*, os.remove, os.rename or os.replace with one of these as the
# destination. See the module docstring for why (the mtime/listing check in main() verifies this held).
HERMES_HOME_REAL = pathlib.Path(r"C:\Users\masoo\AppData\Local\hermes")
HERMES_AGENT_DIR = HERMES_HOME_REAL / "hermes-agent"
CODER1_PROFILE_DIR = HERMES_HOME_REAL / "profiles" / "coder-1"
CODER1_CONFIG = CODER1_PROFILE_DIR / "config.yaml"

# This script's own throwaway work directory (never the real test repository, never under the Hermes home).
BASETEMP = pathlib.Path(r"C:\Users\masoo\ases-wt\_pytest\hermesdocker")
WORKDIR = BASETEMP / "hermes-docker-terminal-check"
REPO = WORKDIR / "repo"
FAKE_HOME = WORKDIR / "fake_hermes_home"
TASK_ID = "hermesdocker-livecheck-task1"
BRANCH = f"wt/{TASK_ID}"

PLANTED_SECRET_NAME = "ASES_PLANTED_SECRET_KEY"
PLANTED_SECRET_VALUE = "ASES-HERMESDOCKER-LIVECHECK-DO-NOT-LEAK-703184"
# Same shape as workergit_live_check.py's container-side check: name-based, case-insensitive, with the
# python base image's own public GPG_KEY fingerprint excluded (see that script's container script comment).
_CREDENTIAL_NAME_RE = re.compile(r"key|token|secret|password", re.IGNORECASE)
_CREDENTIAL_NAME_EXCLUDE = frozenset({"GPG_KEY"})

_FAILURES: list[str] = []
_FINDINGS: list[str] = []
_RESULTS: list[tuple[str, bool]] = []


def _ok(label: str, detail: str = "") -> None:
    print(f"[PASS] {label}" + (f" - {detail}" if detail else ""))
    _RESULTS.append((label, True))


def _fail(label: str, detail: str) -> None:
    print(f"[FAIL] {label} - {detail}")
    _FAILURES.append(f"{label}: {detail}")
    _RESULTS.append((label, False))


def _note(label: str, detail: str) -> None:
    print(f"[NOTE] {label} - {detail}")
    _FINDINGS.append(f"{label}: {detail}")


def _force_rmtree(path: pathlib.Path) -> None:
    """shutil.rmtree that also clears read-only bits (git marks object files read-only; Windows honours
    that for delete). Mirrors scripts/workergit_live_check.py's own helper."""
    def _clear_and_retry(func, failed_path, _exc_info):
        os.chmod(failed_path, stat.S_IWRITE)
        func(failed_path)
    try:
        shutil.rmtree(path, onerror=_clear_and_retry)
    except OSError:
        pass


def _run_git(args: list[str], *, cwd: pathlib.Path) -> subprocess.CompletedProcess:
    argv = [*gitexec_mod.GIT, "-C", str(cwd), *args]
    return subprocess.run(argv, capture_output=True, text=True, timeout=60, env=gitexec_mod.git_env())


def scrub_and_plant_env() -> list[str]:
    """Remove every credential-shaped name from this process's inherited environment (so any real
    key a parent shell happened to export cannot confound the result), then plant one fake
    ASES_PLANTED_SECRET_KEY. Returns the names removed (never their values)."""
    removed = []
    for name in list(os.environ):
        if _CREDENTIAL_NAME_RE.search(name) and name not in _CREDENTIAL_NAME_EXCLUDE:
            removed.append(name)
            os.environ.pop(name, None)
    os.environ[PLANTED_SECRET_NAME] = PLANTED_SECRET_VALUE
    return removed


def step1_build_repo_and_worktree() -> pathlib.Path | None:
    """Same shape as workergit_live_check.py's step1: worktree.useRelativePaths=true, then a worktree cut
    the way Hermes's Kanban dispatcher does (hermes_cli/kanban_db_workspace.py:443
    `<repo>/.worktrees/<task_id>`, `git worktree add -b <branch> <path> HEAD`). Also commits a `.env`
    fixture file so item 4 of "what to build" (what Hermes does with a committed .env) has something
    real to observe."""
    if REPO.exists():
        _force_rmtree(REPO)
    if REPO.exists():
        _fail("clean work directory", f"could not remove the previous run's {REPO}; delete it by hand and re-run")
        return None
    REPO.mkdir(parents=True, exist_ok=True)
    steps = [
        (["init", "-q", "-b", "main"], "init"),
        (["config", "worktree.useRelativePaths", "true"], "config worktree.useRelativePaths"),
        (["config", "core.autocrlf", "false"], "config core.autocrlf"),
    ]
    for args, label in steps:
        result = _run_git(args, cwd=REPO)
        if result.returncode != 0:
            _fail(f"repo setup: {label}", f"{result.stdout}{result.stderr}")
            return None
    (REPO / "README.md").write_text("hermesdocker live check throwaway repo\n", encoding="utf-8")
    (REPO / ".env").write_text("FAKE_WORKER_TOKEN=not-a-real-secret-committed-on-purpose\n", encoding="utf-8")
    add = _run_git(["add", "-A"], cwd=REPO)
    commit = _run_git(
        ["-c", "user.name=ASES hermesdocker live check", "-c", "user.email=ases-hermesdocker-livecheck@example.invalid",
         "commit", "-q", "-m", "hermesdocker live check: initial commit"],
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
    if not gitfile.startswith("gitdir: ../../.git/worktrees/"):
        _fail("relative gitdir", f"expected 'gitdir: ../../.git/worktrees/{TASK_ID}', got {gitfile!r}")
        return None
    _ok("worktree cut with relative gitdir", f"{worktree} on branch {BRANCH}")
    return worktree


def prepare_fake_hermes_home() -> None:
    """Copy ONLY coder-1's config.yaml (never its .env, which holds a real provider key, and never
    anything else) into a throwaway Hermes home under this script's own work directory. See the module
    docstring for why the real profile directory itself is never used as HERMES_HOME."""
    if FAKE_HOME.exists():
        _force_rmtree(FAKE_HOME)
    FAKE_HOME.mkdir(parents=True, exist_ok=True)
    config_text = CODER1_CONFIG.read_text(encoding="utf-8")
    (FAKE_HOME / "config.yaml").write_text(config_text, encoding="utf-8")


def drive_hermes_terminal():
    """Set HERMES_HOME to the throwaway copy, set TERMINAL_CWD exactly as the kanban dispatcher does
    (hermes_cli/kanban_db_dispatch.py:2590 `env["TERMINAL_CWD"] = workspace`), then ask Hermes's OWN
    tools.terminal_tool.ensure_task_env() for a working environment. That call alone is what loads
    coder-1's config through Hermes's real bridge and builds the real DockerEnvironment; see the module
    docstring for exactly which Hermes functions run and why cli.py itself is not imported."""
    if str(HERMES_AGENT_DIR) not in sys.path:
        sys.path.insert(0, str(HERMES_AGENT_DIR))
    try:
        from tools import terminal_tool as terminal_tool_mod
    except Exception as e:
        _fail("import tools.terminal_tool (Hermes's own module)", f"{type(e).__name__}: {e}")
        return None, None
    try:
        env_obj = terminal_tool_mod.ensure_task_env(TASK_ID)
    except Exception as e:
        _fail("tools.terminal_tool.ensure_task_env", f"{type(e).__name__}: {e}")
        return None, terminal_tool_mod
    if env_obj is None:
        _fail(
            "tools.terminal_tool.ensure_task_env",
            "returned None: Hermes resolved env_type to 'local', not 'docker' -- coder-1's terminal.backend "
            "was not bridged the way this script expected",
        )
        return None, terminal_tool_mod
    _ok("ensure_task_env built a real environment", f"{type(env_obj).__module__}.{type(env_obj).__name__}")
    return env_obj, terminal_tool_mod


def check_pinned_image(env_obj) -> None:
    cfg = config_mod.load_swarm_config(REPO_ROOT / "config" / "swarm.yaml")
    pinned = cfg.sandbox["image"]
    actual = getattr(env_obj, "_image", None)
    if actual == pinned:
        _ok("docker image matches config/swarm.yaml's pin", pinned)
    else:
        _fail("docker image matches config/swarm.yaml's pin", f"Hermes used {actual!r}, ASES config pins {pinned!r}")


def print_docker_argv(env_obj) -> None:
    """The exact docker run argv Hermes built, read directly off the constructed DockerEnvironment
    (env values are never in this argv by Hermes's own design: forwarded values travel via the docker
    CLI subprocess's own environment, paired with name-only -e flags -- tools/environments/docker.py's
    own _docker_client_env docstring, issue #96268 -- so printing this argv verbatim is safe)."""
    image = getattr(env_obj, "_image", "?")
    labels = getattr(env_obj, "_labels", {})
    run_args = getattr(env_obj, "_all_run_args", [])
    argv_text = " ".join(str(a) for a in run_args)
    if PLANTED_SECRET_VALUE in argv_text or PLANTED_SECRET_NAME in argv_text:
        _fail("docker argv never contains the planted secret", "found in the printed run args")
    else:
        _ok("docker argv never contains the planted secret (name or value)", "")
    print("--- docker run argv Hermes built (env flags are name-only by Hermes's own design) ---")
    print(f"image: {image}")
    print(f"labels: {labels}")
    print(f"run args: {argv_text}")
    print("--- end docker run argv ---")


def _ran_and_failed(out: str) -> bool:
    """True only when a probe's own `echo EXITCODE:$?` marker was printed with a non-zero code: the command ran
    inside the container and failed, as opposed to the exec never starting at all."""
    codes = re.findall(r"EXITCODE:(\d+)", out)
    return bool(codes) and codes[-1] != "0"


def _probe(env_obj, label: str, command: str, *, cwd: str = "/workspace", timeout: int = 30):
    result = env_obj.execute(command, cwd=cwd, timeout=timeout)
    return result.get("returncode", -1), str(result.get("output", ""))


def run_probes(env_obj, worktree: pathlib.Path) -> tuple[bool, bool]:
    """Runs every probe item 4 of "what to build" asks for. Returns (commit_succeeded,
    packed_refs_lock_line_seen)."""
    rc, out = _probe(env_obj, "pwd", "pwd")
    if rc == 0 and out.strip() == "/workspace":
        _ok("pwd is /workspace", out.strip())
    else:
        _fail("pwd is /workspace", f"rc={rc} output={out.strip()!r}")

    rc, out = _probe(env_obj, "worktree files present", "ls -a")
    if rc == 0 and "README.md" in out and ".env" in out:
        _ok("worktree files present at /workspace", out.strip().replace("\n", " "))
    else:
        _fail("worktree files present at /workspace", f"rc={rc} output={out.strip()!r}")

    rc, out = _probe(env_obj, "id -u", "id -u")
    if rc == 0 and out.strip() and out.strip() != "0":
        _ok("container runs as non-root", f"id -u={out.strip()!r}")
    else:
        _fail("container runs as non-root", f"rc={rc} id -u={out.strip()!r}")

    rc, out = _probe(env_obj, "git status", "git status")
    if rc == 0:
        _ok("git status works")
    else:
        _fail("git status works", f"rc={rc} output={out[:400]!r}")

    commit_cmd = (
        "echo hello-from-hermesdocker-check > f.txt && git add f.txt && "
        "git -c user.name=hermesdocker-check -c user.email=hermesdocker-check@example.invalid "
        "commit -m 'hermesdocker live check: from the sandbox'"
    )
    rc, out = _probe(env_obj, "git add + commit", commit_cmd)
    packed_refs_seen = "Unable to create '/.git/packed-refs.lock'" in out
    commit_ok = rc == 0
    if commit_ok:
        _ok("git add + commit succeeds", f"packed-refs.lock line seen: {packed_refs_seen}")
    else:
        _fail("git add + commit succeeds", f"rc={rc} output={out[:600]!r}")

    rc, out = _probe(env_obj, "hooks write blocked", "(echo pwn > /.git/hooks/pre-commit) 2>&1; echo EXITCODE:$?")
    # Every negative probe must prove it RAN (its own EXITCODE marker printed, non-zero) and failed for the
    # expected reason: an exec that never started must not count as "blocked" (the py311-2 run, where every exec
    # died with exit 127 before the command, passed these vacuously).
    if _ran_and_failed(out) and "Read-only file system" in out:
        _ok("writing /.git/hooks/pre-commit fails", out.strip().replace("\n", " ")[:200])
    else:
        _fail("writing /.git/hooks/pre-commit fails", out.strip()[:300])

    rc, out = _probe(
        env_obj, "config write blocked",
        "git config --file /.git/config core.evil true 2>&1; echo EXITCODE:$?",
    )
    if _ran_and_failed(out) and "Read-only file system" in out:
        _ok("git config --file /.git/config write fails", out.strip().replace("\n", " ")[:200])
    else:
        _fail("git config --file /.git/config write fails", out.strip()[:300])

    rc, out = _probe(env_obj, "env leak check", "env")
    lines = out.splitlines()
    if rc != 0 or not any(ln.startswith("PATH=") for ln in lines):
        _fail("env shows no credential-shaped name and no planted secret", f"env did not run: rc={rc} {out[:200]!r}")
        lines = []
    leaked_names = [
        ln.split("=", 1)[0] for ln in lines
        if "=" in ln and _CREDENTIAL_NAME_RE.search(ln.split("=", 1)[0])
        and ln.split("=", 1)[0] not in _CREDENTIAL_NAME_EXCLUDE
    ]
    planted_leaked = any(PLANTED_SECRET_VALUE in ln or PLANTED_SECRET_NAME in ln for ln in lines)
    if not lines:
        pass  # already reported as a failure above
    elif not leaked_names and not planted_leaked:
        _ok("env shows no credential-shaped name and no planted secret", f"{len(lines)} env vars total")
    else:
        _fail(
            "env shows no credential-shaped name and no planted secret",
            f"leaked names={leaked_names} planted_leaked={planted_leaked}",
        )

    rc, out = _probe(
        env_obj, "network blocked",
        "python3 -c \"import urllib.request; urllib.request.urlopen('https://example.com', timeout=3)\" "
        "2>&1; echo EXITCODE:$?",
        timeout=20,
    )
    if _ran_and_failed(out):
        _ok("network call to https://example.com fails", out.strip().splitlines()[-1] if out.strip() else "")
    else:
        _fail("network call to https://example.com fails", out.strip()[:300])

    rc, out = _probe(env_obj, "committed .env observation", "cat .env 2>&1; echo EXITCODE:$?")
    if rc == 0 and "EXITCODE:0" in out:
        _note(
            "committed .env in the worktree",
            f"Hermes's own worker terminal does not mask it (unlike ASES's own gate-run sandbox, which "
            f"masks a committed .env for the controller's gate commands): container shows {out.strip()!r}",
        )
    else:
        _note(
            "committed .env in the worktree",
            f"could not be observed this run (the exec itself failed before reaching `cat`): {out.strip()!r}",
        )

    return commit_ok, packed_refs_seen


def verify_from_host(worktree: pathlib.Path) -> None:
    log = _run_git(["log", "--oneline", "-1"], cwd=worktree)
    if "hermesdocker live check: from the sandbox" in log.stdout:
        _ok("commit visible from host on the worktree branch", log.stdout.strip())
    else:
        _fail("commit visible from host on the worktree branch", f"git log shows: {log.stdout!r}")


def cleanup_container(env_obj, terminal_tool_mod) -> None:
    container_id = getattr(env_obj, "_container_id", None)
    try:
        env_obj.cleanup()
    except Exception as e:
        _fail("container cleanup() call", f"{type(e).__name__}: {e}")
        return
    try:
        finished = env_obj.wait_for_cleanup(timeout=30)
    except Exception as e:
        finished = False
        _note("wait_for_cleanup raised", f"{type(e).__name__}: {e}")
    if not finished:
        _note("wait_for_cleanup", "teardown thread did not report finished within 30s; checking directly")
    if container_id:
        check = subprocess.run(
            ["docker", "inspect", container_id], capture_output=True, text=True, timeout=15,
        )
        if check.returncode != 0:
            _ok("container Hermes created is stopped and removed", container_id[:12])
        else:
            _fail("container Hermes created is stopped and removed", f"docker inspect still finds {container_id[:12]}")
    else:
        _note("container id", "no container id was captured (nothing to remove by id)")
    try:
        terminal_tool_mod._stop_cleanup_thread()
    except Exception:
        pass


def main() -> int:
    print(
        "HERMESDOCKER live check: Hermes 0.21.3's OWN Docker terminal code, driven by the real coder-1 "
        "profile config, real Docker, a throwaway repo, zero model-provider quota."
    )
    print(f"work directory: {WORKDIR}")

    if WORKDIR.exists():
        _force_rmtree(WORKDIR)
    if WORKDIR.exists():
        print(f"[FAIL] clean work directory - could not remove {WORKDIR} from a previous run; delete it and re-run")
        return 1
    WORKDIR.mkdir(parents=True, exist_ok=True)

    ok, why = sandbox_mod.docker_available()
    if not ok:
        _fail("docker_available", why)
        print("\nSummary: FAIL (Docker is not reachable; nothing below can run)")
        return 1
    _ok("docker_available", why)

    if not CODER1_CONFIG.is_file():
        _fail("coder-1 profile config.yaml exists", str(CODER1_CONFIG))
        print("\nSummary: FAIL")
        return 1

    worktree = step1_build_repo_and_worktree()
    if worktree is None:
        print("\nSummary: FAIL (repo/worktree setup did not succeed; see above)")
        return 1

    prepare_fake_hermes_home()

    removed_names = scrub_and_plant_env()
    if removed_names:
        _note("scrubbed credential-shaped env vars before driving Hermes", ", ".join(sorted(removed_names)))
    os.environ["HERMES_HOME"] = str(FAKE_HOME)
    os.environ["TERMINAL_CWD"] = str(worktree)

    config_mtime_before = CODER1_CONFIG.stat().st_mtime_ns
    profile_listing_before = sorted(p.name for p in CODER1_PROFILE_DIR.iterdir())

    env_obj, terminal_tool_mod = drive_hermes_terminal()
    if env_obj is None:
        _force_rmtree(WORKDIR)
        print("\nSummary: FAIL (Hermes's own code did not produce a working environment; see above)")
        return 1

    config_mtime_after = CODER1_CONFIG.stat().st_mtime_ns
    profile_listing_after = sorted(p.name for p in CODER1_PROFILE_DIR.iterdir())
    if config_mtime_before == config_mtime_after and profile_listing_before == profile_listing_after:
        _ok("coder-1 profile directory untouched", "config.yaml mtime and directory listing both unchanged")
    else:
        _fail(
            "coder-1 profile directory untouched",
            f"mtime changed={config_mtime_before != config_mtime_after} "
            f"listing changed={profile_listing_before != profile_listing_after} "
            f"(before={profile_listing_before} after={profile_listing_after})",
        )

    check_pinned_image(env_obj)
    print_docker_argv(env_obj)

    commit_ok, packed_refs_seen = run_probes(env_obj, worktree)
    if commit_ok:
        verify_from_host(worktree)

    cleanup_container(env_obj, terminal_tool_mod)

    config_mtime_final = CODER1_CONFIG.stat().st_mtime_ns
    if config_mtime_final != config_mtime_before:
        _fail("coder-1 config.yaml mtime unchanged (final check)", "mtime changed across the whole run")

    _force_rmtree(WORKDIR)
    if WORKDIR.exists():
        _note("work directory cleanup", f"{WORKDIR} could not be fully removed; inspect and remove by hand")

    print()
    print("--- PASS/FAIL table ---")
    for label, passed in _RESULTS:
        print(f"{'PASS' if passed else 'FAIL'}: {label}")
    if _FINDINGS:
        print(f"\nFindings ({len(_FINDINGS)}):")
        for item in _FINDINGS:
            print(f" - {item}")
    print(f"\npacked-refs.lock harmless line seen during commit: {packed_refs_seen}")
    if _FAILURES:
        print(f"\nSummary: FAIL ({len(_FAILURES)} finding(s))")
        for item in _FAILURES:
            print(f" - {item}")
        return 1
    print("\nSummary: PASS (Hermes's own Docker terminal code started a working worker container "
          "from the real coder-1 profile config)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
