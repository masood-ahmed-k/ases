# Package AC-D: acceptance 22.8 (merge conflict, now unblocked by CORE's revert wiring)

Files you own: `tests/acceptance/test_22_8_merge_conflict.py` (new). Nothing else. You may NOT edit any file under `src/`, and may NOT
edit `tests/acceptance/conftest.py`. Read `r2_rules.md`, `r5_rules.md`, `r6_rules.md` FIRST, then `tests/acceptance/
test_scenarios_demo.py` in full and copy its style. This package was DEFERRED from the first round 6 wave because it needs the
post-merge revert trigger CORE built; that work has now landed (`mergeq.merge_task`, `controller.process_merge_queue`,
`mergeq.RevertOutcome`, `mergeq.revert_merge`) -- read all four for real before writing anything, do not rely on this file's summary
of them (see below).

## Requirement (blueprint.txt around `[p413]`/`[p414]`, section 22.8)
"Two cards with overlapping touches are serialized by Gate 0. Two cards that still change the same line make the second merge block
its merge card, create a fix or reconciliation card as an extra parent, and merge cleanly afterwards. A seeded post-merge failure is
reverted. The integration branch is green at every commit."
Three separate claims, build one test per claim (plus a shared setup helper), each in its own `world_factory()` world:

## 1. Gate 0 serializes overlapping touches
Build a plan with TWO coder tasks whose `touches` genuinely overlap (e.g. both name `a.py`, or one names `a.py` and the other names
`a.*`/`a.py` through a glob) and NO `depends_on` between them. `plan.parse_and_validate`/`load_plan_file` already does this at Gate 0
(read `plan.py`'s real `serialize_overlapping_tasks`/`Plan.serialization_links`, built well before this round): assert the parsed
`Plan` records a serialization link between the two tasks (the later one in plan order depending on the earlier), and that
`create_cards_from_plan` then creates the SECOND task's work card with the first task's MERGE card as an extra parent, so it does not
even become `ready` until the first one's merge lands -- drive this through `world.create_cards()` and assert the card statuses
directly (`todo` vs `ready`), the same way `test_scenarios_demo.py`'s 22.2 test already asserts T2 waits on T1's merge card. This
sub-test does not need much beyond what 22.2 already proves for dependency ordering; keep it short, its point is confirming
overlapping TOUCHES (not an explicit `depends_on`) triggers the same serialization.

## 2. Two cards that still change the same line: the second merge blocks, gets a fix card, then merges cleanly
This needs touches that do NOT overlap (so Gate 0 does NOT serialize them and both cards run in parallel) but whose real file
changes DO collide on the same line -- the scenario the blueprint is actually testing is a MERGE CONFLICT Gate 0 cannot see coming
(two tasks legitimately allowed to touch the same broad area, e.g. both touching `shared.py` via a shared but non-overlapping glob
is contradictory; instead, give BOTH tasks touches that include `shared.py` explicitly -- Gate 0 WILL then serialize them by the
overlap rule above, which is not what this sub-test wants. Read `plan.touches_overlap`'s real semantics for the exact glob shapes
that do NOT count as overlapping while still letting two real edits land on the same file/line -- for example two DIFFERENT glob
patterns that both happen to match `shared.py` by coincidence of what exists on disk, if `touches_overlap`'s matching is glob-text
based rather than filesystem based (read it to find out), or simply give the SECOND task `depends_on: [T1]` (an explicit
dependency, not touches-based) with touches that do not literally overlap T1's, but whose worker is scripted to ALSO edit
`shared.py` (a file its `touches` glob still covers, e.g. `touches: ["shared.py", "b.py"]` for both). Whichever shape you use,
document in a comment exactly why Gate 0 did NOT serialize them, so a reader is not confused later.
Register two coder workers who BOTH write conflicting content to the same line of `shared.py` (start the seed file with a stable
base commit both branch from). Run until the FIRST task's card merges cleanly (it becomes the new integration tip). Then the SECOND
task's merge attempt hits `mergeq._build_candidate`'s real conflict path (`git merge --squash` returns non-zero, `git merge --abort`
runs, the outcome is `merged=False` with a "merge conflict:" detail) -- assert `controller.process_merge_queue` takes the ordinary
failure path: the merge card stays open/blocked, a FIX CARD is created as an EXTRA PARENT of the merge card (read the real event
name, likely `fix_card_created`, and the real parent-linking call, `kanban_link`), bounded by `fix_cards_per_task`. Then give the fix
card's worker a NON-conflicting edit (it resolves the conflict by writing compatible content, since `ScriptedWorker` cannot actually
run `git merge` interactively -- it just needs to produce a branch whose squash onto the NEW integration tip does not conflict) and
assert the fix card's branch merges cleanly, the merge card completes, and the integration branch has BOTH tasks' intended content in
some resolved form.

## 3. A seeded post-merge failure is reverted
This is what CORE's round 6 work specifically added: `controller.process_merge_queue` now re-runs Gate 3 as `"gate3-postmerge"` on
the NEW integration HEAD immediately after every real (non-no-op) coder merge. Read the real code (`controller.py`, search for
`gate3-postmerge` and the surrounding logic) to get the exact mechanism right before writing this test -- do not guess from this
summary. You need a scenario where task A's own Gate 3 is GREEN in isolation, but task A's merge, once it lands, breaks something
task B's (or the project's) gate profile would catch -- since a single task's own pre-merge Gate 3 only runs the ONE gate profile
declared for that task, seed a project where BOTH tasks share the SAME gate profile name and that gate profile's command checks
something like "both `a.py` and `b.py` import cleanly" or "a combined test file that only exists after task B's edit passes" -- the
simplest reliable construction: task A's pre-merge Gate 3 runs a command that only checks `a.py` in isolation (passes), but the
POST-merge Gate 3 (re-run on the new integration HEAD, same commands) ALSO happens to now see a file task A's own commit did not
touch but which the SEEDED repository state makes newly relevant (for example, seed the repository with a THIRD file,
`combined_check.py`, that task A's own commit inadvertently breaks only once merged onto a specific base -- the cleanest way to force
this deterministically is to have task A's `ScriptedWorker` itself write TWO files: the one its touches legitimately covers, AND
(if `touches_coder`/`Write` lets you target any path within the worktree) modify a shared "integration invariant" file in a way
that passes the FOCUSED pre-merge gate (which only greps for the task's own file) but fails a BROADER post-merge check -- if your
plan's gate profile is defined identically for pre- and post-merge (it is: `process_merge_queue` reuses `gate_cmds` for the postmerge
call, read the real code to confirm), the postmerge check can only differ from the premerge one by what ELSE changed on the
integration branch between them -- so the REALISTIC construction is: task A's own gate profile is narrow (checks only `a.py`); merge
task A (clean, no postmerge trigger since nothing else has changed underneath it yet); THEN get task B's card to `done` with a PASS
verdict but have its WORKER (not its gate profile) secretly ALSO corrupt `a.py` as a side effect of writing `b.py` (a`Write` step
targeting `a.py` with broken content, even though `b.py` is what task B's touches/acceptance criteria are about -- this is exactly
"a seeded post-merge failure": an out-of-scope edit that task B's OWN pre-merge Gate 3, which only runs B's gate profile checking
`b.py`, never catches, but which breaks `a.py`). When task B's merge lands, the postmerge re-check (running B's gate profile, which
per the plan ALSO needs to include an `a.py` check for this to actually fail -- so give BOTH tasks the SAME gate profile, one that
checks both files) is RED. Assert: `mergeq.revert_merge` is called, `RevertOutcome.ok` is True (read its real fields), the squash
commit is genuinely reverted (git history shows a revert commit, and `git show integration:a.py` is back to the working content),
a `post_merge_reverted` event (or whatever the real event kind is, read it) is recorded, the merge card stays open (not completed as
done), a fix card is opened bounded by the budget, and the integration branch was green at every commit you can check with `git log
--format=%H` walking each commit and re-running the gate profile against it (this is the literal "green at every commit" claim --
prove it by actually re-running the check against each commit, not by assuming it).

## Report back
The usual report, plus: the EXACT construction you used to force a real, deterministic post-merge failure (this is the hardest part
of the whole package -- if you found a cleaner or more realistic way than the one sketched above, describe it precisely so it is
reusable), and whether `mergeq.merge_task`'s real signature/behavior for `should_stop`/`project`/the postmerge trigger differed from
what this work order assumed (it was written by reading CORE's code after the fact, but confirm against the actual current tree,
which may have shifted if a later fix landed).
