"""Session log dialog — the WIDGET layer for observing ticket 04.

This module imports ``gi`` at module top (it is a widget module, the only
place allowed to): it is never imported by the pure/executor layer
(:mod:`seestar_controller.observing.capture`) or eagerly by the package
``__init__`` — the package stays gi-free and headless-testable (see
docs/wiki/assumptions.md: the sandbox has no GTK).

On the desktop the system packages are required (PyGObject is NOT a pip
dependency — it needs the system GTK headers to build)::

    sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1

libadwaita >= 1.5 is assumed (``Adw.Dialog``; Linux Mint 22.3 ships 1.5).

:class:`SessionLogDialog` is the per-session log view: an :class:`Adw.Dialog`
listing the :class:`~seestar_controller.observing.capture.SessionCapture`'s
event log (goto / stack / errors / frames stored, with timestamps, oldest
first) plus a summary line with the session id and the frames stored in the
session. It is opened from the Observe view's "Session log" button and
refreshes itself while open (it registers as a log listener).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk, Pango

from seestar_controller.link.frames import KIND_STACK, KIND_SUB
from seestar_controller.store.frames import list_frames

from .capture import KIND_ERROR, LogEvent, SessionCapture

logger = logging.getLogger(__name__)


class SessionLogDialog(Adw.Dialog):
    """The per-session log: a summary line (session, events, frames stored)
    over one row per :class:`~.capture.LogEvent` (oldest first).

    Constructed with the pure :class:`~.capture.SessionCapture`; present it
    with ``dialog.present(window)``. :meth:`refresh` rebuilds the summary +
    rows (the Refresh button, and automatically on every new log event while
    the dialog is open).
    """

    def __init__(self, capture: SessionCapture, **kwargs) -> None:
        kwargs.setdefault("title", f"Session log — {capture.session_id}")
        kwargs.setdefault("content_width", 640)
        kwargs.setdefault("content_height", 480)
        super().__init__(**kwargs)
        self._capture = capture
        self._rows: list[Gtk.ListBoxRow] = []
        self._row_texts: list[str] = []

        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self._refresh_button = Gtk.Button(
            label="Refresh", tooltip_text="Reload the event log and the frames summary"
        )
        self._refresh_button.connect("clicked", lambda *_args: self.refresh())
        header.pack_start(self._refresh_button)
        close_button = Gtk.Button(label="Close")
        close_button.connect("clicked", lambda *_args: self.close())
        header.pack_end(close_button)
        toolbar_view.add_top_bar(header)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._summary_label = Gtk.Label(
            label="",
            xalign=0.0,
            wrap=True,
            margin_top=8,
            margin_bottom=8,
            margin_start=12,
            margin_end=12,
            css_classes=["dim-label"],
        )
        content.append(self._summary_label)
        self._listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        scrolled = Gtk.ScrolledWindow(
            child=self._listbox,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            vexpand=True,
        )
        content.append(scrolled)
        toolbar_view.set_content(content)
        self.set_child(toolbar_view)

        capture.add_listener(self._on_event)
        self.connect("closed", self._on_closed)
        self.refresh()

    # -- public helpers (also used by the widget tests) ------------------------

    @property
    def capture(self) -> SessionCapture:
        """The capture this dialog lists."""
        return self._capture

    @property
    def refresh_button(self) -> Gtk.Button:
        """The Refresh button."""
        return self._refresh_button

    @property
    def summary_label(self) -> Gtk.Label:
        """The summary line (session, events, frames stored)."""
        return self._summary_label

    def rows(self) -> tuple[Gtk.ListBoxRow, ...]:
        """The current event rows, oldest first."""
        return tuple(self._rows)

    def row_count(self) -> int:
        """The number of listed events."""
        return len(self._rows)

    def row_label(self, index: int) -> str:
        """The rendered text of row ``index`` (head caption + body)."""
        return self._row_texts[index]

    def refresh(self) -> None:
        """Rebuild the summary + rows from the capture's log and the
        session's stored frames."""
        self._refresh_summary()
        for row in self._rows:
            self._listbox.remove(row)
        self._rows = []
        self._row_texts = []
        for event in self._capture.events():
            row, text = self._build_row(event)
            self._rows.append(row)
            self._row_texts.append(text)
            self._listbox.append(row)

    # -- internals ---------------------------------------------------------------

    def _refresh_summary(self) -> None:
        """The summary line: session id, event count, frames stored in the
        session (best-effort — a store problem must not break the dialog)."""
        capture = self._capture
        try:
            frames = list_frames(capture.project, capture.session_id)
            subs = sum(1 for frame in frames if frame.kind == KIND_SUB)
            stacks = sum(1 for frame in frames if frame.kind == KIND_STACK)
            frames_text = (
                f"{len(frames)} frame(s) stored ({subs} sub, {stacks} stack)"
            )
        except Exception:
            logger.debug("could not list the session frames", exc_info=True)
            frames_text = "frames unavailable"
        self._summary_label.set_label(
            f"session {capture.session_id} — {len(capture.events())} event(s), {frames_text}"
        )

    def _build_row(self, event: LogEvent) -> tuple[Gtk.ListBoxRow, str]:
        """One log row: a dim caption line (time · kind · source) over the
        detail (``error`` css class for error events)."""
        when = datetime.fromtimestamp(event.ts, UTC).astimezone()
        head = Gtk.Label(
            label=f"{when.strftime('%Y-%m-%d %H:%M:%S')}  ·  {event.kind}  ·  {event.source}",
            xalign=0.0,
            css_classes=["dim-label", "caption"],
        )
        body = Gtk.Label(
            label=event.detail,
            xalign=0.0,
            wrap=True,
            wrap_mode=Pango.WrapMode.WORD_CHAR,
        )
        if event.kind == KIND_ERROR:
            body.add_css_class("error")
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
        row.event = event  # type: ignore[attr-defined]  # test hook
        return row, f"{head.get_label()} | {body.get_label()}"

    def _on_event(self, event: LogEvent) -> None:
        """A new log event while the dialog is open: refresh."""
        self.refresh()

    def _on_closed(self, *_args) -> None:
        """The dialog was closed: stop listening to the log."""
        self._capture.remove_listener(self._on_event)
