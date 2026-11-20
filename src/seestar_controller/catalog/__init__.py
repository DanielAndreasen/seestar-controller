"""Catalog lookups for the Seestar Controller — pure Python, no GTK, no link.

- :mod:`.simbad` — SIMBAD name resolution to J2000 coordinates via the public
  TAP HTTP interface (stdlib :mod:`urllib` only; tests mock the single
  :func:`~.simbad.fetch_tap_json` HTTP boundary)

Module boundary (see ``.scratch/catalog/spec.md``): pure functions returning
plain data. Nothing in this package imports GTK or
:mod:`seestar_controller.link`.
"""

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
    "DEFAULT_MAG_FILTER",
    "DEFAULT_MAX_CANDIDATES",
    "DEFAULT_TIMEOUT_S",
    "SIMBAD_TAP_SYNC_URL",
    "CatalogCandidate",
    "CatalogError",
    "NameNotFoundError",
    "SimbadResponseError",
    "SimbadUnavailableError",
    "build_lookup_query",
    "fetch_tap_json",
    "parse_tap_response",
    "resolve_name",
]
