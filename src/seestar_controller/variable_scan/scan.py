"""The pure variable-scan core — per-field-star lightcurves + ranking.

Pure Python: **no ``gi`` import** (a subprocess test enforces the
boundary) — stdlib + numpy (via :mod:`seestar_controller.photometry`) +
:mod:`seestar_controller.store` only. The heavy third-party imports
(:mod:`astropy.io.fits`) are lazy inside :func:`_read_frame`, the same
pattern as :mod:`seestar_controller.photometry.measure`.

What lives here (variable-scan ticket 01, ``.scratch/variable-scan``):

- :func:`scan_project` — the scan: take the Gaia cross-match source list of
  a stack (the latest cross-matched stack by default) as the field-star
  picks, run the existing aperture-photometry pipeline
  (:func:`seestar_controller.photometry.measure.measure_star`) over ALL
  sub-frames of ALL sessions of the project, build a differential
  lightcurve per field star (against the picked comp ensemble, falling
  back to the leave-one-out field median when no comps are picked), compute
  the robust scatter (MAD) and the median photometric error per star, and
  rank the variable candidates — the picked target/comps/check are
  EXCLUDED from the scan. The results are persisted into the project db
  (see :mod:`.persist`) so they accumulate across nights.
- the records: :class:`FieldStar`, :class:`LightcurvePoint`,
  :class:`StarScanResult`, :class:`SkippedScanFrame`, :class:`ScanReport`.

Documented choices:

- **Source list**: the cross-match table of ONE stack — the latest
  cross-matched stack (``list_crossmatched_stacks``, max frame id) unless
  ``stack_frame_id`` is given. Pixel positions of a stack apply to the
  sub-frames (the stack is the aligned sum of its session's subs).
- **Exclusion**: a cross-matched source within ``exclude_radius_px``
  (default :data:`DEFAULT_EXCLUDE_RADIUS_PX`) of any picked
  target/comp/check pixel position is skipped — those stars are already
  handled by the regular photometry.
- **Differential**: per frame, the comp ensemble is the mean instrumental
  mag of the picked comps measured on that frame (comps only — the check
  star is never in the ensemble, same as photometry); each field star's
  differential point is ``star.mag - ensemble`` with error
  ``sqrt(star_err^2 + ensemble_err^2)`` (the same propagation as
  :func:`~seestar_controller.photometry.measure.differential_mag_err`).
  When NO comps are picked (or none could be measured on a frame), the
  fallback ensemble is the **leave-one-out field median**: the median of
  the OTHER field stars' instrumental mags on that frame.
- **Ranking**: per star, ``mad`` = raw MAD of the differential mags,
  ``median_err`` = median of the point errors,
  ``variability`` = ``mad / median_err`` (see :func:`.stats.variability_index`);
  a star is a **candidate** (``variable``) when it has at least
  ``min_frames`` points AND ``variability >= threshold``. All scanned
  stars get a ``rank`` (1 = most variable) by variability descending.
- **Skips never crash**: an unreadable frame is recorded in
  ``skipped_frames``; a star outside the image or with non-positive flux
  on a frame simply gets no point for that frame (the photometry
  :class:`~seestar_controller.photometry.measure.FrameSkippedError`).
- **Persistence**: :func:`scan_project` appends one ``scan_results`` row
  per scanned star (one transaction) via :func:`.persist.save_scan_results`
  — re-running accumulates runs (keyed by ``scanned_at``); query them back
  with :func:`.persist.scan_results`.
"""

from __future__ import annotations

import io
import logging
import math
import statistics
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from seestar_controller.photometry.measure import (
    FrameSkippedError,
    MeasurementError,
    ensemble_mag,
    ensemble_mag_err,
    measure_star,
)
from seestar_controller.photometry.picking import ApertureParams
from seestar_controller.store.crossmatch import (
    list_crossmatched_stacks,
    load_crossmatch,
)
from seestar_controller.store.errors import ProjectError
from seestar_controller.store.frames import (
    KIND_STACK,
    KIND_SUB,
    _date_obs_jd_utc,
    header_to_dict,
    list_frames,
    read_frame_bytes,
)
from seestar_controller.store.project import Project
from seestar_controller.store.stars import (
    ROLE_CHECK,
    ROLE_COMP,
    ROLE_TARGET,
    load_star_picks,
)

from .errors import ScanError
from .settings import (
    DEFAULT_MIN_FRAMES,
    _check_project,
    load_scan_threshold,
    validate_threshold,
)
from .stats import mad, median, variability_index

logger = logging.getLogger(__name__)

#: Default radius (pixels) around a picked target/comp/check position within
#: which a cross-matched source is excluded from the scan.
DEFAULT_EXCLUDE_RADIUS_PX = 8.0

#: Ensemble mode: differential against the picked comp ensemble.
ENSEMBLE_COMPS = "comps"

#: Ensemble mode fallback: differential against the leave-one-out field
#: median (used when no comps are picked).
ENSEMBLE_FIELD_MEDIAN = "field_median"


@dataclass(frozen=True)
class FieldStar:
    """One cross-matched field star to scan (a crossmatch source).

    - ``source_id`` — the matched Gaia DR3 source id, or None when the
      detection is unmatched;
    - ``ra_deg`` / ``dec_deg`` — the J2000 sky position (degrees);
    - ``x`` / ``y`` — the 0-based pixel position in the stack (x = column,
      y = row);
    - ``matched`` — whether a Gaia row matched.
    """

    source_id: int | None
    ra_deg: float
    dec_deg: float
    x: float
    y: float
    matched: bool


@dataclass(frozen=True)
class LightcurvePoint:
    """One point of a field star's differential lightcurve.

    - ``jd`` — the frame time as Julian Date;
    - ``mag`` — the differential magnitude (star minus ensemble);
    - ``mag_err`` — the propagated error ``sqrt(star_err^2 + ens_err^2)``.
    """

    jd: float
    mag: float
    mag_err: float


@dataclass(frozen=True)
class StarScanResult:
    """The scan result of ONE field star.

    - ``star`` — the :class:`FieldStar`;
    - ``points`` — the differential lightcurve (ordered by jd);
    - ``n_frames`` — the number of points (frames the star was measured on);
    - ``mad`` — the robust scatter (raw MAD of the differential mags);
    - ``median_err`` — the median photometric error of the points;
    - ``variability`` — ``mad / median_err`` (see
      :func:`.stats.variability_index`);
    - ``variable`` — the candidate flag (``n_frames >= min_frames`` and
      ``variability >= threshold``);
    - ``rank`` — the 1-based rank within the scan run (1 = most variable).
    """

    star: FieldStar
    points: tuple[LightcurvePoint, ...]
    n_frames: int
    mad: float
    median_err: float
    variability: float
    variable: bool
    rank: int


@dataclass(frozen=True)
class SkippedScanFrame:
    """One sub-frame that could not be read during the scan:
    ``frame_id`` + the human-readable ``reason``."""

    frame_id: int
    reason: str


@dataclass(frozen=True)
class ScanReport:
    """The result of :func:`scan_project`.

    - ``scanned_at`` — the run time as a UTC ISO-8601 string;
    - ``stack_frame_id`` — the stack whose cross-match source list was
      scanned;
    - ``threshold`` / ``min_frames`` — the scan parameters used;
    - ``ensemble`` — :data:`ENSEMBLE_COMPS` or :data:`ENSEMBLE_FIELD_MEDIAN`;
    - ``n_frames`` — the total number of sub-frames processed (all
      sessions);
    - ``results`` — the per-star :class:`StarScanResult`\\ s, ordered by
      ``rank``;
    - ``skipped_frames`` — the :class:`SkippedScanFrame`\\ s.
    """

    scanned_at: str
    stack_frame_id: int
    threshold: float
    min_frames: int
    ensemble: str
    n_frames: int
    results: tuple[StarScanResult, ...]
    skipped_frames: tuple[SkippedScanFrame, ...] = ()

    @property
    def candidates(self) -> tuple[StarScanResult, ...]:
        """The variable candidates (``variable`` flag set), ordered by
        ``rank`` — the most likely variables first."""
        return tuple(r for r in self.results if r.variable)


def _read_frame(project: Project, frame: Any) -> tuple[Any, dict[str, Any]]:
    """Read the stored FITS of ``frame``: ``(data, header)`` (astropy, lazy
    import) — the same reading path as
    :func:`seestar_controller.photometry.measure._read_frame_fits`.
    Raises :class:`MeasurementError` when the file is unreadable."""
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


def _header_jd(header: Mapping[str, Any] | None) -> float | None:
    """The JD of the FITS ``DATE-OBS`` header card, or None when the header
    has no usable ``DATE-OBS``."""
    if not isinstance(header, Mapping):
        return None
    jd_utc = _date_obs_jd_utc(header.get("DATE-OBS"))
    return None if jd_utc is None else jd_utc[0]


def _finite_float(value: Any, what: str) -> float:
    """Coerce ``value`` to a finite float (bools rejected); raise
    :class:`ScanError` otherwise."""
    if isinstance(value, bool):
        raise ScanError(f"{what} must be a number, got {value!r}")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ScanError(f"{what} must be a number, got {value!r}") from None
    if not math.isfinite(result):
        raise ScanError(f"{what} must be finite, got {value!r}")
    return result


def _validate_min_frames(min_frames: Any) -> int:
    """Validate a minimum-frame count (an int >= 1, bools rejected);
    :class:`TypeError` for a non-int, :class:`ScanError` for a bad value."""
    if isinstance(min_frames, bool) or not isinstance(min_frames, int):
        raise TypeError(f"min_frames must be an int, got {min_frames!r}")
    if min_frames < 1:
        raise ScanError(f"min_frames must be >= 1, got {min_frames!r}")
    return min_frames


def _resolve_stack_frame_id(project: Project, stack_frame_id: Any) -> int:
    """Resolve the stack whose cross-match source list is scanned: the
    given ``stack_frame_id`` (must exist and have a non-empty cross-match
    table) or, when None, the LATEST cross-matched stack. Raises
    :class:`ScanError` when there is none."""
    if stack_frame_id is None:
        stacks = [s for s in list_crossmatched_stacks(project) if s.kind == KIND_STACK]
        if not stacks:
            raise ScanError(
                f"project {project.slug!r} has no cross-matched stack — run the "
                "Gaia cross-match on a stack first (store.crossmatch.save_crossmatch)"
            )
        return max(s.frame_id for s in stacks)
    if isinstance(stack_frame_id, bool) or not isinstance(stack_frame_id, int):
        raise TypeError(f"stack_frame_id must be an int or None, got {stack_frame_id!r}")
    frame = next((f for f in list_frames(project) if f.id == stack_frame_id), None)
    if frame is None:
        raise ScanError(f"project {project.slug!r} has no frame {stack_frame_id!r}")
    if not load_crossmatch(project, stack_frame_id):
        raise ScanError(
            f"frame {stack_frame_id!r} of project {project.slug!r} has no "
            "cross-match table — nothing to scan"
        )
    return stack_frame_id


def _near_any(x: float, y: float, positions: list[tuple[float, float]], radius: float) -> bool:
    """Whether ``(x, y)`` lies within ``radius`` of any of ``positions``."""
    return any(math.hypot(x - px, y - py) <= radius for px, py in positions)


def _pick_aperture(pick: Any) -> ApertureParams:
    """The per-pick aperture of a store
    :class:`~seestar_controller.store.stars.StarPick`."""
    return ApertureParams(
        radius_px=pick.aperture_radius,
        sky_in_px=pick.sky_annulus_inner,
        sky_out_px=pick.sky_annulus_outer,
    )


def scan_project(
    project: Any,
    *,
    threshold: float | None = None,
    min_frames: int | None = None,
    stack_frame_id: int | None = None,
    aperture: ApertureParams | None = None,
    exclude_radius_px: float = DEFAULT_EXCLUDE_RADIUS_PX,
    clock: Callable[[], datetime] | None = None,
) -> ScanReport:
    """Scan the field stars of ``project`` for variability and persist the
    results (one ``scan_results`` row per star, appended — runs accumulate
    across nights). Returns the :class:`ScanReport`.

    - ``threshold`` — the variability-ratio cutoff (``mad / median_err``);
      None = the project's persisted setting
      (:func:`.settings.load_scan_threshold`, default
      :data:`~.settings.DEFAULT_THRESHOLD`);
    - ``min_frames`` — the minimum number of frames a star must be measured
      on before it can be flagged; None =
      :data:`~.settings.DEFAULT_MIN_FRAMES`;
    - ``stack_frame_id`` — the stack whose cross-match source list is the
      pick list; None = the latest cross-matched stack;
    - ``aperture`` — the :class:`~seestar_controller.photometry.picking.ApertureParams`
      used for the field stars (the picked comps always use their own
      per-pick aperture); None = the defaults;
    - ``exclude_radius_px`` — sources within this radius of a picked
      target/comp/check are excluded (default
      :data:`DEFAULT_EXCLUDE_RADIUS_PX`);
    - ``clock`` — the run-time clock (defaults to UTC now); the run time is
      stored as ``scanned_at``.

    Raises :class:`~.errors.ScanError` when there is no cross-matched stack,
    the source list is empty, every source coincides with the picks, or a
    parameter is invalid; :class:`TypeError` for structural type errors
    (a non-Project handle, a non-int ``stack_frame_id``/``min_frames``).
    """
    project = _check_project(project)
    threshold_value = (
        load_scan_threshold(project) if threshold is None else validate_threshold(threshold)
    )
    min_frames_value = (
        DEFAULT_MIN_FRAMES if min_frames is None else _validate_min_frames(min_frames)
    )
    if aperture is None:
        aperture = ApertureParams()
    elif not isinstance(aperture, ApertureParams):
        raise TypeError(f"aperture must be an ApertureParams or None, got {aperture!r}")
    exclude_radius = _finite_float(exclude_radius_px, "exclude_radius_px")
    if exclude_radius < 0.0:
        raise ScanError(f"exclude_radius_px must be >= 0, got {exclude_radius_px!r}")
    stack_id = _resolve_stack_frame_id(project, stack_frame_id)
    sources = load_crossmatch(project, stack_id)
    if not sources:
        raise ScanError(
            f"the cross-match table of frame {stack_id!r} in project "
            f"{project.slug!r} is empty — nothing to scan"
        )
    picks = load_star_picks(project)
    excluded_positions = [
        (p.x, p.y) for p in picks if p.role in (ROLE_TARGET, ROLE_COMP, ROLE_CHECK)
    ]
    stars = [
        FieldStar(
            source_id=rec.source_id,
            ra_deg=rec.ra_deg,
            dec_deg=rec.dec_deg,
            x=rec.x,
            y=rec.y,
            matched=rec.matched,
        )
        for rec in sources
        if not _near_any(rec.x, rec.y, excluded_positions, exclude_radius)
    ]
    if not stars:
        raise ScanError(
            f"every cross-matched source of frame {stack_id!r} in project "
            f"{project.slug!r} coincides with the picked target/comps — "
            "nothing to scan"
        )
    comp_picks = [p for p in picks if p.role == ROLE_COMP]
    ensemble_mode = ENSEMBLE_COMPS if comp_picks else ENSEMBLE_FIELD_MEDIAN
    frames = list_frames(project, kind=KIND_SUB)  # ALL sub-frames of ALL sessions

    points: dict[int, list[LightcurvePoint]] = {i: [] for i in range(len(stars))}
    skipped: list[SkippedScanFrame] = []
    for frame in frames:
        try:
            data, header = _read_frame(project, frame)
        except MeasurementError as exc:
            logger.info("skipping sub-frame %s: %s", frame.id, exc)
            skipped.append(SkippedScanFrame(frame_id=frame.id, reason=str(exc)))
            continue
        jd = _header_jd(header)
        if jd is None:
            jd = frame.jd
        measured: dict[int, Any] = {}
        for i, star in enumerate(stars):
            try:
                measured[i] = measure_star(
                    data, star.x, star.y, aperture, role="field", label=None
                )
            except FrameSkippedError:
                continue  # the star gets no point for this frame
        comp_measurements = []
        for pick in comp_picks:
            try:
                comp_measurements.append(
                    measure_star(
                        data, pick.x, pick.y, _pick_aperture(pick),
                        role=ROLE_COMP, label=pick.label,
                    )
                )
            except FrameSkippedError:
                continue
        if comp_measurements:
            # the comp ensemble of this frame (comps only)
            ens = ensemble_mag([c.mag for c in comp_measurements])
            ens_err = ensemble_mag_err([c.mag_err for c in comp_measurements])
            for i, sm in measured.items():
                points[i].append(
                    LightcurvePoint(
                        jd=jd,
                        mag=sm.mag - ens,
                        mag_err=math.sqrt(sm.mag_err**2 + ens_err**2),
                    )
                )
        elif measured:
            # field-median fallback: leave-one-out median of the OTHER field
            # stars on this frame
            mags = {i: sm.mag for i, sm in measured.items()}
            errs = {i: sm.mag_err for i, sm in measured.items()}
            for i, sm in measured.items():
                others = [mags[j] for j in mags if j != i]
                other_errs = [errs[j] for j in errs if j != i]
                if others:
                    ens = float(statistics.median(others))
                    ens_err = float(statistics.median(other_errs))
                else:  # a single field star: no reference, the point is flat
                    ens, ens_err = sm.mag, 0.0
                points[i].append(
                    LightcurvePoint(
                        jd=jd,
                        mag=sm.mag - ens,
                        mag_err=math.sqrt(sm.mag_err**2 + ens_err**2),
                    )
                )

    # per-star robust stats + ranking
    partial: list[tuple[int, StarScanResult]] = []
    for i, star in enumerate(stars):
        pts = tuple(sorted(points[i], key=lambda p: p.jd))
        mags = [p.mag for p in pts]
        errs = [p.mag_err for p in pts]
        scatter = mad(mags) if mags else 0.0
        med_err = median(errs) if errs else 0.0
        variability = variability_index(scatter, med_err)
        partial.append(
            (
                i,
                StarScanResult(
                    star=star,
                    points=pts,
                    n_frames=len(pts),
                    mad=scatter,
                    median_err=med_err,
                    variability=variability,
                    variable=len(pts) >= min_frames_value and variability >= threshold_value,
                    rank=0,  # assigned below
                ),
            )
        )
    order = sorted(
        partial,
        key=lambda item: (
            -item[1].variability,
            -item[1].mad,
            item[1].star.source_id if item[1].star.source_id is not None else 0,
            item[1].star.ra_deg,
            item[1].star.dec_deg,
            item[1].star.x,
            item[1].star.y,
        ),
    )
    results = tuple(
        StarScanResult(
            star=r.star,
            points=r.points,
            n_frames=r.n_frames,
            mad=r.mad,
            median_err=r.median_err,
            variability=r.variability,
            variable=r.variable,
            rank=rank,
        )
        for rank, (_i, r) in enumerate(order, start=1)
    )
    instant = clock() if clock is not None else datetime.now(UTC)
    report = ScanReport(
        scanned_at=instant.astimezone(UTC).isoformat(timespec="milliseconds"),
        stack_frame_id=stack_id,
        threshold=threshold_value,
        min_frames=min_frames_value,
        ensemble=ensemble_mode,
        n_frames=len(frames),
        results=results,
        skipped_frames=tuple(skipped),
    )
    from .persist import save_scan_results  # lazy: persist imports this module

    save_scan_results(project, report)
    return report
