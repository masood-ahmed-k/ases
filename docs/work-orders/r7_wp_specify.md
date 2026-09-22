# Package SPECIFY: wire `hermes kanban specify` so triage.promote_card can actually promote a card (Option A, user-approved)

Files you own: `src/ases/hermes.py`, `src/ases/triage.py`, `tests/unit/test_hermes_kanban.py`, `tests/unit/test_triage.py`. Nothing
else. Read `r2_rules.md`, `r5_rules.md`, `r6_rules.md`, `r7_rules.md` FIRST -- `r7_rules.md` explains exactly what is and is not
newly authorized (only this one call, only from `triage.promote_card`).

## The decision (already made by the user, quoted verbatim)
Asked whether `triage.promote_card` should call Hermes's own `specify` (an auxiliary-model call) to move a validated proposal out
of triage, or leave that step to the user: the user answered **"option A"**. Build it.

## Ground it in the real Hermes source first (read-only; never write to or run the Hermes install)
Read `C:\Users\masoo\AppData\Local\hermes\hermes-agent\hermes_cli\kanban_parser.py` (search `specify`, `_triage_sweep_args`) and
`kanban.py` (search `_cmd_specify`, `_run_triage_sweep`) and `kanban_db.py` (search `specify_triage_task`, around line 3615) and
`kanban_specify.py` for real. Confirm and correct anything in this paragraph that turns out wrong: the CLI is
`hermes kanban specify <task_id> [--author NAME] [--json]` (task_id is the ONLY positional besides the shared `--all`/`--tenant`
sweep flags, which you will not use: ASES specifies one card at a time, never `--all`); it calls an auxiliary LLM
(`auxiliary.triage_specifier`) to fill in title/body, then moves the task from `triage` to `todo` in one transaction
(`specify_triage_task`); `--json` output shape is a small result object, NOT the flat task-dict shape `kanban_show`/`kanban_create`
return -- read `_run_triage_sweep`'s `json_key` tuple (`"task_id", "ok", "reason", "new_title"`) to get the real field names, and
confirm whether `ok=false` (the auxiliary model declined, or produced something unusable) is a JSON success with `ok: false` inside
it, or a nonzero CLI exit code -- this matters for how your wrapper decides success vs. failure. Also confirm: does `specify` accept
an EXPLICIT `--title`/`--body` to skip the auxiliary model entirely (some Hermes commands offer both an auto and a manual path)? If
it does not, say so plainly; ASES's own use is the auto (auxiliary-LLM) path only, per the user's decision.

## Build `hermes.py`
1. `kanban_specify(board: str, card_id: str, *, author: str | None = None, timeout: int = 120) -> SpecifyResult` (a new frozen
   dataclass: `ok: bool`, `reason: str | None`, `new_title: str | None`, matching the real fields you found). Uses a LONGER default
   timeout than the other kanban wrappers (they default to 30s via `_kanban`'s own default; `specify` makes a real model call, which
   is slower -- read `_kanban`'s signature and pass `timeout=timeout` through, or call `_run` directly if `_kanban` cannot take a
   per-call timeout, whichever the real code needs). Raises `HermesCommandError` on a nonzero exit (same convention as every other
   wrapper); on a zero exit whose JSON says `ok: false` (the auxiliary model declined), DOES NOT raise -- returns `SpecifyResult(ok=False,
   reason=..., new_title=None)`, since that is a normal, expected outcome (the card just was not accepted as a real task), not a
   Hermes/infrastructure failure. Document the distinction clearly in the docstring, since every OTHER wrapper in this file treats a
   Hermes-side "no" as an exception; this one is deliberately different because Hermes's own contract for `specify` is "try to make
   sense of this, tell me if you could not," not "do this or fail."
2. Every existing wrapper's conventions apply: `--` before anything free-text if the real CLI needs it (check whether `specify` takes
   any free-text argument at all; if it is purely `<task_id> [--author] [--json]`, there is nothing to `--`-guard), ASCII-safe
   handling, the same `_kanban`/`_kanban_json` helpers reused where they fit.

## Fix `triage.py`
3. `promote_card(...)`: replace the current `hermes_mod.kanban_promote(board, card_id, reason=reason)` call (which always fails on a
   genuinely triage-status card, per round 6's finding) with `hermes_mod.kanban_specify(board, card_id, author=...)`. On
   `SpecifyResult.ok is True`: proceed to `record_decision(conn, project, card_id, Decision.PROMOTE, reason=reason,
   raised_by_task=raised_by_task)` exactly as today, and ALSO record what Hermes actually did (the card is now `todo`, not `ready`
   like the old `kanban_promote` path implied -- update `promote_card`'s docstring to say this precisely: it hands the card to
   Hermes's own specify path, which lands it in `todo`, not directly `ready`, and normal `todo` -> `ready` promotion via
   `recompute_ready`/dependency satisfaction takes it from there, same as any other card). On `SpecifyResult.ok is False`: raise
   `TriageError` naming the reason Hermes gave (never invent one), and record NOTHING (matching every other refusal path in this
   module: a refusal leaves the card untouched, per the existing tests' own pattern). `force=True` still bypasses ASES's OWN
   `validate()` check (unchanged), but does NOT and CANNOT bypass Hermes's own auxiliary-model judgment inside `specify` -- if
   Hermes says no, `force=True` does not help; document this limit of `force` clearly, since round 6's docstring implied `force`
   always got the card promoted, which is no longer true.
4. Keep every other function in `triage.py` exactly as it is (`list_triage_cards`, `validate`, `archive_card`, `record_decision`,
   `format_triage`); this package only touches `promote_card` and its docstring.

## Update `r2_rules.md`'s standing rule (a small, precise correction, since it is now stale)
5. `docs/work-orders/r2_rules.md` says, in its "What already exists" section, that ASES never calls `specify`/`decompose`. You may
   edit this ONE file to add a precise, narrow correction: a short parenthetical after that sentence saying `kanban_specify` is now
   called, but ONLY from `triage.promote_card`, on the user's explicit decision (2026-09-22, "option A"), and never anywhere else.
   Do not soften or generalize the rule beyond that one exception. (This is the one file outside your four-file list you may touch,
   and only for this one sentence.)

## Tests
`test_hermes_kanban.py`: `kanban_specify` argv shape (task_id, `--author` when given, `--json`), the timeout is longer than the
default, a nonzero exit raises `HermesCommandError`, a zero exit with `ok: false` in the JSON returns `SpecifyResult(ok=False, ...)`
WITHOUT raising, a zero exit with `ok: true` returns the reason/new_title fields correctly, malformed JSON on stdout is handled the
same way every other `_kanban_json` caller handles it (read what that already does, do not invent new behavior). `test_triage.py`:
`promote_card` calls `kanban_specify` (not `kanban_promote`) with the right arguments, a `SpecifyResult(ok=True)` records the
promote decision, a `SpecifyResult(ok=False)` raises `TriageError` with Hermes's own reason and records nothing, `force=True` still
skips ASES's own `validate()` but a Hermes-side `ok=False` still raises even with `force=True` (update or replace any existing test
that assumed the old `kanban_promote`-based behavior, do not leave a stale test asserting the old call). Every test fakes
`hermes.kanban_specify`/the underlying `_run`; nothing here calls a real Hermes or a real model.

## Report back
The usual report, plus: the EXACT JSON shape you found for `specify --json` (field names, and whether `ok: false` is a 0-exit JSON
result or a nonzero CLI exit), quoted from the real source with a file and line number, since this is the one piece of this package
most likely to need a second look if the real behavior turns out subtler than this work order assumed.
