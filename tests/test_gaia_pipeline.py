"""End-to-end demo test for gaia-crossmatch issue 01: a synthetic FITS stack
with a known WCS + injected stars and a mocked Gaia TAP response -> detection
finds the stars, the cross-match pairs them within tolerance, unmatched
detections are flagged, the second call is served from the cache, and a query
failure without cache raises a clear error. No network, no gi."""

from __future__ import annotations

import pytest
from astropy.io import fits as pyfits
from test_gaia_detect import (
    STARS,
    make_stack,
    read_fits,
    stack_fits_bytes,
    star_sky,
    wcs_header,
)
from test_gaia_query import gaia_payload

from seestar_controller.catalog.cache import QueryCache
from seestar_controller.gaia import matching as gc
from seestar_controller.gaia import query as gq
from seestar_controller.gaia.errors import GaiaUnavailableError, WcsMissingError


def gaia_rows_for_stars(header, *, jitter_arcsec: float = 0.3) -> list[dict]:
    """Gaia rows at the injected stars' sky positions (with a small jitter)."""
    rows = []
    for i, (cy, cx, _peak) in enumerate(STARS):
        ra, dec = star_sky(header, cx, cy)
        rows.append(
            {
                "source_id": 1000 + i,
                "ra": ra,
                "dec": dec + jitter_arcsec / 3600.0,
                "g": 12.0 + i,
                "bp": 12.5 + i,
                "rp": 11.5 + i,
                "plx": 0.5 + i,
                "pmra": 1.0 + i,
                "pmde": -1.0 - i,
            }
        )
    # one extra Gaia star with no detection on the stack
    rows.append(
        {
            "source_id": 9999,
            "ra": 83.0,
            "dec": 22.5,
            "g": 18.0,
            "bp": 18.4,
            "rp": 17.6,
            "plx": None,
            "pmra": None,
            "pmde": None,
        }
    )
    return rows


@pytest.fixture()
def stack(tmp_path):
    """A synthetic session STACK written as a real FITS file and read back."""
    data = make_stack()
    header = wcs_header()
    path = tmp_path / "stacks" / "session-1" / "2461365.416668.fits"
    path.parent.mkdir(parents=True)
    path.write_bytes(stack_fits_bytes(data, header))
    return read_fits(path)


@pytest.fixture()
def install_tap(monkeypatch):
    """Install a mocked Gaia TAP boundary; returns (calls, install) where
    ``install(header, jitter_arcsec=...)`` arms the mock for a header."""
    calls: list[str] = []

    def install(header, *, jitter_arcsec: float = 0.3):
        rows = gaia_rows_for_stars(header, jitter_arcsec=jitter_arcsec)

        def fake_fetch(query, **kwargs):
            calls.append(query)
            return gaia_payload(rows)

        monkeypatch.setattr(gq, "fetch_gaia_tap_json", fake_fetch)
        return calls

    return calls, install


def rows_near(rows, cx: float, cy: float, tol: float = 2.0):
    return [r for r in rows if abs(r.x - cx) <= tol and abs(r.y - cy) <= tol]


def test_crossmatch_stack_end_to_end(stack, install_tap, tmp_path):
    data, header = stack
    calls, install = install_tap
    install(header)
    cache = QueryCache(cache_dir=tmp_path / "gaia-cache", ttl=3600.0)

    rows = gc.crossmatch_stack(data, header, cache=cache)

    # every injected star is detected and matched to its Gaia row
    assert len(calls) == 1
    for i, (cy, cx, _peak) in enumerate(STARS):
        near = rows_near(rows, cx, cy)
        assert len(near) == 1, f"injected star at ({cx}, {cy}) missing"
        row = near[0]
        assert row.matched
        assert row.source_id == 1000 + i
        assert row.g == pytest.approx(12.0 + i)
        assert row.bp == pytest.approx(12.5 + i)
        assert row.rp == pytest.approx(11.5 + i)
        assert row.parallax == pytest.approx(0.5 + i)
        assert row.pmra == pytest.approx(1.0 + i)
        assert row.pmdec == pytest.approx(-1.0 - i)
        assert row.separation_arcsec <= 1.5
        assert row.separation_arcsec == pytest.approx(0.3, abs=0.15)

    # the cone search is a Gaia DR3 cone around the field with G < 20
    adql = calls[0]
    assert '"I/355/gaiadr3"' in adql
    assert '"Gmag" < 20.0' in adql
    assert "CIRCLE('ICRS'" in adql
    assert "CONTAINS(POINT('ICRS'" in adql

    # any extra (noise) detections are flagged unmatched, not dropped
    unmatched = [r for r in rows if not r.matched]
    assert all(r.source_id is None and r.g is None for r in unmatched)

    # the second call is served from the disk cache: no new HTTP
    rows2 = gc.crossmatch_stack(data, header, cache=cache)
    assert len(calls) == 1
    assert [(r.x, r.y, r.source_id, r.matched) for r in rows2] == [
        (r.x, r.y, r.source_id, r.matched) for r in rows
    ]


def test_crossmatch_stack_match_radius_is_configurable(stack, install_tap, tmp_path):
    data, header = stack
    calls, install = install_tap
    install(header, jitter_arcsec=1.0)  # Gaia rows 1.0 arcsec off the detections
    cache = QueryCache(cache_dir=tmp_path / "gaia-cache", ttl=3600.0)

    tight = gc.crossmatch_stack(data, header, cache=cache, radius_arcsec=0.5)
    star_rows = [r for cy, cx, _p in STARS for r in rows_near(tight, cx, cy)]
    assert len(star_rows) == len(STARS)
    assert all(not r.matched for r in star_rows)  # 1.0 arcsec > 0.5 arcsec

    wide = gc.crossmatch_stack(data, header, cache=cache, radius_arcsec=1.5)
    star_rows = [r for cy, cx, _p in STARS for r in rows_near(wide, cx, cy)]
    assert all(r.matched for r in star_rows)  # 1.0 arcsec < 1.5 arcsec
    assert all(r.separation_arcsec == pytest.approx(1.0, abs=0.15) for r in star_rows)
    assert len(calls) == 1  # the match radius is client-side: still one query


def test_crossmatch_stack_query_failure_without_cache_raises_clear_error(stack, monkeypatch, tmp_path):
    data, header = stack

    def fail(query, **kwargs):
        raise gq.GaiaUnavailableError("could not reach VizieR Gaia TAP: offline")

    monkeypatch.setattr(gq, "fetch_gaia_tap_json", fail)
    cache = QueryCache(cache_dir=tmp_path / "empty-cache", ttl=3600.0)
    with pytest.raises(GaiaUnavailableError) as excinfo:
        gc.crossmatch_stack(data, header, cache=cache)
    assert "no cached result" in str(excinfo.value)


def test_crossmatch_stack_missing_wcs_raises_plate_solve_first(stack, tmp_path):
    data, _header = stack
    bare = pyfits.Header()
    bare["OBJECT"] = "M 42"
    cache = QueryCache(cache_dir=tmp_path / "gaia-cache", ttl=3600.0)
    with pytest.raises(WcsMissingError) as excinfo:
        gc.crossmatch_stack(data, bare, cache=cache)
    assert "plate-solve" in str(excinfo.value)
