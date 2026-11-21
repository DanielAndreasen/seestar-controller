"""Pure-layer tests for variable-scan ticket 02 — the candidate-view core:
the blink rendering (:mod:`seestar_controller.variable_scan.render`), the
candidates CSV export (:mod:`seestar_controller.variable_scan.export`), and
the shared :class:`~seestar_controller.variable_scan.ScanController`
(:mod:`seestar_controller.variable_scan.controller`).

Headless: no ``gi`` anywhere (the boundary test lives in
tests/test_variable_scan_boundary.py). The blink/export/controller tests run
on the SYNTHETIC project of tests/test_variable_scan.py (the ``scannable``
fixture: 10 sub-frames, one injected sinusoidal variable at source_id 1004),
reusing its fixtures via ``from test_variable_scan import ...`` (the same
pattern as the gaia widget tests). No network anywhere.
"""

from __future__ import annotations

import csv
import io
import json

import numpy as np
import pytest
from PIL import Image
from test_variable_scan import (  # noqa: F401  (project/root/scannable are fixtures)
    ALL_XYS,
    VARIABLE_XY,
    frozen,
    project,
    root,
    scannable,
)
from test_variable_scan import (
    T1 as SCAN_CLOCK,
)

from seestar_controller.store import KIND_SUB, list_frames
from seestar_controller.variable_scan import (
    CANDIDATES_CSV_FILE,
    CANDIDATES_CSV_HEADER,
    DEFAULT_BLINK_DURATION_MS,
    DEFAULT_CUTOUT_SIZE,
    DEFAULT_THRESHOLD,
    SCAN_POINTS_FILE,
    ScanController,
    ScanError,
    export_candidates_csv,
    extract_cutout,
    latest_scan_results,
    load_scan_points,
    load_scan_threshold,
    points_path,
    render_blink_frames,
    render_blink_gif,
    render_cutout_image,
    save_scan_points,
    scan_project,
    tonemap_gray,
)

VARIABLE_SOURCE_ID = 1000 + ALL_XYS.index(VARIABLE_XY)


# --- render: tonemap + cutout --------------------------------------------------------------------


def test_tonemap_gray_maps_to_uint8_with_percentile_stretch():
    data = np.arange(100, dtype=np.float32).reshape(10, 10)
    gray = tonemap_gray(data)
    assert gray.dtype == np.uint8
    assert gray.shape == (10, 10)
    assert gray.min() == 0
    assert gray.max() == 255
    # monotonic in the input
    assert gray[0, 0] < gray[9, 9]


def test_tonemap_gray_nan_and_inf_become_zero():
    data = np.array([[np.nan, 1.0], [np.inf, 2.0]], dtype=np.float32)
    gray = tonemap_gray(data)
    assert gray[0, 0] == 0
    assert gray[0, 1] == 0  # -inf maps to 0 after the finite-only stretch


def test_tonemap_gray_constant_image_does_not_divide_by_zero():
    gray = tonemap_gray(np.full((4, 4), 7.0, dtype=np.float32))
    assert gray.shape == (4, 4)
    assert gray.dtype == np.uint8


def test_tonemap_gray_rejects_bad_input():
    with pytest.raises(ScanError):
        tonemap_gray(np.zeros((2, 2, 2), dtype=np.float32))
    with pytest.raises(ScanError):
        tonemap_gray(np.zeros((2, 2), dtype=np.float32), lo_percentile=-1.0)
    with pytest.raises(ScanError):
        tonemap_gray(np.zeros((2, 2), dtype=np.float32), hi_percentile=101.0)
    with pytest.raises(ScanError):
        tonemap_gray(np.zeros((2, 2), dtype=np.float32), lo_percentile="x")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


def test_extract_cutout_centered_on_star():
    data = np.zeros((64, 64), dtype=np.float32)
    data[32, 32] = 1000.0
    cutout = extract_cutout(data, 32.0, 32.0, cutout_size=16)
    assert cutout.shape == (16, 16)
    assert cutout.dtype == np.float32
    # the star lands at the cutout center
    assert cutout[8, 8] == pytest.approx(1000.0)
    assert cutout.max() == pytest.approx(1000.0)


def test_extract_cutout_at_edge_is_zero_padded_full_size():
    data = np.ones((64, 64), dtype=np.float32)
    cutout = extract_cutout(data, 0.0, 0.0, cutout_size=16)
    assert cutout.shape == (16, 16)
    # only the bottom-right quarter overlaps the image
    assert cutout[8:, 8:].min() == pytest.approx(1.0)
    assert cutout[:8, :].max() == 0.0
    assert cutout[:, :8].max() == 0.0


def test_extract_cutout_rejects_bad_input():
    data = np.zeros((8, 8), dtype=np.float32)
    with pytest.raises(ScanError):
        extract_cutout(np.zeros((2, 2, 2)), 1.0, 1.0)
    with pytest.raises(ScanError):
        extract_cutout(data, 1.0, 1.0, cutout_size=0)
    with pytest.raises(ScanError):
        extract_cutout(data, float("nan"), 1.0)
    with pytest.raises(ScanError):
        extract_cutout(data, "x", 1.0)


def test_render_cutout_image_is_rgb_with_crosshair():
    data = np.zeros((64, 64), dtype=np.float32)
    data[20, 40] = 500.0
    image = render_cutout_image(data, 40.0, 20.0, cutout_size=DEFAULT_CUTOUT_SIZE)
    assert isinstance(image, Image.Image)
    assert image.mode == "RGB"
    assert image.size == (DEFAULT_CUTOUT_SIZE, DEFAULT_CUTOUT_SIZE)
    # the crosshair paints red pixels near the star (cutout center)
    center = image.getpixel((DEFAULT_CUTOUT_SIZE // 2, DEFAULT_CUTOUT_SIZE // 2))
    assert isinstance(center, tuple)
    assert center[0] > center[1]  # reddish


# --- render: blink frames + GIF -------------------------------------------------------------------


def test_render_blink_frames_one_image_per_sub(project, scannable):  # noqa: F811  (fixture params shadow the re-exported imports)
    frames = list_frames(project, kind=KIND_SUB)
    assert len(frames) == scannable.n_subs == 10
    images = render_blink_frames(project, frames, VARIABLE_XY[0], VARIABLE_XY[1])
    assert len(images) == len(frames)
    for image in images:
        assert isinstance(image, Image.Image)
        assert image.mode == "RGB"
        assert image.size == (DEFAULT_CUTOUT_SIZE, DEFAULT_CUTOUT_SIZE)


def test_render_blink_frames_skips_unreadable_frames(project, scannable):  # noqa: F811  (fixture params shadow the re-exported imports)
    frames = list_frames(project, kind=KIND_SUB)
    bogus = frames[0].__class__(
        id=-1,
        session_id=frames[0].session_id,
        kind=KIND_SUB,
        path="subs/no-such-frame.fits",
        jd=frames[0].jd,
        utc=frames[0].utc,
        exposure=10.0,
        gain=50,
        target="M 42",
    )
    images = render_blink_frames(
        project, [bogus, *frames], VARIABLE_XY[0], VARIABLE_XY[1]
    )
    assert len(images) == len(frames)  # the bogus frame was skipped


def test_render_blink_frames_rejects_bad_args(project, scannable):  # noqa: F811  (fixture params shadow the re-exported imports)
    frames = list_frames(project, kind=KIND_SUB)
    with pytest.raises(TypeError):
        render_blink_frames("not-a-project", frames, 1.0, 1.0)
    with pytest.raises(TypeError):
        render_blink_frames(project, "not-a-sequence", 1.0, 1.0)
    with pytest.raises(TypeError):
        render_blink_frames(project, [object()], 1.0, 1.0)
    with pytest.raises(ScanError):
        render_blink_frames(project, frames, float("inf"), 1.0)


def test_render_blink_gif_is_an_animated_gif():
    images = tuple(
        Image.fromarray(
            (np.arange(32 * 32, dtype=np.uint8) + 40 * k).reshape(32, 32), mode="L"
        ).convert("RGB")
        for k in range(3)
    )
    gif = render_blink_gif(images, duration_ms=DEFAULT_BLINK_DURATION_MS)
    assert isinstance(gif, bytes)
    assert gif[:6] in (b"GIF87a", b"GIF89a")
    with Image.open(io.BytesIO(gif)) as opened:
        assert opened.format == "GIF"
        assert opened.n_frames == 3  # ty: ignore[missing-attribute]  (PIL stubs lack n_frames)


def test_render_blink_gif_rejects_bad_args():
    image = Image.new("RGB", (8, 8))
    with pytest.raises(ScanError):
        render_blink_gif(())
    with pytest.raises(ScanError):
        render_blink_gif([image], duration_ms=0)
    with pytest.raises(ScanError):
        render_blink_gif([image], duration_ms=1.5)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError):
        render_blink_gif([object()])  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError):
        render_blink_gif("nope")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


# --- export: candidates CSV ------------------------------------------------------------------------


def test_export_candidates_csv_round_trip(project, scannable, tmp_path):  # noqa: F811  (fixture params shadow the re-exported imports)
    scan_project(project, clock=frozen(SCAN_CLOCK))
    records = latest_scan_results(project)
    assert records
    out = export_candidates_csv(project, records, tmp_path / "candidates.csv")
    assert out == tmp_path / "candidates.csv"
    with open(out, encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    assert tuple(rows[0]) == CANDIDATES_CSV_HEADER
    assert len(rows) == len(records) + 1
    first = rows[1]
    assert first[0] == "1"  # rank
    assert first[1] == str(VARIABLE_SOURCE_ID)  # the injected variable ranks first
    assert first[10] == "1"  # variable flag
    # floats round-trip exactly (repr)
    assert float(first[2]) == records[0].ra_deg
    assert float(first[9]) == records[0].variability
    # no temp file left behind
    assert list(tmp_path.glob("*.tmp")) == []


def test_export_candidates_csv_default_path_is_in_project(project, scannable):  # noqa: F811  (fixture params shadow the re-exported imports)
    scan_project(project, clock=frozen(SCAN_CLOCK))
    records = latest_scan_results(project)
    out = export_candidates_csv(project, records)
    assert out == project.path / CANDIDATES_CSV_FILE
    assert out.exists()
    assert list(project.path.glob("*.tmp")) == []


def test_export_candidates_csv_empty_records_writes_header_only(project, tmp_path):  # noqa: F811  (fixture params shadow the re-exported imports)
    out = export_candidates_csv(project, [], tmp_path / "empty.csv")
    with open(out, encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows == [list(CANDIDATES_CSV_HEADER)]


def test_export_candidates_csv_rejects_bad_args(project, tmp_path):  # noqa: F811  (fixture params shadow the re-exported imports)
    with pytest.raises(TypeError):
        export_candidates_csv("not-a-project", [])
    with pytest.raises(TypeError):
        export_candidates_csv(project, "not-records")
    with pytest.raises(TypeError):
        export_candidates_csv(project, [object()])
    with pytest.raises(TypeError):
        export_candidates_csv(project, [], path=123)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


# --- controller: sidecar ---------------------------------------------------------------------------


def test_scan_points_sidecar_round_trip(project, scannable):  # noqa: F811  (fixture params shadow the re-exported imports)
    report = scan_project(project, clock=frozen(SCAN_CLOCK))
    out = save_scan_points(project, report)
    assert out == points_path(project) == project.path / SCAN_POINTS_FILE
    payload = load_scan_points(project)
    assert payload is not None
    assert payload["schema_version"] == 1
    assert payload["scanned_at"] == report.scanned_at
    top = report.results[0]
    rows = payload["points_by_rank"][str(top.rank)]
    assert len(rows) == len(top.points)
    assert rows[0][0] == pytest.approx(top.points[0].jd)
    assert list(project.path.glob("*.tmp")) == []


def test_load_scan_points_missing_or_corrupt(project):  # noqa: F811  (fixture params shadow the re-exported imports)
    assert load_scan_points(project) is None
    path = points_path(project)
    path.write_text("{not json", encoding="utf-8")
    assert load_scan_points(project) is None
    path.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    assert load_scan_points(project) is None


def test_save_scan_points_rejects_bad_args(project):  # noqa: F811  (fixture params shadow the re-exported imports)
    with pytest.raises(TypeError):
        save_scan_points("not-a-project", object())  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError):
        save_scan_points(project, object())  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


# --- controller: project + refresh + scan ------------------------------------------------------------


def test_controller_without_project_snapshot_never_raises():
    controller = ScanController()
    snap = controller.snapshot
    assert snap.project_name is None
    assert snap.records == ()
    assert snap.candidates == ()
    assert snap.selected is None
    assert snap.points == ()
    assert snap.blink_images == ()
    assert snap.blink_gif is None
    assert snap.error is None
    with pytest.raises(ScanError):
        controller.run_scan()
    with pytest.raises(ScanError):
        controller.set_threshold(3.0)
    with pytest.raises(ScanError):
        controller.select_candidate(0)
    with pytest.raises(ScanError):
        controller.export_csv()


def test_controller_set_project_loads_threshold_and_refreshes(project, scannable):  # noqa: F811  (fixture params shadow the re-exported imports)
    controller = ScanController(project)
    snap = controller.snapshot
    assert snap.project_name == project.name
    assert snap.threshold == DEFAULT_THRESHOLD == load_scan_threshold(project)
    # no scan run yet: no records, no candidates, no error
    assert snap.records == ()
    assert snap.candidates == ()
    assert snap.error is None
    # the sub-frames are loaded for the blink
    assert len(snap.frames) == scannable.n_subs


def test_controller_run_scan_ranks_variable_first_and_selects_it(project, scannable):  # noqa: F811  (fixture params shadow the re-exported imports)
    controller = ScanController(project)
    snap = controller.run_scan()
    assert snap.error is None
    assert len(snap.records) == 4  # the 4 field stars (target/comps excluded)
    assert len(snap.candidates) >= 1
    top = snap.candidates[0]
    assert top.source_id == VARIABLE_SOURCE_ID
    assert top.variable
    # the first candidate is auto-selected: points + blink rendered
    assert snap.selected_index == 0
    assert snap.selected is top
    assert len(snap.points) == scannable.n_subs
    assert len(snap.blink_images) == scannable.n_subs
    assert snap.blink_gif is not None
    assert snap.blink_gif[:6] in (b"GIF87a", b"GIF89a")
    assert snap.frame_index == 0
    assert snap.status is not None
    # the points sidecar was written
    assert points_path(project).exists()


def test_controller_points_survive_a_fresh_controller(project, scannable):  # noqa: F811  (fixture params shadow the re-exported imports)
    first = ScanController(project)
    first.run_scan()
    # a fresh controller on the same project (e.g. after a restart) sees the
    # persisted run + the sidecar points
    second = ScanController(project)
    snap = second.snapshot
    assert snap.error is None
    assert len(snap.records) == 4
    assert snap.selected_index == 0
    assert snap.selected is not None
    assert snap.selected.source_id == VARIABLE_SOURCE_ID
    assert len(snap.points) == scannable.n_subs
    assert len(snap.blink_images) == scannable.n_subs


def test_controller_select_candidate_and_blink_steps(project, scannable):  # noqa: F811  (fixture params shadow the re-exported imports)
    controller = ScanController(project)
    controller.run_scan()
    # lower the threshold so all 4 field stars are candidates
    snap = controller.set_threshold(0.01)
    assert len(snap.candidates) == 4
    snap = controller.select_candidate(1)
    assert snap.selected_index == 1
    assert snap.selected is snap.candidates[1]
    assert len(snap.points) == scannable.n_subs
    assert len(snap.blink_images) == scannable.n_subs
    # blink stepping wraps around
    assert controller.next_frame().frame_index == 1
    assert controller.prev_frame().frame_index == 0
    assert controller.prev_frame().frame_index == scannable.n_subs - 1
    assert controller.select_frame(999).frame_index == scannable.n_subs - 1
    assert controller.select_frame(-5).frame_index == 0
    with pytest.raises(ScanError):
        controller.select_candidate(99)
    with pytest.raises(ScanError):
        controller.select_candidate("x")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    with pytest.raises(ScanError):
        controller.select_frame("x")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


def test_controller_set_threshold_filters_and_persists(project, scannable):  # noqa: F811  (fixture params shadow the re-exported imports)
    controller = ScanController(project)
    controller.run_scan()
    assert controller.snapshot.candidates
    snap = controller.set_threshold(99.0)
    assert snap.threshold == 99.0
    assert snap.candidates == ()
    assert snap.selected is None
    assert snap.points == ()
    assert snap.blink_images == ()
    assert load_scan_threshold(project) == 99.0  # persisted in project.json
    snap = controller.set_threshold(DEFAULT_THRESHOLD)
    assert len(snap.candidates) >= 1
    assert snap.selected_index == 0
    with pytest.raises(ScanError):
        controller.set_threshold("x")
    with pytest.raises(ScanError):
        controller.set_threshold(float("nan"))


def test_controller_export_csv_writes_displayed_candidates(project, scannable, tmp_path):  # noqa: F811  (fixture params shadow the re-exported imports)
    controller = ScanController(project)
    controller.run_scan()
    out = controller.export_csv(tmp_path / "candidates.csv")
    assert out.exists()
    with open(out, encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    assert tuple(rows[0]) == CANDIDATES_CSV_HEADER
    assert len(rows) == len(controller.snapshot.candidates) + 1
    assert rows[1][1] == str(VARIABLE_SOURCE_ID)
    # default destination inside the project
    out2 = controller.export_csv()
    assert out2 == project.path / CANDIDATES_CSV_FILE


def test_controller_listeners_get_frozen_snapshots(project, scannable):  # noqa: F811  (fixture params shadow the re-exported imports)
    controller = ScanController()
    seen: list = []
    controller.add_listener(seen.append)
    assert controller.listeners == (seen.append,)
    controller.set_project(project)
    assert seen  # set_project notified
    before = len(seen)
    controller.run_scan()
    assert len(seen) > before
    snap = seen[-1]
    assert snap.selected is not None
    # a failing listener is isolated
    def boom(_snapshot):
        raise RuntimeError("boom")

    controller.add_listener(boom)
    controller.refresh()
    controller.remove_listener(boom)
    controller.remove_listener(seen.append)
    assert controller.listeners == ()


def test_controller_attach_viewer_adopts_its_project(project):  # noqa: F811  (fixture params shadow the re-exported imports)
    class FakeViewer:
        def __init__(self):
            self.project = project
            self._listeners = []

        def add_listener(self, listener):
            self._listeners.append(listener)

        def remove_listener(self, listener):
            if listener in self._listeners:
                self._listeners.remove(listener)

    viewer = FakeViewer()
    controller = ScanController(viewer_controller=viewer)
    assert controller.viewer_controller is viewer
    assert controller.project is project
    assert controller.snapshot.project_name == project.name
    controller.detach_viewer()
    assert controller.viewer_controller is None
    assert viewer._listeners == []


def test_controller_invoke_on_main_marshals_notifications(project):  # noqa: F811  (fixture params shadow the re-exported imports)
    calls: list = []

    def invoke(callback):
        calls.append(callback)
        callback()

    controller = ScanController(project, invoke_on_main=invoke)
    seen: list = []
    controller.add_listener(seen.append)
    controller.refresh()
    assert calls and seen
