"""hr-diagram ticket 02 — the store side: persisted isochrone params (schema v9).

Covers :mod:`seestar_controller.store.isochrone` (``set_isochrone_params`` /
``isochrone_params``, validation, the singleton upsert, corrupt rows), the
v8 -> v9 migration (``isochrone_params`` table) via ``open_project`` and via
raw :func:`~seestar_controller.store.migrate_db`, the params round-trip
across a project restart, and the store exports. Headless, no network.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from seestar_controller import store
from seestar_controller.store import (
    DB_FILE,
    PROJECT_FILE,
    SCHEMA_VERSION,
    IsochroneParams,
    IsochroneStoreError,
    create_project,
    initialize_db,
    isochrone_params,
    list_sessions,
    migrate_db,
    open_project,
    set_isochrone_params,
    start_session,
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


def table_names(db) -> set[str]:
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


# --- exports + fresh-project schema ------------------------------------------------------------


def test_store_exports_the_isochrone_api():
    for name in ("IsochroneParams", "IsochroneStoreError", "isochrone_params", "set_isochrone_params"):
        assert name in store.__all__
        assert hasattr(store, name)
    assert issubclass(IsochroneStoreError, store.ProjectError)


def test_fresh_project_is_v9_with_the_isochrone_params_table(project):
    assert SCHEMA_VERSION == 9
    assert project.db.execute("PRAGMA user_version").fetchone()[0] == 9
    assert "isochrone_params" in table_names(project.db)
    cols = [row[1] for row in project.db.execute("PRAGMA table_info(isochrone_params)")]
    assert cols == ["id", "age", "feh", "dist_mod", "updated_at"]
    assert project.config["schema_version"] == 9
    # a fresh project has no params yet — a missing row is not an error
    assert isochrone_params(project) is None


# --- set / get round-trip -----------------------------------------------------------------------


def test_set_then_get_round_trip(project):
    saved = set_isochrone_params(project, age=8.0, feh=-0.5, dist_mod=10.0)
    assert isinstance(saved, IsochroneParams)
    assert (saved.age, saved.feh, saved.dist_mod) == (8.0, -0.5, 10.0)
    assert saved.updated_at  # default: a UTC ISO-8601 stamp
    loaded = isochrone_params(project)
    assert loaded == saved
    # explicit updated_at is honored
    stamp = "2026-11-20T22:00:00+00:00"
    saved2 = set_isochrone_params(project, age=9.5, feh=0.0, dist_mod=12.5, updated_at=stamp)
    assert saved2.updated_at == stamp
    assert isochrone_params(project) == saved2


def test_set_is_a_singleton_upsert(project):
    set_isochrone_params(project, age=8.0, feh=0.0, dist_mod=10.0, updated_at="t1")
    set_isochrone_params(project, age=9.0, feh=-1.0, dist_mod=11.0, updated_at="t2")
    rows = project.db.execute("SELECT id, age, feh, dist_mod, updated_at FROM isochrone_params").fetchall()
    assert rows == [(1, 9.0, -1.0, 11.0, "t2")]  # exactly one row, replaced
    assert isochrone_params(project) == IsochroneParams(9.0, -1.0, 11.0, "t2")


def test_params_survive_a_project_restart(root):
    """The issue's demoable criterion: set parameters, reload, still there."""
    p = create_project("M 42", root=root)
    project_dir = root / "m-42"
    set_isochrone_params(p, age=8.5, feh=-0.5, dist_mod=9.75)
    p.close()
    with open_project(project_dir) as p2:
        loaded = isochrone_params(p2)
        assert loaded is not None
        assert (loaded.age, loaded.feh, loaded.dist_mod) == (8.5, -0.5, 9.75)


def test_boundary_values_are_accepted(project):
    saved = set_isochrone_params(project, age=6.0, feh=-4.0, dist_mod=-5.0)
    assert (saved.age, saved.feh, saved.dist_mod) == (6.0, -4.0, -5.0)
    saved = set_isochrone_params(project, age=10.5, feh=0.5, dist_mod=30.0)
    assert (saved.age, saved.feh, saved.dist_mod) == (10.5, 0.5, 30.0)


# --- validation ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"age": 5.9}, {"age": 10.6}, {"age": float("nan")}, {"age": float("inf")},
        {"feh": -4.1}, {"feh": 0.6}, {"feh": float("nan")},
        {"dist_mod": -5.1}, {"dist_mod": 30.1}, {"dist_mod": float("-inf")},
    ],
)
def test_set_rejects_out_of_range_or_non_finite_values(project, kwargs):
    good = {"age": 8.0, "feh": 0.0, "dist_mod": 10.0}
    good.update(kwargs)
    with pytest.raises(IsochroneStoreError) as excinfo:
        set_isochrone_params(project, **good)  # ty: ignore[invalid-argument-type]
    assert next(iter(kwargs)) in str(excinfo.value)
    assert isochrone_params(project) is None  # nothing was written


@pytest.mark.parametrize(
    "kwargs",
    [
        {"age": "8.0"}, {"age": True}, {"age": None},
        {"feh": "0.0"}, {"feh": False}, {"dist_mod": "10"},
    ],
)
def test_set_rejects_non_numbers_with_type_error(project, kwargs):
    good = {"age": 8.0, "feh": 0.0, "dist_mod": 10.0}
    good.update(kwargs)
    with pytest.raises(TypeError):
        set_isochrone_params(project, **good)  # ty: ignore[invalid-argument-type]
    assert isochrone_params(project) is None


def test_set_rejects_a_bad_updated_at_and_writes_nothing(project):
    for bad in ("", 42, None.__class__):  # empty string / non-string
        with pytest.raises(IsochroneStoreError):
            set_isochrone_params(project, age=8.0, feh=0.0, dist_mod=10.0, updated_at=bad)  # ty: ignore[invalid-argument-type]
    assert isochrone_params(project) is None


def test_a_rejected_save_leaves_the_previous_row_intact(project):
    set_isochrone_params(project, age=8.0, feh=0.0, dist_mod=10.0, updated_at="keep")
    with pytest.raises(IsochroneStoreError):
        set_isochrone_params(project, age=99.0, feh=0.0, dist_mod=10.0)
    assert isochrone_params(project) == IsochroneParams(8.0, 0.0, 10.0, "keep")


def test_isochrone_params_raises_on_a_corrupt_row(project):
    # REAL affinity keeps non-numeric TEXT as TEXT -> float() fails on read
    project.db.execute(
        "INSERT INTO isochrone_params (id, age, feh, dist_mod, updated_at) "
        "VALUES (1, 'abc', 0.0, 10.0, 't')"
    )
    with pytest.raises(IsochroneStoreError):
        isochrone_params(project)


def test_the_singleton_check_rejects_a_second_row(project):
    set_isochrone_params(project, age=8.0, feh=0.0, dist_mod=10.0)
    with pytest.raises(sqlite3.IntegrityError):
        project.db.execute(
            "INSERT INTO isochrone_params (id, age, feh, dist_mod, updated_at) "
            "VALUES (2, 8.0, 0.0, 10.0, 't')"
        )


# --- migration 8 -> 9 ---------------------------------------------------------------------------


def make_v8_project(root: Path) -> tuple[Path, str]:
    """A v8 project: the current-build schema MINUS the ``isochrone_params``
    table, ``user_version=8`` and ``schema_version=8`` in project.json, with
    one session row that must survive the migration."""
    p = create_project("M 42", root=root)
    sid = start_session(p, clock=frozen(T0))
    with p.db:
        p.db.execute("DROP TABLE isochrone_params")
        p.db.execute("PRAGMA user_version=8")
    p.config["schema_version"] = 8
    p.save_config()
    p.close()
    return root / "m-42", sid


def test_open_migrates_v8_project_creating_isochrone_params(root):
    project_dir, sid = make_v8_project(root)
    # sanity: the db really is a v8 db without the isochrone_params table
    conn = sqlite3.connect(project_dir / DB_FILE)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 8
    assert "isochrone_params" not in table_names(conn)
    conn.close()

    with open_project(project_dir) as p:
        assert p.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 9
        assert "isochrone_params" in table_names(p.db)
        cols = [row[1] for row in p.db.execute("PRAGMA table_info(isochrone_params)")]
        assert cols == ["id", "age", "feh", "dist_mod", "updated_at"]
        # the pre-existing rows are untouched
        assert [s.id for s in list_sessions(p)] == [sid]
        assert p.config["schema_version"] == SCHEMA_VERSION
        # no params yet, and the migrated project is fully usable
        assert isochrone_params(p) is None
        saved = set_isochrone_params(p, age=8.0, feh=-0.5, dist_mod=10.0)
        assert isochrone_params(p) == saved
    on_disk = json.loads((project_dir / PROJECT_FILE).read_text(encoding="utf-8"))
    assert on_disk["schema_version"] == SCHEMA_VERSION


def test_migrate_db_8_to_9_creates_isochrone_params_and_is_idempotent(tmp_path):
    conn = sqlite3.connect(tmp_path / "db.sqlite")
    initialize_db(conn)
    with conn:
        conn.execute("DROP TABLE isochrone_params")
        conn.execute("PRAGMA user_version=8")
    assert "isochrone_params" not in table_names(conn)
    assert migrate_db(conn) == SCHEMA_VERSION == 9
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 9
    assert "isochrone_params" in table_names(conn)
    # re-running the 8 -> 9 step on a db that already has the table is a no-op
    conn.execute("PRAGMA user_version=8")
    assert migrate_db(conn) == SCHEMA_VERSION
    assert "isochrone_params" in table_names(conn)
    conn.close()
