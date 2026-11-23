"""Pure-layer tests for photometry ticket 03 — the aperture-photometry core
(:mod:`seestar_controller.photometry.measure`).

Headless: no ``gi`` anywhere (the sandbox has no GTK, see
docs/wiki/assumptions.md). The photometry runs on SYNTHETIC FITS frames with
known injected fluxes: 3 comps at constant flux ``F0`` and a target whose
flux varies sinusoidally across N frames, so the injected differential
magnitude of frame ``i`` is ``m_i = -2.5*log10(F_target_i / F0)`` — the
recovered differential mags must match within tolerance and the injected
amplitude must be recovered. Frames are stored in a tmp project via
:mod:`seestar_controller.store` (``create_project`` / ``start_session`` /
``store_frame`` — the same pattern as tests/test_photometry_picking.py).

Also covered: the ensemble math unit tests (mean, error formula), the
flux→mag / flux_err→mag_err propagation, JD from the FITS ``DATE-OBS``
(astropy) with the frame-jd fallback, out-of-frame and non-positive-flux
frames recorded as skipped (never a crash), stacks never measured, store
:class:`~seestar_controller.store.stars.StarPick` records as picks, the
photometry-vs-store ``MeasurementError`` distinction, and the package
boundary (subprocess + source-scan, mirroring tests/test_photometry_picking.py).
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
from astropy.time import Time

from seestar_controller import photometry
from seestar_controller.photometry import (
    ApertureParams,
    FrameSkippedError,
    PhotometryError,
    StarPickSpec,
    differential_mag,
    differential_mag_err,
    ensemble_mag,
    ensemble_mag_err,
    flux_err_to_mag_err,
    flux_to_mag,
    measure,
    measure_frame,
    measure_frame_data,
    measure_project,
    measure_star,
    pixel_to_sky,
)
from seestar_controller.photometry.measure import (
    MAG_ERR_COEFF,
    FrameMeasurement,
    MeasurementError,
    MeasureReport,
    SkippedFrame,
    StarMeasurement,
)
from seestar_controller.store import (
    KIND_STACK,
    KIND_SUB,
    StarPick,
    create_project,
    lightcurve,
    load_star_picks,
    measurements_for_frame,
    save_star_picks,
    start_session,
    store_frame,
)
from seestar_controller.store.errors import (
    MeasurementError as StoreMeasurementError,
)
from seestar_controller.store.measurements import Measurement

#: Directory of the photometry package (source-scan audit).
PHOTOMETRY_PACKAGE_DIR = Path(photometry.__file__).resolve().parent

T0 = datetime(2026, 11, 20, 22, 0, 0, tzinfo=UTC)

# --- synthetic frames with known injected fluxes -------------------------------------------

SIZE = 64
BACKGROUND = 100.0
NOISE = 5.0
F0 = 20000.0  # constant comp peak flux
#: Star positions as (cy, cx) — row, column.
TARGET_POS = (30, 40)
COMP_POSITIONS = ((16, 16), (12, 50), (50, 12))

#: The aperture used everywhere (the defaults).
APERTURE = ApertureParams(radius_px=4.0, sky_in_px=8.0, sky_out_px=12.0)


def date_obs_str(i: int, seconds: int = 10) -> str:
    """The DATE-OBS of frame ``i``: T0 + i*seconds, naive ISOT (UTC)."""
    instant = T0 + timedelta(seconds=seconds * i)
    return instant.isoformat(timespec="milliseconds").replace("+00:00", "")


def expected_jd(i: int) -> float:
    return float(Time(date_obs_str(i), format="isot", scale="utc").jd)


def frame_data(target_peak: float, seed: int) -> np.ndarray:
    """A synthetic 64x64 frame: flat background + Gaussian stars (sigma^2=4)
    + noise, float32. The target sits at TARGET_POS with ``target_peak``, the
    3 comps at COMP_POSITIONS with constant peak ``F0``."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:SIZE, 0:SIZE]
    data = np.full((SIZE, SIZE), BACKGROUND, dtype=np.float64)
    stars = [(TARGET_POS[0], TARGET_POS[1], target_peak)] + [
        (cy, cx, F0) for cy, cx in COMP_POSITIONS
    ]
    for cy, cx, peak in stars:
        data = data + peak * np.exp(-(((yy - cy) ** 2 + (xx - cx) ** 2) / 8.0))
    data = data + rng.normal(0.0, NOISE, size=(SIZE, SIZE))
    return np.ascontiguousarray(data, dtype=np.float32)


def fits_bytes(data: np.ndarray, *, date_obs: str | None = "2026-11-20T22:00:00.000") -> bytes:
    """A small valid FITS file with the given data (+ DATE-OBS unless None)."""
    hdu = pyfits.PrimaryHDU(data=np.asarray(data, dtype=np.float32))
    if date_obs is not None:
        hdu.header["DATE-OBS"] = date_obs
    hdu.header["EXPTIME"] = 10.0
    hdu.header["GAIN"] = 50
    hdu.header["OBJECT"] = "M 42"
    buf = io.BytesIO()
    pyfits.HDUList([hdu]).writeto(buf)
    return buf.getvalue()


def make_picks(target_xy: tuple[float, float] = (40.0, 30.0)) -> list[StarPickSpec]:
    """One target + 3 comps at the synthetic star positions (x=column, y=row)."""
    return [
        StarPickSpec(role="target", x=target_xy[0], y=target_xy[1], label="Target"),
        StarPickSpec(role="comp", x=16.0, y=16.0, label="Comp A"),
        StarPickSpec(role="comp", x=50.0, y=12.0, label="Comp B"),
        StarPickSpec(role="comp", x=12.0, y=50.0, label="Comp C"),
    ]


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


# --- pure math: flux -> mag, error propagation, ensemble --------------------------------------


def test_flux_to_mag_and_flux_err_to_mag_err():
    assert flux_to_mag(1.0) == pytest.approx(0.0)
    assert flux_to_mag(100.0) == pytest.approx(-5.0)
    assert flux_to_mag(10**0.8) == pytest.approx(-2.0)  # -2.5*log10(10^0.8)
    assert MAG_ERR_COEFF == pytest.approx(1.0857, abs=1e-4)  # 2.5/ln(10)
    for bad in (0.0, -1.0, float("nan"), float("inf"), "x", None, True):
        with pytest.raises(MeasurementError):
            flux_to_mag(bad)  # ty: ignore[invalid-argument-type]
    assert flux_err_to_mag_err(100.0, 10.0) == pytest.approx(MAG_ERR_COEFF * 0.1)
    assert flux_err_to_mag_err(100.0, 0.0) == 0.0
    for bad_flux, bad_err in (
        (0.0, 1.0),
        (-5.0, 1.0),
        (float("nan"), 1.0),
        (100.0, -1.0),
        (100.0, float("inf")),
        (100.0, "x"),
    ):
        with pytest.raises(MeasurementError):
            flux_err_to_mag_err(bad_flux, bad_err)  # ty: ignore[invalid-argument-type]


def test_ensemble_and_differential_math():
    # ensemble = mean comp instrumental mag
    assert ensemble_mag([10.0, 12.0, 14.0]) == pytest.approx(12.0)
    assert ensemble_mag([5.0]) == 5.0
    with pytest.raises(MeasurementError):
        ensemble_mag([])
    with pytest.raises(MeasurementError):
        ensemble_mag([1.0, float("nan")])
    # ensemble error = sqrt(mean(comp_err^2))
    assert ensemble_mag_err([0.1, 0.2, 0.3]) == pytest.approx(
        math.sqrt((0.1**2 + 0.2**2 + 0.3**2) / 3)
    )
    assert ensemble_mag_err([0.2]) == pytest.approx(0.2)
    with pytest.raises(MeasurementError):
        ensemble_mag_err([])
    with pytest.raises(MeasurementError):
        ensemble_mag_err([0.1, -0.2])
    # differential mag = target - ensemble
    assert differential_mag(11.0, 12.0) == pytest.approx(-1.0)
    assert differential_mag(12.5, 12.0) == pytest.approx(0.5)
    # error = sqrt(target_err^2 + mean(comp_err^2))
    assert differential_mag_err(0.1, [0.2, 0.4]) == pytest.approx(
        math.sqrt(0.1**2 + (0.2**2 + 0.4**2) / 2)
    )
    assert differential_mag_err(0.0, [0.3]) == pytest.approx(0.3)
    with pytest.raises(MeasurementError):
        differential_mag_err(-0.1, [0.2])
    with pytest.raises(MeasurementError):
        differential_mag_err(0.1, [])


# --- measure_star: one star, photutils aperture + sky annulus -----------------------------------


def test_measure_star_aperture_photometry_values():
    data = frame_data(F0, seed=21)
    sm = measure_star(data, 40.0, 30.0, APERTURE, role="target", label="T")
    assert isinstance(sm, StarMeasurement)
    assert sm.role == "target" and sm.label == "T"
    assert (sm.x, sm.y) == (40.0, 30.0)
    # the expected aperture star flux: F0 * sum(exp(-r^2/8)) over the
    # r <= 4 aperture pixels (photutils 'exact' includes the boundary ring)
    yy, xx = np.mgrid[0:SIZE, 0:SIZE]
    r2 = (yy - TARGET_POS[0]) ** 2 + (xx - TARGET_POS[1]) ** 2
    expected = F0 * float(np.exp(-r2 / 8.0)[r2 <= 16.0].sum())
    assert sm.flux == pytest.approx(expected, rel=0.03)
    assert sm.flux_err > 0.0
    assert sm.mag == pytest.approx(flux_to_mag(sm.flux))
    assert sm.mag_err == pytest.approx(flux_err_to_mag_err(sm.flux, sm.flux_err))
    # out-of-image pick -> FrameSkippedError; bad aperture -> MeasurementError
    with pytest.raises(FrameSkippedError, match="outside"):
        measure_star(data, -1.0, 0.0, APERTURE)
    with pytest.raises(FrameSkippedError, match="outside"):
        measure_star(data, 0.0, float(SIZE), APERTURE)
    with pytest.raises(MeasurementError, match="ApertureParams"):
        measure_star(data, 1.0, 1.0, "nope")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


def test_measure_star_non_positive_flux_skipped():
    zeros = np.zeros((SIZE, SIZE), dtype=np.float32)
    with pytest.raises(FrameSkippedError, match="non-positive"):
        measure_star(zeros, 32.0, 32.0, APERTURE)


def test_measure_star_annulus_outside_image_skipped():
    """Issue 19: a pick whose sky annulus is not fully inside the image is
    skipped at PICK level with the distinct kind ``"annulus-outside-image"``
    — never a silently truncated (biased) annulus background."""
    data = frame_data(F0, seed=23)
    # 10 px from any edge/corner with sky_out=12: the center is inside, the
    # annulus crosses the border
    for x, y in (
        (10.0, 32.0),
        (SIZE - 10.0, 32.0),
        (32.0, 10.0),
        (32.0, SIZE - 10.0),
        (10.0, 10.0),
        (5.0, 32.0),
    ):
        with pytest.raises(FrameSkippedError, match="annulus") as excinfo:
            measure_star(data, x, y, APERTURE)
        assert excinfo.value.kind == "annulus-outside-image"
    # distinct from the center-outside-image kind (the center IS inside here)
    with pytest.raises(FrameSkippedError) as excinfo:
        measure_star(data, -1.0, 32.0, APERTURE)
    assert excinfo.value.kind == "outside-image"
    # exactly at the limit the annulus still fits: comp A at (16, 16) with
    # sky_out=16 (16-16=0 >= 0, 16+16=32 <= 63) measures fine ...
    edge = ApertureParams(radius_px=4.0, sky_in_px=12.0, sky_out_px=16.0)
    sm = measure_star(data, 16.0, 16.0, edge)
    assert sm.flux > 0.0
    # ... but one px more annulus (sky_out=17) skips the same star
    with pytest.raises(FrameSkippedError) as excinfo:
        measure_star(data, 16.0, 16.0, ApertureParams(radius_px=4.0, sky_in_px=13.0, sky_out_px=17.0))
    assert excinfo.value.kind == "annulus-outside-image"


def test_measure_star_ten_px_from_edge_skip_depends_on_annulus():
    """The issue-19 headline: a star 10 px from the edge gets a pick-level
    skip with the default annulus (sky_out=12), but measures normally with a
    smaller annulus that fits (sky_out=8) — no silent bias either way."""
    cy, cx = 32, 10  # the star sits 10 px from the left edge
    rng = np.random.default_rng(77)
    yy, xx = np.mgrid[0:SIZE, 0:SIZE]
    data = np.full((SIZE, SIZE), BACKGROUND) + F0 * np.exp(
        -(((yy - cy) ** 2 + (xx - cx) ** 2) / 8.0)
    )
    data = np.ascontiguousarray(data + rng.normal(0.0, NOISE, size=(SIZE, SIZE)), dtype=np.float32)
    # default annulus: truncated at the border -> pick-level skip
    with pytest.raises(FrameSkippedError) as excinfo:
        measure_star(data, float(cx), float(cy), APERTURE)
    assert excinfo.value.kind == "annulus-outside-image"
    # small annulus (10-8=2 >= 0): fully inside -> measured, flux recovered
    small = ApertureParams(radius_px=4.0, sky_in_px=5.0, sky_out_px=8.0)
    sm = measure_star(data, float(cx), float(cy), small)
    r2 = (yy - cy) ** 2 + (xx - cx) ** 2
    expected = F0 * float(np.exp(-r2 / 8.0)[r2 <= 16.0].sum())
    assert sm.flux == pytest.approx(expected, rel=0.03)
    assert sm.mag == pytest.approx(flux_to_mag(sm.flux))


def test_measure_frame_data_annulus_outside_image_per_pick_semantics():
    """Issue 19 + issue 14 combined at the frame level: a pick whose sky
    annulus crosses the image border is dropped PER-PICK with kind
    ``"annulus-outside-image"`` — a comp drop still leaves the frame measured
    (target + remaining comps); only a TARGET with a truncated annulus skips
    the whole frame (the raise carries the target's drop kind + drops)."""
    data = frame_data(F0, seed=24)
    header = {"DATE-OBS": date_obs_str(0)}
    # a comp 10 px from the left edge: center inside, annulus truncated ->
    # per-PICK drop, the frame is still measured on target + the other comps
    picks = make_picks()
    picks[1] = StarPickSpec(role="comp", x=10.0, y=16.0, label="Comp edge")
    fm = measure_frame_data(data, header, picks, APERTURE)
    assert len(fm.comps) == 2 and fm.mag == pytest.approx(0.0, abs=0.02)
    assert len(fm.dropped_picks) == 1
    drop = fm.dropped_picks[0]
    assert drop.role == "comp" and drop.label == "Comp edge"
    assert drop.kind == "annulus-outside-image" and "annulus" in drop.reason
    # the target near the edge skips the frame with the target's drop kind
    with pytest.raises(FrameSkippedError, match="annulus") as excinfo:
        measure_frame_data(data, header, make_picks(target_xy=(SIZE - 10.0, 30.0)), APERTURE)
    assert excinfo.value.kind == "annulus-outside-image"
    assert any(d.role == "target" for d in excinfo.value.dropped_picks)
    # the unchanged good picks still measure
    fm = measure_frame_data(data, header, make_picks(), APERTURE)
    assert fm.mag == pytest.approx(0.0, abs=0.02)


# --- measure_frame_data: the pure core on a raw array -------------------------------------------


def test_measure_frame_data_recovers_differential_mag_and_jd():
    m_inj = -0.4
    target_peak = F0 * 10 ** (-0.4 * m_inj)
    data = frame_data(target_peak, seed=11)
    header = {"DATE-OBS": date_obs_str(3)}
    fm = measure_frame_data(data, header, make_picks(), APERTURE)
    assert isinstance(fm, FrameMeasurement)
    # recovered differential mag matches the injected -2.5*log10(target/comp)
    assert fm.mag == pytest.approx(m_inj, abs=0.02)
    # JD from the header DATE-OBS (astropy Time.jd)
    assert fm.jd == pytest.approx(expected_jd(3), abs=1e-9)
    # target + comps measured
    assert fm.target.role == "target" and fm.target.label == "Target"
    assert len(fm.comps) == 3 and all(c.role == "comp" for c in fm.comps)
    assert fm.target.flux > 0.0 and fm.target.flux_err > 0.0
    # ensemble + differential wiring
    assert fm.ensemble_mag == pytest.approx(ensemble_mag([c.mag for c in fm.comps]))
    assert fm.mag == pytest.approx(differential_mag(fm.target.mag, fm.ensemble_mag))
    assert fm.mag_err == pytest.approx(
        differential_mag_err(fm.target.mag_err, [c.mag_err for c in fm.comps])
    )
    assert fm.mag_err > 0.0
    # the comps have constant flux -> their instrumental mags agree closely
    comp_mags = [c.mag for c in fm.comps]
    assert max(comp_mags) - min(comp_mags) < 0.01


def test_measure_frame_data_rgb_reduced_to_channel_mean():
    data = frame_data(F0, seed=5)  # target peak == comp peak -> diff mag ~ 0
    rgb = np.stack([data, data, data], axis=-1)
    fm = measure_frame_data(rgb, {"DATE-OBS": date_obs_str(0)}, make_picks(), APERTURE)
    assert fm.mag == pytest.approx(0.0, abs=0.02)


def test_measure_frame_data_jd_fallback_and_missing():
    data = frame_data(F0, seed=6)
    # no DATE-OBS in the header -> the explicit jd fallback is used
    fm = measure_frame_data(data, {}, make_picks(), APERTURE, jd=2460000.5)
    assert fm.jd == 2460000.5
    # a header DATE-OBS always wins over the fallback
    fm2 = measure_frame_data(
        data, {"DATE-OBS": date_obs_str(1)}, make_picks(), APERTURE, jd=2460000.5
    )
    assert fm2.jd == pytest.approx(expected_jd(1), abs=1e-9)
    # neither -> MeasurementError
    with pytest.raises(MeasurementError, match="no JD"):
        measure_frame_data(data, {}, make_picks(), APERTURE)
    with pytest.raises(MeasurementError, match="no JD"):
        measure_frame_data(data, None, make_picks(), APERTURE)


def test_measure_frame_data_out_of_image_and_bad_ensemble():
    data = frame_data(F0, seed=8)
    header = {"DATE-OBS": date_obs_str(0)}
    # target outside the image -> FrameSkippedError (issue 14: the frame is
    # skipped only when the TARGET pick is unusable; the raise carries the
    # target's drop kind + the per-pick drops)
    with pytest.raises(FrameSkippedError, match="outside") as excinfo:
        measure_frame_data(data, header, make_picks(target_xy=(-5.0, 30.0)), APERTURE)
    assert excinfo.value.kind == "outside-image"
    assert any(d.role == "target" for d in excinfo.value.dropped_picks)
    # bad ensembles raise MeasurementError before any photometry
    with pytest.raises(MeasurementError, match="exactly one"):
        measure_frame_data(data, header, [StarPickSpec(role="comp", x=16.0, y=16.0)], APERTURE)
    two_targets = [
        StarPickSpec(role="target", x=40.0, y=30.0),
        StarPickSpec(role="target", x=16.0, y=16.0),
        StarPickSpec(role="comp", x=50.0, y=12.0),
    ]
    with pytest.raises(MeasurementError, match="exactly one"):
        measure_frame_data(data, header, two_targets, APERTURE)
    with pytest.raises(MeasurementError, match="at least one"):
        measure_frame_data(data, header, [StarPickSpec(role="target", x=40.0, y=30.0)], APERTURE)
    # bad aperture / bad picks
    with pytest.raises(MeasurementError, match="aperture"):
        measure_frame_data(data, header, make_picks(), "nope")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    with pytest.raises(MeasurementError):
        measure_frame_data(data, header, [42], APERTURE)
    with pytest.raises(MeasurementError):
        measure_frame_data(data, header, "nope", APERTURE)  # type: ignore[arg-type]
    # non-2D data
    with pytest.raises(MeasurementError, match="2-D"):
        measure_frame_data(np.zeros((4, 4, 4, 4)), header, make_picks(), APERTURE)


def test_measure_frame_data_drops_one_out_of_image_comp_but_still_measures():
    """Issue 14 (per-PICK skip semantics): one comp off-edge is dropped for
    that frame with a recorded reason — the frame is STILL measured on the
    target + the remaining comps."""
    data = frame_data(F0, seed=8)
    header = {"DATE-OBS": date_obs_str(0)}
    picks = make_picks()
    picks[3] = StarPickSpec(role="comp", x=float(SIZE), y=50.0, label="Comp C")  # off-edge
    fm = measure_frame_data(data, header, picks, APERTURE)
    assert isinstance(fm, FrameMeasurement)
    # the frame was measured: target + the 2 remaining comps, correct mag
    assert len(fm.comps) == 2
    assert {c.label for c in fm.comps} == {"Comp A", "Comp B"}
    assert fm.mag == pytest.approx(0.0, abs=0.02)
    assert fm.ensemble_mag == pytest.approx(ensemble_mag([c.mag for c in fm.comps]))
    # the dropped pick is recorded with role/label/reason/kind
    assert len(fm.dropped_picks) == 1
    drop = fm.dropped_picks[0]
    assert drop.role == "comp" and drop.label == "Comp C"
    assert drop.kind == "outside-image" and "outside" in drop.reason
    # a dropped pick is not among the measured stars
    assert all(s.label != "Comp C" for s in (fm.target, *fm.comps))


def test_measure_frame_data_frame_skipped_only_when_target_unusable():
    """Issue 14: the frame is skipped only when the target pick is unusable
    (or no comp remains) — a bad comp/check pick alone never skips it."""
    data = frame_data(F0, seed=9)
    header = {"DATE-OBS": date_obs_str(0)}
    # target off-edge -> the frame is skipped, kind carried from the target's drop
    with pytest.raises(FrameSkippedError, match="outside") as excinfo:
        measure_frame_data(data, header, make_picks(target_xy=(-5.0, 30.0)), APERTURE)
    assert excinfo.value.kind == "outside-image"
    drops = excinfo.value.dropped_picks
    assert len(drops) == 1 and drops[0].role == "target" and drops[0].kind == "outside-image"
    # ALL comps off-edge (target fine) -> no comp remains -> frame skipped
    picks = make_picks()
    for i in (1, 2, 3):
        picks[i] = StarPickSpec(role="comp", x=-10.0, y=float(i), label=f"Comp {i}")
    with pytest.raises(FrameSkippedError, match="no usable comp") as excinfo2:
        measure_frame_data(data, header, picks, APERTURE)
    assert excinfo2.value.kind == "no-comps"
    assert len(excinfo2.value.dropped_picks) == 3
    assert all(d.role == "comp" for d in excinfo2.value.dropped_picks)
    # only the CHECK pick off-edge -> still measured, check is None, drop recorded
    picks = make_picks() + [StarPickSpec(role="check", x=float(SIZE), y=50.0, label="Check")]
    fm = measure_frame_data(data, header, picks, APERTURE)
    assert fm.check is None
    assert fm.check_mag is None and fm.target_minus_check is None
    assert len(fm.comps) == 3 and fm.mag == pytest.approx(0.0, abs=0.02)
    assert len(fm.dropped_picks) == 1
    assert fm.dropped_picks[0].role == "check" and fm.dropped_picks[0].kind == "outside-image"
    # a comp with non-positive flux (an all-zero region) is dropped the same
    # way: zero the comp's neighbourhood only
    data2 = frame_data(F0, seed=9).copy()
    data2[44:57, 6:19] = 0.0  # Comp C at (12, 50) sits in a dark patch
    picks2 = make_picks()
    fm2 = measure_frame_data(data2, header, picks2, APERTURE)
    assert len(fm2.comps) == 2
    assert len(fm2.dropped_picks) == 1 and fm2.dropped_picks[0].kind == "non-positive-flux"


# --- measure_frame: one stored frame -> store Measurement -----------------------------------------


def test_measure_frame_stores_measurement_with_header_jd(project):
    session = start_session(project, clock=frozen(T0))
    m_inj = 0.25
    target_peak = F0 * 10 ** (-0.4 * m_inj)
    frame = store_frame(
        project,
        session,
        KIND_SUB,
        fits_bytes(frame_data(target_peak, seed=7), date_obs=date_obs_str(0)),
        meta={"target": "M 42", "exposure": 10.0, "gain": 50},
    )
    meas = measure_frame(project, frame.id, make_picks(), APERTURE)
    assert isinstance(meas, Measurement)
    assert meas.frame_id == frame.id and meas.session_id == session
    assert meas.mag == pytest.approx(m_inj, abs=0.02)
    assert meas.mag_err is not None and meas.mag_err > 0.0
    assert not meas.excluded
    # JD from the frame's FITS DATE-OBS header
    assert meas.jd == pytest.approx(expected_jd(0), abs=1e-9)
    assert measurements_for_frame(project, frame.id) == [meas]
    assert lightcurve(project) == [meas]


def test_measure_frame_jd_falls_back_to_frame_jd(project):
    session = start_session(project, clock=frozen(T0))
    # no DATE-OBS -> store_frame used the file mtime for the frame's jd
    frame = store_frame(
        project, session, KIND_SUB, fits_bytes(frame_data(F0, seed=3), date_obs=None)
    )
    meas = measure_frame(project, frame.id, make_picks(), APERTURE)
    assert meas.jd == pytest.approx(frame.jd, abs=1e-12)


# --- regression: Seestar-style FITS headers with non-ASCII / control chars ---------------
# Real Seestar S50 FITS files carry two consecutive COMMENT cards (the FITS-standard
# paper citation). dict(astropy_header) joins them into ONE value containing "\n",
# and re-inserting such a value into an astropy Header — which astropy.wcs.WCS does
# for every card — raises ValueError("FITS header values must contain standard
# printable ASCII characters; "\n  and Astrophysics'..." ..."). The photometry read
# path must sanitize such headers (store.frames.header_to_dict).


def seestar_style_fits_bytes(
    data: np.ndarray, *, date_obs: str | None = "2026-11-20T22:00:00.000"
) -> bytes:
    """A small valid FITS file mimicking the Seestar S50: two consecutive COMMENT
    cards (whose joined dict value contains ``\\n``) plus a simple CAR WCS."""
    hdu = pyfits.PrimaryHDU(data=np.asarray(data, dtype=np.float32))
    hdu.header["COMMENT"] = (
        "FITS (Flexible Image Transport System) format is defined in 'Astronomy"
    )
    hdu.header["COMMENT"] = (
        "and Astrophysics', volume 376, page 359; bibcode: 2001A&A...376..359H"
    )
    if date_obs is not None:
        hdu.header["DATE-OBS"] = date_obs
    hdu.header["EXPTIME"] = 10.0
    hdu.header["GAIN"] = 50
    hdu.header["OBJECT"] = "M 42"
    # a simple 2-axis CAR WCS (exactly linear), reference pixel = 1-based (33, 33)
    hdu.header["CTYPE1"] = "RA---CAR"
    hdu.header["CTYPE2"] = "DEC--CAR"
    hdu.header["CRVAL1"] = 83.0
    hdu.header["CRVAL2"] = 22.0
    hdu.header["CRPIX1"] = 33.0
    hdu.header["CRPIX2"] = 33.0
    hdu.header["CDELT1"] = -0.001
    hdu.header["CDELT2"] = 0.001
    buf = io.BytesIO()
    pyfits.HDUList([hdu]).writeto(buf)
    return buf.getvalue()


def test_read_path_sanitizes_seestar_comment_cards(project):
    """The full photometry read path on a Seestar-style FITS (two COMMENT cards
    -> a ``\\n`` in the joined header value): the header dict is sanitized, the
    WCS build (the pre-fix crash site) works, and the frame is measured."""
    from astropy.wcs import WCS

    from seestar_controller.photometry.measure import _read_frame_fits

    session = start_session(project, clock=frozen(T0))
    frame = store_frame(
        project,
        session,
        KIND_SUB,
        seestar_style_fits_bytes(frame_data(F0, seed=3), date_obs=date_obs_str(0)),
        meta={"target": "M 42", "exposure": 10.0, "gain": 50},
    )
    data, header = _read_frame_fits(project, frame)
    assert data is not None
    comment = header.get("COMMENT")
    assert isinstance(comment, str)
    assert "\n" not in comment
    assert all(32 <= ord(c) <= 126 for c in comment)
    assert "Astronomy" in comment and "Astrophysics" in comment
    for value in header.values():  # every string value is printable ASCII
        if isinstance(value, str):
            assert all(32 <= ord(c) <= 126 for c in value)
    # the exact pre-fix failure: WCS(dict) re-inserts every card into a Header
    WCS(header)  # must not raise
    # the GUI pick path on the sanitized header works (0-based (32, 32) == CRPIX)
    coord = pixel_to_sky(header, 32.0, 32.0)
    assert coord.ra_deg == pytest.approx(83.0, abs=1e-9)
    assert coord.dec_deg == pytest.approx(22.0, abs=1e-9)
    # and the frame is measured through the photometry read path
    meas = measure_frame(project, frame.id, make_picks(), APERTURE)
    assert meas.frame_id == frame.id
    assert meas.mag == pytest.approx(0.0, abs=0.02)  # target peak == comp peak F0
    assert meas.jd == pytest.approx(expected_jd(0), abs=1e-9)


def test_measure_frame_unknown_frame_and_skip(project):
    session = start_session(project, clock=frozen(T0))
    frame = store_frame(
        project, session, KIND_SUB, fits_bytes(frame_data(F0, seed=13), date_obs=date_obs_str(0))
    )
    # unknown frame id -> the PHOTOMETRY MeasurementError (not the store's)
    with pytest.raises(MeasurementError) as excinfo:
        measure_frame(project, 9999, make_picks(), APERTURE)
    assert isinstance(excinfo.value, PhotometryError)
    assert isinstance(excinfo.value, ValueError)
    assert not isinstance(excinfo.value, StoreMeasurementError)
    with pytest.raises(MeasurementError):
        measure_frame(project, "1", make_picks(), APERTURE)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    # out-of-frame pick -> FrameSkippedError (a MeasurementError subclass)
    with pytest.raises(FrameSkippedError):
        measure_frame(project, frame.id, make_picks(target_xy=(999.0, 0.0)), APERTURE)
    assert lightcurve(project) == []


# --- measure_project: all sub-frames, sinusoid recovery, skips, subs-only ---------------------------


def test_measure_project_recovers_injected_sinusoid_and_amplitude(project):
    session = start_session(project, clock=frozen(T0))
    n = 12
    amp = 0.3
    injected: list[float] = []
    frames = []
    for i in range(n):
        m_inj = amp * math.sin(2.0 * math.pi * i / n)
        injected.append(m_inj)
        target_peak = F0 * 10 ** (-0.4 * m_inj)
        frame = store_frame(
            project,
            session,
            KIND_SUB,
            fits_bytes(frame_data(target_peak, seed=100 + i), date_obs=date_obs_str(i)),
        )
        frames.append(frame)
    # a STACK frame must NEVER be measured
    store_frame(
        project,
        session,
        KIND_STACK,
        fits_bytes(frame_data(F0 * 2.0, seed=999), date_obs=date_obs_str(50)),
    )

    report = measure_project(project, make_picks(), APERTURE)
    assert isinstance(report, MeasureReport)
    assert report.frames == n  # only the sub-frames, not the stack
    assert len(report.measurements) == n
    assert report.skipped == ()

    lc = lightcurve(project)
    assert len(lc) == n
    assert [m.frame_id for m in lc] == [f.id for f in frames]  # ordered by jd
    for meas, m_inj, i in zip(lc, injected, range(n)):
        # recovered differential mag matches the injected -2.5*log10(target/comp)
        assert meas.mag == pytest.approx(m_inj, abs=0.02)
        assert meas.mag_err is not None and meas.mag_err > 0.0
        # JD from each frame's DATE-OBS
        assert meas.jd == pytest.approx(expected_jd(i), abs=1e-9)
    # the injected amplitude is recovered
    mags = [m.mag for m in lc]
    recovered_amp = (max(mags) - min(mags)) / 2.0
    assert recovered_amp == pytest.approx(amp, abs=0.02)
    # the report's measurements are the stored lightcurve points
    assert [m.id for m in report.measurements] == [m.id for m in lc]


def test_measure_project_skips_out_of_frame_picks_without_crashing(project):
    session = start_session(project, clock=frozen(T0))
    for i in range(3):
        store_frame(
            project,
            session,
            KIND_SUB,
            fits_bytes(frame_data(F0, seed=30 + i), date_obs=date_obs_str(i)),
        )
    report = measure_project(project, make_picks(target_xy=(-5.0, 30.0)), APERTURE)
    assert report.measurements == ()
    assert report.frames == 3
    assert len(report.skipped) == 3
    assert all(isinstance(s, SkippedFrame) and "outside" in s.reason for s in report.skipped)
    assert all(s.kind == "outside-image" for s in report.skipped)
    # issue 14: the skipped frames carry their per-pick drops (the target's),
    # aggregated separately from the frame-level skip reasons
    assert all(len(s.dropped_picks) == 1 and s.dropped_picks[0].role == "target" for s in report.skipped)
    assert report.skip_reasons == {"outside-image": 3}  # frame-level
    assert report.pick_skip_reasons == {"pick-outside-image": 3}  # pick-level
    assert report.pick_skip_reasons_by_label == {"Target": {"pick-outside-image": 3}}
    assert "pick-outside-image×3" in report.pick_skip_summary
    assert lightcurve(project) == []


def test_measure_project_annulus_outside_image_never_silently_measured(project):
    """Issue 19 at the run level (under issue 14 per-PICK skip semantics): a
    pick whose sky annulus crosses the image border is never measured with a
    truncated (biased) annulus — as a COMP it is dropped per-pick with kind
    ``"annulus-outside-image"`` (aggregated as ``pick-annulus-outside-image``,
    the frame still measured on the remaining picks); as the TARGET the
    whole frame is skipped with the frame-level kind
    ``"annulus-outside-image"``. Either way no biased measurement lands in
    the lightcurve."""
    session = start_session(project, clock=frozen(T0))
    for i in range(3):
        store_frame(
            project,
            session,
            KIND_SUB,
            fits_bytes(frame_data(F0, seed=60 + i), date_obs=date_obs_str(i)),
        )
    # a comp 10 px from the left edge: center inside, annulus truncated ->
    # per-PICK drop (issue 14); the frame is still measured on target + 2 comps
    picks = make_picks()
    picks[1] = StarPickSpec(role="comp", x=10.0, y=16.0, label="Comp edge")
    report = measure_project(project, picks, APERTURE)
    assert len(report.measurements) == 3 and report.skipped == ()
    assert report.skip_reasons == {} and report.skip_summary == "no frames skipped"
    assert all(
        len(fm.dropped_picks) == 1
        and fm.dropped_picks[0].role == "comp"
        and fm.dropped_picks[0].label == "Comp edge"
        and fm.dropped_picks[0].kind == "annulus-outside-image"
        and "annulus" in fm.dropped_picks[0].reason
        and len(fm.comps) == 2
        for fm in report.frame_results
    )
    assert report.pick_skip_reasons == {"pick-annulus-outside-image": 3}
    assert report.pick_skip_reasons_by_label == {
        "Comp edge": {"pick-annulus-outside-image": 3}
    }
    assert "pick-annulus-outside-image×3" in report.pick_skip_summary
    assert len(lightcurve(project)) == 3  # measured WITHOUT the biased pick
    # the TARGET near the edge -> the whole frame is skipped, the frame-level
    # skip carries the target's drop kind (issue 14)
    report2 = measure_project(project, make_picks(target_xy=(SIZE - 10.0, 30.0)), APERTURE)
    assert report2.measurements == ()
    assert report2.frames == 3
    assert len(report2.skipped) == 3
    assert all(
        isinstance(s, SkippedFrame)
        and s.kind == "annulus-outside-image"
        and "annulus" in s.reason
        and len(s.dropped_picks) == 1
        and s.dropped_picks[0].role == "target"
        for s in report2.skipped
    )
    assert report2.skip_reasons == {"annulus-outside-image": 3}
    assert "annulus-outside-image×3" in report2.skip_summary
    assert report2.pick_skip_reasons == {"pick-annulus-outside-image": 3}


def test_measure_project_records_pick_level_drops_on_measured_frames(project):
    """Issue 14: one comp off-edge on every frame -> every frame is STILL
    measured (target + 2 comps) and the dropped pick is recorded per frame;
    the report aggregates the pick-level drops apart from frame-level skips."""
    session = start_session(project, clock=frozen(T0))
    n = 3
    for i in range(n):
        store_frame(
            project,
            session,
            KIND_SUB,
            fits_bytes(frame_data(F0, seed=70 + i), date_obs=date_obs_str(i)),
        )
    picks = make_picks()
    picks[3] = StarPickSpec(role="comp", x=float(SIZE), y=50.0, label="Comp C")  # off-edge
    report = measure_project(project, picks, APERTURE)
    # every frame measured — no frame-level skip at all
    assert len(report.measurements) == n and report.skipped == ()
    assert report.skip_reasons == {} and report.skip_summary == "no frames skipped"
    # each measured frame carries the one dropped pick and used 2 comps
    assert all(len(fm.dropped_picks) == 1 for fm in report.frame_results)
    assert all(
        fm.dropped_picks[0].role == "comp"
        and fm.dropped_picks[0].label == "Comp C"
        and fm.dropped_picks[0].kind == "outside-image"
        for fm in report.frame_results
    )
    assert all(len(fm.comps) == 2 for fm in report.frame_results)
    assert all(abs(fm.mag) < 0.02 for fm in report.frame_results)
    # the pick-level aggregation on the report (frame-level vs pick-level)
    assert report.dropped_picks == tuple(
        d for fm in report.frame_results for d in fm.dropped_picks
    )
    assert report.pick_skip_reasons == {"pick-outside-image": n}
    assert report.pick_skip_reasons_by_label == {"Comp C": {"pick-outside-image": n}}
    assert report.pick_skip_summary == (
        f"{n} pick drops (pick-outside-image×{n} — Comp C×{n})"
    )
    assert len(lightcurve(project)) == n


def test_measure_project_skips_non_positive_flux_frame(project):
    session = start_session(project, clock=frozen(T0))
    store_frame(
        project, session, KIND_SUB, fits_bytes(frame_data(F0, seed=40), date_obs=date_obs_str(0))
    )
    # an all-zero frame: background-subtracted flux is exactly 0 -> skipped
    store_frame(
        project,
        session,
        KIND_SUB,
        fits_bytes(np.zeros((SIZE, SIZE), dtype=np.float32), date_obs=date_obs_str(1)),
    )
    store_frame(
        project, session, KIND_SUB, fits_bytes(frame_data(F0, seed=42), date_obs=date_obs_str(2))
    )
    report = measure_project(project, make_picks(), APERTURE)
    assert len(report.measurements) == 2
    assert len(report.skipped) == 1
    assert "non-positive" in report.skipped[0].reason
    assert len(lightcurve(project)) == 2


def test_measure_project_bad_ensemble_raises_before_measuring(project):
    session = start_session(project, clock=frozen(T0))
    store_frame(
        project, session, KIND_SUB, fits_bytes(frame_data(F0, seed=50), date_obs=date_obs_str(0))
    )
    with pytest.raises(MeasurementError, match="exactly one"):
        measure_project(project, [StarPickSpec(role="comp", x=16.0, y=16.0)], APERTURE)
    assert lightcurve(project) == []


def test_measure_project_with_store_star_picks(project):
    """Store StarPick records (per-pick aperture) work directly as picks."""
    session = start_session(project, clock=frozen(T0))
    m_inj = 0.2
    store_frame(
        project,
        session,
        KIND_SUB,
        fits_bytes(frame_data(F0 * 10 ** (-0.4 * m_inj), seed=60), date_obs=date_obs_str(0)),
    )
    stored = [
        StarPick(
            role="target", label="Target", x=40.0, y=30.0, ra_deg=None, dec_deg=None,
            aperture_radius=4.0, sky_annulus_inner=8.0, sky_annulus_outer=12.0,
        ),
        StarPick(
            role="comp", label="Comp A", x=16.0, y=16.0, ra_deg=None, dec_deg=None,
            aperture_radius=4.0, sky_annulus_inner=8.0, sky_annulus_outer=12.0,
        ),
        StarPick(
            role="comp", label="Comp B", x=50.0, y=12.0, ra_deg=None, dec_deg=None,
            aperture_radius=4.0, sky_annulus_inner=8.0, sky_annulus_outer=12.0,
        ),
        StarPick(
            role="comp", label="Comp C", x=12.0, y=50.0, ra_deg=None, dec_deg=None,
            aperture_radius=4.0, sky_annulus_inner=8.0, sky_annulus_outer=12.0,
        ),
    ]
    save_star_picks(project, stored)
    report = measure_project(project, load_star_picks(project))  # no aperture arg needed
    assert len(report.measurements) == 1 and report.skipped == ()
    assert report.measurements[0].mag == pytest.approx(m_inj, abs=0.02)


# --- package boundary (subprocess + source scan) + public API ---------------------------------------


def test_measure_module_imports_no_gi_no_link_no_ui_no_enhance_and_widgets_stay_lazy():
    code = (
        "import sys;"
        "import seestar_controller.photometry;"
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


def test_measure_module_source_scan_no_gi_and_package_module_set():
    source = (PHOTOMETRY_PACKAGE_DIR / "measure.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(import gi|from gi)\b", source, re.MULTILINE), (
        "pure module measure imports gi"
    )
    modules = {path.stem for path in PHOTOMETRY_PACKAGE_DIR.glob("*.py")}
    assert modules == {
        "__init__",
        "picking",
        "suggest",
        "measure",
        "run",
        "star_picker",
        "suggestions_panel",
        "results_view",
    }


def test_measure_public_api_is_exported_from_package():
    for name in (
        "MAG_ERR_COEFF",
        "DroppedPick",
        "FrameMeasurement",
        "FrameSkippedError",
        "MeasureReport",
        "MeasurementError",
        "SkippedFrame",
        "StarMeasurement",
        "differential_mag",
        "differential_mag_err",
        "ensemble_mag",
        "ensemble_mag_err",
        "flux_err_to_mag_err",
        "flux_to_mag",
        "measure_frame",
        "measure_frame_data",
        "measure_project",
        "measure_star",
    ):
        assert name in photometry.__all__, name
        assert hasattr(photometry, name), name
    assert photometry.measure_frame is measure.measure_frame
    assert photometry.measure_project is measure.measure_project
    assert issubclass(photometry.MeasurementError, photometry.PhotometryError)
    assert issubclass(photometry.MeasurementError, ValueError)
    # deliberately distinct from the store's MeasurementError
    assert not issubclass(photometry.MeasurementError, StoreMeasurementError)
    assert issubclass(photometry.FrameSkippedError, photometry.MeasurementError)
