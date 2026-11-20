"""SIMBAD name resolution with mocked HTTP — no real network.

The HTTP boundary is :func:`seestar_controller.catalog.simbad.fetch_tap_json`;
the resolve tests monkeypatch that single function, and the fetch tests
monkeypatch ``urllib.request.urlopen`` to cover the HTTP layer itself.
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
    SIMBAD_TAP_SYNC_URL,
    CatalogCandidate,
    CatalogError,
    NameNotFoundError,
    SimbadResponseError,
    SimbadUnavailableError,
    resolve_name,
    simbad,
)

#: Column order of the mocked TAP JSON payloads (mirrors build_lookup_query).
COLUMNS = ["oid", "main_id", "ra", "dec", "otype", "id", "filter", "flux"]

#: M 42 (Orion Nebula): RA 05h35m17.3s = 83.82208 deg, Dec -05d23m28s = -5.39114 deg.
M42_ROW = [1001, "M 42", 83.82208, -5.39114, "HII", "M 42", "V", 4.0]
#: A second object the same query can match (ambiguous name).
NGC1976_ROW = [1002, "NGC 1976", 83.82458, -5.38972, "EN", "NGC 1976", "V", 8.5]
#: A row without V flux / object type.
NO_MAG_ROW = [2001, "M 43", 83.90875, -5.26778, "", "M 43", None, None]


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


# --- found: single candidate -------------------------------------------------


def test_resolve_single_match_parses_coords_magnitude_type(fake_simbad):
    fake_simbad.responses.append(tap_payload([M42_ROW]))
    candidates = resolve_name("M 42")
    assert len(candidates) == 1
    c = candidates[0]
    assert isinstance(c, CatalogCandidate)
    assert c.name == "M 42"
    assert c.matched_id == "M 42"
    assert c.ra_deg == pytest.approx(83.82208)
    assert c.dec_deg == pytest.approx(-5.39114)
    assert c.coords_j2000 == (c.ra_deg, c.dec_deg)
    assert c.ra_hours == pytest.approx(83.82208 / 15.0)
    assert c.magnitude == pytest.approx(4.0)
    assert c.otype == "HII"


def test_resolve_sends_tap_query_with_exact_identifier(fake_simbad):
    fake_simbad.responses.append(tap_payload([M42_ROW]))
    resolve_name("M 42")
    assert len(fake_simbad.calls) == 1
    call = fake_simbad.calls[0]
    assert call["url"] == SIMBAD_TAP_SYNC_URL
    assert call["maxrec"] == simbad.DEFAULT_MAX_CANDIDATES
    assert call["timeout"] == simbad.DEFAULT_TIMEOUT_S
    assert "FROM ident" in call["query"]
    # plain exact match — the mirror ADQL dialect rejects LOWER() (HTTP 400)
    # and matches i.id = '<name>' case/space-insensitively (verified live)
    assert "i.id = 'M 42'" in call["query"]
    assert "LOWER" not in call["query"]
    assert "f.filter = 'V'" in call["query"]
    # unqualified ORDER BY — qualified ORDER BY b.main_id is rejected on mirrors
    assert "ORDER BY main_id" in call["query"]
    assert "ORDER BY b.main_id" not in call["query"]


def test_resolve_forwards_maxrec_timeout_and_mag_filter(fake_simbad):
    fake_simbad.responses.append(tap_payload([M42_ROW]))
    resolve_name("M 42", mag_filter="B", maxrec=5, timeout=3.0)
    call = fake_simbad.calls[0]
    assert call["maxrec"] == 5
    assert call["timeout"] == 3.0
    assert "f.filter = 'B'" in call["query"]


def test_resolve_accepts_string_numbers_in_rows(fake_simbad):
    row = [1001, "M 42", "83.82208", "-5.39114", "HII", "M 42", "V", "4.0"]
    fake_simbad.responses.append(tap_payload([row]))
    c = resolve_name("M 42")[0]
    assert c.ra_deg == pytest.approx(83.82208)
    assert c.magnitude == pytest.approx(4.0)


# --- ambiguous: multiple candidates ------------------------------------------


def test_resolve_ambiguous_returns_all_candidates_brightest_first(fake_simbad):
    # NGC 1976 row is fainter, so it must come second — nothing is dropped
    fake_simbad.responses.append(tap_payload([M42_ROW, NGC1976_ROW]))
    candidates = resolve_name("M 42")
    assert len(candidates) == 2
    assert [c.name for c in candidates] == ["M 42", "NGC 1976"]
    assert candidates[0].magnitude == pytest.approx(4.0)
    assert candidates[1].magnitude == pytest.approx(8.5)
    assert candidates[1].ra_deg == pytest.approx(83.82458)
    assert candidates[1].dec_deg == pytest.approx(-5.38972)
    assert candidates[1].otype == "EN"


def test_resolve_wildcard_name_uses_like_and_returns_many(fake_simbad):
    fake_simbad.responses.append(tap_payload([M42_ROW, NGC1976_ROW, NO_MAG_ROW]))
    candidates = resolve_name("M 4*")
    assert "LIKE" in fake_simbad.calls[0]["query"]
    assert "'M 4%'" in fake_simbad.calls[0]["query"]
    assert len(candidates) == 3
    # unknown magnitude sorts last
    assert candidates[-1].name == "M 43"
    assert candidates[-1].magnitude is None
    assert candidates[-1].otype is None


def test_wildcard_name_generates_letter_digit_spacing_variants(fake_simbad):
    # SIMBAD stores 'NGC  7789' (two spaces); LIKE is literal on the mirrors,
    # so the wildcard query carries the as-typed + single/double-space variants
    fake_simbad.responses.append(tap_payload([M42_ROW]))
    resolve_name("NGC7789*")
    query = fake_simbad.calls[0]["query"]
    assert "i.id LIKE 'NGC7789%'" in query
    assert "i.id LIKE 'NGC 7789%'" in query
    assert "i.id LIKE 'NGC  7789%'" in query
    assert "LOWER" not in query
    assert "ORDER BY main_id" in query


@pytest.mark.parametrize(
    ("name", "patterns"),
    [
        ("M 4*", ["M 4%"]),  # already spaced -> only the as-typed variant
        ("M4*", ["M4%", "M 4%", "M  4%"]),
        ("NGC7789*", ["NGC7789%", "NGC 7789%", "NGC  7789%"]),
        ("M*", ["M%"]),
    ],
)
def test_wildcard_patterns(name, patterns):
    assert simbad._wildcard_patterns(name) == patterns
    query = simbad.build_lookup_query(name)
    for pattern in patterns:
        assert f"i.id LIKE '{pattern}'" in query
    assert "LOWER" not in query


def test_resolve_dedupes_rows_with_same_oid(fake_simbad):
    fake_simbad.responses.append(tap_payload([M42_ROW, list(M42_ROW)]))
    assert len(resolve_name("M 42")) == 1


# --- not found ----------------------------------------------------------------


def test_resolve_unknown_name_raises_actionable_error(fake_simbad):
    fake_simbad.responses.append(tap_payload([]))
    with pytest.raises(NameNotFoundError) as excinfo:
        resolve_name("M 99")
    assert isinstance(excinfo.value, CatalogError)
    message = str(excinfo.value)
    assert "M 99" in message
    assert "wildcard" in message and "M 99*" in message
    assert "NGC 1976" in message  # identifier-form hint


def test_resolve_empty_data_key_is_not_found(fake_simbad):
    fake_simbad.responses.append(tap_payload([]))
    with pytest.raises(NameNotFoundError, match="no object named"):
        resolve_name("V* bet Per")


# --- input validation ---------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "   ", "\t\n"])
def test_resolve_blank_name_rejected(fake_simbad, bad):
    with pytest.raises(ValueError, match="empty"):
        resolve_name(bad)
    assert fake_simbad.calls == []  # nothing was sent


def test_resolve_non_string_name_rejected(fake_simbad):
    with pytest.raises(ValueError, match="string"):
        resolve_name(None)  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match=">= 1"):
        resolve_name("M 42", maxrec=0)


def test_resolve_escapes_single_quotes_in_name(fake_simbad):
    fake_simbad.responses.append(tap_payload([M42_ROW]))
    resolve_name("M 42's")
    assert "'M 42''s'" in fake_simbad.calls[0]["query"]


# --- malformed / empty responses ----------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {},  # no metadata, no data
        {"metadata": [{"name": "main_id"}], "data": []},  # missing ra/dec columns
        {"metadata": [{"name": c} for c in COLUMNS]},  # no data key
        {"metadata": [{"name": c} for c in COLUMNS], "data": "nope"},  # data not a list
        {"metadata": [{"name": c} for c in COLUMNS], "data": ["nope"]},  # row not a list
        {"metadata": [{"name": c} for c in COLUMNS], "data": [[1, "M 42", "abc", -5.4, "HII", "M 42", "V", 4.0]]},
        {"metadata": [{"name": c} for c in COLUMNS], "data": [[1, "M 42", None, -5.4, "HII", "M 42", "V", 4.0]]},
        {"metadata": [{"name": c} for c in COLUMNS], "data": [[1, "M 42", 370.0, -5.4, "HII", "M 42", "V", 4.0]]},
        {"metadata": [{"name": c} for c in COLUMNS], "data": [[1, "M 42", 83.8, -95.0, "HII", "M 42", "V", 4.0]]},
        {"metadata": [{"name": c} for c in COLUMNS], "data": [[1, "", 83.8, -5.4, "HII", "", "V", 4.0]]},
    ],
)
def test_malformed_responses_raise_response_error(fake_simbad, payload):
    fake_simbad.responses.append(payload)
    with pytest.raises(SimbadResponseError) as excinfo:
        resolve_name("M 42")
    assert isinstance(excinfo.value, CatalogError)


def test_parse_tap_response_rejects_non_object():
    with pytest.raises(SimbadResponseError, match="not a JSON object"):
        simbad.parse_tap_response([1, 2, 3])


def test_fetch_rejects_non_object_json_body(monkeypatch):
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda req, timeout: _FakeResponse(b"[1, 2]")
    )
    with pytest.raises(SimbadResponseError, match="not a JSON object"):
        simbad.fetch_tap_json("SELECT 1")


# --- the HTTP layer itself (urlopen mocked) -----------------------------------


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
        return _FakeResponse(json.dumps(tap_payload([M42_ROW])).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    payload = simbad.fetch_tap_json("SELECT ...", maxrec=7, timeout=2.5)

    request = captured["request"]
    assert request.full_url == SIMBAD_TAP_SYNC_URL
    assert request.get_method() == "POST"
    assert request.headers["Accept"] == "application/json"
    assert captured["timeout"] == 2.5
    form = urllib.parse.parse_qs(request.data.decode("ascii"))
    assert form["REQUEST"] == ["doQuery"]
    assert form["LANG"] == ["ADQL"]
    assert form["QUERY"] == ["SELECT ..."]
    assert form["MAXREC"] == ["7"]
    # FORMAT=json is what the mirrors honor; RESPONSEFORMAT is the TAP
    # standard field CDS Strasbourg honors — both are sent (verified live)
    assert form["FORMAT"] == ["json"]
    assert form["RESPONSEFORMAT"] == ["application/json"]
    assert payload["data"] == [M42_ROW]


def test_fetch_http_error_raises_unavailable(monkeypatch):
    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(
            SIMBAD_TAP_SYNC_URL, 503, "Service Unavailable", None, None  # ty: ignore[invalid-argument-type]
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(SimbadUnavailableError, match="HTTP 503"):
        simbad.fetch_tap_json("SELECT ...")


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
    with pytest.raises(SimbadUnavailableError, match="could not reach SIMBAD"):
        simbad.fetch_tap_json("SELECT ...")


def test_fetch_invalid_json_body_raises_response_error(monkeypatch):
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda req, timeout: _FakeResponse(b"<html>oops")
    )
    with pytest.raises(SimbadResponseError, match="not valid JSON"):
        simbad.fetch_tap_json("SELECT ...")


def test_fetch_error_propagates_through_resolve_name(fake_simbad):
    fake_simbad.responses.append(SimbadUnavailableError("could not reach SIMBAD"))
    with pytest.raises(SimbadUnavailableError):
        resolve_name("M 42")


# --- endpoint fallback (CDS Strasbourg down since 2026-10-29; mirrors verified) ---


def test_fetch_tries_mirrors_in_order_when_cds_is_down(monkeypatch):
    requested = []

    def fake_urlopen(request, timeout):
        requested.append(request.full_url)
        if request.full_url == SIMBAD_TAP_SYNC_URL:
            raise urllib.error.URLError("connection refused")  # CDS is down
        return _FakeResponse(json.dumps(tap_payload([M42_ROW])).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    payload = simbad.fetch_tap_json("SELECT ...", timeout=5.0)
    assert payload["data"] == [M42_ROW]
    # CDS first, then the Harvard mirror — and it stops at the first success
    assert requested == [SIMBAD_TAP_SYNC_URL, simbad.SIMBAD_TAP_SYNC_URL_CFA]


def test_fetch_raises_unavailable_only_after_all_endpoints_fail(monkeypatch):
    requested = []

    def fake_urlopen(request, timeout):
        requested.append(request.full_url)
        raise urllib.error.URLError("down")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(SimbadUnavailableError) as excinfo:
        simbad.fetch_tap_json("SELECT ...")
    assert requested == list(simbad.SIMBAD_TAP_SYNC_URLS)  # all three, in order
    message = str(excinfo.value)
    assert "could not reach SIMBAD" in message
    for url in simbad.SIMBAD_TAP_SYNC_URLS:
        assert url in message  # the aggregate error names every endpoint tried


def test_fetch_http_error_on_first_endpoint_falls_back_to_mirror(monkeypatch):
    requested = []

    def fake_urlopen(request, timeout):
        requested.append(request.full_url)
        if request.full_url == SIMBAD_TAP_SYNC_URL:
            raise urllib.error.HTTPError(
                SIMBAD_TAP_SYNC_URL, 404, "Page unavailable", None, None  # ty: ignore[invalid-argument-type]
            )  # the CDS outage answers 404
        return _FakeResponse(json.dumps(tap_payload([M42_ROW])).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    payload = simbad.fetch_tap_json("SELECT ...")
    assert payload["data"] == [M42_ROW]
    assert requested == [SIMBAD_TAP_SYNC_URL, simbad.SIMBAD_TAP_SYNC_URL_CFA]


def test_fetch_pinned_url_tries_only_that_endpoint(monkeypatch):
    requested = []

    def fake_urlopen(request, timeout):
        requested.append(request.full_url)
        raise urllib.error.URLError("down")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(SimbadUnavailableError) as excinfo:
        simbad.fetch_tap_json("SELECT ...", url=simbad.SIMBAD_TAP_SYNC_URL_IDIA)
    assert requested == [simbad.SIMBAD_TAP_SYNC_URL_IDIA]
    assert simbad.SIMBAD_TAP_SYNC_URL_IDIA in str(excinfo.value)
    assert SIMBAD_TAP_SYNC_URL not in str(excinfo.value)


def test_resolve_name_succeeds_via_mirror_when_cds_is_down(monkeypatch):
    def fake_urlopen(request, timeout):
        if request.full_url == SIMBAD_TAP_SYNC_URL:
            raise urllib.error.URLError("connection refused")
        return _FakeResponse(json.dumps(tap_payload([M42_ROW])).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    candidates = resolve_name("M 42")
    assert len(candidates) == 1
    assert candidates[0].name == "M 42"
    assert candidates[0].ra_deg == pytest.approx(83.82208)


# --- module boundary ----------------------------------------------------------


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
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


def test_public_api_is_exported_from_package():
    from seestar_controller import catalog

    for name in catalog.__all__:
        assert hasattr(catalog, name), name
    assert callable(catalog.resolve_name)
    assert issubclass(catalog.NameNotFoundError, catalog.CatalogError)
