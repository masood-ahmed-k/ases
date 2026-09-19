# Package TM: the tamper check, generated-artifact and secret checks, and a runner hook for gates

Files you own: `src/ases/tamper.py` (new), `src/ases/gates.py` (edit, keep every existing public name and behaviour that
`tests/unit/test_gates.py` and `tests/unit/test_mergeq.py` rely on), `tests/unit/test_tamper.py` (new), `tests/unit/test_gates.py`
(extend). Nothing else. Read `r2_rules.md` first (shared rules apply; ignore its "baseline 669": the suite baseline is whatever it
shows before you start and must never go down; other agents this round own sandbox.py, leases.py and guards.py, and finalgates.py).

## Requirements (read blueprint.txt around [p272] to [p280], [p371] to [p377], test 22.12 at [p421]/[p422])
- ASES-QG-03 (section 14.3): "The tamper check fails Gate 1 when a diff deletes or skips existing tests, adds unconditional passes such
  as || true, weakens assertions in files it did not need to touch, or lowers coverage of the changed area beyond the configured
  tolerance."
- ASES-QG-02: "A diff that changes gate configuration, CI scripts or test runner settings needs an explicit plan task that allows it."
  (The pin check of the gate PROFILES exists already; this is the diff side.)
- ASES-GIT-07 (section 8.1): "No generated artifacts or secrets in commits; secret scan in Gates 1 and 3."
- ASES-SEC-01: the secret scan; do not print a secret value in any finding (show the file and a short redacted hint only).
- ASES-QG-04: gates run in a clean checkout of the exact commit, never the live directory; that already holds (gates.run_gate uses a
  throwaway worktree). The sandbox package will supply a runner that executes the commands inside Docker; you provide the hook.
- Test 22.12: "The fake worker deletes a failing test, then adds a skip marker, then appends || true to the test command, then edits a file
  outside its allowed paths, then leaves an untracked file that would make the build pass. Each attempt must fail Gate 1 with the right
  finding." (Path scope and the untracked-file case are already handled by review.py and by the clean checkout; yours are the first
  three plus config, artifacts and secrets.)

## Build `tamper.py`
1. `Finding` frozen dataclass: kind (str), path (str), detail (str), line (int or None). `kind` is one of `test_file_deleted`,
   `test_deleted`, `skip_marker`, `unconditional_pass`, `assertion_weakened`, `gate_config_changed`, `generated_artifact`,
   `secret_added`, `large_file`, `coverage_lowered`.
2. `parse_diff(diff_text) -> list[FileDiff]` (pure): a small unified-diff parser for `git diff --no-renames` output (also tolerate
   `--no-color` absence: strip ANSI): `FileDiff(path, old_path, status, added_lines, removed_lines, hunks)` where status is A, M or D
   (from `new file mode`, `deleted file mode`, /dev/null headers), added_lines and removed_lines are lists of (line number or None,
   text) and hunks keep the header context text (git prints the enclosing function or section after `@@`). Handles: binary files
   ("Binary files differ" gives an entry with no lines), paths with spaces (the `+++ b/...` line is authoritative, do not split the
   `diff --git` line on spaces), quoted paths with escapes, a diff with no trailing newline, an empty diff.
3. `analyze_diff(diff_text, *, allow_paths=(), gate_config_paths=()) -> list[Finding]` (pure, never raises; malformed input gives an
   empty list or fewer findings, never an exception):
   - `test_file_deleted`: a deleted file (status D) that looks like a test file (path components `tests`, `test`, `__tests__`, `spec`;
     names `test_*.py`, `*_test.py`, `*_test.go`, `*.test.js`, `*.test.ts`, `*.test.tsx`, `*.spec.js`, `*.spec.ts`, `*Test.java`,
     `*_spec.rb`).
   - `test_deleted`: in a modified test file, a removed test definition (`def test_x`, `async def test_x`, `it("..."`, `test("..."`,
     `func TestX`, `@Test` followed by a method, `#[test]` followed by `fn x`, `public void testX`) whose name does not reappear among that
     file's added lines (a rename shows as delete plus add: not a finding when the added lines define a test, but a removal with NO test
     definition added in the file is). Name the removed test in the detail.
   - `skip_marker`: an ADDED line containing any of: `pytest.mark.skip`, `pytest.skip(`, `pytest.mark.xfail`, `unittest.skip`,
     `@skip`, `skipif(`, `skipUnless`, `it.skip(`, `test.skip(`, `describe.skip(`, `xit(`, `xdescribe(`, `xtest(`, `t.Skip(`, `#[ignore]`,
     `@Ignore`, `@Disabled`, `.only(`, `fit(`, `fdescribe(`, `--deselect`, `-k "not`. Case-insensitive for the words; report the marker.
   - `unconditional_pass`: an ADDED line containing `|| true`, `|| :`, `; true` at the end of a command, `set +e`, `exit 0` as the last
     word of a script line inside a file that is a script or a CI file, `--passWithNoTests`, `continue-on-error: true`,
     `allow_failure: true`, `if: false`, `assert True`, `assert 1`, `assert not False`, `self.assertTrue(True)`, `expect(true).toBe(true)`,
     `assert 1 == 1`, `pass  # test`, and `|| exit 0`. (Some of these overlap `assertion_weakened`; report each line once with the most
     specific kind.)
   - `assertion_weakened`: in a test file, per hunk: more removed assertion lines than added ones (`assert `, `self.assert`, `expect(`,
     `.should`, `require.`, `assert_eq!`, `t.Error`) reports the counts; and a removed `assert a == b` style line replaced by an added
     weaker one (`is not None`, `is not False`, `>= 0`, `!= None`, `toBeDefined`, `toBeTruthy`, `assert x`) reports both lines, truncated.
     Only in files that are tests OR in files not listed in `allow_paths` ("in files it did not need to touch": a path matched by an
     `allow_paths` glob is the task's own territory and is exempt from `assertion_weakened` but NOT from `skip_marker`, `test_deleted`,
     `unconditional_pass`, or `test_file_deleted`).
   - `gate_config_changed` (ASES-QG-02): any change to a path in this built-in list, unless the path matches `allow_paths` (the task's
     touches allow it explicitly): `.github/workflows/*`, `.gitlab-ci.yml`, `azure-pipelines.yml`, `Jenkinsfile`, `.circleci/*`,
     `pytest.ini`, `tox.ini`, `setup.cfg`, `pyproject.toml`, `conftest.py` (anywhere), `.coveragerc`, `noxfile.py`, `Makefile`,
     `package.json` (only when a hunk touches the `scripts`, `jest`, or `test` keys: check the changed lines for `"test"`, `"scripts"`,
     `"jest"`), `jest.config.*`, `vitest.config.*`, `karma.conf.*`, `.pre-commit-config.yaml`, `Cargo.toml` only when a `[profile` or
     `[lints` line changed, `go.mod` no; plus every path in `gate_config_paths` (paths the approved gate profile commands name). Glob
     matching uses the same semantics as the touches globs elsewhere: read `review._check_scope` and `plan.touches_overlap` to match them
     (a `**` crosses directories, `*` does not).
   - `generated_artifact` (ASES-GIT-07): an ADDED file (status A) under `__pycache__/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`,
     `node_modules/`, `dist/`, `build/`, `.venv/`, `venv/`, `htmlcov/`, `target/` (only with a `Cargo.toml` sibling is NOT needed: flag it),
     `*.egg-info/`, or named `*.pyc`, `*.pyo`, `*.log`, `.DS_Store`, `coverage.xml`, `.coverage`, `*.sqlite`, `*.sqlite3`, `*.db`,
     `.env`, `.env.*` (except `.env.example` and `.env.sample`), `*.pem`, `*.key`, `id_rsa*`, `id_ed25519*`, `*.p12`, `*.pfx`;
     unless an `allow_paths` glob names it explicitly.
   - `secret_added`: reuse the secret patterns (see 5) on ADDED lines only; the detail names the file and line and NEVER contains the
     matched text (say "secret-shaped value"); a `+++` header is not an added line.
4. `check_range(repo, base, head, *, allow_paths=(), gate_config_paths=(), max_file_bytes=1_000_000, timeout=60) -> list[Finding]`: runs
   `git -C repo diff --no-renames --no-color -U3 <base>...<head>` (three dots: changes since the merge base; look at how
   review._check_scope and review._changed_since pick their range and match it) and `git diff --name-status --no-renames` for the status,
   feeds `analyze_diff`, then adds `large_file` findings for added or modified files bigger than `max_file_bytes` (`git cat-file -s
   <head>:<path>`, skip on failure). When git itself fails (bad range, missing repository, timeout) raise `TamperCheckError` (a plain
   Exception subclass) so a gate that could not run is never a silent pass; the caller decides what to do with it.
5. `format_findings(findings, *, limit=20) -> str`: ASCII-only, one line per finding `kind path[:line]: detail`, with "... and N more"
   when capped; backslash-escapes any non-ASCII so a Windows console cannot crash; never longer than about 3000 characters.
6. `blocking(findings) -> list[Finding]`: everything except `coverage_lowered` below tolerance (all others block Gate 1).
   `coverage_check(before, after, tolerance_points=1.0) -> Finding | None`: pure numeric helper for QG-03's coverage clause (None when
   inside tolerance or when either number is None).

## Edit `gates.py`
- Keep `detect_tamper(diff_text) -> list[str]` and `scan_for_secrets(diff_text) -> list[str]` working with the same return type and the
  same messages where existing tests assert them (read tests/unit/test_gates.py and tests/unit/test_mergeq.py first); `detect_tamper`
  should now delegate to `tamper.analyze_diff` and render each finding as one string (`format`-style), keeping the old cheap markers
  covered. `scan_for_secrets` keeps scanning added lines with the events patterns; extend it so an added FILE whose name is a secret
  file name (`.env`, `*.pem`, `id_rsa`) is reported too, never echoing a value.
- `run_gate(...)` gains an optional keyword `runner=None`: a callable `runner(worktree: pathlib.Path, commands: list[str], timeout: int)
  -> tuple[bool, str]` used instead of the local `_run_commands` when given (this is how the sandbox package runs the commands in a
  container; the default behaviour is unchanged). Everything else about run_gate stays as it is (throwaway worktree at the exact commit,
  cleanup in `finally`, the gate_runs row with pass or fail).
- `run_gate` must strip nothing else and keep recording exactly the same columns.

## Tests
`tests/unit/test_tamper.py`: parse_diff on hand-written diffs (added, modified, deleted, binary, spaces in a path, quoted path, no
trailing newline, empty); each finding kind in both directions (a real change that must be flagged, a look-alike that must NOT: e.g.
`# skip this step` in prose, `pytest.mark.parametrize`, a deleted test that reappears renamed, an allowed config path, `.env.example`);
the exact test-22.12 sequence built with REAL temp git repos (commit a test, then commits that: delete the failing test, add
`@pytest.mark.skip`, append `|| true` to a test command in a script, edit a config path) and `check_range` returning the right kind for
each; `check_range` on a clean diff returns []; `check_range` with a bad range raises TamperCheckError; secret findings never contain
the secret text (assert the planted value is absent from `format_findings`); `format_findings` ASCII and capped; `coverage_check`.
`tests/unit/test_gates.py` (extend): `detect_tamper` delegating still flags the old markers; `scan_for_secrets` on an added `.env`
file and an added `.pem`; `run_gate(..., runner=fake)` uses the runner (assert `_run_commands` not used) and still records the row and
cleans the worktree up; default runner behaviour unchanged.
