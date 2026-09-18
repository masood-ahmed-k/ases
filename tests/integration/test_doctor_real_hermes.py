"""Runs against the REAL local hermes.exe (not a provider, so this doesn't violate ASES-TST-01 -- it's
a local tool-presence check, not a network call to a model). Skips cleanly if hermes isn't on PATH."""
import shutil

import pytest

from ases import hermes

pytestmark = pytest.mark.skipif(shutil.which("hermes") is None, reason="hermes not on PATH")


def test_real_hermes_version_parses():
    version = hermes.hermes_version()
    assert version is not None
    assert version.count(".") == 2


def test_real_hermes_doctor_runs():
    result = hermes.run_doctor()
    assert result.exit_code is not None


def test_real_gateway_status_runs():
    status = hermes.gateway_status()
    assert isinstance(status.running, bool)
