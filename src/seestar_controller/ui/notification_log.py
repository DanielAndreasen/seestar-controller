"""Notification log dialog — the WIDGET layer for ui-shell ticket 04 (part 2).

This module imports ``gi`` at module top (it is a widget module, the only
place allowed to): it is never imported by the pure layer
(:mod:`seestar_controller.ui.notifications`) or eagerly by the package
``__init__`` — the pure layer stays gi-free and headless-testable (see
docs/wiki/assumptions.md: the sandbox has no GTK).

On the desktop the system packages are required (PyGObject is NOT a pip
dependency — it needs the system GTK headers to build)::

    sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1

libadwaita >= 1.5 is assumed (``Adw.Dialog``; Linux Mint 22.3 ships 1.5).

:class:`NotificationLogDialog` is the reviewable log behind the toasts: an
:class:`Adw.Dialog` listing the pure
:class:`~seestar_controller.ui.notifications.NotificationBus`'s
:meth:`~...notifications.NotificationBus.log` entries — time, severity,
source, title, message — newest first, with a **Clear** button that empties
the bus log. It is opened ONLY by the user (the header-bar notifications
button / a toast's "View" button) — background events never open it (no
modal dialogs for background events, spec user story 8).
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk, Pango  # noqa: E402

from .notifications import Notification, NotificationBus, NotificationSeverity  # noqa: E402


class NotificationLogDialog(Adw.Dialog):
    """The notification log: one row per bus entry (newest first) + Clear.

    Constructed with the pure :class:`~.notifications.NotificationBus`;
    present it with ``dialog.present(window)``. :meth:`refresh` rebuilds the
    rows from :meth:`~...notifications.NotificationBus.log` (call it when the
    dialog is opened; the Clear button refreshes too).
    """

    def __init__(self, bus: NotificationBus, **kwargs) -> None:
        kwargs.setdefault("title", "Notifications")
        kwargs.setdefault("content_width", 600)
        kwargs.setdefault("content_height", 420)
        super().__init__(**kwargs)
        self._bus = bus
        self._rows: list[Gtk.ListBoxRow] = []

        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self._clear_button = Gtk.Button(label="Clear", tooltip_text="Clear the notification log")
        self._clear_button.connect("clicked", self._on_clear_clicked)
        header.pack_start(self._clear_button)
        close_button = Gtk.Button(label="Close")
        close_button.connect("clicked", lambda *_args: self.close())
        header.pack_end(close_button)
        toolbar_view.add_top_bar(header)

        self._listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        scrolled = Gtk.ScrolledWindow(
            child=self._listbox,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            vexpand=True,
        )
        toolbar_view.set_content(scrolled)
        self.set_child(toolbar_view)

        self.refresh()

    # -- public helpers (also used by the widget tests) ------------------------

    @property
    def bus(self) -> NotificationBus:
        """The pure bus this dialog lists."""
        return self._bus

    @property
    def clear_button(self) -> Gtk.Button:
        """The Clear button (empties the bus log)."""
        return self._clear_button

    def rows(self) -> tuple[Gtk.ListBoxRow, ...]:
        """The current rows, newest notification first."""
        return tuple(self._rows)

    def row_count(self) -> int:
        """The number of listed notifications."""
        return len(self._rows)

    def refresh(self) -> None:
        """Rebuild the rows from :meth:`~...notifications.NotificationBus.log`."""
        for row in self._rows:
            self._listbox.remove(row)
        self._rows = []
        for notification in self._bus.log():
            row = self._build_row(notification)
            self._rows.append(row)
            self._listbox.append(row)

    # -- internals ---------------------------------------------------------------

    def _build_row(self, notification: Notification) -> Gtk.ListBoxRow:
        """One log row: a dim caption line (time · severity · source) over the
        title + message (error/warning css class by severity)."""
        when = notification.timestamp.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        head = Gtk.Label(
            label=f"{when}  ·  {notification.severity.value.upper()}  ·  {notification.source}",
            xalign=0.0,
            css_classes=["dim-label", "caption"],
        )
        text = (
            f"{notification.title}: {notification.message}"
            if notification.message
            else notification.title
        )
        body = Gtk.Label(
            label=text,
            xalign=0.0,
            wrap=True,
            wrap_mode=Pango.WrapMode.WORD_CHAR,
            ellipsize=Pango.EllipsizeMode.END,
        )
        if notification.severity == NotificationSeverity.ERROR:
            body.add_css_class("error")
        elif notification.severity == NotificationSeverity.WARNING:
            body.add_css_class("warning")
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=2,
            margin_top=6,
            margin_bottom=6,
            margin_start=12,
            margin_end=12,
        )
        box.append(head)
        box.append(body)
        row = Gtk.ListBoxRow(child=box, activatable=False)
        row.notification = notification  # type: ignore[attr-defined]  # test hook
        return row

    def _on_clear_clicked(self, *_args) -> None:
        """Clear button: empty the bus log and refresh the rows."""
        self._bus.clear()
        self.refresh()
