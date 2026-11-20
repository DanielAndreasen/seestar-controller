"""The photometry results panel — the WIDGET layer of photometry ticket 04
(:mod:`seestar_controller.photometry.results_view`).

This module imports ``gi`` at module top (it is a widget module, the only
place allowed to): it is never imported by the pure layer
(:mod:`seestar_controller.photometry.run` / :mod:`.picking` / :mod:`.measure`)
or eagerly by the package ``__init__`` — the pure layer stays gi-free and
headless-testable (see docs/wiki/assumptions.md: the sandbox has no GTK). It
is re-exported as ``photometry.ResultsPanel`` lazily (PEP 562 ``__getattr__``).

On the desktop the system packages are required (PyGObject is NOT a pip
dependency — it needs the system GTK headers to build)::

    sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1

:class:`ResultsPanel` is a thin observer over the pure
:class:`~seestar_controller.photometry.run.RunController` (the app owns one
shared instance, bound to the shared
:class:`~seestar_controller.photometry.picking.PickController`; the window
passes it down via :func:`seestar_controller.ui.views.build_view`):

- a **session chooser** (:class:`Gtk.DropDown` fed from
  :meth:`~seestar_controller.photometry.run.RunController.sessions`) + a
  **Refresh** button + a **Run** button — clicking Run measures ALL sub-frames
  of the chosen session via
  :meth:`~seestar_controller.photometry.run.RunController.run_session`. The
  pure method is synchronous and GTK-free, so the panel runs it on a daemon
  **worker thread** and marshals the progress callback (and the listener
  notifications) back to the GLib main loop with :func:`GLib.idle_add` — the
  main loop NEVER blocks on the FITS reads / photutils measuring;
- a **progress bar + label** while running (``done/total — frame N``);
- a **per-frame table** (:class:`Gtk.ListBox`, one row per lightcurve point:
  jd/utc, mag, mag_err, an "excluded" :class:`Gtk.CheckButton`) — clicking a
  row (or the checkbox) toggles the outlier flag via
  :meth:`~seestar_controller.photometry.run.RunController.set_excluded`, which
  refreshes BOTH the table and the plot;
- a **scatter plot** of mag vs jd on a :class:`Gtk.DrawingArea` (GTK4 has no
  chart widget, so the draw func paints axes + ticks + points + error bars
  with Cairo directly; the mag axis is inverted — brighter at the top — and
  excluded points are drawn in red).

The panel re-renders on every frozen
:class:`~seestar_controller.photometry.run.RunSnapshot`.
"""

from __future__ import annotations

import threading
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import GLib, Gtk  # noqa: E402

from .run import RunController, RunError, RunSnapshot  # noqa: E402

#: Plot margins (pixels): left (jd axis labels), right, top, bottom (jd ticks).
PLOT_MARGIN_LEFT = 64
PLOT_MARGIN_RIGHT = 12
PLOT_MARGIN_TOP = 12
PLOT_MARGIN_BOTTOM = 30

#: Plot colors (RGBA): axes/text gray, points blue, excluded points red.
AXIS_COLOR = (0.55, 0.55, 0.58, 1.0)
POINT_COLOR = (0.35, 0.60, 0.95, 1.0)
EXCLUDED_COLOR = (0.90, 0.35, 0.30, 1.0)

#: One plotted point: ``(jd, mag, mag_err, excluded)``.
PlotPoint = tuple[float, float, float | None, bool]


class ResultsPanel(Gtk.Box):
    """The photometry results panel: session chooser + Run button + progress
    + per-frame table + mag-vs-jd scatter plot, observing a
    :class:`~seestar_controller.photometry.run.RunController`.

    Constructed with the shared run controller (the app passes it; a fresh
    standalone one is created when omitted, so the widget also works alone).
    """

    def __init__(
        self,
        run_controller: RunController | None = None,
        **kwargs,
    ) -> None:
        kwargs.setdefault("orientation", Gtk.Orientation.VERTICAL)
        kwargs.setdefault("spacing", 6)
        super().__init__(**kwargs)
        if run_controller is None:
            run_controller = RunController()
        self._controller = run_controller
        self._main_thread_id = threading.get_ident()
        self._run_thread: threading.Thread | None = None
        self._session_ids: list[str] = []
        self._updating = False  # guard against programmatic signal loops
        self._plot_points: tuple[PlotPoint, ...] = ()

        heading = Gtk.Label(label="Results", xalign=0.0, css_classes=["heading"])
        self.append(heading)

        run_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._session_dropdown = Gtk.DropDown(
            model=Gtk.StringList.new([]),
            tooltip_text="The session whose sub-frames are measured",
        )
        self._session_dropdown.set_hexpand(True)
        run_row.append(self._session_dropdown)
        self._refresh_button = Gtk.Button(
            label="Refresh",
            tooltip_text="Re-list the project's sessions and the stored lightcurve",
        )
        self._refresh_button.connect("clicked", self._on_refresh_clicked)
        run_row.append(self._refresh_button)
        self._run_button = Gtk.Button(
            label="Run",
            tooltip_text=(
                "Measure all sub-frames of the chosen session (worker thread, "
                "progress shown below) and append the results to the lightcurve"
            ),
        )
        self._run_button.connect("clicked", self._on_run_clicked)
        run_row.append(self._run_button)
        self.append(run_row)

        progress_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._progress_bar = Gtk.ProgressBar()
        self._progress_bar.set_hexpand(True)
        self._progress_bar.set_fraction(0.0)
        progress_row.append(self._progress_bar)
        self._progress_label = Gtk.Label(
            label="", xalign=0.0, css_classes=["dim-label", "caption"]
        )
        progress_row.append(self._progress_label)
        self.append(progress_row)

        # per-frame table + scatter plot side by side
        paned = Gtk.Paned(
            orientation=Gtk.Orientation.HORIZONTAL,
            start_child=self._build_table(),
            end_child=self._build_plot(),
            shrink_start_child=False,
            shrink_end_child=False,
            wide_handle=True,
        )
        paned.set_vexpand(True)
        self.append(paned)

        self._status_label = Gtk.Label(label="", wrap=True, halign=Gtk.Align.CENTER)
        self.append(self._status_label)

        self._controller.add_listener(self._on_snapshot)
        self.refresh_sessions()
        self._render(self._controller.snapshot)

    # -- construction ------------------------------------------------------------

    def _build_table(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)
        box.append(Gtk.Label(label="Measurements (click a row to flag/unflag an outlier)",
                             xalign=0.0, css_classes=["caption", "dim-label"]))
        self._list_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self._list_box.add_css_class("boxed-list")
        self._list_box.connect("row-activated", self._on_row_activated)
        scrolled = Gtk.ScrolledWindow(
            child=self._list_box,
            min_content_width=300,
            min_content_height=120,
            max_content_height=220,
            hscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
        )
        scrolled.set_vexpand(True)
        box.append(scrolled)
        return box

    def _build_plot(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)
        box.set_hexpand(True)
        box.set_vexpand(True)
        box.append(Gtk.Label(label="mag vs jd (brighter at the top)", xalign=0.0,
                             css_classes=["caption", "dim-label"]))
        self._plot = Gtk.DrawingArea()
        self._plot.set_hexpand(True)
        self._plot.set_vexpand(True)
        self._plot.set_content_width(320)
        self._plot.set_content_height(200)
        self._plot.set_draw_func(self._draw_plot)
        frame = Gtk.Frame(child=self._plot)
        frame.set_vexpand(True)
        box.append(frame)
        return box

    # -- public helpers (also used by the widget tests) ------------------------------

    @property
    def controller(self) -> RunController:
        """The pure-layer run controller backing this panel."""
        return self._controller

    @property
    def run_controller(self) -> RunController:
        """Alias of :attr:`controller`."""
        return self._controller

    @property
    def run_button(self) -> Gtk.Button:
        """The Run button (starts the worker-thread run)."""
        return self._run_button

    @property
    def refresh_button(self) -> Gtk.Button:
        """The Refresh button (re-lists sessions + lightcurve)."""
        return self._refresh_button

    @property
    def session_dropdown(self) -> Gtk.DropDown:
        """The session chooser."""
        return self._session_dropdown

    @property
    def progress_bar(self) -> Gtk.ProgressBar:
        """The progress bar shown while a run is in progress."""
        return self._progress_bar

    @property
    def progress_label(self) -> Gtk.Label:
        """The progress label (``done/total — frame N`` while running)."""
        return self._progress_label

    @property
    def list_box(self) -> Gtk.ListBox:
        """The per-frame measurements table."""
        return self._list_box

    @property
    def plot(self) -> Gtk.DrawingArea:
        """The mag-vs-jd scatter plot drawing area."""
        return self._plot

    @property
    def status_label(self) -> Gtk.Label:
        """The status label (run summary / clean error)."""
        return self._status_label

    def measurement_rows(self) -> list[Gtk.ListBoxRow]:
        """The current table rows (each carries ``measurement_id``, ``excluded``
        and an ``excluded_check`` :class:`Gtk.CheckButton`)."""
        rows: list[Gtk.ListBoxRow] = []
        index = 0
        while (row := self._list_box.get_row_at_index(index)) is not None:
            rows.append(row)
            index += 1
        return rows

    def plot_points(self) -> tuple[PlotPoint, ...]:
        """The points currently plotted: ``(jd, mag, mag_err, excluded)``."""
        return self._plot_points

    def selected_session_id(self) -> str | None:
        """The session id currently chosen in the dropdown (None when empty)."""
        index = self._session_dropdown.get_selected()
        if 0 <= index < len(self._session_ids):
            return self._session_ids[index]
        return None

    def refresh_sessions(self) -> list:
        """Re-list the project's sessions into the dropdown (keeps the current
        selection when still present). Returns the sessions (empty on error,
        which is shown in the status label)."""
        try:
            sessions = self._controller.sessions()
        except RunError as exc:
            self._set_status(str(exc), error=True)
            return []
        previous = self.selected_session_id()
        self._session_ids = [s.id for s in sessions]
        labels = [f"{s.started_at} — {s.id}" for s in sessions]
        self._session_dropdown.set_model(Gtk.StringList.new(labels))
        if self._session_ids:
            index = self._session_ids.index(previous) if previous in self._session_ids else 0
            self._session_dropdown.set_selected(index)
        return sessions

    def run(self) -> threading.Thread | None:
        """Start the run of the selected session on a daemon worker thread
        (same as clicking Run); returns the thread, so callers (tests) can
        ``join()`` it. Re-entrant: a run already in progress is not restarted."""
        self._on_run_clicked()
        return self._run_thread

    # -- run (worker thread + GLib.idle_add marshalling) ------------------------------

    def _on_run_clicked(self, *_args) -> None:
        if self._run_thread is not None and self._run_thread.is_alive():
            return
        session_id = self.selected_session_id()
        if session_id is None:
            self._set_status("no session to run — store some sub-frames first", error=True)
            return
        self._set_status(f"measuring session {session_id}…", error=False)
        self._run_button.set_sensitive(False)

        def work() -> None:
            try:
                # synchronous + GTK-free: safe on this worker thread; the store
                # writes are marshalled to the db-owning thread by the controller
                self._controller.run_session(session_id, on_progress=self._on_progress)
            except Exception as exc:  # surfaced as a clean status, never a crash
                GLib.idle_add(self._on_run_failed, f"{type(exc).__name__}: {exc}")

        self._run_thread = threading.Thread(
            target=work, name="photometry-run", daemon=True
        )
        self._run_thread.start()

    def _on_progress(self, done: int, total: int, frame_id: int) -> None:
        """The run's progress callback — called on the WORKER thread, so
        marshal the UI update onto the GLib main loop."""
        GLib.idle_add(self._render_progress, done, total, frame_id)

    def _render_progress(self, done: int, total: int, frame_id: int) -> bool:
        fraction = (done / total) if total else 0.0
        self._progress_bar.set_fraction(fraction)
        self._progress_label.set_label(f"measuring {done}/{total} — frame {frame_id}")
        return GLib.SOURCE_REMOVE

    def _on_run_failed(self, message: str) -> bool:
        self._set_status(message, error=True)
        self._run_button.set_sensitive(True)
        return GLib.SOURCE_REMOVE

    def _on_refresh_clicked(self, *_args) -> None:
        self.refresh_sessions()
        self._render(self._controller.snapshot)

    # -- outlier flagging ----------------------------------------------------------------

    def _on_row_activated(self, _list_box: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        """Clicking a table row toggles the outlier exclusion of that point."""
        if self._updating:
            return
        measurement_id = getattr(row, "measurement_id", None)
        if measurement_id is None:
            return
        self._toggle_excluded(measurement_id, not bool(getattr(row, "excluded", False)))

    def _on_excluded_toggled(self, check: Gtk.CheckButton, measurement_id: int) -> None:
        if self._updating:
            return
        self._toggle_excluded(measurement_id, check.get_active())

    def _toggle_excluded(self, measurement_id: int, excluded: bool) -> None:
        try:
            self._controller.set_excluded(measurement_id, excluded)
        except RunError as exc:
            self._set_status(str(exc), error=True)
            return
        self._set_status(
            f"measurement {measurement_id} {'flagged as outlier' if excluded else 'included again'}",
            error=False,
        )
        # set_excluded() emitted a new snapshot -> _render refreshed table+plot

    # -- controller listener ---------------------------------------------------------------

    def _on_snapshot(self, snapshot: RunSnapshot) -> None:
        """Run-controller listener: render on the GLib main loop (the run goes
        on a worker thread, so marshal with ``GLib.idle_add``; on the main
        thread render directly)."""
        if threading.get_ident() == self._main_thread_id:
            self._render(snapshot)
        else:
            GLib.idle_add(self._render, snapshot)

    def _render(self, snapshot: RunSnapshot) -> bool:
        """Render a frozen snapshot: rebuild the table rows, replot, update
        the progress + status. Returns ``GLib.SOURCE_REMOVE`` (one-shot idle
        callback)."""
        self._updating = True
        try:
            while (row := self._list_box.get_row_at_index(0)) is not None:
                self._list_box.remove(row)
            for m in snapshot.lightcurve:
                self._list_box.append(self._make_row(m))
        finally:
            self._updating = False
        self._plot_points = tuple(
            (m.jd, m.mag, m.mag_err, m.excluded) for m in snapshot.lightcurve
        )
        self._plot.queue_draw()
        if snapshot.running and snapshot.progress is not None:
            p = snapshot.progress
            self._progress_bar.set_fraction((p.done / p.total) if p.total else 0.0)
            self._progress_label.set_label(f"measuring {p.done}/{p.total} — frame {p.frame_id}")
        elif snapshot.report is not None:
            r = snapshot.report
            self._progress_bar.set_fraction(1.0)
            self._progress_label.set_label(
                f"{len(r.measurements)} measured, {len(r.skipped)} skipped"
            )
        else:
            self._progress_bar.set_fraction(0.0)
            self._progress_label.set_label("")
        self._run_button.set_sensitive(not snapshot.running)
        if snapshot.error is not None:
            self._set_status(snapshot.error, error=True)
        elif snapshot.report is not None:
            r = snapshot.report
            self._set_status(
                f"session {r.session_id}: {len(r.measurements)} measured, "
                f"{len(r.skipped)} skipped — {len(snapshot.lightcurve)} point(s) in the lightcurve",
                error=False,
            )
        return GLib.SOURCE_REMOVE

    def _make_row(self, m: Any) -> Gtk.ListBoxRow:
        """One table row: jd/utc, mag, mag_err, the excluded checkbox."""
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_margin_top(4)
        box.set_margin_bottom(4)
        box.set_margin_start(8)
        box.set_margin_end(8)
        utc_time = m.utc[11:19] if len(m.utc) >= 19 else m.utc
        jd_label = Gtk.Label(label=f"{m.jd:.6f}  {utc_time}", xalign=0.0, hexpand=True)
        box.append(jd_label)
        box.append(Gtk.Label(label=f"{m.mag:.4f}", xalign=0.0))
        err_text = f"±{m.mag_err:.4f}" if m.mag_err is not None else "—"
        box.append(Gtk.Label(label=err_text, xalign=0.0))
        check = Gtk.CheckButton(label="excluded", active=m.excluded)
        check.set_valign(Gtk.Align.CENTER)
        check.connect("toggled", self._on_excluded_toggled, m.id)
        box.append(check)
        row = Gtk.ListBoxRow(child=box)
        row.measurement_id = m.id  # type: ignore[attr-defined]
        row.excluded = m.excluded  # type: ignore[attr-defined]
        row.excluded_check = check  # type: ignore[attr-defined]
        return row

    # -- the scatter plot (custom Cairo draw: axes + ticks + points + error bars) ------------

    def _draw_plot(self, area: Gtk.DrawingArea, cr: Any, width: int, height: int) -> None:
        """DrawingArea draw func: mag vs jd scatter — axes + ticks, one point
        per lightcurve point (error bars when ``mag_err`` is known), excluded
        points in red; the mag axis is inverted (brighter at the top)."""
        left, right = PLOT_MARGIN_LEFT, PLOT_MARGIN_RIGHT
        top, bottom = PLOT_MARGIN_TOP, PLOT_MARGIN_BOTTOM
        plot_w = max(width - left - right, 1)
        plot_h = max(height - top - bottom, 1)
        points = self._plot_points
        cr.set_font_size(9.0)
        if not points:
            cr.set_source_rgba(*AXIS_COLOR)
            cr.move_to(left, top + plot_h / 2.0)
            cr.show_text("no measurements yet — run a session")
            return
        jds = [p[0] for p in points]
        mags = [p[1] for p in points]
        jd_min, jd_max = min(jds), max(jds)
        mag_min, mag_max = min(mags), max(mags)
        if jd_max - jd_min < 1e-9:
            jd_min, jd_max = jd_min - 1e-6, jd_max + 1e-6
        if mag_max - mag_min < 1e-6:
            mag_min, mag_max = mag_min - 0.05, mag_max + 0.05
        else:
            pad = 0.05 * (mag_max - mag_min)
            mag_min, mag_max = mag_min - pad, mag_max + pad

        def px(jd: float) -> float:
            return left + (jd - jd_min) / (jd_max - jd_min) * plot_w

        def py(mag: float) -> float:
            # inverted mag axis: brighter (smaller mag) at the TOP
            return top + (mag_max - mag) / (mag_max - mag_min) * plot_h

        # axes
        cr.set_source_rgba(*AXIS_COLOR)
        cr.set_line_width(1.0)
        cr.move_to(left, top)
        cr.line_to(left, top + plot_h)
        cr.line_to(left + plot_w, top + plot_h)
        cr.stroke()
        # ticks + labels (5 per axis)
        for i in range(5):
            frac = i / 4.0
            jd = jd_min + frac * (jd_max - jd_min)
            x = px(jd)
            cr.move_to(x, top + plot_h)
            cr.line_to(x, top + plot_h + 4)
            cr.stroke()
            label = f"{jd:.5f}"
            extents = cr.text_extents(label)
            cr.move_to(x - extents.width / 2.0, top + plot_h + 14)
            cr.show_text(label)
            mag = mag_max - frac * (mag_max - mag_min)  # top = mag_min
            y = py(mag)
            cr.move_to(left - 4, y)
            cr.line_to(left, y)
            cr.stroke()
            label = f"{mag:.3f}"
            extents = cr.text_extents(label)
            cr.move_to(left - 6 - extents.width, y + extents.height / 2.0)
            cr.show_text(label)
        # error bars + points
        for jd, mag, mag_err, excluded in points:
            x, y = px(jd), py(mag)
            color = EXCLUDED_COLOR if excluded else POINT_COLOR
            if mag_err is not None:
                cr.set_source_rgba(color[0], color[1], color[2], 0.6)
                cr.set_line_width(1.0)
                cr.move_to(x, py(mag + mag_err))
                cr.line_to(x, py(mag - mag_err))
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
