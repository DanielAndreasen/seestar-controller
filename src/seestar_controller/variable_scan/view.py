"""The variable-candidate view widget — the WIDGET layer of variable-scan
ticket 02 (:mod:`seestar_controller.variable_scan.view`).

This module imports ``gi`` at module top (it is a widget module, the only
place allowed to): it is never imported by the pure layer
(:mod:`seestar_controller.variable_scan.controller` / ``render`` / ``export``)
or eagerly by the package ``__init__`` — the pure layer stays gi-free and
headless-testable (see docs/wiki/assumptions.md). It is re-exported as
``variable_scan.VariableScanView`` lazily (PEP 562 ``__getattr__``).

On the desktop the system packages are required (PyGObject is NOT a pip
dependency — it needs the system GTK headers to build)::

    sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1

:class:`VariableScanView` is a thin observer over the pure
:class:`~seestar_controller.variable_scan.controller.ScanController` (the app
owns one shared instance; the window passes it down via
:func:`seestar_controller.ui.views.build_view` to the Photometry view, which
embeds this widget below its results panel, next to the mover overlay):

- a **header row** — heading, a **Refresh** button, a **Run scan** button
  (runs :func:`~seestar_controller.variable_scan.scan.scan_project` over all
  subs of all sessions on a worker thread), a **threshold slider**
  (:class:`Gtk.Scale`, persisted via
  :func:`~seestar_controller.variable_scan.settings.set_scan_threshold` and
  re-filtering the list live), an **Export CSV** button (async
  :class:`Gtk.FileDialog` save dialog ->
  :func:`~seestar_controller.variable_scan.export.export_candidates_csv`)
  and an **Export GIF** button (the blink animation);
- the **ranked candidate list** (:class:`Gtk.ListBox`) — one row per
  candidate with the rank, the Gaia source id, the **scatter ratio**
  (``variability`` = MAD / median error) and the frame count as columns;
  activating a row selects the candidate on the controller;
- the selected candidate's **lightcurve** — the analysis
  :class:`~seestar_controller.analysis.lightcurve_view.LightcurveView`
  reused through a small duck-typed adapter over the scan controller (the
  scan points mapped to analysis lightcurve points);
- the selected candidate's **blink** — the star's cutout across all sub
  frames rendered by the pure :mod:`.render` layer (PIL), shown in a
  :class:`Gtk.Picture` (PNG bytes -> :class:`Gdk.Texture`) with Prev / Blink
  / Next frame controls (the blink stepping; the animated GIF of the same
  cutouts is produced by :func:`~.render.render_blink_gif` and exportable
  via the Export GIF button — ``Gdk.Texture`` itself cannot animate);
- a **status label** (candidate counts / clean error).

The view re-renders on every frozen
:class:`~seestar_controller.variable_scan.controller.ScanSnapshot`
(listener notifications marshalled onto the GLib main loop when they come
from a worker thread).
"""

from __future__ import annotations

import io
import threading
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Gdk, GLib, Gtk
from PIL import Image

from seestar_controller.analysis.lightcurve import (
    LightcurveError,
    LightcurveSnapshot,
)
from seestar_controller.analysis.lightcurve import (
    LightcurvePoint as AnalysisLightcurvePoint,
)
from seestar_controller.analysis.lightcurve_view import (
    LightcurveView as LightcurveWidget,
)

from .controller import ScanController, ScanSnapshot
from .errors import ScanError
from .export import CANDIDATES_CSV_FILE

#: The blink step interval, in milliseconds.
BLINK_INTERVAL_MS = 500

#: Threshold slider range (the variability ratio cutoff).
THRESHOLD_MIN = 0.5
THRESHOLD_MAX = 10.0
THRESHOLD_STEP = 0.1


def texture_from_pil(image: Image.Image) -> Gdk.Texture:
    """Build a :class:`Gdk.Texture` from a PIL image (PNG bytes ->
    ``Gdk.Texture.new_from_bytes`` — the same pattern as the mover overlay /
    Enhance preview)."""
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return Gdk.Texture.new_from_bytes(GLib.Bytes(buffer.getvalue()))


def _candidate_row_text(record: Any) -> str:
    """The one-line text of a candidate row (also used by the tests)."""
    source = "—" if record.source_id is None else str(record.source_id)
    return (
        f"#{record.rank} {source} ratio={record.variability:.2f} "
        f"frames={record.n_frames}"
    )


class _CandidateLightcurveAdapter:
    """The duck-typed lightcurve-controller adapter feeding the analysis
    :class:`~seestar_controller.analysis.lightcurve_view.LightcurveView`
    with the selected scan candidate's points.

    The analysis widget needs ``add_listener`` / ``remove_listener`` /
    ``snapshot`` (a :class:`~seestar_controller.analysis.lightcurve.LightcurveSnapshot`)
    / ``project`` / ``refresh`` / ``toggle_excluded`` — this adapter provides
    them over the scan controller: :meth:`update` is called by the view on
    every scan snapshot and re-emits to the widget's listeners. Scan
    candidates have no persisted outlier flag, so ``toggle_excluded`` raises
    :class:`~seestar_controller.analysis.lightcurve.LightcurveError` (the
    widget catches it and shows a status).
    """

    def __init__(self, scan_controller: ScanController) -> None:
        self._scan = scan_controller
        self._listeners: list[Any] = []
        self._points: tuple[AnalysisLightcurvePoint, ...] = ()
        self._error: str | None = None

    @property
    def project(self) -> Any:
        """The scan controller's project (drives the widget's placeholder)."""
        return self._scan.project

    def add_listener(self, listener: Any) -> Any:
        if listener not in self._listeners:
            self._listeners.append(listener)
        return listener

    def remove_listener(self, listener: Any) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    @property
    def snapshot(self) -> LightcurveSnapshot:
        sessions = ("scan",) if self._points else ()
        return LightcurveSnapshot(points=self._points, sessions=sessions, error=self._error)

    def refresh(self) -> ScanSnapshot:
        return self._scan.refresh()

    def toggle_excluded(self, measurement_id: int) -> bool:
        raise LightcurveError(
            "scan candidates have no persisted outlier flag — "
            f"cannot toggle point {measurement_id!r}"
        )

    def update(self, snapshot: ScanSnapshot) -> None:
        """Push a scan snapshot: map the selected candidate's points to
        analysis lightcurve points and notify the widget's listeners."""
        self._points = tuple(
            AnalysisLightcurvePoint(
                measurement_id=i,
                jd=point.jd,
                utc="",
                mag=point.mag,
                mag_err=point.mag_err,
                session_id="scan",
                excluded=False,
            )
            for i, point in enumerate(snapshot.points)
        )
        self._error = snapshot.error
        frozen = self.snapshot
        for listener in list(self._listeners):
            listener(frozen)


class VariableScanView(Gtk.Box):
    """The variable-candidate view: header (Refresh / Run scan / threshold
    slider / Export CSV / Export GIF), the ranked candidate list next to the
    selected candidate's lightcurve (the reused analysis LightcurveView) and
    blink (cutout picture + Prev/Blink/Next), and a status label — observing
    a :class:`~seestar_controller.variable_scan.controller.ScanController`.

    Constructed with the shared scan controller (the app passes it; a fresh
    standalone one is created when omitted, so the widget also works alone).
    """

    def __init__(
        self,
        scan_controller: ScanController | None = None,
        **kwargs,
    ) -> None:
        kwargs.setdefault("orientation", Gtk.Orientation.VERTICAL)
        kwargs.setdefault("spacing", 6)
        super().__init__(**kwargs)
        if scan_controller is None:
            scan_controller = ScanController()
        self._controller = scan_controller
        self._main_thread_id = threading.get_ident()
        self._row_texts: list[str] = []
        self._blink_source: int | None = None
        self._busy = False
        self._lightcurve_adapter = _CandidateLightcurveAdapter(scan_controller)

        # header row: heading + Refresh + Run scan + threshold slider + exports
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.append(Gtk.Label(label="Variable scan", xalign=0.0, css_classes=["heading"]))
        self._refresh_button = Gtk.Button(
            label="Refresh",
            tooltip_text="Re-read the latest persisted scan run from the store",
        )
        self._refresh_button.connect("clicked", self._on_refresh_clicked)
        header.append(self._refresh_button)
        self._run_button = Gtk.Button(
            label="Run scan",
            tooltip_text="Scan all sub-frames of all sessions for variable field "
            "stars (runs on a worker thread)",
        )
        self._run_button.connect("clicked", self._on_run_clicked)
        header.append(self._run_button)
        header.append(Gtk.Label(label="Threshold", xalign=0.0, css_classes=["dim-label"]))
        self._threshold_scale = Gtk.Scale(
            orientation=Gtk.Orientation.HORIZONTAL,
            adjustment=Gtk.Adjustment(
                value=3.0,
                lower=THRESHOLD_MIN,
                upper=THRESHOLD_MAX,
                step_increment=THRESHOLD_STEP,
                page_increment=0.5,
            ),
            tooltip_text="Variability-ratio threshold: candidates with "
            "MAD / median error above this are listed (persisted in the project)",
        )
        self._threshold_scale.set_size_request(140, -1)
        self._threshold_scale.set_digits(1)
        self._threshold_handler = self._threshold_scale.connect(
            "value-changed", self._on_threshold_changed
        )
        header.append(self._threshold_scale)
        self._threshold_label = Gtk.Label(label="", css_classes=["dim-label"])
        header.append(self._threshold_label)
        self._export_csv_button = Gtk.Button(
            label="Export CSV",
            tooltip_text="Export the ranked candidate list as CSV",
        )
        self._export_csv_button.connect("clicked", self._on_export_csv_clicked)
        header.append(self._export_csv_button)
        self._export_gif_button = Gtk.Button(
            label="Export GIF",
            tooltip_text="Export the selected candidate's blink as an animated GIF",
        )
        self._export_gif_button.connect("clicked", self._on_export_gif_clicked)
        header.append(self._export_gif_button)
        self.append(header)

        # body: candidate list | lightcurve + blink
        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        body.set_hexpand(True)
        body.set_vexpand(True)
        self._candidates_list = Gtk.ListBox()
        self._candidates_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._candidates_list.add_css_class("boxed-list")
        self._candidates_list.connect("row-activated", self._on_row_activated)
        list_frame = Gtk.Frame(child=self._candidates_list)
        list_frame.set_size_request(300, -1)
        body.append(list_frame)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        right.set_hexpand(True)
        right.set_vexpand(True)
        # duck-typed adapter (documented on _CandidateLightcurveAdapter):
        self._lightcurve_widget = LightcurveWidget(self._lightcurve_adapter)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
        lc_frame = Gtk.Frame(child=self._lightcurve_widget)
        lc_frame.set_vexpand(True)
        right.append(lc_frame)

        # blink: cutout picture | placeholder + frame controls
        blink_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._blink_stack = Gtk.Stack()
        self._blink_stack.set_hexpand(True)
        self._blink_picture = Gtk.Picture()
        self._blink_picture.set_can_shrink(True)
        self._blink_picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self._blink_stack.add_titled(self._blink_picture, "image", "Cutout")
        self._blink_placeholder = Gtk.Label(
            label="No candidate selected — run a scan, then activate a candidate",
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
            wrap=True,
            css_classes=["dim-label"],
        )
        self._blink_stack.add_titled(self._blink_placeholder, "empty", "Empty")
        self._blink_stack.set_visible_child_name("empty")
        blink_box.append(self._blink_stack)
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._prev_button = Gtk.Button(label="\u25c0 Prev", tooltip_text="Previous frame")
        self._prev_button.connect("clicked", lambda *_a: self._controller.prev_frame())
        controls.append(self._prev_button)
        self._blink_button = Gtk.ToggleButton(
            label="Blink", tooltip_text="Step through the frames automatically"
        )
        self._blink_button.connect("toggled", self._on_blink_toggled)
        controls.append(self._blink_button)
        self._next_button = Gtk.Button(label="Next \u25b6", tooltip_text="Next frame")
        self._next_button.connect("clicked", lambda *_a: self._controller.next_frame())
        controls.append(self._next_button)
        self._frame_label = Gtk.Label(label="", xalign=0.0, css_classes=["dim-label"])
        self._frame_label.set_hexpand(True)
        controls.append(self._frame_label)
        blink_box.append(controls)
        right.append(blink_box)
        body.append(right)
        self.append(body)

        # status label (counts / clean error)
        self._status_label = Gtk.Label(label="", wrap=True, halign=Gtk.Align.CENTER)
        self.append(self._status_label)

        self.connect("destroy", self._on_destroy)
        self._controller.add_listener(self._on_snapshot)
        self._render(self._controller.snapshot)

    # -- public helpers (also used by the widget tests) ------------------------------

    @property
    def controller(self) -> ScanController:
        """The pure-layer scan controller backing this view."""
        return self._controller

    @property
    def scan_controller(self) -> ScanController:
        """Alias of :attr:`controller`."""
        return self._controller

    @property
    def candidates_list(self) -> Gtk.ListBox:
        """The ranked candidate list (one row per candidate)."""
        return self._candidates_list

    @property
    def run_button(self) -> Gtk.Button:
        """The Run scan button."""
        return self._run_button

    @property
    def refresh_button(self) -> Gtk.Button:
        """The Refresh button."""
        return self._refresh_button

    @property
    def threshold_scale(self) -> Gtk.Scale:
        """The threshold slider."""
        return self._threshold_scale

    @property
    def threshold_label(self) -> Gtk.Label:
        """The label showing the current threshold value."""
        return self._threshold_label

    @property
    def export_csv_button(self) -> Gtk.Button:
        """The Export CSV button."""
        return self._export_csv_button

    @property
    def export_gif_button(self) -> Gtk.Button:
        """The Export GIF button."""
        return self._export_gif_button

    @property
    def lightcurve_widget(self) -> LightcurveWidget:
        """The reused analysis lightcurve widget of the selected candidate."""
        return self._lightcurve_widget

    @property
    def blink_picture(self) -> Gtk.Picture:
        """The blink cutout picture."""
        return self._blink_picture

    @property
    def blink_stack(self) -> Gtk.Stack:
        """The blink picture/placeholder stack."""
        return self._blink_stack

    @property
    def prev_button(self) -> Gtk.Button:
        return self._prev_button

    @property
    def next_button(self) -> Gtk.Button:
        return self._next_button

    @property
    def blink_button(self) -> Gtk.ToggleButton:
        return self._blink_button

    @property
    def frame_label(self) -> Gtk.Label:
        return self._frame_label

    @property
    def status_label(self) -> Gtk.Label:
        """The status label (counts / clean error)."""
        return self._status_label

    def candidate_row_texts(self) -> list[str]:
        """The one-line texts of the candidate rows, in list order."""
        return list(self._row_texts)

    def selected_candidate_index(self) -> int | None:
        """The index of the selected candidate row (None when none)."""
        row = self._candidates_list.get_selected_row()
        if row is None:
            return None
        return getattr(row, "candidate_index", None)

    # -- events ------------------------------------------------------------------------

    def _on_refresh_clicked(self, *_args) -> None:
        """Refresh button: re-read the store (the controller notifies -> render)."""
        self._controller.refresh()

    def _on_run_clicked(self, *_args) -> None:
        """Run scan button: run the scan on a worker thread (the controller
        notifies -> render; the store calls stay on the db thread)."""
        self._run_async(lambda: self._controller.run_scan())

    def _on_threshold_changed(self, scale: Gtk.Scale) -> None:
        """Threshold slider (``value-changed`` — fires for user drags and
        programmatic ``set_value`` alike): persist + re-filter via the
        controller (the controller notifies -> render). Invalid values are
        rejected by the controller and shown as the status."""
        rounded = round(float(scale.get_value()), 1)
        self._threshold_label.set_label(f"{rounded:.1f}")
        if rounded == self._controller.threshold:
            return
        try:
            self._controller.set_threshold(rounded)
        except ScanError as exc:
            self._set_status(f"invalid threshold: {exc}", error=True)

    def _on_row_activated(self, listbox: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        """Candidate row activated -> select that candidate on the controller
        (which notifies -> render)."""
        index = getattr(row, "candidate_index", None)
        if index is None:
            return
        listbox.select_row(row)
        try:
            self._controller.select_candidate(index)
        except ScanError as exc:
            self._set_status(str(exc), error=True)

    def _on_export_csv_clicked(self, *_args) -> None:
        """Open a :class:`Gtk.FileDialog` save dialog for the candidates CSV;
        the async callback marshals the chosen path back onto the main loop
        via the controller's ``invoke_on_main``."""
        dialog = Gtk.FileDialog(title="Export candidates CSV", accept_label="Export")
        dialog.set_initial_name(CANDIDATES_CSV_FILE)
        dialog.save(
            self.get_root(),
            None,
            lambda dlg, result: self._on_export_dialog_finished(dlg, result, "csv"),
        )

    def _on_export_gif_clicked(self, *_args) -> None:
        """Same as :meth:`_on_export_csv_clicked` for the blink GIF."""
        dialog = Gtk.FileDialog(title="Export blink GIF", accept_label="Export")
        dialog.set_initial_name("blink.gif")
        dialog.save(
            self.get_root(),
            None,
            lambda dlg, result: self._on_export_dialog_finished(dlg, result, "gif"),
        )

    def _on_export_dialog_finished(self, dialog: Gtk.FileDialog, result: Any, kind: str) -> None:
        try:
            file = dialog.save_finish(result)
        except GLib.Error:
            return  # cancelled
        if file is None:
            return
        path = file.get_path()
        if not path:
            return
        # the dialog callback is NOT on the main loop's controller context —
        # marshal the export through invoke_on_main (GLib.idle_add in the app)
        self._controller.invoke_on_main(lambda: self._do_export(path, kind))

    def _do_export(self, path: str, kind: str) -> None:
        """Run the export on the main loop; show the result in the status."""
        try:
            if kind == "csv":
                written = self._controller.export_csv(path)
                n = len(self._controller.snapshot.candidates)
                self._set_status(f"wrote {n} candidate(s) to {written}", error=False)
            else:
                gif = self._controller.snapshot.blink_gif
                if gif is None:
                    self._set_status(
                        "no blink GIF — run a scan and select a candidate first",
                        error=True,
                    )
                    return
                Path(path).write_bytes(gif)
                self._set_status(f"wrote blink GIF to {path}", error=False)
        except (ScanError, TypeError, OSError) as exc:
            self._set_status(f"export failed: {exc}", error=True)

    def _on_blink_toggled(self, button: Gtk.ToggleButton) -> None:
        """Blink toggle: start/stop the periodic frame stepping."""
        if button.get_active():
            self._blink_source = GLib.timeout_add(BLINK_INTERVAL_MS, self._blink_tick)
        else:
            self._stop_blink()

    def _blink_tick(self) -> bool:
        """One blink step: advance to the next frame (wraps). Returns
        ``GLib.SOURCE_CONTINUE`` while blinking."""
        self._controller.next_frame()
        return GLib.SOURCE_CONTINUE

    def _stop_blink(self) -> None:
        if self._blink_source is not None:
            GLib.source_remove(self._blink_source)
            self._blink_source = None

    def _on_destroy(self, *_args) -> None:
        self._stop_blink()
        self._controller.remove_listener(self._on_snapshot)

    def _run_async(self, fn: Any) -> None:
        """Run ``fn`` on a daemon worker thread; the busy state is cleared on
        the main loop when it finishes."""
        if self._busy:
            return
        self._busy = True
        self._set_buttons_sensitive(False)

        def work() -> None:
            try:
                fn()
            finally:
                GLib.idle_add(self._on_work_done)

        threading.Thread(target=work, daemon=True).start()

    def _on_work_done(self) -> bool:
        self._busy = False
        self._set_buttons_sensitive(True)
        return GLib.SOURCE_REMOVE

    def _set_buttons_sensitive(self, sensitive: bool) -> None:
        for button in (self._run_button, self._refresh_button):
            button.set_sensitive(sensitive)

    # -- controller listener ---------------------------------------------------------------

    def _on_snapshot(self, snapshot: ScanSnapshot) -> None:
        """Controller listener: render on the GLib main loop (notifications
        may come from a worker thread, so marshal with ``GLib.idle_add``; on
        the main thread render directly)."""
        if threading.get_ident() == self._main_thread_id:
            self._render(snapshot)
        else:
            GLib.idle_add(self._render, snapshot)

    def _render(self, snapshot: ScanSnapshot) -> bool:
        """Render a frozen snapshot: the threshold slider, the candidate rows,
        the lightcurve (via the adapter), the blink picture + frame label, and
        the status. Returns ``GLib.SOURCE_REMOVE`` (one-shot idle callback)."""
        # threshold slider (block the handler so set_value does not re-apply)
        self._threshold_scale.handler_block(self._threshold_handler)
        try:
            self._threshold_scale.set_value(snapshot.threshold if snapshot.threshold else 3.0)
        finally:
            self._threshold_scale.handler_unblock(self._threshold_handler)
        self._threshold_label.set_label(f"{snapshot.threshold:.1f}" if snapshot.threshold else "")

        # candidate rows (rebuilt when the list changed)
        texts = [_candidate_row_text(r) for r in snapshot.candidates]
        if texts != self._row_texts:
            self._row_texts = texts
            while (row := self._candidates_list.get_row_at_index(0)) is not None:
                self._candidates_list.remove(row)
            for index, record in enumerate(snapshot.candidates):
                row = Gtk.ListBoxRow()
                row.candidate_index = index  # type: ignore[attr-defined]
                row.set_tooltip_text(
                    f"ra {record.ra_deg:.5f} dec {record.dec_deg:.5f} — "
                    f"MAD {record.mad:.4f}, median err {record.median_err:.4f}"
                )
                box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                rank_label = Gtk.Label(label=f"#{record.rank}", xalign=0.0)
                rank_label.set_width_chars(4)
                box.append(rank_label)
                source_label = Gtk.Label(
                    label="—" if record.source_id is None else str(record.source_id),
                    xalign=0.0,
                )
                source_label.set_width_chars(10)
                box.append(source_label)
                ratio_label = Gtk.Label(label=f"{record.variability:.2f}", xalign=0.0)
                ratio_label.set_width_chars(7)
                ratio_label.set_tooltip_text("scatter ratio = MAD / median error")
                box.append(ratio_label)
                frames_label = Gtk.Label(label=str(record.n_frames), xalign=0.0)
                frames_label.set_width_chars(6)
                box.append(frames_label)
                row.set_child(box)
                self._candidates_list.append(row)
            if snapshot.selected_index is not None:
                row = self._candidates_list.get_row_at_index(snapshot.selected_index)
                if row is not None:
                    self._candidates_list.select_row(row)

        # lightcurve of the selected candidate (via the adapter)
        self._lightcurve_adapter.update(snapshot)

        # blink picture
        if snapshot.blink_images and snapshot.frame_index is not None:
            index = min(snapshot.frame_index, len(snapshot.blink_images) - 1)
            self._blink_picture.set_paintable(texture_from_pil(snapshot.blink_images[index]))
            self._blink_stack.set_visible_child_name("image")
        else:
            self._blink_stack.set_visible_child_name("empty")

        # frame label
        n = len(snapshot.frames)
        if snapshot.frame_index is not None and n:
            frame = snapshot.frames[snapshot.frame_index]
            self._frame_label.set_label(
                f"frame {snapshot.frame_index + 1}/{n} (id {frame.id}, {frame.utc})"
            )
        else:
            self._frame_label.set_label("")

        # status
        if snapshot.error is not None:
            self._set_status(snapshot.error, error=True)
        else:
            self._set_status(snapshot.status or "", error=False)
        return GLib.SOURCE_REMOVE

    def _set_status(self, text: str, *, error: bool) -> None:
        self._status_label.set_label(text)
        for css_class in ("error", "dim-label"):
            self._status_label.remove_css_class(css_class)
        self._status_label.add_css_class("error" if error else "dim-label")


__all__ = ["BLINK_INTERVAL_MS", "VariableScanView", "texture_from_pil"]
