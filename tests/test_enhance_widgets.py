"""Widget-layer tests for the Enhance-view image viewer — these SKIP in the
headless sandbox (no ``gi`` module, no GTK system libs, no display; see
docs/wiki/assumptions.md) and run on the desktop, where the system packages
``python3-gi gir1.2-gtk-4.0 gir1.2-adw-1`` are installed (or under
``xvfb-run -a uv run pytest``).

Guards, in order: ``pytest.importorskip("gi")`` (no PyGObject at all),
``gi.require_version`` for the Gtk 4.0 / Adw 1 typelibs, and a
``Gtk.init_check()`` fixture (no display -> skip) — the same pattern as
tests/test_observing_widgets.py.

What is asserted when they run (image-enhance tickets 01 + 03): the
:class:`~seestar_controller.enhance.viewer.ImageViewer` renders one row per
project stack (``row_count`` / ``row_label`` matching
:meth:`~seestar_controller.enhance.fits_io.StackInfo.label`), selecting a row
opens that stack through the pure
:class:`~seestar_controller.enhance.viewer_controller.ViewerController` (the
Gtk.Picture gets a paintable texture, the placeholder hides, the info label
shows the target), with no project the view shows the "no project"
placeholder; and the ticket-03
:class:`~seestar_controller.enhance.enhance_view.EnhanceEditor` builds one
slider per numeric op param + a dropdown per choice param of the default
pipeline, moving a slider calls
:meth:`~seestar_controller.enhance.editor_controller.EditorController.set_param`
(the pipeline changes, undo becomes available), the Before/After toggle
switches the preview stack, and the Undo/Redo/Reset buttons drive the
editor controller (sliders re-sync, sensitivity follows can_undo/can_redo).
Ticket 04 adds the persistence buttons: Save enhanced / Save preset store into
the project (enhanced frame / preset row, reflected in the info label), the
Export PNG / JPEG buttons drive a ``Gtk.FileDialog`` whose callback marshals
the chosen path via ``invoke_on_main`` into the export, and the Enhance view
exposes the four buttons as pass-through properties. The wiring:
``build_view("enhance", viewer_controller=..., editor_controller=...)``
hands the shared controllers to the Enhance view, the app owns one shared
:class:`~seestar_controller.enhance.ViewerController` and one shared
:class:`~seestar_controller.enhance.EditorController`, and the window passes
them down to the Enhance page.

App-improvement ticket 08 adds the stitched-mosaic coverage: a mosaic is a
FILE-based product (``mosaics/<session>.fits``, NOT a ``frames`` row), so the
Enhance view lists it as an extra row (``frame_id=None``, ``kind="mosaic"``)
and opens it by path — selecting its row in the viewer/editor calls
:meth:`~seestar_controller.enhance.ViewerController.open_path`, and stitching
a session from the Projects view opens the mosaic in the shared viewer and
switches to the Enhance view.
"""

import io
import time
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from astropy.io import fits as pyfits

gi = pytest.importorskip("gi")

try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
except ValueError as exc:  # typelibs missing
    pytest.skip(f"GTK 4.0 / libadwaita typelibs missing: {exc}", allow_module_level=True)

from gi.repository import GLib, Gtk

from seestar_controller.enhance import (
    STRETCH_KINDS,
    EditorController,
    ViewerController,
    default_pipeline,
)
from seestar_controller.mosaic import mosaic_path
from seestar_controller.store import (
    KIND_STACK,
    KIND_SUB,
    create_project,
    start_session,
    store_frame,
)

T0 = datetime(2026, 11, 20, 22, 0, 0, tzinfo=UTC)
DATE1 = "2026-11-20T22:00:00.000"
DATE2 = "2026-11-20T22:00:10.000"


def frozen(dt: datetime):
    return lambda: dt


def synthetic_image(size: int = 64, seed: int = 1) -> np.ndarray:
    """A small synthetic stack image (gradient + star peaks + noise)."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:size, 0:size]
    data = ((xx + yy) * 10.0).astype(np.float32)
    data = data + (8000.0 * np.exp(-(((yy - 32) ** 2 + (xx - 32) ** 2) / 8.0))).astype(
        np.float32
    )
    return (data + rng.normal(0.0, 5.0, size=(size, size))).astype(np.float32)


def fits_bytes(data: np.ndarray, *, date_obs: str, obj: str) -> bytes:
    hdu = pyfits.PrimaryHDU(data=data)
    hdu.header["DATE-OBS"] = date_obs
    hdu.header["EXPTIME"] = 10.0
    hdu.header["GAIN"] = 50
    hdu.header["OBJECT"] = obj
    buf = io.BytesIO()
    pyfits.HDUList([hdu]).writeto(buf)
    return buf.getvalue()


@pytest.fixture()
def gtk():
    """GTK initialized, or skip when no display is available (headless)."""
    try:
        ok = Gtk.init_check()
    except Exception as exc:  # pragma: no cover - backend errors  # noqa: BLE001
        pytest.skip(f"Gtk.init_check failed: {exc}")
    if not ok:
        pytest.skip("no display available (headless environment)")
    return Gtk


@pytest.fixture()
def app(gtk, tmp_path):
    """A SeestarApplication with its settings file in tmp_path."""
    from seestar_controller.ui.app import SeestarApplication

    return SeestarApplication(settings_path=str(tmp_path / "settings.json"))


@pytest.fixture()
def project(tmp_path):
    """A tmp project with one session and two stored stacks."""
    p = create_project("M 42 night", root=tmp_path / "projects")
    session = start_session(p, clock=frozen(T0))
    store_frame(
        p, session, KIND_STACK,
        fits_bytes(synthetic_image(64, seed=1), date_obs=DATE1, obj="M 42"),
        meta={"target": "M 42"},
    )
    store_frame(
        p, session, KIND_STACK,
        fits_bytes(synthetic_image(64, seed=2), date_obs=DATE2, obj="M 31"),
        meta={"target": "M 31"},
    )
    yield p
    p.close()


@pytest.fixture()
def viewer_controller(project):
    return ViewerController(project)


@pytest.fixture()
def mixed_project(tmp_path):
    """Issue 05 fixture: a project with TWO subs interleaved with TWO stacks
    (jd order: sub1, stack1, sub2, stack2) — the subs would bury the stacks
    in an unfiltered list."""
    p = create_project("M 42 mixed", root=tmp_path / "projects")
    session = start_session(p, clock=frozen(T0))
    sub1 = store_frame(
        p, session, KIND_SUB,
        fits_bytes(synthetic_image(32, seed=21), date_obs=DATE1, obj="M 42"),
        meta={"target": "M 42"},
    )
    stack1 = store_frame(
        p, session, KIND_STACK,
        fits_bytes(synthetic_image(64, seed=22), date_obs=DATE2, obj="M 42"),
        meta={"target": "M 42"},
    )
    sub2 = store_frame(
        p, session, KIND_SUB,
        fits_bytes(synthetic_image(32, seed=23), date_obs="2026-11-20T22:00:20.000", obj="M 42"),
        meta={"target": "M 42"},
    )
    stack2 = store_frame(
        p, session, KIND_STACK,
        fits_bytes(synthetic_image(64, seed=24), date_obs="2026-11-20T22:00:30.000", obj="M 31"),
        meta={"target": "M 31"},
    )
    yield p, [sub1, stack1, sub2, stack2]
    p.close()


@pytest.fixture()
def viewer(gtk, viewer_controller):
    from seestar_controller.enhance.viewer import ImageViewer

    return ImageViewer(viewer_controller)


def test_build_view_enhance_wires_controllers(gtk):
    from seestar_controller.ui.views import EnhanceView, build_view

    vc = ViewerController()
    ec = EditorController(vc)
    view = build_view("enhance", viewer_controller=vc, editor_controller=ec)
    assert isinstance(view, EnhanceView)
    assert view.viewer_controller is vc
    assert view.editor_controller is ec
    assert view.editor_widget.controller is ec
    assert view.editor_widget.viewer_controller is vc
    # standalone: no controllers passed -> the view creates its own
    standalone = build_view("enhance")
    assert standalone.viewer_controller is not vc
    assert standalone.editor_controller is not ec
    assert (
        standalone.editor_controller.viewer_controller is standalone.viewer_controller
    )


def test_viewer_lists_stacks(viewer, viewer_controller):
    assert viewer.controller is viewer_controller
    assert viewer.row_count() == 2
    assert viewer.row_label(0) == viewer_controller.stacks[0].label()
    assert viewer.row_label(1) == viewer_controller.stacks[1].label()
    # issue 05: newest-first — the DATE2 stack (M 31) is on top
    assert "M 31" in viewer.row_label(0)
    assert "M 42" in viewer.row_label(1)
    assert viewer_controller.stacks[0].utc.startswith("2026-11-20T22:00:10")
    assert viewer_controller.stacks[1].utc.startswith("2026-11-20T22:00:00")
    # the row subtitle starts with the kind label (bolded via markup)
    assert viewer.row_subtitle(0).startswith("stack")
    assert viewer.row_subtitle(1).startswith("stack")
    # default: stacks only, show-all toggle off
    assert not viewer.show_all_button.get_active()
    assert not viewer_controller.show_all
    # nothing open yet: placeholder visible, no paintable
    assert viewer.picture.get_paintable() is None
    assert viewer.placeholder.get_visible()
    assert "no frame open" in viewer.placeholder.get_label()


def test_viewer_open_stack_shows_texture(viewer, viewer_controller):
    image = viewer_controller.open_stack(viewer_controller.stacks[0].frame_id)
    assert image.shape == (64, 64)
    assert viewer.picture.get_paintable() is not None
    assert not viewer.placeholder.get_visible()
    assert "M 31" in viewer.info_label.get_label()  # stacks[0] is the newest (DATE2)
    # the current stack's row is selected
    selected = viewer.list_box.get_selected_row()
    assert selected is not None and selected.frame_id == viewer_controller.stacks[0].frame_id


def test_viewer_row_selection_opens_stack(gtk, viewer, viewer_controller):
    row = viewer.list_box.get_row_at_index(1)
    viewer.list_box.select_row(row)
    assert viewer_controller.current_stack is not None
    assert viewer_controller.current_stack.frame_id == viewer_controller.stacks[1].frame_id
    assert viewer.picture.get_paintable() is not None
    assert "M 42" in viewer.info_label.get_label()  # row 1 = the older DATE1 stack


def test_viewer_refresh_button_relists(gtk, viewer, viewer_controller, project):
    from seestar_controller.store import get_open_session

    # store another stack after the viewer was built; Refresh picks it up
    # (the project fixture already has one open session — reuse it)
    session = get_open_session(project).id  # ty: ignore[missing-union-attribute]
    store_frame(
        project, session, KIND_STACK,
        fits_bytes(synthetic_image(32, seed=9), date_obs="2026-11-20T23:00:00.000", obj="M 33"),
        meta={"target": "M 33"},
    )
    assert viewer.row_count() == 2
    viewer.refresh_button.emit("clicked")
    assert viewer.row_count() == 3


def test_viewer_no_project_placeholder(gtk):
    from seestar_controller.enhance.viewer import ImageViewer

    viewer = ImageViewer(ViewerController())
    assert viewer.row_count() == 0
    assert viewer.picture.get_paintable() is None
    assert viewer.placeholder.get_visible()
    assert "no project" in viewer.placeholder.get_label()


# --- image-enhance issue 05: default list is stacks only (newest first), "Show all" toggle lists subs ---


def test_viewer_default_lists_stacks_only_newest_first(gtk, mixed_project):
    from seestar_controller.enhance.viewer import ImageViewer

    _p, (_sub1, stack1, _sub2, stack2) = mixed_project
    vc = ViewerController(_p)
    viewer = ImageViewer(vc)
    # default: ONLY the stacks, newest first -> [stack2 (M 31), stack1 (M 42)]
    assert viewer.row_count() == 2
    assert not viewer.show_all_button.get_active()
    assert not vc.show_all
    assert viewer.row_label(0) == vc.stacks[0].label()
    assert "M 31" in viewer.row_label(0)
    assert viewer.row_subtitle(0).startswith("stack")
    assert "M 42" in viewer.row_label(1)
    assert viewer.row_subtitle(1).startswith("stack")
    assert [s.frame_id for s in vc.stacks] == [stack2.id, stack1.id]


def test_viewer_show_all_button_lists_subs_and_sub_row_opens(gtk, mixed_project):
    from seestar_controller.enhance.viewer import ImageViewer

    _p, (sub1, stack1, sub2, stack2) = mixed_project
    vc = ViewerController(_p)
    viewer = ImageViewer(vc)
    assert viewer.row_count() == 2
    # flip the "Show all frames" check button next to Refresh
    viewer.show_all_button.set_active(True)
    assert vc.show_all
    assert viewer.row_count() == 4
    # everything listed, still newest first: stack2, sub2, stack1, sub1
    assert [viewer.row_subtitle(i).split(" ")[0] for i in range(4)] == [
        "stack", "sub", "stack", "sub",
    ]
    assert [s.frame_id for s in vc.stacks] == [stack2.id, sub2.id, stack1.id, sub1.id]
    # selecting a sub row opens the sub (any kind openable with show-all on)
    row = viewer.list_box.get_row_at_index(1)
    assert row.frame_id == sub2.id
    viewer.list_box.select_row(row)
    assert vc.current_stack is not None and vc.current_stack.frame_id == sub2.id
    assert vc.current_stack.kind == "sub"
    assert viewer.picture.get_paintable() is not None
    # toggling back hides the subs again; the sub stays openable by id
    viewer.show_all_button.set_active(False)
    assert not vc.show_all
    assert viewer.row_count() == 2
    image = vc.open_stack(sub1.id)
    assert image.shape == (32, 32)
    assert vc.current_stack is not None and vc.current_stack.frame_id == sub1.id


def test_app_owns_shared_controllers_and_window_passes_them_down(gtk, app):
    from seestar_controller.ui.app import SeestarWindow

    assert isinstance(app.viewer_controller, ViewerController)
    assert isinstance(app.editor_controller, EditorController)
    assert app.editor_controller.viewer_controller is app.viewer_controller
    window = SeestarWindow(app, app.state)
    assert window.viewer_controller is app.viewer_controller
    assert window.editor_controller is app.editor_controller
    enhance_page = window.view_stack.get_child_by_name("enhance")
    assert enhance_page.viewer_controller is app.viewer_controller
    assert enhance_page.editor_controller is app.editor_controller
    assert enhance_page.editor_widget.controller is app.editor_controller
    assert enhance_page.editor_widget.viewer_controller is app.viewer_controller


# --- ticket 03: the EnhanceEditor widget (sliders, before/after, undo/redo/reset) -------------------


def pump_main(timeout: float = 5.0) -> None:
    """Drain the default GLib main context (runs the queued ``idle_add``
    callbacks — the editor controller's ``invoke_on_main`` in these tests)."""
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if context.pending():
            context.iteration(False)
        else:
            return


@pytest.fixture()
def editor(gtk, viewer_controller):
    """An EnhanceEditor over the shared viewer controller, with the editor
    controller marshalling render results via GLib.idle_add (like the app)."""
    from seestar_controller.enhance.enhance_view import EnhanceEditor

    ec = EditorController(
        viewer_controller,
        invoke_on_main=GLib.idle_add,
        render_max_size=32,
        debounce_s=0.0,
    )
    return EnhanceEditor(viewer_controller, ec)


def _open_first_stack(editor, viewer_controller) -> None:
    viewer_controller.open_stack(viewer_controller.stacks[0].frame_id)
    assert editor.controller.wait(5.0)
    pump_main()


def test_editor_no_project_placeholder(gtk):
    from seestar_controller.enhance.enhance_view import EnhanceEditor

    widget = EnhanceEditor(ViewerController())
    assert widget.row_count() == 0
    assert widget.slider_count() == 0
    assert widget.placeholder.get_visible()
    assert "no project" in widget.placeholder.get_label()


def test_editor_builds_sliders_per_op_param(editor, viewer_controller):
    _open_first_stack(editor, viewer_controller)
    assert editor.row_count() == 2
    assert editor.op_group_titles() == ["stretch", "denoise", "sharpen"]
    # 6 numeric sliders + 1 dropdown for the default pipeline
    assert editor.slider_count() == 6
    for key in (
        ("stretch", "sigma"),
        ("stretch", "bins"),
        ("denoise", "strength"),
        ("denoise", "radius"),
        ("sharpen", "amount"),
        ("sharpen", "radius"),
    ):
        assert editor.slider(*key) is not None, key
    assert editor.dropdown("stretch", "kind") is not None
    # the slider values match the default pipeline params
    assert editor.slider("denoise", "strength").get_value() == pytest.approx(0.25)
    assert editor.slider("sharpen", "amount").get_value() == pytest.approx(0.5)
    assert editor.slider("stretch", "sigma").get_value() == pytest.approx(10.0)
    # both before/after previews rendered, placeholder hidden, spinner off
    assert editor.before_picture.get_paintable() is not None
    assert editor.after_picture.get_paintable() is not None
    assert not editor.placeholder.get_visible()
    assert not editor.spinner.get_visible()
    assert "M 31" in editor.info_label.get_label()  # stacks[0] = newest (DATE2)


def test_editor_slider_move_calls_set_param(editor, viewer_controller):
    _open_first_stack(editor, viewer_controller)
    ec = editor.controller
    assert not ec.can_undo
    assert not editor.undo_button.get_sensitive()
    editor.slider("denoise", "strength").set_value(0.8)
    assert ec.pipeline.ops[1].params.strength == pytest.approx(0.8)
    assert ec.can_undo
    assert editor.undo_button.get_sensitive()
    assert editor.controller.wait(5.0)
    pump_main()
    assert editor.after_picture.get_paintable() is not None
    assert not editor.spinner.get_visible()


def test_editor_dropdown_calls_set_param(editor, viewer_controller):
    _open_first_stack(editor, viewer_controller)
    dropdown = editor.dropdown("stretch", "kind")
    dropdown.set_selected(STRETCH_KINDS.index("linear"))
    assert editor.controller.pipeline.ops[0].params.kind == "linear"
    assert editor.controller.can_undo


def test_editor_before_after_toggle_switches_preview(editor, viewer_controller):
    _open_first_stack(editor, viewer_controller)
    assert editor.view_stack.get_visible_child_name() == "after"
    editor.before_after_toggle.set_active(True)
    assert editor.view_stack.get_visible_child_name() == "before"
    editor.before_after_toggle.set_active(False)
    assert editor.view_stack.get_visible_child_name() == "after"
    # the two pictures carry different paintables (before != after)
    assert editor.before_picture.get_paintable() is not None
    assert editor.after_picture.get_paintable() is not None


def test_editor_undo_redo_reset_buttons(editor, viewer_controller):
    _open_first_stack(editor, viewer_controller)
    ec = editor.controller
    assert not editor.undo_button.get_sensitive()
    assert not editor.redo_button.get_sensitive()
    editor.slider("sharpen", "amount").set_value(2.0)
    assert editor.undo_button.get_sensitive()
    assert not editor.redo_button.get_sensitive()
    editor.undo_button.emit("clicked")
    assert ec.pipeline.ops[2].params.amount == pytest.approx(0.5)
    assert editor.redo_button.get_sensitive()
    # the slider re-synced to the undone value
    assert editor.slider("sharpen", "amount").get_value() == pytest.approx(0.5)
    editor.redo_button.emit("clicked")
    assert ec.pipeline.ops[2].params.amount == pytest.approx(2.0)
    assert editor.slider("sharpen", "amount").get_value() == pytest.approx(2.0)
    editor.reset_button.emit("clicked")
    assert ec.pipeline == default_pipeline()
    assert editor.slider("sharpen", "amount").get_value() == pytest.approx(0.5)
    assert ec.wait(5.0)
    pump_main()


def test_editor_row_selection_opens_stack(editor, viewer_controller):
    row = editor.list_box.get_row_at_index(1)
    editor.list_box.select_row(row)
    assert viewer_controller.current_stack is not None
    assert viewer_controller.current_stack.frame_id == viewer_controller.stacks[1].frame_id
    assert editor.controller.wait(5.0)
    pump_main()
    assert editor.after_picture.get_paintable() is not None
    assert "M 42" in editor.info_label.get_label()  # row 1 = the older DATE1 stack


# --- ticket 04: persistence buttons (Save enhanced / Save preset / Export PNG / Export JPEG) ----------


def test_editor_persistence_buttons_exist_and_follow_stack_state(gtk):
    from seestar_controller.enhance.enhance_view import EnhanceEditor

    widget = EnhanceEditor(ViewerController())
    for button in (
        widget.save_enhanced_button,
        widget.save_preset_button,
        widget.export_png_button,
        widget.export_jpeg_button,
    ):
        assert button is not None
        assert not button.get_sensitive()  # no stack open yet


def test_editor_persistence_buttons_sensitive_with_open_stack(editor, viewer_controller):
    _open_first_stack(editor, viewer_controller)
    for button in (
        editor.save_enhanced_button,
        editor.save_preset_button,
        editor.export_png_button,
        editor.export_jpeg_button,
    ):
        assert button.get_sensitive()


def test_editor_save_preset_button_stores_preset(editor, viewer_controller, project):
    from seestar_controller.store import load_preset

    _open_first_stack(editor, viewer_controller)
    ec = editor.controller
    editor.slider("denoise", "strength").set_value(0.8)
    assert not ec.snapshot.preset_saved
    editor.save_preset_button.emit("clicked")
    assert ec.snapshot.preset_saved
    assert "preset saved" in editor.info_label.get_label()
    stack = viewer_controller.current_stack
    preset = load_preset(project, stack.frame_id)
    assert preset is not None
    assert preset.pipeline["ops"][1]["params"]["strength"] == pytest.approx(0.8)


def test_editor_save_enhanced_button_stores_enhanced_frame(editor, viewer_controller, project):
    from seestar_controller.store import get_frame_path, list_frames

    _open_first_stack(editor, viewer_controller)
    ec = editor.controller
    stack_ids_before = {f.id for f in list_frames(project)}
    editor.save_enhanced_button.emit("clicked")
    frame = ec.snapshot.saved_frame
    assert frame is not None and frame.kind == "enhanced"
    assert frame.id not in stack_ids_before
    assert get_frame_path(project, frame).is_file()
    assert [f.id for f in list_frames(project, kind="enhanced")] == [frame.id]
    assert "saved" in editor.info_label.get_label()


def test_editor_export_buttons_write_files(editor, viewer_controller, tmp_path):
    _open_first_stack(editor, viewer_controller)
    # the FileDialog itself needs a display interaction; drive the export
    # path the dialog callback marshals into (invoke_on_main -> _do_export)
    editor._do_export(str(tmp_path / "enhanced.png"), "PNG")
    editor._do_export(str(tmp_path / "enhanced.jpg"), "JPEG")
    assert (tmp_path / "enhanced.png").is_file()
    assert (tmp_path / "enhanced.jpg").is_file()
    assert "exported" in editor.status_label.get_label()


def test_enhance_view_exposes_persistence_buttons(gtk):
    from seestar_controller.ui.views import EnhanceView, build_view

    view = build_view("enhance")
    assert isinstance(view, EnhanceView)
    assert view.save_enhanced_button is view.editor_widget.save_enhanced_button
    assert view.save_preset_button is view.editor_widget.save_preset_button
    assert view.export_png_button is view.editor_widget.export_png_button
    assert view.export_jpeg_button is view.editor_widget.export_jpeg_button


# --- app-improvement ticket 08: stitched mosaics listed + openable in the Enhance view -------------

#: geometry of the synthetic WCS tiles used by the Projects-view stitch test
M_CANVAS = 48
M_TILE = 32
M_SCALE = 0.001


def _write_mosaic(project, session_id: str, size: int = 96, seed: int = 5):
    """Write a synthetic mosaic FITS where :func:`stitch_session` puts it
    (``mosaics/<session_id>.fits``) and return its path."""
    path = mosaic_path(project, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    hdu = pyfits.PrimaryHDU(data=synthetic_image(size, seed=seed))
    hdu.header["DATE-OBS"] = DATE2
    hdu.header["EXPTIME"] = 10.0
    hdu.header["OBJECT"] = "M 42"
    buf = io.BytesIO()
    pyfits.HDUList([hdu]).writeto(buf)
    path.write_bytes(buf.getvalue())
    return path


def _mosaic_row_index(view) -> int:
    """The index of the file-based (mosaic) row in a viewer/editor list."""
    for i in range(view.row_count()):
        row = view.list_box.get_row_at_index(i)
        if getattr(row, "frame_id", None) is None:
            return i
    raise AssertionError("no file-based (mosaic) row listed")


def _wcs_tile_bytes(canvas, x0: int, y0: int, i: int, size: int = M_TILE) -> bytes:
    """A 32x32 FITS cutout of the canvas at ``(x0, y0)`` carrying the WCS of
    the cutout (CRVAL = the canvas sky position of its center pixel) — the
    shape :func:`~seestar_controller.mosaic.stitch_session` consumes."""
    from astropy.wcs import WCS

    hdu = pyfits.PrimaryHDU(
        data=np.asarray(canvas[y0 : y0 + size, x0 : x0 + size], dtype=np.float32)
    )
    cw = WCS(
        {
            "CTYPE1": "RA---CAR",
            "CTYPE2": "DEC--CAR",
            "CRVAL1": 83.0,
            "CRVAL2": 22.0,
            "CRPIX1": (canvas.shape[1] + 1) / 2.0,
            "CRPIX2": (canvas.shape[0] + 1) / 2.0,
            "CDELT1": -M_SCALE,
            "CDELT2": M_SCALE,
        }
    )
    sky = cw.pixel_to_world(x0 + (size - 1) / 2.0, y0 + (size - 1) / 2.0)
    hdu.header["CTYPE1"] = "RA---CAR"
    hdu.header["CTYPE2"] = "DEC--CAR"
    hdu.header["CRVAL1"] = float(sky.ra.deg)
    hdu.header["CRVAL2"] = float(sky.dec.deg)
    hdu.header["CRPIX1"] = (size + 1) / 2.0
    hdu.header["CRPIX2"] = (size + 1) / 2.0
    hdu.header["CDELT1"] = -M_SCALE
    hdu.header["CDELT2"] = M_SCALE
    instant = T0 + timedelta(seconds=10 * i)
    hdu.header["DATE-OBS"] = instant.isoformat(timespec="milliseconds").replace("+00:00", "")
    hdu.header["EXPTIME"] = 10.0
    hdu.header["OBJECT"] = "M 42"
    buf = io.BytesIO()
    pyfits.HDUList([hdu]).writeto(buf)
    return buf.getvalue()


def test_viewer_lists_and_opens_stitched_mosaic(gtk, viewer, viewer_controller, project):
    """A stitched mosaic (file-based, NOT a ``frames`` row) shows up as an
    extra row in the image viewer after Refresh, and selecting the row opens
    it by path in the same viewer (open-path dispatch on ``frame_id=None``)."""
    from seestar_controller.store import get_open_session

    session_id = get_open_session(project).id  # ty: ignore[missing-union-attribute]
    assert viewer.row_count() == 2  # only the two stored stacks so far
    mosaic = _write_mosaic(project, session_id)
    viewer.refresh_button.emit("clicked")
    assert viewer.row_count() == 3
    idx = _mosaic_row_index(viewer)
    assert "mosaic" in viewer.row_label(idx) and session_id in viewer.row_label(idx)
    assert "mosaic" in viewer.row_subtitle(idx)
    row = viewer.list_box.get_row_at_index(idx)
    assert getattr(row, "frame_id", None) is None
    assert getattr(row, "entry_path", None) == str(mosaic)
    # selecting the mosaic row opens the file in the viewer
    viewer.list_box.select_row(row)
    current = viewer_controller.current_stack
    assert current is not None
    assert current.kind == "mosaic" and current.frame_id is None
    assert current.path == mosaic
    image = viewer_controller.current_image
    assert image is not None and image.shape == (96, 96)
    assert viewer.picture.get_paintable() is not None
    assert not viewer.placeholder.get_visible()
    assert "mosaic" in viewer.info_label.get_label()
    assert viewer.list_box.get_selected_row() is row


def test_editor_row_selection_opens_mosaic(editor, viewer_controller, project):
    """The EnhanceEditor's entry list carries the mosaic row too; selecting
    it opens the mosaic by path and the editor renders before/after."""
    from seestar_controller.store import get_open_session

    session_id = get_open_session(project).id  # ty: ignore[missing-union-attribute]
    _write_mosaic(project, session_id)
    viewer_controller.refresh_stacks()  # the editor re-lists on the snapshot
    assert editor.row_count() == 3
    idx = _mosaic_row_index(editor)
    assert "mosaic" in editor.row_label(idx)
    editor.list_box.select_row(editor.list_box.get_row_at_index(idx))
    current = viewer_controller.current_stack
    assert current is not None and current.kind == "mosaic" and current.frame_id is None
    assert editor.controller.wait(5.0)
    pump_main()
    assert editor.before_picture.get_paintable() is not None
    assert editor.after_picture.get_paintable() is not None
    assert not editor.placeholder.get_visible()
    assert "mosaic" in editor.info_label.get_label()


def test_projects_view_stitch_opens_mosaic_in_enhance_view(gtk, tmp_path):
    """End to end (the ticket's acceptance): stitching a session from the
    Projects view writes ``mosaics/<sid>.fits`` AND opens the mosaic in the
    shared image viewer (by path — it is not a ``frames`` row) and switches
    to the Enhance view, where the mosaic is listed as a row."""
    from seestar_controller.store import close_session
    from seestar_controller.ui.project_controller import ProjectController
    from seestar_controller.ui.state import AppState
    from seestar_controller.ui.views import build_view

    root = tmp_path / "projects"
    root.mkdir()
    project = create_project("M 42 night", root=root)
    sid = start_session(project, clock=frozen(T0))
    # two overlapping WCS tiles: 32x32 cutouts of a 48x48 canvas at (0,0)/(16,0)
    yy, xx = np.mgrid[0:M_CANVAS, 0:M_CANVAS]
    canvas = np.full((M_CANVAS, M_CANVAS), 100.0)
    canvas = canvas + 9000.0 * np.exp(-(((yy - 16.0) ** 2 + (xx - 16.0) ** 2) / 8.0))
    canvas = canvas + 6000.0 * np.exp(-(((yy - 16.0) ** 2 + (xx - 40.0) ** 2) / 8.0))
    for i, (x0, y0) in enumerate([(0, 0), (16, 0)]):
        store_frame(
            project, sid, KIND_STACK, _wcs_tile_bytes(canvas, x0, y0, i),
            meta={"target": "M 42"},
        )
    close_session(project, sid)
    project_path = project.path
    project.close()

    state = AppState(tmp_path / "settings.json")
    state.set_setting("project_root", str(root))
    controller = ProjectController(state)
    controller.open_project(project_path)
    vc = ViewerController()
    shown: list[str] = []
    page = build_view(
        "projects", project_controller=controller, viewer_controller=vc,
        show_view=shown.append,
    )
    pump_main()
    assert page.session_count() == 1
    page.click_stitch(0)
    pump_main()
    # the mosaic was written and opened in the shared viewer by path
    mosaic = mosaic_path(project, sid)
    assert mosaic.is_file() and mosaic.stat().st_size > 0
    current = vc.current_stack
    assert current is not None and current.kind == "mosaic" and current.frame_id is None
    assert current.path == mosaic
    assert vc.current_image is not None and vc.current_image.shape == (M_TILE, M_TILE + 16)
    assert shown == ["enhance"]
    label = page.data_status_label.get_label()
    assert "stitched mosaic" in label and "opened in the image viewer" in label
    # the Enhance view over the same shared controller lists the mosaic as a row
    enhance = build_view("enhance", viewer_controller=vc, editor_controller=EditorController(vc))
    assert enhance.editor_widget.row_count() == 3
    idx = _mosaic_row_index(enhance.editor_widget)
    assert "mosaic" in enhance.editor_widget.row_label(idx) and sid in enhance.editor_widget.row_label(idx)
