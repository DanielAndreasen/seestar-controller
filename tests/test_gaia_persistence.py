"""Tests for seestar_controller.gaia.persistence — the issue-02 glue that
stores a cross-match table into the project db. tmp_path projects, no
network, no gi."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from seestar_controller import gaia, store
from seestar_controller.gaia import CrossmatchRow
from seestar_controller.gaia.matching import crossmatch
from seestar_controller.gaia.persistence import (
    list_crossmatched_stacks,
    load_crossmatch,
    save_crossmatch,
)
from seestar_controller.gaia.query import GaiaStar
from seestar_controller.store import (
    KIND_STACK,
    CrossmatchError,
    create_project,
    open_project,
    start_session,
    store_frame,
)


@pytest.fixture()
def root(tmp_path):
    return tmp_path / "projects"


@pytest.fixture()
def project(root):
    p = create_project("M 42", root=root)
    yield p
    p.close()


def frozen(dt: datetime):
    return lambda: dt


T0 = datetime(2026, 11, 20, 22, 0, 0, tzinfo=UTC)


def _fits_bytes(date_obs: str) -> bytes:
    import io

    import numpy as np
    from astropy.io import fits as pyfits

    hdu = pyfits.PrimaryHDU(data=np.zeros((8, 8), dtype=np.float32))
    hdu.header["DATE-OBS"] = date_obs
    buf = io.BytesIO()
    pyfits.HDUList([hdu]).writeto(buf)
    return buf.getvalue()


def _add_stack(project, session_id: str):
    instant = T0 + timedelta(seconds=10)
    return store_frame(
        project,
        session_id,
        KIND_STACK,
        _fits_bytes(instant.isoformat(timespec="milliseconds").replace("+00:00", "")),
    )


RA0, DEC0 = 83.633, 22.014
ARCSEC = 1.0 / 3600.0


def _pipeline_rows() -> list[CrossmatchRow]:
    """Run the pure issue-01 pipeline (detection-free: hand-built inputs) and
    return its CrossmatchRow output — the exact type the glue must accept."""
    from seestar_controller.gaia.detect import DetectedSource

    detected = [
        DetectedSource(x=10.0, y=20.0, ra_deg=RA0, dec_deg=DEC0, peak=1000.0, flux=900.0),
        DetectedSource(x=50.0, y=60.0, ra_deg=RA0 + 0.01, dec_deg=DEC0 + 0.01,
                       peak=800.0, flux=700.0),
        DetectedSource(x=99.0, y=99.0, ra_deg=RA0 + 0.5, dec_deg=DEC0 + 0.5,
                       peak=500.0, flux=400.0),
    ]
    gaia_rows = [
        GaiaStar(source_id=111, ra_deg=RA0 + 0.5 * ARCSEC, dec_deg=DEC0, g=14.0,
                 bp=14.5, rp=13.5, parallax=1.25, parallax_error=0.05,
                 pmra=2.5, pmra_error=0.06, pmdec=-3.5, pmdec_error=0.07),
        GaiaStar(source_id=222, ra_deg=RA0 + 0.01 + 0.2 * ARCSEC, dec_deg=DEC0 + 0.01,
                 g=18.0, bp=18.4, rp=17.6, parallax=0.5, parallax_error=0.2,
                 pmra=1.0, pmdec=-1.0),
    ]
    return crossmatch(detected, gaia_rows, radius_arcsec=1.5)


def test_pipeline_output_persists_and_loads_back(project):
    sid = start_session(project, clock=frozen(T0))
    stack = _add_stack(project, sid)
    rows = _pipeline_rows()
    assert [r.matched for r in rows] == [True, True, False]

    saved = save_crossmatch(project, stack.id, rows)
    assert len(saved) == 3
    loaded = load_crossmatch(project, stack.id)
    assert [r.matched for r in loaded] == [True, True, False]
    first = loaded[0]
    assert (first.x, first.y) == (10.0, 20.0)
    assert first.ra_deg == pytest.approx(RA0) and first.dec_deg == pytest.approx(DEC0)
    assert first.source_id == 111
    assert first.g == pytest.approx(14.0)
    assert first.bp == pytest.approx(14.5) and first.rp == pytest.approx(13.5)
    assert first.parallax == pytest.approx(1.25)
    assert first.parallax_error == pytest.approx(0.05)
    assert first.pmra == pytest.approx(2.5)
    assert first.pmra_error == pytest.approx(0.06)
    assert first.pmdec == pytest.approx(-3.5)
    assert first.pmdec_error == pytest.approx(0.07)
    assert first.separation_arcsec == pytest.approx(0.5, abs=0.05)
    # the second matched row carries its (low-SNR) parallax error too
    assert loaded[1].parallax == pytest.approx(0.5)
    assert loaded[1].parallax_error == pytest.approx(0.2)
    assert loaded[1].pmra_error is None and loaded[1].pmdec_error is None
    # an unmatched detection has every Gaia field (value and error) NULL
    unmatched = loaded[2]
    assert unmatched.matched is False and unmatched.source_id is None
    assert unmatched.parallax is None and unmatched.parallax_error is None
    assert unmatched.pmra is None and unmatched.pmra_error is None
    assert unmatched.pmdec is None and unmatched.pmdec_error is None
    assert unmatched.separation_arcsec is None

    # the store-side API sees the same table
    assert store.load_crossmatch(project, stack.id) == tuple(saved)
    summaries = list_crossmatched_stacks(project)
    assert [s.frame_id for s in summaries] == [stack.id]
    assert summaries[0].row_count == 3 and summaries[0].matched_count == 2
    assert summaries[0].unmatched_count == 1


def test_persisted_error_columns_drive_the_hr_points_snr_cut(project):
    """The full chain of ticket hr-ngc7789/02: crossmatch rows carrying the
    Gaia error columns -> persist -> load -> the H-R diagram's parallax-SNR
    cut uses them (row 111 passes, row 222 at SNR 2.5 is dropped)."""
    from seestar_controller.hr_diagram import core as hr

    sid = start_session(project, clock=frozen(T0))
    stack = _add_stack(project, sid)
    save_crossmatch(project, stack.id, _pipeline_rows())
    loaded = load_crossmatch(project, stack.id)
    # the H-R list keeps only the row passing parallax / parallax_error > 5
    points = hr.hr_points(loaded)
    assert [p.source_id for p in points] == [111]
    assert points[0].parallax_error == pytest.approx(0.05)
    assert points[0].abs_g == pytest.approx(hr.absolute_g(14.0, 1.25))
    # the CMD keeps every matched row with complete photometry
    assert [p.source_id for p in hr.cmd_points(loaded)] == [111, 222]
    # the persisted pm errors are readable off the loaded rows (membership input)
    assert loaded[0].pmra_error == pytest.approx(0.06)
    assert loaded[0].pmdec_error == pytest.approx(0.07)


def test_rows_with_error_fields_persist_the_errors(project):
    """Duck-typed rows that DO carry the error fields have them persisted."""

    @dataclass(frozen=True)
    class RichRow:
        x: float
        y: float
        ra_deg: float
        dec_deg: float
        matched: bool
        source_id: int
        g: float
        parallax: float
        parallax_error: float
        pmra: float
        pmra_error: float
        pmdec: float
        pmdec_error: float
        separation_arcsec: float

    sid = start_session(project, clock=frozen(T0))
    stack = _add_stack(project, sid)
    rich = RichRow(
        x=1.0, y=2.0, ra_deg=RA0, dec_deg=DEC0, matched=True, source_id=42,
        g=15.0, parallax=2.5, parallax_error=0.11, pmra=1.0, pmra_error=0.2,
        pmdec=-1.0, pmdec_error=0.3, separation_arcsec=0.3,
    )
    save_crossmatch(project, stack.id, [rich])
    (loaded,) = load_crossmatch(project, stack.id)
    assert loaded.parallax == 2.5 and loaded.parallax_error == 0.11
    assert loaded.pmra == 1.0 and loaded.pmra_error == 0.2
    assert loaded.pmdec == -1.0 and loaded.pmdec_error == 0.3


def test_glue_persists_across_reopen_and_missing_stack_raises(root):
    p = create_project("M 42", root=root)
    sid = start_session(p, clock=frozen(T0))
    stack = _add_stack(p, sid)
    save_crossmatch(p, stack.id, _pipeline_rows())
    p.close()

    with open_project(root / "m-42") as q:
        assert len(load_crossmatch(q, stack.id)) == 3
        with pytest.raises(CrossmatchError, match="no frame 999"):
            save_crossmatch(q, 999, _pipeline_rows())


def test_gaia_package_exports_the_persistence_glue():
    for name in ("save_crossmatch", "load_crossmatch", "list_crossmatched_stacks"):
        assert name in gaia.__all__, name
        assert hasattr(gaia, name), name
    assert gaia.save_crossmatch is save_crossmatch
    assert gaia.load_crossmatch is load_crossmatch
    assert gaia.list_crossmatched_stacks is list_crossmatched_stacks
