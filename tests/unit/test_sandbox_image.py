"""SANDBOXIMG (round 15): lexical checks over the pinned sandbox image build (docker/sandbox/Dockerfile) and the
script that runs the real Docker probes (scripts/sandbox_live_check.py). No Docker here: these read the files as
text, so they run on every machine, with or without Docker installed, unlike scripts/sandbox_live_check.py
itself (real Docker, not collected by pytest, see its own module docstring). One test below DOES import and run
a function from the live-check script (its own _rmtree_retry, a pure filesystem helper), because that one is
worth a real regression test: see its own docstring."""
import importlib.util
import os
import pathlib
import re
import stat

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_DOCKERFILE = _ROOT / "docker" / "sandbox" / "Dockerfile"
_LIVE_CHECK = _ROOT / "scripts" / "sandbox_live_check.py"


def _dockerfile_text() -> str:
    return _DOCKERFILE.read_text(encoding="utf-8")


def _load_live_check():
    """Imports scripts/sandbox_live_check.py as a module without needing it on sys.path or PYTHONPATH (the
    script is not part of the ases package, and pytest's own testpaths never collects it)."""
    spec = importlib.util.spec_from_file_location("sandbox_live_check", _LIVE_CHECK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_dockerfile_exists_and_is_ascii():
    assert _DOCKERFILE.is_file()
    text = _dockerfile_text()
    assert text.isascii()


def test_the_base_image_is_pinned_by_a_full_sha256_digest_not_a_mutable_tag():
    text = _dockerfile_text()
    from_lines = [line for line in text.splitlines() if line.strip().upper().startswith("FROM ")]
    assert len(from_lines) == 1, "exactly one FROM in a single-stage build"
    from_line = from_lines[0]
    assert "@sha256:" in from_line, f"the base image must be pinned by digest: {from_line!r}"
    digest = re.search(r"@sha256:([0-9a-f]{64})\b", from_line)
    assert digest is not None, f"not a full 64-character sha256 digest: {from_line!r}"
    assert ":latest" not in from_line


def test_git_and_pytest_are_each_pinned_to_an_exact_version():
    text = _dockerfile_text()
    # apt: "package=version", never a bare "git" (which would float to whatever is current on rebuild). Matched
    # from an actual RUN line, not a mention of "apt-get install" in a comment above it.
    apt_install = re.search(r"^RUN apt-get install[^\n]*|^\s+&& apt-get install[^\n]*", text, re.MULTILINE)
    assert apt_install is not None
    assert re.search(r"\bgit=\S+", apt_install.group(0)), "git must be pinned with package=version"
    # pip: "pytest==x.y.z", never a bare "pytest" or a floor ("pytest>="). Matched from an actual RUN line.
    pip_install = re.search(r"^RUN pip install[^\n]*", text, re.MULTILINE)
    assert pip_install is not None
    assert re.search(r"\bpytest==\d+\.\d+(\.\d+)?\b", pip_install.group(0)), "pytest must be pinned with =="


def test_the_image_tag_used_elsewhere_is_not_latest():
    """Matches sandbox._unpinned_reason's own rule (a tag other than latest, or an @sha256 digest, counts as
    pinned): the tag this Dockerfile is built as, and that config/swarm.yaml names, must not be 'latest'."""
    from ases import config as config_mod
    from ases import sandbox as sandbox_mod

    cfg = config_mod.load_swarm_config(_ROOT / "config" / "swarm.yaml")
    image = cfg.sandbox["image"]
    assert sandbox_mod._unpinned_reason(image) is None
    assert not image.endswith(":latest")


def test_the_image_runs_as_a_fixed_non_root_user():
    """ASES-SEC-03 wants the host user; docker_run_argv leaves --user unset on native Windows
    (sandbox.host_user_spec returns None there), so the image's OWN default user is what actually runs a
    sandboxed command on this machine. It must not be root."""
    text = _dockerfile_text()
    assert re.search(r"^USER\s+\S+", text, re.MULTILINE), "no USER directive: the image would run as root"
    user_line = re.search(r"^USER\s+(\S+)", text, re.MULTILINE).group(1)
    assert user_line not in ("root", "0"), f"the image must not run as root: USER {user_line}"


def test_the_dockerfile_never_uses_pull_or_add_remote_or_curl_at_build_time():
    """Nothing in this project starts Docker or downloads content on the user's behalf without an explicit,
    reviewable command (section 16, stop-condition actions; sandbox.py's own module docstring says the same
    about pull_command). The Dockerfile's RUN lines may use apt-get/pip against the pinned base's own configured
    sources, but never curl/wget/ADD-from-a-URL to fetch something outside that."""
    text = _dockerfile_text()
    assert "curl" not in text.lower()
    assert "wget" not in text.lower()
    assert not re.search(r"^ADD\s+https?://", text, re.MULTILINE | re.IGNORECASE)


def test_the_live_check_script_exists_and_never_writes_to_the_real_test_repo():
    """scripts/sandbox_live_check.py must never pass the real test-repo-phase3 path to a git command other than
    `clone` (which only reads), and never as run_gate's own repo_path (whose checkout step can write to a
    repository's .git/worktrees in host mode). A real swarm run can be using that repository at the same time
    this script runs (round 15's own instruction)."""
    assert _LIVE_CHECK.is_file()
    text = _LIVE_CHECK.read_text(encoding="utf-8")
    assert text.isascii()

    # Every _run_git(...) and run_gate(...) CALL site (never a "def _run_git(" / "def run_gate(" definition), as
    # its own text block: the text from that call's opening paren up to the next call's start, which for this
    # script's simple, single-line-argument style is enough to tell what each call's arguments reference.
    call_starts = [
        m.start() for m in re.finditer(r"\b(_run_git|run_gate)\(", text)
        if not text[: m.start()].rstrip().endswith("def")
    ]
    assert call_starts, "expected at least one _run_git and one run_gate call"
    chunks = []
    for index, start in enumerate(call_starts):
        end = call_starts[index + 1] if index + 1 < len(call_starts) else len(text)
        chunks.append(text[start:end])

    source_uses = [c for c in chunks if "TEST_REPO_SOURCE" in c]
    assert len(source_uses) == 1, f"TEST_REPO_SOURCE must be used in exactly one call, found {len(source_uses)}"
    assert source_uses[0].startswith("_run_git("), "the one use of TEST_REPO_SOURCE must be a _run_git call"
    assert '"clone"' in source_uses[0], "the one _run_git call against TEST_REPO_SOURCE must be a clone"

    run_gate_calls = [c for c in chunks if c.startswith("run_gate(")]
    assert run_gate_calls, "expected at least one run_gate call"
    for call in run_gate_calls:
        assert "TEST_REPO_SOURCE" not in call
        assert "clone_dir" in call, "every run_gate call must use the local clone, not the real repository"


def test_the_dockerfile_and_live_check_contain_neither_the_em_dash_nor_the_section_sign():
    banned = (chr(0x2014), chr(0xA7))
    for path in (_DOCKERFILE, _LIVE_CHECK, pathlib.Path(__file__)):
        text = path.read_text(encoding="utf-8")
        for char in banned:
            assert char not in text, f"{path.name} contains banned character {char!r}"


def test_rmtree_retry_removes_a_read_only_file_like_a_real_git_clone_leaves_behind(tmp_path):
    """Real bug, found by actually running scripts/sandbox_live_check.py against real Docker twice in a row:
    git marks every loose object file read-only (mode 0444) on every platform, including inside a clone this
    script makes of its own local clone. Windows honours that bit for delete, unlike POSIX, so a plain
    shutil.rmtree(ignore_errors=True) (the first version of this fix) silently left the directory behind,
    and the SECOND run of the script then failed with a confusing `git clone`: "destination path ... already
    exists" instead of a clear message about the real cause. This reproduces the read-only file without
    Docker or git: a single read-only file inside a directory is exactly what defeated the naive rmtree."""
    live_check = _load_live_check()

    target = tmp_path / "looks-like-a-git-clone"
    locked = target / "objects" / "ab" / "cdefabcdef0123456789"
    locked.parent.mkdir(parents=True)
    locked.write_text("pretend loose object", encoding="utf-8")
    os.chmod(locked, stat.S_IREAD)

    live_check._rmtree_retry(target)

    assert not target.exists()


def test_rmtree_retry_retries_when_shutil_rmtree_itself_raises_not_just_when_the_dir_survives(monkeypatch, tmp_path):
    """Round 15 audit finding (SANDBOXIMG): a prior version of _rmtree_retry called shutil.rmtree with no
    try/except around it. The read-only case above never exercises this, because _clear_readonly_and_retry's
    own single retry (chmod then re-run the failed op once) succeeds immediately, so shutil.rmtree never
    raises there. The genuinely transient case the docstring names (a file a Docker bind-mount teardown has
    not yet released) is different: that second attempt inside the onerror hook can ALSO fail, and per
    shutil.rmtree's documented contract that failure propagates straight out of shutil.rmtree itself. Without
    a try/except around the shutil.rmtree(...) call, that propagated straight out of _rmtree_retry too, on
    attempt 1 of 5, before any sleep(delay) ever ran. This reproduces that without a real file lock: a fake
    shutil.rmtree that raises OSError on its first two calls (simulating the lock not yet released) and only
    then succeeds, deleting the directory, on the third."""
    live_check = _load_live_check()

    target = tmp_path / "looks-like-a-git-clone"
    target.mkdir()

    real_rmtree = live_check.shutil.rmtree
    calls = {"count": 0}
    sleeps = []

    def fake_rmtree(path, onerror=None):
        calls["count"] += 1
        if calls["count"] < 3:
            raise OSError("simulated: still locked by an in-progress Docker bind-mount teardown")
        real_rmtree(path, onerror=onerror)

    monkeypatch.setattr(live_check.shutil, "rmtree", fake_rmtree)
    monkeypatch.setattr(live_check.time, "sleep", lambda seconds: sleeps.append(seconds))

    live_check._rmtree_retry(target, attempts=5, delay=1.0)

    assert not target.exists()
    assert calls["count"] == 3, "must actually retry the shutil.rmtree call itself after it raises"
    assert len(sleeps) == 2, "must back off with sleep(delay) between failed attempts, not fail on attempt 1"


def test_rmtree_retry_exhausts_every_attempt_before_raising_the_final_error(monkeypatch, tmp_path):
    """Companion case: when the lock never releases, _rmtree_retry must still run all `attempts` tries (with a
    sleep(delay) between each) before giving up, not fail fast on the first shutil.rmtree exception -- and the
    final error it raises should carry the real underlying cause, not just a bare "could not remove" message."""
    live_check = _load_live_check()

    target = tmp_path / "looks-like-a-git-clone"
    target.mkdir()

    calls = {"count": 0}
    sleeps = []

    def fake_rmtree(path, onerror=None):
        calls["count"] += 1
        raise OSError("simulated: permanently locked")

    monkeypatch.setattr(live_check.shutil, "rmtree", fake_rmtree)
    monkeypatch.setattr(live_check.time, "sleep", lambda seconds: sleeps.append(seconds))

    with pytest.raises(OSError, match="could not remove"):
        live_check._rmtree_retry(target, attempts=5, delay=1.0)

    assert calls["count"] == 5, "every attempt must actually call shutil.rmtree, not stop after the first"
    assert len(sleeps) == 4, "a sleep(delay) must happen between each of the 5 attempts, 4 gaps total"
