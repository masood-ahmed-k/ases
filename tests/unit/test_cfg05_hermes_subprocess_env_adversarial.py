"""Independent ASES-CFG-05 check of the two hermes launch sites the fix names: hermes._run and evals._run_process.

Written by the verifier, not the builder. A real-shaped provider key (and two case variants, because Windows
upper-cases environment names and POSIX does not) is exported into this process, each function under test is called
as its existing tests call it, and only the subprocess layer is faked (hermes.subprocess.run and hermes_path for
_run; the injectable popen for _run_process). The check is on the FLATTENED env dict handed to the fake, so a secret
that survived as a name, as a value, or inside any other value would all be caught. No real hermes runs here."""
import os
import subprocess

from ases import evals, hermes

SECRET = "sk-test-adversarial-12345"
CREDENTIALS = {
    "OPENROUTER_API_KEY": SECRET + "-upper",
    "openrouter_api_key": SECRET + "-lower",  # on Windows this is the same variable as the one above; fine either way
    "Anthropic_Api_Key": SECRET + "-mixed",
}
LAUNCH_VARS = ("PATH", "SYSTEMROOT", "TEMP", "COMSPEC", "PATHEXT") if os.name == "nt" else ("PATH", "HOME")


def _flatten(env) -> str:
    return "\n".join(f"{name}={value}" for name, value in env.items())


def _check(env, where: str) -> None:
    assert env is not None, f"{where} passed no env=, so hermes inherits the whole parent environment"
    assert isinstance(env, dict) and env is not os.environ, f"{where} must hand over a fresh copy, never os.environ"
    flat = _flatten(env)
    assert SECRET not in flat, f"{where}: the provider key reached the hermes environment:\n{flat}"
    assert not {name.upper() for name in CREDENTIALS} & {name.upper() for name in env}
    assert env["ASES_KEEPALIVE"] == "kept" and env["ASES_PASSTHROUGH"] == "kept", f"{where}: scrub is too broad"
    for name in LAUNCH_VARS:
        if name in os.environ:
            assert env[name] == os.environ[name], f"{where}: {name} must survive the scrub or nothing launches"


def test_both_hermes_launch_sites_drop_a_real_shaped_provider_key_and_keep_what_a_launch_needs(monkeypatch, tmp_path):
    for name, value in CREDENTIALS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("ASES_KEEPALIVE", "kept")  # "keep" is not "key"
    monkeypatch.setenv("ASES_PASSTHROUGH", "kept")  # "pass" is not "passw"
    # "TOKENIZER" does contain "token", so this one is expected to be dropped: the pattern is a substring match by
    # design (it mirrors the pre-existing codeeval pattern). Recorded here so the trade-off is visible, not hidden.
    monkeypatch.setenv("ASES_TOKENIZER_MODE", "dropped-by-design")

    # Site 1: hermes._run, the function every hermes subcommand goes through.
    seen_run = {}

    def fake_run(argv, **kwargs):
        seen_run.update(argv=list(argv), kwargs=kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout="Hermes Agent v0.21.3", stderr="")

    monkeypatch.setattr(hermes.subprocess, "run", fake_run)
    monkeypatch.setattr(hermes, "hermes_path", lambda: "hermes")
    result = hermes._run(["--version"], timeout=5)
    assert result.returncode == 0 and seen_run["argv"] == ["hermes", "--version"]
    _check(seen_run["kwargs"].get("env"), "hermes._run")
    assert "ASES_TOKENIZER_MODE" not in seen_run["kwargs"]["env"]
    # The rest of the call is untouched by the fix.
    assert {k: v for k, v in seen_run["kwargs"].items() if k != "env"} == {
        "capture_output": True, "text": True, "timeout": 5, "encoding": "utf-8", "errors": "replace",
    }

    # Site 2: evals._run_process, the evaluation harness's one-shot launch, including its timeout path, so the
    # kill-tree logic is confirmed intact with the new env in place.
    seen_popen = []
    killed = []

    class Proc:
        pid = 4242

        def __init__(self, timeout_first: bool):
            self.calls = 0
            self.timeout_first = timeout_first
            self.returncode = 0

        def communicate(self, timeout=None):
            self.calls += 1
            if self.timeout_first and self.calls == 1:
                raise subprocess.TimeoutExpired("hermes", timeout)
            return ("out", "err")

    def make_popen(timeout_first: bool):
        def popen(argv, **kwargs):
            seen_popen.append((list(argv), kwargs))
            return Proc(timeout_first)
        return popen

    normal = evals._run_process(["hermes", "-z", "p"], tmp_path, 9, popen=make_popen(False), kill_tree=killed.append)
    assert (normal.returncode, normal.stdout, normal.stderr, normal.timed_out, normal.started) == (0, "out", "err", False, True)
    assert killed == []

    timed = evals._run_process(["hermes", "-z", "p"], tmp_path, 9, popen=make_popen(True), kill_tree=killed.append)
    assert timed.returncode == -1 and timed.timed_out and timed.started
    assert killed == [4242], "a timeout must still stop the whole process tree"
    assert "did not finish within 9s" in timed.stderr

    assert len(seen_popen) == 2
    for argv, kwargs in seen_popen:
        assert argv == ["hermes", "-z", "p"]
        env = kwargs.get("env")
        _check(env, "evals._run_process")
        assert "ASES_TOKENIZER_MODE" not in env
        assert env["PYTHONIOENCODING"] == "utf-8" and env["PYTHONUTF8"] == "1"
        assert {k: v for k, v in kwargs.items() if k != "env"} == {
            "cwd": str(tmp_path), "stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
            "text": True, "encoding": "utf-8", "errors": "replace",
        }
