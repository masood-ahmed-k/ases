"""The only module that talks to Hermes (section 9.1: hermes.py; ASES-ARC-04).

Everything else in ASES that needs to know what Hermes is doing goes through here. Phase 1 only needs
read-only status calls (version, doctor, gateway status) -- card creation, kanban queries and worker
dispatch are wired up in Phase 3. Text parsing is used only where Hermes has no --json output for a
given command (confirmed by running --help first, per the blueprint's own instruction to Claude Code);
where JSON exists, later phases must prefer it.
"""
from __future__ import annotations

import dataclasses
import json
import re
import shutil
import subprocess

_VERSION_RE = re.compile(r"Hermes Agent v(\d+\.\d+\.\d+)")


class HermesCommandError(Exception):
    """A `hermes` subcommand exited non-zero. Carries stdout+stderr for the caller to inspect."""

    def __init__(self, args: list[str], returncode: int, output: str):
        self.args = args
        self.returncode = returncode
        self.output = output
        super().__init__(f"hermes {' '.join(args)} exited {returncode}: {output[:500]}")


class HermesNotFound(Exception):
    pass


def hermes_path() -> str:
    path = shutil.which("hermes")
    if not path:
        raise HermesNotFound("`hermes` is not on PATH")
    return path


def _run(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        [hermes_path(), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )


def hermes_version() -> str | None:
    """Returns e.g. '0.21.3', or None if hermes isn't on PATH or the output didn't parse."""
    try:
        result = _run(["--version"], timeout=20)
    except (HermesNotFound, subprocess.TimeoutExpired, FileNotFoundError):
        return None
    match = _VERSION_RE.search(result.stdout)
    return match.group(1) if match else None


@dataclasses.dataclass(frozen=True)
class DoctorResult:
    ok: bool
    exit_code: int | None
    raw_output: str
    warning_lines: tuple[str, ...]
    error_lines: tuple[str, ...]


def run_doctor(timeout: int = 60) -> DoctorResult:
    """Shells out to `hermes doctor`. Hermes has no --json for this command (checked via --help), so we
    fall back to its own pass/fail convention: exit code 0 means healthy. Lines are also scanned for the
    warning (warn) and error (x) glyphs Hermes prints, purely as extra detail for a human reading the
    ASES doctor report -- the exit code is what decides ok, not glyph-counting.
    """
    try:
        result = _run(["doctor"], timeout=timeout)
    except HermesNotFound:
        return DoctorResult(False, None, "hermes not found on PATH", (), ("hermes not found on PATH",))
    except subprocess.TimeoutExpired:
        return DoctorResult(False, None, "hermes doctor timed out", (), ("hermes doctor timed out",))

    raw = result.stdout + result.stderr
    warnings = tuple(line.strip() for line in raw.splitlines() if line.lstrip().startswith(("⚠", "!")))
    errors = tuple(line.strip() for line in raw.splitlines() if line.lstrip().startswith(("✗", "x", "X")))
    return DoctorResult(result.returncode == 0, result.returncode, raw, warnings, errors)


@dataclasses.dataclass(frozen=True)
class GatewayStatus:
    running: bool
    raw_output: str


def gateway_status(timeout: int = 20) -> GatewayStatus:
    try:
        result = _run(["gateway", "status"], timeout=timeout)
    except (HermesNotFound, subprocess.TimeoutExpired):
        return GatewayStatus(False, "hermes gateway status unavailable")
    raw = result.stdout + result.stderr
    running = "not running" not in raw.lower() and "✗" not in raw
    return GatewayStatus(running, raw)


# ---------------------------------------------------------------------------------------------
# Kanban (Phase 3). Every one of these prefers --json (ASES-ARC-04); the two commands that don't
# support it (dispatch's dry-run summary, and plain status changes) fall back to exit-code +
# stdout text, documented per function.
# ---------------------------------------------------------------------------------------------


def _kanban(board: str, args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    result = _run(["kanban", "--board", board, *args], timeout=timeout)
    if result.returncode != 0:
        raise HermesCommandError(["kanban", "--board", board, *args], result.returncode,
                                  result.stdout + result.stderr)
    return result


def _kanban_json(board: str, args: list[str], timeout: int = 30):
    result = _kanban(board, [*args, "--json"], timeout=timeout)
    return json.loads(result.stdout)


def kanban_init(board: str) -> None:
    _kanban(board, ["init"])


def kanban_create(
    board: str, title: str, *, assignee: str | None = None, parent: list[str] | None = None,
    workspace: str = "scratch", branch: str | None = None, project: str | None = None,
    body: str | None = None, idempotency_key: str | None = None, max_retries: int | None = None,
    max_runtime: str | None = None, initial_status: str | None = None,
) -> dict:
    args = ["create", title, "--workspace", workspace]
    if assignee:
        args += ["--assignee", assignee]
    for p in parent or []:
        args += ["--parent", p]
    if branch:
        args += ["--branch", branch]
    if project:
        args += ["--project", project]
    if body:
        args += ["--body", body]
    if idempotency_key:
        args += ["--idempotency-key", idempotency_key]
    if max_retries is not None:
        args += ["--max-retries", str(max_retries)]
    if max_runtime:
        args += ["--max-runtime", max_runtime]
    if initial_status:
        args += ["--initial-status", initial_status]
    return _kanban_json(board, args)


def kanban_show(board: str, card_id: str) -> dict:
    """Returns the flat task dict (same shape as list/create's entries). The raw CLI response wraps
    it as {"task": {...}, "parents": [...], "children": [...], "comments": [...], "events": [...],
    "runs": [...]} -- real shape, confirmed against a live card, not the flat shape this wrongly
    assumed at first (every module that calls kanban_show()["status"] depends on this unwrap)."""
    raw = _kanban_json(board, ["show", card_id])
    task = dict(raw["task"])
    task["_children"] = raw.get("children", [])
    task["_parents"] = raw.get("parents", [])
    task["_runs"] = raw.get("runs", [])
    # Real shapes (probed on a scratch board 2026-09-19): events are [{kind, payload, created_at, run_id}],
    # comments are [{author, body, created_at}]. A block adds a "BLOCKED: <reason>" comment and a `blocked`
    # event whose payload carries `reason`; an unblock adds "UNBLOCK: <reason>".
    task["_events"] = raw.get("events", [])
    task["_comments"] = raw.get("comments", [])
    task["_latest_summary"] = raw.get("latest_summary")
    return task


def kanban_list(board: str, *, status: str | None = None, assignee: str | None = None) -> list[dict]:
    args = ["list"]
    if status:
        args += ["--status", status]
    if assignee:
        args += ["--assignee", assignee]
    return _kanban_json(board, args)


def kanban_link(board: str, parent_id: str, child_id: str) -> None:
    _kanban(board, ["link", parent_id, child_id])


def kanban_dispatch(board: str, *, dry_run: bool = False, max_spawns: int | None = None) -> dict:
    args = ["dispatch"]
    if dry_run:
        args += ["--dry-run"]
    if max_spawns is not None:
        args += ["--max", str(max_spawns)]
    return _kanban_json(board, args)


def kanban_request_changes(board: str, card_id: str, reason: str) -> None:
    """The REVIEWER's verdict (Hermes: "return the active review run to its implementer"). It only works
    on a card claimed in an active review run, so the controller must not use it on a card that merely
    sits in `review`: see kanban_reopen_review."""
    _kanban(board, ["request-changes", card_id, reason])


def kanban_reopen_review(board: str, card_id: str, reason: str) -> None:
    """The controller's own send-back for a card sitting in `review`: review -> ready/todo, restored to
    its implementer, with `reason` recorded as a comment first ("CHANGES REQUESTED: <reason>").

    Checked against real Hermes 0.21.3 on 2026-09-19 (a scratch board): on a card in `review` that no
    reviewer has claimed, `request-changes` prints "task is not in an active review run" and exits 1,
    which _kanban raises as HermesCommandError, while `reopen-review` exits 0 and lands the card in
    `ready`. Passed as `--reason=<text>` in one argument so a reason starting with a dash is never read
    as an option."""
    _kanban(board, ["reopen-review", card_id, f"--reason={reason}"])


def kanban_complete(board: str, card_id: str, *, result: str | None = None, metadata: dict | None = None) -> None:
    args = ["complete", card_id]
    if result:
        args += ["--result", result]
    if metadata is not None:
        args += ["--metadata", json.dumps(metadata)]
    _kanban(board, args)


def kanban_block(board: str, card_id: str, reason: str, *, kind: str | None = None) -> None:
    """Block a card with `reason` (also recorded as a "BLOCKED: <reason>" comment, and as a `blocked` event whose
    payload carries the reason). `kind` is Hermes's typed block reason: `needs_input` is a question for a human,
    `capability` and `transient` describe a failure, `dependency` waits in todo. Hermes 0.21.3 (read from
    kanban_db.py and kanban.py 2026-09-21): block_task only accepts a card that is `running` or `ready`, so blocking
    a card that is already `blocked` (a merge card is created blocked) or in `todo` returns "cannot block" and exits
    1 AFTER the comment was added; and a second block of the same kind after an unblock routes the card to `triage`
    (BLOCK_RECURRENCE_LIMIT is 2) with a `block_loop_detected` event instead of `blocked`. Callers that escalate a
    card they did not just observe running or ready should go through questions.ask_user, which knows both.
    The reason follows `--` so one that starts with a dash is never read as an option, and `--kind` goes BEFORE the
    card id: Hermes's argparse rejects `block <id> --kind K -- <reason>` ("unrecognized arguments"), because the
    optional reason positional has already been matched empty when the option interrupts (checked offline against
    hermes_cli.kanban_parser 2026-09-21, together with the accepted forms)."""
    args = ["block"]
    if kind:
        args += ["--kind", kind]
    _kanban(board, [*args, card_id, "--", reason])


def kanban_schedule(board: str, card_id: str, reason: str) -> None:
    _kanban(board, ["schedule", card_id, reason])


def kanban_unblock(board: str, card_id: str, reason: str | None = None) -> None:
    """Unblock a card; `reason`, when given, is recorded as an "UNBLOCK: <reason>" comment first (this is how
    swarm answer delivers an answer)."""
    args = ["unblock", card_id]
    if reason:
        args += [f"--reason={reason}"]
    _kanban(board, args)


def kanban_comment(board: str, card_id: str, text: str, *, author: str | None = None) -> None:
    """Append a comment to a card. `--` before the text so a comment that starts with a dash is never read as
    an option (checked against real Hermes 2026-09-19)."""
    args = ["comment"]
    if author:
        args += ["--author", author]
    _kanban(board, [*args, card_id, "--", text])


def kanban_promote(board: str, card_id: str, reason: str | None = None) -> None:
    """Promote a todo/blocked card to ready with an audit reason (refused by Hermes while a parent is unfinished)."""
    args = ["promote", card_id]
    if reason:
        args += ["--", reason]
    _kanban(board, args)


def kanban_archive(board: str, card_ids: list[str]) -> None:
    """Archive cards (soft: Hermes keeps them, `archive --rm` is what deletes and this never calls it)."""
    if card_ids:
        _kanban(board, ["archive", *card_ids])


def kanban_set_model(board: str, card_id: str, model: str | None, *, provider: str | None = None) -> None:
    """Pin one card's worker to a model (and provider), or clear the override with model=None."""
    args = ["set-model"]
    if provider and model:
        args += ["--provider", provider]
    _kanban(board, [*args, card_id, model or "none"])


def kanban_reclaim(board: str, card_id: str, *, reason: str | None = None) -> None:
    args = ["reclaim", card_id]
    if reason:
        args += ["--reason", reason]
    _kanban(board, args)


@dataclasses.dataclass(frozen=True)
class SpecifyResult:
    """What `hermes kanban specify <id> --json` reported (fields read from `hermes_cli/kanban_specify.py`'s
    `SpecifyOutcome` and `hermes_cli/kanban.py`'s `_cmd_specify` -> `_run_triage_sweep`, 2026-09-22).

    ok is True exactly when the auxiliary LLM (`auxiliary.triage_specifier`) produced a usable title/body
    and Hermes moved the card `triage` -> `todo` (`kanban_db.specify_triage_task`); reason is Hermes's own
    text, never invented here, explaining the outcome either way (on success it is the literal word
    "specified"; on failure it is one of "unknown task id", "task is not in triage (status=...)", "LLM
    error: <type>", "LLM returned an empty response", "LLM response missing title and body", or "task moved
    out of triage before promotion" (a race), read verbatim from `kanban_specify.py`). new_title is the
    tightened title Hermes wrote when the auxiliary model's reply included one; it can be None even when ok
    is True (the reply had no usable "title" key, which `specify_task` treats as a normal outcome, not an
    error), and is always None when ok is False."""
    ok: bool
    reason: str | None
    new_title: str | None


def kanban_specify(board: str, card_id: str, *, author: str | None = None, timeout: int = 120) -> SpecifyResult:
    """Runs `hermes kanban specify <card_id> [--author NAME] --json`. NEVER pass `--all`: ASES specifies one
    card at a time, per r7_wp_specify.md. This is the ONE real, live auxiliary-model call this file makes
    once a user actually runs it for real (r7_rules.md's round 7 exception, "option A", 2026-09-22, and only
    from `triage.promote_card`); every other wrapper in this module stays exactly as it was.

    `timeout` defaults to 120 seconds (not `_kanban`'s own 30 second default, which is sized for a plain
    database operation): `kanban_specify.py`'s own `specify_task` passes `timeout or 120` to the auxiliary
    LLM call it makes, so 120 is Hermes's own real default for this exact call, not a guess.

    `--author` is passed only when given; when omitted, Hermes computes its own default author (the active
    profile, or "user", read from `kanban.py`'s own `_profile_author`), the same "only pass what you have"
    convention as `kanban_comment`. There is no free-text argument here to `--`-guard (`specify` takes only
    `task_id`, `--author`, `--json`, plus the `--all`/`--tenant` sweep flags ASES never uses), unlike
    `kanban_comment`/`kanban_block`, whose free-text argument DOES need one.

    Deliberately does NOT reuse `_kanban`/`_kanban_json`. Real Hermes behaviour, confirmed by reading
    `hermes_cli/kanban.py`'s `_run_triage_sweep` for the single-task_id path (not `--all`) that ASES always
    takes (2026-09-22): EVERY ok=False outcome (task not found, task not in triage, no auxiliary client
    configured, the model call itself failing, an empty or unusable reply, or the promotion losing a race)
    is printed as one line of JSON on stdout, `{"task_id", "ok": false, "reason": "...", "new_title": null}`,
    and the CLI THEN exits 1 (`_run_triage_sweep`: for a single id, `return 0 if ok_count == 1 else 1`).
    There is no zero-exit path for ok=false here, only zero-exit-and-ok=true or nonzero-exit-and-ok=false.
    This is a correction to this package's own work order, which asked whether ok=false was "a JSON success
    with ok: false inside it, or a nonzero CLI exit code": it is the latter, exit 1, with the JSON diagnostic
    still on stdout. A plain "any nonzero exit raises" rule (every OTHER wrapper's convention, since none of
    them has a JSON-success-shaped nonzero exit) would make an expected ok=false outcome indistinguishable
    from a genuine Hermes/infrastructure failure, which is exactly the distinction this function exists to
    preserve for `triage.promote_card` (see SpecifyResult's docstring). So the rule actually implemented is:
    parse stdout as the four-field JSON object regardless of exit code (0 or 1); when it parses to that
    shape, trust its own "ok" field and never raise for that case (Hermes's own contract for specify is "try
    to make sense of this, tell me if you could not," not "do this or fail," per the package file); only
    raise HermesCommandError when stdout does NOT parse to that shape at all, or the exit code is something
    other than 0 or 1 (a genuine crash, a bad board, hermes missing from PATH, or an argparse usage error,
    none of which prints this JSON shape). Malformed JSON is never given special leniency: json.loads is
    left to raise on it exactly as every other `_kanban_json` caller in this file already does, and that
    ValueError is what triggers the HermesCommandError below (not caught and re-interpreted as ok=false)."""
    args = ["specify", card_id]
    if author:
        args += ["--author", author]
    args += ["--json"]
    full_args = ["kanban", "--board", board, *args]
    result = _run(full_args, timeout=timeout)
    output = result.stdout + result.stderr
    if result.returncode not in (0, 1):
        raise HermesCommandError(full_args, result.returncode, output)
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        raise HermesCommandError(full_args, result.returncode, output) from None
    if not isinstance(payload, dict) or not {"ok", "reason", "new_title"} <= payload.keys():
        raise HermesCommandError(full_args, result.returncode, output)
    return SpecifyResult(
        ok=bool(payload.get("ok")),
        reason=payload.get("reason") or None,
        new_title=payload.get("new_title") or None,
    )


def pause(reason: str | None = None, timeout: int = 20) -> None:
    """ASES-REC-06: halts NEW dispatch/cron/gateway turns. Never kills work already in flight --
    that's Hermes's own documented behavior for `hermes pause`, not a gap here."""
    args = ["pause"]
    if reason:
        args += ["--reason", reason]
    result = _run(args, timeout=timeout)
    if result.returncode != 0:
        raise HermesCommandError(args, result.returncode, result.stdout + result.stderr)


def resume(timeout: int = 20) -> None:
    result = _run(["resume"], timeout=timeout)
    if result.returncode != 0:
        raise HermesCommandError(["resume"], result.returncode, result.stdout + result.stderr)


def session_usage(profile: str, session_id: str, timeout: int = 60) -> dict | None:
    """What one Hermes worker session cost, read from `hermes -p <profile> sessions export` (ASES-CAP-03).

    Checked against real Hermes 0.21.3 on 2026-09-19: the export prints ONE JSON object per session on one
    line, and that object also carries the whole conversation under "messages", which is never returned here.
    "api_call_count" is the number of model API calls the session made, which is what counts against a
    provider's daily quota.

    Returns {"id", "model", "api_call_count", "input_tokens", "output_tokens"} with the numbers as ints (a
    missing or unreadable number is 0, a missing model is ""). Returns None, and never raises, when the
    command cannot run, exits non-zero, times out, or prints no parseable JSON line for exactly this session
    id. None means "unknown, ask again later" and never "zero requests": a caller must record nothing for a
    session it got None for."""
    args = ["-p", profile, "sessions", "export", "--session-id", session_id, "--format", "jsonl", "--redact", "-"]
    try:
        result = _run(args, timeout=timeout)
    except (HermesNotFound, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None

    def count(value) -> int:
        try:
            return max(int(value or 0), 0)
        except (TypeError, ValueError, OverflowError):
            return 0

    for line in result.stdout.splitlines():
        try:
            session = json.loads(line)
        except ValueError:
            continue
        if isinstance(session, dict) and session.get("id") == session_id:
            return {
                "id": session_id,
                "model": str(session.get("model") or ""),
                "api_call_count": count(session.get("api_call_count")),
                "input_tokens": count(session.get("input_tokens")),
                "output_tokens": count(session.get("output_tokens")),
            }
    return None
