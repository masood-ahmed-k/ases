from ases import db, hermes, reconcile


def _seed(conn, project="p1", task_key="T1", work="t_work", merge="t_merge"):
    conn.execute(
        "INSERT INTO plan_tasks (project, task_key, work_card_id, merge_card_id, role, touches, "
        "gate_profile, estimated_requests, created_at) VALUES (?, ?, ?, ?, 'coder', '[]', 'g', 10, "
        "datetime('now'))",
        (project, task_key, work, merge),
    )


def test_clean_state_has_no_findings(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "ases.db")
    _seed(conn)
    monkeypatch.setattr(hermes, "kanban_show", lambda b, cid: {"status": "ready"})

    assert reconcile.check("b", "p1", conn=conn) == []


def test_missing_card_is_flagged(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "ases.db")
    _seed(conn)

    def fake_show(b, cid):
        if cid == "t_work":
            raise hermes.HermesCommandError(["kanban", "show"], 1, "not found")
        return {"status": "ready"}

    monkeypatch.setattr(hermes, "kanban_show", fake_show)

    findings = reconcile.check("b", "p1", conn=conn)
    assert len(findings) == 1
    assert findings[0].kind == "missing_card"
    assert findings[0].task_key == "T1"


def test_done_merge_without_record_is_flagged(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "ases.db")
    _seed(conn)
    monkeypatch.setattr(hermes, "kanban_show", lambda b, cid: (
        {"status": "done"} if cid == "t_merge" else {"status": "done"}
    ))

    findings = reconcile.check("b", "p1", conn=conn)
    assert any(f.kind == "merge_done_without_record" for f in findings)


def test_done_merge_with_proper_record_is_clean(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "ases.db")
    _seed(conn)
    conn.execute(
        "INSERT INTO merge_records (task_key, candidate_sha, gate3_result, squash_commit, reverted, "
        "completed_at) VALUES ('T1', 'abc', 'pass', 'abc', 0, datetime('now'))"
    )
    monkeypatch.setattr(hermes, "kanban_show", lambda b, cid: (
        {"status": "done"} if cid == "t_merge" else {"status": "done"}
    ))

    assert reconcile.check("b", "p1", conn=conn) == []


def test_done_but_reverted_is_flagged(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "ases.db")
    _seed(conn)
    conn.execute(
        "INSERT INTO merge_records (task_key, candidate_sha, gate3_result, squash_commit, reverted, "
        "completed_at) VALUES ('T1', 'abc', 'pass', 'abc', 1, datetime('now'))"
    )
    monkeypatch.setattr(hermes, "kanban_show", lambda b, cid: (
        {"status": "done"} if cid == "t_merge" else {"status": "done"}
    ))

    findings = reconcile.check("b", "p1", conn=conn)
    assert any(f.kind == "done_but_reverted" for f in findings)


def test_only_checks_the_given_project(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "ases.db")
    _seed(conn, project="p1", task_key="T1")
    _seed(conn, project="p2", task_key="T1", work="other_work", merge="other_merge")

    def fake_show(b, cid):
        if cid == "other_work":
            raise AssertionError("should not check project p2's cards")
        return {"status": "ready"}

    monkeypatch.setattr(hermes, "kanban_show", fake_show)
    assert reconcile.check("b", "p1", conn=conn) == []
