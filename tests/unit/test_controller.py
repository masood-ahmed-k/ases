import subprocess

from ases import config, controller, db, hermes, plan as plan_mod, review as review_mod


def _git_ok(*args, cwd):
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result

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


def _plain_repo(tmp_path, name="repo"):
    r = tmp_path / name
    r.mkdir()
    _git_ok("init", "-q", "-b", "integration", cwd=r)
    _git_ok("config", "user.email", "t@t", cwd=r)
    _git_ok("config", "user.name", "t", cwd=r)
    (r / "README.md").write_text("hi\n", encoding="utf-8")
    _git_ok("add", "-A", cwd=r)
    _git_ok("commit", "-q", "-m", "init", cwd=r)
    return r


def test_publish_plan_commits_docs_ases_to_integration(tmp_path):
    repo = _plain_repo(tmp_path)
    (repo / "docs" / "ases").mkdir(parents=True)
    (repo / "docs" / "ases" / "plan.json").write_text("{}", encoding="utf-8")
    before = _git_ok("rev-parse", "HEAD", cwd=repo).stdout.strip()

    sha = controller.publish_plan(repo, "integration")

    after = _git_ok("rev-parse", "HEAD", cwd=repo).stdout.strip()
    assert sha == after
    assert sha != before
    log = _git_ok("log", "-1", "--format=%s", cwd=repo).stdout
    assert "Gate P" in log


def test_publish_plan_is_idempotent_on_rerun(tmp_path):
    repo = _plain_repo(tmp_path)
    (repo / "docs" / "ases").mkdir(parents=True)
    (repo / "docs" / "ases" / "plan.json").write_text("{}", encoding="utf-8")
    first = controller.publish_plan(repo, "integration")
    second = controller.publish_plan(repo, "integration")
    assert first == second  # nothing new staged the second time -- no empty commit


def test_publish_plan_rejects_wrong_branch(tmp_path):
    repo = _plain_repo(tmp_path)
    _git_ok("checkout", "-q", "-b", "not-integration", cwd=repo)
    (repo / "docs" / "ases").mkdir(parents=True)
    (repo / "docs" / "ases" / "plan.json").write_text("{}", encoding="utf-8")
    import pytest
    with pytest.raises(RuntimeError, match="integration"):
        controller.publish_plan(repo, "integration")


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


def test_create_cards_from_plan_caps_worker_card_runtime(tmp_path, monkeypatch):
    """A worker-assigned card must carry a --max-runtime, or a stuck/looping worker never stops on
    its own -- ASES had no code path setting this at all until 2026-09-18, found while chasing an
    unrelated real timeout. Defaults to 45m when budgets doesn't say (config/swarm.yaml's own default);
    honors an explicit card_runtime_minutes otherwise."""
    plan = plan_mod.parse_and_validate(PLAN_RAW, known_roles=set(ROLES), max_cards=40)
    conn = db.connect(tmp_path / "ases.db")
    counter = _FakeCounter()
    created = []

    def fake_create(board, title, **kwargs):
        card = {"id": counter.next_id("t"), "title": title, **kwargs}
        created.append(card)
        return card

    monkeypatch.setattr(hermes, "kanban_create", fake_create)
    controller.create_cards_from_plan("b", "proj1", tmp_path / "repo", plan, _project(tmp_path), conn=conn)

    t1_work = next(c for c in created if c["title"] == "T1: scaffold")
    t1_merge = next(c for c in created if c["title"] == "T1: merge")
    assert t1_work["max_runtime"] == "45m"  # _project()'s budgets={} -> falls back to the default
    assert "max_runtime" not in t1_merge  # never assigned to an agent, nothing to cap


def test_create_cards_from_plan_honors_configured_card_runtime(tmp_path, monkeypatch):
    plan = plan_mod.parse_and_validate(PLAN_RAW, known_roles=set(ROLES), max_cards=40)
    conn = db.connect(tmp_path / "ases.db")
    counter = _FakeCounter()
    created = []
    monkeypatch.setattr(
        hermes, "kanban_create",
        lambda board, title, **kw: created.append({"id": counter.next_id("t"), "title": title, **kw})
        or created[-1],
    )
    project = config.ProjectConfig(
        name="t3", environment="native", data_class="public",
        workspace_root=tmp_path / "ws", ases_home=tmp_path / "home", board="b", integration_branch="integration",
        roles=ROLES, concurrency={}, budgets={"card_runtime_minutes": 20}, hermes_tested_version="0.21.3",
        hermes_native_home=tmp_path / "hermes",
    )

    controller.create_cards_from_plan("b", "proj1", tmp_path / "repo", plan, project, conn=conn)

    t1_work = next(c for c in created if c["title"] == "T1: scaffold")
    assert t1_work["max_runtime"] == "20m"


def test_create_cards_from_plan_persists_to_db(tmp_path, monkeypatch):
    plan = plan_mod.parse_and_validate(PLAN_RAW, known_roles=set(ROLES), max_cards=40)
    conn = db.connect(tmp_path / "ases.db")
    counter = _FakeCounter()
    monkeypatch.setattr(hermes, "kanban_create", lambda board, title, **kw: {"id": counter.next_id("t"), **kw})

    controller.create_cards_from_plan("b", "proj1", tmp_path / "repo", plan, _project(tmp_path), conn=conn)

    rows = conn.execute("SELECT task_key, role FROM plan_tasks ORDER BY task_key").fetchall()
    assert [(r["task_key"], r["role"]) for r in rows] == [("T1", "coder"), ("T2", "reviewer")]


MODELS_CONFIG = {
    "providers": {
        "openrouter": {"limits": {"per_day_default": 50, "per_day_after_credits": 1000}, "credits_purchased": False},
    },
    "models": [
        {"provider": "openrouter", "model": "cohere/north-mini-code:free", "role_class": "reviewer", "pinned": True},
    ],
}


def test_process_budget_gate_parks_unaffordable_ready_card(tmp_path, monkeypatch):
    plan = plan_mod.parse_and_validate(PLAN_RAW, known_roles=set(ROLES), max_cards=40)
    conn = db.connect(tmp_path / "ases.db")
    counter = _FakeCounter()
    monkeypatch.setattr(hermes, "kanban_create", lambda board, title, **kw: {"id": counter.next_id("t"), **kw})
    pairs = controller.create_cards_from_plan("b", "proj1", tmp_path / "repo", plan, _project(tmp_path), conn=conn)

    from ases import ledger
    ledger.record_usage(conn, "openrouter", "any-model", n=50)  # exhaust the daily cap

    reviewer_work_id = pairs[1].work_card_id  # T2 is the reviewer-role task
    monkeypatch.setattr(hermes, "kanban_list", lambda b, status=None, assignee=None: (
        [{"id": reviewer_work_id, "status": "ready"}] if status == "ready" else []
    ))
    scheduled = []
    monkeypatch.setattr(hermes, "kanban_schedule", lambda b, cid, reason: scheduled.append((cid, reason)))

    parked = controller.process_budget_gate("b", plan, MODELS_CONFIG, conn=conn, budgets={})

    assert parked == ["T2"]
    assert scheduled[0][0] == reviewer_work_id


def test_process_budget_gate_leaves_affordable_cards_alone(tmp_path, monkeypatch):
    plan = plan_mod.parse_and_validate(PLAN_RAW, known_roles=set(ROLES), max_cards=40)
    conn = db.connect(tmp_path / "ases.db")
    counter = _FakeCounter()
    monkeypatch.setattr(hermes, "kanban_create", lambda board, title, **kw: {"id": counter.next_id("t"), **kw})
    pairs = controller.create_cards_from_plan("b", "proj1", tmp_path / "repo", plan, _project(tmp_path), conn=conn)

    reviewer_work_id = pairs[1].work_card_id
    monkeypatch.setattr(hermes, "kanban_list", lambda b, status=None, assignee=None: (
        [{"id": reviewer_work_id, "status": "ready"}] if status == "ready" else []
    ))
    scheduled = []
    monkeypatch.setattr(hermes, "kanban_schedule", lambda b, cid, reason: scheduled.append((cid, reason)))

    parked = controller.process_budget_gate("b", plan, MODELS_CONFIG, conn=conn, budgets={})

    assert parked == []
    assert scheduled == []


def test_process_budget_gate_ignores_another_projects_card_with_the_same_task_key(tmp_path, monkeypatch):
    """Real bug, caught before it could actually happen: a board can carry more than one project's
    cards (that's the point of Hermes projects sharing a board), and two different projects' plans
    commonly reuse generic task keys like "T1". Before scoping this lookup by project, a "ready" card
    belonging to a DIFFERENT, unrelated project -- found only because kanban_list(status="ready") lists
    the whole board -- would resolve via plan.task() against *this* plan's same-named task instead of
    its own, and could be budget-parked using the wrong task's numbers entirely."""
    plan_a_raw = {
        "project": "proj-a", "integration_branch": "integration",
        "gate_profiles": {"trivial": ["echo ok"]},
        "tasks": [{"key": "T1", "title": "review something", "role": "reviewer", "depends_on": [],
                   "touches": [], "acceptance": ["n/a"], "gate_profile": "trivial", "estimated_requests": 1}],
    }
    plan_b_raw = {
        "project": "proj-b", "integration_branch": "integration",
        "gate_profiles": {"trivial": ["echo ok"]},
        "tasks": [{"key": "T1", "title": "unrelated coder task", "role": "coder", "depends_on": [],
                   "touches": [], "acceptance": ["n/a"], "gate_profile": "trivial", "estimated_requests": 1}],
    }
    plan_a = plan_mod.parse_and_validate(plan_a_raw, known_roles=set(ROLES), max_cards=40)
    plan_b = plan_mod.parse_and_validate(plan_b_raw, known_roles=set(ROLES), max_cards=40)

    conn = db.connect(tmp_path / "ases.db")
    counter = _FakeCounter()
    monkeypatch.setattr(hermes, "kanban_create", lambda board, title, **kw: {"id": counter.next_id("t"), **kw})
    controller.create_cards_from_plan("b", "proj1", tmp_path / "repo", plan_a, _project(tmp_path), conn=conn)
    pairs_b = controller.create_cards_from_plan("b", "proj2", tmp_path / "repo", plan_b, _project(tmp_path), conn=conn)

    # The only "ready" card on the board right now belongs to plan_b, an unrelated project -- plan_a is
    # never even mentioned. Its T1 key collides with plan_a's, which is exactly the failure mode.
    other_projects_t1_work_id = pairs_b[0].work_card_id
    monkeypatch.setattr(hermes, "kanban_list", lambda b, status=None, assignee=None: (
        [{"id": other_projects_t1_work_id, "status": "ready"}] if status == "ready" else []
    ))
    scheduled = []
    monkeypatch.setattr(hermes, "kanban_schedule", lambda b, cid, reason: scheduled.append((cid, reason)))
    from ases import ledger
    ledger.record_usage(conn, "openrouter", "any-model", n=50)  # exhaust the cap plan_a's T1 (reviewer) uses

    parked = controller.process_budget_gate("b", plan_a, MODELS_CONFIG, conn=conn, budgets={})

    # Without project-scoping, plan_b's card would resolve to plan_a's T1 (role reviewer, exhausted
    # budget) and get wrongly scheduled -- a card from a project this call never mentioned.
    assert parked == []
    assert scheduled == []


def test_process_review_lane_ignores_another_projects_card_with_the_same_task_key(tmp_path, monkeypatch):
    """Same real bug as process_budget_gate's cross-project test above, in the sibling function: a
    "review" card belonging to an unrelated project with a colliding task key must never be re-gated
    using this run's plan -- it isn't part of this run at all."""
    plan_a_raw = {
        "project": "proj-a", "integration_branch": "integration",
        "gate_profiles": {"trivial": ["echo ok"]},
        "tasks": [{"key": "T1", "title": "a", "role": "coder", "depends_on": [], "touches": [],
                   "acceptance": ["n/a"], "gate_profile": "trivial", "estimated_requests": 1}],
    }
    plan_b_raw = {
        "project": "proj-b", "integration_branch": "integration",
        "gate_profiles": {"trivial": ["echo ok"]},
        "tasks": [{"key": "T1", "title": "b", "role": "coder", "depends_on": [], "touches": [],
                   "acceptance": ["n/a"], "gate_profile": "trivial", "estimated_requests": 1}],
    }
    plan_a = plan_mod.parse_and_validate(plan_a_raw, known_roles=set(ROLES), max_cards=40)
    plan_b = plan_mod.parse_and_validate(plan_b_raw, known_roles=set(ROLES), max_cards=40)

    conn = db.connect(tmp_path / "ases.db")
    counter = _FakeCounter()
    monkeypatch.setattr(hermes, "kanban_create", lambda board, title, **kw: {"id": counter.next_id("t"), **kw})
    controller.create_cards_from_plan("b", "proj1", tmp_path / "repo", plan_a, _project(tmp_path), conn=conn)
    pairs_b = controller.create_cards_from_plan("b", "proj2", tmp_path / "repo", plan_b, _project(tmp_path), conn=conn)

    other_projects_t1_work_id = pairs_b[0].work_card_id
    monkeypatch.setattr(hermes, "kanban_list", lambda b, status=None, assignee=None: (
        [{"id": other_projects_t1_work_id, "status": "review"}] if status == "review" else []
    ))
    gate_calls = []
    monkeypatch.setattr(
        review_mod, "gate_before_review",
        lambda *a, **kw: gate_calls.append((a, kw)) or True,
    )

    sent_back = controller.process_review_lane("b", tmp_path / "repo", plan_a, conn=conn)

    # plan_b's card was never even passed to the gate re-check -- it belongs to a different project.
    assert sent_back == []
    assert gate_calls == []


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


# ---------------------------------------------------------------------------------------------
# process_merge_queue: real git conflicts, fix-card creation, budget escalation.
# ---------------------------------------------------------------------------------------------

ONE_TASK_PLAN = {
    "project": "t3", "integration_branch": "integration",
    "gate_profiles": {"trivial": ["echo ok"]},
    "tasks": [
        {"key": "T1", "title": "scaffold", "role": "coder", "depends_on": [], "touches": ["base.txt"],
         "acceptance": ["exists"], "gate_profile": "trivial", "estimated_requests": 10},
    ],
}


def _repo_with_conflict(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git_ok("init", "-q", "-b", "integration", cwd=r)
    _git_ok("config", "user.email", "t@t", cwd=r)
    _git_ok("config", "user.name", "t", cwd=r)
    (r / "base.txt").write_text("base\n", encoding="utf-8")
    _git_ok("add", "-A", cwd=r)
    _git_ok("commit", "-q", "-m", "init", cwd=r)
    _git_ok("checkout", "-q", "-b", "swarm/T1-coder", cwd=r)
    (r / "base.txt").write_text("branch version\n", encoding="utf-8")
    _git_ok("commit", "-aqm", "branch edit", cwd=r)
    _git_ok("checkout", "-q", "integration", cwd=r)
    (r / "base.txt").write_text("integration version\n", encoding="utf-8")
    _git_ok("commit", "-aqm", "diverged", cwd=r)
    return r


def _setup_one_task(tmp_path, monkeypatch, fix_cards_per_task=2):
    plan = plan_mod.parse_and_validate(ONE_TASK_PLAN, known_roles=set(ROLES), max_cards=40)
    conn = db.connect(tmp_path / "ases.db")
    project = config.ProjectConfig(
        name="t3", environment="native", data_class="public",
        workspace_root=tmp_path / "ws", ases_home=tmp_path / "home", board="b",
        integration_branch="integration", roles=ROLES, concurrency={},
        budgets={"fix_cards_per_task": fix_cards_per_task}, hermes_tested_version="0.21.3",
        hermes_native_home=tmp_path / "hermes",
    )
    counter = _FakeCounter()
    created = []

    def fake_create(board, title, **kw):
        card = {"id": counter.next_id("t"), "title": title, "status": "todo", **kw}
        created.append(card)
        return card

    monkeypatch.setattr(hermes, "kanban_create", fake_create)
    pairs = controller.create_cards_from_plan("b", "proj1", tmp_path / "repo", plan, project, conn=conn)
    return plan, conn, project, pairs[0], created


def test_merge_conflict_creates_a_fix_card_not_a_block_only(tmp_path, monkeypatch):
    repo = _repo_with_conflict(tmp_path)
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)

    def fake_show(board, cid):
        if cid == pair.work_card_id:
            return {"status": "done", "branch_name": "swarm/T1-coder"}
        return {"status": "blocked"}

    monkeypatch.setattr(hermes, "kanban_show", fake_show)
    links = []
    monkeypatch.setattr(hermes, "kanban_link", lambda board, parent, child: links.append((parent, child)))
    blocked = []
    monkeypatch.setattr(hermes, "kanban_block", lambda board, cid, reason: blocked.append((cid, reason)))

    merged = controller.process_merge_queue("b", repo, plan, project, conn=conn)

    assert merged == []
    fix_cards = [c for c in created if "fix" in c["title"]]
    assert len(fix_cards) == 1
    assert fix_cards[0]["parent"] == [pair.work_card_id]
    assert fix_cards[0]["max_runtime"] == "45m"  # a fix card is worker-assigned too -- same cap applies
    assert links == [(fix_cards[0]["id"], pair.merge_card_id)]  # fix card linked as EXTRA parent of merge
    assert blocked == []  # not yet at budget -- a fix card was created instead of blocking
    row = conn.execute("SELECT fix_cards FROM plan_tasks WHERE task_key='T1'").fetchone()
    assert row["fix_cards"] == 1


def test_fix_card_budget_exhausted_escalates_to_block(tmp_path, monkeypatch):
    repo = _repo_with_conflict(tmp_path)
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch, fix_cards_per_task=0)

    monkeypatch.setattr(hermes, "kanban_show", lambda board, cid: (
        {"status": "done", "branch_name": "swarm/T1-coder"} if cid == pair.work_card_id
        else {"status": "blocked"}
    ))
    monkeypatch.setattr(hermes, "kanban_link", lambda *a: None)
    blocked = []
    monkeypatch.setattr(hermes, "kanban_block", lambda board, cid, reason: blocked.append((cid, reason)))

    controller.process_merge_queue("b", repo, plan, project, conn=conn)

    assert len(blocked) == 1
    assert blocked[0][0] == pair.merge_card_id
    assert "budget" in blocked[0][1].lower()
    fix_cards = [c for c in created if "fix" in c["title"]]
    assert fix_cards == []  # budget was 0 -- no fix card, straight to escalation


def test_successful_merge_needs_no_fix_card(tmp_path, monkeypatch):
    from ases import mergeq
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_ok("init", "-q", "-b", "integration", cwd=repo)
    _git_ok("config", "user.email", "t@t", cwd=repo)
    _git_ok("config", "user.name", "t", cwd=repo)
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git_ok("add", "-A", cwd=repo)
    _git_ok("commit", "-q", "-m", "init", cwd=repo)
    _git_ok("checkout", "-q", "-b", "swarm/T1-coder", cwd=repo)
    (repo / "new.txt").write_text("x\n", encoding="utf-8")
    _git_ok("add", "-A", cwd=repo)
    _git_ok("commit", "-q", "-m", "add file", cwd=repo)
    _git_ok("checkout", "-q", "integration", cwd=repo)

    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    monkeypatch.setattr(hermes, "kanban_show", lambda board, cid: (
        {"status": "done", "branch_name": "swarm/T1-coder"} if cid == pair.work_card_id
        else {"status": "blocked"}
    ))
    completed = []
    monkeypatch.setattr(hermes, "kanban_complete", lambda board, cid, **kw: completed.append(cid))

    merged = controller.process_merge_queue("b", repo, plan, project, conn=conn)

    assert merged == ["T1"]
    assert completed == [pair.merge_card_id]
    fix_cards = [c for c in created if "fix" in c["title"]]
    assert fix_cards == []
