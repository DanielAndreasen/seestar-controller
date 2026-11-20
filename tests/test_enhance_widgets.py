"""Widget-layer tests for the Enhance-view image viewer — these SKIP in the
headless sandbox (no ``gi`` module, no GTK system libs, no display; see
docs/wiki/assumptions.md) and run on the desktop, where the system packages
``python3-gi gir1.2-gtk-4.0 gir1.2-adw-1`` are installed (or under
``xvfb-run -a uv run pytest``).

Guards, in order: ``pytest.importorskip("gi")`` (no PyGObject at all),
``gi.require_version`` for the Gtk 4.0 / Adw 1 typelibs, and a
``Gtk.init_check()`` fixture (no display -> skip) — the same pattern as
tests/test_observing_widgets.py.

What is asserted when they run (image-enhance ticket 01): the
:class:`~seestar_controller.enhance.viewer.ImageViewer` renders one row per
project stack (``row_count`` / ``row_label`` matching
:meth:`~seestar_controller.enhance.fits_io.StackInfo.label`), selecting a row
opens that stack through the pure
:class:`~seestar_controller.enhance.viewer_controller.ViewerController` (the
Gtk.Picture gets a paintable texture, the placeholder hides, the info label
shows the target), with no project the view shows the "no project"
placeholder, and the wiring: ``build_view("enhance", viewer_controller=...)``
hands the shared controller to the Enhance view, the app owns one shared
:class:`~seestar_controller.enhance.ViewerController`, and the window passes
it down to the Enhance page.
"""

import io
from datetime import datetime, timezone

import numpy as np
import pytest
from astropy.io import fits as pyfits

gi = pytest.importorskip("gi")

try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
except ValueError as exc:  # typelibs missing
    pytest.skip(f"GTK 4.0 / libadwaita typelibs missing: {exc}", allow_module_level=True)

from gi.repository import Gtk  # noqa: E402

from seestar_controller.enhance import ViewerController  # noqa: E402
from seestar_controller.store import KIND_STACK, create_project, start_session, store_frame  # noqa: E402

T0 = datetime(2026, 11, 20, 22, 0, 0, tzinfo=timezone.utc)
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
    except Exception as exc:  # pragma: no cover - backend errors
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
def viewer(gtk, viewer_controller):
    from seestar_controller.enhance.viewer import ImageViewer

    return ImageViewer(viewer_controller)


def test_build_view_enhance_wires_controller(gtk):
    from seestar_controller.ui.views import EnhanceView, build_view

    vc = ViewerController()
    view = build_view("enhance", viewer_controller=vc)
    assert isinstance(view, EnhanceView)
    assert view.viewer_controller is vc
    assert view.viewer_widget.controller is vc
    # standalone: no controller passed -> the view creates its own
    standalone = build_view("enhance")
    assert standalone.viewer_controller is not vc


def test_viewer_lists_stacks(viewer, viewer_controller):
    assert viewer.controller is viewer_controller
    assert viewer.row_count() == 2
    assert viewer.row_label(0) == viewer_controller.stacks[0].label()
    assert viewer.row_label(1) == viewer_controller.stacks[1].label()
    assert "M 42" in viewer.row_label(0)
    # nothing open yet: placeholder visible, no paintable
    assert viewer.picture.get_paintable() is None
    assert viewer.placeholder.get_visible()
    assert "no stack open" in viewer.placeholder.get_label()


def test_viewer_open_stack_shows_texture(viewer, viewer_controller):
    image = viewer_controller.open_stack(viewer_controller.stacks[0].frame_id)
    assert image.shape == (64, 64)
    assert viewer.picture.get_paintable() is not None
    assert not viewer.placeholder.get_visible()
    assert "M 42" in viewer.info_label.get_label()
    # the current stack's row is selected
    selected = viewer.list_box.get_selected_row()
    assert selected is not None and selected.frame_id == viewer_controller.stacks[0].frame_id


def test_viewer_row_selection_opens_stack(gtk, viewer, viewer_controller):
    row = viewer.list_box.get_row_at_index(1)
    viewer.list_box.select_row(row)
    assert viewer_controller.current_stack is not None
    assert viewer_controller.current_stack.frame_id == viewer_controller.stacks[1].frame_id
    assert viewer.picture.get_paintable() is not None
    assert "M 31" in viewer.info_label.get_label()


def test_viewer_refresh_button_relists(gtk, viewer, viewer_controller, project):
    from seestar_controller.store import start_session as _start

    # store another stack after the viewer was built; Refresh picks it up
    session = _start(project, clock=frozen(T0))
    store_frame(
        project, session, KIND_STACK,
        fits_bytes(synthetic_image(32, seed=9), date_obs="2026-11-20T23:00:00.000", obj="M 33"),
        meta={"target": "M 33"},
    )
    assert viewer.row_count() == 2
    viewer.refresh_button.clicked()
    assert viewer.row_count() == 3


def test_viewer_no_project_placeholder(gtk):
    from seestar_controller.enhance.viewer import ImageViewer

    viewer = ImageViewer(ViewerController())
    assert viewer.row_count() == 0
    assert viewer.picture.get_paintable() is None
    assert viewer.placeholder.get_visible()
    assert "no project" in viewer.placeholder.get_label()


def test_app_owns_shared_viewer_controller_and_window_passes_it_down(gtk, app):
    from seestar_controller.ui.app import SeestarWindow

    assert isinstance(app.viewer_controller, ViewerController)
    window = SeestarWindow(app, app.state)
    assert window.viewer_controller is app.viewer_controller
    enhance_page = window.view_stack.get_child_by_name("enhance")
    assert enhance_page.viewer_controller is app.viewer_controller
    assert enhance_page.viewer_widget.controller is app.viewer_controller
