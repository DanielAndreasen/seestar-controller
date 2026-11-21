"""App-level controllers for astrometry ticket 03 — the PURE layer
(:mod:`seestar_controller.astrometry.controller`).

Pure-Python (module boundary per ``.scratch/astrometry/spec.md``): no GTK
imports, no :mod:`seestar_controller.link` / ``ui`` / ``enhance`` /
``photometry`` imports — the pure :mod:`seestar_controller.store` + the
sibling pure modules only. The widget layer (:mod:`.view`) observes these
controllers; the app owns one shared instance of each and passes it down via
:func:`seestar_controller.ui.views.build_view`.

- :class:`SolveController` (+ :class:`SolveSnapshot`) — the "Solve" action of
  the image viewer: it follows the shared viewer controller (duck-typed —
  NOT imported here — the open stack becomes the solve target), runs
  :func:`~.session.solve_project_frame` on the open stack (which writes the
  WCS into the FITS header and persists it via ``store.wcs``), and exports
  the annotated PNG (:mod:`.export`). Operations never raise — failures are
  recorded in the snapshot's ``error`` (the widget shows them as a toast /
  status), because they run on a worker thread;
- :class:`DriftController` (+ :class:`DriftSnapshot`) — the drift plot of the
  session views: it follows the viewer controller's project, lists the
  project's sessions, auto-picks the open session (else the latest), computes
  the astrometry-02 :func:`~.drift.drift_series`, and renders it to PNG bytes
  (:mod:`.plot`).

Threading / sqlite thread-affinity: like the gaia
:class:`~seestar_controller.gaia.overlay.CrossmatchController`, every STORE
call goes through :meth:`_call_store` (direct on the db-owning thread,
otherwise marshalled through ``invoke_on_main`` with a wait — the app binds
``GLib.idle_add``), so the widgets can run the operations on a worker thread.
Listener notifications are marshalled through ``invoke_on_main`` too.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from seestar_controller.store.frames import list_frames
from seestar_controller.store.project import Project
from seestar_controller.store.sessions import Session, get_open_session, list_sessions
from seestar_controller.store.wcs import FrameWcs, load_frame_wcs

from .drift import DriftSeries, drift_series
from .errors import AstrometryError
from .export import (
    DEFAULT_EXPORT_MAX_SIZE,
    default_export_name,
    export_annotated_png,
)
from .plot import DEFAULT_DPI, DEFAULT_HEIGHT_PX, DEFAULT_WIDTH_PX, render_drift_png
from .session import solve_project_frame

logger = logging.getLogger(__name__)

#: How long a worker thread waits for a store call marshalled to the
#: db-owning thread before giving up (same default as the gaia controller).
DB_CALL_TIMEOUT_S = 30.0

InvokeOnMain = Callable[[Callable[[], None]], Any]


def _direct_invoke(callback: Callable[[], None]) -> None:
    callback()


# --- solve controller ---------------------------------------------------------------------


@dataclass(frozen=True)
class SolveSnapshot:
    """An immutable snapshot of the solve panel's state, handed to the
    listeners on any change.

    - ``project_name`` — the current project's display name (None when no
      project is set);
    - ``stack_frame_id`` — the frame id of the open stack (the solve
      target; None when no stack is open);
    - ``stack_label`` — the open stack's one-line label (None when none);
    - ``solving`` — True while a solve is running on the worker thread;
    - ``solved`` — the persisted WCS record of the open stack
      (:func:`~seestar_controller.store.wcs.load_frame_wcs`), None when the
      stack has not been solved yet;
    - ``last_solver`` / ``last_center`` / ``last_rms_arcsec`` /
      ``last_n_stars`` — the outcome of the last solve (None before the
      first);
    - ``status`` — a short human-readable status (e.g. ``"solved: 12 stars,
      RMS 0.42″"``), else None;
    - ``error`` — the last error message, else None.
    """

    project_name: str | None
    stack_frame_id: int | None
    stack_label: str | None
    solving: bool
    solved: FrameWcs | None
    last_solver: str | None
    last_center: tuple[float, float] | None
    last_rms_arcsec: float | None
    last_n_stars: int | None
    status: str | None
    error: str | None


SolveListener = Callable[[SolveSnapshot], None]


class SolveController:
    """The app-level solve controller: the current project, the open stack
    (followed from the shared viewer controller), its persisted WCS, and the
    solve / annotated-export operations.

    - ``project`` — the store project (None = follow the attached viewer
      controller's project, see :meth:`attach_viewer`);
    - ``viewer_controller`` — an optional duck-typed viewer controller (the
      shared enhance viewer controller — NOT imported here): its project is
      adopted when no explicit ``project`` is given, and its currently open
      stack is always adopted, so the solve panel follows the stack open in
      the image viewer;
    - ``invoke_on_main`` — the marshalling callable for listener
      notifications AND for store calls made from a non-db thread (the app
      binds ``GLib.idle_add``; the default calls directly).

    Operations: :attr:`snapshot` (never raises), :meth:`set_project`,
    :meth:`attach_viewer` / :meth:`detach_viewer`, :meth:`refresh_solved`,
    :meth:`solve_open_stack` (plate-solve the open stack — never raises),
    :meth:`export_annotated_png` (write the annotated PNG of the open stack
    to ``path`` — never raises), and listeners with frozen snapshots.
    """

    def __init__(
        self,
        project: Project | None = None,
        viewer_controller: Any | None = None,
        *,
        invoke_on_main: InvokeOnMain | None = None,
    ) -> None:
        self._project: Project | None = None
        self._viewer_controller: Any | None = None
        self._invoke_on_main: InvokeOnMain = invoke_on_main or _direct_invoke
        self._lock = threading.Lock()
        self._listeners: list[SolveListener] = []
        self._db_thread_id: int | None = None
        self._stack_frame_id: int | None = None
        self._stack_label: str | None = None
        self._solving = False
        self._solved: FrameWcs | None = None
        self._last_solver: str | None = None
        self._last_center: tuple[float, float] | None = None
        self._last_rms_arcsec: float | None = None
        self._last_n_stars: int | None = None
        self._status: str | None = None
        self._error: str | None = None
        # set_project FIRST: it clears the stack state, so attaching the
        # viewer afterwards (which adopts the viewer's open stack) must come
        # last — the other order would wipe the just-adopted stack when both
        # ``project`` and ``viewer_controller`` are given.
        if project is not None:
            self.set_project(project)
        if viewer_controller is not None:
            self.attach_viewer(viewer_controller)

    # -- project + viewer -----------------------------------------------------------

    @property
    def project(self) -> Project | None:
        """The current store project (None when not set yet)."""
        return self._project

    @property
    def viewer_controller(self) -> Any | None:
        """The viewer controller this controller follows (if any)."""
        return self._viewer_controller

    @property
    def invoke_on_main(self) -> InvokeOnMain:
        """The marshalling callable (the app binds ``GLib.idle_add``)."""
        return self._invoke_on_main

    def attach_viewer(self, viewer_controller: Any) -> None:
        """Follow ``viewer_controller`` (duck-typed): register as its
        listener and adopt its project (when no explicit project is set) and
        its currently open stack. Re-attaching detaches the previous viewer
        first."""
        if self._viewer_controller is not None:
            self._viewer_controller.remove_listener(self._on_viewer_snapshot)
        self._viewer_controller = viewer_controller
        viewer_controller.add_listener(self._on_viewer_snapshot)
        if self._project is None:
            self.set_project(getattr(viewer_controller, "project", None))
        snapshot = getattr(viewer_controller, "snapshot", None)
        if snapshot is not None:
            self._on_viewer_snapshot(snapshot)

    def detach_viewer(self) -> None:
        """Stop following the viewer controller."""
        if self._viewer_controller is not None:
            self._viewer_controller.remove_listener(self._on_viewer_snapshot)
            self._viewer_controller = None

    def _on_viewer_snapshot(self, snapshot: Any) -> None:
        """Viewer listener: adopt the viewer's project (when no explicit
        project is set) and its open stack, so the solve panel follows the
        stack open in the image viewer. Emits on every viewer change, so
        closing the stack clears the panel."""
        viewer = self._viewer_controller
        if viewer is None:
            return
        if self._project is None:
            self.set_project(getattr(viewer, "project", None))
        current = getattr(snapshot, "current", None)
        if current is not None:
            frame_id = getattr(current, "frame_id", None)
            if isinstance(frame_id, int) and frame_id != self._stack_frame_id:
                label_fn = getattr(current, "label", None)
                label = label_fn() if callable(label_fn) else None
                self._adopt_stack(frame_id, label)
        elif current is None and self._stack_frame_id is not None:
            with self._lock:
                self._stack_frame_id = None
                self._stack_label = None
                self._solved = None
                self._status = None
                self._error = None
            self._emit(self.snapshot)

    def _adopt_stack(self, frame_id: int, label: str | None) -> None:
        """Adopt the viewer's open stack: load its persisted WCS record (if
        any), clear the last-solve outcome, notify."""
        solved: FrameWcs | None = None
        error: str | None = None
        project = self._project
        if project is not None:
            try:
                solved = self._call_store(lambda: load_frame_wcs(project, frame_id))
            except Exception as exc:  # surfaced as snapshot.error, never raised  # noqa: BLE001
                logger.info("loading the frame WCS failed: %s", exc)
                error = f"{type(exc).__name__}: {exc}"
        with self._lock:
            self._stack_frame_id = frame_id
            self._stack_label = label
            self._solved = solved
            self._last_solver = None
            self._last_center = None
            self._last_rms_arcsec = None
            self._last_n_stars = None
            self._status = (
                f"solved ({solved.solver}, {solved.n_stars} stars)"
                if solved is not None
                else None
            )
            self._error = error
        self._emit(self.snapshot)

    def set_project(self, project: Project | None) -> None:
        """Set/replace the current project (None clears it): the stack state
        is cleared. Notifies."""
        self._project = project
        self._db_thread_id = threading.get_ident() if project is not None else None
        with self._lock:
            self._stack_frame_id = None
            self._stack_label = None
            self._solved = None
            self._last_solver = None
            self._last_center = None
            self._last_rms_arcsec = None
            self._last_n_stars = None
            self._status = None
            self._error = None
        self._emit(self.snapshot)

    # -- operations ------------------------------------------------------------------

    @property
    def stack_frame_id(self) -> int | None:
        """The frame id of the open stack (None when none is open)."""
        return self._stack_frame_id

    @property
    def stack_label(self) -> str | None:
        """The one-line label of the open stack (None when none)."""
        return self._stack_label

    def refresh_solved(self) -> SolveSnapshot:
        """Re-load the open stack's persisted WCS record (e.g. after another
        view solved it); notifies and returns the snapshot. Never raises."""
        project = self._project
        frame_id = self._stack_frame_id
        if project is None or frame_id is None:
            self._emit(self.snapshot)
            return self.snapshot
        try:
            solved = self._call_store(lambda: load_frame_wcs(project, frame_id))
            with self._lock:
                self._solved = solved
                self._error = None
                self._status = (
                    f"solved ({solved.solver}, {solved.n_stars} stars)"
                    if solved is not None
                    else None
                )
        except Exception as exc:  # surfaced as snapshot.error, never raised  # noqa: BLE001
            logger.info("refresh_solved failed: %s", exc)
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"
        self._emit(self.snapshot)
        return self.snapshot

    def solve_open_stack(self, *, solver: str = "auto", **solve_kwargs: Any) -> SolveSnapshot:
        """Plate-solve the open stack: :func:`~.session.solve_project_frame`
        (writes the WCS into the stored FITS header, persists the WCS record
        via ``store.wcs`` and the cross-match rows), then re-load the record.
        Never raises — a failure (no project/stack, :class:`~.solve.SolveError`,
        store error) is recorded in the snapshot's ``error`` and the status is
        cleared. Notifies (``solving`` True before, the outcome after) and
        returns the final snapshot."""
        project = self._project
        frame_id = self._stack_frame_id
        if project is None:
            with self._lock:
                self._error = "no project set — open a project first"
            self._emit(self.snapshot)
            return self.snapshot
        if frame_id is None:
            with self._lock:
                self._error = "no stack open — open a stack in the image viewer first"
            self._emit(self.snapshot)
            return self.snapshot
        with self._lock:
            self._solving = True
            self._error = None
            self._status = "solving…"
        self._emit(self.snapshot)
        try:
            result = self._call_store(
                lambda: solve_project_frame(project, frame_id, solver=solver, **solve_kwargs)
            )
            solved = self._call_store(lambda: load_frame_wcs(project, frame_id))
            rms = result.rms_arcsec
            with self._lock:
                self._solving = False
                self._solved = solved
                self._last_solver = result.solver
                self._last_center = (result.center_ra_deg, result.center_dec_deg)
                self._last_rms_arcsec = rms
                self._last_n_stars = result.n_stars
                self._error = None
                self._status = (
                    f"solved: {result.n_stars} stars"
                    + (f", RMS {rms:.2f}\u2033" if rms is not None else "")
                    + f" ({result.solver})"
                )
        except Exception as exc:  # surfaced as snapshot.error, never raised  # noqa: BLE001
            logger.info("solve_open_stack failed: %s", exc)
            with self._lock:
                self._solving = False
                self._error = f"{type(exc).__name__}: {exc}"
                self._status = None
        self._emit(self.snapshot)
        return self.snapshot

    def export_annotated_png(
        self,
        path: str | Path,
        *,
        max_size: int = DEFAULT_EXPORT_MAX_SIZE,
    ) -> SolveSnapshot:
        """Write the annotated PNG of the open stack to ``path`` (see
        :func:`~.export.export_annotated_png`); the status reports the written
        file. Never raises — a failure is recorded in the snapshot's
        ``error``. Notifies and returns the snapshot."""
        project = self._project
        frame_id = self._stack_frame_id
        if project is None or frame_id is None:
            with self._lock:
                self._error = "no stack open — open a stack in the image viewer first"
            self._emit(self.snapshot)
            return self.snapshot
        try:
            out = self._call_store(
                lambda: export_annotated_png(project, frame_id, path, max_size=max_size)
            )
            with self._lock:
                self._error = None
                self._status = f"exported {out}"
        except Exception as exc:  # surfaced as snapshot.error, never raised  # noqa: BLE001
            logger.info("export_annotated_png failed: %s", exc)
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"
                self._status = None
        self._emit(self.snapshot)
        return self.snapshot

    def default_export_path(self, directory: str | Path) -> Path:
        """The default export path of the open stack inside ``directory``
        (:func:`~.export.default_export_name`); raises
        :class:`~.errors.AstrometryError` when no stack is open."""
        frame_id = self._stack_frame_id
        project = self._project
        if project is None or frame_id is None:
            raise AstrometryError("no stack open — open a stack in the image viewer first")
        frame = next((f for f in list_frames(project) if f.id == frame_id), None)
        if frame is None:
            raise AstrometryError(f"the open stack {frame_id!r} is not in the project")
        return Path(directory) / default_export_name(frame)

    # -- snapshot + listeners ---------------------------------------------------------

    @property
    def snapshot(self) -> SolveSnapshot:
        """The frozen :class:`SolveSnapshot` of the current state."""
        with self._lock:
            return SolveSnapshot(
                project_name=self._project.name if self._project is not None else None,
                stack_frame_id=self._stack_frame_id,
                stack_label=self._stack_label,
                solving=self._solving,
                solved=self._solved,
                last_solver=self._last_solver,
                last_center=self._last_center,
                last_rms_arcsec=self._last_rms_arcsec,
                last_n_stars=self._last_n_stars,
                status=self._status,
                error=self._error,
            )

    @property
    def listeners(self) -> tuple[SolveListener, ...]:
        """The registered observer callbacks, in registration order."""
        return tuple(self._listeners)

    def add_listener(self, listener: SolveListener) -> SolveListener:
        """Register an observer callback (idempotent); returns it, so it can
        be used as a decorator."""
        if listener not in self._listeners:
            self._listeners.append(listener)
        return listener

    def remove_listener(self, listener: SolveListener) -> None:
        """Unregister an observer callback (no-op when not registered)."""
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _notify(self, snapshot: SolveSnapshot) -> None:
        for listener in list(self._listeners):
            try:
                listener(snapshot)
            except Exception:
                logger.exception("solve listener failed")

    def _emit(self, snapshot: SolveSnapshot) -> None:
        """Marshal a listener notification through ``invoke_on_main``."""
        self._invoke_on_main(lambda: self._notify(snapshot))

    # -- internals ---------------------------------------------------------------------

    def _call_store(self, fn: Callable[[], Any]) -> Any:
        """Run the store callable ``fn`` and return its result — direct on
        the db-owning thread, otherwise marshalled through
        ``invoke_on_main`` with a wait (the same pattern as the gaia
        cross-match controller)."""
        db_thread = self._db_thread_id
        if db_thread is None or db_thread == threading.get_ident():
            return fn()
        box: dict[str, Any] = {}
        done = threading.Event()

        def wrapper() -> None:
            try:
                box["result"] = fn()
            except BaseException as exc:  # re-raised on the calling thread  # noqa: BLE001
                box["error"] = exc
            finally:
                done.set()

        self._invoke_on_main(wrapper)
        if not done.wait(DB_CALL_TIMEOUT_S):
            raise AstrometryError(
                "timed out waiting for the project db thread — is the main "
                "loop running?"
            )
        if "error" in box:
            raise box["error"]
        return box["result"]


# --- drift controller ---------------------------------------------------------------------


@dataclass(frozen=True)
class DriftSnapshot:
    """An immutable snapshot of the drift plot's state, handed to the
    listeners on any change.

    - ``project_name`` — the current project's display name (None when no
      project is set);
    - ``sessions`` — the project's sessions (ordered by ``started_at``);
    - ``session_id`` — the selected session (the open one, else the latest;
      None when the project has no sessions);
    - ``series`` — the current :class:`~.drift.DriftSeries` (None until a
      successful :meth:`DriftController.refresh`);
    - ``status`` — a short human-readable status (e.g. ``"3 solved frames,
      RA drift +1.20″/h"``), else None;
    - ``error`` — the last error message, else None.
    """

    project_name: str | None
    sessions: tuple[Session, ...]
    session_id: str | None
    series: DriftSeries | None
    status: str | None
    error: str | None


DriftListener = Callable[[DriftSnapshot], None]


class DriftController:
    """The app-level drift controller: the current project (followed from
    the shared viewer controller), its sessions, the selected session's
    :class:`~.drift.DriftSeries`, and the PNG render of it.

    - ``project`` — the store project (None = follow the attached viewer
      controller's project);
    - ``viewer_controller`` — an optional duck-typed viewer controller (the
      shared enhance viewer controller — NOT imported here): its project is
      adopted whenever no explicit project is set;
    - ``invoke_on_main`` — the marshalling callable for listener
      notifications AND for store calls made from a non-db thread (the app
      binds ``GLib.idle_add``; the default calls directly).

    Operations: :attr:`snapshot` (never raises), :meth:`set_project`,
    :meth:`attach_viewer` / :meth:`detach_viewer`, :meth:`set_session`,
    :meth:`refresh` (compute the drift series — never raises),
    :meth:`render_png` (render the current series to PNG bytes), and
    listeners with frozen snapshots.
    """

    def __init__(
        self,
        project: Project | None = None,
        viewer_controller: Any | None = None,
        *,
        invoke_on_main: InvokeOnMain | None = None,
    ) -> None:
        self._project: Project | None = None
        self._viewer_controller: Any | None = None
        self._invoke_on_main: InvokeOnMain = invoke_on_main or _direct_invoke
        self._lock = threading.Lock()
        self._listeners: list[DriftListener] = []
        self._db_thread_id: int | None = None
        self._sessions: tuple[Session, ...] = ()
        self._session_id: str | None = None
        self._series: DriftSeries | None = None
        self._status: str | None = None
        self._error: str | None = None
        if viewer_controller is not None:
            self.attach_viewer(viewer_controller)
        if project is not None:
            self.set_project(project)

    # -- project + viewer -----------------------------------------------------------

    @property
    def project(self) -> Project | None:
        """The current store project (None when not set yet)."""
        return self._project

    @property
    def viewer_controller(self) -> Any | None:
        """The viewer controller this controller follows (if any)."""
        return self._viewer_controller

    @property
    def invoke_on_main(self) -> InvokeOnMain:
        """The marshalling callable (the app binds ``GLib.idle_add``)."""
        return self._invoke_on_main

    def attach_viewer(self, viewer_controller: Any) -> None:
        """Follow ``viewer_controller`` (duck-typed): register as its
        listener and adopt its project (when no explicit project is set), so
        the drift plot follows the project open in the image viewer."""
        if self._viewer_controller is not None:
            self._viewer_controller.remove_listener(self._on_viewer_snapshot)
        self._viewer_controller = viewer_controller
        viewer_controller.add_listener(self._on_viewer_snapshot)
        if self._project is None:
            self.set_project(getattr(viewer_controller, "project", None))

    def detach_viewer(self) -> None:
        """Stop following the viewer controller."""
        if self._viewer_controller is not None:
            self._viewer_controller.remove_listener(self._on_viewer_snapshot)
            self._viewer_controller = None

    def _on_viewer_snapshot(self, snapshot: Any) -> None:
        """Viewer listener: adopt the viewer's project when no explicit
        project is set."""
        if self._project is None:
            self.set_project(getattr(self._viewer_controller, "project", None))

    def set_project(self, project: Project | None) -> None:
        """Set/replace the current project (None clears it): the sessions are
        re-listed and the open session (else the latest) is auto-selected.
        Notifies."""
        self._project = project
        self._db_thread_id = threading.get_ident() if project is not None else None
        with self._lock:
            self._sessions = ()
            self._session_id = None
            self._series = None
            self._status = None
            self._error = None
        if project is not None:
            try:
                sessions = tuple(self._call_store(lambda: list_sessions(project)))
                open_session = self._call_store(lambda: get_open_session(project))
                with self._lock:
                    self._sessions = sessions
                    self._session_id = (
                        open_session.id
                        if open_session is not None
                        else (sessions[-1].id if sessions else None)
                    )
                    self._error = None
            except Exception as exc:  # surfaced as snapshot.error, never raised  # noqa: BLE001
                logger.info("listing the sessions failed: %s", exc)
                with self._lock:
                    self._error = f"{type(exc).__name__}: {exc}"
        self._emit(self.snapshot)

    # -- session + series --------------------------------------------------------------

    @property
    def sessions(self) -> tuple[Session, ...]:
        """The current project's sessions (ordered by ``started_at``)."""
        return self._sessions

    @property
    def session_id(self) -> str | None:
        """The selected session id (None when the project has none)."""
        return self._session_id

    @property
    def series(self) -> DriftSeries | None:
        """The current drift series (None until a successful refresh)."""
        return self._series

    def set_session(self, session_id: str | None) -> DriftSnapshot:
        """Select the session ``session_id`` (None clears the selection and
        the series); notifies and returns the snapshot."""
        if session_id is not None and not isinstance(session_id, str):
            raise TypeError(f"session_id must be a string or None, got {session_id!r}")
        with self._lock:
            self._session_id = session_id
            self._series = None
            self._status = None
            self._error = None
        self._emit(self.snapshot)
        return self.snapshot

    def refresh(self, entry: Any = None) -> DriftSnapshot:
        """Recompute the selected session's drift series
        (:func:`~.drift.drift_series`); never raises — an unknown session
        (:class:`~seestar_controller.store.errors.SessionNotFoundError`), a
        session without solved frames, or a missing requested target
        (:class:`~.drift.DriftError`) is recorded in the snapshot's
        ``error``. Notifies and returns the snapshot."""
        project = self._project
        session_id = self._session_id
        if project is None:
            with self._lock:
                self._error = "no project set — open a project first"
            self._emit(self.snapshot)
            return self.snapshot
        if session_id is None:
            with self._lock:
                self._error = "no session — the project has no sessions yet"
            self._emit(self.snapshot)
            return self.snapshot
        try:
            series = self._call_store(lambda: drift_series(project, session_id, entry))
            with self._lock:
                self._series = series
                self._error = None
                ra_rate = series.ra_drift_arcsec_per_hour
                dec_rate = series.dec_drift_arcsec_per_hour
                self._status = (
                    f"{series.n_solved}/{series.n_frames} solved frames"
                    + (f" · RA {ra_rate:+.2f}\u2033/h" if ra_rate is not None else "")
                    + (f" · Dec {dec_rate:+.2f}\u2033/h" if dec_rate is not None else "")
                )
        except Exception as exc:  # surfaced as snapshot.error, never raised  # noqa: BLE001
            logger.info("drift refresh failed: %s", exc)
            with self._lock:
                self._series = None
                self._error = f"{type(exc).__name__}: {exc}"
                self._status = None
        self._emit(self.snapshot)
        return self.snapshot

    def render_png(
        self,
        *,
        width_px: int = DEFAULT_WIDTH_PX,
        height_px: int = DEFAULT_HEIGHT_PX,
        dpi: int = DEFAULT_DPI,
    ) -> bytes:
        """Render the current series to PNG bytes
        (:func:`~.plot.render_drift_png`). Raises :class:`~.drift.DriftError`
        when no series has been computed yet (call :meth:`refresh` first) or
        a size is invalid."""
        with self._lock:
            series = self._series
        if series is None:
            from .drift import DriftError

            raise DriftError(
                "no drift series — select a session and refresh first"
            )
        return render_drift_png(series, width_px=width_px, height_px=height_px, dpi=dpi)

    # -- snapshot + listeners ---------------------------------------------------------

    @property
    def snapshot(self) -> DriftSnapshot:
        """The frozen :class:`DriftSnapshot` of the current state."""
        with self._lock:
            return DriftSnapshot(
                project_name=self._project.name if self._project is not None else None,
                sessions=self._sessions,
                session_id=self._session_id,
                series=self._series,
                status=self._status,
                error=self._error,
            )

    @property
    def listeners(self) -> tuple[DriftListener, ...]:
        """The registered observer callbacks, in registration order."""
        return tuple(self._listeners)

    def add_listener(self, listener: DriftListener) -> DriftListener:
        """Register an observer callback (idempotent); returns it, so it can
        be used as a decorator."""
        if listener not in self._listeners:
            self._listeners.append(listener)
        return listener

    def remove_listener(self, listener: DriftListener) -> None:
        """Unregister an observer callback (no-op when not registered)."""
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _notify(self, snapshot: DriftSnapshot) -> None:
        for listener in list(self._listeners):
            try:
                listener(snapshot)
            except Exception:
                logger.exception("drift listener failed")

    def _emit(self, snapshot: DriftSnapshot) -> None:
        """Marshal a listener notification through ``invoke_on_main``."""
        self._invoke_on_main(lambda: self._notify(snapshot))

    # -- internals ---------------------------------------------------------------------

    def _call_store(self, fn: Callable[[], Any]) -> Any:
        """Run the store callable ``fn`` and return its result — direct on
        the db-owning thread, otherwise marshalled through
        ``invoke_on_main`` with a wait (the same pattern as the gaia
        cross-match controller)."""
        db_thread = self._db_thread_id
        if db_thread is None or db_thread == threading.get_ident():
            return fn()
        box: dict[str, Any] = {}
        done = threading.Event()

        def wrapper() -> None:
            try:
                box["result"] = fn()
            except BaseException as exc:  # re-raised on the calling thread  # noqa: BLE001
                box["error"] = exc
            finally:
                done.set()

        self._invoke_on_main(wrapper)
        if not done.wait(DB_CALL_TIMEOUT_S):
            raise AstrometryError(
                "timed out waiting for the project db thread — is the main "
                "loop running?"
            )
        if "error" in box:
            raise box["error"]
        return box["result"]


__all__ = [
    "DB_CALL_TIMEOUT_S",
    "DriftController",
    "DriftListener",
    "DriftSnapshot",
    "InvokeOnMain",
    "SolveController",
    "SolveListener",
    "SolveSnapshot",
]
