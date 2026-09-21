"""Two demonstration scenarios that prove the acceptance rig (blueprint 22.2 and 22.6, cores only).

Each one drives the REAL controller (controller.run_pass, review, mergeq, guards, gates, usage, questions, recovery)
against ases.fakes.board.FakeHermes, with real git worktrees and scripted workers. The scenarios themselves come in a
later round; these exist to show the rig can carry them, fast and with no quota.
"""
import subprocess

from ases import events, questions, recovery
from ases.fakes import worker as fw

A_PY = "def add(x, y):\n    return x + y\n"
A_WRONG = "def add(x, y):\n    return x - y\n"
B_PY = "def sub(x, y):\n    return x - y\n"


def _commit_subjects(git, world) -> list[str]:
    return git(world, "log", "--format=%s", "integration").splitlines()


def _is_ancestor(world, ref: str, of: str) -> bool:
    """Whether `ref` is reachable from `of` (git merge-base --is-ancestor: exit 0 yes, 1 no, anything else is an error)."""
    result = subprocess.run(["git", "-C", str(world.repo), "merge-base", "--is-ancestor", ref, of])
    assert result.returncode in (0, 1), f"git merge-base --is-ancestor {ref} {of} exited {result.returncode}"
    return result.returncode == 0


def _refs_moved_only_by_fast_forward(git, world, moves: int) -> None:
    """The newest `moves` reflog entries of the integration branch are the merge queue's fast-forwards, and nothing else
    moved the branch after the plan was published."""
    entries = git(world, "reflog", "show", "integration", "--format=%gs").splitlines()
    assert len(entries) == moves + 2, entries  # the seeded commit, the plan commit, then one fast-forward per merge
    assert all(entry.startswith("merge ") and entry.endswith("Fast-forward") for entry in entries[:moves]), entries


# ---------------------------------------------------------------------------------------------
# Acceptance 22.2, core: two tasks, T2 depends on T1, coders write files, the reviewer passes, both merge cards end done
# ---------------------------------------------------------------------------------------------


def test_22_2_two_tasks_merge_in_dependency_order_one_squash_commit_each(world, create_cards, run_until, git):
    """Blueprint 22.2: coder-1 changes code in a worktree branched from that exact integration HEAD, the controller re-runs
    Gate 1, the Reviewer completes the work card, the merge queue runs Gate 3 on the candidate and fast-forwards with one
    squash commit per task, and both merge cards end as done. ASES-GIT-01, ASES-GIT-02, ASES-GIT-06, ASES-TSK-01/02."""
    fake = world.fake
    fake.register_worker("coder-1", fw.by_task_key({
        "T1": fw.good_coder({"a.py": A_PY}, "add a.py"),
        "T2": fw.good_coder({"b.py": B_PY}, "add b.py"),
    }))

    pairs = create_cards(world)
    t1, t2 = pairs["T1"], pairs["T2"]

    # Four cards, shaped as the blueprint says: a work card and a merge card per task, the second task's work card
    # waiting on the FIRST task's MERGE card (not its work card), and every merge card created blocked.
    assert len(fake.cards()) == 4
    assert fake.card(t1.work_card_id)["status"] == "ready"
    assert fake.card(t2.work_card_id)["status"] == "todo"
    assert fake.card(t2.work_card_id)["_parents"] == [t1.merge_card_id]
    assert fake.card(t1.merge_card_id)["_parents"] == [t1.work_card_id]
    assert fake.card(t1.merge_card_id)["status"] == fake.card(t2.merge_card_id)["status"] == "blocked"

    summaries = run_until(world, lambda w: w.all_merge_cards_done())

    assert summaries[-1]["finished"] is True
    assert all(summary["integrity"] == [] for summary in summaries)
    assert [fake.card(pair.merge_card_id)["status"] for pair in pairs.values()] == ["done", "done"]
    assert [fake.card(pair.work_card_id)["status"] for pair in pairs.values()] == ["done", "done"]

    # One squash commit per task, in dependency order, each naming its cards (ASES-GIT-06).
    assert _commit_subjects(git, world) == ["T2: add b", "T1: add a", "ASES: publish approved plan (Gate P)", "init"]
    for pair, sha in ((t1, git(world, "rev-parse", "integration~1")), (t2, git(world, "rev-parse", "integration"))):
        message = git(world, "log", "-1", "--format=%B", sha)
        assert f"Work card: {pair.work_card_id}" in message and f"Merge card: {pair.merge_card_id}" in message
    assert git(world, "show", "integration:a.py") == A_PY.strip() and git(world, "show", "integration:b.py") == B_PY.strip()

    # The integration branch only ever moved by the merge queue's fast-forward, and no worker commit is on it (squash
    # merge: the workers' own commits live on their branches only).
    _refs_moved_only_by_fast_forward(git, world, moves=2)
    for key in ("T1", "T2"):
        assert not _is_ancestor(world, f"swarm/{key}-coder", "integration"), (
            f"the worker branch of {key} is an ancestor of integration: it was not squashed")

    # ASES-GIT-01: each worktree was branched from the exact integration HEAD at the time its card was dispatched: T1
    # from the published plan, T2 from T1's squash commit (T2 only ran after T1 was merged).
    assert git(world, "rev-parse", "swarm/T1-coder~1") == world.plan_sha
    assert git(world, "rev-parse", "swarm/T2-coder~1") == git(world, "rev-parse", "integration~1")

    # The board and the ASES records agree about who did what: coder-1 handed off, the reviewer completed.
    runs = fake.card(t1.work_card_id)["_runs"]
    assert [(run["profile"], run["outcome"]) for run in runs] == [("coder-1", "review_requested"), ("reviewer", "completed")]
    assert fake.card(t1.work_card_id)["_runs"][0]["metadata"]["commit_sha"] == git(world, "rev-parse", "swarm/T1-coder")
    assert world.conn.execute("SELECT COUNT(*) FROM merge_records WHERE squash_commit IS NOT NULL").fetchone()[0] == 2
    ingested = {row["task_key"] for row in world.conn.execute("SELECT task_key FROM usage_ingested")}
    assert "T1" in ingested  # the real usage ingest found the worker sessions the fake stamped into the runs


def test_22_2_a_worker_question_is_listed_answered_and_unblocks_the_card(world, create_cards, run_until, git):
    """Blueprint 22.2: "One card asks a question, and swarm questions and swarm answer unblock it. Stop the controller once
    in the middle and confirm that state survives." ASES-REC-05."""
    fake = world.fake
    fake.register_worker("coder-1", fw.by_task_key({
        "T1": fw.questioner("Should add() accept floats as well as integers?", then=fw.good_coder({"a.py": A_PY}, "add a.py")),
        "T2": fw.good_coder({"b.py": B_PY}, "add b.py"),
    }))
    t1 = create_cards(world)["T1"]

    run_until(world, lambda w: w.card(t1.work_card_id)["status"] == "blocked")

    asked = [q for q in questions.list_questions(world.board, world.plan, conn=world.conn) if q.card_id == t1.work_card_id]
    assert len(asked) == 1
    assert (asked[0].task_key, asked[0].card_kind, asked[0].assignee) == ("T1", "work", "coder-1")
    assert asked[0].question == "Should add() accept floats as well as integers?"

    # The controller is stopped here and started again: nothing that matters lives in the controller.
    world.restart_controller()
    passes_before = len(world.summaries)
    run_until(world, lambda w: len(w.summaries) >= passes_before + 3)
    assert fake.card(t1.work_card_id)["status"] == "blocked"  # still waiting for a person, however many passes go by

    answered = questions.answer_question(world.board, t1.work_card_id, "Yes, floats are fine.", conn=world.conn)
    assert answered.question == asked[0].question
    card = fake.card(t1.work_card_id)
    assert card["status"] == "ready"
    assert [c["body"] for c in card["_comments"] if c["author"] == "user"] == ["ANSWER: Yes, floats are fine."]
    assert not [q for q in questions.list_questions(world.board, world.plan, conn=world.conn) if q.card_id == t1.work_card_id]

    run_until(world, lambda w: w.all_merge_cards_done())
    assert _commit_subjects(git, world) == ["T2: add b", "T1: add a", "ASES: publish approved plan (Gate P)", "init"]
    assert [e["kind"] for e in fake.card(t1.work_card_id)["_events"]].count("unblocked") == 1


def test_22_2_merge_cards_that_are_only_waiting_are_not_open_questions(world, create_cards):
    """`swarm questions` must list what a person has to answer. A merge card is created blocked and simply waits for its work
    card: it asks nothing, and listing it would bury the real questions."""
    create_cards(world)

    assert questions.list_questions(world.board, world.plan, conn=world.conn) == []


# ---------------------------------------------------------------------------------------------
# Acceptance 22.6, core: the reviewer returns changes once, the card goes back to its implementer, only the corrected
# commit merges
# ---------------------------------------------------------------------------------------------


def test_22_6_reviewer_returns_changes_once_and_only_the_corrected_commit_merges(
    world_factory, one_task_plan, run_until, git,
):
    """Blueprint 22.6: "The fake worker violates an acceptance criterion. The Reviewer must return CHANGES_REQUIRED ..., the
    card must go back to its implementer, and only the corrected commit may merge." ASES-REV-05/06, ASES-GIT-03."""
    world = world_factory(plan_raw=one_task_plan)
    fake = world.fake
    fake.register_worker("coder-1", fw.sequence(
        fw.wrong_coder({"a.py": A_WRONG}, "add a.py"),
        fw.good_coder({"a.py": A_PY}, "fix add: return x + y"),
    ))
    fake.register_worker("reviewer", fw.sequence(
        fw.reviewer_changes(["add(x, y) must return x + y, not x - y"]),
        fw.reviewer_pass(),
    ))
    t1 = world.create_cards()["T1"]
    work = t1.work_card_id

    # The wrong commit is reviewed and sent back to its implementer, not merged.
    run_until(world, lambda w: any(e["kind"] == "changes_requested" for e in w.card(work)["_events"]))
    card = world.card(work)
    assert (card["status"], card["assignee"]) == ("ready", "coder-1")
    (changes,) = [e for e in card["_events"] if e["kind"] == "changes_requested"]
    assert "x + y" in changes["payload"]["reason"] and changes["payload"]["implementer"] == "coder-1"
    wrong_commit = git(world, "rev-parse", "swarm/T1-coder")
    assert git(world, "show", f"{wrong_commit}:a.py") == A_WRONG.strip()
    assert world.card(t1.merge_card_id)["status"] == "blocked"
    assert git(world, "rev-parse", "integration") == world.plan_sha  # nothing merged yet

    # The implementer corrects it, the reviewer passes the new commit, and that is what merges.
    run_until(world, lambda w: w.all_merge_cards_done())
    corrected_commit = git(world, "rev-parse", "swarm/T1-coder")
    assert corrected_commit != wrong_commit

    card = world.card(work)
    assert [(r["profile"], r["outcome"]) for r in card["_runs"]] == [
        ("coder-1", "review_requested"), ("reviewer", "changes_requested"),
        ("coder-1", "review_requested"), ("reviewer", "completed"),
    ]
    assert git(world, "show", "integration:a.py") == A_PY.strip()
    assert "x - y" not in git(world, "log", "-p", "integration", "--", "a.py")
    assert _commit_subjects(git, world)[:2] == ["T1: add a", "ASES: publish approved plan (Gate P)"]
    assert git(world, "rev-parse", "integration~1") == world.plan_sha  # one commit landed on top of the plan

    # The approval that counted names the corrected commit, and only that one was recorded (ASES-GIT-03: a verdict
    # belongs to one commit).
    verdicts = world.conn.execute(
        "SELECT commit_sha, outcome, reviewer_profile FROM review_verdicts WHERE task_key = 'T1'").fetchall()
    assert [(v["commit_sha"], v["outcome"], v["reviewer_profile"]) for v in verdicts] == [
        (corrected_commit, "PASS", "reviewer")]
    assert fake.card(work)["_runs"][-1]["metadata"]["commit"] == corrected_commit  # the verdict named the corrected commit

    # The controller never used its own send-back (Gate 1 stayed green); the reviewer's request is what counted as a
    # review round, and the recovery module reads it off the events the fake wrote.
    assert not [call for call in fake.calls if call.name == "kanban_reopen_review"]
    recovery.refresh_review_rounds(world.board, world.plan, conn=world.conn)  # idempotent: only counts what is new
    assert recovery.load_lineage(world.conn, world.plan.project, "T1").review_rounds == 1
    kinds = {e["kind"] for e in events.recent(world.conn, limit=500)}
    assert "gate1_recheck_failed" not in kinds and "merged" in kinds
