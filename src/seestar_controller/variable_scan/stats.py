"""Pure robust-statistics helpers of the variable scan.

Pure Python (stdlib only) — no GTK, no link, no numpy: the math is
unit-tested on its own, exactly like the photometry math in
:mod:`seestar_controller.photometry.measure`.

- :func:`median` — the median of a finite float sequence;
- :func:`mad` — the **median absolute deviation** (raw, NOT scaled by
  1.4826 — documented choice: the scan compares the raw MAD against the
  median photometric error, so the ratio stays a plain
  "scatter in units of the typical error" index);
- :func:`variability_index` — ``mad / median_err``, the per-star
  variability ratio the candidates are ranked by.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence


def _finite_floats(values: Sequence[float], *, what: str) -> list[float]:
    """Coerce ``values`` to a list of finite floats (bools rejected);
    :class:`ValueError` otherwise."""
    result: list[float] = []
    for value in values:
        if isinstance(value, bool):
            raise ValueError(f"{what} must contain only numbers, got {value!r}")  # noqa: TRY004
        try:
            f = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{what} must contain only numbers, got {value!r}") from None
        if not math.isfinite(f):
            raise ValueError(f"{what} must contain only finite numbers, got {value!r}")
        result.append(f)
    return result


def median(values: Sequence[float]) -> float:
    """The median of ``values`` (finite floats). Raises :class:`ValueError`
    on an empty sequence or a non-finite/non-numeric entry."""
    vals = _finite_floats(values, what="values")
    if not vals:
        raise ValueError("median of an empty sequence")
    return float(statistics.median(vals))


def mad(values: Sequence[float]) -> float:
    """The **median absolute deviation** of ``values``:
    ``median(|v - median(v)|)`` — the raw, unscaled robust scatter.
    Raises :class:`ValueError` on an empty sequence or a non-finite entry."""
    vals = _finite_floats(values, what="values")
    if not vals:
        raise ValueError("mad of an empty sequence")
    med = statistics.median(vals)
    return float(statistics.median([abs(v - med) for v in vals]))


def variability_index(scatter: float, median_err: float) -> float:
    """The variability ratio ``scatter / median_err`` (both finite,
    non-negative; bools rejected).

    A zero ``median_err`` with a positive scatter means "infinitely
    variable" and yields :data:`math.inf`; a zero scatter yields ``0.0``
    regardless of the error. Raises :class:`ValueError` on negative or
    non-finite inputs.
    """
    s = _finite_floats([scatter], what="scatter")[0]
    e = _finite_floats([median_err], what="median_err")[0]
    if s < 0.0:
        raise ValueError(f"scatter must be >= 0, got {scatter!r}")
    if e < 0.0:
        raise ValueError(f"median_err must be >= 0, got {median_err!r}")
    if e > 0.0:
        return s / e
    return math.inf if s > 0.0 else 0.0
