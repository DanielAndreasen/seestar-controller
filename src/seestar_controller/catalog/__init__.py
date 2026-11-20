"""Catalog lookups for the Seestar Controller — pure Python, no GTK, no link.

- :mod:`.coords` — shared J2000 coordinate math: RA/Dec string parsing
  (sexagesimal and decimal), the canonical :class:`~.coords.J2000Coord` type,
  and great-circle :func:`~.coords.angular_distance` (stdlib :mod:`math` only)
- :mod:`.simbad` — SIMBAD name resolution to J2000 coordinates via the public
  TAP HTTP interface (stdlib :mod:`urllib` only; tests mock the single
  :func:`~.simbad.fetch_tap_json` HTTP boundary)

Module boundary (see ``.scratch/catalog/spec.md``): pure functions returning
plain data. Nothing in this package imports GTK or
:mod:`seestar_controller.link`.
"""

from .coords import (
    DEG_PER_HOUR,
    HOURS_PER_CIRCLE,
    CoordinateError,
    J2000Coord,
    angular_distance,
    parse_coords,
    parse_dec,
    parse_ra,
)
from .simbad import (
    DEFAULT_MAG_FILTER,
    DEFAULT_MAX_CANDIDATES,
    DEFAULT_TIMEOUT_S,
    SIMBAD_TAP_SYNC_URL,
    CatalogCandidate,
    CatalogError,
    NameNotFoundError,
    SimbadResponseError,
    SimbadUnavailableError,
    build_lookup_query,
    fetch_tap_json,
    parse_tap_response,
    resolve_name,
)

__all__ = [
    "DEG_PER_HOUR",
    "HOURS_PER_CIRCLE",
    "DEFAULT_MAG_FILTER",
    "DEFAULT_MAX_CANDIDATES",
    "DEFAULT_TIMEOUT_S",
    "SIMBAD_TAP_SYNC_URL",
    "CatalogCandidate",
    "CatalogError",
    "CoordinateError",
    "J2000Coord",
    "NameNotFoundError",
    "SimbadResponseError",
    "SimbadUnavailableError",
    "angular_distance",
    "build_lookup_query",
    "fetch_tap_json",
    "parse_coords",
    "parse_dec",
    "parse_ra",
    "parse_tap_response",
    "resolve_name",
]
