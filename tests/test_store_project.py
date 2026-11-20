"""Project store: create / open / list + db schema — all on tmp_path, no network.

Every test runs against a ``tmp_path`` project root; the module-boundary test
runs a subprocess asserting the store package imports neither GTK nor
``seestar_controller.link``.
"""

import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from seestar_controller.store import (
    DB_FILE,
    DEFAULT_PROJECT_ROOT,
    MAX_SLUG_LENGTH,
    PROJECT_FILE,
    PROJECT_ROOT_ENV_VAR,
    SCHEMA_TABLES,
    SCHEMA_VERSION,
    STACKS_DIR,
    SUBS_DIR,
    InvalidProjectError,
    Project,
    ProjectError,
    ProjectExistsError,
    ProjectNotFoundError,
    ProjectSummary,
    create_project,
    default_project_root,
    list_projects,
    open_project,
    slugify,
)


@pytest.fixture()
def root(tmp_path):
    """A fresh project root."""
    return tmp_path / "projects"


@pytest.fixture()
def project(root):
    """One created project, closed after the test."""
    p = create_project("M 42", root=root)
    yield p
    p.close()


# --- create: layout + project.json ---------------------------------------------


def test_create_makes_top_level_layout(root):
    p = create_project("M 42", root=root)
    p.close()
    project_dir = root / "m-42"
    assert project_dir.is_dir()
    assert (project_dir / PROJECT_FILE).is_file()
    assert (project_dir / DB_FILE).is_file()
    assert (project_dir / SUBS_DIR).is_dir()
    assert (project_dir / STACKS_DIR).is_dir()
    # session subdirs are lazy (later tickets) — top dirs start empty
    assert list((project_dir / SUBS_DIR).iterdir()) == []
    assert list((project_dir / STACKS_DIR).iterdir()) == []


def test_create_returns_open_project(root):
    p = create_project("M 42", root=root)
    try:
        assert isinstance(p, Project)
        assert p.path == root / "m-42"
        assert p.name == "M 42"
        assert p.slug == "m-42"
        assert p.config_path == p.path / PROJECT_FILE
        assert p.db_path == p.path / DB_FILE
        assert isinstance(p.config, dict)
        assert isinstance(p.db, sqlite3.Connection)
        assert repr(p) == f"Project(name='M 42', path={str(p.path)!r})"
    finally:
        p.close()


def test_project_json_contents_and_schema_version(project):
    config = json.loads(project.config_path.read_text(encoding="utf-8"))
    assert config["schema_version"] == SCHEMA_VERSION == 3
    assert config["name"] == "M 42"
    assert config["slug"] == "m-42"
    # placeholders per spec: target / star picks / period
    assert config["target"] is None
    assert config["star_picks"] is None
    assert config["period"] is None
    # the in-memory config is the same dict that was written
    assert project.config == config


def test_created_at_is_utc_iso(project):
    created_at = project.config["created_at"]
    parsed = datetime.fromisoformat(created_at)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == UTC.utcoffset(None)


def test_create_strips_name_and_creates_missing_root(tmp_path):
    root = tmp_path / "deep" / "nested" / "projects"
    p = create_project("  V* bet Per  ", root=root)
    p.close()
    assert p.name == "V* bet Per"
    assert p.slug == "v-bet-per"
    assert p.path == root / "v-bet-per"


@pytest.mark.parametrize("bad", ["", "   ", "\t\n", None, 42, ["M 42"]])
def test_create_rejects_unusable_names(root, bad):
    with pytest.raises(ValueError):
        create_project(bad, root=root)
    assert not root.exists() or not list(root.iterdir())


# --- create -> reopen -> list round-trip ----------------------------------------


def test_create_reopen_roundtrip(root):
    created = create_project("M 42", root=root)
    created_config = dict(created.config)
    created.close()

    reopened = open_project(root / "m-42")
    try:
        assert reopened.name == "M 42"
        assert reopened.slug == "m-42"
        assert reopened.config == created_config
        assert reopened.path == root / "m-42"
        # the reopened db is usable and has the schema
        tables = {
            row[0]
            for row in reopened.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert set(SCHEMA_TABLES) <= tables
    finally:
        reopened.close()


def test_open_project_by_path_object_and_context_manager(root):
    create_project("M 42", root=root).close()
    with open_project(Path(root) / "m-42") as p:
        assert p.name == "M 42"
    with pytest.raises(sqlite3.ProgrammingError):
        p.db.execute("SELECT 1")  # closed connection


def test_list_projects_empty_and_missing_root(tmp_path):
    assert list_projects(tmp_path / "does-not-exist") == []
    empty = tmp_path / "empty"
    empty.mkdir()
    assert list_projects(empty) == []


def test_list_projects_multiple_sorted_by_created_at(root):
    stamps = {
        "M 42": "2026-01-01T00:00:00.000+00:00",
        "V* bet Per": "2026-02-02T00:00:00.000+00:00",
        "NGC 1976": "2026-03-03T00:00:00.000+00:00",
    }
    for name, stamp in stamps.items():
        p = create_project(name, root=root)
        p.config["created_at"] = stamp
        p.save_config()
        p.close()

    summaries = list_projects(root)
    assert [s.name for s in summaries] == ["M 42", "V* bet Per", "NGC 1976"]
    assert all(isinstance(s, ProjectSummary) for s in summaries)
    assert summaries[0].slug == "m-42"
    assert summaries[0].path == root / "m-42"
    assert summaries[0].created_at == "2026-01-01T00:00:00.000+00:00"
    # reopening from a summary works
    with open_project(summaries[1].path) as p:
        assert p.name == "V* bet Per"


def test_list_projects_skips_non_project_entries(root):
    root.mkdir()
    (root / "stray-file.txt").write_text("hello")
    (root / "stray-dir").mkdir()  # no project.json
    (root / "broken").mkdir()
    (root / "broken" / PROJECT_FILE).write_text("{not json")
    (root / "wrong-shape").mkdir()
    (root / "wrong-shape" / PROJECT_FILE).write_text(json.dumps(["not", "a", "dict"]))
    good = create_project("M 42", root=root)
    good.close()

    summaries = list_projects(root)
    assert [s.slug for s in summaries] == ["m-42"]


# --- db schema -------------------------------------------------------------------

#: Expected (column, type, pk) per table, from PRAGMA table_info.
EXPECTED_COLUMNS = {
    "sessions": [
        ("id", "TEXT", 1),
        ("started_at", "TEXT", 0),
        ("ended_at", "TEXT", 0),
    ],
    "frames": [
        ("id", "INTEGER", 1),
        ("session_id", "TEXT", 0),
        ("kind", "TEXT", 0),
        ("path", "TEXT", 0),
        ("jd", "REAL", 0),
        ("utc", "TEXT", 0),
        ("exposure", "REAL", 0),
        ("gain", "REAL", 0),
        ("target", "TEXT", 0),
    ],
    "stars": [
        ("id", "INTEGER", 1),
        ("role", "TEXT", 0),
        ("label", "TEXT", 0),
        ("x", "REAL", 0),
        ("y", "REAL", 0),
        ("ra_deg", "REAL", 0),
        ("dec_deg", "REAL", 0),
        ("aperture_radius", "REAL", 0),
        ("sky_annulus_inner", "REAL", 0),
        ("sky_annulus_outer", "REAL", 0),
    ],
    "measurements": [
        ("id", "INTEGER", 1),
        ("frame_id", "INTEGER", 0),
        ("jd", "REAL", 0),
        ("mag", "REAL", 0),
        ("mag_err", "REAL", 0),
        ("excluded", "INTEGER", 0),
    ],
}

#: Expected indexes per table, from PRAGMA index_list.
EXPECTED_INDEXES = {
    "sessions": {"idx_sessions_started_at"},
    "frames": {"idx_frames_session_id", "idx_frames_jd"},
    "stars": {"idx_stars_role"},
    "measurements": {"idx_measurements_frame_id", "idx_measurements_jd"},
}


def test_db_schema_tables_and_columns(project):
    for table, expected in EXPECTED_COLUMNS.items():
        info = project.db.execute(f"PRAGMA table_info({table})").fetchall()
        assert [(row[1], row[2], row[5]) for row in info] == expected, table


def test_db_schema_indexes(project):
    for table, expected in EXPECTED_INDEXES.items():
        indexes = {row[1] for row in project.db.execute(f"PRAGMA index_list({table})")}
        assert expected <= indexes, table


def test_db_schema_foreign_keys(project):
    fk_measurements = project.db.execute("PRAGMA foreign_key_list(measurements)").fetchall()
    assert [(row[2], row[3], row[4]) for row in fk_measurements] == [("frames", "frame_id", "id")]
    fk_frames = project.db.execute("PRAGMA foreign_key_list(frames)").fetchall()
    assert [(row[2], row[3], row[4]) for row in fk_frames] == [("sessions", "session_id", "id")]
    # enforced on the open connection
    assert project.db.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_db_wal_mode_and_user_version(project):
    assert project.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert project.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_db_wal_mode_persists_across_reopen(root):
    create_project("M 42", root=root).close()
    with open_project(root / "m-42") as p:
        assert p.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert p.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_db_check_constraints_enforced(project):
    with project.db:
        project.db.execute(
            "INSERT INTO sessions (id, started_at) VALUES ('s1', '2026-11-20T22:00:00+00:00')"
        )
    with pytest.raises(sqlite3.IntegrityError), project.db:
        project.db.execute("INSERT INTO frames (session_id, kind, path, jd, utc) VALUES ('s1', 'x', 'p', 1.0, 't')")
    with pytest.raises(sqlite3.IntegrityError), project.db:
        project.db.execute("INSERT INTO stars (role) VALUES ('bogus')")
    with pytest.raises(sqlite3.IntegrityError), project.db:
        project.db.execute(
            "INSERT INTO measurements (frame_id, jd, mag, excluded) VALUES (1, 2.0, 3.0, 2)"
        )


def test_db_foreign_key_enforced(project):
    with pytest.raises(sqlite3.IntegrityError), project.db:
        project.db.execute(
            "INSERT INTO measurements (frame_id, jd, mag) VALUES (999, 2460000.5, 12.3)"
        )


def test_db_transaction_rolls_back_on_error(project):
    with pytest.raises(sqlite3.IntegrityError), project.db:
        project.db.execute(
            "INSERT INTO sessions (id, started_at) VALUES ('s1', '2026-11-20T22:00:00+00:00')"
        )
        project.db.execute("INSERT INTO stars (role) VALUES ('bogus')")  # fails
    count = project.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    assert count == 0  # the whole transaction rolled back


# --- slugify ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, slug",
    [
        ("M 42", "m-42"),
        ("M42", "m42"),
        ("V* bet Per", "v-bet-per"),
        ("NGC 1976", "ngc-1976"),
        ("  padded  name  ", "padded-name"),
        ("already-a-slug", "already-a-slug"),
        ("multiple---dashes", "multiple-dashes"),
        ("under_score", "under-score"),
        ("M42 (Orion)", "m42-orion"),
        ("a" * 100, "a" * MAX_SLUG_LENGTH),
    ],
)
def test_slugify_cases(name, slug):
    assert slugify(name) == slug


@pytest.mark.parametrize("bad", ["", "   ", "!!!", "---", None, 42, 3.14])
def test_slugify_rejects_unusable(bad):
    with pytest.raises(ValueError):
        slugify(bad)


def test_slugify_truncates_and_strips_trailing_dash():
    # 63 a's + dash + 10 b's is 74 chars; truncation lands on the dash
    slug = slugify("a" * 63 + "-" + "b" * 10)
    assert slug == "a" * 63
    assert len(slug) <= MAX_SLUG_LENGTH


def test_create_existing_project_raises_exists(root):
    create_project("M 42", root=root).close()
    with pytest.raises(ProjectExistsError) as excinfo:
        create_project("M 42", root=root)
    assert "m-42" in str(excinfo.value)
    assert issubclass(ProjectExistsError, ProjectError)


def test_slug_collision_raises_exists(root):
    """Documented choice: names that slugify alike collide -> error, no suffix."""
    create_project("M 42", root=root).close()
    for other in ("M-42", "m 42", "M.42"):
        with pytest.raises(ProjectExistsError):
            create_project(other, root=root)
    # only one project directory exists
    assert [p.slug for p in list_projects(root)] == ["m-42"]


# --- open: validation errors -------------------------------------------------------


def test_open_missing_path_raises_not_found(tmp_path):
    with pytest.raises(ProjectNotFoundError) as excinfo:
        open_project(tmp_path / "nope")
    assert "nope" in str(excinfo.value)
    assert issubclass(ProjectNotFoundError, ProjectError)
    # a file is not a project directory either
    some_file = tmp_path / "file"
    some_file.write_text("x")
    with pytest.raises(ProjectNotFoundError):
        open_project(some_file)


def test_open_missing_project_json_raises_invalid(root):
    (root).mkdir(parents=True)
    (root / "m-42").mkdir()
    with pytest.raises(InvalidProjectError, match="project.json"):
        open_project(root / "m-42")


def test_open_unparseable_project_json_raises_invalid(root):
    project_dir = root / "m-42"
    project_dir.mkdir(parents=True)
    (project_dir / PROJECT_FILE).write_text("{not json")
    with pytest.raises(InvalidProjectError, match="parseable JSON"):
        open_project(project_dir)


def test_open_wrong_shape_project_json_raises_invalid(root):
    project_dir = root / "m-42"
    project_dir.mkdir(parents=True)
    (project_dir / PROJECT_FILE).write_text(json.dumps(["not", "a", "dict"]))
    with pytest.raises(InvalidProjectError):
        open_project(project_dir)


def test_open_missing_required_key_raises_invalid(root):
    project_dir = root / "m-42"
    project_dir.mkdir(parents=True)
    (project_dir / PROJECT_FILE).write_text(json.dumps({"name": "M 42"}))
    with pytest.raises(InvalidProjectError, match="missing required key"):
        open_project(project_dir)


@pytest.mark.parametrize("version", [0, 4, 999, "1", 1.5, None, True])
def test_open_unsupported_schema_version_raises_invalid(root, version):
    project_dir = root / "m-42"
    project_dir.mkdir(parents=True)
    (project_dir / PROJECT_FILE).write_text(
        json.dumps(
            {
                "name": "M 42",
                "slug": "m-42",
                "created_at": "2026-11-20T00:00:00+00:00",
                "schema_version": version,
            }
        )
    )
    sqlite3.connect(project_dir / DB_FILE).close()
    with pytest.raises(InvalidProjectError, match="schema_version"):
        open_project(project_dir)


def test_open_missing_db_raises_invalid(root):
    project_dir = root / "m-42"
    project_dir.mkdir(parents=True)
    (project_dir / PROJECT_FILE).write_text(
        json.dumps(
            {
                "name": "M 42",
                "slug": "m-42",
                "created_at": "2026-11-20T00:00:00+00:00",
                "schema_version": 1,
            }
        )
    )
    with pytest.raises(InvalidProjectError, match=DB_FILE):
        open_project(project_dir)


def test_open_corrupt_db_raises_invalid(root):
    project_dir = root / "m-42"
    project_dir.mkdir(parents=True)
    (project_dir / PROJECT_FILE).write_text(
        json.dumps(
            {
                "name": "M 42",
                "slug": "m-42",
                "created_at": "2026-11-20T00:00:00+00:00",
                "schema_version": 1,
            }
        )
    )
    (project_dir / DB_FILE).write_bytes(b"this is not a sqlite database at all")
    with pytest.raises(InvalidProjectError, match="not a valid SQLite"):
        open_project(project_dir)


def test_open_db_missing_tables_raises_invalid(root):
    project_dir = root / "m-42"
    project_dir.mkdir(parents=True)
    (project_dir / PROJECT_FILE).write_text(
        json.dumps(
            {
                "name": "M 42",
                "slug": "m-42",
                "created_at": "2026-11-20T00:00:00+00:00",
                "schema_version": 1,
            }
        )
    )
    conn = sqlite3.connect(project_dir / DB_FILE)
    with conn:
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
        conn.execute("PRAGMA user_version=1")
    conn.close()
    with pytest.raises(InvalidProjectError, match="missing tables"):
        open_project(project_dir)


def test_open_user_version_mismatch_raises_invalid(root):
    p = create_project("M 42", root=root)
    with p.db:
        p.db.execute("PRAGMA user_version=7")
    p.close()
    with pytest.raises(InvalidProjectError, match="user_version"):
        open_project(root / "m-42")


# --- save_config: atomic write ------------------------------------------------------


def test_save_config_roundtrip(project):
    project.config["target"] = {"name": "M 42", "ra_deg": 83.82208, "dec_deg": -5.39114}
    project.config["period"] = 0.5
    out = project.save_config()
    assert out == project.config_path
    on_disk = json.loads(project.config_path.read_text(encoding="utf-8"))
    assert on_disk["target"]["name"] == "M 42"
    assert on_disk["period"] == 0.5
    # reopen: the change persisted
    project.close()
    with open_project(project.path) as p:
        assert p.config["target"]["ra_deg"] == 83.82208
        assert p.config["period"] == 0.5


def test_save_config_leaves_no_tmp_files(project):
    project.config["period"] = 1.25
    project.save_config()
    names = {entry.name for entry in project.path.iterdir()}
    assert not any(name.endswith(".tmp") for name in names)
    assert {PROJECT_FILE, DB_FILE, SUBS_DIR, STACKS_DIR} <= names


def test_save_config_failure_keeps_original_and_cleans_temp(project, monkeypatch):
    original = project.config_path.read_bytes()

    def boom(*args, **kwargs):
        raise OSError("rename failed")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        project.save_config()
    assert project.config_path.read_bytes() == original  # untouched
    assert not list(project.path.glob("*.tmp"))  # temp cleaned up


def test_save_config_interrupt_leaves_no_partial_file(project, monkeypatch):
    original = project.config_path.read_bytes()

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "fsync", interrupt)
    with pytest.raises(KeyboardInterrupt):
        project.save_config()
    assert project.config_path.read_bytes() == original
    assert not list(project.path.glob("*.tmp"))


# --- project root resolution ---------------------------------------------------------


def test_default_project_root_precedence(monkeypatch, tmp_path):
    assert default_project_root({}) == DEFAULT_PROJECT_ROOT
    assert default_project_root({PROJECT_ROOT_ENV_VAR: "/tmp/elsewhere"}) == Path("/tmp/elsewhere")
    monkeypatch.setenv(PROJECT_ROOT_ENV_VAR, str(tmp_path / "from-env"))
    assert default_project_root() == tmp_path / "from-env"


def test_create_and_list_use_env_root(monkeypatch, tmp_path):
    env_root = tmp_path / "env-root"
    monkeypatch.setenv(PROJECT_ROOT_ENV_VAR, str(env_root))
    p = create_project("Env Star")  # root=None -> env var
    p.close()
    assert p.path == env_root / "env-star"
    assert [s.slug for s in list_projects()] == ["env-star"]
    # an explicit root wins over the env var
    explicit = tmp_path / "explicit"
    q = create_project("Env Star", root=explicit)
    q.close()
    assert q.path == explicit / "env-star"
    assert [s.slug for s in list_projects()] == ["env-star"]  # still the env root


# --- module boundary + public API -----------------------------------------------------


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


def test_public_api_is_exported_from_package():
    from seestar_controller import store

    for name in store.__all__:
        assert hasattr(store, name), name
    assert issubclass(store.ProjectError, Exception)
    assert issubclass(store.ProjectExistsError, store.ProjectError)
    assert issubclass(store.ProjectNotFoundError, store.ProjectError)
    assert issubclass(store.InvalidProjectError, store.ProjectError)
    assert callable(store.create_project)
    assert callable(store.open_project)
    assert callable(store.list_projects)
    assert callable(store.slugify)
    assert isinstance(store.Project, type)
    assert isinstance(store.ProjectSummary, type)
    assert store.SCHEMA_VERSION == 3
