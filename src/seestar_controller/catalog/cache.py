"""On-disk JSON query cache for catalog lookups — repeat lookups are instant
and work offline.

Pure-Python (module boundary per ``.scratch/catalog/spec.md``): no GTK
imports, no :mod:`seestar_controller.link` imports, stdlib only.

Each cache entry is one JSON file under the cache dir (by default
``~/.config/seestar-controller/cache/catalog/`` — the same config dir as the
PEM key and the protocol log; re-declared here because this package must not
import :mod:`seestar_controller.link`), named ``<sha256-of-query>.json`` so
the key is stable and filename-safe. An entry looks like::

    {"query": "...", "fetched_at": 1763600000.0, "ttl": 86400.0, "payload": ...}

- ``query`` — the query string the entry is keyed by (stored verbatim so a
  cache file is self-describing);
- ``fetched_at`` — epoch seconds (``time.time``) of the fetch;
- ``ttl`` — time-to-live in seconds for this entry (``null`` = never expires);
- ``payload`` — the JSON-serializable result of the query.

TTL is configurable per cache (:class:`QueryCache` ``ttl=``, default
:data:`DEFAULT_TTL_S` = 24 h, ``None`` = infinite) and per write
(:meth:`QueryCache.set` / :meth:`QueryCache.cached` ``ttl=``). A fresh entry
is served from disk without any network access; an expired entry is removed
and refetched. :meth:`QueryCache.get_any` instead reads an entry ignoring the
TTL (stale allowed, never removed from disk) — the offline fallback used by
the facade in :mod:`.facade` to serve a stale entry when a fetch fails.

Robustness: a corrupted, unreadable, or wrongly-shaped cache file is treated
as a miss and refetched — reading the cache never raises. Writes are atomic
(temp file in the same dir + fsync + :func:`os.replace`, the same pattern as
``link/frames.py``'s ``atomic_write``), so a crash never leaves a partial
cache file; a failed write is best-effort and never breaks the query itself.

The wrapper for catalog queries is :meth:`QueryCache.cached` — pass any query
string and a fetch callable; the cached variant of name resolution is
:func:`resolve_name_cached` (the raw :func:`~.simbad.resolve_name` stays
uncached). Tests mock the single HTTP boundary
:func:`~.simbad.fetch_tap_json`, so no test ever touches the network.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from . import simbad
from .simbad import CatalogCandidate

#: Config dir of the app — the same dir as the PEM key and the protocol log
#: (``seestar_controller.link.keys`` / ``link.protocol_log`` use the same
#: default; re-declared here because this package must not import link).
DEFAULT_CONFIG_DIR = Path.home() / ".config" / "seestar-controller"

#: Default cache location: ``<config dir>/cache/catalog/``.
DEFAULT_CACHE_DIR = DEFAULT_CONFIG_DIR / "cache" / "catalog"

#: Environment variable that overrides the default cache dir (same
#: explicit-arg > env-var > default precedence as the key path).
CACHE_DIR_ENV_VAR = "SEESTAR_CACHE_DIR"

#: Default time-to-live of a cache entry, in seconds (24 h).
DEFAULT_TTL_S = 24 * 60 * 60.0

#: File suffix of cache entries.
CACHE_FILE_SUFFIX = ".json"

#: Sentinel for "use the cache's default TTL" (distinct from ``None`` =
#: never expires).
_DEFAULT_TTL = object()

#: The keys every cache entry must have to be considered well-formed.
_ENTRY_KEYS = frozenset({"query", "fetched_at", "ttl", "payload"})

#: The fields of a serialized :class:`~.simbad.CatalogCandidate`.
_CANDIDATE_FIELDS = frozenset(CatalogCandidate.__dataclass_fields__)


def default_cache_dir(env: Mapping[str, str] | None = None) -> Path:
    """Resolve the default cache dir.

    Precedence: the ``SEESTAR_CACHE_DIR`` environment variable > the default
    ``~/.config/seestar-controller/cache/catalog/``.
    """
    environ = os.environ if env is None else env
    value = environ.get(CACHE_DIR_ENV_VAR)
    if value:
        return Path(value)
    return DEFAULT_CACHE_DIR


def cache_key(query: str) -> str:
    """Stable, filename-safe cache key for a ``query`` string: SHA-256 hex."""
    if not isinstance(query, str):
        raise ValueError(f"query must be a string, got {query!r}")
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def _validate_ttl(ttl: Any) -> float | None:
    """Validate a TTL: ``None`` (never expires) or a positive finite number."""
    if ttl is None:
        return None
    if (
        isinstance(ttl, bool)
        or not isinstance(ttl, (int, float))
        or not math.isfinite(ttl)
        or ttl <= 0
    ):
        raise ValueError(
            f"ttl must be None (never expires) or a positive number of seconds, got {ttl!r}"
        )
    return float(ttl)


def _atomic_write_json(path: Path, obj: Any) -> Path:
    """Write ``obj`` as pretty JSON to ``path`` atomically.

    Same pattern as ``link/frames.py``'s ``atomic_write``: temp file in the
    same directory, flush + fsync, then :func:`os.replace` onto the
    destination. On any failure (including :class:`KeyboardInterrupt`) the
    temp file is removed and the destination is left untouched — a reader
    never sees a partial file. Returns the destination path.
    """
    data = (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.close(fd)  # already closed when the with-block exited
        except OSError:
            pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


class QueryCache:
    """A JSON-file cache under ``cache_dir``, keyed by :func:`cache_key`.

    - ``cache_dir`` — directory holding the ``<sha256>.json`` entries
      (default: :func:`default_cache_dir`, i.e.
      ``~/.config/seestar-controller/cache/catalog/``);
    - ``ttl`` — default time-to-live of entries in seconds
      (:data:`DEFAULT_TTL_S` = 24 h; ``None`` = never expires);
    - ``now`` — clock override, a callable returning epoch seconds
      (default :func:`time.time`; tests inject a fake clock).

    Reads never raise: a missing, unreadable, corrupted, or expired entry is
    a miss (``get`` returns ``None``). Writes are atomic (see
    :func:`_atomic_write_json`). :meth:`get_any` is the stale-allowed read
    used by the offline facade (:mod:`.facade`).
    """

    def __init__(
        self,
        cache_dir: str | Path | None = None,
        *,
        ttl: float | None = DEFAULT_TTL_S,
        now: Callable[[], float] | None = None,
    ) -> None:
        if cache_dir is None:
            cache_dir = default_cache_dir()
        self.cache_dir = Path(cache_dir)
        self.ttl = _validate_ttl(ttl)
        self._now = time.time if now is None else now

    def path_for(self, query: str) -> Path:
        """The cache file for ``query``: ``<cache_dir>/<sha256(query)>.json``."""
        return self.cache_dir / (cache_key(query) + CACHE_FILE_SUFFIX)

    def get(self, query: str) -> Any | None:
        """Return the cached payload for ``query``, or ``None`` on a miss.

        A miss is: no file, an unreadable file, a corrupted/malformed entry,
        or an expired entry (expired entries are also removed from disk).
        Never raises.
        """
        found = self.get_any(query)
        if found is None:
            return None
        payload, fresh = found
        if not fresh:
            self._discard(query)  # expired: drop the stale file, treat as miss
            return None
        return payload

    def get_any(self, query: str) -> tuple[Any, bool] | None:
        """Return ``(payload, is_fresh)`` for ``query``, or ``None`` on a miss.

        Unlike :meth:`get`, an expired entry is returned too (with
        ``is_fresh=False``) and is NOT removed from disk — the offline layer
        (:mod:`.facade`) reads through this method to serve a stale entry as a
        fallback when a fetch fails. Never raises.
        """
        entry = self._read_entry(query)
        if entry is None:
            return None
        ttl = entry["ttl"]
        fresh = ttl is None or self._now() - entry["fetched_at"] <= ttl
        return entry["payload"], fresh

    def set(self, query: str, payload: Any, *, ttl: Any = _DEFAULT_TTL) -> Path:
        """Store ``payload`` for ``query`` atomically; returns the file path.

        ``ttl`` overrides the cache's default TTL for this entry (``None`` =
        never expires). ``payload`` must be JSON-serializable.
        """
        entry = {
            "query": query,
            "fetched_at": float(self._now()),
            "ttl": self.ttl if ttl is _DEFAULT_TTL else _validate_ttl(ttl),
            "payload": payload,
        }
        return _atomic_write_json(self.path_for(query), entry)

    def cached(
        self,
        query: str,
        fetch: Callable[[], Any],
        *,
        ttl: Any = _DEFAULT_TTL,
        validate: Callable[[Any], bool] | None = None,
    ) -> Any:
        """Return the cached payload for ``query``, fetching and storing on a miss.

        This is the wrapper every catalog query goes through: ``fetch`` is
        only called on a miss (fresh entries are served from disk with no
        network access). ``validate`` optionally checks a cached payload's
        shape — a payload that fails validation is treated as a miss and
        refetched. A failed cache write is best-effort and never raises.
        """
        payload = self.get(query)
        if payload is not None and (validate is None or validate(payload)):
            return payload
        payload = fetch()
        try:
            self.set(query, payload, ttl=ttl)
        except OSError:
            pass  # best-effort: a failed cache write never breaks the query
        return payload

    def clear(self) -> int:
        """Remove all cache entries; returns the number of files removed."""
        count = 0
        if self.cache_dir.is_dir():
            for path in self.cache_dir.glob("*" + CACHE_FILE_SUFFIX):
                try:
                    path.unlink()
                    count += 1
                except OSError:
                    pass
        return count

    def _read_entry(self, query: str) -> dict[str, Any] | None:
        """Read and validate the entry file for ``query``; ``None`` if unusable."""
        try:
            raw = self.path_for(query).read_bytes()
        except OSError:
            return None
        try:
            entry = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(entry, dict) or not _ENTRY_KEYS <= entry.keys():
            return None
        if entry["query"] != query:
            return None
        fetched_at = entry["fetched_at"]
        if (
            isinstance(fetched_at, bool)
            or not isinstance(fetched_at, (int, float))
            or not math.isfinite(fetched_at)
        ):
            return None
        ttl = entry["ttl"]
        if ttl is not None and (
            isinstance(ttl, bool)
            or not isinstance(ttl, (int, float))
            or not math.isfinite(ttl)
            or ttl <= 0
        ):
            return None
        return entry

    def _discard(self, query: str) -> None:
        """Best-effort removal of the entry file for ``query``."""
        try:
            self.path_for(query).unlink()
        except OSError:
            pass


def _is_candidate_rows(payload: Any) -> bool:
    """Validate a cached name-resolution payload: a list of candidate dicts."""
    return isinstance(payload, list) and all(
        isinstance(row, dict) and set(row) == _CANDIDATE_FIELDS for row in payload
    )


def _name_query(adql: str, *, maxrec: int) -> str:
    """The canonical cache query string for one name lookup.

    The ADQL alone does not determine the result (``maxrec`` is a separate
    TAP form field), so the key covers both.
    """
    return json.dumps({"query": adql, "maxrec": int(maxrec)}, sort_keys=True)


def resolve_name_cached(
    name: str,
    cache: QueryCache | None = None,
    *,
    mag_filter: str = simbad.DEFAULT_MAG_FILTER,
    maxrec: int = simbad.DEFAULT_MAX_CANDIDATES,
    timeout: float = simbad.DEFAULT_TIMEOUT_S,
) -> list[CatalogCandidate]:
    """Cached variant of :func:`~.simbad.resolve_name` — same result, same errors.

    Goes through ``cache`` (a :class:`QueryCache`; default: one in the app
    config dir with the default TTL): a fresh cache entry is served from
    disk with no network access, so repeat lookups are instant and work
    offline. The raw :func:`~.simbad.resolve_name` stays uncached.

    Only validated results are cached (the TAP response is parsed before it
    is stored, and a cached payload is shape-checked on read), so a malformed
    response or a corrupted cache file is refetched, never served. An empty
    result (no match) is cached too — offline, a known-unknown name still
    raises :class:`~.simbad.NameNotFoundError` without network.

    Raises the same errors as :func:`~.simbad.resolve_name`:
    :class:`ValueError` for a blank name / ``maxrec`` < 1,
    :class:`~.simbad.NameNotFoundError`, :class:`~.simbad.SimbadUnavailableError`,
    :class:`~.simbad.SimbadResponseError`.
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
    query = _name_query(adql, maxrec=maxrec)

    def fetch() -> list[dict[str, Any]]:
        payload = simbad.fetch_tap_json(adql, maxrec=maxrec, timeout=timeout)
        # Parse BEFORE caching: a malformed response is never stored.
        return [dataclasses.asdict(c) for c in simbad.parse_tap_response(payload)]

    rows = cache.cached(query, fetch, validate=_is_candidate_rows)
    candidates = [CatalogCandidate(**row) for row in rows]
    if not candidates:
        raise simbad.NameNotFoundError(
            f"SIMBAD has no object named {query_name!r} — check the spelling; "
            "SIMBAD identifiers look like 'M 42', 'NGC 1976' or 'V* bet Per', "
            f"or append '*' for a wildcard match (e.g. '{query_name}*')"
        )
    return candidates
