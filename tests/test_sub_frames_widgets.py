"""Sub-frames issue 01: WIDGET tests — the Enhance-view frame list shows the
stored frames of the current project behind the "Show all frames" toggle
(image-enhance issue 05: the default list is stacks only, newest first; the
toggle adds the subs/enhanced frames, each row subtitled with its kind),
clicking a row opens the frame in the viewer, and the Projects view's
Stored-data browser frame rows open the clicked frame in the shared image
viewer (switching to the Enhance view when the app passes ``show_view``).

These SKIP in the headless sandbox (no ``gi`` module, no GTK system libs, no
display; see docs/wiki/assumptions.md) and run on the desktop, where the
system packages ``python3-gi gir1.2-gtk-4.0 gir1.2-adw-1`` are installed (or
under ``xvfb-run -a uv run pytest``).

Guards, in order: ``pytest.importorskip("gi")`` (no PyGObject at all),
``gi.require_version`` for the Gtk 4.0 / Adw 1 typelibs, and a
``Gtk.init_check()`` fixture (no display -> skip) — the same pattern as
tests/test_enhance_widgets.py. Rows are clicked with ``.emit("clicked")``
(NOT ``.clicked()``), and widgets are never ``destroy()``-ed (GTK4).

The pure listing/opening behaviour (``list_project_frames``,
``ViewerController.open_stack`` on sub frame ids, RGB subs, the no-gi
subprocess boundary) is covered by tests/test_sub_frames_viewer.py.
"""

import io
from datetime import UTC, datetime

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

from seestar_controller.enhance import EditorController, EnhanceError, ViewerController
from seestar_controller.store import (
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
    """A small synthetic sub image (gradient + star peak + noise)."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:size, 0:size]
    data = ((xx + yy) * 10.0).astype(np.float32)
    data = data + (8000.0 * np.exp(-(((yy - 32) ** 2 + (xx - 32) ** 2) / 8.0))).astype(
        np.float32
    )
    return (data + rng.normal(0.0, 5.0, size=(size, size))).astype(np.float32)


def fits_bytes(data: np.ndarray, *, date_obs: str, obj: str = "NGC7789") -> bytes:
    hdu = pyfits.PrimaryHDU(data=data)
    hdu.header["DATE-OBS"] = date_obs
    hdu.header["EXPTIME"] = 10.0
    hdu.header["GAIN"] = 50
    hdu.header["OBJECT"] = obj
    buf = io.BytesIO()
    pyfits.HDUList([hdu]).writeto(buf)
    return buf.getvalue()


def pump_main() -> None:
    """Run all pending GLib main-context callbacks."""
    ctx = GLib.MainContext.default()
    while ctx.pending():
        ctx.iteration(False)


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
def subs_only_project(tmp_path):
    """The issue's demo case: a project with ONLY two subs (empty stacks/),
    stored before any view is built."""
    project = create_project("NGC7789", root=tmp_path / "projects")
    session = start_session(project, clock=frozen(T0))
    sub1 = store_frame(
        project, session, KIND_SUB,
        fits_bytes(synthetic_image(64, seed=11), date_obs=DATE1),
        meta={"target": "NGC7789"},
    )
    sub2 = store_frame(
        project, session, KIND_SUB,
        fits_bytes(synthetic_image(64, seed=12), date_obs=DATE2),
        meta={"target": "NGC7789"},
    )
    yield project, [sub1, sub2]
    project.close()


@pytest.fixture()
def viewer_controller(subs_only_project):
    project, _subs = subs_only_project
    return ViewerController(project)


# --- the Enhance-view frame list (ImageViewer / EnhanceEditor) ----------------------


def test_viewer_lists_subs_with_kind_in_subtitle(gtk, viewer_controller, subs_only_project):
    """The viewer's frame list defaults to stacks only (empty for this
    subs-only project — issue 05); the "Show all frames" check button lists
    the subs, NEWEST FIRST, each row subtitled with its kind."""
    from seestar_controller.enhance.viewer import ImageViewer

    _project, subs = subs_only_project
    viewer = ImageViewer(viewer_controller)
    assert viewer.row_count() == 0  # default: stacks only, none stored
    assert not viewer.show_all_button.get_active()
    assert not viewer_controller.show_all
    viewer.show_all_button.set_active(True)
    assert viewer_controller.show_all
    assert viewer.row_count() == 2
    # newest first: subs[1] (DATE2) is row 0
    for index, sub in enumerate([subs[1], subs[0]]):
        assert viewer.row_label(index) == viewer_controller.stacks[index].label()
        assert "NGC7789" in viewer.row_label(index)
        assert viewer.row_subtitle(index).startswith("sub")
        row = viewer.list_box.get_row_at_index(index)
        assert row is not None and row.frame_id == sub.id
    # nothing open yet: placeholder visible, no paintable
    assert viewer.picture.get_paintable() is None
    assert viewer.placeholder.get_visible()
    assert "no frame open" in viewer.placeholder.get_label()


def test_viewer_row_select_opens_sub(gtk, viewer_controller, subs_only_project):
    """Clicking a sub row opens the sub: the picture gets a paintable, the
    placeholder hides, the controller's current frame is the sub."""
    from seestar_controller.enhance.viewer import ImageViewer

    _project, subs = subs_only_project
    viewer = ImageViewer(viewer_controller)
    viewer.show_all_button.set_active(True)  # issue 05: list the subs
    row = viewer.list_box.get_row_at_index(0)  # newest first -> subs[1]
    viewer.list_box.select_row(row)
    pump_main()
    assert viewer_controller.current_stack is not None
    assert viewer_controller.current_stack.frame_id == subs[1].id
    assert viewer_controller.current_stack.kind == "sub"
    assert viewer.picture.get_paintable() is not None
    assert not viewer.placeholder.get_visible()
    assert "NGC7789" in viewer.info_label.get_label()
    selected = viewer.list_box.get_selected_row()
    assert selected is not None and selected.frame_id == subs[1].id


def test_enhance_editor_lists_subs(gtk, viewer_controller, subs_only_project):
    """The Enhance view's editor frame list shows the subs too once the
    show-all toggle is on (kind in the subtitle), and selecting a row opens
    the sub through the controller."""
    from seestar_controller.enhance.enhance_view import EnhanceEditor

    _project, subs = subs_only_project
    editor = EnhanceEditor(viewer_controller, EditorController(viewer_controller))
    assert editor.row_count() == 0  # default: stacks only
    editor.show_all_button.set_active(True)
    assert editor.row_count() == 2
    # newest first: subs[1] row 0, subs[0] row 1
    assert editor.row_subtitle(0).startswith("sub")
    assert editor.row_subtitle(1).startswith("sub")
    assert "NGC7789" in editor.row_label(0)
    row = editor.list_box.get_row_at_index(0)
    editor.list_box.select_row(row)
    pump_main()
    assert viewer_controller.current_stack is not None
    assert viewer_controller.current_stack.frame_id == subs[1].id
    assert viewer_controller.current_stack.kind == "sub"


# --- the Projects-view Stored-data browser frame rows ---------------------------------


def _projects_page(tmp_path, subs_only_project, *, viewer_controller=None, show_view=None):
    """Build the Projects view over the subs-only project with a shared
    viewer controller (and an optional ``show_view`` recorder)."""
    from seestar_controller.ui.project_controller import ProjectController
    from seestar_controller.ui.state import AppState
    from seestar_controller.ui.views import build_view

    project, subs = subs_only_project
    state = AppState(tmp_path / "settings.json")
    state.set_setting("project_root", str(project.path.parent))
    controller = ProjectController(state)
    controller.open_project(project.path)
    vc = viewer_controller if viewer_controller is not None else ViewerController()
    shown: list[str] = []
    page = build_view(
        "projects",
        project_controller=controller,
        viewer_controller=vc,
        show_view=show_view if show_view is not None else shown.append,
    )
    pump_main()
    return page, vc, shown, subs


def test_projects_view_frame_rows_open_sub_in_viewer(gtk, tmp_path, subs_only_project):
    """Clicking a Stored-data frame row opens the frame (a sub here) in the
    shared image viewer and switches to the Enhance view."""
    page, vc, shown, subs = _projects_page(tmp_path, subs_only_project)
    assert page.viewer_controller is vc
    assert page.session_count() == 1
    assert page.frame_row_count(0) == 2
    assert "sub" in page.frame_row_label(0, 0)
    assert "sub" in page.frame_row_label(0, 1)
    assert "NGC7789" in page.frame_row_label(0, 0)
    # click the first frame row (a sub) — .emit("clicked"), never .clicked()
    page.click_frame(0, 0)
    pump_main()
    assert vc.current_stack is not None
    assert vc.current_stack.frame_id == subs[0].id
    assert vc.current_stack.kind == "sub"
    assert vc.current_image is not None and vc.current_image.shape == (64, 64)
    assert shown == ["enhance"]
    assert "opened frame" in page.data_status_label.get_label()
    assert str(subs[0].id) in page.data_status_label.get_label()
    # the second row opens the other sub
    page.click_frame(0, 1)
    pump_main()
    assert vc.current_stack is not None and vc.current_stack.frame_id == subs[1].id
    assert shown == ["enhance", "enhance"]


def test_projects_view_frame_row_reports_open_error(gtk, tmp_path, subs_only_project, monkeypatch):
    """When the viewer cannot open the frame, the status label shows the
    error and no view switch happens — the click never raises."""
    page, vc, shown, subs = _projects_page(tmp_path, subs_only_project)

    def _boom(frame_id):
        raise EnhanceError(f"no frame with frame id {frame_id}")

    monkeypatch.setattr(vc, "open_stack", _boom)
    page.click_frame(0, 0)
    pump_main()
    assert vc.current_stack is None
    assert shown == []
    label = page.data_status_label.get_label()
    assert "cannot open frame" in label
    assert str(subs[0].id) in label


def test_projects_view_without_show_view_still_opens_frame(gtk, tmp_path, subs_only_project):
    """Without a ``show_view`` callback the frame still opens in the viewer
    (standalone view); only the view switch is skipped."""
    from seestar_controller.ui.project_controller import ProjectController
    from seestar_controller.ui.state import AppState
    from seestar_controller.ui.views import build_view

    project, subs = subs_only_project
    state = AppState(tmp_path / "settings.json")
    state.set_setting("project_root", str(project.path.parent))
    controller = ProjectController(state)
    controller.open_project(project.path)
    vc = ViewerController()
    page = build_view("projects", project_controller=controller, viewer_controller=vc)
    pump_main()
    page.click_frame(0, 0)
    pump_main()
    assert vc.current_stack is not None and vc.current_stack.frame_id == subs[0].id
    assert "opened frame" in page.data_status_label.get_label()


def test_window_projects_page_uses_the_shared_viewer_controller(gtk, tmp_path, subs_only_project):
    """The running window's Projects page shares the app's viewer controller,
    so a frame opened from the browser lands in the same viewer the Enhance
    view shows."""
    from seestar_controller.ui.app import SeestarApplication, SeestarWindow
    from seestar_controller.ui.views import ProjectsView

    project, subs = subs_only_project
    app = SeestarApplication(settings_path=str(tmp_path / "settings.json"))
    app.state.set_setting("project_root", str(project.path.parent))
    app.state.save()
    app.project_controller.refresh()
    window = SeestarWindow(app, app.state)
    window.open_project_offline(project.path)
    pump_main()
    page = window.view_stack.get_child_by_name("projects")
    assert isinstance(page, ProjectsView)
    assert page.viewer_controller is app.viewer_controller
    assert page.frame_row_count(0) == 2
    page.click_frame(0, 0)
    pump_main()
    assert app.viewer_controller.current_stack is not None
    assert app.viewer_controller.current_stack.frame_id == subs[0].id
    assert app.viewer_controller.current_stack.kind == "sub"
    # the app wired show_view: the window switched to the Enhance view
    assert window.view_stack.get_visible_child_name() == "enhance"
