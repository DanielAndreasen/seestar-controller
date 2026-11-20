"""Pure-layer tests for period-analysis ticket 04 — CSV export + outlier
flagging (:mod:`seestar_controller.analysis.export`,
:meth:`LightcurveController.set_excluded` / :meth:`toggle_excluded`).

Headless: no ``gi`` anywhere (the sandbox has no GTK, see
docs/wiki/assumptions.md). The exports are exercised against a tmp project
with measurements across TWO sessions (the same store helpers as
tests/test_analysis_lightcurve.py) and against plain duck-typed point
iterables (the helpers of tests/test_analysis_periodogram.py).

Covered: the lightcurve CSV export (delegation to the store exporter — same
file bytes, same header ``jd,utc,mag,mag_err,session,target``, exact
``repr(float)`` round-trip re-parsed with the stdlib ``csv`` module, the
``include_excluded`` row toggle, custom paths, the default
``<project>/lightcurve.csv``), the folded CSV export (header
``jd,utc,mag,mag_err,session,target,phase``, rows sorted by phase, phases in
``[0, 1)`` and exactly :func:`phase_of`, from a project — default
``<project>/folded.csv`` — or from a point iterable with a required path,
``jd0`` default = first included observation, ``target=`` override), the
error contract (:class:`TypeError` for bad argument types,
:class:`ExportError` — an :class:`AnalysisError` — for analysis-level
failures, :class:`OSError` propagating from the write), the atomic write (no
``*.tmp`` left, a failed ``os.replace`` keeps the previous file, a
``KeyboardInterrupt`` during fsync leaves no partial file), the outlier
flagging on the controller (``set_excluded`` / ``toggle_excluded`` flip the
store flag, reload + notify the listeners, work from a worker thread via
``invoke_on_main``), the exclusion's effect on the analysis input
(``project_periodogram`` default excludes flagged points — verified against
``n_points`` and the peak power — and the folded snapshot keeps the flagged
point marked), and the package boundary (subprocess + source-scan: the pure
export module never imports ``gi`` / link / ui / photometry / enhance).
"""

import csv
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import seestar_controller.analysis as analysis
from seestar_controller.analysis import (
    FOLDED_CSV_FILE,
    FOLDED_CSV_HEADER,
    AnalysisError,
    ExportError,
    LightcurveController,
    LightcurveError,
    PeriodController,
    export_folded_csv,
    export_lightcurve_csv,
    phase_of,
)
from seestar_controller.analysis.periodogram import project_periodogram
from seestar_controller.store import CSV_FILE, CSV_HEADER, lightcurve, set_excluded
from seestar_controller.store.csv_export import export_csv as store_export_csv

# helpers reused from the sibling ticket test modules (tests/ is on sys.path)
from test_analysis_lightcurve import (  # noqa: E402
    ANALYSIS_PACKAGE_DIR,
    JD0,
    QueuedInvoke,
    two_session_project as _two_session_project_fn,
)
from test_analysis_periodogram import (  # noqa: E402
    P_TRUE,
    SIGMA,
    _project_with_sinusoid,
    gapped_sampling,
    make_points,
    sinusoid_mags,
)


@pytest.fixture()
def root(tmp_path):
    return tmp_path / "projects"


@pytest.fixture()
def project(root):
    from seestar_controller.store import create_project

    p = create_project("M 42 night", root=root)
    yield p
    p.close()


@pytest.fixture()
def two_session_project(project):
    """Two sessions with interleaved jds; session A has one excluded point."""
    return _two_session_project_fn(project)


@pytest.fixture()
def target(project):
    """Set the project config target (a mapping with a name, like ticket 01)."""
    project.config["target"] = {"name": "M 42", "ra_deg": 83.82208, "dec_deg": -5.39114}
    project.save_config()
    return "M 42"


def read_csv(path: Path) -> list[list[str]]:
    """Re-parse a CSV file with the stdlib csv module (standard tool)."""
    with path.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.reader(fh))


# --- lightcurve CSV export (delegates to the store exporter) -------------------------------------


def test_export_lightcurve_csv_matches_the_store_exporter_byte_for_byte(project, two_session_project, target, tmp_path):
    via_analysis = export_lightcurve_csv(project)
    assert via_analysis == project.path / CSV_FILE
    via_store = store_export_csv(project, path=tmp_path / "store.csv")
    assert via_analysis.read_bytes() == via_store.read_bytes()
    # include_excluded too
    a_all = export_lightcurve_csv(project, path=tmp_path / "a_all.csv", include_excluded=True)
    s_all = store_export_csv(project, path=tmp_path / "s_all.csv", include_excluded=True)
    assert a_all.read_bytes() == s_all.read_bytes()


def test_lightcurve_csv_roundtrip_reparse_matches_db_exactly(project, two_session_project, target):
    path = export_lightcurve_csv(project)
    rows = read_csv(path)
    assert tuple(rows[0]) == CSV_HEADER
    measurements = lightcurve(project)  # default: excluded omitted
    assert len(rows) - 1 == len(measurements) == 4  # 5 points, 1 excluded
    for row, m in zip(rows[1:], measurements):
        jd_s, utc_s, mag_s, err_s, session_s, target_s = row
        assert float(jd_s) == m.jd  # exact repr(float) round-trip
        assert utc_s == m.utc
        assert float(mag_s) == m.mag
        if m.mag_err is None:
            assert err_s == ""  # NULL mag_err -> empty field
        else:
            assert float(err_s) == m.mag_err
        assert session_s == m.session_id
        assert target_s == "M 42"
    # rows are ordered by jd (the store lightcurve order)
    assert [float(r[0]) for r in rows[1:]] == sorted(float(r[0]) for r in rows[1:])


def test_lightcurve_csv_include_excluded_toggles_rows(project, two_session_project, target, tmp_path):
    default = read_csv(export_lightcurve_csv(project, path=tmp_path / "d.csv"))
    assert len(default) - 1 == 4
    all_rows = read_csv(export_lightcurve_csv(project, path=tmp_path / "a.csv", include_excluded=True))
    assert len(all_rows) - 1 == 5
    assert len(lightcurve(project, include_excluded=True)) == 5
    # the extra row is the excluded one (jd JD0+0.20, mag 12.40, NULL mag_err)
    extra = [r for r in all_rows[1:] if r not in default[1:]]
    assert len(extra) == 1
    assert float(extra[0][0]) == pytest.approx(JD0 + 0.20)
    assert float(extra[0][2]) == pytest.approx(12.40)
    assert extra[0][3] == ""  # NULL mag_err -> empty field


def test_export_lightcurve_csv_custom_path_str_and_path(project, two_session_project, tmp_path):
    p1 = export_lightcurve_csv(project, path=str(tmp_path / "sub" / "lc.csv"))
    assert p1 == tmp_path / "sub" / "lc.csv"  # parent dirs created
    assert p1.exists()
    p2 = export_lightcurve_csv(project, path=tmp_path / "lc2.csv")
    assert isinstance(p2, Path) and p2.exists()
    assert read_csv(p1)[0] == list(CSV_HEADER)


def test_export_lightcurve_csv_error_paths(project, two_session_project):
    with pytest.raises(TypeError):
        export_lightcurve_csv(project, path=123)
    with pytest.raises(TypeError):
        export_lightcurve_csv(project, include_excluded=1)
    with pytest.raises(ExportError):  # no project -> the store call fails -> wrapped
        export_lightcurve_csv(None)
    assert issubclass(ExportError, AnalysisError) and issubclass(ExportError, ValueError)


# --- folded CSV export ----------------------------------------------------------------------------


def test_folded_csv_from_project_default_path_header_and_phases(project, two_session_project, target):
    period = 0.1
    path = export_folded_csv(project, period)
    assert path == project.path / FOLDED_CSV_FILE
    rows = read_csv(path)
    assert tuple(rows[0]) == FOLDED_CSV_HEADER
    assert len(rows[0]) == 7
    measurements = lightcurve(project)  # excluded omitted by default
    assert len(rows) - 1 == len(measurements) == 4
    jd0 = min(m.jd for m in measurements)  # the fold epoch = first included observation
    phases = [float(r[6]) for r in rows[1:]]
    assert phases == sorted(phases)  # rows sorted by phase (the folded order)
    assert all(0.0 <= p < 1.0 for p in phases)
    for row, m in zip(rows[1:], sorted(measurements, key=lambda m: phase_of(m.jd, period, jd0))):
        jd_s, utc_s, mag_s, err_s, session_s, target_s, phase_s = row
        assert float(jd_s) == m.jd
        assert utc_s == m.utc
        assert float(mag_s) == m.mag
        assert (err_s == "" if m.mag_err is None else float(err_s) == m.mag_err)
        assert session_s == m.session_id
        assert target_s == "M 42"
        assert float(phase_s) == phase_of(m.jd, period, jd0)  # exactly phase_of
        assert float(phase_s) == float(repr(float(phase_s)))  # repr round-trip


def test_folded_csv_include_excluded_toggles_rows(project, two_session_project, target, tmp_path):
    period = 0.1
    default = read_csv(export_folded_csv(project, period, path=tmp_path / "d.csv"))
    assert len(default) - 1 == 4
    all_rows = read_csv(
        export_folded_csv(project, period, path=tmp_path / "a.csv", include_excluded=True)
    )
    assert len(all_rows) - 1 == 5
    # with the excluded point included the epoch is still the min jd (JD0+0.10)
    jd0 = min(float(r[0]) for r in all_rows[1:])
    for row in all_rows[1:]:
        assert float(row[6]) == phase_of(float(row[0]), period, jd0)


def test_folded_csv_from_points_iterable_requires_path_and_supports_jd0_and_target(tmp_path):
    t = gapped_sampling()
    points = make_points(t, sinusoid_mags(t), mag_errs=[SIGMA] * len(t))
    with pytest.raises(ExportError):  # no project -> no default destination
        export_folded_csv(points, P_TRUE)
    path = tmp_path / "folded.csv"
    out = export_folded_csv(points, P_TRUE, path=path, jd0=0.0, target="V* X")
    assert out == path
    rows = read_csv(path)
    assert tuple(rows[0]) == FOLDED_CSV_HEADER
    assert len(rows) - 1 == len(points)
    phases = [float(r[6]) for r in rows[1:]]
    assert phases == sorted(phases)
    for row, p in zip(rows[1:], sorted(points, key=lambda p: phase_of(p.jd, P_TRUE, 0.0))):
        assert float(row[0]) == p.jd
        assert float(row[2]) == p.mag
        assert float(row[3]) == SIGMA
        assert row[4] == "s"  # the session id of the duck-typed points
        assert row[5] == "V* X"  # the explicit target override
        assert float(row[6]) == phase_of(p.jd, P_TRUE, 0.0)
    # default jd0 = the first (min jd) point
    out2 = export_folded_csv(points, P_TRUE, path=tmp_path / "f2.csv")
    jd0 = min(p.jd for p in points)
    for row in read_csv(out2)[1:]:
        assert float(row[6]) == phase_of(float(row[0]), P_TRUE, jd0)


def test_folded_csv_from_points_filters_excluded_unless_requested(tmp_path):
    t = gapped_sampling()[:10]
    points = make_points(t, sinusoid_mags(t), excluded=[i == 3 for i in range(len(t))])
    path = tmp_path / "f.csv"
    export_folded_csv(points, P_TRUE, path=path, jd0=0.0)
    assert len(read_csv(path)) - 1 == len(points) - 1
    export_folded_csv(points, P_TRUE, path=path, jd0=0.0, include_excluded=True)
    assert len(read_csv(path)) - 1 == len(points)


def test_folded_csv_error_paths(project, two_session_project, tmp_path):
    points = make_points(gapped_sampling()[:5], sinusoid_mags(gapped_sampling()[:5]))
    # invalid period -> ExportError (the FoldError is re-wrapped)
    for bad in (0, -1.0, float("nan"), "0.35", None):
        with pytest.raises(ExportError):
            export_folded_csv(project, bad, path=tmp_path / "x.csv")
        with pytest.raises(ExportError):
            export_folded_csv(points, bad, path=tmp_path / "x.csv")
    # empty points without jd0 -> ExportError; with jd0 -> header-only file
    with pytest.raises(ExportError):
        export_folded_csv((), P_TRUE, path=tmp_path / "empty.csv")
    out = export_folded_csv((), P_TRUE, path=tmp_path / "empty.csv", jd0=JD0)
    assert read_csv(out) == [list(FOLDED_CSV_HEADER)]
    # bad argument types -> TypeError
    with pytest.raises(TypeError):
        export_folded_csv(project, P_TRUE, path=123)
    with pytest.raises(TypeError):
        export_folded_csv(project, P_TRUE, path=tmp_path / "x.csv", include_excluded="yes")
    with pytest.raises(TypeError):
        export_folded_csv(project, P_TRUE, path=tmp_path / "x.csv", target=42)
    # points missing attributes -> ExportError
    with pytest.raises(ExportError):
        export_folded_csv([object()], P_TRUE, path=tmp_path / "x.csv", jd0=0.0)
    # a non-iterable, non-project source -> ExportError
    with pytest.raises(ExportError):
        export_folded_csv(42, P_TRUE, path=tmp_path / "x.csv")


def test_folded_csv_atomic_write_leaves_no_tmp(project, two_session_project, tmp_path, monkeypatch):
    dest = tmp_path / "folded.csv"
    export_folded_csv(project, 0.1, path=dest)
    assert dest.exists()
    assert list(tmp_path.glob("*.tmp")) == []
    assert list(project.path.glob("*.tmp")) == []
    # a failed os.replace: OSError propagates, the previous file stays intact, no tmp left
    previous = dest.read_bytes()

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        export_folded_csv(project, 0.1, path=dest)
    monkeypatch.undo()
    assert dest.read_bytes() == previous
    assert list(tmp_path.glob("*.tmp")) == []
    # a KeyboardInterrupt during fsync: no partial file, no tmp left
    fresh = tmp_path / "fresh.csv"

    def interrupt(fd):
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "fsync", interrupt)
    with pytest.raises(KeyboardInterrupt):
        export_folded_csv(project, 0.1, path=fresh)
    monkeypatch.undo()
    assert not fresh.exists()
    assert list(tmp_path.glob("*.tmp")) == []


# --- outlier flagging on the controller + its effect on the analysis input -------------------------


def test_set_excluded_and_toggle_excluded_flip_the_store_flag_and_notify(project, two_session_project):
    lc = LightcurveController(project)
    snapshots: list = []
    lc.add_listener(snapshots.append)
    measurements = lightcurve(project, include_excluded=True)
    target_point = next(m for m in measurements if not m.excluded)
    # toggle: False -> True
    new_state = lc.toggle_excluded(target_point.id)
    assert new_state is True
    assert target_point.id not in {m.id for m in lightcurve(project)}  # gone from the default
    assert target_point.id in {m.id for m in lightcurve(project, include_excluded=True)}
    flagged = next(m for m in lightcurve(project, include_excluded=True) if m.id == target_point.id)
    assert flagged.excluded is True
    # the controller reloaded + notified, and the snapshot carries the flag
    assert snapshots, "listeners notified"
    assert any(p.measurement_id == target_point.id and p.excluded for p in lc.snapshot.points)
    # set_excluded returns the updated point; toggle back restores
    point = lc.set_excluded(target_point.id, False)
    assert point.excluded is False
    assert lc.toggle_excluded(target_point.id) is True
    assert lc.toggle_excluded(target_point.id) is False
    # error paths
    with pytest.raises(LightcurveError):
        lc.set_excluded("not-an-int", True)
    with pytest.raises(LightcurveError):
        lc.set_excluded(target_point.id, 1)
    with pytest.raises(LightcurveError):
        lc.toggle_excluded(10**9)  # unknown id
    with pytest.raises(LightcurveError):
        LightcurveController().toggle_excluded(1)  # no project


def test_toggle_excluded_from_a_worker_thread_via_invoke_on_main(project, two_session_project):
    queued = QueuedInvoke()
    lc = LightcurveController(project, invoke_on_main=queued)  # db thread = this thread
    measurements = lightcurve(project, include_excluded=True)
    target_point = next(m for m in measurements if not m.excluded)
    result: dict = {}

    def work():
        result["state"] = lc.toggle_excluded(target_point.id)

    thread = threading.Thread(target=work)
    thread.start()
    # the store call is marshalled through invoke_on_main: pump until the worker finishes
    while thread.is_alive():
        queued.pump_one(timeout=0.05)
    thread.join(timeout=5)
    queued.drain()
    assert result["state"] is True
    flagged = next(m for m in lightcurve(project, include_excluded=True) if m.id == target_point.id)
    assert flagged.excluded is True


def test_exclusion_changes_the_periodogram_input(project):
    n = _project_with_sinusoid(project)  # n normal points + 1 excluded outlier
    r_default = project_periodogram(project)  # default include_excluded=False
    assert r_default.n_points == n
    r_all = project_periodogram(project, include_excluded=True)
    assert r_all.n_points == n + 1
    assert r_all.peaks[0].power != r_default.peaks[0].power  # the outlier changes the spectrum
    assert abs(r_default.peaks[0].period - P_TRUE) / P_TRUE < 0.01
    # flag one NORMAL point via the store: the default periodogram drops it
    normal = lightcurve(project)[0]
    set_excluded(project, normal.id, True)
    r_flagged = project_periodogram(project)
    assert r_flagged.n_points == n - 1
    assert r_flagged.peaks[0].power != r_default.peaks[0].power


def test_period_controller_honors_exclusion_and_marks_it_in_the_folded_snapshot(project):
    n = _project_with_sinusoid(project)
    lc = LightcurveController(project)
    pc = PeriodController(lc)
    result = pc.compute_periodogram()  # default: excluded filtered
    assert result.n_points == n
    # flag one normal point through the lightcurve controller (the UI gesture path)
    normal = next(p for p in lc.snapshot.points if not p.excluded)
    assert lc.toggle_excluded(normal.measurement_id) is True
    result2 = pc.compute_periodogram()
    assert result2.n_points == n - 1
    # select a period so the folded snapshot is populated
    pc.select_peak(0)
    # the folded snapshot still contains the flagged point, marked excluded
    folded = pc.snapshot.folded
    assert len(folded) == n + 1  # ALL points (the outlier + the newly flagged one)
    excluded_ids = {m.id for m in lightcurve(project, include_excluded=True) if m.excluded}
    assert normal.measurement_id in excluded_ids
    assert {fp.point.measurement_id for fp in folded if fp.point.excluded} == excluded_ids
    # and the folded CSV omits it by default, includes it on request
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        p_default = export_folded_csv(project, pc.period, path=Path(td) / "d.csv")
        p_all = export_folded_csv(project, pc.period, path=Path(td) / "a.csv", include_excluded=True)
        assert len(read_csv(p_default)) - 1 == n - 1
        assert len(read_csv(p_all)) - 1 == n + 1


# --- package boundary (subprocess + source scan) ---------------------------------------------------


def test_export_module_imports_no_gi_no_link_no_ui_no_photometry_no_enhance():
    code = (
        "import sys;"
        "import seestar_controller.analysis;"
        "import seestar_controller.analysis.export;"
        "assert 'gi' not in sys.modules, 'GTK imported';"
        "assert 'seestar_controller.link' not in sys.modules, 'link imported eagerly';"
        "assert 'seestar_controller.ui' not in sys.modules, 'ui imported eagerly';"
        "assert 'seestar_controller.photometry' not in sys.modules, 'photometry imported eagerly';"
        "assert 'seestar_controller.enhance' not in sys.modules, 'enhance imported eagerly';"
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


def test_export_source_is_pure_and_the_package_exports_the_api():
    source = (ANALYSIS_PACKAGE_DIR / "export.py").read_text(encoding="utf-8")
    forbidden = re.compile(
        r"^\s*(import|from)\s+(gi\b|seestar_controller\.(gi|ui|link|photometry|enhance)\b)",
        re.MULTILINE,
    )
    assert not forbidden.search(source), "export.py imports a forbidden module"
    assert not re.search(r"^\s*(import gi|from gi)\b", source, re.MULTILINE)
    for name in (
        "ExportError",
        "FOLDED_CSV_FILE",
        "FOLDED_CSV_HEADER",
        "export_folded_csv",
        "export_lightcurve_csv",
    ):
        assert name in analysis.__all__, name
        assert hasattr(analysis, name), name
    assert analysis.ExportError is ExportError
    assert analysis.export_folded_csv is export_folded_csv
    assert analysis.export_lightcurve_csv is export_lightcurve_csv
    assert FOLDED_CSV_HEADER == CSV_HEADER + ("phase",)
