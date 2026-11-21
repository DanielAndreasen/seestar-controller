"""Astrometry ticket 02 — pointing error + drift statistics (pure).

Headless + no network: synthetic sessions with INJECTED solved-center
offsets (via ``store.save_frame_wcs``) — the requested target comes from the
observing queue entry, the project config ``target``, or the frames' stored
``target`` metadata, and the pointing error / drift math must come out within
tolerance. Missing-WCS frames are skipped with the count reported. Covers
:mod:`seestar_controller.astrometry.drift` (``pointing_error`` /
``drift_series`` / ``separation_arcsec`` / the result types / ``DriftError``)
and the package exports.
"""

from __future__ import annotations

import io
import math
from datetime import UTC, datetime, timedelta

import pytest
from astropy.io import fits as pyfits

from seestar_controller.astrometry import (
    DriftError,
    DriftPoint,
    DriftSeries,
    PointingError,
    drift_series,
    pointing_error,
    separation_arcsec,
)
from seestar_controller.catalog.coords import J2000Coord
from seestar_controller.observing.queue import QueueEntry
from seestar_controller.store import (
    KIND_SUB,
    SessionNotFoundError,
    create_project,
    save_frame_wcs,
    start_session,
    store_frame,
)

# --- fixtures ---------------------------------------------------------------------------


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

#: The requested target of the synthetic sessions (matches CARDS' CRVAL).
REQ_RA_DEG = 83.0
REQ_DEC_DEG = 22.0
REQ_COORD = J2000Coord(ra_deg=REQ_RA_DEG, dec_deg=REQ_DEC_DEG)

CARDS = {
    "CTYPE1": "RA---TAN",
    "CTYPE2": "DEC--TAN",
    "CRVAL1": REQ_RA_DEG,
    "CRVAL2": REQ_DEC_DEG,
    "CRPIX1": 128.5,
    "CRPIX2": 128.5,
    "CDELT1": -0.001,
    "CDELT2": 0.001,
}


def fits_bytes(date_obs: str) -> bytes:
    """A minimal valid FITS file with a DATE-OBS card (drives the frame jd)."""
    hdu = pyfits.PrimaryHDU(data=[[1.0, 2.0], [3.0, 4.0]])
    hdu.header["DATE-OBS"] = date_obs
    buf = io.BytesIO()
    pyfits.HDUList([hdu]).writeto(buf)
    return buf.getvalue()


def add_sub(project, session_id: str, i: int, base: datetime = T0, target_meta=None):
    """Store one synthetic sub (DATE-OBS = base + i*10 s) and return its Frame."""
    instant = base + timedelta(seconds=10 * i)
    meta = {"target": target_meta} if target_meta is not None else None
    return store_frame(
        project,
        session_id,
        KIND_SUB,
        fits_bytes(instant.isoformat(timespec="milliseconds")),
        meta=meta,
    )


def solve(project, frame, ra_deg: float, dec_deg: float):
    """Persist a solved-WCS record for ``frame`` at the given center."""
    return save_frame_wcs(
        project,
        frame.id,
        solver="gaia",
        n_stars=5,
        center_ra_deg=ra_deg,
        center_dec_deg=dec_deg,
        cards=CARDS,
    )


def ra_offset_deg(arcsec: float, dec_deg: float = REQ_DEC_DEG) -> float:
    """The RA difference in degrees that is ``arcsec`` on the sky at ``dec``."""
    return arcsec / (3600.0 * math.cos(math.radians(dec_deg)))


def dec_offset_deg(arcsec: float) -> float:
    return arcsec / 3600.0


@pytest.fixture()
def session(project):
    """One open session with three stored subs (10 s apart)."""
    sid = start_session(project, clock=frozen(T0))
    frames = [add_sub(project, sid, i) for i in range(3)]
    return sid, frames


# --- separation_arcsec -------------------------------------------------------------------


def test_separation_arcsec_known_values():
    assert separation_arcsec(0.0, 0.0, 0.0, 0.0) == pytest.approx(0.0, abs=1e-9)
    assert separation_arcsec(0.0, 0.0, 1.0, 0.0) == pytest.approx(3600.0, rel=1e-12)
    assert separation_arcsec(0.0, 0.0, 0.0, 1.0) == pytest.approx(3600.0, rel=1e-12)
    # one degree from the pole in any RA direction is one degree
    assert separation_arcsec(0.0, 89.0, 180.0, 89.0) == pytest.approx(7200.0, rel=1e-12)
    # RA wraps: 359.999 vs 0.001 at the equator is 0.002 deg = 7.2 arcsec
    assert separation_arcsec(359.999, 0.0, 0.001, 0.0) == pytest.approx(7.2, rel=1e-9)


# --- pointing_error -----------------------------------------------------------------------


def test_pointing_error_injected_ra_offset_via_queue_entry(project, session):
    sid, frames = session
    offset = 10.0  # arcsec eastward
    for f in frames:
        solve(project, f, REQ_RA_DEG + ra_offset_deg(offset), REQ_DEC_DEG)
    entry = QueueEntry(coord=REQ_COORD)
    result = pointing_error(project, sid, entry)
    assert isinstance(result, PointingError)
    assert result.session_id == sid
    assert result.requested_ra_deg == pytest.approx(REQ_RA_DEG)
    assert result.requested_dec_deg == pytest.approx(REQ_DEC_DEG)
    assert result.solved_ra_deg == pytest.approx(REQ_RA_DEG + ra_offset_deg(offset))
    assert result.solved_dec_deg == pytest.approx(REQ_DEC_DEG)
    assert result.error_arcsec == pytest.approx(offset, abs=1e-6)
    assert (result.n_frames, result.n_solved, result.n_missing_wcs) == (3, 3, 0)


def test_pointing_error_injected_dec_offset(project, session):
    sid, frames = session
    for f in frames:
        solve(project, f, REQ_RA_DEG, REQ_DEC_DEG + dec_offset_deg(20.0))
    result = pointing_error(project, sid, QueueEntry(coord=REQ_COORD))
    assert result.error_arcsec == pytest.approx(20.0, abs=1e-6)


def test_pointing_error_zero_offset(project, session):
    sid, frames = session
    for f in frames:
        solve(project, f, REQ_RA_DEG, REQ_DEC_DEG)
    result = pointing_error(project, sid, QueueEntry(coord=REQ_COORD))
    assert result.error_arcsec == pytest.approx(0.0, abs=1e-9)


def test_pointing_error_ra_wraparound(project, session):
    sid, frames = session
    # requested RA 359.999 deg, solved at 0.001 deg -> 0.002 deg = 7.2 arcsec at dec 0
    req = J2000Coord(ra_deg=359.999, dec_deg=0.0)
    for f in frames:
        solve(project, f, 0.001, 0.0)
    result = pointing_error(project, sid, QueueEntry(coord=req))
    assert result.error_arcsec == pytest.approx(7.2, abs=1e-6)
    assert result.solved_ra_deg == pytest.approx(0.001)


def test_pointing_error_uses_median_solved_center(project, session):
    sid, frames = session
    for f, arcsec in zip(frames, (10.0, 20.0, 30.0)):
        solve(project, f, REQ_RA_DEG, REQ_DEC_DEG + dec_offset_deg(arcsec))
    result = pointing_error(project, sid, QueueEntry(coord=REQ_COORD))
    assert result.solved_dec_deg == pytest.approx(REQ_DEC_DEG + dec_offset_deg(20.0))
    assert result.error_arcsec == pytest.approx(20.0, abs=1e-6)


def test_pointing_error_from_config_target(project, session):
    sid, _frames = session
    for f in session[1]:
        solve(project, f, REQ_RA_DEG + ra_offset_deg(5.0), REQ_DEC_DEG)
    project.config["target"] = {"name": "M 42", "ra_deg": REQ_RA_DEG, "dec_deg": REQ_DEC_DEG}
    result = pointing_error(project, sid)  # no entry -> config target
    assert result.requested_ra_deg == pytest.approx(REQ_RA_DEG)
    assert result.error_arcsec == pytest.approx(5.0, abs=1e-6)


def test_pointing_error_from_frame_target_metadata(project):
    sid = start_session(project, clock=frozen(T0))
    # the scope sidecar target {"ra": hours, "dec": deg} is stored as JSON
    frames = [add_sub(project, sid, i, target_meta={"ra": REQ_RA_DEG / 15.0, "dec": REQ_DEC_DEG}) for i in range(2)]
    for f in frames:
        solve(project, f, REQ_RA_DEG + ra_offset_deg(8.0), REQ_DEC_DEG)
    result = pointing_error(project, sid)  # no entry, no config -> frame target
    assert result.requested_ra_deg == pytest.approx(REQ_RA_DEG)
    assert result.requested_dec_deg == pytest.approx(REQ_DEC_DEG)
    assert result.error_arcsec == pytest.approx(8.0, abs=1e-6)


def test_pointing_error_entry_wins_over_config_and_frames(project, session):
    sid, frames = session
    for f in frames:
        solve(project, f, REQ_RA_DEG, REQ_DEC_DEG)
    project.config["target"] = {"name": "other", "ra_deg": 10.0, "dec_deg": 0.0}
    entry = QueueEntry.resolved("M 42", REQ_COORD)
    result = pointing_error(project, sid, entry)
    assert result.requested_ra_deg == pytest.approx(REQ_RA_DEG)
    assert result.error_arcsec == pytest.approx(0.0, abs=1e-9)


def test_pointing_error_accepts_coord_mapping_and_plain_coord(project, session):
    sid, frames = session
    for f in frames:
        solve(project, f, REQ_RA_DEG, REQ_DEC_DEG)
    by_mapping = pointing_error(project, sid, {"ra_deg": REQ_RA_DEG, "dec_deg": REQ_DEC_DEG})
    by_coord = pointing_error(project, sid, REQ_COORD)
    assert by_mapping.error_arcsec == pytest.approx(0.0, abs=1e-9)
    assert by_coord.error_arcsec == pytest.approx(0.0, abs=1e-9)


def test_pointing_error_missing_wcs_frames_counted(project, session):
    sid, frames = session
    solve(project, frames[0], REQ_RA_DEG, REQ_DEC_DEG)
    solve(project, frames[2], REQ_RA_DEG, REQ_DEC_DEG + dec_offset_deg(12.0))
    # frames[1] has no WCS -> skipped, counted
    result = pointing_error(project, sid, QueueEntry(coord=REQ_COORD))
    assert (result.n_frames, result.n_solved, result.n_missing_wcs) == (3, 2, 1)
    assert result.error_arcsec == pytest.approx(6.0, abs=1e-6)  # median of 0 and +12


def test_pointing_error_no_solved_frames_raises(project, session):
    sid, _frames = session
    with pytest.raises(DriftError, match="no solved frames"):
        pointing_error(project, sid, QueueEntry(coord=REQ_COORD))


def test_pointing_error_unknown_session_raises(project):
    with pytest.raises(SessionNotFoundError):
        pointing_error(project, "no-such-session", QueueEntry(coord=REQ_COORD))


def test_pointing_error_no_requested_target_raises(project, session):
    sid, _frames = session
    for f in session[1]:
        solve(project, f, REQ_RA_DEG, REQ_DEC_DEG)
    with pytest.raises(DriftError, match="no requested target"):
        pointing_error(project, sid)


def test_pointing_error_unresolved_entry_raises(project, session):
    sid, frames = session
    for f in frames:
        solve(project, f, REQ_RA_DEG, REQ_DEC_DEG)
    with pytest.raises(DriftError, match="unresolved"):
        pointing_error(project, sid, QueueEntry(name="M 42"))


def test_pointing_error_bad_entry_raises(project, session):
    sid, frames = session
    for f in frames:
        solve(project, f, REQ_RA_DEG, REQ_DEC_DEG)
    with pytest.raises(TypeError):
        pointing_error(project, sid, 42)
    with pytest.raises(DriftError, match="ra_deg"):
        pointing_error(project, sid, {"ra_deg": 1.0})
    with pytest.raises(DriftError, match="invalid entry coordinates"):
        pointing_error(project, sid, {"ra_deg": 1.0, "dec_deg": 123.0})


def test_pointing_error_session_id_type_check(project):
    with pytest.raises(TypeError):
        pointing_error(project, 123)  # ty: ignore[invalid-argument-type]


# --- drift_series -------------------------------------------------------------------------


def test_drift_series_offsets_times_and_rate(project, session):
    sid, frames = session
    # linear dec drift: +5 arcsec per frame, frames 10 s apart
    for i, f in enumerate(frames):
        solve(project, f, REQ_RA_DEG, REQ_DEC_DEG + dec_offset_deg(5.0 * i))
    series = drift_series(project, sid, QueueEntry(coord=REQ_COORD))
    assert isinstance(series, DriftSeries)
    assert series.session_id == sid
    assert series.requested_ra_deg == pytest.approx(REQ_RA_DEG)
    assert (series.n_frames, series.n_solved, series.n_missing_wcs) == (3, 3, 0)
    assert len(series.points) == 3
    assert all(isinstance(p, DriftPoint) for p in series.points)
    # ordered by jd, hours since the first solved frame (10 s = 1/360 h)
    assert [p.frame_id for p in series.points] == [f.id for f in frames]
    # the store's JD derivation carries ~1e-9 d float rounding -> rel=1e-5
    assert [p.hours for p in series.points] == pytest.approx(
        [0.0, 10.0 / 3600.0, 20.0 / 3600.0], rel=1e-5
    )
    assert [p.dec_offset_arcsec for p in series.points] == pytest.approx([0.0, 5.0, 10.0], abs=1e-6)
    assert [p.ra_offset_arcsec for p in series.points] == pytest.approx([0.0, 0.0, 0.0], abs=1e-9)
    assert [p.offset_arcsec for p in series.points] == pytest.approx([0.0, 5.0, 10.0], abs=1e-6)
    # utc carried through
    assert series.points[0].utc == frames[0].utc
    # least-squares slope: 5 arcsec per 10 s = 1800 arcsec/hour
    assert series.dec_drift_arcsec_per_hour == pytest.approx(1800.0, rel=1e-5)
    assert series.ra_drift_arcsec_per_hour == pytest.approx(0.0, abs=1e-9)


def test_drift_series_ra_offset_uses_cos_dec(project, session):
    sid, frames = session
    req = J2000Coord(ra_deg=REQ_RA_DEG, dec_deg=60.0)  # cos(60) = 0.5
    for f in frames:
        solve(project, f, REQ_RA_DEG + 0.01, 60.0)  # +0.01 deg RA
    series = drift_series(project, sid, QueueEntry(coord=req))
    # 0.01 deg * cos(60) * 3600 = 18 arcsec
    assert all(p.ra_offset_arcsec == pytest.approx(18.0, rel=1e-9) for p in series.points)
    assert all(p.dec_offset_arcsec == pytest.approx(0.0, abs=1e-9) for p in series.points)


def test_drift_series_ra_offset_wraps_positive(project, session):
    sid, frames = session
    req = J2000Coord(ra_deg=359.999, dec_deg=0.0)
    for f in frames:
        solve(project, f, 0.001, 0.0)  # +0.002 deg across the wrap
    series = drift_series(project, sid, QueueEntry(coord=req))
    assert all(p.ra_offset_arcsec == pytest.approx(7.2, abs=1e-6) for p in series.points)


def test_drift_series_missing_wcs_skipped_and_counted(project):
    sid = start_session(project, clock=frozen(T0))
    frames = [add_sub(project, sid, i) for i in range(4)]
    solve(project, frames[0], REQ_RA_DEG, REQ_DEC_DEG)
    # frames[1] unsolved
    solve(project, frames[2], REQ_RA_DEG, REQ_DEC_DEG + dec_offset_deg(4.0))
    solve(project, frames[3], REQ_RA_DEG, REQ_DEC_DEG + dec_offset_deg(6.0))
    series = drift_series(project, sid, QueueEntry(coord=REQ_COORD))
    assert (series.n_frames, series.n_solved, series.n_missing_wcs) == (4, 3, 1)
    assert [p.frame_id for p in series.points] == [frames[0].id, frames[2].id, frames[3].id]
    assert [p.dec_offset_arcsec for p in series.points] == pytest.approx([0.0, 4.0, 6.0], abs=1e-6)


def test_drift_series_single_point_has_no_rate(project):
    sid = start_session(project, clock=frozen(T0))
    frame = add_sub(project, sid, 0)
    solve(project, frame, REQ_RA_DEG + ra_offset_deg(3.0), REQ_DEC_DEG)
    series = drift_series(project, sid, QueueEntry(coord=REQ_COORD))
    assert len(series.points) == 1
    assert series.points[0].hours == 0.0
    assert series.points[0].ra_offset_arcsec == pytest.approx(3.0, abs=1e-6)
    assert series.ra_drift_arcsec_per_hour is None
    assert series.dec_drift_arcsec_per_hour is None


def test_drift_series_no_solved_frames_raises(project, session):
    sid, _frames = session
    with pytest.raises(DriftError, match="no solved frames"):
        drift_series(project, sid, QueueEntry(coord=REQ_COORD))


def test_drift_series_unknown_session_raises(project):
    with pytest.raises(SessionNotFoundError):
        drift_series(project, "no-such-session", QueueEntry(coord=REQ_COORD))


def test_drift_series_no_requested_target_raises(project, session):
    sid, frames = session
    for f in frames:
        solve(project, f, REQ_RA_DEG, REQ_DEC_DEG)
    with pytest.raises(DriftError, match="no requested target"):
        drift_series(project, sid)


def test_drift_series_from_config_target(project, session):
    sid, frames = session
    for f in frames:
        solve(project, f, REQ_RA_DEG, REQ_DEC_DEG + dec_offset_deg(2.0))
    project.config["target"] = {"name": "M 42", "ra_deg": REQ_RA_DEG, "dec_deg": REQ_DEC_DEG}
    series = drift_series(project, sid)
    assert series.requested_dec_deg == pytest.approx(REQ_DEC_DEG)
    assert all(p.dec_offset_arcsec == pytest.approx(2.0, abs=1e-6) for p in series.points)


# --- package exports ----------------------------------------------------------------------


def test_drift_api_exported_from_package():
    from seestar_controller import astrometry

    for name in (
        "pointing_error",
        "drift_series",
        "separation_arcsec",
        "PointingError",
        "DriftPoint",
        "DriftSeries",
        "DriftError",
    ):
        assert name in astrometry.__all__, name
        assert hasattr(astrometry, name), name
    assert issubclass(astrometry.DriftError, astrometry.AstrometryError)
    assert issubclass(astrometry.DriftError, ValueError)
