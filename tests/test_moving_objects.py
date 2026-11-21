"""Moving-objects pure core (issue 01): detect / link / flag / persist.

Boundary tests first (the package must never import gi / link / ui /
enhance, enforced in a fresh subprocess + a source scan), then unit tests
of the linker and the flagger with synthetic detections, then an
end-to-end test over a real tmp_path project: synthetic subs with one
injected linear mover + static stars -> the mover track is recovered with
the correct speed/direction and the static stars are not flagged, with and
without a WCS. Persistence round-trip of the tracks sidecar. No network.
"""

from __future__ import annotations

import io
import json
import math
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import numpy as np
import pytest
from astropy.io import fits as pyfits

from seestar_controller.moving_objects import (
    DEFAULT_MAX_MOTION_PX,
    DEFAULT_MIN_FRAMES,
    Detection,
    DetectionError,
    FrameDetections,
    MoverReport,
    MovingObjectsError,
    MpcMatch,
    PersistenceError,
    SessionTracks,
    Track,
    TrackError,
    detect_in_subs,
    flag_linear_movers,
    link_tracks,
    load_tracks,
    save_tracks,
    tracks_path,
)
from seestar_controller.store import (
    KIND_SUB,
    SessionNotFoundError,
    create_project,
    start_session,
    store_frame,
)

MO_PACKAGE_DIR = (
    Path(__file__).resolve().parent.parent / "src" / "seestar_controller" / "moving_objects"
)

T0 = datetime(2026, 11, 20, 22, 0, 0, tzinfo=UTC)
N_FRAMES = 8
CADENCE_S = 30.0
MOVER_SPEED_PX = 2.0  # px per frame, along +x
STAR_SIGMA_PX = 1.5
STAR_FLUX = 2000.0
MOVER_FLUX = 1500.0
STATIC_STARS = ((20.0, 18.0), (46.0, 30.0), (34.0, 52.0))
MOVER_Y = 44.0
MOVER_X0 = 12.0
IMAGE_SIZE = 64


# --- boundary tests -------------------------------------------------------------


def test_moving_objects_package_imports_no_gi_no_link_no_ui_no_enhance():
    code = (
        "import sys;"
        "import seestar_controller.moving_objects;"
        "import seestar_controller.moving_objects.errors;"
        "import seestar_controller.moving_objects.detect;"
        "import seestar_controller.moving_objects.link;"
        "import seestar_controller.moving_objects.flag;"
        "import seestar_controller.moving_objects.persist;"
        "import seestar_controller.moving_objects.mpc;"
        "import seestar_controller.moving_objects.overlay;"
        "assert 'gi' not in sys.modules, 'GTK imported';"
        "assert 'seestar_controller.link' not in sys.modules, 'link imported eagerly';"
        "assert 'seestar_controller.ui' not in sys.modules, 'ui imported eagerly';"
        "assert 'seestar_controller.enhance' not in sys.modules, 'enhance imported eagerly';"
        "assert 'seestar_controller.photometry' not in sys.modules, 'photometry imported';"
        "assert 'seestar_controller.moving_objects.overlay_view' not in sys.modules, ("
        "'widget module imported eagerly');"
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


#: Pure modules must never import gi / link / ui / enhance; the widget module
#: (overlay_view) is the single exception and must import gi at the top,
#: before any ``from gi.repository`` import.
PURE_MODULES = {"__init__", "errors", "detect", "link", "flag", "persist", "mpc", "overlay"}
WIDGET_MODULES = {"overlay_view"}


def test_moving_objects_modules_source_scan_no_gi_import():
    for path in sorted(MO_PACKAGE_DIR.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        if path.stem in WIDGET_MODULES:
            gi_line = re.search(r"^\s*import gi\b", source, re.MULTILINE)
            assert gi_line is not None, f"{path.name} must import gi at module top"
            first_repo = re.search(r"^\s*from gi\.repository", source, re.MULTILINE)
            assert first_repo is not None, f"{path.name} must use gi.repository"
            assert gi_line.start() < first_repo.start(), (
                f"{path.name}: 'import gi' must come before 'from gi.repository'"
            )
            continue
        assert path.stem in PURE_MODULES, f"unexpected module {path.name}"
        assert not re.search(r"^\s*(import gi|from gi)\b", source, re.MULTILINE), (
            f"{path.name} imports gi"
        )
        assert not re.search(
            r"^\s*(import|from)\s+seestar_controller\.(link|ui|enhance)\b",
            source,
            re.MULTILINE,
        ), f"{path.name} imports link/ui/enhance"


def test_moving_objects_package_module_set():
    modules = {path.stem for path in MO_PACKAGE_DIR.glob("*.py")}
    assert modules == PURE_MODULES | WIDGET_MODULES


def test_moving_objects_widget_exports_lazy():
    from seestar_controller import moving_objects

    assert moving_objects._WIDGET_EXPORTS == {"MoverOverlayView": "overlay_view"}
    assert "MoverOverlayView" in moving_objects.__all__
    # accessing the widget name must NOT happen at import time; only the
    # attribute lookup triggers the lazy import (and needs gi):
    assert "overlay_view" not in sys.modules or "gi" in sys.modules


def test_moving_objects_public_api_is_exported_from_package():
    from seestar_controller import moving_objects

    for name in moving_objects.__all__:
        if name in moving_objects._WIDGET_EXPORTS:
            continue  # lazy widget export: importing it needs gi (headless here)
        assert hasattr(moving_objects, name), name
    for name in (
        "Detection",
        "FrameDetections",
        "Track",
        "MoverReport",
        "SessionTracks",
        "MovingObjectsError",
        "DetectionError",
        "TrackError",
        "PersistenceError",
        "MpcError",
        "MpcLookupError",
        "MpcUnavailableError",
        "MpcResponseError",
        "MpcCandidate",
        "MpcMatch",
        "MoverController",
        "MoverSnapshot",
        "detect_in_subs",
        "link_tracks",
        "flag_linear_movers",
        "save_tracks",
        "load_tracks",
        "tracks_path",
        "fetch_mpc_checker_json",
        "mover_track_params",
        "build_checker_params",
        "parse_mpc_response",
        "match_mover",
        "lookup_mover",
        "lookup_mover_cached",
        "lookup_movers",
        "with_matches",
        "lookup_and_persist",
        "tonemap_gray",
        "render_frame_overlay",
        "SCHEMA_VERSION",
        "SUPPORTED_SCHEMA_VERSIONS",
    ):
        assert name in moving_objects.__all__, name


# --- fixtures / helpers -----------------------------------------------------------


@pytest.fixture()
def root(tmp_path):
    return tmp_path / "projects"


@pytest.fixture()
def project(root):
    p = create_project("M 42", root=root)
    yield p
    p.close()


def frozen(dt: datetime):
    return lambda: dt


def date_obs(i: int) -> str:
    """The DATE-OBS string of frame ``i`` (T0 + i * cadence, ms precision)."""
    dt = T0 + timedelta(seconds=CADENCE_S * i)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "")


def make_sub_fits(i: int, *, wcs: bool = False, seed: int = 0) -> bytes:
    """One synthetic 64x64 sub: gaussian background + 3 static stars + one
    linear mover at ``x = MOVER_X0 + MOVER_SPEED_PX * i``, ``y = MOVER_Y``."""
    rng = np.random.default_rng(seed)
    size = IMAGE_SIZE
    data = rng.normal(100.0, 5.0, size=(size, size))
    yy, xx = np.mgrid[0:size, 0:size]

    def add_star(cx: float, cy: float, flux: float) -> None:
        nonlocal data
        data += flux * np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * STAR_SIGMA_PX**2))

    for sx, sy in STATIC_STARS:
        add_star(sx, sy, STAR_FLUX)
    add_star(MOVER_X0 + MOVER_SPEED_PX * i, MOVER_Y, MOVER_FLUX)

    hdu = pyfits.PrimaryHDU(data=data)
    hdu.header["DATE-OBS"] = date_obs(i)
    hdu.header["EXPTIME"] = CADENCE_S
    hdu.header["GAIN"] = 100
    hdu.header["OBJECT"] = "M 42"
    if wcs:
        # 0.5 arcsec/px TAN projection centered on the image
        hdu.header["CTYPE1"] = "RA---TAN"
        hdu.header["CTYPE2"] = "DEC--TAN"
        hdu.header["CRVAL1"] = 83.633
        hdu.header["CRVAL2"] = -5.391
        hdu.header["CRPIX1"] = size / 2 + 0.5
        hdu.header["CRPIX2"] = size / 2 + 0.5
        hdu.header["CDELT1"] = -0.5 / 3600.0
        hdu.header["CDELT2"] = 0.5 / 3600.0
    buf = io.BytesIO()
    pyfits.HDUList([hdu]).writeto(buf)
    return buf.getvalue()


def make_session_with_subs(project, *, wcs: bool = False, n: int = N_FRAMES) -> str:
    """One session with ``n`` synthetic subs (one linear mover inside)."""
    sid = start_session(project, clock=frozen(T0))
    for i in range(n):
        store_frame(project, sid, KIND_SUB, make_sub_fits(i, wcs=wcs, seed=i))
    return sid


def det(fid: int, jd: float, x: float, y: float, **kw) -> Detection:
    """A synthetic pixel-space detection."""
    return Detection(
        frame_id=fid,
        path=f"subs/s/{fid:06d}.fits",
        jd=jd,
        utc=kw.get("utc", "2026-11-20T22:00:00+00:00"),
        x=x,
        y=y,
        peak=kw.get("peak", 1.0),
        flux=kw.get("flux", 1.0),
        ra_deg=kw.get("ra_deg"),
        dec_deg=kw.get("dec_deg"),
    )


def linear_track(track_id: int, n: int, x0: float, y0: float, dx: float, dy: float) -> Track:
    """A perfectly linear track of ``n`` points, one per frame (jd = 2461000 + i)."""
    return Track(
        track_id=track_id,
        points=tuple(
            det(i, 2461000.0 + i, x0 + dx * i, y0 + dy * i) for i in range(n)
        ),
    )


# --- link_tracks unit tests ---------------------------------------------------------


def test_link_tracks_greedy_nearest_neighbor():
    # one mover at 2 px/frame + one static star, 4 frames
    dets = []
    for i in range(4):
        dets.append(det(i, 2461000.0 + i, 10.0 + 2.0 * i, 30.0))
        dets.append(det(i, 2461000.0 + i, 50.0, 50.0))
    tracks = link_tracks(dets, max_motion_px=5.0)
    assert len(tracks) == 2
    assert [t.track_id for t in tracks] == [0, 1]
    mover = next(t for t in tracks if t.n_frames == 4)
    assert mover.frame_ids == (0, 1, 2, 3)
    assert mover.speed_px_per_frame == pytest.approx(2.0)
    assert mover.motion_px == pytest.approx(6.0)
    assert mover.duration_s == pytest.approx(3 * 86400.0)
    static = next(t for t in tracks if t.n_frames == 4 and t is not mover)
    assert static.speed_px_per_frame == pytest.approx(0.0)


def test_link_tracks_accepts_frame_detections_mixed_with_detections():
    # FrameDetections iterates its detections, so a mix flattens fine
    fd = FrameDetections(
        frame=cast(Any, None),
        has_wcs=False,
        detections=(det(0, 2461000.0, 10.0, 30.0), det(0, 2461000.0, 50.0, 50.0)),
    )
    tracks = link_tracks([fd, det(1, 2461001.0, 12.0, 30.0)], max_motion_px=5.0)
    assert len(tracks) == 2
    assert max(t.n_frames for t in tracks) == 2


def test_link_tracks_gate_too_small_splits_track():
    dets = [det(i, 2461000.0 + i, 10.0 + 2.0 * i, 30.0) for i in range(5)]
    tracks = link_tracks(dets, max_motion_px=1.0)  # motion 2 px > gate 1 px
    assert len(tracks) == 5
    assert all(t.n_frames == 1 for t in tracks)


def test_link_tracks_default_gate():
    assert DEFAULT_MAX_MOTION_PX > 0
    dets = [det(i, 2461000.0 + i, 10.0 + 2.0 * i, 30.0) for i in range(3)]
    tracks = link_tracks(dets)  # default gate covers 2 px/frame
    assert len(tracks) == 1
    assert tracks[0].n_frames == 3


@pytest.mark.parametrize("bad", [0, -1.0, float("nan"), float("inf"), "5", True, None])
def test_link_tracks_invalid_max_motion_raises(bad):
    with pytest.raises(TrackError):
        link_tracks([], max_motion_px=bad)


def test_link_tracks_rejects_foreign_items():
    with pytest.raises(TrackError):
        link_tracks([det(0, 2461000.0, 1.0, 1.0), "not-a-detection"])


# --- flag_linear_movers unit tests ---------------------------------------------------


def test_flag_linear_mover_along_x():
    track = linear_track(0, 8, 10.0, 30.0, 2.0, 0.0)
    movers = flag_linear_movers([track], min_frames=DEFAULT_MIN_FRAMES)
    assert len(movers) == 1
    m = movers[0]
    assert isinstance(m, MoverReport)
    assert m.track_id == 0
    assert m.n_frames == 8
    assert m.speed_px_per_frame == pytest.approx(2.0, abs=1e-9)
    assert m.direction_deg == pytest.approx(0.0, abs=1e-6)
    assert m.fit_residual_px == pytest.approx(0.0, abs=1e-9)
    assert m.motion_px == pytest.approx(14.0)
    assert m.start_x == pytest.approx(10.0)
    assert m.end_x == pytest.approx(24.0)
    assert m.speed_arcsec_per_frame is None  # no WCS on the points
    assert m.start_ra_deg is None and m.end_dec_deg is None


def test_flag_linear_mover_diagonal_direction():
    track = linear_track(3, 6, 10.0, 20.0, 1.0, 2.0)
    movers = flag_linear_movers([track], min_frames=5)
    assert len(movers) == 1
    m = movers[0]
    assert m.speed_px_per_frame == pytest.approx(math.hypot(1.0, 2.0))
    assert m.direction_deg == pytest.approx(math.degrees(math.atan2(2.0, 1.0)) % 360.0)
    assert m.track_id == 3


def test_flag_static_stars_not_flagged():
    track = linear_track(0, 8, 30.0, 30.0, 0.0, 0.0)  # zero speed
    assert flag_linear_movers([track]) == []


def test_flag_curved_track_not_flagged():
    # parabola x = i^2: fast (slope 7 px/frame) but NOT linear -> residual cut
    track = Track(
        track_id=0,
        points=tuple(det(i, 2461000.0 + i, float(i * i), 30.0) for i in range(8)),
    )
    assert flag_linear_movers([track]) == []


def test_flag_short_track_not_flagged_unless_min_frames_lowered():
    track = linear_track(0, 3, 10.0, 30.0, 2.0, 0.0)
    assert flag_linear_movers([track], min_frames=5) == []
    movers = flag_linear_movers([track], min_frames=3)
    assert len(movers) == 1
    assert movers[0].n_frames == 3


def test_flag_speed_arcsec_per_frame_with_wcs_points():
    # 2 px/frame at 0.5 arcsec/px -> 1 arcsec/frame; put the sky positions on
    # the equator so the great-circle distance is exactly the RA difference
    n = 8
    points = tuple(
        det(i, 2461000.0 + i, 10.0 + 2.0 * i, 30.0, ra_deg=10.0 + i / 3600.0, dec_deg=0.0)
        for i in range(n)
    )
    track = Track(track_id=0, points=points)
    movers = flag_linear_movers([track])
    assert len(movers) == 1
    assert movers[0].speed_arcsec_per_frame == pytest.approx(1.0, abs=1e-6)
    assert movers[0].start_ra_deg == pytest.approx(10.0)
    assert movers[0].end_ra_deg == pytest.approx(10.0 + 7 / 3600.0)


def test_flag_mixed_wcs_points_degrade_to_none_arcsec():
    points = tuple(
        det(i, 2461000.0 + i, 10.0 + 2.0 * i, 30.0, ra_deg=10.0, dec_deg=0.0)
        if i % 2 == 0
        else det(i, 2461000.0 + i, 10.0 + 2.0 * i, 30.0)
        for i in range(8)
    )
    movers = flag_linear_movers([Track(track_id=0, points=points)])
    assert len(movers) == 1
    assert movers[0].speed_arcsec_per_frame is None
    assert movers[0].speed_px_per_frame == pytest.approx(2.0)


@pytest.mark.parametrize("kwargs", [{"min_frames": 1}, {"min_frames": 0}, {"min_frames": 2.5}])
def test_flag_invalid_min_frames_raises(kwargs):
    with pytest.raises(TrackError):
        flag_linear_movers([], **kwargs)


@pytest.mark.parametrize(
    "kwargs", [{"max_residual_px": -1.0}, {"min_speed_px_per_frame": -0.5}, {"max_residual_px": "x"}]
)
def test_flag_invalid_cuts_raise(kwargs):
    with pytest.raises(TrackError):
        flag_linear_movers([], **kwargs)


def test_flag_rejects_non_track_items():
    with pytest.raises(TrackError):
        flag_linear_movers([det(0, 2461000.0, 1.0, 1.0)])


def test_errors_are_value_errors_and_base():
    assert issubclass(DetectionError, (MovingObjectsError, ValueError))
    assert issubclass(TrackError, (MovingObjectsError, ValueError))
    assert issubclass(PersistenceError, (MovingObjectsError, OSError))


# --- detect_in_subs: validation + e2e over a real project ---------------------------


def test_detect_unknown_session_raises(project):
    with pytest.raises(SessionNotFoundError):
        detect_in_subs(project, "no-such-session")


def test_detect_session_without_subs_returns_empty(project):
    sid = start_session(project, clock=frozen(T0))
    assert detect_in_subs(project, sid) == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"threshold": 0},
        {"threshold": -3.0},
        {"threshold": "low"},
        {"fwhm": 0.1},
        {"fwhm": 100.0},
        {"max_frames": 0},
        {"max_frames": -2},
        {"max_frames": 2.5},
    ],
)
def test_detect_invalid_arguments_raise(project, kwargs):
    sid = start_session(project, clock=frozen(T0))
    with pytest.raises(DetectionError):
        detect_in_subs(project, sid, **kwargs)


def test_detect_max_frames_limits(project):
    sid = make_session_with_subs(project, n=4)
    assert len(detect_in_subs(project, sid, max_frames=2)) == 2
    assert len(detect_in_subs(project, sid)) == 4


def test_detect_finds_injected_sources_no_wcs(project):
    sid = make_session_with_subs(project)
    frames = detect_in_subs(project, sid)
    assert len(frames) == N_FRAMES
    assert all(isinstance(fd, FrameDetections) for fd in frames)
    assert all(not fd.has_wcs for fd in frames)
    for i, fd in enumerate(frames):
        assert len(fd) >= 4  # 3 static stars + the mover (+ maybe noise)
        assert all(d.ra_deg is None and d.dec_deg is None for d in fd)
        # the mover is detected near its injected position
        expected_x = MOVER_X0 + MOVER_SPEED_PX * i
        near = [d for d in fd if abs(d.y - MOVER_Y) < 2.0 and abs(d.x - expected_x) < 2.0]
        assert near, f"mover not detected in frame {i}"
    # ordered by jd
    jds = [fd.frame.jd for fd in frames]
    assert jds == sorted(jds)


def test_detect_with_wcs_attaches_sky_positions(project):
    sid = make_session_with_subs(project, wcs=True)
    frames = detect_in_subs(project, sid)
    assert all(fd.has_wcs for fd in frames)
    assert all(d.ra_deg is not None and d.dec_deg is not None for fd in frames for d in fd)
    # the field center (CRVAL) should be within a few arcmin of every source
    for fd in frames:
        for d in fd:
            assert abs(d.dec_deg - (-5.391)) < 0.01
            assert abs((d.ra_deg - 83.633 + 180.0) % 360.0 - 180.0) < 0.01


# --- end-to-end: detect -> link -> flag ----------------------------------------------


def test_end_to_end_mover_recovered_static_stars_not_flagged(project):
    sid = make_session_with_subs(project)
    frames = detect_in_subs(project, sid)
    tracks = link_tracks(frames, max_motion_px=5.0)
    movers = flag_linear_movers(tracks, min_frames=DEFAULT_MIN_FRAMES)

    assert len(movers) == 1, f"expected exactly the injected mover, got {movers}"
    m = movers[0]
    assert m.n_frames == N_FRAMES
    assert m.speed_px_per_frame == pytest.approx(MOVER_SPEED_PX, abs=0.3)
    assert m.direction_deg == pytest.approx(0.0, abs=5.0)  # along +x
    assert m.fit_residual_px < 0.5
    assert m.start_x == pytest.approx(MOVER_X0, abs=1.0)
    assert m.end_x == pytest.approx(MOVER_X0 + MOVER_SPEED_PX * (N_FRAMES - 1), abs=1.0)
    assert m.start_y == pytest.approx(MOVER_Y, abs=1.0)
    assert m.speed_arcsec_per_frame is None  # no WCS on these subs

    # the static stars form long tracks too, but are not flagged (speed ~0)
    long_tracks = [t for t in tracks if t.n_frames >= DEFAULT_MIN_FRAMES]
    assert len(long_tracks) >= 4  # 3 static stars + the mover
    flagged_ids = {m.track_id for m in movers}
    for t in long_tracks:
        if t.track_id not in flagged_ids:
            assert t.speed_px_per_frame < 0.5


def test_end_to_end_with_wcs_reports_arcsec_speed(project):
    sid = make_session_with_subs(project, wcs=True)
    frames = detect_in_subs(project, sid)
    tracks = link_tracks(frames, max_motion_px=5.0)
    movers = flag_linear_movers(tracks)
    assert len(movers) == 1
    # 2 px/frame * 0.5 arcsec/px = 1 arcsec/frame
    assert movers[0].speed_arcsec_per_frame == pytest.approx(1.0, abs=0.2)
    assert movers[0].start_ra_deg is not None
    assert movers[0].end_ra_deg is not None


# --- persistence ----------------------------------------------------------------------


def test_persistence_round_trip(project):
    sid = make_session_with_subs(project, n=4)
    frames = detect_in_subs(project, sid)
    tracks = link_tracks(frames, max_motion_px=5.0)
    movers = flag_linear_movers(tracks)

    saved_at = "2026-11-20T23:00:00+00:00"
    path = save_tracks(project, sid, tracks, movers, saved_at=saved_at)
    assert path == Path(project.path) / "subs" / sid / "movers.json"
    assert path.is_file()

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    assert payload["session_id"] == sid
    assert payload["saved_at"] == saved_at
    assert len(payload["tracks"]) == len(tracks)
    assert len(payload["movers"]) == len(movers)

    loaded = load_tracks(project, sid)
    assert isinstance(loaded, SessionTracks)
    assert loaded.session_id == sid
    assert loaded.saved_at == saved_at
    assert loaded.tracks == tuple(tracks)
    assert loaded.movers == tuple(movers)


def test_persistence_default_saved_at(project):
    sid = make_session_with_subs(project, n=2)
    tracks = link_tracks(detect_in_subs(project, sid))
    path = save_tracks(project, sid, tracks)
    loaded = load_tracks(project, sid)
    assert loaded.saved_at is not None
    assert loaded.tracks == tuple(tracks)
    assert loaded.movers == ()
    assert path.is_file()


def test_persistence_missing_file_raises(project):
    sid = start_session(project, clock=frozen(T0))
    with pytest.raises(PersistenceError):
        load_tracks(project, sid)


def test_persistence_corrupt_file_raises(project, tmp_path):
    sid = make_session_with_subs(project, n=2)
    path = tracks_path(project, sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json {{{", encoding="utf-8")
    with pytest.raises(PersistenceError):
        load_tracks(project, sid)

    path.write_text(json.dumps({"schema_version": 99, "session_id": sid}), encoding="utf-8")
    with pytest.raises(PersistenceError):
        load_tracks(project, sid)

    path.write_text(
        json.dumps({"schema_version": 1, "session_id": "other-session", "tracks": []}),
        encoding="utf-8",
    )
    with pytest.raises(PersistenceError):
        load_tracks(project, sid)

    path.write_text(json.dumps({"schema_version": 1, "session_id": sid, "tracks": [{"x": 1}]}),
                    encoding="utf-8")
    with pytest.raises(PersistenceError):
        load_tracks(project, sid)


def test_persistence_unknown_session_raises(project):
    with pytest.raises(SessionNotFoundError):
        save_tracks(project, "no-such-session", [])
    with pytest.raises(SessionNotFoundError):
        load_tracks(project, "no-such-session")


@pytest.mark.parametrize("bad", ["../evil", "..", ".", "", "a/b", "a b", 123])
def test_persistence_unsafe_session_id_raises(project, bad):
    with pytest.raises(PersistenceError):
        tracks_path(project, bad)
    with pytest.raises(PersistenceError):
        save_tracks(project, bad, [])
    with pytest.raises(PersistenceError):
        load_tracks(project, bad)


def test_persistence_rejects_foreign_records(project):
    sid = start_session(project, clock=frozen(T0))
    with pytest.raises(PersistenceError):
        save_tracks(project, sid, cast(Any, ["not-a-track"]))
    with pytest.raises(PersistenceError):
        save_tracks(project, sid, [], cast(Any, ["not-a-mover"]))


def test_tracks_path_layout(project):
    sid = start_session(project, clock=frozen(T0))
    path = tracks_path(project, sid)
    assert path.name == "movers.json"
    assert path.parent == Path(project.path) / "subs" / sid


# --- persistence: schema_version 2 / mpc_match (issue 02) ---------------------------


def _sample_match() -> MpcMatch:
    return MpcMatch(
        designation="(1234) 2001 AB2",
        name="Test Asteroid",
        offset_arcsec=3.5,
        motion_arcsec_per_min=12.5,
        pa_deg=95.0,
        v_mag=18.2,
    )


def test_persistence_mpc_match_round_trip(project):
    from dataclasses import replace

    sid = make_session_with_subs(project, n=6)
    frames = detect_in_subs(project, sid)
    tracks = link_tracks(frames, max_motion_px=5.0)
    movers = flag_linear_movers(tracks)
    assert len(movers) == 1
    match = _sample_match()
    assert isinstance(match, MpcMatch)
    movers = tuple(replace(m, mpc_match=match) for m in movers)

    path = save_tracks(project, sid, tracks, movers)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    assert payload["movers"][0]["mpc_match"]["designation"] == "(1234) 2001 AB2"
    assert payload["movers"][0]["mpc_match"]["offset_arcsec"] == pytest.approx(3.5)

    loaded = load_tracks(project, sid)
    assert loaded.movers[0].mpc_match == match


def test_persistence_v1_file_migrates_to_mpc_match_none(project):
    """A schema_version 1 sidecar (no ``mpc_match``) loads fine; its movers
    get ``mpc_match=None`` (migrated on read)."""
    sid = make_session_with_subs(project, n=6)
    frames = detect_in_subs(project, sid)
    tracks = link_tracks(frames, max_motion_px=5.0)
    movers = flag_linear_movers(tracks)
    assert len(movers) == 1
    path = save_tracks(project, sid, tracks, movers)

    # rewrite the sidecar as a v1 file: drop mpc_match from the movers
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema_version"] = 1
    for mover in payload["movers"]:
        mover.pop("mpc_match", None)
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = load_tracks(project, sid)
    assert loaded.movers == tuple(movers)
    assert all(m.mpc_match is None for m in loaded.movers)


def test_persistence_corrupt_mpc_match_raises(project):
    sid = make_session_with_subs(project, n=6)
    frames = detect_in_subs(project, sid)
    tracks = link_tracks(frames, max_motion_px=5.0)
    movers = flag_linear_movers(tracks)
    path = save_tracks(project, sid, tracks, movers)

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["movers"][0]["mpc_match"] = {"designation": "x"}  # missing fields
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PersistenceError):
        load_tracks(project, sid)
