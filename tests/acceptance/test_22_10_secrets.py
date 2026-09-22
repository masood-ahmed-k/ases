"""Acceptance 22.10: secret leak (blueprint.txt [p417]/[p418]; ASES-SEC-01, ASES-GIT-07).

"Plant a fake key in the repository and another in the controller's environment. Neither may appear in any
prompt captured by the fake provider, any card body, any log or the report; env inside a worker terminal shows
no provider key; reading .env from inside the sandbox fails; the planted file blocks Gate 1." [p418]

Two halves, per r6_rules.md. The SANDBOX half ("env inside a worker terminal shows no provider key", "reading
.env from inside the sandbox fails") needs Docker, which this suite never starts; sandbox.py's own unit tests
already prove that half at the unit level with a fake runner (ASES-SEC-02/03, both "BUILT AND WIRED, NOT RUN FOR
REAL" per spec/requirements.yaml). This file proves the rest, end to end, against the real controller:

  1. a secret-shaped VALUE committed inside an ordinary file blocks Gate 1 (tamper.py's secret_added finding,
     ASES-SEC-01) and never appears in any event, card comment/body, gate output, review verdict or report;
  2. a second secret-shaped value that lives only in the TEST PROCESS's own environment (never written to the
     repository, never read by any ASES code) also never appears anywhere, proving the guarantee that nothing in
     ASES reads or forwards its own process environment into a card, an event, or a report;
  3. a whole FILE named like a secret (id_rsa) blocks Gate 1 too (ASES-GIT-07), though through a DIFFERENT
     tamper.py finding kind than a same-file value does: generated_artifact, not secret_added. tamper.py's
     added-file check (_artifact_reason) folds "generated build artifact" and "file whose name marks it as
     holding secrets" into that one finding kind; gates.scan_for_secrets, a separate, merge-time-only helper not
     reached from the review lane, is the function that would call an added secret FILE "secret_added" instead.
     Cross-checked against tests/unit/test_tamper.py's own
     test_adding_a_generated_artifact_or_secret_file_is_flagged, which asserts exactly that kind for "id_rsa".

The fake provider (ases.fakes.provider) is never started in this file: nothing here makes a model call, so there
is no prompt for it to capture, and "reads what was sent to a model" is the only reason to reach for it
(r6_rules.md). Every "never appears" check below goes through _assert_absent, which on failure prints only where
the secret was found and its length, the same discipline ases.fakes.provider.assert_never_received documents
("names which secret ... and never prints the secret itself"), so a failing assertion here can never itself leak
either planted value into the test output.
"""
import json

from ases import events
from ases import report as report_mod
from ases.fakes import worker as fw

# sk-or-v1- is the OpenRouter shape events._SECRET_VALUE_PATTERN already recognises (see events.py's own comment
# on _SECRET_VALUE_PATTERN). Two distinct fake values, so a mix-up between "the repo one" and "the env one" would
# be caught by these tests, not silently masked by asserting the same string twice.
REPO_SECRET = "sk-or-v1-9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d"
ENV_SECRET = "sk-or-v1-1122334455667788990011223344556"

PLAN = {
    "project": "acceptance-secret",
    "integration_branch": "integration",
    "gate_profiles": {"trivial": ["echo ok"]},
    "tasks": [
        {"key": "T1", "title": "add a", "role": "coder", "depends_on": [], "touches": ["a.py"],
         "acceptance": ["a.py defines add(x, y) returning x + y"], "gate_profile": "trivial",
         "estimated_requests": 5},
    ],
}

# Touches "id_*" (not "id_rsa"): tamper.py exempts an added artifact/secret-named file only when an allow glob
# NAMES it explicitly (the marker text "id_rsa" appears literally in the glob). "id_*" COVERS id_rsa (fnmatch)
# without naming it, so the finding still fires, exactly like tests/unit/test_tamper.py's own
# test_an_allow_glob_must_name_the_artifact_to_exempt_it (allow_paths=["*"] still finds ".env"). Plain "*" was
# tried first and rejected at plan-parse time (ASES-QG-02: a touches glob that broad also covers gate/CI
# configuration, which needs its own explicit opt-in), so this is deliberately narrower.
SECRET_FILE_PLAN = {
    "project": "acceptance-secret-file",
    "integration_branch": "integration",
    "gate_profiles": {"trivial": ["echo ok"]},
    "tasks": [
        {"key": "T1", "title": "add a", "role": "coder", "depends_on": [], "touches": ["a.py", "id_*"],
         "acceptance": ["a.py defines add(x, y) returning x + y"], "gate_profile": "trivial",
         "estimated_requests": 5},
    ],
}


def _assert_absent(secret: str, haystacks: dict) -> None:
    """Fail without ever printing `secret`: only the labels of where it was found and its length, never the
    haystacks' content and never the secret itself (see the module docstring)."""
    hits = sorted(label for label, text in haystacks.items() if secret in (text or ""))
    if hits:
        raise AssertionError(f"a planted secret (length {len(secret)}) appeared in: {', '.join(hits)}")


def _surfaces(world) -> dict:
    """Every place a leaked secret could plausibly turn up, as {label: text}: every ASES event payload, every
    gate_runs.detail and review_verdicts.metadata row, every card's body/comments/runs/events, and both report
    renderings (build_report's own JSON-shaped dict, rendered as text and as HTML)."""
    surfaces: dict = {}
    for row in events.recent(world.conn, limit=500):
        surfaces[f"event[{row['kind']}]"] = row["payload"]
    for row in world.conn.execute("SELECT id, detail FROM gate_runs").fetchall():
        surfaces[f"gate_runs[{row['id']}]"] = row["detail"] or ""
    for row in world.conn.execute("SELECT task_key, commit_sha, metadata FROM review_verdicts").fetchall():
        surfaces[f"review_verdicts[{row['task_key']}:{row['commit_sha']}]"] = row["metadata"] or ""
    for base in world.fake.cards():
        cid = base["id"]
        card = world.fake.card(cid)  # cards() lists the flat dict only; card(id) adds _comments/_runs/_events
        surfaces[f"card[{cid}].body"] = card.get("body") or ""
        surfaces[f"card[{cid}].comments"] = json.dumps([c["body"] for c in card["_comments"]])
        surfaces[f"card[{cid}].runs"] = json.dumps(card["_runs"], default=str)
        surfaces[f"card[{cid}].events"] = json.dumps(card["_events"], default=str)
    report = report_mod.build_report(world.board, world.plan, world.project, world.models_config, world.conn)
    surfaces["report.json"] = json.dumps(report, default=str)
    surfaces["report.text"] = report_mod.render_text(report)
    surfaces["report.html"] = report_mod.render_html(report)
    return surfaces


def _run_until_sent_back(world, work: str) -> None:
    world.run_until(lambda w: any(e["kind"] == "review_reopened" for e in w.card(work)["_events"]))


def test_22_10_a_secret_shaped_value_in_a_committed_file_blocks_gate_1_and_never_leaks(world_factory):
    """ASES-SEC-01 / ASES-GIT-07: a coder commits a.py with a fake provider key on one line. Gate 1's tamper
    check finds it (secret_added) and sends the card back before a reviewer is spawned; the finding names only
    the KIND of secret ("provider token", from tamper.secret_hint), never the value itself, and the value never
    reaches an event, a card, a gate run, a review verdict, or either report rendering."""
    world = world_factory(plan_raw=PLAN)
    world.fake.register_worker("coder-1", fw.sequence(
        fw.good_coder({"a.py": f"API_KEY = '{REPO_SECRET}'\n"}, "wire up the client"),
        fw.questioner("halting further attempts for this test"),
    ))
    pair = world.create_cards()["T1"]
    work = pair.work_card_id

    _run_until_sent_back(world, work)

    card = world.card(work)
    assert card["status"] != "done"
    assert not any(run["profile"] == "reviewer" for run in card["_runs"])

    recorded = events.recent(world.conn, limit=500)
    kinds = {e["kind"] for e in recorded}
    assert {"tamper_blocked", "gate1_recheck_failed"} <= kinds
    (blocked,) = [e for e in recorded if e["kind"] == "tamper_blocked"]
    payload = json.loads(blocked["payload"])
    assert payload["task_key"] == "T1" and payload["card_id"] == work
    assert "secret_added" in payload["detail"]
    assert "provider token" in payload["detail"]  # the KIND of secret, never the value

    _assert_absent(REPO_SECRET, _surfaces(world))


def test_22_10_a_key_in_the_controllers_own_environment_never_leaks(world_factory, monkeypatch):
    """Simulates "another [key] in the controller's environment": a second fake key set as an environment
    variable on the TEST PROCESS itself (never written to the repository, never read by any ASES code), which
    must never appear anywhere either, across an ordinary run that actually completes (write, review, merge).
    This should trivially hold, since nothing in ASES reads its own process environment into a card, an event or
    a report; the point is to prove that guarantee, not to hunt a bug (r6_wp_ac_e.md: "document it as a guarantee
    being proven, not a bug being hunted")."""
    monkeypatch.setenv("OPENROUTER_API_KEY", ENV_SECRET)

    world = world_factory(plan_raw=PLAN)
    world.fake.register_worker("coder-1", fw.touches_coder())  # an ordinary, unrelated success path
    world.create_cards()

    world.run_until(lambda w: w.all_merge_cards_done())

    _assert_absent(ENV_SECRET, _surfaces(world))


def test_22_10_a_whole_file_named_like_a_secret_blocks_gate_1(world_factory):
    """"The planted file blocks Gate 1": a coder adds `id_rsa` (placeholder content; the FILE NAME alone is what
    tamper.py reacts to, per _secret_file_marker). The finding kind is generated_artifact, not secret_added (see
    the module docstring)."""
    world = world_factory(plan_raw=SECRET_FILE_PLAN)
    world.fake.register_worker("coder-1", fw.sequence(
        fw.good_coder(
            {"a.py": "def add(x, y):\n    return x + y\n", "id_rsa": "placeholder, not a real key\n"},
            "add a.py and a stray key file",
        ),
        fw.questioner("halting further attempts for this test"),
    ))
    pair = world.create_cards()["T1"]
    work = pair.work_card_id

    _run_until_sent_back(world, work)

    card = world.card(work)
    assert card["status"] != "done"
    assert not any(run["profile"] == "reviewer" for run in card["_runs"])

    (blocked,) = [e for e in events.recent(world.conn, limit=500) if e["kind"] == "tamper_blocked"]
    payload = json.loads(blocked["payload"])
    assert "generated_artifact" in payload["detail"]
    assert "id_rsa" in payload["detail"]
    assert "ASES-GIT-07" in payload["detail"]
    assert "secret_added" not in payload["detail"]
