"""Project store: CSV export — tmp_path, no network.

Every test runs against a ``tmp_path`` project root. Frames are inserted as
``frames`` db rows directly (CSV export only reads the db — no FITS files are
involved), sessions via ``start_session`` with a frozen clock. The round-trip
tests re-parse the written file with the stdlib ``csv`` module and compare
against the db values exactly (``repr`` float round-trip, NULL mag_err ->
empty field).
"""

import csv
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from seestar_controller.store import (
    CSV_FILE,
    CSV_HEADER,
    add_measurement,
    close_session,
    create_project,
    export_csv,
    lightcurve,
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
# f2 in session 2 (night 2), with f2 AFTER f1 and f3 in time.
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


# Ugly non-terminating floats on purpose: repr() must round-trip them exactly.
JD3_MID = JD3 + 0.1 + 0.2
MAG3 = 12.1 + 0.2
MAG_ERR3 = 0.1 + 0.2


@pytest.fixture()
def target(project):
    """Set the project config target (the placeholder shape from ticket 01)."""
    project.config["target"] = {"name": "M 42", "ra_deg": 83.82208, "dec_deg": -5.39114}
    project.save_config()
    return "M 42"


@pytest.fixture()
def three_measurements(project, three_frames):
    """Three measurements: m1 (night 1), m2 (night 2, EXCLUDED, NULL mag_err),
    m3 (night 1, ugly floats, explicit mid-exposure jd)."""
    f1, f2, f3 = three_frames
    m1 = add_measurement(project, f1, 12.1, mag_err=0.01)
    m2 = add_measurement(project, f2, 99.9, mag_err=None, excluded=True)
    m3 = add_measurement(project, f3, MAG3, jd=JD3_MID, mag_err=MAG_ERR3)
    return m1, m2, m3


def read_csv(path: Path) -> list[list[str]]:
    """Re-parse a CSV file with the stdlib csv module (standard tool)."""
    with path.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.reader(fh))


# --- export: destination + header -----------------------------------------------------


def test_export_writes_lightcurve_csv_at_project_root(project, three_measurements):
    out = export_csv(project)
    assert isinstance(out, Path)
    assert out == project.path / CSV_FILE
    assert out == project.path / "lightcurve.csv"
    assert out.is_file()


def test_export_header_is_exact(project, three_measurements, target):
    out = export_csv(project)
    raw = out.read_bytes()
    assert b"\r" not in raw  # "\n" line terminator only
    first_line = raw.split(b"\n")[0]
    assert first_line == b"jd,utc,mag,mag_err,session,target"
    assert ",".join(CSV_HEADER) == "jd,utc,mag,mag_err,session,target"
    rows = read_csv(out)
    assert rows[0] == ["jd", "utc", "mag", "mag_err", "session", "target"]
    assert rows[0] == list(CSV_HEADER)


def test_export_leaves_no_tmp_files(project, three_measurements):
    export_csv(project)
    assert not list(project.path.glob("*.tmp"))


# --- export: round-trip re-parse matches the db exactly ---------------------------------


def test_export_roundtrip_reparse_matches_db_exactly(project, two_sessions, three_frames, three_measurements, target):
    s1, _s2 = two_sessions
    _f1, _f2, _f3 = three_frames
    m1, _m2, m3 = three_measurements
    out = export_csv(project)
    rows = read_csv(out)
    assert rows[0] == list(CSV_HEADER)
    body = rows[1:]
    # default export omits the excluded m2 -> m1, m3 in jd order
    assert len(body) == 2
    # the db rows (the source of truth), same query as lightcurve()
    db_rows = project.db.execute(
        "SELECT m.jd, f.utc, m.mag, m.mag_err, f.session_id FROM measurements m "
        "JOIN frames f ON f.id = m.frame_id WHERE m.excluded = 0 ORDER BY m.jd, m.id"
    ).fetchall()
    assert len(db_rows) == 2
    for csv_row, db_row in zip(body, db_rows):
        jd_csv, utc_csv, mag_csv, mag_err_csv, session_csv, target_csv = csv_row
        # jd: repr(float) round-trips EXACTLY through float()
        assert float(jd_csv) == db_row[0]
        assert jd_csv == repr(db_row[0])  # written as repr(float)
        assert utc_csv == db_row[1]
        assert float(mag_csv) == db_row[2] and mag_csv == repr(db_row[2])
        # mag_err NULL -> empty field; otherwise exact float round-trip
        if db_row[3] is None:
            assert mag_err_csv == ""
        else:
            assert float(mag_err_csv) == db_row[3] and mag_err_csv == repr(db_row[3])
        assert session_csv == db_row[4]
        assert target_csv == "M 42"
    # spot-check the exact expected values (incl. the ugly floats)
    assert body[0] == [repr(JD1), UTC1, repr(12.1), repr(0.01), s1, "M 42"]
    assert body[1] == [repr(JD3_MID), UTC3, repr(MAG3), repr(MAG_ERR3), s1, "M 42"]
    assert float(body[1][0]) == JD3_MID  # 0.1+0.2 round-trips exactly
    assert float(body[1][2]) == MAG3
    assert [m.id for m in lightcurve(project)] == [m1.id, m3.id]
    assert [float(r[0]) for r in body] == [m.jd for m in lightcurve(project)]


def test_export_roundtrip_with_include_excluded_matches_db(project, three_frames, three_measurements, target):
    _f1, _f2, _f3 = three_frames
    _m1, m2, _m3 = three_measurements
    out = export_csv(project, include_excluded=True)
    body = read_csv(out)[1:]
    assert len(body) == 3  # m1, m3, m2 in jd order
    db_rows = project.db.execute(
        "SELECT m.jd, f.utc, m.mag, m.mag_err, f.session_id FROM measurements m "
        "JOIN frames f ON f.id = m.frame_id ORDER BY m.jd, m.id"
    ).fetchall()
    for csv_row, db_row in zip(body, db_rows):
        assert float(csv_row[0]) == db_row[0]
        assert csv_row[1] == db_row[1]
        assert float(csv_row[2]) == db_row[2]
        assert (csv_row[3] == "") == (db_row[3] is None)
        if db_row[3] is not None:
            assert float(csv_row[3]) == db_row[3]
        assert csv_row[4] == db_row[4]
        assert csv_row[5] == "M 42"
    # the excluded point m2 is the last row (largest jd), NULL mag_err -> ""
    assert body[2][3] == ""
    assert float(body[2][0]) == m2.jd
    assert body[2][4] == m2.session_id


# --- export: exclusion policy -----------------------------------------------------------


def test_export_omits_excluded_by_default(project, three_measurements, target):
    m1, m2, m3 = three_measurements
    body = read_csv(export_csv(project))[1:]
    assert [float(r[0]) for r in body] == [m1.jd, m3.jd]  # m2 omitted
    body_explicit = read_csv(export_csv(project, include_excluded=False))[1:]
    assert body_explicit == body
    body_all = read_csv(export_csv(project, include_excluded=True))[1:]
    assert [float(r[0]) for r in body_all] == [m1.jd, m3.jd, m2.jd]  # m2 present


def test_export_reflects_exclusion_flags_set_afterwards(project, three_measurements, target):
    m1, m2, m3 = three_measurements
    assert len(read_csv(export_csv(project))[1:]) == 2
    set_excluded(project, m3.id, True)  # flag another outlier
    body = read_csv(export_csv(project))[1:]
    assert [float(r[0]) for r in body] == [m1.jd]  # only m1 left
    body_all = read_csv(export_csv(project, include_excluded=True))[1:]
    assert [float(r[0]) for r in body_all] == [m1.jd, m3.jd, m2.jd]


def test_export_include_excluded_must_be_bool(project):
    with pytest.raises(TypeError):
        export_csv(project, include_excluded="yes")  # ty: ignore[invalid-argument-type]


# --- export: empty lightcurve ------------------------------------------------------------


def test_export_empty_lightcurve_writes_header_only(project, target):
    out = export_csv(project)
    text = out.read_text(encoding="utf-8")
    assert text == "jd,utc,mag,mag_err,session,target\n"
    rows = read_csv(out)
    assert rows == [list(CSV_HEADER)]


# --- export: atomic write -----------------------------------------------------------------


def test_export_replace_failure_keeps_previous_csv_and_cleans_tmp(project, three_measurements, target, monkeypatch):
    out = export_csv(project)
    original = out.read_bytes()

    def boom(*args, **kwargs):
        raise OSError("rename failed")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        export_csv(project)
    assert out.read_bytes() == original  # previous CSV untouched
    assert not list(project.path.glob("*.tmp"))  # temp cleaned up


def test_export_interrupt_leaves_no_partial_file_and_no_tmp(project, three_measurements, target, monkeypatch):
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "fsync", interrupt)
    with pytest.raises(KeyboardInterrupt):
        export_csv(project)
    assert not (project.path / CSV_FILE).exists()  # no partial CSV
    assert not list(project.path.glob("*.tmp"))


# --- export: custom path + target ---------------------------------------------------------


def test_export_custom_path(project, three_measurements, target, tmp_path):
    custom = tmp_path / "elsewhere" / "curve.csv"
    out = export_csv(project, path=custom)
    assert out == custom
    assert custom.is_file()  # parent dir created
    assert not (project.path / CSV_FILE).exists()  # nothing at the default spot
    rows = read_csv(custom)
    assert rows[0] == list(CSV_HEADER)
    assert len(rows) == 3  # header + 2 non-excluded points
    # a plain string path works too
    custom2 = tmp_path / "curve2.csv"
    out2 = export_csv(project, path=str(custom2))
    assert out2 == custom2 and custom2.is_file()


def test_export_path_bad_type_raises_type_error(project):
    with pytest.raises(TypeError):
        export_csv(project, path=123)  # ty: ignore[invalid-argument-type]


def test_export_target_empty_when_unset(project, three_measurements):
    # no target in the config (the creation-time placeholder is None)
    assert project.config["target"] is None
    body = read_csv(export_csv(project))[1:]
    assert len(body) == 2
    assert all(row[5] == "" for row in body)  # target column empty


def test_export_target_name_with_comma_is_quoted_and_roundtrips(project, three_measurements):
    project.config["target"] = {"name": "M 42, the Orion Nebula"}
    project.save_config()
    out = export_csv(project)
    raw = out.read_text(encoding="utf-8")
    assert '"M 42, the Orion Nebula"' in raw  # csv-quoted
    body = read_csv(out)[1:]
    assert all(row[5] == "M 42, the Orion Nebula" for row in body)  # exact re-parse


# --- export: persistence across reopen ------------------------------------------------------


def test_export_after_reopen_matches(root, project, three_measurements, target):
    before = export_csv(project).read_bytes()
    project.close()
    with open_project(root / "m-42") as p:
        out = export_csv(p)
        assert out == p.path / CSV_FILE
        assert out.read_bytes() == before  # same content after reopen


# --- public API + module boundary -------------------------------------------------------------


def test_csv_api_is_exported_from_package():
    from seestar_controller import store

    for name in ("CSV_FILE", "CSV_HEADER", "export_csv"):
        assert name in store.__all__, name
        assert hasattr(store, name), name
    assert store.CSV_FILE == "lightcurve.csv"
    assert store.CSV_HEADER == ("jd", "utc", "mag", "mag_err", "session", "target")
    assert callable(store.export_csv)


def test_csv_export_module_imports_no_gtk_and_no_link():
    code = (
        "import sys;"
        "import seestar_controller.store.csv_export;"
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
