# Package LS: per-card resource leases, .env.ases, and integrity snapshots of the other worktrees

Files you own: `src/ases/leases.py` (new), `src/ases/guards.py` (extend, keep every existing function and behaviour that
`tests/unit/test_guards.py` relies on), `tests/unit/test_leases.py` (new), `tests/unit/test_guards.py` (extend). Nothing else. Read
`r2_rules.md` first (shared rules apply; ignore its "baseline 669": the suite baseline is whatever it shows before you start and must
never go down; other agents this round own sandbox.py, tamper.py, gates.py and finalgates.py). The schema for your tables already
exists in `src/ases/db.py` (schema v6: `resource_leases`, `worktree_snapshots`); do NOT edit db.py, read it.

## Requirements (read blueprint.txt around [p184] to [p189], test 22.5 at [p407]/[p408], ASES-ROL-08 at [p115])
- ASES-GIT-14 (section 8.4): "Each card gets its own port block, COMPOSE_PROJECT_NAME, database name or schema, and temp directory
  through environment variables written to .env.ases in its worktree. Singletons such as a shared development database are taken through
  a lock table in the ASES DB."
- ASES-GIT-12 (section 8.4): "Before a worker starts and after it stops, the controller snapshots git status --porcelain and HEAD of the
  primary checkout and of every other active worktree. Any change outside the worker's own worktree fails the card and raises a security
  event." (The primary checkout part is `guards.check_primary_checkout`, already built. Yours is the OTHER worktrees.)
- Test 22.5: "Three independent cards at once: separate worktrees, branches and profiles, separate port blocks and compose project names,
  no cross-worktree changes in the integrity snapshots, and three sequential merges. A fourth card stays queued."
- ASES-GIT-01: one worktree per work card. Hermes creates the worktree (under `<primary checkout>/.worktrees/<card id>` on this
  machine) when it claims the card; ASES does not create it.

## Build `leases.py`
1. `CardEnv` frozen dataclass: card_id, port_base (int), port_count (int), ports (tuple[int, ...]), compose_project (str), db_name (str),
   temp_dir (str), `as_env() -> dict[str, str]` returning `ASES_PORT_BASE`, `ASES_PORT_COUNT`, `ASES_PORT_0` ..., `COMPOSE_PROJECT_NAME`,
   `ASES_DB_NAME`, `ASES_TMPDIR`, `TMPDIR`, `TEMP` and `TMP` (the temp dir, so tools that read any of them stay inside it).
2. `LeaseError(Exception)` and `ResourceBusy(LeaseError)` carrying the current holder.
3. `allocate_card_env(conn, project, card_id, *, base_port=42000, block_size=10, max_blocks=100, port_free=is_port_free,
   temp_root=None, now=None) -> CardEnv`: idempotent (a card that already holds a port block gets the same env back); otherwise takes the
   lowest numbered block `port-block:<n>` not actively leased, skipping a block whose FIRST port is not free right now
   (`port_free(port) -> bool`, default a real `socket` bind probe on 127.0.0.1 that never raises; injectable), inserts one active row in
   `resource_leases` (resource `port-block:<n>`, holder card_id, detail JSON with the values handed out). Only the port block is a
   leased resource; the compose project name, database name and temp directory are DERIVED
   deterministically from (project, card id): `ases-<project slug>-<card id>` (lowercase, only [a-z0-9-], at most 63 chars: docker
   compose limits), `ases_<slug>_<card id>` for the database (only [a-z0-9_], at most 63), and `<temp_root or system temp>/ases/<project
   slug>/<card id>`. Raise `LeaseError` when every block is taken. The unique partial index makes a race safe: catch
   `sqlite3.IntegrityError` and try the next block.
4. `release_card_resources(conn, project, card_id, *, now=None) -> int`: sets released_at on every active lease held by the card (port
   block and singletons), returns how many were released, idempotent.
5. `acquire_singleton(conn, project, name, holder, *, now=None) -> None`: takes `singleton:<name>`; raises `ResourceBusy` naming the
   current holder when another card holds it; the same holder taking it again is a no-op. `release_singleton(conn, project, name,
   holder) -> bool` (False when it was not held by that holder). `holders(conn, project) -> list[dict]` (resource, holder, acquired_at)
   of active leases, oldest first.
6. `sweep(conn, project, live_card_ids, *, now=None) -> list[str]`: releases every active lease whose holder is not in `live_card_ids`
   (the ids of cards that are running, ready, review or scheduled; the caller works this out) and returns the resource names released.
7. `write_env_file(worktree, env: CardEnv) -> pathlib.Path`: writes `.env.ases` into the worktree (UTF-8, `KEY=value` lines, sorted,
   values quoted only when needed, a header comment saying it is written by ASES and must not be committed), creates the temp dir, and
   makes git ignore the file WITHOUT touching tracked files: append `.env.ases` to the worktree's own exclude file (resolve it with
   `git -C <worktree> rev-parse --git-path info/exclude`; create it if missing; never duplicate the line). Never writes outside the
   worktree except the temp dir. Idempotent: rewriting produces identical bytes.
8. `provision_running_cards(board, conn, plan, *, allocate=allocate_card_env, kanban_list=hermes.kanban_list, kanban_show=hermes.kanban_show)
   -> list[str]`: for every plan card (work cards of this plan's plan_tasks rows, project scoped) whose status is `running` and whose
   `workspace_path` (a field of the flat card dict from `kanban_show`; look at what hermes.kanban_show returns) exists on disk and has no
   `.env.ases` yet: allocate and write it; returns the card ids provisioned. A card without a workspace path is skipped; one failing card
   never stops the others (collect errors into an `events.record(conn, "provision_error", ...)`, do not raise). This is BEST EFFORT and the
   docstring must say why: the worker starts as soon as Hermes creates the worktree, so `.env.ases` can arrive a few seconds after the
   worker begins; the worker prompt tells it to read `.env.ases` when present.
9. `sweep_finished(board, conn, plan, ...) -> list[str]`: convenience that computes the live card ids from the board and calls `sweep`.

## Extend `guards.py` (ASES-GIT-12, other worktrees)
10. `list_worktrees(repo) -> list[WorktreeInfo]`: parse `git worktree list --porcelain` (path, head, branch or None, detached, bare, locked,
    prunable); read-only, `--no-optional-locks`, never raises (returns [] and the caller sees no worktrees when git fails).
11. `snapshot_worktree(path) -> tuple[str, str]`: (HEAD sha, sha256 of `git status --porcelain -z --untracked-files=normal`); ("", "") when
    unreadable. Use the same `_git` helper style as check_primary_checkout.
12. `check_idle_worktrees(conn, project, repo, running_paths, *, ignore_prefixes=()) -> list[str]`: the problems found in worktrees NO
    running card owns. `running_paths` is the set of worktree paths of currently running cards (normalise separators and case for
    Windows before comparing). For every other worktree of the repository that is not the primary checkout: compare its snapshot with the
    row in `worktree_snapshots` (a first sight records the snapshot and reports nothing); a moved HEAD or a changed status hash is one
    problem ("worktree <path> changed while no card was running in it: HEAD a..b" or "status changed"); then store the new snapshot so
    the same change is reported once, not forever. A worktree that disappeared drops its snapshot row. A worktree of a running card is
    skipped AND its snapshot row is deleted (its baseline is taken again when it stops: `snapshot_after_stop` below).
13. `refresh_snapshots(conn, project, repo, running_paths) -> int`: takes fresh snapshots of every non-running worktree (call it after a
    card stops or after ASES itself legitimately changed a worktree, for example a fix-card repoint) and returns how many it stored.
14. Nothing in guards.py may raise for a git failure; problems are reported as strings like the existing check does.

## Tests
`tests/unit/test_leases.py` (temp DB, temp git repos where git is involved, injected `port_free`): allocate returns distinct blocks for
three cards and the same env for the same card; derived names are lowercase, short and stable, weird card ids and project names are
slugged; a block whose first port is busy is skipped; exhaustion raises LeaseError; release frees the block for reuse; a racing insert
(simulate by pre-inserting the same resource row) falls through to the next block; singletons: acquire, second holder raises
ResourceBusy naming the first, same holder idempotent, release by non-holder returns False, release then re-acquire; `holders`; `sweep`
releases only dead holders; `write_env_file` content and bytes-identical rewrite, the exclude file gets exactly one `.env.ases` line and
`git status --porcelain` in the worktree stays clean (use a real temp repo with a real `git worktree add`); `provision_running_cards`
provisions only running cards with an existing workspace, skips others, survives a failing card and records provision_error; three cards
end with three separate port blocks and compose project names (the 22.5 property).
`tests/unit/test_guards.py` (extend): `list_worktrees` on a real repo with a linked worktree; `check_idle_worktrees` first sight records
and reports nothing, an untouched worktree stays quiet, a commit made in an idle worktree is reported once, an uncommitted change is
reported once, a running card's worktree is skipped, a vanished worktree drops its row, git failure does not raise; `refresh_snapshots`
makes a previously reported change quiet again.
