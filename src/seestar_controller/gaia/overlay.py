"""Cross-match overlay rendering + the cross-match controller — the PURE layer
of gaia-crossmatch issue 03.

Pure-Python (module boundary per ``.scratch/gaia-crossmatch/spec.md``): no
GTK imports, no :mod:`seestar_controller.link` / ``ui`` / ``enhance``
imports — a subprocess test enforces the boundary. numpy + PIL + the store
+ the sibling pure gaia modules only. The widget layer (:mod:`.overlay_view`)
imports ``gi`` at module top and is never imported by this module or eagerly
by the package ``__init__``.

What lives here:

- :func:`tonemap_gray` — a LOCAL percentile tone-map of the stack to uint8
  gray (deliberately NOT :mod:`seestar_controller.enhance.render` — the
  module boundary forbids importing enhance; the overlay only needs a plain
  linear stretch, NaN -> 0);
- :func:`render_stack_overlay` — render the stack as an RGB
  :class:`PIL.Image.Image` with the cross-match drawn on top: every MATCHED
  detection ringed and labelled with its Gaia G magnitude (``G 12.34``),
  every UNMATCHED detection flagged with a distinct X marker;
- :class:`CrossmatchSnapshot` — the frozen snapshot the widget renders;
- :class:`CrossmatchController` — the app-level shared controller (the same
  role as the enhance
  :class:`~seestar_controller.enhance.viewer_controller.ViewerController` and
  the moving-objects
  :class:`~seestar_controller.moving_objects.overlay.MoverController`): it
  follows the shared viewer controller (the open stack becomes the
  cross-match target), runs the issue-01 pipeline (detect -> Gaia cone
  search, cache-first -> match) with the configurable match radius + G
  limit, persists the table via the issue-02 glue, and renders the annotated
  overlay. Listener notifications are marshalled through ``invoke_on_main``
  (the app binds ``GLib.idle_add``).

Threading / sqlite thread-affinity: like the mover controller, every STORE
call goes through :meth:`CrossmatchController._call_store` (direct on the
db-owning thread, otherwise marshalled through ``invoke_on_main`` with a
wait), so the widget can run :meth:`CrossmatchController.run_crossmatch` on
a worker thread. The Gaia HTTP fetch itself runs on the calling (worker)
thread — never on the db thread.

Error policy: argument validation raises immediately (:class:`ValueError`
for a bad radius / G limit, :class:`~.errors.GaiaError` for a missing
project/stack); runtime failures (store, WCS, network, unreadable frame) are
recorded in the snapshot's ``error`` and the listeners are notified — never
raised — because the widget runs the operations on a worker thread and
renders ``snapshot.error`` as the status.
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

from seestar_controller.store.frames import (
    KIND_STACK,
    Frame,
    header_to_dict,
    list_frames,
    read_frame_bytes,
)
from seestar_controller.store.project import Project

from .detect import detect_sources, field_center_and_radius
from .errors import GaiaError
from .matching import DEFAULT_MATCH_RADIUS_ARCSEC, crossmatch
from .persistence import load_crossmatch, save_crossmatch
from .query import DEFAULT_G_LIMIT, query_gaia_cached

logger = logging.getLogger(__name__)

#: Default lower percentile of the finite pixel values for the overlay
#: tone-map black point.
DEFAULT_LO_PERCENTILE = 0.1

#: Default upper percentile of the finite pixel values for the overlay
#: tone-map white point.
DEFAULT_HI_PERCENTILE = 99.5

#: Default maximum overlay dimension in pixels (the stack is downscaled to
#: this before the annotations are drawn, so big stacks stay interactive).
DEFAULT_MAX_OVERLAY_SIZE = 1024

#: RGBA color of a matched detection (ring + point + G-mag label).
MATCHED_COLOR = (90, 255, 140, 255)

#: RGBA color of an unmatched detection (the distinct X marker).
UNMATCHED_COLOR = (255, 90, 90, 255)

#: Radius of a detection point marker, in pixels.
POINT_RADIUS = 3

#: Radius of a matched detection's ring / an unmatched detection's X, in
#: pixels.
RING_RADIUS = 8

#: Width of the marker outlines, in pixels.
LINE_WIDTH = 2

#: Timeout for a store call marshalled to the db-owning thread, in seconds.
DB_CALL_TIMEOUT_S = 30.0

#: Smallest match radius the controls accept, in arcseconds.
MIN_MATCH_RADIUS_ARCSEC = 0.1

#: Largest match radius the controls accept, in arcseconds.
MAX_MATCH_RADIUS_ARCSEC = 10.0

#: Brightest G limit the controls accept.
MIN_G_LIMIT = 10.0

#: Faintest G limit the controls accept.
MAX_G_LIMIT = 22.0

#: A listener: called with a frozen :class:`CrossmatchSnapshot` on any change.
CrossmatchListener = Callable[["CrossmatchSnapshot"], None]

#: The marshalling callable for listener notifications (the app binds
#: ``GLib.idle_add``; the default calls directly).
InvokeOnMain = Callable[[Callable[[], None]], Any]


def _direct_invoke(callback: Callable[[], None]) -> None:
    """The default ``invoke_on_main``: run the callback directly (tests)."""
    callback()


# --- validation ---------------------------------------------------------------------


def _validate_radius_arcsec(value: Any) -> float:
    """Validate a match radius for the controls (raises ``ValueError``)."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not MIN_MATCH_RADIUS_ARCSEC <= float(value) <= MAX_MATCH_RADIUS_ARCSEC
    ):
        raise ValueError(
            f"radius_arcsec must be a finite number in "
            f"[{MIN_MATCH_RADIUS_ARCSEC}, {MAX_MATCH_RADIUS_ARCSEC}] arcsec, got {value!r}"
        )
    return float(value)


def _validate_g_limit(value: Any) -> float:
    """Validate a Gaia G limit for the controls (raises ``ValueError``)."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not MIN_G_LIMIT <= float(value) <= MAX_G_LIMIT
    ):
        raise ValueError(
            f"g_limit must be a finite number in [{MIN_G_LIMIT}, {MAX_G_LIMIT}], got {value!r}"
        )
    return float(value)


def _finite_number(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise GaiaError(f"{what} must be a finite number, got {value!r}")
    return float(value)


def _row_fields(row: Any) -> tuple[float, float, bool, float | None]:
    """The overlay fields of one cross-match row (duck-typed: anything with
    numeric ``x``/``y``, a boolean ``matched`` and — when matched — a finite
    ``g`` for the label, e.g. :class:`~.matching.CrossmatchRow` or
    :class:`~seestar_controller.store.CrossmatchRecord`)."""
    x = _finite_number(getattr(row, "x", None), "row.x")
    y = _finite_number(getattr(row, "y", None), "row.y")
    matched = getattr(row, "matched", None)
    if not isinstance(matched, bool):
        raise GaiaError(f"row.matched must be a bool, got {matched!r}")
    g: float | None = None
    if matched:
        g = _finite_number(getattr(row, "g", None), "row.g of a matched row (the G-mag label)")
    return x, y, matched, g


def _gray(data: Any) -> np.ndarray:
    """A 2-D float32 view of image data (RGB ``(H, W, 3)`` -> luminance)."""
    arr = np.asarray(data, dtype=np.float32)
    if arr.ndim == 3:
        arr = arr.mean(axis=2)
    if arr.ndim != 2:
        raise GaiaError(f"stack image must be 2-D (or RGB), got shape {arr.shape}")
    return arr


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
    Raises :class:`~.errors.GaiaError` for a non-2-D input or bad percentiles.
    """
    arr = np.asarray(data, dtype=np.float32)
    if arr.ndim != 2:
        raise GaiaError(f"tonemap_gray expects a 2-D (H, W) array, got shape {arr.shape}")
    for name, value in (("lo_percentile", lo_percentile), ("hi_percentile", hi_percentile)):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0.0 <= value <= 100.0
        ):
            raise GaiaError(f"{name} must be a finite number in [0, 100], got {value!r}")
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


def render_stack_overlay(
    image_data: Any,
    rows: Sequence[Any],
    *,
    lo_percentile: float = DEFAULT_LO_PERCENTILE,
    hi_percentile: float = DEFAULT_HI_PERCENTILE,
    max_size: int = DEFAULT_MAX_OVERLAY_SIZE,
) -> Image.Image:
    """Render the stack as an RGB PIL image with the cross-match drawn on.

    - ``image_data`` — the stack's 2-D pixel array (float; NaN allowed; an
      RGB ``(H, W, 3)`` array is converted to luminance);
    - ``rows`` — the cross-match rows (duck-typed, see :func:`_row_fields`):
      every MATCHED row is drawn as a filled point + ring and labelled with
      its Gaia G magnitude (``G 12.34``); every UNMATCHED row is drawn as a
      distinct X marker in :data:`UNMATCHED_COLOR`;
    - ``max_size`` — the image is downscaled to at most this many pixels on
      its long side BEFORE the annotations are drawn (annotations stay
      crisp; the row coordinates are scaled accordingly).

    Returns an RGB :class:`PIL.Image.Image`. Raises :class:`~.errors.GaiaError`
    for a bad image, bad percentiles, a bad ``max_size``, or a malformed row.
    """
    if isinstance(max_size, bool) or not isinstance(max_size, int) or max_size < 1:
        raise GaiaError(f"max_size must be a positive int, got {max_size!r}")
    gray = tonemap_gray(_gray(image_data), lo_percentile=lo_percentile, hi_percentile=hi_percentile)
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
    for row in rows:
        x, y, matched, g = _row_fields(row)
        px, py = x * scale, y * scale
        if matched:
            r = POINT_RADIUS * scale
            draw.ellipse((px - r, py - r, px + r, py + r), fill=MATCHED_COLOR)
            rr = RING_RADIUS * scale
            draw.ellipse((px - rr, py - rr, px + rr, py + rr), outline=MATCHED_COLOR, width=LINE_WIDTH)
            draw.text(
                ((px + rr + 2.0), (py - rr - 2.0)),
                f"G {g:.2f}",
                fill=MATCHED_COLOR,
            )
        else:
            rr = RING_RADIUS * scale
            draw.line((px - rr, py - rr, px + rr, py + rr), fill=UNMATCHED_COLOR, width=LINE_WIDTH)
            draw.line((px - rr, py + rr, px + rr, py - rr), fill=UNMATCHED_COLOR, width=LINE_WIDTH)
    return image


def _load_stack_image(project: Project, frame: Frame) -> tuple[np.ndarray, dict[str, Any]]:
    """Read one stored stack's 2-D pixel array + sanitized header dict
    (lazy astropy import)."""
    from astropy.io import fits as pyfits

    raw = read_frame_bytes(project, frame)
    with pyfits.open(io.BytesIO(raw)) as hdul:
        data = _gray(hdul[0].data)
        header = header_to_dict(hdul[0].header)
    return data, header


# --- snapshot + controller ---------------------------------------------------------------


@dataclass(frozen=True)
class CrossmatchSnapshot:
    """An immutable snapshot of everything the overlay view renders, handed
    to the listeners on any change.

    - ``project_name`` — the current project's display name (None when no
      project is set);
    - ``stack_frame_id`` — the frame id of the cross-matched stack (None when
      no stack is open);
    - ``stack_label`` — the stack's one-line label (None when none);
    - ``rows`` — the current cross-match rows (empty until
      :meth:`CrossmatchController.run_crossmatch` ran or a persisted table
      was loaded);
    - ``radius_arcsec`` — the current match radius;
    - ``g_limit`` — the current Gaia G limit;
    - ``image`` — the annotated overlay of the stack (None when no stack is
      open);
    - ``status`` — a short human-readable status (e.g. ``"4/5 matched"``),
      else None;
    - ``error`` — the last error message, else None.
    """

    project_name: str | None
    stack_frame_id: int | None
    stack_label: str | None
    rows: tuple[Any, ...]
    radius_arcsec: float
    g_limit: float
    image: Image.Image | None
    status: str | None
    error: str | None


class CrossmatchController:
    """The app-level cross-match controller: the current project, the open
    stack (followed from the shared viewer controller), its cross-match rows
    + annotated overlay, the match radius / G limit controls, and observer
    notification.

    - ``project`` — the store project (None = follow the attached viewer
      controller's project, see :meth:`attach_viewer`);
    - ``viewer_controller`` — an optional duck-typed viewer controller (the
      shared :class:`~seestar_controller.enhance.viewer_controller.ViewerController`
      — NOT imported here): its project AND its currently open stack + image
      are adopted whenever no explicit project is set, so the cross-match
      overlay follows the stack open in the image viewer;
    - ``invoke_on_main`` — the marshalling callable for listener
      notifications AND for store calls made from a non-db thread (the app
      binds ``GLib.idle_add``; the default calls directly);
    - ``cache`` — the optional
      :class:`~seestar_controller.catalog.cache.QueryCache` for the Gaia
      cone search (default: the gaia cache dir with the default TTL), so
      re-queries on control changes are served from disk when possible.

    Operations: :attr:`snapshot` (never raises), :meth:`set_project`,
    :meth:`attach_viewer` / :meth:`detach_viewer`, :meth:`refresh_stacks`,
    :meth:`select_stack` (standalone stack selection, loads the image +
    any persisted table), :meth:`run_crossmatch` (detect -> Gaia cone
    search, cache-first -> match -> persist -> render),
    :meth:`set_radius_arcsec` / :meth:`set_g_limit` (the controls: validate,
    then re-run the cross-match when a stack is open), and listeners with
    frozen snapshots.
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
        self._listeners: list[CrossmatchListener] = []
        self._db_thread_id: int | None = None
        self._stacks: tuple[Frame, ...] = ()
        self._stack_frame_id: int | None = None
        self._stack_label: str | None = None
        self._image_data: np.ndarray | None = None
        self._header: dict[str, Any] | None = None
        self._sources_frame_id: int | None = None
        self._sources: tuple[Any, ...] = ()
        self._rows: tuple[Any, ...] = ()
        self._radius_arcsec: float = DEFAULT_MATCH_RADIUS_ARCSEC
        self._g_limit: float = DEFAULT_G_LIMIT
        self._image: Image.Image | None = None
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

    def attach_viewer(self, viewer_controller: Any) -> None:
        """Follow ``viewer_controller`` (duck-typed — the shared viewer
        controller): register as its listener and adopt its project (when no
        explicit project is set) and its currently open stack, so the
        cross-match overlay follows the stack open in the image viewer.
        Re-attaching detaches the previous viewer first."""
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
        project is set) and its open stack + image, so the overlay follows
        the image viewer. Emits on every viewer change, so closing the
        stack clears the overlay."""
        viewer = self._viewer_controller
        if viewer is None:
            return
        if self._project is None:
            self.set_project(getattr(viewer, "project", None))
        current = getattr(snapshot, "current", None)
        image = getattr(snapshot, "image", None)
        if current is not None and image is not None:
            frame_id = getattr(current, "frame_id", None)
            if isinstance(frame_id, int) and frame_id != self._stack_frame_id:
                label_fn = getattr(current, "label", None)
                label = label_fn() if callable(label_fn) else None
                self._adopt_stack(frame_id, label, image)
        elif current is None and self._stack_frame_id is not None:
            with self._lock:
                self._stack_frame_id = None
                self._stack_label = None
                self._image_data = None
                self._header = None
                self._sources_frame_id = None
                self._sources = ()
                self._rows = ()
                self._image = None
                self._status = None
                self._error = None
            self._emit(self.snapshot)

    def _adopt_stack(self, frame_id: int, label: str | None, image: Any) -> None:
        """Adopt the viewer's open stack: keep its image data + header, load
        any persisted cross-match table, render, notify."""
        try:
            data = _gray(getattr(image, "data", None))
            header = dict(getattr(image, "header", {}) or {})
            project = self._project
            rows: tuple[Any, ...] = ()
            if project is not None:
                rows = self._call_store(lambda: load_crossmatch(project, frame_id))
            with self._lock:
                self._stack_frame_id = frame_id
                self._stack_label = label
                self._image_data = data
                self._header = header
                self._sources_frame_id = None
                self._sources = ()
                self._rows = tuple(rows)
                self._error = None
            self._render_current()
        except Exception as exc:  # surfaced as snapshot.error, never raised  # noqa: BLE001
            logger.info("adopting the viewer stack failed: %s", exc)
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"
        self._emit(self.snapshot)

    @property
    def invoke_on_main(self) -> InvokeOnMain:
        """The marshalling callable (the app binds ``GLib.idle_add``)."""
        return self._invoke_on_main

    def set_project(self, project: Project | None) -> None:
        """Set/replace the current project (None clears it): the stack list
        is re-listed and the stack state is cleared. Notifies."""
        self._project = project
        self._db_thread_id = threading.get_ident() if project is not None else None
        with self._lock:
            self._stacks = ()
            self._stack_frame_id = None
            self._stack_label = None
            self._image_data = None
            self._header = None
            self._sources_frame_id = None
            self._sources = ()
            self._rows = ()
            self._image = None
            self._status = None
            self._error = None
        if project is not None:
            try:
                self.refresh_stacks()
                return  # refresh_stacks already notified
            except GaiaError as exc:
                with self._lock:
                    self._error = str(exc)
        self._emit(self.snapshot)

    # -- stacks -----------------------------------------------------------

    @property
    def stacks(self) -> tuple[Frame, ...]:
        """The current project's stacks (ordered by ``jd``)."""
        return self._stacks

    def refresh_stacks(self) -> tuple[Frame, ...]:
        """(Re-)list the project's stacks via the store; notifies and
        returns them. Raises :class:`~.errors.GaiaError` when no project is
        set or the store call fails."""
        project = self._require_project()
        try:
            stacks = self._call_store(lambda: tuple(list_frames(project, kind=KIND_STACK)))
        except Exception as exc:
            raise GaiaError(
                f"cannot list the stacks of project {project.name!r}: {exc}"
            ) from exc
        with self._lock:
            self._stacks = stacks
            self._error = None
        self._emit(self.snapshot)
        return self._stacks

    @property
    def stack_frame_id(self) -> int | None:
        """The frame id of the current stack (None when none is open)."""
        return self._stack_frame_id

    @property
    def stack_label(self) -> str | None:
        """The one-line label of the current stack (None when none)."""
        return self._stack_label

    def select_stack(self, frame_id: int) -> CrossmatchSnapshot:
        """Select the stack with frame id ``frame_id`` (standalone use —
        with a viewer attached the viewer's open stack drives the overlay):
        load its image + header from the store, load any persisted
        cross-match table, render, notify, and return the snapshot.

        Raises :class:`~.errors.GaiaError` when no project is set, the id is
        not a stack of the project, or the FITS cannot be read.
        """
        if isinstance(frame_id, bool) or not isinstance(frame_id, int):
            raise GaiaError(f"frame_id must be an int, got {frame_id!r}")
        project = self._require_project()
        frame = next((f for f in self._stacks if f.id == frame_id), None)
        if frame is None:
            frame = next(
                (f for f in self._call_store(lambda: list_frames(project, kind=KIND_STACK)) if f.id == frame_id),
                None,
            )
        if frame is None:
            raise GaiaError(
                f"project {project.name!r} has no stack with frame id {frame_id!r} "
                f"(listed: {[f.id for f in self._stacks] or 'none'})"
            )
        try:
            data, header = self._call_store(lambda: _load_stack_image(project, frame))
            rows = self._call_store(lambda: load_crossmatch(project, frame_id))
        except Exception as exc:
            raise GaiaError(f"cannot load stack {frame_id!r}: {exc}") from exc
        with self._lock:
            self._stack_frame_id = frame_id
            self._stack_label = f"{frame.utc} — {frame.target or 'no target'}"
            self._image_data = data
            self._header = header
            self._sources_frame_id = None
            self._sources = ()
            self._rows = tuple(rows)
            self._error = None
        self._render_current()
        self._emit(self.snapshot)
        return self.snapshot

    # -- controls + the pipeline -----------------------------------------------------------

    @property
    def radius_arcsec(self) -> float:
        """The current match radius in arcseconds."""
        return self._radius_arcsec

    @property
    def g_limit(self) -> float:
        """The current Gaia G magnitude limit."""
        return self._g_limit

    def set_radius_arcsec(self, value: float) -> CrossmatchSnapshot:
        """Set the match radius (validated, raises ``ValueError`` on a bad
        value); when a stack is open, re-runs the cross-match with the new
        radius (the Gaia query is cache-first, so a radius-only change does
        no HTTP at all). Notifies and returns the snapshot."""
        self._radius_arcsec = _validate_radius_arcsec(value)
        if self._stack_frame_id is not None:
            return self.run_crossmatch()
        self._emit(self.snapshot)
        return self.snapshot

    def set_g_limit(self, value: float) -> CrossmatchSnapshot:
        """Set the Gaia G limit (validated, raises ``ValueError`` on a bad
        value); when a stack is open, re-queries Gaia with the new limit
        (served from the disk cache when possible) and re-matches. Notifies
        and returns the snapshot."""
        self._g_limit = _validate_g_limit(value)
        if self._stack_frame_id is not None:
            return self.run_crossmatch()
        self._emit(self.snapshot)
        return self.snapshot

    def run_crossmatch(
        self,
        *,
        radius_arcsec: float | None = None,
        g_limit: float | None = None,
    ) -> CrossmatchSnapshot:
        """Run the cross-match on the current stack: detect the sources
        (cached per stack), query Gaia DR3 around the field (cache-first via
        :func:`~.query.query_gaia_cached`), match within ``radius_arcsec``,
        persist the table (issue-02 glue), render the overlay, notify, and
        return the snapshot.

        ``radius_arcsec`` / ``g_limit`` optionally override the current
        control values first (validated — a bad value raises ``ValueError``
        immediately). Runtime failures (no project/stack, missing WCS,
        Gaia unavailable with no cache, store errors) are recorded in the
        snapshot's ``error`` — never raised.
        """
        if radius_arcsec is not None:
            self._radius_arcsec = _validate_radius_arcsec(radius_arcsec)
        if g_limit is not None:
            self._g_limit = _validate_g_limit(g_limit)
        try:
            project = self._require_project()
            frame_id = self._require_stack()
            data, header = self._require_image()
            with self._lock:
                sources = (
                    self._sources if self._sources_frame_id == frame_id else None
                )
            if sources is None:
                sources = tuple(detect_sources(data, header))
                with self._lock:
                    self._sources = sources
                    self._sources_frame_id = frame_id
            center, radius_deg = field_center_and_radius(header, data.shape)
            stars = query_gaia_cached(
                center.ra_deg,
                center.dec_deg,
                radius_deg,
                self._cache,
                g_limit=self._g_limit,
            )
            rows = crossmatch(sources, stars, radius_arcsec=self._radius_arcsec)
            self._call_store(lambda: save_crossmatch(project, frame_id, rows))
            matched = sum(1 for row in rows if row.matched)
            with self._lock:
                self._rows = tuple(rows)
                self._error = None
                self._status = (
                    f"{matched}/{len(rows)} matched · "
                    f"r={self._radius_arcsec:g}\u2033 · G<{self._g_limit:g}"
                )
            self._render_current()
        except Exception as exc:  # surfaced as snapshot.error, never raised  # noqa: BLE001
            logger.info("cross-match failed: %s", exc)
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"
                self._status = None
        self._emit(self.snapshot)
        return self.snapshot

    # -- snapshot + listeners ---------------------------------------------------------------

    @property
    def rows(self) -> tuple[Any, ...]:
        """The current cross-match rows (empty until a run / persisted load)."""
        return self._rows

    @property
    def image(self) -> Image.Image | None:
        """The annotated overlay image of the current stack (None when none)."""
        return self._image

    @property
    def snapshot(self) -> CrossmatchSnapshot:
        """The frozen :class:`CrossmatchSnapshot` of the current state."""
        with self._lock:
            return CrossmatchSnapshot(
                project_name=self._project.name if self._project is not None else None,
                stack_frame_id=self._stack_frame_id,
                stack_label=self._stack_label,
                rows=self._rows,
                radius_arcsec=self._radius_arcsec,
                g_limit=self._g_limit,
                image=self._image,
                status=self._status,
                error=self._error,
            )

    @property
    def listeners(self) -> tuple[CrossmatchListener, ...]:
        """The registered observer callbacks, in registration order."""
        return tuple(self._listeners)

    def add_listener(self, listener: CrossmatchListener) -> CrossmatchListener:
        """Register an observer callback (idempotent); returns it, so it can
        be used as a decorator. The listener receives a frozen
        :class:`CrossmatchSnapshot` on any change."""
        if listener not in self._listeners:
            self._listeners.append(listener)
        return listener

    def remove_listener(self, listener: CrossmatchListener) -> None:
        """Unregister an observer callback (no-op when not registered)."""
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _notify(self, snapshot: CrossmatchSnapshot) -> None:
        """Hand a frozen snapshot to every listener; a failing listener is
        logged and isolated (never breaks the others or the caller)."""
        for listener in list(self._listeners):
            try:
                listener(snapshot)
            except Exception:
                logger.exception("crossmatch listener failed")

    def _emit(self, snapshot: CrossmatchSnapshot) -> None:
        """Marshal a listener notification through ``invoke_on_main``."""
        self._invoke_on_main(lambda: self._notify(snapshot))

    # -- internals ---------------------------------------------------------------------------

    def _require_project(self) -> Project:
        if self._project is None:
            raise GaiaError("no project set — call set_project() first")
        return self._project

    def _require_stack(self) -> int:
        if self._stack_frame_id is None:
            raise GaiaError("no stack open — open a stack in the image viewer first")
        return self._stack_frame_id

    def _require_image(self) -> tuple[np.ndarray, dict[str, Any]]:
        if self._image_data is None or self._header is None:
            raise GaiaError("the current stack has no image loaded")
        return self._image_data, self._header

    def _call_store(self, fn: Callable[[], Any]) -> Any:
        """Run the store callable ``fn`` and return its result.

        When already on the db-owning thread (or when no db thread was
        captured), ``fn`` runs directly. Otherwise it is marshalled through
        ``invoke_on_main`` (the app: ``GLib.idle_add``) and the calling thread
        waits for the result — the same pattern as the mover controller.
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
            raise GaiaError(
                "timed out waiting for the project db thread — is the main "
                "loop running?"
            )
        if "error" in box:
            raise box["error"]
        return box["result"]

    def _render_current(self) -> None:
        """Re-render the annotated overlay of the current stack (the plain
        tone-mapped stack when no rows yet; None when no stack is open). A
        render failure is recorded in the snapshot's ``error`` (never
        raised)."""
        with self._lock:
            data, rows = self._image_data, self._rows
        if data is None:
            with self._lock:
                self._image = None
            return
        try:
            self._image = render_stack_overlay(data, rows)
        except Exception as exc:  # surfaced as snapshot.error, never raised  # noqa: BLE001
            logger.info("overlay render failed: %s", exc)
            with self._lock:
                self._image = None
                self._error = f"{type(exc).__name__}: {exc}"


__all__ = [
    "DB_CALL_TIMEOUT_S",
    "DEFAULT_HI_PERCENTILE",
    "DEFAULT_LO_PERCENTILE",
    "DEFAULT_MAX_OVERLAY_SIZE",
    "LINE_WIDTH",
    "MATCHED_COLOR",
    "MAX_G_LIMIT",
    "MAX_MATCH_RADIUS_ARCSEC",
    "MIN_G_LIMIT",
    "MIN_MATCH_RADIUS_ARCSEC",
    "POINT_RADIUS",
    "RING_RADIUS",
    "UNMATCHED_COLOR",
    "CrossmatchController",
    "CrossmatchListener",
    "CrossmatchSnapshot",
    "InvokeOnMain",
    "render_stack_overlay",
    "tonemap_gray",
]
