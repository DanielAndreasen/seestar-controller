"""The astrometry view widgets — the WIDGET layer of astrometry ticket 03
(:mod:`seestar_controller.astrometry.view`).

This module imports ``gi`` at module top (it is a widget module, the only
place allowed to): it is never imported by the pure layer
(:mod:`seestar_controller.astrometry`) or eagerly by the package ``__init__``
— the pure layer stays gi-free and headless-testable (see
docs/wiki/assumptions.md: the sandbox has no GTK). The widgets are re-exported
as ``astrometry.SolvePanel`` / ``astrometry.DriftPlotView`` lazily (PEP 562
``__getattr__``).

On the desktop the system packages are required (PyGObject is NOT a pip
dependency — it needs the system GTK headers to build)::

    sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1

- :class:`SolvePanel` — the "Solve" panel of the image viewer (embedded in
  the Enhance view below the cross-match overlay): the open stack's label, a
  **Solve** button running
  :meth:`~seestar_controller.astrometry.controller.SolveController.solve_open_stack`
  on a worker thread (the WCS is written into the FITS header and persisted
  via ``store.wcs``), an **Export annotated PNG** button
  (:class:`Gtk.FileDialog` save dialog; the programmatic
  :meth:`SolvePanel.export_annotated_to` drives the same write without the
  dialog) writing the annotated PNG (star labels + G mags, FOV rectangle,
  scale bar — :mod:`seestar_controller.astrometry.annotate`), and a status
  label. A solve/export outcome is surfaced as a toast through the
  ``show_toast`` callable (the window passes its toast overlay) — a failure
  as a clear ``"Solve failed: …"`` toast, never a crash;
- :class:`DriftPlotView` — the drift plot of the session/observe views: a
  session :class:`Gtk.DropDown`, **Refresh** and **Export PNG** buttons, and
  the RA/Dec-offset-vs-time plot on a :class:`Gtk.DrawingArea` (the pure
  :func:`~seestar_controller.astrometry.plot.render_drift_png` renders the
  :class:`~seestar_controller.astrometry.drift.DriftSeries` with matplotlib's
  Agg backend to PNG bytes; the PNG goes through cairo into the draw func —
  the same pattern as the visibility night-timeline), with an empty-state
  placeholder (:class:`Gtk.Stack`) when there is nothing to plot.

Both views are thin observers over the pure
:class:`~seestar_controller.astrometry.controller.SolveController` /
:class:`~seestar_controller.astrometry.controller.DriftController` (the app
owns one shared instance of each, following the shared viewer controller; the
window passes them down via :func:`seestar_controller.ui.views.build_view`).
Listener notifications are marshalled onto the GLib main loop when they come
from a worker thread.
"""

from __future__ import annotations

import io
import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

import cairo
from gi.repository import Gdk, GLib, Gtk
from PIL import Image

from .controller import DriftController, DriftSnapshot, SolveController, SolveSnapshot
from .drift import DriftError
from .plot import DEFAULT_HEIGHT_PX, DEFAULT_WIDTH_PX

logger = logging.getLogger(__name__)

#: The toast callable signature: ``show_toast(title: str) -> None``.
ShowToast = Callable[[str], None]


def texture_from_pil(image: Image.Image) -> Gdk.Texture:
    """Build a :class:`Gdk.Texture` from a PIL image (PNG bytes ->
    ``Gdk.Texture.new_from_bytes`` — the same pattern as the Enhance view's
    preview and the gaia overlay)."""
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return Gdk.Texture.new_from_bytes(GLib.Bytes(buffer.getvalue()))


class SolvePanel(Gtk.Box):
    """The astrometry panel of the image viewer: open-stack label + Solve +
    Export annotated PNG buttons on top, a status label below — observing a
    :class:`~seestar_controller.astrometry.controller.SolveController`.

    - ``solve_controller`` — the pure-layer solve controller (the app's
      shared one, following the shared viewer controller); a fresh one is
      created when omitted, so the widget also works standalone;
    - ``show_toast`` — an optional ``show_toast(title)`` callable (the window
      passes its toast overlay); solve/export outcomes are reported through
      it when set.
    """

    def __init__(
        self,
        solve_controller: SolveController | None = None,
        *,
        show_toast: ShowToast | None = None,
        **kwargs,
    ) -> None:
        kwargs.setdefault("orientation", Gtk.Orientation.VERTICAL)
        kwargs.setdefault("spacing", 6)
        super().__init__(**kwargs)
        if solve_controller is None:
            solve_controller = SolveController()
        self._controller = solve_controller
        self._show_toast = show_toast
        self._main_thread_id = threading.get_ident()
        self._busy = False

        # header row: heading + stack label + Solve + Export annotated PNG
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.append(Gtk.Label(label="Astrometry", xalign=0.0, css_classes=["heading"]))
        self._stack_label = Gtk.Label(
            label="no stack open",
            xalign=0.0,
            hexpand=True,
            css_classes=["dim-label"],
            tooltip_text="The stack open in the image viewer (the solve target)",
        )
        header.append(self._stack_label)
        self._solve_button = Gtk.Button(
            label="Solve",
            tooltip_text="Plate-solve the open stack (worker thread): the WCS is "
            "written into the FITS header and persisted in the project",
        )
        self._solve_button.connect("clicked", self._on_solve_clicked)
        header.append(self._solve_button)
        self._export_button = Gtk.Button(
            label="Export annotated PNG",
            tooltip_text="Save an annotated PNG of the open stack (star labels + G "
            "mags, FOV rectangle, scale bar)",
        )
        self._export_button.connect("clicked", self._on_export_clicked)
        header.append(self._export_button)
        self.append(header)

        # status label (status / clean error)
        self._status_label = Gtk.Label(label="", wrap=True, halign=Gtk.Align.CENTER)
        self.append(self._status_label)

        self.connect("destroy", self._on_destroy)
        self._controller.add_listener(self._on_snapshot)
        self._render(self._controller.snapshot)

    # -- public helpers (also used by the widget tests) ------------------------------

    @property
    def controller(self) -> SolveController:
        """The pure-layer solve controller backing this panel."""
        return self._controller

    @property
    def solve_controller(self) -> SolveController:
        """Alias of :attr:`controller`."""
        return self._controller

    @property
    def stack_label(self) -> Gtk.Label:
        """The open-stack label."""
        return self._stack_label

    @property
    def solve_button(self) -> Gtk.Button:
        """The Solve button."""
        return self._solve_button

    @property
    def export_button(self) -> Gtk.Button:
        """The Export annotated PNG button."""
        return self._export_button

    @property
    def status_label(self) -> Gtk.Label:
        """The status label (status / clean error)."""
        return self._status_label

    @property
    def busy(self) -> bool:
        """True while a worker-thread operation is running."""
        return self._busy

    def export_annotated_to(self, path: str | Path) -> SolveSnapshot:
        """Programmatic export (the same write the FileDialog drives): write
        the annotated PNG of the open stack to ``path`` and return the
        controller's snapshot."""
        return self._controller.export_annotated_png(path)

    # -- handlers -------------------------------------------------------------------------

    def _on_solve_clicked(self, *_args: Any) -> None:
        """Solve button: plate-solve the open stack on a worker thread; the
        outcome toast fires when the work is done."""
        def on_done() -> None:
            snapshot = self._controller.snapshot
            if snapshot.error is not None:
                self._toast(f"Solve failed: {snapshot.error}")
            elif snapshot.status is not None:
                self._toast(f"Solved: {snapshot.status}")

        self._run_async(lambda: self._controller.solve_open_stack(), on_done=on_done)

    def _on_export_clicked(self, *_args: Any) -> None:
        """Export button: a :class:`Gtk.FileDialog` save dialog; the chosen
        path is written on a worker thread in the async callback."""
        try:
            initial = self._controller.default_export_path(Path.cwd()).name
        except Exception:  # noqa: BLE001 - no stack open yet: generic name
            initial = "annotated-stack.png"
        dialog = Gtk.FileDialog(
            title="Export annotated PNG",
            accept_label="Export PNG",
        )
        dialog.set_initial_name(initial)
        dialog.save(self.get_root(), None, self._on_export_dialog_finished)

    def _on_export_dialog_finished(self, dialog: Gtk.FileDialog, result: Any) -> None:
        """FileDialog save callback: write the annotated PNG to the chosen
        file on a worker thread (inline error when cancelled/failed)."""
        try:
            file = dialog.save_finish(result)
        except Exception:  # noqa: BLE001 - cancelled dialog
            return
        if file is None:
            return
        path = file.get_path()

        def on_done() -> None:
            snapshot = self._controller.snapshot
            if snapshot.error is not None:
                self._toast(f"Export failed: {snapshot.error}")
            elif snapshot.status is not None:
                self._toast(snapshot.status)

        self._run_async(lambda: self._controller.export_annotated_png(path), on_done=on_done)

    def _on_destroy(self, *_args: Any) -> None:
        self._controller.remove_listener(self._on_snapshot)

    def _toast(self, title: str) -> None:
        if self._show_toast is not None:
            self._show_toast(title)

    def _run_async(self, fn: Any, *, on_done: Callable[[], None] | None = None) -> None:
        """Run ``fn`` on a daemon worker thread; the busy state is cleared on
        the main loop when it finishes (and ``on_done`` runs there)."""
        if self._busy:
            return
        self._busy = True
        self._solve_button.set_sensitive(False)
        self._export_button.set_sensitive(False)

        def work() -> None:
            try:
                fn()
            finally:
                GLib.idle_add(self._on_work_done, on_done)

        threading.Thread(target=work, daemon=True).start()

    def _on_work_done(self, on_done: Callable[[], None] | None = None) -> bool:
        self._busy = False
        snapshot = self._controller.snapshot
        has_stack = snapshot.stack_frame_id is not None
        self._solve_button.set_sensitive(has_stack and not snapshot.solving)
        self._export_button.set_sensitive(has_stack)
        if on_done is not None:
            try:
                on_done()
            except Exception:
                logger.exception("solve panel on_done callback failed")
        return GLib.SOURCE_REMOVE

    # -- controller listener ---------------------------------------------------------------

    def _on_snapshot(self, snapshot: SolveSnapshot) -> None:
        """Controller listener: render on the GLib main loop (notifications
        may come from a worker thread, so marshal with ``GLib.idle_add``; on
        the main thread render directly)."""
        if threading.get_ident() == self._main_thread_id:
            self._render(snapshot)
        else:
            GLib.idle_add(self._render, snapshot)

    def _render(self, snapshot: SolveSnapshot) -> bool:
        """Render a frozen snapshot: the stack label, the buttons' sensitivity,
        and the status. Returns ``GLib.SOURCE_REMOVE`` (one-shot idle
        callback)."""
        self._stack_label.set_label(snapshot.stack_label or "no stack open")
        has_stack = snapshot.stack_frame_id is not None
        if not self._busy:
            self._solve_button.set_sensitive(has_stack and not snapshot.solving)
            self._export_button.set_sensitive(has_stack)
        if snapshot.error is not None:
            self._status_label.set_label(snapshot.error)
            self._status_label.add_css_class("error")
        else:
            self._status_label.remove_css_class("error")
            if snapshot.status is not None:
                self._status_label.set_label(snapshot.status)
            elif snapshot.solved is not None:
                self._status_label.set_label(
                    f"solved ({snapshot.solved.solver}, {snapshot.solved.n_stars} stars)"
                )
            else:
                self._status_label.set_label("")
        return GLib.SOURCE_REMOVE


class DriftPlotView(Gtk.Box):
    """The drift plot of the session/observe views: session dropdown +
    Refresh + Export PNG buttons on top, the RA/Dec-offset-vs-time plot
    below, and a status label — observing a
    :class:`~seestar_controller.astrometry.controller.DriftController`.

    - ``drift_controller`` — the pure-layer drift controller (the app's
      shared one, following the shared viewer controller); a fresh one is
      created when omitted, so the widget also works standalone.
    """

    def __init__(
        self,
        drift_controller: DriftController | None = None,
        **kwargs,
    ) -> None:
        kwargs.setdefault("orientation", Gtk.Orientation.VERTICAL)
        kwargs.setdefault("spacing", 8)
        super().__init__(**kwargs)
        if drift_controller is None:
            drift_controller = DriftController()
        self._controller = drift_controller
        self._main_thread_id = threading.get_ident()
        self._busy = False
        self._syncing = False
        self._png_bytes: bytes = b""
        self._surface: cairo.ImageSurface | None = None

        # header row: heading + session dropdown + Refresh + Export PNG
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.append(Gtk.Label(label="Drift", xalign=0.0, css_classes=["heading"]))
        self._session_model = Gtk.StringList()
        self._session_dropdown = Gtk.DropDown(
            model=self._session_model,
            tooltip_text="The session whose solved frames are plotted",
        )
        self._session_dropdown.connect("notify::selected", self._on_session_selected)
        header.append(self._session_dropdown)
        self._refresh_button = Gtk.Button(
            label="Refresh",
            tooltip_text="Recompute the drift series of the selected session",
        )
        self._refresh_button.connect("clicked", lambda *_args: self._run_async(self._controller.refresh))
        header.append(self._refresh_button)
        self._export_button = Gtk.Button(
            label="Export PNG",
            tooltip_text="Save the drift plot as a PNG file",
        )
        self._export_button.connect("clicked", self._on_export_clicked)
        header.append(self._export_button)
        self.append(header)

        # plot | placeholder
        self._stack = Gtk.Stack()
        self._stack.set_hexpand(True)
        self._stack.set_vexpand(True)
        self._plot = Gtk.DrawingArea()
        self._plot.set_hexpand(True)
        self._plot.set_vexpand(True)
        self._plot.set_content_width(DEFAULT_WIDTH_PX)
        self._plot.set_content_height(DEFAULT_HEIGHT_PX)
        self._plot.set_draw_func(self._draw_plot)
        plot_frame = Gtk.Frame(child=self._plot)
        plot_frame.set_vexpand(True)
        self._stack.add_titled(plot_frame, "plot", "Plot")
        self._placeholder_label = Gtk.Label(
            label="No drift to plot — select a session and press Refresh",
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
            wrap=True,
            css_classes=["dim-label"],
        )
        self._stack.add_titled(self._placeholder_label, "empty", "Empty")
        self._stack.set_visible_child_name("empty")
        self.append(self._stack)

        # status label (summary / clean error)
        self._status_label = Gtk.Label(label="", wrap=True, halign=Gtk.Align.CENTER)
        self.append(self._status_label)

        self.connect("destroy", self._on_destroy)
        self._controller.add_listener(self._on_snapshot)
        self._render(self._controller.snapshot)

    # -- public helpers (also used by the widget tests) ------------------------------

    @property
    def controller(self) -> DriftController:
        """The pure-layer drift controller backing this view."""
        return self._controller

    @property
    def drift_controller(self) -> DriftController:
        """Alias of :attr:`controller`."""
        return self._controller

    @property
    def plot(self) -> Gtk.DrawingArea:
        """The drift-plot drawing area."""
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
        """The status label (summary / clean error)."""
        return self._status_label

    @property
    def refresh_button(self) -> Gtk.Button:
        """The Refresh button."""
        return self._refresh_button

    @property
    def export_button(self) -> Gtk.Button:
        """The Export PNG button."""
        return self._export_button

    @property
    def session_dropdown(self) -> Gtk.DropDown:
        """The session dropdown."""
        return self._session_dropdown

    @property
    def session_ids(self) -> tuple[str, ...]:
        """The session ids in the dropdown, in row order."""
        ids: list[str] = []
        index = 0
        while self._session_model.get_item(index) is not None:
            ids.append(self._session_model.get_string(index))
            index += 1
        return tuple(ids)

    @property
    def png_bytes(self) -> bytes:
        """The current rendered PNG bytes (empty before the first render)."""
        return self._png_bytes

    @property
    def series(self) -> Any:
        """The current drift series (None when the last refresh failed)."""
        return self._controller.snapshot.series

    def export_png(self, path: str | Path) -> Path:
        """Write the current drift-plot PNG to ``path``; returns the
        :class:`~pathlib.Path`. Raises :class:`~.drift.DriftError` when
        nothing has been rendered yet."""
        if not self._png_bytes:
            raise DriftError("nothing to export — the drift plot has not been rendered yet")
        out = Path(path)
        out.write_bytes(self._png_bytes)
        return out

    # -- handlers -------------------------------------------------------------------------

    def _on_session_selected(self, dropdown: Gtk.DropDown, *_args: Any) -> None:
        """Session dropdown: select the session in the controller and refresh
        on a worker thread (guarded against programmatic sync)."""
        if self._syncing:
            return
        index = dropdown.get_selected()
        item = self._session_model.get_item(index)
        if item is None:
            return
        session_id = self._session_model.get_string(index)

        def work() -> None:
            self._controller.set_session(session_id)
            self._controller.refresh()

        self._run_async(work)

    def _on_export_clicked(self, *_args: Any) -> None:
        """Export PNG button: a :class:`Gtk.FileDialog` save dialog; the chosen
        path is written via :meth:`export_png` in the async callback."""
        session_id = self._controller.snapshot.session_id or "session"
        dialog = Gtk.FileDialog(
            title="Export drift plot PNG",
            accept_label="Export PNG",
        )
        dialog.set_initial_name(f"drift-{session_id}.png")
        dialog.save(self.get_root(), None, self._on_export_dialog_finished)

    def _on_export_dialog_finished(self, dialog: Gtk.FileDialog, result: Any) -> None:
        """FileDialog save callback: write the PNG to the chosen file (inline
        error when there is nothing to export or the write fails)."""
        try:
            file = dialog.save_finish(result)
        except Exception:  # noqa: BLE001 - cancelled dialog
            return
        if file is None:
            return
        try:
            out = self.export_png(file.get_path())
        except Exception as exc:  # noqa: BLE001 - shown inline, never crash the view
            self._set_status(f"{type(exc).__name__}: {exc}", error=True)
            return
        self._set_status(f"exported {out}", error=False)

    def _on_destroy(self, *_args: Any) -> None:
        self._controller.remove_listener(self._on_snapshot)

    def _run_async(self, fn: Any) -> None:
        """Run ``fn`` on a daemon worker thread; the busy state is cleared on
        the main loop when it finishes."""
        if self._busy:
            return
        self._busy = True
        self._refresh_button.set_sensitive(False)

        def work() -> None:
            try:
                fn()
            finally:
                GLib.idle_add(self._on_work_done)

        threading.Thread(target=work, daemon=True).start()

    def _on_work_done(self) -> bool:
        self._busy = False
        self._refresh_button.set_sensitive(True)
        return GLib.SOURCE_REMOVE

    # -- controller listener ---------------------------------------------------------------

    def _on_snapshot(self, snapshot: DriftSnapshot) -> None:
        """Controller listener: render on the GLib main loop (notifications
        may come from a worker thread, so marshal with ``GLib.idle_add``; on
        the main thread render directly)."""
        if threading.get_ident() == self._main_thread_id:
            self._render(snapshot)
        else:
            GLib.idle_add(self._render, snapshot)

    def _render(self, snapshot: DriftSnapshot) -> bool:
        """Render a frozen snapshot: the session dropdown (without
        re-triggering it), the plot (when a series is available) or the
        placeholder, and the status. Returns ``GLib.SOURCE_REMOVE``."""
        self._syncing = True
        try:
            ids = [s.id for s in snapshot.sessions]
            while self._session_model.get_n_items() > 0:
                self._session_model.remove(0)
            for session_id in ids:
                self._session_model.append(session_id)
            if snapshot.session_id in ids:
                self._session_dropdown.set_selected(ids.index(snapshot.session_id))
            elif ids:
                self._session_dropdown.set_selected(len(ids) - 1)
        finally:
            self._syncing = False
        if snapshot.series is not None:
            try:
                png = self._controller.render_png()
                self._png_bytes = png
                self._surface = cairo.ImageSurface.create_from_png(io.BytesIO(png))
                self._stack.set_visible_child_name("plot")
                self._set_status(snapshot.status or "", error=False)
                self._plot.queue_draw()
            except Exception as exc:  # noqa: BLE001 - shown inline, never crash the view
                self._show_placeholder(
                    "Drift plot unavailable",
                    f"{type(exc).__name__}: {exc}",
                    error=True,
                )
        else:
            self._show_placeholder(
                "No drift to plot — select a session and press Refresh",
                snapshot.error or snapshot.status or "",
                error=snapshot.error is not None,
            )
        return GLib.SOURCE_REMOVE

    def _show_placeholder(self, placeholder_text: str, status_text: str, *, error: bool) -> None:
        """Reset the render state and show the placeholder page + status."""
        self._png_bytes = b""
        self._surface = None
        self._stack.set_visible_child_name("empty")
        self._placeholder_label.set_label(placeholder_text)
        self._set_status(status_text, error=error)
        self._plot.queue_draw()

    def _set_status(self, text: str, *, error: bool) -> None:
        self._status_label.set_label(text)
        for css_class in ("error", "dim-label"):
            self._status_label.remove_css_class(css_class)
        if error:
            self._status_label.add_css_class("error")
        else:
            self._status_label.add_css_class("dim-label")

    def _draw_plot(self, area: Gtk.DrawingArea, cr: Any, width: int, height: int) -> None:
        """DrawingArea draw func: paint the rendered drift plot (PNG -> cairo
        surface) scaled to the widget's allocation, centered, aspect
        preserved."""
        surface = self._surface
        if surface is None:
            cr.set_source_rgba(0.5, 0.5, 0.5, 1.0)
            cr.set_font_size(12.0)
            cr.move_to(12.0, height / 2.0)
            cr.show_text("no drift plot rendered")
            return
        sw = surface.get_width()
        sh = surface.get_height()
        if sw <= 0 or sh <= 0 or width <= 0 or height <= 0:
            return
        scale = min(width / sw, height / sh)
        cr.translate((width - sw * scale) / 2.0, (height - sh * scale) / 2.0)
        cr.scale(scale, scale)
        cr.set_source_surface(surface, 0.0, 0.0)
        cr.paint()


__all__ = [
    "DriftPlotView",
    "SolvePanel",
    "texture_from_pil",
]
