"""Widget-layer smoke tests for the ui shell — these SKIP in the headless
sandbox (no ``gi`` module, no GTK system libs, no display; see
docs/wiki/assumptions.md) and run on the desktop, where the system packages
``python3-gi gir1.2-gtk-4.0 gir1.2-adw-1`` are installed.

Guards, in order: ``pytest.importorskip("gi")`` (no PyGObject at all),
``gi.require_version`` for the Gtk 4.0 / Adw 1 typelibs, and a
``Gtk.init_check()`` fixture (no display -> skip). What is asserted when
they run: every view widget constructs, the window holds the five stack
pages, sidebar selection switches the view and updates the pure-layer
state, the dark-theme helper drives the Adw style manager, geometry
capture/restore round-trips through the pure settings layer, and the
settings dialog (ticket 02) live-validates and persists through the pure
``SettingsController``, and the header-bar connection panel (ticket 03)
reflects the pure ``ConnectionController`` states (connect/disconnect button
+ status label, error styling) and shares the app's controller.
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
    assert dialog.values() == controller.values
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
    assert isinstance(window.connection_panel.get_parent(), Adw.HeaderBar)
