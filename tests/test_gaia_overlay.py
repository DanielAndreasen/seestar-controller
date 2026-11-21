"""Tests for seestar_controller.gaia.overlay — the PURE issue-03 layer: the
overlay render (tone-map + matched G-mag labels / unmatched X markers) and
the CrossmatchController (follows the viewer's open stack, runs the issue-01
pipeline cache-first, re-runs on radius/G-limit changes, persists via the
issue-02 glue, renders the annotated overlay).

No network (the single TAP HTTP boundary ``gaia.query.fetch_gaia_tap_json``
is monkeypatched), no gi."""

from __future__ import annotations

import io
import re
import urllib.error
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from astropy.io import fits as pyfits
from PIL import Image
from test_gaia_detect import STARS, make_stack, wcs_header
from test_gaia_pipeline import gaia_rows_for_stars
from test_gaia_query import gaia_payload

from seestar_controller.catalog.cache import QueryCache
from seestar_controller.gaia import query as gq
from seestar_controller.gaia.errors import GaiaError
from seestar_controller.gaia.overlay import (
    MATCHED_COLOR,
    UNMATCHED_COLOR,
    CrossmatchController,
    render_stack_overlay,
    tonemap_gray,
)
from seestar_controller.gaia.persistence import (
    list_crossmatched_stacks,
    load_crossmatch,
)
from seestar_controller.store import (
    KIND_STACK,
    create_project,
    start_session,
    store_frame,
)
from seestar_controller.store.frames import header_to_dict

T0 = datetime(2026, 11, 20, 22, 0, 0, tzinfo=UTC)


# --- fixtures ---------------------------------------------------------------


@pytest.fixture()
def project(tmp_path):
    p = create_project("M 42", root=tmp_path / "projects")
    yield p
    p.close()


def _fits_bytes(data: np.ndarray, header: pyfits.Header, date_obs: str = "2026-11-20T22:00:10.000") -> bytes:
    """Serialize a synthetic stack as a real FITS file with DATE-OBS."""
    hdu = pyfits.PrimaryHDU(data=np.asarray(data, dtype=np.float32), header=header)
    hdu.header["DATE-OBS"] = date_obs
    buf = io.BytesIO()
    pyfits.HDUList([hdu]).writeto(buf)
    return buf.getvalue()


@pytest.fixture()
def stack_frame(project):
    """One session STACK with a WCS + the injected stars, stored in the
    project; returns (frame, header)."""
    sid = start_session(project, started_at=T0)
    header = wcs_header()
    frame = store_frame(project, sid, KIND_STACK, _fits_bytes(make_stack(), header))
    return frame, header


@pytest.fixture()
def tap(monkeypatch):
    """The mocked Gaia TAP boundary. Returns ``(calls, arm)``; ``arm(header,
    jitter_arcsec=...)`` installs Gaia rows at the injected stars' sky
    positions, filtered server-side by the query's Gmag limit (like the real
    TAP endpoint)."""
    calls: list[str] = []

    def arm(header, *, jitter_arcsec: float = 0.3):
        rows = gaia_rows_for_stars(header, jitter_arcsec=jitter_arcsec)

        def fake_fetch(query, **kwargs):
            calls.append(query)
            match = re.search(r'"Gmag" < ([0-9.eE+-]+)', query)
            limit = float(match.group(1)) if match else 99.0
            return gaia_payload([row for row in rows if row["g"] < limit])

        monkeypatch.setattr(gq, "fetch_gaia_tap_json", fake_fetch)

    return calls, arm


@pytest.fixture()
def cache(tmp_path):
    return QueryCache(cache_dir=tmp_path / "gaia-cache", ttl=3600.0)


def _no_network(monkeypatch):
    def boom(query, **kwargs):
        raise urllib.error.URLError("no network")

    monkeypatch.setattr(gq, "fetch_gaia_tap_json", boom)


def _row(x, y, matched, g=None):
    return SimpleNamespace(
        x=x, y=y, ra_deg=0.0, dec_deg=0.0, matched=matched,
        source_id=1 if matched else None, g=g,
        separation_arcsec=0.3 if matched else None,
    )


# --- tonemap_gray -------------------------------------------------------------


def test_tonemap_gray_stretches_and_maps_nan_to_zero():
    data = np.zeros((4, 4), dtype=np.float32)
    data[2, 2] = 100.0
    data[0, 0] = np.nan
    gray = tonemap_gray(data)
    assert gray.dtype == np.uint8
    assert gray.shape == (4, 4)
    assert gray[0, 0] == 0  # NaN -> 0
    assert gray[2, 2] == 255  # the bright pixel is the white point
    assert gray[1, 1] == 0


def test_tonemap_gray_constant_image_does_not_divide_by_zero():
    gray = tonemap_gray(np.full((3, 3), 7.0, dtype=np.float32))
    assert gray.shape == (3, 3)
    assert (gray == 0).all()


def test_tonemap_gray_rejects_bad_input():
    with pytest.raises(GaiaError):
        tonemap_gray(np.zeros((2, 2, 3), dtype=np.float32))
    with pytest.raises(GaiaError):
        tonemap_gray(np.zeros((2, 2), dtype=np.float32), lo_percentile=101.0)
    with pytest.raises(GaiaError):
        tonemap_gray(np.zeros((2, 2), dtype=np.float32), hi_percentile=-1.0)


# --- render_stack_overlay -------------------------------------------------------


def test_render_stack_overlay_marks_matched_with_g_label_and_unmatched_with_x():
    data = np.full((64, 64), 100.0, dtype=np.float32)
    data[20, 10] = 5000.0  # star at (x=10, y=20)
    data[50, 40] = 4000.0  # star at (x=40, y=50)
    rows = [_row(10.0, 20.0, True, g=12.34), _row(40.0, 50.0, False)]
    image = render_stack_overlay(data, rows)
    assert isinstance(image, Image.Image)
    assert image.mode == "RGB"
    assert image.size == (64, 64)  # no downscale below max_size
    px = np.asarray(image)  # (H, W, 3) uint8
    # matched: the filled point marker sits exactly on the detection
    assert tuple(px[20, 10]) == MATCHED_COLOR[:3]
    # matched: the G-mag label is drawn next to the ring (anti-aliased text,
    # so look for greenish pixels right of the ring, not the exact color)
    label_area = px[10:22, 20:57]
    assert bool(((label_area[:, :, 1] > label_area[:, :, 0] + 50) & (label_area[:, :, 1] > label_area[:, :, 2] + 50)).any())
    # unmatched: the X marker crosses exactly at the detection
    assert tuple(px[50, 40]) == UNMATCHED_COLOR[:3]
    # the unmatched marker is distinct from the matched color
    assert UNMATCHED_COLOR[:3] != MATCHED_COLOR[:3]


def test_render_stack_overlay_downscales_before_drawing():
    data = np.full((256, 256), 100.0, dtype=np.float32)
    rows = [_row(128.0, 128.0, True, g=15.0)]
    image = render_stack_overlay(data, rows, max_size=64)
    assert image.size == (64, 64)
    px = np.asarray(image)
    # the marker moved with the scale: 128 * 64/256 = 32
    assert tuple(px[32, 32]) == MATCHED_COLOR[:3]


def test_render_stack_overlay_accepts_rgb_and_empty_rows():
    image = render_stack_overlay(np.zeros((8, 8, 3), dtype=np.float32), [])
    assert image.mode == "RGB" and image.size == (8, 8)


def test_render_stack_overlay_rejects_bad_rows_and_args():
    data = np.zeros((8, 8), dtype=np.float32)
    with pytest.raises(GaiaError):
        render_stack_overlay(data, [_row("x", 1.0, False)])
    with pytest.raises(GaiaError):
        render_stack_overlay(data, [SimpleNamespace(x=1.0, y=1.0, matched="yes")])
    with pytest.raises(GaiaError):
        # a matched row without a finite g cannot be labelled
        render_stack_overlay(data, [_row(1.0, 1.0, True, g=None)])
    with pytest.raises(GaiaError):
        render_stack_overlay(data, [], max_size=0)
    with pytest.raises(GaiaError):
        render_stack_overlay(np.zeros((2, 2, 3, 3), dtype=np.float32), [])


# --- CrossmatchController: the pipeline -----------------------------------------


def test_controller_run_crossmatch_matches_persists_and_renders(project, stack_frame, tap, cache):
    frame, header = stack_frame
    calls, arm = tap
    arm(header)
    cc = CrossmatchController(project, cache=cache)
    snap = cc.select_stack(frame.id)
    assert snap.stack_frame_id == frame.id
    assert snap.stack_label is not None
    assert snap.rows == ()  # nothing persisted yet
    assert snap.image is not None  # the plain tone-mapped stack is rendered
    assert snap.error is None

    snap = cc.run_crossmatch()
    assert snap.error is None
    assert len(calls) == 1  # one TAP query
    matched = [r for r in snap.rows if r.matched]
    assert len(matched) == len(STARS)  # every injected star matched its Gaia row
    assert all(r.g is not None and r.source_id is not None for r in matched)
    assert snap.image is not None  # the annotated overlay
    assert snap.status is not None and "matched" in snap.status
    # persisted through the issue-02 glue
    saved = load_crossmatch(project, frame.id)
    assert len(saved) == len(snap.rows)
    assert [s.frame_id for s in list_crossmatched_stacks(project)] == [frame.id]
    assert sum(1 for r in saved if r.matched) == len(STARS)


def test_controller_second_run_is_served_from_cache_without_http(project, stack_frame, tap, cache, monkeypatch):
    frame, header = stack_frame
    calls, arm = tap
    arm(header)
    cc = CrossmatchController(project, cache=cache)
    cc.select_stack(frame.id)
    cc.run_crossmatch()
    assert len(calls) == 1
    _no_network(monkeypatch)  # the network is down now
    snap = cc.run_crossmatch()
    assert snap.error is None
    assert len(calls) == 1  # no new HTTP: served from the disk cache
    assert sum(1 for r in snap.rows if r.matched) == len(STARS)


def test_controller_radius_change_rematches_cache_first(project, stack_frame, tap, cache, monkeypatch):
    frame, header = stack_frame
    calls, arm = tap
    arm(header, jitter_arcsec=0.8)  # separations ~0.8 arcsec
    cc = CrossmatchController(project, cache=cache)
    cc.select_stack(frame.id)
    snap = cc.run_crossmatch()
    assert sum(1 for r in snap.rows if r.matched) == len(STARS)  # 0.8 < 1.5
    assert len(calls) == 1
    _no_network(monkeypatch)
    # shrink the radius below the separation: everything unmatched, still no
    # HTTP (same g_limit -> the Gaia query is a cache hit)
    snap = cc.set_radius_arcsec(0.2)
    assert snap.error is None
    assert len(calls) == 1
    assert snap.radius_arcsec == pytest.approx(0.2)
    assert snap.rows and all(not r.matched for r in snap.rows)
    assert all(not r.matched for r in load_crossmatch(project, frame.id))  # re-persisted
    # widen again -> matched again, still cache-first
    snap = cc.set_radius_arcsec(1.5)
    assert sum(1 for r in snap.rows if r.matched) == len(STARS)
    assert len(calls) == 1


def test_controller_g_limit_change_requeries_with_a_new_cache_key(project, stack_frame, tap, cache):
    frame, header = stack_frame
    calls, arm = tap
    arm(header)  # g = 12.0 + i for the injected stars, plus one g=18.0 row
    cc = CrossmatchController(project, cache=cache)
    cc.select_stack(frame.id)
    cc.run_crossmatch()
    assert len(calls) == 1
    snap = cc.set_g_limit(13.0)
    assert snap.error is None
    assert len(calls) == 2  # a new g_limit is a new cache key -> a new query
    assert snap.g_limit == pytest.approx(13.0)
    matched = [r for r in snap.rows if r.matched]
    assert len(matched) == 1  # only g=12.0 < 13 survived the server-side filter
    assert matched[0].g == pytest.approx(12.0)


def test_controller_missing_wcs_is_a_clear_plate_solve_error_not_raised(project, tap, cache):
    sid = start_session(project, started_at=T0)
    header = pyfits.Header()  # no celestial CTYPE cards
    frame = store_frame(project, sid, KIND_STACK, _fits_bytes(make_stack(), header))
    calls, arm = tap
    arm(wcs_header())
    cc = CrossmatchController(project, cache=cache)
    cc.select_stack(frame.id)
    snap = cc.run_crossmatch()
    assert snap.error is not None
    assert "WcsMissingError" in snap.error and "plate-solve" in snap.error
    assert snap.rows == ()
    assert len(calls) == 0  # detection fails before any query


def test_controller_gaia_unavailable_without_cache_is_a_clear_error_not_raised(project, stack_frame, monkeypatch, cache):
    frame, _header = stack_frame
    _no_network(monkeypatch)
    cc = CrossmatchController(project, cache=cache)
    cc.select_stack(frame.id)
    snap = cc.run_crossmatch()
    assert snap.error is not None and "GaiaUnavailableError" in snap.error
    assert snap.rows == ()


def test_controller_select_stack_loads_the_persisted_table(project, stack_frame, tap, cache):
    frame, header = stack_frame
    calls, arm = tap
    arm(header)
    cc = CrossmatchController(project, cache=cache)
    cc.select_stack(frame.id)
    cc.run_crossmatch()
    assert len(calls) == 1
    # a fresh controller over the same project sees the persisted table —
    # no new Gaia query, the overlay renders straight away
    cc2 = CrossmatchController(project, cache=cache)
    snap = cc2.select_stack(frame.id)
    assert snap.error is None
    assert len(snap.rows) == len(cc.snapshot.rows)
    assert snap.image is not None
    assert len(calls) == 1


def test_controller_validation_raises_and_runtime_errors_are_recorded(project):
    cc = CrossmatchController(project)
    with pytest.raises(ValueError):
        cc.set_radius_arcsec(0.0)
    with pytest.raises(ValueError):
        cc.set_radius_arcsec(99.0)
    with pytest.raises(ValueError):
        cc.set_g_limit(5.0)
    with pytest.raises(ValueError):
        cc.run_crossmatch(radius_arcsec=-1.0)
    with pytest.raises(GaiaError):
        cc.select_stack(123)  # no such stack
    no_project = CrossmatchController()
    with pytest.raises(GaiaError):
        no_project.select_stack(1)
    snap = no_project.run_crossmatch()  # runtime failure -> snapshot.error, not raised
    assert snap.error is not None and "no project" in snap.error
    # no stack open -> recorded as well
    snap = cc.run_crossmatch()
    assert snap.error is not None and "no stack" in snap.error


def test_controller_listeners_get_frozen_snapshots(project, stack_frame, tap, cache):
    frame, header = stack_frame
    _calls, arm = tap
    arm(header)
    cc = CrossmatchController(project, cache=cache)
    seen: list[Any] = []
    cc.add_listener(seen.append)
    cc.select_stack(frame.id)
    cc.run_crossmatch()
    assert len(seen) >= 2
    assert all(s.stack_frame_id == frame.id for s in seen[1:])
    cc.remove_listener(seen.append)
    n = len(seen)
    cc.run_crossmatch()
    assert len(seen) == n  # listener removed


# --- CrossmatchController: following the viewer --------------------------------


class _FakeViewer:
    """A duck-typed stand-in for the shared enhance ViewerController."""

    def __init__(self, project):
        self.project = project
        self._listeners: list = []
        self.current: Any = None
        self.image: Any = None

    def add_listener(self, listener):
        self._listeners.append(listener)

    def remove_listener(self, listener):
        self._listeners.remove(listener)

    @property
    def snapshot(self):
        return SimpleNamespace(
            project_name=self.project.name,
            stacks=(),
            current=self.current,
            image=self.image,
            preview=None,
            error=None,
        )

    def emit(self):
        snap = self.snapshot
        for listener in list(self._listeners):
            listener(snap)


def test_controller_follows_the_viewer_open_stack(project, stack_frame, tap, cache):
    frame, header = stack_frame
    calls, arm = tap
    arm(header)
    viewer = _FakeViewer(project)
    cc = CrossmatchController(viewer_controller=viewer, cache=cache)
    assert cc.project is project  # adopted from the viewer
    assert cc.viewer_controller is viewer

    # open a stack on the viewer -> the controller adopts it
    viewer.current = SimpleNamespace(frame_id=frame.id, label=lambda: "the stack")
    viewer.image = SimpleNamespace(data=make_stack(), header=header_to_dict(header))
    viewer.emit()
    snap = cc.snapshot
    assert snap.stack_frame_id == frame.id
    assert snap.stack_label == "the stack"
    assert snap.image is not None

    # the pipeline runs on the adopted stack
    snap = cc.run_crossmatch()
    assert snap.error is None
    assert sum(1 for r in snap.rows if r.matched) == len(STARS)
    assert len(calls) == 1

    # closing the stack on the viewer clears the overlay
    viewer.current = None
    viewer.image = None
    viewer.emit()
    assert cc.snapshot.stack_frame_id is None
    assert cc.snapshot.image is None
    assert cc.snapshot.rows == ()

    # detach stops the following
    cc.detach_viewer()
    assert cc.viewer_controller is None
    viewer.current = SimpleNamespace(frame_id=frame.id, label=lambda: "again")
    viewer.image = SimpleNamespace(data=make_stack(), header=header_to_dict(header))
    viewer.emit()
    assert cc.snapshot.stack_frame_id is None


def test_controller_set_project_clears_state(project, stack_frame, tap, cache):
    frame, header = stack_frame
    _calls, arm = tap
    arm(header)
    cc = CrossmatchController(project, cache=cache)
    cc.select_stack(frame.id)
    cc.run_crossmatch()
    assert cc.snapshot.rows
    cc.set_project(None)
    snap = cc.snapshot
    assert snap.project_name is None
    assert snap.stack_frame_id is None
    assert snap.rows == ()
    assert snap.image is None
