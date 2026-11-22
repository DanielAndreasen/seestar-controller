"""Astrometry ticket 01 — WCS persistence (store v7) + the project glue.

Headless + no network: the Gaia fallback's reference query is INJECTED with
synthetic Gaia stars at the true sky positions of the injected image stars.
Covers :mod:`seestar_controller.store.wcs` (``save_frame_wcs`` /
``load_frame_wcs`` / ``list_solved_frames``, validation, upsert, cascade,
corrupt rows, the 7 -> 8 migration) and :mod:`seestar_controller.astrometry.session`
(``solve_session_frame`` / ``solve_project_frame``: FITS header rewrite +
db record + cross-match rows, and failure leaving nothing behind).
"""

from __future__ import annotations

import io
import json
import math
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits as pyfits
from astropy.wcs import WCS

from seestar_controller import store
from seestar_controller.astrometry import (
    SOLVER_GAIA,
    SolveError,
    cards_to_wcs,
    has_celestial_wcs,
    solve_project_frame,
    solve_session_frame,
)
from seestar_controller.gaia.query import GaiaStar
from seestar_controller.store import (
    DB_FILE,
    KIND_STACK,
    KIND_SUB,
    PROJECT_FILE,
    SCHEMA_VERSION,
    FrameStoreError,
    SessionNotFoundError,
    WcsStoreError,
    create_project,
    list_frames,
    list_solved_frames,
    load_crossmatch,
    load_frame_wcs,
    migrate_db,
    open_project,
    save_frame_wcs,
    start_session,
    store_frame,
)
from seestar_controller.store.frames import get_frame_path

# --- fixtures ---------------------------------------------------------------------------


@pytest.fixture()
def root(tmp_path):
    """A fresh project root."""
    return tmp_path / "projects"


@pytest.fixture()
def project(root):
    """An open project (closed at teardown)."""
    p = create_project("M 42", root=root)
    yield p
    p.close()


def frozen(dt: datetime):
    return lambda: dt


T0 = datetime(2026, 11, 20, 22, 0, 0, tzinfo=UTC)


# --- synthetic frame (same recipe as tests/test_astrometry_solve.py) ---------------------

SCALE = 0.001
SCALE_ARCSEC = SCALE * 3600.0
SIZE = 256
CRVAL = (83.0, 22.0)
STAR_SIGMA = 1.5
BACKGROUND = 100.0
NOISE = 2.0
STARS = (
    (30, 30, 20000.0),
    (30, 120, 18000.0),
    (90, 60, 15000.0),
    (90, 170, 12000.0),
    (60, 100, 9000.0),
    (150, 40, 8000.0),
    (150, 200, 7000.0),
    (200, 90, 6000.0),
    (200, 180, 5500.0),
    (120, 220, 5000.0),
)


def true_header() -> dict:
    return {
        "CTYPE1": "RA---TAN",
        "CTYPE2": "DEC--TAN",
        "CRVAL1": CRVAL[0],
        "CRVAL2": CRVAL[1],
        "CRPIX1": (SIZE + 1) / 2.0,
        "CRPIX2": (SIZE + 1) / 2.0,
        "CDELT1": -SCALE,
        "CDELT2": SCALE,
    }


def make_image(seed: int = 42) -> np.ndarray:
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:SIZE, 0:SIZE]
    data = np.full((SIZE, SIZE), BACKGROUND, dtype=np.float64)
    for cy, cx, peak in STARS:
        data = data + peak * np.exp(-(((yy - cy) ** 2 + (xx - cx) ** 2) / (2.0 * STAR_SIGMA**2)))
    data = data + rng.normal(0.0, NOISE, size=(SIZE, SIZE))
    return np.ascontiguousarray(data, dtype=np.float32)


def gaia_stars_at_stars() -> list[GaiaStar]:
    wcs = WCS(true_header())
    stars = []
    for i, (cy, cx, _peak) in enumerate(STARS):
        coord = wcs.pixel_to_world(cx, cy)
        stars.append(
            GaiaStar(source_id=1000 + i, ra_deg=coord.ra.deg, dec_deg=coord.dec.deg, g=12.0)
        )
    return stars


def fake_query(stars: list[GaiaStar]):
    def query(center_ra: float, center_dec: float, radius_deg: float):
        return stars

    return query


def fits_bytes(data: np.ndarray, date_obs: str, header: dict | None = None) -> bytes:
    """A valid FITS file with ``data``, a DATE-OBS card and extra ``header``."""
    hdu = pyfits.PrimaryHDU(data=np.asarray(data, dtype=np.float32))
    hdu.header["DATE-OBS"] = date_obs
    hdu.header["RA"] = CRVAL[0]
    hdu.header["DEC"] = CRVAL[1]
    for key, value in (header or {}).items():
        hdu.header[key] = value
    buf = io.BytesIO()
    pyfits.HDUList([hdu]).writeto(buf)
    return buf.getvalue()


def add_sub(project, session_id: str, i: int, base: datetime = T0):
    """Store one synthetic sub (DATE-OBS = base + i*10s) and return its Frame."""
    instant = base + timedelta(seconds=10 * i)
    return store_frame(
        project,
        session_id,
        KIND_SUB,
        fits_bytes(make_image(seed=42 + i), instant.isoformat(timespec="milliseconds")),
    )


def residual_arcsec(wcs: WCS, x: float, y: float) -> float:
    a = wcs.pixel_to_world(x, y)
    b = WCS(true_header()).pixel_to_world(x, y)
    return math.hypot(
        (a.ra.deg - b.ra.deg) * 3600.0 * math.cos(math.radians(b.dec.deg)),
        (a.dec.deg - b.dec.deg) * 3600.0,
    )


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


# --- save_frame_wcs / load_frame_wcs / list_solved_frames ---------------------------------


def test_save_and_load_frame_wcs_roundtrip(project):
    sid = start_session(project, clock=frozen(T0))
    frame = add_sub(project, sid, 0)
    saved = save_frame_wcs(
        project,
        frame.id,
        solver="gaia",
        n_stars=10,
        center_ra_deg=83.0,
        center_dec_deg=22.0,
        cards=CARDS,
        rms_arcsec=0.42,
        solved_at="2026-11-20T22:05:00.000+00:00",
    )
    assert saved.frame_id == frame.id
    assert saved.session_id == sid
    assert saved.solver == "gaia"
    assert saved.solved_at == "2026-11-20T22:05:00.000+00:00"
    assert saved.rms_arcsec == pytest.approx(0.42)
    assert saved.n_stars == 10
    assert saved.center_ra_deg == pytest.approx(83.0)
    assert saved.center_dec_deg == pytest.approx(22.0)
    assert saved.cards == CARDS

    loaded = load_frame_wcs(project, frame.id)
    assert loaded is not None
    assert loaded == saved
    # RA is normalized to [0, 360)
    save_frame_wcs(
        project, frame.id, solver="gaia", n_stars=1,
        center_ra_deg=443.0, center_dec_deg=22.0, cards=CARDS,
    )
    reloaded = load_frame_wcs(project, frame.id)
    assert reloaded is not None
    assert reloaded.center_ra_deg == pytest.approx(83.0)
    # to_wcs rebuilds a working astropy WCS (lazy import)
    wcs = reloaded.to_wcs()
    assert wcs.celestial is not None
    assert residual_arcsec(wcs, 100.0, 150.0) < 1e-6


def test_save_frame_wcs_upsert_replaces_and_refreshes(project):
    sid = start_session(project, clock=frozen(T0))
    frame = add_sub(project, sid, 0)
    save_frame_wcs(
        project, frame.id, solver="gaia", n_stars=10,
        center_ra_deg=83.0, center_dec_deg=22.0, cards=CARDS, rms_arcsec=0.9,
        solved_at="2026-11-20T22:05:00.000+00:00",
    )
    save_frame_wcs(
        project, frame.id, solver="solve-field", n_stars=0,
        center_ra_deg=83.001, center_dec_deg=22.0, cards=CARDS,
        solved_at="2026-11-20T23:00:00.000+00:00",
    )
    loaded = load_frame_wcs(project, frame.id)
    assert loaded is not None
    assert loaded.solver == "solve-field"
    assert loaded.n_stars == 0
    assert loaded.rms_arcsec is None
    assert loaded.center_ra_deg == pytest.approx(83.001)
    assert loaded.solved_at == "2026-11-20T23:00:00.000+00:00"
    # still exactly one row
    rows = project.db.execute(
        "SELECT COUNT(*) FROM frame_wcs WHERE frame_id = ?", (frame.id,)
    ).fetchone()
    assert rows[0] == 1


def test_save_frame_wcs_default_solved_at_is_iso_now(project):
    sid = start_session(project, clock=frozen(T0))
    frame = add_sub(project, sid, 0)
    saved = save_frame_wcs(
        project, frame.id, solver="gaia", n_stars=3,
        center_ra_deg=83.0, center_dec_deg=22.0, cards=CARDS,
    )
    assert saved.solved_at.endswith("+00:00")
    datetime.fromisoformat(saved.solved_at)  # parses


def test_load_frame_wcs_missing_returns_none(project):
    sid = start_session(project, clock=frozen(T0))
    frame = add_sub(project, sid, 0)
    assert load_frame_wcs(project, frame.id) is None
    assert load_frame_wcs(project, 999999) is None  # unknown id is a filter
    with pytest.raises(TypeError):
        load_frame_wcs(project, "1")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


def test_list_solved_frames_orders_by_jd_and_filters_session(project):
    sid = start_session(project, clock=frozen(T0))
    f0 = add_sub(project, sid, 0)
    f1 = add_sub(project, sid, 1)
    f2 = add_sub(project, sid, 2)
    save_frame_wcs(project, f2.id, solver="gaia", n_stars=1, center_ra_deg=83.0, center_dec_deg=22.0, cards=CARDS)
    save_frame_wcs(project, f0.id, solver="gaia", n_stars=1, center_ra_deg=83.0, center_dec_deg=22.0, cards=CARDS)
    solved = list_solved_frames(project)
    assert [w.frame_id for w in solved] == [f0.id, f2.id]  # by jd, not by save order
    assert [w.frame_id for w in list_solved_frames(project, session_id=sid)] == [f0.id, f2.id]
    with pytest.raises(SessionNotFoundError):
        list_solved_frames(project, session_id="no-such-session")
    with pytest.raises(TypeError):
        list_solved_frames(project, session_id=42)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    # a second session's frames are listed separately
    from seestar_controller.store import close_session

    close_session(project, sid, clock=frozen(T0 + timedelta(hours=2)))
    sid2 = start_session(project, clock=frozen(T0 + timedelta(hours=3)))
    f3 = add_sub(project, sid2, 0, base=T0 + timedelta(hours=3))
    save_frame_wcs(project, f3.id, solver="gaia", n_stars=1, center_ra_deg=83.0, center_dec_deg=22.0, cards=CARDS)
    assert [w.frame_id for w in list_solved_frames(project, session_id=sid2)] == [f3.id]
    assert [w.session_id for w in list_solved_frames(project)] == [sid, sid, sid2]
    assert f1.id not in {w.frame_id for w in list_solved_frames(project)}


def test_save_frame_wcs_unknown_frame_raises_and_writes_nothing(project):
    with pytest.raises(WcsStoreError, match="no frame 999999"):
        save_frame_wcs(
            project, 999999, solver="gaia", n_stars=1,
            center_ra_deg=83.0, center_dec_deg=22.0, cards=CARDS,
        )
    assert project.db.execute("SELECT COUNT(*) FROM frame_wcs").fetchone()[0] == 0
    with pytest.raises(TypeError):
        save_frame_wcs(
            project, "1", solver="gaia", n_stars=1,  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
            center_ra_deg=83.0, center_dec_deg=22.0, cards=CARDS,
        )


def test_save_frame_wcs_validation(project):
    sid = start_session(project, clock=frozen(T0))
    frame = add_sub(project, sid, 0)
    base = {"solver": "gaia", "n_stars": 10, "center_ra_deg": 83.0, "center_dec_deg": 22.0, "cards": CARDS}
    with pytest.raises(WcsStoreError, match="solver"):
        save_frame_wcs(project, frame.id, **{**base, "solver": ""})  # ty: ignore[invalid-argument-type]
    with pytest.raises(WcsStoreError, match="solver"):
        save_frame_wcs(project, frame.id, **{**base, "solver": 42})  # ty: ignore[invalid-argument-type]
    with pytest.raises(WcsStoreError, match="n_stars"):
        save_frame_wcs(project, frame.id, **{**base, "n_stars": -1})  # ty: ignore[invalid-argument-type]
    with pytest.raises(WcsStoreError, match="n_stars"):
        save_frame_wcs(project, frame.id, **{**base, "n_stars": 1.5})  # ty: ignore[invalid-argument-type]
    with pytest.raises(WcsStoreError, match="n_stars"):
        save_frame_wcs(project, frame.id, **{**base, "n_stars": True})  # ty: ignore[invalid-argument-type]
    with pytest.raises(WcsStoreError, match="center_ra_deg"):
        save_frame_wcs(project, frame.id, **{**base, "center_ra_deg": "83"})  # ty: ignore[invalid-argument-type]
    with pytest.raises(WcsStoreError, match="center_ra_deg"):
        save_frame_wcs(project, frame.id, **{**base, "center_ra_deg": float("nan")})  # ty: ignore[invalid-argument-type]
    with pytest.raises(WcsStoreError, match="center_dec_deg"):
        save_frame_wcs(project, frame.id, **{**base, "center_dec_deg": -91.0})  # ty: ignore[invalid-argument-type]
    with pytest.raises(WcsStoreError, match="center_dec_deg"):
        save_frame_wcs(project, frame.id, **{**base, "center_dec_deg": "22"})  # ty: ignore[invalid-argument-type]
    with pytest.raises(WcsStoreError, match="rms_arcsec"):
        save_frame_wcs(project, frame.id, **{**base, "rms_arcsec": -0.1})  # ty: ignore[invalid-argument-type]
    with pytest.raises(WcsStoreError, match="rms_arcsec"):
        save_frame_wcs(project, frame.id, **{**base, "rms_arcsec": float("inf")})  # ty: ignore[invalid-argument-type]
    with pytest.raises(WcsStoreError, match="solved_at"):
        save_frame_wcs(project, frame.id, **{**base, "solved_at": ""})  # ty: ignore[invalid-argument-type]
    with pytest.raises(WcsStoreError, match="solved_at"):
        save_frame_wcs(project, frame.id, **{**base, "solved_at": 123})  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="cards"):
        save_frame_wcs(project, frame.id, **{**base, "cards": ["not", "a", "mapping"]})  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    with pytest.raises(WcsStoreError, match="JSON-serializable"):
        save_frame_wcs(project, frame.id, **{**base, "cards": {"CRVAL1": object()}})  # ty: ignore[invalid-argument-type]
    # nothing was written by any of the rejected saves
    assert load_frame_wcs(project, frame.id) is None
    # rms_arcsec=None is fine
    saved = save_frame_wcs(
        project, frame.id, solver="gaia", n_stars=10,
        center_ra_deg=83.0, center_dec_deg=22.0, cards=CARDS,
    )
    assert saved.rms_arcsec is None


def test_load_frame_wcs_corrupt_cards_json_raises(project):
    sid = start_session(project, clock=frozen(T0))
    frame = add_sub(project, sid, 0)
    save_frame_wcs(
        project, frame.id, solver="gaia", n_stars=1,
        center_ra_deg=83.0, center_dec_deg=22.0, cards=CARDS,
    )
    with project.db:
        project.db.execute(
            "UPDATE frame_wcs SET cards_json = ? WHERE frame_id = ?",
            ("{not json", frame.id),
        )
    with pytest.raises(WcsStoreError, match="corrupt cards_json"):
        load_frame_wcs(project, frame.id)
    with project.db:
        project.db.execute(
            "UPDATE frame_wcs SET cards_json = ? WHERE frame_id = ?",
            ('["a", "list"]', frame.id),
        )
    with pytest.raises(WcsStoreError, match="invalid cards_json"):
        load_frame_wcs(project, frame.id)


def test_frame_wcs_cascades_on_frame_delete(project):
    sid = start_session(project, clock=frozen(T0))
    frame = add_sub(project, sid, 0)
    save_frame_wcs(
        project, frame.id, solver="gaia", n_stars=1,
        center_ra_deg=83.0, center_dec_deg=22.0, cards=CARDS,
    )
    assert load_frame_wcs(project, frame.id) is not None
    with project.db:
        project.db.execute("DELETE FROM frames WHERE id = ?", (frame.id,))
    assert load_frame_wcs(project, frame.id) is None
    assert list_solved_frames(project) == ()


def test_store_exports_wcs_api():
    for name in (
        "FrameWcs",
        "WcsStoreError",
        "list_solved_frames",
        "load_frame_wcs",
        "save_frame_wcs",
    ):
        assert name in store.__all__
        assert hasattr(store, name)
    assert issubclass(store.WcsStoreError, store.ProjectError)


# --- migration 7 -> 8 ---------------------------------------------------------------------


def make_v7_project(root: Path) -> tuple[Path, str, int]:
    """A v7 project: the current-build schema MINUS the ``frame_wcs`` table,
    ``user_version=7`` and ``schema_version=7`` in project.json, with one
    session + one frame row that must survive the migration."""
    p = create_project("M 42", root=root)
    sid = start_session(p, clock=frozen(T0))
    frame = add_sub(p, sid, 0)
    with p.db:
        p.db.execute("DROP TABLE frame_wcs")
        p.db.execute("PRAGMA user_version=7")
    p.config["schema_version"] = 7
    p.save_config()
    p.close()
    return root / "m-42", sid, frame.id


def table_names(db) -> set[str]:
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_open_migrates_v7_project_creating_frame_wcs(root):
    project_dir, _sid, frame_id = make_v7_project(root)
    # sanity: the db really is a v7 db without the frame_wcs table
    conn = sqlite3.connect(project_dir / DB_FILE)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
    assert "frame_wcs" not in table_names(conn)
    conn.close()

    with open_project(project_dir) as p:
        assert p.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 9
        assert "frame_wcs" in table_names(p.db)
        cols = [row[1] for row in p.db.execute("PRAGMA table_info(frame_wcs)")]
        assert cols == [
            "frame_id", "solver", "solved_at", "rms_arcsec", "n_stars",
            "center_ra_deg", "center_dec_deg", "cards_json",
        ]
        # the pre-existing rows are untouched
        assert [f.id for f in list_frames(p)] == [frame_id]
        assert p.config["schema_version"] == SCHEMA_VERSION
        # the migrated project is fully usable: a WCS record can be saved
        saved = save_frame_wcs(
            p, frame_id, solver="gaia", n_stars=10,
            center_ra_deg=83.0, center_dec_deg=22.0, cards=CARDS, rms_arcsec=0.4,
        )
        assert load_frame_wcs(p, frame_id) == saved
    on_disk = json.loads((project_dir / PROJECT_FILE).read_text(encoding="utf-8"))
    assert on_disk["schema_version"] == SCHEMA_VERSION


def test_migrate_db_6_to_7_creates_frame_wcs_and_is_idempotent(tmp_path):
    conn = sqlite3.connect(tmp_path / "db.sqlite")
    store.initialize_db(conn)
    with conn:
        conn.execute("DROP TABLE frame_wcs")
        conn.execute("PRAGMA user_version=7")
    assert "frame_wcs" not in table_names(conn)
    assert migrate_db(conn) == SCHEMA_VERSION == 9
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 9
    assert "frame_wcs" in table_names(conn)
    # re-running the 7 -> 8 step on a db that already has the table is a no-op
    conn.execute("PRAGMA user_version=7")
    assert migrate_db(conn) == SCHEMA_VERSION
    assert "frame_wcs" in table_names(conn)
    conn.close()


def test_fresh_project_is_v8_with_frame_wcs(project):
    assert SCHEMA_VERSION == 9
    assert project.db.execute("PRAGMA user_version").fetchone()[0] == 9
    assert "frame_wcs" in table_names(project.db)
    assert project.config["schema_version"] == 9


# --- the project glue: solve_session_frame / solve_project_frame ---------------------------


def test_solve_session_frame_persists_wcs_header_and_matches(project):
    sid = start_session(project, clock=frozen(T0))
    frame = add_sub(project, sid, 0)
    result = solve_session_frame(
        project,
        sid,
        frame.id,
        solver=SOLVER_GAIA,
        gaia_query=fake_query(gaia_stars_at_stars()),
        pixel_scale_hint=SCALE_ARCSEC,
    )
    assert result.solver == SOLVER_GAIA
    assert result.n_stars == len(STARS)

    # 1. the WCS record is in the db
    record = load_frame_wcs(project, frame.id)
    assert record is not None
    assert record.session_id == sid
    assert record.solver == SOLVER_GAIA
    assert record.n_stars == len(STARS)
    assert record.rms_arcsec is not None and record.rms_arcsec < 0.1
    assert record.center_ra_deg == pytest.approx(CRVAL[0], abs=0.01)
    assert record.center_dec_deg == pytest.approx(CRVAL[1], abs=0.01)
    assert record.solved_at == result.solved_at
    wcs = record.to_wcs()
    for cy, cx, _peak in STARS:
        assert residual_arcsec(wcs, cx, cy) < 0.5

    # 2. the stored FITS file on disk now carries the solved WCS
    path = get_frame_path(project, frame)
    header = pyfits.getheader(str(path))
    assert has_celestial_wcs(header)
    wcs_disk = cards_to_wcs(header)
    for cy, cx, _peak in STARS:
        assert residual_arcsec(wcs_disk, cx, cy) < 0.5

    # 3. the cross-match rows were persisted
    rows = load_crossmatch(project, frame.id)
    assert len(rows) >= len(STARS)
    assert sum(1 for r in rows if r.matched) == len(STARS)

    # 4. list_solved_frames sees it
    assert [w.frame_id for w in list_solved_frames(project, session_id=sid)] == [frame.id]


def test_solve_project_frame_rgb_stack_both_layouts(project):
    # issue 04: a Seestar stack is an RGB FITS — (3, H, W) channel-first and
    # (H, W, 3) channel-last — and must solve through the project path
    # (prepare_project_frame -> read_fits_bytes -> 2-D luminance): WCS
    # written into the stored FITS + db record with the matched-star count
    sid = start_session(project, clock=frozen(T0))
    base = make_image()
    rgb_cf = np.stack([base, base * 0.8, base * 0.6], axis=0)  # (3, SIZE, SIZE)
    instant = (T0 + timedelta(seconds=5)).isoformat(timespec="milliseconds")
    for rgb in (rgb_cf, np.moveaxis(rgb_cf, 0, -1)):  # channel-first and channel-last
        frame = store_frame(project, sid, KIND_STACK, fits_bytes(rgb, instant))
        result = solve_project_frame(
            project,
            frame.id,
            solver=SOLVER_GAIA,
            gaia_query=fake_query(gaia_stars_at_stars()),
            pixel_scale_hint=SCALE_ARCSEC,
        )
        assert result.solver == SOLVER_GAIA
        assert result.n_stars == len(STARS)
        record = load_frame_wcs(project, frame.id)
        assert record is not None
        assert record.n_stars == len(STARS)
        assert record.center_ra_deg == pytest.approx(CRVAL[0], abs=0.01)
        wcs_disk = cards_to_wcs(pyfits.getheader(str(get_frame_path(project, frame))))
        for cy, cx, _peak in STARS:
            assert residual_arcsec(wcs_disk, cx, cy) < 0.5


def test_solve_project_frame_accepts_frame_and_id_and_flags(project):
    sid = start_session(project, clock=frozen(T0))
    f0 = add_sub(project, sid, 0)
    f1 = add_sub(project, sid, 1)
    query = fake_query(gaia_stars_at_stars())
    r0 = solve_project_frame(
        project, f0, solver=SOLVER_GAIA, gaia_query=query, pixel_scale_hint=SCALE_ARCSEC
    )
    assert r0.solver == SOLVER_GAIA
    r1 = solve_project_frame(
        project, f1.id, solver=SOLVER_GAIA, gaia_query=query, pixel_scale_hint=SCALE_ARCSEC,
        write_header=False, persist=False, save_matches=False,
    )
    assert r1.solver == SOLVER_GAIA
    # flags off: no db record, no header WCS, no crossmatch rows
    assert load_frame_wcs(project, f1.id) is None
    assert not has_celestial_wcs(pyfits.getheader(str(get_frame_path(project, f1))))
    assert load_crossmatch(project, f1.id) == ()
    # but the first frame was fully persisted
    assert load_frame_wcs(project, f0.id) is not None
    assert has_celestial_wcs(pyfits.getheader(str(get_frame_path(project, f0))))
    with pytest.raises(FrameStoreError, match="no frame"):
        solve_project_frame(project, 999999, solver=SOLVER_GAIA, gaia_query=query)
    with pytest.raises(TypeError):
        solve_project_frame(project, "1", solver=SOLVER_GAIA, gaia_query=query)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    stale = store_frame(
        project, sid, KIND_SUB,
        fits_bytes(make_image(), (T0 + timedelta(seconds=99)).isoformat(timespec="milliseconds")),
    )
    bogus = type(f0)(id=999999, session_id=sid, kind=stale.kind, path=stale.path,
                     jd=stale.jd, utc=stale.utc, exposure=None, gain=None, target=None)
    with pytest.raises(FrameStoreError, match="no frame 999999"):
        solve_project_frame(project, bogus, solver=SOLVER_GAIA, gaia_query=query)


def test_solve_session_frame_unknown_session_and_frame(project):
    sid = start_session(project, clock=frozen(T0))
    frame = add_sub(project, sid, 0)
    with pytest.raises(SessionNotFoundError, match="no session"):
        solve_session_frame(project, "no-such-session", frame.id, solver=SOLVER_GAIA,
                            gaia_query=fake_query(gaia_stars_at_stars()))
    with pytest.raises(FrameStoreError, match="no frame"):
        solve_session_frame(project, sid, 999999, solver=SOLVER_GAIA,
                            gaia_query=fake_query(gaia_stars_at_stars()))
    with pytest.raises(TypeError):
        solve_session_frame(project, 42, frame.id)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError):
        solve_session_frame(project, sid, "1")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


def test_failed_solve_leaves_no_wcs_no_header_no_rows(project):
    sid = start_session(project, clock=frozen(T0))
    # a starless sub: the Gaia solver cannot find any source
    instant = T0 + timedelta(seconds=10)
    rng = np.random.default_rng(7)
    blank = np.ascontiguousarray(
        np.full((SIZE, SIZE), BACKGROUND) + rng.normal(0.0, NOISE, (SIZE, SIZE)),
        dtype=np.float32,
    )
    frame = store_frame(
        project, sid, KIND_SUB,
        fits_bytes(blank, instant.isoformat(timespec="milliseconds")),
    )
    path = get_frame_path(project, frame)
    before = path.read_bytes()
    with pytest.raises(SolveError, match="only 0 source"):
        solve_session_frame(
            project, sid, frame.id, solver=SOLVER_GAIA,
            gaia_query=fake_query(gaia_stars_at_stars()), pixel_scale_hint=SCALE_ARCSEC,
        )
    # nothing was written: no db record, no crossmatch rows, the FITS file is
    # byte-identical (no header WCS)
    assert load_frame_wcs(project, frame.id) is None
    assert load_crossmatch(project, frame.id) == ()
    assert path.read_bytes() == before
    assert not has_celestial_wcs(pyfits.getheader(str(path)))
