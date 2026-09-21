# Package QF: make questions, escalation and recovery agree with how Hermes really blocks cards

Files you own: `src/ases/questions.py`, `src/ases/recovery.py`, `src/ases/report.py`, `tests/unit/test_questions.py`,
`tests/unit/test_recovery.py`, `tests/unit/test_report.py`. Nothing else. Read `r2_rules.md`, `r5_rules.md` (the real Hermes facts are
there) and `r5_contracts.md` (the exact `open_question` and `ask_user` signatures you must deliver) first, then the three modules you
own and their tests.

## Why this package exists
Three finished modules were written against a simplified picture of Hermes, and reading the real source (see r5_rules.md) shows it
does not hold: (1) a card the dispatcher gives up on is `blocked` with a `gave_up` event and NO `blocked` event, so
`swarm questions` cannot see it and `swarm answer` refuses it; (2) `hermes.kanban_block` on a card that is already `blocked` (every
merge card) or in `todo` fails after leaving a comment, so the controller's and recovery's "block the card for the user" silently did
not work for those cards; (3) a second question on the same card is routed to `triage` with a `block_loop_detected` event, where no
listing looks; (4) `report.py` duplicates the open-question rule that `questions.py` has, and the two can drift.

## Requirements (blueprint.txt around [p349] to [p358], section 19.5, and the failure table 31)
- ASES-REC-05: "Asking the user is a first-class state, not an exception. A worker or the controller blocks the card with the question
  as the reason. swarm questions lists open questions with their cards; swarm answer <card> "<text>" adds the answer as a card comment
  and unblocks it. ... Unanswered questions MUST NOT time out into guesses."
- ASES-REC-01 (19.2): "An infrastructure failure ... resume in the same worktree with the same model after a backoff. A capability
  failure means the attempt itself was wrong: start the next attempt from a fresh worktree at the current integration HEAD, attach the
  failure bundle ..., and on the second capability failure switch to the next model for that role class."
- ASES-REC-02 (19.3): lineage budgets, "the controller blocks the task with a question for the user".
- ASES-SEC-01: nothing secret-shaped in a card body or comment.

## Build
1. `questions.py`: add `OpenQuestion`, `open_question(card)` and `ask_user(board, card, text, *, conn=None, author="ases")` EXACTLY as
   specified in `r5_contracts.md` (read its rules for signals, ordering, "answered" detection and the three return values). Keep
   `Question`, `QuestionError`, `list_questions`, `answer_question`, `format_questions` with their current signatures and behaviour,
   but rebuild them on `open_question`:
   - `list_questions` also lists cards in `triage` that carry a `block_loop_detected` event with a reason (use
     `hermes.kanban_list(board, status="triage")` in addition to `blocked`; a triage card WITHOUT that event is an agent-proposed card
     waiting for validation, not a question, and must not be listed). Ownership rules (plan_tasks rows, parents, title) stay as they are.
     `Question` gains a last field `source: str = "blocked"`; `format_questions` shows it in the header for the non-default sources
     ("gave up", "asked by ASES", "unblock loop").
   - `answer_question`: for a `blocked` card, post `ANSWER: <text>` then `kanban_unblock(reason="answered by <author>")` exactly as now
     (this also works for `gave_up` and `ases_comment` sources, and unblocking resets Hermes's failure counter, which is the point). For
     a `triage` card (source `block_loop`) post the `ANSWER:` comment FIRST (the answer must not be lost) and then raise `QuestionError`
     saying the card is in Hermes's triage lane and can only leave it through `hermes kanban specify <id>` (an auxiliary model call
     that ASES does not run on its own) or by re-planning; nothing else is called.
   - A card with a `gave_up` event and a `blocked` event: the newer one decides which reason is shown.
2. `recovery.py`: wherever `process_failures` applies `block_for_user` or `mark_credential_unhealthy`, call `questions.ask_user(board,
   card, question_text, conn=conn)` (a lazy import inside the function is fine) instead of `hermes.kanban_block`, and treat
   "already_asked" as converged (no event spam, no re-comment). The question text keeps its current wording (a question ending in "?").
   `recovery_error` is now only for a failure that `ask_user` could not recover from. Also change `switch_model`: it must NOT be applied
   in place any more. A capability failure restarts from a fresh worktree and the second one also switches model (ASES-REC-01, 19.2), so
   `process_failures` returns `switch_model` decisions UNAPPLIED (no `set_model`, no `unblock`), with `model` and `provider` filled in
   from `next_model`, exactly like `fresh_attempt`; the controller creates the replacement card and pins the model on it. Keep the
   `recovery_switch_target` event so a decided switch is remembered. Update the tests that expected `set-model` then `unblock`.
3. `report.py`: replace the private open-question rule with `questions.open_question(card)`; the cards panel counts blocked and
   triage cards that have an open question and reports `source` counts under `cards.questions_by_source`. Nothing else in the report
   changes shape.
4. Tests: every source (`blocked`, `gave_up`, `block_loop`, `ases_comment`), newest-wins ordering including a tie, an answered
   question (later `ANSWER:` or `UNBLOCK:` comment, later `unblocked` event) not counting, a merge card blocked without any event
   that has an `ASES QUESTION:` comment being listed and then leaving the list after `swarm answer`, `ask_user` for each status
   (ready and running go through `kanban_block(kind="needs_input")`, blocked, triage, todo and scheduled go through the comment, a
   refused block falls back to the comment, an identical open question returns "already_asked" and posts nothing, text is redacted and
   capped, the event is recorded only with a `conn`), the triage answer path (comment posted, nothing unblocked, `QuestionError`), the
   recovery paths (ask_user used, no `kanban_block` call for an already-blocked card, `switch_model` returned unapplied with model
   and provider, `recovery_switch_target` still recorded), the report counts. Existing tests that assumed the old behaviour are
   updated, never deleted without a replacement that covers the same risk.

## Report back
The usual report, plus: any other place in `src/` (outside your files) that still calls `hermes.kanban_block` on a card that may be
blocked, todo or triage (`grep -n "kanban_block" src/ases/*.py`), so the controller package can be told.
