"""Primary checkout and idle worktree guards (section 8.4: ASES-GIT-02, ASES-GIT-12).

The primary checkout stays on the integration branch and is never edited by agents: only the merge queue
writes to that branch (ASES-GIT-02). The reviewer profile has file-write tools and a worker can be handed an
absolute path, so a stray write into the primary checkout is possible, and a dirty or moved primary checkout
makes the merge queue's fast-forward fail.

Hermes, not ASES, spawns workers, so a snapshot around each spawn is not possible. Instead the controller runs
check_primary_checkout once per polling pass and compares HEAD against the commit ASES itself last wrote or
verified (the integrity_state table, read and written here).

The same reasoning covers the OTHER worktrees (the second half of ASES-GIT-12): only the running card's own worker
may change its worktree, so a HEAD or a status that moved in a worktree NO running card owns was changed by
something else, for example a worker handed the absolute path of another card's worktree. check_idle_worktrees
compares each such worktree with the snapshot kept in the worktree_snapshots table and reports a change once;
snapshot_worktree, refresh_snapshots and list_worktrees are its parts.

This module only reports: what a violation does (halt the run, raise a security event) is the controller's
decision. The checks never raise and are read-only: every git call runs with --no-optional-locks, so it cannot
take index.lock away from a real git operation, and a git that fails or hangs is reported as a problem, never
as a clean checkout.
"""
from __future__ import annotations

import dataclasses
import hashlib
import os
import pathlib
import sqlite3
import subprocess
from collections.abc import Iterable
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


# ---------------------------------------------------------------------------------------------
# The other worktrees of the repository (ASES-GIT-12)
# ---------------------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class WorktreeInfo:
    """One worktree as `git worktree list --porcelain` reports it. path is a Path, so it compares equal to the
    native spelling of the same directory (git prints forward slashes). head is the full SHA, "" for a bare
    repository. branch is the short branch name (refs/heads/ removed), None on a detached HEAD or a bare entry.
    locked and prunable are flags only; git's free-text reason for either is dropped."""
    path: pathlib.Path
    head: str
    branch: str | None
    detached: bool = False
    bare: bool = False
    locked: bool = False
    prunable: bool = False


def _ascii(text: object) -> str:
    """text made safe to print on a cp1252 console: anything that is not ASCII becomes a backslash escape."""
    return str(text).encode("ascii", "backslashreplace").decode("ascii")


def _worktree_from_fields(fields: list[str]) -> WorktreeInfo | None:
    """One record of the porcelain listing, as its `key value` fields, or None when it has no worktree line.
    A key this parser does not know is skipped, so a newer git that adds one does not break the listing."""
    path: str | None = None
    head = ""
    branch: str | None = None
    detached = bare = locked = prunable = False
    for field in fields:
        key, _, value = field.partition(" ")
        if key == "worktree":
            path = value
        elif key == "HEAD":
            head = value
        elif key == "branch":
            branch = value[len("refs/heads/"):] if value.startswith("refs/heads/") else value
        elif key == "detached":
            detached = True
        elif key == "bare":
            bare = True
        elif key == "locked":
            locked = True
        elif key == "prunable":
            prunable = True
    if path is None:
        return None
    return WorktreeInfo(pathlib.Path(path), head, branch, detached, bare, locked, prunable)


def _parse_worktrees(text: str, terminator: str) -> list[WorktreeInfo]:
    """Parse `git worktree list --porcelain` output. Records are separated by an empty field, and fields end at
    `terminator`: NUL with -z (a path is then read verbatim, even one holding a newline), a newline without."""
    fields = text.split(terminator)
    if terminator == "\n":
        fields = [field.rstrip("\r") for field in fields]
    worktrees: list[WorktreeInfo] = []
    record: list[str] = []
    for field in [*fields, ""]:  # the extra empty field closes the last record when the output lacks a blank line
        if field:
            record.append(field)
            continue
        found = _worktree_from_fields(record)
        if found is not None:
            worktrees.append(found)
        record = []
    return worktrees


def _worktrees(repo: pathlib.Path) -> tuple[list[WorktreeInfo] | None, str]:
    """(worktrees, "") or (None, why git could not list them). `-z` is tried first; a git older than 2.36 has no -z
    for `worktree list` and answers with a usage error (exit 129), and only then is the plain form used."""
    code, out, err = _git(repo, ["worktree", "list", "--porcelain", "-z"])
    terminator = "\0"
    if code == 129:
        code, out, err = _git(repo, ["worktree", "list", "--porcelain"])
        terminator = "\n"
    if code != 0:
        return None, _first_line(err) or f"git exited {code}"
    return _parse_worktrees(out, terminator), ""


def list_worktrees(repo: pathlib.Path) -> list[WorktreeInfo]:
    """Every worktree of the repository, the primary checkout first (git always lists it first), in git's order.
    Read-only, and never raises: a git that fails, hangs or is missing, or a directory that is not a repository,
    gives an empty list, so a caller simply sees no worktrees. check_idle_worktrees needs to tell that apart from
    a repository with none, and asks for the reason itself."""
    worktrees, _why = _worktrees(pathlib.Path(repo))
    return worktrees or []


def _path_key(path: str | os.PathLike) -> str:
    """A path in the one spelling used to compare worktree paths and to key worktree_snapshots: symbolic links and
    Windows short names resolved, separators unified and, on Windows, case folded. git prints a path with forward
    slashes while Hermes and Python use backslashes, and Windows paths differ in case without being different."""
    text = os.fspath(path)
    try:
        text = os.path.realpath(text)
    except (OSError, ValueError):
        text = os.path.normpath(text)
    return os.path.normcase(text)


def _path_keys(paths: Iterable[str | os.PathLike] | str | os.PathLike | None) -> set[str]:
    """_path_key of every non-empty path. A bare string or Path is one path, not a sequence of characters."""
    if paths is None:
        return set()
    if isinstance(paths, (str, os.PathLike)):
        paths = (paths,)
    return {_path_key(path) for path in paths if path is not None and os.fspath(path) != ""}


def snapshot_worktree(path: str | os.PathLike, *, ignore_prefixes: tuple[str, ...] = ()) -> tuple[str, str]:
    """(HEAD sha, sha256 of `git status --porcelain -z --untracked-files=normal`) of one worktree, or ("", "")
    when either cannot be read (not a repository, a missing directory, a git that fails or hangs, no commits).
    Read-only, never raises, the same --no-optional-locks git as the primary check. The hash, not the status
    text, is what is stored: it says THAT the working tree changed, and the change itself is the worker's own
    business, not something to keep a copy of.

    ignore_prefixes works as in check_primary_checkout: a status path under one of them, and a rename whose
    two paths both are, is left out of the hash. With none (the default) the hash covers git's output exactly as
    it is; with some it covers the entries that remain, so every call that shares a stored snapshot must pass the
    same prefixes."""
    path = pathlib.Path(path)
    head, _why = _read_head(path)
    if not head:
        return "", ""
    code, out, _err = _git(path, ["status", "--porcelain", "-z", "--untracked-files=normal"])
    if code != 0:
        return "", ""
    prefixes = _normalize_prefixes(ignore_prefixes)
    if prefixes:
        out = "\0".join(
            f"{status}\0{entry}\0{source}" for status, entry, source in _status_entries(out)
            if not (entry.startswith(prefixes) and (not source or source.startswith(prefixes)))
        )
    return head, hashlib.sha256(out.encode("utf-8")).hexdigest()


def _other_worktrees(worktrees: list[WorktreeInfo], repo: pathlib.Path) -> list[tuple[str, WorktreeInfo]]:
    """(path key, worktree) of every worktree that is neither the primary checkout nor a bare repository, which
    have no working tree to snapshot. The primary checkout is the first entry git lists, and is also skipped by
    path, in case `repo` names a linked worktree: check_primary_checkout owns it either way."""
    repo_key = _path_key(repo)
    others = []
    for index, worktree in enumerate(worktrees):
        key = _path_key(worktree.path)
        if index == 0 or worktree.bare or key == repo_key:
            continue
        others.append((key, worktree))
    return others


def _store_snapshot(conn: sqlite3.Connection, project: str, key: str, head: str, status_hash: str) -> None:
    conn.execute(
        "INSERT INTO worktree_snapshots (project, path, head, status_hash, taken_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(project, path) DO UPDATE SET head=excluded.head, status_hash=excluded.status_hash, "
        "taken_at=excluded.taken_at",
        (project, key, head, status_hash, datetime.now(timezone.utc).isoformat(timespec="seconds")),
    )


def check_idle_worktrees(
    conn: sqlite3.Connection, project: str, repo: pathlib.Path, running_paths: Iterable[str | os.PathLike],
    *, ignore_prefixes: tuple[str, ...] = (),
) -> list[str]:
    """ASES-GIT-12, other worktrees: "Any change outside the worker's own worktree fails the card and raises a
    security event". Reports what changed in a worktree no running card owns, once.

    running_paths is the set of worktree paths of the cards running now (Hermes spells them however it likes:
    separators and, on Windows, case are normalised before comparing). For every other worktree of the
    repository, the primary checkout and a bare repository excepted, the snapshot is compared with the row in
    worktree_snapshots:

      - no row yet is a first sight: the snapshot is recorded and nothing is reported;
      - a moved HEAD, or a changed status hash, is ONE problem for that worktree ("worktree <path> changed while
        no card was running in it: HEAD a..b" and/or "status changed", a and b shortened to 12 characters), and
        the new snapshot is stored so the same change is reported once, not on every pass from now on;
      - a worktree of a running card is skipped, its own worker may change it, and its row is deleted: the
        baseline is taken again the first time the worktree is seen idle (the snapshot after the card stops);
      - a worktree that has disappeared, or that git marks prunable because its directory is gone, has its row
        dropped and is not a problem.

    Deviations from the blueprint text, on purpose and the same as check_primary_checkout: this runs once per
    polling pass, not around each spawn (Hermes, not ASES, spawns workers), and it reports rather than fails a
    card (the writer cannot be attributed to one). What a problem does is the controller's decision.

    Never raises for git. A git that cannot list the worktrees gives one problem saying so, and every stored
    snapshot is kept, so a change made during the outage is still reported when git recovers; a worktree that
    exists but cannot be read is a problem too, with its old snapshot kept. Neither is a clean bill of health.
    ignore_prefixes is passed to snapshot_worktree."""
    repo = pathlib.Path(repo)
    worktrees, why = _worktrees(repo)
    if worktrees is None:
        return [f"cannot list the worktrees of {_ascii(repo)}: {why}"]
    running = _path_keys(running_paths)
    problems: list[str] = []
    baselined: set[str] = set()
    for key, worktree in _other_worktrees(worktrees, repo):
        if key in running:
            conn.execute("DELETE FROM worktree_snapshots WHERE project = ? AND path = ?", (project, key))
            continue
        if worktree.prunable or not worktree.path.is_dir():
            continue
        baselined.add(key)
        shown = _ascii(worktree.path)
        head, status_hash = snapshot_worktree(worktree.path, ignore_prefixes=ignore_prefixes)
        if not head:
            problems.append(f"worktree {shown} could not be inspected: git could not read its HEAD or its status")
            continue
        row = conn.execute(
            "SELECT head, status_hash FROM worktree_snapshots WHERE project = ? AND path = ?", (project, key),
        ).fetchone()
        if row is not None and (row[0], row[1]) == (head, status_hash):
            continue
        if row is not None:
            changes = []
            if row[0] != head:
                changes.append(f"HEAD {row[0][:12]}..{head[:12]}")
            if row[1] != status_hash:
                changes.append("status changed")
            problems.append(f"worktree {shown} changed while no card was running in it: {'; '.join(changes)}")
        _store_snapshot(conn, project, key, head, status_hash)
    for (stored,) in conn.execute("SELECT path FROM worktree_snapshots WHERE project = ?", (project,)).fetchall():
        if stored not in baselined:
            conn.execute("DELETE FROM worktree_snapshots WHERE project = ? AND path = ?", (project, stored))
    return problems


def refresh_snapshots(
    conn: sqlite3.Connection, project: str, repo: pathlib.Path, running_paths: Iterable[str | os.PathLike],
    *, ignore_prefixes: tuple[str, ...] = (),
) -> int:
    """Take a fresh snapshot of every worktree no running card owns and store it, returning how many were stored.
    For the moments ASES itself has just changed a worktree on purpose (a fix-card repoint, say) and would
    otherwise be told about its own change by the next check_idle_worktrees: call this once the change is made,
    and the new state becomes the baseline. It is the one way to accept a change, so it must only ever follow an
    ASES action, never a report.

    The primary checkout, a bare repository, a running card's worktree (it has no baseline while its worker may
    change it) and a worktree whose directory is gone or whose state cannot be read are skipped. Never raises:
    when git cannot list the worktrees nothing is stored and 0 comes back."""
    repo = pathlib.Path(repo)
    worktrees, _why = _worktrees(repo)
    if worktrees is None:
        return 0
    running = _path_keys(running_paths)
    stored = 0
    for key, worktree in _other_worktrees(worktrees, repo):
        if key in running or worktree.prunable or not worktree.path.is_dir():
            continue
        head, status_hash = snapshot_worktree(worktree.path, ignore_prefixes=ignore_prefixes)
        if head:
            _store_snapshot(conn, project, key, head, status_hash)
            stored += 1
    return stored
