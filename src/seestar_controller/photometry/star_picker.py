"""The star-picker widget — the WIDGET layer of
:mod:`seestar_controller.photometry` (photometry ticket 01).

This module imports ``gi`` at module top (it is a widget module, the only
place allowed to): it is never imported by the pure layer
(:mod:`seestar_controller.photometry.picking`) or eagerly by the package
``__init__`` — the pure layer stays gi-free and headless-testable (see
docs/wiki/assumptions.md: the sandbox has no GTK).

On the desktop the system packages are required (PyGObject is NOT a pip
dependency — it needs the system GTK headers to build)::

    sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1

:class:`StarPickerView` is a thin observer over the pure
:class:`~seestar_controller.photometry.picking.PickController` (the app owns
one shared instance, following the shared
:class:`~seestar_controller.enhance.viewer_controller.ViewerController`; the
window passes both down via
:func:`seestar_controller.ui.views.build_view`):

- **left pane** — the project's **stacks** as a :class:`Gtk.ListBox` (one row
  per :class:`~seestar_controller.enhance.fits_io.StackInfo`, selecting a row
  opens the stack via the viewer controller — the pick controller adopts the
  opened image automatically), a **Refresh** button, the **role selector**
  (Target / Comp :class:`Gtk.DropDown`), the **aperture** spin buttons
  (radius + sky-annulus inner/outer, bound to
  :meth:`~seestar_controller.photometry.picking.PickController.set_aperture_params`),
  the **picks list** with a **Remove selected** button, the
  **Save picks** button (persists the picks into the project via the
  controller's ``save()``), and the **catalog comps** panel
  (:class:`~.suggestions_panel.SuggestionsPanel`, ticket 02: list the standard
  stars near the target and add one as a comp pick) — the whole left pane is
  wrapped in a vertical :class:`Gtk.ScrolledWindow` so it scrolls instead of
  forcing the window's minimum height up (see
  :data:`LEFT_PANE_MIN_CONTENT_HEIGHT`);
- **right pane** — the current stack's tone-mapped preview in a
  :class:`Gtk.Picture` (``CONTAIN`` fit, PIL -> PNG -> :class:`Gdk.Texture`,
  the same pattern as :class:`~seestar_controller.enhance.viewer.ImageViewer`)
  with a transparent :class:`Gtk.DrawingArea` overlay on top: a
  :class:`Gtk.GestureClick` translates the click's widget coordinates back to
  0-based image pixels (accounting for the CONTAIN scale/offset AND the
  preview-vs-FITS downscale) and calls
  :meth:`~seestar_controller.photometry.picking.PickController.pick_at`; the
  overlay's draw function paints each pick's aperture circle + dashed sky
  annulus + label (target green, comp blue).

The widget re-renders on every frozen
:class:`~seestar_controller.photometry.picking.PickSession` and
:class:`~seestar_controller.enhance.viewer_controller.ViewerSnapshot`.
"""

from __future__ import annotations

import math
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk

from ..enhance.fits_io import EnhanceError, StackInfo
from ..enhance.render import render_preview
from ..enhance.viewer import texture_from_pil
from ..enhance.viewer_controller import ViewerController, ViewerSnapshot
from .picking import (
    ROLE_COMP,
    ROLE_TARGET,
    PickController,
    PickingError,
    PickSession,
    StarPickSpec,
)
from .suggest import SuggestionController
from .suggestions_panel import SuggestionsPanel

#: Left-pane width at construction (pixels; the user can drag the paned handle).
LEFT_PANE_POSITION = 320

#: Minimum content height (pixels) of the left pane's scrolled window — the
#: left pane scrolls instead of forcing the page/window minimum height up.
LEFT_PANE_MIN_CONTENT_HEIGHT = 200

#: Role selector entries: display label -> store role.
ROLE_CHOICES: tuple[tuple[str, str], ...] = (("Target", ROLE_TARGET), ("Comp", ROLE_COMP))

#: Aperture spin ranges: ``(lower, upper, step, digits)``.
RADIUS_RANGE = (0.5, 50.0, 0.5, 1)
SKY_IN_RANGE = (1.0, 100.0, 0.5, 1)
SKY_OUT_RANGE = (1.0, 200.0, 0.5, 1)

#: Overlay colors (RGBA): the target star is green, comps blue.
TARGET_COLOR = (0.30, 0.90, 0.45, 1.0)
COMP_COLOR = (0.45, 0.70, 1.0, 1.0)

#: One aperture mark in widget coordinates: ``(wx, wy, radius, sky_in, sky_out,
#: role, label)`` — all radii in widget pixels.
Mark = tuple[float, float, float, float, float, str, str]


class StarPickerView(Gtk.Box):
    """The Photometry view's star picker: stack list + role/aperture controls
    + picks list + Save picks + catalog-comps panel on the left, the
    clickable preview with the aperture overlay on the right.

    Constructed with the shared controllers (the app passes them; fresh ones
    are created when omitted, so the widget also works standalone):
    ``picker_controller`` / ``viewer_controller`` (ticket 01) and
    ``suggest_controller`` (ticket 02 — defaults to one bound to this view's
    pick controller).
    """

    def __init__(
        self,
        picker_controller: PickController | None = None,
        viewer_controller: ViewerController | None = None,
        suggest_controller: SuggestionController | None = None,
        **kwargs,
    ) -> None:
        kwargs.setdefault("orientation", Gtk.Orientation.VERTICAL)
        kwargs.setdefault("spacing", 0)
        super().__init__(**kwargs)
        if viewer_controller is None and picker_controller is not None:
            viewer_controller = picker_controller.viewer_controller
        if viewer_controller is None:
            viewer_controller = ViewerController()
        self._viewer_controller = viewer_controller
        if picker_controller is None:
            picker_controller = PickController(viewer_controller=viewer_controller)
        elif picker_controller.viewer_controller is None:
            picker_controller.attach_viewer(viewer_controller)
        self._picker_controller = picker_controller
        if suggest_controller is None:
            # default: follow this view's pick controller (ticket 02); the app
            # passes the shared one, configured with the settings radius
            suggest_controller = SuggestionController(picker_controller=picker_controller)
        self._suggest_controller = suggest_controller
        self._stack_ids: tuple[int, ...] = ()
        self._rows: dict[int, Gtk.ListBoxRow] = {}
        self._updating = False  # guard against programmatic signal loops
        self._shown_image: Any = None
        self._preview_size: tuple[int, int] = (0, 0)
        self._last_marks: tuple[Mark, ...] = ()

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

        self._picker_controller.add_listener(self._on_pick_session)
        self._viewer_controller.add_listener(self._on_viewer_snapshot)
        self._on_viewer_snapshot(self._viewer_controller.snapshot)
        self._on_pick_session(self._picker_controller.snapshot)

    # -- construction ------------------------------------------------------------

    def _build_left_pane(self) -> Gtk.Widget:
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        left.set_margin_top(12)
        left.set_margin_bottom(12)
        left.set_margin_start(12)
        left.set_margin_end(12)
        left.set_hexpand(False)
        left.set_vexpand(True)

        heading = Gtk.Label(label="Stacks", xalign=0.0, css_classes=["heading"])
        left.append(heading)

        self._list_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self._list_box.add_css_class("boxed-list")
        self._list_box.connect("row-selected", self._on_row_selected)
        scrolled = Gtk.ScrolledWindow(
            child=self._list_box,
            min_content_width=260,
            min_content_height=100,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
        )
        left.append(scrolled)

        self._refresh_button = Gtk.Button(
            label="Refresh",
            tooltip_text="Re-list the stacks of the current project",
        )
        self._refresh_button.connect("clicked", self._on_refresh_clicked)
        left.append(self._refresh_button)

        picking_group = Adw.PreferencesGroup(title="Picking")
        role_row = Adw.ActionRow(title="Role", subtitle="Role of the next clicked star")
        self._role_dropdown = Gtk.DropDown(
            model=Gtk.StringList.new([label for label, _role in ROLE_CHOICES]),
            selected=0,
        )
        self._role_dropdown.set_valign(Gtk.Align.CENTER)
        role_row.add_suffix(self._role_dropdown)
        picking_group.add(role_row)
        left.append(picking_group)

        aperture_group = Adw.PreferencesGroup(title="Aperture (px)")
        self._radius_spin = self._make_spin(RADIUS_RANGE, "Aperture radius")
        self._sky_in_spin = self._make_spin(SKY_IN_RANGE, "Sky annulus inner")
        self._sky_out_spin = self._make_spin(SKY_OUT_RANGE, "Sky annulus outer")
        for spin, title in (
            (self._radius_spin, "Radius"),
            (self._sky_in_spin, "Sky inner"),
            (self._sky_out_spin, "Sky outer"),
        ):
            row = Adw.ActionRow(title=title)
            spin.set_valign(Gtk.Align.CENTER)
            row.add_suffix(spin)
            aperture_group.add(row)
        left.append(aperture_group)

        picks_heading = Gtk.Label(label="Picks", xalign=0.0, css_classes=["heading"])
        left.append(picks_heading)

        self._picks_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self._picks_list.add_css_class("boxed-list")
        picks_scrolled = Gtk.ScrolledWindow(
            child=self._picks_list,
            min_content_width=260,
            min_content_height=80,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
        )
        left.append(picks_scrolled)

        self._remove_button = Gtk.Button(
            label="Remove selected",
            tooltip_text="Remove the selected pick from the session",
        )
        self._remove_button.connect("clicked", self._on_remove_clicked)
        left.append(self._remove_button)

        self._save_button = Gtk.Button(
            label="Save picks",
            tooltip_text="Save the picks (target + comps) into the current project",
        )
        self._save_button.connect("clicked", self._on_save_clicked)
        left.append(self._save_button)

        self._suggestions_panel = SuggestionsPanel(self._suggest_controller)
        left.append(self._suggestions_panel)

        self._status_label = Gtk.Label(label="", wrap=True, halign=Gtk.Align.CENTER)
        left.append(self._status_label)
        # The left pane scrolls vertically instead of demanding its full
        # natural height (~1100 px with all groups expanded): the scrolled
        # window's minimum is small, so the Photometry page — and therefore
        # the window — keeps a small minimum size and the WM can maximize it
        # on laptop screens; every control stays reachable via the scrollbar.
        scrolled = Gtk.ScrolledWindow(
            child=left,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            min_content_height=LEFT_PANE_MIN_CONTENT_HEIGHT,
            vexpand=True,
        )
        return scrolled

    def _make_spin(self, range_: tuple[float, float, float, int], tooltip: str) -> Gtk.SpinButton:
        lower, upper, step, digits = range_
        spin = Gtk.SpinButton(
            adjustment=Gtk.Adjustment(
                value=lower, lower=lower, upper=upper, step_increment=step, page_increment=step * 10
            ),
            digits=digits,
            numeric=True,
            tooltip_text=tooltip,
        )
        spin.connect("value-changed", self._on_aperture_changed)
        return spin

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
        self._overlay_area = Gtk.DrawingArea()
        self._overlay_area.set_hexpand(True)
        self._overlay_area.set_vexpand(True)
        self._overlay_area.set_draw_func(self._draw_overlay)
        self._click_gesture = Gtk.GestureClick(button=1)
        self._click_gesture.connect("pressed", self._on_pressed)
        self._overlay_area.add_controller(self._click_gesture)
        self._placeholder = Gtk.Label(
            label="no stack open",
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
            css_classes=["dim-label", "heading"],
        )
        overlay = Gtk.Overlay(child=self._picture)
        overlay.add_overlay(self._placeholder)
        overlay.add_overlay(self._overlay_area)
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
    def controller(self) -> PickController:
        """The pure-layer pick controller backing this widget."""
        return self._picker_controller

    @property
    def picker_controller(self) -> PickController:
        """Alias of :attr:`controller`."""
        return self._picker_controller

    @property
    def viewer_controller(self) -> ViewerController:
        """The pure-layer viewer controller (stack list) backing this widget."""
        return self._viewer_controller

    @property
    def suggest_controller(self) -> SuggestionController:
        """The pure-layer catalog-assist controller (ticket 02) backing the
        suggestions panel."""
        return self._suggest_controller

    @property
    def suggestions_panel(self) -> SuggestionsPanel:
        """The catalog-comps suggestions panel in the left pane (ticket 02)."""
        return self._suggestions_panel

    @property
    def paned(self) -> Gtk.Paned:
        return self._paned

    @property
    def list_box(self) -> Gtk.ListBox:
        """The stack list."""
        return self._list_box

    @property
    def picks_list(self) -> Gtk.ListBox:
        """The picks list."""
        return self._picks_list

    @property
    def role_dropdown(self) -> Gtk.DropDown:
        """The role selector (Target / Comp)."""
        return self._role_dropdown

    @property
    def radius_spin(self) -> Gtk.SpinButton:
        """The aperture-radius spin button."""
        return self._radius_spin

    @property
    def sky_in_spin(self) -> Gtk.SpinButton:
        """The sky-annulus-inner spin button."""
        return self._sky_in_spin

    @property
    def sky_out_spin(self) -> Gtk.SpinButton:
        """The sky-annulus-outer spin button."""
        return self._sky_out_spin

    @property
    def save_button(self) -> Gtk.Button:
        """The Save picks button."""
        return self._save_button

    @property
    def picture(self) -> Gtk.Picture:
        """The preview picture."""
        return self._picture

    @property
    def overlay_area(self) -> Gtk.DrawingArea:
        """The transparent drawing area overlaying the preview (apertures +
        the click gesture)."""
        return self._overlay_area

    def pick_marks(self) -> tuple[Mark, ...]:
        """The current aperture marks in widget coordinates (one per pick)."""
        width, height = self._allocation_size()
        return self._compute_marks(width, height)

    # -- coordinate mapping (CONTAIN fit + preview downscale) ------------------------

    def _allocation_size(self) -> tuple[int, int]:
        """The overlay area's current size (falls back to the preview size
        before the widget is realized)."""
        width = self._overlay_area.get_width()
        height = self._overlay_area.get_height()
        pw, ph = self._preview_size
        if width <= 0:
            width = pw or 1
        if height <= 0:
            height = ph or 1
        return width, height

    def _contain_transform(self, width: int, height: int) -> tuple[float, float, float]:
        """The CONTAIN fit of the preview into ``width`` x ``height``:
        returns ``(scale, ox, oy)`` so that preview pixel ``(px, py)`` lands
        at widget ``(ox + px * scale, oy + py * scale)``."""
        pw, ph = self._preview_size
        if pw <= 0 or ph <= 0:
            return 1.0, 0.0, 0.0
        scale = min(width / pw, height / ph)
        ox = (width - pw * scale) / 2.0
        oy = (height - ph * scale) / 2.0
        return scale, ox, oy

    def widget_to_image(self, wx: float, wy: float) -> tuple[float, float]:
        """Widget coordinates (drawing-area space) -> 0-based FITS image
        pixel ``(x, y)`` (undoing the CONTAIN scale/offset and the
        preview-vs-FITS downscale)."""
        image = self._picker_controller.image
        pw, ph = self._preview_size
        if image is None or pw <= 0 or ph <= 0:
            return wx, wy
        width, height = self._allocation_size()
        scale, ox, oy = self._contain_transform(width, height)
        if scale <= 0.0:
            return wx, wy
        x = (wx - ox) / scale * (image.width / pw)
        y = (wy - oy) / scale * (image.height / ph)
        return x, y

    def image_to_widget(self, x: float, y: float) -> tuple[float, float]:
        """The inverse of :meth:`widget_to_image`: 0-based FITS image pixel
        -> widget coordinates."""
        image = self._picker_controller.image
        pw, ph = self._preview_size
        if image is None or pw <= 0 or ph <= 0:
            return x, y
        width, height = self._allocation_size()
        scale, ox, oy = self._contain_transform(width, height)
        px = x / image.width * pw
        py = y / image.height * ph
        return ox + px * scale, oy + py * scale

    # -- aperture marks + overlay drawing ----------------------------------------------

    def _compute_marks(self, width: int, height: int) -> tuple[Mark, ...]:
        """Compute the aperture marks of the current picks in widget
        coordinates for a ``width`` x ``height`` overlay: one
        ``(wx, wy, radius, sky_in, sky_out, role, label)`` per pick (radii in
        widget pixels)."""
        image = self._picker_controller.image
        pw, ph = self._preview_size
        if image is None or pw <= 0 or ph <= 0:
            return ()
        scale, ox, oy = self._contain_transform(width, height)
        aperture = self._picker_controller.aperture
        # widget pixels per FITS pixel (x and y may differ when the preview
        # is not square-proportional — average for the radii)
        per_x = pw / image.width * scale
        per_y = ph / image.height * scale
        per_px = (per_x + per_y) / 2.0
        marks: list[Mark] = []
        for pick in self._picker_controller.picks:
            px = pick.x / image.width * pw
            py = pick.y / image.height * ph
            wx = ox + px * scale
            wy = oy + py * scale
            marks.append(
                (
                    wx,
                    wy,
                    aperture.radius_px * per_px,
                    aperture.sky_in_px * per_px,
                    aperture.sky_out_px * per_px,
                    pick.role,
                    pick.label or pick.role,
                )
            )
        return tuple(marks)

    def _draw_overlay(self, area: Gtk.DrawingArea, cr: Any, width: int, height: int) -> None:
        """DrawingArea draw func: paint the aperture circle + dashed sky
        annulus + label of every pick (target green, comp blue)."""
        marks = self._compute_marks(width, height)
        self._last_marks = marks
        for wx, wy, radius, sky_in, sky_out, role, label in marks:
            color = TARGET_COLOR if role == ROLE_TARGET else COMP_COLOR
            cr.set_source_rgba(*color)
            cr.set_line_width(1.5)
            cr.arc(wx, wy, radius, 0.0, 2.0 * math.pi)
            cr.stroke()
            cr.set_dash([4.0, 4.0], 0.0)
            cr.arc(wx, wy, sky_in, 0.0, 2.0 * math.pi)
            cr.stroke()
            cr.arc(wx, wy, sky_out, 0.0, 2.0 * math.pi)
            cr.stroke()
            cr.set_dash([], 0.0)
            cr.move_to(wx + radius + 3.0, wy - radius - 3.0)
            cr.set_source_rgba(color[0], color[1], color[2], 0.9)
            cr.show_text(label)

    # -- controller listeners ------------------------------------------------------------

    def _on_pick_session(self, snapshot: PickSession) -> None:
        """Pick-controller listener: re-render the preview, the picks list,
        the aperture spins and the info label."""
        image = snapshot.image
        if image is not None and image is not self._shown_image:
            self._shown_image = image
            self._show_image(image)
        self._placeholder.set_visible(image is None)
        self._rebuild_pick_rows(snapshot.picks)
        self._updating = True
        try:
            aperture = snapshot.aperture
            self._radius_spin.set_value(aperture.radius_px)
            self._sky_in_spin.set_value(aperture.sky_in_px)
            self._sky_out_spin.set_value(aperture.sky_out_px)
        finally:
            self._updating = False
        self._overlay_area.queue_draw()
        self._update_info(snapshot)

    def _on_viewer_snapshot(self, snapshot: ViewerSnapshot) -> None:
        """Viewer listener: re-render the stack list + selection + status."""
        ids = tuple(s.frame_id for s in snapshot.stacks)
        if ids != self._stack_ids:
            self._stack_ids = ids
            self._rebuild_rows(snapshot.stacks)
        if snapshot.current is not None and snapshot.current.frame_id in self._rows:
            self._updating = True
            try:
                self._list_box.select_row(self._rows[snapshot.current.frame_id])
            finally:
                self._updating = False
        if snapshot.error:
            self._set_status(snapshot.error, error=True)

    def _show_image(self, image: Any) -> None:
        """Render the tone-mapped preview of ``image`` into the picture."""
        try:
            preview = render_preview(image)
        except EnhanceError as exc:
            self._set_status(str(exc), error=True)
            return
        self._preview_size = preview.size
        self._picture.set_paintable(texture_from_pil(preview))
        self._overlay_area.queue_draw()

    def _update_info(self, snapshot: PickSession) -> None:
        image = snapshot.image
        if image is None:
            self._info_label.set_label("no stack open — pick a stack on the left")
            return
        target = next((p for p in snapshot.picks if p.role == ROLE_TARGET), None)
        comps = sum(1 for p in snapshot.picks if p.role == ROLE_COMP)
        self._info_label.set_label(
            f"{image.width}x{image.height} — {len(snapshot.picks)} pick(s): "
            f"target {'set' if target is not None else '—'}, {comps} comp(s)"
        )

    # -- signals ---------------------------------------------------------------------------

    def _on_pressed(self, gesture: Gtk.GestureClick, n_press: int, wx: float, wy: float) -> None:
        """Click on the preview: translate widget coords -> image pixel and
        pick with the selected role."""
        if self._updating:
            return
        selected = self._role_dropdown.get_selected()
        role = ROLE_CHOICES[selected][1] if 0 <= selected < len(ROLE_CHOICES) else ROLE_COMP
        x, y = self.widget_to_image(wx, wy)
        try:
            spec = self._picker_controller.pick_at(x, y, role, label=self._next_label(role))
        except PickingError as exc:
            self._set_status(str(exc), error=True)
            return
        self._set_status(f"picked {spec.describe()}", error=False)

    def _next_label(self, role: str) -> str:
        """The automatic label for the next pick: ``"Target"`` / ``"Comp A"``,
        ``"Comp B"``, ..."""
        if role == ROLE_TARGET:
            return "Target"
        n = len(self._picker_controller.comps)
        letter = chr(ord("A") + min(n, 25))
        return f"Comp {letter}"

    def _on_row_selected(self, _list_box: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if row is None or self._updating:
            return
        frame_id = getattr(row, "frame_id", None)
        if frame_id is None:
            return
        try:
            self._viewer_controller.open_stack(frame_id)
        except EnhanceError as exc:
            self._set_status(str(exc), error=True)

    def _on_refresh_clicked(self, *_args) -> None:
        try:
            self._viewer_controller.refresh_stacks()
        except EnhanceError as exc:
            self._set_status(str(exc), error=True)

    def _on_aperture_changed(self, _spin: Gtk.SpinButton) -> None:
        if self._updating:
            return
        try:
            self._picker_controller.set_aperture_params(
                radius_px=self._radius_spin.get_value(),
                sky_in_px=self._sky_in_spin.get_value(),
                sky_out_px=self._sky_out_spin.get_value(),
            )
        except PickingError as exc:
            self._set_status(str(exc), error=True)

    def _on_remove_clicked(self, *_args) -> None:
        row = self._picks_list.get_selected_row()
        index = getattr(row, "pick_index", None) if row is not None else None
        if index is None:
            return
        try:
            removed = self._picker_controller.remove_pick(index)
        except PickingError as exc:
            self._set_status(str(exc), error=True)
            return
        self._set_status(f"removed {removed.describe()}", error=False)

    def _on_save_clicked(self, *_args) -> None:
        try:
            saved = self._picker_controller.save()
        except PickingError as exc:
            self._set_status(str(exc), error=True)
            return
        self._set_status(f"saved {len(saved)} pick(s) into the project", error=False)

    # -- list rows + status ------------------------------------------------------------------

    def _rebuild_rows(self, stacks: tuple[StackInfo, ...]) -> None:
        """Rebuild the stack list rows from ``stacks``."""
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
            box.append(
                Gtk.Label(
                    label=stack.subtitle(), xalign=0.0, css_classes=["dim-label", "caption"]
                )
            )
            row = Gtk.ListBoxRow(child=box)
            row.frame_id = stack.frame_id  # type: ignore[attr-defined]
            self._rows[stack.frame_id] = row
            self._list_box.append(row)

    def _rebuild_pick_rows(self, picks: tuple[StarPickSpec, ...]) -> None:
        """Rebuild the picks list rows from ``picks``."""
        while (row := self._picks_list.get_row_at_index(0)) is not None:
            self._picks_list.remove(row)
        for index, spec in enumerate(picks):
            label = Gtk.Label(label=spec.describe(), xalign=0.0)
            label.set_margin_top(6)
            label.set_margin_bottom(6)
            label.set_margin_start(8)
            label.set_margin_end(8)
            row = Gtk.ListBoxRow(child=label)
            row.pick_index = index  # type: ignore[attr-defined]
            self._picks_list.append(row)

    def _set_status(self, text: str, *, error: bool) -> None:
        self._status_label.set_label(text)
        for css_class in ("error", "dim-label"):
            self._status_label.remove_css_class(css_class)
        if error:
            self._status_label.add_css_class("error")
        else:
            self._status_label.add_css_class("dim-label")
