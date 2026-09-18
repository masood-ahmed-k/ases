from ases import config, controller, db, hermes, plan as plan_mod

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


def _project(tmp_path):
    return config.ProjectConfig(
        name="t3", environment="native", data_class="public",
        workspace_root=tmp_path / "ws", ases_home=tmp_path / "home", board="b", integration_branch="integration",
        roles=ROLES, concurrency={}, budgets={}, hermes_tested_version="0.21.3",
        hermes_native_home=tmp_path / "hermes",
    )


class _FakeCounter:
    def __init__(self):
        self.n = 0

    def next_id(self, prefix):
        self.n += 1
        return f"{prefix}_{self.n}"


def test_create_cards_from_plan_wires_dependencies_on_merge_cards(tmp_path, monkeypatch):
    plan = plan_mod.parse_and_validate(PLAN_RAW, known_roles=set(ROLES), max_cards=40)
    conn = db.connect(tmp_path / "ases.db")
    counter = _FakeCounter()
    created = []

    def fake_create(board, title, **kwargs):
        card = {"id": counter.next_id("t"), "title": title, **kwargs}
        created.append(card)
        return card

    monkeypatch.setattr(hermes, "kanban_create", fake_create)

    pairs = controller.create_cards_from_plan(
        "b", "proj1", tmp_path / "repo", plan, _project(tmp_path), conn=conn
    )

    assert len(pairs) == 2
    t1, t2 = pairs
    # T2's work card depends on T1's MERGE card, not T1's work card (ASES-TSK-02).
    t2_work = next(c for c in created if c["title"].startswith("T2:") and "merge" not in c["title"])
    assert t2_work["parent"] == [t1.merge_card_id]
    # merge cards are created scratch, blocked, parented to their own work card.
    t1_merge = next(c for c in created if c["title"] == "T1: merge")
    assert t1_merge["parent"] == [t1.work_card_id]
    assert t1_merge["initial_status"] == "blocked"
    assert t1_merge["workspace"] == "scratch"


def test_create_cards_from_plan_assigns_correct_profile(tmp_path, monkeypatch):
    plan = plan_mod.parse_and_validate(PLAN_RAW, known_roles=set(ROLES), max_cards=40)
    conn = db.connect(tmp_path / "ases.db")
    created = []
    counter = _FakeCounter()

    def fake_create(board, title, **kwargs):
        card = {"id": counter.next_id("t"), "title": title, **kwargs}
        created.append(card)
        return card

    monkeypatch.setattr(hermes, "kanban_create", fake_create)
    controller.create_cards_from_plan("b", "proj1", tmp_path / "repo", plan, _project(tmp_path), conn=conn)

    t1_work = next(c for c in created if c["title"] == "T1: scaffold")
    t2_work = next(c for c in created if c["title"] == "T2: review scaffold")
    assert t1_work["assignee"] == "coder-1"
    assert t2_work["assignee"] == "reviewer"


def test_create_cards_from_plan_persists_to_db(tmp_path, monkeypatch):
    plan = plan_mod.parse_and_validate(PLAN_RAW, known_roles=set(ROLES), max_cards=40)
    conn = db.connect(tmp_path / "ases.db")
    counter = _FakeCounter()
    monkeypatch.setattr(hermes, "kanban_create", lambda board, title, **kw: {"id": counter.next_id("t"), **kw})

    controller.create_cards_from_plan("b", "proj1", tmp_path / "repo", plan, _project(tmp_path), conn=conn)

    rows = conn.execute("SELECT task_key, role FROM plan_tasks ORDER BY task_key").fetchall()
    assert [(r["task_key"], r["role"]) for r in rows] == [("T1", "coder"), ("T2", "reviewer")]


def test_all_merge_cards_done_false_when_one_pending(tmp_path, monkeypatch):
    plan = plan_mod.parse_and_validate(PLAN_RAW, known_roles=set(ROLES), max_cards=40)
    conn = db.connect(tmp_path / "ases.db")
    counter = _FakeCounter()
    monkeypatch.setattr(hermes, "kanban_create", lambda board, title, **kw: {"id": counter.next_id("t"), **kw})
    controller.create_cards_from_plan("b", "proj1", tmp_path / "repo", plan, _project(tmp_path), conn=conn)

    monkeypatch.setattr(hermes, "kanban_show", lambda board, cid: {"status": "ready"})
    assert controller.all_merge_cards_done("b", plan, conn=conn) is False


def test_all_merge_cards_done_true_when_all_done(tmp_path, monkeypatch):
    plan = plan_mod.parse_and_validate(PLAN_RAW, known_roles=set(ROLES), max_cards=40)
    conn = db.connect(tmp_path / "ases.db")
    counter = _FakeCounter()
    monkeypatch.setattr(hermes, "kanban_create", lambda board, title, **kw: {"id": counter.next_id("t"), **kw})
    controller.create_cards_from_plan("b", "proj1", tmp_path / "repo", plan, _project(tmp_path), conn=conn)

    monkeypatch.setattr(hermes, "kanban_show", lambda board, cid: {"status": "done"})
    assert controller.all_merge_cards_done("b", plan, conn=conn) is True
