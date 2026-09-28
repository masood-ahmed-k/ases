"""The evaluation harness: replay the same tasks against candidate models, keep the raw results locally, and compare
runs before a pinned model changes (blueprint Appendix D and the Phase 7 row of section 16; the entry point behind
`swarm eval`).

Appendix D: "Do not trust a static list of free models. Build a tiny evaluation harness. Replay the same tasks against
candidate models through different providers. Store raw results locally. Evaluation spends real quota, so Phase 2 runs
only E1, E9 and E10 on a short list, and the full set waits for Phase 7." The Phase 7 row adds "regression check before
changing a pinned model". This module is the data-plus-runner half of that: the tasks (E1 to E11) live in
evalkit/, each a small fixture built in code with a deterministic scorer that needs no model, and this file owns
everything around them: what a run costs, whether the day's quota can afford it, the runner, the one function that
really calls a model (default_invoke), the reports, the regression check and the recommendations.

STOP CONDITION (ASES-DOC-04): "Claude Code MUST stop and ask the user before any action that spends money ..." Evaluation
spends real provider quota, so nothing here calls a model unless the caller passed spend=True (the CLI flag is
--spend-quota); without it run_eval returns the plan and its cost and calls nothing. Tests never call a model: every call
goes through an injectable `invoke`.
"""
from __future__ import annotations

import argparse
import dataclasses
import inspect
import json
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timezone

from . import events, hermes, ledger, policy, procenv
from .evalkit import codetasks
from .evalkit import tasks as tasks_mod
from .evalkit.model import (  # re-exported: these are the public spellings
    Candidate,
    EvalError,
    EvalRefused,
    EvalTask,
    InvokeResult,
    Score,
)
from .evalkit.text import ascii_safe, clip

TASKS = tasks_mod.TASKS
score_swarm_project = codetasks.score_swarm_project

__all__ = [
    "Candidate", "EvalTask", "Score", "InvokeResult", "EvalError", "EvalRefused", "RunRecord", "Estimate", "RunSummary",
    "Regression", "RoleValue", "TASKS", "estimate", "calendar_minutes", "check_budget", "run_eval", "default_invoke",
    "load_run", "render_report", "compare", "role_value", "recommend", "candidate_from_config", "load_candidates",
    "score_swarm_project", "main",
]

DEFAULT_TOLERANCE = 0.25  # requests or latency may grow by this fraction before compare() calls it a regression
_IS_WINDOWS = os.name == "nt"  # a module attribute so a test can flip it without touching os.name
_WINDOWS_CMDLINE_LIMIT = 32000  # CreateProcess allows 32766 characters in all; keep a margin for the exe path
_NO_COMBINED_SCORE = (
    "Measurements are reported separately and never merged into one number (blueprint Appendix D.2)."
)
PINNING_SENTENCE = (
    "Pinning or changing a model is your decision: this report never edits config/models.yaml, and ASES never switches "
    "a pinned model on its own."
)


# =====================================================================================================================
# Records
# =====================================================================================================================


def _int_or_none(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


@dataclasses.dataclass(frozen=True)
class RunRecord:
    """One (task, candidate) run with every Appendix D.2 metric kept as its own field: success (inside `score`), tests
    passed, review changes required, retries, fallbacks, latency, requests, approximate token usage and human
    interventions. `input_tokens` and `output_tokens` are None when the model call could not say. `served_model` is the
    model that answered when it was reported (a different one than was asked for is what `fallbacks` counts). `error`
    is empty for a run that completed; a failed run keeps its reason here and a Score with success False."""

    task_id: str
    candidate: str
    run_id: str
    started_at: str
    latency_seconds: float
    requests: int
    input_tokens: int | None
    output_tokens: int | None
    retries: int
    fallbacks: int
    review_changes_required: int
    human_interventions: int
    score: Score
    raw_output_path: str | None
    error: str = ""
    served_model: str | None = None

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id, "candidate": self.candidate, "run_id": self.run_id,
            "started_at": self.started_at, "latency_seconds": round(float(self.latency_seconds), 3),
            "requests": int(self.requests), "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
            "retries": int(self.retries), "fallbacks": int(self.fallbacks),
            "review_changes_required": int(self.review_changes_required),
            "human_interventions": int(self.human_interventions), "score": self.score.to_dict(),
            "raw_output_path": self.raw_output_path, "error": self.error, "served_model": self.served_model,
        }

    @classmethod
    def from_dict(cls, data: dict) -> RunRecord:
        score = data.get("score")
        return cls(
            task_id=str(data["task_id"]), candidate=str(data["candidate"]), run_id=str(data.get("run_id", "")),
            started_at=str(data.get("started_at", "")), latency_seconds=float(data.get("latency_seconds") or 0.0),
            requests=int(data.get("requests") or 0), input_tokens=_int_or_none(data.get("input_tokens")),
            output_tokens=_int_or_none(data.get("output_tokens")), retries=int(data.get("retries") or 0),
            fallbacks=int(data.get("fallbacks") or 0),
            review_changes_required=int(data.get("review_changes_required") or 0),
            human_interventions=int(data.get("human_interventions") or 0),
            score=Score.from_dict(score) if isinstance(score, dict) else Score(False),
            raw_output_path=data.get("raw_output_path"), error=str(data.get("error") or ""),
            served_model=data.get("served_model"),
        )


@dataclasses.dataclass(frozen=True)
class Estimate:
    """What a set of runs is expected to cost: `runs` (tasks times candidates), and the model requests in total, per
    provider (the quota a provider's daily cap counts), per candidate and per task."""

    runs: int
    total_requests: int
    per_provider: dict
    per_candidate: dict
    per_task: dict


@dataclasses.dataclass(frozen=True)
class RunSummary:
    """The outcome of run_eval. `spent` is False for a dry run (nothing was called and nothing was written). `status`
    is 'dry_run', 'complete', 'stopped' (the quota ran out part way: see `warnings`) or 'interrupted' (the run was
    aborted, the summary file says so). `refusals` are the reasons the run could not go ahead (a task that cannot
    run standalone, a quota the provider cannot afford): with spend=True they raise instead, so a non-empty tuple here
    only ever comes from a dry run."""

    run_id: str | None
    spent: bool
    status: str
    planned: tuple
    estimate: Estimate
    refusals: tuple
    records: tuple
    run_dir: pathlib.Path | None
    warnings: tuple = ()


@dataclasses.dataclass(frozen=True)
class Regression:
    """One way a candidate run is worse than the pinned run, naming the task, the metric and both values so the
    reader sees exactly what got worse."""

    task: str
    metric: str
    pinned_value: float | str
    candidate_value: float | str
    detail: str = ""


@dataclasses.dataclass(frozen=True)
class RoleValue:
    """E11 as plain numbers (blueprint D.1: 'success rate and requests per merged task, with and without the role').
    Only the tasks both runs contain are compared, so the two sides answer the same questions. `requests_per_merged`
    is model requests divided by the runs that succeeded (a run that succeeded is a task that merged, or its
    one-shot equivalent); None when nothing succeeded. The deltas are with minus without."""

    tasks_compared: tuple
    runs_with: int
    runs_without: int
    success_rate_with: float | None
    success_rate_without: float | None
    requests_per_merged_with: float | None
    requests_per_merged_without: float | None
    success_rate_delta: float | None
    requests_per_merged_delta: float | None


# =====================================================================================================================
# Candidates
# =====================================================================================================================

_ROLE_SUFFIX = re.compile(r"_(?:candidate|unfunded)$")


def _model_row(models_config: dict, label: str) -> dict | None:
    rows = [m for m in models_config.get("models", []) if f"{m.get('provider')}/{m.get('model')}" == label]
    if len(rows) > 1:
        raise EvalError(f"config/models.yaml declares {ascii_safe(label)} more than once")
    return rows[0] if rows else None


def candidate_from_config(
    models_config: dict, label: str, *, roles: dict | None = None, profile: str | None = None,
) -> Candidate:
    """The Candidate for a `provider/model` label declared in config/models.yaml. An unknown label is an EvalError that
    lists what is declared, never a guess. The Hermes profile that plays the candidate is `profile` when given, else the
    one config/swarm.yaml's roles: map gives the model's role_class (a `coder_candidate` is played by the coder profile,
    a `lead_unfunded` row by the lead profile)."""
    row = _model_row(models_config, label)
    if row is None:
        declared = ", ".join(f"{m.get('provider')}/{m.get('model')}" for m in models_config.get("models", []))
        raise EvalError(f"unknown candidate {ascii_safe(label)!r}; config/models.yaml declares: {ascii_safe(declared)}")
    role_class = row.get("role_class")
    chosen = profile
    if chosen is None:
        base = _ROLE_SUFFIX.sub("", role_class) if role_class else None
        chosen = (roles or {}).get(base) if base else None
    if not chosen:
        raise EvalError(
            f"no Hermes profile is known for {ascii_safe(label)} (role_class {role_class!r}); pass --profile NAME"
        )
    return Candidate(row["provider"], row["model"], role_class, chosen, label)


def load_candidates(
    models_config: dict, labels: Sequence[str], *, roles: dict | None = None, profile: str | None = None,
) -> list[Candidate]:
    """candidate_from_config for each label, in order, without repeats. No labels is an EvalError."""
    seen: list[str] = []
    for label in (piece.strip() for piece in labels):
        if label and label not in seen:
            seen.append(label)
    if not seen:
        raise EvalError("no candidates were named; give provider/model labels from config/models.yaml")
    return [candidate_from_config(models_config, label, roles=roles, profile=profile) for label in seen]


# =====================================================================================================================
# Cost: the estimate and the budget check
# =====================================================================================================================


def estimate(tasks: Sequence[EvalTask], candidates: Sequence[Candidate]) -> Estimate:
    """Total model requests for running every task on every candidate, per provider, per candidate and per task,
    from each task's est_requests. Evaluation spends real quota (ASES-DOC-04), so this is what a dry run prints."""
    per_task = {t.id: t.est_requests * len(candidates) for t in tasks}
    per_candidate: dict[str, int] = {}
    per_provider: dict[str, int] = {}
    each = sum(t.est_requests for t in tasks)
    for c in candidates:
        per_candidate[c.label] = per_candidate.get(c.label, 0) + each
        per_provider[c.provider] = per_provider.get(c.provider, 0) + each
    return Estimate(
        runs=len(tasks) * len(candidates), total_requests=sum(per_provider.values()), per_provider=per_provider,
        per_candidate=per_candidate, per_task=per_task,
    )


def calendar_minutes(est: Estimate, candidates: Sequence[Candidate], providers: dict) -> dict:
    """{provider: minutes} the estimated requests take at each provider's declared rate limit (blueprint 5.4: the plan
    shows the expected calendar time). Pacing only, never a budget decision. None for a provider that declares no rate
    limit. A per-model limit paces each model on its own, so the slowest model decides; an account-wide limit is shared
    by every model, so the provider's total decides. Uses policy.estimate_calendar_minutes for both."""
    result: dict[str, float | None] = {}
    for provider, total in est.per_provider.items():
        values = [
            policy.estimate_calendar_minutes(
                providers, provider, requests_for_model=est.per_candidate[c.label], requests_for_provider=total,
            )
            for c in candidates if c.provider == provider
        ]
        known = [v for v in values if v is not None]
        result[provider] = max(known) if known else None
    return result


def check_budget(
    conn: sqlite3.Connection, models_config: dict, est: Estimate, *, budgets: dict | None = None,
) -> list[str]:
    """ASES-CAP-03: "No card becomes ready without budget for it plus a review reserve; otherwise it is parked". An
    evaluation is held to the same rule: it is refused, before a single call, when a provider's remaining quota today
    (read from the request ledger through policy.check_budget, so the daily reserve of `budgets` is kept back too)
    cannot cover the requests estimated for it. One message per provider that cannot afford its share; an empty list
    means every provider can. A provider with no known daily cap always passes."""
    problems = []
    providers = models_config.get("providers", {})
    for provider, requests in sorted(est.per_provider.items()):
        if requests <= 0:
            continue
        verdict = policy.check_budget(conn, providers, provider, requests, budgets=dict(budgets or {}))
        if not verdict.can_afford:
            problems.append(f"provider {provider}: {verdict.reason}")
    return problems


# =====================================================================================================================
# Calling a model: default_invoke
# =====================================================================================================================


@dataclasses.dataclass(frozen=True)
class _Process:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    started: bool


def _kill_process_tree(pid: int) -> None:
    """Stop a process and everything it started. On Windows `os.kill` does NOT probe a process, it terminates it, so
    it is never used there: `taskkill /PID n /T /F` ends the tree. Best effort, never raises. Every caller takes this as
    an injectable argument so a test never touches a real process.

    Round 13 (TIDY): the actual kill is now procenv.kill_process_tree, the one definition this and gates.py's own
    `_kill_process_tree` both delegate to. `process_group=False` (the default) keeps this caller's own POSIX
    behaviour exactly: `_run_process` starts the Hermes launcher in the caller's own session, not a new one, so
    its pid is not, in general, a process group leader, and `os.killpg` (gates.py's own choice, since ITS command
    runs in its own session) could raise or reach the wrong group here. See procenv.kill_process_tree's docstring
    for the full story. This thin wrapper stays so `evals._kill_process_tree` keeps working as `_run_process`'s
    injectable default and a test can still flip `evals._IS_WINDOWS` and see it take effect here."""
    procenv.kill_process_tree(pid, is_windows=_IS_WINDOWS)


def _run_process(
    argv: list[str], cwd: pathlib.Path, timeout: int, *, popen: Callable = subprocess.Popen,
    kill_tree: Callable[[int], None] = _kill_process_tree,
) -> _Process:
    """Run `argv` with UTF-8 output, no stdin and a credential-scrubbed environment (ASES-CFG-05: a provider key
    exported into the shell that runs `swarm eval` must not reach the hermes process; procenv.scrubbed_environ is
    the one definition of which names count), and never raise. A timeout stops the whole process tree (a Hermes
    launcher starts a Python child, and killing only the launcher would leave the model call running and spending
    quota) and comes back as returncode -1 with timed_out True."""
    env = dict(procenv.scrubbed_environ(), PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    try:
        proc = popen(
            argv, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", env=env,
        )
    except OSError as exc:
        return _Process(-1, "", f"hermes could not be run: {exc}", False, False)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(proc.pid)
        try:
            out, err = proc.communicate(timeout=10)
        except (subprocess.TimeoutExpired, OSError):
            out, err = "", ""
        note = f"the model call did not finish within {timeout}s and was stopped"
        return _Process(-1, out or "", ((err or "") + "\n" + note).strip(), True, True)
    except OSError as exc:
        return _Process(-1, "", f"reading hermes output failed: {exc}", False, True)
    return _Process(proc.returncode, out or "", err or "", False, True)


def _read_usage_report(path: pathlib.Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _report_number(report: dict, *path: str) -> int | None:
    node: object = report
    for step in path:
        node = node.get(step) if isinstance(node, dict) else None
    number = _int_or_none(node)
    return number if number is not None and number >= 0 else None


def default_invoke(
    candidate: Candidate, prompt: str, workdir: pathlib.Path, timeout: int, *, tools: Sequence[str] = (),
    _run: Callable[..., _Process] | None = None,
) -> InvokeResult:
    """The one function that really calls a model: `hermes -p <profile> -z <prompt> -m <model> --provider <provider>`,
    with `-t <toolsets>` when the task needs tools and `--usage-file <path>` to get the run's own accounting. Never used
    by tests (they inject a fake `invoke`); its argv and its accounting are tested through the `_run` argument.

    The flags were read from the Hermes 0.21.3 source on 2026-09-22 (hermes_cli/_parser.py, `_add_top_level_flags`, and
    hermes_cli/oneshot.py): `-z/--oneshot PROMPT` prints only the final response and needs no `chat`; `-m/--model` and
    `--provider` pair with it, and Hermes REFUSES --provider without --model (both are always passed here); `-t/--toolsets`
    takes a comma separated list, and a one-shot call grants no toolset without it, so a text task passes none and E3 passes
    `file`; `-p/--profile` is read before argument parsing. `--usage-file` is the documented way to account for a one-shot
    run: Hermes writes a JSON report (api_calls, token counts, model, session_id) even when the run fails. Exit codes are
    0 completed, 1 no answer or agent error, 2 failed or partial, 130 interrupted.

    Accounting (ASES-CAP-03: the ledger must see real usage). The count that matters is what the provider's daily quota
    counts, and that is the report's `total_including_auxiliary.api_calls`: a plain one-shot call is one main call PLUS
    Hermes's auxiliary title-generation call (the real Phase 2 usage files show api_calls 1 and a total of 2), and Hermes's
    own oneshot.py says pipelines bill on that grand total. `hermes.session_usage` counts only the main loop, so it is asked
    only as a last resort, when the report names a session but carries no counts. Tokens are the main loop's plus the
    auxiliary tasks'. A report with no counts and no session means Hermes failed before its agent ran, which cost nothing
    (0); with no report at all (the run was killed, or Hermes wrote none) a call that started counts as 1 request with
    unknown tokens, and one that never started as 0. Never raises: a missing hermes, a timeout, a prompt too long for a
    Windows command line all come back as returncode -1 with the reason in stderr."""
    run = _run or _run_process
    try:
        exe = hermes.hermes_path()
    except hermes.HermesNotFound as exc:
        return InvokeResult(-1, "", str(exc), 0.0, 0, None, None)
    usage_dir = pathlib.Path(tempfile.mkdtemp(prefix="ases-eval-usage-"))
    usage_file = usage_dir / "usage.json"
    try:
        argv = [exe, "-p", candidate.profile, "-z", prompt, "-m", candidate.model, "--provider", candidate.provider,
                "--usage-file", str(usage_file)]
        if tools:
            argv += ["-t", ",".join(tools)]
        if _IS_WINDOWS:
            length = len(subprocess.list2cmdline(argv))
            if length > _WINDOWS_CMDLINE_LIMIT:
                return InvokeResult(
                    -1, "", f"the prompt is {length} characters as a command line, over the Windows limit "
                    f"(about {_WINDOWS_CMDLINE_LIMIT})", 0.0, 0, None, None,
                )
        began = time.monotonic()
        proc = run(argv, workdir, timeout)
        latency = time.monotonic() - began
        report = _read_usage_report(usage_file)
        requests = input_tokens = output_tokens = served = session_id = None
        if report is not None:
            raw_session = report.get("session_id")
            session_id = raw_session if isinstance(raw_session, str) and raw_session else None
            served = report.get("model") if isinstance(report.get("model"), str) and report.get("model") else None
            requests = _report_number(report, "total_including_auxiliary", "api_calls")
            if requests is None:
                requests = _report_number(report, "api_calls")
            input_tokens = _add(_report_number(report, "input_tokens"), _report_number(report, "auxiliary", "input_tokens"))
            output_tokens = _add(
                _report_number(report, "output_tokens"), _report_number(report, "auxiliary", "output_tokens"))
        if requests is None and session_id and not proc.timed_out:
            session = hermes.session_usage(candidate.profile, session_id)  # last resort: the main loop only
            if session:
                requests = _int_or_none(session.get("api_call_count"))
                if input_tokens is None:
                    input_tokens = _int_or_none(session.get("input_tokens"))
                if output_tokens is None:
                    output_tokens = _int_or_none(session.get("output_tokens"))
                served = served or session.get("model") or None
        if requests is None:
            if report is not None:
                requests = 1 if session_id else 0  # a session was opened, so the model was probably called
            else:
                requests = 1 if proc.started else 0
        return InvokeResult(
            proc.returncode, proc.stdout, proc.stderr, latency, requests, input_tokens, output_tokens,
            served_model=served, session_id=session_id, timed_out=proc.timed_out,
        )
    finally:
        shutil.rmtree(usage_dir, ignore_errors=True)


# =====================================================================================================================
# The runner
# =====================================================================================================================


def _clock(now: object) -> Callable[[], float]:
    """`now` as a zero-argument callable returning epoch seconds: None means the real clock, a number or a datetime is
    a fixed moment, a callable is used as it is (a test hands one that its fake sleep advances)."""
    if now is None:
        return time.time
    if callable(now):
        return now  # type: ignore[return-value]
    if isinstance(now, datetime):
        stamp = now.timestamp()
        return lambda: stamp
    value = float(now)  # type: ignore[arg-type]
    return lambda: value


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="seconds")


def _accepts_tools(fn: Callable) -> bool:
    """True when an `invoke` callable declares a `tools` parameter (or takes **kwargs): the runner then hands it the
    task's toolsets. The contract call is invoke(candidate, prompt, workdir, timeout), so a fake with exactly those four
    parameters is called with exactly those four."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return "tools" in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _norm_model(model: str, provider: str) -> str:
    """A model id for comparison: lower case, without a leading `<provider>/` and without a trailing `:variant` tag."""
    value = model.strip().lower()
    prefix = provider.lower() + "/"
    if value.startswith(prefix):
        value = value[len(prefix):]
    return re.sub(r":[a-z0-9_-]+$", "", value)


def _fallbacks(candidate: Candidate, served_model: str | None) -> int:
    """1 when the run reports a different model than the one asked for (Hermes's fallback chain answered), else 0. A
    heuristic: it compares ids after dropping a provider prefix and a `:free` style tag, and counts nothing when the
    run reported no model."""
    if not served_model:
        return 0
    return 0 if _norm_model(served_model, candidate.provider) == _norm_model(candidate.model, candidate.provider) else 1


class _Pacer:
    """Waits between calls so a provider's declared rate limit is respected. The gap after a run is the time its
    requests need at the limit (policy.estimate_calendar_minutes), measured from when the run STARTED, so a slow run
    does not wait twice. A per-model limit paces (provider, model); an account-wide limit paces the provider."""

    def __init__(self, providers: dict, clock: Callable[[], float], sleep: Callable[[float], None]):
        self._providers = providers
        self._clock = clock
        self._sleep = sleep
        self._next_at: dict[tuple, float] = {}

    def _key(self, candidate: Candidate) -> tuple:
        limits = self._providers.get(candidate.provider, {}).get("limits", {})
        return (candidate.provider, candidate.model) if "per_model_rpm" in limits else (candidate.provider,)

    def wait(self, candidate: Candidate) -> None:
        until = self._next_at.get(self._key(candidate))
        if until is not None:
            delay = until - self._clock()
            if delay > 0:
                self._sleep(delay)

    def note(self, candidate: Candidate, started_at: float, requests: int) -> None:
        count = max(requests, 1)
        minutes = policy.estimate_calendar_minutes(
            self._providers, candidate.provider, requests_for_model=count, requests_for_provider=count,
        )
        if minutes:
            self._next_at[self._key(candidate)] = started_at + minutes * 60.0


def _preflight(tasks: Sequence[EvalTask]) -> list[str]:
    problems = []
    for task in tasks:
        why = tasks_mod.refusal_for(task)
        if why:
            problems.append(why)
    return problems


def _reserve_run_dir(out_dir: pathlib.Path, run_id: str) -> tuple[str, pathlib.Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = 1
    while True:
        name = run_id if suffix == 1 else f"{run_id}-{suffix}"
        try:
            (out_dir / name).mkdir()
        except FileExistsError:
            suffix += 1
            continue
        return name, out_dir / name


def _raw_name(task_id: str, label: str, used: set[str]) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", label).strip("._-")[:80] or "candidate"
    name = f"{task_id}-{slug}.txt"
    counter = 1
    while name in used:
        counter += 1
        name = f"{task_id}-{slug}-{counter}.txt"
    used.add(name)
    return name


def _write_json(path: pathlib.Path, obj: object) -> None:
    """A JSON file, redacted (ASES-SEC-01: secret-shaped values never reach disk) and ASCII only."""
    safe = events.redact(obj) if isinstance(obj, dict) else obj
    path.write_text(json.dumps(safe, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8", newline="\n")


def _add(left: int | None, right: int | None) -> int | None:
    if left is None and right is None:
        return None
    return (left or 0) + (right or 0)


def _rmtree(path: pathlib.Path) -> None:
    def clear(func: Callable, target: str, _exc: object) -> None:
        try:
            os.chmod(target, 0o700)
            func(target)
        except OSError:
            pass

    shutil.rmtree(path, onerror=clear)


def _raw_text(attempts: Sequence[str], stderr: str) -> str:
    """What is stored as the raw output of a run, redacted (ASES-OBS-02: raw results stay local; ASES-SEC-01)."""
    if len(attempts) > 1:
        body = "\n".join(f"=== attempt {i} of {len(attempts)} ===\n{a}" for i, a in enumerate(attempts, start=1))
    else:
        body = attempts[0] if attempts else ""
    if stderr.strip():
        body = body.rstrip("\n") + "\n\n=== stderr ===\n" + stderr
    return events.redact_text(body)


def _run_one(
    task: EvalTask, candidate: Candidate, *, invoke: Callable, workroot: pathlib.Path, run_id: str,
    raw_dir: pathlib.Path, used_names: set[str], pacer: _Pacer, conn: sqlite3.Connection | None,
    clock: Callable[[], float], warnings: list[str],
) -> RunRecord:
    """One run: fixture, prompt, model call(s), score. Nothing in here can stop the others: any exception becomes a
    RunRecord with success False and the reason. The model call is accounted in the ledger the moment it returns, before
    scoring, so a scorer that crashes cannot make quota vanish from the books."""
    started_epoch = clock()
    workdir: pathlib.Path | None = None
    error = ""
    score = Score(False)
    attempts: list[str] = []
    stderr = ""
    latency = 0.0
    requests = 0
    tokens_in: int | None = None
    tokens_out: int | None = None
    retries = 0
    served: str | None = None
    try:
        workdir = pathlib.Path(tempfile.mkdtemp(prefix=f"{task.id.lower()}-", dir=str(workroot)))
        fixture = task.build_fixture(workdir)
        prompt = task.build_prompt(fixture)
        result: InvokeResult | None = None
        while True:
            pacer.wait(candidate)
            began = clock()
            kwargs = {"tools": tuple(task.tools)} if task.tools and _accepts_tools(invoke) else {}
            result = invoke(candidate, prompt, workdir, task.timeout_seconds, **kwargs)
            pacer.note(candidate, began, result.requests)
            latency += float(result.latency_seconds or 0.0)
            requests += int(result.requests or 0)
            tokens_in = _add(tokens_in, result.input_tokens)
            tokens_out = _add(tokens_out, result.output_tokens)
            served = result.served_model or served
            attempts.append(result.stdout or "")
            stderr = result.stderr or ""
            _account(conn, candidate, int(result.requests or 0), warnings)
            if result.returncode != 0 or task.retry_prompt is None or retries >= task.max_retries:
                break
            follow_up = task.retry_prompt(fixture, result.stdout or "")
            if follow_up is None:
                break
            prompt = follow_up
            retries += 1
        merged = dataclasses.replace(
            result, latency_seconds=latency, requests=requests, input_tokens=tokens_in, output_tokens=tokens_out,
            attempts=tuple(attempts), served_model=served,
        )
        if merged.returncode != 0:
            error = f"the model call failed (exit {merged.returncode}): {clip((stderr or merged.stdout).strip(), 300)}"
            score = Score(False, notes="the model call failed, so nothing was scored")
        else:
            score = task.score(fixture, workdir, merged.stdout, merged)
    except Exception as exc:  # noqa: BLE001 - one failing run must never stop the rest
        error = f"{type(exc).__name__}: {clip(str(exc), 300)}"
        score = Score(False, notes="the run raised before it could be scored")
    finally:
        if workdir is not None:
            _rmtree(workdir)
    raw_path = None
    if attempts or stderr:
        name = _raw_name(task.id, candidate.label, used_names)
        (raw_dir / name).write_text(_raw_text(attempts, stderr), encoding="utf-8", newline="\n")
        raw_path = f"raw/{name}"
    findings = score.findings
    return RunRecord(
        task_id=task.id, candidate=candidate.label, run_id=run_id, started_at=_iso(started_epoch),
        latency_seconds=latency, requests=requests, input_tokens=tokens_in, output_tokens=tokens_out, retries=retries,
        fallbacks=_fallbacks(candidate, served),
        review_changes_required=_int_or_none(findings.get("review_changes_required")) or 0,
        human_interventions=_int_or_none(findings.get("human_interventions")) or 0, score=score,
        raw_output_path=raw_path, error=events.redact_text(ascii_safe(error)), served_model=served,
    )


def _cannot_afford(
    conn: sqlite3.Connection, models_config: dict, task: EvalTask, candidate: Candidate, budgets: dict | None,
) -> str | None:
    """Why the day's quota cannot afford ONE more run right now, or None when it can. The estimate is checked once up
    front, but it can be wrong (Hermes makes auxiliary calls, a tool loop runs long) and the swarm may be spending the same
    provider quota at the same time, so ASES-CAP-03 ("No card becomes ready without budget for it plus a review reserve")
    is applied to every run before it starts, against the ledger as it is now, and not once for the whole evaluation."""
    verdict = policy.check_budget(
        conn, models_config.get("providers", {}), candidate.provider, task.est_requests, budgets=dict(budgets or {}),
    )
    return None if verdict.can_afford else verdict.reason


def _account(conn: sqlite3.Connection | None, candidate: Candidate, requests: int, warnings: list[str]) -> None:
    """Count a finished call's requests against the provider's daily quota (ASES-CAP-03). A one-shot evaluation call is
    not a Kanban card, so usage.py never sees it: the harness records it itself."""
    if conn is None or requests <= 0:
        return
    try:
        ledger.record_usage(conn, candidate.provider, candidate.model, requests)
    except sqlite3.Error as exc:
        warnings.append(f"could not record {requests} request(s) for {candidate.label} in the ledger: {exc}")


def _audit(conn: sqlite3.Connection | None, record: RunRecord, warnings: list[str]) -> None:
    if conn is None:
        return
    try:
        # No project here: a one-shot evaluation run is not tied to any ASES project (events.py package, round 9's
        # own example of a genuinely cross-project event).
        events.record(conn, "eval_run", {
            "run_id": record.run_id, "task": record.task_id, "candidate": record.candidate,
            "requests": record.requests, "success": record.score.success,
        })
    except sqlite3.Error as exc:
        warnings.append(f"could not record the eval_run event: {exc}")


def run_eval(
    tasks: Sequence[EvalTask], candidates: Sequence[Candidate], *, invoke: Callable, workroot: pathlib.Path,
    out_dir: pathlib.Path, models_config: dict | None = None, conn: sqlite3.Connection | None = None,
    spend: bool = False, now: object = None, sleep: Callable[[float], None] = time.sleep, budgets: dict | None = None,
) -> RunSummary:
    """Run every task on every candidate, or with spend=False (the default) just say what WOULD run and what it would
    cost, calling nothing and writing nothing.

    ASES-DOC-04 (the stop condition): evaluation spends real quota, so a call is made only when the caller passed
    spend=True; the CLI's --spend-quota is that flag. ASES-CAP-03: with `models_config` and `conn` the estimate is checked
    against the day's remaining quota first, and a run the quota cannot afford is refused before it starts (EvalRefused
    when spending; listed in RunSummary.refusals on a dry run). A task that cannot run standalone (E8, E11) is refused the
    same way, with the instruction for running it properly.

    With spend=True each (task, candidate) gets its own fresh temp directory under `workroot`, `invoke(candidate, prompt,
    workdir, timeout)` is called (once more for a task that allows a corrected retry), the answer is scored, and the
    results go under `out_dir/<run id>/`: one RunRecord per line in results.jsonl, the redacted raw output in
    raw/<task>-<candidate>.txt (ASES-OBS-02: raw results stay local; ASES-SEC-01: secrets are redacted before anything
    is written), and summary.json. Every RunRecord is appended and flushed as soon as its run ends, so an interrupted
    evaluation keeps what it finished. One failing run never stops the rest. Requests are recorded in the ledger when
    `conn` is given, and with `models_config` too every run re-checks the quota before it starts (see _cannot_afford): when
    it can no longer be afforded the evaluation stops, status 'stopped', keeping what finished (a run that was never
    started is a missing result, not a failed one). Between calls the provider's declared rate limit is respected through
    `sleep`."""
    task_list, candidate_list = list(tasks), list(candidates)
    est = estimate(task_list, candidate_list)
    planned = tuple((t.id, c.label) for t in task_list for c in candidate_list)
    problems = _preflight(task_list)
    if conn is not None and models_config is not None:
        problems += check_budget(conn, models_config, est, budgets=budgets)
    if not spend:
        return RunSummary(None, False, "dry_run", planned, est, tuple(problems), (), None)
    if problems:
        raise EvalRefused("; ".join(problems))
    clock = _clock(now)
    workroot = pathlib.Path(workroot)
    workroot.mkdir(parents=True, exist_ok=True)
    run_id, run_dir = _reserve_run_dir(
        pathlib.Path(out_dir), "eval-" + datetime.fromtimestamp(clock(), tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
    )
    raw_dir = run_dir / "raw"
    raw_dir.mkdir()
    results_path = run_dir / "results.jsonl"
    results_path.write_text("", encoding="utf-8")
    pacer = _Pacer((models_config or {}).get("providers", {}), clock, sleep)
    records: list[RunRecord] = []
    warnings: list[str] = []
    used_names: set[str] = set()
    started = clock()
    status = "interrupted"
    gated = conn is not None and models_config is not None
    try:
        halted = ""
        for task in task_list:
            for candidate in candidate_list:
                if gated and task.est_requests > 0:
                    why = _cannot_afford(conn, models_config, task, candidate, budgets)
                    if why:
                        halted = f"stopped before {task.id} on {candidate.label}: {why}; no later run was started"
                        break
                record = _run_one(
                    task, candidate, invoke=invoke, workroot=workroot, run_id=run_id, raw_dir=raw_dir,
                    used_names=used_names, pacer=pacer, conn=conn, clock=clock, warnings=warnings,
                )
                line = json.dumps(events.redact(record.to_dict()), sort_keys=True, ensure_ascii=True)
                with open(results_path, "a", encoding="utf-8", newline="\n") as handle:
                    handle.write(line + "\n")
                    handle.flush()
                records.append(record)
                _audit(conn, record, warnings)
            if halted:
                break
        if halted:
            warnings.append(halted)
        status = "stopped" if halted else "complete"
    finally:
        _write_json(run_dir / "summary.json", _summary_dict(
            run_id, status, started, clock(), task_list, candidate_list, est, records, warnings,
        ))
    return RunSummary(run_id, True, status, planned, est, (), tuple(records), run_dir, tuple(warnings))


def _summary_dict(
    run_id: str, status: str, started: float, finished: float, tasks: Sequence[EvalTask],
    candidates: Sequence[Candidate], est: Estimate, records: Sequence[RunRecord], warnings: Sequence[str],
) -> dict:
    return {
        "run_id": run_id, "status": status, "spend": True, "started_at": _iso(started), "finished_at": _iso(finished),
        "tasks": [t.id for t in tasks],
        "candidates": [
            {"label": c.label, "provider": c.provider, "model": c.model, "profile": c.profile, "role_class": c.role_class}
            for c in candidates
        ],
        "estimate": {
            "runs": est.runs, "total_requests": est.total_requests, "per_provider": est.per_provider,
            "per_candidate": est.per_candidate, "per_task": est.per_task,
        },
        "results": [
            {"task": r.task_id, "candidate": r.candidate, "success": r.score.success, "requests": r.requests,
             "latency_seconds": round(r.latency_seconds, 3), "error": r.error}
            for r in records
        ],
        "warnings": list(warnings), "results_file": "results.jsonl", "note": _NO_COMBINED_SCORE,
    }


# =====================================================================================================================
# Reading runs back, and the report
# =====================================================================================================================


def load_run(directory: str | os.PathLike) -> list[RunRecord]:
    """The RunRecords of a run directory, from its results.jsonl, in the order they were written. A directory with no
    results.jsonl, or a line that is not a run record, is an EvalError that says which, not a traceback."""
    path = pathlib.Path(directory) / "results.jsonl"
    if not path.is_file():
        raise EvalError(f"no results.jsonl in {ascii_safe(directory)}: is it an evaluation run directory?")
    records = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except ValueError as exc:
            raise EvalError(f"results.jsonl line {number} is not valid JSON: {ascii_safe(exc)}") from exc
        try:
            records.append(RunRecord.from_dict(data))
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise EvalError(f"results.jsonl line {number} is not a run record: {ascii_safe(repr(exc))}") from exc
    return records


def _task_order(task_id: str) -> tuple[int, str]:
    match = re.fullmatch(r"E(\d+)", task_id)
    return (int(match.group(1)) if match else 10_000, task_id)


def _task_heading(task_id: str) -> str:
    task = TASKS.get(task_id)
    return f"{task_id} {task.title}" if task else task_id


def _cell(text: object) -> str:
    return ascii_safe(str(text).replace("|", "/").replace("\r", " ").replace("\n", " ")).strip()


def _table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> list[str]:
    lines = ["| " + " | ".join(_cell(h) for h in headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
    return lines


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _outcome_cell(records: Sequence[RunRecord]) -> str:
    if len(records) == 1:
        record = records[0]
        text = "pass" if record.score.success else "FAIL"
        if record.score.tests_total:
            text += f" ({record.score.tests_passed}/{record.score.tests_total} tests)"
        return text
    wins = sum(1 for r in records if r.score.success)
    passed = [r.score.tests_passed for r in records if r.score.tests_total]
    total = [r.score.tests_total for r in records if r.score.tests_total]
    text = f"{wins}/{len(records)} pass"
    if total:
        text += f" ({sum(p or 0 for p in passed)}/{sum(t or 0 for t in total)} tests)"
    return text


def _cost_cell(records: Sequence[RunRecord]) -> str:
    known_in = [r.input_tokens for r in records if r.input_tokens is not None]
    known_out = [r.output_tokens for r in records if r.output_tokens is not None]
    tokens_in = str(sum(known_in)) if known_in else "n/a"
    tokens_out = str(sum(known_out)) if known_out else "n/a"
    return f"{sum(r.requests for r in records)} req, {tokens_in} in, {tokens_out} out"


def _latency_cell(records: Sequence[RunRecord]) -> str:
    return f"{_mean([r.latency_seconds for r in records]):.1f}s"


def _reliability_cell(records: Sequence[RunRecord]) -> str:
    return (
        f"{sum(r.retries for r in records)} retries, {sum(r.fallbacks for r in records)} fallbacks, "
        f"{sum(r.review_changes_required for r in records)} changes, {sum(r.human_interventions for r in records)} human"
    )


_FAMILIES: tuple[tuple[str, Callable[[Sequence[RunRecord]], str]], ...] = (
    ("Outcome: task success and tests passed", _outcome_cell),
    ("Cost: model requests and tokens", _cost_cell),
    ("Time: latency per run", _latency_cell),
    ("Reliability: retries, fallbacks, review changes required, human interventions", _reliability_cell),
)


def render_report(records: Sequence[RunRecord]) -> str:
    """The evaluation report as Markdown, ASCII only (it is printed in a Windows console): one table per metric family
    (outcome, cost, time, reliability), each with a row per task and a column per candidate, then the findings of every
    run (why it succeeded or failed) and the errors. Appendix D.2: "Do not collapse these into a single magic score.
    Keep the individual measurements so you can see why a model succeeds or fails." Nothing here adds measurements up or
    ranks candidates."""
    if not records:
        return "# Evaluation report\n\nNo runs were recorded.\n"
    task_ids = sorted({r.task_id for r in records}, key=_task_order)
    labels = sorted({r.candidate for r in records})
    by_cell: dict[tuple[str, str], list[RunRecord]] = {}
    for record in records:
        by_cell.setdefault((record.task_id, record.candidate), []).append(record)
    lines = [
        "# Evaluation report", "",
        f"Run: {', '.join(sorted({r.run_id for r in records if r.run_id})) or 'unknown'}",
        f"Runs: {len(records)}; tasks: {len(task_ids)}; candidates: {len(labels)}", "", _NO_COMBINED_SCORE, "",
    ]
    for title, cell in _FAMILIES:
        lines += [f"## {title}", ""]
        rows = [
            [_task_heading(tid)] + [cell(by_cell[(tid, lab)]) if (tid, lab) in by_cell else "-" for lab in labels]
            for tid in task_ids
        ]
        lines += _table(["Task", *labels], rows) + [""]
    lines += ["## Findings: why a run succeeded or failed", ""]
    for record in sorted(records, key=lambda r: (_task_order(r.task_id), r.candidate)):
        facts = ", ".join(f"{k}={v}" for k, v in sorted(record.score.findings.items()))
        verdict = "pass" if record.score.success else "FAIL"
        lines.append(f"- {record.task_id} {record.candidate}: {verdict}" + (f" ({facts})" if facts else ""))
        if record.score.notes:
            lines.append(f"  note: {record.score.notes}")
        if record.error:
            lines.append(f"  error: {record.error}")
    return ascii_safe("\n".join(lines)) + "\n"


# =====================================================================================================================
# The regression check, the role comparison, the recommendations
# =====================================================================================================================


def _group_by_task(records: Sequence[RunRecord]) -> dict[str, list[RunRecord]]:
    grouped: dict[str, list[RunRecord]] = {}
    for record in records:
        grouped.setdefault(record.task_id, []).append(record)
    return grouped


def _rate(records: Sequence[RunRecord]) -> float:
    return sum(1 for r in records if r.score.success) / len(records)


def compare(
    candidate_run: Sequence[RunRecord], pinned_run: Sequence[RunRecord], *, tolerance: float = DEFAULT_TOLERANCE,
) -> list[Regression]:
    """The regression check before a pinned model changes (Phase 7: "regression check before changing a pinned model";
    section 23: "A regression benchmark before changing any pinned model"). For every task the pinned run has: the
    candidate must have run it (a missing task is a regression, because an incomplete run proves nothing); its success
    rate must not be lower than the pinned model's; and its mean requests and mean latency may not exceed the pinned
    model's by more than `tolerance` (0.25 means 25 percent). Each Regression names the task, the metric, and both values.
    Better results are never flagged. An empty list means no regression."""
    if tolerance < 0:
        raise EvalError("the tolerance must not be negative")
    candidate_by_task = _group_by_task(candidate_run)
    found: list[Regression] = []
    for task_id, pinned_records in sorted(_group_by_task(pinned_run).items(), key=lambda kv: _task_order(kv[0])):
        candidate_records = candidate_by_task.get(task_id)
        if not candidate_records:
            found.append(Regression(task_id, "coverage", "run", "missing", "the candidate run has no result for this task"))
            continue
        pinned_rate, candidate_rate = _rate(pinned_records), _rate(candidate_records)
        if candidate_rate < pinned_rate:
            found.append(Regression(task_id, "success", round(pinned_rate, 3), round(candidate_rate, 3),
                                    "the candidate succeeds less often than the pinned model"))
        for metric, pick in (("requests", lambda r: float(r.requests)), ("latency_seconds", lambda r: float(r.latency_seconds))):
            pinned_mean = _mean([pick(r) for r in pinned_records])
            candidate_mean = _mean([pick(r) for r in candidate_records])
            if candidate_mean > pinned_mean * (1 + tolerance) + 1e-9:
                found.append(Regression(task_id, metric, round(pinned_mean, 3), round(candidate_mean, 3),
                                        f"more than {tolerance:.0%} above the pinned model"))
    return found


def role_value(run_with: Sequence[RunRecord], run_without: Sequence[RunRecord]) -> RoleValue:
    """E11 (blueprint D.1: "Does a separate role profile beat the core roster on the same tasks?", evidence "Success rate
    and requests per merged task, with and without the role"), as plain numbers over the tasks both runs contain. This
    computes no verdict: the reader decides whether the difference is worth the role."""
    with_by_task, without_by_task = _group_by_task(run_with), _group_by_task(run_without)
    shared = sorted(set(with_by_task) & set(without_by_task), key=_task_order)
    with_records = [r for tid in shared for r in with_by_task[tid]]
    without_records = [r for tid in shared for r in without_by_task[tid]]

    def rate(records: list[RunRecord]) -> float | None:
        return _rate(records) if records else None

    def per_merged(records: list[RunRecord]) -> float | None:
        wins = sum(1 for r in records if r.score.success)
        return sum(r.requests for r in records) / wins if wins else None

    def delta(a: float | None, b: float | None) -> float | None:
        return None if a is None or b is None else a - b

    rate_with, rate_without = rate(with_records), rate(without_records)
    cost_with, cost_without = per_merged(with_records), per_merged(without_records)
    return RoleValue(
        tasks_compared=tuple(shared), runs_with=len(with_records), runs_without=len(without_records),
        success_rate_with=rate_with, success_rate_without=rate_without,
        requests_per_merged_with=cost_with, requests_per_merged_without=cost_without,
        success_rate_delta=delta(rate_with, rate_without), requests_per_merged_delta=delta(cost_with, cost_without),
    )


# Which tasks say something about which role class (blueprint 5.1): the Lead plans and specifies, the Reviewer reviews and
# looks for vulnerabilities, the Debugger finds root causes, and a worker writes, changes and tests code and uses tools.
ROLE_TASKS: dict[str, tuple[str, ...]] = {
    "lead": ("E1", "E2"),
    "reviewer": ("E10", "E7"),
    "debugger": ("E4", "E3"),
    "coder": ("E4", "E5", "E6", "E9"),
}
PROTECTED_ROLES = frozenset({"lead", "reviewer", "debugger"})
_ROUTER_SUFFIXES = ("free", "auto")


def is_dynamic_router(models_config: dict | None, label: str) -> bool:
    """True for a model that routes to whatever is available (a dynamic free router such as `openrouter/free`): a row
    marked `router: true` in config/models.yaml, or an id whose last path segment is `free` or `auto`. Such a model is
    fine for a worker and never for the Lead, Reviewer or Debugger (ASES-MOD-06)."""
    row = _model_row(models_config or {}, label) if models_config else None
    if row is not None and row.get("router") is True:
        return True
    model = (row["model"] if row is not None else label.split("/", 1)[-1]).lower()
    return model.rsplit("/", 1)[-1] in _ROUTER_SUFFIXES


def recommend(records: Sequence[RunRecord], models_config: dict | None) -> list[str]:
    """Plain-English recommendations from a run: for each role class, which candidate to consider given its success and
    its cost, using only candidates that ran every task the role is judged on. Never edits any configuration.

    ASES-MOD-06: "Dynamic free routing is not used for Lead, Reviewer or Debugger; worker routers require post-route
    capability validation". A dynamic router is therefore never recommended for the lead, reviewer or debugger role, and
    the line says why it was passed over. Success and cost are reported side by side (Appendix D.2), never as one score.
    The last line is always the sentence that pinning a model is the user's decision."""
    by_candidate: dict[str, list[RunRecord]] = {}
    for record in records:
        by_candidate.setdefault(record.candidate, []).append(record)
    lines: list[str] = []
    for role, wanted in ROLE_TASKS.items():
        if not any(r.task_id in wanted for r in records):
            continue
        rows = []
        for label, recs in sorted(by_candidate.items()):
            relevant = [r for r in recs if r.task_id in wanted]
            if {r.task_id for r in relevant} != set(wanted):
                continue
            wins = sum(1 for r in relevant if r.score.success)
            per_success = sum(r.requests for r in relevant) / wins if wins else None
            rows.append((label, wins, len(relevant), per_success))
        heading = f"{role} role (judged on {', '.join(wanted)})"
        if not rows:
            lines.append(f"{heading}: no candidate ran all of those tasks, so nothing can be recommended.")
            continue
        rows.sort(key=lambda row: (-row[1] / row[2], row[3] if row[3] is not None else float("inf"), row[0]))
        passed_over = []
        eligible = []
        for row in rows:
            if role in PROTECTED_ROLES and is_dynamic_router(models_config, row[0]):
                passed_over.append(row[0])
            else:
                eligible.append(row)
        for label in passed_over:
            lines.append(f"{heading}: {label} is a dynamic router and is not eligible (ASES-MOD-06).")
        best = eligible[0] if eligible else None
        if best is None or best[1] == 0:
            lines.append(f"{heading}: no eligible candidate succeeded on those tasks, so nothing can be recommended.")
            continue
        label, wins, runs, per_success = best
        cost = f"{per_success:.1f} requests per success" if per_success is not None else "no cost figure"
        text = f"{heading}: consider {label} ({wins} of {runs} runs succeeded, {cost})."
        others = [f"{r[0]} {r[1]} of {r[2]}" for r in eligible[1:]]
        if others:
            text += " Others: " + "; ".join(others) + "."
        if runs < 5:
            text += f" That is only {runs} run(s), which cannot rank models: repeat the evaluation before acting on it."
        lines.append(text)
    if not lines:
        lines.append("There are no runs for any role class to recommend from.")
    lines.append(PINNING_SENTENCE)
    return [ascii_safe(line) for line in lines]


# =====================================================================================================================
# The command line: swarm eval
# =====================================================================================================================


class _UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    """argparse exits with status 2 on a usage error, but exit code 2 here means 'regression found' (compare), so a usage
    error is raised instead and main turns it into exit code 1."""

    def error(self, message: str):  # noqa: D102
        raise _UsageError(message)


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="swarm eval",
        description="Model evaluation harness (blueprint Appendix D). Evaluation spends real provider quota: `run` is a "
        "dry run unless --spend-quota is given.",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("list", help="the tasks E1 to E11 and what one run of each costs")
    run = sub.add_parser("run", help="run tasks against candidates (a dry run without --spend-quota)")
    run.add_argument("--tasks", required=True, help="comma separated task ids, such as E1,E9,E10, or 'all'")
    run.add_argument("--candidates", required=True,
                     help="comma separated provider/model labels from config/models.yaml")
    run.add_argument("--spend-quota", dest="spend_quota", action="store_true",
                     help="really call the models (spends real provider quota)")
    run.add_argument("--out", default=None, help="where to keep the results (default: <ases_home>/evals)")
    run.add_argument("--profile", default=None,
                     help="play every candidate with this Hermes profile instead of the one its role maps to")
    report = sub.add_parser("report", help="print the report of a finished run directory")
    report.add_argument("run_dir")
    cmp = sub.add_parser("compare", help="regression check of a candidate run against a pinned run (exit 2 on regression)")
    cmp.add_argument("candidate_run")
    cmp.add_argument("pinned_run")
    cmp.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE,
                     help="how far requests or latency may grow, as a fraction (default 0.25)")
    cmp.add_argument("--candidate", default=None, help="the candidate label, when its run holds several candidates")
    cmp.add_argument("--pinned", default=None, help="the pinned label, when its run holds several candidates")
    role = sub.add_parser("role-value", help="E11: success rate and requests per merged task with and without a role")
    role.add_argument("with_run")
    role.add_argument("without_run")
    return parser


def _say(text: str = "") -> None:
    print(ascii_safe(text))


def _repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[2]


def _load_models(models_path: pathlib.Path | None) -> dict:
    """config/models.yaml through the real config loader. Imported here because config pulls in the sandbox module,
    which listing the tasks does not need. Any failure is an EvalError, never a traceback."""
    from . import config as ases_config

    try:
        return ases_config.load_models_config(models_path or _repo_root() / "config" / "models.yaml")
    except Exception as exc:  # noqa: BLE001 - ConfigError, a YAML error, an unreadable file: all one message
        raise EvalError(f"cannot read config/models.yaml: {ascii_safe(exc)}") from exc


def _load_project(swarm_path: pathlib.Path | None):
    """config/swarm.yaml (the roles map, the budgets, where the ledger lives) through the real config loader."""
    from . import config as ases_config

    try:
        return ases_config.load_swarm_config(swarm_path or _repo_root() / "config" / "swarm.yaml")
    except Exception as exc:  # noqa: BLE001
        raise EvalError(f"cannot read config/swarm.yaml: {ascii_safe(exc)}") from exc


# What a person should know before approving a real run. E4, E5 and E6 EXECUTE what the model wrote (a patch, a test file)
# in a temp copy with credential-shaped variables stripped from the environment, but with the user's own rights; E3 hands
# the model Hermes's `file` toolset, which can write as well as read, inside a temp repository.
_CODE_TASKS = ("E3", "E4", "E5", "E6")
_CODE_NOTE = (
    "E4, E5 and E6 run code a model wrote, in a temp copy with credential-shaped environment variables removed but with "
    "your own user rights; E3 gives the model Hermes's file tools (which can write) inside a temp repository."
)


def _format_list() -> str:
    lines = ["Evaluation tasks (blueprint Appendix D.1)", "", f"{'ID':<5}{'Kind':<12}{'Requests':<10}What it tests"]
    for task in TASKS.values():
        lines.append(f"{task.id:<5}{task.kind:<12}{task.est_requests:<10}{task.title}: {task.what}")
    lines += [
        "",
        "Requests are what one run costs against the provider's daily quota. E8 needs the whole swarm and E11 compares two "
        "finished runs, so neither runs standalone (`swarm eval run` says how).",
        "Phase 2 of the roadmap runs only " + ", ".join(tasks_mod.PHASE2_IDS)
        + " on a short list; the full set waits for Phase 7.",
        "Evaluation spends real quota (ASES-DOC-04): `swarm eval run` is a dry run unless --spend-quota is given.",
        _CODE_NOTE,
    ]
    return "\n".join(lines)


def _format_plan(
    summary: RunSummary, tasks: Sequence[EvalTask], candidates: Sequence[Candidate], models_config: dict,
    budget_checked: bool,
) -> str:
    est = summary.estimate
    minutes = calendar_minutes(est, candidates, models_config.get("providers", {}))
    lines = [
        "Evaluation plan (a dry run: nothing was called and no quota was spent)",
        "  tasks:      " + ", ".join(f"{t.id} {t.title}" for t in tasks),
        "  candidates: " + "; ".join(f"{c.label} (profile {c.profile})" for c in candidates),
        f"  runs:       {est.runs}",
        f"  estimated requests: {est.total_requests} in total",
    ]
    for provider, count in sorted(est.per_provider.items()):
        wait = minutes.get(provider)
        pace = f", about {wait:.1f} minutes at its declared rate limit" if wait else ""
        lines.append(f"    provider {provider}: {count}{pace}")
    if summary.refusals:
        lines.append("  refused:")
        lines += [f"    - {reason}" for reason in summary.refusals]
    elif budget_checked:
        lines.append("  budget:     every provider can afford its share today")
    else:
        lines.append("  budget:     not checked (no request ledger was available)")
    if any(t.id in _CODE_TASKS for t in tasks):
        lines.append("  note:       " + _CODE_NOTE)
    if not summary.refusals:
        lines.append("To spend this quota, run the same command again with --spend-quota.")
    return "\n".join(lines)


def _format_run(summary: RunSummary) -> str:
    lines = [f"Run {summary.run_id} finished ({summary.status}): {len(summary.records)} runs, results in {summary.run_dir}"]
    for record in summary.records:
        verdict = "PASS" if record.score.success else "FAIL"
        tail = f"  {record.error}" if record.error else ""
        lines.append(
            f"  {record.task_id:<4}{verdict}  {record.candidate}  {record.requests} req  {record.latency_seconds:.1f}s{tail}"
        )
    lines += [f"  warning: {w}" for w in summary.warnings]
    lines.append("No combined score is computed; see `swarm eval report " + str(summary.run_dir) + "` for the tables.")
    return "\n".join(lines)


def _format_compare(regressions: Sequence[Regression], tolerance: float) -> str:
    if not regressions:
        return f"No regression: the candidate is at least as good on every task within {tolerance:.0%} on cost and time."
    lines = [f"{len(regressions)} regression(s) against the pinned model (tolerance {tolerance:.0%}):"]
    for r in regressions:
        lines.append(f"  {r.task}: {r.metric}: pinned {r.pinned_value}, candidate {r.candidate_value} ({r.detail})")
    return "\n".join(lines)


def _format_role_value(value: RoleValue) -> str:
    def number(x: float | None, places: int = 2) -> str:
        return "n/a" if x is None else f"{x:.{places}f}"

    lines = [
        f"Role value over {len(value.tasks_compared)} shared task(s): {', '.join(value.tasks_compared) or 'none'}",
        f"  with the role:    {value.runs_with} runs, success rate {number(value.success_rate_with)}, "
        f"requests per merged task {number(value.requests_per_merged_with)}",
        f"  without the role: {value.runs_without} runs, success rate {number(value.success_rate_without)}, "
        f"requests per merged task {number(value.requests_per_merged_without)}",
        f"  difference (with minus without): success rate {number(value.success_rate_delta)}, "
        f"requests per merged task {number(value.requests_per_merged_delta)}",
        "These are plain numbers: whether the role is worth it is your decision.",
    ]
    return "\n".join(lines)


def _filter_label(records: list[RunRecord], label: str | None, which: str) -> list[RunRecord]:
    labels = sorted({r.candidate for r in records})
    if label is not None:
        if label not in labels:
            raise EvalError(f"the {which} run has no candidate {ascii_safe(label)!r}; it holds: {ascii_safe(', '.join(labels))}")
        return [r for r in records if r.candidate == label]
    if len(labels) > 1:
        raise EvalError(
            f"the {which} run holds {len(labels)} candidates ({ascii_safe(', '.join(labels))}); choose one with --{which}"
        )
    return records


def _cmd_run(args: argparse.Namespace, ctx: dict) -> int:
    task_list = tasks_mod.parse_task_ids(args.tasks)
    models_config = _load_models(ctx["models_path"])
    project = _load_project(ctx["swarm_path"])
    candidates = load_candidates(
        models_config, [p for p in args.candidates.split(",")], roles=project.roles, profile=args.profile,
    )
    conn = None
    db_path = ctx["db_path"]
    try:
        from . import config as ases_config
        from . import db as ases_db

        conn = ases_db.connect(db_path or ases_config.db_path(project))
    except (sqlite3.Error, OSError) as exc:
        if args.spend_quota:
            raise EvalError(f"cannot open the request ledger, which a real run must record into: {ascii_safe(exc)}") from exc
    out_dir = pathlib.Path(args.out) if args.out else (project.ases_home / "evals")
    workroot = ctx["workroot"] or pathlib.Path(tempfile.gettempdir()) / "ases-evals"
    invoke = ctx["invoke"] or default_invoke
    try:
        summary = run_eval(
            task_list, candidates, invoke=invoke, workroot=workroot, out_dir=out_dir, models_config=models_config,
            conn=conn, spend=args.spend_quota, budgets=project.budgets, now=ctx["now"],
            sleep=ctx["sleep"] or time.sleep,
        )
    finally:
        if conn is not None:
            conn.close()
    if not summary.spent:
        _say(_format_plan(summary, task_list, candidates, models_config, conn is not None))
        return 1 if summary.refusals else 0
    _say(_format_run(summary))
    return 1 if summary.status == "stopped" else 0  # an evaluation the quota cut short is a refusal to go on


def main(
    argv: Sequence[str], *, invoke: Callable | None = None, models_path: pathlib.Path | None = None,
    swarm_path: pathlib.Path | None = None, db_path: pathlib.Path | None = None,
    workroot: pathlib.Path | None = None, now: object = None, sleep: Callable[[float], None] | None = None,
) -> int:
    """The entry point behind `swarm eval`: subcommands list, run, report, compare and role-value. Exit codes: 0 ok, 1
    usage or refusal (an unknown task or candidate, a run the quota cannot afford, an unreadable run directory), 2 a
    regression found by `compare`. `run` is a dry run unless --spend-quota is given (ASES-DOC-04). The keyword arguments
    exist for tests: a fake `invoke` and paths for the configuration files, the ledger database and the temp area; the
    command line never sets them, so `swarm eval` always reads config/models.yaml and config/swarm.yaml and calls
    default_invoke."""
    parser = _build_parser()
    try:
        args = parser.parse_args(list(argv))
    except _UsageError as exc:
        print(f"swarm eval: {ascii_safe(exc)}", file=sys.stderr)
        print(ascii_safe(parser.format_usage()).rstrip(), file=sys.stderr)
        return 1
    except SystemExit as exc:  # -h / --help
        return exc.code if isinstance(exc.code, int) else 0
    if args.command is None:
        print(ascii_safe(parser.format_help()).rstrip(), file=sys.stderr)
        return 1
    ctx = {"invoke": invoke, "models_path": models_path, "swarm_path": swarm_path, "db_path": db_path,
           "workroot": workroot, "now": now, "sleep": sleep}
    try:
        if args.command == "list":
            _say(_format_list())
            return 0
        if args.command == "run":
            return _cmd_run(args, ctx)
        if args.command == "report":
            records = load_run(args.run_dir)
            _say(render_report(records))
            try:
                models_config = _load_models(models_path)
            except EvalError:
                models_config = None  # the recommendations still work, they just cannot spot a router by its config row
            _say("## Recommendations")
            _say()
            for line in recommend(records, models_config):
                _say("- " + line)
            return 0
        if args.command == "compare":
            candidate = _filter_label(load_run(args.candidate_run), args.candidate, "candidate")
            pinned = _filter_label(load_run(args.pinned_run), args.pinned, "pinned")
            regressions = compare(candidate, pinned, tolerance=args.tolerance)
            _say(_format_compare(regressions, args.tolerance))
            return 2 if regressions else 0
        if args.command == "role-value":
            _say(_format_role_value(role_value(load_run(args.with_run), load_run(args.without_run))))
            return 0
    except EvalError as exc:
        print(f"swarm eval: {ascii_safe(exc)}", file=sys.stderr)
        return 1
    except OSError as exc:  # an unwritable --out or temp directory, a run directory that cannot be read
        print(f"swarm eval: a file operation failed: {ascii_safe(exc)}", file=sys.stderr)
        return 1
    return 1
