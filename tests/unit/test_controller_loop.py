"""Loop version 2 of the controller (round 5): the halt flag, the merge queue's question and stop handling, failure
recovery (fresh attempts, model switches, re-plans, spent budgets), unparking, bounds and pause, provisioning, idle
worktrees, the final gates, and the order of it all in run_pass.

Everything runs against FakeBoard, an in-memory Hermes board behind the hermes.kanban_* wrappers, and a temp SQLite
database; the few tests that need git use real temp repositories. The autouse fixture below fails any test that
reaches a real `hermes` command, so nothing here can touch a real board."""
import copy
import dataclasses
import json
import os
import pathlib
import subprocess
import sys
import types
from datetime import datetime, timezone

import pytest

from ases import bounds, config, controller, db, events, guards, hermes, intents, ledger, mergeq
from ases import gates as gates_mod
from ases import plan as plan_mod
from ases import questions, recovery, report
from ases import review as review_mod
from ases import sandbox as sandbox_mod
from ases import usage as usage_mod

ROOT = pathlib.Path(__file__).resolve().parents[2]
ROLES = {"lead": "lead", "coder": "coder-1", "reviewer": "reviewer"}

PLAN_RAW = {
    "project": "t3", "integration_branch": "integration",
    "gate_profiles": {"trivial": ["echo ok"]},
    "tasks": [
        {"key": "T1", "title": "scaffold", "role": "coder", "depends_on": [], "touches": ["a.py"],
         "acceptance": ["exists", "is tested"], "gate_profile": "trivial", "estimated_requests": 10},
        {"key": "T2", "title": "extend", "role": "coder", "depends_on": ["T1"], "touches": ["b.py"],
         "acceptance": ["extended"], "gate_profile": "trivial", "estimated_requests": 10},
    ],
}

MODELS = {
    "providers": {
        "xkiro": {"limits": {}},
        "openrouter": {"limits": {"per_day_default": 50, "per_day_after_credits": 1000}, "credits_purchased": False},
    },
    "models": [
        {"provider": "xkiro", "model": "coder-m", "role_class": "coder", "pinned": True},
        {"provider": "xkiro", "model": "coder-alt", "role_class": "coder_candidate"},
        {"provider": "openrouter", "model": "rev-m", "role_class": "reviewer", "pinned": True},
    ],
}

# A coder whose provider is capped, so that its own budget can run out (ledger.record_usage 50 on it).
CAPPED_MODELS = {
    "providers": MODELS["providers"],
    "models": [
        {"provider": "openrouter", "model": "coder-m", "role_class": "coder", "pinned": True},
        {"provider": "openrouter", "model": "rev-m", "role_class": "reviewer", "pinned": True},
    ],
}

REVIEWER_COMPLETED = {
    "id": 5, "outcome": "completed", "profile": "reviewer",
    "metadata": {"review_outcome": "approved", "reviewer_checks": ["read the diff"]},
}
_MERGED = mergeq.MergeOutcome(True, "cand1", "cand1", "pass", "merged")
_NO_OP = mergeq.MergeOutcome(True, None, None, "skipped", "no changes to merge (review-only task)")
_CONFLICT = mergeq.MergeOutcome(False, None, None, None, "merge conflict: CONFLICT (content): Merge conflict in a.py")
_STOPPED = mergeq.MergeOutcome(False, None, None, None, "stopped by the kill switch before Gate 3", stopped=True)

SECRET = "sk-" + "a1B2c3D4e5" * 4  # a secret-shaped value: the redactor's `sk-` shape, 43 characters


@pytest.fixture(autouse=True)
def _no_real_hermes(monkeypatch):
    """Any command that would really reach Hermes fails the test at teardown, even if the code under test swallowed
    the exception it raised."""
    reached = []

    def refuse(args, timeout=60):
        reached.append(list(args))
        raise AssertionError(f"a test reached a real hermes command: {args}")

    monkeypatch.setattr(hermes, "_run", refuse)
    yield
    assert reached == []


@pytest.fixture(autouse=True)
def _post_merge_check_passes(monkeypatch):
    """Round 6 (ASES-GIT-05): process_merge_queue re-runs Gate 3 on the new integration HEAD right after a real
    merge, through gates_mod.run_gate. Most tests here script mergeq.merge_task (script_merge) rather than using a
    real repository, so a fake squash_commit such as "cand1" is never a real commit gates_mod.run_gate could check
    out. Tests about the post-merge check itself replace this stub (see stub_post_merge_gate)."""
    def lenient(repo, commit_sha, gate_name, commands, *, conn=None, task_key="", project=None,
                timeout_per_command=120, runner=None):
        return gates_mod.GateResult(gate_name, commit_sha, True, "ok")

    monkeypatch.setattr(gates_mod, "run_gate", lenient)


def stub_post_merge_gate(monkeypatch, *, passed, detail="gate3-postmerge output"):
    """Replace gates_mod.run_gate with a recorder that answers `passed`/`detail` and remembers every call."""
    calls = []

    def fake(repo, commit_sha, gate_name, commands, *, conn=None, task_key="", project=None,
              timeout_per_command=120, runner=None):
        calls.append({"commit_sha": commit_sha, "gate_name": gate_name, "task_key": task_key, "project": project})
        return gates_mod.GateResult(gate_name, commit_sha, passed, detail)

    monkeypatch.setattr(gates_mod, "run_gate", fake)
    return calls


def failed_run(run_id=1, error="protocol violation: the worker exited without a terminal kanban call",
               profile="coder-1"):
    """A run recovery classifies as a capability failure (Hermes's marker for a worker that ended without a terminal
    kanban call)."""
    return {"id": run_id, "profile": profile, "status": "crashed", "outcome": "crashed", "summary": None,
            "error": error, "metadata": {}, "worker_pid": None, "started_at": 1000, "ended_at": 1100}


class FakeBoard:
    """An in-memory Hermes board. The hermes.kanban_* wrappers the controller uses are replaced by methods of this
    object, which keep every card as the dict kanban_show returns (`_runs`, `_events`, `_comments`, `_parents`) and
    record each call as (name, args, kwargs) in `calls`. `fail[name]` makes every call of that wrapper raise;
    `fail_after[name] = (n, exception)` lets n calls through first; `hooks[name]` runs before a call is performed.
    kanban_create is idempotent by key like the real one, and refuses nothing else."""

    def __init__(self, monkeypatch):
        self.cards = {}
        self.calls = []
        self.fail = {}
        self.fail_after = {}
        self.hooks = {}
        self.keys = {}
        self.counter = 0
        self.tick = 1_000
        for name in ("show", "list", "create", "link", "archive", "set_model", "comment", "block", "unblock",
                     "schedule", "complete", "dispatch"):
            monkeypatch.setattr(hermes, f"kanban_{name}", getattr(self, f"_{name}"))
        monkeypatch.setattr(hermes, "pause", self._pause)

    # -- bookkeeping ------------------------------------------------------------------------------------------

    def _enter(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        hook = self.hooks.get(name)
        if hook is not None:
            hook(*args, **kwargs)
        if name in self.fail:
            raise self.fail[name]
        if name in self.fail_after:
            allowed, exc = self.fail_after[name]
            if sum(1 for call in self.calls if call[0] == name) > allowed:
                raise exc

    def now(self):
        self.tick += 1
        return self.tick

    def writes(self):
        """The names of the calls that change the board, in order."""
        return [name for name, _, _ in self.calls if name not in ("kanban_show", "kanban_list")]

    def calls_of(self, name):
        return [(args, kwargs) for n, args, kwargs in self.calls if n == name]

    def add(self, card_id, status="ready", **fields):
        card = {
            "id": card_id, "title": fields.pop("title", card_id), "status": status, "assignee": None,
            "project_id": None, "branch_name": None, "workspace_path": None, "model_override": None,
            "provider_override": None, "_runs": [], "_events": [], "_comments": [], "_parents": [],
            "_children": [], "_latest_summary": None,
        }
        card.update(fields)
        self.cards[card_id] = card
        return card

    def add_event(self, card_id, kind, payload=None, run_id=None):
        self.cards[card_id]["_events"].append(
            {"kind": kind, "payload": payload or {}, "created_at": self.now(), "run_id": run_id})

    def add_comment(self, card_id, author, body):
        self.cards[card_id]["_comments"].append({"author": author, "body": body, "created_at": self.now()})

    def _flat(self, card_id):
        return {k: v for k, v in copy.deepcopy(self.cards[card_id]).items() if not k.startswith("_")}

    # -- the wrappers -----------------------------------------------------------------------------------------

    def _show(self, board, card_id):
        self._enter("kanban_show", board, card_id)
        if card_id not in self.cards:
            raise hermes.HermesCommandError(["kanban", "show", card_id], 1, "no such task")
        return copy.deepcopy(self.cards[card_id])

    def _list(self, board, *, status=None, assignee=None):
        self._enter("kanban_list", board, status=status)
        return [self._flat(cid) for cid, card in self.cards.items() if status is None or card["status"] == status]

    def _create(self, board, title, **kwargs):
        self._enter("kanban_create", board, title, **kwargs)
        key = kwargs.get("idempotency_key")
        if key in self.keys and self.cards[self.keys[key]]["status"] != "archived":
            return self._flat(self.keys[key])
        self.counter += 1
        card_id = f"n{self.counter}"
        parents = list(kwargs.get("parent") or [])
        if kwargs.get("initial_status") == "blocked":
            status = "blocked"
        elif any(self.cards.get(p, {}).get("status") != "done" for p in parents):
            status = "todo"
        else:
            status = "ready"
        self.add(card_id, status=status, title=title, assignee=kwargs.get("assignee"),
                 project_id=kwargs.get("project"), branch_name=kwargs.get("branch"), _parents=parents,
                 created_kwargs=kwargs)
        if status == "blocked":  # what Hermes 0.21.3 really writes for a card created blocked
            self.add_event(card_id, "blocked", {"reason": "initial_status", "status": "blocked", "actor": "user"})
        if key:
            self.keys[key] = card_id
        return self._flat(card_id)

    def _link(self, board, parent_id, child_id):
        self._enter("kanban_link", board, parent_id, child_id)
        if parent_id not in self.cards[child_id]["_parents"]:
            self.cards[child_id]["_parents"].append(parent_id)

    def _archive(self, board, card_ids):
        self._enter("kanban_archive", board, list(card_ids))
        for card_id in card_ids:
            if self.cards[card_id]["status"] == "archived":
                raise hermes.HermesCommandError(["kanban", "archive", card_id], 1, f"cannot archive {card_id}")
            self.cards[card_id]["status"] = "archived"

    def _set_model(self, board, card_id, model, *, provider=None):
        self._enter("kanban_set_model", board, card_id, model, provider=provider)
        self.cards[card_id]["model_override"] = model
        self.cards[card_id]["provider_override"] = provider

    def _comment(self, board, card_id, text, *, author=None):
        self._enter("kanban_comment", board, card_id, text, author=author)
        self.add_comment(card_id, author or "default", text)

    def _block(self, board, card_id, reason, *, kind=None):
        self._enter("kanban_block", board, card_id, reason, kind=kind)
        self.cards[card_id]["status"] = "blocked"
        self.add_event(card_id, "blocked", {"reason": reason, "kind": kind})

    def _unblock(self, board, card_id, reason=None):
        self._enter("kanban_unblock", board, card_id, reason=reason)
        self.cards[card_id]["status"] = "ready"
        self.add_event(card_id, "unblocked", {})

    def _schedule(self, board, card_id, reason):
        self._enter("kanban_schedule", board, card_id, reason)
        self.cards[card_id]["status"] = "scheduled"
        self.add_event(card_id, "scheduled", {"reason": reason})

    def _complete(self, board, card_id, *, result=None, metadata=None):
        self._enter("kanban_complete", board, card_id, result=result, metadata=metadata)
        self.cards[card_id]["status"] = "done"

    def _dispatch(self, board, **kwargs):
        self._enter("kanban_dispatch", board, **kwargs)
        return {}

    def _pause(self, reason=None, timeout=20):
        self._enter("pause", reason)


@dataclasses.dataclass
class World:
    board: FakeBoard
    plan: plan_mod.Plan
    conn: object
    project: config.ProjectConfig
    pairs: dict
    repo: pathlib.Path

    def work(self, key="T1"):
        return self.pairs[key].work_card_id

    def merge(self, key="T1"):
        return self.pairs[key].merge_card_id

    def row(self, key="T1"):
        return self.conn.execute(
            "SELECT work_card_id, merge_card_id, fix_cards FROM plan_tasks WHERE project = 't3' AND task_key = ?",
            (key,),
        ).fetchone()


def make_world(tmp_path, monkeypatch, *, budgets=None, plan_raw=None):
    board = FakeBoard(monkeypatch)
    plan = plan_mod.parse_and_validate(plan_raw or PLAN_RAW, known_roles=set(ROLES), max_cards=40)
    conn = db.connect(tmp_path / "ases.db")
    project = config.ProjectConfig(
        name="t3", environment="native", data_class="public", workspace_root=tmp_path / "ws",
        ases_home=tmp_path / "home", board="b", integration_branch="integration", roles=ROLES, concurrency={},
        budgets=dict(budgets or {}), hermes_tested_version="0.21.3", hermes_native_home=tmp_path / "hermes",
    )
    repo = tmp_path / "repo"
    pairs = {p.task_key: p for p in controller.create_cards_from_plan("b", "proj1", repo, plan, project, conn=conn)}
    return World(board, plan, conn, project, pairs, repo)


def kinds(conn):
    return [row["kind"] for row in conn.execute("SELECT kind FROM events ORDER BY id")]


def payloads(conn, kind):
    return [json.loads(row["payload"]) for row in conn.execute(
        "SELECT payload FROM events WHERE kind = ? ORDER BY id", (kind,))]


def git(*args, cwd):
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


def git_repo(tmp_path, *, with_branch=True):
    """A real repository on `integration`, with (by default) the failed attempt's branch swarm/T1-coder holding one
    commit that adds new.txt."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-q", "-b", "integration", cwd=repo)
    git("config", "user.email", "t@t", cwd=repo)
    git("config", "user.name", "t", cwd=repo)
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    if with_branch:
        git("checkout", "-q", "-b", "swarm/T1-coder", cwd=repo)
        (repo / "new.txt").write_text("hello from the failed attempt\n", encoding="utf-8")
        git("add", "-A", cwd=repo)
        git("commit", "-q", "-m", "attempt", cwd=repo)
        git("checkout", "-q", "integration", cwd=repo)
    return repo


def fail_work_card(w, key="T1", *, run=None, extra_runs=()):
    """Put a task's work card where Hermes leaves a card it gave up on: blocked, with a failed last run."""
    card = w.board.cards[w.work(key)]
    card["status"] = "blocked"
    card["branch_name"] = f"swarm/{key}-coder"
    card["project_id"] = "p_9"
    card["_runs"] = [run or failed_run(), *extra_runs]
    w.board.add_event(w.work(key), "gave_up", {"failures": 1, "effective_limit": 1, "error": "x",
                                                "trigger_outcome": "crashed"}, run_id=1)
    return card


def decision(w, action, *, key="T1", run_id=1, kind=recovery.FailureKind.CAPABILITY, model=None, provider=None,
             reason="the attempt was wrong"):
    return recovery.Decision(action, reason, model=model, provider=provider, task_key=key, card_id=w.work(key),
                             run_id=run_id, failure_kind=kind)


class Asker:
    """A stand-in for questions.ask_user that records what was asked, as (card id, text)."""

    def __init__(self, monkeypatch, answer="commented"):
        self.asked = []
        self.answer = answer
        monkeypatch.setattr(questions, "ask_user", self._ask, raising=False)

    def _ask(self, board, card, text, *, conn=None, author="ases"):
        self.asked.append((card["id"], text))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


# =============================================================================================================
# create_cards_from_plan
# =============================================================================================================

def test_create_cards_gives_work_cards_max_retries_from_the_attempts_budget(tmp_path, monkeypatch):
    """Table 17: "Attempts per card 3 (--max-retries 3)". Hermes gives up after 2 by default, and a mismatch stalls a
    card silently. Only the work card is a retry-bounded worker card: the merge card is controller-owned."""
    w = make_world(tmp_path, monkeypatch, budgets={"attempts_per_card": 5})

    created = [kw for _, kw in w.board.calls_of("kanban_create")]
    work = [kw for kw in created if kw["idempotency_key"].startswith("ases-work-")]
    merge = [kw for kw in created if kw["idempotency_key"].startswith("ases-merge-")]
    assert len(work) == 2 and all(kw["max_retries"] == 5 for kw in work)
    assert len(merge) == 2 and all("max_retries" not in kw for kw in merge)


def test_create_cards_defaults_max_retries_to_the_blueprint_value_of_three(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)

    work = [kw for _, kw in w.board.calls_of("kanban_create") if kw["idempotency_key"].startswith("ases-work-")]
    assert [kw["max_retries"] for kw in work] == [3, 3]


def test_create_cards_records_and_completes_a_create_cards_intent(tmp_path, monkeypatch):
    """ASES-REC-03/04: "create cards" is one of the multi-step actions that write an intent before and a completion
    after; the key is the project name, since the action spans the whole plan."""
    w = make_world(tmp_path, monkeypatch)

    rows = w.conn.execute("SELECT project, kind, key, completed_at FROM intents").fetchall()
    assert [(r["project"], r["kind"], r["key"]) for r in rows] == [("t3", "create_cards", "t3")]
    assert rows[0]["completed_at"]
    assert intents.open_intents(w.conn, "t3") == []


def test_create_cards_leaves_the_intent_open_when_it_dies_half_way(tmp_path, monkeypatch):
    """A crash in the middle of the loop is exactly what reconcile-on-start looks for: the intent stays open."""
    board = FakeBoard(monkeypatch)
    board.fail_after["kanban_create"] = (3, hermes.HermesCommandError(["kanban", "create"], 1, "hermes went away"))
    plan = plan_mod.parse_and_validate(PLAN_RAW, known_roles=set(ROLES), max_cards=40)
    conn = db.connect(tmp_path / "ases.db")
    project = config.ProjectConfig(
        name="t3", environment="native", data_class="public", workspace_root=tmp_path / "ws",
        ases_home=tmp_path / "home", board="b", integration_branch="integration", roles=ROLES, concurrency={},
        budgets={}, hermes_tested_version="0.21.3", hermes_native_home=tmp_path / "hermes",
    )

    with pytest.raises(hermes.HermesCommandError):
        controller.create_cards_from_plan("b", "proj1", tmp_path / "repo", plan, project, conn=conn)

    (open_intent,) = intents.open_intents(conn, "t3")
    assert (open_intent["kind"], open_intent["key"]) == ("create_cards", "t3")


# =============================================================================================================
# the halt flag
# =============================================================================================================

def test_halted_is_false_without_a_state_row_and_for_every_running_status(tmp_path):
    conn = db.connect(tmp_path / "ases.db")
    assert controller._halted(conn, "t3") == (False, None)
    for status in ("planning", "running", "finished"):
        bounds.set_status(conn, "t3", status)
        assert controller._halted(conn, "t3") == (False, None)


def test_halted_reads_a_stopped_project_and_its_reason(tmp_path):
    conn = db.connect(tmp_path / "ases.db")
    bounds.set_status(conn, "t3", "stopped", "swarm stop")

    assert controller._halted(conn, "t3") == (True, "swarm stop")


def test_halted_reads_a_paused_projects_reason_from_project_state(tmp_path):
    """ASES-CTL-01, the register's fixed known gap: bounds.set_status now keeps a reason for `paused` the same
    way it does for `stopped`, so _halted reads it straight from project_state and never needs the
    project_paused event pause_and_report also records."""
    conn = db.connect(tmp_path / "ases.db")
    bounds.set_status(conn, "t3", "paused", "wall clock reached")

    assert controller._halted(conn, "t3") == (True, "wall clock reached")


def test_halted_falls_back_to_the_project_paused_event_when_project_state_has_no_reason(tmp_path):
    """A `paused` row with no stop_reason (written directly, or by a caller that passed none) still gets its
    reason from the newest project_paused event; only when neither has one does it fall back to the bare status.
    This is the fallback chain _pause_reason exists for, kept for rows older than the fix above."""
    conn = db.connect(tmp_path / "ases.db")
    bounds.set_status(conn, "t3", "paused")
    assert controller._halted(conn, "t3") == (True, "the project is paused")

    events.record(conn, "project_paused", {"project": "someone-else", "reason": "not mine"})
    events.record(conn, "project_paused", {"project": "t3", "reason": "wall clock reached"})

    assert controller._halted(conn, "t3") == (True, "wall clock reached")


# =============================================================================================================
# the merge queue: questions, stops, redaction, intents, tamper
# =============================================================================================================

def stub_check(monkeypatch, result=None):
    """review.check_branch_for_merge passes (or returns `result`), whatever the repository says."""
    monkeypatch.setattr(
        review_mod, "check_branch_for_merge",
        lambda *a, **kw: result or review_mod.BranchCheck(True, "ok", "stubbed", "a" * 40),
    )


def script_merge(monkeypatch, *outcomes):
    """mergeq.merge_task hands back `outcomes` in order (the last repeats), recording every call's keyword arguments."""
    calls = []
    queue = list(outcomes)

    def fake(repo, integration_branch, work_branch, task_key, gate3_commands, **kwargs):
        calls.append({"task_key": task_key, "work_branch": work_branch, **kwargs})
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(mergeq, "merge_task", fake)
    return calls


def ready_to_merge(w, key="T1"):
    w.board.cards[w.work(key)].update(
        status="done", branch_name=f"swarm/{key}-coder", project_id="p_1", _runs=[dict(REVIEWER_COMPLETED)])


class Q:
    """What questions.open_question returns: reason, asked_at, source."""

    def __init__(self, reason="Which database should it use?", asked_at=5, source="ases_comment"):
        self.reason, self.asked_at, self.source = reason, asked_at, source


def test_merge_queue_skips_a_task_whose_merge_card_has_an_open_question(tmp_path, monkeypatch):
    """ASES-REC-05: the human has not answered, so the queue neither re-runs the merge nor asks again."""
    w = make_world(tmp_path, monkeypatch, budgets={"fix_cards_per_task": 0})
    ready_to_merge(w)
    stub_check(monkeypatch)
    calls = script_merge(monkeypatch, _MERGED)
    asker = Asker(monkeypatch)
    monkeypatch.setattr(questions, "open_question", lambda card: Q() if card["id"] == w.merge() else None,
                        raising=False)

    assert controller.process_merge_queue("b", w.repo, w.plan, w.project, conn=w.conn) == []
    assert controller.process_merge_queue("b", w.repo, w.plan, w.project, conn=w.conn) == []

    assert calls == [] and asker.asked == []
    assert "merge_failed" not in kinds(w.conn) and not any(n.startswith("merge_refused") for n in kinds(w.conn))
    assert w.row()["fix_cards"] == 0


def test_a_question_is_asked_once_and_the_queue_then_waits_for_the_answer(tmp_path, monkeypatch):
    """The regression this fixes: a merge card blocked for an exhausted fix budget was re-processed on every pass, and
    real Hermes refuses to block it again, which raised each time."""
    w = make_world(tmp_path, monkeypatch, budgets={"fix_cards_per_task": 0})
    ready_to_merge(w)
    stub_check(monkeypatch)
    calls = script_merge(monkeypatch, _CONFLICT)
    asker = Asker(monkeypatch)
    open_now = []
    monkeypatch.setattr(questions, "open_question", lambda card: open_now[0] if open_now and card["id"] == w.merge()
                        else None, raising=False)

    controller.process_merge_queue("b", w.repo, w.plan, w.project, conn=w.conn)
    open_now.append(Q(asker.asked[0][1], 9, "ases_comment"))  # ask_user's comment is now on the card
    controller.process_merge_queue("b", w.repo, w.plan, w.project, conn=w.conn)
    controller.process_merge_queue("b", w.repo, w.plan, w.project, conn=w.conn)

    assert len(calls) == 1 and len(asker.asked) == 1
    assert w.board.calls_of("kanban_block") == []


def test_a_merge_card_created_blocked_is_not_an_open_question(tmp_path, monkeypatch):
    """Hermes 0.21.3 writes a `blocked` event with the reason `initial_status` when a card is created blocked (read from
    kanban_db.create_task), and every merge card is. With the real questions.open_question the queue must go on."""
    w = make_world(tmp_path, monkeypatch)
    assert any(e["kind"] == "blocked" and e["payload"]["reason"] == "initial_status"
               for e in w.board.cards[w.merge()]["_events"])  # the premise: the event is really there
    ready_to_merge(w)
    stub_check(monkeypatch)
    script_merge(monkeypatch, _MERGED)

    assert controller.process_merge_queue("b", w.repo, w.plan, w.project, conn=w.conn) == ["T1"]


def test_open_question_helper_ignores_only_the_creation_block(tmp_path, monkeypatch):
    creation = {"id": "m", "status": "blocked", "_events": [
        {"kind": "blocked", "payload": {"reason": "initial_status"}, "created_at": 1, "run_id": None}]}
    asked = {"id": "m", "status": "blocked",
             "_events": [{"kind": "blocked", "payload": {"reason": "Which database?"}, "created_at": 2, "run_id": 1}]}
    commented = {"id": "m", "status": "blocked", "_events": creation["_events"],
                 "_comments": [{"author": "ases", "body": "ASES QUESTION: Which database?", "created_at": 9}]}

    assert controller._open_question(creation) is None
    assert controller._open_question(asked).reason == "Which database?"
    assert controller._open_question(commented).source == "ases_comment"


def test_the_exhausted_fix_budget_is_asked_through_ask_user_with_a_redacted_question(tmp_path, monkeypatch):
    """Requirement: everywhere the controller asks the user it goes through ask_user with the card fetched by
    kanban_show, never hermes.kanban_block on a merge card; the text ends with a question and holds the failure
    detail, redacted (command output can carry a secret)."""
    w = make_world(tmp_path, monkeypatch, budgets={"fix_cards_per_task": 0})
    ready_to_merge(w)
    stub_check(monkeypatch)
    script_merge(monkeypatch, mergeq.MergeOutcome(False, "c", None, "fail", f"gate failed: OPENAI_KEY={SECRET} exit 1"))
    asked = []
    monkeypatch.setattr(questions, "ask_user", lambda board, card, text, *, conn=None, author="ases": (
        asked.append((board, card, text, conn)) or "commented"), raising=False)

    controller.process_merge_queue("b", w.repo, w.plan, w.project, conn=w.conn)

    ((board, card, text, conn),) = asked
    assert board == "b" and conn is w.conn
    assert card["id"] == w.merge() and card["status"] == "blocked"  # the card as kanban_show returns it
    assert SECRET not in text and "[redacted]" in text and "gate failed" in text
    assert text.rstrip().endswith("How should this be resolved?")
    assert w.board.calls_of("kanban_block") == []
    (event,) = payloads(w.conn, "fix_card_budget_exhausted")
    assert event == {"task_key": "T1", "asked": "commented"}


def test_the_merge_failed_event_and_the_fix_card_body_are_redacted(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    ready_to_merge(w)
    stub_check(monkeypatch)
    script_merge(monkeypatch, mergeq.MergeOutcome(False, "c", None, "fail", f"gate failed: KEY={SECRET}"))

    controller.process_merge_queue("b", w.repo, w.plan, w.project, conn=w.conn)

    (args, kwargs), = w.board.calls_of("kanban_create")[-1:]
    assert SECRET not in kwargs["body"] and "[redacted]" in kwargs["body"]
    assert "Failure detail:\ngate failed: KEY=[redacted]" in kwargs["body"]
    (failed,) = payloads(w.conn, "merge_failed")
    assert SECRET not in failed["detail"]


def test_a_secret_is_redacted_even_when_truncation_would_have_cut_it(tmp_path, monkeypatch):
    """The detail is redacted BEFORE it is cut to 500 or 1500 characters, so a secret that straddles the cut is not left
    half visible."""
    w = make_world(tmp_path, monkeypatch)
    ready_to_merge(w)
    stub_check(monkeypatch)
    detail = "x" * 489 + " " + SECRET + " tail"       # the secret starts at 490 and straddles the 500 character cut
    script_merge(monkeypatch, mergeq.MergeOutcome(False, "c", None, "fail", detail))

    controller.process_merge_queue("b", w.repo, w.plan, w.project, conn=w.conn)

    (failed,) = payloads(w.conn, "merge_failed")
    assert "sk-" not in failed["detail"]
    assert "sk-" not in w.board.calls_of("kanban_create")[-1][1]["body"]


def test_the_fix_card_gets_max_retries_from_the_attempts_budget(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch, budgets={"attempts_per_card": 4})
    ready_to_merge(w)
    stub_check(monkeypatch)
    script_merge(monkeypatch, _CONFLICT)

    controller.process_merge_queue("b", w.repo, w.plan, w.project, conn=w.conn)

    fix = w.board.calls_of("kanban_create")[-1][1]
    assert fix["idempotency_key"].startswith("ases-fix-") and fix["max_retries"] == 4


def test_the_fix_card_max_retries_defaults_to_three(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    ready_to_merge(w)
    stub_check(monkeypatch)
    script_merge(monkeypatch, _CONFLICT)

    controller.process_merge_queue("b", w.repo, w.plan, w.project, conn=w.conn)

    assert w.board.calls_of("kanban_create")[-1][1]["max_retries"] == 3


def test_merge_task_gets_the_project_and_a_should_stop_that_reads_the_halt_flag(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    ready_to_merge(w)
    stub_check(monkeypatch)
    calls = script_merge(monkeypatch, _MERGED)

    controller.process_merge_queue("b", w.repo, w.plan, w.project, conn=w.conn)

    (call,) = calls
    assert call["project"] == "t3" and callable(call["should_stop"])
    assert call["should_stop"]() is False
    bounds.set_status(w.conn, "t3", "paused")
    assert call["should_stop"]() is True
    bounds.set_status(w.conn, "t3", "running")
    bounds.set_status(w.conn, "t3", "stopped", "swarm stop")
    assert call["should_stop"]() is True


def test_a_stopped_merge_is_not_a_failure_and_ends_the_queue_for_this_pass(tmp_path, monkeypatch):
    """ASES-REC-06: nothing is wrong with the branch, so no merge_failed event, no fix card, no budget; and the later
    task is not started either, because the same flag stops it."""
    w = make_world(tmp_path, monkeypatch)
    ready_to_merge(w, "T1")
    ready_to_merge(w, "T2")
    stub_check(monkeypatch)
    calls = script_merge(monkeypatch, _STOPPED)
    w.board.calls.clear()  # setup's own card creation (round 7's board-lineage check) is not what this checks

    assert controller.process_merge_queue("b", w.repo, w.plan, w.project, conn=w.conn) == []

    assert [c["task_key"] for c in calls] == ["T1"]  # T2 was never attempted
    assert w.row("T1")["fix_cards"] == 0 and w.row("T1")["work_card_id"] == w.work("T1")
    assert w.board.calls_of("kanban_create") == []  # no fix card
    assert "merge_failed" not in kinds(w.conn) and "fix_card_created" not in kinds(w.conn)
    (event,) = payloads(w.conn, "merge_stopped")
    assert event["task_key"] == "T1" and "kill switch" in event["detail"]
    assert w.board.calls_of("kanban_complete") == [] and w.board.calls_of("kanban_link") == []


def test_a_stopped_merge_spends_no_fix_budget_even_when_the_budget_is_zero(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch, budgets={"fix_cards_per_task": 0})
    ready_to_merge(w)
    stub_check(monkeypatch)
    script_merge(monkeypatch, _STOPPED)
    asker = Asker(monkeypatch)

    controller.process_merge_queue("b", w.repo, w.plan, w.project, conn=w.conn)

    assert asker.asked == [] and "fix_card_budget_exhausted" not in kinds(w.conn)


def test_the_halt_flag_is_read_before_each_task(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    ready_to_merge(w, "T1")
    ready_to_merge(w, "T2")
    stub_check(monkeypatch)
    calls = []

    def merge_then_pause(repo, integration_branch, work_branch, task_key, gate3_commands, **kwargs):
        calls.append(task_key)
        bounds.set_status(w.conn, "t3", "paused")  # the kill switch lands while T1 is merging
        return _MERGED

    monkeypatch.setattr(mergeq, "merge_task", merge_then_pause)

    merged = controller.process_merge_queue("b", w.repo, w.plan, w.project, conn=w.conn)

    assert merged == ["T1"] and calls == ["T1"]
    assert "merge_queue_halted" in kinds(w.conn)


# --- round 6: the post-merge check and revert trigger (ASES-GIT-05) -----------------------------------------

def test_post_merge_revert_opens_a_fix_card_and_leaves_the_merge_card_open(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    ready_to_merge(w)
    stub_check(monkeypatch)
    script_merge(monkeypatch, _MERGED)
    stub_post_merge_gate(monkeypatch, passed=False, detail="a later task's merge broke this")
    monkeypatch.setattr(mergeq, "revert_merge", lambda *a, **kw: mergeq.RevertOutcome(True, "revsha", "reverted"))

    merged = controller.process_merge_queue("b", w.repo, w.plan, w.project, conn=w.conn)

    assert merged == []
    assert w.board.cards[w.merge()]["status"] == "blocked"  # never completed: still blocked, as it started
    assert w.row()["fix_cards"] == 1
    assert kinds(w.conn).count("post_merge_reverted") == 1
    assert "merge_failed" in kinds(w.conn) and "fix_card_created" in kinds(w.conn)
    assert "integrity_violation" not in kinds(w.conn)
    assert guards.expected_head(w.conn, "t3") == "revsha"


def test_post_merge_revert_failure_halts_the_run_via_run_pass(tmp_path, monkeypatch):
    """End to end through the real process_merge_queue AND run_pass (not PassRig, which stubs the merge queue
    itself): a post-merge revert that cannot repair the branch must stop the pass before dispatch or finalize."""
    w = make_world(tmp_path, monkeypatch)
    ready_to_merge(w)
    stub_check(monkeypatch)
    script_merge(monkeypatch, _MERGED)
    stub_post_merge_gate(monkeypatch, passed=False)
    monkeypatch.setattr(mergeq, "revert_merge", lambda *a, **kw: mergeq.RevertOutcome(False, None, "still broken"))
    monkeypatch.setattr(guards, "check_primary_checkout", lambda *a, **kw: guards.GuardResult(True, (), "abc", "integration"))
    for name in ("process_idle_worktrees", "process_recovery", "process_unpark", "process_provision",
                 "process_review_lane"):
        monkeypatch.setattr(controller, name, lambda *a, **kw: [])
    monkeypatch.setattr(controller, "process_bounds", lambda *a, **kw: (False, None))
    monkeypatch.setattr(usage_mod, "ingest_run_usage", lambda *a, **kw: [])
    dispatched = []
    monkeypatch.setattr(hermes, "kanban_dispatch", lambda board, **kw: dispatched.append(1) or {})
    finalized = []
    monkeypatch.setattr(controller, "process_finalize", lambda *a, **kw: finalized.append(1) or None)

    summary = controller.run_pass("b", w.repo, w.plan, w.project, {"providers": {}, "models": []}, conn=w.conn)

    assert summary["integrity"] and "T1" in summary["integrity"][0]
    assert dispatched == [1]  # dispatch runs BEFORE the merge queue in run_pass's order, so it already happened
    assert finalized == []    # but finalize, AFTER the merge queue, never runs on an unrepaired branch
    assert summary["merged"] == [] and summary["finished"] is False


def test_a_halted_project_does_not_even_read_the_board(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    ready_to_merge(w)
    stub_check(monkeypatch)
    calls = script_merge(monkeypatch, _MERGED)
    bounds.set_status(w.conn, "t3", "stopped", "swarm stop")
    before = len(w.board.calls)

    assert controller.process_merge_queue("b", w.repo, w.plan, w.project, conn=w.conn) == []

    assert calls == [] and len(w.board.calls) == before
    (event,) = payloads(w.conn, "merge_queue_halted")
    assert event["reason"] == "swarm stop"


def test_a_tamper_check_error_is_retried_next_pass_and_never_a_failure(tmp_path, monkeypatch):
    """ASES-QG-03: git could not answer, so nothing is known: not a failure (no fix card, no budget), and not a pass
    (merge_task is not reached). Recorded once per card and message."""
    w = make_world(tmp_path, monkeypatch)
    ready_to_merge(w)
    stub_check(monkeypatch, review_mod.BranchCheck(
        False, "tamper_check_error", "git could not diff the range", "a" * 40))
    calls = script_merge(monkeypatch, _MERGED)
    creates = len(w.board.calls_of("kanban_create"))

    for _ in range(3):
        assert controller.process_merge_queue("b", w.repo, w.plan, w.project, conn=w.conn) == []

    assert calls == [] and len(w.board.calls_of("kanban_create")) == creates
    assert w.row()["fix_cards"] == 0
    assert "merge_failed" not in kinds(w.conn) and "fix_card_created" not in kinds(w.conn)
    (event,) = payloads(w.conn, "tamper_check_error")
    assert event["task_key"] == "T1" and event["card_id"] == w.work() and "could not diff" in event["detail"]
    assert event["project"] == w.plan.project  # ASES-OBS-01: not a cross-project leak

    stub_check(monkeypatch)  # git recovers: the same task merges on the next pass
    assert controller.process_merge_queue("b", w.repo, w.plan, w.project, conn=w.conn) == ["T1"]


def test_a_different_tamper_check_error_is_recorded_again(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    ready_to_merge(w)
    script_merge(monkeypatch, _MERGED)

    stub_check(monkeypatch, review_mod.BranchCheck(False, "tamper_check_error", "first reason", "a" * 40))
    controller.process_merge_queue("b", w.repo, w.plan, w.project, conn=w.conn)
    stub_check(monkeypatch, review_mod.BranchCheck(False, "tamper_check_error", "second reason", "a" * 40))
    controller.process_merge_queue("b", w.repo, w.plan, w.project, conn=w.conn)

    assert [p["detail"] for p in payloads(w.conn, "tamper_check_error")] == ["first reason", "second reason"]


def test_a_tamper_finding_takes_the_ordinary_failure_path(tmp_path, monkeypatch):
    """Kind `tamper` is a real finding about the diff: a fix card carrying the findings, bounded by the fix budget."""
    w = make_world(tmp_path, monkeypatch)
    ready_to_merge(w)
    stub_check(monkeypatch, review_mod.BranchCheck(False, "tamper", "deleted_test: tests/test_a.py line 3", "a" * 40))
    calls = script_merge(monkeypatch, _MERGED)

    assert controller.process_merge_queue("b", w.repo, w.plan, w.project, conn=w.conn) == []

    assert calls == []
    (failed,) = payloads(w.conn, "merge_failed")
    assert failed["detail"].startswith("tamper: deleted_test")
    (fix_args, fix_kwargs) = w.board.calls_of("kanban_create")[-1]
    assert "deleted_test: tests/test_a.py line 3" in fix_kwargs["body"] and fix_args[1] == "T1: fix (round 1)"
    assert w.row()["fix_cards"] == 1


def test_a_tamper_finding_after_the_fix_budget_is_spent_asks_the_user(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch, budgets={"fix_cards_per_task": 0})
    ready_to_merge(w)
    stub_check(monkeypatch, review_mod.BranchCheck(False, "tamper", "skip marker added", "a" * 40))
    script_merge(monkeypatch, _MERGED)
    asker = Asker(monkeypatch)

    controller.process_merge_queue("b", w.repo, w.plan, w.project, conn=w.conn)

    assert [cid for cid, _ in asker.asked] == [w.merge()] and "skip marker added" in asker.asked[0][1]


def test_a_real_merge_completes_its_card_inside_a_complete_merge_card_intent(tmp_path, monkeypatch):
    """ASES-REC-03/04: "complete a merge card" is a multi-step action with an intent, keyed by the task."""
    w = make_world(tmp_path, monkeypatch)
    ready_to_merge(w)
    stub_check(monkeypatch)
    script_merge(monkeypatch, _MERGED)
    open_during = []
    w.board.hooks["kanban_complete"] = lambda *a, **kw: open_during.extend(
        (i["kind"], i["key"]) for i in intents.open_intents(w.conn, "t3"))

    assert controller.process_merge_queue("b", w.repo, w.plan, w.project, conn=w.conn) == ["T1"]

    assert open_during == [("complete_merge_card", "T1")]
    assert intents.open_intents(w.conn, "t3") == []
    done = w.conn.execute("SELECT completed_at FROM intents WHERE kind = 'complete_merge_card'").fetchall()
    assert len(done) == 1 and done[0]["completed_at"]


def test_a_no_op_merge_also_completes_its_card_inside_an_intent(tmp_path, monkeypatch):
    plan_raw = copy.deepcopy(PLAN_RAW)
    plan_raw["tasks"][0]["role"] = "reviewer"
    w = make_world(tmp_path, monkeypatch, plan_raw=plan_raw)
    ready_to_merge(w)
    script_merge(monkeypatch, _NO_OP)
    open_during = []
    w.board.hooks["kanban_complete"] = lambda *a, **kw: open_during.extend(
        i["kind"] for i in intents.open_intents(w.conn, "t3"))

    assert controller.process_merge_queue("b", w.repo, w.plan, w.project, conn=w.conn) == ["T1"]

    assert open_during == ["complete_merge_card"] and intents.open_intents(w.conn, "t3") == []


def test_a_failing_completion_leaves_the_intent_open_and_the_expected_head_already_updated(tmp_path, monkeypatch):
    """The fast-forward has happened by then. The intent stays open for reconcile, and the integrity guard must already
    expect the new HEAD, or the controller's own merge would be reported as an intruder on the next pass."""
    w = make_world(tmp_path, monkeypatch)
    ready_to_merge(w)
    stub_check(monkeypatch)
    script_merge(monkeypatch, _MERGED)
    guards.set_expected_head(w.conn, "t3", "before")
    w.board.fail["kanban_complete"] = hermes.HermesCommandError(["kanban", "complete"], 1, "hermes went away")

    with pytest.raises(hermes.HermesCommandError):
        controller.process_merge_queue("b", w.repo, w.plan, w.project, conn=w.conn)

    (open_intent,) = intents.open_intents(w.conn, "t3")
    assert (open_intent["kind"], open_intent["key"]) == ("complete_merge_card", "T1")
    assert guards.expected_head(w.conn, "t3") == "cand1"


# =============================================================================================================
# helpers: _clean, _record_once, _as_datetime, _as_epoch
# =============================================================================================================

def test_clean_redacts_collapses_escapes_and_cuts():
    text = f"line one\n  line   two {SECRET} caf{chr(0xE9)} {chr(0x2192)} end"

    cleaned = controller._clean(text)

    assert SECRET not in cleaned and "[redacted]" in cleaned
    assert "\n" not in cleaned and "  " not in cleaned
    assert all(ord(ch) < 128 for ch in cleaned)
    assert "\\xe9" in cleaned and "\\u2192" in cleaned
    assert len(controller._clean("x" * 1000, 50)) == 50
    assert controller._clean(None) == ""


def test_record_once_dedupes_on_the_named_fields_only(tmp_path):
    conn = db.connect(tmp_path / "ases.db")
    match = ("card_id", "error")

    assert controller._record_once(conn, "some_error", {"card_id": "c1", "error": "boom", "n": 1}, match=match)
    assert not controller._record_once(conn, "some_error", {"card_id": "c1", "error": "boom", "n": 2}, match=match)
    assert controller._record_once(conn, "some_error", {"card_id": "c1", "error": "other", "n": 3}, match=match)
    assert controller._record_once(conn, "some_error", {"card_id": "c2", "error": "boom", "n": 4}, match=match)
    assert controller._record_once(conn, "another_kind", {"card_id": "c1", "error": "boom"}, match=match)
    assert len(payloads(conn, "some_error")) == 3


def test_record_once_finds_its_own_earlier_copy_when_the_value_is_redacted_in_storage(tmp_path):
    conn = db.connect(tmp_path / "ases.db")
    payload = {"card_id": "c1", "error": f"failed with {SECRET}"}

    assert controller._record_once(conn, "some_error", payload, match=("card_id", "error"))
    assert not controller._record_once(conn, "some_error", payload, match=("card_id", "error"))


def test_earliest_gate_record_ts_matches_a_short_sha_by_prefix(tmp_path):
    """Round 19 fix round 1 (reviewer finding, minor): a gate1_record_posted event's own `head` is always the
    FULL SHA review.check_branch resolved, but the coder's own hand-off commit_sha (what _evidence_blocked
    passes in here as `head`) may be a short one -- an exact string match would silently read an existing
    record as "none yet", exactly the loosening review.check_branch_for_merge's own established prefix-compare
    ("a prefix compare, case-insensitive, because a quoted SHA may be short") avoids for the same kind of
    commit-identity comparison."""
    conn = db.connect(tmp_path / "ases.db")
    full = "a" * 40
    events.record(conn, "gate1_record_posted", {
        "task_key": "T1", "head": full, "outcome": "pass", "card_id": "w_1",
    }, project="t3")

    assert controller._earliest_gate_record_ts(conn, "t3", "T1", full) is not None            # exact, as before
    assert controller._earliest_gate_record_ts(conn, "t3", "T1", full[:12]) is not None        # short SHA: fixed
    assert controller._earliest_gate_record_ts(conn, "t3", "T1", full.upper()[:12]) is not None  # case-insensitive
    assert controller._earliest_gate_record_ts(conn, "t3", "T1", "b" * 12) is None              # a different commit
    assert controller._earliest_gate_record_ts(conn, "t3", "T1", "") is None


def test_as_datetime_accepts_a_datetime_epoch_seconds_or_an_iso_string():
    moment = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)

    assert controller._as_datetime(None) is None
    assert controller._as_datetime(moment) is moment
    assert controller._as_datetime(moment.timestamp()) == moment
    assert controller._as_datetime(int(moment.timestamp())) == moment
    assert controller._as_datetime("2026-09-21T12:00:00Z") == moment
    for bad in (True, object(), [1]):
        with pytest.raises(ValueError):
            controller._as_datetime(bad)


def test_as_epoch_normalizes_a_naive_datetime_or_iso_string_to_utc_before_converting():
    """Round 19 fix round 2 (reviewer major): _as_epoch used to feed _as_datetime's result straight to
    `.timestamp()`. Python reads a naive (tzinfo-less) datetime's `.timestamp()` in the process's LOCAL
    timezone, not UTC, though `_as_datetime`'s own docstring says naive means UTC, and a naive ISO-8601 string
    (no trailing offset) produces a naive datetime the same way. usage.py's session-as-unit windows compare
    this value directly against run_started_at/run_ended_at, always UTC epoch seconds, so the mismatch quietly
    shifted every one of those comparisons, and `_charge_time`'s ledger-day attribution, by the host's own UTC
    offset. recovery.py's own `_epoch` helper already normalizes naive to UTC first; `_as_epoch` now mirrors it."""
    naive = datetime(2026, 9, 28, 2, 10, 6)
    aware = naive.replace(tzinfo=timezone.utc)

    assert controller._as_epoch(naive) == aware.timestamp()
    assert controller._as_epoch("2026-09-28T02:10:06") == aware.timestamp()   # a naive ISO string too
    assert controller._as_epoch(aware) == aware.timestamp()                  # already tzinfo-aware: unaffected
    assert controller._as_epoch(aware.timestamp()) == aware.timestamp()      # epoch seconds in, epoch seconds out
    assert controller._as_epoch(None) is None


# =============================================================================================================
# process_recovery: the wiring
# =============================================================================================================

def test_process_recovery_refreshes_review_rounds_then_decides_with_the_pass_arguments(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    seen = []
    monkeypatch.setattr(recovery, "refresh_review_rounds",
                        lambda board, plan, *, conn, reviewer_profiles=frozenset(): (
                            seen.append(("refresh", board, plan, conn, reviewer_profiles)) or {}))
    monkeypatch.setattr(recovery, "process_failures", lambda board, plan, project, models, *, conn, now=None: (
        seen.append(("failures", board, plan, project, models, conn, now)) or []))
    moment = datetime(2026, 9, 21, tzinfo=timezone.utc)

    assert controller.process_recovery("b", w.repo, w.plan, w.project, MODELS, conn=w.conn, now=moment) == []

    # Round 19, package REVIEWLADDER, fix D5: process_recovery resolves and passes the reviewer profile(s) so
    # refresh_review_rounds can exclude an evidence-stalling changes_requested from the count itself.
    assert seen == [
        ("refresh", "b", w.plan, w.conn, frozenset({"reviewer"})),
        ("failures", "b", w.plan, w.project, MODELS, w.conn, moment),
    ]


def test_process_recovery_carries_out_the_three_controller_actions_and_reports_every_decision(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    decisions = [
        decision(w, recovery.ACTION_FRESH_ATTEMPT, run_id=1),
        decision(w, recovery.ACTION_SWITCH_MODEL, key="T2", run_id=2, model="coder-alt", provider="xkiro"),
        decision(w, recovery.ACTION_REPLAN, run_id=3),
        decision(w, recovery.ACTION_RESUME, run_id=4, kind=recovery.FailureKind.INFRASTRUCTURE),
        decision(w, recovery.ACTION_NONE, run_id=5, kind=recovery.FailureKind.RATE_LIMIT),
    ]
    monkeypatch.setattr(recovery, "refresh_review_rounds", lambda *a, **kw: {})
    monkeypatch.setattr(recovery, "process_failures", lambda *a, **kw: decisions)
    started, replans = [], []

    def start_fresh(board, repo, plan, project, models, task, old, d, *, conn):
        started.append((task.key, old, d.action))
        return "n9"

    monkeypatch.setattr(controller, "_start_fresh_attempt", start_fresh)
    monkeypatch.setattr(controller, "_request_replan", lambda board, plan, project, task, card, d, *, conn: (
        replans.append((task.key, card)) or True))
    monkeypatch.setattr(controller, "_pending_decisions", lambda *a, **kw: [])
    monkeypatch.setattr(controller, "_escalate_spent_budgets", lambda *a, **kw: [])

    rows = controller.process_recovery("b", w.repo, w.plan, w.project, MODELS, conn=w.conn)

    assert started == [("T1", w.work("T1"), "fresh_attempt"), ("T2", w.work("T2"), "switch_model")]
    assert replans == [("T1", w.work("T1"))]
    assert rows == [
        {"task_key": "T1", "action": "fresh_attempt", "kind": "capability"},
        {"task_key": "T2", "action": "switch_model", "kind": "capability"},
        {"task_key": "T1", "action": "replan", "kind": "capability"},
        {"task_key": "T1", "action": "resume", "kind": "infrastructure"},
        {"task_key": "T1", "action": "none", "kind": "rate_limit"},
    ]


def test_process_recovery_survives_a_decision_whose_action_raises(tmp_path, monkeypatch):
    """One task's failing action must not stop the other tasks: it is recorded (once) and the rest are still done."""
    w = make_world(tmp_path, monkeypatch)
    monkeypatch.setattr(recovery, "refresh_review_rounds", lambda *a, **kw: {})
    monkeypatch.setattr(recovery, "process_failures", lambda *a, **kw: [
        decision(w, recovery.ACTION_FRESH_ATTEMPT, key="T1"), decision(w, recovery.ACTION_FRESH_ATTEMPT, key="T2")])
    tried = []

    def start(board, repo, plan, project, models, task, old, d, *, conn):
        tried.append(task.key)
        if task.key == "T1":
            raise RuntimeError("boom")
        return "n9"

    monkeypatch.setattr(controller, "_start_fresh_attempt", start)

    rows = controller.process_recovery("b", w.repo, w.plan, w.project, MODELS, conn=w.conn)

    assert tried == ["T1", "T2"] and len(rows) == 2
    assert "boom" in payloads(w.conn, "recovery_action_error")[0]["error"]


# =============================================================================================================
# _start_fresh_attempt: the replacement card
# =============================================================================================================

def start(w, action=recovery.ACTION_FRESH_ATTEMPT, *, key="T1", run_id=1, models=MODELS, **decision_kwargs):
    task = w.plan.task(key)
    return controller._start_fresh_attempt(
        "b", w.repo, w.plan, w.project, models, task, w.work(key),
        decision(w, action, key=key, run_id=run_id, **decision_kwargs), conn=w.conn)


def test_a_fresh_attempt_builds_the_replacement_card_exactly(tmp_path, monkeypatch):
    """ASES-REC-01 (blueprint 19.2): a fresh worktree, the failure bundle attached (criteria, diff, gate output,
    reviewer findings), the same role and limits, the failed card's own parents and project."""
    w = make_world(tmp_path, monkeypatch, budgets={"attempts_per_card": 4, "card_runtime_minutes": 30})
    git_repo(tmp_path)
    fail_work_card(w)
    old = w.board.cards[w.work()]
    old["_parents"] = ["dep_merge"]
    w.board.add_comment(w.work(), "default", "BLOCKED: gave up")
    for n in range(1, 5):
        w.board.add_comment(w.work(), "reviewer", f"CHANGES REQUESTED: finding number {n}")
    w.board.add_comment(w.work(), "user", "ANSWER: use the other library")
    w.board.add_comment(w.work(), "coder-1", "an ordinary comment that is not a finding")
    w.conn.execute(
        "INSERT INTO gate_runs (task_key, gate, commit_sha, result, detail, ran_at, project) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("T1", "gate1", "old", "pass", "an older row", "2026-09-21T09:00:00+00:00", "t3"))
    w.conn.execute(
        "INSERT INTO gate_runs (task_key, gate, commit_sha, result, detail, ran_at, project) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("T1", "gate1", "abc", "fail", f"$ pytest\nFAILED test_a [exit 1]\nOPENAI_KEY={SECRET}",
         "2026-09-21T10:00:00+00:00", "t3"))
    w.conn.execute(
        "INSERT INTO gate_runs (task_key, gate, commit_sha, result, detail, ran_at, project) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("OTHER", "gate1", "zzz", "fail", "another task's gate output", "2026-09-21T11:00:00+00:00", "t3"))

    new_id = start(w)

    assert new_id == "n5"
    kw = w.board.cards[new_id]["created_kwargs"]
    task = w.plan.task("T1")
    assert w.board.cards[new_id]["title"] == "T1: retry 1"
    assert {k: kw[k] for k in ("assignee", "workspace", "branch", "project", "parent", "idempotency_key",
                               "max_retries", "max_runtime")} == {
        "assignee": "coder-1", "workspace": "worktree", "branch": "swarm/T1-retry1", "project": "p_9",
        "parent": ["dep_merge"], "idempotency_key": "ases-retry-t3-T1-1", "max_retries": 4, "max_runtime": "30m"}
    body = kw["body"]
    assert body.startswith(controller._work_card_body(task, "reviewer") + "\n\nFailure bundle for card " + w.work())
    assert "## Acceptance criteria\n- exists\n- is tested" in body
    assert "## What failed" in body and "Outcome: crashed" in body and "protocol violation" in body
    assert "+++ b/new.txt" in body and "+hello from the failed attempt" in body            # the diff of the old branch
    assert "FAILED test_a [exit 1]" in body and "an older row" not in body and "another task's" not in body
    assert SECRET not in body and "OPENAI_KEY=[redacted]" in body                          # gate output, redacted
    findings = body.split("## Reviewer findings\n", 1)[1]
    assert "finding number 3" in findings and "finding number 4" in findings and "use the other library" in findings
    assert "finding number 2" not in findings and "ordinary comment" not in findings and "BLOCKED" not in findings
    assert findings.index("finding number 3") < findings.index("finding number 4") < findings.index("other library")


def test_a_fresh_attempt_uses_the_task_role_not_the_failed_cards_assignee(tmp_path, monkeypatch):
    """A card that failed while under review is assigned to the reviewer; its replacement is a coder card."""
    w = make_world(tmp_path, monkeypatch)
    fail_work_card(w)
    w.board.cards[w.work()]["assignee"] = "reviewer"

    new_id = start(w)

    assert w.board.cards[new_id]["created_kwargs"]["assignee"] == "coder-1"


def test_a_fresh_attempt_has_an_empty_diff_section_when_the_branch_does_not_exist(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)  # tmp_path/repo does not even exist
    fail_work_card(w)

    new_id = start(w)

    assert "## Diff so far\n(none provided)" in w.board.cards[new_id]["created_kwargs"]["body"]


def test_a_fresh_attempt_does_the_bookkeeping_in_a_safe_order(tmp_path, monkeypatch):
    """create, link, ingest, archive, then the repoint and the event together. The repoint is last on purpose: every
    step before it is safe to repeat."""
    w = make_world(tmp_path, monkeypatch)
    fail_work_card(w)
    at_archive = {}
    ingested = []
    def ingest(board, card_id, project, models, *, conn, plan_project=None, task_key=None):
        w.board.calls.append(("ingest", (card_id,), {}))
        ingested.append((card_id, plan_project, task_key))
        return []

    monkeypatch.setattr(usage_mod, "ingest_card_usage", ingest)
    w.board.hooks["kanban_archive"] = lambda *a, **kw: at_archive.update(work=w.row()["work_card_id"])

    new_id = start(w)

    order = [name for name in w.board.writes()]
    assert order[-4:] == ["kanban_create", "kanban_link", "ingest", "kanban_archive"]
    # [-1], not [0]: round 7's board-lineage check (bug 1) already linked each task's work card to its
    # merge card during make_world's own setup, so this fresh attempt's own link is the LAST one, not the
    # first, in the call log.
    assert w.board.calls_of("kanban_link")[-1][0] == ("b", new_id, w.merge())     # extra parent of the MERGE card
    assert ingested == [(w.work(), "t3", "T1")]                                   # the OUTGOING card is ingested
    assert w.board.calls_of("kanban_archive")[0][0] == ("b", [w.work()])          # the OLD card is archived
    assert at_archive["work"] == w.work()                                         # ...before the repoint
    assert w.row()["work_card_id"] == new_id and w.row()["fix_cards"] == 0        # repointed; no fix budget spent
    (event,) = payloads(w.conn, "retry_card_created")
    assert event["project"] == "t3" and event["task_key"] == "T1" and event["old_card"] == w.work()
    assert event["new_card"] == new_id and event["n"] == 1 and event["run_id"] == 1
    assert event["action"] == "fresh_attempt" and event["idempotency_key"] == "ases-retry-t3-T1-1"
    assert w.board.calls_of("kanban_set_model") == []                              # a plain retry pins no model


def test_a_fresh_attempt_guards_the_usage_ingest_like_the_fix_path(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    fail_work_card(w)
    monkeypatch.setattr(
        usage_mod, "ingest_card_usage", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("export failed")))

    new_id = start(w)

    assert new_id is not None and w.row()["work_card_id"] == new_id
    assert "export failed" in payloads(w.conn, "usage_ingest_error")[0]["error"]


def test_a_switch_model_attempt_pins_the_decided_model_on_the_new_card_before_it_is_linked(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    fail_work_card(w)

    new_id = start(w, recovery.ACTION_SWITCH_MODEL, model="coder-alt", provider="xkiro")

    assert w.board.calls_of("kanban_set_model") == [(("b", new_id, "coder-alt"), {"provider": "xkiro"})]
    # the model is pinned right after the create, before the link: no worker may start on the model that failed
    assert w.board.writes()[-4:] == ["kanban_create", "kanban_set_model", "kanban_link", "kanban_archive"]
    assert w.board.cards[new_id]["model_override"] == "coder-alt"
    (event,) = payloads(w.conn, "retry_card_created")
    assert event["action"] == "switch_model" and event["model"] == "coder-alt" and event["provider"] == "xkiro"


def record_retry(conn, project, task_key, kind="retry_card_created", **extra):
    """A retry_card_created event (or `kind`) as _start_fresh_attempt records it, for a test that needs earlier ones."""
    events.record(conn, kind, {"project": project, "task_key": task_key, "old_card": "x", "new_card": "y", **extra})


def test_the_retry_number_counts_the_retry_cards_already_created_for_the_task(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    fail_work_card(w)
    for _ in range(2):  # two earlier retries of T1, and one of another task and one of another project
        record_retry(w.conn, "t3", "T1")
    record_retry(w.conn, "t3", "T2")
    record_retry(w.conn, "other", "T1")

    new_id = start(w)

    kw = w.board.cards[new_id]["created_kwargs"]
    assert w.board.cards[new_id]["title"] == "T1: retry 3"
    assert kw["branch"] == "swarm/T1-retry3" and kw["idempotency_key"] == "ases-retry-t3-T1-3"


def test_repeating_a_half_finished_attempt_reuses_the_card_it_already_made(tmp_path, monkeypatch):
    """The idempotency key is stable across a repeat, so a failure after the create never makes a second card."""
    w = make_world(tmp_path, monkeypatch)
    fail_work_card(w)
    w.board.fail["kanban_link"] = hermes.HermesCommandError(["kanban", "link"], 1, "database is locked")

    assert start(w) is None
    assert w.row()["work_card_id"] == w.work() and w.board.cards[w.work()]["status"] == "blocked"  # nothing else moved
    w.board.fail.pop("kanban_link")
    new_id = start(w)

    retries = [c for c in w.board.cards.values() if c["title"].startswith("T1: retry")]
    assert [c["id"] for c in retries] == [new_id] == ["n5"]
    assert w.row()["work_card_id"] == "n5" and w.board.cards[w.work()]["status"] == "archived"
    assert len(payloads(w.conn, "retry_card_created")) == 1


@pytest.mark.parametrize("wrapper", ["kanban_show", "kanban_create", "kanban_link", "kanban_archive"])
def test_a_failing_hermes_call_records_retry_card_error_once_and_returns_none(tmp_path, monkeypatch, wrapper):
    w = make_world(tmp_path, monkeypatch)
    fail_work_card(w)
    w.board.fail[wrapper] = hermes.HermesCommandError(["kanban", wrapper], 1, f"{wrapper} exploded")

    assert start(w) is None
    assert start(w) is None  # the next pass fails the same way

    (event,) = payloads(w.conn, "retry_card_error")  # once, not once per pass
    assert event["project"] == "t3" and event["task_key"] == "T1" and event["old_card"] == w.work()
    assert f"{wrapper} exploded" in event["error"]
    assert w.row()["work_card_id"] == w.work()                     # never repointed
    assert "retry_card_created" not in kinds(w.conn)
    if wrapper != "kanban_archive":
        assert w.board.cards[w.work()]["status"] == "blocked"


def test_a_retry_number_that_collides_with_the_old_card_is_skipped(tmp_path, monkeypatch):
    """If a lost event makes the key come back with the card we are replacing, using it would archive the card we are
    about to point at. The next number is tried instead."""
    w = make_world(tmp_path, monkeypatch)
    fail_work_card(w)
    w.board.keys["ases-retry-t3-T1-1"] = w.work()

    new_id = start(w)

    assert new_id not in (None, w.work())
    assert w.board.cards[new_id]["created_kwargs"]["idempotency_key"] == "ases-retry-t3-T1-2"
    assert w.board.cards[new_id]["title"] == "T1: retry 2"
    assert w.board.cards[w.work()]["status"] == "archived" and w.row()["work_card_id"] == new_id
    assert payloads(w.conn, "retry_card_created")[0]["n"] == 2


def test_a_repeated_attempt_keeps_a_replacement_hermes_ran_to_completion_meanwhile(tmp_path, monkeypatch):
    """Found by driving the real controller against the FakeHermes with a failing `link`: the new card is ready, so
    Hermes dispatched it and a worker finished it while the link kept failing. The repeat must adopt that card (its work
    is real), and must not create a second replacement because the first one is no longer fresh."""
    w = make_world(tmp_path, monkeypatch)
    fail_work_card(w)
    w.board.fail["kanban_link"] = hermes.HermesCommandError(["kanban", "link"], 1, "database is locked")
    assert start(w) is None
    (first,) = [c for c in w.board.cards.values() if c["title"] == "T1: retry 1"]
    first["status"] = "done"                                   # a worker finished it in the meantime
    w.board.fail.pop("kanban_link")

    new_id = start(w)

    assert new_id == first["id"]
    assert [c["id"] for c in w.board.cards.values() if c["title"].startswith("T1: retry")] == [first["id"]]
    assert w.row()["work_card_id"] == first["id"] and w.board.cards[w.work()]["status"] == "archived"
    assert w.board.calls_of("kanban_link")[-1][0] == ("b", first["id"], w.merge())


def test_a_dropped_decision_makes_the_next_attempt_use_a_new_retry_number(tmp_path, monkeypatch):
    """A decision _redrive dropped may already have made a card. The next decision must not adopt that stray card by
    asking for the same idempotency key, so the drop counts towards the retry number."""
    w = make_world(tmp_path, monkeypatch)
    fail_work_card(w)
    record_retry(w.conn, "t3", "T1")
    record_retry(w.conn, "t3", "T1", "retry_card_skipped", run_id=1)
    record_retry(w.conn, "other", "T1", "retry_card_skipped", run_id=1)
    record_retry(w.conn, "t3", "T2", "retry_card_skipped", run_id=1)

    new_id = start(w)

    assert w.board.cards[new_id]["title"] == "T1: retry 3"


def test_an_attempt_that_only_meets_existing_cards_gives_up_with_an_error(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    fail_work_card(w)
    for number in (1, 2, 3):
        w.board.keys[f"ases-retry-t3-T1-{number}"] = w.work()

    assert start(w) is None

    assert "no retry number" in payloads(w.conn, "retry_card_error")[0]["error"]
    assert w.board.cards[w.work()]["status"] == "blocked"


def test_an_attempt_that_died_after_the_archive_finishes_without_archiving_again(tmp_path, monkeypatch):
    """The old card is already archived, plan_tasks still points at it, and the replacement exists but is not linked:
    a repeat finds the card by its key, links it, skips the archive (Hermes refuses to archive twice) and repoints."""
    w = make_world(tmp_path, monkeypatch)
    fail_work_card(w)
    w.board.cards[w.work()]["status"] = "archived"
    made = w.board._create("b", "T1: retry 1", idempotency_key="ases-retry-t3-T1-1", parent=[], workspace="worktree")
    w.board.calls.clear()

    new_id = start(w)

    assert new_id == made["id"]
    assert w.board.calls_of("kanban_archive") == []
    assert w.board.calls_of("kanban_link")[0][0] == ("b", made["id"], w.merge())
    assert w.row()["work_card_id"] == made["id"] and len(payloads(w.conn, "retry_card_created")) == 1


def test_a_reapprove_after_a_fresh_attempt_keeps_the_replacement_card_and_creates_nothing(tmp_path, monkeypatch):
    """The fresh attempt ARCHIVED the original work card, and Hermes does not find an archived card by its idempotency
    key, so creating "the original" again on a re-approve would duplicate the task's work and forget the replacement."""
    w = make_world(tmp_path, monkeypatch)
    fail_work_card(w)
    new_id = start(w)
    assert w.board.cards[w.work()]["status"] == "archived"
    work_creates = lambda: [kw for _, kw in w.board.calls_of("kanban_create")  # noqa: E731
                            if kw["idempotency_key"] == "ases-work-t3-T1"]
    assert len(work_creates()) == 1

    pairs = controller.create_cards_from_plan("b", "proj1", w.repo, w.plan, w.project, conn=w.conn)

    assert len(work_creates()) == 1                                   # not created again
    assert {p.task_key: p.work_card_id for p in pairs}["T1"] == new_id
    assert w.row()["work_card_id"] == new_id and w.row()["merge_card_id"] == w.merge()
    assert w.row("T2")["work_card_id"] == w.work("T2")                # a task that was never retried is as before
    assert len([c for c in w.board.cards.values() if c["title"].startswith("T1: scaffold")]) == 1


def test_a_reapprove_without_any_retry_still_goes_through_the_idempotent_create(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)

    controller.create_cards_from_plan("b", "proj1", w.repo, w.plan, w.project, conn=w.conn)

    creates = [kw["idempotency_key"] for _, kw in w.board.calls_of("kanban_create")]
    assert creates.count("ases-work-t3-T1") == 2 and creates.count("ases-work-t3-T2") == 2


def test_the_retry_of_another_project_does_not_stop_a_reapprove_from_creating(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    record_retry(w.conn, "other", "T1")

    controller.create_cards_from_plan("b", "proj1", w.repo, w.plan, w.project, conn=w.conn)

    creates = [kw["idempotency_key"] for _, kw in w.board.calls_of("kanban_create")]
    assert creates.count("ases-work-t3-T1") == 2


def test_latest_run_id_follows_the_rule_recovery_uses_for_a_decision(tmp_path):
    assert controller._latest_run_id({}) is None
    assert controller._latest_run_id({"_runs": []}) is None
    assert controller._latest_run_id({"_runs": [{"id": 4, "outcome": None}, "junk", {"id": 5, "outcome": " "}]}) is None
    runs = [{"id": 3, "outcome": "crashed"}, {"id": 4, "outcome": "completed"}, {"id": 5, "outcome": None}]
    assert controller._latest_run_id({"_runs": runs}) == 4                       # a run with no outcome is still open
    assert controller._latest_run_id({"_runs": [{"outcome": "crashed"}, {"outcome": "timed_out"}]}) == "#1"


def test_handoff_commit_skips_a_review_requested_run_by_a_reviewer_profile(tmp_path):
    """Round 19, package REVIEWLADDER, fix D4 (research report finding D6): request_review takes the implementer
    from the CURRENT assignee, so a reviewer profile calling it by mistake leaves a review_requested run that is
    the REVIEWER's own words, not the coder's hand-off. Without the `reviewer_profiles` filter this returns the
    reviewer's own (here, deliberately wrong) commit; with it, the coder's real hand-off commit."""
    coder_sha = "a" * 40
    reviewer_sha = "b" * 40
    card = {"_runs": [
        {"id": 1, "profile": "coder-1", "outcome": "review_requested", "metadata": {"commit_sha": coder_sha}},
        {"id": 2, "profile": "reviewer", "outcome": "blocked"},
        {"id": 3, "profile": "reviewer", "outcome": "review_requested", "metadata": {"commit_sha": reviewer_sha}},
    ]}
    assert controller._handoff_commit(card) == reviewer_sha  # unchanged default: no filtering, old behaviour
    assert controller._handoff_commit(card, frozenset({"reviewer"})) == coder_sha
    # A card with ONLY a reviewer-authored review_requested run (the D6 case in isolation, e.g. right after the
    # PROTOCOL stop before anything else happens) has no coder hand-off to fall back to: None, not a guess.
    only_reviewer = {"_runs": [card["_runs"][2]]}
    assert controller._handoff_commit(only_reviewer, frozenset({"reviewer"})) is None


def test_last_error_prefers_the_newest_failed_run_then_the_newest_review_send_back(tmp_path):
    failed = failed_run(1, error="Request timed out after 30s   (twice)")
    ok = {"id": 2, "outcome": "review_requested", "summary": "Implemented the parser", "error": None}
    card = {"_runs": [failed, ok], "_comments": [
        {"author": "reviewer", "body": "CHANGES REQUESTED: the parser drops the last line", "created_at": 1}]}

    assert controller._last_error(card) == "Request timed out after 30s (twice)"    # a normal run's summary is no error
    assert controller._last_error({"_runs": [ok], "_comments": card["_comments"]}) == (
        "CHANGES REQUESTED: the parser drops the last line")
    assert controller._last_error({"_runs": [ok], "_comments": [{"author": "x", "body": "hello"}]}) == ""
    assert controller._last_error({}) == ""
    summary_only = {"_runs": [dict(failed_run(3), error=None, summary="worker died: caf" + chr(0xE9))]}
    assert controller._last_error(summary_only) == "worker died: caf\\xe9"
    assert len(controller._last_error({"_runs": [failed_run(1, error="e" * 900)]})) == 200


def test_num_text_shows_whole_numbers_and_one_decimal_for_minutes():
    assert [controller._num_text(v) for v in (2, 90.0, 61.25, 0.04, 45)] == ["2", "90", "61.2", "0", "45"]


def test_the_scheduled_reason_ties_are_broken_by_position_and_a_junk_list_is_survived():
    same_second = [{"kind": "scheduled", "payload": {"reason": "first"}, "created_at": 5},
                   {"kind": "scheduled", "payload": {"reason": "second"}, "created_at": 5}]
    assert controller._latest_scheduled_reason({"_events": same_second}) == "second"
    no_reason = {"kind": "scheduled", "payload": {}, "created_at": 9}
    assert controller._latest_scheduled_reason({"_events": [*same_second, no_reason]}) is None
    assert controller._latest_scheduled_reason({"_events": ["junk", None, {"kind": "blocked"}]}) is None
    assert controller._latest_scheduled_reason({}) is None


def test_the_branch_diff_is_the_attempt_alone_not_what_integration_gained_since(tmp_path):
    repo = git_repo(tmp_path)
    (repo / "later.txt").write_text("integration moved on\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "integration moves on", cwd=repo)
    task = types.SimpleNamespace(key="T1", role="coder")

    diff = controller._branch_diff(repo, "integration", {"branch_name": "swarm/T1-coder"}, task)
    by_default = controller._branch_diff(repo, "integration", {}, task)          # no branch_name: the plan's own name

    assert "+++ b/new.txt" in diff and "later.txt" not in diff
    assert by_default == diff
    assert controller._branch_diff(repo, "integration", {"branch_name": "swarm/T9-coder"}, task) == ""
    assert controller._branch_diff(tmp_path / "no-such-repo", "integration", {}, task) == ""
    assert controller._branch_diff(None, "integration", {}, task) == ""
    assert controller._branch_diff(repo, "no-such-branch", {"branch_name": "swarm/T1-coder"}, task) == ""


def test_the_last_gate_detail_is_the_newest_row_of_the_task_and_redacted(tmp_path):
    conn = db.connect(tmp_path / "ases.db")
    assert controller._last_gate_detail(conn, "T1", "p1") == ""
    insert = ("INSERT INTO gate_runs (task_key, gate, commit_sha, result, detail, ran_at, project) "
              "VALUES (?, ?, 'c', 'fail', ?, 'now', ?)")
    for gate, detail in (("gate1", "old"), ("gate3", f"new {SECRET}")):
        conn.execute(insert, ("T1", gate, detail, "p1"))
    conn.execute(insert, ("T2", "gate1", None, "p1"))

    assert controller._last_gate_detail(conn, "T1", "p1") == "new [redacted]"
    assert controller._last_gate_detail(conn, "T2", "p1") == ""
    # round 19, package GIT12 (ASES-GIT-12): strict scoping, not NULL-tolerant, proven in test_controller.py's
    # test_last_gate_detail_is_strictly_scoped_to_the_project (this text goes straight into a retry card body).


def test_findings_are_the_last_three_review_or_answer_comments_oldest_first():
    comments = [{"author": "r", "body": f"CHANGES REQUESTED: {n}"} for n in range(1, 6)]
    comments.insert(2, {"author": "u", "body": "  ANSWER: padded and prefixed"})
    comments.append({"author": "c", "body": "no prefix"})
    comments.append("junk")

    assert controller._findings_text({"_comments": comments}) == (
        "CHANGES REQUESTED: 3\n\nCHANGES REQUESTED: 4\n\nCHANGES REQUESTED: 5")
    assert controller._findings_text({"_comments": comments[:3]}) == (
        "CHANGES REQUESTED: 1\n\nCHANGES REQUESTED: 2\n\nANSWER: padded and prefixed")
    assert controller._findings_text({}) == ""


# =============================================================================================================
# failure recovery, end to end with the real recovery module
# =============================================================================================================

def spy_failures(monkeypatch):
    """Record what recovery.process_failures returns on each call, and still call it."""
    real = recovery.process_failures
    returned = []

    def spy(*args, **kwargs):
        result = real(*args, **kwargs)
        returned.append(list(result))
        return result

    monkeypatch.setattr(recovery, "process_failures", spy)
    return returned


def run_recovery(w, models=MODELS):
    return controller.process_recovery("b", w.repo, w.plan, w.project, models, conn=w.conn)


def test_a_capability_failure_gets_a_replacement_card_through_the_real_recovery_module(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    fail_work_card(w)

    rows = run_recovery(w)

    assert rows == [{"task_key": "T1", "action": "fresh_attempt", "kind": "capability"}]
    assert w.row()["work_card_id"] == "n5" and w.board.cards[w.work()]["status"] == "archived"
    assert recovery.load_lineage(w.conn, "t3", "T1").capability_failures == 1


def test_the_second_capability_failure_switches_model_on_the_replacement_card(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    recovery.bump(w.conn, "t3", "T1", "capability_failures")  # the first failure was already handled
    fail_work_card(w, run=failed_run(2))

    rows = run_recovery(w)

    assert rows == [{"task_key": "T1", "action": "switch_model", "kind": "capability"}]
    assert w.board.calls_of("kanban_set_model") == [(("b", "n5", "coder-alt"), {"provider": "xkiro"})]
    assert w.board.calls_of("kanban_unblock") == []  # the failed card is replaced, never resumed in place


def test_a_decision_is_not_lost_when_the_replacement_fails(tmp_path, monkeypatch):
    """The answer to the work order's question. recovery.process_failures records the decision and counts the failure
    BEFORE the controller acts, and never returns a decision for a run that already has one, so without the redo a
    failing Hermes call inside _start_fresh_attempt would lose the decision for good."""
    w = make_world(tmp_path, monkeypatch)
    fail_work_card(w)
    returned = spy_failures(monkeypatch)
    w.board.fail["kanban_create"] = hermes.HermesCommandError(["kanban", "create"], 1, "hermes went away")

    setup_creates = len(w.board.calls_of("kanban_create"))
    rows = run_recovery(w)                                     # pass 1: the create fails

    assert rows == [{"task_key": "T1", "action": "fresh_attempt", "kind": "capability"}]
    assert len(w.board.calls_of("kanban_create")) == setup_creates + 1   # tried once, not again in the same pass
    assert w.row()["work_card_id"] == w.work() and w.board.cards[w.work()]["status"] == "blocked"
    (decided,) = payloads(w.conn, "recovery_decision")
    assert decided["action"] == "fresh_attempt" and decided["applied"] is False and decided["run_id"] == 1
    assert len(payloads(w.conn, "retry_card_error")) == 1

    w.board.fail.pop("kanban_create")
    rows = run_recovery(w)                                     # pass 2: recovery has nothing to say, the redo does

    assert len(returned) == 2 and len(returned[0]) == 1
    assert returned[1] == []                                   # proof: recovery would never have returned it again
    assert rows == [{"task_key": "T1", "action": "fresh_attempt", "kind": "capability"}]
    assert w.row()["work_card_id"] == "n5" and w.board.cards[w.work()]["status"] == "archived"
    assert recovery.load_lineage(w.conn, "t3", "T1").capability_failures == 1  # counted once, not twice

    creates = len(w.board.calls_of("kanban_create"))
    assert run_recovery(w) == []                               # pass 3: nothing left to do
    assert len(w.board.calls_of("kanban_create")) == creates
    assert len(payloads(w.conn, "retry_card_created")) == 1


def test_a_switch_model_decision_is_redone_with_the_model_that_was_decided(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    recovery.bump(w.conn, "t3", "T1", "capability_failures")
    fail_work_card(w, run=failed_run(2))
    w.board.fail["kanban_link"] = hermes.HermesCommandError(["kanban", "link"], 1, "database is locked")
    run_recovery(w)                                            # pass 1: the create and the pin worked, the link failed
    w.board.fail.pop("kanban_link")

    rows = run_recovery(w)

    assert rows == [{"task_key": "T1", "action": "switch_model", "kind": "capability"}]
    assert w.row()["work_card_id"] == "n5" and w.board.cards["n5"]["model_override"] == "coder-alt"
    assert [c for c in w.board.cards.values() if c["title"].startswith("T1: retry")] == [w.board.cards["n5"]]


def test_a_redone_decision_is_dropped_when_the_card_was_resumed_meanwhile(tmp_path, monkeypatch):
    """A person answered the card, or unblocked it, between the passes. Archiving a card that is running again would end
    its worker (Hermes terminates the worker of an archived card), so the decision is dropped, once."""
    w = make_world(tmp_path, monkeypatch)
    fail_work_card(w)
    w.board.fail["kanban_create"] = hermes.HermesCommandError(["kanban", "create"], 1, "hermes went away")
    run_recovery(w)
    w.board.fail.pop("kanban_create")
    w.board.cards[w.work()]["status"] = "running"              # a person resumed it

    assert run_recovery(w) == []
    assert run_recovery(w) == []

    assert w.board.cards[w.work()]["status"] == "running" and w.row()["work_card_id"] == w.work()
    assert w.board.calls_of("kanban_archive") == []
    (skipped,) = payloads(w.conn, "retry_card_skipped")
    assert skipped["old_card"] == w.work() and skipped["run_id"] == 1 and "running" in skipped["reason"]


def test_a_redone_decision_is_dropped_when_the_card_has_a_newer_run(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    fail_work_card(w)
    w.board.fail["kanban_create"] = hermes.HermesCommandError(["kanban", "create"], 1, "hermes went away")
    run_recovery(w)
    w.board.fail.pop("kanban_create")
    w.board.cards[w.work()]["_runs"].append(failed_run(2))     # answered, ran again, failed again: a new decision's job
    monkeypatch.setattr(recovery, "process_failures", lambda *a, **kw: [])

    assert run_recovery(w) == []

    assert w.board.calls_of("kanban_archive") == [] and w.row()["work_card_id"] == w.work()
    assert "newer run" in payloads(w.conn, "retry_card_skipped")[0]["reason"]


def test_a_redone_decision_finishes_a_card_that_is_already_archived(tmp_path, monkeypatch):
    """The previous run archived the card and died before the repoint."""
    w = make_world(tmp_path, monkeypatch)
    fail_work_card(w)
    w.board.fail["kanban_create"] = hermes.HermesCommandError(["kanban", "create"], 1, "hermes went away")
    run_recovery(w)
    w.board.fail.pop("kanban_create")
    w.board.cards[w.work()]["status"] = "archived"

    rows = run_recovery(w)

    assert len(rows) == 1 and w.row()["work_card_id"] == "n5"
    assert w.board.calls_of("kanban_archive") == []


def test_a_decision_on_a_card_that_cannot_be_read_is_retried_not_dropped(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    fail_work_card(w)
    w.board.fail["kanban_create"] = hermes.HermesCommandError(["kanban", "create"], 1, "hermes went away")
    run_recovery(w)
    w.board.fail.pop("kanban_create")
    monkeypatch.setattr(recovery, "process_failures", lambda *a, **kw: [])
    w.board.fail["kanban_show"] = hermes.HermesCommandError(["kanban", "show"], 1, "timeout")

    assert run_recovery(w) == []  # recovery_action_error for the unreadable card, nothing dropped
    w.board.fail.pop("kanban_show")
    assert len(run_recovery(w)) == 1

    assert w.row()["work_card_id"] == "n5" and "retry_card_skipped" not in kinds(w.conn)


def test_an_applied_or_finished_decision_is_never_redone(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    ok = {"project": "t3", "task_key": "T1", "card_id": w.work(), "run_id": 1, "kind": "capability",
          "reason": "r", "model": None, "provider": None}
    events.record(w.conn, "recovery_decision", {**ok, "action": "resume", "applied": True})
    events.record(w.conn, "recovery_decision", {**ok, "action": "block_for_user", "applied": True})
    events.record(w.conn, "recovery_decision", {**ok, "action": "fresh_attempt", "applied": True, "run_id": 2})
    events.record(w.conn, "recovery_decision", {**ok, "action": "fresh_attempt", "applied": False, "run_id": 3})
    events.record(w.conn, "retry_card_created", {"project": "t3", "task_key": "T1", "old_card": w.work(), "run_id": 3})
    events.record(w.conn, "recovery_decision", {**ok, "action": "replan", "applied": False, "run_id": 4})
    events.record(w.conn, "replan_requested", {"project": "t3", "task_key": "T1", "card_id": w.work(), "run_id": 4})
    events.record(w.conn, "recovery_decision", {**ok, "action": "fresh_attempt", "applied": False, "run_id": 5,
                                                "card_id": "an_older_card"})  # the task moved on since

    assert controller._pending_decisions(w.conn, w.plan) == []


def test_pending_decisions_come_back_as_decisions_with_everything_recovery_decided(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    events.record(w.conn, "recovery_decision", {
        "project": "t3", "task_key": "T1", "card_id": w.work(), "run_id": 7, "kind": "runtime",
        "action": "switch_model", "reason": "second failure", "model": "coder-alt", "provider": "xkiro",
        "applied": False})
    events.record(w.conn, "recovery_decision", {
        "project": "t3", "task_key": "T2", "card_id": w.work("T2"), "run_id": "#0", "kind": "nonsense",
        "action": "replan", "reason": "spent", "model": None, "provider": None, "applied": False})
    events.record(w.conn, "recovery_decision", {
        "project": "another", "task_key": "T1", "card_id": "x", "run_id": 1, "kind": "capability",
        "action": "fresh_attempt", "reason": "r", "applied": False})

    first, second = controller._pending_decisions(w.conn, w.plan)
    assert (first.action, first.task_key, first.card_id, first.run_id, first.model, first.provider, first.reason) == (
        "switch_model", "T1", w.work(), 7, "coder-alt", "xkiro", "second failure")
    assert first.failure_kind is recovery.FailureKind.RUNTIME
    assert (second.action, second.run_id, second.failure_kind) == ("replan", "#0", None)
    assert controller._pending_decisions(w.conn, w.plan, skip={(w.work(), 7)}) == [second]


# =============================================================================================================
# _request_replan
# =============================================================================================================

def test_a_replan_asks_the_user_and_counts_it(tmp_path, monkeypatch):
    """ASES-REC-02: the Lead may re-plan once, then the user is asked. The Lead call is not automated, so the person is
    asked what to do, told the command that retries the same card with their guidance, and the re-plan is counted."""
    w = make_world(tmp_path, monkeypatch)
    fail_work_card(w, run=failed_run(1, error=f"Traceback ... key {SECRET} rejected"))
    asker = Asker(monkeypatch)
    task = w.plan.task("T1")
    d = decision(w, recovery.ACTION_REPLAN, reason="The attempt budget of this task is spent (3 failed attempts).")

    assert controller._request_replan("b", w.plan, w.project, task, w.work(), d, conn=w.conn) is True

    ((card_id, text),) = asker.asked
    assert card_id == w.work()
    assert text.startswith("T1: capability problem, last error: ") and "rejected" in text
    assert SECRET not in text and "The attempt budget of this task is spent" in text
    assert f'swarm answer {w.work()} "<guidance>"' in text and "How should this task proceed?" in text
    assert all(ord(ch) < 128 for ch in text)
    assert recovery.load_lineage(w.conn, "t3", "T1").replans == 1
    assert bounds.get_state(w.conn, "t3")["replans"] == 1
    (event,) = payloads(w.conn, "replan_requested")
    assert event["task_key"] == "T1" and event["card_id"] == w.work() and event["run_id"] == 1
    assert event["kind"] == "capability" and event["asked"] == "commented"
    assert (event["review_rounds"], event["fix_cards"], event["capability_failures"]) == (0, 0, 0)


def test_a_replan_that_cannot_ask_counts_nothing_and_is_asked_again_later(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    fail_work_card(w)
    Asker(monkeypatch, answer=hermes.HermesCommandError(["kanban", "comment"], 1, "timeout"))
    task = w.plan.task("T1")
    d = decision(w, recovery.ACTION_REPLAN)

    assert controller._request_replan("b", w.plan, w.project, task, w.work(), d, conn=w.conn) is False
    assert controller._request_replan("b", w.plan, w.project, task, w.work(), d, conn=w.conn) is False

    assert recovery.load_lineage(w.conn, "t3", "T1").replans == 0 and bounds.get_state(w.conn, "t3") is None
    assert "replan_requested" not in kinds(w.conn)
    assert len(payloads(w.conn, "replan_error")) == 1                        # recorded once, not per pass


def test_a_replan_decision_is_redone_after_a_failed_question_through_the_real_recovery_module(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch, budgets={"attempts_per_card": 1})
    fail_work_card(w)                                                         # the first failure of 1 spends the budget
    asker = Asker(monkeypatch, answer=hermes.HermesCommandError(["kanban", "comment"], 1, "timeout"))

    first = run_recovery(w)                                                   # decide replan; the question fails
    asker.answer = "commented"
    second = run_recovery(w)                                                  # redone: asked, counted

    assert first == [{"task_key": "T1", "action": "replan", "kind": "capability"}] == second
    assert recovery.load_lineage(w.conn, "t3", "T1").replans == 1
    assert len(payloads(w.conn, "replan_requested")) == 1 and run_recovery(w) == []


def test_a_replan_decision_is_dropped_when_the_card_is_no_longer_blocked(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch, budgets={"attempts_per_card": 1})
    fail_work_card(w)
    asker = Asker(monkeypatch, answer=hermes.HermesCommandError(["kanban", "comment"], 1, "timeout"))
    run_recovery(w)
    w.board.cards[w.work()]["status"] = "ready"

    assert run_recovery(w) == []

    assert payloads(w.conn, "replan_skipped")[0]["card_id"] == w.work() and len(asker.asked) == 1
    assert recovery.load_lineage(w.conn, "t3", "T1").replans == 0


# =============================================================================================================
# _escalate_spent_budgets: review rounds
# =============================================================================================================

def spend_review_rounds(w, n, key="T1"):
    recovery.bump(w.conn, "t3", key, "review_rounds", n)


def test_a_spent_review_budget_asks_for_a_replan_once_and_a_person_is_not_asked_again_at_once(tmp_path, monkeypatch):
    """Table 17: "Review rounds per plan task 3: Escalate to the Lead, then to the user". After the answer the same
    counters must not produce the same question again: the next escalation needs another round."""
    w = make_world(tmp_path, monkeypatch)
    asker = Asker(monkeypatch)
    spend_review_rounds(w, 3)                                   # the work card is `ready` (or `todo`): not running

    first = run_recovery(w)
    second = run_recovery(w)

    assert first == [{"task_key": "T1", "action": "replan", "kind": "review_rounds"}] and second == []
    assert len(asker.asked) == 1 and "review-round budget" in asker.asked[0][1]
    assert recovery.load_lineage(w.conn, "t3", "T1").replans == 1
    assert payloads(w.conn, "replan_requested")[0]["review_rounds"] == 3


def test_a_further_review_round_after_the_replan_asks_the_user_directly(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    asker = Asker(monkeypatch)
    spend_review_rounds(w, 3)
    run_recovery(w)                                             # the re-plan question
    spend_review_rounds(w, 1)                                   # the guided attempt was rejected again: 4 rounds

    third = run_recovery(w)
    fourth = run_recovery(w)

    assert third == [{"task_key": "T1", "action": "block_for_user", "kind": "review_rounds"}] and fourth == []
    assert len(asker.asked) == 2
    text = asker.asked[1][1]
    assert text.startswith("T1: The review-round budget of this task is spent again") and text.rstrip().endswith("?")
    (event,) = payloads(w.conn, "lineage_escalated")
    assert event["task_key"] == "T1" and event["review_rounds"] == 4 and event["asked"] == "commented"


@pytest.mark.parametrize("status", ["running", "done", "archived"])
def test_a_spent_review_budget_leaves_a_running_finished_or_archived_card_alone(tmp_path, monkeypatch, status):
    """Blocking a running card would cut its worker off mid-attempt."""
    w = make_world(tmp_path, monkeypatch)
    asker = Asker(monkeypatch)
    spend_review_rounds(w, 3)
    w.board.cards[w.work()]["status"] = status

    assert run_recovery(w) == [] and asker.asked == []
    assert "replan_requested" not in kinds(w.conn)


def test_a_spent_review_budget_waits_while_the_card_has_another_open_question(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    asker = Asker(monkeypatch)
    spend_review_rounds(w, 3)
    monkeypatch.setattr(questions, "open_question", lambda card: Q() if card["id"] == w.work() else None, raising=False)

    assert run_recovery(w) == [] and asker.asked == []

    monkeypatch.setattr(questions, "open_question", lambda card: None, raising=False)  # the person answered it
    assert len(run_recovery(w)) == 1 and len(asker.asked) == 1


def test_a_spent_review_budget_is_escalated_only_when_it_is_really_spent(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch, budgets={"review_rounds_per_task": 4})
    asker = Asker(monkeypatch)
    spend_review_rounds(w, 3)

    assert run_recovery(w) == [] and asker.asked == []
    spend_review_rounds(w, 1)
    assert len(run_recovery(w)) == 1


def test_a_zero_review_round_limit_does_not_escalate_a_task_that_has_had_no_round(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch, budgets={"review_rounds_per_task": 0})
    asker = Asker(monkeypatch)

    assert run_recovery(w) == [] and asker.asked == []


def test_the_fix_card_and_attempt_budgets_are_left_to_their_own_owners(tmp_path, monkeypatch):
    """Table 17: fix cards "Escalate to the user" (the merge queue does it at the failure that would need one more;
    acting when the count merely reaches the limit would block a fix card that has not run yet), and attempts are
    decided from the failed run by process_failures."""
    w = make_world(tmp_path, monkeypatch)
    asker = Asker(monkeypatch)
    w.conn.execute("UPDATE plan_tasks SET fix_cards = 2 WHERE project = 't3' AND task_key = 'T1'")
    recovery.bump(w.conn, "t3", "T2", "capability_failures", 3)

    assert run_recovery(w) == [] and asker.asked == []


def test_a_spent_review_budget_asks_the_user_once_the_projects_replans_are_used(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    asker = Asker(monkeypatch)
    recovery.bump(w.conn, "t3", "T2", "replans", 2)             # another task used both project re-plans
    spend_review_rounds(w, 3)

    rows = run_recovery(w)

    assert rows == [{"task_key": "T1", "action": "block_for_user", "kind": "review_rounds"}]
    assert "re-plans" in asker.asked[0][1] and asker.asked[0][1].rstrip().endswith("?")
    assert recovery.load_lineage(w.conn, "t3", "T1").replans == 0     # no re-plan was spent


def test_one_task_that_cannot_be_escalated_does_not_stop_the_others(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    asker = Asker(monkeypatch)
    spend_review_rounds(w, 3, "T1")
    spend_review_rounds(w, 3, "T2")
    w.board.hooks["kanban_show"] = lambda board, cid: (_ for _ in ()).throw(
        hermes.HermesCommandError(["kanban", "show", cid], 1, "gone")) if cid == w.work("T1") else None
    monkeypatch.setattr(recovery, "refresh_review_rounds", lambda *a, **kw: {})
    monkeypatch.setattr(recovery, "process_failures", lambda *a, **kw: [])

    rows = run_recovery(w)

    assert rows == [{"task_key": "T2", "action": "replan", "kind": "review_rounds"}]
    assert "gone" in payloads(w.conn, "recovery_action_error")[0]["error"]
    assert [cid for cid, _ in asker.asked] == [w.work("T2")]


# =============================================================================================================
# process_reviewer_contract (round 19, package REVIEWLADDER; ASES-ROL-05, ASES-ROL-06, ASES-REV-05, ASES-REC-01,
# ASES-REC-05)
# =============================================================================================================

REVIEWER_LADDER_PLAN = {
    "project": "t3", "integration_branch": "integration",
    "gate_profiles": {"trivial": ["echo ok"]},
    "tasks": [
        {"key": "T1", "title": "scaffold", "role": "coder", "depends_on": [], "touches": ["new.txt"],
         "acceptance": ["exists"], "gate_profile": "trivial", "estimated_requests": 10},
    ],
}

EVIDENCE_REASON = (
    "I cannot run the test suite myself to verify these changes work. Please provide test evidence or "
    "execution output showing the tests pass."
)
GENUINE_QUESTION_REASON = "Should slugify keep underscores? The spec says letters or digits."


def _reviewer_world(tmp_path, monkeypatch):
    """A world whose repository is a REAL one (git_repo: integration plus swarm/T1-coder holding one commit that
    adds new.txt) and whose only plan task's touches match it -- the only tests in this file that let a real
    review.record_gate1 reach gates_mod.run_gate (stubbed PASS by the autouse _post_merge_check_passes fixture
    unless a test overrides it)."""
    w = make_world(tmp_path, monkeypatch, plan_raw=REVIEWER_LADDER_PLAN)
    git_repo(tmp_path)  # tmp_path / "repo" == w.repo
    return w


def _stub_gate1_result(monkeypatch, *, passed, detail="gate1 output"):
    """gates_mod.run_gate, accepting **kwargs so it tolerates self_contained_checkout and anything else
    gates.resolve_runner/_run_gate1 may pass -- unlike the autouse _post_merge_check_passes fixture's own
    `lenient`, which this replaces for the length of one test."""
    calls = []

    def fake(repo, commit_sha, gate_name, commands, **kwargs):
        calls.append((commit_sha, gate_name))
        return gates_mod.GateResult(gate_name, commit_sha, passed, detail)

    monkeypatch.setattr(gates_mod, "run_gate", fake)
    return calls


def _coder_handoff_run(commit_sha, run_id=1):
    return {"id": run_id, "profile": "coder-1", "outcome": "review_requested", "status": "review_requested",
            "summary": "ready for review", "error": None,
            "metadata": {"commit_sha": commit_sha, "changed_files": ["new.txt"], "residual_risk": "none"},
            "started_at": 1000, "ended_at": 1010}


def _reviewer_run(run_id, *, outcome, reason, started_at=2000, ended_at=2010, metadata=None):
    return {"id": run_id, "profile": "reviewer", "outcome": outcome, "status": outcome, "summary": reason,
            "error": None, "metadata": metadata or {}, "started_at": started_at, "ended_at": ended_at}


def _put_reviewer_stop(
    w, *, key="T1", coder_commit=None, reviewer_run, claimed_source_status="review", block_recurrences=1,
    card_status=None,
):
    """A work card carrying a coder hand-off (unless `coder_commit` is False) followed by one reviewer-profile
    run, with the `claimed`/`blocked`/`changes_requested` events a real Hermes run_id-correlates to it (see
    controller._event_for_run, _claimed_source_status, _block_recurrences)."""
    card_id = w.work(key)
    runs = [] if coder_commit is False else [_coder_handoff_run(coder_commit or "a" * 40)]
    runs.append(reviewer_run)
    card = w.board.cards[card_id]
    card["_runs"] = runs
    card["status"] = card_status or {"blocked": "blocked", "changes_requested": "ready",
                                      "review_requested": "review"}[reviewer_run["outcome"]]
    card["branch_name"] = "swarm/T1-coder"
    w.board.add_event(card_id, "claimed", {"source_status": claimed_source_status}, run_id=reviewer_run["id"])
    if reviewer_run["outcome"] == "blocked":
        w.board.add_event(card_id, "blocked", {
            "reason": reviewer_run["summary"], "kind": "needs_input", "recurrences": block_recurrences,
            "source_status": claimed_source_status,
        }, run_id=reviewer_run["id"])
    elif reviewer_run["outcome"] == "changes_requested":
        w.board.add_event(card_id, "changes_requested", {
            "reason": reviewer_run["summary"], "implementer": "coder-1", "reviewer": "reviewer", "status": "ready",
        }, run_id=reviewer_run["id"])
    return card


def test_protocol_stop_is_a_capability_failure_and_provenance_broken_never_unblocked(tmp_path, monkeypatch):
    w = _reviewer_world(tmp_path, monkeypatch)
    asker = Asker(monkeypatch)
    _put_reviewer_stop(w, reviewer_run=_reviewer_run(2, outcome="review_requested", reason="handing off"))

    rows = controller.process_reviewer_contract("b", w.repo, w.plan, w.project, conn=w.conn)

    assert rows == [{"task_key": "T1", "card_id": w.work(), "kind": "PROTOCOL", "action": "protocol",
                      "answered": "commented"}]
    assert payloads(w.conn, "reviewer_contract_failure")[0]["kind"] == "PROTOCOL"
    assert payloads(w.conn, "provenance_broken") and asker.asked
    assert w.board.cards[w.work()]["status"] == "review"  # never unblocked or reopened
    assert "unblock" not in w.board.writes() and "kanban_comment" not in [c[0] for c in w.board.calls]

    # A second pass takes no new action: the dedup event already exists for this exact run_id.
    asker.asked.clear()
    rows2 = controller.process_reviewer_contract("b", w.repo, w.plan, w.project, conn=w.conn)
    assert rows2 == [{"task_key": "T1", "card_id": w.work(), "kind": "PROTOCOL", "action": "already_decided"}]
    assert asker.asked == []
    assert len(payloads(w.conn, "reviewer_contract_failure")) == 1


def test_a_second_distinct_protocol_stop_on_the_still_broken_card_is_not_asked_again(tmp_path, monkeypatch):
    """Round 19 fix round 2 (reviewer finding, major, with a live repro): a PROTOCOL card's status stays "review"
    forever by design (_protocol_stop never unblocks or reopens it), so Hermes redispatching the SAME reviewer
    profile onto the SAME still-broken card over time (the real t_4ae270eb incident: runs 33 to 40 all landed
    back on the reviewer profile) produces further, genuinely NEW run_ids on ONE unresolved situation. Unlike the
    sibling test above, no Asker stub is installed: this drives the REAL questions.ask_user for both stops. Before
    this fix, ask_user's own dedup (`open_question(card)`, which never sees a "review"-status card) missed the
    repeat, and the independent reviewer confirmed live that two distinct run_ids produced two "ASES QUESTION:"
    comments and two provenance_broken events -- this pins the comment side of that (the provenance_broken and
    reviewer_contract_failure counts are deliberately NOT deduped across run_ids: each is a genuinely new stop,
    counted as one, exactly like the existing dedup test above already shows for a repeat of the SAME run_id)."""
    w = _reviewer_world(tmp_path, monkeypatch)
    _put_reviewer_stop(w, reviewer_run=_reviewer_run(2, outcome="review_requested", reason="handing off"))

    rows1 = controller.process_reviewer_contract("b", w.repo, w.plan, w.project, conn=w.conn)
    assert rows1[0]["answered"] == "commented"

    # Hermes redispatches the same broken card under a brand-new run_id: same profile, same card, still "review".
    card = w.board.cards[w.work()]
    card["_runs"].append(_reviewer_run(3, outcome="review_requested", reason="handing off"))
    w.board.add_event(w.work(), "claimed", {"source_status": "review"}, run_id=3)

    rows2 = controller.process_reviewer_contract("b", w.repo, w.plan, w.project, conn=w.conn)

    assert rows2 == [{"task_key": "T1", "card_id": w.work(), "kind": "PROTOCOL", "action": "protocol",
                       "answered": "already_asked"}]
    assert w.board.cards[w.work()]["status"] == "review"
    questions_posted = [a[2] for n, a, kw in w.board.calls if n == "kanban_comment" and a[2].startswith("ASES QUESTION:")]
    assert len(questions_posted) == 1


def test_a_security_specialisation_reviewer_run_is_classified_like_the_reviewer_role(tmp_path, monkeypatch):
    """Round 19 fix round 2 (reviewer finding, major): Layer 1's hooks already cover every active
    kanban_lifecycle_only profile (profiles.reviewer_hook_problems), including the built-in "security"
    specialisation (RoleDef.played_by "reviewer") once config/swarm.yaml roles: maps it -- but before this fix,
    Layer 2 (process_reviewer_contract's own reviewer_profiles) only ever built
    frozenset({controller._reviewer_profile(project)}), the literal reviewer role, so a security-profile run
    that broke the contract was invisible here. project.roles is a plain dict on a frozen ProjectConfig, so it is
    mutated in place, the same shape config/swarm.yaml's roles: map would take for a project that configures the
    security specialisation."""
    w = _reviewer_world(tmp_path, monkeypatch)
    w.project.roles["security"] = "security-1"
    run = dict(_reviewer_run(2, outcome="review_requested", reason="handing off"), profile="security-1")
    _put_reviewer_stop(w, reviewer_run=run)

    rows = controller.process_reviewer_contract("b", w.repo, w.plan, w.project, conn=w.conn)

    assert rows == [{"task_key": "T1", "card_id": w.work(), "kind": "PROTOCOL", "action": "protocol",
                      "answered": "commented"}]
    assert payloads(w.conn, "reviewer_contract_failure")[0]["profile"] == "security-1"
    assert payloads(w.conn, "provenance_broken")


def test_process_recovery_excludes_a_security_profiles_evidence_changes_requested_too(tmp_path, monkeypatch):
    """The sibling call site of the same fix (round 19 fix round 2, reviewer finding, major):
    process_recovery's own call to recovery.refresh_review_rounds built the identical single-literal-name
    frozenset, so the D5 exclusion (fix D5: an evidence-only changes_requested is not a review round) silently
    stopped applying whenever the reviewer-class run doing the evidence-stalling was a "security" specialisation
    rather than the literal reviewer profile. Gate 1 is stubbed PASS so process_recovery's OTHER steps (which
    also read the branch) find nothing to act on; only the review-rounds count is under test here."""
    w = _reviewer_world(tmp_path, monkeypatch)
    w.project.roles["security"] = "security-1"
    _stub_gate1_result(monkeypatch, passed=True, detail="1 passed")
    commit = git("rev-parse", "swarm/T1-coder", cwd=w.repo).strip()
    run = dict(_reviewer_run(2, outcome="changes_requested", reason=EVIDENCE_REASON), profile="security-1")
    _put_reviewer_stop(w, coder_commit=commit, reviewer_run=run)

    controller.process_recovery("b", w.repo, w.plan, w.project, MODELS, conn=w.conn)

    assert recovery.load_lineage(w.conn, w.plan.project, "T1").review_rounds == 0


def test_swarm_questions_lists_a_protocol_stop_although_the_card_stays_in_review(tmp_path, monkeypatch):
    """Round 19 fix round 1 (reviewer finding, blocker): ASES-REC-05 ("swarm questions lists open questions with
    their cards") must hold even for a PROTOCOL stop, whose card Hermes leaves in "review" forever (block_task
    accepts only "ready"/"running", research report finding F2 -- ASES can never Hermes-block it either). Unlike
    the sibling test above, no Asker stub is installed here: this drives the REAL questions.ask_user, so it
    actually reaches the "commented, not blocked" fallback the mocked test cannot see, and the real
    questions.list_questions/open_question/answer_question afterward -- the exact gap the independent review
    proved live against a copy of the source (ask_user returned "commented" and a later open_question on the
    same card returned None)."""
    w = _reviewer_world(tmp_path, monkeypatch)
    _put_reviewer_stop(w, reviewer_run=_reviewer_run(2, outcome="review_requested", reason="handing off"))

    rows = controller.process_reviewer_contract("b", w.repo, w.plan, w.project, conn=w.conn)
    assert rows[0]["answered"] == "commented"   # the real ask_user: status "review" cannot be Hermes-blocked

    found = questions.list_questions("b", w.plan, conn=w.conn)

    assert [q.card_id for q in found] == [w.work()]
    assert found[0].source == "ases_comment" and found[0].task_key == "T1"

    # Still never answerable through the ordinary path: design step 6, "never unblocked or reopened by ASES".
    with pytest.raises(questions.QuestionError, match="review"):
        questions.answer_question("b", w.work(), "use candidate B", conn=w.conn)
    assert w.board.cards[w.work()]["status"] == "review"  # the refusal above never touched the card


def test_evidence_blocked_never_reruns_gate1_for_an_already_decided_run(tmp_path, monkeypatch):
    """Round 19 fix round 1 (reviewer finding, major): design step 5 leaves an ask-owner card blocked
    indefinitely ("never unblock, never retry" -- the human may take hours or days to answer), and
    _latest_contract_run keeps returning the SAME already-decided run for as long as nothing new happens on the
    card, so a controller pass (every ~20s) with nothing new to decide for this run_id must do NO gate work at
    all. Before this fix the run_id dedup check ran AFTER review_mod.record_gate1, so the whole Gate 1 suite
    reran on every single pass regardless, with the fresh result thrown away by the dedup check moments later."""
    w = _reviewer_world(tmp_path, monkeypatch)
    calls = _stub_gate1_result(monkeypatch, passed=True, detail="1 passed")
    Asker(monkeypatch)
    # recurrences=2 makes _answer_once_eligible False, so this always resolves to "ask_owner" and stays
    # unresolved: a stop the ladder can never answer, exactly the shape that used to rerun Gate 1 forever.
    _put_reviewer_stop(w, reviewer_run=_reviewer_run(2, outcome="blocked", reason=EVIDENCE_REASON),
                        block_recurrences=2)

    rows1 = controller.process_reviewer_contract("b", w.repo, w.plan, w.project, conn=w.conn)
    assert rows1 == [{"task_key": "T1", "card_id": w.work(), "kind": "EVIDENCE", "action": "ask_owner",
                       "answered": "commented"}]
    assert len(calls) == 1   # Gate 1 ran once, to produce the gate record the ANSWER/ask-owner text cites

    rows2 = controller.process_reviewer_contract("b", w.repo, w.plan, w.project, conn=w.conn)
    assert rows2 == [{"task_key": "T1", "card_id": w.work(), "kind": "EVIDENCE", "action": "already_decided"}]
    assert len(calls) == 1   # NOT rerun on a second pass over the exact same, still-unresolved run_id


def test_evidence_blocked_retries_after_a_gate_infrastructure_error_instead_of_freezing(tmp_path, monkeypatch):
    """Round 19 architect fix (the reviewer's last major finding): Gate 1 that could not even run (Docker down, the
    image missing) says nothing about the commit, so nothing may be decided for that run: the error is recorded
    once, and the next pass, with the sandbox back, decides normally. Before the fix the decision event was written
    on the failed pass ("gate_unknown_stop"), so that run_id was never looked at again."""
    w = _reviewer_world(tmp_path, monkeypatch)
    commit = git("rev-parse", "swarm/T1-coder", cwd=w.repo).strip()
    _put_reviewer_stop(w, coder_commit=commit, reviewer_run=_reviewer_run(2, outcome="blocked", reason=EVIDENCE_REASON))
    outage = {"down": True}

    def gate(repo, commit_sha, gate_name, commands, **kwargs):
        if outage["down"]:
            raise sandbox_mod.SandboxInfrastructureError("the sandbox is enabled but docker CLI not found on PATH")
        return gates_mod.GateResult(gate_name, commit_sha, True, "1 passed")

    monkeypatch.setattr(gates_mod, "run_gate", gate)

    rows1 = controller.process_reviewer_contract("b", w.repo, w.plan, w.project, conn=w.conn)
    rows1b = controller.process_reviewer_contract("b", w.repo, w.plan, w.project, conn=w.conn)
    assert rows1 == rows1b == [{"task_key": "T1", "card_id": w.work(), "kind": "EVIDENCE",
                                "action": "gate_infrastructure_retry"}]
    assert payloads(w.conn, "reviewer_contract_decision") == []
    assert len(payloads(w.conn, "reviewer_contract_gate_error")) == 1   # recorded once, not every pass

    outage["down"] = False
    rows2 = controller.process_reviewer_contract("b", w.repo, w.plan, w.project, conn=w.conn)
    assert rows2 == [{"task_key": "T1", "card_id": w.work(), "kind": "EVIDENCE", "action": "answer_once"}]
    assert w.board.cards[w.work()]["status"] == "ready"


def test_evidence_blocked_answer_once_posts_the_gate_pass_and_unblocks(tmp_path, monkeypatch):
    w = _reviewer_world(tmp_path, monkeypatch)
    _stub_gate1_result(monkeypatch, passed=True, detail="1 passed")
    commit = git("rev-parse", "swarm/T1-coder", cwd=w.repo).strip()
    _put_reviewer_stop(w, coder_commit=commit, reviewer_run=_reviewer_run(2, outcome="blocked", reason=EVIDENCE_REASON))

    rows = controller.process_reviewer_contract("b", w.repo, w.plan, w.project, conn=w.conn)

    assert rows == [{"task_key": "T1", "card_id": w.work(), "kind": "EVIDENCE", "action": "answer_once"}]
    assert payloads(w.conn, "reviewer_evidence_answered") == [{"project": "t3", "task_key": "T1", "commit": commit}]
    comments = [c for n, a, kw in w.board.calls if n == "kanban_comment" for c in [a[2]]]
    assert any(c.startswith("ANSWER:") and commit in c for c in comments)
    # Round 19 fix round 1 (reviewer finding, minor): design (a) line 198's own template quotes "at <ts>".
    assert any(f"commit {commit} at " in c and c.split(f"commit {commit} at ", 1)[1][:4].isdigit()
               for c in comments)
    assert any("ASES gate record" in c for c in comments)  # review.record_gate1's own comment
    assert w.board.cards[w.work()]["status"] == "ready"  # unblocked

    # Never twice for the same commit: unblock it again with a second same-kind block on the SAME commit.
    w.board.cards[w.work()]["status"] = "blocked"
    _put_reviewer_stop(w, coder_commit=commit, reviewer_run=_reviewer_run(3, outcome="blocked", reason=EVIDENCE_REASON),
                       block_recurrences=1)
    rows2 = controller.process_reviewer_contract("b", w.repo, w.plan, w.project, conn=w.conn)
    assert rows2 == [{"task_key": "T1", "card_id": w.work(), "kind": "EVIDENCE", "action": "ask_owner",
                       "answered": "commented"}]
    assert len(payloads(w.conn, "reviewer_evidence_answered")) == 1


def test_evidence_blocked_red_gate_stops_with_no_unblock_and_no_extra_question(tmp_path, monkeypatch):
    w = _reviewer_world(tmp_path, monkeypatch)
    _stub_gate1_result(monkeypatch, passed=False, detail="1 failed")
    asker = Asker(monkeypatch)
    commit = git("rev-parse", "swarm/T1-coder", cwd=w.repo).strip()
    _put_reviewer_stop(w, coder_commit=commit, reviewer_run=_reviewer_run(2, outcome="blocked", reason=EVIDENCE_REASON))

    rows = controller.process_reviewer_contract("b", w.repo, w.plan, w.project, conn=w.conn)

    assert rows == [{"task_key": "T1", "card_id": w.work(), "kind": "EVIDENCE", "action": "gate_red_stop"}]
    assert asker.asked == []
    assert w.board.cards[w.work()]["status"] == "blocked"  # untouched: the ordinary gate record comment is on it
    comments = [a[2] for n, a, kw in w.board.calls if n == "kanban_comment"]
    assert any("ASES gate record" in c and "fail" in c.lower() for c in comments)


@pytest.mark.parametrize("bad", ["recurrences", "source_status", "no_commit"])
def test_evidence_blocked_asks_the_owner_instead_of_answering_once_when_ineligible(tmp_path, monkeypatch, bad):
    w = _reviewer_world(tmp_path, monkeypatch)
    _stub_gate1_result(monkeypatch, passed=True, detail="1 passed")
    asker = Asker(monkeypatch)
    commit = None if bad == "no_commit" else git("rev-parse", "swarm/T1-coder", cwd=w.repo).strip()
    kwargs = {"coder_commit": False if bad == "no_commit" else commit,
              "block_recurrences": 2 if bad == "recurrences" else 1,
              "claimed_source_status": "ready" if bad == "source_status" else "review"}
    _put_reviewer_stop(w, reviewer_run=_reviewer_run(2, outcome="blocked", reason=EVIDENCE_REASON), **kwargs)

    rows = controller.process_reviewer_contract("b", w.repo, w.plan, w.project, conn=w.conn)

    assert rows == [{"task_key": "T1", "card_id": w.work(), "kind": "EVIDENCE", "action": "ask_owner",
                      "answered": "commented"}]
    assert len(asker.asked) == 1 and asker.asked[0][0] == w.work()
    assert payloads(w.conn, "reviewer_evidence_answered") == []


def test_evidence_changes_requested_comments_and_asks_never_unblocks(tmp_path, monkeypatch):
    w = _reviewer_world(tmp_path, monkeypatch)
    asker = Asker(monkeypatch)
    commit = git("rev-parse", "swarm/T1-coder", cwd=w.repo).strip()
    _put_reviewer_stop(w, coder_commit=commit,
                        reviewer_run=_reviewer_run(2, outcome="changes_requested", reason=EVIDENCE_REASON))

    rows = controller.process_reviewer_contract("b", w.repo, w.plan, w.project, conn=w.conn)

    assert rows == [{"task_key": "T1", "card_id": w.work(), "kind": "EVIDENCE", "action": "evidence_changes_requested",
                      "answered": "commented"}]
    comments = [a[2] for n, a, kw in w.board.calls if n == "kanban_comment"]
    assert any(c.startswith("ANSWER:") and "not counted as a review round" in c for c in comments)
    assert "kanban_unblock" not in [n for n, a, kw in w.board.calls]
    assert payloads(w.conn, "reviewer_contract_failure")[0]["kind"] == "EVIDENCE"
    assert asker.asked and asker.asked[0][0] == w.work()

    # A second pass over the SAME run_id (the card still shows the identical changes_requested run: nothing
    # about it changes just because ASES looked at it) comments and asks nothing new.
    asker.asked.clear()
    comment_calls_before = sum(1 for n, a, kw in w.board.calls if n == "kanban_comment")
    rows2 = controller.process_reviewer_contract("b", w.repo, w.plan, w.project, conn=w.conn)
    assert rows2 == [{"task_key": "T1", "card_id": w.work(), "kind": "EVIDENCE", "action": "already_decided"}]
    assert asker.asked == []
    assert sum(1 for n, a, kw in w.board.calls if n == "kanban_comment") == comment_calls_before
    assert len(payloads(w.conn, "reviewer_contract_failure")) == 1


def test_a_second_distinct_evidence_changes_requested_stop_on_the_same_commit_is_not_asked_again(
    tmp_path, monkeypatch,
):
    """The same ask_user dedup gap as the PROTOCOL test above, for the sibling EVIDENCE/changes_requested branch
    (round 19 fix round 2, reviewer finding, major): this card is "ready", not "blocked", the moment it is read,
    also outside open_question's status gate. Real ask_user, no Asker stub: kanban_block is made to fail once
    (a race where Hermes has already moved the card on by the time the controller's own block call lands is
    exactly the shape that leaves a comment-only question sitting on a "ready" card), so the first stop falls
    back to the comment ask_user's docstring describes, and the card is never actually blocked. A second,
    genuinely new run_id on the SAME commit must still produce only ONE "ASES QUESTION:" comment -- the
    "ANSWER: ... not counted" note is deliberately posted every time (design (a) step 7) and is not part of this
    dedup, so it is not asserted on here."""
    w = _reviewer_world(tmp_path, monkeypatch)
    commit = git("rev-parse", "swarm/T1-coder", cwd=w.repo).strip()
    w.board.fail["kanban_block"] = hermes.HermesCommandError(["kanban", "block"], 1, "card moved on")
    _put_reviewer_stop(w, coder_commit=commit,
                        reviewer_run=_reviewer_run(2, outcome="changes_requested", reason=EVIDENCE_REASON))

    rows1 = controller.process_reviewer_contract("b", w.repo, w.plan, w.project, conn=w.conn)
    assert rows1[0]["answered"] == "commented"

    card = w.board.cards[w.work()]
    card["_runs"].append(_reviewer_run(3, outcome="changes_requested", reason=EVIDENCE_REASON))
    w.board.add_event(w.work(), "changes_requested", {
        "reason": EVIDENCE_REASON, "implementer": "coder-1", "reviewer": "reviewer", "status": "ready",
    }, run_id=3)

    rows2 = controller.process_reviewer_contract("b", w.repo, w.plan, w.project, conn=w.conn)

    assert rows2 == [{"task_key": "T1", "card_id": w.work(), "kind": "EVIDENCE", "action": "evidence_changes_requested",
                       "answered": "already_asked"}]
    questions_posted = [a[2] for n, a, kw in w.board.calls if n == "kanban_comment" and a[2].startswith("ASES QUESTION:")]
    assert len(questions_posted) == 1


def test_a_genuine_question_block_is_not_a_reviewer_contract_stop(tmp_path, monkeypatch):
    w = _reviewer_world(tmp_path, monkeypatch)
    asker = Asker(monkeypatch)
    _put_reviewer_stop(w, reviewer_run=_reviewer_run(2, outcome="blocked", reason=GENUINE_QUESTION_REASON))
    before = kinds(w.conn)

    assert controller.process_reviewer_contract("b", w.repo, w.plan, w.project, conn=w.conn) == []
    assert asker.asked == [] and kinds(w.conn) == before


def test_a_non_reviewer_profiles_block_is_never_classified_even_with_evidence_shaped_text(tmp_path, monkeypatch):
    w = _reviewer_world(tmp_path, monkeypatch)
    asker = Asker(monkeypatch)
    run = dict(_reviewer_run(2, outcome="blocked", reason=EVIDENCE_REASON), profile="coder-1")
    _put_reviewer_stop(w, reviewer_run=run)
    before = kinds(w.conn)

    assert controller.process_reviewer_contract("b", w.repo, w.plan, w.project, conn=w.conn) == []
    assert asker.asked == [] and kinds(w.conn) == before


def test_a_completed_reviewer_run_after_a_stop_is_not_reconsidered(tmp_path, monkeypatch):
    """The LATEST run decides, not any earlier one: once a fresh reviewer run completes normally, an older
    blocked run of the same card must never be re-classified."""
    w = _reviewer_world(tmp_path, monkeypatch)
    asker = Asker(monkeypatch)
    card = _put_reviewer_stop(w, reviewer_run=_reviewer_run(2, outcome="blocked", reason=EVIDENCE_REASON))
    card["_runs"].append({"id": 3, "profile": "reviewer", "outcome": "completed", "status": "completed",
                           "summary": "PASS", "error": None, "metadata": {"review_outcome": "approved"},
                           "started_at": 3000, "ended_at": 3010})
    card["status"] = "done"

    assert controller.process_reviewer_contract("b", w.repo, w.plan, w.project, conn=w.conn) == []
    assert asker.asked == []


# =============================================================================================================
# process_budget_gate and process_unpark share one decision
# =============================================================================================================

def coder_task(w):
    return w.plan.task("T1")


def test_affordable_now_is_true_for_a_role_with_no_pinned_provider(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)

    assert controller._affordable_now(w.conn, coder_task(w), {"providers": {}, "models": []}, {}) == (True, "")


def test_affordable_now_explains_a_task_that_its_own_provider_cannot_afford(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    ledger.record_usage(w.conn, "openrouter", "coder-m", n=50)

    ok, reason = controller._affordable_now(w.conn, coder_task(w), CAPPED_MODELS, {})

    assert ok is False and reason.startswith("budget: ")


def test_affordable_now_applies_the_review_reserve_to_coder_tasks_only(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    ledger.record_usage(w.conn, "openrouter", "rev-m", n=45)
    budgets = {"review_reserve_requests": 20, "daily_reserve_percent": 10}
    review_afford = usage_mod.review_budget(w.conn, MODELS, dataclasses.replace(w.project, budgets=budgets))
    assert review_afford is not None and not review_afford.can_afford

    ok, reason = controller._affordable_now(w.conn, coder_task(w), MODELS, budgets, review_afford)
    assert ok is False and reason.startswith("review budget on openrouter: ")
    assert controller._affordable_now(w.conn, coder_task(w), MODELS, budgets, None) == (True, "")

    # Only a coder task needs a review afterwards: a task of any other role with an affordable provider is not held.
    models = {**MODELS, "models": [*MODELS["models"], {"provider": "xkiro", "model": "lead-m", "role_class": "lead",
                                                       "pinned": True}]}
    lead_task = dataclasses.replace(coder_task(w), role="lead")
    assert controller._affordable_now(w.conn, lead_task, models, budgets, review_afford) == (True, "")


def park(w, key, reason, *, status="scheduled"):
    card = w.board.cards[w.work(key)]
    card["status"] = status
    w.board.add_event(w.work(key), "scheduled", {"reason": reason})


def unpark(w, models=MODELS, *, project=True, budgets=None, now=None, repo=None):
    return controller.process_unpark(
        "b", w.plan, models, conn=w.conn, budgets=w.project.budgets if budgets is None else budgets,
        project=w.project if project else None, now=now, repo=repo)


# The reason recovery parked the real stage C card S1 with (2026-09-28, verbatim from the Hermes board), and that
# park's real time: 2026-09-28T02:14:12Z.
S1_QUOTA_REASON = ("The provider's daily quota is used up. Park the card until the next UTC midnight, when the "
                   "quota resets: retrying sooner only burns requests.")
S1_PARKED_AT = 1790561652
SAME_DAY_LATE = datetime(2026, 9, 28, 23, 59, 59, tzinfo=timezone.utc)
AFTER_RESET = datetime(2026, 9, 29, 0, 0, 0, tzinfo=timezone.utc)


def quota_park(w, key, *, created_at=S1_PARKED_AT, reason=S1_QUOTA_REASON):
    card = w.board.cards[w.work(key)]
    card["status"] = "scheduled"
    card["_events"].append(
        {"kind": "scheduled", "payload": {"reason": reason}, "created_at": created_at, "run_id": None})


def test_recoverys_quota_park_reason_is_the_one_process_unpark_recognises():
    """The two halves of a quota park live in two modules: this pins them together. The wording is also the one
    already on the real board, so a card parked before round 18 is released too."""
    decision = recovery.decide(recovery.FailureKind.QUOTA, recovery.Lineage("p", "T1"), bounds.Bounds())
    assert decision.action == recovery.ACTION_PARK
    assert decision.reason == S1_QUOTA_REASON
    assert decision.reason.startswith(recovery.QUOTA_PARK_PREFIX)
    assert not decision.reason.startswith(controller._PARK_PREFIXES)


def test_a_quota_parked_card_waits_for_the_utc_reset_and_is_then_released(tmp_path, monkeypatch):
    """Round 18, found by the real stage C run: recovery's QUOTA park was never released, so S1 sat parked through
    the reset for 30 passes. Blueprint table 19.1: "Daily quota exhausted ... park the card until the reset time";
    the loop's `for card in board.parked_past_reset(): hermes.unblock(card)`."""
    w = make_world(tmp_path, monkeypatch)
    quota_park(w, "T1")

    assert unpark(w, CAPPED_MODELS, now=SAME_DAY_LATE) == []   # the provider said no today: never retry today
    assert w.board.calls_of("kanban_unblock") == [] and w.board.cards[w.work()]["status"] == "scheduled"

    assert unpark(w, CAPPED_MODELS, now=AFTER_RESET) == ["T1"]

    assert w.board.calls_of("kanban_unblock") == [
        (("b", w.work()), {"reason": "the provider's daily quota has reset"})]
    assert w.board.cards[w.work()]["status"] == "ready"
    (event,) = payloads(w.conn, "card_unparked")
    assert event == {"task_key": "T1", "card_id": w.work()}


def test_a_quota_parked_card_past_the_reset_still_waits_while_unaffordable(tmp_path, monkeypatch):
    """Past the reset the same _affordable_now still decides: a card whose provider the ledger shows spent (here,
    as if other work used the new day's requests first) is not released."""
    w = make_world(tmp_path, monkeypatch)
    monkeypatch.setattr(ledger, "default_now", lambda: AFTER_RESET)
    ledger.record_usage(w.conn, "openrouter", "coder-m", n=50)
    quota_park(w, "T1")

    assert unpark(w, CAPPED_MODELS, now=AFTER_RESET) == []
    assert w.board.calls_of("kanban_unblock") == []


def test_a_quota_park_with_no_readable_time_is_never_released(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    quota_park(w, "T1", created_at=None)
    quota_park(w, "T2", created_at="not a time")

    assert unpark(w, CAPPED_MODELS, now=AFTER_RESET) == []
    assert w.board.calls_of("kanban_unblock") == []


def test_quota_reset_passed_is_the_utc_day_boundary():
    parked = datetime(2026, 9, 28, 2, 14, 12, tzinfo=timezone.utc)
    assert recovery.quota_reset_passed(parked, SAME_DAY_LATE) is False
    assert recovery.quota_reset_passed(parked, AFTER_RESET) is True
    assert recovery.quota_reset_passed(S1_PARKED_AT, "2026-09-29T00:00:01+00:00") is True
    assert recovery.quota_reset_passed(S1_PARKED_AT, S1_PARKED_AT) is False
    # A park late in the evening is released a few minutes later, at midnight UTC: that is when the quota resets.
    assert recovery.quota_reset_passed(datetime(2026, 9, 28, 23, 58, tzinfo=timezone.utc), AFTER_RESET) is True
    assert recovery.quota_reset_passed(None, AFTER_RESET) is False
    assert recovery.quota_reset_passed(S1_PARKED_AT, "garbage") is False


# --- round 18b: the pre-unblock Gate 1 re-check for a card still assigned to the reviewer ----------------------

def _stub_check_branch(monkeypatch, result_or_exc):
    """Replace review.check_branch with a recorder that has its REAL parameter names (so a caller passing the
    wrong arguments fails loudly), returning `result_or_exc` or raising it when it is an exception. record_gate1
    itself, and _post_gate_record, run for real, so the fake board's own kanban_comment call log is genuine
    evidence of what process_unpark actually did, not a stand-in for it."""
    calls = []

    def fake(repo, branch, integration_branch, gate1_commands, touches, *, conn, task_key,
              allow_gate_config_changes=False, project_config=None, task=None, project=None):
        calls.append({
            "repo": repo, "branch": branch, "integration_branch": integration_branch,
            "gate1_commands": gate1_commands, "touches": touches, "task_key": task_key,
            "allow_gate_config_changes": allow_gate_config_changes, "project_config": project_config, "task": task,
            "project": project,
        })
        if isinstance(result_or_exc, BaseException):
            raise result_or_exc
        return result_or_exc

    monkeypatch.setattr(review_mod, "check_branch", fake)
    return calls


def test_a_reviewer_assigned_release_runs_gate1_and_posts_the_record_before_unblocking(tmp_path, monkeypatch):
    """Round 18b (the 2026-09-29 stage-C run, real Hermes): S1's work card kept the reviewer as its Kanban
    ASSIGNEE (Hermes sets that when a card goes up for review) straight through a quota park and its release,
    so the gateway dispatched the reviewer the instant kanban_unblock returned it to ready -- with no Gate 1
    re-check and no "ASES gate record" comment on the card at all, because process_review_lane only ever looks
    at status "review", and this card's status went scheduled -> ready without ever passing back through it.
    record_gate1 must now run, and its comment must post, before that unblock."""
    w = make_world(tmp_path, monkeypatch)
    quota_park(w, "T1")
    w.board.cards[w.work()]["assignee"] = "reviewer"
    calls = _stub_check_branch(monkeypatch, review_mod.BranchCheck(True, "ok", "Gate 1 green for c0ffee", "c0ffee"))

    assert unpark(w, CAPPED_MODELS, now=AFTER_RESET, repo=w.repo) == ["T1"]

    assert calls == [{
        "repo": w.repo, "branch": "swarm/T1-coder", "integration_branch": "integration",
        "gate1_commands": ["echo ok"], "touches": ["a.py"], "task_key": "T1",
        "allow_gate_config_changes": False, "project_config": w.project, "task": w.plan.task("T1"),
        "project": w.plan.project,
    }]
    names = [name for name, _, _ in w.board.calls if name in ("kanban_comment", "kanban_unblock")]
    assert names == ["kanban_comment", "kanban_unblock"]  # the gate record is posted BEFORE the release
    (comment_args, _) = w.board.calls_of("kanban_comment")[0]
    assert comment_args[1] == w.work() and review_mod.GATE_RECORD_HEADER in comment_args[2]
    assert w.board.cards[w.work()]["status"] == "ready"
    assert payloads(w.conn, "gate1_recheck_failed") == []


def test_a_coder_assigned_release_does_not_run_gate1(tmp_path, monkeypatch):
    """A card still assigned to its coder (an ordinary coder-phase quota or budget park) never went up for
    review at all: there is no Gate 1 re-check to make here and no gate record to post. This is unpark's own,
    unrelated release path, and a coder-phase park must not be mistaken for one."""
    w = make_world(tmp_path, monkeypatch)
    quota_park(w, "T1")  # default assignee from create_cards_from_plan is "coder-1" (ROLES["coder"])
    calls = _stub_check_branch(monkeypatch, review_mod.BranchCheck(True, "ok", "unused", "unused"))

    assert unpark(w, CAPPED_MODELS, now=AFTER_RESET, repo=w.repo) == ["T1"]

    assert calls == []  # check_branch (and so record_gate1) was never even called
    assert [name for name, _, _ in w.board.calls if name == "kanban_comment"] == []
    assert w.board.cards[w.work()]["status"] == "ready"


def test_an_infrastructure_error_before_release_keeps_the_card_parked_and_is_recorded_once(tmp_path, monkeypatch):
    """Mirrors process_review_lane's own handling of the same two exceptions: Gate 1 could not even run, so this
    is not a red gate and the card is not released -- it stays parked for a later pass to retry, and a second
    pass while the outage continues must not flood the events table (the same _record_once reasoning as
    tamper_check_error and process_review_lane's own sandbox_infrastructure_error)."""
    w = make_world(tmp_path, monkeypatch)
    quota_park(w, "T1")
    w.board.cards[w.work()]["assignee"] = "reviewer"
    _stub_check_branch(monkeypatch, sandbox_mod.SandboxInfrastructureError(
        "the sandbox is enabled but docker CLI not found on PATH"))

    assert unpark(w, CAPPED_MODELS, now=AFTER_RESET, repo=w.repo) == []
    assert unpark(w, CAPPED_MODELS, now=AFTER_RESET, repo=w.repo) == []  # a second pass: not flooded

    assert w.board.cards[w.work()]["status"] == "scheduled"  # never released
    assert [name for name, _, _ in w.board.calls if name == "kanban_unblock"] == []
    rows = payloads(w.conn, "sandbox_infrastructure_error")
    assert len(rows) == 1
    assert rows[0]["task_key"] == "T1" and rows[0]["gate"] == "gate1"
    assert "docker CLI not found" in rows[0]["error"]
    assert payloads(w.conn, "gate1_recheck_failed") == []


def test_a_red_gate1_is_released_anyway_and_recorded_as_gate1_recheck_failed(tmp_path, monkeypatch):
    """A red Gate 1 does not hold the card back the way it does in the review lane: there is no "send it back"
    path here, the card was never in review to begin with. It is released with the FAIL record already on it
    (never say a check passed unless a gate record shows it) and gate1_recheck_failed recorded, exactly as
    process_review_lane records it for its own red re-checks -- the merge queue re-checks its own gate_runs row
    before anything can merge, whatever the reviewer ends up doing with a red record in front of it."""
    w = make_world(tmp_path, monkeypatch)
    quota_park(w, "T1")
    w.board.cards[w.work()]["assignee"] = "reviewer"
    _stub_check_branch(monkeypatch, review_mod.BranchCheck(
        False, "gate1_red", "Gate 1 failed on the controller's re-check:\nexit 1", "c0ffee"))

    assert unpark(w, CAPPED_MODELS, now=AFTER_RESET, repo=w.repo) == ["T1"]  # still released

    assert w.board.cards[w.work()]["status"] == "ready"
    assert [name for name, _, _ in w.board.calls if name == "kanban_comment"] != []
    assert payloads(w.conn, "gate1_recheck_failed") == [{"task_key": "T1"}]


def test_a_tamper_check_that_could_not_run_is_not_a_failed_recheck(tmp_path, monkeypatch):
    """As review.gate_before_review: a tamper_check_error (git could not produce the diff) says nothing about the
    card, so the release records tamper_check_error, never gate1_recheck_failed (round 18b review finding)."""
    w = make_world(tmp_path, monkeypatch)
    quota_park(w, "T1")
    w.board.cards[w.work()]["assignee"] = "reviewer"
    _stub_check_branch(monkeypatch, review_mod.BranchCheck(
        False, "tamper_check_error", "git diff failed: exit 128", "c0ffee"))

    assert unpark(w, CAPPED_MODELS, now=AFTER_RESET, repo=w.repo) == ["T1"]

    assert payloads(w.conn, "gate1_recheck_failed") == []
    (row,) = payloads(w.conn, "tamper_check_error")
    assert row == {"task_key": "T1", "card_id": w.work(), "head": "c0ffee", "reason": "git diff failed: exit 128"}


def test_process_unpark_with_no_repo_keeps_todays_behaviour(tmp_path, monkeypatch):
    """`repo` is optional and None by default: an existing caller (or one with no repo to give) releases exactly
    as it did before this round, with no Gate 1 re-check attempted even for a card assigned to the reviewer."""
    w = make_world(tmp_path, monkeypatch)
    quota_park(w, "T1")
    w.board.cards[w.work()]["assignee"] = "reviewer"
    calls = _stub_check_branch(monkeypatch, review_mod.BranchCheck(True, "ok", "unused", "unused"))

    assert unpark(w, CAPPED_MODELS, now=AFTER_RESET) == ["T1"]  # repo defaults to None

    assert calls == []
    assert [name for name, _, _ in w.board.calls if name == "kanban_comment"] == []


def test_a_budget_parked_card_is_unparked_once_it_is_affordable_again(tmp_path, monkeypatch):
    """ASES-CAP-03 / Table 17 "Park cards until the reset": the gate parks, and nothing used to wake the card."""
    w = make_world(tmp_path, monkeypatch)
    park(w, "T1", "budget: openrouter has 0 of 50 requests left today")

    assert unpark(w, CAPPED_MODELS) == ["T1"]

    assert w.board.calls_of("kanban_unblock") == [(("b", w.work()), {"reason": "budget available again"})]
    assert w.board.cards[w.work()]["status"] == "ready"
    (event,) = payloads(w.conn, "card_unparked")
    assert event == {"task_key": "T1", "card_id": w.work()}


def test_a_card_stays_parked_while_it_is_still_unaffordable(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    ledger.record_usage(w.conn, "openrouter", "coder-m", n=50)
    park(w, "T1", "budget: exhausted")

    assert unpark(w, CAPPED_MODELS) == []

    assert w.board.calls_of("kanban_unblock") == [] and w.board.cards[w.work()]["status"] == "scheduled"


def test_a_card_someone_else_scheduled_is_never_touched(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    park(w, "T1", "waiting for the design review on Friday")
    park(w, "T2", "Budget: capitalised is not our reason either")

    assert unpark(w, CAPPED_MODELS) == []

    assert w.board.calls_of("kanban_unblock") == []


def test_a_scheduled_card_with_no_scheduled_event_is_not_ours(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    w.board.cards[w.work()]["status"] = "scheduled"

    assert unpark(w, CAPPED_MODELS) == []


def test_the_latest_scheduled_event_decides_who_parked_the_card(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    park(w, "T1", "budget: exhausted")               # parked by the controller ...
    w.board.add_event(w.work("T1"), "scheduled", {"reason": "a person rescheduled it"})   # ... then taken over
    park(w, "T2", "someone else's reason")
    w.board.add_event(w.work("T2"), "scheduled", {"reason": "budget: exhausted"})          # taken over the other way

    assert unpark(w, CAPPED_MODELS) == ["T2"]


def test_the_scheduled_reason_is_read_from_a_json_string_payload_too(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    w.board.cards[w.work()]["status"] = "scheduled"
    w.board.cards[w.work()]["_events"].append(
        {"kind": "scheduled", "payload": json.dumps({"reason": "review budget on openrouter: low"}),
         "created_at": "1789832318", "run_id": None})

    assert unpark(w, MODELS, project=False) == ["T1"]


def test_a_review_reserve_park_lasts_until_the_reviewer_provider_can_afford_the_review(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch, budgets={"review_reserve_requests": 20, "daily_reserve_percent": 10})
    ledger.record_usage(w.conn, "openrouter", "rev-m", n=45)
    park(w, "T1", "review budget on openrouter: only 5 left")

    assert unpark(w, MODELS) == []                      # the coder's own provider is fine, the reviewer's is not
    assert unpark(w, MODELS, project=False) == ["T1"]   # without the project there is no review reserve to check


def test_unpark_only_looks_at_this_plans_cards(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    foreign = w.board.add("foreign_card", status="scheduled")
    w.board.add_event("foreign_card", "scheduled", {"reason": "budget: exhausted"})
    w.conn.execute(
        "INSERT INTO plan_tasks (project, task_key, work_card_id, merge_card_id, role, created_at) "
        "VALUES ('other', 'T1', 'foreign_card', 'foreign_merge', 'coder', 'now')")

    assert unpark(w, CAPPED_MODELS) == []

    assert foreign["status"] == "scheduled" and w.board.calls_of("kanban_unblock") == []


def test_one_card_that_cannot_be_unparked_does_not_stop_the_others(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    park(w, "T1", "budget: exhausted")
    park(w, "T2", "budget: exhausted")
    w.board.fail_after["kanban_unblock"] = (0, hermes.HermesCommandError(["kanban", "unblock"], 1, "locked"))
    w.board.hooks["kanban_unblock"] = lambda board, cid, reason=None: (
        w.board.fail_after.pop("kanban_unblock") if cid == w.work("T2") else None)

    assert unpark(w, CAPPED_MODELS) == ["T2"]

    (error_event,) = payloads(w.conn, "unpark_error")
    assert "locked" in error_event["error"]
    assert error_event["project"] == w.plan.project  # ASES-OBS-01: not a cross-project leak


def test_the_gate_and_unpark_agree_about_the_same_card(tmp_path, monkeypatch):
    """One rule on both sides: whatever the gate parks with the ledger in a state, unpark leaves parked in that
    state, and releases in the state that lets the gate leave the card alone."""
    w = make_world(tmp_path, monkeypatch)
    w.board.cards[w.work()]["status"] = "ready"
    ledger.record_usage(w.conn, "openrouter", "coder-m", n=50)

    assert controller.process_budget_gate("b", w.plan, CAPPED_MODELS, conn=w.conn, budgets={}) == ["T1"]
    assert unpark(w, CAPPED_MODELS) == []                     # still exhausted: it stays exactly where the gate put it
    assert w.board.cards[w.work()]["status"] == "scheduled"

    w.conn.execute("DELETE FROM requests_ledger")             # the provider's day rolled over
    assert unpark(w, CAPPED_MODELS) == ["T1"]
    assert controller.process_budget_gate("b", w.plan, CAPPED_MODELS, conn=w.conn, budgets={}) == []


# =============================================================================================================
# _affordable_now, process_budget_gate and process_unpark: the data-class check (round 7, ASES-PRV-01/03, bug 2)
# =============================================================================================================

# xkiro's declared policy is not in policy._SAFE_FOR_PRIVATE (the same real fact test_recovery.py's MODELS
# uses), so a coder pinned to it fails check_data_class for data_class="private" every time.
UNSAFE_FOR_PRIVATE_MODELS = {
    "providers": {"xkiro": {"data_policy": "router_ztr_upstream_varies"}},
    "models": [{"provider": "xkiro", "model": "coder-m", "role_class": "coder", "pinned": True}],
}


def test_affordable_now_parks_for_a_data_class_violation_when_project_is_given(tmp_path, monkeypatch):
    """ASES-PRV-01, confirmed empirically by round 6 (ASES-PRV-01 finding): before this fix,
    process_budget_gate's per-pass check never looked at data_class at all, only budget."""
    w = make_world(tmp_path, monkeypatch)
    private_project = dataclasses.replace(w.project, data_class="private")

    ok, reason = controller._affordable_now(
        w.conn, coder_task(w), UNSAFE_FOR_PRIVATE_MODELS, {}, None, private_project)

    assert ok is False
    assert reason.startswith("data class: ")
    assert "xkiro" in reason and "private" in reason


def test_affordable_now_skips_the_data_class_check_when_project_is_not_given(tmp_path, monkeypatch):
    """Backward compatible: `project` is a new, optional parameter, and every caller that has not been
    updated to pass one (there is none left in this codebase, but a future one is possible) keeps the old
    behaviour exactly, rather than refusing every card because it cannot resolve a data_class."""
    w = make_world(tmp_path, monkeypatch)
    assert controller._affordable_now(w.conn, coder_task(w), UNSAFE_FOR_PRIVATE_MODELS, {}) == (True, "")


def test_affordable_now_checks_the_data_class_before_the_budget(tmp_path, monkeypatch):
    """ASES-PRV-01: "enforced before any other routing rule", the same order Gate P's own cli.cmd_approve
    check follows. A task that is BOTH unaffordable and data-class-unsafe is parked for the data-class
    reason, not the budget one: the budget half is never even reached, let alone recorded."""
    w = make_world(tmp_path, monkeypatch)
    ledger.record_usage(w.conn, "xkiro", "coder-m", n=10_000)   # would also fail an ordinary budget check
    private_project = dataclasses.replace(w.project, data_class="private")

    ok, reason = controller._affordable_now(
        w.conn, coder_task(w), UNSAFE_FOR_PRIVATE_MODELS, {}, None, private_project)

    assert ok is False and reason.startswith("data class: ")


def test_process_budget_gate_parks_a_card_whose_provider_fails_the_data_class(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    w.board.cards[w.work()]["status"] = "ready"
    private_project = dataclasses.replace(w.project, data_class="private")

    parked = controller.process_budget_gate(
        "b", w.plan, UNSAFE_FOR_PRIVATE_MODELS, conn=w.conn, budgets={}, project=private_project)

    assert parked == ["T1"]
    assert w.board.cards[w.work()]["status"] == "scheduled"
    (event,) = payloads(w.conn, "card_parked_for_budget")
    assert event["task_key"] == "T1" and event["reason"].startswith("data class: ")


def test_a_data_class_parked_card_is_never_unparked_even_once_it_would_be_affordable(tmp_path, monkeypatch):
    """ASES-PRV-03 ("The controller MUST NOT relax the class to keep work flowing"): a "data class:" reason
    is deliberately NOT one of controller._PARK_PREFIXES, so process_unpark's own prefix filter skips it
    before ever asking _affordable_now again, the same way it already skips a card someone else scheduled.
    MODELS here is an ordinary, affordable, data-class-irrelevant model set: if this card were EVER going to
    be picked back up automatically, this is exactly the models argument that would do it."""
    w = make_world(tmp_path, monkeypatch)
    park(w, "T1", "data class: provider 'xkiro' (data_policy='router_ztr_upstream_varies') is not confirmed "
                  "safe for data_class=private; needs one of ['local_only', 'no_training', 'zero_data_retention'], "
                  "or use a local model")

    assert unpark(w, MODELS) == []
    assert w.board.cards[w.work()]["status"] == "scheduled"


def test_unpark_does_not_resume_a_budget_park_that_has_since_become_data_class_unsafe(tmp_path, monkeypatch):
    """Belt and braces (process_unpark's own docstring): a card parked for an ordinary budget reason still
    passes the _PARK_PREFIXES filter, but if the project's data_class or the provider's policy changed
    underneath it in the meantime, _affordable_now (given the same `project` process_unpark now threads
    through) refuses it on the SECOND check too, so it is correctly left parked either way."""
    w = make_world(tmp_path, monkeypatch)
    park(w, "T1", "budget: exhausted")
    private_project = dataclasses.replace(w.project, data_class="private")

    assert controller.process_unpark(
        "b", w.plan, UNSAFE_FOR_PRIVATE_MODELS, conn=w.conn, budgets=w.project.budgets, project=private_project,
    ) == []
    assert w.board.cards[w.work()]["status"] == "scheduled"


def test_affordable_now_parks_a_committing_task_when_only_the_reviewer_is_data_class_unsafe(tmp_path, monkeypatch):
    """STOPDOC.md/PROVIDERS.md item 2 (round 19, package STOPGATES): a coder's own provider can be perfectly
    safe while the REVIEWER its finished work will go to next is not -- _affordable_now must catch that too,
    since every committing task ends up reviewed (ASES-PRV-01, 'enforced before any other routing rule')."""
    w = make_world(tmp_path, monkeypatch)
    private_project = dataclasses.replace(w.project, data_class="private")
    models = {
        "providers": {
            "xkiro": {"data_policy": "no_training", "data_policy_verified_at": "2026-09-01"},
            "openrouter": {"data_policy": "some_free_endpoints_train"},
        },
        "models": [
            {"provider": "xkiro", "model": "coder-m", "role_class": "coder", "pinned": True},
            {"provider": "openrouter", "model": "rev-m", "role_class": "reviewer", "pinned": True},
        ],
    }

    ok, reason = controller._affordable_now(w.conn, coder_task(w), models, {}, None, private_project)

    assert ok is False
    assert reason.startswith("data class: ") and "openrouter" in reason


def test_affordable_now_refuses_a_paid_model_unless_allow_paid_models_is_set(tmp_path, monkeypatch):
    """STOPDOC.md item 8 (ASES-DOC-04, stop condition category 1 'spends money'): a paid models.yaml row must
    be refused here exactly like an unaffordable or data-class-unsafe one, unless the project's own
    swarm.yaml opts in with budgets.allow_paid_models: true."""
    w = make_world(tmp_path, monkeypatch)
    models = {
        "providers": MODELS["providers"],
        "models": [{"provider": "xkiro", "model": "coder-m", "role_class": "coder", "pinned": True, "paid": True}],
    }

    ok, reason = controller._affordable_now(w.conn, coder_task(w), models, {})
    assert ok is False and reason.startswith("paid model: ") and "coder-m" in reason

    ok, reason = controller._affordable_now(w.conn, coder_task(w), models, {"allow_paid_models": True})
    assert ok is True


# =============================================================================================================
# process_bounds and pause_and_report
# =============================================================================================================

def test_process_bounds_does_nothing_while_no_bound_that_stops_a_project_is_breached(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    # a task-level breach escalates one task, it does not stop the project
    recovery.bump(w.conn, "t3", "T1", "capability_failures", 9)

    assert controller.process_bounds("b", w.repo, w.plan, w.project, MODELS, conn=w.conn) == (False, None)

    assert w.board.calls_of("pause") == [] and bounds.get_state(w.conn, "t3") is None


def test_a_spent_project_replan_budget_pauses_the_project_and_writes_the_report(tmp_path, monkeypatch):
    """Table 17: "Re-plans per project 2: User decision", ASES-CTL-01: "It is stopped, not finished, when any global
    bound is reached"."""
    w = make_world(tmp_path, monkeypatch)
    bounds.add_replan(w.conn, "t3")
    bounds.add_replan(w.conn, "t3")

    stopped, reason = controller.process_bounds("b", w.repo, w.plan, w.project, MODELS, conn=w.conn)

    assert stopped is True
    assert reason == "replans_per_project reached for project (2 of 2): User decision"
    assert w.board.calls_of("pause") == [((reason,), {})]
    state = bounds.get_state(w.conn, "t3")
    assert state["status"] == "paused"
    assert controller._halted(w.conn, "t3") == (True, reason)
    (bound_event,) = payloads(w.conn, "bounds_reached")
    assert bound_event["bounds"] == [{"name": "replans_per_project", "subject": "project", "used": 2, "limit": 2}]
    (paused_event,) = payloads(w.conn, "project_paused")
    report_dir = pathlib.Path(paused_event["report_dir"])
    assert report_dir.parent == tmp_path / "home" / "reports" / "t3" and report_dir.name.endswith("-paused")
    assert (report_dir / "report.html").is_file() and (report_dir / "report.json").is_file()
    assert json.loads((report_dir / "report.json").read_text(encoding="utf-8"))["project"]


def test_the_project_wall_clock_pauses_with_the_injected_clock(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch, budgets={"project_wall_clock_minutes": 60})
    bounds.start_project(w.conn, "t3", now=datetime(2026, 9, 21, 8, 0, tzinfo=timezone.utc))

    assert controller.process_bounds("b", w.repo, w.plan, w.project, MODELS, conn=w.conn,
                                     now=datetime(2026, 9, 21, 8, 30, tzinfo=timezone.utc)) == (False, None)
    stopped, reason = controller.process_bounds(
        "b", w.repo, w.plan, w.project, MODELS, conn=w.conn, now=datetime(2026, 9, 21, 9, 30, tzinfo=timezone.utc))

    assert stopped and reason == "project_wall_clock_minutes reached for project (90 of 60): Pause and report"


def test_process_bounds_accepts_epoch_seconds_and_iso_text_for_now(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    seen = []
    monkeypatch.setattr(bounds, "evaluate_bounds", lambda board, plan, limits, models, *, conn, now=None: (
        seen.append((board, plan, limits, models, now)) or []))
    moment = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)

    controller.process_bounds("b", w.repo, w.plan, w.project, MODELS, conn=w.conn, now=moment.timestamp())
    controller.process_bounds("b", w.repo, w.plan, w.project, MODELS, conn=w.conn, now="2026-09-21T12:00:00Z")
    controller.process_bounds("b", w.repo, w.plan, w.project, MODELS, conn=w.conn)

    assert [s[4] for s in seen] == [moment, moment, None]
    assert seen[0][:4] == ("b", w.plan, bounds.Bounds.from_budgets(w.project.budgets), MODELS)


def test_process_bounds_rejects_a_malformed_budgets_block_by_name(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch, budgets={"attempts_per_card": True})

    with pytest.raises(ValueError, match="attempts_per_card"):
        controller.process_bounds("b", w.repo, w.plan, w.project, MODELS, conn=w.conn)


def test_pause_and_report_pauses_then_marks_the_state_then_writes_the_report(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    order = []
    w.board.hooks["pause"] = lambda reason: order.append(("pause", reason))
    real_set_status = bounds.set_status
    monkeypatch.setattr(bounds, "set_status", lambda conn, project, status, reason=None, **kw: (
        order.append(("set_status", project, status, reason)), real_set_status(conn, project, status, reason, **kw))[1])
    monkeypatch.setattr(report, "build_report", lambda *a, **kw: order.append(("build",)) or {"project": {}})
    monkeypatch.setattr(
        report, "write_report", lambda data, directory: order.append(("write", pathlib.Path(directory))))
    why = "the wall clock ran out"

    directory = controller.pause_and_report("b", w.repo, w.plan, w.project, MODELS, why, conn=w.conn)

    assert [step[0] for step in order] == ["pause", "set_status", "build", "write"]
    assert order[0] == ("pause", why) and order[1] == ("set_status", "t3", "paused", why)
    assert order[3][1] == pathlib.Path(directory)
    assert pathlib.Path(directory).parent == tmp_path / "home" / "reports" / "t3"
    assert pathlib.Path(directory).name.endswith("Z-paused")
    (event,) = payloads(w.conn, "project_paused")
    assert event["reason"] == "the wall clock ran out" and event["report_dir"] == directory


def test_pause_and_report_passes_an_ascii_redacted_reason_to_hermes(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    reason = f"gate 4 failed on caf{chr(0xE9)}\nkey {SECRET}"

    controller.pause_and_report("b", w.repo, w.plan, w.project, MODELS, reason, conn=w.conn)

    (passed,), _ = w.board.calls_of("pause")[0]
    assert SECRET not in passed and "\n" not in passed and all(ord(ch) < 128 for ch in passed)
    assert "caf\\xe9" in passed and len(passed) < 500


@pytest.mark.parametrize("broken, expected_event", [
    ("pause", "pause_error"), ("set_status", "pause_state_error"),
    ("build_report", "pause_report_error"), ("write_report", "pause_report_error"),
])
def test_every_step_of_pause_and_report_is_guarded_on_its_own(tmp_path, monkeypatch, broken, expected_event):
    """It runs exactly when things are going wrong, so a failing step is an event, never an exception, and the steps
    that can still run do."""
    w = make_world(tmp_path, monkeypatch)
    boom = RuntimeError(f"{broken} exploded")
    ran = []

    def guarded(name, real):
        """`real`, except that it raises when it is the step under test (and records that it ran otherwise)."""
        def call(*args, **kwargs):
            if broken == name:
                raise boom
            ran.append(name)
            return real(*args, **kwargs)
        return call

    if broken == "pause":
        w.board.fail["pause"] = boom
    monkeypatch.setattr(bounds, "set_status", guarded("set_status", bounds.set_status))
    monkeypatch.setattr(report, "build_report", guarded("build_report", report.build_report))
    monkeypatch.setattr(report, "write_report", guarded("write_report", report.write_report))

    directory = controller.pause_and_report("b", w.repo, w.plan, w.project, MODELS, "why", conn=w.conn)

    assert directory.endswith("-paused")
    assert f"{broken} exploded" in payloads(w.conn, expected_event)[0]["error"]
    paused = (bounds.get_state(w.conn, "t3") or {}).get("status") == "paused"
    assert paused == (broken != "set_status")                     # the pause is not lost to the failing report
    assert "project_paused" in kinds(w.conn)
    if broken in ("pause", "set_status"):
        assert ran[-2:] == ["build_report", "write_report"]       # the report is still written


def test_pause_and_report_survives_a_project_without_a_usable_report_directory(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    project = types.SimpleNamespace(name="t3", budgets={})               # no ases_home at all

    assert controller.pause_and_report("b", w.repo, w.plan, project, MODELS, "why", conn=w.conn) == ""

    assert bounds.get_state(w.conn, "t3")["status"] == "paused" and w.board.calls_of("pause") != []
    assert "pause_report_error" in kinds(w.conn)


def test_the_report_directory_is_never_inside_the_repository_and_never_reused(tmp_path):
    project = types.SimpleNamespace(name="my project/v2", ases_home=tmp_path / "home")

    first = controller._report_directory(project, "20260921T100000Z")
    first.mkdir(parents=True)
    second = controller._report_directory(project, "20260921T100000Z")

    assert first == tmp_path / "home" / "reports" / "my_project_v2" / "20260921T100000Z-paused"
    assert second.name == "20260921T100000Z-paused-2"


# =============================================================================================================
# provisioning, idle worktrees
# =============================================================================================================

def test_provisioning_gives_running_cards_their_env_and_sweeps_with_blocked_cards_kept_live(tmp_path, monkeypatch):
    """ASES-GIT-14: a blocked card waits for an answer and resumes with the same ports, so its lease is kept."""
    w = make_world(tmp_path, monkeypatch)
    from ases import leases
    seen = []
    monkeypatch.setattr(leases, "provision_running_cards", lambda board, conn, plan: (
        seen.append(("provision", board, conn, plan)) or ["n1"]))
    monkeypatch.setattr(leases, "sweep_finished", lambda board, conn, plan, *, live_statuses: (
        seen.append(("sweep", board, conn, plan, tuple(live_statuses))) or []))

    assert controller.process_provision("b", w.plan, conn=w.conn) == ["n1"]

    assert seen[0] == ("provision", "b", w.conn, w.plan)
    assert seen[1][:4] == ("sweep", "b", w.conn, w.plan)
    assert set(seen[1][4]) == {"running", "ready", "review", "scheduled", "todo", "blocked"}


def test_a_failing_sweep_does_not_lose_the_cards_that_were_provisioned(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    from ases import leases
    monkeypatch.setattr(leases, "provision_running_cards", lambda *a, **kw: ["n1", "n3"])
    monkeypatch.setattr(leases, "sweep_finished", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db locked")))

    assert controller.process_provision("b", w.plan, conn=w.conn) == ["n1", "n3"]

    assert "db locked" in payloads(w.conn, "lease_sweep_error")[0]["error"]


def test_provisioning_also_sweeps_orphan_containers_when_a_project_is_given(tmp_path, monkeypatch):
    """CONTAINERS (round 17), p353's per-pass half: process_provision runs the orphan container sweep too,
    when it is given a project (run_pass always passes one). The sweep's own logic (which containers, which
    profiles) is tested in test_containers.py; this is wiring only."""
    w = make_world(tmp_path, monkeypatch)
    seen = []
    monkeypatch.setattr(
        controller.containers_mod, "sweep_orphan_containers",
        lambda board, project, conn: seen.append((board, project, conn)),
    )

    controller.process_provision("b", w.plan, w.project, conn=w.conn)

    assert seen == [("b", w.project, w.conn)]


def test_a_failing_container_sweep_does_not_lose_the_cards_that_were_provisioned(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    from ases import leases
    monkeypatch.setattr(leases, "provision_running_cards", lambda *a, **kw: ["n1"])
    monkeypatch.setattr(
        controller.containers_mod, "sweep_orphan_containers",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("docker exploded")),
    )

    assert controller.process_provision("b", w.plan, w.project, conn=w.conn) == ["n1"]

    assert payloads(w.conn, "pass_step_error")[0]["step"] == "container_sweep"


def test_provisioning_without_a_project_never_sweeps_containers(tmp_path, monkeypatch):
    """A caller that does not pass `project` (every call above, and any build older than round 17) keeps the
    sweep off entirely: process_provision must not even look at containers.sweep_orphan_containers."""
    w = make_world(tmp_path, monkeypatch)
    monkeypatch.setattr(
        controller.containers_mod, "sweep_orphan_containers",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not be called without a project")),
    )

    assert controller.process_provision("b", w.plan, conn=w.conn) == []


def test_provisioning_writes_the_env_file_into_a_running_cards_real_worktree(tmp_path, monkeypatch):
    repo = git_repo(tmp_path)
    w = make_world(tmp_path, monkeypatch)
    worktree = tmp_path / "wt-t1"
    git("worktree", "add", "-q", "-b", "wt-t1-branch", str(worktree), "integration", cwd=repo)
    card = w.board.cards[w.work()]
    card.update(status="running", workspace_path=str(worktree))

    assert controller.process_provision("b", w.plan, conn=w.conn) == [w.work()]

    assert (worktree / ".env.ases").is_file()
    assert controller.process_provision("b", w.plan, conn=w.conn) == []       # already provisioned
    card["status"] = "done"                                                   # a finished card's lease is released
    controller.process_provision("b", w.plan, conn=w.conn)
    assert w.conn.execute("SELECT COUNT(*) AS n FROM resource_leases WHERE released_at IS NULL").fetchone()["n"] == 0


def test_idle_worktrees_are_checked_against_every_running_cards_workspace(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    w.board.cards[w.work("T1")].update(status="running", workspace_path="/ws/one")
    w.board.cards[w.work("T2")].update(status="running", workspace_path=None)        # a scratch card: no worktree
    w.board.add("other_project_card", status="running", workspace_path="/ws/two")    # another project's card counts too
    w.board.add("waiting", status="ready", workspace_path="/ws/three")
    seen = []
    problem = "worktree /ws/x changed while no card was running in it: status changed"
    monkeypatch.setattr(guards, "check_idle_worktrees", lambda conn, project, repo, paths, **kw: (
        seen.append((conn, project, repo, list(paths))) or [problem]))

    problems = controller.process_idle_worktrees("b", w.repo, w.plan, conn=w.conn)

    assert seen == [(w.conn, "t3", w.repo, ["/ws/one", "/ws/two"])]
    assert problems == [problem]
    (event,) = payloads(w.conn, "idle_worktree_changed")
    assert event["project"] == "t3" and "changed while no card was running" in event["problem"]


def test_idle_worktree_problems_are_warnings_and_never_raise(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    monkeypatch.setattr(guards, "check_idle_worktrees", lambda *a, **kw: [f"worktree caf{chr(0xE9)} changed"])

    problems = controller.process_idle_worktrees("b", w.repo, w.plan, conn=w.conn)

    assert problems == [f"worktree caf{chr(0xE9)} changed"]
    assert all(ord(ch) < 128 for ch in payloads(w.conn, "idle_worktree_changed")[0]["problem"])


def test_a_worktree_that_changes_while_no_card_runs_is_reported_once_against_a_real_repository(tmp_path, monkeypatch):
    repo = git_repo(tmp_path)
    w = make_world(tmp_path, monkeypatch)
    worktree = tmp_path / "wt-idle"
    git("worktree", "add", "-q", "-b", "wt-idle-branch", str(worktree), "integration", cwd=repo)

    assert controller.process_idle_worktrees("b", repo, w.plan, conn=w.conn) == []       # first sight: a baseline
    (worktree / "stray.txt").write_text("someone wrote here\n", encoding="utf-8")
    problems = controller.process_idle_worktrees("b", repo, w.plan, conn=w.conn)
    assert len(problems) == 1 and "changed while no card was running in it" in problems[0]
    assert controller.process_idle_worktrees("b", repo, w.plan, conn=w.conn) == []       # reported once

    w.board.add("running_here", status="running", workspace_path=str(worktree))         # its own worker may change it
    (worktree / "more.txt").write_text("the worker's own change\n", encoding="utf-8")
    assert controller.process_idle_worktrees("b", repo, w.plan, conn=w.conn) == []


# =============================================================================================================
# process_card_base_checks (round 10, package BASECHECK; ASES-GIT-01, ASES-GIT-16)
# =============================================================================================================


def _plant_branch(repo, branch, base_of="integration"):
    """A branch named `branch` cut from a NEW commit off `base_of`, itself never adopted by ASES -- the way a
    remote-tip sync would plant one. Returns the planted commit's SHA."""
    git("checkout", "-q", "-b", "planted-tmp", base_of, cwd=repo)
    (repo / "planted.txt").write_text("a commit ASES never wrote\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "a commit ASES never wrote", cwd=repo)
    planted = git("rev-parse", "HEAD", cwd=repo).strip()
    git("checkout", "-q", base_of, cwd=repo)
    git("branch", branch, planted, cwd=repo)
    git("branch", "-D", "planted-tmp", cwd=repo)
    return planted


def test_process_card_base_checks_skips_everything_when_no_head_has_been_written_yet(tmp_path, monkeypatch):
    repo = git_repo(tmp_path)  # swarm/T1-coder already exists, cut from integration's own current tip
    w = make_world(tmp_path, monkeypatch)
    w.board.cards[w.work("T1")].update(status="running", branch_name="swarm/T1-coder")

    assert controller.process_card_base_checks("b", repo, w.plan, conn=w.conn) == []

    assert w.board.cards[w.work("T1")]["status"] == "running"  # not blocked: nothing to compare a base against yet
    assert not any(kind.startswith("card_base_") for kind in kinds(w.conn))


def test_process_card_base_checks_verifies_a_card_cut_from_the_adopted_head_and_remembers_it(tmp_path, monkeypatch):
    repo = git_repo(tmp_path)
    w = make_world(tmp_path, monkeypatch)
    guards.adopt_current_head(w.conn, "t3", repo)
    w.board.cards[w.work("T1")].update(status="running", branch_name="swarm/T1-coder")

    problems = controller.process_card_base_checks("b", repo, w.plan, conn=w.conn)

    assert problems == []
    assert w.board.cards[w.work("T1")]["status"] == "running"
    (payload,) = payloads(w.conn, "card_base_verified")
    assert payload["card_id"] == w.work("T1") and payload["task_key"] == "T1" and payload["branch"] == "swarm/T1-coder"

    calls = []
    monkeypatch.setattr(guards, "check_card_base", lambda *a, **kw: calls.append(1))
    assert controller.process_card_base_checks("b", repo, w.plan, conn=w.conn) == []
    assert calls == []  # memoized: no second git-reading check at all
    assert len(payloads(w.conn, "card_base_verified")) == 1  # still just the one event


def test_process_card_base_checks_blocks_a_card_planted_from_a_commit_ases_never_wrote(tmp_path, monkeypatch):
    repo = git_repo(tmp_path, with_branch=False)
    w = make_world(tmp_path, monkeypatch)
    guards.adopt_current_head(w.conn, "t3", repo)
    planted = _plant_branch(repo, "swarm/T1-coder")
    w.board.cards[w.work("T1")].update(status="running", branch_name="swarm/T1-coder")

    problems = controller.process_card_base_checks("b", repo, w.plan, conn=w.conn)

    assert len(problems) == 1 and "ASES-GIT-01" in problems[0] and "T1" in problems[0]
    card = w.board.cards[w.work("T1")]
    assert card["status"] == "blocked"
    question = questions.open_question(card)
    assert question is not None
    assert "ASES-GIT-01" in question.reason and "swarm/T1-coder" in question.reason
    (payload,) = payloads(w.conn, "card_base_violation")
    assert payload["task_key"] == "T1" and payload["branch"] == "swarm/T1-coder" and payload["base"] == planted
    assert payload["expected"]  # the allowed set was included
    assert not payloads(w.conn, "card_base_verified")


def test_process_card_base_checks_skips_a_card_whose_branch_does_not_exist_yet(tmp_path, monkeypatch):
    """Finding 3 (round 12, RUNSTART, ASES-GIT-01/GIT-16): Hermes's own claim_task commits status='running' before
    its LATER `git worktree add -b` subprocess actually creates the branch and its reflog. A controller pass that
    lands in that window must not yank an otherwise-legitimate card into `blocked` over a branch that simply does
    not exist YET; it must be skipped, so the very next pass rechecks it. The merge queue's own `_check_base`
    stays the backstop that keeps a genuinely bad base from ever landing.

    Round 14 (RUNSTART2, finding A2-1): the skip is no longer unrecorded -- one card_base_branch_missing_pass
    event is written per consecutive miss, so a branch that never resolves can eventually be escalated (see
    test_process_card_base_checks_escalates_visibly_after_a_bounded_number_of_misses_but_never_blocks below) -- but
    a single miss, well under the grace bound, still never blocks or violates the card."""
    repo = git_repo(tmp_path, with_branch=False)  # swarm/T1-coder has not been created at all yet
    w = make_world(tmp_path, monkeypatch)
    guards.adopt_current_head(w.conn, "t3", repo)
    w.board.cards[w.work("T1")].update(status="running", branch_name="swarm/T1-coder")

    problems = controller.process_card_base_checks("b", repo, w.plan, conn=w.conn)

    assert problems == []
    assert w.board.cards[w.work("T1")]["status"] == "running"  # not yanked into blocked over a timing artifact
    assert not any(kind.startswith("card_base_violation") or kind.startswith("card_base_branch_unverifiable")
                   for kind in kinds(w.conn))
    (miss,) = payloads(w.conn, "card_base_branch_missing_pass")
    assert miss["task_key"] == "T1" and miss["branch"] == "swarm/T1-coder"

    # Hermes's worktree-add subprocess catches up: the branch now exists, cut from the adopted head.
    git("branch", "swarm/T1-coder", "integration", cwd=repo)
    problems = controller.process_card_base_checks("b", repo, w.plan, conn=w.conn)
    assert problems == []
    (payload,) = payloads(w.conn, "card_base_verified")
    assert payload["branch"] == "swarm/T1-coder"
    assert len(payloads(w.conn, "card_base_branch_missing_pass")) == 1  # the one earlier miss, none added once found


def test_process_card_base_checks_escalates_visibly_after_a_bounded_number_of_misses_but_never_blocks(
    tmp_path, monkeypatch,
):
    """Finding A2-1 (round 14, RUNSTART2, ASES-GIT-01/GIT-16): guards.branch_exists cannot tell "not yet created"
    from "created, then the ref vanished" (an out-of-band deletion), so the skip above must not stay silent
    forever. Past controller._MISSING_BRANCH_GRACE_PASSES consecutive misses, one card_base_branch_unverifiable
    event names the card -- visible in the events log -- but the card is never blocked by this alone: the
    merge-queue's own `_check_base` stays the enforcement backstop, exactly as it already is for the ordinary
    not-yet-created case this grace exists for."""
    repo = git_repo(tmp_path, with_branch=False)  # swarm/T1-coder is never created at all
    w = make_world(tmp_path, monkeypatch)
    guards.adopt_current_head(w.conn, "t3", repo)
    w.board.cards[w.work("T1")].update(status="running", branch_name="swarm/T1-coder")

    for n in range(1, controller._MISSING_BRANCH_GRACE_PASSES):
        problems = controller.process_card_base_checks("b", repo, w.plan, conn=w.conn)
        assert problems == []
        assert not payloads(w.conn, "card_base_branch_unverifiable")  # not escalated yet
        assert len(payloads(w.conn, "card_base_branch_missing_pass")) == n

    problems = controller.process_card_base_checks("b", repo, w.plan, conn=w.conn)  # the bound-th consecutive miss

    assert problems == []
    assert w.board.cards[w.work("T1")]["status"] == "running"  # visibility only, never a block
    (escalation,) = payloads(w.conn, "card_base_branch_unverifiable")
    assert escalation["task_key"] == "T1" and escalation["branch"] == "swarm/T1-coder"
    assert escalation["passes_missing"] == controller._MISSING_BRANCH_GRACE_PASSES

    # Further misses neither pile up a second escalation nor keep growing the miss-pass log forever.
    controller.process_card_base_checks("b", repo, w.plan, conn=w.conn)
    assert len(payloads(w.conn, "card_base_branch_unverifiable")) == 1
    assert len(payloads(w.conn, "card_base_branch_missing_pass")) == controller._MISSING_BRANCH_GRACE_PASSES


def test_process_card_base_checks_still_blocks_a_branch_that_exists_with_a_bad_base(tmp_path, monkeypatch):
    """The other half of finding 3: a branch that DOES exist but was cut from a commit ASES never wrote is still
    blocked immediately, exactly as before -- only "no ref at all yet" gets the grace, never a branch whose
    provenance is demonstrably wrong."""
    repo = git_repo(tmp_path, with_branch=False)
    w = make_world(tmp_path, monkeypatch)
    guards.adopt_current_head(w.conn, "t3", repo)
    _plant_branch(repo, "swarm/T1-coder")
    w.board.cards[w.work("T1")].update(status="running", branch_name="swarm/T1-coder")

    problems = controller.process_card_base_checks("b", repo, w.plan, conn=w.conn)

    assert len(problems) == 1 and "ASES-GIT-01" in problems[0]
    assert w.board.cards[w.work("T1")]["status"] == "blocked"


def test_process_card_base_checks_only_looks_at_this_plans_own_running_cards(tmp_path, monkeypatch):
    repo = git_repo(tmp_path)
    w = make_world(tmp_path, monkeypatch)
    guards.adopt_current_head(w.conn, "t3", repo)
    w.board.add("other_project_card", status="running", branch_name="not-a-real-branch-at-all")

    problems = controller.process_card_base_checks("b", repo, w.plan, conn=w.conn)

    assert problems == []  # no plan_tasks row for it: skipped without even trying to read a nonexistent branch


def test_process_card_base_checks_falls_back_to_the_default_branch_name(tmp_path, monkeypatch):
    """Hermes normally always reports branch_name (controller.create_cards_from_plan passes --branch), but the
    fallback mirrors process_merge_queue's own, for a card whose branch_name comes back empty."""
    repo = git_repo(tmp_path)
    w = make_world(tmp_path, monkeypatch)
    guards.adopt_current_head(w.conn, "t3", repo)
    w.board.cards[w.work("T1")].update(status="running", branch_name=None)

    problems = controller.process_card_base_checks("b", repo, w.plan, conn=w.conn)

    assert problems == []
    (payload,) = payloads(w.conn, "card_base_verified")
    assert payload["branch"] == "swarm/T1-coder"  # f"swarm/{key}-{task.role}"


# =============================================================================================================
# process_finalize
# =============================================================================================================

class FakeFinalgates:
    def __init__(self, status="finished", reason=""):
        self.calls = []
        self.outcome = types.SimpleNamespace(status=status, gate4=None, gate5=None, report_path=None, reason=reason)

    def finalize(self, board, repo, plan, project, models_config, conn, *, now=None):
        self.calls.append((board, repo, plan, project, models_config, conn, now))
        return self.outcome

    def final_gate_question(self, outcome):
        return "Gate 4 found a tracked .env file. Should ASES remove it or leave it?"


def finalize_world(tmp_path, monkeypatch, *, done=True, status="finished"):
    w = make_world(tmp_path, monkeypatch)
    fake = FakeFinalgates(status)
    monkeypatch.setattr(controller, "_finalgates", lambda: fake)
    monkeypatch.setattr(controller, "all_merge_cards_done", lambda *a, **kw: done)
    return w, fake


def run_finalize(w, now=None):
    return controller.process_finalize("b", w.repo, w.plan, w.project, MODELS, conn=w.conn, now=now)


def test_finalize_waits_until_every_merge_card_is_done(tmp_path, monkeypatch):
    w, fake = finalize_world(tmp_path, monkeypatch, done=False)

    assert run_finalize(w) is None and fake.calls == []


def test_finalize_calls_the_final_gates_once_every_merge_card_is_done(tmp_path, monkeypatch):
    """ASES-TSK-04 / ASES-CTL-01: "every merge card is done, Gates 4 and 5 are green on the integration HEAD, and the
    release report is written"."""
    w, fake = finalize_world(tmp_path, monkeypatch)
    moment = datetime(2026, 9, 21, tzinfo=timezone.utc)

    assert run_finalize(w, moment) == "finished"

    assert fake.calls == [("b", w.repo, w.plan, w.project, MODELS, w.conn, moment)]
    assert payloads(w.conn, "finalize_result")[0]["status"] == "finished"
    assert w.board.calls_of("pause") == []


def test_finalize_does_nothing_for_a_project_that_is_already_finished_or_halted(tmp_path, monkeypatch):
    w, fake = finalize_world(tmp_path, monkeypatch)
    bounds.set_status(w.conn, "t3", "finished")
    assert run_finalize(w) is None
    # a stop that landed mid-pass must not start the final gates
    bounds.set_status(w.conn, "t3", "stopped", "swarm stop")
    assert run_finalize(w) is None
    bounds.set_status(w.conn, "t3", "paused")
    assert run_finalize(w) is None

    assert fake.calls == []


def test_a_failed_final_gate_pauses_the_project_with_the_gate_question(tmp_path, monkeypatch):
    w, fake = finalize_world(tmp_path, monkeypatch, status="gate_failed")

    assert run_finalize(w) == "gate_failed"

    reason = "Gate 4 found a tracked .env file. Should ASES remove it or leave it?"
    assert controller._halted(w.conn, "t3") == (True, reason)
    assert w.board.calls_of("pause") == [((reason,), {})]


@pytest.mark.parametrize("status", ["not_ready", "error"])
def test_other_final_statuses_are_returned_without_pausing(tmp_path, monkeypatch, status):
    w, fake = finalize_world(tmp_path, monkeypatch, status=status)

    assert run_finalize(w) == status

    assert w.board.calls_of("pause") == [] and controller._halted(w.conn, "t3") == (False, None)


def test_finalize_uses_the_real_merge_card_statuses(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    fake = FakeFinalgates()
    monkeypatch.setattr(controller, "_finalgates", lambda: fake)

    assert run_finalize(w) is None                                    # merge cards are blocked
    for key in ("T1", "T2"):
        w.board.cards[w.merge(key)]["status"] = "done"
    assert run_finalize(w) == "finished" and len(fake.calls) == 1


# --- round 9 (ASES-QG-04, ASES-SEC-03): Gates 4/5 route through gates.resolve_runner, with no task ---------------


def _sandbox_project(project):
    return dataclasses.replace(project, sandbox={
        "enabled": True, "terminal_backend": "docker", "network_default": False, "mount": "worktree_only",
        "forward_env": [], "network_exceptions": "explicit_allowlist", "image": "registry.example/tool:1.0",
    })


def test_process_finalize_asks_resolve_runner_with_no_task(tmp_path, monkeypatch):
    """Gates 4/5 are project-wide, not task-scoped: resolve_runner is called with no task at all, so no task's
    network exception can ever reach a final gate (ASES-SEC-05, ASES-SEC-07)."""
    w, fake = finalize_world(tmp_path, monkeypatch)
    seen = []
    real = gates_mod.resolve_runner
    monkeypatch.setattr(gates_mod, "resolve_runner", lambda *a, **kw: seen.append((a, kw)) or real(*a, **kw))

    controller.process_finalize("b", w.repo, w.plan, w.project, MODELS, conn=w.conn)

    assert seen == [((w.project,), {})]


def test_process_finalize_with_the_sandbox_off_calls_finalize_with_todays_exact_keywords(tmp_path, monkeypatch):
    """FakeFinalgates.finalize has a fixed signature (now= only, no runner/run4/run5): reaching "finished" here
    at all, with no TypeError, proves the sandbox kwargs are omitted when the switch is off."""
    w, fake = finalize_world(tmp_path, monkeypatch)

    assert run_finalize(w) == "finished"
    assert fake.calls == [("b", w.repo, w.plan, w.project, MODELS, w.conn, None)]


def test_process_finalize_wires_runner_and_self_contained_run4_run5_when_the_sandbox_is_enabled(tmp_path, monkeypatch):
    w, fake = finalize_world(tmp_path, monkeypatch)
    fake.run_gate4 = lambda *a, **kw: None  # finalgates.run_gate4/run_gate5 stand-ins: only their identity matters
    fake.run_gate5 = lambda *a, **kw: None
    seen = {}

    def fake_finalize(board, repo, plan, project, models_config, conn, *, now=None, runner=None, run4=None,
                       run5=None):
        seen.update(runner=runner, run4=run4, run5=run5)
        return fake.outcome

    monkeypatch.setattr(fake, "finalize", fake_finalize)

    controller.process_finalize("b", w.repo, w.plan, _sandbox_project(w.project), MODELS, conn=w.conn)

    assert callable(seen["runner"])
    assert seen["run4"].func is fake.run_gate4 and seen["run4"].keywords == {"self_contained_checkout": True}
    assert seen["run5"].func is fake.run_gate5 and seen["run5"].keywords == {"self_contained_checkout": True}


def test_the_final_gates_module_is_imported_lazily():
    """Importing the controller must not import finalgates: it is built at the same time as the controller and is only
    needed once every merge card is done."""
    code = "import sys, ases.controller; print('ases.finalgates' in sys.modules)"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                            env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    assert result.stdout.strip() == "False", result.stderr


# =============================================================================================================
# run_pass
# =============================================================================================================

STEPS = ["guard", "idle_worktrees", "git12_attribution", "usage", "recovery", "reviewer_contract", "bounds",
         "budget", "unpark", "review", "dispatch", "card_base", "provision", "merge", "finalize"]
SUMMARY_KEYS = {"parked", "dispatch", "sent_back", "merged", "unreviewed", "usage_sessions", "integrity", "warnings",
                "recovery", "reviewer_contract", "unparked", "provisioned", "stopped", "stop_reason", "final",
                "finished"}


class PassRig:
    """Every step of run_pass replaced by a recorder, so the ORDER and the arguments can be pinned, and any one step
    can be made to raise."""

    def __init__(self, tmp_path, monkeypatch):
        self.order = []
        self.args = {}
        self.raises = {}
        self.results = {"idle_worktrees": [], "git12_attribution": [], "usage": ["s1", "s2"], "recovery": [],
                        "reviewer_contract": [], "bounds": (False, None), "budget": ["T9"], "unpark": [],
                        "review": [], "dispatch": {"spawned": 1}, "card_base": [], "provision": [],
                        "merge": ["T1"], "finalize": None}
        self.conn = db.connect(tmp_path / "ases.db")
        self.plan = types.SimpleNamespace(project="p", integration_branch="integration")
        self.project = types.SimpleNamespace(budgets={"k": 1})
        self.models = {"providers": {}, "models": []}
        self.guard_ok = True
        rig = self

        def step(name):
            def run(*args, **kwargs):
                rig.order.append(name)
                rig.args[name] = (args, kwargs)
                if name in rig.raises:
                    raise rig.raises[name]
                return rig.results[name]
            return run

        def guard(repo, integration_branch, expected_head=None, **kw):
            rig.order.append("guard")
            rig.args["guard"] = ((repo, integration_branch, expected_head), kw)
            if rig.raises.get("guard"):
                raise rig.raises["guard"]
            problems = () if rig.guard_ok else ("dirty: ?? stray.txt",)
            return guards.GuardResult(rig.guard_ok, problems, "abc", integration_branch)

        def merge(board, repo, plan, project, *, conn, unreviewed=None, models_config=None, integrity=None):
            rig.order.append("merge")
            rig.args["merge"] = ((board, repo, plan, project), {"conn": conn, "unreviewed": unreviewed,
                                                                "models_config": models_config,
                                                                "integrity": integrity})
            if "merge" in rig.raises:
                raise rig.raises["merge"]
            unreviewed.append("T7")
            if rig.results.get("merge_integrity") and integrity is not None:
                integrity.extend(rig.results["merge_integrity"])
            return rig.results["merge"]

        monkeypatch.setattr(guards, "check_primary_checkout", guard)
        for name, target in (("idle_worktrees", "process_idle_worktrees"),
                             ("git12_attribution", "process_git12_attribution"),
                             ("recovery", "process_recovery"),
                             ("reviewer_contract", "process_reviewer_contract"),
                             ("bounds", "process_bounds"), ("budget", "process_budget_gate"),
                             ("unpark", "process_unpark"), ("review", "process_review_lane"),
                             ("card_base", "process_card_base_checks"),
                             ("provision", "process_provision"), ("finalize", "process_finalize")):
            monkeypatch.setattr(controller, target, step(name))
        monkeypatch.setattr(controller, "process_merge_queue", merge)
        monkeypatch.setattr(usage_mod, "ingest_run_usage", step("usage"))
        monkeypatch.setattr(hermes, "kanban_dispatch", step("dispatch"))

    def run(self, **kwargs):
        return controller.run_pass("b", "the-repo", self.plan, self.project, self.models, conn=self.conn, **kwargs)


def test_run_pass_runs_the_steps_in_the_order_of_the_blueprint_loop(tmp_path, monkeypatch):
    rig = PassRig(tmp_path, monkeypatch)

    summary = rig.run()

    assert rig.order == STEPS
    assert summary["stopped"] is False and summary["integrity"] == [] and summary["warnings"] == []
    assert summary["parked"] == ["T9"] and summary["merged"] == ["T1"] and summary["dispatch"] == {"spawned": 1}
    assert summary["usage_sessions"] == 2 and summary["unreviewed"] == ["T7"]
    assert summary["finished"] is False and summary["final"] is None


def test_run_pass_hands_each_step_what_it_needs(tmp_path, monkeypatch):
    rig = PassRig(tmp_path, monkeypatch)
    moment = datetime(2026, 9, 21, tzinfo=timezone.utc)
    rig.results["idle_worktrees"] = ["warn one"]
    rig.results["recovery"] = [{"task_key": "T1", "action": "fresh_attempt", "kind": "capability"}]
    rig.results["unpark"] = ["T2"]
    rig.results["card_base"] = ["card base problem"]
    rig.results["provision"] = ["n1"]

    summary = rig.run(now=moment)

    everything = ("b", "the-repo", rig.plan, rig.project, rig.models)
    timed = {"conn": rig.conn, "now": moment}
    assert rig.args["idle_worktrees"] == (("b", "the-repo", rig.plan), {"conn": rig.conn})
    # round 19, package GIT12 (ASES-GIT-12): the same five positional args and (conn, now) every other
    # every-project-and-clock-aware step gets (everything/timed, defined above).
    assert rig.args["git12_attribution"] == (everything, timed)
    assert rig.args["recovery"] == (everything, timed)
    assert rig.args["bounds"] == (everything, timed)
    assert rig.args["budget"] == (("b", rig.plan, rig.models),
                                  {"conn": rig.conn, "budgets": rig.project.budgets, "project": rig.project})
    # Round 18: the pass clock reaches process_unpark too, which a quota park's reset check reads.
    # Round 18b: run_pass's own repo reaches it too, for the pre-unblock Gate 1 check.
    assert rig.args["unpark"] == (("b", rig.plan, rig.models),
                                  {"conn": rig.conn, "budgets": rig.project.budgets, "project": rig.project,
                                   "now": moment, "repo": "the-repo"})
    # Round 19 fix round 2 (reviewer finding, major): models_config now reaches process_reviewer_contract too,
    # so profiles.active_reviewer_profiles can resolve every active kanban_lifecycle_only profile, not just the
    # literal reviewer role.
    assert rig.args["reviewer_contract"] == (everything, {"conn": rig.conn})
    assert rig.args["review"] == (("b", "the-repo", rig.plan, rig.project), {"conn": rig.conn})
    assert rig.args["card_base"] == (("b", "the-repo", rig.plan), {"conn": rig.conn})
    assert rig.args["provision"] == (("b", rig.plan, rig.project), {"conn": rig.conn})
    assert rig.args["merge"][1]["models_config"] is rig.models
    assert rig.args["finalize"] == (everything, timed)
    assert (summary["warnings"], summary["recovery"], summary["unparked"], summary["provisioned"]) == (
        ["warn one", "card base problem"], rig.results["recovery"], ["T2"], ["n1"])


@pytest.mark.parametrize("status", ["stopped", "paused"])
def test_a_halted_project_returns_at_once_and_does_nothing_else(tmp_path, monkeypatch, status):
    """ASES-CTL-01: `paused` now keeps its own reason in project_state exactly like `stopped` does (bounds.py's
    set_status), so both statuses report it here the same way and neither needs the project_paused event."""
    rig = PassRig(tmp_path, monkeypatch)
    bounds.set_status(rig.conn, "p", status, "swarm stop")

    summary = rig.run()

    assert rig.order == []                                       # not even the primary-checkout guard
    assert summary["stopped"] is True and summary["stop_reason"] == "swarm stop"
    assert set(summary) == SUMMARY_KEYS and summary["finished"] is False and summary["merged"] == []


def test_an_integrity_violation_still_returns_every_key_and_nothing_after_the_guard_runs(tmp_path, monkeypatch):
    rig = PassRig(tmp_path, monkeypatch)
    rig.guard_ok = False

    summary = rig.run()

    assert rig.order == ["guard"] and set(summary) == SUMMARY_KEYS
    assert summary["integrity"] == ["dirty: ?? stray.txt"] and summary["finished"] is False
    assert len(payloads(rig.conn, "integrity_violation")) == 1


def test_a_bound_that_stops_the_project_ends_the_pass_before_anything_is_dispatched(tmp_path, monkeypatch):
    rig = PassRig(tmp_path, monkeypatch)
    rig.results["bounds"] = (True, "replans_per_project reached for project (2 of 2): User decision")
    rig.results["recovery"] = [{"task_key": "T1", "action": "replan", "kind": "review_rounds"}]

    summary = rig.run()

    assert rig.order == [
        "guard", "idle_worktrees", "git12_attribution", "usage", "recovery", "reviewer_contract", "bounds",
    ]
    assert summary["stopped"] is True and summary["stop_reason"].startswith("replans_per_project reached")
    assert set(summary) == SUMMARY_KEYS and summary["usage_sessions"] == 2
    assert summary["recovery"] == rig.results["recovery"]            # what was done before the stop is still reported
    assert summary["dispatch"] == {} and summary["merged"] == []


@pytest.mark.parametrize("step", ["idle_worktrees", "git12_attribution", "usage", "recovery", "bounds", "unpark",
                                   "card_base", "provision", "finalize"])
def test_a_step_that_is_not_safety_critical_cannot_stop_the_pass(tmp_path, monkeypatch, step):
    """A stale ledger, a failed lease or a final gate that cannot run must not stop the merge queue: the failure is a
    pass_step_error event naming the step, a warning, and every later step still runs."""
    rig = PassRig(tmp_path, monkeypatch)
    rig.raises[step] = RuntimeError(f"{step} exploded")

    summary = rig.run()

    assert rig.order == STEPS                                     # every step ran, in order, including the later ones
    errors = payloads(rig.conn, "pass_step_error")
    assert [(e["step"], step in e["error"]) for e in errors] == [(step, True)]
    assert summary["warnings"] == [f"step {step} failed: RuntimeError: {step} exploded"]
    assert set(summary) == SUMMARY_KEYS and summary["stopped"] is False
    # events.py package, round 9: every _isolated call site is inside run_pass, which already has plan.project
    # ("p" for this rig) in scope, so pass_step_error carries it too instead of leaving the column NULL.
    project_rows = rig.conn.execute("SELECT project FROM events WHERE kind = 'pass_step_error'").fetchall()
    assert project_rows and all(r["project"] == "p" for r in project_rows)
    if step == "usage":                                           # the old, specific event is kept
        assert "usage exploded" in payloads(rig.conn, "usage_ingest_error")[0]["error"]
        assert summary["usage_sessions"] == 0


@pytest.mark.parametrize("step", ["guard", "budget", "review", "dispatch", "merge"])
def test_an_exception_in_a_safety_critical_step_propagates(tmp_path, monkeypatch, step):
    rig = PassRig(tmp_path, monkeypatch)
    rig.raises[step] = RuntimeError(f"{step} exploded")

    with pytest.raises(RuntimeError, match=f"{step} exploded"):
        rig.run()

    assert rig.order == STEPS[:STEPS.index(step) + 1]             # and nothing after it ran
    assert "pass_step_error" not in kinds(rig.conn)


def test_a_failing_bounds_step_is_not_a_stop(tmp_path, monkeypatch):
    rig = PassRig(tmp_path, monkeypatch)
    rig.raises["bounds"] = ValueError("budgets.attempts_per_card must be an integer")

    summary = rig.run()

    assert summary["stopped"] is False and rig.order[-1] == "finalize"


def test_finished_is_true_only_when_bounds_says_the_project_is_finished(tmp_path, monkeypatch):
    rig = PassRig(tmp_path, monkeypatch)
    monkeypatch.setattr(controller, "all_merge_cards_done", lambda *a, **kw: True)  # not what decides it any more

    assert rig.run()["finished"] is False                          # every merge card done, gates not run yet

    bounds.set_status(rig.conn, "p", "finished")
    assert rig.run()["finished"] is True                           # an earlier pass finished it

    other = PassRig(tmp_path / "second", monkeypatch)
    other.results["finalize"] = "finished"
    summary = other.run()
    assert summary["finished"] is True and summary["final"] == "finished"


def test_run_pass_reports_a_failed_final_gate_and_the_pause_it_caused(tmp_path, monkeypatch):
    rig = PassRig(tmp_path, monkeypatch)

    def finalize(*a, **kw):
        rig.order.append("finalize")
        bounds.set_status(rig.conn, "p", "paused")
        events.record(rig.conn, "project_paused", {"project": "p", "reason": "Gate 4 failed. What now?"})
        return "gate_failed"

    monkeypatch.setattr(controller, "process_finalize", finalize)

    summary = rig.run()

    assert summary["final"] == "gate_failed" and summary["finished"] is False
    assert summary["stopped"] is True and summary["stop_reason"] == "Gate 4 failed. What now?"


def test_run_pass_reports_a_stop_that_landed_during_the_merge_queue(tmp_path, monkeypatch):
    rig = PassRig(tmp_path, monkeypatch)

    def merge(board, repo, plan, project, *, conn, unreviewed=None, models_config=None, integrity=None):
        bounds.set_status(conn, "p", "stopped", "swarm stop")      # the kill switch, mid-pass
        return []

    monkeypatch.setattr(controller, "process_merge_queue", merge)

    summary = rig.run()

    assert summary["stopped"] is True and summary["stop_reason"] == "swarm stop"


def test_run_pass_warns_when_the_final_gates_could_not_run(tmp_path, monkeypatch):
    rig = PassRig(tmp_path, monkeypatch)
    rig.results["finalize"] = "error"

    summary = rig.run()

    assert summary["final"] == "error" and any("final gates could not run" in w for w in summary["warnings"])


def test_run_pass_gives_the_guard_the_stored_head_and_the_plans_branch(tmp_path, monkeypatch):
    rig = PassRig(tmp_path, monkeypatch)
    guards.set_expected_head(rig.conn, "p", "deadbeef")
    rig.plan = types.SimpleNamespace(project="p", integration_branch="trunk")

    rig.run()

    assert rig.args["guard"][0] == ("the-repo", "trunk", "deadbeef")


def test_run_pass_summary_carries_every_key_on_a_normal_pass_too(tmp_path, monkeypatch):
    rig = PassRig(tmp_path, monkeypatch)

    assert set(rig.run()) == SUMMARY_KEYS


# =============================================================================================================
# hygiene: the characters the project forbids
# =============================================================================================================

def test_the_files_of_this_package_hold_only_ascii():
    """No em dash, no section sign, nothing above 127 in code, comments, docstrings or strings (the Windows console is
    cp1252 and crashes on the rest)."""
    for path in (ROOT / "src" / "ases" / "controller.py", ROOT / "tests" / "unit" / "test_controller.py",
                 pathlib.Path(__file__)):
        text = path.read_text(encoding="utf-8")
        bad = sorted({hex(ord(ch)) for ch in text if ord(ch) > 127})
        assert bad == [], f"{path.name} holds non-ASCII characters: {bad}"
