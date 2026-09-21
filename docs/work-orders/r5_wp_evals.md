# Package EV: the evaluation harness (phase 7, blueprint Appendix D)

Files you own: `src/ases/evals.py` (new; if it grows past about 1,500 lines split helpers into `src/ases/evalkit/` with an
`__init__.py`, your choice), `tests/unit/test_evals.py` (new). Nothing else. Read `r2_rules.md`, `r5_rules.md` and `r5_contracts.md`
first. Also read `benchmarks/allocate/` in the repository: it is a working example of a hidden-test benchmark with seeded mutants
(`eval_arm.py`, `hidden/`, `reference/`) that scored real agents earlier; reuse its ideas, do not import from it.

## Requirements (blueprint.txt [p458] to [p463], [p499], [p504], [p118] to [p121] section 5.1, ASES-MOD-06, ASES-VER-01, 22.17)
- Appendix D: "Do not trust a static list of free models. Build a tiny evaluation harness. Replay the same tasks against candidate models
  through different providers. Store raw results locally. Evaluation spends real quota, so Phase 2 runs only E1, E9 and E10 on a short
  list, and the full set waits for Phase 7." The tasks: E1 Requirements, E2 Architecture, E3 Repository understanding, E4 Debugging,
  E5 Refactor, E6 Test design, E7 Security, E8 Long-horizon task, E9 Tool use, E10 Review (find an intentionally seeded bug), E11 Role
  value (does a separate role profile beat the core roster on the same tasks).
- D.2 Metrics: "Track task success, tests passed, review changes required, number of retries, fallback count, latency, requests per
  merged task, approximate token usage, and human interventions. Do not collapse these into a single magic score. Keep the individual
  measurements so you can see why a model succeeds or fails."
- Phase 7 row of the roadmap: "All evaluation tasks, replay across candidates, regression check before changing a pinned model.
  Exit: Evaluation report."
- ASES-MOD-06: "Dynamic free routing is not used for Lead, Reviewer or Debugger". ASES-CAP-03: never start work the provider budget cannot
  afford. ASES-OBS-02: transcripts and raw results stay local. ASES-SEC-01: redact secrets in everything stored.
- STOP CONDITION (ASES-DOC-04): evaluation spends real quota. Nothing in this module may call a model unless the caller passed an
  explicit `spend=True` (the CLI flag is `--spend-quota`); the default is a dry run that prints what WOULD be run and how many requests
  it would cost. Your tests never call a model: every model call goes through an injectable `invoke`.

## Design
The harness is data plus a runner. A task is a small repository fixture generated in code (no big files committed), a prompt, and a
deterministic scorer. A run is (task, candidate) where a candidate is (provider, model, profile-or-role).

Build `evals.py` with:
1. `Candidate` (frozen: provider, model, role_class, profile, label) and `EvalTask` (frozen: id "E1".."E11", title, kind in
   {"text", "repo"}, `build_fixture(workdir: pathlib.Path) -> dict` making the seeded repository or context, `build_prompt(fixture) -> str`,
   `score(fixture, workdir, output: str, invoke_result) -> Score`, `est_requests: int`).
2. `Score` (frozen: success bool, tests_passed int or None, tests_total int or None, findings dict[str, int|bool|str], notes str) and
   `RunRecord` (task id, candidate label, run id, started_at, latency_seconds, requests, input_tokens, output_tokens, retries,
   fallbacks, review_changes_required, human_interventions, score, raw_output_path). All the D.2 metrics, separately.
3. The tasks E1 to E11 as real, small, deterministic fixtures with scorers that need no model:
   E1: a vague requirement paragraph; success = the answer lists explicit assumptions covering a fixed set of ambiguity topics (a keyword
   scorer over normalized text, documented as a heuristic). E2: a small spec; success = the answer names the required components and their
   interfaces from a checklist. E3: a generated multi-file repository plus questions with known answers; success = fraction of answers
   found. E4: a tiny Python package with one failing test; the model returns a unified diff or replacement file; success = the test passes
   after applying it in a temp copy (run pytest through `sys.executable`). E5: a refactor request with a passing test suite; success = the
   suite still passes AND a structural check (a function extracted, a name changed) holds. E6: a module with untested edge cases; the model
   returns tests; success = the new tests pass on the reference and FAIL on at least N of M seeded mutants of the module (reuse the mutant
   idea of `benchmarks/allocate/eval_arm.py`). E7: a small code sample with K seeded vulnerabilities; success = at least a threshold of
   them named (keyword scorer). E8: multi-step task scored by a sequence of checks; it needs the whole swarm, so implement it as a
   DESCRIPTOR only (`kind="swarm"`) whose `score` reads a finished project's merge records and review counts; the runner refuses to run
   it standalone and says how (`swarm run` on a throwaway repo). E9: tool use; a fixed set of fake tools described in the prompt, the model
   must emit a JSON tool call; success = valid call with the right arguments after at most one corrected retry (score the trace).
   E10: a diff with one seeded bug; success = the review names the file and the defect class. E11: a comparison, not a task: see 8.
4. `estimate(tasks, candidates) -> Estimate` (total requests, per provider, per candidate) and `check_budget(conn, models_config, estimate)
   -> list[str]` using `policy.check_budget`/`ledger` so an evaluation that the day's quota cannot afford is refused before it starts.
5. `run_eval(tasks, candidates, *, invoke, workroot, out_dir, models_config=None, conn=None, spend=False, now=None, sleep=time.sleep)
   -> RunSummary`: `spend=False` returns the plan without calling anything; with `spend=True` it builds each fixture in its own temp
   directory, calls `invoke(candidate, prompt, workdir, timeout) -> InvokeResult(returncode, stdout, stderr, latency_seconds, requests,
   input_tokens, output_tokens)`, scores, writes raw results under `out_dir/<run id>/` as `results.jsonl` (one `RunRecord` per line plus
   the redacted raw output in `raw/<task>-<candidate>.txt`) and `summary.json`, and records the requests in the ledger when `conn` is
   given. One failing run never stops the rest (a `RunRecord` with success False and the error). Pacing between calls uses the provider's
   declared rate limit (`policy.estimate_calendar_minutes` or the provider's `per_model_rpm`).
6. `default_invoke(candidate, prompt, workdir, timeout)`: runs the one-shot Hermes call `hermes -p <profile> -z <prompt> -m <model>
   --provider <provider>` (read `hermes chat --help` in the Hermes source to get the flags right and cite them) with `-t` naming the
   tools the task needs, UTF-8, a timeout that never raises, and reads the request count and tokens from `hermes.session_usage` when the
   session id can be found, else 1 request and unknown tokens. Never used by tests.
7. Reports: `load_run(dir) -> list[RunRecord]`, `render_report(records) -> str` (Markdown, ASCII, one table per metric family per
   candidate x task, NO combined score), `compare(candidate_run, pinned_run, *, tolerance) -> list[Regression]` implementing the
   regression check ("regression check before changing a pinned model"): a candidate must not be worse than the pinned model on any
   task's success, and requests or latency may not grow past `tolerance` (default 25 percent); each Regression names task, metric,
   pinned value and candidate value. `role_value(run_with, run_without) -> RoleValue` for E11 (success rate and requests per merged task
   with and without the role, as plain numbers).
8. `recommend(records, models_config) -> list[str]`: plain-English recommendations (never edits config): which candidate to consider for
   which role class given success and cost, never a router for lead, reviewer or debugger (ASES-MOD-06), and always the sentence that
   pinning a model is the user's decision.
9. `main(argv: list[str]) -> int`, the entry point behind `swarm eval`: subcommands `list` (tasks and their request estimates),
   `run --tasks E1,E9,E10 --candidates a,b [--spend-quota] [--out DIR]` (dry run without the flag; prints the estimate and how to
   confirm), `report RUN_DIR`, `compare CANDIDATE_RUN PINNED_RUN [--tolerance 0.25]`. Candidates are looked up in `config/models.yaml`
   through the real config loader by `provider/model` label; an unknown label is an error, not a guess. Exit codes: 0 ok, 1 usage or
   refusal, 2 regression found by `compare`.

## Tests (`tests/unit/test_evals.py`; a fake `invoke` everywhere; temp dirs; no network)
Each task's fixture builds and its scorer accepts a known-good answer and rejects a known-bad one (E4 and E6 run real pytest in temp
dirs: keep them fast); the estimate and the budget refusal; dry run calls nothing (assert the fake invoke was never called); `spend=True`
writes `results.jsonl`, `summary.json` and the redacted raw files (plant a secret-shaped value in the fake output and assert it is
absent from disk); one failing run does not stop the rest; the ledger records requests; the report has no combined score and is ASCII;
`compare` flags a success regression and a cost regression and passes an equal run; `role_value`; `recommend` never proposes a router for
a protected role; `main` subcommands, unknown candidate, the exit codes; E8 refuses standalone execution with the instruction.
