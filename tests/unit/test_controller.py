import dataclasses
import subprocess

import pytest

from ases import config, controller, db, events, hermes, mergeq, plan as plan_mod, review as review_mod


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


# ---------------------------------------------------------------------------------------------
# process_merge_queue: the fix-card lifecycle and the benign fast-forward race (2026-09-19 fixes).
# These pin how process_merge_queue branches on the SHAPE of a MergeOutcome and on which card
# plan_tasks says is the task's current work card, so most of them script mergeq.merge_task instead of
# reproducing real git races. The two that are about actually merged content use a real git repo.
# Every hermes.kanban_* call a test could reach is monkeypatched: nothing here can touch a real board.
# ---------------------------------------------------------------------------------------------

REAL_HERMES_PROJECT_ID = "p_36370687"  # the shape of a real card's own project_id; NOT project.name ("t3")

_CONFLICT = mergeq.MergeOutcome(
    merged=False, candidate_sha=None, squash_commit=None, gate3_result=None,
    detail="merge conflict: CONFLICT (content): Merge conflict in base.txt",
)
_RED_GATE3 = mergeq.MergeOutcome(
    merged=False, candidate_sha="cand1", squash_commit=None, gate3_result="fail",
    detail="gate command failed: exit 1",
)
_FF_RACE = mergeq.MergeOutcome(
    merged=False, candidate_sha="cand1", squash_commit=None, gate3_result="pass",
    detail="fast-forward refused, integration branch moved: fatal: Not possible to fast-forward, aborting.",
)
_MERGED = mergeq.MergeOutcome(
    merged=True, candidate_sha="cand1", squash_commit="cand1", gate3_result="pass", detail="merged",
)


def _board_state(monkeypatch, pair, *, project_id=REAL_HERMES_PROJECT_ID):
    """A fake hermes.kanban_show backed by a dict the test can edit between passes (e.g. to move a fix
    card from "running" to "done"). project_id mirrors a real card's own project_id field, and differs
    on purpose from both _project()'s name ("t3") and the id _setup_one_task creates its cards under
    ("proj1"), so a test can tell which of the three a fix card was created under."""
    states = {
        pair.work_card_id: {"status": "done", "branch_name": "swarm/T1-coder", "project_id": project_id},
        pair.merge_card_id: {"status": "blocked"},
    }
    monkeypatch.setattr(hermes, "kanban_show", lambda board, cid: dict(states[cid]))
    return states


def _record_card_actions(monkeypatch):
    """Replace every hermes call process_merge_queue can make on the merge card with a recorder, so a
    test can assert none happened (and a bug can never fall through to a real `hermes` subprocess)."""
    actions = {"link": [], "block": [], "complete": []}
    monkeypatch.setattr(hermes, "kanban_link", lambda board, parent, child: actions["link"].append((parent, child)))
    monkeypatch.setattr(hermes, "kanban_block", lambda board, cid, reason: actions["block"].append((cid, reason)))
    monkeypatch.setattr(hermes, "kanban_complete", lambda board, cid, **kw: actions["complete"].append((cid, kw)))
    return actions


def _script_merge_task(monkeypatch, *outcomes):
    """Replace mergeq.merge_task with a fake that hands back `outcomes` in order (the last one repeats
    forever) and records each call. Reproducing a genuine fast-forward race in a test would prove
    nothing extra: the outcome's shape alone is what process_merge_queue branches on."""
    calls = []
    queue = list(outcomes)

    def fake(repo, integration_branch, work_branch, task_key, gate3_commands, *, conn=None, commit_message=None):
        calls.append({"integration_branch": integration_branch, "work_branch": work_branch, "task_key": task_key})
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(mergeq, "merge_task", fake)
    return calls


def _record_gate_calls(monkeypatch):
    """Replace review.gate_before_review with a recorder that has its REAL parameter names, so a caller
    passing the wrong arguments fails loudly instead of being swallowed by *args."""
    calls = []

    def fake(board, card_id, repo, branch, integration_branch, gate1_commands, touches, *, conn, task_key):
        calls.append({"card_id": card_id, "branch": branch, "integration_branch": integration_branch,
                      "touches": touches, "task_key": task_key})
        return True

    monkeypatch.setattr(review_mod, "gate_before_review", fake)
    return calls


def _fix_cards(created):
    return [c for c in created if "fix" in c["title"]]


def _task_row(conn):
    return conn.execute(
        "SELECT work_card_id, merge_card_id, fix_cards FROM plan_tasks WHERE task_key='T1'"
    ).fetchone()


def test_fix_card_creation_repoints_work_card_id_at_the_fix_card(tmp_path, monkeypatch):
    """Real bug (2026-09-19): a fix card was created and then forgotten. plan_tasks.work_card_id kept
    pointing at the ORIGINAL work card, and process_merge_queue derives the branch to merge from exactly
    that column, so the fix card's output could never be picked up. Real git conflict here, which also
    proves nothing else in the flow depended on the old pointer."""
    repo = _repo_with_conflict(tmp_path)
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    _board_state(monkeypatch, pair)
    _record_card_actions(monkeypatch)

    controller.process_merge_queue("b", repo, plan, project, conn=conn)

    (fix_card,) = _fix_cards(created)
    row = _task_row(conn)
    assert row["work_card_id"] == fix_card["id"]
    assert row["work_card_id"] != pair.work_card_id
    assert row["merge_card_id"] == pair.merge_card_id  # only the work card moves
    assert row["fix_cards"] == 1
    # Created BEFORE the repoint, so it is still parented to the card it replaces.
    assert fix_card["parent"] == [pair.work_card_id]


@pytest.mark.parametrize("fix_status", ["ready", "running", "review"])
def test_merge_queue_does_not_remerge_while_the_fix_card_is_in_flight(tmp_path, monkeypatch, fix_status):
    """Real bug (2026-09-19): while a fix card was still being worked, every poll re-attempted the same
    broken merge (the original work card was still 'done'), burning the fix-card budget before the fix
    had a chance to run. work_card_id now points at the fix card, so the existing 'work card is not done
    yet' check is what makes the queue wait -- no separate 'is a fix in flight' state is needed."""
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    states = _board_state(monkeypatch, pair)
    actions = _record_card_actions(monkeypatch)
    calls = _script_merge_task(monkeypatch, _CONFLICT)

    controller.process_merge_queue("b", tmp_path / "repo", plan, project, conn=conn)  # opens the fix card
    assert len(calls) == 1
    (fix_card,) = _fix_cards(created)
    states[fix_card["id"]] = {
        "status": fix_status, "branch_name": fix_card["branch"], "project_id": REAL_HERMES_PROJECT_ID,
    }

    for _ in range(3):  # several more polls while the fix card is still in flight
        assert controller.process_merge_queue("b", tmp_path / "repo", plan, project, conn=conn) == []

    assert len(calls) == 1  # merge_task was never attempted again
    assert len(_fix_cards(created)) == 1
    assert _task_row(conn)["fix_cards"] == 1  # the fix budget was not burned
    assert actions["block"] == []


def test_merge_queue_merges_the_fix_cards_own_branch_once_it_is_done(tmp_path, monkeypatch):
    """The end-to-end half of the lifecycle, real git: conflict -> fix card -> fix card done -> the merge
    queue merges the FIX branch (the original one still conflicts) and completes the merge card."""
    repo = _repo_with_conflict(tmp_path)
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    states = _board_state(monkeypatch, pair)
    actions = _record_card_actions(monkeypatch)
    assert controller.process_merge_queue("b", repo, plan, project, conn=conn) == []  # conflict -> fix card
    (fix_card,) = _fix_cards(created)

    # The fix worker's result: a branch cut from the CURRENT integration head, so it merges cleanly.
    _git_ok("checkout", "-q", "-b", fix_card["branch"], cwd=repo)
    (repo / "fixed.txt").write_text("resolved\n", encoding="utf-8")
    _git_ok("add", "-A", cwd=repo)
    _git_ok("commit", "-q", "-m", "fix: resolve the conflict", cwd=repo)
    _git_ok("checkout", "-q", "integration", cwd=repo)
    states[fix_card["id"]] = {
        "status": "done", "branch_name": fix_card["branch"], "project_id": REAL_HERMES_PROJECT_ID,
    }

    merged = controller.process_merge_queue("b", repo, plan, project, conn=conn)

    assert merged == ["T1"]
    assert [cid for cid, _ in actions["complete"]] == [pair.merge_card_id]
    assert (repo / "fixed.txt").exists()  # the fix branch's content is what landed on integration
    assert actions["block"] == []
    assert len(_fix_cards(created)) == 1  # and no second fix card was opened


def test_fix_cards_chain_and_each_merge_attempt_follows_the_current_card(tmp_path, monkeypatch):
    """Round 1's fix card is parented to the original work card, round 2's to round 1's (the chain stays
    intact in Hermes), every merge attempt is made against the CURRENT card's branch, and the fix budget
    is spent across the whole chain: the failure after round 2 escalates instead of opening fix3."""
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch, fix_cards_per_task=2)
    states = _board_state(monkeypatch, pair)
    actions = _record_card_actions(monkeypatch)
    calls = _script_merge_task(monkeypatch, _CONFLICT)  # every attempt conflicts
    repo = tmp_path / "repo"

    controller.process_merge_queue("b", repo, plan, project, conn=conn)
    fix1 = created[-1]
    states[fix1["id"]] = {"status": "done", "branch_name": fix1["branch"], "project_id": REAL_HERMES_PROJECT_ID}
    controller.process_merge_queue("b", repo, plan, project, conn=conn)
    fix2 = created[-1]
    states[fix2["id"]] = {"status": "done", "branch_name": fix2["branch"], "project_id": REAL_HERMES_PROJECT_ID}
    controller.process_merge_queue("b", repo, plan, project, conn=conn)

    assert _fix_cards(created) == [fix1, fix2]  # no third fix card
    assert fix1["parent"] == [pair.work_card_id]
    assert fix2["parent"] == [fix1["id"]]
    assert fix2["project"] == REAL_HERMES_PROJECT_ID  # read off fix1, the card being fixed in round 2
    assert [c["work_branch"] for c in calls] == ["swarm/T1-coder", "swarm/T1-fix1", "swarm/T1-fix2"]
    assert actions["link"] == [(fix1["id"], pair.merge_card_id), (fix2["id"], pair.merge_card_id)]
    assert [cid for cid, _ in actions["block"]] == [pair.merge_card_id]  # budget (2) spent -> escalated
    row = _task_row(conn)
    assert row["work_card_id"] == fix2["id"]
    assert row["fix_cards"] == 2


def test_fix_card_is_created_under_the_original_cards_real_hermes_project_id(tmp_path, monkeypatch):
    """Real bug (2026-09-19): the fix card was created with project=project.name, config/swarm.yaml's
    ASES-internal label ("ases" in production), which is not a Hermes project id at all, while every
    other card in the run carries the real one. The real id is read off the card being fixed instead."""
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    _board_state(monkeypatch, pair, project_id=REAL_HERMES_PROJECT_ID)
    _record_card_actions(monkeypatch)
    _script_merge_task(monkeypatch, _CONFLICT)

    controller.process_merge_queue("b", tmp_path / "repo", plan, project, conn=conn)

    (fix_card,) = _fix_cards(created)
    assert fix_card["project"] == REAL_HERMES_PROJECT_ID
    assert fix_card["project"] != project.name


def test_benign_fast_forward_race_retries_next_poll_without_a_fix_card(tmp_path, monkeypatch):
    """Real bug (2026-09-19): mergeq.merge_task returns merged=False with gate3_result="pass" when Gate 3
    was green but the fast-forward was refused because the integration branch moved underneath the
    candidate. That is not a failure of the branch, yet process_merge_queue treated it like one: a real
    fix card (a wasted coder turn) and one unit of fix-card budget. It now costs nothing and is simply
    retried on the next poll."""
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    _board_state(monkeypatch, pair)
    actions = _record_card_actions(monkeypatch)
    calls = _script_merge_task(monkeypatch, _FF_RACE, _MERGED)  # the first poll races, the second merges
    repo = tmp_path / "repo"

    assert controller.process_merge_queue("b", repo, plan, project, conn=conn) == []

    assert _fix_cards(created) == []
    assert actions == {"link": [], "block": [], "complete": []}  # the merge card is left exactly as it was
    row = _task_row(conn)
    assert row["fix_cards"] == 0
    assert row["work_card_id"] == pair.work_card_id  # nothing was repointed either
    kinds = [e["kind"] for e in events.recent(conn)]
    assert "merge_race_retrying" in kinds
    assert "merge_failed" not in kinds and "fix_card_created" not in kinds

    # The next poll simply tries again, against the same branch, and merges.
    assert controller.process_merge_queue("b", repo, plan, project, conn=conn) == ["T1"]
    assert [c["work_branch"] for c in calls] == ["swarm/T1-coder", "swarm/T1-coder"]
    assert [cid for cid, _ in actions["complete"]] == [pair.merge_card_id]


def test_red_gate3_still_opens_a_fix_card(tmp_path, monkeypatch):
    """Guard for the race fix above: the benign-race branch keys on gate3_result == "pass", so a RED
    Gate 3 (gate3_result == "fail") is still a genuine failure of the branch and must still open a fix
    card and spend budget. (The conflict shape, gate3_result None, is already covered by
    test_merge_conflict_creates_a_fix_card_not_a_block_only.)"""
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    _board_state(monkeypatch, pair)
    actions = _record_card_actions(monkeypatch)
    _script_merge_task(monkeypatch, _RED_GATE3)

    controller.process_merge_queue("b", tmp_path / "repo", plan, project, conn=conn)

    assert len(_fix_cards(created)) == 1
    assert _task_row(conn)["fix_cards"] == 1
    assert actions["block"] == []
    kinds = [e["kind"] for e in events.recent(conn)]
    assert "merge_failed" in kinds and "merge_race_retrying" not in kinds


def test_review_lane_finds_and_polices_a_fix_card_after_the_repoint(tmp_path, monkeypatch):
    """Real bug (2026-09-19): once a fix card reached 'review', process_review_lane looked it up by
    plan_tasks.work_card_id, which never held the fix card's id, so the lookup found nothing and Gate 1
    silently never ran on any fix card's diff."""
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    _board_state(monkeypatch, pair)
    _record_card_actions(monkeypatch)
    _script_merge_task(monkeypatch, _CONFLICT)
    controller.process_merge_queue("b", tmp_path / "repo", plan, project, conn=conn)  # opens the fix card
    (fix_card,) = _fix_cards(created)
    monkeypatch.setattr(hermes, "kanban_list", lambda b, status=None, assignee=None: (
        [{"id": fix_card["id"], "status": "review", "branch_name": fix_card["branch"]}]
        if status == "review" else []
    ))
    gate_calls = _record_gate_calls(monkeypatch)

    controller.process_review_lane("b", tmp_path / "repo", plan, conn=conn)

    assert [(c["card_id"], c["branch"], c["task_key"]) for c in gate_calls] == [
        (fix_card["id"], "swarm/T1-fix1", "T1")
    ]


def test_review_lane_passes_the_plans_own_integration_branch_to_the_gate(tmp_path, monkeypatch):
    """review.gate_before_review used to hardcode the literal "integration" for its merge-base. Its one
    caller now hands it plan.integration_branch; "main-line" here is deliberately not that literal, so a
    caller that forgot (or hardcoded it again) is caught."""
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    plan = dataclasses.replace(plan, integration_branch="main-line")
    monkeypatch.setattr(hermes, "kanban_list", lambda b, status=None, assignee=None: (
        [{"id": pair.work_card_id, "status": "review", "branch_name": "swarm/T1-coder"}]
        if status == "review" else []
    ))
    gate_calls = _record_gate_calls(monkeypatch)

    controller.process_review_lane("b", tmp_path / "repo", plan, conn=conn)

    assert [(c["card_id"], c["integration_branch"]) for c in gate_calls] == [(pair.work_card_id, "main-line")]


def test_budget_gate_now_covers_a_fix_card_too(tmp_path, monkeypatch):
    """Side effect of the repoint (2026-09-19), pinned deliberately: process_budget_gate finds a 'ready'
    card by that same work_card_id column, so a fix card, which spends the same provider's daily requests
    as any worker card but used to be invisible to the gate and would have run straight past an exhausted
    cap, is now parked like every other card."""
    from ases import ledger
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    _board_state(monkeypatch, pair)
    _record_card_actions(monkeypatch)
    _script_merge_task(monkeypatch, _CONFLICT)
    controller.process_merge_queue("b", tmp_path / "repo", plan, project, conn=conn)  # opens the fix card
    (fix_card,) = _fix_cards(created)

    ledger.record_usage(conn, "openrouter", "any-model", n=50)  # exhaust the daily cap
    monkeypatch.setattr(hermes, "kanban_list", lambda b, status=None, assignee=None: (
        [{"id": fix_card["id"], "status": "ready"}] if status == "ready" else []
    ))
    scheduled = []
    monkeypatch.setattr(hermes, "kanban_schedule", lambda b, cid, reason: scheduled.append((cid, reason)))
    coder_models_config = {
        "providers": MODELS_CONFIG["providers"],
        "models": [{"provider": "openrouter", "model": "some/coder:free", "role_class": "coder", "pinned": True}],
    }

    parked = controller.process_budget_gate("b", plan, coder_models_config, conn=conn, budgets={})

    assert parked == ["T1"]
    assert scheduled[0][0] == fix_card["id"]


# ---------------------------------------------------------------------------------------------
# gate profile pinning (ASES-QG-02): pin_gate_profiles / verify_gate_pin.
# ---------------------------------------------------------------------------------------------

def test_verify_gate_pin_noop_when_nothing_pinned_yet(tmp_path):
    conn = db.connect(tmp_path / "ases.db")
    controller.verify_gate_pin(conn, "brand-new-project", {"default": ["pytest -q"]})  # must not raise


def test_pin_then_verify_same_profiles_passes(tmp_path):
    conn = db.connect(tmp_path / "ases.db")
    profiles = {"default": ["pytest -q"]}
    controller.pin_gate_profiles(conn, "t3", profiles)
    controller.verify_gate_pin(conn, "t3", dict(profiles))  # must not raise


def test_verify_gate_pin_raises_on_changed_commands(tmp_path):
    import pytest
    conn = db.connect(tmp_path / "ases.db")
    controller.pin_gate_profiles(conn, "t3", {"default": ["pytest -q"]})
    with pytest.raises(controller.GateConfigTamperedError):
        controller.verify_gate_pin(conn, "t3", {"default": ["pytest -q", "|| true"]})


def test_pin_gate_profiles_reapprove_moves_the_pin(tmp_path):
    """A fresh `swarm approve` is the sanctioned way to change gate configuration: pinning twice with
    different content must move the pin, not leave the old one behind to conflict with it."""
    conn = db.connect(tmp_path / "ases.db")
    controller.pin_gate_profiles(conn, "t3", {"default": ["pytest -q"]})
    controller.pin_gate_profiles(conn, "t3", {"default": ["pytest -q", "--maxfail=1"]})
    controller.verify_gate_pin(conn, "t3", {"default": ["pytest -q", "--maxfail=1"]})  # must not raise


def test_verify_gate_pin_scoped_per_project(tmp_path):
    """Mirrors the cross-project isolation pattern in
    test_process_budget_gate_ignores_another_projects_card_with_the_same_task_key above: pinning
    project "a" must not affect verification for an untouched project "b" -- a lookup that forgot to
    scope by project could otherwise match "a"'s row and wrongly refuse "b"."""
    conn = db.connect(tmp_path / "ases.db")
    controller.pin_gate_profiles(conn, "a", {"default": ["pytest -q"]})
    controller.verify_gate_pin(conn, "b", {"default": ["a completely different command"]})  # must not raise
