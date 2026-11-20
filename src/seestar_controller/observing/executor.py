r"""The night executor — a headless-testable state machine driving the queue.

This is the PURE executor layer of :mod:`seestar_controller.observing` (ticket
02): **no ``gi`` import** — stdlib + :mod:`seestar_controller.link` +
:mod:`seestar_controller.store` + :mod:`.queue` only — so the whole night can
be driven and verified headless against the mock Scope (the sandbox has no
GTK, see docs/wiki/assumptions.md; a subprocess test enforces the no-``gi``
boundary). The widget layer (ticket 03) only subscribes to snapshots.

:class:`NightExecutor` walks the plan queue (GLOSSARY: **Plan (queue)**) entry
by entry through an explicit state machine::

    IDLE -> RESOLVING -> SLEWING -> STACKING -> (next entry | DONE)
                     \-> PAUSED (per-target pause, resume/skip/stop)
    any active state --error--> (skip: next entry | abort: ABORTED)

- **RESOLVING** — only for entries that still have an unresolved name: the
  injected ``resolver`` (default: the lazy catalog wrapper, see
  :func:`~.queue.default_name_resolver`) fills the coord; the entry is
  replaced in the wrapped :class:`~.queue.PlanQueue` in place (a ``resolved``
  queue event fires, so the editor shows the coord too);
- **SLEWING** — ``device.goto(ra_hours, dec_deg)`` on the link Device facade
  (blocking until the slew arrives);
- **STACKING** — ``device.start_stack(exposure_s, gain, mosaic)``, then frames
  are counted from the facade's ``Stack`` progress events
  (:meth:`~seestar_controller.link.device.Device.add_stacking_progress_handler`);
  the target ends when ``frames_per_target`` sub-frames are stacked OR
  ``duration_per_target_s`` wall-clock seconds elapsed on the entry (whichever
  comes first; with neither set the target stacks until skip/stop/pause), then
  ``device.stop_stack()``. A ``stack_finished`` event (the Scope stopped the
  Stack on its own) also ends the target;
- **DONE** — the queue is exhausted (or ``stop()`` was commanded): optionally
  ``device.park()`` (``park_at_end=True`` default — spec user story 11), the
  session is closed, terminal state ``DONE``;
- **ABORTED** — an error under ``error_policy="abort"`` (or a session that
  could not be opened): terminal state ``ABORTED`` with ``error`` set.

**Error policy** (documented choice, ticket 02): a Scope error mid-queue — an
exception from a facade call (``RpcError``, ``GotoTimeoutError``,
``ConnectionLostError``, ...) or a pushed ``scope_error`` / ``connection_lost``
event while a step is in flight — is handled per ``error_policy``:

- ``"skip"`` (default) — the error is recorded in the event log and surfaced
  in a snapshot (``error`` set), the current Stack is stopped best-effort, and
  the executor **advances to the next entry** (a flaky target never kills the
  night);
- ``"abort"`` — the Stack is stopped best-effort, the session is closed and
  the executor goes to ``ABORTED`` with the error (a dead link ends the
  night).

**Session** (GLOSSARY: **Session** — one night's run in a **Project**): the
session is opened with :func:`~seestar_controller.store.sessions.start_session`
when :meth:`NightExecutor.start` is called and closed with
:func:`~seestar_controller.store.sessions.close_session` when the run ends
(``DONE`` / ``ABORTED`` / ``stop()``) — both on the **calling thread** of
:meth:`start` / :meth:`run` / :meth:`wait` (the thread that owns the project
db — sqlite connections are thread-affine, so the worker thread never touches
the store). With ``resume_session=<id>`` (ticket 05 — the app was killed
mid-night and the session is still open) :meth:`start` instead **adopts** the
already-open session (verified via
:func:`~seestar_controller.store.sessions.get_open_session`; no new row) and
the run continues in it. The store has no session-log API yet (ticket 04 persists it), so
the executor keeps its own in-memory event log — :attr:`NightExecutor.log`, a
tuple of frozen :class:`ExecutorEvent` ``(ts, state, detail)`` — covering every
transition, per-entry start/finish, errors, session open/close and park.

**Threading** (like the ui :class:`~seestar_controller.ui.connection.ConnectionController`):
:meth:`NightExecutor.start` spawns a daemon worker thread running the loop, so
the blocking facade calls (``goto`` waits for the slew, ...) never run on the
caller's thread; :meth:`run` = start + join and returns the final snapshot;
:meth:`wait` joins the worker (and closes the session). ``pause()`` /
``resume()`` / ``skip()`` / ``stop()`` are thread-safe commands (a
:class:`queue.Queue` consumed by the loop). Every listener notification is
marshalled through ``invoke_on_main`` (the widget layer binds it to
``GLib.idle_add``; the default calls it directly), and listeners receive a
frozen :class:`ExecutorSnapshot` on every transition **and** on every stacking
progress tick (``frames_stacked`` / ``elapsed_s`` updates).

The ``device`` is the link :class:`~seestar_controller.link.device.Device`
facade shape (``goto`` / ``park`` / ``start_stack`` / ``stop_stack`` /
``add_event_handler`` / ``add_stacking_progress_handler`` ...) — injectable, so
tests drive it with the real facade against the mock Scope or with a fake.

NOTE (user AFK, no real scope — docs/wiki/assumptions.md): all link interaction
is exercised against the mock Scope only.
"""

from __future__ import annotations

import logging
import queue as queue_module
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from seestar_controller.link.device import StackingProgress
from seestar_controller.link.resilience import (
    CONNECTION_LOST_EVENT,
    SCOPE_ERROR_EVENT,
    STACK_FINISHED_EVENT,
)
from seestar_controller.store.errors import SessionOpenError
from seestar_controller.store.sessions import (
    close_session,
    get_open_session,
    start_session,
)

from .queue import (
    EVENT_RESOLVED,
    NameResolver,
    PlanQueue,
    QueueEntry,
    _as_coord,
    default_name_resolver,
)

logger = logging.getLogger(__name__)

# --- error policy ---------------------------------------------------------------

#: Error policy: a Scope error mid-queue skips the current entry and advances.
ERROR_POLICY_SKIP = "skip"
#: Error policy: a Scope error mid-queue aborts the whole run (ABORTED).
ERROR_POLICY_ABORT = "abort"
#: All valid ``error_policy`` values.
ERROR_POLICIES: tuple[str, str] = (ERROR_POLICY_SKIP, ERROR_POLICY_ABORT)

# --- internal commands ------------------------------------------------------------

_CMD_PAUSE = "pause"
_CMD_RESUME = "resume"
_CMD_SKIP = "skip"
_CMD_STOP = "stop"


class ExecutorState(str, Enum):
    """The executor's caller-visible state."""

    IDLE = "idle"
    RESOLVING = "resolving"
    SLEWING = "slewing"
    STACKING = "stacking"
    PAUSED = "paused"
    DONE = "done"
    ABORTED = "aborted"

    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.value


#: All executor states, in machine order (PAUSED/DONE/ABORTED aside).
EXECUTOR_STATES: tuple[ExecutorState, ...] = tuple(ExecutorState)

#: The states in which commands (pause/resume/skip/stop) are accepted.
_ACTIVE_STATES: tuple[ExecutorState, ...] = (
    ExecutorState.RESOLVING,
    ExecutorState.SLEWING,
    ExecutorState.STACKING,
    ExecutorState.PAUSED,
)


class ExecutorStateError(RuntimeError):
    """An invalid executor transition (e.g. :meth:`NightExecutor.start`
    while a run is already in flight)."""


@dataclass(frozen=True)
class ExecutorEvent:
    """One entry of the executor's event log (the session log, ticket 04
    persists it into the project).

    - ``ts`` — wall-clock timestamp (:func:`time.time`);
    - ``state`` — the executor state when the event happened;
    - ``detail`` — human-readable detail (entry, error, session, ...).
    """

    ts: float
    state: ExecutorState
    detail: str


@dataclass(frozen=True)
class ExecutorSnapshot:
    """An immutable view of the executor, handed to listeners on every
    transition and every stacking progress tick.

    - ``state`` — the :class:`ExecutorState`;
    - ``queue_index`` — the index of the entry being processed (None in IDLE);
    - ``entry`` — the current :class:`~.queue.QueueEntry` (None in IDLE);
    - ``frames_stacked`` — sub-frames stacked so far on the current entry;
    - ``elapsed_s`` — wall-clock seconds spent on the current entry;
    - ``error`` — the last error message (skip-policy errors, ABORTED), else None;
    - ``session`` — the open session id (set once the run started), else None.
    """

    state: ExecutorState
    queue_index: int | None
    entry: QueueEntry | None
    frames_stacked: int
    elapsed_s: float
    error: str | None
    session: str | None


#: An executor listener: called with an :class:`ExecutorSnapshot` after every
#: transition and progress tick (always via ``invoke_on_main``).
ExecutorListener = Callable[[ExecutorSnapshot], None]

#: Marshals a zero-argument callback onto the "main" thread. The widget layer
#: binds this to ``GLib.idle_add``; the default calls it directly.
InvokeOnMain = Callable[[Callable[[], None]], Any]


def _direct_invoke(callback: Callable[[], None]) -> None:
    """The default ``invoke_on_main``: run the callback directly (tests)."""
    callback()


#: Sentinel for "leave the field unchanged" in :meth:`NightExecutor._transition`.
_UNSET: Any = object()


class NightExecutor:
    """The night executor: drives the device facade through the plan queue.

    - ``device`` — the link Device facade (injectable; tests use the real
      facade against the mock Scope or a fake with the same shape);
    - ``queue`` — the plan: a :class:`~.queue.PlanQueue` (or anything with an
      ``entries`` tuple snapshot, e.g. a
      :class:`~.queue_controller.PlanQueueController`); the entries are
      snapshotted at :meth:`start`, so later edits don't disturb a run;
    - ``project`` — the open store :class:`~seestar_controller.store.project.Project`
      the session is opened in / closed in;
    - ``resolver`` — the name resolver for unresolved entries (default:
      :func:`~.queue.default_name_resolver`);
    - ``frames_per_target`` — stop stacking a target after this many
      sub-frames (counted from ``Stack`` progress events); None = no frame limit;
    - ``duration_per_target_s`` — stop stacking a target after this many
      wall-clock seconds on the entry; None = no time limit. With neither set,
      a target stacks until skip/stop/pause;
    - ``error_policy`` — ``"skip"`` (default) or ``"abort"``, see the module
      docstring;
    - ``resume_session`` — the id of an already-open session to **reattach** to
      instead of opening a new one (ticket 05: the app was killed mid-night
      and the session is still open). :meth:`start` verifies the session is
      really open (via :func:`~seestar_controller.store.sessions.get_open_session`)
      and adopts it — no new row is inserted; the session is still closed by
      :meth:`run` / :meth:`wait` when the run ends. None (default) opens a
      fresh session with :func:`~seestar_controller.store.sessions.start_session`;
    - ``invoke_on_main`` — the marshalling callable for listener notifications
      (default: direct call);
    - ``park_at_end`` — park the Scope when the run ends in DONE (default
      True; not parked on ABORTED);
    - ``poll_interval_s`` — how often the stacking loop wakes to check
      commands/budget (default 0.05 s).
    """

    def __init__(
        self,
        device: Any,
        queue: Any,
        project: Any,
        *,
        resolver: NameResolver | None = None,
        frames_per_target: int | None = None,
        duration_per_target_s: float | None = None,
        error_policy: str = ERROR_POLICY_SKIP,
        resume_session: str | None = None,
        invoke_on_main: InvokeOnMain | None = None,
        park_at_end: bool = True,
        poll_interval_s: float = 0.05,
    ) -> None:
        if error_policy not in ERROR_POLICIES:
            raise ValueError(
                f"error_policy must be one of {ERROR_POLICIES}, got {error_policy!r}"
            )
        if frames_per_target is not None:
            if isinstance(frames_per_target, bool) or not isinstance(frames_per_target, int):
                raise ValueError(
                    f"frames_per_target must be an int or None, got {frames_per_target!r}"
                )
            if frames_per_target < 1:
                raise ValueError(
                    f"frames_per_target must be >= 1 or None, got {frames_per_target}"
                )
        if duration_per_target_s is not None:
            try:
                duration = float(duration_per_target_s)
            except (TypeError, ValueError):
                raise ValueError(
                    f"duration_per_target_s must be a number of seconds or None, "
                    f"got {duration_per_target_s!r}"
                ) from None
            if not duration > 0:
                raise ValueError(
                    f"duration_per_target_s must be > 0 or None, got {duration_per_target_s}"
                )
        if poll_interval_s <= 0:
            raise ValueError(f"poll_interval_s must be > 0, got {poll_interval_s}")
        if resume_session is not None and (
            not isinstance(resume_session, str) or not resume_session
        ):
            raise ValueError(
                f"resume_session must be a non-empty session id or None, got {resume_session!r}"
            )
        entries = getattr(queue, "entries", None)
        if not isinstance(entries, tuple) or not all(
            isinstance(e, QueueEntry) for e in entries
        ):
            raise ValueError(
                "queue must be a PlanQueue (or expose an 'entries' tuple of "
                f"QueueEntry), got {queue!r}"
            )
        self._device = device
        self._queue = queue
        self._project = project
        self._resolver: NameResolver = (
            resolver if resolver is not None else default_name_resolver
        )
        self._frames_per_target = frames_per_target
        self._duration_per_target_s = (
            float(duration_per_target_s) if duration_per_target_s is not None else None
        )
        self._error_policy = error_policy
        self._resume_session = resume_session
        self._invoke_on_main: InvokeOnMain = invoke_on_main or _direct_invoke
        self._park_at_end = bool(park_at_end)
        self._poll_interval_s = float(poll_interval_s)

        self._lock = threading.Lock()
        self._state = ExecutorState.IDLE
        self._queue_index: int | None = None
        self._entry: QueueEntry | None = None
        self._frames_stacked = 0
        self._entry_start: float | None = None
        self._error: str | None = None
        self._session: str | None = None
        self._session_closed = False
        self._log: list[ExecutorEvent] = []
        self._listeners: list[ExecutorListener] = []
        self._commands: queue_module.Queue[str] = queue_module.Queue()
        self._worker: threading.Thread | None = None
        # async error flags, set by device event handlers (reader thread)
        self._pending_error: str | None = None
        self._stack_finished = False

    # -- accessors ---------------------------------------------------------------

    @property
    def device(self) -> Any:
        """The device facade being driven."""
        return self._device

    @property
    def queue(self) -> Any:
        """The plan queue (snapshot taken at :meth:`start`)."""
        return self._queue

    @property
    def project(self) -> Any:
        """The project the session lives in."""
        return self._project

    @property
    def error_policy(self) -> str:
        """The error policy: ``"skip"`` or ``"abort"``."""
        return self._error_policy

    @property
    def resume_session(self) -> str | None:
        """The id of the open session this executor reattaches to (ticket 05),
        or None when it opens a fresh session."""
        return self._resume_session

    @property
    def park_at_end(self) -> bool:
        """Whether the Scope is parked when the run ends in DONE."""
        return self._park_at_end

    @property
    def state(self) -> ExecutorState:
        """The current :class:`ExecutorState`."""
        with self._lock:
            return self._state

    @property
    def session(self) -> str | None:
        """The session id of the current/last run (None before :meth:`start`)."""
        with self._lock:
            return self._session

    @property
    def error(self) -> str | None:
        """The last error message (skip-policy errors, ABORTED), else None."""
        with self._lock:
            return self._error

    @property
    def snapshot(self) -> ExecutorSnapshot:
        """An immutable :class:`ExecutorSnapshot` of the current state."""
        with self._lock:
            return self._snapshot_locked()

    @property
    def log(self) -> tuple[ExecutorEvent, ...]:
        """The executor's event log (session log) as a tuple of frozen
        :class:`ExecutorEvent` — every transition, per-entry start/finish,
        errors, session open/close, park. Ticket 04 persists it into the
        session."""
        with self._lock:
            return tuple(self._log)

    def _snapshot_locked(self) -> ExecutorSnapshot:
        elapsed = 0.0
        if self._entry_start is not None:
            elapsed = max(0.0, time.monotonic() - self._entry_start)
        return ExecutorSnapshot(
            state=self._state,
            queue_index=self._queue_index,
            entry=self._entry,
            frames_stacked=self._frames_stacked,
            elapsed_s=elapsed,
            error=self._error,
            session=self._session,
        )

    # -- listeners ---------------------------------------------------------------

    @property
    def listeners(self) -> tuple[ExecutorListener, ...]:
        """The registered observer callbacks, in registration order."""
        return tuple(self._listeners)

    def add_listener(self, listener: ExecutorListener) -> ExecutorListener:
        """Register an observer callback (idempotent); returns it, so it can
        be used as a decorator. The listener receives an
        :class:`ExecutorSnapshot` after every transition and progress tick,
        marshalled via ``invoke_on_main``."""
        if listener not in self._listeners:
            self._listeners.append(listener)
        return listener

    def remove_listener(self, listener: ExecutorListener) -> None:
        """Unregister an observer callback (no-op when not registered)."""
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _notify(self, snapshot: ExecutorSnapshot) -> None:
        """Call every listener with ``snapshot`` (runs via ``invoke_on_main``;
        a failing listener never breaks the executor or the other listeners)."""
        for listener in list(self._listeners):
            try:
                listener(snapshot)
            except Exception:
                logger.exception("executor listener failed")

    def _emit(self, snapshot: ExecutorSnapshot) -> None:
        """Marshal a listener notification through ``invoke_on_main``."""
        self._invoke_on_main(lambda: self._notify(snapshot))

    # -- event log -----------------------------------------------------------------

    def _log_event(self, state: ExecutorState, detail: str) -> None:
        """Append one :class:`ExecutorEvent` to the log (thread-safe)."""
        with self._lock:
            self._log.append(ExecutorEvent(ts=time.time(), state=state, detail=detail))

    # -- transitions -----------------------------------------------------------------

    def _transition(
        self,
        state: ExecutorState,
        *,
        index: Any = _UNSET,
        entry: Any = _UNSET,
        frames: Any = _UNSET,
        error: Any = _UNSET,
        log_detail: str | None = None,
    ) -> ExecutorSnapshot:
        """Apply a state transition on the worker thread and notify the
        listeners (via ``invoke_on_main``) with the new snapshot."""
        with self._lock:
            self._state = state
            if index is not _UNSET:
                self._queue_index = index
            if entry is not _UNSET:
                self._entry = entry
            if frames is not _UNSET:
                self._frames_stacked = frames
            if error is not _UNSET:
                self._error = error
            if state in (ExecutorState.RESOLVING, ExecutorState.SLEWING, ExecutorState.STACKING):
                self._entry_start = time.monotonic()
            if state in (ExecutorState.IDLE, ExecutorState.DONE, ExecutorState.ABORTED):
                self._entry_start = None
            snapshot = self._snapshot_locked()
            if log_detail is not None:
                self._log.append(
                    ExecutorEvent(ts=time.time(), state=state, detail=log_detail)
                )
        self._emit(snapshot)
        return snapshot

    # -- commands (thread-safe) --------------------------------------------------------

    def _command(self, cmd: str) -> None:
        """Enqueue a command when a run is active (no-op otherwise)."""
        with self._lock:
            active = self._state in _ACTIVE_STATES
        if active:
            self._commands.put(cmd)

    def pause(self) -> None:
        """Pause the current target (per-target pause): during STACKING the
        Stack is stopped and the executor waits in PAUSED; otherwise the pause
        is honored at the next checkpoint. No-op when no run is active."""
        self._command(_CMD_PAUSE)

    def resume(self) -> None:
        """Resume from PAUSED (restarts the Stack with the accumulated frame
        budget). No-op when not paused."""
        self._command(_CMD_RESUME)

    def skip(self) -> None:
        """Skip the current entry and advance to the next one (works from
        PAUSED too). No-op when no run is active."""
        self._command(_CMD_SKIP)

    def stop(self) -> None:
        """Stop the night gracefully: the current Stack is stopped, the run
        ends in DONE (parked when ``park_at_end``), the session is closed by
        :meth:`run` / :meth:`wait`. No-op when no run is active."""
        self._command(_CMD_STOP)

    def _drain_command(self) -> str | None:
        """Non-blocking: the oldest queued command, if any."""
        try:
            return self._commands.get_nowait()
        except queue_module.Empty:
            return None

    # -- start / run / wait --------------------------------------------------------------

    def start(self) -> threading.Thread | None:
        """Start the night: open the session (on the calling thread — the
        thread that owns the project db) and spawn the worker thread running
        the loop.

        With ``resume_session`` set (ticket 05), the already-open session is
        **adopted** instead of opening a new one: :meth:`start` verifies via
        :func:`~seestar_controller.store.sessions.get_open_session` that the
        session is really open and uses its id (no new row is inserted).

        Returns the worker :class:`threading.Thread`, or ``None`` when the
        session could not be opened (or resumed) — in that case the executor
        goes to ``ABORTED`` with the error and no worker runs.

        Raises :class:`ExecutorStateError` when a run is already in flight
        (state not IDLE — wait for DONE/ABORTED first).
        """
        with self._lock:
            if self._state != ExecutorState.IDLE:
                raise ExecutorStateError(
                    f"cannot start while {self._state.value} — wait for DONE/ABORTED"
                )
            self._session_closed = False
            self._pending_error = None
            self._stack_finished = False
            self._error = None
        while True:  # drain stale commands from an earlier run
            try:
                self._commands.get_nowait()
            except queue_module.Empty:
                break
        resumed = self._resume_session is not None
        try:
            if resumed:
                open_session = get_open_session(self._project)
                if open_session is None or open_session.id != self._resume_session:
                    raise SessionOpenError(
                        f"cannot resume session {self._resume_session!r} — no such "
                        f"open session in project "
                        f"{getattr(self._project, 'slug', self._project)!r}"
                    )
                session_id = self._resume_session
            else:
                session_id = start_session(self._project)
        except Exception as exc:  # noqa: BLE001
            message = f"{type(exc).__name__}: {exc}"
            self._log_event(ExecutorState.ABORTED, f"session open failed: {message}")
            self._transition(
                ExecutorState.ABORTED, error=message, log_detail="session open failed"
            )
            return None
        with self._lock:
            self._session = session_id
        self._log_event(
            ExecutorState.IDLE, f"session {session_id} {'resumed' if resumed else 'opened'}"
        )
        worker = threading.Thread(
            target=self._run, name="seestar-observing-executor", daemon=True
        )
        with self._lock:
            self._worker = worker
        worker.start()
        return worker

    def run(self, timeout: float | None = None) -> ExecutorSnapshot:
        """Run the night to completion on a worker thread: :meth:`start` +
        join + close the session (on the calling thread). Returns the final
        :class:`ExecutorSnapshot` (DONE or ABORTED).

        With a ``timeout`` and a still-running worker, returns the current
        snapshot without closing the session (the run is still in flight —
        call :meth:`wait` later to close it)."""
        worker = self.start()
        if worker is not None:
            worker.join(timeout)
            if worker.is_alive():
                return self.snapshot
        self._close_session()
        return self.snapshot

    def wait(self, timeout: float | None = None) -> bool:
        """Join the worker (if any) and close the session on the calling
        thread once the run has finished. Returns True when no worker is
        running (or it finished within ``timeout``)."""
        with self._lock:
            worker = self._worker
        if worker is None:
            self._close_session()
            return True
        worker.join(timeout)
        finished = not worker.is_alive()
        if finished:
            self._close_session()
        return finished

    def _close_session(self) -> None:
        """Close the run's session (idempotent, on the calling thread — the
        thread that owns the project db)."""
        with self._lock:
            session_id = self._session
            if session_id is None or self._session_closed:
                return
            self._session_closed = True
        try:
            close_session(self._project, session_id)
            self._log_event(self.state, f"session {session_id} closed")
        except Exception as exc:  # noqa: BLE001
            logger.warning("close_session(%s) failed: %s", session_id, exc)
            self._log_event(self.state, f"session {session_id} close failed: {exc}")

    # -- device event handlers (run on the transport reader thread) ----------------------

    def _arm_device(self) -> None:
        """Subscribe to the device events the loop reacts to."""
        self._device.add_stacking_progress_handler(self._on_stacking_progress)
        self._device.add_event_handler(SCOPE_ERROR_EVENT, self._on_scope_error)
        self._device.add_event_handler(CONNECTION_LOST_EVENT, self._on_connection_lost)
        self._device.add_event_handler(STACK_FINISHED_EVENT, self._on_stack_finished)

    def _disarm_device(self) -> None:
        """Remove the device subscriptions (best-effort)."""
        try:
            self._device.remove_stacking_progress_handler(self._on_stacking_progress)
        except Exception:  # noqa: BLE001, S110
            pass
        for name, handler in (
            (SCOPE_ERROR_EVENT, self._on_scope_error),
            (CONNECTION_LOST_EVENT, self._on_connection_lost),
            (STACK_FINISHED_EVENT, self._on_stack_finished),
        ):
            try:
                self._device.remove_event_handler(name, handler)
            except Exception:  # noqa: BLE001, S110
                pass

    def _on_stacking_progress(self, progress: StackingProgress) -> None:
        """A ``Stack`` progress event: update the frame count and notify the
        listeners with a progress snapshot (state stays STACKING)."""
        with self._lock:
            if self._state != ExecutorState.STACKING:
                return
            self._frames_stacked = int(progress.frames)
            snapshot = self._snapshot_locked()
        self._emit(snapshot)

    def _on_scope_error(self, event: Any) -> None:
        """A ``scope_error`` event: record it as a pending async error (the
        loop handles it per the error policy; an error raised by our own
        facade call is handled via the exception and clears this flag)."""
        payload = getattr(event, "payload", {}) or {}
        message = (
            f"scope_error on {payload.get('method', '?')!r}: "
            f"code {payload.get('code')} {payload.get('result')}"
        )
        with self._lock:
            self._pending_error = message
        self._log_event(self.state, message)

    def _on_connection_lost(self, event: Any) -> None:
        """A ``connection_lost`` event: the Link dropped mid-queue — record it
        as a pending async error (handled per the error policy)."""
        payload = getattr(event, "payload", {}) or {}
        message = f"connection lost: {payload.get('reason', 'unknown')}"
        with self._lock:
            self._pending_error = message
        self._log_event(self.state, message)

    def _on_stack_finished(self, event: Any) -> None:
        """A ``stack_finished`` event: the Scope stopped the Stack on its own."""
        with self._lock:
            self._stack_finished = True

    def _consume_pending_error(self) -> str | None:
        """The pending async error (scope_error / connection_lost), if any,
        cleared."""
        with self._lock:
            error = self._pending_error
            self._pending_error = None
            return error

    def _consume_stack_finished(self) -> bool:
        """Whether a ``stack_finished`` event arrived since the last check."""
        with self._lock:
            finished = self._stack_finished
            self._stack_finished = False
            return finished

    # -- the run loop (worker thread) ------------------------------------------------------

    def _run(self) -> None:
        """Worker thread body: arm the device events, walk the queue, park on
        DONE, and leave the terminal transition + session close to the caller
        thread (:meth:`run` / :meth:`wait`)."""
        entries = tuple(self._queue.entries)  # snapshot at run start
        self._arm_device()
        try:
            outcome, error = self._loop(entries)
        except Exception as exc:  # unexpected internal failure -> abort
            logger.exception("executor loop crashed")
            outcome, error = "abort", f"{type(exc).__name__}: {exc}"
        finally:
            self._disarm_device()
        if outcome == "abort":
            self._transition(
                ExecutorState.ABORTED, error=error, log_detail=f"aborted: {error}"
            )
            return
        if outcome == "stop":
            self._log_event(ExecutorState.DONE, "stopped by user")
        if self._park_at_end:
            try:
                self._device.park()
                self._log_event(ExecutorState.DONE, "parked at end of run")
            except Exception as exc:  # noqa: BLE001
                message = f"{type(exc).__name__}: {exc}"
                logger.warning("park at end failed: %s", message)
                self._log_event(ExecutorState.DONE, f"park failed: {message}")
        self._transition(ExecutorState.DONE, error=None, log_detail="run done")

    def _loop(self, entries: tuple[QueueEntry, ...]) -> tuple[str, str | None]:
        """Walk the queue; returns ``(outcome, error)`` with outcome one of
        ``"done"`` / ``"stop"`` / ``"abort"``."""
        index = 0
        while index < len(entries):
            outcome = self._process_entry(index, entries[index])
            if outcome in ("stop", "abort"):
                return outcome, self.error
            index += 1  # "next" or "skip"
        return "done", None

    def _process_entry(self, index: int, entry: QueueEntry) -> str:
        """Run one queue entry through RESOLVING -> SLEWING -> STACKING.
        Returns ``"next"`` / ``"skip"`` / ``"stop"`` / ``"abort"``."""
        # -- RESOLVING (only for entries with an unresolved name) ----------------
        if not entry.is_resolved:
            self._transition(
                ExecutorState.RESOLVING,
                index=index,
                entry=entry,
                frames=0,
                error=None,
                log_detail=f"resolving {entry.name!r}",
            )
            cmd = self._drain_command()
            if cmd is not None:
                handled = self._handle_checkpoint(cmd, index, entry, ExecutorState.RESOLVING)
                if handled is not None:
                    return handled
            pending = self._consume_pending_error()
            if pending is not None:
                return self._handle_error(index, entry, pending)
            try:
                coord = _as_coord(self._resolver(entry.name))  # ty: ignore[invalid-argument-type]
                resolved = QueueEntry.resolved(
                    entry.name,  # ty: ignore[invalid-argument-type]
                    coord,
                    mosaic=entry.mosaic,
                    exposure_s=entry.exposure_s,
                    gain=entry.gain,
                )
            except Exception as exc:  # noqa: BLE001
                return self._handle_error(index, entry, exc)
            if isinstance(self._queue, PlanQueue):
                try:
                    self._queue._replace(index, resolved, EVENT_RESOLVED)
                except Exception:
                    logger.exception("could not replace the resolved entry in the queue")
            entry = resolved
            self._transition(
                ExecutorState.RESOLVING,
                index=index,
                entry=entry,
                log_detail=(
                    f"resolved {entry.name!r} -> RA {coord.ra_hours:.4f}h "
                    f"Dec {coord.dec_deg:+.4f}°"
                ),
            )
        # -- SLEWING ---------------------------------------------------------------
        assert entry.coord is not None
        self._transition(
            ExecutorState.SLEWING,
            index=index,
            entry=entry,
            frames=0,
            error=None,
            log_detail=(
                f"goto RA {entry.coord.ra_hours:.4f}h Dec {entry.coord.dec_deg:+.4f}°"
            ),
        )
        try:
            self._device.goto(entry.coord.ra_hours, entry.coord.dec_deg)
        except Exception as exc:  # noqa: BLE001
            return self._handle_error(index, entry, exc)
        cmd = self._drain_command()
        if cmd is not None:
            handled = self._handle_checkpoint(cmd, index, entry, ExecutorState.SLEWING)
            if handled is not None:
                return handled
        pending = self._consume_pending_error()
        if pending is not None:
            return self._handle_error(index, entry, pending)
        # -- STACKING ---------------------------------------------------------------
        return self._stack_entry(index, entry)

    def _handle_checkpoint(
        self, cmd: str, index: int, entry: QueueEntry, phase: ExecutorState
    ) -> str | None:
        """Handle a command drained at a checkpoint. Returns the outcome
        (``"skip"`` / ``"stop"`` / ``"abort"``) or None when the command was
        ``resume``/``pause`` and the entry should continue in ``phase``."""
        if cmd == _CMD_SKIP:
            self._log_event(phase, f"entry {index} skipped by user")
            return "skip"
        if cmd == _CMD_STOP:
            return "stop"
        if cmd == _CMD_PAUSE:
            outcome = self._wait_while_paused(index, entry, phase)
            if outcome == "resume":
                return None  # continue the entry in `phase`
            return outcome
        if cmd == _CMD_RESUME:
            return None  # stale resume (not paused) — ignore
        return None

    def _wait_while_paused(
        self, index: int, entry: QueueEntry, phase: ExecutorState
    ) -> str:
        """Enter PAUSED and block until resume/skip/stop. Returns ``"resume"``
        (re-enter ``phase``), ``"skip"`` or ``"stop"``."""
        self._transition(
            ExecutorState.PAUSED,
            index=index,
            entry=entry,
            log_detail=f"paused on entry {index}",
        )
        while True:
            try:
                cmd = self._commands.get(timeout=self._poll_interval_s)
            except queue_module.Empty:
                cmd = None
            if cmd == _CMD_RESUME:
                self._transition(
                    phase, index=index, entry=entry, log_detail=f"resumed entry {index}"
                )
                return "resume"
            if cmd == _CMD_SKIP:
                self._log_event(ExecutorState.PAUSED, f"entry {index} skipped by user")
                return "skip"
            if cmd == _CMD_STOP:
                return "stop"
            pending = self._consume_pending_error()
            if pending is not None:
                # the link dropped / the scope errored while paused
                self._log_event(ExecutorState.PAUSED, f"error while paused: {pending}")
                if self._error_policy == ERROR_POLICY_ABORT:
                    with self._lock:
                        self._error = pending
                    return "abort"
                return "skip"

    def _stack_entry(self, index: int, entry: QueueEntry) -> str:
        """STACKING phase for one entry: start the Stack, count frames from
        progress events, stop on budget/skip/stop/pause/async error. Returns
        the outcome (``"next"`` / ``"skip"`` / ``"stop"`` / ``"abort"``)."""
        self._transition(
            ExecutorState.STACKING,
            index=index,
            entry=entry,
            frames=0,
            error=None,
            log_detail=(
                f"start_stack exposure={entry.exposure_s:g}s gain={entry.gain} "
                f"mosaic={entry.mosaic!r}"
            ),
        )
        with self._lock:
            self._stack_finished = False
        try:
            self._device.start_stack(entry.exposure_s, entry.gain, entry.mosaic)
        except Exception as exc:  # noqa: BLE001
            return self._handle_error(index, entry, exc)
        while True:
            try:
                cmd = self._commands.get(timeout=self._poll_interval_s)
            except queue_module.Empty:
                cmd = None
            if cmd == _CMD_STOP:
                self._stop_stack_quietly()
                return "stop"
            if cmd == _CMD_SKIP:
                self._stop_stack_quietly()
                self._log_event(ExecutorState.STACKING, f"entry {index} skipped by user")
                return "skip"
            if cmd == _CMD_PAUSE:
                self._stop_stack_quietly()
                outcome = self._wait_while_paused(index, entry, ExecutorState.STACKING)
                if outcome != "resume":
                    return outcome
                # resumed: restart the Stack, the frame budget accumulated so far
                with self._lock:
                    self._stack_finished = False
                try:
                    self._device.start_stack(entry.exposure_s, entry.gain, entry.mosaic)
                except Exception as exc:  # noqa: BLE001
                    return self._handle_error(index, entry, exc)
                continue
            pending = self._consume_pending_error()
            if pending is not None:
                self._stop_stack_quietly()
                return self._handle_error(index, entry, pending)
            if self._consume_stack_finished():
                # the Scope stopped the Stack on its own
                self._log_event(ExecutorState.STACKING, f"stack finished on entry {index}")
                return "next"
            with self._lock:
                frames = self._frames_stacked
                entry_start = self._entry_start
            elapsed = (
                max(0.0, time.monotonic() - entry_start) if entry_start is not None else 0.0
            )
            if self._frames_per_target is not None and frames >= self._frames_per_target:
                self._stop_stack_quietly()
                self._log_event(
                    ExecutorState.STACKING,
                    f"entry {index} done: {frames} sub-frames stacked",
                )
                return "next"
            if (
                self._duration_per_target_s is not None
                and elapsed >= self._duration_per_target_s
            ):
                self._stop_stack_quietly()
                self._log_event(
                    ExecutorState.STACKING,
                    f"entry {index} done: {elapsed:.1f}s elapsed, {frames} sub-frames",
                )
                return "next"

    def _stop_stack_quietly(self) -> None:
        """Best-effort ``stop_stack`` (a failing stop must not mask the real
        outcome — e.g. the link is already down)."""
        try:
            self._device.stop_stack()
        except Exception as exc:  # noqa: BLE001
            logger.debug("stop_stack failed (ignored): %s", exc)

    def _handle_error(self, index: int, entry: QueueEntry, error: Any) -> str:
        """Handle a step error per the error policy. Returns ``"skip"`` or
        ``"abort"``."""
        if isinstance(error, BaseException):
            message = f"{type(error).__name__}: {error}"
        else:
            message = str(error)
        with self._lock:  # an exception from our own call supersedes a pending event
            self._pending_error = None
        self._log_event(self.state, f"error on entry {index}: {message}")
        if self._error_policy == ERROR_POLICY_ABORT:
            with self._lock:
                self._error = message
            return "abort"
        # skip policy: surface the error in a snapshot, then advance
        self._transition(self.state, index=index, entry=entry, error=message)
        return "skip"
