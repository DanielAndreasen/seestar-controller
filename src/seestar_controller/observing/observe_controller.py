r"""The Observe-view controller — the PURE (no ``gi``) wiring layer of ticket 03.

This module binds the pieces the Observe view needs into one app-level
controller, fully testable headless against the mock Scope (the sandbox has
no GTK, see docs/wiki/assumptions.md; a subprocess test enforces the no-``gi``
boundary). Like :mod:`.executor` it is an **executor-layer** module of
:mod:`seestar_controller.observing`: **no ``gi`` import**, but it does import
the link (preview stream) + the ui pure layer (connection / notifications) +
:mod:`.executor`, so it is re-exported **lazily** from the package ``__init__``
(PEP 562 ``_EXECUTOR_EXPORTS``) — importing ``seestar_controller.observing``
alone never pulls the link, the ui package, or this module into
``sys.modules``.

:class:`ObserveController` is the object the app owns (one shared instance,
like the ui :class:`~seestar_controller.ui.connection.ConnectionController`
and the :class:`~.queue_controller.PlanQueueController`) and the Observe view
widget (:mod:`.observe_view`) renders:

- ``connection`` — the ui :class:`~seestar_controller.ui.connection.ConnectionController`
  (device access: ``connection.device`` / ``connection.status`` /
  ``connection.scope_ip``);
- ``queue_controller`` — the shared :class:`~.queue_controller.PlanQueueController`
  (tonight's plan; the queue editor edits it, the executor consumes it);
- ``bus`` — the shared :class:`~seestar_controller.ui.notifications.NotificationBus`
  (night start/finish/error and preview problems surface as notifications);
- ``executor_factory`` — injectable; default builds a :class:`~.executor.NightExecutor`
  from the connected device + the queue + the project;
- ``preview_stream_factory`` — injectable; default builds a
  :class:`~seestar_controller.link.preview.PreviewStream` **from the device**
  (the scope host of the connected transport, image ports :4800/:4804);
- ``invoke_on_main`` — marshals every callback onto the "main" thread (the
  app binds ``GLib.idle_add``; the default calls directly, so tests are
  synchronous);
- ``preview_max_fps`` — the display throttle for preview frames
  (:class:`~seestar_controller.link.preview.FrameThrottle`).

What it does:

- :meth:`ObserveController.start_night` — ``start_night(project, **executor_kwargs)``:
  requires a connected device and a project (the argument, or the one set via
  :meth:`set_project`), creates the :class:`~.executor.NightExecutor` through
  the factory, subscribes to its snapshots, starts the preview stream
  (``start(tele=True)``) and starts the executor. A preview-stream failure
  (unreachable image socket) does NOT kill the night — a warning notification
  is pushed and the run continues without preview (documented choice);
- pause / resume / skip / stop — passthroughs to the running executor
  (no-ops when no night is active);
- :meth:`ObserveController.park` — ``device.park()`` through the connection
  controller's device (the manual Park button);
- preview frames — the stream's callback receives :class:`~PreviewFrame`\\ s
  (already decoded by the stream via
  :func:`~seestar_controller.link.preview.decode_payload`); a raw
  :class:`~seestar_controller.link.image.Frame` is decoded here with
  ``decode_payload`` too. The latest frame is kept in :attr:`latest_frame`
  and delivered to the ``on_preview_frame`` listeners, throttled by a
  :class:`FrameThrottle` (``preview_max_fps``);
- progress — every executor snapshot (transition or stacking tick) is folded
  into a frozen :class:`ObserveViewState` (executor state, current entry,
  frames stacked, elapsed, error, session, queue entries, latest preview
  frame, flags) emitted to the view listeners on any change — executor
  transition, preview frame available, queue change;
- executor DONE / ABORTED → the preview stream is stopped (and a notification
  pushed: info on DONE, error on ABORTED);
- session capture (ticket 04) — :meth:`start_night` also starts a
  :class:`~.capture.SessionCapture` for the opened session (sub-frames /
  Stacks downloaded into the session as they arrive + the per-session event
  log, exposed via :attr:`capture`); DONE/ABORTED stops it. A capture
  failure pushes a warning notification but never kills the night. Every
  frame the capture stores is re-emitted to the **frame-stored listeners**
  (:meth:`add_frame_listener`) as a :class:`~.capture.FrameStoredEvent`,
  marshalled through ``invoke_on_main`` (ticket 07 — the Projects view
  re-renders its stored-data browser on it, live during a night);
- stale sessions (ticket 05) — when a previous session of the project is
  still marked open (the app was killed mid-night), :meth:`start_night`
  raises :class:`StaleSessionError` (a
  :class:`~seestar_controller.store.errors.SessionOpenError` carrying the open
  :class:`~seestar_controller.store.sessions.Session`) **before** creating
  anything, so the Observe view can offer the resume / close-and-start-new
  dialog instead of surfacing the raw error. :meth:`resume_stale_session`
  reattaches the executor to the open session (``resume_session`` kwarg of
  :class:`~.executor.NightExecutor`); :meth:`close_stale_session` closes it via
  :func:`~seestar_controller.store.sessions.close_session` and
  :meth:`close_and_start_new` then starts a fresh night.

All callbacks (executor snapshots, preview frames, queue events) are
marshalled through ``invoke_on_main``, so with ``GLib.idle_add`` nothing
touches GTK from a worker/dispatcher thread.

NOTE (user AFK, no real scope — docs/wiki/assumptions.md): all link
interaction is exercised against the mock Scope only.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from seestar_controller.link.image import Frame
from seestar_controller.link.preview import (
    FrameThrottle,
    PreviewFrame,
    PreviewStream,
    decode_payload,
)
from seestar_controller.store.errors import SessionOpenError
from seestar_controller.store.sessions import (
    Session,
    close_session,
    get_open_session,
)
from seestar_controller.ui.connection import ConnectionController, ConnectionStatus
from seestar_controller.ui.notifications import (
    Notification,
    NotificationBus,
    NotificationSeverity,
)

from .capture import FrameStoredEvent, FrameStoredListener, SessionCapture
from .executor import ExecutorSnapshot, ExecutorState, NightExecutor
from .queue import QueueEntry, QueueEvent
from .queue_controller import PlanQueueController

logger = logging.getLogger(__name__)

#: The ``source`` of observe-derived notifications.
OBSERVE_SOURCE = "observe"

#: Default display throttle for preview frames (frames per second).
DEFAULT_PREVIEW_MAX_FPS = 10.0

#: The executor states in which a night is "active" (commands accepted).
_ACTIVE_EXECUTOR_STATES: tuple[ExecutorState, ...] = (
    ExecutorState.RESOLVING,
    ExecutorState.SLEWING,
    ExecutorState.STACKING,
    ExecutorState.PAUSED,
)

#: The terminal executor states.
_TERMINAL_EXECUTOR_STATES: tuple[ExecutorState, ...] = (
    ExecutorState.DONE,
    ExecutorState.ABORTED,
)


class ObserveStateError(RuntimeError):
    """An invalid observe-controller operation (start without a connection
    or project, park while disconnected, start while a night is running)."""


class StaleSessionError(SessionOpenError):
    """A stale session is still open in the project (ticket 05 — e.g. the app
    was killed mid-night and never closed it): :meth:`ObserveController.start_night`
    raises this instead of letting the raw store error surface, so the Observe
    view can offer the resume / close-and-start-new dialog.

    It **is** a :class:`~seestar_controller.store.errors.SessionOpenError`
    (catch that to handle both), and carries the open session record:

    - ``session`` — the open :class:`~seestar_controller.store.sessions.Session`
      (``id`` + ``started_at``), used for the dialog text and the resume/close
      actions.
    """

    def __init__(self, session: Session) -> None:
        self.session: Session = session
        super().__init__(
            f"session {session.id} from {session.started_at} is still open — "
            "resume it or close it and start a new one"
        )


@dataclass(frozen=True)
class ObserveViewState:
    """An immutable snapshot of everything the Observe view renders, handed
    to the view listeners on any change (executor transition, preview frame
    available, queue change).

    - ``executor_state`` — the :class:`~.executor.ExecutorState` (IDLE when
      no night was started);
    - ``queue_index`` — the index of the entry being processed (None in IDLE);
    - ``entry`` — the current :class:`~.queue.QueueEntry` (None in IDLE);
    - ``frames_stacked`` — sub-frames stacked so far on the current entry;
    - ``elapsed_s`` — wall-clock seconds spent on the current entry;
    - ``error`` — the last executor error, else None;
    - ``session`` — the open session id, else None;
    - ``queue_entries`` — the plan queue snapshot (tuple of QueueEntry);
    - ``preview_frame`` — the latest decoded :class:`~seestar_controller.link.preview.PreviewFrame`
      (None until the first frame arrives);
    - ``preview_running`` — whether the preview stream is running;
    - ``night_active`` — whether a night is in flight (commands accepted).
    """

    executor_state: ExecutorState
    queue_index: int | None
    entry: QueueEntry | None
    frames_stacked: int
    elapsed_s: float
    error: str | None
    session: str | None
    queue_entries: tuple[QueueEntry, ...]
    preview_frame: Any | None
    preview_running: bool
    night_active: bool


#: A view-state listener: called with an :class:`ObserveViewState` on any
#: change (always via ``invoke_on_main``).
ObserveViewListener = Callable[[ObserveViewState], None]

#: A preview-frame listener: called with the latest decoded
#: :class:`~seestar_controller.link.preview.PreviewFrame` (throttled to
#: ``preview_max_fps``; always via ``invoke_on_main``).
PreviewFrameListener = Callable[[Any], None]

# NOTE: the frame-stored listener alias :data:`~.capture.FrameStoredListener`
# (``Callable[[FrameStoredEvent], None]``) is imported from :mod:`.capture`
# above — listeners registered via :meth:`ObserveController.add_frame_listener`
# receive a :class:`~.capture.FrameStoredEvent`, always via
# ``invoke_on_main`` (observing ticket 07).

#: Marshals a zero-argument callback onto the "main" thread (same alias as
#: the ui connection controller / the executor).
InvokeOnMain = Callable[[Callable[[], None]], Any]


def _direct_invoke(callback: Callable[[], None]) -> None:
    """The default ``invoke_on_main``: run the callback directly (tests)."""
    callback()


#: An executor factory: ``factory(device, queue, project, **kwargs) ->
#: NightExecutor``. The default builds the real :class:`~.executor.NightExecutor`.
ExecutorFactory = Callable[..., NightExecutor]

#: A preview-stream factory: ``factory(device, host) -> stream`` where the
#: stream has the :class:`~seestar_controller.link.preview.PreviewStream`
#: shape (``add_callback`` / ``start(tele=, wide=)`` / ``stop()`` /
#: ``running``). The default builds a real
#: :class:`~seestar_controller.link.preview.PreviewStream` from the device's
#: scope host.
PreviewStreamFactory = Callable[[Any, str], Any]

#: A session-capture factory: ``factory(device, project, session_id, executor,
#: **kwargs) -> SessionCapture`` (ticket 04 — frames + session log). The
#: default builds the real :class:`~.capture.SessionCapture`.
CaptureFactory = Callable[..., SessionCapture]


def default_capture_factory(
    device: Any, project: Any, session_id: str, executor: NightExecutor, **kwargs: Any
) -> SessionCapture:
    """The default capture factory: a plain :class:`~.capture.SessionCapture`
    bound to the night's executor (its log is mirrored into the session
    log)."""
    return SessionCapture(device, project, session_id, executor=executor, **kwargs)


def default_executor_factory(device: Any, queue: Any, project: Any, **kwargs: Any) -> NightExecutor:
    """The default executor factory: a plain :class:`~.executor.NightExecutor`."""
    return NightExecutor(device, queue, project, **kwargs)


def default_preview_stream_factory(
    device: Any, host: str, *, max_fps: float | None = DEFAULT_PREVIEW_MAX_FPS
) -> PreviewStream:
    """The default preview-stream factory: a real
    :class:`~seestar_controller.link.preview.PreviewStream` to the scope host
    of the connected device (image ports :4800 tele / :4804 wide).

    ``device`` is only used for documentation here — the host is passed in
    (the controller resolves it from the connection's scope IP, falling back
    to ``device.transport.host``).
    """
    return PreviewStream(host, max_fps=max_fps)


def format_target(entry: QueueEntry | None) -> str:
    """The one-line current-target label: ``"M 42 — RA 5.5881h Dec -5.3911°"``
    for a resolved entry, ``"M 42 (unresolved)"`` for a name-only entry,
    ``"RA … Dec …"`` for a coord-only entry, ``"—"`` when idle."""
    if entry is None:
        return "—"
    if entry.name is not None and entry.coord is not None:
        return (
            f"{entry.name} — RA {entry.coord.ra_hours:.4f}h "
            f"Dec {entry.coord.dec_deg:+.4f}°"
        )
    if entry.name is not None:
        return f"{entry.name} (unresolved)"
    assert entry.coord is not None
    return f"RA {entry.coord.ra_hours:.4f}h Dec {entry.coord.dec_deg:+.4f}°"


def format_elapsed(seconds: float) -> str:
    """The elapsed-time label: ``"42 s"`` (``"0 s"`` when idle)."""
    return f"{max(0.0, seconds):.0f} s"


def format_session_time(started_at: str) -> str:
    """Format a session ``started_at`` ISO-8601 string for the stale-session
    dialog (ticket 05): ``"2026-11-20 22:00:00 UTC"``. A naive timestamp is
    treated as UTC; an unparseable string is returned unchanged."""
    try:
        dt = datetime.fromisoformat(started_at)
    except ValueError:
        return started_at
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _idle_snapshot() -> ExecutorSnapshot:
    """The executor snapshot used before any night was started."""
    return ExecutorSnapshot(
        state=ExecutorState.IDLE,
        queue_index=None,
        entry=None,
        frames_stacked=0,
        elapsed_s=0.0,
        error=None,
        session=None,
    )


class ObserveController:
    """The app-level Observe-view controller: connection + queue + executor +
    preview stream + notifications, emitting frozen :class:`ObserveViewState`
    snapshots.

    - ``connection`` — the ui :class:`~seestar_controller.ui.connection.ConnectionController`;
    - ``queue_controller`` — the shared :class:`~.queue_controller.PlanQueueController`;
    - ``bus`` — the shared notification bus (optional; night/preview events
      are pushed to it when given);
    - ``executor_factory`` / ``preview_stream_factory`` — injectable
      factories (see :data:`ExecutorFactory` / :data:`PreviewStreamFactory`);
    - ``capture_factory`` — injectable session-capture factory (ticket 04,
      see :data:`CaptureFactory`); the default builds a
      :class:`~.capture.SessionCapture` for the night's session;
    - ``invoke_on_main`` — the marshalling callable (default: direct call);
    - ``preview_max_fps`` — the preview display throttle (None disables).

    Frame-stored events (observing ticket 07): every frame the session
    capture stores into the night's session is re-emitted to the listeners
    registered via :meth:`add_frame_listener`, always via
    ``invoke_on_main`` — the Projects view uses this to refresh its
    stored-data browser live during a night.
    """

    def __init__(
        self,
        connection: ConnectionController,
        queue_controller: PlanQueueController,
        bus: NotificationBus | None = None,
        *,
        executor_factory: ExecutorFactory | None = None,
        preview_stream_factory: PreviewStreamFactory | None = None,
        capture_factory: CaptureFactory | None = None,
        invoke_on_main: InvokeOnMain | None = None,
        preview_max_fps: float | None = DEFAULT_PREVIEW_MAX_FPS,
    ) -> None:
        if not hasattr(connection, "device") or not hasattr(connection, "status"):
            raise ValueError(
                f"connection must be a ConnectionController-like object, got {connection!r}"
            )
        if not hasattr(queue_controller, "queue") or not hasattr(queue_controller, "entries"):
            raise ValueError(
                f"queue_controller must be a PlanQueueController-like object, "
                f"got {queue_controller!r}"
            )
        self._connection = connection
        self._queue_controller = queue_controller
        self._bus = bus
        self._executor_factory: ExecutorFactory = executor_factory or default_executor_factory
        self._preview_stream_factory: PreviewStreamFactory = (
            preview_stream_factory or default_preview_stream_factory
        )
        self._capture_factory: CaptureFactory = capture_factory or default_capture_factory
        self._invoke_on_main: InvokeOnMain = invoke_on_main or _direct_invoke
        self._throttle = FrameThrottle(preview_max_fps)
        self._lock = threading.Lock()
        self._executor: NightExecutor | None = None
        self._capture: SessionCapture | None = None
        self._stream: Any | None = None
        self._latest_frame: Any | None = None
        self._project: Any | None = None
        self._listeners: list[ObserveViewListener] = []
        self._preview_listeners: list[PreviewFrameListener] = []
        self._frame_listeners: list[FrameStoredListener] = []
        self._queue_controller.add_listener(self._on_queue_event)

    # -- accessors ---------------------------------------------------------------

    @property
    def connection(self) -> ConnectionController:
        """The observed connection controller."""
        return self._connection

    @property
    def queue_controller(self) -> PlanQueueController:
        """The shared plan-queue controller."""
        return self._queue_controller

    @property
    def bus(self) -> NotificationBus | None:
        """The notification bus (None when not given)."""
        return self._bus

    @property
    def executor(self) -> NightExecutor | None:
        """The running/last night executor (None before :meth:`start_night`)."""
        with self._lock:
            return self._executor

    @property
    def capture(self) -> SessionCapture | None:
        """The session capture of the current/last night (None before
        :meth:`start_night` opened a session) — the "Session log" dialog
        reads it."""
        with self._lock:
            return self._capture

    @property
    def stream(self) -> Any | None:
        """The preview stream of the current/last night (None when preview
        is unavailable)."""
        with self._lock:
            return self._stream

    @property
    def latest_frame(self) -> Any | None:
        """The latest decoded preview frame (a
        :class:`~seestar_controller.link.preview.PreviewFrame`), None until
        the first frame arrived. Updated even when the notification is
        throttled."""
        with self._lock:
            return self._latest_frame

    @property
    def project(self) -> Any | None:
        """The project set via :meth:`set_project` (used by :meth:`start_night`
        when no project argument is given)."""
        with self._lock:
            return self._project

    @property
    def night_active(self) -> bool:
        """Whether a night is in flight (executor in an active state)."""
        with self._lock:
            executor = self._executor
        return executor is not None and executor.state in _ACTIVE_EXECUTOR_STATES

    @property
    def preview_running(self) -> bool:
        """Whether the preview stream is running right now."""
        with self._lock:
            stream = self._stream
        return stream is not None and bool(getattr(stream, "running", False))

    @property
    def snapshot(self) -> ObserveViewState:
        """An immutable :class:`ObserveViewState` of the current state."""
        with self._lock:
            executor = self._executor
            stream = self._stream
            frame = self._latest_frame
        executor_snapshot = executor.snapshot if executor is not None else _idle_snapshot()
        return ObserveViewState(
            executor_state=executor_snapshot.state,
            queue_index=executor_snapshot.queue_index,
            entry=executor_snapshot.entry,
            frames_stacked=executor_snapshot.frames_stacked,
            elapsed_s=executor_snapshot.elapsed_s,
            error=executor_snapshot.error,
            session=executor_snapshot.session,
            queue_entries=self._queue_controller.entries,
            preview_frame=frame,
            preview_running=stream is not None and bool(getattr(stream, "running", False)),
            night_active=executor_snapshot.state in _ACTIVE_EXECUTOR_STATES,
        )

    def set_project(self, project: Any) -> None:
        """Set the project :meth:`start_night` uses when called without a
        project argument (the app sets it when a project is opened)."""
        with self._lock:
            self._project = project
        self._emit()

    # -- listeners ---------------------------------------------------------------

    @property
    def listeners(self) -> tuple[ObserveViewListener, ...]:
        """The registered view-state listeners, in registration order."""
        return tuple(self._listeners)

    def add_listener(self, listener: ObserveViewListener) -> ObserveViewListener:
        """Register a view-state listener (idempotent); returns it, so it can
        be used as a decorator. The listener receives an
        :class:`ObserveViewState` on any change, via ``invoke_on_main``."""
        if listener not in self._listeners:
            self._listeners.append(listener)
        return listener

    def remove_listener(self, listener: ObserveViewListener) -> None:
        """Unregister a view-state listener (no-op when not registered)."""
        if listener in self._listeners:
            self._listeners.remove(listener)

    @property
    def preview_listeners(self) -> tuple[PreviewFrameListener, ...]:
        """The registered preview-frame listeners, in registration order."""
        return tuple(self._preview_listeners)

    def add_preview_listener(self, listener: PreviewFrameListener) -> PreviewFrameListener:
        """Register a preview-frame listener (idempotent); returns it."""
        if listener not in self._preview_listeners:
            self._preview_listeners.append(listener)
        return listener

    def remove_preview_listener(self, listener: PreviewFrameListener) -> None:
        """Unregister a preview-frame listener (no-op when not registered)."""
        if listener in self._preview_listeners:
            self._preview_listeners.remove(listener)

    def on_preview_frame(self, listener: PreviewFrameListener | None = None) -> Any:
        """Register a preview-frame listener; decorator form
        ``@controller.on_preview_frame``."""
        if listener is None:

            def decorator(fn: PreviewFrameListener) -> PreviewFrameListener:
                self.add_preview_listener(fn)
                return fn

            return decorator
        self.add_preview_listener(listener)
        return listener

    @property
    def frame_listeners(self) -> tuple[FrameStoredListener, ...]:
        """The registered frame-stored listeners, in registration order."""
        return tuple(self._frame_listeners)

    def add_frame_listener(self, listener: FrameStoredListener) -> FrameStoredListener:
        """Register a frame-stored listener (idempotent); returns it, so it
        can be used as a decorator. The listener receives a
        :class:`~.capture.FrameStoredEvent` when the session capture stores a
        frame into the night's session — always via ``invoke_on_main``
        (observing ticket 07: the Projects view re-renders its stored-data
        browser on it, live during a night)."""
        if listener not in self._frame_listeners:
            self._frame_listeners.append(listener)
        return listener

    def remove_frame_listener(self, listener: FrameStoredListener) -> None:
        """Unregister a frame-stored listener (no-op when not registered)."""
        if listener in self._frame_listeners:
            self._frame_listeners.remove(listener)

    # -- notifications -------------------------------------------------------------

    def _notify(
        self,
        severity: NotificationSeverity,
        title: str,
        message: str,
    ) -> None:
        """Push a notification to the bus (when given); never raises."""
        if self._bus is None:
            return
        try:
            self._bus.push(Notification(severity, title, message, OBSERVE_SOURCE))
        except Exception:
            logger.exception("could not push observe notification")

    # -- start night ---------------------------------------------------------------

    def _resolve_project(self, project: Any) -> Any:
        """The given project, or the one set via :meth:`set_project`;
        raises :class:`ObserveStateError` when neither is set."""
        with self._lock:
            if project is None:
                project = self._project
        if project is None:
            raise ObserveStateError(
                "no project — open a project first (Projects view)"
            )
        return project

    def open_session(self, project: Any = None) -> Session | None:
        """The project's currently open session (``ended_at IS NULL``), or
        None — the stale-session check behind the resume/close dialog
        (ticket 05)."""
        return get_open_session(self._resolve_project(project))

    def start_night(
        self, project: Any = None, *, resume_session: str | None = None, **executor_kwargs: Any
    ) -> NightExecutor:
        """Start the night: create + start the executor and the preview stream.

        - ``project`` — the store project the session is opened in; defaults
          to the project set via :meth:`set_project`. Raises
          :class:`ObserveStateError` when neither is set;
        - ``resume_session`` — the id of an already-open session to reattach
          to (ticket 05) instead of opening a new one; forwarded to the
          executor factory (the default :class:`~.executor.NightExecutor`
          adopts the open session). Raises :class:`ObserveStateError` when the
          id is not the project's open session;
        - ``**executor_kwargs`` — forwarded to the executor factory (e.g.
          ``frames_per_target=6``, ``duration_per_target_s=600``,
          ``error_policy="abort"``, ``park_at_end=False``).

        Requires a connected device (:class:`ObserveStateError` otherwise) and
        no night already in flight (:class:`ObserveStateError`; wait for
        DONE/ABORTED first). When a **stale session is still open** in the
        project (the app was killed mid-night), raises
        :class:`StaleSessionError` — a
        :class:`~seestar_controller.store.errors.SessionOpenError` carrying the
        open :class:`~seestar_controller.store.sessions.Session` — so the view
        can offer resume / close-and-start-new (see :meth:`resume_stale_session`
        / :meth:`close_and_start_new`) instead of surfacing the raw error.
        The preview stream is started best-effort: a failure (unreachable
        image socket) pushes a warning notification and the night runs without
        preview.

        Returns the started :class:`~.executor.NightExecutor`.
        """
        with self._lock:
            if self._executor is not None and self._executor.state in _ACTIVE_EXECUTOR_STATES:
                raise ObserveStateError(
                    f"a night is already running ({self._executor.state.value}) — "
                    "stop it first"
                )
            device = self._connection.device
            if device is None or self._connection.status != ConnectionStatus.CONNECTED:
                raise ObserveStateError(
                    "not connected to the scope — connect first (header bar)"
                )
            if project is None:
                project = self._project
            if project is None:
                raise ObserveStateError(
                    "no project — open a project first (Projects view)"
                )
            old_executor = self._executor
        # ticket 05: a stale open session blocks the start — raise
        # StaleSessionError (a SessionOpenError) BEFORE creating anything, so
        # the view can offer resume / close-and-start-new.
        open_session = get_open_session(project)
        if open_session is not None and open_session.id != resume_session:
            # a stale session is open and we are not resuming exactly it
            raise StaleSessionError(open_session)
        if resume_session is not None and open_session is None:
            raise ObserveStateError(
                f"cannot resume session {resume_session!r} — no session is open "
                "in the project"
            )
        if old_executor is not None:
            old_executor.remove_listener(self._on_executor_snapshot)
        kwargs = dict(executor_kwargs)
        kwargs.setdefault("resolver", self._queue_controller.resolver)
        kwargs["invoke_on_main"] = self._invoke_on_main
        if resume_session is not None:
            kwargs["resume_session"] = resume_session
        executor = self._executor_factory(
            device, self._queue_controller.queue, project, **kwargs
        )
        executor.add_listener(self._on_executor_snapshot)
        with self._lock:
            self._executor = executor
            self._latest_frame = None
        self._start_preview(device)
        self._notify(
            NotificationSeverity.INFO,
            "Night started",
            f"{len(self._queue_controller)} target(s) queued, session opening",
        )
        executor.start()  # may raise ExecutorStateError (propagated) or return None (ABORTED)
        session_id = executor.session
        if session_id is not None:
            self._start_capture(device, project, session_id, executor)
        self._emit()
        return executor

    # -- stale-session actions (ticket 05) -------------------------------------------

    def resume_stale_session(
        self,
        project: Any = None,
        session_id: str | None = None,
        **executor_kwargs: Any,
    ) -> NightExecutor:
        """Resume the stale open session (ticket 05 — the dialog's **Resume**
        button): reattach the executor to the already-open session instead of
        opening a new one.

        - ``project`` — defaults to the project set via :meth:`set_project`;
        - ``session_id`` — defaults to the project's open session;
        - ``**executor_kwargs`` — forwarded to :meth:`start_night`.

        Raises :class:`ObserveStateError` when there is no open session to
        resume (or the id is not the open one).
        """
        project = self._resolve_project(project)
        if session_id is None:
            session = get_open_session(project)
            if session is None:
                raise ObserveStateError("no open session to resume")
            session_id = session.id
        return self.start_night(project, resume_session=session_id, **executor_kwargs)

    def close_stale_session(self, project: Any = None, session_id: str | None = None) -> Session:
        """Close the stale open session (ticket 05 — the dialog's
        **Close-and-start-new** button, first half) via
        :func:`seestar_controller.store.sessions.close_session` on the calling
        thread. Returns the closed :class:`~seestar_controller.store.sessions.Session`.

        - ``project`` — defaults to the project set via :meth:`set_project`;
        - ``session_id`` — defaults to the project's open session.

        Raises :class:`ObserveStateError` when there is no open session to
        close; the store's own errors (not found / already closed) propagate.
        """
        project = self._resolve_project(project)
        if session_id is None:
            session = get_open_session(project)
            if session is None:
                raise ObserveStateError("no open session to close")
            session_id = session.id
        closed = close_session(project, session_id)
        self._notify(
            NotificationSeverity.INFO,
            "Stale session closed",
            f"session {session_id} (started {closed.started_at}) was closed",
        )
        self._emit()
        return closed

    def close_and_start_new(
        self,
        project: Any = None,
        session_id: str | None = None,
        **executor_kwargs: Any,
    ) -> NightExecutor:
        """Close the stale open session and start a fresh night (ticket 05 —
        the dialog's **Close-and-start-new** button): :meth:`close_stale_session`
        then :meth:`start_night`, which now opens a NEW session."""
        self.close_stale_session(project, session_id)
        return self.start_night(project, **executor_kwargs)

    def _start_capture(
        self, device: Any, project: Any, session_id: str, executor: NightExecutor
    ) -> None:
        """Start the session capture for the new night (ticket 04). Best-effort:
        a capture failure pushes a warning notification but never kills the
        night."""
        with self._lock:
            old = self._capture
        if old is not None:
            # ticket 07: detach the frame-stored forwarding from the old capture
            old.remove_frame_listener(self._on_capture_frame_stored)
            if old.running:
                try:
                    old.stop()
                except Exception:
                    logger.exception("stopping the previous session capture failed")
        try:
            capture = self._capture_factory(
                device,
                project,
                session_id,
                executor,
                invoke_on_main=self._invoke_on_main,
                bus=self._bus,
            )
        except Exception as exc:  # noqa: BLE001 - warn, never kill the night
            message = f"{type(exc).__name__}: {exc}"
            logger.warning("session capture unavailable: %s", message)
            self._notify(
                NotificationSeverity.WARNING,
                "Session capture unavailable",
                f"frames will not be downloaded into the session: {message}",
            )
            return
        with self._lock:
            self._capture = capture
        # ticket 07: forward every frame the capture stores to the
        # frame-stored listeners (marshalled via invoke_on_main)
        capture.add_frame_listener(self._on_capture_frame_stored)
        try:
            capture.start()
        except Exception as exc:  # noqa: BLE001 - warn, never kill the night
            message = f"{type(exc).__name__}: {exc}"
            logger.warning("session capture could not start: %s", message)
            self._notify(
                NotificationSeverity.WARNING,
                "Session capture unavailable",
                f"frames will not be downloaded into the session: {message}",
            )

    def _stop_capture(self) -> None:
        """Stop the session capture (idempotent; never raises)."""
        with self._lock:
            capture = self._capture
        if capture is not None:
            # ticket 07: detach the frame-stored forwarding
            capture.remove_frame_listener(self._on_capture_frame_stored)
            if capture.running:
                try:
                    capture.stop()
                except Exception:
                    logger.exception("stopping the session capture failed")

    def _start_preview(self, device: Any) -> None:
        """Start the preview stream best-effort (tele socket); a failure is a
        warning notification, not a failed night."""
        host = self._connection.scope_ip or getattr(
            getattr(device, "transport", None), "host", ""
        )
        try:
            stream = self._preview_stream_factory(device, host)
            stream.add_callback(self._on_stream_frame)
            stream.start(tele=True)
        except Exception as exc:  # noqa: BLE001
            message = f"{type(exc).__name__}: {exc}"
            logger.warning("preview stream could not start: %s", message)
            self._notify(
                NotificationSeverity.WARNING,
                "Preview unavailable",
                f"the live image stream could not start: {message}",
            )
            with self._lock:
                self._stream = None
            return
        with self._lock:
            self._stream = stream

    def stop_preview(self) -> None:
        """Stop the preview stream (idempotent; safe when none is running)."""
        with self._lock:
            stream = self._stream
            self._stream = None
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                logger.debug("preview stop failed (ignored)", exc_info=True)
        self._emit()

    # -- executor passthroughs -------------------------------------------------------

    def pause(self) -> None:
        """Pause the current target (passthrough; no-op when no night runs)."""
        with self._lock:
            executor = self._executor
        if executor is not None and executor.state in _ACTIVE_EXECUTOR_STATES:
            executor.pause()

    def resume(self) -> None:
        """Resume from PAUSED (passthrough; no-op when not paused)."""
        with self._lock:
            executor = self._executor
        if executor is not None and executor.state == ExecutorState.PAUSED:
            executor.resume()

    def skip(self) -> None:
        """Skip the current entry (passthrough; no-op when no night runs)."""
        with self._lock:
            executor = self._executor
        if executor is not None and executor.state in _ACTIVE_EXECUTOR_STATES:
            executor.skip()

    def stop(self) -> None:
        """Stop the night gracefully (passthrough; no-op when no night runs)."""
        with self._lock:
            executor = self._executor
        if executor is not None and executor.state in _ACTIVE_EXECUTOR_STATES:
            executor.stop()

    def park(self) -> None:
        """Park the Scope now (the manual Park button): ``device.park()``
        through the connection controller's device.

        Raises :class:`ObserveStateError` when not connected.
        """
        device = self._connection.device
        if device is None or self._connection.status != ConnectionStatus.CONNECTED:
            raise ObserveStateError("not connected to the scope — connect first")
        device.park()
        self._notify(NotificationSeverity.INFO, "Scope parked", "the scope was parked")
        self._emit()

    # -- event handlers (marshalled via invoke_on_main) --------------------------------

    def _on_queue_event(self, event: QueueEvent) -> None:
        """Queue-changed event (any thread): re-emit the view state."""
        self._invoke_on_main(self._emit)

    def _on_executor_snapshot(self, snapshot: ExecutorSnapshot) -> None:
        """Executor listener: fold the snapshot into the view state; on a
        terminal state stop the preview and notify."""
        self._invoke_on_main(lambda: self._handle_executor_snapshot(snapshot))

    def _on_capture_frame_stored(self, event: FrameStoredEvent) -> None:
        """A frame was stored into the night's session by the capture (any
        thread): re-emit the event to the frame-stored listeners via
        ``invoke_on_main`` (observing ticket 07 — the Projects view's
        stored-data browser re-renders on it, main-loop safe)."""
        self._invoke_on_main(lambda: self._emit_frame_stored(event))

    def _handle_executor_snapshot(self, snapshot: ExecutorSnapshot) -> None:
        """Apply an executor snapshot (runs via ``invoke_on_main``)."""
        terminal = snapshot.state in _TERMINAL_EXECUTOR_STATES
        if terminal:
            with self._lock:
                stream = self._stream
                self._stream = None
            if stream is not None:
                try:
                    stream.stop()
                except Exception:
                    logger.debug("preview stop failed (ignored)", exc_info=True)
            if snapshot.state == ExecutorState.DONE:
                self._notify(
                    NotificationSeverity.INFO,
                    "Night finished",
                    "the queue is done — the scope is parked" if snapshot.error is None
                    else f"the night ended: {snapshot.error}",
                )
            else:
                self._notify(
                    NotificationSeverity.ERROR,
                    "Night aborted",
                    snapshot.error or "unknown error",
                )
            self._stop_capture()  # ticket 04: DONE/ABORTED stops the frame capture
        self._emit()

    def _on_stream_frame(self, frame: Any) -> None:
        """Preview-stream callback (dispatcher thread): marshal, decode when
        needed, throttle, notify."""
        self._invoke_on_main(lambda: self._handle_stream_frame(frame))

    def _handle_stream_frame(self, frame: Any) -> None:
        """Apply one preview frame (runs via ``invoke_on_main``): decode raw
        frames via :func:`~seestar_controller.link.preview.decode_payload`,
        keep the latest, and notify the preview listeners + view listeners
        when the throttle allows."""
        if isinstance(frame, PreviewFrame):
            preview_frame = frame
        elif isinstance(frame, Frame):
            preview_frame = PreviewFrame(
                source="tele",
                image=decode_payload(frame),
                frame=frame,
                timestamp=time.monotonic(),
            )
        else:
            logger.warning("ignoring unknown preview frame object %r", frame)
            return
        with self._lock:
            self._latest_frame = preview_frame
        if not self._throttle.allow():
            return  # over max_fps: latest_frame is updated, the notification is not
        for listener in list(self._preview_listeners):
            try:
                listener(preview_frame)
            except Exception:
                logger.exception("preview listener failed")
        self._emit()

    # -- emission ---------------------------------------------------------------------

    def _emit(self) -> None:
        """Build the current :class:`ObserveViewState` and notify the view
        listeners (a failing listener never breaks the others)."""
        snapshot = self.snapshot
        for listener in list(self._listeners):
            try:
                listener(snapshot)
            except Exception:
                logger.exception("observe view listener failed")

    def _emit_frame_stored(self, event: FrameStoredEvent) -> None:
        """Notify the frame-stored listeners (runs via ``invoke_on_main``;
        a failing listener never breaks the others)."""
        for listener in list(self._frame_listeners):
            try:
                listener(event)
            except Exception:
                logger.exception("frame-stored listener failed")
