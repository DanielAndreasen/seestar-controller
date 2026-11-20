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
in WAL mode (``PRAGMA journal_mode=WAL``, set once by :func:`initialize_db` and
persisted in the file) so a crash mid-write never corrupts it. The schema
version is stored twice on purpose: as ``schema_version`` in ``project.json``
and as the ``user_version`` pragma in the db — :func:`seestar_controller.store.project.open_project`
requires them to match, and a future migration runner (ticket 07) bumps both.

Note: the ``stars`` table gained its aperture columns (and ``NOT NULL`` on
``x``/``y``) in-place during schema v1 development, before any release — no
migration is needed for v1 projects created by this build; ticket 07 owns
migrations for anything older.
"""

from __future__ import annotations

import sqlite3

#: The schema version this build writes and supports.
SCHEMA_VERSION = 1

#: The tables every project database must have (checked on open).
SCHEMA_TABLES = ("sessions", "frames", "stars", "measurements")

#: The DDL statements, executed in one transaction by :func:`initialize_db`.
#: ``IF NOT EXISTS`` keeps initialization idempotent.
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
    foreign-key enforcement on the connection, creates all tables and indexes
    in ONE transaction, and stamps ``PRAGMA user_version`` with
    :data:`SCHEMA_VERSION`. The caller owns the connection (open/close).
    """
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    with conn:  # one transaction: all tables or none
        for statement in SCHEMA_STATEMENTS:
            conn.execute(statement)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
