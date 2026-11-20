"""On-disk query cache with mocked HTTP — no real network.

The HTTP boundary is :func:`seestar_controller.catalog.simbad.fetch_tap_json`
(monkeypatched, same as tests/test_catalog_names.py); every cache lives in a
``tmp_path`` dir with an injected fake clock, so TTL expiry is tested without
sleeping.
"""

import dataclasses
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from seestar_controller.catalog import (
    CACHE_DIR_ENV_VAR,
    DEFAULT_CACHE_DIR,
    DEFAULT_CONFIG_DIR,
    DEFAULT_TTL_S,
    SIMBAD_TAP_SYNC_URL,
    CatalogCandidate,
    NameNotFoundError,
    QueryCache,
    SimbadUnavailableError,
    cache_key,
    default_cache_dir,
    resolve_name,
    resolve_name_cached,
)
from seestar_controller.catalog import cache as cache_mod
from seestar_controller.catalog import simbad

#: Column order of the mocked TAP JSON payloads (mirrors build_lookup_query).
COLUMNS = ["oid", "main_id", "ra", "dec", "otype", "id", "filter", "flux"]

#: M 42 (Orion Nebula): RA 05h35m17.3s = 83.82208 deg, Dec -05d23m28s = -5.39114 deg.
M42_ROW = [1001, "M 42", 83.82208, -5.39114, "HII", "M 42", "V", 4.0]
#: A row without V flux / object type.
NO_MAG_ROW = [2001, "M 43", 83.90875, -5.26778, "", "M 43", None, None]

#: Fixed epoch the fake clock starts at.
T0 = 1_700_000_000.0


def tap_payload(rows, columns=COLUMNS):
    """Build a SIMBAD TAP JSON body: ``metadata`` column names + ``data`` rows."""
    return {
        "metadata": [{"name": c, "datatype": "char"} for c in columns],
        "data": rows,
    }


@pytest.fixture()
def fake_simbad(monkeypatch):
    """Monkeypatch the single fetch function; queue payloads, record calls."""
    calls = []
    responses = []

    def fake_fetch(query, *, maxrec, timeout, url=SIMBAD_TAP_SYNC_URL):
        calls.append({"query": query, "maxrec": maxrec, "timeout": timeout, "url": url})
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(simbad, "fetch_tap_json", fake_fetch)
    return SimpleNamespace(calls=calls, responses=responses)


@pytest.fixture()
def clock():
    """A mutable fake clock: ``clock[0]`` is 'now' in epoch seconds."""
    return [T0]


@pytest.fixture()
def cache(tmp_path, clock):
    """A QueryCache in a tmp dir with a 60 s TTL and the fake clock."""
    return QueryCache(tmp_path / "cache" / "catalog", ttl=60.0, now=lambda: clock[0])


def cache_files(cache):
    """All cache entry files currently on disk."""
    return sorted(cache.cache_dir.glob("*.json"))


def read_entry(cache, query):
    """Read and parse the entry file for ``query``."""
    return json.loads(cache.path_for(query).read_text(encoding="utf-8"))


# --- miss: fetches and stores ---------------------------------------------------


def test_miss_fetches_and_stores_entry(fake_simbad, cache, clock):
    fake_simbad.responses.append(tap_payload([M42_ROW]))
    candidates = resolve_name_cached("M 42", cache)

    assert len(fake_simbad.calls) == 1
    assert len(candidates) == 1
    c = candidates[0]
    assert c == CatalogCandidate(
        name="M 42", ra_deg=83.82208, dec_deg=-5.39114, magnitude=4.0, otype="HII", matched_id="M 42"
    )
    # exactly one entry file, storing {query, fetched_at, ttl, payload}
    files = cache_files(cache)
    assert len(files) == 1
    entry = json.loads(files[0].read_text(encoding="utf-8"))
    assert set(entry) == {"query", "fetched_at", "ttl", "payload"}
    assert entry["fetched_at"] == T0
    assert entry["ttl"] == 60.0
    assert entry["payload"] == [dataclasses.asdict(c)]
    assert isinstance(entry["query"], str) and "FROM ident" in entry["query"]


def test_miss_forwards_maxrec_timeout_and_mag_filter(fake_simbad, cache):
    fake_simbad.responses.append(tap_payload([M42_ROW]))
    resolve_name_cached("M 42", cache, mag_filter="B", maxrec=5, timeout=3.0)
    call = fake_simbad.calls[0]
    assert call["maxrec"] == 5
    assert call["timeout"] == 3.0
    assert "f.filter = 'B'" in call["query"]


def test_cached_payload_preserves_none_fields(fake_simbad, cache):
    fake_simbad.responses.append(tap_payload([NO_MAG_ROW]))
    first = resolve_name_cached("M 43", cache)
    assert first[0].magnitude is None and first[0].otype is None
    # refetch-free second read reconstructs an equal candidate
    fake_simbad.responses.append(SimbadUnavailableError("offline"))
    second = resolve_name_cached("M 43", cache)
    assert second == first


# --- hit: served from disk with the network down ---------------------------------


def test_second_call_served_from_cache_with_fetch_raising(fake_simbad, cache):
    fake_simbad.responses.append(tap_payload([M42_ROW]))
    first = resolve_name_cached("M 42", cache)
    # network is now down: any fetch would raise
    fake_simbad.responses.append(SimbadUnavailableError("could not reach SIMBAD"))
    second = resolve_name_cached("M 42", cache)
    assert second == first
    assert len(fake_simbad.calls) == 1  # no second fetch — served from disk


def test_raw_resolve_name_stays_uncached(fake_simbad, cache):
    fake_simbad.responses.extend([tap_payload([M42_ROW]), tap_payload([M42_ROW])])
    resolve_name("M 42")
    resolve_name("M 42")
    assert len(fake_simbad.calls) == 2  # the raw query never touches the cache
    assert cache_files(cache) == []


def test_not_found_result_is_cached_and_raises_offline(fake_simbad, cache):
    fake_simbad.responses.append(tap_payload([]))
    with pytest.raises(NameNotFoundError, match="no object named 'M 99'"):
        resolve_name_cached("M 99", cache)
    assert len(fake_simbad.calls) == 1
    # offline, the cached empty result still raises NameNotFoundError
    fake_simbad.responses.append(SimbadUnavailableError("offline"))
    with pytest.raises(NameNotFoundError):
        resolve_name_cached("M 99", cache)
    assert len(fake_simbad.calls) == 1


# --- TTL ------------------------------------------------------------------------


def test_fresh_entry_within_ttl_is_served_from_cache(fake_simbad, cache, clock):
    fake_simbad.responses.append(tap_payload([M42_ROW]))
    resolve_name_cached("M 42", cache)
    clock[0] += 59.0  # still within the 60 s TTL
    fake_simbad.responses.append(SimbadUnavailableError("offline"))
    assert resolve_name_cached("M 42", cache)[0].name == "M 42"
    assert len(fake_simbad.calls) == 1


def test_expired_entry_refetches(fake_simbad, cache, clock):
    fake_simbad.responses.append(tap_payload([M42_ROW]))
    resolve_name_cached("M 42", cache)
    clock[0] += 61.0  # past the 60 s TTL
    fake_simbad.responses.append(tap_payload([M42_ROW, NO_MAG_ROW]))
    candidates = resolve_name_cached("M 42", cache)
    assert len(fake_simbad.calls) == 2  # refetched
    assert len(candidates) == 2
    # the entry was rewritten with the new fetch time; still exactly one file
    assert len(cache_files(cache)) == 1
    entry = json.loads(cache_files(cache)[0].read_text(encoding="utf-8"))
    assert entry["fetched_at"] == T0 + 61.0


def test_expired_entry_file_is_removed_on_read(cache, clock):
    cache.set("SELECT 1", {"data": [1]})
    assert cache.get("SELECT 1") == {"data": [1]}
    clock[0] += 61.0
    assert cache.get("SELECT 1") is None  # expired -> miss
    assert not cache.path_for("SELECT 1").exists()  # stale file removed


def test_ttl_none_never_expires(fake_simbad, tmp_path, clock):
    forever = QueryCache(tmp_path / "cache", ttl=None, now=lambda: clock[0])
    fake_simbad.responses.append(tap_payload([M42_ROW]))
    resolve_name_cached("M 42", forever)
    clock[0] += 10 * 365 * 24 * 3600.0  # ten years later
    fake_simbad.responses.append(SimbadUnavailableError("offline"))
    assert resolve_name_cached("M 42", forever)[0].name == "M 42"
    assert len(fake_simbad.calls) == 1
    entry = json.loads(cache_files(forever)[0].read_text(encoding="utf-8"))
    assert entry["ttl"] is None


def test_per_call_ttl_override(cache, clock):
    cache.cached("q", lambda: {"v": 1}, ttl=5.0)
    assert read_entry(cache, "q")["ttl"] == 5.0
    clock[0] += 6.0
    assert cache.get("q") is None  # the per-entry TTL wins over the cache default
    cache.cached("q2", lambda: {"v": 2}, ttl=None)
    clock[0] += 10**9
    assert cache.get("q2") == {"v": 2}  # per-entry infinite TTL


@pytest.mark.parametrize("bad_ttl", [0, -1, -0.5, float("inf"), float("nan"), "60", True])
def test_invalid_ttl_rejected(bad_ttl, tmp_path):
    with pytest.raises(ValueError, match="ttl"):
        QueryCache(tmp_path / "cache", ttl=bad_ttl)


def test_default_ttl_is_24h(tmp_path):
    assert DEFAULT_TTL_S == 24 * 60 * 60.0
    assert QueryCache(tmp_path / "cache").ttl == DEFAULT_TTL_S


# --- corrupted / unreadable cache files ------------------------------------------


@pytest.mark.parametrize(
    "garbage",
    [
        b"not json at all {{{",
        b"",
        b"\xff\xfe binary garbage",
        b"[1, 2, 3]",  # valid JSON, not an entry object
        b'"just a string"',
        json.dumps({"query": "q", "fetched_at": T0, "ttl": 60.0}).encode(),  # no payload
        json.dumps({"query": "other", "fetched_at": T0, "ttl": 60.0, "payload": 1}).encode(),  # wrong query
        json.dumps({"query": "q", "fetched_at": "soon", "ttl": 60.0, "payload": 1}).encode(),  # bad fetched_at
        json.dumps({"query": "q", "fetched_at": T0, "ttl": -5, "payload": 1}).encode(),  # bad ttl
        json.dumps({"query": "q", "fetched_at": T0, "ttl": 60.0, "payload": 1}, indent=2).encode()[:-1],  # truncated
    ],
)
def test_corrupted_entry_is_a_miss_and_refetches(cache, garbage):
    path = cache.path_for("q")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(garbage)
    calls = []

    def fetch():
        calls.append(1)
        return {"fresh": True}

    assert cache.get("q") is None  # never raises
    assert cache.cached("q", fetch) == {"fresh": True}
    assert calls == [1]  # refetched
    # and the corrupted file was replaced by a valid entry
    assert read_entry(cache, "q")["payload"] == {"fresh": True}


def test_corrupted_name_cache_file_falls_back_to_network(fake_simbad, cache):
    fake_simbad.responses.append(tap_payload([M42_ROW]))
    resolve_name_cached("M 42", cache)
    # scribble garbage over the single cache file
    cache_files(cache)[0].write_bytes(b"garbage{{{")
    fake_simbad.responses.append(tap_payload([M42_ROW]))
    candidates = resolve_name_cached("M 42", cache)
    assert len(fake_simbad.calls) == 2  # fell back to the network
    assert candidates[0].name == "M 42"
    entry = json.loads(cache_files(cache)[0].read_text(encoding="utf-8"))  # rewritten valid
    assert entry["payload"][0]["name"] == "M 42"


def test_wrong_shaped_payload_is_refetched_via_validate(cache):
    cache.set("q", 42)  # valid entry, wrong payload shape for this query
    calls = []

    def fetch():
        calls.append(1)
        return [{"name": "M 42"}]

    rows = cache.cached("q", fetch, validate=lambda p: isinstance(p, list))
    assert rows == [{"name": "M 42"}]
    assert calls == [1]  # the cached 42 failed validation -> refetch


def test_malformed_simbad_response_is_never_cached(fake_simbad, cache):
    fake_simbad.responses.append({"metadata": [], "data": []})  # raises SimbadResponseError
    with pytest.raises(simbad.SimbadResponseError):
        resolve_name_cached("M 42", cache)
    assert cache_files(cache) == []  # nothing stored


# --- key stability ---------------------------------------------------------------


def test_cache_key_is_stable_and_filename_safe():
    assert cache_key("SELECT 1") == cache_key("SELECT 1")
    assert cache_key("SELECT 1") != cache_key("SELECT 2")
    key = cache_key("M 42 -- weird ' chars / and \\ slashes")
    assert set(key) <= set("0123456789abcdef")  # hex -> filename-safe
    with pytest.raises(ValueError, match="string"):
        cache_key(42)


def test_same_query_same_file_different_queries_different_files(fake_simbad, cache):
    fake_simbad.responses.append(tap_payload([M42_ROW]))
    resolve_name_cached("M 42", cache)
    adql = simbad.build_lookup_query("M 42")
    query = cache_mod._name_query(adql, maxrec=simbad.DEFAULT_MAX_CANDIDATES)
    path1 = cache.path_for(query)
    assert cache_files(cache) == [path1]
    # same query -> same file (no duplicate entries; no fetch either)
    resolve_name_cached("M 42", cache)
    assert len(fake_simbad.calls) == 1
    assert cache_files(cache) == [path1]
    # a different name -> a different file
    fake_simbad.responses.append(tap_payload([NO_MAG_ROW]))
    resolve_name_cached("M 43", cache)
    assert len(cache_files(cache)) == 2
    # same name but different maxrec -> a different file (maxrec is part of the key)
    fake_simbad.responses.append(tap_payload([M42_ROW]))
    resolve_name_cached("M 42", cache, maxrec=5)
    assert len(cache_files(cache)) == 3


# --- atomic writes ---------------------------------------------------------------


def test_writes_leave_no_tmp_files(cache):
    cache.set("a", {"v": 1})
    cache.set("b", {"v": 2})
    cache.cached("c", lambda: {"v": 3})
    assert cache_files(cache) and not list(cache.cache_dir.glob("*.tmp"))
    assert not [p for p in cache.cache_dir.iterdir() if p.suffix != ".json"]


def test_failed_write_keeps_original_file_and_no_tmp(cache, monkeypatch):
    cache.set("q", {"v": "original"})
    original = cache.path_for("q").read_bytes()

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(cache_mod.os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        cache.set("q", {"v": "new"})
    assert cache.path_for("q").read_bytes() == original  # destination untouched
    assert not list(cache.cache_dir.glob("*.tmp"))  # temp file cleaned up


def test_failed_cache_write_never_breaks_the_query(cache, monkeypatch):
    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(cache_mod.os, "replace", boom)
    # cached() swallows the write failure and still returns the fetched payload
    assert cache.cached("q", lambda: {"v": 42}) == {"v": 42}


# --- generic wrapper -------------------------------------------------------------


def test_generic_cached_wrapper_roundtrip(cache):
    calls = []

    def fetch():
        calls.append(1)
        return {"data": [1, 2, 3]}

    assert cache.cached("SELECT * FROM x", fetch) == {"data": [1, 2, 3]}
    assert cache.cached("SELECT * FROM x", fetch) == {"data": [1, 2, 3]}
    assert calls == [1]  # second call served from disk
    entry = read_entry(cache, "SELECT * FROM x")
    assert entry["query"] == "SELECT * FROM x"
    assert entry["payload"] == {"data": [1, 2, 3]}


def test_clear_removes_all_entries(cache):
    cache.set("a", 1)
    cache.set("b", 2)
    cache.cached("c", lambda: 3)
    assert cache.clear() == 3
    assert cache_files(cache) == []
    assert cache.clear() == 0  # idempotent, also on a missing dir
    assert QueryCache(cache.cache_dir / "nope").clear() == 0


# --- input validation ------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "   ", "\t\n"])
def test_blank_name_rejected_without_fetch_or_cache(fake_simbad, cache, bad):
    with pytest.raises(ValueError, match="empty"):
        resolve_name_cached(bad, cache)
    assert fake_simbad.calls == []
    assert cache_files(cache) == []


def test_invalid_name_and_maxrec_rejected(fake_simbad, cache):
    with pytest.raises(ValueError, match="string"):
        resolve_name_cached(None, cache)
    with pytest.raises(ValueError, match=">= 1"):
        resolve_name_cached("M 42", cache, maxrec=0)
    assert fake_simbad.calls == []


# --- default location under the app config dir -----------------------------------


def test_default_cache_dir_is_under_the_config_dir(monkeypatch):
    monkeypatch.delenv(CACHE_DIR_ENV_VAR, raising=False)
    assert DEFAULT_CONFIG_DIR == Path.home() / ".config" / "seestar-controller"
    assert default_cache_dir() == DEFAULT_CACHE_DIR
    assert DEFAULT_CACHE_DIR == DEFAULT_CONFIG_DIR / "cache" / "catalog"
    assert QueryCache().cache_dir == DEFAULT_CACHE_DIR
    # env var overrides the default (same precedence pattern as the key path)
    assert default_cache_dir({CACHE_DIR_ENV_VAR: "/tmp/elsewhere"}) == Path("/tmp/elsewhere")
    monkeypatch.setenv(CACHE_DIR_ENV_VAR, "/tmp/from-env")
    assert default_cache_dir() == Path("/tmp/from-env")
    assert QueryCache("/explicit/dir").cache_dir == Path("/explicit/dir")


# --- module boundary -------------------------------------------------------------


def test_catalog_package_imports_no_gtk_and_no_link():
    code = (
        "import sys;"
        "import seestar_controller.catalog;"
        "assert 'gi' not in sys.modules, 'GTK imported';"
        "assert not any(m == 'seestar_controller.link' or m.startswith("
        "'seestar_controller.link.') for m in sys.modules), 'link imported';"
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


def test_public_api_is_exported_from_package():
    import seestar_controller.catalog as catalog

    for name in catalog.__all__:
        assert hasattr(catalog, name), name
    assert callable(catalog.resolve_name_cached)
    assert callable(catalog.cache_key)
    assert callable(catalog.default_cache_dir)
    assert isinstance(catalog.QueryCache, type)
    assert catalog.DEFAULT_TTL_S == 24 * 60 * 60.0
