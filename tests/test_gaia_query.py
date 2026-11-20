"""Tests for seestar_controller.gaia.query — Gaia DR3 TAP query + disk cache.

No network: the single HTTP boundary ``fetch_gaia_tap_json`` (or
``urllib.request.urlopen``) is monkeypatched everywhere."""

from __future__ import annotations

import email.message
import json
import urllib.error
import urllib.request
from typing import Any, cast

import pytest

from seestar_controller.catalog.cache import QueryCache
from seestar_controller.gaia import query as gq
from seestar_controller.gaia.errors import GaiaResponseError, GaiaUnavailableError

CENTER_RA = 83.633
CENTER_DEC = 22.014
RADIUS_DEG = 0.5

GAIA_METADATA = [
    {"name": name}
    for name in ("RAJ2000", "DEJ2000", "Source", "Gmag", "BPmag", "RPmag", "Plx", "pmRA", "pmDE")
]


def gaia_payload(rows: list[dict]) -> dict:
    """A TAP-JSON payload in the VizieR Gaia DR3 shape.

    Each row dict: source_id, ra, dec, g, bp, rp, plx, pmra, pmde.
    """
    data = [
        [
            row["ra"],
            row["dec"],
            row["source_id"],
            row["g"],
            row["bp"],
            row["rp"],
            row["plx"],
            row["pmra"],
            row["pmde"],
        ]
        for row in rows
    ]
    return {"metadata": GAIA_METADATA, "data": data}


def sample_rows() -> list[dict]:
    return [
        {
            "source_id": 3403768831985762944,
            "ra": 83.61,
            "dec": 21.91,
            "g": 19.82,
            "bp": 20.5,
            "rp": 19.1,
            "plx": 0.47,
            "pmra": 0.56,
            "pmde": -0.11,
        },
        {
            "source_id": 42,
            "ra": 83.70,
            "dec": 22.05,
            "g": 12.5,
            "bp": 13.0,
            "rp": 12.0,
            "plx": None,
            "pmra": None,
            "pmde": None,
        },
    ]


@pytest.fixture()
def cache_dir(tmp_path):
    return tmp_path / "gaia-cache"


@pytest.fixture()
def fetch_calls(monkeypatch):
    """Monkeypatch the single HTTP boundary; returns a call recorder."""
    calls: list[dict] = []

    def fake_fetch(query, *, maxrec=gq.DEFAULT_MAXREC, timeout=gq.DEFAULT_TIMEOUT_S, url=gq.VIZIER_TAP_SYNC_URL):
        calls.append({"query": query, "maxrec": maxrec, "timeout": timeout, "url": url})
        return gaia_payload(sample_rows())

    monkeypatch.setattr(gq, "fetch_gaia_tap_json", fake_fetch)
    return calls


# --- the wire query -----------------------------------------------------------------


def test_build_cone_query_selects_the_gaia_columns_cone_and_g_limit():
    adql = gq.build_cone_query(CENTER_RA, CENTER_DEC, RADIUS_DEG, g_limit=20.0)
    assert '"I/355/gaiadr3"' in adql
    for col in ("RAJ2000", "DEJ2000", "Source", "Gmag", "BPmag", "RPmag", "Plx", "pmRA", "pmDE"):
        assert f'"{col}"' in adql
    assert "CONTAINS(POINT('ICRS'" in adql
    assert f"CIRCLE('ICRS', {CENTER_RA!r}, {CENTER_DEC!r}, {RADIUS_DEG!r})" in adql
    assert '"Gmag" < 20.0' in adql
    assert "ORDER BY" in adql


def test_build_cone_query_validates_inputs():
    with pytest.raises(ValueError):
        gq.build_cone_query(CENTER_RA, CENTER_DEC, 0.0)
    with pytest.raises(ValueError):
        gq.build_cone_query(CENTER_RA, CENTER_DEC, 181.0)
    with pytest.raises(ValueError):
        gq.build_cone_query(CENTER_RA, 91.0, RADIUS_DEG)
    with pytest.raises(ValueError):
        gq.build_cone_query(CENTER_RA, CENTER_DEC, RADIUS_DEG, g_limit=0)
    with pytest.raises(ValueError):
        gq.build_cone_query(float("nan"), CENTER_DEC, RADIUS_DEG)
    # non-numeric input is a TypeError, not a ValueError
    with pytest.raises(TypeError):
        gq.build_cone_query(cast(Any, "83.6"), CENTER_DEC, RADIUS_DEG)
    with pytest.raises(TypeError):
        gq.build_cone_query(CENTER_RA, CENTER_DEC, cast(Any, "0.5"))
    # RA wraps modulo 360
    adql = gq.build_cone_query(CENTER_RA + 360.0, CENTER_DEC, RADIUS_DEG)
    assert "83.63" in adql and "22.014" in adql and "0.5" in adql


# --- the HTTP boundary ----------------------------------------------------------------


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_fetch_gaia_tap_json_posts_the_form_and_parses_json(monkeypatch):
    seen: list[urllib.request.Request] = []

    def fake_urlopen(request, timeout=None):
        seen.append(request)
        return _FakeResponse(json.dumps({"metadata": [], "data": []}).encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    payload = gq.fetch_gaia_tap_json("SELECT 1", maxrec=123, timeout=7.0)
    assert payload == {"metadata": [], "data": []}
    request = seen[0]
    assert request.full_url == gq.VIZIER_TAP_SYNC_URL
    assert request.get_method() == "POST"
    body = request.data
    assert isinstance(body, bytes)
    form = body.decode("ascii")
    assert "QUERY=SELECT+1" in form
    assert "MAXREC=123" in form
    assert "REQUEST=doQuery" in form
    assert "LANG=ADQL" in form


def test_fetch_gaia_tap_json_http_and_network_failures_are_unavailable(monkeypatch):
    def raise_http(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 503, "Service Unavailable", email.message.Message(), None
        )

    monkeypatch.setattr(urllib.request, "urlopen", raise_http)
    with pytest.raises(GaiaUnavailableError) as excinfo:
        gq.fetch_gaia_tap_json("SELECT 1")
    assert "503" in str(excinfo.value)

    def raise_url(request, timeout=None):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(urllib.request, "urlopen", raise_url)
    with pytest.raises(GaiaUnavailableError):
        gq.fetch_gaia_tap_json("SELECT 1")


def test_fetch_gaia_tap_json_bad_bodies_are_response_errors(monkeypatch):
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda request, timeout=None: _FakeResponse(b"not json")
    )
    with pytest.raises(GaiaResponseError):
        gq.fetch_gaia_tap_json("SELECT 1")
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda request, timeout=None: _FakeResponse(b"[1, 2]")
    )
    with pytest.raises(GaiaResponseError):
        gq.fetch_gaia_tap_json("SELECT 1")


# --- response parsing -------------------------------------------------------------------


def test_parse_gaia_response_rows_sorted_brightest_first():
    stars = gq.parse_gaia_response(gaia_payload(sample_rows()))
    assert [s.source_id for s in stars] == [42, 3403768831985762944]
    bright = stars[0]
    assert bright.ra_deg == pytest.approx(83.70)
    assert bright.dec_deg == pytest.approx(22.05)
    assert bright.g == pytest.approx(12.5)
    assert bright.bp == pytest.approx(13.0)
    assert bright.rp == pytest.approx(12.0)
    assert bright.parallax is None and bright.pmra is None and bright.pmdec is None
    faint = stars[1]
    assert faint.parallax == pytest.approx(0.47)
    assert faint.pmra == pytest.approx(0.56)
    assert faint.pmdec == pytest.approx(-0.11)
    assert faint.coord.ra_deg == pytest.approx(faint.ra_deg)


def test_parse_gaia_response_empty_data_is_normal():
    assert gq.parse_gaia_response({"metadata": GAIA_METADATA, "data": []}) == []


def test_parse_gaia_response_accepts_gaia_archive_column_names():
    metadata = [{"name": n} for n in ("ra", "dec", "source_id", "phot_g_mean_mag", "parallax", "pmra", "pmdec")]
    payload = {
        "metadata": metadata,
        "data": [[83.6, 22.0, 7, 15.0, 1.5, 2.5, -3.5]],
    }
    (star,) = gq.parse_gaia_response(payload)
    assert star.source_id == 7
    assert star.g == pytest.approx(15.0)
    assert star.parallax == pytest.approx(1.5)
    assert star.pmra == pytest.approx(2.5)
    assert star.pmdec == pytest.approx(-3.5)
    assert star.bp is None and star.rp is None


def test_parse_gaia_response_malformed_payloads_raise():
    with pytest.raises(GaiaResponseError):
        gq.parse_gaia_response(["not", "a", "dict"])
    with pytest.raises(GaiaResponseError):
        gq.parse_gaia_response({"data": []})  # no metadata
    with pytest.raises(GaiaResponseError):
        gq.parse_gaia_response({"metadata": [{"name": "RAJ2000"}], "data": []})  # missing cols
    with pytest.raises(GaiaResponseError):
        gq.parse_gaia_response({"metadata": GAIA_METADATA})  # no data
    with pytest.raises(GaiaResponseError):
        gq.parse_gaia_response({"metadata": GAIA_METADATA, "data": "nope"})
    bad_row = gaia_payload(sample_rows())
    bad_row["data"][0][0] = None  # no RA
    with pytest.raises(GaiaResponseError):
        gq.parse_gaia_response(bad_row)
    bad_row = gaia_payload(sample_rows())
    bad_row["data"][0][3] = "bright"  # non-numeric G
    with pytest.raises(GaiaResponseError):
        gq.parse_gaia_response(bad_row)
    bad_row = gaia_payload(sample_rows())
    bad_row["data"][0][2] = "not-an-id"
    with pytest.raises(GaiaResponseError):
        gq.parse_gaia_response(bad_row)
    bad_row = gaia_payload(sample_rows())
    bad_row["data"][0][0] = 400.0  # RA out of range
    with pytest.raises(GaiaResponseError):
        gq.parse_gaia_response(bad_row)


# --- the raw query ------------------------------------------------------------------------


def test_query_gaia_fetches_and_parses(fetch_calls):
    stars = gq.query_gaia(CENTER_RA, CENTER_DEC, RADIUS_DEG, g_limit=20.0, maxrec=500)
    assert len(stars) == 2
    assert len(fetch_calls) == 1
    call = fetch_calls[0]
    assert call["maxrec"] == 500
    assert f"CIRCLE('ICRS', {CENTER_RA!r}, {CENTER_DEC!r}, {RADIUS_DEG!r})" in call["query"]
    assert '"Gmag" < 20.0' in call["query"]


def test_query_gaia_validates_before_fetching(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("fetch must not run on invalid input")

    monkeypatch.setattr(gq, "fetch_gaia_tap_json", boom)
    with pytest.raises(ValueError):
        gq.query_gaia(CENTER_RA, CENTER_DEC, -1.0)
    with pytest.raises(ValueError):
        gq.query_gaia(CENTER_RA, CENTER_DEC, RADIUS_DEG, maxrec=0)


# --- the cache key --------------------------------------------------------------------------


def test_gaia_cache_key_rounds_center_and_radius():
    key_a = gq.gaia_cache_key(83.63312, 22.01401, 0.50001)
    key_b = gq.gaia_cache_key(83.63314, 22.01404, 0.50004)  # same rounded bucket
    key_c = gq.gaia_cache_key(83.63350, 22.01401, 0.50001)  # different bucket
    assert key_a == key_b
    assert key_a != key_c
    payload = json.loads(key_a)
    assert payload == {
        "ra": 83.6331,
        "dec": 22.014,
        "radius_deg": 0.5,
        "g_limit": 20.0,
        "maxrec": gq.DEFAULT_MAXREC,
    }
    # g_limit / maxrec change the key
    assert gq.gaia_cache_key(83.633, 22.014, 0.5, g_limit=19.0) != key_a
    assert gq.gaia_cache_key(83.633, 22.014, 0.5, maxrec=10) != key_a


def test_default_cache_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.delenv(gq.CACHE_DIR_ENV_VAR, raising=False)
    assert gq.default_cache_dir() == gq.DEFAULT_CACHE_DIR
    assert gq.DEFAULT_CACHE_DIR.name == "gaia"
    monkeypatch.setenv(gq.CACHE_DIR_ENV_VAR, str(tmp_path / "custom"))
    assert gq.default_cache_dir() == tmp_path / "custom"
    assert gq.default_cache_dir({"X": "y"}) == gq.DEFAULT_CACHE_DIR  # explicit env mapping


# --- the cached query -------------------------------------------------------------------------


def test_query_gaia_cached_fetches_once_then_serves_from_cache(cache_dir, fetch_calls):
    cache = QueryCache(cache_dir=cache_dir, ttl=3600.0)
    stars1 = gq.query_gaia_cached(CENTER_RA, CENTER_DEC, RADIUS_DEG, cache)
    assert len(stars1) == 2 and len(fetch_calls) == 1
    # a second call — even with a slightly different (sub-rounding) center —
    # is served from the cache with no HTTP at all
    stars2 = gq.query_gaia_cached(CENTER_RA + 1e-6, CENTER_DEC - 1e-6, RADIUS_DEG + 1e-6, cache)
    assert len(fetch_calls) == 1
    assert [(s.source_id, s.g) for s in stars2] == [(s.source_id, s.g) for s in stars1]
    # a cache file exists under the cache dir
    assert list(cache_dir.glob("*.json"))


def test_query_gaia_cached_different_radius_refetches(cache_dir, fetch_calls):
    cache = QueryCache(cache_dir=cache_dir, ttl=3600.0)
    gq.query_gaia_cached(CENTER_RA, CENTER_DEC, RADIUS_DEG, cache)
    gq.query_gaia_cached(CENTER_RA, CENTER_DEC, RADIUS_DEG + 0.01, cache)
    assert len(fetch_calls) == 2


def test_query_gaia_cached_expired_entry_refetches(cache_dir, fetch_calls):
    now = [1000.0]
    cache = QueryCache(cache_dir=cache_dir, ttl=10.0, now=lambda: now[0])
    gq.query_gaia_cached(CENTER_RA, CENTER_DEC, RADIUS_DEG, cache)
    assert len(fetch_calls) == 1
    now[0] += 5.0  # still fresh
    gq.query_gaia_cached(CENTER_RA, CENTER_DEC, RADIUS_DEG, cache)
    assert len(fetch_calls) == 1
    now[0] += 10.0  # expired -> refetch
    gq.query_gaia_cached(CENTER_RA, CENTER_DEC, RADIUS_DEG, cache)
    assert len(fetch_calls) == 2


def test_query_gaia_cached_failure_without_cache_raises_unavailable(cache_dir, monkeypatch):
    def fail(query, **kwargs):
        raise GaiaUnavailableError("could not reach VizieR Gaia TAP at ...: no route")

    monkeypatch.setattr(gq, "fetch_gaia_tap_json", fail)
    cache = QueryCache(cache_dir=cache_dir, ttl=3600.0)
    with pytest.raises(GaiaUnavailableError) as excinfo:
        gq.query_gaia_cached(CENTER_RA, CENTER_DEC, RADIUS_DEG, cache)
    message = str(excinfo.value)
    assert "no cached result" in message
    assert str(cache_dir) in message  # names the offline cache location
    assert "Retry when the network is back" in message


def test_query_gaia_cached_unexpected_failure_also_becomes_unavailable(cache_dir, monkeypatch):
    def fail(query, **kwargs):
        raise OSError("network unreachable")

    monkeypatch.setattr(gq, "fetch_gaia_tap_json", fail)
    cache = QueryCache(cache_dir=cache_dir, ttl=3600.0)
    with pytest.raises(GaiaUnavailableError):
        gq.query_gaia_cached(CENTER_RA, CENTER_DEC, RADIUS_DEG, cache)


def test_query_gaia_cached_malformed_response_passes_through_without_cache(cache_dir, monkeypatch):
    def bad(query, **kwargs):
        return {"metadata": [], "data": []}  # missing required columns

    monkeypatch.setattr(gq, "fetch_gaia_tap_json", bad)
    cache = QueryCache(cache_dir=cache_dir, ttl=3600.0)
    with pytest.raises(GaiaResponseError):
        gq.query_gaia_cached(CENTER_RA, CENTER_DEC, RADIUS_DEG, cache)
    # a malformed response is never cached
    assert not list(cache_dir.glob("*.json"))


def test_query_gaia_cached_stale_entry_serves_as_offline_fallback(cache_dir, monkeypatch):
    now = [1000.0]
    cache = QueryCache(cache_dir=cache_dir, ttl=10.0, now=lambda: now[0])

    def ok(query, **kwargs):
        return gaia_payload(sample_rows())

    monkeypatch.setattr(gq, "fetch_gaia_tap_json", ok)
    stars = gq.query_gaia_cached(CENTER_RA, CENTER_DEC, RADIUS_DEG, cache)
    assert len(stars) == 2
    now[0] += 100.0  # entry is now expired

    def fail(query, **kwargs):
        raise GaiaUnavailableError("offline")

    monkeypatch.setattr(gq, "fetch_gaia_tap_json", fail)
    stale = gq.query_gaia_cached(CENTER_RA, CENTER_DEC, RADIUS_DEG, cache)
    assert [s.source_id for s in stale] == [s.source_id for s in stars]


def test_query_gaia_cached_corrupted_cache_file_is_refetched(cache_dir, fetch_calls):
    cache = QueryCache(cache_dir=cache_dir, ttl=3600.0)
    gq.query_gaia_cached(CENTER_RA, CENTER_DEC, RADIUS_DEG, cache)
    assert len(fetch_calls) == 1
    for path in cache_dir.glob("*.json"):
        path.write_text("{ not json", encoding="utf-8")
    stars = gq.query_gaia_cached(CENTER_RA, CENTER_DEC, RADIUS_DEG, cache)
    assert len(fetch_calls) == 2
    assert len(stars) == 2


def test_query_gaia_cached_wrong_shaped_cache_entry_is_refetched(cache_dir, fetch_calls):
    cache = QueryCache(cache_dir=cache_dir, ttl=3600.0)
    key = gq.gaia_cache_key(CENTER_RA, CENTER_DEC, RADIUS_DEG)
    cache.set(key, [{"bogus": 1}])  # wrong shape: not gaia rows
    stars = gq.query_gaia_cached(CENTER_RA, CENTER_DEC, RADIUS_DEG, cache)
    assert len(fetch_calls) == 1
    assert len(stars) == 2


def test_query_gaia_cached_empty_result_is_cached(cache_dir, monkeypatch):
    calls = []

    def empty(query, **kwargs):
        calls.append(query)
        return {"metadata": GAIA_METADATA, "data": []}

    monkeypatch.setattr(gq, "fetch_gaia_tap_json", empty)
    cache = QueryCache(cache_dir=cache_dir, ttl=3600.0)
    assert gq.query_gaia_cached(CENTER_RA, CENTER_DEC, RADIUS_DEG, cache) == []
    assert gq.query_gaia_cached(CENTER_RA, CENTER_DEC, RADIUS_DEG, cache) == []
    assert len(calls) == 1  # the known-empty field is served from cache


def test_query_gaia_cached_default_cache_uses_the_gaia_dir(monkeypatch, tmp_path, fetch_calls):
    monkeypatch.setenv(gq.CACHE_DIR_ENV_VAR, str(tmp_path / "gaia-cache"))
    stars = gq.query_gaia_cached(CENTER_RA, CENTER_DEC, RADIUS_DEG)
    assert len(stars) == 2
    assert list((tmp_path / "gaia-cache").glob("*.json"))
    assert len(fetch_calls) == 1


def test_query_gaia_cached_validates_inputs_before_anything(cache_dir, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("must not fetch")

    monkeypatch.setattr(gq, "fetch_gaia_tap_json", boom)
    cache = QueryCache(cache_dir=cache_dir, ttl=3600.0)
    with pytest.raises(ValueError):
        gq.query_gaia_cached(CENTER_RA, 95.0, RADIUS_DEG, cache)
    with pytest.raises(ValueError):
        gq.query_gaia_cached(CENTER_RA, CENTER_DEC, 0.0, cache)
    with pytest.raises(ValueError):
        gq.query_gaia_cached(CENTER_RA, CENTER_DEC, RADIUS_DEG, cache, g_limit=-3)
    with pytest.raises(ValueError):
        gq.query_gaia_cached(CENTER_RA, CENTER_DEC, RADIUS_DEG, cache, maxrec=0)
