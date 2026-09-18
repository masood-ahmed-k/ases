"""plan.json schema and Gate 0 validation (section 9.1: plan.py; ASES-LED-01, ASES-TSK-03).

The Lead never describes a plan in chat -- it writes docs/ases/plan.json, and this module is the only
thing that decides whether that file is well-formed enough to create cards from. A plan that fails here
goes back to the Lead with the exact errors (once), then blocks for the user on a second failure --
that retry policy lives in cli.py, not here; this module only validates.
"""
from __future__ import annotations

import dataclasses
import json
import pathlib


@dataclasses.dataclass(frozen=True)
class PlanTask:
    key: str
    title: str
    role: str
    depends_on: tuple[str, ...]
    touches: tuple[str, ...]
    acceptance: tuple[str, ...]
    gate_profile: str
    estimated_requests: int


@dataclasses.dataclass(frozen=True)
class Plan:
    project: str
    integration_branch: str
    gate_profiles: dict[str, list[str]]
    tasks: tuple[PlanTask, ...]

    def task(self, key: str) -> PlanTask:
        for t in self.tasks:
            if t.key == key:
                return t
        raise KeyError(key)


class PlanError(Exception):
    """Raised with every validation error found, not just the first, so a repair pass sees them all."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def _require_keys(d: dict, keys: list[str], where: str, errors: list[str]) -> None:
    for k in keys:
        if k not in d:
            errors.append(f"{where}: missing required key '{k}'")


def parse_and_validate(raw: dict, *, known_roles: set[str], max_cards: int) -> Plan:
    """Gate 0. Raises PlanError with every problem found; never partially trusts a bad plan."""
    errors: list[str] = []
    _require_keys(raw, ["project", "integration_branch", "gate_profiles", "tasks"], "plan", errors)
    if errors:
        raise PlanError(errors)

    gate_profiles = raw["gate_profiles"]
    if not isinstance(gate_profiles, dict) or not gate_profiles:
        errors.append("plan.gate_profiles must be a non-empty object")

    raw_tasks = raw["tasks"]
    if not isinstance(raw_tasks, list) or not raw_tasks:
        errors.append("plan.tasks must be a non-empty array")
        raise PlanError(errors)

    if len(raw_tasks) > max_cards:
        errors.append(f"plan has {len(raw_tasks)} tasks, exceeding max_cards={max_cards}")

    seen_keys: set[str] = set()
    tasks: list[PlanTask] = []
    for i, rt in enumerate(raw_tasks):
        where = f"tasks[{i}]"
        _require_keys(rt, ["key", "title", "role", "acceptance", "touches", "gate_profile"], where, errors)
        if "key" not in rt:
            continue
        key = rt["key"]
        if key in seen_keys:
            errors.append(f"{where}: duplicate task key '{key}'")
        seen_keys.add(key)

        role = rt.get("role")
        if role is not None and role not in known_roles:
            errors.append(f"{where} ({key}): unknown role '{role}', expected one of {sorted(known_roles)}")

        acceptance = rt.get("acceptance") or []
        if not isinstance(acceptance, list) or not acceptance:
            errors.append(f"{where} ({key}): acceptance must be a non-empty array (ASES-TSK-03)")

        touches = rt.get("touches")
        if not isinstance(touches, list):
            errors.append(f"{where} ({key}): touches must be an array, possibly empty (ASES-TSK-03)")

        gate_profile = rt.get("gate_profile")
        if gate_profile is not None and gate_profile not in gate_profiles:
            errors.append(f"{where} ({key}): gate_profile '{gate_profile}' not declared in plan.gate_profiles")

        estimated_requests = rt.get("estimated_requests", 0)
        if not isinstance(estimated_requests, int) or estimated_requests <= 0:
            errors.append(f"{where} ({key}): estimated_requests must be a positive integer")

        depends_on = tuple(rt.get("depends_on") or [])

        if "key" in rt and "role" in rt:
            tasks.append(PlanTask(
                key=key, title=rt.get("title", ""), role=role, depends_on=depends_on,
                touches=tuple(touches) if isinstance(touches, list) else (),
                acceptance=tuple(acceptance) if isinstance(acceptance, list) else (),
                gate_profile=gate_profile or "", estimated_requests=estimated_requests,
            ))

    # Dangling dependencies and cycles, checked over whatever tasks parsed even if some rows had errors --
    # a plan can have both a missing field on task 3 AND a cycle between tasks 1 and 2.
    keys_present = {t.key for t in tasks}
    for t in tasks:
        for dep in t.depends_on:
            if dep not in keys_present:
                errors.append(f"task '{t.key}' depends_on unknown task '{dep}'")

    _check_cycles(tasks, errors)

    if errors:
        raise PlanError(errors)

    return Plan(
        project=raw["project"], integration_branch=raw["integration_branch"],
        gate_profiles=gate_profiles, tasks=tuple(tasks),
    )


def _check_cycles(tasks: list[PlanTask], errors: list[str]) -> None:
    graph = {t.key: [d for d in t.depends_on if d in {x.key for x in tasks}] for t in tasks}
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {k: WHITE for k in graph}

    def visit(node: str, path: list[str]) -> None:
        color[node] = GRAY
        for dep in graph[node]:
            if color[dep] == GRAY:
                cycle = " -> ".join(path[path.index(dep):] + [dep])
                errors.append(f"dependency cycle: {cycle}")
            elif color[dep] == WHITE:
                visit(dep, path + [dep])
        color[node] = BLACK

    for k in list(graph):
        if color[k] == WHITE:
            visit(k, [k])


def load_plan_file(path: str | pathlib.Path, *, known_roles: set[str], max_cards: int) -> Plan:
    path = pathlib.Path(path)
    if not path.exists():
        raise PlanError([f"plan file not found: {path}"])
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PlanError([f"plan.json is not valid JSON: {exc}"]) from exc
    return parse_and_validate(raw, known_roles=known_roles, max_cards=max_cards)


def topological_order(plan: Plan) -> list[str]:
    """Task keys in an order where every dependency precedes its dependents. Assumes no cycles
    (call parse_and_validate first -- it already rejected cycles)."""
    remaining = {t.key: set(t.depends_on) for t in plan.tasks}
    order: list[str] = []
    while remaining:
        ready = [k for k, deps in remaining.items() if not deps]
        if not ready:
            raise PlanError([f"unresolved dependencies (should have been caught by Gate 0): {remaining}"])
        for k in sorted(ready):
            order.append(k)
            del remaining[k]
        for deps in remaining.values():
            deps -= set(ready)
    return order
