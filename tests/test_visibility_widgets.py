"""Widget-layer tests for visibility-planner ticket 02 — the night-timeline
view (:class:`~seestar_controller.visibility.VisibilityView`) and its wiring
into the Observe page.

These SKIP in the headless sandbox (no ``gi`` module, no GTK system libs, no
display; see docs/wiki/assumptions.md) and run on the desktop, where the system
packages ``python3-gi gir1.2-gtk-4.0 gir1.2-adw-1`` are installed (or under
``xvfb-run -a uv run pytest``).

Guards, in order: ``pytest.importorskip("gi")`` (no PyGObject at all),
``gi.require_version`` for the Gtk 4.0 / Adw 1 typelibs, and a
``Gtk.init_check()`` fixture (no display -> skip) — the same pattern as
tests/test_analysis_widgets.py.

What is asserted when they run: the view renders the pure timeline model
(altitude curves + twilight + moon) through matplotlib Agg -> PNG -> PIL ->
:class:`Gdk.Texture` onto its DrawingArea (plot page shown, PNG bytes valid,
model set, suggested-order rows in the suggested order, Apply button
sensitive); the **Apply suggested order** button writes the reordered entries
back to the observing ``PlanQueueController`` through its public ``move`` API
(queue reordered, ``moved`` events emitted, button then insensitive); queue
changes re-render the view; the empty-queue and missing-site states show the
placeholder with the reason in the status line; **Export PNG** writes a valid
PNG (programmatic :meth:`VisibilityView.export_png`, no dialog); the Refresh
button re-reads the live settings source; and the wiring —
``build_view("observe", queue_controller=..., app_state=...)`` hands the
shared queue controller + app state to the Observe page's visibility widget,
and the app window passes its state down (``SeestarWindow(app, app.state)``).
"""

import datetime
import io
import time

import pytest

gi = pytest.importorskip("gi")

try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
except ValueError as exc:  # typelibs missing
    pytest.skip(f"GTK 4.0 / libadwaita typelibs missing: {exc}", allow_module_level=True)

from gi.repository import GLib, Gtk
from PIL import Image

from seestar_controller.catalog.coords import J2000Coord
from seestar_controller.observing.queue import QueueEntry
from seestar_controller.observing.queue_controller import PlanQueueController
from seestar_controller.ui.state import AppState
from seestar_controller.visibility import VisibilityError, VisibilityView, suggest_order

#: The fixed test site settings: Munich (48.1373 N, 11.5755 E, 520 m, Europe/Berlin).
MUNICH_SETTINGS = {
    "site_latitude": "48.1373",
    "site_longitude": "11.5755",
    "site_elevation_m": "520",
    "site_timezone": "Europe/Berlin",
}

#: The fixed test night.
NIGHT = datetime.date(2025, 3, 20)

#: Test targets (J2000).
COORD_M42 = J2000Coord(ra_deg=83.8215, dec_deg=-5.39114)
COORD_RA20 = J2000Coord.from_hours(20.0, 20.0)
COORD_RA5 = J2000Coord.from_hours(5.0, 25.0)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def pump_main(timeout: float = 5.0) -> None:
    """Drain the default GLib main context (runs queued idle callbacks)."""
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if context.pending():
            context.iteration(False)
        else:
            break


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


def _controller() -> PlanQueueController:
    """A mixed queue: resolved M 42, RA 20h, an unresolved name, RA 5h."""
    qc = PlanQueueController()
    qc.add_resolved("M 42", COORD_M42)
    qc.add_resolved("RA20", COORD_RA20)
    qc.add_entry(QueueEntry(name="Unresolved target"))
    qc.add_resolved("RA5", COORD_RA5)
    return qc


def _view(qc=None, settings=MUNICH_SETTINGS, night=NIGHT) -> VisibilityView:
    return VisibilityView(qc if qc is not None else _controller(), settings=settings, night=night)


# --- the view widget ----------------------------------------------------------------------------


def test_visibility_view_renders_plot_texture_model_and_suggested_order(gtk):
    qc = _controller()
    view = _view(qc)
    pump_main()
    # the plot page is shown (not the placeholder)
    assert view.stack.get_visible_child_name() == "plot"
    # the PNG -> PIL -> Gdk.Texture render happened
    assert view.texture is not None
    assert view.png_bytes[:8] == PNG_MAGIC
    with Image.open(io.BytesIO(view.png_bytes)) as image:
        assert image.format == "PNG" and image.size == (960, 420)
    # the pure model is exposed
    assert view.model is not None
    assert view.night == NIGHT
    assert view.model.labels == ("M 42", "RA20", "Unresolved target", "RA5")
    assert view.model.curve_indices == (0, 1, 3)
    # the suggested order + the list rows (one per entry, in suggested order)
    assert view.suggested_order() == suggest_order(qc.entries, view.model.site, NIGHT) == (3, 0, 1, 2)
    assert view.suggested_row_labels() == (
        "1. RA5",
        "2. M 42",
        "3. RA20",
        "4. Unresolved target",
    )
    # the Apply button is enabled (the queue is not yet in the suggested order)
    assert view.apply_button.get_sensitive()
    # header + status
    assert "2025-03-20" in view.night_label.get_label()
    assert "3 target(s)" in view.status_label.get_label()
    assert "astro dusk" in view.status_label.get_label()
    assert "moon" in view.status_label.get_label()


def test_apply_button_reorders_the_plan_queue(gtk):
    qc = _controller()
    events = []
    qc.add_listener(lambda event: events.append(event))
    view = _view(qc)
    pump_main()
    view.apply_button.emit("clicked")
    pump_main()
    # the queue was reordered through the public move API (moved events emitted)
    assert [e.name for e in qc.entries] == ["RA5", "M 42", "RA20", "Unresolved target"]
    assert any(event.kind == "moved" for event in events)
    # the view re-rendered: the order is now the identity -> Apply disabled
    assert view.suggested_order() == (0, 1, 2, 3)
    assert not view.apply_button.get_sensitive()
    assert "applied" in view.status_label.get_label()
    # the plot still renders after the apply
    assert view.stack.get_visible_child_name() == "plot"
    assert view.png_bytes[:8] == PNG_MAGIC


def test_queue_changes_rerender_the_view(gtk):
    qc = _controller()
    view = _view(qc)
    pump_main()
    assert len(view.model.labels) == 4
    qc.add_resolved("RA12", J2000Coord.from_hours(12.0, 30.0))
    pump_main()
    assert len(view.model.labels) == 5
    assert len(view.suggested_row_labels()) == 5
    qc.clear()
    pump_main()
    assert view.stack.get_visible_child_name() == "empty"
    assert view.model is None


def test_empty_queue_shows_placeholder(gtk):
    view = VisibilityView(PlanQueueController(), settings=MUNICH_SETTINGS, night=NIGHT)
    pump_main()
    assert view.stack.get_visible_child_name() == "empty"
    assert "empty" in view.placeholder_label.get_label()
    assert view.texture is None and view.png_bytes == b"" and view.model is None
    assert not view.apply_button.get_sensitive()
    assert view.suggested_row_labels() == ()


def test_missing_site_shows_error_placeholder(gtk):
    for settings in ({}, None):
        view = VisibilityView(_controller(), settings=settings, night=NIGHT)
        pump_main()
        assert view.stack.get_visible_child_name() == "empty"
        assert "unavailable" in view.placeholder_label.get_label().lower()
        assert "VisibilityError" in view.status_label.get_label()
        assert view.status_label.has_css_class("error")
        assert not view.apply_button.get_sensitive()
        assert view.png_bytes == b""


def test_refresh_rereads_the_live_settings_source(gtk):
    settings = dict(MUNICH_SETTINGS)
    qc = _controller()
    view = VisibilityView(qc, settings=lambda: settings, night=NIGHT)
    pump_main()
    assert view.stack.get_visible_child_name() == "plot"
    # drop the site from the live settings -> Refresh shows the error placeholder
    settings.clear()
    view.refresh_button.emit("clicked")
    pump_main()
    assert view.stack.get_visible_child_name() == "empty"
    assert "VisibilityError" in view.status_label.get_label()
    # restore -> Refresh renders again
    settings.update(MUNICH_SETTINGS)
    view.refresh()
    pump_main()
    assert view.stack.get_visible_child_name() == "plot"
    assert view.png_bytes[:8] == PNG_MAGIC


def test_export_png_writes_a_valid_png(gtk, tmp_path):
    view = _view()
    pump_main()
    out = view.export_png(tmp_path / "night-timeline.png")
    assert out == tmp_path / "night-timeline.png"
    data = out.read_bytes()
    assert data[:8] == PNG_MAGIC
    with Image.open(io.BytesIO(data)) as image:
        assert image.format == "PNG" and image.size == (960, 420)
    # nothing rendered -> VisibilityError
    empty = VisibilityView(PlanQueueController(), settings=MUNICH_SETTINGS, night=NIGHT)
    pump_main()
    with pytest.raises(VisibilityError):
        empty.export_png(tmp_path / "nope.png")


# --- wiring into the app -------------------------------------------------------------------------


def test_build_view_observe_wires_visibility_widget(gtk, tmp_path):
    from seestar_controller.ui.views import ObserveView as ObservePage
    from seestar_controller.ui.views import build_view

    qc = _controller()
    state = AppState(tmp_path / "settings.json")
    for key, value in MUNICH_SETTINGS.items():
        state.set_setting(key, value)
    page = build_view("observe", queue_controller=qc, app_state=state)
    assert isinstance(page, ObservePage)
    assert page.app_state is state
    assert page.visibility_widget.queue_controller is qc
    pump_main()
    # the site comes from the app state's settings -> the plot renders
    assert page.visibility_widget.stack.get_visible_child_name() == "plot"
    assert page.visibility_widget.model is not None
    # standalone (no app_state): a default AppState has no site -> placeholder
    standalone = build_view("observe", queue_controller=qc)
    assert standalone.app_state is not state
    pump_main()
    assert standalone.visibility_widget.stack.get_visible_child_name() == "empty"


def test_app_window_passes_state_down_to_the_observe_page(gtk, tmp_path):
    from seestar_controller.ui.app import SeestarApplication, SeestarWindow

    app = SeestarApplication(settings_path=str(tmp_path / "settings.json"))
    for key, value in MUNICH_SETTINGS.items():
        app.state.set_setting(key, value)
    window = SeestarWindow(app, app.state)
    page = window.view_stack.get_child_by_name("observe")
    assert page.visibility_widget is not None
    assert page.visibility_widget.queue_controller is window.queue_controller
    assert page.app_state is app.state
    # the window's shared queue starts empty -> placeholder ...
    pump_main()
    assert page.visibility_widget.stack.get_visible_child_name() == "empty"
    # ... and adding targets re-renders the plot (site from the app state)
    window.queue_controller.add_resolved("M 42", COORD_M42)
    window.queue_controller.add_resolved("RA5", COORD_RA5)
    pump_main()
    assert page.visibility_widget.stack.get_visible_child_name() == "plot"
    assert page.visibility_widget.model is not None
    assert page.visibility_widget.png_bytes[:8] == PNG_MAGIC
