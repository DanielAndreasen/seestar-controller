"""Project store — on-disk observing campaigns for the Seestar Controller.

Pure Python, no GTK, no link (module boundary per
``.scratch/project-store/spec.md``).

- :mod:`.errors` — the error hierarchy: :class:`~.errors.ProjectError` base,
  with :class:`~.errors.ProjectExistsError`,
  :class:`~.errors.ProjectNotFoundError`, :class:`~.errors.InvalidProjectError`,
  and the session errors :class:`~.errors.SessionOpenError`,
  :class:`~.errors.SessionNotFoundError`,
  :class:`~.errors.SessionAlreadyClosedError`
- :mod:`.schema` — the ``db.sqlite`` schema (sessions, frames, stars,
  measurements) and :data:`~.schema.SCHEMA_VERSION`, initialized in one
  transaction with WAL journal mode
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

Module boundary (see ``.scratch/project-store/spec.md``): nothing in this
package imports GTK or :mod:`seestar_controller.link`.
"""

from .errors import (
    InvalidProjectError,
    ProjectError,
    ProjectExistsError,
    ProjectNotFoundError,
    SessionAlreadyClosedError,
    SessionNotFoundError,
    SessionOpenError,
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
from .schema import SCHEMA_TABLES, SCHEMA_VERSION, initialize_db
from .sessions import (
    Session,
    close_session,
    get_open_session,
    list_sessions,
    start_session,
)

__all__ = [
    "DB_FILE",
    "DEFAULT_PROJECT_ROOT",
    "MAX_SLUG_LENGTH",
    "PROJECT_FILE",
    "PROJECT_ROOT_ENV_VAR",
    "PROJECT_SUBDIRS",
    "SCHEMA_TABLES",
    "SCHEMA_VERSION",
    "STACKS_DIR",
    "SUBS_DIR",
    "InvalidProjectError",
    "Project",
    "ProjectError",
    "ProjectExistsError",
    "ProjectNotFoundError",
    "ProjectSummary",
    "Session",
    "SessionAlreadyClosedError",
    "SessionNotFoundError",
    "SessionOpenError",
    "close_session",
    "create_project",
    "default_project_root",
    "get_open_session",
    "initialize_db",
    "list_projects",
    "list_sessions",
    "open_project",
    "slugify",
    "start_session",
    "utc_now_iso",
]
