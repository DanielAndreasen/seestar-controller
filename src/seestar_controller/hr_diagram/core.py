"""CMD / H-R computation core — the PURE layer of hr-diagram ticket 01.

This is a PURE module: **no ``gi`` import** (the sandbox has no GTK, see
docs/wiki/assumptions.md; a subprocess test enforces the boundary) — stdlib
only (:mod:`math`, :mod:`dataclasses`, :mod:`collections.abc`). It imports
**nothing** from :mod:`seestar_controller` at all: the cross-match table is
consumed duck-typed (mappings or objects with attributes, e.g.
:class:`seestar_controller.store.crossmatch.CrossmatchRecord` from
:func:`seestar_controller.store.crossmatch.load_crossmatch` or a plain dict),
so this package is a READ-ONLY consumer of the gaia-crossmatch table API —
no store schema knowledge, no writes.

What lives here:

- :class:`HrError` — the error type (a :class:`ValueError`);
- :class:`HrPoint` — one frozen diagram point (``source_id``, ``g``,
  ``bp_rp``, ``abs_g``, the ``member`` flag, and the astrometry + errors);
- :class:`Membership` — the frozen result of :func:`membership_filter`;
- :func:`absolute_g` — the absolute-G math
  ``M_G = G + 5 - 5*log10(1000/parallax_mas)``;
- :func:`membership_filter` — sigma-clip on parallax + proper motion around
  the median;
- :func:`cmd_points` — the apparent CMD points (G vs BP-RP);
- :func:`hr_points` — the H-R points (absolute G vs BP-RP) with the parallax
  SNR cut.

Documented choices:

- **Duck-typed table.** ``table`` is any iterable of rows; each row is read
  by mapping key first, then attribute (the same rule as
  :func:`seestar_controller.store.crossmatch.save_crossmatch`). Missing
  optional fields read as ``None``. The table is materialized ONCE (a
  generator argument is consumed exactly once, never twice).
- **Points need complete photometry.** Only ``matched`` rows with a
  ``source_id`` and finite ``g`` / ``bp`` / ``rp`` produce a point; anything
  else (unmatched detections, incomplete Gaia rows) is skipped — a diagram
  point without a color is meaningless. :func:`cmd_points` keeps every such
  row; :func:`hr_points` additionally applies the parallax SNR cut (below).
- **Absolute G.** ``abs_g = g + 5 - 5*log10(1000/parallax_mas)`` (the standard
  ``M = m + 5 + 5*log10(parallax_arcsec)`` with the parallax in mas); it is
  ``None`` when the row has no positive parallax. :func:`hr_points` only
  returns rows whose parallax passes the SNR cut
  ``parallax / parallax_error > parallax_snr`` (default
  :data:`DEFAULT_PARALLAX_SNR` = 5.0, strictly greater), so every H-R point
  has a finite ``abs_g``; rows failing the cut are dropped from the H-R
  list (they stay available via :func:`cmd_points`).
- **Membership sigma-clip.** :func:`membership_filter` collects the matched
  rows with finite ``parallax`` / ``pmra`` / ``pmdec`` (the sample), computes
  the median and a robust sigma per quantity (``1.4826 * MAD``, falling back
  to the population standard deviation when the MAD is 0), and flags a row
  as a member iff it lies within ``sigma_clip`` (default
  :data:`DEFAULT_SIGMA_CLIP` = 3.0) of the median in ALL THREE quantities.
  A quantity with zero spread does not clip. Rows outside the sample
  (missing astrometry) are NOT members. When the sample has fewer than
  :data:`MIN_MEMBERSHIP_SAMPLE` rows there is nothing to clip against, so
  every matched row with a ``source_id`` is a member (documented fallback).
- **Errors.** Structural type errors (``table`` not an iterable,
  ``parallax_snr`` / ``sigma_clip`` not a number, ``membership`` not a
  :class:`Membership`) raise :class:`TypeError`; analysis-level value errors
  (non-finite or non-positive ``parallax_snr`` / ``sigma_clip``) raise
  :class:`HrError`. An empty table is not an error — the functions return
  empty results / an all-members :class:`Membership`.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any


class HrError(ValueError):
    """An hr-diagram analysis failure: an invalid ``parallax_snr`` /
    ``sigma_clip`` value, or a table row missing a required field — always a
    clean :class:`ValueError`, never a crash."""


#: Default parallax SNR cut of :func:`hr_points`:
#: ``parallax / parallax_error > 5``.
DEFAULT_PARALLAX_SNR = 5.0

#: Default sigma-clip threshold of :func:`membership_filter` (in robust
#: sigmas around the median).
DEFAULT_SIGMA_CLIP = 3.0

#: Minimum sample size for :func:`membership_filter` to clip at all; below
#: this every matched row is a member (documented fallback).
MIN_MEMBERSHIP_SAMPLE = 3

#: MAD -> Gaussian sigma scale factor (1 / Phi^-1(0.75)).
MAD_TO_SIGMA = 1.4826


@dataclass(frozen=True)
class HrPoint:
    """One frozen diagram point (a CMD or H-R point).

    - ``source_id`` — the matched Gaia DR3 source id;
    - ``x`` / ``y`` — the detection's 0-based pixel position;
    - ``g`` — the apparent G magnitude;
    - ``bp_rp`` — the color ``BP - RP``;
    - ``abs_g`` — the absolute G magnitude
      (:func:`absolute_g`; ``None`` when the row has no positive parallax —
      never ``None`` for :func:`hr_points` points, which pass the SNR cut);
    - ``parallax`` / ``parallax_error`` — the parallax in mas + its error;
    - ``pmra`` / ``pmdec`` — the proper motion in mas/yr;
    - ``member`` — the cluster-membership flag (see :func:`membership_filter`).
    """

    source_id: int
    x: float
    y: float
    g: float
    bp_rp: float
    abs_g: float | None
    parallax: float | None
    parallax_error: float | None
    pmra: float | None
    pmdec: float | None
    member: bool


@dataclass(frozen=True)
class Membership:
    """The frozen result of :func:`membership_filter`.

    - ``member_ids`` — the ``source_id``\\ s flagged as cluster members;
    - ``parallax_median`` / ``parallax_sigma``, ``pmra_median`` /
      ``pmra_sigma``, ``pmdec_median`` / ``pmdec_sigma`` — the median and the
      robust sigma per clipped quantity (``None`` when the sample is empty);
    - ``sigma_clip`` — the threshold used (in sigmas);
    - ``sample_size`` — how many rows entered the clip sample.
    """

    member_ids: frozenset[int]
    parallax_median: float | None
    parallax_sigma: float | None
    pmra_median: float | None
    pmra_sigma: float | None
    pmdec_median: float | None
    pmdec_sigma: float | None
    sigma_clip: float
    sample_size: int


def _get(row: Any, name: str) -> Any:
    """Read the field ``name`` from a duck-typed row: mapping key first, then
    attribute; ``None`` when the row carries neither."""
    if isinstance(row, Mapping):
        return row.get(name)
    return getattr(row, name, None)


def _finite(value: Any) -> float | None:
    """Coerce ``value`` to a finite float (bools rejected); ``None`` when the
    value is None or not a finite number."""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _check_table(table: Any) -> Iterable[Any]:
    """Validate the ``table`` argument (an iterable of rows, not str/bytes);
    :class:`TypeError` otherwise."""
    if isinstance(table, (str, bytes)) or not isinstance(table, Iterable):
        raise TypeError(f"table must be an iterable of crossmatch rows, got {table!r}")
    return table


def _check_positive_number(value: Any, name: str) -> float:
    """Validate a positive finite number argument; :class:`TypeError` for a
    non-number (bools rejected), :class:`HrError` for non-finite / <= 0."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number, got {value!r}")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise HrError(f"{name} must be a positive finite number, got {value!r}")
    return result


def _check_membership(membership: Any) -> Membership:
    if not isinstance(membership, Membership):
        raise TypeError(f"membership must be a Membership or None, got {membership!r}")
    return membership


def absolute_g(g: float, parallax_mas: float | None) -> float | None:
    """The absolute G magnitude from the apparent ``g`` and the parallax in
    mas: ``M_G = G + 5 - 5*log10(1000/parallax_mas)`` (equivalently
    ``G + 5 + 5*log10(parallax_arcsec)``).

    Returns ``None`` when ``parallax_mas`` is None or not a positive finite
    number (no distance, no absolute magnitude — NaN/inf are not positive).
    """
    g = float(g)
    if parallax_mas is None or not (math.isfinite(parallax_mas) and parallax_mas > 0):
        return None
    return g + 5.0 - 5.0 * math.log10(1000.0 / parallax_mas)


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return 0.5 * (ordered[mid - 1] + ordered[mid])


def _robust_sigma(values: list[float], median: float) -> float:
    """The robust sigma of ``values`` around ``median``: ``1.4826 * MAD``,
    falling back to the population standard deviation when the MAD is 0."""
    mad = _median([abs(v - median) for v in values])
    if mad > 0:
        return MAD_TO_SIGMA * mad
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))


def membership_filter(table: Any, sigma_clip: Any = DEFAULT_SIGMA_CLIP) -> Membership:
    """Sigma-clip the cross-match ``table`` on parallax + proper motion around
    the median and return the frozen :class:`Membership`.

    The clip sample is the matched rows with finite ``parallax`` / ``pmra`` /
    ``pmdec``. Per quantity the median and a robust sigma
    (:data:`MAD_TO_SIGMA` * MAD, population std when MAD is 0) are computed;
    a row is a member iff it is in the sample AND within ``sigma_clip``
    (default :data:`DEFAULT_SIGMA_CLIP` = 3.0) of the median in ALL THREE
    quantities. A quantity with zero spread does not clip. Rows outside the
    sample are not members. With fewer than :data:`MIN_MEMBERSHIP_SAMPLE`
    sample rows there is nothing to clip against: every matched row with a
    ``source_id`` is a member.

    Raises :class:`TypeError` when ``table`` is not an iterable or
    ``sigma_clip`` is not a number, :class:`HrError` when ``sigma_clip`` is
    not positive and finite.
    """
    rows = tuple(_check_table(table))
    sigma_clip = _check_positive_number(sigma_clip, "sigma_clip")
    sample: list[tuple[int, float, float, float]] = []
    for row in rows:
        if not bool(_get(row, "matched")):
            continue
        source_id = _get(row, "source_id")
        parallax = _finite(_get(row, "parallax"))
        pmra = _finite(_get(row, "pmra"))
        pmdec = _finite(_get(row, "pmdec"))
        if source_id is None or parallax is None or pmra is None or pmdec is None:
            continue
        if isinstance(source_id, bool) or not isinstance(source_id, int):
            continue
        sample.append((source_id, parallax, pmra, pmdec))
    if len(sample) < MIN_MEMBERSHIP_SAMPLE:
        member_ids = frozenset(
            sid
            for row in rows
            if bool(_get(row, "matched"))
            and isinstance((sid := _get(row, "source_id")), int)
            and not isinstance(sid, bool)
        )
        return Membership(
            member_ids=member_ids,
            parallax_median=None,
            parallax_sigma=None,
            pmra_median=None,
            pmra_sigma=None,
            pmdec_median=None,
            pmdec_sigma=None,
            sigma_clip=sigma_clip,
            sample_size=len(sample),
        )
    plx = [s[1] for s in sample]
    pmra_vals = [s[2] for s in sample]
    pmdec_vals = [s[3] for s in sample]
    plx_med = _median(plx)
    pmra_med = _median(pmra_vals)
    pmdec_med = _median(pmdec_vals)
    plx_sig = _robust_sigma(plx, plx_med)
    pmra_sig = _robust_sigma(pmra_vals, pmra_med)
    pmdec_sig = _robust_sigma(pmdec_vals, pmdec_med)

    def within(value: float, median: float, sigma: float) -> bool:
        return sigma <= 0 or abs(value - median) <= sigma_clip * sigma

    member_ids = frozenset(
        sid
        for sid, plx_v, pmra_v, pmdec_v in sample
        if within(plx_v, plx_med, plx_sig)
        and within(pmra_v, pmra_med, pmra_sig)
        and within(pmdec_v, pmdec_med, pmdec_sig)
    )
    return Membership(
        member_ids=member_ids,
        parallax_median=plx_med,
        parallax_sigma=plx_sig,
        pmra_median=pmra_med,
        pmra_sigma=pmra_sig,
        pmdec_median=pmdec_med,
        pmdec_sigma=pmdec_sig,
        sigma_clip=sigma_clip,
        sample_size=len(sample),
    )


def _point(row: Any, member_ids: frozenset[int]) -> HrPoint | None:
    """One duck-typed row as an :class:`HrPoint`, or None when the row is
    unmatched or lacks complete photometry (source_id + finite g/bp/rp)."""
    if not bool(_get(row, "matched")):
        return None
    source_id = _get(row, "source_id")
    if isinstance(source_id, bool) or not isinstance(source_id, int):
        return None
    g = _finite(_get(row, "g"))
    bp = _finite(_get(row, "bp"))
    rp = _finite(_get(row, "rp"))
    if g is None or bp is None or rp is None:
        return None
    parallax = _finite(_get(row, "parallax"))
    parallax_error = _finite(_get(row, "parallax_error"))
    return HrPoint(
        source_id=source_id,
        x=float(_finite(_get(row, "x")) or 0.0),
        y=float(_finite(_get(row, "y")) or 0.0),
        g=g,
        bp_rp=bp - rp,
        abs_g=absolute_g(g, parallax),
        parallax=parallax,
        parallax_error=parallax_error,
        pmra=_finite(_get(row, "pmra")),
        pmdec=_finite(_get(row, "pmdec")),
        member=source_id in member_ids,
    )


def cmd_points(table: Any, membership: Any = None) -> tuple[HrPoint, ...]:
    """The apparent CMD points (G vs BP-RP) of the cross-match ``table``, in
    row order: one frozen :class:`HrPoint` per matched row with complete
    photometry (``source_id`` + finite ``g`` / ``bp`` / ``rp``); unmatched and
    incomplete rows are skipped.

    ``abs_g`` is computed when the row has a positive parallax (else None) —
    the CMD itself only needs ``g`` / ``bp_rp``. The ``member`` flag comes
    from ``membership`` when given, else from :func:`membership_filter`
    applied to the same table.

    Raises :class:`TypeError` for a non-iterable ``table`` or a non-Membership
    ``membership``.
    """
    rows = tuple(_check_table(table))
    if membership is None:
        membership = membership_filter(rows)
    else:
        membership = _check_membership(membership)
    return tuple(
        point
        for row in rows
        if (point := _point(row, membership.member_ids)) is not None
    )


def hr_points(
    table: Any,
    parallax_snr: Any = DEFAULT_PARALLAX_SNR,
    membership: Any = None,
) -> tuple[HrPoint, ...]:
    """The H-R points (absolute G vs BP-RP) of the cross-match ``table``, in
    row order: one frozen :class:`HrPoint` per matched row with complete
    photometry AND a parallax passing the SNR cut
    ``parallax / parallax_error > parallax_snr`` (default
    :data:`DEFAULT_PARALLAX_SNR` = 5.0, strictly greater). Every returned
    point has a finite ``abs_g`` = :func:`absolute_g` (``g + 5 -
    5*log10(1000/parallax_mas)``); rows failing the cut (missing/zero error,
    non-positive parallax, SNR <= threshold) are dropped from the H-R list.

    The ``member`` flag comes from ``membership`` when given, else from
    :func:`membership_filter` applied to the same table.

    Raises :class:`TypeError` for a non-iterable ``table``, a non-number
    ``parallax_snr``, or a non-Membership ``membership``; :class:`HrError`
    when ``parallax_snr`` is not positive and finite.
    """
    rows = tuple(_check_table(table))
    parallax_snr = _check_positive_number(parallax_snr, "parallax_snr")
    if membership is None:
        membership = membership_filter(rows)
    else:
        membership = _check_membership(membership)
    points: list[HrPoint] = []
    for row in rows:
        point = _point(row, membership.member_ids)
        if point is None:
            continue
        parallax = point.parallax
        parallax_error = point.parallax_error
        if (
            parallax is None
            or parallax <= 0
            or parallax_error is None
            or parallax_error <= 0
            or parallax / parallax_error <= parallax_snr
        ):
            continue
        points.append(point)
    return tuple(points)
