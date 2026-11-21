"""PARSEC isochrone overlay — the PURE layer of hr-diagram ticket 02.

This is a PURE module: **no ``gi`` import** (the sandbox has no GTK, see
docs/wiki/assumptions.md; a subprocess test enforces the boundary) — stdlib
only. Like the rest of :mod:`seestar_controller.hr_diagram` it imports
**nothing** from :mod:`seestar_controller` at all (the config dir is
re-declared below, exactly like :mod:`seestar_controller.catalog.cache` does,
because this package must not import link/store/catalog).

What lives here:

- :class:`IsochroneError` / :class:`IsochroneFetchError` — the error types;
- :class:`IsochronePoint` — one frozen overlay point (``bp_rp``, ``abs_g``,
  and the apparent ``g = abs_g + dist_mod`` for the CMD overlay);
- :func:`isochrone` — the main entry: ``isochrone(age, feh, dist_mod)`` ->
  the overlay points (abs G vs BP-RP), served from the disk cache when
  possible, else fetched from the fixed PARSEC URL, else (on any fetch
  failure) from the bundled fallback grid;
- :func:`build_isochrone_params` — the PARSEC form fields for (age, feh);
- :func:`fetch_isochrone_json` — **the single HTTP boundary** (stdlib
  :mod:`urllib` POST to :data:`PARSEC_ISOCHRONE_URL`); tests monkeypatch
  exactly this function (or pass ``fetcher=``), so no test ever touches the
  network;
- :func:`parse_isochrone_payload` — parse the PARSEC JSON wire format into
  raw ``(bp_rp, abs_g)`` pairs (the one place wire values become floats);
- the disk cache (:func:`default_cache_dir`, :func:`cache_key`,
  :func:`read_cached_points`, :func:`write_cached_points`) — one JSON file per
  ``(age, feh)`` under ``<config dir>/cache/isochrone/``, keyed by a sha256
  of the rounded parameters, no TTL (isochrones are static);
- :func:`load_fallback_grid` / :func:`fallback_isochrone` — the bundled
  small fallback grid (:data:`FALLBACK_FILE`, a JSON file shipped inside this
  package): the nearest grid entry in (age, feh) serves when the fetch fails
  and the cache is empty.

Documented choices:

- **Parameters.** ``age`` is ``log10(age / yr)`` — the PARSEC convention —
  validated to :data:`MIN_AGE_LOG` .. :data:`MAX_AGE_LOG` (6.0 .. 10.5,
  i.e. ~1 Myr .. ~30 Gyr); ``feh`` is [Fe/H] in
  :data:`MIN_FEH` .. :data:`MAX_FEH` (-4.0 .. 0.5); ``dist_mod`` is the
  distance modulus in mag, any finite number in
  :data:`MIN_DIST_MOD` .. :data:`MAX_DIST_MOD`. Structural type errors
  (non-numbers, bools) raise :class:`TypeError`; value errors (non-finite,
  out of range) raise :class:`IsochroneError` (a :class:`ValueError`).
- **Overlay points.** The PARSEC table gives ABSOLUTE Gaia magnitudes, so
  each point carries ``abs_g`` (the H-R overlay, M_G vs BP-RP) and
  ``g = abs_g + dist_mod`` (the apparent CMD overlay, G vs BP-RP) —
  ``dist_mod`` is therefore applied on return and is NOT part of the cache
  key (the cache stores the absolute pairs).
- **Cache.** Keyed by ``(age, feh)`` rounded to :data:`KEY_AGE_DIGITS` /
  :data:`KEY_FEH_DIGITS` decimals (the issue's "cache keyed by (age, feh)").
  A hit is served with NO network access at all. Reads never raise (a
  corrupt/unreadable entry is a miss); writes are atomic (temp file in the
  same dir + :func:`os.replace`) and best-effort — a failed write never
  breaks the lookup.
- **Fetch + fallback.** On a cache miss the fixed URL is fetched; ANY
  failure of the fetch or the parse (network, HTTP, bad JSON, bad shape)
  falls back to the bundled grid (nearest entry) when ``allow_fallback`` is
  True (default), else re-raises. With ``use_cache=False`` the cache is
  neither read nor written.
- **Wire format.** :func:`parse_isochrone_payload` accepts the PARSEC Gaia
  output as a JSON list of row objects (or ``{"rows": [...]}``) with the
  columns ``Gmag``, ``G_BPmag``, ``G_RPmag`` (finite numbers); the overlay
  pair is ``abs_g = Gmag``, ``bp_rp = G_BPmag - G_RPmag``. Anything else
  raises :class:`IsochroneError`.
"""

from __future__ import annotations

import hashlib
import json
import math
import numbers
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Config dir of the app — the same dir as the PEM key, the protocol log and
#: the catalog cache (re-declared here because this package must not import
#: :mod:`seestar_controller.link` / ``.catalog`` / ``.store``).
DEFAULT_CONFIG_DIR = Path.home() / ".config" / "seestar-controller"

#: Default isochrone cache location: ``<config dir>/cache/isochrone/``.
DEFAULT_CACHE_DIR = DEFAULT_CONFIG_DIR / "cache" / "isochrone"

#: Environment variable that overrides the default cache dir (same
#: explicit-arg > env-var > default precedence as the catalog cache).
CACHE_DIR_ENV_VAR = "SEESTAR_ISOCHRONE_CACHE_DIR"

#: The fixed PARSEC isochrone service URL — the ONLY network endpoint of
#: this module (POSTed a form by :func:`fetch_isochrone_json`).
PARSEC_ISOCHRONE_URL = "https://stev.oapd.inaf.it/cgi-bin/cmd"

#: Default HTTP timeout for one isochrone fetch, in seconds.
DEFAULT_TIMEOUT_S = 30.0

#: User-Agent sent to the PARSEC service (polite-identification convention).
USER_AGENT = "seestar-controller/0.1.0 (isochrone overlay)"

#: Parameter validation ranges (see the module docstring).
MIN_AGE_LOG = 6.0
MAX_AGE_LOG = 10.5
MIN_FEH = -4.0
MAX_FEH = 0.5
MIN_DIST_MOD = -5.0
MAX_DIST_MOD = 30.0

#: Decimals the cache key rounds ``age`` / ``feh`` to.
KEY_AGE_DIGITS = 2
KEY_FEH_DIGITS = 2

#: File suffix of cache entries.
CACHE_FILE_SUFFIX = ".json"

#: The bundled fallback grid shipped inside this package (a JSON file next to
#: this module): ``{"grid": [{"age": ..., "feh": ..., "points": [[bp_rp,
#: abs_g], ...]}, ...]}``.
FALLBACK_FILE = Path(__file__).with_name("isochrone_fallback.json")

#: The PARSEC wire column names parsed by :func:`parse_isochrone_payload`.
PARSEC_G_MAG = "Gmag"
PARSEC_BP_MAG = "G_BPmag"
PARSEC_RP_MAG = "G_RPmag"


class IsochroneError(ValueError):
    """An isochrone failure: invalid parameters (``age`` / ``feh`` /
    ``dist_mod`` out of range or non-finite) or a malformed PARSEC payload —
    always a clean :class:`ValueError`, never a crash."""


class IsochroneFetchError(IsochroneError):
    """The PARSEC service could not be reached or answered unusably
    (network/HTTP failure, non-JSON body) — raised by
    :func:`fetch_isochrone_json`."""


@dataclass(frozen=True)
class IsochronePoint:
    """One frozen isochrone overlay point.

    - ``bp_rp`` — the color ``BP - RP``;
    - ``abs_g`` — the ABSOLUTE G magnitude ``M_G`` from the PARSEC table
      (the H-R diagram overlay: abs G vs BP-RP);
    - ``g`` — the apparent G magnitude ``abs_g + dist_mod`` (the CMD
      overlay: G vs BP-RP).
    """

    bp_rp: float
    abs_g: float
    g: float


def _check_param(value: Any, name: str, lo: float, hi: float) -> float:
    """Validate one isochrone parameter: a real number (bools rejected —
    :class:`TypeError`) in ``[lo, hi]`` and finite (:class:`IsochroneError`);
    returns it as a float."""
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise TypeError(f"{name} must be a number, got {value!r}")
    result = float(value)
    if not math.isfinite(result):
        raise IsochroneError(f"{name} must be finite, got {value!r}")
    if not lo <= result <= hi:
        raise IsochroneError(
            f"{name} must be in [{lo}, {hi}], got {value!r}"
        )
    return result


def check_isochrone_params(age: Any, feh: Any, dist_mod: Any) -> tuple[float, float, float]:
    """Validate the ``(age, feh, dist_mod)`` triple of :func:`isochrone` and
    return it as floats — the single validation point shared by
    :func:`isochrone`, :func:`build_isochrone_params` and
    :func:`fallback_isochrone`."""
    return (
        _check_param(age, "age", MIN_AGE_LOG, MAX_AGE_LOG),
        _check_param(feh, "feh", MIN_FEH, MAX_FEH),
        _check_param(dist_mod, "dist_mod", MIN_DIST_MOD, MAX_DIST_MOD),
    )


def default_cache_dir(env: Mapping[str, str] | None = None) -> Path:
    """Resolve the default cache dir.

    Precedence: the ``SEESTAR_ISOCHRONE_CACHE_DIR`` environment variable >
    the default ``~/.config/seestar-controller/cache/isochrone/``.
    """
    environ = os.environ if env is None else env
    value = environ.get(CACHE_DIR_ENV_VAR)
    if value:
        return Path(value)
    return DEFAULT_CACHE_DIR


def cache_key(age: float, feh: float) -> str:
    """The cache key of an ``(age, feh)`` pair: the sha256 hex of the
    rounded parameters (rounded to :data:`KEY_AGE_DIGITS` /
    :data:`KEY_FEH_DIGITS` decimals, so nearby parameter choices share an
    entry). The ``+ 0.0`` normalizes a rounded ``-0.0`` to ``0.0``, so
    ``feh=-0.004`` and ``feh=0.0`` share the ``"0.00"`` bucket instead of
    splitting into ``"-0.00"`` vs ``"0.00"``."""
    rounded = (
        f"{round(age, KEY_AGE_DIGITS) + 0.0:.{KEY_AGE_DIGITS}f}"
        f"|{round(feh, KEY_FEH_DIGITS) + 0.0:.{KEY_FEH_DIGITS}f}"
    )
    return hashlib.sha256(rounded.encode("utf-8")).hexdigest()


def cache_file(cache_dir: str | Path, age: float, feh: float) -> Path:
    """The cache file for ``(age, feh)``: ``<cache_dir>/<cache_key>.json``."""
    return Path(cache_dir) / (cache_key(age, feh) + CACHE_FILE_SUFFIX)


def read_cached_points(
    cache_dir: str | Path, age: float, feh: float
) -> tuple[tuple[float, float], ...] | None:
    """The cached raw ``(bp_rp, abs_g)`` pairs for ``(age, feh)``, or None
    on a miss. NEVER raises: a missing, unreadable, corrupt, or wrongly
    shaped entry is simply a miss."""
    try:
        raw = cache_file(cache_dir, age, feh).read_text(encoding="utf-8")
        entry = json.loads(raw)
        points = entry["points"]
        if not isinstance(points, list):
            return None
        pairs: list[tuple[float, float]] = []
        for point in points:
            if (
                not isinstance(point, (list, tuple))
                or len(point) != 2
                or not all(isinstance(v, numbers.Real) and not isinstance(v, bool) for v in point)
            ):
                return None
            bp_rp, abs_g = float(point[0]), float(point[1])
            if not (math.isfinite(bp_rp) and math.isfinite(abs_g)):
                return None
            pairs.append((bp_rp, abs_g))
        return tuple(pairs)
    except (OSError, ValueError, TypeError, KeyError):
        return None


def write_cached_points(
    cache_dir: str | Path, age: float, feh: float, points: Sequence[Sequence[float]]
) -> Path | None:
    """Write the raw ``(bp_rp, abs_g)`` pairs for ``(age, feh)`` into the
    cache (atomic: temp file in the same dir + :func:`os.replace`) and
    return the cache file — BEST-EFFORT: any failure returns None and never
    raises."""
    path = cache_file(cache_dir, age, feh)
    entry = {
        "age": round(age, KEY_AGE_DIGITS),
        "feh": round(feh, KEY_FEH_DIGITS),
        "fetched_at": time.time(),
        "points": [[float(p[0]), float(p[1])] for p in points],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=path.parent, prefix=path.name + ".", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(entry, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except OSError:
        return None
    return path


def build_isochrone_params(age: Any, feh: Any) -> dict[str, str]:
    """The PARSEC form fields for the ``(age, feh)`` pair (validated via
    :func:`check_isochrone_params`): ``logAge`` / ``FeH`` as fixed-point
    strings plus the JSON output request. Pure — no network."""
    age_value, feh_value, _ = check_isochrone_params(age, feh, 0.0)
    return {
        "logAge": f"{age_value:.{KEY_AGE_DIGITS}f}",
        "FeH": f"{feh_value:.{KEY_FEH_DIGITS}f}",
        "FORMAT": "json",
    }


def fetch_isochrone_json(
    url: str = PARSEC_ISOCHRONE_URL,
    params: Mapping[str, str] | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> Any:
    """POST the form ``params`` to the fixed PARSEC ``url`` and return the
    parsed JSON body — **the single HTTP boundary of this module** (stdlib
    :mod:`urllib` only). Tests monkeypatch exactly this function, so no test
    ever touches the network.

    Raises :class:`IsochroneFetchError` on network/HTTP failure, on a
    non-JSON body, or when the body is not a JSON list/object.
    """
    form = urllib.parse.urlencode(dict(params or {})).encode("ascii")
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
        raise IsochroneFetchError(
            f"PARSEC returned HTTP {exc.code} ({exc.reason}) at {url}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise IsochroneFetchError(f"could not reach PARSEC at {url}: {exc}") from exc
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IsochroneFetchError(f"PARSEC response is not valid JSON: {exc}") from exc
    if not isinstance(payload, (list, dict)):
        raise IsochroneFetchError(
            f"PARSEC response is not a JSON list/object but {type(payload).__name__}"
        )
    return payload


def parse_isochrone_payload(payload: Any) -> tuple[tuple[float, float], ...]:
    """Parse a PARSEC JSON payload into raw ``(bp_rp, abs_g)`` pairs (the one
    place wire values become floats). Pure.

    Accepted shape: a list of row objects, or ``{"rows": [...]}``, where each
    row has the PARSEC Gaia columns ``Gmag`` / ``G_BPmag`` / ``G_RPmag``
    (finite numbers); the pair is ``abs_g = Gmag``,
    ``bp_rp = G_BPmag - G_RPmag``. Rows are returned in payload order.

    Raises :class:`IsochroneError` when the payload is not that shape or has
    no usable row (an empty isochrone is a failure, so the caller can fall
    back).
    """
    rows = payload.get("rows") if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or not rows:
        raise IsochroneError(
            f"PARSEC payload has no rows (got {type(payload).__name__})"
        )
    pairs: list[tuple[float, float]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise IsochroneError(f"PARSEC row {index} is not an object: {row!r}")
        try:
            g_mag = row[PARSEC_G_MAG]
            bp_mag = row[PARSEC_BP_MAG]
            rp_mag = row[PARSEC_RP_MAG]
        except KeyError as exc:
            raise IsochroneError(
                f"PARSEC row {index} misses the column {exc.args[0]!r}: {row!r}"
            ) from exc
        values = (g_mag, bp_mag, rp_mag)
        if not all(
            isinstance(v, numbers.Real) and not isinstance(v, bool) for v in values
        ):
            raise IsochroneError(f"PARSEC row {index} has non-numeric magnitudes: {row!r}")
        g_float, bp_float, rp_float = float(g_mag), float(bp_mag), float(rp_mag)
        if not all(math.isfinite(v) for v in (g_float, bp_float, rp_float)):
            raise IsochroneError(f"PARSEC row {index} has non-finite magnitudes: {row!r}")
        pairs.append((bp_float - rp_float, g_float))
    return tuple(pairs)


def load_fallback_grid() -> tuple[tuple[float, float, tuple[tuple[float, float], ...]], ...]:
    """The bundled fallback grid as a tuple of ``(age, feh, points)`` entries
    (``points`` = raw ``(bp_rp, abs_g)`` pairs), read from
    :data:`FALLBACK_FILE`. Raises :class:`IsochroneError` when the bundled
    file is missing or malformed (it ships with the package, so that is a
    packaging bug, not a runtime condition)."""
    try:
        raw = FALLBACK_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise IsochroneError(f"bundled fallback grid {FALLBACK_FILE} is unreadable: {exc}") from exc
    grid = data.get("grid") if isinstance(data, dict) else None
    if not isinstance(grid, list) or not grid:
        raise IsochroneError(f"bundled fallback grid {FALLBACK_FILE} has no entries")
    entries: list[tuple[float, float, tuple[tuple[float, float], ...]]] = []
    for index, entry in enumerate(grid):
        if not isinstance(entry, dict):
            raise IsochroneError(f"fallback grid entry {index} is not an object: {entry!r}")
        try:
            age = float(entry["age"])
            feh = float(entry["feh"])
            raw_points = entry["points"]
            if not isinstance(raw_points, list) or not raw_points:
                raise ValueError("points must be a non-empty list")
            points: list[tuple[float, float]] = []
            for point in raw_points:
                if (
                    not isinstance(point, (list, tuple))
                    or len(point) != 2
                    or not all(
                        isinstance(v, numbers.Real) and not isinstance(v, bool) for v in point
                    )
                ):
                    raise ValueError(f"bad point {point!r}")
                bp_rp, abs_g = float(point[0]), float(point[1])
                if not (math.isfinite(bp_rp) and math.isfinite(abs_g)):
                    raise ValueError(f"non-finite point {point!r}")
                points.append((bp_rp, abs_g))
        except (KeyError, TypeError, ValueError) as exc:
            raise IsochroneError(f"fallback grid entry {index} is malformed: {exc}") from exc
        entries.append((age, feh, tuple(points)))
    return tuple(entries)


def fallback_isochrone(
    age: Any, feh: Any, dist_mod: Any
) -> tuple[IsochronePoint, ...]:
    """The bundled fallback: the overlay points of the grid entry NEAREST to
    ``(age, feh)`` (Euclidean in log-age/[Fe/H] space), with ``dist_mod``
    applied. Pure (reads only the bundled file). Raises
    :class:`IsochroneError` on invalid parameters or a broken grid."""
    age_value, feh_value, dist_mod_value = check_isochrone_params(age, feh, dist_mod)
    best: tuple[float, float, tuple[tuple[float, float], ...]] | None = None
    best_distance = math.inf
    for entry_age, entry_feh, points in load_fallback_grid():
        distance = (entry_age - age_value) ** 2 + (entry_feh - feh_value) ** 2
        if distance < best_distance:
            best_distance = distance
            best = (entry_age, entry_feh, points)
    assert best is not None  # load_fallback_grid raises on an empty grid
    return _points(best[2], dist_mod_value)


def _points(
    pairs: Sequence[Sequence[float]], dist_mod: float
) -> tuple[IsochronePoint, ...]:
    """Build the frozen :class:`IsochronePoint` tuple from raw
    ``(bp_rp, abs_g)`` pairs, applying ``dist_mod`` (``g = abs_g + dist_mod``)."""
    return tuple(
        IsochronePoint(bp_rp=float(p[0]), abs_g=float(p[1]), g=float(p[1]) + dist_mod)
        for p in pairs
    )


def isochrone(
    age: Any,
    feh: Any,
    dist_mod: Any,
    *,
    cache_dir: str | Path | None = None,
    fetcher: Callable[..., Any] | None = None,
    use_cache: bool = True,
    allow_fallback: bool = True,
) -> tuple[IsochronePoint, ...]:
    """The PARSEC isochrone overlay for ``(age, feh, dist_mod)`` as a tuple of
    frozen :class:`IsochronePoint` (abs G vs BP-RP, plus the apparent ``g``).

    - ``age`` — ``log10(age / yr)`` in :data:`MIN_AGE_LOG` ..
      :data:`MAX_AGE_LOG`; ``feh`` — [Fe/H] in :data:`MIN_FEH` ..
      :data:`MAX_FEH`; ``dist_mod`` — the distance modulus in mag in
      :data:`MIN_DIST_MOD` .. :data:`MAX_DIST_MOD` (TypeError for
      non-numbers, :class:`IsochroneError` for bad values — raised BEFORE
      anything is read or fetched);
    - ``cache_dir`` — the disk cache dir; None (default) resolves via
      :func:`default_cache_dir` (``SEESTAR_ISOCHRONE_CACHE_DIR`` env var >
      ``~/.config/seestar-controller/cache/isochrone/``);
    - ``fetcher`` — the fetch callable ``fetcher(url=..., params=...,
      timeout=...)``; None (default) uses the module-level
      :func:`fetch_isochrone_json` (looked up at call time, so tests may
      monkeypatch it);
    - ``use_cache`` — False skips the cache entirely (no read, no write);
    - ``allow_fallback`` — False re-raises a fetch/parse failure instead of
      serving the bundled fallback grid.

    Resolution order: a fresh cache entry is served with NO network access;
    on a miss the fixed :data:`PARSEC_ISOCHRONE_URL` is fetched (and the
    result cached); on ANY fetch/parse failure the bundled fallback grid
    serves the nearest ``(age, feh)`` entry (when ``allow_fallback``).
    """
    age_value, feh_value, dist_mod_value = check_isochrone_params(age, feh, dist_mod)
    cache_dir_path = Path(cache_dir) if cache_dir is not None else default_cache_dir()
    if use_cache:
        cached = read_cached_points(cache_dir_path, age_value, feh_value)
        if cached is not None:
            return _points(cached, dist_mod_value)
    fetch = fetcher if fetcher is not None else fetch_isochrone_json
    try:
        payload = fetch(
            url=PARSEC_ISOCHRONE_URL,
            params=build_isochrone_params(age_value, feh_value),
            timeout=DEFAULT_TIMEOUT_S,
        )
        pairs = parse_isochrone_payload(payload)
    except Exception:
        if not allow_fallback:
            raise
        return fallback_isochrone(age_value, feh_value, dist_mod_value)
    if use_cache:
        write_cached_points(cache_dir_path, age_value, feh_value, pairs)
    return _points(pairs, dist_mod_value)
