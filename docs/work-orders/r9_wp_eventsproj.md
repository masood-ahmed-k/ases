# Round 9 package EVENTSPROJ: events carry their project (read `r9_rules.md`)

Touches nearly every module, so it starts after wave A is merged (T2B, CAPDOC, DOCTOR, PAUSEREASON, IDLEWT, MERGEPK, CIPIN are
all on master). Wave B (GITHARDEN: the git subprocess call lines; GATESANDBOX: the gate call sites) is still being built on its
own branches and merges after you; keep every hunk to the `events.record(` line or the `FROM events` query itself, never reflow
the surrounding code, so those merges stay mechanical. Worktree: `C:\Users\masoo\ases-wt\eventsproj`, branch `r9/eventsproj`,
cut from master after wave A. MERGEPK made `merge_records` project-scoped with schema v8: if you need a migration it is v9 (and
GATESANDBOX may also add one; the architect renumbers at merge). Tier 1 item 5.

## Requirements (quoted from blueprint.txt)
- ASES-ARC-03 (p101): "Every ASES record is keyed by the Hermes card ID and, where code is involved, by the commit SHA. On startup
  the controller reconciles the board, the Git repository and its own database before doing anything else (section 19.4)."
- ASES-OBS-01 (p284): "ASES adds a project report, available as swarm status, swarm report and optionally one local page."

## Where things stand
Schema version 7 added a nullable `events.project` column. Nothing writes it: `events.record(conn, kind, payload)` (in
`src/ases/events.py`) inserts `ts, kind, payload` only. There are about 71 `events.record(` call sites across 16 modules. Many
payloads already carry a `"project"` key, and many readers filter with `json_extract(payload, '$.project') = ?`
(`bounds.py`, `controller.py`, `finalgates.py`, `recovery.py`, and others); some readers filter by `kind` alone. Round 9's MERGEPK
made `merge_records` project-scoped, and `gate_runs` already is: read how both treat legacy rows (a project-scoped read matches its
own rows and legacy NULL rows, never another project's) and use the same semantics.

## Build
1. `events.record` gains a keyword-only `project` argument and writes the column. When the argument is omitted, it takes
   `payload["project"]` if present, so every existing call whose payload already names its project fills the column with no change
   at the call site. If both are given and disagree, that is a bug at the call site: raise in tests (decide the production
   behaviour and justify it in the docstring; never silently prefer one).
2. Go through EVERY call site (Grep, do not trust the count above) and classify it: (a) payload already names the project: nothing
   to do; (b) a project is in scope at the call site but not in the payload: pass `project=`; (c) genuinely cross-project (for
   example credential health, which is per provider, or a doctor/eval event with no project): leave the column NULL and say why in
   one comment only where it is not obvious. List every site and its class in your report.
3. Go through EVERY reader of the `events` table (`FROM events` in `src/ases`) and classify it the same way: already scoped through
   the payload (switch to the column with the legacy fallback, `COALESCE(project, json_extract(payload, '$.project'))`, so rows
   written before this package still match), should be scoped but is not (fix it: a project's report or final gate must never count
   another project's events), or legitimately global (leave it, say why). `hardening.py`'s retention delete is global by design.
4. If a migration is needed (an index on `events(project, kind)` for example), follow `db.py`'s rules and justify it with the query
   that needs it.
5. Tests: `record` writes the column from the argument and from the payload, and the disagreement case; a two-project database where
   each project's report, final-gate event count and bounds check see only their own events plus legacy rows (use the smallest real
   entry points you can: `report.py`, `finalgates`, `bounds`), with a before/after proof that at least one of those readers counted
   the other project's events on the old code. Full suite once at the end.

## Files you own
`src/ases/events.py`, every `events.record(` call site and every `FROM events` reader in `src/ases` (only those lines), a migration
in `src/ases/db.py` if needed, and the matching tests.
