"""Toast presenter — the WIDGET layer for ui-shell ticket 04 (part 1).

This module imports ``gi`` at module top (it is a widget module, the only
place allowed to): it is never imported by the pure layer
(:mod:`seestar_controller.ui.notifications` / :mod:`.connection` / …) or
eagerly by the package ``__init__`` — the pure layer stays gi-free and
headless-testable (see docs/wiki/assumptions.md: the sandbox has no GTK).

On the desktop the system packages are required (PyGObject is NOT a pip
dependency — it needs the system GTK headers to build)::

    sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1

:class:`NotificationToasts` bridges the pure
:class:`~seestar_controller.ui.notifications.NotificationBus` to an
:class:`Adw.ToastOverlay`: every bus notification becomes a **non-modal**
:class:`Adw.Toast` (spec user story 8 — background events never open modal
dialogs; the reviewable log is :mod:`.notification_log`, opened only by the
user). Bus callbacks may arrive from worker threads (device event handlers
run on the link's reader thread), so the toast is created through the
``invoke_on_main`` callable — the app binds it to ``GLib.idle_add``, the
same pattern as :mod:`seestar_controller.ui.connection`.

Toasts auto-dismiss after a severity-based timeout (:data:`TOAST_TIMEOUTS`
— errors stay the longest, Adw's default is 5 s) and carry a "View" button
that opens the notification log (``on_open_log``). The overlay is
duck-typed (anything with ``add_toast``), so tests can pass a fake; the
created toasts are also recorded in :attr:`NotificationToasts.toasts` as a
test hook.
"""

from __future__ import annotations

from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw

from .notifications import (
    Notification,
    NotificationBus,
    NotificationSeverity,
)

#: :class:`Adw.Toast` auto-dismiss timeout (seconds) per severity — errors
#: stay longest (documented choice; Adw's own default is 5 s).
TOAST_TIMEOUTS: dict[NotificationSeverity, int] = {
    NotificationSeverity.INFO: 5,
    NotificationSeverity.WARNING: 7,
    NotificationSeverity.ERROR: 10,
}

#: Default toast timeout when the severity is unknown.
DEFAULT_TOAST_TIMEOUT = 5


def toast_title(notification: Notification) -> str:
    """The toast title: ``"<title>: <message>"`` (or just the title)."""
    if notification.message:
        return f"{notification.title}: {notification.message}"
    return notification.title


def _direct_invoke(callback) -> None:
    callback()


class NotificationToasts:
    """Shows pure-bus notifications as non-modal toasts on a ToastOverlay.

    - ``bus`` — the pure :class:`~.notifications.NotificationBus`;
    - ``overlay`` — the :class:`Adw.ToastOverlay` (duck-typed: anything with
      ``add_toast(toast)`` — tests pass a fake);
    - ``invoke_on_main`` — marshals the bus callback onto the main thread
      (the app binds it to ``GLib.idle_add``); default: direct call;
    - ``on_open_log`` — optional callback for the toast's "View" button
      (opens the notification log dialog).

    Use :meth:`detach` to stop listening.
    """

    def __init__(
        self,
        bus: NotificationBus,
        overlay: Any,
        *,
        invoke_on_main=None,
        on_open_log=None,
    ) -> None:
        self._bus = bus
        self._overlay = overlay
        self._invoke_on_main = invoke_on_main or _direct_invoke
        self._on_open_log = on_open_log
        #: test hook: every Adw.Toast created by this presenter, in order
        self.toasts: list[Adw.Toast] = []
        bus.add_listener(self._on_notification)

    @property
    def bus(self) -> NotificationBus:
        """The pure bus this presenter listens to."""
        return self._bus

    @property
    def overlay(self):
        """The toast overlay (duck-typed) toasts are added to."""
        return self._overlay

    def detach(self) -> None:
        """Stop listening on the bus (idempotent)."""
        self._bus.remove_listener(self._on_notification)

    # -- internals ---------------------------------------------------------------

    def _on_notification(self, notification: Notification) -> None:
        """Bus listener — may run on a worker thread; marshal to main."""
        self._invoke_on_main(lambda: self._show(notification))

    def _show(self, notification: Notification) -> None:
        """Create the toast and add it to the overlay (main thread)."""
        toast = Adw.Toast(
            title=toast_title(notification),
            timeout=TOAST_TIMEOUTS.get(notification.severity, DEFAULT_TOAST_TIMEOUT),
        )
        on_open_log = self._on_open_log
        if on_open_log is not None:
            toast.set_button_label("View")
            toast.connect("button-clicked", lambda *_args: on_open_log())
        self.toasts.append(toast)
        self._overlay.add_toast(toast)
