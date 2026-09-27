"""procenv.scrubbed_environ: the one definition of a credential-shaped environment variable name (ASES-CFG-05,
ASES-SEC-01), shared by hermes._run, evals._run_process and evalkit.codeeval.scrubbed_env."""
import os

from ases import procenv

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
