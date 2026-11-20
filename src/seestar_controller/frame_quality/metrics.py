"""Frame-quality metrics core — the PURE scoring + flagging engine of
:mod:`seestar_controller.frame_quality` (frame-quality ticket 01).

This is a PURE module: **no ``gi`` import** (the sandbox has no GTK, see
docs/wiki/assumptions.md; a subprocess test enforces the boundary) — stdlib +
:mod:`numpy` + :mod:`seestar_controller.store` only. The heavy third-party
imports are lazy inside the functions that need them, the same pattern as
:mod:`seestar_controller.photometry.measure`: :mod:`astropy.io.fits` (FITS
reading), :mod:`astropy.stats` (sigma-clipped background stats) and
:mod:`photutils.detection` (``DAOStarFinder`` star detection) are imported
inside :func:`score_frame`.

What lives here:

- :class:`FrameMetrics` — the per-frame metrics record: ``fwhm`` (pixels,
  None when no star was detected), ``bg_median`` / ``bg_std`` (sigma-clipped
  background), ``star_count``;
- :func:`score_frame` — score ONE frame from a FITS path, FITS bytes, or a
  raw array: background via :func:`astropy.stats.sigma_clipped_stats`, star
  detection via ``photutils.detection.DAOStarFinder``, and the FWHM estimated
  from the detected stars (moment estimator on a fixed cutout — see
  :func:`score_frame`);
- the pure flagging math, unit-tested on their own: :func:`robust_zscores`
  (the MAD z-score ``0.6745 * (x - median) / MAD``), :func:`flag_outliers`
  (the indices whose |z| exceeds the threshold on ANY metric) and
  :func:`flag_reasons` (index -> the metric names that flagged it).

Conventions (documented choices):

- **Sources.** :func:`score_frame` accepts a FITS file path (``str`` /
  :class:`pathlib.Path`), raw FITS ``bytes``, or an array-like image. RGB
  frames are reduced to their channel mean first (same as
  :mod:`seestar_controller.photometry.measure`); anything that is not 2-D
  after that raises :class:`QualityError`. Non-finite pixels are masked out
  of the background stats and the star detection.
- **Background.** :func:`astropy.stats.sigma_clipped_stats` with
  ``sigma=3`` over the finite pixels gives ``bg_median`` (the median) and
  ``bg_std`` (the std). A frame with no finite pixel raises
  :class:`QualityError`.
- **Star detection.** ``DAOStarFinder(fwhm=fwhm_guess,
  threshold=threshold_sigma * bg_std, exclude_border=True)`` — the threshold
  is the fitted-Gaussian height on the convolved image, so
  ``threshold_sigma * bg_std`` is a ~N-sigma detection limit. When
  ``bg_std == 0`` (a perfectly flat frame) no star can be distinguished from
  the noise and the frame scores ``star_count=0``, ``fwhm=None``.
- **FWHM estimate.** For each detected star (capped at the
  :data:`MAX_FWHM_STARS` brightest) a circular cutout of
  :data:`FWHM_CUTOUT_RADIUS_PX` around the centroid is background-subtracted
  (clipped at 0) and its radial second moment ``<r^2>`` gives
  ``sigma = sqrt(<r^2> / 2)`` and ``fwhm = 2*sqrt(2*ln2)*sigma``; the frame
  FWHM is the MEDIAN over the stars (robust to a bad cutout). Stars whose
  cutout would clip the image border are skipped. This is a documented
  estimator, not a PSF fit — it is consistent across the frames of a session,
  which is what the relative flagging uses. ``fwhm`` is None when no star
  yields an estimate.
- **Flagging.** :func:`flag_outliers` / :func:`flag_reasons` compute the
  robust (MAD) z-score of every metric (:data:`METRIC_FIELDS`) over the given
  sequence — per session in practice — and flag the frames whose |z| exceeds
  ``mad_threshold`` (default :data:`DEFAULT_MAD_THRESHOLD` = 3.0) on ANY
  metric, two-sided (a blurry frame has a HIGH FWHM, a cloudy frame a HIGH
  background, a lost-tracking frame a LOW star count — all are outliers).
  Frames with ``fwhm=None`` simply do not participate in the FWHM z-scores.
  When the MAD is 0 (all values equal) every deviation is infinitely
  significant: z = 0 for the median value, +/-inf for anything else (see
  :func:`robust_zscores`).
"""

from __future__ import annotations

import io
import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

#: The MAD -> sigma scale for Gaussian data: ``Phi^-1(3/4) ≈ 0.6745``, so
#: ``MAD_SCALE * (x - median) / MAD`` is the robust z-score.
MAD_SCALE = 0.6744897501960817

#: The default flagging threshold: |robust z| > 3.0 flags a frame.
DEFAULT_MAD_THRESHOLD = 3.0

#: The default DAOStarFinder kernel FWHM (pixels) — the expected sharp-star
#: FWHM of a Seestar sub-frame.
DEFAULT_FWHM_PX = 3.5

#: The default detection threshold in units of the background std.
DEFAULT_THRESHOLD_SIGMA = 5.0

#: The sigma-clipping of the background stats.
SIGMA_CLIP = 3.0

#: The radius (pixels) of the FWHM-estimator cutout around each detected star.
FWHM_CUTOUT_RADIUS_PX = 9.0

#: Cap on the number of stars used for the FWHM estimate (the brightest).
MAX_FWHM_STARS = 64

#: The metric fields of :class:`FrameMetrics` that participate in flagging,
#: in order (also the names used in ``flag_reasons``).
METRIC_FIELDS = ("fwhm", "bg_median", "bg_std", "star_count")

#: ``2 * sqrt(2 * ln 2)`` — sigma to FWHM for a Gaussian.
_FWHM_FROM_SIGMA = 2.0 * math.sqrt(2.0 * math.log(2.0))


class QualityError(ValueError):
    """A frame could not be scored: the source is not a readable FITS file /
    not a 2-D image, has no finite pixel, or a flagging input is invalid
    (non-finite value, bad threshold). Deliberately a :class:`ValueError` and
    deliberately distinct from the store's
    :class:`seestar_controller.store.errors.QualityError` (the persistence
    layer's own error)."""


@dataclass(frozen=True)
class FrameMetrics:
    """The quality metrics of ONE frame.

    - ``fwhm`` — the estimated FWHM in pixels (median over the detected
      stars), or None when no star yielded an estimate;
    - ``bg_median`` — the sigma-clipped background median;
    - ``bg_std`` — the sigma-clipped background standard deviation;
    - ``star_count`` — the number of detected stars (DAOStarFinder).
    """

    fwhm: float | None
    bg_median: float
    bg_std: float
    star_count: int


# --- pure flagging math ------------------------------------------------------------------


def robust_zscores(values: Sequence[float]) -> list[float]:
    """The robust (MAD) z-scores of ``values``:
    ``MAD_SCALE * (x - median) / MAD`` with ``MAD = median(|x - median|)``.

    The :data:`MAD_SCALE` factor (``Phi^-1(3/4)``) makes the MAD a consistent
    estimator of the Gaussian sigma, so the result is comparable to a plain
    z-score. When the MAD is 0 (all values equal, or a majority at the median)
    every deviation is infinitely significant: the z-score is 0.0 for values
    equal to the median and ``+/-inf`` (sign of the deviation) for anything
    else — a constant session flags any deviating frame.

    Raises :class:`QualityError` for a non-finite value (NaN/inf would poison
    the median). An empty sequence yields ``[]``.
    """
    vals = [float(v) for v in values]
    for v in vals:
        if not math.isfinite(v):
            raise QualityError(f"robust_zscores needs finite values, got {v!r}")
    if not vals:
        return []
    median = statistics.median(vals)
    mad = statistics.median([abs(v - median) for v in vals])
    if mad == 0.0:
        return [0.0 if v == median else math.copysign(math.inf, v - median) for v in vals]
    return [MAD_SCALE * (v - median) / mad for v in vals]


def _metric_values(metrics: Sequence[FrameMetrics], field: str) -> list[tuple[int, float]]:
    """The ``(index, value)`` pairs of ``field`` over ``metrics``, skipping
    None values (a frame without a FWHM does not participate)."""
    pairs: list[tuple[int, float]] = []
    for i, m in enumerate(metrics):
        value = getattr(m, field)
        if value is None:
            continue
        pairs.append((i, float(value)))
    return pairs


def flag_reasons(
    metrics: Sequence[FrameMetrics], mad_threshold: float = DEFAULT_MAD_THRESHOLD
) -> dict[int, tuple[str, ...]]:
    """The flagging reasons per frame: ``{index: (metric, ...)}`` for every
    frame whose |robust z| exceeds ``mad_threshold`` on at least one metric.

    The z-scores are computed per metric over the whole sequence (per session
    in practice) — see :func:`robust_zscores`. ``mad_threshold`` must be a
    finite number ``> 0``. Frames with ``fwhm=None`` are flagged on the other
    metrics only.
    """
    threshold = _check_threshold(mad_threshold)
    reasons: dict[int, tuple[str, ...]] = {}
    for field in METRIC_FIELDS:
        pairs = _metric_values(metrics, field)
        if not pairs:
            continue
        zscores = robust_zscores([v for _, v in pairs])
        for (index, _), z in zip(pairs, zscores, strict=True):
            if abs(z) > threshold:
                reasons[index] = reasons.get(index, ()) + (field,)
    return reasons


def flag_outliers(
    metrics: Sequence[FrameMetrics], mad_threshold: float = DEFAULT_MAD_THRESHOLD
) -> set[int]:
    """The indices of the OUTLIER frames in ``metrics``: every frame whose
    |robust z| exceeds ``mad_threshold`` (default
    :data:`DEFAULT_MAD_THRESHOLD` = 3.0) on ANY metric — two-sided, so a
    blurry frame (high FWHM), a cloudy frame (high background) and a
    lost-tracking frame (low star count) are all flagged.

    The z-scores are computed per metric over the whole sequence (per session
    in practice). Returns a set of indices into ``metrics`` (empty when
    nothing flags). See :func:`flag_reasons` for the per-metric reasons.
    """
    return set(flag_reasons(metrics, mad_threshold))


def _check_threshold(mad_threshold: Any) -> float:
    """Validate the flagging threshold: a finite number ``> 0``."""
    if isinstance(mad_threshold, bool):
        raise QualityError(f"mad_threshold must be a number, got {mad_threshold!r}")
    try:
        threshold = float(mad_threshold)
    except (TypeError, ValueError):
        raise QualityError(f"mad_threshold must be a number, got {mad_threshold!r}") from None
    if not math.isfinite(threshold) or threshold <= 0.0:
        raise QualityError(f"mad_threshold must be finite and > 0, got {mad_threshold!r}")
    return threshold


# --- frame scoring -------------------------------------------------------------------------


def _as_2d_plane(data: Any) -> np.ndarray:
    """Coerce ``data`` to a 2-D float64 plane (RGB images are reduced to
    their channel mean); raise :class:`QualityError` when it is not
    image-like."""
    arr = np.asarray(data, dtype=np.float64)
    if arr.ndim == 3:
        arr = arr[..., :3].mean(axis=2)
    if arr.ndim != 2 or arr.size == 0:
        raise QualityError(
            f"frame data must be a 2-D image array, got shape {arr.shape!r}"
        )
    return arr


def _read_fits_plane(source: bytes) -> np.ndarray:
    """Parse FITS ``source`` bytes to a 2-D float64 plane (lazy astropy
    import); raise :class:`QualityError` when not a readable FITS with data."""
    from astropy.io import fits as pyfits

    try:
        with pyfits.open(io.BytesIO(source), memmap=False) as hdul:
            data = hdul[0].data
    except Exception as exc:
        raise QualityError(f"not a readable FITS file: {exc}") from exc
    if data is None:
        raise QualityError("the FITS file has no image data in its primary HDU")
    return _as_2d_plane(data)


def _load_plane(source: Any) -> np.ndarray:
    """Resolve a :func:`score_frame` source to a 2-D float64 plane: a path
    (``str`` / :class:`~pathlib.Path`) is read and parsed as FITS, ``bytes``
    are parsed as FITS, anything else is taken as a raw array."""
    if isinstance(source, (str, Path)):
        path = Path(source)
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise QualityError(f"cannot read frame file {path}: {exc}") from exc
        return _read_fits_plane(raw)
    if isinstance(source, (bytes, bytearray, memoryview)):
        return _read_fits_plane(bytes(source))
    return _as_2d_plane(source)


def _background_stats(plane: np.ndarray, sigma_clip: float) -> tuple[float, float]:
    """The sigma-clipped ``(median, std)`` of the finite pixels of ``plane``
    (lazy astropy import). Raises :class:`QualityError` when no pixel is
    finite."""
    from astropy.stats import sigma_clipped_stats

    finite = np.isfinite(plane)
    if not bool(finite.any()):
        raise QualityError("the frame has no finite pixel — cannot score it")
    mask = None if bool(finite.all()) else ~finite
    _mean, median, std = sigma_clipped_stats(plane, sigma=sigma_clip, mask=mask)
    return float(median), float(std)


def _estimate_star_fwhm(
    plane: np.ndarray, bg_median: float, x: float, y: float, radius: float
) -> float | None:
    """The moment-based FWHM estimate of ONE star at ``(x, y)``: a circular
    cutout of ``radius`` around the centroid, background-subtracted (clipped
    at 0), gives ``<r^2>``; ``sigma = sqrt(<r^2>/2)`` and
    ``fwhm = 2*sqrt(2*ln2)*sigma``. Returns None when the cutout clips the
    image border or carries no positive flux."""
    height, width = plane.shape[0], plane.shape[1]
    x0, x1 = math.floor(x - radius), math.ceil(x + radius) + 1
    y0, y1 = math.floor(y - radius), math.ceil(y + radius) + 1
    if x0 < 0 or y0 < 0 or x1 > width or y1 > height:
        return None  # cutout would clip the border — skip this star
    cut = plane[y0:y1, x0:x1]
    yy, xx = np.mgrid[y0:y1, x0:x1]
    r2 = (xx - x) ** 2 + (yy - y) ** 2
    weights = np.clip(cut - bg_median, 0.0, None) * (r2 <= radius * radius)
    total = float(weights.sum())
    if not math.isfinite(total) or total <= 0.0:
        return None
    second_moment = float((weights * r2).sum()) / total
    return _FWHM_FROM_SIGMA * math.sqrt(second_moment / 2.0)


def score_frame(
    source: Any,
    *,
    fwhm: float = DEFAULT_FWHM_PX,
    threshold_sigma: float = DEFAULT_THRESHOLD_SIGMA,
    sigma_clip: float = SIGMA_CLIP,
    cutout_radius: float = FWHM_CUTOUT_RADIUS_PX,
) -> FrameMetrics:
    """Score ONE frame and return its :class:`FrameMetrics`.

    - ``source`` — a FITS file path (``str`` / :class:`~pathlib.Path`), raw
      FITS ``bytes``, or an array-like image (RGB reduced to its channel
      mean; non-finite pixels masked);
    - ``fwhm`` — the DAOStarFinder kernel FWHM in pixels (the expected
      sharp-star FWHM, default :data:`DEFAULT_FWHM_PX`);
    - ``threshold_sigma`` — the detection threshold in units of the
      background std (default :data:`DEFAULT_THRESHOLD_SIGMA`);
    - ``sigma_clip`` — the sigma-clipping of the background stats (default
      :data:`SIGMA_CLIP`);
    - ``cutout_radius`` — the FWHM-estimator cutout radius in pixels
      (default :data:`FWHM_CUTOUT_RADIUS_PX`).

    The background is the sigma-clipped median/std of the finite pixels; the
    stars are detected with ``photutils.detection.DAOStarFinder`` on the
    background-std-scaled threshold (border sources excluded); the frame FWHM
    is the median of the per-star moment estimates over the brightest
    :data:`MAX_FWHM_STARS` detections (None when no star yields an estimate).
    A perfectly flat frame (``bg_std == 0``) scores ``star_count=0`` and
    ``fwhm=None`` — no star can be told apart from the noise.

    Raises :class:`QualityError` when the source cannot be read as a 2-D
    image, has no finite pixel, or a parameter is invalid.
    """
    fwhm_guess = _positive_finite(fwhm, "fwhm")
    thr_sigma = _positive_finite(threshold_sigma, "threshold_sigma")
    sig_clip = _positive_finite(sigma_clip, "sigma_clip")
    radius = _positive_finite(cutout_radius, "cutout_radius")
    plane = _load_plane(source)
    bg_median, bg_std = _background_stats(plane, sig_clip)

    star_count = 0
    fwhm_value: float | None = None
    if bg_std > 0.0:
        from photutils.detection import DAOStarFinder

        finite = np.isfinite(plane)
        mask = None if bool(finite.all()) else ~finite
        finder = DAOStarFinder(
            threshold=thr_sigma * bg_std, fwhm=fwhm_guess, exclude_border=True
        )
        sources = finder(plane, mask=mask)
        if sources is not None and len(sources) > 0:
            star_count = len(sources)
            # the brightest MAX_FWHM_STARS detections feed the FWHM estimate
            order = np.argsort(np.asarray(sources["flux"], dtype=np.float64))[::-1]
            estimates: list[float] = []
            for row_index in order[:MAX_FWHM_STARS]:
                est = _estimate_star_fwhm(
                    plane,
                    bg_median,
                    float(sources["x_centroid"][row_index]),
                    float(sources["y_centroid"][row_index]),
                    radius,
                )
                if est is not None and math.isfinite(est) and est > 0.0:
                    estimates.append(est)
            if estimates:
                fwhm_value = float(statistics.median(estimates))
    return FrameMetrics(
        fwhm=fwhm_value, bg_median=bg_median, bg_std=bg_std, star_count=star_count
    )


def _positive_finite(value: Any, what: str) -> float:
    """Validate a scoring parameter: a finite number ``> 0``."""
    if isinstance(value, bool):
        raise QualityError(f"{what} must be a number, got {value!r}")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise QualityError(f"{what} must be a number, got {value!r}") from None
    if not math.isfinite(result) or result <= 0.0:
        raise QualityError(f"{what} must be finite and > 0, got {value!r}")
    return result
