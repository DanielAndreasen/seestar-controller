"""Mosaic-stitch ticket 01 — the pure stitch core + session stitching.

Headless: no ``gi`` anywhere (a subprocess test enforces the boundary, and
the source scan audits the package). No network: everything runs on
SYNTHETIC FITS tiles with known WCS offsets stored in a tmp project via
:mod:`seestar_controller.store` (``create_project`` / ``start_session`` /
``store_frame`` — the same pattern as tests/test_frame_quality.py).

The synthetic setup: a 256x256 "sky canvas" with a known CAR WCS
(0.001 deg/px) and Gaussian stars (sigma = 1.5 px, no noise, so the seam
assertions are exact). Tiles are 128x128 cutouts of the canvas at known
offsets, each carrying the WCS of its cutout (CRVAL = the canvas WCS at the
cutout's center). Stitching two overlapping tiles at (0, 0) and (64, 0)
must reproduce the canvas on the common grid — stars at the exact canvas
positions, the overlap seam smooth (average AND feather) — and the output
WCS must map canvas pixels to the same sky. Missing-WCS tiles and
non-overlapping tiles must raise :class:`MosaicError` with a clear message.

Also covered: the core math directly (:func:`stitch_tiles` /
:func:`common_grid` / :func:`tile_wcs` / :func:`tile_offset_px`), the
L-shaped 3-tile mosaic with an uncovered NaN corner, a disconnected tile,
RGB (3-D) tiles, a single tile, mixed-ndim and pixel-scale-mismatch errors,
the session-level errors (unknown session, no stacks, bad parameters,
overwrite=False), the file-based output location (``mosaics/<sid>.fits``,
NOT a ``frames`` row), the optional reproject path (skipped when the
package is absent), the package boundary (subprocess + source scan), and —
app-improvement ticket 08 — that the stitched mosaic is listed and openable
in the Enhance view (``list_project_mosaics`` + ``ViewerController.open_path``,
the open-file path for a file-based product).
"""

import io
import math
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits as pyfits
from astropy.wcs import WCS

from seestar_controller import mosaic
from seestar_controller.enhance import (
    EnhanceError,
    ViewerController,
    list_project_mosaics,
)
from seestar_controller.enhance import (
    fits_io as enhance_fits_io,
)
from seestar_controller.enhance.persistence import (
    load_preset_for,
    save_enhanced,
    save_preset_for,
)
from seestar_controller.enhance.pipeline import default_pipeline
from seestar_controller.mosaic import (
    BLEND_AVERAGE,
    BLEND_FEATHER,
    BLENDS,
    MOSAICS_DIR,
    Grid,
    MosaicError,
    Tile,
    common_grid,
    mosaic_path,
    stitch_session,
    stitch_tiles,
    tile_offset_px,
    tile_wcs,
)
from seestar_controller.store import (
    KIND_STACK,
    SessionNotFoundError,
    create_project,
    list_frames,
    start_session,
    store_frame,
)
from seestar_controller.store.frames import header_to_dict

#: Directory of the mosaic package (source-scan audit).
MOSAIC_PACKAGE_DIR = Path(mosaic.__file__).resolve().parent

#: The PURE modules of the package (no ``gi`` import — enforced below).
PURE_MOSAIC_MODULES = ("core", "session")

T0 = datetime(2026, 11, 20, 22, 0, 0, tzinfo=UTC)

# --- synthetic sky canvas + tiles ---------------------------------------------------------

#: The canvas pixel scale (deg/px).
SCALE = 0.001
#: The synthetic sky canvas size (px).
CANVAS = 256
#: The tile size (px).
TILE = 128
#: The canvas background level.
BACKGROUND = 100.0
#: The Gaussian star sigma (px).
STAR_SIGMA = 1.5
#: Star positions on the canvas as (cy, cx, peak) — well separated, and all
#: inside the region covered by the two-tile mosaic (y < 128, x < 192).
STARS = (
    (30, 30, 20000.0),
    (30, 120, 18000.0),
    (90, 60, 15000.0),
    (90, 170, 12000.0),
    (60, 100, 9000.0),
)


def canvas_header(size: int = CANVAS, proj: str = "CAR") -> dict:
    """The canvas WCS header: CRPIX at the 1-based center, CDELT1 negative
    (RA increases to the left), CAR (or ``proj``) projection."""
    return {
        "CTYPE1": f"RA---{proj}",
        "CTYPE2": f"DEC--{proj}",
        "CRVAL1": 83.0,
        "CRVAL2": 22.0,
        "CRPIX1": (size + 1) / 2.0,
        "CRPIX2": (size + 1) / 2.0,
        "CDELT1": -SCALE,
        "CDELT2": SCALE,
    }


def canvas_wcs(size: int = CANVAS, proj: str = "CAR") -> WCS:
    """The canvas WCS."""
    return WCS(canvas_header(size, proj))


def radec(wcs: WCS, x: float, y: float) -> tuple[float, float]:
    """The (ra, dec) in degrees of pixel ``(x, y)`` of ``wcs``."""
    sky = wcs.pixel_to_world(x, y)
    return float(sky.ra.deg), float(sky.dec.deg)


def make_canvas(size: int = CANVAS, stars=STARS) -> np.ndarray:
    """The synthetic sky canvas: flat background + Gaussian stars, NO noise
    (so the stitched mosaic must reproduce it exactly on covered pixels)."""
    yy, xx = np.mgrid[0:size, 0:size]
    data = np.full((size, size), BACKGROUND, dtype=np.float64)
    for cy, cx, peak in stars:
        data = data + peak * np.exp(-(((yy - cy) ** 2 + (xx - cx) ** 2) / (2.0 * STAR_SIGMA**2)))
    return data


def tile_header(x0: int, y0: int, size: int = TILE, proj: str = "CAR") -> dict:
    """The WCS header of the tile cutout at canvas offset ``(x0, y0)``:
    CRVAL = the canvas sky position of the cutout's center pixel."""
    cw = canvas_wcs(proj=proj)
    ra, dec = radec(cw, x0 + (size - 1) / 2.0, y0 + (size - 1) / 2.0)
    return {
        "CTYPE1": f"RA---{proj}",
        "CTYPE2": f"DEC--{proj}",
        "CRVAL1": float(ra),
        "CRVAL2": float(dec),
        "CRPIX1": (size + 1) / 2.0,
        "CRPIX2": (size + 1) / 2.0,
        "CDELT1": -SCALE,
        "CDELT2": SCALE,
    }


def make_tile(canvas: np.ndarray, x0: int, y0: int, size: int = TILE, proj: str = "CAR") -> Tile:
    """A :class:`Tile` of the canvas cutout at ``(x0, y0)`` with its WCS."""
    return Tile(
        name=f"tile({x0},{y0})",
        data=np.ascontiguousarray(canvas[y0 : y0 + size, x0 : x0 + size]),
        header=tile_header(x0, y0, size, proj),
    )


def date_obs_str(i: int, seconds: int = 10) -> str:
    """The DATE-OBS of tile ``i``: T0 + i*seconds, naive ISOT (UTC)."""
    instant = T0 + timedelta(seconds=seconds * i)
    return instant.isoformat(timespec="milliseconds").replace("+00:00", "")


def tile_fits_bytes(
    canvas: np.ndarray,
    x0: int,
    y0: int,
    i: int,
    *,
    size: int = TILE,
    proj: str = "CAR",
    with_wcs: bool = True,
) -> bytes:
    """A small valid FITS file of the tile cutout at ``(x0, y0)`` (with its
    WCS header unless ``with_wcs=False``) + a distinct DATE-OBS."""
    hdu = pyfits.PrimaryHDU(
        data=np.asarray(canvas[y0 : y0 + size, x0 : x0 + size], dtype=np.float32)
    )
    if with_wcs:
        for key, value in tile_header(x0, y0, size, proj).items():
            hdu.header[key] = value
    hdu.header["DATE-OBS"] = date_obs_str(i)
    hdu.header["EXPTIME"] = 10.0
    hdu.header["OBJECT"] = "M 42"
    buf = io.BytesIO()
    pyfits.HDUList([hdu]).writeto(buf)
    return buf.getvalue()


@pytest.fixture()
def root(tmp_path):
    """A fresh project root."""
    return tmp_path / "projects"


@pytest.fixture()
def project(root):
    """An open project (closed at teardown)."""
    p = create_project("M 42", root=root)
    yield p
    p.close()


def frozen(dt: datetime):
    return lambda: dt


def store_tiles(project, session_id: str, canvas: np.ndarray, offsets, **kwargs) -> list:
    """Store the canvas cutouts at ``offsets`` as the session's tile stacks,
    in order (distinct DATE-OBS per tile)."""
    stored = []
    for i, (x0, y0) in enumerate(offsets):
        stored.append(
            store_frame(
                project,
                session_id,
                KIND_STACK,
                tile_fits_bytes(canvas, x0, y0, i, **kwargs),
            )
        )
    return stored


def read_fits(path: Path):
    """Read a FITS file -> (data, sanitized header dict)."""
    with pyfits.open(path, memmap=False) as hdul:
        return hdul[0].data, header_to_dict(hdul[0].header)


def assert_star_at(mosaic: np.ndarray, cy: int, cx: int, radius: int = 4) -> None:
    """The mosaic's peak inside the window around ``(cy, cx)`` is exactly at
    ``(cy, cx)``."""
    window = mosaic[cy - radius : cy + radius + 1, cx - radius : cx + radius + 1]
    peak = np.unravel_index(int(np.nanargmax(window)), window.shape)
    assert (cy - radius + peak[0], cx - radius + peak[1]) == (cy, cx), (
        f"star expected at ({cy}, {cx}), peak at "
        f"({cy - radius + peak[0]}, {cx - radius + peak[1]})"
    )


# --- session stitching: the happy path ------------------------------------------------------


@pytest.mark.parametrize("blend", [BLEND_AVERAGE, BLEND_FEATHER])
def test_stitch_session_two_overlapping_tiles(project, blend):
    """Two overlapping 128px tiles at (0,0) and (64,0): the stitched FITS
    lives at mosaics/<sid>.fits, has shape (128, 192), reproduces the canvas
    exactly on covered pixels (stars at the exact canvas positions, seam
    smooth), and its WCS maps to the canvas sky."""
    sid = start_session(project, clock=frozen(T0))
    canvas = make_canvas()
    store_tiles(project, sid, canvas, [(0, 0), (64, 0)])

    out = stitch_session(project, sid, blend=blend)

    assert out == project.path / MOSAICS_DIR / f"{sid}.fits"
    assert out.is_file()
    data, header = read_fits(out)
    assert data.shape == (TILE, TILE + 64)
    assert data.dtype == np.dtype(">f4")  # FITS float32 (big-endian)
    mosaic = np.asarray(data, dtype=np.float64)
    covered = ~np.isnan(mosaic)
    # the whole (128, 192) canvas is covered by the two tiles
    assert covered.all()
    # exact reproduction of the canvas (no noise -> exact blend)
    assert np.allclose(mosaic, canvas[:TILE, : TILE + 64], atol=1e-4)
    # stars at the exact common-grid (canvas) positions
    for cy, cx, _peak in STARS:
        assert_star_at(mosaic, cy, cx)
    # the seam (overlap columns 64..127) is smooth: the blend reproduces the
    # canvas exactly there — no discontinuity at the tile boundary
    assert np.allclose(mosaic[:, 64:128], canvas[:TILE, 64:128], atol=1e-4)
    # the output WCS maps canvas pixels to the canvas sky
    out_wcs = WCS(header)
    cw = canvas_wcs()
    for px, py in [(0, 0), (63, 10), (100, 50), (191, 127)]:
        ra_out, dec_out = radec(out_wcs, px, py)
        ra_ref, dec_ref = radec(cw, px, py)
        assert math.isclose(ra_out, ra_ref, abs_tol=1e-4)
        assert math.isclose(dec_out, dec_ref, abs_tol=1e-4)
    # provenance cards
    assert header["NMOSAIC"] == 2
    assert header["BLEND"] == blend
    # the mosaic is NOT registered as a frames row
    frames = list_frames(project, session_id=sid)
    assert len(frames) == 2
    assert all("mosaics" not in f.path for f in frames)


def test_stitch_session_reference_tile_not_at_origin(project):
    """When the FIRST tile (the reference) sits at (64, 0) and the second at
    (0, 0), the canvas origin is min = (-64, 0): the output WCS is the
    reference WCS shifted by (-min_x, -min_y) = (+64, 0), so output pixel
    (x, y) is canvas pixel (x, y) — the mosaic reproduces the canvas and the
    stars stay at their canvas positions."""
    sid = start_session(project, clock=frozen(T0))
    canvas = make_canvas()
    store_tiles(project, sid, canvas, [(64, 0), (0, 0)])

    out = stitch_session(project, sid)

    data, header = read_fits(out)
    mosaic = np.asarray(data, dtype=np.float64)
    assert mosaic.shape == (TILE, TILE + 64)
    assert np.allclose(mosaic, canvas[:TILE, : TILE + 64], atol=1e-4)
    # stars at the exact canvas positions
    for cy, cx, _peak in STARS:
        assert_star_at(mosaic, cy, cx)
    # output pixel (x, y) == canvas pixel (x, y): the CRPIX shift by the
    # negative min_x = -64 lands exactly on the canvas WCS
    assert math.isclose(float(header["CRPIX1"]), (TILE + 1) / 2.0 + 64.0)
    out_wcs = WCS(header)
    cw = canvas_wcs()
    for px, py in [(0, 0), (50, 60), (127, 127), (191, 10)]:
        ra_out, dec_out = radec(out_wcs, px, py)
        ra_ref, dec_ref = radec(cw, px, py)
        # ~0.03 px CAR round-trip noise between the tile and canvas WCS
        assert math.isclose(ra_out, ra_ref, abs_tol=1e-4)
        assert math.isclose(dec_out, dec_ref, abs_tol=1e-4)


def test_stitch_session_three_tiles_l_shape(project):
    """A 3-tile L-shape (0,0), (64,0), (0,64): the mosaic is (192, 192), the
    uncovered corner (x >= 128, y >= 128) is NaN, the rest is the canvas."""
    sid = start_session(project, clock=frozen(T0))
    canvas = make_canvas()
    store_tiles(project, sid, canvas, [(0, 0), (64, 0), (0, 64)])

    out = stitch_session(project, sid)

    data, _header = read_fits(out)
    mosaic = np.asarray(data, dtype=np.float64)
    assert mosaic.shape == (192, 192)
    corner = mosaic[128:, 128:]
    assert np.isnan(corner).all()
    covered = ~np.isnan(mosaic)
    assert covered.sum() == 192 * 192 - 64 * 64
    assert np.allclose(mosaic[covered], canvas[:192, :192][covered], atol=1e-4)


def test_stitch_session_single_tile(project):
    """A single tile stitches to itself (same shape, same data, same WCS)."""
    sid = start_session(project, clock=frozen(T0))
    canvas = make_canvas()
    store_tiles(project, sid, canvas, [(32, 48)])

    out = stitch_session(project, sid)

    data, header = read_fits(out)
    mosaic = np.asarray(data, dtype=np.float64)
    assert mosaic.shape == (TILE, TILE)
    assert np.allclose(mosaic, canvas[48 : 48 + TILE, 32 : 32 + TILE], atol=1e-4)
    out_wcs = WCS(header)
    tile_w = WCS(tile_header(32, 48))
    ra_out, dec_out = radec(out_wcs, 10, 20)
    ra_ref, dec_ref = radec(tile_w, 10, 20)
    assert math.isclose(ra_out, ra_ref, abs_tol=1e-9)
    assert math.isclose(dec_out, dec_ref, abs_tol=1e-9)


def test_stitch_session_rgb_tiles(project):
    """3-D (h, w, 3) RGB tiles stitch to a (h, w, 3) mosaic."""
    sid = start_session(project, clock=frozen(T0))
    canvas = make_canvas()
    rgb = np.stack([canvas, canvas * 0.5, canvas * 0.25], axis=-1)

    def rgb_fits(x0, y0, i):
        hdu = pyfits.PrimaryHDU(
            data=np.asarray(rgb[y0 : y0 + TILE, x0 : x0 + TILE], dtype=np.float32)
        )
        for key, value in tile_header(x0, y0).items():
            hdu.header[key] = value
        hdu.header["DATE-OBS"] = date_obs_str(i)
        buf = io.BytesIO()
        pyfits.HDUList([hdu]).writeto(buf)
        return buf.getvalue()

    store_frame(project, sid, KIND_STACK, rgb_fits(0, 0, 0))
    store_frame(project, sid, KIND_STACK, rgb_fits(64, 0, 1))

    out = stitch_session(project, sid)

    data, _header = read_fits(out)
    assert data.shape == (TILE, TILE + 64, 3)
    mosaic = np.asarray(data, dtype=np.float64)
    assert np.allclose(mosaic, rgb[:TILE, : TILE + 64], atol=1e-4)


def test_stitch_session_tan_projection(project):
    """TAN-projected tiles stitch the same way as CAR."""
    sid = start_session(project, clock=frozen(T0))
    canvas = make_canvas()
    store_tiles(project, sid, canvas, [(0, 0), (64, 0)], proj="TAN")

    out = stitch_session(project, sid)

    data, _header = read_fits(out)
    mosaic = np.asarray(data, dtype=np.float64)
    assert mosaic.shape == (TILE, TILE + 64)
    assert np.allclose(mosaic, canvas[:TILE, : TILE + 64], atol=1e-3)
    for cy, cx, _peak in STARS:
        assert_star_at(mosaic, cy, cx)


# --- session stitching: the error cases ------------------------------------------------------


def test_stitch_session_missing_wcs_tile_names_the_frame(project):
    """A tile stack without a celestial WCS -> MosaicError naming the frame
    (its stored path)."""
    sid = start_session(project, clock=frozen(T0))
    canvas = make_canvas()
    store_tiles(project, sid, canvas, [(0, 0), (64, 0)])
    # a third stack without a WCS (distinct DATE-OBS -> stored last)
    bad = store_frame(
        project, sid, KIND_STACK, tile_fits_bytes(canvas, 64, 0, 99, with_wcs=False)
    )

    with pytest.raises(MosaicError, match=re.escape(bad.path)):
        stitch_session(project, sid)


def test_stitch_session_no_overlap_tiles(project):
    """Two tiles that do not overlap -> MosaicError."""
    sid = start_session(project, clock=frozen(T0))
    canvas = make_canvas()
    store_tiles(project, sid, canvas, [(0, 0), (200, 200)])

    with pytest.raises(MosaicError, match="do not overlap"):
        stitch_session(project, sid)


def test_stitch_session_disconnected_third_tile(project):
    """A third tile disconnected from the overlapping pair -> MosaicError
    naming the lost tile."""
    sid = start_session(project, clock=frozen(T0))
    canvas = make_canvas()
    store_tiles(project, sid, canvas, [(0, 0), (64, 0), (200, 200)])

    with pytest.raises(MosaicError, match="do not overlap") as excinfo:
        stitch_session(project, sid)
    assert "tile(200,200)" in str(excinfo.value) or "frame" in str(excinfo.value)


def test_stitch_session_unknown_session(project):
    with pytest.raises(SessionNotFoundError):
        stitch_session(project, "no-such-session")


def test_stitch_session_no_stacks(project):
    sid = start_session(project, clock=frozen(T0))
    with pytest.raises(MosaicError, match="no tile stacks"):
        stitch_session(project, sid)


def test_stitch_session_bad_parameters(project):
    sid = start_session(project, clock=frozen(T0))
    canvas = make_canvas()
    store_tiles(project, sid, canvas, [(0, 0), (64, 0)])

    with pytest.raises(TypeError, match="session_id"):
        stitch_session(project, 123)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="overwrite"):
        stitch_session(project, sid, overwrite="yes")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    with pytest.raises(MosaicError, match="blend"):
        stitch_session(project, sid, blend="bogus")  # type: ignore[arg-type]
    with pytest.raises(MosaicError, match="feather_px"):
        stitch_session(project, sid, feather_px=0.0)
    # reproject is not installed in this environment -> clear error
    with pytest.raises(MosaicError, match="reproject"):
        stitch_session(project, sid, use_reproject=True)


def test_stitch_session_overwrite_false(project):
    sid = start_session(project, clock=frozen(T0))
    canvas = make_canvas()
    store_tiles(project, sid, canvas, [(0, 0), (64, 0)])

    out = stitch_session(project, sid)
    first = out.read_bytes()
    with pytest.raises(MosaicError, match="overwrite"):
        stitch_session(project, sid, overwrite=False)
    # unchanged
    assert out.read_bytes() == first
    # overwrite=True replaces it
    out2 = stitch_session(project, sid, overwrite=True)
    assert out2 == out


def test_mosaic_path(project):
    assert mosaic_path(project, "sid-1") == project.path / "mosaics" / "sid-1.fits"
    with pytest.raises(TypeError, match="session_id"):
        mosaic_path(project, 42)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


# --- the pure core directly -------------------------------------------------------------------


def test_tile_wcs_and_offset():
    canvas = make_canvas()
    ref = make_tile(canvas, 0, 0)
    other = make_tile(canvas, 64, 32)
    ref_wcs = tile_wcs(ref)
    assert ref_wcs.has_celestial
    ox, oy = tile_offset_px(ref_wcs, other)
    assert math.isclose(ox, 64.0, abs_tol=0.1)
    assert math.isclose(oy, 32.0, abs_tol=0.1)


def test_tile_wcs_missing_celestial_raises_naming_tile():
    tile = Tile(name="tile(no-wcs)", data=np.zeros((8, 8)), header={"SIMPLE": True})
    with pytest.raises(MosaicError, match="no-wcs"):
        tile_wcs(tile)
    with pytest.raises(MosaicError, match="no celestial WCS"):
        tile_wcs(tile)


def test_common_grid_shape_and_offsets():
    canvas = make_canvas()
    tiles = [make_tile(canvas, 0, 0), make_tile(canvas, 64, 0), make_tile(canvas, 0, 64)]
    grid = common_grid(tiles, use_reproject=False)
    assert isinstance(grid, Grid)
    assert grid.shape == (192, 192)
    assert grid.offsets == ((0, 0), (64, 0), (0, 64))
    assert grid.min_x == 0.0 and grid.min_y == 0.0
    assert grid.ref_index == 0
    ox, oy = grid.float_offsets[1]
    assert math.isclose(ox, 64.0, abs_tol=0.1)
    assert math.isclose(oy, 0.0, abs_tol=0.1)


def test_common_grid_no_overlap_and_disconnected():
    canvas = make_canvas()
    with pytest.raises(MosaicError, match="do not overlap"):
        common_grid([make_tile(canvas, 0, 0), make_tile(canvas, 200, 200)], use_reproject=False)
    with pytest.raises(MosaicError, match="do not overlap"):
        common_grid(
            [make_tile(canvas, 0, 0), make_tile(canvas, 64, 0), make_tile(canvas, 200, 200)],
            use_reproject=False,
        )


def test_common_grid_empty_and_bad_tiles():
    with pytest.raises(MosaicError, match="no tiles"):
        common_grid([], use_reproject=False)
    with pytest.raises(MosaicError, match="2-D"):
        common_grid([Tile(name="bad", data=np.zeros(8), header=tile_header(0, 0))], use_reproject=False)
    with pytest.raises(MosaicError, match="non-numeric"):
        common_grid(
            [Tile(name="bad", data=np.array([["x"] * 8] * 8), header=tile_header(0, 0))],
            use_reproject=False,
        )


def test_stitch_tiles_mixed_ndim_raises():
    canvas = make_canvas()
    rgb = np.stack([canvas, canvas], axis=-1)
    tiles = [make_tile(canvas, 0, 0), Tile(name="rgb", data=rgb[:TILE, :TILE], header=tile_header(64, 0))]
    with pytest.raises(MosaicError, match="share the same"):
        stitch_tiles(tiles, use_reproject=False)


def test_stitch_tiles_pixel_scale_mismatch_fallback_raises():
    """In the integer-shift fallback a tile with a different pixel scale
    (2x CDELT) raises a clear MosaicError (reproject would handle it)."""
    canvas = make_canvas()
    header = tile_header(64, 0)
    header["CDELT1"] = -2 * SCALE
    header["CDELT2"] = 2 * SCALE
    tiles = [
        make_tile(canvas, 0, 0),
        Tile(name="tile(2x-scale)", data=canvas[:TILE, 64 : 64 + TILE], header=header),
    ]
    with pytest.raises(MosaicError, match="pixel scale"):
        stitch_tiles(tiles, use_reproject=False)


def test_stitch_tiles_bad_blend_and_feather():
    canvas = make_canvas()
    tiles = [make_tile(canvas, 0, 0)]
    with pytest.raises(MosaicError, match="blend"):
        stitch_tiles(tiles, blend="bogus", use_reproject=False)  # type: ignore[arg-type]
    with pytest.raises(MosaicError, match="feather_px"):
        stitch_tiles(tiles, feather_px=-1.0, use_reproject=False)
    with pytest.raises(MosaicError, match="feather_px"):
        stitch_tiles(tiles, feather_px=float("nan"), use_reproject=False)


def test_stitch_tiles_single_tile_and_out_cards():
    canvas = make_canvas()
    tile = make_tile(canvas, 32, 48)
    mosaic, cards = stitch_tiles([tile], use_reproject=False)
    assert mosaic.shape == (TILE, TILE)
    assert np.allclose(mosaic, canvas[48 : 48 + TILE, 32 : 32 + TILE])
    # the output cards carry the reference WCS (CRPIX unchanged: min = 0)
    assert math.isclose(float(cards["CRPIX1"]), (TILE + 1) / 2.0)
    assert cards["CTYPE1"] == "RA---CAR"
    out_wcs = WCS(cards)
    ref_wcs = WCS(tile_header(32, 48))
    ra_out, dec_out = radec(out_wcs, 5, 6)
    ra_ref, dec_ref = radec(ref_wcs, 5, 6)
    assert math.isclose(ra_out, ra_ref, abs_tol=1e-12)
    assert math.isclose(dec_out, dec_ref, abs_tol=1e-12)


def test_stitch_tiles_feather_weights_blend_identical_overlap():
    """Feather blending of identical overlap data is exact (normalized
    weighted average of equal values)."""
    canvas = make_canvas()
    tiles = [make_tile(canvas, 0, 0), make_tile(canvas, 64, 0)]
    mosaic, _cards = stitch_tiles(tiles, blend=BLEND_FEATHER, feather_px=8.0, use_reproject=False)
    assert mosaic.shape == (TILE, TILE + 64)
    assert np.allclose(mosaic, canvas[:TILE, : TILE + 64], atol=1e-9)


def test_stitch_tiles_reproject_path():
    """The reproject path (only when the optional package is installed)."""
    pytest.importorskip("reproject")
    canvas = make_canvas()
    tiles = [make_tile(canvas, 0, 0), make_tile(canvas, 64, 0)]
    mosaic, _cards = stitch_tiles(tiles, use_reproject=True)
    assert mosaic.shape == (TILE, TILE + 64)
    assert np.allclose(mosaic, canvas[:TILE, : TILE + 64], atol=1e-3)


# --- package boundary (subprocess + source scan) + public API -------------------------------


def test_mosaic_package_imports_no_gi_no_link_no_ui_no_astropy():
    code = (
        "import sys;"
        "import seestar_controller.mosaic;"
        "assert 'gi' not in sys.modules, 'GTK imported';"
        "assert 'seestar_controller.link' not in sys.modules, 'link imported eagerly';"
        "assert 'seestar_controller.ui' not in sys.modules, 'ui imported eagerly';"
        "assert 'seestar_controller.enhance' not in sys.modules, 'enhance imported eagerly';"
        "assert 'astropy' not in sys.modules, 'astropy imported eagerly';"
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


def test_mosaic_package_source_scan_no_gi_and_module_set():
    for path in sorted(MOSAIC_PACKAGE_DIR.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        assert not re.search(
            r"^\s*(import|from)\s+seestar_controller\.(link|ui|enhance)\b", source, re.MULTILINE
        ), f"{path.name} imports a widget/link package"
        assert not re.search(r"^\s*(import gi|from gi)\b", source, re.MULTILINE), (
            f"{path.name} imports gi"
        )
        # heavy third-party imports stay lazy (inside functions): no
        # module-level (column 0) astropy/reproject import
        assert not re.search(r"^(import astropy|from astropy)\b", source, re.MULTILINE), (
            f"{path.name} imports astropy at module level"
        )
        assert not re.search(r"^(import reproject|from reproject)\b", source, re.MULTILINE), (
            f"{path.name} imports reproject at module level"
        )
    modules = {path.stem for path in MOSAIC_PACKAGE_DIR.glob("*.py")}
    assert modules == {"__init__", *PURE_MOSAIC_MODULES}


def test_mosaic_public_api_exports():
    for name in mosaic.__all__:
        assert hasattr(mosaic, name), f"mosaic.{name} missing"
    assert set(mosaic.__all__) == {
        "BLEND_AVERAGE",
        "BLEND_FEATHER",
        "BLENDS",
        "DEFAULT_FEATHER_PX",
        "MOSAICS_DIR",
        "SCALE_TOLERANCE",
        "Grid",
        "MosaicError",
        "Tile",
        "common_grid",
        "mosaic_path",
        "stitch_session",
        "stitch_tiles",
        "tile_offset_px",
        "tile_wcs",
    }
    assert BLENDS == (BLEND_AVERAGE, BLEND_FEATHER)
    assert MosaicError.__mro__[1] is ValueError


# --- app-improvement ticket 08: the stitched mosaic opens in the Enhance view -------------------


def test_stitched_mosaic_is_listed_and_openable_in_enhance_viewer(project):
    """After stitching, the mosaic is a file-based entry of the Enhance view:
    ``list_project_mosaics`` lists it (``frame_id=None``, ``kind="mosaic"``,
    NOT a ``frames`` row) and ``ViewerController.open_path`` opens it — the
    open-file path — with the stitched pixels as the loaded image."""
    sid = start_session(project, clock=frozen(T0))
    canvas = make_canvas()
    store_tiles(project, sid, canvas, [(0, 0), (64, 0)])

    # no mosaic yet: the listing is empty (no mosaics/ directory at all)
    assert list_project_mosaics(project) == []

    out = stitch_session(project, sid)

    mosaics = list_project_mosaics(project)
    assert len(mosaics) == 1
    info = mosaics[0]
    assert info.frame_id is None
    assert info.kind == enhance_fits_io.KIND_MOSAIC
    assert info.session_id == sid
    assert info.path == out
    assert info.key == f"mosaic:{sid}"
    assert "mosaic" in info.label() and sid in info.label()
    assert "mosaic" in info.subtitle() and sid in info.subtitle()
    with pytest.raises(EnhanceError):
        list_project_mosaics(None)

    # the viewer controller lists it among its entries and opens it by path
    vc = ViewerController(project)
    assert info in vc.stacks
    assert vc.mosaics == (info,)
    seen = []
    vc.add_listener(seen.append)
    image = vc.open_path(out)
    assert image.path == out
    assert image.data.shape == (TILE, TILE + 64)
    mosaic = np.asarray(read_fits(out)[0], dtype=np.float64)
    assert np.allclose(np.asarray(image.data, dtype=np.float64), mosaic, atol=1e-4)
    assert vc.current_stack == info
    assert vc.current_stack is not None
    assert vc.current_stack.kind == "mosaic"
    assert vc.current_stack.frame_id is None
    assert vc.preview is not None
    assert vc.snapshot.error is None
    assert seen  # the listeners were notified
    # re-opening an unchanged file is served from the path+mtime cache
    assert vc.open_path(out) is image
    # a relative path resolves against the project directory
    assert vc.open_path(f"{MOSAICS_DIR}/{sid}.fits") is image


def test_stitched_mosaic_open_path_errors_and_arbitrary_file(project, tmp_path):
    """``open_path`` on a missing file raises :class:`EnhanceError` (recorded
    in the snapshot, listeners notified); an arbitrary FITS file outside the
    project opens as a transient ``"file"`` entry."""
    sid = start_session(project, clock=frozen(T0))
    canvas = make_canvas()
    store_tiles(project, sid, canvas, [(0, 0), (64, 0)])
    stitch_session(project, sid)

    vc = ViewerController(project)
    seen = []
    vc.add_listener(seen.append)
    with pytest.raises(EnhanceError):
        vc.open_path(project.path / MOSAICS_DIR / "no-such-session.fits")
    assert vc.snapshot.error is not None
    assert "no-such-session.fits" in vc.snapshot.error
    assert seen and vc.snapshot.current is None

    foreign = tmp_path / "elsewhere.fits"
    pyfits.PrimaryHDU(data=np.asarray(canvas[:16, :16], dtype=np.float32)).writeto(foreign)
    image = vc.open_path(foreign)
    assert image.data.shape == (16, 16)
    assert vc.current_stack is not None
    assert vc.current_stack.kind == enhance_fits_io.KIND_FILE
    assert vc.current_stack.frame_id is None
    assert vc.current_stack.session_id == "elsewhere"


def test_mosaic_entry_has_no_preset_but_the_pipeline_consumes_it(project):
    """A mosaic entry has no frames row, so it has no per-frame preset (a
    clear :class:`EnhanceError` on save, None on load) — but the enhance
    pipeline consumes it unchanged: ``save_enhanced`` stores the enhanced
    mosaic as an ``enhanced`` frame of the stitched session."""
    sid = start_session(project, clock=frozen(T0))
    canvas = make_canvas()
    store_tiles(project, sid, canvas, [(0, 0), (64, 0)])
    out = stitch_session(project, sid)

    vc = ViewerController(project)
    image = vc.open_path(out)
    info = vc.current_stack
    assert info is not None and info.kind == "mosaic"

    assert load_preset_for(project, info) is None
    with pytest.raises(EnhanceError, match="no frames row|presets are per stored frame"):
        save_preset_for(project, info, default_pipeline())

    frame = save_enhanced(project, info, default_pipeline(), image)
    assert frame.kind == "enhanced"
    assert frame.session_id == sid
    assert [f.id for f in list_frames(project, session_id=sid, kind="enhanced")] == [frame.id]
    # the enhanced derivative is a normal frame: listed and openable by id
    assert frame.id in [s.frame_id for s in vc.refresh_stacks()]
    assert vc.open_stack(frame.id).data.shape == (TILE, TILE + 64)
