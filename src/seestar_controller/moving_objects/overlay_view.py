"""The mover overlay/blink view widget — the WIDGET layer of moving-objects
issue 02 (:mod:`seestar_controller.moving_objects.overlay_view`).

This module imports ``gi`` at module top (it is a widget module, the only
place allowed to): it is never imported by the pure layer
(:mod:`seestar_controller.moving_objects.overlay`) or eagerly by the package
``__init__`` — the pure layer stays gi-free and headless-testable (see
docs/wiki/assumptions.md: the sandbox has no GTK). It is re-exported as
``moving_objects.MoverOverlayView`` lazily (PEP 562 ``__getattr__``).

On the desktop the system packages are required (PyGObject is NOT a pip
dependency — it needs the system GTK headers to build)::

    sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1

:class:`MoverOverlayView` is a thin observer over the pure
:class:`~seestar_controller.moving_objects.overlay.MoverController` (the app
owns one shared instance; the window passes it down via
:func:`seestar_controller.ui.views.build_view` to the Photometry view, which
embeds this widget below its results panel):

- a **header row** — heading, a **session selector** (:class:`Gtk.DropDown`
  fed from the controller's sessions, the same pattern as the frame-quality
  view's session chooser), a **Refresh** button, a **Detect movers** button
  (runs the detect -> link -> flag -> persist pipeline on a worker thread)
  and an **MPC lookup** button (the cached MPC query, also on a worker
  thread);
- a **movers list** (:class:`Gtk.ListBox`) — one row per flagged mover:
  track id, speed, direction, and the MPC designation when matched;
- the **annotated frame** (:class:`Gtk.Picture`, PIL -> PNG bytes ->
  :class:`Gdk.Texture`, the same pattern as the Enhance view's preview):
  the current sub tone-mapped with every track drawn as a polyline, the
  current frame's detections ringed, and each mover labelled;
- **frame controls** — Prev / Next (wrap around) and a **Blink** toggle
  (a ``GLib.timeout_add`` stepping through the frames) plus a frame label;
- a **status label** (mover/MPC counts, or a clean error message).

The view re-renders on every frozen
:class:`~seestar_controller.moving_objects.overlay.MoverSnapshot`
(listener notifications marshalled onto the GLib main loop when they come
from a worker thread).
"""

from __future__ import annotations

import io
import threading
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Gdk, GLib, Gtk
from PIL import Image

from .overlay import MoverController, MoverSnapshot

#: The blink step interval, in milliseconds.
BLINK_INTERVAL_MS = 500


def texture_from_pil(image: Image.Image) -> Gdk.Texture:
    """Build a :class:`Gdk.Texture` from a PIL image (PNG bytes ->
    ``Gdk.Texture.new_from_bytes`` — the same pattern as the Enhance view's
    preview)."""
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return Gdk.Texture.new_from_bytes(GLib.Bytes(buffer.getvalue()))


def _mover_row_label(mover: Any) -> str:
    """The movers-list row text of one mover."""
    label = (
        f"T{mover.track_id} — {mover.speed_px_per_frame:.1f} px/f, "
        f"{mover.direction_deg:.0f}\u00b0, {mover.n_frames} frames"
    )
    if mover.mpc_match is not None:
        name = f" ({mover.mpc_match.name})" if mover.mpc_match.name else ""
        label += f"  = {mover.mpc_match.designation}{name}"
    return label


class MoverOverlayView(Gtk.Box):
    """The mover overlay/blink view: session selector + Detect/MPC buttons on
    top, the movers list next to the annotated frame, frame controls (Prev /
    Blink / Next) and a status label — observing a
    :class:`~seestar_controller.moving_objects.overlay.MoverController`.

    Constructed with the shared mover controller (the app passes it; a fresh
    standalone one is created when omitted, so the widget also works alone).
    """

    def __init__(
        self,
        mover_controller: MoverController | None = None,
        **kwargs,
    ) -> None:
        kwargs.setdefault("orientation", Gtk.Orientation.VERTICAL)
        kwargs.setdefault("spacing", 6)
        super().__init__(**kwargs)
        if mover_controller is None:
            mover_controller = MoverController()
        self._controller = mover_controller
        self._main_thread_id = threading.get_ident()
        self._session_ids: list[str] = []
        self._blink_source: int | None = None
        self._busy = False

        # header row: heading + session selector + Refresh + Detect + MPC
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.append(Gtk.Label(label="Movers", xalign=0.0, css_classes=["heading"]))
        self._session_dropdown = Gtk.DropDown(
            tooltip_text="The session whose subs are scanned for movers",
        )
        self._session_dropdown.set_hexpand(True)
        self._session_dropdown.connect("notify::selected", self._on_session_selected)
        header.append(self._session_dropdown)
        self._refresh_button = Gtk.Button(
            label="Refresh",
            tooltip_text="Re-read the project's sessions from the store",
        )
        self._refresh_button.connect("clicked", self._on_refresh_clicked)
        header.append(self._refresh_button)
        self._detect_button = Gtk.Button(
            label="Detect movers",
            tooltip_text="Detect sources in the session's subs, link tracks, flag "
            "linear movers, and save them to the session's movers.json",
        )
        self._detect_button.connect("clicked", self._on_detect_clicked)
        header.append(self._detect_button)
        self._lookup_button = Gtk.Button(
            label="MPC lookup",
            tooltip_text="Look each flagged mover up in the MPC Checker (cached) "
            "and record the match in movers.json",
        )
        self._lookup_button.connect("clicked", self._on_lookup_clicked)
        header.append(self._lookup_button)
        self.append(header)

        # body: movers list | annotated frame
        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        body.set_hexpand(True)
        body.set_vexpand(True)
        self._movers_list = Gtk.ListBox()
        self._movers_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self._movers_list.add_css_class("boxed-list")
        list_frame = Gtk.Frame(child=self._movers_list)
        list_frame.set_size_request(220, -1)
        body.append(list_frame)
        self._stack = Gtk.Stack()
        self._stack.set_hexpand(True)
        self._stack.set_vexpand(True)
        self._picture = Gtk.Picture()
        self._picture.set_can_shrink(True)
        self._picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self._stack.add_titled(self._picture, "image", "Frame")
        self._placeholder_label = Gtk.Label(
            label="No frame to show — select a session with subs",
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
            wrap=True,
            css_classes=["dim-label"],
        )
        self._stack.add_titled(self._placeholder_label, "empty", "Empty")
        self._stack.set_visible_child_name("empty")
        body.append(self._stack)
        self.append(body)

        # frame controls: Prev | Blink | Next + frame label
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._prev_button = Gtk.Button(label="\u25c0 Prev", tooltip_text="Previous frame")
        self._prev_button.connect("clicked", lambda *_a: self._controller.prev_frame())
        controls.append(self._prev_button)
        self._blink_button = Gtk.ToggleButton(
            label="Blink", tooltip_text="Step through the frames automatically"
        )
        self._blink_button.connect("toggled", self._on_blink_toggled)
        controls.append(self._blink_button)
        self._next_button = Gtk.Button(label="Next \u25b6", tooltip_text="Next frame")
        self._next_button.connect("clicked", lambda *_a: self._controller.next_frame())
        controls.append(self._next_button)
        self._frame_label = Gtk.Label(label="", xalign=0.0, css_classes=["dim-label"])
        self._frame_label.set_hexpand(True)
        controls.append(self._frame_label)
        self.append(controls)

        # status label (counts / clean error)
        self._status_label = Gtk.Label(label="", wrap=True, halign=Gtk.Align.CENTER)
        self.append(self._status_label)

        self.connect("destroy", self._on_destroy)
        self._controller.add_listener(self._on_snapshot)
        self._render(self._controller.snapshot)

    # -- public helpers (also used by the widget tests) ------------------------------

    @property
    def controller(self) -> MoverController:
        """The pure-layer mover controller backing this view."""
        return self._controller

    @property
    def mover_controller(self) -> MoverController:
        """Alias of :attr:`controller`."""
        return self._controller

    @property
    def session_dropdown(self) -> Gtk.DropDown:
        """The session selector."""
        return self._session_dropdown

    @property
    def movers_list(self) -> Gtk.ListBox:
        """The movers list (one row per flagged mover)."""
        return self._movers_list

    @property
    def picture(self) -> Gtk.Picture:
        """The annotated-frame picture."""
        return self._picture

    @property
    def detect_button(self) -> Gtk.Button:
        """The Detect movers button."""
        return self._detect_button

    @property
    def lookup_button(self) -> Gtk.Button:
        """The MPC lookup button."""
        return self._lookup_button

    @property
    def blink_button(self) -> Gtk.ToggleButton:
        """The Blink toggle."""
        return self._blink_button

    @property
    def frame_label(self) -> Gtk.Label:
        """The frame label (``frame i/n (id ..., utc)``)."""
        return self._frame_label

    @property
    def status_label(self) -> Gtk.Label:
        """The status label (counts / clean error)."""
        return self._status_label

    def selected_session_id(self) -> str | None:
        """The session id currently chosen in the dropdown (None when empty)."""
        index = self._session_dropdown.get_selected()
        if 0 <= index < len(self._session_ids):
            return self._session_ids[index]
        return None

    def refresh_sessions(self) -> list[str]:
        """Re-list the project's sessions into the dropdown (keeps the current
        selection when still present). Returns the session ids."""
        sessions = list(self._controller.sessions)
        previous = self.selected_session_id()
        self._session_ids = list(sessions)
        self._session_dropdown.set_model(Gtk.StringList.new(self._session_ids))
        if self._session_ids:
            index = self._session_ids.index(previous) if previous in self._session_ids else 0
            self._session_dropdown.set_selected(index)
        return self._session_ids

    # -- events ------------------------------------------------------------------------

    def _on_refresh_clicked(self, *_args) -> None:
        """Refresh button: re-read the store (the controller notifies -> render)."""
        self._controller.refresh_sessions()

    def _on_session_selected(self, dropdown: Gtk.DropDown, *_args) -> None:
        """Dropdown selection changed -> select that session on the controller
        (which notifies -> render)."""
        session_id = self.selected_session_id()
        if session_id is not None and session_id != self._controller.session_id:
            self._controller.select_session(session_id)

    def _on_detect_clicked(self, *_args) -> None:
        """Detect movers button: run the pipeline on a worker thread (the
        controller notifies -> render; the store calls stay on the db thread)."""
        self._run_async(lambda: self._controller.analyze_session())

    def _on_lookup_clicked(self, *_args) -> None:
        """MPC lookup button: run the (cached) lookup on a worker thread."""
        self._run_async(lambda: self._controller.lookup_mpc())

    def _on_blink_toggled(self, button: Gtk.ToggleButton) -> None:
        """Blink toggle: start/stop the periodic frame stepping."""
        if button.get_active():
            self._blink_source = GLib.timeout_add(BLINK_INTERVAL_MS, self._blink_tick)
        else:
            self._stop_blink()

    def _blink_tick(self) -> bool:
        """One blink step: advance to the next frame (wraps). Returns
        ``GLib.SOURCE_CONTINUE`` while blinking."""
        self._controller.next_frame()
        return GLib.SOURCE_CONTINUE

    def _stop_blink(self) -> None:
        if self._blink_source is not None:
            GLib.source_remove(self._blink_source)
            self._blink_source = None

    def _on_destroy(self, *_args) -> None:
        self._stop_blink()
        self._controller.remove_listener(self._on_snapshot)

    def _run_async(self, fn: Any) -> None:
        """Run ``fn`` on a daemon worker thread; the busy state is cleared on
        the main loop when it finishes."""
        if self._busy:
            return
        self._busy = True
        self._set_buttons_sensitive(False)

        def work() -> None:
            try:
                fn()
            finally:
                GLib.idle_add(self._on_work_done)

        threading.Thread(target=work, daemon=True).start()

    def _on_work_done(self) -> bool:
        self._busy = False
        self._set_buttons_sensitive(True)
        return GLib.SOURCE_REMOVE

    def _set_buttons_sensitive(self, sensitive: bool) -> None:
        for button in (self._detect_button, self._lookup_button, self._refresh_button):
            button.set_sensitive(sensitive)

    # -- controller listener ---------------------------------------------------------------

    def _on_snapshot(self, snapshot: MoverSnapshot) -> None:
        """Controller listener: render on the GLib main loop (notifications
        may come from a worker thread, so marshal with ``GLib.idle_add``; on
        the main thread render directly)."""
        if threading.get_ident() == self._main_thread_id:
            self._render(snapshot)
        else:
            GLib.idle_add(self._render, snapshot)

    def _render(self, snapshot: MoverSnapshot) -> bool:
        """Render a frozen snapshot: the dropdown (only when the session list
        changed), the movers list, the annotated frame, the frame label and
        the status. Returns ``GLib.SOURCE_REMOVE`` (one-shot idle callback)."""
        if tuple(snapshot.sessions) != tuple(self._session_ids):
            previous = self.selected_session_id()
            self._session_ids = list(snapshot.sessions)
            self._session_dropdown.set_model(Gtk.StringList.new(self._session_ids))
            if self._session_ids:
                if previous in self._session_ids:
                    index = self._session_ids.index(previous)
                elif snapshot.session_id in self._session_ids:
                    index = self._session_ids.index(snapshot.session_id)
                else:
                    index = 0
                self._session_dropdown.set_selected(index)
        elif snapshot.session_id is None and self._session_ids:
            # auto-select the first session so the view shows something
            self._session_dropdown.set_selected(0)

        # movers list
        while (row := self._movers_list.get_row_at_index(0)) is not None:
            self._movers_list.remove(row)
        for mover in snapshot.movers:
            row = Gtk.ListBoxRow()
            row.set_child(Gtk.Label(label=_mover_row_label(mover), xalign=0.0))
            self._movers_list.append(row)

        # annotated frame
        if snapshot.image is not None:
            self._picture.set_paintable(texture_from_pil(snapshot.image))
            self._stack.set_visible_child_name("image")
        else:
            self._stack.set_visible_child_name("empty")

        # frame label + status
        n = len(snapshot.frames)
        if snapshot.frame_index is not None and n:
            frame = snapshot.frames[snapshot.frame_index]
            self._frame_label.set_label(
                f"frame {snapshot.frame_index + 1}/{n} (id {frame.id}, {frame.utc})"
            )
        else:
            self._frame_label.set_label("")
        if snapshot.error is not None:
            self._status_label.set_label(snapshot.error)
            self._status_label.add_css_class("error")
        else:
            self._status_label.remove_css_class("error")
            self._status_label.set_label(snapshot.status or "")
        return GLib.SOURCE_REMOVE
