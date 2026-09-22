# Package ROLES2 (wave 2, dispatch only after FIXES and POLICY have both landed): greenfield bootstrapping and the Tester role's
missing mechanics

Files you own: `src/ases/controller.py`, `src/ases/cli.py`, `src/ases/plan.py`, `tests/unit/test_controller.py`,
`tests/unit/test_controller_loop.py`, `tests/unit/test_cli_commands.py`, `tests/unit/test_plan.py`. Nothing else. Read `r2_rules.md`,
`r5_rules.md`, `r6_rules.md`, `r7_rules.md` FIRST. **Do not start this package until you have confirmed, by reading
`git log -3` and `git status`, that packages FIXES and POLICY have both already committed their round 7 changes to `controller.py`
and `cli.py`** -- you are editing the SAME two files those packages just changed, and must build on their real, current code, not a
stale copy. If either has not landed yet, stop and report that you are blocked, rather than editing an older version of these files.

## Part A: ASES-GIT-10 (section 8.3): "git worktree add needs at least one commit. swarm run MUST create an initial commit and the
integration branch when the repository is empty."
Read `controller.publish_plan` (it currently REQUIRES the repo to already be checked out on `integration_branch`, confirmed by
reading it: `git branch --show-current` must already equal it, or it raises) and `cli.cmd_plan`/`cmd_approve`/`cmd_run`'s real
current preflight steps (they may have changed slightly under FIXES/POLICY; read them fresh). Design and build a single, reusable
function, `controller.ensure_repo_bootstrapped(repo: pathlib.Path, integration_branch: str) -> bool` (returns True when it had to
create anything, False when the repo already had commits and the right branch), that:
1. Detects a truly empty repository: no `.git` directory at all, OR a `.git` directory with zero commits (`git rev-parse HEAD`
   fails). Never touches a repo that already has history, even if it is on the wrong branch (that stays `publish_plan`'s existing
   refusal, unchanged).
2. When empty: `git init` if needed (never with `-q` swallowing a real failure silently; check the exit code), create the
   `integration_branch` as the initial branch (`git init -b <name>` when `.git` does not exist yet; `git checkout -b <name>` or
   `git symbolic-ref HEAD refs/heads/<name>` if `.git` exists but is unborn -- read which is correct for a genuinely fresh
   `.git init` with zero commits, since there is nothing to check out yet), and make exactly ONE initial commit. Content of that
   commit: at minimum a `.gitignore` and a one-line `README.md` saying the repository was bootstrapped by ASES on this date for this
   project (read `docs/architecture.md`'s own established prose style and match it, ASCII only) -- never invent a `pyproject.toml`
   or similar here, that is the SCAFFOLD TASK's job (part B), not this bootstrap step's.
3. Never touches git config (the standing hard rule: no `git config user.name`/`user.email` writes -- if a commit needs an identity
   and none is set, use `git -c user.name=... -c user.email=...` scoped to that one command only, exactly the pattern already used
   elsewhere in this codebase for throwaway repos; read `mergeq.py`/`gates.py` for the exact style already established, match it
   precisely, never a persistent `git config` write).
4. Never raises for an ordinary git failure; returns a clear result the caller can act on, and record an `events.record(conn, ...)`
   entry (kind `repo_bootstrapped`) when it actually created something, so the release report / operations log has a trace of it.
Wire this into whichever of `cmd_plan`/`cmd_approve` is the earliest real touch-point on the repository (read the current
`cli.py` flow fresh; call it once, in the right place, not from multiple commands redundantly) -- `swarm run`'s own primary-checkout
guard already refuses a repo with no commits today (confirm this by reading `guards.check_primary_checkout`'s real behavior on an
empty repo), so bootstrapping must happen BEFORE that guard is ever reached, i.e. no later than `swarm plan` or `swarm approve`,
whichever genuinely runs first against a brand new project.

## Part B: ASES-GIT-11 (section 8.3): "Greenfield projects start with one serialized scaffold task that creates the root files every
later task would otherwise fight over: pyproject.toml, package.json, docker-compose.yml, README.md, AGENTS.md, .gitattributes.
Parallel work starts only after the scaffold is merged."
1. Check `cli.cmd_plan`'s CURRENT Lead prompt for whether it already tells the Lead to plan a scaffold task first when the repo is
   empty (an earlier round may have added this; confirm by reading the real prompt text, do not trust an earlier summary). If it is
   missing or vague, add explicit guidance: when the repository has no meaningful existing files (the Lead inspects the repo itself
   and decides this, per blueprint 18.1 "If it is empty, plan a scaffold task first"), the FIRST task in the plan should be a
   scaffold task whose `touches` covers the root config/tooling files it creates, and every other task should `depends_on` it (or
   have touches broad enough that Gate 0's existing overlap-serialization -- ASES-GIT-08, already built in `plan.py` -- naturally
   orders them after it; read `plan.serialize_overlapping_tasks`/`touches_overlap` to confirm this really does what the blueprint
   asks for a scaffold task's shape, and say so precisely in your report either way).
2. Write a test (unit-level, on `plan.py`, no controller/board needed) proving the mechanism: a plan whose first task's touches is
   broad (e.g. `["pyproject.toml", "package.json", ".gitattributes", "AGENTS.md"]` or similarly root-config-shaped) and whose OTHER
   tasks' touches do not literally overlap it but a later task ALSO needs `depends_on: [scaffold_key]` for parallel work to wait
   correctly -- if Gate 0's existing serialization does not already achieve "parallel work starts only after the scaffold is merged"
   for tasks with NO literal touches overlap and NO explicit dependency, that is a real gap: the blueprint's guarantee then relies on
   the Lead ALWAYS writing explicit `depends_on` edges from every task to the scaffold task, which the prompt guidance (step 1) must
   say explicitly and forcefully, and your test should assert Gate 0 at least does not silently allow disconnected parallel tasks
   when a scaffold task is present with no plan-wide guarantee tying them to it -- read `plan.py`'s real validation rules first,
   do not assume; this may already be adequately covered by explicit `depends_on`, in which case your test just confirms it and your
   report says which mechanism (explicit depends_on, or touches overlap, or both) actually provides the guarantee.

## Part C: `_COMMITTING_ROLES` -- the Tester role is currently broken by a hardcoded `role == "coder"` check in six places
Confirmed by reading the CURRENT `controller.py` (search `"coder"` -- six matches: `_finish_instructions`,
`process_budget_gate`'s review-reserve check, the docstring above `process_merge_queue`, the post-merge-check gate (CORE's new
round 6 code), `merge_task`'s `allow_empty=` argument, and one more conditional near it). Every one of these treats "any role other
than coder" as review-only (no commit, completes with a verdict, no merge). That is correct for `reviewer`, but WRONG for `tester`:
per ASES-QG-05, "The Tester writes acceptance tests from docs/ases/contracts/ ... and the implementation card SHOULD depend on
them" -- a tester's card produces a REAL commit (test files) that must go through review and merge exactly like a coder's, not be
treated as a no-op verdict-only card.
1. Add a module-level constant, `_COMMITTING_ROLES = frozenset({"coder", "tester"})` (or read `plan.py`/`config.py` for whether the
   set of "roles that produce a commit" is meant to be config-driven rather than hardcoded -- if there is already a place roles are
   classified this way, extend that instead of inventing a second one; if not, this frozenset in `controller.py` is the right home,
   matching how the existing code already hardcodes the single-role check).
2. Replace every one of the six (confirm the exact count and locations against the CURRENT file, this work order was written by
   reading an earlier snapshot) `task.role == "coder"` / `task.role != "coder"` checks with the appropriate `task.role in
   _COMMITTING_ROLES` / `task.role not in _COMMITTING_ROLES` form. Update every docstring that says "any role other than coder" to
   say "any role not in `_COMMITTING_ROLES` (reviewer today)" instead, so the next role added is not the same silent trap.
3. `_finish_instructions(role, reviewer_profile)`: a tester's hand-off instructions should read like the coder's (commit, request
   review, never complete its own card), possibly with one adjustment -- point 1 of the coder instructions says "Make the change
   only inside your worktree and only on the paths listed under Touches"; for a tester this is the same, just replace "your worktree"
   with nothing role-specific (the instructions can be IDENTICAL to the coder's, or you may add one tester-specific line about
   writing tests from `docs/ases/contracts/` per ASES-QG-05 -- your judgment, keep it short).
4. Confirm Gate 0 (`plan.py`'s `parse_and_validate`) already accepts `"tester"` as a valid role when it appears in `known_roles`
   (`cli.py` already builds `known_roles=set(project.roles)` from config, so this should be automatic once a project's
   `config/swarm.yaml` maps a `tester:` profile) -- write a test confirming a plan task with `role: "tester"` validates cleanly
   when `"tester"` is in `known_roles`, and is REJECTED (as it always has been for any unmapped role) when it is not. Do not change
   `plan.py`'s role validation logic itself unless you find it is actually missing something; this is very likely already correct
   and this is a confirming test, not new production code.

## Tests
Part A: an empty repo (no `.git`) gets a real integration branch and one commit through `ensure_repo_bootstrapped`; a repo with
`.git` but zero commits gets the same; a repo that already has commits on the wrong branch is untouched (still `publish_plan`'s
existing refusal); a repo already correctly set up is a no-op (returns False, changes nothing); no git identity is ever written to
config; the `repo_bootstrapped` event fires only when something was actually created; wired into the real `cmd_plan`/`cmd_approve`
flow with an acceptance-shaped test if that is where you end up wiring it (a plain unit test on `ensure_repo_bootstrapped` itself is
also required regardless). Part B: the Lead prompt content check, and the Gate 0 test described above. Part C: for at least one full
round-trip (through a real temp git repo, similar to `test_scenarios_demo.py`'s style if you have access to the acceptance fixtures,
or a controller-level unit test if not), a tester-role task's card completes with a real commit, goes through Gate 3, and merges
exactly like a coder's would -- not as a no-op. All six `_COMMITTING_ROLES` sites individually covered by a test showing the old
`"coder"`-only check would have behaved wrongly for `"tester"` and the new one does not.

## Report back
The usual report, plus: the exact six (or however many you actually find) `role == "coder"` sites you fixed, with line numbers in
the CURRENT file (not this work order's, which may be stale by the time you run); whether Gate 0's overlap-serialization alone
already guarantees "parallel work starts only after the scaffold is merged" or whether it depends on the Lead writing explicit
`depends_on` edges (and whether the prompt guidance you wrote makes that clear enough); and where you wired
`ensure_repo_bootstrapped` in, and why that is the earliest correct point.
