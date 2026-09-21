"""finalgates.py: Gates 4 and 5 as controller lifecycle operations and the release report (ASES-TSK-04, ASES-CTL-01,
ASES-QG-01, ASES-SEC-01, ASES-GIT-07, ASES-OBS-02).

The tree scan and finalize run against REAL temp git repos (the scan reads git objects, finalize reads the integration
branch and gates.run_gate builds a throwaway worktree), the database is a temp sqlite file, and only the hermes module
is faked: kanban_show and kanban_list are replaced for every test (an autouse fixture) and every other way into Hermes
fails the test. Gate commands are faked through the `run_gate` and `run4`/`run5` hooks wherever the test is not about
running a command. Non-ASCII test data is built with chr() so that this file stays pure ASCII."""
import dataclasses
import json
import pathlib
import re
import subprocess
import types
from datetime import datetime, timezone

import pytest

from ases import bounds, config, db, events, finalgates, gates, hermes, intents, ledger
from ases import plan as plan_mod
from ases import report as report_mod

NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)
STAMP = "20260921T120000Z"
BOARD = "b"
SECRET = "sk-abcdefghijklmnopqrstuvwx"
SECRET_TAIL = "abcdefghijklmnopq"   # a slice of the value: no finding, event or report may carry even this much
E_ACUTE = chr(0xE9)
ARROW = chr(0x2192)
EM_DASH = chr(0x2014)
SECTION_SIGN = chr(0xA7)
BACKSLASH = chr(92)
ROLES = {"lead": "lead", "coder": "coder-1", "reviewer": "reviewer"}
MODELS = {
    "providers": {"openrouter": {"limits": {"per_day": 50}}, "xkiro": {"limits": {}}},
    "models": [],
}


# --- git helpers -------------------------------------------------------------------------------------------------


def _git(repo, *args, check=True):
    """One git command in `repo` with autocrlf and signing forced off (the user's global config must not change what
    a test commits). Returns stdout, stripped."""
    result = subprocess.run(
        ["git", "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false", "-C", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stdout}{result.stderr}")
    return result.stdout.strip()


def _write_files(repo, files):
    for name, content in files.items():
        target = pathlib.Path(repo) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))


def _commit_all(repo, message="change"):
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _make_repo(path, files=None, branch="integration"):
    path = pathlib.Path(path)
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", branch)
    _write_files(path, {"README.md": "hello\n", **(files or {})})
    _commit_all(path, "init")
    return path


def _head(repo):
    return _git(repo, "rev-parse", "HEAD")


def _scan(tmp_path, files, **kwargs):
    repo = _make_repo(tmp_path / "scanned", files)
    return finalgates.scan_tree(repo, "HEAD", **kwargs)


def _kinds(findings):
    return [finding.kind for finding in findings]


def _of_kind(findings, kind):
    return [finding for finding in findings if finding.kind == kind]


def _exact_size(size, head=""):
    """ASCII text of exactly `size` bytes that starts with `head`."""
    return head + "y" * (size - len(head))


# --- fake Hermes -------------------------------------------------------------------------------------------------


class RealHermesReached(BaseException):
    """Not an Exception on purpose: modules catch Exception around their Hermes reads, and a test that reaches real
    Hermes must fail loudly instead of having that caught."""


class FakeBoard:
    """hermes.kanban_show over a dict of card id -> card dict (or an exception to raise). A card that is not in the
    dict is a plain `done` card, so a test only lists the cards it wants to be different."""

    def __init__(self):
        self.cards = {}
        self.shown = []

    def show(self, board, card_id):
        self.shown.append(card_id)
        card = self.cards.get(card_id)
        if isinstance(card, Exception):
            raise card
        if card is None:
            card = {"id": card_id, "status": "done", "title": f"title of {card_id}", "assignee": None,
                    "_events": [], "_runs": []}
        return dict(card)

    def list(self, board, status=None, assignee=None):
        return []


@pytest.fixture(autouse=True)
def fake_board(monkeypatch):
    board = FakeBoard()

    def forbidden(*args, **kwargs):
        raise RealHermesReached("this test reached Hermes")

    monkeypatch.setattr(hermes, "_run", forbidden)
    monkeypatch.setattr(hermes, "kanban_show", board.show)
    monkeypatch.setattr(hermes, "kanban_list", board.list)
    return board


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "ases.db")


# --- plans, projects, rows ---------------------------------------------------------------------------------------


def _task(key, *, title=None, role="coder"):
    return plan_mod.PlanTask(
        key=key, title=title or f"task {key}", role=role, depends_on=(), touches=(), acceptance=("done",),
        gate_profile="g", estimated_requests=5,
    )


def _plan(*, tasks=2, profiles=None, project="p1", branch="integration", titles=None):
    return plan_mod.Plan(
        project=project, integration_branch=branch,
        gate_profiles={"g": ["echo ok"]} if profiles is None else profiles,
        tasks=tuple(_task(f"T{i}", title=(titles or {}).get(f"T{i}")) for i in range(1, tasks + 1)),
    )


def _project(tmp_path, *, name="ases", ases_home=None, budgets=None):
    return config.ProjectConfig(
        name=name, environment="native", data_class="public", workspace_root=tmp_path / "ws",
        ases_home=tmp_path / "home" if ases_home is None else ases_home, board=BOARD, integration_branch="integration",
        roles=ROLES, concurrency={}, budgets={} if budgets is None else budgets, hermes_tested_version="0.21.3",
        hermes_native_home=tmp_path / "hermes",
    )


def _seed_tasks(conn, plan, *, fix_cards=None):
    for task in plan.tasks:
        conn.execute(
            "INSERT INTO plan_tasks (project, task_key, work_card_id, merge_card_id, role, fix_cards, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, datetime('now'))",
            (plan.project, task.key, f"w_{task.key}", f"m_{task.key}", task.role, (fix_cards or {}).get(task.key, 0)),
        )


def _merge_record(conn, key, squash, gate3="pass"):
    conn.execute(
        "INSERT INTO merge_records (task_key, candidate_sha, gate3_result, squash_commit, reverted, completed_at) "
        "VALUES (?, ?, ?, ?, 0, datetime('now'))", (key, squash, gate3, squash),
    )


def _events(conn, kind):
    rows = conn.execute("SELECT payload FROM events WHERE kind = ? ORDER BY id", (kind,)).fetchall()
    return [json.loads(row["payload"]) for row in rows]


def _final_rows(conn, gate=None):
    if gate is None:
        return conn.execute("SELECT * FROM gate_runs WHERE task_key = '__final__' ORDER BY id").fetchall()
    return conn.execute(
        "SELECT * FROM gate_runs WHERE task_key = '__final__' AND gate = ? ORDER BY id", (gate,),
    ).fetchall()


def _intent_rows(conn):
    return conn.execute("SELECT kind, key, detail, completed_at FROM intents ORDER BY id").fetchall()


class FakeRunGate:
    """A stand-in for gates.run_gate: records every call, returns a GateResult (or raises)."""

    def __init__(self, passed=True, detail="command output", raises=None):
        self.passed, self.detail, self.raises = passed, detail, raises
        self.calls = []

    def __call__(self, repo, sha, gate, commands, **kwargs):
        self.calls.append({"repo": repo, "sha": sha, "gate": gate, "commands": list(commands), **kwargs})
        if self.raises is not None:
            raise self.raises
        return gates.GateResult(gate, sha, self.passed, self.detail)


def _clean_scan(repo, head):
    return []


def _finding(kind, path="src/a.py", line=3, detail="secret-shaped value (provider token)"):
    return finalgates.TreeFinding(kind, path, line, detail)


def _outcome(gate, *, passed=True, findings=(), notes=(), detail="output", head="a" * 40):
    return finalgates.GateOutcome(gate, head, passed, detail, tuple(findings), tuple(notes))


# ============================================================================================================
# Findings: severity, blocking, format
# ============================================================================================================


@pytest.mark.parametrize("kind", [
    "secret_in_tree", "secret_file_tracked", "generated_artifact_tracked", "scan_error",
])
def test_the_four_named_kinds_are_blocking(kind):
    finding = _finding(kind)

    assert finalgates.severity(finding) == "blocking"
    assert finalgates.blocking([finding]) == [finding]
    assert finalgates.advisory([finding]) == []


def test_injection_pattern_is_advisory_and_never_blocking():
    finding = _finding("injection_pattern", detail="eval or exec of a non-literal (heuristic)")

    assert finalgates.severity(finding) == "advisory"
    assert finalgates.blocking([finding]) == []
    assert finalgates.advisory([finding]) == [finding]


def test_a_skipped_note_is_information_only():
    finding = _finding("skipped", line=None, detail="not scanned")

    assert finalgates.severity(finding) == "info"
    assert finalgates.blocking([finding]) == [] and finalgates.advisory([finding]) == []


def test_a_kind_nobody_classified_blocks():
    """Fail closed: an unknown kind must never let the gate pass because it is unknown."""
    assert finalgates.severity("something_new") == "blocking"
    assert finalgates.blocking([_finding("something_new")]) != []


def test_severity_reads_a_kind_string_or_a_finding():
    assert finalgates.severity("injection_pattern") == finalgates.severity(_finding("injection_pattern"))


def test_blocking_and_advisory_take_none():
    assert finalgates.blocking(None) == [] and finalgates.advisory(None) == []


def test_a_tree_finding_is_frozen():
    finding = _finding("secret_in_tree")

    with pytest.raises(dataclasses.FrozenInstanceError):
        finding.path = "elsewhere"


def test_format_finding_is_one_ascii_line_with_path_and_line():
    text = finalgates.format_finding(_finding("secret_in_tree", path="src/a.py", line=12))

    assert text == "secret_in_tree src/a.py:12: secret-shaped value (provider token)"


def test_format_finding_without_a_line_or_a_path():
    assert finalgates.format_finding(_finding("secret_file_tracked", path=".env", line=None, detail="d")) == \
        "secret_file_tracked .env: d"
    assert finalgates.format_finding(_finding("scan_error", path="", line=None, detail="git failed")) == \
        "scan_error: git failed"


def test_format_finding_escapes_non_ascii_and_redacts_a_secret_shaped_path():
    text = finalgates.format_finding(_finding("secret_file_tracked", path=f"caf{E_ACUTE}/{SECRET}.pem", line=None))

    assert text.isascii()
    assert BACKSLASH + "xe9" in text
    assert SECRET not in text and SECRET_TAIL not in text


# ============================================================================================================
# scan_text: the per-file text scan (secrets and the injection heuristics)
# ============================================================================================================


def test_scan_text_reports_the_line_of_a_secret_and_never_the_value():
    findings = finalgates.scan_text("src/config.py", f"import os\n\nAPI = '{SECRET}'\n")

    assert len(findings) == 1
    assert (findings[0].kind, findings[0].path, findings[0].line) == ("secret_in_tree", "src/config.py", 3)
    assert SECRET_TAIL not in repr(findings)


def test_scan_text_numbers_lines_by_the_newline_only():
    """splitlines() would also break on a form feed, so a finding after one would name the wrong line."""
    findings = finalgates.scan_text("notes.txt", f"a{chr(12)}b\nkey {SECRET}\n")

    assert [(f.kind, f.line) for f in findings] == [("secret_in_tree", 2)]


def test_scan_text_handles_windows_line_endings():
    findings = finalgates.scan_text("notes.txt", f"one\r\ntwo {SECRET}\r\nthree\r\n")

    assert [(f.kind, f.line) for f in findings] == [("secret_in_tree", 2)]


def test_scan_text_finds_several_secrets_on_their_own_lines():
    text = f"a\nk1 {SECRET}\nb\nk2 ghp_1234567890abcdefghij\n"

    assert [f.line for f in finalgates.scan_text("x.txt", text)] == [2, 4]


def test_scan_text_names_the_kind_of_secret_and_not_the_secret():
    findings = finalgates.scan_text("k.txt", "-----BEGIN RSA PRIVATE KEY-----\nabc\n")

    assert [(f.kind, f.line) for f in findings] == [("secret_in_tree", 1)]
    assert findings[0].detail == "secret-shaped value (provider token)" and "BEGIN" not in findings[0].detail


def test_scan_text_names_the_extra_shapes_tamper_knows():
    """A GitHub fine-grained token is not in events.py's list: tamper.secret_hint adds it, and the label says which."""
    findings = finalgates.scan_text("k.txt", "t = github_pat_" + "A" * 30 + "\n")

    assert [f.kind for f in findings] == ["secret_in_tree"] and "A" * 10 not in findings[0].detail


def test_scan_text_clean_text_has_no_findings():
    assert finalgates.scan_text("a.py", "x = 1\nprint('hello')\n") == []
    assert finalgates.scan_text("a.txt", "") == []


PY_INJECTION = [
    'subprocess.run(f"ls {path}", shell=True)',
    'subprocess.call("ls %s" % path, shell=True)',
    'subprocess.Popen("ls {}".format(p), shell=True)',
    "subprocess.check_output(f'echo {x}', shell=True, text=True)",
    'subprocess.run("ls " + path, shell=True)',
    'os.system(f"rm -rf {d}")',
    'os.system("rm " + d)',
    'os.system("rm %s" % d)',
    "eval(user_input)",
    "exec(open(f).read())",
    "x = eval(compile(src, 'a', 'exec'))",
    'eval(f"1+{x}")',
    "pickle.loads(data)",
    "obj = pickle.load(handle)",
    "yaml.load(stream)",
    "yaml.load(stream, Loader=yaml.FullLoader)",
    'cur.execute("SELECT * FROM t WHERE id = " + uid)',
    'cur.execute("SELECT * FROM t WHERE id = %s" % uid)',
    'cur.execute(f"SELECT * FROM t WHERE id = {uid}")',
    'conn.execute("SELECT {}".format(x))',
    'cur.executemany(f"INSERT INTO t VALUES ({x})", rows)',
]

PY_NOT_INJECTION = [
    'subprocess.run(["ls", path], shell=False)',
    'subprocess.run("ls -la", shell=True)',
    'subprocess.run(f"ls {path}")',
    "subprocess.run(cmd, shell=True)",
    'os.system("clear")',
    "model.eval()",
    "session.exec(select(User))",
    'exec("print(1)")',
    "eval('1+1')",
    "eval(1)",
    "ast.literal_eval(text)",
    "pickle.dumps(obj)",
    "yaml.safe_load(stream)",
    "yaml.load(stream, Loader=yaml.SafeLoader)",
    "yaml.load(stream, Loader=SafeLoader)",
    'cur.execute("SELECT * FROM t WHERE id = ?", (uid,))',
    'cur.execute("SELECT 1")',
    "cur.execute(query, params)",
    "def execute(self, sql):",
    "executor.submit(job)",
    "# eval(user_input)  is a comment",
    "    # os.system(f'rm {x}')",
]

JS_INJECTION = [
    "eval(code)",
    "  return eval('1+1');",
    "const f = new Function('a', 'return a');",
    "new  Function (body)",
    "child_process.exec(`ls ${dir}`)",
    "exec(`rm -rf ${target}`, cb);",
    "execSync(`git checkout ${branch}`)",
    "el.innerHTML = `<b>${name}</b>`;",
    "el.innerHTML += `<li>${item}</li>`",
]

JS_NOT_INJECTION = [
    "model.eval(x)",
    "retrieval(x)",
    "redis.eval(script)",
    "exec('ls')",
    "child_process.exec('ls -la')",
    "el.innerHTML = '<b>static</b>';",
    "el.innerHTML = `<b>static</b>`;",
    "el.textContent = `${name}`;",
    "// eval(x) in a comment",
    " * eval(x) in a doc comment",
    "/* eval(x) in a block comment */",
]


@pytest.mark.parametrize("line", PY_INJECTION)
def test_each_python_injection_pattern_is_flagged_as_advisory(line):
    findings = finalgates.scan_text("app.py", f"import os\n{line}\n")

    assert _kinds(findings) == ["injection_pattern"]
    assert findings[0].line == 2 and findings[0].path == "app.py"
    assert findings[0].detail.endswith("(heuristic)")
    assert finalgates.blocking(findings) == []


@pytest.mark.parametrize("line", PY_NOT_INJECTION)
def test_ordinary_python_is_not_flagged(line):
    assert finalgates.scan_text("app.py", f"{line}\n") == []


@pytest.mark.parametrize("line", JS_INJECTION)
@pytest.mark.parametrize("name", ["ui.js", "ui.ts", "ui.tsx", "ui.mjs"])
def test_each_javascript_injection_pattern_is_flagged_as_advisory(line, name):
    findings = finalgates.scan_text(name, f"const a = 1;\n{line}\n")

    assert _kinds(findings) == ["injection_pattern"] and findings[0].line == 2
    assert finalgates.blocking(findings) == []


@pytest.mark.parametrize("line", JS_NOT_INJECTION)
def test_ordinary_javascript_is_not_flagged(line):
    assert finalgates.scan_text("ui.js", f"{line}\n") == []


def test_the_rules_follow_the_file_type():
    assert finalgates.scan_text("notes.txt", "eval(user_input)\n") == []
    assert finalgates.scan_text("ui.js", "pickle.loads(data)\n") == []
    assert finalgates.scan_text("app.py", "el.innerHTML = `<b>${x}</b>`\n") == []
    assert finalgates.scan_text("app.py", "new Function(body)\n") == []


def test_a_secret_and_an_injection_pattern_on_one_line_give_two_findings():
    text = f"os.system(f'echo {{x}} {SECRET}')\n"

    assert sorted(_kinds(finalgates.scan_text("a.py", text))) == ["injection_pattern", "secret_in_tree"]


def test_a_minified_line_is_only_read_up_to_the_line_limit():
    long_line = "x = 1;" * 1000 + "eval(payload)"     # the eval sits far beyond 2000 characters

    assert finalgates.scan_text("min.js", long_line + "\n") == []


# ============================================================================================================
# scan_tree: real repos
# ============================================================================================================


def test_scan_tree_of_a_clean_repo_has_no_findings(tmp_path):
    assert _scan(tmp_path, {"app.py": "print('hi')\n", "docs/notes.md": "text\n"}) == []


def test_scan_tree_flags_a_tracked_env_file(tmp_path):
    findings = _scan(tmp_path, {".env": "DEBUG=1\n"})

    assert [(f.kind, f.path, f.line) for f in findings] == [("secret_file_tracked", ".env", None)]
    assert finalgates.blocking(findings) == findings


def test_scan_tree_allows_the_env_templates(tmp_path):
    assert _scan(tmp_path, {".env.example": "DEBUG=\n", ".env.sample": "DEBUG=\n"}) == []


@pytest.mark.parametrize("name", [
    "certs/server.pem", "id_rsa", "keys/id_ed25519", "deploy.key", "store.p12", "store.pfx", "vault.kdbx",
    "credentials.json", "Credentials.JSON", "config/.env.production", "sub/.env", "VAULT.KDBX",
])
def test_scan_tree_flags_a_tracked_file_whose_name_marks_it_as_secret(tmp_path, name):
    findings = _scan(tmp_path, {name: "not a secret in itself\n"})

    assert [(f.kind, f.path) for f in findings] == [("secret_file_tracked", name)]


def test_scan_tree_still_scans_the_content_of_an_allowed_template(tmp_path):
    findings = _scan(tmp_path, {".env.example": f"API_KEY={SECRET}\n"})

    assert [(f.kind, f.path, f.line) for f in findings] == [("secret_in_tree", ".env.example", 1)]


def test_scan_tree_reports_a_secret_by_path_and_line_and_never_the_value(tmp_path):
    findings = _scan(tmp_path, {"src/config.py": f"import os\n\nAPI = '{SECRET}'\n", "ok.py": "x = 1\n"})

    assert [(f.kind, f.path, f.line) for f in findings] == [("secret_in_tree", "src/config.py", 3)]
    assert SECRET not in repr(findings) and SECRET_TAIL not in repr(findings)
    assert all(SECRET_TAIL not in finalgates.format_finding(f) for f in findings)


def test_scan_tree_skips_a_binary_file_without_a_note(tmp_path):
    assert _scan(tmp_path, {"blob.bin": b"\x00\x01\x02" + SECRET.encode() + b"\n"}) == []


def test_scan_tree_decides_binary_from_the_first_8000_bytes_only(tmp_path):
    """git's own rule: a NUL later than that does not make a text file binary."""
    late_nul = b"a" * 9000 + b"\n" + SECRET.encode() + b"\n" + b"\x00"

    assert [(f.kind, f.line) for f in _scan(tmp_path, {"late.txt": late_nul})] == [("secret_in_tree", 2)]


def test_scan_tree_skips_an_oversized_file_and_says_so(tmp_path):
    head = f"k = '{SECRET}'\n"
    findings = _scan(tmp_path, {"big.txt": _exact_size(1_000_001, head)})

    assert [(f.kind, f.path, f.line) for f in findings] == [("skipped", "big.txt", None)]
    assert "1000001" in findings[0].detail and "1000000" in findings[0].detail
    assert finalgates.blocking(findings) == []


def test_scan_tree_scans_a_file_of_exactly_the_size_limit(tmp_path):
    head = f"k = '{SECRET}'\n"
    findings = _scan(tmp_path, {"edge.txt": _exact_size(1_000_000, head)})

    assert [(f.kind, f.path, f.line) for f in findings] == [("secret_in_tree", "edge.txt", 1)]


def test_scan_tree_size_limit_is_a_parameter(tmp_path):
    findings = _scan(tmp_path, {"a.txt": _exact_size(50, f"k = '{SECRET}'\n")}, max_file_bytes=10)

    assert [(f.kind, f.path) for f in findings] == [("skipped", "a.txt")]     # README.md is 6 bytes: still scanned
    assert "50 bytes" in findings[0].detail and "10 byte limit" in findings[0].detail


def test_scan_tree_reports_a_tracked_generated_tree_once_with_a_count(tmp_path):
    files = {
        "pkg/__pycache__/a.cpython-311.pyc": b"\x00\x01",
        "pkg/__pycache__/b.cpython-311.pyc": b"\x00\x02",
        "node_modules/left-pad/index.js": "eval(payload)\n",
        "dist/bundle.js": "var a = 1;\n",
        "x/y.egg-info/PKG-INFO": "Name: y\n",
        "stray.pyc": b"\x00",
        ".venv/lib/site.py": "eval(x)\n",
    }
    findings = _scan(tmp_path, files)
    by_path = {f.path: f for f in findings}

    assert set(by_path) == {"pkg/__pycache__", "node_modules", "dist", "x/y.egg-info", "stray.pyc", ".venv"}
    assert all(f.kind == "generated_artifact_tracked" for f in findings)
    assert "2 tracked file(s)" in by_path["pkg/__pycache__"].detail
    assert "1 tracked file(s)" in by_path["node_modules"].detail
    assert finalgates.blocking(findings) == findings


def test_scan_tree_does_not_read_the_files_of_a_generated_tree(tmp_path):
    files = {"node_modules/x/index.js": f"eval(payload)\nk = '{SECRET}'\n"}

    assert _kinds(_scan(tmp_path, files)) == ["generated_artifact_tracked"]


def test_scan_tree_does_not_mistake_source_files_for_artifacts(tmp_path):
    files = {
        "build": "a file called build\n", "dist": "a file called dist\n", "scripts/build.py": "x = 1\n",
        "src/dist.py": "y = 2\n", "builder/main.py": "z = 3\n", "src/distribution/a.py": "w = 4\n",
    }

    assert _scan(tmp_path, files) == []


def test_scan_tree_reads_generated_directory_names_case_insensitively(tmp_path):
    assert [f.path for f in _scan(tmp_path, {"Node_Modules/x.js": "a\n"})] == ["Node_Modules"]


def test_scan_tree_flags_stray_generated_files_outside_a_generated_directory(tmp_path):
    findings = _scan(tmp_path, {"mod.pyo": b"\x00", "lib/.DS_Store": b"\x00", "ok.py": "x = 1\n"})

    assert [(f.kind, f.path) for f in findings] == [
        ("generated_artifact_tracked", "lib/.DS_Store"), ("generated_artifact_tracked", "mod.pyo"),
    ]


def test_scan_tree_does_not_read_the_target_of_a_symbolic_link(tmp_path):
    repo = _make_repo(tmp_path / "r", {"a.txt": "clean\n"})
    blob = subprocess.run(
        ["git", "-C", str(repo), "hash-object", "-w", "--stdin"], input=SECRET.encode(), capture_output=True,
        check=True,
    ).stdout.decode().strip()
    _git(repo, "update-index", "--add", "--cacheinfo", f"120000,{blob},link")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "symlink")

    assert finalgates.scan_tree(repo, "HEAD") == []


def test_scan_tree_reads_a_file_that_is_not_valid_utf8(tmp_path):
    findings = _scan(tmp_path, {"latin1.txt": b"caf\xe9 \xff\xfe\n" + SECRET.encode() + b"\n"})

    assert [(f.kind, f.line) for f in findings] == [("secret_in_tree", 2)]


def test_scan_tree_flags_a_secret_file_inside_a_generated_tree_twice_over(tmp_path):
    findings = _scan(tmp_path, {"dist/.env": "A=1\n"})

    assert sorted(f.kind for f in findings) == ["generated_artifact_tracked", "secret_file_tracked"]


def test_scan_tree_reports_injection_patterns_as_advisory_findings(tmp_path):
    files = {"app.py": "import os\nos.system(f'ls {x}')\n", "ui.js": "const a = 1;\nel.innerHTML = `${x}`;\n"}
    findings = _scan(tmp_path, files)

    assert [(f.kind, f.path, f.line) for f in findings] == [
        ("injection_pattern", "app.py", 2), ("injection_pattern", "ui.js", 2),
    ]
    assert finalgates.blocking(findings) == [] and len(finalgates.advisory(findings)) == 2


def test_scan_tree_findings_are_sorted_by_path_then_line(tmp_path):
    files = {
        "z.txt": f"{SECRET}\n", "a.txt": f"x\n{SECRET}\n{SECRET}\n", ".env": "A=1\n",
    }
    findings = _scan(tmp_path, files)

    assert [(f.path, f.line) for f in findings] == [(".env", None), ("a.txt", 2), ("a.txt", 3), ("z.txt", 1)]


def test_scan_tree_reads_a_non_ascii_path_and_formats_it_as_ascii(tmp_path):
    name = f"caf{E_ACUTE}.py"
    findings = _scan(tmp_path, {name: f"k = '{SECRET}'\n"})

    assert [(f.path, f.line) for f in findings] == [(name, 1)]
    assert finalgates.format_finding(findings[0]).isascii()


def test_scan_tree_redacts_a_secret_shaped_file_name(tmp_path):
    findings = _scan(tmp_path, {f"{SECRET}.pem": "x\n"})

    assert [f.kind for f in findings] == ["secret_file_tracked"]
    assert SECRET_TAIL not in repr(findings)


def test_scan_tree_reads_the_tree_at_the_ref_it_is_given_not_the_working_copy(tmp_path):
    repo = _make_repo(tmp_path / "r", {"a.txt": "clean\n"})
    clean_head = _head(repo)
    _write_files(repo, {"a.txt": f"{SECRET}\n"})       # uncommitted: not in the tree at HEAD
    _write_files(repo, {"junk.txt": f"{SECRET}\n"})     # untracked

    assert finalgates.scan_tree(repo, clean_head) == []
    _commit_all(repo, "leak")
    assert [f.path for f in finalgates.scan_tree(repo, "HEAD")] == ["a.txt", "junk.txt"]
    assert finalgates.scan_tree(repo, clean_head) == []


def test_scan_tree_leaves_a_submodule_entry_alone(tmp_path):
    repo = _make_repo(tmp_path / "r", {"a.txt": "clean\n"})
    _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{_head(repo)},vendor/sub")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "gitlink")

    assert finalgates.scan_tree(repo, "HEAD") == []


def test_scan_tree_handles_an_empty_file(tmp_path):
    assert _scan(tmp_path, {"empty.txt": b""}) == []


def test_scan_tree_scans_identical_content_under_every_path(tmp_path):
    findings = _scan(tmp_path, {"a.txt": f"{SECRET}\n", "b/c.txt": f"{SECRET}\n"})

    assert [(f.path, f.line) for f in findings] == [("a.txt", 1), ("b/c.txt", 1)]


def test_scan_tree_missing_repository_is_a_scan_error(tmp_path):
    findings = finalgates.scan_tree(tmp_path / "does-not-exist", "HEAD")

    assert _kinds(findings) == ["scan_error"] and findings[0].path == ""
    assert finalgates.blocking(findings) == findings


def test_scan_tree_unknown_ref_is_a_scan_error(tmp_path):
    repo = _make_repo(tmp_path / "r")

    assert _kinds(finalgates.scan_tree(repo, "no-such-branch")) == ["scan_error"]


@pytest.mark.parametrize("ref", ["--output=/tmp/x", "", "a b", None, "a\nb"])
def test_scan_tree_refuses_an_unusable_ref(tmp_path, ref):
    repo = _make_repo(tmp_path / "r")
    findings = finalgates.scan_tree(repo, ref)

    assert _kinds(findings) == ["scan_error"] and "unusable revision" in findings[0].detail


@pytest.mark.parametrize("error", [
    OSError("git is not installed"), subprocess.TimeoutExpired(cmd="git", timeout=1), RuntimeError("boom"),
])
def test_scan_tree_never_raises(tmp_path, monkeypatch, error):
    def explode(*args, **kwargs):
        raise error

    monkeypatch.setattr(subprocess, "run", explode)
    findings = finalgates.scan_tree(tmp_path, "HEAD")

    assert _kinds(findings) == ["scan_error"]


def test_scan_tree_timeout_is_passed_to_git(tmp_path, monkeypatch):
    seen = []
    real_run = subprocess.run

    def spy(args, **kwargs):
        seen.append(kwargs.get("timeout"))
        return real_run(args, **kwargs)

    repo = _make_repo(tmp_path / "r")
    monkeypatch.setattr(subprocess, "run", spy)
    finalgates.scan_tree(repo, "HEAD", timeout=17)

    assert seen and set(seen) == {17}


def test_scan_tree_reads_blobs_in_batches_so_a_big_tree_is_never_held_whole(tmp_path, monkeypatch):
    monkeypatch.setattr(finalgates, "_BATCH_BYTES", 10)     # every blob is its own batch
    files = {f"f{n}.txt": f"line {n} {SECRET}\n" if n % 2 else f"line {n}\n" for n in range(6)}
    findings = _scan(tmp_path, files)

    assert [f.path for f in findings] == ["f1.txt", "f3.txt", "f5.txt"]


def test_an_object_git_cannot_return_is_a_scan_error(tmp_path, monkeypatch):
    monkeypatch.setattr(finalgates, "_read_blobs", lambda repo, oids, timeout: {})
    findings = _scan(tmp_path, {"a.txt": "text\n"})

    assert {f.path for f in _of_kind(findings, "scan_error")} == {"README.md", "a.txt"}
    assert finalgates.blocking(findings) == findings


# ============================================================================================================
# run_gate4
# ============================================================================================================


def _run4(conn, plan, head, *, scan=_clean_scan, run_gate=None, runner=None, repo="repo-path", **kwargs):
    return finalgates.run_gate4(
        repo, plan, conn, head, scan=scan, run_gate=run_gate or FakeRunGate(), runner=runner, **kwargs,
    )


def test_gate4_builtin_scan_blocks_before_the_plan_commands_run(conn):
    fake = FakeRunGate()
    plan = _plan(profiles={"g": ["echo ok"], "gate4": ["pip-audit"]})
    outcome = _run4(
        conn, plan, "a" * 40, scan=lambda repo, ref: [_finding("secret_in_tree", "src/a.py", 3)], run_gate=fake,
    )

    assert outcome.passed is False and outcome.gate == "gate4" and outcome.commit_sha == "a" * 40
    assert fake.calls == []
    assert "NOT run" in outcome.detail and "secret_in_tree src/a.py:3" in outcome.detail
    assert any("gate4 commands were not run" in note for note in outcome.notes)
    assert [row["result"] for row in _final_rows(conn, "gate4")] == ["fail"]


def test_gate4_runs_the_plan_commands_through_run_gate_and_their_failure_fails_the_gate(conn):
    fake = FakeRunGate(passed=False, detail="pip-audit: 2 vulnerabilities")
    runner = object()
    plan = _plan(profiles={"g": ["echo ok"], "gate4": ["pip-audit", "npm audit --audit-level=high"]})
    outcome = _run4(conn, plan, "a" * 40, run_gate=fake, runner=runner, timeout_per_command=77, repo="the-repo")

    assert outcome.passed is False
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert (call["repo"], call["sha"], call["gate"]) == ("the-repo", "a" * 40, "gate4")
    assert call["commands"] == ["pip-audit", "npm audit --audit-level=high"]
    assert call["task_key"] == "__final__" and call["timeout_per_command"] == 77 and call["runner"] is runner
    assert call["conn"] is None            # run_gate writes no row of its own: one combined row is recorded here
    assert "2 vulnerabilities" in outcome.detail
    assert [row["result"] for row in _final_rows(conn, "gate4")] == ["fail"]


def test_gate4_passes_when_the_scan_is_clean_and_the_commands_pass(conn):
    plan = _plan(profiles={"g": ["echo ok"], "gate4": ["pip-audit"]})
    outcome = _run4(conn, plan, "a" * 40, run_gate=FakeRunGate(detail="no known vulnerabilities"))

    assert outcome.passed is True and outcome.notes == ()
    assert gates.last_gate_result(conn, "__final__", "gate4", "a" * 40) == "pass"
    assert bounds.final_gates_green(conn, "a" * 40) is False     # Gate 5 has not run: one gate is not both


def test_gate4_without_a_profile_is_decided_by_the_scan_alone_and_says_so(conn):
    fake = FakeRunGate()
    outcome = _run4(conn, _plan(profiles={"g": ["echo ok"]}), "a" * 40, run_gate=fake)

    assert outcome.passed is True and fake.calls == []
    assert "no gate4 profile in the plan: built-in scan only" in outcome.notes
    assert "no gate4 profile in the plan: built-in scan only" in outcome.detail
    assert [row["result"] for row in _final_rows(conn, "gate4")] == ["pass"]


def test_gate4_records_exactly_one_combined_row(conn):
    plan = _plan(profiles={"g": ["echo ok"], "gate4": ["pip-audit"]})
    _run4(
        conn, plan, "a" * 40, run_gate=FakeRunGate(detail="audit output line"),
        scan=lambda repo, ref: [_finding("injection_pattern", "a.py", 2, "eval or exec of a non-literal (heuristic)")],
    )
    rows = _final_rows(conn, "gate4")

    assert len(rows) == 1
    assert "built-in scan of the tracked tree" in rows[0]["detail"] and "audit output line" in rows[0]["detail"]
    assert len(_events(conn, "final_gate_recorded")) == 1


def test_gate4_advisory_findings_do_not_fail_the_gate_and_are_kept(conn):
    advisory = _finding("injection_pattern", "a.py", 2, "eval or exec of a non-literal (heuristic)")
    outcome = _run4(conn, _plan(profiles={"g": ["x"]}), "a" * 40, scan=lambda repo, ref: [advisory])

    assert outcome.passed is True and outcome.findings == (advisory,)
    assert isinstance(outcome.findings, tuple) and isinstance(outcome.notes, tuple)


def test_gate4_skipped_files_become_a_note_and_not_a_failure(conn):
    skipped = _finding("skipped", "big.bin", None, "not scanned: 2000000 bytes is over the 1000000 byte limit")
    outcome = _run4(conn, _plan(profiles={"g": ["x"]}), "a" * 40, scan=lambda repo, ref: [skipped])

    assert outcome.passed is True
    assert any("1 file(s) over the scan size limit were not scanned" in note for note in outcome.notes)


def test_gate4_a_scan_that_raises_fails_the_gate_with_a_scan_error(conn):
    def broken(repo, ref):
        raise RuntimeError("scanner exploded")

    fake = FakeRunGate()
    outcome = _run4(conn, _plan(profiles={"g": ["x"], "gate4": ["audit"]}), "a" * 40, scan=broken, run_gate=fake)

    assert outcome.passed is False and fake.calls == []
    assert _kinds(outcome.findings) == ["scan_error"] and "scanner exploded" in outcome.findings[0].detail
    assert [row["result"] for row in _final_rows(conn, "gate4")] == ["fail"]


def test_gate4_a_run_gate_that_raises_propagates_and_records_nothing(conn):
    plan = _plan(profiles={"g": ["x"], "gate4": ["audit"]})

    with pytest.raises(RuntimeError, match="docker is down"):
        _run4(conn, plan, "a" * 40, run_gate=FakeRunGate(raises=RuntimeError("docker is down")))
    assert _final_rows(conn) == []


def test_gate4_detail_is_redacted_and_capped(conn):
    noisy = f"start {SECRET}\n" + "line of audit output\n" * 3000 + "THE END OF THE OUTPUT"
    plan = _plan(profiles={"g": ["x"], "gate4": ["audit"]})
    outcome = _run4(conn, plan, "a" * 40, run_gate=FakeRunGate(passed=False, detail=noisy))
    row = _final_rows(conn, "gate4")[0]

    for text in (outcome.detail, row["detail"]):
        assert SECRET not in text and SECRET_TAIL not in text
        assert len(text) < 20_500
        assert "characters omitted" in text and text.endswith("THE END OF THE OUTPUT")
        assert "built-in scan of the tracked tree" in text          # the start survives too, not only the end


def test_gate4_the_recorded_row_and_the_outcome_agree(conn):
    outcome = _run4(conn, _plan(profiles={"g": ["x"]}), "a" * 40)

    assert _final_rows(conn, "gate4")[0]["detail"] == outcome.detail


def test_gate4_ignores_blank_and_non_text_commands_in_the_profile(conn):
    fake = FakeRunGate()
    plan = _plan(profiles={"g": ["x"], "gate4": ["  ", "audit", None, 5]})
    _run4(conn, plan, "a" * 40, run_gate=fake)

    assert fake.calls[0]["commands"] == ["audit"]


def test_gate4_a_profile_with_only_blank_commands_counts_as_no_profile(conn):
    fake = FakeRunGate()
    outcome = _run4(conn, _plan(profiles={"g": ["x"], "gate4": ["   "]}), "a" * 40, run_gate=fake)

    assert fake.calls == [] and "no gate4 profile in the plan: built-in scan only" in outcome.notes


def test_gate4_blank_commit_is_refused_by_the_record(conn):
    with pytest.raises(ValueError):
        _run4(conn, _plan(profiles={"g": ["x"]}), "")


def test_gate4_end_to_end_with_the_real_scan_and_the_real_run_gate(tmp_path, conn):
    repo = _make_repo(tmp_path / "repo", {"app.py": "print('hi')\n"})
    plan = _plan(profiles={"g": ["echo ok"], "gate4": ["python -c \"print('audit ok')\""]})
    outcome = finalgates.run_gate4(repo, plan, conn, _head(repo))

    assert outcome.passed is True and "audit ok" in outcome.detail
    assert len(_final_rows(conn, "gate4")) == 1


def test_gate4_end_to_end_a_tracked_env_file_fails_before_any_command_runs(tmp_path, conn):
    marker = tmp_path / "ran.txt"
    repo = _make_repo(tmp_path / "repo", {".env": "A=1\n"})
    plan = _plan(profiles={"g": ["echo ok"], "gate4": [f"python -c \"open(r'{marker}', 'w').write('x')\""]})
    outcome = finalgates.run_gate4(repo, plan, conn, _head(repo))

    assert outcome.passed is False and not marker.exists()
    assert [(f.kind, f.path) for f in outcome.findings] == [("secret_file_tracked", ".env")]


# ============================================================================================================
# run_gate5
# ============================================================================================================


def _run5(conn, plan, head="a" * 40, *, run_gate=None, runner=None, repo="repo-path", **kwargs):
    return finalgates.run_gate5(repo, plan, conn, head, run_gate=run_gate or FakeRunGate(), runner=runner, **kwargs)


def test_gate5_runs_the_plan_profile(conn):
    fake = FakeRunGate(detail="GET /health 200")
    runner = object()
    plan = _plan(profiles={"g": ["pytest -q"], "gate5": ["python app.py --probe /health"]})
    outcome = _run5(conn, plan, run_gate=fake, runner=runner, timeout_per_command=91, repo="the-repo")

    assert outcome.passed is True and outcome.gate == "gate5" and outcome.notes == ()
    call = fake.calls[0]
    assert call["commands"] == ["python app.py --probe /health"]
    assert (call["repo"], call["sha"], call["gate"]) == ("the-repo", "a" * 40, "gate5")
    assert call["task_key"] == "__final__" and call["timeout_per_command"] == 91 and call["runner"] is runner
    assert call["conn"] is None
    assert "GET /health 200" in outcome.detail
    assert [row["result"] for row in _final_rows(conn, "gate5")] == ["pass"]


def test_gate5_fallback_is_every_distinct_task_command_in_first_seen_order(conn):
    profiles = {
        "fast": ["ruff check .", "pytest -q"],
        "gate4": ["pip-audit"],
        "slow": ["pytest -q", "mypy .", "ruff check ."],
        "other": [" pytest -q ", "npm test"],
    }
    fake = FakeRunGate()
    outcome = _run5(conn, _plan(profiles=profiles), run_gate=fake)

    assert fake.calls[0]["commands"] == ["ruff check .", "pytest -q", "mypy .", "npm test"]
    assert "no gate5 profile in the plan: ran every task gate profile on the integration HEAD" in outcome.notes
    assert outcome.passed is True and len(_final_rows(conn, "gate5")) == 1


def test_gate5_fallback_does_not_rerun_the_security_profile(conn):
    fake = FakeRunGate()
    _run5(conn, _plan(profiles={"gate4": ["pip-audit"], "g": ["pytest -q"]}), run_gate=fake)

    assert fake.calls[0]["commands"] == ["pytest -q"]


def test_gate5_an_empty_gate5_profile_falls_back_to_the_task_profiles(conn):
    fake = FakeRunGate()
    outcome = _run5(conn, _plan(profiles={"g": ["pytest -q"], "gate5": []}), run_gate=fake)

    assert fake.calls[0]["commands"] == ["pytest -q"] and outcome.notes


def test_gate5_a_plan_with_no_profiles_at_all_fails_with_a_clear_message(conn):
    fake = FakeRunGate()
    outcome = _run5(conn, _plan(profiles={}), run_gate=fake)

    assert outcome.passed is False and fake.calls == []
    assert "nothing to run" in outcome.detail and "not a pass" in outcome.detail
    assert [row["result"] for row in _final_rows(conn, "gate5")] == ["fail"]


def test_gate5_a_plan_with_only_a_security_profile_has_nothing_to_smoke_test(conn):
    outcome = _run5(conn, _plan(profiles={"gate4": ["pip-audit"]}))

    assert outcome.passed is False and "nothing to run" in outcome.detail


def test_gate5_failing_commands_fail_the_gate(conn):
    outcome = _run5(conn, _plan(profiles={"g": ["x"]}), run_gate=FakeRunGate(passed=False, detail="1 failed"))

    assert outcome.passed is False and "1 failed" in outcome.detail
    assert [row["result"] for row in _final_rows(conn, "gate5")] == ["fail"]


def test_gate5_a_run_gate_that_raises_propagates_and_records_nothing(conn):
    with pytest.raises(RuntimeError, match="no docker"):
        _run5(conn, _plan(profiles={"g": ["x"]}), run_gate=FakeRunGate(raises=RuntimeError("no docker")))
    assert _final_rows(conn) == []


def test_gate5_detail_is_redacted(conn):
    outcome = _run5(conn, _plan(profiles={"g": ["x"]}), run_gate=FakeRunGate(detail=f"token {SECRET}"))

    assert SECRET not in outcome.detail and SECRET not in _final_rows(conn, "gate5")[0]["detail"]


@pytest.mark.parametrize("profiles", [None, ["not", "a", "mapping"], {"g": "pytest -q"}, {"g": None}])
def test_gate5_a_plan_whose_profiles_are_not_lists_of_commands_has_nothing_to_run(conn, profiles):
    plan = types.SimpleNamespace(project="p1", gate_profiles=profiles)
    outcome = _run5(conn, plan)

    assert outcome.passed is False and "nothing to run" in outcome.detail


def test_gate5_end_to_end_with_the_real_run_gate(tmp_path, conn):
    repo = _make_repo(tmp_path / "repo", {"app.py": "print('hi')\n"})
    plan = _plan(profiles={"g": ["python app.py", "python -c \"print('smoke ok')\""]})
    outcome = finalgates.run_gate5(repo, plan, conn, _head(repo))

    assert outcome.passed is True and "hi" in outcome.detail and "smoke ok" in outcome.detail
    assert len(_final_rows(conn, "gate5")) == 1


# ============================================================================================================
# final_gate_question
# ============================================================================================================


def test_the_question_names_the_gate_the_commit_and_the_first_findings():
    findings = [_finding("secret_in_tree", f"src/f{n}.py", n) for n in range(1, 9)]
    text = finalgates.final_gate_question(_outcome("gate4", passed=False, findings=findings))

    assert text.startswith("Gate 4 (security) failed on the integration branch at commit aaaaaaaaaa")
    assert "8 blocking finding(s), the first 5:" in text
    assert "secret_in_tree src/f1.py:1" in text and "secret_in_tree src/f5.py:5" in text
    assert "src/f6.py" not in text and "and 3 more" in text
    assert "swarm resume" in text and text.rstrip().endswith("How should this be resolved?")


def test_the_question_explains_a_scan_error_as_a_scan_that_could_not_run():
    scan_error = _finding("scan_error", path="", line=None, detail="git ls-tree failed: not a git repository")
    text = finalgates.final_gate_question(_outcome("gate4", passed=False, findings=[scan_error]))
    plain = finalgates.final_gate_question(_outcome("gate4", passed=False, findings=[_finding("secret_in_tree")]))

    assert "scan_error: git ls-tree failed" in text and "the scan itself could not run" in text
    assert "could not run" not in plain


def test_the_question_for_a_smoke_failure_shows_the_last_lines_of_the_output():
    detail = "\n".join(f"line {n}" for n in range(1, 21)) + "\n[exit 1]"
    text = finalgates.final_gate_question(_outcome("gate5", passed=False, detail=detail, notes=("a note",)))

    assert text.startswith("Gate 5 (smoke) failed") and "a note" in text
    assert "line 19" in text and "line 20" in text and "[exit 1]" in text and "line 3" not in text
    assert text.rstrip().endswith("?")


def test_the_question_never_contains_a_secret_value():
    hostile_finding = finalgates.TreeFinding("secret_file_tracked", f"keys/{SECRET}.pem", None, f"leak {SECRET}")
    findings_case = _outcome("gate4", passed=False, findings=[hostile_finding, _finding("secret_in_tree")],
                             detail=f"output with {SECRET}", notes=(f"note {SECRET}",))
    output_case = _outcome("gate5", passed=False, detail=f"first\nAPI {SECRET}\nlast {SECRET}", notes=(f"n {SECRET}",))

    for outcome in (findings_case, output_case):
        text = finalgates.final_gate_question(outcome)
        assert SECRET not in text and SECRET_TAIL not in text


def test_the_question_is_ascii_and_short_even_with_hostile_input():
    findings = [_finding("secret_in_tree", f"caf{E_ACUTE}/{'x' * 300}{n}.py", n) for n in range(40)]
    outcome = _outcome("gate4", passed=False, findings=findings, detail="z" * 5000)
    text = finalgates.final_gate_question(outcome)

    assert text.isascii() and len(text) <= 1500 and text.rstrip().endswith("How should this be resolved?")


def test_the_question_accepts_a_finalize_result_and_uses_the_failing_gate():
    result = finalgates.FinalizeResult(
        "gate_failed", _outcome("gate4"), _outcome("gate5", passed=False, detail="smoke failed"), None, "x",
    )
    text = finalgates.final_gate_question(result)

    assert text.startswith("Gate 5 (smoke) failed") and "smoke failed" in text


def test_the_question_is_still_asked_for_a_stand_in_that_has_only_a_gate_and_a_verdict():
    full = finalgates.final_gate_question(types.SimpleNamespace(gate="gate4", passed=False))
    bare = finalgates.final_gate_question(types.SimpleNamespace(passed=False))

    assert full.startswith("Gate 4 (security) failed on the integration branch, so the project was not finished.")
    assert bare.startswith("Final gate failed on the integration branch,") and "commit" not in bare.split("\n")[0]
    assert full.rstrip().endswith("How should this be resolved?") and bare.rstrip().endswith("resolved?")


def test_the_question_for_a_result_with_no_failure_is_generic():
    passing = finalgates.FinalizeResult("finished", _outcome("gate4"), _outcome("gate5"), None, "")

    for thing in (passing, _outcome("gate4")):
        text = finalgates.final_gate_question(thing)
        assert "did not pass" in text and text.rstrip().endswith("?")


# ============================================================================================================
# release_summary
# ============================================================================================================


def _summary(world, **kwargs):
    kwargs.setdefault("gate4", _outcome("gate4", head=world.head))
    kwargs.setdefault("gate5", _outcome("gate5", head=world.head))
    kwargs.setdefault("now", NOW)
    return finalgates.release_summary(
        BOARD, world.plan, world.project, MODELS, world.conn, world.head, **kwargs,
    )


@pytest.fixture
def world(tmp_path, conn):
    repo = _make_repo(tmp_path / "repo", {"app.py": "print('hi')\n"})
    plan = _plan()
    _seed_tasks(conn, plan)
    return types.SimpleNamespace(
        repo=repo, conn=conn, plan=plan, project=_project(tmp_path), head=_head(repo), tmp=tmp_path,
    )


def test_release_summary_has_the_project_the_branch_the_head_and_the_time(world):
    summary = _summary(world)

    assert summary["project"] == "ases" and summary["plan_project"] == "p1" and summary["board"] == BOARD
    assert summary["integration_branch"] == "integration" and summary["head"] == world.head
    assert summary["generated_at"] == "2026-09-21T12:00:00+00:00"


def test_release_summary_lists_every_task_with_its_squash_commit_from_merge_records(world):
    _merge_record(world.conn, "T1", "c" * 40, "pass")
    world.conn.execute(      # a review-only task: recorded as a no-op, so no squash commit
        "INSERT INTO merge_records (task_key, candidate_sha, gate3_result, squash_commit, reverted, completed_at) "
        "VALUES ('T2', NULL, 'skipped', NULL, 0, datetime('now'))"
    )
    world.conn.execute(      # a third task that has cards but no merge record at all
        "INSERT INTO plan_tasks (project, task_key, work_card_id, merge_card_id, role, created_at) "
        "VALUES ('p1', 'T3', 'w_T3', 'm_T3', 'coder', datetime('now'))"
    )
    world.plan = _plan(tasks=3, titles={"T1": "scaffold", "T3": "unmerged"})
    tasks = {t["task_key"]: t for t in _summary(world)["tasks"]}

    assert tasks["T1"] == {
        "task_key": "T1", "title": "scaffold", "role": "coder", "work_card_id": "w_T1", "merge_card_id": "m_T1",
        "squash_commit": "c" * 40, "gate3_result": "pass",
    }
    assert (tasks["T2"]["squash_commit"], tasks["T2"]["gate3_result"]) == (None, "skipped")
    assert (tasks["T3"]["squash_commit"], tasks["T3"]["gate3_result"]) == (None, None)
    assert [t["task_key"] for t in _summary(world)["tasks"]] == ["T1", "T2", "T3"]


def test_release_summary_follows_a_fix_card_through_the_current_work_card_id(world):
    world.conn.execute("UPDATE plan_tasks SET work_card_id = 'w_T1_fix1', fix_cards = 1 WHERE task_key = 'T1'")

    assert _summary(world)["tasks"][0]["work_card_id"] == "w_T1_fix1"


def test_release_summary_reports_both_gates_with_status_notes_and_finding_counts(world):
    findings = [
        _finding("injection_pattern", "a.py", 4, "eval or exec of a non-literal (heuristic)"),
        _finding("injection_pattern", "b.py", 9, "pickle load of data (heuristic)"),
        _finding("skipped", "big.bin", None, "not scanned: too big"),
    ]
    gate4 = _outcome("gate4", findings=findings, notes=("no gate4 profile in the plan: built-in scan only",))
    gate5 = _outcome("gate5", notes=("no gate5 profile in the plan: ran every task gate profile on the HEAD",))
    gates_summary = _summary(world, gate4=gate4, gate5=gate5)["gates"]

    assert gates_summary["gate4"]["status"] == "pass" and gates_summary["gate5"]["status"] == "pass"
    assert gates_summary["gate4"]["finding_counts"] == {"injection_pattern": 2, "skipped": 1}
    assert (gates_summary["gate4"]["blocking"], gates_summary["gate4"]["advisory"]) == (0, 2)
    assert gates_summary["gate4"]["notes"] == ["no gate4 profile in the plan: built-in scan only"]
    assert gates_summary["gate5"]["finding_counts"] == {} and len(gates_summary["gate5"]["notes"]) == 1
    listed = gates_summary["gate4"]["findings"]
    assert [(f["kind"], f["path"], f["line"]) for f in listed] == [
        ("injection_pattern", "a.py", 4), ("injection_pattern", "b.py", 9), ("skipped", "big.bin", None),
    ]


def test_release_summary_finding_counts_survive_the_redaction(world):
    """Kinds such as secret_in_tree contain the word secret: redaction must not blank their counts."""
    findings = [_finding("secret_in_tree", "a.py", 1), _finding("secret_in_tree", "b.py", 2)]
    summary = _summary(world, gate4=_outcome("gate4", passed=False, findings=findings))

    assert summary["gates"]["gate4"]["finding_counts"] == {"secret_in_tree": 2}
    assert summary["gates"]["gate4"]["status"] == "fail" and summary["gates"]["gate4"]["blocking"] == 2


def test_release_summary_lists_blocking_findings_first_and_caps_the_list(world):
    findings = [_finding("injection_pattern", f"a{n}.py", n, "d") for n in range(60)]
    findings.append(_finding("scan_error", "", None))
    gate = _summary(world, gate4=_outcome("gate4", passed=False, findings=findings))["gates"]["gate4"]

    assert len(gate["findings"]) == 50 and gate["findings_omitted"] == 11
    assert gate["findings"][0]["kind"] == "scan_error"


def test_release_summary_a_gate_that_did_not_run_says_so(world):
    summary = _summary(world, gate5=None)

    assert summary["gates"]["gate5"]["status"] == "not run" and summary["gates"]["gate5"]["commit_sha"] is None


def test_release_summary_never_carries_a_value_and_redacts_everything(world):
    world.plan = _plan(titles={"T1": f"task with {SECRET} in its title"})
    hostile = finalgates.TreeFinding("injection_pattern", f"src/{SECRET}.py", 3, f"detail {SECRET}")
    events.record(world.conn, "question_asked", {"card_id": "m_T1", "text": f"the answer is {SECRET}"})
    events.record(world.conn, "question_answered", {"card_id": "m_T1", "note": SECRET})
    summary = _summary(world, gate4=_outcome("gate4", findings=[hostile], notes=(f"note {SECRET}",)))
    text = json.dumps(summary)

    assert SECRET not in text and SECRET_TAIL not in text
    assert "[redacted]" in text


def test_release_summary_is_json_serialisable(world):
    json.dumps(_summary(world))


def test_release_summary_counts_questions_replans_recovery_and_repairs(world):
    for _ in range(3):
        events.record(world.conn, "question_asked", {"card_id": "m_T1"})
    for _ in range(2):
        events.record(world.conn, "question_answered", {"card_id": "m_T1", "task_key": "T1", "chars": 5})
    bounds.add_replan(world.conn, "p1")
    bounds.add_replan(world.conn, "p1")
    for project in ("p1", "p1", "other"):
        events.record(world.conn, "recovery_decision", {"project": project, "task_key": "T1", "action": "none"})
    for _ in range(4):
        events.record(world.conn, "reconcile_repair", {"task_key": "T1", "kind": "x", "detail": "y"})
    summary = _summary(world)

    assert summary["questions"] == {"asked": 3, "answered": 2}
    assert summary["replans"] == 2
    assert summary["recovery_decisions"] == 2          # the other project's decision is not this project's
    assert summary["reconcile_repairs"] == 4


def test_release_summary_counts_are_zero_on_a_quiet_project(world):
    summary = _summary(world)

    assert summary["questions"] == {"asked": 0, "answered": 0} and summary["replans"] == 0
    assert summary["recovery_decisions"] == 0 and summary["reconcile_repairs"] == 0
    assert summary["bounds_reached"] == [] and summary["notes"] == []


def test_release_summary_reports_the_bounds_that_were_reached(world):
    world.conn.execute("UPDATE plan_tasks SET fix_cards = 2 WHERE task_key = 'T1'")
    ledger.record_usage(world.conn, "openrouter", "m", 45)      # 50 a day minus the 10 percent reserve
    reached = {(b["name"], b["subject"]): b for b in _summary(world)["bounds_reached"]}

    assert reached[("fix_cards_per_task", "T1")]["used"] == 2 and reached[("fix_cards_per_task", "T1")]["limit"] == 2
    assert reached[("fix_cards_per_task", "T1")]["on_reach"] == "Escalate to the user"
    assert ("provider_requests_per_day", "openrouter") in reached
    assert ("fix_cards_per_task", "T2") not in reached


def test_release_summary_reports_a_wall_clock_bound_in_whole_and_tenth_minutes(world, fake_board):
    started = (NOW.timestamp()) - 50 * 60 - 2          # 50 minutes and 2 seconds ago: over the 45 minute limit
    fake_board.cards["w_T1"] = {"id": "w_T1", "status": "running", "_runs": [{"started_at": started}]}
    reached = {(b["name"], b["subject"]): b for b in _summary(world)["bounds_reached"]}

    assert reached[("card_runtime_minutes", "w_T1")]["used"] == 50.0
    assert reached[("card_runtime_minutes", "w_T1")]["limit"] == 45


def test_release_summary_a_bound_that_cannot_be_measured_is_a_note_not_a_failure(world):
    world.project = _project(world.tmp, budgets={"attempts_per_card": "many"})
    summary = _summary(world)

    assert summary["bounds_reached"] == []
    assert any("bounds could not be measured" in note for note in summary["notes"])
    assert summary["tasks"]                                    # the rest of the summary is intact


def test_release_summary_requests_are_recomputed_from_the_ledger_without_a_report(world):
    ledger.record_usage(world.conn, "openrouter", "m", 7)
    world.conn.execute(
        "INSERT INTO usage_ingested (session_id, profile, provider, model, requests, ingested_at, project, task_key, "
        "card_id) VALUES ('s1', 'coder-1', 'xkiro', 'q', 30, datetime('now'), 'p1', 'T1', 'w_T1'), "
        "('s2', 'coder-1', 'xkiro', 'q', 12, datetime('now'), 'p1', 'T2', 'w_T2'), "
        "('s3', 'coder-1', 'xkiro', 'q', 99, datetime('now'), 'other', 'T1', 'x'), "
        "('s4', 'reviewer', 'gone-provider', 'q', 5, datetime('now'), 'p1', 'T1', 'w_T1')"
    )
    requests = _summary(world)["requests"]
    rows = {row["provider"]: row for row in requests["providers"]}

    assert requests["day"] == "2026-09-21"
    openrouter = rows["openrouter"]
    assert (openrouter["limit"], openrouter["used_today"], openrouter["remaining"]) == (50, 7, 43)
    assert (rows["openrouter"]["project_total"], rows["openrouter"]["sessions"]) == (0, 0)
    assert (rows["xkiro"]["limit"], rows["xkiro"]["remaining"]) == (None, None)
    assert (rows["xkiro"]["project_total"], rows["xkiro"]["sessions"]) == (42, 2)     # only this project's sessions
    assert rows["gone-provider"]["project_total"] == 5 and rows["gone-provider"]["limit"] is None


def test_release_summary_takes_the_daily_numbers_from_the_report_it_is_given(world):
    report = {"budget": {"day": "2026-09-20", "providers": [
        {"provider": "openrouter", "limit": 50, "used": 3, "remaining": 47, "reserve": 5, "status": "ok"},
    ]}}
    requests = _summary(world, report=report)["requests"]

    assert requests["day"] == "2026-09-20"
    assert requests["providers"][0] == {
        "provider": "openrouter", "limit": 50, "used_today": 3, "remaining": 47, "project_total": 0, "sessions": 0,
    }


def test_release_summary_without_a_models_config_has_no_providers(world):
    summary = finalgates.release_summary(
        BOARD, world.plan, world.project, None, world.conn, world.head, gate4=None, gate5=None, now=NOW,
    )

    assert summary["requests"]["providers"] == [] and summary["requests"]["day"] == "2026-09-21"


def test_release_summary_of_a_plan_with_no_tasks(world):
    world.plan = _plan(tasks=0)

    assert _summary(world)["tasks"] == []


def test_release_summary_project_name_falls_back_to_the_plan_project(world):
    world.project = types.SimpleNamespace(budgets={})

    assert _summary(world)["project"] == "p1"


# ============================================================================================================
# write_release_report
# ============================================================================================================


@pytest.fixture
def written_reports(monkeypatch):
    """report.write_report replaced by a recorder that writes the two files, for tests that hand in a fake report."""
    calls = []

    def fake_write_report(report, directory):
        directory = pathlib.Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "report.html").write_text("<html></html>", encoding="utf-8")
        (directory / "report.json").write_text("{}", encoding="utf-8")
        calls.append((report, directory))
        return directory / "report.html", directory / "report.json"

    monkeypatch.setattr(report_mod, "write_report", fake_write_report)
    return calls


def test_write_release_report_creates_the_directory_and_release_md(world, written_reports):
    target = world.tmp / "deep" / "er" / "out"
    path = finalgates.write_release_report(_summary(world), {"fake": "report"}, target)

    assert path == target / "release.md" and path.is_file()
    assert (target / "report.html").is_file() and (target / "report.json").is_file()
    assert written_reports == [({"fake": "report"}, target)]


def test_release_md_is_ascii_only_with_plain_newlines_and_no_forbidden_characters(world, written_reports):
    world.plan = _plan(titles={"T1": f"caf{E_ACUTE} {ARROW} deploy"})
    hostile = finalgates.TreeFinding("injection_pattern", f"src/na{E_ACUTE}ve.py", 3, f"a {ARROW} b")
    path = finalgates.write_release_report(
        _summary(world, gate4=_outcome("gate4", findings=[hostile], notes=(f"note {ARROW}",))), {}, world.tmp / "o",
    )
    raw = path.read_bytes()
    text = raw.decode("ascii")

    assert b"\r" not in raw
    assert EM_DASH not in text and SECTION_SIGN not in text
    assert BACKSLASH + "xe9" in text and BACKSLASH + "u2192" in text


def test_release_md_has_the_headed_sections(world, written_reports):
    text = finalgates.write_release_report(_summary(world), {}, world.tmp / "o").read_text(encoding="ascii")

    for heading in ("ASES release report", "Result", "Gate 4 findings", "Plan tasks", "Requests", "Bounds reached",
                    "Questions and recovery", "Files"):
        assert heading in text
    assert "RELEASED: Gate 4 and Gate 5 are green on the integration HEAD above." in text
    assert "Gate 4 (security): PASS" in text and "Gate 5 (smoke): PASS" in text


def test_release_md_shows_the_facts_the_tasks_and_the_gate_notes(world, written_reports):
    _merge_record(world.conn, "T1", "c" * 40)
    gate4 = _outcome("gate4", notes=("no gate4 profile in the plan: built-in scan only",))
    path = finalgates.write_release_report(_summary(world, gate4=gate4), {}, world.tmp / "o")
    text = path.read_text(encoding="ascii")

    assert re.search(rf"Integration HEAD\s+{world.head}\n", text)
    assert re.search(r"Integration branch\s+integration\n", text)
    assert re.search(r"Generated\s+2026-09-21T12:00:00\+00:00 \(UTC\)\n", text)
    assert "note: no gate4 profile in the plan: built-in scan only" in text
    assert re.search(r"T1\s+task T1\s+coder\s+w_T1\s+m_T1\s+" + "c" * 10 + r"\s+pass", text)


def test_release_md_lists_the_advisory_findings_with_file_and_line(world, written_reports):
    findings = [_finding("injection_pattern", "src/app.py", 12, "os.system call with a built command (heuristic)")]
    text = finalgates.write_release_report(
        _summary(world, gate4=_outcome("gate4", findings=findings)), {}, world.tmp / "o",
    ).read_text(encoding="ascii")

    assert "injection_pattern src/app.py:12: os.system call with a built command (heuristic)" in text
    assert "findings by kind: injection_pattern 1" in text


def test_release_md_says_not_released_when_a_gate_did_not_pass(world, written_reports):
    text = finalgates.write_release_report(
        _summary(world, gate5=_outcome("gate5", passed=False)), {}, world.tmp / "o",
    ).read_text(encoding="ascii")

    assert "NOT RELEASED" in text and "Gate 5 (smoke): FAIL" in text
    assert "Gate 5 (smoke): FAIL" in text and "RELEASED: Gate 4" not in text


def test_release_md_without_a_project_report_says_so_and_skips_the_call(world, written_reports):
    path = finalgates.write_release_report(_summary(world), None, world.tmp / "o")

    assert written_reports == []
    assert "were not written: no project report was available" in path.read_text(encoding="ascii")
    assert [child.name for child in path.parent.iterdir()] == ["release.md"]


def test_release_md_is_still_written_when_the_project_report_cannot_be(world, monkeypatch):
    def broken(report, directory):
        raise OSError("disk full")

    monkeypatch.setattr(report_mod, "write_report", broken)
    path = finalgates.write_release_report(_summary(world), {}, world.tmp / "o")

    assert path.is_file()
    assert "could not be written: OSError: disk full" in path.read_text(encoding="ascii")


def test_release_md_redacts_a_hand_edited_summary(world, written_reports):
    summary = _summary(world)
    summary["project"] = f"leak {SECRET}"
    summary["notes"] = [f"note {SECRET}"]
    text = finalgates.write_release_report(summary, {}, world.tmp / "o").read_text(encoding="ascii")

    assert SECRET not in text and SECRET_TAIL not in text and "[redacted]" in text


def test_release_md_renders_an_empty_summary_without_failing(tmp_path, written_reports):
    text = finalgates.write_release_report({}, None, tmp_path / "o").read_text(encoding="ascii")

    assert "ASES release report" in text and "NOT RELEASED" in text and "Plan tasks" in text


def test_release_md_lists_bounds_and_request_rows(world, written_reports):
    world.conn.execute("UPDATE plan_tasks SET fix_cards = 2 WHERE task_key = 'T1'")
    text = finalgates.write_release_report(_summary(world), {}, world.tmp / "o").read_text(encoding="ascii")

    assert "fix_cards_per_task" in text and "Escalate to the user" in text
    assert "openrouter" in text and "xkiro" in text and "Daily limit" in text


def test_the_release_report_and_the_project_report_sit_side_by_side(world):
    report = report_mod.build_report(BOARD, world.plan, world.project, MODELS, world.conn, now=NOW)
    path = finalgates.write_release_report(_summary(world, report=report), report, world.tmp / "out" / "deep")

    assert sorted(child.name for child in path.parent.iterdir()) == ["release.md", "report.html", "report.json"]
    assert json.loads((path.parent / "report.json").read_text(encoding="utf-8"))["project"]["name"] == "ases"


# ============================================================================================================
# finalize
# ============================================================================================================


class FakeGate:
    """A stand-in for run_gate4 or run_gate5: records its calls and writes the same gate_runs row the real one does
    (bounds.record_final_gate), because bounds.is_finished reads only the controller's own records."""

    def __init__(self, gate, *, passed=True, findings=(), notes=(), raises=None, record=True, on_run=None):
        self.gate, self.passed, self.findings, self.notes = gate, passed, findings, notes
        self.raises, self.record, self.on_run = raises, record, on_run
        self.calls = []

    def __call__(self, repo, plan, conn, head, *, runner=None, timeout_per_command=300):
        self.calls.append({"repo": repo, "head": head, "runner": runner, "timeout": timeout_per_command})
        if self.on_run is not None:
            self.on_run()
        if self.raises is not None:
            raise self.raises
        if self.record:
            bounds.record_final_gate(
                conn, plan.project, self.gate, head, self.passed, detail=f"{self.gate} fake output",
            )
        return finalgates.GateOutcome(
            self.gate, head, self.passed, f"{self.gate} fake output", tuple(self.findings), tuple(self.notes),
        )


class Builds:
    """A recording `build` hook for finalize: returns a small fake report."""

    def __init__(self, raises=None):
        self.calls, self.raises = [], raises

    def __call__(self, board, plan, project, models_config, conn, *, now=None):
        self.calls.append({"board": board, "now": now})
        if self.raises is not None:
            raise self.raises
        return {"fake": "report"}


def _finalize(world, *, run4=None, run5=None, build=None, now=NOW, **kwargs):
    return finalgates.finalize(
        BOARD, world.repo, world.plan, world.project, MODELS, world.conn, now=now,
        run4=run4 or FakeGate("gate4"), run5=run5 or FakeGate("gate5"), build=build or Builds(), **kwargs,
    )


def _report_dir(world):
    return world.tmp / "home" / "reports" / "ases" / STAMP


def test_finalize_is_not_ready_while_a_merge_card_is_not_done(world, fake_board, written_reports):
    fake_board.cards["m_T2"] = {"id": "m_T2", "status": "blocked"}
    run4, run5 = FakeGate("gate4"), FakeGate("gate5")
    result = _finalize(world, run4=run4, run5=run5)

    assert result.status == "not_ready" and result.gate4 is None and result.gate5 is None
    assert "m_T2" in result.reason and "T2" in result.reason and "blocked" in result.reason
    assert run4.calls == [] and run5.calls == []
    assert _events(world.conn, "final_gate_started") == [] and _intent_rows(world.conn) == []


def test_finalize_is_not_ready_when_a_merge_card_cannot_be_read(world, fake_board, written_reports):
    fake_board.cards["m_T1"] = hermes.HermesCommandError(["kanban", "show"], 1, "no such card")
    result = _finalize(world)

    assert result.status == "not_ready" and "m_T1" in result.reason and "could not be read" in result.reason


def test_finalize_is_not_ready_when_a_task_has_no_merge_card_yet(world, written_reports):
    world.conn.execute("DELETE FROM plan_tasks WHERE task_key = 'T2'")
    result = _finalize(world)

    assert result.status == "not_ready" and "task T2 has no merge card yet" in result.reason


def test_finalize_is_not_ready_for_a_plan_with_no_tasks(world, written_reports):
    world.plan = _plan(tasks=0)
    result = _finalize(world)

    assert result.status == "not_ready" and "no tasks" in result.reason


def test_finalize_reads_only_as_many_merge_cards_as_it_needs_to_refuse(world, fake_board, written_reports):
    fake_board.cards["m_T1"] = {"id": "m_T1", "status": "ready"}
    _finalize(world)

    assert fake_board.shown == ["m_T1"]


@pytest.mark.parametrize("status", ["stopped", "paused"])
def test_finalize_runs_nothing_for_a_stopped_or_paused_project(world, written_reports, status):
    bounds.set_status(world.conn, "p1", status, "held")
    run4, run5 = FakeGate("gate4"), FakeGate("gate5")
    result = _finalize(world, run4=run4, run5=run5)

    assert result.status == "not_ready" and status in result.reason
    assert run4.calls == [] and run5.calls == []
    assert _intent_rows(world.conn) == [] and _final_rows(world.conn) == []


def test_finalize_error_when_the_integration_branch_cannot_be_read(world, written_reports):
    world.plan = _plan(branch="no-such-branch")
    run4 = FakeGate("gate4")
    result = _finalize(world, run4=run4)

    assert result.status == "error" and "no-such-branch" in result.reason and run4.calls == []
    assert result.gate4 is None and result.report_path is None


def test_finalize_error_when_the_repository_is_missing(world, written_reports):
    result = finalgates.finalize(
        BOARD, world.tmp / "nowhere", world.plan, world.project, MODELS, world.conn, now=NOW,
        run4=FakeGate("gate4"), run5=FakeGate("gate5"), build=Builds(),
    )

    assert result.status == "error"


def test_finalize_refuses_a_branch_name_that_looks_like_an_option(world, written_reports):
    world.plan = _plan(branch="--all")

    assert _finalize(world).status == "error"


def test_finalize_prefers_the_branch_over_a_tag_of_the_same_name(world, written_reports):
    _git(world.repo, "-c", "user.email=t@t", "-c", "user.name=t", "tag", "-a", "-m", "t", "integration", "HEAD~0")
    _write_files(world.repo, {"more.txt": "more\n"})
    tip = _commit_all(world.repo, "second")
    run4 = FakeGate("gate4")
    _finalize(world, run4=run4)

    assert run4.calls[0]["head"] == tip


def test_finalize_gate4_red_stops_before_gate5(world, written_reports):
    run4 = FakeGate("gate4", passed=False, findings=[_finding("secret_in_tree", "src/a.py", 3)])
    run5 = FakeGate("gate5")
    result = _finalize(world, run4=run4, run5=run5)

    assert result.status == "gate_failed" and result.gate4.passed is False and result.gate5 is None
    assert "Gate 4 (security) failed" in result.reason and "1 blocking finding(s)" in result.reason
    assert run5.calls == [] and result.report_path is None
    assert written_reports == []
    assert bounds.get_state(world.conn, "p1") is None or bounds.get_state(world.conn, "p1")["status"] != "finished"
    assert bounds.release_report_written(world.conn, "p1") is False
    rows = _intent_rows(world.conn)
    assert len(rows) == 1 and rows[0]["completed_at"] and f"gate4 fail on {world.head}" in rows[0]["detail"]
    event = _events(world.conn, "final_gate_result")[0]
    assert (event["passed"], event["blocking"], event["finding_counts"]) == (False, 1, {"secret_in_tree": 1})


def test_finalize_gate5_red_after_gate4_green_records_both(world, written_reports):
    run5 = FakeGate("gate5", passed=False)
    result = _finalize(world, run5=run5)

    assert result.status == "gate_failed" and result.gate4.passed is True and result.gate5.passed is False
    assert "Gate 5 (smoke) failed" in result.reason and "blocking" not in result.reason
    assert gates.last_gate_result(world.conn, "__final__", "gate4", world.head) == "pass"
    assert gates.last_gate_result(world.conn, "__final__", "gate5", world.head) == "fail"
    assert written_reports == [] and bounds.release_report_written(world.conn, "p1") is False


def test_finalize_both_green_writes_the_report_marks_it_and_finishes_the_project(world, written_reports):
    result = _finalize(world)
    release = _report_dir(world) / "release.md"

    assert result.status == "finished" and result.report_path == release and release.is_file()
    assert result.gate4.passed and result.gate5.passed and "green" in result.reason
    assert (release.parent / "report.html").is_file() and (release.parent / "report.json").is_file()
    assert bounds.final_gates_green(world.conn, world.head) is True
    assert bounds.release_report_written(world.conn, "p1") is True
    assert bounds.get_state(world.conn, "p1")["status"] == "finished"
    assert bounds.is_finished(BOARD, world.plan, world.head, conn=world.conn) is True
    assert written_reports == [({"fake": "report"}, release.parent)]


def test_finalize_records_every_step_as_an_event(world, written_reports):
    _finalize(world)
    release = str(_report_dir(world) / "release.md")

    assert [e["gate"] for e in _events(world.conn, "final_gate_started")] == ["gate4", "gate5"]
    results = _events(world.conn, "final_gate_result")
    assert [(e["gate"], e["passed"], e["commit_sha"]) for e in results] == [
        ("gate4", True, world.head), ("gate5", True, world.head),
    ]
    assert all(e["project"] == "p1" for e in results)
    assert _events(world.conn, "release_report_written") == [{"project": "p1", "path": release}]
    assert _events(world.conn, "project_finished") == [
        {"project": "p1", "commit_sha": world.head, "report_path": release},
    ]
    assert len(_events(world.conn, "final_gate_recorded")) == 2


def test_finalize_writes_and_completes_an_intent_around_each_gate_and_the_report(world, written_reports):
    _finalize(world)
    rows = _intent_rows(world.conn)

    assert [(r["kind"], r["key"]) for r in rows] == [
        ("run_gate", "p1"), ("run_gate", "p1"), ("write_release_report", "p1"),
    ]
    assert all(r["completed_at"] for r in rows) and intents.open_intents(world.conn, "p1") == []
    assert f"gate4 pass on {world.head}" in rows[0]["detail"] and f"gate5 pass on {world.head}" in rows[1]["detail"]
    assert str(_report_dir(world) / "release.md") in rows[2]["detail"]


def test_finalize_opens_the_intent_before_the_gate_runs(world, written_reports):
    seen = []

    def peek():
        seen.append([(r["kind"], r["completed_at"]) for r in _intent_rows(world.conn)])

    _finalize(world, run4=FakeGate("gate4", on_run=peek))

    assert seen == [[("run_gate", None)]]


def test_finalize_is_idempotent_after_success(world, written_reports):
    first = _finalize(world)
    before = (
        world.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], len(_intent_rows(world.conn)),
        len(_final_rows(world.conn)),
    )
    run4 = FakeGate("gate4", raises=AssertionError("must not run"))
    run5 = FakeGate("gate5", raises=AssertionError("must not run"))
    builds = Builds(raises=AssertionError("must not build"))
    second = _finalize(world, run4=run4, run5=run5, build=builds)

    assert second.status == "finished" and second.report_path == first.report_path
    assert second.reason == "the project is already finished"
    assert run4.calls == [] and run5.calls == [] and builds.calls == []
    assert before == (
        world.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], len(_intent_rows(world.conn)),
        len(_final_rows(world.conn)),
    )
    assert len(written_reports) == 1


def test_finalize_finishes_a_project_whose_gates_and_report_are_recorded_but_status_is_not_flipped(world):
    """The crash between marking the report and finishing the project: the next call finishes it, running nothing."""
    bounds.start_project(world.conn, "p1", now=NOW)
    bounds.record_final_gate(world.conn, "p1", "gate4", world.head, "pass")
    bounds.record_final_gate(world.conn, "p1", "gate5", world.head, "pass")
    bounds.mark_release_report(world.conn, "p1", "somewhere/release.md")
    run4, run5 = FakeGate("gate4"), FakeGate("gate5")
    result = _finalize(world, run4=run4, run5=run5)

    assert result.status == "finished" and run4.calls == [] and run5.calls == []
    assert result.report_path == pathlib.Path("somewhere/release.md")
    assert result.gate4.passed and result.gate5.passed
    assert bounds.get_state(world.conn, "p1")["status"] == "finished"
    assert len(_events(world.conn, "project_finished")) == 1


def test_finalize_uses_the_injected_is_finished(world, written_reports):
    run4, run5 = FakeGate("gate4"), FakeGate("gate5")
    result = _finalize(world, run4=run4, run5=run5, is_finished=lambda board, plan, head, *, conn: True)

    assert result.status == "finished" and run4.calls == [] and run5.calls == []
    assert "already green" in result.reason


def test_finalize_a_finish_that_bounds_refuses_is_an_error_with_both_gates_and_the_report_kept(
    world, written_reports, monkeypatch,
):
    monkeypatch.setattr(bounds, "finish_project", lambda *args, **kwargs: False)
    result = _finalize(world)

    assert result.status == "error" and "refused to finish the project" in result.reason
    assert result.gate4.passed and result.gate5.passed and result.report_path.is_file()
    assert _events(world.conn, "project_finished") == []


def test_finalize_a_project_finished_by_another_process_meanwhile_is_finished(world, written_reports, monkeypatch):
    def another_process_finishes(*args, **kwargs):
        bounds.set_status(world.conn, "p1", "finished")
        return False

    monkeypatch.setattr(bounds, "finish_project", another_process_finishes)
    result = _finalize(world)

    assert result.status == "finished" and "another process" in result.reason and result.report_path.is_file()
    assert _events(world.conn, "project_finished") == []


def test_finalize_a_stop_that_lands_at_the_very_end_is_not_ready_and_keeps_the_report(
    world, written_reports, monkeypatch,
):
    def stop_lands(*args, **kwargs):
        bounds.set_status(world.conn, "p1", "stopped", "kill switch")
        return False

    monkeypatch.setattr(bounds, "finish_project", stop_lands)
    result = _finalize(world)

    assert result.status == "not_ready" and "stopped or paused before it could be marked finished" in result.reason
    assert result.report_path.is_file() and bounds.release_report_written(world.conn, "p1") is True


def test_finalize_skips_a_gate_already_green_for_the_exact_head(world, written_reports):
    bounds.record_final_gate(world.conn, "p1", "gate4", world.head, "pass", detail="from an earlier run")
    run4, run5 = FakeGate("gate4"), FakeGate("gate5")
    result = _finalize(world, run4=run4, run5=run5)

    assert run4.calls == [] and len(run5.calls) == 1 and result.status == "finished"
    assert result.gate4.passed and result.gate4.detail == "from an earlier run"
    assert any("already recorded" in note for note in result.gate4.notes)
    assert [e["gate"] for e in _events(world.conn, "final_gate_started")] == ["gate5"]
    assert len(_final_rows(world.conn, "gate4")) == 1


def test_finalize_runs_only_the_missing_gate_after_an_earlier_gate5_failure_was_fixed(world, written_reports):
    bounds.record_final_gate(world.conn, "p1", "gate4", world.head, "pass")
    bounds.record_final_gate(world.conn, "p1", "gate5", world.head, "fail")
    run4, run5 = FakeGate("gate4"), FakeGate("gate5")
    result = _finalize(world, run4=run4, run5=run5)

    assert run4.calls == [] and len(run5.calls) == 1 and result.status == "finished"


def test_finalize_a_red_rerun_cancels_an_earlier_green(world, written_reports):
    bounds.record_final_gate(world.conn, "p1", "gate4", world.head, "pass")
    bounds.record_final_gate(world.conn, "p1", "gate4", world.head, "fail")
    run4 = FakeGate("gate4")
    _finalize(world, run4=run4)

    assert len(run4.calls) == 1


def test_finalize_reruns_both_gates_when_the_integration_branch_moved(world, written_reports):
    old_head = world.head
    bounds.record_final_gate(world.conn, "p1", "gate4", old_head, "pass")
    bounds.record_final_gate(world.conn, "p1", "gate5", old_head, "pass")
    _write_files(world.repo, {"later.txt": "a later commit\n"})
    new_head = _commit_all(world.repo, "later")
    run4, run5 = FakeGate("gate4"), FakeGate("gate5")
    result = _finalize(world, run4=run4, run5=run5)

    assert result.status == "finished"
    assert [c["head"] for c in run4.calls] == [new_head] and [c["head"] for c in run5.calls] == [new_head]
    assert bounds.final_gates_green(world.conn, new_head) is True


def test_finalize_reads_the_head_of_the_integration_branch_not_the_checked_out_one(world, written_reports):
    _git(world.repo, "checkout", "-q", "-b", "side")
    _write_files(world.repo, {"side.txt": "side\n"})
    _commit_all(world.repo, "side commit")
    run4 = FakeGate("gate4")
    _finalize(world, run4=run4)

    assert run4.calls[0]["head"] == world.head


def test_finalize_a_gate_that_cannot_run_is_an_error_and_not_a_red_gate(world, written_reports):
    run4 = FakeGate("gate4", raises=RuntimeError("docker is down"))
    run5 = FakeGate("gate5")
    result = _finalize(world, run4=run4, run5=run5)

    assert result.status == "error" and "Gate 4 (security) could not run" in result.reason
    assert "docker is down" in result.reason and run5.calls == []
    assert _final_rows(world.conn) == []                                     # no gate row: it is not a red gate
    assert [e["gate"] for e in _events(world.conn, "final_gate_error")] == ["gate4"]
    assert _events(world.conn, "final_gate_result") == []
    rows = _intent_rows(world.conn)
    assert len(rows) == 1 and rows[0]["completed_at"] and "aborted" in rows[0]["detail"]
    assert bounds.get_state(world.conn, "p1") is None or bounds.get_state(world.conn, "p1")["status"] != "finished"


def test_finalize_a_gate5_that_cannot_run_keeps_the_green_gate4(world, written_reports):
    result = _finalize(world, run5=FakeGate("gate5", raises=RuntimeError("no git")))

    assert result.status == "error" and result.gate4.passed and result.gate5 is None
    assert "Gate 5 (smoke) could not run" in result.reason
    assert gates.last_gate_result(world.conn, "__final__", "gate4", world.head) == "pass"


def test_finalize_recovers_after_a_gate_that_could_not_run(world, written_reports):
    first = _finalize(world, run4=FakeGate("gate4", raises=RuntimeError("docker is down")))
    second = _finalize(world)

    assert first.status == "error" and second.status == "finished"


def test_finalize_a_keyboard_interrupt_leaves_the_intent_open(world, written_reports):
    with pytest.raises(KeyboardInterrupt):
        _finalize(world, run4=FakeGate("gate4", raises=KeyboardInterrupt()))

    assert [r["completed_at"] for r in _intent_rows(world.conn)] == [None]
    assert len(intents.open_intents(world.conn, "p1")) == 1


def test_finalize_returns_not_ready_when_the_branch_moved_while_the_gates_ran(world, written_reports):
    def move():
        _write_files(world.repo, {"raced.txt": "raced\n"})
        _commit_all(world.repo, "raced")

    run5 = FakeGate("gate5", on_run=move)
    result = _finalize(world, run5=run5)

    assert result.status == "not_ready" and "moved" in result.reason
    assert world.head[:10] in result.reason and result.report_path is None and written_reports == []
    assert gates.last_gate_result(world.conn, "__final__", "gate5", world.head) == "pass"    # the OLD head's evidence
    assert bounds.release_report_written(world.conn, "p1") is False
    assert bounds.get_state(world.conn, "p1") is None or bounds.get_state(world.conn, "p1")["status"] != "finished"


def test_finalize_after_a_moved_branch_runs_both_gates_on_the_new_head(world, written_reports):
    def move():
        _write_files(world.repo, {"raced.txt": "raced\n"})
        _commit_all(world.repo, "raced")

    _finalize(world, run5=FakeGate("gate5", on_run=move))
    new_head = _head(world.repo)
    run4, run5 = FakeGate("gate4"), FakeGate("gate5")
    result = _finalize(world, run4=run4, run5=run5)

    assert result.status == "finished"
    assert [c["head"] for c in run4.calls] == [new_head] and [c["head"] for c in run5.calls] == [new_head]


def test_finalize_returns_not_ready_when_the_project_is_stopped_between_the_gates(world, written_reports):
    def stop():
        bounds.set_status(world.conn, "p1", "stopped", "kill switch")

    run5 = FakeGate("gate5")
    result = _finalize(world, run4=FakeGate("gate4", on_run=stop), run5=run5)

    assert result.status == "not_ready" and "stopped" in result.reason and run5.calls == []
    assert result.gate4.passed and result.gate5 is None
    assert gates.last_gate_result(world.conn, "__final__", "gate4", world.head) == "pass"


def test_finalize_returns_not_ready_when_the_project_is_stopped_before_the_report(world, written_reports):
    def stop():
        bounds.set_status(world.conn, "p1", "paused", "bound reached")

    result = _finalize(world, run5=FakeGate("gate5", on_run=stop))

    assert result.status == "not_ready" and "paused" in result.reason
    assert written_reports == [] and bounds.release_report_written(world.conn, "p1") is False


def test_finalize_a_project_report_that_cannot_be_built_still_gets_a_release_report(world, written_reports):
    result = _finalize(world, build=Builds(raises=RuntimeError("hermes is slow")))
    text = result.report_path.read_text(encoding="ascii")

    assert result.status == "finished" and written_reports == []
    assert "the project report could not be built" in text and "hermes is slow" in text
    assert "were not written: no project report was available" in text
    assert [child.name for child in result.report_path.parent.iterdir()] == ["release.md"]


def test_finalize_a_release_report_that_cannot_be_written_is_an_error_and_can_be_retried(world, written_reports):
    def broken(summary, report, directory):
        raise OSError("disk full")

    first = _finalize(world, write=broken)

    assert first.status == "error" and "disk full" in first.reason and first.report_path is None
    assert first.gate4.passed and first.gate5.passed
    assert bounds.release_report_written(world.conn, "p1") is False
    assert len(_events(world.conn, "release_report_error")) == 1
    last = _intent_rows(world.conn)[-1]
    assert last["kind"] == "write_release_report" and last["completed_at"] and "aborted" in last["detail"]

    run4, run5 = FakeGate("gate4"), FakeGate("gate5")
    second = _finalize(world, run4=run4, run5=run5)

    assert second.status == "finished" and run4.calls == [] and run5.calls == []      # the green gates were kept


def test_finalize_a_summary_that_cannot_be_built_is_an_error_too(world, written_reports):
    def broken(*args, **kwargs):
        raise KeyError("boom")

    result = _finalize(world, summarize=broken)

    assert result.status == "error" and "release report could not be written" in result.reason


def test_finalize_hands_the_built_report_and_both_outcomes_to_the_summary(world, written_reports):
    seen = {}

    def summarize(board, plan, project, models_config, conn, head, *, gate4, gate5, now, report):
        seen.update(gate4=gate4, gate5=gate5, now=now, report=report, head=head, board=board)
        return {"project": "x"}

    builds = Builds()
    result = _finalize(world, build=builds, summarize=summarize)

    assert result.status == "finished"
    assert seen["report"] == {"fake": "report"} and seen["head"] == world.head and seen["board"] == BOARD
    assert seen["gate4"].gate == "gate4" and seen["gate5"].gate == "gate5" and seen["now"] == NOW
    assert builds.calls == [{"board": BOARD, "now": NOW}]


def test_finalize_forwards_the_runner_and_the_timeout_to_both_gates(world, written_reports):
    runner = object()
    run4, run5 = FakeGate("gate4"), FakeGate("gate5")
    _finalize(world, run4=run4, run5=run5, runner=runner, timeout_per_command=41)

    for fake in (run4, run5):
        assert fake.calls[0]["runner"] is runner and fake.calls[0]["timeout"] == 41
        assert fake.calls[0]["repo"] == world.repo


def test_finalize_report_goes_under_ases_home_reports_project_and_a_utc_stamp(world, written_reports):
    result = _finalize(world)

    assert result.report_path.parent == world.tmp / "home" / "reports" / "ases" / STAMP
    assert result.report_path.name == "release.md"


def test_finalize_a_second_report_in_the_same_second_gets_a_suffix(world, written_reports):
    (_report_dir(world)).mkdir(parents=True)
    result = _finalize(world)

    assert result.report_path.parent.name == f"{STAMP}-2"
    assert (_report_dir(world)).is_dir() and result.report_path.is_file()


def test_finalize_reads_a_naive_now_as_utc(world, written_reports):
    result = _finalize(world, now=datetime(2026, 9, 21, 12, 0, 0))

    assert result.report_path.parent.name == STAMP


def test_finalize_accepts_epoch_seconds_for_now(world, written_reports):
    result = _finalize(world, now=NOW.timestamp())

    assert result.report_path.parent.name == STAMP


def test_finalize_refuses_a_now_it_cannot_read(world, written_reports):
    with pytest.raises(ValueError, match="now must be"):
        _finalize(world, now="yesterday")


def test_finalize_makes_the_project_name_safe_for_a_directory(world, written_reports):
    world.project = _project(world.tmp, name="../../evil name/with:colon")
    result = _finalize(world)

    assert result.report_path.is_file()
    assert result.report_path.parent.parent.parent == world.tmp / "home" / "reports"
    assert result.report_path.parent.parent.name == "evil_name_with_colon"


def test_finalize_a_very_long_project_name_is_cut_for_the_folder(world, written_reports):
    world.project = _project(world.tmp, name="n" * 200)
    result = _finalize(world)

    assert result.report_path.parent.parent.name == "n" * 80


def test_finalize_a_project_name_with_nothing_safe_in_it_still_gets_a_folder(world, written_reports):
    world.project = _project(world.tmp, name="///")
    result = _finalize(world)

    assert result.report_path.parent.parent.name == "project"


def test_finalize_never_writes_the_report_inside_the_repository(world, written_reports):
    world.project = _project(world.tmp, ases_home=world.repo / "home")
    result = _finalize(world)
    outside = world.repo.resolve().parent / "ases-reports" / "ases" / STAMP

    assert result.report_path.parent.resolve() == outside.resolve()
    assert _git(world.repo, "status", "--porcelain") == ""


def test_finalize_without_an_ases_home_falls_back_to_a_folder_beside_the_repository(world, written_reports):
    world.project = types.SimpleNamespace(name="ases", budgets={})
    result = _finalize(world)
    beside = world.repo.resolve().parent / "ases-reports" / "ases" / STAMP

    assert result.report_path.parent.resolve() == beside.resolve()
    assert _git(world.repo, "status", "--porcelain") == ""


def test_finalize_takes_a_string_repo_path(world, written_reports):
    result = _finalize(world)
    again_repo = str(world.repo)
    world2 = types.SimpleNamespace(**{**vars(world), "repo": again_repo})

    assert result.status == "finished" and _finalize(world2).status == "finished"


def test_finalize_events_never_carry_a_secret_value(world, written_reports):
    hostile = finalgates.TreeFinding("injection_pattern", f"src/{SECRET}.py", 1, f"detail {SECRET}")
    run4 = FakeGate("gate4", findings=[hostile], notes=(f"note {SECRET}",))
    _finalize(world, run4=run4)
    dumped = json.dumps([dict(row) for row in world.conn.execute("SELECT kind, payload FROM events")])
    intents_dump = json.dumps([dict(row) for row in world.conn.execute("SELECT detail FROM intents")])

    assert SECRET not in dumped and SECRET_TAIL not in dumped and SECRET not in intents_dump
    result_event = _events(world.conn, "final_gate_result")[0]
    assert result_event["finding_counts"] == {"injection_pattern": 1} and result_event["advisory"] == 1
    assert result_event["blocking"] == 0


def test_finalize_the_result_reports_findings_counts_in_the_release_report(world, written_reports):
    findings = [_finding("injection_pattern", "app.py", 2, "eval or exec of a non-literal (heuristic)")]
    result = _finalize(world, run4=FakeGate("gate4", findings=findings))
    text = result.report_path.read_text(encoding="ascii")

    assert "injection_pattern app.py:2" in text


def test_finalize_with_the_real_gates_end_to_end(tmp_path, conn, fake_board, written_reports):
    repo = _make_repo(tmp_path / "repo", {"app.py": "print('hi')\n"})
    plan = _plan(profiles={"g": ["python app.py"], "gate4": ["python -c \"print('audit ok')\""]})
    _seed_tasks(conn, plan)
    world = types.SimpleNamespace(
        repo=repo, conn=conn, plan=plan, project=_project(tmp_path), head=_head(repo), tmp=tmp_path,
    )
    result = finalgates.finalize(BOARD, repo, plan, world.project, MODELS, conn, now=NOW, build=Builds())
    text = result.report_path.read_text(encoding="ascii")

    assert result.status == "finished"
    assert len(_final_rows(conn, "gate4")) == 1 and len(_final_rows(conn, "gate5")) == 1
    assert "audit ok" in _final_rows(conn, "gate4")[0]["detail"] and "hi" in _final_rows(conn, "gate5")[0]["detail"]
    assert "no gate5 profile in the plan: ran every task gate profile on the integration HEAD" in text
    assert bounds.get_state(conn, "p1")["status"] == "finished"


def test_finalize_with_nothing_injected_uses_the_real_gates_the_real_report_and_the_real_bounds(
    tmp_path, conn, fake_board,
):
    repo = _make_repo(tmp_path / "repo", {"app.py": "print('hi')\n"})
    plan = _plan(profiles={"g": ["python app.py"]})
    _seed_tasks(conn, plan)
    _merge_record(conn, "T1", "c" * 40)
    result = finalgates.finalize(BOARD, repo, plan, _project(tmp_path), MODELS, conn, now=NOW)
    folder = result.report_path.parent
    text = result.report_path.read_text(encoding="ascii")

    assert result.status == "finished" and folder == tmp_path / "home" / "reports" / "ases" / STAMP
    assert sorted(child.name for child in folder.iterdir()) == ["release.md", "report.html", "report.json"]
    assert json.loads((folder / "report.json").read_text(encoding="utf-8"))["project"]["plan_project"] == "p1"
    assert "RELEASED: Gate 4 and Gate 5 are green" in text and "c" * 10 in text
    assert bounds.is_finished(BOARD, plan, _head(repo), conn=conn) is True


def test_finalize_end_to_end_a_tracked_env_file_fails_gate4_and_asks_a_question(
    tmp_path, conn, fake_board, written_reports,
):
    repo = _make_repo(tmp_path / "repo", {"app.py": "print('hi')\n", ".env": f"API_KEY={SECRET}\n"})
    plan = _plan(profiles={"g": ["python app.py"]})
    _seed_tasks(conn, plan)
    result = finalgates.finalize(BOARD, repo, plan, _project(tmp_path), MODELS, conn, now=NOW, build=Builds())
    question = finalgates.final_gate_question(result)

    assert result.status == "gate_failed" and result.gate5 is None
    assert [(f.kind, f.path, f.line) for f in result.gate4.findings if f.kind != "injection_pattern"] == [
        ("secret_file_tracked", ".env", None), ("secret_in_tree", ".env", 1),
    ]
    assert ".env" in question and "Gate 4 (security)" in question and "swarm resume" in question
    assert SECRET not in question and SECRET_TAIL not in question
    assert SECRET not in json.dumps([dict(row) for row in conn.execute("SELECT * FROM gate_runs")])
    assert bounds.get_state(conn, "p1") is None or bounds.get_state(conn, "p1")["status"] != "finished"


def test_finalize_only_reads_cards_from_the_board(world, fake_board, written_reports):
    """Any write to Hermes (complete, block, pause) would reach hermes._run, which fails every test in this file."""
    result = _finalize(world)

    assert result.status == "finished" and set(fake_board.shown) <= {"m_T1", "m_T2", "w_T1", "w_T2"}
    assert {"m_T1", "m_T2"} <= set(fake_board.shown)


# ============================================================================================================
# The module itself
# ============================================================================================================


def _source_paths():
    return [pathlib.Path(finalgates.__file__), pathlib.Path(__file__)]


@pytest.mark.parametrize("path", _source_paths(), ids=lambda p: p.name)
def test_sources_are_ascii_and_free_of_the_banned_characters(path):
    text = path.read_bytes().decode("utf-8")

    assert text.isascii()
    assert EM_DASH not in text and SECTION_SIGN not in text


def test_finalgates_does_not_import_the_controller():
    """controller.py imports finalgates lazily; the other direction would be a cycle."""
    source = pathlib.Path(finalgates.__file__).read_text(encoding="utf-8")

    assert not re.search(r"^\s*(?:from|import)\s+[.\w ]*controller", source, re.MULTILINE)
