"""Tests for moving-objects issue 02 — the pure overlay render + controller
(``moving_objects/overlay.py``). Headless, no network (the MPC HTTP boundary
is monkeypatched), no gi.

Covered: :func:`tonemap_gray` (NaN -> 0, degenerate range widened, percentile
mapping, validation), :func:`render_frame_overlay` (RGB PIL image, downscale,
mover/track colors, current-frame ring, label, validation), and the
:class:`MoverController` over a real tmp_path project with synthetic subs
(one injected linear mover): project/session selection, analyze (detect ->
link -> flag -> persist), the cached MPC lookup persisting ``mpc_match``,
frame blink navigation (clamp + wrap), listeners, and error-in-snapshot
behavior.
"""

from __future__ import annotations

import json

import numpy as np
import pytest
from PIL import Image
from test_moving_objects import (
    linear_track,
    make_session_with_subs,
)

from seestar_controller.catalog.cache import QueryCache
from seestar_controller.moving_objects import (
    MoverController,
    MoverReport,
    MovingObjectsError,
    load_tracks,
    render_frame_overlay,
    tonemap_gray,
    tracks_path,
)
from seestar_controller.moving_objects import mpc as mpc_module
from seestar_controller.moving_objects.overlay import (
    CURRENT_COLOR,
    MOVER_COLOR,
    TRACK_COLOR,
)
from seestar_controller.store import SessionNotFoundError, create_project

# --- tonemap_gray ---------------------------------------------------------------------


def test_tonemap_gray_maps_percentiles_to_uint8():
    rng = np.random.default_rng(0)
    data = rng.normal(100.0, 10.0, size=(32, 32)).astype(np.float32)
    gray = tonemap_gray(data)
    assert gray.dtype == np.uint8
    assert gray.shape == (32, 32)
    assert gray.min() == 0
    assert gray.max() == 255


def test_tonemap_gray_nan_and_inf_become_zero():
    data = np.array([[1.0, np.nan], [np.inf, 5.0]], dtype=np.float32)
    gray = tonemap_gray(data)
    assert gray[0, 1] == 0  # NaN -> 0
    assert gray[1, 0] == 0  # inf -> 0
    # the finite values still map monotonically (5.0 above 1.0)
    assert gray[1, 1] > gray[0, 0]


def test_tonemap_gray_degenerate_constant_frame():
    gray = tonemap_gray(np.full((8, 8), 42.0, dtype=np.float32))
    assert gray.shape == (8, 8)
    assert gray.dtype == np.uint8  # no division by zero


def test_tonemap_gray_all_nan_is_zeros():
    gray = tonemap_gray(np.full((4, 4), np.nan, dtype=np.float32))
    assert (gray == 0).all()


def test_tonemap_gray_rejects_bad_input():
    with pytest.raises(MovingObjectsError):
        tonemap_gray(np.zeros((2, 2, 2), dtype=np.float32))  # not 2-D
    with pytest.raises(MovingObjectsError):
        tonemap_gray(np.zeros((2, 2), dtype=np.float32), lo_percentile=101.0)
    with pytest.raises(MovingObjectsError):
        tonemap_gray(np.zeros((2, 2), dtype=np.float32), hi_percentile=-1.0)


# --- render_frame_overlay ---------------------------------------------------------------


def _overlay_inputs():
    """One mover track (2 px/frame along +x) + one static track, 4 frames."""
    mover_track = linear_track(0, 4, x0=10.0, y0=30.0, dx=2.0, dy=0.0)
    static_track = linear_track(1, 4, x0=50.0, y0=50.0, dx=0.0, dy=0.0)
    mover = MoverReport(
        track_id=0,
        n_frames=4,
        start_utc="2026-11-20T22:00:00+00:00",
        end_utc="2026-11-20T22:01:30+00:00",
        start_jd=2461000.0,
        end_jd=2461000.0 + 1.5 / 1440.0,
        start_x=10.0,
        start_y=30.0,
        end_x=16.0,
        end_y=30.0,
        motion_px=6.0,
        speed_px_per_frame=2.0,
        direction_deg=90.0,
        fit_residual_px=0.01,
        speed_arcsec_per_frame=None,
        start_ra_deg=None,
        start_dec_deg=None,
        end_ra_deg=None,
        end_dec_deg=None,
    )
    rng = np.random.default_rng(1)
    image_data = rng.normal(100.0, 5.0, size=(64, 64)).astype(np.float32)
    return image_data, (mover_track, static_track), (mover,)


def test_render_frame_overlay_rgb_image_with_annotations():
    image_data, tracks, movers = _overlay_inputs()
    image = render_frame_overlay(image_data, tracks, movers, frame_id=2)
    assert isinstance(image, Image.Image)
    assert image.mode == "RGB"
    assert image.size == (64, 64)
    pixels = set(image.getdata())  # ty: ignore[invalid-argument-type]
    assert MOVER_COLOR[:3] in pixels  # the mover polyline/label
    assert TRACK_COLOR[:3] in pixels  # the static track polyline
    assert CURRENT_COLOR[:3] in pixels  # the current-frame ring


def test_render_frame_overlay_downscales_big_frames():
    image_data, tracks, movers = _overlay_inputs()
    big = np.repeat(np.repeat(image_data, 32, axis=0), 32, axis=1)  # 2048x2048
    image = render_frame_overlay(big, tracks, movers, max_size=256)
    assert image.size == (256, 256)
    # annotations are still drawn after the downscale
    assert MOVER_COLOR[:3] in set(image.getdata())  # ty: ignore[invalid-argument-type]


def test_render_frame_overlay_label_mentions_mpc_designation():
    from dataclasses import replace

    from seestar_controller.moving_objects import MpcMatch

    image_data, tracks, movers = _overlay_inputs()
    match = MpcMatch(
        designation="(1234) 2001 AB2", name=None, offset_arcsec=1.0,
        motion_arcsec_per_min=10.0, pa_deg=90.0, v_mag=None,
    )
    movers = tuple(replace(m, mpc_match=match) for m in movers)
    image = render_frame_overlay(image_data, tracks, movers)
    # the label is drawn in the mover color near the track start
    assert MOVER_COLOR[:3] in set(image.getdata())  # ty: ignore[invalid-argument-type]


def test_render_frame_overlay_validates_arguments():
    image_data, tracks, movers = _overlay_inputs()
    with pytest.raises(MovingObjectsError):
        render_frame_overlay(image_data, tracks, movers, max_size=0)
    with pytest.raises(MovingObjectsError):
        render_frame_overlay(image_data, ["not-a-track"], movers)  # type: ignore[list-item]  # ty: ignore[invalid-argument-type]
    with pytest.raises(MovingObjectsError):
        render_frame_overlay(image_data, tracks, ["not-a-mover"])  # type: ignore[list-item]  # ty: ignore[invalid-argument-type]


# --- MoverController (headless, tmp project) -------------------------------------------------


@pytest.fixture()
def project(tmp_path):
    p = create_project("M 42", root=tmp_path / "projects")
    yield p
    p.close()


@pytest.fixture()
def cache(tmp_path):
    return QueryCache(cache_dir=tmp_path / "mpc-cache", ttl=3600.0)


def test_controller_requires_project():
    controller = MoverController()
    assert controller.project is None
    assert controller.snapshot.project_name is None
    with pytest.raises(MovingObjectsError):
        controller.refresh_sessions()
    with pytest.raises(MovingObjectsError):
        controller.select_session("x")
    with pytest.raises(MovingObjectsError):
        controller.analyze_session()


def test_controller_set_project_lists_sessions_and_notifies(project):
    sid = make_session_with_subs(project, n=2)
    controller = MoverController()
    seen: list = []
    controller.add_listener(seen.append)
    controller.set_project(project)
    assert controller.project is project
    assert controller.sessions == (sid,)
    snap = controller.snapshot
    assert snap.project_name == "M 42"
    assert snap.sessions == (sid,)
    assert snap.session_id is None
    assert snap.image is None
    assert seen  # notified (at least once)
    assert seen[-1].sessions == (sid,)
    # set_project(None) clears
    controller.set_project(None)
    assert controller.snapshot.project_name is None
    assert controller.sessions == ()


def test_controller_select_session_unknown_raises_and_records(project):
    make_session_with_subs(project, n=2)
    controller = MoverController(project)
    with pytest.raises(SessionNotFoundError):
        controller.select_session("no-such-session")
    assert "no-such-session" in (controller.snapshot.error or "")


def test_controller_select_session_loads_frames_and_renders(project):
    sid = make_session_with_subs(project, n=4)
    controller = MoverController(project)
    snap = controller.select_session(sid)
    assert snap.session_id == sid
    assert len(snap.frames) == 4
    assert snap.frame_index == 0
    assert isinstance(snap.image, Image.Image)
    assert snap.image.mode == "RGB"
    assert snap.tracks == () and snap.movers == ()  # no sidecar yet
    assert controller.current_frame is snap.frames[0]


def test_controller_analyze_session_finds_mover_and_persists(project):
    sid = make_session_with_subs(project, wcs=True, n=6)
    controller = MoverController(project)
    controller.select_session(sid)
    snap = controller.analyze_session()
    assert snap.error is None
    assert len(snap.movers) == 1
    assert snap.movers[0].speed_px_per_frame == pytest.approx(2.0, abs=0.3)
    assert snap.movers[0].start_ra_deg is not None  # WCS -> sky coords
    assert "1 mover(s)" in (snap.status or "")
    assert isinstance(snap.image, Image.Image)
    # persisted to the sidecar at schema_version 2
    payload = json.loads(tracks_path(project, sid).read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    assert len(payload["movers"]) == 1
    # a fresh select_session loads the persisted tracks/movers
    controller2 = MoverController(project)
    snap2 = controller2.select_session(sid)
    assert len(snap2.movers) == 1
    assert snap2.movers[0].mpc_match is None


def test_controller_lookup_mpc_persists_match(project, cache, monkeypatch):
    sid = make_session_with_subs(project, wcs=True, n=6)
    controller = MoverController(project, cache=cache)
    controller.select_session(sid)
    controller.analyze_session()

    calls: list[dict] = []

    def fake_fetch(params, *, timeout=30.0, url=""):
        calls.append(dict(params))
        # a candidate exactly at the queried track -> offset 0 -> match
        return {
            "objects": [
                {
                    "designation": "(99999) 2026 XX",
                    "name": "Mock Planet",
                    "ra": float(params["ra"]),
                    "dec": float(params["dec"]),
                    "motion": float(params["motion"]),
                    "pa": float(params["pa"]),
                    "v": 17.0,
                }
            ]
        }

    monkeypatch.setattr(mpc_module, "fetch_mpc_checker_json", fake_fetch)
    snap = controller.lookup_mpc()
    assert snap.error is None
    assert len(calls) == 1  # one mover -> one (mocked) HTTP call
    assert "MPC: 1/1 matched" in (snap.status or "")
    match = snap.movers[0].mpc_match
    assert match is not None
    assert match.designation == "(99999) 2026 XX"
    assert match.offset_arcsec == pytest.approx(0.0, abs=1.0)
    # persisted in the sidecar
    reloaded = load_tracks(project, sid)
    assert reloaded.movers[0].mpc_match == match
    # a second lookup is served from the cache: no further HTTP
    snap2 = controller.lookup_mpc()
    assert snap2.movers[0].mpc_match == match
    assert len(calls) == 1


def test_controller_lookup_mpc_without_sidecar_records_error(project, cache):
    sid = make_session_with_subs(project, n=2)  # never analyzed -> no sidecar
    controller = MoverController(project, cache=cache)
    controller.select_session(sid)
    snap = controller.lookup_mpc()  # never raises
    assert snap.error is not None
    assert "PersistenceError" in snap.error


def test_controller_lookup_mpc_network_failure_records_error(project, cache, monkeypatch):
    sid = make_session_with_subs(project, wcs=True, n=6)
    controller = MoverController(project, cache=cache)
    controller.select_session(sid)
    controller.analyze_session()

    def failing_fetch(params, *, timeout=30.0, url=""):
        from seestar_controller.moving_objects import MpcUnavailableError

        raise MpcUnavailableError("offline")

    monkeypatch.setattr(mpc_module, "fetch_mpc_checker_json", failing_fetch)
    snap = controller.lookup_mpc()  # never raises
    assert snap.error is not None
    assert "MpcUnavailableError" in snap.error
    assert snap.movers[0].mpc_match is None


def test_controller_frame_blink_navigation(project):
    sid = make_session_with_subs(project, n=4)
    controller = MoverController(project)
    controller.select_session(sid)
    assert controller.frame_index == 0
    assert controller.next_frame().frame_index == 1
    assert controller.next_frame().frame_index == 2
    assert controller.prev_frame().frame_index == 1
    # wrap around both ways
    assert controller.select_frame(99).frame_index == 3  # clamped
    assert controller.next_frame().frame_index == 0  # wraps forward
    assert controller.prev_frame().frame_index == 3  # wraps backward
    assert controller.select_frame(-5).frame_index == 0  # clamped low
    with pytest.raises(MovingObjectsError):
        controller.select_frame("not-an-int")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    # every step re-rendered the overlay
    assert isinstance(controller.image, Image.Image)


def test_controller_listeners_isolated_and_removable(project):
    sid = make_session_with_subs(project, n=2)
    controller = MoverController(project)
    good: list = []

    def bad(snapshot):
        raise RuntimeError("boom")

    controller.add_listener(bad)
    controller.add_listener(good.append)
    controller.add_listener(good.append)  # idempotent
    assert len(controller.listeners) == 2
    controller.select_session(sid)  # must not raise despite the bad listener
    assert len(good) == 1
    controller.remove_listener(bad)
    assert len(controller.listeners) == 1
    controller.remove_listener(bad)  # no-op
    assert len(controller.listeners) == 1


def test_controller_analyze_without_session_raises(project):
    controller = MoverController(project)
    with pytest.raises(MovingObjectsError):
        controller.analyze_session()
    with pytest.raises(MovingObjectsError):
        controller.lookup_mpc()
