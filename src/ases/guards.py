"""Primary checkout guard (section 8.4: ASES-GIT-02, ASES-GIT-12).

The primary checkout stays on the integration branch and is never edited by agents: only the merge queue
writes to that branch (ASES-GIT-02). The reviewer profile has file-write tools and a worker can be handed an
absolute path, so a stray write into the primary checkout is possible, and a dirty or moved primary checkout
makes the merge queue's fast-forward fail.

Hermes, not ASES, spawns workers, so a snapshot around each spawn is not possible. Instead the controller runs
check_primary_checkout once per polling pass and compares HEAD against the commit ASES itself last wrote or
verified (the integrity_state table, read and written here).

This module only reports: what a violation does (halt the run, raise a security event) is the controller's
decision. The check never raises and is read-only: every git call runs with --no-optional-locks, so it cannot
take index.lock away from a real git operation, and a git that fails or hangs is reported as a problem, never
as a clean checkout.
"""
from __future__ import annotations

import dataclasses
import pathlib
import sqlite3
import subprocess
from datetime import datetime, timezone

_GIT_TIMEOUT = 60  # seconds per git call, the same as the merge queue's


@dataclasses.dataclass(frozen=True)
class GuardResult:
    """One check of the primary checkout. ok is True exactly when problems is empty. head is the full HEAD SHA
    and branch the checked-out branch name; each is "" when git could not say. head is "" when HEAD could not be
    read at all (git missing, not a repository, no commits), which is how a caller tells "could not check" from
    "checked and found a violation"; branch is also "" on a detached HEAD."""
    ok: bool
    problems: tuple[str, ...]
    head: str
    branch: str


def _git(repo: pathlib.Path, args: list[str]) -> tuple[int, str, str]:
    """Run one read-only git command in repo and return (exit code, stdout, stderr). Output is decoded from bytes
    as UTF-8: `status -z` prints paths verbatim, so the locale code page would mangle a non-ASCII name and text
    mode would rewrite a carriage return inside one. A git that cannot be started, or does not answer within the
    timeout, comes back as exit code -1 with the reason in stderr: callers deal in one failure shape and nothing
    here raises."""
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(repo), *args], capture_output=True, timeout=_GIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return -1, "", f"git {args[0]} timed out after {_GIT_TIMEOUT}s"
    except OSError as exc:
        return -1, "", f"git could not be run: {exc}"
    return (
        result.returncode,
        result.stdout.decode("utf-8", errors="replace"),
        result.stderr.decode("utf-8", errors="replace"),
    )


def _first_line(text: str) -> str:
    """The first line of git's stderr, capped and made ASCII-safe so a problem string can be printed or logged
    anywhere. "" when git said nothing."""
    lines = text.strip().splitlines()
    return lines[0][:200].encode("ascii", "backslashreplace").decode("ascii") if lines else ""


def _read_head(repo: pathlib.Path) -> tuple[str, str]:
    """(full HEAD SHA, "") or ("", why it could not be read). Uses --verify -q rather than a bare rev-parse,
    which echoes "HEAD" on stdout while failing in a repository that has no commits."""
    code, out, err = _git(repo, ["rev-parse", "--verify", "-q", "HEAD"])
    head = out.strip()
    if code == 0 and head:
        return head, ""
    return "", _first_line(err) or "HEAD does not resolve to a commit"


def _status_entries(raw: str) -> list[tuple[str, str, str]]:
    """Parse `git status --porcelain -z` into (status code, path, rename source) per entry. Each entry is
    'XY path' ended by a NUL, and a rename or copy (R or C in XY) is followed by one more NUL-ended field, the
    original path: destination first, source second. With -z paths come out verbatim, with no C-style quoting,
    so a space or a quote in a name needs no unescaping. The source is "" for every other entry. Nothing git
    lists is dropped: a field too short to hold a path is still returned, with an empty path."""
    fields = [f for f in raw.split("\0") if f]
    entries: list[tuple[str, str, str]] = []
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        code, path, source = entry[:2], entry[3:], ""
        if ("R" in code or "C" in code) and i < len(fields):
            source = fields[i]
            i += 1
        entries.append((code.strip(), path, source))
    return entries


def _normalize_prefixes(prefixes: tuple[str, ...]) -> tuple[str, ...]:
    """Each ignore prefix as a directory prefix: forward slashes and exactly one trailing slash, so '.worktrees'
    does not also swallow '.worktrees-old/'. An empty prefix is dropped, because as a plain startswith() it would
    match every path and switch the whole check off."""
    if isinstance(prefixes, str):  # a bare string would otherwise be read one character at a time
        prefixes = (prefixes,)
    cleaned = (p.replace("\\", "/").strip("/") for p in prefixes)
    return tuple(p + "/" for p in cleaned if p)


def check_primary_checkout(
    repo: pathlib.Path, integration_branch: str, expected_head: str | None = None,
    *, ignore_prefixes: tuple[str, ...] = (".worktrees/",),
) -> GuardResult:
    """ASES-GIT-12: did anything other than the controller change the primary checkout?

    One problem is reported for each of: the checkout is not on integration_branch (another branch, or a
    detached HEAD); HEAD differs from expected_head, when one is given (None skips the comparison; both SHAs are
    named, shortened to 12 characters); and every path `git status --porcelain` lists, modified, staged,
    deleted, renamed and untracked alike, except paths under an ignore prefix. Paths git itself ignores never
    appear in porcelain output, so they need no handling here. --untracked-files=normal is passed explicitly so a
    status.showUntrackedFiles=no in the checkout's own config cannot hide an untracked file, and it lists an
    untracked directory once, by name, which keeps the result small.

    Hermes keeps its worker worktrees in .worktrees/ inside the primary checkout, so that prefix is ignored by
    default (passing ignore_prefixes replaces it). A prefix names a directory at the start of the path: '.worktrees'
    and '.worktrees/' both match '.worktrees/x' and neither matches '.worktrees-old/x' or 'sub/.worktrees/x'. A
    rename is ignored only when both its paths are, so moving a tracked file into an ignored directory is still
    reported.

    Never raises. A git that fails, hangs or is missing gives ok False and a problem saying so: a check that could
    not run is not a clean bill of health."""
    problems: list[str] = []

    code, out, err = _git(repo, ["symbolic-ref", "--short", "-q", "HEAD"])
    if code == 0:
        branch = out.strip()
        if branch != integration_branch:
            problems.append(f"primary checkout is on branch {ascii(branch)}, expected {ascii(integration_branch)}")
    elif code == 1:  # `symbolic-ref -q` exits 1, silently, on a detached HEAD
        branch = ""
        problems.append(f"primary checkout has a detached HEAD, expected branch {ascii(integration_branch)}")
    else:  # not a repository, a missing directory, git not runnable: nothing else can be trusted either
        why = _first_line(err) or f"git exited {code}"
        return GuardResult(False, (f"cannot inspect the primary checkout at {repo}: {why}",), "", "")

    head, why = _read_head(repo)
    if not head:
        problems.append(f"cannot read HEAD of the primary checkout at {repo}: {why}")
    elif expected_head is not None and head != expected_head:
        problems.append(f"primary checkout HEAD moved: expected {expected_head[:12]}, found {head[:12]}")

    code, out, err = _git(repo, ["status", "--porcelain", "-z", "--untracked-files=normal"])
    if code != 0:
        why = _first_line(err) or f"git exited {code}"
        problems.append(f"git status failed in the primary checkout at {repo}: {why}")
    else:
        prefixes = _normalize_prefixes(ignore_prefixes)
        for status, path, source in _status_entries(out):
            if path.startswith(prefixes) and (not source or source.startswith(prefixes)):
                continue
            named = f"{ascii(path)} (from {ascii(source)})" if source else ascii(path)
            problems.append(f"primary checkout is dirty: {status} {named}")

    return GuardResult(not problems, tuple(problems), head, branch)


def expected_head(conn: sqlite3.Connection, project: str) -> str | None:
    """The primary checkout HEAD ASES itself last wrote or verified for this project, or None when none has been
    recorded yet (a first run, before adopt_current_head)."""
    row = conn.execute("SELECT expected_head FROM integrity_state WHERE project = ?", (project,)).fetchone()
    return None if row is None else row[0]


def set_expected_head(conn: sqlite3.Connection, project: str, sha: str) -> None:
    """Record the primary checkout HEAD ASES has just written (a merge) or verified. An upsert, so calling it
    again, with the same SHA or a new one, leaves one row per project. Refuses a blank SHA: recording one would
    make every later check report a moved HEAD."""
    sha = sha.strip()
    if not sha:
        raise ValueError("expected head must be a commit SHA, not an empty string")
    conn.execute(
        "INSERT INTO integrity_state (project, expected_head, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(project) DO UPDATE SET expected_head=excluded.expected_head, "
        "updated_at=excluded.updated_at",
        (project, sha, datetime.now(timezone.utc).isoformat(timespec="seconds")),
    )


def adopt_current_head(conn: sqlite3.Connection, project: str, repo: pathlib.Path) -> str:
    """Read the primary checkout's HEAD, record it as the expected head and return it. For the start of a run,
    once the checkout has been verified clean: from then on a HEAD other than the recorded one was moved by
    something other than the controller. Raises RuntimeError when HEAD cannot be read, rather than recording
    nothing or garbage."""
    head, why = _read_head(repo)
    if not head:
        raise RuntimeError(f"cannot read HEAD of the primary checkout at {repo}: {why}")
    set_expected_head(conn, project, head)
    return head
