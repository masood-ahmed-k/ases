import dataclasses
import json
import re
import subprocess
import sys

import pytest

from ases import db, gates, hermes, review


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

    result = review.check_branch(
        repo, "swarm/C6", "integration", [f'"{sys.executable}" tools/noisy.py'], ["tools/*"],
        conn=conn, task_key="C6",
    )

    prefix = "Gate 1 failed on the controller's re-check:\n"
    assert result.kind == "gate1_red"
    assert result.detail.startswith(prefix)
    assert len(result.detail) == len(prefix) + 1500
    # Both ends survive (2026-09-19): the command line at the head AND the failure marker at the tail, which the
    # old "keep the first 1500 characters" cut threw away on a long red run.
    assert "...[trimmed]..." in result.detail
    assert result.detail.rstrip().endswith("[exit 1]")


@pytest.mark.parametrize("kind", ["ok", "unresolvable_branch", "no_merge_base", "out_of_scope", "gate1_red"])
def test_check_branch_never_calls_hermes(kind, repo, tmp_path, monkeypatch):
    """check_branch is the decision only: the send-back to Hermes belongs to gate_before_review."""
    branch, touches, commands = _scenario(repo, kind)
    conn = db.connect(tmp_path / "ases.db")
    _forbid_hermes(monkeypatch)

    result = review.check_branch(repo, branch, "integration", commands, touches, conn=conn, task_key="H1")

    assert result.kind == kind
    assert result.ok is (kind == "ok")


@pytest.mark.parametrize("kind", ["unresolvable_branch", "no_merge_base", "out_of_scope", "gate1_red"])
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
