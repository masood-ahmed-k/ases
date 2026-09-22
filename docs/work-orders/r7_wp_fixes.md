# Package FIXES: three real gaps round 6's acceptance tests found in the controller and recovery

Files you own: `src/ases/controller.py`, `src/ases/recovery.py`, `tests/unit/test_controller.py`, `tests/unit/test_controller_loop.py`,
`tests/unit/test_recovery.py`. Nothing else. Read `r2_rules.md`, `r5_rules.md`, `r6_rules.md`, `r7_rules.md` FIRST. No other round 7
package may touch these two source files; a wave 2 (greenfield bootstrapping, the Tester role) needs `controller.py` too and is
sequenced to start only after this package lands. `r6_rules.md`'s zero-quota rule still applies in full to you: use the fake rig
(`ases.fakes.board.FakeHermes`) and existing unit fakes, never a real Hermes or provider call, in any test.

The three bugs below were each found by a round 6 acceptance test; read the exact test and its finding in
`docs/work-orders/builder-findings.md` (search for "AC-G" for bug 1 and 3's origin, "ASES-PRV-01" for bug 2) before changing
anything, so you build against the real failure the test demonstrated, not a paraphrase of it.

## Bug 1: a task's fix or retry card is forgotten if the ASES database is deleted and cards are recreated a third time
`controller.create_cards_from_plan` (read it in full first) decides whether to keep a task's CURRENT card (a fix card
`process_merge_queue` created, or a retry card `_start_fresh_attempt` created) or fall back to creating "the original" again, using
TWO purely local signals: a `plan_tasks` row (for fix cards, via the `fix_cards > 0` clause in its own SQL upsert) and an ASES
`events` table lookup, `_has_retry_card` (for retry cards). Both signals live only in the ASES database. Delete that database (test
22.15's own scenario: "once more after deleting the ASES database") and BOTH are gone, so the function falls into its `else` branch
and calls `hermes_mod.kanban_create` with the task's ORIGINAL idempotency key (`ases-work-{project}-{key}`):
- If the task had a FIX card (never archived, board consistent, ASES bookkeeping goes stale): Hermes's idempotency lookup still
  finds the ORIGINAL (unarchived) work card and returns it, so `plan_tasks.work_card_id` reverts to the wrong, superseded card. No
  duplicate card is created, but ASES now points at a card that is no longer the task's real current one.
- If the task had a RETRY card (`_start_fresh_attempt` ARCHIVES the original when it replaces it): Hermes's idempotency lookup
  EXCLUDES archived cards, finds NOTHING, and creates a genuinely NEW, DUPLICATE work card -- a second card for the same task,
  violating test 22.15's actual claim ("the board must contain each card exactly once"). Confirm this second case with a test of
  your own; it was not directly exercised by round 6's acceptance suite, only implied by reading the same code path.

**Fix the source of truth.** When the local `plan_tasks`/`events` signals are silent (row missing, or present but not naming a
current card), do not assume "no fix/retry card exists" -- ask the BOARD. Read `_start_fresh_attempt` and `process_merge_queue`'s
fix-card-creation code (both in `controller.py`) for exactly how a fix/retry card is linked to the rest of the task's cards: in
particular, look at what gets linked as a PARENT of the task's MERGE card over the task's lifetime (the merge card's `_parents` list,
from `hermes.kanban_show`, is every card that has ever tried to satisfy this task's merge, in link order) -- this is likely your
cleanest, board-native signal for "what is the task's current card," since it does not depend on anything ASES's own database
recorded. Design `_current_work_card(board, plan, key, conn) -> str` (or similar; your name) that: (a) prefers the local
`plan_tasks.work_card_id` when the row exists and is trustworthy (unchanged fast path, no behavior change for the common case); (b)
when the row is missing or you judge it untrustworthy, derives the real current card from the board itself (the merge card's parent
list, or the idempotency-key enumeration `ases-fix-{project}-{key}-{n}`/`ases-retry-{project}-{key}-{n}`/`ases-work-{project}-{key}`
for increasing `n`, picking the one that is (i) not archived and (ii) the LAST one Hermes actually created, whichever real signal you
find is unambiguous -- read the real linking code before choosing, do not guess which one is authoritative). Use this everywhere
`create_cards_from_plan` currently trusts the local-only signals. Write the exact test 22.15 scenario (create twice, delete the
database, create a third time) for BOTH the fix-card case and the retry-card case, and confirm: exactly one card per task, the
RIGHT card (the fix/retry card, not the stale original), no duplicate.

## Bug 2: the private-data-class guarantee is enforced once, at Gate P, never re-checked per pass
Confirmed empirically by round 6 (ASES-PRV-01's finding): `policy.check_data_class` is called only once, by `cli.py`'s
`cmd_approve`/`cmd_critique` at plan-approval time. `controller.process_budget_gate` never calls it. A provider that becomes unsafe
for the project's declared `data_class` (or, per the blueprint, "even when every other provider is exhausted: they park instead")
after approval is never parked for that reason -- only for an ordinary budget shortfall.

**Fix in `_affordable_now`** (`controller.py`), the ONE function `process_budget_gate` and `process_unpark` both already share for
"may this card run now" (read its own docstring: "the same code on both sides means a card is never parked by one rule and unparked
by another" -- keep that property). Add a data-class check ALONGSIDE the existing budget check, in the same style: resolve the
task's provider (`policy.profile_provider`, already imported), call `policy.check_data_class(project.data_class, pp.provider,
provider_policies.get(pp.provider))` (read the REAL signature `cli.cmd_approve` already calls, at its Gate P check, to match it
exactly -- you will need `provider_policies`, the same `{provider: data_policy}` map Gate P builds from `models_config["providers"]`;
build it once, likely by adding it as a parameter `_affordable_now` and its callers thread through, or by deriving it inline from
`models_config` the same way `cmd_approve` does). On a `DataPolicyViolation` (or whatever exception/return shape the real function
uses -- read it, do not assume): park, do NOT raise past `_affordable_now`, with a reason starting `"data class:"` (add that prefix
to `_PARK_PREFIXES` alongside `"budget:"` and `"review budget"`, so `process_unpark` recognizes a data-class park as one of ours --
but READ `process_unpark` first: a data-class violation should almost certainly never be unparked automatically the way a budget
shortfall is, since ASES-PRV-03 says "The controller MUST NOT relax the class to keep work flowing" -- decide whether a
data-class-parked card belongs in `_PARK_PREFIXES` at all, or needs its OWN prefix that `process_unpark` explicitly excludes from
ever auto-resuming; document your choice precisely, since silently letting it auto-unpark once some other provider's budget frees up
would be the exact violation ASES-PRV-03 forbids).
A `project` (the `ProjectConfig`, for `project.data_class`) must reach `_affordable_now`; `process_budget_gate` already takes an
optional `project` parameter for the review-budget half -- extend that same parameter's use rather than adding a new one.

## Bug 3: a card stuck `ready` after an auth- or quota-shaped failure, short of tripping Hermes's own breaker, is invisible to recovery
Confirmed by round 6 (AC-A's finding, and `recovery.process_failures`'s OWN docstring already says so: "A card that Hermes never
blocks (its dispatcher's respawn guard holds it in `ready` ...) is not seen either"). `recovery._recover_task` gates on
`card.get("status") != "blocked": return None` -- a card Hermes's respawn guard is holding in `ready` after a failed run never
reaches any of the classification/decision logic below that line, forever.

**Widen the gate, carefully, to avoid a false positive.** A `ready` card is NORMAL most of the time (never run yet, or successfully
reset for a fresh dispatch); only react to one whose LATEST run (`_latest_run(card)`, already available) both exists and is
classified as a FAILURE, and only after a settle period has passed since that run ended (so ASES never races Hermes's own natural
respawn cadence -- reusing the existing infra-failure backoff shape, or a new fixed constant of your own choosing; document the
number and why). Restrict this to the failure kinds the respawn guard is documented to react to (read `recovery.classify_run`'s AUTH
and QUOTA kinds; do not widen it to every failure kind -- an infrastructure- or capability-shaped failure on a `ready` card is
almost certainly just Hermes about to retry normally, and reacting to it would be the false positive this fix must avoid). When the
widened gate fires, run the SAME classification/decision/apply path `_recover_task` already has for a blocked card (auth ->
`mark_credential_unhealthy`, quota -> `park`) -- do not duplicate that logic, factor the shared body out if the `if status !=
"blocked": return None` line is the only thing standing in the way of reusing it directly. Update the function's own docstring
(remove or correct the sentence saying a respawn-guarded card "is not seen either", since after this change it is, for these two
kinds specifically) and `process_failures`'s docstring the same way.

## Tests
`test_controller.py`/`test_controller_loop.py`: bug 1's exact scenario for both the fix-card and retry-card case (create twice,
delete the database, create a third time, exactly one right card each time); bug 2 (`_affordable_now` parks a card whose resolved
provider fails `check_data_class`, the reason is prefixed correctly, `process_unpark` does NOT auto-resume a data-class park -- or
does, if you concluded that is actually safe; whichever you chose, pin it with a test and explain the choice in your report).
`test_recovery.py`: bug 3 (a `ready` card with a stale auth-shaped failed run, past the settle window, is now recovered exactly like
a blocked one would be; a `ready` card with an infra- or capability-shaped failed run is NOT touched; a `ready` card whose failed run
is still within the settle window is NOT touched yet; a `ready` card with no run at all, or a successful latest run, is unaffected).
Every test uses fakes; nothing here calls a real Hermes or a real model provider.

## Report back
The usual report, plus: for bug 1, exactly which board-native signal you settled on as the source of truth (merge-card parents,
idempotency-key enumeration, or something else) and why; for bug 2, whether a data-class park should ever be auto-unparked and what
you decided; for bug 3, the exact settle-window constant you chose and why that duration is long enough to avoid racing Hermes's own
retry cadence.
