"""Gaia DR3 cone search via direct VizieR TAP HTTP, with a disk cache.

Pure-Python core of the gaia-crossmatch feature (module boundary per
``.scratch/gaia-crossmatch/spec.md``): no GTK imports, no
:mod:`seestar_controller.link` imports — a subprocess test enforces the
boundary. The only network access is stdlib :mod:`urllib` inside the single
HTTP boundary :func:`fetch_gaia_tap_json` — tests monkeypatch exactly that
function, so no test ever touches the network.

What lives here:

- :class:`GaiaStar` — one Gaia DR3 row: ``source_id``, J2000 position, and
  the selected columns ``g`` / ``bp`` / ``rp`` / ``parallax`` / ``pmra`` /
  ``pmdec`` (``None`` when Gaia has no value);
- :func:`build_cone_query` — the ADQL cone search over the VizieR Gaia DR3
  table :data:`GAIA_DR3_TABLE` (``I/355/gaiadr3``) around a center with a
  radius, selecting G/BP/RP/parallax/pmRA/pmDE and ``G < g_limit``;
- :func:`fetch_gaia_tap_json` — POST one ADQL query to the VizieR TAP
  ``sync`` endpoint (:data:`VIZIER_TAP_SYNC_URL`) and return the parsed
  JSON body — **the single HTTP boundary**;
- :func:`parse_gaia_response` — parse the TAP JSON payload into
  :class:`GaiaStar` rows (the one place wire values become floats);
- :func:`query_gaia` — the raw, uncached cone search;
- :func:`query_gaia_cached` — the cached variant: the response is stored on
  disk under the app config dir (``<config>/cache/gaia/``) keyed by the
  **rounded** field center + radius (+ g_limit + maxrec) with a TTL, reusing
  the catalog module's :class:`~seestar_controller.catalog.cache.QueryCache`
  (read-only reuse — this package never modifies catalog). A fresh cache
  entry is served with no HTTP at all; a fetch failure falls back to a stale
  entry when one exists, and raises :class:`~.errors.GaiaUnavailableError`
  when no cache exists.

ENDPOINT (verified live 2026-11-20, see docs/wiki/progress.md): CDS
Strasbourg — including ``tapvizier.cds.unistra.fr`` — has been down since
the 2026-10-29 security incident, and the official VizieR mirrors (Harvard
CfA, IDIA) expose no TAP interface, so :data:`VIZIER_TAP_SYNC_URL` is the
single endpoint, kept for when Strasbourg returns; until then every query
fails with :class:`~.errors.GaiaUnavailableError` (or is served from the
cache). VizieR serves ICRS, which coincides with J2000 at the precision we
need — the same convention as :mod:`seestar_controller.catalog`.

Cache-key rounding: the center and radius are rounded to
:data:`KEY_ROUND_DIGITS` decimals of a degree (~0.4 arcsec) before both the
ADQL and the cache key are built, so two stacks of the same field (whose
centers differ by sub-arcsecond WCS jitter) share one cache entry.
"""

from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from seestar_controller.catalog.cache import DEFAULT_CONFIG_DIR, QueryCache
from seestar_controller.catalog.coords import J2000Coord

from .errors import GaiaResponseError, GaiaUnavailableError

#: VizieR's public TAP synchronous endpoint (CDS Strasbourg; down since the
#: 2026-10-29 security incident, no TAP mirrors exist — kept for when it
#: returns; see module docstring).
VIZIER_TAP_SYNC_URL = "https://tapvizier.cds.unistra.fr/TAPVizieR/TAP/sync"

#: The VizieR table queried: Gaia DR3 (Gaia Data Release 3 source table).
GAIA_DR3_TABLE = "I/355/gaiadr3"

#: Default Gaia magnitude limit of the cone search: ``G < g_limit``.
DEFAULT_G_LIMIT = 20.0

#: Default HTTP timeout for one Gaia query, in seconds (a big cone search is
#: slower than a name lookup).
DEFAULT_TIMEOUT_S = 30.0

#: Default cap on returned rows (TAP ``MAXREC``) — a Seestar field at G < 20
#: holds tens of thousands of Gaia sources.
DEFAULT_MAXREC = 100_000

#: Default time-to-live of a Gaia cache entry, in seconds (7 days — Gaia DR3
#: is a static release, the TTL mainly guards against partial responses).
DEFAULT_TTL_S = 7 * 24 * 60 * 60.0

#: Decimals of a degree the cache key rounds the center and radius to.
KEY_ROUND_DIGITS = 4

#: Default cache location: ``<config dir>/cache/gaia/`` (the config dir is
#: the same as the catalog cache's, re-declared via the catalog module).
DEFAULT_CACHE_DIR = DEFAULT_CONFIG_DIR / "cache" / "gaia"

#: Environment variable that overrides the default Gaia cache dir.
CACHE_DIR_ENV_VAR = "SEESTAR_GAIA_CACHE_DIR"

#: User-Agent sent to VizieR (polite-identification convention).
USER_AGENT = "seestar-controller (Gaia DR3 cross-match)"

#: Largest cone-search radius ADQL accepts, in degrees.
MAX_RADIUS_DEG = 180.0

#: Arcseconds per degree (match-radius unit conversion).
ARCSEC_PER_DEG = 3600.0

#: The Gaia DR3 columns selected by the cone search, in SELECT order.
GAIA_COLUMNS = ("RAJ2000", "DEJ2000", "Source", "Gmag", "BPmag", "RPmag", "Plx", "pmRA", "pmDE")

#: Accepted wire-name aliases per GaiaStar field (VizieR TAP spells them
#: ``RAJ2000``/``Source``/``Gmag``/``Plx``/``pmRA``/``pmDE``; the Gaia archive
#: spells them ``ra``/``source_id``/``phot_g_mean_mag``/``parallax``/...).
_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "ra": ("RAJ2000", "RA_ICRS", "ra", "RA"),
    "dec": ("DEJ2000", "DE_ICRS", "dec", "DEC"),
    "source_id": ("Source", "SourceId", "source_id", "SOURCE_ID"),
    "g": ("Gmag", "G", "phot_g_mean_mag"),
    "bp": ("BPmag", "BP", "phot_bp_mean_mag"),
    "rp": ("RPmag", "RP", "phot_rp_mean_mag"),
    "parallax": ("Plx", "parallax"),
    "pmra": ("pmRA", "pmra", "PMRA"),
    "pmdec": ("pmDE", "pmDE", "pmdec", "PMDE"),
}

#: Fields without which a response cannot be parsed at all.
_REQUIRED_FIELDS = ("ra", "dec", "source_id")


@dataclass(frozen=True)
class GaiaStar:
    """One Gaia DR3 source row.

    ``source_id`` is the Gaia DR3 source identifier; ``ra_deg`` / ``dec_deg``
    the J2000 position in degrees; ``g`` / ``bp`` / ``rp`` the G / BP / RP
    magnitudes; ``parallax`` in mas; ``pmra`` / ``pmdec`` the proper motion
    in mas/yr (RA includes the cos(Dec) factor, as Gaia publishes it). The
    photometric / astrometric fields are ``None`` when Gaia has no value.
    """

    source_id: int
    ra_deg: float
    dec_deg: float
    g: float | None = None
    bp: float | None = None
    rp: float | None = None
    parallax: float | None = None
    pmra: float | None = None
    pmdec: float | None = None

    @property
    def coord(self) -> J2000Coord:
        """The J2000 position as the shared :class:`J2000Coord` type."""
        return J2000Coord(ra_deg=self.ra_deg, dec_deg=self.dec_deg)


# --- cache dir + cache key ---------------------------------------------------------


def default_cache_dir(env: Mapping[str, str] | None = None) -> Path:
    """Resolve the default Gaia cache dir.

    Precedence: the ``SEESTAR_GAIA_CACHE_DIR`` environment variable > the
    default ``~/.config/seestar-controller/cache/gaia/``.
    """
    environ = os.environ if env is None else env
    value = environ.get(CACHE_DIR_ENV_VAR)
    if value:
        return Path(value)
    return DEFAULT_CACHE_DIR


def gaia_cache_key(
    center_ra: float,
    center_dec: float,
    radius_deg: float,
    *,
    g_limit: float = DEFAULT_G_LIMIT,
    maxrec: int = DEFAULT_MAXREC,
    digits: int = KEY_ROUND_DIGITS,
) -> str:
    """The canonical cache query string for one Gaia cone search.

    The center and radius are **rounded** to ``digits`` decimals of a degree
    before the key is built, so nearby fields share one cache entry; the
    g_limit and maxrec are part of the key because they change the result.
    """
    ra, dec, radius = _validate_center_radius(center_ra, center_dec, radius_deg)
    _validate_g_limit(g_limit)
    _validate_maxrec(maxrec)
    return json.dumps(
        {
            "ra": round(ra, digits),
            "dec": round(dec, digits),
            "radius_deg": round(radius, digits),
            "g_limit": float(g_limit),
            "maxrec": int(maxrec),
        },
        sort_keys=True,
    )


# --- input validation ----------------------------------------------------------------


def _validate_center_radius(
    center_ra: Any, center_dec: Any, radius_deg: Any
) -> tuple[float, float, float]:
    """Validate and normalize the cone-search center + radius.

    RA wraps modulo 360; Dec must lie in [-90, 90]; the radius must be in
    ``(0, MAX_RADIUS_DEG]`` degrees. Returns ``(ra_deg, dec_deg, radius_deg)``.

    Raises ``TypeError`` for non-numeric input and ``ValueError`` for
    out-of-range values.
    """
    if isinstance(center_ra, bool) or not isinstance(center_ra, (int, float)):
        raise TypeError(f"center_ra must be a number, got {center_ra!r}")
    if isinstance(center_dec, bool) or not isinstance(center_dec, (int, float)):
        raise TypeError(f"center_dec must be a number, got {center_dec!r}")
    if isinstance(radius_deg, bool) or not isinstance(radius_deg, (int, float)):
        raise TypeError(f"radius_deg must be a number, got {radius_deg!r}")
    ra = float(center_ra)
    dec = float(center_dec)
    radius = float(radius_deg)
    if not (math.isfinite(ra) and math.isfinite(dec) and math.isfinite(radius)):
        raise ValueError(
            f"center and radius must be finite, got ra={center_ra!r} dec={center_dec!r} "
            f"radius_deg={radius_deg!r}"
        )
    ra %= 360.0
    if not -90.0 <= dec <= 90.0:
        raise ValueError(f"center_dec must be in [-90, 90] deg, got {dec}")
    if not 0.0 < radius <= MAX_RADIUS_DEG:
        raise ValueError(f"radius_deg must be in (0, {MAX_RADIUS_DEG}] deg, got {radius}")
    return ra, dec, radius


def _validate_g_limit(g_limit: Any) -> float:
    if (
        isinstance(g_limit, bool)
        or not isinstance(g_limit, (int, float))
        or not math.isfinite(g_limit)
        or g_limit <= 0
    ):
        raise ValueError(f"g_limit must be a positive number, got {g_limit!r}")
    return float(g_limit)


def _validate_maxrec(maxrec: Any) -> int:
    if isinstance(maxrec, bool) or not isinstance(maxrec, int) or maxrec < 1:
        raise ValueError(f"maxrec must be an int >= 1, got {maxrec!r}")
    return int(maxrec)


# --- the wire query -------------------------------------------------------------------


def build_cone_query(
    center_ra: float,
    center_dec: float,
    radius_deg: float,
    *,
    g_limit: float = DEFAULT_G_LIMIT,
) -> str:
    """Build the ADQL Gaia DR3 cone-search query — the one place the wire
    query lives.

    Cone search (``CONTAINS(POINT(...), CIRCLE(...))``) over
    :data:`GAIA_DR3_TABLE` around ``(center_ra, center_dec)`` with
    ``radius_deg``, selecting the Gaia columns :data:`GAIA_COLUMNS`,
    restricted to ``Gmag < g_limit``, brightest first.
    """
    ra, dec, radius = _validate_center_radius(center_ra, center_dec, radius_deg)
    checked_g = _validate_g_limit(g_limit)
    columns = ", ".join(f'"{name}"' for name in GAIA_COLUMNS)
    return (
        f"SELECT {columns} FROM \"{GAIA_DR3_TABLE}\" "
        f"WHERE CONTAINS(POINT('ICRS', \"RAJ2000\", \"DEJ2000\"), "
        f"CIRCLE('ICRS', {ra!r}, {dec!r}, {radius!r})) = 1 "
        f'AND "Gmag" < {checked_g!r} '
        f'ORDER BY "Gmag"'
    )


# --- the single HTTP boundary -----------------------------------------------------------


def fetch_gaia_tap_json(
    query: str,
    *,
    maxrec: int = DEFAULT_MAXREC,
    timeout: float = DEFAULT_TIMEOUT_S,
    url: str = VIZIER_TAP_SYNC_URL,
) -> dict[str, Any]:
    """POST one ADQL ``query`` to the VizieR TAP sync endpoint; return parsed JSON.

    This is the single HTTP boundary of the Gaia query — tests monkeypatch
    this function (or :func:`urllib.request.urlopen`) instead of the network.

    Raises :class:`~.errors.GaiaUnavailableError` on network/HTTP failure and
    :class:`~.errors.GaiaResponseError` when the body is not a JSON object.
    """
    checked_maxrec = _validate_maxrec(maxrec)
    form = urllib.parse.urlencode(
        {
            "REQUEST": "doQuery",
            "LANG": "ADQL",
            "QUERY": query,
            "MAXREC": str(checked_maxrec),
            "FORMAT": "json",
            "RESPONSEFORMAT": "application/json",
        }
    ).encode("ascii")
    request = urllib.request.Request(
        url,
        data=form,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise GaiaUnavailableError(
            f"VizieR Gaia TAP returned HTTP {exc.code} ({exc.reason}) for the query"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GaiaUnavailableError(f"could not reach VizieR Gaia TAP at {url}: {exc}") from exc
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GaiaResponseError(f"VizieR Gaia TAP response is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise GaiaResponseError(
            f"VizieR Gaia TAP response is not a JSON object but {type(payload).__name__}"
        )
    return payload


# --- response parsing ---------------------------------------------------------------------


def _column_index(metadata: Any) -> dict[str, int]:
    """Map the GaiaStar field names to positions in each TAP JSON data row.

    Column names are matched case-insensitively against the accepted aliases
    (:data:`_COLUMN_ALIASES`). Raises :class:`GaiaResponseError` when the
    metadata is missing or lacks a required column.
    """
    if not isinstance(metadata, list) or not metadata:
        raise GaiaResponseError("VizieR Gaia TAP response has no 'metadata' column list")
    lowered: dict[str, int] = {}
    for pos, column in enumerate(metadata):
        if isinstance(column, dict) and isinstance(column.get("name"), str):
            lowered[column["name"].lower()] = pos
    index: dict[str, int] = {}
    for field, aliases in _COLUMN_ALIASES.items():
        for alias in aliases:
            pos = lowered.get(alias.lower())
            if pos is not None:
                index[field] = pos
                break
    missing = [f for f in _REQUIRED_FIELDS if f not in index]
    if missing:
        raise GaiaResponseError(
            f"VizieR Gaia TAP response metadata is missing column(s) {missing}"
        )
    return index


def _optional_float(value: Any, *, field: str) -> float | None:
    """Coerce one wire value to float; ``None``/empty -> ``None``."""
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise GaiaResponseError(f"Gaia row has a non-numeric {field}: {value!r}") from None
    if not math.isfinite(result):
        return None  # NaN/inf on the wire means "no value" in Gaia
    return result


def _tap_row_to_star(row: Any, columns: Mapping[str, int]) -> GaiaStar:
    """Parse one TAP JSON data row into a :class:`GaiaStar` — the single
    place where wire values become floats and a normalized J2000 coord."""
    if not isinstance(row, (list, tuple)):
        raise GaiaResponseError(f"Gaia data row is not a list: {row!r}")

    def cell(field: str) -> Any:
        pos = columns.get(field)
        if pos is None or pos >= len(row):
            return None
        return row[pos]

    ra = _optional_float(cell("ra"), field="RAJ2000")
    dec = _optional_float(cell("dec"), field="DEJ2000")
    if ra is None or dec is None:
        raise GaiaResponseError(f"Gaia row has no coordinates: {row!r}")
    if not 0.0 <= ra < 360.0:
        raise GaiaResponseError(f"Gaia row has RAJ2000 outside [0, 360) deg: {ra}")
    if not -90.0 <= dec <= 90.0:
        raise GaiaResponseError(f"Gaia row has DEJ2000 outside [-90, 90] deg: {dec}")
    source = cell("source_id")
    try:
        source_id = int(source)
    except (TypeError, ValueError):
        raise GaiaResponseError(f"Gaia row has a non-integer source id: {source!r}") from None
    return GaiaStar(
        source_id=source_id,
        ra_deg=ra,
        dec_deg=dec,
        g=_optional_float(cell("g"), field="Gmag"),
        bp=_optional_float(cell("bp"), field="BPmag"),
        rp=_optional_float(cell("rp"), field="RPmag"),
        parallax=_optional_float(cell("parallax"), field="Plx"),
        pmra=_optional_float(cell("pmra"), field="pmRA"),
        pmdec=_optional_float(cell("pmdec"), field="pmDE"),
    )


def parse_gaia_response(payload: Any) -> list[GaiaStar]:
    """Parse a VizieR Gaia TAP JSON payload into :class:`GaiaStar` rows.

    An empty ``data`` list parses to ``[]`` — a normal outcome (an empty
    field), not an error. Malformed payloads raise
    :class:`GaiaResponseError`. The result is sorted brightest-first (G
    ascending), unknown magnitudes last.
    """
    if not isinstance(payload, dict):
        raise GaiaResponseError(
            f"VizieR Gaia TAP response is not a JSON object but {type(payload).__name__}"
        )
    columns = _column_index(payload.get("metadata"))
    data = payload.get("data")
    if data is None:
        raise GaiaResponseError("VizieR Gaia TAP response has no 'data' rows")
    if not isinstance(data, list):
        raise GaiaResponseError(
            f"VizieR Gaia TAP 'data' is not a list but {type(data).__name__}"
        )
    stars = [_tap_row_to_star(row, columns) for row in data]
    stars.sort(key=lambda s: (s.g is None, s.g if s.g is not None else 0.0))
    return stars


# --- serialization for the cache (flat rows, JSON-safe) ---------------------------------

#: The fields of a serialized :class:`GaiaStar` cache row.
_STAR_FIELDS = frozenset(
    {"source_id", "ra_deg", "dec_deg", "g", "bp", "rp", "parallax", "pmra", "pmdec"}
)


def _star_to_row(star: GaiaStar) -> dict[str, Any]:
    """Serialize a :class:`GaiaStar` to a flat JSON-safe dict."""
    return {
        "source_id": star.source_id,
        "ra_deg": star.ra_deg,
        "dec_deg": star.dec_deg,
        "g": star.g,
        "bp": star.bp,
        "rp": star.rp,
        "parallax": star.parallax,
        "pmra": star.pmra,
        "pmdec": star.pmdec,
    }


def _row_to_star(row: Mapping[str, Any]) -> GaiaStar:
    """Rebuild a :class:`GaiaStar` from a cache row (inverse of :func:`_star_to_row`)."""
    return GaiaStar(
        source_id=int(row["source_id"]),
        ra_deg=float(row["ra_deg"]),
        dec_deg=float(row["dec_deg"]),
        g=row["g"],
        bp=row["bp"],
        rp=row["rp"],
        parallax=row["parallax"],
        pmra=row["pmra"],
        pmdec=row["pmdec"],
    )


def _is_gaia_rows(payload: Any) -> bool:
    """Validate a cached Gaia payload: a list of star-row dicts."""
    return isinstance(payload, list) and all(
        isinstance(row, dict) and set(row) == _STAR_FIELDS for row in payload
    )


# --- the public lookups ---------------------------------------------------------------------


def query_gaia(
    center_ra: float,
    center_dec: float,
    radius_deg: float,
    *,
    g_limit: float = DEFAULT_G_LIMIT,
    maxrec: int = DEFAULT_MAXREC,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> list[GaiaStar]:
    """Query Gaia DR3 for all sources with ``G < g_limit`` within
    ``radius_deg`` of ``(center_ra, center_dec)`` — the raw, uncached cone
    search.

    Returns the :class:`GaiaStar` rows sorted brightest-first; an empty
    field returns ``[]`` (a normal outcome, never an error).

    Raises:
        TypeError: non-numeric center / radius (nothing is sent);
        ValueError: out-of-range center / radius / ``g_limit`` / ``maxrec``
            (nothing is sent);
        GaiaUnavailableError: network/HTTP failure reaching VizieR;
        GaiaResponseError: malformed VizieR response.
    """
    ra, dec, radius = _validate_center_radius(center_ra, center_dec, radius_deg)
    checked_g = _validate_g_limit(g_limit)
    checked_maxrec = _validate_maxrec(maxrec)
    adql = build_cone_query(ra, dec, radius, g_limit=checked_g)
    payload = fetch_gaia_tap_json(adql, maxrec=checked_maxrec, timeout=timeout)
    return parse_gaia_response(payload)


def query_gaia_cached(
    center_ra: float,
    center_dec: float,
    radius_deg: float,
    cache: QueryCache | None = None,
    *,
    g_limit: float = DEFAULT_G_LIMIT,
    maxrec: int = DEFAULT_MAXREC,
    timeout: float = DEFAULT_TIMEOUT_S,
    digits: int = KEY_ROUND_DIGITS,
) -> list[GaiaStar]:
    """Cached variant of :func:`query_gaia` — same result, same errors.

    Goes through ``cache`` (a
    :class:`~seestar_controller.catalog.cache.QueryCache`; default: one in
    ``<config>/cache/gaia/`` with :data:`DEFAULT_TTL_S` TTL — the TTL is
    configured on the cache, pass your own ``QueryCache(ttl=...)`` to
    change it). The cache key is the **rounded** center + radius (see
    :func:`gaia_cache_key`), so repeat lookups of the same field are served
    from disk with no HTTP at all and work offline.

    Only validated results are cached (the TAP response is parsed before it
    is stored, and a cached payload is shape-checked on read), so a malformed
    response or a corrupted cache file is refetched, never served. An empty
    result is cached too. If the fetch fails, a valid — even expired —
    cached entry is served as a stale fallback (Gaia DR3 is static, so stale
    beats nothing); with no cache entry at all the failure becomes a clear
    :class:`~.errors.GaiaUnavailableError` (network/timeout) or passes
    through as :class:`~.errors.GaiaResponseError` (malformed response) —
    never a raw traceback.

    Raises the same errors as :func:`query_gaia`, plus
    :class:`~.errors.GaiaUnavailableError` when the query fails and no cache
    exists.
    """
    ra, dec, radius = _validate_center_radius(center_ra, center_dec, radius_deg)
    checked_g = _validate_g_limit(g_limit)
    checked_maxrec = _validate_maxrec(maxrec)
    if cache is None:
        cache = QueryCache(cache_dir=default_cache_dir(), ttl=DEFAULT_TTL_S)
    # Round first: the ADQL and the cache key are built from the same rounded
    # values, so nearby fields share one cache entry.
    ra_r = round(ra, digits)
    dec_r = round(dec, digits)
    radius_r = round(radius, digits) or radius  # never round a tiny radius down to 0
    key = gaia_cache_key(ra, dec, radius, g_limit=checked_g, maxrec=checked_maxrec, digits=digits)
    adql = build_cone_query(ra_r, dec_r, radius_r, g_limit=checked_g)

    def fetch() -> list[dict[str, Any]]:
        payload = fetch_gaia_tap_json(adql, maxrec=checked_maxrec, timeout=timeout)
        # Parse BEFORE caching: a malformed response is never stored.
        return [_star_to_row(s) for s in parse_gaia_response(payload)]

    stale: list[dict[str, Any]] | None = None
    found = cache.get_any(key)
    if found is not None and _is_gaia_rows(found[0]):
        payload, fresh = found
        if fresh:
            return [_row_to_star(row) for row in payload]
        stale = payload  # expired but valid: the offline fallback
    try:
        rows = fetch()
    except GaiaResponseError:
        if stale is not None:
            return [_row_to_star(row) for row in stale]
        raise
    except Exception as exc:  # network/timeout/HTTP/unexpected; never BaseException
        if stale is not None:
            return [_row_to_star(row) for row in stale]
        raise GaiaUnavailableError(
            f"Gaia DR3 cone search around (ra={ra_r}, dec={dec_r}) radius={radius_r} deg "
            f"failed and no cached result is available: {exc}. Retry when the network "
            f"is back — previously queried fields are served from the offline cache at "
            f"{cache.cache_dir}"
        ) from exc
    try:
        cache.set(key, rows)
    except OSError:
        pass  # best-effort: a failed cache write never breaks the query
    return [_row_to_star(row) for row in rows]
