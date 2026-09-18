"""Reconcile-on-start (section 9.1 folds this into recovery.py's job; kept as its own module here
since it's independently testable): compare the Hermes board against the ASES DB before doing
anything else on a fresh controller start (ASES-REC-04, ASES-ARC-03).

Phase 3 scope: the checks that are actually meaningful with what's built so far (card IDs still
resolve, a merge marked done has a matching merge_records row, nothing is both reverted and done).
Orphan worker processes and full crash-mid-merge recovery need real dispatch to exercise -- noted as
a gap, not silently assumed away.
"""
from __future__ import annotations

import dataclasses

from . import hermes as hermes_mod


@dataclasses.dataclass(frozen=True)
class Inconsistency:
    task_key: str
    kind: str
    detail: str


def check(board: str, project: str, *, conn) -> list[Inconsistency]:
    findings: list[Inconsistency] = []
    rows = conn.execute(
        "SELECT task_key, work_card_id, merge_card_id FROM plan_tasks WHERE project = ?", (project,)
    ).fetchall()

    for row in rows:
        key = row["task_key"]
        for label, card_id in (("work", row["work_card_id"]), ("merge", row["merge_card_id"])):
            if not card_id:
                continue
            try:
                card = hermes_mod.kanban_show(board, card_id)
            except hermes_mod.HermesCommandError:
                findings.append(Inconsistency(key, "missing_card", f"{label} card {card_id} no longer resolves"))
                continue

            if label == "merge" and card["status"] == "done":
                mr = conn.execute(
                    "SELECT completed_at, reverted FROM merge_records WHERE task_key = ?", (key,)
                ).fetchone()
                if mr is None or not mr["completed_at"]:
                    findings.append(Inconsistency(
                        key, "merge_done_without_record",
                        f"merge card {card_id} is done but merge_records has no completed_at",
                    ))
                elif mr["reverted"]:
                    findings.append(Inconsistency(
                        key, "done_but_reverted",
                        f"merge card {card_id} reads done but merge_records.reverted=1 -- the "
                        "integration branch was rolled back after this card was completed",
                    ))
    return findings
