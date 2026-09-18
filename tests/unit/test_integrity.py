import subprocess

import pytest

from ases import integrity


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
    (r / "base.txt").write_text("base\n", encoding="utf-8")
    _git("add", "-A", cwd=r)
    _git("commit", "-q", "-m", "init", cwd=r)
    return r


def test_changed_paths_reports_new_and_modified_files(repo):
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("x\n", encoding="utf-8")
    (repo / "base.txt").write_text("changed\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "work", cwd=repo)
    sha = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()

    changed = integrity.changed_paths(repo, sha)
    assert set(changed) == {"src/a.py", "base.txt"}


@pytest.mark.parametrize("changed,touches,expected_outside", [
    (["src/a.py", "src/b.py"], ["src/*"], []),
    (["src/a.py", "README.md"], ["src/*"], ["README.md"]),
    (["a.py"], [], ["a.py"]),                       # no declared touches -> everything is out of scope
    (["src/a.py"], ["src/**", "docs/*"], []),
    ([], ["src/*"], []),
])
def test_paths_outside_touches(changed, touches, expected_outside):
    assert integrity.paths_outside_touches(changed, touches) == expected_outside


def test_snapshot_captures_head_and_dirty_paths(repo):
    (repo / "new.txt").write_text("x\n", encoding="utf-8")
    snap = integrity.snapshot(repo)
    assert snap.head == _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    assert "new.txt" in snap.dirty_paths


def test_diff_snapshots_flags_unexpected_dirty_file(repo):
    before = integrity.snapshot(repo)
    (repo / "surprise.txt").write_text("x\n", encoding="utf-8")
    after = integrity.snapshot(repo)

    findings = integrity.diff_snapshots(before, after)
    assert any("surprise.txt" in f for f in findings)


def test_diff_snapshots_flags_moved_head(repo):
    before = integrity.snapshot(repo)
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "extra commit", cwd=repo)
    after = integrity.snapshot(repo)

    findings = integrity.diff_snapshots(before, after)
    assert any("HEAD moved" in f for f in findings)


def test_diff_snapshots_clean_when_nothing_changed(repo):
    before = integrity.snapshot(repo)
    after = integrity.snapshot(repo)
    assert integrity.diff_snapshots(before, after) == []
