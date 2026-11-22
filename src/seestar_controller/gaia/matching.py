"""Cross-match detected stack sources against Gaia DR3 rows by sky position.

Pure-Python core of the gaia-crossmatch feature (module boundary per
``.scratch/gaia-crossmatch/spec.md``): no GTK imports, no
:mod:`seestar_controller.link` imports — a subprocess test enforces the
boundary. Distances use
:func:`seestar_controller.catalog.coords.angular_distance` (never
reimplemented here).

What lives here:

- :class:`CrossmatchRow` — one output table row: the detection's pixel
  ``(x, y)`` and sky position, the ``matched`` flag, and the matched Gaia
  fields (``source_id``, ``g``, ``bp``, ``rp``, ``parallax`` +
  ``parallax_error``, ``pmra`` + ``pmra_error``, ``pmdec`` +
  ``pmdec_error`` — all ``None`` when unmatched) plus the match
  ``separation_arcsec``;
- :func:`crossmatch` — pair every detection with its nearest Gaia row within
  ``radius_arcsec`` (default :data:`DEFAULT_MATCH_RADIUS_ARCSEC` = 1.5),
  one-to-one (greedy by ascending separation, so the closest pairs win).
  The candidate pairs come from a Dec-band spatial index
  (:func:`_candidate_pairs`), so only pairs that can lie within the radius
  get the exact distance — identical results to evaluating every pair, but
  seconds instead of minutes on a dense field (thousands of detections ×
  up to :data:`~.query.DEFAULT_MAXREC` Gaia rows);
  detections with no Gaia row inside the radius are flagged
  ``matched=False`` — they are the interesting ones (new variables,
  artifacts);
- :func:`crossmatch_stack` — the demoable pipeline over a session STACK:
  :func:`~.detect.detect_sources` -> field center + radius
  (:func:`~.detect.field_center_and_radius`) ->
  :func:`~.query.query_gaia_cached` -> :func:`crossmatch`.
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from seestar_controller.catalog.coords import angular_distance

from .detect import (
    DEFAULT_MARGIN_DEG,
    DEFAULT_THRESHOLD_SIGMA,
    detect_sources,
    field_center_and_radius,
)
from .errors import GaiaError
from .query import (
    ARCSEC_PER_DEG,
    DEFAULT_G_LIMIT,
    DEFAULT_MAXREC,
    DEFAULT_TIMEOUT_S,
    query_gaia_cached,
)

#: Default cross-match radius, in arcseconds.
DEFAULT_MATCH_RADIUS_ARCSEC = 1.5


@dataclass(frozen=True)
class CrossmatchRow:
    """One cross-match table row.

    - ``x`` / ``y`` — the detection's 0-based pixel position;
    - ``ra_deg`` / ``dec_deg`` — the detection's J2000 sky position;
    - ``matched`` — whether a Gaia row lies within the match radius;
    - ``source_id`` / ``g`` / ``bp`` / ``rp`` — the matched Gaia DR3
      magnitudes and source id (``None`` when unmatched);
    - ``parallax`` / ``parallax_error`` — the matched parallax in mas + its
      1-sigma error;
    - ``pmra`` / ``pmra_error``, ``pmdec`` / ``pmdec_error`` — the matched
      proper motion in mas/yr + its 1-sigma error;
    - ``separation_arcsec`` — the sky separation of the matched pair
      (``None`` when unmatched).

    All Gaia fields (values and errors) are ``None`` when unmatched.
    """

    x: float
    y: float
    ra_deg: float
    dec_deg: float
    matched: bool
    source_id: int | None = None
    g: float | None = None
    bp: float | None = None
    rp: float | None = None
    parallax: float | None = None
    parallax_error: float | None = None
    pmra: float | None = None
    pmra_error: float | None = None
    pmdec: float | None = None
    pmdec_error: float | None = None
    separation_arcsec: float | None = None


def _validate_radius_arcsec(radius_arcsec: Any) -> float:
    if (
        isinstance(radius_arcsec, bool)
        or not isinstance(radius_arcsec, (int, float))
        or not math.isfinite(radius_arcsec)
        or radius_arcsec <= 0
    ):
        raise ValueError(
            f"radius_arcsec must be a positive number of arcseconds, got {radius_arcsec!r}"
        )
    return float(radius_arcsec)


def _detection_fields(source: Any) -> tuple[float, float, float, float]:
    """Extract ``(x, y, ra_deg, dec_deg)`` from a detection (duck-typed)."""
    try:
        x = float(source.x)
        y = float(source.y)
        ra = float(source.ra_deg) % 360.0
        dec = float(source.dec_deg)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(
            "each detected source needs numeric x, y, ra_deg, dec_deg attributes, "
            f"got {source!r}"
        ) from exc
    if not all(math.isfinite(v) for v in (x, y, ra, dec)):
        raise ValueError(f"detected source has non-finite coordinates: {source!r}")
    return x, y, ra, dec


def _gaia_fields(star: Any) -> tuple[float, float, dict[str, Any]]:
    """Extract ``(ra_deg, dec_deg, gaia-fields-dict)`` from a Gaia row (duck-typed)."""
    try:
        ra = float(star.ra_deg) % 360.0
        dec = float(star.dec_deg)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(
            f"each Gaia row needs numeric ra_deg, dec_deg attributes, got {star!r}"
        ) from exc
    if not (math.isfinite(ra) and math.isfinite(dec)):
        raise ValueError(f"Gaia row has non-finite coordinates: {star!r}")
    fields = {
        "source_id": getattr(star, "source_id", None),
        "g": getattr(star, "g", None),
        "bp": getattr(star, "bp", None),
        "rp": getattr(star, "rp", None),
        "parallax": getattr(star, "parallax", None),
        "parallax_error": getattr(star, "parallax_error", None),
        "pmra": getattr(star, "pmra", None),
        "pmra_error": getattr(star, "pmra_error", None),
        "pmdec": getattr(star, "pmdec", None),
        "pmdec_error": getattr(star, "pmdec_error", None),
    }
    return ra, dec, fields


def _candidate_pairs(
    detections: list[tuple[float, float, float, float]],
    stars: list[tuple[float, float, dict[str, Any]]],
    radius_arcsec: float,
) -> list[tuple[float, int, int]]:
    """All ``(separation_arcsec, detection_index, star_index)`` pairs whose
    separation is within ``radius_arcsec``.

    A spatial index instead of the detection×Gaia double loop: the Gaia
    rows are sorted by Dec once, and each detection scans only the Dec band
    ``[dec_d - radius_deg, dec_d + radius_deg]`` — a pair can never be
    closer than its Dec difference. Inside the band a cheap RA lower bound
    (``sin(d/2) >= cos(dec_max) * |sin(dra/2)|``, invariant under the RA
    0/360 wrap, with ``dec_max`` clamped at the pole where RA differences
    stop mattering) rejects the rest; only the survivors get the exact
    :func:`~seestar_controller.catalog.coords.angular_distance`. Both bounds
    carry a tiny slack, so the candidate set is exactly the one the
    brute-force loop produces — only pairs near each other are evaluated.
    """
    radius_deg = radius_arcsec / ARCSEC_PER_DEG
    order = sorted(range(len(stars)), key=lambda j: stars[j][1])
    band_dec = [stars[j][1] for j in order]
    slack = 1e-9 * radius_deg + 1e-15  # FP slack: never reject a true match
    candidates: list[tuple[float, int, int]] = []
    for i, (_, _, ra_d, dec_d) in enumerate(detections):
        lo = bisect_left(band_dec, dec_d - radius_deg - slack)
        hi = bisect_right(band_dec, dec_d + radius_deg + slack)
        if lo == hi:
            continue
        # cos of the largest |Dec| a band row can have from this detection.
        cos_dec = math.cos(math.radians(min(90.0, abs(dec_d) + radius_deg)))
        for k in range(lo, hi):
            j = order[k]
            ra_g, dec_g, _ = stars[j]
            lower = 2.0 * math.asin(
                min(1.0, cos_dec * abs(math.sin(math.radians(ra_g - ra_d) / 2.0)))
            )
            if lower > radius_deg + slack:
                continue
            sep_deg = angular_distance((ra_d, dec_d), (ra_g, dec_g))
            sep_arcsec = sep_deg * ARCSEC_PER_DEG
            if sep_arcsec <= radius_arcsec:
                candidates.append((sep_arcsec, i, j))
    return candidates


def crossmatch(
    detected: Iterable[Any],
    gaia_rows: Iterable[Any],
    *,
    radius_arcsec: float = DEFAULT_MATCH_RADIUS_ARCSEC,
) -> list[CrossmatchRow]:
    """Cross-match ``detected`` sources against ``gaia_rows`` by sky position.

    ``detected`` is an iterable of detections (anything with numeric ``x``,
    ``y``, ``ra_deg``, ``dec_deg`` — e.g. :class:`~.detect.DetectedSource`);
    ``gaia_rows`` an iterable of Gaia rows (anything with numeric ``ra_deg``,
    ``dec_deg`` and the Gaia fields — e.g. :class:`~.query.GaiaStar`).

    Each detection is paired with its **nearest** Gaia row within
    ``radius_arcsec`` (default 1.5 arcsec); the pairing is one-to-one —
    candidate pairs are taken in ascending separation order, so the closest
    pairs win and no Gaia row is matched twice. Detections with no Gaia row
    inside the radius come back with ``matched=False`` and all Gaia fields
    ``None``.

    Candidate pairs are found via :func:`_candidate_pairs` (a Dec-band
    spatial index with an RA lower bound), so only pairs that can lie within
    the radius get the exact distance — the result is identical to evaluating
    every detection×Gaia pair, but runs in seconds on a dense field instead
    of minutes.

    Returns one :class:`CrossmatchRow` per detection, in the input detection
    order. Raises :class:`ValueError` on a bad ``radius_arcsec`` or malformed
    input rows.
    """
    radius = _validate_radius_arcsec(radius_arcsec)
    detections = [_detection_fields(d) for d in detected]
    stars = [_gaia_fields(g) for g in gaia_rows]
    candidates = _candidate_pairs(detections, stars, radius)
    candidates.sort()
    matched_i: dict[int, tuple[float, int]] = {}
    used_j: set[int] = set()
    for sep_arcsec, i, j in candidates:
        if i in matched_i or j in used_j:
            continue
        matched_i[i] = (sep_arcsec, j)
        used_j.add(j)
    rows: list[CrossmatchRow] = []
    for i, (x, y, ra, dec) in enumerate(detections):
        if i in matched_i:
            sep_arcsec, j = matched_i[i]
            _, _, fields = stars[j]
            rows.append(
                CrossmatchRow(
                    x=x,
                    y=y,
                    ra_deg=ra,
                    dec_deg=dec,
                    matched=True,
                    source_id=fields["source_id"],
                    g=fields["g"],
                    bp=fields["bp"],
                    rp=fields["rp"],
                    parallax=fields["parallax"],
                    parallax_error=fields["parallax_error"],
                    pmra=fields["pmra"],
                    pmra_error=fields["pmra_error"],
                    pmdec=fields["pmdec"],
                    pmdec_error=fields["pmdec_error"],
                    separation_arcsec=sep_arcsec,
                )
            )
        else:
            rows.append(
                CrossmatchRow(x=x, y=y, ra_deg=ra, dec_deg=dec, matched=False)
            )
    return rows


def crossmatch_stack(
    stack_data: Any,
    header: Any,
    *,
    threshold: float = DEFAULT_THRESHOLD_SIGMA,
    fwhm: float | None = None,
    radius_arcsec: float = DEFAULT_MATCH_RADIUS_ARCSEC,
    g_limit: float = DEFAULT_G_LIMIT,
    margin_deg: float = DEFAULT_MARGIN_DEG,
    cache: Any = None,
    maxrec: int = DEFAULT_MAXREC,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> list[CrossmatchRow]:
    """The full pure-core pipeline over a session STACK.

    1. :func:`~.detect.detect_sources` — detect all sources on ``stack_data``
       and convert them to sky positions via the WCS of ``header``
       (:class:`~.errors.WcsMissingError` — "plate-solve first" — when the
       header has no celestial WCS);
    2. :func:`~.detect.field_center_and_radius` — the cone-search center and
       radius (field diagonal + ``margin_deg``);
    3. :func:`~.query.query_gaia_cached` — the Gaia DR3 cone search (G <
       ``g_limit``), served from the disk cache when fresh;
    4. :func:`crossmatch` — pair detections with Gaia rows within
       ``radius_arcsec``.

    Returns one :class:`CrossmatchRow` per detection (matched or flagged
    unmatched). Raises the errors of the individual steps — in particular
    :class:`~.errors.GaiaUnavailableError` when the Gaia query fails and no
    cache exists.
    """
    import numpy as np

    data = np.asarray(stack_data)
    if data.ndim != 2:
        raise GaiaError(f"stack_data must be a 2-D array, got shape {data.shape}")
    sources = detect_sources(data, header, threshold=threshold, fwhm=fwhm)
    center, radius_deg = field_center_and_radius(header, data.shape, margin_deg=margin_deg)
    stars = query_gaia_cached(
        center.ra_deg,
        center.dec_deg,
        radius_deg,
        cache,
        g_limit=g_limit,
        maxrec=maxrec,
        timeout=timeout,
    )
    return crossmatch(sources, stars, radius_arcsec=radius_arcsec)
