"""Error hierarchy of the project store.

Pure-Python (module boundary per ``.scratch/project-store/spec.md``): no GTK
imports, no :mod:`seestar_controller.link` imports, stdlib only.
"""


class ProjectError(Exception):
    """Base class for all project-store failures."""


class ProjectExistsError(ProjectError):
    """A project already exists at the target path (slug collision)."""


class ProjectNotFoundError(ProjectError):
    """No project exists at the given path."""


class InvalidProjectError(ProjectError):
    """The path looks like a project but is invalid, corrupt, or unsupported
    (unparseable ``project.json``, missing/corrupt ``db.sqlite``, unsupported
    ``schema_version``)."""


class SessionOpenError(ProjectError):
    """A session is already open in the project; starting another one while
    one is open is rejected (see :mod:`.sessions`)."""


class SessionNotFoundError(ProjectError):
    """No session with the given id exists in the project."""


class SessionAlreadyClosedError(ProjectError):
    """The session is already closed (``ended_at`` is set); closing it again
    is rejected."""
