# Package Q: the human channel (swarm questions and swarm answer)

Files you own: `src/ases/questions.py` (new), `tests/unit/test_questions.py` (new). Nothing else.

## Requirements (quote the ids in your docstrings; read the blueprint paragraphs around them)
- ASES-REC-05, section 19.5: "Asking the user is a first-class state, not an exception. A worker or the controller
  blocks the card with the question as the reason. swarm questions lists open questions with their cards; swarm answer
  <card> "<text>" adds the answer as a card comment and unblocks it. Hermes gateway notifications MAY be subscribed so
  that questions reach the user's phone. Unanswered questions MUST NOT time out into guesses."
- ASES-SEC-01 (section 21.1): secrets must never enter card bodies or comments; scan them before they are written.
- Also read: section 22.2 (the end-to-end test asks one card to raise a question that `swarm questions` and `swarm answer`
  unblock) and 19.1 (rows "Budget or bound reached" and "Malformed plan or verdict": the controller blocks the card
  and the user is asked).

## What a "question" is
A card on the board that is `blocked` and carries a block reason nobody has answered yet. Hermes records the reason in a
`blocked` event on the card (`card["_events"]`: dicts {kind, payload, created_at, run_id}; the `blocked` event payload has
`reason`). A merge card that is merely waiting (created blocked by the controller, no `blocked` event) is NOT a question.
A merge card the controller blocked with a reason (for example "fix-card budget (2) exhausted for T1; needs a human
decision") IS one: it is the controller asking the user. Once answered the card is unblocked (status no longer blocked),
so an answered question disappears from the list by itself. Use the LATEST `blocked` event when a card was blocked more
than once.

## Build `questions.py`
1. `QuestionError(Exception)`.
2. `Question` frozen dataclass: card_id, title, task_key (str or None), card_kind ("work", "merge", "fix" or "other"),
   assignee (str or None), question (the reason text), asked_at (int epoch seconds from the event's created_at, 0 if
   missing).
3. `list_questions(board, plan, *, conn) -> list[Question]`: `hermes.kanban_list(board, status="blocked")`, then
   `hermes.kanban_show` for each blocked card. Keep a card only when it belongs to this plan: its id is the work or merge
   card of a row in plan_tasks WHERE project = plan.project (query the table), OR one of its `_parents` is such a card (a
   fix card hangs under the card it replaces, and fix cards are titled "<task key>: fix (round N)"), OR its title
   starts with "<task key>:" for a task key of this plan. Other projects' cards on the same board are excluded (task keys
   collide across projects, so scope by plan_tasks rows, not by key alone). card_kind: "merge" when the id is a
   plan_tasks merge_card_id, "work" when it is a work_card_id, "fix" when the title contains ": fix (round", else
   "other". Skip a blocked card with no `blocked` event carrying a non-empty reason. Oldest question first (asked_at,
   then card_id).
4. `answer_question(board, card_id, text, *, conn, author="user") -> Question`: (a) text must be non-empty after strip,
   else QuestionError; (b) `gates.scan_for_secrets(text)` (see src/ases/gates.py) must find nothing, else QuestionError
   naming the findings but never echoing the secret; (c) fetch the card with `hermes.kanban_show`: it must be `blocked`
   and be an open question by the rule above, else QuestionError ("card X has no open question"); (d) post the answer
   with `hermes.kanban_comment(board, card_id, "ANSWER: " + text.strip(), author=author)` FIRST, then
   `hermes.kanban_unblock(board, card_id, reason="answered by " + author)`; if the comment fails nothing is unblocked
   (let the HermesCommandError propagate), if the unblock fails after the comment was posted let its error propagate too
   and do not retry; (e) `events.record(conn, "question_answered", {"card_id": card_id, "task_key": ..., "chars":
   len(text)})` (never the text itself); (f) return the Question that was answered.
5. `format_questions(questions) -> str`: ASCII only, one block per question: a numbered header line with card id and
   task key, the card kind, the age in minutes or hours relative to `now` (make `now` an optional parameter, epoch
   seconds, for tests), and the question text indented under it. When there are none return the single line
   "No open questions." Escape non-ASCII with backslash escapes so a Windows console never crashes.
6. `notify_hint(questions)`: NOT needed, do not build it.

## Tests (`tests/unit/test_questions.py`; monkeypatch the hermes functions; temp DB with plan_tasks rows inserted directly)
Selection: a blocked plan work card with a `blocked` event is listed with the right fields; a blocked merge card with a
controller block reason is listed with card_kind "merge"; a merge card that is only waiting (no `blocked` event) is not
listed; a blocked card of ANOTHER project that reuses the same task key is not listed; a fix card (title "T1: fix (round
1)", parent = the work card) is listed as "fix"; a card blocked twice reports the latest reason; a `blocked` event with an
empty reason is skipped; ordering is oldest first; a card whose kanban_show raises HermesCommandError is skipped without
breaking the rest (decide and document: skip it and continue). Answering: comment posted before unblock (record the call
order), author "user", the comment text is "ANSWER: <text>", unblock reason "answered by user"; empty text refused with
nothing posted; a secret-shaped text ("sk-abcdefghijklmnopqrstuvwx") refused, nothing posted, and the error message does
not contain the secret; a card that is not blocked, or blocked without a reason event, refused; a comment failure leaves
the card blocked (unblock never called); the event is recorded without the text. Formatting: ASCII only for a card title
containing non-ASCII characters, the empty case, the age rendering with a fixed `now`.
