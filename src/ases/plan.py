"""plan.json schema and Gate 0 validation (section 9.1: plan.py; ASES-LED-01, ASES-TSK-03, ASES-GIT-08).

The Lead never describes a plan in chat -- it writes docs/ases/plan.json, and this module is the only
thing that decides whether that file is well-formed enough to create cards from. A plan that fails here
goes back to the Lead with the exact errors (once), then blocks for the user on a second failure --
that retry policy lives in cli.py, not here; this module only validates.

A plan that passes is then serialized (ASES-GIT-08): two tasks whose touches overlap and that have no
dependency path between them get one added, so they never run, and conflict, in parallel.

Round 9 (ASES-SEC-05, ASES-SEC-07): a task may also declare sandbox_network, an explicit exception to the
sandbox's default-deny network for its own Gate 1 and Gate 3 candidate runs (gates.resolve_runner reads it),
with sandbox_network_reason required whenever it is true. sandbox_network_exceptions() below is what
controller.pin_gate_profiles / verify_gate_pin fold into the gate_profiles pin, so flipping the flag after
approval is caught the same way an edited gate command is.
"""
from __future__ import annotations

import dataclasses
import fnmatch
import json
import pathlib
from collections.abc import Sequence

from . import tamper


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
    # ASES-QG-02 (section 14.3): true only when this task is deliberately allowed to touch gate, CI or
    # test-runner configuration with a touches glob broad enough to cover it (see _gate_config_violation).
    # Optional and False by default, so a plan written before this field existed parses unchanged.
    allow_gate_config_changes: bool = False
    # ASES-SEC-05, ASES-SEC-07 (round 9): an explicit, task-scoped exception to the sandbox's default-deny
    # network (gates.resolve_runner grants network ONLY to this task's own Gate 1 and Gate 3 candidate runs
    # when this is true). sandbox_network_reason must be non-empty whenever this is true (see
    # parse_and_validate); both are optional and default to false/"" so a plan written before this field
    # existed parses unchanged.
    sandbox_network: bool = False
    sandbox_network_reason: str = ""


@dataclasses.dataclass(frozen=True)
class SerializationLink:
    """A dependency Gate 0 added (ASES-GIT-08): `later` now depends on `earlier` because their touches
    overlap and nothing else ordered them."""
    later: str
    earlier: str
    reason: str


@dataclasses.dataclass(frozen=True)
class Plan:
    project: str
    integration_branch: str
    gate_profiles: dict[str, list[str]]
    tasks: tuple[PlanTask, ...]
    # Already applied to `tasks` as depends_on entries; kept so callers can report what Gate 0 added.
    serialization_links: tuple[SerializationLink, ...] = ()
    # ASES-TSK-04 (section 18.2): path globs that finalgates.run_gate4 drops from its blocking set, a plan-author
    # decision published and reviewed at Gate P (see config/swarm.yaml for where this belongs and why). Optional
    # and empty by default, so a plan written before this field existed parses unchanged.
    gate4_allowlist: tuple[str, ...] = ()

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
    else:
        # A profile with no commands is vacuously green (run_gate over [] passes and records a pass), so it
        # would let every diff through Gate 1 and Gate 3 unchecked (found by the merge-check builder).
        for name, commands in gate_profiles.items():
            if (not isinstance(commands, list) or not commands
                    or not all(isinstance(c, str) and c.strip() for c in commands)):
                errors.append(f"plan.gate_profiles.{name} must be a non-empty list of non-empty command strings")

    # ASES-TSK-04: optional, defaults to empty so a plan written before this field existed parses unchanged.
    gate4_allowlist_raw = raw.get("gate4_allowlist", [])
    if not isinstance(gate4_allowlist_raw, list) or not all(isinstance(g, str) for g in gate4_allowlist_raw):
        errors.append("plan.gate4_allowlist must be an array of path glob strings")
        gate4_allowlist_raw = []

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

        allow_gate_config = rt.get("allow_gate_config_changes", False)
        if not isinstance(allow_gate_config, bool):
            errors.append(
                f"{where} ({key}): allow_gate_config_changes must be true or false, got {allow_gate_config!r}"
            )
            allow_gate_config = False

        # ASES-SEC-05, ASES-SEC-07 (round 9): the task-scoped network exception. A reason is required whenever
        # the exception is granted, mirroring table 34's "gates the agent cannot edit" reasoning: a network
        # exception is a plan-author decision published and reviewed at Gate P, so it must say why, not just
        # that it exists.
        sandbox_network = rt.get("sandbox_network", False)
        if not isinstance(sandbox_network, bool):
            errors.append(f"{where} ({key}): sandbox_network must be true or false, got {sandbox_network!r}")
            sandbox_network = False

        sandbox_network_reason = rt.get("sandbox_network_reason", "")
        if not isinstance(sandbox_network_reason, str):
            errors.append(
                f"{where} ({key}): sandbox_network_reason must be a string, got {sandbox_network_reason!r}"
            )
            sandbox_network_reason = ""
        elif sandbox_network and not sandbox_network_reason.strip():
            errors.append(
                f"{where} ({key}): sandbox_network is true but sandbox_network_reason is empty; a network "
                "exception must say why the task's gate run needs it (ASES-SEC-05, ASES-SEC-07)"
            )

        touches = rt.get("touches")
        if not isinstance(touches, list):
            errors.append(f"{where} ({key}): touches must be an array, possibly empty (ASES-TSK-03)")
        elif not all(isinstance(g, str) for g in touches):
            # Serialization below compares touches as globs, so a non-string would crash Gate 0 (card
            # creation would fail on it too, when it joins the touches into the card body).
            errors.append(f"{where} ({key}): touches entries must be path glob strings")
        elif not allow_gate_config:
            # ASES-QG-02: a task whose touches names or is broad enough to also cover gate, CI or test-runner
            # configuration needs the marker, whether the touches entry is a wildcard or a literal path (round
            # 9, CIPIN): tamper.analyze_diff no longer reads a path being in touches as "a plan task allows
            # it" for gate_config_changed, only allow_gate_config_changes does, so a literal, single-file
            # touches on a gate-config path is no longer exempt either (see _gate_config_violation). Without
            # this, Gate 0 would approve a plan Gate 1 can never let through.
            for g in touches:
                hit = _gate_config_violation(g)
                if hit is not None:
                    errors.append(
                        f"{where} ({key}): touches {g!r} names or is broad enough to cover gate/CI "
                        f"configuration ({hit!r}); add \"allow_gate_config_changes\": true if this task is "
                        "meant to change it, or narrow the touches (ASES-QG-02)"
                    )

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
                allow_gate_config_changes=bool(allow_gate_config),
                sandbox_network=bool(sandbox_network), sandbox_network_reason=sandbox_network_reason,
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

    # ASES-GIT-08. Only a plan that passed every check above is serialized, so an error always describes
    # the plan as the Lead wrote it, and the added dependencies cannot close a cycle.
    serialized_tasks, serialization_links = serialize_overlapping_tasks(tasks)

    return Plan(
        project=raw["project"], integration_branch=raw["integration_branch"],
        gate_profiles=gate_profiles, tasks=serialized_tasks, serialization_links=serialization_links,
        gate4_allowlist=tuple(gate4_allowlist_raw),
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


_WILDCARDS = "*?["


def touches_overlap(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    """ASES-GIT-08: could a glob in `a` and a glob in `b` match a common path? An empty touches tuple
    (a task that changes no files, such as a reviewer) overlaps nothing."""
    return _first_overlap(a, b) is not None


def _first_overlap(a: tuple[str, ...], b: tuple[str, ...]) -> tuple[str, str] | None:
    """The first (glob from a, glob from b), as the Lead wrote them, that can match a common path."""
    for ga in a:
        for gb in b:
            if _globs_overlap(_normalize_glob(ga), _normalize_glob(gb)):
                return ga, gb
    return None


def _normalize_glob(glob: str) -> str:
    """Forward slashes and no leading "./" (a plan may say ./src/a.py where git says src/a.py)."""
    return glob.replace("\\", "/").removeprefix("./")


def _literal_prefix(glob: str) -> str:
    """The text before the first wildcard character; every path the glob matches starts with it. The whole
    glob when it has no wildcard, so a plain path (or directory) is treated as literal."""
    cut = min((glob.find(c) for c in _WILDCARDS if c in glob), default=len(glob))
    return glob[:cut]


def _literal_suffix(glob: str) -> str:
    """The text after the last wildcard; every path the glob matches ends with it. The whole glob when it
    has no wildcard. The "]" that closes a character class counts as a wildcard here: without it "[bc]"
    would leave "bc]" behind as if it were literal, and src/a[bc] would be judged disjoint from src/a*c."""
    return glob[max(glob.rfind(c) for c in _WILDCARDS + "]") + 1:]


def _globs_overlap(g1: str, g2: str) -> bool:
    """Could two normalized globs match a common path? Decided conservatively: a false yes only costs
    parallelism, a false no costs a merge conflict."""
    if g1 == g2:
        return True
    # One glob matches the other read as a literal path (src/* against src/a.py).
    if fnmatch.fnmatchcase(g1, g2) or fnmatch.fnmatchcase(g2, g1):
        return True
    # Otherwise compare what is literal at each end. Everything a glob matches starts with its literal
    # prefix and ends with its literal suffix, so two globs can share a path only if one prefix starts with
    # the other and one suffix ends with the other (an empty one trivially does, so a glob ending in a
    # wildcard never fails the suffix test). src/a/* against src/b/* fails the prefix test; src/*.py
    # against src/*.md fails the suffix test.
    p1, p2 = _literal_prefix(g1), _literal_prefix(g2)
    s1, s2 = _literal_suffix(g1), _literal_suffix(g2)
    return (p1.startswith(p2) or p2.startswith(p1)) and (s1.endswith(s2) or s2.endswith(s1))


def _gate_config_violation(touches_glob: str) -> str | None:
    """ASES-QG-02 (section 14.3): the gate-config pattern `touches_glob` names or is broad enough to also reach,
    or None. Checked with the exact same conservative glob-overlap semantics serialize_overlapping_tasks already
    uses (see _globs_overlap), so this check and the merge-time scope check never disagree about what a touches
    glob covers. A literal entry (no wildcard character at all) violates too when it is exactly one of the
    gate-config paths (two literal globs can only overlap by being equal, per _globs_overlap): round 9 (CIPIN)
    removed the old exemption here, because tamper.analyze_diff no longer reads a path being in touches, literal
    or wildcarded, as "a plan task allows it" for gate_config_changed -- only allow_gate_config_changes does. A
    plan Gate 0 approved without the marker must be a plan Gate 1 can actually let through."""
    overlap = _first_overlap((touches_glob,), tamper.GATE_CONFIG_PATTERNS)
    return overlap[1] if overlap is not None else None


def _reaches(deps: dict[str, list[str]], start: str, target: str) -> bool:
    """True when `start` depends on `target`, directly or through any chain of depends_on."""
    seen: set[str] = set()
    stack = [start]
    while stack:
        for dep in deps.get(stack.pop(), ()):
            if dep == target:
                return True
            if dep not in seen:
                seen.add(dep)
                stack.append(dep)
    return False


def serialize_overlapping_tasks(
    tasks: Sequence[PlanTask],
) -> tuple[tuple[PlanTask, ...], tuple[SerializationLink, ...]]:
    """ASES-GIT-08: two tasks whose touches overlap and that have no dependency path either way get
    `later depends_on earlier`, priority being position in the plan (listed first = higher). ASES-TSK-01/02
    make that dependency hold the later task's work card back until the earlier one is merged.

    Pairs are taken in plan order, and each link added counts as a dependency path for the pairs after it.
    A path in EITHER direction skips a pair: if the earlier task already depends on the later one, the plan's
    own order wins, and linking them would close a cycle. Tasks with empty touches are never serialized.

    Returns the tasks (added dependencies appended after the existing ones) and the links added. Run on its
    own output it adds nothing."""
    deps = {t.key: list(t.depends_on) for t in tasks}
    links: list[SerializationLink] = []
    for i, earlier in enumerate(tasks):
        for later in tasks[i + 1:]:
            overlap = _first_overlap(earlier.touches, later.touches)
            if overlap is None:
                continue
            if _reaches(deps, later.key, earlier.key) or _reaches(deps, earlier.key, later.key):
                continue
            deps[later.key].append(earlier.key)
            links.append(SerializationLink(
                later=later.key, earlier=earlier.key,
                reason=f"touches overlap: {overlap[0]!r} vs {overlap[1]!r}",
            ))
    serialized = tuple(dataclasses.replace(t, depends_on=tuple(deps[t.key])) for t in tasks)
    return serialized, tuple(links)


def sandbox_network_exceptions(plan: Plan) -> dict[str, list]:
    """ASES-SEC-05, ASES-SEC-07 (round 9): {task_key: [True, reason]} for every task that carries an explicit,
    Gate-0-validated network exception. Meant to be folded into gates.hash_gate_profiles by
    controller.pin_gate_profiles / verify_gate_pin, so flipping a task's sandbox_network after approval --
    without a fresh `swarm approve` -- is caught the same way an edited gate command is. A task with no
    exception (the default) is left out entirely, so a plan that sets none hashes exactly as it did before this
    field existed."""
    return {t.key: [True, t.sandbox_network_reason] for t in plan.tasks if t.sandbox_network}


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
