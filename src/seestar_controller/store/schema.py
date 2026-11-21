"""SQLite schema of a project database (``db.sqlite``).

Pure-Python (module boundary per ``.scratch/project-store/spec.md``): no GTK
imports, no :mod:`seestar_controller.link` imports, stdlib :mod:`sqlite3` only.

One project database holds the whole observing campaign (GLOSSARY.md):

- ``sessions`` — one row per night (a **Session**): ``id`` is the session slug
  (also the name of its ``subs/<session>/`` / ``stacks/<session>/`` dirs),
  ``started_at``/``ended_at`` are UTC ISO-8601 strings (``ended_at`` is NULL
  while the session is open);
- ``frames`` — one row per stored FITS file (sub-frame, stack, or enhanced
  image): the session FK, ``kind`` (``sub``/``stack``/``enhanced``), the
  project-relative ``path``, the observation time as both ``jd`` (Julian Date
  — the photometry standard) and ``utc`` (ISO-8601), plus
  ``exposure``/``gain``/``target`` metadata;
- ``stars`` — the saved star picks (see :mod:`.stars`): ``role`` is ``target``,
  ``comp`` or ``check`` (the check star of check-star-plot ticket 01 — stored
  in the SAME role column; the 4 -> 5 migration widens the role CHECK to allow
  ``'check'``), with the pixel position (``x``/``y``, required — image
  coordinates) and the sky position (``ra_deg``/``dec_deg``, J2000 degrees,
  nullable — picks may be pixel-only), plus the per-pick aperture settings
  (``aperture_radius``, ``sky_annulus_inner``/``sky_annulus_outer``, all in
  pixels);
- ``measurements`` — one differential-magnitude data point per frame: the
  frame FK, ``jd``, ``mag``, ``mag_err``, and the ``excluded`` outlier flag;
- ``presets`` — one per-image enhancement preset (image-enhance ticket 04,
  see :mod:`.presets`): the frame id (TEXT primary key), the pipeline as a
  JSON dict, and the last-update time;
- ``frame_quality`` — one per-frame quality record (frame-quality ticket 01,
  see :mod:`.quality`): the frame id (INTEGER primary key), the metrics
  (``fwhm``, ``bg_median``, ``bg_std``, ``star_count``), the outlier flag
  (``flagged`` + the JSON ``flag_reasons`` list), and the scoring time;
- ``crossmatch`` — the persisted Gaia cross-match table of a stack
  (gaia-crossmatch ticket 02, see :mod:`.crossmatch`): one row per detected
  source, keyed by the frame FK (``frame_id``, cascade-deleted with the
  frame): the detected pixel position (``x``/``y``) and sky position
  (``ra_deg``/``dec_deg``), the ``matched`` flag, and the matched Gaia DR3
  fields (``source_id``, ``g``/``bp``/``rp``, ``parallax`` +
  ``parallax_error``, ``pmra``/``pmra_error``, ``pmdec``/``pmdec_error``,
  all nullable — NULL when unmatched) plus the match
  ``separation_arcsec``.
- ``frame_wcs`` — the persisted solved WCS of a frame (astrometry ticket 01,
  see :mod:`.wcs`): the frame id (INTEGER primary key, cascade-deleted with
  the frame), the solver that produced it (``solve-field`` or ``gaia``), the
  solve time (UTC ISO-8601), the match RMS in arcsec (nullable) and star
  count, the solved field center (``center_ra_deg``/``center_dec_deg``,
  J2000 degrees), and the full WCS as a JSON dict of FITS cards
  (``cards_json``) so the exact WCS can be rebuilt without the FITS file.

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
- **2 -> 3** — image-enhance ticket 04: create the ``presets`` table and
  widen the ``frames.kind`` CHECK to also allow ``'enhanced'`` (the enhanced
  images saved next to the stacks). SQLite cannot alter a CHECK in place, so
  the ``frames`` table is REBUILT (create ``frames__v3`` with the widened
  CHECK, copy the rows, drop the old table, rename, recreate the indexes).
  The rebuild runs with foreign-key enforcement OFF (see :func:`migrate_db`)
  because with FK ON a ``DROP TABLE`` would fire the implicit ``DELETE FROM``
  and cascade-delete the ``measurements`` rows; a ``PRAGMA foreign_key_check``
  after the rebuild proves nothing was lost.
- **3 -> 4** — frame-quality ticket 01: create the ``frame_quality`` table
  (one quality record per frame: FWHM, background median/std, star count,
  outlier flag + reasons, scoring time). ``IF NOT EXISTS`` keeps the step
  idempotent (a re-run after a crash between the db bump and the
  project.json mirror write is a no-op).
- **4 -> 5** — check-star-plot ticket 01: widen the ``stars.role`` CHECK to
  also allow ``'check'`` (the check star is stored in the SAME role column).
  SQLite cannot alter a CHECK in place, so the ``stars`` table is REBUILT
  exactly like the ``frames`` table in 2 -> 3 (create ``stars__v5`` with the
  widened CHECK, copy the rows, drop the old table, rename, recreate the
  index, ``PRAGMA foreign_key_check`` proves nothing was lost). The rebuild
  runs with foreign-key enforcement OFF (see :func:`migrate_db`) and is
  skipped when the table already allows ``'check'`` (idempotence guard).
- **5 -> 6** — gaia-crossmatch ticket 02: create the ``crossmatch`` table
  (the persisted Gaia cross-match table of a stack: one row per detected
  source, keyed by the ``frame_id`` FK with ``ON DELETE CASCADE``) plus its
  ``frame_id`` index. ``IF NOT EXISTS`` keeps the step idempotent, exactly
  like 3 -> 4.
- **6 -> 7** — astrometry ticket 01: create the ``frame_wcs`` table (the
  persisted solved WCS of a frame: ``frame_id`` INTEGER primary key with
  ``ON DELETE CASCADE``, ``solver``, ``solved_at``, nullable ``rms_arcsec``,
  ``n_stars``, ``center_ra_deg``/``center_dec_deg``, and the full WCS as a
  JSON ``cards_json``). ``IF NOT EXISTS`` keeps the step idempotent, exactly
  like 3 -> 4.

:func:`initialize_db` creates the current schema by running the migrations
on a fresh db (0 -> 1 -> ... -> :data:`SCHEMA_VERSION`).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from .errors import InvalidProjectError

#: The schema version this build writes and supports.
SCHEMA_VERSION = 7

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


#: The ``frames`` table DDL at v3 — identical to the 0 -> 1 DDL except the
#: ``kind`` CHECK also allows ``'enhanced'`` (image-enhance ticket 04). Used
#: by the 2 -> 3 migration to rebuild the table (SQLite cannot alter a CHECK
#: constraint in place).
_FRAMES_V3_DDL = """
CREATE TABLE frames__v3 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('sub', 'stack', 'enhanced')),
    path TEXT NOT NULL,
    jd REAL NOT NULL,
    utc TEXT NOT NULL,
    exposure REAL,
    gain REAL,
    target TEXT
)
"""

#: The ``presets`` table DDL (v3, image-enhance ticket 04): one enhancement
#: preset per frame — ``frame_id`` (TEXT primary key, the frames row id as a
#: string), ``pipeline`` (the pipeline as a JSON dict), ``updated_at`` (UTC
#: ISO-8601).
_PRESETS_DDL = """
CREATE TABLE IF NOT EXISTS presets (
    frame_id TEXT PRIMARY KEY NOT NULL,
    pipeline TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

#: The ``frame_quality`` table DDL (v4, frame-quality ticket 01): one quality
#: record per frame — ``frame_id`` (INTEGER primary key, the ``frames`` row
#: id, cascade-deleted with the frame), the metrics (``fwhm`` in pixels,
#: nullable when no star was detected; ``bg_median``/``bg_std`` from
#: sigma-clipped stats; ``star_count``), the outlier flag (``flagged`` plus
#: ``flag_reasons``, a JSON list of the metric names whose robust z-score
#: exceeded the session threshold), and ``scored_at`` (UTC ISO-8601).
_FRAME_QUALITY_DDL = """
CREATE TABLE IF NOT EXISTS frame_quality (
    frame_id INTEGER PRIMARY KEY NOT NULL REFERENCES frames (id) ON DELETE CASCADE,
    fwhm REAL,
    bg_median REAL NOT NULL,
    bg_std REAL NOT NULL,
    star_count INTEGER NOT NULL CHECK (star_count >= 0),
    flagged INTEGER NOT NULL DEFAULT 0 CHECK (flagged IN (0, 1)),
    flag_reasons TEXT NOT NULL DEFAULT '[]',
    scored_at TEXT NOT NULL
)
"""

#: The ``stars`` table DDL at v5 — identical to the 0 -> 1 DDL except the
#: ``role`` CHECK also allows ``'check'`` (check-star-plot ticket 01: the
#: check star is stored in the SAME role column). Used by the 4 -> 5
#: migration to rebuild the table (SQLite cannot alter a CHECK constraint in
#: place).
_STARS_V5_DDL = """
CREATE TABLE stars__v5 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL CHECK (role IN ('target', 'comp', 'check')),
    label TEXT,
    x REAL NOT NULL,
    y REAL NOT NULL,
    ra_deg REAL,
    dec_deg REAL,
    aperture_radius REAL NOT NULL,
    sky_annulus_inner REAL NOT NULL,
    sky_annulus_outer REAL NOT NULL
)
"""

#: The ``crossmatch`` table DDL (v6, gaia-crossmatch ticket 02): the persisted
#: Gaia cross-match table of ONE stack — one row per detected source,
#: keyed by ``frame_id`` (INTEGER, REFERENCES frames ON DELETE CASCADE, so the
#: table is cascade-deleted with its frame). Each row carries the detection's
#: pixel position (``x``/``y``) and J2000 sky position (``ra_deg``/``dec_deg``),
#: the ``matched`` flag, and the matched Gaia DR3 fields — ``source_id``,
#: ``g``/``bp``/``rp``, ``parallax`` + ``parallax_error`` (mas),
#: ``pmra``/``pmra_error``, ``pmdec``/``pmdec_error`` (mas/yr) — all nullable
#: and NULL for an unmatched detection, plus the match ``separation_arcsec``.
_CROSSMATCH_DDL = """
CREATE TABLE IF NOT EXISTS crossmatch (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    frame_id INTEGER NOT NULL REFERENCES frames (id) ON DELETE CASCADE,
    x REAL NOT NULL,
    y REAL NOT NULL,
    ra_deg REAL NOT NULL,
    dec_deg REAL NOT NULL,
    matched INTEGER NOT NULL DEFAULT 0 CHECK (matched IN (0, 1)),
    source_id INTEGER,
    g REAL,
    bp REAL,
    rp REAL,
    parallax REAL,
    parallax_error REAL,
    pmra REAL,
    pmra_error REAL,
    pmdec REAL,
    pmdec_error REAL,
    separation_arcsec REAL
)
"""

#: The ``crossmatch`` frame index (v6): every per-frame lookup (load, upsert
#: delete, cascade bookkeeping) goes through ``frame_id``.
_CROSSMATCH_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_crossmatch_frame_id ON crossmatch (frame_id)"
)

#: The ``frame_wcs`` table DDL (v7, astrometry ticket 01): the persisted solved
#: WCS of ONE frame — ``frame_id`` (INTEGER primary key, REFERENCES frames ON
#: DELETE CASCADE, so the record is cascade-deleted with its frame), the
#: ``solver`` that produced it (``solve-field`` or ``gaia``), ``solved_at``
#: (UTC ISO-8601), the match ``rms_arcsec`` (nullable — None when the solver
#: reported no RMS), ``n_stars`` (the matched star count), the solved field
#: center (``center_ra_deg``/``center_dec_deg``, J2000 degrees), and the full
#: WCS as a JSON dict of FITS cards (``cards_json``) so the exact WCS can be
#: rebuilt without the FITS file.
_FRAME_WCS_DDL = """
CREATE TABLE IF NOT EXISTS frame_wcs (
    frame_id INTEGER PRIMARY KEY NOT NULL REFERENCES frames (id) ON DELETE CASCADE,
    solver TEXT NOT NULL,
    solved_at TEXT NOT NULL,
    rms_arcsec REAL,
    n_stars INTEGER NOT NULL CHECK (n_stars >= 0),
    center_ra_deg REAL NOT NULL,
    center_dec_deg REAL NOT NULL,
    cards_json TEXT NOT NULL
)
"""

def _migrate_2_to_3(conn: sqlite3.Connection) -> None:
    """Migration step 2 -> 3 (image-enhance ticket 04): create the
    ``presets`` table and widen the ``frames.kind`` CHECK to allow
    ``'enhanced'``.

    The CHECK cannot be altered in place, so the ``frames`` table is rebuilt:
    create ``frames__v3`` with the widened CHECK, copy all rows, drop the old
    table, rename, and recreate the two indexes. This runs with foreign-key
    enforcement OFF (see :func:`migrate_db`) — with FK ON the ``DROP TABLE``
    would fire an implicit ``DELETE FROM frames`` and cascade-delete every
    ``measurements`` row. A ``PRAGMA foreign_key_check`` afterwards proves the
    rebuild lost nothing (a violation raises, rolling the migration back).

    The rebuild is skipped when the ``frames`` table already allows
    ``'enhanced'`` (idempotence guard — e.g. a re-run after a crash between
    the db bump and the project.json mirror write).
    """
    conn.execute(_PRESETS_DDL)
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='frames'"
    ).fetchone()
    if row is not None and "'enhanced'" not in (row[0] or ""):
        conn.execute(_FRAMES_V3_DDL)
        conn.execute(
            "INSERT INTO frames__v3 (id, session_id, kind, path, jd, utc, exposure, gain, target) "
            "SELECT id, session_id, kind, path, jd, utc, exposure, gain, target FROM frames"
        )
        conn.execute("DROP TABLE frames")
        conn.execute("ALTER TABLE frames__v3 RENAME TO frames")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_frames_session_id ON frames (session_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_frames_jd ON frames (jd)")
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise InvalidProjectError(
                f"2 -> 3 migration would break foreign keys: {violations!r}"
            )


def _migrate_4_to_5(conn: sqlite3.Connection) -> None:
    """Migration step 4 -> 5 (check-star-plot ticket 01): widen the
    ``stars.role`` CHECK to also allow ``'check'`` — the check star is stored
    in the SAME role column, so pre-v5 project databases would reject a
    check pick with ``CHECK constraint failed``.

    The CHECK cannot be altered in place, so the ``stars`` table is rebuilt
    exactly like the ``frames`` table in 2 -> 3: create ``stars__v5`` with the
    widened CHECK, copy all rows, drop the old table, rename, and recreate the
    index. This runs with foreign-key enforcement OFF (see
    :func:`migrate_db`); a ``PRAGMA foreign_key_check`` afterwards proves the
    rebuild lost nothing (a violation raises, rolling the migration back).

    The rebuild is skipped when the ``stars`` table already allows
    ``'check'`` (idempotence guard — e.g. a re-run after a crash between the
    db bump and the project.json mirror write).
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='stars'"
    ).fetchone()
    if row is not None and "'check'" not in (row[0] or ""):
        conn.execute(_STARS_V5_DDL)
        conn.execute(
            "INSERT INTO stars__v5 (id, role, label, x, y, ra_deg, dec_deg, "
            "aperture_radius, sky_annulus_inner, sky_annulus_outer) "
            "SELECT id, role, label, x, y, ra_deg, dec_deg, "
            "aperture_radius, sky_annulus_inner, sky_annulus_outer FROM stars"
        )
        conn.execute("DROP TABLE stars")
        conn.execute("ALTER TABLE stars__v5 RENAME TO stars")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_stars_role ON stars (role)")
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise InvalidProjectError(
                f"4 -> 5 migration would break foreign keys: {violations!r}"
            )


#: The migrations, keyed by the version they migrate FROM: applying
#: ``MIGRATIONS[v]`` to a db at ``user_version == v`` brings it to ``v + 1``.
#: :func:`migrate_db` walks the keys in ascending order up to
#: :data:`SCHEMA_VERSION`.
MIGRATIONS: dict[int, tuple[MigrationStep, ...]] = {
    0: tuple(SCHEMA_STATEMENTS),  # empty db -> v1: create the schema
    1: (_add_stars_aperture_columns,),  # dev-build v1 -> v2: aperture columns
    2: (_migrate_2_to_3,),  # v2 -> v3: presets table + frames.kind 'enhanced'
    3: (_FRAME_QUALITY_DDL,),  # v3 -> v4: frame_quality table (ticket 01)
    4: (_migrate_4_to_5,),  # v4 -> v5: stars.role CHECK allows 'check'
    5: (_CROSSMATCH_DDL, _CROSSMATCH_INDEX_DDL),  # v5 -> v6: crossmatch table
    6: (_FRAME_WCS_DDL,),  # v6 -> v7: frame_wcs table (astrometry ticket 01)
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

    When migrations are pending, foreign-key enforcement is switched OFF for
    their duration and restored afterwards (both pragmas run OUTSIDE any
    transaction — the pragma is a no-op inside one). This is required by the
    2 -> 3 ``frames`` table rebuild: with FK ON, ``DROP TABLE frames`` would
    cascade-delete the ``measurements`` rows. A db already at
    :data:`SCHEMA_VERSION` is not touched at all (no pragma, no transaction).

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
    # FK enforcement must be off while migrations run (the 2 -> 3 frames
    # rebuild drops a parent table); only touch the pragma when there is
    # actually something to migrate, and always restore the previous state.
    fk_was_on = False
    if version < SCHEMA_VERSION:
        fk_was_on = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
        if fk_was_on:
            conn.execute("PRAGMA foreign_keys=OFF")
    try:
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
    finally:
        if fk_was_on:
            conn.execute("PRAGMA foreign_keys=ON")
    return version
