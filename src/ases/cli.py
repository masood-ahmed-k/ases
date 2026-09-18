"""ASES command-line interface (section 9.1: cli.py).

Phase 0/1 implements `doctor` and `models`. The rest of the roadmap's command list (init, run, plan,
approve, status, questions, answer, stop, resume, eval, report) is stubbed here with a clear "not built
yet, see section 16" message instead of argparse silently accepting an unimplemented verb.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import time

from . import config as ases_config
from . import controller as controller_mod
from . import db as ases_db
from . import doctor as ases_doctor
from . import hermes as hermes_mod
from . import models as models_mod
from . import plan as plan_mod
from . import policy as policy_mod

_GLYPH = {"pass": "[PASS]", "warn": "[WARN]", "fail": "[FAIL]", "pending": "[PEND]"}
_NOT_BUILT_YET = {
    "init", "status", "questions", "answer", "eval", "report",
}


def _repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[2]


def _load_project() -> ases_config.ProjectConfig:
    return ases_config.load_swarm_config(_repo_root() / "config" / "swarm.yaml")


def _load_models_config() -> dict:
    return ases_config.load_models_config(_repo_root() / "config" / "models.yaml")


def cmd_doctor(_args: argparse.Namespace) -> int:
    project = _load_project()
    models_config = _load_models_config()
    conn = ases_db.connect(ases_config.db_path(project))
    models_mod.sync_from_config(conn, models_config)

    report = ases_doctor.run(project, models_config, conn)
    for check in report.checks:
        ids = f" ({', '.join(check.requirement_ids)})" if check.requirement_ids else ""
        print(f"{_GLYPH[check.status]} {check.name}: {check.detail}{ids}")
    print()
    print("HEALTHY" if report.ok else "NOT HEALTHY -- see FAIL lines above")
    return report.exit_code


def cmd_models(_args: argparse.Namespace) -> int:
    project = _load_project()
    models_config = _load_models_config()
    conn = ases_db.connect(ases_config.db_path(project))
    models_mod.sync_from_config(conn, models_config)

    for m in models_mod.list_models(conn):
        ctx = m.context_length if m.context_length is not None else "undeclared"
        smoke = m.smoke_test_result or "not run"
        pin = "pinned" if m.pinned else "unpinned"
        print(f"{m.provider}/{m.model}  role={m.role_class or '-'}  context={ctx}  smoke={smoke}  {pin}")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    """Invoke the lead profile to write docs/ases/plan.json into the target repo.

    Deliberately not --in / cwd-dependent (the environment bug from Phase 2): the prompt names the
    exact absolute repo path and tells the model not to rely on any inherited working directory.
    """
    project = _load_project()
    repo = pathlib.Path(args.repo).resolve()
    request = args.request
    prompt = (
        f"Repository (use this exact absolute path in every tool call, do not rely on any "
        f"current/working directory): {repo}\n\n"
        f"Project request: {request}\n\n"
        f"Inspect the repository at that path, then write docs/ases/plan.json (absolute path: "
        f"{repo / 'docs' / 'ases' / 'plan.json'}) with this exact top-level shape: "
        f'{{"project": "<slug>", "integration_branch": "{project.integration_branch}", '
        f'"gate_profiles": {{"<name>": ["<shell command>", ...]}}, "tasks": [{{"key": "T1", '
        f'"title": "...", "role": "coder"|"reviewer", "depends_on": ["<task key>", ...], '
        f'"touches": ["<path glob>", ...], "acceptance": ["<criterion>", ...], '
        f'"gate_profile": "<name>", "estimated_requests": <int>}}]}}. '
        f"Keep it small: 2 to 4 tasks. Every task's role must be exactly 'coder' or 'reviewer'. "
        f"Use a trivial, fast gate_profile command since this is a throwaway test repo. "
        f"After writing the file, reply with just the word done."
    )
    result = subprocess.run(
        [hermes_mod.hermes_path(), "-p", "lead", "-z", prompt],
        capture_output=True, text=True, timeout=600, encoding="utf-8", errors="replace",
    )
    print(result.stdout.strip() or result.stderr.strip())
    plan_path = repo / "docs" / "ases" / "plan.json"
    if not plan_path.exists():
        print(f"lead did not write {plan_path}", file=sys.stderr)
        return 1
    print(f"wrote {plan_path}")
    return 0 if result.returncode == 0 else 1


def cmd_approve(args: argparse.Namespace) -> int:
    """Gate 0 on docs/ases/plan.json, then create work+merge card pairs (ASES-LED-01/02)."""
    project = _load_project()
    conn = ases_db.connect(ases_config.db_path(project))
    repo = pathlib.Path(args.repo).resolve()
    plan_path = repo / "docs" / "ases" / "plan.json"

    try:
        plan = plan_mod.load_plan_file(
            plan_path, known_roles=set(project.roles), max_cards=project.budgets.get("max_cards", 40)
        )
    except plan_mod.PlanError as exc:
        print("Gate 0 FAILED:")
        for e in exc.errors:
            print(f"  - {e}")
        return 1
    print(f"Gate 0 passed: {len(plan.tasks)} tasks")

    models_config = _load_models_config()
    per_provider: dict[str, int] = {}
    for task in plan.tasks:
        pp = policy_mod.profile_provider(task.role, models_config)
        if pp is not None:
            per_provider[pp.provider] = per_provider.get(pp.provider, 0) + task.estimated_requests
    unaffordable = []
    for provider, total in per_provider.items():
        afford = policy_mod.check_budget(conn, models_config["providers"], provider, total, budgets=project.budgets)
        print(f"  budget[{provider}]: needs {total}, {afford.reason}")
        if not afford.can_afford:
            unaffordable.append(provider)
    if unaffordable:
        print(f"Gate P REFUSED: cannot afford this plan today on {unaffordable} (ASES-CAP-03). "
              f"Wait for the quota reset or shrink the plan.")
        return 1

    publish_sha = controller_mod.publish_plan(repo, plan.integration_branch)
    print(f"Gate P: published approved plan at {publish_sha}")

    pairs = controller_mod.create_cards_from_plan(
        project.board, args.project_id, repo, plan, project, conn=conn
    )
    for p in pairs:
        print(f"  {p.task_key}: work={p.work_card_id} merge={p.merge_card_id}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Bounded controller loop (section 9.2): dispatch, review-lane policing, merge queue, repeat
    until every merge card is done or max-iterations is hit."""
    project = _load_project()
    conn = ases_db.connect(ases_config.db_path(project))
    repo = pathlib.Path(args.repo).resolve()
    plan_path = repo / "docs" / "ases" / "plan.json"
    plan = plan_mod.load_plan_file(
        plan_path, known_roles=set(project.roles), max_cards=project.budgets.get("max_cards", 40)
    )

    from . import reconcile as reconcile_mod
    findings = reconcile_mod.check(project.board, plan.project, conn=conn)
    for f in findings:
        print(f"[RECONCILE] {f.task_key} {f.kind}: {f.detail}")
    if findings:
        print(f"{len(findings)} inconsistency(ies) found on start -- see above. Continuing; "
              f"none of these are auto-repaired yet (Phase 3 scope).")

    for i in range(args.max_iterations):
        summary = controller_mod.run_pass(project.board, repo, plan, project, conn=conn)
        print(f"[pass {i + 1}] merged={summary['merged']} sent_back={summary['sent_back']} "
              f"finished={summary['finished']}")
        if summary["finished"]:
            print("all merge cards done")
            return 0
        time.sleep(args.sleep_seconds)

    print(f"stopped after {args.max_iterations} passes without finishing (not a failure -- "
          f"just the bound; re-run swarm run to continue polling)")
    return 1


def cmd_stop(args: argparse.Namespace) -> int:
    """ASES-REC-06: swarm stop halts new dispatch and reclaims every running card. Deliberately does
    NOT hunt down and kill arbitrary hermes.exe processes -- the user may have unrelated Hermes chat
    sessions open, and this module has no reliable way to tell an ASES worker's PID from theirs.
    hermes pause + reclaim already gives the real safety property (no new work starts, and a
    reclaimed card's in-flight run is abandoned per Hermes's own semantics)."""
    project = _load_project()
    conn = ases_db.connect(ases_config.db_path(project))
    try:
        hermes_mod.pause(reason="swarm stop")
    except hermes_mod.HermesCommandError as exc:
        print(f"hermes pause failed: {exc}", file=sys.stderr)
        return 1

    reclaimed = []
    for card in hermes_mod.kanban_list(project.board, status="running"):
        try:
            hermes_mod.kanban_reclaim(project.board, card["id"], reason="swarm stop")
            reclaimed.append(card["id"])
        except hermes_mod.HermesCommandError as exc:
            print(f"could not reclaim {card['id']}: {exc}", file=sys.stderr)

    from . import events as events_mod
    events_mod.record(conn, "swarm_stop", {"reclaimed": reclaimed})
    print(f"paused dispatch; reclaimed {len(reclaimed)} running card(s): {reclaimed}")
    return 0


def cmd_resume(_args: argparse.Namespace) -> int:
    try:
        hermes_mod.resume()
    except hermes_mod.HermesCommandError as exc:
        print(f"hermes resume failed: {exc}", file=sys.stderr)
        return 1
    print("resumed")
    return 0


def cmd_not_built_yet(args: argparse.Namespace) -> int:
    print(f"`swarm {args.command}` is not built yet. See section 16 (implementation phases) for when it lands.")
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="swarm", description="ASES: AI Software Engineering Swarm")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="Check the ASES + Hermes environment (acceptance test 22.1)").set_defaults(
        func=cmd_doctor
    )
    sub.add_parser("models", help="List the model registry and its declared capabilities").set_defaults(
        func=cmd_models
    )

    p_plan = sub.add_parser("plan", help="Invoke the lead profile to write docs/ases/plan.json")
    p_plan.add_argument("--repo", required=True, help="Absolute path to the target repository")
    p_plan.add_argument("--request", required=True, help="The project request, in plain language")
    p_plan.set_defaults(func=cmd_plan)

    p_approve = sub.add_parser("approve", help="Gate 0 the plan, then create work+merge cards")
    p_approve.add_argument("--repo", required=True)
    p_approve.add_argument("--project-id", required=True, help="Hermes project id (see `hermes project list`)")
    p_approve.set_defaults(func=cmd_approve)

    p_run = sub.add_parser("run", help="Bounded controller loop: dispatch, review, merge, repeat")
    p_run.add_argument("--repo", required=True)
    p_run.add_argument("--max-iterations", type=int, default=30)
    p_run.add_argument("--sleep-seconds", type=int, default=20)
    p_run.set_defaults(func=cmd_run)

    sub.add_parser("stop", help="Kill switch: pause dispatch, reclaim running cards (ASES-REC-06)").set_defaults(
        func=cmd_stop
    )
    sub.add_parser("resume", help="Lift a swarm stop").set_defaults(func=cmd_resume)

    for name in sorted(_NOT_BUILT_YET):
        sub.add_parser(name, help="(not built yet)").set_defaults(func=cmd_not_built_yet)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ases_config.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
