"""Per-sub source detection for moving-object tracking.

Pure-Python (module boundary per ``.scratch/moving-objects/spec.md``): no
GTK imports, no :mod:`seestar_controller.link` imports — a subprocess test
enforces the boundary. Third-party imports (:mod:`numpy`, :mod:`astropy`,
:mod:`photutils`) are lazy inside the functions that need them, the same
pattern as :mod:`seestar_controller.gaia.detect`.

What lives here:

- :class:`Detection` — one source detected in one sub-frame: the 0-based
  pixel ``(x, y)``, the DAOStarFinder ``peak`` / ``flux``, the frame
  identity (``frame_id`` / ``path`` / ``jd`` / ``utc``) and — when the sub's
  FITS header carries a celestial WCS — the J2000 sky position
  ``(ra_deg, dec_deg)`` (``None`` otherwise: subs often lack a WCS, the
  pixel-space fallback of the spec);
- :class:`FrameDetections` — all detections of one sub-frame plus the
  :class:`~seestar_controller.store.frames.Frame` record and whether a WCS
  was available;
- :func:`detect_in_subs` — run :class:`photutils.detection.DAOStarFinder`
  over every sub-frame of a session (low threshold,
  :data:`DEFAULT_THRESHOLD_SIGMA`), reading the FITS files through the store
  (:func:`~seestar_controller.store.frames.list_frames` /
  :func:`~seestar_controller.store.frames.read_frame_bytes`).

Documented choices:

- **Low threshold.** Movers are faint streaks, so the default threshold is
  :data:`DEFAULT_THRESHOLD_SIGMA` = 3 background sigmas (lower than the
  stack-detection default of 5 in :mod:`seestar_controller.gaia.detect`);
  configurable per call.
- **WCS optional.** Subs of a session share the same pointing, so linking
  happens in PIXEL space; the WCS is only used to attach sky positions (and
  later arcsec/frame speeds) when present. A header without a celestial
  WCS (blank ``CTYPE`` — ``WCS({})`` alone does not raise) simply yields
  ``ra_deg`` / ``dec_deg`` = ``None`` and ``has_wcs=False`` — never an
  error. A WCS that exists but fails for one pixel degrades that one
  detection to ``None`` sky coords instead of failing the frame.
- **Non-finite pixels** (NaN/inf) are replaced by the sigma-clipped median
  before detection, so DAOStarFinder never sees a NaN (same as gaia).
- **Unknown session** raises
  :class:`~seestar_controller.store.errors.SessionNotFoundError` (checked
  against the db before reading anything); a known session with no subs
  returns ``[]`` — a normal outcome.
"""

from __future__ import annotations

import io
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from seestar_controller.catalog.coords import J2000Coord
from seestar_controller.store.errors import SessionNotFoundError
from seestar_controller.store.frames import (
    KIND_SUB,
    Frame,
    header_to_dict,
    list_frames,
    read_frame_bytes,
)
from seestar_controller.store.project import Project

from .errors import DetectionError

#: Default detection threshold in units of the sigma-clipped background RMS
#: — deliberately LOW (movers are faint): a source peak must exceed
#: ``median + threshold * std``.
DEFAULT_THRESHOLD_SIGMA = 3.0

#: Default DAOStarFinder kernel FWHM in pixels (the expected sharp-star
#: size on a sub-frame).
DEFAULT_FWHM_PX = 4.0


@dataclass(frozen=True)
class Detection:
    """One source detected in one sub-frame.

    - ``frame_id`` / ``path`` / ``jd`` / ``utc`` — the frame this detection
      belongs to (the store :class:`~seestar_controller.store.frames.Frame`
      row id, its project-relative FITS path, and the observation time);
    - ``x`` / ``y`` — the 0-based pixel position (``x`` = column, ``y`` =
      row, the same convention as :mod:`seestar_controller.photometry.picking`);
    - ``peak`` / ``flux`` — the DAOStarFinder peak data value / kernel
      flux;
    - ``ra_deg`` / ``dec_deg`` — the J2000 sky position from the sub's WCS,
      or ``None`` when the sub has no celestial WCS (pixel-space fallback).
    """

    frame_id: int
    path: str
    jd: float
    utc: str
    x: float
    y: float
    peak: float
    flux: float
    ra_deg: float | None
    dec_deg: float | None

    @property
    def xy(self) -> tuple[float, float]:
        """``(x, y)`` — the 0-based pixel position."""
        return self.x, self.y

    @property
    def coord(self) -> J2000Coord | None:
        """The J2000 sky position, or ``None`` when the sub has no WCS."""
        if self.ra_deg is None or self.dec_deg is None:
            return None
        return J2000Coord(ra_deg=self.ra_deg, dec_deg=self.dec_deg)


@dataclass(frozen=True)
class FrameDetections:
    """All detections of one sub-frame.

    - ``frame`` — the store :class:`~seestar_controller.store.frames.Frame`;
    - ``has_wcs`` — whether the sub's header carried a celestial WCS;
    - ``detections`` — the :class:`Detection` records, brightest first.
    """

    frame: Frame
    has_wcs: bool
    detections: tuple[Detection, ...]

    def __iter__(self):
        """Iterate the detections (so a list of FrameDetections can be
        flattened into link_tracks directly)."""
        return iter(self.detections)

    def __len__(self) -> int:
        return len(self.detections)


# --- input validation -----------------------------------------------------------


def _validate_threshold(threshold: Any) -> float:
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not math.isfinite(threshold)
        or threshold <= 0
    ):
        raise DetectionError(
            f"threshold must be a positive number of background sigmas, got {threshold!r}"
        )
    return float(threshold)


def _validate_fwhm(fwhm: Any) -> float:
    if (
        isinstance(fwhm, bool)
        or not isinstance(fwhm, (int, float))
        or not math.isfinite(fwhm)
        or not 0.5 <= fwhm <= 50.0
    ):
        raise DetectionError(
            f"fwhm must be a number of pixels in [0.5, 50], got {fwhm!r}"
        )
    return float(fwhm)


def _validate_max_frames(max_frames: Any) -> int | None:
    if max_frames is None:
        return None
    if isinstance(max_frames, bool) or not isinstance(max_frames, int) or max_frames <= 0:
        raise DetectionError(
            f"max_frames must be a positive int or None, got {max_frames!r}"
        )
    return max_frames


def _check_session(project: Project, session_id: str) -> None:
    """Raise :class:`SessionNotFoundError` when ``session_id`` is not a
    session of ``project`` (checked before anything is read)."""
    if not isinstance(session_id, str) or not session_id:
        raise SessionNotFoundError(f"session_id must be a non-empty string, got {session_id!r}")
    row = project.db.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        raise SessionNotFoundError(f"project {project.slug!r} has no session {session_id!r}")


# --- WCS (optional) ---------------------------------------------------------------


def _maybe_wcs(header: Any) -> Any | None:
    """Build an :class:`astropy.wcs.WCS` from a FITS ``header`` mapping, or
    return ``None`` when the header carries no celestial WCS (blank
    ``CTYPE`` — ``WCS({})`` alone does not raise, it builds an identity
    WCS). Never raises: subs without a WCS are the normal case here."""
    if not isinstance(header, Mapping):
        return None
    from astropy.wcs import WCS

    try:
        wcs = WCS(header_to_dict(header))
    except Exception:  # noqa: BLE001 - any WCS build failure means "no WCS"
        return None
    if wcs.naxis < 2 or not any(str(ctype).strip() for ctype in wcs.wcs.ctype):
        return None
    return wcs


def _pixel_to_sky(wcs: Any, x: float, y: float) -> tuple[float, float] | None:
    """0-based pixel -> J2000 ``(ra_deg, dec_deg)`` via ``wcs`` (the FITS WCS
    is 1-based internally), or ``None`` when the conversion fails / yields
    non-finite values (that one detection degrades to pixel-space only)."""
    try:
        ra, dec = wcs.all_pix2world([x + 1.0], [y + 1.0], 1)  # 1-based origin
    except Exception:  # noqa: BLE001
        return None
    ra_deg = float(ra[0]) % 360.0
    dec_deg = float(dec[0])
    if not (math.isfinite(ra_deg) and math.isfinite(dec_deg)):
        return None
    return ra_deg, dec_deg


# --- the detection ------------------------------------------------------------------


def _background_stats(data: Any) -> tuple[float, float]:
    """Sigma-clipped ``(median, std)`` of the finite pixels of ``data``
    (lazy astropy). Raises :class:`DetectionError` when there is no finite
    pixel."""
    import numpy as np
    from astropy.stats import sigma_clipped_stats

    finite = data[np.isfinite(data)]
    if finite.size == 0:
        raise DetectionError("sub-frame has no finite pixel — cannot detect sources")
    _, median, std = sigma_clipped_stats(finite, sigma=3.0)
    median = float(median)
    std = float(std)
    if not math.isfinite(std) or std <= 0.0:
        std = max(std, 1e-12)  # a perfectly flat image: any threshold works
    return median, std


def _detect_in_fits_bytes(
    fits_bytes: bytes,
    frame: Frame,
    *,
    threshold: float,
    fwhm: float,
) -> FrameDetections:
    """Detect the sources in one sub-frame's FITS ``bytes`` (lazy
    astropy/photutils/numpy) and wrap them with the frame record."""
    import numpy as np
    from astropy.io import fits as pyfits
    from photutils.detection import DAOStarFinder

    try:
        with pyfits.open(io.BytesIO(fits_bytes), memmap=False) as hdul:
            data = hdul[0].data
            header = hdul[0].header
    except Exception as exc:
        raise DetectionError(f"frame {frame.id} ({frame.path}) is not a readable FITS: {exc}") from exc
    if data is None:
        raise DetectionError(f"frame {frame.id} ({frame.path}) has no image data")
    try:
        image = np.asarray(data, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise DetectionError(f"frame {frame.id} ({frame.path}) image is not numeric: {exc}") from exc
    if image.ndim != 2 or image.size == 0:
        raise DetectionError(
            f"frame {frame.id} ({frame.path}) must be a non-empty 2-D image, got {image.shape}"
        )

    wcs = _maybe_wcs(header)
    median, std = _background_stats(image)
    clean = np.where(np.isfinite(image), image, median)
    finder = DAOStarFinder(threshold=median + threshold * std, fwhm=fwhm)
    table = finder(clean)
    detections: list[Detection] = []
    if table is not None and len(table) > 0:
        for row in table:
            x = float(row["x_centroid"])
            y = float(row["y_centroid"])
            sky = _pixel_to_sky(wcs, x, y) if wcs is not None else None
            detections.append(
                Detection(
                    frame_id=frame.id,
                    path=frame.path,
                    jd=frame.jd,
                    utc=frame.utc,
                    x=x,
                    y=y,
                    peak=float(row["peak"]),
                    flux=float(row["flux"]),
                    ra_deg=sky[0] if sky is not None else None,
                    dec_deg=sky[1] if sky is not None else None,
                )
            )
    detections.sort(key=lambda d: d.flux, reverse=True)
    return FrameDetections(frame=frame, has_wcs=wcs is not None, detections=tuple(detections))


def detect_in_subs(
    project: Project,
    session_id: str,
    *,
    threshold: float = DEFAULT_THRESHOLD_SIGMA,
    fwhm: float = DEFAULT_FWHM_PX,
    max_frames: int | None = None,
) -> list[FrameDetections]:
    """Detect the sources in every sub-frame of ``session_id`` in ``project``.

    - ``project`` — the open :class:`~seestar_controller.store.project.Project`;
    - ``session_id`` — the session whose ``subs/<session_id>/`` frames are
      scanned; an unknown id raises
      :class:`~seestar_controller.store.errors.SessionNotFoundError`, a
      known session with no subs returns ``[]``;
    - ``threshold`` — the detection threshold in units of the sigma-clipped
      background RMS (default :data:`DEFAULT_THRESHOLD_SIGMA` = 3, low on
      purpose — movers are faint);
    - ``fwhm`` — the DAOStarFinder kernel FWHM in pixels (default
      :data:`DEFAULT_FWHM_PX`);
    - ``max_frames`` — optionally only the FIRST N subs by observation time.

    Returns one :class:`FrameDetections` per sub, ordered by ``jd`` (the
    store's frame order). Each sub's FITS header WCS — when present —
    attaches J2000 sky positions to its detections; without a WCS the
    detections stay in pixel space (``ra_deg`` / ``dec_deg`` = ``None``,
    ``has_wcs=False``) and linking/flagging fall back to pixels.
    """
    _check_session(project, session_id)
    checked_threshold = _validate_threshold(threshold)
    checked_fwhm = _validate_fwhm(fwhm)
    checked_max = _validate_max_frames(max_frames)
    frames = list_frames(project, session_id, kind=KIND_SUB)  # ordered by jd
    if checked_max is not None:
        frames = frames[:checked_max]
    result: list[FrameDetections] = []
    for frame in frames:
        fits_bytes = read_frame_bytes(project, frame)
        result.append(
            _detect_in_fits_bytes(
                fits_bytes, frame, threshold=checked_threshold, fwhm=checked_fwhm
            )
        )
    return result


__all__ = [
    "DEFAULT_FWHM_PX",
    "DEFAULT_THRESHOLD_SIGMA",
    "Detection",
    "FrameDetections",
    "detect_in_subs",
]
