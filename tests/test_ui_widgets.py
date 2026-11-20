"""Widget-layer smoke tests for the ui shell — these SKIP in the headless
sandbox (no ``gi`` module, no GTK system libs, no display; see
docs/wiki/assumptions.md) and run on the desktop, where the system packages
``python3-gi gir1.2-gtk-4.0 gir1.2-adw-1`` are installed.

Guards, in order: ``pytest.importorskip("gi")`` (no PyGObject at all),
``gi.require_version`` for the Gtk 4.0 / Adw 1 typelibs, and a
``Gtk.init_check()`` fixture (no display -> skip). What is asserted when
they run: every view widget constructs, the window holds the five stack
pages, sidebar selection switches the view and updates the pure-layer
state, the dark-theme helper drives the Adw style manager, and geometry
capture/restore round-trips through the pure settings layer.
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
    assert set(ui.VIEW_CLASSES) == set(ui_state.VIEW_IDS)


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
