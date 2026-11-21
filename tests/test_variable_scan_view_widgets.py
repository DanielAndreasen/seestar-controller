"""Widget-layer tests for variable-scan issue 02 — the candidate view of the
Photometry page — these SKIP in the headless sandbox (no ``gi`` module, no
GTK system libs, no display; see docs/wiki/assumptions.md) and run on the
desktop, where the system packages ``python3-gi gir1.2-gtk-4.0
gir1.2-adw-1`` are installed (or under ``xvfb-run -a uv run pytest``).

Guards, in order: ``pytest.importorskip("gi")`` (no PyGObject at all),
``gi.require_version`` for the Gtk 4.0 / Adw 1 typelibs, and a
``Gtk.init_check()`` fixture (no display -> skip) — the same pattern as
tests/test_gaia_overlay_widgets.py.

What is asserted when they run: the wiring —
``build_view("photometry", scan_controller=...)`` hands the shared controller
to the Photometry page's variable-scan panel (standalone construction creates
its own), the app owns one shared
:class:`~seestar_controller.variable_scan.ScanController` following its shared
viewer controller, and the window passes it down to the Photometry page; and
the :class:`~seestar_controller.variable_scan.view.VariableScanView` behavior
against the synthetic ``scannable`` project of tests/test_variable_scan.py
(10 subs, one injected variable at source_id 1004): Run scan on a worker
thread (store calls marshalled to the main loop via ``GLib.idle_add``) fills
the ranked candidate list, activating a row shows the candidate's lightcurve
(the reused analysis LightcurveView) and its blink cutout in the picture,
the threshold slider re-filters live, and the CSV/GIF exports write files.
Buttons are driven with ``emit("clicked")`` (GTK4 has no ``.clicked()``) and
the view is torn down with ``view._on_destroy()`` (GTK4 has no
``.destroy()``). No network anywhere.
"""

import csv
import time

import pytest

gi = pytest.importorskip("gi")

try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
except ValueError as exc:  # typelibs missing
    pytest.skip(f"GTK 4.0 / libadwaita typelibs missing: {exc}", allow_module_level=True)

from gi.repository import GLib, Gtk
from test_variable_scan import (  # noqa: F401  (fixtures)
    ALL_XYS,
    VARIABLE_XY,
    project,
    root,
    scannable,
)

from seestar_controller.variable_scan import CANDIDATES_CSV_HEADER, ScanController

VARIABLE_SOURCE_ID = 1000 + ALL_XYS.index(VARIABLE_XY)


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


def wait_until(predicate, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pump_main(0.05)
        if predicate():
            return True
    return predicate()


# --- wiring ---------------------------------------------------------------------------------


def test_build_view_photometry_wires_scan_controller(gtk):
    from seestar_controller.ui.views import PhotometryView, build_view
    from seestar_controller.variable_scan.view import VariableScanView

    sc = ScanController()
    view = build_view("photometry", scan_controller=sc)
    assert isinstance(view, PhotometryView)
    assert view.view_id == "photometry"
    assert view.scan_controller is sc
    assert isinstance(view.variable_scan_view, VariableScanView)
    assert view.variable_scan_view.controller is sc
    assert view.variable_scan_view.scan_controller is sc
    # standalone: no controller passed -> the view creates its own
    standalone = build_view("photometry")
    assert standalone.scan_controller is not sc
    assert standalone.variable_scan_view.controller is standalone.scan_controller
    standalone.variable_scan_view._on_destroy()
    view.variable_scan_view._on_destroy()


def test_app_owns_shared_scan_controller_and_window_passes_it_down(gtk, tmp_path):
    from seestar_controller.ui.app import SeestarApplication, SeestarWindow

    app = SeestarApplication(settings_path=str(tmp_path / "settings.json"))
    assert isinstance(app.scan_controller, ScanController)
    assert app.scan_controller.viewer_controller is app.viewer_controller
    window = SeestarWindow(app, app.state)
    assert window.scan_controller is app.scan_controller
    page = window.view_stack.get_child_by_name("photometry")
    assert page.scan_controller is app.scan_controller
    assert page.variable_scan_view.controller is app.scan_controller
    page.variable_scan_view._on_destroy()


# --- the candidate view widget ----------------------------------------------------------------


def test_variable_scan_view_run_select_threshold_export(gtk, project, scannable, tmp_path):  # noqa: F811
    from seestar_controller.variable_scan.view import VariableScanView

    # the app binds GLib.idle_add: store calls made from the worker thread are
    # marshalled to the (pumped) main loop — the store's sqlite connection is
    # thread-bound
    sc = ScanController(project, invoke_on_main=lambda cb: GLib.idle_add(cb))
    view = VariableScanView(scan_controller=sc)
    pump_main()
    assert view.controller is sc
    # no scan run yet: empty list, placeholder blink, empty status
    assert view.candidates_list.get_row_at_index(0) is None
    assert view.blink_stack.get_visible_child_name() == "empty"
    assert view.blink_picture.get_paintable() is None
    assert view.lightcurve_widget.stack.get_visible_child_name() == "empty"

    # click Run scan: the scan runs on a worker thread, the ranked candidate
    # list lands on the main loop (emit("clicked") — GTK4 has no .clicked())
    view.run_button.emit("clicked")
    assert wait_until(lambda: view.candidates_list.get_row_at_index(0) is not None)
    pump_main()
    snap = sc.snapshot
    assert snap.error is None
    assert len(snap.candidates) >= 1
    texts = view.candidate_row_texts()
    assert len(texts) == len(snap.candidates)
    # ranked: the first row is the injected variable with its scatter ratio
    assert texts[0].startswith("#1")
    assert str(VARIABLE_SOURCE_ID) in texts[0]
    assert "ratio=" in texts[0]
    assert "frames=10" in texts[0]
    # the first candidate is auto-selected: lightcurve + blink rendered
    assert view.selected_candidate_index() == 0
    assert view.lightcurve_widget.stack.get_visible_child_name() == "plot"
    assert view.blink_stack.get_visible_child_name() == "image"
    assert view.blink_picture.get_paintable() is not None
    assert "frame 1/10" in view.frame_label.get_label()
    assert "candidate" in view.status_label.get_label()

    # activate the first row explicitly: selection stays, blink steps work
    row = view.candidates_list.get_row_at_index(0)
    view.candidates_list.emit("row-activated", row)
    pump_main()
    assert sc.snapshot.selected_index == 0
    view.next_button.emit("clicked")
    pump_main()
    assert "frame 2/10" in view.frame_label.get_label()
    view.prev_button.emit("clicked")
    pump_main()
    assert "frame 1/10" in view.frame_label.get_label()

    # threshold slider: set_value fires value-changed -> persisted + re-filter
    # (the injected variable's ratio is ~91, above the slider max, so it stays
    # listed at every slider position; the constants ~0.01 stay below the
    # slider min 0.5 — the pure tests cover emptying the list via the API)
    view.threshold_scale.set_value(10.0)
    assert wait_until(lambda: sc.snapshot.threshold == 10.0)
    pump_main()
    assert view.threshold_label.get_label() == "10.0"
    assert len(view.candidate_row_texts()) == len(sc.snapshot.candidates) == 1
    assert str(VARIABLE_SOURCE_ID) in view.candidate_row_texts()[0]
    view.threshold_scale.set_value(0.5)
    assert wait_until(lambda: sc.snapshot.threshold == 0.5)
    pump_main()
    assert view.threshold_label.get_label() == "0.5"
    assert len(view.candidate_row_texts()) == 1
    view.threshold_scale.set_value(3.0)
    assert wait_until(lambda: sc.snapshot.threshold == 3.0)
    pump_main()

    # exports (the FileDialog path is async; drive the export directly)
    csv_path = tmp_path / "candidates.csv"
    view._do_export(str(csv_path), "csv")
    pump_main()
    with open(csv_path, encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    assert tuple(rows[0]) == CANDIDATES_CSV_HEADER
    assert len(rows) == len(sc.snapshot.candidates) + 1
    assert rows[1][1] == str(VARIABLE_SOURCE_ID)
    gif_path = tmp_path / "blink.gif"
    view._do_export(str(gif_path), "gif")
    pump_main()
    assert gif_path.read_bytes()[:6] in (b"GIF87a", b"GIF89a")

    # teardown unregisters the listener (GTK4 has no .destroy())
    assert view._on_snapshot in sc.listeners
    view._on_destroy()
    pump_main()
    assert view._on_snapshot not in sc.listeners


def test_variable_scan_view_standalone_creates_own_controller(gtk):
    from seestar_controller.variable_scan.view import VariableScanView

    view = VariableScanView()
    pump_main()
    assert isinstance(view.controller, ScanController)
    assert view.controller.project is None
    assert view.candidates_list.get_row_at_index(0) is None
    assert view.blink_stack.get_visible_child_name() == "empty"
    view._on_destroy()
    pump_main()
