# Round 16 package EVIDENCE: the reviewer sees the controller's gate result (read `r10_rules.md` first)

Worktree `C:\Users\masoo\ases-wt\evidence`, branch `r16/evidence`, cut from master after round 15's work order (`37623dc`).
Every rule in `r10_rules.md` applies (zero quota: no real Hermes worker, no model call). Testing budget: targeted files while
working, ONE full suite at the end.

## What the real run on 2026-09-28 showed (stage C, `docs/stage-c-2026-09-28.md` once written)
Task S1: coder-1 committed `slug.py` and `test_slug.py` with 9 passing tests and handed off. The reviewer (OpenRouter
`cohere/north-mini-code:free`, no terminal) found the code correct and every acceptance criterion met, then REQUESTED CHANGES
anyway: "the terminal tool constraint prevents me from independently verifying that `python -m pytest -q test_slug.py`
actually passes". The extra round used up OpenRouter's free daily quota (50 requests), and S1 was parked until the reset. The
same gap was written down after the first real run on 2026-09-19 ("post the controller's Gate 1 result to the card as a
comment so a reviewer can cite evidence the controller produced") and never built.

## Requirements (quoted from blueprint.txt)
- ASES-QG-01 (p275): "A reviewer may identify that tests are missing, but a code review is never the only quality gate. The
  gate runner executes the project's pinned commands and records the output by commit SHA. An agent cannot mark anything as
  passed by saying so: the controller believes only its own gate records."
- ASES-REV-05 (Appendix F): "The controller re-runs Gate 1 when a card enters review".
- `prompts/reviewer.md` today tells the reviewer to read "the gate records" (which live in the ASES database, invisible to it)
  and "never say a check passed unless a gate record shows it".

## Build
1. When the review lane re-runs Gate 1 on a card that entered review (`review.gate_before_review`, called from
   `controller.process_review_lane`), post the result on the card as a comment through the existing Hermes wrapper: a fixed,
   recognisable header (for example `ASES gate record`), the gate name, pass or fail, the full commit SHA it ran on, the
   commands, and a short tail of the output, redacted with `events.redact_text` (ASES-SEC-01). Post once per commit (do not
   repost every pass). A failed or not-run gate is posted too, truthfully. Keep it zero-quota: posting a comment is a Hermes CLI
   call already faked by `FakeHermes`; check the fake has it.
2. `prompts/reviewer.md`: say where the controller's gate records are (the card comments with that header), that a missing
   record means the controller has not run it YET and is never by itself a reason to request changes, and that the reviewer
   judges the code and whether the tests are adequate, while pass or fail is the controller's gates, which run before any merge
   and block a red one. Keep "never claim a check passed unless a gate record shows it". Bump the prompt's version line the way
   the file already records it, so `swarm init` sees the change.
3. Tests: the comment is posted with the right content and redaction, once per commit, on pass and on fail; the prompt file
   carries the new guidance (a small test that reads it, like the existing prompt tests if any); `swarm init`'s plan shows the
   reviewer SOUL.md change (dry run, on a fake Hermes home).
Files you own: `src/ases/review.py` (gate_before_review), the review-lane call in `src/ases/controller.py`, `prompts/reviewer.md`,
the Hermes comment wrapper in `src/ases/hermes.py` and its fake in `src/ases/fakes/board.py` only if one is missing, and tests.
