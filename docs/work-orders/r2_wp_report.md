# Package S: swarm status and swarm report

Files you own: `src/ases/report.py` (new), `tests/unit/test_report.py` (new). Nothing else.

## Requirements
- ASES-OBS-01, section 15: "The Hermes dashboard already shows the board, runs, worker logs and per-card model choices ...
  ASES MUST NOT rebuild it. ASES adds a project report, available as swarm status, swarm report and optionally one local
  page." Read section 15 fully (blueprint.txt lines around `[p283]` to `[p298]`): 15.1 lists the metrics, 15.2 the panel
  layout (Project, Budget, Cards, Quality, Health, Events, Models).
- ASES-OBS-02: transcripts and logs stay local; nothing here uploads anything. ASES-SEC-01: log commands and results but
  redact known secret patterns (use `events.redact` on every payload you render).
- Section 16 phase 8: "swarm report and the optional local page next to the Hermes dashboard". The page must be bound to
  nothing: it is a static file the user opens, no server.

## Build `report.py`
1. `build_report(board, plan, project, models_config, conn, *, now=None, event_limit=40) -> dict` returning plain data (JSON
   serialisable): keys `generated_at` (UTC ISO seconds), and one entry per panel:
   - `project`: name, board, integration branch, data class, phase-independent facts, and `bounds`: a list of
     {name, used, limit} for the bounds computable from what exists: cards in the plan vs `budgets.max_cards`; for each
     task fix cards used vs `budgets.fix_cards_per_task` (from plan_tasks.fix_cards), review rounds vs
     `review_rounds_per_task` and capability/infra failures (from the lineage table, missing row = 0), re-plans vs
     `replans_per_project` (project_state.replans, missing = 0), wall clock from project_state started_at/deadline_at
     (missing = not set). Do not invent bounds that have no data.
   - `budget`: per provider named in models_config["providers"]: limit today (use `ledger.daily_limit`), used today
     (`ledger.usage_today_for_provider`), remaining, and the reserve (`daily_reserve_percent` of the limit); the cards
     currently parked for budget (`hermes.kanban_list(board, status="scheduled")` filtered to this plan's cards) with the
     latest reason; and per (provider, model, role/profile) the requests ingested today from usage_ingested.
   - `cards`: for every plan task its current work card and its merge card: id, status, assignee (via
     `hermes.kanban_show`; one failing show is reported as status "unknown", it must not break the report); counts per
     status; the number of open questions (blocked cards with a `blocked` event reason, same rule as the questions module:
     a `blocked` event in `card["_events"]` with a non-empty payload reason); merge queue state (merge cards done/total).
   - `quality`: recent gate runs (gate_runs: task_key, gate, commit_sha shortened to 10, result, ran_at) newest first,
     review_verdicts rows, `merge_refused_unreviewed`, `integrity_violation` and any `tamper` events from the events
     table, and merge_records (task, squash commit short, gate3 result, reverted).
   - `health`: the last events whose kind is in {pass_error, usage_ingest_error, merge_failed, merge_race_retrying,
     card_parked_for_budget, integrity_violation, fix_card_created, fix_card_budget_exhausted} with counts per kind
     over the events read, the kind's newest timestamp, and the newest message. (Provider health "from real traffic" comes
     later; do not fake it.)
   - `events`: the last `event_limit` events, payloads passed through `events.redact`, newest first.
   - `models`: the model_registry rows (provider, model, role_class, pinned, context_length, smoke_test_result and its
     time if the columns exist; read `src/ases/models.py` for the ModelRecord fields) sorted with pinned first.
2. `render_status(report) -> str`: the compact one-screen text for `swarm status`: one line per panel summary (project
   line; budget line per provider "openrouter 37/50 used"; cards "3 done, 1 running, 1 blocked (1 question)"; last gate
   result; last 5 health events). ASCII only.
3. `render_text(report) -> str`: the full report for `swarm report`: every panel with a heading and aligned tables.
   ASCII only; anything non-ASCII coming from card titles or event text is backslash-escaped, never dropped silently.
4. `render_html(report) -> str`: one self-contained page (inline CSS, no scripts, no external resources), every value
   HTML-escaped (a card title containing `<script>` must appear inert), a banner "Local report: generated <time>, not
   served, contains source-code paths and card text, keep it on this machine".
5. `write_report(report, directory) -> tuple[pathlib.Path, pathlib.Path]`: writes `report.html` and `report.json` into
   `directory` (created if missing), UTF-8, returns both paths.

## Tests (`tests/unit/test_report.py`; temp DB seeded by inserting rows directly; monkeypatch `hermes.kanban_show` and
`hermes.kanban_list`)
Empty database (no events, no gate runs, no lineage rows) still builds and renders; each panel's content from seeded rows
(one done task with a merge record, one blocked card with a question, one parked card); a failing kanban_show becomes
"unknown" and the rest is intact; secrets: an event payload containing "sk-abcdefghijklmnopqrstuvwx" and a key named
"api_key" never appear in the text, the HTML or the JSON; ASCII-only assertion on render_status and render_text for a
title with an accented character and an emoji; HTML escaping of a `<script>` title and of `&`; bounds computed from
lineage/plan_tasks/project_state rows including a missing row; budget arithmetic with a capped provider (OpenRouter-like
limits in models_config["providers"] as in tests/unit/test_controller.py MODELS_CONFIG) and an uncapped one (limit None,
remaining None rendered as "no known cap"); ordering (newest first, pinned models first); write_report creates the
directory and both files and the JSON round-trips.
