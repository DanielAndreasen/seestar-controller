"""High-level catalog facade: always-cached lookups with graceful offline
degradation.

This is the single high-level entry point of the catalog package — the layer
the app should call. It composes the raw, uncached queries
(:func:`~.simbad.resolve_name`, :func:`~.standards.find_standard_stars`) with
the on-disk :class:`~.cache.QueryCache` and translates **every** failure mode
into the catalog error taxonomy, so a network outage or a broken catalog
response never crashes the app and never surfaces a raw traceback:

- a **fresh** cache entry is served from disk with no network access at all
  (the offline hit);
- on a miss the raw query runs and its parsed, validated result is stored
  best-effort and returned;
- if the fetch fails for **any** reason — network down
  (:class:`~.simbad.SimbadUnavailableError` /
  :class:`~.standards.StandardsUnavailableError`), timeout, HTTP error,
  malformed response (:class:`~.simbad.SimbadResponseError` /
  :class:`~.standards.StandardsResponseError`), or any unexpected error — a
  cached entry is served **even if expired** (*stale-on-failure*). Catalog
  data changes slowly, so a stale entry beats no answer: this is the
  documented graceful-degradation policy of this layer — expired cache +
  network down -> the stale entry is served, not an error;
- with no usable cache entry the failure is translated into the taxonomy:
  unavailable/timeout/unexpected -> :class:`CatalogUnavailableError` naming
  the query and suggesting a retry plus the offline cache location;
  malformed -> the specific ``*ResponseError`` re-raised (already a clean,
  actionable :class:`~.simbad.CatalogError`). Raw
  :class:`urllib.error.URLError` / :class:`TimeoutError` escaping the HTTP
  boundary are caught and translated too — never a raw traceback.

The raw uncached functions stay untouched; this layer only composes them with
the cache (and shares their cache keys, so entries are interchangeable with
:func:`~.cache.resolve_name_cached` / :func:`~.standards.find_standard_stars_cached`).
An empty name-resolution result is cached as well, so offline a known-unknown
name still raises :class:`~.simbad.NameNotFoundError` without network.

Pure-Python (module boundary per ``.scratch/catalog/spec.md``): no GTK
imports, no :mod:`seestar_controller.link` imports, stdlib only. Tests mock
the single HTTP boundaries (:func:`~.simbad.fetch_tap_json`,
:func:`~.standards.fetch_vizier_tap_json`), so no test ever touches the
network.

API shape::

    catalog = Catalog()                                  # one facade, one cache
    candidates = catalog.lookup_name("M 42")             # offline-graceful
    stars = catalog.lookup_standards(coord, 0.5, target_mag=10.0)

or the module-level functions with an explicit (or default) cache::

    lookup_name("M 42", cache)
    lookup_standards(coord, 0.5, cache)
"""

from __future__ import annotations

import dataclasses
from typing import Any, Callable

from . import cache as cache_mod
from . import simbad, standards
from .cache import QueryCache
from .simbad import (
    CatalogCandidate,
    CatalogError,
    NameNotFoundError,
    SimbadResponseError,
)
from .standards import StandardStar, StandardsResponseError


class CatalogUnavailableError(CatalogError):
    """The catalog service could not be reached and no cached result exists.

    Raised by the facade when a lookup's fetch fails (network down, timeout,
    HTTP error, or any unexpected error) **and** the cache holds no usable
    entry for the query. The message names the query and suggests retrying
    when back online plus the offline cache location — never a raw
    :class:`urllib.error.URLError` / :class:`TimeoutError`.
    """


def _translate_failure(exc: BaseException, cache: QueryCache, describe: str) -> CatalogError:
    """Translate one fetch failure into the catalog error taxonomy (no cache hit).

    Malformed-response errors are already clean, actionable catalog errors
    (:class:`~.simbad.SimbadResponseError` /
    :class:`~.standards.StandardsResponseError`) and are returned unchanged;
    everything else (network, timeout, HTTP, or an unexpected error) becomes
    a :class:`CatalogUnavailableError` naming the query.
    """
    if isinstance(exc, (SimbadResponseError, StandardsResponseError)):
        return exc
    return CatalogUnavailableError(
        f"catalog lookup for {describe} failed and no cached result is available: {exc}. "
        "Retry when the network is back — previously looked-up queries are served "
        f"from the offline cache at {cache.cache_dir}"
    )


def _offline_cached(
    cache: QueryCache,
    query: str,
    fetch: Callable[[], Any],
    *,
    validate: Callable[[Any], bool],
    describe: str,
) -> Any:
    """Cache-first lookup with stale-on-failure degradation — the facade core.

    - a fresh, valid cache entry is returned without calling ``fetch``;
    - otherwise ``fetch`` runs; its result is stored best-effort and returned;
    - if ``fetch`` raises for ANY reason, a valid cached entry — even an
      expired one — is served as a stale fallback (catalog data changes
      slowly, so stale beats nothing; the documented policy of this layer);
    - with no usable cache entry the failure is translated by
      :func:`_translate_failure` — never a raw traceback.
    """
    stale: Any = None
    found = cache.get_any(query)
    if found is not None and validate(found[0]):
        payload, fresh = found
        if fresh:
            return payload
        stale = payload  # expired but valid: the offline fallback
    try:
        payload = fetch()
    except Exception as exc:  # CatalogError subclasses included; never BaseException
        if stale is not None:
            return stale
        raise _translate_failure(exc, cache, describe) from exc
    try:
        cache.set(query, payload)
    except OSError:
        pass  # best-effort write, same as QueryCache.cached
    return payload


def lookup_name(
    name: str,
    cache: QueryCache | None = None,
    *,
    mag_filter: str = simbad.DEFAULT_MAG_FILTER,
    maxrec: int = simbad.DEFAULT_MAX_CANDIDATES,
    timeout: float = simbad.DEFAULT_TIMEOUT_S,
) -> list[CatalogCandidate]:
    """Resolve ``name`` to SIMBAD candidates — always cached, offline-graceful.

    The high-level variant of :func:`~.simbad.resolve_name`: same result, same
    :class:`ValueError` / :class:`~.simbad.NameNotFoundError` semantics, but
    the query always goes through ``cache`` (default: one in the app config
    dir) and every fetch failure degrades gracefully (module docstring):
    fresh or stale cache entry -> served from disk; no cache ->
    :class:`CatalogUnavailableError` (network/timeout/unexpected) or the
    ``*ResponseError`` (malformed) — never a raw traceback.
    """
    if cache is None:
        cache = QueryCache()
    if not isinstance(name, str):
        raise ValueError(f"name must be a string, got {name!r}")
    query_name = name.strip()
    if not query_name:
        raise ValueError("name must not be empty")
    if maxrec < 1:
        raise ValueError(f"maxrec must be >= 1, got {maxrec}")
    adql = simbad.build_lookup_query(query_name, mag_filter=mag_filter)
    query = cache_mod._name_query(adql, maxrec=maxrec)

    def fetch() -> list[dict[str, Any]]:
        try:
            candidates = simbad.resolve_name(
                query_name, mag_filter=mag_filter, maxrec=maxrec, timeout=timeout
            )
        except NameNotFoundError:
            return []  # a known-unknown name is a valid, cacheable result
        # Parse BEFORE caching: a malformed response is never stored.
        return [dataclasses.asdict(c) for c in candidates]

    rows = _offline_cached(
        cache,
        query,
        fetch,
        validate=cache_mod._is_candidate_rows,
        describe=f"name {query_name!r}",
    )
    candidates = [CatalogCandidate(**row) for row in rows]
    if not candidates:
        raise NameNotFoundError(
            f"SIMBAD has no object named {query_name!r} — check the spelling; "
            "SIMBAD identifiers look like 'M 42', 'NGC 1976' or 'V* bet Per', "
            f"or append '*' for a wildcard match (e.g. '{query_name}*')"
        )
    return candidates


def lookup_standards(
    coord: Any,
    radius_deg: float,
    cache: QueryCache | None = None,
    *,
    target_mag: float | None = None,
    mag_range: tuple[float, float] | None = None,
    band: str = standards.DEFAULT_BAND,
    maxrec: int = standards.DEFAULT_MAX_STANDARDS,
    timeout: float = standards.DEFAULT_TIMEOUT_S,
) -> list[StandardStar]:
    """Find standard/comparison stars near ``coord`` — always cached, offline-graceful.

    The high-level variant of :func:`~.standards.find_standard_stars`: same
    result (cone search, radius filter, magnitude sort), same
    :class:`ValueError` semantics, but the query always goes through ``cache``
    (default: one in the app config dir) and every fetch failure degrades
    gracefully (module docstring): fresh or stale cache entry -> served from
    disk; no cache -> :class:`CatalogUnavailableError` (network/timeout/
    unexpected) or the ``*ResponseError`` (malformed) — never a raw traceback.
    ``target_mag``/``mag_range`` are not part of the cache key: they only
    re-filter/re-sort the cached rows, so they work offline too.
    """
    if cache is None:
        cache = QueryCache()
    target, radius, checked_band, checked_mag, checked_range = standards._validate_args(
        coord, radius_deg, maxrec=maxrec, band=band, target_mag=target_mag, mag_range=mag_range
    )
    adql = standards.build_cone_query(target, radius, band=checked_band)
    query = standards._standards_query(adql, maxrec=maxrec)

    def fetch() -> list[dict[str, Any]]:
        # target_mag/mag_range are deliberately NOT fetched: they only
        # re-filter/re-sort the cached cone (same as find_standard_stars_cached).
        stars = standards.find_standard_stars(
            target, radius, band=checked_band, maxrec=maxrec, timeout=timeout
        )
        # Parse BEFORE caching: a malformed response is never stored.
        return [standards._star_to_row(s) for s in stars]

    rows = _offline_cached(
        cache,
        query,
        fetch,
        validate=standards._is_standard_rows,
        describe=(
            f"standard stars within {radius} deg of RA {target.ra_hours:.4f}h "
            f"Dec {target.dec_deg:+.4f} deg"
        ),
    )
    stars = [standards._row_to_star(row) for row in rows]
    return standards._finish(stars, target_mag=checked_mag, mag_range=checked_range)


class Catalog:
    """The high-level catalog facade — one object holding one cache.

    ``Catalog()`` uses the default cache (the app config dir,
    :func:`~.cache.default_cache_dir`); pass a :class:`~.cache.QueryCache` to
    pin the location (tests use a ``tmp_path`` cache). Every lookup goes
    through the cache and degrades gracefully offline — see the module
    docstring for the stale-on-failure policy.
    """

    def __init__(self, cache: QueryCache | None = None) -> None:
        self.cache = cache if cache is not None else QueryCache()

    def lookup_name(
        self,
        name: str,
        *,
        mag_filter: str = simbad.DEFAULT_MAG_FILTER,
        maxrec: int = simbad.DEFAULT_MAX_CANDIDATES,
        timeout: float = simbad.DEFAULT_TIMEOUT_S,
    ) -> list[CatalogCandidate]:
        """Cached, offline-graceful name resolution on this facade's cache."""
        return lookup_name(
            name, self.cache, mag_filter=mag_filter, maxrec=maxrec, timeout=timeout
        )

    def lookup_standards(
        self,
        coord: Any,
        radius_deg: float,
        *,
        target_mag: float | None = None,
        mag_range: tuple[float, float] | None = None,
        band: str = standards.DEFAULT_BAND,
        maxrec: int = standards.DEFAULT_MAX_STANDARDS,
        timeout: float = standards.DEFAULT_TIMEOUT_S,
    ) -> list[StandardStar]:
        """Cached, offline-graceful standard-star lookup on this facade's cache."""
        return lookup_standards(
            coord,
            radius_deg,
            self.cache,
            target_mag=target_mag,
            mag_range=mag_range,
            band=band,
            maxrec=maxrec,
            timeout=timeout,
        )
