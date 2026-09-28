"""Acceptance 22.2, full scenario (blueprint.txt [p401]/[p402]; Appendix F: ASES-ARC-02, ASES-ARC-06,
ASES-ARC-09, ASES-TSK-01, ASES-TSK-04, ASES-GIT-01, ASES-GIT-02, ASES-GIT-06, ASES-GIT-10, ASES-GIT-16,
ASES-REC-05).

[p402]: "Start from an empty repository. The Lead writes a plan with two tasks; Gate 0 and Gate P pass; the
controller publishes the approved architecture, contracts, decisions and plan.json to the integration branch
and records that commit; only then does it create four implementation/merge cards. coder-1 changes code in a
worktree branched from that exact integration HEAD; the controller re-runs Gate 1; the Reviewer completes the
work card; the merge queue runs Gate 3 on the candidate and fast-forwards with one squash commit per task; both
merge cards end as done. One card asks a question, and swarm questions and swarm answer unblock it. Stop the
controller once in the middle and confirm that state survives."

Round 17: this file replaces tests/acceptance/test_scenarios_demo.py's 22.2 half, which its own docstring called
"cores only" and left for "a later round". The two functions there drove controller.publish_plan directly on a
hand-seeded repository, with no Gate 0 (plan_mod.parse_and_validate never ran) and no Gate P (no critic verdict
ever existed) before cards were created -- the exact opening two sentences of [p402] were simply never exercised.
Every assertion those two functions made is kept here (moved, not rewritten), now built on top of a real Gate 0
+ Gate P + publish sequence instead of the shortcut, plus the clauses that were still missing: the empty
repository (controller.ensure_repo_bootstrapped), Gate 0 (plan_mod.load_plan_file, the exact function cli._load_plan
calls), Gate P (critic.run_critique / critic.next_step, a fake invoke, never a real hermes call), the ordering
proof that no card exists until after publish, and Gate 3's own recorded result. test_scenarios_demo.py's third
function (merge cards that are only waiting are not open questions) is unrelated to Gate 0/Gate P and is moved
here verbatim on the plain `world`/`create_cards` fixtures. Nothing from that file is dropped; test_scenarios_demo.py
itself is removed as part of this round (see the round 17 report for the exact reasoning).

Drives the REAL controller (controller.run_pass, review, mergeq, guards, gates, usage, questions, recovery),
critic and cli._estimate_lines against ases.fakes.board.FakeHermes, with real git worktrees and scripted
workers. Nothing here starts Hermes, a model, a network connection or Docker."""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess

from ases import cli as cli_mod
from ases import config as config_mod
from ases import controller
from ases import critic as critic_mod
from ases import db
from ases import guards
from ases import plan as plan_mod
from ases import questions
from ases.fakes import worker as fw
from ases.fakes.board import FakeHermes

from tests.acceptance.conftest import BOARD, BUDGETS, DEFAULT_PLAN, MODELS_CONFIG, PROJECT_ID, ROLES, World

A_PY = "def add(x, y):\n    return x + y\n"
B_PY = "def sub(x, y):\n    return x - y\n"

# A valid Gate P PASS verdict, in the review format of section 13.3 (the same shape
# tests/unit/test_critic.py's verdict_text and tests/acceptance/test_22_14_plan_rejection.py's
# _changes_required_json build; no "commit" field, matching that file's own precedent for a verdict this rig
# does not need bound to a specific plan hash to be accepted as valid).
PASS_JSON = (
    '{"review_status": "PASS", "summary": "Small, testable and correctly ordered.", '
    '"architecture_issues": [], "missing_cases": [], "security_issues": [], "test_gaps": [], '
    '"gate_tampering_suspected": false, "required_changes": []}'
)


class _FakeInvoke:
    """A stand-in for the reviewer call Gate P makes (critic.run_critique's `invoke`), copied from
    tests/unit/test_critic.py's own FakeInvoke and reused acceptance-side by
    tests/acceptance/test_22_14_plan_rejection.py: one reply per call, exit 0, so Gate P never touches a real
    hermes or a real model provider (the standing zero-quota rule)."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, profile, prompt, timeout):
        self.calls.append((profile, prompt, timeout))
        reply = self.replies.pop(0)
        return reply if isinstance(reply, tuple) else (0, reply, "")


def _git(cwd, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert result.returncode == 0, f"git {' '.join(args)} failed in {cwd}: {result.stderr.strip()}"
    return result.stdout.strip()


def _bootstrapped_world(tmp_path, monkeypatch, *, plan_raw: dict | None = None) -> World:
    """[p402]'s first three sentences, up to "only then does it create four implementation/merge cards": builds
    a World the same way tests/acceptance/conftest.py's make_world does, except that every step between "empty
    repository" and "publishes ... and records that commit" is the real function cli.cmd_plan / cmd_critique /
    cmd_approve calls, in the same order, instead of make_world's own shortcut (one hand-run `git init` plus a
    direct controller.publish_plan call, no Gate 0, no Gate P):

      1. controller.ensure_repo_bootstrapped on a repository that does not exist on disk at all yet
         (ASES-GIT-10: "git worktree add needs at least one commit ... swarm run MUST create an initial commit
         and the integration branch when the repository is empty").
      2. The plan is written to docs/ases/plan.json (Appendix C.1, the Lead's own prompt: "Write the plan to
         docs/ases/plan.json in the given schema").
      3. Gate 0: plan_mod.load_plan_file, the exact function cli._load_plan calls, reading the file just
         written (not the in-memory dict: a genuine Gate 0 run needs the file on disk, same as the real CLI).
      4. Gate P: cli._estimate_lines (the budget/calendar text Gate P and the approval screen both read),
         then critic.run_critique with a fake invoke returning a PASS verdict, critic.record_critique, and
         critic.next_step must say APPROVE -- exactly cmd_critique's own loop body, one round, no CHANGES_REQUIRED
         (that path belongs to acceptance 22.14).
      5. controller.publish_plan, then controller.pin_gate_profiles: cmd_approve's own next two calls, in that
         order, after which the board is asserted empty one last time before the caller creates any card.

    Returns a World with `pairs` empty: the caller calls world.create_cards() itself, so "only then does it
    create four implementation/merge cards" is an assertion the test makes, not a fact buried in a fixture."""
    plan_raw = json.loads(json.dumps(plan_raw if plan_raw is not None else DEFAULT_PLAN))
    repo = tmp_path / "primary"
    db_path = tmp_path / "ases.db"
    conn = db.connect(db_path)

    assert not repo.exists()  # "start from an empty repository": nothing here yet, not even the directory
    # A .gitattributes written before ensure_repo_bootstrapped's own `git init` (it writes README.md/.gitignore
    # into the plain directory BEFORE running `git init`, so this is present in the working tree from that
    # commit on): this machine's system-level git config carries core.autocrlf=true (confirmed empirically; not
    # set at the global or repo level, so a test cannot reach it through repo-level config the way
    # tests/acceptance/conftest.py's make_world sets core.autocrlf=false right after ITS OWN `git init`, before
    # anything is written). Left engaged, autocrlf silently re-normalizes README.md/.gitignore/plan.json the
    # first time ANY later `git add -A` runs on them from a DIFFERENT process/environment than the one that
    # made the original commit (fakes/worker.py's coder git calls, unlike this module's own gitexec-wrapped
    # ones, take no explicit env override) -- turning a coder's own one-file change into a spurious extra diff
    # on files it never touched, which then fails the touches-scope check for a reason that has nothing to do
    # with anything this scenario is about. "* -text" turns line-ending conversion off entirely, for every file,
    # regardless of which process or environment later touches them.
    repo.mkdir(parents=True)
    (repo / ".gitattributes").write_text("* -text\n", encoding="utf-8", newline="\n")
    created = controller.ensure_repo_bootstrapped(repo, "integration", conn=conn)
    assert created is True  # ASES-GIT-10: it had to build the branch and the initial commit itself
    # ensure_repo_bootstrapped's own commit is scoped with -c user.name/-c user.email (never git config, the
    # standing hard rule about identity): a later PLAIN `git commit` (controller.publish_plan's own, below) still
    # needs a repo-level identity to fall back to, so this machine's lack of a global git identity cannot fail
    # it. core.autocrlf is deliberately left alone (unlike make_world's own from-scratch `git init`): the
    # bootstrap commit above already wrote and added README.md/.gitignore under whatever autocrlf this repo
    # inherited at `git init` time, and setting a DIFFERENT value now would make git compare the working tree
    # against that commit under a rule neither was written with, reporting both as spuriously modified.
    for key, value in (("user.name", "ASES acceptance"), ("user.email", "acceptance@example.invalid"),
                       ("commit.gpgsign", "false")):
        _git(repo, "config", key, value)

    (repo / "docs" / "ases").mkdir(parents=True)
    plan_path = repo / "docs" / "ases" / "plan.json"
    plan_path.write_text(json.dumps(plan_raw, indent=2), encoding="utf-8")
    # [p402] "publishes the approved architecture, contracts, decisions and plan.json": the Lead's other outputs
    # (Appendix C.1: architecture.md, contracts/, decisions/ under docs/ases/), so the publish below is checked
    # for all four, not only plan.json.
    published_docs = {
        "docs/ases/architecture.md": "# Architecture\n\nTwo modules, a and b.\n",
        "docs/ases/contracts/a.md": "# Contract: a\n\na.py defines A = 1.\n",
        "docs/ases/decisions/0001-two-modules.md": "# Decision 0001\n\nKeep a and b separate.\n",
    }
    for relative, text in published_docs.items():
        (repo / relative).parent.mkdir(parents=True, exist_ok=True)
        (repo / relative).write_text(text, encoding="utf-8")

    plan = plan_mod.load_plan_file(plan_path, known_roles=set(ROLES), max_cards=40)  # Gate 0

    project = config_mod.ProjectConfig(
        name=plan.project, environment="native", data_class="public", workspace_root=tmp_path / "ws",
        ases_home=tmp_path / "home", board=BOARD, integration_branch="integration", roles=dict(ROLES),
        concurrency={"max_in_progress": 3, "per_profile": 1, "hard_max": 6},
        budgets=dict(BUDGETS), hermes_tested_version="0.21.3", hermes_native_home=tmp_path / "hermes",
    )
    models_config = json.loads(json.dumps(MODELS_CONFIG))

    estimate = cli_mod._estimate_lines(plan, project, models_config, conn)
    assert not estimate.model_rejected and not estimate.policy_violation and not estimate.unaffordable

    invoke = _FakeInvoke(PASS_JSON)
    critique = critic_mod.run_critique(repo=repo, plan_path=plan_path, invoke=invoke, estimate_text=estimate.text())
    assert critique.valid and critique.status == "PASS"
    critic_mod.record_critique(conn, plan.project, 1, critique)
    assert critic_mod.next_step(critique, 0) == critic_mod.APPROVE  # Gate P: PASS, swarm approve may run
    plan_hash = critic_mod.plan_hash(plan_path)
    assert critic_mod.is_plan_approved_by_critic(conn, plan.project, plan_hash) is True

    publish_sha = controller.publish_plan(repo, "integration")  # "publishes ... and records that commit"
    controller.pin_gate_profiles(conn, plan.project, plan.gate_profiles, plan_mod.pinned_task_fields(plan))
    assert _git(repo, "rev-parse", "integration") == publish_sha
    in_publish = set(_git(repo, "ls-tree", "-r", "--name-only", publish_sha).splitlines())
    assert {"docs/ases/plan.json", *published_docs} <= in_publish  # architecture, contracts, decisions, plan.json
    for relative, text in published_docs.items():
        assert _git(repo, "show", f"{publish_sha}:{relative}") == text.strip()

    fake = FakeHermes(repo, board=BOARD, integration_branch="integration")
    fake.install(monkeypatch)
    assert fake.cards() == []  # [p402]: "only then does it create four implementation/merge cards" -- not yet
    fake.register_worker("lead", fw.ScriptedWorker([fw.Complete("nothing for the lead to do")]))
    fake.register_worker("reviewer", fw.reviewer_pass())
    guards.adopt_current_head(conn, plan.project, repo)

    return World(
        tmp_path=tmp_path, repo=repo, db_path=db_path, conn=conn, plan_raw=plan_raw, plan=plan, project=project,
        models_config=models_config, fake=fake, plan_sha=publish_sha, board=BOARD, project_id=PROJECT_ID,
    )


def _commit_subjects(world: World) -> list[str]:
    return world.git("log", "--format=%s", "integration").splitlines()


def _is_ancestor(world: World, ref: str, of: str) -> bool:
    """Whether `ref` is reachable from `of` (git merge-base --is-ancestor: exit 0 yes, 1 no, anything else is an
    error)."""
    result = subprocess.run(["git", "-C", str(world.repo), "merge-base", "--is-ancestor", ref, of])
    assert result.returncode in (0, 1), f"git merge-base --is-ancestor {ref} {of} exited {result.returncode}"
    return result.returncode == 0


def _refs_moved_only_by_fast_forward(world: World, moves: int) -> None:
    """The newest `moves` reflog entries of the integration branch are the merge queue's fast-forwards, and
    nothing else moved the branch after the plan was published."""
    entries = world.git("reflog", "show", "integration", "--format=%gs").splitlines()
    assert len(entries) == moves + 2, entries  # the seeded commit, the plan commit, then one fast-forward per merge
    assert all(entry.startswith("merge ") and entry.endswith("Fast-forward") for entry in entries[:moves]), entries


def test_22_2_end_to_end_two_tasks_from_an_empty_repository_through_gate_0_and_gate_p(tmp_path, monkeypatch):
    """[p402] in full, for the two-task path: empty repository, Gate 0, Gate P, publish, four cards, coder-1's
    worktree branched from that exact integration HEAD, the Reviewer completing the work card, the merge queue
    running Gate 3 on the candidate and fast-forwarding with one squash commit per task, both merge cards ending
    done. ASES-GIT-01, ASES-GIT-02, ASES-GIT-06, ASES-GIT-10, ASES-TSK-01, ASES-ARC-09, ASES-TSK-04."""
    world = _bootstrapped_world(tmp_path, monkeypatch)
    fake = world.fake
    fake.register_worker("coder-1", fw.by_task_key({
        "T1": fw.good_coder({"a.py": A_PY}, "add a.py"),
        "T2": fw.good_coder({"b.py": B_PY}, "add b.py"),
    }))

    pairs = world.create_cards()  # [p402]: "only then does it create four implementation/merge cards"
    t1, t2 = pairs["T1"], pairs["T2"]

    # Four cards, shaped as the blueprint says: a work card and a merge card per task, the second task's work
    # card waiting on the FIRST task's MERGE card (not its work card), and every merge card created blocked.
    assert len(fake.cards()) == 4
    assert fake.card(t1.work_card_id)["status"] == "ready"
    assert fake.card(t2.work_card_id)["status"] == "todo"
    assert fake.card(t2.work_card_id)["_parents"] == [t1.merge_card_id]
    assert fake.card(t1.merge_card_id)["_parents"] == [t1.work_card_id]
    assert fake.card(t1.merge_card_id)["status"] == fake.card(t2.merge_card_id)["status"] == "blocked"

    summaries = world.run_until(lambda w: w.all_merge_cards_done())

    assert summaries[-1]["finished"] is True
    assert all(summary["integrity"] == [] for summary in summaries)
    assert [fake.card(pair.merge_card_id)["status"] for pair in pairs.values()] == ["done", "done"]
    assert [fake.card(pair.work_card_id)["status"] for pair in pairs.values()] == ["done", "done"]

    # One squash commit per task, in dependency order, each naming its cards (ASES-GIT-06).
    assert _commit_subjects(world) == ["T2: add b", "T1: add a", "ASES: publish approved plan (Gate P)",
                                        "ASES: bootstrap an empty repository (ASES-GIT-10)"]
    for pair, sha in ((t1, world.git("rev-parse", "integration~1")), (t2, world.git("rev-parse", "integration"))):
        message = world.git("log", "-1", "--format=%B", sha)
        assert f"Work card: {pair.work_card_id}" in message and f"Merge card: {pair.merge_card_id}" in message
    assert world.git("show", "integration:a.py") == A_PY.strip()
    assert world.git("show", "integration:b.py") == B_PY.strip()

    # The integration branch only ever moved by the merge queue's fast-forward, and no worker commit is on it
    # (squash merge: the workers' own commits live on their branches only).
    _refs_moved_only_by_fast_forward(world, moves=2)
    for key in ("T1", "T2"):
        assert not _is_ancestor(world, f"swarm/{key}-coder", "integration"), (
            f"the worker branch of {key} is an ancestor of integration: it was not squashed")

    # ASES-GIT-01: each worktree was branched from the exact integration HEAD at the time its card was
    # dispatched: T1 from the published plan, T2 from T1's squash commit (T2 only ran after T1 was merged).
    assert world.git("rev-parse", "swarm/T1-coder~1") == world.plan_sha
    assert world.git("rev-parse", "swarm/T2-coder~1") == world.git("rev-parse", "integration~1")

    # The board and the ASES records agree about who did what: coder-1 handed off, the reviewer completed, and
    # the controller re-ran Gate 1 (ASES-REV-05) before trusting that hand-off: a red gate1 record for this
    # exact commit would have refused the merge (review.check_branch_for_merge), so a green merge proves the
    # re-run happened, and the gate_runs table names it directly.
    runs = fake.card(t1.work_card_id)["_runs"]
    assert [(run["profile"], run["outcome"]) for run in runs] == [("coder-1", "review_requested"), ("reviewer", "completed")]
    t1_commit = world.git("rev-parse", "swarm/T1-coder")
    assert runs[0]["metadata"]["commit_sha"] == t1_commit
    gate1_rows = world.conn.execute(
        "SELECT commit_sha, result FROM gate_runs WHERE task_key = 'T1' AND gate = 'gate1'").fetchall()
    assert [(r["commit_sha"], r["result"]) for r in gate1_rows] == [(t1_commit, "pass")]

    # [p402]: "the merge queue runs Gate 3 on the candidate": one green gate3 record per task, and the merge
    # record names the squash commit that actually landed.
    merge_rows = world.conn.execute(
        "SELECT task_key, gate3_result, squash_commit FROM merge_records WHERE task_key IN ('T1', 'T2') "
        "ORDER BY task_key").fetchall()
    assert [(r["task_key"], r["gate3_result"]) for r in merge_rows] == [("T1", "pass"), ("T2", "pass")]
    assert {r["squash_commit"] for r in merge_rows} == {world.git("rev-parse", "integration~1"),
                                                          world.git("rev-parse", "integration")}
    ingested = {row["task_key"] for row in world.conn.execute("SELECT task_key FROM usage_ingested")}
    assert "T1" in ingested  # the real usage ingest found the worker sessions the fake stamped into the runs


def test_22_2_a_worker_question_through_swarm_questions_and_swarm_answer_survives_a_controller_restart(
    tmp_path, monkeypatch, capsys,
):
    """[p402]: "One card asks a question, and swarm questions and swarm answer unblock it. Stop the controller
    once in the middle and confirm that state survives." ASES-REC-05.

    Driven through cli.cmd_questions and cli.cmd_answer themselves (not just the questions module the cores
    version of this test called directly), so the actual `swarm questions` / `swarm answer` entry points are
    exercised, not only the module behind them. Both commands call cli._load_project() and cli._open_conn(project),
    which are hardwired to a real checkout's config/swarm.yaml and ases_home (cli._repo_root() is
    pathlib.Path(__file__).resolve().parents[2] of cli.py itself) -- paths a temporary acceptance world has
    neither of -- so both are monkeypatched to return this world's own project and connection; cli._load_plan is
    left untouched, so Gate 0 genuinely re-parses the real plan.json this world wrote to disk."""
    world = _bootstrapped_world(tmp_path, monkeypatch)
    fake = world.fake
    fake.register_worker("coder-1", fw.by_task_key({
        "T1": fw.questioner(
            "Should add() accept floats as well as integers?", then=fw.good_coder({"a.py": A_PY}, "add a.py")),
        "T2": fw.good_coder({"b.py": B_PY}, "add b.py"),
    }))
    t1 = world.create_cards()["T1"]

    world.run_until(lambda w: w.card(t1.work_card_id)["status"] == "blocked")

    monkeypatch.setattr(cli_mod, "_load_project", lambda: world.project)
    monkeypatch.setattr(cli_mod, "_open_conn", lambda project: world.conn)

    exit_code = cli_mod.cmd_questions(argparse.Namespace(repo=str(world.repo)))
    assert exit_code == 0
    printed = capsys.readouterr().out
    assert "Should add() accept floats as well as integers?" in printed
    assert t1.work_card_id in printed

    # The controller is stopped here and started again: nothing that matters lives in the controller.
    world.restart_controller()
    passes_before = len(world.summaries)
    world.run_until(lambda w: len(w.summaries) >= passes_before + 3)
    assert fake.card(t1.work_card_id)["status"] == "blocked"  # still waiting for a person, however many passes go by

    answer_exit = cli_mod.cmd_answer(
        argparse.Namespace(card=t1.work_card_id, text="Yes, floats are fine.", author=None))
    assert answer_exit == 0
    capsys.readouterr()  # discard swarm answer's own output before the next command's assertion
    card = fake.card(t1.work_card_id)
    assert card["status"] == "ready"
    assert [c["body"] for c in card["_comments"] if c["author"] == "user"] == ["ANSWER: Yes, floats are fine."]

    exit_code_again = cli_mod.cmd_questions(argparse.Namespace(repo=str(world.repo)))
    assert exit_code_again == 0
    assert capsys.readouterr().out.strip() == "No open questions."

    world.run_until(lambda w: w.all_merge_cards_done())
    assert _commit_subjects(world)[:2] == ["T2: add b", "T1: add a"]
    assert [e["kind"] for e in fake.card(t1.work_card_id)["_events"]].count("unblocked") == 1


def test_22_2_merge_cards_that_are_only_waiting_are_not_open_questions(world, create_cards):
    """`swarm questions` must list what a person has to answer. A merge card is created blocked and simply
    waits for its work card: it asks nothing, and listing it would bury the real questions. (Moved verbatim from
    the removed test_scenarios_demo.py: unrelated to Gate 0/Gate P, so it stays on the plain world/create_cards
    fixtures.)"""
    create_cards(world)

    assert questions.list_questions(world.board, world.plan, conn=world.conn) == []
