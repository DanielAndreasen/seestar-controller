"""Aperture photometry core — the PURE measurement engine of
:mod:`seestar_controller.photometry` (photometry ticket 03).

This is a PURE module: **no ``gi`` import** (the sandbox has no GTK, see
docs/wiki/assumptions.md; a subprocess test enforces the boundary) — stdlib +
:mod:`numpy` + :mod:`seestar_controller.store` + :mod:`.picking` only. The
heavy third-party imports are lazy inside the functions that need them, the
same pattern as :mod:`.picking`: :mod:`astropy.io.fits` (frame reading) and
:mod:`photutils.aperture` (the photometry itself — photutils pulls scipy, both
are expected dependencies per the spec) are imported inside
:func:`measure_frame` / :func:`measure_star`; :mod:`astropy.time` is reached
through :func:`seestar_controller.store.frames._date_obs_jd_utc`.

What lives here:

- :class:`MeasurementError` — the photometry-layer measurement error, a
  :class:`~.picking.PhotometryError` (a :class:`ValueError`). It is
  deliberately NOT :class:`seestar_controller.store.errors.MeasurementError`
  (the store's own error) — store errors are wrapped into this class;
  :class:`FrameSkippedError` subclasses it for the per-frame skip conditions
  (pick outside the image, non-positive/non-finite flux);
- the pure math, unit-tested on their own: :func:`flux_to_mag`
  (``-2.5*log10(flux)``), :func:`flux_err_to_mag_err`
  (``1.0857 * flux_err / flux`` — the standard
  ``2.5/ln(10) * flux_err/flux`` propagation, :data:`MAG_ERR_COEFF`),
  :func:`ensemble_mag` (mean comp instrumental mag),
  :func:`ensemble_mag_err` (``sqrt(mean(comp_err^2))``),
  :func:`differential_mag` (target − ensemble) and
  :func:`differential_mag_err` (``sqrt(target_err^2 + mean(comp_err^2))``);
- :func:`measure_star` — one star: circular aperture + sky annulus via
  ``photutils.aperture.aperture_photometry`` (``method="exact"``), background
  = annulus mean × aperture area subtracted, ``flux_err`` propagated from the
  photutils ``aperture_sum_err`` of both the aperture and the annulus;
- :func:`measure_frame_data` — the pure core on a raw array + header: every
  pick measured, the comp ensemble mean per frame (COMPS ONLY — a ``"check"``
  pick is EXCLUDED from the ensemble, check-star-plot ticket 01), the
  differential mag and its error, the check star's differential mag + error
  against the same ensemble (same error propagation as the target) and the
  target-minus-check curve, and the JD from the FITS ``DATE-OBS`` header
  (astropy ``Time.jd``; an explicit ``jd`` argument is the fallback when the
  header has no usable ``DATE-OBS``). Issue 09: with the optional ``wcs``
  argument (the frame's stored WCS — a
  :class:`~seestar_controller.store.wcs.FrameWcs` record, a mapping of FITS
  cards, or an astropy WCS), every pick that carries a sky coordinate
  (``StarPick.ra_deg``/``dec_deg`` or ``StarPickSpec.coord``) is first
  reprojected to THIS frame's pixel coordinates via
  :func:`~.picking.try_sky_to_pixel`; the number of reprojected picks is
  recorded on the :class:`FrameMeasurement` (``wcs_reprojected``);
  issue 12: every WCS-reprojected pick is then CENTROID-REFINED on the
  actual frame data (:func:`refine_pick_on_frame`) before measuring — the
  solve WCS carries a ~few-px pointing error, so the reprojected pixel can
  sit off the star core; the refinement and its offset are recorded
  (``StarMeasurement.pick_offset``, ``FrameMeasurement.wcs_refined``);
- :func:`measure_frame` — one stored frame of a project →
  :class:`~seestar_controller.store.measurements.Measurement` via
  :func:`seestar_controller.store.measurements.add_measurement` (JD from the
  frame's ``DATE-OBS``, falling back to the frame's stored ``jd``; the
  frame's stored WCS — :func:`seestar_controller.store.wcs.load_frame_wcs` —
  drives the reprojection, issue 09);
- :func:`measure_project` — run over ALL **sub-frames** of a project
  (``store.frames`` ``kind="sub"`` — NEVER stacks/enhanced) and return a
  :class:`MeasureReport` (measurements + skipped frames + the per-frame
  :class:`FrameMeasurement` results carrying the check-star curves; a frame
  whose target/comp/check falls outside the image or whose flux is
  non-positive is recorded as skipped, never a crash).

Conventions (documented choices):

- **Picks** may be :class:`~.picking.StarPickSpec` objects (the pick session's
  picks; they use the ``aperture`` argument, default
  :class:`~.picking.ApertureParams`) or
  :class:`~seestar_controller.store.stars.StarPick` records (loaded via
  :func:`seestar_controller.store.stars.load_star_picks`; each carries its own
  per-pick aperture, which then wins over the ``aperture`` argument). The
  ensemble must contain EXACTLY ONE ``"target"`` and at least one ``"comp"``
  — anything else raises :class:`MeasurementError` before any frame is read.
  A ``"check"`` pick is OPTIONAL and at most one; it is measured like the
  target but is NEVER part of the comp ensemble (the ensemble is the mean of
  the comps only).
- **Per-frame WCS reprojection (issue 09).** A pick may carry a sky position
  in addition to its pixel: ``StarPick.ra_deg``/``dec_deg`` or
  ``StarPickSpec.coord``. When a frame WCS is supplied (``wcs=`` — a
  :class:`~seestar_controller.store.wcs.FrameWcs` record as stored by
  :func:`seestar_controller.store.wcs.load_frame_wcs`, a mapping of FITS
  cards, or an astropy WCS such as ``FrameWcs.to_wcs()``), every such pick is
  reprojected to that frame's pixel coordinates with
  :func:`~.picking.try_sky_to_pixel` and measured THERE — so drifted/rotated
  frames (alt-az field rotation, tracking drift) are measured at the star's
  current position instead of being skipped at the stale fixed pixel. A pick
  without a sky coordinate, or whose conversion fails, falls back to its
  fixed pixel position; a frame without a WCS uses fixed pixel picks for all
  picks. The per-frame count of WCS-reprojected picks is recorded on the
  :class:`FrameMeasurement` (``wcs_reprojected``) and aggregated on the run
  reports (WCS-reprojected vs fixed-pixel frames).
- **Centroid refinement of reprojected picks (issue 12).** The stored solve
  WCS carries a ~few-px pointing error (live data: the true star core sits
  4.5–6.7 px from the WCS-predicted pixel on every frame), so with a small
  aperture (r=4) the star core can fall OUTSIDE the aperture while the sky
  annulus (8–12 px) catches the star wings — the background is overestimated,
  the net flux goes non-positive and the frame is spuriously skipped. After
  the reprojection, every reprojected pick is therefore refined ON THE FRAME
  DATA: :func:`refine_pick_on_frame` searches a
  :data:`REPROJECT_SEARCH_RADIUS_PX` window around the predicted pixel for a
  significant local brightness maximum (peak > background + k·σ, σ = the
  robust window noise, floored by the Poisson-like √background) and centroids
  it in a :data:`REPROJECT_CENTROID_RADIUS_PX` window around that maximum
  (weights ``clip(value - background, 0, None)``). The refined position is
  what gets measured, and the refinement is recorded — the per-star offset
  ``StarMeasurement.pick_offset`` (refined minus predicted) and the per-frame
  ``FrameMeasurement.wcs_refined`` count. When no significant local maximum
  is found near the predicted pixel, the predicted pixel is kept and the
  normal skip logic applies downstream (a no-star frame still skips with
  ``kind="non-positive-flux"``; a reprojected pick outside the image still
  skips with ``kind="outside-image"``, one whose sky annulus crosses the
  image border with ``kind="annulus-outside-image"`` — issue 19).
  Fixed-pixel picks (no WCS, or a failed conversion) are NEVER refined —
  the fallback path is unchanged.
- **Pixel convention: 0-based, ``x`` = column, ``y`` = row** (same as
  :mod:`.picking` and the numpy ``data[y, x]`` array). photutils apertures take
  0-based ``(x, y)`` positions directly.
- **Skip conditions** — PER-PICK first, per frame only as a last resort
  (issue 14): a pick center outside the image, a pick whose sky annulus is
  not fully inside the image (issue 19 — a truncated annulus biases the
  background, so edge stars are dropped rather than silently mis-measured),
  or a background-subtracted flux that is non-positive or non-finite —
  including a WCS-reprojected pick that lands outside the image (issue 09) —
  drops THAT PICK for that frame only, recorded as a :class:`DroppedPick`
  (role/label/reason/``kind``) on the :class:`FrameMeasurement`
  (``dropped_picks``); the frame is still measured when the target + at
  least one comp remain. The frame itself is skipped (a
  :class:`FrameSkippedError` raised by :func:`measure_frame` /
  :func:`measure_frame_data`, recorded by :func:`measure_project`) only when
  the TARGET pick is unusable (the frame skip then carries the target's drop
  ``kind``, e.g. ``"outside-image"``) or when NO comp pick remains (kind
  ``"no-comps"``); the frame-level raise also carries the per-pick drops on
  the error's ``dropped_picks`` attribute. A frame that cannot be
  read/parsed at all raises :class:`MeasurementError` (which
  :func:`measure_project` also records as skipped — a bad frame never kills
  the run). Every skip carries a short machine-readable ``kind`` (e.g.
  ``"outside-image"``, ``"annulus-outside-image"``, ``"non-positive-flux"``,
  ``"unreadable-frame"`` — the ``kind`` attribute of the raised error), which
  the reports aggregate into frame-level skip-reason counts
  (``skip_reasons``) — the per-pick drops are aggregated SEPARATELY, prefixed
  ``pick-`` (``pick_skip_reasons``, e.g. ``{"pick-outside-image": 146}``,
  broken down per pick label in ``pick_skip_reasons_by_label``), so
  frame-level and pick-level drops stay distinguishable.
- **JD** comes from the FITS header ``DATE-OBS`` (parsed with
  :mod:`astropy.time` via
  :func:`seestar_controller.store.frames._date_obs_jd_utc`); when the header
  has no usable ``DATE-OBS``, :func:`measure_frame` falls back to the frame's
  stored ``jd`` (the store's own DATE-OBS/mtime derivation).
- **Error array** for photutils: Poisson-like ``sqrt(clip(data, 1, None))``
  over the finite pixels (non-finite pixels are masked out), so
  ``aperture_sum_err`` is always finite and positive for a real star.
- **Error model (issue 15).** ``mag_err`` is NOT just the photon/read-noise
  propagation ``MAG_ERR_COEFF * flux_err / flux`` — that underestimates the real
  scatter by ~50x on small-aperture runs (median 0.006 vs 0.30 mag observed).
  On top of the Poisson/read-noise term, the model folds in (in quadrature, on
  the flux, before the magnitude propagation):
  - the **positional/centering uncertainty** of the aperture center w.r.t. the
    star core, converted to a fractional flux error via
    :func:`positional_flux_frac` (the aperture-flux curvature
    :func:`_aperture_flux_curvature`, which blows up at small aperture radii —
    e.g. r=1.5 px). The positional uncertainty is: the centroid uncertainty of a
    centroid-refined reprojected pick (``~CENTROID_SIGMA_COEFF * sigma_psf /
    SNR``), plus a residual fraction of its recorded ``pick_offset``; the WCS
    match rms converted to px (:func:`_wcs_rms_px`) when the frame WCS is a
    :class:`~seestar_controller.store.wcs.FrameWcs` carrying ``rms_arcsec``
    (it bounds the astrometric uncertainty of every position in the frame,
    including stale fixed picks); and a default few-px pointing error
    (:data:`DEFAULT_REPROJECT_ERR_PX`) for reprojected picks that were NOT
    refined and whose WCS carries no usable rms;
  - a **small-aperture systematics floor** :func:`aperture_systematics_frac` —
    the fraction of the star light OUTSIDE the aperture is uncertain (PSF /
    seeing / focus variation) and propagates straight into the flux.
- **RGB frames** are reduced to their channel mean first (same as the
  picking snap); anything that is not 2-D after that raises
  :class:`MeasurementError`.
"""

from __future__ import annotations

import io
import logging
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

import numpy as np

from seestar_controller.store.errors import (
    MeasurementError as StoreMeasurementError,
)
from seestar_controller.store.errors import ProjectError, WcsStoreError
from seestar_controller.store.frames import (
    KIND_SUB,
    _date_obs_jd_utc,
    header_to_dict,
    list_frames,
    read_frame_bytes,
)
from seestar_controller.store.measurements import Measurement, add_measurement
from seestar_controller.store.stars import ROLE_CHECK, ROLE_COMP, ROLE_TARGET, StarPick
from seestar_controller.store.wcs import FrameWcs, load_frame_wcs

from .picking import (
    ApertureParams,
    PhotometryError,
    StarPickSpec,
    try_sky_to_pixel,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DroppedPick:
    """One pick DROPPED for ONE frame (issue 14 — per-PICK skip semantics):
    the pick landed outside the image, or its (possibly WCS-reprojected and
    centroid-refined) position gave a non-positive/non-finite flux — so it
    does not contribute to that frame, but the frame itself may still be
    measured (target + at least one comp remain).

    - ``role`` / ``label`` — copied from the pick (``label or role`` names
      the pick in the aggregated reports);
    - ``reason`` — the human-readable failure (the message of the raised
      :class:`FrameSkippedError`);
    - ``kind`` — the machine-readable drop kind (``"outside-image"``,
      ``"non-positive-flux"``, ``"invalid-flux-error"``), aggregated on the
      reports as ``pick-<kind>``.
    """

    role: str
    label: str | None
    reason: str
    kind: str = ""


class MeasurementError(PhotometryError):
    """Any photometry-measurement failure — a :class:`~.picking.PhotometryError`
    (a :class:`ValueError`) with a clear message: a bad pick ensemble (not
    exactly one target / no comps), a bad aperture, an unknown frame, an
    unreadable frame file, a missing JD, or a store failure while saving the
    measurement (the store's
    :class:`~seestar_controller.store.errors.MeasurementError` is wrapped and
    chained — this class is deliberately distinct from it).

    Every error carries a short machine-readable ``kind`` (a class attribute,
    overridable per raise with the ``kind=`` keyword) used to aggregate skip
    reasons on the reports — e.g. ``"unreadable-frame"``, ``"no-jd"``,
    ``"store-failed"``. Issue 14: a frame-level skip raised after per-pick
    drops also carries the :class:`DroppedPick`\\ s of that frame on
    ``dropped_picks`` (empty by default)."""

    kind = "measurement-error"

    def __init__(
        self,
        message: str,
        *,
        kind: str | None = None,
        dropped_picks: Iterable[DroppedPick] = (),
    ) -> None:
        super().__init__(message)
        if kind is not None:
            self.kind = kind
        self.dropped_picks: tuple[DroppedPick, ...] = tuple(dropped_picks)


class FrameSkippedError(MeasurementError):
    """A frame was skipped: the target pick is unusable (it falls outside the
    image, its sky annulus is not fully inside the image (issue 19), or its
    flux is non-positive/non-finite — possibly after the WCS reprojection +
    refinement), or no comp pick remains (issue 14). Carries the
    human-readable ``reason`` and a ``kind`` (``"outside-image"``,
    ``"annulus-outside-image"``, ``"non-positive-flux"``,
    ``"invalid-flux-error"``, ``"no-comps"`` — the target's drop kind when
    the target is the problem) plus the frame's per-pick drops on
    ``dropped_picks``;
    :func:`measure_project` records these as :class:`SkippedFrame` entries
    instead of crashing."""

    kind = "skipped-frame"


#: The magnitude-error coefficient ``2.5 / ln(10) ≈ 1.0857``:
#: ``d(-2.5 log10 f) = -2.5/ln(10) * df/f``.
MAG_ERR_COEFF = 2.5 / math.log(10.0)

#: Half-size of the search window (px) around a WCS-reprojected predicted
#: pixel in which :func:`refine_pick_on_frame` looks for the local brightness
#: maximum before centroiding (issue 12) — ~8–10 px covers the live ~5 px
#: plate-solve pointing error.
REPROJECT_SEARCH_RADIUS_PX = 10.0
#: Half-size of the centroid window (px) around the found maximum (issue 12).
REPROJECT_CENTROID_RADIUS_PX = 5.0
#: A local maximum counts as significant when it exceeds the window
#: background by more than this many (robust) sigmas (issue 12).
REPROJECT_SIGNIFICANCE_SIGMA = 5.0

#: Issue 15 — centroid-uncertainty coefficient: the centroid of a star of PSF
#: width ``sigma_psf`` measured with SNR = ``flux / flux_err`` is uncertain by
#: ~``CENTROID_SIGMA_COEFF * sigma_psf / SNR`` px.
CENTROID_SIGMA_COEFF = 1.0
#: Issue 15 — residual fraction of a refinement ``pick_offset`` kept as
#: positional uncertainty of the refined pick: the refinement corrects the
#: pointing offset, but not perfectly (e.g. a centroid window clamped at the
#: search-window edge biases the centroid when the offset is large).
PICK_OFFSET_RESIDUAL_FRAC = 0.1
#: Issue 15 — default positional uncertainty (px) of a WCS-reprojected pick
#: that was NOT centroid-refined and whose WCS carries no usable rms — the
#: ~few-px plate-solve pointing error seen on live frames (issue 12).
DEFAULT_REPROJECT_ERR_PX = 0.5 * REPROJECT_SEARCH_RADIUS_PX
#: Issue 15 — coefficient of the positional fractional-flux error: for a small
#: aperture the flux varies to second order in a center offset
#: (``F(d) ≈ F(0) * (1 - c2 * d^2)``), and the rms over a Gaussian offset
#: distribution of width ``sigma_pos`` is ``sqrt(3) * c2 * sigma_pos^2``.
POSITION_ERR_COEFF = math.sqrt(3.0)
#: Issue 15 — saturation cap of the positional fractional-flux error (an
#: aperture far off the star core measures essentially no star light).
POSITION_FRAC_MAX = 0.95
#: Issue 15 — small-aperture systematics: the fraction of the star light
#: OUTSIDE the aperture (``exp(-r^2 / (2 sigma_psf^2))``) is uncertain at this
#: level (PSF / seeing / focus variation) and propagates straight into the
#: measured flux.
APERTURE_SYS_COEFF = 0.02
#: Issue 15 — the systematics floor only applies to a SMALL aperture, one that
#: misses more than this fraction of the star light (``r < ~1.18 sigma_psf``);
#: for a larger aperture the edge systematics are negligible next to the
#: photon noise, so the floor is exactly 0 there.
APERTURE_SYS_MIN_OUTSIDE_FRAC = 0.5
#: Issue 15 — floor of the PSF-sigma estimate (px) — smaller than any real PSF.
MIN_PSF_SIGMA_PX = 0.7
#: Issue 15 — minimum half-size (px) of the window used to estimate the PSF
#: sigma around a pick (the window half-size is
#: ``max(PSF_ESTIMATE_MIN_RADIUS_PX, 2 * aperture_radius, 3 * sigma_estimate)``,
#: iterated so the window covers ~3 sigma of the PSF and the second moment is
#: not truncated).
PSF_ESTIMATE_MIN_RADIUS_PX = 6.0


def _finite_float(value: Any, what: str) -> float:
    """Coerce ``value`` to a finite float (bools rejected); raise
    :class:`MeasurementError` otherwise."""
    if isinstance(value, bool):
        raise MeasurementError(f"{what} must be a number, got {value!r}")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise MeasurementError(f"{what} must be a number, got {value!r}") from None
    if not math.isfinite(result):
        raise MeasurementError(f"{what} must be finite, got {value!r}")
    return result


# --- pure photometry math ------------------------------------------------------------


def flux_to_mag(flux: float) -> float:
    """The instrumental magnitude of a flux: ``-2.5 * log10(flux)``.

    Raises :class:`MeasurementError` when ``flux`` is not a finite positive
    number (a non-positive flux has no magnitude — the frame is skipped
    upstream by :func:`measure_star`).
    """
    f = _finite_float(flux, "flux")
    if f <= 0.0:
        raise MeasurementError(f"flux must be positive to convert to a magnitude, got {flux!r}")
    return -2.5 * math.log10(f)


def flux_err_to_mag_err(flux: float, flux_err: float) -> float:
    """Propagate a flux error to a magnitude error:
    ``MAG_ERR_COEFF * flux_err / flux`` (≈ ``1.0857 * flux_err / flux``).

    Raises :class:`MeasurementError` when ``flux`` is not finite positive or
    ``flux_err`` is not a finite non-negative number.
    """
    f = _finite_float(flux, "flux")
    e = _finite_float(flux_err, "flux_err")
    if f <= 0.0:
        raise MeasurementError(f"flux must be positive to propagate its error, got {flux!r}")
    if e < 0.0:
        raise MeasurementError(f"flux_err must be >= 0, got {flux_err!r}")
    return MAG_ERR_COEFF * e / f


def ensemble_mag(comp_mags: Sequence[float]) -> float:
    """The **comp ensemble** magnitude (GLOSSARY.md): the mean of the comp
    instrumental mags of one frame. Raises :class:`MeasurementError` when
    ``comp_mags`` is empty or has a non-finite entry."""
    mags = [_finite_float(m, "comp mag") for m in comp_mags]
    if not mags:
        raise MeasurementError("the comp ensemble needs at least one comp mag")
    return math.fsum(mags) / len(mags)


def ensemble_mag_err(comp_mag_errs: Sequence[float]) -> float:
    """The comp-ensemble magnitude error: ``sqrt(mean(comp_err^2))`` (the
    standard error of the mean of the comp mag errors, in quadrature).
    Raises :class:`MeasurementError` when ``comp_mag_errs`` is empty or has a
    non-finite/negative entry."""
    errs = [_finite_float(e, "comp mag_err") for e in comp_mag_errs]
    if not errs:
        raise MeasurementError("the comp ensemble error needs at least one comp mag_err")
    for e in errs:
        if e < 0.0:
            raise MeasurementError(f"comp mag_err must be >= 0, got {e!r}")
    return math.sqrt(math.fsum(e * e for e in errs) / len(errs))


def differential_mag(target_mag: float, ensemble: float) -> float:
    """The differential magnitude: ``target_mag - ensemble`` (the target's
    instrumental mag minus the comp-ensemble mean — calibration-free)."""
    return _finite_float(target_mag, "target_mag") - _finite_float(ensemble, "ensemble")


def differential_mag_err(target_mag_err: float, comp_mag_errs: Sequence[float]) -> float:
    """The differential-magnitude error:
    ``sqrt(target_err^2 + mean(comp_err^2))`` — the target's mag error and
    the comp-ensemble error (see :func:`ensemble_mag_err`) in quadrature."""
    t = _finite_float(target_mag_err, "target_mag_err")
    if t < 0.0:
        raise MeasurementError(f"target_mag_err must be >= 0, got {target_mag_err!r}")
    return math.sqrt(t * t + ensemble_mag_err(comp_mag_errs) ** 2)


# --- the issue-15 error model: positional + small-aperture systematics --------------------


def _psf_sigma_in_window(plane: np.ndarray, x: float, y: float, half: float) -> float:
    """The PSF sigma (px) estimated in the square window of half-size ``half``
    around ``(x, y)`` (clipped to the image): background = median of the
    finite window pixels, weights ``clip(value - background, 0, None)``, the
    weighted centroid computed first and the second moment taken around IT
    (so a jittered star does not inflate the estimate):
    ``sigma^2 = sum(w * r^2) / (2 * sum(w))`` (a 2-D Gaussian has
    ``E[r^2] = 2 sigma^2``). Floored at :data:`MIN_PSF_SIGMA_PX`, capped at
    ``half``; a window with no positive weights yields the floor."""
    height, width = int(plane.shape[0]), int(plane.shape[1])
    x0 = max(0, math.floor(x - half))
    x1 = min(width, math.ceil(x + half) + 1)
    y0 = max(0, math.floor(y - half))
    y1 = min(height, math.ceil(y + half) + 1)
    if x1 <= x0 or y1 <= y0:
        return MIN_PSF_SIGMA_PX
    win = plane[y0:y1, x0:x1]
    finite = np.isfinite(win)
    if not bool(finite.any()):
        return MIN_PSF_SIGMA_PX
    values = win[finite]
    # sigma-clipped background: a plain median is biased up by the star wings
    # of a bright star, which would clip the weights and underestimate sigma
    background = float(np.median(values))
    for _ in range(3):
        mad = float(np.median(np.abs(values - background)))
        clip = max(1.4826 * mad, math.sqrt(max(background, 1.0)), 1e-12)
        keep = values <= background + 2.5 * clip
        if not bool(keep.any()):
            break
        values = values[keep]
        background = float(np.median(values))
    weights = np.where(finite, np.clip(win - background, 0.0, None), 0.0)
    total = float(weights.sum())
    if not total > 0.0:
        return MIN_PSF_SIGMA_PX
    yy, xx = np.mgrid[y0:y1, x0:x1]
    cx = float((weights * xx).sum() / total)
    cy = float((weights * yy).sum() / total)
    r2 = (xx - cx) ** 2 + (yy - cy) ** 2
    sigma2 = float((weights * r2).sum() / (2.0 * total))
    return float(min(max(math.sqrt(max(sigma2, 0.0)), MIN_PSF_SIGMA_PX), half))


def _estimate_psf_sigma(plane: np.ndarray, x: float, y: float, radius_px: float) -> float:
    """Estimate the Gaussian PSF sigma (px) around the pick ``(x, y)`` from the
    frame ``plane`` (issue 15) via :func:`_psf_sigma_in_window`, iterating the
    window half-size up to ``max(PSF_ESTIMATE_MIN_RADIUS_PX, 2 * radius_px,
    3 * sigma_estimate)`` so the window covers ~3 sigma of the PSF and the
    second moment is not truncated."""
    half = max(PSF_ESTIMATE_MIN_RADIUS_PX, 2.0 * radius_px)
    sigma = MIN_PSF_SIGMA_PX
    for _ in range(3):
        sigma = _psf_sigma_in_window(plane, x, y, half)
        half = max(PSF_ESTIMATE_MIN_RADIUS_PX, 2.0 * radius_px, 3.0 * sigma)
    return sigma


def _aperture_flux_curvature(radius_px: float, sigma_psf: float) -> float:
    """The quadratic coefficient ``c2`` of the aperture-flux loss for a small
    center offset ``d`` on a Gaussian PSF of width ``sigma_psf`` (issue 15):
    ``F(d) ≈ F(0) * (1 - c2 * d^2)`` with
    ``c2 = r^2 * exp(-r^2 / 2 sigma^2) / (4 sigma^4 (1 - exp(-r^2 / 2 sigma^2)))``.

    For a small aperture (``r << sigma``) ``c2 → 1 / (2 sigma^2)`` — the
    aperture sits in the steep wing of the star, so a small centering error
    costs a lot of flux; for ``r >> sigma`` the aperture already encircles the
    star and ``c2 → 0``. ``radius_px`` must be positive, ``sigma_psf`` is
    floored at :data:`MIN_PSF_SIGMA_PX`.
    """
    r = _finite_float(radius_px, "radius_px")
    s = max(_finite_float(sigma_psf, "sigma_psf"), MIN_PSF_SIGMA_PX)
    if r <= 0.0:
        raise MeasurementError(f"radius_px must be positive, got {radius_px!r}")
    s2 = s * s
    r2 = r * r
    x = r2 / (2.0 * s2)
    ex = math.exp(-x)
    denom = 4.0 * s2 * s2 * (1.0 - ex)
    if denom <= 0.0:
        return 0.0
    return r2 * ex / denom


def positional_flux_frac(radius_px: float, sigma_psf: float, positional_err_px: float) -> float:
    """The fractional flux uncertainty of a circular aperture of
    ``radius_px`` on a Gaussian PSF ``sigma_psf`` whose center is uncertain by
    ``positional_err_px`` (1σ, px) — issue 15:
    ``min(POSITION_FRAC_MAX, POSITION_ERR_COEFF * c2 * positional_err_px^2)``
    with :func:`_aperture_flux_curvature` ``c2`` (the rms of the second-order
    flux variation over a Gaussian offset distribution is
    ``sqrt(3) * c2 * sigma_pos^2`` = :data:`POSITION_ERR_COEFF`).

    The result saturates at :data:`POSITION_FRAC_MAX` — an aperture far off
    the star core measures essentially no star light. ``positional_err_px <= 0``
    yields 0 (an exactly centered aperture has no positional error).
    """
    err = _finite_float(positional_err_px, "positional_err_px")
    if err < 0.0:
        raise MeasurementError(f"positional_err_px must be >= 0, got {positional_err_px!r}")
    if err == 0.0:
        return 0.0
    c2 = _aperture_flux_curvature(radius_px, sigma_psf)
    return min(POSITION_FRAC_MAX, POSITION_ERR_COEFF * c2 * err * err)


def aperture_systematics_frac(radius_px: float, sigma_psf: float) -> float:
    """The small-aperture edge-systematics fraction (issue 15): for a SMALL
    aperture the fraction of the star light OUTSIDE the aperture,
    ``exp(-r^2 / (2 sigma_psf^2))``, is uncertain at the
    :data:`APERTURE_SYS_COEFF` level (PSF / seeing / focus variation between
    frames), and that uncertainty propagates straight into the
    background-subtracted flux. The floor applies only when the aperture
    misses more than :data:`APERTURE_SYS_MIN_OUTSIDE_FRAC` of the light
    (``r < ~1.18 sigma_psf`` — e.g. the live-run r=1.5 px vs sigma_psf=2 px
    misses ~75%); for a larger aperture (e.g. the default r=4 px vs
    sigma_psf=2 px misses ~14%) the edge systematics are negligible next to
    the photon noise and the floor is exactly 0."""
    r = _finite_float(radius_px, "radius_px")
    s = max(_finite_float(sigma_psf, "sigma_psf"), MIN_PSF_SIGMA_PX)
    if r <= 0.0:
        raise MeasurementError(f"radius_px must be positive, got {radius_px!r}")
    outside = math.exp(-(r * r) / (2.0 * s * s))
    if outside <= APERTURE_SYS_MIN_OUTSIDE_FRAC:
        return 0.0
    return APERTURE_SYS_COEFF * outside


def _pixel_scale_arcsec(cards: Mapping[str, Any]) -> float | None:
    """The pixel scale in arcsec/px from FITS WCS ``cards``: the ``CD`` matrix
    (``sqrt(|det CD|)``), else ``PC`` × ``CDELT``, else ``CDELT`` alone — all
    in deg/px, converted to arcsec. None when the cards carry no usable scale
    (e.g. a raw Seestar sub with no celestial WCS)."""

    def _num(value: Any) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        f = float(value)
        return f if math.isfinite(f) else None

    cd00, cd01 = _num(cards.get("CD1_1")), _num(cards.get("CD1_2"))
    cd10, cd11 = _num(cards.get("CD2_1")), _num(cards.get("CD2_2"))
    if cd00 is not None and cd01 is not None and cd10 is not None and cd11 is not None:
        det = cd00 * cd11 - cd01 * cd10
        if det != 0.0:
            return abs(det) ** 0.5 * 3600.0
    cdelt1, cdelt2 = _num(cards.get("CDELT1")), _num(cards.get("CDELT2"))
    if cdelt1 is not None and cdelt2 is not None:
        pc00 = _num(cards.get("PC1_1", 1.0))
        pc01 = _num(cards.get("PC1_2", 0.0))
        pc10 = _num(cards.get("PC2_1", 0.0))
        pc11 = _num(cards.get("PC2_2", 1.0))
        det_pc = 1.0
        if pc00 is not None and pc01 is not None and pc10 is not None and pc11 is not None:
            det_pc = pc00 * pc11 - pc01 * pc10
        scale = abs(det_pc) ** 0.5 * math.sqrt(abs(cdelt1 * cdelt2))
        if scale > 0.0:
            return scale * 3600.0
    return None


def _wcs_rms_px(wcs: Any) -> float | None:
    """The WCS match RMS converted to pixels (issue 15): only a
    :class:`~seestar_controller.store.wcs.FrameWcs` record carries
    ``rms_arcsec``; the pixel scale comes from its cards via
    :func:`_pixel_scale_arcsec`. Returns None when the wcs is not a
    ``FrameWcs`` (plain cards / an astropy WCS carry no rms), when the record
    has no rms, or when no pixel scale is derivable."""
    if not isinstance(wcs, FrameWcs):
        return None
    if wcs.rms_arcsec is None:
        return None
    scale = _pixel_scale_arcsec(wcs.cards)
    if scale is None or scale <= 0.0:
        return None
    return wcs.rms_arcsec / scale


# --- per-star aperture photometry ------------------------------------------------------


@dataclass(frozen=True)
class StarMeasurement:
    """One picked star measured on one frame.

    - ``role`` / ``label`` — copied from the pick;
    - ``x`` / ``y`` — the 0-based pixel position measured;
    - ``flux`` — the background-subtracted aperture flux (annulus mean ×
      aperture area subtracted from the aperture sum);
    - ``flux_err`` — the propagated flux error (photutils
      ``aperture_sum_err`` of the aperture and the annulus in quadrature);
    - ``mag`` — the instrumental magnitude ``-2.5*log10(flux)``;
    - ``mag_err`` — the issue-15 error model: ``MAG_ERR_COEFF *
      flux_err_model / flux`` where ``flux_err_model`` is the photon/read-noise
      ``flux_err`` plus, in quadrature, the positional/centering term
      (:func:`positional_flux_frac` × flux) and the small-aperture systematics
      floor (:func:`aperture_systematics_frac` × flux);
    - ``pick_offset`` — issue 12: the refinement offset ``(dx, dy)`` of a
      WCS-reprojected pick that was centroid-refined on the frame data
      (refined position minus the WCS-predicted pixel), or None when the
      star was measured at its predicted/fixed pixel (never refined).
    """

    role: str
    label: str | None
    x: float
    y: float
    flux: float
    flux_err: float
    mag: float
    mag_err: float
    pick_offset: tuple[float, float] | None = None


def _as_2d_plane(data: Any) -> np.ndarray:
    """Coerce ``data`` to a finite-masked 2-D float64 plane (RGB images are
    reduced to their channel mean); raise :class:`MeasurementError` when it
    is not image-like."""
    arr = np.asarray(data, dtype=np.float64)
    if arr.ndim == 3:
        arr = arr[..., :3].mean(axis=2)
    if arr.ndim != 2 or arr.size == 0:
        raise MeasurementError(
            f"frame data must be a 2-D image array, got shape {arr.shape!r}",
            kind="bad-frame-data",
        )
    return arr


def measure_star(
    data: Any,
    x: float,
    y: float,
    aperture: ApertureParams,
    *,
    role: str = ROLE_COMP,
    label: str | None = None,
    pick_offset: tuple[float, float] | None = None,
    positional_err_px: float | None = None,
    wcs_err_px: float | None = None,
) -> StarMeasurement:
    """Measure ONE star at 0-based pixel ``(x, y)`` with circular-aperture +
    sky-annulus photometry (photutils, ``method="exact"``).

    - the pick center must lie inside the image, else
      :class:`FrameSkippedError` (kind ``"outside-image"``);
    - issue 19: the sky annulus must ALSO be fully inside the image (the
      outer annulus circle within the pixel-center hull ``0..width-1`` ×
      ``0..height-1``), else :class:`FrameSkippedError` with kind
      ``"annulus-outside-image"`` — a truncated annulus would silently bias
      the background and the flux of edge stars;
    - the background is the annulus mean (``aperture_sum / annulus.area``)
      times the aperture area, subtracted from the aperture sum;
    - ``flux_err`` = ``sqrt(aperture_sum_err^2 + (ap_area * annulus_sum_err /
      annulus_area)^2)`` — both photutils errors in quadrature (the
      Poisson/read-noise term);
    - the flux error array is Poisson-like ``sqrt(clip(data, 1, None))`` over
      the finite pixels (non-finite pixels are masked);
    - a non-positive or non-finite background-subtracted flux raises
      :class:`FrameSkippedError` (no magnitude exists then);
    - ``pick_offset`` — issue 12: the refinement offset ``(dx, dy)`` of a
      centroid-refined reprojected pick (refined minus predicted pixel),
      recorded verbatim on the returned :class:`StarMeasurement`; None when
      the star is measured at its predicted/fixed pixel;
    - issue 15 (``mag_err``): on top of the photon/read-noise term, the model
      adds in quadrature on the flux
      - the **positional/centering term**: ``positional_flux_frac(radius,
        sigma_psf, sigma_pos) * flux`` where ``sigma_psf`` is estimated from
        the frame around the pick (:func:`_estimate_psf_sigma`) and
        ``sigma_pos`` is the 1σ uncertainty of the aperture center in px —
        ``positional_err_px`` when given (an explicit override, e.g. the WCS
        pointing error of an unrefined reprojected pick), else derived from
        ``pick_offset`` when present: the centroid uncertainty
        ``CENTROID_SIGMA_COEFF * sigma_psf * flux_err / flux`` plus a residual
        ``PICK_OFFSET_RESIDUAL_FRAC`` of ``|pick_offset|``; plus ``wcs_err_px``
        (the frame WCS match rms in px, :func:`_wcs_rms_px`) when given — all
        in quadrature;
      - the **small-aperture systematics floor**:
        ``aperture_systematics_frac(radius, sigma_psf) * flux``.
      Then ``mag_err = MAG_ERR_COEFF * flux_err_model / flux``.

    Returns the :class:`StarMeasurement`.
    """
    if not isinstance(aperture, ApertureParams):
        raise MeasurementError(
            f"aperture must be an ApertureParams, got {aperture!r}",
            kind="bad-aperture",
        )
    px = _finite_float(x, "x")
    py = _finite_float(y, "y")
    if pick_offset is not None:
        if not isinstance(pick_offset, (tuple, list)) or len(pick_offset) != 2:
            raise MeasurementError(
                f"pick_offset must be a (dx, dy) pair of numbers, got {pick_offset!r}"
            )
        pick_offset = (
            _finite_float(pick_offset[0], "pick_offset dx"),
            _finite_float(pick_offset[1], "pick_offset dy"),
        )
    sigma_pos = 0.0
    if positional_err_px is not None:
        sigma_pos = _finite_float(positional_err_px, "positional_err_px")
        if sigma_pos < 0.0:
            raise MeasurementError(
                f"positional_err_px must be >= 0, got {positional_err_px!r}"
            )
    if wcs_err_px is not None:
        wcs_err = _finite_float(wcs_err_px, "wcs_err_px")
        if wcs_err < 0.0:
            raise MeasurementError(f"wcs_err_px must be >= 0, got {wcs_err_px!r}")
        sigma_pos = math.hypot(sigma_pos, wcs_err)
    plane = _as_2d_plane(data)
    height, width = int(plane.shape[0]), int(plane.shape[1])
    if not (0.0 <= px < width and 0.0 <= py < height):
        raise FrameSkippedError(
            # issue 20: unambiguous axis order — height x width in px
            f"pick ({px}, {py}) falls outside the {height}x{width} px image (height x width)",
            kind="outside-image",
        )
    # issue 19: the sky annulus must be FULLY inside the image too — a
    # truncated annulus silently biases the background (and with it the
    # flux), which showed up as comp scatter / >1 mag outliers for edge
    # stars. The check uses the pixel-center hull of the image
    # (columns 0..width-1, rows 0..height-1): the outer annulus circle must
    # fit within it. The aperture (radius < sky_in < sky_out) is covered by
    # this check.
    r_out = aperture.sky_out_px
    if not (r_out <= px <= width - 1 - r_out and r_out <= py <= height - 1 - r_out):
        raise FrameSkippedError(
            f"sky annulus (outer radius {r_out} px) of pick ({px}, {py}) is not "
            f"fully inside the {width}x{height} image",
            kind="annulus-outside-image",
        )

    from photutils.aperture import (
        CircularAnnulus,
        CircularAperture,
        aperture_photometry,
    )

    finite = np.isfinite(plane)
    mask = None if bool(finite.all()) else ~finite
    error = np.sqrt(np.clip(np.where(finite, plane, 1.0), 1.0, None))
    position = [(px, py)]
    ap = CircularAperture(position, r=aperture.radius_px)
    ann = CircularAnnulus(position, r_in=aperture.sky_in_px, r_out=aperture.sky_out_px)
    phot_ap = aperture_photometry(plane, ap, error=error, mask=mask, method="exact")
    phot_ann = aperture_photometry(plane, ann, error=error, mask=mask, method="exact")
    ap_sum = float(phot_ap["aperture_sum"][0])
    ap_err = float(phot_ap["aperture_sum_err"][0])
    ann_sum = float(phot_ann["aperture_sum"][0])
    ann_err = float(phot_ann["aperture_sum_err"][0])
    ap_area = float(ap.area)
    ann_area = float(ann.area)
    bkg_mean = ann_sum / ann_area
    flux = ap_sum - bkg_mean * ap_area
    flux_err = math.sqrt(ap_err * ap_err + (ap_area * ann_err / ann_area) ** 2)
    if not math.isfinite(flux) or flux <= 0.0:
        raise FrameSkippedError(
            f"non-positive flux {flux!r} at pick ({px}, {py}) "
            f"(aperture sum {ap_sum!r}, background {bkg_mean!r})",
            kind="non-positive-flux",
        )
    if not math.isfinite(flux_err) or flux_err < 0.0:
        raise FrameSkippedError(
            f"invalid flux error {flux_err!r} at pick ({px}, {py})",
            kind="invalid-flux-error",
        )
    # issue 15: the full error model — photon/read noise + positional term +
    # small-aperture systematics, folded in quadrature on the flux
    sigma_psf = _estimate_psf_sigma(plane, px, py, aperture.radius_px)
    if positional_err_px is None and pick_offset is not None:
        # a centroid-refined reprojected pick: the refinement corrected the
        # pointing offset; its residual uncertainty is the centroid uncertainty
        # (~ sigma_psf / SNR) plus a residual fraction of the measured offset,
        # combined (in quadrature) with any explicit wcs_err_px already folded in
        sigma_c = CENTROID_SIGMA_COEFF * sigma_psf * flux_err / flux
        sigma_pos = math.hypot(
            sigma_pos,
            sigma_c,
            PICK_OFFSET_RESIDUAL_FRAC * math.hypot(*pick_offset),
        )
    frac_pos = positional_flux_frac(aperture.radius_px, sigma_psf, sigma_pos)
    frac_sys = aperture_systematics_frac(aperture.radius_px, sigma_psf)
    flux_err_model = math.sqrt(
        flux_err * flux_err
        + (frac_pos * flux) ** 2
        + (frac_sys * flux) ** 2
    )
    return StarMeasurement(
        role=role,
        label=label,
        x=px,
        y=py,
        flux=flux,
        flux_err=flux_err,
        mag=flux_to_mag(flux),
        mag_err=flux_err_to_mag_err(flux, flux_err_model),
        pick_offset=pick_offset,
    )


# --- picks normalization -----------------------------------------------------------------


@dataclass(frozen=True)
class _NormPick:
    """One normalized pick: role/label/pixel/sky + the aperture to measure
    with.

    ``ra_deg`` / ``dec_deg`` — the pick's J2000 sky position (from
    ``StarPick.ra_deg``/``dec_deg`` or ``StarPickSpec.coord``), or None for a
    pixel-only pick; used for the per-frame WCS reprojection (issue 09).

    ``reprojected`` — True when this pick's pixel came from the per-frame WCS
    reprojection (issue 09) — only such picks are centroid-refined on the
    frame data (issue 12); ``refined_from`` — the predicted (pre-refinement)
    pixel of a refined pick, else None.
    """

    role: str
    label: str | None
    x: float
    y: float
    aperture: ApertureParams
    ra_deg: float | None = None
    dec_deg: float | None = None
    reprojected: bool = False
    refined_from: tuple[float, float] | None = None


def _sky_of(pick: Any) -> tuple[float | None, float | None]:
    """The pick's sky position as ``(ra_deg, dec_deg)`` — from a store
    :class:`~seestar_controller.store.stars.StarPick` (``ra_deg``/``dec_deg``)
    or a :class:`~.picking.StarPickSpec` (``coord``); ``(None, None)`` for a
    pixel-only pick (either coordinate missing)."""
    if isinstance(pick, StarPick):
        return pick.ra_deg, pick.dec_deg
    coord = getattr(pick, "coord", None)
    if coord is None:
        return None, None
    return float(coord.ra_deg), float(coord.dec_deg)


def _aperture_of(pick: Any, default: ApertureParams | None) -> ApertureParams:
    """The aperture for ``pick``: a store :class:`~.store.stars.StarPick`
    carries its own per-pick aperture (which wins); anything else uses
    ``default`` (or the :class:`ApertureParams` default when None)."""
    if isinstance(pick, StarPick):
        return ApertureParams(
            radius_px=pick.aperture_radius,
            sky_in_px=pick.sky_annulus_inner,
            sky_out_px=pick.sky_annulus_outer,
        )
    if default is not None:
        return default
    return ApertureParams()


def _normalize_picks(
    picks: Iterable[Any], aperture: ApertureParams | None
) -> tuple[_NormPick, ...]:
    """Normalize ``picks`` (StarPickSpec or store StarPick records) to
    :class:`_NormPick` tuples and validate the ensemble: EXACTLY ONE
    ``"target"``, at least one ``"comp"`` and AT MOST ONE ``"check"`` — else
    :class:`MeasurementError` (before any frame is read). The check star is
    kept apart from the comps: the comp ensemble is built from the comps
    only."""
    if aperture is not None and not isinstance(aperture, ApertureParams):
        raise MeasurementError(
            f"aperture must be an ApertureParams or None, got {aperture!r}",
            kind="bad-picks",
        )
    if isinstance(picks, (str, bytes)) or not isinstance(picks, Iterable):
        raise MeasurementError(
            f"picks must be an iterable of StarPickSpec/StarPick, got {picks!r}",
            kind="bad-picks",
        )
    norm: list[_NormPick] = []
    for pick in picks:
        if isinstance(pick, (StarPickSpec, StarPick)):
            role, label, x, y = pick.role, pick.label, pick.x, pick.y
        else:
            raise MeasurementError(
                f"picks must contain only StarPickSpec or StarPick records, got {pick!r}",
                kind="bad-picks",
            )
        if role not in (ROLE_TARGET, ROLE_COMP, ROLE_CHECK):
            raise MeasurementError(
                f"pick role must be 'target', 'comp' or 'check', got {role!r}",
                kind="bad-picks",
            )
        ra_deg, dec_deg = _sky_of(pick)
        norm.append(
            _NormPick(
                role=role,
                label=label,
                x=_finite_float(x, "pick x"),
                y=_finite_float(y, "pick y"),
                aperture=_aperture_of(pick, aperture),
                ra_deg=None if ra_deg is None else _finite_float(ra_deg, "pick ra_deg"),
                dec_deg=None if dec_deg is None else _finite_float(dec_deg, "pick dec_deg"),
            )
        )
    targets = [p for p in norm if p.role == ROLE_TARGET]
    comps = [p for p in norm if p.role == ROLE_COMP]
    checks = [p for p in norm if p.role == ROLE_CHECK]
    if len(targets) != 1:
        raise MeasurementError(
            f"the pick ensemble must contain exactly one 'target' pick, got "
            f"{len(targets)} (comps: {len(comps)}, checks: {len(checks)})",
            kind="bad-picks",
        )
    if not comps:
        raise MeasurementError(
            "the pick ensemble needs at least one 'comp' pick (the comp ensemble)",
            kind="bad-picks",
        )
    if len(checks) > 1:
        raise MeasurementError(
            f"the pick ensemble may contain at most one 'check' pick, got "
            f"{len(checks)}",
            kind="bad-picks",
        )
    return tuple(norm)


def _wcs_cards(wcs: Any) -> Mapping[str, Any] | None:
    """Normalize a per-frame WCS argument (issue 09) to a mapping of FITS
    cards: a :class:`~seestar_controller.store.wcs.FrameWcs` record (its
    ``cards``), a mapping of FITS cards, or an astropy WCS (e.g.
    ``FrameWcs.to_wcs()`` — via ``to_header()``). None stays None (no WCS —
    fixed pixel picks). Anything else raises :class:`MeasurementError`."""
    if wcs is None:
        return None
    if isinstance(wcs, FrameWcs):
        return wcs.cards
    if isinstance(wcs, Mapping):
        return dict(wcs)
    to_header = getattr(wcs, "to_header", None)
    if callable(to_header):  # an astropy.wcs.WCS (e.g. FrameWcs.to_wcs())
        try:
            return dict(to_header())
        except Exception as exc:
            raise MeasurementError(
                f"cannot read the WCS header of {wcs!r}: {exc}", kind="bad-wcs"
            ) from exc
    raise MeasurementError(
        "wcs must be None, a FrameWcs record, a mapping of FITS cards, or an "
        f"astropy WCS, got {wcs!r}",
        kind="bad-wcs",
    )


def refine_pick_on_frame(
    data: Any,
    x: float,
    y: float,
    *,
    search_radius_px: float = REPROJECT_SEARCH_RADIUS_PX,
    centroid_radius_px: float = REPROJECT_CENTROID_RADIUS_PX,
    significance_sigma: float = REPROJECT_SIGNIFICANCE_SIGMA,
) -> tuple[float, float] | None:
    """Refine a predicted pixel ``(x, y)`` on the actual frame ``data`` (issue
    12): find the local brightness maximum within ``search_radius_px`` of the
    predicted pixel and centroid it within ``centroid_radius_px`` of that
    maximum, returning the refined 0-based ``(x, y)``.

    - the search window is the square ``[x ± search_radius_px] × [y ±
      search_radius_px]`` clipped to the image; its robust background is the
      median of its finite pixels and its noise σ the
      ``1.4826 * MAD`` floored by the Poisson-like ``sqrt(max(background,
      1))`` (the same noise model as :func:`measure_star`'s error array);
    - the brightest finite pixel of the window is the candidate maximum; it
      counts as SIGNIFICANT only when it exceeds ``background +
      significance_sigma * σ`` — otherwise (flat/noise window, no star near
      the predicted pixel) **None** is returned and the caller keeps the
      predicted pixel (the normal skip logic then applies downstream);
    - the centroid uses weights ``clip(value - background, 0, None)`` over
      the finite pixels of the centroid window (clamped to the search window
      and the image); when the weights sum to zero the maximum pixel itself
      is returned.

    The refined position always stays within ``search_radius_px`` of the
    predicted pixel. Never raises on bad window geometry (a predicted pixel
    outside the image yields None) — bad *arguments* (non-finite ``x``/``y``,
    non-positive radii/sigma) raise :class:`MeasurementError`.
    """
    px = _finite_float(x, "x")
    py = _finite_float(y, "y")
    search = _finite_float(search_radius_px, "search_radius_px")
    centroid_r = _finite_float(centroid_radius_px, "centroid_radius_px")
    k_sigma = _finite_float(significance_sigma, "significance_sigma")
    if search <= 0.0 or centroid_r <= 0.0:
        raise MeasurementError(
            f"search_radius_px and centroid_radius_px must be positive, got "
            f"{search_radius_px!r} / {centroid_radius_px!r}",
            kind="bad-refinement-params",
        )
    if k_sigma <= 0.0:
        raise MeasurementError(
            f"significance_sigma must be positive, got {significance_sigma!r}",
            kind="bad-refinement-params",
        )
    plane = _as_2d_plane(data)
    height, width = int(plane.shape[0]), int(plane.shape[1])
    x0 = max(0, math.floor(px - search))
    x1 = min(width, math.ceil(px + search) + 1)
    y0 = max(0, math.floor(py - search))
    y1 = min(height, math.ceil(py + search) + 1)
    if x1 <= x0 or y1 <= y0:
        return None  # the predicted pixel is (entirely) outside the image
    win = plane[y0:y1, x0:x1]
    finite = np.isfinite(win)
    if not bool(finite.any()):
        return None
    values = win[finite]
    background = float(np.median(values))
    mad = float(np.median(np.abs(values - background)))
    sigma = max(1.4826 * mad, math.sqrt(max(background, 1.0)), 1e-12)
    masked = np.where(finite, win, -np.inf)
    iy, ix = np.unravel_index(int(np.argmax(masked)), masked.shape)
    peak = float(win[iy, ix])
    if not peak > background + k_sigma * sigma:
        return None  # no significant local maximum near the predicted pixel
    max_x, max_y = x0 + int(ix), y0 + int(iy)
    # the centroid window around the maximum, clamped to the search window
    cx0 = max(x0, math.floor(max_x - centroid_r))
    cx1 = min(x1, math.ceil(max_x + centroid_r) + 1)
    cy0 = max(y0, math.floor(max_y - centroid_r))
    cy1 = min(y1, math.ceil(max_y + centroid_r) + 1)
    cwin = plane[cy0:cy1, cx0:cx1]
    weights = np.where(np.isfinite(cwin), np.clip(cwin - background, 0.0, None), 0.0)
    total = float(weights.sum())
    if not total > 0.0:
        return float(max_x), float(max_y)
    yy, xx = np.mgrid[cy0:cy1, cx0:cx1]
    return float((weights * xx).sum() / total), float((weights * yy).sum() / total)


def resolve_pick_pixels(
    picks: Iterable[_NormPick], wcs: Any
) -> tuple[tuple[_NormPick, ...], int]:
    """The per-frame pixel resolution step (issue 09): reproject every pick
    that carries a sky coordinate (``ra_deg``/``dec_deg``) onto the frame WCS
    ``wcs`` (a :class:`~seestar_controller.store.wcs.FrameWcs` record, a
    mapping of FITS cards, or an astropy WCS) via
    :func:`~.picking.try_sky_to_pixel`, returning the resolved picks (a pick
    without a sky coordinate, or whose conversion fails, keeps its fixed
    pixel) plus the number of WCS-reprojected picks. Reprojected picks are
    flagged ``reprojected=True`` on the returned records — those are the
    picks :func:`_refine_reprojected_picks` centroid-refines on the frame
    data (issue 12).

    With ``wcs=None`` the picks pass through unchanged (fixed pixel picks,
    count 0). The reprojected position is NOT range-checked here — a
    reprojected pick outside the image is a normal per-frame skip
    (:class:`FrameSkippedError`, kind ``"outside-image"``) downstream in
    :func:`measure_star`, and so is a reprojected pick whose sky annulus
    crosses the image border (kind ``"annulus-outside-image"``, issue 19).
    """
    norm = tuple(picks)
    cards = _wcs_cards(wcs)
    if cards is None:
        return norm, 0
    resolved: list[_NormPick] = []
    reprojected = 0
    for pick in norm:
        if pick.ra_deg is not None and pick.dec_deg is not None:
            xy = try_sky_to_pixel(cards, (pick.ra_deg, pick.dec_deg))
            if xy is not None:
                resolved.append(replace(pick, x=xy[0], y=xy[1], reprojected=True))
                reprojected += 1
                continue
            # conversion failed (e.g. the stored cards carry no celestial
            # WCS) -> fixed pixel pick fallback for this pick
            logger.debug(
                "WCS reprojection failed for pick %s — using the fixed pixel "
                "(%.2f, %.2f)",
                pick.role,
                pick.x,
                pick.y,
            )
        resolved.append(pick)
    return tuple(resolved), reprojected


def _refine_reprojected_picks(
    picks: tuple[_NormPick, ...], plane: np.ndarray
) -> tuple[tuple[_NormPick, ...], int]:
    """The issue-12 refinement step: centroid-refine every WCS-reprojected
    pick (``reprojected=True``) of ``picks`` on the frame ``plane`` via
    :func:`refine_pick_on_frame`, returning the refined picks (each carrying
    ``refined_from`` = its predicted pixel) plus the number of refined picks.

    A reprojected pick with NO significant local maximum near its predicted
    pixel is kept at the predicted pixel (the normal skip logic applies
    downstream — e.g. a no-star frame still skips with
    ``kind="non-positive-flux"``). Fixed-pixel picks (``reprojected=False``)
    are never touched — the fallback path is unchanged.
    """
    refined: list[_NormPick] = []
    count = 0
    for pick in picks:
        if not pick.reprojected:
            refined.append(pick)
            continue
        xy = refine_pick_on_frame(plane, pick.x, pick.y)
        if xy is None:
            logger.debug(
                "no significant local maximum near the reprojected pick %s "
                "(%.2f, %.2f) — keeping the predicted pixel",
                pick.role,
                pick.x,
                pick.y,
            )
            refined.append(pick)
            continue
        count += 1
        refined.append(replace(pick, x=xy[0], y=xy[1], refined_from=(pick.x, pick.y)))
    return tuple(refined), count


# --- the pure core on a raw array ---------------------------------------------------------


@dataclass(frozen=True)
class FrameMeasurement:
    """The photometry of ONE frame: every pick measured, the comp ensemble,
    and the differential magnitude.

    - ``jd`` — the frame time as Julian Date (from the header ``DATE-OBS``,
      or the explicit fallback);
    - ``target`` — the target's :class:`StarMeasurement`;
    - ``comps`` — the comps' :class:`StarMeasurement` records (the check star
      is NEVER among them — the ensemble is the mean of the comps only);
    - ``ensemble_mag`` — the comp-ensemble mean instrumental mag;
    - ``mag`` — the differential magnitude ``target.mag - ensemble_mag``;
    - ``mag_err`` — ``sqrt(target.mag_err^2 + mean(comp.mag_err^2))`` where
      each star's ``mag_err`` is the issue-15 error model of
      :func:`measure_star` (photon/read noise + positional/centering term +
      small-aperture systematics);
    - ``check`` — the check star's :class:`StarMeasurement`, or None when no
      check star was picked;
    - ``check_mag`` — the check star's differential magnitude
      ``check.mag - ensemble_mag`` (None without a check star) — computed
      EXACTLY like the target's (same ensemble, same error propagation);
    - ``check_mag_err`` — ``sqrt(check.mag_err^2 + mean(comp.mag_err^2))``
      (None without a check star);
    - ``target_minus_check`` — ``target.mag - check.mag`` (the
      target-minus-check curve; equals ``mag - check_mag`` because the
      ensemble cancels), None without a check star;
    - ``target_minus_check_err`` — ``sqrt(target.mag_err^2 +
      check.mag_err^2)`` — the ensemble cancels in the difference, so the
      error is the quadrature of the two star mag errors (None without a
      check star);
    - ``wcs_reprojected`` — how many picks were reprojected onto THIS frame's
      WCS before measuring (issue 09); 0 means the frame was measured on the
      fixed pixel picks (no WCS, or no pick carried a sky coordinate).
    - ``wcs_refined`` — how many of the reprojected picks were additionally
      centroid-refined on the frame data before measuring (issue 12 — the
      solve WCS has a ~few-px pointing error); the per-star refinement
      offsets ride on the :class:`StarMeasurement`\\ s (``pick_offset``).
      0 when nothing was reprojected/refined (fixed-pixel path unchanged).
    - ``dropped_picks`` — issue 14 (per-PICK skip semantics): the
      :class:`DroppedPick`\\ s of THIS frame — picks that landed outside the
      image or gave a non-positive/non-finite flux and were dropped for this
      frame only (the frame was still measured on the remaining picks).
      Empty when every pick was measured.
    """

    jd: float
    target: StarMeasurement
    comps: tuple[StarMeasurement, ...]
    ensemble_mag: float
    mag: float
    mag_err: float
    check: StarMeasurement | None = None
    check_mag: float | None = None
    check_mag_err: float | None = None
    target_minus_check: float | None = None
    target_minus_check_err: float | None = None
    wcs_reprojected: int = 0
    wcs_refined: int = 0
    dropped_picks: tuple[DroppedPick, ...] = ()


def _header_jd(header: Mapping[str, Any] | None) -> float | None:
    """The JD of the FITS ``DATE-OBS`` header card (astropy ``Time.jd`` via
    :func:`seestar_controller.store.frames._date_obs_jd_utc`), or None when
    the header is missing/not a mapping/has no usable ``DATE-OBS``."""
    if not isinstance(header, Mapping):
        return None
    jd_utc = _date_obs_jd_utc(header.get("DATE-OBS"))
    return None if jd_utc is None else jd_utc[0]


def measure_frame_data(
    data: Any,
    header: Mapping[str, Any] | None,
    picks: Iterable[Any],
    aperture: ApertureParams | None = None,
    *,
    jd: float | None = None,
    wcs: Any = None,
) -> FrameMeasurement:
    """The pure photometry core on a raw frame: measure every pick on ``data``
    (a numpy array + FITS ``header`` mapping) and compute the differential
    magnitude against the comp ensemble.

    - ``picks`` — :class:`~.picking.StarPickSpec` or store
      :class:`~seestar_controller.store.stars.StarPick` records; the ensemble
      must contain exactly one target and at least one comp (a ``"check"``
      pick is optional, at most one, and is EXCLUDED from the comp ensemble);
    - ``aperture`` — the :class:`~.picking.ApertureParams` used for picks that
      do not carry their own (store picks do); None = the defaults;
    - ``jd`` — the fallback Julian Date when the header has no usable
      ``DATE-OBS``; the header ``DATE-OBS`` always wins when present. When
      neither yields a JD, :class:`MeasurementError` is raised.
    - ``wcs`` — the frame's WCS for the per-frame pick reprojection (issue
      09): None (default — fixed pixel picks), a
      :class:`~seestar_controller.store.wcs.FrameWcs` record (e.g. from
      :func:`seestar_controller.store.wcs.load_frame_wcs`), a mapping of FITS
      cards, or an astropy WCS (e.g. ``FrameWcs.to_wcs()``). Every pick that
      carries a sky coordinate is reprojected to this frame's pixel
      coordinates via :func:`~.picking.try_sky_to_pixel` and measured there;
      picks without a sky coordinate (or with a failed conversion) keep
      their fixed pixel. The count of reprojected picks is recorded on the
      result's ``wcs_reprojected``.
    - issue 12: every REPROJECTED pick is then centroid-refined on ``data``
      via :func:`refine_pick_on_frame` (a significant local maximum within
      :data:`REPROJECT_SEARCH_RADIUS_PX` of the predicted pixel, centroided
      within :data:`REPROJECT_CENTROID_RADIUS_PX` of it) before measuring —
      the solve WCS has a ~few-px pointing error, and an unrefined pick can
      miss the star core with the annulus contaminated by the star wings
      (non-positive flux → spurious skip). The refined position is measured,
      the per-star offsets ride on the ``StarMeasurement.pick_offset``\\ s and
      the refined count on the result's ``wcs_refined``. A reprojected pick
      with no significant local maximum near the predicted pixel keeps the
      predicted pixel (the normal skip logic applies). Fixed-pixel picks are
      never refined.
    - issue 15 (error model): every ``StarMeasurement.mag_err`` is the full
      model of :func:`measure_star` — photon/read noise plus the
      positional/centering term plus the small-aperture systematics floor. The
      positional uncertainty of each pick is: the centroid uncertainty + a
      residual fraction of the ``pick_offset`` for refined reprojected picks;
      the WCS match rms in px (:func:`_wcs_rms_px`, only for a
      :class:`~seestar_controller.store.wcs.FrameWcs` with ``rms_arcsec`` and
      a derivable pixel scale) for every pick of a frame whose WCS carries one;
      and :data:`DEFAULT_REPROJECT_ERR_PX` for reprojected picks that were NOT
      refined (measured at the WCS-predicted pixel) when no rms is available.

    Returns the :class:`FrameMeasurement` — with the check star's differential
    mag + error (``check_mag`` / ``check_mag_err``, same error propagation as
    the target) and the target-minus-check curve (``target_minus_check`` /
    ``target_minus_check_err``) when a check pick is present. Issue 14
    (per-PICK skip semantics): a pick that falls outside the image, whose
    sky annulus is not fully inside the image (kind
    ``"annulus-outside-image"``, issue 19), or whose flux is non-positive
    (possibly after the WCS reprojection/refinement) is DROPPED for this
    frame only — recorded as a :class:`DroppedPick` on the result's
    ``dropped_picks`` — and the frame is still measured when the target +
    at least one comp remain. Raises :class:`FrameSkippedError` only when
    the TARGET pick is unusable (the raise carries the target's drop
    ``kind`` plus all per-pick drops on ``dropped_picks``) or when no comp
    pick remains (``kind="no-comps"``).
    """
    norm = _normalize_picks(picks, aperture)
    norm, wcs_reprojected = resolve_pick_pixels(norm, wcs)
    plane = _as_2d_plane(data)
    wcs_refined = 0
    if wcs_reprojected:
        # issue 12: centroid-refine the reprojected picks on the frame data
        norm, wcs_refined = _refine_reprojected_picks(norm, plane)
    # issue 15: the frame WCS match rms (px) is the astrometric uncertainty of
    # every position in this frame — it enters every pick's mag_err
    wcs_err_px = _wcs_rms_px(wcs)
    stars: list[StarMeasurement] = []
    drops: list[DroppedPick] = []
    for p in norm:
        try:
            stars.append(
                measure_star(
                    plane,
                    p.x,
                    p.y,
                    p.aperture,
                    role=p.role,
                    label=p.label,
                    pick_offset=(
                        None
                        if p.refined_from is None
                        else (p.x - p.refined_from[0], p.y - p.refined_from[1])
                    ),
                    # issue 15: an unrefined reprojected pick is measured at
                    # the WCS-predicted pixel, whose pointing error is the WCS
                    # rms (or the default few-px plate-solve error when the
                    # WCS carries no rms)
                    positional_err_px=(
                        (
                            wcs_err_px
                            if wcs_err_px is not None
                            else DEFAULT_REPROJECT_ERR_PX
                        )
                        if p.reprojected and p.refined_from is None
                        else None
                    ),
                    wcs_err_px=wcs_err_px,
                )
            )
        except FrameSkippedError as exc:
            # issue 14: a per-pick failure drops ONLY this pick for this
            # frame (recorded with its reason + kind) — the frame may still
            # be measured on the remaining picks
            logger.info(
                "dropping pick %s (%s) at (%.2f, %.2f) for this frame: %s",
                p.label or p.role,
                p.role,
                p.x,
                p.y,
                exc,
            )
            drops.append(
                DroppedPick(role=p.role, label=p.label, reason=str(exc), kind=_skip_kind(exc))
            )
    target = next((s for s in stars if s.role == ROLE_TARGET), None)
    comps = tuple(s for s in stars if s.role == ROLE_COMP)
    check = next((s for s in stars if s.role == ROLE_CHECK), None)
    if target is None:
        # issue 14: the frame is skipped only when the TARGET pick is
        # unusable — the frame skip carries the target's drop kind + reason
        drop = next(d for d in drops if d.role == ROLE_TARGET)
        raise FrameSkippedError(
            f"the target pick {drop.label or drop.role!r} is unusable on this "
            f"frame: {drop.reason}",
            kind=drop.kind,
            dropped_picks=drops,
        )
    if not comps:
        # issue 14: no comp left -> no comp ensemble -> the frame is skipped
        raise FrameSkippedError(
            "no usable comp pick remains on this frame (the comp ensemble "
            "needs at least one comp): "
            + "; ".join(f"{d.label or d.role} [{d.kind}]" for d in drops),
            kind="no-comps",
            dropped_picks=drops,
        )
    # the comp ensemble: the mean of the COMPS only — the check star is
    # excluded (check-star-plot ticket 01)
    ens = ensemble_mag([s.mag for s in comps])
    mag = differential_mag(target.mag, ens)
    mag_err = differential_mag_err(target.mag_err, [s.mag_err for s in comps])
    check_mag: float | None = None
    check_mag_err: float | None = None
    target_minus_check: float | None = None
    target_minus_check_err: float | None = None
    if check is not None:
        # the check star is measured EXACTLY like the target: same ensemble,
        # same error propagation
        check_mag = differential_mag(check.mag, ens)
        check_mag_err = differential_mag_err(check.mag_err, [s.mag_err for s in comps])
        # target minus check: the ensemble cancels, so the error is the
        # quadrature of the two star mag errors
        target_minus_check = differential_mag(target.mag, check.mag)
        target_minus_check_err = math.sqrt(target.mag_err**2 + check.mag_err**2)
    jd_value = _header_jd(header)
    if jd_value is None:
        if jd is None:
            raise MeasurementError(
                "no JD: the FITS header has no usable DATE-OBS and no fallback "
                "jd was given",
                kind="no-jd",
            )
        jd_value = _finite_float(jd, "jd")
    return FrameMeasurement(
        jd=jd_value,
        target=target,
        comps=comps,
        ensemble_mag=ens,
        mag=mag,
        mag_err=mag_err,
        check=check,
        check_mag=check_mag,
        check_mag_err=check_mag_err,
        target_minus_check=target_minus_check,
        target_minus_check_err=target_minus_check_err,
        wcs_reprojected=wcs_reprojected,
        wcs_refined=wcs_refined,
        dropped_picks=tuple(drops),
    )


# --- stored frames --------------------------------------------------------------------------


@dataclass(frozen=True)
class SkippedFrame:
    """One frame that was skipped during :func:`measure_project` /
    :meth:`~seestar_controller.photometry.run.RunController.run_session`:
    ``frame_id`` + the human-readable ``reason`` + a short machine-readable
    ``kind`` (issue 09) aggregating the skip cause — the ``kind`` attribute of
    the raised :class:`MeasurementError` (e.g. ``"outside-image"``,
    ``"annulus-outside-image"`` (issue 19), ``"non-positive-flux"``,
    ``"unreadable-frame"``, ``"store-failed"``), or
    ``"error"`` when the error carried no kind. Issue 14: ``dropped_picks``
    carries the frame's per-pick drops (the ``dropped_picks`` attribute of the
    raised error) — empty when the failure happened before any pick was
    measured (e.g. an unreadable frame)."""

    frame_id: int
    reason: str
    kind: str = ""
    dropped_picks: tuple[DroppedPick, ...] = ()


def _skip_kind(exc: BaseException) -> str:
    """The aggregation kind of a per-frame failure: the error's ``kind``
    attribute when present, else ``"error"``."""
    kind = getattr(exc, "kind", "")
    return kind if isinstance(kind, str) and kind else "error"


def _skip_drops(exc: BaseException) -> tuple[DroppedPick, ...]:
    """The per-pick drops of a frame-level failure (issue 14): the error's
    ``dropped_picks`` attribute when present, else empty."""
    drops = getattr(exc, "dropped_picks", None)
    return tuple(drops) if drops else ()


def aggregate_pick_skips(drops: Iterable[DroppedPick]) -> dict[str, int]:
    """Aggregate per-pick drops (issue 14) to ``"pick-<kind>" -> count`` —
    e.g. ``{"pick-outside-image": 146}`` — so pick-level drops are
    distinguishable from the frame-level skip reasons (``skip_reasons``)."""
    counts: dict[str, int] = {}
    for drop in drops:
        key = f"pick-{drop.kind or 'error'}"
        counts[key] = counts.get(key, 0) + 1
    return counts


def aggregate_pick_skips_by_label(
    drops: Iterable[DroppedPick],
) -> dict[str, dict[str, int]]:
    """Aggregate per-pick drops (issue 14) per pick label:
    ``{label-or-role: {"pick-<kind>": count}}`` — e.g.
    ``{"Comp C": {"pick-outside-image": 138}}``."""
    result: dict[str, dict[str, int]] = {}
    for drop in drops:
        label = drop.label or drop.role
        kind = f"pick-{drop.kind or 'error'}"
        per_label = result.setdefault(label, {})
        per_label[kind] = per_label.get(kind, 0) + 1
    return result


def format_pick_skip_summary(drops: Iterable[DroppedPick]) -> str:
    """A human-readable one-line pick-drop summary (issue 14):
    ``"146 pick drops (pick-outside-image×146 — Comp C×138, Comp D×8)"``, or
    ``"no pick drops"``."""
    drops = tuple(drops)
    if not drops:
        return "no pick drops"
    n = len(drops)
    text = f"{n} pick drop{'s' if n != 1 else ''}"
    reasons = ", ".join(
        f"{kind}×{count}"
        for kind, count in sorted(aggregate_pick_skips(drops).items())
        if count > 0
    )
    if reasons:
        text += f" ({reasons}"
        label_counts: dict[str, int] = {}
        for drop in drops:
            label = drop.label or drop.role
            label_counts[label] = label_counts.get(label, 0) + 1
        labels = ", ".join(
            f"{label}×{count}"
            for label, count in sorted(label_counts.items())
            if count > 0
        )
        if labels:
            text += f" — {labels}"
        text += ")"
    return text


@dataclass(frozen=True)
class MeasureReport:
    """The result of :func:`measure_project`: the stored measurements plus the
    skipped frames (in frame order).

    - ``measurements`` — the stored
      :class:`~seestar_controller.store.measurements.Measurement`\\ s;
    - ``skipped`` — the :class:`SkippedFrame`\\ s;
    - ``frame_results`` — the full per-frame :class:`FrameMeasurement`\\ s of
      the measured frames (in frame order, aligned with ``measurements``) —
      they carry the check star's differential mag + error and the
      target-minus-check curve (check-star-plot ticket 01) plus the
      ``wcs_reprojected`` count of the per-frame WCS reprojection (issue 09)
      and the ``wcs_refined`` count of the centroid refinement of the
      reprojected picks on the frame data (issue 12) and the per-pick drops
      of the frame (issue 14, ``dropped_picks``).
    - issue 14: ``dropped_picks`` / ``pick_skip_reasons`` /
      ``pick_skip_reasons_by_label`` / ``pick_skip_summary`` aggregate the
      per-PICK drops (over the measured frames' ``frame_results`` AND the
      skipped frames' ``dropped_picks``) separately from the frame-level
      ``skip_reasons`` — keys are prefixed ``pick-`` (e.g.
      ``{"pick-outside-image": 146}``).
    """

    measurements: tuple[Measurement, ...]
    skipped: tuple[SkippedFrame, ...]
    frame_results: tuple[FrameMeasurement, ...] = ()

    @property
    def frames(self) -> int:
        """The total number of sub-frames processed (measured + skipped)."""
        return len(self.measurements) + len(self.skipped)

    @property
    def skip_reasons(self) -> dict[str, int]:
        """The aggregated skip reasons (issue 09): ``kind -> count`` over the
        skipped frames (a skip without a kind counts under ``"error"``)."""
        counts: dict[str, int] = {}
        for skipped in self.skipped:
            kind = skipped.kind or "error"
            counts[kind] = counts.get(kind, 0) + 1
        return counts

    @property
    def skip_summary(self) -> str:
        """A human-readable one-line skip summary: ``"2 frames skipped
        (non-positive-flux×2)"``, ``"1 frame skipped (outside-image×1)"``, or
        ``"no frames skipped"``."""
        count = len(self.skipped)
        if count == 0:
            return "no frames skipped"
        text = f"{count} frame{'s' if count != 1 else ''} skipped"
        reasons = ", ".join(
            f"{kind}×{n}" for kind, n in sorted(self.skip_reasons.items()) if n > 0
        )
        if reasons:
            text += f" ({reasons})"
        return text

    @property
    def wcs_frames(self) -> int:
        """The measured frames with at least one WCS-reprojected pick
        (issue 09)."""
        return sum(1 for fm in self.frame_results if fm.wcs_reprojected > 0)

    @property
    def pixel_frames(self) -> int:
        """The measured frames measured on fixed pixel picks (no WCS or no
        sky-carrying picks — issue 09)."""
        return sum(1 for fm in self.frame_results if fm.wcs_reprojected == 0)

    @property
    def refined_frames(self) -> int:
        """The measured frames with at least one WCS-reprojected pick
        centroid-refined on the frame data (issue 12)."""
        return sum(1 for fm in self.frame_results if fm.wcs_refined > 0)

    @property
    def dropped_picks(self) -> tuple[DroppedPick, ...]:
        """All per-pick drops of the run (issue 14): over the measured
        frames' ``frame_results`` and the skipped frames' ``dropped_picks``."""
        return tuple(
            drop
            for fm in self.frame_results
            for drop in fm.dropped_picks
        ) + tuple(drop for s in self.skipped for drop in s.dropped_picks)

    @property
    def pick_skip_reasons(self) -> dict[str, int]:
        """The aggregated per-PICK drop reasons (issue 14), DISTINCT from the
        frame-level ``skip_reasons``: ``"pick-<kind>" -> count`` over all
        dropped picks — e.g. ``{"pick-outside-image": 146}``."""
        return aggregate_pick_skips(self.dropped_picks)

    @property
    def pick_skip_reasons_by_label(self) -> dict[str, dict[str, int]]:
        """The per-pick drop reasons broken down per pick label (issue 14):
        ``{label-or-role: {"pick-<kind>": count}}``."""
        return aggregate_pick_skips_by_label(self.dropped_picks)

    @property
    def pick_skip_summary(self) -> str:
        """A human-readable one-line pick-drop summary (issue 14):
        ``"146 pick drops (pick-outside-image×146 — Comp C×138, Comp D×8)"``,
        or ``"no pick drops"``."""
        return format_pick_skip_summary(self.dropped_picks)


def _read_frame_fits(project: Any, frame: Any) -> tuple[Any, dict[str, Any]]:
    """Read the stored FITS of ``frame``: ``(data, header)`` (astropy, lazy).
    The header is a plain dict with sanitized values
    (:func:`seestar_controller.store.frames.header_to_dict`) — real Seestar
    FITS carry consecutive ``COMMENT`` cards whose joined value contains
    ``\\n``, which astropy rejects when re-inserted into a Header.
    Raises :class:`MeasurementError` when the file is unreadable or not a
    FITS with data."""
    from astropy.io import fits as pyfits

    try:
        fits_bytes = read_frame_bytes(project, frame)
    except ProjectError as exc:
        raise MeasurementError(
            f"cannot read frame {frame.id} of project {project.slug!r}: {exc}",
            kind="unreadable-frame",
        ) from exc
    try:
        with pyfits.open(io.BytesIO(fits_bytes), memmap=False) as hdul:
            data = hdul[0].data
            header = header_to_dict(hdul[0].header)
    except Exception as exc:
        raise MeasurementError(
            f"frame {frame.id} of project {project.slug!r} is not a readable "
            f"FITS file: {exc}",
            kind="unreadable-frame",
        ) from exc
    if data is None:
        raise MeasurementError(
            f"frame {frame.id} of project {project.slug!r} has no image data",
            kind="unreadable-frame",
        )
    return data, header


#: Sentinel for :func:`_measure_frame_full`'s ``frame_wcs`` argument: load the
#: frame's stored WCS from the project db (:func:`load_frame_wcs`).
_LOAD_WCS = object()


def _load_frame_wcs_lenient(project: Any, frame_id: int) -> FrameWcs | None:
    """The frame's stored WCS record (issue 09), or None when it has none or
    the record cannot be read — a WCS problem never skips a frame by itself,
    the fixed pixel picks are used then (logged)."""
    try:
        return load_frame_wcs(project, frame_id)
    except (WcsStoreError, ProjectError) as exc:
        logger.warning(
            "cannot load the stored WCS of frame %s (%s) — using the fixed "
            "pixel picks for it",
            frame_id,
            exc,
        )
        return None


def _measure_frame_full(
    project: Any,
    frame: Any,
    picks: Iterable[Any],
    aperture: ApertureParams | None,
    *,
    frame_wcs: Any = _LOAD_WCS,
) -> tuple[Measurement, FrameMeasurement]:
    """Measure one already-located stored ``frame`` and store the measurement
    via :func:`seestar_controller.store.measurements.add_measurement`.

    - ``frame_wcs`` — the frame's WCS for the per-frame pick reprojection
      (issue 09): a :class:`~seestar_controller.store.wcs.FrameWcs` record
      (or its cards mapping / an astropy WCS), None for fixed pixel picks, or
      the default sentinel to load the frame's stored WCS from the project db
      via :func:`load_frame_wcs` (a WCS read failure falls back to None).
      The reprojected picks are centroid-refined on the frame data before
      measuring (issue 12).

    Returns ``(measurement, frame_measurement)`` — the stored
    :class:`~seestar_controller.store.measurements.Measurement` and the full
    :class:`FrameMeasurement` (which carries the check-star curves, the
    ``wcs_reprojected`` count and the ``wcs_refined`` refinement count)."""
    data, header = _read_frame_fits(project, frame)
    if frame_wcs is _LOAD_WCS:
        frame_wcs = _load_frame_wcs_lenient(project, frame.id)
    fm = measure_frame_data(
        data, header, picks, aperture=aperture, jd=frame.jd, wcs=frame_wcs
    )
    try:
        measurement = add_measurement(project, frame.id, fm.mag, jd=fm.jd, mag_err=fm.mag_err)
    except StoreMeasurementError as exc:
        raise MeasurementError(
            f"could not store the measurement for frame {frame.id} of project "
            f"{project.slug!r}: {exc}",
            kind="store-failed",
        ) from exc
    return measurement, fm


def _measure_frame_obj(
    project: Any,
    frame: Any,
    picks: Iterable[Any],
    aperture: ApertureParams | None,
    *,
    frame_wcs: Any = _LOAD_WCS,
) -> Measurement:
    """Measure one already-located stored ``frame`` and return only the stored
    :class:`~seestar_controller.store.measurements.Measurement` (see
    :func:`_measure_frame_full`)."""
    return _measure_frame_full(project, frame, picks, aperture, frame_wcs=frame_wcs)[0]


def measure_frame(
    project: Any,
    frame_id: int,
    picks: Iterable[Any],
    aperture: ApertureParams | None = None,
) -> Measurement:
    """Measure ONE stored frame of ``project`` and return the stored
    :class:`~seestar_controller.store.measurements.Measurement`.

    - ``frame_id`` — must exist in the project, else :class:`MeasurementError`;
    - ``picks`` / ``aperture`` — see :func:`measure_frame_data`;
    - the JD comes from the frame's FITS ``DATE-OBS`` header, falling back to
      the frame's stored ``jd`` when the header has none;
    - issue 09: when the frame has a stored WCS
      (:func:`seestar_controller.store.wcs.load_frame_wcs`), picks that carry
      a sky coordinate are reprojected onto it and measured at the
      frame-corrected pixel; otherwise the fixed pixel picks are used;
    - issue 12: the reprojected picks are additionally centroid-refined on
      the frame data (:func:`refine_pick_on_frame`) before measuring;
    - the differential mag + error are stored via
      :func:`seestar_controller.store.measurements.add_measurement` (store
      errors wrapped into :class:`MeasurementError`).

    Raises :class:`FrameSkippedError` when the target pick is unusable (a
    possibly reprojected/refined target pick falls outside the image, its
    sky annulus is not fully inside the image (kind
    ``"annulus-outside-image"``, issue 19), or its flux is non-positive) or
    when no comp pick remains (issue 14).
    """
    if isinstance(frame_id, bool) or not isinstance(frame_id, int):
        raise MeasurementError(f"frame_id must be an int, got {frame_id!r}")
    picks = list(picks)  # materialize: validated once, measured once
    frame = next((f for f in list_frames(project) if f.id == frame_id), None)
    if frame is None:
        raise MeasurementError(
            f"project {project.slug!r} has no frame {frame_id!r}"
        )
    return _measure_frame_obj(project, frame, picks, aperture)


def measure_project(
    project: Any,
    picks: Iterable[Any],
    aperture: ApertureParams | None = None,
) -> MeasureReport:
    """Run aperture photometry over ALL **sub-frames** of ``project``
    (``store.frames`` ``kind="sub"`` only — stacks and enhanced images are
    NEVER measured) and return the :class:`MeasureReport`.

    - ``picks`` / ``aperture`` — see :func:`measure_frame_data`; the ensemble
      is validated ONCE up front (a bad ensemble raises
      :class:`MeasurementError` before any frame is read). A convenient
      source of picks is :func:`seestar_controller.store.stars.load_star_picks`
      (store records carry their own per-pick aperture);
    - issue 09: every sub-frame that has a stored WCS
      (:func:`seestar_controller.store.wcs.load_frame_wcs`) gets its
      sky-carrying picks reprojected onto that frame's pixel coordinates
      before measuring; frames without a WCS (or picks without sky
      coordinates) are measured on the fixed pixel picks. The per-frame
      ``wcs_reprojected`` count rides on the ``frame_results``, and the
      report aggregates WCS-reprojected vs fixed-pixel frames
      (``wcs_frames`` / ``pixel_frames``);
    - issue 12: the reprojected picks are additionally centroid-refined on
      each frame's data before measuring (the solve WCS has a ~few-px
      pointing error); the per-frame ``wcs_refined`` count rides on the
      ``frame_results`` and the report aggregates refined frames
      (``refined_frames``);
    - every sub-frame is measured via :func:`measure_frame`; issue 14
      (per-PICK skip semantics): a pick that falls outside the image, whose
      sky annulus is not fully inside the image (kind
      ``"annulus-outside-image"``, issue 19), or whose flux is non-positive
      is dropped for that frame only (recorded on the frame result's
      ``dropped_picks``) and the frame is still measured when the target +
      at least one comp remain; the frame itself is recorded as a
      :class:`SkippedFrame` (with the skip ``kind``, aggregated on
      ``skip_reasons``, and its per-pick drops on ``dropped_picks``,
      aggregated as ``pick_skip_reasons``) only when the target is unusable
      or no comp remains — or when the frame cannot be read at all. Never a
      crash.
    """
    picks = list(picks)  # materialize: validated once, then measured per frame
    _normalize_picks(picks, aperture)  # validate the ensemble up front (raises)
    measurements: list[Measurement] = []
    frame_results: list[FrameMeasurement] = []
    skipped: list[SkippedFrame] = []
    for frame in list_frames(project, kind=KIND_SUB):
        try:
            measurement, fm = _measure_frame_full(project, frame, picks, aperture)
        except MeasurementError as exc:
            logger.info("skipping sub-frame %s: %s", frame.id, exc)
            skipped.append(
                SkippedFrame(
                    frame_id=frame.id,
                    reason=str(exc),
                    kind=_skip_kind(exc),
                    dropped_picks=_skip_drops(exc),  # issue 14
                )
            )
        else:
            measurements.append(measurement)
            frame_results.append(fm)
    return MeasureReport(
        measurements=tuple(measurements),
        skipped=tuple(skipped),
        frame_results=tuple(frame_results),
    )
