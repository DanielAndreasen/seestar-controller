"""Project store — on-disk observing campaigns for the Seestar Controller.

Pure Python, no GTK, no link (module boundary per
``.scratch/project-store/spec.md``).

- :mod:`.errors` — the error hierarchy: :class:`~.errors.ProjectError` base,
  with :class:`~.errors.ProjectExistsError`,
  :class:`~.errors.ProjectNotFoundError`, :class:`~.errors.InvalidProjectError`,
  and the session errors :class:`~.errors.SessionOpenError`,
  :class:`~.errors.SessionNotFoundError`,
  :class:`~.errors.SessionAlreadyClosedError`, plus
  :class:`~.errors.FrameStoreError`
- :mod:`.schema` — the ``db.sqlite`` schema (sessions, frames, stars,
  measurements), :data:`~.schema.SCHEMA_VERSION`, :func:`~.schema.initialize_db`
  (WAL mode, one transaction per migration), and the migration runner
  :func:`~.schema.migrate_db` with :data:`~.schema.MIGRATIONS` (from-version ->
  SQL steps; ``open_project`` migrates older dbs and rejects newer ones)
- :mod:`.project` — create/open/list: :func:`~.project.create_project`,
  :func:`~.project.open_project`, :func:`~.project.list_projects`,
  :func:`~.project.slugify`, the :class:`~.project.Project` handle (``.path``,
  ``.name``, ``.config``, ``.db``, atomic ``save_config()``) and
  :class:`~.project.ProjectSummary`, plus the project-root resolution
  (``~/SeestarProjects/`` default, ``SEESTAR_PROJECT_ROOT`` override)
- :mod:`.sessions` — the session lifecycle: :func:`~.sessions.start_session`,
  :func:`~.sessions.close_session`, :func:`~.sessions.list_sessions`,
  :func:`~.sessions.get_open_session`, the :class:`~.sessions.Session` record,
  and the session errors :class:`~.errors.SessionOpenError`,
  :class:`~.errors.SessionNotFoundError`,
  :class:`~.errors.SessionAlreadyClosedError`
- :mod:`.frames` — frame storage: :func:`~.frames.store_frame` (atomic FITS
  write into ``subs/<session>/`` / ``stacks/<session>/`` /
  ``enhanced/<session>/`` + the ``frames`` db row), :func:`~.frames.list_frames`,
  :func:`~.frames.get_frame_path`, :func:`~.frames.read_frame_bytes`, the
  :class:`~.frames.Frame` record, the kind constants :data:`~.frames.KIND_SUB` /
  :data:`~.frames.KIND_STACK` / :data:`~.frames.KIND_ENHANCED`,
  :data:`~.frames.KINDS`, :data:`~.frames.KIND_DIRS` (kind -> directory),
  :data:`~.frames.ENHANCED_DIR`, and :class:`~.errors.FrameStoreError`
- :mod:`.stars` — star picks: :func:`~.stars.save_star_picks` (replace all
  picks in one db transaction + mirror into ``project.json``),
  :func:`~.stars.load_star_picks` (target first, then comps by label), the
  :class:`~.stars.StarPick` record (role/label/x/y/ra_deg/dec_deg + per-pick
  aperture radius and sky annulus), the role constants
  :data:`~.stars.ROLE_TARGET` / :data:`~.stars.ROLE_COMP` /
  :data:`~.stars.ROLES`, :func:`~.stars.normalize_role`, and
  :class:`~.errors.StarPickError`
- :mod:`.measurements` — measurements + lightcurve query:
  :func:`~.measurements.add_measurement` (append one point for a frame, jd
  defaults to the frame's jd), :func:`~.measurements.lightcurve` (all
  measurements across all sessions ordered by jd, excluded filtered unless
  ``include_excluded=True``), :func:`~.measurements.set_excluded` (flag/unflag
  an outlier), :func:`~.measurements.measurements_for_frame` (one frame's
  points), the :class:`~.measurements.Measurement` record (frame FK +
  joined session_id/utc, jd, mag, mag_err, excluded), and
  :class:`~.errors.MeasurementError`
- :mod:`.presets` — per-image enhancement presets (image-enhance ticket 04):
  :func:`~.presets.save_preset` (upsert one frame's pipeline JSON in one db
  transaction), :func:`~.presets.load_preset` (None when the frame has none),
  :func:`~.presets.delete_preset`, :func:`~.presets.list_presets`, the
  :class:`~.presets.Preset` record (frame_id, pipeline dict, updated_at), and
  :class:`~.errors.PresetError`
- :mod:`.csv_export` — CSV export: :func:`~.csv_export.export_csv` (atomic
  write of ``lightcurve.csv`` with header :data:`~.csv_export.CSV_HEADER`,
  excluded points omitted unless ``include_excluded=True``), the default
  file name :data:`~.csv_export.CSV_FILE`, and the header columns
  :data:`~.csv_export.CSV_HEADER`

Module boundary (see ``.scratch/project-store/spec.md``): nothing in this
package imports GTK or :mod:`seestar_controller.link`.
"""

from .csv_export import CSV_FILE, CSV_HEADER, export_csv
from .errors import (
    FrameStoreError,
    InvalidProjectError,
    MeasurementError,
    PresetError,
    ProjectError,
    ProjectExistsError,
    ProjectNotFoundError,
    SessionAlreadyClosedError,
    SessionNotFoundError,
    SessionOpenError,
    StarPickError,
)
from .frames import (
    ENHANCED_DIR,
    FITS_SUFFIX,
    KIND_DIRS,
    KIND_ENHANCED,
    KIND_STACK,
    KIND_SUB,
    KINDS,
    Frame,
    get_frame_path,
    list_frames,
    normalize_kind,
    read_frame_bytes,
    store_frame,
)
from .measurements import (
    Measurement,
    add_measurement,
    lightcurve,
    measurements_for_frame,
    set_excluded,
)
from .presets import (
    Preset,
    delete_preset,
    list_presets,
    load_preset,
    save_preset,
)
from .project import (
    DB_FILE,
    DEFAULT_PROJECT_ROOT,
    MAX_SLUG_LENGTH,
    PROJECT_FILE,
    PROJECT_ROOT_ENV_VAR,
    PROJECT_SUBDIRS,
    STACKS_DIR,
    SUBS_DIR,
    Project,
    ProjectSummary,
    create_project,
    default_project_root,
    list_projects,
    open_project,
    slugify,
    utc_now_iso,
)
from .schema import MIGRATIONS, SCHEMA_TABLES, SCHEMA_VERSION, initialize_db, migrate_db
from .sessions import (
    Session,
    close_session,
    get_open_session,
    list_sessions,
    start_session,
)
from .stars import (
    ROLE_COMP,
    ROLE_TARGET,
    ROLES,
    STAR_PICK_FIELDS,
    StarPick,
    load_star_picks,
    normalize_role,
    save_star_picks,
)

__all__ = [
    "CSV_FILE",
    "CSV_HEADER",
    "DB_FILE",
    "DEFAULT_PROJECT_ROOT",
    "ENHANCED_DIR",
    "FITS_SUFFIX",
    "KIND_DIRS",
    "KIND_ENHANCED",
    "KIND_STACK",
    "KIND_SUB",
    "KINDS",
    "MAX_SLUG_LENGTH",
    "MIGRATIONS",
    "PROJECT_FILE",
    "PROJECT_ROOT_ENV_VAR",
    "PROJECT_SUBDIRS",
    "ROLE_COMP",
    "ROLE_TARGET",
    "ROLES",
    "SCHEMA_TABLES",
    "SCHEMA_VERSION",
    "STACKS_DIR",
    "STAR_PICK_FIELDS",
    "SUBS_DIR",
    "Frame",
    "FrameStoreError",
    "InvalidProjectError",
    "Measurement",
    "MeasurementError",
    "Preset",
    "PresetError",
    "Project",
    "ProjectError",
    "ProjectExistsError",
    "ProjectNotFoundError",
    "ProjectSummary",
    "Session",
    "SessionAlreadyClosedError",
    "SessionNotFoundError",
    "SessionOpenError",
    "StarPick",
    "StarPickError",
    "add_measurement",
    "close_session",
    "create_project",
    "default_project_root",
    "delete_preset",
    "export_csv",
    "get_frame_path",
    "get_open_session",
    "initialize_db",
    "lightcurve",
    "list_frames",
    "list_presets",
    "list_projects",
    "list_sessions",
    "load_preset",
    "load_star_picks",
    "measurements_for_frame",
    "migrate_db",
    "normalize_kind",
    "normalize_role",
    "open_project",
    "read_frame_bytes",
    "save_preset",
    "save_star_picks",
    "set_excluded",
    "slugify",
    "start_session",
    "store_frame",
    "utc_now_iso",
]
