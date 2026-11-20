"""Measurements + lightcurve query — the photometry data points of a project.

Pure-Python (module boundary per ``.scratch/project-store/spec.md``): no GTK
imports, no :mod:`seestar_controller.link` imports, stdlib only.

A **Measurement** (GLOSSARY.md) is one differential-magnitude data point
(target minus comp ensemble) at a point in time; the **Lightcurve** is the
series of measurements over time, across ALL sessions of a project. Each
measurement belongs to one stored frame (the ``frames`` FK) and lives in the
``measurements`` table of the project db (see :mod:`.schema`): ``frame_id``,
``jd``, ``mag``, ``mag_err``, and the ``excluded`` outlier flag.

API (all take the open :class:`~.project.Project` handle first):

- :func:`add_measurement` — append one measurement for a frame, returns the
  :class:`Measurement`;
- :func:`lightcurve` — all measurements across all sessions, ordered by
  ``jd``, excluded points filtered unless ``include_excluded=True``;
- :func:`set_excluded` — flag/unflag an outlier point, returns the updated
  :class:`Measurement`;
- :func:`measurements_for_frame` — the measurements of one frame, ordered by
  ``jd``.

Documented choices:

- **Joined fields.** :class:`Measurement` carries ``session_id`` and ``utc``
  joined from the frame (``JOIN frames``), so a lightcurve point knows which
  night and which instant it belongs to without a second query. ``jd`` comes
  from the measurement row itself (it defaults to the frame's ``jd`` but may
  be overridden, e.g. for a mid-exposure correction); ``utc`` always comes
  from the frame.
- **``jd`` default.** :func:`add_measurement` with ``jd=None`` uses the
  frame's ``jd`` (the observation time recorded when the frame was stored).
- **Unknown frame.** The frame FK must exist: :func:`add_measurement` checks
  it inside the same transaction as the insert and raises
  :class:`~.errors.MeasurementError` naming the frame id — a clear error
  instead of the raw FK :class:`sqlite3.IntegrityError` — and nothing is
  written.
- **Validation** (all raise :class:`~.errors.MeasurementError` with the field
  and value in the message, before anything is written): ``mag`` must be a
  finite number (NaN/inf rejected); ``mag_err`` is None or a finite number
  ``>= 0``; an explicit ``jd`` must be finite. Structural type errors
  (``frame_id`` not an int, ``excluded`` not a bool) raise :class:`TypeError`.
- **Ordering.** :func:`lightcurve` and :func:`measurements_for_frame` order by
  ``jd`` (ties broken by the row ``id`` = insertion order), across ALL
  sessions — nights accumulate into one merged curve.
- **Exclusion.** ``excluded`` points are filtered out of :func:`lightcurve`
  unless ``include_excluded=True``; :func:`set_excluded` flags/unflags in one
  transaction and raises :class:`~.errors.MeasurementError` for an unknown
  measurement id. :func:`measurements_for_frame` with an unknown frame id is
  a filter, not an error — it returns ``[]`` (same semantics as
  :func:`.frames.list_frames` with an unknown session).
- **Writes** — every mutation runs in one transaction (``with project.db:``),
  so a rejected append/update leaves no partial row behind.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from typing import Any

from .errors import MeasurementError
from .project import Project

#: Columns selected for a :class:`Measurement` (the join of ``measurements``
#: with ``frames``), in :class:`Measurement` field order after ``id``.
_MEASUREMENT_COLUMNS = (
    "m.id, m.frame_id, f.session_id, m.jd, f.utc, m.mag, m.mag_err, m.excluded"
)

#: The base SELECT joining measurements with their frames.
_SELECT_SQL = (
    f"SELECT {_MEASUREMENT_COLUMNS} FROM measurements m "
    "JOIN frames f ON f.id = m.frame_id"
)


@dataclass(frozen=True)
class Measurement:
    """One differential-magnitude data point (GLOSSARY.md) of a project.

    - ``id`` — the db row id (INTEGER PRIMARY KEY);
    - ``frame_id`` — the frame FK (the sub-frame this point was measured on);
    - ``session_id`` — the frame's session, joined from ``frames`` (the night);
    - ``jd`` — the time of the point as Julian Date (defaults to the frame's
      ``jd``, may be overridden);
    - ``utc`` — the frame's observation time as a UTC ISO-8601 string
      (``+00:00``), joined from ``frames``;
    - ``mag`` — the differential magnitude (target minus comp ensemble);
    - ``mag_err`` — the magnitude uncertainty, or None when unknown;
    - ``excluded`` — the outlier flag (excluded points are filtered out of
      :func:`lightcurve` unless ``include_excluded=True``).
    """

    id: int
    frame_id: int
    session_id: str
    jd: float
    utc: str
    mag: float
    mag_err: float | None
    excluded: bool


def _row_to_measurement(row: sqlite3.Row | tuple[Any, ...]) -> Measurement:
    return Measurement(
        id=int(row[0]),
        frame_id=int(row[1]),
        session_id=str(row[2]),
        jd=float(row[3]),
        utc=str(row[4]),
        mag=float(row[5]),
        mag_err=None if row[6] is None else float(row[6]),
        excluded=bool(row[7]),
    )


def _check_id(value: Any, *, what: str) -> int:
    """Validate a db row id argument (an int, bools rejected);
    :class:`TypeError` otherwise."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{what} must be an int, got {value!r}")
    return value


def _check_bool(value: Any, *, what: str) -> bool:
    """Validate a flag argument (a bool); :class:`TypeError` otherwise."""
    if not isinstance(value, bool):
        raise TypeError(f"{what} must be a bool, got {value!r}")
    return value


def _finite_float(value: Any, *, what: str) -> float:
    """Coerce ``value`` to a finite float (bools rejected); raise
    :class:`MeasurementError` with a clear message otherwise."""
    if isinstance(value, bool):
        raise MeasurementError(f"{what} must be a number, got {value!r}")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise MeasurementError(f"{what} must be a number, got {value!r}") from None
    if not math.isfinite(result):
        raise MeasurementError(f"{what} must be finite, got {value!r}")
    return result


def _mag_err(value: Any) -> float | None:
    """Validate ``mag_err``: None stays None, else a finite float ``>= 0``."""
    if value is None:
        return None
    result = _finite_float(value, what="mag_err")
    if result < 0.0:
        raise MeasurementError(f"mag_err must be >= 0, got {value!r}")
    return result


def add_measurement(
    project: Project,
    frame_id: int,
    mag: float,
    jd: float | None = None,
    mag_err: float | None = None,
    excluded: bool = False,
) -> Measurement:
    """Append one measurement for the frame ``frame_id`` in ``project`` and
    return it as a :class:`Measurement`.

    - ``frame_id`` — the frame the point was measured on; must exist, else
      :class:`~.errors.MeasurementError` is raised (before anything is
      written);
    - ``mag`` — the differential magnitude; must be a finite number
      (NaN/inf rejected);
    - ``jd`` — the time of the point as Julian Date; None (default) uses the
      frame's ``jd``; an explicit value must be finite;
    - ``mag_err`` — the magnitude uncertainty; None (unknown) or a finite
      number ``>= 0``;
    - ``excluded`` — the outlier flag (default False).

    The frame-exists check and the insert run in ONE transaction
    (``with project.db:``), so a rejected append leaves no partial row. The
    returned record carries ``session_id``/``utc`` joined from the frame.
    """
    frame_id = _check_id(frame_id, what="frame_id")
    excluded = _check_bool(excluded, what="excluded")
    mag_value = _finite_float(mag, what="mag")
    mag_err_value = _mag_err(mag_err)
    jd_value = None if jd is None else _finite_float(jd, what="jd")
    with project.db:  # one transaction: the frame check and the insert together
        frame_row = project.db.execute(
            "SELECT id, session_id, jd, utc FROM frames WHERE id = ?", (frame_id,)
        ).fetchone()
        if frame_row is None:
            raise MeasurementError(
                f"project {project.slug!r} has no frame {frame_id!r} — "
                "measurements need an existing frame (see store_frame)"
            )
        if jd_value is None:
            jd_value = float(frame_row[2])  # default: the frame's jd
        cur = project.db.execute(
            "INSERT INTO measurements (frame_id, jd, mag, mag_err, excluded) "
            "VALUES (?, ?, ?, ?, ?)",
            (frame_id, jd_value, mag_value, mag_err_value, int(excluded)),
        )
        measurement_id = int(cur.lastrowid)  # ty: ignore[invalid-argument-type]
    return Measurement(
        id=measurement_id,
        frame_id=frame_id,
        session_id=str(frame_row[1]),
        jd=jd_value,
        utc=str(frame_row[3]),
        mag=mag_value,
        mag_err=mag_err_value,
        excluded=excluded,
    )


def lightcurve(project: Project, include_excluded: bool = False) -> list[Measurement]:
    """The lightcurve of ``project``: all measurements across ALL sessions as
    :class:`Measurement` records, ordered by ``jd`` (ties broken by ``id``).

    - ``include_excluded`` — when False (default), points flagged
      ``excluded`` are filtered out; when True, they are included.

    ``session_id`` and ``utc`` are joined from each point's frame. An empty
    project yields ``[]``.
    """
    include_excluded = _check_bool(include_excluded, what="include_excluded")
    query = _SELECT_SQL
    if not include_excluded:
        query += " WHERE m.excluded = 0"
    query += " ORDER BY m.jd, m.id"
    rows = project.db.execute(query).fetchall()
    return [_row_to_measurement(row) for row in rows]


def set_excluded(project: Project, measurement_id: int, excluded: bool) -> Measurement:
    """Flag (``excluded=True``) or unflag (``excluded=False``) the outlier
    point ``measurement_id`` in ``project`` and return the updated
    :class:`Measurement`.

    The existence check and the update run in ONE transaction
    (``with project.db:``). Raises :class:`~.errors.MeasurementError` when no
    measurement with that id exists.
    """
    measurement_id = _check_id(measurement_id, what="measurement_id")
    excluded = _check_bool(excluded, what="excluded")
    with project.db:  # one transaction: the check and the update together
        row = project.db.execute(
            f"{_SELECT_SQL} WHERE m.id = ?", (measurement_id,)
        ).fetchone()
        if row is None:
            raise MeasurementError(
                f"project {project.slug!r} has no measurement {measurement_id!r}"
            )
        project.db.execute(
            "UPDATE measurements SET excluded = ? WHERE id = ?",
            (int(excluded), measurement_id),
        )
        updated = project.db.execute(
            f"{_SELECT_SQL} WHERE m.id = ?", (measurement_id,)
        ).fetchone()
    return _row_to_measurement(updated)


def measurements_for_frame(project: Project, frame_id: int) -> list[Measurement]:
    """The measurements of the frame ``frame_id`` in ``project`` as
    :class:`Measurement` records, ordered by ``jd`` (ties broken by ``id``).

    An unknown frame id is a filter, not an error — it yields ``[]`` (same
    semantics as :func:`.frames.list_frames` with an unknown session).
    """
    frame_id = _check_id(frame_id, what="frame_id")
    rows = project.db.execute(
        f"{_SELECT_SQL} WHERE m.frame_id = ? ORDER BY m.jd, m.id", (frame_id,)
    ).fetchall()
    return [_row_to_measurement(row) for row in rows]
