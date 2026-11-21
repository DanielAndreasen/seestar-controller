"""Tests for hr-diagram ticket 03's PURE layer: the plot transform math + the
matplotlib-Agg PNG renders (:mod:`seestar_controller.hr_diagram.plot`) and
the store-boundary controller
(:mod:`seestar_controller.hr_diagram.controller`) over a real tmp project.

No gi, no network (the isochrone fetcher is injected and raises -> the
bundled fallback grid; a subprocess test enforces the no-gi boundary).
The widget layer is covered by ``test_hr_diagram_view_widgets.py`` (gi-gated).
"""

from __future__ import annotations

import io
import math
import subprocess
import sys
from datetime import UTC, datetime

import pytest
from PIL import Image
from test_hr_diagram import (
    CLUSTER_IDS,
    FIELD_IDS,
    SNR_IDS,
    _fits_bytes,
    cluster_table,
)

import seestar_controller.hr_diagram as hr
from seestar_controller.hr_diagram import (
    CMD,
    HR,
    DiagramGeometry,
    DiagramModel,
    DiagramWindow,
    HrDiagramController,
    HrError,
    HrPoint,
)
from seestar_controller.hr_diagram.plot import (
    DEFAULT_HEIGHT_PX,
    DEFAULT_STACK_MAX_PX,
    DEFAULT_WIDTH_PX,
    DEFAULT_WINDOW,
    MIN_X_SPAN,
    MIN_Y_SPAN,
    POINT_HIT_RADIUS_PX,
    STACK_HIT_RADIUS_PX,
    abs_g_error,
    data_to_screen,
    full_window,
    nearest_point_by_pixel,
    pan_window,
    point_at,
    point_xy,
    render_diagram_png,
    render_stack_png,
    screen_to_data,
    stack_image_scale,
    zoom_window_at,
)


def _point(
    source_id: int = 1,
    x: float = 10.0,
    y: float = 20.0,
    g: float = 14.0,
    bp_rp: float = 1.2,
    abs_g: float | None = 6.0,
    parallax: float | None = 2.0,
    parallax_error: float | None = 0.02,
    member: bool = True,
) -> HrPoint:
    return HrPoint(
        source_id=source_id,
        x=x,
        y=y,
        g=g,
        bp_rp=bp_rp,
        abs_g=abs_g,
        parallax=parallax,
        parallax_error=parallax_error,
        pmra=5.0,
        pmdec=-2.0,
        member=member,
    )


def _png_size(png: bytes) -> tuple[int, int]:
    assert png[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    return Image.open(io.BytesIO(png)).size


# --- geometry + window validation -------------------------------------------------


def test_geometry_and_window_validation():
    geom = DiagramGeometry(720, 560)
    assert geom.plot_width == 720 - geom.margin_left - geom.margin_right
    assert geom.plot_height == 560 - geom.margin_top - geom.margin_bottom
    assert DiagramGeometry(10, 10).plot_width >= 1  # clamped, never zero
    with pytest.raises(HrError):
        DiagramGeometry(-1, 560)
    with pytest.raises(HrError):
        DiagramGeometry(720, 560, margin_left=True)
    win = DiagramWindow(x_min=-1.0, x_max=5.0, y_min=8.0, y_max=22.0)
    assert win.x_span == 6.0 and win.y_span == 14.0
    with pytest.raises(HrError):
        DiagramWindow(x_min=5.0, x_max=5.0, y_min=8.0, y_max=22.0)
    with pytest.raises(HrError):
        DiagramWindow(x_min=-1.0, x_max=5.0, y_min=22.0, y_max=8.0)
    with pytest.raises(HrError):
        DiagramWindow(x_min=float("nan"), x_max=5.0, y_min=8.0, y_max=22.0)
    with pytest.raises(HrError):
        DiagramWindow(x_min="a", x_max=5.0, y_min=8.0, y_max=22.0)  # ty: ignore[invalid-argument-type]


# --- transforms --------------------------------------------------------------------


def test_data_screen_round_trip_and_inverted_y():
    geom = DiagramGeometry(720, 560)
    win = DiagramWindow(x_min=0.0, x_max=4.0, y_min=10.0, y_max=20.0)
    # y_min (brighter) maps to the TOP margin, y_max to the bottom
    px, py = data_to_screen(win, geom, 0.0, 10.0)
    assert px == pytest.approx(geom.margin_left)
    assert py == pytest.approx(geom.margin_top)
    px, py = data_to_screen(win, geom, 4.0, 20.0)
    assert px == pytest.approx(720 - geom.margin_right)
    assert py == pytest.approx(560 - geom.margin_bottom)
    for x, y in ((1.3, 12.5), (3.9, 19.9), (0.0, 20.0)):
        sx, sy = data_to_screen(win, geom, x, y)
        rx, ry = screen_to_data(win, geom, sx, sy)
        assert (rx, ry) == pytest.approx((x, y))


def test_zoom_keeps_the_cursor_fixed_and_clamps():
    geom = DiagramGeometry(720, 560)
    win = DiagramWindow(x_min=0.0, x_max=4.0, y_min=10.0, y_max=20.0)
    px, py = 300.0, 200.0
    cx, cy = screen_to_data(win, geom, px, py)
    zoomed = zoom_window_at(win, geom, px, py, 2.0)
    assert zoomed.x_span == pytest.approx(win.x_span / 2.0)
    assert zoomed.y_span == pytest.approx(win.y_span / 2.0)
    # the data point under the cursor stays under the cursor
    assert screen_to_data(zoomed, geom, px, py) == pytest.approx((cx, cy))
    # zooming out grows the window
    out = zoom_window_at(win, geom, px, py, 0.5)
    assert out.x_span == pytest.approx(win.x_span * 2.0)
    # clamped to the minimum spans
    deep = zoom_window_at(win, geom, px, py, 1e9)
    assert deep.x_span == pytest.approx(MIN_X_SPAN)
    assert deep.y_span == pytest.approx(MIN_Y_SPAN)
    with pytest.raises(HrError):
        zoom_window_at(win, geom, px, py, 0.0)
    with pytest.raises(HrError):
        zoom_window_at(win, geom, px, py, float("inf"))
    with pytest.raises(TypeError):
        zoom_window_at(win, geom, px, py, "2")  # ty: ignore[invalid-argument-type]


def test_pan_window_shifts_by_the_pixel_delta():
    geom = DiagramGeometry(720, 560)
    win = DiagramWindow(x_min=0.0, x_max=4.0, y_min=10.0, y_max=20.0)
    panned = pan_window(win, geom, 72.0, -56.0)
    assert panned.x_span == win.x_span and panned.y_span == win.y_span
    assert panned.x_min == pytest.approx(win.x_min + 72.0 * win.x_span / geom.plot_width)
    assert panned.y_min == pytest.approx(win.y_min - 56.0 * win.y_span / geom.plot_height)


def test_full_window_padding_and_degenerate():
    assert full_window(CMD, ()) == DEFAULT_WINDOW
    points = (_point(g=12.0, bp_rp=0.5), _point(g=18.0, bp_rp=2.5))
    win = full_window(CMD, points)
    assert win.x_min < 0.5 and win.x_max > 2.5
    assert win.y_min < 12.0 and win.y_max > 18.0
    pad_x = 0.05 * 2.0
    assert win.x_min == pytest.approx(0.5 - pad_x)
    assert win.x_max == pytest.approx(2.5 + pad_x)
    single = full_window(HR, (_point(g=14.0, bp_rp=1.0, abs_g=6.0),))
    assert single.x_max - single.x_min == pytest.approx(0.2)
    assert single.y_max - single.y_min == pytest.approx(0.1)
    with pytest.raises(HrError):
        full_window("nope", points)


# --- point coordinates + hit tests ---------------------------------------------------


def test_point_xy_and_abs_g_error():
    p = _point(g=14.0, bp_rp=1.2, abs_g=6.0, parallax=2.0, parallax_error=0.02)
    assert point_xy(CMD, p) == (1.2, 14.0)
    assert point_xy(HR, p) == (1.2, 6.0)
    assert abs_g_error(p) == pytest.approx(5.0 * 0.02 / (math.log(10.0) * 2.0))
    no_plx = _point(abs_g=None, parallax=None, parallax_error=None)
    with pytest.raises(HrError):
        point_xy(HR, no_plx)
    assert abs_g_error(no_plx) is None
    assert abs_g_error(_point(parallax=0.0)) is None
    assert abs_g_error(_point(parallax=-1.0)) is None
    # non-finite parallax is not positive either -> None (no NaN leak)
    assert abs_g_error(_point(parallax=float("nan"))) is None
    assert abs_g_error(_point(parallax=float("inf"))) is None
    assert abs_g_error(_point(parallax=float("-inf"))) is None
    with pytest.raises(HrError):
        point_xy("nope", p)
    with pytest.raises(TypeError):
        point_xy(3, p)


def test_point_at_hit_test():
    geom = DiagramGeometry(720, 560)
    win = DiagramWindow(x_min=0.0, x_max=4.0, y_min=10.0, y_max=20.0)
    near = _point(source_id=1, g=14.0, bp_rp=2.0)
    far = _point(source_id=2, g=18.0, bp_rp=3.5, member=False)
    points = (near, far)
    sx, sy = data_to_screen(win, geom, *point_xy(CMD, near))
    assert point_at(win, geom, CMD, points, sx, sy) is near
    assert point_at(win, geom, CMD, points, sx + POINT_HIT_RADIUS_PX - 1, sy) is near
    assert point_at(win, geom, CMD, points, sx + POINT_HIT_RADIUS_PX + 5, sy) is None
    # a point without an H-R position is skipped on the H-R page
    no_abs = _point(source_id=3, abs_g=None, parallax=None, parallax_error=None)
    hx, hy = data_to_screen(win, geom, *point_xy(CMD, no_abs))
    assert point_at(win, geom, HR, (no_abs,), hx, hy) is None
    assert point_at(win, geom, CMD, (no_abs,), hx, hy) is no_abs


def test_nearest_point_by_pixel():
    a = _point(source_id=1, x=10.0, y=20.0)
    b = _point(source_id=2, x=100.0, y=200.0)
    assert nearest_point_by_pixel((a, b), 12.0, 20.0) is a
    assert nearest_point_by_pixel((a, b), 10.0 + STACK_HIT_RADIUS_PX, 20.0) is a
    assert nearest_point_by_pixel((a, b), 50.0, 100.0) is None
    assert nearest_point_by_pixel((), 0.0, 0.0) is None


# --- the diagram PNG render -----------------------------------------------------------


def _model(kind: str = CMD, **kwargs) -> DiagramModel:
    points = (
        _point(source_id=1, g=12.0, bp_rp=0.5, abs_g=4.0),
        _point(source_id=2, g=16.0, bp_rp=2.0, abs_g=8.0, member=False),
        _point(source_id=3, g=18.0, bp_rp=3.0, abs_g=10.0, member=False),
    )
    return DiagramModel(kind=kind, points=points, title="test", **kwargs)


def test_render_diagram_png_size_and_options():
    win = full_window(CMD, _model().points)
    png = render_diagram_png(_model(CMD), win)
    assert _png_size(png) == (DEFAULT_WIDTH_PX, DEFAULT_HEIGHT_PX)
    png_small = render_diagram_png(_model(CMD), win, width_px=360, height_px=280, dpi=100)
    assert _png_size(png_small) == (360, 280)
    # the H-R page renders too (different ylabel, abs_g on the y axis)
    hr_png = render_diagram_png(_model(HR), full_window(HR, _model(HR).points))
    assert _png_size(hr_png) == (DEFAULT_WIDTH_PX, DEFAULT_HEIGHT_PX)
    # options change the pixels: error bars, highlight, overlay
    base = render_diagram_png(_model(CMD), win)
    assert render_diagram_png(_model(CMD, show_errors=True), win) != base
    assert render_diagram_png(_model(CMD, highlighted=1), win) != base
    overlay = tuple(hr.IsochronePoint(bp_rp=0.5 + 0.1 * i, abs_g=4.0 + 0.3 * i, g=14.0 + 0.3 * i) for i in range(8))
    assert render_diagram_png(_model(CMD, overlay=overlay), win) != base
    # empty model still renders (placeholder pages must not crash)
    empty_png = render_diagram_png(DiagramModel(kind=CMD, points=()), DEFAULT_WINDOW)
    assert _png_size(empty_png) == (DEFAULT_WIDTH_PX, DEFAULT_HEIGHT_PX)


def test_render_diagram_png_bad_args():
    win = DEFAULT_WINDOW
    with pytest.raises(TypeError):
        render_diagram_png("nope", win)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError):
        render_diagram_png(_model(), "nope")  # ty: ignore[invalid-argument-type]
    with pytest.raises(HrError):
        render_diagram_png(_model(), win, width_px=0)
    with pytest.raises(HrError):
        render_diagram_png(_model(), win, height_px=10**9)
    with pytest.raises(HrError):
        render_diagram_png(_model(), win, dpi=-1)
    with pytest.raises(HrError):
        DiagramModel(kind="nope", points=())
    with pytest.raises(TypeError):
        DiagramModel(kind=CMD, points=(), highlighted=True)
    with pytest.raises(TypeError):
        DiagramModel(kind=CMD, points=(), show_errors=1)  # ty: ignore[invalid-argument-type]


# --- the stack image render ------------------------------------------------------------


def test_render_stack_png_and_scale():
    image = Image.new("RGB", (960, 480), (10, 20, 30))
    points = (_point(source_id=1, x=100.0, y=50.0), _point(source_id=2, x=500.0, y=200.0))
    png = render_stack_png(image, points)
    assert _png_size(png) == (DEFAULT_STACK_MAX_PX, DEFAULT_STACK_MAX_PX // 2)  # downscaled
    plain = render_stack_png(image, ())
    assert png != plain  # markers drawn
    assert render_stack_png(image, points, highlighted=1) != png  # ring drawn
    small = render_stack_png(image, points, max_size=2000)
    assert _png_size(small) == (960, 480)  # not upscaled
    # the scale factor matches the render
    assert stack_image_scale((960, 480)) == pytest.approx(DEFAULT_STACK_MAX_PX / 960.0)
    assert stack_image_scale((100, 50)) == 1.0
    with pytest.raises(HrError):
        stack_image_scale((0, 50))
    with pytest.raises(HrError):
        stack_image_scale((100, 50), max_size=0)
    with pytest.raises(TypeError):
        render_stack_png("nope", points)  # ty: ignore[invalid-argument-type]
    with pytest.raises(HrError):
        render_stack_png(image, points, max_size=-3)


# --- the controller over a real tmp project ---------------------------------------------


def _raising_fetcher(**_kwargs):
    raise RuntimeError("network access blocked in test")


@pytest.fixture()
def project(tmp_path):
    from seestar_controller import store

    project = store.create_project("M 42", root=tmp_path / "projects")
    session_id = store.start_session(project, datetime(2026, 11, 20, 22, 0, 0, tzinfo=UTC))
    frame = store.store_frame(
        project, session_id, store.KIND_STACK, _fits_bytes("2026-11-20T22:00:00.000")
    )
    store.save_crossmatch(project, frame.id, cluster_table())
    return project, frame


@pytest.fixture()
def controller(project, tmp_path):
    return HrDiagramController(
        project[0], isochrone_fetcher=_raising_fetcher, cache_dir=tmp_path / "iso-cache"
    )


def test_controller_without_project():
    ctl = HrDiagramController()
    snap = ctl.snapshot
    assert snap.project_name is None
    assert snap.stacks == () and snap.frame_id is None and snap.table == ()
    assert snap.image is None and snap.image_size is None and snap.overlay == ()
    with pytest.raises(HrError):
        ctl.refresh_stacks()
    with pytest.raises(HrError):
        ctl.select_stack(1)
    with pytest.raises(HrError):
        ctl.refresh()
    with pytest.raises(HrError):
        ctl.export_table("/tmp/never.csv")


def test_controller_select_stack_snapshot(controller, project):
    _, frame = project
    stacks = controller.refresh_stacks()
    assert [s.frame_id for s in stacks] == [frame.id]
    snap = controller.select_stack(frame.id)
    assert snap.error is None
    assert snap.project_name == "M 42"
    assert snap.frame_id == frame.id
    assert snap.stack_label and "2026" in snap.stack_label
    assert len(snap.table) == len(cluster_table())
    assert snap.membership is not None
    assert set(CLUSTER_IDS) <= snap.membership.member_ids
    assert {p.source_id for p in snap.cmd} == set(CLUSTER_IDS) | set(FIELD_IDS) | set(SNR_IDS)
    # the SNR probes: 2997 (SNR 5.025) passes, 2998 (SNR 5.0) and 2999 (2.0) drop
    hr_ids = {p.source_id for p in snap.hr}
    assert 2997 in hr_ids and 2998 not in hr_ids and 2999 not in hr_ids
    assert len(snap.hr) == len(snap.cmd) - 2
    assert all(p.abs_g is not None for p in snap.hr)
    # the stack image is loaded at FULL resolution (the crossmatch x/y grid)
    assert isinstance(snap.image, Image.Image)
    assert snap.image.mode == "RGB"
    assert snap.image_size == (8, 8)
    assert snap.status and "matched" in snap.status
    # no isochrone params yet -> empty overlay
    assert snap.params is None and snap.overlay == ()
    # the selection is reflected in the properties + the snapshot is frozen
    assert controller.frame_id == frame.id
    assert controller.stacks == stacks
    with pytest.raises(AttributeError):  # frozen dataclass
        snap.frame_id = 999


def test_controller_isochrone_overlay_from_store_params(controller, project, tmp_path):
    from seestar_controller import store

    project_obj, frame = project
    controller.select_stack(frame.id)
    assert controller.snapshot.overlay == ()
    store.set_isochrone_params(project_obj, age=9.0, feh=-0.2, dist_mod=10.0)
    snap = controller.refresh()
    assert snap.params is not None
    assert snap.params.age == 9.0 and snap.params.feh == -0.2 and snap.params.dist_mod == 10.0
    # the raising fetcher -> the bundled fallback grid, no network
    assert len(snap.overlay) > 0
    assert all(p.bp_rp is not None and p.abs_g is not None for p in snap.overlay)


def test_controller_export_table_and_listeners(controller, project, tmp_path):
    _, frame = project
    seen: list = []
    controller.add_listener(seen.append)
    controller.select_stack(frame.id)
    assert seen and seen[-1].frame_id == frame.id
    dest = controller.export_table(tmp_path / "table.csv")
    assert dest.exists()
    lines = dest.read_text(encoding="utf-8").splitlines()
    assert lines[0] == ",".join(hr.CSV_HEADER)
    assert len(lines) == 1 + len(cluster_table())
    # remove_listener stops the notifications
    controller.remove_listener(seen.append)
    controller.refresh()
    assert len(seen) == 1


def test_controller_bad_frame_id(controller, project):
    _, frame = project
    with pytest.raises(HrError):
        controller.select_stack(frame.id + 1000)
    with pytest.raises(HrError):
        controller.select_stack("1")
    with pytest.raises(HrError):
        controller.select_stack(True)


def test_controller_follows_viewer_and_auto_selects_open_stack(project, tmp_path):
    project_obj, frame = project

    class _Current:
        frame_id = frame.id

    class _ViewerSnapshot:
        project = project_obj
        current = _Current()

    class _FakeViewer:
        def __init__(self):
            self.project = project_obj
            self.snapshot = _ViewerSnapshot()
            self._listeners: list = []

        def add_listener(self, listener):
            self._listeners.append(listener)

        def remove_listener(self, listener):
            self._listeners.remove(listener)

    viewer = _FakeViewer()
    ctl = HrDiagramController(
        viewer_controller=viewer,
        invoke_on_main=lambda cb: cb(),
        isochrone_fetcher=_raising_fetcher,
        cache_dir=tmp_path / "iso-cache",
    )
    assert ctl.project is project_obj
    # attach_viewer adopted the viewer's open stack (it has a cross-match table)
    assert ctl.frame_id == frame.id
    assert ctl.snapshot.table
    ctl.detach_viewer()
    assert ctl.viewer_controller is None


def test_controller_set_project_clears_and_relists(controller, project, tmp_path):
    from seestar_controller import store

    _, frame = project
    controller.select_stack(frame.id)
    assert controller.frame_id == frame.id
    other = store.create_project("M 43", root=tmp_path / "projects")
    controller.set_project(other)
    assert controller.project is other
    assert controller.stacks == ()  # M 43 has no cross-matched stacks
    assert controller.frame_id is None
    controller.set_project(None)
    assert controller.project is None
    with pytest.raises(HrError):
        controller.refresh_stacks()


# --- package boundary (subprocess): plot + controller import no gi, render offline ------


def test_view_layer_imports_no_gi_and_renders_offline():
    code = """
import socket

class _BlockedSocket:
    def __init__(self, *args, **kwargs):
        raise RuntimeError("network access blocked in test")

socket.socket = _BlockedSocket
socket.create_connection = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("blocked"))
socket.getaddrinfo = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("blocked"))

import io
import sys

import seestar_controller.hr_diagram as hr
import seestar_controller.hr_diagram.plot as plot
import seestar_controller.hr_diagram.controller as controller

assert "gi" not in sys.modules, "GTK imported"
assert "seestar_controller.hr_diagram.view" not in sys.modules, "widget module imported"
assert "seestar_controller.ui" not in sys.modules, "ui imported"

# the plot render works fully offline (matplotlib Agg)
point = hr.HrPoint(source_id=1, x=1.0, y=2.0, g=14.0, bp_rp=1.2, abs_g=6.0,
                   parallax=2.0, parallax_error=0.02, pmra=5.0, pmdec=-2.0, member=True)
model = plot.DiagramModel(kind=plot.CMD, points=(point,), title="t")
png = plot.render_diagram_png(model, plot.full_window(plot.CMD, (point,)))
assert png[:8] == b"\\x89PNG\\r\\n\\x1a\\n"
from PIL import Image
assert Image.open(io.BytesIO(png)).size == (plot.DEFAULT_WIDTH_PX, plot.DEFAULT_HEIGHT_PX)

# the controller class is importable and validates without a project
ctl = controller.HrDiagramController()
try:
    ctl.select_stack(1)
except hr.HrError:
    pass
else:
    raise AssertionError("expected HrError without a project")
print("clean")
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout
