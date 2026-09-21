"""The types shared by the evaluation harness and its tasks (blueprint Appendix D).

They live here, not in evals.py, because the tasks (evalkit/tasks.py) build EvalTask and Score objects and evals.py
imports the tasks: keeping the types in a third module avoids an import cycle. evals.py re-exports every name below,
so `evals.Candidate`, `evals.EvalTask` and the rest are the public spelling.
"""
from __future__ import annotations

import dataclasses
import pathlib
from collections.abc import Callable

# Task kinds. "text" and "repo" run standalone through one-shot Hermes calls; "swarm" needs the whole controller
# (E8) and "comparison" compares two finished runs (E11), so the runner refuses both (see evals.run_eval).
KIND_TEXT = "text"
KIND_REPO = "repo"
KIND_SWARM = "swarm"
KIND_COMPARISON = "comparison"
STANDALONE_KINDS = (KIND_TEXT, KIND_REPO)


class EvalError(Exception):
    """Bad input to the harness: an unknown task id or candidate label, an unreadable run directory. The CLI
    prints the message and exits 1; it is never a crash."""


class EvalRefused(EvalError):
    """A run that must not start: the day's quota cannot afford it (ASES-CAP-03), or it names a task that cannot
    run standalone. Raised before anything is called, so a refusal never costs a request."""


@dataclasses.dataclass(frozen=True)
class Candidate:
    """One thing being evaluated: a model reached through a provider, played by a Hermes profile (blueprint
    Appendix D: replay the same tasks against candidate models through different providers). `label` is
    `provider/model` exactly as config/models.yaml declares the pair, and is what every record and report calls it."""

    provider: str
    model: str
    role_class: str | None
    profile: str
    label: str


@dataclasses.dataclass(frozen=True)
class Score:
    """What a scorer measured, kept as separate numbers (Appendix D.2: never collapsed into one score).
    `tests_passed` and `tests_total` are None for a task with no test suite; `findings` holds the individual
    checks (int, bool or str values only, and no key containing 'key', 'token' or 'secret' with a string value,
    because the event redactor blanks such a value wholesale before it is stored)."""

    success: bool
    tests_passed: int | None = None
    tests_total: int | None = None
    findings: dict = dataclasses.field(default_factory=dict)
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "success": bool(self.success),
            "tests_passed": self.tests_passed,
            "tests_total": self.tests_total,
            "findings": {
                str(k): v if isinstance(v, (bool, int, float, str)) else str(v) for k, v in self.findings.items()
            },
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Score:
        findings = data.get("findings")
        return cls(
            success=bool(data.get("success", False)),
            tests_passed=data.get("tests_passed"),
            tests_total=data.get("tests_total"),
            findings=dict(findings) if isinstance(findings, dict) else {},
            notes=str(data.get("notes") or ""),
        )


@dataclasses.dataclass(frozen=True)
class InvokeResult:
    """What one model call returned. The first seven fields are the contract every `invoke` callable fills; the
    rest have defaults, so a fake that builds an InvokeResult with seven values keeps working.

    `requests` is how many model API calls the run made (what a provider's daily quota counts); `input_tokens` and
    `output_tokens` are None when unknown. `served_model` is the model that actually answered when the caller could
    tell (a different one than was asked for means a fallback answered). `attempts` holds the stdout of every
    attempt of a task that allows a corrected retry (E9), oldest first; the runner fills it, an invoke never does."""

    returncode: int
    stdout: str
    stderr: str
    latency_seconds: float
    requests: int
    input_tokens: int | None
    output_tokens: int | None
    served_model: str | None = None
    session_id: str | None = None
    timed_out: bool = False
    attempts: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class EvalTask:
    """One evaluation task (blueprint Appendix D.1): a small fixture built in code, a prompt, and a scorer that
    needs no model. Nothing about a task calls a provider.

    build_fixture(workdir) writes the seeded repository or context into `workdir` (a fresh temp directory) and
    returns a dict the other two callables read. build_prompt(fixture) is the one prompt sent to the model.
    score(fixture, workdir, output, invoke_result) turns the model's answer into a Score. `est_requests` is what
    one run is expected to cost against the provider's daily quota. `tools` names the Hermes toolsets the task
    needs (`-t`): empty means a pure text answer. `retry_prompt` and `max_retries` let a task allow a corrected
    retry (E9): retry_prompt(fixture, last_output) returns the follow-up prompt, or None when no retry is due."""

    id: str
    title: str
    kind: str
    build_fixture: Callable[[pathlib.Path], dict]
    build_prompt: Callable[[dict], str]
    score: Callable[[dict, pathlib.Path, str, InvokeResult], Score]
    est_requests: int
    tools: tuple[str, ...] = ()
    max_retries: int = 0
    retry_prompt: Callable[[dict, str], str | None] | None = None
    timeout_seconds: int = 600
    what: str = ""
