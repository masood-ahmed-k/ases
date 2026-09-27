"""Per-card resource leases and the .env.ases file (section 8.4: ASES-GIT-14; blueprint test 22.5).

Several workers run at once (kanban.max_in_progress, ASES-ROL-08), each in its own worktree, and nothing in a
worktree stops two of them starting the same dev server on the same port, running `docker compose up` under the
same project name (which recreates the other worker's containers) or writing to the same test database. So every
card is handed its own values by ASES, and they are written to .env.ases in its worktree:

  - a block of ports. This is the one thing that is a LEASED resource: a row in resource_leases (resource
    "port-block:<n>", holder = the card id), and a partial unique index over the rows that are still active, so two
    cards can never hold the same block even when two controller processes race for it;
  - a compose project name, a database name and a temp directory, all DERIVED from (project, card id). They are
    unique exactly when the card ids are, so they need no lease of their own;
  - singletons such as a shared development database, taken through the same table as "singleton:<name>" locks.

Hermes creates the worktree and starts the worker when it claims a card, not ASES (ASES-ARC-02), so nothing can be
written into a worktree before the worker begins. provision_running_cards therefore runs on the polling loop and
is best effort: see its docstring.

A lease is released, never deleted: released_at is stamped, so the table stays a history of who held what.
The only outside calls are hermes.kanban_list and hermes.kanban_show (both injectable, and only from the two
functions that wire this module into the controller pass) and one read-only `git rev-parse` in write_env_file.
Nothing calls a provider.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import pathlib
import re
import socket
import sqlite3
import subprocess
import tempfile
from collections.abc import Callable, Iterable
from datetime import datetime, timezone

from . import events
from . import gitexec
from . import hermes as hermes_mod
from . import plan as plan_mod

ENV_FILE_NAME = ".env.ases"
DEFAULT_BASE_PORT = 42000
DEFAULT_BLOCK_SIZE = 10
DEFAULT_MAX_BLOCKS = 100
# Statuses in which a card still owns its worktree and so its resources. Cards that are done, archived or blocked
# do not: see sweep_finished for the one consequence of that worth knowing.
LIVE_STATUSES = ("running", "ready", "review", "scheduled")

_PORT_BLOCK = "port-block:"
_SINGLETON = "singleton:"
_NAME_LIMIT = 63  # docker compose project names and PostgreSQL identifiers both stop here
_LAST_PORT = 65535
_GIT_TIMEOUT = 30  # seconds for the one git call write_env_file makes


class LeaseError(Exception):
    """A resource could not be leased or an env file could not be written."""


class ResourceBusy(LeaseError):
    """A singleton is already held by another card. `holder` is the card that has it and `resource` the lease
    name ("singleton:<name>"), so the caller can tell the Lead who to wait for. Running out of port blocks is a
    plain LeaseError, not this: nobody in particular is holding what is missing."""

    def __init__(self, resource: str, holder: str):
        self.resource = resource
        self.holder = holder
        super().__init__(f"{resource} is already held by {_ascii(holder)}")


@dataclasses.dataclass(frozen=True)
class CardEnv:
    """The values one card is handed (ASES-GIT-14). ports is the whole block, port_base its first port and
    port_count its length. temp_dir is a plain string so the value round-trips through JSON and an env file."""
    card_id: str
    port_base: int
    port_count: int
    ports: tuple[int, ...]
    compose_project: str
    db_name: str
    temp_dir: str

    def as_env(self) -> dict[str, str]:
        """The environment variables for this card. The temp directory is exported under every name a tool might
        read (TMPDIR, TEMP, TMP as well as ASES_TMPDIR), so a build tool that ignores ours still writes inside
        the card's own directory instead of a shared one."""
        env = {
            "ASES_PORT_BASE": str(self.port_base),
            "ASES_PORT_COUNT": str(self.port_count),
            "COMPOSE_PROJECT_NAME": self.compose_project,
            "ASES_DB_NAME": self.db_name,
            "ASES_TMPDIR": self.temp_dir,
            "TMPDIR": self.temp_dir,
            "TEMP": self.temp_dir,
            "TMP": self.temp_dir,
        }
        for index, port in enumerate(self.ports):
            env[f"ASES_PORT_{index}"] = str(port)
        return env


# ---------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------


def _ascii(text: object) -> str:
    """text made safe to print on a cp1252 console: anything that is not ASCII becomes a backslash escape."""
    return str(text).encode("ascii", "backslashreplace").decode("ascii")


def _stamp(now: datetime | str | None) -> str:
    """The UTC timestamp string stored in acquired_at and released_at. `now` exists so a test can pin the clock:
    a datetime (a naive one is taken as UTC) or an already formatted string; None means the real time."""
    if isinstance(now, str):
        return now
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).isoformat(timespec="seconds")


def is_port_free(port: int) -> bool:
    """True when a TCP socket can be bound to 127.0.0.1:port right now. Never raises: a probe that broke port
    allocation would take a card down for a reason that has nothing to do with the card. Anything that stops the
    bind (port in use, a port range Windows reserves, an invalid number) reads as "not free", which is the safe
    answer for a block we were about to hand out.

    SO_REUSEADDR is deliberately NOT set: on Windows it would let this bind succeed on a port another process is
    already listening on, and the probe would report it free."""
    try:
        if not 1 <= port <= _LAST_PORT:
            return False
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", port))
    except Exception:  # noqa: BLE001 - deliberately broad, see above
        return False
    return True


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8]


def _slug(text: object, sep: str) -> str:
    """text lowercased, with every run of characters outside [a-z0-9] replaced by `sep` and none left at either
    end. Lossy on purpose (two ids that differ only in punctuation slug alike, and Hermes ids never do)."""
    return re.sub(r"[^a-z0-9]+", sep, str(text).lower()).strip(sep)


def _part(text: object, sep: str) -> str:
    """_slug, but never empty: an id made only of characters the slug drops still yields a distinct part (a hash
    of the raw text), so it cannot collapse into the neighbouring separators."""
    return _slug(text, sep) or "x" + _digest(str(text))


def _dir_part(card_id: object) -> str:
    """The card's directory name under the temp root. Unlike the compose and database names it keeps an
    underscore, so a Hermes id such as t_1a2b3c4d is the directory name unchanged. Every other character (a path
    separator, a dot, anything unsafe) becomes an underscore, so an id can never climb out of the temp root."""
    cleaned = re.sub(r"[^a-z0-9_-]+", "_", str(card_id).lower()).strip("_-")
    return cleaned or "x" + _digest(str(card_id))


def _bounded(name: str, sep: str, seed: str) -> str:
    """name unchanged when it fits the 63 character limit. Otherwise it is cut and closed with a hash of the raw
    (project, card id), so two long ids that differ only at the tail stay different instead of both being
    truncated to the same string."""
    if len(name) <= _NAME_LIMIT:
        return name
    tail = _digest(seed)
    return name[: _NAME_LIMIT - len(tail) - 1].rstrip(sep) + sep + tail


def _derive_names(project: str, card_id: str, temp_root: str | os.PathLike | None) -> tuple[str, str, str]:
    """(compose project, database name, temp dir) for one card, derived only from (project, card id), so the same
    card always gets the same three and different cards get different ones. Compose: ases-<project>-<card>,
    [a-z0-9-] only. Database: ases_<project>_<card>, [a-z0-9_] only. Both at most 63 characters. Temp dir:
    <temp root or the system temp>/ases/<project>/<card>."""
    seed = f"{project}\0{card_id}"
    compose = _bounded(f"ases-{_part(project, '-')}-{_part(card_id, '-')}", "-", seed)
    database = _bounded(f"ases_{_part(project, '_')}_{_part(card_id, '_')}", "_", seed)
    root = pathlib.Path(os.fspath(temp_root)) if temp_root is not None else pathlib.Path(tempfile.gettempdir())
    return compose, database, str(root / "ases" / _part(project, "-") / _dir_part(card_id))


def _block_number(resource: str) -> int | None:
    """The n of "port-block:<n>", or None for any other resource or a suffix that is not a whole number."""
    if not resource.startswith(_PORT_BLOCK):
        return None
    suffix = resource[len(_PORT_BLOCK):]
    return int(suffix) if suffix.isascii() and suffix.isdigit() else None


def _block_env(
    project: str, card_id: str, block: int, base_port: int, block_size: int, temp_root: str | os.PathLike | None,
) -> CardEnv:
    port_base = base_port + block * block_size
    compose, database, temp_dir = _derive_names(project, card_id, temp_root)
    return CardEnv(
        card_id=card_id, port_base=port_base, port_count=block_size,
        ports=tuple(range(port_base, port_base + block_size)),
        compose_project=compose, db_name=database, temp_dir=temp_dir,
    )


def _env_from_detail(detail: str | None) -> CardEnv | None:
    """The CardEnv stored in a lease's detail column, or None when the column is missing or unreadable."""
    try:
        raw = json.loads(detail or "")
        return CardEnv(
            card_id=str(raw["card_id"]), port_base=int(raw["port_base"]), port_count=int(raw["port_count"]),
            ports=tuple(int(port) for port in raw["ports"]), compose_project=str(raw["compose_project"]),
            db_name=str(raw["db_name"]), temp_dir=str(raw["temp_dir"]),
        )
    except (TypeError, ValueError, KeyError):
        return None


def _card_lease(conn: sqlite3.Connection, project: str, card_id: str) -> tuple[int, str | None] | None:
    """(block number, detail) of the port block this card holds, or None. A row whose name is not a block number
    (something other than this module wrote it) is not a port block and is ignored."""
    rows = conn.execute(
        "SELECT resource, detail FROM resource_leases "
        "WHERE project = ? AND holder = ? AND released_at IS NULL AND resource LIKE ? ORDER BY id",
        (project, card_id, _PORT_BLOCK + "%"),
    ).fetchall()
    for resource, detail in rows:
        block = _block_number(resource)
        if block is not None:
            return block, detail
    return None


def _leased_blocks(conn: sqlite3.Connection, project: str) -> set[int]:
    """The numbers of every port block with an active lease in this project, whoever holds it."""
    rows = conn.execute(
        "SELECT resource FROM resource_leases WHERE project = ? AND released_at IS NULL AND resource LIKE ?",
        (project, _PORT_BLOCK + "%"),
    ).fetchall()
    numbers = (_block_number(resource) for (resource,) in rows)
    return {number for number in numbers if number is not None}


def _held_env(
    conn: sqlite3.Connection, project: str, card_id: str, base_port: int, block_size: int,
    temp_root: str | os.PathLike | None,
) -> CardEnv | None:
    """The env of the block this card already holds: exactly what was handed out (read back from the lease), or,
    when the lease carries no readable record, rebuilt from the block number."""
    held = _card_lease(conn, project, card_id)
    if held is None:
        return None
    block, detail = held
    return _env_from_detail(detail) or _block_env(project, card_id, block, base_port, block_size, temp_root)


# ---------------------------------------------------------------------------------------------
# Port blocks
# ---------------------------------------------------------------------------------------------


def allocate_card_env(
    conn: sqlite3.Connection, project: str, card_id: str, *, base_port: int = DEFAULT_BASE_PORT,
    block_size: int = DEFAULT_BLOCK_SIZE, max_blocks: int = DEFAULT_MAX_BLOCKS,
    port_free: Callable[[int], bool] | None = None, temp_root: str | os.PathLike | None = None,
    now: datetime | str | None = None,
) -> CardEnv:
    """ASES-GIT-14: "Each card gets its own port block, COMPOSE_PROJECT_NAME, database name or schema, and temp
    directory". Leases the lowest numbered port block the card can have and returns its whole env.

    Idempotent: a card that already holds a block gets back exactly what it was handed, whatever the arguments
    are this time, so a retried provisioning never moves a running worker's ports. Otherwise the lowest block
    "port-block:<n>" with no active lease is taken (block n covers base_port + n * block_size upward, block_size
    ports), skipping a block whose FIRST port is not free right now (port_free, a real bind probe by default, is
    injectable): another process may already be listening there, and leasing the block would only hand the card a
    collision. Blocks are counted per project, the same as the unique index behind them.

    The insert is what makes it safe: two controllers can pick the same block, and the partial unique index lets
    only one of them insert. The loser catches the IntegrityError and moves on to the next block (or, when the
    "loser" was a second call for this very card, returns the block the first call took, so a card never ends up
    holding two).

    Only blocks whose last port still exists are offered, so base_port=65500 with block_size=10 has 3 blocks, not
    100. Raises LeaseError when no block is left and ValueError for a block_size or base_port below 1."""
    if block_size < 1 or base_port < 1:
        raise ValueError("block_size and base_port must be at least 1")
    held = _held_env(conn, project, card_id, base_port, block_size, temp_root)
    if held is not None:
        return held

    probe = port_free if port_free is not None else is_port_free
    usable = max(min(max_blocks, (_LAST_PORT + 1 - base_port) // block_size), 0)
    taken = _leased_blocks(conn, project)
    stamp = _stamp(now)
    for block in range(usable):
        if block in taken:
            continue
        if not probe(base_port + block * block_size):
            continue
        env = _block_env(project, card_id, block, base_port, block_size, temp_root)
        try:
            conn.execute(
                "INSERT INTO resource_leases (project, resource, holder, detail, acquired_at) VALUES (?, ?, ?, ?, ?)",
                (project, f"{_PORT_BLOCK}{block}", card_id, json.dumps(dataclasses.asdict(env), sort_keys=True), stamp),
            )
        except sqlite3.IntegrityError:
            raced = _held_env(conn, project, card_id, base_port, block_size, temp_root)
            if raced is not None:
                return raced
            continue
        return env
    raise LeaseError(
        f"no free port block for {_ascii(card_id)}: all {usable} blocks of {block_size} ports from "
        f"{base_port} are leased or in use"
    )


def release_card_resources(
    conn: sqlite3.Connection, project: str, card_id: str, *, now: datetime | str | None = None,
) -> int:
    """Release every active lease the card holds, its port block and any singleton, and return how many were
    released. Stamps released_at instead of deleting, so the table keeps the history. Idempotent: a second call
    finds nothing active and returns 0."""
    cursor = conn.execute(
        "UPDATE resource_leases SET released_at = ? WHERE project = ? AND holder = ? AND released_at IS NULL",
        (_stamp(now), project, card_id),
    )
    return cursor.rowcount


# ---------------------------------------------------------------------------------------------
# Singletons
# ---------------------------------------------------------------------------------------------


def _active_holder(conn: sqlite3.Connection, project: str, resource: str) -> str | None:
    row = conn.execute(
        "SELECT holder FROM resource_leases WHERE project = ? AND resource = ? AND released_at IS NULL",
        (project, resource),
    ).fetchone()
    return None if row is None else row[0]


def acquire_singleton(
    conn: sqlite3.Connection, project: str, name: str, holder: str, *, now: datetime | str | None = None,
) -> None:
    """ASES-GIT-14: "Singletons such as a shared development database are taken through a lock table in the ASES
    DB". Takes the lock "singleton:<name>" for `holder` (a card id).

    Raises ResourceBusy naming the current holder when another card has it. The same holder asking again is a
    no-op, so a retried step does not fail on its own earlier success. When two callers race, the unique index
    lets one insert win; the loser reads the row again to name the winner (or takes the lock itself if the winner
    released it in between). A blank name or holder is a ValueError: a lease with no holder can never be matched
    to a card and so could never be swept."""
    if not name.strip() or not holder.strip():
        raise ValueError("a singleton needs a non-blank name and a non-blank holder")
    resource = f"{_SINGLETON}{name}"
    for _ in range(3):
        current = _active_holder(conn, project, resource)
        if current is not None:
            if current == holder:
                return
            raise ResourceBusy(resource, current)
        try:
            conn.execute(
                "INSERT INTO resource_leases (project, resource, holder, detail, acquired_at) VALUES (?, ?, ?, ?, ?)",
                (project, resource, holder, json.dumps({"name": name}), _stamp(now)),
            )
        except sqlite3.IntegrityError:
            continue  # someone took it between our read and our insert: read again to find out who
        return
    raise LeaseError(f"could not settle who holds {resource} after three attempts")


def release_singleton(
    conn: sqlite3.Connection, project: str, name: str, holder: str, *, now: datetime | str | None = None,
) -> bool:
    """Release "singleton:<name>". True when `holder` held it and it is now free; False when it was free or is
    held by someone else, in which case it is left alone: a card must not be able to free another card's lock."""
    cursor = conn.execute(
        "UPDATE resource_leases SET released_at = ? "
        "WHERE project = ? AND resource = ? AND holder = ? AND released_at IS NULL",
        (_stamp(now), project, f"{_SINGLETON}{name}", holder),
    )
    return cursor.rowcount > 0


def holders(conn: sqlite3.Connection, project: str) -> list[dict]:
    """The active leases of a project as {resource, holder, acquired_at}, oldest first (the row id breaks a tie
    between two leases stamped in the same second)."""
    rows = conn.execute(
        "SELECT resource, holder, acquired_at FROM resource_leases "
        "WHERE project = ? AND released_at IS NULL ORDER BY acquired_at, id",
        (project,),
    ).fetchall()
    return [
        {"resource": resource, "holder": holder, "acquired_at": acquired_at}
        for resource, holder, acquired_at in rows
    ]


def sweep(
    conn: sqlite3.Connection, project: str, live_card_ids: Iterable[str], *, now: datetime | str | None = None,
) -> list[str]:
    """Release every active lease whose holder is not in live_card_ids, and return the resource names released,
    oldest first. This is the safety net behind release_card_resources: a card that is archived, fails or is
    reclaimed never calls it, and without a sweep its port block would stay leased until the project ends.

    The caller works out which cards are live (running, ready, review or scheduled, see sweep_finished). An empty
    live set releases every lease of the project, which is what a stopped project wants."""
    live = set(live_card_ids)
    stamp = _stamp(now)
    released: list[str] = []
    rows = conn.execute(
        "SELECT id, resource, holder FROM resource_leases WHERE project = ? AND released_at IS NULL "
        "ORDER BY acquired_at, id",
        (project,),
    ).fetchall()
    for lease_id, resource, holder in rows:
        if holder in live:
            continue
        cursor = conn.execute(
            "UPDATE resource_leases SET released_at = ? WHERE id = ? AND released_at IS NULL", (stamp, lease_id),
        )
        if cursor.rowcount:
            released.append(resource)
    return released


# ---------------------------------------------------------------------------------------------
# .env.ases
# ---------------------------------------------------------------------------------------------

# A value made only of these needs no quoting in a shell, in a docker compose env file or in python-dotenv.
_PLAIN_VALUE = re.compile(r"[A-Za-z0-9_@%+=:,./-]+")


def _quote_value(value: str) -> str:
    """value as it is written after KEY=, quoted only when it needs it. Anything outside the plain set (a Windows
    path has backslashes, a directory can have a space) goes in single quotes, which every reader takes
    literally. A value that itself holds a single quote or a line break cannot, so it goes in double quotes with
    the characters that stay special there escaped."""
    if _PLAIN_VALUE.fullmatch(value):
        return value
    if not any(char in value for char in "'\r\n"):
        return f"'{value}'"
    escaped = (
        value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
        .replace("\r", "\\r").replace("\n", "\\n")
    )
    return f'"{escaped}"'


def _env_file_bytes(env: CardEnv) -> bytes:
    """The whole file: a header comment, then KEY=value lines sorted by key, LF line endings and no timestamp,
    so writing the same env twice gives the same bytes. The card id in the header is reduced to printable ASCII:
    a line break in it would otherwise end the comment and inject a variable."""
    card = re.sub(r"[^\x20-\x7e]", "?", env.card_id)
    lines = [
        f"# Written by ASES for card {card} (ASES-GIT-14). Do not edit this file and do not commit it:",
        "# ASES rewrites it, and git ignores it through the repository's info/exclude file.",
        *(f"{key}={_quote_value(value)}" for key, value in sorted(env.as_env().items())),
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _exclude_file(worktree: pathlib.Path) -> pathlib.Path:
    """The exclude file git reads for this worktree, as `git rev-parse --git-path info/exclude` reports it.

    Worth knowing: in a LINKED worktree git answers with the repository's shared .git/info/exclude (checked on
    git 2.54), so the line added there applies to every worktree of the repository, the primary checkout
    included. It is also the one file this module writes outside the worktree and the temp dir. A relative answer
    (the primary checkout reports .git/info/exclude) is relative to the directory git was run in."""
    try:
        result = subprocess.run(
            [*gitexec.GIT, "-C", str(worktree), "rev-parse", "--git-path", "info/exclude"],
            capture_output=True, timeout=_GIT_TIMEOUT, env=gitexec.git_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LeaseError(f"cannot ask git where {_ascii(worktree)} keeps its exclude file: {_ascii(exc)}") from exc
    answer = result.stdout.decode("utf-8", errors="replace").strip()
    if result.returncode != 0 or not answer:
        why = result.stderr.decode("utf-8", errors="replace").strip().splitlines()
        raise LeaseError(
            f"git cannot say where {_ascii(worktree)} keeps its exclude file, so {ENV_FILE_NAME} cannot be "
            f"ignored there: {_ascii(why[0][:200]) if why else 'git gave no answer'}"
        )
    path = pathlib.Path(answer)
    return path if path.is_absolute() else worktree / path


def _ignore_env_file(worktree: pathlib.Path) -> None:
    """Make git ignore .env.ases in this worktree without touching a tracked file: append the name to the
    exclude file, unless a line for it (with or without a leading slash) is already there. The file is only ever
    appended to, so what is already in it stays byte for byte, and a missing file or directory is created."""
    exclude = _exclude_file(worktree)
    existing = exclude.read_bytes() if exclude.is_file() else b""
    # Trailing spaces are dropped by git, leading ones are part of the pattern: only rstrip is safe here.
    if any(line.rstrip() in (ENV_FILE_NAME, "/" + ENV_FILE_NAME)
           for line in existing.decode("utf-8", errors="replace").splitlines()):
        return
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with open(exclude, "ab") as handle:
        if existing and not existing.endswith(b"\n"):
            handle.write(b"\n")
        handle.write(ENV_FILE_NAME.encode("ascii") + b"\n")


def write_env_file(worktree: str | os.PathLike, env: CardEnv) -> pathlib.Path:
    """ASES-GIT-14: write the card's env to .env.ases in its worktree, create its temp directory and make git
    ignore the file. Returns the path of the file.

    git is told to ignore the file BEFORE it exists, so there is never a moment where a worker's `git add -A`
    could pick it up. That is done by appending to the exclude file (see _exclude_file), never by editing
    .gitignore, which is a tracked file and would show up in the diff and trip the touches check (ASES-GIT-13).
    Writes nothing except the file itself, the temp directory and that one exclude line. A worktree that does not
    exist, or is not a git worktree, is a LeaseError and nothing is written: a file that git cannot ignore is worse
    than none.

    Idempotent: the bytes depend only on the env, so a rewrite is identical and is skipped when the file already
    holds them. A symbolic link at the target is removed first, never followed: a worker could otherwise plant
    a link named .env.ases and have ASES overwrite whatever it points at."""
    root = pathlib.Path(os.fspath(worktree))
    if not root.is_dir():
        raise LeaseError(f"worktree {_ascii(root)} does not exist")
    _ignore_env_file(root)
    pathlib.Path(env.temp_dir).mkdir(parents=True, exist_ok=True)
    target = root / ENV_FILE_NAME
    data = _env_file_bytes(env)
    if target.is_symlink():
        target.unlink()
    elif target.is_file() and target.read_bytes() == data:
        return target
    target.write_bytes(data)
    return target


# ---------------------------------------------------------------------------------------------
# Wiring for the controller pass
# ---------------------------------------------------------------------------------------------


def provision_running_cards(
    board: str, conn: sqlite3.Connection, plan: plan_mod.Plan, *,
    allocate: Callable[[sqlite3.Connection, str, str], CardEnv] | None = None,
    kanban_list: Callable[..., list[dict]] | None = None, kanban_show: Callable[[str, str], dict] | None = None,
) -> list[str]:
    """ASES-GIT-14, ASES-GIT-01: give every RUNNING work card of this plan its env file. Returns the ids of the
    cards provisioned by this call, in plan order.

    For each work card in plan_tasks (this project only; process_merge_queue repoints work_card_id at a fix card,
    so it is the task's current card) whose status is running, the flat card from kanban_show supplies
    workspace_path. A card is provisioned when that directory exists on disk and holds no .env.ases yet: its env
    is allocated (idempotently, so a retry after a failed write reuses the same block) and written. A card with
    no workspace_path yet, one whose directory is missing, one that already has its file, and cards that are not
    in this plan are all skipped.

    BEST EFFORT, and it cannot be otherwise: Hermes, not ASES, creates the worktree and starts the worker in one
    step when it claims the card, so the worker is already running before the worktree can be seen. The file
    therefore arrives on the polling pass after that, a few seconds into the run, and the worker prompt tells it
    to read .env.ases when it is present. A worker that never looks gets no isolation from this file, which is why
    the resource leases are recorded in the ASES database regardless.

    It never raises. One card failing (no free block, a worktree that is not a git worktree, a write error) never
    stops the others: it is recorded as a provision_error event naming the card, and the card is retried on the
    next pass. A board that cannot be listed records one provision_error with no card and provisions nothing.

    The three callables are injectable for tests; when left None they resolve to allocate_card_env and the
    hermes module's functions AT CALL TIME, so a test that monkeypatches hermes.kanban_list is honoured."""
    allocate = allocate or allocate_card_env
    kanban_list = kanban_list or hermes_mod.kanban_list
    kanban_show = kanban_show or hermes_mod.kanban_show

    try:
        running = {
            card["id"] for card in kanban_list(board, status="running") if card.get("status", "running") == "running"
        }
    except Exception as exc:  # noqa: BLE001 - best effort: a Hermes hiccup must not break the pass
        events.record(conn, "provision_error", {"card_id": None, "error": f"{type(exc).__name__}: {exc}"[:300]})
        return []

    provisioned: list[str] = []
    rows = conn.execute(
        "SELECT work_card_id FROM plan_tasks WHERE project = ? AND work_card_id IS NOT NULL ORDER BY rowid",
        (plan.project,),
    ).fetchall()
    for (card_id,) in rows:
        if card_id not in running:
            continue
        try:
            workspace = kanban_show(board, card_id).get("workspace_path")
            if not workspace:
                continue
            worktree = pathlib.Path(workspace)
            if not worktree.is_dir() or (worktree / ENV_FILE_NAME).exists():
                continue
            write_env_file(worktree, allocate(conn, plan.project, card_id))
            provisioned.append(card_id)
        except Exception as exc:  # noqa: BLE001 - one card must never stop the others
            events.record(conn, "provision_error", {"card_id": card_id, "error": f"{type(exc).__name__}: {exc}"[:300]})
    return provisioned


def sweep_finished(
    board: str, conn: sqlite3.Connection, plan: plan_mod.Plan, *,
    kanban_list: Callable[..., list[dict]] | None = None, live_statuses: Iterable[str] = LIVE_STATUSES,
    now: datetime | str | None = None,
) -> list[str]:
    """Release the leases of every card that is no longer live, and return the resource names released.

    A card is live while its status is one of live_statuses (default running, ready, review, scheduled: it still
    owns its worktree). The live ids come from the whole board, not just this plan, which can only keep a lease
    that could have been released, never release one that is in use.

    Sweeping on a partial view of the board is worse than not sweeping (it would free the ports of a card that is
    running), so when any listing fails nothing is released, a lease_sweep_error event is recorded and [] comes
    back; the next pass tries again.

    Consequence worth knowing: a BLOCKED card is not live under the default statuses, so its lease is released
    while it waits. If it later resumes it still has the .env.ases it was given, and its old block may by then have
    been handed to another card. Pass live_statuses with "blocked" added if blocked cards should keep their
    resources (they then hold them until they are done or archived).

    kanban_list is injectable, and when None resolves to hermes.kanban_list at call time. A card's own "status"
    key is trusted when it has one, so a listing that ignores the status filter is not read as all-live."""
    kanban_list = kanban_list or hermes_mod.kanban_list
    live: set[str] = set()
    try:
        for status in live_statuses:
            for card in kanban_list(board, status=status):
                if card.get("status", status) == status:
                    live.add(card["id"])
    except Exception as exc:  # noqa: BLE001 - see above: no listing, no sweep
        events.record(conn, "lease_sweep_error", {"error": f"{type(exc).__name__}: {exc}"[:300]})
        return []
    return sweep(conn, plan.project, live, now=now)
