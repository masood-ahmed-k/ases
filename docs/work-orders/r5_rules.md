# Round 5 addendum to the shared rules (read `r2_rules.md` first, then this, then `r5_contracts.md`, then your package file)

Everything in `r2_rules.md` still applies (files you own, no commits, no Hermes changes, ASCII output, no em dash or section
sign, the Windows `os.kill` trap, tests through the compressor). This addendum changes or adds:

- The suite baseline is whatever it shows before you start (about 3,450 at the time of writing); it must never go down.
- The blueprint text extract is `C:/Users/masoo/ases-workspaces/tools/blueprint.txt` (the session scratchpad path inside older
  work orders is stale). Quote requirement IDs from it in docstrings.
- Files outside your package are read-only for you. Other builders are editing other files in the same tree at the same time. If a
  full-suite failure is in a file you do not own, wait a minute and re-run before deciding, and say so in your report. If you need a
  change in someone else's file, put it in your report; do not make it.
- WRITE FILES WITH THE Write OR Edit TOOL, NEVER THROUGH A SHELL HEREDOC when the content has a backslash: the shell tool turns
  `\b` into a backspace character and eats `\\`, which silently corrupts regexes (it happened on 2026-09-21 and broke every secret
  pattern until it was found). The Write tool also decodes `\uXXXX` in file content into real characters: for a non-ASCII test input
  use `chr(0xE9)` or `"\N{LATIN SMALL LETTER E WITH ACUTE}"` built at run time, and finish by scanning your files for any
  character above 127, for the em dash and for the section sign.
- Do not run the full suite more often than you need to (it takes 4 to 5 minutes and other builders are running it too): run your
  own test files while you work, and the full suite once at the end.
- No network, no LLM or API call, no Docker, no real Hermes command that changes state. Reading the Hermes source under
  `C:\Users\masoo\AppData\Local\hermes\hermes-agent` is allowed and encouraged: several rules below were learned from it.

## Real Hermes 0.21.3 facts learned on 2026-09-21 (read from `hermes_cli/kanban_db.py`, `kanban_db_dispatch.py`, `kanban.py`)
- `block_task` accepts only a card that is `running` or `ready`. Blocking a card that is already `blocked` (a merge card is created
  blocked) or in `todo` returns False; the CLI has ALREADY added the "BLOCKED: <reason>" comment by then and then exits 1
  ("cannot block"). So `hermes.kanban_block` on such a card raises `HermesCommandError` after leaving a comment.
- A block writes a `blocked` event whose payload has `reason` and `kind`. A second block of the same `kind` after an unblock (a
  generic block has kind None, and None equals None) routes the card to `triage` with a `block_loop_detected` event instead
  (`BLOCK_RECURRENCE_LIMIT` is 2). Only a block with `--kind needs_input` carries "a question for a human"; the valid kinds are
  `dependency` (waits in todo), `needs_input`, `capability`, `transient`.
- When the dispatcher's circuit breaker trips (`--max-retries N`, default 2), the card goes to `blocked` with a `gave_up` event whose
  payload has `failures`, `effective_limit`, `error` and `trigger_outcome`, and NO `blocked` event.
- `unblock` moves `blocked` or `scheduled` to `ready` (or `todo` while a parent is unfinished, or back to `review`), resets
  `consecutive_failures`, and records an `unblocked` event. `schedule` works from todo, ready, running and blocked and records a
  `scheduled` event with `reason`. `promote` works only from todo or blocked. A `triage` card can leave triage only through
  `hermes kanban specify` (an auxiliary LLM call), which ASES never runs on its own.
- `--max-retries N` trips on the Nth failure (3 allows two retries); `--max-runtime` accepts `45m`; `create_cards_from_plan` already
  passes `max_runtime` for work cards but does NOT pass `max_retries` (round 5 fixes that).
- `hermes.kanban_block(board, card_id, reason, *, kind=None)` now exists with the argv order `block [--kind K] <id> -- <reason>`
  (Hermes's argparse rejects the option after the id).
- `events.redact_text(text)` now exists (the value scan alone) and `events.redact` no longer blanks a number, boolean or null under a
  credential-shaped key such as `input_tokens`. New secret shapes are covered (nvapi-, sk_live_, AWS, Google, Bearer, JWT, PEM header).
