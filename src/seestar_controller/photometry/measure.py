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
  header has no usable ``DATE-OBS``);
- :func:`measure_frame` — one stored frame of a project →
  :class:`~seestar_controller.store.measurements.Measurement` via
  :func:`seestar_controller.store.measurements.add_measurement` (JD from the
  frame's ``DATE-OBS``, falling back to the frame's stored ``jd``);
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
- **Pixel convention: 0-based, ``x`` = column, ``y`` = row** (same as
  :mod:`.picking` and the numpy ``data[y, x]`` array). photutils apertures take
  0-based ``(x, y)`` positions directly.
- **Skip conditions** (per frame, recorded by :func:`measure_project`, raised
  as :class:`FrameSkippedError` by :func:`measure_frame` /
  :func:`measure_frame_data`): a pick center outside the image, or a
  background-subtracted flux that is non-positive or non-finite. A frame
  that cannot be read/parsed at all raises :class:`MeasurementError` (which
  :func:`measure_project` also records as skipped — a bad frame never kills
  the run).
- **JD** comes from the FITS header ``DATE-OBS`` (parsed with
  :mod:`astropy.time` via
  :func:`seestar_controller.store.frames._date_obs_jd_utc`); when the header
  has no usable ``DATE-OBS``, :func:`measure_frame` falls back to the frame's
  stored ``jd`` (the store's own DATE-OBS/mtime derivation).
- **Error array** for photutils: Poisson-like ``sqrt(clip(data, 1, None))``
  over the finite pixels (non-finite pixels are masked out), so
  ``aperture_sum_err`` is always finite and positive for a real star.
- **RGB frames** are reduced to their channel mean first (same as the
  picking snap); anything that is not 2-D after that raises
  :class:`MeasurementError`.
"""

from __future__ import annotations

import io
import logging
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from seestar_controller.store.errors import (
    MeasurementError as StoreMeasurementError,
)
from seestar_controller.store.errors import ProjectError
from seestar_controller.store.frames import (
    KIND_SUB,
    _date_obs_jd_utc,
    header_to_dict,
    list_frames,
    read_frame_bytes,
)
from seestar_controller.store.measurements import Measurement, add_measurement
from seestar_controller.store.stars import ROLE_CHECK, ROLE_COMP, ROLE_TARGET, StarPick

from .picking import ApertureParams, PhotometryError, StarPickSpec

logger = logging.getLogger(__name__)


class MeasurementError(PhotometryError):
    """Any photometry-measurement failure — a :class:`~.picking.PhotometryError`
    (a :class:`ValueError`) with a clear message: a bad pick ensemble (not
    exactly one target / no comps), a bad aperture, an unknown frame, an
    unreadable frame file, a missing JD, or a store failure while saving the
    measurement (the store's
    :class:`~seestar_controller.store.errors.MeasurementError` is wrapped and
    chained — this class is deliberately distinct from it)."""


class FrameSkippedError(MeasurementError):
    """A frame was skipped: a pick falls outside the image, or the
    background-subtracted flux is non-positive/non-finite. Carries the
    human-readable ``reason``; :func:`measure_project` records these as
    :class:`SkippedFrame` entries instead of crashing."""


#: The magnitude-error coefficient ``2.5 / ln(10) ≈ 1.0857``:
#: ``d(-2.5 log10 f) = -2.5/ln(10) * df/f``.
MAG_ERR_COEFF = 2.5 / math.log(10.0)


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
    - ``mag_err`` — ``MAG_ERR_COEFF * flux_err / flux``.
    """

    role: str
    label: str | None
    x: float
    y: float
    flux: float
    flux_err: float
    mag: float
    mag_err: float


def _as_2d_plane(data: Any) -> np.ndarray:
    """Coerce ``data`` to a finite-masked 2-D float64 plane (RGB images are
    reduced to their channel mean); raise :class:`MeasurementError` when it
    is not image-like."""
    arr = np.asarray(data, dtype=np.float64)
    if arr.ndim == 3:
        arr = arr[..., :3].mean(axis=2)
    if arr.ndim != 2 or arr.size == 0:
        raise MeasurementError(
            f"frame data must be a 2-D image array, got shape {arr.shape!r}"
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
) -> StarMeasurement:
    """Measure ONE star at 0-based pixel ``(x, y)`` with circular-aperture +
    sky-annulus photometry (photutils, ``method="exact"``).

    - the pick center must lie inside the image, else
      :class:`FrameSkippedError`;
    - the background is the annulus mean (``aperture_sum / annulus.area``)
      times the aperture area, subtracted from the aperture sum;
    - ``flux_err`` = ``sqrt(aperture_sum_err^2 + (ap_area * annulus_sum_err /
      annulus_area)^2)`` — both photutils errors in quadrature;
    - the flux error array is Poisson-like ``sqrt(clip(data, 1, None))`` over
      the finite pixels (non-finite pixels are masked);
    - a non-positive or non-finite background-subtracted flux raises
      :class:`FrameSkippedError` (no magnitude exists then).

    Returns the :class:`StarMeasurement`.
    """
    if not isinstance(aperture, ApertureParams):
        raise MeasurementError(
            f"aperture must be an ApertureParams, got {aperture!r}"
        )
    px = _finite_float(x, "x")
    py = _finite_float(y, "y")
    plane = _as_2d_plane(data)
    height, width = int(plane.shape[0]), int(plane.shape[1])
    if not (0.0 <= px < width and 0.0 <= py < height):
        raise FrameSkippedError(
            f"pick ({px}, {py}) falls outside the {width}x{height} image"
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
            f"(aperture sum {ap_sum!r}, background {bkg_mean!r})"
        )
    if not math.isfinite(flux_err) or flux_err < 0.0:
        raise FrameSkippedError(
            f"invalid flux error {flux_err!r} at pick ({px}, {py})"
        )
    return StarMeasurement(
        role=role,
        label=label,
        x=px,
        y=py,
        flux=flux,
        flux_err=flux_err,
        mag=flux_to_mag(flux),
        mag_err=flux_err_to_mag_err(flux, flux_err),
    )


# --- picks normalization -----------------------------------------------------------------


@dataclass(frozen=True)
class _NormPick:
    """One normalized pick: role/label/pixel + the aperture to measure with."""

    role: str
    label: str | None
    x: float
    y: float
    aperture: ApertureParams


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
            f"aperture must be an ApertureParams or None, got {aperture!r}"
        )
    if isinstance(picks, (str, bytes)) or not isinstance(picks, Iterable):
        raise MeasurementError(
            f"picks must be an iterable of StarPickSpec/StarPick, got {picks!r}"
        )
    norm: list[_NormPick] = []
    for pick in picks:
        if isinstance(pick, (StarPickSpec, StarPick)):
            role, label, x, y = pick.role, pick.label, pick.x, pick.y
        else:
            raise MeasurementError(
                f"picks must contain only StarPickSpec or StarPick records, got {pick!r}"
            )
        if role not in (ROLE_TARGET, ROLE_COMP, ROLE_CHECK):
            raise MeasurementError(
                f"pick role must be 'target', 'comp' or 'check', got {role!r}"
            )
        norm.append(
            _NormPick(
                role=role,
                label=label,
                x=_finite_float(x, "pick x"),
                y=_finite_float(y, "pick y"),
                aperture=_aperture_of(pick, aperture),
            )
        )
    targets = [p for p in norm if p.role == ROLE_TARGET]
    comps = [p for p in norm if p.role == ROLE_COMP]
    checks = [p for p in norm if p.role == ROLE_CHECK]
    if len(targets) != 1:
        raise MeasurementError(
            f"the pick ensemble must contain exactly one 'target' pick, got "
            f"{len(targets)} (comps: {len(comps)}, checks: {len(checks)})"
        )
    if not comps:
        raise MeasurementError(
            "the pick ensemble needs at least one 'comp' pick (the comp ensemble)"
        )
    if len(checks) > 1:
        raise MeasurementError(
            f"the pick ensemble may contain at most one 'check' pick, got "
            f"{len(checks)}"
        )
    return tuple(norm)


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
    - ``mag_err`` — ``sqrt(target.mag_err^2 + mean(comp.mag_err^2))``;
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
      check star).
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

    Returns the :class:`FrameMeasurement` — with the check star's differential
    mag + error (``check_mag`` / ``check_mag_err``, same error propagation as
    the target) and the target-minus-check curve (``target_minus_check`` /
    ``target_minus_check_err``) when a check pick is present. Raises
    :class:`FrameSkippedError` when a pick falls outside the image or a flux
    is non-positive.
    """
    norm = _normalize_picks(picks, aperture)
    plane = _as_2d_plane(data)
    stars = [
        measure_star(plane, p.x, p.y, p.aperture, role=p.role, label=p.label)
        for p in norm
    ]
    target = next(s for s in stars if s.role == ROLE_TARGET)
    comps = tuple(s for s in stars if s.role == ROLE_COMP)
    check = next((s for s in stars if s.role == ROLE_CHECK), None)
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
                "jd was given"
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
    )


# --- stored frames --------------------------------------------------------------------------


@dataclass(frozen=True)
class SkippedFrame:
    """One frame that was skipped during :func:`measure_project`:
    ``frame_id`` + the human-readable ``reason``."""

    frame_id: int
    reason: str


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
      target-minus-check curve (check-star-plot ticket 01).
    """

    measurements: tuple[Measurement, ...]
    skipped: tuple[SkippedFrame, ...]
    frame_results: tuple[FrameMeasurement, ...] = ()

    @property
    def frames(self) -> int:
        """The total number of sub-frames processed (measured + skipped)."""
        return len(self.measurements) + len(self.skipped)


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
            f"cannot read frame {frame.id} of project {project.slug!r}: {exc}"
        ) from exc
    try:
        with pyfits.open(io.BytesIO(fits_bytes), memmap=False) as hdul:
            data = hdul[0].data
            header = header_to_dict(hdul[0].header)
    except Exception as exc:
        raise MeasurementError(
            f"frame {frame.id} of project {project.slug!r} is not a readable "
            f"FITS file: {exc}"
        ) from exc
    if data is None:
        raise MeasurementError(
            f"frame {frame.id} of project {project.slug!r} has no image data"
        )
    return data, header


def _measure_frame_full(
    project: Any, frame: Any, picks: Iterable[Any], aperture: ApertureParams | None
) -> tuple[Measurement, FrameMeasurement]:
    """Measure one already-located stored ``frame`` and store the measurement
    via :func:`seestar_controller.store.measurements.add_measurement`.

    Returns ``(measurement, frame_measurement)`` — the stored
    :class:`~seestar_controller.store.measurements.Measurement` and the full
    :class:`FrameMeasurement` (which carries the check-star curves)."""
    data, header = _read_frame_fits(project, frame)
    fm = measure_frame_data(data, header, picks, aperture=aperture, jd=frame.jd)
    try:
        measurement = add_measurement(project, frame.id, fm.mag, jd=fm.jd, mag_err=fm.mag_err)
    except StoreMeasurementError as exc:
        raise MeasurementError(
            f"could not store the measurement for frame {frame.id} of project "
            f"{project.slug!r}: {exc}"
        ) from exc
    return measurement, fm


def _measure_frame_obj(
    project: Any, frame: Any, picks: Iterable[Any], aperture: ApertureParams | None
) -> Measurement:
    """Measure one already-located stored ``frame`` and return only the stored
    :class:`~seestar_controller.store.measurements.Measurement` (see
    :func:`_measure_frame_full`)."""
    return _measure_frame_full(project, frame, picks, aperture)[0]


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
    - the differential mag + error are stored via
      :func:`seestar_controller.store.measurements.add_measurement` (store
      errors wrapped into :class:`MeasurementError`).

    Raises :class:`FrameSkippedError` when a pick falls outside the image or
    a flux is non-positive.
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
    - every sub-frame is measured via :func:`measure_frame`; a frame whose
      target/comp falls outside the image or whose flux is non-positive (or
      that cannot be read at all) is recorded as a :class:`SkippedFrame` —
      never a crash.
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
            skipped.append(SkippedFrame(frame_id=frame.id, reason=str(exc)))
        else:
            measurements.append(measurement)
            frame_results.append(fm)
    return MeasureReport(
        measurements=tuple(measurements),
        skipped=tuple(skipped),
        frame_results=tuple(frame_results),
    )
