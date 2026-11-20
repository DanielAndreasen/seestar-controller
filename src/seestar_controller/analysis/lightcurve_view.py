"""The lightcurve plot widget — the WIDGET layer of period-analysis ticket 01
(:mod:`seestar_controller.analysis.lightcurve_view`).

This module imports ``gi`` at module top (it is a widget module, the only
place allowed to): it is never imported by the pure layer
(:mod:`seestar_controller.analysis.lightcurve`) or eagerly by the package
``__init__`` — the pure layer stays gi-free and headless-testable (see
docs/wiki/assumptions.md: the sandbox has no GTK). It is re-exported as
``analysis.LightcurveView`` lazily (PEP 562 ``__getattr__``).

On the desktop the system packages are required (PyGObject is NOT a pip
dependency — it needs the system GTK headers to build)::

    sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1

:class:`LightcurveView` is a thin observer over the pure
:class:`~seestar_controller.analysis.lightcurve.LightcurveController` (the
app owns one shared instance, following the shared viewer controller's
project; the window passes it down via
:func:`seestar_controller.ui.views.build_view`):

- a **header row** — heading, a **Refresh** button (re-reads the store via
  :meth:`~seestar_controller.analysis.lightcurve.LightcurveController.refresh`)
  and a **Reset zoom** button (double-clicking the plot does the same);
- a **plot** on a :class:`Gtk.DrawingArea` (GTK4 has no chart widget, so the
  draw func paints with Cairo directly, like the photometry results view):
  mag vs JD with axes + ticks, **one color per session** (the deterministic
  palette of the pure layer) plus a **legend**, **error bars** per point,
  **excluded points marked red**, and the **inverted mag axis** (brighter at
  the top — the results-view convention). All data<->screen mapping goes
  through the PURE transform functions
  (:func:`~seestar_controller.analysis.lightcurve.data_to_screen` etc.), so
  the math is unit-tested headless;
- **zoom/pan**: a :class:`Gtk.EventControllerScroll` zooms around the cursor
  (scroll up = in; the data point under the cursor stays fixed —
  :func:`~seestar_controller.analysis.lightcurve.zoom_window_at`), a
  :class:`Gtk.GestureDrag` pans (the content follows the cursor —
  :func:`~seestar_controller.analysis.lightcurve.pan_window`), and a
  double-click (:class:`Gtk.GestureClick`, ``n_press == 2``) resets the view
  window to the full data range. The widget stores the current
  :class:`~seestar_controller.analysis.lightcurve.PlotWindow` and redraws on
  every change;
- **outlier marking (ticket 04)**: a single click (:class:`Gtk.GestureClick`,
  ``n_press == 1``) hit-tests the data points — within
  :data:`POINT_HIT_RADIUS_PX` of a point's screen position, see
  :meth:`LightcurveView.point_at` — and toggles that point's ``excluded``
  flag via
  :meth:`~seestar_controller.analysis.lightcurve.LightcurveController.toggle_excluded`
  (the store write + refresh; the point is then drawn red). A double-click
  on a point toggles it once (the ``n_press == 1`` press) and resets the
  zoom (documented interaction);
- an **empty-state placeholder**: a :class:`Gtk.Stack` switches between the
  plot and a centered label when there is no project or no measurements yet.

The view re-renders on every frozen
:class:`~seestar_controller.analysis.lightcurve.LightcurveSnapshot` (listener
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

from .lightcurve import (  # noqa: E402
    AXIS_COLOR,
    EXCLUDED_COLOR,
    ZOOM_FACTOR,
    LightcurveController,
    LightcurveError,
    LightcurvePoint,
    LightcurveSnapshot,
    PlotGeometry,
    PlotWindow,
    data_to_screen,
    full_window,
    pan_window,
    session_color,
    zoom_window_at,
)

#: Initial drawing-area content size (pixels) — the geometry used by the
#: zoom/pan handlers until the first draw reports the real size.
INITIAL_PLOT_WIDTH = 480
INITIAL_PLOT_HEIGHT = 320

#: Hit-test radius (pixels) around a data point for the click-to-toggle
#: outlier gesture (ticket 04).
POINT_HIT_RADIUS_PX = 6.0


class LightcurveView(Gtk.Box):
    """The lightcurve plot: mag vs JD for the current project, one color per
    session (legend), error bars, excluded points marked, inverted mag axis,
    zoom (scroll) / pan (drag) / double-click reset — observing a
    :class:`~seestar_controller.analysis.lightcurve.LightcurveController`.

    Constructed with the shared lightcurve controller (the app passes it; a
    fresh standalone one is created when omitted, so the widget also works
    alone).
    """

    def __init__(
        self,
        lightcurve_controller: LightcurveController | None = None,
        **kwargs,
    ) -> None:
        kwargs.setdefault("orientation", Gtk.Orientation.VERTICAL)
        kwargs.setdefault("spacing", 6)
        super().__init__(**kwargs)
        if lightcurve_controller is None:
            lightcurve_controller = LightcurveController()
        self._controller = lightcurve_controller
        self._main_thread_id = threading.get_ident()
        self._points: tuple[LightcurvePoint, ...] = ()
        self._sessions: tuple[str, ...] = ()
        self._colors: dict[str, tuple[float, float, float, float]] = {}
        self._point_ids: tuple[int, ...] = ()
        self._window: PlotWindow = full_window(())
        self._geom = PlotGeometry(INITIAL_PLOT_WIDTH, INITIAL_PLOT_HEIGHT)
        self._drag_last = (0.0, 0.0)

        # header row: heading + Refresh + Reset zoom
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.append(Gtk.Label(label="Lightcurve", xalign=0.0, css_classes=["heading"]))
        self._refresh_button = Gtk.Button(
            label="Refresh",
            tooltip_text="Re-read the project's lightcurve from the store",
        )
        self._refresh_button.connect("clicked", self._on_refresh_clicked)
        header.append(self._refresh_button)
        self._reset_button = Gtk.Button(
            label="Reset zoom",
            tooltip_text="Show the full lightcurve (double-click the plot does the same)",
        )
        self._reset_button.connect("clicked", lambda *_args: self.reset_zoom())
        header.append(self._reset_button)
        self.append(header)

        # plot | placeholder
        self._stack = Gtk.Stack()
        self._stack.set_hexpand(True)
        self._stack.set_vexpand(True)
        self._plot = Gtk.DrawingArea()
        self._plot.set_hexpand(True)
        self._plot.set_vexpand(True)
        self._plot.set_content_width(INITIAL_PLOT_WIDTH)
        self._plot.set_content_height(INITIAL_PLOT_HEIGHT)
        self._plot.set_draw_func(self._draw_plot)
        plot_frame = Gtk.Frame(child=self._plot)
        plot_frame.set_vexpand(True)
        self._stack.add_titled(plot_frame, "plot", "Plot")
        self._placeholder_label = Gtk.Label(
            label="No measurements yet",
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
            wrap=True,
            css_classes=["dim-label"],
        )
        self._stack.add_titled(self._placeholder_label, "empty", "Empty")
        self._stack.set_visible_child_name("empty")
        self.append(self._stack)

        self._status_label = Gtk.Label(label="", wrap=True, halign=Gtk.Align.CENTER)
        self.append(self._status_label)

        # zoom (scroll around the cursor) + pan (drag) + double-click reset
        scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._on_scroll)
        self._plot.add_controller(scroll)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        self._plot.add_controller(drag)
        click = Gtk.GestureClick(button=1)
        click.connect("pressed", self._on_pressed)
        self._plot.add_controller(click)

        self._controller.add_listener(self._on_snapshot)
        self._render(self._controller.snapshot)

    # -- public helpers (also used by the widget tests) ------------------------------

    @property
    def controller(self) -> LightcurveController:
        """The pure-layer lightcurve controller backing this view."""
        return self._controller

    @property
    def lightcurve_controller(self) -> LightcurveController:
        """Alias of :attr:`controller`."""
        return self._controller

    @property
    def plot(self) -> Gtk.DrawingArea:
        """The mag-vs-JD drawing area."""
        return self._plot

    @property
    def stack(self) -> Gtk.Stack:
        """The plot/placeholder stack."""
        return self._stack

    @property
    def placeholder_label(self) -> Gtk.Label:
        """The empty-state placeholder label."""
        return self._placeholder_label

    @property
    def status_label(self) -> Gtk.Label:
        """The status label (point/session counts / clean error)."""
        return self._status_label

    @property
    def refresh_button(self) -> Gtk.Button:
        """The Refresh button (re-reads the store)."""
        return self._refresh_button

    @property
    def reset_button(self) -> Gtk.Button:
        """The Reset zoom button."""
        return self._reset_button

    def plot_points(self) -> tuple[LightcurvePoint, ...]:
        """The points currently plotted (frozen :class:`LightcurvePoint`\\ s)."""
        return self._points

    def session_colors(self) -> dict[str, tuple[float, float, float, float]]:
        """The current session -> RGBA color mapping (the legend colors)."""
        return dict(self._colors)

    @property
    def window(self) -> PlotWindow:
        """The current view window (the zoom/pan state)."""
        return self._window

    def reset_zoom(self) -> PlotWindow:
        """Reset the view window to the full data range; returns it."""
        self._window = full_window(self._points)
        self._plot.queue_draw()
        return self._window

    def point_at(self, x: float, y: float) -> LightcurvePoint | None:
        """The data point within :data:`POINT_HIT_RADIUS_PX` of the plot pixel
        ``(x, y)`` (the closest one), or None — the hit-test of the
        click-to-toggle outlier gesture (ticket 04)."""
        best = None
        best_dist = POINT_HIT_RADIUS_PX
        for point in self._points:
            px, py = data_to_screen(self._window, self._geom, point.jd, point.mag)
            dist = ((px - x) ** 2 + (py - y) ** 2) ** 0.5
            if dist <= best_dist:
                best_dist = dist
                best = point
        return best

    # -- events ------------------------------------------------------------------------

    def _on_refresh_clicked(self, *_args) -> None:
        """Refresh button: re-read the store (the controller notifies -> render)."""
        self._controller.refresh()

    def _cursor_position(self, controller: Gtk.EventController) -> tuple[float, float]:
        """The cursor position within the plot for ``controller``'s current
        event; falls back to the plot center."""
        event = controller.get_current_event()
        if event is not None:
            ok, x, y = event.get_coords()
            if ok:
                return x, y
        return (
            self._geom.margin_left + self._geom.plot_width / 2.0,
            self._geom.margin_top + self._geom.plot_height / 2.0,
        )

    def _on_scroll(self, controller: Gtk.EventControllerScroll, dx: float, dy: float) -> bool:
        """Scroll = zoom around the cursor (scroll up, ``dy < 0``, zooms in).
        The data point under the cursor stays fixed (pure transform math)."""
        if not self._points:
            return False
        factor = ZOOM_FACTOR ** (-dy)
        x, y = self._cursor_position(controller)
        self._window = zoom_window_at(self._window, self._geom, x, y, factor)
        self._plot.queue_draw()
        return True  # consume: the scroll never reaches a parent scrolled window

    def _on_drag_begin(self, gesture: Gtk.GestureDrag, start_x: float, start_y: float) -> None:
        self._drag_last = (0.0, 0.0)

    def _on_drag_update(self, gesture: Gtk.GestureDrag, offset_x: float, offset_y: float) -> None:
        """Drag = pan: the content follows the cursor (pure transform math)."""
        if not self._points:
            return
        last_x, last_y = self._drag_last
        dx, dy = offset_x - last_x, offset_y - last_y
        self._drag_last = (offset_x, offset_y)
        if dx == 0.0 and dy == 0.0:
            return
        self._window = pan_window(self._window, self._geom, dx, dy)
        self._plot.queue_draw()

    def _on_pressed(self, gesture: Gtk.GestureClick, n_press: int, x: float, y: float) -> None:
        """Single click on a data point toggles its outlier flag (ticket 04);
        double-click resets the zoom to the full data range."""
        if n_press == 2:
            self.reset_zoom()
            return
        if n_press != 1:
            return
        point = self.point_at(x, y)
        if point is None:
            return
        try:
            excluded = self._controller.toggle_excluded(point.measurement_id)
        except LightcurveError as exc:
            self._set_status(f"cannot flag the point: {exc}", error=True)
            return
        self._set_status(
            f"point {point.measurement_id} (jd {point.jd:.6f}) "
            + ("excluded as an outlier" if excluded else "included again")
            + " — it no longer feeds the periodogram",
            error=False,
        )

    # -- controller listener ---------------------------------------------------------------

    def _on_snapshot(self, snapshot: LightcurveSnapshot) -> None:
        """Controller listener: render on the GLib main loop (notifications
        may come from a worker thread, so marshal with ``GLib.idle_add``; on
        the main thread render directly)."""
        if threading.get_ident() == self._main_thread_id:
            self._render(snapshot)
        else:
            GLib.idle_add(self._render, snapshot)

    def _render(self, snapshot: LightcurveSnapshot) -> bool:
        """Render a frozen snapshot: update the points/colors, reset the view
        window when the data changed, switch plot/placeholder, redraw, and
        update the status. Returns ``GLib.SOURCE_REMOVE`` (one-shot idle
        callback)."""
        ids = tuple(p.measurement_id for p in snapshot.points)
        if ids != self._point_ids:
            self._point_ids = ids
            self._window = full_window(snapshot.points)
        self._points = snapshot.points
        self._sessions = snapshot.sessions
        self._colors = {sid: session_color(snapshot.sessions, sid) for sid in snapshot.sessions}
        if self._controller.project is None:
            self._stack.set_visible_child_name("empty")
            self._placeholder_label.set_label(
                "No project open — open a project to see its lightcurve"
            )
        elif not snapshot.points:
            self._stack.set_visible_child_name("empty")
            self._placeholder_label.set_label(
                "No measurements yet — run photometry on a session first"
            )
        else:
            self._stack.set_visible_child_name("plot")
        self._plot.queue_draw()
        if snapshot.error is not None:
            self._set_status(snapshot.error, error=True)
        else:
            self._set_status(
                f"{len(snapshot.points)} point(s) across "
                f"{len(snapshot.sessions)} session(s) — scroll to zoom, drag to pan, "
                "double-click to reset, click a point to toggle its outlier flag",
                error=False,
            )
        return GLib.SOURCE_REMOVE

    # -- the plot (custom Cairo draw, fed by the pure transform math) ------------------------

    def _draw_plot(self, area: Gtk.DrawingArea, cr: Any, width: int, height: int) -> None:
        """DrawingArea draw func: mag vs jd — axes + ticks + legend + error
        bars + one colored point per session (excluded in red); the mag axis
        is inverted (brighter at the top). All mapping goes through the pure
        :mod:`.lightcurve` transform functions."""
        self._geom = PlotGeometry(width, height)
        geom = self._geom
        window = self._window
        left, top = geom.margin_left, geom.margin_top
        plot_w, plot_h = geom.plot_width, geom.plot_height
        cr.set_font_size(9.0)
        points = self._points
        if not points:
            cr.set_source_rgba(*AXIS_COLOR)
            cr.move_to(left, top + plot_h / 2.0)
            cr.show_text("no measurements yet")
            return

        # axes
        cr.set_source_rgba(*AXIS_COLOR)
        cr.set_line_width(1.0)
        cr.move_to(left, top)
        cr.line_to(left, top + plot_h)
        cr.line_to(left + plot_w, top + plot_h)
        cr.stroke()
        # axis titles
        cr.move_to(left + plot_w - 14, top + plot_h + 26)
        cr.show_text("JD")
        cr.move_to(left - 40, top + 10)
        cr.show_text("mag")
        # ticks + labels (5 per axis)
        for i in range(5):
            frac = i / 4.0
            jd = window.jd_min + frac * window.jd_span
            x, _y = data_to_screen(window, geom, jd, window.mag_min)
            cr.move_to(x, top + plot_h)
            cr.line_to(x, top + plot_h + 4)
            cr.stroke()
            label = f"{jd:.5f}"
            extents = cr.text_extents(label)
            cr.move_to(x - extents.width / 2.0, top + plot_h + 14)
            cr.show_text(label)
            mag = window.mag_min + frac * window.mag_span  # top = mag_min (brighter)
            _x, y = data_to_screen(window, geom, window.jd_min, mag)
            cr.move_to(left - 4, y)
            cr.line_to(left, y)
            cr.stroke()
            label = f"{mag:.3f}"
            extents = cr.text_extents(label)
            cr.move_to(left - 6 - extents.width, y + extents.height / 2.0)
            cr.show_text(label)
        # legend (top-right inside the plot): one color chip + session id per session
        legend_x = left + plot_w - 150
        for i, session_id in enumerate(self._sessions):
            color = self._colors.get(session_id, AXIS_COLOR)
            ly = top + 8 + i * 14
            cr.set_source_rgba(*color)
            cr.rectangle(legend_x, ly, 8, 8)
            cr.fill()
            cr.set_source_rgba(*AXIS_COLOR)
            cr.move_to(legend_x + 12, ly + 8)
            cr.show_text(session_id)
        # error bars + points (one color per session, excluded in red)
        for point in points:
            x, y = data_to_screen(window, geom, point.jd, point.mag)
            color = EXCLUDED_COLOR if point.excluded else self._colors.get(
                point.session_id, AXIS_COLOR
            )
            if point.mag_err is not None:
                _x, y_faint = data_to_screen(window, geom, point.jd, point.mag + point.mag_err)
                _x, y_bright = data_to_screen(window, geom, point.jd, point.mag - point.mag_err)
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
