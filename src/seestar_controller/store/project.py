"""Project create/open/list — the on-disk store of one observing campaign.

Pure-Python (module boundary per ``.scratch/project-store/spec.md``): no GTK
imports, no :mod:`seestar_controller.link` imports, stdlib only.

A **Project** (GLOSSARY.md) is a directory under the project root (default
``~/SeestarProjects/``, overridable with the ``SEESTAR_PROJECT_ROOT``
environment variable — same env-override pattern as ``SEESTAR_KEY_PATH`` /
``SEESTAR_CACHE_DIR``)::

    <root>/<slug>/
        project.json      settings, star picks, found period (see below)
        db.sqlite         sessions / frames / stars / measurements / presets /
                          frame_quality (see .schema)
        subs/             sub-frame FITS files, per session: subs/<session>/
        stacks/           stacked FITS files, per session: stacks/<session>/

The ``<session>`` subdirectories are created lazily by the frame-storage
ticket; this module creates only the top-level layout.

``project.json`` at creation time::

    {
      "schema_version": 2,          # SCHEMA_VERSION, mirrored by db user_version
      "name": "M 42",               # the display name as given
      "slug": "m-42",               # the directory name (see slugify)
      "created_at": "2026-11-20T01:23:45.678+00:00",   # UTC ISO-8601
      "target": null,               # placeholder: {"name", "ra_deg", "dec_deg"}
      "star_picks": null,           # placeholder: {"target": ..., "comps": [...]}
      "period": null                # placeholder: found period in days
    }

Times are stored as UTC ISO-8601 strings with an explicit ``+00:00`` offset.

Slug policy (documented choice): :func:`slugify` lowercases the name and maps
every run of non-ASCII-alphanumeric characters to a single dash, so
``"M 42"`` -> ``"m-42"``. A **collision raises** :class:`ProjectExistsError`
rather than silently suffixing — two names that slugify alike (``"M 42"`` and
``"M-42"``) would otherwise produce two projects whose directories differ only
by an invisible suffix, so the caller is told to pick a different name.

Writes are crash-safe: the db runs in WAL mode with transactions (see
:mod:`.schema`), schema upgrades run through the migration runner
(:func:`.schema.migrate_db` — one explicit transaction per migration, so a
crash mid-migration rolls back and the next open re-runs it), and
``project.json`` is written atomically (temp file in the same dir + fsync +
:func:`os.replace`, the same pattern as ``catalog/cache.py`` and
``link/frames.py`` — re-declared here because this package must not import
them); frame and CSV files are likewise temp+rename (see :mod:`.frames` /
:mod:`.csv_export`). A crash mid-write therefore never leaves a partial file
at a final name; a stray ``*.tmp`` left behind by a killed process is ignored
on open and never surfaces through the API (the store has no locking — spec
out of scope — so temp files are never deleted, only never read).
:func:`create_project` is all-or-nothing on top of that: any failure after the
collision check removes the partially created project directory again, so a
failed create never blocks the project name.

Deletion (project-store spec user story 9, implemented by the
app-improvement ticket 16): :func:`delete_project` removes a whole project
directory, but only when the path really is a project (parseable
``project.json`` with the required keys) and only when it is not open — every
:class:`Project` returned by :func:`open_project` registers itself in the
module-level :data:`_OPEN_PROJECTS` weak set and unregisters on
:meth:`Project.close`, and :func:`delete_project` refuses with
:class:`~.errors.ProjectOpenError` while any live handle has the same path.

Re-opening (issue 08): :func:`open_project` on a path that is ALREADY open is
a no-op — it returns the existing live handle instead of a second
:class:`Project`, so re-opening a project can never swap the handle
controllers hold. A handle whose db was nevertheless closed (e.g. it missed
the re-open event) is revived in place by :meth:`Project.ensure_open`, which
reconnects to ``db.sqlite`` (WAL, foreign keys ON) and re-registers the
handle — the app controllers call it before every store access (see their
``_call_store``), so a stale handle can no longer raise
``sqlite3.ProgrammingError: Cannot operate on a closed database``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
import weakref
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

from . import schema
from .errors import (
    InvalidProjectError,
    ProjectExistsError,
    ProjectNotFoundError,
    ProjectOpenError,
)

#: File name of the project settings inside a project directory.
PROJECT_FILE = "project.json"

#: File name of the project database inside a project directory.
DB_FILE = "db.sqlite"

#: Directory for sub-frame FITS files (``subs/<session>/`` per session).
SUBS_DIR = "subs"

#: Directory for stacked FITS files (``stacks/<session>/`` per session).
STACKS_DIR = "stacks"

#: The top-level subdirectories created with every project (session
#: subdirectories under them are created lazily by later tickets).
PROJECT_SUBDIRS = (SUBS_DIR, STACKS_DIR)

#: Default project root: ``~/SeestarProjects/``.
DEFAULT_PROJECT_ROOT = Path.home() / "SeestarProjects"

#: Environment variable that overrides the default project root (same
#: explicit-arg > env-var > default precedence as the key path / cache dir).
PROJECT_ROOT_ENV_VAR = "SEESTAR_PROJECT_ROOT"

#: Maximum length of a slug (keeps directory names filesystem-friendly).
MAX_SLUG_LENGTH = 64

#: The keys every project.json must have to be a valid project.
_REQUIRED_CONFIG_KEYS = ("name", "slug", "created_at", "schema_version")

#: Placeholder keys written at creation (filled in by later tickets).
_CONFIG_PLACEHOLDERS = ("target", "star_picks", "period")

#: Runs of characters that are not [a-z0-9] become one dash in a slug.
_NON_ALNUM_RUN = re.compile(r"[^a-z0-9]+")

#: The live open-project handles (weak refs): :func:`open_project` registers
#: every :class:`Project` it returns, :meth:`Project.close` discards it,
#: :meth:`Project.ensure_open` re-registers a revived one. Used by
#: :func:`open_project` (re-opening an open path returns the existing handle)
#: and by :func:`delete_project` (refuses deleting a project that is open).
_OPEN_PROJECTS: weakref.WeakSet[Project] = weakref.WeakSet()


def _connect_db(db_path: Path) -> sqlite3.Connection:
    """Open ``db_path`` in WAL mode with foreign keys ON — the connection
    setup shared by :func:`open_project` and :meth:`Project.ensure_open`
    (crash safety + referential integrity on every connection)."""
    db = sqlite3.connect(db_path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    return db


def default_project_root(env: Mapping[str, str] | None = None) -> Path:
    """Resolve the default project root.

    Precedence: the ``SEESTAR_PROJECT_ROOT`` environment variable > the default
    ``~/SeestarProjects/``.
    """
    environ = os.environ if env is None else env
    value = environ.get(PROJECT_ROOT_ENV_VAR)
    if value:
        return Path(value)
    return DEFAULT_PROJECT_ROOT


def utc_now_iso() -> str:
    """The current time as a UTC ISO-8601 string with explicit offset,
    e.g. ``2026-11-20T01:23:45.678+00:00`` (parseable by
    :meth:`datetime.datetime.fromisoformat`)."""
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def slugify(name: Any) -> str:
    """Turn a project ``name`` into its directory slug.

    Lowercase ASCII alphanumerics and dashes only: every run of other
    characters becomes one dash, leading/trailing dashes are stripped, and the
    result is capped at :data:`MAX_SLUG_LENGTH` characters. Examples:
    ``"M 42"`` -> ``"m-42"``, ``"V* bet Per"`` -> ``"v-bet-per"``,
    ``"NGC 1976"`` -> ``"ngc-1976"``.

    Raises :class:`ValueError` when the name is not a string or has no usable
    characters (``""``, ``"!!!"``).
    """
    if not isinstance(name, str):
        raise ValueError(f"project name must be a string, got {name!r}")  # noqa: TRY004
    slug = _NON_ALNUM_RUN.sub("-", name.strip().lower()).strip("-")
    if len(slug) > MAX_SLUG_LENGTH:
        slug = slug[:MAX_SLUG_LENGTH].strip("-")
    if not slug:
        raise ValueError(
            f"project name {name!r} has no usable characters for a slug "
            "(lowercase ASCII alphanumerics and dashes only)"
        )
    return slug


def _atomic_write_json(path: Path, obj: Any) -> Path:
    """Write ``obj`` as pretty JSON to ``path`` atomically.

    Same pattern as ``catalog/cache.py`` / ``link/frames.py``: temp file in the
    same directory, flush + fsync, then :func:`os.replace` onto the
    destination. On any failure (including :class:`KeyboardInterrupt`) the
    temp file is removed and the destination is left untouched — a reader
    never sees a partial file. Returns the destination path.
    """
    data = (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.close(fd)  # already closed when the with-block exited
        except OSError:
            pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


@dataclass(frozen=True)
class ProjectSummary:
    """One project as returned by :func:`list_projects` (not opened)."""

    name: str
    slug: str
    path: Path
    created_at: str


class Project:
    """An open project: its directory, its config, and its db connection.

    - ``path`` — the project directory (``<root>/<slug>/``);
    - ``config`` — the parsed ``project.json`` as a dict (mutate it and call
      :meth:`save_config` to persist);
    - ``db`` — the open :class:`sqlite3.Connection` to ``db.sqlite`` with
      foreign keys ON; use ``with project.db:`` for transactional writes.

    Use :meth:`close` (or a ``with`` block) to close the db connection, and
    :meth:`ensure_open` to revive a handle whose db was closed (issue 08 —
    e.g. the same project path was re-opened while this handle was still
    referenced by a controller).
    """

    def __init__(
        self, path: str | Path, config: dict[str, Any], db: sqlite3.Connection
    ) -> None:
        self.path = Path(path)
        self.config = config
        self.db = db
        self._db_closed = False

    @property
    def name(self) -> str:
        """The display name from ``project.json``."""
        return self.config["name"]

    @property
    def slug(self) -> str:
        """The directory slug from ``project.json``."""
        return self.config["slug"]

    @property
    def config_path(self) -> Path:
        """``<path>/project.json``."""
        return self.path / PROJECT_FILE

    @property
    def db_path(self) -> Path:
        """``<path>/db.sqlite``."""
        return self.path / DB_FILE

    def save_config(self) -> Path:
        """Persist ``self.config`` to ``project.json`` atomically.

        Temp file in the same directory + fsync + :func:`os.replace` (see
        :func:`_atomic_write_json`), so a crash never leaves a partial
        ``project.json`` and no ``*.tmp`` file is left behind. Returns the
        written path.
        """
        return _atomic_write_json(self.config_path, self.config)

    def close(self) -> None:
        """Close the db connection and unregister this handle as open
        (so :func:`delete_project` may delete the project again).
        Idempotent."""
        _OPEN_PROJECTS.discard(self)
        self.db.close()
        self._db_closed = True

    def ensure_open(self) -> None:
        """Make sure this handle's db connection is live, reopening it when
        it was closed (issue 08): a :class:`Project` whose db was closed —
        e.g. because the same project path was re-opened and this stale
        handle was kept by a controller — gets a fresh connection to
        ``db.sqlite`` (WAL mode, foreign keys ON, see :func:`_connect_db`)
        and is registered as open again. No-op while the db is still open.

        Must be called on the thread that owns the db connection (the app
        controllers call it from their ``_call_store``, right before the
        store call itself, so the sqlite thread-affinity is preserved).
        """
        if self._db_closed:
            self.db = _connect_db(self.db_path)
            self._db_closed = False
            _OPEN_PROJECTS.add(self)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"Project(name={self.name!r}, path={str(self.path)!r})"


def create_project(name: str, root: str | Path | None = None) -> Project:
    """Create a new project under ``root`` and return it open.

    - ``name`` — the display name; stripped, then :func:`slugify` gives the
      directory name. Raises :class:`ValueError` for an unusable name.
    - ``root`` — the project root (default: :func:`default_project_root`, i.e.
      ``SEESTAR_PROJECT_ROOT`` or ``~/SeestarProjects/``); created if missing.

    Makes the top-level layout (``project.json``, ``db.sqlite``, ``subs/``,
    ``stacks/``), initializes the db with the schema (:mod:`.schema`, WAL mode,
    one transaction), writes ``project.json`` atomically, and returns the
    project via :func:`open_project`.

    All-or-nothing: if any step after the collision check fails (subdir
    creation, db init, the ``project.json`` write, or the final
    :func:`open_project`), the partially created ``<root>/<slug>/`` directory
    is removed again — a failure never leaves a half-initialized project
    behind, so the name is not blocked and can be reused immediately.

    Raises :class:`ProjectExistsError` when ``<root>/<slug>/`` already exists
    (slug collision — documented choice, see :func:`slugify`).
    """
    if not isinstance(name, str):
        raise ValueError(f"project name must be a string, got {name!r}")  # noqa: TRY004
    name = name.strip()
    if not name:
        raise ValueError("project name must not be empty")
    if root is None:
        root = default_project_root()
    root = Path(root)
    slug = slugify(name)
    project_dir = root / slug
    try:
        project_dir.mkdir(parents=True)  # fails on any existing entry -> collision
    except FileExistsError:
        raise ProjectExistsError(
            f"a project already exists at {project_dir} "
            f"(name {name!r} slugs to {slug!r}) — pick a different name"
        ) from None
    try:
        for subdir in PROJECT_SUBDIRS:
            (project_dir / subdir).mkdir()
        conn = sqlite3.connect(project_dir / DB_FILE)
        try:
            schema.initialize_db(conn)
        finally:
            conn.close()
        config: dict[str, Any] = {
            "schema_version": schema.SCHEMA_VERSION,
            "name": name,
            "slug": slug,
            "created_at": utc_now_iso(),
        }
        for placeholder in _CONFIG_PLACEHOLDERS:
            config[placeholder] = None
        _atomic_write_json(project_dir / PROJECT_FILE, config)
        return open_project(project_dir)
    except BaseException:
        # all-or-nothing: remove the partial directory (best effort — never
        # mask the original error) so the name stays usable
        shutil.rmtree(project_dir, ignore_errors=True)
        raise


def _missing_tables(db: sqlite3.Connection) -> list[str]:
    """The :data:`schema.SCHEMA_TABLES` missing from ``db``, sorted."""
    tables = {
        row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    return sorted(set(schema.SCHEMA_TABLES) - tables)


def open_project(path: str | Path, *, fresh: bool = False) -> Project:
    """Open and validate the project at ``path`` (its directory).

    Re-opening an ALREADY-OPEN path is a no-op (issue 08): when a live
    handle from a previous :func:`open_project` call has the same (resolved)
    path, that existing handle is returned instead of a new :class:`Project`
    with a second db connection — so re-opening a project can never swap the
    handle the controllers hold. The returned handle is guaranteed live
    (:meth:`Project.ensure_open` revives it if its db was closed).

    ``fresh=True`` opts out of the no-op and always opens a new
    :class:`Project` with its own db connection, even when the path is
    already open — for callers that genuinely need a second, thread-local
    connection (the report worker thread, whose sqlite objects must be
    created on that thread; WAL makes the concurrent access safe). The extra
    handle is registered in :data:`_OPEN_PROJECTS` like any other and is
    closed by the caller.

    Validation: ``path`` must be an existing directory containing a parseable
    ``project.json`` with the required keys (``name``, ``slug``,
    ``created_at``, ``schema_version``) and a supported ``schema_version``
    (``1..SCHEMA_VERSION``), plus a ``db.sqlite`` that is a valid SQLite
    database containing all :data:`schema.SCHEMA_TABLES`.

    The db's ``user_version`` is authoritative for the schema: when it is
    older than :data:`schema.SCHEMA_VERSION`, :func:`schema.migrate_db`
    migrates it (each migration in one transaction) and the new version is
    mirrored into ``project.json`` (atomic write) — both copies of the
    version are bumped together. A db with ``user_version`` newer than
    :data:`schema.SCHEMA_VERSION` is rejected. The opened db runs in WAL mode
    (crash safety, also for migrated dbs) with foreign keys ON.

    Raises :class:`ProjectNotFoundError` when ``path`` is not a directory and
    :class:`InvalidProjectError` for anything invalid/corrupt/unsupported.

    The db connection is explicitly closed on every failure path (including
    a failing mirror write of ``project.json``) — no handle is leaked; on
    success the caller owns the connection (see :class:`Project`).
    """
    path = Path(path)
    resolved = path.resolve()
    if not fresh:
        for open_handle in _OPEN_PROJECTS:
            if Path(open_handle.path).resolve() == resolved:
                # already open: return the existing live handle (issue 08) —
                # opening a second connection would leave every holder of the
                # old handle with a closed db ("Cannot operate on a closed
                # database") once that handle is closed
                open_handle.ensure_open()
                return open_handle
    if not path.is_dir():
        raise ProjectNotFoundError(f"no project at {path} (not a directory)")
    config_path = path / PROJECT_FILE
    if not config_path.is_file():
        raise InvalidProjectError(f"{path} is not a project: missing {PROJECT_FILE}")
    try:
        config = json.loads(config_path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidProjectError(f"{config_path} is not parseable JSON: {exc}") from exc
    if not isinstance(config, dict):
        raise InvalidProjectError(f"{config_path} must contain a JSON object, got {config!r}")
    for key in _REQUIRED_CONFIG_KEYS:
        if key not in config:
            raise InvalidProjectError(f"{config_path} is missing required key {key!r}")
    version = config["schema_version"]
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or not 1 <= version <= schema.SCHEMA_VERSION
    ):
        raise InvalidProjectError(
            f"unsupported schema_version {version!r} in {config_path} "
            f"(this version supports 1..{schema.SCHEMA_VERSION})"
        )
    db_path = path / DB_FILE
    if not db_path.is_file():
        raise InvalidProjectError(f"{path} is not a project: missing {DB_FILE}")
    db: sqlite3.Connection | None = None
    opened = False
    try:
        db = _connect_db(db_path)
        user_version = db.execute("PRAGMA user_version").fetchone()[0]
        if user_version >= 1:
            # a db at version >= 1 must already have all tables; a v0 db is
            # empty and gets them from the 0 -> 1 migration below
            missing = _missing_tables(db)
            if missing:
                raise InvalidProjectError(f"{db_path} is missing tables {missing}")
        schema.migrate_db(db)  # older db -> SCHEMA_VERSION; newer db -> error
        missing = _missing_tables(db)
        if missing:
            raise InvalidProjectError(f"{db_path} is missing tables {missing}")
        project = Project(path=path, config=config, db=db)
        if config["schema_version"] != schema.SCHEMA_VERSION:
            # the db was migrated (or a previous open crashed between the db
            # migration and this mirror write): bump the project.json copy too
            config["schema_version"] = schema.SCHEMA_VERSION
            project.save_config()
        _OPEN_PROJECTS.add(project)
        opened = True
        return project
    except sqlite3.DatabaseError as exc:
        raise InvalidProjectError(f"{db_path} is not a valid SQLite database: {exc}") from exc
    finally:
        if not opened and db is not None:
            db.close()  # close the connection on ANY failure path


def list_projects(root: str | Path | None = None) -> list[ProjectSummary]:
    """List all projects under ``root`` (default: :func:`default_project_root`).

    Returns one :class:`ProjectSummary` (``name``, ``slug``, ``path``,
    ``created_at``) per subdirectory that has a parseable ``project.json``
    with the required keys; stray files/directories and unparseable entries
    are skipped silently. A missing ``root`` yields an empty list. Sorted by
    ``created_at`` (oldest first), then ``slug``.
    """
    if root is None:
        root = default_project_root()
    root = Path(root)
    summaries: list[ProjectSummary] = []
    if not root.is_dir():
        return []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        try:
            config = json.loads((entry / PROJECT_FILE).read_bytes().decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue  # not a project directory — skipped
        if not isinstance(config, dict):
            continue
        name, slug, created_at = (
            config.get("name"),
            config.get("slug"),
            config.get("created_at"),
        )
        if not (
            isinstance(name, str)
            and isinstance(slug, str)
            and isinstance(created_at, str)
        ):
            continue
        summaries.append(
            ProjectSummary(name=name, slug=slug, path=entry, created_at=created_at)
        )
    summaries.sort(key=lambda s: (s.created_at, s.slug))
    return summaries


def delete_project(path: str | Path) -> Path:
    """Delete the project at ``path`` — the whole directory tree (db, FITS
    files, lightcurve, everything) — and return the deleted path.

    Safety rules (a directory is only ever deleted when it really is a
    project, and never while it is in use):

    - ``path`` must be an existing directory whose ``project.json`` is
      parseable and has all :data:`_REQUIRED_CONFIG_KEYS` — anything else
      raises :class:`ProjectNotFoundError` (missing / not a directory) or
      :class:`InvalidProjectError` (not a project directory) and is left
      completely untouched;
    - the project must not be open: while any live :class:`Project` handle
      from :func:`open_project` (not yet :meth:`Project.close` d) has this
      path, :class:`ProjectOpenError` is raised and nothing is deleted —
      close the project first.

    Uses :func:`shutil.rmtree` on the validated project directory. The store
    has no locking (spec out of scope), so the caller must ensure no other
    process is writing the project.
    """
    path = Path(path)
    if not path.is_dir():
        raise ProjectNotFoundError(f"no project at {path} (not a directory)")
    config_path = path / PROJECT_FILE
    if not config_path.is_file():
        raise InvalidProjectError(f"{path} is not a project: missing {PROJECT_FILE}")
    try:
        config = json.loads(config_path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidProjectError(f"{config_path} is not parseable JSON: {exc}") from exc
    if not isinstance(config, dict) or any(
        key not in config for key in _REQUIRED_CONFIG_KEYS
    ):
        raise InvalidProjectError(
            f"{config_path} is missing required keys {_REQUIRED_CONFIG_KEYS} "
            f"— {path} is not a project directory"
        )
    resolved = path.resolve()
    for open_handle in _OPEN_PROJECTS:
        if Path(open_handle.path).resolve() == resolved:
            raise ProjectOpenError(
                f"project at {path} is open (as {open_handle.name!r}) — "
                "close it before deleting"
            )
    shutil.rmtree(path)
    return path
