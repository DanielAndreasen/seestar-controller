"""Settings dialog widget — the WIDGET layer for ui-shell ticket 02.

This module imports ``gi`` at module top (it is a widget module, the only
place allowed to): it is never imported by the pure layer
(:mod:`seestar_controller.ui.state` / :mod:`.settings`) or eagerly by the
package ``__init__`` — the pure layer stays gi-free and headless-testable
(see docs/wiki/assumptions.md: the sandbox has no GTK).

On the desktop the system packages are required (PyGObject is NOT a pip
dependency — it needs the system GTK headers to build)::

    sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1

libadwaita >= 1.5 is assumed (``Adw.Dialog``; Linux Mint 22.3 ships 1.5).

Layout: an :class:`Adw.Dialog` titled "Settings" with an
:class:`Adw.ToolbarView` — an :class:`Adw.HeaderBar` carrying **Cancel**
(left) and **Save** (right, ``suggested-action``) — and an
:class:`Adw.PreferencesPage` content with one :class:`Adw.PreferencesGroup`
holding one :class:`Adw.EntryRow` per EDITABLE settings key (``scope_ip``,
``key_path``, ``project_root``, ``photometry_comp_search_radius_deg``), each
with a validation label as its suffix. The app-managed ``last_project``
setting (projects-view ticket 01 — written by the project controller when a
project is opened) is persisted with the rest of the settings but not shown
as an editable row.

Validation UX (documented choices):

- **Live validation** — every entry ``changed`` re-runs the pure-layer
  :func:`~seestar_controller.ui.settings.validate_settings` /
  :func:`~seestar_controller.ui.settings.flag_settings` over all three
  entries; each row's label shows the hard error (red, ``error`` css class)
  or the soft flag (amber, ``warning`` css class), or hides when clean.
- **Save semantics: the Save button is INSENSITIVE while any hard validation
  error exists** (chosen over blocking the click, so the user immediately
  sees why saving is impossible); soft flags (e.g. a missing PEM key file)
  never block saving. Clicking Save with valid values runs
  :meth:`~seestar_controller.ui.settings.SettingsController.update` (which
  persists atomically and notifies listeners) and closes the dialog;
  :meth:`SettingsDialog.save` returns whether it persisted (also usable
  programmatically / from tests).
- The dialog registers itself as a controller listener, so external settings
  changes refresh the entries while it is open.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk

from .settings import SettingsController, SettingsFieldError
from .state import EDITABLE_SETTINGS_KEYS

#: field key -> entry row title.
FIELD_TITLES: dict[str, str] = {
    "scope_ip": "Scope IP",
    "key_path": "PEM key path",
    "project_root": "Project root",
    "photometry_comp_search_radius_deg": "Comp search radius (deg)",
}


class SettingsDialog(Adw.Dialog):
    """The settings dialog: one entry row per settings key + Save/Cancel.

    Constructed with a :class:`~seestar_controller.ui.settings.SettingsController`
    (the pure layer does the validating/persisting; this widget stays thin).
    Present it with ``dialog.present(window)``.
    """

    def __init__(self, controller: SettingsController, **kwargs) -> None:
        kwargs.setdefault("title", "Settings")
        kwargs.setdefault("content_width", 520)
        super().__init__(**kwargs)
        self._controller = controller
        self._updating = False  # guards the listener -> entry refresh loop

        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        cancel_button = Gtk.Button(label="Cancel")
        cancel_button.connect("clicked", lambda *_args: self.close())
        self._save_button = Gtk.Button(label="Save")
        self._save_button.add_css_class("suggested-action")
        self._save_button.connect("clicked", self._on_save_clicked)
        header.pack_start(cancel_button)
        header.pack_end(self._save_button)
        toolbar_view.add_top_bar(header)

        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(
            title="Connection, storage & photometry",
            description=(
                "Scope address, PEM key, project root, and the photometry "
                "comp-search radius — saved to the app config dir and reloaded "
                "on start."
            ),
        )
        self._rows: dict[str, Adw.EntryRow] = {}
        self._error_labels: dict[str, Gtk.Label] = {}
        for key in EDITABLE_SETTINGS_KEYS:
            row = Adw.EntryRow(title=FIELD_TITLES[key])
            row.set_text(controller.values.get(key, ""))
            error_label = Gtk.Label(
                label="",
                visible=False,
                halign=Gtk.Align.END,
                wrap=True,
                max_width_chars=42,
            )
            row.add_suffix(error_label)
            row.connect("changed", self._on_entry_changed)
            group.add(row)
            self._rows[key] = row
            self._error_labels[key] = error_label
        page.add(group)
        toolbar_view.set_content(page)
        self.set_child(toolbar_view)

        # External settings changes refresh the entries while the dialog is open.
        controller.add_listener(self._on_settings_changed)
        self.refresh_validation()

    # -- public helpers (also used by the widget tests) ------------------------

    @property
    def controller(self) -> SettingsController:
        """The pure-layer settings controller backing this dialog."""
        return self._controller

    @property
    def save_button(self) -> Gtk.Button:
        """The Save button (insensitive while invalid — documented choice)."""
        return self._save_button

    def entry_for(self, key: str) -> Adw.EntryRow:
        """The entry row for a settings ``key``."""
        return self._rows[key]

    def error_label_for(self, key: str) -> Gtk.Label:
        """The validation label for a settings ``key``."""
        return self._error_labels[key]

    def values(self) -> dict[str, str]:
        """The current entry texts as a settings dict."""
        return {key: self._rows[key].get_text() for key in EDITABLE_SETTINGS_KEYS}

    def set_field(self, key: str, value: str) -> None:
        """Set one entry's text (triggers live re-validation via ``changed``)."""
        self._rows[key].set_text(value)

    def save_sensitive(self) -> bool:
        """Whether Save is currently enabled (no hard validation errors)."""
        return self._save_button.get_sensitive()

    def refresh_validation(self) -> None:
        """Re-run live validation: update the error labels + Save sensitivity."""
        values = self.values()
        errors = self._controller.validate(values)
        flags = self._controller.flags(values)
        for key in EDITABLE_SETTINGS_KEYS:
            label = self._error_labels[key]
            if key in errors:
                label.set_label(errors[key])
                label.set_visible(True)
                label.remove_css_class("warning")
                label.add_css_class("error")
            elif key in flags:
                label.set_label(flags[key])
                label.set_visible(True)
                label.remove_css_class("error")
                label.add_css_class("warning")
            else:
                label.set_label("")
                label.set_visible(False)
                label.remove_css_class("error")
                label.remove_css_class("warning")
        self._save_button.set_sensitive(not errors)

    def save(self) -> bool:
        """Attempt to save; returns True when the values were persisted.

        Blocked (returns False, nothing persisted) while any hard validation
        error exists — the Save button is also insensitive then (documented
        choice). On success the controller persists + notifies and the dialog
        closes.
        """
        values = self.values()
        if self._controller.validate(values):
            self.refresh_validation()
            return False
        try:
            self._controller.update(**values)
        except SettingsFieldError:
            self.refresh_validation()
            return False
        self.close()
        return True

    # -- internals ---------------------------------------------------------------

    def _on_entry_changed(self, *_args) -> None:
        self.refresh_validation()

    def _on_save_clicked(self, *_args) -> None:
        self.save()

    def _on_settings_changed(self, values: dict[str, str]) -> None:
        if self._updating:
            return
        self._updating = True
        try:
            for key in EDITABLE_SETTINGS_KEYS:
                text = values.get(key, "")
                if self._rows[key].get_text() != text:
                    self._rows[key].set_text(text)  # fires changed -> refresh
            self.refresh_validation()
        finally:
            self._updating = False
