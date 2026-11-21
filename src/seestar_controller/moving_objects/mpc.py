"""MPC lookup for flagged movers — HTTP boundary + disk cache + pure matching.

Pure-Python core of moving-objects issue 02 (module boundary per
``.scratch/moving-objects/spec.md``): no GTK imports, no
:mod:`seestar_controller.link` imports — a subprocess test enforces the
boundary. The only network access is stdlib :mod:`urllib` inside the single
HTTP boundary :func:`fetch_mpc_checker_json` — tests monkeypatch exactly
that function, so no test ever touches the network.

What lives here:

- :class:`MpcCandidate` — one known object returned by the MPC Checker:
  designation, optional name, ephemeris position (ra/dec), sky-plane rate
  (arcsec/min) and position angle (deg) at the query time, optional V mag;
- :class:`MpcMatch` — the match result for one mover (designation/name +
  the observed-vs-candidate offsets); persisted in the ``movers.json``
  sidecar as ``mpc_match`` on the mover entry (schema_version 2);
- :func:`mover_track_params` — the sky track of a
  :class:`~.flag.MoverReport` (mid-time position, rate in arcsec/min,
  position angle) — raises :class:`~.errors.MpcLookupError` when the mover
  has no sky coordinates (subs without a WCS);
- :func:`build_checker_params` — the MPC Checker form fields for one track;
- :func:`fetch_mpc_checker_json` — POST the form to the MPC Checker
  (:data:`MPC_CHECKER_URL`) and return the parsed JSON body — **the single
  HTTP boundary**;
- :func:`parse_mpc_response` — parse the checker JSON into
  :class:`MpcCandidate` rows (the one place wire values become floats);
- :func:`match_mover` — the PURE matcher: best candidate within the
  offset / rate / position-angle tolerances, else ``None``;
- :func:`lookup_mover` — the raw, uncached lookup (params -> fetch -> parse
  -> match);
- :func:`lookup_mover_cached` — the cached variant, following the catalog
  module's HTTP+disk-cache pattern (the gaia module's
  :func:`~seestar_controller.gaia.query.query_gaia_cached` shape): the
  candidate rows are stored under ``<config>/cache/mpc/`` keyed by the
  **rounded** track params (+ obscode) with a TTL, reusing
  :class:`~seestar_controller.catalog.cache.QueryCache` (read-only reuse).
  A fresh entry is served with no HTTP at all; on a fetch failure a valid
  — even expired — entry is served as a stale fallback (MPC data is updated
  daily, so stale beats nothing); with no cache entry the failure becomes
  :class:`~.errors.MpcUnavailableError`;
- :func:`lookup_movers` — one lookup per mover (movers without sky
  coordinates get ``None`` — the MPC cannot match a pixel-only track);
- :func:`with_matches` — attach the matches to the mover reports
  (``dataclasses.replace``);
- :func:`lookup_and_persist` — the demo path: load a session's tracks,
  look every mover up (cached), and save the matches back into the
  ``movers.json`` sidecar (schema_version 2 — the store schema is NOT
  touched).

ENDPOINT: the MPC Checker web service
(``https://www.minorplanetcenter.net/cgi-bin/checkmp.cgi``) — given a sky
position, a time, a rate and a position angle, it returns the known minor
planets whose ephemeris matches (the same service the MPC web "MPChecker"
form uses; its data is updated daily, hence the 7-day cache TTL).
"""

from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from seestar_controller.catalog.cache import DEFAULT_CONFIG_DIR, QueryCache
from seestar_controller.catalog.coords import J2000Coord, angular_distance

from .errors import MpcLookupError, MpcResponseError, MpcUnavailableError
from .flag import MoverReport

if TYPE_CHECKING:
    from .persist import SessionTracks

#: The MPC Checker web service (POST form -> machine-readable JSON response).
MPC_CHECKER_URL = "https://www.minorplanetcenter.net/cgi-bin/checkmp.cgi"

#: Default HTTP timeout for one MPC query, in seconds.
DEFAULT_TIMEOUT_S = 30.0

#: Default time-to-live of an MPC cache entry, in seconds (7 days — the MPC
#: Checker data is updated daily, the TTL mainly guards partial responses).
DEFAULT_TTL_S = 7 * 24 * 60 * 60.0

#: Decimals of a degree the cache key rounds the track position to
#: (~0.4 arcsec, like the gaia cone-search key).
KEY_ROUND_DIGITS = 4

#: Decimals the cache key rounds the rate (arcsec/min) to.
KEY_ROUND_MOTION_DIGITS = 2

#: Decimals the cache key rounds the position angle (deg) to.
KEY_ROUND_PA_DIGITS = 1

#: Decimals the cache key rounds the mid-time JD to (~0.4 s).
KEY_ROUND_JD_DIGITS = 5

#: Default cache location: ``<config dir>/cache/mpc/`` (the config dir is
#: the same as the catalog cache's, re-declared via the catalog module).
DEFAULT_CACHE_DIR = DEFAULT_CONFIG_DIR / "cache" / "mpc"

#: Environment variable that overrides the default MPC cache dir.
CACHE_DIR_ENV_VAR = "SEESTAR_MPC_CACHE_DIR"

#: User-Agent sent to the MPC (polite-identification convention).
USER_AGENT = "seestar-controller (MPC Checker mover lookup)"

#: Default observatory code for the checker query: ``500`` = geocentric
#: (the subs' WCS gives topocentric positions, but at mover speeds the
#: parallax correction is far below the match tolerance).
DEFAULT_OBSCODE = "500"

#: Default maximum sky offset between the observed mid-track position and
#: the candidate ephemeris, in arcseconds.
DEFAULT_MAX_OFFSET_ARCSEC = 30.0

#: Default maximum RELATIVE rate difference (``|cand - obs| / obs``).
DEFAULT_MAX_MOTION_REL_DIFF = 0.25

#: Default maximum position-angle difference, in degrees.
DEFAULT_MAX_PA_DIFF_DEG = 15.0

#: Arcseconds per degree.
ARCSEC_PER_DEG = 3600.0

#: Minutes per day (JD difference -> minutes).
MIN_PER_DAY = 24.0 * 60.0


@dataclass(frozen=True)
class MpcCandidate:
    """One known object returned by the MPC Checker at the query time.

    - ``designation`` — the provisional/numbered designation (``"(1234)"``
      or ``"1234"`` style, as returned);
    - ``name`` — the object's name when the MPC knows one, else ``None``;
    - ``ra_deg`` / ``dec_deg`` — the ephemeris position at the query time;
    - ``motion_arcsec_per_min`` — the sky-plane rate;
    - ``pa_deg`` — the position angle of motion (deg, east of north);
    - ``v_mag`` — the predicted V magnitude, ``None`` when unknown.
    """

    designation: str
    name: str | None
    ra_deg: float
    dec_deg: float
    motion_arcsec_per_min: float
    pa_deg: float
    v_mag: float | None = None


@dataclass(frozen=True)
class MpcMatch:
    """The MPC match result for one mover — persisted as ``mpc_match`` in
    the ``movers.json`` sidecar (schema_version 2).

    - ``designation`` / ``name`` — the matched known object;
    - ``offset_arcsec`` — the sky distance between the observed mid-track
      position and the candidate ephemeris at the mid-time;
    - ``motion_arcsec_per_min`` / ``pa_deg`` — the candidate's rate and
      position angle at the query time (for display alongside the observed
      ones);
    - ``v_mag`` — the predicted V magnitude, ``None`` when unknown.
    """

    designation: str
    name: str | None
    offset_arcsec: float
    motion_arcsec_per_min: float
    pa_deg: float
    v_mag: float | None = None


# --- cache dir + cache key ---------------------------------------------------------


def default_cache_dir(env: Mapping[str, str] | None = None) -> Path:
    """Resolve the default MPC cache dir.

    Precedence: the ``SEESTAR_MPC_CACHE_DIR`` environment variable > the
    default ``~/.config/seestar-controller/cache/mpc/``.
    """
    environ = os.environ if env is None else env
    value = environ.get(CACHE_DIR_ENV_VAR)
    if value:
        return Path(value)
    return DEFAULT_CACHE_DIR


def mpc_cache_key(
    mid_jd: float,
    ra_deg: float,
    dec_deg: float,
    motion_arcsec_per_min: float,
    pa_deg: float,
    *,
    obscode: str = DEFAULT_OBSCODE,
    digits: int = KEY_ROUND_DIGITS,
) -> str:
    """The canonical cache query string for one MPC track lookup.

    The track parameters are **rounded** before the key is built (position
    to ``digits`` decimals of a degree, rate to :data:`KEY_ROUND_MOTION_DIGITS`,
    PA to :data:`KEY_ROUND_PA_DIGITS`, mid-time to :data:`KEY_ROUND_JD_DIGITS`),
    so two detections of the same mover share one cache entry; the obscode
    is part of the key because it changes the result.
    """
    return json.dumps(
        {
            "jd": round(float(mid_jd), KEY_ROUND_JD_DIGITS),
            "ra": round(float(ra_deg) % 360.0, digits),
            "dec": round(float(dec_deg), digits),
            "motion": round(float(motion_arcsec_per_min), KEY_ROUND_MOTION_DIGITS),
            "pa": round(float(pa_deg) % 360.0, KEY_ROUND_PA_DIGITS),
            "obscode": str(obscode),
        },
        sort_keys=True,
    )


# --- the mover's sky track -----------------------------------------------------------


def _jd_to_utc_datetime(jd: float) -> Any:
    """A Julian Date as an aware UTC :class:`datetime.datetime` (lazy astropy)."""
    from astropy.time import Time

    return Time(float(jd), format="jd", scale="utc").utc.to_datetime()


def _wrap_180(delta: float) -> float:
    """Wrap an angle difference to [-180, 180) degrees."""
    return (float(delta) + 180.0) % 360.0 - 180.0


def mover_track_params(mover: MoverReport) -> dict[str, float]:
    """The sky track of a :class:`~.flag.MoverReport` as a dict with keys
    ``mid_jd``, ``ra_deg``, ``dec_deg``, ``motion_arcsec_per_min``,
    ``pa_deg``.

    - the position is the mid-track position (the average of the start/end
      sky positions, RA-wrap aware);
    - the rate is the great-circle distance start->end divided by the track
      duration, in arcsec/min;
    - the position angle is the sky-plane direction of motion
      (``atan2(dRA * cos(dec), dDec)``, deg east of north).

    Raises :class:`~.errors.MpcLookupError` when the mover carries no sky
    coordinates (the subs had no WCS) — the MPC matches on the sky track,
    so a pixel-only mover cannot be queried.
    """
    if not isinstance(mover, MoverReport):
        raise MpcLookupError(
            f"mover must be a MoverReport, got {type(mover).__name__}"
        )
    if (
        mover.start_ra_deg is None
        or mover.start_dec_deg is None
        or mover.end_ra_deg is None
        or mover.end_dec_deg is None
    ):
        raise MpcLookupError(
            f"mover track {mover.track_id} has no sky coordinates (subs without "
            "a WCS) — the MPC lookup needs the sky track"
        )
    start = J2000Coord(ra_deg=mover.start_ra_deg, dec_deg=mover.start_dec_deg)
    end = J2000Coord(ra_deg=mover.end_ra_deg, dec_deg=mover.end_dec_deg)
    mid_jd = (mover.start_jd + mover.end_jd) / 2.0
    duration_min = (mover.end_jd - mover.start_jd) * MIN_PER_DAY
    if duration_min <= 0.0:
        raise MpcLookupError(
            f"mover track {mover.track_id} has a non-positive duration "
            f"({duration_min} min)"
        )
    motion_arcsec_per_min = angular_distance(start, end) * ARCSEC_PER_DEG / duration_min
    dec_mid = (start.dec_deg + end.dec_deg) / 2.0
    d_ra = _wrap_180(end.ra_deg - start.ra_deg)
    d_dec = end.dec_deg - start.dec_deg
    pa_deg = math.degrees(math.atan2(d_ra * math.cos(math.radians(dec_mid)), d_dec)) % 360.0
    ra_mid = (start.ra_deg + _wrap_180(end.ra_deg - start.ra_deg) / 2.0) % 360.0
    return {
        "mid_jd": mid_jd,
        "ra_deg": ra_mid,
        "dec_deg": dec_mid,
        "motion_arcsec_per_min": motion_arcsec_per_min,
        "pa_deg": pa_deg,
    }


def build_checker_params(
    mover: MoverReport,
    *,
    obscode: str = DEFAULT_OBSCODE,
) -> dict[str, str]:
    """The MPC Checker form fields for one mover's sky track.

    The checker is queried at the track's mid-time with the mid position,
    the sky-plane rate (arcsec/min) and the position angle (deg) — the
    fields the MPC "MPChecker" form takes. Raises
    :class:`~.errors.MpcLookupError` for a mover without sky coordinates.
    """
    if not isinstance(obscode, str) or not obscode.strip():
        raise MpcLookupError(f"obscode must be a non-empty string, got {obscode!r}")
    track = mover_track_params(mover)
    moment = _jd_to_utc_datetime(track["mid_jd"])
    return {
        "ra": f"{track['ra_deg']:.6f}",
        "dec": f"{track['dec_deg']:.6f}",
        "motion": f"{track['motion_arcsec_per_min']:.3f}",
        "pa": f"{track['pa_deg']:.2f}",
        "year": str(moment.year),
        "month": str(moment.month),
        "day": str(moment.day),
        "hour": str(moment.hour),
        "minute": str(moment.minute),
        "second": f"{moment.second + moment.microsecond / 1e6:.2f}",
        "obscode": obscode.strip(),
        "format": "json",
    }


# --- the single HTTP boundary -----------------------------------------------------------


def fetch_mpc_checker_json(
    params: Mapping[str, str],
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
    url: str = MPC_CHECKER_URL,
) -> dict[str, Any]:
    """POST one MPC Checker form to :data:`MPC_CHECKER_URL`; return parsed JSON.

    This is the single HTTP boundary of the MPC lookup — tests monkeypatch
    this function (or :func:`urllib.request.urlopen`) instead of the network.

    Raises :class:`~.errors.MpcUnavailableError` on network/HTTP failure and
    :class:`~.errors.MpcResponseError` when the body is not a JSON object.
    """
    if not isinstance(params, Mapping):
        raise MpcLookupError(f"params must be a mapping, got {params!r}")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise MpcLookupError(f"timeout must be a positive number of seconds, got {timeout!r}")
    form = urllib.parse.urlencode({k: str(v) for k, v in params.items()}).encode("ascii")
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
        with urllib.request.urlopen(request, timeout=float(timeout)) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise MpcUnavailableError(
            f"MPC Checker returned HTTP {exc.code} ({exc.reason}) for the query"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise MpcUnavailableError(f"could not reach the MPC Checker at {url}: {exc}") from exc
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MpcResponseError(f"MPC Checker response is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise MpcResponseError(
            f"MPC Checker response is not a JSON object but {type(payload).__name__}"
        )
    return payload


# --- response parsing ---------------------------------------------------------------------

#: Accepted wire-name aliases per MpcCandidate field.
_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "designation": ("designation", "desig", "number", "packed"),
    "name": ("name", "object"),
    "ra": ("ra", "ra_deg", "RAJ2000", "RA"),
    "dec": ("dec", "dec_deg", "DEJ2000", "DEC"),
    "motion": ("motion", "rate", "motion_arcsec_per_min", "sky_motion"),
    "pa": ("pa", "posang", "position_angle", "pa_deg"),
    "v": ("v", "v_mag", "mag", "V"),
}


def _cell(row: Mapping[str, Any], field: str) -> Any:
    for alias in _COLUMN_ALIASES[field]:
        if alias in row:
            return row[alias]
    lowered = {str(k).lower(): v for k, v in row.items()}
    for alias in _COLUMN_ALIASES[field]:
        if alias.lower() in lowered:
            return lowered[alias.lower()]
    return None


def _as_float(value: Any, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise MpcResponseError(f"MPC object has a non-numeric {field}: {value!r}")
    try:
        result = float(value)
    except ValueError:
        raise MpcResponseError(f"MPC object has a non-numeric {field}: {value!r}") from None
    if not math.isfinite(result):
        raise MpcResponseError(f"MPC object has a non-finite {field}: {value!r}")
    return result


def _as_optional_float(value: Any, *, field: str) -> float | None:
    if value is None or value == "":
        return None
    return _as_float(value, field=field)


def _parse_candidate(row: Any) -> MpcCandidate:
    """Parse one MPC Checker object row into an :class:`MpcCandidate`."""
    if not isinstance(row, Mapping):
        raise MpcResponseError(f"MPC object entry is not an object: {row!r}")
    designation = _cell(row, "designation")
    if not isinstance(designation, str) or not designation.strip():
        raise MpcResponseError(f"MPC object entry has no designation: {row!r}")
    ra = _as_float(_cell(row, "ra"), field="ra")
    dec = _as_float(_cell(row, "dec"), field="dec")
    if not 0.0 <= ra < 360.0:
        raise MpcResponseError(f"MPC object has ra outside [0, 360) deg: {ra}")
    if not -90.0 <= dec <= 90.0:
        raise MpcResponseError(f"MPC object has dec outside [-90, 90] deg: {dec}")
    motion = _as_float(_cell(row, "motion"), field="motion")
    pa = _as_float(_cell(row, "pa"), field="pa") % 360.0
    if motion < 0.0:
        raise MpcResponseError(f"MPC object has a negative rate: {motion}")
    name = _cell(row, "name")
    return MpcCandidate(
        designation=designation.strip(),
        name=None if name is None else str(name),
        ra_deg=ra,
        dec_deg=dec,
        motion_arcsec_per_min=motion,
        pa_deg=pa,
        v_mag=_as_optional_float(_cell(row, "v"), field="v"),
    )


def parse_mpc_response(payload: Any) -> list[MpcCandidate]:
    """Parse an MPC Checker JSON payload into :class:`MpcCandidate` rows.

    The object list is read from the ``objects`` key (``results`` /
    ``data`` accepted as aliases); an empty list parses to ``[]`` — a normal
    outcome (no known object near the track), not an error. Malformed
    payloads raise :class:`~.errors.MpcResponseError`.
    """
    if not isinstance(payload, dict):
        raise MpcResponseError(
            f"MPC Checker response is not a JSON object but {type(payload).__name__}"
        )
    rows = None
    for key in ("objects", "results", "data"):
        if key in payload:
            rows = payload[key]
            break
    if rows is None:
        raise MpcResponseError("MPC Checker response has no 'objects' list")
    if not isinstance(rows, list):
        raise MpcResponseError(
            f"MPC Checker 'objects' is not a list but {type(rows).__name__}"
        )
    return [_parse_candidate(row) for row in rows]


# --- serialization for the cache (flat rows, JSON-safe) ---------------------------------

#: The fields of a serialized :class:`MpcCandidate` cache row.
_CANDIDATE_FIELDS = frozenset(
    {"designation", "name", "ra_deg", "dec_deg", "motion_arcsec_per_min", "pa_deg", "v_mag"}
)


def _candidate_to_row(candidate: MpcCandidate) -> dict[str, Any]:
    return {
        "designation": candidate.designation,
        "name": candidate.name,
        "ra_deg": candidate.ra_deg,
        "dec_deg": candidate.dec_deg,
        "motion_arcsec_per_min": candidate.motion_arcsec_per_min,
        "pa_deg": candidate.pa_deg,
        "v_mag": candidate.v_mag,
    }


def _row_to_candidate(row: Mapping[str, Any]) -> MpcCandidate:
    return MpcCandidate(
        designation=str(row["designation"]),
        name=row["name"],
        ra_deg=float(row["ra_deg"]),
        dec_deg=float(row["dec_deg"]),
        motion_arcsec_per_min=float(row["motion_arcsec_per_min"]),
        pa_deg=float(row["pa_deg"]),
        v_mag=row["v_mag"],
    )


def _is_candidate_rows(payload: Any) -> bool:
    """Validate a cached MPC payload: a list of candidate-row dicts."""
    return isinstance(payload, list) and all(
        isinstance(row, dict) and set(row) == _CANDIDATE_FIELDS for row in payload
    )


# --- the pure matcher ---------------------------------------------------------------------


def _pa_diff_deg(a: float, b: float) -> float:
    """The absolute position-angle difference in degrees, in [0, 180]."""
    return abs(_wrap_180(a - b))


def match_mover(
    mover: MoverReport,
    candidates: Sequence[MpcCandidate],
    *,
    max_offset_arcsec: float = DEFAULT_MAX_OFFSET_ARCSEC,
    max_motion_rel_diff: float = DEFAULT_MAX_MOTION_REL_DIFF,
    max_pa_diff_deg: float = DEFAULT_MAX_PA_DIFF_DEG,
) -> MpcMatch | None:
    """Match one mover against MPC candidates — PURE (no network).

    A candidate matches when ALL of these hold:

    - the sky offset between the observed mid-track position and the
      candidate ephemeris is at most ``max_offset_arcsec``;
    - the relative rate difference ``|cand - obs| / obs`` is at most
      ``max_motion_rel_diff`` (the observed rate is always > 0 for a flagged
      mover);
    - the position-angle difference is at most ``max_pa_diff_deg``.

    The best candidate (lowest offset, then lowest rate difference) wins;
    ``None`` when no candidate is within tolerance. Raises
    :class:`~.errors.MpcLookupError` for a mover without sky coordinates.
    """
    for name, value in (
        ("max_offset_arcsec", max_offset_arcsec),
        ("max_motion_rel_diff", max_motion_rel_diff),
        ("max_pa_diff_deg", max_pa_diff_deg),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0.0
        ):
            raise MpcLookupError(f"{name} must be a non-negative finite number, got {value!r}")
    track = mover_track_params(mover)
    observed = J2000Coord(ra_deg=track["ra_deg"], dec_deg=track["dec_deg"])
    obs_motion = track["motion_arcsec_per_min"]
    obs_pa = track["pa_deg"]
    best: tuple[float, float, MpcCandidate] | None = None
    for candidate in candidates:
        offset_arcsec = (
            angular_distance(observed, J2000Coord(ra_deg=candidate.ra_deg, dec_deg=candidate.dec_deg))
            * ARCSEC_PER_DEG
        )
        if offset_arcsec > max_offset_arcsec:
            continue
        motion_diff = abs(candidate.motion_arcsec_per_min - obs_motion) / obs_motion
        if motion_diff > max_motion_rel_diff:
            continue
        if _pa_diff_deg(candidate.pa_deg, obs_pa) > max_pa_diff_deg:
            continue
        key = (offset_arcsec, motion_diff)
        if best is None or key < (best[0], best[1]):
            best = (offset_arcsec, motion_diff, candidate)
    if best is None:
        return None
    offset_arcsec, _motion_diff, candidate = best
    return MpcMatch(
        designation=candidate.designation,
        name=candidate.name,
        offset_arcsec=offset_arcsec,
        motion_arcsec_per_min=candidate.motion_arcsec_per_min,
        pa_deg=candidate.pa_deg,
        v_mag=candidate.v_mag,
    )


# --- the public lookups ---------------------------------------------------------------------


def lookup_mover(
    mover: MoverReport,
    *,
    obscode: str = DEFAULT_OBSCODE,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> MpcMatch | None:
    """Look one mover up in the MPC — the raw, uncached query.

    Builds the checker form from the mover's sky track, fetches, parses and
    matches. Returns the :class:`MpcMatch`, or ``None`` when no known object
    is within tolerance (a normal outcome, never an error).

    Raises :class:`~.errors.MpcLookupError` (bad argument / no sky track),
    :class:`~.errors.MpcUnavailableError` (network/HTTP failure) and
    :class:`~.errors.MpcResponseError` (malformed response).
    """
    params = build_checker_params(mover, obscode=obscode)
    payload = fetch_mpc_checker_json(params, timeout=timeout)
    return match_mover(mover, parse_mpc_response(payload))


def lookup_mover_cached(
    mover: MoverReport,
    cache: QueryCache | None = None,
    *,
    obscode: str = DEFAULT_OBSCODE,
    timeout: float = DEFAULT_TIMEOUT_S,
    digits: int = KEY_ROUND_DIGITS,
) -> MpcMatch | None:
    """Cached variant of :func:`lookup_mover` — same result, same errors.

    Goes through ``cache`` (a
    :class:`~seestar_controller.catalog.cache.QueryCache`; default: one in
    ``<config>/cache/mpc/`` with :data:`DEFAULT_TTL_S` TTL). The cache key is
    the **rounded** track params (see :func:`mpc_cache_key`), so repeat
    lookups of the same mover are served from disk with no HTTP at all and
    work offline.

    Only validated results are cached (the response is parsed before it is
    stored, and a cached payload is shape-checked on read), so a malformed
    response or a corrupted cache file is refetched, never served. An empty
    result (no known object) is cached too. If the fetch fails, a valid —
    even expired — cached entry is served as a stale fallback; with no cache
    entry at all the failure becomes :class:`~.errors.MpcUnavailableError`.
    """
    track = mover_track_params(mover)
    if cache is None:
        cache = QueryCache(cache_dir=default_cache_dir(), ttl=DEFAULT_TTL_S)
    key = mpc_cache_key(
        track["mid_jd"],
        track["ra_deg"],
        track["dec_deg"],
        track["motion_arcsec_per_min"],
        track["pa_deg"],
        obscode=obscode,
        digits=digits,
    )
    params = build_checker_params(mover, obscode=obscode)

    def fetch() -> list[dict[str, Any]]:
        payload = fetch_mpc_checker_json(params, timeout=timeout)
        # Parse BEFORE caching: a malformed response is never stored.
        return [_candidate_to_row(c) for c in parse_mpc_response(payload)]

    stale: list[dict[str, Any]] | None = None
    found = cache.get_any(key)
    if found is not None and _is_candidate_rows(found[0]):
        payload, fresh = found
        if fresh:
            return match_mover(mover, [_row_to_candidate(row) for row in payload])
        stale = payload  # expired but valid: the offline fallback
    try:
        rows = fetch()
    except MpcResponseError:
        if stale is not None:
            return match_mover(mover, [_row_to_candidate(row) for row in stale])
        raise
    except Exception as exc:  # network/timeout/HTTP/unexpected; never BaseException
        if stale is not None:
            return match_mover(mover, [_row_to_candidate(row) for row in stale])
        raise MpcUnavailableError(
            f"MPC lookup for mover track {mover.track_id} failed and no cached "
            f"result is available: {exc}. Retry when the network is back — "
            f"previously queried tracks are served from the offline cache at "
            f"{cache.cache_dir}"
        ) from exc
    try:
        cache.set(key, rows)
    except OSError:
        pass  # best-effort: a failed cache write never breaks the query
    return match_mover(mover, [_row_to_candidate(row) for row in rows])


def lookup_movers(
    movers: Sequence[MoverReport],
    cache: QueryCache | None = None,
    *,
    obscode: str = DEFAULT_OBSCODE,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> list[MpcMatch | None]:
    """Look every mover up (cached); returns the matches aligned with
    ``movers`` (``None`` where no known object matched).

    A mover without sky coordinates (subs without a WCS) gets ``None`` —
    the MPC matches on the sky track, so a pixel-only mover cannot be
    queried (documented choice; :func:`lookup_mover` raises for it instead).
    """
    matches: list[MpcMatch | None] = []
    for mover in movers:
        if not isinstance(mover, MoverReport):
            raise MpcLookupError(
                f"movers must be MoverReport records, got {type(mover).__name__}"
            )
        try:
            matches.append(
                lookup_mover_cached(mover, cache, obscode=obscode, timeout=timeout)
            )
        except MpcLookupError:
            matches.append(None)  # no sky track -> no MPC query possible
    return matches


def with_matches(
    movers: Sequence[MoverReport],
    matches: Sequence[MpcMatch | None],
) -> list[MoverReport]:
    """Attach the lookup ``matches`` to the ``movers`` (aligned by index)
    and return the new :class:`~.flag.MoverReport` records (frozen dataclass
    -> ``dataclasses.replace``)."""
    import dataclasses

    if len(movers) != len(matches):
        raise MpcLookupError(
            f"movers and matches must have the same length, got {len(movers)} and {len(matches)}"
        )
    return [
        dataclasses.replace(mover, mpc_match=match) for mover, match in zip(movers, matches)
    ]


def lookup_and_persist(
    project: Any,
    session_id: str,
    *,
    cache: QueryCache | None = None,
    obscode: str = DEFAULT_OBSCODE,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> SessionTracks:
    """Look up every persisted mover of a session and save the matches back
    into the ``movers.json`` sidecar (schema_version 2); returns the
    re-loaded :class:`~.persist.SessionTracks`.

    The tracks are read from the sidecar (raises
    :class:`~.errors.PersistenceError` when missing — run the detect/link/
    flag pipeline first, e.g. via
    :meth:`~.overlay.MoverController.analyze_session`). Each mover is looked
    up through :func:`lookup_movers` (cached); the matches are attached with
    :func:`with_matches` and written back with
    :func:`~.persist.save_tracks`. The store schema is NOT touched.
    """
    from .persist import load_tracks, save_tracks

    session = load_tracks(project, session_id)
    matches = lookup_movers(session.movers, cache, obscode=obscode, timeout=timeout)
    movers = with_matches(session.movers, matches)
    save_tracks(project, session_id, list(session.tracks), movers)
    return load_tracks(project, session_id)


__all__ = [
    "ARCSEC_PER_DEG",
    "CACHE_DIR_ENV_VAR",
    "DEFAULT_CACHE_DIR",
    "DEFAULT_MAX_MOTION_REL_DIFF",
    "DEFAULT_MAX_OFFSET_ARCSEC",
    "DEFAULT_MAX_PA_DIFF_DEG",
    "DEFAULT_OBSCODE",
    "DEFAULT_TIMEOUT_S",
    "DEFAULT_TTL_S",
    "KEY_ROUND_DIGITS",
    "MPC_CHECKER_URL",
    "USER_AGENT",
    "MpcCandidate",
    "MpcMatch",
    "build_checker_params",
    "default_cache_dir",
    "fetch_mpc_checker_json",
    "lookup_and_persist",
    "lookup_mover",
    "lookup_mover_cached",
    "lookup_movers",
    "match_mover",
    "mover_track_params",
    "mpc_cache_key",
    "parse_mpc_response",
    "with_matches",
]
