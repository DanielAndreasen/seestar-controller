"""Lomb-Scargle periodogram engine — the PURE core of period-analysis ticket 02
(:mod:`seestar_controller.analysis.periodogram`).

This is a PURE module: **no ``gi`` import** (the sandbox has no GTK, see
docs/wiki/assumptions.md; a subprocess test enforces the boundary) — stdlib +
:mod:`numpy` + :mod:`astropy.timeseries` + :mod:`seestar_controller.store`
only. No :mod:`seestar_controller.link`, no :mod:`ui`, no :mod:`photometry`,
no :mod:`enhance` imports.

What lives here (the engine; the widget layer of later tickets renders the
result):

- :class:`PeriodogramError` — the error type (an
  :class:`~seestar_controller.analysis.lightcurve.AnalysisError`, i.e. a
  :class:`ValueError`);
- :class:`Peak` — one frozen candidate peak: ``period`` (days, = 1/frequency),
  ``frequency`` (cycles/day), ``power``;
- :class:`PeriodogramResult` — the frozen result: the grid info
  (``n_points``, ``baseline``, ``median_dt``, ``f_min``, ``f_max``, ``df``,
  ``n_frequencies``, ``normalization``), the ``peaks`` tuple (sorted by power
  descending), the full ``frequencies``/``powers`` arrays, and
  ``false_alarm_probability`` (the Baluev FAP of the top peak, or None);
- :func:`frequency_grid` — the frequency grid (cycles/day) built from the
  observation baseline and cadence, directly unit-testable;
- :func:`select_peaks` — local-maxima extraction + deduplication, directly
  unit-testable;
- :func:`compute_periodogram` — the engine: points in, periodogram out;
- :func:`project_periodogram` — the store-backed convenience wrapper (reads
  the project's lightcurve via
  :func:`seestar_controller.store.measurements.lightcurve`, whose default
  filters excluded points).

Documented choices (the grid):

- **Minimum frequency** — ``f_min = min_cycles / baseline`` with
  ``baseline = t_max - t_min`` and ``min_cycles=2.0`` by default: at least ~2
  full cycles over the total baseline, so the lowest searched period is half
  the baseline.
- **Maximum frequency** — the Nyquist-like cadence limit
  ``f_max = nyquist_factor * 0.5 / median_dt`` (``nyquist_factor=1.0`` by
  default), where ``median_dt`` is the median of the POSITIVE consecutive time
  gaps (duplicate times are ignored for the cadence). The grid always spans at
  least up to ``f_max`` (the point count uses ``ceil``).
- **Spacing** — ``df = 1 / (oversample * baseline)`` with ``oversample=5.0``
  by default: 5x finer than the nominal frequency resolution ``1/baseline``,
  so a peak is sampled by ~5 grid points per resolution element.
- **Guards** — fewer than 2 times, a zero/negative baseline (all times
  equal), non-finite values, an empty grid (``f_max <= f_min``, e.g. only two
  points), or a grid larger than ``max_grid_points`` (default
  :data:`MAX_GRID_POINTS`) all raise :class:`PeriodogramError` — a clean
  error, never a crash or a silent empty spectrum.

Documented choices (the periodogram):

- **Normalization** — ``normalization='standard'`` by default (astropy's
  standard normalization: power in [0, 1], where 1 is a perfect sinusoid fit
  of the data; this makes "is the top peak dominant?" a simple threshold on
  power, and the false-alarm probability well-defined). ``'psd'`` is also
  accepted. The choice is recorded in ``PeriodogramResult.normalization``.
- **Method** — ``LombScargle(...).power(grid, method='auto')``: astropy picks
  the fast Press-Rybicki algorithm for our uniform grid (falling back to the
  direct method), which handles the uneven sampling and gaps — that is the
  point of Lomb-Scargle.
- **Errors** — ``mag_err`` is used as ``dy`` when present. Points with
  ``mag_err=None`` are filled with the MEDIAN of the known errors (documented
  choice); when ALL errors are None the periodogram is unweighted
  (``dy=None``). Negative errors are rejected.
- **Excluded points** — :func:`compute_periodogram` filters points flagged
  ``excluded`` unless ``include_excluded=True`` (the same policy as the store's
  :func:`~seestar_controller.store.measurements.lightcurve` default, which
  :func:`project_periodogram` relies on).
- **Peak extraction** — local maxima of the power array (endpoints included
  when they beat their single neighbor), sorted by power descending, then
  DEDUPLICATED: candidates closer than ``1/baseline`` in frequency (the
  nominal resolution) are merged, keeping the highest power. ``top_n`` peaks
  are returned (default 10).
- **False alarm probability** — the Baluev (2008) FAP of the TOP peak over
  the searched grid (``minimum_frequency=f_min``, ``maximum_frequency`` = the
  last grid point), exposed as ``PeriodogramResult.false_alarm_probability``
  (None when there are no peaks or the estimate is unavailable, e.g. some
  normalizations). A simple significance criterion for callers/tests: the
  top peak is dominant when its FAP is below the chosen level (e.g. 0.01) —
  pure noise yields FAP >> 0.01 and a small standard-normalized power.
- **Constant magnitudes** — all-equal ``mag`` values (zero variance) raise
  :class:`PeriodogramError` (astropy would produce all-NaN powers).

``points`` arguments are duck-typed: anything with ``jd`` / ``mag`` /
``mag_err`` / ``excluded`` attributes works —
:class:`~seestar_controller.analysis.lightcurve.LightcurvePoint` and the
store's :class:`~seestar_controller.store.measurements.Measurement` both do.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import numpy as np
from astropy.timeseries import LombScargle

from seestar_controller.store.measurements import lightcurve as _store_lightcurve

from .lightcurve import AnalysisError

logger = logging.getLogger(__name__)


class PeriodogramError(AnalysisError):
    """A periodogram failure: invalid points/grid parameters, a degenerate
    lightcurve (too few points, zero baseline, constant magnitudes), or an
    astropy failure — always a clean :class:`AnalysisError`, never a crash."""


#: Default minimum number of cycles over the baseline (``f_min = min_cycles /
#: baseline``).
DEFAULT_MIN_CYCLES = 2.0

#: Default oversampling factor: ``df = 1 / (oversample * baseline)`` — 5x the
#: nominal resolution ``1/baseline``.
DEFAULT_OVERSAMPLE = 5.0

#: Default multiple of the Nyquist-like cadence limit: ``f_max =
#: nyquist_factor * 0.5 / median_dt``.
DEFAULT_NYQUIST_FACTOR = 1.0

#: Default number of peaks returned.
DEFAULT_TOP_N = 10

#: Default Lomb-Scargle normalization ('standard': power in [0, 1]).
DEFAULT_NORMALIZATION = "standard"

#: The accepted Lomb-Scargle normalizations.
ALLOWED_NORMALIZATIONS = ("standard", "psd")

#: Guard: the frequency grid never exceeds this many points (memory/time);
#: raise :class:`PeriodogramError` instead of building a huge grid.
MAX_GRID_POINTS = 2_000_000


# --------------------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Peak:
    """One frozen periodogram candidate peak.

    - ``period`` — the period in days (``1 / frequency``);
    - ``frequency`` — the frequency in cycles/day;
    - ``power`` — the Lomb-Scargle power at that frequency (in [0, 1] for the
      'standard' normalization).
    """

    period: float
    frequency: float
    power: float

    def __post_init__(self) -> None:
        for name in ("period", "frequency", "power"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise PeriodogramError(f"Peak.{name} must be a number, got {value!r}")
            if not math.isfinite(float(value)):
                raise PeriodogramError(f"Peak.{name} must be finite, got {value!r}")
        if self.frequency <= 0.0:
            raise PeriodogramError(f"Peak.frequency must be > 0, got {self.frequency!r}")
        if self.period <= 0.0:
            raise PeriodogramError(f"Peak.period must be > 0, got {self.period!r}")


@dataclass(frozen=True, eq=False)
class PeriodogramResult:
    """The frozen result of :func:`compute_periodogram`.

    - ``n_points`` — the number of lightcurve points used (after exclusion
      filtering);
    - ``baseline`` — ``t_max - t_min`` in days;
    - ``median_dt`` — the median positive consecutive time gap in days (the
      cadence the grid's upper end is derived from);
    - ``f_min`` / ``f_max`` — the searched frequency range in cycles/day
      (``f_min = min_cycles / baseline``; ``f_max = nyquist_factor * 0.5 /
      median_dt``; the grid spans at least up to ``f_max``);
    - ``df`` — the uniform grid spacing (``1 / (oversample * baseline)``);
    - ``n_frequencies`` — the grid size (== ``len(frequencies)`` ==
      ``len(powers)``);
    - ``normalization`` — the Lomb-Scargle normalization used;
    - ``peaks`` — the top-N candidate peaks (tuple of :class:`Peak`, sorted by
      power descending, deduplicated to >= 1/baseline in frequency);
    - ``frequencies`` / ``powers`` — the full grid and power spectrum
      (:class:`numpy.ndarray`, float64);
    - ``false_alarm_probability`` — the Baluev FAP of the top peak over the
      searched grid, or None when unavailable.

    The dataclass is frozen but ``eq=False`` (the arrays make value equality
    meaningless); identity semantics apply.
    """

    n_points: int
    baseline: float
    median_dt: float
    f_min: float
    f_max: float
    df: float
    n_frequencies: int
    normalization: str
    peaks: tuple[Peak, ...]
    frequencies: np.ndarray
    powers: np.ndarray
    false_alarm_probability: float | None

    def __post_init__(self) -> None:
        if isinstance(self.n_points, bool) or not isinstance(self.n_points, int) or self.n_points < 2:
            raise PeriodogramError(f"n_points must be an int >= 2, got {self.n_points!r}")
        for name in ("baseline", "median_dt", "f_min", "f_max", "df"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise PeriodogramError(f"{name} must be a finite number, got {value!r}")
        if self.baseline <= 0.0 or self.median_dt <= 0.0:
            raise PeriodogramError(f"baseline/median_dt must be > 0, got {self.baseline!r}/{self.median_dt!r}")
        if self.f_min <= 0.0 or self.f_max < self.f_min or self.df <= 0.0:
            raise PeriodogramError(
                f"invalid grid range: f_min={self.f_min!r}, f_max={self.f_max!r}, df={self.df!r}"
            )
        if self.normalization not in ALLOWED_NORMALIZATIONS:
            raise PeriodogramError(f"normalization must be one of {ALLOWED_NORMALIZATIONS}, got {self.normalization!r}")
        if not isinstance(self.n_frequencies, int) or self.n_frequencies < 2:
            raise PeriodogramError(f"n_frequencies must be an int >= 2, got {self.n_frequencies!r}")
        frequencies = np.asarray(self.frequencies)
        powers = np.asarray(self.powers)
        if frequencies.shape != (self.n_frequencies,) or powers.shape != (self.n_frequencies,):
            raise PeriodogramError(
                f"frequencies/powers must have shape ({self.n_frequencies},), got "
                f"{frequencies.shape}/{powers.shape}"
            )
        if not isinstance(self.peaks, tuple) or not all(isinstance(p, Peak) for p in self.peaks):
            raise PeriodogramError("peaks must be a tuple of Peak")
        if self.false_alarm_probability is not None and not (
            isinstance(self.false_alarm_probability, (int, float))
            and math.isfinite(float(self.false_alarm_probability))
        ):
            raise PeriodogramError(
                f"false_alarm_probability must be None or a finite number, got {self.false_alarm_probability!r}"
            )


# --------------------------------------------------------------------------------------
# Validation helpers
# --------------------------------------------------------------------------------------


def _as_float_array(values: Any, *, what: str) -> np.ndarray:
    """Coerce ``values`` to a 1-D float64 array of finite values, else raise
    :class:`PeriodogramError` naming ``what``."""
    try:
        arr = np.asarray(values, dtype=float)
    except (TypeError, ValueError) as exc:
        raise PeriodogramError(f"{what} must be a 1-D array of numbers: {exc}") from exc
    if arr.ndim != 1:
        raise PeriodogramError(f"{what} must be 1-D, got shape {arr.shape}")
    if arr.size and not bool(np.all(np.isfinite(arr))):
        raise PeriodogramError(f"{what} must contain only finite values")
    return arr


def _number_param(value: Any, *, name: str, minimum: float | None = None, strictly_above: bool = False) -> float:
    """Validate a numeric keyword parameter (bools rejected); return it as a
    float. ``minimum`` bounds it (strictly when ``strictly_above``)."""
    if isinstance(value, bool) or not isinstance(value, (int, float, np.integer, np.floating)):
        raise PeriodogramError(f"{name} must be a number, got {value!r}")
    result = float(value)
    if not math.isfinite(result):
        raise PeriodogramError(f"{name} must be finite, got {value!r}")
    if minimum is not None:
        if strictly_above and not result > minimum:
            raise PeriodogramError(f"{name} must be > {minimum}, got {value!r}")
        if not strictly_above and result < minimum:
            raise PeriodogramError(f"{name} must be >= {minimum}, got {value!r}")
    return result


def _int_param(value: Any, *, name: str, minimum: int) -> int:
    """Validate an integer keyword parameter (bools rejected)."""
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise PeriodogramError(f"{name} must be an int, got {value!r}")
    result = int(value)
    if result < minimum:
        raise PeriodogramError(f"{name} must be >= {minimum}, got {value!r}")
    return result


# --------------------------------------------------------------------------------------
# The frequency grid
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _GridSpec:
    """Internal: the grid plus the quantities it was derived from."""

    frequencies: np.ndarray
    baseline: float
    median_dt: float
    f_min: float
    f_max: float
    df: float


def _grid_spec(
    times: Any,
    *,
    min_cycles: float,
    oversample: float,
    nyquist_factor: float,
    max_grid_points: int,
) -> _GridSpec:
    """Build the frequency grid spec from ``times`` (see :func:`frequency_grid`
    for the documented formulas)."""
    t = _as_float_array(times, what="times")
    if t.size < 2:
        raise PeriodogramError(
            f"need at least 2 times to build a frequency grid, got {t.size}"
        )
    min_cycles = _number_param(min_cycles, name="min_cycles", minimum=0.0, strictly_above=True)
    oversample = _number_param(oversample, name="oversample", minimum=1.0)
    nyquist_factor = _number_param(nyquist_factor, name="nyquist_factor", minimum=0.0, strictly_above=True)
    max_grid_points = _int_param(max_grid_points, name="max_grid_points", minimum=1)

    baseline = float(t.max() - t.min())
    if not baseline > 0.0:
        raise PeriodogramError(
            f"cannot build a frequency grid: the observation baseline is {baseline} "
            "(all times equal?) — need at least two distinct times"
        )
    dt = np.diff(np.sort(t))
    positive = dt[dt > 0.0]
    if positive.size == 0:
        raise PeriodogramError("cannot build a frequency grid: all consecutive time gaps are zero")
    median_dt = float(np.median(positive))

    f_min = min_cycles / baseline
    f_max = nyquist_factor * 0.5 / median_dt
    df = 1.0 / (oversample * baseline)
    if not f_max > f_min:
        raise PeriodogramError(
            f"empty frequency grid: f_max ({f_max:.6g} /d) <= f_min ({f_min:.6g} /d) — "
            "too few distinct times for the baseline/cadence (add points, lower "
            "min_cycles, or lower nyquist_factor)"
        )
    n = math.ceil((f_max - f_min) / df) + 1  # ceil: the grid spans at least up to f_max
    if n > max_grid_points:
        raise PeriodogramError(
            f"the frequency grid would have {n} points (> max_grid_points={max_grid_points}) — "
            "use a smaller oversample or nyquist_factor, or a larger min_cycles"
        )
    frequencies = f_min + df * np.arange(n, dtype=float)
    return _GridSpec(
        frequencies=frequencies,
        baseline=baseline,
        median_dt=median_dt,
        f_min=f_min,
        f_max=f_max,
        df=df,
    )


def frequency_grid(
    times: Iterable[float],
    *,
    min_cycles: float = DEFAULT_MIN_CYCLES,
    oversample: float = DEFAULT_OVERSAMPLE,
    nyquist_factor: float = DEFAULT_NYQUIST_FACTOR,
    max_grid_points: int = MAX_GRID_POINTS,
) -> np.ndarray:
    """The Lomb-Scargle frequency grid (cycles/day) for the observation times
    ``times`` (e.g. Julian Dates).

    - ``f_min = min_cycles / baseline`` — by default 2 cycles over the total
      baseline ``t_max - t_min``;
    - ``f_max = nyquist_factor * 0.5 / median_dt`` — the Nyquist-like cadence
      limit from the median POSITIVE consecutive gap (duplicate times are
      ignored for the cadence); the returned grid spans at least up to
      ``f_max``;
    - uniform spacing ``df = 1 / (oversample * baseline)`` — by default 5x the
      nominal resolution ``1/baseline``.

    Raises :class:`PeriodogramError` for fewer than 2 times, a zero/negative
    baseline (all times equal), non-finite values, an empty range
    (``f_max <= f_min``), or a grid larger than ``max_grid_points``.
    """
    return _grid_spec(
        times,
        min_cycles=min_cycles,
        oversample=oversample,
        nyquist_factor=nyquist_factor,
        max_grid_points=max_grid_points,
    ).frequencies


# --------------------------------------------------------------------------------------
# Peak extraction
# --------------------------------------------------------------------------------------


def select_peaks(
    frequencies: Iterable[float],
    powers: Iterable[float],
    *,
    top_n: int = DEFAULT_TOP_N,
    min_separation: float,
) -> tuple[Peak, ...]:
    """Extract the top-N peaks from a power spectrum.

    Local maxima of ``powers`` over the strictly increasing ``frequencies``
    (endpoints count when they beat their single neighbor) are sorted by power
    descending and DEDUPLICATED: candidates closer than ``min_separation`` in
    frequency are merged, keeping the highest power (the iteration is in
    power-descending order, so the first — highest — of a cluster wins).

    Returns a tuple of :class:`Peak` sorted by power descending (at most
    ``top_n``; empty when the spectrum has no local maxima). ``min_separation``
    is typically the nominal resolution ``1/baseline``.
    """
    f = _as_float_array(frequencies, what="frequencies")
    p = _as_float_array(powers, what="powers")
    if f.size != p.size:
        raise PeriodogramError(
            f"frequencies and powers must have the same length, got {f.size} and {p.size}"
        )
    top_n = _int_param(top_n, name="top_n", minimum=1)
    min_separation = _number_param(min_separation, name="min_separation", minimum=0.0)
    if f.size == 0:
        return ()
    if f.size > 1 and not bool(np.all(np.diff(f) > 0)):
        raise PeriodogramError("frequencies must be strictly increasing")

    if f.size == 1:
        indices = np.array([0], dtype=int)
    else:
        interior = np.where((p[1:-1] > p[:-2]) & (p[1:-1] >= p[2:]))[0] + 1
        ends = [i for i, cond in ((0, p[0] > p[1]), (f.size - 1, p[-1] > p[-2])) if cond]
        indices = np.concatenate([interior, np.array(ends, dtype=int)]) if ends else interior
    if indices.size == 0:
        return ()

    order = indices[np.argsort(p[indices], kind="stable")[::-1]]  # power descending
    selected: list[int] = []
    for i in order:
        fi = float(f[i])
        if all(abs(fi - float(f[j])) >= min_separation for j in selected):
            selected.append(int(i))
        if len(selected) >= top_n:
            break
    return tuple(
        Peak(period=1.0 / float(f[i]), frequency=float(f[i]), power=float(p[i]))
        for i in selected
    )


# --------------------------------------------------------------------------------------
# The engine
# --------------------------------------------------------------------------------------


def _extract_points(points: Any, *, include_excluded: bool) -> tuple[tuple[float, float, float | None], ...]:
    """Normalize ``points`` to a tuple of ``(jd, mag, mag_err)`` triples,
    filtering excluded points unless ``include_excluded``."""
    try:
        pts = tuple(points)
    except TypeError as exc:
        raise PeriodogramError(f"points must be an iterable of lightcurve points: {exc}") from exc
    total = len(pts)
    if not include_excluded:
        pts = tuple(p for p in pts if not bool(getattr(p, "excluded", False)))
    if len(pts) < 2:
        hint = " (all points excluded?)" if total > len(pts) else ""
        raise PeriodogramError(
            f"need at least 2 lightcurve points to compute a periodogram, got {len(pts)}{hint}"
        )
    triples: list[tuple[float, float, float | None]] = []
    for p in pts:
        try:
            jd = p.jd
            mag = p.mag
            mag_err = p.mag_err
        except AttributeError as exc:
            raise PeriodogramError(
                "points must expose jd/mag/mag_err attributes "
                "(LightcurvePoint or store Measurement): missing "
                f"{exc}"
            ) from exc
        triples.append((jd, mag, mag_err))
    return tuple(triples)


def compute_periodogram(
    points: Iterable[Any],
    *,
    top_n: int = DEFAULT_TOP_N,
    min_cycles: float = DEFAULT_MIN_CYCLES,
    oversample: float = DEFAULT_OVERSAMPLE,
    nyquist_factor: float = DEFAULT_NYQUIST_FACTOR,
    include_excluded: bool = False,
    normalization: str = DEFAULT_NORMALIZATION,
    max_grid_points: int = MAX_GRID_POINTS,
) -> PeriodogramResult:
    """Compute the Lomb-Scargle periodogram of the lightcurve ``points``.

    ``points`` is any iterable of duck-typed points with ``jd`` / ``mag`` /
    ``mag_err`` / ``excluded`` attributes —
    :class:`~seestar_controller.analysis.lightcurve.LightcurvePoint` or the
    store's :class:`~seestar_controller.store.measurements.Measurement`.

    - ``top_n`` — how many candidate peaks to return (default 10);
    - ``min_cycles`` / ``oversample`` / ``nyquist_factor`` / ``max_grid_points``
      — the frequency grid parameters, see :func:`frequency_grid`;
    - ``include_excluded`` — when False (default), points flagged ``excluded``
      are filtered out (the store lightcurve's default policy); when True they
      are included;
    - ``normalization`` — 'standard' (default, power in [0, 1]) or 'psd'.

    Returns a frozen :class:`PeriodogramResult` with the grid info, the full
    spectrum, the deduplicated top-N :class:`Peak`\\ s (sorted by power
    descending), and the Baluev false-alarm probability of the top peak.
    Raises :class:`PeriodogramError` on any invalid/degenerate input.
    """
    if not isinstance(include_excluded, bool):
        raise PeriodogramError(f"include_excluded must be a bool, got {include_excluded!r}")
    if not isinstance(normalization, str) or normalization not in ALLOWED_NORMALIZATIONS:
        raise PeriodogramError(
            f"normalization must be one of {ALLOWED_NORMALIZATIONS}, got {normalization!r}"
        )
    top_n = _int_param(top_n, name="top_n", minimum=1)

    triples = _extract_points(points, include_excluded=include_excluded)
    t = _as_float_array([jd for jd, _, _ in triples], what="point jd values")
    y = _as_float_array([mag for _, mag, _ in triples], what="point mag values")

    errs = [mag_err for _, _, mag_err in triples]
    known = [e for e in errs if e is not None]
    dy: np.ndarray | None = None
    if known:
        known_arr = _as_float_array(known, what="mag_err values")
        if bool(np.any(known_arr < 0.0)):
            raise PeriodogramError("mag_err must be >= 0 for every point")
        fill = float(np.median(known_arr))
        dy = np.array([fill if e is None else float(e) for e in errs], dtype=float)

    if not float(np.ptp(y)) > 0.0:
        raise PeriodogramError("all magnitudes are identical — no variability to analyze")

    spec = _grid_spec(
        t,
        min_cycles=min_cycles,
        oversample=oversample,
        nyquist_factor=nyquist_factor,
        max_grid_points=max_grid_points,
    )

    try:
        ls = LombScargle(t, y, dy=dy, normalization=normalization)
        power = np.asarray(ls.power(spec.frequencies, method="auto"), dtype=float)
    except PeriodogramError:
        raise
    except Exception as exc:
        raise PeriodogramError(f"LombScargle failed: {exc}") from exc
    if power.shape != spec.frequencies.shape or not bool(np.all(np.isfinite(power))):
        raise PeriodogramError("LombScargle produced a non-finite power spectrum")

    peaks = select_peaks(
        spec.frequencies, power, top_n=top_n, min_separation=1.0 / spec.baseline
    )

    fap: float | None = None
    if peaks:
        try:
            fap = float(
                ls.false_alarm_probability(
                    peaks[0].power,
                    method="baluev",
                    minimum_frequency=spec.f_min,
                    maximum_frequency=float(spec.frequencies[-1]),
                )
            )
        except Exception as exc:  # e.g. a normalization without a defined FAP  # noqa: BLE001
            logger.info("false-alarm probability unavailable: %s", exc)
            fap = None

    return PeriodogramResult(
        n_points=len(triples),
        baseline=spec.baseline,
        median_dt=spec.median_dt,
        f_min=spec.f_min,
        f_max=spec.f_max,
        df=spec.df,
        n_frequencies=int(spec.frequencies.size),
        normalization=normalization,
        peaks=peaks,
        frequencies=spec.frequencies,
        powers=power,
        false_alarm_probability=fap,
    )


def project_periodogram(
    project: Any,
    *,
    include_excluded: bool = False,
    **kwargs: Any,
) -> PeriodogramResult:
    """The periodogram of a project's lightcurve: reads the measurements via
    :func:`seestar_controller.store.measurements.lightcurve` (whose default
    ``include_excluded=False`` filters outlier points — pass
    ``include_excluded=True`` to include them) and computes the periodogram
    with :func:`compute_periodogram` (``kwargs`` forwarded, e.g. ``top_n=``).
    """
    if not isinstance(include_excluded, bool):
        raise PeriodogramError(f"include_excluded must be a bool, got {include_excluded!r}")
    measurements = _store_lightcurve(project, include_excluded=include_excluded)
    return compute_periodogram(measurements, include_excluded=include_excluded, **kwargs)
