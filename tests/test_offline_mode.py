"""Pure-layer tests for offline-analysis issue 01 — the offline mode core
(``src/seestar_controller/ui/mode.py``): the app/window mode flag
(``offline`` vs ``connected``), the offline-open path that opens a store
project WITHOUT touching the link layer, and the pure scope-action gating
helpers.

Everything here runs headless (no ``gi``, no display, no network — see
docs/wiki/assumptions.md): the ``ui_harness`` conftest fixture provides the
app-level pure controllers with a :class:`FakeDevice`, and the link layer is
mocked to fail where relevant (a device factory that raises) — the offline
open must still work. The widget-layer tests (banner, gated buttons) live in
``tests/test_offline_mode_widgets.py`` and skip in the headless sandbox.
"""

import pytest

from seestar_controller.store import create_project
from seestar_controller.store.errors import ProjectError
from seestar_controller.ui.connection import ConnectionController, ConnectionStatus
from seestar_controller.ui.mode import (
    OFFLINE_SCOPE_TOOLTIP,
    SCOPE_ACTIONS,
    AppMode,
    ModeController,
    UnknownScopeActionError,
)
from seestar_controller.ui.project_controller import ProjectController
from seestar_controller.ui.state import UIStateError


@pytest.fixture()
def offline_harness(ui_harness):
    """The ui harness plus a project controller (root in tmp_path) and a mode
    controller bound to both the connection and the project controller — the
    same wiring the app does."""
    root = ui_harness.tmp_path / "projects"
    root.mkdir()
    ui_harness.state.set_setting("project_root", str(root))
    projects = ProjectController(ui_harness.state)
    mode = ModeController()
    mode.attach_connection(ui_harness.connection)
    mode.attach_project_controller(projects)
    ui_harness.projects = projects
    ui_harness.mode = mode
    return ui_harness


# --- the mode flag + gating helpers ---------------------------------------------


def test_default_mode_is_connected_and_all_scope_actions_enabled():
    mode = ModeController()
    assert mode.mode == AppMode.CONNECTED
    assert not mode.is_offline
    assert str(AppMode.OFFLINE) == "offline"
    for action in SCOPE_ACTIONS:
        assert mode.scope_action_enabled(action)
        assert mode.scope_action_tooltip(action) is None
    assert "connect" in SCOPE_ACTIONS
    assert "goto_slew" in SCOPE_ACTIONS
    assert "live_preview" in SCOPE_ACTIONS
    assert "start_night" in SCOPE_ACTIONS
    assert "pause_resume_stop" in SCOPE_ACTIONS


def test_offline_mode_disables_every_scope_action_with_tooltip():
    mode = ModeController(AppMode.OFFLINE)
    assert mode.is_offline
    for action in SCOPE_ACTIONS:
        assert not mode.scope_action_enabled(action)
        assert mode.scope_action_tooltip(action) == OFFLINE_SCOPE_TOOLTIP


def test_unknown_scope_action_raises():
    mode = ModeController()
    with pytest.raises(UnknownScopeActionError) as excinfo:
        mode.scope_action_enabled("teleport")
    assert isinstance(excinfo.value, UIStateError)
    assert isinstance(excinfo.value, ValueError)
    with pytest.raises(UnknownScopeActionError):
        mode.scope_action_tooltip("teleport")


def test_set_mode_notifies_listeners_once_per_change():
    mode = ModeController()
    seen: list[AppMode] = []
    mode.add_listener(seen.append)
    mode.add_listener(seen.append)  # idempotent
    assert mode.set_mode(AppMode.OFFLINE) == AppMode.OFFLINE
    assert seen == [AppMode.OFFLINE]
    mode.set_mode(AppMode.OFFLINE)  # unchanged -> no notification
    assert seen == [AppMode.OFFLINE]
    mode.set_mode("connected")  # str accepted
    assert seen == [AppMode.OFFLINE, AppMode.CONNECTED]
    assert mode.listeners == (seen.append,)
    mode.remove_listener(seen.append)
    mode.set_mode(AppMode.OFFLINE)
    assert seen == [AppMode.OFFLINE, AppMode.CONNECTED]


# --- the offline-open path (link layer mocked to fail) ---------------------------


def test_open_project_offline_opens_store_project_and_never_touches_link(offline_harness):
    """The core acceptance test: with the link layer mocked to fail (the
    device factory raises if ever called), opening a project offline still
    works — pure store access, mode -> offline, no device, no session."""
    harness = offline_harness
    calls: list[str] = []

    def failing_factory(ip, key_path):
        calls.append(ip)
        raise OSError("link layer down (mocked)")

    failing_connection = ConnectionController(
        harness.settings, device_factory=failing_factory
    )
    harness.mode.attach_connection(failing_connection)

    project = harness.projects.create_project("M 42")
    assert harness.mode.mode == AppMode.CONNECTED  # normal create -> connected

    opened = harness.mode.open_project_offline(harness.projects, project.path)
    assert opened.path == project.path
    assert harness.projects.current is not None
    assert harness.projects.current.path == project.path
    assert harness.mode.mode == AppMode.OFFLINE
    assert harness.mode.is_offline
    # persisted as last_project
    assert harness.state.get_setting("last_project") == str(project.path)
    # the link layer was NEVER touched: no factory call, no device, still
    # disconnected on both connection controllers
    assert calls == []
    assert failing_connection.device is None
    assert failing_connection.status == ConnectionStatus.DISCONNECTED
    assert harness.connection.device is None
    assert harness.connection.status == ConnectionStatus.DISCONNECTED


def test_open_project_offline_survives_a_failed_connect(offline_harness):
    """Link layer mocked to fail: connect() lands in the error state, and the
    offline open still works afterwards (and the failed connect does not
    switch the mode)."""
    harness = offline_harness

    def failing_factory(ip, key_path):
        raise OSError("link layer down (mocked)")

    failing_connection = ConnectionController(
        harness.settings, device_factory=failing_factory
    )
    harness.mode.attach_connection(failing_connection)
    project = harness.projects.create_project("M 31")

    failing_connection.connect()
    assert failing_connection.wait(5)
    assert failing_connection.status == ConnectionStatus.ERROR
    assert "link layer down" in (failing_connection.error or "")

    opened = harness.mode.open_project_offline(harness.projects, project.path)
    assert opened.path == project.path
    assert harness.mode.mode == AppMode.OFFLINE
    # the failed connect did not flip the mode back
    assert failing_connection.status == ConnectionStatus.ERROR
    assert harness.mode.mode == AppMode.OFFLINE


def test_open_project_offline_error_propagates_and_leaves_mode_unchanged(offline_harness):
    harness = offline_harness
    harness.projects.create_project("M 42")
    with pytest.raises(ProjectError):
        harness.mode.open_project_offline(
            harness.projects, harness.tmp_path / "no-such-project"
        )
    assert harness.mode.mode == AppMode.CONNECTED
    assert harness.projects.current is not None  # unchanged


def test_open_project_offline_same_project_twice_stays_offline(offline_harness):
    harness = offline_harness
    project = harness.projects.create_project("M 42")
    harness.mode.open_project_offline(harness.projects, project.path)
    assert harness.mode.mode == AppMode.OFFLINE
    # re-opening the SAME project offline: the project event fires with an
    # unchanged current path — the guard keeps the mode offline
    harness.mode.open_project_offline(harness.projects, project.path)
    assert harness.mode.mode == AppMode.OFFLINE


# --- returning to connected mode ---------------------------------------------------


def test_normal_project_open_returns_to_connected_mode(offline_harness):
    harness = offline_harness
    first = harness.projects.create_project("M 42")
    second = create_project("M 31", root=harness.projects.root)
    harness.mode.open_project_offline(harness.projects, first.path)
    assert harness.mode.mode == AppMode.OFFLINE
    # a normal open (the Projects view's Open button path) -> connected
    harness.projects.open_project(second.path)
    assert harness.mode.mode == AppMode.CONNECTED


def test_project_list_refresh_keeps_offline_mode(offline_harness):
    """A mere list refresh (same current project — e.g. a project_root
    settings change) must NOT kick the user out of offline mode."""
    harness = offline_harness
    project = harness.projects.create_project("M 42")
    harness.mode.open_project_offline(harness.projects, project.path)
    assert harness.mode.mode == AppMode.OFFLINE
    harness.projects.refresh()
    assert harness.mode.mode == AppMode.OFFLINE


def test_successful_connect_returns_to_connected_mode(offline_harness):
    """With the FakeDevice (link up), a successful connect switches the mode
    back to connected."""
    harness = offline_harness
    project = harness.projects.create_project("M 42")
    harness.mode.open_project_offline(harness.projects, project.path)
    assert harness.mode.mode == AppMode.OFFLINE
    harness.connection.connect()
    assert harness.connection.wait(5)
    assert harness.connection.status == ConnectionStatus.CONNECTED
    assert harness.mode.mode == AppMode.CONNECTED
    harness.connection.disconnect()
    # disconnecting alone does not switch to offline (documented semantics:
    # only the explicit offline open sets offline)
    assert harness.mode.mode == AppMode.CONNECTED
