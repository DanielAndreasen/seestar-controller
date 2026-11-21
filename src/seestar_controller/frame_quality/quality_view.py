"""The session-quality view widget — the WIDGET layer of frame-quality ticket
02 (:mod:`seestar_controller.frame_quality.quality_view`).

This module imports ``gi`` at module top (it is a widget module, the only
place allowed to): it is never imported by the pure layer
(:mod:`seestar_controller.frame_quality.trends`) or eagerly by the package
``__init__`` — the pure layer stays gi-free and headless-testable (see
docs/wiki/assumptions.md: the sandbox has no GTK). It is re-exported as
``frame_quality.SessionQualityView`` lazily (PEP 562 ``__getattr__``).

On the desktop the system packages are required (PyGObject is NOT a pip
dependency — it needs the system GTK headers to build)::

    sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1

The widget reuses the analysis plotting widgets of the period-analysis
lightcurve view (:mod:`seestar_controller.analysis.lightcurve_view`): the
:class:`Gtk.DrawingArea` + Cairo draw pattern, the
:class:`~seestar_controller.analysis.lightcurve.PlotGeometry` pixel geometry,
and the :data:`~seestar_controller.analysis.lightcurve.AXIS_COLOR` /
:data:`~seestar_controller.analysis.lightcurve.EXCLUDED_COLOR` /
:data:`~seestar_controller.analysis.lightcurve.SESSION_PALETTE` colors. All
data<->screen mapping goes through the PURE transform functions of
:mod:`.trends` (:func:`~.trends.trend_to_screen` etc.), so the math is
unit-tested headless.

:class:`SessionQualityView` is a thin observer over the pure
:class:`~seestar_controller.frame_quality.trends.QualityController` (the app
owns one shared instance, following the shared viewer controller's project;
the window passes it down via
:func:`seestar_controller.ui.views.build_view`):

- a **header row** — heading, a **session selector** (:class:`Gtk.DropDown`
  fed from the controller's sessions, the same pattern as the photometry
  results panel's session chooser) and a **Refresh** button (re-reads the
  store via :meth:`~seestar_controller.frame_quality.trends.QualityController.refresh`);
- a **per-session summary card** — session, target, frames scored, flagged
  count with the rejected percent, median FWHM and median background;
- **three trend plots** (:class:`TrendPlot`, one :class:`Gtk.DrawingArea`
  each, painted with Cairo): **FWHM vs time** (focus drift), **background vs
  time** (sky quality / clouds) and **airmass vs time** (the session target's
  sec z at the controller's observer — placeholder text when the target
  coordinates are unknown). Each plot draws axes + ticks + title, connects
  the points with a line and marks the **flagged (rejected) frames red**;
- a **status label** (frame/rejected counts, or a clean error message).

The view re-renders on every frozen
:class:`~seestar_controller.frame_quality.trends.QualitySnapshot` (listener
notifications marshalled onto the GLib main loop when they come from a worker
thread).
"""

from __future__ import annotations

import threading
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import GLib, Gtk

from seestar_controller.analysis.lightcurve import (
    AXIS_COLOR,
    EXCLUDED_COLOR,
    SESSION_PALETTE,
    PlotGeometry,
)

from .trends import (
    DEFAULT_TREND_WINDOW,
    QualityController,
    QualitySnapshot,
    TrendPoint,
    TrendWindow,
    full_trend_window,
    trend_to_screen,
)

#: Initial drawing-area content size (pixels) — the geometry used until the
#: first draw reports the real size (same idea as the lightcurve view).
INITIAL_PLOT_WIDTH = 400
INITIAL_PLOT_HEIGHT = 150

#: The trend series color (the session palette's blue — the same palette the
#: lightcurve view uses for its first session).
SERIES_COLOR = SESSION_PALETTE[0]


class TrendPlot(Gtk.DrawingArea):
    """One trend plot: x = JD, y = the metric — axes + ticks + title, the
    points connected by a line, flagged (rejected) points marked red.

    The data<->screen mapping goes through the pure
    :func:`~seestar_controller.frame_quality.trends.trend_to_screen` with the
    analysis lightcurve view's :class:`PlotGeometry` (duck-typed geom).
    """

    def __init__(self, title: str, y_label: str, **kwargs) -> None:
        kwargs.setdefault("hexpand", True)
        kwargs.setdefault("vexpand", True)
        kwargs.setdefault("content_width", INITIAL_PLOT_WIDTH)
        kwargs.setdefault("content_height", INITIAL_PLOT_HEIGHT)
        super().__init__(**kwargs)
        self._title = title
        self._y_label = y_label
        self._points: tuple[TrendPoint, ...] = ()
        self._values: tuple[float | None, ...] = ()
        self._placeholder = "no data"
        self._window: TrendWindow = DEFAULT_TREND_WINDOW
        self._geom = PlotGeometry(INITIAL_PLOT_WIDTH, INITIAL_PLOT_HEIGHT, margin_top=20)
        self.set_draw_func(self._draw)

    # -- public helpers (also used by the widget tests) ------------------------------

    @property
    def title(self) -> str:
        """The plot's title (drawn above the plot)."""
        return self._title

    @property
    def y_label(self) -> str:
        """The y axis label."""
        return self._y_label

    def points(self) -> tuple[TrendPoint, ...]:
        """The trend points currently plotted."""
        return self._points

    def values(self) -> tuple[float | None, ...]:
        """The plotted y values (aligned with :meth:`points`; None = not drawn)."""
        return self._values

    @property
    def window(self) -> TrendWindow:
        """The current view window."""
        return self._window

    def set_series(
        self,
        points: tuple[TrendPoint, ...],
        values: tuple[float | None, ...],
        *,
        placeholder: str | None = None,
    ) -> None:
        """Set the plotted series (``values`` aligned with ``points``; None
        values are not drawn) and reset the window to the full data range.
        ``placeholder`` overrides the empty-state text."""
        if len(values) != len(points):
            raise ValueError(
                f"values must align with points ({len(values)} != {len(points)})"
            )
        self._points = tuple(points)
        self._values = tuple(values)
        if placeholder is not None:
            self._placeholder = placeholder
        drawable = [v for v in self._values if v is not None]
        if self._points and drawable:
            self._window = full_trend_window(
                [p.jd for p in self._points], drawable  # type: ignore[arg-type]
            )
        else:
            self._window = DEFAULT_TREND_WINDOW
        self.queue_draw()

    # -- the plot (custom Cairo draw, fed by the pure transform math) ------------------

    def _draw(self, area: Gtk.DrawingArea, cr: Any, width: int, height: int) -> None:
        """DrawingArea draw func: axes + ticks + title + the point series
        (flagged points red)."""
        self._geom = PlotGeometry(width, height, margin_top=20)
        geom = self._geom
        window = self._window
        left, top = geom.margin_left, geom.margin_top
        plot_w, plot_h = geom.plot_width, geom.plot_height
        cr.set_font_size(9.0)
        # title (above the plot, inside the enlarged top margin)
        cr.set_source_rgba(*AXIS_COLOR)
        cr.move_to(left, top - 8)
        cr.show_text(self._title)
        drawable = [
            (p, v) for p, v in zip(self._points, self._values) if v is not None
        ]
        if not drawable:
            cr.set_source_rgba(*AXIS_COLOR)
            cr.move_to(left + plot_w / 2.0 - 30, top + plot_h / 2.0)
            cr.show_text(self._placeholder)
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
        cr.show_text(self._y_label)
        # ticks + labels (5 per axis)
        for i in range(5):
            frac = i / 4.0
            x_val = window.x_min + frac * window.x_span
            px, _py = trend_to_screen(window, geom, x_val, window.y_min)
            cr.move_to(px, top + plot_h)
            cr.line_to(px, top + plot_h + 4)
            cr.stroke()
            label = f"{x_val:.5f}"
            extents = cr.text_extents(label)
            cr.move_to(px - extents.width / 2.0, top + plot_h + 14)
            cr.show_text(label)
            y_val = window.y_min + frac * window.y_span
            _px, py = trend_to_screen(window, geom, window.x_min, y_val)
            cr.move_to(left - 4, py)
            cr.line_to(left, py)
            cr.stroke()
            label = f"{y_val:.4g}"
            extents = cr.text_extents(label)
            cr.move_to(left - 6 - extents.width, py + extents.height / 2.0)
            cr.show_text(label)
        # the series: a connecting line + one circle per point (flagged red)
        screen_points = [
            (trend_to_screen(window, geom, p.jd, v), p.flagged) for p, v in drawable
        ]
        cr.set_source_rgba(SERIES_COLOR[0], SERIES_COLOR[1], SERIES_COLOR[2], 0.6)
        cr.set_line_width(1.0)
        for (px, py), _flagged in screen_points:
            cr.line_to(px, py)
        cr.stroke()
        for (px, py), flagged in screen_points:
            color = EXCLUDED_COLOR if flagged else SERIES_COLOR
            cr.set_source_rgba(*color)
            cr.arc(px, py, 3.0, 0.0, 6.2832)
            cr.fill()


class SessionQualityView(Gtk.Box):
    """The session-quality view: session selector + Refresh on top, the
    per-session summary card, and the three trend plots (FWHM / background /
    airmass vs time) — observing a
    :class:`~seestar_controller.frame_quality.trends.QualityController`.

    Constructed with the shared quality controller (the app passes it; a
    fresh standalone one is created when omitted, so the widget also works
    alone).
    """

    def __init__(
        self,
        quality_controller: QualityController | None = None,
        **kwargs,
    ) -> None:
        kwargs.setdefault("orientation", Gtk.Orientation.VERTICAL)
        kwargs.setdefault("spacing", 6)
        super().__init__(**kwargs)
        if quality_controller is None:
            quality_controller = QualityController()
        self._controller = quality_controller
        self._main_thread_id = threading.get_ident()
        self._session_ids: list[str] = []

        # header row: heading + session selector + Refresh
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.append(Gtk.Label(label="Session quality", xalign=0.0, css_classes=["heading"]))
        self._session_dropdown = Gtk.DropDown(
            tooltip_text="The session whose quality trends are shown",
        )
        self._session_dropdown.set_hexpand(True)
        self._session_dropdown.connect("notify::selected", self._on_session_selected)
        header.append(self._session_dropdown)
        self._refresh_button = Gtk.Button(
            label="Refresh",
            tooltip_text="Re-read the project's sessions and quality records from the store",
        )
        self._refresh_button.connect("clicked", self._on_refresh_clicked)
        header.append(self._refresh_button)
        self.append(header)

        # the per-session summary card
        self._summary_card = Gtk.Frame()
        self._summary_card.add_css_class("card")
        grid = Gtk.Grid(
            column_spacing=12, row_spacing=2, margin_top=6, margin_bottom=6,
            margin_start=12, margin_end=12,
        )
        self._summary_card.set_child(grid)
        self._session_value_label = self._summary_row(grid, 0, "Session")
        self._target_value_label = self._summary_row(grid, 1, "Target")
        self._frames_value_label = self._summary_row(grid, 2, "Frames scored")
        self._flagged_value_label = self._summary_row(grid, 3, "Rejected")
        self._fwhm_value_label = self._summary_row(grid, 4, "Median FWHM")
        self._background_value_label = self._summary_row(grid, 5, "Median background")
        self.append(self._summary_card)

        # the three trend plots
        plots_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._fwhm_plot = TrendPlot("FWHM vs time (focus drift)", "FWHM (px)")
        self._background_plot = TrendPlot("Background vs time (sky quality)", "bg (ADU)")
        self._airmass_plot = TrendPlot("Airmass vs time", "airmass")
        for plot in (self._fwhm_plot, self._background_plot, self._airmass_plot):
            frame = Gtk.Frame(child=plot)
            plots_box.append(frame)
        self.append(plots_box)

        self._status_label = Gtk.Label(label="", wrap=True, halign=Gtk.Align.CENTER)
        self.append(self._status_label)

        self._controller.add_listener(self._on_snapshot)
        self._render(self._controller.snapshot)

    @staticmethod
    def _summary_row(grid: Gtk.Grid, row: int, caption: str) -> Gtk.Label:
        """One summary-card row: a dim caption + a value label; returns the
        value label."""
        caption_label = Gtk.Label(
            label=caption, xalign=0.0, halign=Gtk.Align.START, css_classes=["dim-label"]
        )
        value_label = Gtk.Label(label="—", xalign=0.0, halign=Gtk.Align.START)
        grid.attach(caption_label, 0, row, 1, 1)
        grid.attach(value_label, 1, row, 1, 1)
        return value_label

    # -- public helpers (also used by the widget tests) ------------------------------

    @property
    def controller(self) -> QualityController:
        """The pure-layer quality controller backing this view."""
        return self._controller

    @property
    def quality_controller(self) -> QualityController:
        """Alias of :attr:`controller`."""
        return self._controller

    @property
    def session_dropdown(self) -> Gtk.DropDown:
        """The session selector."""
        return self._session_dropdown

    @property
    def refresh_button(self) -> Gtk.Button:
        """The Refresh button (re-reads the store)."""
        return self._refresh_button

    @property
    def summary_card(self) -> Gtk.Frame:
        """The per-session summary card."""
        return self._summary_card

    @property
    def session_value_label(self) -> Gtk.Label:
        """The summary card's session value."""
        return self._session_value_label

    @property
    def target_value_label(self) -> Gtk.Label:
        """The summary card's target value."""
        return self._target_value_label

    @property
    def frames_value_label(self) -> Gtk.Label:
        """The summary card's frames-scored value."""
        return self._frames_value_label

    @property
    def flagged_value_label(self) -> Gtk.Label:
        """The summary card's rejected value (count + percent)."""
        return self._flagged_value_label

    @property
    def fwhm_value_label(self) -> Gtk.Label:
        """The summary card's median-FWHM value."""
        return self._fwhm_value_label

    @property
    def background_value_label(self) -> Gtk.Label:
        """The summary card's median-background value."""
        return self._background_value_label

    @property
    def fwhm_plot(self) -> TrendPlot:
        """The FWHM-vs-time plot."""
        return self._fwhm_plot

    @property
    def background_plot(self) -> TrendPlot:
        """The background-vs-time plot."""
        return self._background_plot

    @property
    def airmass_plot(self) -> TrendPlot:
        """The airmass-vs-time plot."""
        return self._airmass_plot

    @property
    def status_label(self) -> Gtk.Label:
        """The status label (counts / clean error)."""
        return self._status_label

    def selected_session_id(self) -> str | None:
        """The session id currently chosen in the dropdown (None when empty)."""
        index = self._session_dropdown.get_selected()
        if 0 <= index < len(self._session_ids):
            return self._session_ids[index]
        return None

    def refresh_sessions(self) -> list:
        """Re-list the project's sessions into the dropdown (keeps the current
        selection when still present). Returns the session ids."""
        sessions = list(self._controller.sessions())
        previous = self.selected_session_id()
        self._session_ids = [s for s in sessions]
        labels = list(self._session_ids)
        self._session_dropdown.set_model(Gtk.StringList.new(labels))
        if self._session_ids:
            index = (
                self._session_ids.index(previous) if previous in self._session_ids else 0
            )
            self._session_dropdown.set_selected(index)
        return self._session_ids

    # -- events ------------------------------------------------------------------------

    def _on_refresh_clicked(self, *_args) -> None:
        """Refresh button: re-read the store (the controller notifies -> render)."""
        self._controller.refresh()

    def _on_session_selected(self, dropdown: Gtk.DropDown, *_args) -> None:
        """Dropdown selection changed -> select that session on the controller
        (which notifies -> render)."""
        session_id = self.selected_session_id()
        if session_id is not None and session_id != self._controller.selected_session:
            self._controller.select_session(session_id)

    # -- controller listener ---------------------------------------------------------------

    def _on_snapshot(self, snapshot: QualitySnapshot) -> None:
        """Controller listener: render on the GLib main loop (notifications
        may come from a worker thread, so marshal with ``GLib.idle_add``; on
        the main thread render directly)."""
        if threading.get_ident() == self._main_thread_id:
            self._render(snapshot)
        else:
            GLib.idle_add(self._render, snapshot)

    def _render(self, snapshot: QualitySnapshot) -> bool:
        """Render a frozen snapshot: update the dropdown (only when the
        session list changed, so the selection is not reset on every render),
        the summary card, the three plots, and the status. Returns
        ``GLib.SOURCE_REMOVE`` (one-shot idle callback)."""
        if tuple(snapshot.sessions) != tuple(self._session_ids):
            previous = self.selected_session_id()
            self._session_ids = list(snapshot.sessions)
            labels = list(self._session_ids)
            self._session_dropdown.set_model(Gtk.StringList.new(labels))
            if self._session_ids:
                if previous in self._session_ids:
                    index = self._session_ids.index(previous)
                elif snapshot.session_id in self._session_ids:
                    index = self._session_ids.index(snapshot.session_id)
                else:
                    index = 0
                self._session_dropdown.set_selected(index)
        trends = snapshot.trends
        if trends is None:
            self._session_value_label.set_label(snapshot.session_id or "—")
            self._target_value_label.set_label("—")
            self._frames_value_label.set_label("0")
            self._flagged_value_label.set_label("0 (0.0%)")
            self._fwhm_value_label.set_label("—")
            self._background_value_label.set_label("—")
            self._fwhm_plot.set_series((), ())
            self._background_plot.set_series((), ())
            self._airmass_plot.set_series(
                (), (), placeholder="target coordinates unknown — no airmass"
            )
        else:
            self._session_value_label.set_label(trends.session_id)
            self._target_value_label.set_label(trends.target or "—")
            self._frames_value_label.set_label(str(trends.frame_count))
            self._flagged_value_label.set_label(
                f"{trends.flagged_count} ({trends.rejected_percent:.1f}%)"
            )
            self._fwhm_value_label.set_label(
                "—" if trends.median_fwhm is None else f"{trends.median_fwhm:.2f} px"
            )
            self._background_value_label.set_label(
                "—" if trends.median_bg_median is None else f"{trends.median_bg_median:.1f} ADU"
            )
            self._fwhm_plot.set_series(trends.points, tuple(p.fwhm for p in trends.points))
            self._background_plot.set_series(
                trends.points, tuple(p.bg_median for p in trends.points)
            )
            if trends.has_airmass:
                self._airmass_plot.set_series(trends.points, trends.airmass)
            else:
                self._airmass_plot.set_series(
                    (), (), placeholder="target coordinates unknown — no airmass"
                )
        if snapshot.error is not None:
            self._set_status(snapshot.error, error=True)
        elif self._controller.project is None:
            self._set_status("No project open — open a project to see its session quality", error=False)
        elif trends is None:
            self._set_status("No sessions yet — store some sub-frames first", error=False)
        else:
            self._set_status(
                f"{trends.frame_count} scored frame(s), {trends.flagged_count} rejected "
                f"({trends.rejected_percent:.1f}%) — red points are flagged outliers",
                error=False,
            )
        return GLib.SOURCE_REMOVE

    # -- status ------------------------------------------------------------------------------

    def _set_status(self, text: str, *, error: bool) -> None:
        self._status_label.set_label(text)
        for css_class in ("error", "dim-label"):
            self._status_label.remove_css_class(css_class)
        if error:
            self._status_label.add_css_class("error")
        else:
            self._status_label.add_css_class("dim-label")
