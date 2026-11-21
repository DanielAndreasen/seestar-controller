"""Project store: per-frame quality records — tmp_path, no network.

Covers :mod:`seestar_controller.store.quality` (frame-quality ticket 01,
schema v4): ``save_frame_quality`` (upsert in one transaction, frame-exists
check, validation), ``frame_quality_for_frame``, ``session_quality`` (the
per-session table joined with frames + the summary: median FWHM, % rejected),
cascade delete with the frame, corrupt-row handling, and the public exports.
"""

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from seestar_controller import store
from seestar_controller.store import (
    KIND_STACK,
    KIND_SUB,
    QualityError,
    SessionNotFoundError,
    close_session,
    create_project,
    frame_quality_for_frame,
    save_frame_quality,
    session_quality,
    start_session,
    store_frame,
)
from seestar_controller.store.quality import FrameQuality, SessionQuality


@pytest.fixture()
def root(tmp_path):
    """A fresh project root."""
    return tmp_path / "projects"


@pytest.fixture()
def project(root):
    """An open project (closed at teardown)."""
    p = create_project("M 42", root=root)
    yield p
    p.close()


def frozen(dt: datetime):
    return lambda: dt


T0 = datetime(2026, 11, 20, 22, 0, 0, tzinfo=UTC)


def _fits_bytes(date_obs: str) -> bytes:
    """A minimal valid FITS file (the quality tests never read the pixels)."""
    import io

    import numpy as np
    from astropy.io import fits as pyfits

    hdu = pyfits.PrimaryHDU(data=np.zeros((8, 8), dtype=np.float32))
    hdu.header["DATE-OBS"] = date_obs
    buf = io.BytesIO()
    pyfits.HDUList([hdu]).writeto(buf)
    return buf.getvalue()


def _add_sub(project, session_id: str, i: int):
    """Store one sub-frame with DATE-OBS = T0 + i*10s and return its Frame."""
    instant = T0 + timedelta(seconds=10 * i)
    return store_frame(
        project,
        session_id,
        KIND_SUB,
        _fits_bytes(instant.isoformat(timespec="milliseconds").replace("+00:00", "")),
    )


# --- save_frame_quality ---------------------------------------------------------------


def test_save_and_load_frame_quality_roundtrip(project):
    sid = start_session(project, clock=frozen(T0))
    frame = _add_sub(project, sid, 0)
    q = save_frame_quality(
        project,
        frame.id,
        fwhm=3.55,
        bg_median=100.2,
        bg_std=5.01,
        star_count=10,
        flagged=True,
        flag_reasons=("fwhm", "bg_std"),
        scored_at="2026-11-20T23:00:00.000+00:00",
    )
    assert isinstance(q, FrameQuality)
    assert q.frame_id == frame.id and q.session_id == sid
    assert q.fwhm == 3.55 and q.bg_median == 100.2 and q.bg_std == 5.01
    assert q.star_count == 10 and q.flagged is True
    assert q.flag_reasons == ("fwhm", "bg_std")
    assert q.scored_at == "2026-11-20T23:00:00.000+00:00"
    # read back
    loaded = frame_quality_for_frame(project, frame.id)
    assert loaded == q
    # defaults: not flagged, no reasons, scored_at = now (a UTC ISO string)
    q2 = save_frame_quality(
        project, frame.id, fwhm=None, bg_median=1.0, bg_std=0.5, star_count=0
    )
    assert q2.flagged is False and q2.flag_reasons == () and q2.fwhm is None
    assert q2.scored_at.endswith("+00:00")
    assert frame_quality_for_frame(project, frame.id) == q2  # upsert replaced it


def test_save_frame_quality_unknown_frame_raises(project):
    with pytest.raises(QualityError, match="no frame 999"):
        save_frame_quality(
            project, 999, fwhm=3.0, bg_median=1.0, bg_std=0.5, star_count=1
        )
    assert frame_quality_for_frame(project, 999) is None  # filter, not an error


@pytest.mark.parametrize("frame_id", [True, 1.5, "3", None])
def test_save_frame_quality_bad_frame_id_type_raises(project, frame_id):
    with pytest.raises(TypeError, match="frame_id"):
        save_frame_quality(
            project, frame_id, fwhm=3.0, bg_median=1.0, bg_std=0.5, star_count=1
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"fwhm": float("nan")},
        {"fwhm": float("inf")},
        {"fwhm": "x"},
        {"bg_median": float("inf")},
        {"bg_std": None},
        {"star_count": -1},
        {"star_count": 2.5},
        {"star_count": True},
        {"flagged": 1},
        {"flag_reasons": "fwhm"},
        {"flag_reasons": ("fwhm", 3)},
        {"scored_at": ""},
        {"scored_at": 12},
    ],
)
def test_save_frame_quality_invalid_values_raise_before_writing(project, kwargs):
    sid = start_session(project, clock=frozen(T0))
    frame = _add_sub(project, sid, 0)
    base = {"fwhm": 3.0, "bg_median": 1.0, "bg_std": 0.5, "star_count": 1}
    base.update(kwargs)
    with pytest.raises((QualityError, TypeError)):
        save_frame_quality(project, frame.id, **base)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    assert frame_quality_for_frame(project, frame.id) is None  # nothing written


def test_save_frame_quality_runs_in_one_transaction(project):
    """A failure inside the transaction (unknown frame) leaves no row."""
    sid = start_session(project, clock=frozen(T0))
    _add_sub(project, sid, 0)
    with pytest.raises(QualityError):
        save_frame_quality(
            project, 4242, fwhm=3.0, bg_median=1.0, bg_std=0.5, star_count=1
        )
    rows = project.db.execute("SELECT COUNT(*) FROM frame_quality").fetchone()
    assert rows[0] == 0


# --- session_quality --------------------------------------------------------------------


def test_session_quality_table_summary_and_ordering(project):
    sid = start_session(project, clock=frozen(T0))
    frames = [_add_sub(project, sid, i) for i in range(4)]
    # store out of jd order on purpose: the query orders by jd
    save_frame_quality(project, frames[2].id, fwhm=3.5, bg_median=100.0, bg_std=5.0, star_count=10)
    save_frame_quality(project, frames[0].id, fwhm=3.6, bg_median=101.0, bg_std=5.1, star_count=11)
    save_frame_quality(project, frames[3].id, fwhm=None, bg_median=900.0, bg_std=40.0, star_count=2, flagged=True, flag_reasons=("bg_median", "star_count"))
    save_frame_quality(project, frames[1].id, fwhm=3.4, bg_median=100.5, bg_std=4.9, star_count=10)

    q = session_quality(project, sid)
    assert isinstance(q, SessionQuality)
    assert q.session_id == sid
    assert [f.frame_id for f in q.frames] == [f.id for f in frames]  # jd order
    assert q.frame_count == 4
    assert q.flagged_count == 1 and q.flagged[0].frame_id == frames[3].id
    assert q.rejected_percent == pytest.approx(25.0)
    assert q.median_fwhm == pytest.approx(3.5)  # median of 3.6, 3.4, 3.5 (None skipped)
    assert q.median_bg_median == pytest.approx(100.75)
    assert q.median_bg_std == pytest.approx(5.05)  # median of 5.1, 4.9, 5.0, 40.0
    assert q.median_star_count == pytest.approx(10.0)
    # every record carries the joined session id
    assert all(f.session_id == sid for f in q.frames)


def test_session_quality_empty_session_and_unknown_session(project):
    sid = start_session(project, clock=frozen(T0))
    q = session_quality(project, sid)
    assert q.frames == () and q.frame_count == 0
    assert q.flagged_count == 0 and q.rejected_percent == 0.0
    assert q.median_fwhm is None and q.median_bg_median is None
    with pytest.raises(SessionNotFoundError, match="no session"):
        session_quality(project, "no-such-session")
    with pytest.raises(TypeError, match="session_id"):
        session_quality(project, 123)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


def test_session_quality_only_its_own_frames(project):
    sid1 = start_session(project, clock=frozen(T0))
    f1 = _add_sub(project, sid1, 0)
    save_frame_quality(project, f1.id, fwhm=3.0, bg_median=1.0, bg_std=0.5, star_count=1)
    # second session: close the first, start another
    close_session(project, sid1, clock=frozen(T0))
    sid2 = start_session(project, clock=frozen(T0))
    f2 = _add_sub(project, sid2, 0)
    save_frame_quality(project, f2.id, fwhm=9.0, bg_median=2.0, bg_std=1.5, star_count=3, flagged=True)
    q1 = session_quality(project, sid1)
    q2 = session_quality(project, sid2)
    assert [f.frame_id for f in q1.frames] == [f1.id]
    assert [f.frame_id for f in q2.frames] == [f2.id]
    assert q1.flagged_count == 0 and q2.flagged_count == 1


def test_session_quality_ignores_stacks_and_other_kinds(project):
    sid = start_session(project, clock=frozen(T0))
    sub = _add_sub(project, sid, 0)
    stack = store_frame(
        project, sid, KIND_STACK, _fits_bytes("2026-11-20T22:05:00.000")
    )
    save_frame_quality(project, sub.id, fwhm=3.0, bg_median=1.0, bg_std=0.5, star_count=1)
    save_frame_quality(project, stack.id, fwhm=9.0, bg_median=2.0, bg_std=1.5, star_count=3)
    q = session_quality(project, sid)
    # the query returns all quality records of the session's frames; the
    # SCORING (frame_quality.score_session) only ever scores subs — here both
    # rows exist because both were saved explicitly
    assert {f.frame_id for f in q.frames} == {sub.id, stack.id}


def test_quality_record_cascade_deleted_with_frame(project):
    sid = start_session(project, clock=frozen(T0))
    frame = _add_sub(project, sid, 0)
    save_frame_quality(project, frame.id, fwhm=3.0, bg_median=1.0, bg_std=0.5, star_count=1)
    assert frame_quality_for_frame(project, frame.id) is not None
    with project.db:
        project.db.execute("DELETE FROM frames WHERE id = ?", (frame.id,))
    assert frame_quality_for_frame(project, frame.id) is None
    assert session_quality(project, sid).frames == ()


def test_corrupt_flag_reasons_row_raises_quality_error(project):
    sid = start_session(project, clock=frozen(T0))
    frame = _add_sub(project, sid, 0)
    save_frame_quality(project, frame.id, fwhm=3.0, bg_median=1.0, bg_std=0.5, star_count=1)
    with project.db:
        project.db.execute(
            "UPDATE frame_quality SET flag_reasons = ? WHERE frame_id = ?",
            ("{not json", frame.id),
        )
    with pytest.raises(QualityError, match="flag_reasons"):
        frame_quality_for_frame(project, frame.id)
    with project.db:
        project.db.execute(
            "UPDATE frame_quality SET flag_reasons = ? WHERE frame_id = ?",
            (json.dumps({"a": 1}), frame.id),
        )
    with pytest.raises(QualityError, match="flag_reasons"):
        session_quality(project, sid)


def test_frame_quality_table_schema_constraints(project):
    sid = start_session(project, clock=frozen(T0))
    frame = _add_sub(project, sid, 0)
    with project.db:
        with pytest.raises(sqlite3.IntegrityError):  # star_count < 0
            project.db.execute(
                "INSERT INTO frame_quality (frame_id, bg_median, bg_std, star_count, flagged, flag_reasons, scored_at) "
                "VALUES (?, 1.0, 0.5, -1, 0, '[]', 't')",
                (frame.id,),
            )
        with pytest.raises(sqlite3.IntegrityError):  # flagged not 0/1
            project.db.execute(
                "INSERT INTO frame_quality (frame_id, bg_median, bg_std, star_count, flagged, flag_reasons, scored_at) "
                "VALUES (?, 1.0, 0.5, 1, 2, '[]', 't')",
                (frame.id,),
            )
        with pytest.raises(sqlite3.IntegrityError):  # unknown frame FK
            project.db.execute(
                "INSERT INTO frame_quality (frame_id, bg_median, bg_std, star_count, flagged, flag_reasons, scored_at) "
                "VALUES (9999, 1.0, 0.5, 1, 0, '[]', 't')"
            )


# --- public API -------------------------------------------------------------------------


def test_public_api_exports_quality_module():
    for name in (
        "FrameQuality",
        "QualityError",
        "SessionQuality",
        "frame_quality_for_frame",
        "save_frame_quality",
        "session_quality",
    ):
        assert name in store.__all__, name
        assert hasattr(store, name), name
    assert issubclass(store.QualityError, store.ProjectError)
    assert store.save_frame_quality is save_frame_quality
    assert store.session_quality is session_quality
    # the schema carries the v4 table
    assert store.SCHEMA_VERSION == 8
    conn = sqlite3.connect(":memory:")
    store.initialize_db(conn)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "frame_quality" in tables
    conn.close()


def test_quality_module_source_scan_no_gi():
    import re
    from pathlib import Path

    from seestar_controller.store import quality

    source = Path(quality.__file__).read_text(encoding="utf-8")
    assert not re.search(r"^\s*(import gi|from gi)\b", source, re.MULTILINE)
    assert not re.search(r"^\s*(import|from)\s+seestar_controller\.link\b", source, re.MULTILINE)
