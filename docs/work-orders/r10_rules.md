# Round 10 addendum to the shared rules (read `r9_rules.md` and the files it names, then this, then your package file)

Everything in `r9_rules.md` still applies unchanged: zero quota (never a real Hermes, a real model provider or Docker; a product
command that WOULD make a real call is built and tested only against fakes), never commit or push, never use `git stash` at all,
one worktree per package under `C:\Users\masoo\ases-wt\<package>` on branch `r10/<package>`, its own pytest
`--basetemp=C:/Users/masoo/ases-wt/_pytest/<package>`, run pytest from your worktree root with the absolute interpreter
`C:/Users/masoo/ases/.venv/Scripts/python.exe`, `PYTHONPATH=src` for ad-hoc scripts, no em dash or section sign, quote the
blueprint (`C:\Users\masoo\ases-workspaces\tools\blueprint.txt`) before building.

Round 10 specifics:
- Every branch is cut from the master commit that adds these work orders (rounds 8 and 9 merged, code identical to `d1c4aab`).
  Baseline: 5668 passed, 2 skipped, 0 failed. It must never go down.
- **Write files with the Write or Edit tool.** A shell heredoc on this machine eats backslashes: round 9 lost `\n` escapes inside a
  test's string literals that way. Never generate Python source through a heredoc.
- In Git Bash, `/tmp` is not `C:\tmp`; a Windows Python cannot read a file you wrote to `/tmp`. Use a directory under your
  worktree's `--basetemp` for scratch files.
- The code has two new single definitions since round 9; use them, never re-inline: `gitexec.GIT`/`gitexec.git_env()`/
  `gitexec.DIFF_SAFETY` for every git subprocess (a completeness test fails on a bare `["git", ...]`), and
  `events.PROJECT_SCOPE_SQL` for any project-scoped read of the `events` table.
