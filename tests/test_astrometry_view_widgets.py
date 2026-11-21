"""Widget-layer tests for astrometry issue 03 — the Enhance view's solve
panel (:class:`~seestar_controller.astrometry.view.SolvePanel`) and the
Observe view's drift plot
(:class:`~seestar_controller.astrometry.view.DriftPlotView`) — these SKIP in
the headless sandbox (no ``gi`` module, no GTK system libs, no display; see
docs/wiki/assumptions.md) and run on the desktop, where the system packages
``python3-gi gir1.2-gtk-4.0 gir1.2-adw-1`` are installed (or under
``xvfb-run -a uv run pytest``).

Guards, in order: ``pytest.importorskip("gi")`` (no PyGObject at all),
``gi.require_version`` for the Gtk 4.0 / Adw 1 typelibs, and a
``Gtk.init_check()`` fixture (no display -> skip) — the same pattern as
tests/test_gaia_overlay_widgets.py.

What is asserted when they run: the wiring —
``build_view("enhance", solve_controller=...)`` hands the shared controller
to the Enhance page's solve panel (standalone construction creates its own,
following the page's viewer controller), ``build_view("observe",
drift_controller=...)`` likewise for the drift plot, the app owns one shared
:class:`~seestar_controller.astrometry.SolveController` /
:class:`~seestar_controller.astrometry.DriftController` following its shared
viewer controller, and the window passes them down to the pages; and the
widgets' behavior against a tmp project with one synthetic stack / session:
the Solve button runs the solver on a worker thread (store calls marshalled
to the main loop via ``GLib.idle_add``, the solver boundary
``astrometry.session.solve_frame`` monkeypatched — no network) and reports
success/failure via the toast callable, the annotated export writes a PNG,
and the drift plot renders the series to a cairo surface + exports the PNG.

GTK4 notes: buttons are triggered with ``.emit("clicked")`` (there is no
``.clicked()`` method on PyGObject/GTK4) and views are torn down by calling
``view._on_destroy()`` directly (``Gtk.Widget`` has no ``.destroy()``).
"""

import time

import pytest

gi = pytest.importorskip("gi")

try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
except ValueError as exc:  # typelibs missing
    pytest.skip(f"GTK 4.0 / libadwaita typelibs missing: {exc}", allow_module_level=True)

from gi.repository import GLib, Gtk
from test_astrometry_view import (  # noqa: F401  (helpers + fixtures)
    CARDS,
    FakeViewer,
    _drift_project,
    _fake_result,
    project,
)
from test_gaia_overlay import stack_frame  # noqa: F401  (fixture)

from seestar_controller.astrometry import (
    DriftController,
    DriftError,
    SolveController,
    SolveError,
)
from seestar_controller.astrometry import session as astrometry_session

PNG_MAGIC = b"\x89PNG"


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
    """Drain the default GLib main context (runs queued idle callbacks)."""
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        time.sleep(0.01)


def wait_until(predicate, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pump_main(0.05)
        if predicate():
            return True
    return predicate()


# --- wiring ---------------------------------------------------------------------------------


def test_build_view_enhance_wires_solve_controller(gtk):
    from seestar_controller.astrometry.view import SolvePanel
    from seestar_controller.ui.views import EnhanceView as EnhancePage
    from seestar_controller.ui.views import build_view

    sc = SolveController()
    view = build_view("enhance", solve_controller=sc)
    assert isinstance(view, EnhancePage)
    assert view.view_id == "enhance"
    assert view.solve_controller is sc
    assert isinstance(view.solve_panel, SolvePanel)
    assert view.solve_panel.controller is sc
    assert view.solve_panel.solve_controller is sc
    # standalone: no controller passed -> the view creates its own, following
    # the page's own viewer controller
    standalone = build_view("enhance")
    assert standalone.solve_controller is not sc
    assert standalone.solve_panel.controller is standalone.solve_controller
    assert standalone.solve_controller.viewer_controller is standalone.viewer_controller


def test_build_view_observe_wires_drift_controller(gtk):
    from seestar_controller.astrometry.view import DriftPlotView
    from seestar_controller.ui.views import ObserveView as ObservePage
    from seestar_controller.ui.views import build_view

    dc = DriftController()
    view = build_view("observe", drift_controller=dc)
    assert isinstance(view, ObservePage)
    assert view.view_id == "observe"
    assert view.drift_controller is dc
    assert isinstance(view.drift_plot, DriftPlotView)
    assert view.drift_plot.controller is dc
    assert view.drift_plot.drift_controller is dc
    # standalone: no controller passed -> the view creates its own
    standalone = build_view("observe")
    assert standalone.drift_controller is not dc
    assert standalone.drift_plot.controller is standalone.drift_controller


def test_app_owns_shared_astrometry_controllers_and_window_passes_them_down(gtk, tmp_path):
    from seestar_controller.ui.app import SeestarApplication, SeestarWindow

    app = SeestarApplication(settings_path=str(tmp_path / "settings.json"))
    assert isinstance(app.solve_controller, SolveController)
    assert isinstance(app.drift_controller, DriftController)
    assert app.solve_controller.viewer_controller is app.viewer_controller
    assert app.drift_controller.viewer_controller is app.viewer_controller
    window = SeestarWindow(app, app.state)
    assert window.solve_controller is app.solve_controller
    assert window.drift_controller is app.drift_controller
    enhance_page = window.view_stack.get_child_by_name("enhance")
    assert enhance_page.solve_controller is app.solve_controller
    assert enhance_page.solve_panel.controller is app.solve_controller
    observe_page = window.view_stack.get_child_by_name("observe")
    assert observe_page.drift_controller is app.drift_controller
    assert observe_page.drift_plot.controller is app.drift_controller


# --- the solve panel widget -----------------------------------------------------------------


def test_solve_panel_solve_success_failure_and_export(gtk, project, stack_frame, monkeypatch, tmp_path):  # noqa: F811
    from seestar_controller.astrometry.view import SolvePanel

    frame, _header = stack_frame
    viewer = FakeViewer(project, frame_id=frame.id)
    # the app binds GLib.idle_add: store calls made from the worker thread are
    # marshalled to the (pumped) main loop — the store's sqlite connection is
    # thread-bound
    sc = SolveController(
        project=project,
        viewer_controller=viewer,
        invoke_on_main=lambda cb: GLib.idle_add(cb),
    )
    toasts: list[str] = []
    view = SolvePanel(solve_controller=sc, show_toast=toasts.append)
    pump_main()
    assert view.controller is sc
    assert view.stack_label.get_label() == "stack label"
    assert view.solve_button.get_sensitive()

    # Solve (success): the solver boundary is monkeypatched — no network
    monkeypatch.setattr(astrometry_session, "solve_frame", lambda **kwargs: _fake_result())
    view.solve_button.emit("clicked")  # Gtk.Button has no .clicked() on GTK4
    assert wait_until(lambda: sc.snapshot.solved is not None)
    pump_main()
    snap = sc.snapshot
    assert snap.error is None
    assert snap.last_solver == "gaia"
    assert "solved" in view.status_label.get_label()
    assert any(t.startswith("Solved:") for t in toasts)

    # Solve (failure): a clean failure toast, nothing raised
    def boom(**kwargs):
        raise SolveError("boom: no stars")

    monkeypatch.setattr(astrometry_session, "solve_frame", boom)
    view.solve_button.emit("clicked")
    assert wait_until(lambda: sc.snapshot.error is not None and "boom" in sc.snapshot.error)
    pump_main()
    assert "boom" in view.status_label.get_label()
    assert any("Solve failed" in t and "boom" in t for t in toasts)

    # programmatic annotated export writes a PNG
    out = tmp_path / "annotated.png"
    snap = view.export_annotated_to(out)
    assert snap.error is None
    assert out.read_bytes().startswith(PNG_MAGIC)

    # destroying the view unregisters its listener (GTK4 has no
    # Widget.destroy(), so invoke the destroy handler directly)
    assert view._on_snapshot in sc.listeners
    view._on_destroy()
    pump_main()
    assert view._on_snapshot not in sc.listeners


def test_solve_panel_without_stack_disables_buttons(gtk, project):  # noqa: F811
    from seestar_controller.astrometry.view import SolvePanel

    sc = SolveController(project=project, invoke_on_main=lambda cb: GLib.idle_add(cb))
    view = SolvePanel(solve_controller=sc)
    pump_main()
    assert "no stack open" in view.stack_label.get_label()
    assert not view.solve_button.get_sensitive()
    assert not view.export_button.get_sensitive()
    # Solve with no stack open: a clean error status, never raised
    view.solve_button.emit("clicked")
    assert wait_until(lambda: sc.snapshot.error is not None)
    pump_main()
    assert "no stack open" in view.status_label.get_label()
    view._on_destroy()


# --- the drift plot widget ------------------------------------------------------------------


def test_drift_plot_view_renders_series_and_exports_png(gtk, project, tmp_path):  # noqa: F811
    from seestar_controller.astrometry.view import DriftPlotView

    sid, _frames = _drift_project(project)
    dc = DriftController(project=project, invoke_on_main=lambda cb: GLib.idle_add(cb))
    view = DriftPlotView(drift_controller=dc)
    pump_main()
    assert view.controller is dc
    assert view.session_ids == (sid,)
    # nothing refreshed yet: the placeholder page is shown
    assert view.stack.get_visible_child_name() == "empty"
    assert view.png_bytes == b""

    # Refresh: the series is computed on a worker thread (store calls
    # marshalled to the main loop) and rendered to PNG -> cairo surface
    view.refresh_button.emit("clicked")
    assert wait_until(lambda: view.series is not None and bool(view.png_bytes))
    pump_main()
    assert len(view.series.points) == 3
    assert view.stack.get_visible_child_name() == "plot"
    assert view.png_bytes.startswith(PNG_MAGIC)
    assert "solved frames" in view.status_label.get_label()

    # export the rendered PNG
    out = tmp_path / "drift.png"
    assert view.export_png(out) == out
    assert out.read_bytes().startswith(PNG_MAGIC)

    # an unknown session: clean error on the placeholder, never raised
    dc.set_session("no-such-session")
    pump_main()
    view.refresh_button.emit("clicked")
    assert wait_until(lambda: dc.snapshot.error is not None)
    pump_main()
    assert view.stack.get_visible_child_name() == "empty"
    assert "SessionNotFoundError" in view.status_label.get_label()
    with pytest.raises(DriftError):
        view.export_png(tmp_path / "nothing.png")  # nothing rendered

    view._on_destroy()  # unregister the listener (GTK4 has no Widget.destroy())
    pump_main()
    assert view._on_snapshot not in dc.listeners
