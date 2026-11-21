"""Widget-layer tests for offline-analysis issue 01 — the offline mode core in
the app shell: the "Open project (offline)" path (``win.open-offline`` action +
:meth:`SeestarWindow.open_project_offline`), the mode flag on app + window, the
offline banner, and the gated scope actions (connect button, night controls,
live preview). These SKIP in the headless sandbox (no ``gi`` module, no GTK
system libs, no display; see docs/wiki/assumptions.md) and run on the desktop,
where the system packages ``python3-gi gir1.2-gtk-4.0 gir1.2-adw-1`` are
installed (or under ``xvfb-run -a uv run pytest``).

Guards, in order: ``pytest.importorskip("gi")`` (no PyGObject at all),
``gi.require_version`` for the Gtk 4.0 / Adw 1 typelibs, and a
``Gtk.init_check()`` fixture (no display -> skip) — the same pattern as
tests/test_ui_widgets.py / tests/test_gaia_overlay_widgets.py.

What is asserted when they run: the window starts ``connected`` with the
banner hidden and the ``win.open-offline`` action registered; opening a project
offline (link layer never touched — the connection stays disconnected with no
device) switches app + window to ``offline``, reveals the banner, gates the
header Connect button and the Observe view's night controls (insensitive +
offline tooltip) and hides the live preview, and shows the project as current
in the Projects view; the gating is re-applied after observe-controller state
changes; a normal project open (Projects view Open button, ``.emit("clicked")``)
switches back to ``connected`` and restores the banner/buttons/preview; and
with a fake connected device the Start button is sensitive while connected,
stays gated while offline, and is restored on return to connected.
"""

import pytest

gi = pytest.importorskip("gi")

try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
except ValueError as exc:  # typelibs missing
    pytest.skip(f"GTK 4.0 / libadwaita typelibs missing: {exc}", allow_module_level=True)

from gi.repository import Gtk

from seestar_controller.store import create_project
from seestar_controller.ui.connection import ConnectionController, ConnectionStatus
from seestar_controller.ui.mode import (
    OFFLINE_BANNER_TITLE,
    OFFLINE_PREVIEW_LABEL,
    OFFLINE_SCOPE_TOOLTIP,
    AppMode,
    ModeController,
)

#: The scope-dependent night-control buttons gated in offline mode.
NIGHT_BUTTONS = ("start_button", "pause_button", "skip_button", "stop_button", "park_button")


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


@pytest.fixture()
def app(gtk, tmp_path):
    """A SeestarApplication with its settings file in tmp_path and a scope IP
    set (pattern: tests/test_ui_widgets.py — without it ``connect()`` lands in
    the "scope IP is not set" error state)."""
    from seestar_controller.ui.app import SeestarApplication

    application = SeestarApplication(settings_path=str(tmp_path / "settings.json"))
    application.state.set_setting("scope_ip", "192.168.1.50")
    return application


@pytest.fixture()
def project_root(app, tmp_path):
    """Point the app's project root at a tmp dir and refresh the list."""
    root = tmp_path / "projects"
    root.mkdir()
    app.state.set_setting("project_root", str(root))
    app.state.save()
    app.project_controller.refresh()
    return root


def _observe_page(window):
    from seestar_controller.ui.views import ObserveView

    page = window.view_stack.get_child_by_name("observe")
    assert isinstance(page, ObserveView)
    return page


def _projects_page(window):
    from seestar_controller.ui.views import ProjectsView

    page = window.view_stack.get_child_by_name("projects")
    assert isinstance(page, ProjectsView)
    return page


# --- default (connected) state ----------------------------------------------------


def test_window_starts_connected_with_banner_hidden_and_open_offline_action(app, gtk):
    from seestar_controller.ui.app import SeestarWindow

    window = SeestarWindow(app, app.state)
    assert window.mode == AppMode.CONNECTED
    assert app.mode == AppMode.CONNECTED
    assert window.mode_controller is app.mode_controller
    assert window.offline_banner.get_revealed() is False
    assert window.offline_banner.get_title() == OFFLINE_BANNER_TITLE
    # the "Open project (offline)…" action is registered on the window
    assert window.lookup_action("open-offline") is not None
    # nothing gated while connected
    panel = window.connection_panel
    assert panel.scope_gated is False
    assert panel.button.get_sensitive() is True  # disconnected -> Connect enabled
    page = _observe_page(window)
    assert page.scope_gated is False
    widget = page.observe_widget
    assert widget.start_button.get_tooltip_text() == "Start the queue against the connected scope"
    assert widget.picture.get_visible() is True


# --- offline open: banner + gating -------------------------------------------------


def test_open_project_offline_gates_everything_and_never_touches_link(app, gtk, project_root):
    from seestar_controller.ui.app import SeestarWindow

    project = create_project("M 42", root=project_root)
    # the project was created on disk behind the controller's back — re-list
    # so the Projects view renders the row (open_project does not re-list)
    app.project_controller.refresh()
    window = SeestarWindow(app, app.state)

    opened = window.open_project_offline(project.path)

    assert opened.path == project.path
    assert window.mode == AppMode.OFFLINE
    assert app.mode == AppMode.OFFLINE
    assert app.project_controller.current is not None
    assert app.project_controller.current.path == project.path
    # banner revealed
    assert window.offline_banner.get_revealed() is True
    # the link layer was never touched: still disconnected, no device
    assert window.connection_controller.status == ConnectionStatus.DISCONNECTED
    assert window.connection_controller.device is None
    # connect button gated
    panel = window.connection_panel
    assert panel.scope_gated is True
    assert panel.button.get_sensitive() is False
    assert panel.button.get_tooltip_text() == OFFLINE_SCOPE_TOOLTIP
    # night controls gated + live preview hidden
    page = _observe_page(window)
    assert page.scope_gated is True
    widget = page.observe_widget
    for name in NIGHT_BUTTONS:
        button = getattr(widget, name)
        assert button.get_sensitive() is False, name
        assert button.get_tooltip_text() == OFFLINE_SCOPE_TOOLTIP, name
    assert widget.picture.get_visible() is False
    assert widget.preview_label.get_label() == OFFLINE_PREVIEW_LABEL
    # the opened project is visible as current in the Projects view (the
    # offline open switches to it)
    assert window.state.current_view_id == "projects"
    projects_page = _projects_page(window)
    assert projects_page.row_count() == 1
    assert projects_page.is_current_row(0)


def test_gating_is_reapplied_after_observe_state_changes(app, gtk, project_root):
    """The observe widget re-renders its buttons on every controller state
    change; the offline gating must be re-applied after each of them."""
    from seestar_controller.ui.app import SeestarWindow

    project = create_project("M 42", root=project_root)
    window = SeestarWindow(app, app.state)
    window.open_project_offline(project.path)
    widget = _observe_page(window).observe_widget
    # a state change (project pushed to the observe controller) re-renders the
    # widget — the gating listener runs after and re-disables the buttons
    window.observe_controller.set_project(window.project_controller.current)
    for name in NIGHT_BUTTONS:
        button = getattr(widget, name)
        assert button.get_sensitive() is False, name
        assert button.get_tooltip_text() == OFFLINE_SCOPE_TOOLTIP, name
    assert widget.picture.get_visible() is False


# --- back to connected -----------------------------------------------------------------


def test_normal_project_open_ungates_and_restores(app, gtk, project_root):
    from seestar_controller.ui.app import SeestarWindow

    first = create_project("M 42", root=project_root)
    create_project("M 31", root=project_root)
    # re-list so the Projects view has both rows to click (open_project does
    # not re-list the controller's project tuple)
    app.project_controller.refresh()
    window = SeestarWindow(app, app.state)
    window.open_project_offline(first.path)
    assert window.mode == AppMode.OFFLINE

    # the Projects view's Open button (normal open) -> connected mode
    _projects_page(window).click_open(1)  # .emit("clicked") internally

    assert window.mode == AppMode.CONNECTED
    assert window.offline_banner.get_revealed() is False
    panel = window.connection_panel
    assert panel.scope_gated is False
    assert panel.button.get_sensitive() is True  # disconnected -> Connect
    assert panel.button.get_tooltip_text() != OFFLINE_SCOPE_TOOLTIP
    page = _observe_page(window)
    assert page.scope_gated is False
    widget = page.observe_widget
    # base sensitivity restored (start is insensitive only because there is
    # no connection — its original tooltip is back)
    assert widget.start_button.get_tooltip_text() == "Start the queue against the connected scope"
    assert widget.picture.get_visible() is True
    assert widget.preview_label.get_label() == ""


# --- with a (fake) connected scope -------------------------------------------------------


def test_start_button_sensitive_when_connected_but_gated_while_offline(app, gtk, project_root):
    """With the link up (FakeDevice — no network), Start night is sensitive;
    opening offline gates it (and keeps it gated across state changes), and
    returning to connected restores it."""
    from conftest import FakeDevice

    from seestar_controller.observing import ObserveController, PlanQueueController
    from seestar_controller.ui.app import SeestarWindow
    from seestar_controller.ui.notifications import NotificationBus

    device = FakeDevice()
    connection = ConnectionController(
        app.settings_controller, device_factory=lambda ip, key_path: device
    )
    queue = PlanQueueController()
    observe = ObserveController(connection, queue, NotificationBus())
    mode = ModeController()
    mode.attach_connection(connection)
    mode.attach_project_controller(app.project_controller)
    window = SeestarWindow(
        app,
        app.state,
        connection_controller=connection,
        queue_controller=queue,
        observe_controller=observe,
        mode_controller=mode,
    )
    project = create_project("M 42", root=project_root)
    other = create_project("M 31", root=project_root)

    connection.connect()
    assert connection.wait(5)
    assert connection.status == ConnectionStatus.CONNECTED
    assert mode.mode == AppMode.CONNECTED  # successful connect -> connected
    observe.set_project(project)
    widget = _observe_page(window).observe_widget
    assert widget.start_button.get_sensitive() is True  # connected + project

    window.open_project_offline(project.path)
    assert mode.mode == AppMode.OFFLINE
    assert widget.start_button.get_sensitive() is False
    assert widget.start_button.get_tooltip_text() == OFFLINE_SCOPE_TOOLTIP
    # the offline open did NOT touch the link: still connected, device alive
    assert connection.status == ConnectionStatus.CONNECTED
    assert connection.device is device
    # state changes while offline keep the button gated
    observe.set_project(project)
    assert widget.start_button.get_sensitive() is False

    # a normal open returns to connected and restores the button
    app.project_controller.open_project(other.path)
    assert mode.mode == AppMode.CONNECTED
    assert window.offline_banner.get_revealed() is False
    observe.set_project(other)
    assert widget.start_button.get_sensitive() is True
    assert widget.start_button.get_tooltip_text() == "Start the queue against the connected scope"
