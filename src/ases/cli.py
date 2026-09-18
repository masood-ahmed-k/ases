"""ASES command-line interface (section 9.1: cli.py).

Phase 0/1 implements `doctor` and `models`. The rest of the roadmap's command list (init, run, plan,
approve, status, questions, answer, stop, resume, eval, report) is stubbed here with a clear "not built
yet, see section 16" message instead of argparse silently accepting an unimplemented verb.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

from . import config as ases_config
from . import db as ases_db
from . import doctor as ases_doctor
from . import models as models_mod

_GLYPH = {"pass": "[PASS]", "warn": "[WARN]", "fail": "[FAIL]", "pending": "[PEND]"}
_NOT_BUILT_YET = {
    "init", "run", "plan", "approve", "status", "questions", "answer", "stop", "resume", "eval", "report",
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
