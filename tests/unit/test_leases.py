"""leases.py: per-card port blocks, derived names, singletons, .env.ases and the controller-pass wiring
(ASES-GIT-14, blueprint test 22.5).

The database is a temp sqlite file, git is real (temp repos and a real `git worktree add`), and the port probe and
the hermes functions are injected, so nothing here binds a port that matters, touches a real board or calls
Hermes. Every test runs with the working directory moved into its own temp dir: write_env_file resolves a relative
exclude path against the worktree, and this way a bug that resolved it against the cwd could only ever land in a
temp dir, never in the real ASES repository's .git."""
import dataclasses
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from ases import db, events, guards, hermes, leases, plan as plan_mod

PROJECT = "demo"
UTC = timezone.utc


@pytest.fixture(autouse=True)
def _cwd_in_tmp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "ases.db")


def _free(port):
    return True


def _git(*args, cwd):
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git("init", "-q", "-b", "integration", cwd=r)
    _git("config", "user.email", "t@t", cwd=r)
    _git("config", "user.name", "t", cwd=r)
    (r / "base.txt").write_text("base\n", encoding="utf-8")
    (r / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    _git("add", "-A", cwd=r)
    _git("commit", "-q", "-m", "init", cwd=r)
    return r


def _worktree(repo, name):
    """A real linked worktree at <repo>/.worktrees/<name>, where Hermes puts them."""
    wt = repo / ".worktrees" / name
    _git("worktree", "add", "-q", "-b", f"swarm/{name}", str(wt), cwd=repo)
    return wt


def _alloc(conn, card_id, project=PROJECT, **kwargs):
    kwargs.setdefault("port_free", _free)
    return leases.allocate_card_env(conn, project, card_id, **kwargs)


def _env(tmp_path, card_id, project=PROJECT):
    """An allocated-looking env whose temp dir is under tmp_path, so writing it never leaves the sandbox."""
    return leases.CardEnv(
        card_id=card_id, port_base=42000, port_count=2, ports=(42000, 42001),
        compose_project=f"ases-{project}-{card_id}", db_name=f"ases_{project}_{card_id}",
        temp_dir=str(tmp_path / "tmp" / project / card_id),
    )


def _rows(conn):
    """Every lease row as (holder, resource, active), oldest first."""
    cur = conn.execute("SELECT holder, resource, released_at IS NULL FROM resource_leases ORDER BY id")
    return [(holder, resource, bool(active)) for holder, resource, active in cur.fetchall()]


def _event_payloads(conn, kind):
    return [json.loads(row["payload"]) for row in events.recent(conn, limit=100) if row["kind"] == kind]


def _read_env(path):
    values = {}
    for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            values[key] = value.strip("'")
    return values


# ---------------------------------------------------------------------------------------------
# CardEnv and the errors
# ---------------------------------------------------------------------------------------------


def _sample_env(**over):
    values = dict(
        card_id="t_1", port_base=42000, port_count=3, ports=(42000, 42001, 42002),
        compose_project="ases-demo-t-1", db_name="ases_demo_t_1", temp_dir="/tmp/ases/demo/t_1",
    )
    values.update(over)
    return leases.CardEnv(**values)


def test_as_env_carries_every_variable_a_card_needs():
    assert _sample_env().as_env() == {
        "ASES_PORT_BASE": "42000", "ASES_PORT_COUNT": "3",
        "ASES_PORT_0": "42000", "ASES_PORT_1": "42001", "ASES_PORT_2": "42002",
        "COMPOSE_PROJECT_NAME": "ases-demo-t-1", "ASES_DB_NAME": "ases_demo_t_1",
        "ASES_TMPDIR": "/tmp/ases/demo/t_1", "TMPDIR": "/tmp/ases/demo/t_1",
        "TEMP": "/tmp/ases/demo/t_1", "TMP": "/tmp/ases/demo/t_1",
    }


def test_as_env_has_a_port_variable_for_each_port_and_no_more():
    env = _sample_env(port_count=1, ports=(42000,)).as_env()

    assert "ASES_PORT_0" in env and "ASES_PORT_1" not in env


def test_card_env_is_frozen():
    env = _sample_env()

    with pytest.raises(dataclasses.FrozenInstanceError):
        env.port_base = 1


def test_resource_busy_is_a_lease_error_that_names_the_holder_and_the_resource():
    err = leases.ResourceBusy("singleton:dev-db", "t_1")

    assert isinstance(err, leases.LeaseError)
    assert (err.resource, err.holder) == ("singleton:dev-db", "t_1")
    assert "t_1" in str(err) and "singleton:dev-db" in str(err)


# ---------------------------------------------------------------------------------------------
# Derived names
# ---------------------------------------------------------------------------------------------


def test_a_hermes_style_card_gets_the_documented_names(conn, tmp_path):
    env = _alloc(conn, "t_1a2b3c4d", temp_root=tmp_path)

    assert env.compose_project == "ases-demo-t-1a2b3c4d"
    assert env.db_name == "ases_demo_t_1a2b3c4d"
    assert env.temp_dir == str(tmp_path / "ases" / "demo" / "t_1a2b3c4d")


@pytest.mark.parametrize("project,card", [
    ("My Project!", "T 1 / weird"),
    ("Caf\u00e9 \u00dcber", "\u00e9\u00e8 card"),
    ("UPPER_case.name", "CARD..ID"),
    ("a" * 200, "b" * 200),
    ("--x--", "__y__"),
    ("p", "../../etc/passwd"),
    ("***", "###"),
])
def test_derived_names_are_lowercase_short_and_use_only_the_allowed_characters(conn, tmp_path, project, card):
    env = _alloc(conn, card, project=project, temp_root=tmp_path)

    assert re.fullmatch(r"ases-[a-z0-9]+(-[a-z0-9]+)*", env.compose_project), env.compose_project
    assert len(env.compose_project) <= 63
    assert re.fullmatch(r"ases_[a-z0-9]+(_[a-z0-9]+)*", env.db_name), env.db_name
    assert len(env.db_name) <= 63
    relative = pathlib.Path(env.temp_dir).relative_to(tmp_path)
    assert len(relative.parts) == 3 and relative.parts[0] == "ases"
    assert all(re.fullmatch(r"[a-z0-9_-]+", part) for part in relative.parts[1:]), relative


def test_the_same_card_gets_the_same_names_from_a_fresh_database(tmp_path):
    first = _alloc(db.connect(tmp_path / "one.db"), "T 1 / weird", project="My Project", temp_root=tmp_path)
    second = _alloc(db.connect(tmp_path / "two.db"), "T 1 / weird", project="My Project", temp_root=tmp_path)

    assert first == second


def test_names_of_exactly_63_characters_are_left_alone_and_64_are_cut_with_a_hash(conn, tmp_path):
    fits = _alloc(conn, "c" * 56, project="p", temp_root=tmp_path)
    over = _alloc(conn, "c" * 57, project="p", temp_root=tmp_path)

    assert fits.compose_project == "ases-p-" + "c" * 56 and len(fits.compose_project) == 63
    assert fits.db_name == "ases_p_" + "c" * 56 and len(fits.db_name) == 63
    assert re.fullmatch(r"ases-p-c{47}-[0-9a-f]{8}", over.compose_project)
    assert re.fullmatch(r"ases_p_c{47}_[0-9a-f]{8}", over.db_name)


def test_two_long_cards_that_differ_only_at_the_tail_stay_distinct(conn, tmp_path):
    a = _alloc(conn, "c" * 100 + "a", temp_root=tmp_path)
    b = _alloc(conn, "c" * 100 + "b", temp_root=tmp_path)

    assert len(a.compose_project) == len(b.compose_project) == 63
    assert len(a.db_name) == len(b.db_name) == 63
    assert a.compose_project != b.compose_project and a.db_name != b.db_name


def test_a_cut_name_never_ends_a_part_with_a_separator_before_the_hash(conn, tmp_path):
    # the 54 character head of this name ends in the dash that follows "aaaa...", which must not double up
    env = _alloc(conn, "a" * 46 + "-" + "b" * 30, project="p", temp_root=tmp_path)

    assert "--" not in env.compose_project and "__" not in env.db_name
    assert len(env.compose_project) <= 63


@pytest.mark.parametrize("card", ["../../evil", "..", "a/../../b", "C:\\evil", "/abs/path", "a\\..\\b"])
def test_a_card_id_cannot_climb_out_of_the_temp_root(conn, tmp_path, card):
    root = tmp_path / "root"

    env = _alloc(conn, card, temp_root=root)

    assert pathlib.Path(env.temp_dir).parent == root / "ases" / "demo"


def test_the_card_directory_trims_edge_separators_but_keeps_inner_underscores_and_dashes(conn, tmp_path):
    env = _alloc(conn, "-_Task_1-a_-", temp_root=tmp_path)

    assert pathlib.Path(env.temp_dir).name == "task_1-a"
    assert env.compose_project == "ases-demo-task-1-a"
    assert env.db_name == "ases_demo_task_1_a"


def test_ids_made_only_of_dropped_characters_still_get_distinct_names(conn, tmp_path):
    a = _alloc(conn, "###", temp_root=tmp_path)
    b = _alloc(conn, "@@@", temp_root=tmp_path)

    assert re.fullmatch(r"ases-demo-x[0-9a-f]{8}", a.compose_project)
    assert re.fullmatch(r"ases_demo_x[0-9a-f]{8}", a.db_name)
    assert a.compose_project != b.compose_project
    assert a.db_name != b.db_name
    assert a.temp_dir != b.temp_dir


def test_temp_root_defaults_to_the_system_temp_directory(conn):
    import tempfile

    env = _alloc(conn, "t_1")

    assert pathlib.Path(env.temp_dir) == pathlib.Path(tempfile.gettempdir()) / "ases" / "demo" / "t_1"


# ---------------------------------------------------------------------------------------------
# Port blocks
# ---------------------------------------------------------------------------------------------


def test_three_cards_get_three_distinct_port_blocks_and_compose_projects(conn, tmp_path):
    envs = [_alloc(conn, f"t_{n}", temp_root=tmp_path) for n in (1, 2, 3)]

    assert [e.port_base for e in envs] == [42000, 42010, 42020]
    assert all(e.port_count == 10 and len(e.ports) == 10 for e in envs)
    all_ports = [port for e in envs for port in e.ports]
    assert len(set(all_ports)) == len(all_ports) == 30
    assert len({e.compose_project for e in envs}) == 3
    assert len({e.db_name for e in envs}) == 3
    assert len({e.temp_dir for e in envs}) == 3
    assert [h["resource"] for h in leases.holders(conn, PROJECT)] == ["port-block:0", "port-block:1", "port-block:2"]


def test_block_n_starts_at_base_port_plus_n_block_sizes(conn, tmp_path):
    a = _alloc(conn, "a", base_port=50000, block_size=4, temp_root=tmp_path)
    b = _alloc(conn, "b", base_port=50000, block_size=4, temp_root=tmp_path)

    assert (a.port_base, a.port_count, a.ports) == (50000, 4, (50000, 50001, 50002, 50003))
    assert (b.port_base, b.port_count, b.ports) == (50004, 4, (50004, 50005, 50006, 50007))


def test_the_same_card_gets_the_same_env_back_and_holds_exactly_one_block(conn, tmp_path):
    first = _alloc(conn, "t_1", temp_root=tmp_path)
    second = _alloc(conn, "t_1", temp_root=tmp_path)

    assert first == second
    assert _rows(conn) == [("t_1", "port-block:0", True)]


def test_a_card_that_holds_a_block_gets_it_back_even_when_its_port_is_busy_now(conn, tmp_path):
    first = _alloc(conn, "t_1", temp_root=tmp_path)

    again = leases.allocate_card_env(conn, PROJECT, "t_1", port_free=lambda port: False, temp_root=tmp_path)

    assert again == first


def test_a_repeat_call_with_other_parameters_returns_what_was_handed_out(conn, tmp_path):
    first = _alloc(conn, "t_1", temp_root=tmp_path)

    again = _alloc(conn, "t_1", temp_root=tmp_path / "elsewhere", base_port=50000, block_size=4)

    assert again == first


@pytest.mark.parametrize("detail", [None, "", "not json", "{}", '{"ports": 5}'])
def test_a_lease_without_a_readable_record_is_rebuilt_from_its_block_number(conn, tmp_path, detail):
    conn.execute(
        "INSERT INTO resource_leases (project, resource, holder, detail, acquired_at) "
        "VALUES (?, 'port-block:3', 't_1', ?, 'x')", (PROJECT, detail),
    )

    env = _alloc(conn, "t_1", temp_root=tmp_path, base_port=43000, block_size=5)

    assert (env.port_base, env.port_count, env.ports) == (43015, 5, tuple(range(43015, 43020)))
    assert env.compose_project == "ases-demo-t-1"


def test_a_singleton_the_card_holds_is_not_mistaken_for_its_port_block(conn, tmp_path):
    leases.acquire_singleton(conn, PROJECT, "dev-db", "t_1")

    env = _alloc(conn, "t_1", temp_root=tmp_path)

    assert env.port_base == 42000
    assert {h["resource"] for h in leases.holders(conn, PROJECT)} == {"singleton:dev-db", "port-block:0"}


def test_a_lease_named_like_a_block_but_not_numbered_occupies_no_block(conn, tmp_path):
    for resource, holder in (("port-block:abc", "t_1"), ("port-block:", "x"), ("port-block:0x", "y")):
        conn.execute(
            "INSERT INTO resource_leases (project, resource, holder, acquired_at) VALUES (?, ?, ?, 'x')",
            (PROJECT, resource, holder),
        )

    env = _alloc(conn, "t_1", temp_root=tmp_path)

    assert env.port_base == 42000


def test_a_block_whose_first_port_is_busy_is_skipped_and_never_leased(conn, tmp_path):
    env = _alloc(conn, "t_1", temp_root=tmp_path, port_free=lambda port: port != 42000)

    assert env.port_base == 42010
    assert _rows(conn) == [("t_1", "port-block:1", True)]


def test_only_the_first_port_of_a_block_is_probed_and_leased_blocks_are_not_probed(conn, tmp_path):
    probed = []

    def probe(port):
        probed.append(port)
        return True

    _alloc(conn, "t_1", temp_root=tmp_path, port_free=probe)
    assert probed == [42000]

    _alloc(conn, "t_2", temp_root=tmp_path, port_free=probe)
    assert probed == [42000, 42010]  # block 0 is leased, so it is skipped without a probe


def test_a_busy_port_inside_a_block_does_not_matter_only_its_first_port_does(conn, tmp_path):
    env = _alloc(conn, "t_1", temp_root=tmp_path, port_free=lambda port: port != 42001)

    assert env.port_base == 42000


def test_when_every_block_is_taken_allocation_raises_a_lease_error_and_leases_nothing(conn, tmp_path):
    _alloc(conn, "a", max_blocks=2, temp_root=tmp_path)
    _alloc(conn, "b", max_blocks=2, temp_root=tmp_path)

    with pytest.raises(leases.LeaseError, match="no free port block") as raised:
        _alloc(conn, "c", max_blocks=2, temp_root=tmp_path)

    assert not isinstance(raised.value, leases.ResourceBusy)
    assert [h["holder"] for h in leases.holders(conn, PROJECT)] == ["a", "b"]


def test_blocks_whose_first_port_is_busy_can_exhaust_the_supply_too(conn, tmp_path):
    with pytest.raises(leases.LeaseError, match="no free port block"):
        _alloc(conn, "a", max_blocks=3, port_free=lambda port: False, temp_root=tmp_path)

    assert leases.holders(conn, PROJECT) == []


def test_releasing_a_card_frees_its_block_for_the_next_card_and_keeps_the_history(conn, tmp_path):
    a = _alloc(conn, "a", temp_root=tmp_path)
    _alloc(conn, "b", temp_root=tmp_path)
    assert leases.release_card_resources(conn, PROJECT, "a") == 1

    c = _alloc(conn, "c", temp_root=tmp_path)

    assert c.port_base == a.port_base  # the lowest free block is reused
    assert _rows(conn) == [("a", "port-block:0", False), ("b", "port-block:1", True), ("c", "port-block:0", True)]


def test_a_racing_insert_falls_through_to_the_next_block(conn, tmp_path):
    raced = []

    def probe(port):
        if port == 42000 and not raced:  # a rival takes block 0 after we chose it and before we insert
            raced.append(True)
            conn.execute(
                "INSERT INTO resource_leases (project, resource, holder, acquired_at) "
                "VALUES (?, 'port-block:0', 'rival', 'x')", (PROJECT,),
            )
        return True

    env = _alloc(conn, "t_1", temp_root=tmp_path, port_free=probe)

    assert env.port_base == 42010
    assert _rows(conn) == [("rival", "port-block:0", True), ("t_1", "port-block:1", True)]


def test_a_racing_insert_by_the_same_card_returns_its_own_block_and_never_a_second_one(conn, tmp_path):
    winner = []

    def probe(port):
        if not winner:  # a second controller allocates for this very card between our read and our insert
            winner.append(leases.allocate_card_env(conn, PROJECT, "t_1", port_free=_free, temp_root=tmp_path))
        return True

    env = _alloc(conn, "t_1", temp_root=tmp_path, port_free=probe)

    assert env == winner[0]
    assert _rows(conn) == [("t_1", "port-block:0", True)]


def test_blocks_are_counted_per_project(conn, tmp_path):
    a = _alloc(conn, "t_1", project="p1", temp_root=tmp_path)
    b = _alloc(conn, "t_1", project="p2", temp_root=tmp_path)

    # the unique index is per project, so two projects sharing one database are each handed block 0
    assert a.port_base == b.port_base == 42000
    assert a.compose_project != b.compose_project


def test_the_lease_records_the_values_that_were_handed_out(conn, tmp_path):
    env = _alloc(conn, "t_1", temp_root=tmp_path)

    detail = conn.execute("SELECT detail FROM resource_leases").fetchone()[0]

    assert json.loads(detail) == {
        "card_id": "t_1", "port_base": 42000, "port_count": 10, "ports": list(range(42000, 42010)),
        "compose_project": env.compose_project, "db_name": env.db_name, "temp_dir": env.temp_dir,
    }


def test_only_blocks_whose_last_port_exists_are_offered(conn, tmp_path):
    envs = [_alloc(conn, f"c{n}", base_port=65500, block_size=10, temp_root=tmp_path) for n in range(3)]

    assert [e.port_base for e in envs] == [65500, 65510, 65520]
    assert max(envs[-1].ports) == 65529
    with pytest.raises(leases.LeaseError):
        _alloc(conn, "c3", base_port=65500, block_size=10, temp_root=tmp_path)  # block 3 would end at 65539


def test_a_block_ending_exactly_on_port_65535_is_allowed_and_one_port_further_is_not(conn, tmp_path):
    env = _alloc(conn, "fits", base_port=65526, block_size=10, temp_root=tmp_path)
    assert (env.ports[0], env.ports[-1]) == (65526, 65535)

    with pytest.raises(leases.LeaseError):
        _alloc(conn, "over", base_port=65527, block_size=10, temp_root=tmp_path)


@pytest.mark.parametrize("kwargs", [{"base_port": 70000}, {"max_blocks": 0}, {"max_blocks": -4}])
def test_a_configuration_with_no_block_to_offer_raises_a_lease_error(conn, tmp_path, kwargs):
    with pytest.raises(leases.LeaseError, match="no free port block"):
        _alloc(conn, "t_1", temp_root=tmp_path, **kwargs)


@pytest.mark.parametrize("kwargs", [{"block_size": 0}, {"block_size": -3}, {"base_port": 0}, {"base_port": -1}])
def test_a_nonsense_block_size_or_base_port_is_a_value_error(conn, tmp_path, kwargs):
    with pytest.raises(ValueError):
        _alloc(conn, "t_1", temp_root=tmp_path, **kwargs)

    assert leases.holders(conn, PROJECT) == []


def test_acquired_at_follows_the_injected_clock(conn, tmp_path):
    _alloc(conn, "t_1", temp_root=tmp_path, now=datetime(2026, 9, 19, 12, 30, 45, tzinfo=UTC))

    assert leases.holders(conn, PROJECT)[0]["acquired_at"] == "2026-09-19T12:30:45+00:00"


def test_a_naive_clock_is_taken_as_utc_an_aware_one_is_converted_and_a_string_is_kept(conn):
    leases.acquire_singleton(conn, PROJECT, "naive", "a", now=datetime(2026, 9, 19, 12, 0, 0))
    leases.acquire_singleton(
        conn, PROJECT, "aware", "b", now=datetime(2026, 9, 19, 14, 0, 0, tzinfo=timezone(timedelta(hours=2))),
    )
    leases.acquire_singleton(conn, PROJECT, "text", "c", now="2000-01-01T00:00:00+00:00")

    stamps = {h["holder"]: h["acquired_at"] for h in leases.holders(conn, PROJECT)}

    assert stamps == {
        "a": "2026-09-19T12:00:00+00:00", "b": "2026-09-19T12:00:00+00:00", "c": "2000-01-01T00:00:00+00:00",
    }


def test_without_a_clock_acquired_at_is_the_current_utc_time_to_the_second(conn):
    before = datetime.now(UTC).replace(microsecond=0)
    leases.acquire_singleton(conn, PROJECT, "x", "a")

    stamp = leases.holders(conn, PROJECT)[0]["acquired_at"]

    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00", stamp)
    assert before <= datetime.fromisoformat(stamp) <= datetime.now(UTC) + timedelta(seconds=1)


# ---------------------------------------------------------------------------------------------
# The real port probe
# ---------------------------------------------------------------------------------------------


def test_is_port_free_is_false_while_something_listens_on_the_port_and_true_once_it_stops():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        assert leases.is_port_free(port) is False
    finally:
        listener.close()

    assert leases.is_port_free(port) is True


@pytest.mark.parametrize("port", [0, -1, 65536, 70000, "80", None, 3.5])
def test_is_port_free_never_raises_and_is_false_for_an_invalid_port(port):
    assert leases.is_port_free(port) is False


# ---------------------------------------------------------------------------------------------
# Releasing
# ---------------------------------------------------------------------------------------------


def test_release_frees_the_block_and_every_singleton_of_the_card_and_counts_them(conn, tmp_path):
    _alloc(conn, "a", temp_root=tmp_path)
    leases.acquire_singleton(conn, PROJECT, "dev-db", "a")
    leases.acquire_singleton(conn, PROJECT, "cache", "a")
    _alloc(conn, "b", temp_root=tmp_path)

    assert leases.release_card_resources(conn, PROJECT, "a") == 3

    assert [h["holder"] for h in leases.holders(conn, PROJECT)] == ["b"]


def test_release_is_idempotent_and_keeps_the_first_release_time(conn, tmp_path):
    _alloc(conn, "a", temp_root=tmp_path)
    first = datetime(2026, 9, 19, 10, 0, 0, tzinfo=UTC)

    assert leases.release_card_resources(conn, PROJECT, "a", now=first) == 1
    assert leases.release_card_resources(conn, PROJECT, "a", now=first + timedelta(hours=1)) == 0

    assert conn.execute("SELECT released_at FROM resource_leases").fetchone()[0] == "2026-09-19T10:00:00+00:00"


def test_release_of_a_card_with_nothing_leased_returns_zero(conn):
    assert leases.release_card_resources(conn, PROJECT, "nobody") == 0


def test_release_only_touches_the_named_project(conn, tmp_path):
    _alloc(conn, "a", project="p1", temp_root=tmp_path)
    _alloc(conn, "a", project="p2", temp_root=tmp_path)

    assert leases.release_card_resources(conn, "p1", "a") == 1

    assert leases.holders(conn, "p1") == []
    assert [h["holder"] for h in leases.holders(conn, "p2")] == ["a"]


# ---------------------------------------------------------------------------------------------
# Singletons
# ---------------------------------------------------------------------------------------------


def test_a_second_holder_of_a_singleton_gets_resource_busy_naming_the_first(conn):
    leases.acquire_singleton(conn, PROJECT, "dev-db", "t_1")

    with pytest.raises(leases.ResourceBusy) as raised:
        leases.acquire_singleton(conn, PROJECT, "dev-db", "t_2")

    assert raised.value.holder == "t_1"
    assert raised.value.resource == "singleton:dev-db"
    assert "t_1" in str(raised.value)
    assert [(h["resource"], h["holder"]) for h in leases.holders(conn, PROJECT)] == [("singleton:dev-db", "t_1")]


def test_the_same_holder_taking_a_singleton_again_is_a_no_op(conn):
    leases.acquire_singleton(conn, PROJECT, "dev-db", "t_1", now=datetime(2026, 9, 19, 9, 0, 0, tzinfo=UTC))
    leases.acquire_singleton(conn, PROJECT, "dev-db", "t_1", now=datetime(2026, 9, 19, 9, 30, 0, tzinfo=UTC))

    assert _rows(conn) == [("t_1", "singleton:dev-db", True)]
    assert leases.holders(conn, PROJECT)[0]["acquired_at"] == "2026-09-19T09:00:00+00:00"


def test_releasing_a_singleton_needs_its_holder(conn):
    leases.acquire_singleton(conn, PROJECT, "dev-db", "t_1")

    assert leases.release_singleton(conn, PROJECT, "dev-db", "t_2") is False
    assert [h["holder"] for h in leases.holders(conn, PROJECT)] == ["t_1"]  # not freed by a stranger
    assert leases.release_singleton(conn, PROJECT, "never-taken", "t_1") is False
    assert leases.release_singleton(conn, PROJECT, "dev-db", "t_1") is True
    assert leases.release_singleton(conn, PROJECT, "dev-db", "t_1") is False  # already free
    assert leases.holders(conn, PROJECT) == []


def test_a_released_singleton_can_be_taken_by_someone_else(conn):
    leases.acquire_singleton(conn, PROJECT, "dev-db", "t_1")
    leases.release_singleton(conn, PROJECT, "dev-db", "t_1")

    leases.acquire_singleton(conn, PROJECT, "dev-db", "t_2")

    assert _rows(conn) == [("t_1", "singleton:dev-db", False), ("t_2", "singleton:dev-db", True)]


def test_release_singleton_stamps_the_injected_clock(conn):
    leases.acquire_singleton(conn, PROJECT, "dev-db", "t_1")

    leases.release_singleton(conn, PROJECT, "dev-db", "t_1", now=datetime(2026, 9, 19, 8, 0, 0, tzinfo=UTC))

    assert conn.execute("SELECT released_at FROM resource_leases").fetchone()[0] == "2026-09-19T08:00:00+00:00"


def test_singletons_are_separate_per_name_and_per_project(conn):
    leases.acquire_singleton(conn, PROJECT, "dev-db", "t_1")

    leases.acquire_singleton(conn, PROJECT, "cache", "t_2")  # another name
    leases.acquire_singleton(conn, "other", "dev-db", "t_2")  # another project

    assert len(leases.holders(conn, PROJECT)) == 2 and len(leases.holders(conn, "other")) == 1


@pytest.mark.parametrize("name,holder", [("", "t_1"), ("   ", "t_1"), ("dev-db", ""), ("dev-db", "  ")])
def test_a_singleton_needs_a_non_blank_name_and_holder(conn, name, holder):
    with pytest.raises(ValueError):
        leases.acquire_singleton(conn, PROJECT, name, holder)

    assert leases.holders(conn, PROJECT) == []


class _RivalOnInsert:
    """A connection whose first `times` inserts into resource_leases lose a race: just before the real insert goes
    through, a rival inserts the same resource, so the real insert hits the unique index and raises. With
    rival_lets_go the rival releases again right after the failure, as if it had finished in between."""

    def __init__(self, conn, resource, rival, *, times=1, rival_lets_go=False):
        self._conn, self._resource, self._rival = conn, resource, rival
        self._left, self._lets_go = times, rival_lets_go

    def execute(self, sql, params=()):
        if self._left and sql.lstrip().upper().startswith("INSERT INTO RESOURCE_LEASES"):
            self._left -= 1
            self._conn.execute(
                "INSERT INTO resource_leases (project, resource, holder, acquired_at) VALUES (?, ?, ?, 'x')",
                (PROJECT, self._resource, self._rival),
            )
            try:
                return self._conn.execute(sql, params)
            finally:
                if self._lets_go:
                    self._conn.execute(
                        "UPDATE resource_leases SET released_at = 'y' WHERE holder = ? AND released_at IS NULL",
                        (self._rival,),
                    )
        return self._conn.execute(sql, params)


def test_a_lost_race_for_a_singleton_names_the_winner(conn):
    racy = _RivalOnInsert(conn, "singleton:dev-db", "rival")

    with pytest.raises(leases.ResourceBusy) as raised:
        leases.acquire_singleton(racy, PROJECT, "dev-db", "t_1")

    assert raised.value.holder == "rival"


def test_a_lost_race_against_the_same_holder_is_not_an_error(conn):
    racy = _RivalOnInsert(conn, "singleton:dev-db", "t_1")

    leases.acquire_singleton(racy, PROJECT, "dev-db", "t_1")

    assert [h["holder"] for h in leases.holders(conn, PROJECT)] == ["t_1"]


def test_a_lost_race_is_retried_when_the_winner_has_already_let_go(conn):
    racy = _RivalOnInsert(conn, "singleton:dev-db", "rival", times=2, rival_lets_go=True)

    leases.acquire_singleton(racy, PROJECT, "dev-db", "t_1")  # loses twice, wins on the third attempt

    assert [h["holder"] for h in leases.holders(conn, PROJECT)] == ["t_1"]


def test_a_singleton_that_keeps_losing_the_race_gives_up_with_a_lease_error(conn):
    racy = _RivalOnInsert(conn, "singleton:dev-db", "rival", times=3, rival_lets_go=True)

    with pytest.raises(leases.LeaseError, match="could not settle"):
        leases.acquire_singleton(racy, PROJECT, "dev-db", "t_1")

    assert not any(h["holder"] == "t_1" for h in leases.holders(conn, PROJECT))


# ---------------------------------------------------------------------------------------------
# holders and sweep
# ---------------------------------------------------------------------------------------------


def test_holders_lists_only_active_leases_with_three_keys_oldest_first(conn):
    stamp = lambda second: datetime(2026, 9, 19, 12, 0, second, tzinfo=UTC)  # noqa: E731
    leases.acquire_singleton(conn, PROJECT, "b", "c2", now=stamp(5))
    leases.acquire_singleton(conn, PROJECT, "a", "c1", now=stamp(1))
    leases.acquire_singleton(conn, PROJECT, "z", "c3", now=stamp(1))  # the same second as "a", a later row
    leases.acquire_singleton(conn, PROJECT, "gone", "c4", now=stamp(2))
    leases.release_singleton(conn, PROJECT, "gone", "c4")

    assert leases.holders(conn, PROJECT) == [
        {"resource": "singleton:a", "holder": "c1", "acquired_at": "2026-09-19T12:00:01+00:00"},
        {"resource": "singleton:z", "holder": "c3", "acquired_at": "2026-09-19T12:00:01+00:00"},
        {"resource": "singleton:b", "holder": "c2", "acquired_at": "2026-09-19T12:00:05+00:00"},
    ]


def test_holders_is_scoped_to_the_project(conn):
    leases.acquire_singleton(conn, "p1", "x", "a")
    leases.acquire_singleton(conn, "p2", "x", "b")

    assert [h["holder"] for h in leases.holders(conn, "p1")] == ["a"]
    assert leases.holders(conn, "nobody") == []


def test_sweep_releases_only_dead_holders_and_names_what_it_released_oldest_first(conn, tmp_path):
    for card in ("a", "b", "c"):
        _alloc(conn, card, temp_root=tmp_path)
    leases.acquire_singleton(conn, PROJECT, "dev-db", "b")

    released = leases.sweep(conn, PROJECT, {"a", "c"})

    assert released == ["port-block:1", "singleton:dev-db"]
    assert [h["holder"] for h in leases.holders(conn, PROJECT)] == ["a", "c"]


def test_sweep_with_no_live_cards_releases_everything_and_a_second_sweep_finds_nothing(conn, tmp_path):
    _alloc(conn, "a", temp_root=tmp_path)
    _alloc(conn, "b", temp_root=tmp_path)

    assert leases.sweep(conn, PROJECT, set()) == ["port-block:0", "port-block:1"]
    assert leases.sweep(conn, PROJECT, set()) == []
    assert leases.holders(conn, PROJECT) == []


def test_sweep_accepts_any_iterable_including_a_generator(conn, tmp_path):
    for card in ("a", "b"):
        _alloc(conn, card, temp_root=tmp_path)

    released = leases.sweep(conn, PROJECT, (card for card in ["a"]))

    assert released == ["port-block:1"]


def test_sweep_leaves_other_projects_alone_and_stamps_the_injected_clock(conn, tmp_path):
    _alloc(conn, "a", project="p1", temp_root=tmp_path)
    _alloc(conn, "a", project="p2", temp_root=tmp_path)

    assert leases.sweep(conn, "p1", set(), now=datetime(2026, 9, 19, 7, 0, 0, tzinfo=UTC)) == ["port-block:0"]

    assert [h["holder"] for h in leases.holders(conn, "p2")] == ["a"]
    released_at = conn.execute("SELECT released_at FROM resource_leases WHERE project = 'p1'").fetchone()[0]
    assert released_at == "2026-09-19T07:00:00+00:00"


# ---------------------------------------------------------------------------------------------
# write_env_file
# ---------------------------------------------------------------------------------------------

_PLAIN = re.compile(r"[A-Za-z0-9_@%+=:,./-]+")


def _exclude_path(worktree):
    answer = _git("rev-parse", "--git-path", "info/exclude", cwd=worktree).stdout.strip()
    path = pathlib.Path(answer)
    return path if path.is_absolute() else pathlib.Path(worktree) / path


def _sample_file(temp_dir):
    return _sample_env(temp_dir=temp_dir)


def test_the_env_file_is_a_header_then_sorted_key_value_lines(repo, tmp_path):
    wt = _worktree(repo, "t_1")
    temp_dir = (tmp_path / "tmp" / "t_1").as_posix()
    if not _PLAIN.fullmatch(temp_dir):
        pytest.skip("this temp directory needs quoting, which test_a_windows_style_temp_dir_is_single_quoted covers")

    path = leases.write_env_file(wt, _sample_file(temp_dir))

    assert path == wt / ".env.ases"
    assert path.read_bytes() == (
        "# Written by ASES for card t_1 (ASES-GIT-14). Do not edit this file and do not commit it:\n"
        "# ASES rewrites it, and git ignores it through the repository's info/exclude file.\n"
        "ASES_DB_NAME=ases_demo_t_1\n"
        "ASES_PORT_0=42000\n"
        "ASES_PORT_1=42001\n"
        "ASES_PORT_2=42002\n"
        "ASES_PORT_BASE=42000\n"
        "ASES_PORT_COUNT=3\n"
        f"ASES_TMPDIR={temp_dir}\n"
        "COMPOSE_PROJECT_NAME=ases-demo-t-1\n"
        f"TEMP={temp_dir}\n"
        f"TMP={temp_dir}\n"
        f"TMPDIR={temp_dir}\n"
    ).encode("utf-8")


@pytest.mark.skipif(os.sep != "\\", reason="a backslash path only exists on Windows")
def test_a_windows_style_temp_dir_is_single_quoted(repo, tmp_path):
    wt = _worktree(repo, "t_1")
    temp_dir = str(tmp_path / "tmp" / "t_1")

    values = _read_env(leases.write_env_file(wt, _sample_file(temp_dir)))

    text = (wt / ".env.ases").read_text(encoding="utf-8")
    assert f"ASES_TMPDIR='{temp_dir}'\n" in text
    assert values["ASES_TMPDIR"] == temp_dir


def test_the_env_file_uses_unix_line_endings_and_ends_with_a_newline(repo, tmp_path):
    wt = _worktree(repo, "t_1")

    data = leases.write_env_file(wt, _env(tmp_path, "t_1")).read_bytes()

    assert b"\r" not in data and data.endswith(b"\n") and not data.endswith(b"\n\n")


def test_rewriting_an_env_file_gives_identical_bytes_and_does_not_touch_the_file(repo, tmp_path):
    wt = _worktree(repo, "t_1")
    env = _env(tmp_path, "t_1")
    path = leases.write_env_file(wt, env)
    before = path.read_bytes()
    os.utime(path, ns=(10**18, 10**18))  # an old timestamp: a rewrite would replace it with the current time

    again = leases.write_env_file(wt, env)

    assert again == path and path.read_bytes() == before
    assert path.stat().st_mtime_ns == 10**18


def test_an_edited_env_file_is_restored(repo, tmp_path):
    wt = _worktree(repo, "t_1")
    env = _env(tmp_path, "t_1")
    path = leases.write_env_file(wt, env)
    good = path.read_bytes()
    path.write_text("ASES_PORT_BASE=1\n", encoding="utf-8")

    leases.write_env_file(wt, env)

    assert path.read_bytes() == good


def test_git_ignores_the_env_file_and_no_tracked_file_is_touched(repo, tmp_path):
    wt = _worktree(repo, "t_1")
    tracked = {name: (wt / name).read_bytes() for name in ("base.txt", "tracked.txt")}

    leases.write_env_file(wt, _env(tmp_path, "t_1"))

    assert _git("status", "--porcelain", cwd=wt).stdout == ""
    _git("check-ignore", "-q", ".env.ases", cwd=wt)  # exits 0 only when git ignores it
    assert _git("diff", "--stat", cwd=wt).stdout == ""
    assert not (wt / ".gitignore").exists()
    assert {name: (wt / name).read_bytes() for name in tracked} == tracked
    assert _exclude_path(wt).read_text(encoding="utf-8").splitlines().count(".env.ases") == 1


def test_writing_twice_leaves_exactly_one_ignore_line(repo, tmp_path):
    wt = _worktree(repo, "t_1")
    env = _env(tmp_path, "t_1")

    leases.write_env_file(wt, env)
    leases.write_env_file(wt, env)
    leases.write_env_file(wt, _env(tmp_path, "t_1"))

    assert _exclude_path(wt).read_text(encoding="utf-8").splitlines().count(".env.ases") == 1


def test_the_primary_checkouts_relative_exclude_path_is_resolved_against_the_worktree(repo, tmp_path):
    leases.write_env_file(repo, _env(tmp_path, "t_1"))  # cwd is elsewhere: a relative answer must not follow it

    assert (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8").splitlines().count(".env.ases") == 1
    assert _git("status", "--porcelain", cwd=repo).stdout == ""
    assert not (tmp_path / ".git").exists()


def test_an_exclude_file_is_only_appended_to_and_a_missing_final_newline_is_added(repo, tmp_path):
    wt = _worktree(repo, "t_1")
    exclude = _exclude_path(wt)
    exclude.write_bytes(b"first\r\nsecond")

    leases.write_env_file(wt, _env(tmp_path, "t_1"))

    assert exclude.read_bytes() == b"first\r\nsecond\n.env.ases\n"


@pytest.mark.parametrize("existing", [b".env.ases\n", b"/.env.ases\n", b".env.ases  \n", b"a\n.env.ases"])
def test_an_existing_ignore_line_is_not_duplicated(repo, tmp_path, existing):
    wt = _worktree(repo, "t_1")
    exclude = _exclude_path(wt)
    exclude.write_bytes(existing)

    leases.write_env_file(wt, _env(tmp_path, "t_1"))

    assert exclude.read_bytes() == existing


def test_a_line_that_only_looks_like_the_pattern_does_not_count(repo, tmp_path):
    wt = _worktree(repo, "t_1")
    exclude = _exclude_path(wt)
    exclude.write_bytes(b" .env.ases\n.env.ases.bak\n# .env.ases\n")  # a leading space is part of a git pattern

    leases.write_env_file(wt, _env(tmp_path, "t_1"))

    assert exclude.read_bytes() == b" .env.ases\n.env.ases.bak\n# .env.ases\n.env.ases\n"


def test_a_missing_exclude_file_and_info_directory_are_created(repo, tmp_path):
    wt = _worktree(repo, "t_1")
    exclude = _exclude_path(wt)
    shutil.rmtree(exclude.parent)

    leases.write_env_file(wt, _env(tmp_path, "t_1"))

    assert exclude.read_bytes() == b".env.ases\n"
    assert _git("status", "--porcelain", cwd=wt).stdout == ""


def test_the_cards_temp_directory_is_created(repo, tmp_path):
    wt = _worktree(repo, "t_1")
    env = _env(tmp_path, "t_1")
    assert not pathlib.Path(env.temp_dir).exists()

    leases.write_env_file(wt, env)
    leases.write_env_file(wt, env)  # already there: not an error

    assert pathlib.Path(env.temp_dir).is_dir()


def test_a_symbolic_link_named_env_ases_is_replaced_and_never_followed(repo, tmp_path):
    wt = _worktree(repo, "t_1")
    victim = tmp_path / "victim.txt"
    victim.write_text("precious\n", encoding="utf-8")
    try:
        os.symlink(victim, wt / ".env.ases")
    except (OSError, NotImplementedError):
        pytest.skip("this account cannot create symbolic links")

    leases.write_env_file(wt, _env(tmp_path, "t_1"))

    assert victim.read_text(encoding="utf-8") == "precious\n"
    assert not (wt / ".env.ases").is_symlink()
    assert _read_env(wt / ".env.ases")["ASES_PORT_BASE"] == "42000"


def test_the_symbolic_link_branch_removes_the_target_before_writing_even_where_links_cannot_be_made(
    repo, tmp_path, monkeypatch,
):
    wt = _worktree(repo, "t_1")
    (wt / ".env.ases").write_text("junk\n", encoding="utf-8")
    unlinked = []
    real_unlink = pathlib.Path.unlink

    def spy_unlink(self, *args, **kwargs):
        unlinked.append(self.name)
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "is_symlink", lambda self: self.name == ".env.ases")
    monkeypatch.setattr(pathlib.Path, "unlink", spy_unlink)

    leases.write_env_file(wt, _env(tmp_path, "t_1"))

    assert unlinked == [".env.ases"]
    assert _read_env(wt / ".env.ases")["ASES_PORT_BASE"] == "42000"


def test_a_missing_worktree_is_a_lease_error_and_nothing_is_written(tmp_path):
    env = _env(tmp_path, "t_1")

    with pytest.raises(leases.LeaseError, match="does not exist"):
        leases.write_env_file(tmp_path / "nope", env)

    assert not (tmp_path / "nope").exists() and not pathlib.Path(env.temp_dir).exists()


def test_a_directory_that_is_not_a_git_worktree_gets_no_env_file(tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))  # never discover a repository above the temp dir
    env = _env(tmp_path, "t_1")

    with pytest.raises(leases.LeaseError, match="cannot be ignored"):
        leases.write_env_file(plain, env)

    assert not (plain / ".env.ases").exists() and not pathlib.Path(env.temp_dir).exists()


@pytest.mark.parametrize("error", [FileNotFoundError("git"), subprocess.TimeoutExpired("git", 30)])
def test_a_git_that_cannot_run_is_a_lease_error_and_nothing_is_written(repo, tmp_path, monkeypatch, error):
    wt = _worktree(repo, "t_1")

    def fail(cmd, **kwargs):
        raise error

    monkeypatch.setattr(leases.subprocess, "run", fail)

    with pytest.raises(leases.LeaseError, match="cannot ask git"):
        leases.write_env_file(wt, _env(tmp_path, "t_1"))

    assert not (wt / ".env.ases").exists()


def test_a_hostile_card_id_cannot_inject_a_variable_through_the_header(repo, tmp_path):
    wt = _worktree(repo, "t_1")
    env = _sample_env(card_id="x\nEVIL=1\r\n\u00e9", temp_dir=str(tmp_path / "tmp" / "x"))

    lines = leases.write_env_file(wt, env).read_text(encoding="utf-8").splitlines()

    assert lines[0] == "# Written by ASES for card x?EVIL=1??? (ASES-GIT-14). Do not edit this file and do not commit it:"
    assert not any(line.startswith("EVIL") for line in lines)


@pytest.mark.parametrize("value,expected", [
    ("42000", "42000"),
    ("ases-demo-t-1", "ases-demo-t-1"),
    ("ases_demo_t_1", "ases_demo_t_1"),
    ("C:/Temp/ases/x", "C:/Temp/ases/x"),
    ("a@b%c+d=e:f,g.h", "a@b%c+d=e:f,g.h"),
    ("C:\\Temp\\ases", "'C:\\Temp\\ases'"),
    ("/tmp/with space/x", "'/tmp/with space/x'"),
    ("", "''"),
    ("a#b", "'a#b'"),
    ("$HOME/x", "'$HOME/x'"),
    ("a`b", "'a`b'"),
    ('a"b', "'a\"b'"),
    ("O'Brien", "\"O'Brien\""),
    ("it's $5 \"x\" `y` \\z", "\"it's \\$5 \\\"x\\\" \\`y\\` \\\\z\""),
    ("line1\nline2", '"line1\\nline2"'),
    ("cr\rx", '"cr\\rx"'),
])
def test_values_are_quoted_only_when_they_need_it(value, expected):
    assert leases._quote_value(value) == expected


# ---------------------------------------------------------------------------------------------
# provision_running_cards and sweep_finished (the controller-pass wiring)
# ---------------------------------------------------------------------------------------------


def _plan(keys=("T1", "T2", "T3"), project=PROJECT):
    return plan_mod.parse_and_validate({
        "project": project, "integration_branch": "integration", "gate_profiles": {"trivial": ["echo ok"]},
        "tasks": [
            {"key": key, "title": f"task {key}", "role": "coder", "depends_on": [], "touches": [f"{key.lower()}/**"],
             "acceptance": ["done"], "gate_profile": "trivial", "estimated_requests": 5}
            for key in keys
        ],
    }, known_roles={"coder"}, max_cards=40)


def _seed(conn, cards, project=PROJECT):
    """plan_tasks rows for {task key: work card id}, in that order."""
    for key, card_id in cards.items():
        conn.execute(
            "INSERT INTO plan_tasks (project, task_key, work_card_id, merge_card_id, role, created_at) "
            "VALUES (?, ?, ?, ?, 'coder', datetime('now'))", (project, key, card_id, f"m_{key}"),
        )


class _Board:
    """hermes.kanban_list and hermes.kanban_show over a dict of card id -> {status, workspace_path}. The list
    honours its status filter like the real one does, and reports each card's own status."""

    def __init__(self, cards):
        self.cards = cards
        self.listed = []
        self.shown = []

    def kanban_list(self, board, *, status=None, assignee=None):
        self.listed.append(status)
        return [
            {"id": card_id, "status": card["status"]} for card_id, card in self.cards.items()
            if status is None or card["status"] == status
        ]

    def kanban_show(self, board, card_id):
        self.shown.append(card_id)
        return {"id": card_id, **self.cards[card_id]}


def _provision(conn, tmp_path, board, plan=None, **overrides):
    def allocate(conn_, project, card_id):
        return leases.allocate_card_env(conn_, project, card_id, port_free=_free, temp_root=tmp_path / "tmp")

    kwargs = {"allocate": allocate, "kanban_list": board.kanban_list, "kanban_show": board.kanban_show}
    kwargs.update(overrides)
    return leases.provision_running_cards("b", conn, plan or _plan(), **kwargs)


def _three_running_cards(conn, repo):
    worktrees = {card: _worktree(repo, card) for card in ("c1", "c2", "c3")}
    _seed(conn, {"T1": "c1", "T2": "c2", "T3": "c3"})
    board = _Board({card: {"status": "running", "workspace_path": str(wt)} for card, wt in worktrees.items()})
    return worktrees, board


def test_running_cards_with_a_worktree_are_provisioned_in_plan_order(conn, repo, tmp_path):
    worktrees, board = _three_running_cards(conn, repo)

    provisioned = _provision(conn, tmp_path, board)

    assert provisioned == ["c1", "c2", "c3"]
    for card, wt in worktrees.items():
        values = _read_env(wt / ".env.ases")
        assert values["COMPOSE_PROJECT_NAME"] == f"ases-demo-{card}"
        assert values["ASES_DB_NAME"] == f"ases_demo_{card}"
    assert [h["holder"] for h in leases.holders(conn, PROJECT)] == ["c1", "c2", "c3"]
    assert board.listed == ["running"]


def test_cards_are_provisioned_in_the_order_the_plan_created_them_not_in_key_order(conn, repo, tmp_path):
    a, b, c = (_worktree(repo, name) for name in ("c1", "c2", "c3"))
    _seed(conn, {"T3": "c3", "T1": "c1", "T2": "c2"})  # inserted out of key order, the way a plan may create them
    board = _Board({
        "c1": {"status": "running", "workspace_path": str(a)},
        "c2": {"status": "running", "workspace_path": str(b)},
        "c3": {"status": "running", "workspace_path": str(c)},
    })

    assert _provision(conn, tmp_path, board) == ["c3", "c1", "c2"]


def test_cards_that_are_not_running_are_skipped_without_being_looked_at(conn, repo, tmp_path):
    worktrees, board = _three_running_cards(conn, repo)
    board.cards["c2"]["status"] = "ready"
    board.cards["c3"]["status"] = "done"

    provisioned = _provision(conn, tmp_path, board)

    assert provisioned == ["c1"]
    assert board.shown == ["c1"]
    assert not (worktrees["c2"] / ".env.ases").exists() and not (worktrees["c3"] / ".env.ases").exists()
    assert [h["holder"] for h in leases.holders(conn, PROJECT)] == ["c1"]


def test_a_listing_that_ignores_the_status_filter_is_not_read_as_all_running(conn, repo, tmp_path):
    worktrees, board = _three_running_cards(conn, repo)
    board.cards["c2"]["status"] = "done"

    provisioned = _provision(
        conn, tmp_path, board,
        kanban_list=lambda b, status=None: [{"id": c, "status": v["status"]} for c, v in board.cards.items()],
    )

    assert provisioned == ["c1", "c3"]


@pytest.mark.parametrize("workspace", [None, "", "missing"])
def test_a_card_without_a_workspace_or_with_a_missing_directory_is_skipped_quietly(conn, repo, tmp_path, workspace):
    worktrees, board = _three_running_cards(conn, repo)
    board.cards["c2"]["workspace_path"] = str(tmp_path / "missing") if workspace == "missing" else workspace

    provisioned = _provision(conn, tmp_path, board)

    assert provisioned == ["c1", "c3"]
    assert _event_payloads(conn, "provision_error") == []


def test_a_card_that_has_no_workspace_field_at_all_is_skipped(conn, repo, tmp_path):
    worktrees, board = _three_running_cards(conn, repo)
    del board.cards["c1"]["workspace_path"]

    assert _provision(conn, tmp_path, board) == ["c2", "c3"]


def test_a_card_that_already_has_its_env_file_is_left_alone_and_gets_no_allocation(conn, repo, tmp_path):
    worktrees, board = _three_running_cards(conn, repo)
    (worktrees["c2"] / ".env.ases").write_text("mine\n", encoding="utf-8")
    allocated = []

    def allocate(conn_, project, card_id):
        allocated.append(card_id)
        return leases.allocate_card_env(conn_, project, card_id, port_free=_free, temp_root=tmp_path / "tmp")

    provisioned = _provision(conn, tmp_path, board, allocate=allocate)

    assert provisioned == ["c1", "c3"] and allocated == ["c1", "c3"]
    assert (worktrees["c2"] / ".env.ases").read_text(encoding="utf-8") == "mine\n"


def test_cards_outside_this_plan_and_other_projects_are_ignored(conn, repo, tmp_path):
    mine, theirs, stray = (_worktree(repo, name) for name in ("c1", "c2", "stray"))
    _seed(conn, {"T1": "c1"})
    _seed(conn, {"T1": "c2"}, project="other")  # another project's plan owns c2, and reuses the task key T1
    board = _Board({  # all three run on the same board; "stray" is in no plan at all
        "c1": {"status": "running", "workspace_path": str(mine)},
        "c2": {"status": "running", "workspace_path": str(theirs)},
        "stray": {"status": "running", "workspace_path": str(stray)},
    })

    provisioned = _provision(conn, tmp_path, board, plan=_plan(keys=("T1",)))

    assert provisioned == ["c1"]
    assert board.shown == ["c1"]
    assert not (theirs / ".env.ases").exists() and not (stray / ".env.ases").exists()
    assert [h["holder"] for h in leases.holders(conn, PROJECT)] == ["c1"]


def test_a_task_without_a_work_card_yet_is_skipped(conn, repo, tmp_path):
    wt = _worktree(repo, "c1")
    conn.execute(
        "INSERT INTO plan_tasks (project, task_key, work_card_id, role, created_at) "
        "VALUES (?, 'T1', NULL, 'coder', datetime('now'))", (PROJECT,),
    )
    board = _Board({"c1": {"status": "running", "workspace_path": str(wt)}})

    assert _provision(conn, tmp_path, board, plan=_plan(keys=("T1",))) == []


def test_a_failing_card_never_stops_the_others_and_is_recorded_as_a_provision_error(conn, repo, tmp_path):
    worktrees, board = _three_running_cards(conn, repo)

    def flaky(conn_, project, card_id):
        if card_id == "c2":
            raise leases.LeaseError("no free port block for c2")
        return leases.allocate_card_env(conn_, project, card_id, port_free=_free, temp_root=tmp_path / "tmp")

    provisioned = _provision(conn, tmp_path, board, allocate=flaky)

    assert provisioned == ["c1", "c3"]
    assert _event_payloads(conn, "provision_error") == [
        {"card_id": "c2", "error": "LeaseError: no free port block for c2"}
    ]
    assert (worktrees["c1"] / ".env.ases").exists() and (worktrees["c3"] / ".env.ases").exists()


def test_a_card_whose_write_fails_keeps_its_lease_and_is_provisioned_on_the_next_pass(
    conn, repo, tmp_path, monkeypatch,
):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))  # never discover a repository above the temp dir
    worktrees, board = _three_running_cards(conn, repo)
    plain = tmp_path / "plain"  # exists, but is no git worktree and lies in no repository: the write must fail
    plain.mkdir()
    board.cards["c2"]["workspace_path"] = str(plain)

    first = _provision(conn, tmp_path, board)

    assert first == ["c1", "c3"]
    errors = _event_payloads(conn, "provision_error")
    assert [e["card_id"] for e in errors] == ["c2"] and "LeaseError" in errors[0]["error"]
    assert not (plain / ".env.ases").exists()
    blocks = {h["holder"]: h["resource"] for h in leases.holders(conn, PROJECT)}
    assert blocks == {"c1": "port-block:0", "c2": "port-block:1", "c3": "port-block:2"}

    board.cards["c2"]["workspace_path"] = str(worktrees["c2"])  # Hermes has made the worktree; the next pass finds it
    second = _provision(conn, tmp_path, board)

    assert second == ["c2"]
    assert _read_env(worktrees["c2"] / ".env.ases")["ASES_PORT_BASE"] == "42010"  # the same block, not a new one
    assert len(leases.holders(conn, PROJECT)) == 3


def test_a_board_that_cannot_be_listed_records_one_error_and_provisions_nothing(conn, repo, tmp_path):
    worktrees, board = _three_running_cards(conn, repo)

    def down(board_name, status=None):
        raise hermes.HermesCommandError(["kanban", "list"], 1, "database is locked")

    assert _provision(conn, tmp_path, board, kanban_list=down) == []

    errors = _event_payloads(conn, "provision_error")
    assert len(errors) == 1 and errors[0]["card_id"] is None and "HermesCommandError" in errors[0]["error"]
    assert not (worktrees["c1"] / ".env.ases").exists()


def test_a_card_that_cannot_be_shown_is_recorded_and_the_rest_carry_on(conn, repo, tmp_path):
    worktrees, board = _three_running_cards(conn, repo)
    real_show = board.kanban_show

    def show(board_name, card_id):
        if card_id == "c1":
            raise hermes.HermesCommandError(["kanban", "show"], 1, "boom")
        return real_show(board_name, card_id)

    assert _provision(conn, tmp_path, board, kanban_show=show) == ["c2", "c3"]
    assert [e["card_id"] for e in _event_payloads(conn, "provision_error")] == ["c1"]


def test_a_second_pass_provisions_nothing_and_takes_no_more_blocks(conn, repo, tmp_path):
    worktrees, board = _three_running_cards(conn, repo)
    _provision(conn, tmp_path, board)

    assert _provision(conn, tmp_path, board) == []

    assert len(leases.holders(conn, PROJECT)) == 3


def test_the_default_functions_are_resolved_at_call_time_so_monkeypatching_hermes_works(
    conn, repo, tmp_path, monkeypatch,
):
    worktrees, board = _three_running_cards(conn, repo)
    monkeypatch.setattr(hermes, "kanban_list", board.kanban_list)
    monkeypatch.setattr(hermes, "kanban_show", board.kanban_show)
    real_allocate = leases.allocate_card_env
    spied = []

    def spy(conn_, project, card_id):
        spied.append(card_id)
        return real_allocate(conn_, project, card_id, port_free=_free, temp_root=tmp_path / "tmp")

    monkeypatch.setattr(leases, "allocate_card_env", spy)

    provisioned = leases.provision_running_cards("b", conn, _plan())

    assert provisioned == ["c1", "c2", "c3"] and spied == ["c1", "c2", "c3"]


def test_three_running_cards_end_with_separate_port_blocks_and_compose_projects_and_a_fourth_stays_queued(
    conn, repo, tmp_path,
):
    """Blueprint test 22.5, the part that belongs to leases: separate worktrees, branches, port blocks and
    compose project names, and no cross-worktree change in the integrity snapshots."""
    worktrees, board = _three_running_cards(conn, repo)
    queued = _worktree(repo, "c4")
    _seed(conn, {"T4": "c4"})
    board.cards["c4"] = {"status": "ready", "workspace_path": str(queued)}
    plan = _plan(keys=("T1", "T2", "T3", "T4"))
    guards.check_idle_worktrees(conn, PROJECT, repo, set())  # the integrity baseline before anything is provisioned

    provisioned = _provision(conn, tmp_path, board, plan=plan)

    assert provisioned == ["c1", "c2", "c3"]
    envs = {card: _read_env(wt / ".env.ases") for card, wt in worktrees.items()}
    assert len({v["COMPOSE_PROJECT_NAME"] for v in envs.values()}) == 3
    assert len({v["ASES_DB_NAME"] for v in envs.values()}) == 3
    assert len({v["ASES_TMPDIR"] for v in envs.values()}) == 3
    bases = sorted(int(v["ASES_PORT_BASE"]) for v in envs.values())
    assert bases == [42000, 42010, 42020]
    ports = [int(v[f"ASES_PORT_{i}"]) for v in envs.values() for i in range(int(v["ASES_PORT_COUNT"]))]
    assert len(ports) == len(set(ports)) == 30
    branches = {_git("branch", "--show-current", cwd=wt).stdout.strip() for wt in worktrees.values()}
    assert branches == {"swarm/c1", "swarm/c2", "swarm/c3"}
    assert not (queued / ".env.ases").exists()  # the fourth card has not been dispatched: no env, no lease
    assert [h["holder"] for h in leases.holders(conn, PROJECT)] == ["c1", "c2", "c3"]
    for wt in worktrees.values():
        assert _git("status", "--porcelain", cwd=wt).stdout == ""
    assert guards.check_idle_worktrees(conn, PROJECT, repo, set()) == []  # writing .env.ases changed no snapshot


def _leased_cards(conn, tmp_path, cards):
    for card in cards:
        _alloc(conn, card, temp_root=tmp_path)


def test_sweep_finished_keeps_running_ready_review_and_scheduled_holders_and_frees_the_rest(conn, tmp_path):
    statuses = {"r": "running", "y": "ready", "v": "review", "s": "scheduled", "d": "done", "k": "blocked",
                "a": "archived"}
    _leased_cards(conn, tmp_path, statuses)
    board = _Board({card: {"status": status} for card, status in statuses.items()})

    released = leases.sweep_finished("b", conn, _plan(), kanban_list=board.kanban_list)

    assert released == ["port-block:4", "port-block:5", "port-block:6"]
    assert [h["holder"] for h in leases.holders(conn, PROJECT)] == ["r", "y", "v", "s"]
    assert board.listed == ["running", "ready", "review", "scheduled"]


def test_sweep_finished_does_not_read_a_card_under_the_wrong_status_as_live(conn, tmp_path):
    _leased_cards(conn, tmp_path, ["r", "d"])
    board = _Board({"r": {"status": "running"}, "d": {"status": "done"}})
    every_card = lambda b, status=None: [  # noqa: E731 - a listing that ignores the status filter
        {"id": c, "status": v["status"]} for c, v in board.cards.items()
    ]

    released = leases.sweep_finished("b", conn, _plan(), kanban_list=every_card)

    assert released == ["port-block:1"]


def test_sweep_finished_can_be_told_to_keep_blocked_cards_too(conn, tmp_path):
    _leased_cards(conn, tmp_path, ["r", "k", "d"])
    board = _Board({"r": {"status": "running"}, "k": {"status": "blocked"}, "d": {"status": "done"}})

    released = leases.sweep_finished(
        "b", conn, _plan(), kanban_list=board.kanban_list, live_statuses=(*leases.LIVE_STATUSES, "blocked"),
    )

    assert released == ["port-block:2"]
    assert [h["holder"] for h in leases.holders(conn, PROJECT)] == ["r", "k"]


def test_sweep_finished_releases_nothing_when_any_listing_fails(conn, tmp_path):
    _leased_cards(conn, tmp_path, ["r", "d"])
    board = _Board({"r": {"status": "running"}, "d": {"status": "done"}})

    def flaky(board_name, status=None):
        if status == "review":  # the failure comes after two good listings: a partial view of the board
            raise hermes.HermesCommandError(["kanban", "list"], 1, "database is locked")
        return board.kanban_list(board_name, status=status)

    assert leases.sweep_finished("b", conn, _plan(), kanban_list=flaky) == []

    assert [h["holder"] for h in leases.holders(conn, PROJECT)] == ["r", "d"]
    errors = _event_payloads(conn, "lease_sweep_error")
    assert len(errors) == 1 and "HermesCommandError" in errors[0]["error"]


def test_sweep_finished_only_frees_this_plans_project_and_stamps_the_clock(conn, tmp_path):
    _alloc(conn, "d", project=PROJECT, temp_root=tmp_path)
    _alloc(conn, "d", project="other", temp_root=tmp_path)
    board = _Board({"d": {"status": "done"}})

    released = leases.sweep_finished(
        "b", conn, _plan(), kanban_list=board.kanban_list, now=datetime(2026, 9, 19, 6, 0, 0, tzinfo=UTC),
    )

    assert released == ["port-block:0"]
    assert [h["holder"] for h in leases.holders(conn, "other")] == ["d"]
    stamp = conn.execute("SELECT released_at FROM resource_leases WHERE project = ?", (PROJECT,)).fetchone()[0]
    assert stamp == "2026-09-19T06:00:00+00:00"


def test_sweep_finished_resolves_hermes_at_call_time(conn, tmp_path, monkeypatch):
    _leased_cards(conn, tmp_path, ["r", "d"])
    board = _Board({"r": {"status": "running"}, "d": {"status": "done"}})
    monkeypatch.setattr(hermes, "kanban_list", board.kanban_list)

    assert leases.sweep_finished("b", conn, _plan()) == ["port-block:1"]
