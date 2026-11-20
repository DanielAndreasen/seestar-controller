"""Connection panel widget — the WIDGET layer for ui-shell ticket 03.

This module imports ``gi`` at module top (it is a widget module, the only
place allowed to): it is never imported by the pure layer
(:mod:`seestar_controller.ui.state` / :mod:`.settings` / :mod:`.connection`)
or eagerly by the package ``__init__`` — the pure layer stays gi-free and
headless-testable (see docs/wiki/assumptions.md: the sandbox has no GTK).

On the desktop the system packages are required (PyGObject is NOT a pip
dependency — it needs the system GTK headers to build)::

    sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1

Layout: a horizontal :class:`Gtk.Box` packed into the header bar
(``pack_start``, left of the title) holding:

- a :class:`Gtk.Button` with an :class:`Adw.ButtonContent` (label + icon)
  that acts as Connect / Disconnect and reflects the state:

  =================  =========================  ==========================
  state              button                     styling
  =================  =========================  ==========================
  ``disconnected``   "Connect"                  default, sensitive
  ``connecting``     "Connecting…"              insensitive
  ``connected``      "Disconnect"               ``suggested-action``, sensitive
  ``reconnecting``   "Reconnecting…"            insensitive
  ``error``          "Retry"                    ``error`` css class, sensitive
  =================  =========================  ==========================

- a status :class:`Gtk.Label` next to it: "Disconnected" /
  "Connecting to <ip>…" / "Connected — mode …, target …" (the Scope's
  current state on a successful connect, formatted by the pure
  :func:`~seestar_controller.ui.connection.describe_scope_state`) /
  "Link lost — reconnecting…" / "Connection error: <message>" (``error``
  css class).

The panel is a thin observer: it registers itself as a listener on the pure
:class:`~seestar_controller.ui.connection.ConnectionController` and re-renders
from each :class:`~...connection.ConnectionSnapshot`. The app binds the
controller's ``invoke_on_main`` to ``GLib.idle_add``, so :meth:`refresh`
always runs on the main thread. Errors surface through the listener
notifications for now — ticket 04 wires the notification bus (banners) on
top of them.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk, Pango  # noqa: E402

from .connection import (  # noqa: E402
    ConnectionController,
    ConnectionSnapshot,
    ConnectionStateError,
    ConnectionStatus,
    describe_scope_state,
)

#: Button label / icon per connection status.
_BUTTON_CONTENT: dict[ConnectionStatus, tuple[str, str]] = {
    ConnectionStatus.DISCONNECTED: ("Connect", "network-wired-symbolic"),
    ConnectionStatus.CONNECTING: ("Connecting…", "content-loading-symbolic"),
    ConnectionStatus.CONNECTED: ("Disconnect", "media-playback-stop-symbolic"),
    ConnectionStatus.RECONNECTING: ("Reconnecting…", "content-loading-symbolic"),
    ConnectionStatus.ERROR: ("Retry", "view-refresh-symbolic"),
}


class ConnectionPanel(Gtk.Box):
    """The header-bar connection panel: Connect/Disconnect button + status
    label, driven by a pure-layer :class:`~.connection.ConnectionController`.

    Constructed with the controller; the app packs it into the header bar.
    """

    def __init__(self, controller: ConnectionController, **kwargs) -> None:
        kwargs.setdefault("orientation", Gtk.Orientation.HORIZONTAL)
        kwargs.setdefault("spacing", 8)
        kwargs.setdefault("valign", Gtk.Align.CENTER)
        super().__init__(**kwargs)
        self._controller = controller

        self._button = Gtk.Button()
        self._button_content = Adw.ButtonContent()
        self._button.set_child(self._button_content)
        self._button.connect("clicked", self._on_clicked)
        self.append(self._button)

        self._status_label = Gtk.Label(xalign=0.0, ellipsize=Pango.EllipsizeMode.END)
        self.append(self._status_label)

        controller.add_listener(self.refresh)
        self.refresh(controller.snapshot)

    # -- public helpers (also used by the widget tests) ------------------------

    @property
    def controller(self) -> ConnectionController:
        """The pure-layer connection controller backing this panel."""
        return self._controller

    @property
    def button(self) -> Gtk.Button:
        """The Connect/Disconnect button."""
        return self._button

    @property
    def status_label(self) -> Gtk.Label:
        """The status label (state text + scope state when connected)."""
        return self._status_label

    def refresh(self, snapshot: ConnectionSnapshot) -> None:
        """Re-render the panel from a :class:`~.connection.ConnectionSnapshot`
        (the controller listener callback — always on the main thread)."""
        status = snapshot.status
        label, icon = _BUTTON_CONTENT[status]
        self._button_content.set_label(label)
        self._button_content.set_icon_name(icon)
        for css_class in ("suggested-action", "error"):
            self._button.remove_css_class(css_class)
        if status == ConnectionStatus.CONNECTED:
            self._button.add_css_class("suggested-action")
        elif status == ConnectionStatus.ERROR:
            self._button.add_css_class("error")
        self._button.set_sensitive(
            status
            in (
                ConnectionStatus.DISCONNECTED,
                ConnectionStatus.CONNECTED,
                ConnectionStatus.ERROR,
            )
        )
        self._button.set_tooltip_text(
            f"Connect to the scope at {snapshot.scope_ip}"
            if status == ConnectionStatus.DISCONNECTED
            else "Disconnect from the scope"
            if status == ConnectionStatus.CONNECTED
            else label
        )

        if status == ConnectionStatus.DISCONNECTED:
            text = "Disconnected"
        elif status == ConnectionStatus.CONNECTING:
            text = f"Connecting to {snapshot.scope_ip}…"
        elif status == ConnectionStatus.CONNECTED:
            text = f"Connected to {snapshot.scope_ip} — {describe_scope_state(snapshot.scope_state)}"
        elif status == ConnectionStatus.RECONNECTING:
            text = "Link lost — reconnecting…"
        else:  # ERROR
            text = f"Connection error: {snapshot.error or 'unknown error'}"
        self._status_label.set_label(text)
        for css_class in ("error", "success", "dim-label"):
            self._status_label.remove_css_class(css_class)
        if status == ConnectionStatus.ERROR:
            self._status_label.add_css_class("error")
        elif status == ConnectionStatus.CONNECTED:
            self._status_label.add_css_class("success")
        else:
            self._status_label.add_css_class("dim-label")

    # -- internals ---------------------------------------------------------------

    def _on_clicked(self, *_args) -> None:
        """Button click: connect from disconnected/error, disconnect otherwise."""
        status = self._controller.status
        if status in (ConnectionStatus.DISCONNECTED, ConnectionStatus.ERROR):
            try:
                self._controller.connect()
            except ConnectionStateError:
                pass  # a racing double-click — the state machine rejects it
        else:
            self._controller.disconnect()
