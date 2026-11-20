"""Stale-session dialog — the WIDGET layer for observing ticket 05.

This module imports ``gi`` at module top (it is a widget module, the only
place allowed to): it is never imported by the pure/executor layer
(:mod:`seestar_controller.observing.observe_controller` /
:mod:`.executor`) or eagerly by the package ``__init__`` — the package stays
gi-free and headless-testable (see docs/wiki/assumptions.md: the sandbox has
no GTK).

On the desktop the system packages are required (PyGObject is NOT a pip
dependency — it needs the system GTK headers to build)::

    sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1

libadwaita >= 1.5 is assumed (``Adw.Dialog``; Linux Mint 22.3 ships 1.5).

:class:`StaleSessionDialog` is the resume/close offer shown when "Start
night" hits a stale open session: the pure
:class:`~seestar_controller.observing.observe_controller.ObserveController`
raises :class:`~seestar_controller.observing.observe_controller.StaleSessionError`
(a :class:`~seestar_controller.store.errors.SessionOpenError`) and the Observe
view presents this dialog instead of surfacing the raw error. The message is
"Session <id> from <time> is still open — the app was closed before the night
ended. Resume it, or close it and start a new one?" with two buttons:

- **Resume** — ``on_resume`` callback (the view calls the controller's
  ``resume_stale_session``, reattaching the executor to the open session);
- **Close and start new** — ``on_close_and_start_new`` callback (the view
  calls the controller's ``close_and_start_new``, which closes the session via
  :func:`seestar_controller.store.sessions.close_session` and starts fresh).

The two actions are plain injected callbacks, so the dialog itself stays a
dumb widget (headless-testable through the Observe view's widget harness).
"""

from __future__ import annotations

from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk

from seestar_controller.store.sessions import Session

from .observe_controller import format_session_time

#: Response id of the Resume button.
RESPONSE_RESUME = "resume"
#: Response id of the Close-and-start-new button.
RESPONSE_CLOSE_AND_START_NEW = "close_and_start_new"


class StaleSessionDialog(Adw.Dialog):
    """The stale-session offer: "Session <id> from <time> is still open ..."
    with **Resume** / **Close and start new** buttons.

    Constructed with the open :class:`~seestar_controller.store.sessions.Session`
    record and the two action callbacks; present it with
    ``dialog.present(window)``. A button closes the dialog and then invokes
    its callback (the Observe view performs the controller action).
    """

    def __init__(
        self,
        session: Session,
        *,
        on_resume: Callable[[], None],
        on_close_and_start_new: Callable[[], None],
        **kwargs,
    ) -> None:
        kwargs.setdefault("title", "Session still open")
        kwargs.setdefault("content_width", 480)
        super().__init__(**kwargs)
        self._session = session
        self._on_resume = on_resume
        self._on_close_and_start_new = on_close_and_start_new

        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(Adw.HeaderBar())

        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=16,
            margin_top=18,
            margin_bottom=18,
            margin_start=18,
            margin_end=18,
        )
        self._message_label = Gtk.Label(
            label=self.build_message(),
            wrap=True,
            xalign=0.0,
            justify=Gtk.Justification.LEFT,
        )
        content.append(self._message_label)

        buttons = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.END
        )
        self._close_button = Gtk.Button(
            label="Close and start new",
            tooltip_text=(
                "Close the open session (store close_session) and start a fresh night"
            ),
        )
        self._close_button.connect("clicked", self._on_close_clicked)
        self._resume_button = Gtk.Button(
            label="Resume",
            css_classes=["suggested-action"],
            tooltip_text="Reattach to the open session and continue the night",
        )
        self._resume_button.connect("clicked", self._on_resume_clicked)
        buttons.append(self._close_button)
        buttons.append(self._resume_button)
        content.append(buttons)

        toolbar_view.set_content(content)
        self.set_child(toolbar_view)

    # -- public helpers (also used by the widget tests) ----------------------------

    @property
    def session(self) -> Session:
        """The stale open session this dialog offers to resume/close."""
        return self._session

    @property
    def message_label(self) -> Gtk.Label:
        """The message body (session id + start time + the offer)."""
        return self._message_label

    @property
    def resume_button(self) -> Gtk.Button:
        """The **Resume** button."""
        return self._resume_button

    @property
    def close_button(self) -> Gtk.Button:
        """The **Close and start new** button."""
        return self._close_button

    def build_message(self) -> str:
        """The dialog body: "Session <id> from <time> is still open — the app
        was closed before the night ended. Resume it, or close it and start a
        new one?"."""
        return (
            f"Session {self._session.id} from "
            f"{format_session_time(self._session.started_at)} is still open — "
            "the app was closed before the night ended.\n\n"
            "Resume it, or close it and start a new one?"
        )

    # -- button handlers -------------------------------------------------------------

    def _on_resume_clicked(self, *_args) -> None:
        """Resume: close the dialog, then run the injected ``on_resume``."""
        self.close()
        self._on_resume()

    def _on_close_clicked(self, *_args) -> None:
        """Close-and-start-new: close the dialog, then run the injected
        ``on_close_and_start_new``."""
        self.close()
        self._on_close_and_start_new()
