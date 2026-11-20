"""Tests for seestar_controller.gaia.detect — source detection + WCS on a
synthetic session STACK. No network, no gi (the package is pure)."""

from __future__ import annotations

import io
import math
import warnings
from typing import Any, cast

import numpy as np
import pytest
from astropy.io import fits as pyfits

from seestar_controller.gaia import detect as gd
from seestar_controller.gaia.errors import DetectionError, WcsMissingError

# --- synthetic stack ------------------------------------------------------------

SIZE = 128
CENTER_RA = 83.633
CENTER_DEC = 22.014
SCALE_DEG_PER_PIX = 0.01  # 36 arcsec/pix
BACKGROUND = 1000.0
NOISE = 5.0
PSF_SIGMA = 2.0

#: Injected stars as (y, x, peak).
STARS = [
    (30.0, 40.0, 5000.0),
    (80.0, 20.0, 3000.0),
    (60.0, 90.0, 2500.0),
    (100.0, 70.0, 2200.0),
]


def make_stack(
    stars: list[tuple[float, float, float]] = STARS,
    *,
    size: int = SIZE,
    seed: int = 42,
    background: float = BACKGROUND,
    noise: float = NOISE,
    sigma: float = PSF_SIGMA,
) -> np.ndarray:
    """A synthetic stack: flat background + Gaussian stars + noise, float32."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:size, 0:size]
    data = np.full((size, size), background, dtype=np.float64)
    for cy, cx, peak in stars:
        data = data + peak * np.exp(-(((yy - cy) ** 2 + (xx - cx) ** 2) / (2.0 * sigma**2)))
    data = data + rng.normal(0.0, noise, size=(size, size))
    return np.ascontiguousarray(data, dtype=np.float32)


def wcs_header(
    size: int = SIZE,
    *,
    crval: tuple[float, float] = (CENTER_RA, CENTER_DEC),
    scale: float = SCALE_DEG_PER_PIX,
) -> pyfits.Header:
    """A TAN WCS header centered on ``crval`` with ``scale`` deg/pixel."""
    header = pyfits.Header()
    header["CTYPE1"] = "RA---TAN"
    header["CTYPE2"] = "DEC--TAN"
    header["CRVAL1"] = crval[0]
    header["CRVAL2"] = crval[1]
    header["CRPIX1"] = size / 2 + 0.5
    header["CRPIX2"] = size / 2 + 0.5
    header["CDELT1"] = -scale
    header["CDELT2"] = scale
    return header


def stack_fits_bytes(data: np.ndarray, header: pyfits.Header) -> bytes:
    """Serialize the synthetic stack as a real FITS file (round-trip helper)."""
    hdu = pyfits.PrimaryHDU(data=np.asarray(data, dtype=np.float32), header=header)
    buf = io.BytesIO()
    pyfits.HDUList([hdu]).writeto(buf)
    return buf.getvalue()


def read_fits(path) -> tuple[np.ndarray, pyfits.Header]:
    with pyfits.open(path, memmap=False) as hdul:
        return np.asarray(hdul[0].data, dtype=np.float64), hdul[0].header.copy()


def star_sky(header, x: float, y: float) -> tuple[float, float]:
    """The J2000 sky position of an injected star (0-based pixel)."""
    coord = gd.pixel_to_sky(header, x, y)
    return coord.ra_deg, coord.dec_deg


# --- detection --------------------------------------------------------------------


def test_detect_sources_finds_the_injected_stars_with_sky_positions():
    data = make_stack()
    header = wcs_header()
    sources = gd.detect_sources(data, header)
    assert len(sources) >= len(STARS)
    for cy, cx, peak in STARS:
        matches = [s for s in sources if abs(s.x - cx) <= 1.5 and abs(s.y - cy) <= 1.5]
        assert len(matches) == 1, f"star at ({cx}, {cy}) not detected exactly once"
        source = matches[0]
        ra, dec = star_sky(header, cx, cy)
        assert source.ra_deg == pytest.approx(ra, abs=1e-3)
        assert source.dec_deg == pytest.approx(dec, abs=1e-3)
        assert source.peak == pytest.approx(peak + BACKGROUND, rel=0.05)
        assert source.flux > 0.0
    # sorted brightest first
    fluxes = [s.flux for s in sources]
    assert fluxes == sorted(fluxes, reverse=True)


def test_detect_sources_uses_the_header_wcs_not_the_pixel_grid():
    data = make_stack()
    header = wcs_header(crval=(10.0, -20.0))
    sources = gd.detect_sources(data, header)
    star = next(s for s in sources if abs(s.x - 40.0) <= 1.5 and abs(s.y - 30.0) <= 1.5)
    ra, dec = star_sky(header, 40.0, 30.0)
    assert star.ra_deg == pytest.approx(ra, abs=1e-3)
    assert star.dec_deg == pytest.approx(dec, abs=1e-3)
    assert 9.0 < star.ra_deg < 11.0 and -21.0 < star.dec_deg < -19.0


def test_detect_sources_missing_wcs_raises_plate_solve_first():
    data = make_stack()
    header = pyfits.Header()  # no CTYPE cards at all
    header["OBJECT"] = "M 42"
    with pytest.raises(WcsMissingError) as excinfo:
        gd.detect_sources(data, header)
    assert "plate-solve" in str(excinfo.value)
    # None header too
    with pytest.raises(WcsMissingError):
        gd.detect_sources(data, None)
    # a non-mapping header
    with pytest.raises(WcsMissingError):
        gd.detect_sources(data, cast(Any, ["not", "a", "mapping"]))


def test_detect_sources_blank_ctype_is_also_missing_wcs():
    data = make_stack()
    header = wcs_header()
    header["CTYPE1"] = ""
    header["CTYPE2"] = ""
    with pytest.raises(WcsMissingError):
        gd.detect_sources(data, header)


def test_detect_sources_threshold_is_configurable():
    data = make_stack()
    header = wcs_header()
    default = gd.detect_sources(data, header, threshold=5.0)
    # a huge threshold: nothing is detected (a normal empty result; photutils
    # warns "No sources were found" — silence it here)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert gd.detect_sources(data, header, threshold=1e6) == []
    # a lower threshold detects at least as many sources
    low = gd.detect_sources(data, header, threshold=3.0)
    assert len(low) >= len(default)
    with pytest.raises(DetectionError):
        gd.detect_sources(data, header, threshold=0)
    with pytest.raises(DetectionError):
        gd.detect_sources(data, header, threshold=-1.0)
    with pytest.raises(DetectionError):
        gd.detect_sources(data, header, threshold=float("nan"))


def test_detect_sources_explicit_fwhm_and_bad_fwhm():
    data = make_stack()
    header = wcs_header()
    sources = gd.detect_sources(data, header, fwhm=4.0)
    assert len(sources) >= len(STARS)
    for bad in cast(Any, (0.0, -2.0, float("inf"), "wide")):
        with pytest.raises(DetectionError):
            gd.detect_sources(data, header, fwhm=bad)


def test_detect_sources_handles_nan_pixels():
    data = make_stack()
    data[0:8, 0:8] = np.nan
    data[60, 60] = np.inf
    header = wcs_header()
    sources = gd.detect_sources(data, header)
    assert len(sources) >= len(STARS)
    assert all(math.isfinite(s.ra_deg) and math.isfinite(s.dec_deg) for s in sources)


def test_detect_sources_bad_image_raises():
    header = wcs_header()
    with pytest.raises(DetectionError):
        gd.detect_sources(np.zeros((4,)), header)  # 1-D
    with pytest.raises(DetectionError):
        gd.detect_sources(np.zeros((0, 0)), header)  # empty
    with pytest.raises(DetectionError):
        gd.detect_sources(np.full((8, 8), np.nan), header)  # no finite pixel
    with pytest.raises(DetectionError):
        gd.detect_sources("not an array", header)


def test_detect_sources_accepts_a_plain_dict_header():
    data = make_stack()
    header = dict(wcs_header())
    sources = gd.detect_sources(data, header)
    assert len(sources) >= len(STARS)


def test_detect_sources_on_a_real_fits_round_trip(tmp_path):
    data = make_stack()
    header = wcs_header()
    path = tmp_path / "stack.fits"
    path.write_bytes(stack_fits_bytes(data, header))
    loaded_data, loaded_header = read_fits(path)
    sources = gd.detect_sources(loaded_data, loaded_header)
    assert len(sources) >= len(STARS)
    star = next(s for s in sources if abs(s.x - 40.0) <= 1.5 and abs(s.y - 30.0) <= 1.5)
    ra, dec = star_sky(header, 40.0, 30.0)
    assert star.ra_deg == pytest.approx(ra, abs=1e-3)
    assert star.dec_deg == pytest.approx(dec, abs=1e-3)


# --- FWHM estimate ------------------------------------------------------------------


def test_estimate_fwhm_from_the_image():
    data = make_stack(sigma=2.0)
    fwhm = gd.estimate_fwhm(data)
    # true FWHM = 2.355 * 2 = 4.71 px; the second-moment estimate is close
    assert 3.0 < fwhm < 8.0


def test_estimate_fwhm_fallbacks_and_clamps():
    flat = np.full((32, 32), 1000.0)  # no blob at all
    assert gd.estimate_fwhm(flat) == gd.DEFAULT_FWHM_PX
    # one huge blob (sigma 15 px -> FWHM ~35 px) -> clamped to MAX_FWHM_PX
    yy, xx = np.mgrid[0:128, 0:128]
    rng = np.random.default_rng(1)
    blob = 1000.0 + 5000.0 * np.exp(-(((yy - 64) ** 2 + (xx - 64) ** 2) / (2 * 15.0**2)))
    blob = blob + rng.normal(0.0, 1.0, size=(128, 128))
    assert gd.estimate_fwhm(blob) == gd.MAX_FWHM_PX
    with pytest.raises(DetectionError):
        gd.estimate_fwhm(np.zeros((4,)), threshold_sigma=5.0)
    with pytest.raises(DetectionError):
        gd.estimate_fwhm(np.full((8, 8), 1.0), threshold_sigma=0)


# --- WCS helpers ----------------------------------------------------------------------


def test_pixel_to_sky_center_and_star():
    header = wcs_header()
    center = gd.pixel_to_sky(header, (SIZE - 1) / 2.0, (SIZE - 1) / 2.0)
    assert center.ra_deg == pytest.approx(CENTER_RA, abs=1e-6)
    assert center.dec_deg == pytest.approx(CENTER_DEC, abs=1e-6)
    coord = gd.pixel_to_sky(header, 40.0, 30.0)
    # 24 px in x, 30 px in y at 0.01 deg/pix -> ~0.384 deg from the center
    from seestar_controller.catalog.coords import angular_distance

    assert angular_distance(center, coord) == pytest.approx(
        math.hypot(24.0, 30.0) * SCALE_DEG_PER_PIX, abs=0.03
    )
    with pytest.raises(DetectionError):
        gd.pixel_to_sky(header, float("nan"), 3.0)
    with pytest.raises(WcsMissingError):
        gd.pixel_to_sky(pyfits.Header(), 1.0, 1.0)


def test_field_center_and_radius_covers_the_whole_field():
    header = wcs_header()
    center, radius_deg = gd.field_center_and_radius(header, (SIZE, SIZE), margin_deg=0.05)
    assert center.ra_deg == pytest.approx(CENTER_RA, abs=1e-6)
    assert center.dec_deg == pytest.approx(CENTER_DEC, abs=1e-6)
    # half the diagonal of a 128 px * 0.01 deg field ~ 0.905 deg + margin
    assert 0.9 < radius_deg < 1.0
    # the corners are inside the cone
    from seestar_controller.catalog.coords import angular_distance

    for cx, cy in ((0.0, 0.0), (SIZE - 1.0, 0.0), (0.0, SIZE - 1.0), (SIZE - 1.0, SIZE - 1.0)):
        corner = gd.pixel_to_sky(header, cx, cy)
        assert angular_distance(center, corner) <= radius_deg
    # margin is added
    _, radius_no_margin = gd.field_center_and_radius(header, (SIZE, SIZE), margin_deg=0.0)
    assert radius_deg == pytest.approx(radius_no_margin + 0.05)
    with pytest.raises(WcsMissingError):
        gd.field_center_and_radius(pyfits.Header(), (SIZE, SIZE))
    with pytest.raises(DetectionError):
        gd.field_center_and_radius(header, (0, 10))
    with pytest.raises(DetectionError):
        gd.field_center_and_radius(header, (SIZE, SIZE), margin_deg=-1.0)
