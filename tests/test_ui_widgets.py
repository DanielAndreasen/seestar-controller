"""Widget-layer smoke tests for the ui shell — these SKIP in the headless
sandbox (no ``gi`` module, no GTK system libs, no display; see
docs/wiki/assumptions.md) and run on the desktop, where the system packages
``python3-gi gir1.2-gtk-4.0 gir1.2-adw-1`` are installed.

Guards, in order: ``pytest.importorskip("gi")`` (no PyGObject at all),
``gi.require_version`` for the Gtk 4.0 / Adw 1 typelibs, and a
``Gtk.init_check()`` fixture (no display -> skip). Dummy-backend note: when
``gi`` is importable but there is no display, ``Gtk.init_check()`` returns
False and these tests skip; on the desktop they run against a real display or
a virtual one (``xvfb-run -a uv run pytest``), which stands in for a dummy
GTK backend. What is asserted when they run: the ticket-05 smoke test
constructs EVERY view class and adds it to an ``Adw.ViewStack`` (no exception,
expected widget types, one page per view id) and instantiates the application
window holding the five stack pages; sidebar selection switches the view and
updates the pure-layer state, the dark-theme helper drives the Adw style
manager, geometry capture/restore round-trips through the pure settings
layer, the main window opens maximized (``is_maximized()`` right after
construction — a restored smaller geometry stays the default size but cannot
un-maximize the window), and the settings dialog (ticket 02) live-validates and persists
through the pure ``SettingsController``, and the header-bar connection panel
(ticket 03) reflects the pure ``ConnectionController`` states
(connect/disconnect button + status label, error styling) and shares the
app's controller, and the notification shell (ticket 04) is wired: the
window content sits in an ``Adw.ToastOverlay`` fed by the shared pure
``NotificationBus`` through a ``NotificationToasts`` presenter (non-modal
toasts, severity-based timeouts, main-thread marshalling), the header bar
has a notifications button opening the user-invoked ``NotificationLogDialog``
(bus log, newest first, Clear), and the app owns the shared bus +
``NotificationController``. The fit tests assert that every view page wraps
its content in a vertical ``Gtk.ScrolledWindow`` with a small minimum content
height (pages scroll instead of demanding their full natural height) and that
the window's program-specified minimum size stays well under 1600x900, so the
WM can actually apply the maximize request on laptop screens.
"""

import importlib.util
import subprocess
import sys

import pytest

gi = pytest.importorskip("gi")

try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
except ValueError as exc:  # typelibs missing
    pytest.skip(f"GTK 4.0 / libadwaita typelibs missing: {exc}", allow_module_level=True)

from gi.repository import Adw, Gtk  # noqa: E402

from seestar_controller.ui import state as ui_state  # noqa: E402


def _has_ancestor(widget, cls) -> bool:
    """True when ``cls`` appears among ``widget``'s ancestors (Adw.HeaderBar
    packs children into an internal Gtk.Box, so get_parent() alone is not
    enough)."""
    parent = widget.get_parent()
    while parent is not None:
        if isinstance(parent, cls):
            return True
        parent = parent.get_parent()
    return False


@pytest.fixture()
def gtk():
    """GTK initialized, or skip when no display is available (headless)."""
    try:
        ok = Gtk.init_check()
    except Exception as exc:  # pragma: no cover - backend errors
        pytest.skip(f"Gtk.init_check failed: {exc}")
    if not ok:
        pytest.skip("no display available (headless environment)")
    return Gtk


@pytest.fixture()
def app(gtk, tmp_path):
    """A SeestarApplication with its settings file in tmp_path."""
    from seestar_controller.ui.app import SeestarApplication

    return SeestarApplication(settings_path=str(tmp_path / "settings.json"))


def test_every_view_widget_constructs(gtk):
    from seestar_controller.ui.views import VIEW_CLASSES, build_view

    assert set(VIEW_CLASSES) == set(ui_state.VIEW_IDS)
    for view in ui_state.VIEWS:
        widget = build_view(view.id)
        assert isinstance(widget, Gtk.Box)
        assert widget.view_id == view.id
        assert widget.title == view.title
    with pytest.raises(ui_state.UnknownViewError):
        build_view("nope")


def test_smoke_every_view_constructs_and_adds_to_a_view_stack(gtk):
    """Ticket-05 smoke test: construct EVERY view class, add each to an
    Adw.ViewStack — no exception, expected widget types, one page per view."""
    from seestar_controller.ui.views import VIEW_CLASSES

    stack = Adw.ViewStack()
    for view in ui_state.VIEWS:
        widget = VIEW_CLASSES[view.id]()
        assert type(widget) is VIEW_CLASSES[view.id]
        assert isinstance(widget, Gtk.Box)
        assert widget.view_id == view.id
        assert widget.title == view.title
        stack.add_titled(widget, view.id, view.title)  # must not raise
    assert stack.get_pages().get_n_items() == len(ui_state.VIEWS)
    for view in ui_state.VIEWS:
        page = stack.get_child_by_name(view.id)
        assert page is not None
        assert page.view_id == view.id
        assert stack.get_page(page).get_title() == view.title


def test_window_has_five_stack_pages_and_sidebar_switches_views(app, gtk):
    from seestar_controller.ui.app import SeestarWindow

    window = SeestarWindow(app, app.state)
    stack = window.view_stack
    for view in ui_state.VIEWS:
        assert stack.get_child_by_name(view.id) is not None
    assert stack.get_visible_child_name() == app.state.current_view_id == "observe"

    for view_id in ui_state.VIEW_IDS:
        window.select_view(view_id)
        assert stack.get_visible_child_name() == view_id
        assert app.state.current_view_id == view_id

    with pytest.raises(ui_state.UnknownViewError):
        window.select_view("nope")


def test_dark_theme_drives_the_style_manager(app, gtk):
    from seestar_controller.ui.app import apply_dark_theme

    apply_dark_theme(True)
    assert Adw.StyleManager.get_default().get_color_scheme() == Adw.ColorScheme.FORCE_DARK
    apply_dark_theme(False)
    assert Adw.StyleManager.get_default().get_color_scheme() == Adw.ColorScheme.FORCE_LIGHT


def test_geometry_capture_and_restore_round_trip(app, gtk, tmp_path):
    from seestar_controller.ui.app import SeestarWindow, _capture_geometry, _restore_geometry

    window = SeestarWindow(app, app.state)
    window.set_default_size(1024, 700)
    geometry = _capture_geometry(window)
    assert geometry["width"] >= 1 and geometry["height"] >= 1

    # persist through the pure layer, then re-apply to a fresh window
    app.state.set_window_geometry(
        geometry["width"], geometry["height"], geometry["x"], geometry["y"]
    )
    app.state.save()
    reloaded = ui_state.load_state(str(tmp_path / "settings.json"))
    assert reloaded.window_geometry() == app.state.window_geometry()

    window2 = SeestarWindow(app, reloaded)
    assert window2.get_default_size() == (reloaded.window_width, reloaded.window_height)
    _restore_geometry(window2, reloaded.window_geometry())  # must not raise


def test_window_opens_maximized(app, gtk):
    """The main window opens maximized (user request: "full screen
    (maximized)", NOT fullscreen): maximize() is requested in the window
    constructor, after the geometry restore. On GTK 4.14 ``is_maximized()``
    is already True for an unrealized window carrying a pending maximize
    request, so this is observable pre-map (no present/main-loop pump)."""
    from seestar_controller.ui.app import SeestarWindow

    window = SeestarWindow(app, app.state)
    assert window.is_maximized()
    # sensible default (unmaximized) size: the state default 1280x800
    assert window.get_default_size() == (
        ui_state.DEFAULT_WINDOW_WIDTH,
        ui_state.DEFAULT_WINDOW_HEIGHT,
    )


def test_view_pages_scroll_instead_of_demanding_full_height(app, gtk):
    """Every view page wraps its content in a vertical Gtk.ScrolledWindow
    whose minimum height is only PAGE_MIN_CONTENT_HEIGHT — a page scrolls
    instead of forcing the window to demand its full natural height (a page
    minimum taller than the workarea is what made the WM drop the maximize
    request, leaving controls off-screen on laptop screens)."""
    from seestar_controller.ui.app import SeestarWindow
    from seestar_controller.ui.views import PAGE_MIN_CONTENT_HEIGHT

    window = SeestarWindow(app, app.state)
    stack = window.view_stack
    for view in ui_state.VIEWS:
        page = stack.get_child_by_name(view.id)
        assert page is not None
        scrolled = page.scrolled
        assert isinstance(scrolled, Gtk.ScrolledWindow)
        assert page.get_first_child() is scrolled  # the page's only child
        # GTK4 wraps a non-scrollable child in a Gtk.Viewport automatically
        inner = scrolled.get_child()
        if isinstance(inner, Gtk.Viewport):
            inner = inner.get_child()
        assert inner is page.content
        assert scrolled.props.hscrollbar_policy == Gtk.PolicyType.NEVER
        assert scrolled.props.vscrollbar_policy == Gtk.PolicyType.AUTOMATIC
        assert scrolled.props.min_content_height == PAGE_MIN_CONTENT_HEIGHT
        minimum, _natural = page.get_preferred_size()
        assert minimum.height <= PAGE_MIN_CONTENT_HEIGHT + 160  # small slack
        assert minimum.width <= 1200


def test_window_minimum_size_fits_a_laptop_screen(app, gtk):
    """The window's program-specified minimum size must stay well under a
    1600x900 laptop screen: when the minimum exceeds the workarea, the WM
    (marco) leaves _NET_WM_STATE empty and the maximize request is silently
    not applied."""
    from seestar_controller.ui.app import SeestarWindow

    window = SeestarWindow(app, app.state)
    minimum, _natural = window.get_preferred_size()
    assert minimum.width < 1600
    assert minimum.height < 900


def test_restored_smaller_geometry_does_not_unmaximize_the_window(app, gtk, tmp_path):
    """A saved smaller size is still restored as the default size, but it
    cannot un-maximize the window on open: maximize() is requested after
    _restore_geometry in the constructor."""
    from seestar_controller.ui.app import SeestarWindow

    state = ui_state.AppState(str(tmp_path / "settings.json"))
    state.set_window_geometry(640, 480, 10, 20)
    window = SeestarWindow(app, state)
    assert window.is_maximized()  # maximize still wins on open
    assert window.get_default_size() == (640, 480)  # restored size kept as default


def test_close_request_persists_state(app, gtk, tmp_path):
    from seestar_controller.ui.app import SeestarWindow

    settings_path = tmp_path / "settings.json"
    app2_state = ui_state.AppState(settings_path)
    window = SeestarWindow(app, app2_state)
    window.select_view("photometry")
    assert window._on_close_request() is False  # close allowed
    on_disk = ui_state.load_state(str(settings_path))
    assert on_disk.current_view_id == "photometry"
    assert on_disk.window_geometry()["width"] >= 1


def test_widget_layer_is_reachable_lazily_from_the_package(gtk):
    import seestar_controller.ui as ui

    assert "gi" in sys.modules  # the widget layer really imported gi here
    assert ui.SeestarApplication is not None
    assert ui.build_view("observe") is not None
    assert ui.SettingsDialog is not None
    assert set(ui.VIEW_CLASSES) == set(ui_state.VIEW_IDS)


# --- settings dialog (ticket 02) ------------------------------------------------------


def _settings_controller(tmp_path):
    from seestar_controller.ui.settings import SettingsController

    state = ui_state.AppState(str(tmp_path / "settings.json"))
    state.set_setting("project_root", str(tmp_path))  # an existing directory
    return SettingsController(state), state


def test_settings_dialog_live_validation_and_save(app, gtk, tmp_path):
    from seestar_controller.ui.settings_dialog import SettingsDialog

    controller, state = _settings_controller(tmp_path)
    dialog = SettingsDialog(controller)

    # entries are initialized from the controller values; defaults are valid
    # (the dialog edits only the editable keys — last_project is app-managed)
    from seestar_controller.ui.state import EDITABLE_SETTINGS_KEYS

    assert dialog.values() == {
        key: value for key, value in controller.values.items() if key in EDITABLE_SETTINGS_KEYS
    }
    assert dialog.save_sensitive()

    # invalid IP -> error label visible, save blocked, nothing persisted
    dialog.set_field("scope_ip", "999.1.1.1")
    label = dialog.error_label_for("scope_ip")
    assert label.get_visible() and "scope_ip" in label.get_label()
    assert not dialog.save_sensitive()
    assert dialog.save() is False
    assert not state.settings_path.exists()

    # valid again -> save enabled; save persists + notifies listeners
    seen = []
    controller.add_listener(seen.append)
    dialog.set_field("scope_ip", "192.168.1.50")
    assert dialog.save_sensitive()
    assert dialog.save() is True
    assert seen == [controller.values]
    reloaded = ui_state.load_state(str(state.settings_path))
    assert reloaded.get_setting("scope_ip") == "192.168.1.50"
    assert reloaded.get_setting("project_root") == str(tmp_path)


def test_settings_dialog_flags_missing_key_file_but_save_allowed(app, gtk, tmp_path):
    from seestar_controller.ui.settings_dialog import SettingsDialog

    controller, state = _settings_controller(tmp_path)
    dialog = SettingsDialog(controller)
    missing = tmp_path / "missing.pem"
    dialog.set_field("key_path", str(missing))
    label = dialog.error_label_for("key_path")
    assert label.get_visible() and "not found" in label.get_label()
    assert dialog.save_sensitive()  # a soft flag never blocks saving
    assert dialog.save() is True
    reloaded = ui_state.load_state(str(state.settings_path))
    assert reloaded.get_setting("key_path") == str(missing)


def test_settings_dialog_missing_project_root_blocks_save(app, gtk, tmp_path):
    from seestar_controller.ui.settings_dialog import SettingsDialog

    controller, state = _settings_controller(tmp_path)
    dialog = SettingsDialog(controller)
    dialog.set_field("project_root", str(tmp_path / "no-such-dir"))
    label = dialog.error_label_for("project_root")
    assert label.get_visible() and "not an existing directory" in label.get_label()
    assert not dialog.save_sensitive()
    assert dialog.save() is False
    assert not state.settings_path.exists()


def test_window_header_has_settings_action_and_shared_controller(app, gtk):
    from seestar_controller.ui.app import SeestarWindow
    from seestar_controller.ui.settings_dialog import SettingsDialog

    window = SeestarWindow(app, app.state)
    assert window.lookup_action("settings") is not None
    assert window.settings_controller is app.settings_controller
    window._on_settings_action()  # presents the dialog without raising
    assert isinstance(window._settings_dialog, SettingsDialog)
    assert window._settings_dialog.controller is app.settings_controller


def test_python_m_ui_app_module_exists():
    # `python -m seestar_controller.ui.app` must be a valid module path even
    # headless (it fails at `import gi`, not at module resolution)
    code = (
        "import importlib.util, sys;"
        "spec = importlib.util.find_spec('seestar_controller.ui.app');"
        "assert spec is not None;"
        "print('found')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert "found" in result.stdout


# --- connection panel (ticket 03) ------------------------------------------------------


class _FakeScopeState:
    """Duck-typed stand-in for ``link.device.DeviceState``."""

    def __init__(self, mode="idle", target=(5.5, 12.25)):
        self.mode = mode
        self.target = target


class _FakeDevice:
    """Minimal stand-in for the link facade — widget tests never touch the
    network; the pure controller only needs state/handlers/disconnect."""

    def __init__(self, scope_state=None):
        self._scope_state = scope_state or _FakeScopeState()
        self.handlers = {}
        self.disconnected = False

    def state(self):
        return self._scope_state

    def add_event_handler(self, name, handler):
        self.handlers[name] = handler

    def remove_event_handler(self, name):
        self.handlers.pop(name, None)

    def disconnect(self):
        self.disconnected = True


def _connection_controller(tmp_path, device_factory):
    from seestar_controller.ui.connection import ConnectionController
    from seestar_controller.ui.settings import SettingsController

    state = ui_state.AppState(str(tmp_path / "settings.json"))
    state.set_setting("scope_ip", "192.168.1.50")
    state.set_setting("project_root", str(tmp_path))
    # default invoke_on_main (direct call): no GLib loop needed in the test
    return ConnectionController(SettingsController(state), device_factory=device_factory)


def test_header_connection_panel_reflects_controller_states(app, gtk, tmp_path):
    from seestar_controller.ui.connection import ConnectionStatus
    from seestar_controller.ui.connection_panel import ConnectionPanel

    device = _FakeDevice()
    controller = _connection_controller(tmp_path, lambda ip, key_path: device)
    panel = ConnectionPanel(controller)
    assert panel.controller is controller

    # disconnected: "Connect" button + "Disconnected" status
    assert panel.button.get_child().get_label() == "Connect"
    assert panel.status_label.get_label() == "Disconnected"
    assert not panel.button.has_css_class("suggested-action")
    assert panel.button.get_sensitive()

    # click -> connect -> connected: "Disconnect" + suggested-action, and the
    # status line shows the scope's current state (mode + target)
    panel.button.emit("clicked")
    assert controller.wait(5)
    assert controller.status == ConnectionStatus.CONNECTED
    assert panel.button.get_child().get_label() == "Disconnect"
    assert panel.button.has_css_class("suggested-action")
    label = panel.status_label.get_label()
    assert "Connected to 192.168.1.50" in label
    assert "mode idle" in label and "5.50" in label

    # click again -> disconnect
    panel.button.emit("clicked")
    assert controller.status == ConnectionStatus.DISCONNECTED
    assert panel.button.get_child().get_label() == "Connect"
    assert not panel.button.has_css_class("suggested-action")
    assert panel.status_label.get_label() == "Disconnected"
    assert device.disconnected


def test_header_connection_panel_error_state(app, gtk, tmp_path):
    from seestar_controller.ui.connection import ConnectionStatus
    from seestar_controller.ui.connection_panel import ConnectionPanel

    def failing_factory(ip, key_path):
        raise OSError("connection refused")

    controller = _connection_controller(tmp_path, failing_factory)
    panel = ConnectionPanel(controller)
    panel.button.emit("clicked")
    assert controller.wait(5)
    assert controller.status == ConnectionStatus.ERROR
    assert panel.button.get_child().get_label() == "Retry"
    assert panel.button.has_css_class("error")
    assert panel.status_label.has_css_class("error")
    assert "connection refused" in panel.status_label.get_label()


def test_window_header_has_connection_panel_and_shared_controller(app, gtk):
    from seestar_controller.ui.app import SeestarWindow

    window = SeestarWindow(app, app.state)
    assert window.connection_controller is app.connection_controller
    assert window.connection_panel.controller is app.connection_controller
    # the panel is packed into the header bar (left side)
    assert _has_ancestor(window.connection_panel, Adw.HeaderBar)


# --- notifications + error surfacing (ticket 04) ---------------------------------------


class _FakeToastOverlay:
    """Duck-typed stand-in for ``Adw.ToastOverlay`` — records add_toast."""

    def __init__(self):
        self.toasts = []

    def add_toast(self, toast):
        self.toasts.append(toast)


def _notification(severity, title, message="boom", source="connection"):
    from seestar_controller.ui.notifications import Notification

    return Notification(severity, title, message, source)


def test_window_wraps_content_in_toast_overlay_and_shares_the_bus(app, gtk):
    from seestar_controller.ui.app import SeestarWindow
    from seestar_controller.ui.toast import NotificationToasts

    window = SeestarWindow(app, app.state)
    # the whole content sits inside an Adw.ToastOverlay (non-modal toasts)
    assert isinstance(window.toast_overlay, Adw.ToastOverlay)
    assert window.get_content() is window.toast_overlay
    assert isinstance(window.toast_overlay.get_child(), Adw.ToolbarView)
    # one shared bus + controller between app and window
    assert window.notification_bus is app.notification_bus
    assert window.notification_controller is app.notification_controller
    assert window.notification_controller.bus is window.notification_bus
    # the presenter listens on that bus and targets the overlay
    assert isinstance(window.toast_presenter, NotificationToasts)
    assert window.toast_presenter.bus is window.notification_bus
    assert window.toast_presenter.overlay is window.toast_overlay
    # header-bar notifications button + win.notifications action
    assert window.lookup_action("notifications") is not None
    assert _has_ancestor(window.notifications_button, Adw.HeaderBar)


def test_toast_presenter_adds_non_modal_toast_per_bus_push(gtk):
    from seestar_controller.ui.notifications import NotificationBus, NotificationSeverity
    from seestar_controller.ui.toast import TOAST_TIMEOUTS, NotificationToasts

    bus = NotificationBus()
    overlay = _FakeToastOverlay()
    opened = []
    presenter = NotificationToasts(
        bus, overlay, on_open_log=lambda: opened.append(True)
    )

    bus.push(_notification(NotificationSeverity.ERROR, "Connection error", "boom"))
    bus.push(_notification(NotificationSeverity.INFO, "Reconnected", "link is back"))
    assert len(overlay.toasts) == 2
    assert len(presenter.toasts) == 2  # test hook mirrors the overlay

    error_toast, info_toast = overlay.toasts
    assert error_toast.get_title() == "Connection error: boom"
    assert error_toast.get_timeout() == TOAST_TIMEOUTS[NotificationSeverity.ERROR] == 10
    assert info_toast.get_timeout() == TOAST_TIMEOUTS[NotificationSeverity.INFO] == 5
    assert TOAST_TIMEOUTS[NotificationSeverity.WARNING] == 7  # errors stay longest
    # the "View" button opens the log (user-invoked, never automatic)
    assert error_toast.get_button_label() == "View"
    error_toast.emit("button-clicked")
    assert opened == [True]

    # detach stops the presenter
    presenter.detach()
    bus.push(_notification(NotificationSeverity.WARNING, "Link lost", "reconnecting"))
    assert len(overlay.toasts) == 2


def test_toast_presenter_marshals_worker_thread_callbacks_via_invoke_on_main(gtk):
    from seestar_controller.ui.notifications import NotificationBus, NotificationSeverity
    from seestar_controller.ui.toast import NotificationToasts

    bus = NotificationBus()
    overlay = _FakeToastOverlay()
    queue = []
    presenter = NotificationToasts(bus, overlay, invoke_on_main=queue.append)

    # bus callbacks may come from worker threads: nothing is shown until the
    # main loop drains the queued callback
    bus.push(_notification(NotificationSeverity.WARNING, "Link lost", "reconnecting"))
    assert overlay.toasts == []
    assert len(queue) == 1
    while queue:
        queue.pop(0)()
    assert len(overlay.toasts) == 1
    assert overlay.toasts[0].get_title() == "Link lost: reconnecting"


def test_notification_log_dialog_lists_bus_log_newest_first_with_clear(gtk):
    from seestar_controller.ui.notification_log import NotificationLogDialog
    from seestar_controller.ui.notifications import NotificationBus, NotificationSeverity

    bus = NotificationBus()
    bus.push(_notification(NotificationSeverity.INFO, "Reconnected", "link is back"))
    bus.push(_notification(NotificationSeverity.ERROR, "Connection error", "boom"))
    dialog = NotificationLogDialog(bus)
    assert dialog.bus is bus

    # newest first, one row per entry, row carries the notification (hook)
    assert dialog.row_count() == 2
    rows = dialog.rows()
    assert rows[0].notification.title == "Connection error"
    assert rows[1].notification.title == "Reconnected"
    assert rows[0].notification.severity == NotificationSeverity.ERROR

    # Clear empties the bus log and refreshes the rows
    dialog.clear_button.emit("clicked")
    assert len(bus) == 0
    assert dialog.row_count() == 0

    # new entries appear on refresh
    bus.push(_notification(NotificationSeverity.WARNING, "Link lost", "reconnecting"))
    dialog.refresh()
    assert dialog.row_count() == 1
    assert dialog.rows()[0].notification.title == "Link lost"


def test_window_notifications_action_presents_log_dialog_on_shared_bus(app, gtk):
    from seestar_controller.ui.app import SeestarWindow
    from seestar_controller.ui.notification_log import NotificationLogDialog
    from seestar_controller.ui.notifications import NotificationSeverity

    window = SeestarWindow(app, app.state)
    app.notification_bus.push(
        _notification(NotificationSeverity.ERROR, "Connection error", "boom")
    )
    window._on_notifications_action()  # presents the dialog without raising
    dialog = window._notification_log_dialog
    assert isinstance(dialog, NotificationLogDialog)
    assert dialog.bus is app.notification_bus
    assert dialog.row_count() == 1  # the pushed error is listed


def test_notification_widgets_are_reachable_lazily_from_the_package(gtk):
    import seestar_controller.ui as ui

    assert ui.NotificationToasts is not None
    assert ui.NotificationLogDialog is not None
    # the pure names stay eagerly importable (no gi needed for them)
    assert ui.NotificationBus is not None
    assert ui.NotificationController is not None


# --- projects-view ticket 01 -----------------------------------------------------------


@pytest.fixture()
def project_state(tmp_path):
    """An AppState with the project root pointed at a tmp directory."""
    from seestar_controller.ui.state import AppState

    state = AppState(tmp_path / "settings.json")
    root = tmp_path / "projects"
    root.mkdir()
    state.set_setting("project_root", str(root))
    return state


def test_projects_view_lists_creates_and_opens(gtk, project_state):
    from seestar_controller.ui.project_controller import ProjectController
    from seestar_controller.ui.views import build_view

    controller = ProjectController(project_state)
    view = build_view("projects", project_controller=controller)
    assert view.view_id == "projects"
    assert view.controller is controller
    # empty state: no rows, banner says no project open, root line shows the root
    assert view.row_count() == 0
    assert "No project open" in view.current_label.get_label()
    assert str(project_state.get_setting("project_root")) in view.root_label.get_label()

    # create via the name entry + Create button
    view.name_entry.set_text("M 42")
    view.create_button.emit("clicked")
    assert view.row_count() == 1
    assert view.row_label(0) == "M 42"
    assert "m-42" in view.row_subtitle(0)
    assert view.is_current_row(0)  # the created project becomes current
    assert "M 42" in view.current_label.get_label()
    assert controller.current is not None and controller.current.name == "M 42"
    assert project_state.get_setting("last_project") == str(controller.current.path)
    assert view.name_entry.get_text() == ""  # cleared after a successful create
    assert view.status_label.has_css_class("success")

    # a second project; opening the first row switches the current project
    view.name_entry.set_text("M 31")
    view.create_button.emit("clicked")
    assert view.row_count() == 2
    assert view.is_current_row(1) and not view.is_current_row(0)
    view.click_open(0)
    assert controller.current is not None and controller.current.name == "M 42"
    assert view.is_current_row(0) and not view.is_current_row(1)
    assert "M 42" in view.current_label.get_label()
    assert project_state.get_setting("last_project") == str(controller.current.path)


def test_projects_view_inline_errors_on_duplicate_and_invalid_name(gtk, project_state):
    from seestar_controller.ui.project_controller import ProjectController
    from seestar_controller.ui.views import build_view

    controller = ProjectController(project_state)
    view = build_view("projects", project_controller=controller)
    view.name_entry.set_text("M 42")
    view.create_button.emit("clicked")
    assert view.row_count() == 1

    # duplicate (same slug) -> inline error, nothing changes
    view.name_entry.set_text("m 42")
    view.create_button.emit("clicked")
    assert view.status_label.has_css_class("error")
    assert "already exists" in view.status_label.get_label()
    assert view.row_count() == 1
    assert controller.current is not None and controller.current.name == "M 42"

    # invalid name -> inline error, nothing changes
    view.name_entry.set_text("!!!")
    view.create_button.emit("clicked")
    assert view.status_label.has_css_class("error")
    assert view.row_count() == 1


def test_projects_view_inline_error_when_open_fails(gtk, project_state):
    from seestar_controller.ui.project_controller import ProjectController
    from seestar_controller.ui.views import build_view

    controller = ProjectController(project_state)
    view = build_view("projects", project_controller=controller)
    view.name_entry.set_text("M 42")
    view.create_button.emit("clicked")
    assert view.row_count() == 1
    # the project becomes invalid after it was listed (project.json removed)
    (controller.current.path / "project.json").unlink()
    view.click_open(0)
    assert view.status_label.has_css_class("error")
    assert controller.current is not None  # unchanged


def test_projects_view_constructs_standalone_and_refreshes_on_events(gtk):
    from seestar_controller.ui.views import ProjectsView, build_view

    view = build_view("projects")  # no controller -> its own over a default state
    assert isinstance(view, ProjectsView)
    assert view.controller is not None
    assert view.row_count() == 0
    # an external event (refresh) re-renders the list
    view.controller.refresh()
    assert view.row_count() == 0


def test_app_restores_last_project_and_pushes_it_to_all_controllers(gtk, tmp_path):
    from seestar_controller.store import create_project
    from seestar_controller.ui.app import SeestarApplication
    from seestar_controller.ui.state import AppState

    settings_path = tmp_path / "settings.json"
    root = tmp_path / "projects"
    root.mkdir()
    state = AppState(settings_path)
    state.set_setting("project_root", str(root))
    project = create_project("M 42", root=root)
    state.set_setting("last_project", str(project.path))
    state.save()

    app = SeestarApplication(settings_path=str(settings_path))
    assert app.project_controller.current is not None
    assert app.project_controller.current.path == project.path
    # the restored project was pushed to every attached shared controller
    current = app.project_controller.current
    assert app.observe_controller.project is current
    assert app.viewer_controller.project is current
    assert app.picker_controller.project is current
    assert app.run_controller.project is current
    assert app.lightcurve_controller.project is current
    assert app.period_controller.project is current


def test_window_shares_the_project_controller_with_the_views(app, gtk, tmp_path):
    from seestar_controller.ui.app import SeestarWindow

    window = SeestarWindow(app, app.state)
    assert window.project_controller is app.project_controller
    stack = window.view_stack
    projects_page = stack.get_child_by_name("projects")
    observe_page = stack.get_child_by_name("observe")
    assert projects_page.controller is app.project_controller
    assert observe_page.project_controller is app.project_controller

    # creating a project through the shared controller reaches the views AND
    # the attached controllers (project root pointed at tmp first)
    root = tmp_path / "projects"
    root.mkdir()
    app.state.set_setting("project_root", str(root))
    app.state.save()
    project = app.project_controller.create_project("M 42")
    assert projects_page.row_count() == 1
    assert projects_page.is_current_row(0)
    assert observe_page.observe_widget.project is project
    assert app.observe_controller.project is project
    assert app.viewer_controller.project is project
    assert app.lightcurve_controller.project is project


class _StubObserveController:
    """Minimal duck-typed ObserveController for the start-button test."""

    def __init__(self, queue_controller, status):
        from types import SimpleNamespace

        from seestar_controller.observing import ExecutorState, ObserveViewState

        self._queue_controller = queue_controller
        self.connection = SimpleNamespace(status=status)
        self.project = None
        self._snapshot = ObserveViewState(
            executor_state=ExecutorState.IDLE,
            queue_index=None,
            entry=None,
            frames_stacked=0,
            elapsed_s=0.0,
            error=None,
            session=None,
            queue_entries=queue_controller.entries,
            preview_frame=None,
            preview_running=False,
            night_active=False,
        )
        self.listeners = []
        self.started_with = None

    @property
    def queue_controller(self):
        return self._queue_controller

    @property
    def snapshot(self):
        return self._snapshot

    def add_listener(self, listener):
        self.listeners.append(listener)
        return listener

    def remove_listener(self, listener):
        if listener in self.listeners:
            self.listeners.remove(listener)

    def start_night(self, project=None, **kwargs):
        self.started_with = project

    def pause(self):
        pass

    def resume(self):
        pass

    def skip(self):
        pass

    def stop(self):
        pass

    def park(self):
        pass


def test_observe_view_start_button_enables_when_a_project_opens(gtk, project_state):
    from seestar_controller.observing import PlanQueueController
    from seestar_controller.ui.connection import ConnectionStatus
    from seestar_controller.ui.project_controller import ProjectController
    from seestar_controller.ui.views import build_view

    controller = ProjectController(project_state)
    queue = PlanQueueController()
    stub = _StubObserveController(queue, ConnectionStatus.CONNECTED)
    view = build_view(
        "observe",
        queue_controller=queue,
        observe_controller=stub,
        project_controller=controller,
    )
    widget = view.observe_widget
    # connected but no project open -> Start disabled
    assert not widget.start_button.get_sensitive()
    # opening a project through the project controller enables Start
    project = controller.create_project("M 42")
    assert widget.start_button.get_sensitive()
    assert widget.project is project
    assert view.project_controller is controller
    # and Start passes the current project to start_night
    widget.start_button.emit("clicked")
    assert stub.started_with is project
