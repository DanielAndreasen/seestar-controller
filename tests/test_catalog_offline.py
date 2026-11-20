"""Offline and error behavior of the catalog facade — mocked HTTP, no real network.

The HTTP boundaries are :func:`seestar_controller.catalog.simbad.fetch_tap_json`
and :func:`seestar_controller.catalog.standards.fetch_vizier_tap_json`
(monkeypatched, same pattern as tests/test_catalog_names.py and
tests/test_catalog_standards.py); every cache lives in a ``tmp_path`` dir with
an injected fake clock, so TTL expiry is tested without sleeping.

"Network down" means the boundary raises — a raw :class:`urllib.error.URLError`
(as the OS would), a raw :class:`TimeoutError`, or the taxonomy's unavailable
errors. The facade (:func:`~seestar_controller.catalog.lookup_name` /
:func:`~seestar_controller.catalog.lookup_standards`) must then serve the
cache (fresh or stale) or raise a clean, actionable
:class:`~seestar_controller.catalog.CatalogError` — never a raw traceback.
"""

import json
import subprocess
import sys
import urllib.error
from types import SimpleNamespace

import pytest

from seestar_controller.catalog import (
    SIMBAD_TAP_SYNC_URL,
    VIZIER_TAP_SYNC_URL,
    Catalog,
    CatalogCandidate,
    CatalogError,
    CatalogUnavailableError,
    J2000Coord,
    NameNotFoundError,
    QueryCache,
    SimbadResponseError,
    SimbadUnavailableError,
    StandardStar,
    StandardsResponseError,
    StandardsUnavailableError,
    find_standard_stars_cached,
    lookup_name,
    lookup_standards,
    resolve_name,
    resolve_name_cached,
)
from seestar_controller.catalog import simbad, standards

#: Column order of the mocked SIMBAD TAP JSON payloads (mirrors build_lookup_query).
SIMBAD_COLUMNS = ["oid", "main_id", "ra", "dec", "otype", "id", "filter", "flux"]

#: M 42 (Orion Nebula): RA 05h35m17.3s = 83.82208 deg, Dec -05d23m28s = -5.39114 deg.
M42_ROW = [1001, "M 42", 83.82208, -5.39114, "HII", "M 42", "V", 4.0]

#: Column order of the mocked VizieR TAP JSON payloads (mirrors build_cone_query).
VIZIER_COLUMNS = ["RAJ2000", "DEJ2000", "Vmag"]

#: Target: M 42 — RA 05h35m17.3s = 83.82208 deg, Dec -05d23m28s = -5.39114 deg.
TARGET = J2000Coord.from_degrees(83.82208, -5.39114)
RADIUS = 0.5

#: Mocked APASS rows [RAJ2000, DEJ2000, Vmag] around the target (all inside).
STAR_01 = [83.82208, -5.29114, 9.5]  # 0.10 deg north
STAR_02 = [84.32208, -5.39114, 11.0]  # ~0.498 deg east
STAR_04 = [83.82208, -5.19114, 10.2]  # 0.20 deg north

#: Fixed epoch the fake clock starts at.
T0 = 1_700_000_000.0

#: A raw network error, as the OS/urllib would raise it (NOT pre-translated).
NETWORK_DOWN = urllib.error.URLError("no route to host")


def simbad_payload(rows, columns=SIMBAD_COLUMNS):
    """Build a SIMBAD TAP JSON body: ``metadata`` column names + ``data`` rows."""
    return {
        "metadata": [{"name": c, "datatype": "char"} for c in columns],
        "data": rows,
    }


def vizier_payload(rows, columns=VIZIER_COLUMNS):
    """Build a VizieR TAP JSON body: ``metadata`` column names + ``data`` rows."""
    return {
        "metadata": [{"name": c, "datatype": "double"} for c in columns],
        "data": rows,
    }


@pytest.fixture()
def fake_simbad(monkeypatch):
    """Monkeypatch the single SIMBAD fetch function; queue payloads, record calls."""
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
def fake_vizier(monkeypatch):
    """Monkeypatch the single VizieR fetch function; queue payloads, record calls."""
    calls = []
    responses = []

    def fake_fetch(query, *, maxrec, timeout, url=VIZIER_TAP_SYNC_URL):
        calls.append({"query": query, "maxrec": maxrec, "timeout": timeout, "url": url})
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(standards, "fetch_vizier_tap_json", fake_fetch)
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


# --- lookup_name: offline hits ---------------------------------------------------


def test_lookup_name_fresh_cache_hit_served_offline_without_fetch(fake_simbad, cache):
    fake_simbad.responses.append(simbad_payload([M42_ROW]))
    first = lookup_name("M 42", cache)
    assert len(fake_simbad.calls) == 1
    fake_simbad.responses.append(NETWORK_DOWN)  # network is now down
    second = lookup_name("M 42", cache)
    assert second == first  # offline hit: served from disk
    assert len(fake_simbad.calls) == 1  # no fetch attempted at all
    assert second[0] == CatalogCandidate(
        name="M 42", ra_deg=83.82208, dec_deg=-5.39114, magnitude=4.0, otype="HII", matched_id="M 42"
    )


def test_lookup_name_expired_entry_served_stale_offline(fake_simbad, cache, clock):
    fake_simbad.responses.append(simbad_payload([M42_ROW]))
    first = lookup_name("M 42", cache)
    clock[0] += 61.0  # past the 60 s TTL -> the entry is expired
    fake_simbad.responses.append(NETWORK_DOWN)
    second = lookup_name("M 42", cache)
    assert second == first  # stale entry served, not an error (graceful degradation)
    assert len(fake_simbad.calls) == 2  # the fetch was attempted and failed
    # the stale entry file is still on disk, untouched (served, not discarded)
    assert len(cache_files(cache)) == 1
    entry = json.loads(cache_files(cache)[0].read_text(encoding="utf-8"))
    assert entry["fetched_at"] == T0


def test_lookup_name_not_found_is_cached_and_raised_offline(fake_simbad, cache):
    fake_simbad.responses.append(simbad_payload([]))
    with pytest.raises(NameNotFoundError, match="no object named 'M 99'"):
        lookup_name("M 99", cache)
    fake_simbad.responses.append(NETWORK_DOWN)
    with pytest.raises(NameNotFoundError, match="no object named 'M 99'"):
        lookup_name("M 99", cache)  # the cached empty result, no fetch
    assert len(fake_simbad.calls) == 1


def test_lookup_name_shares_cache_entries_with_resolve_name_cached(fake_simbad, cache):
    fake_simbad.responses.append(simbad_payload([M42_ROW]))
    via_facade = lookup_name("M 42", cache)
    fake_simbad.responses.append(NETWORK_DOWN)
    via_cached = resolve_name_cached("M 42", cache)  # same cache key -> served from disk
    assert via_cached == via_facade
    assert len(fake_simbad.calls) == 1


# --- lookup_name: no cache + network down -> actionable error ---------------------


def test_lookup_name_no_cache_network_down_raises_actionable_error(fake_simbad, cache):
    fake_simbad.responses.append(NETWORK_DOWN)  # raw URLError, not pre-translated
    with pytest.raises(CatalogUnavailableError) as excinfo:
        lookup_name("M 42", cache)
    exc = excinfo.value
    assert isinstance(exc, CatalogError)
    assert not isinstance(exc, urllib.error.URLError)  # never a raw URLError
    message = str(exc)
    assert "M 42" in message  # names the query
    assert "no cached result" in message
    assert "Retry" in message and "offline cache" in message  # actionable suggestion
    assert str(cache.cache_dir) in message  # names the cache location
    assert "no route to host" in message  # keeps the underlying cause
    assert cache_files(cache) == []  # nothing cached


def test_lookup_name_simbad_unavailable_without_cache_is_translated(fake_simbad, cache):
    fake_simbad.responses.append(SimbadUnavailableError("could not reach SIMBAD at ..."))
    with pytest.raises(CatalogUnavailableError, match="could not reach SIMBAD"):
        lookup_name("M 42", cache)


def test_lookup_name_timeout_without_cache_raises_clean_error(fake_simbad, cache):
    fake_simbad.responses.append(TimeoutError("timed out"))
    with pytest.raises(CatalogUnavailableError) as excinfo:
        lookup_name("M 42", cache)
    assert not isinstance(excinfo.value, TimeoutError)  # not a raw timeout
    assert isinstance(excinfo.value, CatalogError)
    assert "M 42" in str(excinfo.value)


def test_lookup_name_unexpected_fetch_error_is_translated_not_raw(fake_simbad, cache):
    fake_simbad.responses.append(RuntimeError("boom"))
    with pytest.raises(CatalogUnavailableError) as excinfo:
        lookup_name("M 42", cache)
    assert not isinstance(excinfo.value, RuntimeError)
    assert "boom" in str(excinfo.value)


# --- lookup_name: timeout / malformed with and without cache ----------------------


def test_lookup_name_timeout_with_expired_cache_serves_stale(fake_simbad, cache, clock):
    fake_simbad.responses.append(simbad_payload([M42_ROW]))
    first = lookup_name("M 42", cache)
    clock[0] += 61.0  # expired -> a fetch is attempted
    fake_simbad.responses.append(TimeoutError("timed out"))
    assert lookup_name("M 42", cache) == first  # stale served on timeout too
    assert len(fake_simbad.calls) == 2


def test_lookup_name_malformed_response_with_cache_serves_stale(fake_simbad, cache, clock):
    fake_simbad.responses.append(simbad_payload([M42_ROW]))
    first = lookup_name("M 42", cache)
    clock[0] += 61.0
    # malformed payload: parse_tap_response raises SimbadResponseError inside the fetch
    fake_simbad.responses.append({"metadata": [], "data": []})
    assert lookup_name("M 42", cache) == first
    assert len(fake_simbad.calls) == 2


def test_lookup_name_response_error_with_cache_serves_stale(fake_simbad, cache, clock):
    fake_simbad.responses.append(simbad_payload([M42_ROW]))
    first = lookup_name("M 42", cache)
    clock[0] += 61.0
    fake_simbad.responses.append(SimbadResponseError("SIMBAD response is not valid JSON"))
    assert lookup_name("M 42", cache) == first


def test_lookup_name_malformed_response_without_cache_raises_clean_error(fake_simbad, cache):
    fake_simbad.responses.append({"metadata": [], "data": []})
    with pytest.raises(SimbadResponseError) as excinfo:
        lookup_name("M 42", cache)
    assert isinstance(excinfo.value, CatalogError)  # in the taxonomy, not a raw traceback
    assert "metadata" in str(excinfo.value)
    assert cache_files(cache) == []  # a malformed response is never cached


def test_lookup_name_validation_rejected_before_fetch_or_cache(fake_simbad, cache):
    with pytest.raises(ValueError, match="empty"):
        lookup_name("   ", cache)
    with pytest.raises(ValueError, match="string"):
        lookup_name(None, cache)
    with pytest.raises(ValueError, match=">= 1"):
        lookup_name("M 42", cache, maxrec=0)
    assert fake_simbad.calls == []
    assert cache_files(cache) == []


def test_raw_resolve_name_stays_uncached_and_untranslated(fake_simbad, cache):
    fake_simbad.responses.append(SimbadUnavailableError("offline"))
    with pytest.raises(SimbadUnavailableError):  # the raw path does not translate
        resolve_name("M 42")
    assert cache_files(cache) == []


# --- lookup_standards: offline hits ------------------------------------------------


def test_lookup_standards_fresh_cache_hit_served_offline_without_fetch(fake_vizier, cache):
    fake_vizier.responses.append(vizier_payload([STAR_01, STAR_02, STAR_04]))
    first = lookup_standards(TARGET, RADIUS, cache)
    assert len(fake_vizier.calls) == 1
    fake_vizier.responses.append(NETWORK_DOWN)  # network is now down
    second = lookup_standards(TARGET, RADIUS, cache)
    assert second == first
    assert len(fake_vizier.calls) == 1  # no fetch attempted at all
    assert all(isinstance(s, StandardStar) for s in second)
    assert [s.magnitude for s in second] == [9.5, 10.2, 11.0]


def test_lookup_standards_expired_entry_served_stale_offline(fake_vizier, cache, clock):
    fake_vizier.responses.append(vizier_payload([STAR_01]))
    first = lookup_standards(TARGET, RADIUS, cache)
    clock[0] += 61.0  # past the 60 s TTL -> expired
    fake_vizier.responses.append(NETWORK_DOWN)
    second = lookup_standards(TARGET, RADIUS, cache)
    assert second == first  # stale entry served, not an error
    assert len(fake_vizier.calls) == 2  # the fetch was attempted and failed
    assert len(cache_files(cache)) == 1  # the stale file is kept


def test_lookup_standards_empty_result_is_cached_and_returned_offline(fake_vizier, cache):
    fake_vizier.responses.append(vizier_payload([]))
    assert lookup_standards(TARGET, RADIUS, cache) == []
    fake_vizier.responses.append(NETWORK_DOWN)
    assert lookup_standards(TARGET, RADIUS, cache) == []  # no raise, no fetch
    assert len(fake_vizier.calls) == 1


def test_lookup_standards_target_mag_resorts_offline_without_refetch(fake_vizier, cache):
    fake_vizier.responses.append(vizier_payload([STAR_01, STAR_02, STAR_04]))
    brightest = lookup_standards(TARGET, RADIUS, cache)
    assert [s.magnitude for s in brightest] == [9.5, 10.2, 11.0]
    # target_mag is not part of the cache key: same cone, re-sorted, offline
    fake_vizier.responses.append(NETWORK_DOWN)
    closest = lookup_standards(TARGET, RADIUS, cache, target_mag=10.0)
    assert [s.magnitude for s in closest] == [10.2, 9.5, 11.0]
    assert len(fake_vizier.calls) == 1


def test_lookup_standards_shares_cache_entries_with_find_standard_stars_cached(fake_vizier, cache):
    fake_vizier.responses.append(vizier_payload([STAR_01]))
    via_facade = lookup_standards(TARGET, RADIUS, cache)
    fake_vizier.responses.append(NETWORK_DOWN)
    via_cached = find_standard_stars_cached(TARGET, RADIUS, cache)  # same key -> disk
    assert via_cached == via_facade
    assert len(fake_vizier.calls) == 1


# --- lookup_standards: no cache + network down -> actionable error -----------------


def test_lookup_standards_no_cache_network_down_raises_actionable_error(fake_vizier, cache):
    fake_vizier.responses.append(NETWORK_DOWN)  # raw URLError
    with pytest.raises(CatalogUnavailableError) as excinfo:
        lookup_standards(TARGET, RADIUS, cache)
    exc = excinfo.value
    assert isinstance(exc, CatalogError)
    assert not isinstance(exc, urllib.error.URLError)  # never a raw URLError
    message = str(exc)
    assert "0.5" in message and "deg" in message  # names the query (the cone)
    assert "no cached result" in message
    assert "Retry" in message and "offline cache" in message
    assert str(cache.cache_dir) in message
    assert cache_files(cache) == []


def test_lookup_standards_standards_unavailable_without_cache_is_translated(fake_vizier, cache):
    fake_vizier.responses.append(StandardsUnavailableError("could not reach VizieR at ..."))
    with pytest.raises(CatalogUnavailableError, match="could not reach VizieR"):
        lookup_standards(TARGET, RADIUS, cache)


def test_lookup_standards_timeout_without_cache_raises_clean_error(fake_vizier, cache):
    fake_vizier.responses.append(TimeoutError("timed out"))
    with pytest.raises(CatalogUnavailableError) as excinfo:
        lookup_standards(TARGET, RADIUS, cache)
    assert not isinstance(excinfo.value, TimeoutError)
    assert isinstance(excinfo.value, CatalogError)


# --- lookup_standards: timeout / malformed with and without cache -------------------


def test_lookup_standards_timeout_with_expired_cache_serves_stale(fake_vizier, cache, clock):
    fake_vizier.responses.append(vizier_payload([STAR_01]))
    first = lookup_standards(TARGET, RADIUS, cache)
    clock[0] += 61.0
    fake_vizier.responses.append(TimeoutError("timed out"))
    assert lookup_standards(TARGET, RADIUS, cache) == first
    assert len(fake_vizier.calls) == 2


def test_lookup_standards_malformed_response_with_cache_serves_stale(fake_vizier, cache, clock):
    fake_vizier.responses.append(vizier_payload([STAR_01]))
    first = lookup_standards(TARGET, RADIUS, cache)
    clock[0] += 61.0
    fake_vizier.responses.append({"metadata": [], "data": []})  # -> StandardsResponseError
    assert lookup_standards(TARGET, RADIUS, cache) == first
    assert len(fake_vizier.calls) == 2


def test_lookup_standards_malformed_response_without_cache_raises_clean_error(fake_vizier, cache):
    fake_vizier.responses.append({"metadata": [], "data": []})
    with pytest.raises(StandardsResponseError) as excinfo:
        lookup_standards(TARGET, RADIUS, cache)
    assert isinstance(excinfo.value, CatalogError)
    assert "metadata" in str(excinfo.value)
    assert cache_files(cache) == []  # never cached


def test_lookup_standards_validation_rejected_before_fetch_or_cache(fake_vizier, cache):
    with pytest.raises(ValueError, match="radius_deg"):
        lookup_standards(TARGET, 0, cache)
    with pytest.raises(ValueError, match="band"):
        lookup_standards(TARGET, RADIUS, cache, band="K")
    with pytest.raises(ValueError, match="maxrec"):
        lookup_standards(TARGET, RADIUS, cache, maxrec=0)
    assert fake_vizier.calls == []
    assert cache_files(cache) == []


# --- the Catalog facade -------------------------------------------------------------


def test_catalog_facade_holds_one_cache_and_degrades_offline(fake_simbad, fake_vizier, cache):
    catalog = Catalog(cache)
    assert catalog.cache is cache
    fake_simbad.responses.append(simbad_payload([M42_ROW]))
    fake_vizier.responses.append(vizier_payload([STAR_01]))
    candidates = catalog.lookup_name("M 42")
    stars = catalog.lookup_standards(TARGET, RADIUS)
    # network down on both services
    fake_simbad.responses.append(NETWORK_DOWN)
    fake_vizier.responses.append(StandardsUnavailableError("offline"))
    assert catalog.lookup_name("M 42") == candidates
    assert catalog.lookup_standards(TARGET, RADIUS) == stars
    assert len(fake_simbad.calls) == 1 and len(fake_vizier.calls) == 1  # no refetch


def test_catalog_facade_default_cache_is_a_query_cache():
    assert isinstance(Catalog().cache, QueryCache)


# --- module boundary -----------------------------------------------------------------


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
    assert issubclass(catalog.CatalogUnavailableError, catalog.CatalogError)
    assert callable(catalog.lookup_name)
    assert callable(catalog.lookup_standards)
    assert isinstance(catalog.Catalog, type)
