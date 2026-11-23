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

Ticket 02 (catalog assist for comps): the wiring — ``build_view`` /
:class:`~seestar_controller.ui.app.SeestarApplication` /
:class:`~seestar_controller.ui.app.SeestarWindow` share one
:class:`~seestar_controller.photometry.SuggestionController` bound to the
shared pick controller and configured with the comp-search radius from the
settings (a settings change updates the radius live) — and the
:class:`~seestar_controller.photometry.suggestions_panel.SuggestionsPanel`
behavior with a MOCKED catalog lookup (no network): the panel lists the
suggestions after a search, an Add button adds the star as a comp pick at
its WCS pixel, the Search button runs the lookup on a worker thread, the
sort dropdown (issue 07) re-sorts the rows nearest-first / brightest-first /
closest-in-magnitude-to-the-target client-side (no new lookup), and a
catalog failure (``CatalogUnavailableError``) shows as a clean error status.

Ticket 04 (run + results view): the wiring — ``build_view`` /
:class:`~seestar_controller.ui.app.SeestarApplication` /
:class:`~seestar_controller.ui.app.SeestarWindow` share one
:class:`~seestar_controller.photometry.RunController` bound to the shared pick
controller, and the window passes it down to the Photometry page's
:class:`~seestar_controller.photometry.results_view.ResultsPanel` — and the
panel behavior: after a run the per-frame table lists one row per measurement
(jd/utc, mag, mag_err, excluded checkbox) and the scatter plot carries one
point per lightcurve point, the Run button runs the session on a worker thread
(store calls marshalled through ``GLib.idle_add``, never blocking the main
loop), a failed run shows a clean error status, and clicking a table row
toggles the outlier exclusion (``set_excluded``) refreshing both the table
and the plot.

Check-star-plot ticket 02 (check-star series in the results view): the role
selector gains a **Check** entry — clicking a star with it designates the
check star (amber overlay mark, ``"Check"`` label, replace semantics), and
Save picks persists it via store.stars (load order target -> check -> comps);
and the results panel plots the check star's differential curve as a second
series next to the target's (from the last run report's ``frame_results``)
plus a **target − check** toggle for the target-minus-check curve, with the
excluded flag carried over to the check series.

Issue 06 (view too crowded): the Photometry view is an ``Adw.ViewStack`` +
``Adw.ViewSwitcher`` with one workflow tab at a time ("Pick stars" / "Run
photometry" / "Variables" / "Movers") — asserted via ``view.view_stack`` /
``view.view_switcher`` / ``view.select_tab``; and the picker's **go to RA/Dec
or name** entry — ``picker.go_to("5h32m00s, +22d00m00s")`` places the target
pick via ``PickController.add_at_sky`` on the solved frame, a name is resolved
through the injectable ``name_resolver`` (mocked here — no network), failures
(outside the frame, unresolvable name) show as a clean error status, and the
hover readout (``picker.hover_text`` / ``picker.sky_at_widget`` /
``picker.hover_label``) shows the RA/Dec of the hovered pixel.
"""

import io
import threading
import time
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

from seestar_controller.catalog.coords import J2000Coord
from seestar_controller.catalog.facade import CatalogUnavailableError
from seestar_controller.catalog.standards import StandardStar
from seestar_controller.enhance import ViewerController
from seestar_controller.photometry import (
    PickController,
    PickingError,
    RunController,
    SuggestionController,
    sky_to_pixel,
)
from seestar_controller.store import (
    KIND_STACK,
    KIND_SUB,
    StarPick,
    create_project,
    lightcurve,
    list_frames,
    list_sessions,
    load_star_picks,
    save_frame_quality,
    save_star_picks,
    start_session,
    store_frame,
)

T0 = datetime(2026, 11, 20, 22, 0, 0, tzinfo=UTC)
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


def synthetic_image(
    size: int = 64, seed: int = 1, *, extra_stars: tuple[tuple[int, int, float], ...] = ()
) -> np.ndarray:
    """A small synthetic stack image (background + Gaussian star peaks).
    ``extra_stars`` appends more ``(cy, cx, peak)`` stars (used for the check
    star, check-star-plot ticket 02)."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:size, 0:size]
    data = np.full((size, size), 10.0, dtype=np.float64)
    stars = ((30, 40, 8000.0), (16, 16, 5000.0), (50, 12, 3000.0)) + tuple(extra_stars)
    for cy, cx, peak in stars:
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
    except Exception as exc:  # pragma: no cover - backend errors  # noqa: BLE001
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


def _mock_lookup(stars):
    """A mocked catalog callable (no network in widget tests)."""

    def lookup(coord, radius_deg):
        return list(stars)

    return lookup


#: On-frame standard stars near the widget-test target pixel (40, 30) -> sky
#: (82.992, 21.998) on the widget wcs_header (crpix 33,33): A 0.012 deg away,
#: B ~0.0046 deg away (both inside the 64x64 frame).
WIDGET_STAR_A = StandardStar(
    name="APASS A", coord=J2000Coord(ra_deg=82.992, dec_deg=22.01), magnitude=11.2, band="V", distance_deg=0.0
)
WIDGET_STAR_B = StandardStar(
    name="APASS B", coord=J2000Coord(ra_deg=82.997, dec_deg=21.998), magnitude=13.4, band="R", distance_deg=0.0
)


@pytest.fixture()
def suggest_controller(picker_controller):
    """A catalog-assist controller with a MOCKED (empty) lookup — the default
    would hit the real catalog; tests override ``_lookup`` as needed."""
    return SuggestionController(picker_controller, lookup=_mock_lookup([]))


@pytest.fixture()
def picker(gtk, picker_controller, viewer_controller, suggest_controller):
    from seestar_controller.photometry.star_picker import StarPickerView

    return StarPickerView(
        picker_controller=picker_controller,
        viewer_controller=viewer_controller,
        suggest_controller=suggest_controller,
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


@pytest.fixture()
def mixed_project(tmp_path):
    """Issue 05 fixture: a project with two subs AND one stack (jd order:
    sub, stack, sub) — an unfiltered list would bury the stack among subs."""
    p = create_project("M 42 mixed", root=tmp_path / "projects")
    session = start_session(p, clock=frozen(T0))
    sub1 = store_frame(
        p, session, KIND_SUB,
        fits_bytes(synthetic_image(64, seed=3), date_obs=DATE1),
        meta={"target": "M 42"},
    )
    stack = store_frame(
        p, session, KIND_STACK,
        fits_bytes(synthetic_image(64, seed=4), date_obs="2026-11-20T22:00:10.000"),
        meta={"target": "M 42"},
    )
    sub2 = store_frame(
        p, session, KIND_SUB,
        fits_bytes(synthetic_image(64, seed=5), date_obs="2026-11-20T22:00:20.000"),
        meta={"target": "M 42"},
    )
    yield p, [sub1, stack, sub2]
    p.close()


def test_picker_lists_stacks_only_by_default_show_all_lists_subs(gtk, mixed_project):
    """Issue 05: the star picker shares the ViewerController, so its frame
    list DEFAULTS TO THE STACKS ONLY (picking is done on stacks), newest
    first; the "Show all frames" toggle also lists the subs, and a sub row
    can still be opened with the toggle on."""
    from seestar_controller.photometry.star_picker import StarPickerView

    _p, (sub1, stack, sub2) = mixed_project
    vc = ViewerController(_p)
    vc.refresh_stacks()
    pc = PickController(viewer_controller=vc)
    sc = SuggestionController(pc, lookup=_mock_lookup([]))
    picker = StarPickerView(
        picker_controller=pc, viewer_controller=vc, suggest_controller=sc
    )
    pump_main()
    # default: only the stack row, and the toggle is off
    assert not vc.show_all
    assert not picker.show_all_button.get_active()
    assert set(picker._rows) == {stack.id}
    assert vc.stacks[0].frame_id == stack.id
    # flip the toggle next to Refresh: subs appear, newest first
    picker.show_all_button.set_active(True)
    pump_main()
    assert vc.show_all
    assert set(picker._rows) == {sub1.id, stack.id, sub2.id}
    assert [s.frame_id for s in vc.stacks] == [sub2.id, stack.id, sub1.id]
    # selecting the stack row opens it for picking (as before)
    picker.list_box.select_row(picker._rows[stack.id])
    pump_main()
    assert vc.current_stack is not None and vc.current_stack.frame_id == stack.id
    assert vc.current_stack.kind == KIND_STACK
    assert picker.controller.image is vc.current_image
    # a sub row is openable with show-all on
    picker.list_box.select_row(picker._rows[sub2.id])
    pump_main()
    assert vc.current_stack is not None and vc.current_stack.frame_id == sub2.id
    assert vc.current_stack.kind == KIND_SUB


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
    picker.save_button.emit("clicked")
    pump_main()
    stored = load_star_picks(project)
    assert [p.role for p in stored] == ["target", "comp", "comp"]
    assert [p.label for p in stored] == ["Target", "Comp A", "Comp B"]
    assert stored[0].ra_deg is not None
    assert "saved 3" in picker._status_label.get_label()


# ---------------------------------------------------------------------------
# Ticket 02: catalog assist for comps (shared SuggestionController wiring +
# the SuggestionsPanel widget, with a MOCKED catalog lookup — no network)
# ---------------------------------------------------------------------------


def _join_search(panel) -> None:
    """Wait for the panel's worker-thread search (if any) to finish."""
    thread = panel._search_thread
    if thread is not None:
        thread.join(timeout=10.0)


def test_build_view_photometry_wires_suggest_controller(gtk):
    from seestar_controller.ui.views import PhotometryView, build_view

    vc = ViewerController()
    pc = PickController(viewer_controller=vc)
    sc = SuggestionController(pc, lookup=_mock_lookup([]))
    view = build_view(
        "photometry", picker_controller=pc, viewer_controller=vc, suggest_controller=sc
    )
    assert isinstance(view, PhotometryView)
    assert view.suggest_controller is sc
    assert view.picker_widget.suggest_controller is sc
    assert view.picker_widget.suggestions_panel.controller is sc
    # standalone: no controllers passed -> the view creates its own, bound to
    # its own pick controller
    standalone = build_view("photometry")
    assert standalone.suggest_controller is not sc
    assert standalone.suggest_controller.picker_controller is standalone.picker_controller


def test_app_owns_shared_suggest_controller_and_window_passes_it_down(gtk, tmp_path):
    from seestar_controller.ui.app import SeestarApplication, SeestarWindow

    app = SeestarApplication(settings_path=str(tmp_path / "settings.json"))
    assert isinstance(app.suggest_controller, SuggestionController)
    assert app.suggest_controller.picker_controller is app.picker_controller
    assert app.suggest_controller.radius_deg == pytest.approx(0.5)  # settings default (issue 08: ~S50 FOV)
    window = SeestarWindow(app, app.state)
    assert window.suggest_controller is app.suggest_controller
    photometry_page = window.view_stack.get_child_by_name("photometry")
    assert photometry_page.suggest_controller is app.suggest_controller
    assert photometry_page.picker_widget.suggestions_panel.controller is app.suggest_controller
    # a settings change updates the shared controller's radius live (listener);
    # point project_root at an existing dir first (validation requires it)
    app.settings_controller.update(project_root=str(tmp_path))
    app.settings_controller.update(photometry_comp_search_radius_deg="0.25")
    assert app.suggest_controller.radius_deg == pytest.approx(0.25)


def test_suggestions_panel_lists_suggestions_and_add_button_adds_comp(
    picker, viewer_controller, suggest_controller
):
    panel = picker.suggestions_panel
    assert panel.controller is suggest_controller
    # the panel registered its worker-thread search as the controller's runner,
    # so automatic searches never block the GLib main loop
    assert suggest_controller.search_runner == panel.search  # bound methods: == not is
    viewer_controller.open_stack(viewer_controller.stacks[0].frame_id)
    pump_main()
    # mock the catalog BEFORE picking the target, so the automatic search
    # (target changed -> worker thread) already uses it
    suggest_controller._lookup = _mock_lookup([WIDGET_STAR_A, WIDGET_STAR_B])
    picker.controller.pick_at(40, 30, "target", label="Target", snap=False)
    panel.run_search()  # the synchronous search the worker thread runs
    _join_search(panel)
    pump_main()
    # nearest-first: B (0.0043 deg from the target) before A (0.013 deg)
    assert [s.name for s in suggest_controller.suggestions] == ["APASS B", "APASS A"]
    rows = panel.suggestion_rows()
    assert len(rows) == 2
    assert rows[0].suggestion_index == 0 and rows[1].suggestion_index == 1
    assert "APASS B" in rows[0].get_child().get_first_child().get_label()
    assert "2 standard(s)" in panel.status_label.get_label()
    # clicking Add converts the star's sky position to a pixel via the frame
    # WCS and adds it as a comp pick (comps accumulate, one target)
    rows[0].add_button.emit("clicked")
    pump_main()
    comps = picker.controller.comps
    assert len(comps) == 1
    comp = comps[0]
    assert comp.role == "comp"
    assert comp.label == "APASS B"
    assert comp.coord == WIDGET_STAR_B.coord
    expected_x, expected_y = sky_to_pixel(picker.controller.image.header, WIDGET_STAR_B.coord)
    assert (comp.x, comp.y) == pytest.approx((expected_x, expected_y), abs=1e-6)
    assert picker.controller.target is not None and picker.controller.target.label == "Target"
    assert "added comp" in panel.status_label.get_label()


def test_suggestions_panel_shows_clean_error_on_catalog_failure(
    picker, viewer_controller, suggest_controller
):
    def failing_lookup(coord, radius_deg):
        raise CatalogUnavailableError("offline and no cache")

    panel = picker.suggestions_panel
    viewer_controller.open_stack(viewer_controller.stacks[0].frame_id)
    pump_main()
    suggest_controller._lookup = failing_lookup
    picker.controller.pick_at(40, 30, "target", label="Target", snap=False)
    snapshot = panel.run_search()  # must NOT raise — the failure is a state
    _join_search(panel)
    pump_main()
    assert snapshot.error is not None and "offline" in snapshot.error
    assert snapshot.suggestions == ()
    assert panel.suggestion_rows() == []
    status = panel.status_label
    assert "offline" in status.get_label()


def test_suggestions_panel_greys_out_and_disables_out_of_frame_rows(
    picker, viewer_controller, suggest_controller
):
    """Issue 08: suggestions whose sky position falls outside the open frame's
    footprint are listed greyed with the Add button disabled (never offered as
    addable comps); in-frame rows stay normal and addable; the status line
    reports the out-of-frame count and the radius label the frame FOV."""
    # ~2 deg from the target (82.992, 21.998) — far outside the 64x64 frame
    far = StandardStar(
        name="APASS FAR",
        coord=J2000Coord(ra_deg=82.992, dec_deg=24.0),
        magnitude=10.0,
        band="V",
        distance_deg=0.0,
    )
    panel = picker.suggestions_panel
    viewer_controller.open_stack(viewer_controller.stacks[0].frame_id)
    pump_main()
    suggest_controller._lookup = _mock_lookup([WIDGET_STAR_A, WIDGET_STAR_B, far])
    suggest_controller.set_radius(2.5)  # wide enough to list the far star
    picker.controller.pick_at(40, 30, "target", label="Target", snap=False)
    snapshot = panel.run_search()
    _join_search(panel)
    pump_main()
    # nearest-first: B, A (on-frame), then FAR (off-frame)
    assert [s.name for s in snapshot.suggestions] == ["APASS B", "APASS A", "APASS FAR"]
    assert [s.in_frame for s in snapshot.suggestions] == [True, True, False]
    assert snapshot.out_of_frame_count == 1
    assert snapshot.frame_fov_deg is not None and snapshot.frame_fov_deg < 0.1
    rows = panel.suggestion_rows()
    assert len(rows) == 3
    assert [r.in_frame for r in rows] == [True, True, False]
    # the out-of-frame row is greyed and its Add button disabled
    far_row = rows[2]
    assert far_row.add_button.get_sensitive() is False
    assert "outside the frame" in far_row.get_child().get_first_child().get_label()
    label = far_row.get_child().get_first_child()
    assert label.has_css_class("dim-label")
    assert "APASS FAR" in label.get_label()
    # the in-frame rows keep an enabled Add button
    assert rows[0].add_button.get_sensitive() is True
    assert rows[1].add_button.get_sensitive() is True
    # the status line reports the out-of-frame count; the radius label the FOV
    assert "(1 outside the frame)" in panel.status_label.get_label()
    assert "frame FOV" in panel.radius_label.get_label()
    # clicking the disabled row's Add does nothing; the pure layer refuses too
    from seestar_controller.photometry import SuggestionError

    with pytest.raises(SuggestionError, match="outside the open frame"):
        suggest_controller.add_suggestion(2)
    assert picker.controller.comps == ()
    # an in-frame row still adds its comp
    rows[0].add_button.emit("clicked")
    pump_main()
    assert [p.label for p in picker.controller.comps] == ["APASS B"]


def test_suggestions_panel_sort_dropdown_resorts_the_rows(
    picker, viewer_controller, suggest_controller
):
    """Issue 07: the sort dropdown next to Search re-sorts the comp list —
    nearest-first (default), brightest-first, closest mag to the target pick —
    client-side (no new catalog lookup)."""
    from seestar_controller.photometry import (
        SORT_CLOSEST_MAG,
        SORT_DISTANCE,
        SORT_MAGNITUDE,
        SORT_MODES,
    )
    from seestar_controller.photometry.suggestions_panel import SORT_LABELS

    panel = picker.suggestions_panel
    # the dropdown offers the three sort modes and mirrors the controller's
    # default (nearest-first)
    model = panel.sort_dropdown.get_model()
    assert [model.get_item(i).get_string() for i in range(model.get_n_items())] == list(SORT_LABELS)
    assert len(SORT_LABELS) == len(SORT_MODES) == 3
    assert SORT_MODES[panel.sort_dropdown.get_selected()] == suggest_controller.sort_mode == SORT_DISTANCE

    viewer_controller.open_stack(viewer_controller.stacks[0].frame_id)
    pump_main()
    calls: list = []

    def counting_lookup(coord, radius_deg):
        calls.append((coord, radius_deg))
        return [WIDGET_STAR_A, WIDGET_STAR_B]

    suggest_controller._lookup = counting_lookup
    picker.controller.pick_at(40, 30, "target", label="Target", snap=False)
    panel.run_search()  # the synchronous search the worker thread runs
    _join_search(panel)
    pump_main()

    def row_names():
        return [
            rows.get_child().get_first_child().get_label().split(" — ")[0]
            for rows in panel.suggestion_rows()
        ]

    # default: nearest-first — B (~0.0046 deg) before A (~0.013 deg)
    assert row_names() == ["APASS B", "APASS A"]
    n_calls = len(calls)

    # brightest first: A (V 11.2) before B (R 13.4) — a client-side re-sort,
    # the catalog is NOT queried again
    panel.sort_dropdown.set_selected(SORT_MODES.index(SORT_MAGNITUDE))
    pump_main()
    assert suggest_controller.sort_mode == SORT_MAGNITUDE
    assert row_names() == ["APASS A", "APASS B"]
    assert len(calls) == n_calls

    # closest in magnitude to the target pick (target mag 11.0): A |0.2| first
    suggest_controller.target_magnitude = 11.0
    panel.sort_dropdown.set_selected(SORT_MODES.index(SORT_CLOSEST_MAG))
    pump_main()
    assert suggest_controller.sort_mode == SORT_CLOSEST_MAG
    assert row_names() == ["APASS A", "APASS B"]
    assert len(calls) == n_calls

    # and back to nearest-first
    panel.sort_dropdown.set_selected(SORT_MODES.index(SORT_DISTANCE))
    pump_main()
    assert suggest_controller.sort_mode == SORT_DISTANCE
    assert row_names() == ["APASS B", "APASS A"]
    assert len(calls) == n_calls
    # the rows still carry their (re-sorted) indices + Add buttons
    rows = panel.suggestion_rows()
    assert [r.suggestion_index for r in rows] == [0, 1]
    assert all(hasattr(r, "add_button") for r in rows)


# --- ticket 04: run + results view ---------------------------------------------------------------


def _sub_fits(i: int) -> bytes:
    """A synthetic sub-frame FITS (constant stars) with DATE-OBS T0 + i*10s."""
    seconds = i * 10
    date_obs = f"2026-11-20T22:{seconds // 60:02d}:{seconds % 60:02d}.000"
    return fits_bytes(synthetic_image(64, seed=10 + i), date_obs=date_obs)


def _store_subs(project, n: int = 4) -> str:
    """Store ``n`` synthetic sub-frames in the project's (single) session and
    return the session id."""
    session = list_sessions(project)[0].id
    for i in range(n):
        store_frame(project, session, KIND_SUB, _sub_fits(i))
    return session


def _save_widget_picks(project) -> None:
    """Target at (40, 30) + comps at (16, 16) and (50, 12) — the synthetic
    image's star positions (x=column, y=row)."""
    save_star_picks(
        project,
        [
            StarPick(role="target", label="Target", x=40.0, y=30.0, ra_deg=None,
                     dec_deg=None, aperture_radius=4.0, sky_annulus_inner=8.0,
                     sky_annulus_outer=12.0),
            StarPick(role="comp", label="Comp A", x=16.0, y=16.0, ra_deg=None,
                     dec_deg=None, aperture_radius=4.0, sky_annulus_inner=8.0,
                     sky_annulus_outer=12.0),
            StarPick(role="comp", label="Comp B", x=12.0, y=50.0, ra_deg=None,
                     dec_deg=None, aperture_radius=4.0, sky_annulus_inner=8.0,
                     sky_annulus_outer=12.0),
        ],
    )


def _drain_run(thread, timeout: float = 30.0) -> None:
    """Pump the GLib main loop while the run worker thread is alive (the loop
    runs the marshalled store calls + renders), then join + final drain."""
    deadline = time.monotonic() + timeout
    while thread.is_alive() and time.monotonic() < deadline:
        pump_main(0.1)
        time.sleep(0.005)
    thread.join(timeout=10.0)
    assert not thread.is_alive()
    pump_main()


def test_build_view_photometry_wires_run_controller(gtk):
    from seestar_controller.ui.views import PhotometryView, build_view

    vc = ViewerController()
    pc = PickController(viewer_controller=vc)
    rc = RunController(picker_controller=pc)
    view = build_view(
        "photometry", picker_controller=pc, viewer_controller=vc, run_controller=rc
    )
    assert isinstance(view, PhotometryView)
    assert view.run_controller is rc
    assert view.results_panel.controller is rc
    # standalone: no run controller passed -> the view creates its own, bound
    # to its own pick controller
    standalone = build_view("photometry")
    assert standalone.run_controller is not rc
    assert standalone.run_controller.picker_controller is standalone.picker_controller
    assert standalone.results_panel.controller is standalone.run_controller


def test_app_owns_shared_run_controller_and_window_passes_it_down(gtk, tmp_path):
    from seestar_controller.ui.app import SeestarApplication, SeestarWindow

    app = SeestarApplication(settings_path=str(tmp_path / "settings.json"))
    assert isinstance(app.run_controller, RunController)
    assert app.run_controller.picker_controller is app.picker_controller
    window = SeestarWindow(app, app.state)
    assert window.run_controller is app.run_controller
    photometry_page = window.view_stack.get_child_by_name("photometry")
    assert photometry_page.run_controller is app.run_controller
    assert photometry_page.results_panel.controller is app.run_controller


def test_results_panel_shows_table_and_plot_after_a_run(gtk, project):
    from seestar_controller.photometry.results_view import ResultsPanel

    session = _store_subs(project, n=4)
    _save_widget_picks(project)
    rc = RunController(project=project)
    panel = ResultsPanel(run_controller=rc)
    assert panel.controller is rc
    assert panel.selected_session_id() == session  # the only session, preselected
    assert panel.measurement_rows() == []
    assert panel.plot_points() == ()

    report = rc.run_session(session)  # main thread: rendered synchronously
    assert len(report.measurements) == 4 and report.skipped == ()
    rows = panel.measurement_rows()
    assert len(rows) == 4
    assert [r.measurement_id for r in rows] == [m.id for m in report.measurements]
    assert all(r.excluded is False for r in rows)
    assert all(r.excluded_check.get_active() is False for r in rows)
    points = panel.plot_points()
    assert len(points) == 4
    assert [p[0] for p in points] == [m.jd for m in report.measurements]  # jd
    assert [p[1] for p in points] == [m.mag for m in report.measurements]  # mag
    assert all(p[2] is not None for p in points)  # mag_err
    assert all(p[3] is False for p in points)  # excluded flag
    assert panel.progress_bar.get_fraction() == 1.0
    assert "4 measured, 0 skipped" in panel.progress_label.get_label()
    assert "4 measured" in panel.status_label.get_label()
    assert panel.run_button.get_sensitive() is True
    assert len(lightcurve(project)) == 4


def test_results_panel_shows_exclusion_summary_for_flagged_frames(gtk, project):
    # frame-quality ticket 03: a flagged sub-frame is excluded from the run by
    # default and the panel surfaces the count + reasons
    from seestar_controller.photometry.results_view import ResultsPanel

    session = _store_subs(project, n=4)
    _save_widget_picks(project)
    flagged = list_frames(project, session_id=session, kind=KIND_SUB)[2]
    save_frame_quality(
        project,
        flagged.id,
        fwhm=9.4,
        bg_median=100.0,
        bg_std=5.0,
        star_count=8,
        flagged=True,
        flag_reasons=("fwhm",),
    )
    rc = RunController(project=project)
    panel = ResultsPanel(run_controller=rc)
    report = rc.run_session(session)  # main thread: rendered synchronously
    assert [e.frame_id for e in report.excluded] == [flagged.id]
    assert report.excluded[0].reasons == ("fwhm",)
    assert len(report.measurements) == 3
    # the exclusion label carries the count + reasons, progress + status the count
    assert panel.exclusion_label.get_label() == "1 frame excluded (fwhm×1)"
    assert "3 measured, 0 skipped, 1 excluded" in panel.progress_label.get_label()
    assert "1 excluded" in panel.status_label.get_label()
    assert len(panel.measurement_rows()) == 3
    assert len(panel.plot_points()) == 3
    assert len(lightcurve(project)) == 3


def test_results_panel_row_click_toggles_outlier_exclusion(gtk, project):
    from seestar_controller.photometry.results_view import ResultsPanel

    session = _store_subs(project, n=3)
    _save_widget_picks(project)
    rc = RunController(project=project)
    panel = ResultsPanel(run_controller=rc)
    report = rc.run_session(session)
    victim = report.measurements[0]
    row = panel.measurement_rows()[0]
    assert row.measurement_id == victim.id and row.excluded is False

    panel._on_row_activated(panel.list_box, row)  # click -> flag as outlier
    assert victim.id not in [m.id for m in lightcurve(project)]
    assert victim.id in [m.id for m in lightcurve(project, include_excluded=True)]
    rows = panel.measurement_rows()  # the table was rebuilt
    assert rows[0].excluded is True
    assert rows[0].excluded_check.get_active() is True
    assert panel.plot_points()[0][3] is True  # the plot point is flagged too
    assert "outlier" in panel.status_label.get_label()

    panel._on_row_activated(panel.list_box, panel.measurement_rows()[0])  # click again
    assert victim.id in [m.id for m in lightcurve(project)]
    assert panel.measurement_rows()[0].excluded is False
    assert panel.measurement_rows()[0].excluded_check.get_active() is False
    assert panel.plot_points()[0][3] is False

    # the checkbox toggles the same flag
    other = report.measurements[1]
    panel.measurement_rows()[1].excluded_check.set_active(True)
    assert other.id not in [m.id for m in lightcurve(project)]
    assert panel.plot_points()[1][3] is True


def test_results_panel_run_button_runs_on_a_worker_thread(gtk, project):
    from seestar_controller.photometry.results_view import ResultsPanel

    session = _store_subs(project, n=4)
    _save_widget_picks(project)
    # like the app: marshal the pure-layer callbacks onto the GLib main loop
    rc = RunController(project=project, invoke_on_main=lambda cb: GLib.idle_add(cb))
    panel = ResultsPanel(run_controller=rc)
    assert panel.selected_session_id() == session

    thread = panel.run()  # same as clicking the Run button
    assert thread is not None and thread.name == "photometry-run"
    assert not panel.run_button.get_sensitive()  # disabled while running
    _drain_run(thread)  # pumps the main loop — never blocks it

    assert len(panel.measurement_rows()) == 4
    assert len(panel.plot_points()) == 4
    assert len(lightcurve(project)) == 4
    assert "4 measured, 0 skipped" in panel.progress_label.get_label()
    assert panel.progress_bar.get_fraction() == 1.0
    assert panel.run_button.get_sensitive() is True


def test_results_panel_shows_clean_error_when_run_fails(gtk, project):
    from seestar_controller.photometry.results_view import ResultsPanel

    _store_subs(project, n=2)
    # NO picks stored -> the run fails with a clean status, never a crash
    rc = RunController(project=project, invoke_on_main=lambda cb: GLib.idle_add(cb))
    panel = ResultsPanel(run_controller=rc)
    thread = panel.run()
    _drain_run(thread)
    assert "no target pick" in panel.status_label.get_label()
    assert panel.run_button.get_sensitive() is True
    assert panel.measurement_rows() == []
    assert panel.plot_points() == ()
    assert lightcurve(project) == []
    assert panel.status_label.has_css_class("error")


def test_search_button_runs_lookup_on_worker_thread(picker, viewer_controller, suggest_controller):
    panel = picker.suggestions_panel
    viewer_controller.open_stack(viewer_controller.stacks[0].frame_id)
    pump_main()
    picker.controller.pick_at(40, 30, "target", label="Target", snap=False)
    _join_search(panel)  # the automatic search from the target pick (empty mock)
    pump_main()
    assert panel.suggestion_rows() == []
    suggest_controller._lookup = _mock_lookup([WIDGET_STAR_A, WIDGET_STAR_B])
    panel.search_button.emit("clicked")
    thread = panel._search_thread
    assert thread is not None and thread is not threading.current_thread()
    thread.join(timeout=10.0)
    pump_main()  # the worker's listener notification was marshalled via idle_add
    assert len(panel.suggestion_rows()) == 2
    assert "2 standard(s)" in panel.status_label.get_label()


# ---------------------------------------------------------------------------
# Check-star-plot ticket 02: check-star role in the picker + check series in
# the results view
# ---------------------------------------------------------------------------

#: The check star's position in the synthetic frames (cy, cx) = (48, 48) —
#: well clear of every other star's aperture + sky annulus (nearest star is
#: the target at (30, 40), ~19.7 px away; annulus outer is 12 px).
CHECK_CY, CHECK_CX = 48, 48


def test_role_dropdown_has_check_and_check_pick_persists(gtk, picker, viewer_controller, project):
    viewer_controller.open_stack(viewer_controller.stacks[0].frame_id)
    pump_main()
    # the role selector offers Target / Comp / Check
    model = picker.role_dropdown.get_model()
    assert [model.get_item(i).get_string() for i in range(model.get_n_items())] == [
        "Target",
        "Comp",
        "Check",
    ]
    # set the project FIRST (set_project loads the stored picks, replacing
    # the session's), then pick target + comp + check via dropdown + clicks
    picker.controller.set_project(project)
    for index, (x, y) in ((0, (40, 30)), (1, (16, 16)), (2, (50, 12))):
        picker.role_dropdown.set_selected(index)
        wx, wy = picker.image_to_widget(x, y)
        picker._on_pressed(None, 1, wx, wy)
    picks = picker.controller.picks
    assert [p.role for p in picks] == ["target", "comp", "check"]
    check = picker.controller.check
    assert check is not None and check.label == "Check"
    assert check.x == pytest.approx(50.0, abs=1.0)  # snapped onto the star
    # the overlay carries one mark per pick, the check one included
    assert [m[5] for m in picker.pick_marks()] == ["target", "comp", "check"]
    assert "check set" in picker._info_label.get_label()
    # Save picks persists the check star via store.stars (load order:
    # target -> check -> comps)
    picker.save_button.emit("clicked")
    pump_main()
    stored = load_star_picks(project)
    assert [p.role for p in stored] == ["target", "check", "comp"]
    assert [p.label for p in stored] == ["Target", "Check", "Comp A"]
    assert "saved 3" in picker._status_label.get_label()
    # the project.json mirror carries the check pick too
    assert project.config["star_picks"]["check"]["role"] == "check"
    assert project.config["star_picks"]["check"]["label"] == "Check"


def _sub_fits_with_check(i: int) -> bytes:
    """A synthetic sub-frame FITS with a 4th (check) star at (48, 48)."""
    seconds = i * 10
    date_obs = f"2026-11-20T22:{seconds // 60:02d}:{seconds % 60:02d}.000"
    return fits_bytes(
        synthetic_image(64, seed=10 + i, extra_stars=((CHECK_CY, CHECK_CX, 3000.0),)),
        date_obs=date_obs,
    )


def _store_subs_with_check(project, n: int = 4) -> str:
    """Store ``n`` synthetic sub-frames (with the check star) in the project's
    session and return the session id."""
    session = list_sessions(project)[0].id
    for i in range(n):
        store_frame(project, session, KIND_SUB, _sub_fits_with_check(i))
    return session


def _save_widget_picks_with_check(project) -> None:
    """Target + 2 comps + the check star at (48, 48) — the synthetic frames'
    star positions (x=column, y=row)."""
    save_star_picks(
        project,
        [
            StarPick(role="target", label="Target", x=40.0, y=30.0, ra_deg=None,
                     dec_deg=None, aperture_radius=4.0, sky_annulus_inner=8.0,
                     sky_annulus_outer=12.0),
            StarPick(role="comp", label="Comp A", x=16.0, y=16.0, ra_deg=None,
                     dec_deg=None, aperture_radius=4.0, sky_annulus_inner=8.0,
                     sky_annulus_outer=12.0),
            StarPick(role="comp", label="Comp B", x=12.0, y=50.0, ra_deg=None,
                     dec_deg=None, aperture_radius=4.0, sky_annulus_inner=8.0,
                     sky_annulus_outer=12.0),
            StarPick(role="check", label="Check", x=float(CHECK_CX), y=float(CHECK_CY),
                     ra_deg=None, dec_deg=None, aperture_radius=4.0,
                     sky_annulus_inner=8.0, sky_annulus_outer=12.0),
        ],
    )


def test_results_panel_shows_check_series_and_tmc_toggle(gtk, project):
    from seestar_controller.photometry.results_view import ResultsPanel

    session = _store_subs_with_check(project, n=4)
    _save_widget_picks_with_check(project)
    rc = RunController(project=project)
    panel = ResultsPanel(run_controller=rc)
    report = rc.run_session(session)  # main thread: rendered synchronously
    assert len(report.measurements) == 4 and report.skipped == ()
    assert len(report.check_points) == 4  # the run had a check star

    # the target series is unchanged; the check series sits next to it
    assert len(panel.plot_points()) == 4
    check_points = panel.check_plot_points()
    assert len(check_points) == 4
    assert [p[0] for p in check_points] == [p[0] for p in report.check_points]  # jd
    assert [p[1] for p in check_points] == [p[1] for p in report.check_points]  # check_mag
    assert all(p[2] is not None and p[2] > 0.0 for p in check_points)  # check_mag_err
    assert all(p[3] is False for p in check_points)  # excluded flag
    # constant stars: the check differential mag is CONSTANT across frames
    # (it is a differential mag — check.mag - ensemble — so its offset is not
    # zero, but its frame-to-frame spread stays well within the errors)
    check_mags = [p[1] for p in check_points]
    assert max(check_mags) - min(check_mags) < 5.0 * (check_points[0][2] or 1.0)
    # the target-minus-check series is available, hidden until the toggle
    tmc_points = panel.tmc_plot_points()
    assert len(tmc_points) == 4
    assert [p[0] for p in tmc_points] == [p[0] for p in report.target_minus_check_points]
    assert panel.show_target_minus_check() is False
    assert panel.tmc_toggle.get_active() is False
    panel.set_show_target_minus_check(True)  # same as clicking the toggle
    assert panel.show_target_minus_check() is True
    assert panel.tmc_toggle.get_active() is True
    panel.tmc_toggle.set_active(False)
    assert panel.show_target_minus_check() is False
    # flagging a row as outlier flags the check/tmc points of that frame too
    panel._on_row_activated(panel.list_box, panel.measurement_rows()[0])
    assert panel.plot_points()[0][3] is True
    assert panel.check_plot_points()[0][3] is True
    assert panel.tmc_plot_points()[0][3] is True


def test_results_panel_without_check_star_has_empty_check_series(gtk, project):
    from seestar_controller.photometry.results_view import ResultsPanel

    session = _store_subs(project, n=3)  # no check star in the frames/picks
    _save_widget_picks(project)
    rc = RunController(project=project)
    panel = ResultsPanel(run_controller=rc)
    report = rc.run_session(session)
    assert report.check_points == ()  # no check star picked
    assert len(panel.plot_points()) == 3
    assert panel.check_plot_points() == ()
    assert panel.tmc_plot_points() == ()
    # the toggle is harmless without a check star (nothing extra is drawn)
    panel.set_show_target_minus_check(True)
    assert panel.show_target_minus_check() is True
    assert panel.tmc_plot_points() == ()


# ---------------------------------------------------------------------------
# Issue 06: the Photometry view's workflow tabs + the picker's go-to entry +
# hover sky-coord readout
# ---------------------------------------------------------------------------


def test_photometry_view_has_four_workflow_tabs(gtk):
    from seestar_controller.ui.views import (
        PHOTOMETRY_TABS,
        PhotometryView,
        build_view,
    )

    vc = ViewerController()
    pc = PickController(viewer_controller=vc)
    view = build_view("photometry", picker_controller=pc, viewer_controller=vc)
    assert isinstance(view, PhotometryView)
    assert view.tabs == PHOTOMETRY_TABS == (
        ("pick", "Pick stars"),
        ("run", "Run photometry"),
        ("variables", "Variables"),
        ("movers", "Movers"),
    )
    stack = view.view_stack
    assert view.view_switcher.get_stack() is stack
    pages = stack.get_pages()
    names = [pages.get_item(i).get_name() for i in range(pages.get_n_items())]
    titles = [pages.get_item(i).get_title() for i in range(pages.get_n_items())]
    assert names == ["pick", "run", "variables", "movers"]
    assert titles == ["Pick stars", "Run photometry", "Variables", "Movers"]
    # each tab carries its workflow widget; the old properties still work
    assert stack.get_child_by_name("pick") is view.picker_widget
    assert stack.get_child_by_name("run") is view.results_panel
    assert stack.get_child_by_name("variables") is view.variable_scan_view
    assert stack.get_child_by_name("movers") is view.mover_overlay
    assert view.tab_child("pick") is view.picker_widget
    assert view.picker_widget.controller is pc  # the shared controllers still wired
    # switching tabs switches the visible page
    assert view.visible_tab == "pick"
    view.select_tab("run")
    assert view.visible_tab == "run"
    assert stack.get_visible_child() is view.results_panel
    view.select_tab("movers")
    assert view.visible_tab == "movers"
    assert stack.get_visible_child() is view.mover_overlay
    # standalone construction (no controllers passed) has the same tabs
    standalone = build_view("photometry")
    assert [t for t, _title in standalone.tabs] == names
    assert standalone.tab_child("pick") is standalone.picker_widget


def test_app_window_photometry_page_has_the_tabs(gtk, tmp_path):
    from seestar_controller.ui.app import SeestarApplication, SeestarWindow

    app = SeestarApplication(settings_path=str(tmp_path / "settings.json"))
    window = SeestarWindow(app, app.state)
    page = window.view_stack.get_child_by_name("photometry")
    assert [t for t, _title in page.tabs] == ["pick", "run", "variables", "movers"]
    assert page.tab_child("pick") is page.picker_widget
    assert page.picker_widget.controller is app.picker_controller
    assert page.tab_child("run") is page.results_panel
    assert page.results_panel.controller is app.run_controller
    page.select_tab("variables")
    assert page.visible_tab == "variables"


def test_goto_entry_places_target_pick_at_sky_coords(picker, viewer_controller):
    viewer_controller.open_stack(viewer_controller.stacks[0].frame_id)
    pump_main()
    picker.role_dropdown.set_selected(0)  # Target
    # wcs_header: crval (83, 22), crpix (33, 33) 1-based, cdelt (-0.001, 0.001)
    # -> sky (83, 22) is 0-based pixel (32, 32), inside the 64x64 frame;
    # 5h32m00s == 83.0 deg RA
    spec = picker.go_to("5h32m00s, +22d00m00s")
    assert spec.role == "target"
    assert spec.coord == J2000Coord(ra_deg=83.0, dec_deg=22.0)
    assert (spec.x, spec.y) == pytest.approx((32.0, 32.0), abs=1e-6)
    assert spec.label == "Target"
    assert picker.controller.target is spec
    assert [m[5] for m in picker.pick_marks()] == ["target"]  # overlay re-rendered
    # the same flow through the entry + Go button
    picker.goto_entry.set_text("5.5333333 +22.0")
    picker.goto_button.emit("clicked")
    pump_main()
    target = picker.controller.target
    assert target is not None and target.coord is not None
    assert target.coord.ra_deg == pytest.approx(83.0, abs=1e-4)
    assert "went to" in picker._status_label.get_label()


def test_goto_name_resolves_via_name_resolver_and_places_target(picker, viewer_controller):
    viewer_controller.open_stack(viewer_controller.stacks[0].frame_id)
    pump_main()
    calls = []

    def resolver(name):
        calls.append(name)
        return J2000Coord(ra_deg=83.0, dec_deg=22.0)

    picker._name_resolver = resolver  # mocked — no network in widget tests
    picker.role_dropdown.set_selected(0)  # Target
    spec = picker.go_to("M 42")  # the synchronous API resolves through the resolver
    assert calls == ["M 42"]
    assert spec.role == "target" and spec.label == "M 42"
    assert spec.coord == J2000Coord(ra_deg=83.0, dec_deg=22.0)
    # through the entry + Go button the resolve runs on a worker thread and is
    # marshalled back to the main loop
    picker.goto_entry.set_text("M 42")
    picker.goto_button.emit("clicked")
    thread = picker._goto_thread
    assert thread is not None and thread is not threading.current_thread()
    thread.join(timeout=10.0)
    pump_main()  # the idle_add'ed result places the pick
    target = picker.controller.target
    assert target is not None and target.label == "M 42"
    assert calls == ["M 42", "M 42"]


def test_goto_failures_show_clean_error_status(picker, viewer_controller):
    viewer_controller.open_stack(viewer_controller.stacks[0].frame_id)
    pump_main()
    # a sky position mapping outside the 64x64 frame
    with pytest.raises(PickingError, match="outside"):
        picker.go_to("0h00m00s, +89d00m00s")
    assert picker.controller.picks == ()
    picker.goto_entry.set_text("0h00m00s, +89d00m00s")
    picker.goto_button.emit("clicked")
    pump_main()
    assert picker.controller.picks == ()
    assert "outside" in picker._status_label.get_label()
    assert picker._status_label.has_css_class("error")
    # an unresolvable name
    def failing_resolver(name):
        raise RuntimeError("offline and no cache")

    picker._name_resolver = failing_resolver
    with pytest.raises(PickingError, match="cannot resolve"):
        picker.go_to("Foo Bar")
    picker.goto_entry.set_text("Foo Bar")
    picker.goto_button.emit("clicked")
    thread = picker._goto_thread
    assert thread is not None
    thread.join(timeout=10.0)
    pump_main()
    assert picker.controller.picks == ()
    assert "cannot resolve" in picker._status_label.get_label()
    assert picker._status_label.has_css_class("error")
    # a malformed entry
    picker.goto_entry.set_text("83.0, abc")
    picker.goto_button.emit("clicked")
    pump_main()
    assert "cannot parse" in picker._status_label.get_label()


def test_hover_readout_shows_sky_coords_of_hovered_pixel(picker, viewer_controller):
    from seestar_controller.photometry import try_pixel_to_sky

    viewer_controller.open_stack(viewer_controller.stacks[0].frame_id)
    pump_main()
    wx, wy = picker.image_to_widget(40, 30)
    coord = picker.sky_at_widget(wx, wy)
    expected = try_pixel_to_sky(picker.controller.image.header, 40, 30)
    assert coord is not None and expected is not None
    assert coord == expected
    text = picker.hover_text(wx, wy)
    assert "RA" in text and "Dec" in text and "px 40.0, 30.0" in text
    # the motion controller drives the hover label
    picker._on_motion(None, wx, wy)
    assert picker.hover_label.get_label() == text
    picker._on_motion_leave()
    assert picker.hover_label.get_label() == ""
    # no image open -> empty readout
    picker.controller.set_image(None)
    pump_main()
    assert picker.hover_text(wx, wy) == ""
    assert picker.sky_at_widget(wx, wy) is None
