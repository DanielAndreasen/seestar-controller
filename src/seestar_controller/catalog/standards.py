"""Standard-star (comparison-star) lookup near a target position.

The catalog assist for photometry comp picking (GLOSSARY: **comparison star** /
**comp ensemble**): given target coordinates and a radius, return the known
standard/comparison stars within that radius, filtered by angular distance and
sorted by magnitude closeness to the target's magnitude.

Pure-Python catalog client (module boundary per ``.scratch/catalog/spec.md``):
no GTK imports, no :mod:`seestar_controller.link` imports, stdlib only.

Catalog choice: **APASS DR9** — the VizieR table ``II/336/apass9`` (AAVSO
Photometric All-Sky Survey) — queried through the public **VizieR TAP**
synchronous endpoint (:data:`VIZIER_TAP_SYNC_URL`) with an ADQL cone search
(:func:`build_cone_query`) when CDS Strasbourg answers, and through the
classic **viz-bin asu-tsv** interface on the official VizieR mirrors
(:data:`VIZIER_ASU_TSV_URLS`, :func:`fetch_vizier_asu_tsv`) otherwise. APASS
is chosen because it is all-sky and dense (a cone search around any target
actually returns stars) and carries B/V/g/r/i magnitudes; the AAVSO chart
sequences are not published as a single TAP table, and the Landolt standards
(``V/139``) are sparse — about a thousand stars in a handful of standard
fields, so a cone search around an arbitrary target usually comes back empty.
The photometric band is selected through :data:`BAND_COLUMNS` (default ``V``).

ENDPOINTS (verified live 2026-11-20, see docs/wiki/progress.md): **CDS
Strasbourg is down** — a cybersecurity incident since ~2026-10-29;
``tapvizier.cds.unistra.fr`` times out and ``vizier.cds.unistra.fr`` answers
404, with no ETA for recovery, and no TAP mirrors exist. The official VizieR
mirrors (Harvard CfA, IDIA) serve the classic ``viz-bin/asu-tsv`` interface,
so :func:`fetch_standards_payload` — the single HTTP boundary — tries the
endpoints in order (CDS TAP -> Harvard asu-tsv -> IDIA asu-tsv) and normalizes
both response shapes into the same TAP-JSON payload
(``{"metadata": ..., "data": ...}``) that :func:`parse_cone_response` consumes;
the first success wins and cache keys stay endpoint-agnostic (keyed on the
ADQL cone query). VizieR serves ICRS, which coincides with J2000 at the
precision we need — the same convention as :mod:`.simbad`.

The HTTP layer is the single function :func:`fetch_standards_payload`, which
composes the per-endpoint primitives :func:`fetch_vizier_tap_json` (CDS TAP)
and :func:`fetch_vizier_asu_tsv` + :func:`parse_asu_tsv` (mirrors) — tests
monkeypatch those functions (or :func:`urllib.request.urlopen` for the fetch
itself), so no test ever touches the network.

Filtering reuses :func:`~.coords.angular_distance` (never reimplemented here).
An empty result — no standards nearby — is a **normal** outcome and returns
``[]``; it never raises. All catalog queries go through the cache wrapper
(ticket 03 review nit F4): :func:`find_standard_stars_cached` routes through
:meth:`~.cache.QueryCache.cached`, so repeat lookups are instant and work
offline; the raw :func:`find_standard_stars` stays uncached.

Errors reuse the catalog taxonomy: :class:`~.simbad.CatalogError` with
:class:`StandardsUnavailableError` (network/HTTP failure, mirroring
:class:`~.simbad.SimbadUnavailableError`) and :class:`StandardsResponseError`
(malformed response, mirroring :class:`~.simbad.SimbadResponseError`) — never
a raw traceback. A malformed/error body from one endpoint (an HTML error
page, a rate-limit page, a truncated TSV — anything
:func:`parse_asu_tsv` rejects) does **not** abort the lookup: it is recorded
and the next endpoint is tried; the aggregate error is raised only when
every endpoint failed, and its malformed-response messages carry a snippet
of the response's first line for diagnosis.
"""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .cache import QueryCache
from .coords import J2000Coord, angular_distance
from .simbad import CatalogError

#: VizieR's public TAP synchronous endpoint at CDS Strasbourg (canonical; down
#: since the 2026-10-29 security incident — kept first for when it returns).
VIZIER_TAP_SYNC_URL = "https://tapvizier.cds.unistra.fr/TAPVizieR/TAP/sync"

#: Official VizieR mirror at the Center for Astrophysics | Harvard & Smithsonian
#: (classic ``viz-bin/asu-tsv`` interface — no TAP mirrors exist).
VIZIER_ASU_TSV_URL_CFA = "https://vizier.cfa.harvard.edu/viz-bin/asu-tsv"

#: Official VizieR mirror at IDIA (South Africa), same ``asu-tsv`` interface.
VIZIER_ASU_TSV_URL_IDIA = "https://vizier.idia.ac.za/viz-bin/asu-tsv"

#: VizieR asu-tsv mirror endpoints tried after the CDS TAP endpoint (verified
#: live 2026-11-20 during the CDS outage — see module docstring).
VIZIER_ASU_TSV_URLS = (VIZIER_ASU_TSV_URL_CFA, VIZIER_ASU_TSV_URL_IDIA)

#: The VizieR table queried for standard/comparison stars: APASS DR9.
VIZIER_TABLE = "II/336/apass9"

#: Default HTTP timeout for one VizieR query, in seconds.
DEFAULT_TIMEOUT_S = 15.0

#: Default cap on returned rows (TAP ``MAXREC``).
DEFAULT_MAX_STANDARDS = 100

#: Default photometric band whose magnitude is reported.
DEFAULT_BAND = "V"

#: Band letter -> APASS DR9 magnitude column (the one place the mapping lives).
BAND_COLUMNS = {"B": "Bmag", "V": "Vmag", "g": "gmag", "r": "rmag", "i": "imag"}

#: User-Agent sent to VizieR (polite-identification convention).
USER_AGENT = "seestar-controller (catalog standard-star lookup)"

#: Columns without which a response cannot be parsed at all (plus the band's
#: magnitude column, added by the parser).
_REQUIRED_COLUMNS = ("RAJ2000", "DEJ2000")

#: Largest cone-search radius ADQL accepts, in degrees.
MAX_RADIUS_DEG = 180.0


class StandardsUnavailableError(CatalogError):
    """VizieR could not be reached (network or HTTP failure)."""


class StandardsResponseError(CatalogError):
    """VizieR returned a malformed or unusable response."""


@dataclass(frozen=True)
class StandardStar:
    """One known standard/comparison star near the target.

    ``coord`` is the J2000 position (degrees); ``magnitude`` is the star's
    magnitude in ``band`` (``None`` when the catalog has none);
    ``distance_deg`` is the great-circle distance from the target, computed
    with :func:`~.coords.angular_distance`.
    """

    name: str
    coord: J2000Coord
    magnitude: float | None
    band: str
    distance_deg: float

    @property
    def ra_deg(self) -> float:
        """Right ascension in degrees, [0, 360)."""
        return self.coord.ra_deg

    @property
    def dec_deg(self) -> float:
        """Declination in degrees, [-90, 90]."""
        return self.coord.dec_deg

    @property
    def ra_hours(self) -> float:
        """Right ascension in hours, [0, 24) — the unit ``link.Device.goto`` takes."""
        return self.coord.ra_hours

    @property
    def coords_j2000(self) -> tuple[float, float]:
        """``(ra_deg, dec_deg)`` — J2000 coordinates in degrees."""
        return self.coord.as_degrees()


# --- serialization for the cache (flat rows, JSON-safe) ------------------------

#: The fields of a serialized :class:`StandardStar` cache row.
_STAR_FIELDS = frozenset(
    {"name", "ra_deg", "dec_deg", "magnitude", "band", "distance_deg"}
)


def _star_to_row(star: StandardStar) -> dict[str, Any]:
    """Serialize a :class:`StandardStar` to a flat JSON-safe dict."""
    return {
        "name": star.name,
        "ra_deg": star.ra_deg,
        "dec_deg": star.dec_deg,
        "magnitude": star.magnitude,
        "band": star.band,
        "distance_deg": star.distance_deg,
    }


def _row_to_star(row: Mapping[str, Any]) -> StandardStar:
    """Rebuild a :class:`StandardStar` from a cache row (inverse of :func:`_star_to_row`)."""
    return StandardStar(
        name=row["name"],
        coord=J2000Coord.from_degrees(row["ra_deg"], row["dec_deg"]),
        magnitude=row["magnitude"],
        band=row["band"],
        distance_deg=row["distance_deg"],
    )


def _is_standard_rows(payload: Any) -> bool:
    """Validate a cached standard-star payload: a list of star-row dicts."""
    return isinstance(payload, list) and all(
        isinstance(row, dict) and set(row) == _STAR_FIELDS for row in payload
    )


def _standards_query(adql: str, *, maxrec: int) -> str:
    """The canonical cache query string for one cone search.

    The ADQL alone does not determine the result (``maxrec`` is a separate
    TAP form field), so the key covers both — same pattern as the name-lookup
    key in :mod:`.cache`. ``target_mag``/``mag_range`` are deliberately NOT in
    the key: they only re-filter/re-sort the cached rows, never change the
    fetched cone.
    """
    return json.dumps({"query": adql, "maxrec": int(maxrec)}, sort_keys=True)


# --- input coercion / validation ------------------------------------------------


def _as_coord(coord: Any) -> J2000Coord:
    """Coerce ``coord`` to a :class:`J2000Coord` (the single normalization point).

    Accepts a :class:`~.coords.J2000Coord`, a ``(ra_deg, dec_deg)`` pair, or
    any object with ``ra_deg``/``dec_deg`` attributes (e.g. a SIMBAD
    candidate). Invalid values raise :class:`~.coords.CoordinateError` (a
    :class:`ValueError`).
    """
    if isinstance(coord, J2000Coord):
        return coord
    if isinstance(coord, (tuple, list)) and len(coord) == 2:
        return J2000Coord.from_degrees(coord[0], coord[1])
    ra = getattr(coord, "ra_deg", None)
    dec = getattr(coord, "dec_deg", None)
    if ra is not None and dec is not None:
        return J2000Coord.from_degrees(ra, dec)
    raise ValueError(
        "coord must be a J2000Coord, a (ra_deg, dec_deg) pair, or an object "
        f"with ra_deg/dec_deg attributes, got {coord!r}"
    )


def _validate_radius(radius_deg: Any) -> float:
    if (
        isinstance(radius_deg, bool)
        or not isinstance(radius_deg, (int, float))
        or not math.isfinite(radius_deg)
        or not 0.0 < radius_deg <= MAX_RADIUS_DEG
    ):
        raise ValueError(
            f"radius_deg must be a number in (0, {MAX_RADIUS_DEG}] deg, got {radius_deg!r}"
        )
    return float(radius_deg)


def _validate_band(band: Any) -> str:
    if band not in BAND_COLUMNS:
        raise ValueError(
            f"band must be one of {sorted(BAND_COLUMNS)}, got {band!r}"
        )
    return band


def _validate_target_mag(target_mag: Any) -> float | None:
    if target_mag is None:
        return None
    if (
        isinstance(target_mag, bool)
        or not isinstance(target_mag, (int, float))
        or not math.isfinite(target_mag)
    ):
        raise ValueError(f"target_mag must be a finite number or None, got {target_mag!r}")
    return float(target_mag)


def _validate_mag_range(mag_range: Any) -> tuple[float, float] | None:
    if mag_range is None:
        return None
    if not isinstance(mag_range, (tuple, list)) or len(mag_range) != 2:
        raise ValueError(
            f"mag_range must be a (lo, hi) pair or None, got {mag_range!r}"
        )
    lo, hi = mag_range
    for value in (lo, hi):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ValueError(f"mag_range bounds must be finite numbers, got {mag_range!r}")
    if lo > hi:
        raise ValueError(f"mag_range must have lo <= hi, got {mag_range!r}")
    return float(lo), float(hi)


def _validate_args(
    coord: Any,
    radius_deg: Any,
    *,
    maxrec: int,
    band: Any,
    target_mag: Any,
    mag_range: Any,
) -> tuple[J2000Coord, float, str, float | None, tuple[float, float] | None]:
    """Validate the shared arguments of both lookup functions (one place)."""
    target = _as_coord(coord)
    radius = _validate_radius(radius_deg)
    if isinstance(maxrec, bool) or not isinstance(maxrec, int) or maxrec < 1:
        raise ValueError(f"maxrec must be an int >= 1, got {maxrec!r}")
    checked_band = _validate_band(band)
    checked_mag = _validate_target_mag(target_mag)
    checked_range = _validate_mag_range(mag_range)
    return target, radius, checked_band, checked_mag, checked_range


# --- the wire query --------------------------------------------------------------


def build_cone_query(
    coord: J2000Coord | Any, radius_deg: float, *, band: str = DEFAULT_BAND
) -> str:
    """Build the ADQL cone-search query — the one place the wire query lives.

    Cone search around ``(ra, dec)`` with ``radius_deg`` (ADQL
    ``CONTAINS(POINT(...), CIRCLE(...))``) over the APASS DR9 table, returning
    positions and the band's magnitude, brightest first.
    """
    target = _as_coord(coord)
    radius = _validate_radius(radius_deg)
    checked_band = _validate_band(band)
    mag_column = BAND_COLUMNS[checked_band]
    return (
        f"SELECT RAJ2000, DEJ2000, {mag_column} FROM \"{VIZIER_TABLE}\" "
        f"WHERE CONTAINS(POINT('ICRS', RAJ2000, DEJ2000), "
        f"CIRCLE('ICRS', {target.ra_deg!r}, {target.dec_deg!r}, {radius!r})) = 1 "
        f"ORDER BY {mag_column}"
    )


# --- the single HTTP boundary -----------------------------------------------------


def fetch_vizier_tap_json(
    query: str,
    *,
    maxrec: int = DEFAULT_MAX_STANDARDS,
    timeout: float = DEFAULT_TIMEOUT_S,
    url: str = VIZIER_TAP_SYNC_URL,
) -> dict[str, Any]:
    """POST one ADQL ``query`` to VizieR's TAP sync endpoint; return parsed JSON.

    This is the single HTTP boundary of the standard-star lookup — tests
    monkeypatch this function (or :func:`urllib.request.urlopen`) instead of
    the network.

    Raises :class:`StandardsUnavailableError` on network/HTTP failure and
    :class:`StandardsResponseError` when the body is not a JSON object.
    """
    form = urllib.parse.urlencode(
        {
            "REQUEST": "doQuery",
            "LANG": "ADQL",
            "QUERY": query,
            "MAXREC": str(int(maxrec)),
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
        raise StandardsUnavailableError(
            f"VizieR returned HTTP {exc.code} ({exc.reason}) for the query"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise StandardsUnavailableError(f"could not reach VizieR at {url}: {exc}") from exc
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        first_line = _first_line_snippet(body.decode("utf-8", errors="replace"))
        raise StandardsResponseError(
            f"VizieR response is not valid JSON: {exc} (first line: {first_line!r})"
        ) from exc
    if not isinstance(payload, dict):
        raise StandardsResponseError(
            f"VizieR response is not a JSON object but {type(payload).__name__}"
        )
    return payload


# --- the classic viz-bin asu-tsv mirror interface ---------------------------------


def build_asu_tsv_url(
    coord: Any,
    radius_deg: float,
    *,
    band: str = DEFAULT_BAND,
    maxrec: int = DEFAULT_MAX_STANDARDS,
    url: str = VIZIER_ASU_TSV_URL_CFA,
) -> str:
    """Build one ``viz-bin/asu-tsv`` cone-search URL on a VizieR mirror.

    Parameters (verified live 2026-11-20 on the Harvard and IDIA mirrors):
    ``-source=<table>``, ``-c=<ra_deg>+<dec_deg>`` (center in degrees, the sign
    of Dec included, ``+`` kept literal), ``-c.rm=<radius_arcmin>`` (radius in
    **arcminutes** = ``radius_deg * 60``), ``-out.max=<n>`` (row cap) and
    ``-out.all=`` (all columns).
    """
    target = _as_coord(coord)
    radius = _validate_radius(radius_deg)
    _validate_band(band)
    if isinstance(maxrec, bool) or not isinstance(maxrec, int) or maxrec < 1:
        raise ValueError(f"maxrec must be an int >= 1, got {maxrec!r}")
    params = [
        ("-source", VIZIER_TABLE),
        ("-c", f"{target.ra_deg!r}+{target.dec_deg!r}"),
        ("-c.rm", repr(radius * 60.0)),  # degrees -> arcminutes
        ("-out.max", str(int(maxrec))),
        ("-out.all", ""),
    ]
    # keep the '+' separator of -c literal (the mirrors parse it that way)
    query = urllib.parse.urlencode(params, safe="+", quote_via=urllib.parse.quote)
    return f"{url}?{query}"


def fetch_vizier_asu_tsv(
    coord: Any,
    radius_deg: float,
    *,
    band: str = DEFAULT_BAND,
    maxrec: int = DEFAULT_MAX_STANDARDS,
    timeout: float = DEFAULT_TIMEOUT_S,
    url: str = VIZIER_ASU_TSV_URL_CFA,
) -> str:
    """GET the classic ``viz-bin/asu-tsv`` cone search on one VizieR mirror.

    Returns the raw ASU TSV text (``#``-prefixed metadata lines, then the
    header / unit / dashes rows, then tab-separated data rows) — parsed by
    :func:`parse_asu_tsv`. Raises :class:`StandardsUnavailableError` on
    network/HTTP failure and :class:`StandardsResponseError` when the body is
    not decodable text.
    """
    target_url = build_asu_tsv_url(coord, radius_deg, band=band, maxrec=maxrec, url=url)
    request = urllib.request.Request(
        target_url,
        headers={
            "Accept": "text/tab-separated-values",
            "User-Agent": USER_AGENT,
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise StandardsUnavailableError(
            f"VizieR mirror returned HTTP {exc.code} ({exc.reason}) at {url}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise StandardsUnavailableError(f"could not reach VizieR mirror at {url}: {exc}") from exc
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StandardsResponseError(
            f"VizieR asu-tsv response at {url} is not valid UTF-8: {exc}"
        ) from exc


def _line_snippet(line: str, limit: int = 80) -> str:
    """A short, single-line snippet of ``line`` for error messages.

    Strips surrounding whitespace and truncates to ``limit`` characters (with
    a ``...`` marker) so an HTML error page never floods the message.
    """
    stripped = line.strip()
    if len(stripped) > limit:
        return stripped[:limit] + "..."
    return stripped


def _first_line_snippet(text: str, limit: int = 80) -> str:
    """The snippet of the first non-empty line of ``text`` (``""`` when empty)."""
    for line in text.splitlines():
        if line.strip():
            return _line_snippet(line, limit)
    return ""


def parse_asu_tsv(text: Any, *, band: str = DEFAULT_BAND) -> dict[str, Any]:
    """Parse a ``viz-bin/asu-tsv`` response into the TAP-JSON payload shape.

    Normalizes the ASU TSV (``#`` metadata lines, a header row of column
    names, a unit row, a dashes separator row, then tab-separated data rows)
    into ``{"metadata": [{"name": ...}, ...], "data": [[...], ...]}`` — the
    exact shape :func:`parse_cone_response` consumes, so both endpoint kinds
    flow through the same parser. Column names are normalized by stripping
    apostrophes (the asu-tsv header spells the APASS bands ``g'mag`` /
    ``r'mag`` / ``i'mag`` where TAP spells them ``gmag`` / ``rmag`` /
    ``imag``). Raises :class:`StandardsResponseError` on a malformed response
    (an HTML error page, a rate-limit page, a truncated TSV); the message
    always shows a snippet of what the response actually contained — the
    parsed header row and the first response line — so a bad mirror is
    diagnosable from the error alone.
    """
    if not isinstance(text, str):
        raise StandardsResponseError(
            f"VizieR asu-tsv response is not text but {type(text).__name__}"
        )
    checked_band = _validate_band(band)
    mag_column = BAND_COLUMNS[checked_band]
    rows = [
        line
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not rows:
        raise StandardsResponseError(
            "VizieR asu-tsv response has no header row "
            f"(first line: {_first_line_snippet(text)!r})"
        )
    header = [name.strip().replace("'", "") for name in rows[0].split("\t")]
    rest = rows[1:]
    separator = None
    for i, line in enumerate(rest):
        fields = line.split("\t")
        if fields and all(field and set(field) <= {"-"} for field in fields):
            separator = i  # the dashes row between the unit row and the data
            break
    if separator is not None:
        data_lines = rest[separator + 1 :]
    else:
        data_lines = rest[1:] if len(rest) > 1 else []  # skip the unit row
    missing = [c for c in (*(_REQUIRED_COLUMNS), mag_column) if c not in header]
    if missing:
        raise StandardsResponseError(
            f"VizieR asu-tsv response is missing column(s) {missing} "
            f"(header: {_line_snippet(rows[0])!r}, "
            f"first line: {_first_line_snippet(text)!r})"
        )
    data = [[field.strip() for field in line.split("\t")] for line in data_lines]
    return {"metadata": [{"name": name} for name in header], "data": data}


def fetch_standards_payload(
    coord: Any,
    radius_deg: float,
    *,
    band: str = DEFAULT_BAND,
    maxrec: int = DEFAULT_MAX_STANDARDS,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    """Fetch one APASS cone search from VizieR — the single HTTP boundary.

    Tries the endpoints in order — CDS TAPVizieR (:func:`fetch_vizier_tap_json`
    with the ADQL cone query, for when Strasbourg is back), then each
    asu-tsv mirror (:func:`fetch_vizier_asu_tsv` + :func:`parse_asu_tsv`) —
    and normalizes both into the same TAP-JSON payload
    :func:`parse_cone_response` consumes. The first success wins; each
    endpoint gets its own ``timeout``.

    **Every** endpoint failure — network/HTTP (:class:`StandardsUnavailableError`)
    *and* a malformed body from an endpoint that did answer (an HTML error
    page, a rate-limit page, a truncated TSV: :class:`StandardsResponseError`
    from :func:`parse_asu_tsv` / :func:`fetch_vizier_tap_json`) — is recorded
    and the next endpoint is tried; one bad mirror never aborts the lookup.
    Only when **every** endpoint failed does this raise, with an aggregate
    message naming each endpoint and its failure (the malformed-response
    messages carry a snippet of the response's first line):
    :class:`StandardsResponseError` when any endpoint answered with a
    malformed body (the lookup reached VizieR — the response is the problem),
    else :class:`StandardsUnavailableError` when no endpoint could be reached
    at all. Cache keys stay endpoint-agnostic (keyed on the ADQL cone query,
    as before).
    """
    target = _as_coord(coord)
    radius = _validate_radius(radius_deg)
    checked_band = _validate_band(band)
    if isinstance(maxrec, bool) or not isinstance(maxrec, int) or maxrec < 1:
        raise ValueError(f"maxrec must be an int >= 1, got {maxrec!r}")
    adql = build_cone_query(target, radius, band=checked_band)
    attempts: list[tuple[str, Callable[[], dict[str, Any]]]] = [
        (
            f"CDS TAP {VIZIER_TAP_SYNC_URL}",
            lambda: fetch_vizier_tap_json(adql, maxrec=maxrec, timeout=timeout),
        )
    ]
    for mirror in VIZIER_ASU_TSV_URLS:
        attempts.append(
            (
                f"asu-tsv {mirror}",
                lambda mirror=mirror: parse_asu_tsv(
                    fetch_vizier_asu_tsv(
                        target, radius, band=checked_band, maxrec=maxrec, timeout=timeout, url=mirror
                    ),
                    band=checked_band,
                ),
            )
        )
    failures: list[str] = []
    saw_malformed_response = False
    for label, attempt in attempts:
        try:
            return attempt()
        except (StandardsUnavailableError, StandardsResponseError) as exc:
            # a failed endpoint (unreachable OR malformed body) falls through
            # to the next one — recorded for the aggregate error below
            failures.append(f"{label}: {exc}")
            saw_malformed_response = saw_malformed_response or isinstance(
                exc, StandardsResponseError
            )
    aggregate = "; ".join(failures)
    if saw_malformed_response:
        raise StandardsResponseError(
            "VizieR returned a malformed response from every endpoint that answered ("
            + aggregate
            + ")"
        )
    raise StandardsUnavailableError(
        "could not reach VizieR at any endpoint (" + aggregate + ")"
    )


# --- response parsing ---------------------------------------------------------------


def _column_index(metadata: Any, *, mag_column: str) -> dict[str, int]:
    """Map TAP JSON metadata column names to positions in each data row."""
    if not isinstance(metadata, list) or not metadata:
        raise StandardsResponseError("VizieR response has no 'metadata' column list")
    index: dict[str, int] = {}
    for pos, column in enumerate(metadata):
        if isinstance(column, dict) and isinstance(column.get("name"), str):
            index[column["name"]] = pos
    missing = [c for c in (*(_REQUIRED_COLUMNS), mag_column) if c not in index]
    if missing:
        raise StandardsResponseError(
            f"VizieR response metadata is missing column(s) {missing}"
        )
    return index


def _optional_float(value: Any, *, field: str) -> float | None:
    """Coerce one wire value to float; ``None``/empty -> ``None``."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise StandardsResponseError(
            f"VizieR row has a non-numeric {field}: {value!r}"
        ) from None


def _star_name(ra_deg: float, dec_deg: float) -> str:
    """Synthesize a stable label for an APASS row (the table has no name column)."""
    return f"APASS J{ra_deg:.5f}{dec_deg:+09.5f}"


def _tap_row_to_star(
    row: Any, columns: Mapping[str, int], *, band: str, target: J2000Coord
) -> StandardStar:
    """Parse one TAP JSON data row into a :class:`StandardStar`.

    The single place where wire values become a J2000 coord and a distance —
    the distance is computed with :func:`~.coords.angular_distance`, never
    reimplemented.
    """
    if not isinstance(row, (list, tuple)):
        raise StandardsResponseError(f"VizieR data row is not a list: {row!r}")

    def cell(name: str) -> Any:
        pos = columns.get(name)
        if pos is None or pos >= len(row):
            return None
        return row[pos]

    mag_column = BAND_COLUMNS[band]
    ra = _optional_float(cell("RAJ2000"), field="RAJ2000")
    dec = _optional_float(cell("DEJ2000"), field="DEJ2000")
    if ra is None or dec is None:
        raise StandardsResponseError(f"VizieR row has no coordinates: {row!r}")
    # Wire data must already be in range (no wrapping of catalog values); the
    # shared J2000Coord is the single place raw values become a normalized coord.
    if not 0.0 <= ra < 360.0:
        raise StandardsResponseError(f"VizieR row has RAJ2000 outside [0, 360) deg: {ra}")
    if not -90.0 <= dec <= 90.0:
        raise StandardsResponseError(f"VizieR row has DEJ2000 outside [-90, 90] deg: {dec}")
    coord = J2000Coord.from_degrees(ra, dec)
    return StandardStar(
        name=_star_name(ra, dec),
        coord=coord,
        magnitude=_optional_float(cell(mag_column), field=f"{mag_column} (magnitude)"),
        band=band,
        distance_deg=angular_distance(target, coord),
    )


def parse_cone_response(
    payload: Any,
    *,
    target: Any,
    radius_deg: float,
    band: str = DEFAULT_BAND,
) -> list[StandardStar]:
    """Parse a VizieR TAP JSON payload into radius-filtered standard stars.

    Each row becomes a :class:`StandardStar` with ``distance_deg`` computed
    from ``target`` via :func:`~.coords.angular_distance`; rows farther than
    ``radius_deg`` are dropped (the client-side cone filter — the server-side
    ``CIRCLE`` is the first pass, this is the authoritative one). The result
    is sorted brightest-first, unknown magnitudes last. An empty ``data``
    list parses to ``[]`` — a normal outcome, not an error. Malformed
    payloads raise :class:`StandardsResponseError`.
    """
    if not isinstance(payload, dict):
        raise StandardsResponseError(
            f"VizieR response is not a JSON object but {type(payload).__name__}"
        )
    checked_band = _validate_band(band)
    mag_column = BAND_COLUMNS[checked_band]
    columns = _column_index(payload.get("metadata"), mag_column=mag_column)
    data = payload.get("data")
    if data is None:
        raise StandardsResponseError("VizieR response has no 'data' rows")
    if not isinstance(data, list):
        raise StandardsResponseError(
            f"VizieR 'data' is not a list but {type(data).__name__}"
        )
    center = _as_coord(target)
    radius = _validate_radius(radius_deg)
    stars: list[StandardStar] = []
    for row in data:
        star = _tap_row_to_star(row, columns, band=checked_band, target=center)
        if star.distance_deg <= radius:  # the authoritative client-side cone filter
            stars.append(star)
    stars.sort(
        key=lambda s: (s.magnitude is None, s.magnitude if s.magnitude is not None else 0.0)
    )
    return stars


# --- filtering / sorting -------------------------------------------------------------


def _apply_mag_range(
    stars: list[StandardStar], mag_range: tuple[float, float] | None
) -> list[StandardStar]:
    """Keep only stars whose magnitude lies in ``mag_range`` (unknown mag drops out)."""
    if mag_range is None:
        return stars
    lo, hi = mag_range
    return [
        s for s in stars if s.magnitude is not None and lo <= s.magnitude <= hi
    ]


def _sort_stars(
    stars: list[StandardStar], *, target_mag: float | None
) -> list[StandardStar]:
    """Sort by magnitude closeness to ``target_mag``, else brightest-first.

    Stars with unknown magnitude always sort last.
    """
    if target_mag is None:
        stars.sort(
            key=lambda s: (
                s.magnitude is None,
                s.magnitude if s.magnitude is not None else 0.0,
            )
        )
    else:
        stars.sort(
            key=lambda s: (
                s.magnitude is None,
                abs(s.magnitude - target_mag) if s.magnitude is not None else 0.0,
            )
        )
    return stars


def _finish(
    stars: list[StandardStar],
    *,
    target_mag: float | None,
    mag_range: tuple[float, float] | None,
) -> list[StandardStar]:
    """Apply the per-call magnitude range filter and sort (shared by both lookups)."""
    return _sort_stars(_apply_mag_range(stars, mag_range), target_mag=target_mag)


# --- the public lookups ---------------------------------------------------------------


def find_standard_stars(
    coord: Any,
    radius_deg: float,
    *,
    target_mag: float | None = None,
    mag_range: tuple[float, float] | None = None,
    band: str = DEFAULT_BAND,
    maxrec: int = DEFAULT_MAX_STANDARDS,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> list[StandardStar]:
    """Find known standard/comparison stars within ``radius_deg`` of ``coord``.

    ``coord`` is the target position — a :class:`~.coords.J2000Coord`, a
    ``(ra_deg, dec_deg)`` pair, or any object with ``ra_deg``/``dec_deg``
    (e.g. a SIMBAD candidate). The APASS DR9 cone search is queried via
    :func:`fetch_standards_payload` (CDS TAP first, then the asu-tsv mirrors);
    rows are parsed to :class:`StandardStar` with ``distance_deg`` computed
    by :func:`~.coords.angular_distance`, filtered to
    ``distance_deg <= radius_deg``, then:

    - ``mag_range`` (optional ``(lo, hi)``) keeps only stars with a known
      magnitude inside the range;
    - sorted by **magnitude closeness to ``target_mag``** when given, else
      **brightest-first**; unknown magnitudes sort last.

    An empty result (no standards nearby) is a normal outcome: ``[]`` is
    returned, nothing raises.

    Raises:
        ValueError: invalid ``coord`` / ``radius_deg`` / ``maxrec`` / ``band``
            / ``target_mag`` / ``mag_range`` (nothing is sent).
        StandardsUnavailableError: network/HTTP failure reaching VizieR.
        StandardsResponseError: malformed VizieR response.
    """
    target, radius, checked_band, checked_mag, checked_range = _validate_args(
        coord, radius_deg, maxrec=maxrec, band=band, target_mag=target_mag, mag_range=mag_range
    )
    payload = fetch_standards_payload(
        target, radius, band=checked_band, maxrec=maxrec, timeout=timeout
    )
    stars = parse_cone_response(payload, target=target, radius_deg=radius, band=checked_band)
    return _finish(stars, target_mag=checked_mag, mag_range=checked_range)


def find_standard_stars_cached(
    coord: Any,
    radius_deg: float,
    cache: QueryCache | None = None,
    *,
    target_mag: float | None = None,
    mag_range: tuple[float, float] | None = None,
    band: str = DEFAULT_BAND,
    maxrec: int = DEFAULT_MAX_STANDARDS,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> list[StandardStar]:
    """Cached variant of :func:`find_standard_stars` — same result, same errors.

    Goes through ``cache`` (a :class:`~.cache.QueryCache`; default: one in the
    app config dir with the default TTL) via :meth:`~.cache.QueryCache.cached`
    (ticket 03 nit F4: every catalog query goes through the wrapper): a fresh
    cache entry is served from disk with no network access, so repeat lookups
    are instant and work offline. The raw :func:`find_standard_stars` stays
    uncached.

    Only validated results are cached (the TAP response is parsed and
    radius-filtered before it is stored, and a cached payload is shape-checked
    on read), so a malformed response or a corrupted cache file is refetched,
    never served. An empty result (no standards nearby) is cached too —
    offline, a known-empty cone still returns ``[]`` without network.
    ``target_mag``/``mag_range`` are not part of the cache key: they only
    re-filter/re-sort the cached rows.

    Raises the same errors as :func:`find_standard_stars`.
    """
    if cache is None:
        cache = QueryCache()
    target, radius, checked_band, checked_mag, checked_range = _validate_args(
        coord, radius_deg, maxrec=maxrec, band=band, target_mag=target_mag, mag_range=mag_range
    )
    adql = build_cone_query(target, radius, band=checked_band)
    query = _standards_query(adql, maxrec=maxrec)

    def fetch() -> list[dict[str, Any]]:
        payload = fetch_standards_payload(
            target, radius, band=checked_band, maxrec=maxrec, timeout=timeout
        )
        # Parse BEFORE caching: a malformed response is never stored.
        stars = parse_cone_response(
            payload, target=target, radius_deg=radius, band=checked_band
        )
        return [_star_to_row(s) for s in stars]

    rows = cache.cached(query, fetch, validate=_is_standard_rows)
    stars = [_row_to_star(row) for row in rows]
    return _finish(stars, target_mag=checked_mag, mag_range=checked_range)
