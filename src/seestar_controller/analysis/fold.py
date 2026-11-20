"""Period selection + phase fold — the PURE core of period-analysis ticket 03
(:mod:`seestar_controller.analysis.fold`).

This is a PURE module: **no ``gi`` import** (the sandbox has no GTK, see
docs/wiki/assumptions.md; a subprocess test enforces the boundary) — stdlib +
:mod:`seestar_controller.store` + the sibling pure modules
:mod:`.lightcurve` / :mod:`.periodogram` only. No :mod:`seestar_controller.link`,
no :mod:`ui`, no :mod:`photometry`, no :mod:`enhance` imports. The widget layer
(:mod:`.period_view`) imports ``gi`` at module top and is never imported by
this module or eagerly by the package ``__init__``.

What lives here:

- :class:`FoldError` / :class:`PeriodError` — the error hierarchy (both
  :class:`~seestar_controller.analysis.lightcurve.AnalysisError`, i.e.
  :class:`ValueError`\\ s);
- :func:`validate_period` — the period validation (a finite number > 0 in
  days; bools rejected);
- :func:`phase_of` — the fold math: ``phase = ((jd - jd0) / period) mod 1``,
  always in ``[0, 1)`` (the float-rounding edge that can yield exactly ``1.0``
  is clamped to ``0.0``);
- :class:`FoldedPoint` — one frozen folded point: the original
  :class:`~seestar_controller.analysis.lightcurve.LightcurvePoint` plus its
  ``phase``;
- :func:`fold_points` — fold a point iterable by ``period`` (``jd0`` defaults
  to the FIRST observation = the min jd of the points) and return the
  :class:`FoldedPoint`\\ s **sorted by phase**;
- :func:`folded_window` — the full plot window of folded points (phase 0–1 on
  the x axis, padded; mag range padded 5% like
  :func:`~seestar_controller.analysis.lightcurve.full_window`) — the folded
  plot reuses the pure :class:`~seestar_controller.analysis.lightcurve.PlotWindow`
  / :class:`~seestar_controller.analysis.lightcurve.PlotGeometry` transforms
  with ``x = phase``;
- :class:`PeriodSnapshot` — the frozen controller snapshot: ``period`` (days,
  or None), ``source`` (``'peak'`` / ``'manual'`` / None), ``peak_index``,
  ``jd0``, the last ``result`` (:class:`~seestar_controller.analysis.periodogram.PeriodogramResult`)
  with its ``peaks``, the ``folded`` points, and ``error`` (never raises);
- :class:`PeriodController` — the app-level shared controller (the app owns
  one, bound to the shared
  :class:`~seestar_controller.analysis.lightcurve.LightcurveController`'s
  project): :meth:`~PeriodController.compute_periodogram` runs
  :func:`~seestar_controller.analysis.periodogram.project_periodogram` on
  demand, :meth:`~PeriodController.select_peak` picks a peak of the computed
  periodogram, :meth:`~PeriodController.set_manual_period` enters a period in
  days, :meth:`~PeriodController.clear` resets — and **every selection is
  persisted into the project's ``project.json``** (``config['period']``, a
  plain float in days, via :meth:`~seestar_controller.store.project.Project.save_config`)
  and **loaded back on** :meth:`~PeriodController.set_project`. Listeners
  (:meth:`~PeriodController.add_listener` /
  :meth:`~PeriodController.remove_listener`) get a frozen
  :class:`PeriodSnapshot` on every change, marshalled through
  ``invoke_on_main`` (the app binds ``GLib.idle_add``; the default calls
  directly, so tests are synchronous).

Conventions (documented choices):

- **Fold math.** ``phase = ((jd - jd0) / period) mod 1`` with ``jd0``
  defaulting to the first observation (the min jd of the folded points).
  Python's float ``%`` returns a value in ``[0, 1)`` for a positive divisor
  except the rounding edge (``(-1e-20) % 1.0 == 1.0``), which is clamped to
  ``0.0`` — so ``phase`` is ALWAYS in ``[0, 1)``.
- **Period validation.** A period must be a finite number > 0 (days); bools,
  NaN/inf, zero and negatives raise :class:`FoldError`. The same validation
  guards manual entry AND the persisted value on load (a corrupt persisted
  value is ignored with a logged warning, never a crash).
- **Persistence shape.** ``project.json``'s ``period`` key holds a plain
  float (days) or null — the placeholder written at project creation (see
  :mod:`seestar_controller.store.project`). Only the period is persisted, not
  the source; on load the restored period is reported with source
  ``'manual'`` (documented choice). Saving happens on every selection
  (:meth:`~PeriodController.select_peak` / :meth:`~PeriodController.set_manual_period`
  / :meth:`~PeriodController.clear`); loading happens in
  :meth:`~PeriodController.set_project`. With no project set, selections are
  kept in memory only. A persistence failure raises :class:`PeriodError`
  (chained) and leaves the in-memory state unchanged.
- **Threading / sqlite thread-affinity.** Same pattern as
  :class:`~seestar_controller.analysis.lightcurve.LightcurveController`: every
  STORE call (periodogram read, ``save_config``) goes through
  :meth:`PeriodController._call_store` — direct on the db-owning thread,
  otherwise marshalled through ``invoke_on_main`` with a wait. The points
  themselves come from the bound lightcurve controller's snapshot (which
  already marshals its own store reads and never raises).
- **The periodogram is computed on demand** (:meth:`~PeriodController.compute_periodogram`),
  not on every snapshot — it is the expensive step; the snapshot carries the
  last computed result (``result`` / ``peaks``) and the folded points of the
  current period.
"""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from .lightcurve import (
    DB_CALL_TIMEOUT_S,
    AnalysisError,
    InvokeOnMain,
    LightcurveController,
    LightcurvePoint,
    PlotWindow,
    _direct_invoke,
    _point_from_measurement,
)
from .periodogram import (
    DEFAULT_TOP_N,
    Peak,
    PeriodogramResult,
    project_periodogram,
)

logger = logging.getLogger(__name__)


class FoldError(AnalysisError):
    """A fold-math failure: an invalid period (not a finite number > 0), an
    invalid jd0, or an empty point set without an explicit jd0 — always a
    clean :class:`AnalysisError`, never a crash."""


class PeriodError(FoldError):
    """A period-controller failure: no periodogram computed yet, a peak index
    out of range, or a persistence (project.json) failure — wrapped from the
    underlying cause."""


#: The selection source when the period came from a periodogram peak.
SOURCE_PEAK = "peak"

#: The selection source when the period was entered manually (or restored
#: from project.json — only the period is persisted, see module docstring).
SOURCE_MANUAL = "manual"

#: The accepted source values.
SOURCES = (SOURCE_PEAK, SOURCE_MANUAL)

#: The project.json config key holding the chosen period (days, float or
#: null) — the placeholder written at project creation.
PERIOD_CONFIG_KEY = "period"


# --------------------------------------------------------------------------------------
# Period validation + the fold math
# --------------------------------------------------------------------------------------


def validate_period(period: Any) -> float:
    """Validate a period in days and return it as a float.

    A period must be a finite number > 0 (bools rejected); anything else
    raises :class:`FoldError` naming the value.
    """
    if isinstance(period, bool) or not isinstance(period, (int, float)):
        raise FoldError(f"period must be a number (days), got {period!r}")
    result = float(period)
    if not math.isfinite(result):
        raise FoldError(f"period must be finite, got {period!r}")
    if not result > 0.0:
        raise FoldError(f"period must be > 0 days, got {period!r}")
    return result


def _validate_jd0(jd0: Any) -> float:
    """Validate an explicit epoch ``jd0`` (a finite number; bools rejected)."""
    if isinstance(jd0, bool) or not isinstance(jd0, (int, float)):
        raise FoldError(f"jd0 must be a number (JD), got {jd0!r}")
    result = float(jd0)
    if not math.isfinite(result):
        raise FoldError(f"jd0 must be finite, got {jd0!r}")
    return result


def phase_of(jd: Any, period: Any, jd0: Any) -> float:
    """The phase of ``jd`` for ``period`` (days) and epoch ``jd0`` (JD):

    ``phase = ((jd - jd0) / period) mod 1``, always in ``[0, 1)``.

    ``period`` is validated with :func:`validate_period`; ``jd`` / ``jd0``
    must be finite numbers. The float-rounding edge of ``%`` (which can
    return exactly ``1.0`` for a tiny negative dividend) is clamped to
    ``0.0``, so the result is always ``0.0 <= phase < 1.0``.
    """
    p = validate_period(period)
    for name, value in (("jd", jd), ("jd0", jd0)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise FoldError(f"{name} must be a number (JD), got {value!r}")
        if not math.isfinite(float(value)):
            raise FoldError(f"{name} must be finite, got {value!r}")
    phase = (float(jd) - float(jd0)) / p % 1.0
    if phase >= 1.0:  # float-rounding edge: (-1e-20) % 1.0 == 1.0
        phase = 0.0
    return phase


# --------------------------------------------------------------------------------------
# Folded points
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FoldedPoint:
    """One frozen phase-folded lightcurve point: the original
    :class:`~seestar_controller.analysis.lightcurve.LightcurvePoint` plus its
    ``phase`` in ``[0, 1)``."""

    point: LightcurvePoint
    phase: float

    def __post_init__(self) -> None:
        if not isinstance(self.point, LightcurvePoint):
            raise FoldError(
                f"FoldedPoint.point must be a LightcurvePoint, got {type(self.point).__name__}"
            )
        phase = self.phase
        if isinstance(phase, bool) or not isinstance(phase, (int, float)):
            raise FoldError(f"FoldedPoint.phase must be a number, got {phase!r}")
        if not math.isfinite(float(phase)) or not 0.0 <= float(phase) < 1.0:
            raise FoldError(f"FoldedPoint.phase must be in [0, 1), got {phase!r}")


def fold_points(
    points: Iterable[Any],
    period: Any,
    jd0: Any = None,
) -> tuple[FoldedPoint, ...]:
    """Fold ``points`` by ``period`` (days) and return the
    :class:`FoldedPoint`\\ s **sorted by phase** (stable: ties keep the input
    order).

    - ``period`` — validated with :func:`validate_period` (> 0, finite);
    - ``jd0`` — the epoch (JD); None (default) uses the FIRST observation,
      i.e. the min jd of ``points``. An empty ``points`` with ``jd0=None``
      raises :class:`FoldError` (no observation to define the epoch);
      an empty ``points`` with an explicit ``jd0`` yields ``()``.

    ``points`` is duck-typed: anything with a ``jd`` attribute works
    (:class:`~seestar_controller.analysis.lightcurve.LightcurvePoint` and the
    store's :class:`~seestar_controller.store.measurements.Measurement` both
    do) — but the returned :class:`FoldedPoint`\\ s wrap
    :class:`LightcurvePoint`\\ s, so store measurements are mapped first when
    needed (see :func:`_as_lightcurve_point`).
    """
    p = validate_period(period)
    try:
        pts = tuple(points)
    except TypeError as exc:
        raise FoldError(f"points must be an iterable of lightcurve points: {exc}") from exc
    if jd0 is None:
        if not pts:
            raise FoldError(
                "cannot fold an empty point set without an explicit jd0 "
                "(the default epoch is the first observation)"
            )
        jds: list[float] = []
        for pt in pts:
            try:
                jds.append(float(pt.jd))
            except AttributeError as exc:
                raise FoldError(
                    "points must expose a jd attribute (LightcurvePoint or "
                    f"store Measurement): missing {exc}"
                ) from exc
        epoch = min(jds)
    else:
        epoch = _validate_jd0(jd0)
    folded: list[FoldedPoint] = []
    for pt in pts:
        try:
            jd = pt.jd
        except AttributeError as exc:
            raise FoldError(
                "points must expose a jd attribute (LightcurvePoint or store "
                f"Measurement): missing {exc}"
            ) from exc
        point = pt if isinstance(pt, LightcurvePoint) else _as_lightcurve_point(pt)
        folded.append(FoldedPoint(point=point, phase=phase_of(jd, p, epoch)))
    folded.sort(key=lambda fp: fp.phase)
    return tuple(folded)


def _as_lightcurve_point(m: Any) -> LightcurvePoint:
    """Map a store measurement (or any duck-typed point) to a
    :class:`LightcurvePoint` — delegates to the lightcurve module's mapper."""
    return _point_from_measurement(m)


def folded_window(folded: Iterable[FoldedPoint]) -> PlotWindow:
    """The full plot window of the folded points (the reset view of the folded
    plot): the x axis is the phase, fixed to ``[-0.02, 1.02]`` (phase 0–1
    padded by 2% on each side); the y axis is the mag range of the folded
    points padded by 5% on each side (degenerate range expanded by ±0.05 mag),
    exactly like :func:`~seestar_controller.analysis.lightcurve.full_window`.

    The returned :class:`~seestar_controller.analysis.lightcurve.PlotWindow`
    is used with the pure transforms with ``x = phase`` (``jd_min``/``jd_max``
    carry the phase range). Empty ``folded`` yields a phase 0–1 window with
    the default mag range.
    """
    folded = tuple(folded)
    if not folded:
        return PlotWindow(jd_min=-0.02, jd_max=1.02, mag_min=-1.0, mag_max=1.0)
    mags = [fp.point.mag for fp in folded]
    mag_min, mag_max = min(mags), max(mags)
    if mag_max - mag_min < 1e-6:
        mag_min, mag_max = mag_min - 0.05, mag_max + 0.05
    else:
        pad = 0.05 * (mag_max - mag_min)
        mag_min, mag_max = mag_min - pad, mag_max + pad
    return PlotWindow(jd_min=-0.02, jd_max=1.02, mag_min=mag_min, mag_max=mag_max)


def periodogram_window(result: PeriodogramResult) -> PlotWindow:
    """The full plot window of a periodogram result for plotting **power vs
    period**: the x axis is the period in days spanning
    ``[1/f_grid_max, 1/f_grid_min]`` (the grid's frequency range inverted —
    small periods on the left), and the y axis is the NEGATED power, so the
    pure transforms (which draw the window's ``mag_min`` at the TOP) put the
    LARGEST power at the top. The power range is padded by 5% on each side
    (degenerate range expanded by ±0.05).

    Plot a spectrum point with ``data_to_screen(window, geom, period,
    -power)``; read the power axis back as ``-mag``.
    """
    freqs = result.frequencies
    powers = result.powers
    x_min = 1.0 / float(freqs[-1])  # the largest grid frequency -> smallest period
    x_max = 1.0 / float(freqs[0])  # the smallest grid frequency -> largest period
    p_min = min(float(p) for p in powers)
    p_max = max(float(p) for p in powers)
    if p_max - p_min < 1e-12:
        p_min, p_max = p_min - 0.05, p_max + 0.05
    else:
        pad = 0.05 * (p_max - p_min)
        p_min, p_max = p_min - pad, p_max + pad
    # y = -power: mag_min (drawn at the top) carries the largest power
    return PlotWindow(jd_min=x_min, jd_max=x_max, mag_min=-p_max, mag_max=-p_min)


# --------------------------------------------------------------------------------------
# The snapshot
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PeriodSnapshot:
    """The frozen snapshot of a :class:`PeriodController`, handed to the
    listeners on every change.

    - ``period`` — the chosen period in days, or None when none is selected;
    - ``source`` — ``'peak'`` / ``'manual'``, or None when no period is
      selected;
    - ``peak_index`` — the index into ``peaks`` when ``source == 'peak'``,
      else None;
    - ``jd0`` — the fold epoch (the first observation's jd), or None when
      there are no points;
    - ``result`` — the last computed
      :class:`~seestar_controller.analysis.periodogram.PeriodogramResult`, or
      None when :meth:`~PeriodController.compute_periodogram` has not run;
    - ``peaks`` — ``result.peaks`` (empty when no result);
    - ``folded`` — the folded points (ALL sessions, sorted by phase) of the
      current period, or ``()`` when no period is selected / no points;
    - ``error`` — the last error message, else None.
    """

    period: float | None
    source: str | None
    peak_index: int | None
    jd0: float | None
    result: PeriodogramResult | None
    peaks: tuple[Peak, ...]
    folded: tuple[FoldedPoint, ...]
    error: str | None

    def __post_init__(self) -> None:
        if self.period is not None:
            validate_period(self.period)  # raises FoldError on a corrupt snapshot
        if self.source is not None and self.source not in SOURCES:
            raise FoldError(f"source must be one of {SOURCES} or None, got {self.source!r}")
        if self.source == SOURCE_PEAK and not isinstance(self.peak_index, int):
            raise FoldError("peak_index must be an int when source == 'peak'")
        if not isinstance(self.peaks, tuple) or not all(isinstance(p, Peak) for p in self.peaks):
            raise FoldError("peaks must be a tuple of Peak")
        if not isinstance(self.folded, tuple) or not all(
            isinstance(fp, FoldedPoint) for fp in self.folded
        ):
            raise FoldError("folded must be a tuple of FoldedPoint")


#: A period listener: called with a frozen :class:`PeriodSnapshot` on any
#: change (always marshalled via ``invoke_on_main``).
PeriodListener = Callable[[PeriodSnapshot], None]


# --------------------------------------------------------------------------------------
# The controller
# --------------------------------------------------------------------------------------


class PeriodController:
    """The app-level period controller (period-analysis ticket 03).

    - ``lightcurve_controller`` — the shared
      :class:`~seestar_controller.analysis.lightcurve.LightcurveController`
      whose project (and points) this controller follows; when omitted, a
      standalone one is created (optionally over ``project``), so the
      controller also works alone in tests;
    - ``project`` — an explicit project for the standalone lightcurve
      controller (ignored when ``lightcurve_controller`` is given);
    - ``invoke_on_main`` — the marshalling callable for listener notifications
      AND for store calls made from a non-db thread (the app binds
      ``GLib.idle_add``; the default calls directly);
    - ``top_n`` — how many peaks :meth:`compute_periodogram` keeps (default
      :data:`~seestar_controller.analysis.periodogram.DEFAULT_TOP_N`).

    Operations: :attr:`snapshot` (the frozen :class:`PeriodSnapshot` — never
    raises), :meth:`compute_periodogram` (run the Lomb-Scargle periodogram of
    the project's lightcurve on demand), :meth:`select_peak` (choose a peak —
    persisted), :meth:`set_manual_period` (enter a period in days —
    persisted), :meth:`clear` (reset — persisted as null),
    :meth:`set_project` (bind a project + load its persisted period),
    :meth:`refresh` (re-emit), and listeners with frozen snapshots.
    """

    def __init__(
        self,
        lightcurve_controller: LightcurveController | None = None,
        project: Any | None = None,
        *,
        invoke_on_main: InvokeOnMain | None = None,
        top_n: int = DEFAULT_TOP_N,
    ) -> None:
        self._invoke_on_main: InvokeOnMain = invoke_on_main or _direct_invoke
        self._lock = threading.Lock()
        self._listeners: list[PeriodListener] = []
        self._db_thread_id: int | None = None
        if lightcurve_controller is None:
            lightcurve_controller = LightcurveController(
                project, invoke_on_main=self._invoke_on_main
            )
        self._lc = lightcurve_controller
        self._top_n = top_n
        self._period: float | None = None
        self._source: str | None = None
        self._peak_index: int | None = None
        self._result: PeriodogramResult | None = None
        self._error: str | None = None
        self._loaded_project: Any | None = None  # project whose period was loaded
        if self._lc.project is not None:
            self._db_thread_id = threading.get_ident()
            self._load_persisted_period(self._lc.project)
        self._lc.add_listener(self._on_lightcurve_snapshot)

    # -- reading ---------------------------------------------------------------

    @property
    def lightcurve_controller(self) -> LightcurveController:
        """The lightcurve controller this controller follows."""
        return self._lc

    @property
    def project(self) -> Any | None:
        """The current project (the lightcurve controller's)."""
        return self._lc.project

    @property
    def invoke_on_main(self) -> InvokeOnMain:
        """The marshalling callable (the app binds ``GLib.idle_add``)."""
        return self._invoke_on_main

    @property
    def top_n(self) -> int:
        """The number of peaks kept by :meth:`compute_periodogram`."""
        return self._top_n

    @property
    def period(self) -> float | None:
        """The currently selected period in days (None when none)."""
        return self._period

    @property
    def source(self) -> str | None:
        """Where the current period came from: ``'peak'`` / ``'manual'`` / None."""
        return self._source

    @property
    def result(self) -> PeriodogramResult | None:
        """The last computed periodogram result (None before the first
        :meth:`compute_periodogram`)."""
        return self._result

    @property
    def listeners(self) -> tuple[PeriodListener, ...]:
        """The registered observer callbacks, in registration order."""
        return tuple(self._listeners)

    @property
    def points(self) -> tuple[LightcurvePoint, ...]:
        """The current lightcurve points (the bound lightcurve controller's
        snapshot points — ALL sessions, excluded included)."""
        return self._lc.snapshot.points

    @property
    def snapshot(self) -> PeriodSnapshot:
        """The frozen :class:`PeriodSnapshot` of the current state. NEVER
        raises: a fold failure (or a lightcurve read failure) is surfaced as
        ``error``."""
        lc_snapshot = self._lc.snapshot
        points = lc_snapshot.points
        jd0 = min((p.jd for p in points), default=None)
        folded: tuple[FoldedPoint, ...] = ()
        error = self._error if self._error is not None else lc_snapshot.error
        if self._period is not None and points:
            try:
                folded = fold_points(points, self._period, jd0=jd0)
            except FoldError as exc:  # surfaced as snapshot.error, never a crash
                logger.info("fold failed: %s", exc)
                error = f"{type(exc).__name__}: {exc}"
                folded = ()
        peaks = self._result.peaks if self._result is not None else ()
        return PeriodSnapshot(
            period=self._period,
            source=self._source,
            peak_index=self._peak_index,
            jd0=jd0,
            result=self._result,
            peaks=peaks,
            folded=folded,
            error=error,
        )

    def folded_points(self) -> tuple[FoldedPoint, ...]:
        """The folded points of the current period (sorted by phase); ``()``
        when no period is selected or there are no points."""
        return self.snapshot.folded

    # -- operations ---------------------------------------------------------------

    def compute_periodogram(self, **kwargs: Any) -> PeriodogramResult:
        """Compute the Lomb-Scargle periodogram of the project's lightcurve
        ON DEMAND via
        :func:`~seestar_controller.analysis.periodogram.project_periodogram`
        (excluded points filtered, like the store default; ``kwargs``
        forwarded, e.g. ``include_excluded=True``; ``top_n`` defaults to the
        controller's). Stores the result, clears a previous error, notifies
        the listeners, and returns it.

        Raises :class:`PeriodError` when there is no project or the
        computation fails (wrapped from
        :class:`~seestar_controller.analysis.periodogram.PeriodogramError`).
        """
        project = self.project
        if project is None:
            raise PeriodError("no project — cannot compute a periodogram")
        kwargs.setdefault("top_n", self._top_n)
        try:
            result = self._call_store(
                lambda: project_periodogram(project, **kwargs)
            )
        except Exception as exc:
            self._error = f"{type(exc).__name__}: {exc}"
            self._emit(self.snapshot)
            raise PeriodError(f"periodogram failed: {exc}") from exc
        self._result = result
        self._error = None
        self._emit(self.snapshot)
        return result

    def select_peak(self, index: int) -> float:
        """Select the periodogram peak ``index`` (into
        :attr:`snapshot.peaks`): sets the period to ``peaks[index].period``
        with source ``'peak'``, PERSISTS it into the project's
        ``project.json``, recomputes the folded points, notifies, and returns
        the period.

        Raises :class:`PeriodError` when no periodogram has been computed yet
        or ``index`` is out of range (nothing is changed or persisted then).
        """
        if isinstance(index, bool) or not isinstance(index, int):
            raise PeriodError(f"peak index must be an int, got {index!r}")
        result = self._result
        if result is None:
            raise PeriodError(
                "no periodogram computed yet — call compute_periodogram() first"
            )
        if not 0 <= index < len(result.peaks):
            raise PeriodError(
                f"peak index {index} out of range — the periodogram has "
                f"{len(result.peaks)} peak(s)"
            )
        period = result.peaks[index].period
        self._apply_selection(period, SOURCE_PEAK, index)
        return period

    def set_manual_period(self, days: Any) -> float:
        """Enter a period manually (days): validates it
        (:func:`validate_period`), sets it with source ``'manual'``,
        PERSISTS it into the project's ``project.json``, recomputes the folded
        points, notifies, and returns the period.

        Raises :class:`FoldError` for an invalid period (nothing is changed or
        persisted then).
        """
        period = validate_period(days)  # raises FoldError on invalid input
        self._apply_selection(period, SOURCE_MANUAL, None)
        return period

    def clear(self) -> None:
        """Reset the selection (period None, source None), persist null into
        the project's ``project.json``, and notify."""
        self._persist(None)
        self._period = None
        self._source = None
        self._peak_index = None
        self._emit(self.snapshot)

    def refresh(self) -> PeriodSnapshot:
        """Re-emit the current snapshot to the listeners (e.g. after the
        lightcurve changed) and return it."""
        self._emit(self.snapshot)
        return self.snapshot

    def set_project(self, project: Any | None) -> None:
        """Bind an explicit project on the underlying lightcurve controller
        (None clears it), capture the db-owning thread, and let the
        lightcurve listener load the persisted period from the project's
        ``project.json`` (``config['period']`` — a corrupt value is ignored
        with a warning) and notify.

        The restored period is reported with source ``'manual'`` (only the
        period is persisted — documented choice).
        """
        if project is not None:
            self._db_thread_id = threading.get_ident()
        self._lc.set_project(project)  # emits -> _on_lightcurve_snapshot loads + emits

    # -- selection + persistence -----------------------------------------------------

    def _apply_selection(self, period: float, source: str, peak_index: int | None) -> None:
        """Validate-persist-apply: persist the period FIRST (a failure raises
        :class:`PeriodError` and leaves the in-memory state unchanged), then
        apply and notify."""
        self._persist(period)
        self._period = period
        self._source = source
        self._peak_index = peak_index
        self._error = None
        self._emit(self.snapshot)

    def _persist(self, period: float | None) -> None:
        """Write ``period`` (days, or None) into the current project's
        ``project.json`` (``config['period']``) via
        :meth:`~seestar_controller.store.project.Project.save_config` —
        through :meth:`_call_store` (the config write is a store call). With
        no project set this is a no-op (in-memory only). A failure raises
        :class:`PeriodError` chained from the cause."""
        project = self.project
        if project is None:
            return
        try:
            self._call_store(lambda: self._write_period_config(project, period))
        except PeriodError:
            raise
        except Exception as exc:
            raise PeriodError(
                f"cannot persist the period into project "
                f"{getattr(project, 'slug', project)!r}: {exc}"
            ) from exc

    @staticmethod
    def _write_period_config(project: Any, period: float | None) -> None:
        """Set ``config['period']`` and atomically save ``project.json``."""
        project.config[PERIOD_CONFIG_KEY] = None if period is None else float(period)
        project.save_config()

    def _load_persisted_period(self, project: Any) -> None:
        """Load the persisted period from ``project.config['period']`` (read
        through :meth:`_call_store`): a valid float > 0 becomes the current
        period with source ``'manual'``; null/missing clears the selection; a
        corrupt value is ignored with a logged warning (never a crash)."""
        if project is self._loaded_project:
            return
        self._loaded_project = project
        try:
            raw = self._call_store(lambda: project.config.get(PERIOD_CONFIG_KEY))
        except Exception as exc:  # noqa: BLE001
            logger.warning("cannot read the persisted period of %r: %s", project, exc)
            return
        if raw is None:
            self._period = None
            self._source = None
            self._peak_index = None
            return
        try:
            period = validate_period(raw)
        except FoldError as exc:
            logger.warning(
                "ignoring the corrupt persisted period %r of project %r: %s",
                raw,
                getattr(project, "slug", project),
                exc,
            )
            return
        self._period = period
        self._source = SOURCE_MANUAL  # only the period is persisted
        self._peak_index = None

    # -- lightcurve listener -----------------------------------------------------------

    def _on_lightcurve_snapshot(self, _snapshot: Any) -> None:
        """The bound lightcurve controller changed (project switch, refresh):
        adopt a changed project's persisted period (clear the selection when
        the project is cleared) and re-emit our snapshot (the folded points
        follow the new points)."""
        project = self._lc.project
        if project is None:
            if self._loaded_project is not None:
                self._loaded_project = None
                self._period = None
                self._source = None
                self._peak_index = None
        elif project is not self._loaded_project:
            self._db_thread_id = threading.get_ident()
            self._load_persisted_period(project)
        self._emit(self.snapshot)

    # -- listeners ---------------------------------------------------------------

    def add_listener(self, listener: PeriodListener) -> PeriodListener:
        """Register an observer callback (idempotent); returns it, so it can
        be used as a decorator. The listener receives a frozen
        :class:`PeriodSnapshot` on any change (marshalled via
        ``invoke_on_main``)."""
        if listener not in self._listeners:
            self._listeners.append(listener)
        return listener

    def remove_listener(self, listener: PeriodListener) -> None:
        """Unregister an observer callback (no-op when not registered)."""
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _notify(self, snapshot: PeriodSnapshot) -> None:
        """Hand a frozen snapshot to every listener; a failing listener is
        logged and isolated (never breaks the others or the caller)."""
        for listener in list(self._listeners):
            try:
                listener(snapshot)
            except Exception:
                logger.exception("period listener failed")

    def _emit(self, snapshot: PeriodSnapshot) -> None:
        """Marshal a listener notification through ``invoke_on_main``."""
        self._invoke_on_main(lambda: self._notify(snapshot))

    # -- store marshalling (sqlite is thread-affine) --------------------------------

    def _call_store(self, fn: Callable[[], Any]) -> Any:
        """Run the store callable ``fn`` and return its result.

        When already on the db-owning thread (or when no db thread was
        captured), ``fn`` runs directly. Otherwise it is marshalled through
        ``invoke_on_main`` (the app: ``GLib.idle_add``) and the calling thread
        waits for the result — the same pattern as
        :class:`~seestar_controller.analysis.lightcurve.LightcurveController`.
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
            raise PeriodError(
                "timed out waiting for the project db thread — is the main "
                "loop running?"
            )
        if "error" in box:
            raise box["error"]
        return box["result"]
