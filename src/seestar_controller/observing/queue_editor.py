"""The plan-queue editor widget — the WIDGET layer of :mod:`seestar_controller.observing`.

This module imports ``gi`` at module top (it is a widget module, the only
place allowed to): it is never imported by the pure layer
(:mod:`seestar_controller.observing.queue` / :mod:`.queue_controller`) or
eagerly by the package ``__init__`` — the pure layer stays gi-free and
headless-testable (see docs/wiki/assumptions.md: the sandbox has no GTK).

On the desktop the system packages are required (PyGObject is NOT a pip
dependency — it needs the system GTK headers to build)::

    sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1

:class:`QueueEditor` is a thin observer over a pure
:class:`~.queue_controller.PlanQueueController` (the app owns one shared
instance; the Observe view gets it from the app/window):

- a :class:`Gtk.ListBox` of the queued entries, one row each showing
  ``entry.describe()`` — ``name (ra, dec) | mosaic | exp | gain`` — with
  per-row **up / down / duplicate / remove** buttons driving the controller's
  reorder/remove/duplicate ops (buttons, not drag-and-drop — simpler and
  testable);
- an **Add** area: a name :class:`Adw.EntryRow` + a **Resolve** button (calls
  the controller's ``resolve_name`` — the catalog via the resolver — and shows
  the result or the error inline in the status label), RA/Dec
  :class:`Adw.EntryRow`\\ s (validated live through the catalog's
  ``parse_ra``/``parse_dec`` when Add is clicked), a mosaic switch with pan/tilt
  spin buttons, and exposure/gain spin buttons — all with the validated ranges
  (exposure 1-60 s, gain 0-100, mosaic 0-10 deg, mirroring the link's stacking
  contract);
- the **Add** button: name XOR RA/Dec (both given -> inline error); a name
  adds an unresolved entry (or a resolved one when Resolve succeeded for that
  name), RA/Dec adds a coord entry. Validation failures show inline in the
  status label and add nothing.

The editor re-renders the list on every queue-changed event (it registers as a
controller listener), so all edits — from this widget or anywhere else — show
up.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk, Pango

from seestar_controller.catalog.coords import J2000Coord

from .queue import (
    DEFAULT_EXPOSURE_S,
    DEFAULT_GAIN,
    EXPOSURE_MAX_S,
    EXPOSURE_MIN_S,
    GAIN_MAX,
    GAIN_MIN,
    MOSAIC_MAX_DEG,
    MOSAIC_MIN_DEG,
    QueueError,
)
from .queue_controller import PlanQueueController

#: Per-row button spec: (key, icon, tooltip).
_ROW_BUTTONS: tuple[tuple[str, str, str], ...] = (
    ("up", "arrow-up-symbolic", "Move up"),
    ("down", "arrow-down-symbolic", "Move down"),
    ("duplicate", "edit-copy-symbolic", "Duplicate"),
    ("remove", "list-remove-symbolic", "Remove"),
)


class QueueEditor(Gtk.Box):
    """The plan-queue editor: entry list + add area, driven by a pure
    :class:`~.queue_controller.PlanQueueController`.

    Constructed with the controller (the app passes its shared instance; a
    fresh one is created when omitted, so the widget also works standalone).
    """

    def __init__(
        self, controller: PlanQueueController | None = None, **kwargs
    ) -> None:
        kwargs.setdefault("orientation", Gtk.Orientation.VERTICAL)
        kwargs.setdefault("spacing", 12)
        kwargs.setdefault("margin_top", 12)
        kwargs.setdefault("margin_bottom", 12)
        kwargs.setdefault("margin_start", 12)
        kwargs.setdefault("margin_end", 12)
        super().__init__(**kwargs)
        self._controller = (
            controller if controller is not None else PlanQueueController()
        )
        #: the last successful Resolve of the name field: (name, coord)
        self._resolved: tuple[str, J2000Coord] | None = None
        #: per-row buttons, rebuilt with the list: list of {key: Gtk.Button}
        self._row_buttons: list[dict[str, Gtk.Button]] = []

        # --- the queue list ---------------------------------------------------
        self._list_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self._list_box.add_css_class("boxed-list")
        scrolled = Gtk.ScrolledWindow(
            child=self._list_box,
            vexpand=True,
            min_content_height=180,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
        )
        self.append(scrolled)

        # --- add area: name / RA / Dec ----------------------------------------
        group = Adw.PreferencesGroup(title="Add target")
        self._name_entry = Adw.EntryRow(title="Name (catalog, e.g. 'M 42')")
        self._ra_entry = Adw.EntryRow(title="RA (hours: '5.5881' or '5h35m19s')")
        self._dec_entry = Adw.EntryRow(title="Dec (degrees: '-5.39' or '-5°23′28″')")
        group.add(self._name_entry)
        group.add(self._ra_entry)
        group.add(self._dec_entry)
        self.append(group)

        # --- add area: mosaic / exposure / gain ---------------------------------
        # A FlowBox (not a Box: GTK4 boxes do not wrap) so the controls wrap
        # onto several lines on narrow windows — the row's minimum width is
        # one control, not the sum, keeping the window's minimum size small.
        # The FlowBox MUST keep the default halign=FILL: with halign=CENTER
        # the parent Box allocates only the FlowBox's natural width (one
        # control) while the height was negotiated at the full pane width,
        # so the wrapped lines overflow the allocation and the controls end
        # up clipped below the window (off-screen, unreachable).
        controls = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            homogeneous=False,
            min_children_per_line=1,
            max_children_per_line=3,
            row_spacing=8,
            column_spacing=16,
        )
        mosaic_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, valign=Gtk.Align.CENTER)
        mosaic_box.append(Gtk.Label(label="Mosaic", valign=Gtk.Align.CENTER))
        self._mosaic_switch = Gtk.Switch(valign=Gtk.Align.CENTER, tooltip_text="Mosaic framing on/off")
        self._mosaic_switch.connect("notify::active", self._on_mosaic_toggled)
        mosaic_box.append(self._mosaic_switch)
        self._pan_spin = self._make_spin(MOSAIC_MIN_DEG, MOSAIC_MAX_DEG, 0.0, digits=1)
        self._tilt_spin = self._make_spin(MOSAIC_MIN_DEG, MOSAIC_MAX_DEG, 0.0, digits=1)
        for spin, label_text in ((self._pan_spin, "pan°"), (self._tilt_spin, "tilt°")):
            mosaic_box.append(Gtk.Label(label=label_text, valign=Gtk.Align.CENTER))
            mosaic_box.append(spin)
        self._pan_spin.set_sensitive(False)
        self._tilt_spin.set_sensitive(False)
        controls.append(mosaic_box)
        self._exposure_spin = self._make_spin(
            EXPOSURE_MIN_S, EXPOSURE_MAX_S, DEFAULT_EXPOSURE_S, digits=1
        )
        self._gain_spin = self._make_spin(GAIN_MIN, GAIN_MAX, DEFAULT_GAIN, digits=0)
        for spin, label_text in ((self._exposure_spin, "Exposure s"), (self._gain_spin, "Gain")):
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, valign=Gtk.Align.CENTER)
            box.append(Gtk.Label(label=label_text, valign=Gtk.Align.CENTER))
            box.append(spin)
            controls.append(box)
        self.append(controls)

        # --- add area: Resolve + Add buttons + inline status --------------------
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.CENTER)
        self._resolve_button = Gtk.Button(label="Resolve", tooltip_text="Resolve the name via the catalog")
        self._resolve_button.connect("clicked", self._on_resolve_clicked)
        self._add_button = Gtk.Button(label="Add to queue", css_classes=["suggested-action"])
        self._add_button.connect("clicked", self._on_add_clicked)
        buttons.append(self._resolve_button)
        buttons.append(self._add_button)
        self.append(buttons)
        self._status_label = Gtk.Label(label="", wrap=True, halign=Gtk.Align.CENTER)
        self.append(self._status_label)

        self._controller.add_listener(self._on_queue_changed)
        self._rebuild()

    # -- construction helpers -----------------------------------------------------

    @staticmethod
    def _make_spin(lower: float, upper: float, value: float, *, digits: int) -> Gtk.SpinButton:
        adjustment = Gtk.Adjustment(
            value=value,
            lower=lower,
            upper=upper,
            step_increment=1 if digits == 0 else 0.5,
            page_increment=10 if digits == 0 else 5,
        )
        return Gtk.SpinButton(adjustment=adjustment, digits=digits, valign=Gtk.Align.CENTER, numeric=True)

    # -- public helpers (also used by the widget tests) --------------------------------

    @property
    def controller(self) -> PlanQueueController:
        """The pure-layer plan-queue controller backing this editor."""
        return self._controller

    @property
    def list_box(self) -> Gtk.ListBox:
        """The entry list."""
        return self._list_box

    @property
    def name_entry(self) -> Adw.EntryRow:
        return self._name_entry

    @property
    def ra_entry(self) -> Adw.EntryRow:
        return self._ra_entry

    @property
    def dec_entry(self) -> Adw.EntryRow:
        return self._dec_entry

    @property
    def mosaic_switch(self) -> Gtk.Switch:
        return self._mosaic_switch

    @property
    def pan_spin(self) -> Gtk.SpinButton:
        return self._pan_spin

    @property
    def tilt_spin(self) -> Gtk.SpinButton:
        return self._tilt_spin

    @property
    def exposure_spin(self) -> Gtk.SpinButton:
        return self._exposure_spin

    @property
    def gain_spin(self) -> Gtk.SpinButton:
        return self._gain_spin

    @property
    def resolve_button(self) -> Gtk.Button:
        return self._resolve_button

    @property
    def add_button(self) -> Gtk.Button:
        return self._add_button

    @property
    def status_label(self) -> Gtk.Label:
        return self._status_label

    def row_count(self) -> int:
        """The number of entry rows currently rendered."""
        return len(self._row_buttons)

    def row_label(self, index: int) -> str:
        """The label text of the row at ``index``."""
        row = self._list_box.get_row_at_index(index)
        assert row is not None
        box = row.get_child()
        label = box.get_first_child()
        return label.get_label()

    def click_row_button(self, index: int, key: str) -> None:
        """Click the per-row button ``key`` ('up'/'down'/'duplicate'/'remove')
        of the row at ``index``."""
        self._row_buttons[index][key].emit("clicked")

    # -- rendering ----------------------------------------------------------------------

    def _on_queue_changed(self, event) -> None:
        """Controller listener: the queue changed -> re-render the list."""
        self._rebuild()

    def _rebuild(self) -> None:
        """Re-render the entry list from the controller's snapshot."""
        while (row := self._list_box.get_first_child()) is not None:
            self._list_box.remove(row)
        self._row_buttons = []
        for index, entry in enumerate(self._controller.entries):
            self._list_box.append(self._make_row(index, entry))

    def _make_row(self, index: int, entry) -> Gtk.ListBoxRow:
        box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
            margin_top=6,
            margin_bottom=6,
            margin_start=12,
            margin_end=12,
        )
        label = Gtk.Label(
            label=entry.describe(),
            xalign=0.0,
            hexpand=True,
            ellipsize=Pango.EllipsizeMode.END,
            valign=Gtk.Align.CENTER,
        )
        box.append(label)
        buttons: dict[str, Gtk.Button] = {}
        ops = {
            "up": lambda i=index: self._controller.move_up(i),
            "down": lambda i=index: self._controller.move_down(i),
            "duplicate": lambda i=index: self._controller.duplicate(i),
            "remove": lambda i=index: self._controller.remove(i),
        }
        for key, icon, tooltip in _ROW_BUTTONS:
            button = Gtk.Button(icon_name=icon, tooltip_text=tooltip, valign=Gtk.Align.CENTER)
            button.connect("clicked", lambda _btn, op=ops[key]: op())
            box.append(button)
            buttons[key] = button
        self._row_buttons.append(buttons)
        row = Gtk.ListBoxRow(child=box, activatable=False)
        row.queue_index = index  # type: ignore[attr-defined]
        return row

    # -- add area ------------------------------------------------------------------------

    def _on_mosaic_toggled(self, *_args) -> None:
        active = self._mosaic_switch.get_active()
        self._pan_spin.set_sensitive(active)
        self._tilt_spin.set_sensitive(active)

    def _current_mosaic(self):
        if not self._mosaic_switch.get_active():
            return False
        return (self._pan_spin.get_value(), self._tilt_spin.get_value())

    def _set_status(self, text: str, *, error: bool) -> None:
        self._status_label.set_label(text)
        for css_class in ("error", "success", "dim-label"):
            self._status_label.remove_css_class(css_class)
        if error:
            self._status_label.add_css_class("error")
        elif text:
            self._status_label.add_css_class("success")
        else:
            self._status_label.add_css_class("dim-label")

    def _on_resolve_clicked(self, *_args) -> None:
        """Resolve button: look the name up via the controller (catalog) and
        show the result — or the error — inline."""
        name = self._name_entry.get_text().strip()
        if not name:
            self._resolved = None
            self._set_status("enter a name to resolve first", error=True)
            return
        try:
            coord = self._controller.resolve_name(name)
        except QueueError as exc:
            self._resolved = None
            self._set_status(str(exc), error=True)
            return
        self._resolved = (name, coord)
        self._set_status(
            f"{name} → RA {coord.ra_hours:.4f}h Dec {coord.dec_deg:+.4f}°", error=False
        )

    def _on_add_clicked(self, *_args) -> None:
        """Add button: name XOR RA/Dec -> add through the controller; inline
        errors on validation failure (nothing is added then)."""
        name = self._name_entry.get_text().strip()
        ra = self._ra_entry.get_text().strip()
        dec = self._dec_entry.get_text().strip()
        mosaic = self._current_mosaic()
        exposure_s = self._exposure_spin.get_value()
        gain = int(self._gain_spin.get_value())
        try:
            if name and (ra or dec):
                raise QueueError("give either a name or RA/Dec, not both")
            if ra or dec:
                if not (ra and dec):
                    raise QueueError("RA and Dec must be given together")
                entry = self._controller.add_by_coords(
                    ra, dec, mosaic=mosaic, exposure_s=exposure_s, gain=gain
                )
                assert entry.coord is not None
                self._set_status(
                    f"added RA {entry.coord.ra_hours:.4f}h Dec {entry.coord.dec_deg:+.4f}°",
                    error=False,
                )
            elif name:
                if self._resolved is not None and self._resolved[0] == name:
                    entry = self._controller.add_resolved(
                        name,
                        self._resolved[1],
                        mosaic=mosaic,
                        exposure_s=exposure_s,
                        gain=gain,
                    )
                    self._set_status(f"added {entry.describe()}", error=False)
                else:
                    entry = self._controller.add_by_name(
                        name, mosaic=mosaic, exposure_s=exposure_s, gain=gain
                    )
                    self._set_status(
                        f"added {name} (unresolved — Resolve looks it up in the catalog)",
                        error=False,
                    )
            else:
                raise QueueError("enter a target name or RA/Dec first")
        except QueueError as exc:
            self._set_status(str(exc), error=True)
            return
        # success: clear the target fields (parameters stay for the next entry)
        self._name_entry.set_text("")
        self._ra_entry.set_text("")
        self._dec_entry.set_text("")
        self._resolved = None
