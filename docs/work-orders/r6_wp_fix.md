# Package FIX: one Bounds, one meaning for stop_requested, and the health panel's missing event kinds

Files you own: `src/ases/recovery.py`, `src/ases/killswitch.py`, `src/ases/report.py`, and their test files
(`tests/unit/test_recovery.py`, `test_killswitch.py`, `test_report.py`). Nothing else. Read `r2_rules.md`, `r5_rules.md`, `r6_rules.md`
first. This is a consolidation package: every change here should be behavior-preserving for existing callers except where the report
explicitly says otherwise. Run the FULL suite at the end, not just your three files, because `bounds.stop_requested` and
`killswitch.stop_requested` are both read by `controller.py` (owned by package CORE this round; you may read `controller.py`, never
edit it) and a meaning change could ripple.

## Problem 1: two `Bounds` classes (found by the bounds builder)
`bounds.py` (which you do NOT own this round; read it, never edit it) has `Bounds`: 8 fields, `attempts_per_card`,
`review_rounds_per_task`, `fix_cards_per_task`, `replans_per_project`, `max_cards`, `card_runtime_minutes`, `daily_reserve_percent`,
`project_wall_clock_minutes`, with STRICT `from_budgets` parsing (rejects bools, negatives, non-int, a reserve above 100). `recovery.py`
(which you DO own) has its OWN `Bounds`: 4 fields (`attempts_per_card`, `review_rounds_per_task`, `fix_cards_per_task`,
`replans_per_project`), with lenient parsing. `recovery.exhausted(lineage, bounds)` reads only those 4 fields.
- Make `recovery.py` use `bounds.Bounds` as the single source of truth: import it (`from . import bounds as bounds_mod`, matching the
  import style already used elsewhere in `recovery.py` for other sibling modules -- read the file's existing imports first), and change
  every function that currently takes a `recovery.Bounds` (`exhausted`, `decide`, anything else, read the real call sites) to accept a
  `bounds_mod.Bounds` instead. Since `bounds.Bounds` is a superset (8 fields vs. 4), this is safe: `exhausted` and `decide` only ever
  read the 4 fields they already read.
- Keep a thin, deprecated alias `recovery.Bounds = bounds.Bounds` (a module-level assignment, not a new class) so any code that still
  does `from ases.recovery import Bounds` (check: does anything outside `recovery.py` and its own tests import it that way? grep before
  you decide) keeps working, and add ONE line to the module docstring saying `recovery.Bounds` is now an alias of `bounds.Bounds`, kept
  for backward compatibility, and to prefer importing `bounds.Bounds` directly. Do NOT keep two independently-defined classes: that is
  the bug you are fixing, an alias is not a duplicate.
- `recovery.Bounds.from_budgets` no longer exists as a separate lenient implementation; callers get `bounds.Bounds.from_budgets`'s
  strict behavior. Check whether `recovery.py`'s OWN callers (inside the file, and `process_failures`) ever passed a budgets dict that
  the strict parser would now reject (a bool, a negative, a missing key it relied on defaulting differently) -- read both `from_budgets`
  implementations carefully before merging, and if the lenient one had a deliberately different default for some key, decide and
  document which behavior wins (the strict one, unless you find a concrete reason recovery.py's tests depended on the lenient one, in
  which case say so in your report rather than silently picking one).

## Problem 2: two `stop_requested` functions with different meanings (found by the bounds and killswitch builders)
`bounds.stop_requested(conn, project) -> bool` is true for `stopped` OR `paused`. `killswitch.stop_requested(conn, project) -> bool`
(which you DO own) is true only for `stopped`. `controller.py` (package CORE, not you) reads one of them to decide whether to halt a
pass; find out which one it actually calls today (`grep -n "stop_requested" src/ases/controller.py`) before deciding anything.
- The correct meaning, per the blueprint: a PAUSED project (breached a bound, `pause_and_report`) should stop new work exactly like a
  STOPPED one (`swarm stop`) -- both are "the loop must not do anything new until a human looks", which is `bounds.stop_requested`'s
  reading. A killswitch-specific need (does `killswitch.stop_all`/`resume_all` need to tell "the user explicitly stopped it" apart from
  "a bound paused it", for example to decide whether `swarm resume` should also clear a bound-triggered pause vs. only a user-triggered
  stop) is the reason two functions might legitimately need to exist with two names and two meanings -- read `killswitch.resume_all`'s
  real logic (it "refuses to resume while `report.blocked` is non-empty", per an earlier builder's note) and decide: if `killswitch.py`
  genuinely needs the narrower "stopped, not paused" check for its OWN internal logic (deciding what `resume_all` clears), keep
  `killswitch.stop_requested` but RENAME it to something that says exactly what it checks (`killswitch.stop_flag_set` or similar) so it
  is never confused with the general-purpose check again, and have `killswitch.py`'s OTHER uses of "should I still be doing work"
  (inside `stop_all`, if any) call `bounds.stop_requested` instead. If, after reading both files, you conclude `killswitch.py` never
  actually needed the narrower check for anything real, DELETE `killswitch.stop_requested` and have every caller (inside and outside
  the file) use `bounds.stop_requested`.
- Whichever you choose, leave exactly ONE function that answers "should new work be held back right now" with the stopped-or-paused
  meaning, and make its name and its callers agree. `grep -rn "stop_requested" src tests` before and after your change and paste both
  greps' worth of call sites into your report so the architect can see nothing was missed.

## Problem 3: `report.HEALTH_KINDS` is missing four event kinds that now exist (found by the MR builder)
`report.py`'s `HEALTH_KINDS` constant lists the events its health panel counts and surfaces. Round 5 added `model_mismatch` (usage.py),
`should_stop_error` (mergeq.py), `tamper_check_error` and `tamper_blocked` (review.py) -- none are in the list. Add all four. Check
whether `report.py`'s quality panel (which already shows anything whose kind CONTAINS "tamper", per the MR builder's note) would now
double-count the two tamper kinds once they are also in `HEALTH_KINDS`; if so, decide and document which panel owns them (health,
since that is what `HEALTH_KINDS` is for) and adjust the quality panel's separate "contains tamper" catch-all so it does not also list
them, unless double-listing in two panels is actually fine and intentional (read how the two panels present data before deciding; a
health-panel COUNT and a quality-panel LISTING of individual events are different things and may both be useful -- use your judgment
and say what you chose).

## Tests
`test_recovery.py`: `recovery.Bounds is bounds.Bounds` (or `recovery.Bounds` is exactly the alias, whichever you built), `exhausted`
and `decide` work identically with a `bounds.Bounds` instance as they did with the old `recovery.Bounds`, the strict-parsing behavior
change (if any) is pinned by a test that shows what used to be accepted and now is not, or that nothing changed. `test_killswitch.py`:
whatever you renamed/removed, plus every EXISTING test in the file still passes (do not silently delete a test that covered removed
behavior without confirming the behavior itself is genuinely gone, not just relocated). `test_report.py`: the four new kinds appear in
`HEALTH_KINDS` and are counted by the health panel from seeded events; the tamper double-count decision is pinned by a test either way.

## Report back
The usual report, plus the two full `grep -rn "stop_requested"` outputs (before your change, from your own working notes; after, from
the final tree), and whichever `Bounds`/`from_budgets` behavior difference you found and how you resolved it.
