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


class FrameStoreError(ProjectError):
    """Storing or reading a frame failed: the bytes are not a readable FITS
    file, the ``frames`` db row could not be inserted, the frame file is
    missing on disk, or the stored path is unsafe (see :mod:`.frames`)."""


class StarPickError(ProjectError, ValueError):
    """Invalid star picks (see :mod:`.stars`): a bad role/label/pixel
    coordinate/sky coordinate/aperture value on a :class:`~.stars.StarPick`,
    or a pick ensemble that does not contain exactly one target pick.

    Subclasses both :class:`ProjectError` and :class:`ValueError` — pick
    validation failures are value errors, so callers may catch either.
    """


class MeasurementError(ProjectError):
    """A measurement could not be appended or updated (see
    :mod:`.measurements`): the frame FK does not exist, the measurement id is
    unknown, or a ``jd``/``mag``/``mag_err`` value is invalid (non-numeric,
    non-finite, or a negative ``mag_err``)."""
