"""Standard-star lookup near a target with mocked HTTP — no real network.

The HTTP boundary is
:func:`seestar_controller.catalog.standards.fetch_vizier_tap_json`; the lookup
tests monkeypatch that single function, and the fetch tests monkeypatch
``urllib.request.urlopen`` to cover the HTTP layer itself (same pattern as
tests/test_catalog_names.py). The cached variant is tested with a QueryCache
in a ``tmp_path`` dir, like tests/test_catalog_cache.py.
"""

import json
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from types import SimpleNamespace

import pytest

from seestar_controller.catalog import (
    VIZIER_TAP_SYNC_URL,
    CatalogError,
    J2000Coord,
    QueryCache,
    StandardStar,
    StandardsResponseError,
    StandardsUnavailableError,
    angular_distance,
    find_standard_stars,
    find_standard_stars_cached,
)
from seestar_controller.catalog import standards

#: Column order of the mocked TAP JSON payloads (mirrors build_cone_query).
COLUMNS = ["RAJ2000", "DEJ2000", "Vmag"]

#: Target: M 42 — RA 05h35m17.3s = 83.82208 deg, Dec -05d23m28s = -5.39114 deg.
TARGET = J2000Coord.from_degrees(83.82208, -5.39114)
RADIUS = 0.5

#: Mocked APASS rows [RAJ2000, DEJ2000, Vmag] around the target.
STAR_01 = [83.82208, -5.29114, 9.5]  # 0.10 deg north -> inside
STAR_02 = [84.32208, -5.39114, 11.0]  # ~0.498 deg east -> inside
STAR_03 = [83.82208, -4.88114, 8.0]  # 0.51 deg north -> just OUTSIDE the radius
STAR_04 = [83.82208, -5.19114, 10.2]  # 0.20 deg north -> inside
NO_MAG = [83.92208, -5.39114, None]  # ~0.100 deg east -> inside, no magnitude

#: Fixed epoch the fake clock starts at.
T0 = 1_700_000_000.0


def tap_payload(rows, columns=COLUMNS):
    """Build a VizieR TAP JSON body: ``metadata`` column names + ``data`` rows."""
    return {
        "metadata": [{"name": c, "datatype": "double"} for c in columns],
        "data": rows,
    }


@pytest.fixture()
def fake_vizier(monkeypatch):
    """Monkeypatch the single fetch function; queue payloads, record calls."""
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


# --- cone-search query construction ----------------------------------------------


def test_cone_query_is_built_and_sent_to_vizier_tap(fake_vizier):
    fake_vizier.responses.append(tap_payload([STAR_01]))
    find_standard_stars(TARGET, RADIUS)
    assert len(fake_vizier.calls) == 1
    call = fake_vizier.calls[0]
    assert call["url"] == VIZIER_TAP_SYNC_URL
    assert call["maxrec"] == standards.DEFAULT_MAX_STANDARDS
    assert call["timeout"] == standards.DEFAULT_TIMEOUT_S
    query = call["query"]
    assert 'FROM "II/336/apass9"' in query
    assert "SELECT RAJ2000, DEJ2000, Vmag" in query
    assert "CONTAINS(POINT('ICRS', RAJ2000, DEJ2000)" in query
    assert "CIRCLE('ICRS', 83.82208, -5.39114, 0.5)" in query
    assert "ORDER BY Vmag" in query


def test_build_cone_query_band_and_radius(fake_vizier):
    query = standards.build_cone_query(TARGET, 0.25, band="B")
    assert "SELECT RAJ2000, DEJ2000, Bmag" in query
    assert "CIRCLE('ICRS', 83.82208, -5.39114, 0.25)" in query
    assert "ORDER BY Bmag" in query
    with pytest.raises(ValueError, match="band"):
        standards.build_cone_query(TARGET, 0.25, band="K")


def test_lookup_forwards_maxrec_timeout_and_band(fake_vizier):
    fake_vizier.responses.append(
        tap_payload([STAR_01], columns=["RAJ2000", "DEJ2000", "Bmag"])
    )
    find_standard_stars(TARGET, RADIUS, band="B", maxrec=7, timeout=3.0)
    call = fake_vizier.calls[0]
    assert call["maxrec"] == 7
    assert call["timeout"] == 3.0
    assert "Bmag" in call["query"]


def test_coord_accepted_as_pair_and_duck_typed(fake_vizier):
    fake_vizier.responses.append(tap_payload([STAR_01]))
    find_standard_stars((83.82208, -5.39114), RADIUS)
    assert "CIRCLE('ICRS', 83.82208, -5.39114, 0.5)" in fake_vizier.calls[0]["query"]
    fake_vizier.responses.append(tap_payload([STAR_01]))
    find_standard_stars(SimpleNamespace(ra_deg=83.82208, dec_deg=-5.39114), RADIUS)
    assert "CIRCLE('ICRS', 83.82208, -5.39114, 0.5)" in fake_vizier.calls[1]["query"]


def test_invalid_coord_rejected_without_fetch(fake_vizier):
    with pytest.raises(ValueError):
        find_standard_stars("M 42", RADIUS)
    with pytest.raises(ValueError):
        find_standard_stars((83.8,), RADIUS)
    assert fake_vizier.calls == []


# --- parsing rows to StandardStar with distance ------------------------------------


def test_rows_parse_to_standard_stars_with_distance(fake_vizier):
    fake_vizier.responses.append(tap_payload([STAR_01, STAR_02, STAR_04]))
    stars = find_standard_stars(TARGET, RADIUS)
    assert len(stars) == 3
    assert all(isinstance(s, StandardStar) for s in stars)
    by_mag = {s.magnitude: s for s in stars}
    s1 = by_mag[9.5]
    assert s1.name == "APASS J83.82208-05.29114"
    assert isinstance(s1.coord, J2000Coord)
    assert s1.ra_deg == pytest.approx(83.82208)
    assert s1.dec_deg == pytest.approx(-5.29114)
    assert s1.coords_j2000 == (s1.ra_deg, s1.dec_deg)
    assert s1.ra_hours == pytest.approx(83.82208 / 15.0)
    assert s1.band == "V"
    # distance comes from coords.angular_distance, not a reimplementation
    assert s1.distance_deg == pytest.approx(angular_distance(TARGET, s1.coord))
    assert s1.distance_deg == pytest.approx(0.1, abs=1e-9)
    assert by_mag[11.0].distance_deg == pytest.approx(0.4978, abs=1e-3)
    assert by_mag[10.2].distance_deg == pytest.approx(0.2, abs=1e-9)


def test_string_numbers_in_rows_are_accepted(fake_vizier):
    row = ["83.82208", "-5.29114", "9.5"]
    fake_vizier.responses.append(tap_payload([row]))
    star = find_standard_stars(TARGET, RADIUS)[0]
    assert star.ra_deg == pytest.approx(83.82208)
    assert star.magnitude == pytest.approx(9.5)


def test_distance_is_correct_across_the_ra_0_seam(fake_vizier):
    seam_target = J2000Coord.from_degrees(359.95, 0.0)
    fake_vizier.responses.append(tap_payload([[0.05, 0.0, 12.0]]))
    stars = find_standard_stars(seam_target, 0.5)
    assert "CIRCLE('ICRS', 359.95, 0.0, 0.5)" in fake_vizier.calls[0]["query"]
    assert len(stars) == 1
    assert stars[0].distance_deg == pytest.approx(0.1, abs=1e-9)


# --- radius filter ------------------------------------------------------------------


def test_star_just_outside_radius_is_excluded(fake_vizier):
    fake_vizier.responses.append(tap_payload([STAR_01, STAR_03]))
    stars = find_standard_stars(TARGET, RADIUS)
    # STAR_03 sits 0.51 deg away — outside the 0.5 deg cone, even though the
    # server-side CIRCLE would be the first pass, the client filter drops it
    assert angular_distance(TARGET, (STAR_03[0], STAR_03[1])) == pytest.approx(0.51)
    assert [s.magnitude for s in stars] == [9.5]


def test_radius_boundary_is_inclusive(fake_vizier):
    # a star 0.49 deg away is inside a 0.5 deg radius
    fake_vizier.responses.append(tap_payload([[83.82208, -4.90114, 7.7]]))
    stars = find_standard_stars(TARGET, RADIUS)
    assert [s.magnitude for s in stars] == [7.7]


# --- sorting -------------------------------------------------------------------------


def test_sorted_brightest_first_by_default(fake_vizier):
    fake_vizier.responses.append(tap_payload([STAR_02, STAR_01, NO_MAG, STAR_04]))
    stars = find_standard_stars(TARGET, RADIUS)
    assert [s.magnitude for s in stars] == [9.5, 10.2, 11.0, None]  # unknown mag last


def test_sorted_by_magnitude_closeness_when_target_mag_given(fake_vizier):
    fake_vizier.responses.append(tap_payload([STAR_02, STAR_01, NO_MAG, STAR_04]))
    stars = find_standard_stars(TARGET, RADIUS, target_mag=10.0)
    # |mag - 10|: 10.2 -> 0.2, 9.5 -> 0.5, 11.0 -> 1.0, None -> last
    assert [s.magnitude for s in stars] == [10.2, 9.5, 11.0, None]


def test_target_mag_closeness_ordering(fake_vizier):
    fake_vizier.responses.append(tap_payload([STAR_01, STAR_04]))  # mags 9.5 and 10.2
    stars = find_standard_stars(TARGET, RADIUS, target_mag=9.75)
    # |9.5 - 9.75| = 0.25 < |10.2 - 9.75| = 0.45
    assert [s.magnitude for s in stars] == [9.5, 10.2]


# --- magnitude range filter ------------------------------------------------------------


def test_mag_range_filters_and_drops_unknown_magnitude(fake_vizier):
    fake_vizier.responses.append(tap_payload([STAR_01, STAR_02, NO_MAG, STAR_04]))
    stars = find_standard_stars(TARGET, RADIUS, mag_range=(9.0, 10.5))
    assert [s.magnitude for s in stars] == [9.5, 10.2]  # 11.0 out of range, None dropped


def test_mag_range_upper_half(fake_vizier):
    fake_vizier.responses.append(tap_payload([STAR_01, STAR_02, STAR_04]))
    stars = find_standard_stars(TARGET, RADIUS, mag_range=(10.0, 12.0))
    assert [s.magnitude for s in stars] == [10.2, 11.0]


def test_mag_range_combined_with_target_mag_sort(fake_vizier):
    fake_vizier.responses.append(tap_payload([STAR_01, STAR_02, STAR_04]))
    stars = find_standard_stars(TARGET, RADIUS, target_mag=11.0, mag_range=(9.0, 10.5))
    assert [s.magnitude for s in stars] == [10.2, 9.5]  # 11.0 filtered out, then sorted


# --- empty result is normal --------------------------------------------------------------


def test_empty_result_returns_empty_list(fake_vizier):
    fake_vizier.responses.append(tap_payload([]))
    stars = find_standard_stars(TARGET, RADIUS)
    assert stars == []
    assert isinstance(stars, list)


# --- input validation ---------------------------------------------------------------------


@pytest.mark.parametrize("bad", [0, -0.1, -1, float("nan"), float("inf"), 180.5, "0.5", True, None])
def test_invalid_radius_rejected_without_fetch(fake_vizier, bad):
    with pytest.raises(ValueError, match="radius_deg"):
        find_standard_stars(TARGET, bad)
    assert fake_vizier.calls == []


def test_invalid_maxrec_band_target_mag_mag_range_rejected(fake_vizier):
    with pytest.raises(ValueError, match="maxrec"):
        find_standard_stars(TARGET, RADIUS, maxrec=0)
    with pytest.raises(ValueError, match="band"):
        find_standard_stars(TARGET, RADIUS, band="K")
    with pytest.raises(ValueError, match="target_mag"):
        find_standard_stars(TARGET, RADIUS, target_mag="bright")
    with pytest.raises(ValueError, match="target_mag"):
        find_standard_stars(TARGET, RADIUS, target_mag=float("nan"))
    with pytest.raises(ValueError, match="mag_range"):
        find_standard_stars(TARGET, RADIUS, mag_range=(10.0, 5.0))
    with pytest.raises(ValueError, match="mag_range"):
        find_standard_stars(TARGET, RADIUS, mag_range=(1.0, 2.0, 3.0))
    with pytest.raises(ValueError, match="mag_range"):
        find_standard_stars(TARGET, RADIUS, mag_range=("a", 5.0))
    assert fake_vizier.calls == []


# --- malformed responses --------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {},  # no metadata, no data
        {"metadata": [{"name": "RAJ2000"}], "data": []},  # missing DEJ2000/Vmag columns
        {"metadata": [{"name": c} for c in COLUMNS]},  # no data key
        {"metadata": [{"name": c} for c in COLUMNS], "data": "nope"},  # data not a list
        {"metadata": [{"name": c} for c in COLUMNS], "data": ["nope"]},  # row not a list
        {"metadata": [{"name": c} for c in COLUMNS], "data": [["abc", -5.4, 9.5]]},  # bad ra
        {"metadata": [{"name": c} for c in COLUMNS], "data": [[None, -5.4, 9.5]]},  # no ra
        {"metadata": [{"name": c} for c in COLUMNS], "data": [[370.0, -5.4, 9.5]]},  # ra out of range
        {"metadata": [{"name": c} for c in COLUMNS], "data": [[83.8, -95.0, 9.5]]},  # dec out of range
        {"metadata": [{"name": c} for c in COLUMNS], "data": [[83.8, -5.4, "bright"]]},  # bad mag
    ],
)
def test_malformed_responses_raise_response_error(fake_vizier, payload):
    fake_vizier.responses.append(payload)
    with pytest.raises(StandardsResponseError) as excinfo:
        find_standard_stars(TARGET, RADIUS)
    assert isinstance(excinfo.value, CatalogError)


def test_parse_cone_response_rejects_non_object():
    with pytest.raises(StandardsResponseError, match="not a JSON object"):
        standards.parse_cone_response([1, 2, 3], target=TARGET, radius_deg=RADIUS)


def test_fetch_rejects_non_object_json_body(monkeypatch):
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda req, timeout: _FakeResponse(b"[1, 2]")
    )
    with pytest.raises(StandardsResponseError, match="not a JSON object"):
        standards.fetch_vizier_tap_json("SELECT 1")


def test_fetch_error_propagates_through_lookup(fake_vizier):
    fake_vizier.responses.append(StandardsUnavailableError("could not reach VizieR"))
    with pytest.raises(StandardsUnavailableError) as excinfo:
        find_standard_stars(TARGET, RADIUS)
    assert isinstance(excinfo.value, CatalogError)


# --- the HTTP layer itself (urlopen mocked) -------------------------------------------------


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_fetch_posts_tap_form_and_parses_json(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return _FakeResponse(json.dumps(tap_payload([STAR_01])).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    payload = standards.fetch_vizier_tap_json("SELECT ...", maxrec=7, timeout=2.5)

    request = captured["request"]
    assert request.full_url == VIZIER_TAP_SYNC_URL
    assert request.get_method() == "POST"
    assert request.headers["Accept"] == "application/json"
    assert captured["timeout"] == 2.5
    form = urllib.parse.parse_qs(request.data.decode("ascii"))
    assert form["REQUEST"] == ["doQuery"]
    assert form["LANG"] == ["ADQL"]
    assert form["QUERY"] == ["SELECT ..."]
    assert form["MAXREC"] == ["7"]
    assert form["RESPONSEFORMAT"] == ["application/json"]
    assert payload["data"] == [STAR_01]


def test_fetch_http_error_raises_unavailable(monkeypatch):
    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(
            VIZIER_TAP_SYNC_URL, 503, "Service Unavailable", None, None
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(StandardsUnavailableError, match="HTTP 503"):
        standards.fetch_vizier_tap_json("SELECT ...")


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.URLError("no route to host"),
        TimeoutError("timed out"),
        OSError("network unreachable"),
    ],
)
def test_fetch_network_failure_raises_unavailable(monkeypatch, error):
    def fake_urlopen(request, timeout):
        raise error

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(StandardsUnavailableError, match="could not reach VizieR"):
        standards.fetch_vizier_tap_json("SELECT ...")


def test_fetch_invalid_json_body_raises_response_error(monkeypatch):
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda req, timeout: _FakeResponse(b"<html>oops")
    )
    with pytest.raises(StandardsResponseError, match="not valid JSON"):
        standards.fetch_vizier_tap_json("SELECT ...")


# --- cached variant ---------------------------------------------------------------------------


def test_cached_second_call_served_from_cache_with_fetch_raising(fake_vizier, cache):
    fake_vizier.responses.append(tap_payload([STAR_01, STAR_02, STAR_04]))
    first = find_standard_stars_cached(TARGET, RADIUS, cache)
    # network is now down: any fetch would raise
    fake_vizier.responses.append(StandardsUnavailableError("could not reach VizieR"))
    second = find_standard_stars_cached(TARGET, RADIUS, cache)
    assert second == first
    assert len(fake_vizier.calls) == 1  # no second fetch — served from disk
    assert all(isinstance(s, StandardStar) for s in second)
    assert second[0].coord == first[0].coord


def test_cached_entry_stores_flat_star_rows(fake_vizier, cache, clock):
    fake_vizier.responses.append(tap_payload([STAR_01]))
    find_standard_stars_cached(TARGET, RADIUS, cache)
    files = cache_files(cache)
    assert len(files) == 1
    entry = json.loads(files[0].read_text(encoding="utf-8"))
    assert set(entry) == {"query", "fetched_at", "ttl", "payload"}
    assert entry["fetched_at"] == T0
    assert entry["ttl"] == 60.0
    # the entry's query is the JSON cache key wrapping the ADQL cone search
    inner = json.loads(entry["query"])
    assert inner["maxrec"] == standards.DEFAULT_MAX_STANDARDS
    assert 'FROM "II/336/apass9"' in inner["query"]
    assert "CIRCLE('ICRS', 83.82208, -5.39114, 0.5)" in inner["query"]
    assert entry["payload"] == [
        {
            "name": "APASS J83.82208-05.29114",
            "ra_deg": 83.82208,
            "dec_deg": -5.29114,
            "magnitude": 9.5,
            "band": "V",
            "distance_deg": pytest.approx(0.1),
        }
    ]


def test_raw_find_standard_stars_stays_uncached(fake_vizier, cache):
    fake_vizier.responses.extend([tap_payload([STAR_01]), tap_payload([STAR_01])])
    find_standard_stars(TARGET, RADIUS)
    find_standard_stars(TARGET, RADIUS)
    assert len(fake_vizier.calls) == 2  # the raw query never touches the cache
    assert cache_files(cache) == []


def test_cached_target_mag_resorts_without_refetch(fake_vizier, cache):
    fake_vizier.responses.append(tap_payload([STAR_01, STAR_02, STAR_04]))
    brightest = find_standard_stars_cached(TARGET, RADIUS, cache)
    assert [s.magnitude for s in brightest] == [9.5, 10.2, 11.0]
    # target_mag is not part of the cache key: same cone, re-sorted, offline
    fake_vizier.responses.append(StandardsUnavailableError("offline"))
    closest = find_standard_stars_cached(TARGET, RADIUS, cache, target_mag=10.0)
    assert [s.magnitude for s in closest] == [10.2, 9.5, 11.0]
    assert len(fake_vizier.calls) == 1


def test_cached_different_radius_or_band_refetches(fake_vizier, cache):
    fake_vizier.responses.append(tap_payload([STAR_01]))
    find_standard_stars_cached(TARGET, RADIUS, cache)
    fake_vizier.responses.append(tap_payload([STAR_01]))
    find_standard_stars_cached(TARGET, 1.0, cache)  # different radius -> different cone
    assert len(fake_vizier.calls) == 2
    assert len(cache_files(cache)) == 2
    fake_vizier.responses.append(
        tap_payload([STAR_01], columns=["RAJ2000", "DEJ2000", "Bmag"])
    )
    find_standard_stars_cached(TARGET, RADIUS, cache, band="B")  # different band -> different cone
    assert len(fake_vizier.calls) == 3
    assert len(cache_files(cache)) == 3


def test_cached_empty_result_is_cached_and_returned_offline(fake_vizier, cache):
    fake_vizier.responses.append(tap_payload([]))
    assert find_standard_stars_cached(TARGET, RADIUS, cache) == []
    assert len(fake_vizier.calls) == 1
    fake_vizier.responses.append(StandardsUnavailableError("offline"))
    assert find_standard_stars_cached(TARGET, RADIUS, cache) == []  # no raise, no fetch
    assert len(fake_vizier.calls) == 1


def test_malformed_response_is_never_cached(fake_vizier, cache):
    fake_vizier.responses.append({"metadata": [], "data": []})  # raises StandardsResponseError
    with pytest.raises(StandardsResponseError):
        find_standard_stars_cached(TARGET, RADIUS, cache)
    assert cache_files(cache) == []  # nothing stored


def test_corrupted_cache_file_falls_back_to_network(fake_vizier, cache):
    fake_vizier.responses.append(tap_payload([STAR_01]))
    find_standard_stars_cached(TARGET, RADIUS, cache)
    cache_files(cache)[0].write_bytes(b"garbage{{{")  # scribble over the entry
    fake_vizier.responses.append(tap_payload([STAR_01, STAR_04]))
    stars = find_standard_stars_cached(TARGET, RADIUS, cache)
    assert len(fake_vizier.calls) == 2  # fell back to the network
    assert [s.magnitude for s in stars] == [9.5, 10.2]
    entry = json.loads(cache_files(cache)[0].read_text(encoding="utf-8"))  # rewritten valid
    assert len(entry["payload"]) == 2


def test_cached_validation_rejects_wrong_shaped_payload(fake_vizier, cache):
    # a valid cache entry whose payload is not a list of star rows -> refetch
    adql = standards.build_cone_query(TARGET, RADIUS)
    cache.set(standards._standards_query(adql, maxrec=standards.DEFAULT_MAX_STANDARDS), 42)
    fake_vizier.responses.append(tap_payload([STAR_01]))
    stars = find_standard_stars_cached(TARGET, RADIUS, cache)
    assert len(fake_vizier.calls) == 1
    assert [s.magnitude for s in stars] == [9.5]


def test_cached_input_validation_rejected_without_fetch_or_cache(fake_vizier, cache):
    with pytest.raises(ValueError, match="radius_deg"):
        find_standard_stars_cached(TARGET, 0, cache)
    with pytest.raises(ValueError, match="maxrec"):
        find_standard_stars_cached(TARGET, RADIUS, cache, maxrec=0)
    with pytest.raises(ValueError, match="band"):
        find_standard_stars_cached(TARGET, RADIUS, cache, band="K")
    assert fake_vizier.calls == []
    assert cache_files(cache) == []


# --- module boundary -------------------------------------------------------------------------


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
    assert callable(catalog.find_standard_stars)
    assert callable(catalog.find_standard_stars_cached)
    assert callable(catalog.build_cone_query)
    assert callable(catalog.fetch_vizier_tap_json)
    assert callable(catalog.parse_cone_response)
    assert isinstance(catalog.StandardStar, type)
    assert issubclass(catalog.StandardsUnavailableError, catalog.CatalogError)
    assert issubclass(catalog.StandardsResponseError, catalog.CatalogError)
    assert catalog.VIZIER_TABLE == "II/336/apass9"
    assert catalog.DEFAULT_BAND == "V"
