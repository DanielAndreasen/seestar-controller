"""Project store — on-disk observing campaigns for the Seestar Controller.

Pure Python, no GTK, no link (module boundary per
``.scratch/project-store/spec.md``).

- :mod:`.errors` — the error hierarchy: :class:`~.errors.ProjectError` base,
  with :class:`~.errors.ProjectExistsError`,
  :class:`~.errors.ProjectNotFoundError`, :class:`~.errors.InvalidProjectError`,
  :class:`~.errors.ProjectOpenError`, and the session errors
  :class:`~.errors.SessionOpenError`,
  :class:`~.errors.SessionNotFoundError`,
  :class:`~.errors.SessionAlreadyClosedError`, plus
  :class:`~.errors.FrameStoreError`
- :mod:`.schema` — the ``db.sqlite`` schema (sessions, frames, stars,
  measurements), :data:`~.schema.SCHEMA_VERSION`, :func:`~.schema.initialize_db`
  (WAL mode, one transaction per migration), and the migration runner
  :func:`~.schema.migrate_db` with :data:`~.schema.MIGRATIONS` (from-version ->
  SQL steps; ``open_project`` migrates older dbs and rejects newer ones)
- :mod:`.project` — create/open/list/delete: :func:`~.project.create_project`,
  :func:`~.project.open_project`, :func:`~.project.list_projects`,
  :func:`~.project.delete_project` (removes the whole project directory;
  refuses with :class:`~.errors.ProjectOpenError` while the project is open),
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
  :func:`~.stars.load_star_picks` (target first, then the check star, then
  comps by label), the :class:`~.stars.StarPick` record (role/label/x/y/
  ra_deg/dec_deg + per-pick aperture radius and sky annulus), the role
  constants :data:`~.stars.ROLE_TARGET` / :data:`~.stars.ROLE_COMP` /
  :data:`~.stars.ROLE_CHECK` / :data:`~.stars.ROLES`,
  :func:`~.stars.normalize_role`, and :class:`~.errors.StarPickError`
- :mod:`.measurements` — measurements + lightcurve query:
  :func:`~.measurements.add_measurement` (upsert one point for a frame —
  re-adding a frame replaces its previous row; jd defaults to the frame's
  jd), :func:`~.measurements.lightcurve` (all measurements across all
  sessions ordered by jd, at most one — the newest — row per frame, excluded
  filtered unless ``include_excluded=True``),
  :func:`~.measurements.set_excluded` (flag/unflag an outlier),
  :func:`~.measurements.measurements_for_frame` (one frame's newest point),
  :func:`~.measurements.clear_session_measurements` (delete a whole session's
  measurement rows — the re-run reset), the :class:`~.measurements.Measurement`
  record (frame FK + joined session_id/utc, jd, mag, mag_err, excluded), and
  :class:`~.errors.MeasurementError`
- :mod:`.presets` — per-image enhancement presets (image-enhance ticket 04):
  :func:`~.presets.save_preset` (upsert one frame's pipeline JSON in one db
  transaction), :func:`~.presets.load_preset` (None when the frame has none),
  :func:`~.presets.delete_preset`, :func:`~.presets.list_presets`, the
  :class:`~.presets.Preset` record (frame_id, pipeline dict, updated_at), and
  :class:`~.errors.PresetError`
- :mod:`.quality` — per-frame quality records (frame-quality ticket 01):
  :func:`~.quality.save_frame_quality` (upsert one frame's metrics + outlier
  flag in one db transaction), :func:`~.quality.frame_quality_for_frame`
  (None when the frame has none), :func:`~.quality.session_quality` (one
  session's quality table joined with its frames, ordered by ``jd``, as a
  :class:`~.quality.SessionQuality` with the summary: median FWHM,
  % rejected, ...), the :class:`~.quality.FrameQuality` record (frame FK +
  joined session_id, fwhm, bg_median, bg_std, star_count, flagged,
  flag_reasons, scored_at), and :class:`~.errors.QualityError`
- :mod:`.crossmatch` — persisted Gaia cross-match tables (gaia-crossmatch
  ticket 02): :func:`~.crossmatch.save_crossmatch` (validated upsert of one
  frame's whole table in one db transaction, duck-typed rows),
  :func:`~.crossmatch.load_crossmatch` (one frame's table, empty tuple when
  it has none), :func:`~.crossmatch.list_crossmatched_stacks` (the frames
  that have a table, as :class:`~.crossmatch.CrossmatchedStack` summaries),
  the :class:`~.crossmatch.CrossmatchRecord` row (frame FK, detected x/y +
  RA/Dec, matched flag, Gaia source_id, G/BP/RP, parallax + error,
  pmra/pmdec + errors, separation), and :class:`~.errors.CrossmatchError`
- :mod:`.wcs` — persisted solved WCS per frame (astrometry ticket 01):
  :func:`~.wcs.save_frame_wcs` (validated upsert of one frame's WCS record in
  one db transaction), :func:`~.wcs.load_frame_wcs` (None when the frame has
  none), :func:`~.wcs.list_solved_frames` (the solved frames, optionally of
  one session, ordered by ``jd``), the :class:`~.wcs.FrameWcs` record (frame
  FK + joined session_id, solver, solved_at, rms_arcsec, n_stars, center
  RA/Dec, the WCS as a dict of FITS cards, :meth:`~.wcs.FrameWcs.to_wcs`),
  and :class:`~.errors.WcsStoreError`
- :mod:`.isochrone` — the project's persisted isochrone overlay parameters
  (hr-diagram ticket 02): :func:`~.isochrone.set_isochrone_params` (validated
  upsert of the singleton project-level row in one db transaction),
  :func:`~.isochrone.isochrone_params` (the saved record, None when never
  set), the :class:`~.isochrone.IsochroneParams` record (age = log10(age/yr),
  feh, dist_mod, updated_at), and :class:`~.errors.IsochroneStoreError`
- :mod:`.csv_export` — CSV export: :func:`~.csv_export.export_csv` (atomic
  write of ``lightcurve.csv`` with header :data:`~.csv_export.CSV_HEADER`,
  excluded points omitted unless ``include_excluded=True``), the default
  file name :data:`~.csv_export.CSV_FILE`, and the header columns
  :data:`~.csv_export.CSV_HEADER`

Module boundary (see ``.scratch/project-store/spec.md``): nothing in this
package imports GTK or :mod:`seestar_controller.link`.
"""

from .crossmatch import (
    CrossmatchedStack,
    CrossmatchRecord,
    list_crossmatched_stacks,
    load_crossmatch,
    save_crossmatch,
)
from .csv_export import CSV_FILE, CSV_HEADER, export_csv
from .errors import (
    CrossmatchError,
    FrameStoreError,
    InvalidProjectError,
    IsochroneStoreError,
    MeasurementError,
    PresetError,
    ProjectError,
    ProjectExistsError,
    ProjectNotFoundError,
    ProjectOpenError,
    QualityError,
    SessionAlreadyClosedError,
    SessionNotFoundError,
    SessionOpenError,
    StarPickError,
    WcsStoreError,
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
from .isochrone import IsochroneParams, isochrone_params, set_isochrone_params
from .measurements import (
    Measurement,
    add_measurement,
    clear_session_measurements,
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
    delete_project,
    list_projects,
    open_project,
    slugify,
    utc_now_iso,
)
from .quality import (
    FrameQuality,
    SessionQuality,
    frame_quality_for_frame,
    save_frame_quality,
    session_quality,
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
    ROLE_CHECK,
    ROLE_COMP,
    ROLE_TARGET,
    ROLES,
    STAR_PICK_FIELDS,
    StarPick,
    load_star_picks,
    normalize_role,
    save_star_picks,
)
from .wcs import FrameWcs, list_solved_frames, load_frame_wcs, save_frame_wcs

__all__ = [
    "CSV_FILE",
    "CSV_HEADER",
    "DB_FILE",
    "DEFAULT_PROJECT_ROOT",
    "ENHANCED_DIR",
    "FITS_SUFFIX",
    "KINDS",
    "KIND_DIRS",
    "KIND_ENHANCED",
    "KIND_STACK",
    "KIND_SUB",
    "MAX_SLUG_LENGTH",
    "MIGRATIONS",
    "PROJECT_FILE",
    "PROJECT_ROOT_ENV_VAR",
    "PROJECT_SUBDIRS",
    "ROLES",
    "ROLE_CHECK",
    "ROLE_COMP",
    "ROLE_TARGET",
    "SCHEMA_TABLES",
    "SCHEMA_VERSION",
    "STACKS_DIR",
    "STAR_PICK_FIELDS",
    "SUBS_DIR",
    "CrossmatchError",
    "CrossmatchRecord",
    "CrossmatchedStack",
    "Frame",
    "FrameQuality",
    "FrameStoreError",
    "FrameWcs",
    "InvalidProjectError",
    "IsochroneParams",
    "IsochroneStoreError",
    "Measurement",
    "MeasurementError",
    "Preset",
    "PresetError",
    "Project",
    "ProjectError",
    "ProjectExistsError",
    "ProjectNotFoundError",
    "ProjectOpenError",
    "ProjectSummary",
    "QualityError",
    "Session",
    "SessionAlreadyClosedError",
    "SessionNotFoundError",
    "SessionOpenError",
    "SessionQuality",
    "StarPick",
    "StarPickError",
    "WcsStoreError",
    "add_measurement",
    "clear_session_measurements",
    "close_session",
    "create_project",
    "default_project_root",
    "delete_preset",
    "delete_project",
    "export_csv",
    "frame_quality_for_frame",
    "get_frame_path",
    "get_open_session",
    "initialize_db",
    "isochrone_params",
    "lightcurve",
    "list_crossmatched_stacks",
    "list_frames",
    "list_presets",
    "list_projects",
    "list_sessions",
    "list_solved_frames",
    "load_crossmatch",
    "load_frame_wcs",
    "load_preset",
    "load_star_picks",
    "measurements_for_frame",
    "migrate_db",
    "normalize_kind",
    "normalize_role",
    "open_project",
    "read_frame_bytes",
    "save_crossmatch",
    "save_frame_quality",
    "save_frame_wcs",
    "save_preset",
    "save_star_picks",
    "session_quality",
    "set_excluded",
    "set_isochrone_params",
    "slugify",
    "start_session",
    "store_frame",
    "utc_now_iso",
]
