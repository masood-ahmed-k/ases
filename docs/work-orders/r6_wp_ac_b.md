# Package AC-B: acceptance 22.5 (parallel) and 22.13 (kill switch)

Files you own: `tests/acceptance/test_22_5_parallel.py` (new), `tests/acceptance/test_22_13_kill_switch.py` (new). Nothing else. You
may NOT edit any file under `src/`, and may NOT edit `tests/acceptance/conftest.py` (use `world_factory` for a three-task plan). Read
`r2_rules.md`, `r5_rules.md`, `r6_rules.md` FIRST, then `tests/acceptance/test_scenarios_demo.py` in full and copy its style.

## 22.5, the parallel test (blueprint.txt around `[p407]`/`[p408]`)
"Three independent cards at once: separate worktrees, branches and profiles, separate port blocks and compose project names, no
cross-worktree changes in the integrity snapshots, and three sequential merges. A fourth card stays queued because of
kanban.max_in_progress."
Build a `world_factory` plan with FOUR independent coder tasks (no `depends_on` between them, non-overlapping `touches` so Gate 0 does
not serialize them) and THREE profiles (`coder-1`, `coder-2`, `coder-3` -- register a worker for each on the `FakeHermes`, read
`fake.register_worker` and `fake.max_in_progress`/`max_in_progress_per_profile` defaults; you will need FOUR cards but only three
profiles that can run at once, since `max_in_progress_per_profile` is 1 by default). Use `fw.Sleep(seconds)` (read its real behavior in
`worker.py`: it keeps a card `running` across passes) inside each worker's steps so all three can be observed `running`
SIMULTANEOUSLY before any of them finishes -- do one `world.one_pass()`, then assert exactly three cards are `running` and the fourth
is still `todo`/`ready`/queued (whichever `kanban_dispatch` leaves it as when `max_in_progress` is hit; read `FakeHermes`'s dispatch
logic or just assert the observed status, do not guess). Then let them finish (each worker's remaining steps write its own file(s) and
request review) and confirm:
- Each running card had its OWN worktree (`fake.worktree(card_id)`, three distinct real paths under `.worktrees/`), its own branch
  (read from the card dict, e.g. `branch_name`), and its own profile.
- `leases.py` (built in an earlier round; you do not own it, only USE it through the real controller) gave each card its own port
  block and compose project name: after `process_provision` has run for a card (a pass where it is `running`), read its
  `.env.ases` file inside `fake.worktree(card_id)` (a real file on disk, since the fake cuts real git worktrees) and assert the three
  cards' `ASES_PORT_BASE`/`COMPOSE_PROJECT_NAME` values are all different. If `.env.ases` has not been written yet by the pass you
  checked (leases.py's own note: "best effort... .env.ases can arrive a few seconds after the worker begins"), run one more pass before
  asserting, or check across the passes where the card was running.
- No cross-worktree changes: after all three finish, `guards.check_idle_worktrees` (read its real signature; it may need to be called
  directly or you may find `controller.process_idle_worktrees` already runs it every pass and records warnings) reports NOTHING about
  these three worktrees while they were running or after, i.e. no spurious `idle_worktree_changed` events for cards that were properly
  tracked as running throughout. (Package CORE this round does NOT touch idle-worktree code, so this should be stable; if you observe
  a false positive from the KNOWN gap the leases builder flagged -- "a card re-dispatched into the same worktree... looks like a change
  in an idle worktree" -- do not silently work around it: note it plainly in your report as a reproduction of that known issue, and
  decide whether to `xfail(strict=True, reason=...)` that one specific assertion with the citation, while keeping the rest of the
  scenario green.)
- Three sequential merges: `run_until(world, lambda w: <all four merge cards done>, max_passes=...)`, then assert (like the 22.2 demo
  does) that the integration branch moved by exactly four fast-forwards, in SOME order (dependency order does not constrain these four
  since none depend on each other, but each merge is still one at a time -- `mergeq`'s own serialization, read the existing "Serialized
  -- one merge_task call at a time" comment in `controller.process_merge_queue`), and every file from every task exists on
  `integration` at the end.

## 22.13, the kill switch test (blueprint.txt around `[p423]`/`[p424]`)
"With three cards running, swarm stop must leave no worker process, container or merge step running after 30 seconds, and swarm
resume must continue correctly."
`killswitch.stop_all` (read its real signature from `r2_rules.md`'s package K summary and the actual file) takes injectable
`kanban_list`/`kanban_show`/`reclaim` (point these at the SAME `FakeHermes` instance's methods, e.g. `kanban_list=lambda *a, **kw:
hermes.kanban_list(*a, **kw)` after `fake.install(monkeypatch)`, since `install` already replaced the `hermes` module's functions --
simplest is to NOT pass overrides for those three and let `stop_all`'s defaults (`hermes.pause`, `hermes.kanban_list`, etc.) resolve to
the now-faked `hermes` module) but takes REAL `pid_alive`/`killer`/`command_line` by default, which must NOT run against this
process's or the OS's real processes. Inject FAKES for those three (matching the style `test_killswitch.py` already uses -- read a few
of its tests for the exact fake shapes expected) that report every `FakeHermes` worker pid as alive-then-dead once "killed", since
`FakeHermes` worker pids are deliberately outside any real range and a real `pid_alive`/`terminate_tree` would just report them
unverified, which is not what this scenario is testing.
Build a plan with three independent coder tasks, three profiles, each worker using `fw.Sleep` so all three are `running`
simultaneously (same setup as 22.5's first half; you may copy the plan/worker shape, do not import between your two files, just repeat
the small amount of setup). Then:
1. Call `killswitch.stop_all(world.board, world.plan, conn=world.conn, killer=<your fake>, alive=<your fake>,
   command_line=<your fake that returns a string containing the card id, per killswitch's safety rule>)` and assert the returned
   `StopReport`: `flag_set` true, `paused` true, all three cards in `reclaimed`, all three worker pids in `killed`, `within_deadline`
   true (your fakes should resolve instantly, well under any real deadline), `unverified` empty (since your fakes always answer).
2. Assert `bounds.stop_requested(world.conn, world.plan.project)` (or whichever function package FIX leaves as the canonical one --
   read the CURRENT `killswitch.py`/`bounds.py` at the time you run, since package FIX may have already renamed something; if
   `killswitch.stop_requested` no longer exists, use whatever replaced it) is now true, and that a further `world.one_pass()` does
   NOTHING (no dispatch, no merge progress) while stopped -- assert the summary reflects a halted pass (read `run_pass`'s real
   contract from `r5_contracts.md`: `stopped`/`stop_reason` keys).
3. `killswitch.resume_all(world.board, world.plan, conn=world.conn, reconcile=<a callable that runs the real reconcile.reconcile(...,
   apply=True) and returns its report>)`: assert it clears the flag and returns `{"resumed": True}` (or the real shape, check it), then
   a further `world.one_pass()` makes progress again, and `run_until` gets all three (now-reclaimed-and-idle) cards re-dispatched and
   finished (since they were reclaimed, not completed, they should be picked up again from `ready`/`todo`, matching how a real
   reclaimed card behaves -- read the fake's own reclaim semantics, or `FakeHermes`'s `kanban_reclaim`, to know what status it leaves
   the card in).
"No... container running": since Docker is never started anywhere in this test suite, assert this the same way AC-A asserts "never
probes the provider" -- by construction (nothing here starts Docker), plus call `killswitch.default_list_containers` (or whatever the
real function is named) with a fake `runner` that would fail loudly if actually invoked with a real `docker` command, confirming
`stop_all`'s container-stop step used your injected fake and not a real one.

## Report back
The usual report, plus: whatever real signatures for `killswitch.stop_all`'s injectable parameters differed from what this order
assumed, and the exact fake shapes you used for `pid_alive`/`terminate_tree`/`process_command_line` (so AC-C, which also needs a
crash-and-restart scenario, can reuse the same pattern if useful -- do not import between your files, just keep the shapes similar).
