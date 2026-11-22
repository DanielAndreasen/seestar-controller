"""Astrometry ticket 01 — the pure plate-solve core.

Headless: no ``gi`` anywhere (a subprocess test enforces the boundary, see
tests/test_astrometry_boundary.py). No network: the Gaia fallback's reference
query is INJECTED (``gaia_query=``) with synthetic Gaia stars at the true sky
positions of the injected image stars, and the ``solve-field`` subprocess is
a FAKE executable placed on PATH by monkeypatching ``PATH``.

The synthetic setup: a 256x256 frame with a known TAN WCS (0.001 deg/px =
3.6 arcsec/px, CRVAL = (83, 22)) and 10 Gaussian stars (sigma 1.5 px) + noise.
The Gaia fallback must recover the WCS within tolerance (pixel->sky residuals
< 0.5 arcsec everywhere, center within a few arcsec), the solved cards must
round-trip through a FITS header (``write_wcs_to_fits`` + re-read), the fake
``solve-field`` binary must be picked up via ``shutil.which`` when on PATH
(and its failures must raise clear ``SolveError``\\ s), and unsolvable frames
must fail with a clear message.
"""

from __future__ import annotations

import io
import math
import stat
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits as pyfits
from astropy.wcs import WCS

from seestar_controller import astrometry
from seestar_controller.astrometry import (
    SOLVER_AUTO,
    SOLVER_GAIA,
    SOLVER_SOLVE_FIELD,
    SolveError,
    cards_to_wcs,
    has_celestial_wcs,
    header_center,
    header_pixel_scale_arcsec,
    provisional_wcs_cards,
    read_fits_bytes,
    solve_fits_file,
    solve_frame,
    wcs_to_cards,
    write_wcs_to_fits,
)
from seestar_controller.gaia.query import GaiaStar

# --- synthetic sky ------------------------------------------------------------------------

#: The true pixel scale (deg/px) of the synthetic frame.
SCALE = 0.001
#: The true pixel scale in arcsec/px.
SCALE_ARCSEC = SCALE * 3600.0
#: The synthetic frame size (px).
SIZE = 256
#: The true field center (CRVAL).
CRVAL = (83.0, 22.0)
#: The Gaussian star sigma (px).
STAR_SIGMA = 1.5
#: The background level.
BACKGROUND = 100.0
#: The noise sigma.
NOISE = 2.0
#: Star positions on the frame as (cy, cx, peak).
STARS = (
    (30, 30, 20000.0),
    (30, 120, 18000.0),
    (90, 60, 15000.0),
    (90, 170, 12000.0),
    (60, 100, 9000.0),
    (150, 40, 8000.0),
    (150, 200, 7000.0),
    (200, 90, 6000.0),
    (200, 180, 5500.0),
    (120, 220, 5000.0),
)
#: The accepted pixel->sky residual tolerance (arcsec).
RESIDUAL_TOL_ARCSEC = 0.5


def true_header() -> dict:
    """The TRUE WCS header of the synthetic frame (TAN, 0.001 deg/px)."""
    return {
        "CTYPE1": "RA---TAN",
        "CTYPE2": "DEC--TAN",
        "CRVAL1": CRVAL[0],
        "CRVAL2": CRVAL[1],
        "CRPIX1": (SIZE + 1) / 2.0,
        "CRPIX2": (SIZE + 1) / 2.0,
        "CDELT1": -SCALE,
        "CDELT2": SCALE,
    }


def true_wcs() -> WCS:
    return WCS(true_header())


def make_image(seed: int = 42) -> np.ndarray:
    """The synthetic 256x256 frame: background + Gaussian stars + noise."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:SIZE, 0:SIZE]
    data = np.full((SIZE, SIZE), BACKGROUND, dtype=np.float64)
    for cy, cx, peak in STARS:
        data = data + peak * np.exp(-(((yy - cy) ** 2 + (xx - cx) ** 2) / (2.0 * STAR_SIGMA**2)))
    data = data + rng.normal(0.0, NOISE, size=(SIZE, SIZE))
    return np.ascontiguousarray(data, dtype=np.float32)


def blank_image(seed: int = 7) -> np.ndarray:
    """A starless frame (background + noise only)."""
    rng = np.random.default_rng(seed)
    data = np.full((SIZE, SIZE), BACKGROUND, dtype=np.float64) + rng.normal(
        0.0, NOISE, size=(SIZE, SIZE)
    )
    return np.ascontiguousarray(data, dtype=np.float32)


def gaia_stars_at_stars(
    offset_arcsec: tuple[float, float] = (0.0, 0.0),
    noise_arcsec: float = 0.0,
    seed: int = 0,
) -> list[GaiaStar]:
    """Synthetic Gaia stars at the TRUE sky positions of the injected image
    stars, optionally shifted by a constant ``offset_arcsec`` =
    (d_ra*cos(dec), d_dec) plus per-star Gaussian noise of ``noise_arcsec``
    (arcsec, per axis)."""
    wcs = true_wcs()
    rng = np.random.default_rng(seed)
    stars = []
    for i, (cy, cx, _peak) in enumerate(STARS):
        coord = wcs.pixel_to_world(cx, cy)
        d_ra, d_dec = offset_arcsec
        if noise_arcsec:
            d_ra += float(rng.normal(0.0, noise_arcsec))
            d_dec += float(rng.normal(0.0, noise_arcsec))
        ra = (coord.ra.deg + d_ra / 3600.0 / math.cos(math.radians(coord.dec.deg))) % 360.0
        dec = coord.dec.deg + d_dec / 3600.0
        stars.append(GaiaStar(source_id=1000 + i, ra_deg=ra, dec_deg=dec, g=12.0))
    return stars


class FakeQuery:
    """An injectable ``gaia_query`` returning fixed ``stars``; every call is
    recorded in ``calls`` as ``(center_ra, center_dec, radius_deg)``."""

    def __init__(self, stars: list[GaiaStar]):
        self.stars = stars
        self.calls: list[tuple[float, float, float]] = []

    def __call__(self, center_ra: float, center_dec: float, radius_deg: float):
        self.calls.append((center_ra, center_dec, radius_deg))
        return self.stars


def fake_query(stars: list[GaiaStar]) -> FakeQuery:
    return FakeQuery(stars)


def blind_header() -> dict:
    """A header WITHOUT a WCS but with RA/DEC hint cards (like a raw sub)."""
    return {"RA": CRVAL[0], "DEC": CRVAL[1], "DATE-OBS": "2026-11-20T22:00:00.000"}


def fits_bytes(data: np.ndarray, header: dict | None = None) -> bytes:
    """A small valid FITS file with ``data`` and ``header`` cards."""
    hdu = pyfits.PrimaryHDU(data=np.asarray(data, dtype=np.float32))
    for key, value in (header or {}).items():
        hdu.header[key] = value
    buf = io.BytesIO()
    pyfits.HDUList([hdu]).writeto(buf)
    return buf.getvalue()


def residual_arcsec(wcs: WCS, x: float, y: float) -> float:
    """The pixel->sky residual of ``wcs`` vs the true WCS at pixel (x, y)."""
    a = wcs.pixel_to_world(x, y)
    b = true_wcs().pixel_to_world(x, y)
    return math.hypot(
        (a.ra.deg - b.ra.deg) * 3600.0 * math.cos(math.radians(b.dec.deg)),
        (a.dec.deg - b.dec.deg) * 3600.0,
    )


# --- wcs card helpers ---------------------------------------------------------------------


def test_has_celestial_wcs_and_header_center():
    assert has_celestial_wcs(true_header())
    assert not has_celestial_wcs(blind_header())
    assert not has_celestial_wcs({})
    assert header_center(true_header()) == pytest.approx(CRVAL)
    assert header_center(blind_header()) == pytest.approx(CRVAL)
    assert header_center({}) is None
    assert header_center({"RA": 370.0, "DEC": 22.0}) == pytest.approx((10.0, 22.0))
    assert header_center({"RA": 83.0, "DEC": 95.0}) is None  # invalid dec


def test_header_pixel_scale_arcsec():
    assert header_pixel_scale_arcsec(true_header()) == pytest.approx(SCALE_ARCSEC)
    cd_header = {
        "CTYPE1": "RA---TAN",
        "CTYPE2": "DEC--TAN",
        "CRVAL1": 83.0,
        "CRVAL2": 22.0,
        "CD1_1": -SCALE,
        "CD1_2": 0.0,
        "CD2_1": 0.0,
        "CD2_2": SCALE,
    }
    assert header_pixel_scale_arcsec(cd_header) == pytest.approx(SCALE_ARCSEC)
    pc_header = {
        "CTYPE1": "RA---TAN",
        "CTYPE2": "DEC--TAN",
        "CRVAL1": 83.0,
        "CRVAL2": 22.0,
        "CDELT1": -SCALE,
        "CDELT2": SCALE,
        "PC1_1": 1.0,
        "PC1_2": 0.0,
        "PC2_1": 0.0,
        "PC2_2": 1.0,
    }
    assert header_pixel_scale_arcsec(pc_header) == pytest.approx(SCALE_ARCSEC)
    # PC matrix without CDELT2: scale = sqrt(|det PC|) * |CDELT1|
    pc_only = {
        "CTYPE1": "RA---TAN",
        "CTYPE2": "DEC--TAN",
        "CRVAL1": 83.0,
        "CRVAL2": 22.0,
        "CDELT1": -SCALE,
        "PC1_1": 2.0,
        "PC1_2": 0.0,
        "PC2_1": 0.0,
        "PC2_2": 2.0,
    }
    assert header_pixel_scale_arcsec(pc_only) == pytest.approx(2.0 * SCALE_ARCSEC)
    assert header_pixel_scale_arcsec(blind_header()) is None
    assert header_pixel_scale_arcsec({}) is None


def test_provisional_wcs_cards():
    cards = provisional_wcs_cards(CRVAL[0], CRVAL[1], SCALE_ARCSEC, (SIZE, SIZE))
    assert cards["CTYPE1"] == "RA---TAN"
    assert cards["CRVAL1"] == pytest.approx(CRVAL[0])
    assert cards["CRVAL2"] == pytest.approx(CRVAL[1])
    assert cards["CRPIX1"] == (SIZE + 1) / 2.0
    assert cards["CDELT1"] == pytest.approx(-SCALE)
    assert cards["CDELT2"] == pytest.approx(SCALE)
    wcs = cards_to_wcs(cards)
    assert wcs.celestial is not None
    # the provisional WCS maps the image center to the hint
    center = wcs.pixel_to_world((SIZE - 1) / 2.0, (SIZE - 1) / 2.0)
    assert center.ra.deg == pytest.approx(CRVAL[0], abs=1e-9)
    assert center.dec.deg == pytest.approx(CRVAL[1], abs=1e-9)
    with pytest.raises(ValueError):
        provisional_wcs_cards(83.0, 95.0, 3.6, (10, 10))
    with pytest.raises(ValueError):
        provisional_wcs_cards(83.0, 22.0, 0.0, (10, 10))
    with pytest.raises(ValueError):
        provisional_wcs_cards(83.0, 22.0, 3.6, (0, 10))
    with pytest.raises(TypeError):
        provisional_wcs_cards("83", 22.0, 3.6, (10, 10))  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


def test_wcs_cards_round_trip():
    wcs = true_wcs()
    cards = wcs_to_cards(wcs)
    wcs2 = cards_to_wcs(cards)
    for x, y in ((0.0, 0.0), (100.0, 200.0), (255.0, 255.0)):
        assert residual_arcsec(wcs2, x, y) < 1e-6
    with pytest.raises(TypeError):
        cards_to_wcs(["not", "a", "mapping"])  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


def test_write_wcs_to_fits_round_trip_and_stale_key_removal(tmp_path):
    path = tmp_path / "frame.fits"
    # start with a WRONG WCS (10x scale) to prove stale keys are removed
    wrong = dict(true_header())
    wrong["CDELT1"] = -SCALE * 10
    wrong["CDELT2"] = SCALE * 10
    wrong["CD1_1"] = -SCALE * 10
    wrong["CD2_2"] = SCALE * 10
    path.write_bytes(fits_bytes(make_image(), wrong))

    cards = wcs_to_cards(true_wcs())
    returned = write_wcs_to_fits(path, cards)
    assert returned == path
    # no temp files left behind
    assert list(tmp_path.glob(".wcs-*")) == []

    header = pyfits.getheader(str(path))
    assert has_celestial_wcs(header)
    assert header["CDELT1"] == pytest.approx(-SCALE)
    assert "CD1_1" not in header  # the stale CD matrix is gone
    wcs = cards_to_wcs(header)
    for x, y in ((0.0, 0.0), (128.0, 128.0), (255.0, 255.0)):
        assert residual_arcsec(wcs, x, y) < 1e-6
    # the data survived the rewrite
    with pyfits.open(path, memmap=False) as hdul:
        assert hdul[0].data is not None
        assert hdul[0].data.shape == (SIZE, SIZE)


def test_write_wcs_to_fits_errors(tmp_path):
    path = tmp_path / "frame.fits"
    path.write_bytes(fits_bytes(make_image(), blind_header()))
    with pytest.raises(TypeError):
        write_wcs_to_fits(path, ["not", "a", "mapping"])  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError):
        write_wcs_to_fits(path, {"TOOLONGKEY": 1.0})
    with pytest.raises(SolveError):
        write_wcs_to_fits(tmp_path / "missing.fits", {"CRVAL1": 1.0})
    bad = tmp_path / "bad.fits"
    bad.write_bytes(b"not a fits file at all")
    with pytest.raises(SolveError):
        write_wcs_to_fits(bad, {"CRVAL1": 1.0})


def test_read_fits_bytes():
    data, header = read_fits_bytes(fits_bytes(make_image(), blind_header()))
    assert data.shape == (SIZE, SIZE)
    assert header["RA"] == pytest.approx(CRVAL[0])
    with pytest.raises(SolveError):
        read_fits_bytes(b"garbage, not fits")


# --- the Gaia pattern-match fallback --------------------------------------------------------


def test_solve_frame_gaia_recovers_known_wcs():
    query = fake_query(gaia_stars_at_stars())
    result = solve_frame(
        data=make_image(),
        header=blind_header(),
        solver=SOLVER_GAIA,
        gaia_query=query,
        pixel_scale_hint=SCALE_ARCSEC,
    )
    assert result.solver == SOLVER_GAIA
    assert result.n_stars == len(STARS)
    assert result.rms_arcsec is not None and result.rms_arcsec < 0.1
    assert len(result.matches) == len(STARS)
    assert len(result.rows) >= len(STARS)
    # the query was called once with a cone around the field center
    assert len(query.calls) == 1
    q_ra, q_dec, q_radius = query.calls[0]
    assert q_ra == pytest.approx(CRVAL[0], abs=0.5)
    assert q_dec == pytest.approx(CRVAL[1], abs=0.5)
    assert q_radius > 0.1  # covers the 0.256 deg field diagonal
    # the recovered center is within a few arcsec of the true center
    center_sep = math.hypot(
        (result.center_ra_deg - CRVAL[0]) * 3600.0 * math.cos(math.radians(CRVAL[1])),
        (result.center_dec_deg - CRVAL[1]) * 3600.0,
    )
    assert center_sep < 5.0
    # pixel->sky residuals everywhere (stars + corners) within tolerance
    for cy, cx, _peak in STARS:
        assert residual_arcsec(result.wcs, cx, cy) < RESIDUAL_TOL_ARCSEC
    for x, y in ((0.0, 0.0), (255.0, 0.0), (0.0, 255.0), (255.0, 255.0), (128.0, 200.0)):
        assert residual_arcsec(result.wcs, x, y) < RESIDUAL_TOL_ARCSEC
    # the cards rebuild the same WCS
    wcs2 = cards_to_wcs(result.cards)
    assert residual_arcsec(wcs2, 100.0, 150.0) < RESIDUAL_TOL_ARCSEC


def test_solve_frame_gaia_uses_header_wcs_hint_when_present():
    # a header that already carries the (approximate) WCS: no explicit hints
    query = fake_query(gaia_stars_at_stars())
    result = solve_frame(
        data=make_image(), header=true_header(), solver=SOLVER_GAIA, gaia_query=query
    )
    assert result.n_stars == len(STARS)
    for cy, cx, _peak in STARS:
        assert residual_arcsec(result.wcs, cx, cy) < RESIDUAL_TOL_ARCSEC


def test_solve_frame_gaia_tolerates_slightly_off_hints():
    off_ra = CRVAL[0] + 5.0 / 3600.0 / math.cos(math.radians(CRVAL[1]))
    off_dec = CRVAL[1] - 3.0 / 3600.0
    query = fake_query(gaia_stars_at_stars())
    result = solve_frame(
        data=make_image(),
        header={},
        solver=SOLVER_GAIA,
        gaia_query=query,
        center_hint=(off_ra, off_dec),
        pixel_scale_hint=SCALE_ARCSEC * 1.05,
    )
    assert result.n_stars == len(STARS)
    for cy, cx, _peak in STARS:
        assert residual_arcsec(result.wcs, cx, cy) < RESIDUAL_TOL_ARCSEC


def test_solve_fits_file_from_path_and_header_round_trip(tmp_path):
    path = tmp_path / "sub.fits"
    path.write_bytes(fits_bytes(make_image(), blind_header()))
    query = fake_query(gaia_stars_at_stars())
    result = solve_fits_file(
        path, solver=SOLVER_GAIA, gaia_query=query, pixel_scale_hint=SCALE_ARCSEC
    )
    assert result.solver == SOLVER_GAIA
    # write the solved WCS into the FITS header and re-read it
    write_wcs_to_fits(path, result.cards)
    header = pyfits.getheader(str(path))
    assert has_celestial_wcs(header)
    wcs = cards_to_wcs(header)
    for cy, cx, _peak in STARS:
        assert residual_arcsec(wcs, cx, cy) < RESIDUAL_TOL_ARCSEC
    # solving the now-solved file again works from the header alone
    result2 = solve_fits_file(path, solver=SOLVER_GAIA, gaia_query=query)
    assert result2.n_stars == len(STARS)
    assert residual_arcsec(result2.wcs, 100.0, 150.0) < RESIDUAL_TOL_ARCSEC


def test_solve_frame_unsolvable_blank_frame():
    query = fake_query(gaia_stars_at_stars())
    with pytest.raises(SolveError, match="only 0 source"):
        solve_frame(
            data=blank_image(),
            header=blind_header(),
            solver=SOLVER_GAIA,
            gaia_query=query,
            pixel_scale_hint=SCALE_ARCSEC,
        )


def test_solve_frame_unsolvable_no_gaia_matches():
    query = fake_query([])  # an empty field: no reference stars
    with pytest.raises(SolveError, match="matched a Gaia star"):
        solve_frame(
            data=make_image(),
            header=blind_header(),
            solver=SOLVER_GAIA,
            gaia_query=query,
            pixel_scale_hint=SCALE_ARCSEC,
        )


def test_solve_frame_unsolvable_blind_without_hint():
    query = fake_query(gaia_stars_at_stars())
    with pytest.raises(SolveError, match="cannot solve blind"):
        solve_frame(data=make_image(), header={}, solver=SOLVER_GAIA, gaia_query=query)


def test_solve_frame_unsolvable_rms_too_high():
    # Gaia stars scattered by ~2 arcsec per axis around the true positions:
    # every star still matches (generous radius), but the fit RMS (~2.8
    # arcsec) exceeds the 1.0 arcsec tolerance
    query = fake_query(gaia_stars_at_stars(noise_arcsec=2.0, seed=3))
    with pytest.raises(SolveError, match="RMS"):
        solve_frame(
            data=make_image(),
            header=blind_header(),
            solver=SOLVER_GAIA,
            gaia_query=query,
            pixel_scale_hint=SCALE_ARCSEC,
            match_radius_arcsec=10.0,
        )


def test_solve_frame_default_query_goes_through_query_gaia(monkeypatch):
    # the default gaia_query is query_gaia — prove it (and that no test ever
    # touches the real network: the monkeypatched query raises instead)
    import seestar_controller.astrometry.solve as solve_mod

    def no_network(*args, **kwargs):
        raise RuntimeError("network touched")

    monkeypatch.setattr(solve_mod, "query_gaia", no_network)
    with pytest.raises(SolveError, match="network touched"):
        solve_frame(
            data=make_image(),
            header=blind_header(),
            solver=SOLVER_GAIA,
            pixel_scale_hint=SCALE_ARCSEC,
        )


def test_solve_frame_bad_parameters():
    query = fake_query(gaia_stars_at_stars())
    data, header = make_image(), blind_header()
    with pytest.raises(ValueError, match="solver"):
        solve_frame(data=data, header=header, solver="bogus", gaia_query=query)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="min_matches"):
        solve_frame(data=data, header=header, solver=SOLVER_GAIA, gaia_query=query, min_matches=2)
    with pytest.raises(TypeError, match="min_matches"):
        solve_frame(data=data, header=header, solver=SOLVER_GAIA, gaia_query=query, min_matches="6")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="match_radius_arcsec"):
        solve_frame(data=data, header=header, solver=SOLVER_GAIA, gaia_query=query, match_radius_arcsec=0.0)
    with pytest.raises(ValueError, match="rms_tolerance_arcsec"):
        solve_frame(data=data, header=header, solver=SOLVER_GAIA, gaia_query=query, rms_tolerance_arcsec=-1.0)
    with pytest.raises(ValueError, match="pixel_scale_hint"):
        solve_frame(data=data, header=header, solver=SOLVER_GAIA, gaia_query=query, pixel_scale_hint=-3.6)
    with pytest.raises(ValueError, match="center_hint"):
        solve_frame(data=data, header=header, solver=SOLVER_GAIA, gaia_query=query, center_hint=(1.0, 2.0, 3.0))  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="center_hint"):
        solve_frame(data=data, header=header, solver=SOLVER_GAIA, gaia_query=query, center_hint=("a", 1.0))  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="gaia_query"):
        solve_frame(data=data, header=header, solver=SOLVER_GAIA, gaia_query=42)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="either source"):
        solve_frame("some.fits", data=data, header=header, gaia_query=query)
    with pytest.raises(ValueError, match="give a FITS path"):
        solve_frame(gaia_query=query)
    with pytest.raises(TypeError, match="source must be"):
        solve_frame(123, gaia_query=query)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="header must be a mapping"):
        solve_frame(data=data, header=["not", "a", "mapping"], gaia_query=query)  # type: ignore[arg-type]
    with pytest.raises(SolveError, match="2-D"):
        solve_frame(data=np.zeros((4, 4, 4)), header=header, gaia_query=query)


# --- the solve-field subprocess -------------------------------------------------------------

#: A fake ``solve-field`` executable: parses ``--out <prefix>`` and writes a
#: FITS ``<prefix>.wcs`` with a known WCS (the true WCS of the synthetic
#: frame). Env switches: ``FAKE_SOLVE_FAIL=1`` exits 3 with a stderr message,
#: ``FAKE_SOLVE_SLEEP=<s>`` sleeps first, ``FAKE_SOLVE_NO_WCS=1`` exits 0
#: without writing the .wcs.
FAKE_SOLVE_FIELD = f"""#!{sys.executable}
import os
import sys
import time

args = sys.argv[1:]
out = args[args.index("--out") + 1]
# solve-field 0.93 resolves --out relative to the input file's directory
# (it joins dirname(input) + out even for absolute out), so mimic that
out = os.path.join(os.path.dirname(os.path.abspath(args[-1])), out)
if os.environ.get("FAKE_SOLVE_FAIL"):
    print("fake solve-field: no solution found", file=sys.stderr)
    sys.exit(3)
if os.environ.get("FAKE_SOLVE_SLEEP"):
    time.sleep(float(os.environ["FAKE_SOLVE_SLEEP"]))
if not os.environ.get("FAKE_SOLVE_NO_WCS"):
    from astropy.io import fits as pyfits
    header = pyfits.Header({true_header()!r})
    pyfits.PrimaryHDU(header=header).writeto(out + ".wcs")
sys.exit(0)
"""


@pytest.fixture()
def fake_solve_field_bin(tmp_path, monkeypatch) -> Path:
    """A bin dir with the fake ``solve-field`` on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "solve-field"
    exe.write_text(FAKE_SOLVE_FIELD, encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", str(bin_dir))
    return bin_dir


@pytest.fixture()
def empty_bin(tmp_path, monkeypatch) -> Path:
    """A bin dir WITHOUT solve-field on PATH (the Gaia fallback must kick in)."""
    bin_dir = tmp_path / "empty-bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir))
    return bin_dir


def test_solve_frame_solve_field_binary_success(fake_solve_field_bin):
    result = solve_frame(
        data=make_image(), header=blind_header(), solver=SOLVER_SOLVE_FIELD
    )
    assert result.solver == SOLVER_SOLVE_FIELD
    assert result.center_ra_deg == pytest.approx(CRVAL[0], abs=1e-9)
    assert result.center_dec_deg == pytest.approx(CRVAL[1], abs=1e-9)
    for cy, cx, _peak in STARS:
        assert residual_arcsec(result.wcs, cx, cy) < 1e-6
    # the cards round-trip
    assert residual_arcsec(cards_to_wcs(result.cards), 100.0, 150.0) < 1e-6


def test_solve_frame_auto_prefers_solve_field_when_on_path(fake_solve_field_bin):
    result = solve_frame(data=make_image(), header=blind_header(), solver=SOLVER_AUTO)
    assert result.solver == SOLVER_SOLVE_FIELD


def test_solve_frame_auto_falls_back_to_gaia_without_binary(empty_bin):
    query = fake_query(gaia_stars_at_stars())
    result = solve_frame(
        data=make_image(),
        header=blind_header(),
        solver=SOLVER_AUTO,
        gaia_query=query,
        pixel_scale_hint=SCALE_ARCSEC,
    )
    assert result.solver == SOLVER_GAIA
    assert result.n_stars == len(STARS)


def test_solve_frame_solve_field_required_but_missing(empty_bin):
    with pytest.raises(SolveError, match="solve-field"):
        solve_frame(data=make_image(), header=blind_header(), solver=SOLVER_SOLVE_FIELD)


def test_solve_frame_solve_field_failure(fake_solve_field_bin, monkeypatch):
    monkeypatch.setenv("FAKE_SOLVE_FAIL", "1")
    with pytest.raises(SolveError, match="exit code 3.*no solution found"):
        solve_frame(data=make_image(), header=blind_header(), solver=SOLVER_SOLVE_FIELD)


def test_solve_frame_solve_field_timeout(fake_solve_field_bin, monkeypatch):
    monkeypatch.setenv("FAKE_SOLVE_SLEEP", "30")
    with pytest.raises(SolveError, match="timed out"):
        solve_frame(
            data=make_image(),
            header=blind_header(),
            solver=SOLVER_SOLVE_FIELD,
            solve_field_timeout_s=0.5,
        )


def test_solve_frame_solve_field_wrote_no_wcs(fake_solve_field_bin, monkeypatch):
    monkeypatch.setenv("FAKE_SOLVE_NO_WCS", "1")
    with pytest.raises(SolveError, match="no solve.wcs"):
        solve_frame(data=make_image(), header=blind_header(), solver=SOLVER_SOLVE_FIELD)


def test_solve_frame_solve_field_from_fits_path(fake_solve_field_bin, tmp_path):
    path = tmp_path / "sub.fits"
    path.write_bytes(fits_bytes(make_image(), blind_header()))
    result = solve_fits_file(path, solver=SOLVER_SOLVE_FIELD)
    assert result.solver == SOLVER_SOLVE_FIELD
    assert residual_arcsec(result.wcs, 60.0, 90.0) < 1e-6


def test_solve_frame_solve_field_bad_fits_path(tmp_path):
    with pytest.raises(SolveError, match="cannot read the FITS file"):
        solve_fits_file(tmp_path / "missing.fits", solver=SOLVER_GAIA, gaia_query=fake_query([]))


def test_astrometry_module_has_no_gi_after_solving(fake_solve_field_bin):
    # even after a full solve (subprocess + astropy inside functions), the
    # package import itself stayed pure — checked in a fresh subprocess
    code = (
        "import sys;"
        "import seestar_controller.astrometry;"
        "assert 'gi' not in sys.modules;"
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout
    assert astrometry.SOLVERS == (SOLVER_AUTO, SOLVER_SOLVE_FIELD, SOLVER_GAIA)
