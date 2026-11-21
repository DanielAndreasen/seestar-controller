"""Project store: persisted Gaia cross-match tables — tmp_path, no network.

Covers :mod:`seestar_controller.store.crossmatch` (gaia-crossmatch ticket 02,
schema v6): ``save_crossmatch`` (validated upsert of one frame's whole table
in one transaction, frame-exists check, duck-typed rows), ``load_crossmatch``,
``list_crossmatched_stacks``, cascade delete with the frame, the v5 -> 6
migration, validation errors, and the public exports.
"""

import json
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from seestar_controller import store
from seestar_controller.store import (
    DB_FILE,
    KIND_STACK,
    KIND_SUB,
    PROJECT_FILE,
    SCHEMA_VERSION,
    CrossmatchError,
    CrossmatchRecord,
    create_project,
    initialize_db,
    list_crossmatched_stacks,
    list_frames,
    load_crossmatch,
    migrate_db,
    open_project,
    save_crossmatch,
    start_session,
    store_frame,
)


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
JD1 = 2461365.416668
UTC1 = "2026-11-20T22:00:00.000+00:00"


def _fits_bytes(date_obs: str) -> bytes:
    """A minimal valid FITS file (the crossmatch tests never read the pixels)."""
    import io

    import numpy as np
    from astropy.io import fits as pyfits

    hdu = pyfits.PrimaryHDU(data=np.zeros((8, 8), dtype=np.float32))
    hdu.header["DATE-OBS"] = date_obs
    buf = io.BytesIO()
    pyfits.HDUList([hdu]).writeto(buf)
    return buf.getvalue()


def _add_stack(project, session_id: str, i: int = 0):
    """Store one stack frame with DATE-OBS = T0 + i*10s and return its Frame."""
    instant = T0 + timedelta(seconds=10 * i)
    return store_frame(
        project,
        session_id,
        KIND_STACK,
        _fits_bytes(instant.isoformat(timespec="milliseconds").replace("+00:00", "")),
    )


def _add_frame_row(project, session_id, jd, utc, kind="stack") -> int:
    """Insert a ``frames`` row directly (no FITS file needed) and return its id."""
    with project.db:
        cur = project.db.execute(
            "INSERT INTO frames (session_id, kind, path, jd, utc) VALUES (?, ?, ?, ?, ?)",
            (session_id, kind, f"stacks/{session_id}/{jd:.6f}.fits", jd, utc),
        )
        return int(cur.lastrowid)


def _row(**overrides) -> dict:
    """A valid matched crossmatch row (as a mapping) with overridable fields."""
    row = {
        "x": 10.5,
        "y": 20.25,
        "ra_deg": 83.633,
        "dec_deg": 22.014,
        "matched": True,
        "source_id": 4062284912345678901,
        "g": 14.5,
        "bp": 15.0,
        "rp": 14.0,
        "parallax": 1.25,
        "parallax_error": 0.03,
        "pmra": 2.5,
        "pmra_error": 0.05,
        "pmdec": -3.5,
        "pmdec_error": 0.04,
        "separation_arcsec": 0.42,
    }
    row.update(overrides)
    return row


def _unmatched_row(**overrides) -> dict:
    row = _row(
        matched=False,
        source_id=None,
        g=None,
        bp=None,
        rp=None,
        parallax=None,
        parallax_error=None,
        pmra=None,
        pmra_error=None,
        pmdec=None,
        pmdec_error=None,
        separation_arcsec=None,
    )
    row.update(overrides)
    return row


# --- round-trip -----------------------------------------------------------------------


def test_save_and_load_round_trip(project):
    sid = start_session(project, clock=frozen(T0))
    stack = _add_stack(project, sid)
    rows = [
        _row(),
        _row(x=50.0, y=60.0, ra_deg=83.64, dec_deg=22.02, source_id=111, g=18.0,
             separation_arcsec=0.2),
        _unmatched_row(x=3.0, y=4.0, ra_deg=83.70, dec_deg=22.05),
    ]
    saved = save_crossmatch(project, stack.id, rows)
    assert saved == tuple(
        CrossmatchRecord(frame_id=stack.id, **{k: v for k, v in r.items()})
        for r in rows
    )
    loaded = load_crossmatch(project, stack.id)
    assert loaded == saved
    first = loaded[0]
    assert first.frame_id == stack.id
    assert first.x == 10.5 and first.y == 20.25
    assert first.ra_deg == 83.633 and first.dec_deg == 22.014
    assert first.matched is True
    assert first.source_id == 4062284912345678901
    assert first.g == 14.5 and first.bp == 15.0 and first.rp == 14.0
    assert first.parallax == 1.25 and first.parallax_error == 0.03
    assert first.pmra == 2.5 and first.pmra_error == 0.05
    assert first.pmdec == -3.5 and first.pmdec_error == 0.04
    assert first.separation_arcsec == 0.42
    unmatched = loaded[2]
    assert unmatched.matched is False
    assert unmatched.source_id is None and unmatched.g is None
    assert unmatched.parallax is None and unmatched.parallax_error is None
    assert unmatched.pmra is None and unmatched.pmra_error is None
    assert unmatched.pmdec is None and unmatched.pmdec_error is None
    assert unmatched.separation_arcsec is None


def test_save_accepts_attribute_rows_and_normalizes_ra(project):
    """Duck-typed rows (objects with attributes, e.g. gaia CrossmatchRow) work;
    ra_deg is normalized into [0, 360)."""

    class Row:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    sid = start_session(project, clock=frozen(T0))
    stack = _add_stack(project, sid)
    rows = [
        Row(x=1.0, y=2.0, ra_deg=370.5, dec_deg=-10.0, matched=False),
        Row(x=3.0, y=4.0, ra_deg=10.0, dec_deg=20.0, matched=True, source_id=7,
            g=12.0, separation_arcsec=0.1),
    ]
    saved = save_crossmatch(project, stack.id, rows)
    assert [r.ra_deg for r in saved] == [10.5, 10.0]
    loaded = load_crossmatch(project, stack.id)
    assert loaded[0].ra_deg == 10.5 and loaded[0].dec_deg == -10.0
    assert loaded[0].matched is False and loaded[0].source_id is None
    assert loaded[1].source_id == 7 and loaded[1].g == 12.0
    assert loaded[1].parallax is None and loaded[1].parallax_error is None


def test_save_replaces_the_previous_table_of_the_frame(project):
    """Upsert semantics: saving again for the same stack replaces its rows."""
    sid = start_session(project, clock=frozen(T0))
    stack = _add_stack(project, sid)
    save_crossmatch(project, stack.id, [_row(), _row(x=1.0), _row(x=2.0)])
    assert len(load_crossmatch(project, stack.id)) == 3
    save_crossmatch(project, stack.id, [_unmatched_row(x=9.0)])
    loaded = load_crossmatch(project, stack.id)
    assert len(loaded) == 1
    assert loaded[0].x == 9.0 and loaded[0].matched is False
    # the raw table has exactly one row for the frame
    count = project.db.execute(
        "SELECT COUNT(*) FROM crossmatch WHERE frame_id = ?", (stack.id,)
    ).fetchone()[0]
    assert count == 1


def test_save_empty_rows_clears_the_table(project):
    sid = start_session(project, clock=frozen(T0))
    stack = _add_stack(project, sid)
    save_crossmatch(project, stack.id, [_row()])
    assert save_crossmatch(project, stack.id, []) == ()
    assert load_crossmatch(project, stack.id) == ()


def test_table_persists_across_close_and_reopen(root):
    """The demoable: run a crossmatch on a stack, close + reopen the project,
    the table is still there and queryable."""
    p = create_project("M 42", root=root)
    sid = start_session(p, clock=frozen(T0))
    stack = _add_stack(p, sid)
    save_crossmatch(p, stack.id, [_row(), _unmatched_row()])
    p.close()

    with open_project(root / "m-42") as q:
        loaded = load_crossmatch(q, stack.id)
        assert len(loaded) == 2
        assert loaded[0].source_id == 4062284912345678901
        assert loaded[1].matched is False
        assert [s.frame_id for s in list_crossmatched_stacks(q)] == [stack.id]


# --- missing stack / unknown frame ------------------------------------------------------


def test_save_on_missing_stack_raises_crossmatch_error(project):
    sid = start_session(project, clock=frozen(T0))
    stack = _add_stack(project, sid)
    with pytest.raises(CrossmatchError, match="no frame 999"):
        save_crossmatch(project, 999, [_row()])
    # nothing was written anywhere
    assert load_crossmatch(project, stack.id) == ()
    total = project.db.execute("SELECT COUNT(*) FROM crossmatch").fetchone()[0]
    assert total == 0


def test_load_on_unknown_frame_returns_empty(project):
    assert load_crossmatch(project, 12345) == ()


def test_frame_id_and_rows_structural_type_errors(project):
    sid = start_session(project, clock=frozen(T0))
    stack = _add_stack(project, sid)
    with pytest.raises(TypeError, match="frame_id must be an int"):
        save_crossmatch(project, "1", [_row()])  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="frame_id must be an int"):
        load_crossmatch(project, 1.5)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="rows must be an iterable"):
        save_crossmatch(project, stack.id, "not-rows")
    with pytest.raises(TypeError, match="matched must be a bool"):
        save_crossmatch(project, stack.id, [_row(matched=1)])
    assert load_crossmatch(project, stack.id) == ()


# --- validation -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "override, match",
    [
        ({"x": float("nan")}, "x must be finite"),
        ({"x": "abc"}, "x must be a number"),
        ({"x": None}, "x must be a number"),
        ({"x": True}, "x must be a number"),
        ({"y": float("inf")}, "y must be finite"),
        ({"ra_deg": float("nan")}, "ra_deg must be finite"),
        ({"dec_deg": 91.0}, "dec_deg must be within"),
        ({"dec_deg": -90.5}, "dec_deg must be within"),
        ({"source_id": 1.5}, "source_id must be an int or None"),
        ({"source_id": True}, "source_id must be an int or None"),
        ({"source_id": 2**63}, "does not fit a SQLite INTEGER"),
        ({"g": float("nan")}, "g must be finite"),
        ({"bp": "x"}, "bp must be a number"),
        ({"parallax": float("inf")}, "parallax must be finite"),
        ({"parallax_error": "x"}, "parallax_error must be a number"),
        ({"pmra": None, "pmra_error": float("nan")}, "pmra_error must be finite"),
        ({"pmdec": [1]}, "pmdec must be a number"),
        ({"separation_arcsec": -0.1}, "separation_arcsec must be >= 0"),
        ({"separation_arcsec": float("nan")}, "separation_arcsec must be finite"),
    ],
)
def test_invalid_row_values_raise_and_write_nothing(project, override, match):
    sid = start_session(project, clock=frozen(T0))
    stack = _add_stack(project, sid)
    with pytest.raises(CrossmatchError, match=match):
        save_crossmatch(project, stack.id, [_row(**override)])
    assert load_crossmatch(project, stack.id) == ()
    total = project.db.execute("SELECT COUNT(*) FROM crossmatch").fetchone()[0]
    assert total == 0


def test_row_missing_a_required_field_raises(project):
    sid = start_session(project, clock=frozen(T0))
    stack = _add_stack(project, sid)
    bad = _row()
    del bad["y"]
    with pytest.raises(CrossmatchError, match="missing the required field 'y'"):
        save_crossmatch(project, stack.id, [bad])
    assert load_crossmatch(project, stack.id) == ()


def test_second_invalid_row_leaves_no_partial_write(project):
    """All rows are validated BEFORE the transaction: a bad row after good
    ones writes nothing."""
    sid = start_session(project, clock=frozen(T0))
    stack = _add_stack(project, sid)
    with pytest.raises(CrossmatchError, match="dec_deg"):
        save_crossmatch(project, stack.id, [_row(), _row(dec_deg=123.0)])
    assert load_crossmatch(project, stack.id) == ()


# --- list_crossmatched_stacks ------------------------------------------------------------


def test_list_crossmatched_stacks(project):
    sid = start_session(project, clock=frozen(T0))
    stack_a = _add_stack(project, sid, 0)
    stack_b = _add_stack(project, sid, 1)
    sub = store_frame(
        project, sid, KIND_SUB,
        _fits_bytes((T0 + timedelta(seconds=5)).isoformat(timespec="milliseconds").replace("+00:00", "")),
    )
    save_crossmatch(project, stack_a.id, [_row(), _unmatched_row()])
    save_crossmatch(project, stack_b.id, [_row(), _row(), _row()])
    summaries = list_crossmatched_stacks(project)
    assert [s.frame_id for s in summaries] == [stack_a.id, stack_b.id]
    a, b = summaries
    assert a.session_id == sid and a.kind == KIND_STACK
    assert a.row_count == 2 and a.matched_count == 1 and a.unmatched_count == 1
    assert b.row_count == 3 and b.matched_count == 3 and b.unmatched_count == 0
    # the sub has no table and is not listed
    assert sub.id not in {s.frame_id for s in summaries}


def test_list_crossmatched_stacks_empty(project):
    assert list_crossmatched_stacks(project) == ()


# --- cascade delete -----------------------------------------------------------------------


def test_crossmatch_rows_cascade_deleted_with_frame(project):
    sid = start_session(project, clock=frozen(T0))
    stack = _add_stack(project, sid)
    save_crossmatch(project, stack.id, [_row(), _unmatched_row()])
    assert len(load_crossmatch(project, stack.id)) == 2
    with project.db:
        project.db.execute("DELETE FROM frames WHERE id = ?", (stack.id,))
    assert load_crossmatch(project, stack.id) == ()
    assert list_crossmatched_stacks(project) == ()
    total = project.db.execute("SELECT COUNT(*) FROM crossmatch").fetchone()[0]
    assert total == 0


# --- migration 5 -> 6 ----------------------------------------------------------------------


def make_v5_project(root: Path) -> tuple[Path, str, int]:
    """A v5 project: the current-build schema MINUS the ``crossmatch`` table,
    ``user_version=5`` and ``schema_version=5`` in project.json, with one
    session + one stack row that must survive the migration."""
    p = create_project("M 42", root=root)
    sid = start_session(p, clock=frozen(T0))
    frame_id = _add_frame_row(p, sid, JD1, UTC1)
    with p.db:
        p.db.execute("DROP TABLE crossmatch")
        p.db.execute("PRAGMA user_version=5")
    p.config["schema_version"] = 5
    p.save_config()
    p.close()
    return root / "m-42", sid, frame_id


def table_names(conn) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_open_migrates_v5_project_creating_crossmatch(root):
    project_dir, _sid, frame_id = make_v5_project(root)
    # sanity: the db really is a v5 db without the crossmatch table
    conn = sqlite3.connect(project_dir / DB_FILE)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
    assert "crossmatch" not in table_names(conn)
    conn.close()

    with open_project(project_dir) as p:
        assert p.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 8
        assert "crossmatch" in table_names(p.db)
        cols = [row[1] for row in p.db.execute("PRAGMA table_info(crossmatch)")]
        assert cols == [
            "id", "frame_id", "x", "y", "ra_deg", "dec_deg", "matched",
            "source_id", "g", "bp", "rp", "parallax", "parallax_error",
            "pmra", "pmra_error", "pmdec", "pmdec_error", "separation_arcsec",
        ]
        # the FK of the new table points at frames with cascade delete
        fk = p.db.execute("PRAGMA foreign_key_list(crossmatch)").fetchall()
        assert [(row[2], row[3], row[4], row[6]) for row in fk] == [
            ("frames", "frame_id", "id", "CASCADE")
        ]
        # the pre-existing rows are untouched
        assert [f.id for f in list_frames(p)] == [frame_id]
        assert p.config["schema_version"] == SCHEMA_VERSION
        # the migrated project is fully usable: a crossmatch can be saved
        save_crossmatch(p, frame_id, [_row(), _unmatched_row()])
        loaded = load_crossmatch(p, frame_id)
        assert len(loaded) == 2 and loaded[0].source_id == 4062284912345678901
        assert [s.frame_id for s in list_crossmatched_stacks(p)] == [frame_id]
    on_disk = json.loads((project_dir / PROJECT_FILE).read_text(encoding="utf-8"))
    assert on_disk["schema_version"] == SCHEMA_VERSION


def test_migrate_db_5_to_6_creates_crossmatch_and_is_idempotent(tmp_path):
    conn = sqlite3.connect(tmp_path / "db.sqlite")
    initialize_db(conn)
    with conn:
        conn.execute("DROP TABLE crossmatch")
        conn.execute("PRAGMA user_version=5")
    assert "crossmatch" not in table_names(conn)
    assert migrate_db(conn) == SCHEMA_VERSION == 8
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 8
    assert "crossmatch" in table_names(conn)
    # re-running the 5 -> 6 step on a db that already has the table is a no-op
    conn.execute("PRAGMA user_version=5")
    assert migrate_db(conn) == SCHEMA_VERSION
    assert "crossmatch" in table_names(conn)
    conn.close()


def test_keyboard_interrupt_mid_5_to_6_rolls_back_and_reopen_recovers(root):
    project_dir, _sid, _frame_id = make_v5_project(root)
    conn = sqlite3.connect(project_dir / DB_FILE)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 5

    class KillSwitch:
        def __init__(self, real):
            self._real = real

        def execute(self, sql, *args):
            if sql.lstrip().upper().startswith("CREATE TABLE IF NOT EXISTS CROSSMATCH"):
                raise KeyboardInterrupt("simulated kill mid-migration")
            return self._real.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self._real, name)

    with pytest.raises(KeyboardInterrupt):
        migrate_db(KillSwitch(conn))  # ty: ignore[invalid-argument-type]
    conn.close()
    # the migration rolled back completely: still v5, no table
    conn = sqlite3.connect(project_dir / DB_FILE)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
    assert "crossmatch" not in table_names(conn)
    conn.close()
    # reopening the project re-runs and completes the migration
    with open_project(project_dir) as p:
        assert p.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert "crossmatch" in table_names(p.db)


# --- public API + module boundary -----------------------------------------------------------


def test_public_api_exports_crossmatch():
    for name in (
        "CrossmatchError",
        "CrossmatchRecord",
        "CrossmatchedStack",
        "save_crossmatch",
        "load_crossmatch",
        "list_crossmatched_stacks",
    ):
        assert name in store.__all__, name
        assert hasattr(store, name), name
    assert issubclass(store.CrossmatchError, store.ProjectError)
    assert store.save_crossmatch is save_crossmatch
    assert store.load_crossmatch is load_crossmatch
    assert store.list_crossmatched_stacks is list_crossmatched_stacks
    assert store.SCHEMA_VERSION == 8
    assert set(store.MIGRATIONS) == {0, 1, 2, 3, 4, 5, 6, 7}
    conn = sqlite3.connect(":memory:")
    store.initialize_db(conn)
    assert "crossmatch" in table_names(conn)
    conn.close()


def test_crossmatch_module_source_scan_no_gi_no_link():
    import re

    path = (
        Path(store.crossmatch.__file__).resolve()
    )
    source = path.read_text(encoding="utf-8")
    assert not re.search(r"^\s*(import gi|from gi)\b", source, re.MULTILINE)
    assert not re.search(
        r"^\s*(import|from)\s+seestar_controller\.link\b", source, re.MULTILINE
    )


def test_store_package_imports_no_gtk_and_no_link():
    code = (
        "import sys;"
        "import seestar_controller.store;"
        "assert 'gi' not in sys.modules, 'GTK imported';"
        "assert not any(m == 'seestar_controller.link' or m.startswith("
        "'seestar_controller.link.') for m in sys.modules), 'link imported';"
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout
