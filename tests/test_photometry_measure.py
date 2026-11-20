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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits as pyfits
from astropy.time import Time

import seestar_controller.photometry as photometry
from seestar_controller.photometry import measure
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
    measure_frame,
    measure_frame_data,
    measure_project,
    measure_star,
)
from seestar_controller.photometry.measure import (
    MAG_ERR_COEFF,
    FrameMeasurement,
    MeasureReport,
    MeasurementError,
    SkippedFrame,
    StarMeasurement,
)
from seestar_controller.store import (
    KIND_STACK,
    KIND_SUB,
    StarPick,
    create_project,
    lightcurve,
    list_frames,
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

T0 = datetime(2026, 11, 20, 22, 0, 0, tzinfo=timezone.utc)

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
            flux_to_mag(bad)
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
            flux_err_to_mag_err(bad_flux, bad_err)


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
        measure_star(data, 1.0, 1.0, "nope")  # type: ignore[arg-type]


def test_measure_star_non_positive_flux_skipped():
    zeros = np.zeros((SIZE, SIZE), dtype=np.float32)
    with pytest.raises(FrameSkippedError, match="non-positive"):
        measure_star(zeros, 32.0, 32.0, APERTURE)


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
    # target outside the image -> FrameSkippedError
    with pytest.raises(FrameSkippedError, match="outside"):
        measure_frame_data(data, header, make_picks(target_xy=(-5.0, 30.0)), APERTURE)
    # a comp outside the image -> skipped too
    picks = make_picks()
    picks[1] = StarPickSpec(role="comp", x=float(SIZE), y=16.0)
    with pytest.raises(FrameSkippedError, match="outside"):
        measure_frame_data(data, header, picks, APERTURE)
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
        measure_frame_data(data, header, make_picks(), "nope")  # type: ignore[arg-type]
    with pytest.raises(MeasurementError):
        measure_frame_data(data, header, [42], APERTURE)
    with pytest.raises(MeasurementError):
        measure_frame_data(data, header, "nope", APERTURE)  # type: ignore[arg-type]
    # non-2D data
    with pytest.raises(MeasurementError, match="2-D"):
        measure_frame_data(np.zeros((4, 4, 4, 4)), header, make_picks(), APERTURE)


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
        measure_frame(project, "1", make_picks(), APERTURE)  # type: ignore[arg-type]
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
    assert lightcurve(project) == []


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
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
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
