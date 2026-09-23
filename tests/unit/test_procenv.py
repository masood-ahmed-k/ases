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
