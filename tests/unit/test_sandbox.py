"""The worker sandbox policy (ASES-SEC-02/03/05/06/07, ASES-CFG-04, ASES-QG-04). No Docker, no network, no writes
to Hermes: every probe gets a fake runner, and paths are lexical strings so the results are the same on every
platform. The one test that reads real Hermes files only reads, and skips when Hermes is not installed."""
import ast
import dataclasses
import json
import logging
import os
import pathlib
import re
import shutil
import subprocess
import sys
import types

import pytest
import yaml

from ases import sandbox
from ases.sandbox import SandboxConfigError, SandboxInfrastructureError, SandboxPolicy

HOME = "C:/Users/tester"
POSIX_HOME = "/home/tester"
IMAGE = "registry.example/ases-toolchain:1.4.2"
POLICY = SandboxPolicy(image=IMAGE)
WIN_WT = "C:\\work\\wt"
DIGEST = "sha256:" + "a" * 64
PLANTED = "sk-or-v1-PLANTEDSECRET0123456789"
PLANTED_2 = "ghp_PLANTEDENVSECRET0123456789"


def good() -> dict:
    return sandbox.terminal_block(POLICY)


def problems_of(block: dict, policy: SandboxPolicy = POLICY) -> list[str]:
    return sandbox.check_terminal_block(block, policy, home=HOME)


def result(code=0, out="", err=""):
    return subprocess.CompletedProcess([], code, out, err)


class FakeDocker:
    """A runner that never starts anything: it records every argv and answers by docker sub-command."""

    def __init__(self, info=None, inspect=None, run=None):
        self.calls = []
        self._info = info or result(0, "27.0.1\n")
        self._inspect = inspect or result(0, "sha256:abc\n")
        self._run = run or (lambda argv: result(0, ""))

    def __call__(self, argv, timeout):
        assert isinstance(timeout, (int, float)) and timeout > 0, "every probe must carry a timeout"
        self.calls.append(list(argv))
        sub = argv[1]
        if sub == "info":
            return self._info
        if sub == "image":
            return self._inspect
        if sub == "run":
            return self._run(argv)
        raise AssertionError(f"unexpected docker command: {argv}")

    @property
    def runs(self):
        return [c for c in self.calls if c[1] == "run"]


def sandbox_run(env_out="PATH=/usr/bin\nHOME=/root\n", cat=(0, "", "")):
    """The `docker run` handler of a healthy sandbox: env shows nothing sensitive, and the planted .env is there
    but empty, because a mask is an empty file laid over it."""
    def handler(argv):
        command = argv[-1]
        if command == "env":
            return result(0, env_out)
        if command.startswith("cat "):
            return result(*cat)
        raise AssertionError(f"unexpected sandbox command: {command}")
    return handler


@pytest.fixture
def worktree(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".env").write_text(f"OPENROUTER_API_KEY={PLANTED}\n", encoding="utf-8")
    (wt / "app.py").write_text("print('hi')\n", encoding="utf-8")
    return wt


@pytest.fixture
def empty_file(tmp_path):
    path = tmp_path / "empty"
    path.write_bytes(b"")
    return path


# ---------------------------------------------------------------------------------------------------------------
# SandboxPolicy
# ---------------------------------------------------------------------------------------------------------------


def test_policy_defaults():
    policy = SandboxPolicy(image=IMAGE)
    assert (policy.image, policy.cpu, policy.memory_mb, policy.pids_limit) == (IMAGE, 2, 4096, 512)
    assert policy.network is False
    assert policy.forward_env == () and policy.extra_deny == ()


def test_policy_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        POLICY.network = True


def test_policy_turns_lists_into_tuples():
    policy = SandboxPolicy(image=IMAGE, forward_env=["TZ", "LANG"], extra_deny=["*.kdbx"])
    assert policy.forward_env == ("TZ", "LANG")
    assert policy.extra_deny == ("*.kdbx",)


def test_policy_task_scoped_network_exception_is_a_replace():
    opened = dataclasses.replace(POLICY, network=True)
    assert opened.network is True and POLICY.network is False


@pytest.mark.parametrize("kwargs", [
    {"cpu": 0}, {"cpu": -1}, {"cpu": True}, {"cpu": "2"}, {"cpu": float("nan")}, {"cpu": float("inf")},
    {"memory_mb": 0}, {"memory_mb": -5}, {"memory_mb": True}, {"memory_mb": 1.5}, {"memory_mb": "4096"},
    {"pids_limit": 0}, {"pids_limit": -1}, {"pids_limit": False}, {"pids_limit": 2.0},
    {"network": "no"}, {"network": 0},
    {"forward_env": "TZ"}, {"forward_env": {"TZ": 1}}, {"forward_env": [1]},
    {"forward_env": ["OPENAI_API_KEY"]}, {"forward_env": ["GITHUB_TOKEN"]}, {"forward_env": ["not a name"]},
    {"extra_deny": ["a/b"]}, {"extra_deny": ["a\\b"]}, {"extra_deny": [" "]}, {"extra_deny": "x"},
])
def test_policy_refuses_values_that_would_switch_a_limit_off(kwargs):
    with pytest.raises(SandboxConfigError):
        SandboxPolicy(image=IMAGE, **kwargs)


def test_policy_refuses_a_non_string_image():
    with pytest.raises(SandboxConfigError):
        SandboxPolicy(image=None)


def test_policy_accepts_fractional_cpu_and_an_allowed_forward_name():
    policy = SandboxPolicy(image=IMAGE, cpu=1.5, forward_env=("TZ",))
    assert policy.cpu == 1.5 and policy.forward_env == ("TZ",)


APPENDIX_B_BLOCK = {
    "terminal_backend": "docker", "network_default": False, "mount": "worktree_only", "forward_env": [],
    "network_exceptions": "explicit_allowlist",
}


def test_from_config_reads_appendix_b_verbatim_in_a_whole_document():
    policy = SandboxPolicy.from_config({"project": {"name": "x"}, "sandbox": dict(APPENDIX_B_BLOCK)})
    assert policy == SandboxPolicy(image="")


def test_from_config_accepts_the_block_itself():
    assert SandboxPolicy.from_config(dict(APPENDIX_B_BLOCK)) == SandboxPolicy(image="")


@pytest.mark.parametrize("cfg", [{}, {"project": {"name": "x"}}, {"sandbox": None}, {"sandbox": {}}])
def test_from_config_without_a_block_gives_the_safe_defaults(cfg):
    policy = SandboxPolicy.from_config(cfg)
    assert policy == SandboxPolicy(image="")
    assert policy.network is False


def test_from_config_reads_the_optional_extensions():
    policy = SandboxPolicy.from_config({"sandbox": {
        **APPENDIX_B_BLOCK, "image": IMAGE, "cpu": 4, "memory_mb": 8192, "pids_limit": 128,
        "forward_env": ["TZ"], "extra_deny": ["*.kdbx"],
    }})
    assert policy == SandboxPolicy(
        image=IMAGE, cpu=4, memory_mb=8192, pids_limit=128, forward_env=("TZ",), extra_deny=("*.kdbx",),
    )


@pytest.mark.parametrize("cfg,needle", [
    ({"sandbox": {"terminal_backend": "local"}}, "terminal_backend"),
    ({"sandbox": {"network_default": True}}, "network_default"),
    ({"sandbox": {"network_default": "false"}}, "network_default"),
    ({"sandbox": {"mount": "everything"}}, "mount"),
    ({"sandbox": {"network_exceptions": "any"}}, "network_exceptions"),
    ({"sandbox": {"forward_env": "PATH"}}, "forward_env"),
    ({"sandbox": {"forward_env": ["OPENAI_API_KEY"]}}, "forward_env"),
    ({"sandbox": {"forward_env": ["not a name"]}}, "forward_env"),
    ({"sandbox": {"mounts": "worktree_only"}}, "unknown"),
    ({"sandbox": {"mount": "worktree_only", "newkey": 1}}, "newkey"),
    ({"sandbox": "docker"}, "mapping"),
    ({"sandbox": ["docker"]}, "mapping"),
    ({"sandbox": {"cpu": 0}}, "cpu"),
    ({"sandbox": {"cpu": "2"}}, "cpu"),
    ({"sandbox": {"memory_mb": 0}}, "memory_mb"),
    ({"sandbox": {"pids_limit": -1}}, "pids_limit"),
    ({"sandbox": {"extra_deny": ["a/b"]}}, "extra_deny"),
    ({"sandbox": {"image": 5}}, "image"),
    ({"terminal_backend": "local"}, "terminal_backend"),
    ("not a dict", "mapping"),
    (None, "mapping"),
])
def test_from_config_refuses_unknown_or_malformed_values(cfg, needle):
    with pytest.raises(SandboxConfigError, match=needle):
        SandboxPolicy.from_config(cfg)


def test_network_default_true_names_the_requirement():
    with pytest.raises(SandboxConfigError, match="ASES-SEC-05"):
        SandboxPolicy.from_config({"sandbox": {"network_default": True}})


# ---------------------------------------------------------------------------------------------------------------
# looks_like_credential
# ---------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", [
    "OPENROUTER_API_KEY", "openai_api_key", "GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "DB_PASSWORD", "DB_PASSWD",
    "GOOGLE_APPLICATION_CREDENTIALS", "MONKEY",
])
def test_credential_shaped_names(name):
    assert sandbox.looks_like_credential(name) is True


@pytest.mark.parametrize("name", ["PATH", "HOME", "CI", "PYTHONUNBUFFERED", "TZ", "LANG", "DEBUG"])
def test_ordinary_names_are_not_credential_shaped(name):
    assert sandbox.looks_like_credential(name) is False


# ---------------------------------------------------------------------------------------------------------------
# terminal_block
# ---------------------------------------------------------------------------------------------------------------


def test_terminal_block_is_table_33_without_cwd_and_with_the_no_reuse_key():
    """Two deliberate differences from the blueprint's table 33, both proven from the Hermes source: cwd is left
    out (it would stop the worktree being mounted) and docker_persist_across_processes is false (a reused
    container keeps the previous card's mount)."""
    assert good() == {
        "backend": "docker", "docker_image": IMAGE,
        "docker_mount_cwd_to_workspace": True, "docker_run_as_host_user": True, "docker_forward_env": [],
        "docker_network": False, "container_cpu": 2, "container_memory": 4096,
        "docker_persist_across_processes": False,
    }
    assert "cwd" not in good()


def test_terminal_block_cpu_is_an_int_when_whole_and_a_float_otherwise():
    assert type(sandbox.terminal_block(SandboxPolicy(image=IMAGE, cpu=2.0))["container_cpu"]) is int
    assert sandbox.terminal_block(SandboxPolicy(image=IMAGE, cpu=1.5))["container_cpu"] == 1.5


def test_terminal_block_follows_the_policy():
    block = sandbox.terminal_block(SandboxPolicy(
        image=IMAGE, memory_mb=8192, network=True, forward_env=("TZ",),
    ))
    assert block["container_memory"] == 8192
    assert block["docker_network"] is True
    assert block["docker_forward_env"] == ["TZ"]


def test_terminal_block_forward_env_is_a_fresh_list():
    block = sandbox.terminal_block(SandboxPolicy(image=IMAGE, forward_env=("TZ",)))
    block["docker_forward_env"].append("LEAK")
    assert sandbox.terminal_block(SandboxPolicy(image=IMAGE, forward_env=("TZ",)))["docker_forward_env"] == ["TZ"]


def test_terminal_block_survives_a_yaml_round_trip():
    assert yaml.safe_load(yaml.safe_dump({"terminal": good()}))["terminal"] == good()


def test_terminal_block_passes_its_own_check_and_a_different_policy_does_not():
    assert problems_of(good()) == []
    assert problems_of(sandbox.terminal_block(SandboxPolicy(image=IMAGE, memory_mb=8192))) != []


def _hermes_source():
    candidates = []
    if os.environ.get("LOCALAPPDATA"):
        candidates.append(pathlib.Path(os.environ["LOCALAPPDATA"]) / "hermes" / "hermes-agent")
    candidates.append(pathlib.Path.home() / ".hermes" / "hermes-agent")
    swarm = pathlib.Path(__file__).resolve().parents[2] / "config" / "swarm.yaml"
    if swarm.exists():
        home = (yaml.safe_load(swarm.read_text(encoding="utf-8")) or {}).get("hermes", {}).get("native_home")
        if home:
            candidates.append(pathlib.Path(home) / "hermes-agent")
    for candidate in candidates:
        if (candidate / "cli-config.yaml.example").is_file() and (candidate / "hermes_cli" / "config.py").is_file():
            return candidate
    return None


def _extract(source, relative, names, namespace):
    """Execute only the named top-level functions and assignments of a Hermes source file, in memory. Nothing is
    imported from Hermes and nothing is written; a name that no longer exists raises LookupError."""
    text = (source / relative).read_text(encoding="utf-8", errors="replace")
    keep, found = [], set()
    for node in ast.parse(text).body:
        label = node.name if isinstance(node, ast.FunctionDef) else (
            node.targets[0].id if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) else None)
        if label in names:
            keep.append(node)
            found.add(label)
    if found != set(names):
        raise LookupError(f"{relative} no longer defines {sorted(set(names) - found)}")
    exec(compile(ast.Module(body=keep, type_ignores=[]), relative, "exec"), namespace)


def _hermes_worker_mount(source, worktree, terminal_section, monkeypatch):
    """One kanban worker, using Hermes's own code: the dispatcher pins TERMINAL_CWD to the worktree, the worker's
    CLI mirrors the profile's terminal: section into the environment (cli.py), and the terminal tool derives the
    host directory it will bind-mount at /workspace (tools/terminal_tool.py). Returns (TERMINAL_CWD, container
    cwd, mounted host directory)."""
    saved = dict(os.environ)
    try:
        for name in [n for n in os.environ if n.startswith("TERMINAL_")]:
            del os.environ[name]
        os.environ["TERMINAL_CWD"] = str(worktree)
        cli = {"os": os, "json": json}
        _extract(source, "cli.py", {"_mirror_config_to_env", "_TERMINAL_ENV_MAPPINGS", "_CWD_PLACEHOLDERS",
                                    "_AUXILIARY_TASK_ENV"}, cli)
        tool = {"os": os, "re": re, "Any": object, "_tenv": lambda name, default="": os.environ.get(name, default)}
        _extract(source, "tools/terminal_tool_config.py", {
            "_HOST_CWD_PREFIXES", "_WINDOWS_DRIVE_RE", "_is_host_cwd", "_CONTAINER_BACKENDS", "_BUILTIN_BACKENDS",
            "_plugin_registry_lookup", "_plugin_env_flag", "_is_container_backend", "_is_unusable_container_cwd",
            "_safe_getcwd",
        }, tool)
        tool.update(logger=logging.getLogger("hermes-stub"), _VERCEL_SANDBOX_DEFAULT_CWD="/root")
        _extract(source, "tools/terminal_tool.py", {"_resolve_config_cwd", "_DEFAULT_CWD_BY_BACKEND"}, tool)
        stub = types.ModuleType("hermes_cli.config")
        stub._is_ssh_remote_tilde_cwd = lambda backend, cwd: False
        monkeypatch.setitem(sys.modules, "hermes_cli.config", stub)
        cli["_mirror_config_to_env"]({"terminal": dict(terminal_section)}, True)
        mounted = os.environ.get("TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE", "false").lower() in ("true", "1", "yes")
        cwd, host = tool["_resolve_config_cwd"]("docker", mounted)
        return os.environ.get("TERMINAL_CWD"), cwd, host
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_table_33s_cwd_would_defeat_the_worktree_mount_on_the_installed_hermes(tmp_path, monkeypatch):
    """The reason terminal_block has no cwd. Runs Hermes 0.21.x's real functions in memory (see _extract): with the
    blueprint's cwd: /workspace the mount source is not the worktree; without cwd, or with a placeholder, it is."""
    source = _hermes_source()
    if source is None or not (source / "cli.py").is_file() or not (source / "tools" / "terminal_tool.py").is_file():
        pytest.skip("Hermes 0.21.x source is not installed here")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    literal = {**good(), "cwd": "/workspace"}
    try:
        _, _, mounted_literal = _hermes_worker_mount(source, worktree, literal, monkeypatch)
        no_cwd = _hermes_worker_mount(source, worktree, good(), monkeypatch)
        placeholders = [
            _hermes_worker_mount(source, worktree, {**good(), "cwd": p}, monkeypatch) for p in (".", "auto")
        ]
    except (LookupError, SyntaxError, NameError, KeyError, AttributeError) as exc:
        pytest.skip(f"Hermes source layout changed ({type(exc).__name__}: {exc}); re-verify the cwd rule by hand")
    same = os.path.normcase
    assert mounted_literal is None or same(mounted_literal) != same(str(worktree)), "the literal table 33 mounts it"
    assert sandbox.check_terminal_block(literal, POLICY, home=HOME), "the checker must refuse the literal block"
    for _, cwd, host in (no_cwd, *placeholders):
        assert cwd == "/workspace" and same(host) == same(str(worktree))
    assert same(no_cwd[0]) == same(str(worktree)), "the dispatcher's pin must survive the profile config"


def test_every_key_terminal_block_emits_exists_in_the_installed_hermes():
    """Read-only. The blueprint says to confirm the keys against the example config, but docker_network and
    docker_persist_across_processes are real and not in that file, so a key also counts when Hermes bridges it
    from config.yaml (TERMINAL_CONFIG_ENV_MAP), which is what makes a terminal: key take effect."""
    source = _hermes_source()
    if source is None:
        pytest.skip("Hermes 0.21.x source is not installed here")
    example = (source / "cli-config.yaml.example").read_text(encoding="utf-8", errors="replace")
    config_py = (source / "hermes_cli" / "config.py").read_text(encoding="utf-8", errors="replace")
    start = config_py.index("TERMINAL_CONFIG_ENV_MAP = {")
    bridge = config_py[start:config_py.index("def _terminal_env_value", start)]
    for key in good():
        assert f'"{key}"' in bridge, f"{key} is not bridged from config.yaml by this Hermes"
        assert re.search(rf"\b{key}\b", example) or key in {"docker_network", "docker_persist_across_processes"}, (
            f"{key} is in neither the example config nor the known omissions"
        )


# ---------------------------------------------------------------------------------------------------------------
# check_terminal_block: one deviation at a time, in both directions
# ---------------------------------------------------------------------------------------------------------------


def test_compliant_block_has_no_problems():
    assert problems_of(good()) == []


@pytest.mark.parametrize("terminal", [None, [], "docker", 5])
def test_a_terminal_that_is_not_a_mapping_is_one_problem(terminal):
    assert len(problems_of(terminal)) == 1


@pytest.mark.parametrize("mutate,needle", [
    (lambda b: b.update(backend="local"), "backend is 'local'"),
    (lambda b: b.update(backend="Docker"), "backend"),
    (lambda b: b.pop("backend"), "backend is missing"),
    (lambda b: b.update(docker_mount_cwd_to_workspace=False), "docker_mount_cwd_to_workspace"),
    (lambda b: b.update(docker_mount_cwd_to_workspace="true"), "docker_mount_cwd_to_workspace"),
    (lambda b: b.pop("docker_mount_cwd_to_workspace"), "docker_mount_cwd_to_workspace"),
    (lambda b: b.update(docker_run_as_host_user=False), "docker_run_as_host_user"),
    (lambda b: b.pop("docker_run_as_host_user"), "docker_run_as_host_user"),
    (lambda b: b.update(docker_persist_across_processes=True), "docker_persist_across_processes"),
    (lambda b: b.pop("docker_persist_across_processes"), "docker_persist_across_processes"),
])
def test_each_structural_key_is_checked(mutate, needle):
    block = good()
    mutate(block)
    found = problems_of(block)
    assert len(found) == 1, found
    assert needle in found[0]


def test_a_non_docker_backend_reports_only_the_backend():
    block = good()
    block.update(backend="local", cwd="/root", docker_network=True)
    assert len(problems_of(block)) == 1


@pytest.mark.parametrize("cwd", ["/workspace", "/workspace/", "/root", "C:\\work\\wt", "src", "", None, 5, "Auto"])
def test_an_explicit_cwd_is_refused_because_it_defeats_the_worktree_mount(cwd):
    """Table 33 says cwd: /workspace. On Hermes 0.21.3 the worker's CLI exports terminal.cwd over the worktree
    the dispatcher pinned as TERMINAL_CWD, and the mount source is then a host directory named workspace."""
    block = good()
    block["cwd"] = cwd
    found = problems_of(block)
    assert len(found) == 1 and found[0].startswith("cwd is "), found
    assert "worktree would not be mounted" in found[0] and "leave cwd out" in found[0]


@pytest.mark.parametrize("cwd", [".", "auto", "cwd"])
def test_a_cwd_placeholder_is_ignored_by_hermes_for_docker_so_it_is_accepted(cwd):
    block = good()
    block["cwd"] = cwd
    assert problems_of(block) == []


def test_a_block_without_cwd_is_fine():
    block = good()
    assert "cwd" not in block
    assert problems_of(block) == []


@pytest.mark.parametrize("value,needle", [
    (["OPENAI_API_KEY"], "credential-shaped name OPENAI_API_KEY"),
    (["GITHUB_TOKEN", "TZ"], "credential-shaped name GITHUB_TOKEN"),
    (["TZ"], "forwards TZ, which the policy does not allow"),
    ([5], "not a string"),
    ("TZ", "not a list"),
    (None, "not a list"),
])
def test_docker_forward_env_is_checked(value, needle):
    block = good()
    block["docker_forward_env"] = value
    found = problems_of(block)
    assert any("docker_forward_env" in p and needle in p for p in found), found


def test_docker_forward_env_may_carry_a_name_the_policy_allows_but_never_a_credential():
    policy = SandboxPolicy(image=IMAGE, forward_env=("TZ",))
    block = sandbox.terminal_block(policy)
    assert problems_of(block, policy) == []
    block["docker_forward_env"] = ["TZ", "OPENAI_API_KEY"]
    found = problems_of(block, policy)
    assert len(found) == 1 and "OPENAI_API_KEY" in found[0]


def test_docker_forward_env_absent_is_fine():
    block = good()
    del block["docker_forward_env"]
    assert problems_of(block) == []


def test_docker_env_credential_names_and_secret_shaped_values_are_refused():
    block = good()
    block["docker_env"] = {"DEBUG": "1", "OPENROUTER_API_KEY": "x"}
    found = problems_of(block)
    assert len(found) == 1 and "docker_env sets credential-shaped name OPENROUTER_API_KEY" in found[0]
    block["docker_env"] = {"HARMLESS": PLANTED}
    found = problems_of(block)
    assert len(found) == 1 and "docker_env value for HARMLESS looks like a secret" in found[0]
    assert PLANTED not in found[0]


def test_docker_env_with_plain_values_is_fine_and_a_non_mapping_is_not():
    block = good()
    block["docker_env"] = {"DEBUG": "1", "PYTHONUNBUFFERED": "1"}
    assert problems_of(block) == []
    for bad in (None, ["A=1"], "A=1"):
        block["docker_env"] = bad
        assert problems_of(block) == ["docker_env is not a mapping"]


def test_env_passthrough_is_checked_like_docker_forward_env():
    block = good()
    block["env_passthrough"] = ["GITHUB_TOKEN"]
    assert any("env_passthrough forwards credential-shaped name GITHUB_TOKEN" in p for p in problems_of(block))
    block["env_passthrough"] = ["TZ"]
    assert any("env_passthrough forwards TZ" in p for p in problems_of(block))
    block["env_passthrough"] = []
    assert problems_of(block) == []
    block["env_passthrough"] = "TZ"
    assert problems_of(block) == ["env_passthrough is not a list"]


def test_credential_files_would_mount_host_files():
    block = good()
    block["credential_files"] = ["mcp-tokens/x.json"]
    found = problems_of(block)
    assert len(found) == 1 and "credential_files" in found[0]
    for fine in ([], None):
        block["credential_files"] = fine
        assert problems_of(block) == []
    block["credential_files"] = "x.json"
    assert problems_of(block) == ["credential_files is not a list"]


def test_network_true_needs_the_policy_to_allow_it():
    block = good()
    block["docker_network"] = True
    found = problems_of(block)
    assert len(found) == 1 and "docker_network is true" in found[0]
    opened = dataclasses.replace(POLICY, network=True)
    assert problems_of(block, opened) == []
    assert problems_of(sandbox.terminal_block(opened), opened) == []


def test_docker_network_missing_means_hermes_default_true():
    block = good()
    del block["docker_network"]
    found = problems_of(block)
    assert len(found) == 1 and "docker_network is not set" in found[0] and "defaults to true" in found[0]
    assert problems_of(block, dataclasses.replace(POLICY, network=True)) == []


@pytest.mark.parametrize("value", ["false", 0, "no", []])
def test_docker_network_must_be_a_real_boolean(value):
    block = good()
    block["docker_network"] = value
    assert problems_of(block) == ["docker_network is not true or false"]


@pytest.mark.parametrize("key,unit", [("container_cpu", "CPU"), ("container_memory", "memory")])
@pytest.mark.parametrize("value", [0, -1, "2", True, None, [], float("nan")])
def test_a_missing_or_nonpositive_limit_is_a_problem(key, unit, value):
    block = good()
    if value is None:
        del block[key]
    else:
        block[key] = value
    found = problems_of(block)
    assert len(found) == 1 and found[0].startswith(key) and unit in found[0], found


def test_a_limit_above_the_policy_is_a_problem_and_at_the_policy_is_not():
    block = good()
    block["container_cpu"] = 2
    block["container_memory"] = 4096
    assert problems_of(block) == []
    block["container_cpu"] = 3
    assert problems_of(block) == ["container_cpu 3 exceeds the policy limit 2"]
    block["container_cpu"] = 1
    block["container_memory"] = 4097
    assert problems_of(block) == ["container_memory 4097 exceeds the policy limit 4096"]
    block["container_memory"] = 1024
    assert problems_of(block) == []


@pytest.mark.parametrize("image,fragment", [
    ("python", "no tag"),
    ("python:latest", "'latest'"),
    ("python:LATEST", "'latest'"),
    ("localhost:5000/toolchain", "no tag"),
    ("registry.example/team/toolchain", "no tag"),
    ("python:", "no tag"),
    ("python@sha256:abc123", "full sha256"),
    ("python@sha256:" + "g" * 64, "full sha256"),
    ("python@md5:" + "a" * 32, "full sha256"),
])
def test_unpinned_images_are_refused(image, fragment):
    block = good()
    block["docker_image"] = image
    found = problems_of(block)
    assert len(found) == 1 and "docker_image" in found[0] and fragment in found[0], found


@pytest.mark.parametrize("image", [
    "python:3.11-slim", "localhost:5000/toolchain:1.0", "registry.example/team/toolchain:v2",
    "python@" + DIGEST, "python:latest@" + DIGEST, "registry.example:5000/x@" + DIGEST,
])
def test_pinned_images_are_accepted(image):
    block = good()
    block["docker_image"] = image
    assert problems_of(block) == []


@pytest.mark.parametrize("image", [None, "", "   ", 5, [], ["python:3"]])
def test_a_missing_image_is_a_problem(image):
    block = good()
    block["docker_image"] = image
    assert problems_of(block) == ["docker_image is missing"]


@pytest.mark.parametrize("image", ["--privileged", "py thon:1", "-v:1", "image:1;rm", "a\nb:1"])
def test_an_image_that_is_not_a_reference_is_refused(image):
    block = good()
    block["docker_image"] = image
    found = problems_of(block)
    assert len(found) == 1 and "not a valid image reference" in found[0]


@pytest.mark.parametrize("volume,needle", [
    ("C:\\Users\\tester\\.ssh:/root/.ssh:ro", "is or lies inside ~/.ssh"),
    ("C:/Users/tester/.aws:/root/.aws", "~/.aws"),
    ("/c/Users/tester/.ssh:/x", "~/.ssh"),
    ("~/.ssh:/root/.ssh", "~/.ssh"),
    ("~:/home", "is the home directory"),
    ("C:\\Users\\tester:/home", "is the home directory"),
    ("C:\\Users:/x", "parent of the home directory"),
    ("C:\\:/host", "is a drive root"),
    ("/:/host", "is a drive root"),
    ("C:\\Users\\tester\\AppData:/x", "parent of"),
    ("C:\\Users\\tester\\keys\\id_rsa:/x", "name that marks it sensitive"),
    ("C:\\projects\\other:/workspace", "targets /workspace"),
    ("C:\\projects\\other:/workspace/sub:ro", "targets /workspace"),
    ("./local:/x:ro", "is not an absolute path"),
    ("..\\up:/x", "is not an absolute path"),
    ("\\\\server\\share:/x", "is a drive root"),
])
def test_dangerous_docker_volumes_are_refused(volume, needle):
    block = good()
    block["docker_volumes"] = [volume]
    found = problems_of(block)
    assert any("docker_volumes entry" in p and needle in p for p in found), found


def test_docker_volumes_posix_home_is_refused():
    block = good()
    block["docker_volumes"] = ["/home/tester/.ssh:/root/.ssh", "/home/tester:/h"]
    found = sandbox.check_terminal_block(block, POLICY, home=POSIX_HOME)
    assert len(found) == 2 and "~/.ssh" in found[0] and "home directory" in found[1]


@pytest.mark.parametrize("volume", [
    "C:\\cache\\pip:/root/.cache/pip:ro", "/srv/cache:/cache", "named_volume:/data", "nocolon", "", "  ",
    "C:\\cache\\npm:/root/.npm",
])
def test_harmless_docker_volumes_are_accepted(volume):
    block = good()
    block["docker_volumes"] = [volume]
    assert problems_of(block) == []


@pytest.mark.parametrize("volume", [":/workspace", ":/workspace/sub:ro", ":/x", ":"])
def test_a_volume_with_no_host_part_is_judged_like_hermes_does(volume):
    """Hermes only skips an entry with no colon at all; ':/workspace' still counts as an explicit /workspace mount."""
    block = good()
    block["docker_volumes"] = [volume]
    found = problems_of(block)
    if "/workspace" in volume:
        assert len(found) == 1 and "targets /workspace" in found[0], found
    else:
        assert found == []


def test_split_volume_handles_every_shape():
    assert sandbox._split_volume("nocolon") == ("", "", "")
    assert sandbox._split_volume("") == ("", "", "")
    assert sandbox._split_volume("/a:/b") == ("/a", "/b", "")
    assert sandbox._split_volume("/a:/b:ro,z") == ("/a", "/b", "ro,z")
    assert sandbox._split_volume("C:\\a b\\c:/d:ro") == ("C:\\a b\\c", "/d", "ro")
    assert sandbox._split_volume("C:/a:/d") == ("C:/a", "/d", "")
    assert sandbox._split_volume("\\\\?\\C:\\a:/d") == ("\\\\?\\C:\\a", "/d", "")
    assert sandbox._split_volume("named:/data") == ("named", "/data", "")
    assert sandbox._split_volume(":/workspace") == ("", "/workspace", "")
    assert sandbox._split_volume("  /a:/b  ") == ("/a", "/b", "")


def test_a_home_directory_that_cannot_be_found_is_a_config_error_not_a_crash(monkeypatch):
    def no_home():
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(pathlib.Path, "home", staticmethod(no_home))
    with pytest.raises(SandboxConfigError, match="home directory"):
        sandbox.check_terminal_block(good(), POLICY)
    with pytest.raises(SandboxConfigError, match="home directory"):
        sandbox.docker_run_argv(POLICY, WIN_WT, "x")
    # with home given, nothing needs to look it up
    assert sandbox.check_terminal_block(good(), POLICY, home=HOME) == []
    assert sandbox.docker_run_argv(POLICY, WIN_WT, "x", home=HOME)


def test_a_runner_that_returns_bytes_is_decoded():
    def bytes_runner(argv, timeout):
        return types.SimpleNamespace(returncode=1, stdout=b"", stderr="caf\u00e9 daemon down".encode("utf-8") + b"\xff")

    ok, why = sandbox.docker_available(bytes_runner)
    assert ok is False and "daemon down" in why and why.isascii()
    result_ = types.SimpleNamespace(returncode=0, stdout=b"27.1\n", stderr=b"")
    ok, why = sandbox.docker_available(lambda argv, timeout: result_)
    assert ok is True and "27.1" in why


def test_a_root_owned_home_does_not_make_its_own_worktrees_a_system_path_problem():
    """A root-run controller on WSL2 keeps worktrees under /root; the home rules still guard the home itself."""
    assert sandbox.mount_problems(["/root/ws/wt"], "/root/ws/wt", "/root") == []
    assert "home directory" in sandbox.mount_problems(["/root"], "/root/ws/wt", "/root")[0]
    assert "lies inside /etc" in sandbox.mount_problems(["/etc/x"], "/root/ws/wt", "/root")[0]
    assert "lies inside /root" in sandbox.mount_problems(["/root/ws/wt"], "/root/ws/wt", "/home/tester")[0]


def test_malformed_docker_volumes_are_reported():
    block = good()
    block["docker_volumes"] = [5]
    assert problems_of(block) == ["docker_volumes has an entry that is not a string"]
    block["docker_volumes"] = "C:\\x:/y"
    assert problems_of(block) == ["docker_volumes is not a list"]
    block["docker_volumes"] = None
    assert problems_of(block) == ["docker_volumes is not a list"]
    block["docker_volumes"] = []
    assert problems_of(block) == []


@pytest.mark.parametrize("args", [
    [], ["--pids-limit", "64"], ["--pids-limit=512"], ["--shm-size", "1g"], ["--shm-size=512m"],
    ["--ulimit", "nofile=65536:65536"], ["--init"], ["--hostname", "worker-1"], ["--label", "ases=1"],
    ["--user", "1000:1000"], ["-u", "node"], ["--user=1000"],
])
def test_harmless_docker_extra_args_are_accepted(args):
    block = good()
    block["docker_extra_args"] = args
    assert problems_of(block) == []


@pytest.mark.parametrize("args,needle", [
    (["--privileged"], "--privileged"),
    (["--cap-add", "SYS_ADMIN"], "--cap-add"),
    (["-v", "C:\\Users\\tester:/host"], "-v"),
    (["--volume=C:\\x:/y"], "--volume"),
    (["--mount", "type=bind,source=C:\\x,target=/y"], "--mount"),
    (["--network=host"], "--network"),
    (["--network", "host"], "--network"),
    (["--net", "bridge"], "--net"),
    (["-e", "OPENAI_API_KEY=abc"], "-e"),
    (["--env-file", "C:\\x\\.env"], "--env-file"),
    (["--memory", "64g"], "--memory"),
    (["--cpus", "64"], "--cpus"),
    (["--pid=host"], "--pid"),
    (["--device", "/dev/sda"], "--device"),
    (["--security-opt", "seccomp=unconfined"], "--security-opt"),
    (["--gpus=all"], "--gpus"),
    (["--pids-limit", "0"], "value '0'"),
    (["--pids-limit", "-1"], "value '-1'"),
    (["--pids-limit", "513"], "value '513'"),
    (["--pids-limit", "lots"], "value 'lots'"),
    (["--pids-limit"], "without a value"),
    (["--user", "root"], "value 'root'"),
    (["--user=0:0"], "value '0:0'"),
    (["-u", "0"], "value '0'"),
    (["--shm-size", "huge"], "value 'huge'"),
    (["--ulimit", "nproc=-1"], "value 'nproc=-1'"),
    (["--hostname", "a b"], "value 'a b'"),
    (["--init=false"], "value it does not take"),
    (["evil/image:1", "sh"], "bare word 'evil/image:1'"),
    (["--init", "evil-image"], "bare word 'evil-image'"),
    ([5], "not a string"),
])
def test_dangerous_docker_extra_args_are_refused(args, needle):
    block = good()
    block["docker_extra_args"] = args
    found = problems_of(block)
    assert found and all("docker_extra_args" in p for p in found), found
    assert any(needle in p for p in found), found


def test_an_unknown_flag_takes_its_value_with_it():
    block = good()
    block["docker_extra_args"] = ["-v", "C:\\x:/y", "--init"]
    found = problems_of(block)
    assert len(found) == 1 and "-v" in found[0], found


def test_docker_extra_args_that_is_not_a_list_is_refused():
    for bad in (None, "--init", {"a": 1}):
        block = good()
        block["docker_extra_args"] = bad
        assert problems_of(block) == ["docker_extra_args is not a list"]


def test_the_pids_limit_ceiling_is_the_policy():
    tight = SandboxPolicy(image=IMAGE, pids_limit=100)
    block = sandbox.terminal_block(tight)
    block["docker_extra_args"] = ["--pids-limit", "100"]
    assert problems_of(block, tight) == []
    block["docker_extra_args"] = ["--pids-limit", "101"]
    assert len(problems_of(block, tight)) == 1


def test_shared_container_key_and_snap_compat_are_refused():
    block = good()
    block["docker_shared_container_key"] = "team"
    assert problems_of(block) == ["docker_shared_container_key is set (profiles would share one container)"]
    block["docker_shared_container_key"] = ""
    assert problems_of(block) == []
    block["docker_snap_compat"] = True
    assert problems_of(block) == ["docker_snap_compat is true (it drops no-new-privileges)"]
    block["docker_snap_compat"] = False
    assert problems_of(block) == []


def test_every_problem_is_one_ascii_sentence_naming_a_key():
    block = good()
    block.update(docker_image="caf\u00e9:latest", docker_forward_env=["TOK\u00c9N"], docker_extra_args=["--x\u2192y"])
    block["docker_env"] = {"K\u00c9Y": "1"}
    found = problems_of(block)
    assert len(found) >= 3
    assert all(p.isascii() for p in found), found
    assert all(re.match(r"^[a-z_]+ ", p) for p in found), found


def test_check_terminal_block_default_home_is_the_real_one():
    block = good()
    block["docker_volumes"] = [str(pathlib.Path.home()) + ":/h"]
    assert any("home directory" in p for p in sandbox.check_terminal_block(block, POLICY))


# ---------------------------------------------------------------------------------------------------------------
# check_profile_config, load_profile_config
# ---------------------------------------------------------------------------------------------------------------


def test_a_profile_config_without_a_terminal_block_is_a_problem():
    real_shape = {"_config_version": 21, "agent": {}, "display": {}, "model": {"default": "x", "provider": "y"},
                  "platform_toolsets": {}, "plugins": {}, "providers": {}}
    found = sandbox.check_profile_config(real_shape, POLICY, home=HOME)
    assert len(found) == 1 and "missing" in found[0] and "local backend" in found[0]


@pytest.mark.parametrize("config", [{}, None, [], "x", {"terminal": None}])
def test_a_missing_or_null_terminal_is_one_problem(config):
    assert len(sandbox.check_profile_config(config, POLICY, home=HOME)) == 1


def test_a_profile_config_with_the_block_is_checked_like_the_block():
    assert sandbox.check_profile_config({"model": {}, "terminal": good()}, POLICY, home=HOME) == []
    bad = good()
    bad["docker_network"] = True
    assert len(sandbox.check_profile_config({"terminal": bad}, POLICY, home=HOME)) == 1
    assert len(sandbox.check_profile_config({"terminal": "local"}, POLICY, home=HOME)) == 1


def write_profile(root, name, config):
    directory = root / name
    directory.mkdir(parents=True)
    if config is not None:
        (directory / "config.yaml").write_text(
            config if isinstance(config, str) else yaml.safe_dump(config), encoding="utf-8",
        )
    return directory


def test_load_profile_config_reads_yaml(tmp_path):
    directory = write_profile(tmp_path, "coder-1", {"model": {"default": "m"}, "terminal": good()})
    assert sandbox.load_profile_config(directory) == {"model": {"default": "m"}, "terminal": good()}


def test_load_profile_config_missing_or_empty_is_an_empty_dict(tmp_path):
    assert sandbox.load_profile_config(write_profile(tmp_path, "a", None)) == {}
    assert sandbox.load_profile_config(write_profile(tmp_path, "b", "")) == {}
    assert sandbox.load_profile_config(write_profile(tmp_path, "c", "# only a comment\n")) == {}
    assert sandbox.load_profile_config(tmp_path / "does-not-exist") == {}


def test_load_profile_config_accepts_a_byte_order_mark(tmp_path):
    directory = tmp_path / "bom"
    directory.mkdir()
    (directory / "config.yaml").write_bytes(b"\xef\xbb\xbfterminal:\n  backend: docker\n")
    assert sandbox.load_profile_config(directory) == {"terminal": {"backend": "docker"}}


def test_load_profile_config_bad_yaml_raises_without_quoting_the_file(tmp_path):
    directory = write_profile(tmp_path, "bad", "api_key: sk-or-v1-TOPSECRETVALUE12345\nmodel: [unclosed\n")
    with pytest.raises(SandboxConfigError) as excinfo:
        sandbox.load_profile_config(directory)
    assert "not valid YAML" in str(excinfo.value)
    assert "TOPSECRET" not in str(excinfo.value) and "sk-or" not in str(excinfo.value)


def test_load_profile_config_a_scalar_yaml_cannot_build_raises_instead_of_crashing(tmp_path):
    """yaml.safe_load raises ValueError, not YAMLError, for the date 2001-13-45."""
    with pytest.raises(SandboxConfigError, match="not valid YAML"):
        sandbox.load_profile_config(write_profile(tmp_path, "date", "released: 2001-13-45\n"))


def test_load_profile_config_invalid_utf8_raises_instead_of_crashing(tmp_path):
    directory = tmp_path / "binary"
    directory.mkdir()
    (directory / "config.yaml").write_bytes(b"model: \xff\xfe\x00bad\n")
    with pytest.raises(SandboxConfigError, match="cannot read"):
        sandbox.load_profile_config(directory)


def test_load_profile_config_not_a_mapping_raises(tmp_path):
    with pytest.raises(SandboxConfigError, match="not a mapping"):
        sandbox.load_profile_config(write_profile(tmp_path, "list", "- a\n- b\n"))


def test_load_profile_config_unreadable_raises(tmp_path):
    directory = tmp_path / "dir-as-file"
    (directory / "config.yaml").mkdir(parents=True)
    with pytest.raises(SandboxConfigError, match="cannot read"):
        sandbox.load_profile_config(directory)


# ---------------------------------------------------------------------------------------------------------------
# The sensitive-path model
# ---------------------------------------------------------------------------------------------------------------

REQUIRED_SENSITIVE = [
    ".ssh", ".aws", ".azure", ".config/gcloud", ".kube", ".docker/config.json", ".gnupg", ".npmrc", ".pypirc",
    ".netrc", ".git-credentials", "AppData/Local/Google/Chrome/User Data",
    "AppData/Local/Microsoft/Edge/User Data", "AppData/Roaming/Mozilla/Firefox", ".config/google-chrome",
    ".mozilla",
]


@pytest.mark.parametrize("home,base", [
    (HOME, "C:/Users/tester"), (POSIX_HOME, "/home/tester"), ("C:\\Users\\tester", "C:/Users/tester"),
    (pathlib.PureWindowsPath(HOME), "C:/Users/tester"), (pathlib.PurePosixPath(POSIX_HOME), "/home/tester"),
])
def test_sensitive_host_paths_lists_every_required_location_under_home(home, base):
    listed = sandbox.sensitive_host_paths(home)
    shown = [p.as_posix() for p in listed]
    for relative in REQUIRED_SENSITIVE:
        assert f"{base}/{relative}" in shown, relative
    assert all(p.as_posix().startswith(base + "/") for p in listed)
    assert len(shown) == len(set(shown))


def test_sensitive_host_paths_gives_paths_back_for_a_path_home(tmp_path):
    listed = sandbox.sensitive_host_paths(tmp_path)
    assert all(isinstance(p, pathlib.Path) for p in listed)
    assert listed[0] == tmp_path / ".ssh"


def test_sensitive_host_paths_includes_the_hermes_and_claude_homes():
    shown = [p.as_posix() for p in sandbox.sensitive_host_paths(HOME)]
    for relative in (".hermes", "AppData/Local/hermes", ".claude", ".config/gh", "AppData/Roaming/GitHub CLI"):
        assert f"{HOME}/{relative}" in shown


@pytest.mark.parametrize("home", ["relative/home", "", "C:", "~"])
def test_sensitive_host_paths_needs_an_absolute_home(home):
    with pytest.raises(SandboxConfigError):
        sandbox.sensitive_host_paths(home)


def test_sensitive_name_patterns():
    patterns = sandbox.sensitive_name_patterns()
    for required in (".env", ".env.*", "*.pem", "*.key", "id_rsa*", "id_ed25519*", "*.p12", "*.pfx",
                     "credentials.json", "secrets.*"):
        assert required in patterns
    assert isinstance(patterns, tuple)


@pytest.mark.parametrize("path", [
    "C:/Users/tester/.ssh", "C:/Users/tester/.ssh/id_rsa", "C:\\Users\\tester\\.aws\\credentials",
    "C:/Users/tester/.azure/x", "C:/Users/tester/.config/gcloud/creds.db", "C:/Users/tester/.kube/config",
    "C:/Users/tester/.docker/config.json", "C:/Users/tester/.gnupg/private-keys-v1.d/k",
    "C:/Users/tester/.npmrc", "C:/Users/tester/.pypirc", "C:/Users/tester/.netrc",
    "C:/Users/tester/.git-credentials",
    "C:/Users/tester/AppData/Local/Google/Chrome/User Data/Default/Login Data",
    "C:/Users/tester/AppData/Local/Microsoft/Edge/User Data",
    "C:/Users/tester/AppData/Roaming/Mozilla/Firefox/Profiles/x/logins.json",
    "C:/Users/tester/.config/google-chrome/Default", "C:/Users/tester/.mozilla/firefox",
    "C:/Users/tester/.hermes/auth.json", "C:/Users/tester/AppData/Local/hermes/profiles/lead/auth.json",
    # names, anywhere
    "D:/proj/.env", "D:/proj/.env.local", "D:/proj/.env.production", "D:/proj/.envrc", "D:/proj/DEPLOY.PEM",
    "D:/proj/server.key", "D:/proj/id_rsa", "D:/proj/id_rsa.pub", "D:/proj/id_ed25519", "D:/proj/cert.p12",
    "D:/proj/cert.pfx", "D:/proj/credentials.json", "D:/proj/secrets.yaml", "D:/proj/Secrets.JSON",
    "D:/proj/id_ecdsa", "D:/proj/id_dsa.pub", "D:/proj/id_ed25519.pub",
    # normalisation
    "C:/Users/tester/proj/../.ssh/id_rsa", "C:\\Users\\tester\\proj\\..\\.aws", "c:/users/TESTER/.SSH/x",
    "/c/Users/tester/.ssh/x",
])
def test_is_sensitive_path_true(path):
    assert sandbox.is_sensitive_path(path, HOME) is True


@pytest.mark.parametrize("path", [
    "C:/Users/tester/proj/README.md", "C:/Users/tester/.docker/other.json", "C:/Users/tester/.sshd/x",
    "C:/Users/tester/AppData/Local/Google/Chrome/Application/chrome.exe", "C:/Users/tester/AppData/Local/Temp",
    "D:/proj/.envelope", "D:/proj/env", "D:/proj/keyboard.txt", "D:/proj/id_x", "D:/proj/secrets",
    "D:/proj/credentials.txt", "C:/Users/testers/.ssh/x", "C:/Users/tester2/.ssh", "D:/Users/tester/.ssh/x",
    "C:/Users/tester/proj/../src/main.py",
])
def test_is_sensitive_path_false(path):
    assert sandbox.is_sensitive_path(path, HOME) is False


def test_is_sensitive_path_posix_home_is_strict_about_case_for_locations_but_names_fold():
    assert sandbox.is_sensitive_path("/home/tester/.ssh/id_rsa", POSIX_HOME) is True
    assert sandbox.is_sensitive_path("/home/tester/.SSH/x", POSIX_HOME) is True   # deny rules fold
    assert sandbox.is_sensitive_path("/home/other/.ssh/x", POSIX_HOME) is False
    assert sandbox.is_sensitive_path("/srv/app/.env", POSIX_HOME) is True


def test_is_sensitive_path_extra_patterns():
    assert sandbox.is_sensitive_path("D:/x/vault.kdbx", HOME) is False
    assert sandbox.is_sensitive_path("D:/x/vault.kdbx", HOME, extra_patterns=("*.kdbx",)) is True


def test_is_sensitive_path_accepts_path_objects():
    assert sandbox.is_sensitive_path(pathlib.PureWindowsPath("C:/Users/tester/.ssh/id_rsa"), HOME) is True
    assert sandbox.is_sensitive_path(pathlib.PurePosixPath("/srv/.env"), POSIX_HOME) is True


# ---------------------------------------------------------------------------------------------------------------
# mount_problems
# ---------------------------------------------------------------------------------------------------------------


def mp(mounts, worktree=WIN_WT, home=HOME):
    return sandbox.mount_problems(mounts, worktree, home)


@pytest.mark.parametrize("mount", [
    WIN_WT, WIN_WT + "\\src", WIN_WT + "\\a\\b\\c", "C:/work/wt", "C:\\work\\wt\\", "c:\\WORK\\WT",
    "C:\\work\\wt\\src\\..\\docs", "C:\\work\\wt\\.\\src", "/c/work/wt/src", "/mnt/c/work/wt",
    pathlib.PureWindowsPath(WIN_WT),
])
def test_the_worktree_and_anything_inside_it_is_acceptable(mount):
    assert mp([mount]) == []


@pytest.mark.parametrize("mount,fragment", [
    ("C:\\Users\\tester", "is the home directory"),
    ("c:/users/TESTER", "is the home directory"),
    ("C:\\", "is a drive root"),
    ("C:/", "is a drive root"),
    ("D:\\", "is a drive root"),
    ("\\\\server\\share", "is a drive root"),
    ("C:\\work", "is a parent of the worktree"),
    ("C:\\work\\wt\\..", "is a parent of the worktree"),
    ("C:\\Users", "is a parent of the home directory"),
    ("C:\\Users\\tester\\.ssh", "is or lies inside ~/.ssh"),
    ("C:\\Users\\tester\\.ssh\\id_rsa", "~/.ssh"),
    ("C:\\Users\\tester\\AppData\\Local\\Google\\Chrome\\User Data\\Default", "Chrome"),
    ("C:\\Users\\tester\\AppData", "is a parent of"),
    ("C:\\Users\\tester\\.config", "is a parent of ~/.config/gcloud"),
    ("C:\\Users\\tester\\.docker", "is a parent of ~/.docker/config.json"),
    ("C:\\work\\wt\\.env", "name that marks it sensitive (.env)"),
    ("C:\\work\\wt\\keys\\server.pem", "name that marks it sensitive (*.pem)"),
    ("C:\\other\\place", "is outside the worktree"),
    ("C:\\work\\wt2", "is outside the worktree"),
    ("C:\\work\\wt\\..\\other", "is outside the worktree"),
    ("D:\\work\\wt", "is outside the worktree"),
    ("relative\\path", "is not an absolute path"),
    ("C:relative", "is not an absolute path"),
    ("..\\wt", "is not an absolute path"),
    ("", "is not an absolute path"),
])
def test_everything_else_is_a_problem_with_the_right_reason(mount, fragment):
    found = mp([mount])
    assert len(found) == 1, found
    assert fragment in found[0], found


def test_a_bare_string_mount_is_one_mount_not_a_list_of_characters():
    assert mp("C:\\") == ["mount C:\\ is a drive root"]
    assert mp(WIN_WT) == []


def test_every_bad_mount_in_the_list_is_reported_once_and_good_ones_are_not():
    found = mp([WIN_WT, "C:\\Users\\tester", WIN_WT + "\\src", "C:\\"])
    assert len(found) == 2
    assert "home directory" in found[0] and "drive root" in found[1]
    assert mp([]) == []
    assert mp(()) == []


def test_a_relative_worktree_makes_every_mount_a_problem():
    assert mp([WIN_WT], worktree="wt") == ["worktree wt is not an absolute path"]


def test_posix_paths_are_case_sensitive_for_the_allow_rule():
    ok = sandbox.mount_problems(["/work/wt/src", "/work/wt"], "/work/wt", POSIX_HOME)
    assert ok == []
    bad = sandbox.mount_problems(["/work/WT/src"], "/work/wt", POSIX_HOME)
    assert len(bad) == 1 and "outside the worktree" in bad[0]
    assert "parent of the worktree" in sandbox.mount_problems(["/work"], "/work/wt", POSIX_HOME)[0]
    assert "drive root" in sandbox.mount_problems(["/"], "/work/wt", POSIX_HOME)[0]
    assert "home directory" in sandbox.mount_problems(["/home/tester"], "/work/wt", POSIX_HOME)[0]
    assert "parent of the home directory" in sandbox.mount_problems(["/home"], "/work/wt", POSIX_HOME)[0]
    assert "~/.ssh" in sandbox.mount_problems(["/home/tester/.ssh"], "/work/wt", POSIX_HOME)[0]
    assert "outside" in sandbox.mount_problems(["/work/wt/../x"], "/work/wt", POSIX_HOME)[0]
    assert sandbox.mount_problems(["/work/wt/a/../b"], "/work/wt", POSIX_HOME) == []


def test_a_different_drive_is_never_inside_the_worktree():
    assert len(mp(["D:\\work\\wt\\src"])) == 1


def test_mount_problems_are_ascii_even_for_a_non_ascii_path():
    found = mp(["C:\\Users\\t\u00e9st\\x"])
    assert found and all(p.isascii() for p in found)


def test_mount_problems_needs_an_absolute_home():
    with pytest.raises(SandboxConfigError):
        mp([WIN_WT], home="relative")


# ---------------------------------------------------------------------------------------------------------------
# sensitive_files_in and mask_args
# ---------------------------------------------------------------------------------------------------------------


def make_tree(root):
    (root / "src" / "deep").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "sub" / ".git").mkdir(parents=True)
    files = [
        ".env", ".env.local", "src/.env.production", "src/deep/id_rsa", "src/deep/server.PEM", "secrets.yaml",
        "credentials.json", "app.py", "src/main.py", "README.md", ".git/.env", ".git/id_rsa", "sub/.git/secrets.json",
        "sub/notes.p12",
    ]
    for name in files:
        (root / name).write_text("x", encoding="utf-8")
    return root


def relative_names(found, root):
    return sorted(p.relative_to(root).as_posix() for p in found)


def test_sensitive_files_in_finds_nested_files_and_skips_every_git_directory(tmp_path):
    root = make_tree(tmp_path / "wt")
    assert relative_names(sandbox.sensitive_files_in(root), root) == [
        ".env", ".env.local", "credentials.json", "secrets.yaml", "src/.env.production", "src/deep/id_rsa",
        "src/deep/server.PEM", "sub/notes.p12",
    ]


def test_sensitive_files_in_returns_sorted_absolute_paths(tmp_path):
    root = make_tree(tmp_path / "wt")
    found = sandbox.sensitive_files_in(root)
    assert found == sorted(found)
    assert all(p.is_absolute() and p.is_file() for p in found)


def test_sensitive_files_in_ignores_a_directory_named_env(tmp_path):
    root = tmp_path / "wt"
    (root / ".env" / "lib").mkdir(parents=True)
    (root / ".env" / "lib" / "site.py").write_text("x", encoding="utf-8")
    assert sandbox.sensitive_files_in(root) == []


def test_sensitive_files_in_extra_patterns(tmp_path):
    root = tmp_path / "wt"
    root.mkdir()
    (root / "vault.kdbx").write_text("x", encoding="utf-8")
    assert sandbox.sensitive_files_in(root) == []
    assert relative_names(sandbox.sensitive_files_in(root, extra_patterns=("*.kdbx",)), root) == ["vault.kdbx"]


def test_sensitive_files_in_does_not_follow_symlinks(tmp_path):
    root = tmp_path / "wt"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "id_rsa").write_text("x", encoding="utf-8")
    try:
        os.symlink(outside, root / "linked", target_is_directory=True)
        os.symlink(outside / "id_rsa", root / ".env")
    except (OSError, NotImplementedError):
        pytest.skip("this platform or account cannot create symlinks")
    assert sandbox.sensitive_files_in(root) == []


def test_sensitive_files_in_skips_a_symlink_even_where_symlinks_cannot_be_created(tmp_path, monkeypatch):
    root = tmp_path / "wt"
    root.mkdir()
    (root / ".env").write_text("x", encoding="utf-8")
    (root / "id_rsa").write_text("x", encoding="utf-8")
    real = pathlib.Path.is_symlink
    monkeypatch.setattr(pathlib.Path, "is_symlink", lambda self: self.name == ".env" or real(self))
    assert relative_names(sandbox.sensitive_files_in(root), root) == ["id_rsa"]


def test_sensitive_files_in_walks_without_following_symlinks_and_reports_unreadable_folders(tmp_path, monkeypatch):
    seen = {}

    def fake_walk(top, **kwargs):
        seen.update(kwargs, top=top)
        return iter(())

    monkeypatch.setattr(sandbox.os, "walk", fake_walk)
    assert sandbox.sensitive_files_in(tmp_path) == []
    assert seen["followlinks"] is False and seen["top"] == str(tmp_path)
    with pytest.raises(SandboxConfigError, match="cannot scan"):
        seen["onerror"](PermissionError(13, "Access is denied", "C:\\wt\\private"))


def test_sensitive_files_in_a_missing_worktree_raises(tmp_path):
    with pytest.raises(SandboxConfigError, match="cannot scan"):
        sandbox.sensitive_files_in(tmp_path / "missing")


def test_mask_args_shape(tmp_path, empty_file):
    root = make_tree(tmp_path / "wt")
    args = sandbox.mask_args(root, empty_file)
    names = [".env", ".env.local", "credentials.json", "secrets.yaml", "src/.env.production", "src/deep/id_rsa",
             "src/deep/server.PEM", "sub/notes.p12"]
    expected = []
    for name in names:
        expected += ["--mount", f"type=bind,source={empty_file},target=/workspace/{name},readonly"]
    assert args == expected


def test_mask_args_is_empty_when_nothing_is_sensitive(tmp_path, empty_file):
    root = tmp_path / "wt"
    root.mkdir()
    (root / "app.py").write_text("x", encoding="utf-8")
    assert sandbox.mask_args(root, empty_file) == []


def test_mask_args_needs_a_real_empty_source(tmp_path):
    root = tmp_path / "wt"
    root.mkdir()
    with pytest.raises(SandboxConfigError, match="does not exist"):
        sandbox.mask_args(root, tmp_path / "nope")
    full = tmp_path / "full"
    full.write_text("secret", encoding="utf-8")
    with pytest.raises(SandboxConfigError, match="not empty"):
        sandbox.mask_args(root, full)
    with pytest.raises(SandboxConfigError):
        sandbox.mask_args(root, tmp_path)


def test_mask_args_refuses_more_files_than_the_limit(tmp_path, empty_file):
    root = tmp_path / "wt"
    root.mkdir()
    for index in range(5):
        (root / f"k{index}.pem").write_text("x", encoding="utf-8")
    assert len(sandbox.mask_args(root, empty_file, limit=5)) == 10
    with pytest.raises(SandboxConfigError, match="5 files with sensitive names"):
        sandbox.mask_args(root, empty_file, limit=4)
    assert sandbox.MAX_MASKS == 64


def test_mask_args_quotes_a_comma_in_a_path(tmp_path, empty_file):
    root = tmp_path / "wt"
    (root / "a,b").mkdir(parents=True)
    (root / "a,b" / ".env").write_text("x", encoding="utf-8")
    args = sandbox.mask_args(root, empty_file)
    assert args == ["--mount", f'type=bind,source={empty_file},"target=/workspace/a,b/.env",readonly']


def test_mask_args_extra_patterns(tmp_path, empty_file):
    root = tmp_path / "wt"
    root.mkdir()
    (root / "vault.kdbx").write_text("x", encoding="utf-8")
    assert sandbox.mask_args(root, empty_file) == []
    assert len(sandbox.mask_args(root, empty_file, extra_patterns=("*.kdbx",))) == 2


# ---------------------------------------------------------------------------------------------------------------
# docker_run_argv
# ---------------------------------------------------------------------------------------------------------------


def argv(**kwargs):
    kwargs.setdefault("home", HOME)
    return sandbox.docker_run_argv(POLICY, WIN_WT, "make test", **kwargs)


def pair(args, flag):
    """The value after flag, asserting the flag appears exactly once."""
    assert args.count(flag) == 1, f"{flag} appears {args.count(flag)} times"
    return args[args.index(flag) + 1]


def test_the_default_argv_is_exactly_this():
    assert argv() == [
        "docker", "run", "--rm", "--pull", "never",
        "--network", "none", "--cpus", "2", "--memory", "4096m", "--pids-limit", "512",
        "--security-opt", "no-new-privileges", "--cap-drop", "ALL",
        "--mount", "type=bind,source=C:\\work\\wt,target=/workspace",
        "-w", "/workspace",
        IMAGE, "sh", "-lc", "make test",
    ]


def test_the_fully_loaded_argv_is_exactly_this():
    args = argv(container_name="ases-gate-1", user="1000:1000", env={"CI": "1", "TZ": "UTC"}, network=True)
    assert args == [
        "docker", "run", "--rm", "--pull", "never", "--name", "ases-gate-1",
        "--network", "bridge", "--cpus", "2", "--memory", "4096m", "--pids-limit", "512",
        "--security-opt", "no-new-privileges", "--cap-drop", "ALL", "--user", "1000:1000",
        "--mount", "type=bind,source=C:\\work\\wt,target=/workspace",
        "-w", "/workspace", "-e", "CI=1", "-e", "TZ=UTC",
        IMAGE, "sh", "-lc", "make test",
    ]


def test_network_is_none_by_default_and_bridge_only_when_asked():
    assert pair(argv(), "--network") == "none"
    assert pair(argv(network=False), "--network") == "none"
    assert pair(argv(network=True), "--network") == "bridge"
    open_policy = dataclasses.replace(POLICY, network=True)
    assert pair(sandbox.docker_run_argv(open_policy, WIN_WT, "x", home=HOME), "--network") == "bridge"
    assert pair(sandbox.docker_run_argv(open_policy, WIN_WT, "x", home=HOME, network=False), "--network") == "none"
    assert pair(sandbox.docker_run_argv(POLICY, WIN_WT, "x", home=HOME, network=None), "--network") == "none"


def test_the_limits_and_hardening_come_from_the_policy():
    policy = SandboxPolicy(image=IMAGE, cpu=1.5, memory_mb=2048, pids_limit=64)
    args = sandbox.docker_run_argv(policy, WIN_WT, "x", home=HOME)
    assert pair(args, "--cpus") == "1.5"
    assert pair(args, "--memory") == "2048m"
    assert pair(args, "--pids-limit") == "64"
    assert pair(args, "--security-opt") == "no-new-privileges"
    assert pair(args, "--cap-drop") == "ALL"
    assert pair(argv(), "--cpus") == "2"


def test_it_never_pulls_and_is_not_read_only():
    args = argv()
    assert pair(args, "--pull") == "never"
    assert "--rm" in args
    assert "--read-only" not in args
    assert "--privileged" not in args


def test_there_is_no_env_file_and_no_inherited_environment(monkeypatch):
    monkeypatch.setenv("ASES_TEST_PROVIDER_API_KEY", PLANTED)
    monkeypatch.setenv("ASES_TEST_PLAIN", "visible")
    args = argv()
    assert "--env-file" not in args and "-e" not in args and "--env" not in args
    assert not any(PLANTED in a or "visible" in a for a in args)
    assert not any(a.startswith("--env-file") for a in argv(env={"CI": "1"}))


def test_only_the_given_env_is_passed():
    args = argv(env={"CI": "1", "LANG": "C.UTF-8"})
    assert [a for a in args if a == "-e"] == ["-e", "-e"]
    assert "CI=1" in args and "LANG=C.UTF-8" in args
    assert "-e" not in argv(env={})
    assert "-e" not in argv(env=None)


@pytest.mark.parametrize("name", [
    "OPENAI_API_KEY", "GITHUB_TOKEN", "DB_PASSWORD", "MY_SECRET", "google_application_credentials",
])
def test_a_credential_shaped_env_name_raises(name):
    with pytest.raises(SandboxConfigError, match="credential"):
        argv(env={name: "x"})


@pytest.mark.parametrize("name", ["1BAD", "has space", "a=b", "", "-x", "A-B"])
def test_an_env_name_that_is_not_a_variable_name_raises(name):
    with pytest.raises(SandboxConfigError, match="variable name"):
        argv(env={name: "x"})


def test_an_env_value_with_a_nul_raises():
    with pytest.raises(SandboxConfigError, match="NUL"):
        argv(env={"CI": "a\0b"})


def test_user_and_name_only_when_given():
    plain = argv()
    assert "--user" not in plain and "--name" not in plain
    assert pair(argv(user="1000:1000"), "--user") == "1000:1000"
    assert pair(argv(container_name="ases-1"), "--name") == "ases-1"


@pytest.mark.parametrize("user", ["1000", "1000:1000", "node", "node:node", "app.user-1_x", "0:0", "root"])
def test_a_plain_user_is_passed_through_and_root_is_the_callers_call(user):
    """docker_run_argv checks the FORMAT only. A controller that itself runs as root on WSL2 has host user 0."""
    assert pair(argv(user=user), "--user") == user


@pytest.mark.parametrize("user", [
    "", "1000\n", "a b", "-v /:/host", "1000:", ":1000", "a:b:c", "a;b", "$(id)", "1000 ",
])
def test_a_user_that_is_not_a_name_or_uid_raises(user):
    with pytest.raises(SandboxConfigError, match="container user"):
        argv(user=user)


@pytest.mark.parametrize("name", ["", "a b", "-x", "a;b", "/x", "a/b"])
def test_a_bad_container_name_raises(name):
    with pytest.raises(SandboxConfigError, match="container name"):
        argv(container_name=name)


def test_the_worktree_is_mounted_with_mount_syntax_and_never_dash_v():
    args = argv()
    assert "-v" not in args and "--volume" not in args
    assert pair(args, "--mount") == "type=bind,source=C:\\work\\wt,target=/workspace"
    assert pair(argv(), "-w") == "/workspace"


@pytest.mark.parametrize("worktree,source", [
    ("C:\\a\\b", "C:\\a\\b"),
    ("C:/a/b", "C:/a/b"),
    ("C:\\a\\b\\", "C:\\a\\b"),
    ("C:\\Program Files\\x y\\wt", "C:\\Program Files\\x y\\wt"),
    ("/srv/work/wt", "/srv/work/wt"),
])
def test_a_worktree_path_becomes_the_bind_source(worktree, source):
    args = sandbox.docker_run_argv(POLICY, worktree, "x", home=HOME)
    assert pair(args, "--mount") == f"type=bind,source={source},target=/workspace"


def test_a_comma_or_quote_in_the_worktree_path_is_csv_quoted():
    args = sandbox.docker_run_argv(POLICY, "C:\\a,b\\wt", "x", home=HOME)
    assert pair(args, "--mount") == 'type=bind,"source=C:\\a,b\\wt",target=/workspace'
    args = sandbox.docker_run_argv(POLICY, 'C:\\a"b\\wt', "x", home=HOME)
    assert pair(args, "--mount") == 'type=bind,"source=C:\\a""b\\wt",target=/workspace'


def test_a_control_character_in_a_path_raises():
    with pytest.raises(SandboxConfigError, match="control character"):
        sandbox.docker_run_argv(POLICY, "C:\\a\nb\\wt", "x", home=HOME)


@pytest.mark.parametrize("worktree", [
    "wt", "relative\\wt", "C:", "C:\\", "/", "C:\\Users\\tester", "C:\\Users", "C:\\Users\\tester\\.ssh",
    "C:\\Users\\tester\\.ssh\\proj", "C:\\Users\\tester\\AppData", "C:\\work\\.env",
])
def test_a_dangerous_worktree_raises(worktree):
    with pytest.raises(SandboxConfigError):
        sandbox.docker_run_argv(POLICY, worktree, "x", home=HOME)


def test_a_foreign_extra_mount_raises_with_the_reasons():
    for foreign in ("C:\\Users\\tester\\.ssh", "C:\\Users\\tester", "C:\\", "C:\\work", "C:\\other\\dir", "rel"):
        with pytest.raises(SandboxConfigError, match="mount"):
            argv(extra_mounts=[foreign])


def test_an_extra_mount_inside_the_worktree_is_a_read_only_overlay_at_the_same_path():
    args = argv(extra_mounts=["C:\\work\\wt\\tests", "C:\\work\\wt\\ci\\gates.yaml"])
    mounts = [args[i + 1] for i, a in enumerate(args) if a == "--mount"]
    assert mounts == [
        "type=bind,source=C:\\work\\wt,target=/workspace",
        "type=bind,source=C:\\work\\wt\\tests,target=/workspace/tests,readonly",
        "type=bind,source=C:\\work\\wt\\ci\\gates.yaml,target=/workspace/ci/gates.yaml,readonly",
    ]


def test_an_extra_mount_of_the_worktree_itself_makes_the_primary_mount_read_only():
    """docker refuses two mounts at /workspace, so there must be exactly one, and it must be read-only."""
    args = argv(extra_mounts=[WIN_WT])
    mounts = [args[i + 1] for i, a in enumerate(args) if a == "--mount"]
    assert mounts == ["type=bind,source=C:\\work\\wt,target=/workspace,readonly"]
    args = argv(extra_mounts=[WIN_WT, "C:\\work\\wt\\tests"])
    mounts = [args[i + 1] for i, a in enumerate(args) if a == "--mount"]
    assert mounts == [
        "type=bind,source=C:\\work\\wt,target=/workspace,readonly",
        "type=bind,source=C:\\work\\wt\\tests,target=/workspace/tests,readonly",
    ]


def test_an_extra_mount_given_twice_is_mounted_once_even_with_different_case_on_windows():
    args = argv(extra_mounts=["C:\\work\\wt\\tests", "C:\\work\\wt\\tests", "c:\\WORK\\wt\\TESTS"])
    mounts = [args[i + 1] for i, a in enumerate(args) if a == "--mount"]
    assert mounts == [
        "type=bind,source=C:\\work\\wt,target=/workspace",
        "type=bind,source=C:\\work\\wt\\tests,target=/workspace/tests,readonly",
    ]


def test_posix_extra_mounts_that_differ_only_in_case_are_different_paths():
    args = sandbox.docker_run_argv(POLICY, "/work/wt", "x", home=POSIX_HOME, extra_mounts=["/work/wt/a", "/work/wt/A"])
    targets = [a.split("target=")[1].split(",")[0] for a in args if a.startswith("type=bind")]
    assert targets == ["/workspace", "/workspace/a", "/workspace/A"]


def test_a_bare_string_extra_mount_is_one_mount():
    args = argv(extra_mounts="C:\\work\\wt\\tests")
    assert sum(1 for a in args if a == "--mount") == 2


def test_masks_are_added_only_when_an_empty_file_is_given(tmp_path, empty_file):
    root = tmp_path / "wt"
    (root / "sub").mkdir(parents=True)
    (root / ".env").write_text("x", encoding="utf-8")
    (root / "sub" / "id_rsa").write_text("x", encoding="utf-8")
    without = sandbox.docker_run_argv(POLICY, root, "x", home=HOME)
    assert sum(1 for a in without if a == "--mount") == 1
    masked = sandbox.docker_run_argv(POLICY, root, "x", home=HOME, empty_file=empty_file)
    mounts = [masked[i + 1] for i, a in enumerate(masked) if a == "--mount"]
    assert mounts == [
        f"type=bind,source={root},target=/workspace",
        f"type=bind,source={empty_file},target=/workspace/.env,readonly",
        f"type=bind,source={empty_file},target=/workspace/sub/id_rsa,readonly",
    ]
    assert masked.index(mounts[0]) < masked.index(mounts[1])


def test_extra_deny_patterns_from_the_policy_are_masked(tmp_path, empty_file):
    root = tmp_path / "wt"
    root.mkdir()
    (root / "vault.kdbx").write_text("x", encoding="utf-8")
    policy = SandboxPolicy(image=IMAGE, extra_deny=("*.kdbx",))
    args = sandbox.docker_run_argv(policy, root, "x", home=HOME, empty_file=empty_file)
    assert f"type=bind,source={empty_file},target=/workspace/vault.kdbx,readonly" in args


def test_a_bad_mask_source_raises_through_the_argv(tmp_path):
    root = tmp_path / "wt"
    root.mkdir()
    with pytest.raises(SandboxConfigError):
        sandbox.docker_run_argv(POLICY, root, "x", home=HOME, empty_file=tmp_path / "missing")


def _policy_with(image):
    policy = SandboxPolicy(image="placeholder:1")
    object.__setattr__(policy, "image", image)   # a policy that skipped validation, as a buggy caller could hold
    return policy


@pytest.mark.parametrize("image", ["", "--privileged", "a b", "x;y", "python:3\n", None, 5])
def test_a_bad_image_raises(image):
    with pytest.raises(SandboxConfigError, match="image"):
        sandbox.docker_run_argv(_policy_with(image), WIN_WT, "x", home=HOME)


def test_the_command_must_be_a_string():
    for command in (["make", "test"], None, 5):
        with pytest.raises(SandboxConfigError, match="command"):
            sandbox.docker_run_argv(POLICY, WIN_WT, command, home=HOME)


def test_a_command_is_one_argument_after_sh_lc():
    args = sandbox.docker_run_argv(POLICY, WIN_WT, "echo 'a b'; ls", home=HOME)
    assert args[-3:] == ["sh", "-lc", "echo 'a b'; ls"]
    assert args[-4] == IMAGE


def test_every_argv_element_is_a_string(tmp_path, empty_file):
    root = tmp_path / "wt"
    root.mkdir()
    (root / ".env").write_text("x", encoding="utf-8")
    args = sandbox.docker_run_argv(
        SandboxPolicy(image=IMAGE, cpu=1.5), root, "x", home=HOME, empty_file=empty_file,
        env={"CI": 1}, user=1000, container_name="n",
    )
    assert all(isinstance(a, str) for a in args)


def test_the_argv_works_with_the_real_home_by_default(worktree):
    args = sandbox.docker_run_argv(POLICY, worktree, "echo hi")
    assert pair(args, "--mount") == f"type=bind,source={worktree},target=/workspace"


def test_host_user_spec_uses_the_injected_ids_and_none_without_them():
    assert sandbox.host_user_spec(getuid=lambda: 1000, getgid=lambda: 1001) == "1000:1001"
    assert sandbox.host_user_spec(getuid=lambda: 0, getgid=lambda: 0) == "0:0"

    def boom():
        raise OSError("no ids")

    assert sandbox.host_user_spec(getuid=boom, getgid=lambda: 1) is None
    if not hasattr(os, "getuid"):
        assert sandbox.host_user_spec() is None
    else:
        assert sandbox.host_user_spec() == f"{os.getuid()}:{os.getgid()}"


# ---------------------------------------------------------------------------------------------------------------
# The runner, docker_available, image_present, pull_command
# ---------------------------------------------------------------------------------------------------------------


def test_default_runner_returns_output_and_exit_code():
    ok = sandbox.default_runner([sys.executable, "-c", "print('ok')"], 60)
    assert ok.returncode == 0 and ok.stdout.strip() == "ok"
    bad = sandbox.default_runner([sys.executable, "-c", "import sys; sys.stderr.write('bad'); sys.exit(3)"], 60)
    assert bad.returncode == 3 and bad.stderr == "bad"


def test_default_runner_never_raises_on_a_timeout():
    slow = sandbox.default_runner([sys.executable, "-c", "import time; time.sleep(60)"], 0.5)
    assert slow.returncode == sandbox.RC_TIMEOUT == 124
    assert "timed out after 0.5s" in slow.stderr


def test_default_runner_never_raises_on_a_missing_command():
    missing = sandbox.default_runner(["ases-no-such-command-xyz", "--version"], 10)
    assert missing.returncode == sandbox.RC_NOT_FOUND == 127
    assert "not found" in missing.stderr and "ases-no-such-command-xyz" in missing.stderr


def test_default_runner_never_raises_on_an_os_error(monkeypatch):
    def boom(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(sandbox.subprocess, "run", boom)
    denied = sandbox.default_runner(["docker", "info"], 10)
    assert denied.returncode == sandbox.RC_ERROR == 126
    assert "could not run docker" in denied.stderr


def test_default_runner_closes_stdin_and_passes_the_timeout(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    sandbox.default_runner(["docker", "info"], 7)
    assert seen["timeout"] == 7 and seen["stdin"] == subprocess.DEVNULL and seen["capture_output"] is True
    assert seen["text"] is True and seen["encoding"] == "utf-8" and seen["errors"] == "replace"
    assert "shell" not in seen or seen["shell"] is False


def test_default_runner_inherits_the_environment_unless_one_is_given(monkeypatch):
    """The plain (argv, timeout) call every probe makes inherits the parent's environment, credentials included:
    the key visibility test must be able to see a credential that a mis-built docker run would forward. A caller
    that starts hermes passes its own scrubbed copy (profiles.apply_init, ASES-CFG-05)."""
    seen = []

    def fake_run(argv, **kwargs):
        seen.append(kwargs.get("env"))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    sandbox.default_runner(["docker", "info"], 7)
    sandbox.default_runner(["hermes", "profile", "create", "x"], 7, env={"PATH": "p", "ASES_ONLY": "1"})
    assert seen == [None, {"PATH": "p", "ASES_ONLY": "1"}]


def test_every_probe_carries_its_own_timeout():
    """20 seconds to ask docker a question, 120 to run a container."""
    seen = {}

    def recording(argv, timeout):
        seen[argv[1]] = timeout
        return result(0, "27\n")

    sandbox.docker_available(recording)
    sandbox.image_present(IMAGE, recording)
    assert seen == {"info": 20, "image": 20}
    runs = []

    def run_recording(argv, timeout):
        runs.append(timeout)
        return result(0, "27\n") if argv[1] == "info" else result(1, "", "bad address")

    sandbox.exfiltration_probe(POLICY, WIN_WT, runner=run_recording, home=HOME)
    assert runs == [20, 120]


def test_docker_available_when_the_daemon_answers():
    runner = FakeDocker(info=result(0, "27.0.1\n"))
    ok, why = sandbox.docker_available(runner)
    assert ok is True and "27.0.1" in why
    assert runner.calls == [["docker", "info", "--format", "{{.ServerVersion}}"]]


@pytest.mark.parametrize("info,fragment", [
    (result(127, "", "command not found: docker"), "docker CLI not found on PATH"),
    (result(124, "", "timed out after 20s"), "timed out"),
    (result(1, "", "Cannot connect to the Docker daemon at npipe:////./pipe/docker_engine. Is it running?\nmore"),
     "docker daemon not reachable: Cannot connect to the Docker daemon"),
    (result(1, "", ""), "exit 1"),
    (result(126, "", "could not run docker: denied"), "not reachable"),
])
def test_docker_available_says_why_not(info, fragment):
    ok, why = sandbox.docker_available(FakeDocker(info=info))
    assert ok is False and fragment in why
    assert why.isascii() and "\n" not in why


def test_docker_available_never_raises_on_a_broken_runner():
    def raising(argv, timeout):
        raise RuntimeError("boom")

    ok, why = sandbox.docker_available(raising)
    assert ok is False and "RuntimeError" in why

    ok, why = sandbox.docker_available(lambda argv, timeout: object())
    assert ok is False


def test_docker_available_is_ascii_for_a_non_ascii_message():
    ok, why = sandbox.docker_available(FakeDocker(info=result(1, "", "erreur: caf\u00e9 \u2192 ferm\u00e9")))
    assert ok is False and why.isascii()


def test_image_present_asks_docker_inspect_and_never_pulls():
    runner = FakeDocker(inspect=result(0, "sha256:abc\n"))
    assert sandbox.image_present(IMAGE, runner) is True
    assert runner.calls == [["docker", "image", "inspect", "--format", "{{.Id}}", IMAGE]]
    assert sandbox.image_present(IMAGE, FakeDocker(inspect=result(1, "", "No such image"))) is False
    assert sandbox.image_present(IMAGE, FakeDocker(inspect=result(124, "", "timed out"))) is False
    assert sandbox.image_present(IMAGE, FakeDocker(inspect=result(127, "", "not found"))) is False


def test_image_present_does_not_call_docker_for_a_bad_reference():
    runner = FakeDocker()
    for image in ("", "--all", "a b", None, 5):
        assert sandbox.image_present(image, runner) is False
    assert runner.calls == []


def test_image_present_never_raises_on_a_broken_runner():
    def raising(argv, timeout):
        raise OSError("gone")

    assert sandbox.image_present(IMAGE, raising) is False


def test_pull_command_returns_the_argv_and_runs_nothing(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("pull_command must not execute anything")

    monkeypatch.setattr(sandbox.subprocess, "run", forbidden)
    monkeypatch.setattr(sandbox.subprocess, "Popen", forbidden)
    monkeypatch.setattr(sandbox, "default_runner", forbidden)
    assert sandbox.pull_command(IMAGE) == ["docker", "pull", IMAGE]
    with pytest.raises(SandboxConfigError):
        sandbox.pull_command("--all-tags")
    with pytest.raises(SandboxConfigError):
        sandbox.pull_command("")


def test_remove_container_command_returns_the_argv_and_runs_nothing(monkeypatch):
    monkeypatch.setattr(sandbox.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran")))
    assert sandbox.remove_container_command("ases-gate-1") == ["docker", "rm", "-f", "ases-gate-1"]
    for bad in ("", "-f", "a b", None):
        with pytest.raises(SandboxConfigError):
            sandbox.remove_container_command(bad)


def test_the_module_never_starts_docker_or_pulls_on_its_own(monkeypatch):
    """Only the runner an integration passes in may touch docker, and docker_run_argv is only a builder."""
    monkeypatch.setattr(sandbox.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran")))
    monkeypatch.setattr(sandbox.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran")))
    argv()
    sandbox.terminal_block(POLICY)
    sandbox.check_terminal_block(good(), POLICY, home=HOME)
    sandbox.mount_problems([WIN_WT], WIN_WT, HOME)
    sandbox.pull_command(IMAGE)


# ---------------------------------------------------------------------------------------------------------------
# key_visibility_test (test 22.10)
# ---------------------------------------------------------------------------------------------------------------


def kv(worktree, empty_file, runner, secrets=(PLANTED, PLANTED_2), policy=POLICY):
    return sandbox.key_visibility_test(policy, worktree, list(secrets), runner=runner, empty_file=empty_file, home=HOME)


def test_key_visibility_passes_when_env_is_clean_and_the_mask_blocks_the_read(worktree, empty_file):
    runner = FakeDocker(run=sandbox_run())
    outcome = kv(worktree, empty_file, runner)
    assert outcome.passed is True and outcome.findings == []
    assert len(runner.runs) == 2


def test_key_visibility_passes_when_the_masked_read_is_empty_or_denied(worktree, empty_file):
    for cat in ((0, "", ""), (0, "  \n", ""), (1, "", "cat: /workspace/.env: Permission denied"),
                (1, "", "cat: /workspace/.env: Is a directory")):
        outcome = kv(worktree, empty_file, FakeDocker(run=sandbox_run(cat=cat)))
        assert outcome.passed is True and outcome.findings == [], cat


@pytest.mark.parametrize("stderr", [
    "cat: /workspace/.env: No such file or directory",
    "cat: can't open '/workspace/.env': No such file or directory",
    "CAT: /WORKSPACE/.ENV: NO SUCH FILE OR DIRECTORY",
])
def test_key_visibility_a_planted_env_that_is_not_there_proves_nothing(worktree, empty_file, stderr):
    outcome = kv(worktree, empty_file, FakeDocker(run=sandbox_run(cat=(1, "", stderr))))
    assert outcome.passed is False
    assert outcome.findings == [
        "the planted /workspace/.env is not visible inside the sandbox, so the mask proved nothing "
        "(is the worktree mounted?)"
    ]


def test_key_visibility_runs_env_and_cat_through_docker_run_argv_with_masks_and_no_network(worktree, empty_file):
    runner = FakeDocker(run=sandbox_run())
    kv(worktree, empty_file, runner)
    env_argv, cat_argv = runner.runs
    assert env_argv[-3:] == ["sh", "-lc", "env"]
    assert cat_argv[-3:] == ["sh", "-lc", "cat /workspace/.env"]
    for run in runner.runs:
        assert pair(run, "--network") == "none"
        assert f"type=bind,source={empty_file},target=/workspace/.env,readonly" in run
        assert pair(run, "--pull") == "never"
    assert runner.calls[0][1] == "info"


def test_key_visibility_fails_when_a_secret_is_in_the_environment(worktree, empty_file):
    runner = FakeDocker(run=sandbox_run(env_out=f"PATH=/usr/bin\nOPENROUTER_API_KEY={PLANTED}\n"))
    outcome = kv(worktree, empty_file, runner)
    assert outcome.passed is False
    assert outcome.findings == ["secret #0 appeared in the output of env inside the sandbox"]


def test_key_visibility_names_the_index_of_the_secret_not_its_position_among_usable_ones(worktree, empty_file):
    runner = FakeDocker(run=sandbox_run(env_out=f"X={PLANTED_2}\n"))
    outcome = kv(worktree, empty_file, runner, secrets=("", None, PLANTED_2))
    assert outcome.findings == ["secret #2 appeared in the output of env inside the sandbox"]


def test_key_visibility_fails_when_the_env_secret_is_on_stderr(worktree, empty_file):
    def run(argv):
        return result(0, "PATH=/x\n", f"warning {PLANTED}") if argv[-1] == "env" else result(1, "", "cat: no")

    outcome = kv(worktree, empty_file, FakeDocker(run=run))
    assert outcome.passed is False
    assert outcome.findings == ["secret #0 appeared in the output of env inside the sandbox"]


def test_key_visibility_fails_when_the_env_file_is_readable(worktree, empty_file):
    runner = FakeDocker(run=sandbox_run(cat=(0, "SOMETHING_ELSE=1\n", "")))
    outcome = kv(worktree, empty_file, runner)
    assert outcome.passed is False
    assert len(outcome.findings) == 1 and "/workspace/.env was readable" in outcome.findings[0]


def test_key_visibility_fails_when_the_planted_secret_comes_back_from_the_env_file(worktree, empty_file):
    runner = FakeDocker(run=sandbox_run(cat=(0, f"OPENROUTER_API_KEY={PLANTED}\n", "")))
    outcome = kv(worktree, empty_file, runner)
    assert outcome.passed is False
    assert any("readable" in f for f in outcome.findings)
    assert "secret #0 appeared in the output of cat /workspace/.env" in outcome.findings


def test_key_visibility_fails_when_the_secret_is_on_the_env_file_stderr(worktree, empty_file):
    runner = FakeDocker(run=sandbox_run(cat=(1, "", f"cat: {PLANTED_2}: denied")))
    outcome = kv(worktree, empty_file, runner)
    assert outcome.findings == ["secret #1 appeared in the output of cat /workspace/.env"]


def test_key_visibility_findings_never_contain_a_secret_value(worktree, empty_file):
    runs = [
        sandbox_run(env_out=f"A={PLANTED}\nB={PLANTED_2}\n", cat=(0, f"{PLANTED}\n{PLANTED_2}\n", "")),
        lambda argv: result(125, "", f"docker: Error response from daemon: {PLANTED} and {PLANTED_2} invalid"),
        lambda argv: result(0, PLANTED, PLANTED_2),
    ]
    for run in runs:
        outcome = kv(worktree, empty_file, FakeDocker(run=run))
        assert outcome.passed is False
        blob = "\n".join(outcome.findings)
        assert PLANTED not in blob and PLANTED_2 not in blob
        assert blob.isascii()


def test_key_visibility_scrubs_a_secret_out_of_a_stderr_excerpt(worktree, empty_file):
    runner = FakeDocker(run=lambda argv: result(125, "", f"error mentioning {PLANTED} here"))
    outcome = kv(worktree, empty_file, runner)
    assert any("[secret #0]" in f for f in outcome.findings)
    assert not any(PLANTED in f for f in outcome.findings)


def test_key_visibility_is_never_a_silent_pass_when_docker_is_unavailable(worktree, empty_file):
    for info in (result(127, "", "command not found: docker"), result(1, "", "Cannot connect"), result(124, "", "t")):
        runner = FakeDocker(info=info, run=sandbox_run())
        outcome = kv(worktree, empty_file, runner)
        assert outcome.passed is False
        assert len(outcome.findings) == 1 and outcome.findings[0].startswith("the test could not run: ")
        assert runner.runs == []


def test_key_visibility_needs_at_least_one_secret_to_look_for(worktree, empty_file):
    for secrets in ((), ("",), (None,), ("", None)):
        outcome = kv(worktree, empty_file, FakeDocker(run=sandbox_run()), secrets=secrets)
        assert outcome.passed is False
        assert outcome.findings == ["the test could not run: no secret values were supplied to look for"]
    outcome = sandbox.key_visibility_test(POLICY, worktree, None, runner=FakeDocker(), empty_file=empty_file, home=HOME)
    assert outcome.passed is False


def test_key_visibility_needs_a_planted_env_file(tmp_path, empty_file):
    bare = tmp_path / "bare"
    bare.mkdir()
    runner = FakeDocker(run=sandbox_run())
    outcome = kv(bare, empty_file, runner)
    assert outcome.passed is False
    assert len(outcome.findings) == 1 and "no .env" in outcome.findings[0]
    assert runner.runs == []


def test_key_visibility_reports_both_blockers_at_once(tmp_path, empty_file):
    bare = tmp_path / "bare"
    bare.mkdir()
    outcome = kv(bare, empty_file, FakeDocker(), secrets=())
    assert len(outcome.findings) == 2


def test_key_visibility_a_failed_docker_run_is_not_a_working_mask(worktree, empty_file):
    for code in (125, 126, 127, 124, 2):
        runner = FakeDocker(run=lambda argv, code=code: result(code, "", "docker failed"))
        outcome = kv(worktree, empty_file, runner)
        assert outcome.passed is False, code
        assert any("could not run inside the sandbox" in f for f in outcome.findings)
    # cat exiting 1 because it cannot read the file is fine, as long as docker itself ran and the env run worked
    denied = FakeDocker(run=sandbox_run(cat=(1, "", "cat: /workspace/.env: Permission denied")))
    assert kv(worktree, empty_file, denied).passed is True


def test_key_visibility_reports_an_env_run_that_could_not_execute(worktree, empty_file):
    def run(argv):
        return result(125, "", "no such image") if argv[-1] == "env" else result(1, "", "cat: denied")

    outcome = kv(worktree, empty_file, FakeDocker(run=run))
    assert outcome.passed is False
    assert outcome.findings == ["env could not run inside the sandbox (exit 125): no such image"]


def test_key_visibility_never_raises(worktree, empty_file):
    def raising(argv, timeout):
        raise RuntimeError("boom")

    outcome = kv(worktree, empty_file, raising)
    assert outcome.passed is False and outcome.findings

    def raise_on_run(argv, timeout):
        if argv[1] == "run":
            raise RuntimeError("boom")
        return result(0, "27\n")

    outcome = kv(worktree, empty_file, raise_on_run)
    assert outcome.passed is False and outcome.findings

    outcome = sandbox.key_visibility_test(
        POLICY, "relative-worktree", [PLANTED], runner=FakeDocker(), empty_file=empty_file, home=HOME,
    )
    assert outcome.passed is False


@pytest.mark.parametrize("bad_worktree", [None, 5, "", "\0", [], "relative"])
def test_key_visibility_never_raises_for_a_worktree_that_is_not_a_path(bad_worktree, empty_file):
    outcome = sandbox.key_visibility_test(
        POLICY, bad_worktree, [PLANTED], runner=FakeDocker(run=sandbox_run()), empty_file=empty_file, home=HOME,
    )
    assert outcome.passed is False and outcome.findings


def test_key_visibility_reports_a_bad_mask_source_instead_of_raising(worktree, tmp_path):
    outcome = kv(worktree, tmp_path / "missing", FakeDocker(run=sandbox_run()))
    assert outcome.passed is False and "the test could not run" in outcome.findings[0]


def test_key_visibility_result_shape():
    good_result = sandbox.KeyVisibilityResult(True)
    assert good_result.findings == [] and good_result.passed is True
    with pytest.raises(dataclasses.FrozenInstanceError):
        good_result.passed = False


# ---------------------------------------------------------------------------------------------------------------
# exfiltration_probe (test 22.11)
# ---------------------------------------------------------------------------------------------------------------


def probe(worktree, run, policy=POLICY, info=None):
    runner = FakeDocker(run=run, info=info)
    return sandbox.exfiltration_probe(policy, worktree, runner=runner, home=HOME), runner


@pytest.mark.parametrize("code,out,err", [
    (1, "", "wget: bad address 'example.com'"),
    (6, "", "curl: (6) Could not resolve host: example.com"),
    (1, "", "wget: unable to resolve host address 'example.com'"),
    (1, "", "socket.gaierror: [Errno -3] Temporary failure in name resolution"),
    (1, "", "OSError: [Errno 101] Network is unreachable"),
    (7, "", "curl: (7) Failed to connect to example.com port 80: Connection refused"),
    (4, "", "wget: download timed out"),
])
def test_the_probe_passes_when_the_call_fails_with_a_network_error(worktree, code, out, err):
    outcome, runner = probe(worktree, lambda argv: result(code, out, err))
    assert outcome.passed is True and outcome.findings == []
    assert len(runner.runs) == 1


@pytest.mark.parametrize("usage", [
    "BusyBox v1.36 multi-call binary.\nUsage: wget [-T SEC] URL\n  -T SEC  Network read timeout is SEC seconds",
    "wget: unrecognized option '-T'\nUsage: wget [OPTION]... [URL]...\n  --timeout=SECS  set all timeout values",
    "curl: option --resolve: is unknown\ncurl: try 'curl --help' for more information (resolve, timeout)",
])
def test_a_clients_usage_text_is_not_mistaken_for_a_blocked_network(worktree, usage):
    """The words timeout and resolve appear in usage text, so they are not markers on their own."""
    outcome, _ = probe(worktree, lambda argv: result(1, "", usage))
    assert outcome.passed is False
    assert "not with a network error" in outcome.findings[0]


def test_an_http_error_page_means_the_network_was_reachable(worktree):
    outcome, _ = probe(worktree, lambda argv: result(8, "", "wget: server returned error: HTTP/1.1 403 Forbidden"))
    assert outcome.passed is False and "not with a network error" in outcome.findings[0]


def test_the_probe_fails_when_the_call_succeeds(worktree):
    outcome, _ = probe(worktree, lambda argv: result(0, "<html>", ""))
    assert outcome.passed is False
    assert outcome.findings == ["the network call succeeded from inside the sandbox: the network is not disabled"]


def test_the_probe_fails_when_the_image_has_no_client(worktree):
    outcome, _ = probe(worktree, lambda argv: result(99, "", "no network client in the image"))
    assert outcome.passed is False and "proved nothing" in outcome.findings[0]


def test_the_probe_fails_when_the_failure_is_not_a_network_error(worktree):
    outcome, _ = probe(worktree, lambda argv: result(1, "", "wget: unrecognized option '-T'"))
    assert outcome.passed is False
    assert "not with a network error" in outcome.findings[0] and "unrecognized option" in outcome.findings[0]


@pytest.mark.parametrize("code", [124, 125, 126, 127])
def test_the_probe_fails_when_docker_could_not_run_it(worktree, code):
    outcome, _ = probe(worktree, lambda argv: result(code, "", "Unable to find image locally"))
    assert outcome.passed is False and f"exit {code}" in outcome.findings[0]


def test_the_probe_is_never_a_silent_pass_without_docker(worktree):
    outcome, runner = probe(worktree, lambda argv: result(1, "", "bad address"), info=result(1, "", "no daemon"))
    assert outcome.passed is False and "could not run" in outcome.findings[0]
    assert runner.runs == []


def test_the_probe_forces_the_network_off_whatever_the_policy_says(worktree):
    open_policy = dataclasses.replace(POLICY, network=True)
    outcome, runner = probe(worktree, lambda argv: result(1, "", "bad address"), policy=open_policy)
    assert outcome.passed is True
    assert pair(runner.runs[0], "--network") == "none"
    assert runner.runs[0][-2] == "-lc"
    assert runner.runs[0][-1] == sandbox._EXFIL_COMMAND
    assert "example.com" in runner.runs[0][-1]


def test_the_probe_never_raises(worktree, tmp_path):
    def raising(argv, timeout):
        raise RuntimeError("boom")

    assert sandbox.exfiltration_probe(POLICY, worktree, runner=raising, home=HOME).passed is False
    assert sandbox.exfiltration_probe(POLICY, "relative", runner=FakeDocker(), home=HOME).passed is False


def run_probe_script(stubs):
    """Run the probe script under a real sh with the network clients replaced by shell functions, so every branch
    executes and nothing can reach a network. stubs maps a tool name to (exit code, text on stderr), or is empty
    for an image with no client at all."""
    shell = shutil.which("sh")
    if shell is None:
        pytest.skip("no sh on this machine to run the probe script")
    known = "".join(f"{tool}) return 0;; " for tool in stubs)
    lines = ["command() { case \"$2\" in " + known + "*) return 1;; esac; }"]
    for tool, (code, text) in stubs.items():
        lines.append(f"{tool}() {{ echo \"{tool}: $*: {text}\" >&2; return {code}; }}")
    lines.append(sandbox._EXFIL_COMMAND)
    return subprocess.run([shell, "-c", "\n".join(lines)], capture_output=True, text=True, timeout=60)


def test_the_probe_script_uses_wget_first_and_asks_for_a_short_timeout():
    ran = run_probe_script({"wget": (4, "bad address"), "curl": (6, "x"), "python3": (1, "x")})
    assert ran.returncode == 4
    assert "wget: -T 3 -O /dev/null http://example.com" in ran.stderr and "curl" not in ran.stderr


def test_the_probe_script_falls_back_to_curl_then_python():
    ran = run_probe_script({"curl": (6, "Could not resolve host"), "python3": (1, "x")})
    assert ran.returncode == 6 and "curl: -sS -m 3 -o /dev/null http://example.com" in ran.stderr
    ran = run_probe_script({"python3": (1, "gaierror")})
    assert ran.returncode == 1
    assert "socket.create_connection(('example.com', 80), 3)" in ran.stderr and "import socket" in ran.stderr


def test_the_probe_script_exits_99_and_says_so_when_the_image_has_no_client():
    ran = run_probe_script({})
    assert ran.returncode == sandbox._EXFIL_RC_NO_CLIENT == 99
    assert "no network client in the image" in ran.stderr


def test_the_probe_script_passes_a_clients_success_through():
    assert run_probe_script({"wget": (0, "")}).returncode == 0


def test_the_probe_script_is_valid_posix_sh():
    shell = shutil.which("sh")
    if shell is None:
        pytest.skip("no sh on this machine to parse the probe script")
    parsed = subprocess.run([shell, "-n", "-c", sandbox._EXFIL_COMMAND], capture_output=True, text=True, timeout=30)
    assert parsed.returncode == 0, parsed.stderr


# ---------------------------------------------------------------------------------------------------------------
# doctor_checks
# ---------------------------------------------------------------------------------------------------------------


def test_doctor_rows_for_a_healthy_setup(tmp_path):
    root = tmp_path / "profiles"
    write_profile(root, "coder-1", {"model": {}, "terminal": good()})
    rows = sandbox.doctor_checks(POLICY, [root / "coder-1"], runner=FakeDocker(), home=HOME)
    assert rows == [
        ("sandbox_docker", True, rows[0][2]),
        ("sandbox_image", True, f"image {IMAGE} is present locally"),
        ("sandbox_profile[coder-1]", True, "terminal block is compliant"),
    ]
    assert "reachable" in rows[0][2]


def test_doctor_rows_report_each_kind_of_profile_problem(tmp_path):
    root = tmp_path / "profiles"
    bad = good()
    bad["docker_network"] = True
    write_profile(root, "no-terminal", {"model": {}})
    write_profile(root, "loose", {"terminal": bad})
    write_profile(root, "broken", "model: [unclosed\n")
    write_profile(root, "no-file", None)
    rows = sandbox.doctor_checks(
        POLICY, [root / n for n in ("no-terminal", "loose", "broken", "no-file")], runner=FakeDocker(), home=HOME,
    )
    by_name = {name: (ok, detail) for name, ok, detail in rows}
    for name, needle in (
        ("no-terminal", "local backend"), ("loose", "docker_network is true"), ("broken", "not valid YAML"),
        ("no-file", "local backend"),
    ):
        ok, detail = by_name[f"sandbox_profile[{name}]"]
        assert ok is False and needle in detail, (name, detail)


def test_doctor_row_when_docker_is_down_and_the_image_is_not_checked(tmp_path):
    runner = FakeDocker(info=result(1, "", "Cannot connect to the Docker daemon"))
    rows = sandbox.doctor_checks(POLICY, [], runner=runner, home=HOME)
    assert [(n, ok) for n, ok, _ in rows] == [("sandbox_docker", False), ("sandbox_image", False)]
    assert "Cannot connect" in rows[0][2] and "not checked" in rows[1][2]
    assert not any(c[1] == "image" for c in runner.calls)


def test_doctor_row_for_a_missing_image_carries_the_pull_command_but_does_not_run_it():
    runner = FakeDocker(inspect=result(1, "", "No such image"))
    rows = sandbox.doctor_checks(POLICY, [], runner=runner, home=HOME)
    assert rows[1][:2] == ("sandbox_image", False)
    assert f"a human runs: docker pull {IMAGE}" in rows[1][2]
    assert not any(c[1] == "pull" for c in runner.calls)


def test_doctor_image_argument_wins_and_no_image_means_no_row():
    runner = FakeDocker()
    rows = sandbox.doctor_checks(POLICY, [], runner=runner, image="other/img:2", home=HOME)
    assert rows[1] == ("sandbox_image", True, "image other/img:2 is present locally")
    assert ["docker", "image", "inspect", "--format", "{{.Id}}", "other/img:2"] in runner.calls
    rows = sandbox.doctor_checks(SandboxPolicy(image=""), [], runner=FakeDocker(), home=HOME)
    assert [name for name, _, _ in rows] == ["sandbox_docker"]
    rows = sandbox.doctor_checks(SandboxPolicy(image=""), [], runner=FakeDocker(), image="x/y:1", home=HOME)
    assert [name for name, _, _ in rows] == ["sandbox_docker", "sandbox_image"]


def test_doctor_a_bad_image_name_is_a_failed_row_not_an_exception():
    rows = sandbox.doctor_checks(POLICY, [], runner=FakeDocker(), image="--all", home=HOME)
    assert rows[1][:2] == ("sandbox_image", False)


def test_doctor_rows_are_plain_tuples_and_ascii_and_secret_free(tmp_path):
    root = tmp_path / "profiles"
    secret_config = {"terminal": {**good(), "docker_env": {"MY_API_KEY": PLANTED}}, "providers": {"k": PLANTED}}
    write_profile(root, "caf\u00e9", secret_config)
    rows = sandbox.doctor_checks(POLICY, [root / "caf\u00e9"], runner=FakeDocker(), home=HOME)
    assert all(isinstance(row, tuple) and len(row) == 3 and isinstance(row[1], bool) for row in rows)
    blob = "\n".join(f"{n} {d}" for n, _, d in rows)
    assert blob.isascii() and PLANTED not in blob


def test_doctor_with_no_profiles_and_no_image_is_just_the_docker_row():
    rows = sandbox.doctor_checks(SandboxPolicy(image=""), [], runner=FakeDocker(), home=HOME)
    assert [row[0] for row in rows] == ["sandbox_docker"]


def test_doctor_a_single_path_is_one_profile(tmp_path):
    directory = write_profile(tmp_path, "solo", {"terminal": good()})
    rows = sandbox.doctor_checks(SandboxPolicy(image=""), directory, runner=FakeDocker(), home=HOME)
    assert [r[0] for r in rows] == ["sandbox_docker", "sandbox_profile[solo]"]
    assert rows[1][1] is True


def test_doctor_a_profile_entry_that_is_not_a_path_is_a_failed_row(tmp_path):
    rows = sandbox.doctor_checks(SandboxPolicy(image=""), [None, 5], runner=FakeDocker(), home=HOME)
    assert [(n, ok) for n, ok, _ in rows] == [("sandbox_docker", True), ("sandbox_profile[?]", False),
                                             ("sandbox_profile[?]", False)]


def test_doctor_a_config_that_cannot_be_read_is_a_failed_row(tmp_path):
    directory = tmp_path / "binary"
    directory.mkdir()
    (directory / "config.yaml").write_bytes(b"\xff\xfe\x00")
    rows = sandbox.doctor_checks(SandboxPolicy(image=""), [directory], runner=FakeDocker(), home=HOME)
    assert rows[1][0] == "sandbox_profile[binary]" and rows[1][1] is False and "cannot read" in rows[1][2]


def test_doctor_never_raises_when_the_runner_does(tmp_path):
    def raising(argv, timeout):
        raise RuntimeError("boom")

    rows = sandbox.doctor_checks(POLICY, [tmp_path], runner=raising, home=HOME)
    assert rows[0][0] == "sandbox_docker" and rows[0][1] is False


# ---------------------------------------------------------------------------------------------------------------
# Spellings that must not hide a sensitive location. Each of these got past the first version of the checks.
# ---------------------------------------------------------------------------------------------------------------

SSH_SPELLINGS = [
    "C:\\Users\\tester\\.ssh.",                  # Win32 ignores a trailing dot ...
    "C:\\Users\\tester\\.ssh. .",                # ... and trailing spaces
    "C:\\Users\\tester\\.ssh .\\id_rsa",
    "C:\\Users\\tester\\proj\\..\\.ssh",
    "c:/USERS/Tester/.SSH",
    "\\\\?\\C:\\Users\\tester\\.ssh",            # the Win32 device prefix, both slash styles
    "//?/C:/Users/tester/.ssh",
    "\\\\.\\C:\\Users\\tester\\.ssh",
    "\\\\?\\UNC\\localhost\\c$\\Users\\tester\\.ssh",
    "\\\\localhost\\C$\\Users\\tester\\.ssh",     # the local drive through its administrative share
    "//localhost/c$/Users/tester/.ssh",
    "/c/Users/tester/.ssh",                      # Git Bash, WSL and Cygwin spellings
    "/mnt/c/Users/tester/.ssh",
    "/cygdrive/c/Users/tester/.ssh",
]


@pytest.mark.parametrize("spelling", SSH_SPELLINGS)
def test_no_spelling_hides_a_sensitive_path(spelling):
    assert sandbox.is_sensitive_path(spelling, HOME) is True


@pytest.mark.parametrize("spelling", SSH_SPELLINGS)
def test_no_spelling_gets_a_sensitive_path_through_as_a_mount(spelling):
    assert mp([spelling]) != []


@pytest.mark.parametrize("spelling", SSH_SPELLINGS)
def test_no_spelling_gets_a_sensitive_path_through_as_a_docker_volume(spelling):
    block = good()
    block["docker_volumes"] = [spelling + ":/root/x:ro"]
    found = problems_of(block)
    assert found and all(p.startswith("docker_volumes entry") for p in found), found


@pytest.mark.parametrize("spelling", [
    "C:\\Users\\tester\\.ssh::$INDEX_ALLOCATION",   # an NTFS stream name after the file name
    "C:\\Users\\tester\\.ssh\\id_rsa:Zone.Identifier",
])
def test_an_ntfs_stream_suffix_does_not_hide_a_sensitive_path(spelling):
    assert sandbox.is_sensitive_path(spelling, HOME) is True
    assert mp([spelling]) != []


def test_a_device_prefix_and_trailing_dots_are_the_same_path_for_the_allow_rule():
    assert mp(["\\\\?\\C:\\work\\wt\\src"]) == []
    assert mp(["//?/C:/work/wt/src"]) == []
    assert mp(["C:\\work\\wt.\\src"]) == []      # Win32 reads 'wt.' as 'wt'
    assert mp(["C:\\work\\wt \\src"]) == []


def test_an_administrative_share_is_denied_as_the_local_drive_but_never_accepted_as_the_worktree():
    found = mp(["\\\\localhost\\c$\\work\\wt\\src"])
    assert len(found) == 1 and "outside the worktree" in found[0]
    assert sandbox.is_sensitive_path("\\\\anyhost\\c$\\Users\\tester\\.aws", HOME) is True
    assert sandbox.is_sensitive_path("\\\\anyhost\\share\\Users\\tester\\.aws", HOME) is False


def test_a_double_slash_posix_path_is_not_a_share():
    assert sandbox.mount_problems(["//work/wt/src"], "/work/wt", POSIX_HOME) == []
    assert sandbox.is_sensitive_path("//home/tester/.ssh/id_rsa", POSIX_HOME) is True


@pytest.mark.parametrize("volume,needle", [
    ("/var/run/docker.sock:/var/run/docker.sock", "container-engine socket"),
    ("//var/run/docker.sock:/var/run/docker.sock", "container-engine socket"),
    ("/run/docker.sock:/x:ro", "container-engine socket"),
    ("/var/run/podman/podman.sock:/x", "container-engine socket"),
    ("/somewhere/else/engine.sock:/x", "container-engine socket"),
    ("C:\\pipes\\docker_engine:/x", "container-engine socket"),
    ("C:\\pipes\\dockerDesktopLinuxEngine:/x", "container-engine socket"),
    ("//./pipe/docker_engine://./pipe/docker_engine", "not an absolute path"),
    ("\\\\.\\pipe\\docker_engine:/x", "not an absolute path"),
    ("/var/run:/x", "parent of /var/run/docker.sock"),
    ("/run:/x", "parent of /run/docker.sock"),
    ("/var:/x", "is a parent of"),
    ("/var/run/podman:/x", "lies inside /var/run/podman"),
    ("/run/podman/sub:/x", "lies inside /run/podman"),
    ("/var/lib/docker:/x", "lies inside /var/lib/docker"),
    ("/var/lib/docker/volumes:/x", "lies inside /var/lib/docker"),
    ("/etc:/x:ro", "lies inside /etc"),
    ("/etc/ssl/certs:/x:ro", "lies inside /etc"),
    ("/root:/x", "lies inside /root"),
    ("/root/.ssh:/x", "lies inside /root"),
    ("/proc:/x", "lies inside /proc"),
    ("/sys/fs/cgroup:/x", "lies inside /sys"),
    ("/dev:/x", "lies inside /dev"),
    ("/dev/sda:/x", "lies inside /dev"),
    ("/boot:/x", "lies inside /boot"),
])
def test_the_container_engine_and_system_directories_are_never_a_worker_mount(volume, needle):
    block = good()
    block["docker_volumes"] = [volume]
    found = problems_of(block)
    assert found and all(p.startswith("docker_volumes entry") for p in found), found
    assert any(needle in p for p in found), found


def test_system_paths_and_sockets_are_refused_as_mounts_and_worktrees_too():
    for path, fragment in (
        ("/var/run/docker.sock", "container-engine socket"), ("/etc", "lies inside /etc"),
        ("/etc/passwd", "lies inside /etc"), ("/var", "is a parent of"),
    ):
        found = sandbox.mount_problems([path], "/work/wt", POSIX_HOME)
        assert len(found) == 1 and fragment in found[0], (path, found)
    for worktree in ("/etc/wt", "/var/run/docker.sock", "/dev/shm/wt", "/root/wt", "/proc/1"):
        with pytest.raises(SandboxConfigError):
            sandbox.docker_run_argv(POLICY, worktree, "x", home=POSIX_HOME)


def test_ordinary_absolute_directories_are_still_fine_as_volumes():
    for volume in ("/srv/cache:/cache", "/var/cache/pip:/root/.cache/pip:ro", "/opt/toolchain:/opt/tc:ro",
                   "/usr/share/zoneinfo:/usr/share/zoneinfo:ro", "/tmp/x:/y", "/data:/data"):
        block = good()
        block["docker_volumes"] = [volume]
        assert problems_of(block) == [], volume


@pytest.mark.parametrize("args", [
    ["--user", "00"], ["--user", "0000:0"], ["--user", "+0"], ["--user", "-0"], ["--user", "Root"],
    ["--user", "root:root"], ["--user", " root "], ["-u", "0:1000"], ["--user=00"], ["--user", ""], ["--user", ":1000"],
])
def test_every_spelling_of_root_is_refused_as_a_user(args):
    block = good()
    block["docker_extra_args"] = args
    found = problems_of(block)
    assert len(found) == 1 and "the policy does not allow" in found[0], found


@pytest.mark.parametrize("args", [["--user", "1000:0"], ["--user", "10"], ["--user", "nobody"], ["-u", "node:node"]])
def test_a_non_root_user_is_fine_even_with_the_root_group(args):
    block = good()
    block["docker_extra_args"] = args
    assert problems_of(block) == []


@pytest.mark.parametrize("value", ["\u0663", "+5", " 5", "5 ", "0x10", "1e2", "\uff15", ""])
def test_a_pids_limit_must_be_plain_ascii_digits(value):
    block = good()
    block["docker_extra_args"] = ["--pids-limit", value]
    found = problems_of(block)
    assert len(found) == 1 and found[0].isascii(), found


def test_a_pids_limit_with_leading_zeros_is_still_a_number_within_the_policy():
    block = good()
    block["docker_extra_args"] = ["--pids-limit", "0512"]
    assert problems_of(block) == []
    block["docker_extra_args"] = ["--pids-limit", "0513"]
    assert len(problems_of(block)) == 1


# ---------------------------------------------------------------------------------------------------------------
# Odds and ends: identifiers must match whole, checks must not mutate, the standing character rule
# ---------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["CI\n", "TZ\r\n", "A\n"])
def test_an_env_name_with_a_trailing_newline_is_not_a_variable_name(name):
    with pytest.raises(SandboxConfigError, match="variable name"):
        argv(env={name: "x"})


@pytest.mark.parametrize("name", ["ases-1\n", "n\n"])
def test_a_container_name_with_a_trailing_newline_raises(name):
    with pytest.raises(SandboxConfigError, match="container name"):
        argv(container_name=name)
    with pytest.raises(SandboxConfigError):
        sandbox.remove_container_command(name)


def test_an_image_with_a_trailing_newline_is_not_a_reference():
    assert sandbox.image_present("python:3\n", FakeDocker()) is False
    with pytest.raises(SandboxConfigError):
        sandbox.pull_command("python:3\n")
    with pytest.raises(SandboxConfigError):
        SandboxPolicy(image=IMAGE, forward_env=("TZ\n",))
    block = good()
    block["docker_image"] = "python:3\n"
    assert problems_of(block) == []   # whitespace around a config value is stripped, as Hermes's YAML would not
    block["docker_image"] = "py\nthon:3"
    assert "not a valid image reference" in problems_of(block)[0]


def test_check_terminal_block_does_not_change_its_input():
    import copy

    block = good()
    block["docker_extra_args"] = ["--pids-limit", "64"]
    block["docker_volumes"] = ["C:\\cache:/c"]
    block["docker_env"] = {"DEBUG": "1"}
    before = copy.deepcopy(block)
    problems_of(block)
    assert block == before


def test_the_terminal_block_a_policy_produces_is_not_shared_between_calls():
    first = sandbox.terminal_block(POLICY)
    first["backend"] = "local"
    assert sandbox.terminal_block(POLICY)["backend"] == "docker"


def test_a_unc_share_is_a_root():
    assert mp(["\\\\server\\share"]) == ["mount \\\\server\\share is a drive root"]
    assert "outside the worktree" in mp(["\\\\server\\share\\dir"])[0]


# ---------------------------------------------------------------------------------------------------------------
# sandbox_command_runner (round 9, ASES-QG-04, ASES-SEC-03/05/06/07): the `runner` hook gates.run_gate takes
# ---------------------------------------------------------------------------------------------------------------


def commands_run(handler):
    """A FakeDocker `run` handler that answers by the LAST word of the `sh -lc <command>` argv (the command
    string itself), so a test can script pass/fail per command without caring about the rest of the argv."""
    def inner(argv):
        return handler(argv[-1])
    return inner


def test_sandbox_command_runner_runs_one_docker_run_per_command_with_only_the_worktree_mounted(worktree, monkeypatch):
    monkeypatch.setattr(sandbox, "host_user_spec", lambda: None)
    docker = FakeDocker(run=commands_run(lambda cmd: result(0, f"ran {cmd}")))
    run = sandbox.sandbox_command_runner(POLICY, process_runner=docker)

    passed, output = run(worktree, ["pytest -q", "ruff check"], 60)

    assert passed is True
    assert "ran pytest -q" in output and "ran ruff check" in output
    assert len(docker.runs) == 2
    for call in docker.runs:
        assert pair(call, "--network") == "none"
        assert call[call.index("--mount") + 1].startswith(f"type=bind,source={worktree}")
        assert "-e" not in call  # no environment is ever forwarded
        assert call[-3:-1] == ["sh", "-lc"]


def test_sandbox_command_runner_defaults_to_no_network_and_grants_it_only_when_asked(worktree, monkeypatch):
    monkeypatch.setattr(sandbox, "host_user_spec", lambda: None)

    off = FakeDocker(run=commands_run(lambda cmd: result(0, "")))
    sandbox.sandbox_command_runner(POLICY, process_runner=off)(worktree, ["x"], 60)
    assert pair(off.runs[0], "--network") == "none"

    on = FakeDocker(run=commands_run(lambda cmd: result(0, "")))
    sandbox.sandbox_command_runner(POLICY, network=True, process_runner=on)(worktree, ["x"], 60)
    assert pair(on.runs[0], "--network") == "bridge"


def test_sandbox_command_runner_uses_the_host_user_when_the_platform_has_one(worktree, monkeypatch):
    monkeypatch.setattr(sandbox, "host_user_spec", lambda: "1000:1000")
    docker = FakeDocker(run=commands_run(lambda cmd: result(0, "")))

    sandbox.sandbox_command_runner(POLICY, process_runner=docker)(worktree, ["x"], 60)

    assert pair(docker.runs[0], "--user") == "1000:1000"


def test_sandbox_command_runner_stops_at_the_first_failing_command(worktree, monkeypatch):
    monkeypatch.setattr(sandbox, "host_user_spec", lambda: None)
    docker = FakeDocker(run=commands_run(
        lambda cmd: result(0, "first ok") if cmd == "first" else result(1, "", "boom")
    ))
    run = sandbox.sandbox_command_runner(POLICY, process_runner=docker)

    passed, output = run(worktree, ["first", "second", "third"], 60)

    assert passed is False
    assert "first ok" in output and "boom" in output and "[exit 1]" in output
    assert "third" not in output
    assert len(docker.runs) == 2  # third never ran


def test_sandbox_command_runner_a_timeout_is_a_red_result_with_a_clear_line(worktree, monkeypatch):
    monkeypatch.setattr(sandbox, "host_user_spec", lambda: None)

    def timing_out(argv):
        return result(sandbox.RC_TIMEOUT, "", "timed out after 5s")

    run = sandbox.sandbox_command_runner(POLICY, process_runner=FakeDocker(run=timing_out))

    passed, output = run(worktree, ["sleep 999"], 5)

    assert passed is False and "[TIMEOUT after 5s]" in output


def test_sandbox_command_runner_raises_infrastructure_error_when_docker_is_unavailable(worktree):
    docker = FakeDocker(info=result(127, "", "docker: command not found"))
    run = sandbox.sandbox_command_runner(POLICY, process_runner=docker)

    with pytest.raises(SandboxInfrastructureError, match="docker CLI not found"):
        run(worktree, ["x"], 60)

    assert docker.runs == []  # never attempted a docker run it could not have started


def test_sandbox_command_runner_raises_infrastructure_error_when_the_image_is_missing(worktree):
    docker = FakeDocker(inspect=result(1, "", "no such image"))
    run = sandbox.sandbox_command_runner(POLICY, process_runner=docker)

    with pytest.raises(SandboxInfrastructureError, match=re.escape(IMAGE)):
        run(worktree, ["x"], 60)

    assert docker.runs == []


def test_sandbox_command_runner_infrastructure_error_never_a_silent_pass_or_host_fallback(worktree, monkeypatch):
    """Docker down must raise, never return (True, ...) and never run the command on the host instead."""
    monkeypatch.setattr(sandbox.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError(
        "the host must never run a gate command when the sandbox is enabled"
    )))
    docker = FakeDocker(info=result(1, "", "daemon not running"))
    run = sandbox.sandbox_command_runner(POLICY, process_runner=docker)

    with pytest.raises(SandboxInfrastructureError):
        run(worktree, ["echo hi"], 60)


# ---------------------------------------------------------------------------------------------------------------
# Finding 1 (round 12, ASES-CFG-04/ASES-CFG-05): the gate path's own docker-launching subprocesses must not
# inherit the controller's whole environment (a credential-shaped variable, or a DOCKER_HOST/DOCKER_CONTEXT/
# DOCKER_TLS_VERIFY/DOCKER_CERT_PATH override that would silently redirect the sandbox to a different daemon).
# ---------------------------------------------------------------------------------------------------------------


def test_gate_docker_env_drops_credentials_and_docker_context_overrides_but_keeps_everything_else(monkeypatch):
    monkeypatch.setenv("HERMES_PROVIDER_API_KEY", "sk-super-secret-value-12345")
    monkeypatch.setenv("DOCKER_HOST", "tcp://unexpected-remote-daemon.example:2375")
    monkeypatch.setenv("DOCKER_CONTEXT", "some-other-context")
    monkeypatch.setenv("DOCKER_TLS_VERIFY", "1")
    monkeypatch.setenv("DOCKER_CERT_PATH", "C:/some/certs")
    monkeypatch.setenv("ASES_GATE_DOCKER_ENV_PROBE", "kept")

    env = sandbox._gate_docker_env()

    for dropped in ("HERMES_PROVIDER_API_KEY", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY",
                    "DOCKER_CERT_PATH"):
        assert dropped not in env
    assert env.get("ASES_GATE_DOCKER_ENV_PROBE") == "kept"
    assert env.get("PATH") == os.environ.get("PATH")  # not just emptied: an ordinary, non-credential var survives


def test_gate_docker_runner_calls_default_runner_with_the_scrubbed_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-shouldnotbevisible0123456789")
    seen = {}

    def fake_default_runner(argv, timeout, *, env=None):
        seen.update(argv=list(argv), timeout=timeout, env=env)
        return result(0, "ok")

    monkeypatch.setattr(sandbox, "default_runner", fake_default_runner)

    out = sandbox._gate_docker_runner(["docker", "info"], 20)

    assert out.stdout == "ok"
    assert seen["argv"] == ["docker", "info"] and seen["timeout"] == 20
    assert "OPENROUTER_API_KEY" not in seen["env"]


def test_sandbox_command_runner_default_process_runner_scrubs_credentials_and_docker_context_overrides(
    monkeypatch, worktree,
):
    """Finding 1's own reproduction method (r12_audit_findings.md): plant a credential-shaped variable and a
    DOCKER_HOST override in the parent process, call the real sandbox_command_runner with NO process_runner
    override -- exactly gates.resolve_runner's own production call shape -- and check what env the docker CLI
    process (docker_available, image_present and the actual `docker run`) actually received. Before this fix
    every one of the three saw env=None (subprocess.run's "inherit everything"), including the planted values."""
    monkeypatch.setattr(sandbox, "host_user_spec", lambda: None)
    monkeypatch.setenv("HERMES_PROVIDER_API_KEY", "sk-super-secret-value-12345")
    monkeypatch.setenv("DOCKER_HOST", "tcp://unexpected-remote-daemon.example:2375")
    seen_envs = []

    def fake_default_runner(argv, timeout, *, env=None):
        seen_envs.append(env)
        sub = argv[1]
        if sub == "info":
            return result(0, "27.0.1\n")
        if sub == "image":
            return result(0, "sha256:abc\n")
        return result(0, "ran")

    monkeypatch.setattr(sandbox, "default_runner", fake_default_runner)

    run = sandbox.sandbox_command_runner(POLICY)  # no process_runner override: the real production default
    passed, _ = run(worktree, ["echo hi"], 60)

    assert passed is True
    assert len(seen_envs) == 3  # docker info, docker image inspect, docker run
    for env in seen_envs:
        assert env is not None, "the default process runner must not inherit the parent's whole environment"
        assert "HERMES_PROVIDER_API_KEY" not in env
        assert "DOCKER_HOST" not in env


def test_default_runner_itself_is_unchanged_and_still_inherits_when_no_env_is_given():
    """The fix wraps sandbox_command_runner's default `process_runner`; it must not touch default_runner itself,
    which key_visibility_test and exfiltration_probe still call directly with env=None on purpose (see
    default_runner's own docstring): their whole point is to see what a real, unscrubbed environment would leak
    through a mis-built `docker run`."""
    import inspect

    params = inspect.signature(sandbox.default_runner).parameters
    assert params["env"].default is None


def test_the_sandbox_files_contain_neither_the_em_dash_nor_the_section_sign():
    """The owner's standing rule for code, comments, docstrings, tests and strings."""
    banned = (chr(0x2014), chr(0xA7))
    for path in (pathlib.Path(sandbox.__file__), pathlib.Path(__file__)):
        text = path.read_text(encoding="utf-8")
        for char in banned:
            assert char not in text, f"{path.name} contains U+{ord(char):04X}"
