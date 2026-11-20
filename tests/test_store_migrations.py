"""Project store: crash safety + schema migrations — tmp_path, no network.

Every test runs against a ``tmp_path`` project root. Covers the migration
runner (``migrate_db`` / ``MIGRATIONS``): v0 empty db -> current version, dev
-build v1 ``stars`` table (no aperture columns) -> v2 with rows preserved,
newer ``user_version`` rejected, idempotent re-opens, and simulated kills
(KeyboardInterrupt) mid-transaction / mid-migration / stray ``*.tmp`` files
leaving the project consistent on reopen.
"""

import json
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from seestar_controller.store import (
    DB_FILE,
    PROJECT_FILE,
    ROLE_COMP,
    ROLE_TARGET,
    SCHEMA_TABLES,
    SCHEMA_VERSION,
    STACKS_DIR,
    SUBS_DIR,
    InvalidProjectError,
    StarPick,
    add_measurement,
    create_project,
    get_open_session,
    initialize_db,
    lightcurve,
    list_frames,
    list_projects,
    load_star_picks,
    migrate_db,
    open_project,
    read_frame_bytes,
    save_star_picks,
    schema,
    start_session,
)


@pytest.fixture()
def root(tmp_path):
    """A fresh project root."""
    return tmp_path / "projects"


def frozen(dt: datetime):
    """A frozen clock returning ``dt`` on every call."""
    return lambda: dt


T0 = datetime(2026, 11, 20, 22, 0, 0, tzinfo=UTC)
JD1 = 2461365.416668
UTC1 = "2026-11-20T22:00:00.000+00:00"


def make_pick(
    role=ROLE_TARGET,
    label="TIC 123",
    x=100.0,
    y=200.0,
    ra_deg=83.82208,
    dec_deg=-5.39114,
    aperture_radius=4.0,
    sky_annulus_inner=8.0,
    sky_annulus_outer=12.0,
):
    """A valid StarPick with overridable fields."""
    return StarPick(
        role=role,
        label=label,
        x=x,
        y=y,
        ra_deg=ra_deg,
        dec_deg=dec_deg,
        aperture_radius=aperture_radius,
        sky_annulus_inner=sky_annulus_inner,
        sky_annulus_outer=sky_annulus_outer,
    )


TARGET = make_pick()
COMP_A = make_pick(role=ROLE_COMP, label="comp A", x=301.0, y=401.0)


def add_frame_row(project, session_id, jd, utc, kind="sub") -> int:
    """Insert a ``frames`` row directly (no FITS file needed) and return its id."""
    with project.db:
        cur = project.db.execute(
            "INSERT INTO frames (session_id, kind, path, jd, utc) VALUES (?, ?, ?, ?, ?)",
            (session_id, kind, f"subs/{session_id}/{jd:.6f}.fits", jd, utc),
        )
        return int(cur.lastrowid)


class KillSwitch:
    """A proxy around a sqlite3 connection that raises :class:`KeyboardInterrupt`
    when ``should_die(sql)`` matches an ``execute()`` call — simulating a process
    kill at that exact statement. ``with`` blocks delegate to the real
    connection, so the transaction semantics (rollback on exception) are
    preserved. Every executed statement is recorded in ``calls``."""

    def __init__(self, conn, should_die):
        self._conn = conn
        self._should_die = should_die
        self.calls: list[str] = []

    def execute(self, sql, *args):
        self.calls.append(sql)
        if self._should_die(sql):
            raise KeyboardInterrupt("simulated kill mid-write")
        return self._conn.execute(sql, *args)

    def __enter__(self):
        return self._conn.__enter__()

    def __exit__(self, *exc):
        return self._conn.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def dies_on(prefix: str):
    """A ``should_die`` predicate matching statements starting with ``prefix``."""
    return lambda sql: sql.lstrip().upper().startswith(prefix)


#: The OLD (dev-build v1) ``stars`` table: no aperture columns, nullable x/y.
OLD_STARS_DDL = """
CREATE TABLE stars (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL CHECK (role IN ('target', 'comp')),
    label TEXT,
    x REAL,
    y REAL,
    ra_deg REAL,
    dec_deg REAL
)
"""


def make_v0_project(root: Path) -> Path:
    """A valid project layout whose db is an empty v0 sqlite file
    (``user_version=0``, no tables)."""
    project_dir = root / "m-42"
    project_dir.mkdir(parents=True)
    (project_dir / SUBS_DIR).mkdir()
    (project_dir / STACKS_DIR).mkdir()
    config = {
        "schema_version": 1,
        "name": "M 42",
        "slug": "m-42",
        "created_at": "2026-11-20T00:00:00.000+00:00",
        "target": None,
        "star_picks": None,
        "period": None,
    }
    (project_dir / PROJECT_FILE).write_text(
        json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    conn = sqlite3.connect(project_dir / DB_FILE)
    conn.execute("PRAGMA user_version=0")  # explicit: empty db, no tables
    conn.close()
    return project_dir


def make_v1_project(root: Path) -> Path:
    """A dev-build v1 project: all tables, but the OLD ``stars`` table (no
    aperture columns) with one existing row, ``user_version=1`` and
    ``schema_version=1`` in project.json."""
    p = create_project("M 42", root=root)
    with p.db:
        p.db.execute("DROP TABLE stars")
        p.db.execute(OLD_STARS_DDL)
        p.db.execute("CREATE INDEX IF NOT EXISTS idx_stars_role ON stars (role)")
        p.db.execute(
            "INSERT INTO stars (role, label, x, y, ra_deg, dec_deg) "
            "VALUES ('target', 'TIC 123', 100.0, 200.0, 83.82208, -5.39114)"
        )
        p.db.execute("PRAGMA user_version=1")
    p.config["schema_version"] = 1
    p.save_config()
    p.close()
    return root / "m-42"


def table_names(db) -> set[str]:
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


# --- (a) migration from version 0 (empty db) to the current version ------------


def test_open_migrates_v0_empty_db_to_current_version(root):
    project_dir = make_v0_project(root)
    # sanity: the db really is an empty v0 db before the open
    conn = sqlite3.connect(project_dir / DB_FILE)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
    conn.close()

    with open_project(project_dir) as p:
        assert p.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert set(SCHEMA_TABLES) <= table_names(p.db)
        assert p.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert p.config["schema_version"] == SCHEMA_VERSION
        # the migrated project is fully usable
        sid = start_session(p, clock=frozen(T0))
        assert get_open_session(p).id == sid  # ty: ignore[missing-union-attribute]
    # the version bump is mirrored into project.json on disk
    on_disk = json.loads((project_dir / PROJECT_FILE).read_text(encoding="utf-8"))
    assert on_disk["schema_version"] == SCHEMA_VERSION


def test_migrate_db_on_fresh_connection_creates_schema(tmp_path):
    conn = sqlite3.connect(tmp_path / "db.sqlite")
    assert migrate_db(conn) == SCHEMA_VERSION
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert set(SCHEMA_TABLES) <= table_names(conn)
    conn.close()


def test_initialize_db_creates_current_schema_directly(tmp_path):
    conn = sqlite3.connect(tmp_path / "db.sqlite")
    initialize_db(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert set(SCHEMA_TABLES) <= table_names(conn)
    cols = [row[1] for row in conn.execute("PRAGMA table_info(stars)")]
    assert cols[-3:] == ["aperture_radius", "sky_annulus_inner", "sky_annulus_outer"]
    conn.close()


# --- (b) migration 1 -> 2: dev-build v1 stars table without aperture columns ----


def test_open_migrates_v1_dev_build_project_preserving_star_rows(root):
    project_dir = make_v1_project(root)
    with open_project(project_dir) as p:
        assert p.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        cols = [row[1] for row in p.db.execute("PRAGMA table_info(stars)")]
        assert cols[-3:] == ["aperture_radius", "sky_annulus_inner", "sky_annulus_outer"]
        # the existing row is preserved and backfilled with the default aperture
        picks = load_star_picks(p)
        assert len(picks) == 1
        assert picks[0].role == ROLE_TARGET and picks[0].label == "TIC 123"
        assert picks[0].x == 100.0 and picks[0].y == 200.0
        assert (
            picks[0].aperture_radius,
            picks[0].sky_annulus_inner,
            picks[0].sky_annulus_outer,
        ) == (4.0, 8.0, 12.0)
        assert p.config["schema_version"] == SCHEMA_VERSION
    on_disk = json.loads((project_dir / PROJECT_FILE).read_text(encoding="utf-8"))
    assert on_disk["schema_version"] == SCHEMA_VERSION


def test_migrate_db_1_to_2_adds_aperture_columns_and_backfills(tmp_path):
    conn = sqlite3.connect(tmp_path / "db.sqlite")
    with conn:
        conn.execute(OLD_STARS_DDL)
        conn.execute("INSERT INTO stars (role, label, x, y) VALUES ('comp', 'A', 1.0, 2.0)")
        conn.execute("PRAGMA user_version=1")
    assert migrate_db(conn) == SCHEMA_VERSION == 4
    cols = [row[1] for row in conn.execute("PRAGMA table_info(stars)")]
    assert cols == [
        "id", "role", "label", "x", "y", "ra_deg", "dec_deg",
        "aperture_radius", "sky_annulus_inner", "sky_annulus_outer",
    ]
    row = conn.execute(
        "SELECT role, aperture_radius, sky_annulus_inner, sky_annulus_outer FROM stars"
    ).fetchone()
    assert row == ("comp", 4.0, 8.0, 12.0)  # NOT NULL DEFAULT backfill
    conn.close()


def test_migrate_db_1_to_2_is_noop_when_columns_already_present(tmp_path):
    """A v1 db created by the current build already has the aperture columns;
    the 1 -> 2 migration must not fail on the duplicate ALTER."""
    conn = sqlite3.connect(tmp_path / "db.sqlite")
    initialize_db(conn)
    conn.execute("PRAGMA user_version=1")  # pretend it was stamped v1
    with conn:
        conn.execute(
            "INSERT INTO stars (role, label, x, y, aperture_radius, sky_annulus_inner, "
            "sky_annulus_outer) VALUES ('target', 'T', 1.0, 2.0, 5.0, 9.0, 13.0)"
        )
    assert migrate_db(conn) == SCHEMA_VERSION
    row = conn.execute(
        "SELECT aperture_radius, sky_annulus_inner, sky_annulus_outer FROM stars"
    ).fetchone()
    assert row == (5.0, 9.0, 13.0)  # untouched — no default backfill
    conn.close()


# --- (b2) migration 3 -> 4: frame_quality table (frame-quality ticket 01) ----------


def make_v3_project(root: Path) -> tuple[Path, str, int]:
    """A v3 project: the current-build schema MINUS the ``frame_quality``
    table, ``user_version=3`` and ``schema_version=3`` in project.json, with
    one session + one frame row that must survive the migration."""
    p = create_project("M 42", root=root)
    sid = start_session(p, clock=frozen(T0))
    frame_id = add_frame_row(p, sid, JD1, UTC1)
    with p.db:
        p.db.execute("DROP TABLE frame_quality")
        p.db.execute("PRAGMA user_version=3")
    p.config["schema_version"] = 3
    p.save_config()
    p.close()
    return root / "m-42", sid, frame_id


def test_open_migrates_v3_project_creating_frame_quality(root):
    project_dir, sid, frame_id = make_v3_project(root)
    # sanity: the db really is a v3 db without the frame_quality table
    conn = sqlite3.connect(project_dir / DB_FILE)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    assert "frame_quality" not in table_names(conn)
    conn.close()

    with open_project(project_dir) as p:
        assert p.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert "frame_quality" in table_names(p.db)
        cols = [row[1] for row in p.db.execute("PRAGMA table_info(frame_quality)")]
        assert cols == [
            "frame_id", "fwhm", "bg_median", "bg_std", "star_count",
            "flagged", "flag_reasons", "scored_at",
        ]
        # the pre-existing rows are untouched
        assert [f.id for f in list_frames(p)] == [frame_id]
        assert get_open_session(p).id == sid  # ty: ignore[missing-union-attribute]
        assert p.config["schema_version"] == SCHEMA_VERSION
        # the migrated project is fully usable: quality records can be saved
        from seestar_controller.store import save_frame_quality, session_quality

        save_frame_quality(
            p, frame_id, fwhm=3.5, bg_median=100.0, bg_std=5.0, star_count=10,
            flagged=True, flag_reasons=("fwhm",),
        )
        q = session_quality(p, sid)
        assert q.frame_count == 1 and q.flagged_count == 1
        assert q.frames[0].flag_reasons == ("fwhm",)
    on_disk = json.loads((project_dir / PROJECT_FILE).read_text(encoding="utf-8"))
    assert on_disk["schema_version"] == SCHEMA_VERSION


def test_migrate_db_3_to_4_creates_frame_quality_and_is_idempotent(tmp_path):
    conn = sqlite3.connect(tmp_path / "db.sqlite")
    initialize_db(conn)
    with conn:
        conn.execute("DROP TABLE frame_quality")
        conn.execute("PRAGMA user_version=3")
    assert "frame_quality" not in table_names(conn)
    assert migrate_db(conn) == SCHEMA_VERSION == 4
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
    assert "frame_quality" in table_names(conn)
    # re-running the 3 -> 4 step on a db that already has the table is a no-op
    conn.execute("PRAGMA user_version=3")
    assert migrate_db(conn) == SCHEMA_VERSION
    assert "frame_quality" in table_names(conn)
    conn.close()


def test_keyboard_interrupt_mid_3_to_4_rolls_back_and_reopen_recovers(root):
    project_dir, _sid, _frame_id = make_v3_project(root)
    conn = sqlite3.connect(project_dir / DB_FILE)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    spy = KillSwitch(conn, dies_on("CREATE TABLE IF NOT EXISTS FRAME_QUALITY"))
    with pytest.raises(KeyboardInterrupt):
        migrate_db(spy)  # ty: ignore[invalid-argument-type]
    conn.close()
    # the migration rolled back completely: still v3, no table
    conn = sqlite3.connect(project_dir / DB_FILE)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    assert "frame_quality" not in table_names(conn)
    conn.close()
    # reopening the project re-runs and completes the migration
    with open_project(project_dir) as p:
        assert p.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert "frame_quality" in table_names(p.db)


# --- (c) user_version / schema_version newer than this build ---------------------


def test_open_db_user_version_newer_than_build_raises_invalid(root):
    p = create_project("M 42", root=root)
    with p.db:
        p.db.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    p.close()
    with pytest.raises(InvalidProjectError, match="user_version"):
        open_project(root / "m-42")


def test_open_config_schema_version_newer_than_build_raises_invalid(root):
    p = create_project("M 42", root=root)
    p.config["schema_version"] = SCHEMA_VERSION + 1
    p.save_config()
    p.close()
    with pytest.raises(InvalidProjectError, match="schema_version"):
        open_project(root / "m-42")


def test_migrate_db_rejects_newer_version(tmp_path):
    conn = sqlite3.connect(tmp_path / "db.sqlite")
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    with pytest.raises(InvalidProjectError, match="user_version"):
        migrate_db(conn)
    conn.close()


def test_migrate_db_rejects_negative_version(tmp_path):
    conn = sqlite3.connect(tmp_path / "db.sqlite")
    conn.execute("PRAGMA user_version=-1")
    with pytest.raises(InvalidProjectError, match="user_version"):
        migrate_db(conn)
    conn.close()


def test_migrate_db_requires_no_open_transaction(tmp_path):
    conn = sqlite3.connect(tmp_path / "db.sqlite")
    conn.execute("BEGIN")
    with pytest.raises(InvalidProjectError, match="open transaction"):
        migrate_db(conn)
    conn.execute("ROLLBACK")
    conn.close()


# --- (d) simulated kill mid-write: db stays consistent on reopen ------------------


def test_keyboard_interrupt_mid_add_measurement_leaves_db_consistent(root, monkeypatch):
    p = create_project("M 42", root=root)
    session = start_session(p, clock=frozen(T0))
    frame_id = add_frame_row(p, session, JD1, UTC1)
    first = add_measurement(p, frame_id, 12.34, mag_err=0.01)
    p.close()

    p = open_project(root / "m-42")
    # kill inside the `with project.db:` block, on the INSERT
    monkeypatch.setattr(p, "db", KillSwitch(p.db, dies_on("INSERT INTO MEASUREMENTS")))
    with pytest.raises(KeyboardInterrupt):
        add_measurement(p, frame_id, 99.99)
    p.close()

    # reopen: WAL recovery, no partial rows, old data intact
    with open_project(root / "m-42") as q:
        assert [m.id for m in lightcurve(q)] == [first.id]
        assert [m.mag for m in lightcurve(q)] == [12.34]
        assert q.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert q.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert q.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert list_frames(q)[0].id == frame_id


def test_keyboard_interrupt_mid_save_star_picks_leaves_old_picks(root, monkeypatch):
    p = create_project("M 42", root=root)
    old_picks = save_star_picks(p, [TARGET, COMP_A])
    p.close()

    p = open_project(root / "m-42")
    # kill inside the `with project.db:` block, on the first INSERT after the DELETE
    monkeypatch.setattr(p, "db", KillSwitch(p.db, dies_on("INSERT INTO STARS")))
    with pytest.raises(KeyboardInterrupt):
        save_star_picks(p, [make_pick(label="NEW")])
    p.close()

    # reopen: the DELETE + INSERT rolled back, the old picks and the mirror survive
    with open_project(root / "m-42") as q:
        assert load_star_picks(q) == old_picks
        assert q.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        on_disk = json.loads(q.config_path.read_text(encoding="utf-8"))
        assert on_disk["star_picks"]["target"]["label"] == "TIC 123"
        assert [c["label"] for c in on_disk["star_picks"]["comps"]] == ["comp A"]


def test_keyboard_interrupt_mid_migration_rolls_back_and_reopen_recovers(root):
    project_dir = make_v0_project(root)
    conn = sqlite3.connect(project_dir / DB_FILE)
    # kill during the 0 -> 1 migration, on the first CREATE TABLE
    with pytest.raises(KeyboardInterrupt):
        migrate_db(KillSwitch(conn, dies_on("CREATE TABLE")))  # ty: ignore[invalid-argument-type]
    # the migration rolled back completely: still v0, no tables (DDL included)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
    conn.close()

    # reopening the project re-runs the migration and succeeds
    with open_project(project_dir) as p:
        assert p.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert set(SCHEMA_TABLES) <= table_names(p.db)
        assert p.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_keyboard_interrupt_mid_alter_rolls_back_then_completes(root):
    project_dir = make_v1_project(root)
    conn = sqlite3.connect(project_dir / DB_FILE)
    # kill during the 1 -> 2 migration, on the first ALTER TABLE
    with pytest.raises(KeyboardInterrupt):
        migrate_db(KillSwitch(conn, dies_on("ALTER TABLE")))  # ty: ignore[invalid-argument-type]
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    cols = {row[1] for row in conn.execute("PRAGMA table_info(stars)")}
    assert "aperture_radius" not in cols
    assert conn.execute("SELECT COUNT(*) FROM stars").fetchone()[0] == 1  # row untouched

    # re-running the migration on a clean connection completes it
    assert migrate_db(conn) == SCHEMA_VERSION
    cols = {row[1] for row in conn.execute("PRAGMA table_info(stars)")}
    assert {"aperture_radius", "sky_annulus_inner", "sky_annulus_outer"} <= cols
    row = conn.execute(
        "SELECT aperture_radius, sky_annulus_inner, sky_annulus_outer FROM stars"
    ).fetchone()
    assert row == (4.0, 8.0, 12.0)
    conn.close()


# --- (e) stray .tmp files from a crash are ignored ---------------------------------


def test_stray_tmp_files_are_ignored_on_open_and_list(root):
    p = create_project("M 42", root=root)
    session = start_session(p, clock=frozen(T0))
    frame_id = add_frame_row(p, session, JD1, UTC1)
    frame_path = p.path / SUBS_DIR / session / f"{JD1:.6f}.fits"
    frame_path.parent.mkdir(parents=True, exist_ok=True)
    frame_path.write_bytes(b"fake fits bytes")
    # simulate a crash mid-write: stray temp files next to project.json,
    # the CSV export, and a frame
    (p.path / "project.json.abc123.tmp").write_text("partial", encoding="utf-8")
    (p.path / "lightcurve.csv.def456.tmp").write_text("partial", encoding="utf-8")
    (frame_path.parent / ".frame-xyz789.tmp").write_bytes(b"partial")
    p.close()

    with open_project(root / "m-42") as q:
        assert q.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        frames = list_frames(q)
        assert [f.id for f in frames] == [frame_id]  # only the real frame
        assert read_frame_bytes(q, frames[0]) == b"fake fits bytes"
        # stray temps are ignored, not deleted (no locking — spec out of scope)
        assert (q.path / "project.json.abc123.tmp").is_file()
        assert (q.path / "lightcurve.csv.def456.tmp").is_file()
        assert (frame_path.parent / ".frame-xyz789.tmp").is_file()
    assert [s.slug for s in list_projects(root)] == ["m-42"]


# --- (f) idempotence: opening twice does not re-migrate ----------------------------


def test_migrate_db_is_noop_on_current_version(root):
    p = create_project("M 42", root=root)
    with p.db:
        p.db.execute(
            "INSERT INTO sessions (id, started_at) VALUES ('s1', '2026-11-20T22:00:00+00:00')"
        )
    p.close()
    conn = sqlite3.connect(root / "m-42" / DB_FILE)
    spy = KillSwitch(conn, lambda sql: False)  # counts, never dies
    assert migrate_db(spy) == SCHEMA_VERSION  # ty: ignore[invalid-argument-type]
    assert spy.calls == ["PRAGMA user_version"]  # only the version read, no steps
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    conn.close()


def test_open_twice_does_not_re_migrate(root, monkeypatch):
    project_dir = make_v0_project(root)
    seen: list[int] = []
    real_migrate = schema.migrate_db

    def spy(conn):
        seen.append(conn.execute("PRAGMA user_version").fetchone()[0])
        return real_migrate(conn)

    monkeypatch.setattr(schema, "migrate_db", spy)
    with open_project(project_dir) as p:
        assert p.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    with open_project(project_dir) as p:
        assert p.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    # the first open migrated from 0, the second found the db already current
    assert seen == [0, SCHEMA_VERSION]
    on_disk = json.loads((project_dir / PROJECT_FILE).read_text(encoding="utf-8"))
    assert on_disk["schema_version"] == SCHEMA_VERSION


def test_create_then_open_twice_never_migrates(root, monkeypatch):
    create_project("M 42", root=root).close()
    seen: list[int] = []
    real_migrate = schema.migrate_db

    def spy(conn):
        seen.append(conn.execute("PRAGMA user_version").fetchone()[0])
        return real_migrate(conn)

    monkeypatch.setattr(schema, "migrate_db", spy)
    with open_project(root / "m-42"):
        pass
    with open_project(root / "m-42"):
        pass
    assert seen == [SCHEMA_VERSION, SCHEMA_VERSION]  # fresh db is already current


def test_open_bumps_config_mirror_when_db_already_migrated(root):
    """Simulates a crash between the db migration and the project.json write:
    db at SCHEMA_VERSION, config still at 1 -> open bumps the mirror only."""
    p = create_project("M 42", root=root)
    p.config["schema_version"] = 1
    p.save_config()
    p.close()
    with open_project(root / "m-42") as q:
        assert q.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert q.config["schema_version"] == SCHEMA_VERSION
    on_disk = json.loads((root / "m-42" / PROJECT_FILE).read_text(encoding="utf-8"))
    assert on_disk["schema_version"] == SCHEMA_VERSION


# --- public API + module boundary ----------------------------------------------------


def test_public_api_exports_migration_runner():
    from seestar_controller import store

    assert callable(store.migrate_db)
    assert isinstance(store.MIGRATIONS, dict)
    assert set(store.MIGRATIONS) == {0, 1, 2, 3}
    assert store.SCHEMA_VERSION == 4
    assert "migrate_db" in store.__all__
    assert "MIGRATIONS" in store.__all__
    assert issubclass(store.InvalidProjectError, store.ProjectError)


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
