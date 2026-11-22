"""Gaia DR3 cone search via Gaia TAP HTTP (VizieR, falling back to the ESA mirror), with a disk cache.

Pure-Python core of the gaia-crossmatch feature (module boundary per
``.scratch/gaia-crossmatch/spec.md``): no GTK imports, no
:mod:`seestar_controller.link` imports — a subprocess test enforces the
boundary. The only network access is stdlib :mod:`urllib` inside the single
HTTP boundary :func:`fetch_gaia_tap_json` — tests monkeypatch exactly that
function, so no test ever touches the network.

What lives here:

- :class:`GaiaStar` — one Gaia DR3 row: ``source_id``, J2000 position, and
  the selected columns ``g`` / ``bp`` / ``rp`` / ``parallax`` +
  ``parallax_error`` / ``pmra`` + ``pmra_error`` / ``pmdec`` +
  ``pmdec_error`` (``None`` when Gaia has no value);
- :func:`build_cone_query` — the ADQL cone search over the Gaia DR3 table of
  the queried endpoint (VizieR's :data:`GAIA_DR3_TABLE` ``I/355/gaiadr3`` or
  the ESA archive's :data:`ESA_GAIA_DR3_TABLE` ``gaiadr3.gaia_source``)
  around a center with a radius, selecting G/BP/RP/parallax(+error)/
  pmRA(+error)/pmDE(+error) and ``G < g_limit``;
- :func:`fetch_gaia_tap_json` — POST one ADQL query to one Gaia TAP
  ``sync`` endpoint (``url=``, default :data:`VIZIER_TAP_SYNC_URL`) and
  return the parsed JSON body — **the single HTTP boundary**;
- :func:`parse_gaia_response` — parse the TAP JSON payload into
  :class:`GaiaStar` rows (the one place wire values become floats);
- :func:`query_gaia` — the raw, uncached cone search over the endpoint chain
  :data:`GAIA_TAP_SYNC_URLS` (or a single pinned ``url=``);
- :func:`query_gaia_cached` — the cached variant: the response is stored on
  disk under the app config dir (``<config>/cache/gaia/``) keyed by the
  **rounded** field center + radius (+ g_limit + maxrec) with a TTL, reusing
  the catalog module's :class:`~seestar_controller.catalog.cache.QueryCache`
  (read-only reuse — this package never modifies catalog). A fresh cache
  entry is served with no HTTP at all; a fetch failure falls back to a stale
  entry when one exists, and raises :class:`~.errors.GaiaUnavailableError`
  when no cache exists.

ENDPOINTS (verified live 2026-11-20, see docs/wiki/progress.md): CDS
Strasbourg — including ``tapvizier.cds.unistra.fr`` — has been down since
the 2026-10-29 security incident, and the official VizieR mirrors (Harvard
CfA, IDIA) expose no TAP interface, so :data:`VIZIER_TAP_SYNC_URL` alone is
not enough: :func:`query_gaia` / :func:`query_gaia_cached` try
:data:`GAIA_TAP_SYNC_URLS` in order — VizieR first (kept for when Strasbourg
returns), then the ESA Gaia TAP mirror :data:`ESA_GAIA_TAP_SYNC_URL`
(``gea.esac.esa.int``, the Gaia archive at ESAC, verified HTTP 200) — and
the first endpoint that answers wins. Each endpoint is queried with the ADQL
dialect of its own schema (:func:`build_cone_query`); an explicit ``url=``
pins a single endpoint. Both serve ICRS, which coincides with J2000 at the
precision we need — the same convention as :mod:`seestar_controller.catalog`.

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
#: returns; see module docstring). Queried with the VizieR ADQL dialect.
VIZIER_TAP_SYNC_URL = "https://tapvizier.cds.unistra.fr/TAPVizieR/TAP/sync"

#: The ESA Gaia TAP mirror's ``sync`` endpoint — the Gaia archive at ESAC
#: (verified HTTP 200 on 2026-11-20, see docs/wiki/progress.md): the fallback
#: endpoint while VizieR is down. Queried with the Gaia-archive ADQL dialect.
ESA_GAIA_TAP_SYNC_URL = "https://gea.esac.esa.int/tap-server/tap/sync"

#: Gaia TAP ``sync`` endpoints tried in order by :func:`query_gaia` /
#: :func:`query_gaia_cached` when no explicit ``url=`` is given: VizieR first
#: (for when Strasbourg returns), then the ESA Gaia TAP mirror.
GAIA_TAP_SYNC_URLS = (
    VIZIER_TAP_SYNC_URL,
    ESA_GAIA_TAP_SYNC_URL,
)

#: The VizieR table queried: Gaia DR3 (Gaia Data Release 3 source table).
GAIA_DR3_TABLE = "I/355/gaiadr3"

#: The Gaia DR3 source table on the ESA Gaia archive (schema-qualified).
ESA_GAIA_DR3_TABLE = "gaiadr3.gaia_source"

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

#: User-Agent sent to the Gaia TAP endpoints (polite-identification convention).
USER_AGENT = "seestar-controller (Gaia DR3 cross-match)"

#: Largest cone-search radius ADQL accepts, in degrees.
MAX_RADIUS_DEG = 180.0

#: Arcseconds per degree (match-radius unit conversion).
ARCSEC_PER_DEG = 3600.0

#: The Gaia DR3 columns selected by the cone search, in SELECT order.
GAIA_COLUMNS = (
    "RAJ2000",
    "DEJ2000",
    "Source",
    "Gmag",
    "BPmag",
    "RPmag",
    "Plx",
    "PlxError",
    "pmRA",
    "pmRAError",
    "pmDE",
    "pmDEError",
)

#: The Gaia DR3 columns selected by the cone search on the ESA Gaia archive,
#: in SELECT order (the archive's own spellings, parsed via the same aliases).
ESA_GAIA_COLUMNS = (
    "ra",
    "dec",
    "source_id",
    "phot_g_mean_mag",
    "phot_bp_mean_mag",
    "phot_rp_mean_mag",
    "parallax",
    "pmra",
    "pmdec",
)

#: Accepted wire-name aliases per GaiaStar field (VizieR TAP spells them
#: ``RAJ2000``/``Source``/``Gmag``/``Plx``/``PlxError``/``pmRA``/``pmRAError``/
#: ``pmDE``/``pmDEError``; the Gaia archive spells them
#: ``ra``/``source_id``/``phot_g_mean_mag``/``parallax``/``parallax_error``/...).
_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "ra": ("RAJ2000", "RA_ICRS", "ra", "RA"),
    "dec": ("DEJ2000", "DE_ICRS", "dec", "DEC"),
    "source_id": ("Source", "SourceId", "source_id", "SOURCE_ID"),
    "g": ("Gmag", "G", "phot_g_mean_mag"),
    "bp": ("BPmag", "BP", "phot_bp_mean_mag"),
    "rp": ("RPmag", "RP", "phot_rp_mean_mag"),
    "parallax": ("Plx", "parallax"),
    "parallax_error": ("PlxError", "parallax_error"),
    "pmra": ("pmRA", "pmra", "PMRA"),
    "pmra_error": ("pmRAError", "pmra_error", "PMRAError"),
    "pmdec": ("pmDE", "pmDE", "pmdec", "PMDE"),
    "pmdec_error": ("pmDEError", "pmdec_error", "PMDEError"),
}

#: Fields without which a response cannot be parsed at all.
_REQUIRED_FIELDS = ("ra", "dec", "source_id")


@dataclass(frozen=True)
class _TapDialect:
    """The ADQL dialect of one Gaia TAP endpoint: its table and column names.

    VizieR spells the Gaia DR3 table ``I/355/gaiadr3`` with columns
    ``RAJ2000``/``DEJ2000``/``Gmag``/...; the ESA Gaia archive spells it
    ``gaiadr3.gaia_source`` with ``ra``/``dec``/``phot_g_mean_mag``/... — the
    same cone search needs a per-endpoint query (:func:`build_cone_query`),
    while the *response* parsing accepts both spellings (:data:`_COLUMN_ALIASES`).
    """

    table: str
    columns: tuple[str, ...]
    ra: str
    dec: str
    g: str


#: The ADQL dialect per known endpoint URL; an unknown ``url=`` gets the
#: VizieR dialect (the schema of :data:`GAIA_DR3_TABLE`).
_TAP_DIALECTS: dict[str, _TapDialect] = {
    VIZIER_TAP_SYNC_URL: _TapDialect(
        table=GAIA_DR3_TABLE, columns=GAIA_COLUMNS, ra="RAJ2000", dec="DEJ2000", g="Gmag"
    ),
    ESA_GAIA_TAP_SYNC_URL: _TapDialect(
        table=ESA_GAIA_DR3_TABLE,
        columns=ESA_GAIA_COLUMNS,
        ra="ra",
        dec="dec",
        g="phot_g_mean_mag",
    ),
}


@dataclass(frozen=True)
class GaiaStar:
    """One Gaia DR3 source row.

    ``source_id`` is the Gaia DR3 source identifier; ``ra_deg`` / ``dec_deg``
    the J2000 position in degrees; ``g`` / ``bp`` / ``rp`` the G / BP / RP
    magnitudes; ``parallax`` / ``parallax_error`` the parallax in mas and its
    1-sigma error; ``pmra`` / ``pmra_error``, ``pmdec`` / ``pmdec_error`` the
    proper motion in mas/yr (RA includes the cos(Dec) factor, as Gaia
    publishes it) and its 1-sigma error. The photometric / astrometric
    fields are ``None`` when Gaia has no value.
    """

    source_id: int
    ra_deg: float
    dec_deg: float
    g: float | None = None
    bp: float | None = None
    rp: float | None = None
    parallax: float | None = None
    parallax_error: float | None = None
    pmra: float | None = None
    pmra_error: float | None = None
    pmdec: float | None = None
    pmdec_error: float | None = None

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
    url: str = VIZIER_TAP_SYNC_URL,
) -> str:
    """Build the ADQL Gaia DR3 cone-search query — the one place the wire
    query lives.

    Cone search (``CONTAINS(POINT(...), CIRCLE(...))``) around
    ``(center_ra, center_dec)`` with ``radius_deg``, selecting the Gaia
    columns and restricted to the G magnitude ``< g_limit``, brightest
    first. The table and column names follow the endpoint ``url`` (the ADQL
    dialect of its schema — :data:`VIZIER_TAP_SYNC_URL` queries
    :data:`GAIA_DR3_TABLE` with the VizieR spellings, :data:`ESA_GAIA_TAP_SYNC_URL`
    the ESA archive table :data:`ESA_GAIA_DR3_TABLE` with the archive
    spellings; any other URL gets the VizieR dialect).
    """
    ra, dec, radius = _validate_center_radius(center_ra, center_dec, radius_deg)
    checked_g = _validate_g_limit(g_limit)
    dialect = _TAP_DIALECTS.get(url, _TAP_DIALECTS[VIZIER_TAP_SYNC_URL])
    columns = ", ".join(f'"{name}"' for name in dialect.columns)
    return (
        f'SELECT {columns} FROM "{dialect.table}" '
        f"WHERE CONTAINS(POINT('ICRS', \"{dialect.ra}\", \"{dialect.dec}\"), "
        f"CIRCLE('ICRS', {ra!r}, {dec!r}, {radius!r})) = 1 "
        f'AND "{dialect.g}" < {checked_g!r} '
        f'ORDER BY "{dialect.g}"'
    )


# --- the single HTTP boundary -----------------------------------------------------------


def fetch_gaia_tap_json(
    query: str,
    *,
    maxrec: int = DEFAULT_MAXREC,
    timeout: float = DEFAULT_TIMEOUT_S,
    url: str = VIZIER_TAP_SYNC_URL,
) -> dict[str, Any]:
    """POST one ADQL ``query`` to one Gaia TAP sync endpoint; return parsed JSON.

    This is the single HTTP boundary of the Gaia query — tests monkeypatch
    this function (or :func:`urllib.request.urlopen`) instead of the network.
    ``url`` is the endpoint to POST to (:data:`VIZIER_TAP_SYNC_URL` by
    default, :data:`ESA_GAIA_TAP_SYNC_URL` for the ESA mirror, or any other
    mirror pinned by the caller); the endpoint *chain* itself is walked one
    level up, in :func:`query_gaia` / :func:`query_gaia_cached`.

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
            f"Gaia TAP at {url} returned HTTP {exc.code} ({exc.reason}) for the query"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GaiaUnavailableError(f"could not reach Gaia TAP at {url}: {exc}") from exc
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GaiaResponseError(f"Gaia TAP at {url} response is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise GaiaResponseError(
            f"Gaia TAP at {url} response is not a JSON object but {type(payload).__name__}"
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
        raise GaiaResponseError("Gaia TAP response has no 'metadata' column list")
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
            f"Gaia TAP response metadata is missing column(s) {missing}"
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
        parallax_error=_optional_float(cell("parallax_error"), field="PlxError"),
        pmra=_optional_float(cell("pmra"), field="pmRA"),
        pmra_error=_optional_float(cell("pmra_error"), field="pmRAError"),
        pmdec=_optional_float(cell("pmdec"), field="pmDE"),
        pmdec_error=_optional_float(cell("pmdec_error"), field="pmDEError"),
    )


def parse_gaia_response(payload: Any) -> list[GaiaStar]:
    """Parse a Gaia TAP JSON payload into :class:`GaiaStar` rows.

    An empty ``data`` list parses to ``[]`` — a normal outcome (an empty
    field), not an error. Malformed payloads raise
    :class:`GaiaResponseError`. The result is sorted brightest-first (G
    ascending), unknown magnitudes last.
    """
    if not isinstance(payload, dict):
        raise GaiaResponseError(
            f"Gaia TAP response is not a JSON object but {type(payload).__name__}"
        )
    columns = _column_index(payload.get("metadata"))
    data = payload.get("data")
    if data is None:
        raise GaiaResponseError("Gaia TAP response has no 'data' rows")
    if not isinstance(data, list):
        raise GaiaResponseError(
            f"Gaia TAP 'data' is not a list but {type(data).__name__}"
        )
    stars = [_tap_row_to_star(row, columns) for row in data]
    stars.sort(key=lambda s: (s.g is None, s.g if s.g is not None else 0.0))
    return stars


# --- serialization for the cache (flat rows, JSON-safe) ---------------------------------

#: The fields of a serialized :class:`GaiaStar` cache row.
_STAR_FIELDS = frozenset(
    {
        "source_id",
        "ra_deg",
        "dec_deg",
        "g",
        "bp",
        "rp",
        "parallax",
        "parallax_error",
        "pmra",
        "pmra_error",
        "pmdec",
        "pmdec_error",
    }
)

#: The cache-row fields every entry has carried since the cache format was
#: introduced — cache entries written before the error columns were selected
#: (``parallax_error`` / ``pmra_error`` / ``pmdec_error``) are still valid,
#: the error fields just read back as ``None``.
_REQUIRED_STAR_FIELDS = _STAR_FIELDS - {"parallax_error", "pmra_error", "pmdec_error"}


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
        "parallax_error": star.parallax_error,
        "pmra": star.pmra,
        "pmra_error": star.pmra_error,
        "pmdec": star.pmdec,
        "pmdec_error": star.pmdec_error,
    }


def _row_to_star(row: Mapping[str, Any]) -> GaiaStar:
    """Rebuild a :class:`GaiaStar` from a cache row (inverse of :func:`_star_to_row`).

    The error fields are read with ``.get``: a cache row written before the
    error columns were selected has no such keys and yields ``None``.
    """
    return GaiaStar(
        source_id=int(row["source_id"]),
        ra_deg=float(row["ra_deg"]),
        dec_deg=float(row["dec_deg"]),
        g=row["g"],
        bp=row["bp"],
        rp=row["rp"],
        parallax=row["parallax"],
        parallax_error=row.get("parallax_error"),
        pmra=row["pmra"],
        pmra_error=row.get("pmra_error"),
        pmdec=row["pmdec"],
        pmdec_error=row.get("pmdec_error"),
    )


def _is_gaia_rows(payload: Any) -> bool:
    """Validate a cached Gaia payload: a list of star-row dicts whose keys are
    the required star fields plus at most the optional error fields."""
    return isinstance(payload, list) and all(
        isinstance(row, dict) and _REQUIRED_STAR_FIELDS <= set(row) <= _STAR_FIELDS
        for row in payload
    )


# --- the endpoint chain -----------------------------------------------------------------------


def _cone_search(
    ra: float,
    dec: float,
    radius: float,
    *,
    g_limit: float,
    maxrec: int,
    timeout: float,
    url: str | None,
) -> list[GaiaStar]:
    """Run the cone search over the endpoint chain; return the parsed rows.

    ``url=None`` tries :data:`GAIA_TAP_SYNC_URLS` in order — VizieR first,
    then the ESA Gaia mirror — and the first endpoint that answers wins; an
    explicit ``url`` pins exactly that one endpoint (no fallback). Each
    endpoint is POSTed the ADQL of its own dialect (:func:`build_cone_query`)
    via the single HTTP boundary :func:`fetch_gaia_tap_json`.

    A network/HTTP failure (:class:`~.errors.GaiaUnavailableError`) moves on
    to the next endpoint; when **every** endpoint is unreachable, one
    aggregated :class:`~.errors.GaiaUnavailableError` naming all endpoints
    tried is raised. A malformed response from an endpoint that did answer
    raises :class:`~.errors.GaiaResponseError` immediately (no fallback —
    that is a response problem, not an unreachable endpoint).
    """
    endpoints = (url,) if url is not None else GAIA_TAP_SYNC_URLS
    failures: list[str] = []
    for endpoint in endpoints:
        adql = build_cone_query(ra, dec, radius, g_limit=g_limit, url=endpoint)
        try:
            payload = fetch_gaia_tap_json(adql, maxrec=maxrec, timeout=timeout, url=endpoint)
        except GaiaUnavailableError as exc:
            failures.append(f"{endpoint}: {exc}")
            continue
        return parse_gaia_response(payload)
    raise GaiaUnavailableError(
        "could not reach the Gaia TAP at any endpoint ("
        + ", ".join(endpoints)
        + "): "
        + "; ".join(failures)
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
    url: str | None = None,
) -> list[GaiaStar]:
    """Query Gaia DR3 for all sources with ``G < g_limit`` within
    ``radius_deg`` of ``(center_ra, center_dec)`` — the raw, uncached cone
    search.

    Endpoints are tried in order — :data:`GAIA_TAP_SYNC_URLS` (VizieR, then
    the ESA Gaia mirror) unless ``url`` pins a single one — each with the
    ADQL dialect of its schema and its own ``timeout``; the first success
    wins. :class:`~.errors.GaiaUnavailableError` is raised only when **every**
    endpoint is unreachable (network/HTTP failure); a malformed response from
    an endpoint that did answer raises :class:`~.errors.GaiaResponseError`
    immediately.

    Returns the :class:`GaiaStar` rows sorted brightest-first; an empty
    field returns ``[]`` (a normal outcome, never an error).

    Raises:
        TypeError: non-numeric center / radius (nothing is sent);
        ValueError: out-of-range center / radius / ``g_limit`` / ``maxrec``
            (nothing is sent);
        GaiaUnavailableError: no endpoint could be reached;
        GaiaResponseError: malformed response from the endpoint that answered.
    """
    ra, dec, radius = _validate_center_radius(center_ra, center_dec, radius_deg)
    checked_g = _validate_g_limit(g_limit)
    checked_maxrec = _validate_maxrec(maxrec)
    return _cone_search(
        ra, dec, radius, g_limit=checked_g, maxrec=checked_maxrec, timeout=timeout, url=url
    )


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
    url: str | None = None,
) -> list[GaiaStar]:
    """Cached variant of :func:`query_gaia` — same result, same errors.

    Goes through ``cache`` (a
    :class:`~seestar_controller.catalog.cache.QueryCache`; default: one in
    ``<config>/cache/gaia/`` with :data:`DEFAULT_TTL_S` TTL — the TTL is
    configured on the cache, pass your own ``QueryCache(ttl=...)`` to
    change it). The cache key is the **rounded** center + radius (see
    :func:`gaia_cache_key`), so repeat lookups of the same field are served
    from disk with no HTTP at all and work offline — the cache always wins
    before any endpoint is contacted.

    The fetch walks the same endpoint chain as :func:`query_gaia`
    (:data:`GAIA_TAP_SYNC_URLS`, or the single pinned ``url=``): an
    unreachable endpoint falls back to the next one (e.g. VizieR -> the ESA
    Gaia mirror), and only when every endpoint fails does the error surface.

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

    def fetch() -> list[dict[str, Any]]:
        stars = _cone_search(
            ra_r, dec_r, radius_r, g_limit=checked_g, maxrec=checked_maxrec, timeout=timeout, url=url
        )
        # Parsed BEFORE caching: a malformed response is never stored.
        return [_star_to_row(s) for s in stars]

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
