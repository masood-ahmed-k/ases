# Round 13 package TIDY: the two carry-forwards from round 12 (read `r10_rules.md` first)

Worktree `C:\Users\masoo\ases-wt\tidy`, branch `r13/tidy`, cut from `f7d4672` (rounds 11 and 12 merged; baseline 5814 passed,
2 skipped). Every rule in `r10_rules.md` applies. Owner's testing budget: targeted files while working, ONE full suite at the
end.

1. **One process-tree kill.** Round 12's GATEINFRA added `gates._kill_process_tree` (taskkill `/T /F` on Windows, a process
   group kill on POSIX) because it did not own `evals.py`, which already has its own. Make ONE definition in a module both can
   import without an import cycle (`procenv.py` is the natural home: it is already "how ASES starts a subprocess" and has no
   ASES imports), switch both callers to it, and keep each caller's behaviour exactly (read both implementations first; if they
   differ, say how and which behaviour the shared one keeps and why). Existing tests for both stay green; add one direct test of
   the shared helper that kills a real child that spawned a grandchild.
2. **Leaked gate worktrees are visible.** Round 12 records an event when a throwaway gate or merge worktree cannot be removed
   (read `gates.run_gate` and `mergeq.merge_task` for the event kind). Blueprint p185 (ASES-GIT-12) makes the controller
   responsible for what sits outside a worker's worktree. Add a `swarm doctor` row (WARN, never FAIL: a leftover directory is
   disk and hygiene, not a broken gate) that reports leaked gate/merge worktrees still present: from those events and/or
   `git worktree list` of the project repository (doctor already takes `--repo`), naming each path and how to clean it
   (`git worktree prune`, then deleting the directory). Read-only: doctor never deletes anything. Tests on throwaway repos.
Files you own: `src/ases/procenv.py`, the kill helper and its callers in `src/ases/gates.py` and `src/ases/evals.py`, one check
in `src/ases/doctor.py`, and tests.
