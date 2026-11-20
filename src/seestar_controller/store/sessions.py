"""Session lifecycle — one night's observing run within a project.

Pure-Python (module boundary per ``.scratch/project-store/spec.md``): no GTK
imports, no :mod:`seestar_controller.link` imports, stdlib only.

A **Session** (GLOSSARY.md) is one night's observing run inside a
:class:`~.project.Project`. Sessions live in the ``sessions`` table of the
project db (see :mod:`.schema`): ``id`` TEXT PRIMARY KEY, ``started_at`` /
``ended_at`` UTC ISO-8601 strings, ``ended_at`` NULL while the session is
open.

API (all take the open :class:`~.project.Project` handle first):

- :func:`start_session` — open a new session, returns its id;
- :func:`close_session` — record the end of a session, returns the
  :class:`Session`;
- :func:`list_sessions` — all sessions ordered by ``started_at``;
- :func:`get_open_session` — the session with ``ended_at IS NULL``, or None.

Documented choices:

- **Session id** — a UTC ISO-8601 *basic-format* timestamp slug derived from
  the start time: ``YYYYMMDDTHHMMSSffffffZ`` (e.g. ``20261120T220000123456Z``).
  Lexicographic order equals chronological order, it is filesystem-safe (the
  id doubles as the ``subs/<session>/`` / ``stacks/<session>/`` directory name
  of ticket 03), and it is unique to the microsecond. On a collision — only
  possible with a frozen/injected clock, two starts in the same microsecond —
  a ``-2``, ``-3``, ... suffix is appended instead of failing.
- **Start while one is open is REJECTED** with :class:`SessionOpenError`
  (the alternative would be auto-closing the open session at the new start
  time). Rejection is safer for data integrity: a session left open by a
  crash is never silently ended, the caller must close it explicitly (or
  investigate) before starting the next night.
- **Times** — stored as UTC ISO-8601 strings with an explicit ``+00:00``
  offset and millisecond precision (same format as
  :func:`~.project.utc_now_iso`). ``started_at`` / ``ended_at`` arguments
  accept a :class:`datetime` (naive is treated as UTC, aware is converted to
  UTC) or an ISO-8601 string; ``None`` means "now" from the injectable
  ``clock`` (default: the system UTC clock), which tests can replace with a
  frozen clock for deterministic ids and timestamps.
- **Writes** — every mutation runs in one transaction (``with project.db:``),
  so a rejected start/close leaves no partial row behind.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .errors import (
    ProjectError,
    SessionAlreadyClosedError,
    SessionNotFoundError,
    SessionOpenError,
)
from .project import Project

#: A clock: returns the current time as a (timezone-aware or naive) datetime.
#: Tests inject a frozen clock for deterministic session ids and timestamps.
Clock = Callable[[], datetime]

#: Upper bound for the collision suffix when deriving a session id.
_MAX_ID_SUFFIX = 999


@dataclass(frozen=True)
class Session:
    """One night's observing run (GLOSSARY.md) as stored in the db.

    - ``id`` — the session slug (TEXT PK, see the module docstring);
    - ``started_at`` — UTC ISO-8601 string with ``+00:00`` offset;
    - ``ended_at`` — UTC ISO-8601 string, or None while the session is open.
    """

    id: str
    started_at: str
    ended_at: str | None


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(dt: datetime) -> str:
    """Format ``dt`` as a UTC ISO-8601 string with explicit ``+00:00`` offset
    and millisecond precision (same format as :func:`.project.utc_now_iso`)."""
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _session_id_slug(dt: datetime) -> str:
    """The session id for a start time ``dt``: UTC ISO-8601 basic format with
    microsecond precision, e.g. ``20261120T220000123456Z``."""
    utc = dt.astimezone(timezone.utc)
    return utc.strftime("%Y%m%dT%H%M%S") + f"{utc.microsecond:06d}Z"


def _coerce_utc(value: datetime | str | None, clock: Clock) -> datetime:
    """Resolve a timestamp argument to an aware UTC datetime.

    None -> ``clock()``; a string is parsed with
    :meth:`datetime.datetime.fromisoformat` (``Z`` suffix allowed); a naive
    datetime is treated as UTC (documented choice); an aware datetime is
    converted to UTC.
    """
    if value is None:
        value = clock()
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"not an ISO-8601 timestamp: {value!r}") from exc
    if not isinstance(value, datetime):
        raise TypeError(
            f"timestamp must be a datetime, an ISO-8601 string, or None, got {value!r}"
        )
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)  # naive -> UTC
    return value.astimezone(timezone.utc)


def _row_to_session(row: sqlite3.Row | tuple[Any, ...]) -> Session:
    return Session(id=row[0], started_at=row[1], ended_at=row[2])


def start_session(
    project: Project,
    started_at: datetime | str | None = None,
    *,
    clock: Clock = _default_clock,
) -> str:
    """Start a new session (one night) in ``project`` and return its id.

    - ``started_at`` — the start time: a :class:`datetime` (naive is treated
      as UTC), an ISO-8601 string, or None for "now" from ``clock``;
    - ``clock`` — injectable clock used when ``started_at`` is None (tests
      pass a frozen clock for deterministic ids/timestamps).

    Inserts a row into ``sessions`` with the UTC start (ISO ``+00:00``) and
    ``ended_at`` NULL, in one transaction (``with project.db:``), and returns
    the new session id (UTC timestamp slug, see the module docstring).

    Raises :class:`SessionOpenError` when a session is already open —
    starting while one is open is rejected (documented choice, see the module
    docstring); close it first with :func:`close_session`.
    """
    start = _coerce_utc(started_at, clock)
    started_iso = _utc_iso(start)
    base_id = _session_id_slug(start)
    with project.db:  # one transaction: the open-check and the insert together
        open_row = project.db.execute(
            "SELECT id FROM sessions WHERE ended_at IS NULL ORDER BY started_at, id LIMIT 1"
        ).fetchone()
        if open_row is not None:
            raise SessionOpenError(
                f"project {project.slug!r} already has an open session "
                f"{open_row[0]!r} — close it with close_session() before "
                "starting a new one"
            )
        session_id = base_id
        suffix = 2
        while project.db.execute(
            "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
        ).fetchone():
            if suffix > _MAX_ID_SUFFIX:
                raise ProjectError(
                    f"cannot derive a unique session id from {base_id!r} "
                    f"(tried suffixes up to -{_MAX_ID_SUFFIX})"
                )
            session_id = f"{base_id}-{suffix}"
            suffix += 1
        project.db.execute(
            "INSERT INTO sessions (id, started_at, ended_at) VALUES (?, ?, NULL)",
            (session_id, started_iso),
        )
    return session_id


def close_session(
    project: Project,
    session_id: str,
    ended_at: datetime | str | None = None,
    *,
    clock: Clock = _default_clock,
) -> Session:
    """Close the session ``session_id`` in ``project`` and return it.

    - ``ended_at`` — the end time: a :class:`datetime` (naive is treated as
      UTC), an ISO-8601 string, or None for "now" from ``clock``.

    Records the UTC end (ISO ``+00:00``) in one transaction
    (``with project.db:``) and returns the updated :class:`Session`.

    Raises :class:`SessionNotFoundError` when no session with that id exists,
    and :class:`SessionAlreadyClosedError` when it is already closed.
    """
    end = _coerce_utc(ended_at, clock)
    ended_iso = _utc_iso(end)
    with project.db:  # one transaction: the checks and the update together
        row = project.db.execute(
            "SELECT id, started_at, ended_at FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise SessionNotFoundError(
                f"project {project.slug!r} has no session {session_id!r}"
            )
        if row[2] is not None:
            raise SessionAlreadyClosedError(
                f"session {session_id!r} in project {project.slug!r} is already "
                f"closed (ended_at {row[2]})"
            )
        project.db.execute(
            "UPDATE sessions SET ended_at = ? WHERE id = ?", (ended_iso, session_id)
        )
    return Session(id=row[0], started_at=row[1], ended_at=ended_iso)


def list_sessions(project: Project) -> list[Session]:
    """All sessions of ``project`` as :class:`Session` records, ordered by
    ``started_at`` (ties broken by ``id``)."""
    rows = project.db.execute(
        "SELECT id, started_at, ended_at FROM sessions ORDER BY started_at, id"
    ).fetchall()
    return [_row_to_session(row) for row in rows]


def get_open_session(project: Project) -> Session | None:
    """The open session of ``project`` (``ended_at IS NULL``), or None.

    At most one session can be open (starting while one is open is rejected),
    so this returns at most one record; if the db was edited by hand and
    several are open, the earliest by ``started_at`` wins.
    """
    row = project.db.execute(
        "SELECT id, started_at, ended_at FROM sessions "
        "WHERE ended_at IS NULL ORDER BY started_at, id LIMIT 1"
    ).fetchone()
    return _row_to_session(row) if row is not None else None
