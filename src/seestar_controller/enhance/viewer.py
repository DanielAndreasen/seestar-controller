"""The Enhance-view image viewer widget — the WIDGET layer of
:mod:`seestar_controller.enhance` (image-enhance ticket 01).

This module imports ``gi`` at module top (it is a widget module, the only
place allowed to): it is never imported by the pure layer
(:mod:`seestar_controller.enhance.fits_io` / :mod:`.render` /
:mod:`.viewer_controller`) or eagerly by the package ``__init__`` — the pure
layer stays gi-free and headless-testable (see docs/wiki/assumptions.md: the
sandbox has no GTK).

On the desktop the system packages are required (PyGObject is NOT a pip
dependency — it needs the system GTK headers to build)::

    sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1

:class:`ImageViewer` is a thin observer over the pure
:class:`~seestar_controller.enhance.viewer_controller.ViewerController` (the
app owns one shared instance; the window passes it down via
:func:`seestar_controller.ui.views.build_view`):

- **left pane** — the project's listed entries as a :class:`Gtk.ListBox`
  (one row per :class:`~seestar_controller.enhance.fits_io.StackInfo`, label +
  subtitle with the entry kind emphasized) inside a scrolled window, plus a
  **Refresh** button (re-lists the entries — newly stored frames and newly
  stitched mosaics appear) and a **Show all frames** check button (the
  list defaults to the stacks + file-based mosaics, newest-first — with 100+
  subs the stack row would be buried otherwise; toggling lists the subs and
  enhanced frames too, image-enhance issue 05) and an inline status label
  for errors; selecting a row opens that entry via
  :meth:`~seestar_controller.enhance.viewer_controller.ViewerController.open_stack`
  (a stored frame) or
  :meth:`~seestar_controller.enhance.viewer_controller.ViewerController.open_path`
  (a file-based entry, e.g. a mosaic);
- **right pane** — the tone-mapped preview in a :class:`Gtk.Picture`
  (``content_fit=CONTAIN`` — basic fit-to-widget zoom) fed from the
  controller's PIL preview: PIL -> PNG bytes -> :class:`Gdk.Texture`
  (``Gdk.Texture.new_from_bytes``, the same pattern as
  :func:`seestar_controller.observing.observe_view.texture_from_array`) over a
  placeholder overlay ("no project" / "no frame open"), plus an info label
  (target, utc, session, pixel size);
- the two panes sit in a :class:`Gtk.Paned` (same documented choice as the
  Observe view).

The widget re-renders on every frozen
:class:`~seestar_controller.enhance.viewer_controller.ViewerSnapshot` (it
registers as a controller listener); it never blocks the main loop — all
loading/rendering happens synchronously in the controller, which is fast
because the preview is downscaled before tone mapping.
"""

from __future__ import annotations

import io

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Gdk, GLib, Gtk
from PIL import Image

from .fits_io import EnhanceError, StackInfo
from .viewer_controller import ViewerController, ViewerSnapshot

#: Left-pane width at construction (pixels; the user can drag the paned handle).
LEFT_PANE_POSITION = 360


def subtitle_markup(stack: StackInfo) -> str:
    """The row subtitle as Pango markup with the entry ``kind`` EMPHASIZED
    (bold) at the front, so the kind scans at a glance in a long list:
    ``<b>stack</b> · session … · jd …`` (image-enhance issue 05)."""
    subtitle = stack.subtitle()
    kind = stack.kind
    rest = subtitle[len(kind) :] if subtitle.startswith(kind) else f" {subtitle}"
    return f"<b>{GLib.markup_escape_text(kind)}</b>{GLib.markup_escape_text(rest)}"


def texture_from_pil(image: Image.Image) -> Gdk.Texture:
    """Build a :class:`Gdk.Texture` from a PIL image (PNG bytes ->
    ``Gdk.Texture.new_from_bytes`` — the same pattern as the Observe view's
    preview)."""
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return Gdk.Texture.new_from_bytes(GLib.Bytes(buffer.getvalue()))


class ImageViewer(Gtk.Box):
    """The Enhance-view image viewer: entry list (stacks + file-based
    mosaics by default, newest-first; subs/enhanced behind the "show all"
    toggle) on the left, tone-mapped preview on the right, driven by a pure
    :class:`~seestar_controller.enhance.viewer_controller.ViewerController`.

    Constructed with the controller (the app passes its shared instance; a
    fresh one is created when omitted, so the widget also works standalone).
    """

    def __init__(
        self, controller: ViewerController | None = None, **kwargs
    ) -> None:
        kwargs.setdefault("orientation", Gtk.Orientation.VERTICAL)
        kwargs.setdefault("spacing", 0)
        super().__init__(**kwargs)
        self._controller = (
            controller if controller is not None else ViewerController()
        )
        self._keys: tuple[str, ...] = ()
        self._rows: dict[str, Gtk.ListBoxRow] = {}
        self._updating = False  # guard against programmatic row selection loops

        paned = Gtk.Paned(
            orientation=Gtk.Orientation.HORIZONTAL,
            start_child=self._build_left_pane(),
            end_child=self._build_right_pane(),
            position=LEFT_PANE_POSITION,
            shrink_start_child=False,
            shrink_end_child=False,
            wide_handle=True,
        )
        self._paned = paned
        self.append(paned)

        self._controller.add_listener(self._on_snapshot)
        self._on_snapshot(self._controller.snapshot)

    # -- construction ------------------------------------------------------------

    def _build_left_pane(self) -> Gtk.Widget:
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        left.set_margin_top(12)
        left.set_margin_bottom(12)
        left.set_margin_start(12)
        left.set_margin_end(12)
        left.set_hexpand(False)
        left.set_vexpand(True)

        heading = Gtk.Label(label="Frames", xalign=0.0, css_classes=["heading"])
        left.append(heading)

        self._list_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self._list_box.add_css_class("boxed-list")
        self._list_box.connect("row-selected", self._on_row_selected)
        scrolled = Gtk.ScrolledWindow(
            child=self._list_box,
            min_content_width=280,
            min_content_height=240,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            vexpand=True,
        )
        left.append(scrolled)

        self._refresh_button = Gtk.Button(
            label="Refresh",
            tooltip_text="Re-list the entries (stacks and mosaics; subs/enhanced with "
            "'Show all frames') of the current project",
        )
        self._refresh_button.connect("clicked", self._on_refresh_clicked)
        self._show_all_button = Gtk.CheckButton(
            label="Show all frames",
            tooltip_text="Also list the subs and enhanced frames (default: stacks and "
            "mosaics only, newest first)",
        )
        self._show_all_button.connect("toggled", self._on_show_all_toggled)
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        controls.append(self._refresh_button)
        controls.append(self._show_all_button)
        left.append(controls)

        self._status_label = Gtk.Label(label="", wrap=True, halign=Gtk.Align.CENTER)
        left.append(self._status_label)
        return left

    def _build_right_pane(self) -> Gtk.Widget:
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        right.set_margin_top(12)
        right.set_margin_bottom(12)
        right.set_margin_start(12)
        right.set_margin_end(12)
        right.set_hexpand(True)
        right.set_vexpand(True)

        self._picture = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True)
        self._picture.set_hexpand(True)
        self._picture.set_vexpand(True)
        self._picture.set_size_request(320, 240)
        self._placeholder = Gtk.Label(
            label="no project",
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
            css_classes=["dim-label", "heading"],
        )
        overlay = Gtk.Overlay(child=self._picture)
        overlay.add_overlay(self._placeholder)
        frame = Gtk.Frame(child=overlay)
        frame.set_vexpand(True)
        right.append(frame)

        self._info_label = Gtk.Label(
            label="", halign=Gtk.Align.CENTER, css_classes=["dim-label", "caption"]
        )
        right.append(self._info_label)
        return right

    # -- public helpers (also used by the widget tests) ------------------------------

    @property
    def controller(self) -> ViewerController:
        """The pure-layer viewer controller backing this widget."""
        return self._controller

    @property
    def paned(self) -> Gtk.Paned:
        """The two-pane split."""
        return self._paned

    @property
    def list_box(self) -> Gtk.ListBox:
        """The frame list."""
        return self._list_box

    @property
    def picture(self) -> Gtk.Picture:
        """The preview picture (right pane)."""
        return self._picture

    @property
    def placeholder(self) -> Gtk.Label:
        """The placeholder overlay label (right pane)."""
        return self._placeholder

    @property
    def info_label(self) -> Gtk.Label:
        """The info label under the preview (target/session/size)."""
        return self._info_label

    @property
    def status_label(self) -> Gtk.Label:
        """The inline status label (left pane, errors)."""
        return self._status_label

    @property
    def refresh_button(self) -> Gtk.Button:
        """The Refresh button (re-lists the entries)."""
        return self._refresh_button

    @property
    def show_all_button(self) -> Gtk.CheckButton:
        """The 'Show all frames' check button next to Refresh (the list
        filter toggle — image-enhance issue 05)."""
        return self._show_all_button

    def row_count(self) -> int:
        """The number of frame rows currently shown (test helper)."""
        return len(self._rows)

    def row_label(self, index: int) -> str:
        """The label of the frame row at ``index`` (test helper)."""
        row = self._list_box.get_row_at_index(index)
        if row is None:
            return ""
        child = row.get_child()
        label = child.get_first_child() if child is not None else None
        return label.get_label() if label is not None else ""

    def row_subtitle(self, index: int) -> str:
        """The plain-text subtitle (kind · session · jd · …) of the frame row
        at ``index`` (test helper; the widget renders it with the kind
        bolded via Pango markup)."""
        row = self._list_box.get_row_at_index(index)
        if row is None:
            return ""
        child = row.get_child()
        subtitle = child.get_last_child() if child is not None else None
        return subtitle.get_text() if subtitle is not None else ""

    # -- rendering ---------------------------------------------------------------------

    def _on_snapshot(self, snapshot: ViewerSnapshot) -> None:
        """Controller listener: a new ViewerSnapshot -> re-render the entry
        list, the preview picture, the placeholder and the labels."""
        # entry list: rebuild only when the entry keys changed
        keys = tuple(s.key for s in snapshot.stacks)
        if keys != self._keys:
            self._keys = keys
            self._rebuild_rows(snapshot.stacks)

        # keep the toggle in sync with the controller's filter (without
        # re-entering the toggled handler)
        self._updating = True
        try:
            if self._show_all_button.get_active() != snapshot.show_all:
                self._show_all_button.set_active(snapshot.show_all)
            # select the current entry's row (without re-opening it)
            if snapshot.current is not None and snapshot.current.key in self._rows:
                self._list_box.select_row(self._rows[snapshot.current.key])
        finally:
            self._updating = False

        # preview picture + placeholder + info label
        if snapshot.project_name is None:
            self._picture.set_paintable(None)
            self._placeholder.set_label("no project — open one in the Projects view")
            self._placeholder.set_visible(True)
            self._info_label.set_label("")
        elif snapshot.preview is not None and snapshot.current is not None:
            try:
                self._picture.set_paintable(texture_from_pil(snapshot.preview))
                self._placeholder.set_visible(False)
                image = snapshot.image
                size = f"{image.width}×{image.height}px" if image is not None else ""
                self._info_label.set_label(
                    f"{snapshot.current.label()} · {snapshot.current.subtitle()} · {size}"
                )
            except Exception:  # noqa: BLE001
                # a bad preview must never break the view
                self._placeholder.set_label("preview could not be rendered")
                self._placeholder.set_visible(True)
        else:
            self._picture.set_paintable(None)
            self._placeholder.set_label("no frame open — select one on the left")
            self._placeholder.set_visible(True)
            self._info_label.set_label("")

        # inline error
        if snapshot.error:
            self._set_status(snapshot.error, error=True)
        else:
            self._set_status("", error=False)

    def _rebuild_rows(self, stacks: tuple[StackInfo, ...]) -> None:
        """Rebuild the entry list rows from ``stacks`` (one row per listed
        entry — a stored frame or a file-based mosaic; the subtitle carries
        the entry kind, bolded at the front so it scans)."""
        self._rows = {}
        while (row := self._list_box.get_row_at_index(0)) is not None:
            self._list_box.remove(row)
        for stack in stacks:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            box.set_margin_top(6)
            box.set_margin_bottom(6)
            box.set_margin_start(8)
            box.set_margin_end(8)
            box.append(Gtk.Label(label=stack.label(), xalign=0.0))
            subtitle = Gtk.Label(xalign=0.0, css_classes=["caption"])
            subtitle.set_markup(subtitle_markup(stack))
            box.append(subtitle)
            row = Gtk.ListBoxRow(child=box)
            row.key = stack.key  # type: ignore[attr-defined]
            row.frame_id = stack.frame_id  # type: ignore[attr-defined]
            row.entry_path = str(stack.path)  # type: ignore[attr-defined]
            self._rows[stack.key] = row
            self._list_box.append(row)

    def _set_status(self, text: str, *, error: bool) -> None:
        self._status_label.set_label(text)
        for css_class in ("error", "dim-label"):
            self._status_label.remove_css_class(css_class)
        if error:
            self._status_label.add_css_class("error")
        else:
            self._status_label.add_css_class("dim-label")

    # -- signal handlers -----------------------------------------------------------------

    def _on_row_selected(self, _list_box: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        """An entry row was selected -> open that entry (a stored frame by
        frame id, a file-based entry such as a mosaic by path; inline error
        on failure)."""
        if row is None or self._updating:
            return
        frame_id = getattr(row, "frame_id", None)
        if frame_id is not None:
            open_entry = lambda: self._controller.open_stack(frame_id)
        else:
            entry_path = getattr(row, "entry_path", None)
            if entry_path is None:
                return
            open_entry = lambda: self._controller.open_path(entry_path)
        try:
            open_entry()
        except EnhanceError as exc:
            self._set_status(str(exc), error=True)

    def _on_refresh_clicked(self, *_args) -> None:
        """Refresh: re-list the entries (inline error on failure)."""
        try:
            self._controller.refresh_stacks()
        except EnhanceError as exc:
            self._set_status(str(exc), error=True)

    def _on_show_all_toggled(self, button: Gtk.CheckButton) -> None:
        """'Show all frames' toggled: switch the controller's list filter
        (stacks + mosaics by default, all stored frames when on)."""
        if self._updating:
            return
        self._controller.set_show_all(button.get_active())
