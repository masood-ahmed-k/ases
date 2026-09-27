"""Every command of swarm, through cli.main([...]), with the modules behind it faked.

The commands are thin: what they decide is which module to call, with what, in what order, what to print (ASCII
only: the Windows console is cp1252) and which exit code to end with. So each test fakes the module a command calls
(or puts a stub in sys.modules for the modules other packages own: questions, profiles, evals, hardening), runs
`cli.main`, and asserts on the calls, the console and the exit code. Real temp SQLite databases are used wherever a
command reads or writes the database, so the critic events, the project state and the kill switch flag are real.

Accented letters and emoji are built with chr() at run time: the Write tool decodes unicode escapes in a file, and
a non-ASCII character in a source file is exactly what the console rule forbids.
"""
import copy
import dataclasses
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

from ases import bounds, cli, config, controller, critic, db, doctor, events, guards, hermes, killswitch, ledger
from ases import models, reconcile

try:  # another package's module: imported here, before _profiles_stub/_stub swap a fake into sys.modules
    from ases import profiles as real_profiles
except ImportError:
    real_profiles = None

needs_profiles = pytest.mark.skipif(real_profiles is None, reason="ases.profiles is not built in this checkout yet")

ACCENT = "caf" + chr(0xE9)
EMOJI = chr(0x1F600)
ARROW = chr(0x2192)

ROLES = {"lead": "lead", "coder": "coder-1", "reviewer": "reviewer"}

PLAN_RAW = {
    "project": "t3",
    "integration_branch": "integration",
    "gate_profiles": {"trivial": ["echo ok"]},
    "tasks": [
        {"key": "T1", "title": "scaffold", "role": "coder", "depends_on": [], "touches": ["a.py"],
         "acceptance": ["exists"], "gate_profile": "trivial", "estimated_requests": 10},
        {"key": "T2", "title": "review scaffold", "role": "reviewer", "depends_on": ["T1"],
         "touches": [], "acceptance": ["reviewed"], "gate_profile": "trivial", "estimated_requests": 5},
    ],
}

# xkiro has no published daily cap and is paced at 10 requests a minute; openrouter has a 50 a day cap.
MODELS_CONFIG = {
    "providers": {
        "xkiro": {"data_policy": "no_training", "limits": {"rpm": 10}},
        "openrouter": {"limits": {"per_day_default": 50, "per_day_after_credits": 1000, "rpm": 20},
                       "credits_purchased": False},
    },
    "models": [
        {"provider": "xkiro", "model": "coder-model", "role_class": "coder", "pinned": True},
        {"provider": "openrouter", "model": "review-model", "role_class": "reviewer", "pinned": True},
    ],
}


def _project(tmp_path, **overrides):
    values = dict(
        name="ases", environment="native", data_class="public", workspace_root=tmp_path / "ws",
        ases_home=tmp_path / "home", board="b", integration_branch="integration", roles=dict(ROLES), concurrency={},
        budgets={"replans_per_project": 2, "max_cards": 40, "attempts_per_card": 3},
        hermes_tested_version="0.21.3", hermes_native_home=tmp_path / "hermes",
    )
    values.update(overrides)
    return config.ProjectConfig(**values)


def _stub(monkeypatch, name, **attrs):
    """Put a fake `ases.<name>` module where cli._lazy will find it."""
    module = types.ModuleType(f"ases.{name}")
    for key, value in attrs.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, f"ases.{name}", module)
    return module


@pytest.fixture
def world(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "docs" / "ases").mkdir(parents=True)
    plan_file = repo / "docs" / "ases" / "plan.json"
    plan_file.write_text(json.dumps(PLAN_RAW), encoding="utf-8")
    project = _project(tmp_path)
    monkeypatch.setattr(cli, "_load_project", lambda: project)
    monkeypatch.setattr(cli, "_load_models_config", lambda: copy.deepcopy(MODELS_CONFIG))
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    conn = db.connect(config.db_path(project))
    return types.SimpleNamespace(
        tmp=tmp_path, repo=repo, plan_file=plan_file, project=project, conn=conn,
        plan_hash=lambda: critic.plan_hash(plan_file),
    )


def _console(capsys):
    captured = capsys.readouterr()
    return captured.out, captured.err


def _fake_clock(*readings):
    """A time.monotonic that answers the readings in turn and then keeps answering the last one, so a stray caller
    (a library, a thread) can never exhaust it."""
    remaining = list(readings)
    return lambda: remaining.pop(0) if len(remaining) > 1 else remaining[0]


def _events(conn, kind):
    return [json.loads(row["payload"]) for row in conn.execute("SELECT payload FROM events WHERE kind = ?", (kind,))]


# ---------------------------------------------------------------------------------------------
# The plumbing: output, lazy imports, report directories, main
# ---------------------------------------------------------------------------------------------


def test_every_command_of_section_9_1_is_registered_and_none_is_left_unbuilt():
    choices = set(cli.build_parser()._subparsers._group_actions[0].choices)

    assert {"init", "doctor", "run", "plan", "approve", "status", "questions", "answer", "stop", "resume", "models",
            "eval", "report"} <= choices
    assert {"critique", "clean", "retention"} <= choices
    assert not hasattr(cli, "_NOT_BUILT_YET") and not hasattr(cli, "cmd_not_built_yet")


def test_ascii_escapes_non_ascii_and_makes_control_characters_visible():
    assert cli._ascii(f"{ACCENT} {ARROW} {EMOJI}") == r"caf\xe9 \u2192 \U0001f600"
    assert cli._ascii("a\x1b[31mred\x07") == r"a\x1b[31mred\x07"
    assert cli._ascii("line one\r\nline two\ttabbed") == "line one\nline two\ttabbed"  # CRLF is one newline
    assert cli._ascii("plain ascii, 100% fine") == "plain ascii, 100% fine"


def test_out_and_err_write_ascii_to_the_right_streams(capsys):
    cli._out(f"to stdout {ACCENT}")
    cli._err(f"to stderr {ARROW}")

    out, err = _console(capsys)
    assert out == "to stdout caf\\xe9\n"
    assert err == "to stderr \\u2192\n"


def test_output_survives_a_console_that_only_knows_cp1252(world, monkeypatch):
    """The Windows console is cp1252 with errors="strict": one arrow used to crash the whole command."""
    console = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict", write_through=True)
    monkeypatch.setattr(sys, "stdout", console)
    _stub(monkeypatch, "questions", list_questions=lambda board, plan, conn: [],
          format_questions=lambda found: f"{ARROW} {EMOJI} {ACCENT}")

    assert cli.main(["questions", "--repo", str(world.repo)]) == 0

    console.flush()
    assert console.buffer.getvalue().decode("cp1252").strip() == r"\u2192 \U0001f600 caf\xe9"


def test_lazy_returns_the_module_and_a_missing_one_is_one_line_and_exit_1(monkeypatch, capsys):
    from ases import plan as plan_mod

    assert cli._lazy("plan") is plan_mod
    monkeypatch.setitem(sys.modules, "ases.not_built_yet", None)  # `import ases.not_built_yet` raises ImportError

    with pytest.raises(cli._Exit) as caught:
        cli._lazy("not_built_yet")

    assert caught.value.code == 1
    _, err = _console(capsys)
    assert err.count("\n") == 1 and "'not_built_yet'" in err and "nothing was changed" in err


def test_a_command_that_needs_a_missing_module_says_so_and_exits_1_while_others_still_work(world, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "ases.hardening", None)

    assert cli.main(["retention"]) == 1

    _, err = _console(capsys)
    assert "'hardening'" in err and "Traceback" not in err
    assert cli.main(["models"]) == 0  # one missing module does not touch the other commands


class _FrozenClock(datetime):
    """datetime whose now() is always 2026-09-22 10:00:00 UTC, so two calls are 'in the same second' for certain."""

    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 9, 22, 10, 0, 0, tzinfo=tz)


def test_reports_dir_is_under_ases_home_by_kind_and_project_name_and_never_reused(world, monkeypatch):
    monkeypatch.setattr(cli, "datetime", _FrozenClock)

    first = cli._reports_dir(world.project, "reports")
    assert first == world.project.ases_home / "reports" / "ases" / "20260922T100000Z"  # no colons: a Windows name

    first.mkdir(parents=True)
    second = cli._reports_dir(world.project, "reports")
    assert second == world.project.ases_home / "reports" / "ases" / "20260922T100000Z-2"  # not the same directory
    second.mkdir()
    assert cli._reports_dir(world.project, "reports").name == "20260922T100000Z-3"

    assert cli._reports_dir(world.project, "stops").parent == world.project.ases_home / "stops" / "ases"


def test_reports_dir_uses_the_real_clock_in_the_documented_format(world):
    assert re.fullmatch(r"\d{8}T\d{6}Z", cli._reports_dir(world.project, "reports").name)


def test_reports_dir_makes_a_project_name_safe_as_one_path_segment(tmp_path):
    project = _project(tmp_path, name="../evil:name")

    directory = cli._reports_dir(project, "reports")

    assert directory.parent == tmp_path / "home" / "reports" / "___evil_name"
    assert tmp_path / "home" in directory.parents


def test_is_inside_answers_for_nested_sibling_and_missing_paths(tmp_path):
    (tmp_path / "repo").mkdir()

    assert cli._is_inside(tmp_path / "repo" / "out", tmp_path / "repo")
    assert cli._is_inside(tmp_path / "repo", tmp_path / "repo")
    assert not cli._is_inside(tmp_path / "repo-sibling", tmp_path / "repo")
    assert not cli._is_inside(tmp_path / "elsewhere", tmp_path / "repo")
    assert not cli._is_inside(tmp_path / "repo" / ".." / "elsewhere", tmp_path / "repo")


def test_main_reports_a_config_error_as_one_line_and_exit_2(monkeypatch, capsys):
    def broken():
        raise config.ConfigError("swarm.yaml is missing required key: project.name")

    monkeypatch.setattr(cli, "_load_project", broken)

    assert cli.main(["models"]) == 2
    assert _console(capsys)[1] == "config error: swarm.yaml is missing required key: project.name\n"


def test_main_turns_a_hermes_failure_into_one_line_and_exit_1(world, monkeypatch, capsys):
    def down(board, plan, *, conn):
        raise hermes.HermesCommandError(["kanban", "list"], 1, f"database is locked {ACCENT}")

    _stub(monkeypatch, "questions", list_questions=down, format_questions=lambda found: "")

    assert cli.main(["questions", "--repo", str(world.repo)]) == 1

    _, err = _console(capsys)
    assert "swarm questions: Hermes failed" in err and "database is locked caf\\xe9" in err
    assert "Traceback" not in err


def test_main_ends_a_ctrl_c_in_any_command_with_one_line_and_exit_130(world, monkeypatch, capsys):
    def interrupted(board, plan, *, conn):
        raise KeyboardInterrupt

    _stub(monkeypatch, "questions", list_questions=interrupted, format_questions=lambda found: "")

    assert cli.main(["questions", "--repo", str(world.repo)]) == 130

    assert _console(capsys)[1] == "interrupted (Ctrl-C)\n"


def test_a_missing_subcommand_is_an_argparse_error():
    with pytest.raises(SystemExit) as caught:
        cli.main([])

    assert caught.value.code == 2


def test_a_command_function_returns_the_code_of_an_early_exit_instead_of_raising(world, capsys):
    """cmd_* can be called directly (test_cli_run.py does): _Exit must not escape them."""
    world.plan_file.write_text("{ not json", encoding="utf-8")

    assert cli.cmd_status(types.SimpleNamespace(repo=str(world.repo))) == 1
    assert "Gate 0 FAILED:" in _console(capsys)[0]


# ---------------------------------------------------------------------------------------------
# questions, answer (ASES-REC-05)
# ---------------------------------------------------------------------------------------------


def _questions_stub(monkeypatch, *, found=None, answer=None):
    class QuestionError(Exception):
        pass

    calls = types.SimpleNamespace(listed=[], formatted=[], answered=[])

    def list_questions(board, plan, *, conn):
        calls.listed.append((board, plan.project, conn))
        return list(found if found is not None else [types.SimpleNamespace(card_id="t_1")])

    def format_questions(questions, now=None):
        calls.formatted.append(list(questions))
        return f"1. t_1 (task T1, work, asked just now) - Scaffold {ACCENT}\n    Which database?" if questions \
            else "No open questions."

    def answer_question(board, card_id, text, *, conn, author="user"):
        calls.answered.append((board, card_id, text, author))
        if answer is not None:
            return answer(board, card_id, text)
        return types.SimpleNamespace(
            card_id=card_id, title=f"Scaffold {ACCENT}", task_key="T1", card_kind="work",
            question="Which database?\nPostgres or SQLite?", asked_at=0,
        )

    module = _stub(monkeypatch, "questions", QuestionError=QuestionError, list_questions=list_questions,
                   format_questions=format_questions, answer_question=answer_question)
    return module, calls


def test_questions_lists_the_open_questions_of_the_plan_and_exits_0(world, monkeypatch, capsys):
    _, calls = _questions_stub(monkeypatch)

    assert cli.main(["questions", "--repo", str(world.repo)]) == 0

    (board, project, conn), = calls.listed
    assert (board, project) == ("b", "t3")
    out, _ = _console(capsys)
    assert "1. t_1 (task T1, work, asked just now) - Scaffold caf\\xe9" in out and "Which database?" in out
    assert out.isascii()


def test_questions_with_none_open_prints_the_empty_message_and_still_exits_0(world, monkeypatch, capsys):
    _questions_stub(monkeypatch, found=[])

    assert cli.main(["questions", "--repo", str(world.repo)]) == 0

    assert _console(capsys)[0] == "No open questions.\n"


def test_questions_needs_a_plan_that_passes_gate_0(world, monkeypatch, capsys):
    _, calls = _questions_stub(monkeypatch)
    world.plan_file.write_text(json.dumps({"project": "x"}), encoding="utf-8")

    assert cli.main(["questions", "--repo", str(world.repo)]) == 1

    assert calls.listed == []
    assert "Gate 0 FAILED:" in _console(capsys)[0]


def test_questions_against_the_real_module_lists_a_blocked_card(world, monkeypatch, capsys):
    """One test with the real questions module: the call signatures and the printed block are the real ones."""
    conn = world.conn
    conn.execute("INSERT INTO plan_tasks (project, task_key, work_card_id, merge_card_id, role, created_at) "
                 "VALUES ('t3', 'T1', 'w1', 'm1', 'coder', datetime('now'))")
    monkeypatch.setattr(hermes, "kanban_list", lambda board, status=None, assignee=None: (
        [{"id": "w1", "title": f"T1: scaffold {ACCENT}"}] if status == "blocked" else []))
    monkeypatch.setattr(hermes, "kanban_show", lambda board, card_id: {
        "id": card_id, "status": "blocked", "title": f"T1: scaffold {ACCENT}", "assignee": "coder-1",
        "_parents": [], "_children": [], "_runs": [], "_comments": [],
        "_events": [{"kind": "blocked", "payload": {"reason": "Which database?"}, "created_at": 1789832318,
                     "run_id": None}],
    })

    assert cli.main(["questions", "--repo", str(world.repo)]) == 0

    out, _ = _console(capsys)
    assert "w1" in out and "Which database?" in out and "T1" in out
    assert out.isascii()


def test_answer_prints_the_question_that_was_answered_and_the_word_answered(world, monkeypatch, capsys):
    _, calls = _questions_stub(monkeypatch)

    assert cli.main(["answer", "t_1", "Use SQLite, the file is enough"]) == 0

    assert calls.answered == [("b", "t_1", "Use SQLite, the file is enough", "user")]
    out, err = _console(capsys)
    assert "answered card t_1 (task T1, work): Scaffold caf\\xe9" in out
    assert "The question was:" in out and "    Which database?" in out and "    Postgres or SQLite?" in out
    assert out.rstrip().endswith("answered") and err == ""


def test_answer_never_echoes_the_answer_text_back(world, monkeypatch, capsys):
    _questions_stub(monkeypatch)

    assert cli.main(["answer", "t_1", "ANSWER-TEXT-THAT-MUST-NOT-COME-BACK"]) == 0

    out, err = _console(capsys)
    assert "ANSWER-TEXT-THAT-MUST-NOT-COME-BACK" not in out + err


def test_answer_passes_the_author(world, monkeypatch):
    _, calls = _questions_stub(monkeypatch)

    cli.main(["answer", "t_1", "yes", "--author", "masood"])

    assert calls.answered[0][3] == "masood"


def test_answer_needs_no_repository_only_the_board(world, monkeypatch):
    world.repo.rename(world.tmp / "gone")  # there is no plan to load, and answer must not look for one
    _, calls = _questions_stub(monkeypatch)

    assert cli.main(["answer", "t_1", "yes"]) == 0

    assert calls.answered[0][0] == "b"


def test_answer_prints_a_question_error_to_stderr_and_exits_1_without_the_answer(world, monkeypatch, capsys):
    def refuse(board, card_id, text):
        raise sys.modules["ases.questions"].QuestionError("answer refused: possible secret on line 1 of the answer")

    _questions_stub(monkeypatch, answer=refuse)

    assert cli.main(["answer", "t_1", "sk-abcdefghijklmnopqrstuvwx is the key"]) == 1

    out, err = _console(capsys)
    assert err == "swarm answer: answer refused: possible secret on line 1 of the answer\n"
    assert out == ""
    assert "sk-abcdefghijklmnopqrstuvwx" not in out + err


def test_answer_says_what_to_do_when_hermes_fails_after_the_comment(world, monkeypatch, capsys):
    def hermes_fails(board, card_id, text):
        raise hermes.HermesCommandError(["kanban", "unblock"], 1, "cannot unblock")

    _questions_stub(monkeypatch, answer=hermes_fails)

    assert cli.main(["answer", "t_1", "ANSWER-TEXT"]) == 1

    out, err = _console(capsys)
    assert "Hermes refused" in err and "swarm questions" in err
    assert "ANSWER-TEXT" not in out + err


def test_answer_against_the_real_module_posts_the_comment_then_unblocks(world, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(hermes, "kanban_show", lambda board, card_id: {
        "id": card_id, "status": "blocked", "title": "T1: scaffold", "assignee": "coder-1", "_parents": [],
        "_runs": [], "_comments": [], "_children": [],
        "_events": [{"kind": "blocked", "payload": {"reason": "Which database?"}, "created_at": 1789832318,
                     "run_id": None}],
    })
    monkeypatch.setattr(hermes, "kanban_comment", lambda board, card_id, text, author=None: calls.append(
        ("comment", card_id, text, author)))
    monkeypatch.setattr(hermes, "kanban_unblock", lambda board, card_id, reason=None: calls.append(
        ("unblock", card_id, reason)))

    assert cli.main(["answer", "w1", "SQLite is enough", "--author", "masood"]) == 0

    assert [c[0] for c in calls] == ["comment", "unblock"]
    assert calls[0][2] == "ANSWER: SQLite is enough" and calls[0][3] == "masood"
    out, _ = _console(capsys)
    assert "Which database?" in out and out.rstrip().endswith("answered")
    assert "SQLite is enough" not in out


# ---------------------------------------------------------------------------------------------
# status, report (ASES-OBS-01, ASES-OBS-02)
# ---------------------------------------------------------------------------------------------


def _report_stub(monkeypatch):
    calls = types.SimpleNamespace(build=[], written=[])
    report = {"generated_at": "2026-09-22T10:00:00+00:00", "project": {"name": "ases"}}

    def build_report(board, plan, project, models_config, conn, **kwargs):
        calls.build.append((board, plan.project, project.name, sorted(models_config)))
        return report

    def write_report(data, directory):
        directory = pathlib.Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "report.html").write_text("<html></html>", encoding="utf-8")
        (directory / "report.json").write_text("{}", encoding="utf-8")
        calls.written.append(directory)
        return directory / "report.html", directory / "report.json"

    monkeypatch.setattr(cli.report_mod, "build_report", build_report)
    monkeypatch.setattr(cli.report_mod, "render_status", lambda data: f"STATUS one screen {ACCENT}")
    monkeypatch.setattr(cli.report_mod, "render_text", lambda data: f"FULL REPORT {ACCENT}")
    monkeypatch.setattr(cli.report_mod, "write_report", write_report)
    return calls


def test_status_builds_the_report_and_prints_the_compact_screen(world, monkeypatch, capsys):
    calls = _report_stub(monkeypatch)

    assert cli.main(["status", "--repo", str(world.repo)]) == 0

    assert calls.build == [("b", "t3", "ases", ["models", "providers"])]
    out, _ = _console(capsys)
    assert out == "STATUS one screen caf\\xe9\n"
    assert calls.written == [] and not (world.project.ases_home / "reports").exists()  # status writes nothing


def test_report_prints_the_full_text_and_writes_nothing_by_default(world, monkeypatch, capsys):
    calls = _report_stub(monkeypatch)

    assert cli.main(["report", "--repo", str(world.repo)]) == 0

    assert _console(capsys)[0] == "FULL REPORT caf\\xe9\n"
    assert calls.written == []


def test_report_html_writes_the_page_and_json_under_ases_home_outside_the_repo(world, monkeypatch, capsys):
    calls = _report_stub(monkeypatch)

    assert cli.main(["report", "--repo", str(world.repo), "--html"]) == 0

    (directory,) = calls.written
    assert directory.parent == world.project.ases_home / "reports" / "ases"
    assert not cli._is_inside(directory, world.repo)
    out, _ = _console(capsys)
    assert f"report page: {directory / 'report.html'}" in out
    assert f"report data: {directory / 'report.json'}" in out
    assert (directory / "report.html").exists() and (directory / "report.json").exists()


def test_report_out_writes_where_it_is_told_and_implies_the_page(world, monkeypatch, capsys):
    calls = _report_stub(monkeypatch)
    target = world.tmp / "elsewhere" / "r1"

    assert cli.main(["report", "--repo", str(world.repo), "--out", str(target)]) == 0

    assert calls.written == [target]
    assert (target / "report.html").exists()


def test_report_refuses_an_out_directory_inside_the_repository(world, monkeypatch, capsys):
    calls = _report_stub(monkeypatch)

    assert cli.main(["report", "--repo", str(world.repo), "--out", str(world.repo / "reports")]) == 1

    out, err = _console(capsys)
    assert "REFUSED" in err and "inside the repository" in err
    assert calls.build == [] and calls.written == []  # refused before any work was done
    assert not (world.repo / "reports").exists()


def test_report_and_status_need_a_plan_that_passes_gate_0(world, monkeypatch, capsys):
    calls = _report_stub(monkeypatch)
    world.plan_file.unlink()

    assert cli.main(["status", "--repo", str(world.repo)]) == 1
    assert cli.main(["report", "--repo", str(world.repo)]) == 1

    assert calls.build == []
    assert "plan file not found" in _console(capsys)[0]


def test_status_and_report_against_the_real_report_module(world, monkeypatch, capsys):
    """One pass through the real report.py with Hermes unreachable: the wiring and the argument order are real."""
    def unreachable(*args, **kwargs):
        raise hermes.HermesCommandError(["kanban"], 1, "hermes is not running")

    monkeypatch.setattr(hermes, "kanban_show", unreachable)
    monkeypatch.setattr(hermes, "kanban_list", lambda board, status=None, assignee=None: [])
    events.record(world.conn, "pass_error", {"error": f"a {ACCENT} title {EMOJI}"})

    assert cli.main(["status", "--repo", str(world.repo)]) == 0
    status_out, _ = _console(capsys)
    assert cli.main(["report", "--repo", str(world.repo), "--html"]) == 0
    report_out, _ = _console(capsys)

    assert "ases" in status_out and status_out.isascii()
    assert "ASES project report" in report_out and report_out.isascii()
    written = list((world.project.ases_home / "reports" / "ases").glob("*/report.html"))
    assert len(written) == 1 and (written[0].parent / "report.json").exists()


# ---------------------------------------------------------------------------------------------
# plan (the Lead) and _run_lead
# ---------------------------------------------------------------------------------------------


class _Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def test_run_lead_calls_hermes_with_the_lead_profile_and_the_file_and_terminal_toolsets(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli.hermes_mod, "hermes_path", lambda: "hermes")

    def fake_run(argv, **kwargs):
        seen.update(argv=argv, kwargs=kwargs)
        return _Completed(0, "done\n", "")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    result = cli._run_lead(pathlib.Path("r"), "the prompt")

    assert seen["argv"] == ["hermes", "-p", "lead", "-z", "the prompt", "-t", "file,terminal"]
    assert seen["kwargs"]["timeout"] == 1800 and seen["kwargs"]["encoding"] == "utf-8"
    assert result == cli._LeadResult(True, 0, "done")


def test_run_lead_starts_hermes_with_a_credential_scrubbed_environment(monkeypatch):
    """ASES-CFG-05 (blueprint 10.2): a provider key exported into the shell that runs `swarm plan` must not reach
    the Lead's hermes process, while PATH (and SYSTEMROOT on Windows) still must. Nothing else about the call
    changes."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-adversarial-12345")
    monkeypatch.setenv("ASES_HARMLESS_SETTING", "kept")
    monkeypatch.setattr(cli.hermes_mod, "hermes_path", lambda: "hermes")
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(argv=argv, kwargs=kwargs)
        return _Completed(0, "done\n", "")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    assert cli._run_lead(pathlib.Path("r"), "p") == cli._LeadResult(True, 0, "done")

    env = seen["kwargs"].get("env")
    assert env is not None, "_run_lead passed no env=, so the Lead inherits the whole parent environment"
    assert "OPENROUTER_API_KEY" not in {name.upper() for name in env}
    assert "sk-test-adversarial-12345" not in env.values()
    assert env["ASES_HARMLESS_SETTING"] == "kept" and env["PATH"] == os.environ["PATH"]
    if os.name == "nt":
        assert env["SYSTEMROOT"] == os.environ["SYSTEMROOT"]
    assert {k: v for k, v in seen["kwargs"].items() if k != "env"} == {
        "capture_output": True, "text": True, "timeout": 1800, "encoding": "utf-8", "errors": "replace",
    }


def test_run_lead_falls_back_to_stderr_when_stdout_is_empty(monkeypatch):
    monkeypatch.setattr(cli.hermes_mod, "hermes_path", lambda: "hermes")
    monkeypatch.setattr(cli.subprocess, "run", lambda argv, **kw: _Completed(1, "", "boom\n"))

    assert cli._run_lead(pathlib.Path("r"), "p") == cli._LeadResult(True, 1, "boom")


def test_run_lead_reports_a_timeout_with_the_partial_output_even_when_it_is_bytes(monkeypatch):
    monkeypatch.setattr(cli.hermes_mod, "hermes_path", lambda: "hermes")

    def slow(argv, **kwargs):
        raise cli.subprocess.TimeoutExpired(argv, 1800, output=b"half an answer", stderr="and a warning")

    monkeypatch.setattr(cli.subprocess, "run", slow)

    result = cli._run_lead(pathlib.Path("r"), "p")

    assert not result.ran and result.timed_out
    assert result.partial == "half an answerand a warning" and "1800s" in result.problem


def test_run_lead_never_raises_when_hermes_is_missing_or_cannot_start(monkeypatch):
    def missing():
        raise cli.hermes_mod.HermesNotFound("`hermes` is not on PATH")

    monkeypatch.setattr(cli.hermes_mod, "hermes_path", missing)
    assert cli._run_lead(pathlib.Path("r"), "p").problem == "`hermes` is not on PATH"

    monkeypatch.setattr(cli.hermes_mod, "hermes_path", lambda: "hermes")

    def cannot(argv, **kwargs):
        raise OSError("access denied")

    monkeypatch.setattr(cli.subprocess, "run", cannot)
    result = cli._run_lead(pathlib.Path("r"), "p")
    assert not result.ran and "access denied" in result.problem


def test_run_lead_refuses_a_prompt_that_cannot_be_a_windows_command_line(monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(cli.hermes_mod, "hermes_path", lambda: "hermes")
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **kw: pytest.fail("must not start a process"))

    result = cli._run_lead(pathlib.Path("r"), "x" * 40000)

    assert not result.ran and "over the Windows limit" in result.problem


def test_plan_writes_the_prompt_with_the_absolute_paths_and_reports_the_file(world, monkeypatch, capsys):
    prompts = []

    def lead(repo, prompt):
        prompts.append((repo, prompt))
        world.plan_file.write_text("{}", encoding="utf-8")
        return cli._LeadResult(True, 0, "done")

    monkeypatch.setattr(cli, "_run_lead", lead)

    assert cli.main(["plan", "--repo", str(world.repo), "--request", "Build a todo app"]) == 0

    (repo, prompt), = prompts
    assert repo == world.repo.resolve()
    assert "Build a todo app" in prompt and str(world.repo.resolve()) in prompt
    assert '"integration_branch": "integration"' in prompt
    out, _ = _console(capsys)
    assert "done" in out and f"wrote {world.plan_file.resolve()}" in out


def test_plan_prompt_asks_the_lead_to_write_contracts_decisions_and_agents_md(world, monkeypatch):
    """ASES-GIT-15 (section 8.5): 'Contracts and decisions therefore live in the repository ... an AGENTS.md
    at the root, which Hermes loads automatically from the working directory.' A substring assertion is
    enough here -- the prose belongs to the prompt, not to this test."""
    prompts = []

    def lead(repo, prompt):
        prompts.append(prompt)
        world.plan_file.write_text("{}", encoding="utf-8")
        return cli._LeadResult(True, 0, "done")

    monkeypatch.setattr(cli, "_run_lead", lead)

    cli.main(["plan", "--repo", str(world.repo), "--request", "Build a todo app"])

    (prompt,) = prompts
    assert "docs/ases/contracts/" in prompt and "ASES-GIT-15" in prompt
    assert "docs/ases/decisions/" in prompt
    assert "AGENTS.md" in prompt


def test_plan_exits_1_when_the_lead_wrote_no_plan(world, monkeypatch, capsys):
    world.plan_file.unlink()
    monkeypatch.setattr(cli, "_run_lead", lambda repo, prompt: cli._LeadResult(True, 0, "done"))

    assert cli.main(["plan", "--repo", str(world.repo), "--request", "x"]) == 1

    assert "lead did not write" in _console(capsys)[1]


def test_plan_exits_1_when_the_lead_exits_non_zero_even_if_it_wrote_a_file(world, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_run_lead", lambda repo, prompt: cli._LeadResult(True, 1, "it broke"))

    assert cli.main(["plan", "--repo", str(world.repo), "--request", "x"]) == 1

    assert "wrote" in _console(capsys)[0]


def test_plan_says_a_timeout_is_not_necessarily_stuck_and_shows_the_partial_output(world, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_run_lead", lambda repo, prompt: cli._LeadResult(
        False, problem="lead did not finish within 1800s", timed_out=True, partial="thinking " + ACCENT))

    assert cli.main(["plan", "--repo", str(world.repo), "--request", "x"]) == 1

    err = _console(capsys)[1]
    assert "not necessarily stuck" in err and "partial output:" in err and "thinking caf\\xe9" in err


def test_plan_says_when_the_lead_could_not_run_at_all(world, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_run_lead", lambda repo, prompt: cli._LeadResult(False, problem="hermes is not on PATH"))

    assert cli.main(["plan", "--repo", str(world.repo), "--request", "x"]) == 1

    assert _console(capsys)[1] == "swarm plan: hermes is not on PATH\n"


# ---------------------------------------------------------------------------------------------
# plan: ensure_repo_bootstrapped wiring (ASES-GIT-10, round 7 part A) and the scaffold-task prompt
# guidance (ASES-GIT-11, round 7 part B).
# ---------------------------------------------------------------------------------------------


def test_plan_bootstraps_an_empty_repository_before_ever_asking_the_lead(world, monkeypatch, capsys):
    """world.repo (a plain directory, no .git) is exactly the "brand new project" case ASES-GIT-10 is about:
    cmd_plan is the earliest real touch-point, so the branch and the one commit must exist before the Lead is
    ever asked to inspect the repository."""
    assert not (world.repo / ".git").exists()

    def lead(repo, prompt):
        assert (repo / ".git").is_dir()  # bootstrapped BEFORE the Lead ever runs
        world.plan_file.write_text("{}", encoding="utf-8")
        return cli._LeadResult(True, 0, "done")

    monkeypatch.setattr(cli, "_run_lead", lead)

    assert cli.main(["plan", "--repo", str(world.repo), "--request", "x"]) == 0

    assert (world.repo / ".git").is_dir()
    branch = subprocess.run(
        ["git", "-C", str(world.repo), "symbolic-ref", "--short", "HEAD"], capture_output=True, text=True,
    ).stdout.strip()
    assert branch == "integration"
    log = subprocess.run(
        ["git", "-C", str(world.repo), "log", "--oneline"], capture_output=True, text=True,
    ).stdout.strip().splitlines()
    assert len(log) == 1
    out, _ = _console(capsys)
    assert "bootstrapped an empty repository" in out and "ASES-GIT-10" in out
    rows = _events(world.conn, "repo_bootstrapped")
    assert len(rows) == 1 and rows[0]["repo"] == str(world.repo.resolve())


def test_plan_never_touches_a_repository_that_already_has_real_history(world, monkeypatch, capsys):
    subprocess.run(["git", "-C", str(world.repo), "init", "-q", "-b", "integration"], check=True)
    (world.repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(world.repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(world.repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"],
        check=True,
    )
    tip = subprocess.run(
        ["git", "-C", str(world.repo), "rev-parse", "HEAD"], capture_output=True, text=True,
    ).stdout.strip()

    def lead(repo, prompt):
        world.plan_file.write_text("{}", encoding="utf-8")
        return cli._LeadResult(True, 0, "done")

    monkeypatch.setattr(cli, "_run_lead", lead)

    assert cli.main(["plan", "--repo", str(world.repo), "--request", "x"]) == 0

    after = subprocess.run(
        ["git", "-C", str(world.repo), "rev-parse", "HEAD"], capture_output=True, text=True,
    ).stdout.strip()
    assert after == tip  # untouched
    out, _ = _console(capsys)
    assert "bootstrapped" not in out
    assert _events(world.conn, "repo_bootstrapped") == []


def test_plan_prompt_tells_the_lead_to_plan_a_scaffold_task_first_when_the_repo_is_empty(world, monkeypatch):
    """ASES-GIT-11 (section 8.3): 'Greenfield projects start with one serialized scaffold task ... Parallel work
    starts only after the scaffold is merged.' The prompt must say this explicitly and tell the Lead to wire
    every other task's depends_on to the scaffold task by name -- Gate 0's touches-overlap serialization alone
    does not guarantee that (see test_plan.py's Gate 0 test)."""
    prompts = []

    def lead(repo, prompt):
        prompts.append(prompt)
        world.plan_file.write_text("{}", encoding="utf-8")
        return cli._LeadResult(True, 0, "done")

    monkeypatch.setattr(cli, "_run_lead", lead)

    cli.main(["plan", "--repo", str(world.repo), "--request", "Build a todo app"])

    (prompt,) = prompts
    assert "ASES-GIT-11" in prompt and "scaffold task" in prompt
    assert "depends_on" in prompt and "Gate 0" in prompt
    assert "pyproject.toml" in prompt and "package.json" in prompt


def test_plan_prompt_offers_the_tester_role_only_when_the_project_maps_one(tmp_path, monkeypatch, world):
    prompts = []

    def lead(repo, prompt):
        prompts.append(prompt)
        world.plan_file.write_text("{}", encoding="utf-8")
        return cli._LeadResult(True, 0, "done")

    monkeypatch.setattr(cli, "_run_lead", lead)

    cli.main(["plan", "--repo", str(world.repo), "--request", "x"])
    (no_tester_prompt,) = prompts
    assert "'tester'" not in no_tester_prompt and '"tester"' not in no_tester_prompt

    prompts.clear()
    project_with_tester = _project(tmp_path, roles=dict(ROLES, tester="tester-1"))
    monkeypatch.setattr(cli, "_load_project", lambda: project_with_tester)

    cli.main(["plan", "--repo", str(world.repo), "--request", "x"])
    (with_tester_prompt,) = prompts
    assert "'tester'" in with_tester_prompt and '"coder"|"reviewer"|"tester"' in with_tester_prompt


# ---------------------------------------------------------------------------------------------
# critique (Gate P: ASES-REV-01, ASES-REV-02)
# ---------------------------------------------------------------------------------------------


def _critique(status="PASS", *, valid=True, **fields):
    changes = ["add a scaffold task before T1"] if status == "CHANGES_REQUIRED" else []
    values = dict(valid=valid, status=status, summary=f"{status} summary", required_changes=changes)
    values.update(fields)
    return critic.PlanCritique(**values)


class _Reviewer:
    """A fake critic.run_critique that answers from a script and binds each valid verdict to the hash of the plan
    file at the moment it is asked, as the real run_critique does."""

    def __init__(self, monkeypatch, script):
        self.script = list(script)
        self.calls = []
        monkeypatch.setattr(cli.critic_mod, "run_critique", self)

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        critique = self.script.pop(0)
        if critique.valid:
            critique = dataclasses.replace(critique, plan_hash=critic.plan_hash(kwargs["plan_path"]))
        return critique


def _lead_that_rewrites(world, monkeypatch):
    prompts = []

    def lead(repo, prompt):
        prompts.append(prompt)
        raw = json.loads(world.plan_file.read_text(encoding="utf-8"))
        raw["tasks"][0]["title"] = f"scaffold, rewrite {len(prompts)}"
        world.plan_file.write_text(json.dumps(raw), encoding="utf-8")
        return cli._LeadResult(True, 0, "done")

    monkeypatch.setattr(cli, "_run_lead", lead)
    return prompts


def _no_lead(monkeypatch):
    monkeypatch.setattr(cli, "_run_lead", lambda repo, prompt: pytest.fail("the Lead must not be run"))


def _critique_argv(world, *extra):
    return ["critique", "--repo", str(world.repo), "--request", "Build a todo app", *extra]


def test_critique_pass_says_swarm_approve_may_run_and_exits_0(world, monkeypatch, capsys):
    reviewer = _Reviewer(monkeypatch, [_critique("PASS", summary="A sound small plan.")])
    _no_lead(monkeypatch)

    assert cli.main(_critique_argv(world)) == 0

    out, _ = _console(capsys)
    assert "status: PASS" in out and "summary: A sound small plan." in out
    assert out.rstrip().endswith("PASS: swarm approve may run")
    assert f"plan hash {world.plan_hash()[:12]}" in out
    assert len(reviewer.calls) == 1


def test_critique_records_the_verdict_bound_to_the_hash_of_the_plan_it_sent(world, monkeypatch):
    _Reviewer(monkeypatch, [_critique("PASS")])

    cli.main(_critique_argv(world))

    (event,) = _events(world.conn, "plan_critique")
    assert (event["project"], event["round"], event["status"], event["valid"]) == ("t3", 1, "PASS", True)
    assert event["plan_hash"] == world.plan_hash()
    assert critic.is_plan_approved_by_critic(world.conn, "t3", world.plan_hash())


def test_critique_gives_the_reviewer_the_same_estimate_text_the_approve_screen_shows(world, monkeypatch, capsys):
    reviewer = _Reviewer(monkeypatch, [_critique("PASS")])

    cli.main(_critique_argv(world))

    estimate = reviewer.calls[0]["estimate_text"]
    plan = cli.plan_mod.load_plan_file(world.plan_file, known_roles=set(ROLES), max_cards=40)
    expected = cli._estimate_lines(plan, world.project, MODELS_CONFIG, world.conn)
    assert estimate == expected.text() and estimate
    assert "budget[xkiro]: needs 10" in estimate and "budget[openrouter]: needs 5" in estimate
    assert "Estimated calendar time" in estimate and "xkiro/coder-model: needs 10 request(s), ~1.0 min" in estimate


def test_critique_passes_the_repository_the_plan_the_profile_and_the_timeout(world, monkeypatch):
    reviewer = _Reviewer(monkeypatch, [_critique("PASS"), _critique("PASS")])

    cli.main(_critique_argv(world))
    cli.main(_critique_argv(world, "--profile", "reviewer-2", "--timeout", "120"))

    first, second = reviewer.calls
    assert first["repo"] == world.repo.resolve() and first["plan_path"] == world.plan_file.resolve()
    assert (first["profile"], first["timeout"]) == ("reviewer", 900)  # the profile the roles map gives the reviewer
    assert (second["profile"], second["timeout"]) == ("reviewer-2", 120)


def test_critique_uses_the_reviewer_profile_of_the_roles_map_by_default(world, monkeypatch):
    project = _project(world.tmp, roles={"lead": "lead", "coder": "coder-1", "reviewer": "reviewer-b"})
    monkeypatch.setattr(cli, "_load_project", lambda: project)
    reviewer = _Reviewer(monkeypatch, [_critique("PASS")])

    cli.main(_critique_argv(world))

    assert reviewer.calls[0]["profile"] == "reviewer-b"


def test_critique_changes_required_without_auto_replan_prints_the_feedback_and_stops(world, monkeypatch, capsys):
    _Reviewer(monkeypatch, [_critique("CHANGES_REQUIRED", architecture_issues=["no scaffold"])])
    _no_lead(monkeypatch)

    assert cli.main(_critique_argv(world)) == 1

    out, _ = _console(capsys)
    assert "status: CHANGES_REQUIRED" in out
    assert "required changes:" in out and "1. add a scaffold task before T1" in out
    assert "architecture issues:" in out and "1. no scaffold" in out
    assert "the plan goes back to the Lead (re-plan 1 of at most 2)" in out and "--auto-replan" in out
    assert "Project request: Build a todo app" in out  # the feedback prompt the Lead would get
    assert "Plan file to rewrite" in out and str(world.plan_file.resolve()) in out
    assert critic.critique_rounds_used(world.conn, "t3") == 1  # the round was recorded


def test_critique_auto_replan_runs_the_lead_then_critiques_the_rewritten_plan(world, monkeypatch, capsys):
    reviewer = _Reviewer(monkeypatch, [_critique("CHANGES_REQUIRED"), _critique("PASS")])
    prompts = _lead_that_rewrites(world, monkeypatch)
    first_hash = world.plan_hash()

    assert cli.main(_critique_argv(world, "--auto-replan")) == 0

    (prompt,) = prompts
    assert "add a scaffold task before T1" in prompt and "Project request: Build a todo app" in prompt
    assert len(reviewer.calls) == 2
    out, _ = _console(capsys)
    assert "Re-planning (1 of at most 2)" in out and out.rstrip().endswith("PASS: swarm approve may run")
    assert out.count("Gate 0 passed") == 2  # the rewritten plan went through Gate 0 again
    rounds = sorted((e["round"], e["status"]) for e in _events(world.conn, "plan_critique"))
    assert rounds == [(1, "CHANGES_REQUIRED"), (2, "PASS")]
    # The verdict belongs to the rewritten plan only: the plan hash binding.
    assert world.plan_hash() != first_hash
    assert critic.is_plan_approved_by_critic(world.conn, "t3", world.plan_hash())
    assert not critic.is_plan_approved_by_critic(world.conn, "t3", first_hash)


def test_critique_auto_replan_stops_after_the_round_limit_and_asks_the_user(world, monkeypatch, capsys):
    reviewer = _Reviewer(monkeypatch, [_critique("CHANGES_REQUIRED")] * 3)
    prompts = _lead_that_rewrites(world, monkeypatch)

    assert cli.main(_critique_argv(world, "--auto-replan")) == 1

    assert len(prompts) == 2 and len(reviewer.calls) == 3  # two re-plans, then the third verdict is final
    out, _ = _console(capsys)
    assert "A person must decide" in out and "budgets.replans_per_project = 2" in out
    assert "already gone back to the Lead 2 time(s)" in out
    assert critic.critique_rounds_used(world.conn, "t3") == 3


def test_critique_round_limit_follows_budgets_replans_per_project(world, monkeypatch, capsys):
    project = _project(world.tmp, budgets={"replans_per_project": 1, "max_cards": 40})
    monkeypatch.setattr(cli, "_load_project", lambda: project)
    reviewer = _Reviewer(monkeypatch, [_critique("CHANGES_REQUIRED")] * 2)
    prompts = _lead_that_rewrites(world, monkeypatch)

    assert cli.main(_critique_argv(world, "--auto-replan")) == 1

    assert len(prompts) == 1 and len(reviewer.calls) == 2
    assert "budgets.replans_per_project = 1" in _console(capsys)[0]


def test_critique_counts_rounds_across_invocations(world, monkeypatch, capsys):
    """Two earlier CHANGES_REQUIRED verdicts are already recorded: a third goes to the user, not to the Lead."""
    for round_no in (1, 2):
        critic.record_critique(world.conn, "t3", round_no, _critique("CHANGES_REQUIRED"))
    _Reviewer(monkeypatch, [_critique("CHANGES_REQUIRED")])
    _no_lead(monkeypatch)

    assert cli.main(_critique_argv(world, "--auto-replan")) == 1

    assert "A person must decide" in _console(capsys)[0]
    assert sorted(e["round"] for e in _events(world.conn, "plan_critique")) == [1, 2, 3]


def test_critique_blocked_goes_to_the_user_even_with_auto_replan(world, monkeypatch, capsys):
    _Reviewer(monkeypatch, [_critique("BLOCKED", summary="The request contradicts itself.")])
    _no_lead(monkeypatch)

    assert cli.main(_critique_argv(world, "--auto-replan")) == 1

    out, _ = _console(capsys)
    assert "status: BLOCKED" in out and "A person must decide: the reviewer BLOCKED the plan" in out


def test_critique_invalid_answer_prints_the_problems_and_is_not_a_round(world, monkeypatch, capsys):
    bad = critic.PlanCritique(valid=False, problems=("review_status is missing", "summary is missing"))
    _Reviewer(monkeypatch, [bad])
    _no_lead(monkeypatch)

    assert cli.main(_critique_argv(world, "--auto-replan")) == 1

    out, _ = _console(capsys)
    assert "no valid verdict" in out and "1. review_status is missing" in out and "2. summary is missing" in out
    assert "could not be accepted even after the one repair request" in out
    (event,) = _events(world.conn, "plan_critique")
    assert event["valid"] is False and event["round"] == 1
    assert critic.critique_rounds_used(world.conn, "t3") == 0  # nothing was sent back to the Lead


def test_critique_warns_about_suspected_gate_tampering_even_on_a_pass(world, monkeypatch, capsys):
    _Reviewer(monkeypatch, [_critique("PASS", gate_tampering_suspected=True)])

    assert cli.main(_critique_argv(world)) == 0

    assert "WARNING: the reviewer suspects gate tampering" in _console(capsys)[0]


def test_critique_that_finds_the_plan_changed_meanwhile_is_not_a_pass(world, monkeypatch, capsys):
    reviewer = _Reviewer(monkeypatch, [_critique("PASS")])

    def edit_while_it_thinks(**kwargs):
        verdict = reviewer(**kwargs)
        raw = json.loads(world.plan_file.read_text(encoding="utf-8"))
        raw["tasks"][0]["title"] = "edited by the user while the reviewer was thinking"
        world.plan_file.write_text(json.dumps(raw), encoding="utf-8")
        return verdict

    monkeypatch.setattr(cli.critic_mod, "run_critique", edit_while_it_thinks)
    sent_hash = world.plan_hash()

    assert cli.main(_critique_argv(world)) == 1

    assert "the plan file changed while the critique ran" in _console(capsys)[0]
    assert critic.is_plan_approved_by_critic(world.conn, "t3", sent_hash)  # the verdict is about the old file
    assert not critic.is_plan_approved_by_critic(world.conn, "t3", world.plan_hash())


def test_critique_needs_a_plan_that_passes_gate_0_before_the_reviewer_is_asked(world, monkeypatch, capsys):
    reviewer = _Reviewer(monkeypatch, [_critique("PASS")])
    world.plan_file.write_text(json.dumps({**PLAN_RAW, "tasks": []}), encoding="utf-8")

    assert cli.main(_critique_argv(world)) == 1

    assert reviewer.calls == []
    assert "Gate 0 FAILED:" in _console(capsys)[0]


def test_critique_auto_replan_stops_when_the_lead_does_not_finish(world, monkeypatch, capsys):
    reviewer = _Reviewer(monkeypatch, [_critique("CHANGES_REQUIRED"), _critique("PASS")])
    monkeypatch.setattr(cli, "_run_lead", lambda repo, prompt: cli._LeadResult(False, problem="timed out"))

    assert cli.main(_critique_argv(world, "--auto-replan")) == 1

    assert "the Lead did not finish: timed out" in _console(capsys)[1]
    assert len(reviewer.calls) == 1


def test_critique_auto_replan_stops_when_the_rewritten_plan_fails_gate_0(world, monkeypatch, capsys):
    reviewer = _Reviewer(monkeypatch, [_critique("CHANGES_REQUIRED"), _critique("PASS")])

    def lead_breaks_the_plan(repo, prompt):
        world.plan_file.write_text(json.dumps({**PLAN_RAW, "tasks": []}), encoding="utf-8")
        return cli._LeadResult(True, 0, "done")

    monkeypatch.setattr(cli, "_run_lead", lead_breaks_the_plan)

    assert cli.main(_critique_argv(world, "--auto-replan")) == 1

    assert "Gate 0 FAILED:" in _console(capsys)[0] and len(reviewer.calls) == 1


def test_critique_notes_when_approve_would_refuse_the_plan_anyway(world, monkeypatch, capsys):
    project = _project(world.tmp, data_class="private")  # openrouter declares no data policy
    monkeypatch.setattr(cli, "_load_project", lambda: project)
    reviewer = _Reviewer(monkeypatch, [_critique("PASS")])

    assert cli.main(_critique_argv(world)) == 0

    assert "swarm approve would currently refuse this plan" in _console(capsys)[0]
    assert "Gate P would REFUSE this plan (data policy, ASES-PRV-01)" in reviewer.calls[0]["estimate_text"]


def test_critique_output_is_ascii_for_reviewer_text(world, monkeypatch, capsys):
    _Reviewer(monkeypatch, [_critique("CHANGES_REQUIRED", summary=f"{ACCENT} {ARROW} {EMOJI}")])

    cli.main(_critique_argv(world))

    out, err = _console(capsys)
    assert out.isascii() and err.isascii() and "caf\\xe9" in out


# ---------------------------------------------------------------------------------------------
# _scaffolding_warnings (ASES-GIT-15, section 8.5)
# ---------------------------------------------------------------------------------------------


def _write_contract(repo, name="api.md", text="boundary"):
    d = repo / "docs" / "ases" / "contracts"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text, encoding="utf-8")


def _write_decision(repo, name="d1.md", text="decision"):
    d = repo / "docs" / "ases" / "decisions"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text, encoding="utf-8")


def _write_agents_md(repo, text="This project builds a todo app."):
    (repo / "AGENTS.md").write_text(text, encoding="utf-8")


def test_scaffolding_warnings_is_empty_when_all_three_are_present(world):
    _write_contract(world.repo)
    _write_decision(world.repo)
    _write_agents_md(world.repo)

    assert cli._scaffolding_warnings(world.repo) == []


def test_scaffolding_warnings_names_each_missing_path_never_refusing(world):
    warnings = cli._scaffolding_warnings(world.repo)  # nothing written at all yet (world only seeds plan.json)

    assert len(warnings) == 3
    assert any("docs/ases/contracts/" in w and "ASES-GIT-15" in w for w in warnings)
    assert any("docs/ases/decisions/" in w for w in warnings)
    assert any("AGENTS.md" in w for w in warnings)


def test_scaffolding_warnings_treats_an_empty_directory_as_missing(world):
    (world.repo / "docs" / "ases" / "contracts").mkdir(parents=True)
    (world.repo / "docs" / "ases" / "decisions").mkdir(parents=True)
    _write_agents_md(world.repo)

    warnings = cli._scaffolding_warnings(world.repo)

    assert len(warnings) == 2
    assert any("docs/ases/contracts/" in w for w in warnings)
    assert any("docs/ases/decisions/" in w for w in warnings)


def test_scaffolding_warnings_treats_an_empty_agents_md_as_missing(world):
    _write_contract(world.repo)
    _write_decision(world.repo)
    (world.repo / "AGENTS.md").write_text("   \n", encoding="utf-8")

    warnings = cli._scaffolding_warnings(world.repo)

    assert len(warnings) == 1 and "AGENTS.md" in warnings[0]


def test_scaffolding_warnings_reports_only_what_is_actually_missing(world):
    _write_contract(world.repo)
    _write_decision(world.repo)
    # AGENTS.md is still missing

    warnings = cli._scaffolding_warnings(world.repo)

    assert len(warnings) == 1 and "AGENTS.md" in warnings[0]


# ---------------------------------------------------------------------------------------------
# approve (Gate P: ASES-REV-03, ASES-CTL-01)
# ---------------------------------------------------------------------------------------------


def _approve_world(world, monkeypatch):
    calls = []
    monkeypatch.setattr(cli.controller_mod, "publish_plan",
                        lambda repo, branch: calls.append(("publish", branch)) or "abc1234")
    monkeypatch.setattr(cli.controller_mod, "pin_gate_profiles",
                        lambda conn, project, gate_profiles, *rest: calls.append(("pin", project)) or "hash")

    def create(board, project_id, repo, plan, project, *, conn):
        calls.append(("create", board, project_id))
        return [controller.CardPair("T1", "w1", "m1"), controller.CardPair("T2", "w2", "m2")]

    monkeypatch.setattr(cli.controller_mod, "create_cards_from_plan", create)
    return calls


def _approve_argv(world, *extra):
    return ["approve", "--repo", str(world.repo), "--project-id", "p_1", *extra]


def _record_pass(world, summary="A sound small plan.", **fields):
    critic.record_critique(world.conn, "t3", 1, _critique("PASS", summary=summary, plan_hash=world.plan_hash(),
                                                          **fields))


def test_approve_refuses_a_plan_that_has_no_critic_pass_and_creates_nothing(world, monkeypatch, capsys):
    calls = _approve_world(world, monkeypatch)

    assert cli.main(_approve_argv(world, "--yes")) == 1  # --yes skips the question, never the critic

    assert calls == []
    _, err = _console(capsys)
    assert "Gate P REFUSED (ASES-REV-03)" in err and "no critic PASS" in err
    assert f"plan hash {world.plan_hash()[:12]}" in err and "swarm critique" in err


def test_approve_with_a_critic_pass_and_yes_publishes_pins_and_creates_the_cards(world, monkeypatch, capsys):
    calls = _approve_world(world, monkeypatch)
    _record_pass(world)

    assert cli.main(_approve_argv(world, "--yes")) == 0

    assert calls == [("publish", "integration"), ("pin", "t3"), ("create", "b", "p_1")]
    out, _ = _console(capsys)
    assert "Gate 0 passed: 2 tasks" in out
    assert "  budget[xkiro]: needs 10, provider has no known daily cap" in out
    assert "  budget[openrouter]: needs 5, within budget" in out
    assert "Estimated calendar time (pacing, not a budget decision -- ASES-CAP-04):" in out
    assert "PASS for this exact plan (round 1" in out and "summary: A sound small plan." in out
    assert "Gate P: published approved plan at abc1234" in out
    assert "  T1: work=w1 merge=m1" in out and "  T2: work=w2 merge=m2" in out
    assert _events(world.conn, "critic_skipped") == []


def test_approve_shows_the_same_estimate_lines_critique_hands_to_the_reviewer(world, monkeypatch, capsys):
    _approve_world(world, monkeypatch)
    _record_pass(world)
    reviewer = _Reviewer(monkeypatch, [_critique("PASS")])

    cli.main(_approve_argv(world, "--yes"))
    approve_out, _ = _console(capsys)
    cli.main(_critique_argv(world))

    for line in reviewer.calls[0]["estimate_text"].splitlines():
        assert line in approve_out.splitlines()


def test_approve_refuses_when_the_plan_was_edited_after_the_pass(world, monkeypatch, capsys):
    calls = _approve_world(world, monkeypatch)
    _record_pass(world)
    raw = json.loads(world.plan_file.read_text(encoding="utf-8"))
    raw["tasks"][0]["title"] = "edited after the critique"
    world.plan_file.write_text(json.dumps(raw), encoding="utf-8")

    assert cli.main(_approve_argv(world, "--yes")) == 1

    assert calls == []
    assert "Gate P REFUSED (ASES-REV-03)" in _console(capsys)[1]


def test_approve_refuses_when_the_newest_critique_of_the_plan_is_not_a_pass(world, monkeypatch, capsys):
    calls = _approve_world(world, monkeypatch)
    _record_pass(world)
    critic.record_critique(world.conn, "t3", 2, _critique("CHANGES_REQUIRED", plan_hash=world.plan_hash(),
                                                          summary="the second look found a hole"))

    assert cli.main(_approve_argv(world, "--yes")) == 1

    assert calls == []
    err = _console(capsys)[1]
    assert "latest critique of this plan: CHANGES_REQUIRED (round 2): the second look found a hole" in err


def test_approve_skip_critic_approves_records_the_event_and_says_so_on_the_screen(world, monkeypatch, capsys):
    calls = _approve_world(world, monkeypatch)

    assert cli.main(_approve_argv(world, "--yes", "--skip-critic")) == 0

    assert [c[0] for c in calls] == ["publish", "pin", "create"]
    out, _ = _console(capsys)
    assert "SKIPPED with --skip-critic" in out and "No independent reviewer has passed this plan" in out
    (event,) = _events(world.conn, "critic_skipped")
    assert event["project"] == "t3" and event["plan_hash"] == world.plan_hash()


def test_approve_skip_critic_does_not_record_a_skip_when_a_pass_exists(world, monkeypatch, capsys):
    _approve_world(world, monkeypatch)
    _record_pass(world)

    assert cli.main(_approve_argv(world, "--yes", "--skip-critic")) == 0

    assert _events(world.conn, "critic_skipped") == []
    assert "SKIPPED" not in _console(capsys)[0]


def test_approve_skip_critic_records_the_skip_only_once_the_user_has_approved(world, monkeypatch, capsys):
    calls = _approve_world(world, monkeypatch)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")

    assert cli.main(_approve_argv(world, "--skip-critic")) == 1

    assert calls == [] and _events(world.conn, "critic_skipped") == []
    assert "Not approved -- no plan published, no cards created." in _console(capsys)[0]


def test_approve_shows_the_latest_critique_on_the_screen_when_skipping(world, monkeypatch, capsys):
    _approve_world(world, monkeypatch)
    critic.record_critique(world.conn, "t3", 1, _critique("CHANGES_REQUIRED", plan_hash=world.plan_hash(),
                                                          summary="one task is missing"))

    assert cli.main(_approve_argv(world, "--yes", "--skip-critic")) == 0

    out, _ = _console(capsys)
    assert "latest critique of this plan: CHANGES_REQUIRED (round 1)" in out and "summary: one task is missing" in out


def test_approve_warns_on_the_screen_when_the_critic_suspects_gate_tampering(world, monkeypatch, capsys):
    _approve_world(world, monkeypatch)
    _record_pass(world, gate_tampering_suspected=True)

    assert cli.main(_approve_argv(world, "--yes")) == 0

    assert "the reviewer suspects gate tampering in this plan" in _console(capsys)[0]


@pytest.mark.parametrize("reply, approved", [("y", True), ("yes", True), ("YES ", True), ("n", False), ("", False),
                                             ("maybe", False)])
def test_approve_asks_and_only_a_yes_counts(world, monkeypatch, capsys, reply, approved):
    calls = _approve_world(world, monkeypatch)
    _record_pass(world)
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or reply)

    code = cli.main(_approve_argv(world))

    assert code == (0 if approved else 1)
    assert prompts == ["Proceed? [y/N] "]
    assert bool(calls) == approved
    out, _ = _console(capsys)
    assert "About to publish this plan and create 2 card(s) on board 'b'" in out


def test_approve_with_no_terminal_to_ask_is_not_an_approval(world, monkeypatch, capsys):
    calls = _approve_world(world, monkeypatch)
    _record_pass(world)

    def no_stdin(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", no_stdin)

    assert cli.main(_approve_argv(world)) == 1

    assert calls == [] and "Not approved" in _console(capsys)[0]


def test_approve_deadline_minutes_is_shown_and_stored_only_after_approval(world, monkeypatch, capsys):
    calls = _approve_world(world, monkeypatch)
    _record_pass(world)
    before = datetime.now(timezone.utc)

    assert cli.main(_approve_argv(world, "--yes", "--deadline-minutes", "90")) == 0

    state = bounds.get_state(world.conn, "t3")
    deadline = datetime.fromisoformat(state["deadline_at"])
    assert timedelta(minutes=89) < deadline - before < timedelta(minutes=91)
    out, _ = _console(capsys)
    assert f"Project wall-clock: 90 minutes from approval (deadline {state['deadline_at']})" in out
    assert [c[0] for c in calls] == ["publish", "pin", "create"]


def test_approve_does_not_store_the_deadline_when_the_user_declines(world, monkeypatch, capsys):
    _approve_world(world, monkeypatch)
    _record_pass(world)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")

    assert cli.main(_approve_argv(world, "--deadline-minutes", "90")) == 1

    assert bounds.get_state(world.conn, "t3") is None
    assert "Project wall-clock: 90 minutes from approval" in _console(capsys)[0]  # it was shown, not stored


def test_approve_shows_an_existing_deadline_and_says_when_there_is_none(world, monkeypatch, capsys):
    _approve_world(world, monkeypatch)
    _record_pass(world)

    cli.main(_approve_argv(world, "--yes"))
    assert "Project wall-clock: not set (no time limit; pass --deadline-minutes N" in _console(capsys)[0]

    bounds.set_deadline(world.conn, "t3", "2030-01-01T00:00:00+00:00")
    cli.main(_approve_argv(world, "--yes"))
    assert "Project wall-clock: deadline 2030-01-01T00:00:00+00:00 (set earlier)" in _console(capsys)[0]


@pytest.mark.parametrize("value", ["0", "-5", "soon"])
def test_approve_refuses_a_deadline_that_is_not_a_positive_number(world, value):
    with pytest.raises(SystemExit) as caught:
        cli.main(_approve_argv(world, "--deadline-minutes", value))

    assert caught.value.code == 2


def test_approve_gate_0_failure_prints_every_error_and_exits_1(world, monkeypatch, capsys):
    calls = _approve_world(world, monkeypatch)
    world.plan_file.write_text(json.dumps({**PLAN_RAW, "tasks": []}), encoding="utf-8")

    assert cli.main(_approve_argv(world, "--yes", "--skip-critic")) == 1

    assert calls == []
    assert _console(capsys)[0].startswith("Gate 0 FAILED:\n  - ")


def test_approve_refuses_a_provider_the_data_class_does_not_allow(world, monkeypatch, capsys):
    calls = _approve_world(world, monkeypatch)
    project = _project(world.tmp, data_class="private")  # openrouter declares no data_policy
    monkeypatch.setattr(cli, "_load_project", lambda: project)
    _record_pass(world)

    assert cli.main(_approve_argv(world, "--yes")) == 1

    assert calls == []
    assert "Gate P REFUSED (ASES-PRV-01)" in _console(capsys)[1]


def test_approve_refuses_a_private_plan_whose_provider_has_a_policy_but_no_verification_date(
    world, monkeypatch, capsys,
):
    """ASES-PRV-04: xkiro's data_policy (no_training) is compatible with data_class=private on its own, but
    MODELS_CONFIG never gives it a data_policy_verified_at, so Gate P must still refuse, and the message must
    name the real, specific problem (no recorded verification date), not the unrelated policy string."""
    calls = _approve_world(world, monkeypatch)
    project = _project(world.tmp, data_class="private")
    monkeypatch.setattr(cli, "_load_project", lambda: project)
    _record_pass(world)

    assert cli.main(_approve_argv(world, "--yes")) == 1

    assert calls == []
    err = _console(capsys)[1]
    assert "Gate P REFUSED (ASES-PRV-01)" in err
    assert "no recorded verification date" in err and "data_policy_verified_at" in err


def test_approve_accepts_a_private_plan_once_every_provider_has_a_verified_policy(world, monkeypatch, capsys):
    calls = _approve_world(world, monkeypatch)
    project = _project(world.tmp, data_class="private")
    monkeypatch.setattr(cli, "_load_project", lambda: project)
    models_config = copy.deepcopy(MODELS_CONFIG)
    models_config["providers"]["xkiro"]["data_policy_verified_at"] = "2026-09-19"
    models_config["providers"]["openrouter"]["data_policy"] = "no_training"
    models_config["providers"]["openrouter"]["data_policy_verified_at"] = "2026-09-19"
    monkeypatch.setattr(cli, "_load_models_config", lambda: models_config)
    _record_pass(world)

    assert cli.main(_approve_argv(world, "--yes")) == 0

    assert calls != []
    assert "Gate P REFUSED" not in _console(capsys)[1]


def test_approve_refuses_a_plan_the_days_quota_cannot_afford(world, monkeypatch, capsys):
    calls = _approve_world(world, monkeypatch)
    ledger.record_usage(world.conn, "openrouter", "review-model", n=50)  # the whole daily cap
    _record_pass(world)

    assert cli.main(_approve_argv(world, "--yes")) == 1

    assert calls == []
    out, _ = _console(capsys)
    assert "Gate P REFUSED: cannot afford this plan today on ['openrouter'] (ASES-CAP-03)" in out
    assert "Estimated calendar time" not in out  # the pacing estimate is for a plan that can go ahead


def test_approve_says_why_when_the_plan_cannot_be_published(world, monkeypatch, capsys):
    calls = _approve_world(world, monkeypatch)

    def wrong_branch(repo, branch):
        raise RuntimeError("publish_plan expected the repo to be on 'integration', found 'main'")

    monkeypatch.setattr(cli.controller_mod, "publish_plan", wrong_branch)
    _record_pass(world)

    assert cli.main(_approve_argv(world, "--yes")) == 1

    assert calls == []  # nothing pinned, no card created
    assert "swarm approve REFUSED: publish_plan expected the repo to be on 'integration'" in _console(capsys)[1]


def test_approve_prints_what_gate_0_serialized(world, monkeypatch, capsys):
    _approve_world(world, monkeypatch)
    raw = copy.deepcopy(PLAN_RAW)
    raw["tasks"][1] = {**raw["tasks"][1], "role": "coder", "depends_on": [], "touches": ["a.py"]}
    world.plan_file.write_text(json.dumps(raw), encoding="utf-8")
    critic.record_critique(world.conn, "t3", 1, _critique("PASS", plan_hash=world.plan_hash()))

    assert cli.main(_approve_argv(world, "--yes")) == 0

    assert "Gate 0 serialized T2 after T1" in _console(capsys)[0]


def test_approve_prints_a_scaffolding_warning_for_each_missing_path_before_the_yes_no_prompt(
    world, monkeypatch, capsys,
):
    calls = _approve_world(world, monkeypatch)
    _record_pass(world)
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or "y")

    assert cli.main(_approve_argv(world)) == 0  # ASES-GIT-15 is Inspection: never a refusal

    out, _ = _console(capsys)
    lines = out.splitlines()
    warning_lines = [i for i, line in enumerate(lines) if line.startswith("WARNING:")]
    prompt_line_index = next(i for i, line in enumerate(lines) if "About to publish" in line)
    assert len(warning_lines) == 3
    assert all(i < prompt_line_index for i in warning_lines)  # shown before the y/N prompt, not after
    assert any("docs/ases/contracts/" in lines[i] for i in warning_lines)
    assert any("docs/ases/decisions/" in lines[i] for i in warning_lines)
    assert any("AGENTS.md" in lines[i] for i in warning_lines)
    assert bool(calls)  # it never refused the plan over this


def test_approve_prints_no_scaffolding_warning_when_all_three_paths_are_present(world, monkeypatch, capsys):
    _approve_world(world, monkeypatch)
    _record_pass(world)
    (world.repo / "docs" / "ases" / "contracts").mkdir(parents=True)
    (world.repo / "docs" / "ases" / "contracts" / "api.md").write_text("x", encoding="utf-8")
    (world.repo / "docs" / "ases" / "decisions").mkdir(parents=True)
    (world.repo / "docs" / "ases" / "decisions" / "d1.md").write_text("x", encoding="utf-8")
    (world.repo / "AGENTS.md").write_text("context for every worker", encoding="utf-8")

    assert cli.main(_approve_argv(world, "--yes")) == 0

    assert "WARNING:" not in _console(capsys)[0]


# ---------------------------------------------------------------------------------------------
# run (section 9.2)
# ---------------------------------------------------------------------------------------------


class _RunWorld:
    """swarm run with everything outside the startup steps and the loop faked."""

    def __init__(self, world, monkeypatch):
        self.world = world
        self.reconcile_calls = []
        self.report = reconcile.ReconcileReport()
        self.passes = []
        self.run_calls = []
        monkeypatch.setattr(cli.controller_mod, "verify_gate_pin", lambda *a, **kw: None)
        monkeypatch.setattr(guards, "check_primary_checkout",
                            lambda *a, **kw: guards.GuardResult(True, (), "abc", "integration"))
        monkeypatch.setattr(guards, "adopt_current_head", lambda conn, project, repo: "abc")

        def fake_reconcile(board, repo, plan, *, conn, apply=True, **kwargs):
            self.reconcile_calls.append((board, plan.project, apply))
            if isinstance(self.report, BaseException):
                raise self.report
            return self.report

        monkeypatch.setattr(reconcile, "reconcile", fake_reconcile)

        def fake_run_pass(board, repo, plan, project, models_config, *, conn, **kwargs):
            self.run_calls.append(board)
            item = self.passes.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item

        monkeypatch.setattr(cli.controller_mod, "run_pass", fake_run_pass)

    def argv(self, *extra):
        return ["run", "--repo", str(self.world.repo), "--max-iterations", "5", "--sleep-seconds", "0", *extra]


_QUIET = {"parked": [], "merged": [], "sent_back": [], "finished": False}


def _pass(**fields):
    return {**_QUIET, **fields}


@pytest.fixture
def runw(world, monkeypatch):
    return _RunWorld(world, monkeypatch)


def test_run_starts_the_project_reconciles_then_loops_until_finished(runw, capsys):
    runw.passes = [_pass(), _pass(merged=["T1"], finished=True)]

    assert cli.main(runw.argv()) == 0

    assert runw.reconcile_calls == [("b", "t3", True)]  # a real reconcile, with repairs applied
    assert bounds.get_state(runw.world.conn, "t3")["status"] == "running"
    out, _ = _console(capsys)
    assert out.splitlines()[0] == "[pass 1] parked=[] merged=[] sent_back=[] finished=False"
    assert "[pass 2] parked=[] merged=['T1'] sent_back=[] finished=True" in out
    assert out.rstrip().endswith("all merge cards done")


@pytest.mark.parametrize("status, reason, said", [
    ("stopped", "swarm stop", "is stopped (swarm stop)"),
    ("stopped", None, "is stopped (no reason was recorded)"),
    ("finished", None, "is finished; there is nothing left to run"),
    ("paused", None, "is paused (a bound was reached or a final gate failed)"),
    ("paused", "project_wall_clock_minutes reached: 240 of 240",
     "is paused (project_wall_clock_minutes reached: 240 of 240)"),
])
def test_run_refuses_a_stopped_finished_or_paused_project_with_exit_4_and_says_why(runw, capsys, status, reason,
                                                                                   said):
    bounds.set_status(runw.world.conn, "t3", status, reason)
    runw.passes = [_pass(finished=True)]

    assert cli.main(runw.argv()) == 4

    assert runw.run_calls == [] and runw.reconcile_calls == []  # nothing ran, not even reconcile
    assert said in _console(capsys)[1]


def test_run_tells_the_operator_that_swarm_resume_lifts_a_pause_or_a_stop(runw, capsys):
    bounds.set_status(runw.world.conn, "t3", "paused")
    cli.main(runw.argv())
    assert "swarm resume" in _console(capsys)[1]

    bounds.set_status(runw.world.conn, "t3", "stopped", "swarm stop")
    cli.main(runw.argv())
    assert "swarm resume lifts a stop" in _console(capsys)[1]


def test_run_after_swarm_resume_of_a_paused_project_goes_on(runw, capsys):
    bounds.set_status(runw.world.conn, "t3", "paused")
    bounds.set_status(runw.world.conn, "t3", "running")  # what swarm resume does
    runw.passes = [_pass(finished=True)]

    assert cli.main(runw.argv()) == 0


def test_run_prints_each_reconcile_repair_and_finding_and_goes_on(runw, capsys):
    runw.report = reconcile.ReconcileReport(
        findings=[reconcile.Inconsistency("T1", "worker_gone", "card w1 is running but its worker (pid 4242) is gone"),
                  reconcile.Inconsistency("T2", "orphan_worktree", f"worktree of card w9 {ACCENT} left in place")],
        repairs=[reconcile.Repair("T1", "worker_gone_reclaimed", "reclaim card w1: worker pid 4242 is gone", True),
                 reconcile.Repair("T1", "candidate_discarded", "candidate never landed; the queue redoes it", False)],
    )
    runw.passes = [_pass(finished=True)]

    assert cli.main(runw.argv()) == 0

    out, err = _console(capsys)
    assert "[RECONCILE] found T1 worker_gone: card w1 is running but its worker (pid 4242) is gone" in out
    assert "[RECONCILE] found T2 orphan_worktree: worktree of card w9 caf\\xe9 left in place" in out
    assert "[RECONCILE] repaired T1 worker_gone_reclaimed: reclaim card w1: worker pid 4242 is gone" in out
    assert "[RECONCILE] note T1 candidate_discarded: candidate never landed; the queue redoes it" in out
    assert "BLOCKED" not in out and err == ""


def test_run_refuses_with_exit_5_when_reconcile_leaves_something_it_could_not_repair(runw, capsys):
    blocked = reconcile.Inconsistency("T1", "missing_card", "work card w1 no longer resolves")
    runw.report = reconcile.ReconcileReport(findings=[blocked], blocked=[blocked])
    runw.passes = [_pass(finished=True)]

    assert cli.main(runw.argv()) == 5

    assert runw.run_calls == []  # nothing was dispatched
    out, err = _console(capsys)
    assert "[RECONCILE] BLOCKED T1 missing_card: work card w1 no longer resolves" in out
    assert "found T1 missing_card" not in out  # a blocked finding is shown once, as BLOCKED
    assert "REFUSED (ASES-REC-04)" in err and "1 item(s)" in err and "--ignore-reconcile" in err


def test_run_ignore_reconcile_overrides_loudly_and_goes_on(runw, capsys):
    blocked = reconcile.Inconsistency("T1", "missing_card", "work card w1 no longer resolves")
    runw.report = reconcile.ReconcileReport(findings=[blocked], blocked=[blocked])
    runw.passes = [_pass(finished=True)]

    assert cli.main(runw.argv("--ignore-reconcile")) == 0

    out, err = _console(capsys)
    assert "[RECONCILE] BLOCKED T1 missing_card" in out
    assert "WARNING: --ignore-reconcile given" in err and "NOT fixed" in err
    assert runw.run_calls == ["b"]


def test_run_treats_a_reconcile_that_crashes_as_blocked(runw, capsys):
    runw.report = RuntimeError("git is not installed")
    runw.passes = [_pass(finished=True)]

    assert cli.main(runw.argv()) == 5

    out, err = _console(capsys)
    assert "reconcile could not run: RuntimeError: git is not installed" in out
    assert runw.run_calls == []


def test_run_with_a_crashing_reconcile_can_still_be_overridden(runw, capsys):
    runw.report = RuntimeError("git is not installed")
    runw.passes = [_pass(finished=True)]

    assert cli.main(runw.argv("--ignore-reconcile")) == 0


def test_run_stops_with_exit_4_when_the_pass_says_the_project_is_stopped(runw, capsys):
    runw.passes = [_pass(stopped=True, stop_reason="project wall clock reached"), _pass(finished=True)]

    assert cli.main(runw.argv()) == 4

    assert len(runw.run_calls) == 1  # it did not poll on
    out, _ = _console(capsys)
    assert "[pass 1] STOPPED: project wall clock reached" in out and "swarm resume" in out


def test_run_reads_the_stop_reason_from_the_project_state_when_the_pass_gives_none(runw, monkeypatch, capsys):
    def stop_during_the_pass(*args, **kwargs):
        bounds.set_status(runw.world.conn, "t3", "stopped", "swarm stop")  # what swarm stop does meanwhile
        return _pass(stopped=True, stop_reason=None)

    monkeypatch.setattr(cli.controller_mod, "run_pass", stop_during_the_pass)

    assert cli.main(runw.argv()) == 4

    assert "STOPPED: swarm stop" in _console(capsys)[0]


def test_run_final_finished_ends_the_loop_with_exit_0_and_says_the_gates_are_green(runw, capsys):
    runw.passes = [_pass(merged=["T2"], final="finished", finished=True)]

    assert cli.main(runw.argv()) == 0

    out, _ = _console(capsys)
    assert "final=finished finished=True" in out
    assert "all merge cards done; Gates 4 and 5 are green and the release report is written" in out


def test_run_final_gate_failed_prints_the_question_and_exits_4(runw, capsys):
    runw.passes = [_pass(final="gate_failed", stopped=True,
                         stop_reason="Gate 4 failed on the integration HEAD: 2 secrets found. What should happen?")]

    assert cli.main(runw.argv()) == 4

    out, _ = _console(capsys)
    assert "FINAL GATE FAILED" in out and "the project is paused, not finished" in out
    assert "Gate 4 failed on the integration HEAD: 2 secrets found. What should happen?" in out
    assert "swarm resume" in out
    assert "STOPPED" not in out  # the gate failure is what is said, once


def test_run_final_gate_failed_reads_the_failing_gate_row_when_no_question_came_with_the_pass(runw, capsys):
    bounds.record_final_gate(runw.world.conn, "t3", "gate4", "0123456789abcdef", "fail",
                             detail=f"secret in config.py line 3\nsk-abcdefghijklmnopqrstuvwx\n{ACCENT}")
    runw.passes = [_pass(final="gate_failed")]

    assert cli.main(runw.argv()) == 4

    out, _ = _console(capsys)
    assert "final gate gate4 failed on 0123456789" in out
    assert "secret in config.py line 3" in out and "caf\\xe9" in out
    assert "sk-abcdefghijklmnopqrstuvwx" not in out  # redacted


def test_run_final_gate_failed_shows_at_most_five_detail_lines_each_cut_at_200_characters(runw, capsys):
    detail = "\n".join(["x" * 300] + [f"finding number {n}" for n in range(1, 9)])
    bounds.record_final_gate(runw.world.conn, "t3", "gate5", "0123456789abcdef", "fail", detail=detail)
    runw.passes = [_pass(final="gate_failed")]

    assert cli.main(runw.argv()) == 4

    lines = [ln for ln in _console(capsys)[0].splitlines() if ln.startswith("      ")]  # the indented detail lines
    assert lines == ["      " + "x" * 200] + [f"      finding number {n}" for n in range(1, 5)]  # five in all


def test_run_final_gate_failed_without_any_row_still_says_what_happened(runw, capsys):
    runw.passes = [_pass(final="gate_failed")]

    assert cli.main(runw.argv()) == 4

    assert "a final gate failed (Gate 4 or Gate 5)" in _console(capsys)[0]


def test_run_a_final_that_is_not_ready_or_errored_does_not_end_the_loop(runw, capsys):
    runw.passes = [_pass(final="not_ready"), _pass(final="error"), _pass(final="finished", finished=True)]

    assert cli.main(runw.argv()) == 0

    out, _ = _console(capsys)
    assert "final=not_ready" in out and "final=error" in out and len(runw.run_calls) == 3


def test_run_ctrl_c_in_a_pass_prints_one_line_and_exits_130(runw, capsys):
    runw.passes = [_pass(), KeyboardInterrupt()]

    assert cli.main(runw.argv()) == 130

    _, err = _console(capsys)
    assert err.count("\n") == 1 and "interrupted (Ctrl-C)" in err and "Traceback" not in err


def test_run_ctrl_c_while_sleeping_between_passes_exits_130(runw, monkeypatch, capsys):
    runw.passes = [_pass(), _pass(finished=True)]

    def sleep(seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", sleep)

    assert cli.main(runw.argv()) == 130

    assert len(runw.run_calls) == 1


def test_run_ctrl_c_during_reconcile_exits_130(runw, capsys):
    runw.report = KeyboardInterrupt()

    assert cli.main(runw.argv()) == 130


def test_run_exit_code_1_when_the_iteration_bound_is_reached(runw, capsys):
    runw.passes = [_pass()] * 5

    assert cli.main(runw.argv()) == 1

    out, _ = _console(capsys)
    assert "stopped after 5 passes without finishing (not a failure" in out and len(runw.run_calls) == 5


def test_run_exit_code_1_when_the_gate_profiles_changed_since_approval(runw, monkeypatch, capsys):
    def tampered(conn, project, gate_profiles, *rest):
        raise controller.GateConfigTamperedError("the gate profiles differ from the ones approved")

    monkeypatch.setattr(cli.controller_mod, "verify_gate_pin", tampered)

    assert cli.main(runw.argv()) == 1

    assert "REFUSED (ASES-QG-02)" in _console(capsys)[1] and runw.reconcile_calls == []


def test_run_exit_code_2_after_five_failed_passes_in_a_row(runw, capsys):
    runw.passes = [RuntimeError("boom")] * 5

    assert cli.main(runw.argv("--max-iterations", "9")) == 2

    assert "stopping after 5 failed passes in a row" in _console(capsys)[1]


def test_run_exit_code_3_when_the_primary_checkout_is_not_trusted(runw, monkeypatch, capsys):
    monkeypatch.setattr(guards, "check_primary_checkout", lambda *a, **kw: guards.GuardResult(
        False, ("primary checkout is dirty: ?? stray.txt",), "abc", "integration"))
    runw.passes = [_pass(finished=True)]

    assert cli.main(runw.argv()) == 3

    assert "REFUSED (ASES-GIT-12)" in _console(capsys)[1]
    assert runw.reconcile_calls == [] and bounds.get_state(runw.world.conn, "t3") is None  # not started either


def test_run_exit_code_3_on_a_security_event_in_a_pass(runw, capsys):
    runw.passes = [_pass(integrity=["primary checkout is dirty: ?? stray.txt"])]

    assert cli.main(runw.argv()) == 3

    assert "SECURITY EVENT" in _console(capsys)[1]


def test_run_gate_0_failure_is_a_clean_exit_1_not_a_traceback(runw, capsys):
    runw.world.plan_file.write_text("{ not json", encoding="utf-8")

    assert cli.main(runw.argv()) == 1

    assert "Gate 0 FAILED:" in _console(capsys)[0]


def test_run_pass_line_gains_the_new_counters_only_when_they_are_not_zero(runw, capsys):
    runw.passes = [
        _pass(unparked=["T2"], recovery=[{"task_key": "T1", "action": "fresh_attempt", "kind": "capability"}],
              warnings=["idle worktree changed", "another"], provisioned=["w1"]),
        _pass(unparked=[], recovery=[], warnings=[], provisioned=[], final=None, finished=True),
    ]

    assert cli.main(runw.argv()) == 0

    first, second = [ln for ln in _console(capsys)[0].splitlines() if " finished=" in ln]
    assert first == ("[pass 1] parked=[] merged=[] sent_back=[] unparked=['T2'] recovery=1 warnings=2 "
                     "provisioned=1 finished=False")
    assert second == "[pass 2] parked=[] merged=[] sent_back=[] finished=True"


def test_run_prints_warnings_recovery_decisions_and_unparked_cards_one_per_line(runw, capsys):
    runw.passes = [_pass(
        warnings=[f"idle worktree of card w1 changed {ACCENT}"],
        recovery=[{"task_key": "T1", "action": "switch_model", "kind": "capability"}],
        unparked=["T2", "T3"],
    ), _pass(finished=True)]

    assert cli.main(runw.argv()) == 0

    out, err = _console(capsys)
    assert "[pass 1] WARNING: idle worktree of card w1 changed caf\\xe9" in out
    assert "[pass 1] recovery: T1 switch_model (kind capability)" in out
    assert "[pass 1] unparked: T2, T3" in out
    assert out.isascii() and err == ""  # a warning never halts and is not an error


def test_run_works_with_the_old_summary_shape_that_lacks_the_new_keys(runw, capsys):
    runw.passes = [{"parked": [], "merged": ["T1"], "sent_back": [], "finished": True}]

    assert cli.main(runw.argv()) == 0


def test_run_output_is_ascii_for_reconcile_and_pass_errors(runw, capsys):
    runw.report = reconcile.ReconcileReport(repairs=[reconcile.Repair("T1", "x", f"card {EMOJI} {ARROW}", True)])
    runw.passes = [RuntimeError(f"boom {ACCENT}"), _pass(finished=True)]

    assert cli.main(runw.argv()) == 0

    out, err = _console(capsys)
    assert out.isascii() and err.isascii() and "boom caf\\xe9" in err


# ---------------------------------------------------------------------------------------------
# stop (ASES-REC-06)
# ---------------------------------------------------------------------------------------------


def _fake_stop_all(monkeypatch, report=None, raises=None):
    calls = []

    def fake(board, plan, *, conn, reason="swarm stop", deadline_seconds=30.0, **kwargs):
        calls.append(types.SimpleNamespace(board=board, project=plan.project, plan=plan, reason=reason,
                                           deadline=deadline_seconds))
        if raises is not None:
            raise raises
        return report if report is not None else killswitch.StopReport(
            started_at="2026-09-22T10:00:00+00:00", finished_at="2026-09-22T10:00:04+00:00", seconds=3.2,
            paused=True, flag_set=True, reclaimed=["w1", "w2"], killed=[{"card_id": "w1", "pid": 4101}],
            containers_stopped=["ases-w1"], within_deadline=True)

    monkeypatch.setattr(cli.killswitch_mod, "stop_all", fake)
    return calls


def _add_plan_tasks(conn, *projects):
    for project in projects:
        conn.execute("INSERT INTO plan_tasks (project, task_key, work_card_id, merge_card_id, role, created_at) "
                     "VALUES (?, 'T1', ?, ?, 'coder', datetime('now'))", (project, f"w-{project}", f"m-{project}"))


def test_stop_stops_the_plan_prints_a_compact_summary_and_writes_the_report_outside_the_repo(world, monkeypatch, capsys):
    calls = _fake_stop_all(monkeypatch)

    assert cli.main(["stop", "--repo", str(world.repo)]) == 0

    (call,) = calls
    assert (call.board, call.project, call.reason) == ("b", "t3", "swarm stop")
    out, _ = _console(capsys)
    assert ("swarm stop [project t3]: paused=yes flag=yes reclaimed=2 killed=1 unverified=0 "
            "containers_stopped=1 seconds=3.2 within_deadline=yes") in out
    (path,) = list((world.project.ases_home / "stops" / "ases").glob("*/stop-*.json"))
    assert f"stop report: {path}" in out
    assert not cli._is_inside(path, world.repo)
    assert json.loads(path.read_text(encoding="utf-8"))["reclaimed"] == ["w1", "w2"]
    assert _events(world.conn, "swarm_stop")[0]["project"] == "t3"


def test_stop_passes_the_reason(world, monkeypatch):
    calls = _fake_stop_all(monkeypatch)

    cli.main(["stop", "--repo", str(world.repo), "--reason", "user pulled the plug"])

    assert calls[0].reason == "user pulled the plug"


def test_stop_exits_1_when_it_could_not_finish_within_the_deadline(world, monkeypatch, capsys):
    report = killswitch.StopReport(paused=True, flag_set=True, seconds=41.0, within_deadline=False,
                                   notes=["the time limit was reached"])
    _fake_stop_all(monkeypatch, report)

    assert cli.main(["stop", "--repo", str(world.repo)]) == 1

    out, _ = _console(capsys)
    assert "within_deadline=NO" in out and "note: the time limit was reached" in out
    assert list((world.project.ases_home / "stops").glob("*/*/stop-*.json")) or \
        list((world.project.ases_home / "stops").rglob("stop-*.json"))  # the report is written even then


def test_stop_says_loudly_when_dispatch_was_not_paused_or_the_flag_was_not_set(world, monkeypatch, capsys):
    report = killswitch.StopReport(paused=False, flag_set=False, within_deadline=True,
                                   notes=["hermes pause failed: no answer within 10.0s"])
    _fake_stop_all(monkeypatch, report)

    code = cli.main(["stop", "--repo", str(world.repo)])

    out, _ = _console(capsys)
    assert "paused=NO flag=NO" in out
    assert "WARNING: hermes pause did not succeed, so new cards may still be dispatched" in out
    assert "WARNING: the stop flag is not set" in out
    assert code == 0  # the exit code follows within_deadline (ASES-REC-06), the warnings say the rest


def test_stop_lists_what_was_left_alone_and_why(world, monkeypatch, capsys):
    report = killswitch.StopReport(
        paused=True, flag_set=True, within_deadline=True,
        unverified=[{"card_id": "w1", "pid": 4101, "why": "command line does not contain the card id"}],
        reclaim_errors=[{"card_id": "w2", "error": f"hermes said no {ACCENT}"}])
    _fake_stop_all(monkeypatch, report)

    cli.main(["stop", "--repo", str(world.repo)])

    out, _ = _console(capsys)
    assert "unverified=1" in out
    assert "left alone: card w1 pid 4101: command line does not contain the card id" in out
    assert "reclaim error: w2 hermes said no caf\\xe9" in out and out.isascii()


def test_stop_never_raises_when_the_kill_switch_itself_blows_up(world, monkeypatch, capsys):
    _fake_stop_all(monkeypatch, raises=RuntimeError("the sky fell"))

    assert cli.main(["stop", "--repo", str(world.repo)]) == 1

    _, err = _console(capsys)
    assert err == "swarm stop failed: RuntimeError: the sky fell\n"


def test_stop_never_raises_even_when_the_configuration_is_broken(monkeypatch, capsys):
    def broken():
        raise config.ConfigError("swarm.yaml is missing required key: project.name")

    monkeypatch.setattr(cli, "_load_project", broken)

    assert cli.main(["stop"]) == 1  # not 2: a stop is never reported as a config problem

    assert "swarm stop failed: ConfigError" in _console(capsys)[1]


def test_stop_reports_a_stop_report_that_cannot_be_written_but_still_prints_the_summary(world, monkeypatch, capsys):
    _fake_stop_all(monkeypatch)

    def cannot_write(report, directory):
        raise OSError("the disk is full")

    monkeypatch.setattr(cli.killswitch_mod, "write_stop_report", cannot_write)

    assert cli.main(["stop", "--repo", str(world.repo)]) == 0  # the stop itself succeeded

    out, err = _console(capsys)
    assert "swarm stop [project t3]" in out and "could not write the stop report: the disk is full" in err


def test_stop_without_repo_stops_every_project_found_in_plan_tasks(world, monkeypatch, capsys):
    _add_plan_tasks(world.conn, "alpha", "beta")
    calls = _fake_stop_all(monkeypatch)

    assert cli.main(["stop"]) == 0

    assert [c.project for c in calls] == ["alpha", "beta"]
    assert all(c.board == "b" for c in calls)
    out, _ = _console(capsys)
    assert "swarm stop [project alpha]" in out and "swarm stop [project beta]" in out
    assert len(list((world.project.ases_home / "stops").rglob("stop-*.json"))) == 2  # one report each


def test_stop_without_repo_and_with_no_projects_still_pauses_under_the_configured_name(world, monkeypatch):
    calls = _fake_stop_all(monkeypatch)

    assert cli.main(["stop"]) == 0

    assert [c.project for c in calls] == ["ases"]


def test_stop_stand_in_plan_has_only_what_the_kill_switch_reads(world, monkeypatch):
    _add_plan_tasks(world.conn, "alpha")
    calls = _fake_stop_all(monkeypatch)

    cli.main(["stop"])

    assert calls[0].plan.project == "alpha"


def test_stop_gives_each_further_project_only_what_is_left_of_the_30_seconds(world, monkeypatch):
    _add_plan_tasks(world.conn, "alpha", "beta")
    calls = _fake_stop_all(monkeypatch)
    monkeypatch.setattr(cli.time, "monotonic", _fake_clock(0.0, 0.0, 12.0))  # start, first project, second project

    cli.main(["stop"])

    assert calls[0].deadline == 30.0 and calls[1].deadline == pytest.approx(18.0)


def test_stop_with_a_plan_that_fails_gate_0_falls_back_to_the_project_named_in_the_file(world, monkeypatch, capsys):
    world.plan_file.write_text(json.dumps({"project": "t3", "tasks": "not a list"}), encoding="utf-8")
    calls = _fake_stop_all(monkeypatch)

    assert cli.main(["stop", "--repo", str(world.repo)]) == 0

    assert [c.project for c in calls] == ["t3"]


def test_stop_with_a_plan_file_that_cannot_be_read_falls_back_to_every_project(world, monkeypatch):
    world.plan_file.write_bytes(b"\xff\xfe not utf-8 \x00")
    _add_plan_tasks(world.conn, "alpha")
    calls = _fake_stop_all(monkeypatch)

    assert cli.main(["stop", "--repo", str(world.repo)]) == 0

    assert [c.project for c in calls] == ["alpha"]


def test_stop_against_the_real_kill_switch_sets_the_flag_pauses_and_writes_the_report(world, monkeypatch, capsys):
    """The real killswitch.stop_all with Hermes faked and no cards: no process or container is touched."""
    paused = []
    monkeypatch.setattr(hermes, "pause", lambda reason=None, timeout=20: paused.append(reason))

    assert cli.main(["stop", "--repo", str(world.repo), "--reason", "operator stop"]) == 0

    assert paused == ["operator stop"]
    assert killswitch.stop_requested(world.conn, "t3")
    assert bounds.get_state(world.conn, "t3")["stop_reason"] == "operator stop"
    out, _ = _console(capsys)
    assert "paused=yes flag=yes" in out and "within_deadline=yes" in out
    assert len(list((world.project.ases_home / "stops").rglob("stop-*.json"))) == 1


# ---------------------------------------------------------------------------------------------
# resume (ASES-REC-06)
# ---------------------------------------------------------------------------------------------


def _resume_world(world, monkeypatch, report=None):
    """The real killswitch.resume_all with Hermes and reconcile faked."""
    seen = types.SimpleNamespace(reconciled=[], resumed=0)
    monkeypatch.setattr(hermes, "resume", lambda timeout=20: setattr(seen, "resumed", seen.resumed + 1))

    def fake_reconcile(board, repo, plan, *, conn, apply=True, **kwargs):
        seen.reconciled.append((board, pathlib.Path(repo), plan.project, apply))
        if isinstance(report, BaseException):
            raise report
        return report if report is not None else reconcile.ReconcileReport()

    monkeypatch.setattr(reconcile, "reconcile", fake_reconcile)
    return seen


def test_resume_reconciles_first_lifts_the_stop_and_says_what_happened(world, monkeypatch, capsys):
    killswitch.request_stop(world.conn, "t3", "swarm stop")
    seen = _resume_world(world, monkeypatch)

    assert cli.main(["resume", "--repo", str(world.repo)]) == 0

    assert seen.reconciled == [("b", world.repo.resolve(), "t3", True)]  # a real reconcile, repairs applied
    assert seen.resumed == 1
    assert not killswitch.stop_requested(world.conn, "t3")
    assert "project t3: resumed" in _console(capsys)[0]
    assert _events(world.conn, "swarm_resume")[0]["project"] == "t3"


def test_resume_prints_the_reconcile_repairs(world, monkeypatch, capsys):
    killswitch.request_stop(world.conn, "t3")
    report = reconcile.ReconcileReport(
        repairs=[reconcile.Repair("T1", "worker_gone_reclaimed", f"reclaim card w1 {ACCENT}", True)])
    _resume_world(world, monkeypatch, report)

    assert cli.main(["resume", "--repo", str(world.repo)]) == 0

    assert "[RECONCILE] repaired T1 worker_gone_reclaimed: reclaim card w1 caf\\xe9" in _console(capsys)[0]


def test_resume_with_blocked_findings_keeps_the_system_stopped_and_prints_them(world, monkeypatch, capsys):
    killswitch.request_stop(world.conn, "t3", "swarm stop")
    blocked = reconcile.Inconsistency("T1", "missing_card", "work card w1 no longer resolves")
    seen = _resume_world(world, monkeypatch, reconcile.ReconcileReport(findings=[blocked], blocked=[blocked]))

    assert cli.main(["resume", "--repo", str(world.repo)]) == 1

    assert seen.resumed == 0  # Hermes was not resumed
    assert killswitch.stop_requested(world.conn, "t3")  # and the stop flag stays set
    out, err = _console(capsys)
    assert "[RECONCILE] BLOCKED T1 missing_card: work card w1 no longer resolves" in out
    assert "project t3: NOT resumed" in err and "need a person" in err


def test_resume_with_a_reconcile_that_crashes_keeps_the_system_stopped(world, monkeypatch, capsys):
    killswitch.request_stop(world.conn, "t3")
    seen = _resume_world(world, monkeypatch, RuntimeError("git is gone"))

    assert cli.main(["resume", "--repo", str(world.repo)]) == 1

    assert seen.resumed == 0 and killswitch.stop_requested(world.conn, "t3")
    assert "NOT resumed" in _console(capsys)[1]


def test_resume_when_hermes_refuses_leaves_the_project_stopped(world, monkeypatch, capsys):
    killswitch.request_stop(world.conn, "t3")
    _resume_world(world, monkeypatch)

    def refuse(timeout=20):
        raise hermes.HermesCommandError(["resume"], 1, "gateway not reachable")

    monkeypatch.setattr(hermes, "resume", refuse)

    assert cli.main(["resume", "--repo", str(world.repo)]) == 1

    assert killswitch.stop_requested(world.conn, "t3")
    assert "hermes resume failed" in _console(capsys)[1]


def test_resume_extend_minutes_moves_the_deadline_before_anything_else(world, monkeypatch, capsys):
    killswitch.request_stop(world.conn, "t3")
    _resume_world(world, monkeypatch)
    before = datetime.now(timezone.utc)

    assert cli.main(["resume", "--repo", str(world.repo), "--extend-minutes", "30"]) == 0

    deadline = datetime.fromisoformat(bounds.get_state(world.conn, "t3")["deadline_at"])
    assert timedelta(minutes=29) < deadline - before < timedelta(minutes=31)
    out, _ = _console(capsys)
    assert f"project t3: deadline set to {deadline.isoformat(timespec='seconds')} (30 minutes from now)" in out


def test_resume_extends_the_deadline_even_when_the_resume_is_then_refused(world, monkeypatch, capsys):
    killswitch.request_stop(world.conn, "t3")
    blocked = reconcile.Inconsistency("T1", "missing_card", "gone")
    _resume_world(world, monkeypatch, reconcile.ReconcileReport(findings=[blocked], blocked=[blocked]))

    assert cli.main(["resume", "--repo", str(world.repo), "--extend-minutes", "30"]) == 1

    assert bounds.get_state(world.conn, "t3")["deadline_at"] is not None


def test_resume_sets_a_paused_project_back_to_running(world, monkeypatch, capsys):
    bounds.start_project(world.conn, "t3")
    bounds.set_status(world.conn, "t3", "paused", "project wall clock reached")
    _resume_world(world, monkeypatch)

    assert cli.main(["resume", "--repo", str(world.repo)]) == 0

    assert bounds.get_state(world.conn, "t3")["status"] == "running"
    out = _console(capsys)[0]
    assert "project t3: was paused, now running" in out
    assert "(reason: project wall clock reached)" in out  # ASES-CTL-01: the pause reason is shown on resume too


def test_resume_says_nothing_extra_when_the_pause_had_no_recorded_reason(world, monkeypatch, capsys):
    bounds.start_project(world.conn, "t3")
    bounds.set_status(world.conn, "t3", "paused")
    _resume_world(world, monkeypatch)

    assert cli.main(["resume", "--repo", str(world.repo)]) == 0

    assert "project t3: was paused, now running" in _console(capsys)[0]
    assert "(reason:" not in _console(capsys)[0]


def test_resume_a_stopped_project_returns_to_running_when_it_had_started(world, monkeypatch):
    bounds.start_project(world.conn, "t3")
    killswitch.request_stop(world.conn, "t3", "swarm stop")
    _resume_world(world, monkeypatch)

    cli.main(["resume", "--repo", str(world.repo)])

    state = bounds.get_state(world.conn, "t3")
    assert state["status"] == "running" and state["stop_reason"] is None


def test_resume_warns_when_the_deadline_has_already_passed(world, monkeypatch, capsys):
    bounds.start_project(world.conn, "t3")
    bounds.set_deadline(world.conn, "t3", "2020-01-01T00:00:00+00:00")
    bounds.set_status(world.conn, "t3", "paused")
    _resume_world(world, monkeypatch)

    assert cli.main(["resume", "--repo", str(world.repo)]) == 0

    out, _ = _console(capsys)
    assert "WARNING: the project deadline (2020-01-01T00:00:00+00:00) has already passed" in out
    assert "--extend-minutes" in out


def test_resume_without_repo_resumes_every_project_and_says_reconcile_was_not_run(world, monkeypatch, capsys):
    _add_plan_tasks(world.conn, "alpha", "beta")
    killswitch.request_stop(world.conn, "alpha")
    killswitch.request_stop(world.conn, "beta")
    seen = _resume_world(world, monkeypatch)

    assert cli.main(["resume"]) == 0

    assert seen.reconciled == []  # there is no repository to reconcile against
    assert seen.resumed == 2
    assert not killswitch.stop_requested(world.conn, "alpha") and not killswitch.stop_requested(world.conn, "beta")
    out, _ = _console(capsys)
    assert out.count("reconcile-on-start was NOT run here") == 2 and "swarm run reconciles" in out
    assert "project alpha: resumed" in out and "project beta: resumed" in out


def test_resume_with_a_plan_that_fails_gate_0_does_nothing(world, monkeypatch, capsys):
    killswitch.request_stop(world.conn, "t3")
    seen = _resume_world(world, monkeypatch)
    world.plan_file.write_text(json.dumps({**PLAN_RAW, "tasks": []}), encoding="utf-8")

    assert cli.main(["resume", "--repo", str(world.repo)]) == 1

    assert seen.resumed == 0 and killswitch.stop_requested(world.conn, "t3")
    assert "Gate 0 FAILED:" in _console(capsys)[0]


@pytest.mark.parametrize("value", ["0", "-1", "later"])
def test_resume_refuses_an_extension_that_is_not_a_positive_number(value):
    with pytest.raises(SystemExit) as caught:
        cli.main(["resume", "--extend-minutes", value])

    assert caught.value.code == 2


# ---------------------------------------------------------------------------------------------
# init (ASES-ROL-*, ASES-ARC-08, ASES-SEC-03)
# ---------------------------------------------------------------------------------------------


class _Change:
    """What profiles.Change offers the CLI: line() for the terminal, and `actionable` (False for a warning row)."""

    def __init__(self, text, actionable=True):
        self.text = text
        self.actionable = actionable

    def line(self):
        return self.text


def _profiles_stub(monkeypatch, *, changes=None, plan_takes_reuse=False, apply_takes_reuse=False,
                   apply_takes_conn=True, result=None, residual_risks=None):
    calls = types.SimpleNamespace(plan=[], apply=[])
    listed = list(changes if changes is not None else [
        _Change(f"create_profile coder-2: hermes profile create coder-2 {ACCENT}"),
        _Change("set_config lead: model.default -> glm-5.3"),
    ])

    def plan_init(project, models_config, hermes_home, prompts_dir, *, sandbox_enabled=False, include_global=False,
                  include_inactive=False, policy=None):
        calls.plan.append(dict(project=project, models_config=models_config, hermes_home=hermes_home,
                               prompts_dir=prompts_dir, sandbox_enabled=sandbox_enabled,
                               include_global=include_global, include_inactive=include_inactive, policy=policy))
        return list(listed)

    def outcome(changes):
        return result if result is not None else types.SimpleNamespace(
            applied=list(changes), failed=[], backups=[], credential_names_copied=[], skipped=[])

    if apply_takes_conn:
        def apply_init(changes, hermes_home, prompts_dir, *, confirmed=False, runner=None, now=None, conn=None):
            calls.apply.append(dict(changes=changes, hermes_home=hermes_home, prompts_dir=prompts_dir,
                                    confirmed=confirmed, conn=conn))
            return outcome(changes)
    else:
        def apply_init(changes, hermes_home, prompts_dir, *, confirmed=False, runner=None, now=None):
            calls.apply.append(dict(changes=changes, hermes_home=hermes_home, prompts_dir=prompts_dir,
                                    confirmed=confirmed, conn=None))
            return outcome(changes)

    if plan_takes_reuse:
        original_plan = plan_init

        def plan_init(project, models_config, hermes_home, prompts_dir, *, reuse_credentials_from=None, **kw):
            calls.plan_reuse = reuse_credentials_from
            return original_plan(project, models_config, hermes_home, prompts_dir, **kw)

    if apply_takes_reuse:
        original_apply = apply_init

        def apply_init(changes, hermes_home, prompts_dir, *, reuse_credentials_from=None, **kw):
            calls.apply_reuse = reuse_credentials_from
            return original_apply(changes, hermes_home, prompts_dir, **kw)

    stub_kwargs = dict(plan_init=plan_init, apply_init=apply_init)
    if residual_risks is not None:
        stub_kwargs["residual_risks"] = lambda: list(residual_risks)
    _stub(monkeypatch, "profiles", **stub_kwargs)
    return calls


def test_init_dry_run_prints_the_change_list_one_line_each_and_writes_nothing(world, monkeypatch, capsys):
    calls = _profiles_stub(monkeypatch)

    assert cli.main(["init"]) == 0

    assert calls.apply == []
    out, _ = _console(capsys)
    lines = out.splitlines()
    assert lines[0] == f"swarm init: 2 change(s) planned for the Hermes profiles under {world.project.hermes_native_home} (dry run)"
    assert lines[1] == "  create_profile coder-2: hermes profile create coder-2 caf\\xe9"
    assert lines[2] == "  set_config lead: model.default -> glm-5.3"
    assert "Nothing was written. To make these changes run swarm init --apply --yes" in out


def test_init_reads_the_real_hermes_home_and_prompts_directory_from_the_config(world, monkeypatch):
    calls = _profiles_stub(monkeypatch)

    cli.main(["init"])

    (call,) = calls.plan
    assert call["hermes_home"] == world.project.hermes_native_home
    assert call["prompts_dir"] == cli._repo_root() / "prompts"
    assert call["project"] is world.project and call["models_config"] == MODELS_CONFIG


def test_init_flags_default_to_off(world, monkeypatch):
    calls = _profiles_stub(monkeypatch)

    cli.main(["init"])

    call = calls.plan[0]
    assert (call["sandbox_enabled"], call["include_global"], call["include_inactive"], call["policy"]) == \
        (False, False, False, None)


def test_init_global_sandbox_and_include_inactive_are_passed_on(world, monkeypatch):
    calls = _profiles_stub(monkeypatch)

    cli.main(["init", "--global", "--sandbox", "--include-inactive"])

    call = calls.plan[0]
    assert call["include_global"] is True and call["include_inactive"] is True and call["sandbox_enabled"] is True
    assert call["policy"].network is False  # the SandboxPolicy of the config's sandbox: block


def test_init_takes_the_sandbox_from_the_config_when_it_is_enabled_there(world, monkeypatch):
    project = _project(world.tmp, sandbox={**config.DEFAULT_SANDBOX, "enabled": True, "image": "python:3.11"})
    monkeypatch.setattr(cli, "_load_project", lambda: project)
    calls = _profiles_stub(monkeypatch)

    cli.main(["init"])

    assert calls.plan[0]["sandbox_enabled"] is True and calls.plan[0]["policy"].image == "python:3.11"


def test_init_refuses_a_sandbox_block_the_policy_cannot_be_built_from(world, monkeypatch, capsys):
    project = _project(world.tmp, sandbox={"enabled": True, "network_default": True})
    monkeypatch.setattr(cli, "_load_project", lambda: project)
    calls = _profiles_stub(monkeypatch)

    assert cli.main(["init"]) == 1

    assert calls.plan == [] and "network_default" in _console(capsys)[1]


def test_init_with_nothing_to_change_says_so_and_exits_0(world, monkeypatch, capsys):
    calls = _profiles_stub(monkeypatch, changes=[])

    assert cli.main(["init", "--apply", "--yes"]) == 0

    assert calls.apply == []
    assert "already in the desired state; nothing to do." in _console(capsys)[0]


def test_init_apply_without_yes_is_refused_with_an_explanation_and_changes_nothing(world, monkeypatch, capsys):
    calls = _profiles_stub(monkeypatch)

    assert cli.main(["init", "--apply"]) == 1

    assert calls.apply == []
    out, err = _console(capsys)
    assert "  set_config lead: model.default -> glm-5.3" in out  # the list is shown so --yes is an informed choice
    assert "--apply REFUSED" in err and "real Hermes profiles" in err and "--yes" in err


def test_init_apply_yes_applies_exactly_the_planned_changes_with_confirmed_true(world, monkeypatch, capsys):
    result = types.SimpleNamespace(
        applied=["a", "b"], failed=[], backups=["C:/h/profiles/lead/config.yaml.ases-bak-20260922T100000Z"],
        credential_names_copied=["XKIRO_API_KEY"])
    calls = _profiles_stub(monkeypatch, result=result)

    assert cli.main(["init", "--apply", "--yes"]) == 0

    (call,) = calls.apply
    assert call["confirmed"] is True
    assert [c.text for c in call["changes"]] == [f"create_profile coder-2: hermes profile create coder-2 {ACCENT}",
                                                 "set_config lead: model.default -> glm-5.3"]
    assert call["hermes_home"] == world.project.hermes_native_home
    out, _ = _console(capsys)
    assert "applied 2 change(s), 0 failed" in out
    assert "backup: C:/h/profiles/lead/config.yaml.ases-bak-20260922T100000Z" in out
    assert "credentials copied (names only): XKIRO_API_KEY" in out


def test_init_apply_reports_each_failure_and_exits_1(world, monkeypatch, capsys):
    result = types.SimpleNamespace(applied=["a"], failed=[_Change("write_soul reviewer: file is locked")],
                                   backups=[], credential_names_copied=[])
    _profiles_stub(monkeypatch, result=result)

    assert cli.main(["init", "--apply", "--yes"]) == 1

    out, _ = _console(capsys)
    assert "applied 1 change(s), 1 failed" in out and "FAILED: write_soul reviewer: file is locked" in out


def test_init_yes_alone_does_not_apply(world, monkeypatch):
    calls = _profiles_stub(monkeypatch)

    assert cli.main(["init", "--yes"]) == 0

    assert calls.apply == []


def test_init_reuse_credentials_goes_to_whichever_function_takes_it(world, monkeypatch):
    calls = _profiles_stub(monkeypatch, plan_takes_reuse=True, apply_takes_reuse=True)

    assert cli.main(["init", "--apply", "--yes", "--reuse-credentials-from", "coder-1"]) == 0

    assert calls.plan_reuse == "coder-1" and calls.apply_reuse == "coder-1"


def test_init_reuse_credentials_can_live_in_apply_init_alone(world, monkeypatch):
    calls = _profiles_stub(monkeypatch, apply_takes_reuse=True)

    assert cli.main(["init", "--apply", "--yes", "--reuse-credentials-from", "coder-1"]) == 0

    assert calls.apply_reuse == "coder-1" and not hasattr(calls, "plan_reuse")


def test_init_refuses_reuse_credentials_when_the_profiles_module_has_no_such_option(world, monkeypatch, capsys):
    calls = _profiles_stub(monkeypatch)

    assert cli.main(["init", "--apply", "--yes", "--reuse-credentials-from", "coder-1"]) == 1

    assert calls.plan == [] and calls.apply == []  # not silently ignored, and nothing was planned or changed
    assert "does not support --reuse-credentials-from" in _console(capsys)[1]


def test_init_never_passes_reuse_credentials_unless_asked(world, monkeypatch):
    calls = _profiles_stub(monkeypatch, plan_takes_reuse=True, apply_takes_reuse=True)

    cli.main(["init", "--apply", "--yes"])

    assert calls.plan_reuse is None and calls.apply_reuse is None


def test_init_a_converged_home_has_only_warnings_and_there_is_nothing_to_apply(world, monkeypatch, capsys):
    """profiles.plan_init returns warning rows even for a fully converged home (the sandbox is off): they are shown
    but they are not changes."""
    calls = _profiles_stub(monkeypatch, changes=[
        _Change("lead: warning sandbox: the sandbox is not enabled", actionable=False),
    ])

    assert cli.main(["init", "--apply", "--yes"]) == 0

    assert calls.apply == []
    lines = _console(capsys)[0].splitlines()
    assert lines[0] == f"swarm init: 0 change(s) planned for the Hermes profiles under " \
                       f"{world.project.hermes_native_home}, 1 warning(s)"
    assert lines[1] == "  lead: warning sandbox: the sandbox is not enabled"
    assert lines[2] == ("The Hermes profiles are already in the desired state; nothing to do. "
                        "The warnings above are conditions ASES reports but does not change.")


def test_init_counts_warnings_apart_from_changes_and_hands_apply_init_every_row(world, monkeypatch, capsys):
    calls = _profiles_stub(monkeypatch, changes=[
        _Change("coder-2: create_profile x"), _Change("coder-2: warning credentials: needs credentials from the user",
                                                      actionable=False),
    ])

    assert cli.main(["init"]) == 0

    out, _ = _console(capsys)
    assert out.splitlines()[0] == (f"swarm init: 1 change(s) planned for the Hermes profiles under "
                                   f"{world.project.hermes_native_home}, 1 warning(s) (dry run)")

    assert cli.main(["init", "--apply", "--yes"]) == 0
    assert len(calls.apply[0]["changes"]) == 2  # apply_init itself skips the warning rows and reports them


def test_init_apply_prints_what_apply_init_skipped(world, monkeypatch, capsys):
    result = types.SimpleNamespace(
        applied=["a"], failed=[], backups=[], credential_names_copied=[],
        skipped=[_Change("coder-2: warning credentials: needs credentials from the user", actionable=False)])
    _profiles_stub(monkeypatch, result=result)

    assert cli.main(["init", "--apply", "--yes"]) == 0

    out, _ = _console(capsys)
    assert "applied 1 change(s), 0 failed" in out
    assert "  skipped: coder-2: warning credentials: needs credentials from the user" in out


def test_init_gives_apply_init_a_connection_for_its_audit_event_when_it_takes_one(world, monkeypatch):
    calls = _profiles_stub(monkeypatch)

    cli.main(["init", "--apply", "--yes"])

    assert calls.apply[0]["conn"] is not None
    calls.apply[0]["conn"].execute("SELECT 1")  # a real, usable connection to the ASES database


def test_init_does_not_pass_a_connection_apply_init_does_not_take(world, monkeypatch):
    calls = _profiles_stub(monkeypatch, apply_takes_conn=False)

    assert cli.main(["init", "--apply", "--yes"]) == 0

    assert calls.apply[0]["conn"] is None


def test_init_dry_run_opens_no_database_connection(world, monkeypatch):
    calls = _profiles_stub(monkeypatch)
    monkeypatch.setattr(cli, "_open_conn", lambda project: pytest.fail("a dry run must not open the database"))

    assert cli.main(["init"]) == 0

    assert calls.apply == []


def test_init_without_the_profiles_module_is_one_line_and_exit_1(world, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "ases.profiles", None)

    assert cli.main(["init"]) == 1

    assert "'profiles'" in _console(capsys)[1]


# --- residual risks (ASES-ROL-05): profiles.residual_risks() printed next to swarm init's plan -------------------


def test_init_prints_residual_risks_after_the_change_list(world, monkeypatch, capsys):
    risks = ["Reviewer file access: keeps write tools its prompt forbids it to use.",
             "Kanban toolset: appended to every dispatcher-spawned worker regardless of profile."]
    _profiles_stub(monkeypatch, residual_risks=risks)

    assert cli.main(["init"]) == 0

    out, _ = _console(capsys)
    assert "Known limits swarm init cannot fix (ASES-ROL-05):" in out
    assert f"  - {risks[0]}" in out and f"  - {risks[1]}" in out


def test_init_apply_also_prints_residual_risks(world, monkeypatch, capsys):
    risks = ["Reviewer file access: keeps write tools its prompt forbids it to use."]
    _profiles_stub(monkeypatch, residual_risks=risks)

    assert cli.main(["init", "--apply", "--yes"]) == 0

    assert f"  - {risks[0]}" in _console(capsys)[0]


def test_init_prints_nothing_extra_when_there_are_no_residual_risks(world, monkeypatch, capsys):
    _profiles_stub(monkeypatch, residual_risks=[])

    assert cli.main(["init"]) == 0

    assert "ASES-ROL-05" not in _console(capsys)[0]


def test_init_prints_nothing_extra_when_the_profiles_module_has_no_residual_risks_function(world, monkeypatch, capsys):
    _profiles_stub(monkeypatch)  # no residual_risks kwarg: matches a profiles build from before it existed

    assert cli.main(["init"]) == 0

    assert "ASES-ROL-05" not in _console(capsys)[0]


@needs_profiles
def test_init_prints_the_real_residual_risks(world, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "ases.profiles", real_profiles)
    monkeypatch.setattr(real_profiles, "plan_init", lambda *a, **kw: [])

    assert cli.main(["init"]) == 0

    out, _ = _console(capsys)
    assert "Known limits swarm init cannot fix (ASES-ROL-05):" in out
    for risk in real_profiles.RESIDUAL_RISKS:
        assert f"  - {risk}" in out


# ---------------------------------------------------------------------------------------------
# eval, clean, retention
# ---------------------------------------------------------------------------------------------


def test_eval_passes_everything_after_eval_to_evals_main_and_returns_its_exit_code(monkeypatch):
    seen = []
    _stub(monkeypatch, "evals", main=lambda argv: seen.append(list(argv)) or 2)

    assert cli.main(["eval", "run", "--tasks", "E1,E9", "--candidates", "a,b", "--spend-quota"]) == 2

    assert seen == [["run", "--tasks", "E1,E9", "--candidates", "a,b", "--spend-quota"]]


@pytest.mark.parametrize("argv", [["list"], ["--help"], ["-h"], ["report", "runs/1"], ["compare", "a", "b", "--tolerance", "0.1"],
                                  ["run", "--out", "C:/x"], []])
def test_eval_never_lets_the_swarm_parser_touch_its_arguments(monkeypatch, argv):
    seen = []
    _stub(monkeypatch, "evals", main=lambda args: seen.append(list(args)) or 0)

    assert cli.main(["eval", *argv]) == 0

    assert seen == [argv]


@pytest.mark.parametrize("returned, expected", [(0, 0), (1, 1), (2, 2), (None, 0), ("weird", 1)])
def test_eval_exit_code_follows_what_evals_main_returns(monkeypatch, returned, expected):
    _stub(monkeypatch, "evals", main=lambda argv: returned)

    assert cli.main(["eval", "list"]) == expected


def test_eval_survives_evals_main_calling_sys_exit(monkeypatch):
    def exits(argv):
        raise SystemExit(2)

    _stub(monkeypatch, "evals", main=exits)
    assert cli.main(["eval", "run"]) == 2

    def exits_quietly(argv):
        raise SystemExit()

    _stub(monkeypatch, "evals", main=exits_quietly)
    assert cli.main(["eval", "list"]) == 0


def test_eval_without_the_evals_module_is_one_line_and_exit_1(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "ases.evals", None)

    assert cli.main(["eval", "list"]) == 1

    assert "'evals'" in _console(capsys)[1]


def _hardening_stub(monkeypatch, *, clean_report=None, retention_report=None, retention_raises=None):
    calls = types.SimpleNamespace(clean=[], retention=[])

    def clean(repo, integration_branch, *, board, conn, plan_project, apply=False, **kwargs):
        calls.clean.append(dict(repo=repo, integration_branch=integration_branch, board=board,
                                plan_project=plan_project, apply=apply))
        return clean_report if clean_report is not None else types.SimpleNamespace(errors=[], text="CLEAN REPORT")

    def retention(ases_home, days, *, apply=False, **kwargs):
        calls.retention.append(dict(ases_home=ases_home, days=days, apply=apply))
        if retention_raises is not None:
            raise retention_raises
        return retention_report if retention_report is not None else types.SimpleNamespace(
            errors=[], text=f"RETENTION REPORT {ACCENT}")

    _stub(monkeypatch, "hardening", clean=clean, retention=retention,
          format_clean_report=lambda report: report.text, format_retention_report=lambda report: report.text)
    return calls


def test_clean_is_a_dry_run_by_default(world, monkeypatch, capsys):
    calls = _hardening_stub(monkeypatch)

    assert cli.main(["clean", "--repo", str(world.repo)]) == 0

    (call,) = calls.clean
    assert call == dict(repo=world.repo.resolve(), integration_branch="integration", board="b", plan_project="t3",
                        apply=False)
    assert _console(capsys)[0] == "CLEAN REPORT\n"  # the report says itself whether it was a dry run


def test_clean_apply_passes_apply_true(world, monkeypatch, capsys):
    calls = _hardening_stub(monkeypatch)

    assert cli.main(["clean", "--repo", str(world.repo), "--apply"]) == 0

    assert calls.clean[0]["apply"] is True
    assert _console(capsys)[0] == "CLEAN REPORT\n"


def test_clean_exits_1_when_the_report_has_errors(world, monkeypatch, capsys):
    _hardening_stub(monkeypatch, clean_report=types.SimpleNamespace(errors=["could not remove x"], text="R"))

    assert cli.main(["clean", "--repo", str(world.repo), "--apply"]) == 1


def test_clean_needs_a_plan_that_passes_gate_0(world, monkeypatch, capsys):
    calls = _hardening_stub(monkeypatch)
    world.plan_file.unlink()

    assert cli.main(["clean", "--repo", str(world.repo)]) == 1

    assert calls.clean == [] and "Gate 0 FAILED:" in _console(capsys)[0]


def test_clean_requires_repo():
    with pytest.raises(SystemExit) as caught:
        cli.main(["clean"])

    assert caught.value.code == 2


def test_retention_is_a_dry_run_and_defaults_to_the_longer_configured_window(world, monkeypatch, capsys):
    calls = _hardening_stub(monkeypatch)

    assert cli.main(["retention"]) == 0

    assert calls.retention == [dict(ases_home=world.project.ases_home, days=90, apply=False)]
    assert _console(capsys)[0] == "RETENTION REPORT caf\\xe9\n"  # the report says itself whether it was a dry run


def test_retention_default_follows_the_retention_block_of_the_config(world, monkeypatch):
    project = _project(world.tmp, retention={"logs_days": 120, "reports_days": 60})
    monkeypatch.setattr(cli, "_load_project", lambda: project)
    calls = _hardening_stub(monkeypatch)

    cli.main(["retention"])

    assert calls.retention[0]["days"] == 120  # the longer of the two, so nothing goes earlier than either allows


def test_retention_days_overrides_the_config_and_apply_is_passed_on(world, monkeypatch, capsys):
    calls = _hardening_stub(monkeypatch)

    assert cli.main(["retention", "--days", "7", "--apply"]) == 0

    assert calls.retention == [dict(ases_home=world.project.ases_home, days=7, apply=True)]


@pytest.mark.parametrize("value", ["0", "-3", "week"])
def test_retention_refuses_days_below_1_before_anything_is_touched(monkeypatch, value):
    calls = _hardening_stub(monkeypatch)

    with pytest.raises(SystemExit) as caught:
        cli.main(["retention", "--days", value])

    assert caught.value.code == 2 and calls.retention == []


def test_retention_exits_1_when_hardening_refuses_in_its_report(world, monkeypatch, capsys):
    """hardening.retention does not raise for days below 1: it returns a report with `refused` set and does nothing."""
    _hardening_stub(monkeypatch, retention_report=types.SimpleNamespace(
        errors=[], refused="days must be at least 1, got 0: nothing was done", text="swarm retention: REFUSED."))

    assert cli.main(["retention", "--apply"]) == 1

    assert _console(capsys)[0] == "swarm retention: REFUSED.\n"


def test_retention_reports_a_refusal_from_hardening(world, monkeypatch, capsys):
    _hardening_stub(monkeypatch, retention_raises=ValueError("days must be at least 1"))

    assert cli.main(["retention", "--days", "1"]) == 1

    assert _console(capsys)[1] == "swarm retention: days must be at least 1\n"


def test_retention_exits_1_when_the_report_has_errors(world, monkeypatch):
    _hardening_stub(monkeypatch, retention_report=types.SimpleNamespace(errors=["locked file"], text="R"))

    assert cli.main(["retention", "--apply"]) == 1


# ---------------------------------------------------------------------------------------------
# doctor, models
# ---------------------------------------------------------------------------------------------


def test_doctor_prints_every_row_in_ascii_and_returns_the_report_exit_code(world, monkeypatch, capsys):
    rows = (
        doctor.DoctorCheck("python_version", "pass", "running Python 3.11.9"),
        doctor.DoctorCheck("sandbox_docker", "warn", f"docker {ACCENT} not found {ARROW}", ("ASES-SEC-03",)),
        doctor.DoctorCheck("profile_state[1]", "warn", "reviewer has a terminal toolset", ("ASES-ROL-05",)),
        doctor.DoctorCheck("gateway_dispatcher", "pending", "gateway not running"),
    )
    monkeypatch.setattr(cli.ases_doctor, "run", lambda project, models_config, conn: doctor.DoctorReport(rows))

    assert cli.main(["doctor"]) == 0

    out, _ = _console(capsys)
    assert "[PASS] python_version: running Python 3.11.9" in out
    assert "[WARN] sandbox_docker: docker caf\\xe9 not found \\u2192 (ASES-SEC-03)" in out
    assert "[WARN] profile_state[1]: reviewer has a terminal toolset (ASES-ROL-05)" in out
    assert "[PEND] gateway_dispatcher: gateway not running" in out
    assert out.rstrip().endswith("HEALTHY") and out.isascii()


def test_doctor_exits_1_and_says_not_healthy_when_a_row_fails(world, monkeypatch, capsys):
    rows = (doctor.DoctorCheck("sandbox_docker", "fail", "docker daemon not reachable"),)
    monkeypatch.setattr(cli.ases_doctor, "run", lambda project, models_config, conn: doctor.DoctorReport(rows))

    assert cli.main(["doctor"]) == 1

    out, _ = _console(capsys)
    assert "[FAIL] sandbox_docker: docker daemon not reachable" in out
    assert out.rstrip().endswith("NOT HEALTHY -- see FAIL lines above")


def test_doctor_passes_the_repo_to_the_checks_only_when_given(world, monkeypatch, capsys):
    """Round 10: `swarm doctor --repo` hands the repository to doctor.run, so the base-commit check's
    prerequisite (core.logAllRefUpdates) is checked there; plain `swarm doctor` passes no repo at all."""
    seen = []

    def run(project, models_config, conn, **kwargs):
        seen.append(kwargs)
        return doctor.DoctorReport((doctor.DoctorCheck("python_version", "pass", "ok"),))

    monkeypatch.setattr(cli.ases_doctor, "run", run)

    assert cli.main(["doctor"]) == 0
    assert cli.main(["doctor", "--repo", str(world.repo)]) == 0

    assert seen == [{}, {"repo": world.repo.resolve()}]


def test_doctor_with_a_repo_reports_the_real_log_all_ref_updates_row(world, capsys):
    """End to end on a real throwaway repository: with --repo the row is no longer "pending"."""
    subprocess.run(["git", "init", "-q", str(world.repo)], check=True, capture_output=True)
    rows = doctor._check_log_all_ref_updates(world.repo)

    assert rows.status in ("pass", "warn") and rows.status != "pending"


def test_models_lists_the_registry(world, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_load_models_config", lambda: {
        "providers": {}, "models": [{"provider": "xkiro", "model": "coder-model", "context_length": 65536,
                                     "role_class": "coder", "pinned": True}]})

    assert cli.main(["models"]) == 0

    assert "xkiro/coder-model  role=coder  context=65536  smoke=not run  pinned" in _console(capsys)[0]


# ---------------------------------------------------------------------------------------------
# smoke-test (ASES-MOD-04): models.record_smoke_test's first production caller
# ---------------------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _Candidate:
    provider: str
    model: str
    role_class: str | None
    profile: str
    label: str


class _EvalError(Exception):
    pass


def _fake_candidate_from_config(models_config, label, *, roles=None, profile=None):
    rows = [m for m in models_config.get("models", []) if f"{m['provider']}/{m['model']}" == label]
    if not rows:
        raise _EvalError(f"unknown candidate {label!r}")
    row = rows[0]
    role_class = row.get("role_class")
    chosen = profile or (roles or {}).get(role_class)
    if not chosen:
        raise _EvalError(f"no profile is known for {label!r}")
    return _Candidate(row["provider"], row["model"], role_class, chosen, label)


def _evals_stub(monkeypatch, invoke=None):
    """A fake `ases.evals` module: candidate_from_config and EvalError behave like the real ones closely enough
    for the CLI wiring under test, and default_invoke is `invoke` (or, with none given, a call that fails the
    test, for the --spend-quota refusal tests where the model must never be reached)."""
    calls = types.SimpleNamespace(invoke=[])

    def default_invoke(candidate, prompt, workdir, timeout, **kwargs):
        calls.invoke.append(dict(candidate=candidate, prompt=prompt, workdir=pathlib.Path(workdir),
                                 timeout=timeout, kwargs=kwargs))
        return invoke(candidate, prompt, pathlib.Path(workdir), timeout, **kwargs)

    _stub(
        monkeypatch, "evals", candidate_from_config=_fake_candidate_from_config, EvalError=_EvalError,
        default_invoke=default_invoke if invoke is not None
        else (lambda *a, **kw: pytest.fail("the model must not be called")),
    )
    return calls


def _write_marker(workdir: pathlib.Path) -> None:
    (workdir / cli._SMOKE_TEST_MARKER_FILE).write_text(cli._SMOKE_TEST_MARKER_BODY, encoding="utf-8")


def _smoke_record(conn, model: str):
    return {m.model: m for m in models.list_models(conn)}[model]


def test_smoke_test_is_refused_without_spend_quota_and_calls_nothing(world, monkeypatch, capsys):
    calls = _evals_stub(monkeypatch)

    assert cli.main(["smoke-test", "--provider", "xkiro", "--model", "coder-model"]) == 0

    assert calls.invoke == []
    out, _ = _console(capsys)
    assert "dry run" in out and "nothing was called and no quota was spent" in out
    assert "xkiro/coder-model (profile coder-1)" in out
    assert "run the same command again with --spend-quota" in out


def test_smoke_test_refuses_an_unknown_candidate_before_spending_anything(world, monkeypatch, capsys):
    calls = _evals_stub(monkeypatch)

    assert cli.main(["smoke-test", "--provider", "nope", "--model", "x", "--spend-quota"]) == 1

    assert calls.invoke == []
    assert "unknown candidate" in _console(capsys)[1]


def test_smoke_test_records_a_pass_when_the_tool_call_and_the_reply_are_both_correct(world, monkeypatch, capsys):
    def invoke(candidate, prompt, workdir, timeout, **kwargs):
        assert candidate.label == "xkiro/coder-model" and candidate.profile == "coder-1"
        assert kwargs.get("tools") == ("file",)
        _write_marker(workdir)
        return types.SimpleNamespace(returncode=0, stdout='{"file_written": true}', stderr="",
                                     latency_seconds=1.75, requests=2, timed_out=False)

    calls = _evals_stub(monkeypatch, invoke=invoke)

    assert cli.main(["smoke-test", "--provider", "xkiro", "--model", "coder-model", "--spend-quota"]) == 0

    assert len(calls.invoke) == 1 and calls.invoke[0]["timeout"] == cli._SMOKE_TEST_TIMEOUT_DEFAULT
    out, _ = _console(capsys)
    assert "PASS" in out and "2 request(s)" in out
    record = _smoke_record(world.conn, "coder-model")
    assert record.smoke_tested is True
    assert "verified" in record.smoke_test_detail
    # ASES-MOD-04: "Record the result and the latency" - the latency must be PERSISTED, not just printed.
    assert "latency 1.8s" in record.smoke_test_detail and "2 request(s)" in record.smoke_test_detail


def test_smoke_test_persists_latency_even_on_a_recorded_failure(world, monkeypatch, capsys):
    """The latency belongs in the persisted detail on every path that reaches a result, not only on PASS."""
    def invoke(candidate, prompt, workdir, timeout, **kwargs):
        _write_marker(workdir)
        return types.SimpleNamespace(returncode=0, stdout="sure, all done!", stderr="", latency_seconds=0.9,
                                     requests=1, timed_out=False)

    _evals_stub(monkeypatch, invoke=invoke)

    assert cli.main(["smoke-test", "--provider", "xkiro", "--model", "coder-model", "--spend-quota"]) == 1

    record = _smoke_record(world.conn, "coder-model")
    assert record.smoke_test_result == "fail"
    assert "not valid JSON" in record.smoke_test_detail
    assert "latency 0.9s" in record.smoke_test_detail and "1 request(s)" in record.smoke_test_detail


def test_smoke_test_records_a_fail_when_the_reply_is_not_valid_json(world, monkeypatch, capsys):
    def invoke(candidate, prompt, workdir, timeout, **kwargs):
        _write_marker(workdir)
        return types.SimpleNamespace(returncode=0, stdout="sure, all done!", stderr="", latency_seconds=0.9,
                                     requests=1, timed_out=False)

    _evals_stub(monkeypatch, invoke=invoke)

    assert cli.main(["smoke-test", "--provider", "xkiro", "--model", "coder-model", "--spend-quota"]) == 1

    out, _ = _console(capsys)
    assert "FAIL" in out and "not valid JSON" in out
    record = _smoke_record(world.conn, "coder-model")
    assert record.smoke_tested is False and record.smoke_test_result == "fail"


def test_smoke_test_records_a_fail_when_the_tool_was_never_actually_used(world, monkeypatch, capsys):
    """The model can print plausible JSON without ever having called the file tool; the marker file is the proof."""
    def invoke(candidate, prompt, workdir, timeout, **kwargs):
        return types.SimpleNamespace(returncode=0, stdout='{"file_written": true}', stderr="", latency_seconds=0.5,
                                     requests=1, timed_out=False)

    _evals_stub(monkeypatch, invoke=invoke)

    assert cli.main(["smoke-test", "--provider", "openrouter", "--model", "review-model", "--spend-quota"]) == 1

    out, _ = _console(capsys)
    assert "FAIL" in out and "did not create" in out
    assert _smoke_record(world.conn, "review-model").smoke_test_result == "fail"


def test_smoke_test_records_a_fail_when_the_model_call_itself_fails(world, monkeypatch, capsys):
    def invoke(candidate, prompt, workdir, timeout, **kwargs):
        return types.SimpleNamespace(returncode=2, stdout="", stderr="provider unavailable", latency_seconds=0.3,
                                     requests=1, timed_out=False)

    _evals_stub(monkeypatch, invoke=invoke)

    assert cli.main(["smoke-test", "--provider", "xkiro", "--model", "coder-model", "--spend-quota"]) == 1

    out, _ = _console(capsys)
    assert "FAIL" in out and "exit 2" in out and "provider unavailable" in out


def test_smoke_test_records_a_fail_on_timeout(world, monkeypatch, capsys):
    def invoke(candidate, prompt, workdir, timeout, **kwargs):
        return types.SimpleNamespace(returncode=-1, stdout="", stderr="", latency_seconds=float(timeout),
                                     requests=1, timed_out=True)

    calls = _evals_stub(monkeypatch, invoke=invoke)

    assert cli.main(["smoke-test", "--provider", "xkiro", "--model", "coder-model", "--spend-quota",
                     "--timeout", "5"]) == 1

    assert calls.invoke[0]["timeout"] == 5
    out, _ = _console(capsys)
    assert "FAIL" in out and "did not finish within 5s" in out


def test_smoke_test_lets_the_profile_be_overridden(world, monkeypatch):
    def invoke(candidate, prompt, workdir, timeout, **kwargs):
        _write_marker(workdir)
        return types.SimpleNamespace(returncode=0, stdout='{"file_written": true}', stderr="", latency_seconds=1.0,
                                     requests=1, timed_out=False)

    calls = _evals_stub(monkeypatch, invoke=invoke)

    cli.main(["smoke-test", "--provider", "xkiro", "--model", "coder-model", "--profile", "coder-2",
             "--spend-quota"])

    assert calls.invoke[0]["candidate"].profile == "coder-2"


def test_smoke_test_removes_its_temp_workdir_after_the_call(world, monkeypatch):
    seen = {}

    def invoke(candidate, prompt, workdir, timeout, **kwargs):
        _write_marker(workdir)
        seen["workdir"] = pathlib.Path(workdir)
        return types.SimpleNamespace(returncode=0, stdout='{"file_written": true}', stderr="", latency_seconds=1.0,
                                     requests=1, timed_out=False)

    _evals_stub(monkeypatch, invoke=invoke)

    cli.main(["smoke-test", "--provider", "xkiro", "--model", "coder-model", "--spend-quota"])

    assert not seen["workdir"].exists()


def test_smoke_test_records_a_fail_instead_of_crashing_when_the_invoke_call_itself_raises(world, monkeypatch, capsys):
    """evals.default_invoke documents itself as never raising, but this command must not trust that blindly: an
    exception from the call (or from anything else in the try block) is a recorded [FAIL], never a traceback."""
    def invoke(candidate, prompt, workdir, timeout, **kwargs):
        raise RuntimeError("provider connection reset")

    _evals_stub(monkeypatch, invoke=invoke)

    assert cli.main(["smoke-test", "--provider", "xkiro", "--model", "coder-model", "--spend-quota"]) == 1

    out, _ = _console(capsys)
    assert "FAIL" in out and "RuntimeError" in out and "provider connection reset" in out
    record = _smoke_record(world.conn, "coder-model")
    assert record.smoke_tested is False and record.smoke_test_result == "fail"


def test_smoke_test_records_a_fail_instead_of_crashing_when_the_temp_workdir_cannot_be_created(world, monkeypatch, capsys):
    def broken_mkdtemp(*args, **kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr(cli.tempfile, "mkdtemp", broken_mkdtemp)
    calls = _evals_stub(monkeypatch)  # the model must never be reached: there is no workdir to run it in

    assert cli.main(["smoke-test", "--provider", "xkiro", "--model", "coder-model", "--spend-quota"]) == 1

    assert calls.invoke == []
    out, _ = _console(capsys)
    assert "FAIL" in out and "no space left on device" in out
    assert _smoke_record(world.conn, "coder-model").smoke_test_result == "fail"


# --- _validate_smoke_test in isolation (the malformed-result cases, without going through the CLI) ---------------


def test_validate_smoke_test_passes_when_the_marker_file_and_the_reply_are_both_correct(tmp_path):
    _write_marker(tmp_path)

    ok, detail = cli._validate_smoke_test(tmp_path, '{"file_written": true}')

    assert ok is True and "verified" in detail


def test_validate_smoke_test_fails_when_the_marker_file_is_missing(tmp_path):
    ok, detail = cli._validate_smoke_test(tmp_path, '{"file_written": true}')

    assert ok is False and "did not create" in detail


def test_validate_smoke_test_fails_when_the_marker_file_has_the_wrong_content(tmp_path):
    (tmp_path / cli._SMOKE_TEST_MARKER_FILE).write_text('{"ok": false}', encoding="utf-8")

    ok, detail = cli._validate_smoke_test(tmp_path, '{"file_written": true}')

    assert ok is False and "expected content" in detail


def test_validate_smoke_test_fails_when_the_marker_file_is_not_json(tmp_path):
    (tmp_path / cli._SMOKE_TEST_MARKER_FILE).write_text("not json", encoding="utf-8")

    ok, detail = cli._validate_smoke_test(tmp_path, '{"file_written": true}')

    assert ok is False and "is not valid JSON" in detail


def test_validate_smoke_test_fails_when_the_reply_is_not_json(tmp_path):
    _write_marker(tmp_path)

    ok, detail = cli._validate_smoke_test(tmp_path, "sure, done!")

    assert ok is False and "final reply is not valid JSON" in detail


def test_validate_smoke_test_fails_when_the_reply_json_lacks_the_expected_shape(tmp_path):
    _write_marker(tmp_path)

    ok, detail = cli._validate_smoke_test(tmp_path, '{"something_else": 1}')

    assert ok is False and "expected structured result" in detail


# ---------------------------------------------------------------------------------------------
# Edges: the guards that keep a command from failing in a way nobody sees
# ---------------------------------------------------------------------------------------------


def test_accepts_finds_a_named_keyword_or_a_var_keyword_and_gives_up_on_a_signature_it_cannot_read():
    assert cli._accepts(lambda a, *, reuse_credentials_from=None: 0, "reuse_credentials_from")
    assert cli._accepts(lambda a, **kwargs: 0, "reuse_credentials_from")
    assert not cli._accepts(lambda a, b=1: 0, "reuse_credentials_from")
    assert not cli._accepts(lambda a, *rest: 0, "reuse_credentials_from")
    assert not cli._accepts(int, "reuse_credentials_from")  # a builtin type whose signature cannot be read


def test_the_real_config_loaders_read_the_files_of_the_repository():
    project = cli._load_project()
    models_config = cli._load_models_config()

    assert project.integration_branch == "integration" and project.sandbox_enabled is False
    assert {"providers", "models"} <= set(models_config)


def test_a_plan_role_with_no_pinned_model_adds_nothing_to_the_estimate(world, monkeypatch):
    plan = cli.plan_mod.load_plan_file(world.plan_file, known_roles=set(ROLES), max_cards=40)
    only_coder = copy.deepcopy(MODELS_CONFIG)
    only_coder["models"] = [m for m in only_coder["models"] if m["role_class"] == "coder"]

    estimate = cli._estimate_lines(plan, world.project, only_coder, world.conn)

    assert [line.split(":")[0] for line in estimate.budget_lines] == ["  budget[xkiro]"]  # the reviewer task has none
    assert estimate.policy_violation is None and estimate.unaffordable == ()


def test_the_calendar_line_says_when_a_provider_declares_no_rate_limit(world, monkeypatch):
    plan = cli.plan_mod.load_plan_file(world.plan_file, known_roles=set(ROLES), max_cards=40)
    unpaced = copy.deepcopy(MODELS_CONFIG)
    unpaced["providers"]["xkiro"]["limits"] = {}

    estimate = cli._estimate_lines(plan, world.project, unpaced, world.conn)

    assert "  xkiro/coder-model: needs 10 request(s); provider declares no rate limit to pace against" in \
        estimate.calendar_lines


def test_run_says_the_project_is_paused_when_a_pause_lands_without_a_recorded_reason(runw, monkeypatch, capsys):
    """No reason was passed to set_status here, so project_state has none and _halt_reason falls back to the bare
    status (a pause given a reason is covered by the tests around _refuse_unless_startable and swarm resume)."""
    def pause_during_the_pass(*args, **kwargs):
        bounds.set_status(runw.world.conn, "t3", "paused")
        return _pass()

    monkeypatch.setattr(cli.controller_mod, "run_pass", pause_during_the_pass)

    assert cli.main(runw.argv()) == 4

    assert "[pass 2] STOPPED: the project is paused" in _console(capsys)[0]


def test_stop_is_still_a_stop_when_its_audit_event_cannot_be_written(world, monkeypatch, capsys):
    _fake_stop_all(monkeypatch)

    def locked(conn, kind, payload=None):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(cli.events_mod, "record", locked)

    assert cli.main(["stop", "--repo", str(world.repo)]) == 0

    assert "swarm stop [project t3]" in _console(capsys)[0]


def test_resume_is_still_a_resume_when_its_audit_event_cannot_be_written(world, monkeypatch, capsys):
    killswitch.request_stop(world.conn, "t3")
    _resume_world(world, monkeypatch)

    def locked(conn, kind, payload=None):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(cli.events_mod, "record", locked)

    assert cli.main(["resume", "--repo", str(world.repo)]) == 0

    assert "project t3: resumed" in _console(capsys)[0]


def test_the_estimate_the_reviewer_reads_says_when_the_quota_cannot_afford_the_plan(world, monkeypatch, capsys):
    ledger.record_usage(world.conn, "openrouter", "review-model", n=50)
    reviewer = _Reviewer(monkeypatch, [_critique("PASS")])

    assert cli.main(_critique_argv(world)) == 0

    assert "Gate P would REFUSE this plan today: it cannot be afforded on ['openrouter'] (ASES-CAP-03)" in \
        reviewer.calls[0]["estimate_text"]
    assert "swarm approve would currently refuse this plan" in _console(capsys)[0]


def test_critique_lists_only_findings_that_say_something(world, monkeypatch, capsys):
    _Reviewer(monkeypatch, [_critique("CHANGES_REQUIRED", required_changes=["", "   ", "the one real change"],
                                      test_gaps=[" "])])

    cli.main(_critique_argv(world))

    out, _ = _console(capsys)
    assert "  required changes:\n    1. the one real change\n" in out
    assert "test gaps" not in out


def test_critique_cannot_loop_forever_even_if_next_step_keeps_asking_for_a_replan(world, monkeypatch, capsys):
    reviewer = _Reviewer(monkeypatch, [_critique("CHANGES_REQUIRED")] * 10)
    prompts = _lead_that_rewrites(world, monkeypatch)
    monkeypatch.setattr(cli.critic_mod, "next_step", lambda critique, rounds_used, max_rounds=2: critic.REPLAN)

    assert cli.main(_critique_argv(world, "--auto-replan")) == 1

    assert len(reviewer.calls) == len(prompts) == 4  # replans_per_project (2) + 2 cycles, then it gives up
    assert "the round limit was reached" in _console(capsys)[1]


def test_run_prints_a_recovery_decision_that_is_not_a_dict_as_it_is(runw, capsys):
    runw.passes = [_pass(recovery=["T1 needs a fresh attempt"]), _pass(finished=True)]

    assert cli.main(runw.argv()) == 0

    assert "[pass 1] recovery: T1 needs a fresh attempt" in _console(capsys)[0]


def test_stop_never_gives_a_further_project_less_than_5_seconds(world, monkeypatch):
    _add_plan_tasks(world.conn, "alpha", "beta")
    calls = _fake_stop_all(monkeypatch)
    monkeypatch.setattr(cli.time, "monotonic", _fake_clock(0.0, 0.0, 40.0))  # the first project used up 40 seconds

    cli.main(["stop"])

    assert calls[0].deadline == 30.0 and calls[1].deadline == 5.0


def test_stop_exit_code_is_1_when_any_of_several_projects_missed_the_deadline(world, monkeypatch):
    _add_plan_tasks(world.conn, "alpha", "beta")
    reports = iter([killswitch.StopReport(paused=True, flag_set=True, within_deadline=False),
                    killswitch.StopReport(paused=True, flag_set=True, within_deadline=True)])
    calls = []

    def fake(board, plan, *, conn, reason="swarm stop", deadline_seconds=30.0, **kwargs):
        calls.append(plan.project)
        return next(reports)

    monkeypatch.setattr(cli.killswitch_mod, "stop_all", fake)

    assert cli.main(["stop"]) == 1

    assert calls == ["alpha", "beta"]  # the second was still stopped after the first came up short


def test_resume_ignores_a_deadline_it_cannot_read_instead_of_crashing(world, monkeypatch, capsys):
    bounds.start_project(world.conn, "t3")
    world.conn.execute("UPDATE project_state SET deadline_at = 'not a timestamp' WHERE project = 't3'")
    _resume_world(world, monkeypatch)

    assert cli.main(["resume", "--repo", str(world.repo)]) == 0

    out, _ = _console(capsys)
    assert "project t3: resumed" in out and "WARNING" not in out


def test_resume_with_a_future_deadline_does_not_warn(world, monkeypatch, capsys):
    bounds.start_project(world.conn, "t3")
    bounds.set_deadline(world.conn, "t3", "2999-01-01T00:00:00+00:00")
    _resume_world(world, monkeypatch)

    assert cli.main(["resume", "--repo", str(world.repo)]) == 0

    assert "WARNING" not in _console(capsys)[0]


def test_report_says_when_the_files_cannot_be_written(world, monkeypatch, capsys):
    _report_stub(monkeypatch)

    def cannot_write(report, directory):
        raise OSError("the disk is full")

    monkeypatch.setattr(cli.report_mod, "write_report", cannot_write)

    assert cli.main(["report", "--repo", str(world.repo), "--html"]) == 1

    out, err = _console(capsys)
    assert "FULL REPORT" in out  # the terminal report was still shown
    assert "could not write the report to" in err and "the disk is full" in err


def test_init_says_in_one_line_when_planning_fails_and_writes_nothing(world, monkeypatch, capsys):
    calls = _profiles_stub(monkeypatch)

    def cannot_plan(*args, **kwargs):
        raise RuntimeError("config.yaml of lead is not valid YAML")

    sys.modules["ases.profiles"].plan_init = cannot_plan

    assert cli.main(["init", "--apply", "--yes"]) == 1

    assert calls.apply == []
    err = _console(capsys)[1]
    assert err == "swarm init: could not plan the changes: RuntimeError: config.yaml of lead is not valid YAML\n"


def test_init_tells_the_operator_what_state_the_files_may_be_in_when_applying_crashes(world, monkeypatch, capsys):
    _profiles_stub(monkeypatch)

    def crashes(*args, **kwargs):
        raise OSError("disk removed")

    sys.modules["ases.profiles"].apply_init = crashes

    assert cli.main(["init", "--apply", "--yes"]) == 1

    err = _console(capsys)[1]
    assert "applying stopped with OSError: disk removed" in err
    assert "Some changes may already have been made" in err and "backup" in err


def test_approve_refuses_when_the_plan_is_edited_while_the_question_waits_for_an_answer(world, monkeypatch, capsys):
    """The critic PASS and the screen are about the bytes hashed before the question: publish_plan commits whatever
    is on disk when the answer comes, so an edit in between must never be published."""
    calls = _approve_world(world, monkeypatch)
    _record_pass(world)

    def edit_then_yes(prompt=""):
        raw = json.loads(world.plan_file.read_text(encoding="utf-8"))
        raw["tasks"][0]["title"] = "changed while the prompt was open"
        world.plan_file.write_text(json.dumps(raw), encoding="utf-8")
        return "y"

    monkeypatch.setattr("builtins.input", edit_then_yes)

    assert cli.main(_approve_argv(world, "--skip-critic", "--deadline-minutes", "30")) == 1

    assert calls == []  # nothing published, pinned or created
    assert _events(world.conn, "critic_skipped") == [] and bounds.get_state(world.conn, "t3") is None
    assert "the plan file changed while this screen was open" in _console(capsys)[1]


def test_run_honours_a_stop_that_lands_between_passes_before_the_next_pass_starts(runw, monkeypatch, capsys):
    passes = []

    def pass_one_then_someone_runs_swarm_stop(*args, **kwargs):
        passes.append(1)
        bounds.set_status(runw.world.conn, "t3", "stopped", "swarm stop")  # from another terminal
        return _pass()

    monkeypatch.setattr(cli.controller_mod, "run_pass", pass_one_then_someone_runs_swarm_stop)

    assert cli.main(runw.argv()) == 4

    assert passes == [1]  # the second pass never started
    out, _ = _console(capsys)
    assert "[pass 2] STOPPED: swarm stop" in out and "swarm resume" in out


def test_run_keeps_going_when_the_stop_flag_cannot_be_read_between_passes(runw, monkeypatch, capsys):
    """The pass reads the flag itself; one unreadable read here must not end an unattended run."""
    runw.passes = [_pass(), _pass(finished=True)]

    def locked(conn, project):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(cli.bounds_mod, "stop_requested", locked)

    assert cli.main(runw.argv()) == 0

    assert len(runw.run_calls) == 2


def test_resume_of_a_finished_project_says_only_hermes_dispatch_was_resumed(world, monkeypatch, capsys):
    bounds.set_status(world.conn, "t3", "finished")
    _resume_world(world, monkeypatch)

    assert cli.main(["resume", "--repo", str(world.repo)]) == 0

    out, _ = _console(capsys)
    assert "project t3: is finished, so only Hermes dispatch was resumed" in out
    assert bounds.get_state(world.conn, "t3")["status"] == "finished"  # a stray resume never un-finishes a project


def test_a_command_that_was_never_given_the_new_attributes_still_runs(world, monkeypatch):
    """test_cli_run.py and older callers build the Namespace by hand: the options added later default safely."""
    runw = _RunWorld(world, monkeypatch)
    runw.passes = [_pass(finished=True)]
    args = types.SimpleNamespace(repo=str(world.repo), max_iterations=2, sleep_seconds=0)

    assert cli.cmd_run(args) == 0


# ---------------------------------------------------------------------------------------------
# The modules other packages built, for real where that is safe: read-only, offline, temp directories only
# ---------------------------------------------------------------------------------------------


def test_eval_list_runs_the_real_harness_and_its_help_is_the_harness_help(capsys):
    pytest.importorskip("ases.evals")

    assert cli.main(["eval", "list"]) == 0
    listing, _ = _console(capsys)
    assert cli.main(["eval", "--help"]) == 0  # the harness's own argument parser answers --help, not swarm's
    helped, _ = _console(capsys)

    assert "Evaluation tasks" in listing and "E1 " in listing and "E10" in listing
    assert "usage: swarm eval" in helped and "--spend-quota" in helped


def test_init_dry_run_against_the_real_profiles_module_lists_real_changes_and_writes_nothing(world, monkeypatch, capsys):
    pytest.importorskip("ases.profiles")
    monkeypatch.setattr(cli, "_open_conn", lambda project: pytest.fail("a dry run must not open the database"))

    assert cli.main(["init"]) == 0

    out, err = _console(capsys)
    assert "change(s) planned for the Hermes profiles under" in out and "(dry run)" in out
    assert "create_profile" in out and "write_soul" in out  # a home with no profiles at all needs them all
    assert out.isascii() and err == ""
    assert not world.project.hermes_native_home.exists()  # nothing was written, not even the directory


def test_init_sandbox_dry_run_against_the_real_profiles_module_does_not_crash_without_an_image(world, monkeypatch, capsys):
    pytest.importorskip("ases.profiles")

    assert cli.main(["init", "--sandbox", "--include-inactive"]) == 0

    out, _ = _console(capsys)
    assert "(dry run)" in out and out.isascii()
    assert not world.project.hermes_native_home.exists()


def test_retention_dry_run_against_the_real_hardening_module_removes_nothing(world, monkeypatch, capsys):
    pytest.importorskip("ases.hardening")
    old = world.project.ases_home / "reports" / "ases" / "20200101T000000Z"
    old.mkdir(parents=True)
    (old / "report.html").write_text("old", encoding="utf-8")
    stamp = (datetime.now(timezone.utc) - timedelta(days=400)).timestamp()
    os.utime(old / "report.html", (stamp, stamp))
    os.utime(old, (stamp, stamp))

    assert cli.main(["retention", "--days", "30"]) == 0

    out, err = _console(capsys)
    assert "swarm retention (DRY RUN)" in out and out.isascii() and err == ""
    assert (old / "report.html").exists()  # a dry run: still there
    assert cli.main(["retention", "--days", "30", "--apply"]) == 0
    assert "swarm retention (APPLIED)" in _console(capsys)[0]


def test_clean_dry_run_against_the_real_hardening_module_on_a_real_repository(world, monkeypatch, capsys):
    pytest.importorskip("ases.hardening")
    _committed_repo(world)
    monkeypatch.setattr(hermes, "kanban_list", lambda *a, **k: [])

    def unreadable(board, card_id):
        raise hermes.HermesCommandError(["kanban", "show"], 1, "no such card")

    monkeypatch.setattr(hermes, "kanban_show", unreadable)

    assert cli.main(["clean", "--repo", str(world.repo)]) == 0

    out, err = _console(capsys)
    assert "swarm clean (DRY RUN)" in out and "integration branch integration" in out and "Dry run" in out
    assert out.isascii()


# ---------------------------------------------------------------------------------------------
# swarm run's startup against the real guard, pin, project state and reconcile (only Hermes and the pass are faked)
# ---------------------------------------------------------------------------------------------


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, text=True).stdout.strip()


def _committed_repo(world):
    """The world's repository as a real git checkout on `integration` with the plan committed."""
    for args in (["init", "-q", "-b", "integration"], ["config", "user.email", "t@t"], ["config", "user.name", "t"],
                 ["add", "-A"], ["commit", "-q", "-m", "plan"]):
        _git(world.repo, *args)
    return world.repo


def _real_startup(world, monkeypatch):
    _committed_repo(world)
    controller.pin_gate_profiles(world.conn, "t3", PLAN_RAW["gate_profiles"])
    passes = []
    monkeypatch.setattr(cli.controller_mod, "run_pass", lambda *a, **kw: passes.append(1) or _pass(finished=True))
    return passes


def _run_argv(world):
    return ["run", "--repo", str(world.repo), "--max-iterations", "2", "--sleep-seconds", "0"]


def test_run_refuses_when_a_tasks_sandbox_network_exception_changed_after_approval(world, monkeypatch, capsys):
    """ASES-QG-02 with ASES-SEC-05/SEC-07 (round 9): the pin taken at approve time covers each task's network
    exception, and `swarm run`'s pre-flight passes the plan's CURRENT exceptions, so a plan.json edited after
    approval to grant a task network is refused exactly like an edited gate command."""
    passes = _real_startup(world, monkeypatch)  # pinned with PLAN_RAW: no task has a network exception
    plan_file = world.repo / "docs" / "ases" / "plan.json"
    raw = json.loads(plan_file.read_text(encoding="utf-8"))
    raw["tasks"][0]["sandbox_network"] = True
    raw["tasks"][0]["sandbox_network_reason"] = "added after approval"
    plan_file.write_text(json.dumps(raw), encoding="utf-8")

    assert cli.main(_run_argv(world)) == 1

    assert passes == []
    out, err = _console(capsys)
    assert "REFUSED (ASES-QG-02)" in err


def test_run_refuses_when_a_tasks_allow_gate_config_changes_marker_changed_after_approval(world, monkeypatch, capsys):
    """ASES-QG-02 (round 10, GATEPIN): round 9's CIPIN made a task's allow_gate_config_changes marker the only
    thing that lets a diff change gate/CI/test-runner configuration, but never pinned the marker itself, so a
    plan.json edited after approval to set it went unnoticed. GATEPIN folds the marker into the same pin
    plan.pinned_task_fields / gates.hash_gate_profiles already cover for a task's network exception, so
    `swarm run`'s pre-flight now refuses this exactly like an edited gate command, modeled on
    test_run_refuses_when_a_tasks_sandbox_network_exception_changed_after_approval above."""
    passes = _real_startup(world, monkeypatch)  # pinned with PLAN_RAW: no task sets the marker
    plan_file = world.repo / "docs" / "ases" / "plan.json"
    raw = json.loads(plan_file.read_text(encoding="utf-8"))
    raw["tasks"][0]["allow_gate_config_changes"] = True
    plan_file.write_text(json.dumps(raw), encoding="utf-8")

    assert cli.main(_run_argv(world)) == 1

    assert passes == []
    out, err = _console(capsys)
    assert "REFUSED (ASES-QG-02)" in err


def test_run_startup_with_the_real_guard_pin_bounds_and_reconcile(world, monkeypatch, capsys):
    passes = _real_startup(world, monkeypatch)

    assert cli.main(_run_argv(world)) == 0

    assert passes == [1]
    assert bounds.get_state(world.conn, "t3")["status"] == "running"
    assert guards.expected_head(world.conn, "t3") == _git(world.repo, "rev-parse", "HEAD")
    out, err = _console(capsys)
    assert "[RECONCILE]" not in out and err == ""  # a clean database and a clean checkout: nothing to say


def test_run_startup_refuses_a_dirty_primary_checkout_before_it_starts_the_project(world, monkeypatch, capsys):
    passes = _real_startup(world, monkeypatch)
    (world.repo / "stray.txt").write_text("left behind", encoding="utf-8")

    assert cli.main(_run_argv(world)) == 3

    assert passes == [] and bounds.get_state(world.conn, "t3") is None
    assert "stray.txt" in _console(capsys)[1]


def test_run_startup_refuses_gate_profiles_that_changed_after_approval(world, monkeypatch, capsys):
    passes = _real_startup(world, monkeypatch)
    raw = json.loads(world.plan_file.read_text(encoding="utf-8"))
    raw["gate_profiles"] = {"trivial": ["echo ok || true"]}  # the classic way to make a red gate green
    world.plan_file.write_text(json.dumps(raw), encoding="utf-8")
    _git(world.repo, "commit", "-q", "-am", "quietly weaken the gate")

    assert cli.main(_run_argv(world)) == 1

    assert passes == [] and "REFUSED (ASES-QG-02)" in _console(capsys)[1]


def test_run_startup_reconciles_for_real_and_blocks_on_an_open_create_cards_intent(world, monkeypatch, capsys):
    """An interrupted card creation is exactly what section 19.4 says blocks until it is repeated."""
    passes = _real_startup(world, monkeypatch)
    world.conn.execute("INSERT INTO intents (project, kind, key, started_at) VALUES ('t3', 'create_cards', 't3', "
                       "datetime('now'))")

    assert cli.main(_run_argv(world)) == 5

    assert passes == []
    out, err = _console(capsys)
    assert "[RECONCILE] BLOCKED" in out and "open_intent" in out and "re-run swarm approve" in out
    assert "REFUSED (ASES-REC-04)" in err
    assert cli.main([*_run_argv(world), "--ignore-reconcile"]) == 0  # the operator's explicit override


# ---------------------------------------------------------------------------------------------
# A cmd_* function called directly with a hand-built Namespace that has only the required attributes
# ---------------------------------------------------------------------------------------------


def test_critique_and_approve_default_every_optional_flag(world, monkeypatch, capsys):
    _Reviewer(monkeypatch, [_critique("PASS")])
    calls = _approve_world(world, monkeypatch)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")

    assert cli.cmd_critique(types.SimpleNamespace(repo=str(world.repo), request="Build a todo app")) == 0
    assert cli.cmd_approve(types.SimpleNamespace(repo=str(world.repo), project_id="p_1")) == 1  # no yes: it asks

    assert calls == [] and "Not approved" in _console(capsys)[0]


def test_approve_yes_and_the_rest_default_when_only_the_required_attributes_are_given(world, monkeypatch):
    calls = _approve_world(world, monkeypatch)
    _record_pass(world)

    assert cli.cmd_approve(types.SimpleNamespace(repo=str(world.repo), project_id="p_1", yes=True)) == 0

    assert [c[0] for c in calls] == ["publish", "pin", "create"]


def test_answer_report_stop_and_resume_default_every_optional_flag(world, monkeypatch, capsys):
    _, answers = _questions_stub(monkeypatch)
    _report_stub(monkeypatch)
    stops = _fake_stop_all(monkeypatch)
    _resume_world(world, monkeypatch)

    assert cli.cmd_answer(types.SimpleNamespace(card="t_1", text="yes")) == 0
    assert answers.answered[0][3] == "user"
    assert cli.cmd_report(types.SimpleNamespace(repo=str(world.repo))) == 0
    assert cli.cmd_stop(types.SimpleNamespace()) == 0  # no repo, no reason
    assert stops[0].reason == "swarm stop"
    assert cli.cmd_resume(types.SimpleNamespace()) == 0  # no repo, no extension


def test_init_clean_and_retention_default_every_optional_flag_to_a_dry_run(world, monkeypatch):
    profiles = _profiles_stub(monkeypatch)
    hardening = _hardening_stub(monkeypatch)

    assert cli.cmd_init(types.SimpleNamespace()) == 0
    assert cli.cmd_clean(types.SimpleNamespace(repo=str(world.repo))) == 0
    assert cli.cmd_retention(types.SimpleNamespace()) == 0

    assert profiles.apply == [] and profiles.plan[0]["sandbox_enabled"] is False
    assert hardening.clean[0]["apply"] is False
    assert hardening.retention == [dict(ases_home=world.project.ases_home, days=90, apply=False)]


def test_retention_passes_a_days_of_zero_on_so_hardening_refuses_it_instead_of_using_the_default(world, monkeypatch):
    hardening = _hardening_stub(monkeypatch, retention_report=types.SimpleNamespace(
        errors=[], refused="days must be at least 1, got 0", text="REFUSED"))

    assert cli.cmd_retention(types.SimpleNamespace(days=0, apply=True)) == 1

    assert hardening.retention == [dict(ases_home=world.project.ases_home, days=0, apply=True)]


def test_a_database_migration_error_is_one_line_and_exit_1_not_a_traceback(monkeypatch, capsys):
    """db.connect refuses a database from a newer ASES, a failed migration and a failed backup with MigrationError."""
    monkeypatch.setattr(cli, "_load_project", lambda: types.SimpleNamespace(board="b"))

    def boom(project):
        raise db.MigrationError("database is at version 9 but this ASES only knows version 7", version=9)

    monkeypatch.setattr(cli, "_open_conn", boom)

    assert cli.main(["answer", "t_1", "some text"]) == 1

    out, err = _console(capsys)
    assert out == ""
    assert err == ("swarm answer: the ASES database cannot be opened: database is at version 9 but this ASES only "
                   "knows version 7\n")


def test_answer_names_hermes_kanban_unblock_because_a_second_answer_is_refused(world, monkeypatch, capsys):
    def hermes_fails(board, card_id, text):
        raise hermes.HermesCommandError(["kanban", "unblock"], 1, "cannot unblock")

    _questions_stub(monkeypatch, answer=hermes_fails)

    assert cli.main(["answer", "t_77", "ANSWER-TEXT"]) == 1

    err = _console(capsys)[1]
    assert "hermes kanban unblock t_77" in err and "refuse a second one" in err
