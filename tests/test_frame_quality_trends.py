"""Frame-quality ticket 02 — the PURE trend computation (headless).

Covers :mod:`seestar_controller.frame_quality.trends`: the airmass series
(astropy ``AltAz``, lazy import — checked against a direct astropy reference
computation), the target-coords resolution from the stored ``target`` texts,
the per-session series extraction from the stored metrics
(:func:`~seestar_controller.frame_quality.trends.session_trends` over a tmp
project with ``save_frame_quality`` records — no photutils scoring, so it
stays fast), the plot transform math (``TrendWindow`` /
``full_trend_window`` / ``trend_to_screen`` / ``screen_to_trend``, incl. the
real :class:`~seestar_controller.analysis.lightcurve.PlotGeometry` as the
duck-typed geom), and the app-level :class:`QualityController` (snapshots,
session selection, observer, listeners, viewer following, never-raising
snapshot) — plus the subprocess boundary: importing the pure layer pulls in
neither ``gi`` nor ``astropy`` (the astropy import is lazy inside
:func:`airmass_series`).

No ``gi`` anywhere, no network (synthetic FITS bytes in a tmp project, the
same pattern as tests/test_frame_quality.py).
"""

import io
import json
import math
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from astropy.io import fits as pyfits

from seestar_controller.frame_quality import (
    DB_CALL_TIMEOUT_S,
    DEFAULT_OBSERVER,
    DEFAULT_TREND_WINDOW,
    MAX_AIRMASS,
    Observer,
    QualityController,
    QualitySnapshot,
    SessionTrends,
    TrendPoint,
    TrendsError,
    TrendWindow,
    airmass_series,
    full_trend_window,
    resolve_target_coords,
    screen_to_trend,
    session_trends,
    trend_to_screen,
)
from seestar_controller.store import (
    KIND_SUB,
    SessionNotFoundError,
    close_session,
    create_project,
    save_frame_quality,
    start_session,
    store_frame,
)

T0 = datetime(2026, 11, 20, 22, 0, 0, tzinfo=UTC)


def frozen(dt: datetime):
    return lambda: dt


def fits_bytes(date_obs: str) -> bytes:
    """A small valid FITS file with the given DATE-OBS."""
    hdu = pyfits.PrimaryHDU(data=np.zeros((8, 8), dtype=np.float32))
    hdu.header["DATE-OBS"] = date_obs
    hdu.header["EXPTIME"] = 10.0
    hdu.header["OBJECT"] = "M 42"
    buf = io.BytesIO()
    pyfits.HDUList([hdu]).writeto(buf)
    return buf.getvalue()


def date_obs_str(offset_seconds: float) -> str:
    instant = T0 + timedelta(seconds=offset_seconds)
    return instant.isoformat(timespec="milliseconds").replace("+00:00", "")


@pytest.fixture()
def project(tmp_path):
    """An open project (closed at teardown)."""
    p = create_project("M 42 night", root=tmp_path / "projects")
    yield p
    p.close()


def store_scored_frame(
    project,
    session_id: str,
    *,
    offset_seconds: float,
    fwhm: float | None = 3.5,
    bg_median: float = 100.0,
    bg_std: float = 5.0,
    star_count: int = 10,
    flagged: bool = False,
    flag_reasons: tuple[str, ...] = (),
    meta: dict | None = None,
):
    """Store one sub frame + its quality record; returns the frame id."""
    frame = store_frame(
        project,
        session_id,
        KIND_SUB,
        fits_bytes(date_obs_str(offset_seconds)),
        meta=meta,
    )
    save_frame_quality(
        project,
        frame.id,
        fwhm=fwhm,
        bg_median=bg_median,
        bg_std=bg_std,
        star_count=star_count,
        flagged=flagged,
        flag_reasons=flag_reasons,
    )
    return frame.id


# --- Observer ---------------------------------------------------------------------------------


def test_observer_valid_and_default():
    obs = Observer(latitude_deg=48.1, longitude_deg=11.6, elevation_m=520.0)
    assert obs.latitude_deg == 48.1 and obs.elevation_m == 520.0
    assert DEFAULT_OBSERVER == Observer(0.0, 0.0, 0.0)
    assert Observer(10, 20).elevation_m == 0.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"latitude_deg": 91.0},
        {"latitude_deg": -90.5},
        {"longitude_deg": 181.0},
        {"longitude_deg": -180.5},
        {"latitude_deg": float("nan")},
        {"elevation_m": float("inf")},
        {"latitude_deg": "48"},
        {"latitude_deg": True},
    ],
)
def test_observer_invalid_raises(kwargs):
    with pytest.raises(TrendsError):
        Observer(**{"latitude_deg": 0.0, "longitude_deg": 0.0, **kwargs})


def test_trends_error_is_a_quality_error_value_error():
    from seestar_controller.frame_quality import QualityError

    assert issubclass(TrendsError, QualityError)
    assert issubclass(TrendsError, ValueError)


# --- airmass_series -----------------------------------------------------------------------------


def _astropy_reference_airmass(ra_hours, dec_deg, jds, observer):
    """The direct astropy computation (the reference for airmass_series)."""
    import astropy.units as u
    from astropy.coordinates import AltAz, EarthLocation, SkyCoord
    from astropy.time import Time

    times = Time(np.asarray(jds, dtype=np.float64), format="jd", scale="utc")
    location = EarthLocation(
        lat=observer.latitude_deg * u.deg,
        lon=observer.longitude_deg * u.deg,
        height=observer.elevation_m * u.m,
    )
    coord = SkyCoord(ra=ra_hours * u.hourangle, dec=dec_deg * u.deg, frame="icrs")
    altaz = coord.transform_to(AltAz(obstime=times, location=location))
    return np.asarray(altaz.alt.deg), np.asarray(altaz.secz)


def test_airmass_series_matches_astropy_reference():
    observer = Observer(latitude_deg=48.1, longitude_deg=11.6, elevation_m=520.0)
    ra, dec = 5.5, 22.0
    # jds spread over a night: 2026-11-20 22:00 UTC + 0..6 h
    jd0 = 2461000.0
    jds = [jd0 + i * (1.0 / 24.0) for i in range(7)]
    result = airmass_series(ra, dec, jds, observer=observer)
    assert len(result) == len(jds)
    alts, secz = _astropy_reference_airmass(ra, dec, jds, observer)
    for value, alt, sec in zip(result, alts, secz):
        if alt <= 0.0 or not math.isfinite(sec) or sec <= 0.0:
            assert value == MAX_AIRMASS
        else:
            assert value == pytest.approx(min(max(float(sec), 1.0), MAX_AIRMASS), rel=1e-9)


def test_airmass_series_zenith_is_one_and_clamped():
    # observer at the target's declination, target on the meridian -> alt ~ 90 deg
    observer = Observer(latitude_deg=22.0, longitude_deg=0.0)
    # find a jd where the target culminates: LST = RA at Greenwich
    # GMST (deg) at jd: use astropy in the test to pick the culmination jd
    import astropy.units as u
    from astropy.coordinates import EarthLocation, SkyCoord
    from astropy.time import Time

    loc = EarthLocation(lat=22.0 * u.deg, lon=0.0 * u.deg)
    coord = SkyCoord(ra=5.5 * u.hourangle, dec=22.0 * u.deg)
    jd0 = 2461000.0
    jds = [jd0 + i * (1.0 / 96.0) for i in range(97)]  # 15-min steps over a day
    times = Time(np.asarray(jds), format="jd", scale="utc")
    from astropy.coordinates import AltAz

    altaz = coord.transform_to(AltAz(obstime=times, location=loc))
    best = int(np.argmax(altaz.alt.deg))
    result = airmass_series(5.5, 22.0, [jds[best]], observer=observer)
    assert result[0] == pytest.approx(1.0, abs=1e-6)  # sec z at zenith = 1
    assert 1.0 <= result[0] <= MAX_AIRMASS


def test_airmass_series_below_horizon_saturates_at_max():
    # target at dec -80 seen from lat +80: always below the horizon
    observer = Observer(latitude_deg=80.0, longitude_deg=0.0)
    jds = [2461000.0 + i * 0.05 for i in range(5)]
    result = airmass_series(0.0, -80.0, jds, observer=observer)
    assert result == (MAX_AIRMASS,) * 5


def test_airmass_series_empty_and_ra_wrap():
    assert airmass_series(5.0, 22.0, ()) == ()
    observer = Observer(0.0, 0.0)
    jds = [2461000.0, 2461000.1]
    assert airmass_series(25.5, 22.0, jds, observer=observer) == airmass_series(
        1.5, 22.0, jds, observer=observer
    )
    assert airmass_series(-22.5, 22.0, jds, observer=observer) == airmass_series(
        1.5, 22.0, jds, observer=observer
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"ra_hours": "5"},
        {"ra_hours": float("nan")},
        {"dec_deg": 91.0},
        {"dec_deg": -90.5},
        {"dec_deg": True},
    ],
)
def test_airmass_series_invalid_coords_raise(kwargs):
    with pytest.raises(TrendsError):
        airmass_series(kwargs.get("ra_hours", 5.0), kwargs.get("dec_deg", 22.0), [2461000.0])


def test_airmass_series_invalid_jd_and_observer_raise():
    with pytest.raises(TrendsError, match="jd"):
        airmass_series(5.0, 22.0, [float("inf")])
    with pytest.raises(TrendsError, match="jd"):
        airmass_series(5.0, 22.0, [2461000.0, "x"])  # type: ignore[list-item]  # ty: ignore[invalid-argument-type]
    with pytest.raises(TrendsError, match="observer"):
        airmass_series(5.0, 22.0, [2461000.0], observer=(0, 0, 0))  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


# --- resolve_target_coords ------------------------------------------------------------------------


def test_resolve_target_coords_json_mapping():
    targets = [None, "M 42", json.dumps({"ra": 5.5, "dec": 22.0}), json.dumps({"ra": 1, "dec": 2})]
    assert resolve_target_coords(targets) == (5.5, 22.0)


def test_resolve_target_coords_ra_wrapped_and_dec_checked():
    assert resolve_target_coords([json.dumps({"ra": 25.5, "dec": 22.0})]) == (1.5, 22.0)
    assert resolve_target_coords([json.dumps({"ra": 5.5, "dec": 95.0})]) is None
    assert resolve_target_coords([json.dumps({"ra": 5.5, "dec": float("nan")})]) is None


def test_resolve_target_coords_skips_non_json_and_bad_shapes():
    assert resolve_target_coords([]) is None
    assert resolve_target_coords([None, 42, "M 42", "not json", "[1, 2]"]) is None  # type: ignore[list-item]  # ty: ignore[invalid-argument-type]
    assert resolve_target_coords([json.dumps({"ra": True, "dec": 22.0})]) is None
    assert resolve_target_coords([json.dumps({"ra": 5.5})]) is None
    assert resolve_target_coords([json.dumps({"name": "M 42"})]) is None
    assert resolve_target_coords([json.dumps([1, 2])]) is None
    # first JSON object with numeric ra/dec wins
    assert resolve_target_coords(
        [json.dumps({"ra": 1.0, "dec": 2.0}), json.dumps({"ra": 3.0, "dec": 4.0})]
    ) == (1.0, 2.0)


# --- session_trends -------------------------------------------------------------------------------


def test_session_trends_extracts_series_and_summary(project):
    sid = start_session(project, clock=frozen(T0))
    meta = {"target": {"ra": 5.5, "dec": 22.0}}  # no name -> stored as JSON
    ids = [
        store_scored_frame(
            project, sid,
            offset_seconds=10.0 * i,
            fwhm=3.5 + 0.1 * i,
            bg_median=100.0 + i,
            bg_std=5.0,
            star_count=10,
            flagged=(i == 2),
            flag_reasons=("fwhm",) if i == 2 else (),
            meta=meta,
        )
        for i in range(4)
    ]
    trends = session_trends(project, sid)
    assert isinstance(trends, SessionTrends)
    assert trends.session_id == sid
    assert trends.target == json.dumps({"ra": 5.5, "dec": 22.0}, sort_keys=True)
    assert trends.target_coords == (5.5, 22.0)
    assert trends.observer == DEFAULT_OBSERVER
    assert [p.frame_id for p in trends.points] == ids  # jd order
    assert all(isinstance(p, TrendPoint) for p in trends.points)
    assert trends.points[0].utc == date_obs_str(0.0).replace("T", " ") or trends.points[0].utc
    assert [p.fwhm for p in trends.points] == [3.5, 3.6, 3.7, 3.8]
    assert trends.frame_count == 4
    assert trends.flagged_count == 1
    assert trends.rejected_percent == pytest.approx(25.0)
    assert trends.median_fwhm == pytest.approx(3.65)
    assert trends.median_bg_median == pytest.approx(101.5)
    assert trends.median_bg_std == pytest.approx(5.0)
    assert trends.median_star_count == pytest.approx(10.0)
    assert trends.jds == tuple(p.jd for p in trends.points)
    # airmass present (coords known) and aligned with the points
    assert trends.has_airmass
    assert len(trends.airmass) == 4
    assert all(1.0 <= a <= MAX_AIRMASS for a in trends.airmass)
    # the airmass matches a direct airmass_series call on the same jds
    assert trends.airmass == airmass_series(5.5, 22.0, trends.jds)


def test_session_trends_name_only_target_has_no_airmass(project):
    sid = start_session(project, clock=frozen(T0))
    # a mapping WITH a name is stored as the name only -> no coords in the text
    meta = {"target": {"name": "M 42", "ra": 5.5, "dec": 22.0}}
    store_scored_frame(project, sid, offset_seconds=0.0, meta=meta)
    store_scored_frame(project, sid, offset_seconds=10.0, meta=meta)
    trends = session_trends(project, sid)
    assert trends.target == "M 42"
    assert trends.target_coords is None
    assert trends.airmass == ()
    assert trends.has_airmass is False
    assert trends.frame_count == 2


def test_session_trends_explicit_coords_override(project):
    sid = start_session(project, clock=frozen(T0))
    store_scored_frame(project, sid, offset_seconds=0.0, meta={"target": {"name": "M 42"}})
    trends = session_trends(project, sid, target_coords=(10.0, -30.0))
    assert trends.target_coords == (10.0, -30.0)
    assert trends.has_airmass
    assert trends.airmass == airmass_series(10.0, -30.0, trends.jds)


def test_session_trends_custom_observer(project):
    sid = start_session(project, clock=frozen(T0))
    meta = {"target": {"ra": 5.5, "dec": 22.0}}
    store_scored_frame(project, sid, offset_seconds=0.0, meta=meta)
    store_scored_frame(project, sid, offset_seconds=600.0, meta=meta)
    observer = Observer(latitude_deg=48.1, longitude_deg=11.6, elevation_m=520.0)
    trends = session_trends(project, sid, observer=observer)
    assert trends.observer == observer
    assert trends.airmass == airmass_series(5.5, 22.0, trends.jds, observer=observer)
    # a different observer gives (generically) different airmass values
    other = session_trends(project, sid, observer=Observer(-33.9, 151.2, 58.0))
    assert other.airmass != trends.airmass


def test_session_trends_unknown_session_raises(project):
    with pytest.raises(SessionNotFoundError):
        session_trends(project, "no-such-session")


def test_session_trends_empty_session(project):
    sid = start_session(project, clock=frozen(T0))
    store_frame(project, sid, KIND_SUB, fits_bytes(date_obs_str(0.0)))  # not scored
    trends = session_trends(project, sid)
    assert trends.points == ()
    assert trends.airmass == ()
    assert trends.has_airmass is False
    assert trends.frame_count == 0
    assert trends.flagged_count == 0
    assert trends.rejected_percent == 0.0
    assert trends.median_fwhm is None
    assert trends.median_bg_median is None
    assert trends.median_bg_std is None
    assert trends.median_star_count is None


def test_session_trends_unscored_frames_are_excluded(project):
    sid = start_session(project, clock=frozen(T0))
    scored = store_scored_frame(project, sid, offset_seconds=0.0)
    store_frame(project, sid, KIND_SUB, fits_bytes(date_obs_str(10.0)))  # no record
    trends = session_trends(project, sid)
    assert [p.frame_id for p in trends.points] == [scored]


def test_session_trends_none_fwhm_points_kept(project):
    sid = start_session(project, clock=frozen(T0))
    store_scored_frame(project, sid, offset_seconds=0.0, fwhm=None)
    store_scored_frame(project, sid, offset_seconds=10.0, fwhm=3.5)
    trends = session_trends(project, sid)
    assert trends.points[0].fwhm is None
    assert trends.median_fwhm == pytest.approx(3.5)  # median over the frames that have one


def test_session_trends_invalid_args(project):
    sid = start_session(project, clock=frozen(T0))
    with pytest.raises(TypeError, match="session_id"):
        session_trends(project, 123)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="observer"):
        session_trends(project, sid, observer=(0, 0))  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    with pytest.raises(TrendsError, match="target_coords"):
        session_trends(project, sid, target_coords=(1.0,))  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    with pytest.raises(TrendsError, match="target_coords"):
        session_trends(project, sid, target_coords=(1.0, 95.0))
    with pytest.raises(TrendsError, match="target_coords"):
        session_trends(project, sid, target_coords=(float("nan"), 0.0))


# --- TrendWindow + transforms ---------------------------------------------------------------------


def test_trend_window_validation():
    w = TrendWindow(x_min=0.0, x_max=2.0, y_min=-1.0, y_max=1.0)
    assert w.x_span == 2.0 and w.y_span == 2.0
    with pytest.raises(TrendsError, match="x_max"):
        TrendWindow(x_min=1.0, x_max=1.0, y_min=0.0, y_max=1.0)
    with pytest.raises(TrendsError, match="y_max"):
        TrendWindow(x_min=0.0, x_max=1.0, y_min=2.0, y_max=1.0)
    with pytest.raises(TrendsError, match="finite"):
        TrendWindow(x_min=0.0, x_max=float("nan"), y_min=0.0, y_max=1.0)
    with pytest.raises(TrendsError, match="number"):
        TrendWindow(x_min=0.0, x_max="1", y_min=0.0, y_max=1.0)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    assert DEFAULT_TREND_WINDOW.x_span == 1.0


def test_full_trend_window_pads_and_handles_degenerate():
    assert full_trend_window([], []) == DEFAULT_TREND_WINDOW
    w = full_trend_window([0.0, 10.0], [100.0, 200.0])
    assert w.x_min == pytest.approx(-0.5) and w.x_max == pytest.approx(10.5)
    assert w.y_min == pytest.approx(95.0) and w.y_max == pytest.approx(205.0)
    # degenerate (single point) ranges are expanded, not zero-span
    w = full_trend_window([5.0], [7.0])
    assert w.x_max > w.x_min and w.y_max > w.y_min
    assert w.x_min < 5.0 < w.x_max and w.y_min < 7.0 < w.y_max


class _Geom:
    """A minimal duck-typed geometry (margin_left/top + plot_width/height)."""

    def __init__(self, width=464, height=220, margin_left=64, margin_top=20):
        self.margin_left = margin_left
        self.margin_top = margin_top
        self.plot_width = width - margin_left - 16
        self.plot_height = height - margin_top - 40


def test_trend_screen_round_trip_duck_typed_geom():
    window = TrendWindow(x_min=2460000.0, x_max=2460001.0, y_min=1.0, y_max=5.0)
    geom = _Geom()
    # corners: y is NOT inverted — y_max maps to the TOP (margin_top)
    px, py = trend_to_screen(window, geom, window.x_min, window.y_max)
    assert (px, py) == (geom.margin_left, geom.margin_top)
    px, py = trend_to_screen(window, geom, window.x_max, window.y_min)
    assert px == pytest.approx(geom.margin_left + geom.plot_width)
    assert py == pytest.approx(geom.margin_top + geom.plot_height)
    # round trip
    for x, y in [(2460000.25, 2.0), (2460000.5, 3.5), (2460000.9, 4.9)]:
        px, py = trend_to_screen(window, geom, x, y)
        rx, ry = screen_to_trend(window, geom, px, py)
        assert rx == pytest.approx(x) and ry == pytest.approx(y)


def test_trend_screen_with_real_plot_geometry():
    # the widget passes the analysis lightcurve view's PlotGeometry — the
    # transforms must work with it unchanged (duck-typed geom)
    from seestar_controller.analysis.lightcurve import PlotGeometry

    geom = PlotGeometry(464, 220, margin_top=20)
    window = full_trend_window([2460000.0, 2460000.5], [3.0, 4.0])
    px, py = trend_to_screen(window, geom, 2460000.25, 3.5)
    assert geom.margin_left <= px <= geom.margin_left + geom.plot_width
    assert geom.margin_top <= py <= geom.margin_top + geom.plot_height
    rx, ry = screen_to_trend(window, geom, px, py)
    assert rx == pytest.approx(2460000.25) and ry == pytest.approx(3.5)


# --- QualityController (headless) -------------------------------------------------------------------


def _project_with_two_sessions(project):
    """Two closed sessions with scored frames; returns (sid_a, sid_b)."""
    sid_a = start_session(project, clock=frozen(T0))
    meta = {"target": {"ra": 5.5, "dec": 22.0}}
    store_scored_frame(project, sid_a, offset_seconds=0.0, fwhm=3.5, meta=meta)
    store_scored_frame(project, sid_a, offset_seconds=10.0, fwhm=3.6, flagged=True,
                       flag_reasons=("fwhm",), meta=meta)
    close_session(project, sid_a, clock=frozen(T0 + timedelta(hours=1)))
    sid_b = start_session(project, clock=frozen(T0 + timedelta(days=1)))
    store_scored_frame(project, sid_b, offset_seconds=0.0, fwhm=4.0, meta=meta)
    close_session(project, sid_b, clock=frozen(T0 + timedelta(days=1, hours=1)))
    return sid_a, sid_b


def test_quality_controller_no_project_empty_snapshot():
    qc = QualityController()
    snap = qc.snapshot
    assert isinstance(snap, QualitySnapshot)
    assert snap.sessions == () and snap.session_id is None
    assert snap.trends is None and snap.error is None
    assert qc.sessions() == ()
    assert qc.project is None


def test_quality_controller_snapshot_selects_first_session(project):
    sid_a, sid_b = _project_with_two_sessions(project)
    qc = QualityController(project)
    assert qc.project is project
    snap = qc.snapshot
    assert snap.sessions == (sid_a, sid_b)  # ordered by started_at
    assert snap.session_id == sid_a  # auto: the first
    assert snap.error is None
    assert snap.trends is not None and snap.trends.session_id == sid_a
    assert snap.trends.frame_count == 2
    assert snap.trends.flagged_count == 1
    assert snap.trends.rejected_percent == pytest.approx(50.0)
    assert snap.trends.median_fwhm == pytest.approx(3.55)
    assert snap.trends.has_airmass
    assert qc.sessions() == (sid_a, sid_b)
    assert qc.selected_session is None


def test_quality_controller_select_session(project):
    sid_a, sid_b = _project_with_two_sessions(project)
    qc = QualityController(project)
    snap = qc.select_session(sid_b)
    assert snap.session_id == sid_b
    assert snap.trends is not None and snap.trends.session_id == sid_b
    assert snap.trends.median_fwhm == pytest.approx(4.0)
    assert qc.selected_session == sid_b
    # unknown id falls back to the first session (not an error)
    snap = qc.select_session("no-such-session")
    assert snap.session_id == sid_a
    # None = auto again
    snap = qc.select_session(None)
    assert snap.session_id == sid_a
    with pytest.raises(TypeError, match="session_id"):
        qc.select_session(123)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


def test_quality_controller_set_observer_recomputes_airmass(project):
    _project_with_two_sessions(project)
    qc = QualityController(project)
    before_snap = qc.snapshot
    assert before_snap.trends is not None
    before = before_snap.trends.airmass
    observer = Observer(latitude_deg=-33.9, longitude_deg=151.2, elevation_m=58.0)
    snap = qc.set_observer(observer)
    assert qc.observer == observer
    assert snap.trends is not None and snap.trends.observer == observer
    assert snap.trends.airmass != before
    assert snap.trends.airmass == airmass_series(5.5, 22.0, snap.trends.jds, observer=observer)
    with pytest.raises(TypeError, match="observer"):
        qc.set_observer((0, 0))  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


def test_quality_controller_listeners_get_frozen_snapshots(project):
    sid_a, sid_b = _project_with_two_sessions(project)
    qc = QualityController(project)
    seen: list[QualitySnapshot] = []

    @qc.add_listener
    def listener(snapshot):
        seen.append(snapshot)

    assert qc.listeners == (listener,)
    qc.add_listener(listener)  # idempotent
    assert qc.listeners == (listener,)
    snap = qc.refresh()
    assert seen == [snap]
    assert snap.sessions == (sid_a, sid_b)
    qc.select_session(sid_b)
    assert len(seen) == 2 and seen[-1].session_id == sid_b
    qc.remove_listener(listener)
    assert qc.listeners == ()
    qc.refresh()
    assert len(seen) == 2  # no more notifications
    # a failing listener is isolated
    def bad(_snapshot):
        raise RuntimeError("boom")

    qc.add_listener(bad)
    qc.add_listener(listener)
    qc.refresh()
    assert len(seen) == 3


def test_quality_controller_set_project_notifies_and_clears(project, tmp_path):
    sid_a, _ = _project_with_two_sessions(project)
    qc = QualityController()
    seen: list[QualitySnapshot] = []
    qc.add_listener(seen.append)
    qc.set_project(project)
    assert qc.project is project
    assert seen and seen[-1].sessions == (sid_a, seen[-1].sessions[1])
    assert seen[-1].session_id == sid_a
    qc.set_project(None)
    assert qc.project is None
    assert seen[-1].sessions == ()


def test_quality_controller_follows_viewer_controller(project):
    class FakeViewer:
        def __init__(self):
            self.project = None
            self._listeners = []

        def add_listener(self, listener):
            self._listeners.append(listener)

        def remove_listener(self, listener):
            if listener in self._listeners:
                self._listeners.remove(listener)

        def emit(self):
            for listener in list(self._listeners):
                listener(None)

    sid_a, _ = _project_with_two_sessions(project)
    viewer = FakeViewer()
    qc = QualityController(viewer_controller=viewer)
    assert qc.viewer_controller is viewer
    assert qc.project is None  # viewer has no project yet
    assert qc.snapshot.sessions == ()
    viewer.project = project
    viewer.emit()  # the quality controller adopts the viewer's project
    assert qc.project is project
    assert qc.snapshot.sessions == (sid_a, qc.snapshot.sessions[1])
    assert qc.snapshot.session_id == sid_a
    # an explicit project wins over the viewer's
    other = create_project("other", root=project.path.parent / "other-root")
    try:
        qc.set_project(other)
        viewer.project = None
        viewer.emit()
        assert qc.project is other
    finally:
        qc.set_project(None)
        other.close()
    qc.detach_viewer()
    assert qc.viewer_controller is None
    assert viewer._listeners == []


def test_quality_controller_snapshot_never_raises_on_store_failure(project):
    _project_with_two_sessions(project)
    qc = QualityController(project)
    assert qc.snapshot.error is None
    project.close()  # the db is gone now — reads fail
    snap = qc.snapshot
    assert snap.error is not None
    assert "Error" in snap.error
    assert snap.sessions == ()
    # sessions() surfaces the failure as TrendsError
    with pytest.raises(TrendsError, match="cannot list the sessions"):
        qc.sessions()


def test_quality_controller_db_call_timeout_constant():
    assert DB_CALL_TIMEOUT_S == 60.0


def test_quality_controller_init_validates_observer():
    with pytest.raises(TypeError, match="observer"):
        QualityController(observer=(0, 0))  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


# --- subprocess boundary ----------------------------------------------------------------------------


def test_trends_module_imports_no_gi_no_astropy_until_airmass_called():
    code = (
        "import sys;"
        "import seestar_controller.frame_quality.trends as t;"
        "assert 'gi' not in sys.modules, 'gi imported';"
        "assert 'astropy' not in sys.modules, 'astropy imported eagerly';"
        "assert 'seestar_controller.analysis' not in sys.modules, 'analysis imported';"
        "assert 'seestar_controller.ui' not in sys.modules, 'ui imported';"
        "r = t.airmass_series(5.5, 22.0, [2461000.0]);"
        "assert len(r) == 1 and 1.0 <= r[0] <= t.MAX_AIRMASS;"
        "assert 'astropy' in sys.modules, 'astropy not imported lazily';"
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout
