"""Pure-layer tests for variable-scan ticket 01 — the scan core
(:mod:`seestar_controller.variable_scan`).

Headless: no ``gi`` anywhere (the sandbox has no GTK, see
docs/wiki/assumptions.md); a subprocess boundary test lives in
tests/test_variable_scan_boundary.py. The scans run on SYNTHETIC FITS
sub-frames with known injected fluxes (the same helpers as
tests/test_photometry_run.py: constant-flux stars at ``F0`` and ONE field
star whose flux varies sinusoidally, so the injected differential magnitude
of frame ``i`` is ``m_i = amp*sin(2*pi*i/n)``), stored in a tmp project via
:mod:`seestar_controller.store`, with a Gaia cross-match table on the stack
(:func:`~seestar_controller.store.save_crossmatch`) as the source list.

Covered: :func:`~seestar_controller.variable_scan.scan_project` over ALL
sub-frames of ALL sessions (the injected variable ranks first, the constants
stay below the threshold), the comp-ensemble differential + the field-median
fallback, the target/comp/check exclusion, the latest-stack default, skipped
unreadable frames, the error paths, the persisted threshold setting, the
min-frames filter, the persistence round-trip + accumulation across runs
(:func:`~seestar_controller.variable_scan.scan_results` /
``latest_scan_results``), the schema 6 -> 7 migration of the
``scan_results`` table, the pure stats math, and no-network.
"""

import io
import json
import math
import socket
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pytest
from astropy.io import fits as pyfits

from seestar_controller.store import (
    KIND_STACK,
    KIND_SUB,
    ROLE_CHECK,
    ROLE_COMP,
    ROLE_TARGET,
    SCHEMA_VERSION,
    StarPick,
    close_session,
    create_project,
    get_frame_path,
    initialize_db,
    list_frames,
    load_crossmatch,
    migrate_db,
    open_project,
    save_crossmatch,
    save_star_picks,
    start_session,
    store_frame,
)
from seestar_controller.variable_scan import (
    DEFAULT_MIN_FRAMES,
    DEFAULT_THRESHOLD,
    ENSEMBLE_COMPS,
    ENSEMBLE_FIELD_MEDIAN,
    SCAN_RESULTS_TABLE,
    THRESHOLD_CONFIG_KEY,
    ScanError,
    ScanReport,
    latest_scan_results,
    load_scan_threshold,
    mad,
    median,
    save_scan_results,
    scan_project,
    scan_results,
    set_scan_threshold,
    validate_threshold,
    variability_index,
)

T0 = datetime(2026, 11, 20, 22, 0, 0, tzinfo=UTC)
T1 = datetime(2026, 11, 21, 10, 0, 0, tzinfo=UTC)
T2 = datetime(2026, 11, 21, 11, 0, 0, tzinfo=UTC)

# --- synthetic frames with known injected fluxes (same recipe as test_photometry_run.py) ---------

SIZE = 64
BACKGROUND = 100.0
NOISE = 5.0
F0 = 20000.0  # constant peak flux of every star except the variable

#: Star positions as (x, y) = (column, row); all pairwise distances >= 18 px
#: (aperture 4 + annulus 12 = 16 px max extent) and >= 12 px from the edges.
TARGET_XY = (32.0, 32.0)
COMP_XYS = ((14.0, 14.0), (50.0, 14.0), (14.0, 50.0))
FIELD_XYS = ((50.0, 50.0), (32.0, 14.0), (14.0, 32.0), (50.0, 32.0))
#: The ONE variable field star (index into FIELD_XYS).
VARIABLE_XY = FIELD_XYS[0]
ALL_XYS = (TARGET_XY,) + COMP_XYS + FIELD_XYS

#: The injected sinusoid amplitude (mag) of the variable star.
AMP = 0.3


def date_obs_str(i: int, base_seconds: int = 0, seconds: int = 10) -> str:
    """The DATE-OBS of frame ``i``: T0 + base + i*seconds, naive ISOT (UTC)."""
    instant = T0 + timedelta(seconds=base_seconds + seconds * i)
    return instant.isoformat(timespec="milliseconds").replace("+00:00", "")


def var_peak(m_inj: float) -> float:
    """The peak flux of the variable star for an injected differential mag."""
    return F0 * 10 ** (-0.4 * m_inj)


def frame_data(variable_peak: float, seed: int) -> np.ndarray:
    """A synthetic 64x64 frame: flat background + Gaussian stars (sigma^2=4)
    at ALL_XYS (constant ``F0`` except the variable at ``variable_peak``)
    + noise, float32."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:SIZE, 0:SIZE]
    data = np.full((SIZE, SIZE), BACKGROUND, dtype=np.float64)
    for k, (cx, cy) in enumerate(ALL_XYS):
        peak = variable_peak if ALL_XYS[k] == VARIABLE_XY else F0
        data = data + peak * np.exp(-(((yy - cy) ** 2 + (xx - cx) ** 2) / 8.0))
    data = data + rng.normal(0.0, NOISE, size=(SIZE, SIZE))
    return np.ascontiguousarray(data, dtype=np.float32)


def fits_bytes(data: np.ndarray, *, date_obs: str | None = "2026-11-20T22:00:00.000") -> bytes:
    """A small valid FITS file with the given data (+ DATE-OBS unless None)."""
    hdu = pyfits.PrimaryHDU(data=np.asarray(data, dtype=np.float32))
    if date_obs is not None:
        hdu.header["DATE-OBS"] = date_obs
    hdu.header["EXPTIME"] = 10.0
    hdu.header["GAIN"] = 50
    hdu.header["OBJECT"] = "M 42"
    buf = io.BytesIO()
    pyfits.HDUList([hdu]).writeto(buf)
    return buf.getvalue()


def frozen(dt: datetime):
    """A frozen clock returning ``dt`` on every call."""
    return lambda: dt


def crossmatch_rows(positions=ALL_XYS, first_source_id: int = 1000) -> list[dict]:
    """Cross-match rows (dicts) for ``positions`` — all matched Gaia sources."""
    return [
        {
            "x": x,
            "y": y,
            "ra_deg": 83.0 + x * 0.001,
            "dec_deg": 22.0 + y * 0.001,
            "matched": True,
            "source_id": first_source_id + k,
            "g": 15.0,
            "bp": 15.5,
            "rp": 14.5,
            "separation_arcsec": 0.1,
        }
        for k, (x, y) in enumerate(positions)
    ]


def make_pick(role: str, label: str, xy: tuple[float, float]) -> StarPick:
    return StarPick(
        role=role,
        label=label,
        x=xy[0],
        y=xy[1],
        ra_deg=None,
        dec_deg=None,
        aperture_radius=4.0,
        sky_annulus_inner=8.0,
        sky_annulus_outer=12.0,
    )


def store_default_picks(project) -> list[StarPick]:
    """One target + 3 comps at the synthetic positions."""
    picks = [make_pick(ROLE_TARGET, "Target", TARGET_XY)] + [
        make_pick(ROLE_COMP, f"Comp {chr(65 + k)}", xy) for k, xy in enumerate(COMP_XYS)
    ]
    return save_star_picks(project, picks)


@pytest.fixture()
def root(tmp_path):
    return tmp_path / "projects"


@pytest.fixture()
def project(root):
    p = create_project("M 42 night", root=root)
    yield p
    p.close()


@pytest.fixture()
def scannable(project):
    """A project with TWO sessions of synthetic sub-frames (8 + 2), one
    stack with a Gaia cross-match table over ALL 8 sources, and the default
    target+comps picks. The first field star varies sinusoidally."""
    n1, n2 = 8, 2
    s1 = start_session(project, started_at=T0)
    subs = []
    for i in range(n1):
        m_inj = AMP * math.sin(2.0 * math.pi * i / n1)
        subs.append(
            store_frame(
                project,
                s1,
                KIND_SUB,
                fits_bytes(frame_data(var_peak(m_inj), seed=100 + i), date_obs=date_obs_str(i)),
            )
        )
    stack = store_frame(
        project, s1, KIND_STACK, fits_bytes(frame_data(F0, seed=999), date_obs=date_obs_str(50))
    )
    close_session(project, s1)
    s2 = start_session(project, started_at=T0 + timedelta(hours=1))
    for j in range(n2):
        i = n1 + j
        m_inj = AMP * math.sin(2.0 * math.pi * i / n1)
        subs.append(
            store_frame(
                project,
                s2,
                KIND_SUB,
                fits_bytes(
                    frame_data(var_peak(m_inj), seed=200 + j),
                    date_obs=date_obs_str(j, base_seconds=3600),
                ),
            )
        )
    close_session(project, s2)
    save_crossmatch(project, stack.id, crossmatch_rows())
    store_default_picks(project)
    return SimpleNamespace(subs=subs, stack=stack, n_subs=n1 + n2)


# --- the scan itself -----------------------------------------------------------------------------


def test_scan_ranks_injected_variable_first_constants_below_threshold(project, scannable):
    report = scan_project(project, clock=frozen(T1))
    assert isinstance(report, ScanReport)
    assert report.stack_frame_id == scannable.stack.id
    assert report.ensemble == ENSEMBLE_COMPS
    assert report.threshold == DEFAULT_THRESHOLD
    assert report.min_frames == DEFAULT_MIN_FRAMES
    assert report.n_frames == scannable.n_subs == 10
    assert report.skipped_frames == ()
    # the picked target/comps are excluded: exactly the 4 field stars scanned
    assert len(report.results) == len(FIELD_XYS)
    assert {(r.star.x, r.star.y) for r in report.results} == set(FIELD_XYS)
    # ranks are a 1..N permutation, results ordered by rank
    assert [r.rank for r in report.results] == [1, 2, 3, 4]
    top = report.results[0]
    assert (top.star.x, top.star.y) == VARIABLE_XY
    assert top.star.source_id == 1000 + ALL_XYS.index(VARIABLE_XY)
    assert top.variable is True
    assert top.n_frames == 10
    assert top.variability > 10.0  # ~0.21 mag MAD vs ~0.0005 mag error
    assert top.mad > 0.1
    assert top.median_err > 0.0
    # exactly one candidate: the injected variable
    assert report.candidates == (top,)
    # the constants stay below the threshold
    for r in report.results[1:]:
        assert r.variable is False
        assert r.variability < report.threshold
        assert r.n_frames == 10
    # the lightcurve points are ordered by jd and carry the injected signal
    pts = top.points
    assert len(pts) == 10
    assert [p.jd for p in pts] == sorted(p.jd for p in pts)
    assert all(p.mag_err > 0.0 for p in pts)
    mags = [p.mag for p in pts]
    assert max(mags) - min(mags) > 0.4  # the injected +-0.3 mag sinusoid


def test_scan_frozen_clock_sets_scanned_at(project, scannable):
    report = scan_project(project, clock=frozen(T1))
    assert report.scanned_at == T1.astimezone(UTC).isoformat(timespec="milliseconds")


def test_scan_field_median_fallback_with_target_only_picks(project, scannable):
    save_star_picks(project, [make_pick(ROLE_TARGET, "Target", TARGET_XY)])
    report = scan_project(project)
    assert report.ensemble == ENSEMBLE_FIELD_MEDIAN
    # the target is excluded, the 3 comps + 4 field stars are scanned
    assert len(report.results) == 7
    top = report.results[0]
    assert (top.star.x, top.star.y) == VARIABLE_XY
    assert top.variable is True
    assert report.candidates == (top,)


def test_scan_field_median_fallback_with_no_picks(project, scannable):
    save_star_picks(project, [])
    report = scan_project(project)
    assert report.ensemble == ENSEMBLE_FIELD_MEDIAN
    assert len(report.results) == len(ALL_XYS)  # nothing excluded
    top = report.results[0]
    assert (top.star.x, top.star.y) == VARIABLE_XY
    assert top.variable is True
    assert report.candidates == (top,)


def test_scan_excludes_check_pick_too(project, scannable):
    picks = [make_pick(ROLE_TARGET, "Target", TARGET_XY)]
    picks += [make_pick(ROLE_COMP, f"Comp {chr(65 + k)}", xy) for k, xy in enumerate(COMP_XYS)]
    picks.append(make_pick(ROLE_CHECK, "Check", FIELD_XYS[1]))
    save_star_picks(project, picks)
    report = scan_project(project)
    # the check star's source (FIELD_XYS[1]) is excluded as well
    assert {(r.star.x, r.star.y) for r in report.results} == {
        FIELD_XYS[0],
        FIELD_XYS[2],
        FIELD_XYS[3],
    }
    assert report.candidates[0].star.x == VARIABLE_XY[0]


def test_scan_uses_latest_crossmatched_stack_by_default(project, scannable):
    # a second, later stack with its own (smaller) cross-match table
    s3 = start_session(project, started_at=T0 + timedelta(hours=2))
    stack2 = store_frame(
        project, s3, KIND_STACK, fits_bytes(frame_data(F0, seed=998), date_obs=date_obs_str(60))
    )
    close_session(project, s3)
    save_crossmatch(project, stack2.id, crossmatch_rows(FIELD_XYS[:2], first_source_id=2000))
    report = scan_project(project)
    assert report.stack_frame_id == stack2.id
    assert len(report.results) == 2
    # an explicit stack_frame_id scans that stack's source list instead
    report1 = scan_project(project, stack_frame_id=scannable.stack.id)
    assert report1.stack_frame_id == scannable.stack.id
    assert len(report1.results) == len(FIELD_XYS)


def test_scan_skips_unreadable_frame(project, scannable):
    victim = scannable.subs[3]
    get_frame_path(project, victim).unlink()
    report = scan_project(project)
    assert report.n_frames == scannable.n_subs  # all subs were attempted
    assert [s.frame_id for s in report.skipped_frames] == [victim.id]
    assert report.skipped_frames[0].reason  # human-readable
    assert all(r.n_frames == scannable.n_subs - 1 for r in report.results)
    # the variable still ranks first on the remaining 9 frames
    assert (report.candidates[0].star.x, report.candidates[0].star.y) == VARIABLE_XY


# --- error paths ---------------------------------------------------------------------------------


def test_scan_requires_a_project_handle(project):
    with pytest.raises(TypeError):
        scan_project(None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        scan_project("M 42")  # type: ignore[arg-type]


def test_scan_without_crossmatched_stack_fails(project):
    s = start_session(project, started_at=T0)
    store_frame(project, s, KIND_SUB, fits_bytes(frame_data(F0, 1), date_obs=date_obs_str(0)))
    close_session(project, s)
    with pytest.raises(ScanError, match="no cross-matched stack"):
        scan_project(project)


def test_scan_bad_stack_frame_id(project, scannable):
    with pytest.raises(ScanError, match="no frame"):
        scan_project(project, stack_frame_id=999)
    with pytest.raises(TypeError):
        scan_project(project, stack_frame_id="1")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    # a frame without a cross-match table
    s = start_session(project, started_at=T0 + timedelta(hours=3))
    plain = store_frame(project, s, KIND_STACK, fits_bytes(frame_data(F0, 2)))
    close_session(project, s)
    with pytest.raises(ScanError, match="no cross-match table"):
        scan_project(project, stack_frame_id=plain.id)
    # an explicitly emptied cross-match table
    save_crossmatch(project, scannable.stack.id, [])
    with pytest.raises(ScanError, match="no cross-match table"):
        scan_project(project, stack_frame_id=scannable.stack.id)


def test_scan_all_sources_coinciding_with_picks_fails(project, scannable):
    save_crossmatch(project, scannable.stack.id, crossmatch_rows((TARGET_XY,) + COMP_XYS))
    with pytest.raises(ScanError, match="coincides"):
        scan_project(project)


def test_scan_invalid_parameters(project, scannable):
    for bad in (0, -1.0, "x", float("nan"), float("inf"), True):
        with pytest.raises(ScanError):
            scan_project(project, threshold=bad)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    for bad in (0, -3):
        with pytest.raises(ScanError):
            scan_project(project, min_frames=bad)  # type: ignore[arg-type]
    for bad in ("3", 2.5, True):
        with pytest.raises(TypeError):
            scan_project(project, min_frames=bad)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError):
        scan_project(project, aperture="4/8/12")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    for bad in (-1.0, "x", float("nan")):
        with pytest.raises(ScanError):
            scan_project(project, exclude_radius_px=bad)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


# --- the persisted threshold setting --------------------------------------------------------------


def test_threshold_setting_round_trip(project, root):
    assert load_scan_threshold(project) == DEFAULT_THRESHOLD
    assert THRESHOLD_CONFIG_KEY not in project.config
    assert set_scan_threshold(project, 5.5) == 5.5
    assert load_scan_threshold(project) == 5.5
    assert project.config[THRESHOLD_CONFIG_KEY] == 5.5
    # persisted on disk in project.json
    on_disk = json.loads((root / project.slug / "project.json").read_text(encoding="utf-8"))
    assert on_disk[THRESHOLD_CONFIG_KEY] == 5.5
    # survives a reopen
    project.close()
    reopened = open_project(root / project.slug)
    try:
        assert load_scan_threshold(reopened) == 5.5
    finally:
        reopened.close()


def test_threshold_setting_corrupt_value_falls_back_to_default(project):
    project.config[THRESHOLD_CONFIG_KEY] = "bogus"
    assert load_scan_threshold(project) == DEFAULT_THRESHOLD
    project.config[THRESHOLD_CONFIG_KEY] = -2.0
    assert load_scan_threshold(project) == DEFAULT_THRESHOLD
    project.config[THRESHOLD_CONFIG_KEY] = None
    assert load_scan_threshold(project) == DEFAULT_THRESHOLD


def test_set_scan_threshold_invalid_writes_nothing(project):
    for bad in (0, -1.0, "x", float("nan"), True):
        with pytest.raises(ScanError):
            set_scan_threshold(project, bad)  # type: ignore[arg-type]
    assert THRESHOLD_CONFIG_KEY not in project.config
    with pytest.raises(TypeError):
        set_scan_threshold(None, 2.0)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        load_scan_threshold("x")  # type: ignore[arg-type]


def test_validate_threshold_pure():
    assert validate_threshold(3) == 3.0
    assert validate_threshold(0.5) == 0.5
    for bad in (0, -1, "x", None, float("nan"), True):
        with pytest.raises(ScanError):
            validate_threshold(bad)  # type: ignore[arg-type]


def test_scan_uses_persisted_threshold_when_none(project, scannable):
    set_scan_threshold(project, 1e9)
    report = scan_project(project)  # threshold=None -> the persisted setting
    assert report.threshold == 1e9
    assert report.candidates == ()
    assert all(not r.variable for r in report.results)
    # the default setting flags exactly the injected variable
    set_scan_threshold(project, DEFAULT_THRESHOLD)
    report2 = scan_project(project)
    assert len(report2.candidates) == 1


def test_min_frames_filter(project, scannable):
    report = scan_project(project, min_frames=100)
    assert report.min_frames == 100
    assert report.candidates == ()
    assert all(not r.variable for r in report.results)
    assert [r.rank for r in report.results] == [1, 2, 3, 4]  # ranks still assigned
    assert all(r.n_frames == 10 for r in report.results)
    report2 = scan_project(project, min_frames=10)
    assert len(report2.candidates) == 1  # 10 frames >= 10 -> still flagged


# --- persistence ----------------------------------------------------------------------------------


def test_scan_persists_results_and_runs_accumulate(project, scannable):
    report1 = scan_project(project, clock=frozen(T1))
    rows1 = scan_results(project)
    assert len(rows1) == len(FIELD_XYS)
    latest = latest_scan_results(project)
    assert len(latest) == len(FIELD_XYS)
    # round-trip: the persisted rows match the report, ordered by rank
    assert [r.scanned_at for r in latest] == [report1.scanned_at] * len(latest)
    for rec, res in zip(latest, report1.results):
        assert rec.stack_frame_id == report1.stack_frame_id
        assert rec.source_id == res.star.source_id
        assert rec.ra_deg == res.star.ra_deg
        assert rec.dec_deg == res.star.dec_deg
        assert rec.x == res.star.x
        assert rec.y == res.star.y
        assert rec.n_frames == res.n_frames
        assert rec.mad == res.mad
        assert rec.median_err == res.median_err
        assert rec.variability == res.variability
        assert rec.variable == res.variable
        assert rec.rank == res.rank
        assert rec.threshold == report1.threshold
        assert rec.min_frames == report1.min_frames
        assert rec.id > 0
    # a second run (later clock) accumulates: newest run first
    report2 = scan_project(project, clock=frozen(T2))
    rows2 = scan_results(project)
    assert len(rows2) == 2 * len(FIELD_XYS)
    assert {r.scanned_at for r in rows2} == {report1.scanned_at, report2.scanned_at}
    assert all(r.scanned_at == report2.scanned_at for r in rows2[: len(FIELD_XYS)])
    latest2 = latest_scan_results(project)
    assert len(latest2) == len(FIELD_XYS)
    assert all(r.scanned_at == report2.scanned_at for r in latest2)
    assert [r.rank for r in latest2] == [1, 2, 3, 4]
    # row ids are unique and increasing
    assert len({r.id for r in rows2}) == len(rows2)


def test_persist_type_errors(project, scannable):
    report = scan_project(project)
    with pytest.raises(TypeError):
        save_scan_results(None, report)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        save_scan_results(project, "not a report")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError):
        scan_results(None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        latest_scan_results(None)  # type: ignore[arg-type]
    # nothing extra was written by the failed calls
    assert len(scan_results(project)) == len(report.results)


def test_scan_results_empty_project(project):
    assert scan_results(project) == ()
    assert latest_scan_results(project) == ()


# --- schema migration 6 -> 7 ----------------------------------------------------------------------


def test_migration_6_to_7_in_memory():
    conn = sqlite3.connect(":memory:")
    try:
        initialize_db(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 8
        conn.execute("DROP TABLE scan_results")
        conn.execute("PRAGMA user_version=6")
        assert migrate_db(conn) == SCHEMA_VERSION
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({SCAN_RESULTS_TABLE})")]
        assert cols == [
            "id", "scanned_at", "stack_frame_id", "source_id", "ra_deg", "dec_deg",
            "x", "y", "n_frames", "mad", "median_err", "variability", "variable",
            "candidate_rank", "threshold", "min_frames",
        ]
        fks = conn.execute(f"PRAGMA foreign_key_list({SCAN_RESULTS_TABLE})").fetchall()
        assert len(fks) == 1
        # PRAGMA foreign_key_list columns: id, seq, table, from, to, ..., on_delete
        assert fks[0][2] == "frames"
        assert fks[0][3] == "stack_frame_id" and fks[0][4] == "id"
        assert fks[0][6] == "CASCADE"
        indexes = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?",
                (SCAN_RESULTS_TABLE,),
            )
        }
        assert "idx_scan_results_stack_frame_id" in indexes
        assert "idx_scan_results_scanned_at" in indexes
        # idempotent: re-running the migration on a v6 db that already has the
        # table (IF NOT EXISTS) succeeds
        conn.execute("PRAGMA user_version=6")
        assert migrate_db(conn) == SCHEMA_VERSION
    finally:
        conn.close()


def test_reopen_migrates_v6_db_to_v7_and_preserves_rows(root):
    project = create_project("Mig night", root=root)
    slug = project.slug
    session = start_session(project, started_at=T0)
    frame = store_frame(
        project, session, KIND_SUB, fits_bytes(frame_data(F0, 1), date_obs=date_obs_str(0))
    )
    close_session(project, session)
    # simulate a v6 db: drop the scan_results table, stamp user_version=6
    project.db.execute(f"DROP TABLE {SCAN_RESULTS_TABLE}")
    project.db.execute("PRAGMA user_version=6")
    project.config["schema_version"] = 6
    project.save_config()
    project.close()
    reopened = open_project(root / slug)
    try:
        assert reopened.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        tables = {
            r[0]
            for r in reopened.db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert SCAN_RESULTS_TABLE in tables
        # existing rows preserved
        assert [f.id for f in list_frames(reopened)] == [frame.id]
        # the new version is mirrored into project.json
        assert reopened.config["schema_version"] == 8
        on_disk = json.loads((root / slug / "project.json").read_text(encoding="utf-8"))
        assert on_disk["schema_version"] == 8
        # idempotent re-open after another downgrade (table already exists)
        reopened.db.execute("PRAGMA user_version=6")
    finally:
        reopened.close()
    again = open_project(root / slug)
    try:
        assert again.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert [f.id for f in list_frames(again)] == [frame.id]
    finally:
        again.close()


def test_scan_results_cascade_delete_with_stack(project, scannable):
    scan_project(project, clock=frozen(T1))
    assert len(scan_results(project)) == len(FIELD_XYS)
    with project.db:
        project.db.execute("DELETE FROM frames WHERE id = ?", (scannable.stack.id,))
    assert scan_results(project) == ()
    assert latest_scan_results(project) == ()


# --- pure stats -----------------------------------------------------------------------------------


def test_stats_median_mad_variability():
    assert median([1.0, 2.0, 3.0]) == 2.0
    assert median([1.0, 2.0, 3.0, 4.0]) == 2.5
    assert median([5]) == 5.0
    assert mad([1.0, 1.0, 1.0, 1.0]) == 0.0
    assert mad([1.0, 2.0, 3.0]) == 1.0
    assert mad([-2.0, 0.0, 2.0]) == 2.0
    assert variability_index(2.0, 1.0) == 2.0
    assert variability_index(0.0, 0.0) == 0.0
    assert variability_index(1.0, 0.0) == math.inf
    assert variability_index(0.0, 5.0) == 0.0
    with pytest.raises(ValueError):
        median([])
    with pytest.raises(ValueError):
        mad([])
    with pytest.raises(ValueError):
        median([1.0, float("nan")])
    with pytest.raises(ValueError):
        mad([float("inf")])
    with pytest.raises(ValueError):
        median(["x"])  # type: ignore[list-item]  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError):
        variability_index(-1.0, 1.0)
    with pytest.raises(ValueError):
        variability_index(1.0, -1.0)
    with pytest.raises(ValueError):
        variability_index(float("nan"), 1.0)
    with pytest.raises(ValueError):
        variability_index(True, 1.0)


# --- no network -----------------------------------------------------------------------------------


def test_scan_never_touches_the_network(project, scannable, monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    report = scan_project(project)
    assert len(report.candidates) == 1
    assert len(scan_results(project)) == len(FIELD_XYS)


def test_crossmatch_source_list_is_read_from_the_stack(project, scannable):
    # the scan's picks are exactly the stack's cross-match table minus the
    # picked target/comps (sanity: the store API the scan builds on)
    rows = load_crossmatch(project, scannable.stack.id)
    assert len(rows) == len(ALL_XYS)
    assert all(r.matched for r in rows)
