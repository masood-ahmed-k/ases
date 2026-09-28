# Round 14: the second audit's three findings, and an injectable ledger clock (read `r10_rules.md` first)

Two packages, each in its own worktree `C:\Users\masoo\ases-wt\<name>` on branch `r14/<name>`, cut from master after TIDY
merged (`447a591` plus these work orders; baseline 5825 passed, 2 skipped). Every rule in `r10_rules.md` applies. Owner's
testing budget: targeted files while working, ONE full suite at the end.

## RUNSTART2 (`runstart2`): findings A2-0, A2-1, A2-2 in `r14_audit2_findings.md`

Requirements: ASES-REC-04 (p352): "On start the controller compares the board, Git and its database ... It repairs what is
safe and blocks the rest with an explanation." ASES-CTL-01 (p200, the project wall clock). ASES-MOD-02 / acceptance 22.4
(p406): "the controller must reject it before any card starts." Blueprint p169 (ASES-GIT-01/16): "Phase 3 MUST verify the
actual base commit before a worker starts."
1. A2-0 (medium): `swarm run`'s MOD-02 model pre-flight runs after `_start_project_or_refuse` has marked the project running and
   started its wall clock. Move it before, with the other refusals, so any refusal leaves `project_state` untouched. A test that
   a model rejection at run time leaves the project's state exactly as it was.
2. A2-2 (low): between `_refuse_unless_startable`'s read and `bounds.start_project`, reconcile now runs, widening the window in
   which a pause could be overwritten back to running. Re-read the state immediately before starting and refuse (exit 4,
   pointing at `swarm resume`) if it is no longer startable. Test it.
3. A2-1 (low): `process_card_base_checks` skips a card whose branch does not exist yet with no bound and no record. Add a
   bounded grace (the `guards.check_idle_worktrees` grace-key pattern): record how many consecutive passes a running card's
   branch has been missing; past a small bound (choose it, justify it in the docstring), record an event naming the card so it
   is visible, without blocking it (the merge-queue refusal stays the enforcement). Read the skeptic's corrected framing in the
   findings file first: Hermes's own failure limit already handles a worktree add that really fails.
Files you own: the run-start code in `src/ases/cli.py`, `process_card_base_checks` in `src/ases/controller.py`, the helpers it
needs in `src/ases/guards.py`, and tests.

## CLOCK (`clock`): the ledger's clock is injectable, ASES-CAP-03

Requirement (p133): "A card MUST NOT become ready unless the remaining budget covers its estimated cost plus a review reserve.
Otherwise the controller parks it with hermes kanban schedule until the reset time." The register's note: the daily-reset
arithmetic can only be tested by monkeypatching `ledger._today` (see `tests/acceptance/test_22_9_quota.py`'s own comment).
1. Give `ledger.py` an injectable clock without changing any production behaviour: every public function that reads "today"
   takes an optional keyword (for example `now: datetime | None = None`, or one module-level clock object), defaulting to the
   real UTC time exactly as today. Thread it only as far as the callers that already have a clock of their own need it (for
   example `report.build_report(now=...)` already has one: make the report's "requests today" use the same day as the rest of
   the report). Do not rewrite callers that do not need it.
2. Convert `tests/acceptance/test_22_9_quota.py` to the injectable clock instead of monkeypatching a private function, and add a
   test for the UTC day boundary (usage recorded at 23:59:59 UTC counts against that day; at 00:00:00 the next day it resets).
Files you own: `src/ases/ledger.py`, the "today" reads in `src/ases/report.py` and `src/ases/usage.py` only if item 1 needs
them, `tests/unit/test_ledger.py`, `tests/acceptance/test_22_9_quota.py`.
