import subprocess

import pytest

from ases import db, hermes, review


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
