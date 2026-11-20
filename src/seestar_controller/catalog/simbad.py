"""SIMBAD name resolution: object names -> J2000 coordinates.

Pure-Python catalog client (module boundary per ``.scratch/catalog/spec.md``):
no GTK imports, no :mod:`seestar_controller.link` imports, stdlib only.

The HTTP layer is a single function, :func:`fetch_tap_json`, which POSTs one
ADQL query to SIMBAD's public TAP ``sync`` endpoints — :data:`SIMBAD_TAP_SYNC_URLS`,
CDS Strasbourg first, then the official mirrors — with :mod:`urllib.request`
and returns the parsed JSON body. Tests mock exactly that one function (or
``urllib.request.urlopen`` for the fetch itself), so no test ever touches the
network.

Coordinates are **always J2000** — SIMBAD serves ICRS, which coincides with
J2000 at the precision we need — and always in **degrees**. All parsing of
wire values into :class:`CatalogCandidate` happens in one place,
:func:`_row_to_candidate`.

ENDPOINTS (verified live 2026-11-20, see docs/wiki/progress.md): **CDS
Strasbourg is down** — a cybersecurity incident since ~2026-10-29; the
canonical ``simbad.cds.unistra.fr`` answers 404 "Page unavailable" or times
out, with no ETA for recovery. The official mirrors listed on
https://cds.unistra.fr/ work and serve the identical TAP JSON shape
(``{"metadata": ..., "data": ...}``), so :func:`fetch_tap_json` tries
:data:`SIMBAD_TAP_SYNC_URLS` in order and the first success wins. Mirror ADQL
dialect notes (probed live): the mirrors require the ``FORMAT=json`` form
field (``RESPONSEFORMAT`` alone yields VOTable XML there — both are sent),
reject ``LOWER(...)`` and qualified ``ORDER BY b.main_id``, and match
``i.id = '<name>'`` case- and space-insensitively, so the lookup query uses
plain ``=`` and an unqualified ``ORDER BY main_id`` (see
:func:`build_lookup_query`).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping

from .coords import J2000Coord

#: SIMBAD's public TAP synchronous endpoint at CDS Strasbourg (canonical;
#: down since the 2026-10-29 security incident — see module docstring).
SIMBAD_TAP_SYNC_URL = "https://simbad.cds.unistra.fr/simbad/sim-tap/sync"

#: Official SIMBAD TAP mirror at the Center for Astrophysics | Harvard & Smithsonian.
SIMBAD_TAP_SYNC_URL_CFA = "https://simbad.cfa.harvard.edu/simbad/sim-tap/sync"

#: Official SIMBAD TAP mirror at IDIA (South Africa).
SIMBAD_TAP_SYNC_URL_IDIA = "https://simbad.idia.ac.za/simbad/sim-tap/sync"

#: SIMBAD TAP ``sync`` endpoints tried in order: CDS Strasbourg first, then
#: the official mirrors (verified live 2026-11-20 during the CDS outage).
SIMBAD_TAP_SYNC_URLS = (
    SIMBAD_TAP_SYNC_URL,
    SIMBAD_TAP_SYNC_URL_CFA,
    SIMBAD_TAP_SYNC_URL_IDIA,
)

#: Default HTTP timeout for one SIMBAD query, in seconds.
DEFAULT_TIMEOUT_S = 15.0

#: Default cap on returned rows (TAP ``MAXREC``).
DEFAULT_MAX_CANDIDATES = 50

#: Default photometric filter whose flux is reported as the magnitude.
DEFAULT_MAG_FILTER = "V"

#: User-Agent sent to SIMBAD (polite-identification convention).
USER_AGENT = "seestar-controller (catalog name resolution)"

#: Columns without which a response cannot be parsed at all.
_REQUIRED_COLUMNS = ("main_id", "ra", "dec")


class CatalogError(Exception):
    """Base class for catalog lookup failures."""


class NameNotFoundError(CatalogError):
    """SIMBAD has no object matching the queried name."""


class SimbadResponseError(CatalogError):
    """SIMBAD returned a malformed or unusable response."""


class SimbadUnavailableError(CatalogError):
    """SIMBAD could not be reached (network or HTTP failure)."""


@dataclass(frozen=True)
class CatalogCandidate:
    """One SIMBAD match for a queried name.

    Coordinates are J2000 in degrees: ``ra_deg`` in [0, 360), ``dec_deg`` in
    [-90, 90]. ``magnitude`` is the flux in the chosen filter (``None`` when
    SIMBAD has none), ``otype`` the SIMBAD object type (``None`` when absent).
    """

    name: str
    ra_deg: float
    dec_deg: float
    magnitude: float | None = None
    otype: str | None = None
    matched_id: str | None = None

    @property
    def ra_hours(self) -> float:
        """Right ascension in hours, [0, 24) — the unit ``link.Device.goto`` takes."""
        return self.coord.ra_hours

    @property
    def coord(self) -> J2000Coord:
        """The J2000 position as the shared :class:`~.coords.J2000Coord` type."""
        return J2000Coord(ra_deg=self.ra_deg, dec_deg=self.dec_deg)

    @property
    def coords_j2000(self) -> tuple[float, float]:
        """``(ra_deg, dec_deg)`` — J2000 coordinates in degrees."""
        return self.coord.as_degrees()


def _post_tap_form(
    url: str, query: str, *, maxrec: int, timeout: float
) -> dict[str, Any]:
    """POST one ADQL ``query`` to one TAP sync ``url``; return the parsed JSON.

    The per-endpoint primitive of :func:`fetch_tap_json`. Both ``FORMAT=json``
    (honored by the mirrors) and ``RESPONSEFORMAT=application/json`` (the TAP
    standard field honored by CDS Strasbourg) are sent — verified live
    2026-11-20 that the mirrors need ``FORMAT`` to answer JSON at all.

    Raises :class:`SimbadUnavailableError` on network/HTTP failure and
    :class:`SimbadResponseError` when the body is not a JSON object.
    """
    form = urllib.parse.urlencode(
        {
            "REQUEST": "doQuery",
            "LANG": "ADQL",
            "QUERY": query,
            "MAXREC": str(int(maxrec)),
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
        raise SimbadUnavailableError(
            f"SIMBAD returned HTTP {exc.code} ({exc.reason}) for the query"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SimbadUnavailableError(f"could not reach SIMBAD at {url}: {exc}") from exc
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SimbadResponseError(f"SIMBAD response is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise SimbadResponseError(
            f"SIMBAD response is not a JSON object but {type(payload).__name__}"
        )
    return payload


def fetch_tap_json(
    query: str,
    *,
    maxrec: int = DEFAULT_MAX_CANDIDATES,
    timeout: float = DEFAULT_TIMEOUT_S,
    url: str | None = None,
) -> dict[str, Any]:
    """POST one ADQL ``query`` to SIMBAD's TAP sync endpoints; return parsed JSON.

    This is the single HTTP boundary of the catalog module — tests monkeypatch
    this function (or :func:`urllib.request.urlopen`) instead of the network.

    Endpoints are tried in order — :data:`SIMBAD_TAP_SYNC_URLS` (CDS
    Strasbourg, then the official Harvard and IDIA mirrors) unless ``url``
    pins a single one — each with its own ``timeout``; the first success wins.
    :class:`SimbadUnavailableError` is raised only when **every** endpoint
    fails (network/HTTP failure); a malformed body from an endpoint that did
    answer raises :class:`SimbadResponseError` immediately.
    """
    endpoints = (url,) if url is not None else SIMBAD_TAP_SYNC_URLS
    failures: list[str] = []
    for endpoint in endpoints:
        try:
            return _post_tap_form(endpoint, query, maxrec=maxrec, timeout=timeout)
        except SimbadUnavailableError as exc:
            failures.append(f"{endpoint}: {exc}")
    raise SimbadUnavailableError(
        "could not reach SIMBAD at any endpoint ("
        + ", ".join(endpoints)
        + "): "
        + "; ".join(failures)
    )


def _adql_quote(value: str) -> str:
    """Escape a string literal for ADQL (single quotes are doubled)."""
    return value.replace("'", "''")


def _wildcard_patterns(name: str) -> list[str]:
    """ADQL ``LIKE`` patterns for a wildcard name: as-typed plus spacing variants.

    ``*`` becomes ``%``. SIMBAD stores identifiers with spaces between the
    letter and digit runs (``NGC  7789`` with two spaces, ``M 42`` with one),
    and ``LIKE`` on the mirrors is literal (case/space sensitive), so for
    wildcard names we also generate the variants with a single and a double
    space inserted at every letter/digit boundary. Best-effort: the exact
    match is the primary path.
    """
    base = name.replace("*", "%")
    variants = [base]
    boundaries = [
        i
        for i in range(1, len(base))
        if (base[i - 1].isalpha() and base[i].isdigit())
        or (base[i - 1].isdigit() and base[i].isalpha())
    ]
    if boundaries:
        single = list(base)
        double = list(base)
        for i in reversed(boundaries):
            single.insert(i, " ")
            double.insert(i, "  ")
        variants.append("".join(single))
        variants.append("".join(double))
    unique: list[str] = []
    seen: set[str] = set()
    for variant in variants:
        if variant not in seen:
            seen.add(variant)
            unique.append(variant)
    return unique


def build_lookup_query(name: str, *, mag_filter: str = DEFAULT_MAG_FILTER) -> str:
    """Build the ADQL name-lookup query — the one place the wire query lives.

    Exact identifier match by default (``i.id = '<name>'`` — case- and
    space-insensitive on the mirrors, verified live 2026-11-20); a name
    containing ``*`` or ``%`` is treated as a wildcard matched with ``LIKE``
    over the spacing variants from :func:`_wildcard_patterns` (``%`` in ADQL
    ``LIKE``). The query deliberately avoids ``LOWER(...)`` and qualified
    ``ORDER BY`` columns — the mirror ADQL dialect rejects both (HTTP 400);
    it uses the unqualified ``ORDER BY main_id`` instead.
    """
    ident = _adql_quote(name)
    if "*" in name or "%" in name:
        patterns = [_adql_quote(p) for p in _wildcard_patterns(name)]
        predicate = "(" + " OR ".join(f"i.id LIKE '{p}'" for p in patterns) + ")"
    else:
        predicate = f"i.id = '{ident}'"
    return (
        "SELECT b.oid, b.main_id, b.ra, b.dec, b.otype, i.id, f.filter, f.flux "
        "FROM ident i "
        "JOIN basic b ON b.oid = i.oidref "
        f"LEFT JOIN flux f ON f.oidref = b.oid AND f.filter = '{_adql_quote(mag_filter)}' "
        f"WHERE {predicate} "
        "ORDER BY main_id"
    )


def _column_index(metadata: Any) -> dict[str, int]:
    """Map TAP JSON metadata column names to positions in each data row."""
    if not isinstance(metadata, list) or not metadata:
        raise SimbadResponseError("SIMBAD response has no 'metadata' column list")
    index: dict[str, int] = {}
    for pos, column in enumerate(metadata):
        if isinstance(column, dict) and isinstance(column.get("name"), str):
            index[column["name"]] = pos
    missing = [c for c in _REQUIRED_COLUMNS if c not in index]
    if missing:
        raise SimbadResponseError(
            f"SIMBAD response metadata is missing column(s) {missing}"
        )
    return index


def _optional_float(value: Any, *, field: str) -> float | None:
    """Coerce one wire value to float; ``None``/empty -> ``None``."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise SimbadResponseError(
            f"SIMBAD row has a non-numeric {field}: {value!r}"
        ) from None


def _row_to_candidate(row: Any, columns: Mapping[str, int]) -> CatalogCandidate:
    """Parse one TAP JSON data row into a candidate.

    The single place where wire values become J2000 degrees.
    """
    if not isinstance(row, (list, tuple)):
        raise SimbadResponseError(f"SIMBAD data row is not a list: {row!r}")

    def cell(name: str) -> Any:
        pos = columns.get(name)
        if pos is None or pos >= len(row):
            return None
        return row[pos]

    ra = _optional_float(cell("ra"), field="ra")
    dec = _optional_float(cell("dec"), field="dec")
    if ra is None or dec is None:
        raise SimbadResponseError(f"SIMBAD row has no coordinates: {row!r}")
    # Wire data must already be in range (no wrapping of catalog values); the
    # shared J2000Coord is the single place raw values become a normalized coord.
    if not 0.0 <= ra < 360.0:
        raise SimbadResponseError(f"SIMBAD row has ra outside [0, 360) deg: {ra}")
    if not -90.0 <= dec <= 90.0:
        raise SimbadResponseError(f"SIMBAD row has dec outside [-90, 90] deg: {dec}")
    coord = J2000Coord.from_degrees(ra, dec)
    name = cell("main_id") or cell("id")
    if not isinstance(name, str) or not name:
        raise SimbadResponseError(f"SIMBAD row has no main_id: {row!r}")
    otype = cell("otype")
    matched = cell("id")
    return CatalogCandidate(
        name=name,
        ra_deg=coord.ra_deg,
        dec_deg=coord.dec_deg,
        magnitude=_optional_float(cell("flux"), field="flux (magnitude)"),
        otype=otype if isinstance(otype, str) and otype else None,
        matched_id=matched if isinstance(matched, str) and matched else None,
    )


def parse_tap_response(payload: Any) -> list[CatalogCandidate]:
    """Parse a SIMBAD TAP JSON payload into candidates, deduped by ``oid``.

    An empty ``data`` list parses to an empty list (the caller turns that into
    :class:`NameNotFoundError`). Malformed payloads raise
    :class:`SimbadResponseError`. Candidates are sorted brightest-first, with
    unknown magnitudes last.
    """
    if not isinstance(payload, dict):
        raise SimbadResponseError(
            f"SIMBAD response is not a JSON object but {type(payload).__name__}"
        )
    columns = _column_index(payload.get("metadata"))
    data = payload.get("data")
    if data is None:
        raise SimbadResponseError("SIMBAD response has no 'data' rows")
    if not isinstance(data, list):
        raise SimbadResponseError(
            f"SIMBAD 'data' is not a list but {type(data).__name__}"
        )
    candidates: list[CatalogCandidate] = []
    seen_oids: set[Any] = set()
    oid_pos = columns.get("oid")
    for row in data:
        candidate = _row_to_candidate(row, columns)
        if oid_pos is not None and isinstance(row, (list, tuple)) and oid_pos < len(row):
            oid = row[oid_pos]
            if oid is not None:
                if oid in seen_oids:
                    continue
                seen_oids.add(oid)
        candidates.append(candidate)
    candidates.sort(
        key=lambda c: (c.magnitude is None, c.magnitude if c.magnitude is not None else 0.0)
    )
    return candidates


def resolve_name(
    name: str,
    *,
    mag_filter: str = DEFAULT_MAG_FILTER,
    maxrec: int = DEFAULT_MAX_CANDIDATES,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> list[CatalogCandidate]:
    """Resolve an object ``name`` to SIMBAD candidates with J2000 coordinates.

    Returns a list of :class:`CatalogCandidate` — possibly several, because
    SIMBAD can match one name to multiple objects; the caller chooses, we
    never silently pick the first. Sorted brightest-first (unknown magnitude
    last).

    Raises:
        ValueError: ``name`` is empty/blank or ``maxrec`` < 1.
        NameNotFoundError: SIMBAD has no match — the message names the query
            and suggests refinements (spelling, SIMBAD identifier forms like
            ``M 42`` / ``NGC 1976`` / ``V* bet Per``, or a ``*`` wildcard).
        SimbadUnavailableError: network/HTTP failure reaching SIMBAD.
        SimbadResponseError: malformed SIMBAD response.
    """
    if not isinstance(name, str):
        raise ValueError(f"name must be a string, got {name!r}")
    query_name = name.strip()
    if not query_name:
        raise ValueError("name must not be empty")
    if maxrec < 1:
        raise ValueError(f"maxrec must be >= 1, got {maxrec}")
    query = build_lookup_query(query_name, mag_filter=mag_filter)
    payload = fetch_tap_json(query, maxrec=maxrec, timeout=timeout)
    candidates = parse_tap_response(payload)
    if not candidates:
        raise NameNotFoundError(
            f"SIMBAD has no object named {query_name!r} — check the spelling; "
            "SIMBAD identifiers look like 'M 42', 'NGC 1976' or 'V* bet Per', "
            f"or append '*' for a wildcard match (e.g. '{query_name}*')"
        )
    return candidates
