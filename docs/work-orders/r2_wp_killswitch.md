# Package K: the kill switch (swarm stop, swarm resume, the stop flag)

Files you own: `src/ases/killswitch.py` (new), `tests/unit/test_killswitch.py` (new). Nothing else. (`cmd_stop`/`cmd_resume` in cli.py
currently do a bare hermes pause + reclaim; the architect will replace their bodies with calls into your module. Read them first.)

## Requirements (quote the ids; read blueprint.txt around `[p356]` and `[p357]`, table 31 rows "Worker exceeds its runtime", "Controller crash")
- ASES-REC-06, section 19.6: "swarm stop MUST stop the whole system within 30 seconds: hermes pause to stop new dispatch, reclaim
  every running card, terminate worker process trees and sandboxes, stop the merge queue between steps, and write a stop report.
  hermes pause alone is not enough, because it never kills work in flight. swarm resume reverses it after reconcile-on-start."
- Section 22.13: "With three cards running, swarm stop must leave no worker process, container or merge step running after 30
  seconds, and swarm resume must continue correctly."
- Section 21.3 last bullets: "Keep the kill switch working at all times."
- The stop flag lives in the `project_state` table (status `stopped`; see src/ases/db.py, do not edit it). Another package writes
  `bounds.py` helpers for that table but you may not import it: read/write the table directly with small functions of your own
  (`request_stop`, `clear_stop`, `stop_requested`), single statements, UTC isoformat seconds. The polling loop and the merge queue will
  call `stop_requested(conn, project)` between steps; the architect wires that.

## Safety rules for killing (read twice)
Never terminate a process unless ALL of these hold: it is the `worker_pid` of a run of a card of THIS plan that is currently running (or
review-running), AND its command line (injectable `command_line(pid)`) contains that card's id, AND it is not the current process or
its parent. The user may have unrelated Hermes chat sessions, the Hermes gateway and the dispatcher running; killing those is a failure
of this package. A process whose command line cannot be read is not killed (it is reported as "could not verify"). On Windows never use
os.kill (it terminates on any signal, including 0): use `taskkill /PID <n> /T /F` through an injectable `killer(pid) -> bool`, and
`process_command_line`, `pid_alive` helpers are yours to write here too (duplicate small helpers are fine; do not import reconcile.py).

## Build `killswitch.py`
1. `StopReport` dataclass (started_at, finished_at, seconds, paused (bool), reclaimed (list of card ids), reclaim_errors (list of
   {card_id, error}), killed (list of {card_id, pid}), unverified (list of {card_id, pid, why}), containers_stopped (list of str),
   flag_set (bool), within_deadline (bool), notes (list of str)) with `to_dict()` (JSON-serialisable).
2. `request_stop(conn, project, reason) / clear_stop(conn, project) / stop_requested(conn, project) -> bool`.
3. `stop_all(board, plan, *, conn, deadline_seconds=30, pause=hermes.pause, kanban_list=hermes.kanban_list,
   kanban_show=hermes.kanban_show, reclaim=hermes.kanban_reclaim, killer=terminate_tree, alive=pid_alive,
   command_line=process_command_line, list_containers=default_list_containers, stop_container=default_stop_container, now=time.monotonic,
   sleep=time.sleep) -> StopReport` performing IN THIS ORDER, each step time-boxed so the whole call finishes inside `deadline_seconds`:
   (a) set the stop flag FIRST (so the polling loop and merge queue see it immediately), (b) `pause` (a failure is recorded, the
   rest still runs), (c) list `running` cards (and cards `review` with a live run) and keep only cards of this plan (plan_tasks rows
   for plan.project: current work cards and merge cards; also cards whose parents include one, i.e. fix cards) so unrelated boards are
   untouched, (d) `reclaim` each (per-card failure recorded, continue), (e) for each card: find the live pid, verify with the safety
   rules above, `killer(pid)`; then poll `alive(pid)` briefly (bounded by the remaining deadline) and record a note when a pid is still
   alive at the end, (f) containers: `list_containers(card_ids)` returns the names of Docker containers whose name or labels contain a
   card id (default implementation runs `docker ps --format "{{.Names}}"` and filters by card id, and returns [] when Docker is absent
   or the daemon is down; never raises); `stop_container(name)` is `docker stop -t 5 <name>` and never raises; only containers matching
   a card id of this plan are stopped, (g) write nothing else. Return the report. Never raise: every failure is a report entry.
4. `write_stop_report(report, directory) -> pathlib.Path`: `stop-<UTC timestamp>.json` in `directory` (created), UTF-8, returns the path.
5. `resume_all(board, plan, *, conn, resume=hermes.resume, reconcile=None) -> dict`: clear the stop flag ONLY after `reconcile` (an
   optional zero-argument callable the architect passes; the blueprint says resume happens "after reconcile-on-start") returned without
   raising and reported nothing blocked (it may return an object with a `blocked` list, or None; treat a non-empty `blocked` as "not
   safe to resume": keep the flag, do not call `resume`, return {"resumed": False, "reason": ...}); then call `resume()`; return
   {"resumed": True}. A `resume` failure keeps the flag set and returns {"resumed": False, "reason": ...}.
6. `default_list_containers`, `default_stop_container`, `pid_alive`, `terminate_tree`, `process_command_line`: the real
   implementations, each never raising, each with a short timeout.

## Tests (`tests/unit/test_killswitch.py`; inject fakes for EVERY external call, no real process, no docker, no hermes)
Order of operations (record a call log: flag first, then pause, then list, reclaim, kill, containers); pause failing does not stop
the rest; only cards of this plan are reclaimed and killed (a running card of another project on the same board is untouched); the
three safety rules (command line lacking the card id: not killed and listed as unverified; unreadable command line: not killed;
the current process pid and its parent are never killed even if they match); a reclaim failure on one card does not stop the others;
the deadline: with a fake clock that jumps, later steps are skipped and `within_deadline` is False and the flag is still set; a pid still
alive after the kill is noted; containers filtered by card id and a stop failure recorded; write_stop_report file name and content;
resume_all with a clean reconcile clears the flag and resumes, with blocked findings keeps the flag, with a raising reconcile keeps
the flag, with a raising resume keeps the flag; request_stop/clear_stop/stop_requested round trip and idempotence.
