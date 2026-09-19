# Package FG: Gates 4 and 5 as controller lifecycle operations, and the release report

Files you own: `src/ases/finalgates.py` (new), `tests/unit/test_finalgates.py` (new). Nothing else. Read `r2_rules.md` first (shared rules
apply; ignore its "baseline 669": the suite baseline is whatever it shows before you start and must never go down). By the time you
start, these modules from the previous round EXIST and are yours to import and read (do not edit them): `bounds.py`
(`record_final_gate`, `final_gates_green`, `mark_release_report`, `release_report_written`, `is_finished`, `finish_project`, project state
helpers), `report.py` (`build_report`, `render_text`, `render_html`, `write_report`), `questions.py`, `recovery.py`, `critic.py`,
`killswitch.py`, `reconcile.py`, `intents.py`, plus `tamper.py`, `sandbox.py` and `gates.py` (with the new `runner=` hook of `run_gate`).
Read their real signatures before you call them, and match what you find rather than what this file guesses.

## Requirements (read blueprint.txt around [p272], [p336] to [p339], the Gate 4 and Gate 5 rows of table 24, [p368] on the final report)
- ASES-TSK-04 (section 18.2): "Final integration security and smoke gates are controller lifecycle operations, not a worker": "After T9, T10
  and T11 are merged: controller runs Gate 4, then Gate 5, then writes the final report." ("T10 and T11 are later review/test phases, not an
  agent named controller.")
- Table 24: "Gate 4: security | Before final release, on the integration HEAD | Controller | Secrets scan, dependency audit (hermes security and
  ecosystem audit tools), auth checks, obvious injection paths"; "Gate 5: smoke | Final | Controller | Start the app, hit the health endpoint,
  run a representative user flow (hermes verify can detect the run recipe)".
- ASES-CTL-01 (section 9.3): "A project is finished when every merge card is done, Gates 4 and 5 are green on the integration HEAD, and the
  release report is written."
- ASES-QG-01: the controller believes only its own gate records; each gate result is stored by commit SHA (use `bounds.record_final_gate`).
- ASES-SEC-01 / ASES-GIT-07: secrets never in commits; the tree scan reports file and line, never the value.
- ASES-OBS-01/02: the release report is local files only.

## Design
Gates 4 and 5 run on the exact integration HEAD in a clean throwaway worktree (reuse `gates.run_gate` for command execution so the sandbox
runner hook applies; its `task_key` argument is the string `"__final__"` and the gate name `gate4` or `gate5`, which is what
`bounds.record_final_gate` and `bounds.final_gates_green` expect: read bounds.py to see exactly how it stores rows and DO NOT double
insert: if `run_gate(conn=conn, task_key="__final__")` already records the row, use that; otherwise call `record_final_gate`, one or the
other, never both).
The plan may define gate profiles named `gate4` and `gate5` (lists of shell commands, for example `pip-audit` or `npm audit --audit-level=high`
for Gate 4, and a command that starts the app and probes a health endpoint for Gate 5). The controller ALWAYS adds its own built-in checks.

## Build `finalgates.py`
1. `TreeFinding` frozen dataclass (kind, path, line or None, detail) and `scan_tree(repo, ref, *, timeout=120) -> list[TreeFinding]`
   (Gate 4's built-in secrets and hygiene scan): list the tracked files at `ref` with `git ls-tree -r -z --name-only <ref>`, flag any tracked
   file whose name is a secret file (`.env`, `.env.*` except `.env.example` and `.env.sample`, `*.pem`, `*.key`, `id_rsa*`, `id_ed25519*`,
   `*.p12`, `*.pfx`, `*.kdbx`, `credentials.json`), any generated artifact (reuse the lists in `tamper.py` if it exposes them, else
   `__pycache__`, `node_modules`, `*.pyc`, `dist/`, `build/`, `.venv/`), and scan the text of every tracked text file up to 1 MB
   (`git show <ref>:<path>`, skip binary by NUL byte, skip files over the cap and say so in a `skipped` note) with the same secret patterns as
   `events.redact`/`gates.scan_for_secrets` (read them: one place defines what a secret looks like), reporting path and line with the value
   NEVER echoed. Also flag "obvious injection paths" with a SMALL fixed rule list, each a finding of kind `injection_pattern` (severity
   advisory, see 2): Python `subprocess.*(..., shell=True)` with an f-string or `%`/`.format` argument, `os.system(` with a formatted string,
   `eval(` or `exec(` on a non-literal, `pickle.loads(`, `yaml.load(` without `SafeLoader`, string-concatenated SQL passed to `execute(`; JS
   `eval(`, `new Function(`, `child_process.exec(` with a template literal, `innerHTML =` with a template literal. Keep them regex-based and
   documented as heuristics. Never raise: a git failure returns a single finding of kind `scan_error`.
2. `Severity` handling: `secret_in_tree`, `secret_file_tracked`, `generated_artifact_tracked`, `scan_error` are BLOCKING; `injection_pattern`
   is ADVISORY (listed in the report, does not fail the gate). `blocking(findings)`.
3. `GateOutcome` frozen dataclass (gate, commit_sha, passed, detail, findings tuple, notes tuple).
4. `run_gate4(repo, plan, conn, head, *, runner=None, scan=scan_tree, run_gate=gates.run_gate, timeout_per_command=300) -> GateOutcome`:
   built-in scan first (blocking findings fail the gate WITHOUT running the plan's commands, saying so), then the plan's `gate4` profile
   commands when present through `run_gate` on `head` (`task_key="__final__"`, gate name `gate4`), recording ONE combined final row via
   the one mechanism described in Design. When the plan has no gate4 profile the built-in scan alone decides and the detail says
   "no gate4 profile in the plan: built-in scan only" (a note, not a failure).
5. `run_gate5(repo, plan, conn, head, *, runner=None, run_gate=gates.run_gate, timeout_per_command=300) -> GateOutcome`: the plan's `gate5`
   profile commands when present; otherwise the smoke fallback is every DISTINCT command of every gate profile in the plan, in the order they
   first appear, run on the integration HEAD (for a library this is the full test suite), with the note "no gate5 profile in the plan: ran
   every task gate profile on the integration HEAD". A plan with no profiles at all fails Gate 5 with a clear message (nothing to run is not a
   pass).
6. `release_summary(board, plan, project, models_config, conn, head, *, gate4, gate5, now=None) -> dict`: plain JSON-serialisable data for the
   release report: project name, integration branch, head SHA, generated_at, per plan task {task key, title, role, work card id, merge card
   id, squash commit or None (from merge_records), gate3 result}, the two final gate outcomes (status, notes, finding counts by kind, never
   values), the request budget used per provider (from the ledger via `report.build_report` when it returns it, else recompute), the bounds
   reached, questions asked and answered (count of `question_answered` events), re-plans used, and the count of `recovery_decision` and
   `reconcile_repair` events. Everything passed through `events.redact` before it is returned.
7. `write_release_report(summary, report, directory) -> pathlib.Path`: writes `release.md` (plain text, ASCII only, headed sections), and
   also calls `report.write_report(report, directory)` so `report.html` and `report.json` sit beside it (skip that call and note it when
   `report` is None); returns the path of `release.md`. Creates the directory. UTF-8 file, ASCII content.
8. `finalize(board, repo, plan, project, models_config, conn, *, now=None, run4=run_gate4, run5=run_gate5, build=report.build_report,
   is_finished=bounds.is_finished, ...) -> FinalizeResult`: the single lifecycle step the controller calls once every merge card is done:
   (a) refuse and return `FinalizeResult(status="not_ready", reason=...)` when a merge card is not done (`hermes.kanban_show` each, or use
   `controller.all_merge_cards_done` semantics: do not import controller.py, it imports you later), (b) read the integration branch HEAD
   (`git rev-parse <integration_branch>`; a failure returns status "error"), (c) skip gates already green for that exact HEAD
   (`bounds.final_gates_green` for both means both are skipped; a green gate4 with no gate5 runs only gate5), (d) run Gate 4, and only if
   it passed run Gate 5 (record the failing one, stop), (e) when both are green write the release report into
   `<project.ases_home>/reports/<project name>/<UTC timestamp>/` (use the attribute the ProjectConfig really has: read config.py; fall back
   to `<repo>/../ases-reports` never inside the repository, so a report never dirties the primary checkout), call
   `bounds.mark_release_report(conn, project name, path)`, (f) call `bounds.finish_project(...)` and return
   `FinalizeResult(status="finished"|"gate_failed"|"not_ready"|"error", gate4, gate5, report_path, reason)`. Each step records an event
   (`final_gate_started`, `final_gate_result`, `release_report_written`, `project_finished`) and writes intent records with
   `intents.begin/complete` around the gates and the report so a crash mid-way is visible to reconcile. Idempotent: calling it again after
   success runs nothing and returns status "finished".
9. A failed final gate must leave a card-shaped trail for the human: `finalize` returns the failing outcome; the controller decides how to
   surface it (it will block the project with a question). Provide `final_gate_question(outcome) -> str`, a short plain-English question
   that names the gate, the first few finding lines (values never shown) and what the user can do next.

## Tests (`tests/unit/test_finalgates.py`; real temp git repos, temp DB, monkeypatch hermes; fake `run_gate` where needed)
scan_tree: a clean repo; a tracked `.env`; `.env.example` allowed; a tracked `.pem`; a secret-shaped value in a text file reports path and
line and never the value (assert the planted value is absent from every finding string); a binary file skipped; an oversized file skipped
with a note; each injection pattern flagged as advisory and not blocking; a git failure gives scan_error. run_gate4: built-in scan blocks
before plan commands run; plan commands run through the injected run_gate and their failure fails the gate; no gate4 profile note; one
combined recorded row. run_gate5: the plan profile; the fallback union of distinct commands in first-seen order; no profiles at all fails.
release_summary: content, per-task squash commits from merge_records, redaction of a secret-shaped value planted in an event payload.
write_release_report: files created, ASCII only, directory created, report.html and report.json beside it. finalize: not_ready with one
merge card not done; gate4 red stops before gate5; both green writes the report, marks it, finishes the project and status is finished;
idempotent second call runs nothing; a green gate4 for the exact head is not re-run; an integration branch that moved since a previous
green run re-runs both; a git failure gives status error; events and intent records are written and completed. final_gate_question never
contains a secret value.
