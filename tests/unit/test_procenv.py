"""procenv.scrubbed_environ: the one definition of a credential-shaped environment variable name (ASES-CFG-05,
ASES-SEC-01), shared by hermes._run, evals._run_process and evalkit.codeeval.scrubbed_env.

Also procenv.kill_process_tree (round 13, TIDY): the one "stop a process and everything it started"
implementation gates.py's and evals.py's own `_kill_process_tree` both delegate to. Their own test files already
prove the branch logic against fakes (never a real process); this file proves the real-process case once,
directly, against the shared helper itself."""
import os
import subprocess
import sys
import time

from ases import procenv, reconcile

WORDS = ("key", "token", "secret", "passw", "credential", "auth", "cookie", "session")


def _names(env) -> set:
    return {name.upper() for name in env}  # Windows upper-cases environment names; compare the same way everywhere


def test_scrubbed_environ_drops_exactly_the_credential_shaped_names_and_keeps_the_rest(monkeypatch):
    for word in WORDS:
        monkeypatch.setenv(f"X_{word.upper()}_Y", "dropped")
        monkeypatch.setenv(f"x_{word}_y", "dropped")  # case does not matter (on Windows this is the same variable)
    monkeypatch.setenv("ASES_HARMLESS_SETTING", "kept")
    monkeypatch.setenv("ASES_KEEPS_PROFILE_DIR", "kept too")  # "keep" is not "key"
    env = procenv.scrubbed_environ()
    for word in WORDS:
        assert f"X_{word.upper()}_Y" not in _names(env)
    assert "dropped" not in env.values()
    assert env["ASES_HARMLESS_SETTING"] == "kept" and env["ASES_KEEPS_PROFILE_DIR"] == "kept too"
    assert env["PATH"] == os.environ["PATH"]  # a launch still finds its executables
    if os.name == "nt":
        assert env["SYSTEMROOT"] == os.environ["SYSTEMROOT"]  # without it Windows programs fail to start
    assert _names(env) == {name.upper() for name in os.environ if not procenv._CREDENTIAL_ENV.search(name)}
    env["ASES_ONLY_IN_THE_COPY"] = "1"  # a fresh dict, never os.environ itself
    assert "ASES_ONLY_IN_THE_COPY" not in os.environ


# --- round 8: the GIT_AUTHOR_* exemption (ASES-CFG-04/CFG-05, architect decision) --------------------------------


def test_scrubbed_environ_exempts_git_author_identity_even_though_it_matches_auth(monkeypatch):
    """GIT_AUTHOR_NAME/EMAIL/DATE match _CREDENTIAL_ENV only because "AUTHOR" contains "auth". They carry no
    secret and grant no capability, and a gate command that makes a commit can need them, so they are exempt by
    exact name (procenv._EXEMPT_EXACT_NAMES)."""
    monkeypatch.setenv("GIT_AUTHOR_NAME", "A U Thor")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "a@example.test")
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2026-09-27T00:00:00Z")

    env = procenv.scrubbed_environ()

    assert env["GIT_AUTHOR_NAME"] == "A U Thor"
    assert env["GIT_AUTHOR_EMAIL"] == "a@example.test"
    assert env["GIT_AUTHOR_DATE"] == "2026-09-27T00:00:00Z"


def test_scrubbed_environ_exemption_is_case_insensitive_like_the_pattern_it_overrides(monkeypatch):
    monkeypatch.setenv("git_author_name", "lower case name")

    env = procenv.scrubbed_environ()

    assert "lower case name" in env.values()


def test_scrubbed_environ_does_not_exempt_capability_bearing_names_that_also_say_auth(monkeypatch):
    """SSH_AUTH_SOCK (an ssh-agent socket) and XAUTHORITY are capability-bearing, not merely auth-shaped like
    GIT_AUTHOR_*, so neither is added to the exemption: both are still dropped."""
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    monkeypatch.setenv("XAUTHORITY", "/home/x/.Xauthority")

    env = procenv.scrubbed_environ()

    assert "SSH_AUTH_SOCK" not in _names(env)
    assert "XAUTHORITY" not in _names(env)


def test_scrubbed_environ_leaves_sessionname_dropped_as_before(monkeypatch):
    """Round 8 design decision: SESSIONNAME gets no exemption and stays dropped, exactly as the pre-existing
    "session" pattern already dropped it."""
    monkeypatch.setenv("SESSIONNAME", "Console")

    env = procenv.scrubbed_environ()

    assert "SESSIONNAME" not in _names(env)


def test_scrubbed_environ_exemption_is_by_exact_name_not_by_substring(monkeypatch):
    """A variable that merely CONTAINS "GIT_AUTHOR" but is not one of the three exact exempt names is still
    dropped: the exemption is not a looser prefix or substring match."""
    monkeypatch.setenv("GIT_AUTHOR_NAME_EXTRA", "should still be dropped")

    env = procenv.scrubbed_environ()

    assert "GIT_AUTHOR_NAME_EXTRA" not in _names(env)


# --- round 13 (TIDY): kill_process_tree, the shared "stop a process and everything it started" ------------------


def test_kill_process_tree_kills_a_real_child_and_the_grandchild_it_spawned(tmp_path):
    """gates.py and evals.py each need this because a single Popen.kill()/terminate() (or, on POSIX, a plain
    os.kill/os.killpg on the wrong pid) can leave a grandchild running: a shell=True command's real process
    (gates.py) or a launcher's own model-call child (evals.py). Proves the shared helper really ends both, on
    whichever platform this suite runs on, rather than only the mocked branch logic each caller's own test file
    already covers."""
    grandchild_started = tmp_path / "grandchild-started.txt"
    child_started = tmp_path / "child-started.txt"
    grandchild_pid_file = tmp_path / "grandchild-pid.txt"

    grandchild_script = tmp_path / "grandchild.py"
    grandchild_script.write_text(
        "import pathlib, sys, time\n"
        "pathlib.Path(sys.argv[1]).write_text('1')\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    child_script = tmp_path / "child.py"
    child_script.write_text(
        "import pathlib, subprocess, sys, time\n"
        "pathlib.Path(sys.argv[1]).write_text('1')\n"
        "gc = subprocess.Popen([sys.executable, sys.argv[3], sys.argv[2]])\n"
        "pathlib.Path(sys.argv[4]).write_text(str(gc.pid))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )

    # Matches gates.py's own _run_commands: on POSIX the child gets its own session, so its pid is also its
    # process group id and os.killpg(child.pid, ...) (process_group=True below) ends the whole group, the
    # grandchild included. Nothing extra is needed on Windows: taskkill /T walks the real OS process tree either
    # way.
    popen_kwargs = {} if os.name == "nt" else {"start_new_session": True}
    child = subprocess.Popen(
        [sys.executable, str(child_script), str(child_started), str(grandchild_started),
         str(grandchild_script), str(grandchild_pid_file)],
        **popen_kwargs,
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not (child_started.exists() and grandchild_started.exists()):
            time.sleep(0.1)
        assert child_started.exists() and grandchild_started.exists(), "the child and grandchild never started"
        grandchild_pid = int(grandchild_pid_file.read_text().strip())
        assert reconcile.pid_alive(child.pid) is True
        assert reconcile.pid_alive(grandchild_pid) is True

        procenv.kill_process_tree(child.pid, is_windows=(os.name == "nt"), process_group=True)

        # Polled, not child.wait(timeout=...): a bare wait() would raise TimeoutExpired on a slow machine and
        # skip the grandchild check entirely, rather than reporting the aliveness this test is actually about.
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and reconcile.pid_alive(child.pid):
            time.sleep(0.1)
        assert reconcile.pid_alive(child.pid) is False
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and reconcile.pid_alive(grandchild_pid):
            time.sleep(0.1)
        assert reconcile.pid_alive(grandchild_pid) is False
        child.wait(timeout=5)  # reap the now-dead child so it does not linger as a zombie
    finally:
        if child.poll() is None:
            child.kill()
