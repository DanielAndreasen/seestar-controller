"""Annotated overlay rendering + the mover controller — the PURE layer of
moving-objects issue 02.

Pure-Python (module boundary per ``.scratch/moving-objects/spec.md``): no
GTK imports, no :mod:`seestar_controller.link` / ``ui`` / ``enhance``
imports — a subprocess test enforces the boundary. numpy + PIL + the store
+ the sibling pure modules only. The widget layer (:mod:`.overlay_view`)
imports ``gi`` at module top and is never imported by this module or
eagerly by the package ``__init__``.

What lives here:

- :func:`tonemap_gray` — a LOCAL percentile tone-map of one frame to uint8
  gray (deliberately NOT :mod:`seestar_controller.enhance.render` — the
  module boundary forbids importing enhance; the overlay only needs a plain
  linear stretch, NaN -> 0);
- :func:`render_frame_overlay` — render one session frame as an RGB
  :class:`PIL.Image.Image` with the detected tracks drawn on top: every
  track as a polyline through its detections, every detection as a point
  marker, the detections of the CURRENT frame ringed, and each flagged
  mover labelled (``T{id} {speed}px/f {dir}°`` plus the MPC designation when
  the lookup matched one);
- :class:`MoverSnapshot` — the frozen snapshot the widget renders;
- :class:`MoverController` — the app-level shared controller (the same role
  as the enhance :class:`~seestar_controller.enhance.viewer_controller.ViewerController`
  and the analysis
  :class:`~seestar_controller.analysis.lightcurve.LightcurveController`):
  current project, session list, selected session's subs + tracks + movers,
  the displayed frame (with wrap-around prev/next — the blink), the
  annotated overlay image, and frozen-snapshot listener notifications
  marshalled through ``invoke_on_main`` (the app binds ``GLib.idle_add``).

Threading / sqlite thread-affinity: like the lightcurve controller, every
STORE call goes through :meth:`MoverController._call_store` (direct on the
db-owning thread, otherwise marshalled through ``invoke_on_main`` with a
wait), so the widget can run :meth:`MoverController.analyze_session` and
:meth:`MoverController.lookup_mpc` on a worker thread. The MPC HTTP fetch
itself runs on the calling (worker) thread — never on the db thread.

Error policy: argument validation raises immediately
(:class:`~.errors.MovingObjectsError` subclasses); runtime failures (store,
network, unreadable frame) are recorded in the snapshot's ``error`` and the
listeners are notified — never raised — because the widget runs the
operations on a worker thread and renders ``snapshot.error`` as the status.
"""

from __future__ import annotations

import io
import logging
import math
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

from seestar_controller.store.errors import SessionNotFoundError
from seestar_controller.store.frames import (
    KIND_SUB,
    Frame,
    list_frames,
    read_frame_bytes,
)
from seestar_controller.store.project import Project
from seestar_controller.store.sessions import list_sessions

from .detect import DEFAULT_FWHM_PX, DEFAULT_THRESHOLD_SIGMA, detect_in_subs
from .errors import MovingObjectsError, PersistenceError
from .flag import (
    DEFAULT_MAX_RESIDUAL_PX,
    DEFAULT_MIN_FRAMES,
    DEFAULT_MIN_SPEED_PX_PER_FRAME,
    MoverReport,
    flag_linear_movers,
)
from .link import DEFAULT_MAX_MOTION_PX, Track, link_tracks
from .mpc import DEFAULT_OBSCODE, DEFAULT_TIMEOUT_S, lookup_movers, with_matches
from .persist import load_tracks, save_tracks

logger = logging.getLogger(__name__)

#: Default lower percentile of the finite pixel values for the overlay
#: tone-map black point.
DEFAULT_LO_PERCENTILE = 0.1

#: Default upper percentile of the finite pixel values for the overlay
#: tone-map white point.
DEFAULT_HI_PERCENTILE = 99.5

#: Default maximum overlay dimension in pixels (the frame is downscaled to
#: this before the annotations are drawn, so big subs stay interactive).
DEFAULT_MAX_OVERLAY_SIZE = 1024

#: RGBA color of a non-mover track polyline.
TRACK_COLOR = (255, 90, 90, 255)

#: RGBA color of a flagged mover's polyline + label.
MOVER_COLOR = (255, 215, 60, 255)

#: RGBA color of the current-frame detection rings.
CURRENT_COLOR = (255, 255, 255, 255)

#: Radius of a detection point marker, in pixels.
POINT_RADIUS = 3

#: Radius of the current-frame ring, in pixels.
RING_RADIUS = 8

#: Width of a track polyline, in pixels.
LINE_WIDTH = 2

#: Timeout for a store call marshalled to the db-owning thread, in seconds.
DB_CALL_TIMEOUT_S = 30.0

#: A listener: called with a frozen :class:`MoverSnapshot` on any change.
MoverListener = Callable[["MoverSnapshot"], None]

#: The marshalling callable for listener notifications (the app binds
#: ``GLib.idle_add``; the default calls directly).
InvokeOnMain = Callable[[Callable[[], None]], Any]


def _direct_invoke(callback: Callable[[], None]) -> None:
    """The default ``invoke_on_main``: run the callback directly (tests)."""
    callback()


# --- tone map + overlay rendering ------------------------------------------------------


def tonemap_gray(
    data: Any,
    *,
    lo_percentile: float = DEFAULT_LO_PERCENTILE,
    hi_percentile: float = DEFAULT_HI_PERCENTILE,
) -> np.ndarray:
    """Tone-map a 2-D float image to a uint8 gray image (NaN/inf -> 0).

    The black/white points are the ``lo_percentile`` / ``hi_percentile``
    percentiles of the FINITE values; a degenerate range (``hi <= lo``, e.g.
    a constant frame) is widened so the mapping never divides by zero.
    Raises :class:`~.errors.MovingObjectsError` for a non-2-D input or bad
    percentiles.
    """
    arr = np.asarray(data, dtype=np.float32)
    if arr.ndim != 2:
        raise MovingObjectsError(
            f"tonemap_gray expects a 2-D (H, W) array, got shape {arr.shape}"
        )
    for name, value in (("lo_percentile", lo_percentile), ("hi_percentile", hi_percentile)):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0.0 <= value <= 100.0
        ):
            raise MovingObjectsError(
                f"{name} must be a finite number in [0, 100], got {value!r}"
            )
    finite = np.isfinite(arr)
    values = arr[finite]
    if values.size == 0:
        return np.zeros(arr.shape, dtype=np.uint8)
    lo = float(np.percentile(values, lo_percentile))
    hi = float(np.percentile(values, hi_percentile))
    if hi <= lo:
        hi = lo + 1.0
    scaled = (arr - lo) / (hi - lo)
    scaled = np.where(finite, scaled, 0.0)
    np.clip(scaled, 0.0, 1.0, out=scaled)
    return (scaled * 255.0).astype(np.uint8)


def _mover_label(mover: MoverReport) -> str:
    """The overlay label of one mover: track id, speed, direction, and the
    MPC designation when the lookup matched a known object."""
    label = (
        f"T{mover.track_id} {mover.speed_px_per_frame:.1f}px/f "
        f"{mover.direction_deg:.0f}\u00b0"
    )
    if mover.mpc_match is not None:
        label += f"  {mover.mpc_match.designation}"
    return label


def render_frame_overlay(
    image_data: Any,
    tracks: Sequence[Track],
    movers: Sequence[MoverReport],
    *,
    frame_id: int | None = None,
    lo_percentile: float = DEFAULT_LO_PERCENTILE,
    hi_percentile: float = DEFAULT_HI_PERCENTILE,
    max_size: int = DEFAULT_MAX_OVERLAY_SIZE,
) -> Image.Image:
    """Render one session frame as an RGB PIL image with the tracks drawn on.

    - ``image_data`` — the frame's 2-D pixel array (float; NaN allowed);
    - ``tracks`` / ``movers`` — the session's linked tracks and flagged
      movers (a mover's track is drawn in the mover color, the rest in the
      track color);
    - ``frame_id`` — the displayed frame's id: its detections get a white
      ring (the "current position" marker of the blink);
    - ``max_size`` — the image is downscaled to at most this many pixels on
      its long side BEFORE the annotations are drawn (annotations stay
      crisp; coordinates are scaled accordingly).

    Returns an RGB :class:`PIL.Image.Image`. Raises
    :class:`~.errors.MovingObjectsError` for a bad image or bad arguments.
    """
    if isinstance(max_size, bool) or not isinstance(max_size, int) or max_size < 1:
        raise MovingObjectsError(f"max_size must be a positive int, got {max_size!r}")
    gray = tonemap_gray(image_data, lo_percentile=lo_percentile, hi_percentile=hi_percentile)
    image = Image.fromarray(gray, mode="L").convert("RGB")
    scale = 1.0
    if max_size and max(image.size) > max_size:
        scale = max_size / float(max(image.size))
        new_size = (
            max(1, round(image.width * scale)),
            max(1, round(image.height * scale)),
        )
        image = image.resize(new_size, Image.Resampling.BILINEAR)
    draw = ImageDraw.Draw(image)
    for mover in movers:
        if not isinstance(mover, MoverReport):
            raise MovingObjectsError(
                f"movers must be MoverReport records, got {type(mover).__name__}"
            )
    mover_ids = {m.track_id for m in movers}
    for track in tracks:
        if not isinstance(track, Track):
            raise MovingObjectsError(
                f"tracks must be Track records, got {type(track).__name__}"
            )
        color = MOVER_COLOR if track.track_id in mover_ids else TRACK_COLOR
        points = [(p.x * scale, p.y * scale) for p in track.points]
        if len(points) >= 2:
            draw.line(points, fill=color, width=LINE_WIDTH)
        for point, (px, py) in zip(track.points, points):
            r = POINT_RADIUS * scale
            draw.ellipse((px - r, py - r, px + r, py + r), fill=color)
            if frame_id is not None and point.frame_id == frame_id:
                rr = RING_RADIUS * scale
                draw.ellipse((px - rr, py - rr, px + rr, py + rr), outline=CURRENT_COLOR, width=2)
    for mover in movers:
        track = next((t for t in tracks if t.track_id == mover.track_id), None)
        anchor = None
        if track is not None:
            if frame_id is not None:
                anchor = next((p for p in track.points if p.frame_id == frame_id), None)
            if anchor is None:
                anchor = track.points[0]
        if anchor is None:
            anchor_x, anchor_y = mover.start_x, mover.start_y
        else:
            anchor_x, anchor_y = anchor.x, anchor.y
        draw.text(
            ((anchor_x + RING_RADIUS + 2) * scale, (anchor_y - RING_RADIUS - 2) * scale),
            _mover_label(mover),
            fill=MOVER_COLOR,
        )
    return image


def _frame_image_data(project: Project, frame: Frame) -> np.ndarray:
    """Read one stored sub-frame's 2-D pixel array (lazy astropy import)."""
    from astropy.io import fits as pyfits

    raw = read_frame_bytes(project, frame)
    with pyfits.open(io.BytesIO(raw)) as hdul:
        data = hdul[0].data
    arr = np.asarray(data, dtype=np.float32)
    if arr.ndim != 2:
        raise MovingObjectsError(
            f"frame {frame.id} is not a 2-D image (shape {arr.shape}) — cannot render"
        )
    return arr


# --- snapshot + controller ---------------------------------------------------------------


@dataclass(frozen=True)
class MoverSnapshot:
    """An immutable snapshot of everything the overlay view renders, handed
    to the listeners on any change.

    - ``project_name`` — the current project's display name (None when no
      project is set);
    - ``sessions`` — the project's session ids (ordered by ``started_at``);
    - ``session_id`` — the selected session (None when none);
    - ``frames`` — the selected session's subs (ordered by ``jd``);
    - ``tracks`` / ``movers`` — the session's linked tracks / flagged movers
      (empty until :meth:`MoverController.analyze_session` ran or a saved
      sidecar was loaded);
    - ``frame_index`` — the index of the displayed frame in ``frames``
      (None when the session has no subs);
    - ``image`` — the annotated overlay of the displayed frame (None when no
      frame is displayed);
    - ``status`` — a short human-readable status (e.g. ``"2 movers"``,
      ``"MPC: 1/2 matched"``), else None;
    - ``error`` — the last error message, else None.
    """

    project_name: str | None
    sessions: tuple[str, ...]
    session_id: str | None
    frames: tuple[Frame, ...]
    tracks: tuple[Track, ...]
    movers: tuple[MoverReport, ...]
    frame_index: int | None
    image: Image.Image | None
    status: str | None
    error: str | None


class MoverController:
    """The app-level moving-objects controller: the current project, its
    sessions, the selected session's subs + tracks + movers, the displayed
    (blinked) frame with its annotated overlay, and observer notification.

    - ``project`` — the store project (None = follow the attached viewer
      controller's project, see :meth:`attach_viewer`);
    - ``viewer_controller`` — an optional duck-typed viewer controller (the
      shared :class:`~seestar_controller.enhance.viewer_controller.ViewerController`
      — NOT imported here): its project is adopted whenever no explicit
      project is set, so the mover overlay follows the currently open project;
    - ``invoke_on_main`` — the marshalling callable for listener
      notifications AND for store calls made from a non-db thread (the app
      binds ``GLib.idle_add``; the default calls directly);
    - ``cache`` — the optional :class:`~seestar_controller.catalog.cache.QueryCache`
      used by :meth:`lookup_mpc` (default: the MPC cache dir with the
      default TTL).

    Operations: :attr:`snapshot` (never raises), :meth:`set_project`,
    :meth:`attach_viewer` / :meth:`detach_viewer`, :meth:`refresh_sessions`,
    :meth:`select_session`, :meth:`analyze_session` (detect -> link -> flag
    -> persist), :meth:`lookup_mpc` (cached MPC lookup -> persist the
    matches), :meth:`select_frame` / :meth:`next_frame` / :meth:`prev_frame`
    (the blink), and listeners with frozen snapshots.
    """

    def __init__(
        self,
        project: Project | None = None,
        viewer_controller: Any | None = None,
        *,
        invoke_on_main: InvokeOnMain | None = None,
        cache: Any | None = None,
    ) -> None:
        self._project: Project | None = None
        self._viewer_controller: Any | None = None
        self._invoke_on_main: InvokeOnMain = invoke_on_main or _direct_invoke
        self._cache = cache
        self._lock = threading.Lock()
        self._listeners: list[MoverListener] = []
        self._db_thread_id: int | None = None
        self._sessions: tuple[str, ...] = ()
        self._session_id: str | None = None
        self._frames: tuple[Frame, ...] = ()
        self._tracks: tuple[Track, ...] = ()
        self._movers: tuple[MoverReport, ...] = ()
        self._frame_index: int | None = None
        self._image: Image.Image | None = None
        self._status: str | None = None
        self._error: str | None = None
        if viewer_controller is not None:
            self.attach_viewer(viewer_controller)
        if project is not None:
            self.set_project(project)

    # -- project + sessions -----------------------------------------------------------

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
        no explicit project is set, so the mover overlay follows the currently
        open project. Re-attaching detaches the previous viewer first."""
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
        project is set) so the overlay follows the open project — emits on
        every viewer change, so closing the project clears the view."""
        if self._project is not None or self._viewer_controller is None:
            return
        self.set_project(getattr(self._viewer_controller, "project", None))

    @property
    def invoke_on_main(self) -> InvokeOnMain:
        """The marshalling callable (the app binds ``GLib.idle_add``)."""
        return self._invoke_on_main

    def set_project(self, project: Project | None) -> None:
        """Set/replace the current project (None clears it): the session list
        is re-listed and the session state is cleared. Notifies."""
        self._project = project
        self._db_thread_id = threading.get_ident() if project is not None else None
        with self._lock:
            self._sessions = ()
            self._session_id = None
            self._frames = ()
            self._tracks = ()
            self._movers = ()
            self._frame_index = None
            self._image = None
            self._status = None
            self._error = None
        if project is not None:
            try:
                self.refresh_sessions()
                return  # refresh_sessions already notified
            except MovingObjectsError as exc:
                with self._lock:
                    self._error = str(exc)
        self._emit(self.snapshot)

    @property
    def sessions(self) -> tuple[str, ...]:
        """The current project's session ids (ordered by ``started_at``)."""
        return self._sessions

    def refresh_sessions(self) -> tuple[str, ...]:
        """(Re-)list the project's sessions via the store; notifies and
        returns the ids. Raises :class:`~.errors.MovingObjectsError` when no
        project is set or the store call fails."""
        project = self._require_project()
        try:
            sessions = self._call_store(lambda: list_sessions(project))
        except Exception as exc:
            raise MovingObjectsError(
                f"cannot list the sessions of project {project.name!r}: {exc}"
            ) from exc
        with self._lock:
            self._sessions = tuple(s.id for s in sessions)
            self._error = None
        self._emit(self.snapshot)
        return self._sessions

    # -- session selection + analysis ---------------------------------------------------

    @property
    def session_id(self) -> str | None:
        """The selected session id (None when none)."""
        return self._session_id

    def select_session(self, session_id: str) -> MoverSnapshot:
        """Select a session: list its subs, load the persisted tracks/movers
        from the sidecar when present (empty otherwise), display the first
        frame, and notify. Raises
        :class:`~seestar_controller.store.errors.SessionNotFoundError` for an
        unknown session id (recorded in the snapshot first)."""
        project = self._require_project()
        if not isinstance(session_id, str) or not session_id:
            raise MovingObjectsError(f"session_id must be a non-empty string, got {session_id!r}")
        known = self._call_store(lambda: {s.id for s in list_sessions(project)})
        if session_id not in known:
            exc = SessionNotFoundError(f"unknown session {session_id!r}")
            with self._lock:
                self._error = str(exc)
            self._emit(self.snapshot)
            raise exc
        frames = tuple(self._call_store(lambda: list_frames(project, session_id, kind=KIND_SUB)))
        try:
            session = self._call_store(lambda: load_tracks(project, session_id))
            tracks, movers = session.tracks, session.movers
        except PersistenceError:
            tracks, movers = (), ()  # no sidecar yet: analyze_session creates it
        with self._lock:
            self._session_id = session_id
            self._frames = frames
            self._tracks = tuple(tracks)
            self._movers = tuple(movers)
            self._frame_index = 0 if frames else None
            self._status = (
                f"{len(movers)} mover(s) in {len(tracks)} track(s)"
                if tracks or movers
                else None
            )
            self._error = None
        self._render_current()
        self._emit(self.snapshot)
        return self.snapshot

    def analyze_session(
        self,
        session_id: str | None = None,
        *,
        threshold: float = DEFAULT_THRESHOLD_SIGMA,
        fwhm: float = DEFAULT_FWHM_PX,
        max_frames: int | None = None,
        max_motion_px: float = DEFAULT_MAX_MOTION_PX,
        min_frames: int = DEFAULT_MIN_FRAMES,
        max_residual_px: float = DEFAULT_MAX_RESIDUAL_PX,
        min_speed_px_per_frame: float = DEFAULT_MIN_SPEED_PX_PER_FRAME,
    ) -> MoverSnapshot:
        """Run the detect -> link -> flag pipeline over a session's subs and
        persist the result to the ``movers.json`` sidecar, then display the
        session (first frame). ``session_id`` defaults to the selected
        session. Runtime failures are recorded in ``snapshot.error`` (and
        the listeners notified), never raised; returns the snapshot."""
        project = self._require_project()
        sid = session_id if session_id is not None else self._session_id
        if sid is None:
            raise MovingObjectsError("no session selected — call select_session() first")
        try:
            detections = self._call_store(
                lambda: detect_in_subs(
                    project, sid, threshold=threshold, fwhm=fwhm, max_frames=max_frames
                )
            )
            tracks = link_tracks(detections, max_motion_px=max_motion_px)
            movers = flag_linear_movers(
                tracks,
                min_frames=min_frames,
                max_residual_px=max_residual_px,
                min_speed_px_per_frame=min_speed_px_per_frame,
            )
            self._call_store(lambda: save_tracks(project, sid, tracks, movers))
            frames = tuple(self._call_store(lambda: list_frames(project, sid, kind=KIND_SUB)))
            with self._lock:
                self._session_id = sid
                self._frames = frames
                self._tracks = tuple(tracks)
                self._movers = tuple(movers)
                self._frame_index = 0 if frames else None
                self._status = f"{len(movers)} mover(s) in {len(tracks)} track(s)"
                self._error = None
            self._render_current()
        except Exception as exc:  # surfaced as snapshot.error, never raised  # noqa: BLE001
            logger.info("mover analysis failed: %s", exc)
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"
        self._emit(self.snapshot)
        return self.snapshot

    def lookup_mpc(
        self,
        session_id: str | None = None,
        *,
        obscode: str = DEFAULT_OBSCODE,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> MoverSnapshot:
        """Look every persisted mover of a session up in the MPC (cached —
        the HTTP fetch runs on the CALLING thread, the store reads/writes on
        the db thread) and persist the matches into the sidecar
        (schema_version 2). ``session_id`` defaults to the selected session.
        Runtime failures (network, no sidecar yet) are recorded in
        ``snapshot.error``, never raised; returns the snapshot."""
        project = self._require_project()
        sid = session_id if session_id is not None else self._session_id
        if sid is None:
            raise MovingObjectsError("no session selected — call select_session() first")
        try:
            session = self._call_store(lambda: load_tracks(project, sid))
            # the network call itself stays OFF the db thread:
            matches = lookup_movers(session.movers, self._cache, obscode=obscode, timeout=timeout)
            movers = with_matches(session.movers, matches)
            self._call_store(lambda: save_tracks(project, sid, list(session.tracks), movers))
            reloaded = self._call_store(lambda: load_tracks(project, sid))
            n_match = sum(1 for m in reloaded.movers if m.mpc_match is not None)
            with self._lock:
                self._session_id = sid
                self._tracks = reloaded.tracks
                self._movers = reloaded.movers
                self._status = f"MPC: {n_match}/{len(reloaded.movers)} matched"
                self._error = None
            self._render_current()
        except Exception as exc:  # surfaced as snapshot.error, never raised  # noqa: BLE001
            logger.info("MPC lookup failed: %s", exc)
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"
        self._emit(self.snapshot)
        return self.snapshot

    # -- frame selection (the blink) ------------------------------------------------------

    @property
    def frame_index(self) -> int | None:
        """The index of the displayed frame in :attr:`snapshot.frames`."""
        return self._frame_index

    @property
    def current_frame(self) -> Frame | None:
        """The displayed sub-frame (None when the session has no subs)."""
        if self._frame_index is None or not self._frames:
            return None
        return self._frames[self._frame_index]

    @property
    def image(self) -> Image.Image | None:
        """The annotated overlay of the displayed frame (None when none)."""
        return self._image

    def select_frame(self, index: int) -> MoverSnapshot:
        """Display the frame at ``index`` (clamped into the session's subs);
        re-renders the overlay and notifies."""
        if isinstance(index, bool) or not isinstance(index, int):
            raise MovingObjectsError(f"index must be an int, got {index!r}")
        with self._lock:
            if not self._frames:
                self._frame_index = None
                self._image = None
            else:
                self._frame_index = min(max(index, 0), len(self._frames) - 1)
        self._render_current()
        self._emit(self.snapshot)
        return self.snapshot

    def next_frame(self) -> MoverSnapshot:
        """Display the next frame (wraps around — the blink step)."""
        with self._lock:
            if self._frames and self._frame_index is not None:
                self._frame_index = (self._frame_index + 1) % len(self._frames)
        self._render_current()
        self._emit(self.snapshot)
        return self.snapshot

    def prev_frame(self) -> MoverSnapshot:
        """Display the previous frame (wraps around — the blink step)."""
        with self._lock:
            if self._frames and self._frame_index is not None:
                self._frame_index = (self._frame_index - 1) % len(self._frames)
        self._render_current()
        self._emit(self.snapshot)
        return self.snapshot

    # -- snapshot + listeners ---------------------------------------------------------------

    @property
    def snapshot(self) -> MoverSnapshot:
        """The frozen :class:`MoverSnapshot` of the current state (never
        raises)."""
        with self._lock:
            return MoverSnapshot(
                project_name=self._project.name if self._project is not None else None,
                sessions=self._sessions,
                session_id=self._session_id,
                frames=self._frames,
                tracks=self._tracks,
                movers=self._movers,
                frame_index=self._frame_index,
                image=self._image,
                status=self._status,
                error=self._error,
            )

    @property
    def listeners(self) -> tuple[MoverListener, ...]:
        """The registered observer callbacks, in registration order."""
        return tuple(self._listeners)

    def add_listener(self, listener: MoverListener) -> MoverListener:
        """Register an observer callback (idempotent); returns it, so it can
        be used as a decorator. The listener receives a frozen
        :class:`MoverSnapshot` on any change (marshalled via
        ``invoke_on_main``)."""
        if listener not in self._listeners:
            self._listeners.append(listener)
        return listener

    def remove_listener(self, listener: MoverListener) -> None:
        """Unregister an observer callback (no-op when not registered)."""
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _notify(self, snapshot: MoverSnapshot) -> None:
        """Hand a frozen snapshot to every listener; a failing listener is
        logged and isolated (never breaks the others or the caller)."""
        for listener in list(self._listeners):
            try:
                listener(snapshot)
            except Exception:
                logger.exception("mover listener failed")

    def _emit(self, snapshot: MoverSnapshot) -> None:
        """Marshal a listener notification through ``invoke_on_main``."""
        self._invoke_on_main(lambda: self._notify(snapshot))

    # -- internals ---------------------------------------------------------------------------

    def _require_project(self) -> Project:
        if self._project is None:
            raise MovingObjectsError("no project set — call set_project() first")
        return self._project

    def _call_store(self, fn: Callable[[], Any]) -> Any:
        """Run the store callable ``fn`` and return its result.

        When already on the db-owning thread (or when no db thread was
        captured), ``fn`` runs directly. Otherwise it is marshalled through
        ``invoke_on_main`` (the app: ``GLib.idle_add``) and the calling thread
        waits for the result — the same pattern as the lightcurve controller.

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
            raise MovingObjectsError(
                "timed out waiting for the project db thread — is the main "
                "loop running?"
            )
        if "error" in box:
            raise box["error"]
        return box["result"]

    def _render_current(self) -> None:
        """Re-render the annotated overlay of the displayed frame (None when
        no frame is displayed). A render failure is recorded in the
        snapshot's ``error`` (never raised)."""
        frame = self.current_frame
        project = self._project
        if frame is None or project is None:
            with self._lock:
                self._image = None
            return
        try:
            data = self._call_store(lambda: _frame_image_data(project, frame))
            with self._lock:
                tracks, movers = self._tracks, self._movers
            self._image = render_frame_overlay(data, tracks, movers, frame_id=frame.id)
        except Exception as exc:  # surfaced as snapshot.error, never raised  # noqa: BLE001
            logger.info("overlay render failed: %s", exc)
            with self._lock:
                self._image = None
                self._error = f"{type(exc).__name__}: {exc}"


__all__ = [
    "CURRENT_COLOR",
    "DB_CALL_TIMEOUT_S",
    "DEFAULT_HI_PERCENTILE",
    "DEFAULT_LO_PERCENTILE",
    "DEFAULT_MAX_OVERLAY_SIZE",
    "LINE_WIDTH",
    "MOVER_COLOR",
    "POINT_RADIUS",
    "RING_RADIUS",
    "TRACK_COLOR",
    "InvokeOnMain",
    "MoverController",
    "MoverListener",
    "MoverSnapshot",
    "render_frame_overlay",
    "tonemap_gray",
]
