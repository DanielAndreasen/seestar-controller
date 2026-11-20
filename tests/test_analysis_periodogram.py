"""Pure-layer tests for period-analysis ticket 02 — the Lomb-Scargle core
(:mod:`seestar_controller.analysis.periodogram`).

Headless: no ``gi`` anywhere (the sandbox has no GTK, see
docs/wiki/assumptions.md). The engine is exercised with synthetic data of
KNOWN period (P = 0.35 d, amplitude 0.2 mag, noise sigma 0.01 mag) over an
uneven, GAPPED ~20 d baseline (nightly blocks with jittered start times, two
nights skipped entirely, one intra-night gap) — Lomb-Scargle exists exactly
for this sampling. The store-backed path is exercised against a tmp project
built with the same helpers as tests/test_analysis_lightcurve.py (imported
from there: ``start_session`` / ``store_frame`` / ``add_measurement`` /
``close_session`` wrappers).

Covered: the frequency grid (:func:`frequency_grid` spans from ~2 cycles over
the baseline up to the Nyquist-like cadence limit, uniform oversampled
spacing, custom parameters, duplicate times, and every degenerate input ->
:class:`PeriodogramError`), peak extraction (:func:`select_peaks` local
maxima, power-descending order, dedup within ``min_separation`` keeping the
highest, ``top_n``, validation), the engine (:func:`compute_periodogram` —
sinusoid recovery within 1% incl. big gaps, with/without errors, mixed None
errors, psd normalization, pure noise -> no dominant peak by a simple
criterion (top power < 0.1 AND Baluev FAP > 0.01), excluded points filtered by
default / included on request, all error paths, frozen result), the store
wrapper (:func:`project_periodogram` honoring the store lightcurve default),
and the package boundary (subprocess + source-scan).
"""

import re
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

# tmp-project measurement helpers reused from the ticket-01 test module
from test_analysis_lightcurve import (
    JD0,
    T0,
    date_obs_str,
    fits_bytes,
    frozen,
)

from seestar_controller import analysis
from seestar_controller.analysis import periodogram as periodogram_module
from seestar_controller.analysis.lightcurve import AnalysisError, LightcurvePoint
from seestar_controller.analysis.periodogram import (
    ALLOWED_NORMALIZATIONS,
    DEFAULT_MIN_CYCLES,
    DEFAULT_NORMALIZATION,
    DEFAULT_NYQUIST_FACTOR,
    DEFAULT_OVERSAMPLE,
    DEFAULT_TOP_N,
    MAX_GRID_POINTS,
    Peak,
    PeriodogramError,
    PeriodogramResult,
    compute_periodogram,
    frequency_grid,
    project_periodogram,
    select_peaks,
)
from seestar_controller.store import (
    KIND_SUB,
    add_measurement,
    close_session,
    create_project,
    lightcurve,
    start_session,
    store_frame,
)

#: Directory of the analysis package (source-scan audit).
ANALYSIS_PACKAGE_DIR = Path(analysis.__file__).resolve().parent

#: The known period of the synthetic sinusoid (days).
P_TRUE = 0.35

#: Amplitude (mag) and noise sigma (mag) of the synthetic sinusoid.
AMP = 0.2
SIGMA = 0.01


# --------------------------------------------------------------------------------------
# Synthetic sampling + points helpers
# --------------------------------------------------------------------------------------


def gapped_sampling(
    seed: int = 42,
    nights: int = 20,
    per_night: int = 40,
    cadence_s: float = 60.0,
    skip_nights=(5, 12),
    night_jitter: float = 0.3,
    intra_gap=(8, 10, 20),
) -> np.ndarray:
    """Uneven, gapped sampling over a ~20 d baseline: nightly blocks of
    ``per_night`` points at ``cadence_s`` with per-point jitter, jittered night
    start times, the nights in ``skip_nights`` missing entirely, and an
    intra-night gap (``intra_gap = (night, lo, hi)`` drops points lo..hi-1 of
    that night). Sorted ascending."""
    rng = np.random.default_rng(seed)
    ts: list[float] = []
    for night in range(nights):
        if night in skip_nights:
            continue
        base = night * 1.0 + rng.uniform(0.0, night_jitter)
        for i in range(per_night):
            if intra_gap and night == intra_gap[0] and intra_gap[1] <= i < intra_gap[2]:
                continue
            ts.append(base + i * cadence_s / 86400.0 + rng.normal(0.0, 5.0 / 86400.0))
    return np.array(sorted(ts))


def sinusoid_mags(t: np.ndarray, seed: int = 7, phase: float = 0.7) -> np.ndarray:
    """The synthetic sinusoid (P_TRUE, AMP, SIGMA) sampled at ``t``."""
    rng = np.random.default_rng(seed)
    return 12.0 + AMP * np.sin(2.0 * np.pi * t / P_TRUE + phase) + rng.normal(0.0, SIGMA, len(t))


def noise_mags(t: np.ndarray, seed: int = 99) -> np.ndarray:
    """Pure noise (no sinusoid) sampled at ``t``."""
    return 12.0 + np.random.default_rng(seed).normal(0.0, SIGMA, len(t))


def make_points(t, mags, mag_errs=None, excluded=None) -> tuple[LightcurvePoint, ...]:
    """Duck-typed points (LightcurvePoint) from parallel arrays."""
    t = list(t)
    mags = list(mags)
    n = len(t)
    mag_errs = list(mag_errs) if mag_errs is not None else [None] * n
    excluded = list(excluded) if excluded is not None else [False] * n
    return tuple(
        LightcurvePoint(i, float(jd), "utc", float(m), e, "s", x)
        for i, (jd, m, e, x) in enumerate(zip(t, mags, mag_errs, excluded))
    )


def dense_times(n: int = 60, seed: int = 5) -> np.ndarray:
    """A simple dense uneven sampling over 20 d (valid grid for error tests)."""
    return np.sort(np.random.default_rng(seed).uniform(0.0, 20.0, n))


@pytest.fixture()
def root(tmp_path):
    return tmp_path / "projects"


@pytest.fixture()
def project(root):
    p = create_project("M 42 period", root=root)
    yield p
    p.close()


# --------------------------------------------------------------------------------------
# Frequency grid
# --------------------------------------------------------------------------------------


def test_frequency_grid_spans_two_cycles_over_baseline_up_to_the_cadence_limit():
    t = gapped_sampling()
    grid = frequency_grid(t)
    baseline = float(t.max() - t.min())
    dt = np.diff(np.sort(t))
    median_dt = float(np.median(dt[dt > 0]))
    # f_min = 2 cycles over the baseline (<= 2/baseline with the default min_cycles)
    assert grid[0] == pytest.approx(DEFAULT_MIN_CYCLES / baseline)
    assert grid[0] <= 2.0 / baseline + 1e-12
    # f_max reaches the Nyquist-like cadence limit 0.5 / median(dt)
    assert grid[-1] >= DEFAULT_NYQUIST_FACTOR * 0.5 / median_dt
    # uniform spacing df = 1 / (oversample * baseline) — 5x the 1/baseline resolution
    df = 1.0 / (DEFAULT_OVERSAMPLE * baseline)
    assert np.allclose(np.diff(grid), df)
    assert grid.size == int(np.ceil((grid[-1] - grid[0]) / df)) + 1
    assert np.all(np.diff(grid) > 0)  # strictly increasing
    assert grid.dtype == np.float64


def test_frequency_grid_custom_parameters():
    t = gapped_sampling()
    baseline = float(t.max() - t.min())
    dt = np.diff(np.sort(t))
    median_dt = float(np.median(dt[dt > 0]))
    grid = frequency_grid(t, min_cycles=4.0, oversample=10.0, nyquist_factor=2.0)
    assert grid[0] == pytest.approx(4.0 / baseline)
    assert grid[-1] >= 2.0 * 0.5 / median_dt
    assert np.allclose(np.diff(grid), 1.0 / (10.0 * baseline))


def test_frequency_grid_ignores_duplicate_times_for_the_cadence():
    t = np.array([0.0, 0.0, 0.1, 0.2, 0.3, 10.0])  # duplicates + a big gap
    grid = frequency_grid(t)
    dt = np.diff(np.sort(t))
    median_dt = float(np.median(dt[dt > 0]))  # median of the POSITIVE gaps = 0.1
    assert grid[0] == pytest.approx(2.0 / 10.0)
    assert grid[-1] >= 0.5 / median_dt


def test_frequency_grid_rejects_degenerate_input():
    with pytest.raises(PeriodogramError):
        frequency_grid([])  # empty
    with pytest.raises(PeriodogramError):
        frequency_grid([2460000.5])  # single point
    with pytest.raises(PeriodogramError):
        frequency_grid([1.0, 1.0, 1.0])  # constant times -> zero baseline
    with pytest.raises(PeriodogramError):
        frequency_grid([1.0, float("nan"), 2.0])  # non-finite
    with pytest.raises(PeriodogramError):
        frequency_grid([[1.0, 2.0], [3.0, 4.0]])  # 2-D  # ty: ignore[invalid-argument-type]
    with pytest.raises(PeriodogramError):
        frequency_grid("abc")  # non-numeric  # ty: ignore[invalid-argument-type]
    with pytest.raises(PeriodogramError):
        frequency_grid([1.0, 2.0])  # two points: f_max <= f_min -> empty grid
    with pytest.raises(PeriodogramError):
        frequency_grid([0.0, 1.0, 2.0], min_cycles=0.0)
    with pytest.raises(PeriodogramError):
        frequency_grid([0.0, 1.0, 2.0], min_cycles=float("inf"))
    with pytest.raises(PeriodogramError):
        frequency_grid([0.0, 1.0, 2.0], oversample=0.5)  # < 1
    with pytest.raises(PeriodogramError):
        frequency_grid([0.0, 1.0, 2.0], nyquist_factor=0.0)
    with pytest.raises(PeriodogramError):
        frequency_grid([0.0, 1.0, 2.0], nyquist_factor=-1.0)
    with pytest.raises(PeriodogramError):
        frequency_grid(np.arange(100.0), max_grid_points=10)  # grid too large
    with pytest.raises(PeriodogramError):
        frequency_grid([0.0, 1.0, 2.0], min_cycles=True)  # bool rejected


# --------------------------------------------------------------------------------------
# Peak extraction
# --------------------------------------------------------------------------------------


def test_select_peaks_returns_local_maxima_sorted_by_power_descending():
    f = np.arange(1.0, 11.0)  # 1..10
    p = np.array([0.1, 0.5, 0.2, 0.9, 0.3, 0.3, 0.8, 0.1, 0.4, 0.2])
    peaks = select_peaks(f, p, top_n=10, min_separation=0.0)
    # local maxima at f = 2 (0.5), 4 (0.9), 7 (0.8), 9 (0.4)
    assert [pk.frequency for pk in peaks] == [4.0, 7.0, 2.0, 9.0]
    assert [pk.power for pk in peaks] == [0.9, 0.8, 0.5, 0.4]
    for pk in peaks:
        assert isinstance(pk, Peak)
        assert pk.period == pytest.approx(1.0 / pk.frequency)


def test_select_peaks_dedups_within_min_separation_keeping_the_highest():
    f = np.array([1.0, 1.05, 1.10, 1.15, 2.0, 3.0])
    p = np.array([0.1, 0.9, 0.5, 0.8, 0.2, 0.7])
    # local maxima at 1.05 (0.9), 1.15 (0.8), 3.0 (0.7)
    peaks = select_peaks(f, p, top_n=10, min_separation=0.2)
    # 1.05 and 1.15 are closer than 0.2 -> merged, keeping 1.05 (higher power)
    assert [pk.frequency for pk in peaks] == [1.05, 3.0]
    assert [pk.power for pk in peaks] == [0.9, 0.7]
    # a smaller separation keeps both
    peaks2 = select_peaks(f, p, top_n=10, min_separation=0.05)
    assert [pk.frequency for pk in peaks2] == [1.05, 1.15, 3.0]


def test_select_peaks_top_n_and_validation():
    f = np.arange(1.0, 20.0)
    p = np.zeros(19)
    p[3], p[10], p[15] = 0.5, 0.9, 0.7
    peaks = select_peaks(f, p, top_n=2, min_separation=0.0)
    assert len(peaks) == 2
    assert peaks[0].frequency == 11.0 and peaks[0].power == 0.9
    assert select_peaks([], [], top_n=5, min_separation=1.0) == ()
    with pytest.raises(PeriodogramError):
        select_peaks([1.0, 2.0], [0.1], top_n=5, min_separation=1.0)  # length mismatch
    with pytest.raises(PeriodogramError):
        select_peaks([2.0, 1.0], [0.1, 0.2], top_n=5, min_separation=1.0)  # not increasing
    with pytest.raises(PeriodogramError):
        select_peaks([1.0, 2.0], [0.1, 0.2], top_n=0, min_separation=1.0)  # top_n < 1
    with pytest.raises(PeriodogramError):
        select_peaks([1.0, 2.0], [0.1, 0.2], top_n=5, min_separation=-1.0)
    with pytest.raises(PeriodogramError):
        select_peaks([1.0, float("nan")], [0.1, 0.2], top_n=5, min_separation=1.0)


def test_peak_validation():
    with pytest.raises(PeriodogramError):
        Peak(period=0.0, frequency=1.0, power=0.5)
    with pytest.raises(PeriodogramError):
        Peak(period=1.0, frequency=0.0, power=0.5)
    with pytest.raises(PeriodogramError):
        Peak(period=1.0, frequency=1.0, power=float("nan"))
    with pytest.raises(PeriodogramError):
        Peak(period=1.0, frequency=1.0, power=True)
    pk = Peak(period=0.35, frequency=1.0 / 0.35, power=0.9)
    with pytest.raises(Exception):  # noqa: B017
        pk.power = 0.1  # frozen  # ty: ignore[invalid-assignment]
    assert issubclass(PeriodogramError, AnalysisError)
    assert issubclass(PeriodogramError, ValueError)


# --------------------------------------------------------------------------------------
# The engine: sinusoid recovery (uneven + gapped sampling)
# --------------------------------------------------------------------------------------


def test_sinusoid_period_recovered_within_1_percent_over_gapped_sampling():
    t = gapped_sampling()
    mag = sinusoid_mags(t)
    err = np.full(len(t), SIGMA)
    result = compute_periodogram(make_points(t, mag, err))
    assert isinstance(result, PeriodogramResult)
    assert result.n_points == len(t)
    top = result.peaks[0]
    # the top peak is the true period, within 1% (measured ~0.1%)
    assert abs(top.period - P_TRUE) / P_TRUE < 0.01
    assert top.power > 0.9  # standard normalization: ~1 for a clean sinusoid
    assert result.false_alarm_probability is not None
    assert result.false_alarm_probability < 1e-6  # hugely significant
    # peaks sorted by power descending, period == 1/frequency, top_n default
    powers = [pk.power for pk in result.peaks]
    assert powers == sorted(powers, reverse=True)
    assert len(result.peaks) == DEFAULT_TOP_N
    for pk in result.peaks:
        assert pk.period == pytest.approx(1.0 / pk.frequency)
    # grid info is consistent
    assert result.baseline == pytest.approx(float(t.max() - t.min()))
    assert result.f_min == pytest.approx(DEFAULT_MIN_CYCLES / result.baseline)
    assert result.f_max == pytest.approx(DEFAULT_NYQUIST_FACTOR * 0.5 / result.median_dt)
    assert result.df == pytest.approx(1.0 / (DEFAULT_OVERSAMPLE * result.baseline))
    assert result.n_frequencies == len(result.frequencies) == len(result.powers)
    assert result.frequencies[0] == pytest.approx(result.f_min)
    assert result.frequencies[-1] >= result.f_max
    assert result.normalization == DEFAULT_NORMALIZATION == "standard"
    # standard normalization: powers in [0, 1]
    assert np.all(result.powers >= 0.0) and np.all(result.powers <= 1.0)
    # the full spectrum is exposed (the widget will plot it)
    assert result.powers[int(np.argmax(result.powers))] == pytest.approx(top.power)


def test_sinusoid_recovered_without_mag_errors_and_with_mixed_none_errors():
    t = gapped_sampling()
    mag = sinusoid_mags(t)
    # all mag_err None -> unweighted periodogram
    r_none = compute_periodogram(make_points(t, mag))
    assert abs(r_none.peaks[0].period - P_TRUE) / P_TRUE < 0.01
    # some mag_err None -> filled with the median of the known errors
    errs = [SIGMA if i % 7 else None for i in range(len(t))]
    r_mixed = compute_periodogram(make_points(t, mag, errs))
    assert abs(r_mixed.peaks[0].period - P_TRUE) / P_TRUE < 0.01
    assert r_mixed.n_points == len(t)


def test_sinusoid_recovered_despite_a_large_gap_in_the_sampling():
    # five consecutive nights missing entirely (a ~5 d hole in the middle)
    t = gapped_sampling(skip_nights=(5, 6, 7, 8, 9))
    mag = sinusoid_mags(t, seed=11)
    result = compute_periodogram(make_points(t, mag, np.full(len(t), SIGMA)))
    assert len(t) < 700  # really fewer points
    assert abs(result.peaks[0].period - P_TRUE) / P_TRUE < 0.01
    assert result.peaks[0].power > 0.9


def test_psd_normalization_is_accepted_and_recovers_the_period():
    t = gapped_sampling()
    mag = sinusoid_mags(t)
    result = compute_periodogram(
        make_points(t, mag, np.full(len(t), SIGMA)), normalization="psd"
    )
    assert result.normalization == "psd"
    assert ALLOWED_NORMALIZATIONS == ("standard", "psd")
    assert abs(result.peaks[0].period - P_TRUE) / P_TRUE < 0.01
    assert np.all(result.powers >= 0.0)  # psd powers are unbounded but non-negative


def test_top_n_is_configurable():
    t = gapped_sampling()
    mag = sinusoid_mags(t)
    points = make_points(t, mag, np.full(len(t), SIGMA))
    r3 = compute_periodogram(points, top_n=3)
    assert len(r3.peaks) == 3
    assert r3.peaks[0].period == pytest.approx(compute_periodogram(points).peaks[0].period)


# --------------------------------------------------------------------------------------
# The engine: pure noise -> no dominant peak
# --------------------------------------------------------------------------------------


def test_pure_noise_has_no_dominant_peak():
    """Simple significance criterion (documented in the module): the top peak
    of pure noise is NOT dominant — its standard-normalized power stays far
    below a coherent signal's (< 0.1 here; measured ~0.03) AND its Baluev
    false-alarm probability is above the 1% level (> 0.01; measured ~0.1-1)."""
    t = gapped_sampling(seed=1)
    mag = noise_mags(t, seed=99)
    result = compute_periodogram(make_points(t, mag, np.full(len(t), SIGMA)))
    assert len(result.peaks) == DEFAULT_TOP_N  # peaks exist...
    top = result.peaks[0]
    assert top.power < 0.1  # ...but none is dominant
    assert result.false_alarm_probability is not None
    assert result.false_alarm_probability > 0.01  # not significant at the 1% level
    # and the top power is not wildly above the spectrum's median either
    assert top.power < 50.0 * float(np.median(result.powers))


# --------------------------------------------------------------------------------------
# The engine: excluded points honored
# --------------------------------------------------------------------------------------


def test_excluded_points_are_filtered_by_default_and_included_on_request():
    t = gapped_sampling()
    mag = sinusoid_mags(t)
    # six wild outliers, flagged excluded
    out_t = np.linspace(0.5, 18.0, 6)
    all_t = np.concatenate([t, out_t])
    all_mag = np.concatenate([mag, np.full(6, 17.0)])
    all_err = np.full(len(all_t), SIGMA)
    excluded = [False] * len(t) + [True] * 6
    points = make_points(all_t, all_mag, all_err, excluded)

    r_default = compute_periodogram(points)  # include_excluded=False (default)
    assert r_default.n_points == len(t)
    assert abs(r_default.peaks[0].period - P_TRUE) / P_TRUE < 0.01  # outliers gone

    r_all = compute_periodogram(points, include_excluded=True)
    assert r_all.n_points == len(t) + 6


def test_all_points_excluded_is_a_clean_error():
    t = [0.0, 0.01, 0.02, 5.0]  # valid grid even with only 4 points
    points = make_points(t, [12.0, 12.1, 11.9, 12.05], excluded=[True] * 4)
    with pytest.raises(PeriodogramError, match="excluded"):
        compute_periodogram(points)
    # ...but fine when explicitly included
    r = compute_periodogram(points, include_excluded=True)
    assert r.n_points == 4


# --------------------------------------------------------------------------------------
# The engine: error paths + frozen result
# --------------------------------------------------------------------------------------


def test_compute_periodogram_rejects_degenerate_input():
    with pytest.raises(PeriodogramError):
        compute_periodogram([])  # no points
    one = (LightcurvePoint(1, JD0, "utc", 12.0, None, "s", False),)
    with pytest.raises(PeriodogramError):
        compute_periodogram(one)  # single point
    with pytest.raises(PeriodogramError):
        compute_periodogram(42)  # not iterable  # ty: ignore[invalid-argument-type]
    t = dense_times()
    with pytest.raises(PeriodogramError):
        compute_periodogram(make_points(t, np.full(len(t), 12.0)))  # constant mags
    with pytest.raises(PeriodogramError):
        compute_periodogram(make_points(t, np.where(np.arange(len(t)) == 3, np.nan, 12.0 + 0.01 * np.arange(len(t)))))
    with pytest.raises(PeriodogramError):
        compute_periodogram(make_points(t, 12.0 + 0.01 * np.arange(len(t)), [0.01] * (len(t) - 1) + [-0.02]))
    with pytest.raises(PeriodogramError):
        compute_periodogram(make_points(t, 12.0 + 0.01 * np.arange(len(t))), normalization="bogus")
    with pytest.raises(PeriodogramError):
        compute_periodogram(make_points(t, 12.0 + 0.01 * np.arange(len(t))), top_n=0)
    with pytest.raises(PeriodogramError):
        compute_periodogram(make_points(t, 12.0 + 0.01 * np.arange(len(t))), include_excluded="yes")  # ty: ignore[invalid-argument-type]
    with pytest.raises(PeriodogramError):
        compute_periodogram(make_points([0.0, 1.0, 2.0], [12.0, 12.1, 11.9]))  # f_max <= f_min
    with pytest.raises(PeriodogramError):
        compute_periodogram(make_points(t, 12.0 + 0.01 * np.arange(len(t))), max_grid_points=10)
    # points without the duck-typed attributes
    with pytest.raises(PeriodogramError):
        compute_periodogram([object(), object(), object()])


def test_result_is_frozen_and_carries_the_grid():
    t = gapped_sampling()
    result = compute_periodogram(make_points(t, sinusoid_mags(t), np.full(len(t), SIGMA)))
    with pytest.raises(Exception):  # noqa: B017
        result.peaks = ()  # frozen  # ty: ignore[invalid-assignment]
    with pytest.raises(Exception):  # noqa: B017
        result.peaks[0].power = 0.0  # peaks frozen  # ty: ignore[invalid-assignment]
    assert isinstance(result.frequencies, np.ndarray)
    assert isinstance(result.powers, np.ndarray)
    assert result.frequencies.dtype == np.float64 and result.powers.dtype == np.float64
    assert isinstance(result.false_alarm_probability, float)
    # PeriodogramResult validation
    with pytest.raises(PeriodogramError):
        PeriodogramResult(
            n_points=1, baseline=1.0, median_dt=0.1, f_min=0.1, f_max=1.0, df=0.1,
            n_frequencies=10, normalization="standard", peaks=(), frequencies=np.zeros(10),
            powers=np.zeros(10), false_alarm_probability=None,
        )
    with pytest.raises(PeriodogramError):
        PeriodogramResult(
            n_points=10, baseline=1.0, median_dt=0.1, f_min=0.1, f_max=1.0, df=0.1,
            n_frequencies=10, normalization="bogus", peaks=(), frequencies=np.zeros(10),
            powers=np.zeros(10), false_alarm_probability=None,
        )
    with pytest.raises(PeriodogramError):
        PeriodogramResult(
            n_points=10, baseline=1.0, median_dt=0.1, f_min=0.1, f_max=1.0, df=0.1,
            n_frequencies=10, normalization="standard", peaks=(), frequencies=np.zeros(9),
            powers=np.zeros(10), false_alarm_probability=None,
        )


# --------------------------------------------------------------------------------------
# The store-backed wrapper (tmp project)
# --------------------------------------------------------------------------------------


def _project_with_sinusoid(project) -> int:
    """One session with one sub-frame + measurement per synthetic point
    (jd = JD0 + t), plus ONE excluded wild outlier. Returns the point count
    (without the outlier)."""
    t = gapped_sampling()
    mag = sinusoid_mags(t)
    session_id = start_session(project, clock=frozen(T0))
    for i, (jd, m) in enumerate(zip(t, mag)):
        frame = store_frame(project, session_id, KIND_SUB, fits_bytes(date_obs_str(10.0 * i)))
        add_measurement(project, frame.id, float(m), jd=JD0 + float(jd), mag_err=SIGMA)
    frame = store_frame(project, session_id, KIND_SUB, fits_bytes(date_obs_str(999.0)))
    add_measurement(project, frame.id, 17.0, jd=JD0 + 0.5, mag_err=SIGMA, excluded=True)
    close_session(project, session_id)
    return len(t)


def test_project_periodogram_honors_the_store_lightcurve_default(project):
    n = _project_with_sinusoid(project)
    # default: the store lightcurve filters excluded points
    r_default = project_periodogram(project)
    assert r_default.n_points == n == len(lightcurve(project))
    assert abs(r_default.peaks[0].period - P_TRUE) / P_TRUE < 0.01
    # include_excluded=True: the outlier is in
    r_all = project_periodogram(project, include_excluded=True)
    assert r_all.n_points == n + 1 == len(lightcurve(project, include_excluded=True))
    # compute_periodogram accepts the store Measurements directly (duck-typed)
    r_direct = compute_periodogram(lightcurve(project))
    assert r_direct.n_points == n
    assert r_direct.peaks[0].period == pytest.approx(r_default.peaks[0].period)
    # kwargs are forwarded
    r_top3 = project_periodogram(project, top_n=3)
    assert len(r_top3.peaks) == 3
    with pytest.raises(PeriodogramError):
        project_periodogram(project, include_excluded="yes")  # ty: ignore[invalid-argument-type]


def test_project_periodogram_of_an_empty_project_is_a_clean_error(project):
    with pytest.raises(PeriodogramError):
        project_periodogram(project)


# --------------------------------------------------------------------------------------
# Package boundary (subprocess + source scan)
# --------------------------------------------------------------------------------------


def test_periodogram_module_imports_no_gi_no_link_no_ui_no_photometry_no_enhance():
    code = (
        "import sys;"
        "import seestar_controller.analysis;"
        "import seestar_controller.analysis.periodogram;"
        "import seestar_controller.analysis.fold;"
        "assert 'gi' not in sys.modules, 'GTK imported';"
        "assert 'seestar_controller.link' not in sys.modules, 'link imported eagerly';"
        "assert 'seestar_controller.ui' not in sys.modules, 'ui imported eagerly';"
        "assert 'seestar_controller.photometry' not in sys.modules, 'photometry imported eagerly';"
        "assert 'seestar_controller.enhance' not in sys.modules, 'enhance imported eagerly';"
        "assert 'seestar_controller.analysis.lightcurve_view' not in sys.modules, "
        "'lightcurve_view imported eagerly';"
        "assert 'seestar_controller.analysis.period_view' not in sys.modules, "
        "'period_view imported eagerly';"
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


def test_periodogram_source_has_no_forbidden_imports_and_the_package_lists_it():
    source = (ANALYSIS_PACKAGE_DIR / "periodogram.py").read_text(encoding="utf-8")
    forbidden = re.compile(
        r"^\s*(import|from)\s+(gi\b|seestar_controller\.(gi|ui|link|photometry|enhance)\b)",
        re.MULTILINE,
    )
    assert not forbidden.search(source), "periodogram.py imports a forbidden module"
    assert not re.search(r"^\s*(import gi|from gi)\b", source, re.MULTILINE)
    modules = {path.stem for path in ANALYSIS_PACKAGE_DIR.glob("*.py")}
    assert modules == {
        "__init__", "lightcurve", "periodogram", "fold", "export",
        "lightcurve_view", "period_view",
    }
    assert periodogram_module.PeriodogramError is PeriodogramError


def test_public_api_is_exported_from_package():
    for name in (
        "ALLOWED_NORMALIZATIONS",
        "DEFAULT_MIN_CYCLES",
        "DEFAULT_NORMALIZATION",
        "DEFAULT_NYQUIST_FACTOR",
        "DEFAULT_OVERSAMPLE",
        "DEFAULT_TOP_N",
        "MAX_GRID_POINTS",
        "Peak",
        "PeriodogramError",
        "PeriodogramResult",
        "compute_periodogram",
        "frequency_grid",
        "project_periodogram",
        "select_peaks",
    ):
        assert name in analysis.__all__, name
        assert hasattr(analysis, name), name
    assert analysis.PeriodogramError is PeriodogramError
    assert analysis.compute_periodogram is compute_periodogram
    assert DEFAULT_MIN_CYCLES == 2.0
    assert DEFAULT_OVERSAMPLE == 5.0
    assert DEFAULT_NYQUIST_FACTOR == 1.0
    assert DEFAULT_TOP_N == 10
    assert DEFAULT_NORMALIZATION == "standard"
    assert MAX_GRID_POINTS >= 1_000_000
