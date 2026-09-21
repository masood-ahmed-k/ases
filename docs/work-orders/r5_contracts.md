# Interface contracts between the round 5 packages

Several packages are built at the same moment. Build against THESE signatures, not against whatever you find half-written in
another builder's file. Tests that touch another package's function should monkeypatch it (use `raising=False` when the function
may not exist yet) or import it lazily inside the function under test. If you need anything not listed here from another package,
do not edit its file: describe the need in your report.

## questions.py (package QF owns it; the controller and recovery use it)
```python
@dataclasses.dataclass(frozen=True)
class OpenQuestion:
    reason: str        # the question text, redacted, ASCII-escaped
    asked_at: int      # epoch seconds (0 when unknown)
    source: str        # "blocked" | "gave_up" | "block_loop" | "ases_comment"

def open_question(card: dict) -> OpenQuestion | None
def ask_user(board: str, card: dict, text: str, *, conn=None, author: str = "ases") -> str
```
`card` is a `hermes.kanban_show` dict (`status`, `_events`, `_comments`, `_runs`, ...). `open_question` returns None unless the card
is `blocked` or `triage`. Signals, newest wins (compare `created_at`; on a tie the comment counts as newer): a `blocked` event with a
non-empty payload `reason` (source "blocked"); a `gave_up` event (source "gave_up", reason built from its payload:
"gave up after N failure(s): <error>"); a `block_loop_detected` event with a reason (source "block_loop"); a comment by author `ases`
whose body starts with `ASES QUESTION:` (source "ases_comment", reason = the rest). A signal older than the latest `unblocked` event
or the latest comment starting `ANSWER:` or `UNBLOCK:` does not count (it was answered).
`ask_user` returns "already_asked" when an identical question is already open; for a `ready` or `running` card it calls
`hermes.kanban_block(board, id, text, kind="needs_input")` and returns "blocked", falling back to the comment path when Hermes
refuses; for any other status (blocked, triage, todo, scheduled) it posts `ASES QUESTION: <text>` with `hermes.kanban_comment(...,
author=author)` and returns "commented". Text is redacted with `events.redact_text` and capped at 1500 characters. It records a
`question_asked` event when `conn` is given. `list_questions` and `answer_question` keep their signatures and now use `open_question`.

## controller.run_pass (package CT owns it; the CLI package reads it)
`run_pass(board, repo, plan, project, models_config, *, conn, now=None) -> dict` with these keys, all always present:
`parked` (list), `dispatch` (dict), `sent_back` (list), `merged` (list), `unreviewed` (list), `usage_sessions` (int),
`integrity` (list of problem strings: a non-empty list halts the run, exit 3), `warnings` (list of strings, never halt),
`recovery` (list of {task_key, action, kind}), `unparked` (list of task keys), `provisioned` (list of card ids),
`stopped` (bool), `stop_reason` (str or None), `final` (None, or the status string of `finalgates.finalize`: "finished",
"gate_failed", "not_ready" or "error"), `finished` (bool: the project is finished, meaning `bounds` says so).
`controller.pause_and_report(board, repo, plan, project, models_config, reason, *, conn) -> str` (returns the report directory).
`create_cards_from_plan` keeps its signature.

## mergeq and review (package MR owns them; the controller calls them)
`mergeq.merge_task(repo, integration_branch, branch, task_key, gate_commands, *, conn, commit_message, allow_empty=False,
expected_head=None, project=None, should_stop=None) -> MergeOutcome`. `MergeOutcome` gains a last field `stopped: bool = False`.
`should_stop` is a zero-argument callable polled before the candidate is built, before Gate 3 and before the fast-forward; when it
returns True the candidate is discarded, no merge record is left half-written, and the outcome is `merged=False, stopped=True`.
When `project` is given the candidate build plus Gate 3 run inside an `intents.intent(conn, project, KIND_BUILD_CANDIDATE,
task_key)` and the fast-forward inside `KIND_FAST_FORWARD`. `mergeq.revert_merge` gains `project=None` and uses `KIND_REVERT` the
same way. `review.BranchCheck.kind` gains `"tamper"` (blocking findings from `tamper.check_range`) and `"tamper_check_error"`
(git failed; nothing is known). Function signatures in `review.py` do not change.

## finalgates (package FG), profiles (package PF), evals (package EV), hardening (package HD)
Exactly as in their work orders (`r3_wp_finalgates.md`, `r4_wp_profiles.md`, `r5_wp_evals.md`, `r5_wp_hardening.md`):
`finalgates.finalize(board, repo, plan, project, models_config, conn, *, now=None) -> FinalizeResult(status, gate4, gate5,
report_path, reason)`; `finalgates.final_gate_question(outcome) -> str`; `profiles.plan_init(...)`, `profiles.apply_init(...)`,
`profiles.verify_state(...)`; `evals.main(argv: list[str]) -> int`; `hardening.clean(...)`, `hardening.retention(...)` and their
`format_*` functions. The CLI package imports each of these lazily inside the command that needs it.

## Names that must not change (other modules already use them)
`hermes.py` wrappers (only `kanban_block` gained `kind=`), `events.record/redact/redact_text`, `bounds.*`, `recovery.*`,
`killswitch.*`, `reconcile.*`, `intents.*`, `leases.*`, `guards.*`, `tamper.*`, `sandbox.*`, `critic.*`, `report.*`.
