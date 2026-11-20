"""The period-analysis widget — the WIDGET layer of period-analysis ticket 03
(:mod:`seestar_controller.analysis.period_view`).

This module imports ``gi`` at module top (it is a widget module, the only
place allowed to): it is never imported by the pure layer
(:mod:`seestar_controller.analysis.fold`) or eagerly by the package
``__init__`` — the pure layer stays gi-free and headless-testable (see
docs/wiki/assumptions.md: the sandbox has no GTK). It is re-exported as
``analysis.PeriodView`` lazily (PEP 562 ``__getattr__``).

On the desktop the system packages are required (PyGObject is NOT a pip
dependency — it needs the system GTK headers to build)::

    sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1

:class:`PeriodView` is a thin observer over the pure
:class:`~seestar_controller.analysis.fold.PeriodController` (the app owns one
shared instance, bound to the shared lightcurve controller's project; the
window passes it down via
:func:`seestar_controller.ui.views.build_view`):

- a **header row** — heading, a **Compute periodogram** button (runs the
  Lomb-Scargle periodogram on a worker thread; the result arrives as a
  controller snapshot marshalled onto the GLib main loop), and a **manual
  period entry** (a :class:`Gtk.SpinButton` in days + a **Set period**
  button, validated by the pure layer — an invalid value surfaces as a red
  status line, never a crash);
- the **periodogram plot** (power vs period) on a :class:`Gtk.DrawingArea`:
  the full spectrum as a line, the **top-N peaks as clickable markers** — a
  :class:`Gtk.GestureClick` hit-tests the markers (within
  :data:`PEAK_HIT_RADIUS_PX` of a peak's screen position) and calls
  :meth:`~seestar_controller.analysis.fold.PeriodController.select_peak`,
  which selects AND persists the period;
- the **folded lightcurve plot** (mag vs phase 0–1) on a second
  :class:`Gtk.DrawingArea`: ALL sessions colored like the lightcurve view
  (the deterministic palette of the pure layer), error bars per point,
  excluded points marked red, the mag axis inverted (brighter at the top).
  Both plots reuse the PURE :class:`~seestar_controller.analysis.lightcurve.PlotGeometry`
  / :class:`~seestar_controller.analysis.lightcurve.PlotWindow` transforms —
  with ``x = phase`` on the folded plot and ``x = period`` / ``y = -power`` on
  the periodogram plot (see
  :func:`~seestar_controller.analysis.fold.periodogram_window`) — so all the
  mapping math is unit-tested headless;
- **zoom/pan** on both plots: scroll zooms around the cursor, drag pans,
  double-click resets to the full window (the same pure
  :func:`~seestar_controller.analysis.lightcurve.zoom_window_at` /
  :func:`~seestar_controller.analysis.lightcurve.pan_window` math as the
  lightcurve view);
- **outlier marking (ticket 04)**: a single click on the FOLDED plot
  hit-tests the folded points (within :data:`FOLDED_POINT_HIT_RADIUS_PX` of a
  point's screen position, see :meth:`PeriodView.folded_point_at`) and
  toggles that point's ``excluded`` flag via the bound lightcurve
  controller's
  :meth:`~seestar_controller.analysis.lightcurve.LightcurveController.toggle_excluded`
  — the store write refreshes the lightcurve, the period controller
  re-emits, and the point is redrawn marked (red). Excluded points never
  feed the periodogram (the store lightcurve default).

The view re-renders on every frozen
:class:`~seestar_controller.analysis.fold.PeriodSnapshot` (listener
notifications marshalled onto the GLib main loop when they come from a
worker thread).
"""

from __future__ import annotations

import threading
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import GLib, Gtk  # noqa: E402

from .fold import (  # noqa: E402
    SOURCE_PEAK,
    FoldedPoint,
    FoldError,
    PeriodController,
    PeriodError,
    PeriodSnapshot,
    folded_window,
    periodogram_window,
)
from .lightcurve import (  # noqa: E402
    AXIS_COLOR,
    EXCLUDED_COLOR,
    ZOOM_FACTOR,
    LightcurveError,
    PlotGeometry,
    PlotWindow,
    data_to_screen,
    pan_window,
    session_color,
    zoom_window_at,
)
from .periodogram import Peak  # noqa: E402

#: Initial drawing-area content size (pixels) — the geometry used by the
#: zoom/pan handlers until the first draw reports the real size.
INITIAL_PLOT_WIDTH = 360
INITIAL_PLOT_HEIGHT = 240

#: Hit-test radius (pixels) around a peak marker for the click-to-select.
PEAK_HIT_RADIUS_PX = 10.0

#: Hit-test radius (pixels) around a folded data point for the
#: click-to-toggle outlier gesture (ticket 04).
FOLDED_POINT_HIT_RADIUS_PX = 6.0

#: The manual period entry range (days): the SpinButton's adjustment bounds.
MANUAL_PERIOD_MIN_DAYS = 1e-9
MANUAL_PERIOD_MAX_DAYS = 1e6


class PeriodView(Gtk.Box):
    """The period-analysis panel: periodogram plot (power vs period, top-N
    peaks clickable) + manual period entry + the phase-folded lightcurve
    (mag vs phase 0–1, one color per session, error bars, excluded marked) —
    observing a :class:`~seestar_controller.analysis.fold.PeriodController`.

    Constructed with the shared period controller (the app passes it; a fresh
    standalone one is created when omitted, so the widget also works alone).
    """

    def __init__(
        self,
        period_controller: PeriodController | None = None,
        **kwargs,
    ) -> None:
        kwargs.setdefault("orientation", Gtk.Orientation.VERTICAL)
        kwargs.setdefault("spacing", 6)
        super().__init__(**kwargs)
        if period_controller is None:
            period_controller = PeriodController()
        self._controller = period_controller
        self._main_thread_id = threading.get_ident()
        self._result = None  # the last PeriodogramResult
        self._peaks: tuple[Peak, ...] = ()
        self._folded: tuple[FoldedPoint, ...] = ()
        self._sessions: tuple[str, ...] = ()
        self._colors: dict[str, tuple[float, float, float, float]] = {}
        self._period: float | None = None
        self._source: str | None = None
        self._data_key: tuple = ()  # change detection for window resets
        self._periodogram_window: PlotWindow = periodogram_window_of_empty()
        self._folded_window: PlotWindow = folded_window(())
        self._periodogram_geom = PlotGeometry(INITIAL_PLOT_WIDTH, INITIAL_PLOT_HEIGHT)
        self._folded_geom = PlotGeometry(INITIAL_PLOT_WIDTH, INITIAL_PLOT_HEIGHT)
        self._drag_last: dict[int, tuple[float, float]] = {}

        # header row: heading + Compute periodogram + manual period entry
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.append(Gtk.Label(label="Period analysis", xalign=0.0, css_classes=["heading"]))
        self._compute_button = Gtk.Button(
            label="Compute periodogram",
            tooltip_text="Run the Lomb-Scargle periodogram over the project's lightcurve",
        )
        self._compute_button.connect("clicked", self._on_compute_clicked)
        header.append(self._compute_button)
        header.append(Gtk.Label(label="Period (days):", xalign=0.0))
        self._period_spin = Gtk.SpinButton(
            adjustment=Gtk.Adjustment(
                value=1.0,
                lower=MANUAL_PERIOD_MIN_DAYS,
                upper=MANUAL_PERIOD_MAX_DAYS,
                step_increment=0.01,
                page_increment=0.1,
            ),
            digits=6,
            numeric=True,
            tooltip_text="Enter a known period in days",
        )
        self._period_spin.connect("activate", self._on_set_period)
        header.append(self._period_spin)
        self._set_period_button = Gtk.Button(
            label="Set period",
            tooltip_text="Use the entered period (saved in the project)",
        )
        self._set_period_button.connect("clicked", self._on_set_period)
        header.append(self._set_period_button)
        self.append(header)

        # the two plots side by side: periodogram | folded lightcurve
        paned = Gtk.Paned(
            orientation=Gtk.Orientation.HORIZONTAL,
            start_child=self._build_periodogram_plot(),
            end_child=self._build_folded_plot(),
            shrink_start_child=False,
            shrink_end_child=False,
            wide_handle=True,
        )
        paned.set_hexpand(True)
        paned.set_vexpand(True)
        self.append(paned)

        self._status_label = Gtk.Label(label="", wrap=True, halign=Gtk.Align.CENTER)
        self.append(self._status_label)

        self._controller.add_listener(self._on_snapshot)
        self._render(self._controller.snapshot)

    # -- construction helpers ---------------------------------------------------------

    def _build_periodogram_plot(self) -> Gtk.Widget:
        """The periodogram plot (power vs period) with peak click + zoom/pan."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.append(Gtk.Label(label="Periodogram (power vs period — click a peak)", xalign=0.5, css_classes=["dim-label"]))
        self._periodogram_plot = Gtk.DrawingArea()
        self._periodogram_plot.set_hexpand(True)
        self._periodogram_plot.set_vexpand(True)
        self._periodogram_plot.set_content_width(INITIAL_PLOT_WIDTH)
        self._periodogram_plot.set_content_height(INITIAL_PLOT_HEIGHT)
        self._periodogram_plot.set_draw_func(self._draw_periodogram)
        frame = Gtk.Frame(child=self._periodogram_plot)
        frame.set_vexpand(True)
        box.append(frame)
        self._add_plot_gestures(self._periodogram_plot, on_double_click=self.reset_periodogram_zoom)
        click = Gtk.GestureClick(button=1)
        click.connect("pressed", self._on_peak_pressed)
        self._periodogram_plot.add_controller(click)
        return box

    def _build_folded_plot(self) -> Gtk.Widget:
        """The folded lightcurve plot (mag vs phase 0–1) with zoom/pan."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.append(Gtk.Label(label="Folded lightcurve (mag vs phase)", xalign=0.5, css_classes=["dim-label"]))
        self._folded_plot = Gtk.DrawingArea()
        self._folded_plot.set_hexpand(True)
        self._folded_plot.set_vexpand(True)
        self._folded_plot.set_content_width(INITIAL_PLOT_WIDTH)
        self._folded_plot.set_content_height(INITIAL_PLOT_HEIGHT)
        self._folded_plot.set_draw_func(self._draw_folded)
        frame = Gtk.Frame(child=self._folded_plot)
        frame.set_vexpand(True)
        box.append(frame)
        self._add_plot_gestures(self._folded_plot, on_double_click=self.reset_folded_zoom)
        return box

    def _add_plot_gestures(self, plot: Gtk.DrawingArea, *, on_double_click) -> None:
        """Scroll zoom-at-cursor + drag pan + double-click reset on ``plot``
        (the pure transform math; the window is chosen by the plot)."""
        scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._on_scroll, plot)
        plot.add_controller(scroll)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin, plot)
        drag.connect("drag-update", self._on_drag_update, plot)
        plot.add_controller(drag)
        click = Gtk.GestureClick(button=1)
        click.connect("pressed", self._on_plot_pressed, plot, on_double_click)
        plot.add_controller(click)

    # -- public helpers (also used by the widget tests) ------------------------------

    @property
    def controller(self) -> PeriodController:
        """The pure-layer period controller backing this view."""
        return self._controller

    @property
    def period_controller(self) -> PeriodController:
        """Alias of :attr:`controller`."""
        return self._controller

    @property
    def compute_button(self) -> Gtk.Button:
        """The Compute periodogram button."""
        return self._compute_button

    @property
    def period_spin(self) -> Gtk.SpinButton:
        """The manual period entry (days)."""
        return self._period_spin

    @property
    def set_period_button(self) -> Gtk.Button:
        """The Set period button (applies the manual entry)."""
        return self._set_period_button

    @property
    def periodogram_plot(self) -> Gtk.DrawingArea:
        """The periodogram drawing area."""
        return self._periodogram_plot

    @property
    def folded_plot(self) -> Gtk.DrawingArea:
        """The folded lightcurve drawing area."""
        return self._folded_plot

    @property
    def status_label(self) -> Gtk.Label:
        """The status label (period/source/counts or a clean error)."""
        return self._status_label

    def peaks(self) -> tuple[Peak, ...]:
        """The currently plotted periodogram peaks."""
        return self._peaks

    def folded_points(self) -> tuple[FoldedPoint, ...]:
        """The currently plotted folded points (sorted by phase)."""
        return self._folded

    def session_colors(self) -> dict[str, tuple[float, float, float, float]]:
        """The current session -> RGBA color mapping (the folded plot legend)."""
        return dict(self._colors)

    @property
    def periodogram_window(self) -> PlotWindow:
        """The current periodogram view window (x = period, y = -power)."""
        return self._periodogram_window

    @property
    def folded_window(self) -> PlotWindow:
        """The current folded view window (x = phase, y = mag)."""
        return self._folded_window

    def peak_screen_positions(self) -> list[tuple[float, float]]:
        """The screen positions of the peak markers (for hit-testing/tests)."""
        return [
            data_to_screen(self._periodogram_window, self._periodogram_geom, pk.period, -pk.power)
            for pk in self._peaks
        ]

    def reset_periodogram_zoom(self) -> PlotWindow:
        """Reset the periodogram window to the full spectrum; returns it."""
        self._periodogram_window = (
            periodogram_window(self._result) if self._result is not None else periodogram_window_of_empty()
        )
        self._periodogram_plot.queue_draw()
        return self._periodogram_window

    def reset_folded_zoom(self) -> PlotWindow:
        """Reset the folded window to the full phase 0–1 range; returns it."""
        self._folded_window = folded_window(self._folded)
        self._folded_plot.queue_draw()
        return self._folded_window

    def folded_point_at(self, x: float, y: float) -> FoldedPoint | None:
        """Hit-test the folded plot: the closest folded point whose screen
        position is within :data:`FOLDED_POINT_HIT_RADIUS_PX` of ``(x, y)``
        (widget coordinates), or ``None``. Used by the click-to-toggle
        outlier gesture (ticket 04)."""
        if not self._folded:
            return None
        best = None
        best_dist = FOLDED_POINT_HIT_RADIUS_PX
        for fp in self._folded:
            px, py = data_to_screen(self._folded_window, self._folded_geom, fp.phase, fp.point.mag)
            dist = ((px - x) ** 2 + (py - y) ** 2) ** 0.5
            if dist <= best_dist:
                best_dist = dist
                best = fp
        return best

    # -- events ------------------------------------------------------------------------

    def _on_compute_clicked(self, *_args) -> None:
        """Compute periodogram button: run it on a worker thread (it can take
        a moment); the result arrives as a snapshot on the main loop. A
        failure is carried by the snapshot's ``error`` (rendered below)."""
        self._set_status("computing periodogram…", error=False)

        def work() -> None:
            try:
                self._controller.compute_periodogram()
            except PeriodError:
                pass  # the snapshot carries the error; rendered by the listener

        threading.Thread(target=work, name="periodogram-worker", daemon=True).start()

    def _on_set_period(self, *_args) -> None:
        """Set period button / entry activate: apply the manual entry through
        the pure controller (validated there); an invalid value surfaces as a
        red status line."""
        try:
            period = self._controller.set_manual_period(self._period_spin.get_value())
        except FoldError as exc:
            # FoldError (the superclass) covers both an invalid entry
            # (validate_period raises FoldError) and a persistence failure
            # (PeriodError, a FoldError subclass) — the red status path.
            self._set_status(f"invalid period: {exc}", error=True)
            return
        self._set_status(f"period set manually to {period:.6g} d (saved in the project)", error=False)

    def _on_peak_pressed(self, gesture: Gtk.GestureClick, n_press: int, x: float, y: float) -> None:
        """Click on the periodogram plot: hit-test the peak markers (within
        :data:`PEAK_HIT_RADIUS_PX`) and select the closest one — the pure
        controller persists it. Single click only."""
        if n_press != 1 or not self._peaks:
            return
        best_index = None
        best_dist = PEAK_HIT_RADIUS_PX
        for i, (px, py) in enumerate(self.peak_screen_positions()):
            dist = ((px - x) ** 2 + (py - y) ** 2) ** 0.5
            if dist <= best_dist:
                best_dist = dist
                best_index = i
        if best_index is None:
            return
        try:
            period = self._controller.select_peak(best_index)
        except PeriodError as exc:
            self._set_status(f"cannot select peak: {exc}", error=True)
            return
        self._set_status(
            f"selected peak #{best_index}: period {period:.6g} d (saved in the project)",
            error=False,
        )

    def _window_for(self, plot: Gtk.DrawingArea) -> PlotWindow:
        return self._periodogram_window if plot is self._periodogram_plot else self._folded_window

    def _set_window_for(self, plot: Gtk.DrawingArea, window: PlotWindow) -> None:
        if plot is self._periodogram_plot:
            self._periodogram_window = window
        else:
            self._folded_window = window

    def _geom_for(self, plot: Gtk.DrawingArea) -> PlotGeometry:
        return self._periodogram_geom if plot is self._periodogram_plot else self._folded_geom

    def _cursor_position(self, controller: Gtk.EventController, plot: Gtk.DrawingArea) -> tuple[float, float]:
        """The cursor position within ``plot`` for the controller's current
        event; falls back to the plot center."""
        event = controller.get_current_event()
        if event is not None:
            ok, x, y = event.get_coords()
            if ok:
                return x, y
        geom = self._geom_for(plot)
        return (
            geom.margin_left + geom.plot_width / 2.0,
            geom.margin_top + geom.plot_height / 2.0,
        )

    def _on_scroll(self, controller: Gtk.EventControllerScroll, dx: float, dy: float, plot: Gtk.DrawingArea) -> bool:
        """Scroll = zoom around the cursor (scroll up zooms in)."""
        window = self._window_for(plot)
        factor = ZOOM_FACTOR ** (-dy)
        x, y = self._cursor_position(controller, plot)
        self._set_window_for(plot, zoom_window_at(window, self._geom_for(plot), x, y, factor))
        plot.queue_draw()
        return True

    def _on_drag_begin(self, gesture: Gtk.GestureDrag, start_x: float, start_y: float, plot: Gtk.DrawingArea) -> None:
        self._drag_last[id(plot)] = (0.0, 0.0)

    def _on_drag_update(self, gesture: Gtk.GestureDrag, offset_x: float, offset_y: float, plot: Gtk.DrawingArea) -> None:
        """Drag = pan: the content follows the cursor."""
        last_x, last_y = self._drag_last.get(id(plot), (0.0, 0.0))
        dx, dy = offset_x - last_x, offset_y - last_y
        self._drag_last[id(plot)] = (offset_x, offset_y)
        if dx == 0.0 and dy == 0.0:
            return
        self._set_window_for(plot, pan_window(self._window_for(plot), self._geom_for(plot), dx, dy))
        plot.queue_draw()

    def _on_plot_pressed(self, gesture: Gtk.GestureClick, n_press: int, x: float, y: float, plot: Gtk.DrawingArea, on_double_click) -> None:
        """Plot click: double-click resets the plot's zoom to its full
        window; a single click on the folded plot hit-tests the data points
        (within :data:`FOLDED_POINT_HIT_RADIUS_PX`) and toggles the outlier
        flag of the closest one through the shared lightcurve controller
        (ticket 04)."""
        if n_press == 2:
            on_double_click()
            return
        if n_press != 1 or plot is not self._folded_plot:
            return
        fp = self.folded_point_at(x, y)
        if fp is None:
            return
        try:
            now_excluded = self._controller.lightcurve_controller.toggle_excluded(fp.point.measurement_id)
        except LightcurveError as exc:
            self._set_status(f"cannot toggle outlier flag: {exc}", error=True)
            return
        state = "excluded as an outlier" if now_excluded else "included again"
        self._set_status(f"point at phase {fp.phase:.4f} {state}", error=False)

    # -- controller listener ---------------------------------------------------------------

    def _on_snapshot(self, snapshot: PeriodSnapshot) -> None:
        """Controller listener: render on the GLib main loop (notifications
        may come from a worker thread, so marshal with ``GLib.idle_add``; on
        the main thread render directly)."""
        if threading.get_ident() == self._main_thread_id:
            self._render(snapshot)
        else:
            GLib.idle_add(self._render, snapshot)

    def _render(self, snapshot: PeriodSnapshot) -> bool:
        """Render a frozen snapshot: update the result/peaks/folded/colors,
        reset the view windows when the data changed, redraw, update the
        status. Returns ``GLib.SOURCE_REMOVE`` (one-shot idle callback)."""
        lc_sessions = self._controller.lightcurve_controller.snapshot.sessions
        key = (
            id(snapshot.result),
            tuple(p.point.measurement_id for p in snapshot.folded),
            snapshot.period,
            lc_sessions,
        )
        if key != self._data_key:
            self._data_key = key
            if snapshot.result is not None:
                self._periodogram_window = periodogram_window(snapshot.result)
            self._folded_window = folded_window(snapshot.folded)
        self._result = snapshot.result
        self._peaks = snapshot.peaks
        self._folded = snapshot.folded
        self._sessions = lc_sessions
        self._colors = {sid: session_color(lc_sessions, sid) for sid in lc_sessions}
        self._period = snapshot.period
        self._source = snapshot.source
        # keep the manual entry in sync with the selected period (without
        # fighting the user while they edit)
        if snapshot.period is not None and not self._period_spin.has_focus():
            if abs(self._period_spin.get_value() - snapshot.period) > 1e-12:
                self._period_spin.set_value(snapshot.period)
        self._periodogram_plot.queue_draw()
        self._folded_plot.queue_draw()
        if snapshot.error is not None:
            self._set_status(snapshot.error, error=True)
        elif snapshot.period is not None:
            origin = (
                f"peak #{snapshot.peak_index}" if snapshot.source == SOURCE_PEAK else "manual"
            )
            fap = ""
            if snapshot.result is not None and snapshot.result.false_alarm_probability is not None:
                fap = f", FAP {snapshot.result.false_alarm_probability:.2g}"
            self._set_status(
                f"period {snapshot.period:.6g} d ({origin}{fap}) — "
                f"{len(snapshot.folded)} point(s) folded across "
                f"{len(self._sessions)} session(s); click a peak or enter a period",
                error=False,
            )
        else:
            n_peaks = len(snapshot.peaks)
            self._set_status(
                f"no period selected — compute the periodogram ({n_peaks} peak(s) ready) "
                "or enter a period manually",
                error=False,
            )
        return GLib.SOURCE_REMOVE

    # -- the plots (custom Cairo draw, fed by the pure transform math) ------------------------

    def _draw_axes(self, cr: Any, geom: PlotGeometry, window: PlotWindow, x_label: str, y_label: str, y_negated: bool) -> None:
        """Axes + 5 ticks per axis + titles. ``y_negated`` flips the y tick
        labels back (the periodogram plots -power so the max power is on top)."""
        left, top = geom.margin_left, geom.margin_top
        plot_w, plot_h = geom.plot_width, geom.plot_height
        cr.set_source_rgba(*AXIS_COLOR)
        cr.set_line_width(1.0)
        cr.move_to(left, top)
        cr.line_to(left, top + plot_h)
        cr.line_to(left + plot_w, top + plot_h)
        cr.stroke()
        cr.move_to(left + plot_w - 30, top + plot_h + 26)
        cr.show_text(x_label)
        cr.move_to(left - 34, top + 10)
        cr.show_text(y_label)
        for i in range(5):
            frac = i / 4.0
            x_val = window.jd_min + frac * window.jd_span
            x, _y = data_to_screen(window, geom, x_val, window.mag_min)
            cr.move_to(x, top + plot_h)
            cr.line_to(x, top + plot_h + 4)
            cr.stroke()
            label = f"{x_val:.6g}"
            extents = cr.text_extents(label)
            cr.move_to(x - extents.width / 2.0, top + plot_h + 14)
            cr.show_text(label)
            y_val = window.mag_min + frac * window.mag_span  # top = mag_min
            _x, y = data_to_screen(window, geom, window.jd_min, y_val)
            cr.move_to(left - 4, y)
            cr.line_to(left, y)
            cr.stroke()
            label = f"{-y_val:.3g}" if y_negated else f"{y_val:.3f}"
            extents = cr.text_extents(label)
            cr.move_to(left - 6 - extents.width, y + extents.height / 2.0)
            cr.show_text(label)

    def _draw_periodogram(self, area: Gtk.DrawingArea, cr: Any, width: int, height: int) -> None:
        """Periodogram draw func: power vs period (y = -power so the largest
        power is at the top), the spectrum as a line, the top-N peaks as
        markers (clickable)."""
        self._periodogram_geom = PlotGeometry(width, height)
        geom = self._periodogram_geom
        window = self._periodogram_window
        cr.set_font_size(9.0)
        self._draw_axes(cr, geom, window, "period [d]", "power", y_negated=True)
        if self._result is None:
            cr.set_source_rgba(*AXIS_COLOR)
            cr.move_to(geom.margin_left, geom.margin_top + geom.plot_height / 2.0)
            cr.show_text("no periodogram yet — click Compute periodogram")
            return
        freqs = self._result.frequencies
        powers = self._result.powers
        # the spectrum line (x = period = 1/f, y = -power)
        cr.set_source_rgba(*AXIS_COLOR)
        cr.set_line_width(1.0)
        started = False
        for f, p in zip(freqs, powers):
            x, y = data_to_screen(window, geom, 1.0 / float(f), -float(p))
            if not started:
                cr.move_to(x, y)
                started = True
            else:
                cr.line_to(x, y)
        cr.stroke()
        # the peak markers (highlighted; click hit-tested in _on_peak_pressed)
        for i, peak in enumerate(self._peaks):
            x, y = data_to_screen(window, geom, peak.period, -peak.power)
            cr.set_source_rgba(0.95, 0.62, 0.25, 1.0)
            cr.arc(x, y, 5.0, 0.0, 6.2832)
            cr.fill()
            cr.set_source_rgba(*AXIS_COLOR)
            cr.move_to(x + 7, y - 6)
            cr.show_text(f"#{i} {peak.period:.4g} d")

    def _draw_folded(self, area: Gtk.DrawingArea, cr: Any, width: int, height: int) -> None:
        """Folded lightcurve draw func: mag vs phase 0–1 (x = phase), one
        color per session + legend, error bars, excluded points red, the mag
        axis inverted (brighter at the top)."""
        self._folded_geom = PlotGeometry(width, height)
        geom = self._folded_geom
        window = self._folded_window
        cr.set_font_size(9.0)
        self._draw_axes(cr, geom, window, "phase", "mag", y_negated=False)
        if not self._folded:
            cr.set_source_rgba(*AXIS_COLOR)
            cr.move_to(geom.margin_left, geom.margin_top + geom.plot_height / 2.0)
            cr.show_text("no period selected — click a peak or enter a period")
            return
        # legend (top-right inside the plot)
        legend_x = geom.margin_left + geom.plot_width - 150
        for i, session_id in enumerate(self._sessions):
            color = self._colors.get(session_id, AXIS_COLOR)
            ly = geom.margin_top + 8 + i * 14
            cr.set_source_rgba(*color)
            cr.rectangle(legend_x, ly, 8, 8)
            cr.fill()
            cr.set_source_rgba(*AXIS_COLOR)
            cr.move_to(legend_x + 12, ly + 8)
            cr.show_text(session_id)
        # error bars + points (x = phase)
        for fp in self._folded:
            point = fp.point
            x, y = data_to_screen(window, geom, fp.phase, point.mag)
            color = EXCLUDED_COLOR if point.excluded else self._colors.get(
                point.session_id, AXIS_COLOR
            )
            if point.mag_err is not None:
                _x, y_faint = data_to_screen(window, geom, fp.phase, point.mag + point.mag_err)
                _x, y_bright = data_to_screen(window, geom, fp.phase, point.mag - point.mag_err)
                cr.set_source_rgba(color[0], color[1], color[2], 0.6)
                cr.set_line_width(1.0)
                cr.move_to(x, y_faint)
                cr.line_to(x, y_bright)
                cr.stroke()
            cr.set_source_rgba(*color)
            cr.arc(x, y, 3.0, 0.0, 6.2832)
            cr.fill()

    # -- status ------------------------------------------------------------------------------

    def _set_status(self, text: str, *, error: bool) -> None:
        self._status_label.set_label(text)
        for css_class in ("error", "dim-label"):
            self._status_label.remove_css_class(css_class)
        if error:
            self._status_label.add_css_class("error")
        else:
            self._status_label.add_css_class("dim-label")


def periodogram_window_of_empty() -> PlotWindow:
    """The placeholder periodogram window (no result yet): period 0.1–10 d,
    power 0–1 (negated)."""
    return PlotWindow(jd_min=0.1, jd_max=10.0, mag_min=-1.0, mag_max=0.0)
