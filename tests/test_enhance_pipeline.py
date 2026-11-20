"""Image-enhance ticket 02: pure enhancement-pipeline tests — the composable
ops (stretch / denoise / sharpen / white balance / background subtraction)
and the Pipeline that applies them in order. No gi, no GTK, no network.

Covers the pure layer added by ticket 02:

- :mod:`seestar_controller.enhance.ops` — every op is a pure function
  image+params -> image on float32 ``(H, W)`` / ``(H, W, 3)`` arrays:
  :func:`~seestar_controller.enhance.ops.apply_stretch` (linear/asinh/
  histogram, all monotonic on a ramp, NaN mask preserved),
  :func:`~seestar_controller.enhance.ops.apply_denoise` (reduces the std of
  injected gaussian noise on a flat field while preserving the mean),
  :func:`~seestar_controller.enhance.ops.apply_sharpen` (unsharp mask
  increases the gradient magnitude at a step edge),
  :func:`~seestar_controller.enhance.ops.apply_white_balance` (gray-world
  neutralizes a known color cast; explicit gains; grayscale no-op),
  :func:`~seestar_controller.enhance.ops.apply_background` (median grid and
  polynomial fit flatten a linear gradient); each params dataclass validates
  its ranges (-> :class:`EnhanceError`) and round-trips through
  ``to_dict``/``from_dict``; no op mutates its input;
- :mod:`seestar_controller.enhance.pipeline` — :class:`Op` (frozen
  name+params, validated), :class:`Pipeline` (ops applied IN ORDER, re-runnable
  from the original data, input never mutated, immutable
  :meth:`~seestar_controller.enhance.pipeline.Pipeline.with_param`,
  ``to_dict``/``from_dict`` round-trip), :func:`default_pipeline`,
  :func:`render_pipeline` (pipeline + tonemap + downscale preview);
- a golden-hash regression: a fixed synthetic input + the default pipeline ->
  a hardcoded sha256 of the output bytes;
- NaN handling (mask preserved by every op, all-NaN images don't crash);
- the package boundary: a subprocess test (no ``gi``, no ``link``, no
  ``ui``) + the source-scan audit in tests/test_enhance_fits.py now covers
  ``ops`` and ``pipeline`` too.

The widget tests live in tests/test_enhance_widgets.py and skip without ``gi``.
"""

import dataclasses
import hashlib
import re
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import seestar_controller.enhance as enhance
from seestar_controller.enhance import (
    OP_NAMES,
    OPS,
    BackgroundParams,
    DenoiseParams,
    EnhanceError,
    FitsImage,
    Op,
    Pipeline,
    SharpenParams,
    StretchParams,
    WhiteBalanceParams,
    apply_background,
    apply_denoise,
    apply_sharpen,
    apply_stretch,
    apply_white_balance,
    default_pipeline,
    render_pipeline,
)
from seestar_controller.enhance import ops, pipeline

#: Directory of the enhance package (source-scan audit).
ENHANCE_PACKAGE_DIR = Path(enhance.__file__).resolve().parent

#: The golden-hash regression: sha256 over dtype + shape + raw bytes of
#: ``default_pipeline().apply(golden_image())`` (computed once, hardcoded).
GOLDEN_SHA256 = "d6e97c34b431754a230646a8d99a1ad77f7ff6015248868d19a89a8b9b69dd7f"


def golden_image(size: int = 64, seed: int = 42) -> np.ndarray:
    """The fixed synthetic stack image for the golden-hash regression: a
    smooth gradient + three star peaks + gaussian noise, float32, with NaN
    at the four corners."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:size, 0:size]
    data = ((xx + yy) * 10.0).astype(np.float32)
    for cy, cx, peak in ((16, 16, 5000.0), (40, 48, 8000.0), (50, 12, 3000.0)):
        data = data + (peak * np.exp(-(((yy - cy) ** 2 + (xx - cx) ** 2) / 8.0))).astype(
            np.float32
        )
    data = data + rng.normal(0.0, 5.0, size=(size, size)).astype(np.float32)
    data[0, 0] = np.nan
    data[0, -1] = np.nan
    data[-1, 0] = np.nan
    data[-1, -1] = np.nan
    return np.ascontiguousarray(data, dtype=np.float32)


def ramp_image(n: int = 100) -> np.ndarray:
    """A 10x10 monotonic ramp 0..1000 (float32)."""
    return np.linspace(0.0, 1000.0, n, dtype=np.float32).reshape(10, 10)


def flat_noisy_image(size: int = 64, mean: float = 100.0, sigma: float = 10.0, seed: int = 7):
    """A flat field with injected gaussian noise (float32)."""
    rng = np.random.default_rng(seed)
    return (mean + rng.normal(0.0, sigma, size=(size, size))).astype(np.float32)


def step_edge_image() -> np.ndarray:
    """A 32x64 step edge: 0 on the left half, 100 on the right half."""
    img = np.zeros((32, 64), dtype=np.float32)
    img[:, 32:] = 100.0
    return img


def gradient_image(size: int = 64, slope: float = 10.0, offset: float = 5.0) -> np.ndarray:
    """A linear horizontal gradient ``slope * x + offset`` (float32)."""
    _, xx = np.mgrid[0:size, 0:size]
    return (xx * slope + offset).astype(np.float32)


def cast_rgb_image() -> np.ndarray:
    """A 16x16 RGB image with a known color cast: R=150, G=100, B=50."""
    img = np.zeros((16, 16, 3), dtype=np.float32)
    img[..., 0] = 150.0
    img[..., 1] = 100.0
    img[..., 2] = 50.0
    return img


#: All (apply function, default params) pairs — for the purity/NaN sweeps.
ALL_OPS = (
    (apply_stretch, StretchParams()),
    (apply_denoise, DenoiseParams()),
    (apply_sharpen, SharpenParams()),
    (apply_white_balance, WhiteBalanceParams()),
    (apply_background, BackgroundParams()),
)


# --- stretch -----------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["linear", "asinh", "histogram"])
def test_stretch_preserves_ordering_on_ramp(kind):
    out = apply_stretch(ramp_image(), StretchParams(kind=kind))
    assert out.dtype == np.float32 and out.shape == (10, 10)
    flat = out.ravel().astype(np.float64)
    assert np.all(np.diff(flat) >= 0)  # monotonic non-decreasing
    assert out.min() == pytest.approx(0.0) and out.max() == pytest.approx(1.0)
    assert out.min() >= 0.0 and out.max() <= 1.0  # normalized to [0, 1]


def test_stretch_linear_is_min_max_normalize():
    out = apply_stretch(ramp_image(), StretchParams(kind="linear"))
    np.testing.assert_allclose(out, ramp_image() / 1000.0, rtol=1e-6)


def test_stretch_asinh_brightens_midtones_vs_linear():
    ramp = ramp_image()
    linear = apply_stretch(ramp, StretchParams(kind="linear"))
    asinh = apply_stretch(ramp, StretchParams(kind="asinh", sigma=10.0))
    mid = (5, 0)  # value 500 of 0..1000
    assert asinh[mid] > linear[mid]
    # larger sigma -> stronger midtone lift
    softer = apply_stretch(ramp, StretchParams(kind="asinh", sigma=100.0))
    assert softer[mid] > asinh[mid]


def test_stretch_histogram_equalizes_two_value_image():
    img = np.zeros((10, 10), dtype=np.float32)
    img.ravel()[::2] = 100.0  # half 0, half 100
    out = apply_stretch(img, StretchParams(kind="histogram"))
    assert set(np.unique(out).tolist()) == {0.0, 1.0}  # cdf maps to the extremes


def test_stretch_histogram_spreads_a_skewed_distribution():
    # exponential-ish values: most pixels dark, a few bright
    rng = np.random.default_rng(3)
    img = (rng.exponential(1.0, size=(32, 32)) * 100).astype(np.float32)
    out = apply_stretch(img, StretchParams(kind="histogram"))
    flat = out.ravel()
    assert np.all(np.diff(np.sort(flat)) >= 0)  # monotonic
    # equalization uses the range: the median lands near 0.5-ish, well above
    # the linear stretch's median for a skewed distribution
    linear = apply_stretch(img, StretchParams(kind="linear"))
    assert np.median(out) > np.median(linear)


def test_stretch_constant_image_maps_to_zero():
    out = apply_stretch(np.full((8, 8), 5.0, dtype=np.float32), StretchParams())
    assert np.all(out == 0.0)


def test_stretch_rgb_keeps_shape_and_is_monotonic_per_channel():
    rng = np.random.default_rng(11)
    img = rng.uniform(0, 1000, size=(10, 12, 3)).astype(np.float32)
    out = apply_stretch(img, StretchParams(kind="asinh"))
    assert out.shape == (10, 12, 3) and out.dtype == np.float32
    for c in range(3):
        flat = np.sort(out[..., c].ravel())
        assert np.all(np.diff(flat) >= 0)


def test_stretch_nan_mask_preserved_and_input_not_mutated():
    img = golden_image()
    before = img.copy()
    out = apply_stretch(img, StretchParams(kind="asinh"))
    assert np.isnan(out[0, 0]) and np.isnan(out[-1, -1])
    assert np.isfinite(out[5, 5])
    assert np.array_equal(img, before, equal_nan=True)  # not mutated
    assert out is not img


def test_stretch_all_nan_returns_all_nan():
    img = np.full((4, 4), np.nan, dtype=np.float32)
    out = apply_stretch(img, StretchParams())
    assert np.isnan(out).all()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"kind": "bogus"},
        {"kind": ""},
        {"kind": "LINEAR"},
        {"kind": None},
        {"sigma": 0.0},
        {"sigma": -1.0},
        {"sigma": float("nan")},
        {"sigma": "10"},
        {"sigma": True},
        {"bins": 1},
        {"bins": 0},
        {"bins": 70000},
        {"bins": 2.5},
        {"bins": True},
    ],
)
def test_stretch_params_validation(kwargs):
    with pytest.raises(EnhanceError):
        StretchParams(**kwargs)


def test_stretch_params_dict_roundtrip():
    params = StretchParams(kind="histogram", sigma=25.0, bins=128)
    assert StretchParams.from_dict(params.to_dict()) == params
    assert params.to_dict() == {"kind": "histogram", "sigma": 25.0, "bins": 128}
    with pytest.raises(EnhanceError):
        StretchParams.from_dict({"kind": "linear", "bogus": 1})
    with pytest.raises(EnhanceError):
        StretchParams.from_dict("not a dict")
    with pytest.raises(EnhanceError):
        StretchParams.from_dict({"kind": "bogus"})  # re-validated on load


def test_apply_stretch_rejects_wrong_params_type():
    with pytest.raises(EnhanceError):
        apply_stretch(ramp_image(), DenoiseParams())  # type: ignore[arg-type]


# --- denoise -----------------------------------------------------------------------------------


def test_denoise_reduces_injected_noise_and_preserves_mean():
    img = flat_noisy_image(sigma=10.0)
    before_std = float(img.std())
    out = apply_denoise(img, DenoiseParams(strength=1.0, radius=1))
    after_std = float(out.std())
    assert after_std < 0.5 * before_std  # noise measurably reduced
    assert float(out.mean()) == pytest.approx(float(img.mean()), abs=0.5)  # mean kept


def test_denoise_strength_blends_original_and_blur():
    img = flat_noisy_image()
    identity = apply_denoise(img, DenoiseParams(strength=0.0))
    np.testing.assert_array_equal(identity, img)  # strength 0 = no-op
    full = apply_denoise(img, DenoiseParams(strength=1.0))
    half = apply_denoise(img, DenoiseParams(strength=0.5))
    np.testing.assert_allclose(half, 0.5 * img + 0.5 * full, rtol=1e-6)


def test_denoise_larger_radius_blurs_more():
    img = flat_noisy_image()
    r1 = float(apply_denoise(img, DenoiseParams(strength=1.0, radius=1)).std())
    r3 = float(apply_denoise(img, DenoiseParams(strength=1.0, radius=3)).std())
    assert r3 < r1


def test_denoise_rgb_and_nan_and_purity():
    rng = np.random.default_rng(5)
    img = rng.uniform(0, 100, size=(16, 16, 3)).astype(np.float32)
    img[0, 0] = np.nan
    before = img.copy()
    out = apply_denoise(img, DenoiseParams(strength=0.8, radius=2))
    assert out.shape == (16, 16, 3) and out.dtype == np.float32
    assert np.isnan(out[0, 0]).all() and np.isfinite(out[8, 8]).all()
    assert np.array_equal(img, before, equal_nan=True)


def test_denoise_all_nan_no_crash():
    out = apply_denoise(np.full((4, 4), np.nan, dtype=np.float32), DenoiseParams())
    assert np.isnan(out).all()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"strength": -0.1},
        {"strength": 1.5},
        {"strength": float("nan")},
        {"strength": "0.5"},
        {"strength": True},
        {"radius": 0},
        {"radius": -1},
        {"radius": 100},
        {"radius": 1.5},
        {"radius": True},
    ],
)
def test_denoise_params_validation(kwargs):
    with pytest.raises(EnhanceError):
        DenoiseParams(**kwargs)


def test_denoise_params_dict_roundtrip():
    params = DenoiseParams(strength=0.3, radius=2)
    assert DenoiseParams.from_dict(params.to_dict()) == params
    assert params.to_dict() == {"strength": 0.3, "radius": 2}


# --- sharpen -----------------------------------------------------------------------------------


def test_sharpen_increases_edge_contrast():
    img = step_edge_image()
    before = float(np.abs(np.diff(img, axis=1)).max())
    out = apply_sharpen(img, SharpenParams(amount=1.0, radius=1))
    after = float(np.abs(np.diff(out, axis=1)).max())
    assert after > before  # unsharp mask steepens the edge
    # and overshoots on both sides of the edge
    assert out[:, 31].max() < img[:, 31].min() or out[:, 31].min() < 0.0
    assert out[:, 32].max() > 100.0


def test_sharpen_amount_zero_is_identity_and_scales_with_amount():
    img = step_edge_image()
    np.testing.assert_array_equal(
        apply_sharpen(img, SharpenParams(amount=0.0)), img
    )
    g1 = float(np.abs(np.diff(apply_sharpen(img, SharpenParams(amount=1.0)), axis=1)).max())
    g2 = float(np.abs(np.diff(apply_sharpen(img, SharpenParams(amount=2.0)), axis=1)).max())
    assert g2 > g1


def test_sharpen_nan_and_purity():
    img = golden_image()
    before = img.copy()
    out = apply_sharpen(img, SharpenParams(amount=1.5, radius=2))
    assert np.isnan(out[0, 0]) and np.isnan(out[-1, -1]) and np.isfinite(out[5, 5])
    assert np.array_equal(img, before, equal_nan=True)


def test_sharpen_all_nan_no_crash():
    out = apply_sharpen(np.full((4, 4), np.nan, dtype=np.float32), SharpenParams())
    assert np.isnan(out).all()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"amount": -0.1},
        {"amount": 3.5},
        {"amount": float("inf")},
        {"amount": "1"},
        {"amount": False},
        {"radius": 0},
        {"radius": 2.5},
    ],
)
def test_sharpen_params_validation(kwargs):
    with pytest.raises(EnhanceError):
        SharpenParams(**kwargs)


def test_sharpen_params_dict_roundtrip():
    params = SharpenParams(amount=1.5, radius=3)
    assert SharpenParams.from_dict(params.to_dict()) == params


# --- white balance -------------------------------------------------------------------------------


def test_white_balance_gray_world_neutralizes_color_cast():
    img = cast_rgb_image()  # R=150, G=100, B=50
    out = apply_white_balance(img, WhiteBalanceParams(mode="gray-world"))
    means = [float(out[..., c].mean()) for c in range(3)]
    assert means[0] == pytest.approx(100.0, abs=1e-3)
    assert means[1] == pytest.approx(100.0, abs=1e-3)
    assert means[2] == pytest.approx(100.0, abs=1e-3)
    assert max(means) - min(means) < 1e-3  # channel means ~equal


def test_white_balance_gray_world_keeps_neutral_image():
    rng = np.random.default_rng(9)
    base = rng.uniform(50, 150, size=(16, 16, 1)).astype(np.float32)
    img = np.repeat(base, 3, axis=2)  # already neutral
    out = apply_white_balance(img, WhiteBalanceParams(mode="gray-world"))
    np.testing.assert_allclose(out, img, rtol=1e-5)


def test_white_balance_explicit_gains():
    img = cast_rgb_image()
    out = apply_white_balance(img, WhiteBalanceParams(mode="gains", gains=(2.0, 1.0, 0.5)))
    assert float(out[..., 0].mean()) == pytest.approx(300.0)
    assert float(out[..., 1].mean()) == pytest.approx(100.0)
    assert float(out[..., 2].mean()) == pytest.approx(25.0)


def test_white_balance_grayscale_is_noop_copy():
    img = ramp_image()
    out = apply_white_balance(img, WhiteBalanceParams(mode="gray-world"))
    np.testing.assert_array_equal(out, img)
    assert out is not img  # still a copy
    out[0, 0] = -1.0
    assert img[0, 0] == 0.0  # mutating the output doesn't touch the input


def test_white_balance_nan_preserved_and_input_not_mutated():
    img = np.stack([golden_image()] * 3, axis=-1)
    before = img.copy()
    out = apply_white_balance(img, WhiteBalanceParams())
    assert np.isnan(out[0, 0, 0]) and np.isnan(out[-1, -1, 2])
    assert np.isfinite(out[5, 5, 1])
    assert np.array_equal(img, before, equal_nan=True)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"mode": "bogus"},
        {"mode": "GRAY-WORLD"},
        {"mode": None},
        {"gains": (1.0, 1.0)},
        {"gains": (1.0, 1.0, 1.0, 1.0)},
        {"gains": (1.0, 0.0, 1.0)},
        {"gains": (1.0, -1.0, 1.0)},
        {"gains": (1.0, float("nan"), 1.0)},
        {"gains": (1.0, "x", 1.0)},
        {"gains": 1.0},
        {"gains": "111"},
    ],
)
def test_white_balance_params_validation(kwargs):
    with pytest.raises(EnhanceError):
        WhiteBalanceParams(**kwargs)


def test_white_balance_params_dict_roundtrip_and_json_list_gains():
    params = WhiteBalanceParams(mode="gains", gains=(2.0, 1.0, 0.5))
    assert WhiteBalanceParams.from_dict(params.to_dict()) == params
    # JSON persistence turns the tuple into a list — from_dict accepts it
    as_json = {"mode": "gains", "gains": [2.0, 1.0, 0.5]}
    assert WhiteBalanceParams.from_dict(as_json) == params
    assert params.gains == (2.0, 1.0, 0.5)  # normalized back to a tuple


# --- background subtraction ------------------------------------------------------------------------


def test_background_median_flattens_linear_gradient():
    img = gradient_image(slope=10.0)
    orig_std = float(img.std())
    out = apply_background(img, BackgroundParams(method="median"))
    residual_std = float(out.std())
    assert residual_std < orig_std / 3  # residual std << original
    assert float(out.mean()) == pytest.approx(0.0, abs=1.0)


def test_background_poly_flattens_linear_gradient_exactly():
    img = gradient_image(slope=10.0)
    out = apply_background(img, BackgroundParams(method="poly", degree=1))
    assert float(out.std()) < 1e-3  # a degree-1 fit removes a linear gradient
    assert float(out.mean()) == pytest.approx(0.0, abs=1e-3)


def test_background_poly_degree2_fits_a_quadratic():
    yy, xx = np.mgrid[0:64, 0:64]
    img = (0.01 * xx ** 2 + 5.0 * yy + 100.0).astype(np.float32)
    out = apply_background(img, BackgroundParams(method="poly", degree=2))
    assert float(out.std()) < float(img.std()) / 10


def test_background_subtracts_constant_offset():
    img = flat_noisy_image(mean=250.0, sigma=0.0)
    out = apply_background(img, BackgroundParams(method="median"))
    assert float(np.median(out)) == pytest.approx(0.0, abs=1e-3)


def test_background_rgb_per_channel_and_nan_and_purity():
    img = np.stack([gradient_image(), gradient_image() * 2, gradient_image() * 0.5], axis=-1)
    img[0, 0] = np.nan
    before = img.copy()
    out = apply_background(img, BackgroundParams(method="poly", degree=1))
    assert out.shape == img.shape and out.dtype == np.float32
    for c in range(3):
        assert float(np.nanstd(out[..., c])) < float(np.nanstd(img[..., c])) / 10
    assert np.isnan(out[0, 0, 0])
    assert np.array_equal(img, before, equal_nan=True)


def test_background_all_nan_no_crash():
    out = apply_background(np.full((4, 4), np.nan, dtype=np.float32), BackgroundParams())
    assert np.isnan(out).all()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"method": "bogus"},
        {"method": "MEDIAN"},
        {"method": None},
        {"degree": -1},
        {"degree": 11},
        {"degree": 1.5},
        {"degree": True},
    ],
)
def test_background_params_validation(kwargs):
    with pytest.raises(EnhanceError):
        BackgroundParams(**kwargs)


def test_background_params_dict_roundtrip():
    params = BackgroundParams(method="poly", degree=3)
    assert BackgroundParams.from_dict(params.to_dict()) == params
    assert params.to_dict() == {"method": "poly", "degree": 3}


# --- purity + NaN sweep over all ops ----------------------------------------------------------------


@pytest.mark.parametrize("fn,params", ALL_OPS, ids=lambda p: getattr(p, "__name__", "params"))
def test_every_op_is_pure_and_nan_safe(fn, params):
    img = golden_image()
    if fn is apply_white_balance:
        img = np.stack([img] * 3, axis=-1)  # white balance needs RGB
    before = img.copy()
    out = fn(img, params)
    assert out.dtype == np.float32 and out.shape == img.shape
    assert out is not img
    assert np.array_equal(img, before, equal_nan=True)  # input unmutated
    # NaN mask preserved: NaN in -> NaN out, finite stays finite
    assert np.array_equal(np.isnan(out), np.isnan(img))


@pytest.mark.parametrize("fn,params", ALL_OPS, ids=lambda p: getattr(p, "__name__", "params"))
def test_every_op_handles_all_nan(fn, params):
    img = np.full((8, 8), np.nan, dtype=np.float32)
    if fn is apply_white_balance:
        img = np.full((8, 8, 3), np.nan, dtype=np.float32)
    out = fn(img, params)
    assert np.isnan(out).all()


def test_ops_reject_bad_shapes_and_non_numeric():
    for fn, params in ALL_OPS:
        with pytest.raises(EnhanceError):
            fn(np.zeros((4,), dtype=np.float32), params)
        with pytest.raises(EnhanceError):
            fn(np.zeros((2, 2, 2, 2), dtype=np.float32), params)
        with pytest.raises(EnhanceError):
            fn(np.zeros((4, 4, 4), dtype=np.float32), params)
        with pytest.raises(EnhanceError):
            fn("not an array", params)  # type: ignore[arg-type]


def test_ops_registry():
    assert OP_NAMES == ("stretch", "denoise", "sharpen", "white_balance", "background")
    assert set(OPS) == set(OP_NAMES)
    for name, spec in OPS.items():
        assert spec.name == name
        assert callable(spec.apply)
        assert issubclass(spec.params_type, object)


# --- Op ----------------------------------------------------------------------------------------------


def test_op_validates_name_and_params_type():
    op = Op("stretch", StretchParams(kind="linear"))
    assert op.name == "stretch" and op.spec is OPS["stretch"]
    with pytest.raises(EnhanceError) as excinfo:
        Op("bogus", StretchParams())
    assert "unknown op name" in str(excinfo.value)
    with pytest.raises(EnhanceError) as excinfo:
        Op("stretch", DenoiseParams())  # wrong params type
    assert "expects StretchParams" in str(excinfo.value)
    with pytest.raises(dataclasses.FrozenInstanceError):
        op.name = "denoise"  # type: ignore[misc]


def test_op_dict_roundtrip():
    op = Op("white_balance", WhiteBalanceParams(mode="gains", gains=(1.5, 1.0, 0.8)))
    assert Op.from_dict(op.to_dict()) == op
    assert op.to_dict() == {
        "op": "white_balance",
        "params": {"mode": "gains", "gains": (1.5, 1.0, 0.8)},
    }
    with pytest.raises(EnhanceError):
        Op.from_dict({"op": "bogus", "params": {}})
    with pytest.raises(EnhanceError):
        Op.from_dict({"params": {}})  # missing op
    with pytest.raises(EnhanceError):
        Op.from_dict({"op": "stretch"})  # missing params
    with pytest.raises(EnhanceError):
        Op.from_dict("nope")


# --- Pipeline ------------------------------------------------------------------------------------------


def test_empty_pipeline_returns_float32_copy():
    img = ramp_image()
    out = Pipeline().apply(img)
    assert out.dtype == np.float32
    np.testing.assert_array_equal(out, img)
    assert out is not img
    assert Pipeline().names == ()


def test_pipeline_applies_ops_in_order():
    img = flat_noisy_image()
    img[::2, ::2] += 500.0  # some structure
    stretch_only = apply_stretch(img, StretchParams(kind="asinh"))
    # denoise with strength 0 is the identity -> [stretch, denoise(0)] == stretch
    pipe = Pipeline(
        (Op("stretch", StretchParams(kind="asinh")), Op("denoise", DenoiseParams(strength=0.0)))
    )
    np.testing.assert_array_equal(pipe.apply(img), stretch_only)
    # and the reverse order is NOT the same when the second op is real
    pa = Pipeline(
        (Op("stretch", StretchParams(kind="asinh")), Op("denoise", DenoiseParams(strength=1.0)))
    )
    pb = Pipeline(
        (Op("denoise", DenoiseParams(strength=1.0)), Op("stretch", StretchParams(kind="asinh")))
    )
    assert not np.allclose(pa.apply(img), pb.apply(img))  # order matters


def test_pipeline_is_rerunnable_from_original_and_never_mutates():
    img = golden_image()
    before = img.copy()
    pipe = default_pipeline()
    out1 = pipe.apply(img)
    out2 = pipe.apply(img)
    assert np.array_equal(out1, out2, equal_nan=True)  # same result twice
    assert np.array_equal(img, before, equal_nan=True)  # input untouched
    # FitsImage input works too and its data is untouched
    fits = FitsImage(data=img, header={}, path=None)
    out3 = pipe.apply(fits)
    assert np.array_equal(out3, out1, equal_nan=True)
    assert np.array_equal(fits.data, before, equal_nan=True)


def test_pipeline_rejects_non_op_entries():
    with pytest.raises(EnhanceError):
        Pipeline(("stretch",))  # type: ignore[arg-type]
    with pytest.raises(dataclasses.FrozenInstanceError):
        Pipeline().ops = ()  # type: ignore[misc]


def test_pipeline_with_param_is_immutable():
    pipe = default_pipeline()
    changed = pipe.with_param("denoise", strength=0.9)
    assert isinstance(changed, Pipeline)
    assert changed is not pipe
    assert changed.ops[1].params.strength == 0.9
    assert pipe.ops[1].params.strength == 0.25  # original unchanged
    assert changed.names == pipe.names
    # chaining
    changed2 = changed.with_param("stretch", kind="histogram", bins=64)
    assert changed2.ops[0].params.kind == "histogram"
    assert changed.ops[0].params.kind == "asinh"
    # unknown op name
    with pytest.raises(EnhanceError) as excinfo:
        pipe.with_param("bogus", strength=0.5)
    assert "no op named" in str(excinfo.value)
    # invalid change re-validates
    with pytest.raises(EnhanceError):
        pipe.with_param("denoise", strength=99.0)


def test_pipeline_dict_roundtrip():
    pipe = Pipeline(
        (
            Op("background", BackgroundParams(method="poly", degree=2)),
            Op("stretch", StretchParams(kind="histogram", sigma=5.0, bins=128)),
            Op("white_balance", WhiteBalanceParams(mode="gains", gains=(1.2, 1.0, 0.9))),
        )
    )
    assert Pipeline.from_dict(pipe.to_dict()) == pipe
    assert pipe.to_dict()["ops"][0]["op"] == "background"
    with pytest.raises(EnhanceError):
        Pipeline.from_dict({"ops": [{"op": "bogus", "params": {}}]})
    with pytest.raises(EnhanceError):
        Pipeline.from_dict({"ops": "stretch"})
    with pytest.raises(EnhanceError):
        Pipeline.from_dict(["not", "a", "dict"])
    assert Pipeline.from_dict({}) == Pipeline()  # empty ops default


def test_default_pipeline_shape():
    pipe = default_pipeline()
    assert pipe.names == ("stretch", "denoise", "sharpen")
    out = pipe.apply(golden_image())
    assert out.shape == (64, 64) and out.dtype == np.float32
    assert np.isnan(out[0, 0])  # NaN mask carried through the whole pipeline


def test_render_pipeline_grayscale_and_rgb():
    img = golden_image()
    preview = render_pipeline(default_pipeline(), FitsImage(data=img, header={}), max_size=32)
    assert isinstance(preview, Image.Image)
    assert preview.mode == "L" and max(preview.size) <= 32
    rgb = np.stack([img] * 3, axis=-1)
    pipe = Pipeline((Op("white_balance", WhiteBalanceParams()), Op("stretch", StretchParams())))
    preview_rgb = render_pipeline(pipe, FitsImage(data=rgb, header={}), max_size=32)
    assert preview_rgb.mode == "RGB" and max(preview_rgb.size) <= 32
    with pytest.raises(EnhanceError):
        render_pipeline("not a pipeline", FitsImage(data=img, header={}))  # type: ignore[arg-type]
    with pytest.raises(EnhanceError):
        render_pipeline(default_pipeline(), img)  # type: ignore[arg-type]


# --- golden-hash regression ---------------------------------------------------------------------------


def test_golden_hash_default_pipeline_on_fixed_input():
    out = default_pipeline().apply(golden_image())
    digest = hashlib.sha256()
    digest.update(out.dtype.str.encode("ascii"))
    digest.update(str(out.shape).encode("ascii"))
    digest.update(out.tobytes())
    assert digest.hexdigest() == GOLDEN_SHA256


# --- package boundary + exports ------------------------------------------------------------------------


def test_pipeline_modules_import_no_gi_no_link_no_ui():
    code = (
        "import sys;"
        "import seestar_controller.enhance;"
        "import seestar_controller.enhance.ops;"
        "import seestar_controller.enhance.pipeline;"
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


def test_ops_and_pipeline_sources_have_no_gi_import():
    for name in ("ops", "pipeline"):
        source = (ENHANCE_PACKAGE_DIR / f"{name}.py").read_text(encoding="utf-8")
        assert not re.search(r"^\s*(import gi|from gi)\b", source, re.MULTILINE), name


def test_public_api_is_exported_from_package():
    for name in (
        "OP_NAMES",
        "OPS",
        "STRETCH_KINDS",
        "WB_MODES",
        "BACKGROUND_METHODS",
        "StretchParams",
        "DenoiseParams",
        "SharpenParams",
        "WhiteBalanceParams",
        "BackgroundParams",
        "OpSpec",
        "apply_stretch",
        "apply_denoise",
        "apply_sharpen",
        "apply_white_balance",
        "apply_background",
        "Op",
        "Pipeline",
        "default_pipeline",
        "render_pipeline",
    ):
        assert name in enhance.__all__, name
        assert hasattr(enhance, name), name
    assert enhance.apply_stretch is ops.apply_stretch
    assert enhance.Pipeline is pipeline.Pipeline
    assert enhance.Op is pipeline.Op
    assert enhance.default_pipeline is pipeline.default_pipeline
    assert enhance.render_pipeline is pipeline.render_pipeline
    assert enhance.OPS is ops.OPS
