"""Acceptance 22.10, gate-environment half (blueprint.txt [p418]; ASES-CFG-04, ASES-CFG-05, round 8).

"env inside a worker terminal shows no provider key" -- test_22_10_secrets.py proves the leak-into-a-card-or-
report half of acceptance 22.10 end to end; this file proves the other half named there, now that round 8 closed
it: a gate command itself, run by the controller's own host gate runner, cannot see a provider key planted in the
controller's own process environment. Per r8_wp_gateenv.md: "An acceptance-level check ... a world_factory world
whose gate profile command prints the presence marker, with a planted OPENROUTER_API_KEY in the test process
environment, driven through a real controller pass on FakeHermes; assert the recorded Gate 1 (and Gate 3 if the
scenario reaches it) output says NOKEY."

Like test_22_10_secrets.py, this drives the real controller (create_cards_from_plan, run_pass) on FakeHermes; the
only thing scripted is the coder/reviewer personas (the default ones from conftest.py: a coder that writes the
files its card touches, a reviewer that approves what it is shown). Nothing here starts a real Hermes, a real
model provider, or Docker: the gate profile's own command is the probe, run by gates.py's host runner
(_run_commands), which is exactly what round 8 fixed.
"""
import sys

PLANTED_KEY = "sk-or-v1-PLANTEDGATEENVVALUE0123456789"

# A python -c command that prints KEYSEEN if OPENROUTER_API_KEY is visible to it, NOKEY otherwise. sys.executable
# is quoted so the probe does not depend on a PATH lookup of "python" (same discipline as tests/unit/test_gates.py).
PRESENCE_PROBE = (
    f'"{sys.executable}" -c '
    '"import os; print(\'KEYSEEN\' if os.environ.get(\'OPENROUTER_API_KEY\') else \'NOKEY\')"'
)

PLAN = {
    "project": "acceptance-gate-env",
    "integration_branch": "integration",
    "gate_profiles": {"trivial": [PRESENCE_PROBE]},
    "tasks": [
        {"key": "T1", "title": "add a", "role": "coder", "depends_on": [], "touches": ["a.py"],
         "acceptance": ["a.py defines add(x, y) returning x + y"], "gate_profile": "trivial",
         "estimated_requests": 5},
    ],
}


def _gate_runs(world) -> list[dict]:
    return [dict(r) for r in world.conn.execute("SELECT gate, result, detail FROM gate_runs ORDER BY id")]


def test_22_10_a_planted_key_in_the_controllers_environment_is_invisible_to_a_gate_command(world_factory, monkeypatch):
    """Plants OPENROUTER_API_KEY on the TEST PROCESS's own environment (never written to the repository, never
    read by any ASES code) and runs an ordinary task to completion. The plan's only gate command is the presence
    probe, so every gate_runs row this scenario writes must say NOKEY: Gate 1 (the review-lane re-check) and, once
    the card merges, Gate 3 (the merge-queue gate) and gate3-postmerge (the post-merge re-check on the new
    integration HEAD, ASES-GIT-05) all run the same host runner, gates._run_commands, through the one choke point
    round 8 fixed."""
    monkeypatch.setenv("OPENROUTER_API_KEY", PLANTED_KEY)

    world = world_factory(plan_raw=PLAN)
    world.create_cards()

    world.run_until(lambda w: w.all_merge_cards_done())

    rows = _gate_runs(world)
    assert rows, "expected at least one gate_runs row (Gate 1 and/or Gate 3) for this scenario"

    # The probe's own printed line, never the echoed "$ <cmd>" line gates.py's runner puts above it: the command
    # TEXT itself contains the literal words "KEYSEEN" and "NOKEY" (it is python source that names both branches
    # of the if/else), so checking the whole row for those words would pass or fail for the wrong reason. Only the
    # last line is the probe's actual stdout.
    def last_line(row):
        return row["detail"].strip().splitlines()[-1]

    gate1_rows = [r for r in rows if r["gate"] == "gate1"]
    assert gate1_rows, "expected the review lane's Gate 1 re-check to have run and recorded a row"
    for row in gate1_rows:
        assert row["result"] == "pass"
        assert last_line(row) == "NOKEY"

    gate3_rows = [r for r in rows if r["gate"].startswith("gate3")]
    for row in gate3_rows:
        assert row["result"] == "pass"
        assert last_line(row) == "NOKEY"

    # Never PLANTED_KEY itself in any gate_runs row this scenario wrote, and never a KEYSEEN VERDICT (the probe's
    # actual printed output, as opposed to the word appearing in the echoed command text above it).
    assert not any(PLANTED_KEY in row["detail"] for row in rows)
    assert not any(last_line(row) == "KEYSEEN" for row in rows)
