"""hermes._run: the environment a hermes subprocess is started with (ASES-CFG-05).

Blueprint 10.2: "Never export provider keys in the shell that launches the gateway or the controller". Nothing stops
a person doing it anyway, so the one function every hermes subcommand goes through must not hand such a variable on
to the hermes process, while everything a launch needs (PATH, SYSTEMROOT on Windows) must still be there. The scrub
itself is procenv.scrubbed_environ (tested in test_procenv.py); this file checks that _run really uses it and changes
nothing else about the call. `subprocess.run` and `hermes_path` are faked, so no real hermes runs here."""
import os
import subprocess

from ases import hermes

# One name per word the pattern knows, in mixed case, plus the two spellings a real leak would most likely take.
CREDENTIAL_NAMES = (
    "OPENAI_API_KEY", "openrouter_api_key", "My_Token", "CLIENT_SECRET", "DB_PASSWORD", "DB_PASSWD",
    "AWS_CREDENTIALS", "AUTH_HEADER", "HTTP_COOKIE", "TMUX_SESSION_NAME",
)
HARMLESS = "ASES_HARMLESS_SETTING"


def _seen_run(monkeypatch, *, returncode=0, stdout="Hermes Agent v0.21.3", stderr=""):
    """Fakes subprocess.run as hermes.py sees it (and hermes_path, so no PATH lookup); returns what _run called it
    with, as {"argv": [...], "kwargs": {...}}."""
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(argv=list(argv), kwargs=kwargs)
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(hermes.subprocess, "run", fake_run)
    monkeypatch.setattr(hermes, "hermes_path", lambda: "hermes")
    return seen


def test_run_starts_hermes_with_a_credential_scrubbed_environment(monkeypatch):
    for index, name in enumerate(CREDENTIAL_NAMES):
        monkeypatch.setenv(name, f"leaked-value-{index}")
    monkeypatch.setenv(HARMLESS, "kept")
    seen = _seen_run(monkeypatch)
    hermes._run(["--version"], timeout=5)
    env = seen["kwargs"].get("env")
    assert env is not None, "hermes._run passed no env=, so the hermes process inherits the whole parent environment"
    present = {name.upper() for name in env}  # Windows upper-cases environment names; compare the same way everywhere
    assert not {name.upper() for name in CREDENTIAL_NAMES} & present
    assert not any(value.startswith("leaked-value-") for value in env.values())
    assert env[HARMLESS] == "kept"
    assert env["PATH"] == os.environ["PATH"]  # a launch still finds its executables
    if os.name == "nt":
        assert env["SYSTEMROOT"] == os.environ["SYSTEMROOT"]  # without it Windows programs fail to start


def test_run_changes_nothing_else_about_the_subprocess_call(monkeypatch):
    seen = _seen_run(monkeypatch, returncode=3, stdout="out", stderr="err")
    result = hermes._run(["kanban", "--board", "b", "list", "--json"], timeout=7)
    assert seen["argv"] == ["hermes", "kanban", "--board", "b", "list", "--json"]
    assert {k: v for k, v in seen["kwargs"].items() if k != "env"} == {
        "capture_output": True, "text": True, "timeout": 7, "encoding": "utf-8", "errors": "replace",
    }
    assert (result.returncode, result.stdout, result.stderr) == (3, "out", "err")
