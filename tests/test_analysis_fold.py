"""Pure-layer tests for period-analysis ticket 03 — period selection + phase
fold (:mod:`seestar_controller.analysis.fold`).

Headless: no ``gi`` anywhere (the sandbox has no GTK, see
docs/wiki/assumptions.md). The fold math is exercised on KNOWN jd/period
values (exact phases, the mod-1 wrap, the float-rounding edge clamp, the
default/custom jd0); the controller is exercised against a tmp project with
a synthetic sinusoid of known period (P = 0.35 d, imported from
tests/test_analysis_periodogram.py) and against a two-session project built
with the store helpers imported from tests/test_analysis_lightcurve.py.

Covered: :func:`validate_period` / :func:`phase_of` (exact values + every
invalid input), :func:`fold_points` (sorted by phase, jd0 default = min jd,
custom jd0, empty sets, store-Measurement duck-typing), the frozen
:class:`FoldedPoint` / :class:`PeriodSnapshot` validation,
:func:`folded_window` / :func:`periodogram_window` (the plot windows reusing
the pure transforms with x = phase / x = period, y = -power),
:class:`PeriodController` (compute_periodogram on demand, select_peak,
set_manual_period, clear, set_project, listeners incl. the queued
``invoke_on_main`` marshalling and the worker-thread store marshalling, the
never-raising snapshot), the **persistence round-trip** (the chosen period is
written to ``project.json`` on every selection and loaded back by a fresh
controller / ``set_project``; a corrupt persisted value is ignored), the
folded points covering ALL sessions sorted by phase, and the package boundary
(subprocess + source-scan).
"""

import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

# tmp-project measurement helpers reused from the ticket-01 test module
from test_analysis_lightcurve import (
    QueuedInvoke,
    two_session_project,
)

# synthetic sinusoid helpers reused from the ticket-02 test module
from test_analysis_periodogram import (
    P_TRUE,
    SIGMA,
    _project_with_sinusoid,
    gapped_sampling,
    make_points,
    sinusoid_mags,
)

from seestar_controller import analysis
from seestar_controller.analysis import fold as fold_module
from seestar_controller.analysis import lightcurve as lightcurve_module
from seestar_controller.analysis.fold import (
    PERIOD_CONFIG_KEY,
    SOURCE_MANUAL,
    SOURCE_PEAK,
    SOURCES,
    FoldedPoint,
    FoldError,
    PeriodController,
    PeriodError,
    PeriodSnapshot,
    fold_points,
    folded_window,
    periodogram_window,
    phase_of,
    validate_period,
)
from seestar_controller.analysis.lightcurve import (
    DB_CALL_TIMEOUT_S,
    AnalysisError,
    LightcurvePoint,
    PlotGeometry,
    data_to_screen,
)
from seestar_controller.analysis.periodogram import (
    DEFAULT_TOP_N,
    compute_periodogram,
)
from seestar_controller.store import (
    create_project,
)
from seestar_controller.store import (
    lightcurve as store_lightcurve,
)

#: Directory of the analysis package (source-scan audit).
ANALYSIS_PACKAGE_DIR = Path(analysis.__file__).resolve().parent


@pytest.fixture()
def root(tmp_path):
    return tmp_path / "projects"


@pytest.fixture()
def project(root):
    p = create_project("M 42 fold", root=root)
    yield p
    p.close()


def _pt(mid: int, jd: float, mag: float = 12.0, session: str = "s", excluded: bool = False, mag_err=None):
    """A small LightcurvePoint helper."""
    return LightcurvePoint(mid, jd, "utc", mag, mag_err, session, excluded)


# --- period validation + the fold math ---------------------------------------------------------------


def test_validate_period_accepts_positive_finite_numbers_and_rejects_the_rest():
    assert validate_period(0.35) == 0.35
    assert validate_period(1) == 1.0
    assert validate_period(1e-9) == 1e-9
    for bad in (0, -1, -0.5, float("nan"), float("inf"), float("-inf"), True, "0.5", None):
        with pytest.raises(FoldError):
            validate_period(bad)
    assert issubclass(FoldError, AnalysisError)
    assert issubclass(PeriodError, FoldError)
    assert issubclass(FoldError, ValueError)


def test_phase_of_exact_values_mod1_wrap_and_the_rounding_edge():
    # jd0 = 100.0, period = 0.5 d — all values exact in binary floating point
    assert phase_of(100.0, 0.5, 100.0) == 0.0
    assert phase_of(100.25, 0.5, 100.0) == 0.5
    assert phase_of(100.5, 0.5, 100.0) == 0.0  # wraps mod 1
    assert phase_of(99.75, 0.5, 100.0) == 0.5  # negative dividend wraps into [0, 1)
    assert phase_of(101.0, 0.5, 100.0) == 0.0
    assert phase_of(100.125, 0.25, 100.0) == 0.5
    # the float-rounding edge of % ((-1e-20) % 1.0 == 1.0) is clamped to 0.0
    assert phase_of(100.0 - 1e-20, 0.5, 100.0) == 0.0
    # phase is ALWAYS in [0, 1)
    for jd in (99.9, 100.0, 100.1, 100.3, 100.7, 123.456):
        p = phase_of(jd, 0.35, 100.0)
        assert 0.0 <= p < 1.0


def test_phase_of_rejects_invalid_inputs():
    for bad_period in (0, -1, float("nan"), float("inf"), True, "x"):
        with pytest.raises(FoldError):
            phase_of(100.0, bad_period, 100.0)
    for bad in (float("nan"), float("inf"), True, "x"):
        with pytest.raises(FoldError):
            phase_of(bad, 0.5, 100.0)  # type: ignore[arg-type]
        with pytest.raises(FoldError):
            phase_of(100.0, 0.5, bad)  # type: ignore[arg-type]


# --- fold_points --------------------------------------------------------------------------------------


def test_fold_points_sorted_by_phase_with_default_jd0_being_the_min_jd():
    pts = [_pt(1, 100.5), _pt(2, 100.0), _pt(3, 100.25), _pt(4, 100.75)]
    folded = fold_points(pts, 0.5)
    assert isinstance(folded, tuple)
    assert all(isinstance(fp, FoldedPoint) for fp in folded)
    # sorted by phase (stable: ties keep the input order)
    assert [fp.phase for fp in folded] == [0.0, 0.0, 0.5, 0.5]
    # the default jd0 is the FIRST observation = min jd = 100.0 here
    by_id = {fp.point.measurement_id: fp.phase for fp in folded}
    assert by_id == {1: 0.0, 2: 0.0, 3: 0.5, 4: 0.5}
    phases = [fp.phase for fp in folded]
    assert phases == sorted(phases)
    assert all(0.0 <= p < 1.0 for p in phases)
    # the original points are carried
    assert {fp.point.measurement_id for fp in folded} == {1, 2, 3, 4}


def test_fold_points_default_jd0_is_the_min_jd_even_when_not_first_in_the_input():
    pts = [_pt(1, 100.5), _pt(2, 99.75)]  # the min jd is the SECOND point
    folded = fold_points(pts, 0.5)
    by_id = {fp.point.measurement_id: fp.phase for fp in folded}
    assert by_id == {1: 0.5, 2: 0.0}  # jd0 = 99.75 (the min), so 99.75 -> phase 0


def test_fold_points_with_a_custom_jd0():
    pts = [_pt(1, 100.0), _pt(2, 100.25), _pt(3, 100.5)]
    folded = fold_points(pts, 0.5, jd0=100.25)
    by_id = {fp.point.measurement_id: fp.phase for fp in folded}
    assert by_id == {1: 0.5, 2: 0.0, 3: 0.5}


def test_fold_points_empty_sets_and_invalid_input():
    with pytest.raises(FoldError):
        fold_points([], 0.5)  # no points and no explicit jd0 -> no epoch
    assert fold_points([], 0.5, jd0=100.0) == ()
    with pytest.raises(FoldError):
        fold_points([_pt(1, 100.0)], 0)  # invalid period
    with pytest.raises(FoldError):
        fold_points([_pt(1, 100.0)], 0.5, jd0=float("nan"))
    with pytest.raises(FoldError):
        fold_points(42, 0.5)  # not iterable  # ty: ignore[invalid-argument-type]
    with pytest.raises(FoldError):
        fold_points([object()], 0.5)  # no jd attribute


def test_fold_points_accepts_store_measurements(project):
    """Duck-typed store Measurements are mapped to LightcurvePoints."""
    two_session_project(project)
    measurements = store_lightcurve(project, include_excluded=True)
    folded = fold_points(measurements, 0.1)
    assert len(folded) == len(measurements) == 5
    assert all(isinstance(fp.point, LightcurvePoint) for fp in folded)
    assert {fp.point.measurement_id for fp in folded} == {m.id for m in measurements}
    phases = [fp.phase for fp in folded]
    assert phases == sorted(phases)


def test_folded_point_is_frozen_and_validated():
    fp = FoldedPoint(point=_pt(1, 100.0), phase=0.25)
    assert fp.phase == 0.25 and fp.point.measurement_id == 1
    with pytest.raises(Exception):  # noqa: B017
        fp.phase = 0.5  # frozen  # ty: ignore[invalid-assignment]
    with pytest.raises(FoldError):
        FoldedPoint(point=_pt(1, 100.0), phase=1.0)  # phase must be < 1
    with pytest.raises(FoldError):
        FoldedPoint(point=_pt(1, 100.0), phase=-0.1)
    with pytest.raises(FoldError):
        FoldedPoint(point=_pt(1, 100.0), phase=float("nan"))
    with pytest.raises(FoldError):
        FoldedPoint(point=_pt(1, 100.0), phase=True)
    with pytest.raises(FoldError):
        FoldedPoint(point="not-a-point", phase=0.5)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


# --- the plot windows (pure transforms with x = phase / x = period) ------------------------------------


def test_folded_window_phase_axis_and_mag_padding():
    w = folded_window(())
    assert w.jd_min == -0.02 and w.jd_max == 1.02  # x = phase 0-1, padded 2%
    assert w.mag_min == -1.0 and w.mag_max == 1.0
    folded = fold_points([_pt(1, 100.0, mag=12.0), _pt(2, 100.2, mag=12.4)], 0.5)
    w = folded_window(folded)
    assert w.jd_min == -0.02 and w.jd_max == 1.02
    assert w.mag_min == pytest.approx(12.0 - 0.05 * 0.4)  # mag padded 5% like full_window
    assert w.mag_max == pytest.approx(12.4 + 0.05 * 0.4)


def test_periodogram_window_negated_power_axis_puts_max_power_on_top():
    t = gapped_sampling()
    result = compute_periodogram(make_points(t, sinusoid_mags(t), np.full(len(t), SIGMA)))
    w = periodogram_window(result)
    # x = period = 1/frequency over the grid (small periods on the left)
    assert w.jd_min == pytest.approx(1.0 / float(result.frequencies[-1]))
    assert w.jd_max == pytest.approx(1.0 / float(result.frequencies[0]))
    # y = -power (padded 5%): mag_min (drawn at the TOP) carries the max power
    p_min = float(np.min(result.powers))
    p_max = float(np.max(result.powers))
    assert w.mag_min == pytest.approx(-(p_max + 0.05 * (p_max - p_min)))
    assert w.mag_max == pytest.approx(-(p_min - 0.05 * (p_max - p_min)))
    geom = PlotGeometry(480, 320)
    top = result.peaks[0]
    _x, y_top = data_to_screen(w, geom, top.period, -top.power)
    _x, y_bottom = data_to_screen(w, geom, top.period, -p_min)
    assert y_top < y_bottom  # max power is ABOVE the min power on screen
    # max power sits just below the top edge (the window pads the power 5%)
    assert y_top == pytest.approx(geom.margin_top + (0.05 / 1.1) * geom.plot_height)


# --- PeriodSnapshot validation ---------------------------------------------------------------------------


def test_period_snapshot_is_frozen_and_validated():
    snap = PeriodSnapshot(
        period=0.5, source=SOURCE_PEAK, peak_index=2, jd0=100.0,
        result=None, peaks=(), folded=(), error=None,
    )
    assert snap.peak_index == 2
    with pytest.raises(Exception):  # noqa: B017
        snap.period = 0.6  # frozen  # ty: ignore[invalid-assignment]
    with pytest.raises(FoldError):
        PeriodSnapshot(period=-1.0, source=None, peak_index=None, jd0=None, result=None, peaks=(), folded=(), error=None)
    with pytest.raises(FoldError):
        PeriodSnapshot(period=0.5, source="bogus", peak_index=None, jd0=None, result=None, peaks=(), folded=(), error=None)
    with pytest.raises(FoldError):
        PeriodSnapshot(period=0.5, source=SOURCE_PEAK, peak_index=None, jd0=None, result=None, peaks=(), folded=(), error=None)
    with pytest.raises(FoldError):
        PeriodSnapshot(period=None, source=None, peak_index=None, jd0=None, result=None, peaks=[], folded=(), error=None)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


# --- PeriodController: compute + select_peak + fold --------------------------------------------------------


def test_controller_compute_periodogram_select_peak_and_fold(project):
    n = _project_with_sinusoid(project)
    ctrl = PeriodController(project=project)
    assert ctrl.project is project
    assert ctrl.lightcurve_controller.project is project
    # before computing: no result, no peaks, no period
    snap = ctrl.snapshot
    assert snap.result is None and snap.peaks == () and snap.period is None
    assert snap.source is None and snap.error is None
    # compute on demand
    result = ctrl.compute_periodogram()
    assert ctrl.result is result
    assert len(result.peaks) == DEFAULT_TOP_N
    assert abs(result.peaks[0].period - P_TRUE) / P_TRUE < 0.01  # the true period
    snap = ctrl.snapshot
    assert snap.result is result and snap.peaks == result.peaks and snap.error is None
    # select the top peak
    period = ctrl.select_peak(0)
    assert period == result.peaks[0].period
    assert ctrl.period == period
    assert ctrl.source == SOURCE_PEAK
    snap = ctrl.snapshot
    assert snap.period == period and snap.source == SOURCE_PEAK and snap.peak_index == 0
    # the folded points cover ALL the lightcurve points (excluded included),
    # sorted by phase, with jd0 = the first observation
    assert len(ctrl.points) == n + 1  # the excluded outlier is in the lightcurve
    assert len(snap.folded) == len(ctrl.points)
    assert all(isinstance(fp, FoldedPoint) for fp in snap.folded)
    phases = [fp.phase for fp in snap.folded]
    assert phases == sorted(phases)
    assert all(0.0 <= p < 1.0 for p in phases)
    assert snap.jd0 == min(p.jd for p in ctrl.points)
    assert ctrl.folded_points() == snap.folded


def test_select_peak_errors_leave_the_state_unchanged(project):
    _project_with_sinusoid(project)
    ctrl = PeriodController(project=project)
    with pytest.raises(PeriodError, match="no periodogram"):
        ctrl.select_peak(0)
    result = ctrl.compute_periodogram()
    for bad in (-1, len(result.peaks), 100):
        with pytest.raises(PeriodError):
            ctrl.select_peak(bad)
    for bad in (0.5, "0", True, None):
        with pytest.raises(PeriodError):
            ctrl.select_peak(bad)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    assert ctrl.period is None and ctrl.source is None
    assert ctrl.snapshot.peak_index is None


def test_set_manual_period_validates_applies_and_folds_all_sessions(project):
    session_a, session_b = two_session_project(project)
    ctrl = PeriodController(project=project)
    assert ctrl.set_manual_period(0.1) == 0.1
    assert ctrl.period == 0.1 and ctrl.source == SOURCE_MANUAL
    snap = ctrl.snapshot
    assert snap.period == 0.1 and snap.source == SOURCE_MANUAL and snap.peak_index is None
    # the folded points cover ALL sessions (5 points across 2 sessions)
    assert len(snap.folded) == 5
    assert {fp.point.session_id for fp in snap.folded} == {session_a, session_b}
    phases = [fp.phase for fp in snap.folded]
    assert phases == sorted(phases)  # sorted by phase
    # invalid input raises FoldError and changes nothing
    for bad in (0, -1, float("nan"), float("inf"), True, "0.1"):
        with pytest.raises(FoldError):
            ctrl.set_manual_period(bad)
    assert ctrl.period == 0.1 and ctrl.source == SOURCE_MANUAL


def test_set_manual_period_exception_contract_the_widget_relies_on(project):
    """Regression test for the period-view manual-entry handler: it catches
    ``FoldError`` (the SUPERCLASS), because :meth:`PeriodController.set_manual_period`
    raises ``FoldError`` for an invalid entry AND ``PeriodError`` (a
    ``FoldError`` subclass) for a persistence failure — one
    ``except FoldError`` in the widget covers both red-status paths."""
    assert issubclass(PeriodError, FoldError)  # the widget's except FoldError covers PeriodError
    ctrl = PeriodController(project=project)
    # invalid input raises FoldError (not PeriodError) and changes nothing
    for bad in (0, -1, float("nan"), float("inf"), True, "0.1", None):
        with pytest.raises(FoldError):
            ctrl.set_manual_period(bad)
    assert ctrl.period is None and ctrl.source is None
    # a persistence failure raises PeriodError — still a FoldError, so the
    # widget's single `except FoldError` renders it as the red status too
    def _boom() -> None:
        raise RuntimeError("disk full")

    project.save_config = _boom  # type: ignore[method-assign]
    with pytest.raises(PeriodError) as excinfo:
        ctrl.set_manual_period(0.5)
    assert isinstance(excinfo.value, FoldError)
    assert ctrl.period is None  # the in-memory state is unchanged on failure


def test_controller_without_a_project():
    ctrl = PeriodController()
    assert ctrl.project is None
    snap = ctrl.snapshot
    assert snap.period is None and snap.source is None and snap.error is None
    assert snap.folded == () and snap.peaks == () and snap.result is None
    with pytest.raises(PeriodError):
        ctrl.compute_periodogram()
    # a manual selection works in memory (nothing to persist without a project)
    assert ctrl.set_manual_period(0.5) == 0.5
    assert ctrl.snapshot.period == 0.5 and ctrl.snapshot.folded == ()
    ctrl.clear()
    assert ctrl.period is None and ctrl.source is None


def test_compute_periodogram_failures_are_clean_period_errors(project):
    ctrl = PeriodController(project=project)  # empty project
    with pytest.raises(PeriodError):
        ctrl.compute_periodogram()
    assert ctrl.result is None
    assert ctrl.snapshot.error is not None  # surfaced in the snapshot, never a crash
    assert ctrl.snapshot.period is None


def test_snapshot_never_raises_on_a_store_failure(project, monkeypatch):
    two_session_project(project)
    ctrl = PeriodController(project=project)
    ctrl.set_manual_period(0.1)
    assert ctrl.snapshot.error is None

    def boom(*args, **kwargs):
        raise RuntimeError("injected store failure")

    # issue 08: a closed db handle is revived by ensure_open(), so the failure
    # is injected at the lightcurve store read instead of via project.close()
    monkeypatch.setattr(lightcurve_module, "lightcurve", boom)
    snap = ctrl.snapshot
    assert snap.error is not None
    assert "Error" in snap.error


# --- persistence: save on select, load on set_project (round-trip through project.json) ---------------------


def _read_persisted_period(project) -> object:
    return json.loads(project.config_path.read_text(encoding="utf-8"))[PERIOD_CONFIG_KEY]


def test_period_is_persisted_on_selection_and_loaded_back_round_trip(project):
    _project_with_sinusoid(project)
    assert _read_persisted_period(project) is None  # the placeholder
    ctrl = PeriodController(project=project)
    # manual selection persists a plain float (days)
    ctrl.set_manual_period(0.35)
    assert _read_persisted_period(project) == 0.35
    assert isinstance(_read_persisted_period(project), float)
    # a FRESH controller over the same project loads it back (source 'manual')
    ctrl2 = PeriodController(project=project)
    assert ctrl2.period == 0.35
    assert ctrl2.source == SOURCE_MANUAL
    assert ctrl2.snapshot.period == 0.35
    assert len(ctrl2.snapshot.folded) == len(ctrl2.points)  # folded on load
    # peak selection persists the peak's period
    ctrl2.compute_periodogram()
    period = ctrl2.select_peak(0)
    assert _read_persisted_period(project) == period
    assert ctrl2.source == SOURCE_PEAK
    # clear persists null
    ctrl2.clear()
    assert _read_persisted_period(project) is None
    assert PeriodController(project=project).period is None


def test_set_project_loads_the_persisted_period_and_clears_on_switch(project, root):
    _project_with_sinusoid(project)
    PeriodController(project=project).set_manual_period(0.42)
    other = create_project("M 31 fold", root=root)
    try:
        ctrl = PeriodController()  # no project yet
        assert ctrl.period is None
        ctrl.set_project(project)  # loads the persisted period
        assert ctrl.project is project
        assert ctrl.period == 0.42
        assert ctrl.source == SOURCE_MANUAL
        # switching to another project loads ITS persisted period (null -> cleared)
        ctrl.set_project(other)
        assert ctrl.project is other
        assert ctrl.period is None
        # clearing the project clears the selection
        ctrl.set_project(project)
        assert ctrl.period == 0.42
        ctrl.set_project(None)
        assert ctrl.project is None
        assert ctrl.snapshot.period is None
    finally:
        other.close()


def test_corrupt_persisted_period_is_ignored_never_a_crash(project):
    for bad in ("bogus", -5.0, 0, float("nan"), [0.5]):
        project.config[PERIOD_CONFIG_KEY] = bad
        project.save_config()
        ctrl = PeriodController(project=project)
        assert ctrl.period is None
        assert ctrl.snapshot.error is None
        assert ctrl.snapshot.period is None


# --- listeners + threading ---------------------------------------------------------------------------------


def test_listeners_add_remove_idempotent_and_failing_listener_isolated(project):
    _project_with_sinusoid(project)
    ctrl = PeriodController(project=project)
    seen: list[PeriodSnapshot] = []
    ctrl.add_listener(seen.append)
    ctrl.add_listener(seen.append)  # idempotent
    assert ctrl.listeners == (seen.append,)
    ctrl.set_manual_period(0.5)
    assert len(seen) == 1 and seen[0].period == 0.5

    def bad(_snapshot):
        raise RuntimeError("boom")

    ctrl.add_listener(bad)
    ctrl.set_manual_period(0.6)  # must not raise
    assert seen[-1].period == 0.6
    ctrl.remove_listener(seen.append)
    ctrl.remove_listener(seen.append)  # no-op
    assert ctrl.listeners == (bad,)
    ctrl.set_manual_period(0.7)
    assert seen[-1].period == 0.6  # no longer notified


def test_queued_invoke_marshals_listener_notifications(project):
    _project_with_sinusoid(project)
    queued = QueuedInvoke()
    ctrl = PeriodController(project=project, invoke_on_main=queued)
    queued.drain()  # the constructor's notifications
    seen: list[PeriodSnapshot] = []
    ctrl.add_listener(seen.append)
    ctrl.set_manual_period(0.5)  # same thread: the store write runs directly...
    assert seen == []  # ...but the notification is queued
    queued.drain()
    assert len(seen) == 1
    assert seen[0].period == 0.5
    assert len(seen[0].folded) == len(ctrl.points)


def test_compute_periodogram_from_a_worker_thread_marshals_store_calls(project):
    """The project db lives on the main thread; ``compute_periodogram`` called
    from a worker thread marshals the store read through ``invoke_on_main``
    (pumped by the main thread here, GLib.idle_add in the app)."""
    _project_with_sinusoid(project)
    queued = QueuedInvoke()
    ctrl = PeriodController(project=project, invoke_on_main=queued)
    queued.drain()
    box: dict = {}

    def work():
        box["result"] = ctrl.compute_periodogram()

    worker = threading.Thread(target=work, name="test-fold-worker", daemon=True)
    worker.start()
    deadline = time.monotonic() + DB_CALL_TIMEOUT_S
    while worker.is_alive() and time.monotonic() < deadline:
        queued.pump_one(timeout=0.05)  # the "main loop" runs the store read
    worker.join(timeout=10.0)
    assert not worker.is_alive()
    result = box["result"]
    assert abs(result.peaks[0].period - P_TRUE) / P_TRUE < 0.01


# --- package boundary (subprocess + source scan) --------------------------------------------------------------


def test_fold_module_imports_no_gi_no_link_no_ui_no_photometry_no_enhance_and_widgets_stay_lazy():
    code = (
        "import sys;"
        "import seestar_controller.analysis;"
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


def test_fold_source_is_pure_and_period_view_is_a_widget_module_lazy_exported():
    modules = {path.stem for path in ANALYSIS_PACKAGE_DIR.glob("*.py")}
    assert modules == {
        "__init__", "lightcurve", "periodogram", "fold", "export",
        "lightcurve_view", "period_view",
    }
    forbidden = re.compile(
        r"^\s*(import|from)\s+(gi\b|seestar_controller\.(gi|ui|link|photometry|enhance)\b)",
        re.MULTILINE,
    )
    source = (ANALYSIS_PACKAGE_DIR / "fold.py").read_text(encoding="utf-8")
    assert not forbidden.search(source), "fold.py imports a forbidden module"
    assert not re.search(r"^\s*(import gi|from gi)\b", source, re.MULTILINE), "fold.py imports gi"
    # the widget module imports gi at module top, before from gi.repository
    lines = (ANALYSIS_PACKAGE_DIR / "period_view.py").read_text(encoding="utf-8").splitlines()
    gi_lines = [i for i, line in enumerate(lines) if line.strip() == "import gi"]
    assert gi_lines, "period_view.py has no 'import gi' at module top"
    repo_line = next(
        i for i, line in enumerate(lines) if line.startswith("from gi.repository")
    )
    assert gi_lines[0] < repo_line, "'import gi' must come before 'from gi.repository'"
    assert analysis._WIDGET_EXPORTS == {
        "LightcurveView": "lightcurve_view",
        "PeriodView": "period_view",
    }
    assert fold_module.PeriodController is PeriodController


def test_public_api_is_exported_from_package():
    for name in (
        "PERIOD_CONFIG_KEY",
        "SOURCE_MANUAL",
        "SOURCE_PEAK",
        "SOURCES",
        "FoldedPoint",
        "FoldError",
        "PeriodController",
        "PeriodError",
        "PeriodListener",
        "PeriodSnapshot",
        "folded_window",
        "fold_points",
        "periodogram_window",
        "phase_of",
        "validate_period",
    ):
        assert name in analysis.__all__, name
        assert hasattr(analysis, name), name
    assert "PeriodView" in analysis.__all__  # lazy widget export
    assert analysis.PeriodController is PeriodController
    assert analysis.fold_points is fold_points
    assert analysis.phase_of is phase_of
    assert PERIOD_CONFIG_KEY == "period"
    assert SOURCES == (SOURCE_PEAK, SOURCE_MANUAL)
