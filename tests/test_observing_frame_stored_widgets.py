"""Widget-layer tests for observing ticket 07 — the Projects view's
stored-data browser refreshes live when a frame is stored mid-night (no
project reopen). These SKIP in the headless sandbox (no ``gi`` module, no GTK
system libs, no display; see docs/wiki/assumptions.md) and run on the
desktop (or under ``xvfb-run -a uv run pytest``) — the same guard pattern as
tests/test_observing_widgets.py / tests/test_ui_projects_stack_subs.py.

The chain under test: :class:`~seestar_controller.observing.SessionCapture`
fires a frame-stored event on every frame it stores (``poll_once`` here, the
worker loop in the app), the
:class:`~seestar_controller.observing.ObserveController` re-emits it via
``invoke_on_main`` (``GLib.idle_add`` in the app), and the
:class:`~seestar_controller.ui.views.ProjectsView` re-renders its stored-data
browser (``_rebuild_data``) on the main loop — so each stored sub appears as
a frame row and the session subtitle's subs/stacks counts update while the
night runs.

Two levels: a standalone ``build_view("projects", ...)`` view wired to a real
observe controller over a FakeDevice (frames stored by hand via
``poll_once``), and a full window (``SeestarWindow`` + ``open_project_offline``
+ ``start_night``) where the night stores frames from its worker threads.
Device double: the ``FakeDevice`` from tests/test_observing_capture.py. No
network; no widget is ever ``destroy()``-ed (GTK4).
"""

import re
import time
from types import SimpleNamespace

import pytest
from test_observing_capture import (
    FakeDevice,
    FakePreviewStream,
    coord_entry,
)

gi = pytest.importorskip("gi")

try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
except ValueError as exc:  # typelibs missing
    pytest.skip(f"GTK 4.0 / libadwaita typelibs missing: {exc}", allow_module_level=True)

from gi.repository import GLib, Gtk  # ty: ignore[missing-module-member]

from seestar_controller.observing import ExecutorState
from seestar_controller.store import (
    KIND_STACK,
    KIND_SUB,
    close_session,
    create_project,
    start_session,
)


def _pump() -> None:
    """Run all pending GLib main-context callbacks (the app's
    ``invoke_on_main`` is ``GLib.idle_add``)."""
    ctx = GLib.MainContext.default()
    while ctx.pending():
        ctx.iteration(False)


def _pump_until(predicate, timeout: float = 20.0) -> bool:
    """Pump the main context until ``predicate()`` is true."""
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while ctx.pending():
            ctx.iteration(False)
        if predicate():
            return True
        time.sleep(0.02)
    while ctx.pending():
        ctx.iteration(False)
    return predicate()


@pytest.fixture()
def gtk():
    """GTK initialized, or skip when no display is available (headless)."""
    try:
        ok = Gtk.init_check()
    except Exception as exc:  # pragma: no cover - backend errors  # noqa: BLE001
        pytest.skip(f"Gtk.init_check failed: {exc}")
    if not ok:
        pytest.skip("no display available (headless environment)")
    return Gtk


def _idle_on_main():
    """The app's ``invoke_on_main`` binding."""
    return lambda cb: GLib.idle_add(cb)


def _subtitle_counts(page) -> tuple[int, int]:
    """The (subs, stacks) counts of the session row's subtitle label."""
    subtitle = page.session_row_subtitle(0)
    subs_match = re.search(r"(\d+) subs", subtitle)
    stacks_match = re.search(r"(\d+) stacks", subtitle)
    assert subs_match is not None and stacks_match is not None, subtitle
    return int(subs_match.group(1)), int(stacks_match.group(1))


def _observe_controller(state, device):
    """A real ObserveController over a fake-device connection, marshalling
    via ``GLib.idle_add`` like the app."""
    from seestar_controller.observing import ObserveController
    from seestar_controller.observing.queue_controller import PlanQueueController
    from seestar_controller.ui.connection import ConnectionController
    from seestar_controller.ui.notifications import NotificationBus
    from seestar_controller.ui.settings import SettingsController

    connection = ConnectionController(
        SettingsController(state), device_factory=lambda ip, key_path: device
    )
    connection.connect()
    assert connection.wait(2.0)
    queue_controller = PlanQueueController()
    controller = ObserveController(
        connection,
        queue_controller,
        NotificationBus(),
        preview_stream_factory=lambda d, host: FakePreviewStream(),
        invoke_on_main=_idle_on_main(),
    )
    return controller, queue_controller


def _slow_capture_factory(device, project, session_id, executor, **kwargs):
    """A capture whose worker never polls on its own (tests drive
    ``poll_once`` by hand)."""
    from seestar_controller.observing import SessionCapture

    kwargs["poll_interval_s"] = 60.0
    return SessionCapture(device, project, session_id, executor=executor, **kwargs)


@pytest.fixture()
def live_view(gtk, tmp_path):
    """A standalone Projects view wired to a real observe controller over a
    FakeDevice: project open, session started, capture attached."""
    from seestar_controller.ui.project_controller import ProjectController
    from seestar_controller.ui.state import AppState
    from seestar_controller.ui.views import build_view

    root = tmp_path / "projects"
    root.mkdir()
    state = AppState(tmp_path / "settings.json")
    state.set_setting("project_root", str(root))
    state.set_setting("scope_ip", "10.9.8.7")
    device = FakeDevice()
    controller, _queue = _observe_controller(state, device)
    controller._capture_factory = _slow_capture_factory

    project_controller = ProjectController(state)
    project = project_controller.create_project("Live Night")
    session = start_session(project)
    view = build_view(
        "projects",
        project_controller=project_controller,
        observe_controller=controller,
        invoke_on_main=_idle_on_main(),
    )
    controller._start_capture(device, project, session, None)
    capture = controller.capture
    assert capture is not None
    yield SimpleNamespace(
        view=view,
        device=device,
        capture=capture,
        controller=controller,
        project_controller=project_controller,
        project=project,
        session=session,
    )
    controller._stop_capture()
    close_session(project, session)
    project.close()


def test_stored_data_browser_refreshes_live_when_a_frame_is_stored_during_a_session(
    gtk, live_view
):
    """The ticket's acceptance: with a session running, each stored sub
    appears in Stored data within a poll cycle, and the session subtitle
    counts update — without reopening the project."""
    page = live_view.view
    assert page.observe_controller is live_view.controller
    assert page.session_count() == 1
    assert page.frame_row_count(0) == 0
    assert "0 subs" in page.session_row_subtitle(0)

    # two subs land mid-night and the capture stores them
    live_view.device.add_frame(KIND_SUB)
    live_view.device.add_frame(KIND_SUB)
    assert live_view.capture.poll_once() == 2
    _pump()  # frame-stored events are marshalled via GLib.idle_add
    assert page.frame_row_count(0) == 2
    assert "2 subs" in page.session_row_subtitle(0)
    assert "sub #" in page.frame_row_label(0, 0)
    # no project reopen: the current project is still the same object
    assert page.controller.current is live_view.project

    # a stack lands too — the counts update live again
    live_view.device.add_frame(KIND_STACK)
    assert live_view.capture.poll_once() == 1
    _pump()
    assert page.frame_row_count(0) == 3
    subtitle = page.session_row_subtitle(0)
    assert "2 subs" in subtitle
    assert "1 stacks" in subtitle
    assert "stack #" in page.frame_row_label(0, 2)
    assert page.controller.current is live_view.project


@pytest.fixture()
def app(gtk, tmp_path):
    """A SeestarApplication with its settings file in tmp_path."""
    from seestar_controller.ui.app import SeestarApplication

    application = SeestarApplication(settings_path=str(tmp_path / "settings.json"))
    application.state.set_setting("scope_ip", "10.9.8.7")
    return application


def test_window_projects_page_refreshes_live_during_a_running_night(app, gtk, tmp_path):
    """End to end at window level: a night started on the shared observe
    controller stores frames from its worker threads, and the Projects page
    (opened offline before the night) shows the session, its frame rows, and
    the live counts — without any project reopen."""
    from seestar_controller.ui.app import SeestarWindow
    from seestar_controller.ui.views import ProjectsView

    root = tmp_path / "projects"
    root.mkdir()
    app.state.set_setting("project_root", str(root))
    app.state.save()
    app.project_controller.refresh()

    device = FakeDevice()
    device.auto_frames = True
    observe, queue_controller = _observe_controller(app.state, device)
    window = SeestarWindow(
        app, app.state, queue_controller=queue_controller, observe_controller=observe
    )
    assert window.observe_controller is observe

    project = create_project("Live Night", root=root)
    window.open_project_offline(project.path)
    _pump()
    page = window.view_stack.get_child_by_name("projects")
    assert isinstance(page, ProjectsView)
    assert page.observe_controller is observe
    assert page.session_count() == 0  # no session yet
    current_before = page.controller.current  # the opened project, as the controller holds it
    assert current_before is not None and current_before.path == project.path

    queue_controller.add_entry(coord_entry(5.5, -5.0))
    observe.start_night(project, frames_per_target=2)
    assert _pump_until(
        lambda: observe.executor is not None
        and observe.executor.state in (ExecutorState.DONE, ExecutorState.ABORTED),
        timeout=20.0,
    )
    # the stored frames re-rendered the browser live during the night
    assert _pump_until(lambda: page.session_count() == 1, timeout=10.0)
    assert _pump_until(lambda: _subtitle_counts(page)[0] >= 2, timeout=10.0)
    assert _pump_until(lambda: _subtitle_counts(page)[1] >= 1, timeout=10.0)
    subs, stacks = _subtitle_counts(page)
    assert subs >= 2  # the subs stored mid-night
    assert stacks >= 1  # the final Stack landed too
    assert page.frame_row_count(0) == subs + stacks  # rows and live counts agree
    assert "sub #" in page.frame_row_label(0, 0)
    # still no reopen: the controller's current project never changed
    assert page.controller.current is current_before
    assert app.project_controller.current is current_before

    project.close()
