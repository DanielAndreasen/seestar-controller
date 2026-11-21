"""Widget-layer tests for gaia-crossmatch issue 03 — the cross-match overlay
panel of the image viewer — these SKIP in the headless sandbox (no ``gi``
module, no GTK system libs, no display; see docs/wiki/assumptions.md) and run
on the desktop, where the system packages ``python3-gi gir1.2-gtk-4.0
gir1.2-adw-1`` are installed (or under ``xvfb-run -a uv run pytest``).

Guards, in order: ``pytest.importorskip("gi")`` (no PyGObject at all),
``gi.require_version`` for the Gtk 4.0 / Adw 1 typelibs, and a
``Gtk.init_check()`` fixture (no display -> skip) — the same pattern as
tests/test_moving_objects_widgets.py.

What is asserted when they run: the wiring —
``build_view("enhance", crossmatch_controller=...)`` hands the shared
controller to the Enhance page's cross-match overlay panel (standalone
construction creates its own, following the page's viewer controller), the
app owns one shared
:class:`~seestar_controller.gaia.overlay.CrossmatchController` following its
shared viewer controller, and the window passes it down to the Enhance page;
and the :class:`~seestar_controller.gaia.overlay_view.CrossmatchOverlayView`
behavior against a tmp project with one synthetic WCS stack: selecting the
stack shows the tone-mapped stack, the Cross-match button runs the pipeline
on a worker thread (store calls marshalled to the main loop via
``GLib.idle_add``, the Gaia TAP HTTP boundary mocked — no network) and the
annotated overlay + status land on the main loop, and the radius spin
re-matches cache-first (no new HTTP).
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
from test_gaia_detect import STARS
from test_gaia_overlay import cache, project, stack_frame, tap  # noqa: F401  (fixtures)

from seestar_controller.gaia import CrossmatchController


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


def test_build_view_enhance_wires_crossmatch_controller(gtk):
    from seestar_controller.gaia.overlay_view import CrossmatchOverlayView
    from seestar_controller.ui.views import EnhanceView as EnhancePage
    from seestar_controller.ui.views import build_view

    cc = CrossmatchController()
    view = build_view("enhance", crossmatch_controller=cc)
    assert isinstance(view, EnhancePage)
    assert view.view_id == "enhance"
    assert view.crossmatch_controller is cc
    assert isinstance(view.crossmatch_overlay, CrossmatchOverlayView)
    assert view.crossmatch_overlay.controller is cc
    assert view.crossmatch_overlay.crossmatch_controller is cc
    # standalone: no controller passed -> the view creates its own, following
    # the page's own viewer controller
    standalone = build_view("enhance")
    assert standalone.crossmatch_controller is not cc
    assert standalone.crossmatch_overlay.controller is standalone.crossmatch_controller
    assert standalone.crossmatch_controller.viewer_controller is standalone.viewer_controller


def test_app_owns_shared_crossmatch_controller_and_window_passes_it_down(gtk, tmp_path):
    from seestar_controller.ui.app import SeestarApplication, SeestarWindow

    app = SeestarApplication(settings_path=str(tmp_path / "settings.json"))
    assert isinstance(app.crossmatch_controller, CrossmatchController)
    assert app.crossmatch_controller.viewer_controller is app.viewer_controller
    window = SeestarWindow(app, app.state)
    assert window.crossmatch_controller is app.crossmatch_controller
    page = window.view_stack.get_child_by_name("enhance")
    assert page.crossmatch_controller is app.crossmatch_controller
    assert page.crossmatch_overlay.controller is app.crossmatch_controller


# --- the overlay panel widget -----------------------------------------------------------------


def test_crossmatch_overlay_view_runs_and_rematches_cache_first(gtk, project, stack_frame, tap, cache, tmp_path):  # noqa: F811
    from seestar_controller.gaia.overlay_view import CrossmatchOverlayView

    frame, header = stack_frame
    calls, arm = tap
    arm(header, jitter_arcsec=0.8)
    # the app binds GLib.idle_add: store calls made from the worker thread are
    # marshalled to the (pumped) main loop — the store's sqlite connection is
    # thread-bound
    cc = CrossmatchController(
        project,
        cache=cache,
        invoke_on_main=lambda cb: GLib.idle_add(cb),
    )
    view = CrossmatchOverlayView(crossmatch_controller=cc)
    pump_main()
    assert view.controller is cc
    # no stack yet: the placeholder is shown
    assert view.picture.get_paintable() is None
    assert "no stack open" in view.stack_label.get_label()

    # select the stack (main thread): the tone-mapped stack is shown
    cc.select_stack(frame.id)
    pump_main()
    assert cc.snapshot.stack_frame_id == frame.id
    assert view.picture.get_paintable() is not None
    assert "no stack open" not in view.stack_label.get_label()
    assert view.status_label.get_label() == ""

    # click Cross-match: the pipeline runs on a worker thread, the annotated
    # overlay + status land on the main loop (emit("clicked") — Gtk.Button has
    # no .clicked() method on PyGObject/GTK4)
    view.run_button.emit("clicked")
    assert wait_until(lambda: bool(cc.snapshot.rows) and "matched" in view.status_label.get_label())
    pump_main()
    snap = cc.snapshot
    assert snap.error is None
    assert snap.image is not None
    assert view.picture.get_paintable() is not None
    assert "matched" in view.status_label.get_label()
    assert len(calls) == 1  # one Gaia TAP query
    # every injected star matched its Gaia row (0.8″ jitter < 1.5″ radius);
    # DAOStarFinder also finds spurious sources, which stay unmatched
    assert sum(1 for r in snap.rows if r.matched) == len(STARS)

    # shrink the radius below the jitter: re-match, cache-first (no new HTTP)
    view.radius_spin.set_value(0.2)
    assert wait_until(
        lambda: bool(cc.snapshot.rows) and all(not r.matched for r in cc.snapshot.rows)
    )
    pump_main()
    assert cc.snapshot.radius_arcsec == pytest.approx(0.2)
    assert len(calls) == 1  # the Gaia query was a cache hit
    assert "0/" in view.status_label.get_label()

    # widen again: matched again, still cache-first
    view.radius_spin.set_value(1.5)
    assert wait_until(lambda: any(r.matched for r in cc.snapshot.rows))
    pump_main()
    assert len(calls) == 1

    # destroying the view unregisters its listener (GTK4 Gtk.Widget has no
    # .destroy(), so invoke the destroy handler directly)
    assert view._on_snapshot in cc.listeners
    view._on_destroy()
    pump_main()
    assert view._on_snapshot not in cc.listeners


def test_crossmatch_overlay_view_shows_clean_error_when_gaia_unavailable(
    gtk, project, stack_frame, monkeypatch, cache, tmp_path  # noqa: F811
):
    import urllib.error

    from seestar_controller.gaia import query as gq
    from seestar_controller.gaia.overlay_view import CrossmatchOverlayView

    frame, _header = stack_frame

    def boom(query, **kwargs):
        raise urllib.error.URLError("no network")

    monkeypatch.setattr(gq, "fetch_gaia_tap_json", boom)
    cc = CrossmatchController(
        project,
        cache=cache,
        invoke_on_main=lambda cb: GLib.idle_add(cb),
    )
    view = CrossmatchOverlayView(crossmatch_controller=cc)
    pump_main()
    cc.select_stack(frame.id)
    pump_main()
    view.run_button.emit("clicked")
    # the failure is shown as a clean status, not raised
    assert wait_until(lambda: "GaiaUnavailableError" in view.status_label.get_label())
    pump_main()
    assert cc.snapshot.error is not None and "GaiaUnavailableError" in cc.snapshot.error
    assert cc.snapshot.rows == ()
    view._on_destroy()  # unregister the listener (GTK4 has no Widget.destroy())
