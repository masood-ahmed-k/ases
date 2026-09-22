# Package TV: gate-configuration edits can no longer hide behind a wildcard touches glob, and Gate 4 gets an allowlist

Files you own: `src/ases/tamper.py`, `src/ases/finalgates.py`, `src/ases/plan.py`, `src/ases/config.py`,
`config/swarm.yaml` (a small documented addition only), and the matching test files (`tests/unit/test_tamper.py`,
`test_finalgates.py`, `test_plan.py`, `test_config.py`). Nothing else. Read `r2_rules.md`, `r5_rules.md`, `r6_rules.md` first. Package
CORE owns `gates.py`, `mergeq.py`, `bounds.py`, `controller.py` this round; do not touch them (you may read them).

## The two real problems (found by the MR and FG builders during round 5, quote these in your commit/report)
1. ASES-QG-02 (section 14.3): "A diff that changes gate configuration, CI scripts or test runner settings needs an explicit plan task
   that allows it." `tamper.analyze_diff`'s `gate_config_changed` finding is exempted by `allow_paths` (the task's own `touches`), which
   is correct for an ordinary file but wrong here: a task whose `touches` includes a wildcard glob (`*`, `**`, `src/**`) SILENTLY exempts
   every gate-config file too, because `_glob_match` (or whatever the real helper is named; read it) treats a broad glob as covering
   everything under it, including `pytest.ini` or `.github/workflows/ci.yml`. Verified by the MR builder: "a `pytest.ini` edit is
   `out_of_scope` with touches `src/*` and `ok` with touches `*`". The fix belongs at PLAN TIME (Gate 0), not at diff time: a task's
   touches must never be broad enough to cover a gate-configuration path unless that task explicitly says it is allowed to touch gate
   configuration.
2. ASES-TSK-04 / Gate 4 (section 18.2, table 24): the built-in Gate 4 tree scan (`finalgates.scan_tree`) has no allowlist, so it "fails
   on ASES's own repository (37 fake `sk-` keys in tests and docs, 8 advisory hits)" and "any repository that tracks `dist/` or
   `build/` fails permanently" (the FG builder's own words). A real project needs a way to say "this file is a known, reviewed
   exception," the same way `tamper.py`'s `allow_paths` lets a task's touches exempt a path from the DIFF-time check, but Gate 4 has no
   equivalent for the TREE-time check.

## 1. Gate 0: reject touches that are broad enough to cover gate configuration (`plan.py`)
Read `plan.py`'s `parse_and_validate` (Gate 0) and the touches-overlap/serialization logic it already has (`touches_overlap`,
`serialize_overlapping_tasks`) so you match its existing glob semantics (the MR builder found these use `fnmatch`, where `*` and `**`
are equivalent; match that, do not invent a stricter matcher that disagrees with the one already enforced at merge time).
- `GATE_CONFIG_PATTERNS`: a small, explicit constant list of path PATTERNS that count as gate/CI configuration, reusing (importing, not
  copying) whatever list `tamper.py` already has for its own `gate_config_changed` finding if one is exported; if `tamper.py` only has
  it as a private constant, add a small public export there (a one-line addition to `tamper.py`, which you own this round) rather than
  duplicating the list in two files.
- A task's `touches` entry is REJECTED at Gate 0 (a validation error, same style as the existing cycle/missing-criterion errors, naming
  the task key and the offending glob) when it matches ANY gate-config pattern UNLESS the task's `gate_profile` or a new, explicit
  per-task boolean marks it as intentionally allowed. Add the marker the way the blueprint's own plan schema would: a task field
  `allow_gate_config_changes: bool` (default false), documented in the Lead's prompt-building code in `cli.py`... you do not own
  `cli.py` this round, so do NOT edit it; instead make the schema change backward compatible (the field is optional, defaults to
  false, an existing plan.json with no such field parses exactly as before UNLESS one of its touches is broad enough to be rejected,
  which is the new behavior you are adding) and say in your report that `cli.py`'s Lead-prompt text should mention the new field, for
  the architect to add.
- A touches entry that is a NARROW, explicit match for a gate-config file (`pytest.ini` exactly, not `*`) is NOT rejected even without
  the marker: rejecting only BROAD globs (a bare `*`, `**`, or a glob whose match set demonstrably includes files outside what looks
  like the task's own directory) is the point; a task legitimately allowed to touch exactly `pytest.ini` and nothing else should not
  need the marker. Write the rule precisely and test the boundary (a `src/**` touches on a repo where `pytest.ini` lives at the root is
  NOT rejected unless `pytest.ini` itself literally matches `src/**`; a bare `**` or `*` at the plan root IS rejected because it matches
  everything).

## 2. Gate 4 allowlist (`finalgates.py`)
- `run_gate4(repo, plan, conn, head, *, runner=None, scan=scan_tree, run_gate=gates.run_gate, timeout_per_command=300,
  allow_paths=())` gains `allow_paths` (read the real current signature first and add the parameter without breaking existing
  callers/tests: give it a default of `()`). `scan_tree`/`scan_text` (read their real names) already produce `TreeFinding(kind, path,
  line, detail)`; a finding whose `path` matches one of `allow_paths` (same glob semantics as everywhere else, `fnmatch`) is dropped
  from the blocking set BEFORE `blocking()` decides whether Gate 4 fails, but is still LISTED in the outcome (as an informational
  "allowed_secret_in_tree" or similar note, never silently invisible: an allowlisted finding should still be visible in the release
  report so a human can audit what was excused).
- Where does `allow_paths` come from? Read the plan schema (`plan.py`, which you also own this round) and add an OPTIONAL top-level
  plan field, `gate4_allowlist: list[str]` (path globs), defaulting to an empty tuple when absent (an existing plan.json parses
  unchanged). `finalize()` (read its real call site) passes `plan.gate4_allowlist` through to `run_gate4`. This is a plan-author
  decision recorded in the approved, published plan (visible at Gate P, in the plan diff), not a hidden config file: the blueprint's
  security default is "a data class the router cannot override" and gates "the agent cannot edit" (table 34), and an allowlist that
  lived in `config/swarm.yaml` instead would let a WORKER (who can edit `config/swarm.yaml` if its touches allow it) quietly excuse its
  own planted secret; a plan field is controller-published and reviewed by the critic and the user at Gate P, which is the right level
  of trust for this decision.
- Add a short DOCUMENTED example to `config/swarm.yaml` (a comment block only, not a live config key, since the allowlist lives in the
  plan) explaining where `gate4_allowlist` goes and why (point at `docs/ases/plan.json`, section 18.2, ASES-TSK-04).
- `config.py`: only if your reading of `finalize()`'s real signature shows it needs a NEW `ProjectConfig` field to pass the allowlist
  through (it should not: the allowlist lives on the PLAN, not the project config, since it is plan-specific); if you find you do not
  need to touch `config.py` at all, say so in your report and leave it alone.

## 3. `tamper.py`
- Export whatever gate-config pattern list `plan.py` needs (see part 1) as a public name if it is not already one.
- Nothing else in `tamper.py` changes: `analyze_diff`'s `gate_config_changed` finding keeps behaving exactly as it does today (still
  exemptable by `allow_paths` for genuinely narrow, explicit touches); the fix is that Gate 0 now refuses to let a task's touches be
  broad enough for that exemption to matter for gate-config paths. Do not weaken or remove the diff-time finding.

## Tests
`test_plan.py`: a task whose touches is a bare `*`/`**` and covers a gate-config file is rejected at Gate 0 with a clear error naming
the task and the file; the same touches WITH `allow_gate_config_changes: true` is accepted; a narrow, explicit touches on exactly one
gate-config file is accepted without the marker; a touches that does not reach any gate-config file is unaffected; an existing plan.json
fixture (no `allow_gate_config_changes` field anywhere) still parses. `test_tamper.py`: the exported pattern list, no behavior change
to `analyze_diff` itself (existing tests still pass unmodified). `test_finalgates.py`: `run_gate4(allow_paths=...)` drops an allowlisted
finding from `blocking()` but keeps it in the outcome's findings list with a distinct kind/note; Gate 4 now PASSES on a scan tree
containing only allowlisted secret-shaped files, and still FAILS on one containing a non-allowlisted one; the plan's `gate4_allowlist`
field flows through `finalize()` end to end (a fake `run_gate4` spy showing what it was called with is fine, or the real one on a
temp repo). `test_config.py`: only if you actually changed `config.py`; if not, no new tests needed there and say so.

## Report back
The usual report, plus: run Gate 4's `scan_tree` against ASES's OWN repository (read-only, no writes) with an allowlist that covers
`tests/` and `docs/` sample-key patterns you find, and confirm it now passes; list exactly which paths you had to allowlist.
