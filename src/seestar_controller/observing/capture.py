r"""Session capture — data capture during the night (observing ticket 04).

PURE executor layer of :mod:`seestar_controller.observing`: **no ``gi``**
import, but it does import the link (frame downloads) + the store (frame
storage, session lookup) + the ui pure layer (notification bus), so it is
re-exported **lazily** from the package ``__init__`` (PEP 562
``_EXECUTOR_EXPORTS``) — importing ``seestar_controller.observing`` alone
never pulls the link or the store into ``sys.modules``.

What it does
------------

:class:`SessionCapture` runs alongside a :class:`~.executor.NightExecutor`
during a night and captures the data as it arrives:

- **frame downloads** — a worker thread polls
  :meth:`~seestar_controller.link.device.Device.download_frames`
  (``since=`` the per-kind newest frame seen so far) every ``poll_interval_s``
  AND
  wakes up immediately on every pushed ``Stack`` progress event
  (:meth:`~...device.Device.add_stacking_progress_handler`), for both kinds
  (``"sub"`` sub-frames and ``"stack"`` Stacks). Each downloaded frame is
  stored into the current session via
  :func:`~seestar_controller.store.frames.store_frame` (FITS file under
  ``subs/<session>/`` / ``stacks/<session>/`` + the ``frames`` db row),
  deduplicated by the Scope frame id;
- **session event log** — :class:`SessionLog` records timestamped events:
  the executor's own log (goto / stack start-stop / errors / session
  open-close, mirrored from :attr:`~.executor.NightExecutor.log`) plus the
  capture's own events (frames stored, download/store errors, capture
  start/stop).

Where the event log lives (documented choice)
---------------------------------------------

The store has **no session-events API** (its schema is
``sessions``/``frames``/``stars``/``measurements`` only, and this ticket may
not touch ``store/``), so the log is kept in memory by :class:`SessionLog`
and persisted as **JSONL** at ``subs/<session_id>/session_log.jsonl`` — the
session directory (the session id doubles as the ``subs/<session>/`` dir
name) — rewritten atomically on every append (temp file + fsync +
:func:`os.replace`, via
:func:`~seestar_controller.link.frames.atomic_write_bytes`, so a crash never
leaves a partial file). JSONL keeps the log greppable and line-oriented;
re-opening a capture in the same session (:meth:`SessionLog.load`) re-reads
the file, which also re-seeds the frame dedupe. The executor's in-memory
``log`` stays the source of truth for the run; this file is its per-session
persistence.

Threading model (documented choice)
-----------------------------------

sqlite connections are thread-affine (verified: cross-thread use raises
``sqlite3.ProgrammingError``), so — like the executor — **all store calls
happen on the thread that owns the project db** (the "main" thread; the app
binds ``GLib.idle_add`` via ``invoke_on_main``). The capture worker thread
only does the network downloads and then *schedules* the store step:
``invoke_on_main(lambda: self._apply(items))``. :meth:`SessionCapture.poll_once`
runs one full cycle synchronously on the **calling** thread (used by tests
and by :meth:`stop`'s final cycle). Capture errors (download/store failures)
become ``error`` log events + bus notifications — they never kill the night.

NOTE (user AFK, no real scope — docs/wiki/assumptions.md): all link
interaction is exercised against the mock Scope only.
"""

from __future__ import annotations

import json
import logging
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from seestar_controller.link.frames import (
    KIND_STACK,
    KIND_SUB,
    atomic_write_bytes,
    meta_to_sidecar,
)
from seestar_controller.store.frames import store_frame
from seestar_controller.store.project import SUBS_DIR
from seestar_controller.store.sessions import list_sessions
from seestar_controller.ui.notifications import (
    Notification,
    NotificationBus,
    NotificationSeverity,
)

logger = logging.getLogger(__name__)

#: The ``source`` of capture-derived notifications.
CAPTURE_SOURCE = "capture"

#: The session event-log file name inside the session directory.
SESSION_LOG_FILE = "session_log.jsonl"

#: Session-log event kinds.
KIND_GOTO = "goto"
KIND_STACK = "stack"
KIND_ERROR = "error"
KIND_FRAME = "frame"
KIND_SESSION = "session"
KIND_CAPTURE = "capture"
KIND_EXECUTOR = "executor"

#: All valid :class:`LogEvent` kinds.
LOG_KINDS = (
    KIND_GOTO,
    KIND_STACK,
    KIND_ERROR,
    KIND_FRAME,
    KIND_SESSION,
    KIND_CAPTURE,
    KIND_EXECUTOR,
)

#: The log sources: executor-mirrored events vs the capture's own events.
LOG_SOURCE_EXECUTOR = "executor"
LOG_SOURCE_CAPTURE = "capture"
LOG_SOURCES = (LOG_SOURCE_EXECUTOR, LOG_SOURCE_CAPTURE)

#: A session-log listener: called with the new :class:`LogEvent` on every
#: append (synchronously, on the appending thread).
LogListener = Callable[["LogEvent"], None]


def _direct_invoke(callback: Callable[[], None]) -> None:
    """The default ``invoke_on_main``: run the callback directly (tests)."""
    callback()
    return None


def session_log_path(project: Any, session_id: str) -> Path:
    """The path of the session event log:
    ``<project>/subs/<session_id>/session_log.jsonl`` (the session
    directory — the session id doubles as the ``subs/<session>/`` dir name)."""
    return Path(project.path) / SUBS_DIR / session_id / SESSION_LOG_FILE


def classify_executor_event(detail: str) -> str:
    """Map an executor log detail to a session-log kind.

    ``"goto RA …"`` -> ``goto``; anything mentioning an error / failure /
    lost connection / abort -> ``error``; stack-related details
    (``start_stack …``, ``entry N done: … sub-frames stacked``,
    ``stack finished …``) -> ``stack``; ``"session …"`` -> ``session``;
    everything else (resolving, resolved, skipped, stopped, parked) ->
    ``executor``.
    """
    text = detail.lower()
    if text.startswith("goto"):
        return KIND_GOTO
    if any(word in text for word in ("error", "failed", "lost", "aborted")):
        return KIND_ERROR
    if "stack" in text:
        return KIND_STACK
    if text.startswith("session"):
        return KIND_SESSION
    return KIND_EXECUTOR


@dataclass(frozen=True)
class LogEvent:
    """One entry of the session event log.

    - ``ts`` — wall-clock timestamp (:func:`time.time`, epoch seconds);
    - ``kind`` — one of :data:`LOG_KINDS`;
    - ``detail`` — human-readable detail;
    - ``source`` — :data:`LOG_SOURCE_EXECUTOR` when mirrored from the
      executor's log, else :data:`LOG_SOURCE_CAPTURE`;
    - ``state`` — the executor state value when mirrored, else ``""``;
    - ``frame_id`` — the Scope frame id for ``frame`` events (the dedupe
      key), else None.
    """

    ts: float
    kind: str
    detail: str
    source: str = LOG_SOURCE_CAPTURE
    state: str = ""
    frame_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """The JSONL record of this event."""
        return {
            "ts": self.ts,
            "kind": self.kind,
            "detail": self.detail,
            "source": self.source,
            "state": self.state,
            "frame_id": self.frame_id,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> LogEvent:
        """Parse one JSONL record back into a :class:`LogEvent`."""
        return cls(
            ts=float(raw["ts"]),
            kind=str(raw["kind"]),
            detail=str(raw.get("detail", "")),
            source=str(raw.get("source", LOG_SOURCE_CAPTURE)),
            state=str(raw.get("state", "")),
            frame_id=raw.get("frame_id"),
        )


class SessionLog:
    """The per-session event log: in-memory + a JSONL file (see the module
    docstring for where the file lives and why).

    Every :meth:`append` adds the event in memory, rewrites the whole JSONL
    file atomically (the log is small — a night's events), and notifies the
    listeners synchronously (a failing listener never breaks the others).
    :meth:`load` re-reads an existing file (restart within the same
    session), skipping corrupt lines.
    """

    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        self._path = Path(path)
        self._clock = clock
        self._events: list[LogEvent] = []
        self._listeners: list[LogListener] = []
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        """The JSONL file this log persists to."""
        return self._path

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)

    def events(self) -> tuple[LogEvent, ...]:
        """A snapshot of all events, oldest first."""
        with self._lock:
            return tuple(self._events)

    @property
    def listeners(self) -> tuple[LogListener, ...]:
        """The registered listeners, in registration order."""
        return tuple(self._listeners)

    def add_listener(self, listener: LogListener) -> LogListener:
        """Register a listener (idempotent); returns it (decorator form)."""
        if listener not in self._listeners:
            self._listeners.append(listener)
        return listener

    def remove_listener(self, listener: LogListener) -> None:
        """Unregister a listener (no-op when not registered)."""
        if listener in self._listeners:
            self._listeners.remove(listener)

    def append(
        self,
        kind: str,
        detail: str,
        *,
        source: str = LOG_SOURCE_CAPTURE,
        state: str = "",
        frame_id: str | None = None,
        ts: float | None = None,
    ) -> LogEvent:
        """Append one event: in memory + atomically persisted + listeners
        notified. Returns the frozen :class:`LogEvent`."""
        if kind not in LOG_KINDS:
            raise ValueError(f"kind must be one of {LOG_KINDS}, got {kind!r}")
        event = LogEvent(
            ts=self._clock() if ts is None else float(ts),
            kind=kind,
            detail=str(detail),
            source=source,
            state=state,
            frame_id=frame_id,
        )
        with self._lock:
            self._events.append(event)
            self._persist_locked()
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(event)
            except Exception:
                logger.exception("session log listener failed")
        return event

    def load(self) -> int:
        """Re-read the JSONL file into memory (restart within the same
        session); returns the number of events loaded. A missing file
        yields 0; corrupt lines are skipped (and logged)."""
        with self._lock:
            self._events = []
            count = 0
            if self._path.exists():
                for line in self._path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = LogEvent.from_dict(json.loads(line))
                    except (ValueError, KeyError, TypeError):
                        logger.warning("skipping corrupt line in %s", self._path)
                        continue
                    if event.kind not in LOG_KINDS:
                        logger.warning("skipping unknown kind in %s", self._path)
                        continue
                    self._events.append(event)
                    count += 1
            return count

    def _persist_locked(self) -> None:
        """Rewrite the whole JSONL file atomically (caller holds the lock)."""
        data = "".join(
            json.dumps(event.to_dict(), sort_keys=True) + "\n" for event in self._events
        ).encode("utf-8")
        atomic_write_bytes(self._path, data)


class SessionCapture:
    """Data capture for one session of a night: downloads sub-frames/Stacks
    into the session as they arrive + keeps the session event log.

    - ``device`` — the link Device facade (or a duck-typed double);
    - ``project`` — the store project the session lives in (construct on the
      thread that owns the project db — the constructor reads the session
      row);
    - ``session_id`` — the open session's id;
    - ``executor`` — the running :class:`~.executor.NightExecutor` whose log
      is mirrored into the session log (optional);
    - ``poll_interval_s`` — the download poll interval (the worker also
      wakes on every pushed ``Stack`` progress event);
    - ``invoke_on_main`` — marshals the store step onto the db-owning
      thread (the app binds ``GLib.idle_add``; default: direct call);
    - ``bus`` — the notification bus for capture errors (optional);
    - ``download_timeout`` — per-request timeout for the downloads;
    - ``clock`` — the wall clock (tests inject a fake).

    Use :meth:`start` / :meth:`stop` for the worker-driven capture, or
    :meth:`poll_once` to run one cycle synchronously on the calling thread
    (tests). Capture errors never raise — they become ``error`` events and
    bus notifications.
    """

    def __init__(
        self,
        device: Any,
        project: Any,
        session_id: str,
        *,
        executor: Any = None,
        poll_interval_s: float = 0.5,
        invoke_on_main: Callable[[Callable[[], None]], Any] | None = None,
        bus: NotificationBus | None = None,
        download_timeout: float | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(session_id, str) or not session_id:
            raise ValueError(f"session_id must be a non-empty string, got {session_id!r}")
        if poll_interval_s <= 0:
            raise ValueError(f"poll_interval_s must be > 0, got {poll_interval_s!r}")
        self._device = device
        self._project = project
        self._session_id = session_id
        self._executor = executor
        self._poll_interval_s = float(poll_interval_s)
        self._invoke_on_main = invoke_on_main or _direct_invoke
        self._bus = bus
        self._download_timeout = download_timeout
        self._clock = clock
        self._log = SessionLog(session_log_path(project, session_id), clock=clock)
        self._log.load()
        self._stored_ids: set[str] = {
            event.frame_id for event in self._log.events() if event.frame_id
        }
        # per-kind "since" watermark: a Stack is recorded when stacking stops,
        # i.e. AFTER the last sub-frames, so a single watermark would skip
        # subs recorded just before the Stack but downloaded in a later poll
        start_epoch = self._session_start_epoch()
        self._since: dict[str, float] = {KIND_SUB: start_epoch, KIND_STACK: start_epoch}
        self._mirrored = 0
        self._cycle_lock = threading.Lock()  # serializes download cycles
        self._apply_lock = threading.Lock()  # serializes store/log applies
        self._lock = threading.Lock()  # running/worker state
        self._stop_event = threading.Event()
        self._wake = threading.Event()
        self._running = False
        self._worker: threading.Thread | None = None
        self._armed = False
        self._download_missing = False

    # -- accessors ---------------------------------------------------------------

    @property
    def device(self) -> Any:
        """The device frames are downloaded from."""
        return self._device

    @property
    def project(self) -> Any:
        """The store project the session lives in."""
        return self._project

    @property
    def session_id(self) -> str:
        """The captured session's id."""
        return self._session_id

    @property
    def executor(self) -> Any:
        """The executor whose log is mirrored (None when not given)."""
        return self._executor

    @property
    def log(self) -> SessionLog:
        """The session event log (:class:`SessionLog`)."""
        return self._log

    @property
    def running(self) -> bool:
        """Whether the capture worker is running."""
        with self._lock:
            return self._running

    @property
    def worker(self) -> threading.Thread | None:
        """The capture worker thread (None before :meth:`start`)."""
        with self._lock:
            return self._worker

    @property
    def poll_interval_s(self) -> float:
        """The download poll interval."""
        return self._poll_interval_s

    @property
    def since(self) -> float:
        """The newest frame epoch seen so far (the max over the per-kind
        watermarks; the next download's ``since`` per kind)."""
        return max(self._since.values())

    @property
    def stored_frame_ids(self) -> frozenset[str]:
        """The Scope frame ids already stored in the session (the dedupe set)."""
        return frozenset(self._stored_ids)

    def events(self) -> tuple[LogEvent, ...]:
        """A snapshot of the session event log, oldest first."""
        return self._log.events()

    @property
    def listeners(self) -> tuple[LogListener, ...]:
        """The registered log listeners."""
        return self._log.listeners

    def add_listener(self, listener: LogListener) -> LogListener:
        """Register a log listener (idempotent); returns it (decorator form)."""
        return self._log.add_listener(listener)

    def remove_listener(self, listener: LogListener) -> None:
        """Unregister a log listener (no-op when not registered)."""
        self._log.remove_listener(listener)

    # -- lifecycle -----------------------------------------------------------------

    def start(self) -> threading.Thread | None:
        """Start the capture worker: arm the ``Stack``-event wakeup, log
        "capture started", spawn the daemon worker thread. Returns the
        worker thread (the same one when already running)."""
        with self._lock:
            if self._running:
                return self._worker
            self._running = True
            self._stop_event.clear()
            self._wake.clear()
        self._arm_device()
        if getattr(self._device, "download_frames", None) is None:
            self._download_missing = True
            self._log.append(
                KIND_ERROR, "device has no download_frames — frame capture disabled"
            )
            self._notify_bus(
                NotificationSeverity.WARNING,
                "Frame capture unavailable",
                "the connected device cannot download frames",
            )
        self._log.append(
            KIND_CAPTURE,
            f"capture started for session {self._session_id} "
            f"(poll every {self._poll_interval_s:g}s)",
        )
        worker = threading.Thread(
            target=self._run, name="seestar-observing-capture", daemon=True
        )
        with self._lock:
            self._worker = worker
        worker.start()
        return worker

    def stop(self) -> None:
        """Stop the capture (idempotent, never raises): stop the worker, run
        one final cycle on the CALLING thread (catches the last Stack),
        disarm the device handler, log "capture stopped"."""
        with self._lock:
            if not self._running:
                return
            self._running = False
            worker = self._worker
        self._stop_event.set()
        self._wake.set()
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=10.0)
        self._disarm_device()
        try:
            self.poll_once()  # final cycle on the calling thread
        except Exception:
            logger.exception("final capture cycle failed")
        self._log.append(KIND_CAPTURE, "capture stopped")

    def poll_once(self) -> int:
        """One full capture cycle on the CALLING thread (which must own the
        project db): mirror new executor log entries, download the frames
        newer than :attr:`since` and store them (dedupe by frame id).

        Returns the number of frames stored. This is what the worker
        schedules via ``invoke_on_main``; tests call it directly.
        """
        with self._cycle_lock:
            items = self._download_new_frames()
            return self._apply(items)

    # -- worker loop -----------------------------------------------------------------

    def _run(self) -> None:
        """Worker loop: wait for the poll interval OR a ``Stack`` event, then
        run one cycle (download here, store via ``invoke_on_main``)."""
        while not self._stop_event.is_set():
            self._wake.wait(self._poll_interval_s)
            self._wake.clear()
            if self._stop_event.is_set():
                break
            self._cycle_on_main()

    def _cycle_on_main(self) -> None:
        """Worker: download the new frames (network, this thread), then
        schedule the store step on the db-owning thread."""
        try:
            with self._cycle_lock:
                items = self._download_new_frames()
        except Exception as exc:  # noqa: BLE001 - logged as an error event
            items = [("error", exc)]
        self._invoke_on_main(lambda: self._apply(items))

    def _on_stack_progress(self, progress: Any) -> None:
        """A pushed ``Stack`` progress event: wake the worker to poll promptly."""
        self._wake.set()

    # -- download + store --------------------------------------------------------------

    def _download_new_frames(self) -> list[tuple]:
        """Download the frames newer than the per-kind ``since`` watermark into
        a scratch dir (network — safe on any thread). Returns items:
        ``("frame", kind, meta, fits_bytes)`` or ``("error", exc)``. Updates
        the per-kind watermark to the newest frame epoch of that kind."""
        if self._download_missing or getattr(self._device, "download_frames", None) is None:
            return []
        items: list[tuple] = []
        with tempfile.TemporaryDirectory(prefix="seestar-capture-") as scratch:
            for kind in (KIND_SUB, KIND_STACK):
                try:
                    downloaded = self._device.download_frames(
                        scratch, kind=kind, since=self._since[kind], timeout=self._download_timeout
                    )
                except Exception as exc:  # noqa: BLE001 - one error per cycle; retried next poll
                    items.append(("error", exc))
                    break
                for frame in downloaded:
                    meta = frame.meta
                    try:
                        fits = Path(frame.path).read_bytes()
                    except OSError as exc:
                        items.append(("error", exc))
                        continue
                    items.append(("frame", kind, meta, fits))
                    if meta.epoch >= self._since[kind]:
                        self._since[kind] = meta.epoch
        return items

    def _apply(self, items: list[tuple]) -> int:
        """Store the downloaded frames + mirror the executor log. Must run on
        the thread that owns the project db (scheduled via ``invoke_on_main``).
        Returns the number of frames stored; never raises."""
        stored = 0
        try:
            with self._apply_lock:
                self._mirror_executor_log()
                for item in items:
                    if item[0] == "error":
                        exc = item[1]
                        message = f"{type(exc).__name__}: {exc}"
                        self._log.append(KIND_ERROR, f"frame download failed: {message}")
                        self._notify_bus(
                            NotificationSeverity.ERROR, "Frame download failed", message
                        )
                        continue
                    _, kind, meta, fits = item
                    if meta.id in self._stored_ids:
                        continue  # already stored (re-poll / restart)
                    try:
                        frame = store_frame(
                            self._project,
                            self._session_id,
                            kind,
                            fits,
                            meta=meta_to_sidecar(meta, size=len(fits)),
                        )
                    except Exception as exc:  # noqa: BLE001 - logged, never kills the night
                        message = f"{type(exc).__name__}: {exc}"
                        self._log.append(
                            KIND_ERROR,
                            f"could not store {kind} frame {meta.id}: {message}",
                        )
                        self._notify_bus(
                            NotificationSeverity.ERROR,
                            "Frame store failed",
                            f"{kind} frame {meta.id}: {message}",
                        )
                        continue
                    self._stored_ids.add(meta.id)
                    stored += 1
                    self._log.append(
                        KIND_FRAME,
                        f"stored {kind} frame {meta.id} -> {frame.path} "
                        f"({len(fits)} bytes)",
                        frame_id=meta.id,
                    )
        except Exception:
            logger.exception("capture apply failed")
        return stored

    def _mirror_executor_log(self) -> None:
        """Mirror the executor log entries not yet in the session log."""
        if self._executor is None:
            return
        log = self._executor.log
        if len(log) < self._mirrored:
            self._mirrored = 0  # defensive: the executor restarted
        for event in log[self._mirrored :]:
            state = event.state
            self._log.append(
                classify_executor_event(event.detail),
                event.detail,
                source=LOG_SOURCE_EXECUTOR,
                state=getattr(state, "value", str(state)),
                ts=event.ts,
            )
        self._mirrored = len(log)

    # -- helpers -----------------------------------------------------------------------

    def _session_start_epoch(self) -> float:
        """The session's start time as epoch seconds (the initial ``since``);
        falls back to now when the session row cannot be read."""
        try:
            for session in list_sessions(self._project):
                if session.id == self._session_id:
                    return datetime.fromisoformat(session.started_at).timestamp()
        except Exception:
            logger.debug("could not read the session row (using now as since)", exc_info=True)
        return self._clock()

    def _arm_device(self) -> None:
        """Register the ``Stack``-progress wakeup on the device (best-effort)."""
        add = getattr(self._device, "add_stacking_progress_handler", None)
        if add is None:
            return
        try:
            add(self._on_stack_progress)
            self._armed = True
        except Exception:
            logger.debug("could not arm the stack-progress handler", exc_info=True)

    def _disarm_device(self) -> None:
        """Remove the ``Stack``-progress wakeup (best-effort)."""
        remove = getattr(self._device, "remove_stacking_progress_handler", None)
        if self._armed and remove is not None:
            try:
                remove(self._on_stack_progress)
            except Exception:
                logger.debug("could not disarm the stack-progress handler", exc_info=True)
        self._armed = False

    def _notify_bus(self, severity: NotificationSeverity, title: str, message: str) -> None:
        """Push a notification to the bus (when given); never raises."""
        if self._bus is None:
            return
        try:
            self._bus.push(Notification(severity, title, message, CAPTURE_SOURCE))
        except Exception:
            logger.exception("could not push capture notification")
