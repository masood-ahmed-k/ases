# Round 10 package BASECHECK: verify a work card's base commit (read `r10_rules.md` first)

Worktree `C:\Users\masoo\ases-wt\basecheck`, branch `r10/basecheck`.

## Requirement (blueprint p169, quoted exactly)
"The safe default for parallel agents is one Git worktree per work card. The primary checkout stays on the integration branch and
is never edited by agents. Current Hermes can sync a worktree from the freshly fetched remote tip by default; ASES requires the
worktree base to be the exact local integration HEAD. Set worktree_sync: false for ASES-managed worktrees, or have the controller
create the worktree manually from the pinned integration HEAD. Phase 3 MUST verify the actual base commit before a worker starts.
[ASES-GIT-01] [ASES-GIT-16]"

Register: ASES-GIT-01 and ASES-GIT-16 are `partial` for exactly the last sentence: nothing in `src/ases` verifies a card's base.

## Facts (verified by the architect on 2026-09-27; re-check anything you rely on)
- Hermes's Kanban dispatcher ignores `worktree_sync`: it always runs `git worktree add -b <branch> <path> HEAD` from the board's
  repository (`kanban_db_workspace._ensure_git_worktree` in the installed Hermes source, read-only, under
  `C:\Users\masoo\AppData\Local\hermes\hermes-agent`; see also `src/ases/profiles.py` module docstring item 4). So a new card's
  base is the primary checkout's HEAD at the moment Hermes dispatches it. A RETRIED card reuses its existing branch
  (`worktree add <path> <branch>`), so its base is the one it was first created from.
- `guards.check_primary_checkout` already requires the primary checkout to be on the integration branch, clean, and at the head
  ASES itself last wrote (`guards.expected_head`, `set_expected_head`, `adopt_current_head`) at the start of every pass.
- Dispatch happens either inside `controller.run_pass` (`hermes.kanban_dispatch(board)`) or asynchronously by Hermes's own gateway.
  ASES never spawns a worker and cannot stop one from STARTING. So "before a worker starts" is enforced as: detected on the first
  pass that sees the card running (and immediately after `run_pass`'s own dispatch), and made impossible to merge.
- `src/ases/fakes/board.py` creates real git worktrees (`worktree add -b <branch> <target> <integration>` around line 1793),
  so the check can be tested for real on the fake board.

## Build
1. A base-commit check: for a work card's branch, find the commit it was CREATED from and require it to be an integration head
   ASES itself wrote or adopted. Decide how to find the creation commit robustly (the branch's reflog "branch: Created from"
   entry, if `core.logAllRefUpdates` is on; or another signal you can justify; say what happens when the signal is missing:
   fail closed with a clear reason, never pass silently) and how ASES knows its own past heads (the current expected head is
   not enough: a card dispatched before a later merge legitimately has an older base, so keep or derive the set of heads ASES
   has written: adopted start head, every fast-forward and revert it made). Put the logic where it belongs (guards.py is the
   natural home, next to check_primary_checkout) and run every git call through `gitexec`.
2. Wire it in twice. (a) Detection: every pass, for each running work card not yet verified, check its base; a wrong or
   unverifiable base records an `integrity_violation`-style event naming the card, its base and the expected set, and blocks
   the card for the user through the existing question path (never a silent requeue). Once verified, do not re-check the same
   card and branch every pass. (b) Enforcement: the merge queue refuses to merge a branch whose base fails the check, with a
   refusal event, so even a card the detector missed can never land. Keep hunks small in `controller.py` and `mergeq.py`.
3. `swarm doctor`: a check that `core.logAllRefUpdates` (or whatever your signal needs) is on for the project repository, WARN
   if not, since the base check depends on it.
4. Tests on the fake board and throwaway repos: a card dispatched at the current head passes; a card dispatched before a merge
   (older ASES-written head) passes; a branch created from a commit ASES never wrote (plant it by hand, the way a remote-tip
   sync would) is detected, blocked, and refused by the merge queue; a missing signal fails closed with its reason; a retried
   card on its original branch still passes. Before/after proof: the planted-base test lets the merge through on the old code.
   One acceptance-level test in a NEW file under `tests/acceptance/` through a real controller pass on `FakeHermes`.

## Files you own
`src/ases/guards.py` (the new check and its helpers), the call sites in `src/ases/controller.py` and `src/ases/mergeq.py`,
`src/ases/doctor.py` (one check), a schema migration in `src/ases/db.py` only if you truly need one (next free number is 9; say
why), and tests. Other round 10 packages touch other parts of `controller.py`, `doctor.py` and `cli.py` on their own branches.
