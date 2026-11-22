"""Sub-frames issue 02: WIDGET tests — the analysis chain's widgets accept
SOLVED SUBS: the image viewer's Solve panel is sensitive with a sub open and
solving it persists the WCS for the sub frame id; the photometry star picker
lists the sub and a row click opens it in the viewer; the H-R diagram view's
frame dropdown lists the cross-matched sub and renders the diagram from it.

These SKIP in the headless sandbox (no ``gi`` module, no GTK system libs, no
display; see docs/wiki/assumptions.md) and run on the desktop (or under
``xvfb-run -a uv run pytest``). Guards, in order: ``pytest.importorskip("gi")``,
``gi.require_version`` for Gtk 4.0 / Adw 1, and a ``Gtk.init_check()`` fixture.
Buttons are clicked with ``.emit("clicked")`` (NOT ``.clicked()``) and widgets
are never ``destroy()``-ed (GTK4). No network: the solver boundary is
monkeypatched and the Gaia TAP fetch is blocked.

The pure behaviour (solve sub -> WCS round-trip, crossmatch on a sub, H-R
from a sub crossmatch, picking on a solved sub, the no-gi subprocess
boundary) is covered by tests/test_sub_frames_analysis.py.
"""

from __future__ import annotations

import io
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
from test_gaia_detect import make_stack, wcs_header
from test_hr_diagram import cluster_table

from seestar_controller.astrometry import SolveController, SolveResult
from seestar_controller.astrometry import session as astrometry_session
from seestar_controller.enhance import ViewerController
from seestar_controller.hr_diagram import HrDiagramController
from seestar_controller.hr_diagram.view import CMD, HR, HrDiagramView
from seestar_controller.photometry import PickController, SuggestionController
from seestar_controller.photometry.star_picker import StarPickerView
from seestar_controller.store import (
    KIND_SUB,
    create_project,
    load_frame_wcs,
    save_crossmatch,
    start_session,
    store_frame,
)

T0 = datetime(2026, 11, 20, 22, 0, 0, tzinfo=UTC)
DATE1 = "2026-11-20T22:00:00.000"
DATE2 = "2026-11-20T22:00:10.000"

#: A solved-WCS card set (TAN around the synthetic field center).
CARDS = {
    "CTYPE1": "RA---TAN",
    "CTYPE2": "DEC--TAN",
    "CRVAL1": 83.0,
    "CRVAL2": 22.0,
    "CRPIX1": 128.5,
    "CRPIX2": 128.5,
    "CDELT1": -0.001,
    "CDELT2": 0.001,
}
REQ_RA_DEG = 83.0
REQ_DEC_DEG = 22.0


def frozen(dt: datetime):
    return lambda: dt


def sub_fits_bytes(data: np.ndarray, header: dict | None, date_obs: str) -> bytes:
    hdu = pyfits.PrimaryHDU(data=np.asarray(data, dtype=np.float32))
    for key, value in (header or {}).items():
        hdu.header[key] = value
    hdu.header["DATE-OBS"] = date_obs
    hdu.header["OBJECT"] = "NGC7789"
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
    """A tmp project (NGC7789) with one session, one raw sub and one
    solved+cross-matched sub."""
    p = create_project("NGC7789", root=tmp_path / "projects")
    session = start_session(p, clock=frozen(T0))
    raw = store_frame(
        p, session, KIND_SUB, sub_fits_bytes(make_stack(), {}, DATE1)
    )
    header = wcs_header()
    solved = store_frame(
        p, session, KIND_SUB, sub_fits_bytes(make_stack(), dict(header), DATE2)
    )
    save_crossmatch(p, solved.id, cluster_table())
    yield p, raw, solved
    p.close()


@pytest.fixture()
def viewer(project):
    vc = ViewerController(project[0])
    vc.refresh_stacks()
    return vc


def pump_main(timeout: float = 5.0) -> None:
    """Drain the default GLib main context (runs queued idle callbacks)."""
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        time.sleep(0.01)


def wait_until(predicate, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pump_main(0.05)
        if predicate():
            return True
    return predicate()


def _fake_result() -> SolveResult:
    return SolveResult(
        wcs=None,
        cards=dict(CARDS),
        solver="gaia",
        solved_at="2026-11-20T22:05:00",
        center_ra_deg=REQ_RA_DEG,
        center_dec_deg=REQ_DEC_DEG,
        n_stars=5,
        rms_arcsec=0.4,
        matches=(),
        rows=(),
    )


def _raising_fetcher(**_kwargs):
    raise RuntimeError("network access blocked in test")


# --- the viewer's Solve panel on an open sub ---------------------------------


def test_solve_panel_on_open_sub_solves_and_persists(gtk, project, viewer, monkeypatch):
    """Open a raw SUB in the image viewer: the Solve panel is sensitive, and
    clicking Solve plate-solves the sub and persists the WCS via store.wcs
    for the sub frame id (solver boundary monkeypatched — no network)."""
    from seestar_controller.astrometry.view import SolvePanel

    p, raw, _solved = project
    viewer.open_stack(raw.id)
    pump_main()
    sc = SolveController(
        project=p,
        viewer_controller=viewer,
        invoke_on_main=lambda cb: GLib.idle_add(cb),
    )
    toasts: list[str] = []
    view = SolvePanel(solve_controller=sc, show_toast=toasts.append)
    pump_main()
    assert sc.snapshot.stack_frame_id == raw.id  # the open SUB is the target
    assert view.solve_button.get_sensitive()

    monkeypatch.setattr(astrometry_session, "solve_frame", lambda **kwargs: _fake_result())
    view.solve_button.emit("clicked")  # Gtk.Button has no .clicked() on GTK4
    assert wait_until(lambda: sc.snapshot.solved is not None)
    pump_main()
    snap = sc.snapshot
    assert snap.error is None and snap.last_solver == "gaia"
    assert "solved" in view.status_label.get_label()
    assert any(t.startswith("Solved:") for t in toasts)
    # persisted for the SUB frame id
    record = load_frame_wcs(p, raw.id)
    assert record is not None and record.frame_id == raw.id
    view._on_destroy()


# --- the photometry star picker lists and opens a sub -------------------------


def test_star_picker_lists_sub_and_row_click_opens_it(gtk, project, viewer):
    """The star picker's frame list shows the subs (with the kind in the
    row subtitle); selecting the sub row opens it in the shared viewer."""
    _p, raw, solved = project
    # issue 05: the picker list defaults to stacks only — flip the show-all
    # toggle so the subs are listed
    viewer.set_show_all(True)
    pc = PickController(viewer_controller=viewer)
    suggest = SuggestionController(pc, lookup=lambda coord, radius_deg: [])
    picker = StarPickerView(
        picker_controller=pc, viewer_controller=viewer, suggest_controller=suggest
    )
    pump_main()
    rows = [picker.list_box.get_row_at_index(i) for i in range(2)]
    assert all(r is not None for r in rows)
    assert set(picker._rows) == {raw.id, solved.id}
    # the listed frames are the subs, newest first, with the kind in the subtitle
    assert [s.kind for s in viewer.stacks] == [KIND_SUB, KIND_SUB]
    assert [s.frame_id for s in viewer.stacks] == [solved.id, raw.id]  # newest first
    assert all(s.subtitle().startswith("sub") for s in viewer.stacks)

    # selecting the raw sub row opens it in the viewer
    picker.list_box.select_row(picker._rows[raw.id])
    pump_main()
    assert viewer.current_stack is not None
    assert viewer.current_stack.frame_id == raw.id
    assert pc.image is viewer.current_image  # the pick controller followed


# --- the H-R view's frame dropdown includes the cross-matched sub -------------


def test_hr_view_dropdown_lists_crossmatched_sub(gtk, project, tmp_path):
    """The H-R view's frame selector lists the cross-matched SUB (any kind)
    and selecting it renders the CMD/H-R pages from the sub's cross-match."""
    p, _raw, solved = project
    ctl = HrDiagramController(
        p,
        invoke_on_main=lambda cb: GLib.idle_add(cb),
        isochrone_fetcher=_raising_fetcher,
        cache_dir=tmp_path / "iso-cache",
    )
    view = HrDiagramView(ctl)
    pump_main()
    stacks = ctl.refresh_stacks()
    assert [(s.frame_id, s.kind) for s in stacks] == [(solved.id, KIND_SUB)]
    ctl.select_stack(solved.id)
    pump_main()
    assert view.stack_dropdown.get_model().get_n_items() == 1
    assert view.stack_dropdown.get_selected() == 0
    assert view.stack.get_visible_child_name() == "plot"
    assert view.png_bytes(CMD)[:8] == b"\x89PNG\r\n\x1a\n"
    assert view.png_bytes(HR)[:8] == b"\x89PNG\r\n\x1a\n"
    assert "matched" in view.status_label.get_label()
