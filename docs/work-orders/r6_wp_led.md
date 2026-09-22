# Package LED: agent-proposed cards land in triage and are validated before promotion (ASES-LED-03)

Files you own: `src/ases/triage.py` (new), `tests/unit/test_triage.py` (new). Nothing else. Do NOT edit `controller.py` or `cli.py`
(other packages, or the architect afterward, wire your module in). Read `r2_rules.md`, `r5_rules.md`, `r6_rules.md` first.

## Requirement (quote it; read blueprint.txt around `[p246]` to `[p249]`, section 12.4, and the failure/card-state table around
`[p242]`)
- ASES-LED-03, section 12.4: "Agents may propose follow-up work during the project with the Hermes kanban_create tool. Such cards MUST
  land in triage. The controller validates them like plan tasks, charges them to the lineage budget of the task that raised them, and
  promotes or archives them."
- The table row (blueprint.txt around `[p242]`): "Proposed by an agent during the project | W in triage | Controller validates
  (section 12.4), then promotes or archives".
- Test 22.15 (idempotent re-run): "A card proposed by an agent must stay in triage until it is validated."
- Section 19.4/22.7 (crash recovery) and `r2_rules.md`'s standing rule: "ASES never runs `specify`/`decompose` on its own" (the round 5
  profiles builder found Hermes's own `kanban.auto_decompose` defaults to true, which would do this automatically and must be turned
  off; that is `profiles.py`'s job, already built, not yours). Your module NEVER calls `hermes kanban specify` or `hermes kanban
  decompose`.

## First, read the real Hermes worker-side surface (read-only; never write to the Hermes install; never call it for real)
Confirm, by reading `C:\Users\masoo\AppData\Local\hermes\hermes-agent\hermes_cli\` source (the same files earlier builders read:
`kanban.py`, `kanban_db.py`, `kanban_parser.py`, and whatever exposes the AGENT-facing kanban tool surface, likely under `agent/` or
`tools/`), exactly which kanban actions a WORKER's tool call can perform: can a worker create a new card at all (the coder prompt,
Appendix C.2, says "If you see work outside your card, propose a follow-up card instead of doing it" -- what tool call does that map
to)? Does `kanban_create` from inside a worker's tool call default to `initial_status="triage"`, or does the worker have to ask for it,
or is card creation from a worker's own tool call not exposed at all (in which case "propose a follow-up card" might mean something
else, like a structured comment or a specific tool name)? Write exactly what you found, with file and function names, at the top of
your report. This determines whether `triage.py` needs a `propose_card` helper (if ASES itself must create the card in triage on the
worker's behalf, from something in the run metadata or hand-off) or only a `list_triage_cards`/validate/promote surface (if Hermes
already lands worker-created cards in triage on its own).

## Build `triage.py`
1. `TriageError(Exception)`.
2. `TriageCard` frozen dataclass: card_id, title, body, proposed_by (the assignee/profile of the run that created it, or None),
   raised_by_task (str or None: the plan task whose card's run proposed this one, found the same way `questions.py` and `leases.py`
   scope cards to a plan -- read how they use `plan_tasks` rows and a card's `_parents`/title prefix), created_at (epoch seconds).
3. `list_triage_cards(board, plan, *, conn) -> list[TriageCard]`: `hermes.kanban_list(board, status="triage")`, scoped to this plan the
   same way `questions.list_questions` scopes (read it: NOT by task-key string alone, cross-project task-key collisions are real).
   EXCLUDE a card that is in triage because of Hermes's own `block_loop_detected` unblock-loop routing (that is a QUESTION, already
   handled by `questions.py`; read its rule for telling the two apart, likely a `block_loop_detected` event vs. no such event on this
   card) -- LED-03 triage cards are agent-PROPOSED new work, not a recovery state of an existing card. Oldest first.
4. `Decision` frozen dataclass or a plain Enum: `promote`, `archive`. `record_decision(conn, project, card_id, decision, *, reason=None,
   raised_by_task=None) -> None`: writes one `events.record(conn, "triage_decision", {...})` row (card_id, decision, reason redacted,
   raised_by_task) AND, when `raised_by_task` is known, bumps that task's lineage the way the blueprint says ("charges them to the
   lineage budget of the task that raised them"): read `recovery.py`'s `bump` (you do not own `recovery.py`; call its PUBLIC function,
   do not edit it) with a field name that makes sense for a proposed card -- if `recovery.Lineage`/`bump` has no field for this yet,
   use the closest existing one that the blueprint's intent matches (a fix card is the closest existing "extra work this task spawned"
   concept; read `recovery.bump`'s whitelist and pick from it) and say in your report if a new lineage field
   (`proposed_cards`) is the more correct long-term answer for a later round -- do not add a new column to the `lineage` table yourself
   (you do not own `db.py`).
5. `validate(board, card_id, *, conn, plan, known_roles) -> ValidationResult` (frozen: ok bool, problems tuple of str): checks a triage
   card's body/title against the SAME shape Gate 0 requires of a plan task where that makes sense for a free-text proposal -- at
   minimum: non-empty title, a body long enough to act on (not blank), and if the body contains a structured hint (a task-like shape:
   look for something parseable, but do not require one; a plain English proposal is valid too as long as a human can read it). This is
   deliberately lighter than full Gate 0: a triage card is a PROPOSAL for a human/controller to accept or reject, not itself a
   ready-to-run task. Never raises on malformed input; every problem is one ASCII sentence.
6. `promote_card(board, card_id, *, conn, project, raised_by_task=None, reason=None) -> None`: `hermes.kanban_promote(board, card_id,
   reason=...)` (read its real signature), then `record_decision(..., "promote", ...)`. Refuses (raises `TriageError`) when `validate`
   would report a problem, unless the caller passes `force=True` (a human override) -- add that parameter.
7. `archive_card(board, card_id, *, conn, project, raised_by_task=None, reason=None) -> None`: `hermes.kanban_archive(board, [card_id])`
   then `record_decision(..., "archive", ...)`.
8. `format_triage(cards, now=None) -> str`: ASCII-only, same style as `questions.format_questions` (read it and match the look), one
   block per card, oldest first, "No proposed cards awaiting validation." when empty.
9. Nothing here is wired into `run_pass` or the CLI. Say clearly in your report: (a) a proposed `process_triage(board, plan, *, conn)`
   step for the controller loop that lists triage cards and reports them (never auto-promotes: promotion is always a human decision via
   `swarm triage promote <id>` / `swarm triage archive <id>`, matching how `swarm answer` works for questions), and (b) the two CLI
   commands `swarm triage` (list) and the promote/archive actions, for the architect to add.

## Tests (`tests/unit/test_triage.py`; monkeypatch `hermes`; temp DB with `plan_tasks` rows; real Hermes never touched)
`list_triage_cards`: a genuinely proposed card is listed; a `block_loop_detected` triage card (a question, not a proposal) is excluded;
cross-project task-key collision excluded (same pattern `test_questions.py` uses -- read it); ordering. `validate`: empty title/body
rejected, a reasonable proposal accepted, malformed input never raises. `promote_card`/`archive_card`: the right Hermes calls in the
right order, `record_decision` writes the event and bumps lineage (assert which field, and that a task with no `raised_by_task` given
does not touch lineage at all), `force=True` bypasses a validation refusal, a refusal leaves the card untouched (no Hermes call).
`format_triage`: ASCII, empty case, ordering. Whatever you found about the real worker-side card-proposal mechanism, add ONE test that
documents it as a comment/docstring reference even if it cannot be exercised without a real Hermes (a "this is what real Hermes does,
verified by reading kanban.py:LINE, not run here" note is enough).
