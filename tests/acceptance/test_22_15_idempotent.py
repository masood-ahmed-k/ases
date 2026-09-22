"""Acceptance 22.15: idempotent re-run (blueprint.txt [p427]/[p428]).

"Run card creation twice from the same approved plan, and once more after deleting the ASES database. The
board must contain each card exactly once. A card proposed by an agent must stay in triage until it is
validated."

Uses world/create_cards from tests/acceptance/conftest.py (ASES-LED-02, ASES-REC-03): the real
controller.create_cards_from_plan against ases.fakes.board.FakeHermes, no real hermes and no real provider
(r6_rules.md's hard constraint).
"""
from __future__ import annotations

import json
import pathlib

import pytest

from ases import db
from ases import events
from ases import hermes as hermes_mod
from ases import triage as triage_mod


def _db_files(db_path: pathlib.Path) -> list[pathlib.Path]:
    """db.connect opens the database in WAL mode, which leaves -wal and -shm siblings next to the main file;
    all three must go for "deleting the ASES database" to mean what it says."""
    return [pathlib.Path(f"{db_path}{suffix}") for suffix in ("", "-wal", "-shm")]


def _delete_database(world) -> None:
    world.conn.close()
    for path in _db_files(world.db_path):
        if path.exists():
            path.unlink()
    assert not world.db_path.exists()
    world.conn = db.connect(world.db_path)  # a FRESH ASES database; the fake Hermes board is untouched


def test_22_15_card_creation_is_idempotent_twice_and_once_more_after_the_database_is_deleted(world, create_cards):
    """ASES-LED-02 ("The controller creates cards from the approved plan with idempotency keys") and
    ASES-REC-03 ("Card creation is idempotent"; its requirements.yaml note names exactly this gap: "it has
    not been repeated twice against a real board or after deleting the database (test 22.15)"). Two calls on
    the same (conn, plan) must not add a single card; a third call after the ASES database itself is deleted
    must not either, because Hermes's own idempotency_key, not ASES's plan_tasks table, is what create_cards_
    from_plan actually depends on for this guarantee (r6_wp_ac_g.md's own framing: "this is the whole point
    of the test")."""
    pairs_1 = create_cards(world)
    card_count = len(world.fake.cards())
    assert card_count == 4  # DEFAULT_PLAN: T1 and T2, one work card and one merge card each

    pairs_2 = world.create_cards()
    assert len(world.fake.cards()) == card_count
    assert pairs_2 == pairs_1  # the exact same CardPair objects (same work and merge card ids) come back

    rows_before = {
        row["task_key"]: (row["work_card_id"], row["merge_card_id"], row["fix_cards"])
        for row in world.conn.execute(
            "SELECT task_key, work_card_id, merge_card_id, fix_cards FROM plan_tasks WHERE project = ?",
            (world.plan.project,),
        )
    }
    assert set(rows_before) == {"T1", "T2"}

    _delete_database(world)

    pairs_3 = world.create_cards()

    # The board still has each card exactly once: Hermes's own idempotency_key recovered the same ids, not
    # a lookup in ASES's (just-emptied) database.
    assert len(world.fake.cards()) == card_count
    assert pairs_3 == pairs_1

    # In THIS scenario (no fix card was ever opened for either task) plan_tasks is also correctly
    # repopulated: create_cards_from_plan's plain path (no existing plan_tasks row, so no _has_retry_card
    # special-case) calls hermes.kanban_create with the ORIGINAL idempotency_key, which is also the CORRECT,
    # still-current card here, so what gets INSERTed after the third call matches what was there before the
    # database was deleted. See test_22_15_a_fix_card_is_forgotten_after_the_database_is_deleted below for
    # the case where this stops being true.
    rows_after = {
        row["task_key"]: (row["work_card_id"], row["merge_card_id"], row["fix_cards"])
        for row in world.conn.execute(
            "SELECT task_key, work_card_id, merge_card_id, fix_cards FROM plan_tasks WHERE project = ?",
            (world.plan.project,),
        )
    }
    assert rows_after == rows_before


def test_22_15_a_fix_card_is_forgotten_after_the_database_is_deleted(world, create_cards):
    """A genuine gap, found by reading controller.create_cards_from_plan and process_merge_queue closely
    (report format asks for exactly this finding). Once a merge fails, process_merge_queue opens a fix card
    with idempotency_key f"ases-fix-{project}-{key}-{fix_cards + 1}" (controller.py) and repoints plan_tasks
    via one UPDATE: "work_card_id=CASE WHEN plan_tasks.fix_cards > 0 THEN plan_tasks.work_card_id ELSE
    excluded.work_card_id END". That CASE WHEN is the ONLY thing protecting the fix card's place as the
    task's current card on a re-approve, and it can only fire when a plan_tasks ROW ALREADY EXISTS TO READ
    fix_cards FROM. Deleting the ASES database deletes that row (and the fix_cards counter) too, so a third
    create_cards_from_plan call has an empty plan_tasks table, takes the plain (no existing row) INSERT path,
    and calls hermes.kanban_create with the ORIGINAL work card's idempotency_key
    (f"ases-work-{project}-{key}"), which Hermes correctly and idempotently returns (it is not archived, only
    superseded) -- but that is the WRONG card now: the fix card, not the original, is what is actually
    current on the (unchanged) board.

    The board itself stays internally consistent (no duplicate card is created: Hermes's idempotency_key
    still does its job on each individual kanban_create call), but ASES's own bookkeeping for this task is
    now wrong, silently, with no error raised. This is reported, not papered over: see the report for
    exactly this finding, quoting controller.py's line."""
    pairs = create_cards(world)
    t1 = pairs["T1"]

    # Reproduce, in every particular (idempotency key shape, the fields set, the UPDATE statement), exactly
    # what controller.process_merge_queue's fix-card branch does on a failed merge or a red Gate 3
    # (controller.py), so this test isolates create_cards_from_plan's OWN recovery behaviour rather than
    # also re-exercising process_merge_queue's conflict handling (out of this package's scope).
    fix_card = hermes_mod.kanban_create(
        world.board, "T1: fix (round 1)", assignee="coder-1", workspace="worktree", branch="swarm/T1-fix1",
        project=world.project_id, body="Merge attempt for T1 failed. Fix in a fresh worktree.",
        parent=[t1.work_card_id], idempotency_key=f"ases-fix-{world.plan.project}-T1-1",
        max_retries=3, max_runtime="45m",
    )
    world.conn.execute(
        "UPDATE plan_tasks SET fix_cards = fix_cards + 1, work_card_id = ? WHERE project = ? AND task_key = ?",
        (fix_card["id"], world.plan.project, "T1"),
    )
    row = world.conn.execute(
        "SELECT work_card_id, fix_cards FROM plan_tasks WHERE project = ? AND task_key = 'T1'",
        (world.plan.project,),
    ).fetchone()
    assert (row["work_card_id"], row["fix_cards"]) == (fix_card["id"], 1)  # the fix card really is T1's card now

    card_count_before = len(world.fake.cards())  # T1 work + T1 fix + T1 merge + T2 work + T2 merge

    _delete_database(world)

    world.create_cards()

    # The board stays correct: no duplicate card appeared.
    assert len(world.fake.cards()) == card_count_before

    # ASES's bookkeeping for T1 does not: work_card_id reverted to the ORIGINAL, now-superseded work card,
    # and the fix_cards counter itself was lost (reset to 0), even though the fix card is still sitting
    # right there on the board as T1's real, current card.
    row_after = world.conn.execute(
        "SELECT work_card_id, fix_cards FROM plan_tasks WHERE project = ? AND task_key = 'T1'",
        (world.plan.project,),
    ).fetchone()
    assert row_after["work_card_id"] == t1.work_card_id       # wrong: this is the ORIGINAL, superseded card
    assert row_after["work_card_id"] != fix_card["id"]        # T1's actually-current card was forgotten
    assert row_after["fix_cards"] == 0                        # this row has no memory the fix card ever existed


def _put_card_directly_in_triage(world, *, title: str, body: str) -> dict:
    """A card whose Hermes status is exactly "triage", the state list_triage_cards/validate/promote_card/
    archive_card all operate on. NOT built with hermes.kanban_create(..., initial_status="triage") (that was
    this file's first attempt, before src/ases/triage.py existed to say otherwise): that call raises
    HermesCommandError, because FakeHermes.kanban_create's VALID_INITIAL_STATUSES is ("running", "blocked")
    only, matching real Hermes (triage.py's own module docstring, read closely: "initial_status is checked
    ONLY for the literal string 'blocked'; there is no branch for 'triage' at all"). triage.py documents the
    real mechanism as a WORKER's own in-task kanban_create(triage=true) tool call -- a different code path
    that neither ases.fakes.board.FakeHermes (no agent_create method) nor ases.fakes.worker (no Propose
    step) model at all today. Rather than inventing a fake proposal mechanism of its own, this helper builds
    the card normally and then sets its status directly on the fake's internal task record, which is the
    same state a real triage=true call would have left behind, and is genuinely the only way to reach it
    against this rig as it exists this round."""
    card = hermes_mod.kanban_create(world.board, title, body=body)
    world.fake._tasks[card["id"]].status = "triage"
    return card


def test_22_15_a_triage_card_is_never_touched_by_ordinary_run_pass_calls(world):
    """"A card proposed by an agent must stay in triage until it is validated" (ASES-LED-03): nothing in the
    controller loop looks at a triage-status card at all, so any number of ordinary run_pass passes must
    leave it exactly where it is. No plan cards are created in this test (world.create_cards() is never
    called): the triage card's own behaviour does not depend on anything else being on the board."""
    card = _put_card_directly_in_triage(
        world, title="T1: a follow-up the coder noticed",
        body="Investigate the slow test in test_foo.py; it looks flaky, not just slow.",
    )
    assert world.fake.card(card["id"])["status"] == "triage"

    for _ in range(5):
        world.one_pass()

    assert world.fake.card(card["id"])["status"] == "triage"


def test_22_15_triage_lists_and_validates_the_card_and_archive_card_moves_it_out(world):
    """src/ases/triage.py (package LED, this round): list_triage_cards finds the proposal, validate() passes
    it (a real title, a body well over MIN_BODY_LENGTH, no structured JSON hint to fail), and
    triage.archive_card (hermes.kanban_archive, which real Hermes and the fake both accept from ANY
    non-archived status, triage included) is the explicit, human-driven action that finally moves it, with a
    triage_decision event recording why. See the finding below for promote_card, the other of the two
    explicit actions named in the blueprint text, which does not currently work the same way."""
    card = _put_card_directly_in_triage(
        world, title="T1: a follow-up the coder noticed",
        body="Investigate the slow test in test_foo.py; it looks flaky, not just slow.",
    )

    found = triage_mod.list_triage_cards(world.board, world.plan, conn=world.conn)
    assert [c.card_id for c in found] == [card["id"]]
    # No plan_tasks rows exist in this test (world.create_cards() is never called), so ownership falls back to
    # triage.py's title-prefix rule: the title starts with "T1:", one of this plan's own task keys, so the
    # proposal is attributed to T1 even with no card in plan_tasks to confirm it by id.
    assert found[0].raised_by_task == "T1"

    result = triage_mod.validate(world.board, card["id"], conn=world.conn)
    assert result.ok and result.problems == ()

    triage_mod.archive_card(
        world.board, card["id"], conn=world.conn, project=world.plan.project, reason="not worth doing right now",
    )

    assert world.fake.card(card["id"])["status"] == "archived"
    assert not [c.card_id for c in triage_mod.list_triage_cards(world.board, world.plan, conn=world.conn)]
    decisions = [e for e in events.recent(world.conn, limit=200) if e["kind"] == "triage_decision"]
    assert len(decisions) == 1
    payload = json.loads(decisions[0]["payload"])
    assert (payload["card_id"], payload["decision"]) == (card["id"], "archive")


def test_22_15_promote_card_now_works_on_a_genuinely_triage_status_card(world):
    """Round 6 found a real gap here (see the round 6 section of builder-findings.md): triage.promote_card called
    hermes.kanban_promote, which only accepts a card already in todo/blocked, so it always failed on a card
    genuinely sitting in triage. The user was asked and chose "option A": ASES may call Hermes's own
    `specify` (an auxiliary-model call) from promote_card, and only from there (r7_wp_specify.md, round 7).
    That is now built: promote_card calls hermes.kanban_specify, which moves triage -> todo (real Hermes's
    only mechanism for a card to leave triage at all), and this test asserts the FIXED behavior in place of
    the gap round 6 documented."""
    card = _put_card_directly_in_triage(
        world, title="T1: a follow-up the coder noticed",
        body="Investigate the slow test in test_foo.py; it looks flaky, not just slow.",
    )

    triage_mod.promote_card(
        world.board, card["id"], conn=world.conn, project=world.plan.project, reason="looks worth doing",
    )

    assert world.fake.card(card["id"])["status"] == "todo"  # kanban_specify's real landing status, not "ready"
    events = [e for e in world.fake.events(card["id"]) if e["kind"] == "specified"]
    assert events, "the real Hermes kanban_specify call (via the fake) left no trace of moving the card"
