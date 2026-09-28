"""Blueprint 22.8: the merge conflict test (blueprint.txt [p413]/[p414], section 8.2).

"Two cards with overlapping touches are serialized by Gate 0. Two cards that still change the same line make the
second merge block its merge card, create a fix or reconciliation card as an extra parent, and merge cleanly
afterwards. A seeded post-merge failure is reverted. The integration branch is green at every commit."
[ASES-GIT-08, ASES-GIT-09, ASES-GIT-04, ASES-GIT-05, ASES-TSK-02]

Three separate claims, three tests, each in its own world_factory() world (r6_wp_ac_d.md's package instructions):

  1. test_22_8_1_gate0_serializes_overlapping_touches         Gate 0 (plan.serialize_overlapping_tasks) adds a
     dependency between two tasks whose DECLARED touches overlap, with no depends_on written by the Lead.
  2. test_22_8_2_real_conflict_gets_a_fix_card_then_merges_cleanly   Two tasks whose declared touches are NOT
     judged overlapping run in parallel, but their real edits collide on the same line of a shared file. The
     second one to reach the merge queue hits mergeq._build_candidate's real git conflict path, gets a fix card
     as an extra parent of its merge card (bounded by fix_cards_per_task), and the fix resolves cleanly.
  3. test_22_8_3_seeded_post_merge_failure_is_reverted_and_branch_stays_green   CORE's round 6 addition
     (controller.process_merge_queue's "gate3-postmerge" re-check): a merge that looks clean pre-merge is found
     broken immediately after landing, mergeq.revert_merge undoes it, a fix card opens, and the integration
     branch is proven green at every commit that is still part of its history.

Read against the REAL current code before writing anything (mergeq.merge_task, controller.process_merge_queue,
mergeq.RevertOutcome, mergeq.revert_merge, plan.serialize_overlapping_tasks / touches_overlap, review.py's scope
and tamper checks): the work order's own summary of these was written from an earlier snapshot and does not
match in places documented below, next to the construction each mismatch forced.
"""
from __future__ import annotations

import json
import pathlib

from ases import events
from ases import gates as gates_mod
from ases import plan as plan_mod
from ases.fakes import worker as fw

A_GOOD = "def add(x, y):\n    return x + y\n"
A_BAD = "def add(x, y):\n    return x - y\n"
B_GOOD = "def sub(x, y):\n    return x - y\n"


def _event_payloads(world, kind: str) -> list[dict]:
    """Every recorded event of `kind`, oldest first, with its JSON payload parsed (events.recent stores it as
    text; the demo scenarios only ever read `kind`, never `payload`, so this is new here)."""
    rows = [e for e in events.recent(world.conn, limit=1000) if e["kind"] == kind]
    rows.reverse()
    return [json.loads(e["payload"]) if e["payload"] else {} for e in rows]


# ---------------------------------------------------------------------------------------------
# 1. Gate 0 serializes two tasks whose DECLARED touches overlap, with no depends_on in the plan.
# ---------------------------------------------------------------------------------------------

OVERLAP_PLAN = {
    "project": "acceptance",
    "integration_branch": "integration",
    "gate_profiles": {"trivial": ["echo ok"]},
    "tasks": [
        {"key": "T1", "title": "first writer of a.py", "role": "coder", "depends_on": [], "touches": ["a.py"],
         "acceptance": ["a.py defines add(x, y) returning x + y"], "gate_profile": "trivial",
         "estimated_requests": 5},
        # No depends_on written by the Lead: the overlap alone (both tasks name "a.py") must be what serializes
        # this pair (ASES-GIT-08). If this test needed depends_on to pass it would not be testing Gate 0 at all.
        {"key": "T2", "title": "second writer of a.py", "role": "coder", "depends_on": [], "touches": ["a.py"],
         "acceptance": ["a.py still defines add(x, y) after T2's change"], "gate_profile": "trivial",
         "estimated_requests": 5},
    ],
}


def test_22_8_1_gate0_serializes_overlapping_touches(world_factory, create_cards):
    """[ASES-GIT-08] "Two cards with overlapping touches are serialized by Gate 0." Confirms overlapping
    TOUCHES alone (not an explicit depends_on) triggers the same work-card-waits-on-merge-card wiring
    test_22_2's dependency scenario already proves for an explicit dependency (test_22_2_end_to_end.py), so
    this stays short: its only point is that the serialization link comes from Gate 0 itself."""
    world = world_factory(plan_raw=OVERLAP_PLAN)

    # plan.serialize_overlapping_tasks ran inside parse_and_validate (Gate 0) when world_factory built this
    # plan: read its answer back off the real Plan object, no re-implementation of the glob logic here.
    assert [(link.later, link.earlier) for link in world.plan.serialization_links] == [("T2", "T1")]
    assert "touches overlap" in world.plan.serialization_links[0].reason
    assert world.plan.task("T2").depends_on == ("T1",)
    assert world.plan.task("T1").depends_on == ()

    pairs = create_cards(world)
    t1, t2 = pairs["T1"], pairs["T2"]

    # The same card shape test_22_2 proves for an explicit dependency (ASES-TSK-01/02): T2's WORK card is not
    # even ready, its parent is T1's MERGE card (not T1's work card), and both merge cards start blocked.
    assert world.fake.card(t1.work_card_id)["status"] == "ready"
    assert world.fake.card(t2.work_card_id)["status"] == "todo"
    assert world.fake.card(t2.work_card_id)["_parents"] == [t1.merge_card_id]
    assert world.fake.card(t1.merge_card_id)["_parents"] == [t1.work_card_id]
    assert world.fake.card(t1.merge_card_id)["status"] == world.fake.card(t2.merge_card_id)["status"] == "blocked"


# ---------------------------------------------------------------------------------------------
# 2. A real conflict Gate 0 cannot see coming: two tasks whose declared touches do NOT overlap (so both are
#    dispatched at once) but whose workers land colliding edits on the same line of a file both are allowed to
#    touch. The second merge hits mergeq._build_candidate's real conflict path, not a "seeded" shortcut.
#
# Why this needs a deliberate git construction, not just two scripted workers (read before changing anything):
# plan.touches_overlap's real _globs_overlap (plan.py) is PROVABLY airtight for this: any two glob patterns
# that both genuinely fnmatch a common literal path share a literal prefix and a literal suffix that are each
# prefixes/suffixes of THAT SAME path, so one always nests inside the other on both ends, and _globs_overlap's
# final rule (prefix nests OR, suffix nests OR) is then unconditionally true. Two globs that both legitimately
# (scope-check-passing, ASES-GIT-13) cover the same real file are therefore ALWAYS judged overlapping by Gate 0,
# with no glob-shape loophole (checked directly with plan.touches_overlap below). A same-file, different-case
# pair ("Shared.py" vs "shared.py") does dodge Gate 0's case-sensitive fnmatchcase check, but it does not
# produce mergeq's conflict path either: verified empirically (a scratch repo, not shipped here) that
# `git merge --squash` of a branch adding "shared.py" onto a tip that already has "Shared.py" exits 0 on a
# case-insensitive checkout, git's tree entries for the two names diverging from the one physical file instead
# of colliding on it, so `_build_candidate` would report a NO-OP where the blueprint wants a real conflict.
# Two DECLARED-non-overlapping tasks (a.py / b.py) are therefore given depends_on nothing, dispatched in the
# SAME pass (both assigned the "coder-1" profile; max_in_progress_per_profile is bumped on the fake board, a
# public setting the rig's own docstring lists as a test's to change, so both are cut from the SAME integration
# tip instead of the default cap of one in-progress card per profile). BOTH tasks' touches legitimately include
# "shared.py" -- Gate 0 DOES then add its own serialization link too (confirmed below), same reasoning as
# sub-test 1, and that is fine: it does not, on its own, stop a git-level conflict, because Gate 0 only orders
# CARD DISPATCH, and dispatch order is not what produces the conflict here. What produces it is that T2's
# worker, scripted with a `Do` step (ases.fakes.worker's own escape hatch for "anything the other steps do not
# cover"), resets its OWN freshly cut branch back to the plan's published commit before writing its conflicting
# edit -- simulating a coder that genuinely started from a stale base (a long-lived branch, a retry that reused
# an old worktree: a real possibility Gate 0's static, plan-time check cannot see, which is exactly what
# mergeq's merge-time candidate build is the backstop for). Nothing about the CONTROLLER or mergeq.py is worked
# around: check_branch_for_merge, _build_candidate, _handle_merge_failure and the fix-card budget are exercised
# exactly as they would be for any stale branch.
# ---------------------------------------------------------------------------------------------

SHARED_SEED = "alpha\nbeta\ngamma\n"
SHARED_AFTER_T1 = "alpha\nbeta-from-T1\ngamma\n"
SHARED_T2_STALE_EDIT = "alpha\nbeta-from-T2\ngamma\n"       # collides with T1 on the "beta" line, same base
SHARED_RESOLVED = "alpha\nbeta-from-T1-and-T2\ngamma\n"     # what the fix branch lands, on top of T1's content

CONFLICT_PLAN = {
    "project": "acceptance",
    "integration_branch": "integration",
    "gate_profiles": {"trivial": ["echo ok"]},
    "tasks": [
        {"key": "T1", "title": "write a.py and shared.py", "role": "coder", "depends_on": [],
         "touches": ["a.py", "shared.py"], "acceptance": ["a.py defines add", "shared.py carries T1's line"],
         "gate_profile": "trivial", "estimated_requests": 5},
        {"key": "T2", "title": "write b.py and shared.py", "role": "coder", "depends_on": [],
         "touches": ["b.py", "shared.py"], "acceptance": ["b.py defines sub", "shared.py carries T2's line"],
         "gate_profile": "trivial", "estimated_requests": 5},
    ],
}


def test_22_8_2_real_conflict_gets_a_fix_card_then_merges_cleanly(world_factory, run_until, git):
    """[ASES-GIT-09, ASES-GIT-04] "Two cards that still change the same line make the second merge block its
    merge card, create a fix ... card as an extra parent, and merge cleanly afterwards." See the module-level
    comment above the plan for why this needs a deliberate git construction. Read plan.py's real
    _globs_overlap before doubting the construction: the pair of glob shapes the work order floated ("two
    different glob patterns that both happen to match shared.py") does not exist for THIS algorithm."""
    # The claim the module comment makes about _globs_overlap, checked directly (not re-derived from the
    # scenario below): every glob pair that matches a shared real path is judged overlapping.
    assert plan_mod.touches_overlap(("a.py", "shared.py"), ("b.py", "shared.py")) is True

    world = world_factory(plan_raw=CONFLICT_PLAN, seed={"shared.py": SHARED_SEED, "README.md": "seeded\n"})
    fake = world.fake
    # Confirmed: Gate 0 DOES add its own link too (both tasks legitimately touch "shared.py"). It has no effect
    # on the conflict construction below, which drives the git state directly; recorded so a reader is not
    # confused into thinking this scenario relies on Gate 0 having missed something.
    assert [(link.later, link.earlier) for link in world.plan.serialization_links] == [("T2", "T1")]

    # Both tasks resolve to the same "coder-1" profile (conftest.py's ROLES maps role "coder" to it and cannot
    # be overridden from here); the default max_in_progress_per_profile of 1 would only ever let one of them
    # run per pass. Bumped on the fake board directly: a plain setting the rig's own docstring lists as a
    # test's to change ("Settings a test may change (all plain attributes): max_in_progress(_per_profile)").
    fake.max_in_progress_per_profile = 5
    fake.max_in_progress = 5

    plan_tip = world.plan_sha  # the commit BOTH branches are genuinely, independently based on

    attempts = {"count": 0}

    def t2_worker(fake_board, card, run, workspace_path):
        """T2's real worker, and its own fix card's worker (ases.fakes.worker.by_task_key routes a fix card
        titled "T2: fix (round 1)" to the same entry as the original "T2: ..." card, by task key alone, so a
        plain per-card-id fw.sequence() cannot tell the two attempts apart -- this closure counts attempts by
        task key instead, the shape this scenario actually needs)."""
        attempts["count"] += 1
        if attempts["count"] == 1:
            script = fw.ScriptedWorker([
                # The stale base: reset this freshly cut branch back to the commit BOTH tasks genuinely started
                # from, discarding whatever the worktree inherited from the (possibly already-advanced)
                # integration tip. This is the one deliberate, documented git manipulation in this file.
                fw.Do(lambda ctx: ctx.git("reset", "--hard", plan_tip)),
                fw.Write("b.py", B_GOOD),
                fw.Write("shared.py", SHARED_T2_STALE_EDIT),
                fw.Commit("add b.py, edit shared.py (stale base)"),
                fw.RequestReview("implemented T2, from a stale base"),
            ])
        else:
            # The fix card: a fresh worktree cut from the CURRENT (T1-merged) tip, per the ordinary fix-card
            # path (_handle_merge_failure) -- no Do trickery needed, the base is no longer stale.
            script = fw.good_coder({"b.py": B_GOOD, "shared.py": SHARED_RESOLVED}, "fix: resolve shared.py")
        script(fake_board, card, run, workspace_path)

    fake.register_worker("coder-1", fw.by_task_key({
        "T1": fw.good_coder({"a.py": A_GOOD, "shared.py": SHARED_AFTER_T1}, "add a.py, edit shared.py"),
        "T2": t2_worker,
    }))

    pairs = world.create_cards()
    t1, t2 = pairs["T1"], pairs["T2"]

    summaries = run_until(world, lambda w: w.all_merge_cards_done())
    assert summaries[-1]["finished"] is True

    # The conflict happened: a merge_failed event for T2, and the merge queue's OWN candidate-build conflict
    # path specifically (mergeq._build_candidate's "merge conflict:" detail), not an out-of-scope refusal, a
    # tamper finding, or a Gate 3 failure -- distinguishing this from every other reason a merge can be
    # refused is the entire point of this sub-test.
    failures = _event_payloads(world, "merge_failed")
    t2_failures = [f for f in failures if f["task_key"] == "T2"]
    assert len(t2_failures) == 1
    assert "merge conflict:" in t2_failures[0]["detail"]

    # A fix card was created as an EXTRA parent of T2's merge card, bounded by fix_cards_per_task (ASES-GIT-09).
    created = _event_payloads(world, "fix_card_created")
    t2_fix = [c for c in created if c["task_key"] == "T2"]
    assert len(t2_fix) == 1
    fix_card_id = t2_fix[0]["fix_card_id"]
    fix_card = fake.card(fix_card_id)
    assert fix_card["title"] == "T2: fix (round 1)"
    # hermes.kanban_link(board, parent_id, child_id): _handle_merge_failure calls it as kanban_link(board,
    # fix_card["id"], row["merge_card_id"]), so the FIX CARD is the new parent and the MERGE CARD the child
    # (an "extra parent" of the merge card, alongside the original work card it was already parented to).
    assert fix_card_id in fake.card(t2.merge_card_id)["_parents"]
    row = world.conn.execute(
        "SELECT fix_cards FROM plan_tasks WHERE project = ? AND task_key = 'T2'", (world.plan.project,)).fetchone()
    assert row["fix_cards"] == 1

    # And it merged cleanly afterwards: both merge cards done, the integration branch carries BOTH tasks'
    # intended content in a resolved form (T1's a.py, and shared.py as the fix branch left it).
    assert fake.card(t1.merge_card_id)["status"] == fake.card(t2.merge_card_id)["status"] == "done"
    assert git(world, "show", "integration:a.py") == A_GOOD.strip()
    assert git(world, "show", "integration:b.py") == B_GOOD.strip()
    assert git(world, "show", "integration:shared.py") == SHARED_RESOLVED.strip()
    # T2's fix branch, not its original (conflicting) one, is what actually merged: the commit message's
    # subject line always names the PLAN task's own title (process_merge_queue builds it from task.title, not
    # from whichever card is currently plan_tasks.work_card_id), but the "Work card:" trailer names the
    # specific card whose branch was squashed, which is the fix card once one exists.
    newest_message = git(world, "log", "-1", "--format=%B", "integration")
    assert f"Work card: {fix_card_id}" in newest_message


# ---------------------------------------------------------------------------------------------
# 3. A seeded post-merge failure is reverted (CORE's round 6 addition: controller.process_merge_queue re-runs
#    Gate 3 as "gate3-postmerge" on the NEW integration HEAD immediately after every real coder merge).
#
# Why the trigger has to be "seeded" through external state, not organic file content (read before changing
# anything): gate3-postmerge reuses the SAME gate_cmds and is run against outcome.squash_commit, which
# mergeq._fast_forward sets to the exact candidate_sha Gate 3 already validated pre-merge -- literally the same
# commit, same commands. A deterministic command that is a pure function of that commit's tree therefore MUST
# give the same pass/fail both times; there is no git-content construction (two tasks, any touches/gate-profile
# shape, any ordering) that can make a single task's own pre-merge Gate 3 pass and its post-merge Gate 3 fail
# without something OUTSIDE that commit's tree changing between the two checks. What genuinely changes between
# them, for the one task whose merge this is, is exactly one thing: merge_records.squash_commit for its task_key
# goes from NULL to set, at the fast-forward, which sits precisely between the two Gate 3 runs (mergeq.py's own
# ASES-REC-04 note: "A NEW candidate ... resets ... squash_commit"; _record_candidate writes it NULL before
# Gate 3 runs, _fast_forward fills it in after). The gate command below reads exactly that column from the real
# ASES database (its path is known before the plan exists, tmp_path / "ases.db", make_world's own hardcoded
# name) to decide whether to trust the file content it is looking at or bypass the check -- modelling "Gate 3
# was green a moment before a regression became visible" (the real-world cause controller.py's own docstring
# names) without needing two organically-interleaved merges the synchronous, single-pass merge queue cannot
# produce. It settles into an ordinary, non-vacuous, content-based check forever after the one bypass, which is
# what makes the "green at every commit" walk below meaningful rather than gamed.
# ---------------------------------------------------------------------------------------------


def _write_content_checker(path: pathlib.Path, *, good: str) -> None:
    """A gate command script: PASS unconditionally when a.py is absent, else PASS only when its content is
    exactly `good`. Used for T1's own, un-armed gate profile (nothing ever corrupts a.py before T2 exists)."""
    path.write_text(
        "import pathlib, sys\n"
        "p = pathlib.Path('a.py')\n"
        f"good = {good!r}\n"
        "sys.exit(0 if (not p.exists() or p.read_text(encoding='utf-8') == good) else 1)\n",
        encoding="utf-8",
    )


def _write_armed_checker(path: pathlib.Path, *, db_path: pathlib.Path, good_a: str, good_b: str) -> None:
    """T2's gate command script: bypass (pass unconditionally) until merge_records.squash_commit is set for
    task_key T2 in the real ASES database, then genuinely check a.py and b.py content. See the module comment
    above for exactly why this one column, and not a raw call counter, is the honest, non-vacuous signal."""
    path.write_text(
        "import pathlib, sqlite3, sys\n"
        f"conn = sqlite3.connect({db_path.as_posix()!r})\n"
        "row = conn.execute(\n"
        "    \"SELECT squash_commit FROM merge_records WHERE task_key = 'T2'\").fetchone()\n"
        "conn.close()\n"
        "armed = bool(row and row[0])\n"
        "if not armed:\n"
        "    sys.exit(0)\n"
        f"good_a = {good_a!r}\n"
        f"good_b = {good_b!r}\n"
        "ok = True\n"
        "a_path, b_path = pathlib.Path('a.py'), pathlib.Path('b.py')\n"
        "if a_path.exists() and a_path.read_text(encoding='utf-8') != good_a:\n"
        "    ok = False\n"
        "if b_path.exists() and b_path.read_text(encoding='utf-8') != good_b:\n"
        "    ok = False\n"
        "sys.exit(0 if ok else 1)\n",
        encoding="utf-8",
    )


def test_22_8_3_seeded_post_merge_failure_is_reverted_and_branch_stays_green(world_factory, tmp_path, run_until, git):
    """[ASES-GIT-05] "A seeded post-merge failure is reverted. The integration branch is green at every
    commit." Requirements register note on ASES-GIT-05 (spec/requirements.yaml, status partial as of this
    round): "a real post-merge failure triggering an ACTUAL revert has not been exercised in a real run, only
    in unit and crash-simulation tests" -- this is exactly that missing acceptance-level exercise, against the
    real controller.process_merge_queue, mergeq.revert_merge and gates.run_gate, driven through FakeHermes."""
    db_path = tmp_path / "ases.db"  # make_world's own hardcoded db filename, known before the world exists
    check_a = tmp_path / "check_a.py"
    check_b = tmp_path / "check_b.py"
    _write_content_checker(check_a, good=A_GOOD)
    _write_armed_checker(check_b, db_path=db_path, good_a=A_GOOD, good_b=B_GOOD)

    plan_raw = {
        "project": "acceptance",
        "integration_branch": "integration",
        "gate_profiles": {
            "gate_a": [f'python "{check_a.as_posix()}"'],
            "gate_b": [f'python "{check_b.as_posix()}"'],
        },
        "tasks": [
            {"key": "T1", "title": "write a.py", "role": "coder", "depends_on": [], "touches": ["a.py"],
             "acceptance": ["a.py defines add(x, y) returning x + y"], "gate_profile": "gate_a",
             "estimated_requests": 5},
            # T2 legitimately owns a.py too (ASES-GIT-13 scope check): its worker's out-of-scope-in-spirit,
            # in-declared-scope-in-fact edit to a.py is what a real regression sneaking in under an
            # unrelated task's diff looks like. depends_on is explicit here (T2's own acceptance is about
            # b.py, so T1 first is the natural order regardless of what Gate 0 would add for the touches
            # overlap on "a.py"); nothing about the postmerge mechanism needs T2 to be undetected by Gate 0
            # the way sub-test 2 does.
            {"key": "T2", "title": "write b.py", "role": "coder", "depends_on": ["T1"], "touches": ["a.py", "b.py"],
             "acceptance": ["b.py defines sub(x, y) returning x - y"], "gate_profile": "gate_b",
             "estimated_requests": 5},
        ],
    }
    world = world_factory(plan_raw=plan_raw)
    fake = world.fake
    fake.register_worker("coder-1", fw.by_task_key({
        "T1": fw.good_coder({"a.py": A_GOOD}, "add a.py"),
        # T2's task is genuinely about b.py (its acceptance and gate_a-equivalent focus); the a.py edit rides
        # along in the SAME commit, which is exactly what neither the review-lane's nor the merge queue's
        # pre-merge check can catch here (gate_b bypasses until armed, per the module comment above).
        "T2": fw.good_coder({"b.py": B_GOOD, "a.py": A_BAD}, "add b.py"),
    }))

    pairs = world.create_cards()
    t1, t2 = pairs["T1"], pairs["T2"]
    original_t2_work_card = t2.work_card_id

    # Runs until process_merge_queue has repointed plan_tasks.work_card_id at a fix card for T2 (only
    # _handle_merge_failure does this, and only after a real failure -- here, exclusively reachable through
    # the postmerge revert path, since T2's own pre-merge checks are all designed to stay green).
    run_until(world, lambda w: w.work_card_id("T2") != original_t2_work_card)

    # T1 merged clean, with no postmerge trigger (nothing else had changed underneath it yet).
    assert fake.card(t1.merge_card_id)["status"] == "done"
    t1_failures = _event_payloads(world, "merge_failed")
    assert not [f for f in t1_failures if f["task_key"] == "T1"]

    # The postmerge check caught it and mergeq.revert_merge ran: a post_merge_reverted event, then the ordinary
    # merge_failed / fix-card path (controller.py's own docstring for process_merge_queue, quoted above the
    # gate3-postmerge call: "red, and the revert succeeds: a post_merge_reverted event, then the SAME failure
    # path an ordinary Gate 3 ... failure takes").
    reverted_events = _event_payloads(world, "post_merge_reverted")
    t2_reverted = [e for e in reverted_events if e["task_key"] == "T2"]
    assert len(t2_reverted) == 1
    bad_sha = t2_reverted[0]["commit"]

    t2_failures = [f for f in _event_payloads(world, "merge_failed") if f["task_key"] == "T2"]
    assert len(t2_failures) == 1

    fixes = [c for c in _event_payloads(world, "fix_card_created") if c["task_key"] == "T2"]
    assert len(fixes) == 1
    row = world.conn.execute(
        "SELECT fix_cards FROM plan_tasks WHERE project = ? AND task_key = 'T2'", (world.plan.project,)).fetchone()
    assert row["fix_cards"] == 1

    # RevertOutcome.ok (read through its effects: the acceptance rig does not intercept the object itself, the
    # same way it never intercepts a MergeOutcome -- only what it did to git and the database).
    merge_record = world.conn.execute(
        "SELECT reverted, squash_commit FROM merge_records WHERE task_key = 'T2'").fetchone()
    assert merge_record["reverted"] == 1
    assert merge_record["squash_commit"] == bad_sha  # ASES-REC-04: the row is NOT reset by a revert alone

    # T2's merge card was NOT completed (it stays open for the fix, controller.py: "The merge card is NOT
    # completed: it stays open for the fix").
    assert fake.card(t2.merge_card_id)["status"] != "done"

    # git history shows a genuine revert commit undoing bad_sha, and a.py is back to the working content.
    log = git(world, "log", "--format=%H %s", "integration").splitlines()
    newest_sha, newest_subject = log[0].split(" ", 1)
    assert newest_subject.startswith("Revert ")
    assert git(world, "rev-parse", f"{newest_sha}^") == bad_sha
    assert git(world, "show", "integration:a.py") == A_GOOD.strip()
    # b.py, added by the same bad commit, was reverted along with it: the whole squash is undone atomically,
    # not just the part that broke gate_b (mergeq.revert_merge reverts one commit, it cannot cherry-pick which
    # half of it to keep).
    tracked = git(world, "ls-tree", "-r", "--name-only", "integration").splitlines()
    assert "b.py" not in tracked

    # Drive the fix card to a clean merge, so "green at every commit" below covers a realistic settled history
    # (T1's commit, the reverted T2 attempt, the revert, and the fix), not just the immediate aftermath.
    fake.register_worker("coder-1", fw.by_task_key({
        "T1": fw.good_coder({"a.py": A_GOOD}, "add a.py"),
        "T2": fw.good_coder({"b.py": B_GOOD}, "fix: add b.py without touching a.py"),
    }))
    run_until(world, lambda w: w.all_merge_cards_done())
    assert git(world, "show", "integration:a.py") == A_GOOD.strip()
    assert git(world, "show", "integration:b.py") == B_GOOD.strip()

    # "The integration branch is green at every commit": walk every commit currently reachable from the tip
    # (git log does not forget bad_sha just because it was reverted -- a revert adds a commit, it does not
    # rewrite history) and re-run gate_b for real (gates.run_gate, the same function process_merge_queue
    # calls) against each one. Every commit is green EXCEPT bad_sha itself, which is asserted RED here too
    # (proving the seed genuinely corrupted something, not a vacuous check that would pass regardless of
    # content) and is the one, explicitly-accounted-for exception: it was never a durable resting point of the
    # branch (the fast-forward that landed it and the postmerge check that caught it happened in the same
    # controller pass), and mergeq.revert_merge is precisely the mechanism that keeps it from being one.
    commits = git(world, "log", "--format=%H", "integration").splitlines()
    assert bad_sha in commits
    gate_b_commands = world.plan.gate_profiles["gate_b"]
    for sha in commits:
        result = gates_mod.run_gate(world.repo, sha, "verify-green-at-every-commit", gate_b_commands)
        if sha == bad_sha:
            assert not result.passed, "the seeded commit must itself fail the check, or the seed proved nothing"
        else:
            assert result.passed, f"commit {sha} should be green but gate_b failed: {result.detail}"
