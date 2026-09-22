# Round 7 addendum to the shared rules (read `r2_rules.md`, `r5_rules.md`, `r6_rules.md`, then this, then your package file)

Everything in the earlier rules files still applies: files you own, no commits, ASCII output, no em dash or section sign, the Windows
`os.kill` trap, the Write-tool `\uXXXX` decoding trap, write regex/backslash-heavy files with Write/Edit never a shell heredoc, tests
through the compressor, real Hermes facts, and the HARD constraint from round 6: **your TESTS never call a real Hermes, a real model
provider, or Docker.** Everything you test runs on `ases.fakes.board.FakeHermes` or plain unit fakes.

## What changed this round: one real Hermes call is now explicitly authorized in PRODUCT code
Round 6 found that `triage.promote_card` can never work as built, because moving a card out of Hermes's `triage` status needs
`hermes kanban specify`, which calls an auxiliary language model (a small helper model Hermes itself is configured with, separate
from the project's lead/coder/reviewer). The standing rule had been "ASES never runs specify/decompose on its own." The user was
asked directly and chose: **ASES may call `hermes kanban specify` from `triage.promote_card`, and only from there.** This is a real,
live call once a user actually runs it for real -- but building and TESTING it is still zero-quota, exactly like every other Hermes
wrapper in `hermes.py`: your tests fake the call the same way `test_hermes_kanban.py` fakes `kanban_promote`/`kanban_archive`/etc.,
never a real subprocess. Do not read this as a wider license: every other "ASES never calls X" rule in `r2_rules.md`/`r5_rules.md`
still holds exactly as written. If your package is not `SPECIFY`, this paragraph does not apply to you.

## Suite baseline and package boundaries
The baseline is whatever the suite shows before you start (about 5,420 passed at the time these orders were written, after the
`fail_next` fix); it must never go down. Full-suite runs now take 12 to 20 minutes: run your own test files while you work, the full
suite once near the end. If several agents run the full suite at once, a transient `FileNotFoundError` under pytest's shared temp
base directory is another agent's run colliding with yours, not your bug (re-run before deciding). Quote requirement IDs from
`C:\Users\masoo\ases-workspaces\tools\blueprint.txt` and from `spec/requirements.yaml`; where they disagree, the blueprint wins.
Package `FIXES` owns `controller.py` and `recovery.py` this round; no other round 7 package may touch them (a wave 2 will follow
once FIXES lands, for the items that also need `controller.py`).
