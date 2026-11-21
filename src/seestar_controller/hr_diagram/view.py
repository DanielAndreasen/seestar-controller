"""The H-R diagram view — the WIDGET layer of hr-diagram ticket 03.

This is the widget module of :mod:`seestar_controller.hr_diagram`: ``gi`` is
imported at module top (before any ``from gi.repository`` import) and the
package ``__init__`` exports :class:`HrDiagramView` lazily (PEP 562), so
importing the package never pulls in GTK (a subprocess test enforces the
boundary).

:class:`HrDiagramView` renders the apparent CMD and the H-R diagram of a
stack's cross-matched stars, observing an
:class:`~seestar_controller.hr_diagram.controller.HrDiagramController`:

- the diagram pages (CMD / H-R) are rendered by the PURE
  :func:`~seestar_controller.hr_diagram.plot.render_diagram_png` (matplotlib
  Agg -> PNG bytes) and painted onto a :class:`Gtk.DrawingArea` through the
  PNG -> PIL -> :class:`Gdk.Texture` path (cached as a cairo surface — the
  same image-enhance/visibility pattern), switchable with a
  :class:`Gtk.StackSwitcher`;
- the isochrone overlay comes from the controller's snapshot (params from
  ``store.isochrone_params``, hr-diagram ticket 02);
- zoom (scroll around the cursor) / pan (drag) / double-click reset re-render
  the current page through the pure transform math
  (:func:`~seestar_controller.hr_diagram.plot.zoom_window_at` /
  :func:`~seestar_controller.hr_diagram.plot.pan_window`);
- the **error-bar toggle** re-renders with the parallax-propagated magnitude
  errors (:func:`~seestar_controller.hr_diagram.plot.abs_g_error`);
- **click a diagram point** highlights the star (a ring on the diagram AND on
  the stack image), and **click the stack image** highlights the nearest
  crossmatched star on the diagram — coordinated via the crossmatch table's
  pixel positions (:class:`~seestar_controller.hr_diagram.plot.HrPoint` ``x``
  / ``y``): the stack image is rendered by
  :func:`~seestar_controller.hr_diagram.plot.render_stack_png` with the
  markers at those positions;
- **Export PNG** (the current page's PNG, :class:`Gtk.FileDialog` save
  dialog; the programmatic :meth:`HrDiagramView.export_png` drives the same
  write) and **Export CSV** (the cross-match table via the controller's
  :meth:`~seestar_controller.hr_diagram.controller.HrDiagramController.export_table`,
  reusing :func:`~seestar_controller.hr_diagram.export.export_table_csv` —
  the same conventions as :mod:`seestar_controller.analysis.export`).

Long operations (Refresh, stack selection) run on a daemon worker thread; the
controller marshals the store calls + listener notifications through its
``invoke_on_main`` (the app binds ``GLib.idle_add``), and the view re-renders
on the main loop.
"""

from __future__ import annotations

import io
import threading
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")

import cairo
from gi.repository import Gdk, GLib, Gtk
from PIL import Image

from .controller import HrDiagramController, HrDiagramSnapshot
from .core import HrError, HrPoint
from .plot import (
    CMD,
    DEFAULT_HEIGHT_PX,
    DEFAULT_STACK_MAX_PX,
    DEFAULT_WIDTH_PX,
    HR,
    KINDS,
    ZOOM_FACTOR,
    DiagramGeometry,
    DiagramModel,
    DiagramWindow,
    full_window,
    nearest_point_by_pixel,
    pan_window,
    point_at,
    render_diagram_png,
    render_stack_png,
    stack_image_scale,
    zoom_window_at,
)

#: The render size of the diagram pages (pixels) — the PNG is rendered at
#: exactly this size and scaled to the widget allocation when painted, so the
#: pure transform geometry (:class:`DiagramGeometry`) matches the PNG
#: pixel-for-pixel.
PLOT_WIDTH_PX = DEFAULT_WIDTH_PX
PLOT_HEIGHT_PX = DEFAULT_HEIGHT_PX

#: The max long side (pixels) of the rendered stack image.
STACK_MAX_PX = DEFAULT_STACK_MAX_PX

#: Initial drawing-area content size (pixels) before the first allocation.
INITIAL_PLOT_WIDTH = 480
INITIAL_PLOT_HEIGHT = 360

#: Default export file names.
EXPORT_PNG_NAME = "hr-diagram.png"
EXPORT_CSV_NAME = "crossmatch-table.csv"


def texture_from_pil(image: Image.Image) -> Gdk.Texture:
    """Build a :class:`Gdk.Texture` from a PIL image (PNG bytes ->
    ``Gdk.Texture.new_from_bytes`` — the same pattern as the Enhance viewer
    and the visibility timeline)."""
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return Gdk.Texture.new_from_bytes(GLib.Bytes(buffer.getvalue()))


def _texture_from_png(png: bytes) -> Gdk.Texture:
    """Build a :class:`Gdk.Texture` from PNG bytes."""
    return Gdk.Texture.new_from_bytes(GLib.Bytes(png))


class HrDiagramView(Gtk.Box):
    """The H-R diagram view: switchable CMD / H-R pages of the selected
    cross-matched stack, the isochrone overlay, zoom/pan, the error-bar
    toggle, click-to-highlight in both directions (diagram <-> stack image),
    and PNG/CSV export — observing an
    :class:`~seestar_controller.hr_diagram.controller.HrDiagramController`.

    Constructed with the shared controller (the app passes it; a fresh
    standalone one is created when omitted, so the widget also works alone).
    """

    def __init__(
        self,
        controller: HrDiagramController | None = None,
        **kwargs,
    ) -> None:
        kwargs.setdefault("orientation", Gtk.Orientation.VERTICAL)
        kwargs.setdefault("spacing", 6)
        super().__init__(**kwargs)
        if controller is None:
            controller = HrDiagramController()
        self._controller = controller
        self._main_thread_id = threading.get_ident()
        self._busy = False
        self._syncing = False

        # render state
        self._snapshot: HrDiagramSnapshot | None = None
        self._geom = DiagramGeometry(PLOT_WIDTH_PX, PLOT_HEIGHT_PX)
        self._windows: dict[str, DiagramWindow] = {kind: full_window(kind, ()) for kind in KINDS}
        self._points_by_kind: dict[str, tuple[HrPoint, ...]] = {CMD: (), HR: ()}
        self._point_ids: tuple[int, ...] = ()
        self._show_errors = False
        self._highlighted: int | None = None
        self._png: dict[str, bytes] = {}
        self._surface: dict[str, cairo.ImageSurface | None] = {kind: None for kind in KINDS}
        self._texture: dict[str, Gdk.Texture | None] = {kind: None for kind in KINDS}
        self._draw_transform = (1.0, 0.0, 0.0)  # scale, off_x, off_y (widget px)
        self._stack_png = b""
        self._stack_surface: cairo.ImageSurface | None = None
        self._stack_texture: Gdk.Texture | None = None
        self._stack_draw_transform = (1.0, 0.0, 0.0)
        self._stack_marker_scale = 1.0
        self._drag_last = (0.0, 0.0)
        self._stack_frame_ids: tuple[int, ...] = ()

        self._build_header()
        self._build_body()

        self._controller.add_listener(self._on_snapshot)
        self._render(self._controller.snapshot)

    # -- construction ----------------------------------------------------------------------

    def _build_header(self) -> None:
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.set_margin_top(6)
        header.set_margin_start(6)
        header.set_margin_end(6)
        header.append(Gtk.Label(label="H-R diagram", xalign=0.0, css_classes=["heading"]))
        self._stack_model = Gtk.StringList()
        self._stack_dropdown = Gtk.DropDown(model=self._stack_model)
        self._stack_dropdown.set_tooltip_text("The cross-matched stack to plot")
        self._stack_dropdown.connect("notify::selected", self._on_stack_selected)
        header.append(self._stack_dropdown)
        self._refresh_button = Gtk.Button(
            label="Refresh",
            tooltip_text="Re-read the project's cross-matched stacks from the store",
        )
        self._refresh_button.connect("clicked", self._on_refresh_clicked)
        header.append(self._refresh_button)
        self._errors_button = Gtk.ToggleButton(
            label="Error bars",
            tooltip_text="Draw the parallax-propagated magnitude error bars",
        )
        self._errors_button.connect("toggled", self._on_errors_toggled)
        header.append(self._errors_button)
        self._reset_button = Gtk.Button(
            label="Reset zoom",
            tooltip_text="Show the full data range (double-click the plot does the same)",
        )
        self._reset_button.connect("clicked", lambda *_args: self.reset_zoom())
        header.append(self._reset_button)
        self._export_png_button = Gtk.Button(label="Export PNG")
        self._export_png_button.set_tooltip_text("Save the current diagram page as a PNG")
        self._export_png_button.connect("clicked", self._on_export_png_clicked)
        header.append(self._export_png_button)
        self._export_csv_button = Gtk.Button(label="Export CSV")
        self._export_csv_button.set_tooltip_text("Save the cross-match table as a CSV")
        self._export_csv_button.connect("clicked", self._on_export_csv_clicked)
        header.append(self._export_csv_button)
        self._status_label = Gtk.Label(label="", halign=Gtk.Align.START, hexpand=True, wrap=True)
        header.append(self._status_label)
        self.append(header)

    def _build_body(self) -> None:
        self._stack = Gtk.Stack()
        self._stack.set_hexpand(True)
        self._stack.set_vexpand(True)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, wide_handle=True)
        paned.set_hexpand(True)
        paned.set_vexpand(True)

        # left: the CMD / H-R pages + switcher
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._pages = Gtk.Stack()
        self._pages.set_hexpand(True)
        self._pages.set_vexpand(True)
        self._plots: dict[str, Gtk.DrawingArea] = {}
        for kind in KINDS:
            plot = Gtk.DrawingArea()
            plot.set_hexpand(True)
            plot.set_vexpand(True)
            plot.set_content_width(INITIAL_PLOT_WIDTH)
            plot.set_content_height(INITIAL_PLOT_HEIGHT)
            plot.set_draw_func(self._draw_plot, kind)
            scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
            scroll.connect("scroll", self._on_scroll, kind)
            plot.add_controller(scroll)
            drag = Gtk.GestureDrag()
            drag.connect("drag-begin", self._on_drag_begin)
            drag.connect("drag-update", self._on_drag_update, kind)
            plot.add_controller(drag)
            click = Gtk.GestureClick(button=1)
            click.connect("pressed", self._on_plot_pressed, kind)
            plot.add_controller(click)
            frame = Gtk.Frame(child=plot)
            frame.set_vexpand(True)
            self._pages.add_titled(frame, kind, "CMD" if kind == CMD else "H-R")
            self._plots[kind] = plot
        self._pages.set_visible_child_name(CMD)
        switcher = Gtk.StackSwitcher(stack=self._pages)
        left.append(switcher)
        left.append(self._pages)
        paned.set_start_child(left)

        # right: the stack image with the cross-match markers
        self._stack_image = Gtk.DrawingArea()
        self._stack_image.set_hexpand(True)
        self._stack_image.set_vexpand(True)
        self._stack_image.set_content_width(240)
        self._stack_image.set_content_height(240)
        self._stack_image.set_draw_func(self._draw_stack_image)
        image_click = Gtk.GestureClick(button=1)
        image_click.connect("pressed", self._on_image_pressed)
        self._stack_image.add_controller(image_click)
        image_frame = Gtk.Frame(child=self._stack_image)
        image_frame.set_vexpand(True)
        paned.set_end_child(image_frame)
        paned.set_position(560)

        self._stack.add_titled(paned, "plot", "Plot")
        self._placeholder_label = Gtk.Label(
            label="No cross-matched stack selected",
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
            wrap=True,
            css_classes=["dim-label"],
        )
        self._stack.add_titled(self._placeholder_label, "empty", "Empty")
        self._stack.set_visible_child_name("empty")
        self.append(self._stack)

    # -- public helpers (also used by the widget tests) ------------------------------

    @property
    def controller(self) -> HrDiagramController:
        """The pure-layer H-R diagram controller backing this view."""
        return self._controller

    @property
    def hr_diagram_controller(self) -> HrDiagramController:
        """Alias of :attr:`controller`."""
        return self._controller

    @property
    def stack(self) -> Gtk.Stack:
        """The plot/placeholder stack."""
        return self._stack

    @property
    def pages(self) -> Gtk.Stack:
        """The CMD/H-R page stack (visible child name = the kind)."""
        return self._pages

    @property
    def placeholder_label(self) -> Gtk.Label:
        """The empty-state placeholder label."""
        return self._placeholder_label

    @property
    def status_label(self) -> Gtk.Label:
        """The status label (counts / clean error)."""
        return self._status_label

    @property
    def refresh_button(self) -> Gtk.Button:
        return self._refresh_button

    @property
    def reset_button(self) -> Gtk.Button:
        return self._reset_button

    @property
    def errors_button(self) -> Gtk.ToggleButton:
        return self._errors_button

    @property
    def export_png_button(self) -> Gtk.Button:
        return self._export_png_button

    @property
    def export_csv_button(self) -> Gtk.Button:
        return self._export_csv_button

    @property
    def stack_dropdown(self) -> Gtk.DropDown:
        return self._stack_dropdown

    @property
    def kind(self) -> str:
        """The visible diagram page (:data:`~.plot.CMD` or :data:`~.plot.HR`)."""
        return self._pages.get_visible_child_name() or CMD

    def plot(self, kind: str | None = None) -> Gtk.DrawingArea:
        """The drawing area of the ``kind`` page (default: the visible one)."""
        return self._plots[kind if kind is not None else self.kind]

    @property
    def stack_image(self) -> Gtk.DrawingArea:
        """The stack-image drawing area (markers + highlight ring)."""
        return self._stack_image

    def window(self, kind: str | None = None) -> DiagramWindow:
        """The current view window of the ``kind`` page."""
        return self._windows[kind if kind is not None else self.kind]

    def png_bytes(self, kind: str | None = None) -> bytes:
        """The rendered PNG bytes of the ``kind`` page (empty when none)."""
        return self._png.get(kind if kind is not None else self.kind, b"")

    def texture(self, kind: str | None = None) -> Gdk.Texture | None:
        """The current page texture (PNG -> PIL -> ``Gdk.Texture``), or None."""
        return self._texture.get(kind if kind is not None else self.kind)

    @property
    def stack_png_bytes(self) -> bytes:
        """The rendered stack-image PNG bytes (empty when none)."""
        return self._stack_png

    @property
    def stack_texture(self) -> Gdk.Texture | None:
        """The stack-image texture, or None."""
        return self._stack_texture

    @property
    def selected_source_id(self) -> int | None:
        """The highlighted star's ``source_id`` (None when none)."""
        return self._highlighted

    @property
    def show_errors(self) -> bool:
        """Whether the error bars are drawn."""
        return self._show_errors

    def set_kind(self, kind: str) -> None:
        """Switch the visible diagram page to ``kind`` (CMD / HR)."""
        if kind not in KINDS:
            raise HrError(f"unknown diagram kind {kind!r} — valid: {', '.join(KINDS)}")
        self._pages.set_visible_child_name(kind)
        self._render_page(kind)
        self._plots[kind].queue_draw()

    def reset_zoom(self, kind: str | None = None) -> DiagramWindow:
        """Reset the view window of ``kind`` (default: the visible page) to
        the full data range; re-renders and returns the window."""
        target = kind if kind is not None else self.kind
        self._windows[target] = full_window(target, self._points_by_kind[target])
        self._render_page(target)
        self._plots[target].queue_draw()
        return self._windows[target]

    def point_at(self, x: float, y: float, kind: str | None = None) -> HrPoint | None:
        """The diagram point within the hit radius of the WIDGET pixel
        ``(x, y)`` on the ``kind`` page (default: visible), or None — the
        hit-test of the click-to-highlight gesture (widget coords are mapped
        to the PNG pixel space first)."""
        target = kind if kind is not None else self.kind
        px, py = self._to_png_coords(x, y)
        return point_at(
            self._windows[target], self._geom, target, self._points_by_kind[target], px, py
        )

    def select_point(self, source_id: int | None) -> None:
        """Highlight the star with ``source_id`` on both the diagram and the
        stack image (None clears the highlight); re-renders."""
        if source_id is not None and (isinstance(source_id, bool) or not isinstance(source_id, int)):
            raise HrError(f"source_id must be an int or None, got {source_id!r}")
        self._highlighted = source_id
        for kind in KINDS:
            self._render_page(kind)
            self._plots[kind].queue_draw()
        self._render_stack_image()
        self._stack_image.queue_draw()
        if source_id is not None:
            self._set_status(f"highlighted star {source_id}", error=False)

    def stack_image_point_at(self, x: float, y: float) -> HrPoint | None:
        """The crossmatched star nearest to the WIDGET pixel ``(x, y)`` on the
        stack image (within the hit radius), or None — the hit-test of the
        image-click gesture (widget coords -> PNG coords -> original image
        pixels, where the crossmatch ``x`` / ``y`` live)."""
        if not self._snapshot or not self._snapshot.image_size:
            return None
        px, py = self._to_stack_png_coords(x, y)
        scale = self._stack_marker_scale
        if scale <= 0:
            return None
        return nearest_point_by_pixel(
            self._points_by_kind[CMD], px / scale, py / scale
        )

    def export_png(self, path: str | Path) -> Path:
        """Write the current page's rendered PNG to ``path``; returns the
        :class:`~pathlib.Path`. Raises :class:`HrError` when nothing has been
        rendered yet."""
        png = self.png_bytes()
        if not png:
            raise HrError("nothing to export — no diagram has been rendered yet")
        out = Path(path)
        out.write_bytes(png)
        return out

    def export_csv(self, path: str | Path) -> Path:
        """Write the selected stack's cross-match table as a CSV to ``path``
        (via the controller); returns the :class:`~pathlib.Path`. Raises
        :class:`HrError` when no stack is selected."""
        return self._controller.export_table(path)

    # -- events ------------------------------------------------------------------------

    def _on_refresh_clicked(self, *_args) -> None:
        """Refresh button: re-read the store on a worker thread (the
        controller notifies -> render)."""
        self._run_async(lambda: self._controller.refresh())

    def _on_stack_selected(self, dropdown: Gtk.DropDown, *_args) -> None:
        """Stack dropdown: select the chosen cross-matched stack on a worker
        thread."""
        if self._syncing:
            return
        index = dropdown.get_selected()
        if index == Gtk.INVALID_LIST_POSITION or index >= len(self._stack_frame_ids):
            return
        frame_id = self._stack_frame_ids[index]
        self._run_async(lambda: self._controller.select_stack(frame_id))

    def _on_errors_toggled(self, button: Gtk.ToggleButton) -> None:
        """Error-bar toggle: re-render both pages with/without the
        parallax-propagated magnitude errors."""
        self._show_errors = button.get_active()
        for kind in KINDS:
            self._render_page(kind)
            self._plots[kind].queue_draw()

    def _on_export_png_clicked(self, *_args) -> None:
        """Export PNG button: a :class:`Gtk.FileDialog` save dialog; the
        chosen path is written via :meth:`export_png` in the async callback."""
        dialog = Gtk.FileDialog(title="Export diagram PNG", accept_label="Export PNG")
        dialog.set_initial_name(EXPORT_PNG_NAME)
        dialog.save(
            self.get_root(),
            None,
            lambda dlg, result: self._on_export_png_finished(dlg, result),
        )

    def _on_export_png_finished(self, dialog: Gtk.FileDialog, result: Any) -> None:
        try:
            file = dialog.save_finish(result)
        except GLib.Error:
            return  # cancelled
        if file is None:
            return
        path = file.get_path()
        if not path:
            return
        self._controller.invoke_on_main(lambda: self._do_export_png(path))

    def _do_export_png(self, path: str) -> None:
        try:
            written = self.export_png(path)
        except (HrError, OSError) as exc:
            self._set_status(f"export failed: {exc}", error=True)
            return
        self._set_status(f"exported {written}", error=False)

    def _on_export_csv_clicked(self, *_args) -> None:
        """Export CSV button: a :class:`Gtk.FileDialog` save dialog for the
        cross-match table."""
        dialog = Gtk.FileDialog(title="Export cross-match CSV", accept_label="Export CSV")
        dialog.set_initial_name(EXPORT_CSV_NAME)
        dialog.save(
            self.get_root(),
            None,
            lambda dlg, result: self._on_export_csv_finished(dlg, result),
        )

    def _on_export_csv_finished(self, dialog: Gtk.FileDialog, result: Any) -> None:
        try:
            file = dialog.save_finish(result)
        except GLib.Error:
            return  # cancelled
        if file is None:
            return
        path = file.get_path()
        if not path:
            return
        self._controller.invoke_on_main(lambda: self._do_export_csv(path))

    def _do_export_csv(self, path: str) -> None:
        try:
            written = self.export_csv(path)
        except (HrError, OSError) as exc:
            self._set_status(f"export failed: {exc}", error=True)
            return
        self._set_status(f"exported {written}", error=False)

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

    def _on_scroll(self, controller: Gtk.EventControllerScroll, dx: float, dy: float, kind: str) -> bool:
        """Scroll = zoom around the cursor (scroll up, ``dy < 0``, zooms in);
        the data point under the cursor stays fixed (pure transform math)."""
        if not self._points_by_kind[kind]:
            return False
        factor = ZOOM_FACTOR ** (-dy)
        x, y = self._cursor_position(controller)
        px, py = self._to_png_coords(x, y)
        self._windows[kind] = zoom_window_at(self._windows[kind], self._geom, px, py, factor)
        self._render_page(kind)
        self._plots[kind].queue_draw()
        return True  # consume: the scroll never reaches a parent scrolled window

    def _on_drag_begin(self, gesture: Gtk.GestureDrag, start_x: float, start_y: float) -> None:
        self._drag_last = (0.0, 0.0)

    def _on_drag_update(self, gesture: Gtk.GestureDrag, offset_x: float, offset_y: float, kind: str) -> None:
        """Drag = pan: the content follows the cursor (pure transform math)."""
        if not self._points_by_kind[kind]:
            return
        last_x, last_y = self._drag_last
        dx, dy = offset_x - last_x, offset_y - last_y
        self._drag_last = (offset_x, offset_y)
        if dx == 0.0 and dy == 0.0:
            return
        scale = self._draw_transform[0]
        if scale <= 0:
            return
        self._windows[kind] = pan_window(
            self._windows[kind], self._geom, (dx / scale), (dy / scale)
        )
        self._render_page(kind)
        self._plots[kind].queue_draw()

    def _on_plot_pressed(self, gesture: Gtk.GestureClick, n_press: int, x: float, y: float, kind: str) -> None:
        """Single click on a diagram point highlights the star (on the diagram
        AND on the stack image); double-click resets the zoom of the page."""
        if n_press == 2:
            self.reset_zoom(kind)
            return
        if n_press != 1:
            return
        point = self.point_at(x, y, kind)
        if point is None:
            return
        self.select_point(point.source_id)

    def _on_image_pressed(self, gesture: Gtk.GestureClick, n_press: int, x: float, y: float) -> None:
        """Click on the stack image highlights the nearest crossmatched star
        on the diagram (coordinated via the crossmatch pixel positions)."""
        if n_press != 1:
            return
        point = self.stack_image_point_at(x, y)
        if point is None:
            return
        self.select_point(point.source_id)

    # -- worker thread ---------------------------------------------------------------------

    def _run_async(self, fn: Any) -> None:
        """Run ``fn`` on a daemon worker thread (the controller marshals the
        store calls + notifications through its ``invoke_on_main``)."""
        if self._busy:
            return
        self._busy = True
        self._refresh_button.set_sensitive(False)

        def work() -> None:
            try:
                fn()
            except Exception as exc:  # surfaced via the snapshot, never crash  # noqa: BLE001
                import logging

                logging.getLogger(__name__).info("hr-diagram operation failed: %s", exc)
            finally:
                GLib.idle_add(self._on_work_done)

        threading.Thread(target=work, daemon=True).start()

    def _on_work_done(self) -> bool:
        self._busy = False
        self._refresh_button.set_sensitive(True)
        return GLib.SOURCE_REMOVE

    # -- controller listener ---------------------------------------------------------------

    def _on_snapshot(self, snapshot: HrDiagramSnapshot) -> None:
        """Controller listener: render on the GLib main loop (notifications
        may come from a worker thread, so marshal with ``GLib.idle_add``; on
        the main thread render directly)."""
        if threading.get_ident() == self._main_thread_id:
            self._render(snapshot)
        else:
            GLib.idle_add(self._render, snapshot)

    def _render(self, snapshot: HrDiagramSnapshot) -> bool:
        """Render a frozen snapshot: update the stack dropdown, the points,
        reset the windows when the data changed, re-render both pages + the
        stack image, switch plot/placeholder, and update the status. Returns
        ``GLib.SOURCE_REMOVE`` (one-shot idle callback)."""
        self._snapshot = snapshot
        ids = tuple(p.source_id for p in snapshot.cmd)
        if ids != self._point_ids:
            self._point_ids = ids
            for kind in KINDS:
                self._windows[kind] = full_window(kind, self._points_for(snapshot, kind))
        self._points_by_kind = {kind: self._points_for(snapshot, kind) for kind in KINDS}

        # the stack dropdown: one row per cross-matched stack
        self._syncing = True
        try:
            while self._stack_model.get_n_items() > 0:
                self._stack_model.remove(0)
            self._stack_frame_ids = tuple(s.frame_id for s in snapshot.stacks)
            for s in snapshot.stacks:
                self._stack_model.append(f"{s.frame_id} · {s.matched_count}/{s.row_count} matched")
            if snapshot.frame_id is not None and snapshot.frame_id in self._stack_frame_ids:
                self._stack_dropdown.set_selected(self._stack_frame_ids.index(snapshot.frame_id))
            else:
                self._stack_dropdown.set_selected(Gtk.INVALID_LIST_POSITION)
        finally:
            self._syncing = False

        if snapshot.project_name is None:
            self._show_placeholder("No project open — open a project to see its H-R diagram")
        elif not snapshot.stacks:
            self._show_placeholder(
                "No cross-matched stacks yet — run the Gaia cross-match on a stack first"
            )
        elif snapshot.frame_id is None:
            self._show_placeholder("Select a cross-matched stack above")
        else:
            self._stack.set_visible_child_name("plot")
            for kind in KINDS:
                self._render_page(kind)
                self._plots[kind].queue_draw()
            self._render_stack_image()
            self._stack_image.queue_draw()

        if snapshot.error is not None:
            self._set_status(snapshot.error, error=True)
        elif snapshot.status is not None:
            self._set_status(
                f"{snapshot.stack_label or ''} — {snapshot.status} — scroll to zoom, "
                "drag to pan, double-click to reset, click a point to highlight the star",
                error=False,
            )
        return GLib.SOURCE_REMOVE

    def _points_for(self, snapshot: HrDiagramSnapshot, kind: str) -> tuple[HrPoint, ...]:
        return snapshot.cmd if kind == CMD else snapshot.hr

    def _show_placeholder(self, text: str) -> None:
        self._png = {}
        self._surface = {kind: None for kind in KINDS}
        self._texture = {kind: None for kind in KINDS}
        self._stack_png = b""
        self._stack_surface = None
        self._stack_texture = None
        self._stack.set_visible_child_name("empty")
        self._placeholder_label.set_label(text)
        self._set_status(text, error=False)

    # -- rendering ---------------------------------------------------------------------------

    def _render_page(self, kind: str) -> None:
        """Render one diagram page to PNG bytes + cairo surface + texture
        (matplotlib Agg, pure :mod:`.plot`). Any failure clears the page."""
        snapshot = self._snapshot
        if snapshot is None or snapshot.frame_id is None:
            return
        points = self._points_by_kind.get(kind, ())
        if not points:
            self._png[kind] = b""
            self._surface[kind] = None
            self._texture[kind] = None
            return
        try:
            model = DiagramModel(
                kind=kind,
                points=points,
                overlay=snapshot.overlay,
                show_errors=self._show_errors,
                highlighted=self._highlighted,
                title=snapshot.stack_label or "",
            )
            png = render_diagram_png(
                model, self._windows[kind], width_px=PLOT_WIDTH_PX, height_px=PLOT_HEIGHT_PX
            )
        except Exception as exc:  # shown inline, never crash the view  # noqa: BLE001
            self._set_status(f"render failed: {type(exc).__name__}: {exc}", error=True)
            return
        self._png[kind] = png
        self._surface[kind] = cairo.ImageSurface.create_from_png(io.BytesIO(png))
        self._texture[kind] = _texture_from_png(png)

    def _render_stack_image(self) -> None:
        """Render the stack image with the cross-match markers + the highlight
        ring (pure :func:`~.plot.render_stack_png`)."""
        snapshot = self._snapshot
        if snapshot is None or snapshot.image is None or snapshot.image_size is None:
            self._stack_png = b""
            self._stack_surface = None
            self._stack_texture = None
            self._stack_marker_scale = 1.0
            return
        try:
            png = render_stack_png(
                snapshot.image,
                self._points_by_kind.get(CMD, ()),
                self._highlighted,
                max_size=STACK_MAX_PX,
            )
        except Exception as exc:  # shown inline, never crash the view  # noqa: BLE001
            self._set_status(f"stack render failed: {type(exc).__name__}: {exc}", error=True)
            return
        self._stack_png = png
        self._stack_surface = cairo.ImageSurface.create_from_png(io.BytesIO(png))
        self._stack_texture = _texture_from_png(png)
        self._stack_marker_scale = stack_image_scale(snapshot.image_size, STACK_MAX_PX)

    def _to_png_coords(self, x: float, y: float) -> tuple[float, float]:
        """Map widget pixel coords of the visible plot to PNG pixel coords
        (the inverse of the draw transform)."""
        scale, off_x, off_y = self._draw_transform
        if scale <= 0:
            return x, y
        return (x - off_x) / scale, (y - off_y) / scale

    def _to_stack_png_coords(self, x: float, y: float) -> tuple[float, float]:
        """Map widget pixel coords of the stack image to its PNG pixel coords."""
        scale, off_x, off_y = self._stack_draw_transform
        if scale <= 0:
            return x, y
        return (x - off_x) / scale, (y - off_y) / scale

    def _draw_plot(self, area: Gtk.DrawingArea, cr: Any, width: int, height: int, kind: str) -> None:
        """DrawingArea draw func: paint the rendered page PNG (cached as a
        cairo surface) scaled to the widget's allocation, centered, aspect
        preserved — and remember the transform for the event handlers."""
        surface = self._surface.get(kind)
        if surface is None:
            cr.set_source_rgba(0.5, 0.5, 0.5, 1.0)
            cr.set_font_size(12.0)
            cr.move_to(12.0, height / 2.0)
            cr.show_text("no diagram rendered")
            return
        sw = surface.get_width()
        sh = surface.get_height()
        if sw <= 0 or sh <= 0 or width <= 0 or height <= 0:
            return
        scale = min(width / sw, height / sh)
        off_x = (width - sw * scale) / 2.0
        off_y = (height - sh * scale) / 2.0
        if kind == self.kind:
            self._draw_transform = (scale, off_x, off_y)
        cr.translate(off_x, off_y)
        cr.scale(scale, scale)
        cr.set_source_surface(surface, 0.0, 0.0)
        cr.paint()

    def _draw_stack_image(self, area: Gtk.DrawingArea, cr: Any, width: int, height: int) -> None:
        """DrawingArea draw func: paint the rendered stack PNG (markers
        included) scaled to the allocation, centered, aspect preserved."""
        surface = self._stack_surface
        if surface is None:
            cr.set_source_rgba(0.5, 0.5, 0.5, 1.0)
            cr.set_font_size(12.0)
            cr.move_to(12.0, height / 2.0)
            cr.show_text("no stack image")
            return
        sw = surface.get_width()
        sh = surface.get_height()
        if sw <= 0 or sh <= 0 or width <= 0 or height <= 0:
            return
        scale = min(width / sw, height / sh)
        off_x = (width - sw * scale) / 2.0
        off_y = (height - sh * scale) / 2.0
        self._stack_draw_transform = (scale, off_x, off_y)
        cr.translate(off_x, off_y)
        cr.scale(scale, scale)
        cr.set_source_surface(surface, 0.0, 0.0)
        cr.paint()

    # -- status ------------------------------------------------------------------------------

    def _set_status(self, text: str, *, error: bool) -> None:
        self._status_label.set_label(text)
        for css_class in ("error", "dim-label"):
            self._status_label.remove_css_class(css_class)
        if error:
            self._status_label.add_css_class("error")
        else:
            self._status_label.add_css_class("dim-label")


__all__ = ["HrDiagramView", "texture_from_pil"]
