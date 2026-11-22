"""The variable-scan controller — the PURE layer of variable-scan ticket 02
(:mod:`seestar_controller.variable_scan.controller`).

Pure Python: **no ``gi`` import** (a subprocess test enforces the boundary) —
stdlib + PIL (via :mod:`.render`) + :mod:`seestar_controller.store` + the
sibling pure modules only. The widget layer (:mod:`.view`) imports ``gi`` at
module top and observes this controller.

What lives here:

- :class:`ScanSnapshot` — the frozen snapshot the widget renders;
- :class:`ScanController` — the app-level shared controller (the same role
  as the moving-objects
  :class:`~seestar_controller.moving_objects.overlay.MoverController`): the
  current project, the latest scan run's ranked records, the live threshold
  filter, the selected candidate with its lightcurve points and its blink
  (cutout images + animated GIF), and frozen-snapshot listener notifications
  marshalled through ``invoke_on_main`` (the app binds ``GLib.idle_add``);
- the lightcurve-points sidecar: :func:`save_scan_points` /
  :func:`load_scan_points` — the per-candidate lightcurve points of the
  latest run as a JSON file next to ``project.json`` (FILE-BASED, no store
  schema bump: the db ``scan_results`` rows of ticket 01 carry only the
  aggregates, the points live in the sidecar keyed by rank).

Threading / sqlite thread-affinity: like the mover controller, every STORE
call goes through :meth:`ScanController._call_store` (direct on the
db-owning thread, otherwise marshalled through ``invoke_on_main`` with a
wait), so the widget can run :meth:`ScanController.run_scan` on a worker
thread.

Error policy: argument validation raises immediately
(:class:`~.errors.ScanError`); runtime failures (store, unreadable frames)
are recorded in the snapshot's ``error`` and the listeners are notified —
never raised — because the widget runs the operations on a worker thread and
renders ``snapshot.error`` as the status.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from seestar_controller.store.frames import KIND_SUB, Frame, list_frames
from seestar_controller.store.project import Project

from .errors import ScanError
from .export import export_candidates_csv
from .persist import ScanResultRecord, latest_scan_results
from .render import (
    DEFAULT_BLINK_DURATION_MS,
    render_blink_frames,
    render_blink_gif,
)
from .scan import DEFAULT_MIN_FRAMES, LightcurvePoint, ScanReport, scan_project
from .settings import (
    _check_project,
    load_scan_threshold,
    set_scan_threshold,
    validate_threshold,
)

logger = logging.getLogger(__name__)

#: File name of the lightcurve-points sidecar inside a project directory.
SCAN_POINTS_FILE = "scan_points.json"

#: Schema version of the points sidecar.
SCAN_POINTS_SCHEMA_VERSION = 1

#: Timeout for a store call marshalled to the db-owning thread, in seconds.
DB_CALL_TIMEOUT_S = 30.0

#: A listener: called with a frozen :class:`ScanSnapshot` on any change.
ScanListener = Callable[["ScanSnapshot"], None]

#: The marshalling callable for listener notifications (the app binds
#: ``GLib.idle_add``; the default calls directly).
InvokeOnMain = Callable[[Callable[[], None]], Any]


def _direct_invoke(callback: Callable[[], None]) -> None:
    """The default ``invoke_on_main``: run the callback directly (tests)."""
    callback()


# --- the lightcurve-points sidecar -----------------------------------------------------------


def points_path(project: Any) -> Path:
    """The path of the points sidecar: ``<project>/scan_points.json``."""
    project = _check_project(project)
    return project.path / SCAN_POINTS_FILE


def save_scan_points(project: Any, report: ScanReport) -> Path:
    """Persist the per-star lightcurve points of ``report`` (a
    :class:`~.scan.ScanReport`) to the sidecar ``<project>/scan_points.json``
    (atomic write: temp file + fsync + :func:`os.replace`), keyed by the
    1-based ``rank`` of each result — the db rows of ticket 01 carry only the
    aggregates, the points live here. Returns the written path.

    Raises :class:`TypeError` for a non-Project handle or a non-ScanReport,
    :class:`~.errors.ScanError` for a write failure.
    """
    project = _check_project(project)
    if not isinstance(report, ScanReport):
        raise TypeError(
            f"report must be a variable_scan ScanReport, got {type(report).__name__}"
        )
    payload = {
        "schema_version": SCAN_POINTS_SCHEMA_VERSION,
        "scanned_at": report.scanned_at,
        "points_by_rank": {
            str(result.rank): [
                [point.jd, point.mag, point.mag_err] for point in result.points
            ]
            for result in report.results
        },
    }
    destination = points_path(project)
    fd, tmp_name = tempfile.mkstemp(
        prefix=destination.name + ".", suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=1, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, destination)
    except BaseException:  # noqa: BLE001  (temp removed on ANY failure)
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise ScanError(
            f"could not write the scan points sidecar of project {project.slug!r}"
        ) from None
    return destination


def load_scan_points(project: Any) -> dict[str, Any] | None:
    """Load the points sidecar of ``project``: the parsed JSON dict, or None
    when the file is missing or corrupt (a corrupt sidecar is ignored —
    never a crash; the candidates then simply have no points)."""
    project = _check_project(project)
    path = points_path(project)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        logger.info("ignoring corrupt scan points sidecar %s", path)
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _points_for_rank(payload: dict[str, Any] | None, rank: int) -> tuple[LightcurvePoint, ...]:
    """The :class:`~.scan.LightcurvePoint`\\ s of rank ``rank`` from a loaded
    sidecar payload (empty when absent/corrupt)."""
    if payload is None:
        return ()
    raw = payload.get("points_by_rank")
    if not isinstance(raw, dict):
        return ()
    rows = raw.get(str(rank))
    if not isinstance(rows, list):
        return ()
    points: list[LightcurvePoint] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) != 3:
            continue
        try:
            points.append(
                LightcurvePoint(jd=float(row[0]), mag=float(row[1]), mag_err=float(row[2]))
            )
        except (TypeError, ValueError):
            continue
    return tuple(points)


# --- snapshot + controller -------------------------------------------------------------------


@dataclass(frozen=True)
class ScanSnapshot:
    """An immutable snapshot of everything the candidate view renders, handed
    to the listeners on any change.

    - ``project_name`` — the current project's display name (None when no
      project is set);
    - ``records`` — the latest scan run's persisted rows (rank order);
    - ``threshold`` — the current (persisted) variability threshold;
    - ``candidates`` — the records with ``variability >= threshold`` (the
      displayed ranked candidate list, rank order);
    - ``selected_index`` — the index of the selected candidate in
      ``candidates`` (None when none);
    - ``selected`` — the selected :class:`~.persist.ScanResultRecord`, else
      None;
    - ``points`` — the selected candidate's lightcurve points (from the
      sidecar; empty when none);
    - ``frames`` — ALL the project's sub-frames (the blink frames, ordered
      by jd);
    - ``frame_index`` — the index of the displayed blink frame (None when
      there are no frames);
    - ``blink_images`` — the selected candidate's cutout per blink frame
      (aligned with ``frames``; empty until a candidate is selected);
    - ``blink_gif`` — the animated GIF (bytes) of the blink, else None;
    - ``status`` — a short human-readable status, else None;
    - ``error`` — the last error message, else None.
    """

    project_name: str | None
    records: tuple[ScanResultRecord, ...]
    threshold: float
    candidates: tuple[ScanResultRecord, ...]
    selected_index: int | None
    selected: ScanResultRecord | None
    points: tuple[LightcurvePoint, ...]
    frames: tuple[Frame, ...]
    frame_index: int | None
    blink_images: tuple[Image.Image, ...]
    blink_gif: bytes | None
    status: str | None
    error: str | None


class ScanController:
    """The app-level variable-scan controller: the current project, the
    latest scan run's ranked records, the live threshold filter, the selected
    candidate (lightcurve points + blink cutouts/GIF), and observer
    notification.

    - ``project`` — the store project (None = follow the attached viewer
      controller's project, see :meth:`attach_viewer`);
    - ``viewer_controller`` — an optional duck-typed viewer controller (the
      shared viewer controller — NOT imported here): its project is adopted
      whenever no explicit project is set, so the candidate view follows the
      currently open project;
    - ``invoke_on_main`` — the marshalling callable for listener
      notifications AND for store calls made from a non-db thread (the app
      binds ``GLib.idle_add``; the default calls directly).

    Operations: :attr:`snapshot` (never raises), :meth:`set_project`,
    :meth:`attach_viewer` / :meth:`detach_viewer`, :meth:`refresh` (reload the
    persisted latest run + points sidecar + subs), :meth:`run_scan` (scan ->
    persist db rows + points sidecar -> refresh), :meth:`select_candidate`,
    :meth:`set_threshold` (validate + persist + re-filter),
    :meth:`select_frame` / :meth:`next_frame` / :meth:`prev_frame` (the
    blink), :meth:`export_csv`, and listeners with frozen snapshots.
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
        self._listeners: list[ScanListener] = []
        self._db_thread_id: int | None = None
        self._records: tuple[ScanResultRecord, ...] = ()
        self._points_payload: dict[str, Any] | None = None
        self._threshold: float = 0.0
        self._selected_index: int | None = None
        self._points: tuple[LightcurvePoint, ...] = ()
        self._frames: tuple[Frame, ...] = ()
        self._frame_index: int | None = None
        self._blink_images: tuple[Image.Image, ...] = ()
        self._blink_gif: bytes | None = None
        self._status: str | None = None
        self._error: str | None = None
        if viewer_controller is not None:
            self.attach_viewer(viewer_controller)
        if project is not None:
            self.set_project(project)

    # -- project + viewer -------------------------------------------------------------

    @property
    def project(self) -> Project | None:
        """The current store project (None when not set yet)."""
        return self._project

    @property
    def viewer_controller(self) -> Any | None:
        """The viewer controller this controller follows (if any)."""
        return self._viewer_controller

    def attach_viewer(self, viewer_controller: Any) -> None:
        """Follow ``viewer_controller`` (duck-typed — the shared viewer
        controller): register as its listener and adopt its project whenever
        no explicit project is set, so the candidate view follows the
        currently open project. Re-attaching detaches the previous viewer
        first."""
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

    def _on_viewer_snapshot(self, _snapshot: Any) -> None:
        """Viewer listener: adopt the viewer's project (when no explicit
        project is set) so the view follows the open project — emits on every
        viewer change, so closing the project clears the view."""
        if self._project is not None or self._viewer_controller is None:
            return
        self.set_project(getattr(self._viewer_controller, "project", None))

    @property
    def invoke_on_main(self) -> InvokeOnMain:
        """The marshalling callable (the app binds ``GLib.idle_add``)."""
        return self._invoke_on_main

    def set_project(self, project: Project | None) -> None:
        """Set/replace the current project (None clears it): the threshold is
        re-loaded from the project settings and the persisted latest run is
        refreshed. Notifies."""
        self._project = project
        self._db_thread_id = threading.get_ident() if project is not None else None
        with self._lock:
            self._records = ()
            self._points_payload = None
            self._threshold = 0.0
            self._selected_index = None
            self._points = ()
            self._frames = ()
            self._frame_index = None
            self._blink_images = ()
            self._blink_gif = None
            self._status = None
            self._error = None
        if project is not None:
            try:
                with self._lock:
                    self._threshold = self._call_store(lambda: load_scan_threshold(project))
                self.refresh()
                return  # refresh already notified
            except Exception as exc:  # surfaced as snapshot.error, never raised  # noqa: BLE001
                logger.info("scan set_project failed: %s", exc)
                with self._lock:
                    self._error = f"{type(exc).__name__}: {exc}"
        self._emit(self.snapshot)

    # -- refresh + scan -----------------------------------------------------------------

    @property
    def threshold(self) -> float:
        """The current variability threshold (0.0 when no project is set)."""
        return self._threshold

    def refresh(self) -> ScanSnapshot:
        """(Re-)load the persisted latest scan run (db rows + points sidecar)
        and the project's sub-frames via the store; re-apply the threshold
        filter and the selection; notify; return the snapshot. Runtime
        failures are recorded in ``snapshot.error``, never raised."""
        project = self._require_project()
        try:
            records = tuple(self._call_store(lambda: latest_scan_results(project)))
            payload = self._call_store(lambda: load_scan_points(project))
            frames = tuple(self._call_store(lambda: list_frames(project, kind=KIND_SUB)))
            with self._lock:
                self._records = records
                self._points_payload = payload
                self._frames = frames
                self._error = None
                self._apply_filter_locked()
                self._status = self._make_status_locked()
            self._render_blink()
        except Exception as exc:  # surfaced as snapshot.error, never raised  # noqa: BLE001
            logger.info("scan refresh failed: %s", exc)
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"
        self._emit(self.snapshot)
        return self.snapshot

    def run_scan(
        self,
        *,
        threshold: float | None = None,
        min_frames: int | None = None,
    ) -> ScanSnapshot:
        """Run :func:`~.scan.scan_project` over ALL sub-frames of ALL
        sessions of the current project (with the given ``threshold`` —
        default: the current one — and ``min_frames``), persist the db rows
        (ticket 01) AND the points sidecar, then refresh + notify. Runtime
        failures are recorded in ``snapshot.error``, never raised; returns
        the snapshot."""
        project = self._require_project()
        effective = self._threshold if threshold is None else validate_threshold(threshold)
        try:
            report = self._call_store(
                lambda: scan_project(
                    project,
                    threshold=effective,
                    min_frames=DEFAULT_MIN_FRAMES if min_frames is None else min_frames,
                )
            )
            self._call_store(lambda: save_scan_points(project, report))
            with self._lock:
                self._threshold = effective
                self._error = None
            self.refresh()
            return self.snapshot
        except Exception as exc:  # surfaced as snapshot.error, never raised  # noqa: BLE001
            logger.info("scan run failed: %s", exc)
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"
        self._emit(self.snapshot)
        return self.snapshot

    # -- threshold + selection ------------------------------------------------------------

    def set_threshold(self, threshold: Any) -> ScanSnapshot:
        """Validate + persist a new variability ``threshold`` into the
        project's ``project.json`` (via :func:`~.settings.set_scan_threshold`),
        re-filter the candidate list (keeping the selection when the selected
        candidate is still above the threshold), notify, and return the
        snapshot. Raises :class:`~.errors.ScanError` for an invalid threshold
        or when no project is set."""
        project = self._require_project()
        value = validate_threshold(threshold)
        self._call_store(lambda: set_scan_threshold(project, value))
        with self._lock:
            self._threshold = value
            self._apply_filter_locked()
            self._status = self._make_status_locked()
            self._error = None
        self._render_blink()
        self._emit(self.snapshot)
        return self.snapshot

    def select_candidate(self, index: int) -> ScanSnapshot:
        """Select the candidate at ``index`` in the filtered candidate list
        (rank order): its lightcurve points are loaded from the sidecar and
        its blink (cutout images + GIF) is rendered; notifies and returns the
        snapshot. Raises :class:`~.errors.ScanError` for a non-int or
        out-of-range index, or when no project is set."""
        self._require_project()
        if isinstance(index, bool) or not isinstance(index, int):
            raise ScanError(f"index must be an int, got {index!r}")
        with self._lock:
            candidates = self._candidates_locked()
            if not candidates or not 0 <= index < len(candidates):
                raise ScanError(
                    f"candidate index {index!r} out of range "
                    f"(0..{max(len(candidates) - 1, 0)})"
                )
            self._selected_index = index
            self._error = None
        self._render_blink()
        self._emit(self.snapshot)
        return self.snapshot

    # -- the blink -------------------------------------------------------------------------

    @property
    def frame_index(self) -> int | None:
        """The index of the displayed blink frame in :attr:`snapshot.frames`."""
        return self._frame_index

    def select_frame(self, index: int) -> ScanSnapshot:
        """Display the blink frame at ``index`` (clamped into the sub list);
        notifies."""
        if isinstance(index, bool) or not isinstance(index, int):
            raise ScanError(f"index must be an int, got {index!r}")
        with self._lock:
            if not self._frames:
                self._frame_index = None
            else:
                self._frame_index = min(max(index, 0), len(self._frames) - 1)
        self._emit(self.snapshot)
        return self.snapshot

    def next_frame(self) -> ScanSnapshot:
        """Display the next blink frame (wraps around — the blink step)."""
        with self._lock:
            if self._frames and self._frame_index is not None:
                self._frame_index = (self._frame_index + 1) % len(self._frames)
        self._emit(self.snapshot)
        return self.snapshot

    def prev_frame(self) -> ScanSnapshot:
        """Display the previous blink frame (wraps around — the blink step)."""
        with self._lock:
            if self._frames and self._frame_index is not None:
                self._frame_index = (self._frame_index - 1) % len(self._frames)
        self._emit(self.snapshot)
        return self.snapshot

    # -- export -----------------------------------------------------------------------------

    def export_csv(self, path: str | Path | None = None) -> Path:
        """Export the currently displayed candidate list (the filtered
        candidates of the latest run, rank order) to a CSV file via
        :func:`~.export.export_candidates_csv` (default
        ``<project>/variable_candidates.csv``); returns the written path.
        Raises :class:`~.errors.ScanError` when no project is set and
        :class:`TypeError` / :class:`OSError` per the exporter."""
        project = self._require_project()
        with self._lock:
            records = self._candidates_locked()
        return self._call_store(lambda: export_candidates_csv(project, records, path))

    # -- snapshot + listeners -----------------------------------------------------------------

    @property
    def snapshot(self) -> ScanSnapshot:
        """The frozen :class:`ScanSnapshot` of the current state (never
        raises)."""
        with self._lock:
            selected = (
                self._candidates_locked()[self._selected_index]
                if self._selected_index is not None
                and 0 <= self._selected_index < len(self._candidates_locked())
                else None
            )
            return ScanSnapshot(
                project_name=self._project.name if self._project is not None else None,
                records=self._records,
                threshold=self._threshold,
                candidates=self._candidates_locked(),
                selected_index=self._selected_index,
                selected=selected,
                points=self._points,
                frames=self._frames,
                frame_index=self._frame_index,
                blink_images=self._blink_images,
                blink_gif=self._blink_gif,
                status=self._status,
                error=self._error,
            )

    @property
    def listeners(self) -> tuple[ScanListener, ...]:
        """The registered observer callbacks, in registration order."""
        return tuple(self._listeners)

    def add_listener(self, listener: ScanListener) -> ScanListener:
        """Register an observer callback (called with a frozen
        :class:`ScanSnapshot` on any change); returns it."""
        if listener not in self._listeners:
            self._listeners.append(listener)
        return listener

    def remove_listener(self, listener: ScanListener) -> None:
        """Unregister an observer callback (no-op when not registered)."""
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _notify(self, snapshot: ScanSnapshot) -> None:
        """Hand a frozen snapshot to every listener; a failing listener is
        logged and isolated (never breaks the others or the caller)."""
        for listener in list(self._listeners):
            try:
                listener(snapshot)
            except Exception:
                logger.exception("scan listener failed")

    def _emit(self, snapshot: ScanSnapshot) -> None:
        """Marshal a listener notification through ``invoke_on_main``."""
        self._invoke_on_main(lambda: self._notify(snapshot))

    # -- internals -----------------------------------------------------------------------------

    def _require_project(self) -> Project:
        if self._project is None:
            raise ScanError("no project set — call set_project() first")
        return self._project

    def _candidates_locked(self) -> tuple[ScanResultRecord, ...]:
        """The records with ``variability >= threshold`` (rank order).
        Caller must hold the lock."""
        return tuple(r for r in self._records if r.variability >= self._threshold)

    def _apply_filter_locked(self) -> None:
        """Re-apply the threshold filter + selection. Caller must hold the
        lock. Auto-selects the first candidate when nothing is selected."""
        candidates = self._candidates_locked()
        if self._selected_index is None:
            self._selected_index = 0 if candidates else None
        else:
            selected = self._selected_record_locked()
            if selected is None or selected not in candidates:
                self._selected_index = 0 if candidates else None
        self._points = self._selected_points_locked()

    def _selected_record_locked(self) -> ScanResultRecord | None:
        candidates = self._candidates_locked()
        if self._selected_index is not None and 0 <= self._selected_index < len(candidates):
            return candidates[self._selected_index]
        return None

    def _selected_points_locked(self) -> tuple[LightcurvePoint, ...]:
        """The selected candidate's lightcurve points from the sidecar payload
        cached in ``self._points_payload``. Caller must hold the lock."""
        record = self._selected_record_locked()
        if record is None:
            return ()
        return _points_for_rank(self._points_payload, record.rank)

    def _make_status_locked(self) -> str:
        candidates = self._candidates_locked()
        return (
            f"{len(candidates)} candidate(s) of {len(self._records)} scanned "
            f"star(s) — threshold {self._threshold:.1f}"
        )

    def _render_blink(self) -> None:
        """(Re-)render the selected candidate's blink: the cutout images of
        all sub-frames + the animated GIF, and the selected candidate's
        points. A render failure is recorded in the snapshot's ``error``
        (never raised)."""
        project = self._project
        with self._lock:
            record = self._selected_record_locked()
            frames = self._frames
            self._points = self._selected_points_locked()
            if record is None or project is None:
                self._blink_images = ()
                self._blink_gif = None
                self._frame_index = 0 if frames else None
                return
            x, y = record.x, record.y
        try:
            images = self._call_store(
                lambda: render_blink_frames(project, frames, x, y)
            )
            gif = render_blink_gif(images, duration_ms=DEFAULT_BLINK_DURATION_MS)
            with self._lock:
                self._blink_images = images
                self._blink_gif = gif
                self._frame_index = 0 if frames else None
                self._error = None
        except Exception as exc:  # surfaced as snapshot.error, never raised  # noqa: BLE001
            logger.info("blink render failed: %s", exc)
            with self._lock:
                self._blink_images = ()
                self._blink_gif = None
                self._error = f"{type(exc).__name__}: {exc}"

    def _call_store(self, fn: Callable[[], Any]) -> Any:
        """Run the store callable ``fn`` and return its result.

        When already on the db-owning thread (or when no db thread was
        captured), ``fn`` runs directly. Otherwise it is marshalled through
        ``invoke_on_main`` (the app: ``GLib.idle_add``) and the calling thread
        waits for the result — the same pattern as the mover controller.

        Before ``fn`` runs, the current project handle is ensured live
        (:meth:`~seestar_controller.store.project.Project.ensure_open`) when
        its db was closed (issue 08: re-opening a project used to leave
        controllers holding a stale, closed handle) — on the same thread
        ``fn`` runs on, so the sqlite thread-affinity is preserved."""
        project = self._project
        db_thread = self._db_thread_id
        if db_thread is None or db_thread == threading.get_ident():
            if project is not None:
                project.ensure_open()  # issue 08: revive a closed db handle
            return fn()
        box: dict[str, Any] = {}
        done = threading.Event()

        def wrapper() -> None:
            try:
                if project is not None:
                    project.ensure_open()  # issue 08: same, on the db thread
                box["result"] = fn()
            except BaseException as exc:  # re-raised on the calling thread  # noqa: BLE001
                box["error"] = exc
            finally:
                done.set()

        self._invoke_on_main(wrapper)
        if not done.wait(DB_CALL_TIMEOUT_S):
            raise ScanError(
                "timed out waiting for the project db thread — is the main "
                "loop running?"
            )
        if "error" in box:
            raise box["error"]
        return box["result"]


__all__ = [
    "DB_CALL_TIMEOUT_S",
    "SCAN_POINTS_FILE",
    "SCAN_POINTS_SCHEMA_VERSION",
    "InvokeOnMain",
    "ScanController",
    "ScanListener",
    "ScanSnapshot",
    "load_scan_points",
    "points_path",
    "save_scan_points",
]
