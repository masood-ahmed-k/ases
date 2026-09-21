"""The registry of evaluation tasks E1 to E11 (blueprint Appendix D.1) and how a list of task ids is read.

E1 to E7, E9 and E10 run standalone: one fixture, one prompt, one model call (E9 allows one corrected retry), one
scorer that needs no model. E8 (Long-horizon task) needs the whole swarm and E11 (Role value) compares two finished
runs, so both are DESCRIPTORS here: they appear in the list with what they need, and the runner refuses to run them
and says how (refusal_for). Phase 2 of the roadmap runs only E1, E9 and E10 on a short list; the full set waits for
Phase 7.
"""
from __future__ import annotations

import pathlib

from . import codetasks, texttasks
from .model import KIND_COMPARISON, STANDALONE_KINDS, EvalError, EvalTask, InvokeResult, Score

E11_REFUSAL = (
    "E11 (Role value) is a comparison, not a task: it needs two finished runs of the SAME tasks, one through the role "
    "profile and one through the core roster (for example `swarm eval run ... --profile tester` and again with "
    "`--profile coder-1`). Then compare them with `swarm eval role-value WITH_RUN WITHOUT_RUN`."
)


def _e11_fixture(workdir: pathlib.Path) -> dict:
    return {}


def _e11_prompt(fixture: dict) -> str:
    return ""


def _e11_score(fixture: dict, workdir: pathlib.Path, output: str, result: InvokeResult) -> Score:
    return Score(False, notes="E11 compares two finished runs: use `swarm eval role-value`, not a scorer")


E11 = EvalTask(
    id="E11", title="Role value", kind=KIND_COMPARISON, build_fixture=_e11_fixture, build_prompt=_e11_prompt,
    score=_e11_score, est_requests=0,
    what="does a separate role profile beat the core roster on the same tasks (a comparison of two runs)",
)

TASKS: dict[str, EvalTask] = {
    task.id: task
    for task in (
        texttasks.E1, texttasks.E2, texttasks.E3, codetasks.E4, codetasks.E5, codetasks.E6, texttasks.E7,
        codetasks.E8, texttasks.E9, texttasks.E10, E11,
    )
}
ALL_IDS: tuple[str, ...] = tuple(TASKS)
STANDALONE_IDS: tuple[str, ...] = tuple(t.id for t in TASKS.values() if t.kind in STANDALONE_KINDS)
# Appendix D: 'Phase 2 runs only E1, E9 and E10 on a short list, and the full set waits for Phase 7.'
PHASE2_IDS: tuple[str, ...] = ("E1", "E9", "E10")


def refusal_for(task: EvalTask) -> str | None:
    """Why `task` cannot be run standalone, with the instruction for running it properly; None when it can be run."""
    if task.kind in STANDALONE_KINDS:
        return None
    return codetasks.E8_REFUSAL if task.id == "E8" else E11_REFUSAL


def parse_task_ids(spec: str) -> list[EvalTask]:
    """A comma separated list such as 'E1,E9,e10' as EvalTask objects, in the order given, without repeats. 'all'
    means every task that can run standalone. An unknown id is an EvalError, never skipped."""
    tasks: list[EvalTask] = []
    for part in (piece.strip() for piece in spec.split(",")):
        if not part:
            continue
        if part.lower() == "all":
            wanted = [TASKS[i] for i in STANDALONE_IDS]
        elif part.upper() in TASKS:
            wanted = [TASKS[part.upper()]]
        else:
            raise EvalError(f"unknown task {part!r}; the tasks are {', '.join(ALL_IDS)} (or 'all')")
        for task in wanted:
            if task not in tasks:
                tasks.append(task)
    if not tasks:
        raise EvalError("no tasks were named; give a comma separated list such as E1,E9,E10")
    return tasks
