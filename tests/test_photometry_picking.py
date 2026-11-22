"""Pure-layer tests for photometry ticket 01 — star picking on an image
(:mod:`seestar_controller.photometry.picking`) — and ticket 05 — picking on
REAL Seestar frames (subs without a celestial WCS, RGB stacks with SIP).

Headless: no ``gi`` anywhere (the sandbox has no GTK, see
docs/wiki/assumptions.md). The WCS helpers are exercised with synthetic FITS
headers built via :class:`astropy.wcs.WCS` (CAR — exactly linear — and TAN),
and the save/load round-trip runs on a tmp project via
:mod:`seestar_controller.store` (``create_project`` / ``start_session`` /
``store_frame`` with a synthetic WCS FITS stack — the same pattern as
tests/test_enhance_fits.py).

Ticket 05 regression tests: a synthetic Seestar-style sub header (NAXIS=2, NO
CTYPE cards, the consecutive COMMENT cards real frames carry), a synthetic
RGB stack (NAXIS=3 + SIP distortion keywords), and a small REAL Seestar sub
cropped into tests/fixtures/seestar_sub_no_wcs.fits (tests never read the
user's home dir) — on all of them ``pick_at`` succeeds (pixel-only without a
WCS, sky coords attached on the RGB stack), the picks save/load and measure.

Also covered: the package boundary — a subprocess test (no ``gi``, no
``link``, no ``ui``, no ``enhance`` in ``sys.modules``, the widget layer stays
lazy) + a source-scan audit mirroring tests/test_enhance_fits.py.

The catalog-assist pure layer (ticket 02, :mod:`seestar_controller.photometry.suggest`)
is tested in tests/test_photometry_suggest.py; the widget layer
(``StarPickerView`` + ``SuggestionsPanel``) in tests/test_photometry_widgets.py
(skips headless).
"""

import dataclasses
import io
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits as pyfits

from seestar_controller import photometry
from seestar_controller.catalog.coords import J2000Coord
from seestar_controller.photometry import (
    ApertureParams,
    GotoTarget,
    PickController,
    PickingError,
    PickSession,
    StarPickSpec,
    has_celestial_wcs,
    parse_goto_target,
    picking,
    pixel_to_sky,
    sky_to_pixel,
    try_pixel_to_sky,
)
from seestar_controller.store import (
    KIND_STACK,
    KIND_SUB,
    create_project,
    load_star_picks,
    start_session,
    store_frame,
)

#: Directory of the photometry package (source-scan audit).
PHOTOMETRY_PACKAGE_DIR = Path(photometry.__file__).resolve().parent

#: Test fixtures shipped with the repo (never the user's home dir).
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

#: A small REAL Seestar S50 sub (96x96 crop of a ngc7789 sub, original header
#: kept): NAXIS=2, NO CTYPE cards — no celestial WCS.
REAL_SEESTAR_SUB = FIXTURES_DIR / "seestar_sub_no_wcs.fits"

#: The pure photometry modules — no ``gi`` import allowed (subprocess-enforced).
PURE_PHOTOMETRY_MODULES = ("__init__", "picking", "suggest", "measure", "run")

#: The widget photometry modules — ``gi`` at module top, lazy-exported via PEP 562.
WIDGET_PHOTOMETRY_MODULES = ("star_picker", "suggestions_panel", "results_view")

T0 = datetime(2026, 11, 20, 22, 0, 0, tzinfo=UTC)
DATE1 = "2026-11-20T22:00:00.000"


# --- synthetic WCS headers + images ---------------------------------------------------


def wcs_header(
    *,
    crval: tuple[float, float] = (83.0, 22.0),
    crpix: tuple[float, float] = (51.0, 51.0),
    cdelt: tuple[float, float] = (-0.001, 0.001),
    ctype: tuple[str, str] = ("RA---CAR", "DEC--CAR"),
) -> dict:
    """A plain-dict FITS header with a simple 2-axis WCS (astropy.wcs.WCS).

    CAR is exactly linear (no cos(dec) scaling), so pixel<->sky is exact;
    pass ``ctype=("RA---TAN", "DEC--TAN")`` for the gnomonic projection.
    """
    from astropy.wcs import WCS

    w = WCS(naxis=2)
    w.wcs.ctype = list(ctype)
    w.wcs.crval = list(crval)
    w.wcs.crpix = list(crpix)
    w.wcs.cdelt = list(cdelt)
    return dict(w.to_header())


class FakeImage:
    """A duck-typed FitsImage stand-in (``data`` / ``header`` / ``width`` /
    ``height``) — the pure layer never imports enhance."""

    def __init__(self, data: np.ndarray, header: dict) -> None:
        self.data = np.asarray(data, dtype=np.float32)
        self.header = header

    @property
    def width(self) -> int:
        return int(self.data.shape[1])

    @property
    def height(self) -> int:
        return int(self.data.shape[0])


def synthetic_image(
    size: int = 64,
    stars: tuple[tuple[int, int, float], ...] = ((30, 40, 8000.0), (16, 16, 5000.0)),
    background: float = 10.0,
    noise: float = 1.0,
    seed: int = 1,
) -> np.ndarray:
    """A synthetic image: a flat background + Gaussian star peaks + noise,
    float32. ``stars`` are ``(cy, cx, peak)`` (row, column, peak)."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:size, 0:size]
    data = np.full((size, size), background, dtype=np.float64)
    for cy, cx, peak in stars:
        data = data + peak * np.exp(-(((yy - cy) ** 2 + (xx - cx) ** 2) / 8.0))
    data = data + rng.normal(0.0, noise, size=(size, size))
    return np.ascontiguousarray(data, dtype=np.float32)


def fits_bytes(data: np.ndarray, *, wcs: dict | None = None, date_obs: str = DATE1) -> bytes:
    """A small valid FITS file with the given data + WCS header cards."""
    hdu = pyfits.PrimaryHDU(data=data)
    hdu.header["DATE-OBS"] = date_obs
    hdu.header["EXPTIME"] = 10.0
    hdu.header["GAIN"] = 50
    hdu.header["OBJECT"] = "M 42"
    if wcs is not None:
        for key, value in wcs.items():
            hdu.header[key] = value
    buf = io.BytesIO()
    pyfits.HDUList([hdu]).writeto(buf)
    return buf.getvalue()


def seestar_sub_header() -> dict:
    """A synthetic Seestar S50 SUB header (ticket 05): NAXIS=2 with NO CTYPE
    cards (no celestial WCS), the two consecutive COMMENT cards real frames
    carry (joined value contains ``\\n``), and the RA/DEC object cards (which
    are NOT WCS cards)."""
    return {
        "SIMPLE": True,
        "BITPIX": 16,
        "NAXIS": 2,
        "NAXIS1": 64,
        "NAXIS2": 64,
        "EXTEND": True,
        "COMMENT": (
            "  FITS (Flexible Image Transport System) format is defined in 'Astronomy\n"
            "  and Astrophysics', volume 376, page 359; bibcode: 2001A&A...376..359H"
        ),
        "BZERO": 32768,
        "BSCALE": 1,
        "CREATOR": "ZWO Seestar S50",
        "PRODUCER": "ZWO",
        "IMAGETYP": "Light",
        "EXPOSURE": 10.0,
        "EXPTIME": 10.0,
        "GAIN": 80,
        "RA": 359.47083,
        "DEC": 56.726111,
        "DATE-OBS": "2026-11-20T21:33:33.024310",
        "FILTER": "IRCUT",
        "INSTRUME": "Seestar S50",
        "OBJECT": "M 31",
    }


def rgb_stack_header(*, sip: bool = True, wcsaxes: int = 3) -> dict:
    """A synthetic Seestar RGB STACK header (ticket 05): NAXIS=3 (color axis
    3) with a celestial WCS on axes 1-2 and, when ``sip``, SIP distortion
    keywords — a plain ``WCS(dict)`` raises on this ("SIP distortions only
    work in 2 dimensions")."""
    from astropy.wcs import WCS

    w = WCS(naxis=3)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN", "STOKES"]
    w.wcs.crval = [83.0, 22.0, 1.0]
    w.wcs.crpix = [51.0, 51.0, 1.0]
    w.wcs.cdelt = [-0.001, 0.001, 1.0]
    header = dict(w.to_header())
    header.update(
        {
            "NAXIS": 3,
            "NAXIS1": 64,
            "NAXIS2": 64,
            "NAXIS3": 3,
            "BITPIX": -32,
            "WCSAXES": wcsaxes,
        }
    )
    if sip:
        header.update(
            {
                "A_ORDER": 2,
                "B_ORDER": 2,
                "A_2_0": 1e-5,
                "B_0_2": 1e-5,
                "A_1_1": 1e-6,
                "B_1_1": -1e-6,
            }
        )
    return header


def rgb_image(size: int = 64) -> np.ndarray:
    """A synthetic RGB image: the synthetic frame stacked to (size, size, 3)."""
    return np.stack([synthetic_image(size)] * 3, axis=-1)


def frozen(dt: datetime):
    return lambda: dt


@pytest.fixture()
def root(tmp_path):
    return tmp_path / "projects"


@pytest.fixture()
def project(root):
    p = create_project("M 42 night", root=root)
    yield p
    p.close()


@pytest.fixture()
def wcs_stack_project(project):
    """A tmp project with one session and one stored stack carrying a WCS."""
    session = start_session(project, clock=frozen(T0))
    frame = store_frame(
        project,
        session,
        KIND_STACK,
        fits_bytes(synthetic_image(64), wcs=wcs_header(), date_obs=DATE1),
        meta={"target": "M 42", "exposure": 10.0, "gain": 50},
    )
    return project, frame


# --- WCS pixel <-> sky ------------------------------------------------------------------


def test_pixel_to_sky_at_reference_pixel_is_crval():
    header = wcs_header(crval=(83.0, 22.0), crpix=(51.0, 51.0))
    # 0-based pixel (50, 50) == 1-based WCS pixel (51, 51) == CRPIX -> CRVAL
    coord = pixel_to_sky(header, 50, 50)
    assert isinstance(coord, J2000Coord)
    assert coord.ra_deg == pytest.approx(83.0, abs=1e-9)
    assert coord.dec_deg == pytest.approx(22.0, abs=1e-9)


def test_pixel_to_sky_car_is_linear():
    header = wcs_header(crval=(83.0, 22.0), crpix=(51.0, 51.0), cdelt=(-0.001, 0.001))
    # at the reference column the RA stays CRVAL; dec moves linearly with y
    coord = pixel_to_sky(header, 50, 40)
    assert coord.ra_deg == pytest.approx(83.0, abs=1e-9)
    assert coord.dec_deg == pytest.approx(22.0 - 10 * 0.001, abs=1e-9)
    # moving in x moves the RA (cdelt1 < 0 -> westward)
    off = pixel_to_sky(header, 60, 50)
    assert off.ra_deg < 83.0 and off.dec_deg == pytest.approx(22.0, abs=1e-6)


def test_pixel_to_sky_tan_projection_dec_exact():
    header = wcs_header(ctype=("RA---TAN", "DEC--TAN"))
    coord = pixel_to_sky(header, 50, 50)
    assert coord.dec_deg == pytest.approx(22.0, abs=1e-9)
    # TAN scales RA by cos(dec) away from the reference pixel
    off = pixel_to_sky(header, 60, 50)
    assert off.ra_deg == pytest.approx(83.0 - 10 * 0.001 / np.cos(np.radians(22.0)), rel=1e-6)


def test_sky_pixel_round_trip():
    header = wcs_header()
    for x, y in ((0.0, 0.0), (50.0, 50.0), (63.5, 12.25), (10.0, 60.0)):
        coord = pixel_to_sky(header, x, y)
        rx, ry = sky_to_pixel(header, coord)
        assert rx == pytest.approx(x, abs=1e-6)
        assert ry == pytest.approx(y, abs=1e-6)


def test_sky_to_pixel_accepts_pair_and_duck_typed_coord():
    header = wcs_header()
    coord = pixel_to_sky(header, 20.0, 30.0)
    assert sky_to_pixel(header, (coord.ra_deg, coord.dec_deg)) == pytest.approx(
        sky_to_pixel(header, coord)
    )

    class Duck:
        ra_deg = coord.ra_deg
        dec_deg = coord.dec_deg

    assert sky_to_pixel(header, Duck()) == pytest.approx(sky_to_pixel(header, coord))
    with pytest.raises(PickingError):
        sky_to_pixel(header, "not a coord")
    with pytest.raises(PickingError):
        sky_to_pixel(header, (1.0, 2.0, 3.0))


def test_pixel_to_sky_missing_wcs_raises():
    for bad in ({}, None, {"NAXIS": 2}, {"CTYPE1": "", "CTYPE2": ""}, "nope", 42):
        with pytest.raises(PickingError):
            pixel_to_sky(bad, 0, 0)  # ty: ignore[invalid-argument-type]
    with pytest.raises(PickingError):
        sky_to_pixel({}, J2000Coord(ra_deg=83.0, dec_deg=22.0))


def test_wcs_helpers_sanitize_header_values_with_control_or_non_ascii_chars():
    """Regression: real Seestar FITS carry two consecutive COMMENT cards, so
    ``dict(astropy_header)["COMMENT"]`` contains ``\\n``; ``astropy.wcs.WCS``
    re-inserts every card of the given mapping into a fresh Header and raised
    ``ValueError`` ("FITS header values must contain standard printable ASCII
    characters") on such a value. The WCS helpers must sanitize the mapping
    first, so picking works on real Seestar frames."""
    header = wcs_header()
    header["COMMENT"] = (
        "FITS (Flexible Image Transport System) format is defined in 'Astronomy\n"
        "  and Astrophysics', volume 376, page 359; bibcode: 2001A&A...376..359H"
    )
    header["OBJECT"] = "M 42\nNGC 7789"  # control char inside a value
    header["NOTE"] = "caf\u00e9"  # non-ASCII char inside a value
    coord = pixel_to_sky(header, 50, 50)
    assert coord.ra_deg == pytest.approx(83.0, abs=1e-9)
    assert coord.dec_deg == pytest.approx(22.0, abs=1e-9)
    x, y = sky_to_pixel(header, coord)
    assert (x, y) == pytest.approx((50.0, 50.0), abs=1e-6)


def test_pixel_to_sky_bad_xy_raises():
    header = wcs_header()
    for bad in (float("nan"), float("inf"), "x", None, True):
        with pytest.raises(PickingError):
            pixel_to_sky(header, bad, 0)  # ty: ignore[invalid-argument-type]
        with pytest.raises(PickingError):
            pixel_to_sky(header, 0, bad)  # ty: ignore[invalid-argument-type]


# --- ApertureParams + StarPickSpec --------------------------------------------------------


def test_aperture_params_defaults_validation_and_frozen():
    ap = ApertureParams()
    assert (ap.radius_px, ap.sky_in_px, ap.sky_out_px) == (4.0, 8.0, 12.0)
    assert ApertureParams(radius_px=5, sky_in_px=10, sky_out_px=20).radius_px == 5.0
    for bad in (
        {"radius_px": 0.0},
        {"radius_px": -1.0},
        {"radius_px": float("nan")},
        {"radius_px": True},
        {"sky_in_px": 0.0},
        {"sky_out_px": -2.0},
        {"sky_in_px": 12.0, "sky_out_px": 8.0},  # inner must be < outer
        {"sky_in_px": 8.0, "sky_out_px": 8.0},
    ):
        with pytest.raises(PickingError):
            ApertureParams(**bad)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ap.radius_px = 9.0  # type: ignore[misc]  # ty: ignore[invalid-assignment]


def test_star_pick_spec_validation_describe_and_frozen():
    coord = J2000Coord(ra_deg=83.0, dec_deg=22.0)
    spec = StarPickSpec(role="target", x=40, y=30, coord=coord, label="TIC 1")
    assert spec.x == 40.0 and spec.y == 30.0
    text = spec.describe()
    assert "TIC 1" in text and "target" in text and "(40.0, 30.0)" in text
    pixel_only = StarPickSpec(role="comp", x=1, y=2)
    assert pixel_only.coord is None and "RA" not in pixel_only.describe()
    for bad in (
        {"role": "bogus", "x": 0, "y": 0},
        {"role": "target", "x": float("nan"), "y": 0},
        {"role": "target", "x": 0, "y": float("inf")},
        {"role": "target", "x": 0, "y": 0, "coord": "83,22"},
        {"role": "target", "x": 0, "y": 0, "label": 42},
    ):
        with pytest.raises(PickingError):
            StarPickSpec(**bad)  # ty: ignore[invalid-argument-type]
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.x = 1.0  # type: ignore[misc]  # ty: ignore[invalid-assignment]


# --- PickController: picking ---------------------------------------------------------------


def test_pick_at_requires_image_and_bounds_and_validates_role():
    header = wcs_header()
    ctrl = PickController()
    with pytest.raises(PickingError, match="no image"):
        ctrl.pick_at(1, 1, "target")
    ctrl.set_image(FakeImage(synthetic_image(32), header))
    for x, y in ((-1.0, 0.0), (32.0, 0.0), (0.0, -1.0), (0.0, 32.0)):
        with pytest.raises(PickingError, match="outside"):
            ctrl.pick_at(x, y, "target")
    with pytest.raises(PickingError, match="role"):
        ctrl.pick_at(1, 1, "bogus")
    with pytest.raises(PickingError):
        ctrl.pick_at(float("nan"), 1, "target")


def test_pick_at_snaps_to_bright_centroid_and_converts_to_sky():
    header = wcs_header()
    image = FakeImage(synthetic_image(64, stars=((30, 40, 8000.0),)), header)
    ctrl = PickController()
    ctrl.set_image(image)
    spec = ctrl.pick_at(42, 32, "target", label="Target")
    # snapped onto the star at (x=40, y=30)
    assert spec.x == pytest.approx(40.0, abs=0.75)
    assert spec.y == pytest.approx(30.0, abs=0.75)
    assert spec.role == "target" and spec.label == "Target"
    # the coord is the WCS position of the snapped pixel
    expected = pixel_to_sky(header, spec.x, spec.y)
    assert spec.coord is not None
    assert spec.coord.ra_deg == pytest.approx(expected.ra_deg, abs=1e-9)
    assert spec.coord.dec_deg == pytest.approx(expected.dec_deg, abs=1e-9)
    assert ctrl.picks == (spec,)
    assert ctrl.target is spec


def test_pick_at_snap_false_and_flat_or_nan_window_use_exact_pixel():
    header = wcs_header()
    ctrl = PickController()
    ctrl.set_image(FakeImage(synthetic_image(64, stars=((30, 40, 8000.0),)), header))
    exact = ctrl.pick_at(42, 32, "comp", snap=False)
    assert (exact.x, exact.y) == (42.0, 32.0)
    # flat window: no source -> exact click pixel even with snap=True
    flat = FakeImage(np.full((32, 32), 5.0, dtype=np.float32), header)
    ctrl.set_image(flat)
    spec = ctrl.pick_at(7, 9, "comp")
    assert (spec.x, spec.y) == (7.0, 9.0)
    # all-NaN window: exact click pixel
    nan_img = FakeImage(np.full((32, 32), np.nan, dtype=np.float32), header)
    nan_img.data[10, 10] = 1.0  # outside the click window
    ctrl.set_image(nan_img)
    spec = ctrl.pick_at(3, 3, "comp")
    assert (spec.x, spec.y) == (3.0, 3.0)


# --- ticket 05: real Seestar frames — subs without WCS, RGB stacks with SIP -------------------


def test_pick_at_on_seestar_sub_without_wcs_is_pixel_only():
    """A raw Seestar sub (NAXIS=2, no CTYPE cards) has no celestial WCS:
    ``pick_at`` still works — the pick is pixel-only (``coord=None``) — while
    the strict :func:`pixel_to_sky` keeps raising :class:`PickingError`."""
    header = seestar_sub_header()
    assert not has_celestial_wcs(header)
    assert try_pixel_to_sky(header, 40, 30) is None
    with pytest.raises(PickingError, match="no WCS"):
        pixel_to_sky(header, 40, 30)
    with pytest.raises(PickingError, match="plate-solve"):
        sky_to_pixel(header, J2000Coord(ra_deg=83.0, dec_deg=22.0))

    ctrl = PickController()
    ctrl.set_image(FakeImage(synthetic_image(64, stars=((30, 40, 8000.0),)), header))
    spec = ctrl.pick_at(42, 32, "target", label="Target")
    assert spec.coord is None  # pixel-only pick
    assert spec.x == pytest.approx(40.0, abs=0.75)  # still snapped to the star
    assert spec.y == pytest.approx(30.0, abs=0.75)
    assert ctrl.target is spec and "RA" not in spec.describe()
    comp = ctrl.pick_at(16, 16, "comp", label="Comp A", snap=False)
    assert comp.coord is None and ctrl.comps == (comp,)
    # add_at_sky needs a WCS (a sky position cannot map to a pixel without
    # one) — a clear plate-solve hint, not a crash
    with pytest.raises(PickingError, match="plate-solve"):
        ctrl.add_at_sky(J2000Coord(ra_deg=83.0, dec_deg=22.0))


def test_pick_at_on_rgb_stack_with_sip_attaches_sky_coords():
    """An RGB stack (NAXIS=3 + SIP keywords) breaks a plain ``WCS(dict)``;
    the WCS is built from the 2D celestial subset (color axis dropped, SIP
    kept in 2D), so ``pick_at`` attaches sky coords as on any WCS frame."""
    header = rgb_stack_header(sip=True)
    assert has_celestial_wcs(header)
    # the strict helpers work on the RGB header directly
    coord = pixel_to_sky(header, 50, 50)  # 0-based (50,50) == CRPIX -> CRVAL
    assert coord.ra_deg == pytest.approx(83.0, abs=1e-9)
    assert coord.dec_deg == pytest.approx(22.0, abs=1e-9)
    x, y = sky_to_pixel(header, coord)
    assert (x, y) == pytest.approx((50.0, 50.0), abs=1e-6)

    ctrl = PickController()
    ctrl.set_image(FakeImage(rgb_image(64), header))
    spec = ctrl.pick_at(42, 32, "target", label="Target")
    assert spec.coord is not None  # sky coords attached on the RGB stack
    assert spec.x == pytest.approx(40.0, abs=0.75)  # snapped (channel mean)
    assert spec.y == pytest.approx(30.0, abs=0.75)
    expected = pixel_to_sky(header, spec.x, spec.y)
    assert spec.coord.ra_deg == pytest.approx(expected.ra_deg, abs=1e-9)
    assert spec.coord.dec_deg == pytest.approx(expected.dec_deg, abs=1e-9)
    # round-trip through the RGB WCS
    rx, ry = sky_to_pixel(header, spec.coord)
    assert (rx, ry) == pytest.approx((spec.x, spec.y), abs=1e-6)


def test_rgb_stack_variants_wcs_build():
    """The WCS build tolerates the RGB-stack header shapes: 3D WCS without
    SIP (celestial subset), WCSAXES=2 on 3D data, and SIP stripped."""
    from astropy.wcs import WCS

    for header in (
        rgb_stack_header(sip=False),  # 3D WCS, no SIP -> .celestial subset
        rgb_stack_header(sip=True, wcsaxes=2),  # WCSAXES=2 on NAXIS=3 data
    ):
        assert has_celestial_wcs(header)
        coord = pixel_to_sky(header, 50, 50)
        assert coord.ra_deg == pytest.approx(83.0, abs=1e-9)
        assert coord.dec_deg == pytest.approx(22.0, abs=1e-9)
    # a plain WCS(dict) really raises on the SIP variant (the regression)
    with pytest.raises(Exception, match="2 dimensions"):
        WCS(rgb_stack_header(sip=True))
    # non-celestial 2D axes are rejected (no usable CELESTIAL WCS)
    assert not has_celestial_wcs({"CTYPE1": "X", "CTYPE2": "Y"})
    assert not has_celestial_wcs({"CTYPE1": "RA---TAN", "CTYPE2": "X"})


def test_real_seestar_sub_fixture_picks_pixel_only_and_measures():
    """The real-FITS fixture (a 96x96 crop of a real Seestar S50 sub from the
    ngc7789 project, original header): no CTYPE -> pixel-only picks that save,
    load and MEASURE. Tests read only tests/fixtures/, never the home dir."""
    with pyfits.open(REAL_SEESTAR_SUB) as hdul:
        data = np.asarray(hdul[0].data)
        header = hdul[0].header
    assert "CTYPE1" not in header  # the real sub really has no WCS
    assert not has_celestial_wcs(header)
    image = FakeImage(data, header)
    ctrl = PickController()
    ctrl.set_image(image)
    # click on the brightest pixel of the real frame (a real star)
    iy, ix = np.unravel_index(int(np.argmax(data)), data.shape)
    spec = ctrl.pick_at(float(ix), float(iy), "target", label="Target")
    assert spec.coord is None
    assert spec.x == pytest.approx(float(ix), abs=1.5)
    assert spec.y == pytest.approx(float(iy), abs=1.5)
    # a comp on the second-brightest pixel away from the target (another star)
    masked = np.array(data, dtype=np.float64)
    masked[max(0, iy - 12) : iy + 13, max(0, ix - 12) : ix + 13] = -np.inf
    jy, jx = np.unravel_index(int(np.argmax(masked)), masked.shape)
    comp = ctrl.pick_at(float(jx), float(jy), "comp", label="Comp A", snap=False)
    assert comp.coord is None

    # the pixel-only picks are measurable (measure needs no WCS)
    from seestar_controller.photometry import measure_frame_data

    fm = measure_frame_data(
        np.asarray(image.data, dtype=np.float64),
        header,
        ctrl.picks,
        jd=2461365.0,
    )
    assert fm.mag is not None and np.isfinite(fm.mag)


def test_pixel_only_picks_save_load_and_measure_on_tmp_project(root):
    """End-to-end on a tmp project with a stored no-WCS Seestar-style sub:
    pick target + comp (pixel-only), save, reload, measure the stored frame."""
    from seestar_controller.photometry import measure_frame

    project = create_project("ngc7789 night", root=root)
    try:
        session = start_session(project, clock=frozen(T0))
        frame = store_frame(
            project,
            session,
            KIND_SUB,
            fits_bytes(synthetic_image(64, stars=((30, 40, 8000.0), (16, 16, 5000.0))), wcs=None),
            meta={"target": "M 31", "exposure": 10.0, "gain": 80},
        )
        ctrl = PickController(project=project)
        ctrl.set_image(FakeImage(synthetic_image(64, stars=((30, 40, 8000.0), (16, 16, 5000.0))), seestar_sub_header()))
        target = ctrl.pick_at(42, 32, "target", label="Target")
        comp = ctrl.pick_at(16, 16, "comp", label="Comp A", snap=False)
        assert target.coord is None and comp.coord is None

        saved = ctrl.save()
        assert [p.role for p in saved] == ["target", "comp"]
        stored = load_star_picks(project)
        assert stored[0].ra_deg is None and stored[0].dec_deg is None  # NULL sky coords
        assert stored[1].ra_deg is None and stored[1].dec_deg is None
        assert project.config["star_picks"]["target"]["ra_deg"] is None

        # a fresh controller restores the pixel-only picks
        reloaded = PickController(project=project)
        assert [p.role for p in reloaded.picks] == ["target", "comp"]
        assert all(p.coord is None for p in reloaded.picks)
        assert reloaded.picks[0].x == pytest.approx(target.x)

        # ... and the stored no-WCS sub frame measures fine with them
        m = measure_frame(project, frame.id, reloaded.picks)
        assert m.mag is not None and np.isfinite(m.mag)
        assert m.mag_err is not None and np.isfinite(m.mag_err)
    finally:
        project.close()


def test_pick_at_target_replaces_and_comps_accumulate():
    header = wcs_header()
    ctrl = PickController()
    ctrl.set_image(FakeImage(synthetic_image(64), header))
    t1 = ctrl.pick_at(10, 10, "target", snap=False)
    c1 = ctrl.pick_at(20, 20, "comp", snap=False, label="Comp A")
    c2 = ctrl.pick_at(30, 30, "comp", snap=False, label="Comp B")
    t2 = ctrl.pick_at(40, 40, "target", snap=False)
    assert ctrl.picks == (c1, c2, t2)  # the first target was replaced
    assert ctrl.target is t2 and t1 not in ctrl.picks
    assert ctrl.comps == (c1, c2)


def test_remove_pick_set_role_clear():
    header = wcs_header()
    ctrl = PickController()
    ctrl.set_image(FakeImage(synthetic_image(64), header))
    t = ctrl.pick_at(10, 10, "target", snap=False)
    c1 = ctrl.pick_at(20, 20, "comp", snap=False)
    c2 = ctrl.pick_at(30, 30, "comp", snap=False)
    # set_role to target replaces the other target
    moved = ctrl.set_role(1, "target")
    assert moved.role == "target"
    assert ctrl.picks == (moved, c2) and ctrl.target is moved
    with pytest.raises(PickingError):
        ctrl.set_role(0, "bogus")
    # remove (negatives allowed)
    removed = ctrl.remove_pick(-1)
    assert removed is c2
    assert ctrl.picks == (moved,)
    with pytest.raises(PickingError):
        ctrl.remove_pick(5)
    with pytest.raises(PickingError):
        ctrl.remove_pick("0")  # ty: ignore[invalid-argument-type]
    ctrl.clear()
    assert ctrl.picks == () and ctrl.target is None
    assert t is not None and c1 is not None  # silence linters


def test_aperture_setters():
    ctrl = PickController()
    ap = ctrl.set_aperture_params(radius_px=6.0, sky_out_px=20.0)
    assert (ap.radius_px, ap.sky_in_px, ap.sky_out_px) == (6.0, 8.0, 20.0)
    assert ctrl.aperture is ap
    with pytest.raises(PickingError):
        ctrl.set_aperture_params(sky_in_px=30.0)  # 30 > sky_out 20
    assert ctrl.aperture.sky_in_px == 8.0  # unchanged on error
    with pytest.raises(PickingError):
        ctrl.set_aperture("nope")  # ty: ignore[invalid-argument-type]
    assert ctrl.set_aperture(ApertureParams(radius_px=2.0)).radius_px == 2.0


# --- listeners + frozen snapshot ------------------------------------------------------------


def test_listeners_idempotent_decorator_isolated_and_frozen_snapshot():
    header = wcs_header()
    ctrl = PickController()
    ctrl.set_image(FakeImage(synthetic_image(32), header))
    calls: list[PickSession] = []

    @ctrl.add_listener
    def on_session(snapshot: PickSession) -> None:
        calls.append(snapshot)

    ctrl.add_listener(on_session)  # idempotent
    assert ctrl.listeners == (on_session,)

    def boom(_snapshot: PickSession) -> None:
        raise RuntimeError("listener bug")

    ctrl.add_listener(boom)
    ctrl.pick_at(5, 5, "target", snap=False)  # boom is isolated
    assert len(calls) == 1
    snapshot = calls[0]
    assert isinstance(snapshot, PickSession)
    assert snapshot.image is ctrl.image
    assert snapshot.picks == ctrl.picks
    assert snapshot.aperture == ctrl.aperture
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.picks = ()  # type: ignore[misc]  # ty: ignore[invalid-assignment]
    ctrl.remove_listener(on_session)
    ctrl.remove_listener(on_session)  # no-op
    ctrl.clear()
    assert len(calls) == 1
    assert ctrl.listeners == (boom,)


# --- persistence (save/load round-trip on a tmp project) --------------------------------------


def test_save_load_round_trip_on_tmp_project(wcs_stack_project):
    from seestar_controller.enhance import ViewerController

    project, frame = wcs_stack_project
    vc = ViewerController(project)
    vc.refresh_stacks()
    vc.open_stack(frame.id)
    ctrl = PickController(project=project, viewer_controller=vc)
    assert ctrl.image is vc.current_image  # followed the viewer's open stack

    target = ctrl.pick_at(40, 30, "target", label="Target")
    comp_a = ctrl.pick_at(16, 16, "comp", label="Comp A")
    comp_b = ctrl.pick_at(50, 12, "comp", label="Comp B")
    ctrl.set_aperture_params(radius_px=6.0, sky_in_px=10.0, sky_out_px=18.0)

    saved = ctrl.save()
    assert [p.role for p in saved] == ["target", "comp", "comp"]
    # the store rows carry the session aperture + sky coords
    stored = load_star_picks(project)
    assert [p.role for p in stored] == ["target", "comp", "comp"]  # target first
    assert stored[0].label == "Target"
    assert stored[0].ra_deg == pytest.approx(target.coord.ra_deg)  # ty: ignore[missing-union-attribute]
    assert stored[0].dec_deg == pytest.approx(target.coord.dec_deg)  # ty: ignore[missing-union-attribute]
    assert (stored[0].aperture_radius, stored[0].sky_annulus_inner, stored[0].sky_annulus_outer) == (
        6.0,
        10.0,
        18.0,
    )
    # the project.json mirror
    mirror = project.config["star_picks"]
    assert mirror["target"]["role"] == "target" and mirror["target"]["label"] == "Target"
    assert [c["label"] for c in mirror["comps"]] == ["Comp A", "Comp B"]

    # a fresh controller on the same project restores picks + aperture
    reloaded = PickController(project=project)
    assert reloaded.image is None  # no viewer attached
    picks = reloaded.picks
    assert [p.role for p in picks] == ["target", "comp", "comp"]
    assert [p.label for p in picks] == ["Target", "Comp A", "Comp B"]
    assert picks[0].coord is not None
    assert picks[0].coord.ra_deg == pytest.approx(target.coord.ra_deg)  # ty: ignore[missing-union-attribute]
    assert picks[0].x == pytest.approx(target.x) and picks[0].y == pytest.approx(target.y)
    assert (picks[1].coord.ra_deg, picks[1].coord.dec_deg) == pytest.approx(  # ty: ignore[missing-union-attribute]
        (comp_a.coord.ra_deg, comp_a.coord.dec_deg)  # ty: ignore[missing-union-attribute]
    )
    assert comp_b.coord is not None
    assert (reloaded.aperture.radius_px, reloaded.aperture.sky_in_px, reloaded.aperture.sky_out_px) == (
        6.0,
        10.0,
        18.0,
    )
    # explicit load() also works and notifies
    assert reloaded.load() == picks


def test_save_load_errors_and_empty_save_clears(wcs_stack_project):
    from seestar_controller.enhance import ViewerController

    project, frame = wcs_stack_project
    vc = ViewerController(project)
    vc.refresh_stacks()
    vc.open_stack(frame.id)

    ctrl = PickController(viewer_controller=vc)
    with pytest.raises(PickingError, match="no project"):
        ctrl.save()
    with pytest.raises(PickingError, match="no project"):
        ctrl.load()

    ctrl.set_project(project)
    ctrl.pick_at(40, 30, "target", label="Target")
    ctrl.pick_at(16, 16, "comp", label="Comp A")
    ctrl.save()
    assert len(load_star_picks(project)) == 2

    # comps-only ensemble is rejected by the store (exactly one target); the
    # failed save leaves the previous picks untouched (replace + rollback)
    ctrl.clear()
    ctrl.pick_at(16, 16, "comp", label="Comp A")
    with pytest.raises(PickingError, match="exactly one"):
        ctrl.save()
    assert len(load_star_picks(project)) == 2

    # an empty save clears the stored picks (set_project reloaded the 2 picks;
    # re-picking the target replaces the stored target -> still 2 picks)
    ctrl.set_project(project)
    ctrl.pick_at(40, 30, "target")
    ctrl.save()
    assert len(load_star_picks(project)) == 2
    ctrl.clear()
    assert ctrl.save() == []
    assert load_star_picks(project) == []
    assert project.config["star_picks"] == {"target": None, "comps": [], "check": None}

    # set_project(None) clears the session
    ctrl.set_project(None)
    assert ctrl.project is None and ctrl.picks == ()


def test_controller_follows_viewer_open_stack(wcs_stack_project):
    from seestar_controller.enhance import ViewerController

    project, frame = wcs_stack_project
    vc = ViewerController(project)
    ctrl = PickController(viewer_controller=vc)  # attached before any open
    assert ctrl.image is None
    vc.refresh_stacks()
    assert vc.stacks and vc.stacks[0].frame_id == frame.id
    image = vc.open_stack(frame.id)
    assert ctrl.image is image  # adopted via the viewer listener
    spec = ctrl.pick_at(40, 30, "target")
    assert spec.coord is not None
    # the coord matches the WCS of the stored FITS header
    expected = pixel_to_sky(image.header, spec.x, spec.y)
    assert spec.coord.ra_deg == pytest.approx(expected.ra_deg, abs=1e-9)
    # re-attaching detaches the previous viewer
    other = ViewerController()
    ctrl.attach_viewer(other)
    assert ctrl.viewer_controller is other
    vc.open_stack(frame.id)  # no longer adopted
    assert ctrl.image is image


# --- issue 06: parse_goto_target (the picker's "go to RA/Dec or name" entry) --------------


def test_parse_goto_target_coords_comma_sexagesimal_and_decimal():
    t = parse_goto_target("5h32m00s, +22d00m00s")
    assert t == GotoTarget(kind="coords", coord=J2000Coord(ra_deg=83.0, dec_deg=22.0), name="")
    assert t.coord is not None and t.coord.ra_deg == pytest.approx(83.0)
    t = parse_goto_target("  5.5333333  +22.0  ")  # whitespace-separated, decimal
    assert t.kind == "coords"
    assert t.coord is not None and t.coord.ra_deg == pytest.approx(83.0, abs=1e-4)
    assert t.coord.dec_deg == pytest.approx(22.0)
    # a plain RA number is HOURS (the link.Device.goto unit), like J2000Coord.parse
    t = parse_goto_target("83.0, 22.0")
    assert t.kind == "coords"
    assert t.coord is not None and t.coord.ra_deg == pytest.approx((83.0 % 24.0) * 15.0)


def test_parse_goto_target_names():
    for text in ("M 42", "NGC1976", "V* bet Per", "m42", "  M   42  "):
        t = parse_goto_target(text)
        assert t.kind == "name"
        assert t.coord is None
        assert t.name == text.strip()


def test_parse_goto_target_errors():
    with pytest.raises(PickingError, match="string"):
        parse_goto_target(123)
    with pytest.raises(PickingError, match="enter a sky coordinate"):
        parse_goto_target("   ")
    # a comma-separated pair that does not parse is a malformed coordinate,
    # not a name
    with pytest.raises(PickingError, match="cannot parse"):
        parse_goto_target("83.0, abc")
    with pytest.raises(PickingError, match="cannot parse"):
        parse_goto_target("12h00m00s, 95d00m00s")  # Dec out of range


# --- package boundary (subprocess + source scan) ----------------------------------------------


def test_pure_photometry_modules_import_no_gi_no_link_no_ui_no_enhance_and_widget_stays_lazy():
    code = (
        "import sys;"
        "import seestar_controller.photometry;"
        "import seestar_controller.photometry.picking;"
        "import seestar_controller.photometry.suggest;"
        "import seestar_controller.photometry.measure;"
        "import seestar_controller.photometry.run;"
        "assert 'gi' not in sys.modules, 'GTK imported';"
        "assert 'seestar_controller.link' not in sys.modules, 'link imported eagerly';"
        "assert 'seestar_controller.ui' not in sys.modules, 'ui imported eagerly';"
        "assert 'seestar_controller.enhance' not in sys.modules, 'enhance imported eagerly';"
        "assert 'seestar_controller.photometry.star_picker' not in sys.modules, "
        "'star_picker imported eagerly';"
        "assert 'seestar_controller.photometry.suggestions_panel' not in sys.modules, "
        "'suggestions_panel imported eagerly';"
        "assert 'seestar_controller.photometry.results_view' not in sys.modules, "
        "'results_view imported eagerly';"
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


def test_every_photometry_module_is_pure_or_widget_and_lazy_exported():
    modules = {path.stem for path in PHOTOMETRY_PACKAGE_DIR.glob("*.py")}
    assert modules == set(PURE_PHOTOMETRY_MODULES) | set(WIDGET_PHOTOMETRY_MODULES)
    for name in PURE_PHOTOMETRY_MODULES:
        source = (PHOTOMETRY_PACKAGE_DIR / f"{name}.py").read_text(encoding="utf-8")
        assert not re.search(r"^\s*(import gi|from gi)\b", source, re.MULTILINE), (
            f"pure module {name} imports gi"
        )
    for name in WIDGET_PHOTOMETRY_MODULES:
        lines = (PHOTOMETRY_PACKAGE_DIR / f"{name}.py").read_text(encoding="utf-8").splitlines()
        gi_lines = [i for i, line in enumerate(lines) if line.strip() == "import gi"]
        assert gi_lines, f"widget module {name} has no 'import gi' at module top"
        repo_line = next(
            i for i, line in enumerate(lines) if line.startswith("from gi.repository")
        )
        assert gi_lines[0] < repo_line, (
            f"widget module {name}: 'import gi' must come before 'from gi.repository'"
        )
    assert set(photometry._WIDGET_EXPORTS.values()) == set(WIDGET_PHOTOMETRY_MODULES)


def test_public_api_is_exported_from_package():
    for name in (
        "ApertureParams",
        "CompSuggestion",
        "DEFAULT_APERTURE_RADIUS_PX",
        "DEFAULT_COMP_SEARCH_RADIUS_DEG",
        "DEFAULT_SKY_IN_PX",
        "DEFAULT_SKY_OUT_PX",
        "GotoTarget",
        "LookupFn",
        "MAX_COMP_SEARCH_RADIUS_DEG",
        "NO_WCS_HINT",
        "PhotometryError",
        "PickController",
        "PickListener",
        "PickSession",
        "PickingError",
        "ROLE_CHECK",
        "ROLE_COMP",
        "ROLE_TARGET",
        "ROLES",
        "SNAP_WINDOW_PX",
        "StarPickSpec",
        "SuggestListener",
        "SuggestionController",
        "SuggestionError",
        "SuggestionSnapshot",
        "default_standards_lookup",
        "has_celestial_wcs",
        "parse_goto_target",
        "pixel_to_sky",
        "sky_to_pixel",
        "try_pixel_to_sky",
    ):
        assert name in photometry.__all__, name
        assert hasattr(photometry, name), name
    assert "StarPickerView" in photometry.__all__
    assert "SuggestionsPanel" in photometry.__all__
    assert "ResultsPanel" in photometry.__all__
    assert photometry._WIDGET_EXPORTS == {
        "StarPickerView": "star_picker",
        "SuggestionsPanel": "suggestions_panel",
        "ResultsPanel": "results_view",
    }
    assert photometry.PickController is picking.PickController
    assert photometry.pixel_to_sky is picking.pixel_to_sky
    assert issubclass(photometry.PhotometryError, ValueError)
    assert issubclass(photometry.PickingError, photometry.PhotometryError)
    assert issubclass(photometry.SuggestionError, photometry.PhotometryError)
    with pytest.raises(AttributeError):
        photometry.no_such_thing  # type: ignore[attr-defined]  # noqa: B018


# --- check-star role in the pick session (check-star-plot ticket 01) -------------------------


def test_star_pick_spec_accepts_check_role():
    spec = StarPickSpec(role="check", x=12.0, y=50.0, label="Check K")
    assert spec.role == "check" and "check" in spec.describe()
    assert photometry.ROLE_CHECK == "check"
    assert "check" in photometry.ROLES


def test_pick_at_check_replaces_and_check_property():
    header = wcs_header()
    ctrl = PickController()
    ctrl.set_image(FakeImage(synthetic_image(64), header))
    ctrl.pick_at(10, 10, "target", snap=False)
    ctrl.pick_at(20, 20, "comp", snap=False, label="Comp A")
    k1 = ctrl.pick_at(30, 30, "check", snap=False, label="Check K")
    assert ctrl.check is k1
    assert [p.role for p in ctrl.picks] == ["target", "comp", "check"]
    # re-picking the check star replaces the existing check pick
    k2 = ctrl.pick_at(40, 40, "check", snap=False, label="Check K2")
    assert ctrl.check is k2 and k1 not in ctrl.picks
    assert [p.role for p in ctrl.picks] == ["target", "comp", "check"]
    # set_role to check replaces the other check pick too
    moved = ctrl.set_role(1, "check")
    assert moved.role == "check"
    assert ctrl.check is moved
    assert [p.role for p in ctrl.picks] == ["target", "check"]
    # set_role back to comp: the check slot is free again
    back = ctrl.set_role(1, "comp")
    assert ctrl.check is None and back.role == "comp"
    assert ctrl.comps == (back,)


def test_check_pick_save_load_round_trip_on_tmp_project(wcs_stack_project):
    project, _frame = wcs_stack_project
    ctrl = PickController(project=project)
    ctrl.set_image(FakeImage(synthetic_image(64), wcs_header()))
    ctrl.pick_at(40, 30, "target", label="Target")
    ctrl.pick_at(16, 16, "comp", label="Comp A")
    check = ctrl.pick_at(12, 50, "check", label="Check K")

    saved = ctrl.save()
    assert [p.role for p in saved] == ["target", "comp", "check"]
    stored = load_star_picks(project)
    # store load order: target first, then the check star, then the comps
    assert [p.role for p in stored] == ["target", "check", "comp"]
    assert stored[1].label == "Check K" and stored[1].role == "check"
    mirror = project.config["star_picks"]
    assert mirror["check"]["role"] == "check" and mirror["check"]["label"] == "Check K"
    assert [c["role"] for c in mirror["comps"]] == ["comp"]

    reloaded = PickController(project=project)
    assert [p.role for p in reloaded.picks] == ["target", "check", "comp"]
    assert reloaded.check is not None
    assert reloaded.check.label == "Check K"
    assert reloaded.check.x == pytest.approx(check.x)
    assert reloaded.check.y == pytest.approx(check.y)
    assert reloaded.check.coord is not None
    assert reloaded.check.coord.ra_deg == pytest.approx(check.coord.ra_deg)  # ty: ignore[missing-union-attribute]
