import copy
import dataclasses
import json
import subprocess

import pytest

from ases import config, controller, db, events, hermes, mergeq, plan as plan_mod, review as review_mod
from ases import gates as gates_mod
from ases import guards as guards_mod
from ases import questions as questions_mod
from ases import sandbox as sandbox_mod
from ases import usage as usage_mod


def _git_ok(*args, cwd):
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result


@pytest.fixture(autouse=True)
def _round5_steps_are_inert(monkeypatch):
    """Loop version 2 added steps to run_pass (idle worktrees, recovery, bounds, unpark, provisioning, final gates) and
    a question channel to the merge queue (questions.open_question and ask_user). The tests in THIS file are about the
    older parts of the controller, and several of them run run_pass or the merge queue with fake boards and plans that
    have no repository, no ases_home and no Hermes behind them, so the new steps do nothing here: without this a
    forgotten stub would fall through to a real `hermes` subprocess. The new steps have tests of their own in
    test_controller_loop.py. Tests that are about a question or a step replace these stubs."""
    monkeypatch.setattr(controller, "process_idle_worktrees", lambda *a, **kw: [])
    monkeypatch.setattr(controller, "process_recovery", lambda *a, **kw: [])
    monkeypatch.setattr(controller, "process_bounds", lambda *a, **kw: (False, None))
    monkeypatch.setattr(controller, "process_unpark", lambda *a, **kw: [])
    monkeypatch.setattr(controller, "process_provision", lambda *a, **kw: [])
    monkeypatch.setattr(controller, "process_finalize", lambda *a, **kw: None)
    monkeypatch.setattr(questions_mod, "open_question", lambda card: None, raising=False)
    monkeypatch.setattr(questions_mod, "ask_user", lambda *a, **kw: "commented", raising=False)


@pytest.fixture(autouse=True)
def _empty_board_lineage_by_default(monkeypatch):
    """Round 7 (ASES-REC-03, bug 1): create_cards_from_plan's board-native fallback
    (controller._board_current_work_card) reads hermes.kanban_show and calls hermes.kanban_link whenever a
    task's plan_tasks row does not exist yet -- which is every ordinary FIRST-time card creation too (a fresh
    plan_tasks table has no row for any task yet), not only the database-deleted recovery scenario the fix
    targets, and not only in tests that call create_cards_from_plan directly: _setup_one_task and similar
    helpers below call it internally, before the test gets a chance to install its own kanban_show/kanban_link.
    An empty lineage here (no fix or retry card exists) is the correct default for a task nothing has been
    done to yet, matching every test in this file that predates round 7. A test about the board-lineage
    fallback itself, or about kanban_show/kanban_link for some other reason, replaces this with its own
    monkeypatch.setattr call afterward, which simply overrides it for that test."""
    monkeypatch.setattr(hermes, "kanban_show", lambda board, cid: {"id": cid, "_parents": []})
    monkeypatch.setattr(hermes, "kanban_link", lambda board, parent, child: None)


@pytest.fixture(autouse=True)
def _post_merge_check_passes(monkeypatch):
    """Round 6 (ASES-GIT-05): process_merge_queue re-runs Gate 3 on the new integration HEAD right after a real
    merge. Every test below that is not about it gets a stub that always passes, so a scripted MergeOutcome's
    fake SHA (such as "cand1") never has to be a real commit gates.run_gate could check out. Tests about the
    post-merge check itself replace this stub (see _stub_post_merge_gate)."""
    def lenient(repo, commit_sha, gate_name, commands, *, conn=None, task_key="", project=None,
                timeout_per_command=120, runner=None):
        return gates_mod.GateResult(gate_name, commit_sha, True, "ok")

    monkeypatch.setattr(gates_mod, "run_gate", lenient)


def _stub_post_merge_gate(monkeypatch, *, passed, detail="gate3-postmerge output"):
    """Replace gates_mod.run_gate (the post-merge check only reaches it, in this file: Gate 1 and the pre-merge
    Gate 3 are stubbed at a higher level, review.check_branch_for_merge and mergeq.merge_task) with a recorder
    that answers `passed`/`detail` and remembers every call, with its REAL keyword names."""
    calls = []

    def fake(repo, commit_sha, gate_name, commands, *, conn=None, task_key="", project=None,
              timeout_per_command=120, runner=None):
        calls.append({"commit_sha": commit_sha, "gate_name": gate_name, "commands": commands,
                      "task_key": task_key, "project": project})
        return gates_mod.GateResult(gate_name, commit_sha, passed, detail)

    monkeypatch.setattr(gates_mod, "run_gate", fake)
    return calls


ROLES = {"lead": "lead", "coder": "coder-1", "reviewer": "reviewer"}

# Hermes records one run per attempt with the profile that ran it. A work card the reviewer approved ends with
# a completed run by the reviewer profile, and only such a card may merge (ASES-GIT-03, 2026-09-19). Every
# fake "done" work card below carries this unless a test deliberately says otherwise.
REVIEWER_COMPLETED = {
    "outcome": "completed", "profile": "reviewer",
    "metadata": {"review_outcome": "approved", "reviewer_checks": ["read the diff"]},
}
CODER_COMPLETED = {"outcome": "completed", "profile": "coder-1"}

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
    links = []

    def fake_create(board, title, **kwargs):
        card = {"id": counter.next_id("t"), "title": title, **kwargs}
        created.append(card)
        return card

    monkeypatch.setattr(hermes, "kanban_create", fake_create)
    monkeypatch.setattr(hermes, "kanban_link", lambda board, parent, child: links.append((parent, child)))

    pairs = controller.create_cards_from_plan(
        "b", "proj1", tmp_path / "repo", plan, _project(tmp_path), conn=conn
    )

    assert len(pairs) == 2
    t1, t2 = pairs
    # T2's work card depends on T1's MERGE card, not T1's work card (ASES-TSK-02).
    t2_work = next(c for c in created if c["title"].startswith("T2:") and "merge" not in c["title"])
    assert t2_work["parent"] == [t1.merge_card_id]
    # merge cards are created scratch, blocked. A genuinely new task's merge card has no board history yet
    # (round 7 bug 1's _board_current_work_card sees an empty lineage), so it is created first with no
    # parent, and its own work card is wired to it with an explicit kanban_link afterward rather than a
    # parent= argument at creation time -- the same end state (the merge card's own parent is its work
    # card) either way, which is what this checks.
    t1_merge = next(c for c in created if c["title"] == "T1: merge")
    assert t1_merge["initial_status"] == "blocked"
    assert t1_merge["workspace"] == "scratch"
    assert (t1.work_card_id, t1.merge_card_id) in links


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


def _idempotent_kanban_create(monkeypatch):
    """A fake hermes.kanban_create that returns the SAME card for a repeated idempotency_key, like real
    Hermes. The plain fakes above mint a fresh id on every call, so calling create_cards_from_plan a
    second time against them is not a re-approve at all: only an idempotent fake hands back the ORIGINAL
    work card the way the real board does, which is what the re-approve tests below depend on."""
    counter = _FakeCounter()
    by_key = {}

    def fake_create(board, title, **kwargs):
        key = kwargs.get("idempotency_key")
        if key is not None and key in by_key:
            return dict(by_key[key])
        card = {"id": counter.next_id("t"), "title": title, **kwargs}
        if key is not None:
            by_key[key] = card
        return dict(card)

    monkeypatch.setattr(hermes, "kanban_create", fake_create)


def _set_plan_task(conn, plan, task_key, *, work_card_id, fix_cards):
    """Put one plan_tasks row in a chosen state: what process_merge_queue leaves behind after a failed
    merge (fix_cards=1, work_card_id repointed at the fix card), or a stale id with fix_cards=0 so that a
    refresh on re-approve is visible."""
    conn.execute(
        "UPDATE plan_tasks SET work_card_id = ?, fix_cards = ? WHERE project = ? AND task_key = ?",
        (work_card_id, fix_cards, plan.project, task_key),
    )


def _plan_task_row(conn, plan, task_key):
    return conn.execute(
        "SELECT work_card_id, merge_card_id, fix_cards FROM plan_tasks WHERE project = ? AND task_key = ?",
        (plan.project, task_key),
    ).fetchone()


class _LineageBoard:
    """A minimal board model rich enough for the round 7 bug 1 tests below (ASES-REC-03): idempotent,
    archive-aware kanban_create (a repeated idempotency_key returns the newest NON-archived match, exactly
    like ases.fakes.board.FakeHermes and, per hermes_cli/kanban_db.py, real Hermes), kanban_show with
    `_parents` and `created_at`, kanban_link and kanban_archive. Everything else about a real board (workers,
    dispatch, runs, ...) is irrelevant to create_cards_from_plan and is not modelled. created_at is a plain
    counter, not a wall clock: only its ORDER matters to _board_current_work_card, per its own docstring."""

    def __init__(self):
        self.cards: dict[str, dict] = {}
        self.links: list[tuple[str, str]] = []
        self._seq = 0

    def create(self, board, title, **kwargs):
        key = kwargs.get("idempotency_key")
        if key is not None:
            matches = [c for c in self.cards.values()
                       if c.get("idempotency_key") == key and c["status"] != "archived"]
            if matches:
                return dict(matches[-1])
        self._seq += 1
        card = {
            "id": f"c{self._seq}", "title": title, "status": kwargs.get("initial_status") or "ready",
            "created_at": self._seq, "idempotency_key": key,
        }
        self.cards[card["id"]] = card
        for parent in kwargs.get("parent") or []:
            self.links.append((parent, card["id"]))
        return dict(card)

    def show(self, board, card_id):
        card = dict(self.cards[card_id])
        card["_parents"] = [p for p, c in self.links if c == card_id]
        return card

    def link(self, board, parent_id, child_id):
        self.links.append((parent_id, child_id))

    def archive(self, board, card_ids):
        for card_id in card_ids:
            self.cards[card_id]["status"] = "archived"


def _stub_lineage_board(monkeypatch):
    fake = _LineageBoard()
    monkeypatch.setattr(hermes, "kanban_create", fake.create)
    monkeypatch.setattr(hermes, "kanban_show", fake.show)
    monkeypatch.setattr(hermes, "kanban_link", fake.link)
    monkeypatch.setattr(hermes, "kanban_archive", fake.archive)
    return fake


def test_board_current_work_card_reads_an_empty_lineage_as_a_genuinely_new_task(tmp_path, monkeypatch):
    fake = _stub_lineage_board(monkeypatch)
    plan = plan_mod.parse_and_validate(PLAN_RAW, known_roles=set(ROLES), max_cards=40)
    conn = db.connect(tmp_path / "ases.db")

    current_id, merge, fix_cards_seen = controller._board_current_work_card("b", "proj1", plan, "T1", conn=conn)

    assert current_id is None and fix_cards_seen == 0
    assert merge["title"] == "T1: merge" and merge["status"] == "blocked"
    assert fake.show("b", merge["id"])["_parents"] == []


def test_board_current_work_card_prefers_the_newest_non_archived_lineage_member(tmp_path, monkeypatch):
    """The scenario builder-findings.md's AC-G report names: a fix card (never archived) and, layered on top
    of it, a retry card that archives the fix card. created_at order (not id, not link order: see the
    function's own docstring for why) must pick the retry card, and fix_cards_seen must count the fix card
    even though it is no longer current."""
    fake = _stub_lineage_board(monkeypatch)
    plan = plan_mod.parse_and_validate(PLAN_RAW, known_roles=set(ROLES), max_cards=40)
    conn = db.connect(tmp_path / "ases.db")

    original = fake.create("b", "T1: scaffold", idempotency_key="ases-work-t3-T1")
    merge = fake.create("b", "T1: merge", initial_status="blocked", idempotency_key="ases-merge-t3-T1")
    fake.link("b", original["id"], merge["id"])
    fix1 = fake.create("b", "T1: fix (round 1)", idempotency_key="ases-fix-t3-T1-1")
    fake.link("b", fix1["id"], merge["id"])
    retry1 = fake.create("b", "T1: retry 1", idempotency_key="ases-retry-t3-T1-1")
    fake.link("b", retry1["id"], merge["id"])
    fake.archive("b", [fix1["id"]])   # _start_fresh_attempt archives the card it replaces

    current_id, merge_again, fix_cards_seen = controller._board_current_work_card(
        "b", "proj1", plan, "T1", conn=conn)

    assert current_id == retry1["id"]
    assert merge_again["id"] == merge["id"]
    assert fix_cards_seen == 1


def test_reapprove_after_the_database_is_deleted_finds_a_fix_card_not_the_stale_original(tmp_path, monkeypatch):
    """Round 7 bug 1, the first of the two cases builder-findings.md's AC-G report names (search
    "ASES-PRV-01"... no, search "AC-G" for bug 1's origin per r7_wp_fixes.md): once the ASES database is
    deleted (test 22.15's own scenario, "create twice ... and once more after deleting the ASES database"),
    the plain "no existing row" path used to call kanban_create with the ORIGINAL work card's idempotency
    key. Hermes's idempotency lookup happily finds it (a fix never archives the card it replaces), so
    plan_tasks silently reverted to the STALE, superseded original -- no duplicate card, but wrong
    bookkeeping, exactly test_22_15_a_fix_card_is_forgotten_after_the_database_is_deleted's own finding.
    _board_current_work_card now asks the board instead."""
    fake = _stub_lineage_board(monkeypatch)
    plan = plan_mod.parse_and_validate(PLAN_RAW, known_roles=set(ROLES), max_cards=40)
    conn = db.connect(tmp_path / "ases.db")

    first = controller.create_cards_from_plan("b", "proj1", tmp_path / "repo", plan, _project(tmp_path), conn=conn)
    t1 = next(p for p in first if p.task_key == "T1")

    # Reproduce exactly what process_merge_queue's _handle_merge_failure does on a failed merge: a fix card,
    # linked as an EXTRA parent of the merge card, plan_tasks repointed (the original is NEVER archived).
    fix_card = fake.create(
        "b", "T1: fix (round 1)", assignee="coder-1", workspace="worktree", branch="swarm/T1-fix1",
        project="proj1", body="fix it", parent=[t1.work_card_id],
        idempotency_key="ases-fix-t3-T1-1", max_retries=3, max_runtime="45m",
    )
    fake.link("b", fix_card["id"], t1.merge_card_id)
    conn.execute(
        "UPDATE plan_tasks SET fix_cards = fix_cards + 1, work_card_id = ? WHERE project = ? AND task_key = ?",
        (fix_card["id"], plan.project, "T1"),
    )
    card_count_before = len(fake.cards)

    conn.execute("DELETE FROM plan_tasks")   # what deleting the ASES database (test 22.15's scenario) leaves

    third = controller.create_cards_from_plan("b", "proj1", tmp_path / "repo", plan, _project(tmp_path), conn=conn)

    assert len(fake.cards) == card_count_before   # the board still has each card exactly once: no duplicate
    t1_third = next(p for p in third if p.task_key == "T1")
    assert t1_third.work_card_id == fix_card["id"]        # the RIGHT card: the fix card, not the stale original
    assert t1_third.work_card_id != t1.work_card_id
    assert t1_third.merge_card_id == t1.merge_card_id
    row = _plan_task_row(conn, plan, "T1")
    assert row["work_card_id"] == fix_card["id"]
    assert row["fix_cards"] == 1   # the budget counter is recovered from the board too, not reset to 0


def test_reapprove_after_the_database_is_deleted_finds_a_retry_card_with_no_duplicate(tmp_path, monkeypatch):
    """Round 7 bug 1's second, more serious case (builder-findings.md's AC-G report: "not directly exercised
    by round 6's acceptance suite, only implied by reading the same code path"; r7_wp_fixes.md asks for a
    test of this case specifically). _start_fresh_attempt ARCHIVES the card it replaces, so once the local
    signals are silent, calling kanban_create with the ORIGINAL idempotency key no longer finds it (Hermes
    ignores an archived card by key) and used to create a genuine SECOND, duplicate work card for the same
    task. _board_current_work_card must ask the board BEFORE that call, not after: once a duplicate exists
    there is no way to undo it."""
    fake = _stub_lineage_board(monkeypatch)
    plan = plan_mod.parse_and_validate(PLAN_RAW, known_roles=set(ROLES), max_cards=40)
    conn = db.connect(tmp_path / "ases.db")

    first = controller.create_cards_from_plan("b", "proj1", tmp_path / "repo", plan, _project(tmp_path), conn=conn)
    t1 = next(p for p in first if p.task_key == "T1")

    # Reproduce exactly what _start_fresh_attempt does on a capability failure: a retry card, linked as an
    # EXTRA parent of the merge card, the OLD card archived, plan_tasks repointed.
    retry_card = fake.create(
        "b", "T1: retry 1", assignee="coder-1", workspace="worktree", branch="swarm/T1-retry1",
        project="proj1", body="try again", parent=[],
        idempotency_key="ases-retry-t3-T1-1", max_retries=3, max_runtime="45m",
    )
    fake.link("b", retry_card["id"], t1.merge_card_id)
    fake.archive("b", [t1.work_card_id])
    events.record(conn, "retry_card_created", {
        "project": plan.project, "task_key": "T1", "old_card": t1.work_card_id, "new_card": retry_card["id"], "n": 1,
    })
    conn.execute(
        "UPDATE plan_tasks SET work_card_id = ? WHERE project = ? AND task_key = ?",
        (retry_card["id"], plan.project, "T1"),
    )
    card_count_before = len(fake.cards)

    conn.execute("DELETE FROM plan_tasks")

    third = controller.create_cards_from_plan("b", "proj1", tmp_path / "repo", plan, _project(tmp_path), conn=conn)

    # The real bug: the board used to gain a genuine second work card for T1 here.
    assert len(fake.cards) == card_count_before
    t1_third = next(p for p in third if p.task_key == "T1")
    assert t1_third.work_card_id == retry_card["id"]
    assert t1_third.work_card_id != t1.work_card_id
    assert fake.cards[t1.work_card_id]["status"] == "archived"   # still archived, never resurrected
    row = _plan_task_row(conn, plan, "T1")
    assert row["work_card_id"] == retry_card["id"]


def test_reapprove_keeps_the_fix_card_repoint(tmp_path, monkeypatch):
    """Real bug (2026-09-19): process_merge_queue repoints plan_tasks.work_card_id at a fix card, so the
    column means the task's CURRENT card. A re-approve (also how gate configuration is changed,
    ASES-QG-02) calls create_cards_from_plan again; kanban_create is idempotent by key, so it hands back
    the ORIGINAL work card, and the upsert used to reset work_card_id to it while fix_cards kept its
    count. The live fix card was forgotten: the merge queue went back to merging the original,
    still-broken branch, and the review and budget lanes stopped seeing the fix card."""
    plan = plan_mod.parse_and_validate(PLAN_RAW, known_roles=set(ROLES), max_cards=40)
    conn = db.connect(tmp_path / "ases.db")
    _idempotent_kanban_create(monkeypatch)
    first = controller.create_cards_from_plan("b", "proj1", tmp_path / "repo", plan, _project(tmp_path), conn=conn)
    t1 = next(p for p in first if p.task_key == "T1")
    _set_plan_task(conn, plan, "T1", work_card_id="fix_card_1", fix_cards=1)  # a fix card was opened

    again = controller.create_cards_from_plan("b", "proj1", tmp_path / "repo", plan, _project(tmp_path), conn=conn)

    assert again == first  # premise: the re-approve got the ORIGINAL cards back, not fresh ones
    row = _plan_task_row(conn, plan, "T1")
    assert row["work_card_id"] == "fix_card_1"
    assert row["work_card_id"] != t1.work_card_id  # not reset to the original work card
    assert row["fix_cards"] == 1
    assert row["merge_card_id"] == t1.merge_card_id  # the merge card never moves, and is still the right one


def test_reapprove_with_no_fix_cards_still_refreshes_work_card_id(tmp_path, monkeypatch):
    """Guard against over-correcting the fix above: the column is only held once a fix card has been
    spent. A task that never needed one has its work_card_id refreshed to the (idempotent) work card on
    a re-approve, exactly as before, so a stale id is still healed."""
    plan = plan_mod.parse_and_validate(PLAN_RAW, known_roles=set(ROLES), max_cards=40)
    conn = db.connect(tmp_path / "ases.db")
    _idempotent_kanban_create(monkeypatch)
    first = controller.create_cards_from_plan("b", "proj1", tmp_path / "repo", plan, _project(tmp_path), conn=conn)
    t1 = next(p for p in first if p.task_key == "T1")
    _set_plan_task(conn, plan, "T1", work_card_id="stale_card", fix_cards=0)

    controller.create_cards_from_plan("b", "proj1", tmp_path / "repo", plan, _project(tmp_path), conn=conn)

    row = _plan_task_row(conn, plan, "T1")
    assert row["work_card_id"] == t1.work_card_id  # the stale id was reset to the idempotent work card
    assert row["fix_cards"] == 0
    assert row["merge_card_id"] == t1.merge_card_id


def test_reapprove_guard_is_per_task(tmp_path, monkeypatch):
    """The hold keys on each task's OWN fix_cards, not on the plan having had a fix anywhere: T1 spent a
    fix card and keeps its repoint, while T2 (never fixed, and given a stale id so that a refresh is
    visible) is refreshed normally by the very same re-approve."""
    plan = plan_mod.parse_and_validate(PLAN_RAW, known_roles=set(ROLES), max_cards=40)
    conn = db.connect(tmp_path / "ases.db")
    _idempotent_kanban_create(monkeypatch)
    first = {
        p.task_key: p
        for p in controller.create_cards_from_plan("b", "proj1", tmp_path / "repo", plan, _project(tmp_path), conn=conn)
    }
    _set_plan_task(conn, plan, "T1", work_card_id="fix_card_1", fix_cards=1)
    _set_plan_task(conn, plan, "T2", work_card_id="stale_card", fix_cards=0)

    controller.create_cards_from_plan("b", "proj1", tmp_path / "repo", plan, _project(tmp_path), conn=conn)

    t1_row = _plan_task_row(conn, plan, "T1")
    t2_row = _plan_task_row(conn, plan, "T2")
    assert (t1_row["work_card_id"], t1_row["fix_cards"]) == ("fix_card_1", 1)  # held: T1 spent a fix card
    assert (t2_row["work_card_id"], t2_row["fix_cards"]) == (first["T2"].work_card_id, 0)  # T2 never did
    assert t1_row["merge_card_id"] == first["T1"].merge_card_id
    assert t2_row["merge_card_id"] == first["T2"].merge_card_id


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


def _one_task_plan_raw(role):
    """ONE_TASK_PLAN with its single task given `role` (a review-only task is any role but "coder")."""
    raw = copy.deepcopy(ONE_TASK_PLAN)
    raw["tasks"][0]["role"] = role
    return raw


def _setup_one_task(tmp_path, monkeypatch, fix_cards_per_task=2, plan_raw=ONE_TASK_PLAN, roles=ROLES):
    # known_roles from the same `roles` the project itself gets (round 7 part C fix: this used to read the
    # module-level ROLES unconditionally, so a caller's own `roles=` override was silently ignored by Gate 0
    # while still taking effect on the ProjectConfig below -- never noticed before because no caller passed a
    # role Gate 0 did not already know about).
    plan = plan_mod.parse_and_validate(plan_raw, known_roles=set(roles), max_cards=40)
    conn = db.connect(tmp_path / "ases.db")
    project = config.ProjectConfig(
        name="t3", environment="native", data_class="public",
        workspace_root=tmp_path / "ws", ases_home=tmp_path / "home", board="b",
        integration_branch="integration", roles=roles, concurrency={},
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
            return {"id": cid, "status": "done", "branch_name": "swarm/T1-coder", "_runs": [REVIEWER_COMPLETED]}
        return {"id": cid, "status": "blocked"}

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
    """The escalation is a question put through questions.ask_user (a merge card is created blocked, and real Hermes
    refuses to block a blocked card), never a direct hermes.kanban_block. (Kept under its old name: it used to assert
    on hermes.kanban_block itself.)"""
    repo = _repo_with_conflict(tmp_path)
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch, fix_cards_per_task=0)

    monkeypatch.setattr(hermes, "kanban_show", lambda board, cid: (
        {"id": cid, "status": "done", "branch_name": "swarm/T1-coder", "_runs": [REVIEWER_COMPLETED]}
        if cid == pair.work_card_id else {"id": cid, "status": "blocked"}
    ))
    monkeypatch.setattr(hermes, "kanban_link", lambda *a: None)
    blocked = []
    monkeypatch.setattr(hermes, "kanban_block", lambda *a, **kw: blocked.append(a))
    asked = []
    monkeypatch.setattr(questions_mod, "ask_user", lambda board, card, text, *, conn=None, author="ases": (
        asked.append((card["id"], text)) or "commented"))

    controller.process_merge_queue("b", repo, plan, project, conn=conn)

    assert blocked == []  # kanban_block is never called on a merge card
    assert len(asked) == 1
    assert asked[0][0] == pair.merge_card_id
    assert "budget" in asked[0][1].lower()
    assert asked[0][1].rstrip().endswith("How should this be resolved?")
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
        {"id": cid, "status": "done", "branch_name": "swarm/T1-coder", "_runs": [REVIEWER_COMPLETED]}
        if cid == pair.work_card_id else {"id": cid, "status": "blocked"}
    ))
    completed = []
    monkeypatch.setattr(hermes, "kanban_complete", lambda board, cid, **kw: completed.append(cid))

    merged = controller.process_merge_queue("b", repo, plan, project, conn=conn)

    assert merged == ["T1"]
    assert completed == [pair.merge_card_id]
    fix_cards = [c for c in created if "fix" in c["title"]]
    assert fix_cards == []


def _repo_with_one_commit_on_a_work_branch(tmp_path):
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
    return repo


def _one_coder_task_ready_to_merge(tmp_path, monkeypatch, repo):
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    monkeypatch.setattr(hermes, "kanban_show", lambda board, cid: (
        {"id": cid, "status": "done", "branch_name": "swarm/T1-coder", "_runs": [REVIEWER_COMPLETED]}
        if cid == pair.work_card_id else {"id": cid, "status": "blocked"}
    ))
    monkeypatch.setattr(hermes, "kanban_complete", lambda board, cid, **kw: None)
    return plan, conn, project, pair, created


# --- round 9 (ASES-QG-04, ASES-SEC-03): an infrastructure failure at any of the three merge-queue gate calls is
# recorded and holds the merge, never a red gate and never a silently completed merge on the host instead --------


def test_merge_queue_pre_merge_check_branch_for_merge_infrastructure_failure_holds_the_merge(tmp_path, monkeypatch):
    repo = _repo_with_one_commit_on_a_work_branch(tmp_path)
    plan, conn, project, pair, created = _one_coder_task_ready_to_merge(tmp_path, monkeypatch, repo)
    merge_task_calls = []
    monkeypatch.setattr(mergeq, "merge_task", lambda *a, **kw: merge_task_calls.append(1) or _CONFLICT)

    def boom(*a, **kw):
        raise sandbox_mod.SandboxInfrastructureError("the sandbox is enabled but docker CLI not found on PATH")

    monkeypatch.setattr(review_mod, "check_branch_for_merge", boom)

    merged = controller.process_merge_queue("b", repo, plan, project, conn=conn)

    assert merged == []
    assert merge_task_calls == []  # never reached: nothing was built and nothing needs undoing
    rows = _refusals(conn, "sandbox_infrastructure_error")
    assert len(rows) == 1 and rows[0] == {
        "task_key": "T1", "card_id": pair.work_card_id, "gate": "gate1",
        "error": "the sandbox is enabled but docker CLI not found on PATH",
    }
    assert _refusals(conn, "merge_failed") == []
    fix_cards = [c for c in created if "fix" in c["title"]]
    assert fix_cards == []


def test_merge_queue_gate3_candidate_infrastructure_failure_holds_the_merge(tmp_path, monkeypatch):
    repo = _repo_with_one_commit_on_a_work_branch(tmp_path)
    plan, conn, project, pair, created = _one_coder_task_ready_to_merge(tmp_path, monkeypatch, repo)
    before = _git_ok("rev-parse", "integration", cwd=repo).stdout.strip()

    def boom(*a, **kw):
        raise sandbox_mod.SandboxInfrastructureError("the sandbox is enabled but image x is not present locally")

    monkeypatch.setattr(mergeq, "merge_task", boom)

    merged = controller.process_merge_queue("b", repo, plan, project, conn=conn)

    assert merged == []
    rows = _refusals(conn, "sandbox_infrastructure_error")
    assert len(rows) == 1 and rows[0] == {
        "task_key": "T1", "card_id": pair.merge_card_id, "gate": "gate3",
        "error": "the sandbox is enabled but image x is not present locally",
    }
    assert _refusals(conn, "merge_failed") == []
    fix_cards = [c for c in created if "fix" in c["title"]]
    assert fix_cards == []
    # the integration branch itself was never touched
    assert _git_ok("rev-parse", "integration", cwd=repo).stdout.strip() == before


def test_merge_queue_post_merge_infrastructure_failure_still_completes_the_merge(tmp_path, monkeypatch):
    """The fast-forward already landed on the integration branch by the time the post-merge re-check runs: an
    infra failure there must not trigger a revert (nothing is actually known to be wrong) and must not leave the
    merge card open either (a repeat pass would re-run merge_task on content already merged, which mergeq itself
    then correctly refuses as "nothing to commit", opening a spurious fix card for a task that succeeded)."""
    repo = _repo_with_one_commit_on_a_work_branch(tmp_path)
    plan, conn, project, pair, created = _one_coder_task_ready_to_merge(tmp_path, monkeypatch, repo)
    completed = []
    monkeypatch.setattr(hermes, "kanban_complete", lambda board, cid, **kw: completed.append(cid))
    reverts = []
    monkeypatch.setattr(mergeq, "revert_merge", lambda *a, **kw: reverts.append(1))
    real_run_gate = gates_mod.run_gate

    def boom_on_postmerge_only(repo_arg, sha, gate_name, commands, **kwargs):
        # The Gate 3 CANDIDATE run (inside mergeq.merge_task, which also calls gates_mod.run_gate) must still
        # succeed for real, so the fast-forward actually happens and this test reaches the post-merge re-check
        # it means to exercise; only "gate3-postmerge" fails to start.
        if gate_name == "gate3-postmerge":
            raise sandbox_mod.SandboxInfrastructureError("the sandbox is enabled but docker CLI not found on PATH")
        return real_run_gate(repo_arg, sha, gate_name, commands, **kwargs)

    monkeypatch.setattr(gates_mod, "run_gate", boom_on_postmerge_only)

    merged = controller.process_merge_queue("b", repo, plan, project, conn=conn)

    assert merged == ["T1"]
    assert completed == [pair.merge_card_id]
    assert reverts == []
    rows = _refusals(conn, "sandbox_infrastructure_error")
    assert len(rows) == 1
    assert rows[0]["task_key"] == "T1" and rows[0]["gate"] == "gate3-postmerge"
    assert _refusals(conn, "post_merge_reverted") == []
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
    integration_moved=True,
)
# Same gate3_result="pass" shape as _FF_RACE, but mergeq found the integration tip had NOT moved (the real
# text git prints for a dirty primary checkout), so the free retry must not apply (2026-09-19 fix).
_FF_REFUSED_NOT_A_RACE = mergeq.MergeOutcome(
    merged=False, candidate_sha="cand1", squash_commit=None, gate3_result="pass",
    detail=("fast-forward refused although the integration branch did NOT move (still 0123456789ab); this is "
            "not a race, a dirty or wrong-branch primary checkout is the likely cause: error: Your local "
            "changes to the following files would be overwritten by merge:\n\tbase.txt\nAborting"),
    integration_moved=False,
)
_MERGED = mergeq.MergeOutcome(
    merged=True, candidate_sha="cand1", squash_commit="cand1", gate3_result="pass", detail="merged",
)


def _board_state(monkeypatch, pair, *, project_id=REAL_HERMES_PROJECT_ID, branch="swarm/T1-coder"):
    """A fake hermes.kanban_show backed by a dict the test can edit between passes (e.g. to move a fix
    card from "running" to "done"). project_id mirrors a real card's own project_id field, and differs
    on purpose from both _project()'s name ("t3") and the id _setup_one_task creates its cards under
    ("proj1"), so a test can tell which of the three a fix card was created under. Like the real
    kanban_show, every card it returns carries its own "id" (the squash commit message names it), so an
    entry a test adds later needs no id of its own. `branch` is what the work card reports as its branch.
    A "done" card carries a reviewer-completed run unless its entry sets its own "_runs" (a test that wants a
    card its implementer finished itself, or one nobody finished, says so explicitly)."""
    states = {
        pair.work_card_id: {"status": "done", "branch_name": branch, "project_id": project_id},
        pair.merge_card_id: {"status": "blocked"},
    }

    def show(board, cid):
        card = {"id": cid, **states[cid]}
        if card["status"] == "done":
            card.setdefault("_runs", [dict(REVIEWER_COMPLETED)])
        return card

    monkeypatch.setattr(hermes, "kanban_show", show)
    return states


def _record_card_actions(monkeypatch):
    """Replace every hermes call process_merge_queue can make on a card with a recorder, so a
    test can assert none happened (and a bug can never fall through to a real `hermes` subprocess)."""
    actions = {"link": [], "block": [], "complete": [], "ask": [], "reopen_review": []}
    monkeypatch.setattr(hermes, "kanban_link", lambda board, parent, child: actions["link"].append((parent, child)))
    monkeypatch.setattr(hermes, "kanban_block", lambda board, cid, reason: actions["block"].append((cid, reason)))
    monkeypatch.setattr(hermes, "kanban_complete", lambda board, cid, **kw: actions["complete"].append((cid, kw)))
    monkeypatch.setattr(hermes, "kanban_reopen_review", lambda board, cid, reason: (
        actions["reopen_review"].append((cid, reason))))
    # The controller puts every question to a person through questions.ask_user; "ask" is where those land.
    monkeypatch.setattr(questions_mod, "ask_user", lambda board, card, text, *, conn=None, author="ases": (
        actions["ask"].append((card["id"], text)) or "commented"), raising=False)
    return actions


def _script_merge_task(monkeypatch, *outcomes):
    """Replace mergeq.merge_task with a fake that hands back `outcomes` in order (the last one repeats
    forever) and records each call. Reproducing a genuine fast-forward race in a test would prove
    nothing extra: the outcome's shape alone is what process_merge_queue branches on."""
    calls = []
    queue = list(outcomes)

    def fake(repo, integration_branch, work_branch, task_key, gate3_commands, *, conn=None, commit_message=None,
             allow_empty=False, expected_head=None, project=None, should_stop=None, project_config=None, task=None):
        calls.append({"integration_branch": integration_branch, "work_branch": work_branch, "task_key": task_key,
                      "commit_message": commit_message, "allow_empty": allow_empty,
                      "expected_head": expected_head, "project": project, "should_stop": should_stop})
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(mergeq, "merge_task", fake)
    return calls


def _record_gate_calls(monkeypatch):
    """Replace review.gate_before_review with a recorder that has its REAL parameter names, so a caller
    passing the wrong arguments fails loudly instead of being swallowed by *args."""
    calls = []

    def fake(
        board, card_id, repo, branch, integration_branch, gate1_commands, touches, *, conn, task_key,
        allow_gate_config_changes=False, project_config=None, task=None,
    ):
        calls.append({"card_id": card_id, "branch": branch, "integration_branch": integration_branch,
                      "touches": touches, "task_key": task_key,
                      "allow_gate_config_changes": allow_gate_config_changes})
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
    # The squash commit of a repointed task names the CURRENT card (the fix card) and its branch, while the
    # merge card is the task's own, which never moves.
    message = _git_ok("log", "-1", "--format=%B", "integration", cwd=repo).stdout
    assert f"Work card: {fix_card['id']}\n" in message and f"Merge card: {pair.merge_card_id}\n" in message
    assert f"Branch: {fix_card['branch']}\n" in message and f"Work card: {pair.work_card_id}\n" not in message


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
    assert [cid for cid, _ in actions["ask"]] == [pair.merge_card_id]  # budget (2) spent -> a question, once
    assert actions["block"] == []  # ...put through ask_user, never kanban_block on the merge card
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
    """Real bug (2026-09-19): mergeq.merge_task returns merged=False with gate3_result="pass" and
    integration_moved=True when Gate 3 was green but the fast-forward was refused because the integration
    branch verifiably moved underneath the candidate. That is not a failure of the branch, yet
    process_merge_queue treated it like one: a real fix card (a wasted coder turn) and one unit of
    fix-card budget. It now costs nothing and is simply retried on the next poll. (The free retry keys on
    integration_moved, not gate3_result; the tests below pin what happens when it is False.)"""
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    _board_state(monkeypatch, pair)
    actions = _record_card_actions(monkeypatch)
    calls = _script_merge_task(monkeypatch, _FF_RACE, _MERGED)  # the first poll races, the second merges
    repo = tmp_path / "repo"

    assert controller.process_merge_queue("b", repo, plan, project, conn=conn) == []

    assert _fix_cards(created) == []
    assert actions == {"link": [], "block": [], "complete": [], "ask": [], "reopen_review": []}  # the merge card is left exactly as it was
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
    """Guard for the race fix above: the benign-race branch keys on integration_moved, which only a
    Gate-3-green outcome whose fast-forward was refused can carry, so a RED Gate 3 (gate3_result ==
    "fail") is still a genuine failure of the branch and must still open a fix card and spend budget.
    (The conflict shape, gate3_result None, is already covered by
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


def test_ff_refusal_with_the_integration_tip_unmoved_is_a_real_failure_not_a_free_retry(tmp_path, monkeypatch):
    """Real bug (2026-09-19), the flip side of the race test above. The free retry used to key on
    gate3_result == "pass" alone, but git also refuses a fast-forward while the integration tip has NOT
    moved (a dirty primary checkout, a wrong-branch checkout, an index.lock). Those are refused again on
    every poll, so they retried silently, bounded only by --max-iterations, instead of surfacing. mergeq
    now reports whether the tip verifiably moved; with integration_moved False, a Gate-3-green refusal is
    an ordinary failure again: a merge_failed event, a fix card, one unit of fix budget spent."""
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    _board_state(monkeypatch, pair)
    actions = _record_card_actions(monkeypatch)
    _script_merge_task(monkeypatch, _FF_REFUSED_NOT_A_RACE)  # gate3_result == "pass", integration_moved False

    assert controller.process_merge_queue("b", tmp_path / "repo", plan, project, conn=conn) == []

    (fix_card,) = _fix_cards(created)
    row = _task_row(conn)
    assert row["fix_cards"] == 1  # budget spent, exactly like any other failure
    assert row["work_card_id"] == fix_card["id"]
    assert actions["link"] == [(fix_card["id"], pair.merge_card_id)]
    assert actions["block"] == []  # the first failure opens a fix card; a block comes once the budget is spent
    recent = events.recent(conn)
    kinds = [e["kind"] for e in recent]
    assert "merge_failed" in kinds and "fix_card_created" in kinds
    assert "merge_race_retrying" not in kinds
    failed = next(e for e in recent if e["kind"] == "merge_failed")
    assert "did NOT move" in failed["payload"]  # git's own diagnosis is what surfaces, in the event...
    assert "did NOT move" in fix_card["body"]  # ...and in the fix card's body


def test_ff_refusal_with_the_integration_tip_unmoved_blocks_once_the_fix_budget_is_spent(tmp_path, monkeypatch):
    """The end of that same path: with no fix budget left, a refusal that is not a race escalates to a
    block carrying git's own text for a human, rather than being retried for free."""
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch, fix_cards_per_task=0)
    _board_state(monkeypatch, pair)
    actions = _record_card_actions(monkeypatch)
    _script_merge_task(monkeypatch, _FF_REFUSED_NOT_A_RACE)

    assert controller.process_merge_queue("b", tmp_path / "repo", plan, project, conn=conn) == []

    assert _fix_cards(created) == []
    ((blocked_id, reason),) = actions["ask"]
    assert actions["block"] == []
    assert blocked_id == pair.merge_card_id
    assert "budget" in reason.lower() and "did NOT move" in reason
    kinds = [e["kind"] for e in events.recent(conn)]
    assert "merge_failed" in kinds and "fix_card_budget_exhausted" in kinds
    assert "merge_race_retrying" not in kinds


def test_dirty_primary_checkout_is_a_real_failure_end_to_end_not_a_silent_retry(tmp_path, monkeypatch):
    """Real git and the real mergeq.merge_task, nothing scripted: an uncommitted edit in the primary
    checkout to a file the merge changes makes git refuse the fast-forward with the integration tip
    unmoved. That used to be swallowed as a 'race' and retried silently on every poll; it must now come
    out of process_merge_queue as an ordinary failure, and integration must be left exactly as it was."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_ok("init", "-q", "-b", "integration", cwd=repo)
    _git_ok("config", "user.email", "t@t", cwd=repo)
    _git_ok("config", "user.name", "t", cwd=repo)
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git_ok("add", "-A", cwd=repo)
    _git_ok("commit", "-q", "-m", "init", cwd=repo)
    _git_ok("checkout", "-q", "-b", "swarm/T1-coder", cwd=repo)
    (repo / "base.txt").write_text("branch version\n", encoding="utf-8")
    _git_ok("commit", "-aqm", "branch edit", cwd=repo)
    _git_ok("checkout", "-q", "integration", cwd=repo)
    (repo / "base.txt").write_text("uncommitted local edit\n", encoding="utf-8")  # dirty, tip unmoved
    tip = _git_ok("rev-parse", "integration", cwd=repo).stdout.strip()
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    _board_state(monkeypatch, pair)
    _record_card_actions(monkeypatch)

    assert controller.process_merge_queue("b", repo, plan, project, conn=conn) == []

    (fix_card,) = _fix_cards(created)
    assert _task_row(conn)["fix_cards"] == 1
    kinds = [e["kind"] for e in events.recent(conn)]
    assert "merge_failed" in kinds and "merge_race_retrying" not in kinds
    assert "did NOT move" in fix_card["body"]
    assert _git_ok("rev-parse", "integration", cwd=repo).stdout.strip() == tip


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


def test_review_lane_passes_the_tasks_allow_gate_config_changes_marker_to_the_gate(tmp_path, monkeypatch):
    """ASES-QG-02 (round 9, CIPIN): the tamper check's exemption is the plan task's own
    allow_gate_config_changes marker, not the task's touches, so process_review_lane must forward it from
    plan_mod.PlanTask through to review.gate_before_review -- checked here for both settings so a caller that
    hardcoded either value, or dropped the field, is caught."""
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    task = plan.task("T1")
    plan = dataclasses.replace(plan, tasks=(dataclasses.replace(task, allow_gate_config_changes=True),))
    monkeypatch.setattr(hermes, "kanban_list", lambda b, status=None, assignee=None: (
        [{"id": pair.work_card_id, "status": "review", "branch_name": "swarm/T1-coder"}]
        if status == "review" else []
    ))
    gate_calls = _record_gate_calls(monkeypatch)

    controller.process_review_lane("b", tmp_path / "repo", plan, conn=conn)

    assert [c["allow_gate_config_changes"] for c in gate_calls] == [True]


def test_review_lane_passes_the_project_and_task_to_gate_before_review(tmp_path, monkeypatch):
    """Round 9 (ASES-QG-04, ASES-SEC-03/05/07): the caller hands gate_before_review the project (for
    gates.resolve_runner) and the task itself (for its own network exception)."""
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    monkeypatch.setattr(hermes, "kanban_list", lambda b, status=None, assignee=None: (
        [{"id": pair.work_card_id, "status": "review", "branch_name": "swarm/T1-coder"}]
        if status == "review" else []
    ))
    seen = {}

    def fake(board, card_id, repo, branch, integration_branch, gate1_commands, touches, *, conn, task_key,
              allow_gate_config_changes=False, project_config=None, task=None):
        seen["project_config"] = project_config
        seen["task"] = task
        return True

    monkeypatch.setattr(review_mod, "gate_before_review", fake)

    controller.process_review_lane("b", tmp_path / "repo", plan, project, conn=conn)

    assert seen["project_config"] is project
    assert seen["task"] is plan.task("T1")


def test_review_lane_an_infrastructure_failure_is_recorded_and_the_card_is_held_not_sent_back(tmp_path, monkeypatch):
    """Round 9 (ASES-QG-04, ASES-SEC-03): the sandbox is enabled but Gate 1's re-check could not even start.
    Not a red gate: the card stays in review (never sent back) to be re-checked next pass, and a
    sandbox_infrastructure_error event records what happened."""
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    monkeypatch.setattr(hermes, "kanban_list", lambda b, status=None, assignee=None: (
        [{"id": pair.work_card_id, "status": "review", "branch_name": "swarm/T1-coder"}]
        if status == "review" else []
    ))

    def boom(*args, **kwargs):
        raise sandbox_mod.SandboxInfrastructureError("the sandbox is enabled but docker CLI not found on PATH")

    monkeypatch.setattr(review_mod, "gate_before_review", boom)

    sent_back = controller.process_review_lane("b", tmp_path / "repo", plan, project, conn=conn)

    assert sent_back == []
    rows = _refusals(conn, "sandbox_infrastructure_error")
    assert len(rows) == 1
    assert rows[0]["task_key"] == "T1" and rows[0]["gate"] == "gate1"
    assert "docker CLI not found" in rows[0]["error"]
    assert _refusals(conn, "gate1_recheck_failed") == []  # never treated as a red gate


def test_review_lane_an_infrastructure_failure_is_recorded_only_once_per_card_per_pass(tmp_path, monkeypatch):
    """A persistent outage must not flood the events table: same reasoning as tamper_check_error (_record_once)."""
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    monkeypatch.setattr(hermes, "kanban_list", lambda b, status=None, assignee=None: (
        [{"id": pair.work_card_id, "status": "review", "branch_name": "swarm/T1-coder"}]
        if status == "review" else []
    ))
    monkeypatch.setattr(review_mod, "gate_before_review", lambda *a, **kw: (_ for _ in ()).throw(
        sandbox_mod.SandboxInfrastructureError("the sandbox is enabled but docker CLI not found on PATH")
    ))

    controller.process_review_lane("b", tmp_path / "repo", plan, project, conn=conn)
    controller.process_review_lane("b", tmp_path / "repo", plan, project, conn=conn)

    assert len(_refusals(conn, "sandbox_infrastructure_error")) == 1


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


# --- round 9 (ASES-SEC-05/-07): sandbox_network_exceptions is pinned alongside the gate profiles ----------------


def test_pin_and_verify_with_no_network_exceptions_behaves_exactly_as_before(tmp_path):
    conn = db.connect(tmp_path / "ases.db")
    profiles = {"default": ["pytest -q"]}
    controller.pin_gate_profiles(conn, "t3", profiles, None)
    controller.verify_gate_pin(conn, "t3", dict(profiles), {})  # must not raise: None and {} agree


def test_verify_gate_pin_raises_when_a_network_exception_is_added_after_approval(tmp_path):
    conn = db.connect(tmp_path / "ases.db")
    profiles = {"default": ["pytest -q"]}
    controller.pin_gate_profiles(conn, "t3", profiles)  # approved with no exception at all

    with pytest.raises(controller.GateConfigTamperedError):
        controller.verify_gate_pin(conn, "t3", profiles, {"T1": [True, "installs a package"]})


def test_verify_gate_pin_raises_when_a_network_exception_is_edited_after_approval(tmp_path):
    conn = db.connect(tmp_path / "ases.db")
    profiles = {"default": ["pytest -q"]}
    controller.pin_gate_profiles(conn, "t3", profiles, {"T1": [True, "installs a package"]})

    with pytest.raises(controller.GateConfigTamperedError):
        controller.verify_gate_pin(conn, "t3", profiles, {"T1": [True, "a different reason entirely"]})


def test_verify_gate_pin_raises_when_a_network_exception_is_removed_after_approval(tmp_path):
    """The flag itself was flipped back off after approval without a fresh `swarm approve`: still a change from
    what Gate P saw, so it must be caught exactly like adding one."""
    conn = db.connect(tmp_path / "ases.db")
    profiles = {"default": ["pytest -q"]}
    controller.pin_gate_profiles(conn, "t3", profiles, {"T1": [True, "installs a package"]})

    with pytest.raises(controller.GateConfigTamperedError):
        controller.verify_gate_pin(conn, "t3", profiles, {})


def test_pin_and_verify_with_the_same_network_exception_passes(tmp_path):
    conn = db.connect(tmp_path / "ases.db")
    profiles = {"default": ["pytest -q"]}
    exceptions = {"T1": [True, "installs a package"]}
    controller.pin_gate_profiles(conn, "t3", profiles, exceptions)
    controller.verify_gate_pin(conn, "t3", dict(profiles), dict(exceptions))  # must not raise


# ---------------------------------------------------------------------------------------------
# Review-only tasks merge as a recorded no-op, work and fix cards say how to hand off, and the squash
# commit carries the card IDs (2026-09-19). Real git where the merged content matters; a scripted
# mergeq.merge_task where only the SHAPE of an outcome, or the arguments it was called with, matter.
# ---------------------------------------------------------------------------------------------

NO_OP_RESULT = "no changes to merge (review-only task)"
_NO_OP = mergeq.MergeOutcome(
    merged=True, candidate_sha=None, squash_commit=None, gate3_result="skipped", detail=NO_OP_RESULT,
)


def _repo_with_branch_at_tip(tmp_path, branch):
    """A worker that made no commit (a reviewer): its branch sits exactly at the integration tip."""
    repo = _plain_repo(tmp_path)
    _git_ok("branch", branch, cwd=repo)
    return repo


def _repo_with_a_coder_commit(tmp_path):
    repo = _plain_repo(tmp_path)
    _git_ok("checkout", "-q", "-b", "swarm/T1-coder", cwd=repo)
    (repo / "new.txt").write_text("x\n", encoding="utf-8")
    _git_ok("add", "-A", cwd=repo)
    _git_ok("commit", "-q", "-m", "add file", cwd=repo)
    _git_ok("checkout", "-q", "integration", cwd=repo)
    return repo


def _merged_events(conn):
    """The payloads of the "merged" events, task_key included (events.record used to redact it, because the
    credential pattern matched any field name containing "key"; fixed 2026-09-19)."""
    return [json.loads(e["payload"]) for e in events.recent(conn) if e["kind"] == "merged"]


def _merge_record(conn):
    return conn.execute("SELECT * FROM merge_records WHERE task_key='T1'").fetchone()


@pytest.mark.parametrize("role", ["reviewer", "lead"])
def test_review_only_task_with_an_empty_branch_completes_its_merge_card_as_a_no_op(tmp_path, monkeypatch, role):
    """Real git and the real mergeq.merge_task. A reviewer never commits, so its branch is empty against the
    integration tip; that used to be "nothing to commit" and a spurious fix card. Any role but "coder" gets
    the no-op: the merge card completes, nothing is merged, no fix card, no fix budget."""
    branch = f"swarm/T1-{role}"
    repo = _repo_with_branch_at_tip(tmp_path, branch)
    tip = _git_ok("rev-parse", "integration", cwd=repo).stdout.strip()
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch, plan_raw=_one_task_plan_raw(role))
    _board_state(monkeypatch, pair, branch=branch)
    actions = _record_card_actions(monkeypatch)

    merged = controller.process_merge_queue("b", repo, plan, project, conn=conn)

    assert merged == ["T1"]
    assert actions == {
        "link": [], "block": [], "ask": [], "reopen_review": [],
        "complete": [(pair.merge_card_id, {"result": NO_OP_RESULT,
                                           "metadata": {"squash_commit": None, "no_op": True}})],
    }
    assert _merged_events(conn) == [{"task_key": "T1", "sha": None, "no_op": True}]
    kinds = [e["kind"] for e in events.recent(conn)]
    assert "merge_failed" not in kinds and "fix_card_created" not in kinds
    assert _fix_cards(created) == []
    row = _task_row(conn)
    assert row["fix_cards"] == 0  # no fix budget was consumed
    assert row["work_card_id"] == pair.work_card_id  # and nothing was repointed
    assert _git_ok("rev-parse", "integration", cwd=repo).stdout.strip() == tip  # nothing was merged
    record = _merge_record(conn)  # the shape reconcile expects of a done merge card
    assert (record["gate3_result"], record["squash_commit"], record["reverted"]) == ("skipped", None, 0)
    assert record["completed_at"]


def test_coder_task_with_an_empty_branch_is_still_a_merge_failure(tmp_path, monkeypatch):
    """allow_empty is keyed on the task's role. A coder that produced no commit has not done its job, so
    its empty branch keeps failing the merge and opening a fix card instead of being recorded as a no-op."""
    repo = _repo_with_branch_at_tip(tmp_path, "swarm/T1-coder")
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    _board_state(monkeypatch, pair)
    actions = _record_card_actions(monkeypatch)

    merged = controller.process_merge_queue("b", repo, plan, project, conn=conn)

    assert merged == []
    assert actions["complete"] == []
    (fix_card,) = _fix_cards(created)
    assert _task_row(conn)["fix_cards"] == 1
    failed = next(e for e in events.recent(conn) if e["kind"] == "merge_failed")
    assert "nothing to commit" in failed["payload"]
    assert "Failure detail:\nnothing to commit" in fix_card["body"]
    assert _merged_events(conn) == []
    assert _merge_record(conn) is None  # nothing was recorded as merged


def test_normal_coder_merge_still_completes_with_the_squash_sha(tmp_path, monkeypatch):
    """The real-merge path is unchanged: result "merged <sha>", metadata {"squash_commit": sha}, and a
    "merged" event carrying just the task key and sha (no no_op key)."""
    repo = _repo_with_a_coder_commit(tmp_path)
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    _board_state(monkeypatch, pair)
    actions = _record_card_actions(monkeypatch)

    assert controller.process_merge_queue("b", repo, plan, project, conn=conn) == ["T1"]

    sha = _git_ok("rev-parse", "integration", cwd=repo).stdout.strip()
    assert actions["complete"] == [(pair.merge_card_id, {"result": f"merged {sha}",
                                                         "metadata": {"squash_commit": sha}})]
    assert _merged_events(conn) == [{"task_key": "T1", "sha": sha}]
    record = _merge_record(conn)
    assert (record["gate3_result"], record["squash_commit"]) == ("pass", sha)
    assert _fix_cards(created) == []


@pytest.mark.parametrize("role, expected", [
    ("coder", False),
    ("tester", False),  # round 7 part C: a tester commits too, so it is not allow_empty either (ASES-QG-05)
    ("reviewer", True), ("lead", True),
])
def test_allow_empty_is_passed_only_for_roles_outside_committing_roles(tmp_path, monkeypatch, role, expected):
    roles = dict(ROLES, tester="tester-1") if role == "tester" else ROLES
    plan, conn, project, pair, created = _setup_one_task(
        tmp_path, monkeypatch, plan_raw=_one_task_plan_raw(role), roles=roles,
    )
    _board_state(monkeypatch, pair, branch=f"swarm/T1-{role}")
    _record_card_actions(monkeypatch)
    calls = _script_merge_task(monkeypatch, _MERGED)

    controller.process_merge_queue("b", tmp_path / "repo", plan, project, conn=conn)

    assert [c["allow_empty"] for c in calls] == [expected]


def test_tester_task_with_a_real_commit_merges_exactly_like_a_coders_would(tmp_path, monkeypatch):
    """ASES-QG-05 (round 7, part C): the Tester writes acceptance tests and its card produces a real commit
    that must go through review and merge exactly like a coder's, not the review-only no-op path. Real git,
    real mergeq.merge_task, real Gate 3 (the plan's own trivial gate command). Before the fix, the six
    `role == "coder"` sites would have used allow_empty=True here (mergeq.merge_task never even looks for a
    commit) and completed this as a no-op with squash_commit=None, silently losing the tester's actual work."""
    repo = _plain_repo(tmp_path)
    _git_ok("checkout", "-q", "-b", "swarm/T1-tester", cwd=repo)
    (repo / "test_new.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    _git_ok("add", "-A", cwd=repo)
    _git_ok("commit", "-q", "-m", "add acceptance test", cwd=repo)
    _git_ok("checkout", "-q", "integration", cwd=repo)

    plan, conn, project, pair, created = _setup_one_task(
        tmp_path, monkeypatch, plan_raw=_one_task_plan_raw("tester"), roles=dict(ROLES, tester="tester-1"),
    )
    _board_state(monkeypatch, pair, branch="swarm/T1-tester")
    actions = _record_card_actions(monkeypatch)

    assert controller.process_merge_queue("b", repo, plan, project, conn=conn) == ["T1"]

    sha = _git_ok("rev-parse", "integration", cwd=repo).stdout.strip()
    assert actions["complete"] == [(pair.merge_card_id, {"result": f"merged {sha}",
                                                         "metadata": {"squash_commit": sha}})]
    assert _merged_events(conn) == [{"task_key": "T1", "sha": sha}]
    record = _merge_record(conn)
    assert (record["gate3_result"], record["squash_commit"]) == ("pass", sha)  # real Gate 3 ran, not skipped
    assert _fix_cards(created) == []


def test_tester_task_with_an_empty_branch_is_also_a_merge_failure_not_a_no_op(tmp_path, monkeypatch):
    """The pre-fix `role == "coder"` checks treated a tester exactly like a reviewer: review-only, no commit
    required. A tester is a _COMMITTING_ROLES role now (ASES-QG-05), so an empty branch fails the merge and
    opens a fix card, exactly the way test_coder_task_with_an_empty_branch_is_still_a_merge_failure does for a
    coder -- and exactly UNLIKE test_review_only_task_with_an_empty_branch_completes_its_merge_card_as_a_no_op,
    which is still correct for reviewer/lead."""
    repo = _repo_with_branch_at_tip(tmp_path, "swarm/T1-tester")
    plan, conn, project, pair, created = _setup_one_task(
        tmp_path, monkeypatch, plan_raw=_one_task_plan_raw("tester"), roles=dict(ROLES, tester="tester-1"),
    )
    _board_state(monkeypatch, pair, branch="swarm/T1-tester")
    actions = _record_card_actions(monkeypatch)

    merged = controller.process_merge_queue("b", repo, plan, project, conn=conn)

    assert merged == []
    assert actions["complete"] == []
    (fix_card,) = _fix_cards(created)
    assert _task_row(conn)["fix_cards"] == 1
    failed = next(e for e in events.recent(conn) if e["kind"] == "merge_failed")
    assert "nothing to commit" in failed["payload"]
    assert _merged_events(conn) == []
    assert _merge_record(conn) is None


def test_a_no_op_merge_is_never_routed_through_the_failure_path(tmp_path, monkeypatch):
    """With a fix budget of zero any failure blocks the merge card at once. A no-op must complete it
    instead, which shows it never reaches the failure path (or the fix budget) at all."""
    plan, conn, project, pair, created = _setup_one_task(
        tmp_path, monkeypatch, fix_cards_per_task=0, plan_raw=_one_task_plan_raw("reviewer"),
    )
    _board_state(monkeypatch, pair, branch="swarm/T1-reviewer")
    actions = _record_card_actions(monkeypatch)
    _script_merge_task(monkeypatch, _NO_OP)

    assert controller.process_merge_queue("b", tmp_path / "repo", plan, project, conn=conn) == ["T1"]

    assert [cid for cid, _ in actions["complete"]] == [pair.merge_card_id]
    assert actions["block"] == [] and actions["link"] == []
    assert _fix_cards(created) == [] and _task_row(conn)["fix_cards"] == 0
    kinds = [e["kind"] for e in events.recent(conn)]
    assert "merge_failed" not in kinds and "fix_card_budget_exhausted" not in kinds


def test_squash_commit_message_carries_the_card_ids(tmp_path, monkeypatch):
    """ASES-GIT-06: one squash commit per plan task, with the card ID in the commit message."""
    repo = _repo_with_a_coder_commit(tmp_path)
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    _board_state(monkeypatch, pair)
    _record_card_actions(monkeypatch)

    assert controller.process_merge_queue("b", repo, plan, project, conn=conn) == ["T1"]

    message = _git_ok("log", "-1", "--format=%B", "integration", cwd=repo).stdout.strip()
    assert pair.work_card_id in message and pair.merge_card_id in message and "scaffold" in message
    assert message == (
        f"T1: scaffold\n\nWork card: {pair.work_card_id}\nMerge card: {pair.merge_card_id}\n"
        "Branch: swarm/T1-coder\nControlled by ASES (one squash commit per plan task)."
    )


def _plan_task(key):
    """One of PLAN_RAW's tasks: T1 is a coder task (touches a.py), T2 a reviewer task (touches nothing)."""
    return plan_mod.parse_and_validate(PLAN_RAW, known_roles=set(ROLES), max_cards=40).task(key)


def test_coder_work_card_body_tells_the_worker_to_commit_and_request_review():
    body = controller._work_card_body(_plan_task("T1"))

    assert body.startswith("Role: coder")
    assert "kanban_request_review" in body
    assert "Do NOT call kanban_complete" in body
    assert "Commit your work" in body
    lines = body.splitlines()
    # The pre-existing lines are untouched, and the handoff block follows them after one blank line.
    assert lines[:6] == ["Role: coder", "Acceptance criteria:", "- exists", "Touches: a.py",
                         "Gate profile: trivial", ""]
    assert lines[6] == "How to finish (ASES):"


def test_reviewer_work_card_body_tells_the_worker_to_give_a_verdict_not_request_review():
    body = controller._work_card_body(_plan_task("T2"))

    assert body.startswith("Role: reviewer")
    assert "kanban_complete" in body and "PASS or FAIL" in body
    assert "kanban_request_review" not in body
    assert "Do NOT call kanban_complete" not in body
    lines = body.splitlines()
    # T2 touches nothing, so there is no Touches line; the handoff block still follows after a blank line.
    assert lines[:6] == ["Role: reviewer", "Acceptance criteria:", "- reviewed", "Gate profile: trivial", "",
                         "How to finish (ASES):"]


def test_finish_instructions_wording_is_pinned():
    """The words are load-bearing (a worker follows them literally), so any edit to them must be deliberate."""
    reviewer_block = [
        "How to finish (ASES):",
        "1. Read the acceptance criteria above and inspect the code in your worktree. You review; you do not "
        "edit product files.",
        "2. Give your verdict with kanban_complete: the summary starts with PASS or FAIL, and the metadata "
        "carries verdict, findings and criteria_checked.",
        "3. If you need a human decision, call kanban_block with one precise question.",
        "4. This card has no commit to merge; its merge card completes as a recorded no-op.",
    ]

    assert controller._finish_instructions("coder") == [
        "How to finish (ASES):",
        "1. Make the change only inside your worktree and only on the paths listed under Touches.",
        "2. Run the commands of your gate profile and fix what they report. Never edit tests, gate settings "
        "or CI files to make a check pass.",
        "3. Commit your work on this card's branch (git add, then git commit). Uncommitted work is not merged.",
        '4. Hand off with kanban_request_review, and always pass reviewer="reviewer" so the independent '
        "reviewer profile takes the card (without it the card stays assigned to you and you would review "
        "your own work). Give a one or two sentence summary, plus metadata with changed_files, the "
        "verification commands you ran, residual_risk and the commit SHA. Do NOT call kanban_complete on "
        "this card. The reviewer completes it after approving, and only then does the merge queue run.",
    ]
    assert controller._finish_instructions("reviewer") == reviewer_block
    assert controller._finish_instructions("lead") == reviewer_block  # any role not in _COMMITTING_ROLES
    # ASES-QG-05 (round 7 part C): a tester commits and hands off exactly like a coder, not like a reviewer.
    assert controller._finish_instructions("tester") == controller._finish_instructions("coder")
    assert controller._finish_instructions("tester") != reviewer_block


@pytest.mark.parametrize("role, present, absent", [
    ("coder", ["kanban_request_review", "Do NOT call kanban_complete", "Commit your work"], ["PASS or FAIL"]),
    ("reviewer", ["kanban_complete", "PASS or FAIL"], ["kanban_request_review"]),
])
def test_fix_card_body_keeps_the_failure_detail_and_appends_the_handoff_for_its_role(
    tmp_path, monkeypatch, role, present, absent,
):
    """The handoff text is chosen by the FIX task's role (task.role), not hardcoded to a coder's."""
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch, plan_raw=_one_task_plan_raw(role))
    _board_state(monkeypatch, pair, branch=f"swarm/T1-{role}")
    _record_card_actions(monkeypatch)
    _script_merge_task(monkeypatch, _CONFLICT)

    controller.process_merge_queue("b", tmp_path / "repo", plan, project, conn=conn)

    (fix_card,) = _fix_cards(created)
    body = fix_card["body"]
    assert body.startswith(
        "Merge attempt for T1 failed. Fix in a fresh worktree.\n\nFailure detail:\nmerge conflict: CONFLICT"
    )
    assert all(text in body for text in present) and not any(text in body for text in absent)
    # Appended after the existing text, preceded by a blank line, and nothing follows it.
    assert body.endswith("\n\n" + "\n".join(controller._finish_instructions(role)))


def test_fix_card_body_carries_the_tasks_touches_and_gate_profile(tmp_path, monkeypatch):
    """The hand-off steps tell the worker to stay inside "the paths listed under Touches" and to run "the
    commands of your gate profile"; a fix card's body used to carry neither line for them to refer to."""
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    _board_state(monkeypatch, pair)
    _record_card_actions(monkeypatch)
    _script_merge_task(monkeypatch, _CONFLICT)

    controller.process_merge_queue("b", tmp_path / "repo", plan, project, conn=conn)

    (fix_card,) = _fix_cards(created)
    body = fix_card["body"]
    assert "Failure detail:\nmerge conflict" in body
    assert "\nTouches: base.txt\n" in body and "\nGate profile: trivial\n" in body
    # In order: failure detail, then the scope, then the hand-off steps.
    assert body.index("Failure detail:") < body.index("Touches: base.txt") < body.index("How to finish (ASES):")


def test_work_card_body_still_lists_touches_and_gate_profile_in_the_original_order():
    body = controller._work_card_body(_plan_task("T1"))
    lines = body.splitlines()
    assert lines[0] == "Role: coder" and lines[1] == "Acceptance criteria:"
    assert lines.index("Gate profile: trivial") < lines.index("How to finish (ASES):")


def test_coder_body_names_the_default_reviewer_profile():
    assert 'reviewer="reviewer"' in controller._work_card_body(_plan_task("T1"))


def test_created_coder_card_names_the_projects_own_reviewer_profile(tmp_path, monkeypatch):
    """Hermes has no default reviewer: kanban_request_review only reassigns the card when reviewer= is given,
    so a coder that omits it would review its own work. The profile named is the one the project's roles map
    gives "reviewer" (here "rev-2"), the same one the reviewer-role task's own card is assigned to."""
    plan = plan_mod.parse_and_validate(PLAN_RAW, known_roles=set(ROLES), max_cards=40)
    conn = db.connect(tmp_path / "ases.db")
    counter = _FakeCounter()
    created = []
    monkeypatch.setattr(
        hermes, "kanban_create",
        lambda board, title, **kw: created.append({"id": counter.next_id("t"), "title": title, **kw})
        or created[-1],
    )
    project = dataclasses.replace(_project(tmp_path), roles={**ROLES, "reviewer": "rev-2"})

    controller.create_cards_from_plan("b", "proj1", tmp_path / "repo", plan, project, conn=conn)

    t1_work = next(c for c in created if c["title"] == "T1: scaffold")
    t2_work = next(c for c in created if c["title"] == "T2: review scaffold")
    assert 'reviewer="rev-2"' in t1_work["body"]
    assert 'reviewer="reviewer"' not in t1_work["body"]
    assert t2_work["assignee"] == "rev-2"


def test_fix_card_body_names_the_projects_own_reviewer_profile(tmp_path, monkeypatch):
    plan, conn, project, pair, created = _setup_one_task(
        tmp_path, monkeypatch, roles={**ROLES, "reviewer": "rev-2"},
    )
    states = _board_state(monkeypatch, pair)
    # This project's reviewer profile is "rev-2", so a card its reviewer approved was completed by that profile.
    states[pair.work_card_id]["_runs"] = [
        {"outcome": "completed", "profile": "rev-2", "metadata": {"review_outcome": "approved"}},
    ]
    _record_card_actions(monkeypatch)
    _script_merge_task(monkeypatch, _CONFLICT)

    controller.process_merge_queue("b", tmp_path / "repo", plan, project, conn=conn)

    (fix_card,) = _fix_cards(created)
    assert 'reviewer="rev-2"' in fix_card["body"]
    assert 'reviewer="reviewer"' not in fix_card["body"]


def test_a_project_with_no_reviewer_role_falls_back_to_the_literal_reviewer_profile(tmp_path, monkeypatch):
    """policy.resolve_assignee raises for an unmapped role, but a missing mapping is doctor's job to flag, not
    the card body's: a coder task's work card AND its fix card are still built, naming the literal
    profile "reviewer"."""
    plan, conn, project, pair, created = _setup_one_task(
        tmp_path, monkeypatch, roles={"lead": "lead", "coder": "coder-1"},
    )
    t1_work = next(c for c in created if c["title"] == "T1: scaffold")
    _board_state(monkeypatch, pair)
    _record_card_actions(monkeypatch)
    _script_merge_task(monkeypatch, _CONFLICT)

    controller.process_merge_queue("b", tmp_path / "repo", plan, project, conn=conn)

    (fix_card,) = _fix_cards(created)
    assert 'reviewer="reviewer"' in t1_work["body"]
    assert 'reviewer="reviewer"' in fix_card["body"]


# ---------------------------------------------------------------------------------------------
# ASES-GIT-03: a work card merges only if the reviewer profile completed it, and a pass polices the
# review lane before it dispatches (2026-09-19).
# ---------------------------------------------------------------------------------------------

def _refused_events(conn):
    return [json.loads(e["payload"]) for e in events.recent(conn, limit=200) if e["kind"] == "merge_refused_unreviewed"]


def test_a_card_its_own_implementer_completed_is_refused_not_merged(tmp_path, monkeypatch):
    """Real finding (2026-09-19): the first real coder run finished its own card with kanban_complete instead
    of asking for review, and the merge queue merged anything that read "done", so no independent reviewer
    ever saw the diff. Now only a card the reviewer profile completed merges."""
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    states = _board_state(monkeypatch, pair)
    states[pair.work_card_id]["_runs"] = [CODER_COMPLETED]
    actions = _record_card_actions(monkeypatch)
    calls = _script_merge_task(monkeypatch, _MERGED)
    unreviewed = []

    merged = controller.process_merge_queue("b", tmp_path / "repo", plan, project, conn=conn, unreviewed=unreviewed)
    controller.process_merge_queue("b", tmp_path / "repo", plan, project, conn=conn)  # the next poll

    assert merged == []
    assert calls == []  # merge_task was never reached, on either poll
    assert unreviewed == ["T1"]
    assert actions == {"link": [], "block": [], "complete": [], "ask": [], "reopen_review": []}  # the merge card was left exactly as it was
    assert _fix_cards(created) == []  # a refusal is not a merge failure: no fix card,
    assert _task_row(conn)["fix_cards"] == 0  # and no fix budget spent
    # Recorded once for the card, not once per poll.
    assert _refused_events(conn) == [{
        "task_key": "T1", "card_id": pair.work_card_id, "completed_by": "coder-1",
        "needs_completion_by": "reviewer",
    }]


def test_a_done_card_that_no_run_completed_is_refused(tmp_path, monkeypatch):
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    states = _board_state(monkeypatch, pair)
    states[pair.work_card_id]["_runs"] = []  # e.g. marked done by hand, with no run behind it
    _record_card_actions(monkeypatch)
    calls = _script_merge_task(monkeypatch, _MERGED)

    assert controller.process_merge_queue("b", tmp_path / "repo", plan, project, conn=conn) == []

    assert calls == []
    assert [e["completed_by"] for e in _refused_events(conn)] == [None]


@pytest.mark.parametrize("runs, merges", [
    ([REVIEWER_COMPLETED], True),
    ([{"outcome": "review_requested", "profile": "coder-1"}, REVIEWER_COMPLETED], True),
    # A crashed or otherwise unfinished run after the approval is not a completion: the latest COMPLETED run decides.
    ([REVIEWER_COMPLETED, {"outcome": "crashed", "profile": "coder-1"}], True),
    ([CODER_COMPLETED, REVIEWER_COMPLETED], True),  # finished by the implementer once, approved after
    ([REVIEWER_COMPLETED, CODER_COMPLETED], False),  # approved, then reopened and finished by the implementer
    ([CODER_COMPLETED], False),
    ([{"outcome": "review_requested", "profile": "coder-1"}], False),  # asked for review, nobody completed it
])
def test_the_latest_completed_run_decides_whether_a_card_may_merge(tmp_path, monkeypatch, runs, merges):
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    states = _board_state(monkeypatch, pair)
    states[pair.work_card_id]["_runs"] = runs
    _record_card_actions(monkeypatch)
    calls = _script_merge_task(monkeypatch, _MERGED)

    merged = controller.process_merge_queue("b", tmp_path / "repo", plan, project, conn=conn)

    assert merged == (["T1"] if merges else [])
    assert len(calls) == (1 if merges else 0)
    assert len(_refused_events(conn)) == (0 if merges else 1)


def test_run_pass_polices_the_review_lane_before_it_dispatches_and_reports_unreviewed_tasks(tmp_path, monkeypatch):
    """Real bug (2026-09-19): dispatch ran first and it also claims cards waiting in `review` and spawns their
    reviewer, so the Gate 1 re-check (which only acts on cards still in `review`) was skipped for every card
    dispatch reached first. The blueprint's loop re-runs Gate 1 before anything else claims the card."""
    import types

    order = []
    monkeypatch.setattr(usage_mod, "ingest_run_usage", lambda *a, **kw: order.append("usage") or ["s1", "s2"])
    monkeypatch.setattr(controller, "process_budget_gate", lambda *a, **kw: order.append("budget") or [])
    monkeypatch.setattr(controller, "process_review_lane", lambda *a, **kw: order.append("review") or [])
    monkeypatch.setattr(hermes, "kanban_dispatch", lambda board, **kw: order.append("dispatch") or {})

    passed = {}

    def fake_merge_queue(board, repo, plan, project, *, conn, unreviewed=None, models_config=None, integrity=None):
        order.append("merge")
        passed["merge_models_config"] = models_config
        unreviewed.append("T1")
        return []

    monkeypatch.setattr(controller, "process_budget_gate", lambda *a, **kw: (
        order.append("budget") or passed.update(gate_project=kw.get("project")) or []))
    monkeypatch.setattr(controller, "process_merge_queue", fake_merge_queue)
    monkeypatch.setattr(controller, "all_merge_cards_done", lambda *a, **kw: False)

    _guard_ok(monkeypatch)
    plan = types.SimpleNamespace(project="p", integration_branch="integration")
    project = types.SimpleNamespace(budgets={})
    models_config = {"providers": {}, "models": []}

    summary = controller.run_pass(
        "b", None, plan, project, models_config, conn=db.connect(tmp_path / "ases.db"),
    )

    # The budget gate needs the project for the review reserve; the merge queue needs the models config to ingest
    # the outgoing card's usage before a fix-card repoint. Both were once easy to drop without a test noticing.
    assert passed["gate_project"] is project
    assert passed["merge_models_config"] is models_config

    # Usage first (ASES-CAP-03: the budget gate is only as honest as the ledger), then the review lane
    # before dispatch, as the blueprint's loop does.
    assert order == ["usage", "budget", "review", "dispatch", "merge"]
    assert summary["unreviewed"] == ["T1"]
    assert summary["usage_sessions"] == 2
    assert summary["finished"] is False


# ---------------------------------------------------------------------------------------------
# Real usage into the ledger, and the review reserve (ASES-CAP-03, 2026-09-19).
# ---------------------------------------------------------------------------------------------

def test_run_pass_survives_a_usage_ingest_failure(tmp_path, monkeypatch):
    """A stale ledger for one pass is acceptable; a dead pass is not (one unreadable card would otherwise
    stall the whole polling loop, since the ingest runs first)."""
    import types

    conn = db.connect(tmp_path / "ases.db")
    order = []

    def boom(*a, **kw):
        raise hermes.HermesCommandError(["kanban", "show", "x"], 1, "card vanished")

    monkeypatch.setattr(usage_mod, "ingest_run_usage", boom)
    monkeypatch.setattr(controller, "process_budget_gate", lambda *a, **kw: order.append("budget") or [])
    monkeypatch.setattr(controller, "process_review_lane", lambda *a, **kw: order.append("review") or [])
    monkeypatch.setattr(hermes, "kanban_dispatch", lambda board, **kw: order.append("dispatch") or {})
    monkeypatch.setattr(controller, "process_merge_queue", lambda *a, **kw: order.append("merge") or [])
    monkeypatch.setattr(controller, "all_merge_cards_done", lambda *a, **kw: False)

    _guard_ok(monkeypatch)
    plan = types.SimpleNamespace(project="p", integration_branch="integration")

    summary = controller.run_pass("b", None, plan, types.SimpleNamespace(budgets={}), {}, conn=conn)

    assert order == ["budget", "review", "dispatch", "merge"]  # every later step still ran
    assert summary["usage_sessions"] == 0
    (event,) = [json.loads(e["payload"]) for e in events.recent(conn) if e["kind"] == "usage_ingest_error"]
    assert "card vanished" in event["error"]


REVIEW_MODELS_CONFIG = {
    "providers": {
        "xkiro": {"limits": {}},  # no known daily cap: the coder's own provider never runs dry
        "openrouter": {"limits": {"per_day_default": 50, "per_day_after_credits": 1000}, "credits_purchased": False},
    },
    "models": [
        {"provider": "xkiro", "model": "coder-m", "role_class": "coder", "pinned": True},
        {"provider": "openrouter", "model": "rev-m", "role_class": "reviewer", "pinned": True},
    ],
}


def _ready_cards_gate(tmp_path, monkeypatch, *, used_on_reviewer_provider, project_budgets):
    plan = plan_mod.parse_and_validate(PLAN_RAW, known_roles=set(ROLES), max_cards=40)
    conn = db.connect(tmp_path / "ases.db")
    counter = _FakeCounter()
    monkeypatch.setattr(hermes, "kanban_create", lambda board, title, **kw: {"id": counter.next_id("t"), **kw})
    project = dataclasses.replace(_project(tmp_path), budgets=project_budgets)
    pairs = controller.create_cards_from_plan("b", "proj1", tmp_path / "repo", plan, project, conn=conn)
    from ases import ledger
    if used_on_reviewer_provider:
        ledger.record_usage(conn, "openrouter", "rev-m", n=used_on_reviewer_provider)
    ready = [pairs[0].work_card_id, pairs[1].work_card_id]  # T1 (coder) and T2 (reviewer role)
    monkeypatch.setattr(hermes, "kanban_list", lambda b, status=None, assignee=None: (
        [{"id": cid, "status": "ready"} for cid in ready] if status == "ready" else []
    ))
    scheduled = []
    monkeypatch.setattr(hermes, "kanban_schedule", lambda b, cid, reason: scheduled.append((cid, reason)))
    return plan, conn, project, pairs, scheduled


def test_budget_gate_parks_coder_cards_when_the_reviewer_provider_cannot_afford_the_review_reserve(
    tmp_path, monkeypatch,
):
    """ASES-CAP-03's review half: a coder card that finishes needs a review pass on the REVIEWER's provider.
    Here the coder's provider has no cap but OpenRouter (the reviewer) is nearly spent, so starting the coder
    card would only leave finished work sitting in review with nobody able to review it today. The reviewer
    role's own card is a different question and is left alone: it has its own provider check."""
    plan, conn, project, pairs, scheduled = _ready_cards_gate(
        tmp_path, monkeypatch, used_on_reviewer_provider=35,  # 15 of 50 left, minus the 10% reserve = 10 usable
        project_budgets={"review_reserve_requests": 20, "daily_reserve_percent": 10},
    )

    parked = controller.process_budget_gate(
        "b", plan, REVIEW_MODELS_CONFIG, conn=conn, budgets={}, project=project,
    )

    assert parked == ["T1"]
    assert [cid for cid, _ in scheduled] == [pairs[0].work_card_id]
    assert "review budget on openrouter" in scheduled[0][1]
    assert any(e["kind"] == "card_parked_for_budget" for e in events.recent(conn))


def test_budget_gate_starts_coder_cards_when_the_review_reserve_is_affordable(tmp_path, monkeypatch):
    plan, conn, project, pairs, scheduled = _ready_cards_gate(
        tmp_path, monkeypatch, used_on_reviewer_provider=10,  # 40 left: plenty for a 20 request reserve
        project_budgets={"review_reserve_requests": 20, "daily_reserve_percent": 10},
    )

    parked = controller.process_budget_gate(
        "b", plan, REVIEW_MODELS_CONFIG, conn=conn, budgets={}, project=project,
    )

    assert parked == [] and scheduled == []


def test_budget_gate_without_a_project_never_applies_the_review_reserve(tmp_path, monkeypatch):
    """The review reserve needs the project's roles and budgets; the pre-existing callers that pass none keep
    exactly the behaviour they had."""
    plan, conn, project, pairs, scheduled = _ready_cards_gate(
        tmp_path, monkeypatch, used_on_reviewer_provider=45,
        project_budgets={"review_reserve_requests": 20},
    )

    parked = controller.process_budget_gate("b", plan, REVIEW_MODELS_CONFIG, conn=conn, budgets={})

    assert "T1" not in parked


TESTER_PLAN_RAW = {
    "project": "t3",
    "integration_branch": "integration",
    "gate_profiles": {"trivial": ["echo ok"]},
    "tasks": [
        {"key": "T1", "title": "write acceptance tests", "role": "tester", "depends_on": [], "touches": ["a.py"],
         "acceptance": ["exists"], "gate_profile": "trivial", "estimated_requests": 10},
        {"key": "T2", "title": "review the tests", "role": "reviewer", "depends_on": ["T1"],
         "touches": [], "acceptance": ["reviewed"], "gate_profile": "trivial", "estimated_requests": 5},
    ],
}
TESTER_ROLES = dict(ROLES, tester="tester-1")
# Mirrors REVIEW_MODELS_CONFIG, but the committing role pinned is "tester", not "coder".
TESTER_REVIEW_MODELS_CONFIG = {
    "providers": {
        "xkiro": {"limits": {}},  # no known daily cap: the tester's own provider never runs dry
        "openrouter": {"limits": {"per_day_default": 50, "per_day_after_credits": 1000}, "credits_purchased": False},
    },
    "models": [
        {"provider": "xkiro", "model": "tester-m", "role_class": "tester", "pinned": True},
        {"provider": "openrouter", "model": "rev-m", "role_class": "reviewer", "pinned": True},
    ],
}


def _tester_ready_cards_gate(tmp_path, monkeypatch, *, used_on_reviewer_provider, project_budgets):
    plan = plan_mod.parse_and_validate(TESTER_PLAN_RAW, known_roles=set(TESTER_ROLES), max_cards=40)
    conn = db.connect(tmp_path / "ases.db")
    counter = _FakeCounter()
    monkeypatch.setattr(hermes, "kanban_create", lambda board, title, **kw: {"id": counter.next_id("t"), **kw})
    project = dataclasses.replace(_project(tmp_path), roles=TESTER_ROLES, budgets=project_budgets)
    pairs = controller.create_cards_from_plan("b", "proj1", tmp_path / "repo", plan, project, conn=conn)
    from ases import ledger
    if used_on_reviewer_provider:
        ledger.record_usage(conn, "openrouter", "rev-m", n=used_on_reviewer_provider)
    ready = [pairs[0].work_card_id, pairs[1].work_card_id]  # T1 (tester) and T2 (reviewer role)
    monkeypatch.setattr(hermes, "kanban_list", lambda b, status=None, assignee=None: (
        [{"id": cid, "status": "ready"} for cid in ready] if status == "ready" else []
    ))
    scheduled = []
    monkeypatch.setattr(hermes, "kanban_schedule", lambda b, cid, reason: scheduled.append((cid, reason)))
    return plan, conn, project, pairs, scheduled


def test_budget_gate_parks_tester_cards_too_when_the_reviewer_provider_cannot_afford_the_review_reserve(
    tmp_path, monkeypatch,
):
    """ASES-QG-05 (round 7, part C): a tester's card needs a review pass exactly like a coder's now
    (_COMMITTING_ROLES), so the review-reserve half of ASES-CAP-03 (_affordable_now) must apply to it too. The
    pre-fix `task.role == "coder"` check would have skipped this entirely for a tester task, letting it
    dispatch even though nobody could afford to review the work it would produce."""
    plan, conn, project, pairs, scheduled = _tester_ready_cards_gate(
        tmp_path, monkeypatch, used_on_reviewer_provider=35,  # 15 of 50 left, minus the 10% reserve = 10 usable
        project_budgets={"review_reserve_requests": 20, "daily_reserve_percent": 10},
    )

    parked = controller.process_budget_gate(
        "b", plan, TESTER_REVIEW_MODELS_CONFIG, conn=conn, budgets={}, project=project,
    )

    assert parked == ["T1"]
    assert [cid for cid, _ in scheduled] == [pairs[0].work_card_id]
    assert "review budget on openrouter" in scheduled[0][1]
    assert any(e["kind"] == "card_parked_for_budget" for e in events.recent(conn))


def test_budget_gate_starts_tester_cards_when_the_review_reserve_is_affordable(tmp_path, monkeypatch):
    plan, conn, project, pairs, scheduled = _tester_ready_cards_gate(
        tmp_path, monkeypatch, used_on_reviewer_provider=10,  # 40 left: plenty for a 20 request reserve
        project_budgets={"review_reserve_requests": 20, "daily_reserve_percent": 10},
    )

    parked = controller.process_budget_gate(
        "b", plan, TESTER_REVIEW_MODELS_CONFIG, conn=conn, budgets={}, project=project,
    )

    assert parked == [] and scheduled == []


def test_fix_card_creation_ingests_the_outgoing_cards_usage_before_the_repoint(tmp_path, monkeypatch):
    """Found by the usage builder: process_merge_queue repoints plan_tasks.work_card_id at a fix card, after
    which the per-pass ingest never reads the outgoing card again, so a reviewer run that ended since the last
    pass would go uncounted. The outgoing card is ingested first, by id, and only when models_config is given."""
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    _board_state(monkeypatch, pair)
    _record_card_actions(monkeypatch)
    _script_merge_task(monkeypatch, _CONFLICT)
    calls = []

    def fake_ingest(board, card_id, proj, models_config, *, conn, plan_project=None, task_key=None):
        calls.append((card_id, _task_row(conn)["work_card_id"], plan_project, task_key))  # work_card_id at call time
        return []

    monkeypatch.setattr(usage_mod, "ingest_card_usage", fake_ingest)

    controller.process_merge_queue(
        "b", tmp_path / "repo", plan, project, conn=conn, models_config=REVIEW_MODELS_CONFIG,
    )

    # the ORIGINAL card, before it was repointed, attributed to its plan task
    assert calls == [(pair.work_card_id, pair.work_card_id, plan.project, "T1")]
    assert _task_row(conn)["work_card_id"] != pair.work_card_id  # and only then was it repointed


def test_fix_card_creation_without_models_config_does_not_ingest(tmp_path, monkeypatch):
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    _board_state(monkeypatch, pair)
    _record_card_actions(monkeypatch)
    _script_merge_task(monkeypatch, _CONFLICT)
    monkeypatch.setattr(usage_mod, "ingest_card_usage", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no")))

    controller.process_merge_queue("b", tmp_path / "repo", plan, project, conn=conn)

    assert len(_fix_cards(created)) == 1  # the fix card was still made


def test_a_failing_outgoing_card_ingest_does_not_stop_the_fix_card(tmp_path, monkeypatch):
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    _board_state(monkeypatch, pair)
    _record_card_actions(monkeypatch)
    _script_merge_task(monkeypatch, _CONFLICT)

    def boom(*a, **kw):
        raise RuntimeError("export failed")

    monkeypatch.setattr(usage_mod, "ingest_card_usage", boom)

    controller.process_merge_queue(
        "b", tmp_path / "repo", plan, project, conn=conn, models_config=REVIEW_MODELS_CONFIG,
    )

    assert len(_fix_cards(created)) == 1
    assert _task_row(conn)["work_card_id"] != pair.work_card_id
    assert any(e["kind"] == "usage_ingest_error" for e in events.recent(conn))


# ---------------------------------------------------------------------------------------------
# ensure_repo_bootstrapped (ASES-GIT-10, round 7 part A): git worktree add needs at least one commit, so an
# empty repository gets a branch and one commit before swarm run's primary-checkout guard would otherwise
# refuse it outright.
# ---------------------------------------------------------------------------------------------


def test_ensure_repo_bootstrapped_creates_branch_and_one_commit_when_there_is_no_git_at_all(tmp_path):
    repo = tmp_path / "brand_new"  # does not exist on disk yet either

    created = controller.ensure_repo_bootstrapped(repo, "integration")

    assert created is True
    assert (repo / ".git").is_dir()
    assert _git_ok("symbolic-ref", "--short", "HEAD", cwd=repo).stdout.strip() == "integration"
    log = _git_ok("log", "--oneline", cwd=repo).stdout.strip().splitlines()
    assert len(log) == 1  # exactly one commit
    assert (repo / "README.md").exists() and (repo / ".gitignore").exists()
    assert _git_ok("status", "--porcelain", cwd=repo).stdout == ""  # everything on disk was committed


def test_ensure_repo_bootstrapped_creates_branch_and_commit_when_git_exists_with_zero_commits(tmp_path):
    """`.git init -b <name>` only sets the initial branch while `.git` does not exist yet; a re-init on an
    existing unborn `.git` silently ignores --initial-branch, so this case needs `git checkout -B` instead."""
    repo = tmp_path / "unborn"
    repo.mkdir()
    _git_ok("init", "-q", cwd=repo)  # no -b: default branch name, zero commits

    created = controller.ensure_repo_bootstrapped(repo, "integration")

    assert created is True
    assert _git_ok("symbolic-ref", "--short", "HEAD", cwd=repo).stdout.strip() == "integration"
    assert len(_git_ok("log", "--oneline", cwd=repo).stdout.strip().splitlines()) == 1


def test_ensure_repo_bootstrapped_never_touches_a_repo_with_real_history_even_on_the_wrong_branch(tmp_path):
    """ASES-GIT-10 says 'when the repository is empty', not 'when it happens to be on the wrong branch': that
    stays publish_plan's own refusal, unchanged."""
    repo = _plain_repo(tmp_path, name="existing")  # already has one commit, on "integration"
    _git_ok("checkout", "-q", "-b", "some-other-branch", cwd=repo)
    tip = _git_ok("rev-parse", "HEAD", cwd=repo).stdout.strip()

    created = controller.ensure_repo_bootstrapped(repo, "integration")

    assert created is False
    assert _git_ok("symbolic-ref", "--short", "HEAD", cwd=repo).stdout.strip() == "some-other-branch"
    assert _git_ok("rev-parse", "HEAD", cwd=repo).stdout.strip() == tip
    assert (repo / "README.md").read_text(encoding="utf-8") == "hi\n"  # _plain_repo's own file, not rewritten


def test_ensure_repo_bootstrapped_is_a_no_op_on_an_already_correct_repo(tmp_path):
    repo = _plain_repo(tmp_path, name="already_fine")  # already on "integration" with a commit
    tip = _git_ok("rev-parse", "HEAD", cwd=repo).stdout.strip()

    created = controller.ensure_repo_bootstrapped(repo, "integration")

    assert created is False
    assert _git_ok("rev-parse", "HEAD", cwd=repo).stdout.strip() == tip


def test_ensure_repo_bootstrapped_never_writes_git_config(tmp_path):
    repo = tmp_path / "no_identity"

    assert controller.ensure_repo_bootstrapped(repo, "integration") is True

    local_config = (repo / ".git" / "config").read_text(encoding="utf-8")
    assert "[user]" not in local_config  # the commit's identity was scoped to that one git call, never persisted


def test_ensure_repo_bootstrapped_keeps_a_pre_existing_readme_and_gitignore(tmp_path):
    """A file already on disk before ASES ever looked at this repository (someone started adding source before
    running git init) is kept, not clobbered by the bootstrap's own README/.gitignore -- but is still swept
    into the one commit (git add -A), so it is not left as an untracked change for the next guard to trip on."""
    repo = tmp_path / "pre_seeded"
    repo.mkdir()
    (repo / "README.md").write_text("the real readme\n", encoding="utf-8")
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")

    created = controller.ensure_repo_bootstrapped(repo, "integration")

    assert created is True
    assert (repo / "README.md").read_text(encoding="utf-8") == "the real readme\n"
    assert _git_ok("status", "--porcelain", cwd=repo).stdout == ""
    tracked = _git_ok("ls-files", cwd=repo).stdout.split()
    assert "app.py" in tracked and "README.md" in tracked


def test_ensure_repo_bootstrapped_records_an_event_only_on_success(tmp_path):
    repo = tmp_path / "brand_new"
    conn = db.connect(tmp_path / "ases.db")

    created = controller.ensure_repo_bootstrapped(repo, "integration", conn=conn)

    assert created is True
    rows = [json.loads(e["payload"]) for e in events.recent(conn) if e["kind"] == "repo_bootstrapped"]
    assert len(rows) == 1
    assert rows[0]["integration_branch"] == "integration" and rows[0]["repo"] == str(repo)
    assert rows[0]["commit"] == _git_ok("rev-parse", "HEAD", cwd=repo).stdout.strip()


def test_ensure_repo_bootstrapped_records_no_event_when_nothing_was_done(tmp_path):
    repo = _plain_repo(tmp_path)
    conn = db.connect(tmp_path / "ases.db")

    assert controller.ensure_repo_bootstrapped(repo, "integration", conn=conn) is False

    assert [e for e in events.recent(conn) if e["kind"].startswith("repo_bootstrap")] == []


def test_ensure_repo_bootstrapped_without_a_connection_still_bootstraps_but_records_nothing(tmp_path):
    """A plain unit test of the git mechanics needs no database at all (conn defaults to None)."""
    repo = tmp_path / "brand_new"

    assert controller.ensure_repo_bootstrapped(repo, "integration") is True
    assert (repo / ".git").is_dir()


def test_ensure_repo_bootstrapped_records_an_error_event_and_returns_false_on_a_git_failure(tmp_path, monkeypatch):
    """Never raises for an ordinary git failure (the standing rule): a step that fails is reported through the
    event, not an exception, and the caller gets False back (nothing was durably created)."""
    repo = tmp_path / "brand_new"
    conn = db.connect(tmp_path / "ases.db")
    real = controller._bootstrap_git

    def fail_on_commit(repo_arg, args):
        if args and args[0] == "-c":
            return subprocess.CompletedProcess(args, 1, "", "commit failed: no identity available")
        return real(repo_arg, args)

    monkeypatch.setattr(controller, "_bootstrap_git", fail_on_commit)

    created = controller.ensure_repo_bootstrapped(repo, "integration", conn=conn)

    assert created is False
    errors = [json.loads(e["payload"]) for e in events.recent(conn) if e["kind"] == "repo_bootstrap_error"]
    assert len(errors) == 1 and errors[0]["step"] == "commit"
    assert "commit failed" in errors[0]["detail"]
    assert [e for e in events.recent(conn) if e["kind"] == "repo_bootstrapped"] == []


# ---------------------------------------------------------------------------------------------
# The primary checkout guard (ASES-GIT-12, 2026-09-19).
# ---------------------------------------------------------------------------------------------

def _guard_ok(monkeypatch, expected=None):
    """Make run_pass's per-pass guard pass without a real repository (guards.py has its own git tests)."""
    seen = []

    def fake_check(repo, integration_branch, expected_head=None, **kw):
        seen.append((integration_branch, expected_head))
        return guards_mod.GuardResult(True, (), "abc123", integration_branch)

    monkeypatch.setattr(guards_mod, "check_primary_checkout", fake_check)
    return seen


def test_run_pass_halts_before_doing_anything_when_the_primary_checkout_was_changed(tmp_path, monkeypatch):
    """ASES-GIT-12: a dirty primary checkout, a HEAD ASES did not move or a wrong branch stops the pass BEFORE
    usage, budget, review, dispatch or merge run, records a security event, and reports the problems so the
    polling loop can halt. Nothing is safe to build on until a human has looked."""
    import types

    conn = db.connect(tmp_path / "ases.db")
    order = []
    monkeypatch.setattr(usage_mod, "ingest_run_usage", lambda *a, **kw: order.append("usage") or [])
    monkeypatch.setattr(controller, "process_budget_gate", lambda *a, **kw: order.append("budget") or [])
    monkeypatch.setattr(controller, "process_review_lane", lambda *a, **kw: order.append("review") or [])
    monkeypatch.setattr(hermes, "kanban_dispatch", lambda board, **kw: order.append("dispatch") or {})
    monkeypatch.setattr(controller, "process_merge_queue", lambda *a, **kw: order.append("merge") or [])
    monkeypatch.setattr(guards_mod, "check_primary_checkout", lambda *a, **kw: guards_mod.GuardResult(
        False, ("primary checkout is dirty: ?? 'stray.txt'", "primary checkout HEAD moved: expected aaa, found bbb"),
        "bbb", "integration"))
    plan = types.SimpleNamespace(project="p", integration_branch="integration")

    summary = controller.run_pass("b", tmp_path, plan, types.SimpleNamespace(budgets={}), {}, conn=conn)

    assert order == []  # not one later step ran
    assert summary["integrity"] == ["primary checkout is dirty: ?? 'stray.txt'",
                                    "primary checkout HEAD moved: expected aaa, found bbb"]
    assert summary["finished"] is False and summary["merged"] == [] and summary["dispatch"] == {}
    (event,) = [json.loads(e["payload"]) for e in events.recent(conn) if e["kind"] == "integrity_violation"]
    assert event["head"] == "bbb" and len(event["problems"]) == 2


def test_run_pass_passes_the_stored_expected_head_and_the_plans_integration_branch_to_the_guard(tmp_path, monkeypatch):
    import types

    conn = db.connect(tmp_path / "ases.db")
    guards_mod.set_expected_head(conn, "p", "deadbeef")
    seen = _guard_ok(monkeypatch)
    monkeypatch.setattr(usage_mod, "ingest_run_usage", lambda *a, **kw: [])
    monkeypatch.setattr(controller, "process_budget_gate", lambda *a, **kw: [])
    monkeypatch.setattr(controller, "process_review_lane", lambda *a, **kw: [])
    monkeypatch.setattr(hermes, "kanban_dispatch", lambda board, **kw: {})
    monkeypatch.setattr(controller, "process_merge_queue", lambda *a, **kw: [])
    monkeypatch.setattr(controller, "all_merge_cards_done", lambda *a, **kw: False)
    plan = types.SimpleNamespace(project="p", integration_branch="trunk")

    summary = controller.run_pass("b", tmp_path, plan, types.SimpleNamespace(budgets={}), {}, conn=conn)

    assert seen == [("trunk", "deadbeef")]
    assert summary["integrity"] == []


def test_run_pass_halts_when_the_merge_queue_cannot_repair_the_branch_after_a_revert(tmp_path, monkeypatch):
    """ASES-GIT-05, round 6: process_merge_queue's own integrity out-param, wired into run_pass the same way the
    primary-checkout guard's problems already are (an integrity_violation event, run_pass returns with
    `integrity` set) -- nothing later in the pass is safe to run on top of a branch that could not be repaired."""
    import types

    conn = db.connect(tmp_path / "ases.db")
    seen = _guard_ok(monkeypatch)
    order = []
    monkeypatch.setattr(usage_mod, "ingest_run_usage", lambda *a, **kw: order.append("usage") or [])
    monkeypatch.setattr(controller, "process_budget_gate", lambda *a, **kw: order.append("budget") or [])
    monkeypatch.setattr(controller, "process_review_lane", lambda *a, **kw: order.append("review") or [])
    monkeypatch.setattr(hermes, "kanban_dispatch", lambda board, **kw: order.append("dispatch") or {})
    monkeypatch.setattr(controller, "process_finalize", lambda *a, **kw: order.append("finalize") or None)

    def fake_merge_queue(*a, integrity=None, **kw):
        order.append("merge")
        if integrity is not None:
            integrity.append("post-merge Gate 3 failed for T1 and the revert could not repair the branch")
        return []

    monkeypatch.setattr(controller, "process_merge_queue", fake_merge_queue)
    plan = types.SimpleNamespace(project="p", integration_branch="trunk")

    summary = controller.run_pass("b", tmp_path, plan, types.SimpleNamespace(budgets={}), {}, conn=conn)

    assert seen  # the guard itself ran and was green: this halt comes from the merge queue, not the guard
    assert order == ["usage", "budget", "review", "dispatch", "merge"]  # finalize never ran
    assert summary["integrity"] == ["post-merge Gate 3 failed for T1 and the revert could not repair the branch"]
    assert summary["final"] is None and summary["finished"] is False


def test_a_real_merge_records_the_new_head_as_the_expected_one(tmp_path, monkeypatch):
    """The merge queue moves the primary checkout's HEAD itself, so the guard must expect the new tip or the
    controller's own fast-forward would be reported as a violation on the very next pass."""
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    _board_state(monkeypatch, pair)
    _record_card_actions(monkeypatch)
    _script_merge_task(monkeypatch, _MERGED)

    assert controller.process_merge_queue("b", tmp_path / "repo", plan, project, conn=conn) == ["T1"]

    assert guards_mod.expected_head(conn, plan.project) == "cand1"


def test_a_no_op_merge_leaves_the_expected_head_alone(tmp_path, monkeypatch):
    repo = _repo_with_branch_at_tip(tmp_path, "swarm/T1-reviewer")
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch, plan_raw=_one_task_plan_raw("reviewer"))
    _board_state(monkeypatch, pair, branch="swarm/T1-reviewer")
    _record_card_actions(monkeypatch)
    guards_mod.set_expected_head(conn, plan.project, "before")

    controller.process_merge_queue("b", repo, plan, project, conn=conn)

    assert guards_mod.expected_head(conn, plan.project) == "before"  # nothing was committed, HEAD did not move


def test_a_failed_merge_leaves_the_expected_head_alone(tmp_path, monkeypatch):
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    _board_state(monkeypatch, pair)
    _record_card_actions(monkeypatch)
    _script_merge_task(monkeypatch, _CONFLICT)
    guards_mod.set_expected_head(conn, plan.project, "before")

    controller.process_merge_queue("b", tmp_path / "repo", plan, project, conn=conn)

    assert guards_mod.expected_head(conn, plan.project) == "before"


# ---------------------------------------------------------------------------------------------
# The post-merge check and revert trigger (round 6, ASES-GIT-05, section 8.1: "The integration branch MUST stay
# runnable. If a post-merge check fails, the queue reverts the squash commit, records it, blocks the merge card
# and opens a fix card."). mergeq.merge_task itself is scripted (as above); only gates_mod.run_gate (the
# post-merge check) and mergeq.revert_merge need their own stubs here.
# ---------------------------------------------------------------------------------------------

def test_post_merge_check_green_completes_normally_and_checks_the_right_commit(tmp_path, monkeypatch):
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    _board_state(monkeypatch, pair)
    actions = _record_card_actions(monkeypatch)
    _script_merge_task(monkeypatch, _MERGED)
    calls = _stub_post_merge_gate(monkeypatch, passed=True)

    merged = controller.process_merge_queue("b", tmp_path / "repo", plan, project, conn=conn)

    assert merged == ["T1"]
    assert len(calls) == 1
    assert calls[0] == {"commit_sha": "cand1", "gate_name": "gate3-postmerge", "commands": ["echo ok"],
                        "task_key": "T1", "project": "t3"}
    assert actions["complete"] == [(pair.merge_card_id, {"result": "merged cand1",
                                                          "metadata": {"squash_commit": "cand1"}})]
    assert guards_mod.expected_head(conn, plan.project) == "cand1"
    assert _fix_cards(created) == []


def test_post_merge_check_red_but_revert_succeeds_opens_a_fix_card_and_leaves_the_merge_card_open(
    tmp_path, monkeypatch,
):
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    _board_state(monkeypatch, pair)
    actions = _record_card_actions(monkeypatch)
    _script_merge_task(monkeypatch, _MERGED)
    _stub_post_merge_gate(monkeypatch, passed=False, detail="another task's merge broke this")
    revert_calls = []

    def fake_revert(repo, squash_commit, *, conn=None, task_key="", project=None):
        revert_calls.append({"squash_commit": squash_commit, "task_key": task_key, "project": project})
        return mergeq.RevertOutcome(True, "revertsha1", "reverted cand1", aborted=False)

    monkeypatch.setattr(mergeq, "revert_merge", fake_revert)

    merged = controller.process_merge_queue("b", tmp_path / "repo", plan, project, conn=conn)

    assert merged == []  # never merged: the branch it landed on turned out broken
    assert revert_calls == [{"squash_commit": "cand1", "task_key": "T1", "project": "t3"}]
    assert actions["complete"] == []  # the merge card is NEVER completed on this path
    assert guards_mod.expected_head(conn, plan.project) == "revertsha1"  # the revert's own new HEAD
    fix_cards = _fix_cards(created)
    assert len(fix_cards) == 1 and _task_row(conn)["fix_cards"] == 1
    kinds = [e["kind"] for e in events.recent(conn)]
    assert "post_merge_reverted" in kinds and "merge_failed" in kinds and "fix_card_created" in kinds
    assert "integrity_violation" not in kinds
    (reverted_event,) = _refusals(conn, "post_merge_reverted")
    assert reverted_event == {"task_key": "T1", "commit": "cand1", "detail": "another task's merge broke this"}


def test_post_merge_check_red_and_revert_also_fails_halts_via_the_integrity_out_param(tmp_path, monkeypatch):
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    _board_state(monkeypatch, pair)
    actions = _record_card_actions(monkeypatch)
    _script_merge_task(monkeypatch, _MERGED)
    _stub_post_merge_gate(monkeypatch, passed=False, detail="broken")
    monkeypatch.setattr(mergeq, "revert_merge", lambda *a, **kw: mergeq.RevertOutcome(
        False, None, "git revert failed and --abort also failed", aborted=False,
    ))
    integrity: list[str] = []

    merged = controller.process_merge_queue(
        "b", tmp_path / "repo", plan, project, conn=conn, integrity=integrity,
    )

    assert merged == []
    assert integrity and "T1" in integrity[0] and "cand1" in integrity[0]
    assert actions["complete"] == []
    assert _fix_cards(created) == [] and _task_row(conn)["fix_cards"] == 0  # not the ordinary failure path
    kinds = [e["kind"] for e in events.recent(conn)]
    assert "integrity_violation" in kinds
    assert "fix_card_created" not in kinds and "merge_failed" not in kinds
    assert guards_mod.expected_head(conn, plan.project) is None  # never set: no known-good HEAD to vouch for


def test_tester_task_gets_the_post_merge_gate3_recheck_like_a_coders_would(tmp_path, monkeypatch):
    """Round 7 part C, site 6: the post-merge Gate 3 recheck (ASES-GIT-05) used to run only `if task.role ==
    "coder"`. A tester's real commit needs the same protection: a project-level regression another task's merge
    introduced on the shared tip must not go unchecked just because the task that exposed it is a tester."""
    plan, conn, project, pair, created = _setup_one_task(
        tmp_path, monkeypatch, plan_raw=_one_task_plan_raw("tester"), roles=dict(ROLES, tester="tester-1"),
    )
    _board_state(monkeypatch, pair, branch="swarm/T1-tester")
    actions = _record_card_actions(monkeypatch)
    _script_merge_task(monkeypatch, _MERGED)
    calls = _stub_post_merge_gate(monkeypatch, passed=True)

    merged = controller.process_merge_queue("b", tmp_path / "repo", plan, project, conn=conn)

    assert merged == ["T1"]
    assert len(calls) == 1  # the postcheck actually ran; the old role=="coder" gate would have left it None
    assert calls[0]["task_key"] == "T1"
    assert actions["complete"] == [(pair.merge_card_id, {"result": "merged cand1",
                                                          "metadata": {"squash_commit": "cand1"}})]


def test_tester_task_with_a_red_post_merge_gate_is_reverted_not_silently_kept(tmp_path, monkeypatch):
    """The red half of the same site: without the fix, a regression the tester's merge exposed would have
    landed on the integration branch for good, because the postcheck was None and never even asked -- proved
    empirically by reverting the six sites and re-running this file: this test then fails (merged == ["T1"]
    instead of [], no fix card, no post_merge_reverted event)."""
    plan, conn, project, pair, created = _setup_one_task(
        tmp_path, monkeypatch, plan_raw=_one_task_plan_raw("tester"), roles=dict(ROLES, tester="tester-1"),
    )
    _board_state(monkeypatch, pair, branch="swarm/T1-tester")
    actions = _record_card_actions(monkeypatch)
    _script_merge_task(monkeypatch, _MERGED)
    _stub_post_merge_gate(monkeypatch, passed=False, detail="a regression the tester's merge exposed")
    monkeypatch.setattr(mergeq, "revert_merge", lambda *a, **kw: mergeq.RevertOutcome(
        True, "revertsha1", "reverted cand1", aborted=False,
    ))

    merged = controller.process_merge_queue("b", tmp_path / "repo", plan, project, conn=conn)

    assert merged == []  # never merged: the branch it landed on turned out broken
    assert actions["complete"] == []  # the merge card is never completed on this path
    fix_cards = _fix_cards(created)
    assert len(fix_cards) == 1 and _task_row(conn)["fix_cards"] == 1
    kinds = [e["kind"] for e in events.recent(conn)]
    assert "post_merge_reverted" in kinds and "merge_failed" in kinds and "fix_card_created" in kinds


def test_process_merge_queue_without_an_integrity_list_still_stops_the_loop_on_an_unrepaired_revert(
    tmp_path, monkeypatch,
):
    """integrity is optional (the same shape as unreviewed): a caller that does not pass one still gets the loop
    stopped, just with nowhere to read the problem back from except the event."""
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    _board_state(monkeypatch, pair)
    _record_card_actions(monkeypatch)
    _script_merge_task(monkeypatch, _MERGED)
    _stub_post_merge_gate(monkeypatch, passed=False)
    monkeypatch.setattr(mergeq, "revert_merge", lambda *a, **kw: mergeq.RevertOutcome(False, None, "still broken"))

    merged = controller.process_merge_queue("b", tmp_path / "repo", plan, project, conn=conn)  # no integrity=

    assert merged == []
    assert [e["kind"] for e in events.recent(conn)].count("integrity_violation") == 1


# ---------------------------------------------------------------------------------------------
# Merge-time checks in front of merge_task (ASES-GIT-03, GIT-13, REV-05, REV-06, QG-01, 2026-09-19). review.py has
# its own tests for the checks themselves; these pin how process_merge_queue USES them.
# ---------------------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _pre_merge_check_passes(monkeypatch):
    """The merge queue now calls review.check_branch_for_merge before merge_task. Every test below that is about
    something else gets a passing check whose head is the real branch tip when the repository exists, so the
    real-git tests still merge the right commit. Tests about the check itself replace this stub."""
    def lenient(repo, branch, integration_branch, gate1_commands, touches, *, conn, task_key, **kwargs):
        head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "-q", branch], capture_output=True, text=True,
        ).stdout.strip() or "0" * 40
        return review_mod.BranchCheck(True, "ok", "stubbed for the merge-queue tests", head)

    monkeypatch.setattr(review_mod, "check_branch_for_merge", lenient)


def _stub_check(monkeypatch, result):
    seen = []

    def fake(repo, branch, integration_branch, gate1_commands, touches, *, conn, task_key, **kwargs):
        seen.append({"branch": branch, "integration_branch": integration_branch, "gate1_commands": gate1_commands,
                     "touches": touches, "task_key": task_key, **kwargs})
        return result

    monkeypatch.setattr(review_mod, "check_branch_for_merge", fake)
    return seen


def _work_card_runs(monkeypatch, pair, runs, **board_kwargs):
    states = _board_state(monkeypatch, pair, **board_kwargs)
    states[pair.work_card_id]["_runs"] = runs
    return states


def _reviewer_run(metadata):
    return {"outcome": "completed", "profile": "reviewer", "metadata": metadata}


def _refusals(conn, kind):
    return [json.loads(e["payload"]) for e in events.recent(conn, limit=200) if e["kind"] == kind]


def test_a_reviewer_completion_with_no_verdict_metadata_is_refused(tmp_path, monkeypatch):
    """ASES-REV-06: the verdict is a tool call with schema-checked metadata. A reviewer that completed the card
    without any is not a recorded PASS."""
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    _work_card_runs(monkeypatch, pair, [_reviewer_run(None)])
    _record_card_actions(monkeypatch)
    calls = _script_merge_task(monkeypatch, _MERGED)
    unreviewed = []

    merged = controller.process_merge_queue("b", tmp_path / "repo", plan, project, conn=conn, unreviewed=unreviewed)
    controller.process_merge_queue("b", tmp_path / "repo", plan, project, conn=conn)  # the next poll

    assert merged == [] and calls == [] and unreviewed == ["T1"]
    (event,) = _refusals(conn, "merge_refused_invalid_verdict")  # once, not once per poll
    assert event["card_id"] == pair.work_card_id and event["task_key"] == "T1" and event["problems"]
    assert _fix_cards(created) == [] and _task_row(conn)["fix_cards"] == 0  # a refusal is not a merge failure


def test_tester_task_with_no_verdict_metadata_is_refused_not_merged_blindly(tmp_path, monkeypatch):
    """Round 7 part C, site 4: the verdict-validation gate (ASES-REV-06) used to run only `if task.role ==
    "coder"`. Without the fix, a tester's card with no verdict metadata at all would skip straight past this
    check (pre_merge_outcome stays None) and merge_task would be called anyway, regardless of whether anyone
    ever actually reviewed the work -- proved empirically: reverting the six sites makes `calls` non-empty and
    this test fail."""
    plan, conn, project, pair, created = _setup_one_task(
        tmp_path, monkeypatch, plan_raw=_one_task_plan_raw("tester"), roles=dict(ROLES, tester="tester-1"),
    )
    _work_card_runs(monkeypatch, pair, [_reviewer_run(None)], branch="swarm/T1-tester")
    _record_card_actions(monkeypatch)
    calls = _script_merge_task(monkeypatch, _MERGED)
    unreviewed = []

    merged = controller.process_merge_queue("b", tmp_path / "repo", plan, project, conn=conn, unreviewed=unreviewed)

    assert merged == [] and calls == [] and unreviewed == ["T1"]  # merge_task was never even called
    (event,) = _refusals(conn, "merge_refused_invalid_verdict")
    assert event["card_id"] == pair.work_card_id and event["task_key"] == "T1"


@pytest.mark.parametrize("metadata", [
    {"review_outcome": "changes_needed"},  # not "approved": unusable, not a well-formed CHANGES_REQUIRED
    {"review_outcome": "approved", "review_status": "CHANGES_REQUIRED"},  # contradiction is never read as PASS
    {"review_outcome": "approved", "commit": "not-hex"},
    "not json at all",
])
def test_a_malformed_verdict_is_refused(tmp_path, monkeypatch, metadata):
    """Kept under its round 5 name and shape: these four are genuinely malformed (validate_verdict.valid is
    False), the ONLY case that still takes the merge_refused_invalid_verdict path. A well-formed CHANGES_REQUIRED
    or BLOCKED verdict is a different, round 6 case: see test_a_valid_changes_required_or_blocked_verdict below."""
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    _work_card_runs(monkeypatch, pair, [_reviewer_run(metadata)])
    actions = _record_card_actions(monkeypatch)
    calls = _script_merge_task(monkeypatch, _MERGED)

    assert controller.process_merge_queue("b", tmp_path / "repo", plan, project, conn=conn) == []

    assert calls == [] and actions["reopen_review"] == []
    assert len(_refusals(conn, "merge_refused_invalid_verdict")) == 1


@pytest.mark.parametrize("metadata,outcome", [
    ({"review_status": "CHANGES_REQUIRED", "required_changes": ["add a test"]}, "CHANGES_REQUIRED"),
    ({"review_status": "BLOCKED"}, "BLOCKED"),
])
def test_a_valid_changes_required_or_blocked_verdict_reopens_the_card_instead_of_refusing_it(
    tmp_path, monkeypatch, metadata, outcome,
):
    """Found by the FK builder (round 6): a reviewer that calls kanban_complete with a CHANGES_REQUIRED or
    BLOCKED verdict, instead of kanban_request_changes/kanban_block, used to be refused forever as an "invalid"
    verdict -- the card was `done`, refused every poll, with no path back to its implementer. The verdict is well
    formed (validate_verdict.valid is True here), so it is treated as if the reviewer had used
    kanban_reopen_review: back to the implementer, never the unreviewed-refusal path."""
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    _work_card_runs(monkeypatch, pair, [_reviewer_run(metadata)])
    actions = _record_card_actions(monkeypatch)
    calls = _script_merge_task(monkeypatch, _MERGED)
    unreviewed = []

    merged = controller.process_merge_queue(
        "b", tmp_path / "repo", plan, project, conn=conn, unreviewed=unreviewed,
    )

    assert merged == [] and calls == [] and unreviewed == []  # never merged, and not the unreviewed path either
    # _reviewer_run sets no run "summary", so the reason falls back to the default text (see the next test for a
    # run that DOES carry one).
    assert actions["reopen_review"] == [(pair.work_card_id, f"reviewer completed the card with a {outcome} verdict")]
    assert actions["complete"] == [] and actions["block"] == []  # never completed, never blocked directly
    assert _refusals(conn, "merge_refused_invalid_verdict") == []  # NOT the malformed-verdict path
    (event,) = _refusals(conn, "reviewer_completed_with_changes_requested")
    assert event == {"task_key": "T1", "card_id": pair.work_card_id, "outcome": outcome}
    assert _fix_cards(created) == [] and _task_row(conn)["fix_cards"] == 0  # not a merge failure either


def test_a_changes_required_verdict_uses_the_runs_own_summary_as_the_reopen_reason(tmp_path, monkeypatch):
    """Verdict has no summary field of its own (validate_verdict's schema check does not carry free text); the
    reason comes from the run's own "summary" (r2_rules.md: kanban_show's _runs carry one), which the
    Verdict-shaped metadata used in the parametrized test above happens not to set for BLOCKED."""
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    run = _reviewer_run({"review_status": "CHANGES_REQUIRED"})
    run["summary"] = "FAIL: the retry loop has no backoff"
    _work_card_runs(monkeypatch, pair, [run])
    actions = _record_card_actions(monkeypatch)
    _script_merge_task(monkeypatch, _MERGED)

    controller.process_merge_queue("b", tmp_path / "repo", plan, project, conn=conn)

    assert actions["reopen_review"] == [(pair.work_card_id, "FAIL: the retry loop has no backoff")]


@pytest.mark.parametrize("metadata", [
    {"review_outcome": "approved"},                                  # what the Hermes review skill emits
    {"review_status": "PASS", "summary": "fine", "test_gaps": []},   # the blueprint's shape (section 13.3)
    json.dumps({"review_outcome": "approved"}),                      # metadata as a JSON string
])
def test_both_verdict_shapes_and_a_json_string_are_accepted_as_a_pass(tmp_path, monkeypatch, metadata):
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    _work_card_runs(monkeypatch, pair, [_reviewer_run(metadata)])
    _record_card_actions(monkeypatch)
    calls = _script_merge_task(monkeypatch, _MERGED)

    assert controller.process_merge_queue("b", tmp_path / "repo", plan, project, conn=conn) == ["T1"]

    assert len(calls) == 1


def test_the_merge_time_check_gets_the_tasks_own_gate_commands_touches_and_branch(tmp_path, monkeypatch):
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    _board_state(monkeypatch, pair)
    _record_card_actions(monkeypatch)
    _script_merge_task(monkeypatch, _MERGED)
    seen = _stub_check(monkeypatch, review_mod.BranchCheck(True, "ok", "fine", "abc123def456"))

    controller.process_merge_queue("b", tmp_path / "repo", plan, project, conn=conn)

    assert len(seen) == 1
    assert {k: seen[0][k] for k in ("branch", "integration_branch", "gate1_commands", "touches", "task_key")} == {
        "branch": "swarm/T1-coder", "integration_branch": "integration",
        "gate1_commands": ["echo ok"], "touches": ["base.txt"], "task_key": "T1"}
    assert seen[0]["require_binding"] is True  # the merge queue always asks for the approval to be bound
    assert seen[0]["allow_gate_config_changes"] is False  # ONE_TASK_PLAN's task does not set the marker


def test_the_merge_time_check_gets_the_tasks_allow_gate_config_changes_marker(tmp_path, monkeypatch):
    """ASES-QG-02 (round 9, CIPIN): process_merge_queue must forward plan_mod.PlanTask.allow_gate_config_changes
    to review.check_branch_for_merge, the merge queue's own authoritative tamper check, exactly as
    process_review_lane forwards it to gate_before_review."""
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    task = plan.task("T1")
    plan = dataclasses.replace(plan, tasks=(dataclasses.replace(task, allow_gate_config_changes=True),))
    _board_state(monkeypatch, pair)
    _record_card_actions(monkeypatch)
    _script_merge_task(monkeypatch, _MERGED)
    seen = _stub_check(monkeypatch, review_mod.BranchCheck(True, "ok", "fine", "abc123def456"))

    controller.process_merge_queue("b", tmp_path / "repo", plan, project, conn=conn)

    assert len(seen) == 1
    assert seen[0]["allow_gate_config_changes"] is True


def test_merge_task_is_told_the_exact_commit_that_was_checked(tmp_path, monkeypatch):
    """The time-of-check gap: a commit pushed after the check must not ride in unchecked."""
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    _board_state(monkeypatch, pair)
    _record_card_actions(monkeypatch)
    calls = _script_merge_task(monkeypatch, _MERGED)
    _stub_check(monkeypatch, review_mod.BranchCheck(True, "ok", "fine", "abc123def456"))

    controller.process_merge_queue("b", tmp_path / "repo", plan, project, conn=conn)

    assert calls[0]["expected_head"] == "abc123def456"


@pytest.mark.parametrize("kind", ["out_of_scope", "gate1_red", "stale_review", "unresolvable_branch", "no_merge_base"])
def test_a_failed_merge_time_check_takes_the_ordinary_failure_path_and_never_merges(tmp_path, monkeypatch, kind):
    """A red result is a capability failure, not a refusal: merge_failed, a fix card carrying the exact reason,
    bounded by fix_cards_per_task. merge_task is never reached."""
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    _board_state(monkeypatch, pair)
    _record_card_actions(monkeypatch)
    calls = _script_merge_task(monkeypatch, _MERGED)
    _stub_check(monkeypatch, review_mod.BranchCheck(False, kind, f"detail for {kind}", "abc123def456"))

    merged = controller.process_merge_queue("b", tmp_path / "repo", plan, project, conn=conn)

    assert merged == [] and calls == []
    (failed,) = _refusals(conn, "merge_failed")
    assert kind in failed["detail"] and f"detail for {kind}" in failed["detail"]
    (fix_card,) = _fix_cards(created)
    assert f"detail for {kind}" in fix_card["body"]
    assert _task_row(conn)["fix_cards"] == 1


def test_a_failed_check_after_the_fix_budget_is_spent_blocks_the_merge_card(tmp_path, monkeypatch):
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch, fix_cards_per_task=0)
    _board_state(monkeypatch, pair)
    actions = _record_card_actions(monkeypatch)
    _script_merge_task(monkeypatch, _MERGED)
    _stub_check(monkeypatch, review_mod.BranchCheck(False, "out_of_scope", "stray path", "abc123def456"))

    controller.process_merge_queue("b", tmp_path / "repo", plan, project, conn=conn)

    assert [cid for cid, _ in actions["ask"]] == [pair.merge_card_id]
    assert actions["block"] == []
    assert _fix_cards(created) == []


def test_a_verdict_quoting_a_different_commit_than_the_checked_head_is_refused(tmp_path, monkeypatch):
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    _work_card_runs(monkeypatch, pair, [_reviewer_run({"review_status": "PASS", "commit": "aaaaaaa"})])
    _record_card_actions(monkeypatch)
    calls = _script_merge_task(monkeypatch, _MERGED)
    _stub_check(monkeypatch, review_mod.BranchCheck(True, "ok", "fine", "bbbbbbbb" + "0" * 32))

    assert controller.process_merge_queue("b", tmp_path / "repo", plan, project, conn=conn) == []

    assert calls == []
    (event,) = _refusals(conn, "merge_refused_verdict_commit_mismatch")
    assert event["reviewed_commit"] == "aaaaaaa" and event["branch_head"].startswith("bbbbbbbb")


def test_a_verdict_quoting_a_prefix_of_the_checked_head_merges(tmp_path, monkeypatch):
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    _work_card_runs(monkeypatch, pair, [_reviewer_run({"review_status": "PASS", "commit": "bbbbbbb"})])
    _record_card_actions(monkeypatch)
    calls = _script_merge_task(monkeypatch, _MERGED)
    _stub_check(monkeypatch, review_mod.BranchCheck(True, "ok", "fine", "bbbbbbbb" + "0" * 32))

    assert controller.process_merge_queue("b", tmp_path / "repo", plan, project, conn=conn) == ["T1"]

    assert len(calls) == 1


def test_an_accepted_verdict_is_stored_by_commit_sha_exactly_once(tmp_path, monkeypatch):
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    _work_card_runs(monkeypatch, pair, [_reviewer_run({"review_outcome": "approved", "reviewer_checks": ["ok"]})])
    _record_card_actions(monkeypatch)
    _script_merge_task(monkeypatch, _MERGED)
    head = "c" * 40
    _stub_check(monkeypatch, review_mod.BranchCheck(True, "ok", "fine", head))

    controller.process_merge_queue("b", tmp_path / "repo", plan, project, conn=conn)
    controller.process_merge_queue("b", tmp_path / "repo", plan, project, conn=conn)  # a retry stores nothing new

    rows = conn.execute("SELECT * FROM review_verdicts").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert (row["project"], row["task_key"], row["commit_sha"], row["card_id"], row["outcome"],
            row["reviewer_profile"]) == (plan.project, "T1", head, pair.work_card_id, "PASS", "reviewer")
    assert "approved" in row["metadata"]


def test_a_refused_verdict_is_not_stored(tmp_path, monkeypatch):
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    _work_card_runs(monkeypatch, pair, [_reviewer_run(None)])
    _record_card_actions(monkeypatch)
    _script_merge_task(monkeypatch, _MERGED)

    controller.process_merge_queue("b", tmp_path / "repo", plan, project, conn=conn)

    assert conn.execute("SELECT COUNT(*) AS n FROM review_verdicts").fetchone()["n"] == 0


def test_a_reviewer_role_task_needs_no_verdict_and_no_branch_check(tmp_path, monkeypatch):
    """A reviewer-role task has no diff, no Gate 1 record and no verdict in the review-lane schema: its merge is
    the recorded no-op, and none of the coder-only checks apply."""
    repo = _repo_with_branch_at_tip(tmp_path, "swarm/T1-reviewer")
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch, plan_raw=_one_task_plan_raw("reviewer"))
    _work_card_runs(monkeypatch, pair, [_reviewer_run(None)], branch="swarm/T1-reviewer")
    _record_card_actions(monkeypatch)

    def _forbidden(*a, **kw):
        raise AssertionError("no branch check for a reviewer-role task")

    monkeypatch.setattr(review_mod, "check_branch_for_merge", _forbidden)

    assert controller.process_merge_queue("b", repo, plan, project, conn=conn) == ["T1"]

    assert _refusals(conn, "merge_refused_invalid_verdict") == []


# ---------------------------------------------------------------------------------------------
# The approval is bound to a commit (ASES-GIT-03), found by an independent nemotron review: the Hermes review
# skill's verdict has no commit field, so the reviewed commit comes from the verdict when it quotes one and from
# the coder's hand-off otherwise.
# ---------------------------------------------------------------------------------------------

def _handoff_run(commit=None, key="commit_sha", as_json=False):
    metadata = {key: commit} if commit is not None else {"summary": "done"}
    return {"outcome": "review_requested", "profile": "coder-1", "metadata": json.dumps(metadata) if as_json else metadata}


def _reviewed_commit_passed(tmp_path, monkeypatch, runs):
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    _work_card_runs(monkeypatch, pair, runs)
    _record_card_actions(monkeypatch)
    _script_merge_task(monkeypatch, _MERGED)
    seen = _stub_check(monkeypatch, review_mod.BranchCheck(True, "ok", "fine", "a" * 40))
    controller.process_merge_queue("b", tmp_path / "repo", plan, project, conn=conn)
    return seen[0]["reviewed_commit"]


def test_the_reviewed_commit_is_the_one_the_verdict_quotes(tmp_path, monkeypatch):
    runs = [_handoff_run("1111111"), _reviewer_run({"review_status": "PASS", "commit": "aaaaaaa"})]

    assert _reviewed_commit_passed(tmp_path, monkeypatch, runs) == "aaaaaaa"  # the verdict wins over the hand-off


def test_without_a_verdict_commit_the_reviewed_commit_is_the_one_the_coder_handed_off(tmp_path, monkeypatch):
    runs = [_handoff_run("a" * 40), _reviewer_run({"review_outcome": "approved"})]

    assert _reviewed_commit_passed(tmp_path, monkeypatch, runs) == "a" * 40


@pytest.mark.parametrize("handoff", [
    _handoff_run("abcdef1", key="commit"),              # the alternative field name
    _handoff_run("abcdef1", as_json=True),              # metadata that arrives as a JSON string
])
def test_the_hand_off_commit_is_found_under_either_field_and_as_a_json_string(tmp_path, monkeypatch, handoff):
    runs = [handoff, _reviewer_run({"review_outcome": "approved"})]

    assert _reviewed_commit_passed(tmp_path, monkeypatch, runs) == "abcdef1"


def test_the_latest_hand_off_is_the_one_that_counts(tmp_path, monkeypatch):
    """A changes-requested round produces a second hand-off: the approval is for the LAST commit handed off."""
    runs = [_handoff_run("1111111"), _handoff_run("2222222"), _reviewer_run({"review_outcome": "approved"})]

    assert _reviewed_commit_passed(tmp_path, monkeypatch, runs) == "2222222"


@pytest.mark.parametrize("handoff_runs", [
    [],                                                  # no hand-off at all
    [_handoff_run(None)],                                # a hand-off that names no commit
    [_handoff_run("not-a-sha")],                         # not hex
    [_handoff_run("abc")],                               # too short to identify a commit
    [{"outcome": "review_requested", "profile": "coder-1", "metadata": "{broken"}],
    [{"outcome": "review_requested", "profile": "coder-1", "metadata": None}],
    [{"outcome": "review_requested", "profile": "coder-1", "metadata": ["a" * 40]}],
])
def test_no_commit_anywhere_passes_none_so_the_check_can_refuse_an_unbound_approval(tmp_path, monkeypatch, handoff_runs):
    runs = [*handoff_runs, _reviewer_run({"review_outcome": "approved"})]

    assert _reviewed_commit_passed(tmp_path, monkeypatch, runs) is None


def test_an_unbound_approval_takes_the_failure_path_and_never_merges(tmp_path, monkeypatch):
    plan, conn, project, pair, created = _setup_one_task(tmp_path, monkeypatch)
    _board_state(monkeypatch, pair)
    _record_card_actions(monkeypatch)
    calls = _script_merge_task(monkeypatch, _MERGED)
    _stub_check(monkeypatch, review_mod.BranchCheck(False, "unbound_review", "cannot bind", "a" * 40))

    assert controller.process_merge_queue("b", tmp_path / "repo", plan, project, conn=conn) == []

    assert calls == []
    assert "unbound_review" in _refusals(conn, "merge_failed")[0]["detail"]
