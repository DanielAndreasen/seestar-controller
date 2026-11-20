"""Photometry run + results — the PURE controller layer of photometry ticket 04
(:mod:`seestar_controller.photometry.run`).

This is a PURE module: **no ``gi`` import** (the sandbox has no GTK, see
docs/wiki/assumptions.md; a subprocess test enforces the boundary) — stdlib +
:mod:`seestar_controller.store` + the sibling pure modules :mod:`.picking` /
:mod:`.measure` only. No :mod:`seestar_controller.link`, no :mod:`ui`, no
:mod:`enhance` imports. The widget layer (:mod:`.results_view`) imports ``gi``
at module top and is never imported by this module or eagerly by the package
``__init__``.

What lives here:

- :class:`RunError` — the run-layer error, a :class:`~.picking.PhotometryError`
  (hence a :class:`ValueError`): no project, an unknown session, an unrunnable
  pick ensemble (no target pick), a run already in progress, a store failure,
  or an unexpected failure while running (wrapped and chained);
- :class:`RunProgress` — one frozen progress tick (``done`` / ``total`` /
  ``frame_id``);
- :class:`RunReport` — the frozen result of :meth:`RunController.run_session`:
  the stored :class:`~seestar_controller.store.measurements.Measurement`\\ s,
  the :class:`~.measure.SkippedFrame`\\ s, the ``session_id``, and the
  per-frame :class:`~.measure.FrameMeasurement` results (``frame_results`` —
  carrying the check star's differential mag + error and the
  target-minus-check curve, check-star-plot ticket 01);
- :class:`RunSnapshot` — the frozen controller snapshot handed to the
  listeners: ``running``, the last ``session_id`` / ``progress`` / ``report`` /
  ``error``, and the project's **lightcurve** points (the results accessor,
  see :meth:`RunController.lightcurve_points`);
- :class:`RunController` — the app-level shared controller (the app owns one,
  bound to the shared :class:`~.picking.PickController`):

  - :meth:`~RunController.run_session` — run aperture photometry over ALL
    **sub-frames** of one session (``store.frames`` ``kind="sub"`` filtered by
    ``session_id`` — stacks/enhanced are NEVER measured), appending each result
    via :func:`seestar_controller.store.measurements.add_measurement` (jd, mag,
    mag_err from the :class:`~.measure.FrameMeasurement`), collecting skipped
    frames, reporting progress via the ``on_progress(done, total, frame)``
    callback (called after each frame, so a full run yields the sequence
    ``1..N``), and returning the frozen :class:`RunReport`. It is a plain
    synchronous, GTK-free method — **safe to call from a worker thread** (the
    widget layer does exactly that, so the GLib main loop never blocks);
  - the picks come from the shared picker controller; when the picker session
    is empty, the stored picks are loaded from the project
    (:func:`seestar_controller.store.stars.load_star_picks`);
  - :meth:`~RunController.lightcurve_points` /
    :meth:`~RunController.set_excluded` — the results accessor: the project's
    lightcurve points and outlier flagging (via
    :func:`seestar_controller.store.measurements.lightcurve` /
    :func:`~seestar_controller.store.measurements.set_excluded`), also exposed
    on every snapshot;
  - :meth:`~RunController.sessions` — the project's sessions (the Run button's
    chooser);
  - listeners (:meth:`~RunController.add_listener` /
    :meth:`~RunController.remove_listener`) get a frozen :class:`RunSnapshot`
    on every change (run start, every progress tick, run end, exclusion
    change), marshalled through ``invoke_on_main`` (the app binds
    ``GLib.idle_add``; the default calls directly, so tests are synchronous).

Conventions (documented choices):

- **Threading / sqlite thread-affinity.** ``run_session`` itself never touches
  GTK, so it can run on any thread. The project db, however, is
  thread-affine (``sqlite3`` default ``check_same_thread=True`` — verified),
  so every STORE call (``list_sessions`` / ``list_frames`` / ``load_star_picks``
  / ``add_measurement`` / ``lightcurve`` / ``set_excluded``) goes through
  :meth:`RunController._call_store`, which runs it directly when already on the
  db-owning thread and otherwise marshals it through ``invoke_on_main`` and
  waits for the result (the same pattern as observing's ``SessionCapture``:
  heavy work on the worker, store writes on the db-owning thread). The
  db-owning thread is captured when the project is set
  (:meth:`~RunController.set_project` / the constructor / adopting the picker
  controller's project — all main-thread flows in the app). The FITS reading
  and the photutils measuring stay on the calling (worker) thread — they need
  no db.
- **Picks resolution.** The shared picker controller's picks win (measured
  with the picker session's aperture); when the picker session is empty, the
  project's stored picks are loaded (store records carry their own per-pick
  aperture). Either way the ensemble is validated up front (exactly one
  target + at least one comp — :func:`~.measure._normalize_picks`) and a bad
  ensemble raises :class:`RunError` BEFORE any frame is measured.
- **Per-frame failures never kill the run**: a frame that cannot be read, a
  pick outside the image, or a non-positive flux is recorded as a
  :class:`~.measure.SkippedFrame` (the same skip semantics as
  :func:`~.measure.measure_project`).
- **One run at a time**: calling :meth:`~RunController.run_session` while a
  run is in progress raises :class:`RunError`.
- **Unknown session**: a ``session_id`` not in
  :func:`seestar_controller.store.sessions.list_sessions` raises
  :class:`RunError` naming the known sessions. A session with zero sub-frames
  is a valid (empty) run.
- **Progress**: ``on_progress(done, total, frame)`` is called after each frame
  with ``done`` = frames processed so far (1..N), ``total`` = the session's
  sub-frame count, and the current :class:`~seestar_controller.store.frames.Frame`.
  The same tick is also emitted to the listeners as
  :attr:`RunSnapshot.progress`.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from seestar_controller.store.errors import (
    MeasurementError as StoreMeasurementError,
)
from seestar_controller.store.errors import ProjectError, StarPickError
from seestar_controller.store.frames import KIND_SUB, Frame, list_frames
from seestar_controller.store.measurements import (
    Measurement,
    add_measurement,
    lightcurve,
)
from seestar_controller.store.measurements import (
    set_excluded as store_set_excluded,
)
from seestar_controller.store.sessions import Session, list_sessions
from seestar_controller.store.stars import load_star_picks

from .measure import (
    FrameMeasurement,
    MeasurementError,
    SkippedFrame,
    _normalize_picks,
    _read_frame_fits,
    measure_frame_data,
)
from .picking import ApertureParams, PhotometryError, PickController, PickSession

logger = logging.getLogger(__name__)


class RunError(PhotometryError):
    """Any photometry-run failure — a :class:`~.picking.PhotometryError` (hence
    a :class:`ValueError`) with a clear message: no project set, an unknown or
    bad session id, an unrunnable pick ensemble (e.g. no target pick), a run
    already in progress, a store failure while listing/reading/storing, or an
    unexpected error while running (wrapped and chained)."""


#: How long :meth:`RunController._call_store` waits for a marshalled store call
#: on the db-owning thread before giving up (the main loop must be running).
DB_CALL_TIMEOUT_S = 60.0


@dataclass(frozen=True)
class RunProgress:
    """One frozen progress tick of a run.

    - ``done`` — sub-frames processed so far (1..``total`` after each frame);
    - ``total`` — the session's sub-frame count;
    - ``frame_id`` — the frame just processed.
    """

    done: int
    total: int
    frame_id: int


@dataclass(frozen=True)
class RunReport:
    """The frozen result of :meth:`RunController.run_session`.

    - ``measurements`` — the stored
      :class:`~seestar_controller.store.measurements.Measurement`\\ s (one per
      successfully measured sub-frame, in frame order);
    - ``skipped`` — the :class:`~.measure.SkippedFrame`\\ s (in frame order);
    - ``session_id`` — the session that was measured;
    - ``frame_results`` — the full per-frame
      :class:`~.measure.FrameMeasurement`\\ s of the measured frames (in
      frame order, aligned with ``measurements``) — they carry the check
      star's differential mag + error (``check_mag`` / ``check_mag_err``) and
      the target-minus-check curve (``target_minus_check`` /
      ``target_minus_check_err``) when a check star was picked
      (check-star-plot ticket 01).
    """

    measurements: tuple[Measurement, ...]
    skipped: tuple[SkippedFrame, ...]
    session_id: str
    frame_results: tuple[FrameMeasurement, ...] = ()

    @property
    def frames(self) -> int:
        """The total number of sub-frames processed (measured + skipped)."""
        return len(self.measurements) + len(self.skipped)

    @property
    def check_points(self) -> tuple[tuple[float, float, float], ...]:
        """The check-star curve of the run: ``(jd, check_mag, check_mag_err)``
        per measured frame that has a check star (empty when no check star was
        picked)."""
        return tuple(
            (fm.jd, fm.check_mag, fm.check_mag_err)
            for fm in self.frame_results
            if fm.check_mag is not None and fm.check_mag_err is not None
        )

    @property
    def target_minus_check_points(self) -> tuple[tuple[float, float, float], ...]:
        """The target-minus-check curve of the run: ``(jd, target_minus_check,
        target_minus_check_err)`` per measured frame that has a check star
        (empty when no check star was picked)."""
        return tuple(
            (fm.jd, fm.target_minus_check, fm.target_minus_check_err)
            for fm in self.frame_results
            if fm.target_minus_check is not None and fm.target_minus_check_err is not None
        )


#: The progress callback of :meth:`RunController.run_session`:
#: ``on_progress(done, total, frame)`` — called after each frame (on the
#: calling thread), so a full run yields the ``done`` sequence ``1..N``.
ProgressCallback = Callable[[int, int, Frame], None]


@dataclass(frozen=True)
class RunSnapshot:
    """The frozen snapshot of a :class:`RunController`, handed to the listeners
    on every change.

    - ``running`` — True while a run is in progress;
    - ``session_id`` — the session of the last run (None before the first);
    - ``progress`` — the last :class:`RunProgress` tick while running, else
      None;
    - ``report`` — the last :class:`RunReport` (None before the first
      successful run);
    - ``error`` — the last run error message, else None;
    - ``lightcurve`` — the project's lightcurve points (ALL measurements
      including excluded ones, ordered by jd) — the results accessor.
    """

    running: bool
    session_id: str | None
    progress: RunProgress | None
    report: RunReport | None
    error: str | None
    lightcurve: tuple[Measurement, ...]


#: A run listener: called with a frozen :class:`RunSnapshot` on any change
#: (always marshalled via ``invoke_on_main``).
RunListener = Callable[[RunSnapshot], None]

#: Marshals a zero-argument callback onto the "main" thread. The widget layer
#: binds this to ``GLib.idle_add``; the default calls it directly.
InvokeOnMain = Callable[[Callable[[], None]], Any]


def _direct_invoke(callback: Callable[[], None]) -> None:
    """The default ``invoke_on_main``: run the callback directly (tests)."""
    callback()


class RunController:
    """The app-level photometry run + results controller.

    - ``project`` — the store project to measure into (None = follow the
      picker controller's project, see :meth:`set_project`);
    - ``picker_controller`` — the shared
      :class:`~.picking.PickController`; its picks are used for the run (the
      stored picks are the fallback when its session is empty), and its
      project is adopted when no explicit project is set;
    - ``invoke_on_main`` — the marshalling callable for listener notifications
      AND for store calls made from a non-db thread (the app binds
      ``GLib.idle_add``; the default calls directly).

    Operations: :meth:`run_session` (the run), :meth:`sessions`,
    :meth:`lightcurve_points`, :meth:`set_excluded` (outlier flagging),
    :meth:`set_project`, listeners with frozen :class:`RunSnapshot`\\ s.
    """

    def __init__(
        self,
        project: Any | None = None,
        picker_controller: PickController | None = None,
        *,
        invoke_on_main: InvokeOnMain | None = None,
    ) -> None:
        self._project: Any | None = None
        self._picker_controller: PickController | None = None
        self._invoke_on_main: InvokeOnMain = invoke_on_main or _direct_invoke
        self._lock = threading.Lock()
        self._listeners: list[RunListener] = []
        self._running = False
        self._session_id: str | None = None
        self._progress: RunProgress | None = None
        self._report: RunReport | None = None
        self._error: str | None = None
        self._lightcurve: tuple[Measurement, ...] | None = None
        self._db_thread_id: int | None = None
        if picker_controller is not None:
            self.attach_picker(picker_controller)
        if project is not None:
            self.set_project(project)

    # -- reading ---------------------------------------------------------------

    @property
    def project(self) -> Any | None:
        """The current project (explicit, else the picker controller's)."""
        if self._project is not None:
            return self._project
        if self._picker_controller is not None:
            return self._picker_controller.project
        return None

    @property
    def picker_controller(self) -> PickController | None:
        """The pick controller this run controller follows (if any)."""
        return self._picker_controller

    @property
    def invoke_on_main(self) -> InvokeOnMain:
        """The marshalling callable (the app binds ``GLib.idle_add``)."""
        return self._invoke_on_main

    @property
    def running(self) -> bool:
        """True while a run is in progress."""
        with self._lock:
            return self._running

    @property
    def session_id(self) -> str | None:
        """The session of the last run (None before the first)."""
        with self._lock:
            return self._session_id

    @property
    def progress(self) -> RunProgress | None:
        """The last progress tick while running, else None."""
        with self._lock:
            return self._progress

    @property
    def report(self) -> RunReport | None:
        """The last :class:`RunReport` (None before the first run)."""
        with self._lock:
            return self._report

    @property
    def error(self) -> str | None:
        """The last run error message, else None."""
        with self._lock:
            return self._error

    @property
    def snapshot(self) -> RunSnapshot:
        """The frozen :class:`RunSnapshot` of the current state.

        The lightcurve is read through :meth:`_call_store` (never while
        holding the lock — a marshalled store call waits for the db-owning
        thread, and the lock must stay free so that thread can compute its
        own snapshots)."""
        with self._lock:
            lc = self._lightcurve
            running, session_id = self._running, self._session_id
            progress, report, error = self._progress, self._report, self._error
        if lc is None:
            project = self.project
            if project is not None:
                try:
                    lc = tuple(
                        self._call_store(lambda: lightcurve(project, include_excluded=True))
                    )
                except ProjectError:
                    lc = ()
            else:
                lc = ()
            with self._lock:
                self._lightcurve = lc
        return RunSnapshot(
            running=running,
            session_id=session_id,
            progress=progress,
            report=report,
            error=error,
            lightcurve=lc,
        )

    # -- listeners ---------------------------------------------------------------

    @property
    def listeners(self) -> tuple[RunListener, ...]:
        """The registered observer callbacks, in registration order."""
        return tuple(self._listeners)

    def add_listener(self, listener: RunListener) -> RunListener:
        """Register an observer callback (idempotent); returns it, so it can
        be used as a decorator. The listener receives a frozen
        :class:`RunSnapshot` on any change (marshalled via ``invoke_on_main``)."""
        if listener not in self._listeners:
            self._listeners.append(listener)
        return listener

    def remove_listener(self, listener: RunListener) -> None:
        """Unregister an observer callback (no-op when not registered)."""
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _notify(self, snapshot: RunSnapshot) -> None:
        """Hand a frozen snapshot to every listener; a failing listener is
        logged and isolated (never breaks the others or the caller)."""
        for listener in list(self._listeners):
            try:
                listener(snapshot)
            except Exception:
                logger.exception("photometry run listener failed")

    def _emit(self) -> None:
        """Marshal a listener notification through ``invoke_on_main``."""
        snapshot = self.snapshot  # no lock held while the store call runs
        self._invoke_on_main(lambda: self._notify(snapshot))

    # -- project / picker ----------------------------------------------------------

    def set_project(self, project: Any | None) -> None:
        """Set/replace the explicit project (None clears it — the picker
        controller's project is then followed again). The db-owning thread is
        captured here (sqlite is thread-affine). Notifies."""
        self._project = project
        if project is not None:
            self._db_thread_id = threading.get_ident()
        with self._lock:
            self._lightcurve = None
        self._emit()

    def attach_picker(self, picker_controller: PickController) -> None:
        """Follow ``picker_controller``: its picks are used for runs, and its
        project is adopted (with the db-owning thread captured) when no
        explicit project is set. Re-attaching detaches the previous picker."""
        if self._picker_controller is not None:
            self._picker_controller.remove_listener(self._on_pick_session)
        self._picker_controller = picker_controller
        picker_controller.add_listener(self._on_pick_session)
        project = picker_controller.project
        if project is not None and self._project is None:
            self._project = project
            self._db_thread_id = threading.get_ident()
            with self._lock:
                self._lightcurve = None
            self._emit()

    def detach_picker(self) -> None:
        """Stop following the pick controller."""
        if self._picker_controller is not None:
            self._picker_controller.remove_listener(self._on_pick_session)
            self._picker_controller = None

    def _on_pick_session(self, _session: PickSession) -> None:
        """Pick-controller listener: adopt the picker's project (first time)
        so runs measure into it."""
        if self._project is not None or self._picker_controller is None:
            return
        project = self._picker_controller.project
        if project is not None:
            self._project = project
            self._db_thread_id = threading.get_ident()
            with self._lock:
                self._lightcurve = None
            self._emit()

    def _require_project(self) -> Any:
        """The current project; raises :class:`RunError` when none is set."""
        project = self.project
        if project is None:
            raise RunError(
                "no project — set one with set_project() first "
                "(the Projects view is a later ticket)"
            )
        return project

    # -- store marshalling (sqlite is thread-affine) --------------------------------

    def _call_store(self, fn: Callable[[], Any]) -> Any:
        """Run the store callable ``fn`` and return its result.

        When already on the db-owning thread (or when no db thread was
        captured), ``fn`` runs directly. Otherwise it is marshalled through
        ``invoke_on_main`` (the app: ``GLib.idle_add``) and the calling thread
        waits for the result — so :meth:`run_session` is safe to call from a
        worker thread while every sqlite access stays on the db-owning thread
        (the same pattern as observing's ``SessionCapture``).
        """
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
            raise RunError(
                "timed out waiting for the project db thread — is the main "
                "loop running?"
            )
        if "error" in box:
            raise box["error"]
        return box["result"]

    # -- results accessor --------------------------------------------------------------

    def sessions(self) -> list[Session]:
        """The project's sessions (ordered by ``started_at``) — the Run
        button's chooser. Raises :class:`RunError` when no project is set."""
        project = self._require_project()
        try:
            return self._call_store(lambda: list_sessions(project))
        except ProjectError as exc:
            raise RunError(
                f"cannot list the sessions of project {project.slug!r}: {exc}"
            ) from exc

    def lightcurve_points(self, include_excluded: bool = True) -> tuple[Measurement, ...]:
        """The project's lightcurve points (all sessions, ordered by jd) as a
        tuple of :class:`~seestar_controller.store.measurements.Measurement`.

        - ``include_excluded`` — True (default) includes outlier-flagged
          points (the results view shows them so they can be un-flagged);
          False filters them out (the store's default view).

        Raises :class:`RunError` when no project is set or the store read
        fails.
        """
        project = self._require_project()
        try:
            return tuple(
                self._call_store(lambda: lightcurve(project, include_excluded=include_excluded))
            )
        except ProjectError as exc:
            raise RunError(
                f"cannot read the lightcurve of project {project.slug!r}: {exc}"
            ) from exc

    def set_excluded(self, measurement_id: int, excluded: bool = True) -> Measurement:
        """Flag (``excluded=True``) or unflag an outlier lightcurve point via
        :func:`seestar_controller.store.measurements.set_excluded`; returns the
        updated :class:`Measurement`, refreshes the snapshot's lightcurve and
        notifies the listeners.

        Raises :class:`RunError` when no project is set, ``excluded`` is not a
        bool, the measurement id is unknown, or the store write fails.
        """
        project = self._require_project()
        if not isinstance(excluded, bool):
            raise RunError(f"excluded must be a bool, got {excluded!r}")
        if isinstance(measurement_id, bool) or not isinstance(measurement_id, int):
            raise RunError(f"measurement_id must be an int, got {measurement_id!r}")
        try:
            updated = self._call_store(
                lambda: store_set_excluded(project, measurement_id, excluded)
            )
        except StoreMeasurementError as exc:
            raise RunError(
                f"cannot flag measurement {measurement_id!r} of project "
                f"{project.slug!r} as excluded={excluded}: {exc}"
            ) from exc
        except ProjectError as exc:
            raise RunError(
                f"cannot flag measurement {measurement_id!r} of project "
                f"{project.slug!r}: {exc}"
            ) from exc
        lc = tuple(self._call_store(lambda: lightcurve(project, include_excluded=True)))
        with self._lock:
            self._lightcurve = lc
        self._emit()
        return updated

    # -- the run ------------------------------------------------------------------------

    def _resolve_picks(self, project: Any) -> tuple[list[Any], ApertureParams | None]:
        """The picks for a run: the picker controller's picks (with its session
        aperture) when its session is non-empty, else the project's stored
        picks (per-pick apertures). The ensemble is validated up front;
        raises :class:`RunError` when unrunnable."""
        picker = self._picker_controller
        if picker is not None:
            picks = list(picker.picks)
            if picks:
                aperture = picker.aperture
                try:
                    _normalize_picks(picks, aperture)
                except MeasurementError as exc:
                    raise RunError(
                        f"the current pick session cannot be measured: {exc}"
                    ) from exc
                return picks, aperture
        try:
            stored = self._call_store(lambda: load_star_picks(project))
        except (StarPickError, ProjectError) as exc:
            raise RunError(
                f"cannot load the stored star picks of project {project.slug!r}: {exc}"
            ) from exc
        if not stored:
            raise RunError(
                "no target pick — pick a target star and at least one comparison "
                "star first (or save picks into the project)"
            )
        try:
            _normalize_picks(stored, None)
        except MeasurementError as exc:
            raise RunError(
                f"the stored star picks of project {project.slug!r} cannot be "
                f"measured: {exc}"
            ) from exc
        return list(stored), None

    def _measure_one(
        self, project: Any, frame: Frame, picks: list[Any], aperture: ApertureParams | None
    ) -> tuple[Measurement, FrameMeasurement]:
        """Measure one stored frame (FITS read + photometry on the calling
        thread; the store insert on the db-owning thread) and return
        ``(measurement, frame_measurement)`` — the stored
        :class:`Measurement` and the full :class:`~.measure.FrameMeasurement`
        (carrying the check-star curves). Raises :class:`MeasurementError`
        (a :class:`~.picking.PhotometryError`) on any per-frame failure — the
        caller records it as skipped."""
        data, header = _read_frame_fits(project, frame)
        fm = measure_frame_data(data, header, picks, aperture=aperture, jd=frame.jd)
        try:
            measurement = self._call_store(
                lambda: add_measurement(project, frame.id, fm.mag, jd=fm.jd, mag_err=fm.mag_err)
            )
        except StoreMeasurementError as exc:
            raise MeasurementError(
                f"could not store the measurement for frame {frame.id} of "
                f"project {project.slug!r}: {exc}"
            ) from exc
        return measurement, fm

    def run_session(
        self,
        session_id: str,
        on_progress: ProgressCallback | None = None,
    ) -> RunReport:
        """Run aperture photometry over ALL sub-frames of ``session_id`` and
        return the frozen :class:`RunReport`.

        - ``session_id`` — must be an existing session of the project (else
          :class:`RunError` naming the known sessions);
        - ``on_progress`` — optional ``on_progress(done, total, frame)``
          callback, called after each frame on the CALLING thread (a full run
          yields the ``done`` sequence ``1..N``); the same tick is emitted to
          the listeners as :attr:`RunSnapshot.progress`;
        - every sub-frame (``kind="sub"`` only — stacks/enhanced are never
          measured) is measured with the resolved picks and its result appended
          via :func:`seestar_controller.store.measurements.add_measurement`
          (jd, mag, mag_err); a per-frame failure is recorded as a
          :class:`~.measure.SkippedFrame`, never a crash;
        - the picks come from the shared picker controller, falling back to the
          project's stored picks (see :meth:`_resolve_picks`);
        - one run at a time: a concurrent call raises :class:`RunError`.

        The method is synchronous and GTK-free — safe to call from a worker
        thread (store calls are marshalled to the db-owning thread, see
        :meth:`_call_store`). Listener notifications go through
        ``invoke_on_main``. Raises :class:`RunError` (a
        :class:`~.picking.PhotometryError`) on any run-level failure; the error
        is also recorded in :attr:`RunSnapshot.error`.
        """
        if isinstance(session_id, bool) or not isinstance(session_id, str) or not session_id:
            raise RunError(f"session_id must be a non-empty string, got {session_id!r}")
        if on_progress is not None and not callable(on_progress):
            raise RunError(f"on_progress must be callable or None, got {on_progress!r}")
        project = self._require_project()
        with self._lock:
            if self._running:
                raise RunError(
                    "a photometry run is already in progress — wait for it to finish"
                )
            self._running = True
            self._session_id = session_id
            self._progress = None
            self._report = None
            self._error = None
        self._emit()
        try:
            known = {s.id for s in self._call_store(lambda: list_sessions(project))}
            if session_id not in known:
                raise RunError(
                    f"project {project.slug!r} has no session {session_id!r} "
                    f"(known sessions: {', '.join(sorted(known)) or 'none'})"
                )
            picks, aperture = self._resolve_picks(project)
            frames = self._call_store(
                lambda: list_frames(project, session_id=session_id, kind=KIND_SUB)
            )
            total = len(frames)
            measurements: list[Measurement] = []
            frame_results: list[FrameMeasurement] = []
            skipped: list[SkippedFrame] = []
            for index, frame in enumerate(frames):
                try:
                    measurement, fm = self._measure_one(project, frame, picks, aperture)
                except MeasurementError as exc:
                    logger.info(
                        "skipping sub-frame %s of session %s: %s", frame.id, session_id, exc
                    )
                    skipped.append(SkippedFrame(frame_id=frame.id, reason=str(exc)))
                else:
                    measurements.append(measurement)
                    frame_results.append(fm)
                done = index + 1
                progress = RunProgress(done=done, total=total, frame_id=frame.id)
                with self._lock:
                    self._progress = progress
                if on_progress is not None:
                    on_progress(done, total, frame)
                self._emit()
            report = RunReport(
                measurements=tuple(measurements),
                skipped=tuple(skipped),
                session_id=session_id,
                frame_results=tuple(frame_results),
            )
            # refresh the snapshot's lightcurve (store call OUTSIDE the lock)
            lc = tuple(
                self._call_store(lambda: lightcurve(project, include_excluded=True))
            )
            with self._lock:
                self._report = report
                self._lightcurve = lc
            return report
        except PhotometryError as exc:
            with self._lock:
                self._error = str(exc)
            raise
        except Exception as exc:
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"
            raise RunError(
                f"the photometry run over session {session_id!r} of project "
                f"{project.slug!r} failed: {type(exc).__name__}: {exc}"
            ) from exc
        finally:
            with self._lock:
                self._running = False
                self._progress = None
            self._emit()
