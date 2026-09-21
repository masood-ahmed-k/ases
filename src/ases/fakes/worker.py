"""Scripted fake workers and personas for the acceptance rig (blueprint 22.0: "a scripted fake worker that performs chosen file edits").

A worker here is what a dispatched Hermes profile does with a card, written as data. `FakeHermes.kanban_dispatch` calls
the worker registered for the card's profile, synchronously, as `worker(fake, card, run, workspace_path)`, with the
card's REAL git worktree as the working directory, and the worker reports through the fake's `agent_*` methods, the same
things a Hermes worker's kanban tools do. Because the worker runs inside the dispatch call, one controller pass advances
every dispatched card, and a scenario is deterministic and instant.

    coder = ScriptedWorker([Write("a.py", "x = 1\\n"), Commit("add a.py"), RequestReview("added a.py")])
    fake.register_worker("coder-1", coder)

A worker may be a plain function of the same four arguments, or a factory: a callable of the card that returns a worker,
so one profile can behave differently per card (`by_task_key({"T1": ..., "T2": ...})`). A worker that contains a `Sleep`
keeps its card `running` and finishes when the fake clock reaches the wake-up time (FakeHermes.tick), which is what lets a
scenario have several cards running at once.

Personas (`good_coder`, `slow_coder`, `wrong_coder`, `tampering_coder`, `questioner`, `crasher`, `reviewer_pass`,
`reviewer_changes`, `reviewer_wrong_commit`, `touches_coder`, `sequence`) are functions that return a ready worker. The
reviewer personas write the verdict metadata in BOTH shapes review.validate_verdict accepts (the blueprint's `review_status`
and the Hermes review skill's `review_outcome`), naming the reviewed commit, because Hermes 0.21.3's request-changes
carries no metadata at all: a CHANGES_REQUIRED verdict travels in the reason text.

Everything a worker writes is ASCII, and git commits are made with a fixed identity so no repository needs one configured.
"""
from __future__ import annotations

import dataclasses
import pathlib
import re
import subprocess
from collections.abc import Callable, Iterable, Mapping

_IDENTITY = ("-c", "user.name=ASES fake worker", "-c", "user.email=fake-worker@example.invalid",
             "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false")

_TASK_KEY = re.compile(r"^\s*([^:\s][^:]*?)\s*:")
_TOUCHES_LINE = re.compile(r"^Touches:\s*(.+)$", re.MULTILINE)


def task_key(card: dict) -> str | None:
    """The plan task key a card title starts with ("T1: scaffold" and "T1: fix (round 1)" are both T1), or None."""
    match = _TASK_KEY.match((card or {}).get("title") or "")
    return match.group(1) if match else None


class WorkerContext:
    """What one worker invocation sees: the fake, its card and run (as they were when it was spawned), and its workspace.
    Steps use it to touch files and run git in the worktree. `written` is the list of paths the steps wrote or deleted, and
    `stopped` ends the script (a crash or a hang means nothing else runs)."""

    def __init__(self, fake, card: dict, run: dict, workspace) -> None:
        self.fake = fake
        self.card = card
        self.run = run
        self.workspace = pathlib.Path(workspace) if workspace is not None else None
        self.written: list[str] = []
        self.stopped = False

    @property
    def card_id(self) -> str:
        return self.card["id"]

    @property
    def run_id(self) -> int:
        return self.run["id"]

    @property
    def profile(self) -> str:
        return self.run.get("profile") or self.card.get("assignee") or "worker"

    def path(self, relative) -> pathlib.Path:
        """A path in the worktree. An absolute path is used as it is, so a worker can deliberately write OUTSIDE its worktree
        (the prompt-injection and integrity scenarios need exactly that)."""
        if self.workspace is None:
            raise RuntimeError(f"card {self.card_id} has no workspace: give it workspace='worktree' and a repo")
        return self.workspace / relative

    def git(self, *args: str, check: bool = True) -> str:
        """`git -C <worktree> <args>` with a fixed identity. Returns stripped stdout; a non-zero exit raises unless check=False."""
        if self.workspace is None:
            raise RuntimeError(f"card {self.card_id} has no workspace to run git in")
        result = subprocess.run(
            ["git", "-C", str(self.workspace), *_IDENTITY, *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60,
        )
        if check and result.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed in {self.workspace}: {(result.stderr or result.stdout).strip()}")
        return result.stdout.strip()

    def head(self) -> str:
        return self.git("rev-parse", "HEAD")

    def handoff_commit(self) -> str | None:
        """The commit the coder named in its latest review hand-off (a run whose outcome is review_requested), or None."""
        for run in reversed(self.fake.card(self.card_id)["_runs"]):
            if run.get("outcome") == "review_requested":
                metadata = run.get("metadata") or {}
                value = metadata.get("commit_sha") or metadata.get("commit")
                return value if isinstance(value, str) and value else None
        return None

    def reviewed_commit(self) -> str:
        """What a reviewer looks at: the head of the worktree it was given, else the commit the coder named."""
        if self.workspace is not None:
            return self.head()
        named = self.handoff_commit()
        if named is None:
            raise RuntimeError(f"card {self.card_id} has no worktree and no hand-off commit to review")
        return named

    def resolve(self, value):
        """Replace the placeholder string "@HEAD" (anywhere in a nested dict or list) with the worktree's HEAD sha."""
        if value == "@HEAD":
            return self.head()
        if isinstance(value, dict):
            return {key: self.resolve(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.resolve(item) for item in value]
        return value


# ---------------------------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------------------------


class Step:
    """One thing a scripted worker does. Subclasses are frozen dataclasses; run(ctx) does it."""

    def run(self, ctx: WorkerContext) -> None:
        raise NotImplementedError


@dataclasses.dataclass(frozen=True)
class Write(Step):
    """Write `text` to `path` (a file in the worktree; absolute paths are written as they are). Bytes are exact: no newline translation."""
    path: str
    text: str

    def run(self, ctx: WorkerContext) -> None:
        target = ctx.path(self.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.text.encode("utf-8"))
        ctx.written.append(self.path)


@dataclasses.dataclass(frozen=True)
class Append(Step):
    """Append `text` to `path`, creating it when it does not exist (the `|| true` tampering attempt appends to a test command)."""
    path: str
    text: str

    def run(self, ctx: WorkerContext) -> None:
        target = ctx.path(self.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "ab") as handle:
            handle.write(self.text.encode("utf-8"))
        ctx.written.append(self.path)


@dataclasses.dataclass(frozen=True)
class Modify(Step):
    """Rewrite an existing file: `transform(old text) -> new text`. FileNotFoundError when the file is not there."""
    path: str
    transform: Callable[[str], str]

    def run(self, ctx: WorkerContext) -> None:
        target = ctx.path(self.path)
        old = target.read_bytes().decode("utf-8")
        target.write_bytes(self.transform(old).encode("utf-8"))
        ctx.written.append(self.path)


@dataclasses.dataclass(frozen=True)
class Delete(Step):
    """Delete `path` from the worktree (FileNotFoundError when it is not there: a script that deletes a file that does not exist is a bug)."""
    path: str

    def run(self, ctx: WorkerContext) -> None:
        ctx.path(self.path).unlink()
        ctx.written.append(self.path)


@dataclasses.dataclass(frozen=True)
class Commit(Step):
    """`git add -A` and commit everything changed so far. Nothing to commit is an error unless allow_empty."""
    message: str = "work"
    allow_empty: bool = False

    def run(self, ctx: WorkerContext) -> None:
        ctx.git("add", "-A")
        if not ctx.git("status", "--porcelain") and not self.allow_empty:
            raise RuntimeError(f"Commit step for card {ctx.card_id}: nothing to commit (the script wrote nothing)")
        ctx.git("commit", "-q", "-m", self.message, *(["--allow-empty"] if self.allow_empty else []))


@dataclasses.dataclass(frozen=True)
class Untracked(Step):
    """Leave a file in the worktree that is never added to git: it exists for a worker's own run and is missing from a clean checkout."""
    path: str
    text: str

    def run(self, ctx: WorkerContext) -> None:
        target = ctx.path(self.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.text.encode("utf-8"))


@dataclasses.dataclass(frozen=True)
class RequestReview(Step):
    """Hand the card off for review (kanban_request_review) naming `reviewer`. With metadata None the hand-off carries
    {commit_sha: HEAD, changed_files, residual_risk}, like the coder prompt asks; metadata you give is used as it is,
    except that the string "@HEAD" anywhere in it becomes the worktree's HEAD sha."""
    summary: str = "ready for review"
    metadata: dict | None = None
    reviewer: str | None = "reviewer"

    def run(self, ctx: WorkerContext) -> None:
        if self.metadata is None:
            metadata = {
                "commit_sha": ctx.head(), "changed_files": list(dict.fromkeys(ctx.written)),
                "residual_risk": "none",
            }
        else:
            metadata = ctx.resolve(self.metadata)
        ctx.fake.agent_request_review(
            ctx.card_id, summary=self.summary, metadata=metadata, reviewer=self.reviewer, run_id=ctx.run_id)


@dataclasses.dataclass(frozen=True)
class Complete(Step):
    """kanban_complete with `result` as the summary. (A coder must NOT do this on a real card: only the reviewer completes.)"""
    result: str = "done"
    metadata: dict | None = None

    def run(self, ctx: WorkerContext) -> None:
        ctx.fake.agent_complete(
            ctx.card_id, summary=self.result, metadata=ctx.resolve(self.metadata) if self.metadata else None,
            run_id=ctx.run_id)


@dataclasses.dataclass(frozen=True)
class RequestChanges(Step):
    """A reviewer sends the card back to its implementer (kanban_request_changes)."""
    reason: str

    def run(self, ctx: WorkerContext) -> None:
        ctx.fake.agent_request_changes(ctx.card_id, self.reason, run_id=ctx.run_id)


@dataclasses.dataclass(frozen=True)
class Block(Step):
    """Ask a human something: kanban_block with a reason (kind needs_input by default)."""
    reason: str
    kind: str | None = "needs_input"

    def run(self, ctx: WorkerContext) -> None:
        ctx.fake.agent_block(ctx.card_id, self.reason, kind=self.kind, run_id=ctx.run_id)


@dataclasses.dataclass(frozen=True)
class Crash(Step):
    """The process dies here, booked at once (FakeHermes.agent_fail): outcome crashed, timed_out, spawn_failed or
    rate_limited. Nothing after this step runs."""
    error: str = "worker crashed"
    outcome: str = "crashed"
    exit_code: int | None = None

    def run(self, ctx: WorkerContext) -> None:
        ctx.fake.agent_fail(ctx.card_id, self.error, self.outcome, run_id=ctx.run_id, exit_code=self.exit_code)
        ctx.stopped = True


@dataclasses.dataclass(frozen=True)
class Timeout(Step):
    """The worker hangs for good: the process stays alive, the card stays `running`, and the run is timed out by
    FakeHermes.tick once its max_runtime has passed. Nothing after this step runs."""

    def run(self, ctx: WorkerContext) -> None:
        ctx.fake.agent_hang(ctx.card_id, run_id=ctx.run_id)
        ctx.stopped = True


@dataclasses.dataclass(frozen=True)
class Comment(Step):
    """kanban_comment on the worker's own card, signed with its profile."""
    text: str

    def run(self, ctx: WorkerContext) -> None:
        ctx.fake.agent_comment(ctx.card_id, self.text, author=ctx.profile)


@dataclasses.dataclass(frozen=True)
class Heartbeat(Step):
    """kanban_heartbeat: extend the claim, record the heartbeat."""
    note: str | None = None

    def run(self, ctx: WorkerContext) -> None:
        ctx.fake.agent_heartbeat(ctx.card_id, self.note, run_id=ctx.run_id)


@dataclasses.dataclass(frozen=True)
class Sleep(Step):
    """The worker keeps working for `seconds` of fake time: the card stays `running` and the steps after this one run when
    FakeHermes.tick moves the clock that far (unless the run's max_runtime elapses first). ScriptedWorker handles it."""
    seconds: int

    def run(self, ctx: WorkerContext) -> None:
        raise RuntimeError("Sleep is handled by ScriptedWorker, not run directly")


@dataclasses.dataclass(frozen=True)
class Do(Step):
    """Escape hatch: `fn(ctx)`, for anything the other steps do not cover."""
    fn: Callable[[WorkerContext], None]

    def run(self, ctx: WorkerContext) -> None:
        self.fn(ctx)


def _flatten(steps) -> list[Step]:
    flat: list[Step] = []
    for step in steps:
        if isinstance(step, (list, tuple)):
            flat.extend(_flatten(step))
        elif isinstance(step, Step):
            flat.append(step)
        else:
            raise TypeError(f"not a worker step: {step!r}")
    return flat


class ScriptedWorker:
    """A worker that performs `steps` in order in the card's real worktree. `steps` may nest lists (a helper can return a
    list of steps). The worker is a callable (fake, card, run, workspace_path) -> None, so it can be registered directly."""

    def __init__(self, steps: Iterable) -> None:
        self.steps = tuple(_flatten(steps))

    def __call__(self, fake, card: dict, run: dict, workspace_path) -> None:
        self._run_from(0, WorkerContext(fake, card, run, workspace_path))

    def _run_from(self, index: int, ctx: WorkerContext) -> None:
        position = index
        while position < len(self.steps):
            step = self.steps[position]
            if isinstance(step, Sleep):
                resume = position + 1
                ctx.fake.defer(ctx.card_id, ctx.run_id, step.seconds, lambda: self._run_from(resume, ctx))
                return
            step.run(ctx)
            if ctx.stopped:
                return
            position += 1


# ---------------------------------------------------------------------------------------------
# Factories and composition
# ---------------------------------------------------------------------------------------------


class WorkerFactory:
    """A callable of the card that returns the worker for it. FakeHermes tells it from a worker by `is_factory`."""

    is_factory = True

    def __init__(self, choose: Callable[[dict], object]) -> None:
        self._choose = choose

    def __call__(self, card: dict):
        return self._choose(card)


def by_task_key(workers: Mapping[str, object], default=None) -> WorkerFactory:
    """One profile, a different worker per plan task: by_task_key({"T1": coder_a, "T2": coder_b}). A fix card of T1 is
    titled "T1: fix (round N)", so it gets T1's worker too. KeyError for a task with no worker and no default."""

    def choose(card: dict):
        key = task_key(card)
        if key in workers:
            return workers[key]
        if default is not None:
            return default
        raise KeyError(f"no worker scripted for task {key!r} (card {card.get('id')}, title {card.get('title')!r})")

    return WorkerFactory(choose)


class _Sequence:
    """The n-th time a card is dispatched to this worker, run the n-th of `workers` (the last one for every later time)."""

    def __init__(self, workers) -> None:
        if not workers:
            raise ValueError("sequence() needs at least one worker")
        self.workers = list(workers)
        self._seen: dict[str, int] = {}

    def __call__(self, fake, card: dict, run: dict, workspace_path) -> None:
        index = self._seen.get(card["id"], 0)
        self._seen[card["id"]] = index + 1
        self.workers[min(index, len(self.workers) - 1)](fake, card, run, workspace_path)


def sequence(*workers) -> _Sequence:
    """A worker that behaves differently each time the same card comes back to it (wrong first, corrected second)."""
    return _Sequence(workers)


# ---------------------------------------------------------------------------------------------
# Coder personas
# ---------------------------------------------------------------------------------------------


def write_files(files: Mapping[str, str]) -> list[Step]:
    """The Write steps for `files` ({path: text}): a builder for a script that mixes them with other steps."""
    return [Write(path, text) for path, text in files.items()]


def write_and_commit(files: Mapping[str, str], message: str = "work") -> list[Step]:
    """Write `files` and commit them: the two steps almost every coder script starts with."""
    return [*write_files(files), Commit(message)]


_writes = write_files


def good_coder(
    files: Mapping[str, str], message: str = "implement the task", *,
    summary: str = "implemented the task and ran the gate commands", reviewer: str | None = "reviewer",
    metadata: dict | None = None,
) -> ScriptedWorker:
    """Write `files`, commit them, and hand off for review naming `reviewer` (the hand-off carries the commit sha)."""
    return ScriptedWorker([*_writes(files), Commit(message), RequestReview(summary, metadata, reviewer)])


def slow_coder(
    files: Mapping[str, str], message: str = "implement the task", *, seconds: int = 600,
    summary: str = "implemented the task", reviewer: str | None = "reviewer",
) -> ScriptedWorker:
    """Write `files` now, then keep working for `seconds` (the card stays `running`, uncommitted work in its worktree),
    then commit and hand off. Needs FakeHermes.tick to move the clock past the wake-up."""
    return ScriptedWorker([
        *_writes(files), Heartbeat("still working"), Sleep(seconds), Commit(message),
        RequestReview(summary, None, reviewer),
    ])


def wrong_coder(
    files: Mapping[str, str], message: str = "implement the task", *,
    summary: str = "implemented the task; every acceptance criterion is met", reviewer: str | None = "reviewer",
) -> ScriptedWorker:
    """A coder whose `files` violate an acceptance criterion while its summary claims all are met: the reviewer must
    catch it (acceptance 22.6). Same script as good_coder; the difference is in the content it is given."""
    return good_coder(files, message, summary=summary, reviewer=reviewer)


_SKIP_LINE = re.compile(r"^(\s*)((?:async\s+)?def\s+test)", re.MULTILINE)


def _add_skip_marker(text: str) -> str:
    match = _SKIP_LINE.search(text)
    if match is None:
        raise ValueError("no test function to put a skip marker on")
    return text[:match.start()] + f'{match.group(1)}@pytest.mark.skip(reason="temporarily skipped")\n' + text[match.start():]


TAMPER_KINDS = ("delete_test", "skip_marker", "or_true", "outside_paths", "untracked")


def tampering_coder(
    kind: str, *, path: str | None = None, message: str | None = None,
    summary: str = "implemented the task; the gate is green", reviewer: str | None = "reviewer",
) -> ScriptedWorker:
    """One of the five acceptance 22.12 attempts to turn a red gate green, then a hand-off for review:
      delete_test    delete a failing test file (default tests/test_feature.py; it must exist in the seeded repository)
      skip_marker    put @pytest.mark.skip before the first test in it (same default file)
      or_true        append `pytest || true` to a test command script (default scripts/test.sh)
      outside_paths  write a file outside the task's touches (default OUTSIDE_TOUCHES.md)
      untracked      commit an app.py that imports a helper, and leave helper.py untracked (it makes the build pass only in
                     this worktree: a clean checkout of the commit does not have it)
    `path` overrides the file each kind touches."""
    if kind not in TAMPER_KINDS:
        raise ValueError(f"unknown tampering kind {kind!r}, expected one of {TAMPER_KINDS}")
    commit = Commit(message or f"work ({kind})")
    if kind == "delete_test":
        steps: list[Step] = [Delete(path or "tests/test_feature.py"), commit]
    elif kind == "skip_marker":
        steps = [Modify(path or "tests/test_feature.py", _add_skip_marker), commit]
    elif kind == "or_true":
        steps = [Append(path or "scripts/test.sh", "\npytest || true\n"), commit]
    elif kind == "outside_paths":
        steps = [Write(path or "OUTSIDE_TOUCHES.md", "edited outside the allowed paths\n"), commit]
    else:
        steps = [Write("app.py", "import helper\nprint(helper.VALUE)\n"), commit,
                 Untracked(path or "helper.py", "VALUE = 1\n")]
    return ScriptedWorker([*steps, RequestReview(summary, None, reviewer)])


class _Questioner:
    def __init__(self, reason: str, then, kind: str | None) -> None:
        self.reason = reason
        self.then = then
        self.kind = kind
        self._asked: set[str] = set()

    def __call__(self, fake, card: dict, run: dict, workspace_path) -> None:
        if card["id"] not in self._asked:
            self._asked.add(card["id"])
            fake.agent_block(card["id"], self.reason, kind=self.kind, run_id=run["id"])
        elif self.then is None:
            fake.agent_complete(card["id"], summary="answered, and done", run_id=run["id"])
        else:
            self.then(fake, card, run, workspace_path)


def questioner(reason: str, *, then=None, kind: str | None = "needs_input") -> _Questioner:
    """Ask a human `reason` (kanban_block, kind needs_input) the first time a card is dispatched. Once the card is answered
    and dispatched again it runs `then` (default: complete with a note)."""
    return _Questioner(reason, then, kind)


class _Crasher:
    def __init__(self, times: int, error: str, outcome: str, then, exit_code: int | None) -> None:
        self.times = times
        self.error = error
        self.outcome = outcome
        self.then = then
        self.exit_code = exit_code
        self._seen: dict[str, int] = {}

    def __call__(self, fake, card: dict, run: dict, workspace_path) -> None:
        count = self._seen.get(card["id"], 0)
        self._seen[card["id"]] = count + 1
        if count < self.times:
            fake.agent_fail(card["id"], self.error, self.outcome, run_id=run["id"], exit_code=self.exit_code)
        elif self.then is None:
            fake.agent_complete(card["id"], summary="recovered after the crashes", run_id=run["id"])
        else:
            self.then(fake, card, run, workspace_path)


def crasher(
    times: int, *, error: str = "pid 4242 exited with code 1", outcome: str = "crashed", then=None,
    exit_code: int | None = None,
) -> _Crasher:
    """Fail the first `times` dispatches of each card (`outcome`: crashed, timed_out, spawn_failed or rate_limited, with
    `error` as the failure text), then run `then` (default: complete). With the default failure limit of 2 a card that
    crashes twice is given up on: `gave_up`, `blocked`."""
    return _Crasher(times, error, outcome, then, exit_code)


def touches_coder(
    message: str = "implement the task", *, summary: str = "implemented the task", reviewer: str | None = "reviewer",
) -> WorkerFactory:
    """A coder for any plan: it reads the `Touches:` line the controller puts in the card body and writes one file for it
    (a literal path as it is, a glob with its stars replaced by the task key, no touches at all a <key>.txt), commits and
    hands off. Handy as a default worker when a scenario does not care what the code is."""

    def choose(card: dict):
        key = task_key(card) or card["id"]
        match = _TOUCHES_LINE.search(card.get("body") or "")
        paths = [p.strip() for p in match.group(1).split(",") if p.strip()] if match else []
        files = {}
        for path in paths:
            files[re.sub(r"\*+", key, path.replace("**/", ""))] = f"# {card.get('title')}\n"
        if not files:
            files[f"{key}.txt"] = f"{card.get('title')}\n"
        return good_coder(files, message, summary=summary, reviewer=reviewer)

    return WorkerFactory(choose)


# ---------------------------------------------------------------------------------------------
# Reviewer personas
# ---------------------------------------------------------------------------------------------


def _verdict(status: str, commit: str, summary: str, *, checks=(), required=()) -> dict:
    """Review metadata in the shapes review.validate_verdict accepts. A PASS carries both outcome keys and they agree."""
    metadata = {
        "review_status": status, "commit": commit, "summary": summary, "architecture_issues": [],
        "missing_cases": [], "security_issues": [], "test_gaps": [], "gate_tampering_suspected": False,
        "required_changes": list(required), "reviewer_checks": list(checks),
    }
    if status == "PASS":
        metadata["review_outcome"] = "approved"
    return metadata


def reviewer_pass(
    *, summary: str = "PASS: the change meets the acceptance criteria.",
    checks: Iterable[str] = ("read the diff", "checked the acceptance criteria"), commit: str | None = None,
):
    """Approve: kanban_complete with a PASS verdict (blueprint and Hermes-skill shapes) naming the commit under review
    (the worktree's HEAD unless `commit` is given). Completing the card is what makes it eligible to merge."""

    def worker(fake, card: dict, run: dict, workspace_path) -> None:
        ctx = WorkerContext(fake, card, run, workspace_path)
        sha = commit or ctx.reviewed_commit()
        fake.agent_complete(
            card["id"], summary=summary, metadata=_verdict("PASS", sha, summary, checks=checks), run_id=run["id"])

    return worker


def reviewer_changes(
    required: Iterable[str], *, summary: str = "CHANGES_REQUIRED: the change does not meet the acceptance criteria.",
    commit: str | None = None,
):
    """Send the card back: kanban_request_changes with the required changes as a numbered list in the reason, plus the
    reviewed commit (Hermes keeps no metadata on a request for changes, so the verdict lives in the text)."""
    items = list(required)

    def worker(fake, card: dict, run: dict, workspace_path) -> None:
        ctx = WorkerContext(fake, card, run, workspace_path)
        sha = commit or ctx.reviewed_commit()
        lines = [summary, f"Reviewed commit: {sha}", *(f"{n}. {item}" for n, item in enumerate(items, start=1))]
        fake.agent_request_changes(card["id"], "\n".join(lines), run_id=run["id"])

    return worker


def reviewer_wrong_commit(
    *, summary: str = "PASS: the change meets the acceptance criteria.", commit: str | None = None,
):
    """A PASS that names a commit other than the one at the head of the branch (the parent of HEAD, else forty zeros): the
    approval cannot be bound to what would be merged, and the merge queue must not trust it (ASES-GIT-03)."""

    def worker(fake, card: dict, run: dict, workspace_path) -> None:
        ctx = WorkerContext(fake, card, run, workspace_path)
        sha = commit
        if sha is None:
            sha = ctx.git("rev-parse", "HEAD~1", check=False) if ctx.workspace is not None else ""
            sha = sha if re.fullmatch(r"[0-9a-f]{40}", sha or "") else "0" * 40
        fake.agent_complete(
            card["id"], summary=summary, metadata=_verdict("PASS", sha, summary, checks=("read the diff",)),
            run_id=run["id"])

    return worker
