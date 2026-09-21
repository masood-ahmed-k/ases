"""The acceptance rig (ASES-TST-01, ASES-TST-02; blueprint 22.0).

"A fake OpenAI-compatible server with scripted responses, a --board ases-test board, throwaway Git repositories with
seeded content, and a scripted fake worker that performs chosen file edits." This module assembles the last three for the
scenarios of section 22 (the first is ases.fakes.provider and is only needed where a scenario reads prompts):

  world           a temp PRIMARY repository on the branch `integration` (one seeded commit, then the approved plan published
                  the way `swarm approve` publishes it), a temp ASES database, a programmatic ProjectConfig and models
                  config in the shape tests/unit/test_controller.py uses, and a FakeHermes bound to that repository and
                  installed over the hermes module, with default personas for lead, coder-1 and reviewer;
  create_cards    calls the real controller.create_cards_from_plan;
  run_until       calls the real controller.run_pass in a loop, moving the fake clock between passes (the CLI's polling
                  interval, 20 seconds), until a predicate holds, and returns every summary;
  git             runs git in the primary repository;
  world_factory   builds another world (a different plan, seeded files, budgets).

Nothing here starts Hermes, a model, a network connection or Docker. The controller, review, mergeq, guards, gates, usage,
questions and recovery modules that a scenario drives are the real ones.
"""
from __future__ import annotations

import dataclasses
import inspect
import json
import pathlib
import subprocess

import pytest

from ases import config, controller, db, guards
from ases import plan as plan_mod
from ases.fakes import worker as fw
from ases.fakes.board import FakeHermes

BOARD = "ases-test"
PROJECT_ID = "p_acceptance"
ROLES = {"lead": "lead", "coder": "coder-1", "reviewer": "reviewer"}
POLL_SECONDS = 20  # `swarm run --sleep-seconds` defaults to 20, so a pass advances the fake clock by as much

# Two coder tasks, the second depending on the first (blueprint 22.2: "The Lead writes a plan with two tasks").
DEFAULT_PLAN = {
    "project": "acceptance",
    "integration_branch": "integration",
    "gate_profiles": {"trivial": ["echo ok"]},
    "tasks": [
        {"key": "T1", "title": "add a", "role": "coder", "depends_on": [], "touches": ["a.py"],
         "acceptance": ["a.py defines add(x, y) returning x + y"], "gate_profile": "trivial",
         "estimated_requests": 5},
        {"key": "T2", "title": "add b", "role": "coder", "depends_on": ["T1"], "touches": ["b.py"],
         "acceptance": ["b.py defines sub(x, y) returning x - y"], "gate_profile": "trivial",
         "estimated_requests": 5},
    ],
}

# Just T1, for scenarios about one card (blueprint 22.6).
ONE_TASK_PLAN = {**DEFAULT_PLAN, "tasks": DEFAULT_PLAN["tasks"][:1]}

# The provider and model rows the budget gate and the usage ingest read. No provider here has a daily cap.
MODELS_CONFIG = {
    "providers": {"fake": {"limits": {}}},
    "models": [
        {"provider": "fake", "model": "fake-coder", "role_class": "coder", "pinned": True},
        {"provider": "fake", "model": "fake-reviewer", "role_class": "reviewer", "pinned": True},
    ],
}

BUDGETS = {
    "attempts_per_card": 3, "review_rounds_per_task": 3, "fix_cards_per_task": 2, "replans_per_project": 2,
    "max_cards": 40, "card_runtime_minutes": 45, "daily_reserve_percent": 10, "review_reserve_requests": 20,
}


def _git(cwd, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert result.returncode == 0, f"git {' '.join(args)} failed in {cwd}: {result.stderr.strip()}"
    return result.stdout.strip()


@dataclasses.dataclass
class World:
    """Everything a scenario needs, and the helpers that drive it. `pairs` (task key to CardPair) fills in when the cards
    are created; `summaries` collects the summary of every controller pass so far."""
    tmp_path: pathlib.Path
    repo: pathlib.Path
    db_path: pathlib.Path
    conn: object
    plan_raw: dict
    plan: plan_mod.Plan
    project: config.ProjectConfig
    models_config: dict
    fake: FakeHermes
    plan_sha: str
    board: str = BOARD
    project_id: str = PROJECT_ID
    pairs: dict = dataclasses.field(default_factory=dict)
    summaries: list = dataclasses.field(default_factory=list)

    def git(self, *args: str) -> str:
        """Run git in the primary repository and return its stripped stdout (a failure fails the test)."""
        return _git(self.repo, *args)

    def create_cards(self) -> dict:
        """The real controller.create_cards_from_plan, for this plan on the fake board. Returns {task key: CardPair}."""
        created = controller.create_cards_from_plan(
            self.board, self.project_id, self.repo, self.plan, self.project, conn=self.conn)
        self.pairs = {pair.task_key: pair for pair in created}
        return self.pairs

    def one_pass(self) -> dict:
        """One real controller.run_pass. `now` is handed over when this controller accepts it, so the controller's clock
        is the fake board's."""
        kwargs = {"conn": self.conn}
        if "now" in inspect.signature(controller.run_pass).parameters:
            kwargs["now"] = self.fake.now
        summary = controller.run_pass(
            self.board, self.repo, self.plan, self.project, self.models_config, **kwargs)
        self.summaries.append(summary)
        return summary

    def run_until(self, predicate, max_passes: int = 40, step: int = POLL_SECONDS) -> list:
        """Run controller passes until `predicate(world)` is true, checking after each pass and moving the fake clock by
        `step` seconds between passes (which is what wakes sleeping workers and times out hung ones). Returns the summaries
        of THIS call. Fails, with the board printed, when the predicate still does not hold after `max_passes`."""
        ran = []
        for _ in range(max_passes):
            ran.append(self.one_pass())
            if predicate(self):
                return ran
            self.fake.tick(step)
        raise AssertionError(
            f"the predicate still did not hold after {max_passes} passes\n{self.fake.describe()}\n"
            f"last summary: {ran[-1] if ran else None}")

    def restart_controller(self) -> None:
        """Stop the controller and start it again: the ASES database is closed and reopened. State that survives a restart
        lives in the database, on the board and in git, never in this object (blueprint 22.2: "Stop the controller once in
        the middle and confirm that state survives")."""
        self.conn.close()
        self.conn = db.connect(self.db_path)

    def card(self, card_id: str) -> dict:
        return self.fake.card(card_id)

    def work_card_id(self, task_key: str) -> str:
        """The task's CURRENT work card (a fix card once one was opened), as plan_tasks says."""
        row = self.conn.execute(
            "SELECT work_card_id FROM plan_tasks WHERE project = ? AND task_key = ?",
            (self.plan.project, task_key)).fetchone()
        return row["work_card_id"]

    def all_merge_cards_done(self) -> bool:
        return bool(self.pairs) and all(
            self.fake.card(pair.merge_card_id)["status"] == "done" for pair in self.pairs.values())


def make_world(
    tmp_path: pathlib.Path, monkeypatch, *, plan_raw: dict | None = None, seed: dict | None = None,
    budgets: dict | None = None, models_config: dict | None = None,
) -> World:
    """Build a world: a primary repository with `seed` files committed, then the plan published to `integration`
    (controller.publish_plan, the step `swarm approve` runs before any card exists), the ASES database, the project
    config and a FakeHermes installed over the hermes module. `plan_raw` defaults to DEFAULT_PLAN."""
    plan_raw = json.loads(json.dumps(plan_raw if plan_raw is not None else DEFAULT_PLAN))
    repo = tmp_path / "primary"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "integration")
    for key, value in (("user.name", "ASES acceptance"), ("user.email", "acceptance@example.invalid"),
                       ("commit.gpgsign", "false"), ("core.autocrlf", "false")):
        _git(repo, "config", key, value)
    for name, text in (seed or {"README.md": "seeded repository\n"}).items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")

    (repo / "docs" / "ases").mkdir(parents=True)
    (repo / "docs" / "ases" / "plan.json").write_text(json.dumps(plan_raw, indent=2), encoding="utf-8")
    plan_sha = controller.publish_plan(repo, "integration")

    plan = plan_mod.parse_and_validate(plan_raw, known_roles=set(ROLES), max_cards=40)
    db_path = tmp_path / "ases.db"
    conn = db.connect(db_path)
    project = config.ProjectConfig(
        name=plan.project, environment="native", data_class="public", workspace_root=tmp_path / "ws",
        ases_home=tmp_path / "home", board=BOARD, integration_branch="integration", roles=dict(ROLES),
        concurrency={"max_in_progress": 3, "per_profile": 1, "hard_max": 6},
        budgets={**BUDGETS, **(budgets or {})}, hermes_tested_version="0.21.3",
        hermes_native_home=tmp_path / "hermes",
    )
    fake = FakeHermes(repo, board=BOARD, integration_branch="integration")
    fake.install(monkeypatch)
    # Default personas: a coder that writes the files its card touches, a reviewer that approves what it is shown, and a
    # lead that never gets a card here (the Lead writes plan.json outside the board) but must exist as a profile.
    fake.register_worker("lead", fw.ScriptedWorker([fw.Complete("nothing for the lead to do")]))
    fake.register_worker("coder-1", fw.touches_coder())
    fake.register_worker("reviewer", fw.reviewer_pass())
    # The controller's own integrity baseline, as `swarm run` adopts it before its first pass.
    guards.adopt_current_head(conn, plan.project, repo)
    return World(
        tmp_path=tmp_path, repo=repo, db_path=db_path, conn=conn, plan_raw=plan_raw, plan=plan, project=project,
        models_config=models_config if models_config is not None else json.loads(json.dumps(MODELS_CONFIG)),
        fake=fake, plan_sha=plan_sha,
    )


@pytest.fixture
def world(tmp_path, monkeypatch):
    """The default world: DEFAULT_PLAN (T1, then T2 which depends on it) on a fresh repository and an empty fake board."""
    made = make_world(tmp_path, monkeypatch)
    yield made
    made.conn.close()


@pytest.fixture
def world_factory(tmp_path, monkeypatch):
    """make_world bound to this test's tmp_path and monkeypatch, for a scenario that needs its own plan or seed files."""
    built = []

    def factory(**kwargs) -> World:
        made = make_world(tmp_path, monkeypatch, **kwargs)
        built.append(made)
        return made

    yield factory
    for made in built:
        made.conn.close()


@pytest.fixture
def one_task_plan() -> dict:
    """A copy of ONE_TASK_PLAN (just T1), for world_factory(plan_raw=...)."""
    return json.loads(json.dumps(ONE_TASK_PLAN))


@pytest.fixture
def create_cards():
    """create_cards(world): the real controller.create_cards_from_plan. Returns {task key: CardPair}."""
    return lambda w: w.create_cards()


@pytest.fixture
def run_until():
    """run_until(world, predicate, max_passes=40): the real controller.run_pass until predicate(world) holds. Returns the summaries."""
    return lambda w, predicate, max_passes=40, step=POLL_SECONDS: w.run_until(predicate, max_passes, step)


@pytest.fixture
def git():
    """git(world, *args): git in the world's primary repository, stripped stdout."""
    return lambda w, *args: w.git(*args)
