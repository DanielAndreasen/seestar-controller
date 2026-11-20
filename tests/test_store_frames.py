"""Project store: frame storage (sub-frames + stacks as FITS files + db rows).

Every test runs against a ``tmp_path`` project root, no network. Synthetic
FITS files are generated with astropy.io.fits (the same pattern the link mock
scope uses) carrying DATE-OBS / EXPTIME / GAIN / OBJECT headers.
"""

import io
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits as pyfits
from astropy.time import Time

from seestar_controller.store import (
    FITS_SUFFIX,
    KIND_STACK,
    KIND_SUB,
    KINDS,
    Frame,
    FrameStoreError,
    ProjectError,
    SessionNotFoundError,
    close_session,
    create_project,
    get_frame_path,
    list_frames,
    open_project,
    read_frame_bytes,
    start_session,
    store_frame,
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


T0 = datetime(2026, 11, 20, 22, 0, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(hours=1, minutes=30)
T2 = T0 + timedelta(hours=3)


@pytest.fixture()
def session(project):
    """One open session with a deterministic id."""
    return start_session(project, clock=frozen(T0))


def make_fits(*, date_obs=None, exptime=None, gain=None, obj=None, seed=0) -> bytes:
    """A small valid FITS file (astropy.io.fits) with the given header cards."""
    rng = np.random.default_rng(seed)
    data = rng.integers(100, 900, size=(16, 16)).astype(np.int16)
    hdu = pyfits.PrimaryHDU(data=data)
    if date_obs is not None:
        hdu.header["DATE-OBS"] = date_obs
    if exptime is not None:
        hdu.header["EXPTIME"] = float(exptime)
    if gain is not None:
        hdu.header["GAIN"] = gain
    if obj is not None:
        hdu.header["OBJECT"] = obj
    buf = io.BytesIO()
    pyfits.HDUList([hdu]).writeto(buf)
    return buf.getvalue()


def jd_of(date_obs: str) -> float:
    """The expected JD of a DATE-OBS string (astropy Time.jd)."""
    return float(Time(date_obs, format="isot", scale="utc").jd)


DATE1 = "2026-11-20T22:00:00.000"
DATE2 = "2026-11-20T22:00:10.000"
DATE3 = "2026-11-20T22:00:20.500"


# --- store_frame: sub + stack round-trip ------------------------------------------


def test_store_sub_writes_file_and_db_row(project, session):
    fits = make_fits(date_obs=DATE1, exptime=10.0, gain=50, obj="M 42")
    frame = store_frame(
        project, session, KIND_SUB, fits, meta={"exposure": 10.0, "gain": 50, "target": "M 42"}
    )
    assert isinstance(frame, Frame)
    assert frame.session_id == session
    assert frame.kind == KIND_SUB
    assert frame.id >= 1
    # jd from the header DATE-OBS, utc = ISO of the same instant
    assert frame.jd == jd_of(DATE1)
    assert frame.utc == "2026-11-20T22:00:00.000+00:00"
    assert datetime.fromisoformat(frame.utc).utcoffset() == timezone.utc.utcoffset(None)
    # metadata from meta
    assert frame.exposure == 10.0
    assert frame.gain == 50.0
    assert frame.target == "M 42"
    # path relative to the project root, under subs/<session>/, named from the jd
    assert frame.path == f"subs/{session}/{frame.jd:.6f}{FITS_SUFFIX}"
    path = project.path / frame.path
    assert path.is_file()
    assert path.read_bytes() == fits  # byte-identical
    # the db row
    row = project.db.execute(
        "SELECT session_id, kind, path, jd, utc, exposure, gain, target FROM frames WHERE id = ?",
        (frame.id,),
    ).fetchone()
    assert row == (session, "sub", frame.path, frame.jd, frame.utc, 10.0, 50.0, "M 42")
    # helpers
    assert get_frame_path(project, frame) == path
    assert read_frame_bytes(project, frame) == fits


def test_store_stack_goes_to_stacks_dir(project, session):
    fits = make_fits(date_obs=DATE2, exptime=300.0, gain=80, obj="M 42")
    frame = store_frame(project, session, KIND_STACK, fits)
    assert frame.kind == KIND_STACK
    assert frame.path == f"stacks/{session}/{frame.jd:.6f}{FITS_SUFFIX}"
    assert (project.path / frame.path).is_file()
    assert read_frame_bytes(project, frame) == fits
    row = project.db.execute("SELECT kind, path FROM frames WHERE id = ?", (frame.id,)).fetchone()
    assert row == ("stack", frame.path)


def test_store_creates_session_dir_lazily(project, session):
    assert not (project.path / "subs" / session).exists()
    store_frame(project, session, "sub", make_fits(date_obs=DATE1))
    assert (project.path / "subs" / session).is_dir()
    assert not (project.path / "stacks" / session).exists()


def test_store_into_closed_session_is_allowed(project, session):
    """Documented choice: frames may be added to a closed session (late downloads)."""
    close_session(project, session, clock=frozen(T1))
    frame = store_frame(project, session, "sub", make_fits(date_obs=DATE1))
    assert frame.session_id == session
    assert list_frames(project, session_id=session) == [frame]


# --- metadata: meta overrides, header fallback --------------------------------------


def test_meta_overrides_header_values(project, session):
    fits = make_fits(date_obs=DATE1, exptime=10.0, gain=50, obj="Header Target")
    frame = store_frame(
        project, session, "sub", fits, meta={"exposure": 20.0, "gain": 80, "target": "Meta Target"}
    )
    assert frame.exposure == 20.0
    assert frame.gain == 80.0
    assert frame.target == "Meta Target"


def test_header_values_are_the_fallback(project, session):
    fits = make_fits(date_obs=DATE1, exptime=10.0, gain=50, obj="M 42")
    for meta in (None, {}):
        frame = store_frame(project, session, "sub", fits, meta=meta)
        assert frame.exposure == 10.0
        assert frame.gain == 50.0
        assert frame.target == "M 42"


def test_partial_meta_overrides_only_given_keys(project, session):
    fits = make_fits(date_obs=DATE1, exptime=10.0, gain=50, obj="M 42")
    frame = store_frame(project, session, "sub", fits, meta={"gain": 99, "id": "ignored", "time": "ignored"})
    assert frame.exposure == 10.0  # from the header
    assert frame.gain == 99.0  # from meta
    assert frame.target == "M 42"  # from the header


def test_meta_target_mapping_uses_name(project, session):
    fits = make_fits(date_obs=DATE1)
    frame = store_frame(
        project, session, "sub", fits, meta={"target": {"name": "M 42", "ra": 5.59, "dec": -5.39}}
    )
    assert frame.target == "M 42"


def test_meta_target_mapping_without_name_is_json(project, session):
    fits = make_fits(date_obs=DATE1)
    frame = store_frame(project, session, "sub", fits, meta={"target": {"ra": 5.59, "dec": -5.39}})
    assert json.loads(frame.target) == {"ra": 5.59, "dec": -5.39}


def test_missing_metadata_is_none(project, session):
    frame = store_frame(project, session, "sub", make_fits(date_obs=DATE1))
    assert frame.exposure is None
    assert frame.gain is None
    assert frame.target is None
    row = project.db.execute(
        "SELECT exposure, gain, target FROM frames WHERE id = ?", (frame.id,)
    ).fetchone()
    assert row == (None, None, None)


@pytest.mark.parametrize("bad_meta", [{"exposure": "ten"}, {"gain": [1]}, {"exposure": float("nan")}])
def test_bad_meta_numbers_raise_value_error(project, session, bad_meta):
    with pytest.raises(ValueError):
        store_frame(project, session, "sub", make_fits(date_obs=DATE1), meta=bad_meta)
    assert list_frames(project) == []


# --- JD extraction: DATE-OBS, fallback to mtime --------------------------------------


def test_jd_from_date_obs_matches_astropy(project, session):
    for date_obs in (DATE1, DATE2, DATE3, "2026-11-21T05:30:00.123456"):
        frame = store_frame(project, session, "sub", make_fits(date_obs=date_obs))
        assert frame.jd == jd_of(date_obs)
        expected_utc = Time(date_obs, format="isot", scale="utc").to_datetime(timezone.utc)
        assert frame.utc == expected_utc.isoformat(timespec="milliseconds")


def test_jd_falls_back_to_file_mtime_without_date_obs(project, session):
    frame = store_frame(project, session, "sub", make_fits(exptime=10.0))
    path = get_frame_path(project, frame)
    mtime = path.stat().st_mtime
    expected_jd = float(Time(datetime.fromtimestamp(mtime, timezone.utc), scale="utc").jd)
    assert frame.jd == pytest.approx(expected_jd, abs=1e-9)
    expected_utc = datetime.fromtimestamp(mtime, timezone.utc).isoformat(timespec="milliseconds")
    assert frame.utc == expected_utc
    # the file name is derived from that jd too
    assert frame.path == f"subs/{session}/{frame.jd:.6f}{FITS_SUFFIX}"


@pytest.mark.parametrize("bad_date_obs", ["not-a-date", "2026-13-40T99:00:00", ""])
def test_jd_falls_back_to_mtime_on_invalid_date_obs(project, session, bad_date_obs):
    frame = store_frame(project, session, "sub", make_fits(date_obs=bad_date_obs))
    path = get_frame_path(project, frame)
    mtime = path.stat().st_mtime
    expected_jd = float(Time(datetime.fromtimestamp(mtime, timezone.utc), scale="utc").jd)
    assert frame.jd == pytest.approx(expected_jd, abs=1e-9)


# --- file naming + collisions -----------------------------------------------------------


def test_frame_file_name_is_jd_with_fixed_precision(project, session):
    frame = store_frame(project, session, "sub", make_fits(date_obs=DATE3))
    assert frame.path.endswith(f"{jd_of(DATE3):.6f}{FITS_SUFFIX}")


def test_same_jd_collision_gets_suffix(project, session):
    fits = make_fits(date_obs=DATE1)
    first = store_frame(project, session, "sub", fits)
    second = store_frame(project, session, "sub", fits, meta={"exposure": 30.0})
    assert first.path == f"subs/{session}/{first.jd:.6f}{FITS_SUFFIX}"
    assert second.path == f"subs/{session}/{first.jd:.6f}-2{FITS_SUFFIX}"
    assert first.id != second.id
    assert (project.path / first.path).is_file()
    assert (project.path / second.path).is_file()
    assert {f.path for f in list_frames(project)} == {first.path, second.path}


# --- list_frames: filters + ordering ------------------------------------------------------


@pytest.fixture()
def two_sessions(project):
    """Two closed sessions with deterministic ids."""
    s1 = start_session(project, clock=frozen(T0))
    close_session(project, s1, clock=frozen(T1))
    s2 = start_session(project, clock=frozen(T2))
    close_session(project, s2, clock=frozen(T2 + timedelta(hours=1)))
    return s1, s2


def test_list_frames_empty(project):
    assert list_frames(project) == []


def test_list_frames_ordered_by_jd_across_sessions_and_kinds(project, two_sessions):
    s1, s2 = two_sessions
    # stored out of chronological order on purpose
    late = store_frame(project, s1, "sub", make_fits(date_obs=DATE3))
    early = store_frame(project, s2, "sub", make_fits(date_obs=DATE1))
    mid_stack = store_frame(project, s1, "stack", make_fits(date_obs=DATE2))
    frames = list_frames(project)
    assert [f.id for f in frames] == [early.id, mid_stack.id, late.id]
    assert [f.jd for f in frames] == sorted(f.jd for f in frames)
    # per-session filter (still ordered by jd: the DATE2 stack before the DATE3 sub)
    assert [f.id for f in list_frames(project, session_id=s1)] == [mid_stack.id, late.id]
    assert [f.id for f in list_frames(project, session_id=s2)] == [early.id]
    # kind filter
    assert [f.id for f in list_frames(project, kind=KIND_SUB)] == [early.id, late.id]
    assert [f.id for f in list_frames(project, kind=KIND_STACK)] == [mid_stack.id]
    # combined
    assert [f.id for f in list_frames(project, session_id=s1, kind=KIND_STACK)] == [mid_stack.id]
    assert list_frames(project, session_id=s1, kind=KIND_SUB) == [late]
    # unknown session is a filter, not an error -> empty list
    assert list_frames(project, session_id="no-such-session") == []


def test_list_frames_returns_full_metadata(project, session):
    fits = make_fits(date_obs=DATE1, exptime=10.0, gain=50, obj="M 42")
    stored = store_frame(project, session, "sub", fits, meta={"target": "M 42"})
    (listed,) = list_frames(project)
    assert listed == stored  # frozen dataclass equality, all fields
    assert listed.exposure == 10.0 and listed.gain == 50.0 and listed.target == "M 42"


# --- failure paths: interrupted write, db failure, unknown session, bad input -------------


def test_interrupted_write_leaves_no_partial_file_and_no_row(project, session, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("rename failed")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        store_frame(project, session, "sub", make_fits(date_obs=DATE1))
    session_dir = project.path / "subs" / session
    assert session_dir.is_dir()
    assert list(session_dir.iterdir()) == []  # no partial file, no temp
    assert not list(project.path.rglob("*.tmp"))
    assert list_frames(project) == []  # no db row


def test_keyboard_interrupt_during_write_leaves_no_file_and_no_row(project, session, monkeypatch):
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "fsync", interrupt)
    with pytest.raises(KeyboardInterrupt):
        store_frame(project, session, "sub", make_fits(date_obs=DATE1))
    session_dir = project.path / "subs" / session
    assert list(session_dir.iterdir()) == []
    assert not list(project.path.rglob("*.tmp"))
    assert list_frames(project) == []


def test_db_insert_failure_removes_the_file(project, session):
    with project.db:
        project.db.execute(
            "CREATE TRIGGER no_frames BEFORE INSERT ON frames "
            "BEGIN SELECT RAISE(ABORT, 'boom'); END"
        )
    with pytest.raises(FrameStoreError) as excinfo:
        store_frame(project, session, "sub", make_fits(date_obs=DATE1))
    assert "frames row" in str(excinfo.value)
    assert issubclass(FrameStoreError, ProjectError)
    assert list_frames(project) == []  # the transaction rolled back
    session_dir = project.path / "subs" / session
    assert list(session_dir.iterdir()) == []  # no orphan file
    assert not list(project.path.rglob("*.tmp"))


def test_unknown_session_raises_clear_error_before_writing(project):
    with pytest.raises(SessionNotFoundError) as excinfo:
        store_frame(project, "no-such-session", "sub", make_fits(date_obs=DATE1))
    assert "no-such-session" in str(excinfo.value)
    assert issubclass(SessionNotFoundError, ProjectError)
    assert not (project.path / "subs" / "no-such-session").exists()  # nothing written
    assert list_frames(project) == []


@pytest.mark.parametrize("kind", ["bogus", "", None, 42])
def test_store_frame_invalid_kind_raises_value_error(project, session, kind):
    with pytest.raises(ValueError):
        store_frame(project, session, kind, make_fits(date_obs=DATE1))
    assert list_frames(project) == []


@pytest.mark.parametrize("kind", ["bogus", "", 42])
def test_list_frames_invalid_kind_raises_value_error(project, kind):
    with pytest.raises(ValueError):
        list_frames(project, kind=kind)  # None is the default = no filter


def test_non_fits_bytes_raise_frame_store_error(project, session):
    for garbage in (b"this is not a FITS file" * 20, b""):
        with pytest.raises(FrameStoreError):
            store_frame(project, session, "sub", garbage)
    assert list_frames(project) == []
    assert not (project.path / "subs" / session).exists() or not list(
        (project.path / "subs" / session).iterdir()
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"session_id": 42},
        {"fits_bytes": "not bytes"},
        {"fits_bytes": None},
        {"meta": 42},
        {"meta": ["exposure"]},
    ],
)
def test_bad_argument_types_raise(project, session, kwargs):
    args = dict(session_id=session, kind="sub", fits_bytes=make_fits(date_obs=DATE1), meta=None)
    args.update(kwargs)
    with pytest.raises(TypeError):
        store_frame(project, **args)
    assert list_frames(project) == []


def test_bytearray_fits_bytes_accepted(project, session):
    frame = store_frame(project, session, "sub", bytearray(make_fits(date_obs=DATE1)))
    assert read_frame_bytes(project, frame) == make_fits(date_obs=DATE1)


# --- helpers: path safety + missing file ---------------------------------------------------


def _bare_frame(path: str) -> Frame:
    return Frame(
        id=1,
        session_id="s",
        kind="sub",
        path=path,
        jd=2460000.5,
        utc="2026-01-01T00:00:00.000+00:00",
        exposure=None,
        gain=None,
        target=None,
    )


@pytest.mark.parametrize("bad_path", ["../evil.fits", "subs/../../evil.fits", "/etc/passwd"])
def test_get_frame_path_rejects_escaping_paths(project, bad_path):
    with pytest.raises(FrameStoreError):
        get_frame_path(project, _bare_frame(bad_path))


def test_read_frame_bytes_missing_file_raises(project, session):
    frame = store_frame(project, session, "sub", make_fits(date_obs=DATE1))
    get_frame_path(project, frame).unlink()
    with pytest.raises(FrameStoreError):
        read_frame_bytes(project, frame)


# --- persistence across reopen ---------------------------------------------------------------


def test_frames_persist_across_reopen(root, project, session):
    sub = store_frame(
        project, session, "sub", make_fits(date_obs=DATE1, exptime=10.0, gain=50, obj="M 42")
    )
    stack = store_frame(project, session, "stack", make_fits(date_obs=DATE2, exptime=300.0))
    sub_bytes = read_frame_bytes(project, sub)
    project.close()

    with open_project(root / "m-42") as p:
        frames = list_frames(p)
        assert frames == [sub, stack]  # full records survive the reopen
        assert list_frames(p, session_id=session, kind=KIND_SUB) == [sub]
        assert read_frame_bytes(p, sub) == sub_bytes
        assert get_frame_path(p, stack).is_file()
        # storing still works on the reopened project
        again = store_frame(p, session, "sub", make_fits(date_obs=DATE3))
        assert [f.id for f in list_frames(p)] == [sub.id, stack.id, again.id]


# --- public API + module boundary --------------------------------------------------------------


def test_frame_api_is_exported_from_package():
    import seestar_controller.store as store

    for name in (
        "Frame",
        "FrameStoreError",
        "KIND_SUB",
        "KIND_STACK",
        "KINDS",
        "FITS_SUFFIX",
        "store_frame",
        "list_frames",
        "get_frame_path",
        "read_frame_bytes",
        "normalize_kind",
    ):
        assert name in store.__all__, name
        assert hasattr(store, name), name
    assert issubclass(store.FrameStoreError, store.ProjectError)
    assert callable(store.store_frame)
    assert callable(store.list_frames)
    assert callable(store.get_frame_path)
    assert callable(store.read_frame_bytes)
    assert isinstance(store.Frame, type)
    assert store.KINDS == ("sub", "stack", "enhanced")
    assert store.normalize_kind("sub") == "sub"
    with pytest.raises(ValueError):
        store.normalize_kind("nope")


def test_frames_module_imports_no_gtk_and_no_link():
    code = (
        "import sys;"
        "import seestar_controller.store.frames;"
        "assert 'gi' not in sys.modules, 'GTK imported';"
        "assert not any(m == 'seestar_controller.link' or m.startswith("
        "'seestar_controller.link.') for m in sys.modules), 'link imported';"
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout
