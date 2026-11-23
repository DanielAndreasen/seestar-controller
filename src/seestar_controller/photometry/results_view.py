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
  chooser is re-listed on every render when the controller's sessions differ
  from the listed ids — the window is built before any project is opened, so
  the chooser only fills once a project is set, without a manual Refresh. The
  pure method is synchronous and GTK-free, so the panel runs it on a daemon
  **worker thread** and marshals the progress callback (and the listener
  notifications) back to the GLib main loop with :func:`GLib.idle_add` — the
  main loop NEVER blocks on the FITS reads / photutils measuring;
- issue 10: a **Solve unsolved subs** button next to Run + a **WCS coverage
  label** in the same row — clicking Solve batch plate-solves every sub-frame
  of the chosen session that has no stored ``frame_wcs`` row yet, via
  :meth:`~seestar_controller.photometry.run.RunController.solve_session_unsolved`
  (the pure
  :func:`seestar_controller.astrometry.session.solve_session_unsolved`, with
  the store reads/writes marshalled to the db-owning thread through the
  controller's ``_call_store`` while the slow solves stay on the worker). It
  runs on a daemon worker thread like the run, with progress
  (``solving done/total — frame N``), per-frame solve failures recorded and
  skipped (never kill the batch), and afterwards the label shows the WCS
  coverage (``"WCS: 508 of 512 subs solved"``) so a re-Run can reproject the
  sky picks onto every frame (issue 09);
- a **progress bar + label** while running (``done/total — frame N``), and
  after a run the summary ``N measured, N skipped, N excluded`` plus a
  dedicated **exclusion label** (frame-quality ticket 03: how many sub-frames
  were excluded from the run and why — e.g. ``2 frames excluded (fwhm×2)`` —
  from the report's :class:`~seestar_controller.photometry.run.ExcludedFrame`\\ s,
  by default the session's flagged frames) and a **skip label** (issue 09:
  the aggregated skip reasons of the run — e.g. ``479 frames skipped
  (non-positive-flux×479)`` — plus the WCS-reprojection coverage, e.g.
  ``8 of 10 frames via per-frame WCS reprojection (2 fixed-pixel)``, from the
  report's ``skip_summary`` / ``wcs_summary``);
- a **per-frame table** (:class:`Gtk.ListBox`, one row per lightcurve point:
  jd/utc, mag, mag_err, an "excluded" :class:`Gtk.CheckButton`) — clicking a
  row (or the checkbox) toggles the outlier flag via
  :meth:`~seestar_controller.photometry.run.RunController.set_excluded`, which
  refreshes BOTH the table and the plot;
- a **scatter plot** of mag vs jd on a :class:`Gtk.DrawingArea` (GTK4 has no
  chart widget, so the draw func paints axes + ticks + points + error bars
  with Cairo directly; the mag axis is inverted — brighter at the top — and
  excluded points are drawn in red). When the last run had a **check star**
  picked (check-star-plot tickets 01 + 02), the plot carries the check star's
  differential curve as a **second series** (amber, next to the target's
  blue) plus a **target − check** toggle (:class:`Gtk.CheckButton`) that adds
  the target-minus-check curve (purple) — the check curves come from the last
  :class:`~seestar_controller.photometry.run.RunReport`'s ``frame_results``
  (the store schema keeps only the target's mag per measurement, so the check
  series covers the frames of the last run), aligned to the lightcurve points
  by frame id, with a legend naming each series.

The panel re-renders on every frozen
:class:`~seestar_controller.photometry.run.RunSnapshot`.
"""

from __future__ import annotations

import threading
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import GLib, Gtk

from seestar_controller.astrometry import SessionSolveReport
from seestar_controller.store.frames import Frame

from .run import RunController, RunError, RunSnapshot

#: Plot margins (pixels): left (jd axis labels), right, top, bottom (jd ticks).
PLOT_MARGIN_LEFT = 64
PLOT_MARGIN_RIGHT = 12
PLOT_MARGIN_TOP = 12
PLOT_MARGIN_BOTTOM = 30

#: Plot colors (RGBA): axes/text gray, target points blue, the check-star
#: series amber, the target-minus-check series purple, excluded points red
#: (check-star-plot ticket 02).
AXIS_COLOR = (0.55, 0.55, 0.58, 1.0)
POINT_COLOR = (0.35, 0.60, 0.95, 1.0)
CHECK_COLOR = (0.98, 0.72, 0.20, 1.0)
TMC_COLOR = (0.75, 0.45, 0.95, 1.0)
EXCLUDED_COLOR = (0.90, 0.35, 0.30, 1.0)

#: One plotted point: ``(jd, mag, mag_err, excluded)``.
PlotPoint = tuple[float, float, float | None, bool]


class ResultsPanel(Gtk.Box):
    """The photometry results panel: session chooser + Run button + progress
    + per-frame table + mag-vs-jd scatter plot (target series, plus the
    check-star series and the target-minus-check toggle when the last run had
    a check star — check-star-plot ticket 02), observing a
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
        self._solve_thread: threading.Thread | None = None  # issue 10
        self._session_ids: list[str] = []
        self._updating = False  # guard against programmatic signal loops
        self._plot_points: tuple[PlotPoint, ...] = ()
        self._check_points: tuple[PlotPoint, ...] = ()
        self._tmc_points: tuple[PlotPoint, ...] = ()
        self._show_tmc = False  # the target-minus-check toggle state

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
                "progress shown below) and append the results to the lightcurve; "
                "flagged frames (frame quality) are excluded by default"
            ),
        )
        self._run_button.connect("clicked", self._on_run_clicked)
        run_row.append(self._run_button)
        # issue 10: batch plate-solve the session's unsolved subs (no frame_wcs
        # row yet) so a re-Run can reproject the sky picks onto every frame
        self._solve_button = Gtk.Button(
            label="Solve unsolved subs",
            tooltip_text=(
                "Plate-solve every sub-frame of the chosen session that has no "
                "stored WCS yet (worker thread, progress shown below; a frame "
                "that cannot be solved is skipped) — enables per-frame WCS "
                "reprojection on the next Run"
            ),
        )
        self._solve_button.connect("clicked", self._on_solve_clicked)
        run_row.append(self._solve_button)
        # issue 10: the session's WCS coverage near the Run button
        # ("WCS: 4 of 512 subs solved")
        self._wcs_label = Gtk.Label(
            label="", xalign=0.0, css_classes=["dim-label", "caption"]
        )
        run_row.append(self._wcs_label)
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

        # frame-quality ticket 03: how many frames were excluded from the last
        # run and why (e.g. "2 frames excluded (fwhm×2)")
        self._exclusion_label = Gtk.Label(
            label="", xalign=0.0, css_classes=["dim-label", "caption"]
        )
        self.append(self._exclusion_label)

        # issue 09: the aggregated skip reasons of the last run (e.g. "479
        # frames skipped (non-positive-flux×479)") + the WCS-reprojection
        # coverage of the measured frames (e.g. "8 of 10 frames via per-frame
        # WCS reprojection (2 fixed-pixel)")
        self._skip_label = Gtk.Label(
            label="", xalign=0.0, css_classes=["dim-label", "caption"]
        )
        self.append(self._skip_label)

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
            min_content_width=260,
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
        plot_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        plot_header.append(Gtk.Label(label="mag vs jd (brighter at the top)", xalign=0.0,
                                     hexpand=True, css_classes=["caption", "dim-label"]))
        # the target-minus-check toggle (check-star-plot ticket 02): adds the
        # target − check curve as a third series when a check star was picked
        self._tmc_toggle = Gtk.CheckButton(
            label="target − check",
            tooltip_text=(
                "Also plot the target-minus-check curve (needs a check star in "
                "the last run)"
            ),
        )
        self._tmc_toggle.set_valign(Gtk.Align.CENTER)
        self._tmc_toggle.connect("toggled", self._on_tmc_toggled)
        plot_header.append(self._tmc_toggle)
        box.append(plot_header)
        self._plot = Gtk.DrawingArea()
        self._plot.set_hexpand(True)
        self._plot.set_vexpand(True)
        # content size = the plot's minimum size; kept small so the panel
        # (and the window) keeps a small minimum width on laptop screens
        self._plot.set_content_width(260)
        self._plot.set_content_height(160)
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
    def solve_button(self) -> Gtk.Button:
        """The Solve unsolved subs button (issue 10: starts the worker-thread
        batch plate-solve of the session's unsolved subs)."""
        return self._solve_button

    @property
    def wcs_label(self) -> Gtk.Label:
        """The WCS coverage label (issue 10): ``"WCS: 4 of 512 subs solved"``
        for the chosen session; empty when no session is selected."""
        return self._wcs_label

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
    def exclusion_label(self) -> Gtk.Label:
        """The exclusion label (frame-quality ticket 03): after a run, how many
        sub-frames were excluded and why (``"2 frames excluded (fwhm×2)"``);
        empty before the first run."""
        return self._exclusion_label

    @property
    def skip_label(self) -> Gtk.Label:
        """The skip label (issue 09): after a run, the aggregated skip reasons
        plus the WCS-reprojection coverage (``"2 frames skipped
        (non-positive-flux×2) — 8 of 10 frames via per-frame WCS reprojection
        (2 fixed-pixel)"``); empty before the first run."""
        return self._skip_label

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
        """The target series currently plotted: ``(jd, mag, mag_err, excluded)``."""
        return self._plot_points

    def check_plot_points(self) -> tuple[PlotPoint, ...]:
        """The check-star series currently plotted (empty when the last run had
        no check star): ``(jd, check_mag, check_mag_err, excluded)``."""
        return self._check_points

    def tmc_plot_points(self) -> tuple[PlotPoint, ...]:
        """The target-minus-check series currently plotted (empty when the last
        run had no check star): ``(jd, target_minus_check, err, excluded)``."""
        return self._tmc_points

    @property
    def tmc_toggle(self) -> Gtk.CheckButton:
        """The target-minus-check toggle checkbox."""
        return self._tmc_toggle

    def show_target_minus_check(self) -> bool:
        """Whether the target-minus-check curve is currently shown."""
        return self._show_tmc

    def set_show_target_minus_check(self, show: bool) -> None:
        """Set the target-minus-check toggle (same as clicking it)."""
        self._tmc_toggle.set_active(bool(show))

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
            # no project (anymore): clear the chooser so no stale ids remain
            self._session_ids = []
            self._session_dropdown.set_model(Gtk.StringList.new([]))
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

    def solve_unsolved_subs(self) -> threading.Thread | None:
        """Start the batch solve of the selected session's unsolved subs on a
        daemon worker thread (same as clicking Solve unsolved subs); returns
        the thread, so callers (tests) can ``join()`` it. Re-entrant: a solve
        (or run) already in progress is not restarted."""
        self._on_solve_clicked()
        return self._solve_thread

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
            except Exception as exc:  # surfaced as a clean status, never a crash  # noqa: BLE001
                GLib.idle_add(self._on_run_failed, f"{type(exc).__name__}: {exc}")

        self._run_thread = threading.Thread(
            target=work, name="photometry-run", daemon=True
        )
        self._run_thread.start()

    def _on_progress(self, done: int, total: int, frame: Frame) -> None:
        """The run's progress callback — called on the WORKER thread, so
        marshal the UI update onto the GLib main loop."""
        GLib.idle_add(self._render_progress, done, total, frame)

    def _render_progress(self, done: int, total: int, frame: Frame) -> bool:
        fraction = (done / total) if total else 0.0
        self._progress_bar.set_fraction(fraction)
        self._progress_label.set_label(f"measuring {done}/{total} — frame {frame.id}")
        return GLib.SOURCE_REMOVE

    def _on_run_failed(self, message: str) -> bool:
        self._set_status(message, error=True)
        self._run_button.set_sensitive(True)
        return GLib.SOURCE_REMOVE

    # -- batch solve of the session's unsolved subs (issue 10) -------------------------------

    def _on_solve_clicked(self, *_args) -> None:
        if self._run_thread is not None and self._run_thread.is_alive():
            return  # a run is in progress — no concurrent solve
        if self._solve_thread is not None and self._solve_thread.is_alive():
            return
        session_id = self.selected_session_id()
        if session_id is None:
            self._set_status("no session to solve — store some sub-frames first", error=True)
            return
        self._set_status(f"solving unsolved subs of session {session_id}…", error=False)
        self._run_button.set_sensitive(False)
        self._solve_button.set_sensitive(False)

        def work() -> None:
            try:
                # synchronous + GTK-free: safe on this worker thread; the store
                # reads/writes are marshalled to the db-owning thread by the
                # controller (_call_store), the solves stay on this thread
                report = self._controller.solve_session_unsolved(
                    session_id, on_progress=self._on_solve_progress
                )
            except Exception as exc:  # surfaced as a clean status, never a crash  # noqa: BLE001
                GLib.idle_add(self._on_solve_failed, f"{type(exc).__name__}: {exc}")
            else:
                GLib.idle_add(self._on_solve_done, session_id, report)

        self._solve_thread = threading.Thread(
            target=work, name="photometry-solve", daemon=True
        )
        self._solve_thread.start()

    def _on_solve_progress(self, done: int, total: int, frame: Frame) -> None:
        """The batch solve's progress callback — called on the WORKER thread,
        so marshal the UI update onto the GLib main loop."""
        GLib.idle_add(self._render_solve_progress, done, total, frame)

    def _render_solve_progress(self, done: int, total: int, frame: Frame) -> bool:
        fraction = (done / total) if total else 0.0
        self._progress_bar.set_fraction(fraction)
        self._progress_label.set_label(f"solving {done}/{total} — frame {frame.id}")
        return GLib.SOURCE_REMOVE

    def _on_solve_done(self, session_id: str, report: SessionSolveReport) -> bool:
        """The batch solve finished (on the GLib main loop): show the report
        summary, refresh the WCS coverage label, re-enable the buttons."""
        self._progress_bar.set_fraction(1.0 if report.attempted else 0.0)
        self._progress_label.set_label(report.summary)
        self._set_status(f"session {session_id}: {report.summary}", error=False)
        self._refresh_wcs_label()
        self._run_button.set_sensitive(True)
        self._solve_button.set_sensitive(True)
        return GLib.SOURCE_REMOVE

    def _on_solve_failed(self, message: str) -> bool:
        self._set_status(message, error=True)
        self._run_button.set_sensitive(True)
        self._solve_button.set_sensitive(True)
        return GLib.SOURCE_REMOVE

    def _refresh_wcs_label(self) -> None:
        """Re-read the WCS coverage of the chosen session and update the label
        (issue 10: ``"WCS: 508 of 512 subs solved"``); empty when no session is
        selected or the coverage cannot be read."""
        session_id = self.selected_session_id()
        if session_id is None:
            self._wcs_label.set_label("")
            return
        try:
            coverage = self._controller.wcs_coverage(session_id)
        except RunError:
            self._wcs_label.set_label("")
            return
        self._wcs_label.set_label(f"WCS: {coverage.summary}")


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
        """Render a frozen snapshot: re-list the session chooser when the
        controller's sessions differ from the listed ids (a project opened
        after this view was built — the same pattern as MoverOverlayView /
        QualityView), rebuild the table rows, replot, update the progress +
        status. Returns ``GLib.SOURCE_REMOVE`` (one-shot idle callback)."""
        try:
            current_sessions = [s.id for s in self._controller.sessions()]
        except RunError:
            current_sessions = []  # no project (anymore)
        if current_sessions != self._session_ids:
            self.refresh_sessions()
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
        self._check_points, self._tmc_points = self._check_series(snapshot)
        self._plot.queue_draw()
        if snapshot.running and snapshot.progress is not None:
            p = snapshot.progress
            self._progress_bar.set_fraction((p.done / p.total) if p.total else 0.0)
            self._progress_label.set_label(f"measuring {p.done}/{p.total} — frame {p.frame_id}")
        elif snapshot.report is not None:
            r = snapshot.report
            self._progress_bar.set_fraction(1.0)
            self._progress_label.set_label(
                f"{len(r.measurements)} measured, {len(r.skipped)} skipped, "
                f"{r.excluded_count} excluded"
            )
        else:
            self._progress_bar.set_fraction(0.0)
            self._progress_label.set_label("")
        # frame-quality ticket 03: the exclusion summary of the last run
        self._exclusion_label.set_label(
            snapshot.report.exclusion_summary if snapshot.report is not None else ""
        )
        # issue 09: the aggregated skip reasons + WCS-reprojection coverage of
        # the last run (e.g. "479 frames skipped (non-positive-flux×479) — 33
        # of 512 frames via per-frame WCS reprojection (479 fixed-pixel)")
        if snapshot.report is not None:
            r = snapshot.report
            skip_text = r.skip_summary
            if r.measurements:
                skip_text = f"{skip_text} — {r.wcs_summary}"
            self._skip_label.set_label(skip_text)
        else:
            self._skip_label.set_label("")
        self._run_button.set_sensitive(not snapshot.running)
        # issue 10: the solve button is disabled while a run or a solve batch
        # is in progress
        solve_running = self._solve_thread is not None and self._solve_thread.is_alive()
        self._solve_button.set_sensitive(not snapshot.running and not solve_running)
        # issue 10: the WCS coverage of the chosen session near the Run button
        self._refresh_wcs_label()
        if snapshot.error is not None:
            self._set_status(snapshot.error, error=True)
        elif snapshot.report is not None:
            r = snapshot.report
            self._set_status(
                f"session {r.session_id}: {len(r.measurements)} measured, "
                f"{len(r.skipped)} skipped, {r.excluded_count} excluded — "
                f"{len(snapshot.lightcurve)} point(s) in the lightcurve",
                error=False,
            )
        return GLib.SOURCE_REMOVE

    def _check_series(self, snapshot: RunSnapshot) -> tuple[tuple[PlotPoint, ...], tuple[PlotPoint, ...]]:
        """The check-star and target-minus-check series of the last run:
        ``(check_points, tmc_points)``, each ``(jd, mag, mag_err, excluded)``.

        The check curves live in the last :class:`RunReport`'s
        ``frame_results`` (the store keeps only the target's mag per
        measurement), zipped with the report's ``measurements`` (same order)
        and aligned to the lightcurve's excluded flags by frame id. Empty when
        no report yet or the run had no check star.
        """
        report = snapshot.report
        if report is None:
            return (), ()
        excluded_by_frame = {m.frame_id: m.excluded for m in snapshot.lightcurve}
        check_points: list[PlotPoint] = []
        tmc_points: list[PlotPoint] = []
        for measurement, fm in zip(report.measurements, report.frame_results):
            excluded = excluded_by_frame.get(measurement.frame_id, measurement.excluded)
            if fm.check_mag is not None and fm.check_mag_err is not None:
                check_points.append((fm.jd, fm.check_mag, fm.check_mag_err, excluded))
            if fm.target_minus_check is not None and fm.target_minus_check_err is not None:
                tmc_points.append(
                    (fm.jd, fm.target_minus_check, fm.target_minus_check_err, excluded)
                )
        return tuple(check_points), tuple(tmc_points)

    def _on_tmc_toggled(self, toggle: Gtk.CheckButton) -> None:
        """The target-minus-check toggle: show/hide the third series."""
        self._show_tmc = toggle.get_active()
        self._plot.queue_draw()

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
        """DrawingArea draw func: mag vs jd scatter — axes + ticks + legend,
        one point per lightcurve point (error bars when ``mag_err`` is known),
        plus the check-star series (amber) and, when the toggle is on, the
        target-minus-check series (purple) — excluded points in red; the mag
        axis is inverted (brighter at the top)."""
        left, right = PLOT_MARGIN_LEFT, PLOT_MARGIN_RIGHT
        top, bottom = PLOT_MARGIN_TOP, PLOT_MARGIN_BOTTOM
        plot_w = max(width - left - right, 1)
        plot_h = max(height - top - bottom, 1)
        # the visible series: (legend label, color, points)
        series: list[tuple[str, tuple[float, float, float, float], tuple[PlotPoint, ...]]] = [
            ("target", POINT_COLOR, self._plot_points)
        ]
        if self._check_points:
            series.append(("check", CHECK_COLOR, self._check_points))
        if self._show_tmc and self._tmc_points:
            series.append(("target−check", TMC_COLOR, self._tmc_points))
        cr.set_font_size(9.0)
        if not self._plot_points:
            cr.set_source_rgba(*AXIS_COLOR)
            cr.move_to(left, top + plot_h / 2.0)
            cr.show_text("no measurements yet — run a session")
            return
        all_points = [p for _label, _color, points in series for p in points]
        jds = [p[0] for p in all_points]
        mags = [p[1] for p in all_points]
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
        # legend (top-right inside the plot): one color chip + name per series
        legend_x = left + plot_w - 92
        for i, (name, color, _points) in enumerate(series):
            ly = top + 8 + i * 14
            cr.set_source_rgba(*color)
            cr.rectangle(legend_x, ly, 8, 8)
            cr.fill()
            cr.set_source_rgba(*AXIS_COLOR)
            cr.move_to(legend_x + 12, ly + 8)
            cr.show_text(name)
        # error bars + points per series (excluded points in red)
        for _name, color, points in series:
            for jd, mag, mag_err, excluded in points:
                x, y = px(jd), py(mag)
                point_color = EXCLUDED_COLOR if excluded else color
                if mag_err is not None:
                    cr.set_source_rgba(point_color[0], point_color[1], point_color[2], 0.6)
                    cr.set_line_width(1.0)
                    cr.move_to(x, py(mag + mag_err))
                    cr.line_to(x, py(mag - mag_err))
                    cr.stroke()
                cr.set_source_rgba(*point_color)
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
