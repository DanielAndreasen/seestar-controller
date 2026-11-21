"""The Enhance-editor widget — the WIDGET layer of
:mod:`seestar_controller.enhance` (image-enhance tickets 03 + 04).

This module imports ``gi`` at module top (it is a widget module, the only
place allowed to): it is never imported by the pure layer
(:mod:`seestar_controller.enhance.editor_controller` /
:mod:`.viewer_controller` / :mod:`.pipeline` / ...) or eagerly by the package
``__init__`` — the pure layer stays gi-free and headless-testable (see
docs/wiki/assumptions.md: the sandbox has no GTK).

On the desktop the system packages are required (PyGObject is NOT a pip
dependency — it needs the system GTK headers to build)::

    sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1

:class:`EnhanceEditor` is a thin observer over the two pure controllers (the
app owns one shared
:class:`~seestar_controller.enhance.viewer_controller.ViewerController` and
one shared
:class:`~seestar_controller.enhance.editor_controller.EditorController`; the
window passes them down via
:func:`seestar_controller.ui.views.build_view`):

- **left pane** — the project's listed entries (all stored **frames** — subs,
  stacks, enhanced, sub-frames ticket 01 — PLUS the file-based stitched
  **mosaics**, app-improvement ticket 08) as a :class:`Gtk.ListBox` (one row
  per :class:`~seestar_controller.enhance.fits_io.StackInfo`, subtitle with the
  entry kind; selecting a row opens the entry via the viewer controller) with
  a **Refresh** button, and
  below it the **per-op parameter sliders**: one
  :class:`Adw.PreferencesGroup` per pipeline op (from the
  :data:`~seestar_controller.enhance.ops.OPS` registry + the params dataclass
  fields), a :class:`Gtk.Scale` per numeric param (ranges from
  :data:`PARAM_RANGES`), a :class:`Gtk.DropDown` per kind/mode/method param
  (:data:`PARAM_CHOICES`), and three scales for the ``gains`` tuple. Every
  control calls :meth:`~seestar_controller.enhance.editor_controller.EditorController.set_param`
  on change (the controller debounces the re-render);
- **right pane** — the **before/after preview**: a :class:`Gtk.Stack` with the
  original ("before") and enhanced ("after") :class:`Gtk.Picture`\\ s fed from
  the editor snapshot's ``original_preview`` / ``enhanced_preview`` (PIL ->
  PNG -> :class:`Gdk.Texture`, the same pattern as
  :class:`~seestar_controller.enhance.viewer.ImageViewer`), switched by a
  **Before/After** toggle button, over a placeholder overlay; plus the
  **Undo** / **Redo** / **Reset** buttons (sensitivity from
  ``can_undo`` / ``can_redo``) and a :class:`Gtk.Spinner` shown while
  ``rendering``;
- the two panes sit in a :class:`Gtk.Paned` (same documented choice as the
  Observe view / ImageViewer).

Ticket 04 adds the persistence buttons to the right-pane toolbar:
**Save enhanced** (render the current pipeline at full resolution and store it
as an ``enhanced`` frame in the project — the original stack FITS untouched),
**Save preset** (persist the current pipeline as the open stack's preset in
the project db), and **Export PNG** / **Export JPEG** (a
:class:`Gtk.FileDialog` save dialog — async; the chosen path is marshalled
back via the editor controller's ``invoke_on_main`` before
:meth:`~seestar_controller.enhance.editor_controller.EditorController.export`
runs). All four are sensitive only while a stack is open; failures surface in
the status label.

The widget re-renders on every frozen
:class:`~seestar_controller.enhance.editor_controller.EditorSnapshot` and
:class:`~seestar_controller.enhance.viewer_controller.ViewerSnapshot`; it
never blocks the main loop — the renders run on the editor controller's
worker thread and land via its ``invoke_on_main`` (``GLib.idle_add`` in the
app).
"""

from __future__ import annotations

import dataclasses
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk

from .editor_controller import EditorController, EditorSnapshot
from .fits_io import EnhanceError, StackInfo
from .ops import BACKGROUND_METHODS, STRETCH_KINDS, WB_MODES
from .pipeline import Pipeline
from .viewer import texture_from_pil
from .viewer_controller import ViewerController, ViewerSnapshot

#: Left-pane width at construction (pixels; the user can drag the paned handle).
LEFT_PANE_POSITION = 380

#: Slider width request (pixels).
SLIDER_WIDTH = 180

#: Choice-valued params (field name -> the allowed values) rendered as a
#: :class:`Gtk.DropDown` instead of a slider.
PARAM_CHOICES: dict[str, tuple[str, ...]] = {
    "kind": STRETCH_KINDS,
    "mode": WB_MODES,
    "method": BACKGROUND_METHODS,
}

#: Numeric param slider ranges: ``(op_name, field_name) -> (lower, upper,
#: step, digits)``. The ``gains`` entry is the PER-CHANNEL range (the tuple
#: field gets one slider per channel).
PARAM_RANGES: dict[tuple[str, str], tuple[float, float, float, int]] = {
    ("stretch", "sigma"): (0.1, 100.0, 0.1, 2),
    ("stretch", "bins"): (2, 65536, 1, 0),
    ("denoise", "strength"): (0.0, 1.0, 0.01, 2),
    ("denoise", "radius"): (1, 64, 1, 0),
    ("sharpen", "amount"): (0.0, 3.0, 0.01, 2),
    ("sharpen", "radius"): (1, 64, 1, 0),
    ("white_balance", "gains"): (0.1, 5.0, 0.05, 2),
    ("background", "degree"): (0, 10, 1, 0),
}

#: Channel labels for tuple-valued params (``gains``).
GAINS_CHANNELS = ("r", "g", "b")


class EnhanceEditor(Gtk.Box):
    """The Enhance-view editor: entry list (all stored frames — subs included —
    plus the file-based mosaics) + per-op sliders on the left, before/after
    preview + undo/redo/reset on the right.

    Constructed with the two controllers (the app passes its shared
    instances; fresh ones are created when omitted, so the widget also works
    standalone).
    """

    def __init__(
        self,
        viewer_controller: ViewerController | None = None,
        editor_controller: EditorController | None = None,
        **kwargs,
    ) -> None:
        kwargs.setdefault("orientation", Gtk.Orientation.VERTICAL)
        kwargs.setdefault("spacing", 0)
        super().__init__(**kwargs)
        self._viewer_controller = (
            viewer_controller if viewer_controller is not None else ViewerController()
        )
        self._editor_controller = (
            editor_controller
            if editor_controller is not None
            else EditorController(self._viewer_controller)
        )
        self._keys: tuple[str, ...] = ()
        self._rows: dict[str, Gtk.ListBoxRow] = {}
        self._updating = False  # guard against programmatic signal loops
        self._rendered_pipeline: Pipeline | None = None
        self._sliders: dict[tuple[str, str], Gtk.Scale] = {}
        self._dropdowns: dict[tuple[str, str], Gtk.DropDown] = {}
        self._shown_original: Any = None
        self._shown_enhanced: Any = None
        self._viewer_snapshot: ViewerSnapshot | None = None

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

        self._viewer_controller.add_listener(self._on_viewer_snapshot)
        self._editor_controller.add_listener(self._on_editor_snapshot)
        self._on_viewer_snapshot(self._viewer_controller.snapshot)
        self._on_editor_snapshot(self._editor_controller.snapshot)

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
            min_content_height=140,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
        )
        left.append(scrolled)

        self._refresh_button = Gtk.Button(
            label="Refresh",
            tooltip_text="Re-list the entries (frames and mosaics) of the current project",
        )
        self._refresh_button.connect("clicked", self._on_refresh_clicked)
        left.append(self._refresh_button)

        self._status_label = Gtk.Label(label="", wrap=True, halign=Gtk.Align.CENTER)
        left.append(self._status_label)

        enhance_heading = Gtk.Label(label="Enhance", xalign=0.0, css_classes=["heading"])
        left.append(enhance_heading)

        self._sliders_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        sliders_scrolled = Gtk.ScrolledWindow(
            child=self._sliders_box,
            min_content_width=280,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            vexpand=True,
        )
        left.append(sliders_scrolled)
        return left

    def _build_right_pane(self) -> Gtk.Widget:
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        right.set_margin_top(12)
        right.set_margin_bottom(12)
        right.set_margin_start(12)
        right.set_margin_end(12)
        right.set_hexpand(True)
        right.set_vexpand(True)

        # toolbar: before/after toggle + undo/redo/reset + spinner
        # A FlowBox (not a Box: GTK4 boxes do not wrap) so the buttons wrap
        # onto several lines on narrow windows — the toolbar's minimum width
        # is one button, not the sum of all eight, keeping the window's
        # minimum size small enough to maximize on laptop screens.
        toolbar = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            homogeneous=False,
            min_children_per_line=1,
            max_children_per_line=8,
            row_spacing=8,
            column_spacing=8,
        )
        self._before_after_toggle = Gtk.ToggleButton(
            label="Before",
            tooltip_text="Show the original (before) instead of the enhanced (after) image",
        )
        self._before_after_toggle.connect("toggled", self._on_before_after_toggled)
        toolbar.append(self._before_after_toggle)
        self._undo_button = Gtk.Button(label="Undo", tooltip_text="Undo the last parameter change")
        self._undo_button.connect("clicked", lambda *_a: self._editor_controller.undo())
        toolbar.append(self._undo_button)
        self._redo_button = Gtk.Button(label="Redo", tooltip_text="Redo the last undone change")
        self._redo_button.connect("clicked", lambda *_a: self._editor_controller.redo())
        toolbar.append(self._redo_button)
        self._reset_button = Gtk.Button(
            label="Reset", tooltip_text="Reset all parameters to the defaults"
        )
        self._reset_button.connect("clicked", lambda *_a: self._editor_controller.reset())
        toolbar.append(self._reset_button)
        # ticket 04: persistence buttons (save enhanced / save preset / export)
        self._save_enhanced_button = Gtk.Button(
            label="Save enhanced",
            tooltip_text="Save the enhanced image into the project (the original stack is untouched)",
        )
        self._save_enhanced_button.connect("clicked", self._on_save_enhanced_clicked)
        toolbar.append(self._save_enhanced_button)
        self._save_preset_button = Gtk.Button(
            label="Save preset",
            tooltip_text="Save the current parameters as this stack's preset (reapplied on open)",
        )
        self._save_preset_button.connect("clicked", self._on_save_preset_clicked)
        toolbar.append(self._save_preset_button)
        self._export_png_button = Gtk.Button(
            label="Export PNG", tooltip_text="Export the enhanced image as a PNG file"
        )
        self._export_png_button.connect(
            "clicked", lambda *_a: self._on_export_clicked("PNG")
        )
        toolbar.append(self._export_png_button)
        self._export_jpeg_button = Gtk.Button(
            label="Export JPEG", tooltip_text="Export the enhanced image as a JPEG file"
        )
        self._export_jpeg_button.connect(
            "clicked", lambda *_a: self._on_export_clicked("JPEG")
        )
        toolbar.append(self._export_jpeg_button)
        self._spinner = Gtk.Spinner(halign=Gtk.Align.END, hexpand=True, visible=False)
        toolbar.append(self._spinner)
        right.append(toolbar)

        # before/after preview stack
        self._before_picture = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True)
        self._after_picture = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True)
        for picture in (self._before_picture, self._after_picture):
            picture.set_hexpand(True)
            picture.set_vexpand(True)
        self._view_stack = Gtk.Stack()
        self._view_stack.add_named(self._before_picture, "before")
        self._view_stack.add_named(self._after_picture, "after")
        self._view_stack.set_visible_child_name("after")
        self._placeholder = Gtk.Label(
            label="no frame open",
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
            css_classes=["dim-label", "heading"],
        )
        overlay = Gtk.Overlay(child=self._view_stack)
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
    def controller(self) -> EditorController:
        """The pure-layer editor controller backing this widget."""
        return self._editor_controller

    @property
    def editor_controller(self) -> EditorController:
        """Alias of :attr:`controller`."""
        return self._editor_controller

    @property
    def viewer_controller(self) -> ViewerController:
        """The pure-layer viewer controller (frame list) backing this widget."""
        return self._viewer_controller

    @property
    def paned(self) -> Gtk.Paned:
        return self._paned

    @property
    def list_box(self) -> Gtk.ListBox:
        return self._list_box

    @property
    def refresh_button(self) -> Gtk.Button:
        return self._refresh_button

    @property
    def status_label(self) -> Gtk.Label:
        return self._status_label

    @property
    def sliders_box(self) -> Gtk.Box:
        return self._sliders_box

    @property
    def view_stack(self) -> Gtk.Stack:
        return self._view_stack

    @property
    def before_picture(self) -> Gtk.Picture:
        return self._before_picture

    @property
    def after_picture(self) -> Gtk.Picture:
        return self._after_picture

    @property
    def before_after_toggle(self) -> Gtk.ToggleButton:
        return self._before_after_toggle

    @property
    def undo_button(self) -> Gtk.Button:
        return self._undo_button

    @property
    def redo_button(self) -> Gtk.Button:
        return self._redo_button

    @property
    def reset_button(self) -> Gtk.Button:
        return self._reset_button

    @property
    def save_enhanced_button(self) -> Gtk.Button:
        return self._save_enhanced_button

    @property
    def save_preset_button(self) -> Gtk.Button:
        return self._save_preset_button

    @property
    def export_png_button(self) -> Gtk.Button:
        return self._export_png_button

    @property
    def export_jpeg_button(self) -> Gtk.Button:
        return self._export_jpeg_button

    @property
    def spinner(self) -> Gtk.Spinner:
        return self._spinner

    @property
    def placeholder(self) -> Gtk.Label:
        return self._placeholder

    @property
    def info_label(self) -> Gtk.Label:
        return self._info_label

    def slider_count(self) -> int:
        """The number of parameter sliders currently built (test helper)."""
        return len(self._sliders)

    def slider(self, op_name: str, field: str) -> Gtk.Scale:
        """The slider for ``(op_name, field)`` (``gains.r``/``.g``/``.b`` for
        the tuple field) — test helper."""
        return self._sliders[(op_name, field)]

    def dropdown(self, op_name: str, field: str) -> Gtk.DropDown:
        """The dropdown for a choice param — test helper."""
        return self._dropdowns[(op_name, field)]

    def op_group_titles(self) -> list[str]:
        """The titles of the per-op slider groups, in order (test helper)."""
        titles = []
        child = self._sliders_box.get_first_child()
        while child is not None:
            titles.append(child.get_title())
            child = child.get_next_sibling()
        return titles

    def row_count(self) -> int:
        """The number of entry rows currently shown (test helper)."""
        return len(self._rows)

    def row_label(self, index: int) -> str:
        """The label of the entry row at ``index`` (test helper)."""
        row = self._list_box.get_row_at_index(index)
        if row is None:
            return ""
        child = row.get_child()
        label = child.get_first_child() if child is not None else None
        return label.get_label() if label is not None else ""

    def row_subtitle(self, index: int) -> str:
        """The subtitle (kind · session · jd · …) of the entry row at
        ``index`` (test helper)."""
        row = self._list_box.get_row_at_index(index)
        if row is None:
            return ""
        child = row.get_child()
        subtitle = child.get_last_child() if child is not None else None
        return subtitle.get_label() if subtitle is not None else ""

    # -- slider construction ---------------------------------------------------------

    def _rebuild_sliders(self, pipeline: Pipeline | None) -> None:
        """Rebuild the per-op slider groups from ``pipeline`` (None clears)."""
        self._sliders = {}
        self._dropdowns = {}
        while (child := self._sliders_box.get_first_child()) is not None:
            self._sliders_box.remove(child)
        if pipeline is None:
            return
        for op in pipeline.ops:
            group = Adw.PreferencesGroup(title=op.name)
            for field in dataclasses.fields(op.params):
                self._add_param_control(group, op.name, field.name, getattr(op.params, field.name))
            self._sliders_box.append(group)

    def _add_param_control(self, group: Adw.PreferencesGroup, op_name: str, field_name: str, value: Any) -> None:
        """Add one control for the param ``(op_name, field_name)`` with current
        ``value`` to ``group``: a DropDown for choice params, one Scale per
        channel for tuple params, a Scale for numeric params."""
        if field_name in PARAM_CHOICES:
            choices = PARAM_CHOICES[field_name]
            model = Gtk.StringList.new(list(choices))
            selected = choices.index(value) if value in choices else 0
            dropdown = Gtk.DropDown(model=model, selected=selected)
            dropdown.connect(
                "notify::selected",
                lambda dd, _pspec, op=op_name, field=field_name: self._on_dropdown_selected(
                    op, field, dd
                ),
            )
            self._dropdowns[(op_name, field_name)] = dropdown
            row = Adw.ActionRow(title=field_name)
            row.add_suffix(dropdown)
            group.add(row)
            return
        if isinstance(value, tuple):  # e.g. gains=(r, g, b): one slider per channel
            lower, upper, step, digits = PARAM_RANGES[(op_name, field_name)]
            for channel, channel_value in zip(GAINS_CHANNELS, value):
                scale = self._make_scale(lower, upper, step, digits, float(channel_value))
                key = (op_name, f"{field_name}.{channel}")
                self._sliders[key] = scale
                scale.connect(
                    "value-changed",
                    lambda s, op=op_name, field=field_name: self._on_gains_changed(op, field),
                )
                row = Adw.ActionRow(title=f"{field_name} {channel}")
                row.add_suffix(scale)
                group.add(row)
            return
        if isinstance(value, bool):
            return  # no bool params today — skipped defensively
        if isinstance(value, (int, float)):
            lower, upper, step, digits = PARAM_RANGES[(op_name, field_name)]
            is_int = isinstance(value, int)
            scale = self._make_scale(lower, upper, step, digits, float(value))
            self._sliders[(op_name, field_name)] = scale
            scale.connect(
                "value-changed",
                lambda s, op=op_name, field=field_name, integer=is_int: self._on_scale_changed(
                    op, field, s, integer
                ),
            )
            row = Adw.ActionRow(title=field_name)
            row.add_suffix(scale)
            group.add(row)
            return
        # unknown param type: skipped defensively

    @staticmethod
    def _make_scale(
        lower: float, upper: float, step: float, digits: int, value: float
    ) -> Gtk.Scale:
        adjustment = Gtk.Adjustment(
            value=value,
            lower=lower,
            upper=upper,
            step_increment=step,
            page_increment=step * 10,
        )
        scale = Gtk.Scale(
            orientation=Gtk.Orientation.HORIZONTAL,
            adjustment=adjustment,
            draw_value=True,
            digits=digits,
        )
        scale.set_size_request(SLIDER_WIDTH, -1)
        scale.set_hexpand(True)
        return scale

    def _sync_slider_values(self, pipeline: Pipeline) -> None:
        """Move the sliders/dropdowns to ``pipeline``'s param values (guarded
        against signal loops) — used on undo/redo/reset/stack change."""
        self._updating = True
        try:
            for op in pipeline.ops:
                for field in dataclasses.fields(op.params):
                    value = getattr(op.params, field.name)
                    if field.name in PARAM_CHOICES:
                        dropdown = self._dropdowns.get((op.name, field.name))
                        choices = PARAM_CHOICES[field.name]
                        if dropdown is not None and value in choices:
                            dropdown.set_selected(choices.index(value))
                    elif isinstance(value, tuple):
                        for channel, channel_value in zip(GAINS_CHANNELS, value):
                            scale = self._sliders.get((op.name, f"{field.name}.{channel}"))
                            if scale is not None and abs(scale.get_value() - float(channel_value)) > 1e-9:
                                scale.set_value(float(channel_value))
                    elif isinstance(value, (int, float)) and not isinstance(value, bool):
                        scale = self._sliders.get((op.name, field.name))
                        if scale is not None and abs(scale.get_value() - float(value)) > 1e-9:
                            scale.set_value(float(value))
        finally:
            self._updating = False

    # -- signal handlers ---------------------------------------------------------------

    def _on_scale_changed(self, op_name: str, field: str, scale: Gtk.Scale, is_int: bool) -> None:
        if self._updating:
            return
        value: Any = scale.get_value()
        if is_int:
            value = round(value)
        try:
            self._editor_controller.set_param(op_name, **{field: value})
        except EnhanceError as exc:
            self._set_status(str(exc), error=True)

    def _on_gains_changed(self, op_name: str, field: str) -> None:
        if self._updating:
            return
        gains = tuple(
            self._sliders[(op_name, f"{field}.{channel}")].get_value()
            for channel in GAINS_CHANNELS
        )
        try:
            self._editor_controller.set_param(op_name, **{field: gains})
        except EnhanceError as exc:
            self._set_status(str(exc), error=True)

    def _on_dropdown_selected(self, op_name: str, field: str, dropdown: Gtk.DropDown) -> None:
        if self._updating:
            return
        choices = PARAM_CHOICES[field]
        index = dropdown.get_selected()
        if index >= len(choices):
            return
        try:
            self._editor_controller.set_param(op_name, **{field: choices[index]})
        except EnhanceError as exc:
            self._set_status(str(exc), error=True)

    def _on_before_after_toggled(self, toggle: Gtk.ToggleButton) -> None:
        self._view_stack.set_visible_child_name("before" if toggle.get_active() else "after")

    def _on_row_selected(self, _list_box: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        """An entry row was selected -> open that entry (a stored frame by
        frame id, a file-based entry such as a mosaic by path; inline error
        on failure)."""
        if row is None or self._updating:
            return
        frame_id = getattr(row, "frame_id", None)
        if frame_id is not None:
            open_entry = lambda: self._viewer_controller.open_stack(frame_id)
        else:
            entry_path = getattr(row, "entry_path", None)
            if entry_path is None:
                return
            open_entry = lambda: self._viewer_controller.open_path(entry_path)
        try:
            open_entry()
        except EnhanceError as exc:
            self._set_status(str(exc), error=True)

    def _on_refresh_clicked(self, *_args) -> None:
        try:
            self._viewer_controller.refresh_stacks()
        except EnhanceError as exc:
            self._set_status(str(exc), error=True)

    def _on_save_enhanced_clicked(self, *_args) -> None:
        try:
            frame = self._editor_controller.save_enhanced()
        except EnhanceError as exc:
            self._set_status(str(exc), error=True)
            return
        self._set_status(f"saved enhanced image: {frame.path}", error=False)

    def _on_save_preset_clicked(self, *_args) -> None:
        try:
            preset = self._editor_controller.save_preset()
        except EnhanceError as exc:
            self._set_status(str(exc), error=True)
            return
        self._set_status(
            f"saved preset for stack {preset.frame_id} ({len(preset.pipeline.get('ops', ()))} ops)",
            error=False,
        )

    def _on_export_clicked(self, fmt: str) -> None:
        """Open a :class:`Gtk.FileDialog` save dialog for ``fmt`` ("PNG" /
        "JPEG"); the async callback marshals the chosen path back onto the
        main thread via the editor controller's ``invoke_on_main``."""
        dialog = Gtk.FileDialog(
            title=f"Export {fmt}",
            accept_label=f"Export {fmt}",
        )
        suffix = ".png" if fmt == "PNG" else ".jpg"
        dialog.set_initial_name(f"enhanced{suffix}")
        dialog.save(
            self.get_root(),
            None,
            lambda dlg, result, fmt=fmt: self._on_export_dialog_finished(dlg, result, fmt),
        )

    def _on_export_dialog_finished(self, dialog: Gtk.FileDialog, result, fmt: str) -> None:
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
        self._editor_controller.invoke_on_main(lambda: self._do_export(path, fmt))

    def _do_export(self, path: str, fmt: str) -> None:
        try:
            written = self._editor_controller.export(path, format=fmt)
        except EnhanceError as exc:
            self._set_status(str(exc), error=True)
            return
        self._set_status(f"exported {written}", error=False)

    # -- rendering ---------------------------------------------------------------------

    def _on_viewer_snapshot(self, snapshot: ViewerSnapshot) -> None:
        """Viewer listener: re-render the frame list + selection + status."""
        self._viewer_snapshot = snapshot
        keys = tuple(s.key for s in snapshot.stacks)
        if keys != self._keys:
            self._keys = keys
            self._rebuild_rows(snapshot.stacks)
        if snapshot.current is not None and snapshot.current.key in self._rows:
            self._updating = True
            try:
                self._list_box.select_row(self._rows[snapshot.current.key])
            finally:
                self._updating = False
        if snapshot.error:
            self._set_status(snapshot.error, error=True)
        elif not self._editor_controller.snapshot.error:
            self._set_status("", error=False)

    def _on_editor_snapshot(self, snapshot: EditorSnapshot) -> None:
        """Editor listener: sliders, before/after previews, spinner, buttons,
        placeholder, info label, error."""
        pipeline = snapshot.pipeline
        if pipeline is None:
            if self._rendered_pipeline is not None:
                self._rendered_pipeline = None
                self._rebuild_sliders(None)
        elif self._rendered_pipeline is None or pipeline.names != self._rendered_pipeline.names:
            self._rendered_pipeline = pipeline
            self._rebuild_sliders(pipeline)
        else:
            self._rendered_pipeline = pipeline
            self._sync_slider_values(pipeline)

        # before/after previews
        if snapshot.original_preview is not None and snapshot.original_preview is not self._shown_original:
            self._shown_original = snapshot.original_preview
            self._before_picture.set_paintable(texture_from_pil(snapshot.original_preview))
        if snapshot.enhanced_preview is not None and snapshot.enhanced_preview is not self._shown_enhanced:
            self._shown_enhanced = snapshot.enhanced_preview
            self._after_picture.set_paintable(texture_from_pil(snapshot.enhanced_preview))

        # placeholder: no project / no stack / nothing rendered yet
        viewer = self._viewer_snapshot
        if viewer is not None and viewer.project_name is None:
            self._placeholder.set_label("no project — open one in the Projects view")
            self._placeholder.set_visible(True)
        elif snapshot.stack is None:
            self._placeholder.set_label("no frame open — select one on the left")
            self._placeholder.set_visible(True)
        elif snapshot.original_preview is None and snapshot.enhanced_preview is None:
            self._placeholder.set_label("rendering…")
            self._placeholder.set_visible(True)
        else:
            self._placeholder.set_visible(False)

        # spinner + buttons
        self._spinner.set_visible(snapshot.rendering)
        if snapshot.rendering:
            self._spinner.start()
        else:
            self._spinner.stop()
        self._undo_button.set_sensitive(snapshot.can_undo)
        self._redo_button.set_sensitive(snapshot.can_redo)
        self._reset_button.set_sensitive(snapshot.pipeline is not None)
        # ticket 04: the persistence buttons need an open stack
        has_stack = snapshot.stack is not None and snapshot.pipeline is not None
        self._save_enhanced_button.set_sensitive(has_stack)
        self._save_preset_button.set_sensitive(has_stack)
        self._export_png_button.set_sensitive(has_stack)
        self._export_jpeg_button.set_sensitive(has_stack)

        # info label
        if snapshot.stack is not None:
            ops = ", ".join(snapshot.pipeline.names) if snapshot.pipeline else ""
            extras = []
            if snapshot.preset_saved:
                extras.append("preset saved")
            if snapshot.saved_frame is not None:
                extras.append(f"saved {snapshot.saved_frame.path}")
            suffix = f" · {' · '.join(extras)}" if extras else ""
            self._info_label.set_label(f"{snapshot.stack.label()} · {ops}{suffix}")
        else:
            self._info_label.set_label("")

        # inline error
        if snapshot.error:
            self._set_status(snapshot.error, error=True)
        elif self._viewer_snapshot is None or not self._viewer_snapshot.error:
            self._set_status("", error=False)

    def _rebuild_rows(self, stacks: tuple[StackInfo, ...]) -> None:
        """Rebuild the entry list rows from ``stacks`` (one row per listed
        entry — a stored frame or a file-based mosaic)."""
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
