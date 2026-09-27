"""Tests for spec/check_requirements.py: --docx / ASES_BLUEPRINT_DOCX / DEFAULT_DOCX precedence and
the not-found error message (HK-PATH, round 8).

spec/ is not on pythonpath (only src/ is, per pyproject.toml), so the module under test is loaded
directly from its file path rather than imported by dotted name.

These tests exercise path resolution only, never Appendix F extraction from a real docx: every path
used here is a tmp_path file that is never created, so FileNotFoundError is the expected, fast result.
"""
import importlib.util
import pathlib

import pytest

_MODULE_PATH = pathlib.Path(__file__).resolve().parents[2] / "spec" / "check_requirements.py"

_ENV_VAR = "ASES_BLUEPRINT_DOCX"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_requirements_under_test", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def cr(monkeypatch):
    monkeypatch.delenv(_ENV_VAR, raising=False)
    return _load_module()


def test_default_docx_points_at_the_new_aises_folder(cr):
    assert cr.DEFAULT_DOCX == pathlib.Path(
        r"C:\Users\masoo\OneDrive\Desktop\AISES\ASES_Swarm_Implementation_Blueprint_v1.2.docx"
    )


def test_resolve_docx_path_uses_default_when_nothing_else_given(cr):
    assert cr.resolve_docx_path(None) == cr.DEFAULT_DOCX


def test_resolve_docx_path_uses_env_var_when_no_cli_flag(cr, monkeypatch):
    monkeypatch.setenv(_ENV_VAR, r"D:\somewhere\blueprint.docx")
    assert cr.resolve_docx_path(None) == pathlib.Path(r"D:\somewhere\blueprint.docx")


def test_resolve_docx_path_cli_flag_wins_over_env_var(cr, monkeypatch):
    monkeypatch.setenv(_ENV_VAR, r"D:\somewhere\blueprint.docx")
    cli_path = pathlib.Path(r"E:\explicit\blueprint.docx")
    assert cr.resolve_docx_path(cli_path) == cli_path


def test_resolve_docx_path_cli_flag_wins_over_default(cr):
    cli_path = pathlib.Path(r"E:\explicit\blueprint.docx")
    assert cr.resolve_docx_path(cli_path) == cli_path


def test_empty_env_var_falls_back_to_default(cr, monkeypatch):
    monkeypatch.setenv(_ENV_VAR, "")
    assert cr.resolve_docx_path(None) == cr.DEFAULT_DOCX


def test_not_found_error_names_the_path_and_the_overrides(cr, tmp_path):
    missing = tmp_path / "nope.docx"
    with pytest.raises(FileNotFoundError) as excinfo:
        cr.extract_appendix_f(missing)
    message = str(excinfo.value)
    assert str(missing) in message
    assert "--docx" in message
    assert _ENV_VAR in message


def test_main_check_reports_missing_docx_from_cli_flag(cr, tmp_path, capsys):
    missing = tmp_path / "nope.docx"
    exit_code = cr.main(["--check", "--docx", str(missing)])
    assert exit_code == 2
    captured = capsys.readouterr()
    assert str(missing) in captured.err
    assert "--docx" in captured.err
    assert _ENV_VAR in captured.err


def test_main_check_falls_back_to_env_var_when_no_cli_flag(cr, tmp_path, monkeypatch, capsys):
    missing = tmp_path / "env-nope.docx"
    monkeypatch.setenv(_ENV_VAR, str(missing))
    exit_code = cr.main(["--check"])
    assert exit_code == 2
    captured = capsys.readouterr()
    assert str(missing) in captured.err


def test_main_check_cli_flag_overrides_env_var(cr, tmp_path, monkeypatch, capsys):
    env_missing = tmp_path / "env-nope.docx"
    cli_missing = tmp_path / "cli-nope.docx"
    monkeypatch.setenv(_ENV_VAR, str(env_missing))
    exit_code = cr.main(["--check", "--docx", str(cli_missing)])
    assert exit_code == 2
    captured = capsys.readouterr()
    assert str(cli_missing) in captured.err
    assert str(env_missing) not in captured.err
