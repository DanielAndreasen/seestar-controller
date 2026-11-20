"""Widget-layer tests for the Photometry view's star picker — these SKIP in
the headless sandbox (no ``gi`` module, no GTK system libs, no display; see
docs/wiki/assumptions.md) and run on the desktop, where the system packages
``python3-gi gir1.2-gtk-4.0 gir1.2-adw-1`` are installed (or under
``xvfb-run -a uv run pytest``).

Guards, in order: ``pytest.importorskip("gi")`` (no PyGObject at all),
``gi.require_version`` for the Gtk 4.0 / Adw 1 typelibs, and a
``Gtk.init_check()`` fixture (no display -> skip) — the same pattern as
tests/test_enhance_widgets.py.

What is asserted when they run (photometry ticket 01): the wiring —
``build_view("photometry", picker_controller=..., viewer_controller=...)``
hands the shared controllers to the Photometry view (standalone construction
creates its own), the app owns one shared
:class:`~seestar_controller.photometry.PickController` following its shared
viewer controller, and the window passes both down to the Photometry page;
and the :class:`~seestar_controller.photometry.star_picker.StarPickerView`
behavior — opening a stack shows the preview texture, a click
(``_on_pressed`` with widget coords from ``image_to_widget``) adds a pick
with the dropdown's role, ``pick_marks()`` returns one aperture mark per pick,
the radius spin updates the controller's aperture, and the Save picks button
persists the picks into the project (``load_star_picks``).
"""

import io
import time
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

from gi.repository import GLib, Gtk  # noqa: E402

from seestar_controller.enhance import ViewerController  # noqa: E402
from seestar_controller.photometry import PickController  # noqa: E402
from seestar_controller.store import (  # noqa: E402
    KIND_STACK,
    create_project,
    load_star_picks,
    start_session,
    store_frame,
)

T0 = datetime(2026, 11, 20, 22, 0, 0, tzinfo=timezone.utc)
DATE1 = "2026-11-20T22:00:00.000"


def frozen(dt: datetime):
    return lambda: dt


def wcs_header() -> dict:
    """A plain-dict FITS header with a simple CAR WCS (astropy.wcs.WCS)."""
    from astropy.wcs import WCS

    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---CAR", "DEC--CAR"]
    w.wcs.crval = [83.0, 22.0]
    w.wcs.crpix = [33.0, 33.0]
    w.wcs.cdelt = [-0.001, 0.001]
    return dict(w.to_header())


def synthetic_image(size: int = 64, seed: int = 1) -> np.ndarray:
    """A small synthetic stack image (background + Gaussian star peaks)."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:size, 0:size]
    data = np.full((size, size), 10.0, dtype=np.float64)
    for cy, cx, peak in ((30, 40, 8000.0), (16, 16, 5000.0), (50, 12, 3000.0)):
        data = data + peak * np.exp(-(((yy - cy) ** 2 + (xx - cx) ** 2) / 8.0))
    data = data + rng.normal(0.0, 1.0, size=(size, size))
    return np.ascontiguousarray(data, dtype=np.float32)


def fits_bytes(data: np.ndarray, *, date_obs: str = DATE1, obj: str = "M 42") -> bytes:
    hdu = pyfits.PrimaryHDU(data=data)
    hdu.header["DATE-OBS"] = date_obs
    hdu.header["EXPTIME"] = 10.0
    hdu.header["GAIN"] = 50
    hdu.header["OBJECT"] = obj
    for key, value in wcs_header().items():
        hdu.header[key] = value
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
def project(tmp_path):
    """A tmp project with one session and one stored WCS stack."""
    p = create_project("M 42 night", root=tmp_path / "projects")
    session = start_session(p, clock=frozen(T0))
    store_frame(
        p,
        session,
        KIND_STACK,
        fits_bytes(synthetic_image(64), date_obs=DATE1, obj="M 42"),
        meta={"target": "M 42"},
    )
    yield p
    p.close()


@pytest.fixture()
def viewer_controller(project):
    vc = ViewerController(project)
    vc.refresh_stacks()
    return vc


@pytest.fixture()
def picker_controller(viewer_controller):
    return PickController(viewer_controller=viewer_controller)


@pytest.fixture()
def picker(gtk, picker_controller, viewer_controller):
    from seestar_controller.photometry.star_picker import StarPickerView

    return StarPickerView(
        picker_controller=picker_controller, viewer_controller=viewer_controller
    )


def pump_main(timeout: float = 5.0) -> None:
    """Drain the default GLib main context (runs queued idle callbacks)."""
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if context.pending():
            context.iteration(False)
        else:
            break


def test_build_view_photometry_wires_controllers(gtk):
    from seestar_controller.ui.views import PhotometryView, build_view

    vc = ViewerController()
    pc = PickController(viewer_controller=vc)
    view = build_view("photometry", picker_controller=pc, viewer_controller=vc)
    assert isinstance(view, PhotometryView)
    assert view.picker_controller is pc
    assert view.viewer_controller is vc
    assert view.picker_widget.controller is pc
    assert view.picker_widget.viewer_controller is vc
    assert view.save_button is view.picker_widget.save_button
    # standalone: no controllers passed -> the view creates its own
    standalone = build_view("photometry")
    assert standalone.picker_controller is not pc
    assert standalone.picker_controller.viewer_controller is standalone.viewer_controller


def test_app_owns_shared_picker_controller_and_window_passes_it_down(gtk, tmp_path):
    from seestar_controller.ui.app import SeestarApplication, SeestarWindow

    app = SeestarApplication(settings_path=str(tmp_path / "settings.json"))
    assert isinstance(app.picker_controller, PickController)
    assert app.picker_controller.viewer_controller is app.viewer_controller
    window = SeestarWindow(app, app.state)
    assert window.picker_controller is app.picker_controller
    photometry_page = window.view_stack.get_child_by_name("photometry")
    assert photometry_page.picker_controller is app.picker_controller
    assert photometry_page.viewer_controller is app.viewer_controller


def test_picker_lists_stacks_and_open_stack_shows_preview(picker, viewer_controller):
    assert picker.list_box.get_row_at_index(0) is not None
    assert picker.picture.get_paintable() is None  # nothing open yet
    image = viewer_controller.open_stack(viewer_controller.stacks[0].frame_id)
    pump_main()
    assert picker.controller.image is image  # followed the viewer
    assert picker.picture.get_paintable() is not None
    assert "64x64" in picker._info_label.get_label()


def test_click_picks_star_with_dropdown_role(picker, viewer_controller):
    viewer_controller.open_stack(viewer_controller.stacks[0].frame_id)
    pump_main()
    # role selector: 0 = Target, 1 = Comp
    picker.role_dropdown.set_selected(0)
    wx, wy = picker.image_to_widget(40, 30)  # the bright star at (x=40, y=30)
    picker._on_pressed(None, 1, wx, wy)
    assert len(picker.controller.picks) == 1
    target = picker.controller.target
    assert target is not None and target.label == "Target"
    assert target.x == pytest.approx(40.0, abs=1.0)  # snapped onto the star
    assert target.coord is not None
    # a comp click with the dropdown on "Comp"
    picker.role_dropdown.set_selected(1)
    wx, wy = picker.image_to_widget(16, 16)
    picker._on_pressed(None, 1, wx, wy)
    assert [p.label for p in picker.controller.comps] == ["Comp A"]
    # widget->image mapping round-trips through the CONTAIN transform
    x, y = picker.widget_to_image(*picker.image_to_widget(50.0, 12.0))
    assert (x, y) == pytest.approx((50.0, 12.0), abs=1e-6)


def test_aperture_marks_one_per_pick(picker, viewer_controller):
    viewer_controller.open_stack(viewer_controller.stacks[0].frame_id)
    pump_main()
    assert picker.pick_marks() == ()
    for x, y, role in ((40, 30, "target"), (16, 16, "comp"), (50, 12, "comp")):
        picker.controller.pick_at(x, y, role)
    marks = picker.pick_marks()
    assert len(marks) == 3
    roles = [m[5] for m in marks]
    assert roles == ["target", "comp", "comp"]
    # radii follow the session aperture (defaults 4 / 8 / 12 px, scaled to the
    # preview size — the 64px image is shown at <= 1024px, so scale >= 1)
    for _wx, _wy, radius, sky_in, sky_out, _role, _label in marks:
        assert radius > 0 and sky_in > radius and sky_out > sky_in
    # changing the radius spin updates the controller + the marks
    picker.radius_spin.set_value(10.0)
    pump_main()
    assert picker.controller.aperture.radius_px == 10.0
    assert picker.pick_marks()[0][2] > marks[0][2]


def test_save_button_persists_picks_into_project(gtk, picker, viewer_controller, project):
    viewer_controller.open_stack(viewer_controller.stacks[0].frame_id)
    pump_main()
    picker.controller.set_project(project)
    picker.controller.pick_at(40, 30, "target", label="Target")
    picker.controller.pick_at(16, 16, "comp", label="Comp A")
    picker.controller.pick_at(50, 12, "comp", label="Comp B")
    assert load_star_picks(project) == []
    picker.save_button.clicked()
    pump_main()
    stored = load_star_picks(project)
    assert [p.role for p in stored] == ["target", "comp", "comp"]
    assert [p.label for p in stored] == ["Target", "Comp A", "Comp B"]
    assert stored[0].ra_deg is not None
    assert "saved 3" in picker._status_label.get_label()
