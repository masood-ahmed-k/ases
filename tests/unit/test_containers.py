"""ases.containers: the worker sandbox orphan sweep (package CONTAINERS, round 17; section 19.4 p353,
ASES-REC-04, ASES-SEC-03). Every outside effect (Docker, Hermes) is a fake: no test here reaches a real
Docker daemon or a real Hermes board. See the module's own docstring for the empirical finding this design
is built on (a real, CLI-dispatched worker's container label hermes-task-id is always "default", never the
card id, so an orphan is found by Hermes profile instead of by card id)."""
import json
import subprocess

import pytest

from ases import config, containers, db

ROLES = {"lead": "lead", "coder": "coder-1", "reviewer": "reviewer"}


def _project(tmp_path, *, sandbox_enabled=True, roles=None, name="p1", board="b"):
    return config.ProjectConfig(
        name=name, environment="native", data_class="public",
        workspace_root=tmp_path / "ws", ases_home=tmp_path / "home", board=board, integration_branch="integration",
        roles=ROLES if roles is None else roles, concurrency={}, budgets={}, hermes_tested_version="0.21.3",
        hermes_native_home=tmp_path / "hermes", sandbox={"enabled": sandbox_enabled},
    )


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "ases.db")


def events_of(conn, kind):
    return [json.loads(row[0]) for row in
            conn.execute("SELECT payload FROM events WHERE kind = ? ORDER BY id", (kind,)).fetchall()]


def event_projects(conn, kind):
    """The events.py schema's own `project` COLUMN (events.PROJECT_SQL), separate from the payload dict."""
    return [row[0] for row in conn.execute("SELECT project FROM events WHERE kind = ? ORDER BY id", (kind,)).fetchall()]


def _cards(*, running=()):
    """A kanban_list fake: status='running' returns one dict per name in `running`; any other status (or a
    caller that forgets the filter) is refused, so a test notices if the wrong status were ever asked for."""
    def kanban_list(board, *, status=None):
        assert status == "running"
        return [{"id": f"t_{name}", "assignee": name, "status": "running"} for name in running]
    return kanban_list


# ---------------------------------------------------------------------------------------------
# default_list_hermes_containers
# ---------------------------------------------------------------------------------------------


def test_default_list_hermes_containers_parses_name_and_profile(monkeypatch):
    seen = []
    listing = "hermes-a1b2c3d4|coder-1\nhermes-e5f6a7b8|reviewer\n"
    monkeypatch.setattr(containers, "_run", lambda args, timeout: seen.append(args) or (0, listing))

    result = containers.default_list_hermes_containers()

    assert result == [("hermes-a1b2c3d4", "coder-1"), ("hermes-e5f6a7b8", "reviewer")]
    assert seen == [[
        "docker", "ps", "--filter", "label=hermes-agent=1", "--format", '{{.Names}}|{{.Label "hermes-profile"}}',
    ]]


def test_default_list_hermes_containers_handles_an_empty_profile_label(monkeypatch):
    monkeypatch.setattr(containers, "_run", lambda args, timeout: (0, "hermes-a1b2c3d4|\n"))

    assert containers.default_list_hermes_containers() == [("hermes-a1b2c3d4", "")]


@pytest.mark.parametrize("failure", [
    FileNotFoundError("docker"), subprocess.TimeoutExpired("docker", 20), OSError("daemon"), RuntimeError("x"),
])
def test_default_list_hermes_containers_returns_nothing_when_docker_is_absent_or_broken(monkeypatch, failure):
    def broken(args, timeout):
        raise failure

    monkeypatch.setattr(containers, "_run", broken)

    assert containers.default_list_hermes_containers() == []


def test_default_list_hermes_containers_returns_nothing_on_a_non_zero_exit(monkeypatch):
    monkeypatch.setattr(containers, "_run", lambda args, timeout: (1, "hermes-a1b2c3d4|coder-1\n"))

    assert containers.default_list_hermes_containers() == []


# ---------------------------------------------------------------------------------------------
# default_running_profiles
# ---------------------------------------------------------------------------------------------


def test_default_running_profiles_collects_the_assignees_of_running_cards():
    assert containers.default_running_profiles("b", kanban_list=_cards(running=["coder-1", "reviewer"])) == {
        "coder-1", "reviewer",
    }


def test_default_running_profiles_ignores_a_card_with_no_assignee():
    kanban_list = lambda board, *, status: [{"id": "t_1", "status": "running"}]
    assert containers.default_running_profiles("b", kanban_list=kanban_list) == set()


def test_default_running_profiles_ignores_entries_that_are_not_a_mapping():
    kanban_list = lambda board, *, status: ["not-a-dict", None]
    assert containers.default_running_profiles("b", kanban_list=kanban_list) == set()


def test_default_running_profiles_is_none_when_the_listing_fails():
    def broken(board, *, status):
        raise RuntimeError("hermes is down")

    assert containers.default_running_profiles("b", kanban_list=broken) is None


def test_default_running_profiles_defaults_to_the_real_hermes_kanban_list(monkeypatch):
    from ases import hermes as hermes_mod
    seen = []
    monkeypatch.setattr(hermes_mod, "kanban_list", lambda board, *, status: seen.append((board, status)) or [])

    assert containers.default_running_profiles("b") == set()
    assert seen == [("b", "running")]


# ---------------------------------------------------------------------------------------------
# find_orphan_containers
# ---------------------------------------------------------------------------------------------


def test_no_project_profiles_means_nothing_is_ever_found(tmp_path):
    project = _project(tmp_path, roles={})

    def must_not_be_called():
        raise AssertionError("must not list containers with no project profiles")

    assert containers.find_orphan_containers("b", project, list_containers=must_not_be_called) == []


def test_no_hermes_containers_at_all_means_nothing_is_found_and_the_board_is_never_asked(tmp_path):
    project = _project(tmp_path)

    def must_not_be_called(board, *, status):
        raise AssertionError("must not list running cards when there are no containers to begin with")

    result = containers.find_orphan_containers(
        "b", project, list_containers=lambda: [], kanban_list=must_not_be_called,
    )

    assert result == []


def test_a_container_of_an_unrelated_profile_is_never_returned(tmp_path):
    project = _project(tmp_path)  # profiles: lead, coder-1, reviewer

    result = containers.find_orphan_containers(
        "b", project,
        list_containers=lambda: [("hermes-abc", "some-other-projects-profile")],
        kanban_list=_cards(running=[]),
    )

    assert result == []


def test_a_profile_with_a_running_card_is_left_alone_entirely(tmp_path):
    project = _project(tmp_path)

    result = containers.find_orphan_containers(
        "b", project,
        list_containers=lambda: [("hermes-abc", "coder-1"), ("hermes-def", "coder-1")],
        kanban_list=_cards(running=["coder-1"]),
    )

    assert result == []  # both containers belong to coder-1, which has a card running right now


def test_a_profile_with_no_running_card_is_orphaned(tmp_path):
    project = _project(tmp_path)

    result = containers.find_orphan_containers(
        "b", project,
        list_containers=lambda: [("hermes-abc", "coder-1")],
        kanban_list=_cards(running=["reviewer"]),  # coder-1 is not running anything
    )

    assert result == [("hermes-abc", "coder-1")]


def test_only_the_idle_profiles_container_is_found_when_several_profiles_are_mixed(tmp_path):
    project = _project(tmp_path)

    result = containers.find_orphan_containers(
        "b", project,
        list_containers=lambda: [
            ("hermes-live", "coder-1"), ("hermes-idle", "reviewer"), ("hermes-other-project", "some-other-profile"),
        ],
        kanban_list=_cards(running=["coder-1"]),
    )

    assert result == [("hermes-idle", "reviewer")]


def test_a_listing_that_raises_finds_nothing_this_pass(tmp_path):
    project = _project(tmp_path)

    def broken():
        raise RuntimeError("docker hung")

    assert containers.find_orphan_containers("b", project, list_containers=broken) == []


def test_when_the_board_cannot_be_read_nothing_is_swept_this_pass_not_everything(tmp_path):
    """None, never [], must come back: a caller reading [] as "nothing orphaned" would be wrong when the
    truth is "unknown"."""
    project = _project(tmp_path)

    def broken_kanban_list(board, *, status):
        raise RuntimeError("hermes is down")

    result = containers.find_orphan_containers(
        "b", project, list_containers=lambda: [("hermes-abc", "coder-1")], kanban_list=broken_kanban_list,
    )

    assert result is None


# ---------------------------------------------------------------------------------------------
# sweep_orphan_containers
# ---------------------------------------------------------------------------------------------


def test_sandbox_disabled_is_a_cheap_no_op(tmp_path, conn):
    project = _project(tmp_path, sandbox_enabled=False)

    def must_not_be_called(*a, **kw):
        raise AssertionError("must not list containers while the sandbox is disabled")

    report = containers.sweep_orphan_containers(
        "b", project, conn, list_containers=must_not_be_called, kanban_list=must_not_be_called,
    )

    assert report.stopped == [] and report.notes == []


def test_an_orphaned_container_is_stopped_and_recorded(tmp_path, conn):
    project = _project(tmp_path)
    stopped = []

    report = containers.sweep_orphan_containers(
        "b", project, conn,
        list_containers=lambda: [("hermes-abc", "coder-1")],
        kanban_list=_cards(running=[]),
        stop_container=lambda name: stopped.append(name) or True,
    )

    assert report.stopped == ["hermes-abc"]
    assert stopped == ["hermes-abc"]
    assert events_of(conn, "orphan_container_stopped") == [{"container": "hermes-abc", "profile": "coder-1"}]
    assert event_projects(conn, "orphan_container_stopped") == ["p1"]  # events.PROJECT_SQL scoping


def test_a_running_cards_container_is_never_stopped(tmp_path, conn):
    project = _project(tmp_path)

    def must_not_stop(name):
        raise AssertionError(f"must not stop {name}: its profile has a card running")

    report = containers.sweep_orphan_containers(
        "b", project, conn,
        list_containers=lambda: [("hermes-abc", "coder-1")],
        kanban_list=_cards(running=["coder-1"]),
        stop_container=must_not_stop,
    )

    assert report.stopped == []
    assert events_of(conn, "orphan_container_stopped") == []


def test_an_unrelated_containers_profile_is_never_stopped(tmp_path, conn):
    project = _project(tmp_path)

    def must_not_stop(name):
        raise AssertionError(f"must not stop {name}: not one of this project's profiles")

    report = containers.sweep_orphan_containers(
        "b", project, conn,
        list_containers=lambda: [("hermes-abc", "another-projects-profile")],
        kanban_list=_cards(running=[]),
        stop_container=must_not_stop,
    )

    assert report.stopped == []


def test_a_stop_that_returns_false_is_noted_and_recorded_as_failed(tmp_path, conn):
    project = _project(tmp_path)

    report = containers.sweep_orphan_containers(
        "b", project, conn,
        list_containers=lambda: [("hermes-abc", "coder-1")],
        kanban_list=_cards(running=[]),
        stop_container=lambda name: False,
    )

    assert report.stopped == []
    assert "hermes-abc" in report.notes[0]
    assert events_of(conn, "orphan_container_stop_failed") == [{"container": "hermes-abc", "profile": "coder-1"}]
    assert event_projects(conn, "orphan_container_stop_failed") == ["p1"]


def test_a_stop_that_raises_does_not_stop_the_sweep_of_the_others(tmp_path, conn):
    project = _project(tmp_path)

    def flaky(name):
        if name == "hermes-first":
            raise RuntimeError("docker daemon hiccup")
        return True

    report = containers.sweep_orphan_containers(
        "b", project, conn,
        list_containers=lambda: [("hermes-first", "coder-1"), ("hermes-second", "reviewer")],
        kanban_list=_cards(running=[]),
        stop_container=flaky,
    )

    assert report.stopped == ["hermes-second"]
    assert any("hermes-first" in note and "docker daemon hiccup" in note for note in report.notes)
    assert [e["error"] for e in events_of(conn, "orphan_container_stop_failed")] == ["RuntimeError: docker daemon hiccup"]


def test_docker_being_absent_is_a_no_op_not_a_failure(tmp_path, conn):
    project = _project(tmp_path)

    report = containers.sweep_orphan_containers(
        "b", project, conn, list_containers=lambda: [], kanban_list=_cards(running=[]),
    )

    assert report.stopped == [] and report.notes == []


def test_an_unreadable_board_is_noted_and_nothing_is_stopped(tmp_path, conn):
    project = _project(tmp_path)

    def must_not_stop(name):
        raise AssertionError("must not stop anything when the board could not be read")

    def broken_kanban_list(board, *, status):
        raise RuntimeError("hermes is down")

    report = containers.sweep_orphan_containers(
        "b", project, conn,
        list_containers=lambda: [("hermes-abc", "coder-1")],
        kanban_list=broken_kanban_list,
        stop_container=must_not_stop,
    )

    assert report.stopped == []
    assert "could not list running cards" in report.notes[0]


def test_default_stop_container_is_killswitchs_own_docker_stop(monkeypatch, tmp_path, conn):
    """sweep_orphan_containers defaults to killswitch.default_stop_container (docker stop -t 5), never a
    second, independent implementation of "stop a container"."""
    from ases import killswitch
    seen = []
    monkeypatch.setattr(killswitch, "_run", lambda args, timeout: seen.append(args) or (0, ""))
    project = _project(tmp_path)

    report = containers.sweep_orphan_containers(
        "b", project, conn, list_containers=lambda: [("hermes-abc", "coder-1")], kanban_list=_cards(running=[]),
    )

    assert report.stopped == ["hermes-abc"]
    assert seen == [["docker", "stop", "-t", "5", "hermes-abc"]]
