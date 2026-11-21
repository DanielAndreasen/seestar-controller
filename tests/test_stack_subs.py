"""Tests for :mod:`seestar_controller.stacking` — the pure "Stack subs"
core of sub-frames issue 03 (``.scratch/sub-frames/issues/03-stack-subs.md``):
median-combining one session's sub-frames into a ``kind='stack'`` FITS
written back into the project, so all stack-based views work on the result.

Pure tests: no ``gi``, no network (a test monkeypatches the socket module),
synthetic tmp projects only. The package boundary (no gi/link/ui/enhance,
lazy astropy) is enforced separately in tests/test_stacking_boundary.py.
"""

import io
import socket
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from astropy.io import fits as pyfits

from seestar_controller.stacking import (
    SkippedSub,
    StackSubsError,
    StackSubsResult,
    median_combine,
    stack_session_subs,
)
from seestar_controller.store import (
    KIND_STACK,
    KIND_SUB,
    close_session,
    create_project,
    get_frame_path,
    list_frames,
    list_sessions,
    read_frame_bytes,
    start_session,
    store_frame,
)
from seestar_controller.store.errors import SessionNotFoundError

SIZE = 32
T0 = datetime(2026, 11, 20, 22, 0, 0, tzinfo=UTC)


def date_obs_str(i: int, seconds: int = 10) -> str:
    """The DATE-OBS of frame ``i``: T0 + i*seconds, naive ISOT (UTC)."""
    instant = T0 + timedelta(seconds=seconds * i)
    return instant.isoformat(timespec="milliseconds").replace("+00:00", "")


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


def flat_data(value: float) -> np.ndarray:
    """A flat SIZE x SIZE float32 frame with every pixel at ``value``."""
    return np.full((SIZE, SIZE), value, dtype=np.float32)


@pytest.fixture()
def root(tmp_path):
    """A tmp project root."""
    r = tmp_path / "projects"
    r.mkdir()
    return r


@pytest.fixture()
def project(root):
    """A synthetic project (closed on teardown)."""
    p = create_project("M 42 night", root=root)
    yield p
    p.close()


@pytest.fixture()
def session(project):
    """One open session in the project (closed on teardown)."""
    sid = start_session(project, started_at=T0)
    yield sid
    for s in list_sessions(project):
        if s.id == sid and s.ended_at is None:
            close_session(project, sid)


def _store_sub(project, sid, data, i, seconds=10):
    return store_frame(
        project, sid, KIND_SUB, fits_bytes(data, date_obs=date_obs_str(i, seconds=seconds))
    )


def _read_stack(project, frame):
    """Read the stored stack frame back: (data, header)."""
    raw = read_frame_bytes(project, frame)
    with pyfits.open(io.BytesIO(raw), memmap=False) as hdul:
        return hdul[0].data, hdul[0].header


# --- stack_session_subs: the happy path -------------------------------------------------


def test_stack_session_subs_median_combines_and_writes_a_stack_frame(project, session):
    subs = [_store_sub(project, session, flat_data(v), i) for i, v in enumerate((1.0, 2.0, 3.0))]

    result = stack_session_subs(project, session)

    assert isinstance(result, StackSubsResult)
    assert result.n_subs == 3
    assert result.skipped == ()
    frame = result.frame
    assert frame.kind == KIND_STACK
    assert frame.session_id == session
    # listed as a stack of the session, stored under stacks/<session>/
    stacks = list_frames(project, session_id=session, kind=KIND_STACK)
    assert [f.id for f in stacks] == [frame.id]
    path = get_frame_path(project, frame)
    assert path.exists() and path.stat().st_size > 0
    assert path.parent == project.path / "stacks" / session
    # the combine: every pixel is the median (2.0) of the three subs
    data, header = _read_stack(project, frame)
    assert data.shape == (SIZE, SIZE)
    assert np.allclose(data, 2.0)
    # header: inherited cards + DATE-OBS = median jd + HISTORY of the combine
    assert header["OBJECT"] == "M 42"
    assert float(header["EXPTIME"]) == 10.0
    assert "DATE-OBS" in header
    assert "median-combined 3 subs" in str(header["HISTORY"])
    expected_jd = float(np.median([f.jd for f in subs]))
    assert frame.jd == pytest.approx(expected_jd, abs=1e-6)
    obs = datetime.fromisoformat(str(header["DATE-OBS"])).replace(tzinfo=UTC)
    obs_jd = 2440587.5 + obs.timestamp() / 86400.0
    assert obs_jd == pytest.approx(frame.jd, abs=1e-6)
    # the Enhance viewer's stack list picks the new stack up automatically
    from seestar_controller.enhance.fits_io import list_project_stacks

    infos = list_project_stacks(project)
    assert [info.frame_id for info in infos] == [frame.id]
    assert infos[0].path == path


def test_stack_session_subs_even_count_averages_the_middle_two(project, session):
    _store_sub(project, session, flat_data(1.0), 0)
    _store_sub(project, session, flat_data(4.0), 1)

    result = stack_session_subs(project, session)

    data, _header = _read_stack(project, result.frame)
    assert np.allclose(data, 2.5)


def test_stack_session_subs_rgb_subs_combine_per_channel(project, session):
    rgb = np.zeros((SIZE, SIZE, 3), dtype=np.float32)
    for i, v in enumerate((1.0, 2.0, 3.0)):
        rgb[..., :] = v
        _store_sub(project, session, rgb.copy(), i)

    result = stack_session_subs(project, session)

    data, _header = _read_stack(project, result.frame)
    assert data.shape == (SIZE, SIZE, 3)
    assert np.allclose(data, 2.0)


def test_stack_session_subs_rerun_appends_a_new_stack_frame(project, session):
    _store_sub(project, session, flat_data(1.0), 0)
    _store_sub(project, session, flat_data(2.0), 1)

    first = stack_session_subs(project, session)
    second = stack_session_subs(project, session)

    assert first.frame.id != second.frame.id
    stacks = list_frames(project, session_id=session, kind=KIND_STACK)
    assert [f.id for f in stacks] == [first.frame.id, second.frame.id]


# --- stack_session_subs: per-frame failures are skipped, never crash --------------------


def test_unreadable_sub_is_skipped(project, session):
    _store_sub(project, session, flat_data(1.0), 0)
    missing = _store_sub(project, session, flat_data(99.0), 1)
    _store_sub(project, session, flat_data(3.0), 2)
    get_frame_path(project, missing).unlink()

    result = stack_session_subs(project, session)

    assert result.n_subs == 2
    assert len(result.skipped) == 1
    skipped = result.skipped[0]
    assert isinstance(skipped, SkippedSub)
    assert skipped.frame_id == missing.id
    assert skipped.reason
    data, header = _read_stack(project, result.frame)
    assert np.allclose(data, 2.0)  # median of 1.0 and 3.0
    assert "skipped 1" in str(header["HISTORY"])


def test_garbage_sub_is_skipped(project, session):
    _store_sub(project, session, flat_data(1.0), 0)
    garbage = _store_sub(project, session, flat_data(99.0), 1)
    _store_sub(project, session, flat_data(3.0), 2)
    get_frame_path(project, garbage).write_bytes(b"this is not a FITS file")

    result = stack_session_subs(project, session)

    assert result.n_subs == 2
    assert [s.frame_id for s in result.skipped] == [garbage.id]
    assert "not a readable FITS" in result.skipped[0].reason
    data, _header = _read_stack(project, result.frame)
    assert np.allclose(data, 2.0)


def test_shape_mismatched_sub_is_skipped(project, session):
    _store_sub(project, session, flat_data(1.0), 0)
    odd = _store_sub(project, session, np.zeros((SIZE + 1, SIZE), dtype=np.float32), 1)
    _store_sub(project, session, flat_data(3.0), 2)

    result = stack_session_subs(project, session)

    assert result.n_subs == 2
    assert [s.frame_id for s in result.skipped] == [odd.id]
    assert "shape" in result.skipped[0].reason
    data, _header = _read_stack(project, result.frame)
    assert data.shape == (SIZE, SIZE)
    assert np.allclose(data, 2.0)


# --- stack_session_subs: error cases -----------------------------------------------------


def test_no_subs_raises_stack_subs_error(project, session):
    with pytest.raises(StackSubsError, match="no subs"):
        stack_session_subs(project, session)


def test_session_with_only_a_stack_raises_stack_subs_error(project, session):
    store_frame(project, session, KIND_STACK, fits_bytes(flat_data(1.0), date_obs=date_obs_str(0)))
    with pytest.raises(StackSubsError, match="no subs"):
        stack_session_subs(project, session)


def test_all_subs_unreadable_raises_stack_subs_error(project, session):
    subs = [_store_sub(project, session, flat_data(float(i)), i) for i in range(2)]
    for sub in subs:
        get_frame_path(project, sub).unlink()
    with pytest.raises(StackSubsError, match="no readable sub"):
        stack_session_subs(project, session)


def test_unknown_session_raises_session_not_found(project):
    with pytest.raises(SessionNotFoundError):
        stack_session_subs(project, "20261120T999999999999Z")


def test_type_errors(project, session):
    with pytest.raises(TypeError, match="Project"):
        stack_session_subs(None, session)
    with pytest.raises(TypeError, match="session_id"):
        stack_session_subs(project, 123)


# --- median_combine: the pure math --------------------------------------------------------


def test_median_combine_exact_median_and_dtype():
    arrs = [np.full((4, 4), v, dtype=np.float32) for v in (1.0, 2.0, 3.0)]
    combined = median_combine(arrs)
    assert combined.shape == (4, 4)
    assert combined.dtype == np.float32
    assert np.allclose(combined, 2.0)


def test_median_combine_even_count_averages_middle_two():
    combined = median_combine([np.full((3, 3), 1.0), np.full((3, 3), 2.0)])
    assert np.allclose(combined, 1.5)


def test_median_combine_3d_arrays():
    arrs = [np.full((4, 4, 3), v) for v in (1.0, 5.0, 9.0)]
    combined = median_combine(arrs)
    assert combined.shape == (4, 4, 3)
    assert np.allclose(combined, 5.0)


def test_median_combine_empty_raises():
    with pytest.raises(StackSubsError):
        median_combine([])


def test_median_combine_shape_mismatch_raises():
    with pytest.raises(StackSubsError, match="shape mismatch"):
        median_combine([np.zeros((4, 4)), np.zeros((5, 5))])


def test_median_combine_type_errors():
    with pytest.raises(TypeError):
        median_combine(5)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError):
        median_combine("abc")
    with pytest.raises(TypeError, match="numpy arrays"):
        median_combine([np.zeros(3), [1, 2, 3]])


# --- no network ---------------------------------------------------------------------------


def test_stack_session_subs_never_touches_the_network(project, session, monkeypatch):
    def _no_network(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", _no_network)
    monkeypatch.setattr(socket, "create_connection", _no_network)
    monkeypatch.setattr(socket, "getaddrinfo", _no_network)

    _store_sub(project, session, flat_data(1.0), 0)
    _store_sub(project, session, flat_data(2.0), 1)
    result = stack_session_subs(project, session)
    assert result.n_subs == 2
    data, _header = _read_stack(project, result.frame)
    assert np.allclose(data, 1.5)
