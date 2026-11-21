"""The least-squares transit fit — the PURE core of transit-fit ticket 01
(:mod:`seestar_controller.transit.fit`).

This is a PURE module: **no ``gi`` import** (the sandbox has no GTK, see
docs/wiki/assumptions.md; a subprocess test enforces the boundary) —
:mod:`numpy` + :mod:`scipy.optimize` + stdlib + the sibling pure modules
(:mod:`.model`, :mod:`seestar_controller.analysis.fold`,
:mod:`seestar_controller.store`). No :mod:`seestar_controller.link`, no
``ui``, no ``photometry``, no ``enhance`` imports, and no network.

What lives here:

- :class:`TransitParams` — the frozen parameter set ``t0`` (mid-transit
  phase, wrapped to ``[0, 1)``), ``depth`` (magnitudes, >= 0), ``duration``
  (phase units, in ``(0, 1)``) and ``period`` (days, > 0), with
  :attr:`~TransitParams.duration_days` and a JSON-able ``to_dict`` /
  ``from_dict``;
- :class:`TransitErrors` — the frozen 1-sigma uncertainties matching
  :class:`TransitParams` (``period == 0.0`` marks a period that was held
  fixed);
- :class:`FitResult` — the frozen fit result: ``params`` and 1-sigma
  ``errors`` (:class:`TransitParams` / :class:`TransitErrors`), the raw
  ``residuals`` (data - model, magnitudes),
  ``chi2``, ``n_points``, ``dof``, ``rms``, the fitted ``model_mags``, and
  ``period_fixed`` / ``success`` / ``message``;
- :func:`fit_transit` — fit the :func:`~.model.transit_model` trapezoid to a
  folded lightcurve with :func:`scipy.optimize.least_squares` on the
  inverse-variance-weighted residuals ``(mags - model) / errs``; free
  parameters ``t0``, ``depth``, ``duration`` and — only when
  ``fit_period=True`` (then ``jd`` is required and the phases are recomputed
  from the trial period inside the residual) — the ``period``. The
  parameter uncertainties come from the covariance matrix
  ``cov = inv(J^T J) * chi2 / dof`` (pinv fallback when singular) evaluated at
  the solution, ``errors = sqrt(diag(cov))``;
- :func:`fit_project_transit` — the store-backed wrapper: read the project's
  lightcurve, take the period from the argument or from the project config
  (``config['period']``, as persisted by period-analysis), fold with
  :func:`seestar_controller.analysis.fold.fold_points`, and fit.

Conventions (documented choices):

- **Weighting.** ``errs=None`` means uniform weights (all 1); otherwise every
  error must be finite and > 0. Points with unknown errors in a project
  lightcurve are replaced by the median of the known errors (see
  :func:`fit_project_transit`).
- **t0 is circular.** The model is periodic in ``t0`` with period 1, so ``t0``
  is fitted unbounded and reported wrapped into ``[0, 1)``.
- **chi2 scaling.** The covariance is scaled by ``chi2 / dof`` (the standard
  practice for weighted least squares); with ``dof <= 0`` the scale falls
  back to 1.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
from scipy.optimize import least_squares

from seestar_controller.analysis.fold import (
    PERIOD_CONFIG_KEY,
    FoldError,
    fold_points,
    validate_period,
)
from seestar_controller.store import lightcurve as store_lightcurve

from .model import TransitError, transit_model

#: Lower bound for the fitted duration / period (phase units / days).
_MIN_POSITIVE = 1e-12


@dataclass(frozen=True)
class TransitParams:
    """One frozen transit parameter set.

    - ``t0`` — mid-transit phase, wrapped into ``[0, 1)`` on construction;
    - ``depth`` — transit depth in magnitudes, finite, >= 0 (positive =
      fainter at mid-transit);
    - ``duration`` — total transit duration in PHASE units, in ``(0, 1)``;
    - ``period`` — the period in days, finite, > 0 (the fixed project period
      or the fitted one).
    """

    t0: float
    depth: float
    duration: float
    period: float

    def __post_init__(self) -> None:
        for name in ("t0", "depth", "duration", "period"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TransitError(f"TransitParams.{name} must be a number, got {value!r}")
            if not math.isfinite(float(value)):
                raise TransitError(f"TransitParams.{name} must be finite, got {value!r}")
        if self.depth < 0.0:
            raise TransitError(f"TransitParams.depth must be >= 0, got {self.depth!r}")
        if not 0.0 < self.duration < 1.0:
            raise TransitError(f"TransitParams.duration must be in (0, 1), got {self.duration!r}")
        if self.period <= 0.0:
            raise TransitError(f"TransitParams.period must be > 0, got {self.period!r}")
        object.__setattr__(self, "t0", float(self.t0) % 1.0)

    @property
    def duration_days(self) -> float:
        """The total transit duration in days (``duration * period``)."""
        return self.duration * self.period

    def to_dict(self) -> dict[str, float]:
        """A JSON-able dict of the four parameters."""
        return {
            "t0": float(self.t0),
            "depth": float(self.depth),
            "duration": float(self.duration),
            "period": float(self.period),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> TransitParams:
        """Build from a dict (unknown keys ignored); :class:`TransitError` on
        anything malformed."""
        if not isinstance(raw, dict):
            raise TransitError(f"transit params must be a dict, got {raw!r}")
        try:
            return cls(
                t0=raw["t0"],
                depth=raw["depth"],
                duration=raw["duration"],
                period=raw["period"],
            )
        except KeyError as exc:
            raise TransitError(f"transit params missing the key {exc}") from exc
        except TypeError as exc:
            raise TransitError(f"malformed transit params: {exc}") from exc


@dataclass(frozen=True)
class TransitErrors:
    """One frozen set of 1-sigma parameter uncertainties (magnitudes / phase
    units / days, matching :class:`TransitParams`). Every value is finite and
    >= 0; ``period == 0.0`` marks a period that was held FIXED (not fitted)."""

    t0: float
    depth: float
    duration: float
    period: float

    def __post_init__(self) -> None:
        for name in ("t0", "depth", "duration", "period"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TransitError(f"TransitErrors.{name} must be a number, got {value!r}")
            if not math.isfinite(float(value)) or float(value) < 0.0:
                raise TransitError(f"TransitErrors.{name} must be finite and >= 0, got {value!r}")

    def to_dict(self) -> dict[str, float]:
        """A JSON-able dict of the four uncertainties."""
        return {
            "t0": float(self.t0),
            "depth": float(self.depth),
            "duration": float(self.duration),
            "period": float(self.period),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> TransitErrors:
        """Build from a dict (unknown keys ignored); :class:`TransitError` on
        anything malformed."""
        if not isinstance(raw, dict):
            raise TransitError(f"transit errors must be a dict, got {raw!r}")
        try:
            return cls(
                t0=raw["t0"],
                depth=raw["depth"],
                duration=raw["duration"],
                period=raw["period"],
            )
        except KeyError as exc:
            raise TransitError(f"transit errors missing the key {exc}") from exc
        except TypeError as exc:
            raise TransitError(f"malformed transit errors: {exc}") from exc


@dataclass(frozen=True, eq=False)
class FitResult:
    """One frozen least-squares transit fit result.

    - ``params`` / ``errors`` — the best-fit :class:`TransitParams` and the
      1-sigma :class:`TransitErrors` from the covariance matrix
      (``errors.period`` is 0.0 when the period was held fixed);
    - ``residuals`` — the raw residuals ``mags - model`` in magnitudes;
    - ``chi2`` — the weighted chi-square ``sum(((mags - model) / errs)^2)``;
    - ``n_points`` / ``dof`` — the point count and ``n_points - n_params``;
    - ``rms`` — the root-mean-square of the raw residuals;
    - ``model_mags`` — the fitted model at the input phases (recomputed from
      ``jd`` with the fitted period when the period was free);
    - ``period_fixed`` — True when the period was held fixed;
    - ``success`` / ``message`` — the :func:`scipy.optimize.least_squares`
      status.
    """

    params: TransitParams
    errors: TransitErrors
    residuals: np.ndarray
    chi2: float
    n_points: int
    dof: int
    rms: float
    model_mags: np.ndarray
    period_fixed: bool
    success: bool
    message: str

    def __post_init__(self) -> None:
        if not isinstance(self.params, TransitParams):
            raise TransitError(f"FitResult.params must be TransitParams, got {self.params!r}")
        if not isinstance(self.errors, TransitErrors):
            raise TransitError(f"FitResult.errors must be TransitErrors, got {self.errors!r}")
        for name in ("residuals", "model_mags"):
            value = getattr(self, name)
            if not isinstance(value, np.ndarray):
                raise TransitError(f"FitResult.{name} must be a numpy array, got {value!r}")
            if value.ndim != 1 or value.shape != self.residuals.shape:
                raise TransitError(f"FitResult.{name} must be 1-D with the residuals' shape")
            if not np.all(np.isfinite(value)):
                raise TransitError(f"FitResult.{name} must be finite")
        if self.residuals.shape != self.model_mags.shape:
            raise TransitError("FitResult.residuals and model_mags must have the same shape")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, FitResult):
            return NotImplemented
        return (
            self.params == other.params
            and self.errors == other.errors
            and np.array_equal(self.residuals, other.residuals)
            and np.array_equal(self.model_mags, other.model_mags)
            and self.chi2 == other.chi2
            and self.n_points == other.n_points
            and self.dof == other.dof
            and self.rms == other.rms
            and self.period_fixed == other.period_fixed
            and self.success == other.success
            and self.message == other.message
        )

    def to_dict(self) -> dict[str, Any]:
        """A JSON-able dict of the whole result (arrays become lists)."""
        return {
            "params": self.params.to_dict(),
            "errors": self.errors.to_dict(),
            "residuals": [float(v) for v in self.residuals],
            "chi2": float(self.chi2),
            "n_points": int(self.n_points),
            "dof": int(self.dof),
            "rms": float(self.rms),
            "model_mags": [float(v) for v in self.model_mags],
            "period_fixed": bool(self.period_fixed),
            "success": bool(self.success),
            "message": str(self.message),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> FitResult:
        """Rebuild from :meth:`to_dict` output (unknown keys ignored);
        :class:`TransitError` on anything malformed."""
        if not isinstance(raw, dict):
            raise TransitError(f"fit result must be a dict, got {raw!r}")
        try:
            return cls(
                params=TransitParams.from_dict(raw["params"]),
                errors=TransitErrors.from_dict(raw["errors"]),
                residuals=np.asarray(raw["residuals"], dtype=float),
                chi2=raw["chi2"],
                n_points=raw["n_points"],
                dof=raw["dof"],
                rms=raw["rms"],
                model_mags=np.asarray(raw["model_mags"], dtype=float),
                period_fixed=raw["period_fixed"],
                success=raw["success"],
                message=raw["message"],
            )
        except KeyError as exc:
            raise TransitError(f"fit result missing the key {exc}") from exc
        except TypeError as exc:
            raise TransitError(f"malformed fit result: {exc}") from exc


def _as_1d_float_array(values: Any, what: str) -> np.ndarray:
    try:
        array = np.asarray(values, dtype=float)
    except (TypeError, ValueError) as exc:
        raise TransitError(f"{what} must be a 1-D array of numbers: {exc}") from exc
    if array.ndim != 1:
        raise TransitError(f"{what} must be 1-D, got shape {array.shape}")
    if not np.all(np.isfinite(array)):
        raise TransitError(f"{what} must be finite (no NaN/inf)")
    return array


def _validate_guess(value: Any, what: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TransitError(f"{what} must be a number, got {value!r}")
    result = float(value)
    if not math.isfinite(result):
        raise TransitError(f"{what} must be finite, got {value!r}")
    return result


def fit_transit(
    phases: Any,
    mags: Any,
    errs: Any,
    period: Any,
    *,
    jd: Any = None,
    jd0: Any = None,
    fit_period: bool = False,
    t0_guess: Any = None,
    depth_guess: Any = None,
    duration_guess: Any = None,
    max_nfev: int | None = None,
) -> FitResult:
    """Fit the trapezoid transit model to a folded lightcurve.

    - ``phases`` — the folded phases in ``[0, 1)`` (any finite reals; wrapped
      by the model), 1-D, length ``n``;
    - ``mags`` — the differential magnitudes, 1-D, length ``n``;
    - ``errs`` — the magnitude uncertainties, 1-D, length ``n``, each finite
      and > 0; None means uniform weights (all 1);
    - ``period`` — the period in days (finite, > 0); held FIXED by default;
    - ``jd`` — the Julian Dates of the points (1-D, length ``n``); REQUIRED
      when ``fit_period=True`` (the phases are then recomputed from the trial
      period inside the residual), ignored otherwise;
    - ``jd0`` — the epoch for the phase recomputation when ``fit_period=True``;
      None (default) uses ``min(jd)``;
    - ``fit_period`` — when True, the period is a fourth free parameter
      (``jd`` required); the returned ``params.period`` is the fitted period
      and ``period_fixed`` is False;
    - ``t0_guess`` / ``depth_guess`` / ``duration_guess`` — optional initial
      guesses (defaults: the phase of the faintest point, the max-minus-median
      depth, and 0.1 phase units);
    - ``max_nfev`` — optional cap on the least-squares evaluations.

    Requires ``n > n_params`` (so ``dof >= 1``). Returns the frozen
    :class:`FitResult`. Raises :class:`TransitError` for any invalid input.
    """
    phase_array = _as_1d_float_array(phases, "phases")
    mag_array = _as_1d_float_array(mags, "mags")
    if phase_array.shape != mag_array.shape:
        raise TransitError(
            f"phases and mags must have the same length, got {phase_array.shape} vs {mag_array.shape}"
        )
    n_points = int(phase_array.size)
    if errs is None:
        err_array = np.ones(n_points, dtype=float)
    else:
        err_array = _as_1d_float_array(errs, "errs")
        if err_array.shape != phase_array.shape:
            raise TransitError("errs must have the same length as phases/mags")
        if np.any(err_array <= 0.0):
            raise TransitError("errs must all be > 0")
    try:
        period_value = validate_period(period)
    except FoldError as exc:
        raise TransitError(f"invalid period: {exc}") from exc

    fit_period = bool(fit_period)
    n_params = 4 if fit_period else 3
    if n_points <= n_params:
        raise TransitError(
            f"need more than {n_params} points to fit {n_params} parameters, got {n_points}"
        )

    jd_array: np.ndarray | None = None
    jd0_value = 0.0
    if fit_period:
        if jd is None:
            raise TransitError("jd is required when fit_period=True (the period is refit from jd)")
        jd_array = _as_1d_float_array(jd, "jd")
        if jd_array.shape != phase_array.shape:
            raise TransitError("jd must have the same length as phases/mags")
        if jd0 is None:
            jd0_value = float(np.min(jd_array))
        else:
            if isinstance(jd0, bool) or not isinstance(jd0, (int, float)):
                raise TransitError(f"jd0 must be a number (JD), got {jd0!r}")
            jd0_value = float(jd0)
            if not math.isfinite(jd0_value):
                raise TransitError(f"jd0 must be finite, got {jd0!r}")

    t0_guess_value = _validate_guess(t0_guess, "t0_guess")
    depth_guess_value = _validate_guess(depth_guess, "depth_guess")
    duration_guess_value = _validate_guess(duration_guess, "duration_guess")

    def _residuals(p: np.ndarray) -> np.ndarray:
        t0, depth, duration = float(p[0]), float(p[1]), float(p[2])
        if fit_period:
            assert jd_array is not None
            trial_phase = np.mod((jd_array - jd0_value) / float(p[3]), 1.0)
        else:
            trial_phase = phase_array
        model = transit_model(trial_phase, t0, depth, duration)
        return (mag_array - model) / err_array

    if t0_guess_value is None:
        t0_guess_value = float(phase_array[int(np.argmax(mag_array))])
    if depth_guess_value is None:
        depth_guess_value = max(float(np.max(mag_array) - np.median(mag_array)), 1e-3)
    if duration_guess_value is None:
        duration_guess_value = 0.1
    if duration_guess_value <= 0.0 or duration_guess_value >= 1.0:
        raise TransitError(f"duration_guess must be in (0, 1), got {duration_guess!r}")

    lower = np.array([-np.inf, 0.0, _MIN_POSITIVE] + ([_MIN_POSITIVE] if fit_period else []))
    upper = np.array([np.inf, np.inf, 1.0 - 1e-12] + ([np.inf] if fit_period else []))
    x0 = np.array(
        [t0_guess_value, max(depth_guess_value, 0.0), duration_guess_value]
        + ([period_value] if fit_period else [])
    )
    x0 = np.minimum(np.maximum(x0, lower), upper)

    kwargs: dict[str, Any] = {}
    if max_nfev is not None:
        kwargs["max_nfev"] = int(max_nfev)
    res = least_squares(_residuals, x0, bounds=(lower, upper), method="trf", **kwargs)

    t0_fit = float(res.x[0])
    depth_fit = float(res.x[1])
    duration_fit = float(res.x[2])
    period_fit = float(res.x[3]) if fit_period else period_value

    chi2 = float(np.sum(res.fun**2))
    dof = n_points - n_params
    scale = chi2 / dof if dof > 0 else 1.0
    jac = np.asarray(res.jac, dtype=float)
    try:
        cov = np.linalg.inv(jac.T @ jac) * scale
    except np.linalg.LinAlgError:
        cov = np.linalg.pinv(jac.T @ jac) * scale
    error_values = np.sqrt(np.clip(np.diag(cov), 0.0, None))

    params = TransitParams(t0=t0_fit, depth=depth_fit, duration=duration_fit, period=period_fit)
    errors = TransitErrors(
        t0=float(error_values[0]),
        depth=float(error_values[1]),
        duration=float(error_values[2]),
        period=float(error_values[3]) if fit_period else 0.0,
    )

    if fit_period:
        assert jd_array is not None
        model_phase = np.mod((jd_array - jd0_value) / period_fit, 1.0)
    else:
        model_phase = phase_array
    model_mags = transit_model(model_phase, t0_fit, depth_fit, duration_fit)
    residuals = mag_array - model_mags
    rms = float(np.sqrt(np.mean(residuals**2))) if n_points else 0.0

    return FitResult(
        params=params,
        errors=errors,
        residuals=residuals,
        chi2=chi2,
        n_points=n_points,
        dof=dof,
        rms=rms,
        model_mags=model_mags,
        period_fixed=not fit_period,
        success=bool(res.success),
        message=str(res.message),
    )


def fit_project_transit(
    project: Any,
    *,
    period: Any = None,
    jd0: Any = None,
    fit_period: bool = False,
    **fit_kwargs: Any,
) -> FitResult:
    """Fit the transit model to a PROJECT's folded lightcurve (the store-backed
    wrapper around :func:`fit_transit`).

    - the lightcurve is read with
      :func:`seestar_controller.store.measurements.lightcurve` (excluded
      outlier points are filtered out);
    - ``period`` — the period in days; None (default) takes the persisted
      project period ``config['period']`` (as written by period-analysis);
      :class:`TransitError` when neither is available/valid;
    - ``jd0`` — the fold epoch; None (default) uses the first observation
      (min jd), and the same epoch is used for the period refit;
    - ``fit_period`` / ``**fit_kwargs`` — forwarded to :func:`fit_transit`.

    Points with an unknown ``mag_err`` get the median of the known errors (or
    uniform weights when no error is known at all).
    """
    points = store_lightcurve(project)
    if not points:
        raise TransitError(f"project {getattr(project, 'slug', project)!r} has no measurements")
    if period is None:
        raw_period = project.config.get(PERIOD_CONFIG_KEY)
        if raw_period is None:
            raise TransitError(
                f"project {getattr(project, 'slug', project)!r} has no period: pass period=... "
                f"or select one first (config[{PERIOD_CONFIG_KEY!r}] is unset)"
            )
        try:
            period = validate_period(raw_period)
        except FoldError as exc:
            raise TransitError(
                f"project {getattr(project, 'slug', project)!r} has a corrupt persisted period "
                f"{raw_period!r}: {exc}"
            ) from exc
    else:
        try:
            period = validate_period(period)
        except FoldError as exc:
            raise TransitError(f"invalid period: {exc}") from exc

    jds = np.array([float(pt.jd) for pt in points], dtype=float)
    epoch = float(np.min(jds)) if jd0 is None else float(jd0)
    folded = fold_points(points, period, epoch)
    phases = np.array([fp.phase for fp in folded], dtype=float)
    mags = np.array([float(fp.point.mag) for fp in folded], dtype=float)
    raw_errs = [fp.point.mag_err for fp in folded]
    known: list[float] = []
    cleaned: list[float | None] = []
    for raw_err in raw_errs:
        if raw_err is None:
            cleaned.append(None)
            continue
        value = float(raw_err)
        if math.isfinite(value) and value > 0.0:
            known.append(value)
            cleaned.append(value)
        else:
            cleaned.append(None)
    if known:
        fill = float(np.median(known))
        errs = np.array([fill if e is None else e for e in cleaned], dtype=float)
    else:
        errs = None
    return fit_transit(
        phases,
        mags,
        errs,
        period,
        jd=jds,
        jd0=epoch,
        fit_period=fit_period,
        **fit_kwargs,
    )
