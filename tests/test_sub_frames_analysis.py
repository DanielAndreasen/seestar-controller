"""Sub-frames issue 02: PURE tests — the analysis chain works on SOLVED SUBS:
the viewer's Solve action accepts any open frame (a sub included) and
persists the WCS via ``store.wcs`` for sub frame ids (no schema bump — the
``frame_wcs`` table references ``frames(id)`` of any kind), the Gaia
cross-match overlay runs on a solved sub, the H-R diagram view's frame
selector includes solved + cross-matched subs and builds the CMD/H-R from
their cross-match, and the photometry picker accepts a solved sub.

No gi (a subprocess boundary test enforces it), no network: the plate
solver's Gaia reference query is INJECTED (``gaia_query=``) and the single
TAP HTTP boundary ``gaia.query.fetch_gaia_tap_json`` is monkeypatched.

Covers (``.scratch/sub-frames/issues/02-solve-crossmatch-subs.md``):

- :class:`~seestar_controller.astrometry.SolveController` following the
  shared :class:`~seestar_controller.enhance.ViewerController` with a SUB
  open: ``solve_open_stack`` plate-solves the sub (Gaia solver, injected
  query), the WCS record round-trips through ``store.wcs`` for the sub id,
  the solved WCS is written into the stored FITS header, the cross-match
  rows are persisted, and ``list_solved_frames`` sees the sub; a failed
  solve on a sub records the error and writes nothing;
- :class:`~seestar_controller.gaia.overlay.CrossmatchController` following
  the viewer with a solved sub open: ``run_crossmatch`` detects, matches
  (TAP monkeypatched), persists the table for the sub id, and renders;
  ``refresh_stacks`` lists ALL frames (subs included) and ``select_stack``
  accepts a sub frame id; ``list_crossmatched_stacks`` reports the sub with
  ``kind="sub"``; an unsolved sub yields a clear recorded error, never a
  raise; Gaia-unavailable-without-cache is a recorded error too;
- :class:`~seestar_controller.hr_diagram.HrDiagramController`:
  ``refresh_stacks`` lists the cross-matched sub (any kind) and
  ``select_stack(sub_id)`` builds the CMD/H-R points + table from the sub's
  cross-match; unknown frame ids raise a clear :class:`HrError`;
- :class:`~seestar_controller.photometry.picking.PickController` following
  the viewer with a solved sub open: ``pick_at`` on a star returns a sky
  coord (the sub's WCS); an unsolved sub is pixel-only (``coord=None``);
- the package boundary: a subprocess test asserting the touched pure
  modules import no ``gi`` / ``link`` / ``ui``.

The widget tests (Solve button sensitive on an open sub, the star-picker
list shows the sub, the H-R dropdown lists the cross-matched sub) live in
tests/test_sub_frames_analysis_widgets.py and skip without ``gi``.
"""

from __future__ import annotations

import io
import re
import subprocess
import sys
import urllib.error
from datetime import UTC, datetime

import numpy as np
import pytest
from astropy.io import fits as pyfits
from test_astrometry_solve import (
    CRVAL,
    RESIDUAL_TOL_ARCSEC,
    SCALE_ARCSEC,
    blank_image,
    blind_header,
    fake_query,
    gaia_stars_at_stars,
    make_image,
    residual_arcsec,
)
from test_astrometry_solve import (
    STARS as SOLVE_STARS,
)
from test_gaia_detect import STARS as DETECT_STARS
from test_gaia_detect import make_stack, star_sky, wcs_header
from test_gaia_pipeline import gaia_rows_for_stars
from test_gaia_query import gaia_payload
from test_hr_diagram import cluster_table

from seestar_controller.astrometry import (
    SOLVER_GAIA,
    SolveController,
    has_celestial_wcs,
)
from seestar_controller.catalog.cache import QueryCache
from seestar_controller.enhance import ViewerController
from seestar_controller.gaia import query as gq
from seestar_controller.gaia.errors import GaiaError
from seestar_controller.gaia.overlay import CrossmatchController
from seestar_controller.hr_diagram import HrDiagramController, HrError
from seestar_controller.photometry.picking import PickController
from seestar_controller.store import (
    KIND_STACK,
    KIND_SUB,
    create_project,
    get_frame_path,
    list_crossmatched_stacks,
    list_solved_frames,
    load_crossmatch,
    load_frame_wcs,
    save_crossmatch,
    start_session,
    store_frame,
)
from seestar_controller.store.schema import SCHEMA_VERSION

T0 = datetime(2026, 11, 20, 22, 0, 0, tzinfo=UTC)
DATE1 = "2026-11-20T22:00:00.000"
DATE2 = "2026-11-20T22:00:10.000"


# --- fixtures ---------------------------------------------------------------


@pytest.fixture()
def project(tmp_path):
    """One created project (NGC7789 — the demoable target), closed after."""
    p = create_project("NGC7789", root=tmp_path / "projects")
    yield p
    p.close()


@pytest.fixture()
def session(project):
    return start_session(project, started_at=T0)


def sub_fits_bytes(data: np.ndarray, header: dict | None, date_obs: str) -> bytes:
    """Serialize a sub as FITS bytes with DATE-OBS (drives jd/utc)."""
    hdu = pyfits.PrimaryHDU(data=np.asarray(data, dtype=np.float32))
    for key, value in (header or {}).items():
        hdu.header[key] = value
    hdu.header["DATE-OBS"] = date_obs
    hdu.header["EXPTIME"] = 10.0
    hdu.header["OBJECT"] = "NGC7789"
    buf = io.BytesIO()
    pyfits.HDUList([hdu]).writeto(buf)
    return buf.getvalue()


@pytest.fixture()
def raw_sub(project, session):
    """One RAW sub (no WCS — the solve target) stored in the project."""
    return store_frame(
        project, session, KIND_SUB, sub_fits_bytes(make_image(), blind_header(), DATE1)
    )


@pytest.fixture()
def solved_sub(project, session):
    """One SOLVED sub: the synthetic star field WITH a celestial WCS in the
    header (exactly what the solve writes back), as (frame, header)."""
    header = wcs_header()
    frame = store_frame(
        project, session, KIND_SUB, sub_fits_bytes(make_stack(), dict(header), DATE2)
    )
    return frame, header


@pytest.fixture()
def cache(tmp_path):
    return QueryCache(cache_dir=tmp_path / "gaia-cache", ttl=3600.0)


@pytest.fixture()
def tap(monkeypatch):
    """The mocked Gaia TAP boundary: ``arm(header)`` installs Gaia rows at
    the injected stars' sky positions (server-side Gmag filter included)."""
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


def _no_network(monkeypatch):
    def boom(query, **kwargs):
        raise urllib.error.URLError("no network")

    monkeypatch.setattr(gq, "fetch_gaia_tap_json", boom)


def _raising_fetcher(**_kwargs):
    raise RuntimeError("network access blocked in test")


# --- Solve: the viewer's Solve action accepts an open sub --------------------


def test_solve_controller_solves_an_open_sub_and_persists_wcs(project, raw_sub):
    """The full demoable chain, pure layer: open the sub in the shared
    viewer, Solve -> the WCS is persisted via store.wcs for the SUB frame
    id (no schema bump: frame_wcs references frames(id) of any kind), the
    stored FITS header carries the solved WCS, and the Gaia solver's
    cross-match rows are persisted too."""
    viewer = ViewerController(project=project)
    viewer.refresh_stacks()
    # issue 05: the default list is stacks only (empty here — the project
    # has just the sub); the sub is in the full listing and behind show-all
    assert not viewer.stacks
    assert [s.frame_id for s in viewer.all_stacks] == [raw_sub.id]
    viewer.set_show_all(True)
    assert [s.frame_id for s in viewer.stacks] == [raw_sub.id]
    viewer.open_stack(raw_sub.id)

    controller = SolveController(project=project, viewer_controller=viewer)
    snap = controller.snapshot
    assert snap.stack_frame_id == raw_sub.id  # the open SUB is the solve target
    assert snap.solved is None

    snap = controller.solve_open_stack(
        solver=SOLVER_GAIA,
        gaia_query=fake_query(gaia_stars_at_stars()),
        pixel_scale_hint=SCALE_ARCSEC,
    )
    assert snap.error is None
    assert snap.solved is not None
    assert snap.solved.solver == SOLVER_GAIA
    assert snap.solved.n_stars == len(SOLVE_STARS)

    # 1. the WCS record round-trips through store.wcs for the sub id
    record = load_frame_wcs(project, raw_sub.id)
    assert record is not None
    assert record.frame_id == raw_sub.id
    assert record.session_id == raw_sub.session_id
    assert record.solver == SOLVER_GAIA
    assert record.n_stars == len(SOLVE_STARS)
    assert record.center_ra_deg == pytest.approx(CRVAL[0], abs=0.01)
    assert record.center_dec_deg == pytest.approx(CRVAL[1], abs=0.01)
    wcs = record.to_wcs()
    for cy, cx, _peak in SOLVE_STARS:
        assert residual_arcsec(wcs, cx, cy) < RESIDUAL_TOL_ARCSEC

    # 2. the stored FITS file on disk now carries the solved WCS (round-trip)
    header = pyfits.getheader(str(get_frame_path(project, raw_sub)))
    assert has_celestial_wcs(header)
    from seestar_controller.astrometry import cards_to_wcs

    for cy, cx, _peak in SOLVE_STARS:
        assert residual_arcsec(cards_to_wcs(header), cx, cy) < RESIDUAL_TOL_ARCSEC

    # 3. the Gaia solver's cross-match rows were persisted for the sub id
    rows = load_crossmatch(project, raw_sub.id)
    assert len(rows) >= len(SOLVE_STARS)
    assert sum(1 for r in rows if r.matched) == len(SOLVE_STARS)

    # 4. list_solved_frames sees the sub; the schema needed NO bump
    assert [w.frame_id for w in list_solved_frames(project)] == [raw_sub.id]
    assert project.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION

    # 5. a fresh controller over the same project sees the persisted WCS
    controller2 = SolveController(project=project, viewer_controller=viewer)
    assert controller2.snapshot.solved is not None
    assert controller2.snapshot.solved.frame_id == raw_sub.id


def test_solve_controller_failed_solve_on_sub_writes_nothing(project, session):
    """An unsolvable sub (starless): the error is recorded in the snapshot,
    and neither the db nor the FITS header is touched."""
    frame = store_frame(
        project, session, KIND_SUB, sub_fits_bytes(blank_image(), blind_header(), DATE1)
    )
    viewer = ViewerController(project=project)
    viewer.refresh_stacks()
    viewer.open_stack(frame.id)
    controller = SolveController(project=project, viewer_controller=viewer)
    snap = controller.solve_open_stack(
        solver=SOLVER_GAIA,
        gaia_query=fake_query(gaia_stars_at_stars()),
        pixel_scale_hint=SCALE_ARCSEC,
    )
    assert snap.error is not None
    assert snap.solved is None
    assert load_frame_wcs(project, frame.id) is None
    assert load_crossmatch(project, frame.id) == ()
    header = pyfits.getheader(str(get_frame_path(project, frame)))
    assert not has_celestial_wcs(header)


# --- Gaia cross-match overlay on a solved sub --------------------------------


def test_crossmatch_overlay_runs_on_a_solved_sub(project, solved_sub, tap, cache):
    """Open the solved sub in the viewer: the overlay follows it, runs the
    cross-match (TAP monkeypatched), persists the table for the SUB id, and
    renders; the standalone list/selector accept the sub too."""
    frame, header = solved_sub
    calls, arm = tap
    arm(header)

    viewer = ViewerController(project=project)
    viewer.refresh_stacks()
    viewer.open_stack(frame.id)

    cc = CrossmatchController(viewer_controller=viewer, cache=cache)
    assert cc.stack_frame_id == frame.id  # the open SUB is the overlay target

    snap = cc.run_crossmatch()
    assert snap.error is None
    assert snap.stack_frame_id == frame.id
    assert len(snap.rows) >= len(DETECT_STARS)
    assert sum(1 for r in snap.rows if r.matched) == len(DETECT_STARS)
    assert snap.image is not None
    assert len(calls) == 1  # exactly one Gaia cone query (no network beyond the mock)

    # persisted for the sub id, and listed as a cross-matched SUB
    rows = load_crossmatch(project, frame.id)
    assert len(rows) == len(snap.rows)
    listed = list_crossmatched_stacks(project)
    assert [(s.frame_id, s.kind) for s in listed] == [(frame.id, KIND_SUB)]

    # the standalone frame list includes ALL kinds (subs, stacks, enhanced)
    stack = store_frame(
        project, frame.session_id, KIND_STACK,
        sub_fits_bytes(make_stack(), dict(header), "2026-11-20T22:01:00.000"),
    )
    frames = cc.refresh_stacks()
    assert {f.id for f in frames} == {frame.id, stack.id}
    assert {f.kind for f in frames} == {KIND_SUB, KIND_STACK}

    # the standalone selector accepts the sub frame id
    snap2 = cc.select_stack(frame.id)
    assert snap2.error is None
    assert snap2.stack_frame_id == frame.id
    assert len(snap2.rows) == len(rows)
    assert snap2.image is not None
    with pytest.raises(GaiaError, match="no frame with frame id"):
        cc.select_stack(frame.id + 1000)


def test_crossmatch_on_unsolved_sub_is_a_clear_recorded_error(project, session, tap, cache, monkeypatch):
    """A raw sub (no WCS) opened in the viewer: run_crossmatch records the
    clear plate-solve error in the snapshot — never raises, no network."""
    _no_network(monkeypatch)
    frame = store_frame(
        project, session, KIND_SUB, sub_fits_bytes(make_stack(), {}, DATE1)
    )
    viewer = ViewerController(project=project)
    viewer.refresh_stacks()
    viewer.open_stack(frame.id)
    cc = CrossmatchController(viewer_controller=viewer, cache=cache)
    snap = cc.run_crossmatch()
    assert snap.error is not None
    assert "WcsMissingError" in snap.error and "plate-solve" in snap.error
    assert load_crossmatch(project, frame.id) == ()


def test_crossmatch_gaia_unavailable_without_cache_on_sub_is_recorded(project, solved_sub, cache, monkeypatch):
    """Solved sub + Gaia down + empty cache: a clear recorded error, not a
    raise (and the tests never touch the network)."""
    _no_network(monkeypatch)
    frame, _header = solved_sub
    cc = CrossmatchController(project, cache=cache)
    cc.select_stack(frame.id)
    snap = cc.run_crossmatch()
    assert snap.error is not None
    assert "URLError" in snap.error or "Gaia" in snap.error


# --- H-R diagram from a solved + cross-matched sub ---------------------------


def test_hr_diagram_selector_includes_and_builds_from_a_sub(project, solved_sub, tmp_path):
    """A cross-matched sub appears in the H-R frame selector (any kind) and
    select_stack builds the CMD/H-R points from the sub's cross-match."""
    frame, _header = solved_sub
    save_crossmatch(project, frame.id, cluster_table())

    ctl = HrDiagramController(
        project, isochrone_fetcher=_raising_fetcher, cache_dir=tmp_path / "iso-cache"
    )
    stacks = ctl.refresh_stacks()
    assert [(s.frame_id, s.kind) for s in stacks] == [(frame.id, KIND_SUB)]

    snap = ctl.select_stack(frame.id)
    assert snap.error is None
    assert snap.frame_id == frame.id
    assert len(snap.table) == len(cluster_table())
    assert len(snap.cmd) > 0 and len(snap.hr) > 0
    assert snap.image is not None  # the sub image is tone-mapped for the plot
    assert snap.stack_label is not None

    # unknown / bad ids raise a clear HrError
    with pytest.raises(HrError, match="no frame with frame id"):
        ctl.select_stack(frame.id + 1000)
    with pytest.raises(HrError):
        ctl.select_stack("1")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]

    # refresh keeps the sub selected
    snap2 = ctl.refresh()
    assert snap2.frame_id == frame.id


def test_hr_diagram_auto_selects_an_open_sub_with_a_crossmatch(project, solved_sub, tmp_path):
    """With a viewer attached, opening a cross-matched sub auto-selects it
    in the H-R view (the diagram follows the frame open in the image
    viewer)."""
    frame, _header = solved_sub
    save_crossmatch(project, frame.id, cluster_table())

    viewer = ViewerController(project=project)
    viewer.refresh_stacks()
    ctl = HrDiagramController(
        project, viewer_controller=viewer,
        isochrone_fetcher=_raising_fetcher, cache_dir=tmp_path / "iso-cache",
    )
    assert ctl.frame_id is None
    viewer.open_stack(frame.id)  # notifies -> the controller auto-selects
    assert ctl.frame_id == frame.id
    assert len(ctl.snapshot.table) == len(cluster_table())


# --- photometry picker accepts a solved sub ----------------------------------


def test_photometry_picker_on_a_solved_sub_gives_sky_coords(project, solved_sub):
    """The pick controller follows the viewer: with a SOLVED sub open, a
    pick on a star snaps and converts to a J2000 coord via the sub's WCS."""
    frame, header = solved_sub
    viewer = ViewerController(project=project)
    viewer.refresh_stacks()
    viewer.open_stack(frame.id)

    pc = PickController(project, viewer_controller=viewer)
    assert pc.image is not None

    cy, cx, _peak = DETECT_STARS[0]
    spec = pc.pick_at(cx, cy, "target")
    assert spec.role == "target"
    assert spec.coord is not None
    ra, dec = star_sky(header, cx, cy)
    assert spec.coord.ra_deg == pytest.approx(ra, abs=0.01)
    assert spec.coord.dec_deg == pytest.approx(dec, abs=0.01)
    assert pc.target is spec


def test_photometry_picker_on_a_raw_sub_is_pixel_only(project, session):
    """A raw sub (no WCS) still picks in pixel space (ticket 05 semantics),
    with ``coord=None`` — picking always works."""
    frame = store_frame(
        project, session, KIND_SUB, sub_fits_bytes(make_stack(), {}, DATE1)
    )
    viewer = ViewerController(project=project)
    viewer.refresh_stacks()
    viewer.open_stack(frame.id)
    pc = PickController(project, viewer_controller=viewer)
    cy, cx, _peak = DETECT_STARS[0]
    spec = pc.pick_at(cx, cy, "comp")
    assert spec.coord is None
    assert spec.x == pytest.approx(cx, abs=1.0)
    assert spec.y == pytest.approx(cy, abs=1.0)


# --- package boundary ---------------------------------------------------------


def test_touched_pure_modules_import_no_gi_no_link_no_ui():
    """Subprocess boundary: the pure modules this ticket touches (the gaia
    overlay controller, the H-R controller, the astrometry solve controller,
    the photometry pick controller, the enhance viewer controller) import no
    ``gi`` / ``link`` / ``ui`` — the widget layer stays lazy."""
    code = (
        "import sys;"
        "import seestar_controller.gaia.overlay;"
        "import seestar_controller.hr_diagram.controller;"
        "import seestar_controller.astrometry.controller;"
        "import seestar_controller.photometry.picking;"
        "import seestar_controller.enhance.viewer_controller;"
        "assert 'gi' not in sys.modules, 'GTK imported';"
        "assert 'seestar_controller.link' not in sys.modules, 'link imported';"
        "assert 'seestar_controller.ui' not in sys.modules, 'ui imported';"
        "assert 'seestar_controller.gaia.overlay_view' not in sys.modules, 'widget imported';"
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


def test_no_test_touches_the_network(project, solved_sub, cache, monkeypatch):
    """Guard: with the TAP boundary hard-blocked, the whole sub analysis
    chain still runs offline (solver query injected, cross-match served from
    the persisted table / cache)."""
    _no_network(monkeypatch)
    frame, _header = solved_sub
    # solve-side: the injected gaia_query never goes through fetch_gaia_tap_json
    viewer = ViewerController(project=project)
    viewer.refresh_stacks()
    viewer.open_stack(frame.id)
    cc = CrossmatchController(viewer_controller=viewer, cache=cache)
    # no persisted table yet + no network -> recorded error, not a raise
    snap = cc.run_crossmatch()
    assert snap.error is not None
    # once the table is persisted (as the solve does), the overlay renders
    # straight from the store without any Gaia query
    save_crossmatch(project, frame.id, cluster_table())
    snap = cc.select_stack(frame.id)
    assert snap.error is None
    assert len(snap.rows) == len(cluster_table())
