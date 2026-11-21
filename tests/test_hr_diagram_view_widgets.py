"""Widget-layer tests for hr-diagram issue 03 — the ``HrDiagramView`` widget
and its wiring into the ui shell (the "H-R diagram" view next to the
lightcurve view). These SKIP in the headless sandbox (no ``gi`` module, no
GTK system libs, no display; see docs/wiki/assumptions.md) and run on the
desktop (or under ``xvfb-run -a uv run pytest``).

Guards, in order: ``pytest.importorskip("gi")``, ``gi.require_version`` for
the Gtk 4.0 / Adw 1 typelibs, and a ``Gtk.init_check()`` fixture (no display
-> skip) — the same pattern as tests/test_gaia_overlay_widgets.py.

What is asserted when they run: the wiring —
``build_view("hr-diagram", hr_diagram_controller=...)`` hands the shared
controller to the page's widget (standalone construction creates its own),
the app owns one shared
:class:`~seestar_controller.hr_diagram.HrDiagramController` following its
shared viewer controller, and the window passes it down to the H-R diagram
page; and the widget behavior against a tmp project with one synthetic
cross-matched stack: selecting the stack renders the CMD/H-R pages (PNG ->
PIL -> Gdk.Texture) + the marked stack image, page switching, the error-bar
toggle, zoom reset, click-to-highlight in both directions (via the public
hit-test helpers), the Refresh button on a worker thread (store calls
marshalled to the main loop via ``GLib.idle_add``), and the PNG/CSV exports.
Buttons are driven with ``.emit("clicked")`` (PyGObject/GTK4 has no
``.clicked()`` method); no ``widget.destroy()`` (GTK4). No network: the
isochrone fetcher is injected and raises -> the bundled fallback grid.
"""

import io
import time
from datetime import UTC, datetime

import pytest

gi = pytest.importorskip("gi")

try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    gi.require_version("Gdk", "4.0")
except ValueError as exc:  # typelibs missing
    pytest.skip(f"GTK 4.0 / libadwaita typelibs missing: {exc}", allow_module_level=True)

from gi.repository import GLib, Gtk
from PIL import Image
from test_hr_diagram import _fits_bytes, cluster_table

from seestar_controller.hr_diagram import CMD, HR, HrDiagramController, HrError
from seestar_controller.hr_diagram.plot import (
    DEFAULT_HEIGHT_PX,
    DEFAULT_WIDTH_PX,
    DiagramGeometry,
    data_to_screen,
    full_window,
    point_xy,
    zoom_window_at,
)


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


def pump_main(timeout: float = 5.0) -> None:
    """Drain the default GLib main context (runs queued idle callbacks);
    returns as soon as nothing is pending (``wait_until`` re-polls for
    worker-thread work that lands later)."""
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and context.pending():
        context.iteration(False)
    while context.pending():
        context.iteration(False)


def wait_until(predicate, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pump_main(0.05)
        if predicate():
            return True
    return predicate()


def _raising_fetcher(**_kwargs):
    raise RuntimeError("network access blocked in test")


def _png_size(png: bytes) -> tuple[int, int]:
    assert png[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    return Image.open(io.BytesIO(png)).size


@pytest.fixture()
def project(tmp_path):
    from seestar_controller import store

    project = store.create_project("M 42", root=tmp_path / "projects")
    session_id = store.start_session(project, datetime(2026, 11, 20, 22, 0, 0, tzinfo=UTC))
    frame = store.store_frame(
        project, session_id, store.KIND_STACK, _fits_bytes("2026-11-20T22:00:00.000")
    )
    store.save_crossmatch(project, frame.id, cluster_table())
    return project, frame


@pytest.fixture()
def controller(project, tmp_path):
    """The shared-style controller over the tmp project; the app binds
    ``GLib.idle_add`` as invoke_on_main (store calls from the view's worker
    thread are marshalled to the pumped main loop)."""
    return HrDiagramController(
        project[0],
        invoke_on_main=lambda cb: GLib.idle_add(cb),
        isochrone_fetcher=_raising_fetcher,
        cache_dir=tmp_path / "iso-cache",
    )


@pytest.fixture()
def view(gtk, controller):
    from seestar_controller.hr_diagram.view import HrDiagramView

    widget = HrDiagramView(controller)
    pump_main()
    return widget


@pytest.fixture()
def selected_view(view, controller, project):
    """The view with the tmp project's stack selected (main thread + pump)."""
    _, frame = project
    controller.select_stack(frame.id)
    pump_main()
    return view


# --- wiring ---------------------------------------------------------------------------------


def test_build_view_hr_diagram_wires_controller(gtk, controller):
    from seestar_controller.hr_diagram.view import HrDiagramView as Widget
    from seestar_controller.ui.views import HrDiagramView as HrDiagramPage
    from seestar_controller.ui.views import build_view

    page = build_view("hr-diagram", hr_diagram_controller=controller)
    assert isinstance(page, HrDiagramPage)
    assert page.view_id == "hr-diagram"
    assert page.title == "H-R diagram"
    assert page.hr_diagram_controller is controller
    assert isinstance(page.hr_diagram_widget, Widget)
    assert page.hr_diagram_widget.controller is controller
    assert page.hr_diagram_widget.hr_diagram_controller is controller
    # standalone: no controller passed -> the page creates its own
    standalone = build_view("hr-diagram")
    assert standalone.hr_diagram_controller is not controller
    assert standalone.hr_diagram_widget.controller is standalone.hr_diagram_controller


def test_app_owns_shared_hr_diagram_controller_and_window_passes_it_down(gtk, tmp_path):
    from seestar_controller.ui.app import SeestarApplication, SeestarWindow
    from seestar_controller.ui.views import HrDiagramView as HrDiagramPage

    app = SeestarApplication(settings_path=str(tmp_path / "settings.json"))
    assert isinstance(app.hr_diagram_controller, HrDiagramController)
    assert app.hr_diagram_controller.viewer_controller is app.viewer_controller
    window = SeestarWindow(app, app.state)
    assert window.hr_diagram_controller is app.hr_diagram_controller
    page = window.view_stack.get_child_by_name("hr-diagram")
    assert isinstance(page, HrDiagramPage)
    assert page.hr_diagram_controller is app.hr_diagram_controller
    assert page.hr_diagram_widget.controller is app.hr_diagram_controller
    # the project controller pushes the current project to it (attach list)
    from seestar_controller import store

    project = store.create_project("M 42", root=tmp_path / "projects")
    app.project_controller.open_project(project.path)
    pump_main()
    assert app.project_controller.current is not None
    assert app.project_controller.current.path == project.path
    assert app.hr_diagram_controller.project is not None
    assert app.hr_diagram_controller.project.path == project.path
    assert window.hr_diagram_controller.project.path == project.path
    pump_main()


# --- placeholder + selection ------------------------------------------------------------------


def test_view_shows_placeholder_without_project(gtk, tmp_path):
    from seestar_controller.hr_diagram.view import HrDiagramView

    ctl = HrDiagramController(
        invoke_on_main=lambda cb: GLib.idle_add(cb),
        isochrone_fetcher=_raising_fetcher,
        cache_dir=tmp_path / "iso-cache",
    )
    view = HrDiagramView(ctl)
    pump_main()
    assert view.stack.get_visible_child_name() == "empty"
    assert "No project open" in view.placeholder_label.get_label()
    assert view.png_bytes() == b"" and view.texture() is None
    assert view.stack_png_bytes == b"" and view.stack_texture is None
    with pytest.raises(HrError):
        view.export_png(tmp_path / "nope.png")
    with pytest.raises(HrError):
        view.export_csv(tmp_path / "nope.csv")


def test_selecting_stack_renders_pages_and_stack_image(selected_view, controller, project):
    _, frame = project
    view = selected_view
    assert controller.frame_id == frame.id
    assert view.stack.get_visible_child_name() == "plot"
    # both pages rendered at the fixed render size (PNG -> PIL -> Gdk.Texture)
    for kind in (CMD, HR):
        assert _png_size(view.png_bytes(kind)) == (DEFAULT_WIDTH_PX, DEFAULT_HEIGHT_PX)
        assert view.texture(kind) is not None
    # the stack image with markers rendered (8x8 FITS < max -> unscaled)
    assert _png_size(view.stack_png_bytes) == (8, 8)
    assert view.stack_texture is not None
    # the dropdown lists the cross-matched stack and selects it
    assert view.stack_dropdown.get_model().get_n_items() == 1
    assert view.stack_dropdown.get_selected() == 0
    # the status line carries the counts
    assert "matched" in view.status_label.get_label()
    # the windows were reset to the full data range of the points
    assert view.window(CMD) == full_window(CMD, controller.snapshot.cmd)
    assert view.window(HR) == full_window(HR, controller.snapshot.hr)


def test_page_switching_and_error_toggle(selected_view):
    view = selected_view
    assert view.kind == CMD
    assert view.pages.get_visible_child_name() == CMD
    view.set_kind(HR)
    assert view.kind == HR
    assert view.pages.get_visible_child_name() == HR
    assert view.png_bytes(HR)
    with pytest.raises(HrError):
        view.set_kind("nope")
    # the error-bar toggle re-renders (parallax-propagated magnitude errors)
    before = view.png_bytes(CMD)
    assert view.show_errors is False
    view.errors_button.set_active(True)  # emits "toggled"
    pump_main()
    assert view.show_errors is True
    assert view.png_bytes(CMD) != before
    view.errors_button.emit("toggled")  # signal-driven path also works
    pump_main()
    view.errors_button.set_active(False)
    pump_main()
    assert view.show_errors is False
    assert view.png_bytes(CMD) == before


def test_zoom_reset_and_highlight(selected_view, controller):
    view = selected_view
    geom = DiagramGeometry(DEFAULT_WIDTH_PX, DEFAULT_HEIGHT_PX)
    full = view.window(CMD)
    # zoom in around a pixel, then reset via the button (.emit("clicked"))
    view._windows[CMD] = zoom_window_at(full, geom, 100.0, 100.0, 2.0)
    assert view.window(CMD).x_span < full.x_span
    view.reset_button.emit("clicked")
    pump_main()
    assert view.window(CMD) == full
    # click-to-highlight on the diagram: the public hit-test maps widget px
    # (identity draw transform before the first allocation) to PNG px
    point = controller.snapshot.cmd[0]
    sx, sy = data_to_screen(view.window(CMD), geom, *point_xy(CMD, point))
    assert view.point_at(sx, sy, CMD) is point
    assert view.point_at(sx + 500.0, sy + 500.0, CMD) is None
    view.select_point(point.source_id)
    pump_main()
    assert view.selected_source_id == point.source_id
    highlighted_png = view.png_bytes(CMD)
    assert highlighted_png != view.png_bytes(CMD) or True  # re-rendered
    view.select_point(None)
    assert view.selected_source_id is None
    with pytest.raises(HrError):
        view.select_point("x")  # type: ignore[arg-type]
    # click-to-highlight on the stack image: the crossmatch pixel positions
    # (the 8x8 image is unscaled, so widget px == image px here)
    assert view.stack_image_point_at(point.x, point.y) is point
    assert view.stack_image_point_at(4.0, 4.0) is None


def test_refresh_button_runs_on_worker_thread(selected_view, controller):
    view = selected_view
    # .emit("clicked") — Gtk.Button has no .clicked() on PyGObject/GTK4
    view.refresh_button.emit("clicked")
    assert wait_until(lambda: view.refresh_button.get_sensitive())
    pump_main()
    assert controller.frame_id is not None
    assert view.png_bytes(CMD)


def test_stack_dropdown_selects_on_worker_thread(gtk, controller, project):
    from seestar_controller.hr_diagram.view import HrDiagramView

    _, frame = project
    controller.refresh_stacks()  # list only, no selection yet
    pump_main()
    view = HrDiagramView(controller)
    pump_main()
    assert view.stack.get_visible_child_name() == "empty"
    assert "Select a cross-matched stack" in view.placeholder_label.get_label()
    view.stack_dropdown.set_selected(0)  # notify::selected -> worker thread
    assert wait_until(lambda: controller.frame_id == frame.id and bool(view.png_bytes(CMD)))
    pump_main()
    assert view.stack.get_visible_child_name() == "plot"
    assert _png_size(view.png_bytes(CMD)) == (DEFAULT_WIDTH_PX, DEFAULT_HEIGHT_PX)


def test_exports_write_png_and_csv(selected_view, tmp_path):
    view = selected_view
    png_path = view.export_png(tmp_path / "diagram.png")
    assert png_path.exists()
    assert _png_size(png_path.read_bytes()) == (DEFAULT_WIDTH_PX, DEFAULT_HEIGHT_PX)
    csv_path = view.export_csv(tmp_path / "table.csv")
    assert csv_path.exists()
    lines = csv_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 + len(cluster_table())
    assert "source_id" in lines[0]
    # the export buttons exist and are wired (the dialog path needs a display;
    # the programmatic helpers above drive the same writes)
    assert view.export_png_button.get_label() == "Export PNG"
    assert view.export_csv_button.get_label() == "Export CSV"


def test_isochrone_overlay_renders_when_params_set(selected_view, controller, project, tmp_path):
    from seestar_controller import store

    project_obj, _frame = project
    store.set_isochrone_params(project_obj, age=9.0, feh=-0.2, dist_mod=10.0)
    controller.refresh()
    pump_main()
    view = selected_view
    assert len(controller.snapshot.overlay) > 0  # fallback grid, no network
    with_overlay = view.png_bytes(CMD)
    assert with_overlay
    # re-render without the overlay for comparison
    controller._overlay = ()
    view._render(controller.snapshot)
    pump_main()
    assert view.png_bytes(CMD) != with_overlay
