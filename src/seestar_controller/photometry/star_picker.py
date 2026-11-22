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
  opened image automatically; the list defaults to the stacks, newest-first —
  photometry is done on stacks, so the subs stay behind the **Show all
  frames** check button next to Refresh, image-enhance issue 05), a
  **Refresh** button, the **role selector**
  (Target / Comp / Check :class:`Gtk.DropDown` — the check star is designated
  here and persisted via store.stars on Save, check-star-plot ticket 02),
  the **go-to entry** (issue 06: "RA/Dec or name" + **Go** button — a sky
  coordinate is parsed by
  :func:`~seestar_controller.photometry.picking.parse_goto_target` and placed
  via
  :meth:`~seestar_controller.photometry.picking.PickController.add_at_sky`
  with the selected role; an object name is resolved through the injectable
  ``name_resolver`` — default: SIMBAD via the catalog facade, cached and
  offline-graceful — on a worker thread, since the lookup blocks on the
  network), the **aperture** spin buttons
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
  :meth:`~seestar_controller.photometry.picking.PickController.pick_at`; a
  :class:`Gtk.EventControllerMotion` (issue 06) drives the **hover sky-coord
  readout** — a caption label under the preview showing the RA/Dec (via
  :func:`~seestar_controller.photometry.picking.try_pixel_to_sky`) and pixel
  of the hovered position; the overlay's draw function paints each pick's
  aperture circle + dashed sky annulus + label (target green, comp blue, check
  amber).

The widget re-renders on every frozen
:class:`~seestar_controller.photometry.picking.PickSession` and
:class:`~seestar_controller.enhance.viewer_controller.ViewerSnapshot`.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Callable
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk

from ..catalog.coords import J2000Coord
from ..enhance.fits_io import EnhanceError, StackInfo
from ..enhance.render import render_preview
from ..enhance.viewer import subtitle_markup, texture_from_pil
from ..enhance.viewer_controller import ViewerController, ViewerSnapshot
from .picking import (
    ROLE_CHECK,
    ROLE_COMP,
    ROLE_TARGET,
    GotoTarget,
    PickController,
    PickingError,
    PickSession,
    StarPickSpec,
    has_celestial_wcs,
    parse_goto_target,
    try_pixel_to_sky,
)
from .suggest import SuggestionController
from .suggestions_panel import SuggestionsPanel

#: Left-pane width at construction (pixels; the user can drag the paned handle).
LEFT_PANE_POSITION = 320

#: Minimum content height (pixels) of the left pane's scrolled window — the
#: left pane scrolls instead of forcing the page/window minimum height up.
LEFT_PANE_MIN_CONTENT_HEIGHT = 200

#: Role selector entries: display label -> store role (the check star is
#: designated here — check-star-plot ticket 02; at most one check pick, with
#: replace semantics like the target).
ROLE_CHOICES: tuple[tuple[str, str], ...] = (
    ("Target", ROLE_TARGET),
    ("Comp", ROLE_COMP),
    ("Check", ROLE_CHECK),
)

#: Aperture spin ranges: ``(lower, upper, step, digits)``.
RADIUS_RANGE = (0.5, 50.0, 0.5, 1)
SKY_IN_RANGE = (1.0, 100.0, 0.5, 1)
SKY_OUT_RANGE = (1.0, 200.0, 0.5, 1)

#: Overlay colors (RGBA): the target star is green, comps blue, the check star
#: amber (check-star-plot ticket 02).
TARGET_COLOR = (0.30, 0.90, 0.45, 1.0)
COMP_COLOR = (0.45, 0.70, 1.0, 1.0)
CHECK_COLOR = (0.98, 0.72, 0.20, 1.0)

#: One aperture mark in widget coordinates: ``(wx, wy, radius, sky_in, sky_out,
#: role, label)`` — all radii in widget pixels.
Mark = tuple[float, float, float, float, float, str, str]

#: The name resolver used when none is injected: SIMBAD via the catalog facade
#: (cached, offline-graceful). Returns the first candidate's
#: :class:`~seestar_controller.catalog.coords.J2000Coord`; raises on failure
#: (the widget wraps it into a clean status / :class:`PickingError`).
NameResolver = Callable[[str], Any]


def default_name_resolver(name: str) -> J2000Coord:
    """Resolve an object ``name`` to a J2000 coord via SIMBAD (the catalog
    facade's :func:`~seestar_controller.catalog.facade.lookup_name` — cached,
    offline-graceful). Blocks on the network on a cache miss, so the widget
    runs it on a worker thread."""
    from seestar_controller.catalog.facade import lookup_name

    return lookup_name(name)[0].coord


class StarPickerView(Gtk.Box):
    """The Photometry view's star picker: stack list + role/aperture controls
    + picks list + Save picks + catalog-comps panel on the left, the
    clickable preview with the aperture overlay on the right.

    Constructed with the shared controllers (the app passes them; fresh ones
    are created when omitted, so the widget also works standalone):
    ``picker_controller`` / ``viewer_controller`` (ticket 01) and
    ``suggest_controller`` (ticket 02 — defaults to one bound to this view's
    pick controller). ``name_resolver`` (issue 06) resolves the object names
    of the go-to entry; it defaults to :func:`default_name_resolver` (SIMBAD
    via the catalog facade) and is injectable so tests never touch the
    network.
    """

    def __init__(
        self,
        picker_controller: PickController | None = None,
        viewer_controller: ViewerController | None = None,
        suggest_controller: SuggestionController | None = None,
        name_resolver: NameResolver | None = None,
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
        self._name_resolver: NameResolver = (
            name_resolver if name_resolver is not None else default_name_resolver
        )
        self._goto_thread: threading.Thread | None = None
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
        self._show_all_button = Gtk.CheckButton(
            label="Show all frames",
            tooltip_text="Also list the subs and enhanced frames (default: stacks only, "
            "newest first — photometry is done on stacks)",
        )
        self._show_all_button.connect("toggled", self._on_show_all_toggled)
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        controls.append(self._refresh_button)
        controls.append(self._show_all_button)
        left.append(controls)

        picking_group = Adw.PreferencesGroup(title="Picking")
        role_row = Adw.ActionRow(
            title="Role",
            subtitle="Role of the next clicked star (Target / Comp / Check)",
        )
        self._role_dropdown = Gtk.DropDown(
            model=Gtk.StringList.new([label for label, _role in ROLE_CHOICES]),
            selected=0,
        )
        self._role_dropdown.set_valign(Gtk.Align.CENTER)
        role_row.add_suffix(self._role_dropdown)
        picking_group.add(role_row)
        left.append(picking_group)

        # issue 06: "go to RA/Dec or name" — places the pick via
        # PickController.add_at_sky on the solved frame (no need to recognize
        # the target star visually)
        goto_group = Adw.PreferencesGroup(title="Go to target")
        goto_row = Adw.ActionRow(
            title="RA/Dec or name",
            subtitle=(
                "Place the next pick at a sky position (RA in hours, Dec in "
                "degrees — sexagesimal ok) or a resolved object name"
            ),
        )
        self._goto_entry = Gtk.Entry(
            placeholder_text="e.g. 5h32m00s +22d00m00s, or M 42",
            tooltip_text=(
                "Go to a sky coordinate (RA/Dec, comma or space separated) or an "
                "object name (resolved via SIMBAD); the pick is placed at the "
                "WCS pixel of the solved frame with the selected role"
            ),
            hexpand=True,
        )
        self._goto_entry.set_valign(Gtk.Align.CENTER)
        self._goto_entry.connect("activate", self._on_goto_activated)
        self._goto_button = Gtk.Button(
            label="Go",
            tooltip_text="Resolve the entry and place the pick at that sky position",
            valign=Gtk.Align.CENTER,
        )
        self._goto_button.connect("clicked", self._on_goto_clicked)
        goto_row.add_suffix(self._goto_entry)
        goto_row.add_suffix(self._goto_button)
        goto_group.add(goto_row)
        left.append(goto_group)

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
            tooltip_text="Save the picks (target + comps + check) into the current project",
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
        # issue 06: hover sky-coord readout (EventControllerMotion on the same
        # overlay — the motion position is mapped through the same CONTAIN
        # transform as the clicks)
        self._motion_controller = Gtk.EventControllerMotion()
        self._motion_controller.connect("motion", self._on_motion)
        self._motion_controller.connect("leave", self._on_motion_leave)
        self._overlay_area.add_controller(self._motion_controller)
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

        # issue 06: the hover sky-coord readout (caption under the preview)
        self._hover_label = Gtk.Label(
            label="", halign=Gtk.Align.CENTER, css_classes=["dim-label", "caption"]
        )
        right.append(self._hover_label)
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
    def refresh_button(self) -> Gtk.Button:
        """The Refresh button (re-lists the stacks)."""
        return self._refresh_button

    @property
    def show_all_button(self) -> Gtk.CheckButton:
        """The 'Show all frames' check button next to Refresh (the list
        filter toggle — image-enhance issue 05)."""
        return self._show_all_button

    @property
    def picks_list(self) -> Gtk.ListBox:
        """The picks list."""
        return self._picks_list

    @property
    def role_dropdown(self) -> Gtk.DropDown:
        """The role selector (Target / Comp / Check)."""
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
    def goto_entry(self) -> Gtk.Entry:
        """The "go to RA/Dec or name" entry (issue 06)."""
        return self._goto_entry

    @property
    def goto_button(self) -> Gtk.Button:
        """The go-to button next to the entry (issue 06)."""
        return self._goto_button

    @property
    def hover_label(self) -> Gtk.Label:
        """The hover sky-coord readout under the preview (issue 06)."""
        return self._hover_label

    @property
    def motion_controller(self) -> Gtk.EventControllerMotion:
        """The hover motion controller on the overlay (issue 06)."""
        return self._motion_controller

    @property
    def name_resolver(self) -> NameResolver:
        """The object-name resolver of the go-to entry (issue 06)."""
        return self._name_resolver

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

    # -- go to RA/Dec or name (issue 06) ----------------------------------------------

    def _selected_role(self) -> str:
        """The role currently selected in the role dropdown."""
        selected = self._role_dropdown.get_selected()
        return ROLE_CHOICES[selected][1] if 0 <= selected < len(ROLE_CHOICES) else ROLE_COMP

    def _resolve_name(self, name: str) -> J2000Coord:
        """Resolve ``name`` through :attr:`name_resolver` into a
        :class:`J2000Coord`; any resolver failure becomes a clean
        :class:`PickingError`."""
        try:
            resolved = self._name_resolver(name)
        except Exception as exc:  # any resolver failure becomes a clean error
            raise PickingError(f"cannot resolve {name!r}: {exc}") from exc
        ra = getattr(resolved, "ra_deg", None)
        dec = getattr(resolved, "dec_deg", None)
        if (ra is None or dec is None) and isinstance(resolved, (tuple, list)) and len(resolved) == 2:
            ra, dec = resolved[0], resolved[1]
        if ra is None or dec is None:
            raise PickingError(
                f"name resolver returned {resolved!r} for {name!r} — not a sky coord"
            )
        try:
            return J2000Coord(ra_deg=float(ra), dec_deg=float(dec))
        except (TypeError, ValueError) as exc:
            raise PickingError(
                f"name resolver returned {resolved!r} for {name!r} — not a sky coord: {exc}"
            ) from exc

    def go_to(self, text: str) -> StarPickSpec:
        """Synchronously go to a sky coordinate or object name (issue 06):
        parse ``text`` (see
        :func:`~seestar_controller.photometry.picking.parse_goto_target`),
        resolve a name through :attr:`name_resolver`, and place the pick with
        the selected role via
        :meth:`~seestar_controller.photometry.picking.PickController.add_at_sky`
        on the current (solved) frame.

        Returns the new :class:`StarPickSpec` (a name entry is used as the
        pick's label). Raises :class:`PickingError` with a clean message on a
        malformed entry, an unresolvable name, no image, no WCS, or a position
        mapping outside the frame.
        """
        target: GotoTarget = parse_goto_target(text)
        if target.kind == "coords":
            coord = target.coord
            assert coord is not None
            label: str | None = None
        else:
            coord = self._resolve_name(target.name)
            label = target.name
        role = self._selected_role()
        return self._picker_controller.add_at_sky(
            coord, role, label=label or self._next_label(role)
        )

    def _on_goto_activated(self, *_args) -> None:
        self._on_goto_clicked()

    def _on_goto_clicked(self, *_args) -> None:
        """Go button / Enter in the go-to entry: a coordinate is placed
        directly; a name is resolved on a worker thread (the SIMBAD lookup
        blocks on the network) and placed on the main loop when done."""
        text = self._goto_entry.get_text()
        try:
            target = parse_goto_target(text)
        except PickingError as exc:
            self._set_status(str(exc), error=True)
            return
        if target.kind == "coords":
            self._apply_goto(target.coord, label=None)
            return
        if self._goto_thread is not None and self._goto_thread.is_alive():
            return  # a resolve is already running
        self._set_status(f"resolving {target.name!r}…", error=False)
        thread = threading.Thread(
            target=self._resolve_worker,
            args=(target.name,),
            name="photometry-goto-resolve",
            daemon=True,
        )
        self._goto_thread = thread
        thread.start()

    def _resolve_worker(self, name: str) -> None:
        """Worker thread: resolve ``name`` (blocks on the network on a cache
        miss), then marshal the result back to the GLib main loop (the pick
        controller's listeners touch GTK widgets)."""
        try:
            coord = self._resolve_name(name)
        except PickingError as exc:
            GLib.idle_add(self._on_goto_resolved, name, None, str(exc))
            return
        GLib.idle_add(self._on_goto_resolved, name, coord, None)

    def _on_goto_resolved(
        self, name: str, coord: J2000Coord | None, error: str | None
    ) -> bool:
        """Main-loop callback of the name resolve; returns
        ``GLib.SOURCE_REMOVE`` (one-shot idle callback)."""
        if error is not None or coord is None:
            self._set_status(f"cannot resolve {name!r}: {error}", error=True)
        else:
            self._apply_goto(coord, label=name)
        return GLib.SOURCE_REMOVE

    def _apply_goto(self, coord: J2000Coord | None, *, label: str | None) -> StarPickSpec | None:
        """Place the pick at ``coord`` with the selected role via
        ``add_at_sky``; a failure is a clean error status. Returns the spec
        (None on failure)."""
        if coord is None:
            return None
        role = self._selected_role()
        try:
            spec = self._picker_controller.add_at_sky(
                coord, role, label=label or self._next_label(role)
            )
        except PickingError as exc:
            self._set_status(str(exc), error=True)
            return None
        self._set_status(f"went to {spec.describe()}", error=False)
        return spec

    # -- hover sky-coord readout (issue 06) --------------------------------------------

    def sky_at_widget(self, wx: float, wy: float) -> J2000Coord | None:
        """The sky coord of the hovered widget position ``(wx, wy)`` (mapped
        through the same CONTAIN transform as the clicks); None when no image
        is open or the frame has no usable WCS."""
        image = self._picker_controller.image
        if image is None:
            return None
        x, y = self.widget_to_image(wx, wy)
        return try_pixel_to_sky(image.header, x, y)

    def hover_text(self, wx: float, wy: float) -> str:
        """The hover readout text for the widget position ``(wx, wy)``:
        RA/Dec + pixel when the frame has a WCS, pixel-only otherwise."""
        image = self._picker_controller.image
        if image is None:
            return ""
        x, y = self.widget_to_image(wx, wy)
        coord = try_pixel_to_sky(image.header, x, y)
        if coord is None:
            return f"px {x:.1f}, {y:.1f} — no WCS on this frame"
        return (
            f"RA {coord.ra_hours:.5f}h  Dec {coord.dec_deg:+.5f}°"
            f"  —  px {x:.1f}, {y:.1f}"
        )

    def _on_motion(self, _controller: Gtk.EventControllerMotion, wx: float, wy: float) -> None:
        self._hover_label.set_label(self.hover_text(wx, wy))

    def _on_motion_leave(self, *_args) -> None:
        self._hover_label.set_label("")


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
        annulus + label of every pick (target green, comp blue, check amber)."""
        marks = self._compute_marks(width, height)
        self._last_marks = marks
        for wx, wy, radius, sky_in, sky_out, role, label in marks:
            if role == ROLE_TARGET:
                color = TARGET_COLOR
            elif role == ROLE_CHECK:
                color = CHECK_COLOR
            else:
                color = COMP_COLOR
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
        self._updating = True
        try:
            # keep the toggle in sync with the controller's filter
            if self._show_all_button.get_active() != snapshot.show_all:
                self._show_all_button.set_active(snapshot.show_all)
            if snapshot.current is not None and snapshot.current.frame_id in self._rows:
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
        check = next((p for p in snapshot.picks if p.role == ROLE_CHECK), None)
        comps = sum(1 for p in snapshot.picks if p.role == ROLE_COMP)
        wcs_note = "" if has_celestial_wcs(image.header) else " — no WCS (pixel-only picks)"
        self._info_label.set_label(
            f"{image.width}x{image.height}{wcs_note} — {len(snapshot.picks)} pick(s): "
            f"target {'set' if target is not None else '—'}, "
            f"check {'set' if check is not None else '—'}, {comps} comp(s)"
        )

    # -- signals ---------------------------------------------------------------------------

    def _on_pressed(self, gesture: Gtk.GestureClick, n_press: int, wx: float, wy: float) -> None:
        """Click on the preview: translate widget coords -> image pixel and
        pick with the selected role."""
        if self._updating:
            return
        role = self._selected_role()
        x, y = self.widget_to_image(wx, wy)
        try:
            spec = self._picker_controller.pick_at(x, y, role, label=self._next_label(role))
        except PickingError as exc:
            self._set_status(str(exc), error=True)
            return
        if spec.coord is None:
            # pixel-only pick: the frame has no usable WCS (e.g. a raw Seestar
            # sub) — the pick works in pixel space, sky coords need a
            # plate-solved frame (ticket 05)
            self._set_status(
                f"picked {spec.describe()} — pixel-only (no WCS on this frame; "
                "plate-solve it for sky coords)",
                error=False,
            )
        else:
            self._set_status(f"picked {spec.describe()}", error=False)

    def _next_label(self, role: str) -> str:
        """The automatic label for the next pick: ``"Target"`` / ``"Check"`` /
        ``"Comp A"``, ``"Comp B"``, ..."""
        if role == ROLE_TARGET:
            return "Target"
        if role == ROLE_CHECK:
            return "Check"
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

    def _on_show_all_toggled(self, button: Gtk.CheckButton) -> None:
        """'Show all frames' toggled: switch the controller's list filter
        (stacks by default — photometry is done on stacks; all stored frames
        when on)."""
        if self._updating:
            return
        self._viewer_controller.set_show_all(button.get_active())

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
        """Rebuild the stack list rows from ``stacks`` (the subtitle carries
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
