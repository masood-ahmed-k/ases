"""Independent ASES-CFG-05 check of the `swarm critique` reviewer launch, taken from the top of the real path.

cli.py calls critic.run_critique(...) with no invoke= (cli.py, the swarm critique command), so the reviewer's hermes
process is started by whatever run_critique's DEFAULT invoke is (critic.default_invoke at the time of writing). The
builder's test drove default_invoke directly; this one drives run_critique with the default wiring, forces the repair
call as well (an invalid first reply), and checks EVERY subprocess.run call the critique made. subprocess.run and
hermes_path are faked, so no real hermes runs and no model is called."""
import os
import subprocess

from ases import critic, hermes

MARKER = "leak-marker-7f3a"
CREDENTIALS = {
    "OPENROUTER_API_KEY": f"sk-or-{MARKER}-1",
    "openai_api_key": f"sk-{MARKER}-2",  # lower case: Windows upper-cases it, POSIX keeps it; both must go
    "HF_TOKEN": f"hf_{MARKER}-3",
    "AWS_SECRET_ACCESS_KEY": f"aws-{MARKER}-4",
    "NPM_AUTH": f"npm-{MARKER}-5",
    "DB_PASSWORD": f"pw-{MARKER}-6",
}
LAUNCH_VARS = ("PATH", "SYSTEMROOT", "TEMP", "COMSPEC", "PATHEXT") if os.name == "nt" else ("PATH", "HOME")


def test_swarm_critique_default_path_never_hands_a_credential_to_the_reviewer_process(tmp_path, monkeypatch):
    for name, value in CREDENTIALS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("ASES_HARMLESS_SETTING", "kept")
    monkeypatch.setattr(hermes, "hermes_path", lambda: "hermes")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return subprocess.CompletedProcess(argv, 0, "not json at all", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    repo = tmp_path / "repo"
    (repo / "docs" / "ases").mkdir(parents=True)
    plan = repo / "docs" / "ases" / "plan.json"
    plan.write_text('{"project": "demo", "tasks": []}\n', encoding="utf-8")

    critique = critic.run_critique(repo=repo, plan_path=plan, estimate_text="budget: 1 request", timeout=5)

    assert not critique.valid
    assert len(calls) == 2, "the first call plus one repair call, both through the default invoke"
    banned_names = {name.upper() for name in CREDENTIALS}
    for argv, kwargs in calls:
        assert argv[:4] == ["hermes", "-p", "reviewer", "-z"] and "-t" not in argv
        env = kwargs.get("env")
        assert env is not None, "no env= was passed: the reviewer inherits the whole parent environment"
        assert env is not os.environ and isinstance(env, dict)
        assert not banned_names & {name.upper() for name in env}
        assert not any(MARKER in value for value in env.values())
        assert env["ASES_HARMLESS_SETTING"] == "kept"
        for name in LAUNCH_VARS:
            if name in os.environ:
                assert env[name] == os.environ[name], f"{name} must survive the scrub or nothing launches"
        assert kwargs["timeout"] == 5 and kwargs["capture_output"] is True and kwargs["text"] is True
        assert kwargs["encoding"] == "utf-8" and kwargs["errors"] == "replace"
