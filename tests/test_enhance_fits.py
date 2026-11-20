"""Image-enhance ticket 01: pure-layer tests — FITS loading, tone mapping,
project-stack listing, and the viewer controller. No gi, no GTK, no network.

Covers the pure layer of ``seestar_controller.enhance``:

- :func:`~seestar_controller.enhance.fits_io.load_fits` — synthetic FITS files
  written with astropy (a known gradient + star peaks + NaN corners): shape /
  dtype / values / header round-trip, BZERO (uint16 round-trip), BSCALE+BZERO
  and BLANK patched at the byte level (astropy strips manually-set scaling
  cards on write, so the tests patch the 80-byte header cards directly), RGB
  ``(3, H, W)`` -> ``(H, W, 3)``, and the error cases (missing file, garbage
  bytes, no image data, unsupported shape) -> :class:`EnhanceError`;
- :func:`~seestar_controller.enhance.fits_io.list_project_stacks` — a tmp
  project with stored stacks (via ``store_frame``) + a sub-frame that must
  NOT appear: frame ids, session, absolute path, jd/utc, target/exposure/gain,
  labels; None project -> EnhanceError; empty project -> [];
- :func:`~seestar_controller.enhance.render.tonemap` — monotonicity, default
  percentile lo/hi, explicit lo/hi, NaN -> 0, uint8 range, every stretch,
  asinh brighter than linear at mid, gamma darkens, degenerate/constant/all-NaN
  images, the RGB path, and bad-parameter errors;
- :func:`~seestar_controller.enhance.render.to_pil_image` /
  :func:`~seestar_controller.enhance.render.downscale` /
  :func:`~seestar_controller.enhance.render.render_preview` — modes, mean
  pooling, downscale-before-tonemap fast path (a 2048² stack renders in well
  under 0.5 s);
- :class:`~seestar_controller.enhance.viewer_controller.ViewerController` —
  stack listing, open_stack (current image + preview), the path+mtime FITS
  cache, unknown id / corrupt file / no project errors recorded in the
  snapshot, set_project, frozen snapshots, listener add/remove/idempotency and
  isolation of a raising listener;
- the package boundary: a subprocess test (no ``gi``, no ``link``, no ``ui``,
  the widget layer stays lazy) + a source-scan audit mirroring
  tests/test_observing_queue.py.

The widget tests live in tests/test_enhance_widgets.py and skip without ``gi``.
"""

import dataclasses
import io
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits as pyfits
from astropy.time import Time
from PIL import Image

import seestar_controller.enhance as enhance
from seestar_controller.enhance import (
    EnhanceError,
    FitsImage,
    StackInfo,
    ViewerController,
    ViewerSnapshot,
    downscale,
    list_project_stacks,
    load_fits,
    render_preview,
    to_pil_image,
    tonemap,
)
from seestar_controller.enhance import fits_io, render, viewer_controller
from seestar_controller.store import (
    KIND_STACK,
    KIND_SUB,
    create_project,
    get_frame_path,
    start_session,
    store_frame,
)

#: Directory of the enhance package (source-scan audit).
ENHANCE_PACKAGE_DIR = Path(enhance.__file__).resolve().parent

#: The pure enhance modules — no ``gi`` import allowed (subprocess-enforced).
PURE_ENHANCE_MODULES = ("__init__", "fits_io", "render", "viewer_controller")

#: The widget enhance modules — ``gi`` at module top, lazy-exported via PEP 562.
WIDGET_ENHANCE_MODULES = ("viewer",)


T0 = datetime(2026, 11, 20, 22, 0, 0, tzinfo=timezone.utc)
DATE1 = "2026-11-20T22:00:00.000"
DATE2 = "2026-11-20T22:00:10.000"
DATE3 = "2026-11-20T22:00:20.500"


def frozen(dt: datetime):
    """A frozen clock returning ``dt`` on every call."""
    return lambda: dt


def jd_of(date_obs: str) -> float:
    """The expected JD of a DATE-OBS string (astropy Time.jd)."""
    return float(Time(date_obs, format="isot", scale="utc").jd)


def synthetic_image(size: int = 64, seed: int = 1, with_nan: bool = True) -> np.ndarray:
    """A synthetic stack image: a smooth gradient + a few star peaks + noise,
    float32, with NaN at the four corners when ``with_nan``."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:size, 0:size]
    data = ((xx + yy) * 10.0).astype(np.float32)  # gradient 0 .. (2*size-2)*10
    for cy, cx, peak in ((16, 16, 5000.0), (40, 48, 8000.0), (50, 12, 3000.0)):
        data = data + (peak * np.exp(-(((yy - cy) ** 2 + (xx - cx) ** 2) / 8.0))).astype(
            np.float32
        )
    data = data + rng.normal(0.0, 5.0, size=(size, size)).astype(np.float32)
    if with_nan:
        data[0, 0] = np.nan
        data[0, -1] = np.nan
        data[-1, 0] = np.nan
        data[-1, -1] = np.nan
    return np.ascontiguousarray(data, dtype=np.float32)


def fits_bytes(
    data: np.ndarray,
    *,
    date_obs: str = DATE1,
    exptime: float = 10.0,
    gain: float = 50,
    obj: str = "M 42",
) -> bytes:
    """A small valid FITS file (astropy.io.fits) with the given data + header
    cards — the same pattern tests/test_store_frames.py uses."""
    hdu = pyfits.PrimaryHDU(data=data)
    hdu.header["DATE-OBS"] = date_obs
    hdu.header["EXPTIME"] = float(exptime)
    hdu.header["GAIN"] = gain
    hdu.header["OBJECT"] = obj
    buf = io.BytesIO()
    pyfits.HDUList([hdu]).writeto(buf)
    return buf.getvalue()


def write_fits(path: Path, data: np.ndarray, **kwargs) -> Path:
    path.write_bytes(fits_bytes(data, **kwargs))
    return path


def _insert_header_cards(fits_bytes_: bytes, cards: list[tuple[str, object]]) -> bytes:
    """Insert 80-byte header cards before the END card of the primary HDU's
    first 2880-byte block, keeping the block size (astropy strips manually-set
    BZERO/BSCALE/BLANK cards on write, so the scaling tests patch the bytes
    directly)."""
    block = bytearray(fits_bytes_[:2880])
    end = block.find(b"END".ljust(80))
    assert end != -1 and end % 80 == 0, "no END card in the first header block"
    insert = b"".join(pyfits.Card(key, value).image.encode("ascii") for key, value in cards)
    n = len(cards)
    new_block = bytes(block[:end]) + insert + bytes(block[end : 2880 - 80 * n])
    assert len(new_block) == 2880
    return new_block + fits_bytes_[2880:]


# --- fixtures: a tmp project with stored stacks ---------------------------------------


@pytest.fixture()
def root(tmp_path):
    """A fresh project root."""
    return tmp_path / "projects"


@pytest.fixture()
def project(root):
    """One created project, closed after the test."""
    p = create_project("M 42 night", root=root)
    yield p
    p.close()


@pytest.fixture()
def session(project):
    """One open session with a deterministic start time."""
    return start_session(project, clock=frozen(T0))


@pytest.fixture()
def stored_stacks(project, session):
    """Two stored stacks (different DATE-OBS) + one sub-frame; returns
    ``([stack_frame_1, stack_frame_2], sub_frame)``."""
    s1 = store_frame(
        project,
        session,
        KIND_STACK,
        fits_bytes(synthetic_image(32, seed=1), date_obs=DATE1, obj="M 42"),
        meta={"target": "M 42", "exposure": 10.0, "gain": 50},
    )
    s2 = store_frame(
        project,
        session,
        KIND_STACK,
        fits_bytes(synthetic_image(32, seed=2), date_obs=DATE2, obj="M 31"),
        meta={"target": "M 31"},  # exposure/gain fall back to the FITS header
    )
    sub = store_frame(
        project, session, KIND_SUB, fits_bytes(synthetic_image(16, seed=3), date_obs=DATE3)
    )
    return [s1, s2], sub


# --- load_fits ---------------------------------------------------------------------------


def test_load_fits_basic_roundtrip(tmp_path):
    data = synthetic_image(64, seed=1)
    path = write_fits(tmp_path / "stack.fits", data)
    image = load_fits(path)
    assert isinstance(image, FitsImage)
    assert image.path == path
    assert image.shape == (64, 64)
    assert image.height == 64 and image.width == 64
    assert not image.is_rgb
    assert image.data.dtype == np.float32
    np.testing.assert_allclose(image.data, data, rtol=1e-6, equal_nan=True)
    # NaN corners are preserved as NaN
    assert np.isnan(image.data[0, 0]) and np.isnan(image.data[-1, -1])
    # header cards come through as a plain dict
    assert image.header["DATE-OBS"] == DATE1
    assert float(image.header["EXPTIME"]) == 10.0
    assert float(image.header["GAIN"]) == 50
    assert image.header["OBJECT"] == "M 42"


def test_load_fits_accepts_str_path(tmp_path):
    path = write_fits(tmp_path / "stack.fits", synthetic_image(16, seed=2))
    image = load_fits(str(path))
    assert image.shape == (16, 16)


def test_load_fits_uint16_bzero_roundtrip(tmp_path):
    """A uint16 FITS is stored as int16 + BZERO=32768; load_fits applies BZERO
    manually and returns the original physical values."""
    rng = np.random.default_rng(7)
    data = rng.integers(0, 65535, size=(24, 24)).astype(np.uint16)
    path = write_fits(tmp_path / "u16.fits", data)
    image = load_fits(path)
    assert float(image.header["BZERO"]) == 32768.0
    np.testing.assert_allclose(image.data, data.astype(np.float32), rtol=1e-6)


def test_load_fits_bscale_bzero_patched_cards(tmp_path):
    """BSCALE/BZERO patched into the header bytes are applied:
    physical = raw * BSCALE + BZERO."""
    raw = np.arange(16, dtype=np.int16).reshape(4, 4) * 100 - 500  # int16, no scaling on write
    path = tmp_path / "scaled.fits"
    path.write_bytes(_insert_header_cards(fits_bytes(raw), [("BSCALE", 2.0), ("BZERO", 100.0)]))
    image = load_fits(path)
    expected = raw.astype(np.float32) * 2.0 + 100.0
    np.testing.assert_allclose(image.data, expected, rtol=1e-6)


def test_load_fits_blank_maps_to_nan(tmp_path):
    """Integer data with a BLANK card: pixels equal to BLANK become NaN."""
    raw = np.arange(16, dtype=np.int16).reshape(4, 4)
    raw[1, 2] = -999
    raw[3, 0] = -999
    path = tmp_path / "blank.fits"
    path.write_bytes(_insert_header_cards(fits_bytes(raw), [("BLANK", -999)]))
    image = load_fits(path)
    assert np.isnan(image.data[1, 2]) and np.isnan(image.data[3, 0])
    assert not np.isnan(image.data[0, 0])
    np.testing.assert_allclose(
        image.data[0, 0], np.float32(raw[0, 0]), rtol=1e-6
    )


def test_load_fits_rgb_axis_normalized(tmp_path):
    """FITS RGB data is stored as (3, H, W); load_fits normalizes to (H, W, 3)."""
    rng = np.random.default_rng(3)
    data = rng.uniform(0, 1000, size=(3, 20, 30)).astype(np.float32)
    path = write_fits(tmp_path / "rgb.fits", data)
    image = load_fits(path)
    assert image.is_rgb
    assert image.shape == (20, 30, 3)
    np.testing.assert_allclose(
        image.data, np.transpose(data, (1, 2, 0)), rtol=1e-6, equal_nan=True
    )


def test_load_fits_nonfinite_becomes_nan(tmp_path):
    data = synthetic_image(16, seed=4, with_nan=False)
    data[5, 5] = np.inf
    data[6, 6] = -np.inf
    path = write_fits(tmp_path / "inf.fits", data)
    image = load_fits(path)
    assert np.isnan(image.data[5, 5]) and np.isnan(image.data[6, 6])
    assert np.isfinite(image.data[0, 1])


def test_load_fits_missing_file_raises(tmp_path):
    with pytest.raises(EnhanceError) as excinfo:
        load_fits(tmp_path / "nope.fits")
    assert "cannot read FITS file" in str(excinfo.value)


def test_load_fits_garbage_bytes_raise(tmp_path):
    path = tmp_path / "garbage.fits"
    path.write_bytes(b"this is not a FITS file at all" * 100)
    with pytest.raises(EnhanceError):
        load_fits(path)


def test_load_fits_no_image_data_raises(tmp_path):
    buf = io.BytesIO()
    pyfits.HDUList([pyfits.PrimaryHDU()]).writeto(buf)  # empty primary HDU
    path = tmp_path / "empty.fits"
    path.write_bytes(buf.getvalue())
    with pytest.raises(EnhanceError) as excinfo:
        load_fits(path)
    assert "no image data" in str(excinfo.value)


def test_load_fits_unsupported_shape_raises(tmp_path):
    path = write_fits(tmp_path / "oned.fits", np.arange(10, dtype=np.float32))
    with pytest.raises(EnhanceError) as excinfo:
        load_fits(path)
    assert "unsupported FITS data shape" in str(excinfo.value)


# --- list_project_stacks -------------------------------------------------------------------


def test_list_project_stacks(project, session, stored_stacks):
    stack_frames, sub = stored_stacks
    infos = list_project_stacks(project)
    assert len(infos) == 2  # the sub-frame is NOT listed
    assert all(isinstance(i, StackInfo) for i in infos)
    # ordered by jd (DATE1 before DATE2)
    assert [i.frame_id for i in infos] == [f.id for f in stack_frames]
    info = infos[0]
    assert info.session_id == session
    assert info.session_started_at == "2026-11-20T22:00:00.000+00:00"
    assert info.path.is_absolute() and info.path.exists()
    assert info.path == get_frame_path(project, stack_frames[0])
    assert info.jd == pytest.approx(jd_of(DATE1))
    assert info.utc.startswith("2026-11-20T22:00:00")
    # meta overrides the header; the second stack falls back to the header cards
    assert info.target == "M 42" and info.exposure == 10.0 and info.gain == 50.0
    other = infos[1]
    assert other.target == "M 31" and other.exposure == 10.0 and other.gain == 50.0
    assert other.jd == pytest.approx(jd_of(DATE2))
    # labels
    assert info.label() == f"{info.utc} — M 42"
    assert "no target" in StackInfo(
        frame_id=0, session_id="s", session_started_at=None, path=info.path,
        jd=0.0, utc="2026-01-01T00:00:00.000+00:00", target=None,
        exposure=None, gain=None,
    ).label()
    assert "session" in info.subtitle() and "jd" in info.subtitle()
    assert "10 s" in info.subtitle() and "gain 50" in info.subtitle()


def test_list_project_stacks_empty_project(project):
    assert list_project_stacks(project) == []


def test_list_project_stacks_none_project_raises():
    with pytest.raises(EnhanceError) as excinfo:
        list_project_stacks(None)
    assert "no project" in str(excinfo.value)


# --- tonemap --------------------------------------------------------------------------------


def test_tonemap_monotonic_and_uint8():
    ramp = np.linspace(0.0, 1000.0, 100, dtype=np.float32).reshape(10, 10)
    out = tonemap(ramp, lo=0.0, hi=1000.0)
    assert out.dtype == np.uint8
    assert out.shape == (10, 10)
    assert out[0, 0] == 0 and out[-1, -1] == 255
    flat = out.ravel()
    assert np.all(np.diff(flat.astype(np.int32)) >= 0)  # monotonic non-decreasing
    assert out.min() >= 0 and out.max() <= 255


def test_tonemap_default_percentiles_span_full_range():
    ramp = np.linspace(0.0, 1000.0, 1000, dtype=np.float32).reshape(20, 50)
    out = tonemap(ramp)  # default 0.1% / 99.5% percentiles
    assert out[0, 0] == 0 and out[-1, -1] == 255


def test_tonemap_explicit_lo_hi_clip():
    data = np.array([[0.0, 50.0], [100.0, 200.0]], dtype=np.float32)
    out = tonemap(data, lo=50.0, hi=100.0)
    assert out[0, 0] == 0  # below lo clips to 0
    assert out[0, 1] == 0
    assert out[1, 0] == 255
    assert out[1, 1] == 255  # above hi clips to 255
    mid = tonemap(np.array([[75.0]], dtype=np.float32), lo=50.0, hi=100.0)
    assert mid[0, 0] == 128  # (75-50)/50 = 0.5 -> 127.5 -> rint -> 128


def test_tonemap_nan_maps_to_zero():
    data = np.array([[np.nan, 0.0], [100.0, np.inf]], dtype=np.float32)
    out = tonemap(data, lo=0.0, hi=100.0)
    assert out[0, 0] == 0 and out[1, 1] == 0  # NaN and +inf -> 0
    assert out[0, 1] == 0 and out[1, 0] == 255


def test_tonemap_all_stretches_monotonic_uint8():
    ramp = np.linspace(0.0, 1000.0, 100, dtype=np.float32).reshape(10, 10)
    for stretch in enhance.STRETCHES:
        out = tonemap(ramp, lo=0.0, hi=1000.0, stretch=stretch)
        assert out.dtype == np.uint8, stretch
        assert out[0, 0] == 0 and out[-1, -1] == 255, stretch
        flat = out.ravel().astype(np.int32)
        assert np.all(np.diff(flat) >= 0), stretch


def test_tonemap_asinh_brighter_than_linear_at_mid():
    mid = np.array([[500.0]], dtype=np.float32)
    linear = tonemap(mid, lo=0.0, hi=1000.0, stretch="linear")[0, 0]
    asinh = tonemap(mid, lo=0.0, hi=1000.0, stretch="asinh")[0, 0]
    assert asinh > linear


def test_tonemap_gamma_darkens():
    mid = np.array([[500.0]], dtype=np.float32)
    linear = tonemap(mid, lo=0.0, hi=1000.0)[0, 0]
    dark = tonemap(mid, lo=0.0, hi=1000.0, gamma=2.0)[0, 0]
    bright = tonemap(mid, lo=0.0, hi=1000.0, gamma=0.5)[0, 0]
    assert dark < linear < bright


def test_tonemap_constant_image_no_crash():
    out = tonemap(np.full((8, 8), 5.0, dtype=np.float32))
    assert out.dtype == np.uint8
    assert np.all(out == out[0, 0])  # uniform


def test_tonemap_all_nan_is_zeros():
    out = tonemap(np.full((4, 4), np.nan, dtype=np.float32))
    assert np.all(out == 0)


def test_tonemap_rgb_global_range():
    rng = np.random.default_rng(11)
    data = rng.uniform(0, 1000, size=(10, 12, 3)).astype(np.float32)
    out = tonemap(data, lo=0.0, hi=1000.0)
    assert out.shape == (10, 12, 3) and out.dtype == np.uint8
    # the same lo/hi applies to all channels (color balance preserved)
    gray = tonemap(data[..., 0], lo=0.0, hi=1000.0)
    np.testing.assert_array_equal(out[..., 0], gray)


@pytest.mark.parametrize("stretch", ["bogus", "", "LINEAR", None])
def test_tonemap_bad_stretch_raises(stretch):
    with pytest.raises(EnhanceError) as excinfo:
        tonemap(np.zeros((4, 4), dtype=np.float32), stretch=stretch)
    assert "unknown stretch" in str(excinfo.value)


@pytest.mark.parametrize("gamma", [0, -1, float("nan"), float("inf"), "1", True, None])
def test_tonemap_bad_gamma_raises(gamma):
    with pytest.raises(EnhanceError) as excinfo:
        tonemap(np.zeros((4, 4), dtype=np.float32), gamma=gamma)
    assert "gamma" in str(excinfo.value)


@pytest.mark.parametrize("lo,hi", [("x", None), (None, "y"), (float("nan"), None), (0.0, float("inf")), (True, None)])
def test_tonemap_bad_lo_hi_raises(lo, hi):
    with pytest.raises(EnhanceError):
        tonemap(np.zeros((4, 4), dtype=np.float32), lo=lo, hi=hi)


@pytest.mark.parametrize("shape", [(4,), (4, 4, 4), (2, 2, 2, 2)])
def test_tonemap_bad_shape_raises(shape):
    with pytest.raises(EnhanceError) as excinfo:
        tonemap(np.zeros(shape, dtype=np.float32))
    assert "tonemap expects" in str(excinfo.value)


# --- to_pil_image / downscale / render_preview ----------------------------------------------


def test_to_pil_image_modes():
    gray = to_pil_image(np.zeros((10, 12), dtype=np.uint8))
    assert isinstance(gray, Image.Image)
    assert gray.mode == "L" and gray.size == (12, 10)
    rgb = to_pil_image(np.zeros((10, 12, 3), dtype=np.uint8))
    assert rgb.mode == "RGB" and rgb.size == (12, 10)


def test_to_pil_image_rejects_non_uint8_and_bad_shape():
    with pytest.raises(EnhanceError):
        to_pil_image(np.zeros((4, 4), dtype=np.float32))
    with pytest.raises(EnhanceError):
        to_pil_image(np.zeros((4, 4, 4), dtype=np.uint8))


def test_downscale_mean_pooling():
    data = np.arange(2048 * 1024, dtype=np.float32).reshape(2048, 1024)
    small = downscale(data, 1024)
    assert small.shape == (1024, 512)  # factor 2 on both axes (2048x1024 -> 1024x512)
    # mean of the first 2x2 block
    assert small[0, 0] == pytest.approx(data[:2, :2].mean())
    assert small.dtype == np.float32


def test_downscale_small_image_unchanged():
    data = np.arange(64, dtype=np.float32).reshape(8, 8)
    out = downscale(data, 1024)
    assert out.shape == (8, 8)
    np.testing.assert_array_equal(out, data)
    assert downscale(data, 0) is data or np.array_equal(downscale(data, 0), data)


def test_downscale_rgb_keeps_channels():
    data = np.zeros((256, 128, 3), dtype=np.float32)
    out = downscale(data, 64)
    assert out.shape == (64, 32, 3)  # factor 4 on both axes


def test_render_preview_downscales_and_is_fast():
    big = synthetic_image(2048, seed=5, with_nan=True)
    image = FitsImage(data=big, header={}, path=None)
    start = time.perf_counter()
    preview = render_preview(image, max_size=1024)
    elapsed = time.perf_counter() - start
    assert isinstance(preview, Image.Image)
    assert preview.mode == "L"
    assert max(preview.size) <= 1024
    assert elapsed < 0.5, f"render_preview took {elapsed:.3f}s (fast path broken)"


def test_render_preview_rgb():
    rng = np.random.default_rng(6)
    data = rng.uniform(0, 1000, size=(300, 200, 3)).astype(np.float32)
    preview = render_preview(FitsImage(data=data, header={}), max_size=128)
    assert preview.mode == "RGB"
    assert max(preview.size) <= 128


def test_render_preview_passes_tonemap_params():
    data = np.linspace(0, 1000, 100, dtype=np.float32).reshape(10, 10)
    image = FitsImage(data=data, header={})
    linear = render_preview(image, max_size=1024)
    asinh = render_preview(image, max_size=1024, stretch="asinh")
    assert linear.mode == "L" and asinh.mode == "L"
    # asinh brightens the midtones -> the mean pixel value goes up
    assert np.asarray(asinh).mean() > np.asarray(linear).mean()


# --- ViewerController ------------------------------------------------------------------------


def test_controller_lists_stacks_on_init(project, stored_stacks):
    stack_frames, _sub = stored_stacks
    vc = ViewerController(project)
    assert vc.project is project
    assert [s.frame_id for s in vc.stacks] == [f.id for f in stack_frames]
    snap = vc.snapshot
    assert isinstance(snap, ViewerSnapshot)
    assert snap.project_name == "M 42 night"
    assert len(snap.stacks) == 2
    assert snap.current is None and snap.image is None and snap.preview is None
    assert snap.error is None


def test_controller_open_stack_loads_and_renders(project, stored_stacks):
    stack_frames, _sub = stored_stacks
    vc = ViewerController(project)
    seen = []
    vc.add_listener(seen.append)
    image = vc.open_stack(stack_frames[0].id)
    assert isinstance(image, FitsImage)
    assert image.shape == (32, 32)
    assert vc.current_stack is vc.stacks[0]
    assert vc.current_image is image
    assert isinstance(vc.preview, Image.Image)
    assert max(vc.preview.size) <= 1024
    snap = vc.snapshot
    assert snap.current is not None and snap.image is image and snap.preview is vc.preview
    assert snap.error is None
    assert seen and seen[-1] is snap or seen[-1].current is not None  # notified


def test_controller_open_stack_cache_by_path_and_mtime(project, stored_stacks):
    stack_frames, _sub = stored_stacks
    vc = ViewerController(project)
    first = vc.open_stack(stack_frames[0].id)
    again = vc.open_stack(stack_frames[0].id)
    assert again is first  # unchanged file -> cache hit
    # touch the file (new mtime) -> re-read
    path = vc.stacks[0].path
    os.utime(path, (time.time() + 5, time.time() + 5))
    reread = vc.open_stack(stack_frames[0].id)
    assert reread is not first


def test_controller_open_unknown_id_raises_and_records(project, stored_stacks):
    vc = ViewerController(project)
    seen = []
    vc.add_listener(seen.append)
    with pytest.raises(EnhanceError) as excinfo:
        vc.open_stack(99999)
    assert "no stack with frame id" in str(excinfo.value)
    assert vc.snapshot.error is not None and "99999" in vc.snapshot.error
    assert vc.current_stack is None
    assert seen and seen[-1].error is not None  # listeners notified of the error


def test_controller_open_corrupt_file_raises(project, stored_stacks):
    stack_frames, _sub = stored_stacks
    vc = ViewerController(project)
    vc.stacks[0].path.write_bytes(b"garbage, not FITS" * 50)
    with pytest.raises(EnhanceError):
        vc.open_stack(stack_frames[0].id)
    assert vc.snapshot.error is not None


def test_controller_no_project():
    vc = ViewerController()
    assert vc.project is None
    assert vc.stacks == ()
    assert vc.snapshot.project_name is None
    with pytest.raises(EnhanceError) as excinfo:
        vc.refresh_stacks()
    assert "no project" in str(excinfo.value)
    with pytest.raises(EnhanceError):
        vc.open_stack(1)
    assert vc.snapshot.error is not None


def test_controller_set_project_and_clear(project, stored_stacks, root):
    vc = ViewerController()
    seen = []
    vc.add_listener(seen.append)
    vc.set_project(project)
    assert len(vc.stacks) == 2
    assert vc.snapshot.project_name == "M 42 night"
    # open a stack, then switch project: the open stack is closed
    vc.open_stack(vc.stacks[0].frame_id)
    other = create_project("M 31 night", root=root)
    try:
        vc.set_project(other)
        assert vc.stacks == ()  # the other project has no stacks yet
        assert vc.current_stack is None and vc.preview is None
        assert vc.snapshot.project_name == "M 31 night"
    finally:
        other.close()
    vc.set_project(None)
    assert vc.stacks == () and vc.snapshot.project_name is None
    assert len(seen) >= 3  # notified on each change


def test_controller_snapshot_is_frozen(project, stored_stacks):
    vc = ViewerController(project)
    snap = vc.snapshot
    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.error = "x"  # type: ignore[misc]


def test_controller_listeners_idempotent_decorator_and_isolated(project, stored_stacks):
    stack_frames, _sub = stored_stacks
    vc = ViewerController(project)
    calls_a, calls_b = [], []

    def raising(_snapshot):
        raise RuntimeError("boom")

    vc.add_listener(calls_a.append)
    vc.add_listener(calls_a.append)  # idempotent
    vc.add_listener(raising)
    vc.add_listener(calls_b.append)
    assert len(vc.listeners) == 3

    @vc.add_listener
    def on_change(snapshot):
        on_change.seen = snapshot

    assert on_change in vc.listeners
    vc.open_stack(stack_frames[0].id)  # the raising listener must not break the others
    assert len(calls_a) == 1 and len(calls_b) == 1
    assert on_change.seen.current is not None
    vc.remove_listener(calls_a.append)
    vc.remove_listener(calls_a.append)  # no-op when absent
    assert len(vc.listeners) == 3
    vc.remove_listener(raising)
    vc.refresh_stacks()
    assert len(calls_a) == 1 and len(calls_b) == 2


def test_controller_preview_max_size(project, stored_stacks):
    stack_frames, _sub = stored_stacks
    vc = ViewerController(project, preview_max_size=16)
    vc.open_stack(stack_frames[0].id)
    assert max(vc.preview.size) <= 16


# --- package boundary (subprocess + source scan) ----------------------------------------------


def test_pure_enhance_modules_import_no_gi_no_link_no_ui_and_widget_stays_lazy():
    imports = "".join(
        f"import seestar_controller.enhance.{name};" for name in PURE_ENHANCE_MODULES[1:]
    )
    code = (
        "import sys;"
        "import seestar_controller.enhance;"
        f"{imports}"
        "assert 'gi' not in sys.modules, 'GTK imported';"
        "assert 'seestar_controller.link' not in sys.modules, 'link imported eagerly';"
        "assert 'seestar_controller.ui' not in sys.modules, 'ui imported eagerly';"
        "assert 'seestar_controller.enhance.viewer' not in sys.modules, "
        "'viewer imported eagerly';"
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


def test_every_enhance_module_is_pure_or_widget_and_lazy_exported():
    modules = {path.stem for path in ENHANCE_PACKAGE_DIR.glob("*.py")}
    assert modules == set(PURE_ENHANCE_MODULES) | set(WIDGET_ENHANCE_MODULES)
    for name in PURE_ENHANCE_MODULES:
        source = (ENHANCE_PACKAGE_DIR / f"{name}.py").read_text(encoding="utf-8")
        assert not re.search(r"^\s*(import gi|from gi)\b", source, re.MULTILINE), (
            f"pure module {name} imports gi"
        )
    for name in WIDGET_ENHANCE_MODULES:
        lines = (ENHANCE_PACKAGE_DIR / f"{name}.py").read_text(encoding="utf-8").splitlines()
        gi_lines = [i for i, line in enumerate(lines) if line.strip() == "import gi"]
        assert gi_lines, f"widget module {name} has no 'import gi' at module top"
        repo_line = next(
            i for i, line in enumerate(lines) if line.startswith("from gi.repository")
        )
        assert gi_lines[0] < repo_line, (
            f"widget module {name}: 'import gi' must come before 'from gi.repository'"
        )
    assert set(enhance._WIDGET_EXPORTS.values()) == set(WIDGET_ENHANCE_MODULES)


def test_public_api_is_exported_from_package():
    for name in (
        "EnhanceError",
        "FitsImage",
        "StackInfo",
        "ViewerController",
        "ViewerListener",
        "ViewerSnapshot",
        "load_fits",
        "list_project_stacks",
        "tonemap",
        "to_pil_image",
        "downscale",
        "render_preview",
        "STRETCHES",
        "DEFAULT_STRETCH",
        "DEFAULT_LO_PERCENTILE",
        "DEFAULT_HI_PERCENTILE",
        "DEFAULT_MAX_PREVIEW_SIZE",
        "MAX_CACHE_ENTRIES",
    ):
        assert name in enhance.__all__, name
        assert hasattr(enhance, name), name
    assert "ImageViewer" in enhance.__all__ and "ImageViewer" in enhance._WIDGET_EXPORTS
    assert enhance.load_fits is fits_io.load_fits
    assert enhance.tonemap is render.tonemap
    assert enhance.ViewerController is viewer_controller.ViewerController
    assert issubclass(enhance.EnhanceError, Exception)
