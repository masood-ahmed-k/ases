# Rules that apply to every ASES work package (read first, then read your own package file)

You are building one self-contained module of the ASES swarm controller: Python 3.11, repository
`C:\Users\masoo\ases`, git branch `master`. The controller sits on top of Hermes Agent 0.21.3 (Kanban board,
profiles, dispatcher) and drives a Lead, coders and a Reviewer through Gates to a merge queue.

## Hard rules
- Do NOT commit, push, stash, checkout, reset, or touch the git config of the ASES repo. The architect commits.
- Edit or create ONLY the files your package lists. Other agents are editing OTHER files in parallel. If a full-suite
  failure is clearly in a file you do not own (an import error, an AttributeError from someone else's module), wait a
  minute and re-run before deciding, and say so in your report.
- Do NOT edit docs/, spec/, config/, the ASES database `data/ases.db`, or anything outside the repository, and do NOT
  touch the user's Hermes installation, profiles, boards, or `config.yaml`. No network calls, no LLM/API calls, no
  Docker. Tests use temp SQLite databases (`db.connect(tmp_path / "ases.db")`), temp git repos, and monkeypatching of
  the `hermes` module functions only.
- Do NOT run mutation testing (the architect does that afterwards).
- Windows trap: on Windows `os.kill(pid, 0)` does NOT probe a process, it TERMINATES it. Never call `os.kill` with any
  signal on Windows. To test whether a PID is alive use `ctypes` (`OpenProcess` with PROCESS_QUERY_LIMITED_INFORMATION,
  then `GetExitCodeProcess`, exit code 259 = STILL_ACTIVE) or `tasklist /FI "PID eq N" /FO CSV /NH`; to terminate a
  process tree use `taskkill /PID N /T /F`. Every such helper must be injectable so tests never touch a real process.
- Output that a person will read in a terminal must be ASCII only (the Windows console is cp1252 and crashes on a
  non-ASCII character such as an arrow). Escape or replace anything else that comes from a card title or agent text.

## Style
- Match the surrounding code: docstrings that say WHY, requirement IDs quoted in the docstring of the function that
  satisfies them, comment density like the neighbouring modules (read two of them first).
- Never use the em dash character or the section-sign character anywhere (code, comments, docstrings, tests, strings).
  Use a comma, a colon, parentheses or a plain hyphen. Write "section 19.4" instead of the sign.
- Run tests only through the compressor, from `C:\Users\masoo\ases`:
  `python C:/Users/masoo/.claude/scripts/quiet.py -l pytest -- python -m pytest -q --tb=short`
  If a failure needs the full traceback, re-run the bare command with `-x` on that test.

## The requirement source
The authoritative document is the blueprint (v1.2). Its full text, one paragraph per line with ids like `[p355]`,
tables rendered as text, is at
`C:/Users/masoo/AppData/Local/Temp/claude/C--Users-masoo-OneDrive-Desktop-AISES/1c797c27-23b9-42e6-808e-abd898fdf3ec/scratchpad/spec/blueprint.txt`
(use grep for an id such as `ASES-REC-05`, and read the paragraphs around it). The requirement register is
`spec/requirements.yaml` in the repo (read your rows; the note says what is and is not done). Where your package text
and the blueprint disagree, the blueprint wins: build to the blueprint and tell me about the difference.

## What already exists (read the code, do not rebuild it)
- `src/ases/hermes.py`: the ONLY module that talks to Hermes. `kanban_show(board, id)` returns the flat card dict plus
  `_children`, `_parents`, `_runs` (list of dicts: id, profile, status, outcome, summary, error, metadata, started_at,
  ended_at, worker_pid), `_events` (list of {kind, payload, created_at, run_id}), `_comments` ({author, body,
  created_at}) and `_latest_summary`. Also `kanban_list(board, status=, assignee=)`, `kanban_create`, `kanban_link`,
  `kanban_dispatch`, `kanban_complete`, `kanban_block(board, id, reason)`, `kanban_schedule(board, id, reason)`,
  `kanban_unblock(board, id, reason=None)`, `kanban_comment(board, id, text, author=None)`,
  `kanban_promote`, `kanban_archive(board, ids)`, `kanban_set_model(board, id, model, provider=)`,
  `kanban_reclaim(board, id, reason=)`, `kanban_reopen_review`, `pause(reason)`, `resume()`, `session_usage(profile, sid)`,
  and `kanban_specify(board, id, author=, timeout=120)` (round 7, 2026-09-22, "option A": this one auxiliary-model call
  is now made for real, but ONLY from `triage.promote_card`, never anywhere else; see r7_rules.md).
  A card's status is one of triage, todo, scheduled, ready, running, blocked, review, done, archived.
- `src/ases/events.py` (`record(conn, kind, payload)`, `redact`, `recent`), `ledger.py`, `policy.py`
  (`resolve_assignee`, `profile_provider(role, models_config)`, `check_budget`), `config.py` (`ProjectConfig`:
  name, environment, data_class, workspace_root, ases_home, board, integration_branch, roles {role: profile},
  concurrency, budgets {attempts_per_card, review_rounds_per_task, fix_cards_per_task, replans_per_project,
  max_cards, card_runtime_minutes, daily_reserve_percent, review_reserve_requests}, ...), `plan.py` (`Plan`,
  `PlanTask`, `parse_and_validate`, `load_plan_file`, `topological_order`), `gates.py` (`run_gate`, `scan_for_secrets`,
  `detect_tamper`), `mergeq.py` (`merge_task`, `revert_merge`), `review.py`, `usage.py`, `guards.py`, `controller.py`
  (`process_*` functions and `run_pass`; you must NOT edit it, the architect wires your module in).
- Database schema v5 is in `src/ases/db.py` (do NOT edit): requests_ledger, model_registry, events, plan_tasks
  (project, task_key, work_card_id, merge_card_id, role, touches, gate_profile, estimated_requests, fix_cards),
  gate_runs, merge_records, gate_pins, usage_ingested (with project, task_key, card_id), review_verdicts,
  integrity_state, lineage (project, task_key, review_rounds, capability_failures, infra_failures, replans, seen_card,
  seen_events, updated_at), project_state (project, started_at, deadline_at, replans, status, stop_reason,
  updated_at), intents (id, project, kind, key, detail, started_at, completed_at).
- Real Hermes facts (probed 2026-09-19): a block adds a "BLOCKED: <reason>" comment by author "default" and a
  `blocked` event whose payload has `reason`; an unblock with a reason adds "UNBLOCK: <reason>". Merge cards are
  created `blocked` by the controller with no assignee and no `blocked` event. Worker command lines look like
  `hermes_cli.main -p <profile> --cli ... chat -q "work kanban task <card id>"`.
- Tests: `tests/unit/` uses pytest, real temp git repos where git behaviour matters, and monkeypatching of
  `hermes` functions. Look at `tests/unit/test_controller.py` and `test_usage.py` for the idioms (fake board dicts).

## Your report (when done)
Run the full suite through the compressor and report: (1) what you built, one line per public function or class,
(2) the new test names, (3) the final pass count, (4) anything you deviated from or could not do, (5) anything
surprising you noticed in the code around your change (do not fix it, tell me). Do not print secrets.
