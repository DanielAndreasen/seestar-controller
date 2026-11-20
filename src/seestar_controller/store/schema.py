"""SQLite schema of a project database (``db.sqlite``).

Pure-Python (module boundary per ``.scratch/project-store/spec.md``): no GTK
imports, no :mod:`seestar_controller.link` imports, stdlib :mod:`sqlite3` only.

One project database holds the whole observing campaign (GLOSSARY.md):

- ``sessions`` — one row per night (a **Session**): ``id`` is the session slug
  (also the name of its ``subs/<session>/`` / ``stacks/<session>/`` dirs),
  ``started_at``/``ended_at`` are UTC ISO-8601 strings (``ended_at`` is NULL
  while the session is open);
- ``frames`` — one row per stored FITS file (sub-frame or stack): the session
  FK, ``kind`` (``sub``/``stack``), the project-relative ``path``, the
  observation time as both ``jd`` (Julian Date — the photometry standard) and
  ``utc`` (ISO-8601), plus ``exposure``/``gain``/``target`` metadata;
- ``stars`` — the saved star picks (see :mod:`.stars`): ``role`` is ``target``
  or ``comp``, with the pixel position (``x``/``y``, required — image
  coordinates) and the sky position (``ra_deg``/``dec_deg``, J2000 degrees,
  nullable — picks may be pixel-only), plus the per-pick aperture settings
  (``aperture_radius``, ``sky_annulus_inner``/``sky_annulus_outer``, all in
  pixels);
- ``measurements`` — one differential-magnitude data point per frame: the
  frame FK, ``jd``, ``mag``, ``mag_err``, and the ``excluded`` outlier flag.

All writes go through SQLite transactions (``with conn:``); the database runs
in WAL mode (``PRAGMA journal_mode=WAL``, set once by :func:`initialize_db`,
re-asserted by :func:`seestar_controller.store.project.open_project`, and
persisted in the file) so a crash mid-write never corrupts it.

The schema version is stored twice on purpose: as ``schema_version`` in
``project.json`` and as the ``user_version`` pragma in the db.
:func:`migrate_db` is the migration runner: it reads ``user_version`` and
applies every pending migration from :data:`MIGRATIONS` in ascending order,
each in ONE explicit transaction together with its version bump (an explicit
``BEGIN`` is used because the legacy isolation level would autocommit DDL, so
a crash mid-migration rolls back completely and the next open re-runs it).
:func:`seestar_controller.store.project.open_project` calls it on every open
and then mirrors the new version into ``project.json`` — both copies are
bumped together.

Migration history:

- **0 -> 1** — create the schema: an empty db (``user_version=0``, no tables)
  gets :data:`SCHEMA_STATEMENTS` (``IF NOT EXISTS``, so it also completes a
  partially created v0 db);
- **1 -> 2** — add the per-pick aperture columns (``aperture_radius``,
  ``sky_annulus_inner``, ``sky_annulus_outer``) to the ``stars`` table when
  missing. Ticket 04 extended the ``stars`` table in place within schema v1,
  so dev-build v1 databases may lack them; existing rows are backfilled with
  the default aperture (:data:`STARS_APERTURE_DEFAULTS`) via
  ``NOT NULL DEFAULT``, which keeps the columns ``NOT NULL`` exactly like in
  a fresh v2 db. (``x``/``y`` nullability cannot be altered in place; the
  store always writes ``x``/``y``.)

:func:`initialize_db` creates the v2 schema directly (the current
:data:`SCHEMA_STATEMENTS`).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from .errors import InvalidProjectError

#: The schema version this build writes and supports.
SCHEMA_VERSION = 2

#: The tables every project database must have (checked on open).
SCHEMA_TABLES = ("sessions", "frames", "stars", "measurements")

#: The DDL statements creating the schema — applied as migration 0 -> 1 by
#: :func:`migrate_db` (and hence by :func:`initialize_db` on a fresh db).
#: ``IF NOT EXISTS`` keeps them idempotent.
SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY NOT NULL,
        started_at TEXT NOT NULL,
        ended_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sessions_started_at ON sessions (started_at)",
    """
    CREATE TABLE IF NOT EXISTS frames (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK (kind IN ('sub', 'stack')),
        path TEXT NOT NULL,
        jd REAL NOT NULL,
        utc TEXT NOT NULL,
        exposure REAL,
        gain REAL,
        target TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_frames_session_id ON frames (session_id)",
    "CREATE INDEX IF NOT EXISTS idx_frames_jd ON frames (jd)",
    """
    CREATE TABLE IF NOT EXISTS stars (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        role TEXT NOT NULL CHECK (role IN ('target', 'comp')),
        label TEXT,
        x REAL NOT NULL,
        y REAL NOT NULL,
        ra_deg REAL,
        dec_deg REAL,
        aperture_radius REAL NOT NULL,
        sky_annulus_inner REAL NOT NULL,
        sky_annulus_outer REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_stars_role ON stars (role)",
    """
    CREATE TABLE IF NOT EXISTS measurements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        frame_id INTEGER NOT NULL REFERENCES frames (id) ON DELETE CASCADE,
        jd REAL NOT NULL,
        mag REAL NOT NULL,
        mag_err REAL,
        excluded INTEGER NOT NULL DEFAULT 0 CHECK (excluded IN (0, 1))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_measurements_frame_id ON measurements (frame_id)",
    "CREATE INDEX IF NOT EXISTS idx_measurements_jd ON measurements (jd)",
)


def initialize_db(conn: sqlite3.Connection) -> None:
    """Initialize a fresh project database on ``conn``.

    Sets WAL journal mode (crash safety; persisted in the db file), enables
    foreign-key enforcement on the connection, then creates the schema and
    stamps ``PRAGMA user_version`` with :data:`SCHEMA_VERSION` by running
    :func:`migrate_db` on the fresh (``user_version=0``) connection — so a
    freshly created db goes through exactly the same migration path as an
    old one, each migration in one explicit transaction. The caller owns the
    connection (open/close).
    """
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    migrate_db(conn)


#: A migration step: a SQL statement executed as-is, or a callable taking the
#: connection (for conditional DDL — SQLite has no ``ADD COLUMN IF NOT
#: EXISTS``, so the 1 -> 2 step checks ``PRAGMA table_info`` first).
MigrationStep = str | Callable[[sqlite3.Connection], None]

#: The default aperture (pixels) backfilled into pre-v2 ``stars`` rows by the
#: 1 -> 2 migration: ``(column, default)`` pairs. A migrated row is immediately
#: a valid :class:`~.stars.StarPick` (radius > 0, 0 < inner < outer).
STARS_APERTURE_DEFAULTS = (
    ("aperture_radius", 4.0),
    ("sky_annulus_inner", 8.0),
    ("sky_annulus_outer", 12.0),
)


def _add_stars_aperture_columns(conn: sqlite3.Connection) -> None:
    """Migration step 1 -> 2: add the per-pick aperture columns to the
    ``stars`` table when they are missing (dev-build v1 dbs predate ticket
    04's in-place extension). Existing rows are backfilled with
    :data:`STARS_APERTURE_DEFAULTS` via ``NOT NULL DEFAULT``."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(stars)")}
    for column, default in STARS_APERTURE_DEFAULTS:
        if column not in existing:
            conn.execute(
                f"ALTER TABLE stars ADD COLUMN {column} REAL NOT NULL DEFAULT {default}"
            )


#: The migrations, keyed by the version they migrate FROM: applying
#: ``MIGRATIONS[v]`` to a db at ``user_version == v`` brings it to ``v + 1``.
#: :func:`migrate_db` walks the keys in ascending order up to
#: :data:`SCHEMA_VERSION`.
MIGRATIONS: dict[int, tuple[MigrationStep, ...]] = {
    0: tuple(SCHEMA_STATEMENTS),  # empty db -> v1: create the schema
    1: (_add_stars_aperture_columns,),  # dev-build v1 -> v2: aperture columns
}


def migrate_db(conn: sqlite3.Connection) -> int:
    """Migrate the project database on ``conn`` up to :data:`SCHEMA_VERSION`.

    Reads ``PRAGMA user_version`` and applies every pending migration from
    :data:`MIGRATIONS` in ascending order. Each migration runs in ONE
    explicit transaction (``BEGIN IMMEDIATE`` ... ``COMMIT``) that also
    contains its ``PRAGMA user_version`` bump, so a crash mid-migration rolls
    back to the previous version completely — DDL included, which the legacy
    isolation level would otherwise autocommit — and the next call re-runs
    the migration. Returns the resulting version, always
    :data:`SCHEMA_VERSION` on success (a db already at
    :data:`SCHEMA_VERSION` is left untouched).

    ``conn`` must not have an open transaction (``open_project`` guarantees
    that). The caller owns the connection and is responsible for WAL mode.

    Raises :class:`~.errors.InvalidProjectError` when the db's
    ``user_version`` is newer than this build (``> SCHEMA_VERSION``), negative,
    or has no migration path to :data:`SCHEMA_VERSION`; a corrupt db surfaces
    as :class:`sqlite3.DatabaseError` (the caller wraps it into
    :class:`~.errors.InvalidProjectError`).
    """
    if conn.in_transaction:
        raise InvalidProjectError(
            "migrate_db needs a connection without an open transaction"
        )
    user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if user_version > SCHEMA_VERSION:
        raise InvalidProjectError(
            f"db user_version {user_version} is newer than this build supports "
            f"(SCHEMA_VERSION {SCHEMA_VERSION}) — the project was written by a "
            "newer version of seestar-controller"
        )
    if user_version < 0:
        raise InvalidProjectError(f"db user_version {user_version} is invalid (corrupt db)")
    version = user_version
    while version < SCHEMA_VERSION:
        steps = MIGRATIONS.get(version)
        if steps is None:
            raise InvalidProjectError(
                f"no migration path from db user_version {version} to "
                f"SCHEMA_VERSION {SCHEMA_VERSION}"
            )
        conn.execute("BEGIN IMMEDIATE")
        try:
            for step in steps:
                if callable(step):
                    step(conn)
                else:
                    conn.execute(step)
            conn.execute(f"PRAGMA user_version={version + 1}")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass  # the transaction is already gone
            raise
        conn.execute("COMMIT")
        version += 1
    return version
