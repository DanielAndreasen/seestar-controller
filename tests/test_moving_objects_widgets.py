"""Widget-layer tests for moving-objects issue 02 — the mover overlay/blink
view — these SKIP in the headless sandbox (no ``gi`` module, no GTK system
libs, no display; see docs/wiki/assumptions.md) and run on the desktop, where
the system packages ``python3-gi gir1.2-gtk-4.0 gir1.2-adw-1`` are installed
(or under ``xvfb-run -a uv run pytest``).

Guards, in order: ``pytest.importorskip("gi")`` (no PyGObject at all),
``gi.require_version`` for the Gtk 4.0 / Adw 1 typelibs, and a
``Gtk.init_check()`` fixture (no display -> skip) — the same pattern as
tests/test_analysis_widgets.py.

What is asserted when they run: the wiring —
``build_view("photometry", mover_controller=...)`` hands the shared controller
to the Photometry page's mover overlay widget (standalone construction creates
its own), the app owns one shared
:class:`~seestar_controller.moving_objects.overlay.MoverController` following
its shared viewer controller, and the window passes it down to the Photometry
page; and the :class:`~seestar_controller.moving_objects.overlay_view.MoverOverlayView`
behavior against a tmp project with one synthetic session (one injected
linear mover): the session dropdown lists/selects the session, the Detect
button runs the pipeline on a worker thread and the movers list + annotated
frame + status update on the main loop, the MPC lookup button records the
match (mocked HTTP boundary — no network), and the Blink toggle steps through
the frames.
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
from test_moving_objects import make_session_with_subs

from seestar_controller.catalog.cache import QueryCache
from seestar_controller.moving_objects import MoverController
from seestar_controller.moving_objects import mpc as mpc_module
from seestar_controller.store import create_project


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
def project(tmp_path):
    p = create_project("M 42 night", root=tmp_path / "projects")
    yield p
    p.close()


def pump_main(timeout: float = 5.0) -> None:
    """Drain the default GLib main context (runs queued idle callbacks)."""
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        time.sleep(0.01)


def pump_for(seconds: float) -> None:
    """Pump the main loop for ``seconds`` (lets timeouts fire)."""
    deadline = time.monotonic() + seconds
    context = GLib.MainContext.default()
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        time.sleep(0.02)


def wait_until(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pump_main(0.05)
        if predicate():
            return True
    return predicate()


# --- wiring ---------------------------------------------------------------------------------


def test_build_view_photometry_wires_mover_controller(gtk):
    from seestar_controller.moving_objects.overlay_view import MoverOverlayView
    from seestar_controller.ui.views import PhotometryView as PhotometryPage
    from seestar_controller.ui.views import build_view

    mc = MoverController()
    view = build_view("photometry", mover_controller=mc)
    assert isinstance(view, PhotometryPage)
    assert view.view_id == "photometry"
    assert view.mover_controller is mc
    assert isinstance(view.mover_overlay, MoverOverlayView)
    assert view.mover_overlay.controller is mc
    assert view.mover_overlay.mover_controller is mc
    # standalone: no controller passed -> the view creates its own
    standalone = build_view("photometry")
    assert standalone.mover_controller is not mc
    assert standalone.mover_overlay.controller is standalone.mover_controller


def test_app_owns_shared_mover_controller_and_window_passes_it_down(gtk, tmp_path):
    from seestar_controller.ui.app import SeestarApplication, SeestarWindow

    app = SeestarApplication(settings_path=str(tmp_path / "settings.json"))
    assert isinstance(app.mover_controller, MoverController)
    assert app.mover_controller.viewer_controller is app.viewer_controller
    window = SeestarWindow(app, app.state)
    assert window.mover_controller is app.mover_controller
    page = window.view_stack.get_child_by_name("photometry")
    assert page.mover_controller is app.mover_controller
    assert page.mover_overlay.controller is app.mover_controller


# --- the overlay/blink widget -----------------------------------------------------------------


def test_mover_overlay_view_lists_session_and_shows_frame(gtk, project, tmp_path):
    from seestar_controller.moving_objects.overlay_view import MoverOverlayView

    sid = make_session_with_subs(project, n=6)
    mc = MoverController(project, cache=QueryCache(cache_dir=tmp_path / "mpc-cache"))
    view = MoverOverlayView(mover_controller=mc)
    pump_main()
    # the dropdown lists the session and auto-selects it
    assert view.session_dropdown.get_model().get_n_items() == 1
    assert view.selected_session_id() == sid
    assert mc.session_id == sid
    # the frame is shown (not the placeholder)
    assert view.picture.get_paintable() is not None
    # no movers yet
    assert view.movers_list.get_row_at_index(0) is None
    assert "frame 1/6" in view.frame_label.get_label()
    view._on_destroy()  # GTK4 has no Widget.destroy()


def test_mover_overlay_view_detect_button_finds_mover(gtk, project, tmp_path):
    from seestar_controller.moving_objects.overlay_view import MoverOverlayView

    sid = make_session_with_subs(project, wcs=True, n=6)
    # the app binds GLib.idle_add: store calls made from the worker thread are
    # marshalled to the (pumped) main loop — the store's sqlite connection is
    # thread-bound
    mc = MoverController(
        project,
        cache=QueryCache(cache_dir=tmp_path / "mpc-cache"),
        invoke_on_main=lambda cb: GLib.idle_add(cb),
    )
    view = MoverOverlayView(mover_controller=mc)
    pump_main()
    assert view.selected_session_id() == sid
    # click Detect: the pipeline runs on a worker thread, the render lands on
    # the main loop via GLib.idle_add (emit("clicked") — Gtk.Button has no
    # .clicked() method on PyGObject/GTK4)
    view.detect_button.emit("clicked")
    assert wait_until(lambda: mc.snapshot.movers and view.movers_list.get_row_at_index(0) is not None)
    pump_main()
    row = view.movers_list.get_row_at_index(0)
    assert row is not None
    label = row.get_child().get_label()
    assert "T" in label and "px/f" in label
    assert "1 mover(s)" in view.status_label.get_label()
    assert view.picture.get_paintable() is not None
    view._on_destroy()  # GTK4 has no Widget.destroy()


def test_mover_overlay_view_lookup_button_records_match(gtk, project, tmp_path, monkeypatch):
    from seestar_controller.moving_objects.overlay_view import MoverOverlayView

    make_session_with_subs(project, wcs=True, n=6)
    # the app binds GLib.idle_add: store calls made from the worker thread are
    # marshalled to the (pumped) main loop — the store's sqlite connection is
    # thread-bound
    mc = MoverController(
        project,
        cache=QueryCache(cache_dir=tmp_path / "mpc-cache"),
        invoke_on_main=lambda cb: GLib.idle_add(cb),
    )
    view = MoverOverlayView(mover_controller=mc)
    pump_main()
    mc.analyze_session()
    pump_main()
    assert len(mc.snapshot.movers) == 1

    def fake_fetch(params, *, timeout=30.0, url=""):
        return {
            "objects": [
                {
                    "designation": "(99999) 2026 XX",
                    "name": "Mock Planet",
                    "ra": float(params["ra"]),
                    "dec": float(params["dec"]),
                    "motion": float(params["motion"]),
                    "pa": float(params["pa"]),
                    "v": 17.0,
                }
            ]
        }

    monkeypatch.setattr(mpc_module, "fetch_mpc_checker_json", fake_fetch)
    view.lookup_button.emit("clicked")  # Gtk.Button has no .clicked() on GTK4
    assert wait_until(lambda: mc.snapshot.movers[0].mpc_match is not None)
    pump_main()
    assert "MPC: 1/1 matched" in view.status_label.get_label()
    row = view.movers_list.get_row_at_index(0)
    assert row is not None
    assert "(99999) 2026 XX" in row.get_child().get_label()
    view._on_destroy()  # GTK4 has no Widget.destroy()


def test_mover_overlay_view_blink_steps_frames(gtk, project, tmp_path):
    from seestar_controller.moving_objects.overlay_view import MoverOverlayView

    make_session_with_subs(project, n=6)
    mc = MoverController(project, cache=QueryCache(cache_dir=tmp_path / "mpc-cache"))
    view = MoverOverlayView(mover_controller=mc)
    pump_main()
    assert mc.frame_index == 0
    view.blink_button.set_active(True)
    pump_for(1.2)  # > 2 blink intervals (500 ms)
    view.blink_button.set_active(False)
    pump_main()
    assert mc.frame_index is not None and mc.frame_index > 0
    # destroy stops the blink source and removes the listener
    n_listeners = len(mc.listeners)
    view._on_destroy()  # GTK4 has no Widget.destroy()
    assert len(mc.listeners) == n_listeners - 1


def test_mover_overlay_view_refresh_and_selection(gtk, project, tmp_path):
    from seestar_controller.moving_objects.overlay_view import MoverOverlayView

    sid = make_session_with_subs(project, n=2)
    mc = MoverController(project, cache=QueryCache(cache_dir=tmp_path / "mpc-cache"))
    view = MoverOverlayView(mover_controller=mc)
    pump_main()
    assert view.refresh_sessions() == [sid]
    # selecting the dropdown entry selects the session on the controller
    view.session_dropdown.set_selected(0)
    pump_main()
    assert mc.session_id == sid
    view._on_destroy()  # GTK4 has no Widget.destroy()
