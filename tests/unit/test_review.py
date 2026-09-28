import dataclasses
import json
import re
import subprocess
import sys

import pytest

from ases import db, gates, hermes, review, tamper


def _git(*args, cwd):
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git("init", "-q", "-b", "integration", cwd=r)
    _git("config", "user.email", "t@t", cwd=r)
    _git("config", "user.name", "t", cwd=r)
    (r / "README.md").write_text("hi\n", encoding="utf-8")
    _git("add", "-A", cwd=r)
    _git("commit", "-q", "-m", "init", cwd=r)
    return r


def _branch_with_changes(repo, branch, files: dict):
    _git("checkout", "-q", "-b", branch, cwd=repo)
    for path, content in files.items():
        full = repo / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "work", cwd=repo)
    _git("checkout", "-q", "integration", cwd=repo)


# A value shaped like a provider key, planted where the tamper check must find it and must never repeat it.
_LEAK = "sk-or-v1-PLANTEDVALUE0123456789abcd"


@pytest.fixture(autouse=True)
def _fake_gate_record_comment(monkeypatch):
    """Round 16 (ASES-QG-01): gate_before_review now posts a gate-record comment through hermes.kanban_comment
    on every result. This file's tests stub hermes functions one at a time rather than installing the full
    ases.fakes.board.FakeHermes, so without this, an unmocked call would reach for a real `hermes` process --
    exactly the zero-quota rule this repo is built under. Autouse so none of this file's tests written before
    round 16 need a change of their own; a test that cares about the comment's own content overrides this with
    its own monkeypatch, the same way every other hermes function here is stubbed one at a time."""
    monkeypatch.setattr(hermes, "kanban_comment", lambda *a, **kw: None)


def test_in_scope_diff_passes_and_reaches_gate1(repo, tmp_path, monkeypatch):
    _branch_with_changes(repo, "swarm/T1", {"src/a.py": "x=1\n"})
    conn = db.connect(tmp_path / "ases.db")
    changes_requested = []
    monkeypatch.setattr(hermes, "kanban_reopen_review", lambda b, c, r: changes_requested.append(r))

    ok = review.gate_before_review(
        "b", "t_1", repo, "swarm/T1", "integration", ["echo gate-ok"], ["src/*"], conn=conn, task_key="T1",
    )

    assert ok is True
    assert changes_requested == []


def test_out_of_scope_diff_is_sent_back_before_gate1_runs(repo, tmp_path, monkeypatch):
    _branch_with_changes(repo, "swarm/T2", {"src/a.py": "x=1\n", "SECRETS.md": "oops\n"})
    conn = db.connect(tmp_path / "ases.db")
    changes_requested = []
    monkeypatch.setattr(hermes, "kanban_reopen_review", lambda b, c, r: changes_requested.append(r))
    gate_ran = []
    monkeypatch.setattr("ases.gates.run_gate", lambda *a, **kw: gate_ran.append(1))

    ok = review.gate_before_review(
        "b", "t_2", repo, "swarm/T2", "integration", ["echo gate-ok"], ["src/*"], conn=conn, task_key="T2",
    )

    assert ok is False
    assert len(changes_requested) == 1
    assert "SECRETS.md" in changes_requested[0]
    assert gate_ran == []  # never reached Gate 1 -- the path check comes first


def test_empty_touches_means_no_changes_allowed(repo, tmp_path, monkeypatch):
    _branch_with_changes(repo, "swarm/T3", {"anything.txt": "x\n"})
    conn = db.connect(tmp_path / "ases.db")
    changes_requested = []
    monkeypatch.setattr(hermes, "kanban_reopen_review", lambda b, c, r: changes_requested.append(r))

    ok = review.gate_before_review(
        "b", "t_3", repo, "swarm/T3", "integration", ["echo gate-ok"], [], conn=conn, task_key="T3",
    )

    assert ok is False
    assert "anything.txt" in changes_requested[0]


def test_red_gate1_after_in_scope_diff_is_still_sent_back(repo, tmp_path, monkeypatch):
    _branch_with_changes(repo, "swarm/T4", {"src/a.py": "x=1\n"})
    conn = db.connect(tmp_path / "ases.db")
    changes_requested = []
    monkeypatch.setattr(hermes, "kanban_reopen_review", lambda b, c, r: changes_requested.append(r))

    ok = review.gate_before_review(
        "b", "t_4", repo, "swarm/T4", "integration", ["exit 1"], ["src/*"], conn=conn, task_key="T4",
    )

    assert ok is False
    assert "Gate 1 failed" in changes_requested[0]


def test_send_back_uses_reopen_review_never_the_reviewers_request_changes(repo, tmp_path, monkeypatch):
    """Real bug (2026-09-19): every send-back went through `hermes kanban request-changes`, the REVIEWER's
    verdict. Real Hermes rejects it (exit 1, "task is not in an active review run") on a card that merely
    sits in `review`, which is where process_review_lane finds cards, and the wrapper raises that out of
    the whole polling loop. Every earlier test mocked the wrapper, so none could see it. The controller's
    own send-back is `reopen-review`."""
    _branch_with_changes(repo, "swarm/T5", {"SECRETS.md": "oops\n"})
    conn = db.connect(tmp_path / "ases.db")
    reopened = []
    monkeypatch.setattr(hermes, "kanban_reopen_review", lambda b, c, r: reopened.append((b, c, r)))

    def _forbidden(*args, **kwargs):
        raise AssertionError("gate_before_review must not call request-changes on a card sitting in review")

    monkeypatch.setattr(hermes, "kanban_request_changes", _forbidden)

    ok = review.gate_before_review(
        "b", "t_5", repo, "swarm/T5", "integration", ["echo gate-ok"], ["src/*"], conn=conn, task_key="T5",
    )

    assert ok is False
    assert len(reopened) == 1
    assert reopened[0][:2] == ("b", "t_5")
    assert "SECRETS.md" in reopened[0][2]


def test_unresolvable_branch_is_sent_back_with_reopen_review(repo, tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "ases.db")
    reopened = []
    monkeypatch.setattr(hermes, "kanban_reopen_review", lambda b, c, r: reopened.append(r))

    ok = review.gate_before_review(
        "b", "t_6", repo, "swarm/does-not-exist", "integration", ["echo gate-ok"], ["src/*"],
        conn=conn, task_key="T6",
    )

    assert ok is False
    assert reopened == ["could not resolve branch swarm/does-not-exist"]


def test_no_merge_base_fails_closed_instead_of_checking_only_the_last_commit(repo, tmp_path, monkeypatch):
    """A branch with unrelated history has no merge-base with integration. This used to fall back to
    inspecting only the branch's last commit, so an out-of-scope path in an earlier commit went unseen;
    now the card is sent back and Gate 1 is never reached."""
    _git("checkout", "-q", "--orphan", "swarm/T7", cwd=repo)
    _git("rm", "-rf", "-q", ".", cwd=repo)
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("x=1\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "unrelated history", cwd=repo)
    _git("checkout", "-q", "integration", cwd=repo)
    conn = db.connect(tmp_path / "ases.db")
    reopened = []
    monkeypatch.setattr(hermes, "kanban_reopen_review", lambda b, c, r: reopened.append(r))
    gate_ran = []
    monkeypatch.setattr("ases.gates.run_gate", lambda *a, **kw: gate_ran.append(1))

    ok = review.gate_before_review(
        "b", "t_7", repo, "swarm/T7", "integration", ["echo gate-ok"], ["src/*"], conn=conn, task_key="T7",
    )

    assert ok is False
    assert len(reopened) == 1
    assert "merge-base" in reopened[0]
    assert gate_ran == []


def test_touches_check_diffs_against_the_configured_integration_branch_not_a_literal(tmp_path, monkeypatch):
    """Real bug (2026-09-19): the merge-base was computed against a hardcoded "integration", so a project
    whose integration branch is spelled any other way (here "main-line") got an empty merge-base and the
    touches check silently fell back to inspecting only the branch's LAST commit. The stray path here is
    in the FIRST commit and the last commit is entirely in scope, so only a diff taken from the real
    merge-base can see it."""
    work_repo = tmp_path / "repo"
    work_repo.mkdir()
    _git("init", "-q", "-b", "main-line", cwd=work_repo)  # no branch called "integration" exists at all
    _git("config", "user.email", "t@t", cwd=work_repo)
    _git("config", "user.name", "t", cwd=work_repo)
    (work_repo / "README.md").write_text("hi\n", encoding="utf-8")
    _git("add", "-A", cwd=work_repo)
    _git("commit", "-q", "-m", "init", cwd=work_repo)
    _git("checkout", "-q", "-b", "swarm/T9", cwd=work_repo)
    (work_repo / "SECRETS.md").write_text("oops\n", encoding="utf-8")  # first commit: out of scope
    _git("add", "-A", cwd=work_repo)
    _git("commit", "-q", "-m", "stray file", cwd=work_repo)
    (work_repo / "src").mkdir()
    (work_repo / "src" / "a.py").write_text("x=1\n", encoding="utf-8")  # last commit: in scope
    _git("add", "-A", cwd=work_repo)
    _git("commit", "-q", "-m", "real work", cwd=work_repo)
    _git("checkout", "-q", "main-line", cwd=work_repo)
    conn = db.connect(tmp_path / "ases.db")
    changes_requested = []
    monkeypatch.setattr(hermes, "kanban_reopen_review", lambda b, c, reason: changes_requested.append(reason))

    ok = review.gate_before_review(
        "b", "t_9", work_repo, "swarm/T9", "main-line", ["echo gate-ok"], ["src/*"], conn=conn, task_key="T9",
    )

    assert ok is False
    assert len(changes_requested) == 1
    assert "SECRETS.md" in changes_requested[0]
    assert "src/a.py" not in changes_requested[0]  # only the stray path is flagged


# --- helpers for the check_branch / check_branch_for_merge / verdict tests ---------------------------------


def _head(repo, ref):
    return _git("rev-parse", ref, cwd=repo).stdout.strip()


def _add_commit(repo, branch, files: dict):
    """A further commit on an existing branch; leaves the repo back on integration."""
    _git("checkout", "-q", branch, cwd=repo)
    for path, content in files.items():
        full = repo / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "more work", cwd=repo)
    _git("checkout", "-q", "integration", cwd=repo)


def _orphan_branch(repo, branch):
    """A branch with unrelated history: it has no merge-base with integration."""
    _git("checkout", "-q", "--orphan", branch, cwd=repo)
    _git("rm", "-rf", "-q", ".", cwd=repo)
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("x=1\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "unrelated history", cwd=repo)
    _git("checkout", "-q", "integration", cwd=repo)


def _record_gate(conn, task_key, commit_sha, result, gate="gate1"):
    """Seed a gate_runs row the way gates.run_gate writes one."""
    conn.execute(
        "INSERT INTO gate_runs (task_key, gate, commit_sha, result, detail, ran_at) VALUES (?, ?, ?, ?, ?, ?)",
        (task_key, gate, commit_sha, result, "seeded by the test", "2026-09-19T00:00:00+00:00"),
    )


def _gate_rows(conn, task_key):
    return [
        tuple(r) for r in conn.execute(
            "SELECT gate, commit_sha, result FROM gate_runs WHERE task_key = ? ORDER BY id", (task_key,)
        )
    ]


def _forbid_gate1(monkeypatch):
    """Any attempt to run a gate fails the test loudly. Returns the attempted calls too, so a caller that
    swallowed the error would still be caught by asserting the list is empty."""
    attempts = []

    def _boom(*args, **kwargs):
        attempts.append(args)
        raise AssertionError("Gate 1 must not be run here")

    monkeypatch.setattr(gates, "run_gate", _boom)
    return attempts


def _fake_gate1(monkeypatch, passed=True):
    """Replace the gate runner with one that only counts its calls (no worktree, no gate_runs row)."""
    calls = []

    def _fake(repo, commit_sha, gate_name, commands, **kwargs):
        calls.append((commit_sha, gate_name))
        return gates.GateResult(gate_name, commit_sha, passed, "fake gate output")

    monkeypatch.setattr(gates, "run_gate", _fake)
    return calls


def _forbid_hermes(monkeypatch):
    """Any call into the Hermes wrapper fails the test."""
    def _boom(*args, **kwargs):
        raise AssertionError("no Hermes call is allowed here")

    for name in dir(hermes):
        if name.startswith("kanban_"):
            monkeypatch.setattr(hermes, name, _boom)
    monkeypatch.setattr(hermes, "_run", _boom, raising=False)


def _forbid_review_sendback(monkeypatch):
    """Only kanban_reopen_review is forbidden, i.e. "the card is not sent back". Round 16:
    gate_before_review now also posts a gate-record comment through kanban_comment even when the card stays in
    review (a "not run" record, see test_review.py's dedicated gate-record tests) -- the autouse
    _fake_gate_record_comment fixture already stubs THAT call to a no-op, so a test using this helper is
    checking one thing, not asserting "no Hermes call at all" against a call this round intentionally adds."""
    def _boom(*args, **kwargs):
        raise AssertionError("the card must not be sent back")

    monkeypatch.setattr(hermes, "kanban_reopen_review", _boom)


def _scenario(repo, kind):
    """A branch that produces the given BranchCheck kind: (branch, touches, gate 1 commands)."""
    if kind == "unresolvable_branch":
        return "swarm/no-such-branch", ["src/*"], ["echo gate-ok"]
    if kind == "no_merge_base":
        _orphan_branch(repo, "swarm/orphan")
        return "swarm/orphan", ["src/*"], ["echo gate-ok"]
    if kind == "out_of_scope":
        _branch_with_changes(repo, "swarm/stray", {"src/a.py": "x=1\n", "SECRETS.md": "oops\n"})
        return "swarm/stray", ["src/*"], ["echo gate-ok"]
    if kind == "gate1_red":
        _branch_with_changes(repo, "swarm/red", {"src/a.py": "x=1\n"})
        return "swarm/red", ["src/*"], ["exit 1"]
    if kind == "tamper":
        _branch_with_changes(repo, "swarm/tamper", {"src/a.py": "x=1\n", "src/config.py": f"API_KEY = '{_LEAK}'\n"})
        return "swarm/tamper", ["src/*"], ["echo gate-ok"]
    assert kind == "ok"
    _branch_with_changes(repo, "swarm/fine", {"src/a.py": "x=1\n"})
    return "swarm/fine", ["src/*"], ["echo gate-ok"]


# --- check_branch: the decision behind gate_before_review, with no Hermes call -----------------------------


def test_check_branch_ok_returns_the_head_and_records_gate1(repo, tmp_path):
    _branch_with_changes(repo, "swarm/C1", {"src/a.py": "x=1\n"})
    conn = db.connect(tmp_path / "ases.db")
    head = _head(repo, "swarm/C1")

    result = review.check_branch(
        repo, "swarm/C1", "integration", ["echo gate-ok"], ["src/*"], conn=conn, task_key="C1",
    )

    assert (result.ok, result.kind, result.head) == (True, "ok", head)
    assert _gate_rows(conn, "C1") == [("gate1", head, "pass")]


def test_check_branch_unresolvable_branch(repo, tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "ases.db")
    attempts = _forbid_gate1(monkeypatch)

    result = review.check_branch(
        repo, "swarm/does-not-exist", "integration", ["echo gate-ok"], ["src/*"], conn=conn, task_key="C2",
    )

    assert result == review.BranchCheck(
        ok=False, kind="unresolvable_branch", detail="could not resolve branch swarm/does-not-exist", head="",
    )
    assert attempts == []


def test_check_branch_no_merge_base_fails_closed(repo, tmp_path, monkeypatch):
    _orphan_branch(repo, "swarm/C3")
    conn = db.connect(tmp_path / "ases.db")
    attempts = _forbid_gate1(monkeypatch)

    result = review.check_branch(
        repo, "swarm/C3", "integration", ["echo gate-ok"], ["src/*"], conn=conn, task_key="C3",
    )

    assert result == review.BranchCheck(
        ok=False, kind="no_merge_base",
        detail="could not compute a merge-base between 'integration' and 'swarm/C3', so the touches check "
               "cannot be trusted",
        head=_head(repo, "swarm/C3"),
    )
    assert attempts == []


def test_check_branch_out_of_scope_names_the_offending_paths(repo, tmp_path, monkeypatch):
    _branch_with_changes(repo, "swarm/C4", {"src/a.py": "x=1\n", "SECRETS.md": "oops\n"})
    conn = db.connect(tmp_path / "ases.db")
    attempts = _forbid_gate1(monkeypatch)

    result = review.check_branch(
        repo, "swarm/C4", "integration", ["echo gate-ok"], ["src/*"], conn=conn, task_key="C4",
    )

    assert result == review.BranchCheck(
        ok=False, kind="out_of_scope",
        detail="diff touches paths outside the card's declared touches (['src/*']): ['SECRETS.md']. "
               "Either the task needs widening or these changes need to come out.",
        head=_head(repo, "swarm/C4"),
    )
    assert attempts == []


def test_check_branch_red_gate1_carries_the_evidence_and_records_the_failure(repo, tmp_path):
    _branch_with_changes(repo, "swarm/C5", {"src/a.py": "x=1\n"})
    conn = db.connect(tmp_path / "ases.db")
    head = _head(repo, "swarm/C5")

    result = review.check_branch(
        repo, "swarm/C5", "integration", ["exit 1"], ["src/*"], conn=conn, task_key="C5",
    )

    assert (result.ok, result.kind, result.head) == (False, "gate1_red", head)
    assert result.detail.startswith("Gate 1 failed on the controller's re-check:\n")
    assert "[exit 1]" in result.detail
    assert _gate_rows(conn, "C5") == [("gate1", head, "fail")]


def test_check_branch_trims_the_gate1_evidence_to_1500_characters(repo, tmp_path):
    _branch_with_changes(repo, "swarm/C6", {"tools/noisy.py": "print('x' * 5000)\nraise SystemExit(1)\n"})
    conn = db.connect(tmp_path / "ases.db")

    # The gate command names tools/noisy.py itself (gate_config_paths), and the branch adds that exact file, so
    # this needs the ASES-QG-02 marker to get past the tamper check to Gate 1 at all -- not what this test is
    # about (it is about the length of Gate 1's OWN evidence), so it is granted here.
    result = review.check_branch(
        repo, "swarm/C6", "integration", [f'"{sys.executable}" tools/noisy.py'], ["tools/*"],
        conn=conn, task_key="C6", allow_gate_config_changes=True,
    )

    prefix = "Gate 1 failed on the controller's re-check:\n"
    assert result.kind == "gate1_red"
    assert result.detail.startswith(prefix)
    assert len(result.detail) == len(prefix) + 1500
    # Both ends survive (2026-09-19): the command line at the head AND the failure marker at the tail, which the
    # old "keep the first 1500 characters" cut threw away on a long red run.
    assert "...[trimmed]..." in result.detail
    assert result.detail.rstrip().endswith("[exit 1]")


@pytest.mark.parametrize("kind", ["ok", "unresolvable_branch", "no_merge_base", "out_of_scope", "tamper", "gate1_red"])
def test_check_branch_never_calls_hermes(kind, repo, tmp_path, monkeypatch):
    """check_branch is the decision only: the send-back to Hermes belongs to gate_before_review."""
    branch, touches, commands = _scenario(repo, kind)
    conn = db.connect(tmp_path / "ases.db")
    _forbid_hermes(monkeypatch)

    result = review.check_branch(repo, branch, "integration", commands, touches, conn=conn, task_key="H1")

    assert result.kind == kind
    assert result.ok is (kind == "ok")


@pytest.mark.parametrize("kind", ["unresolvable_branch", "no_merge_base", "out_of_scope", "tamper", "gate1_red"])
def test_gate_before_review_sends_back_exactly_the_check_detail(kind, repo, tmp_path, monkeypatch):
    """gate_before_review is a wrapper: what it sends to reopen-review IS the check's detail, for every way
    a branch can fail (the texts themselves are pinned by the check_branch tests above)."""
    branch, touches, commands = _scenario(repo, kind)
    conn = db.connect(tmp_path / "ases.db")
    reopened = []
    monkeypatch.setattr(hermes, "kanban_reopen_review", lambda b, c, r: reopened.append((b, c, r)))

    check = review.check_branch(repo, branch, "integration", commands, touches, conn=conn, task_key="G1")
    ok = review.gate_before_review(
        "b", "t_g", repo, branch, "integration", commands, touches, conn=conn, task_key="G1",
    )

    assert check.kind == kind
    assert ok is False
    assert reopened == [("b", "t_g", check.detail)]


# --- check_branch_for_merge: the merge queue's own check, independent of the review lane -------------------


def test_merge_check_reuses_a_green_gate1_record_for_the_head(repo, tmp_path, monkeypatch):
    _branch_with_changes(repo, "swarm/M1", {"src/a.py": "x=1\n"})
    conn = db.connect(tmp_path / "ases.db")
    head = _head(repo, "swarm/M1")
    _record_gate(conn, "M1", head, "pass")
    attempts = _forbid_gate1(monkeypatch)

    # The command is "exit 1" on purpose: were Gate 1 run again it would come back red, so an ok can only
    # come from believing the controller's own green record.
    result = review.check_branch_for_merge(
        repo, "swarm/M1", "integration", ["exit 1"], ["src/*"], conn=conn, task_key="M1",
    )

    assert (result.ok, result.kind, result.head) == (True, "ok", head)
    assert attempts == []
    assert _gate_rows(conn, "M1") == [("gate1", head, "pass")]  # and nothing new was written


def test_merge_check_reuses_the_record_the_review_lane_check_left_behind(repo, tmp_path, monkeypatch):
    """The two halves agree: the gate_runs row check_branch writes is exactly what the merge queue trusts."""
    _branch_with_changes(repo, "swarm/M2", {"src/a.py": "x=1\n"})
    conn = db.connect(tmp_path / "ases.db")
    assert review.check_branch(
        repo, "swarm/M2", "integration", ["echo gate-ok"], ["src/*"], conn=conn, task_key="M2",
    ).ok
    attempts = _forbid_gate1(monkeypatch)

    result = review.check_branch_for_merge(
        repo, "swarm/M2", "integration", ["echo gate-ok"], ["src/*"], conn=conn, task_key="M2",
    )

    assert (result.ok, result.kind) == (True, "ok")
    assert attempts == []


def test_merge_check_prefers_the_record_for_the_head_over_an_earlier_one(repo, tmp_path, monkeypatch):
    """A card that went review, changes, new commit, review again has green rows for both heads. The current
    head's row decides, so it is ok and not a stale_review."""
    _branch_with_changes(repo, "swarm/M3", {"src/a.py": "x=1\n"})
    earlier = _head(repo, "swarm/M3")
    _add_commit(repo, "swarm/M3", {"src/b.py": "y=2\n"})
    head = _head(repo, "swarm/M3")
    conn = db.connect(tmp_path / "ases.db")
    _record_gate(conn, "M3", earlier, "pass")
    _record_gate(conn, "M3", head, "pass")
    attempts = _forbid_gate1(monkeypatch)

    result = review.check_branch_for_merge(
        repo, "swarm/M3", "integration", ["echo gate-ok"], ["src/*"], conn=conn, task_key="M3",
    )

    assert (result.ok, result.kind, result.head) == (True, "ok", head)
    assert attempts == []


def test_merge_check_refuses_a_head_that_moved_after_gate1_was_green(repo, tmp_path, monkeypatch):
    """ASES-GIT-03: any later commit voids the review and the gate record. Gate 1 is not run to paper over it."""
    _branch_with_changes(repo, "swarm/M4", {"src/a.py": "x=1\n"})
    reviewed = _head(repo, "swarm/M4")
    conn = db.connect(tmp_path / "ases.db")
    _record_gate(conn, "M4", reviewed, "pass")
    _add_commit(repo, "swarm/M4", {"src/b.py": "y=2\n"})  # pushed after the review
    moved = _head(repo, "swarm/M4")
    attempts = _forbid_gate1(monkeypatch)

    result = review.check_branch_for_merge(
        repo, "swarm/M4", "integration", ["echo gate-ok"], ["src/*"], conn=conn, task_key="M4",
    )

    assert (result.ok, result.kind, result.head) == (False, "stale_review", moved)
    assert f"green for {reviewed}" in result.detail
    assert f"head is {moved}" in result.detail
    assert attempts == []
    assert _gate_rows(conn, "M4") == [("gate1", reviewed, "pass")]


def test_merge_check_names_the_most_recent_earlier_green_commit(repo, tmp_path, monkeypatch):
    _branch_with_changes(repo, "swarm/M5", {"src/a.py": "x=1\n"})
    first = _head(repo, "swarm/M5")
    _add_commit(repo, "swarm/M5", {"src/b.py": "y=2\n"})
    second = _head(repo, "swarm/M5")
    _add_commit(repo, "swarm/M5", {"src/c.py": "z=3\n"})
    conn = db.connect(tmp_path / "ases.db")
    _record_gate(conn, "M5", first, "pass")
    _record_gate(conn, "M5", second, "pass")
    _forbid_gate1(monkeypatch)

    result = review.check_branch_for_merge(
        repo, "swarm/M5", "integration", ["echo gate-ok"], ["src/*"], conn=conn, task_key="M5",
    )

    assert result.kind == "stale_review"
    assert f"green for {second}" in result.detail
    assert first not in result.detail


@pytest.mark.parametrize("seed_at", ["head", "earlier"])
@pytest.mark.parametrize(
    "seed_task, seed_gate",
    [pytest.param("M6-other", "gate1", id="other-task"), pytest.param("M6", "gate3", id="other-gate")],
)
def test_merge_check_only_trusts_this_tasks_gate1_records(seed_task, seed_gate, seed_at, repo, tmp_path,
                                                          monkeypatch):
    """A green row for another task, or for another gate, is neither a reusable record for the head nor
    evidence that the head moved: Gate 1 has to run."""
    _branch_with_changes(repo, "swarm/M6", {"src/a.py": "x=1\n"})
    earlier = _head(repo, "swarm/M6")
    _add_commit(repo, "swarm/M6", {"src/b.py": "y=2\n"})
    head = _head(repo, "swarm/M6")
    conn = db.connect(tmp_path / "ases.db")
    _record_gate(conn, seed_task, head if seed_at == "head" else earlier, "pass", gate=seed_gate)
    calls = _fake_gate1(monkeypatch, passed=True)

    result = review.check_branch_for_merge(
        repo, "swarm/M6", "integration", ["echo gate-ok"], ["src/*"], conn=conn, task_key="M6",
    )

    assert (result.ok, result.kind, result.head) == (True, "ok", head)
    assert calls == [(head, "gate1")]


def test_merge_check_does_not_read_a_red_record_for_an_earlier_commit_as_green(repo, tmp_path, monkeypatch):
    """Only 'pass' rows say the branch moved after a green Gate 1. A red row for an older commit says
    nothing of the kind, so with no green record at all Gate 1 runs."""
    _branch_with_changes(repo, "swarm/M7", {"src/a.py": "x=1\n"})
    earlier = _head(repo, "swarm/M7")
    _add_commit(repo, "swarm/M7", {"src/b.py": "y=2\n"})
    head = _head(repo, "swarm/M7")
    conn = db.connect(tmp_path / "ases.db")
    _record_gate(conn, "M7", earlier, "fail")
    calls = _fake_gate1(monkeypatch, passed=True)

    result = review.check_branch_for_merge(
        repo, "swarm/M7", "integration", ["echo gate-ok"], ["src/*"], conn=conn, task_key="M7",
    )

    assert (result.ok, result.kind, result.head) == (True, "ok", head)
    assert calls == [(head, "gate1")]


def test_a_red_gate1_record_for_the_head_is_never_treated_as_green(repo, tmp_path, monkeypatch):
    _branch_with_changes(repo, "swarm/M8", {"src/a.py": "x=1\n"})
    conn = db.connect(tmp_path / "ases.db")
    head = _head(repo, "swarm/M8")
    _record_gate(conn, "M8", head, "fail")
    calls = _fake_gate1(monkeypatch, passed=False)

    result = review.check_branch_for_merge(
        repo, "swarm/M8", "integration", ["exit 1"], ["src/*"], conn=conn, task_key="M8",
    )

    assert (result.ok, result.kind, result.head) == (False, "gate1_red", head)
    assert calls == [(head, "gate1")]  # it was run again, not waved through on the strength of a red row


def test_merge_check_runs_and_records_gate1_when_the_controller_holds_no_record(repo, tmp_path):
    """The review-lane re-check never ran (Hermes's dispatcher claimed the card first), so no gate_runs row
    exists: the merge queue runs Gate 1 itself instead of trusting that it once did."""
    _branch_with_changes(repo, "swarm/M9", {"src/a.py": "x=1\n"})
    conn = db.connect(tmp_path / "ases.db")
    head = _head(repo, "swarm/M9")
    assert _gate_rows(conn, "M9") == []

    result = review.check_branch_for_merge(
        repo, "swarm/M9", "integration", ["echo gate-ok"], ["src/*"], conn=conn, task_key="M9",
    )

    assert (result.ok, result.kind, result.head) == (True, "ok", head)
    assert _gate_rows(conn, "M9") == [("gate1", head, "pass")]


def test_merge_check_reports_a_red_gate1_when_it_has_to_run_it(repo, tmp_path):
    _branch_with_changes(repo, "swarm/M10", {"src/a.py": "x=1\n"})
    conn = db.connect(tmp_path / "ases.db")
    head = _head(repo, "swarm/M10")

    result = review.check_branch_for_merge(
        repo, "swarm/M10", "integration", ["exit 1"], ["src/*"], conn=conn, task_key="M10",
    )

    assert (result.ok, result.kind, result.head) == (False, "gate1_red", head)
    assert result.detail.startswith("Gate 1 failed on the controller's re-check:\n")
    assert _gate_rows(conn, "M10") == [("gate1", head, "fail")]


def test_merge_check_refuses_an_out_of_scope_diff_even_with_a_green_record(repo, tmp_path, monkeypatch):
    """The whole point of the merge-time check: the review lane never saw this card, so the touches check
    it would have done has to happen here. A green Gate 1 record does not excuse a stray path."""
    _branch_with_changes(repo, "swarm/M11", {"src/a.py": "x=1\n", "SECRETS.md": "oops\n"})
    conn = db.connect(tmp_path / "ases.db")
    head = _head(repo, "swarm/M11")
    _record_gate(conn, "M11", head, "pass")
    attempts = _forbid_gate1(monkeypatch)

    result = review.check_branch_for_merge(
        repo, "swarm/M11", "integration", ["echo gate-ok"], ["src/*"], conn=conn, task_key="M11",
    )

    assert (result.ok, result.kind, result.head) == (False, "out_of_scope", head)
    assert "SECRETS.md" in result.detail
    assert attempts == []


def test_merge_check_unresolvable_branch(repo, tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "ases.db")
    attempts = _forbid_gate1(monkeypatch)

    result = review.check_branch_for_merge(
        repo, "swarm/does-not-exist", "integration", ["echo gate-ok"], ["src/*"], conn=conn, task_key="M12",
    )

    assert result == review.BranchCheck(
        ok=False, kind="unresolvable_branch", detail="could not resolve branch swarm/does-not-exist", head="",
    )
    assert attempts == []


def test_merge_check_no_merge_base_fails_closed_even_with_a_green_record(repo, tmp_path, monkeypatch):
    _orphan_branch(repo, "swarm/M13")
    conn = db.connect(tmp_path / "ases.db")
    head = _head(repo, "swarm/M13")
    _record_gate(conn, "M13", head, "pass")
    attempts = _forbid_gate1(monkeypatch)

    result = review.check_branch_for_merge(
        repo, "swarm/M13", "integration", ["echo gate-ok"], ["src/*"], conn=conn, task_key="M13",
    )

    assert (result.ok, result.kind, result.head) == (False, "no_merge_base", head)
    assert attempts == []


def test_branch_check_and_verdict_are_frozen():
    check = review.BranchCheck(ok=True, kind="ok", detail="", head="abc")
    verdict = review.Verdict(valid=True, outcome="PASS", problems=(), commit=None, tamper_suspected=False)
    with pytest.raises(dataclasses.FrozenInstanceError):
        check.ok = False
    with pytest.raises(dataclasses.FrozenInstanceError):
        verdict.valid = False


# --- validate_verdict (ASES-REV-06): the blueprint's schema and the Hermes review skill's ------------------

_HEAD = "0123456789abcdef0123456789abcdef01234567"
_LIST_KEYS = [
    "architecture_issues", "missing_cases", "security_issues", "test_gaps", "required_changes",
    "reviewer_checks",
]


def _blueprint(**overrides):
    """A complete review object in the blueprint's own schema (section 13.3), a PASS."""
    meta = {
        "review_status": "PASS", "commit": "0123456", "summary": "reads correctly",
        "architecture_issues": [], "missing_cases": [], "security_issues": [], "test_gaps": [],
        "gate_tampering_suspected": False, "required_changes": [],
    }
    meta.update(overrides)
    return meta


def _skill(**overrides):
    """The metadata Hermes's own review skill has a reviewer send with kanban_complete."""
    meta = {"review_outcome": "approved", "reviewer_checks": ["read the diff"]}
    meta.update(overrides)
    return meta


def _single_problem(verdict):
    assert len(verdict.problems) == 1, verdict.problems
    return verdict.problems[0]


def test_validate_verdict_accepts_the_blueprint_shape():
    assert review.validate_verdict(_blueprint()) == review.Verdict(
        valid=True, outcome="PASS", problems=(), commit="0123456", tamper_suspected=False,
    )


def test_validate_verdict_accepts_the_hermes_review_skill_shape():
    assert review.validate_verdict(_skill()) == review.Verdict(
        valid=True, outcome="PASS", problems=(), commit=None, tamper_suspected=False,
    )


@pytest.mark.parametrize("meta", [_blueprint(), _skill()], ids=["blueprint", "hermes-skill"])
def test_validate_verdict_reads_a_json_string_like_the_dict(meta):
    from_text = review.validate_verdict(json.dumps(meta))
    assert from_text.valid is True
    assert from_text == review.validate_verdict(meta)


@pytest.mark.parametrize("status", ["CHANGES_REQUIRED", "BLOCKED"])
def test_a_changes_required_or_blocked_verdict_is_still_a_valid_verdict(status):
    verdict = review.validate_verdict(_blueprint(review_status=status))
    assert (verdict.valid, verdict.outcome, verdict.problems) == (True, status, ())


@pytest.mark.parametrize("metadata", [None, "", "   ", "null"], ids=["none", "empty", "blank", "json-null"])
def test_validate_verdict_missing_metadata(metadata):
    verdict = review.validate_verdict(metadata)
    assert (verdict.valid, verdict.outcome, verdict.commit, verdict.tamper_suspected) == (False, None, None, False)
    assert "missing" in _single_problem(verdict)


@pytest.mark.parametrize(
    "metadata",
    [
        pytest.param([], id="empty-list"),
        pytest.param(["review_status", "PASS"], id="list"),
        pytest.param(7, id="int"),
        pytest.param(1.5, id="float"),
        pytest.param(True, id="bool"),
        pytest.param('["PASS"]', id="json-list"),
        pytest.param("42", id="json-number"),
        pytest.param('"PASS"', id="json-string"),
        pytest.param("not json at all", id="plain-text"),
        pytest.param("{'review_outcome': 'approved'}", id="python-repr-not-json"),
        pytest.param("{", id="truncated-json"),
        pytest.param("[" * 100_000, id="deeply-nested-json"),
    ],
)
def test_validate_verdict_rejects_metadata_that_is_not_a_json_object(metadata):
    """The deeply nested string is the one that matters: json.loads raises RecursionError for it, which is
    not a ValueError, and the text comes from an agent."""
    verdict = review.validate_verdict(metadata)
    assert (verdict.valid, verdict.outcome) == (False, None)
    assert "not a JSON object" in _single_problem(verdict)


def test_validate_verdict_needs_an_outcome_key():
    for metadata in ({"summary": "no verdict here"}, {}):
        verdict = review.validate_verdict(metadata)
        assert (verdict.valid, verdict.outcome) == (False, None)
        problem = _single_problem(verdict)
        assert "review_status" in problem and "review_outcome" in problem


@pytest.mark.parametrize(
    "value",
    ["pass", "Pass", "APPROVED", "approved", "CHANGES_REQUESTED", "", " PASS", None, True, 1, ["PASS"]],
)
def test_validate_verdict_rejects_an_unknown_review_status(value):
    verdict = review.validate_verdict(_blueprint(review_status=value))
    assert (verdict.valid, verdict.outcome) == (False, None)
    assert _single_problem(verdict).startswith("review_status is ")


@pytest.mark.parametrize(
    "value",
    ["Approved", "APPROVED", "approve", "changes_requested", "PASS", "", " approved", None, True, 1, ["approved"]],
)
def test_validate_verdict_rejects_a_review_outcome_that_is_not_exactly_approved(value):
    verdict = review.validate_verdict(_skill(review_outcome=value))
    assert (verdict.valid, verdict.outcome) == (False, None)
    problem = _single_problem(verdict)
    assert problem.startswith("review_outcome is ")
    if isinstance(value, str) and value:
        assert repr(value) in problem  # the offending value is reported


def test_validate_verdict_accepts_both_outcome_keys_when_they_agree():
    verdict = review.validate_verdict({**_blueprint(), **_skill()})
    assert (verdict.valid, verdict.outcome, verdict.problems) == (True, "PASS", ())


@pytest.mark.parametrize(
    "status, outcome",
    [
        ("PASS", "changes_requested"), ("PASS", "rejected"), ("PASS", "Approved"), ("PASS", True),
        ("CHANGES_REQUIRED", "approved"), ("BLOCKED", "approved"),
    ],
)
def test_validate_verdict_treats_disagreeing_outcome_keys_as_a_problem(status, outcome):
    verdict = review.validate_verdict({"review_status": status, "review_outcome": outcome})
    assert verdict.valid is False
    assert verdict.outcome is None  # a contradiction is never read as an approval
    assert any("disagree" in p for p in verdict.problems)


@pytest.mark.parametrize("commit", ["abcdef1", "ABCDEF1", _HEAD, "A" * 40, "a" * 7])
def test_validate_verdict_accepts_a_7_to_40_character_hex_commit(commit):
    verdict = review.validate_verdict(_blueprint(commit=commit))
    assert (verdict.valid, verdict.commit) == (True, commit)


@pytest.mark.parametrize(
    "commit",
    ["abcdef", "a" * 41, "xyz1234", "abcdefg", "", " abcdef1", "abcdef1 ", "abcdef1\n", None, 1234567,
     ["abcdef1"], True],
)
def test_validate_verdict_rejects_a_malformed_commit(commit):
    verdict = review.validate_verdict(_blueprint(commit=commit))
    assert verdict.valid is False
    assert verdict.commit is None
    assert verdict.outcome == "PASS"  # only the commit is wrong; the outcome itself is still readable
    assert _single_problem(verdict).startswith("commit is not 7 to 40 hex characters")


def test_validate_verdict_commit_is_optional():
    meta = _blueprint()
    del meta["commit"]
    verdict = review.validate_verdict(meta)
    assert (verdict.valid, verdict.commit) == (True, None)


@pytest.mark.parametrize("key", _LIST_KEYS)
@pytest.mark.parametrize("value", ["a string", {"a": 1}, None, 3, True], ids=["str", "dict", "null", "int", "bool"])
def test_validate_verdict_rejects_a_list_field_that_is_not_a_list(key, value):
    verdict = review.validate_verdict(_blueprint(**{key: value}))
    assert verdict.valid is False
    assert verdict.outcome == "PASS"
    assert _single_problem(verdict).startswith(f"{key} is not a list")


@pytest.mark.parametrize("key", _LIST_KEYS)
def test_validate_verdict_accepts_a_list_field_that_is_a_list(key):
    for value in ([], ["one"], ["one", {"two": 2}, 3]):
        assert review.validate_verdict(_blueprint(**{key: value})).valid is True


@pytest.mark.parametrize("flag", [True, False])
def test_validate_verdict_reports_the_tamper_flag(flag):
    verdict = review.validate_verdict(_blueprint(gate_tampering_suspected=flag))
    assert (verdict.valid, verdict.tamper_suspected) == (True, flag)  # a suspected PASS is still well formed


def test_validate_verdict_tamper_flag_is_false_when_absent():
    assert review.validate_verdict(_skill()).tamper_suspected is False


@pytest.mark.parametrize("flag", ["true", "false", "yes", 1, 0, None, [], {}])
def test_validate_verdict_rejects_a_tamper_flag_that_is_not_a_bool(flag):
    verdict = review.validate_verdict(_blueprint(gate_tampering_suspected=flag))
    assert verdict.valid is False
    assert verdict.tamper_suspected is False
    assert _single_problem(verdict).startswith("gate_tampering_suspected is not a bool")


def test_validate_verdict_tolerates_extra_keys():
    verdict = review.validate_verdict(_skill(summary="ok", confidence=0.9, nested={"a": [1, 2]}))
    assert (verdict.valid, verdict.outcome, verdict.problems) == (True, "PASS", ())


def test_validate_verdict_reports_every_problem_not_just_the_first():
    verdict = review.validate_verdict(
        {"review_status": "nope", "commit": "zz", "test_gaps": "none", "gate_tampering_suspected": "no"}
    )
    assert verdict.valid is False
    assert isinstance(verdict.problems, tuple)
    assert len(verdict.problems) == 4


def test_validate_verdict_clips_a_huge_value_in_its_problem_text():
    verdict = review.validate_verdict(_blueprint(review_status="x" * 10_000))
    assert len(_single_problem(verdict)) < 200


# --- verdict_matches_head (ASES-GIT-03): a PASS counts for one commit --------------------------------------


@pytest.mark.parametrize(
    "commit, expected",
    [
        pytest.param(None, True, id="no-commit-named"),
        pytest.param(_HEAD, True, id="full-sha"),
        pytest.param(_HEAD[:7], True, id="short-sha"),
        pytest.param(_HEAD[:12], True, id="12-chars"),
        pytest.param(_HEAD[:12].upper(), True, id="uppercase-hex"),
        pytest.param("1234567", False, id="inside-the-head-but-not-a-prefix"),
        pytest.param("fedcba9", False, id="different-commit"),
        pytest.param(_HEAD[:-1] + "8", False, id="one-digit-off"),
        pytest.param(_HEAD + "0", False, id="longer-than-the-head"),
    ],
)
def test_verdict_matches_head(commit, expected):
    verdict = review.Verdict(valid=True, outcome="PASS", problems=(), commit=commit, tamper_suspected=False)
    assert review.verdict_matches_head(verdict, _HEAD) is expected


def test_verdict_matches_head_is_false_for_an_empty_head():
    verdict = review.Verdict(valid=True, outcome="PASS", problems=(), commit="0123456", tamper_suspected=False)
    assert review.verdict_matches_head(verdict, "") is False


def test_a_quoted_short_sha_from_validate_verdict_matches_only_its_own_head():
    verdict = review.validate_verdict(_blueprint(commit=_HEAD[:9]))
    assert review.verdict_matches_head(verdict, _HEAD) is True
    assert review.verdict_matches_head(verdict, "f" * 40) is False


# --- record_verdict: stored by commit, redacted, idempotent ------------------------------------------------


def _verdict_rows(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM review_verdicts ORDER BY project, task_key, commit_sha")]


def test_record_verdict_stores_the_verdict_by_project_task_and_commit(tmp_path):
    """`outcome` is stored as given: the caller passes the normalised outcome from validate_verdict."""
    conn = db.connect(tmp_path / "ases.db")

    review.record_verdict(conn, "proj", "T1", _HEAD, "t_1", "PASS", "reviewer", _skill())

    (row,) = _verdict_rows(conn)
    assert (row["project"], row["task_key"], row["commit_sha"], row["card_id"]) == ("proj", "T1", _HEAD, "t_1")
    assert (row["outcome"], row["reviewer_profile"]) == ("PASS", "reviewer")
    assert json.loads(row["metadata"]) == _skill()
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00", row["recorded_at"])


def test_record_verdict_is_idempotent_and_the_latest_verdict_for_a_commit_wins(tmp_path):
    conn = db.connect(tmp_path / "ases.db")
    for _ in range(2):
        review.record_verdict(conn, "proj", "T1", _HEAD, "t_1", "PASS", "reviewer", _skill())
    assert len(_verdict_rows(conn)) == 1

    conn.execute("UPDATE review_verdicts SET recorded_at = '2000-01-01T00:00:00+00:00'")
    review.record_verdict(
        conn, "proj", "T1", _HEAD, "t_2", "CHANGES_REQUIRED", "reviewer-2",
        _blueprint(review_status="CHANGES_REQUIRED"),
    )

    (row,) = _verdict_rows(conn)
    assert (row["card_id"], row["outcome"], row["reviewer_profile"]) == ("t_2", "CHANGES_REQUIRED", "reviewer-2")
    assert json.loads(row["metadata"])["review_status"] == "CHANGES_REQUIRED"
    assert row["recorded_at"] != "2000-01-01T00:00:00+00:00"


def test_record_verdict_keeps_one_row_per_project_task_and_commit(tmp_path):
    conn = db.connect(tmp_path / "ases.db")
    review.record_verdict(conn, "proj", "T1", _HEAD, "t_1", "PASS", "reviewer", _skill())
    review.record_verdict(conn, "proj", "T1", "f" * 40, "t_1", "PASS", "reviewer", _skill())  # a later commit
    review.record_verdict(conn, "proj", "T2", _HEAD, "t_2", "PASS", "reviewer", _skill())  # another task
    review.record_verdict(conn, "other", "T1", _HEAD, "t_3", "PASS", "reviewer", _skill())  # another project

    assert len(_verdict_rows(conn)) == 4


def test_record_verdict_redacts_secrets_before_they_reach_the_table(tmp_path):
    secret = "sk-abcdefghijklmnopqrstuvwx"
    conn = db.connect(tmp_path / "ases.db")
    metadata = _skill(
        reviewer_checks=[f"ran the app with {secret} and it worked", "read the diff"],
        notes={"deep": [f"used {secret}"]},
        api_key="credential-named-key-with-a-plain-value",
    )

    review.record_verdict(conn, "proj", "T1", _HEAD, "t_1", "PASS", "reviewer", metadata)
    review.record_verdict(conn, "proj", "T2", _HEAD, "t_2", "PASS", "reviewer", json.dumps(metadata))

    for row in _verdict_rows(conn):
        assert secret not in row["metadata"]
        stored = json.loads(row["metadata"])
        assert stored["api_key"] == "[redacted]"
        assert stored["reviewer_checks"] == ["ran the app with [redacted] and it worked", "read the diff"]
        assert stored["review_outcome"] == "approved"  # only the secrets went, not the rest


def test_record_verdict_redacts_text_metadata_that_is_not_json(tmp_path):
    secret = "sk-abcdefghijklmnopqrstuvwx"
    conn = db.connect(tmp_path / "ases.db")

    review.record_verdict(conn, "proj", "T1", _HEAD, "t_1", "PASS", "reviewer", f"approved, the key was {secret}")

    (row,) = _verdict_rows(conn)
    assert secret not in row["metadata"]


@pytest.mark.parametrize("metadata", [None, "null"], ids=["none", "json-null"])
def test_record_verdict_stores_null_when_there_is_no_metadata(metadata, tmp_path):
    conn = db.connect(tmp_path / "ases.db")

    review.record_verdict(conn, "proj", "T1", _HEAD, "t_1", "BLOCKED", "reviewer", metadata)

    (row,) = _verdict_rows(conn)
    assert row["metadata"] is None


def test_record_verdict_stores_a_value_json_cannot_encode_as_text_instead_of_raising(tmp_path):
    conn = db.connect(tmp_path / "ases.db")

    review.record_verdict(
        conn, "proj", "T1", _HEAD, "t_1", "PASS", "reviewer", _skill(checked_paths={"src/a.py", "src/a.py"}),
    )

    (row,) = _verdict_rows(conn)
    assert json.loads(row["metadata"])["checked_paths"] == "{'src/a.py'}"


def test_record_verdict_parses_a_json_string_before_storing_it(tmp_path):
    conn = db.connect(tmp_path / "ases.db")

    review.record_verdict(conn, "proj", "T1", _HEAD, "t_1", "PASS", "reviewer", json.dumps(_skill()))

    (row,) = _verdict_rows(conn)
    assert json.loads(row["metadata"]) == _skill()  # a dict, not the text of one wrapped in a string


def test_evidence_short_output_is_returned_untouched():
    assert review._evidence("short") == "short"
    assert review._evidence("x" * 1500) == "x" * 1500


def test_evidence_keeps_the_head_and_the_tail_within_the_limit():
    text = "HEAD" + "m" * 5000 + "TAIL"

    trimmed = review._evidence(text)

    assert len(trimmed) == 1500
    assert trimmed.startswith("HEAD") and trimmed.endswith("TAIL")
    assert "...[trimmed]..." in trimmed


def test_a_rename_into_scope_still_reveals_the_out_of_scope_path_it_removed(repo, tmp_path):
    """Found by the merge-check builder: git diff --name-only detects renames, so `git mv SECRETS.md src/a.py`
    listed only src/a.py and the touches check never saw that SECRETS.md was removed."""
    (repo / "SECRETS.md").write_text("keep me out of scope\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "add a file outside the task's scope", cwd=repo)
    _git("checkout", "-q", "-b", "swarm/RN1", cwd=repo)
    (repo / "src").mkdir()
    _git("mv", "SECRETS.md", "src/a.py", cwd=repo)
    _git("commit", "-q", "-m", "rename into scope", cwd=repo)
    _git("checkout", "-q", "integration", cwd=repo)
    conn = db.connect(tmp_path / "ases.db")

    result = review.check_branch(repo, "swarm/RN1", "integration", ["echo ok"], ["src/*"], conn=conn, task_key="RN1")

    assert result.ok is False and result.kind == "out_of_scope"
    assert "SECRETS.md" in result.detail


# ---------------------------------------------------------------------------------------------
# Binding the approval to a commit (ASES-GIT-03): reviewed_commit / require_binding.
# ---------------------------------------------------------------------------------------------

def test_binding_with_no_reviewed_commit_is_refused_before_anything_runs(repo, tmp_path, monkeypatch):
    _branch_with_changes(repo, "swarm/BD1", {"src/a.py": "x=1\n"})
    conn = db.connect(tmp_path / "ases.db")
    attempts = _forbid_gate1(monkeypatch)

    result = review.check_branch_for_merge(
        repo, "swarm/BD1", "integration", ["echo ok"], ["src/*"], conn=conn, task_key="BD1",
        require_binding=True, reviewed_commit=None,
    )

    assert result.ok is False and result.kind == "unbound_review"
    assert result.head == _head(repo, "swarm/BD1") and "cannot be bound" in result.detail
    assert attempts == []


def test_binding_refuses_a_head_that_is_not_the_reviewed_commit_even_with_no_gate_record(repo, tmp_path, monkeypatch):
    """The race the nemotron review found: no Gate 1 record exists yet (the review lane never saw the card), a
    commit was added after the approval, and only the binding can tell it is not the reviewed one."""
    _branch_with_changes(repo, "swarm/BD2", {"src/a.py": "x=1\n"})
    reviewed = _head(repo, "swarm/BD2")
    _git("checkout", "-q", "swarm/BD2", cwd=repo)
    (repo / "src" / "late.py").write_text("late = 1\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "committed after the approval", cwd=repo)
    _git("checkout", "-q", "integration", cwd=repo)
    conn = db.connect(tmp_path / "ases.db")
    attempts = _forbid_gate1(monkeypatch)

    result = review.check_branch_for_merge(
        repo, "swarm/BD2", "integration", ["echo ok"], ["src/*"], conn=conn, task_key="BD2",
        require_binding=True, reviewed_commit=reviewed,
    )

    assert result.ok is False and result.kind == "stale_review"
    assert reviewed in result.detail and result.head == _head(repo, "swarm/BD2")
    assert attempts == []


@pytest.mark.parametrize("transform", [lambda sha: sha, lambda sha: sha[:7], lambda sha: sha[:12].upper()])
def test_binding_accepts_the_full_sha_a_short_prefix_and_an_uppercase_prefix(repo, tmp_path, transform):
    _branch_with_changes(repo, "swarm/BD3", {"src/a.py": "x=1\n"})
    reviewed = transform(_head(repo, "swarm/BD3"))
    conn = db.connect(tmp_path / "ases.db")

    result = review.check_branch_for_merge(
        repo, "swarm/BD3", "integration", ["echo ok"], ["src/*"], conn=conn, task_key="BD3",
        require_binding=True, reviewed_commit=reviewed,
    )

    assert result.ok is True and result.kind == "ok"


def test_without_require_binding_reviewed_commit_is_ignored_as_before(repo, tmp_path):
    _branch_with_changes(repo, "swarm/BD4", {"src/a.py": "x=1\n"})
    conn = db.connect(tmp_path / "ases.db")

    result = review.check_branch_for_merge(
        repo, "swarm/BD4", "integration", ["echo ok"], ["src/*"], conn=conn, task_key="BD4",
        reviewed_commit="deadbeef",
    )

    assert result.ok is True


def test_scope_is_still_checked_before_the_binding(repo, tmp_path):
    _branch_with_changes(repo, "swarm/BD5", {"SECRETS.md": "oops\n"})
    conn = db.connect(tmp_path / "ases.db")

    result = review.check_branch_for_merge(
        repo, "swarm/BD5", "integration", ["echo ok"], ["src/*"], conn=conn, task_key="BD5",
        require_binding=True, reviewed_commit=None,
    )

    assert result.kind == "out_of_scope"


# ---------------------------------------------------------------------------------------------
# Round 5: the tamper check is part of Gate 1 (ASES-QG-03, ASES-QG-02, ASES-GIT-07). It runs after the scope check
# and before Gate 1, in both the review lane and the merge queue, over the merge-base range the scope check used.
# Real temp git repos throughout; the only fake is tamper.check_range where a test needs it to fail or to be watched.
# ---------------------------------------------------------------------------------------------

_TEST_FILE = "def test_a():\n    assert 1 == 1\n\n\ndef test_b():\n    assert 2 == 2\n"

# name -> (files committed on integration first, what the branch changes (None deletes the file), the task's touches,
# the finding kind the tamper check must report, and the path it must name)
_TAMPER_SCENARIOS = {
    "deleted-test-file": (
        {"tests/test_a.py": _TEST_FILE}, {"tests/test_a.py": None}, ["tests/*"], "test_file_deleted",
        "tests/test_a.py",
    ),
    "removed-test": (
        {"tests/test_a.py": _TEST_FILE}, {"tests/test_a.py": "def test_a():\n    assert 1 == 1\n"}, ["tests/*"],
        "test_deleted", "tests/test_a.py",
    ),
    "skip-marker": (
        {"tests/test_a.py": _TEST_FILE},
        {"tests/test_a.py": "import pytest\n\n\n@pytest.mark.skip\n" + _TEST_FILE}, ["tests/*"], "skip_marker",
        "tests/test_a.py",
    ),
    "or-true": (
        {"scripts/ci.sh": "#!/bin/sh\npytest -q\n"}, {"scripts/ci.sh": "#!/bin/sh\npytest -q || true\n"},
        ["scripts/*"], "unconditional_pass", "scripts/ci.sh",
    ),
    "artifact": (
        {}, {"src/__pycache__/a.cpython-311.pyc": "junk\n", "src/a.py": "x = 1\n"}, ["src/*"], "generated_artifact",
        "src/__pycache__/a.cpython-311.pyc",
    ),
    "secret": ({}, {"src/config.py": f"API_KEY = '{_LEAK}'\n"}, ["src/*"], "secret_added", "src/config.py"),
}


def _commit_on_integration(repo, files: dict):
    for path, content in files.items():
        full = repo / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "base", cwd=repo)


def _branch_with_edit(repo, branch, changes: dict):
    """A branch that writes each path in `changes` (None deletes it); leaves the repo back on integration."""
    _git("checkout", "-q", "-b", branch, cwd=repo)
    for path, content in changes.items():
        full = repo / path
        if content is None:
            full.unlink()
        else:
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(content, encoding="utf-8")
    _git("add", "-A", "-f", cwd=repo)  # -f: a global ignore file must not hide the __pycache__ a test plants
    _git("commit", "-q", "-m", "work", cwd=repo)
    _git("checkout", "-q", "integration", cwd=repo)


def _tamper_branch(repo, scenario, branch):
    base_files, changes, touches, kind, path = _TAMPER_SCENARIOS[scenario]
    if base_files:
        _commit_on_integration(repo, base_files)
    _branch_with_edit(repo, branch, changes)
    return touches, kind, path


def _events(conn, kind):
    return [json.loads(r["payload"]) for r in conn.execute(
        "SELECT payload FROM events WHERE kind = ? ORDER BY id", (kind,))]


def _raise_in_check_range(monkeypatch, exc):
    def boom(*args, **kwargs):
        raise exc

    monkeypatch.setattr(tamper, "check_range", boom)


# --- what blocks: one real repo per kind that matters --------------------------------------------------------------


@pytest.mark.parametrize("scenario", list(_TAMPER_SCENARIOS))
def test_check_branch_blocks_each_kind_of_tampering_before_gate1_runs(scenario, repo, tmp_path, monkeypatch):
    touches, kind, path = _tamper_branch(repo, scenario, "swarm/K1")
    conn = db.connect(tmp_path / "ases.db")
    attempts = _forbid_gate1(monkeypatch)

    result = review.check_branch(repo, "swarm/K1", "integration", ["echo gate-ok"], touches, conn=conn, task_key="K1")

    assert (result.ok, result.kind, result.head) == (False, "tamper", _head(repo, "swarm/K1"))
    assert f"{kind} {path}" in result.detail
    assert _LEAK not in result.detail and "PLANTEDVALUE" not in result.detail  # a finding never repeats a secret
    assert attempts == [] and _gate_rows(conn, "K1") == []                      # Gate 1 was never reached


@pytest.mark.parametrize("scenario", list(_TAMPER_SCENARIOS))
def test_a_tampering_card_is_sent_back_exactly_like_a_red_gate1_with_the_findings_as_the_reason(
    scenario, repo, tmp_path, monkeypatch,
):
    touches, kind, path = _tamper_branch(repo, scenario, "swarm/K2")
    conn = db.connect(tmp_path / "ases.db")
    reopened = []
    monkeypatch.setattr(hermes, "kanban_reopen_review", lambda b, c, r: reopened.append((b, c, r)))
    _forbid_gate1(monkeypatch)
    check = review.check_branch(repo, "swarm/K2", "integration", ["echo gate-ok"], touches, conn=conn, task_key="K2")

    ok = review.gate_before_review(
        "b", "t_k", repo, "swarm/K2", "integration", ["echo gate-ok"], touches, conn=conn, task_key="K2",
    )

    assert ok is False
    assert reopened == [("b", "t_k", check.detail)]
    assert f"{kind} {path}" in reopened[0][2] and _LEAK not in reopened[0][2]
    assert _events(conn, "tamper_check_error") == []  # a finding is not an error of the check
    # What was found is on record too (the send-back is only a comment on the card, and the report reads events).
    assert _events(conn, "tamper_blocked") == [{
        "task_key": "K2", "card_id": "t_k", "head": _head(repo, "swarm/K2"), "detail": check.detail,
    }]


@pytest.mark.parametrize("kind", ["unresolvable_branch", "no_merge_base", "out_of_scope", "gate1_red"])
def test_only_a_tamper_send_back_records_a_tamper_blocked_event(kind, repo, tmp_path, monkeypatch):
    branch, touches, commands = _scenario(repo, kind)
    conn = db.connect(tmp_path / "ases.db")
    monkeypatch.setattr(hermes, "kanban_reopen_review", lambda b, c, r: None)

    ok = review.gate_before_review("b", "t_x", repo, branch, "integration", commands, touches, conn=conn, task_key="X1")

    assert ok is False
    assert _events(conn, "tamper_blocked") == [] and _events(conn, "tamper_check_error") == []


@pytest.mark.parametrize("scenario", list(_TAMPER_SCENARIOS))
def test_the_merge_check_returns_the_tamper_result_even_with_a_green_gate1_record_for_the_head(
    scenario, repo, tmp_path, monkeypatch,
):
    touches, kind, path = _tamper_branch(repo, scenario, "swarm/K3")
    conn = db.connect(tmp_path / "ases.db")
    head = _head(repo, "swarm/K3")
    _record_gate(conn, "K3", head, "pass")  # the record may predate the check: it does not excuse the diff
    attempts = _forbid_gate1(monkeypatch)

    result = review.check_branch_for_merge(
        repo, "swarm/K3", "integration", ["echo gate-ok"], touches, conn=conn, task_key="K3",
    )

    assert (result.ok, result.kind, result.head) == (False, "tamper", head)
    assert f"{kind} {path}" in result.detail
    assert attempts == []
    # The two checks read the same range with the same rules, so they say the same thing.
    assert result == review.check_branch(
        repo, "swarm/K3", "integration", ["echo gate-ok"], touches, conn=conn, task_key="K3",
    )


def test_the_reason_is_the_tamper_modules_own_findings_text(repo, tmp_path, monkeypatch):
    touches, _kind, _path = _tamper_branch(repo, "skip-marker", "swarm/K4")
    conn = db.connect(tmp_path / "ases.db")
    _forbid_gate1(monkeypatch)
    base = _git("merge-base", "integration", "swarm/K4", cwd=repo).stdout.strip()
    findings = tamper.check_range(repo, base, _head(repo, "swarm/K4"), allow_paths=touches)

    result = review.check_branch(repo, "swarm/K4", "integration", ["echo gate-ok"], touches, conn=conn, task_key="K4")

    assert findings and len(tamper.format_findings(findings)) <= 1500
    assert result.detail == tamper.format_findings(tamper.blocking(findings))


def test_a_long_findings_text_is_trimmed_like_gate1_evidence_keeping_both_ends(repo, tmp_path):
    names = {f"src/__pycache__/module_number_{i:02d}_with_a_long_name_to_fill_the_line.cpython-311.pyc": "x\n"
             for i in range(25)}
    _branch_with_edit(repo, "swarm/K5", names)
    conn = db.connect(tmp_path / "ases.db")
    base = _git("merge-base", "integration", "swarm/K5", cwd=repo).stdout.strip()
    raw = tamper.format_findings(tamper.blocking(
        tamper.check_range(repo, base, _head(repo, "swarm/K5"), allow_paths=["src/*"])))

    result = review.check_branch(repo, "swarm/K5", "integration", ["echo gate-ok"], ["src/*"], conn=conn, task_key="K5")

    assert len(raw) > 1500 and result.kind == "tamper"
    assert result.detail == review._evidence(raw) and len(result.detail) == 1500
    assert "\n...[trimmed]...\n" in result.detail
    assert result.detail.startswith("generated_artifact src/__pycache__/module_number_00")
    assert re.search(r"\.\.\. and \d+ more$", result.detail)  # the tail, with the count of what was left out


def test_only_blocking_findings_block_and_only_they_are_reported(repo, tmp_path, monkeypatch):
    _branch_with_changes(repo, "swarm/K6", {"src/a.py": "x=1\n"})
    conn = db.connect(tmp_path / "ases.db")
    _fake_gate1(monkeypatch, passed=True)
    informational = tamper.Finding("large_file", "src/a.py", "just so you know", blocks=False)

    monkeypatch.setattr(tamper, "check_range", lambda *a, **kw: [informational])
    only_info = review.check_branch(repo, "swarm/K6", "integration", ["echo gate-ok"], ["src/*"], conn=conn, task_key="K6")

    monkeypatch.setattr(
        tamper, "check_range",
        lambda *a, **kw: [informational, tamper.Finding("skip_marker", "src/a.py", "skip marker added: @skip", 3)],
    )
    mixed = review.check_branch(repo, "swarm/K6", "integration", ["echo gate-ok"], ["src/*"], conn=conn, task_key="K6")

    assert (only_info.ok, only_info.kind) == (True, "ok")
    assert (mixed.ok, mixed.kind) == (False, "tamper")
    assert mixed.detail == "skip_marker src/a.py:3: skip marker added: @skip"


def test_an_ordinary_change_with_new_tests_and_new_assertions_passes_the_tamper_check(repo, tmp_path):
    _commit_on_integration(repo, {"tests/test_a.py": _TEST_FILE, "src/a.py": "x = 1\n"})
    _branch_with_edit(repo, "swarm/K7", {
        "tests/test_a.py": _TEST_FILE + "\n\ndef test_c():\n    assert double(3) == 6\n    assert double(4) == 8\n",
        "src/a.py": "x = 2\n", "src/b.py": "y = 3\n",
    })
    conn = db.connect(tmp_path / "ases.db")

    result = review.check_branch(
        repo, "swarm/K7", "integration", ["echo gate-ok"], ["src/*", "tests/*"], conn=conn, task_key="K7",
    )

    assert (result.ok, result.kind) == (True, "ok")
    assert _gate_rows(conn, "K7") == [("gate1", _head(repo, "swarm/K7"), "pass")]


# --- the range, the allow paths and the gate config paths the tamper check is given ---------------------------------


def _spy_check_range(monkeypatch):
    seen = []
    real = tamper.check_range

    def spy(repo_arg, base, head, **kwargs):
        seen.append((base, head, kwargs))
        return real(repo_arg, base, head, **kwargs)

    monkeypatch.setattr(tamper, "check_range", spy)
    return seen


def test_the_tamper_check_reads_the_range_the_scope_check_read_and_is_given_the_touches_and_gate_files(
    repo, tmp_path, monkeypatch,
):
    """The branch is cut, then a test file lands on integration. Only a range that starts at the merge-base leaves
    that file out (compared tip to tip it would read as a test the branch deleted). The spy pins the base to the
    merge-base and the head to the resolved head, and the run pins that the range is really the branch's own."""
    _branch_with_changes(repo, "swarm/G2", {"tools/check.py": "print('ok')\n", "src/a.py": "x = 1\n"})
    cut_from = _head(repo, "integration")
    _commit_on_integration(repo, {"tests/test_new.py": _TEST_FILE})
    conn = db.connect(tmp_path / "ases.db")
    seen = _spy_check_range(monkeypatch)
    touches = ["tools/*", "src/*"]

    # tools/check.py is also what the gate command names (gate_config_paths), so ASES-QG-02 needs the marker
    # here too; that is not what this test is about (it is about the range check_branch computes and hands on).
    result = review.check_branch(
        repo, "swarm/G2", "integration", ["python tools/check.py", "echo gate-ok"], touches, conn=conn,
        task_key="G2", allow_gate_config_changes=True,
    )

    assert (result.ok, result.kind) == (True, "ok"), result.detail
    (base, head, kwargs), = seen
    assert head == _head(repo, "swarm/G2")
    assert base == cut_from == _git("merge-base", "integration", "swarm/G2", cwd=repo).stdout.strip()
    assert base != _head(repo, "integration")
    assert kwargs["allow_paths"] == touches
    assert list(kwargs["gate_config_paths"]) == ["tools/check.py"]
    assert kwargs["allow_gate_config_changes"] is True


def test_the_merge_check_gives_the_tamper_check_the_same_range_touches_and_gate_files(repo, tmp_path, monkeypatch):
    _branch_with_changes(repo, "swarm/G3", {"tools/check.py": "print('ok')\n", "src/a.py": "x = 1\n"})
    conn = db.connect(tmp_path / "ases.db")
    seen = _spy_check_range(monkeypatch)
    touches = ["tools/*", "src/*"]

    result = review.check_branch_for_merge(
        repo, "swarm/G3", "integration", ["python tools/check.py"], touches, conn=conn, task_key="G3",
        allow_gate_config_changes=True,
    )

    assert result.ok is True
    (base, head, kwargs), = seen
    assert head == _head(repo, "swarm/G3") and base == _git("merge-base", "integration", "swarm/G3", cwd=repo).stdout.strip()
    assert kwargs["allow_paths"] == touches and list(kwargs["gate_config_paths"]) == ["tools/check.py"]
    assert kwargs["allow_gate_config_changes"] is True


def test_a_gate_config_file_the_tasks_touches_name_is_not_allowed_without_the_marker(repo, tmp_path):
    """ASES-QG-02 (round 9, CIPIN): changing test-runner configuration needs a plan task that EXPLICITLY allows
    it, not merely a task whose touches happen to name the file. Before this fix, naming pytest.ini literally in
    touches was enough on its own -- the tamper check read "in touches" as "a plan task allows it", and since
    review.check_branch's own scope check (_check_scope) never lets a diff through unless every changed path is
    already inside touches, that reading meant gate_config_changed could never actually block anything reaching
    this function: whatever passed the scope check was automatically read as allowed. A task could quietly
    change pytest.ini merely by listing it in touches, marker or not."""
    _commit_on_integration(repo, {"pytest.ini": "[pytest]\naddopts = -q\n", "src/a.py": "x = 1\n"})
    _branch_with_edit(repo, "swarm/G4", {"pytest.ini": "[pytest]\naddopts = -q -x\n"})
    conn = db.connect(tmp_path / "ases.db")

    result = review.check_branch(
        repo, "swarm/G4", "integration", ["echo gate-ok"], ["pytest.ini"], conn=conn, task_key="G4",
    )

    assert (result.ok, result.kind) == (False, "tamper")
    assert "gate_config_changed pytest.ini" in result.detail
    assert "allow_gate_config_changes" in result.detail


def test_a_gate_config_file_the_tasks_touches_name_is_allowed_with_the_marker(repo, tmp_path):
    """The other half: a plan task that DOES set allow_gate_config_changes (plan.PlanTask, threaded through
    controller.py's task.allow_gate_config_changes) still gets its own declared change through."""
    _commit_on_integration(repo, {"pytest.ini": "[pytest]\naddopts = -q\n", "src/a.py": "x = 1\n"})
    _branch_with_edit(repo, "swarm/G4b", {"pytest.ini": "[pytest]\naddopts = -q -x\n"})
    conn = db.connect(tmp_path / "ases.db")

    result = review.check_branch(
        repo, "swarm/G4b", "integration", ["echo gate-ok"], ["pytest.ini"], conn=conn, task_key="G4b",
        allow_gate_config_changes=True,
    )

    assert (result.ok, result.kind) == (True, "ok")


def test_a_gate_config_file_the_tasks_touches_do_not_name_never_gets_past_the_scope_check(repo, tmp_path, monkeypatch):
    """A THIRD way ASES-QG-02 is enforced: the scope check runs first, so a config change the task does not name
    anywhere in its touches is refused as out_of_scope before the tamper check (which would also report it, via
    gate_config_changed) is ever consulted."""
    _commit_on_integration(repo, {"pytest.ini": "[pytest]\naddopts = -q\n", "src/a.py": "x = 1\n"})
    _branch_with_edit(repo, "swarm/G5", {"pytest.ini": "[pytest]\naddopts = -q -x\n", "src/a.py": "x = 2\n"})
    conn = db.connect(tmp_path / "ases.db")
    seen = _spy_check_range(monkeypatch)

    result = review.check_branch(repo, "swarm/G5", "integration", ["echo gate-ok"], ["src/*"], conn=conn, task_key="G5")

    assert (result.ok, result.kind) == (False, "out_of_scope") and "pytest.ini" in result.detail
    assert seen == []


def test_the_scope_check_comes_before_the_tamper_check_in_both_lanes(repo, tmp_path, monkeypatch):
    _branch_with_changes(repo, "swarm/O1", {"src/config.py": f"API_KEY = '{_LEAK}'\n", "SECRETS.md": "oops\n"})
    conn = db.connect(tmp_path / "ases.db")
    seen = _spy_check_range(monkeypatch)

    lane = review.check_branch(repo, "swarm/O1", "integration", ["echo gate-ok"], ["src/*"], conn=conn, task_key="O1")
    queue = review.check_branch_for_merge(
        repo, "swarm/O1", "integration", ["echo gate-ok"], ["src/*"], conn=conn, task_key="O1",
    )

    assert lane.kind == queue.kind == "out_of_scope"
    assert seen == []


def test_the_binding_checks_come_before_the_tamper_check_and_the_tamper_check_before_any_gate_record(
    repo, tmp_path, monkeypatch,
):
    touches, _kind, _path = _tamper_branch(repo, "secret", "swarm/O2")
    conn = db.connect(tmp_path / "ases.db")
    head = _head(repo, "swarm/O2")
    _forbid_gate1(monkeypatch)

    def merge_check(reviewed):
        return review.check_branch_for_merge(
            repo, "swarm/O2", "integration", ["echo gate-ok"], touches, conn=conn, task_key="O2",
            require_binding=True, reviewed_commit=reviewed,
        )

    assert merge_check(None).kind == "unbound_review"
    assert merge_check("0" * 7).kind == "stale_review"
    assert merge_check(head).kind == "tamper"                # the approval is bound to this commit, which tampers
    _record_gate(conn, "O2", "1" * 40, "pass")               # a green record for an EARLIER commit, which would
    assert merge_check(head).kind == "tamper"                # otherwise read as "stale_review": tamper wins


# --- a check that could not run ---------------------------------------------------------------------------------------


def test_a_tamper_check_that_cannot_run_is_a_tamper_check_error_and_gate1_is_not_run(repo, tmp_path, monkeypatch):
    _branch_with_changes(repo, "swarm/E1", {"src/a.py": "x=1\n"})
    conn = db.connect(tmp_path / "ases.db")
    _raise_in_check_range(monkeypatch, tamper.TamperCheckError("git diff failed: fatal: bad object"))
    attempts = _forbid_gate1(monkeypatch)

    result = review.check_branch(repo, "swarm/E1", "integration", ["echo gate-ok"], ["src/*"], conn=conn, task_key="E1")

    assert result == review.BranchCheck(
        ok=False, kind="tamper_check_error", detail="the tamper check could not run: git diff failed: fatal: bad object",
        head=_head(repo, "swarm/E1"),
    )
    assert attempts == [] and _gate_rows(conn, "E1") == []


def test_an_unexpected_failure_of_the_check_is_also_a_tamper_check_error_never_a_clean_pass(repo, tmp_path, monkeypatch):
    _branch_with_changes(repo, "swarm/E2", {"src/a.py": "x=1\n"})
    conn = db.connect(tmp_path / "ases.db")
    _raise_in_check_range(monkeypatch, RuntimeError("something broke inside the check"))
    attempts = _forbid_gate1(monkeypatch)

    result = review.check_branch(repo, "swarm/E2", "integration", ["echo gate-ok"], ["src/*"], conn=conn, task_key="E2")

    assert (result.ok, result.kind) == (False, "tamper_check_error")
    assert result.detail == "the tamper check could not run: RuntimeError: something broke inside the check"
    assert attempts == []


def test_the_error_reason_is_one_short_ascii_line_and_never_repeats_a_secret(repo, tmp_path, monkeypatch):
    _branch_with_changes(repo, "swarm/E3", {"src/a.py": "x=1\n"})
    conn = db.connect(tmp_path / "ases.db")
    _raise_in_check_range(monkeypatch, tamper.TamperCheckError(
        f"git failed near caf{chr(0xE9)}\n  with a token {_LEAK}\n" + "x" * 500))
    _forbid_gate1(monkeypatch)

    result = review.check_branch(repo, "swarm/E3", "integration", ["echo gate-ok"], ["src/*"], conn=conn, task_key="E3")

    assert result.kind == "tamper_check_error"
    assert result.detail.isascii() and "\n" not in result.detail
    assert "caf\\xe9" in result.detail and _LEAK not in result.detail and "[redacted]" in result.detail
    assert len(result.detail) <= len("the tamper check could not run: ") + 300 + len("[redacted]")


def test_gate_before_review_keeps_the_card_in_review_when_the_tamper_check_could_not_run(repo, tmp_path, monkeypatch):
    """A check that failed to run says nothing about the card, and the merge-time check is authoritative and fails
    closed. So the card is NOT sent back (kanban_reopen_review is never called), the failure is recorded, and it
    returns True. Round 16: a "not run" gate record IS posted (ASES-QG-01, see the dedicated gate-record tests)
    -- _forbid_review_sendback checks only that the card stays in review, not that no Hermes call was made."""
    _branch_with_changes(repo, "swarm/E4", {"src/a.py": "x=1\n"})
    conn = db.connect(tmp_path / "ases.db")
    _raise_in_check_range(monkeypatch, tamper.TamperCheckError("git diff failed: boom"))
    _forbid_review_sendback(monkeypatch)
    attempts = _forbid_gate1(monkeypatch)

    ok = review.gate_before_review(
        "b", "t_e", repo, "swarm/E4", "integration", ["echo gate-ok"], ["src/*"], conn=conn, task_key="E4",
    )

    assert ok is True and attempts == []
    assert _events(conn, "tamper_check_error") == [{
        "task_key": "E4", "card_id": "t_e", "head": _head(repo, "swarm/E4"),
        "reason": "the tamper check could not run: git diff failed: boom",
    }]


def test_gate_before_review_passes_its_project_through_to_a_tamper_check_error_event(repo, tmp_path, monkeypatch):
    """events.py package, round 9: gate_before_review's own tamper_check_error event must carry the same project
    as gate1_recheck_failed, the sibling event its one caller (controller.process_review_lane) already records
    with plan.project a few lines later -- checked against the events.project column directly, since _events()
    above only reads the payload, which never carries "project" for this event."""
    _branch_with_changes(repo, "swarm/E4B", {"src/a.py": "x=1\n"})
    conn = db.connect(tmp_path / "ases.db")
    _raise_in_check_range(monkeypatch, tamper.TamperCheckError("git diff failed: boom"))
    _forbid_review_sendback(monkeypatch)
    _forbid_gate1(monkeypatch)

    ok = review.gate_before_review(
        "b", "t_e4b", repo, "swarm/E4B", "integration", ["echo gate-ok"], ["src/*"], conn=conn, task_key="E4B",
        project="proj-a",
    )

    assert ok is True
    row = conn.execute("SELECT project FROM events WHERE kind = 'tamper_check_error'").fetchone()
    assert row["project"] == "proj-a"


def test_gate_before_review_passes_its_project_through_to_a_tamper_blocked_event(repo, tmp_path, monkeypatch):
    """Same as above, for the tamper_blocked event a real tampering finding records (as opposed to a check that
    could not run at all)."""
    touches, _, _ = _tamper_branch(repo, "deleted-test-file", "swarm/E4C")
    conn = db.connect(tmp_path / "ases.db")
    monkeypatch.setattr(hermes, "kanban_reopen_review", lambda b, c, r: None)
    _forbid_gate1(monkeypatch)

    ok = review.gate_before_review(
        "b", "t_e4c", repo, "swarm/E4C", "integration", ["echo gate-ok"], touches, conn=conn, task_key="E4C",
        project="proj-b",
    )

    assert ok is False
    row = conn.execute("SELECT project FROM events WHERE kind = 'tamper_blocked'").fetchone()
    assert row["project"] == "proj-b"


def test_the_merge_check_returns_a_tamper_check_error_as_it_is_and_no_green_record_excuses_it(repo, tmp_path, monkeypatch):
    _branch_with_changes(repo, "swarm/E5", {"src/a.py": "x=1\n"})
    conn = db.connect(tmp_path / "ases.db")
    head = _head(repo, "swarm/E5")
    _record_gate(conn, "E5", head, "pass")
    _raise_in_check_range(monkeypatch, tamper.TamperCheckError("git diff failed: boom"))
    attempts = _forbid_gate1(monkeypatch)

    result = review.check_branch_for_merge(
        repo, "swarm/E5", "integration", ["echo gate-ok"], ["src/*"], conn=conn, task_key="E5",
    )

    assert result == review.BranchCheck(
        ok=False, kind="tamper_check_error", detail="the tamper check could not run: git diff failed: boom", head=head,
    )
    assert attempts == [] and _events(conn, "tamper_check_error") == []  # recording it is the controller's decision


# --- gate_config_paths --------------------------------------------------------------------------------------------


_EXTENSIONS = [".sh", ".py", ".js", ".ts", ".mjs", ".cjs", ".json", ".yaml", ".yml", ".toml", ".cfg", ".ini", ".mk",
               ".bat", ".ps1", ".gradle"]


@pytest.fixture
def gate_repo(repo):
    """A repo whose integration branch holds the files the gate commands below name. Returns (repo, head)."""
    _commit_on_integration(repo, {
        "tools/check.py": "print('ok')\n", "tools/my script.py": "print('ok')\n", "scripts/run.sh": "echo ok\n",
        "pytest.ini": "[pytest]\n", "check.py": "print('ok')\n", "tests/test_a.py": _TEST_FILE,
        "docs/notes.txt": "notes\n", "notes.txt": "notes\n", "README": "readme\n", "UP.PY": "print('ok')\n",
        **{f"x{ext}": "content\n" for ext in _EXTENSIONS},
    })
    return repo, _head(repo, "integration")


def test_gate_config_paths_finds_a_script_path(gate_repo):
    repo, head = gate_repo

    assert review.gate_config_paths(["python tools/check.py"], repo, head) == ["tools/check.py"]
    assert review.gate_config_paths(["bash scripts/run.sh -x"], repo, head) == ["scripts/run.sh"]


def test_gate_config_paths_finds_a_config_file_with_no_path_separator(gate_repo):
    repo, head = gate_repo

    assert review.gate_config_paths(["pytest -c pytest.ini"], repo, head) == ["pytest.ini"]
    assert review.gate_config_paths(["python check.py"], repo, head) == ["check.py"]


def test_gate_config_paths_skips_a_flag_even_when_it_names_a_real_file(gate_repo):
    repo, head = gate_repo

    assert review.gate_config_paths(["pytest --rcfile=tools/check.py -c pytest.ini"], repo, head) == ["pytest.ini"]
    assert review.gate_config_paths(["pytest -x -q --tb=short"], repo, head) == []


def test_gate_config_paths_drops_a_path_that_is_not_in_the_commit(gate_repo):
    repo, head = gate_repo

    assert review.gate_config_paths(["python tools/missing.py", "python check.py"], repo, head) == ["check.py"]


def test_gate_config_paths_reads_the_commit_not_the_working_tree(gate_repo):
    repo, head = gate_repo
    _branch_with_edit(repo, "swarm/P1", {"tools/new_gate.py": "print('new')\n"})
    branch_head = _head(repo, "swarm/P1")
    (repo / "only_on_disk.py").write_text("print('x')\n", encoding="utf-8")   # untracked, never committed
    (repo / "tools" / "check.py").unlink()                                     # gone from disk, still in the commit

    assert review.gate_config_paths(["python only_on_disk.py"], repo, head) == []
    assert review.gate_config_paths(["python tools/check.py"], repo, head) == ["tools/check.py"]
    assert review.gate_config_paths(["python tools/new_gate.py"], repo, branch_head) == ["tools/new_gate.py"]
    assert review.gate_config_paths(["python tools/new_gate.py"], repo, head) == []  # it is a later commit's file


def test_gate_config_paths_reads_a_quoted_path_with_a_space_in_it(gate_repo):
    repo, head = gate_repo

    assert review.gate_config_paths(['python "tools/my script.py"'], repo, head) == ["tools/my script.py"]
    assert review.gate_config_paths(["python 'tools/my script.py'"], repo, head) == ["tools/my script.py"]


def test_gate_config_paths_finds_a_file_with_a_non_ascii_name(repo):
    name = f"tools/caf{chr(0xE9)}.py"  # built at run time so that this test file stays pure ASCII
    _commit_on_integration(repo, {name: "print('ok')\n"})

    assert review.gate_config_paths([f"python {name}"], repo, _head(repo, "integration")) == [name]


def test_gate_config_paths_does_not_count_a_directory_as_a_file(gate_repo):
    repo, head = gate_repo

    assert review.gate_config_paths(["pytest tests/", "pytest tests"], repo, head) == []
    assert review.gate_config_paths(["pytest tests/test_a.py"], repo, head) == ["tests/test_a.py"]


def test_gate_config_paths_normalises_the_path_it_reports(gate_repo):
    repo, head = gate_repo

    assert review.gate_config_paths(["python ./tools/check.py", "bash .//scripts/run.sh"], repo, head) == [
        "tools/check.py", "scripts/run.sh"]
    assert review.gate_config_paths(["python tools/./check.py", "python tools//check.py"], repo, head) == [
        "tools/check.py"]
    assert review.gate_config_paths(["python a/../check.py", "python tools/../tools/check.py"], repo, head) == [
        "check.py", "tools/check.py"]


def test_gate_config_paths_normalises_a_windows_path_to_forward_slashes(gate_repo):
    """shlex reads the backslash of tools\\check.py as an escape (giving toolscheck.py), so a command with a
    backslash is read a second time without escapes."""
    repo, head = gate_repo

    assert review.gate_config_paths(["python tools\\check.py"], repo, head) == ["tools/check.py"]
    assert review.gate_config_paths(['python "tools\\my script.py"'], repo, head) == ["tools/my script.py"]


def test_gate_config_paths_ignores_an_absolute_path_and_one_that_climbs_out_of_the_repo(gate_repo):
    repo, head = gate_repo

    assert review.gate_config_paths(
        ["python /tools/check.py", "python //tools/check.py", "python ../tools/check.py", "python ../../check.py",
         "ruff check ./", "ruff check ../", "ruff check ."], repo, head) == []


def test_gate_config_paths_removes_duplicates_and_keeps_the_order_of_first_appearance(gate_repo):
    repo, head = gate_repo

    commands = ["python tools/check.py", "bash scripts/run.sh tools/check.py", "python ./tools/check.py", "pytest -c pytest.ini"]
    assert review.gate_config_paths(commands, repo, head) == ["tools/check.py", "scripts/run.sh", "pytest.ini"]


def test_gate_config_paths_knows_every_script_and_config_extension(gate_repo):
    repo, head = gate_repo

    commands = [f"run x{ext}" for ext in _EXTENSIONS]
    assert review.gate_config_paths(commands, repo, head) == [f"x{ext}" for ext in _EXTENSIONS]
    assert review.gate_config_paths(["python UP.PY"], repo, head) == ["UP.PY"]   # the extension is matched without case


def test_gate_config_paths_needs_a_separator_or_a_known_extension(gate_repo):
    repo, head = gate_repo

    assert review.gate_config_paths(["cat notes.txt", "cat README"], repo, head) == []        # exist, but neither
    assert review.gate_config_paths(["cat docs/notes.txt"], repo, head) == ["docs/notes.txt"]  # a separator is enough


def test_gate_config_paths_accepts_a_single_command_given_as_a_string(gate_repo):
    repo, head = gate_repo

    assert review.gate_config_paths("python tools/check.py", repo, head) == ["tools/check.py"]


def test_gate_config_paths_ignores_a_quoted_word_with_a_line_break_in_it(gate_repo):
    """A newline in a batch name would start a second name, and a quoted word can carry one: it is dropped."""
    repo, head = gate_repo

    assert review.gate_config_paths(['python "tools/check.py\nscripts/run.sh"'], repo, head) == []
    assert review.gate_config_paths(['python "tools/check.py\rscripts/run.sh"'], repo, head) == []
    assert review.gate_config_paths(['python "tools/check.py" "scripts/run.sh"'], repo, head) == [
        "tools/check.py", "scripts/run.sh"]


@pytest.mark.parametrize("commands", [
    None, [], [None, 5, ["nested"], b"bytes"], ["python 'unterminated"], ["echo \"tools/check.py"],
    ["python tools/check.py\x00x.py"], [""], ["   "], 12,
], ids=["none", "empty", "not-strings", "unbalanced-single", "unbalanced-double", "nul", "empty-string", "blank", "int"])
def test_gate_config_paths_never_raises_on_odd_commands(gate_repo, commands):
    repo, head = gate_repo

    result = review.gate_config_paths(commands, repo, head)

    assert isinstance(result, list) and all(isinstance(p, str) for p in result)


def test_gate_config_paths_finds_nothing_for_an_unbalanced_quote_rather_than_a_wrong_path(gate_repo):
    repo, head = gate_repo

    assert review.gate_config_paths(["python 'unterminated"], repo, head) == []
    assert review.gate_config_paths(["python tools/check.py 'oops"], repo, head) == ["tools/check.py"]  # str.split fallback
    # Unbalanced AND a backslash: both readings fail to parse, and the plain split still finds the Windows path.
    assert review.gate_config_paths(["python 'x tools\\check.py"], repo, head) == ["tools/check.py"]


def test_gate_config_paths_never_raises_when_git_cannot_answer(gate_repo, tmp_path, monkeypatch):
    repo, head = gate_repo

    assert review.gate_config_paths(["python tools/check.py"], tmp_path / "not-a-repo", head) == []
    assert review.gate_config_paths(["python tools/check.py"], repo, "0" * 40) == []
    # An odd `head` finds nothing instead of reading the index (":path") or adding a name to the batch.
    for odd in ("", None, "-x", ":", head + "\n:tools/check.py", "a b", head + "\0"):
        assert review.gate_config_paths(["python tools/check.py"], repo, odd) == [], repr(odd)

    def no_git(*args, **kwargs):
        raise FileNotFoundError("git is not installed")

    monkeypatch.setattr(review.subprocess, "run", no_git)

    assert review.gate_config_paths(["python tools/check.py"], repo, head) == []


# --- round 9 (ASES-QG-04, ASES-SEC-03/05/07): Gate 1 routes through gates.resolve_runner -----------------------


class _FakeProjectConfig:
    def __init__(self, enabled=True):
        self.sandbox_enabled = enabled

    def sandbox_policy_config(self):
        return {"sandbox": {"image": "registry.example/toolchain:1.0"}}


class _FakeTask:
    def __init__(self, sandbox_network=False, sandbox_network_reason=""):
        self.sandbox_network = sandbox_network
        self.sandbox_network_reason = sandbox_network_reason


def _watch_resolve_runner(monkeypatch):
    calls = []
    real = gates.resolve_runner

    def spy(project_config, task=None):
        calls.append((project_config, task))
        return real(project_config, task)

    monkeypatch.setattr(gates, "resolve_runner", spy)
    return calls


def test_check_branch_passes_project_config_and_task_to_resolve_runner(repo, tmp_path, monkeypatch):
    _branch_with_changes(repo, "swarm/RR1", {"src/a.py": "x=1\n"})
    conn = db.connect(tmp_path / "ases.db")
    calls = _watch_resolve_runner(monkeypatch)
    project_config, task = _FakeProjectConfig(enabled=False), _FakeTask()

    review.check_branch(
        repo, "swarm/RR1", "integration", ["echo gate-ok"], ["src/*"], conn=conn, task_key="RR1",
        project_config=project_config, task=task,
    )

    assert calls == [(project_config, task)]


def test_check_branch_for_merge_passes_project_config_and_task_to_resolve_runner(repo, tmp_path, monkeypatch):
    _branch_with_changes(repo, "swarm/RR2", {"src/a.py": "x=1\n"})
    conn = db.connect(tmp_path / "ases.db")
    calls = _watch_resolve_runner(monkeypatch)
    project_config, task = _FakeProjectConfig(enabled=False), _FakeTask()

    review.check_branch_for_merge(
        repo, "swarm/RR2", "integration", ["echo gate-ok"], ["src/*"], conn=conn, task_key="RR2",
        project_config=project_config, task=task,
    )

    assert calls == [(project_config, task)]


def test_check_branch_with_no_project_config_resolves_the_host_runner_exactly_as_before(repo, tmp_path):
    """Default None/None: today's behaviour, unchanged (the resolution call still happens, but resolves to the
    host runner, exactly as gates.run_gate's own default)."""
    _branch_with_changes(repo, "swarm/RR3", {"src/a.py": "x=1\n"})
    conn = db.connect(tmp_path / "ases.db")
    head = _head(repo, "swarm/RR3")

    result = review.check_branch(
        repo, "swarm/RR3", "integration", ["echo gate-ok"], ["src/*"], conn=conn, task_key="RR3",
    )

    assert (result.ok, result.head) == (True, head)
    assert _gate_rows(conn, "RR3") == [("gate1", head, "pass")]


def test_check_branch_when_sandbox_enabled_gives_run_gate_the_sandbox_runner_and_self_contained_checkout(
    repo, tmp_path, monkeypatch,
):
    _branch_with_changes(repo, "swarm/RR4", {"src/a.py": "x=1\n"})
    conn = db.connect(tmp_path / "ases.db")
    seen = {}

    def fake_run_gate(repo_arg, head, gate_name, commands, *, conn, task_key, runner=None,
                       self_contained_checkout=False, **kwargs):
        seen["runner"] = runner
        seen["self_contained_checkout"] = self_contained_checkout
        return gates.GateResult(gate_name, head, True, "ok")

    monkeypatch.setattr(gates, "run_gate", fake_run_gate)

    review.check_branch(
        repo, "swarm/RR4", "integration", ["echo gate-ok"], ["src/*"], conn=conn, task_key="RR4",
        project_config=_FakeProjectConfig(enabled=True), task=_FakeTask(),
    )

    assert seen["self_contained_checkout"] is True
    assert callable(seen["runner"])


def test_gate_before_review_passes_project_config_and_task_through_to_check_branch(repo, tmp_path, monkeypatch):
    _branch_with_changes(repo, "swarm/RR5", {"src/a.py": "x=1\n"})
    conn = db.connect(tmp_path / "ases.db")
    seen = {}

    def fake_check_branch(repo_arg, branch, integration_branch, commands, touches, *, conn, task_key,
                           allow_gate_config_changes=False, project_config=None, task=None):
        seen["project_config"] = project_config
        seen["task"] = task
        return review.BranchCheck(True, "ok", "stubbed", "a" * 40)

    monkeypatch.setattr(review, "check_branch", fake_check_branch)
    project_config, task = _FakeProjectConfig(), _FakeTask()

    review.gate_before_review(
        "b", "card1", repo, "swarm/RR5", "integration", ["echo gate-ok"], ["src/*"], conn=conn, task_key="RR5",
        project_config=project_config, task=task,
    )

    assert (seen["project_config"], seen["task"]) == (project_config, task)


# --- gate_before_review: the ASES-QG-01 gate-record comment (round 16) ---------------------------------------


def _posted_comments(monkeypatch):
    """Replace hermes.kanban_comment (which the autouse _fake_gate_record_comment fixture already stubs to a
    no-op) with one that also records what was posted, so a test can inspect it."""
    posted = []
    monkeypatch.setattr(
        hermes, "kanban_comment", lambda board, card_id, text, **kw: posted.append((board, card_id, text)),
    )
    return posted


def test_format_gate_record_shows_the_header_gate_commit_result_and_commands():
    text = review._format_gate_record("abc123", "pass", ["pytest -q", "ruff check"], "line one\nline two")
    out = text.splitlines()
    assert out[0] == review.GATE_RECORD_HEADER == "ASES gate record"
    assert "gate: gate1" in out and "commit: abc123" in out and "result: PASS" in out
    assert "commands: pytest -q; ruff check" in out
    assert "line one" in text and "line two" in text


def test_format_gate_record_keeps_only_a_short_tail_of_a_long_output():
    body = "x" * 5000
    text = review._format_gate_record("abc", "fail", ["echo x"], body)
    tail = text.splitlines()[-1]
    assert len(tail) == review._GATE_RECORD_TAIL_LIMIT == len(body[-review._GATE_RECORD_TAIL_LIMIT:])
    assert tail == body[-review._GATE_RECORD_TAIL_LIMIT:]


def test_format_gate_record_redacts_a_secret_shaped_value_in_the_output():
    text = review._format_gate_record("abc", "fail", ["echo x"], f"leaked: {_LEAK}")
    assert _LEAK not in text and "[redacted]" in text


def test_format_gate_record_has_no_trailing_body_line_when_there_is_no_output():
    text = review._format_gate_record("", "not_run", ["echo x"], "")
    assert text.splitlines()[-1] == "commands: echo x"


def test_gate_before_review_posts_a_pass_gate_record_with_the_real_output_and_only_once(
    repo, tmp_path, monkeypatch,
):
    """ASES-QG-01 / ASES-REV-05: a green Gate 1 re-check is posted to the card, naming the gate, the outcome, the
    full commit SHA, the pinned commands and a tail of the real gate output -- and only once, even though Gate 1
    re-runs on every review-lane poll while the card sits waiting for the reviewer to be dispatched (the second
    call here is exactly that second poll, on the exact same, unmoved commit)."""
    _branch_with_changes(repo, "swarm/GR1", {"src/a.py": "x=1\n"})
    conn = db.connect(tmp_path / "ases.db")
    posted = _posted_comments(monkeypatch)
    head = _head(repo, "swarm/GR1")

    ok1 = review.gate_before_review(
        "b", "t_1", repo, "swarm/GR1", "integration", ["echo gate-output-marker"], ["src/*"],
        conn=conn, task_key="GR1",
    )
    ok2 = review.gate_before_review(
        "b", "t_1", repo, "swarm/GR1", "integration", ["echo gate-output-marker"], ["src/*"],
        conn=conn, task_key="GR1",
    )

    assert (ok1, ok2) == (True, True)
    assert len(posted) == 1  # not reposted on the second, identical pass
    board, card_id, text = posted[0]
    assert (board, card_id) == ("b", "t_1")
    assert text.splitlines()[0] == review.GATE_RECORD_HEADER
    assert f"commit: {head}" in text and "result: PASS" in text
    assert "commands: echo gate-output-marker" in text
    assert "gate-output-marker" in text  # the real gate output's own tail made it into the comment


def test_gate_before_review_posts_a_fail_gate_record(repo, tmp_path, monkeypatch):
    branch, touches, commands = _scenario(repo, "gate1_red")
    conn = db.connect(tmp_path / "ases.db")
    monkeypatch.setattr(hermes, "kanban_reopen_review", lambda *a, **kw: None)
    posted = _posted_comments(monkeypatch)

    ok = review.gate_before_review(
        "b", "t_2", repo, branch, "integration", commands, touches, conn=conn, task_key="GR2",
    )

    assert ok is False
    assert len(posted) == 1
    text = posted[0][2]
    assert "result: FAIL" in text and "[exit 1]" in text


def test_gate_before_review_posts_a_not_run_gate_record_truthfully_for_an_out_of_scope_diff(
    repo, tmp_path, monkeypatch,
):
    branch, touches, commands = _scenario(repo, "out_of_scope")
    conn = db.connect(tmp_path / "ases.db")
    monkeypatch.setattr(hermes, "kanban_reopen_review", lambda *a, **kw: None)
    posted = _posted_comments(monkeypatch)
    gate_ran = _forbid_gate1(monkeypatch)

    ok = review.gate_before_review(
        "b", "t_3", repo, branch, "integration", commands, touches, conn=conn, task_key="GR3",
    )

    assert ok is False and gate_ran == []  # Gate 1 truly never ran
    assert len(posted) == 1
    text = posted[0][2]
    assert "result: NOT RUN" in text and "SECRETS.md" in text


def test_gate_before_review_posts_a_not_run_gate_record_for_a_tamper_check_error_and_never_reposts_it(
    repo, tmp_path, monkeypatch,
):
    """The one case where the card stays in review (never sent back) and the check is retried on the exact
    same, unmoved commit every poll: the "not run" record must still be posted only once."""
    _branch_with_changes(repo, "swarm/GR4", {"src/a.py": "x=1\n"})
    conn = db.connect(tmp_path / "ases.db")
    _raise_in_check_range(monkeypatch, tamper.TamperCheckError("git diff failed: boom"))
    posted = _posted_comments(monkeypatch)

    ok1 = review.gate_before_review(
        "b", "t_4", repo, "swarm/GR4", "integration", ["echo gate-ok"], ["src/*"], conn=conn, task_key="GR4",
    )
    ok2 = review.gate_before_review(
        "b", "t_4", repo, "swarm/GR4", "integration", ["echo gate-ok"], ["src/*"], conn=conn, task_key="GR4",
    )

    assert (ok1, ok2) == (True, True)
    assert len(posted) == 1
    assert "result: NOT RUN" in posted[0][2]


def test_gate_before_review_reposts_when_a_retried_tamper_check_error_goes_on_to_an_actual_pass(
    repo, tmp_path, monkeypatch,
):
    """A DIFFERENT outcome for the SAME commit is new information for the reviewer and must still reach the
    card, even though _gate_record_posted already has an entry for this (task_key, commit): the dedup key
    includes the outcome, not just the commit, exactly so a transient tamper-check failure that clears on
    retry is not hidden behind a stale "not run yet" forever."""
    _branch_with_changes(repo, "swarm/GR5", {"src/a.py": "x=1\n"})
    conn = db.connect(tmp_path / "ases.db")
    posted = _posted_comments(monkeypatch)
    real_check_range = tamper.check_range
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise tamper.TamperCheckError("git diff failed: boom")
        return real_check_range(*args, **kwargs)

    monkeypatch.setattr(tamper, "check_range", flaky)

    ok1 = review.gate_before_review(
        "b", "t_5", repo, "swarm/GR5", "integration", ["echo gate-ok"], ["src/*"], conn=conn, task_key="GR5",
    )
    ok2 = review.gate_before_review(
        "b", "t_5", repo, "swarm/GR5", "integration", ["echo gate-ok"], ["src/*"], conn=conn, task_key="GR5",
    )

    assert (ok1, ok2) == (True, True)
    assert [text.splitlines()[3] for _, _, text in posted] == ["result: NOT RUN", "result: PASS"]


def test_gate_before_review_records_a_gate1_record_posted_event_with_the_project(repo, tmp_path, monkeypatch):
    _branch_with_changes(repo, "swarm/GR7", {"src/a.py": "x=1\n"})
    conn = db.connect(tmp_path / "ases.db")
    _posted_comments(monkeypatch)
    head = _head(repo, "swarm/GR7")

    review.gate_before_review(
        "b", "t_7", repo, "swarm/GR7", "integration", ["echo gate-ok"], ["src/*"], conn=conn, task_key="GR7",
        project="proj-z",
    )

    assert _events(conn, "gate1_record_posted") == [
        {"task_key": "GR7", "card_id": "t_7", "head": head, "outcome": "pass"},
    ]
    row = conn.execute("SELECT project FROM events WHERE kind = 'gate1_record_posted'").fetchone()
    assert row["project"] == "proj-z"


def test_gate_before_review_survives_a_hermes_failure_while_posting_the_gate_record(
    repo, tmp_path, monkeypatch,
):
    """Round 16 review finding: a Hermes hiccup while posting the gate-record comment (kanban_comment raising
    hermes.HermesCommandError, simulating a real Hermes CLI/network failure) must not turn an otherwise-green
    Gate 1 into an uncaught exception. gate_before_review's only caller (controller.process_review_lane) is not
    wrapped in the pass's `_isolated` helper, so an exception here would stop the review lane and every step
    after it in that controller pass, not just this one card."""
    _branch_with_changes(repo, "swarm/GR8", {"src/a.py": "x=1\n"})
    conn = db.connect(tmp_path / "ases.db")
    head = _head(repo, "swarm/GR8")

    def boom(board, card_id, text, **kw):
        raise hermes.HermesCommandError(["kanban", "comment"], 1, "hermes: connection refused")

    monkeypatch.setattr(hermes, "kanban_comment", boom)

    ok = review.gate_before_review(
        "b", "t_8", repo, "swarm/GR8", "integration", ["echo gate-ok"], ["src/*"],
        conn=conn, task_key="GR8", project="proj-z",
    )

    assert ok is True  # the gate result is still returned correctly despite the failed comment post
    assert _events(conn, "gate1_record_posted") == []
    failed = _events(conn, "gate_record_post_failed")
    assert len(failed) == 1
    assert failed[0]["task_key"] == "GR8" and failed[0]["card_id"] == "t_8"
    assert failed[0]["head"] == head and failed[0]["outcome"] == "pass"
    assert "HermesCommandError" in failed[0]["error"]
    row = conn.execute("SELECT project FROM events WHERE kind = 'gate_record_post_failed'").fetchone()
    assert row["project"] == "proj-z"


def test_gate_before_review_retries_the_gate_record_post_on_the_next_pass_after_a_hermes_failure(
    repo, tmp_path, monkeypatch,
):
    """Since a failed post is never marked as posted (_gate_record_posted has no matching event), the next
    review-lane pass on the same, unmoved commit tries again -- and this time it goes through."""
    _branch_with_changes(repo, "swarm/GR9", {"src/a.py": "x=1\n"})
    conn = db.connect(tmp_path / "ases.db")
    calls = {"n": 0}
    posted = []

    def flaky(board, card_id, text, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise hermes.HermesCommandError(["kanban", "comment"], 1, "hermes: connection refused")
        posted.append((board, card_id, text))

    monkeypatch.setattr(hermes, "kanban_comment", flaky)

    ok1 = review.gate_before_review(
        "b", "t_9", repo, "swarm/GR9", "integration", ["echo gate-ok"], ["src/*"], conn=conn, task_key="GR9",
    )
    ok2 = review.gate_before_review(
        "b", "t_9", repo, "swarm/GR9", "integration", ["echo gate-ok"], ["src/*"], conn=conn, task_key="GR9",
    )

    assert (ok1, ok2) == (True, True)
    assert len(posted) == 1  # the retried post succeeded, and only that one comment went to the card
    assert len(_events(conn, "gate1_record_posted")) == 1
    assert len(_events(conn, "gate_record_post_failed")) == 1


def test_check_branch_for_merge_never_posts_a_gate_record(repo, tmp_path, monkeypatch):
    """The gate-record comment is a review-lane thing (gate_before_review posts it, not check_branch itself):
    the merge queue's own, independent check must not also start posting comments no work order asked for."""
    _branch_with_changes(repo, "swarm/GR6", {"src/a.py": "x=1\n"})
    conn = db.connect(tmp_path / "ases.db")
    posted = _posted_comments(monkeypatch)

    result = review.check_branch_for_merge(
        repo, "swarm/GR6", "integration", ["echo gate-ok"], ["src/*"], conn=conn, task_key="GR6",
    )

    assert result.kind == "ok" and posted == []
