"""Pure-layer tests for transit-fit ticket 01 — the analytic transit model +
least-squares fit core (:mod:`seestar_controller.transit`).

Headless: no ``gi`` anywhere (the sandbox has no GTK, see
docs/wiki/assumptions.md). The model math is exercised on known phases (the
trapezoid shape, the circular wrap at phase 0/1, the ramp midpoint, the
validation); the fit is exercised on a synthetic folded lightcurve with an
INJECTED transit of known parameters (P = 2.5 d, t0 = 0.5, depth = 0.02 mag,
duration = 0.08 phase units, Gaussian noise sigma = 0.003 mag) — the fit must
recover t0/depth/duration within the covariance-matrix uncertainties with
chi2/dof ~ 1 and flat residuals — fixed vs free period, the input validation,
the store-backed wrapper against a tmp project (period from the project
config), the persistence round-trip (``project.json`` config key + JSON
export), and the package boundary (subprocess + source-scan; no network).

Covered: :func:`transit_model` (shape + wrap + every invalid input),
:class:`TransitParams` / :class:`TransitErrors` (validation, t0 wrap,
dict round-trip), :func:`fit_transit` (recovery within uncertainties, chi2,
flat residuals, fixed vs free period, uniform weights, every invalid input),
:class:`FitResult` (dict round-trip + equality), :func:`fit_project_transit`
(tmp project, period from config / argument / missing, empty project, unknown
mag_errs), :func:`save_fit` / :func:`load_fit` / :func:`export_fit_json`
(round-trip incl. a fresh ``open_project``, corrupt value, atomic export), and
the package boundary (subprocess + source-scan).
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

# tmp-project helpers reused from the period-analysis test modules
from test_analysis_lightcurve import (
    JD0,
    T0,
    date_obs_str,
    fits_bytes,
    frozen,
)

from seestar_controller import transit
from seestar_controller.store import (
    KIND_SUB,
    add_measurement,
    close_session,
    create_project,
    open_project,
    start_session,
    store_frame,
)
from seestar_controller.transit import (
    FIT_CONFIG_KEY,
    INGRESS_FRACTION,
    FitResult,
    TransitError,
    TransitErrors,
    TransitParams,
    export_fit_json,
    fit_project_transit,
    fit_transit,
    load_fit,
    save_fit,
    transit_model,
)
from seestar_controller.transit import fit as fit_module
from seestar_controller.transit import model as model_module
from seestar_controller.transit import persist as persist_module

#: Directory of the transit package (source-scan audit).
TRANSIT_PACKAGE_DIR = Path(transit.__file__).resolve().parent

# --- the synthetic injected transit -------------------------------------------------

P_TRUE = 2.5  # days
T0_TRUE = 0.5  # phase
DEPTH_TRUE = 0.02  # mag
DURATION_TRUE = 0.08  # phase units
SIGMA = 0.003  # mag
N_POINTS = 240


def synthetic_lightcurve(seed: int = 42, n: int = N_POINTS):
    """A synthetic folded lightcurve with an injected transit: returns
    ``(jd, phases, mags, errs)`` (jd relative to JD0, phases in [0, 1))."""
    rng = np.random.default_rng(seed)
    jd = JD0 + np.sort(rng.uniform(0.0, 5.0 * P_TRUE, n))
    phases = np.mod((jd - JD0) / P_TRUE, 1.0)
    mags = transit_model(phases, T0_TRUE, DEPTH_TRUE, DURATION_TRUE) + rng.normal(
        0.0, SIGMA, n
    )
    errs = np.full(n, SIGMA)
    return jd, phases, mags, errs


@pytest.fixture()
def root(tmp_path):
    return tmp_path / "projects"


@pytest.fixture()
def project(root):
    p = create_project("M 42 transit", root=root)
    yield p
    p.close()


def _project_with_transit(project, seed: int = 42, n: int = N_POINTS) -> int:
    """One session with one sub-frame + measurement per synthetic point
    (jd = the synthetic jd, mag_err = SIGMA). Returns the point count."""
    jd, _phases, mags, _errs = synthetic_lightcurve(seed=seed, n=n)
    session_id = start_session(project, clock=frozen(T0))
    for i, (jd_i, mag) in enumerate(zip(jd, mags)):
        frame = store_frame(project, session_id, KIND_SUB, fits_bytes(date_obs_str(10.0 * i)))
        add_measurement(project, frame.id, float(mag), jd=float(jd_i), mag_err=SIGMA)
    close_session(project, session_id)
    return len(jd)


# --- the model ------------------------------------------------------------------------


def test_transit_model_trapezoid_shape():
    duration = 0.08
    half = duration / 2.0
    ramp = duration * INGRESS_FRACTION
    # out of transit -> 0, full transit -> depth, ramp midpoint -> depth / 2
    assert transit_model(0.0, t0=0.5, depth=0.02, duration=duration).item() == 0.0
    assert transit_model(0.5, t0=0.5, depth=0.02, duration=duration).item() == pytest.approx(0.02)
    mid_ramp = 0.5 + half - ramp / 2.0
    assert transit_model(mid_ramp, t0=0.5, depth=0.02, duration=duration).item() == pytest.approx(
        0.01
    )
    # full-transit core edge and just outside
    assert transit_model(0.5 + half - ramp, t0=0.5, depth=0.02, duration=duration).item() == (
        pytest.approx(0.02)
    )
    assert transit_model(0.5 + half + 1e-9, t0=0.5, depth=0.02, duration=duration).item() == 0.0
    # vectorized over an array, baseline 0
    phases = np.linspace(0.0, 1.0, 501)
    model = transit_model(phases, t0=0.5, depth=0.02, duration=duration)
    assert isinstance(model, np.ndarray) and model.shape == phases.shape
    assert model.min() == 0.0 and model.max() == pytest.approx(0.02)
    # the model is periodic in t0 with period 1 (up to float rounding at the
    # trapezoid edges)
    np.testing.assert_allclose(
        transit_model(phases, t0=1.5, depth=0.02, duration=duration), model, atol=1e-12
    )


def test_transit_model_circular_wrap_at_phase_boundary():
    # a transit centered at t0 = 0 straddles the 0/1 boundary
    # (duration 0.08 -> half = 0.04, ramp = 0.02; full core for d <= 0.02)
    model = transit_model([0.0, 0.99, 0.97, 0.03, 0.5], t0=0.0, depth=0.03, duration=0.08)
    assert model[0].item() == pytest.approx(0.03)  # d = 0 -> full transit
    assert model[1].item() == pytest.approx(0.03)  # d = 0.01 -> still full core
    assert model[2].item() == pytest.approx(0.015)  # d = 0.03 -> ramp midpoint
    assert model[3].item() == pytest.approx(0.015)  # d = 0.03 -> ramp midpoint
    assert model[4].item() == 0.0  # d = 0.5 -> out of transit
    # scalar input -> 0-d array; phases outside [0, 1) are wrapped
    assert transit_model(1.5, t0=0.5, depth=0.02, duration=0.08).item() == pytest.approx(0.02)
    assert transit_model(-0.5, t0=0.5, depth=0.02, duration=0.08).item() == pytest.approx(0.02)


def test_transit_model_validation():
    good = {"t0": 0.5, "depth": 0.02, "duration": 0.08}
    for bad in ({"depth": -0.1}, {"depth": float("nan")}, {"depth": "x"}, {"depth": True}):
        with pytest.raises(TransitError):
            transit_model(0.5, **{**good, **bad})
    for bad in ({"duration": 0.0}, {"duration": 1.0}, {"duration": -0.5}, {"duration": float("inf")}):
        with pytest.raises(TransitError):
            transit_model(0.5, **{**good, **bad})
    for bad in ({"t0": float("nan")}, {"t0": None}):
        with pytest.raises(TransitError):
            transit_model(0.5, **{**good, **bad})
    with pytest.raises(TransitError):
        transit_model([0.0, float("nan")], **good)
    with pytest.raises(TransitError):
        transit_model("not-a-number", **good)
    assert issubclass(TransitError, ValueError)


# --- params / errors ------------------------------------------------------------------


def test_transit_params_validation_wrap_and_dict_roundtrip():
    params = TransitParams(t0=1.5, depth=0.02, duration=0.08, period=2.5)
    assert params.t0 == pytest.approx(0.5)  # wrapped into [0, 1)
    assert params.duration_days == pytest.approx(0.08 * 2.5)
    assert TransitParams.from_dict(params.to_dict()) == params
    assert TransitParams.from_dict({**params.to_dict(), "extra": 1}) == params  # unknown keys ok
    for bad in (
        {"t0": 0.5, "depth": -1.0, "duration": 0.08, "period": 2.5},
        {"t0": 0.5, "depth": 0.02, "duration": 0.0, "period": 2.5},
        {"t0": 0.5, "depth": 0.02, "duration": 1.0, "period": 2.5},
        {"t0": 0.5, "depth": 0.02, "duration": 0.08, "period": 0.0},
        {"t0": float("nan"), "depth": 0.02, "duration": 0.08, "period": 2.5},
    ):
        with pytest.raises(TransitError):
            TransitParams(**bad)
    with pytest.raises((TransitError, TypeError)):  # missing period
        TransitParams(t0=0.5, depth=0.02, duration=0.08)  # ty: ignore[missing-argument]
    with pytest.raises(TransitError):
        TransitParams.from_dict([1, 2, 3])

    errors = TransitErrors(t0=0.001, depth=0.001, duration=0.002, period=0.0)
    assert TransitErrors.from_dict(errors.to_dict()) == errors
    with pytest.raises(TransitError):
        TransitErrors(t0=-0.1, depth=0.0, duration=0.0, period=0.0)
    with pytest.raises(TransitError):
        TransitErrors(t0=0.0, depth=0.0, duration=0.0, period=float("nan"))


# --- fit_transit: recovery ------------------------------------------------------------


def test_fit_transit_recovers_injected_transit_within_uncertainties():
    _jd, phases, mags, errs = synthetic_lightcurve()
    result = fit_transit(phases, mags, errs, P_TRUE)
    assert isinstance(result, FitResult)
    assert result.success
    assert result.period_fixed
    assert result.params.period == P_TRUE
    assert result.errors.period == 0.0  # fixed period -> zero uncertainty
    assert result.n_points == N_POINTS and result.dof == N_POINTS - 3
    # recovered within 3 sigma of the injected values
    assert abs(result.params.t0 - T0_TRUE) < 3.0 * result.errors.t0
    assert abs(result.params.depth - DEPTH_TRUE) < 3.0 * result.errors.depth
    assert abs(result.params.duration - DURATION_TRUE) < 3.0 * result.errors.duration
    # chi2/dof ~ 1 for correctly weighted residuals, rms ~ the noise level
    assert 0.5 < result.chi2 / result.dof < 1.5
    assert result.rms == pytest.approx(SIGMA, rel=0.2)
    # residuals flat: small mean, and per-phase-bin means within 3 sigma / sqrt(bin)
    assert abs(float(np.mean(result.residuals))) < 3.0 * SIGMA / np.sqrt(N_POINTS)
    bins = np.array_split(np.argsort(phases), 10)
    for idx in bins:
        assert abs(float(np.mean(result.residuals[idx]))) < 3.0 * SIGMA / np.sqrt(len(idx))
    # model mags / residuals shapes and consistency
    assert result.model_mags.shape == phases.shape == result.residuals.shape
    np.testing.assert_allclose(result.residuals, mags - result.model_mags)
    np.testing.assert_allclose(
        result.model_mags, transit_model(phases, result.params.t0, result.params.depth, result.params.duration)
    )


def test_fit_transit_free_period_recovers_period_within_uncertainty():
    jd, phases, mags, errs = synthetic_lightcurve()
    result = fit_transit(phases, mags, errs, P_TRUE, jd=jd, jd0=JD0, fit_period=True)
    assert not result.period_fixed
    assert result.dof == N_POINTS - 4
    assert result.errors.period > 0.0
    assert abs(result.params.period - P_TRUE) < 3.0 * result.errors.period
    assert abs(result.params.t0 - T0_TRUE) < 3.0 * result.errors.t0
    assert abs(result.params.depth - DEPTH_TRUE) < 3.0 * result.errors.depth
    assert abs(result.params.duration - DURATION_TRUE) < 3.0 * result.errors.duration
    assert 0.5 < result.chi2 / result.dof < 1.5
    # the model mags are recomputed from jd with the FITTED period
    fitted_phases = np.mod((jd - JD0) / result.params.period, 1.0)
    np.testing.assert_allclose(
        result.model_mags,
        transit_model(fitted_phases, result.params.t0, result.params.depth, result.params.duration),
    )


def test_fit_transit_uniform_weights_and_explicit_guesses():
    _jd, phases, mags, _errs = synthetic_lightcurve()
    result = fit_transit(phases, mags, None, P_TRUE)  # errs=None -> uniform weights
    assert result.success
    assert abs(result.params.depth - DEPTH_TRUE) < 0.005
    guessed = fit_transit(
        phases,
        mags,
        np.full(N_POINTS, SIGMA),
        P_TRUE,
        t0_guess=0.48,
        depth_guess=0.015,
        duration_guess=0.06,
    )
    assert guessed.success
    assert abs(guessed.params.depth - DEPTH_TRUE) < 3.0 * guessed.errors.depth


def test_fit_transit_input_validation():
    _jd, phases, mags, errs = synthetic_lightcurve(n=20)
    with pytest.raises(TransitError):  # length mismatch
        fit_transit(phases[:-1], mags, errs, P_TRUE)
    with pytest.raises(TransitError):  # errs length mismatch
        fit_transit(phases, mags, errs[:-1], P_TRUE)
    with pytest.raises(TransitError):  # non-positive error
        fit_transit(phases, mags, np.where(np.arange(20) == 3, 0.0, SIGMA), P_TRUE)
    with pytest.raises(TransitError):  # NaN mag
        bad = mags.copy(); bad[0] = float("nan")
        fit_transit(phases, bad, errs, P_TRUE)
    with pytest.raises(TransitError):  # invalid period
        fit_transit(phases, mags, errs, -1.0)
    with pytest.raises(TransitError):  # too few points for 3 params
        fit_transit(phases[:3], mags[:3], errs[:3], P_TRUE)
    with pytest.raises(TransitError):  # too few points for 4 params
        fit_transit(phases[:4], mags[:4], errs[:4], P_TRUE, jd=np.arange(4.0), fit_period=True)
    with pytest.raises(TransitError):  # fit_period without jd
        fit_transit(phases, mags, errs, P_TRUE, fit_period=True)
    with pytest.raises(TransitError):  # 2-D input
        fit_transit(phases.reshape(4, 5), mags.reshape(4, 5), errs.reshape(4, 5), P_TRUE)
    with pytest.raises(TransitError):  # bad duration guess
        fit_transit(phases, mags, errs, P_TRUE, duration_guess=2.0)


def test_fit_result_dict_roundtrip_and_equality():
    _jd, phases, mags, errs = synthetic_lightcurve()
    result = fit_transit(phases, mags, errs, P_TRUE)
    clone = FitResult.from_dict(result.to_dict())
    assert clone == result
    assert clone != fit_transit(phases, mags, errs, P_TRUE + 0.1)
    assert clone != "not a fit result"
    payload = result.to_dict()
    assert json.dumps(payload)  # JSON-able
    assert set(payload) == {
        "params", "errors", "residuals", "chi2", "n_points", "dof",
        "rms", "model_mags", "period_fixed", "success", "message",
    }
    with pytest.raises(TransitError):
        FitResult.from_dict({"params": {}})
    with pytest.raises(TransitError):
        FitResult.from_dict([1, 2])


# --- fit_project_transit (store-backed wrapper) ---------------------------------------


def test_fit_project_transit_uses_period_from_project_config(project):
    jd, _phases, _mags, _errs = synthetic_lightcurve()
    _project_with_transit(project)
    project.config["period"] = P_TRUE
    project.save_config()
    result = fit_project_transit(project)
    assert result.period_fixed
    assert result.params.period == P_TRUE
    assert result.n_points == N_POINTS
    assert abs(result.params.depth - DEPTH_TRUE) < 3.0 * result.errors.depth
    assert abs(result.params.duration - DURATION_TRUE) < 3.0 * result.errors.duration
    # the project fold uses the first observation (min jd) as epoch, so the
    # recovered t0 is relative to THAT epoch, not to JD0
    epoch = float(np.min(jd))
    t0_expected = (T0_TRUE + (JD0 - epoch) / P_TRUE) % 1.0
    t0_gap = abs(((result.params.t0 - t0_expected + 0.5) % 1.0) - 0.5)
    assert t0_gap < 3.0 * result.errors.t0
    # explicit period argument overrides the config
    other = fit_project_transit(project, period=P_TRUE * 1.001)
    assert other.params.period == pytest.approx(P_TRUE * 1.001)
    # free period through the wrapper
    free = fit_project_transit(project, fit_period=True)
    assert not free.period_fixed
    assert abs(free.params.period - P_TRUE) < 3.0 * free.errors.period


def test_fit_project_transit_missing_period_and_empty_project(project, root):
    _project_with_transit(project)
    with pytest.raises(TransitError):  # no period anywhere
        fit_project_transit(project)
    project.config["period"] = "corrupt"
    with pytest.raises(TransitError):
        fit_project_transit(project)
    empty = create_project("empty", root=root)
    try:
        with pytest.raises(TransitError):  # no measurements
            fit_project_transit(empty, period=P_TRUE)
    finally:
        empty.close()


def test_fit_project_transit_unknown_mag_errs_get_median_fill(project):
    jd, _phases, mags, _errs = synthetic_lightcurve(n=60)
    session_id = start_session(project, clock=frozen(T0))
    for i, (jd_i, mag) in enumerate(zip(jd, mags)):
        frame = store_frame(project, session_id, KIND_SUB, fits_bytes(date_obs_str(10.0 * i)))
        # every 5th point has an unknown error
        add_measurement(
            project, frame.id, float(mag), jd=float(jd_i), mag_err=None if i % 5 == 0 else SIGMA
        )
    close_session(project, session_id)
    result = fit_project_transit(project, period=P_TRUE)
    assert result.n_points == 60
    assert result.success
    assert abs(result.params.depth - DEPTH_TRUE) < 3.0 * result.errors.depth


# --- persistence ----------------------------------------------------------------------


def test_save_and_load_fit_roundtrip_through_project_json(project, root):
    _project_with_transit(project)
    project.config["period"] = P_TRUE
    result = fit_project_transit(project)
    assert load_fit(project) is None  # nothing saved yet
    save_fit(project, result)
    # in-memory config + on-disk project.json both carry the fit
    assert FIT_CONFIG_KEY in project.config
    assert "fitted_at" in project.config[FIT_CONFIG_KEY]
    on_disk = json.loads(project.config_path.read_text(encoding="utf-8"))
    assert on_disk[FIT_CONFIG_KEY]["params"] == result.params.to_dict()
    # round-trip on the same project and on a FRESH open of the project dir
    assert load_fit(project) == result
    project.close()
    reopened = open_project(project.path)
    try:
        assert load_fit(reopened) == result
    finally:
        reopened.close()
    # corrupt persisted value -> TransitError, missing -> None
    project.config[FIT_CONFIG_KEY] = {"params": {"t0": "x"}}
    with pytest.raises(TransitError):
        load_fit(project)
    project.config[FIT_CONFIG_KEY] = None
    assert load_fit(project) is None
    with pytest.raises(TransitError):
        save_fit(project, "not a fit result")  # ty: ignore[invalid-argument-type]


def test_export_fit_json_atomic_roundtrip(project, tmp_path):
    _project_with_transit(project)
    result = fit_project_transit(project, period=P_TRUE)
    out = tmp_path / "exports" / "transit_fit.json"
    returned = export_fit_json(result, out)
    assert returned == out
    assert out.exists()
    assert not list(out.parent.glob("*.tmp"))  # atomic write leaves no temp file
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload == result.to_dict()
    assert FitResult.from_dict(payload) == result
    with pytest.raises(TransitError):
        export_fit_json(42, tmp_path / "nope.json")  # ty: ignore[invalid-argument-type]


# --- package boundary (subprocess + source scan) --------------------------------------


def test_transit_package_imports_no_gi_no_link_no_ui_no_photometry_no_enhance():
    code = (
        "import sys;"
        "import seestar_controller.transit;"
        "import seestar_controller.transit.model;"
        "import seestar_controller.transit.fit;"
        "import seestar_controller.transit.persist;"
        "assert 'gi' not in sys.modules, 'GTK imported';"
        "assert 'seestar_controller.link' not in sys.modules, 'link imported eagerly';"
        "assert 'seestar_controller.ui' not in sys.modules, 'ui imported eagerly';"
        "assert 'seestar_controller.photometry' not in sys.modules, 'photometry imported eagerly';"
        "assert 'seestar_controller.enhance' not in sys.modules, 'enhance imported eagerly';"
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


def test_transit_source_is_pure_and_network_free():
    modules = {path.stem for path in TRANSIT_PACKAGE_DIR.glob("*.py")}
    assert modules == {"__init__", "model", "fit", "persist"}
    forbidden = re.compile(
        r"^\s*(import|from)\s+"
        r"(gi\b|seestar_controller\.(gi|ui|link|photometry|enhance)\b"
        r"|socket\b|urllib\b|requests\b|http\b|http\.client\b)",
        re.MULTILINE,
    )
    for path in sorted(TRANSIT_PACKAGE_DIR.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        assert not forbidden.search(source), f"{path.name} imports a forbidden module"
        assert not re.search(r"^\s*(import gi|from gi)\b", source, re.MULTILINE), (
            f"{path.name} imports gi"
        )


def test_public_api_is_exported_from_package():
    for name in (
        "FIT_CONFIG_KEY",
        "INGRESS_FRACTION",
        "FitResult",
        "TransitErrors",
        "TransitError",
        "TransitParams",
        "export_fit_json",
        "fit_project_transit",
        "fit_transit",
        "load_fit",
        "save_fit",
        "transit_model",
    ):
        assert name in transit.__all__, name
        assert hasattr(transit, name), name
    assert transit.transit_model is model_module.transit_model
    assert transit.fit_transit is fit_module.fit_transit
    assert transit.save_fit is persist_module.save_fit
    assert FIT_CONFIG_KEY == "transit_fit"
    assert 0.0 < INGRESS_FRACTION <= 0.5
