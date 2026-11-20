"""Project store: measurements + lightcurve query — tmp_path, no network.

Every test runs against a ``tmp_path`` project root. Frames are inserted as
``frames`` db rows directly (measurements only need the frame FK row — no FITS
files are involved), sessions via ``start_session`` with a frozen clock.
Round-trips close and reopen the project to prove persistence.
"""

import dataclasses
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest

from seestar_controller.store import (
    Measurement,
    MeasurementError,
    ProjectError,
    add_measurement,
    close_session,
    create_project,
    lightcurve,
    measurements_for_frame,
    open_project,
    set_excluded,
    start_session,
)


@pytest.fixture()
def root(tmp_path):
    """A fresh project root."""
    return tmp_path / "projects"


@pytest.fixture()
def project(root):
    """One created project, closed after the test."""
    p = create_project("M 42", root=root)
    yield p
    p.close()


def frozen(dt: datetime):
    """A frozen clock returning ``dt`` on every call."""
    return lambda: dt


T0 = datetime(2026, 11, 20, 22, 0, 0, tzinfo=UTC)
T1 = T0 + timedelta(hours=1, minutes=30)
T2 = T0 + timedelta(hours=3)


@pytest.fixture()
def two_sessions(project):
    """Two closed sessions with deterministic ids."""
    s1 = start_session(project, clock=frozen(T0))
    close_session(project, s1, clock=frozen(T1))
    s2 = start_session(project, clock=frozen(T2))
    close_session(project, s2, clock=frozen(T2 + timedelta(hours=1)))
    return s1, s2


# Julian Dates + UTC strings of three frames: f1/f3 in session 1 (night 1),
# f2 in session 2 (night 2), with f2 BETWEEN f1 and f3 in time.
JD1 = 2461365.416668  # 2026-11-20T22:00:00Z
UTC1 = "2026-11-20T22:00:00.000+00:00"
JD3 = 2461365.423113  # 2026-11-20T22:09:10Z, same night, later
UTC3 = "2026-11-20T22:09:10.000+00:00"
JD2 = 2461366.416668  # 2026-11-21T22:00:00Z, next night
UTC2 = "2026-11-21T22:00:00.000+00:00"


def add_frame_row(project, session_id, jd, utc, kind="sub") -> int:
    """Insert a ``frames`` row directly (no FITS file needed) and return its id."""
    with project.db:
        cur = project.db.execute(
            "INSERT INTO frames (session_id, kind, path, jd, utc) VALUES (?, ?, ?, ?, ?)",
            (session_id, kind, f"subs/{session_id}/{jd:.6f}.fits", jd, utc),
        )
        return int(cur.lastrowid)


@pytest.fixture()
def three_frames(project, two_sessions):
    """Three frames across two sessions: (f1, f2, f3) with jd1 < jd3 < jd2."""
    s1, s2 = two_sessions
    f1 = add_frame_row(project, s1, JD1, UTC1)
    f2 = add_frame_row(project, s2, JD2, UTC2)
    f3 = add_frame_row(project, s1, JD3, UTC3)
    return f1, f2, f3


# --- add_measurement: append + returned record ----------------------------------------


def test_add_measurement_appends_row_and_returns_measurement(project, two_sessions, three_frames):
    s1, _s2 = two_sessions
    f1, _f2, _f3 = three_frames
    m = add_measurement(project, f1, 12.345, mag_err=0.012)
    assert isinstance(m, Measurement)
    assert m.id >= 1
    assert m.frame_id == f1
    assert m.session_id == s1  # joined from the frame
    assert m.jd == JD1  # default: the frame's jd
    assert m.utc == UTC1  # joined from the frame
    assert m.mag == 12.345
    assert m.mag_err == 0.012
    assert m.excluded is False
    # the db row
    row = project.db.execute(
        "SELECT frame_id, jd, mag, mag_err, excluded FROM measurements WHERE id = ?", (m.id,)
    ).fetchone()
    assert row == (f1, JD1, 12.345, 0.012, 0)


def test_add_measurement_jd_defaults_to_frame_jd(project, three_frames):
    f1, _f2, _f3 = three_frames
    m = add_measurement(project, f1, 10.0)
    assert m.jd == JD1
    row = project.db.execute("SELECT jd FROM measurements WHERE id = ?", (m.id,)).fetchone()
    assert row == (JD1,)


def test_add_measurement_explicit_jd_overrides_frame_jd(project, three_frames):
    f1, _f2, _f3 = three_frames
    m = add_measurement(project, f1, 10.0, jd=JD1 + 0.0001)  # mid-exposure correction
    assert m.jd == JD1 + 0.0001
    assert m.utc == UTC1  # utc still comes from the frame
    row = project.db.execute("SELECT jd FROM measurements WHERE id = ?", (m.id,)).fetchone()
    assert row == (JD1 + 0.0001,)


def test_add_measurement_mag_err_none_is_allowed(project, three_frames):
    f1, _f2, _f3 = three_frames
    m = add_measurement(project, f1, 11.0, mag_err=None)
    assert m.mag_err is None
    row = project.db.execute(
        "SELECT mag_err FROM measurements WHERE id = ?", (m.id,)
    ).fetchone()
    assert row == (None,)


def test_add_measurement_excluded_true_at_insert(project, three_frames):
    f1, _f2, _f3 = three_frames
    m = add_measurement(project, f1, 99.0, excluded=True)
    assert m.excluded is True
    row = project.db.execute(
        "SELECT excluded FROM measurements WHERE id = ?", (m.id,)
    ).fetchone()
    assert row == (1,)


def test_add_measurement_int_values_coerced_to_float(project, three_frames):
    f1, _f2, _f3 = three_frames
    m = add_measurement(project, f1, 12, jd=JD1, mag_err=0)
    assert m.mag == 12.0 and isinstance(m.mag, float)
    assert m.mag_err == 0.0 and isinstance(m.mag_err, float)


# --- add_measurement: failure paths ------------------------------------------------------


def test_add_measurement_unknown_frame_raises_clear_error(project, two_sessions):
    with pytest.raises(MeasurementError) as excinfo:
        add_measurement(project, 424242, 12.0)
    assert "424242" in str(excinfo.value)
    assert issubclass(MeasurementError, ProjectError)
    assert project.db.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] == 0


@pytest.mark.parametrize("bad_mag", [float("nan"), float("inf"), float("-inf")])
def test_add_measurement_rejects_non_finite_mag(project, three_frames, bad_mag):
    f1, _f2, _f3 = three_frames
    with pytest.raises(MeasurementError) as excinfo:
        add_measurement(project, f1, bad_mag)
    assert "mag" in str(excinfo.value)
    assert project.db.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] == 0


@pytest.mark.parametrize("bad_mag", ["ten", None, True, [12.0]])
def test_add_measurement_rejects_non_numeric_mag(project, three_frames, bad_mag):
    f1, _f2, _f3 = three_frames
    with pytest.raises(MeasurementError):
        add_measurement(project, f1, bad_mag)
    assert project.db.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] == 0


@pytest.mark.parametrize("bad_err", [float("nan"), float("inf"), -0.5, -1])
def test_add_measurement_rejects_bad_mag_err(project, three_frames, bad_err):
    f1, _f2, _f3 = three_frames
    with pytest.raises(MeasurementError) as excinfo:
        add_measurement(project, f1, 12.0, mag_err=bad_err)
    assert "mag_err" in str(excinfo.value)
    assert project.db.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] == 0


@pytest.mark.parametrize("bad_jd", [float("nan"), float("inf"), float("-inf")])
def test_add_measurement_rejects_non_finite_jd(project, three_frames, bad_jd):
    f1, _f2, _f3 = three_frames
    with pytest.raises(MeasurementError) as excinfo:
        add_measurement(project, f1, 12.0, jd=bad_jd)
    assert "jd" in str(excinfo.value)
    assert project.db.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"frame_id": "1"},
        {"frame_id": True},
        {"frame_id": 1.5},
        {"frame_id": None},
        {"excluded": "yes"},
        {"excluded": 1},
    ],
)
def test_add_measurement_bad_argument_types_raise_type_error(project, three_frames, kwargs):
    f1, _f2, _f3 = three_frames
    args = {"frame_id": f1, "mag": 12.0, "excluded": False}
    args.update(kwargs)
    with pytest.raises(TypeError):
        add_measurement(project, **args)
    assert project.db.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] == 0


# --- lightcurve: multi-session merge + ordering -------------------------------------------


def test_lightcurve_empty(project):
    assert lightcurve(project) == []
    assert lightcurve(project, include_excluded=True) == []


def test_lightcurve_merges_sessions_ordered_by_jd(project, two_sessions, three_frames):
    s1, s2 = two_sessions
    f1, f2, f3 = three_frames
    # appended OUT of chronological order on purpose
    m3 = add_measurement(project, f3, 12.3)  # night 1, late
    m2 = add_measurement(project, f2, 12.2)  # night 2
    m1 = add_measurement(project, f1, 12.1)  # night 1, early
    curve = lightcurve(project)
    assert [m.id for m in curve] == [m1.id, m3.id, m2.id]  # merged JD order
    assert [m.jd for m in curve] == [JD1, JD3, JD2]
    # session_id + utc joined from each point's frame
    assert [m.session_id for m in curve] == [s1, s1, s2]
    assert [m.utc for m in curve] == [UTC1, UTC3, UTC2]
    assert [m.mag for m in curve] == [12.1, 12.3, 12.2]
    assert all(isinstance(m, Measurement) for m in curve)


def test_lightcurve_ties_break_by_id(project, three_frames):
    f1, _f2, _f3 = three_frames
    first = add_measurement(project, f1, 12.0, jd=JD1)
    second = add_measurement(project, f1, 12.1, jd=JD1)  # same jd
    curve = lightcurve(project)
    assert [m.id for m in curve] == [first.id, second.id]


def test_lightcurve_filters_excluded_unless_included(project, three_frames):
    f1, f2, f3 = three_frames
    m1 = add_measurement(project, f1, 12.1)
    m2 = add_measurement(project, f2, 12.2, excluded=True)  # outlier
    m3 = add_measurement(project, f3, 12.3)
    assert [m.id for m in lightcurve(project)] == [m1.id, m3.id]
    assert [m.id for m in lightcurve(project, include_excluded=False)] == [m1.id, m3.id]
    assert [m.id for m in lightcurve(project, include_excluded=True)] == [m1.id, m3.id, m2.id]
    assert lightcurve(project, include_excluded=True)[2].excluded is True


def test_lightcurve_include_excluded_must_be_bool(project):
    with pytest.raises(TypeError):
        lightcurve(project, include_excluded="yes")  # ty: ignore[invalid-argument-type]


# --- set_excluded --------------------------------------------------------------------------


def test_set_excluded_flags_and_unflags(project, three_frames):
    f1, f2, _f3 = three_frames
    m1 = add_measurement(project, f1, 12.1)
    m2 = add_measurement(project, f2, 12.2)
    assert [m.id for m in lightcurve(project)] == [m1.id, m2.id]

    flagged = set_excluded(project, m2.id, True)
    assert isinstance(flagged, Measurement)
    assert flagged.id == m2.id and flagged.excluded is True
    assert flagged.mag == 12.2 and flagged.frame_id == f2  # full record returned
    row = project.db.execute(
        "SELECT excluded FROM measurements WHERE id = ?", (m2.id,)
    ).fetchone()
    assert row == (1,)
    assert [m.id for m in lightcurve(project)] == [m1.id]  # filtered out
    assert [m.id for m in lightcurve(project, include_excluded=True)] == [m1.id, m2.id]

    unflagged = set_excluded(project, m2.id, False)
    assert unflagged.excluded is False
    row = project.db.execute(
        "SELECT excluded FROM measurements WHERE id = ?", (m2.id,)
    ).fetchone()
    assert row == (0,)
    assert [m.id for m in lightcurve(project)] == [m1.id, m2.id]


def test_set_excluded_unknown_id_raises_clear_error(project, three_frames):
    f1, _f2, _f3 = three_frames
    add_measurement(project, f1, 12.0)
    with pytest.raises(MeasurementError) as excinfo:
        set_excluded(project, 987654, True)
    assert "987654" in str(excinfo.value)
    assert issubclass(MeasurementError, ProjectError)
    # nothing changed
    assert [m.excluded for m in lightcurve(project, include_excluded=True)] == [False]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"measurement_id": "1"},
        {"measurement_id": True},
        {"measurement_id": 1.5},
        {"excluded": "yes"},
        {"excluded": 0},
    ],
)
def test_set_excluded_bad_argument_types_raise_type_error(project, three_frames, kwargs):
    f1, _f2, _f3 = three_frames
    m = add_measurement(project, f1, 12.0)
    args = {"measurement_id": m.id, "excluded": True}
    args.update(kwargs)
    with pytest.raises(TypeError):
        set_excluded(project, **args)


# --- measurements_for_frame ------------------------------------------------------------------


def test_measurements_for_frame_returns_only_that_frames_points(project, three_frames):
    f1, f2, f3 = three_frames
    m1 = add_measurement(project, f1, 12.1)
    m2 = add_measurement(project, f2, 12.2)
    m3 = add_measurement(project, f3, 12.3)
    m1b = add_measurement(project, f1, 12.15, jd=JD1 + 0.001)  # same frame, later jd
    got = measurements_for_frame(project, f1)
    assert [m.id for m in got] == [m1.id, m1b.id]  # ordered by jd
    assert measurements_for_frame(project, f2) == [m2]
    assert measurements_for_frame(project, f3) == [m3]
    assert all(m.frame_id == f1 for m in got)


def test_measurements_for_frame_unknown_frame_is_empty_filter(project, three_frames):
    f1, _f2, _f3 = three_frames
    add_measurement(project, f1, 12.0)
    assert measurements_for_frame(project, 424242) == []


@pytest.mark.parametrize("bad_id", ["1", True, 1.5, None])
def test_measurements_for_frame_bad_id_raises_type_error(project, bad_id):
    with pytest.raises(TypeError):
        measurements_for_frame(project, bad_id)


# --- persistence across reopen ---------------------------------------------------------------


def test_measurements_persist_across_reopen(root, project, two_sessions, three_frames):
    s1, s2 = two_sessions
    f1, f2, f3 = three_frames
    m1 = add_measurement(project, f1, 12.1, mag_err=0.01)
    m2 = add_measurement(project, f2, 12.2, excluded=True)
    m3 = add_measurement(project, f3, 12.3, jd=JD3 + 0.0001, mag_err=0.03)
    project.close()

    with open_project(root / "m-42") as p:
        curve = lightcurve(p)
        assert curve == [m1, m3]  # full records survive the reopen, excluded filtered
        assert [m.id for m in lightcurve(p, include_excluded=True)] == [m1.id, m3.id, m2.id]
        assert curve[0].session_id == s1 and curve[0].utc == UTC1
        assert curve[1].session_id == s1 and curve[1].jd == JD3 + 0.0001
        # flag/unflag still works on the reopened project
        flagged = set_excluded(p, m1.id, True)
        assert flagged.excluded is True
        assert lightcurve(p) == [m3]
        # appending still works on the reopened project
        m4 = add_measurement(p, f2, 12.25)
        assert m4.session_id == s2 and m4.utc == UTC2
        # jd order: m1 (JD1) < m3 (JD3+0.0001) < m2 = m4 (JD2, tie by id)
        assert [m.id for m in lightcurve(p, include_excluded=True)] == [
            m1.id,
            m3.id,
            m2.id,
            m4.id,
        ]
        assert measurements_for_frame(p, f2) == [m2, m4]


# --- public API + module boundary --------------------------------------------------------------


def test_measurement_api_is_exported_from_package():
    from seestar_controller import store

    for name in (
        "Measurement",
        "MeasurementError",
        "add_measurement",
        "lightcurve",
        "set_excluded",
        "measurements_for_frame",
    ):
        assert name in store.__all__, name
        assert hasattr(store, name), name
    assert issubclass(store.MeasurementError, store.ProjectError)
    assert callable(store.add_measurement)
    assert callable(store.lightcurve)
    assert callable(store.set_excluded)
    assert callable(store.measurements_for_frame)
    assert isinstance(store.Measurement, type)
    # dataclass field order: id, frame_id, session_id, jd, utc, mag, mag_err, excluded
    assert [f.name for f in dataclasses.fields(store.Measurement)] == [
        "id",
        "frame_id",
        "session_id",
        "jd",
        "utc",
        "mag",
        "mag_err",
        "excluded",
    ]


def test_measurements_module_imports_no_gtk_and_no_link():
    code = (
        "import sys;"
        "import seestar_controller.store.measurements;"
        "assert 'gi' not in sys.modules, 'GTK imported';"
        "assert not any(m == 'seestar_controller.link' or m.startswith("
        "'seestar_controller.link.') for m in sys.modules), 'link imported';"
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout
