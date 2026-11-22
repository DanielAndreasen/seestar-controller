"""The Observe view widget — the WIDGET layer of observing ticket 03.

This module imports ``gi`` at module top (it is a widget module, the only
place allowed to): it is never imported by the pure layer
(:mod:`seestar_controller.observing.observe_controller` /
:mod:`.queue_controller` / :mod:`.executor`) or eagerly by the package
``__init__`` — the pure layer stays gi-free and headless-testable (see
docs/wiki/assumptions.md: the sandbox has no GTK).

On the desktop the system packages are required (PyGObject is NOT a pip
dependency — it needs the system GTK headers to build)::

    sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1

:class:`ObserveView` is a thin observer over the pure
:class:`~seestar_controller.observing.observe_controller.ObserveController`
(the app owns one shared instance; the window passes it down via
:func:`seestar_controller.ui.views.build_view`):

- **left pane** — the :class:`~.queue_editor.QueueEditor` (ticket 01) plus the
  night controls: **Start night**, **Pause/Resume** (one toggle button), **Skip**,
  **Stop**, **Park**, **Session log** (ticket 04 — opens the per-session event
  log + stored frames dialog) — all bound to the controller's passthroughs,
  with an inline status label for errors (start without connection/project,
  park while disconnected) and for the busy state of an in-flight stop
  (ticket 09 — while the executor stops and the final Stacked frame is still
  downloading, the night controls are insensitized and the status row shows
  "Stopping… downloading final frames…" until the executor reaches ``done``
  and the capture has stored the final frames). **Start night** hitting a
  stale open session
  (ticket 05 — the app was killed mid-night) opens the
  :class:`~.stale_session_dialog.StaleSessionDialog` (Resume / Close-and-start-new,
  presented via ``GLib.idle_add``) instead of surfacing the raw
  ``SessionOpenError``;
- **right pane** — the live preview (a :class:`Gtk.Picture` fed from the
  latest decoded frame: the ndarray is encoded to PNG via PIL and wrapped in
  a :class:`Gdk.Texture` with ``Gdk.Texture.new_from_bytes`` — marshalled
  onto the main loop already, because the controller's ``invoke_on_main`` is
  ``GLib.idle_add`` in the app) over a "no preview yet" placeholder, plus a
  **Night progress** group: current target (name + RA/Dec), executor state,
  sub-frames stacked, elapsed, queue position, session;
- the two panes sit in a :class:`Gtk.Paned` (documented choice — simpler than
  ``Adw.OverlaySplitView`` and keeps both panes visible on a wide window).

The widget re-renders on every :class:`~.observe_controller.ObserveViewState`
snapshot (it registers as a controller listener); it never blocks the main
loop — all heavy work (executor, preview sockets) lives in the controllers'
threads.
"""

from __future__ import annotations

import io
import logging
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

import numpy as np
from gi.repository import Adw, Gdk, GLib, Gtk
from PIL import Image

from seestar_controller.store.errors import SessionOpenError
from seestar_controller.store.sessions import Session, get_open_session
from seestar_controller.ui.connection import ConnectionStatus

from .observe_controller import (
    ObserveController,
    ObserveStateError,
    ObserveViewState,
    format_elapsed,
    format_target,
)
from .queue_controller import PlanQueueController
from .queue_editor import QueueEditor
from .session_log_dialog import SessionLogDialog
from .stale_session_dialog import StaleSessionDialog

logger = logging.getLogger(__name__)

#: Left-pane width at construction (pixels; the user can drag the paned handle).
LEFT_PANE_POSITION = 560

#: Status text shown while a stop is in flight (ticket 09): the executor is
#: stopping and the capture is downloading the final Stacked frame.
STOPPING_STATUS_TEXT = "Stopping… downloading final frames…"


def texture_from_array(arr: np.ndarray) -> Gdk.Texture:
    """Build a :class:`Gdk.Texture` from a decoded preview ndarray.

    Grayscale ``(h, w)`` arrays are expanded to RGB; the array is encoded to
    PNG via PIL and wrapped with ``Gdk.Texture.new_from_bytes`` (kept simple
    on purpose — the preview is a status image, not a processing target).
    """
    if arr.ndim == 2:
        arr = np.repeat(arr[:, :, None], 3, axis=2)
    image = Image.fromarray(np.ascontiguousarray(arr, dtype=np.uint8)).convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return Gdk.Texture.new_from_bytes(GLib.Bytes(buffer.getvalue()))


class ObserveView(Gtk.Box):
    """The Observe view: queue editor + night controls on the left, live
    preview + queue progress on the right, driven by a pure
    :class:`~.observe_controller.ObserveController`.

    - ``observe_controller`` — the app's shared controller (required);
    - ``queue_controller`` — the queue editor's controller (defaults to the
      observe controller's own queue controller);
    - ``project`` — the store project the Start button passes to
      ``start_night`` (defaults to the controller's project, set via
      :meth:`~.observe_controller.ObserveController.set_project`).
    """

    def __init__(
        self,
        observe_controller: ObserveController,
        *,
        queue_controller: PlanQueueController | None = None,
        project: Any = None,
        **kwargs,
    ) -> None:
        kwargs.setdefault("orientation", Gtk.Orientation.VERTICAL)
        kwargs.setdefault("spacing", 0)
        super().__init__(**kwargs)
        self._controller = observe_controller
        self._queue_controller = (
            queue_controller
            if queue_controller is not None
            else observe_controller.queue_controller
        )
        self._project = project
        self._last_frame_id: int | None = None
        self._log_dialog: SessionLogDialog | None = None
        self._stale_dialog: StaleSessionDialog | None = None

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

        self._controller.add_listener(self._on_state)
        self._on_state(self._controller.snapshot)

    # -- construction ------------------------------------------------------------

    def _build_left_pane(self) -> Gtk.Widget:
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        left.set_margin_top(12)
        left.set_margin_bottom(12)
        left.set_margin_start(12)
        left.set_margin_end(12)
        left.set_hexpand(True)
        left.set_vexpand(True)

        self._queue_editor = QueueEditor(self._queue_controller)
        left.append(self._queue_editor)

        # Gtk.Box has no wrap property in GTK4; a FlowBox wraps the buttons
        # onto several lines on narrow windows, so the row's minimum width is
        # one button (not the sum) and the window keeps a small minimum size.
        # The FlowBox MUST keep the default halign=FILL: with halign=CENTER
        # the parent Box allocates only the FlowBox's natural width (one
        # button) while the height was negotiated at the full pane width, so
        # the wrapped lines overflow the allocation and the buttons end up
        # clipped below the window (off-screen, unreachable).
        controls = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            homogeneous=False,
            min_children_per_line=1,
            max_children_per_line=6,
            row_spacing=8,
            column_spacing=8,
        )
        self._start_button = Gtk.Button(
            label="Start night", css_classes=["suggested-action"],
            tooltip_text="Start the queue against the connected scope",
        )
        self._start_button.connect("clicked", self._on_start_clicked)
        self._pause_button = Gtk.Button(
            label="Pause", tooltip_text="Pause / resume the current target"
        )
        self._pause_button.connect("clicked", self._on_pause_clicked)
        self._skip_button = Gtk.Button(label="Skip", tooltip_text="Skip the current target")
        self._skip_button.connect("clicked", lambda *_a: self._controller.skip())
        self._stop_button = Gtk.Button(label="Stop", tooltip_text="Stop the night gracefully")
        self._stop_button.connect("clicked", lambda *_a: self._controller.stop())
        self._park_button = Gtk.Button(label="Park", tooltip_text="Park the scope now")
        self._park_button.connect("clicked", self._on_park_clicked)
        self._log_button = Gtk.Button(
            label="Session log",
            tooltip_text="Open the event log + stored frames of the current session",
        )
        self._log_button.connect("clicked", self._on_log_clicked)
        for button in (
            self._start_button,
            self._pause_button,
            self._skip_button,
            self._stop_button,
            self._park_button,
            self._log_button,
        ):
            controls.append(button)
        left.append(controls)

        self._status_label = Gtk.Label(label="", wrap=True, halign=Gtk.Align.CENTER)
        left.append(self._status_label)
        return left

    def _build_right_pane(self) -> Gtk.Widget:
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        right.set_margin_top(12)
        right.set_margin_bottom(12)
        right.set_margin_start(12)
        right.set_margin_end(12)
        right.set_hexpand(True)
        right.set_vexpand(True)

        # preview area: picture + "no preview yet" placeholder overlay
        self._picture = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True)
        self._picture.set_hexpand(True)
        self._picture.set_vexpand(True)
        self._picture.set_size_request(240, 180)
        placeholder = Gtk.Label(
            label="no preview yet",
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
            css_classes=["dim-label", "heading"],
        )
        self._preview_overlay = Gtk.Overlay(child=self._picture)
        self._preview_overlay.add_overlay(placeholder)
        self._preview_placeholder = placeholder
        frame = Gtk.Frame(child=self._preview_overlay)
        frame.set_vexpand(True)
        right.append(frame)
        self._preview_label = Gtk.Label(
            label="", halign=Gtk.Align.CENTER, css_classes=["dim-label", "caption"]
        )
        right.append(self._preview_label)

        # progress group
        group = Adw.PreferencesGroup(title="Night progress")
        self._target_row = Adw.ActionRow(title="Target")
        self._state_row = Adw.ActionRow(title="State")
        self._frames_row = Adw.ActionRow(title="Sub-frames stacked")
        self._elapsed_row = Adw.ActionRow(title="Elapsed on target")
        self._queue_row = Adw.ActionRow(title="Queue position")
        self._session_row = Adw.ActionRow(title="Session")
        for row in (
            self._target_row,
            self._state_row,
            self._frames_row,
            self._elapsed_row,
            self._queue_row,
            self._session_row,
        ):
            group.add(row)
        right.append(group)
        return right

    # -- public helpers (also used by the widget tests) ------------------------------

    @property
    def controller(self) -> ObserveController:
        """The pure-layer observe controller backing this view."""
        return self._controller

    @property
    def queue_controller(self) -> PlanQueueController:
        """The plan-queue controller behind the editor."""
        return self._queue_controller

    @property
    def queue_editor(self) -> QueueEditor:
        """The queue editor widget (left pane)."""
        return self._queue_editor

    @property
    def paned(self) -> Gtk.Paned:
        """The two-pane split."""
        return self._paned

    @property
    def picture(self) -> Gtk.Picture:
        """The preview picture (right pane)."""
        return self._picture

    @property
    def start_button(self) -> Gtk.Button:
        return self._start_button

    @property
    def project(self) -> Any:
        """The project the Start button passes to ``start_night`` (set via
        the constructor or :meth:`set_project`)."""
        return self._project

    @property
    def pause_button(self) -> Gtk.Button:
        return self._pause_button

    @property
    def skip_button(self) -> Gtk.Button:
        return self._skip_button

    @property
    def stop_button(self) -> Gtk.Button:
        return self._stop_button

    @property
    def park_button(self) -> Gtk.Button:
        return self._park_button

    @property
    def log_button(self) -> Gtk.Button:
        """The 'Session log' button (opens the per-session log dialog)."""
        return self._log_button

    @property
    def log_dialog(self) -> SessionLogDialog | None:
        """The currently open session log dialog (None when closed/never opened)."""
        return self._log_dialog

    @property
    def stale_dialog(self) -> StaleSessionDialog | None:
        """The currently open stale-session (resume/close) dialog — ticket 05,
        None when closed/never opened."""
        return self._stale_dialog

    @property
    def status_label(self) -> Gtk.Label:
        return self._status_label

    @property
    def preview_label(self) -> Gtk.Label:
        return self._preview_label

    @property
    def target_row(self) -> Adw.ActionRow:
        return self._target_row

    @property
    def state_row(self) -> Adw.ActionRow:
        return self._state_row

    @property
    def frames_row(self) -> Adw.ActionRow:
        return self._frames_row

    @property
    def elapsed_row(self) -> Adw.ActionRow:
        return self._elapsed_row

    @property
    def queue_row(self) -> Adw.ActionRow:
        return self._queue_row

    @property
    def session_row(self) -> Adw.ActionRow:
        return self._session_row

    def set_project(self, project: Any) -> None:
        """Set the project the Start button passes to ``start_night``."""
        self._project = project
        self._on_state(self._controller.snapshot)

    def row_subtitles(self) -> dict[str, str]:
        """The current progress-row subtitles (test helper)."""
        return {
            "target": self._target_row.get_subtitle(),
            "state": self._state_row.get_subtitle(),
            "frames": self._frames_row.get_subtitle(),
            "elapsed": self._elapsed_row.get_subtitle(),
            "queue": self._queue_row.get_subtitle(),
            "session": self._session_row.get_subtitle(),
        }

    # -- rendering ---------------------------------------------------------------------

    def _on_state(self, state: ObserveViewState) -> None:
        """Controller listener: a new ObserveViewState -> re-render labels,
        buttons and the preview picture."""
        self._target_row.set_subtitle(format_target(state.entry))
        self._state_row.set_subtitle(state.executor_state.value)
        self._frames_row.set_subtitle(str(state.frames_stacked))
        self._elapsed_row.set_subtitle(format_elapsed(state.elapsed_s))
        if state.queue_index is not None and state.queue_entries:
            self._queue_row.set_subtitle(
                f"{state.queue_index + 1} / {len(state.queue_entries)}"
            )
        else:
            self._queue_row.set_subtitle(f"0 / {len(state.queue_entries)}")
        self._session_row.set_subtitle(state.session or "—")

        # preview picture from the latest frame
        frame = state.preview_frame
        if frame is not None and getattr(frame, "id", None) != self._last_frame_id:
            self._last_frame_id = getattr(frame, "id", None)
            try:
                self._picture.set_paintable(texture_from_array(frame.image))
                self._preview_placeholder.set_visible(False)
                width, height = frame.width, frame.height
                self._preview_label.set_label(
                    f"{frame.source} frame #{frame.id} — {width}×{height}"
                )
            except Exception:  # noqa: BLE001
                # a bad frame must never break the view
                self._preview_label.set_label("preview frame could not be rendered")
        elif frame is None:
            self._preview_placeholder.set_visible(True)

        # buttons — ticket 09: while a stop is in flight (state.stopping) the
        # night controls are gated, so the user cannot stack commands onto the
        # stopping run; they re-enable when the terminal snapshot arrives.
        connected = self._controller.connection.status == ConnectionStatus.CONNECTED
        has_project = self._project is not None or self._controller.project is not None
        busy = state.stopping
        self._start_button.set_sensitive(
            connected and has_project and not state.night_active and not busy
        )
        self._pause_button.set_sensitive(state.night_active and not busy)
        self._pause_button.set_label(
            "Resume" if state.executor_state.value == "paused" else "Pause"
        )
        self._skip_button.set_sensitive(state.night_active and not busy)
        self._stop_button.set_sensitive(state.night_active and not busy)
        self._park_button.set_sensitive(connected and not busy)

        # status row — the in-flight stop (ticket 09) takes precedence: it is
        # the feedback that keeps the window from looking hung while the
        # executor stops and the final frames download.
        if state.stopping:
            self._set_status(STOPPING_STATUS_TEXT, error=False)
        elif state.error:
            self._set_status(state.error, error=True)
        elif not state.night_active and state.executor_state.value in ("done", "aborted"):
            self._set_status(f"night {state.executor_state.value}", error=False)
        else:
            self._set_status("", error=False)

    def _set_status(self, text: str, *, error: bool) -> None:
        self._status_label.set_label(text)
        for css_class in ("error", "dim-label"):
            self._status_label.remove_css_class(css_class)
        if error:
            self._status_label.add_css_class("error")
        else:
            self._status_label.add_css_class("dim-label")

    # -- button handlers -----------------------------------------------------------------

    def _on_start_clicked(self, *_args) -> None:
        """Start night: ``controller.start_night(project)``; a stale open
        session (:class:`~seestar_controller.store.errors.SessionOpenError`,
        ticket 05) opens the resume/close dialog instead of an inline error;
        any other failure shows inline (never crash the view)."""
        project = self._project if self._project is not None else self._controller.project
        try:
            self._controller.start_night(project)
        except SessionOpenError as exc:
            # stale open session (the app was killed mid-night): offer
            # resume / close-and-start-new. Marshalled onto the main loop via
            # GLib.idle_add (like every other callback), so the dialog is only
            # ever presented from the main loop.
            GLib.idle_add(self._present_stale_session_dialog, project, exc)
            return
        except Exception as exc:  # noqa: BLE001 - shown inline, never crash the view
            self._set_status(f"{type(exc).__name__}: {exc}", error=True)
            return
        self._set_status("night running", error=False)

    def _present_stale_session_dialog(self, project: Any, exc: SessionOpenError) -> bool:
        """Present the stale-session dialog (ticket 05). Runs via
        ``GLib.idle_add`` on the main loop; returns False so the idle source
        is removed. Falls back to the inline error when no session record is
        available."""
        session = getattr(exc, "session", None)
        if session is None and project is not None:
            try:
                session = get_open_session(project)
            except Exception:
                logger.debug("could not look up the open session", exc_info=True)
                session = None
        if session is None:
            self._set_status(f"{type(exc).__name__}: {exc}", error=True)
            return False
        self._stale_dialog = StaleSessionDialog(
            session,
            on_resume=lambda: self._on_stale_session_resume(project, session),
            on_close_and_start_new=lambda: self._on_stale_session_close_and_start_new(
                project, session
            ),
        )
        # "closed" is only a backstop: with libadwaita 1.5 ``Adw.Dialog.close()``
        # never emits it (animation-gated), so the button actions below clear
        # the reference directly (app-improvement 04).
        self._stale_dialog.connect("closed", self._on_stale_dialog_closed)
        parent = self.get_root()
        self._stale_dialog.present(parent if parent is not None else self)
        return False  # GLib.SOURCE_REMOVE

    def _on_stale_dialog_closed(self, *_args) -> None:
        """The stale-session dialog was closed: forget it (backstop for the
        environments where Adw.Dialog does emit "closed")."""
        self._forget_stale_dialog()

    def _forget_stale_dialog(self) -> None:
        """Forget the stale-session dialog reference (idempotent)."""
        self._stale_dialog = None

    def _on_stale_session_resume(self, project: Any, session: Session) -> None:
        """Dialog **Resume** (ticket 05): reattach the executor to the open
        session via the controller's ``resume_stale_session``. The dialog
        reference is cleared here, deterministically on the button action —
        not only via the animation-gated "closed" signal (app-improvement 04)."""
        self._forget_stale_dialog()
        try:
            self._controller.resume_stale_session(project, session.id)
        except Exception as exc:  # noqa: BLE001 - shown inline, never crash the view
            self._set_status(f"{type(exc).__name__}: {exc}", error=True)
            return
        self._set_status(f"resumed session {session.id}", error=False)

    def _on_stale_session_close_and_start_new(self, project: Any, session: Session) -> None:
        """Dialog **Close and start new** (ticket 05): close the stale session
        (store ``close_session``) and start a fresh night via the controller's
        ``close_and_start_new``. The dialog reference is cleared here,
        deterministically on the button action (app-improvement 04)."""
        self._forget_stale_dialog()
        try:
            self._controller.close_and_start_new(project, session.id)
        except Exception as exc:  # noqa: BLE001 - shown inline, never crash the view
            self._set_status(f"{type(exc).__name__}: {exc}", error=True)
            return
        self._set_status("night running", error=False)

    def _on_pause_clicked(self, *_args) -> None:
        """Pause/Resume toggle: resume when PAUSED, else pause."""
        if self._controller.snapshot.executor_state.value == "paused":
            self._controller.resume()
        else:
            self._controller.pause()

    def _on_park_clicked(self, *_args) -> None:
        """Park: ``controller.park()``; inline error when not connected."""
        try:
            self._controller.park()
        except ObserveStateError as exc:
            self._set_status(str(exc), error=True)
            return
        self._set_status("scope parked", error=False)

    def _on_log_clicked(self, *_args) -> None:
        """Session log: open the per-session log dialog for the current/last
        night's capture (inline status when no session was captured yet)."""
        capture = getattr(self._controller, "capture", None)
        if capture is None:
            self._set_status("no session yet — start a night first", error=False)
            return
        self._log_dialog = SessionLogDialog(capture)
        parent = self.get_root()
        self._log_dialog.present(parent if parent is not None else self)
