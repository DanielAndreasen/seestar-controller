"""Catalog lookups for the Seestar Controller — pure Python, no GTK, no link.

- :mod:`.coords` — shared J2000 coordinate math: RA/Dec string parsing
  (sexagesimal and decimal), the canonical :class:`~.coords.J2000Coord` type,
  and great-circle :func:`~.coords.angular_distance` (stdlib :mod:`math` only)
- :mod:`.simbad` — SIMBAD name resolution to J2000 coordinates via the public
  TAP HTTP interface with official-mirror fallback (CDS Strasbourg first,
  then Harvard/IDIA — CDS has been down since 2026-10-29; stdlib
  :mod:`urllib` only; tests mock the single
  :func:`~.simbad.fetch_tap_json` HTTP boundary)
- :mod:`.cache` — on-disk JSON query cache under the app config dir with a
  configurable TTL: :class:`~.cache.QueryCache` wraps any catalog query
  (:meth:`~.cache.QueryCache.cached`), and :func:`~.cache.resolve_name_cached`
  is the cached variant of :func:`~.simbad.resolve_name` (repeat lookups are
  instant and work offline; the raw ``resolve_name`` stays uncached)
- :mod:`.standards` — standard-star (comparison-star) lookup near a target:
  :func:`~.standards.find_standard_stars` cone-searches APASS DR9 via VizieR
  (CDS TAP first, then the asu-tsv mirrors) for known standard stars within a
  radius (filtered by :func:`~.coords.angular_distance`, sorted by magnitude
  closeness to the target), and
  :func:`~.standards.find_standard_stars_cached` is its cached variant — the
  catalog assist for photometry comp picking
- :mod:`.facade` — the single high-level entry point: :class:`~.facade.Catalog`
  / :func:`~.facade.lookup_name` / :func:`~.facade.lookup_standards` always use
  the cache and degrade gracefully offline — a fresh or stale (expired) cache
  entry is served on any fetch failure, and with no cache the failure becomes
  a clean :class:`~.facade.CatalogUnavailableError` / ``*ResponseError`, never
  a raw traceback

Module boundary (see ``.scratch/catalog/spec.md``): pure functions returning
plain data. Nothing in this package imports GTK or
:mod:`seestar_controller.link`.
"""

from .cache import (
    CACHE_DIR_ENV_VAR,
    DEFAULT_CACHE_DIR,
    DEFAULT_CONFIG_DIR,
    DEFAULT_TTL_S,
    QueryCache,
    cache_key,
    default_cache_dir,
    resolve_name_cached,
)
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
from .facade import (
    Catalog,
    CatalogUnavailableError,
    lookup_name,
    lookup_standards,
)
from .simbad import (
    DEFAULT_MAG_FILTER,
    DEFAULT_MAX_CANDIDATES,
    DEFAULT_TIMEOUT_S,
    SIMBAD_TAP_SYNC_URL,
    SIMBAD_TAP_SYNC_URL_CFA,
    SIMBAD_TAP_SYNC_URL_IDIA,
    SIMBAD_TAP_SYNC_URLS,
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
from .standards import (
    BAND_COLUMNS,
    DEFAULT_BAND,
    DEFAULT_MAX_STANDARDS,
    VIZIER_ASU_TSV_URL_CFA,
    VIZIER_ASU_TSV_URL_IDIA,
    VIZIER_ASU_TSV_URLS,
    VIZIER_TABLE,
    VIZIER_TAP_SYNC_URL,
    StandardsResponseError,
    StandardStar,
    StandardsUnavailableError,
    build_asu_tsv_url,
    build_cone_query,
    fetch_standards_payload,
    fetch_vizier_asu_tsv,
    fetch_vizier_tap_json,
    find_standard_stars,
    find_standard_stars_cached,
    parse_asu_tsv,
    parse_cone_response,
)

__all__ = [
    "BAND_COLUMNS",
    "CACHE_DIR_ENV_VAR",
    "DEFAULT_BAND",
    "DEFAULT_CACHE_DIR",
    "DEFAULT_CONFIG_DIR",
    "DEFAULT_MAG_FILTER",
    "DEFAULT_MAX_CANDIDATES",
    "DEFAULT_MAX_STANDARDS",
    "DEFAULT_TIMEOUT_S",
    "DEFAULT_TTL_S",
    "DEG_PER_HOUR",
    "HOURS_PER_CIRCLE",
    "SIMBAD_TAP_SYNC_URL",
    "SIMBAD_TAP_SYNC_URLS",
    "SIMBAD_TAP_SYNC_URL_CFA",
    "SIMBAD_TAP_SYNC_URL_IDIA",
    "VIZIER_ASU_TSV_URLS",
    "VIZIER_ASU_TSV_URL_CFA",
    "VIZIER_ASU_TSV_URL_IDIA",
    "VIZIER_TABLE",
    "VIZIER_TAP_SYNC_URL",
    "Catalog",
    "CatalogCandidate",
    "CatalogError",
    "CatalogUnavailableError",
    "CoordinateError",
    "J2000Coord",
    "NameNotFoundError",
    "QueryCache",
    "SimbadResponseError",
    "SimbadUnavailableError",
    "StandardStar",
    "StandardsResponseError",
    "StandardsUnavailableError",
    "angular_distance",
    "build_asu_tsv_url",
    "build_cone_query",
    "build_lookup_query",
    "cache_key",
    "default_cache_dir",
    "fetch_standards_payload",
    "fetch_tap_json",
    "fetch_vizier_asu_tsv",
    "fetch_vizier_tap_json",
    "find_standard_stars",
    "find_standard_stars_cached",
    "lookup_name",
    "lookup_standards",
    "parse_asu_tsv",
    "parse_cone_response",
    "parse_coords",
    "parse_dec",
    "parse_ra",
    "parse_tap_response",
    "resolve_name",
    "resolve_name_cached",
]
