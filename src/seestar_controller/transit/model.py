"""The analytic transit model — the PURE core of transit-fit ticket 01
(:mod:`seestar_controller.transit.model`).

This is a PURE module: **no ``gi`` import** (the sandbox has no GTK, see
docs/wiki/assumptions.md; a subprocess test enforces the boundary) —
:mod:`numpy` + stdlib only. No :mod:`seestar_controller.link`, no ``ui``, no
``photometry``, no ``enhance`` imports, and no network.

What lives here:

- :class:`TransitError` — the error type (a :class:`ValueError`);
- :data:`INGRESS_FRACTION` — the fraction of the total transit duration used
  for EACH of the ingress and egress ramps of the trapezoid;
- :func:`transit_model` — the analytic Mandel-Agol-style **trapezoid** transit
  model in numpy (no ``batman`` dependency): given the phase(s) in ``[0, 1)``,
  the mid-transit phase ``t0``, the transit ``depth`` (magnitudes — positive
  depth means FAINTER at mid-transit, since the magnitude scale is inverted)
  and the total ``duration`` (in phase units, a fraction of the period), it
  returns the model differential magnitudes ``depth * w(phase)`` with the
  trapezoid weight ``w`` in ``[0, 1]``: 0 outside the transit, 1 in the
  full-transit core, and a linear ramp of width ``duration * INGRESS_FRACTION``
  on each side (a box model in the limit of a vanishing ramp). The phase
  distance to ``t0`` is CIRCULAR (the lightcurve is periodic), so a transit
  straddling phase 0/1 is handled exactly.

The model is deliberately simple (depth + duration parameterization, no limb
darkening, no physical Rp/Rs — see the spec's out-of-scope): it is the
workhorse for the least-squares fit in :mod:`.fit`.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np


class TransitError(ValueError):
    """Raised for invalid transit-model / fit inputs (a :class:`ValueError`)."""


#: Fraction of the total transit duration used for EACH ingress/egress ramp
#: of the trapezoid (must be <= 0.5 so the ramp never exceeds half the
#: duration). 0.25 gives a flat full-transit core of half the total duration.
INGRESS_FRACTION = 0.25


def _finite_float(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, np.floating, np.integer)):
        raise TransitError(f"{what} must be a number, got {value!r}")
    result = float(value)
    if not math.isfinite(result):
        raise TransitError(f"{what} must be finite, got {value!r}")
    return result


def transit_model(phase: Any, t0: Any, depth: Any, duration: Any) -> np.ndarray:
    """The analytic trapezoid transit model: model mags at ``phase``.

    ``model = depth * w(phase)`` with the trapezoid weight ``w`` in ``[0, 1]``
    (baseline 0 — these are differential magnitudes):

    - ``d`` — the CIRCULAR phase distance to ``t0`` in ``[0, 0.5]``:
      ``d = |((phase - t0 + 0.5) mod 1) - 0.5|``;
    - ``half = duration / 2`` — half the total duration (phase units);
    - ``ramp = duration * INGRESS_FRACTION`` — the ingress/egress width;
    - ``w = 0`` for ``d >= half`` (out of transit), ``w = 1`` for
      ``d <= half - ramp`` (full transit), linear in between.

    - ``phase`` — a scalar or array of phases (any real values; wrapped mod 1);
      must be finite. Returns a float :class:`numpy.ndarray` of the broadcast
      shape (a 0-d array for a scalar input).
    - ``t0`` — the mid-transit phase (any finite number; the model is periodic
      in ``t0`` with period 1, so values outside ``[0, 1)`` are fine);
    - ``depth`` — the transit depth in magnitudes; finite, ``>= 0`` (positive
      = fainter at mid-transit);
    - ``duration`` — the total transit duration in PHASE units (a fraction of
      the period); finite, strictly between 0 and 1.

    Raises :class:`TransitError` for any invalid input.
    """
    t0_value = _finite_float(t0, "t0")
    depth_value = _finite_float(depth, "depth")
    if depth_value < 0.0:
        raise TransitError(f"depth must be >= 0 (magnitudes), got {depth!r}")
    duration_value = _finite_float(duration, "duration")
    if not 0.0 < duration_value < 1.0:
        raise TransitError(f"duration must be in (0, 1) phase units, got {duration!r}")
    try:
        phase_array = np.asarray(phase, dtype=float)
    except (TypeError, ValueError) as exc:
        raise TransitError(f"phase must be a number or an array of numbers: {exc}") from exc
    if not np.all(np.isfinite(phase_array)):
        raise TransitError("phase must be finite (no NaN/inf)")

    wrapped = np.mod(phase_array - t0_value + 0.5, 1.0) - 0.5  # in [-0.5, 0.5)
    distance = np.abs(wrapped)  # circular distance to t0, in [0, 0.5]
    half = duration_value / 2.0
    ramp = duration_value * INGRESS_FRACTION
    weight = np.clip((half - distance) / ramp, 0.0, 1.0)
    return depth_value * weight
