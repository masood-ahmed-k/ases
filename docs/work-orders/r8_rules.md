# Round 8 addendum to the shared rules (read `r2_rules.md`, `r5_rules.md`, `r6_rules.md`, `r7_rules.md`, then this, then your package file)

Everything in the earlier rules files still applies: files you own, no commits, ASCII output, no em dash or section sign, the Windows
`os.kill` trap, the Write-tool `\uXXXX` decoding trap, write regex/backslash-heavy files with Write/Edit never a shell heredoc, tests
through the compressor, real Hermes facts, and the HARD constraint from round 6: **nothing you do calls a real Hermes, a real model
provider, or Docker.** Everything you test runs on `ases.fakes.board.FakeHermes` or plain unit fakes. The user's words this round:
"we will test properly later. lets build."

## Round 8 specifics
- Repo: `C:\Users\masoo\ases`, branch `master`. Python is `.venv\Scripts\python.exe` (Git Bash: `.venv/Scripts/python.exe`).
- Full suite on this machine: `python C:/Users/masoo/.claude/scripts/quiet.py -l pytest -- .venv/Scripts/python.exe -m pytest -q
  --tb=line --ignore=tests/integration/test_doctor_real_hermes.py`. The `--ignore` is REQUIRED: a real `hermes.exe` is on PATH and that
  one test calls it for real. Baseline before round 8: 5,512 passed, 2 skipped, 0 failed. It must never go down. A full run takes 12 to
  26 minutes: run your own test files while you work, the full suite once near the end.
- **Never run a bare `git stash`.** Other agents may have uncommitted edits in the same tree. For a before/after proof, copy the one
  file you changed aside (or `git stash push -- <only the paths you changed>`), run the test against the old code, then restore it,
  and confirm with `git diff --stat` that the tree is exactly as you left it.
- Blueprint text: `C:\Users\masoo\ases-workspaces\tools\blueprint.txt`. The blueprint docx now lives at
  `C:\Users\masoo\OneDrive\Desktop\AISES\ASES_Swarm_Implementation_Blueprint_v1.2.docx` (it moved; see package HK-PATH).
- Never `git commit`, never `git push`. The architect commits.
- Your final report is appended verbatim to `docs/work-orders/builder-findings.md` by the architect, so write it for a reader who
  was not here: what you changed, why, the exact test counts you observed, and anything you found but did not fix.
