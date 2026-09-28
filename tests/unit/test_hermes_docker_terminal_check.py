"""HERMESDOCKER (round 16): lexical checks over scripts/hermes_docker_terminal_check.py. No Docker and no
Hermes here: these read the script as text (plus config/swarm.yaml, pure YAML) so they run on every
machine, in the plain ASES venv, unlike the live-check script itself (real Docker AND the Hermes install,
not collected by pytest, see its own module docstring). Style matches tests/unit/test_sandbox_image.py."""
import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_LIVE_CHECK = _ROOT / "scripts" / "hermes_docker_terminal_check.py"

# The four names the live-check script's own docstring says must only ever be read (stat/read_text/
# read_bytes/iterdir/sys.path), never written to. Kept here as a literal list, not imported from the
# script, so this test does not need to import anything Hermes- or Docker-shaped to check it.
_HERMES_HOME_IDENTIFIERS = ("HERMES_HOME_REAL", "HERMES_AGENT_DIR", "CODER1_PROFILE_DIR", "CODER1_CONFIG")

# Write-capable call markers. A line containing one of these is a write (or remove/rename), and must not
# also mention one of the four identifiers above as its target.
_WRITE_CALL_MARKERS = (
    "open(", ".write_text(", ".write_bytes(", "shutil.copy", "shutil.move(",
    "os.remove(", "os.rename(", "os.replace(", "os.makedirs(", ".mkdir(",
)


def _live_check_text() -> str:
    return _LIVE_CHECK.read_text(encoding="utf-8")


def test_the_script_exists_and_is_ascii():
    assert _LIVE_CHECK.is_file()
    text = _live_check_text()
    assert text.isascii()


def test_the_script_contains_neither_the_em_dash_nor_the_section_sign():
    banned = (chr(0x2014), chr(0xA7))
    for path in (_LIVE_CHECK, pathlib.Path(__file__)):
        text = path.read_text(encoding="utf-8")
        for char in banned:
            assert char not in text, f"{path.name} contains banned character {char!r}"


def test_the_script_never_references_the_real_test_repository_path():
    text = _live_check_text()
    assert "test-repo-phase3" not in text


def test_the_script_never_writes_under_the_hermes_home():
    """Every line that calls a write/remove/rename/makedirs-shaped function must not also name one of
    the four Hermes-home path constants: those constants are read-only in this script (stat, read_text,
    read_bytes, iterdir, sys.path insert), and any write goes to this script's own throwaway WORKDIR/
    FAKE_HOME instead. This is exactly what the live-check script's own module docstring claims; this
    test is the lexical proof of it."""
    text = _live_check_text()
    offending = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if any(marker in line for marker in _WRITE_CALL_MARKERS):
            for name in _HERMES_HOME_IDENTIFIERS:
                if name in line:
                    offending.append((lineno, name, line.strip()))
    assert not offending, f"write-shaped call references a Hermes-home constant: {offending}"


def test_the_script_never_shells_out_to_docker_run_itself():
    """The task is explicit: prove Hermes's OWN docker run, never a docker run this script builds
    itself. scripts/workergit_live_check.py legitimately builds `docker run`/`docker build` argv by
    hand (it is proving the mount design in isolation); this script must not -- it may only ever
    inspect or tear down what Hermes's own DockerEnvironment already started (docker inspect / stop /
    rm are fine)."""
    text = _live_check_text()
    assert re.search(r'\[\s*"docker"\s*,\s*"run"', text) is None, "must never construct a 'docker run' argv itself"
    assert re.search(r'\[\s*"docker"\s*,\s*"build"', text) is None, "must never build its own docker image"


def test_the_script_reads_the_pinned_image_from_swarm_config_rather_than_hardcoding_a_literal():
    """Matches sandbox._unpinned_reason's own rule and test_sandbox_image.py's own pattern: the pinned
    image name must come from config/swarm.yaml at run time (load_swarm_config + .sandbox["image"]),
    never a separate hardcoded string literal that could silently drift from it."""
    from ases import config as config_mod

    text = _live_check_text()
    assert "load_swarm_config" in text
    assert '.sandbox["image"]' in text or ".sandbox['image']" in text

    cfg = config_mod.load_swarm_config(_ROOT / "config" / "swarm.yaml")
    pinned = cfg.sandbox["image"]
    # The pinned name itself must not appear as a separate literal anywhere in the script: it is only
    # ever obtained dynamically, through the call checked above, never spelled out a second time.
    assert pinned not in text, f"the live-check script hardcodes the pinned image name {pinned!r} as a literal"


def test_the_script_scrubs_credential_shaped_env_vars_and_plants_a_fake_one():
    """Lexical proxy for "what to build" item 2: the script must scrub before driving Hermes, and the
    planted name must look like a credential (so it would trip Hermes's own forwarding/env checks the
    same way a real key would) without being one."""
    text = _live_check_text()
    assert "PLANTED_SECRET_NAME" in text
    assert "PLANTED_SECRET_VALUE" in text
    assert "scrub_and_plant_env" in text
