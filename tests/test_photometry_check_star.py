"""Pure-layer tests for the check-star role in the photometry core
(check-star-plot ticket 01).

Headless: no ``gi`` anywhere (the sandbox has no GTK, see
docs/wiki/assumptions.md) and NO NETWORK — everything runs on SYNTHETIC
FITS frames with known injected fluxes, built in-memory and stored in a
tmp project via :mod:`seestar_controller.store` (the same helpers/pattern
as tests/test_photometry_measure.py and tests/test_photometry_run.py).

Covered:

- synthetic CLOUD-DIMMING frames (every star in the field dimmed by the
  same factor ``d``): the comp ensemble tracks the cloud
  (``-2.5*log10(d)`` — proving the cloud is really in the data), the
  target's and the check star's differential mags co-move with the
  ensemble (both stay ~0 within errors), and the target-minus-check
  curve is flat within errors per frame;
- cloud + intrinsic target variation: the target's diff mag recovers the
  injected variation, the check star stays flat, and target-minus-check
  recovers the injected variation (the cloud cancels);
- the check star is PROVABLY EXCLUDED from the comp ensemble: measuring
  the same frame with vs without the check pick gives identical
  comps/ensemble/target mag, and a 10x brighter check star does not
  shift the target mag at all;
- the per-frame check differential mag + error are computed EXACTLY like
  the target's (same ensemble, same error propagation) and exposed via
  :func:`~seestar_controller.photometry.measure.measure_frame_data`
  (``FrameMeasurement.check_mag`` / ``check_mag_err`` /
  ``target_minus_check`` / ``target_minus_check_err``), via
  :func:`~seestar_controller.photometry.measure.measure_project`
  (``MeasureReport.frame_results``) and via the run report
  (``RunReport.frame_results`` / ``check_points`` /
  ``target_minus_check_points``);
- the check-star choice persists per project (store save/load round-trip,
  project.json mirror, load order target -> check -> comps);
- validation: at most one check pick per ensemble;
- the package boundary: a subprocess test (no ``gi``, no ``ui``, no
  ``link`` in ``sys.modules``) + a source-scan audit.
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
from seestar_controller import store as store_package
from seestar_controller.photometry import (
    ApertureParams,
    StarPickSpec,
    measure_frame_data,
    measure_project,
)
from seestar_controller.photometry.measure import (
    FrameMeasurement,
    MeasurementError,
    MeasureReport,
)
from seestar_controller.photometry.run import RunController, RunReport
from seestar_controller.store import (
    KIND_SUB,
    StarPick,
    create_project,
    load_star_picks,
    save_star_picks,
    start_session,
    store_frame,
)
from seestar_controller.store.stars import ROLE_CHECK

#: Directory of the photometry package (source-scan audit).
PHOTOMETRY_PACKAGE_DIR = Path(photometry.__file__).resolve().parent
#: Directory of the store package (source-scan audit of stars.py).
STORE_PACKAGE_DIR = Path(store_package.__file__).resolve().parent

T0 = datetime(2026, 11, 20, 22, 0, 0, tzinfo=UTC)

# --- synthetic frames with known injected fluxes (same as test_photometry_measure.py) -------------

SIZE = 64
BACKGROUND = 100.0
NOISE = 5.0
F0 = 20000.0  # constant comp peak flux
#: Star positions as (cy, cx) — row, column.
TARGET_POS = (30, 40)
COMP_POSITIONS = ((16, 16), (12, 50), (50, 12))
#: The check star sits at a free spot, well clear of every other star's
#: aperture + sky annulus (nearest star is the target, ~19.7 px away).
CHECK_POS = (48, 48)

#: The aperture used everywhere (the defaults).
APERTURE = ApertureParams(radius_px=4.0, sky_in_px=8.0, sky_out_px=12.0)


def date_obs_str(i: int, seconds: int = 10) -> str:
    """The DATE-OBS of frame ``i``: T0 + i*seconds, naive ISOT (UTC)."""
    instant = T0 + timedelta(seconds=seconds * i)
    return instant.isoformat(timespec="milliseconds").replace("+00:00", "")


def expected_jd(i: int) -> float:
    return float(Time(date_obs_str(i), format="isot", scale="utc").jd)


def frame_data(
    target_peak: float,
    seed: int,
    *,
    dim: float = 1.0,
    check_peak: float = F0,
) -> np.ndarray:
    """A synthetic 64x64 frame: flat background + Gaussian stars
    (sigma^2=4) + noise, float32. The target sits at TARGET_POS with
    ``target_peak``, the check star at CHECK_POS with ``check_peak`` and
    the 3 comps at COMP_POSITIONS with peak ``F0`` — every star dimmed by
    the cloud factor ``dim`` (a cloud dims the whole field)."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:SIZE, 0:SIZE]
    data = np.full((SIZE, SIZE), BACKGROUND, dtype=np.float64)
    stars = [
        (TARGET_POS[0], TARGET_POS[1], target_peak * dim),
        (CHECK_POS[0], CHECK_POS[1], check_peak * dim),
    ] + [(cy, cx, F0 * dim) for cy, cx in COMP_POSITIONS]
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


def make_picks(*, with_check: bool = True) -> list[StarPickSpec]:
    """One target + 3 comps (+ one check star) at the synthetic star
    positions (x=column, y=row)."""
    picks = [
        StarPickSpec(role="target", x=40.0, y=30.0, label="Target"),
        StarPickSpec(role="comp", x=16.0, y=16.0, label="Comp A"),
        StarPickSpec(role="comp", x=50.0, y=12.0, label="Comp B"),
        StarPickSpec(role="comp", x=12.0, y=50.0, label="Comp C"),
    ]
    if with_check:
        picks.append(StarPickSpec(role=ROLE_CHECK, x=48.0, y=48.0, label="Check K"))
    return picks


def store_picks(project, *, with_check: bool = True) -> list[StarPick]:
    """Persist the synthetic picks into the project as store StarPick records."""
    stored = [
        StarPick(
            role=p.role, label=p.label, x=p.x, y=p.y, ra_deg=None, dec_deg=None,
            aperture_radius=4.0, sky_annulus_inner=8.0, sky_annulus_outer=12.0,
        )
        for p in make_picks(with_check=with_check)
    ]
    save_star_picks(project, stored)
    return stored


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


# --- cloud-dimming frames: target & check co-move, target-minus-check flat -------------------------


def test_cloud_dimming_target_and_check_comove_and_tmc_flat_within_errors():
    dims = [1.0, 0.7, 0.4, 0.9, 0.55, 1.0]
    fms: list[FrameMeasurement] = []
    for i, d in enumerate(dims):
        # a cloud dims EVERY star by d; no intrinsic variation
        data = frame_data(F0, seed=200 + i, dim=d)
        fm = measure_frame_data(
            data, {"DATE-OBS": date_obs_str(i)}, make_picks(), APERTURE
        )
        fms.append(fm)
    base = fms[0]
    for i, (d, fm) in enumerate(zip(dims, fms)):
        cloud = -2.5 * math.log10(d / dims[0])
        # the cloud is really in the data: the comp ensemble tracks it
        assert fm.ensemble_mag - base.ensemble_mag == pytest.approx(cloud, abs=0.02)
        # the target co-moves with the ensemble: its diff mag stays ~0
        assert abs(fm.mag) < 3.0 * fm.mag_err
        # the check star co-moves too: its diff mag stays ~0 within errors
        assert fm.check is not None and fm.check.role == ROLE_CHECK
        assert fm.check_mag is not None and fm.check_mag_err is not None
        assert abs(fm.check_mag) < 3.0 * fm.check_mag_err
        # the target-minus-check curve is flat within errors
        assert fm.target_minus_check is not None and fm.target_minus_check_err is not None
        assert abs(fm.target_minus_check) < 3.0 * fm.target_minus_check_err
        # exact identities: tmc == mag - check_mag (the ensemble cancels)
        assert fm.target_minus_check == pytest.approx(fm.mag - fm.check_mag, abs=1e-12)
        # the check star is measured EXACTLY like the target: same ensemble,
        # same error propagation
        assert fm.check_mag == pytest.approx(fm.check.mag - fm.ensemble_mag, abs=1e-12)
        comp_err_sq_mean = sum(c.mag_err**2 for c in fm.comps) / len(fm.comps)
        assert fm.check_mag_err == pytest.approx(
            math.sqrt(fm.check.mag_err**2 + comp_err_sq_mean), abs=1e-12
        )
        assert fm.mag_err == pytest.approx(
            math.sqrt(fm.target.mag_err**2 + comp_err_sq_mean), abs=1e-12
        )
        assert fm.target_minus_check_err == pytest.approx(
            math.sqrt(fm.target.mag_err**2 + fm.check.mag_err**2), abs=1e-12
        )
        assert fm.jd == pytest.approx(expected_jd(i), abs=1e-9)


def test_cloud_plus_intrinsic_target_variation_check_flat_tmc_recovers_signal():
    n = 6
    dims = [1.0, 0.8, 0.5, 0.95, 0.6, 1.0]
    for i in range(n):
        m_inj = 0.3 * math.sin(2.0 * math.pi * i / n)
        d = dims[i]
        # the target varies intrinsically (m_inj) AND the cloud dims all
        data = frame_data(F0 * 10 ** (-0.4 * m_inj), seed=300 + i, dim=d)
        fm = measure_frame_data(
            data, {"DATE-OBS": date_obs_str(i)}, make_picks(), APERTURE
        )
        # the target's diff mag recovers the injected variation despite the cloud
        assert fm.mag == pytest.approx(m_inj, abs=0.02)
        # the check star has no intrinsic variation: it stays flat within errors
        assert fm.check_mag is not None and fm.check_mag_err is not None
        assert abs(fm.check_mag) < 3.0 * fm.check_mag_err
        # target-minus-check recovers the injected variation (the cloud cancels)
        assert fm.target_minus_check == pytest.approx(m_inj, abs=0.03)


# --- the check star is provably EXCLUDED from the comp ensemble ------------------------------------


def test_ensemble_provably_excludes_the_check_star():
    header = {"DATE-OBS": date_obs_str(0)}
    data = frame_data(F0, seed=42, dim=0.8)
    with_check = measure_frame_data(data, header, make_picks(with_check=True), APERTURE)
    without = measure_frame_data(data, header, make_picks(with_check=False), APERTURE)
    # without a check pick the check fields are None
    assert without.check is None
    assert without.check_mag is None and without.check_mag_err is None
    assert without.target_minus_check is None and without.target_minus_check_err is None
    # with a check pick: identical comps / ensemble / target photometry —
    # the check star does NOT enter the ensemble
    assert with_check.check is not None
    assert with_check.comps == without.comps
    assert all(s.role == "comp" for s in with_check.comps)
    assert with_check.check not in with_check.comps
    assert with_check.ensemble_mag == without.ensemble_mag
    assert with_check.mag == without.mag
    assert with_check.mag_err == without.mag_err
    # a WILDLY brighter check star (10x flux) still does not shift the
    # target mag or the ensemble: same frame, with vs without the check pick
    bright = frame_data(F0, seed=42, dim=0.8, check_peak=10.0 * F0)
    fm_bright = measure_frame_data(bright, header, make_picks(), APERTURE)
    fm_bright_nochk = measure_frame_data(bright, header, make_picks(with_check=False), APERTURE)
    assert fm_bright.mag == fm_bright_nochk.mag
    assert fm_bright.mag_err == fm_bright_nochk.mag_err
    assert fm_bright.ensemble_mag == fm_bright_nochk.ensemble_mag
    assert fm_bright.comps == fm_bright_nochk.comps
    # ... but its own diff mag is 2.5 mag brighter (-2.5*log10(10)); the
    # comps sit >= 36 px away, so the ensemble is identical between the two
    # frames and the check_mag difference is exactly the flux ratio
    assert fm_bright.check_mag is not None and with_check.check_mag is not None
    assert fm_bright.ensemble_mag == pytest.approx(with_check.ensemble_mag, abs=1e-9)
    assert fm_bright.check_mag == pytest.approx(with_check.check_mag - 2.5, abs=0.02)


def test_measure_rejects_two_check_picks():
    picks = make_picks() + [StarPickSpec(role=ROLE_CHECK, x=20.0, y=20.0, label="Check 2")]
    with pytest.raises(MeasurementError, match="at most one 'check'"):
        measure_frame_data(
            frame_data(F0, seed=1), {"DATE-OBS": date_obs_str(0)}, picks, APERTURE
        )


# --- run report: per-frame check curves exposed via RunReport --------------------------------------


def test_run_session_report_carries_check_star_curves(project):
    session = start_session(project, clock=frozen(T0))
    dims = [1.0, 0.7, 0.4, 0.9, 0.55, 1.0]
    for i, d in enumerate(dims):
        store_frame(
            project,
            session,
            KIND_SUB,
            fits_bytes(frame_data(F0, seed=400 + i, dim=d), date_obs=date_obs_str(i)),
        )
    store_picks(project, with_check=True)
    ctrl = RunController(project=project)
    report = ctrl.run_session(session)

    assert isinstance(report, RunReport)
    assert len(report.measurements) == 6 and report.skipped == ()
    assert len(report.frame_results) == 6
    for fm, meas in zip(report.frame_results, report.measurements):
        assert fm.check is not None and fm.check.role == ROLE_CHECK
        assert fm.check_mag is not None and fm.check_mag_err is not None
        assert fm.check_mag_err > 0.0
        assert fm.target_minus_check is not None and fm.target_minus_check_err is not None
        assert fm.jd == pytest.approx(meas.jd, abs=1e-9)  # aligned with the measurements
        # cloud-dimmed frames: check curve + tmc flat within errors
        assert abs(fm.check_mag) < 3.0 * fm.check_mag_err
        assert abs(fm.target_minus_check) < 3.0 * fm.target_minus_check_err
    # the run-level curves are exactly the per-frame values, in frame order
    assert report.check_points == tuple(
        (fm.jd, fm.check_mag, fm.check_mag_err) for fm in report.frame_results
    )
    assert report.target_minus_check_points == tuple(
        (fm.jd, fm.target_minus_check, fm.target_minus_check_err) for fm in report.frame_results
    )
    assert [p[0] for p in report.check_points] == [m.jd for m in report.measurements]
    assert [p[0] for p in report.target_minus_check_points] == [m.jd for m in report.measurements]
    assert all(p[2] > 0.0 for p in report.check_points)
    assert all(p[2] > 0.0 for p in report.target_minus_check_points)


def test_run_session_without_check_star_has_empty_check_curves(project):
    session = start_session(project, clock=frozen(T0))
    for i in range(3):
        store_frame(
            project,
            session,
            KIND_SUB,
            fits_bytes(frame_data(F0, seed=500 + i), date_obs=date_obs_str(i)),
        )
    store_picks(project, with_check=False)
    ctrl = RunController(project=project)
    report = ctrl.run_session(session)
    assert len(report.frame_results) == 3
    assert all(
        fm.check is None and fm.check_mag is None and fm.target_minus_check is None
        for fm in report.frame_results
    )
    assert report.check_points == ()
    assert report.target_minus_check_points == ()


# --- persistence: the check-star choice round-trips through the store ------------------------------


def test_check_star_pick_store_round_trip_then_measure_project(project):
    session = start_session(project, clock=frozen(T0))
    for i in range(2):
        store_frame(
            project,
            session,
            KIND_SUB,
            fits_bytes(frame_data(F0, seed=600 + i, dim=0.8 if i else 1.0), date_obs=date_obs_str(i)),
        )
    store_picks(project, with_check=True)

    loaded = load_star_picks(project)
    # load order: target first, then the check star, then the comps
    assert [p.role for p in loaded] == ["target", "check", "comp", "comp", "comp"]
    assert loaded[1].label == "Check K" and loaded[1].role == ROLE_CHECK
    # the project.json mirror: check is its own key, NOT among the comps
    mirror = project.config["star_picks"]
    assert mirror["check"]["role"] == "check" and mirror["check"]["label"] == "Check K"
    assert mirror["check"]["x"] == pytest.approx(48.0) and mirror["check"]["y"] == pytest.approx(48.0)
    assert [c["role"] for c in mirror["comps"]] == ["comp", "comp", "comp"]

    # the loaded picks (store records) measure with the check star
    report = measure_project(project, loaded)
    assert isinstance(report, MeasureReport)
    assert len(report.frame_results) == 2 and report.skipped == ()
    for fm in report.frame_results:
        assert fm.check is not None and fm.check.role == ROLE_CHECK
        assert fm.check_mag is not None and fm.check_mag_err is not None
        assert fm.target_minus_check is not None and fm.target_minus_check_err is not None
        assert all(s.role == "comp" for s in fm.comps)  # ensemble excludes the check


# --- package boundary: no gi in a subprocess + source scan (no network anywhere) -------------------


def test_check_star_role_imports_no_gi_no_ui_no_link_in_subprocess():
    code = (
        "import sys;"
        "import seestar_controller.photometry as p;"
        "import seestar_controller.photometry.measure;"
        "import seestar_controller.photometry.run;"
        "from seestar_controller.store.stars import ROLE_CHECK, ROLES;"
        "assert p.ROLE_CHECK == 'check' == ROLE_CHECK;"
        "assert 'check' in ROLES and 'check' in p.ROLES;"
        "spec = p.StarPickSpec(role='check', x=48.0, y=48.0, label='Check K');"
        "assert spec.role == 'check' and 'check' in spec.describe();"
        "assert 'gi' not in sys.modules, 'GTK imported';"
        "assert 'seestar_controller.ui' not in sys.modules, 'ui imported eagerly';"
        "assert 'seestar_controller.link' not in sys.modules, 'link imported eagerly';"
        "assert 'seestar_controller.enhance' not in sys.modules, 'enhance imported eagerly';"
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


def test_check_star_sources_have_no_gi_import():
    paths = [
        PHOTOMETRY_PACKAGE_DIR / name
        for name in ("__init__.py", "picking.py", "measure.py", "run.py")
    ]
    paths.append(STORE_PACKAGE_DIR / "stars.py")
    paths.append(STORE_PACKAGE_DIR / "schema.py")
    for path in paths:
        source = path.read_text(encoding="utf-8")
        assert not re.search(r"^\s*(import gi|from gi)\b", source, re.MULTILINE), (
            f"pure module {path} imports gi"
        )
