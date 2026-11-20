"""Per-frame quality records — the persisted metrics + outlier flags of a project.

Pure-Python (module boundary per ``.scratch/project-store/spec.md``): no GTK
imports, no :mod:`seestar_controller.link` imports, stdlib only.

A **frame-quality record** (frame-quality ticket 01) is the persisted quality
scoring of ONE stored frame: the metrics computed by
:mod:`seestar_controller.frame_quality` (FWHM in pixels, background median
and std from sigma-clipped stats, detected star count) plus the outlier flag
the per-session robust z-score pass set. Records live in the
``frame_quality`` table of the project db (see :mod:`.schema`, migration
3 -> 4): ``frame_id`` (INTEGER primary key — the ``frames`` row id,
cascade-deleted with the frame), ``fwhm`` (nullable — None when no star was
detected), ``bg_median`` / ``bg_std``, ``star_count``, ``flagged`` (0/1) with
``flag_reasons`` (a JSON list of the metric names whose robust z-score
exceeded the session threshold), and ``scored_at`` (UTC ISO-8601).

API (all take the open :class:`~.project.Project` handle first):

- :func:`save_frame_quality` — save/replace the quality record of one frame,
  returns the :class:`FrameQuality`;
- :func:`frame_quality_for_frame` — the record of one frame, or None;
- :func:`session_quality` — the quality table of one session (records joined
  with their frames, ordered by ``jd``) as a :class:`SessionQuality` with the
  summary (median FWHM, % rejected, ...).

Documented choices:

- **Frame must exist.** :func:`save_frame_quality` checks the ``frames`` row
  inside the same transaction as the write and raises
  :class:`~.errors.QualityError` naming the frame id — a clear error instead
  of the raw FK :class:`sqlite3.IntegrityError`.
- **Upsert.** Saving twice for the same frame replaces the record
  (``INSERT OR REPLACE``) and refreshes ``scored_at`` — re-scoring a session
  overwrites the previous flags, the latest run wins.
- **Validation** (all raise :class:`~.errors.QualityError` with the field and
  value in the message, before anything is written): ``fwhm`` is None or a
  finite number; ``bg_median`` / ``bg_std`` are finite numbers; ``star_count``
  is an int ``>= 0``; ``flagged`` is a bool; ``flag_reasons`` is an iterable of
  strings (stored as a JSON list). Structural type errors (``frame_id`` not
  an int) raise :class:`TypeError`.
- **Reads are tolerant of missing rows, loud about corrupt ones.**
  :func:`frame_quality_for_frame` returns None when the frame has no record
  (an unknown frame id is a filter, not an error — same semantics as
  :func:`.frames.list_frames` with an unknown session); a corrupt row (invalid
  JSON reasons, or JSON that is not a list of strings) raises
  :class:`~.errors.QualityError`. :func:`session_quality` raises
  :class:`~.errors.SessionNotFoundError` for an unknown session id (it is a
  lookup, not a filter) and returns an empty :class:`SessionQuality` when the
  session exists but has no scored frames.
- **Summary math.** :class:`SessionQuality` computes the medians with
  :func:`statistics.median` over the present values (``median_fwhm`` is None
  when no frame has a FWHM); ``rejected_percent`` is
  ``100 * flagged / total`` (0.0 when the session has no scored frames).
- **Writes** — every mutation runs in one transaction (``with project.db:``),
  so a rejected save leaves no partial row behind.
"""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal, overload

from .errors import QualityError, SessionNotFoundError
from .project import Project, utc_now_iso

#: Columns selected for a :class:`FrameQuality` (the join of ``frame_quality``
#: with ``frames``), in :class:`FrameQuality` field order after ``frame_id``.
_QUALITY_COLUMNS = (
    "q.frame_id, f.session_id, q.fwhm, q.bg_median, q.bg_std, q.star_count, "
    "q.flagged, q.flag_reasons, q.scored_at"
)

#: The base SELECT joining frame_quality with its frame.
_SELECT_SQL = (
    f"SELECT {_QUALITY_COLUMNS} FROM frame_quality q "
    "JOIN frames f ON f.id = q.frame_id"
)


@dataclass(frozen=True)
class FrameQuality:
    """One persisted per-frame quality record (the ``frame_quality`` row).

    - ``frame_id`` — the ``frames`` row id the record belongs to;
    - ``session_id`` — the frame's session, joined from ``frames`` (the night);
    - ``fwhm`` — the estimated FWHM in pixels, or None when no star was
      detected on the frame;
    - ``bg_median`` / ``bg_std`` — the sigma-clipped background median and
      standard deviation;
    - ``star_count`` — the number of detected stars;
    - ``flagged`` — the outlier flag (robust z-score beyond the session
      threshold on at least one metric);
    - ``flag_reasons`` — the metric names that flagged the frame (e.g.
      ``("fwhm",)``), empty when not flagged;
    - ``scored_at`` — the scoring time as a UTC ISO-8601 string (``+00:00``).
    """

    frame_id: int
    session_id: str
    fwhm: float | None
    bg_median: float
    bg_std: float
    star_count: int
    flagged: bool
    flag_reasons: tuple[str, ...]
    scored_at: str


def _row_to_quality(row: sqlite3.Row | tuple[Any, ...]) -> FrameQuality:
    reasons_raw = str(row[7])
    try:
        reasons = json.loads(reasons_raw)
    except (TypeError, ValueError) as exc:
        raise QualityError(
            f"quality record of frame {row[0]!r} has corrupt flag_reasons JSON: {exc}"
        ) from exc
    if not isinstance(reasons, list) or not all(isinstance(r, str) for r in reasons):
        raise QualityError(
            f"quality record of frame {row[0]!r} has invalid flag_reasons "
            f"(not a JSON list of strings): {reasons_raw!r}"
        )
    return FrameQuality(
        frame_id=int(row[0]),
        session_id=str(row[1]),
        fwhm=None if row[2] is None else float(row[2]),
        bg_median=float(row[3]),
        bg_std=float(row[4]),
        star_count=int(row[5]),
        flagged=bool(row[6]),
        flag_reasons=tuple(reasons),
        scored_at=str(row[8]),
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


@overload
def _finite_float(value: Any, *, what: str, allow_none: Literal[False] = False) -> float: ...
@overload
def _finite_float(value: Any, *, what: str, allow_none: Literal[True]) -> float | None: ...
def _finite_float(value: Any, *, what: str, allow_none: bool = False) -> float | None:
    """Coerce ``value`` to a finite float (bools rejected); None stays None
    when ``allow_none``; raise :class:`QualityError` otherwise."""
    if value is None:
        if allow_none:
            return None
        raise QualityError(f"{what} must be a number, got None")
    if isinstance(value, bool):
        raise QualityError(f"{what} must be a number, got {value!r}")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise QualityError(f"{what} must be a number, got {value!r}") from None
    if not math.isfinite(result):
        raise QualityError(f"{what} must be finite, got {value!r}")
    return result


def _check_star_count(value: Any) -> int:
    """Validate ``star_count``: an int ``>= 0`` (bools rejected)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise QualityError(f"star_count must be an int, got {value!r}")
    if value < 0:
        raise QualityError(f"star_count must be >= 0, got {value!r}")
    return value


def _check_reasons(value: Any) -> tuple[str, ...]:
    """Validate ``flag_reasons``: an iterable of strings (not a str/bytes)."""
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise QualityError(
            f"flag_reasons must be an iterable of strings, got {value!r}"
        )
    reasons = tuple(value)
    if not all(isinstance(r, str) for r in reasons):
        raise QualityError(f"flag_reasons must contain only strings, got {reasons!r}")
    return reasons


def save_frame_quality(
    project: Project,
    frame_id: int,
    *,
    fwhm: float | None,
    bg_median: float,
    bg_std: float,
    star_count: int,
    flagged: bool = False,
    flag_reasons: Iterable[str] = (),
    scored_at: str | None = None,
) -> FrameQuality:
    """Save (or replace) the quality record of the frame ``frame_id`` in
    ``project`` and return it as a :class:`FrameQuality`.

    - ``frame_id`` — the ``frames`` row id; must exist, else
      :class:`~.errors.QualityError` is raised (before anything is written);
    - ``fwhm`` — the estimated FWHM in pixels, or None when no star was
      detected; must be a finite number or None;
    - ``bg_median`` / ``bg_std`` — the sigma-clipped background median and
      std; finite numbers;
    - ``star_count`` — the detected star count; an int ``>= 0``;
    - ``flagged`` — the outlier flag (default False);
    - ``flag_reasons`` — the metric names that flagged the frame (an iterable
      of strings, stored as a JSON list; default empty);
    - ``scored_at`` — the scoring time as a UTC ISO-8601 string; None (default)
      uses :func:`.project.utc_now_iso`.

    The frame-exists check and the upsert run in ONE transaction
    (``with project.db:``). Saving again for the same frame replaces the
    record and refreshes ``scored_at``. The returned record carries
    ``session_id`` joined from the frame.
    """
    frame_id = _check_id(frame_id, what="frame_id")
    flagged = _check_bool(flagged, what="flagged")
    fwhm_value = _finite_float(fwhm, what="fwhm", allow_none=True)
    bg_median_value = _finite_float(bg_median, what="bg_median")
    bg_std_value = _finite_float(bg_std, what="bg_std")
    star_count_value = _check_star_count(star_count)
    reasons = _check_reasons(flag_reasons)
    if scored_at is None:
        scored_at = utc_now_iso()
    elif not isinstance(scored_at, str) or not scored_at:
        raise QualityError(f"scored_at must be a non-empty string or None, got {scored_at!r}")
    reasons_json = json.dumps(list(reasons))
    with project.db:  # one transaction: the frame check and the upsert together
        row = project.db.execute(
            "SELECT session_id FROM frames WHERE id = ?", (frame_id,)
        ).fetchone()
        if row is None:
            raise QualityError(
                f"project {project.slug!r} has no frame {frame_id!r} — "
                "quality records need an existing frame (see store_frame)"
            )
        project.db.execute(
            "INSERT OR REPLACE INTO frame_quality "
            "(frame_id, fwhm, bg_median, bg_std, star_count, flagged, flag_reasons, scored_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                frame_id,
                fwhm_value,
                bg_median_value,
                bg_std_value,
                star_count_value,
                int(flagged),
                reasons_json,
                scored_at,
            ),
        )
        session_id = str(row[0])
    return FrameQuality(
        frame_id=frame_id,
        session_id=session_id,
        fwhm=fwhm_value,
        bg_median=bg_median_value,
        bg_std=bg_std_value,
        star_count=star_count_value,
        flagged=flagged,
        flag_reasons=reasons,
        scored_at=scored_at,
    )


def frame_quality_for_frame(project: Project, frame_id: int) -> FrameQuality | None:
    """The quality record of the frame ``frame_id`` in ``project`` as a
    :class:`FrameQuality`, or None when the frame has no record.

    An unknown frame id is a filter, not an error — it yields None (same
    semantics as :func:`.frames.list_frames` with an unknown session). A
    corrupt record row raises :class:`~.errors.QualityError`.
    """
    frame_id = _check_id(frame_id, what="frame_id")
    row = project.db.execute(
        f"{_SELECT_SQL} WHERE q.frame_id = ?", (frame_id,)
    ).fetchone()
    if row is None:
        return None
    return _row_to_quality(row)


@dataclass(frozen=True)
class SessionQuality:
    """The persisted quality table of one session plus its summary.

    - ``session_id`` — the session the records belong to;
    - ``frames`` — the :class:`FrameQuality` records of the session's frames,
      ordered by the frames' ``jd`` (ties broken by frame id).

    Summary properties: :attr:`frame_count`, :attr:`flagged` (the flagged
    records), :attr:`flagged_count`, :attr:`rejected_percent` (``100 *
    flagged / total``, 0.0 when empty), and the medians :attr:`median_fwhm`
    (None when no frame has a FWHM), :attr:`median_bg_median`,
    :attr:`median_bg_std`, :attr:`median_star_count`.
    """

    session_id: str
    frames: tuple[FrameQuality, ...]

    @property
    def frame_count(self) -> int:
        """The number of scored frames in the session."""
        return len(self.frames)

    @property
    def flagged(self) -> tuple[FrameQuality, ...]:
        """The flagged (rejected) records of the session."""
        return tuple(f for f in self.frames if f.flagged)

    @property
    def flagged_count(self) -> int:
        """The number of flagged (rejected) frames."""
        return len(self.flagged)

    @property
    def rejected_percent(self) -> float:
        """The rejected fraction in percent: ``100 * flagged / total``
        (0.0 when the session has no scored frames)."""
        if not self.frames:
            return 0.0
        return 100.0 * len(self.flagged) / len(self.frames)

    @property
    def median_fwhm(self) -> float | None:
        """The median FWHM (pixels) over the frames that have one, or None."""
        values = [f.fwhm for f in self.frames if f.fwhm is not None]
        if not values:
            return None
        return float(statistics.median(values))

    @property
    def median_bg_median(self) -> float | None:
        """The median background level over the scored frames, or None."""
        if not self.frames:
            return None
        return float(statistics.median(f.bg_median for f in self.frames))

    @property
    def median_bg_std(self) -> float | None:
        """The median background std over the scored frames, or None."""
        if not self.frames:
            return None
        return float(statistics.median(f.bg_std for f in self.frames))

    @property
    def median_star_count(self) -> float | None:
        """The median detected star count over the scored frames, or None."""
        if not self.frames:
            return None
        return float(statistics.median(f.star_count for f in self.frames))


def session_quality(project: Project, session_id: str) -> SessionQuality:
    """The persisted quality table of the session ``session_id`` in
    ``project`` as a :class:`SessionQuality` (records + summary).

    The records are joined with their frames and ordered by the frames' ``jd``
    (ties broken by frame id). A session that exists but has no scored frames
    yields an empty :class:`SessionQuality` (``frames == ()``, medians None,
    ``rejected_percent == 0.0``).

    Raises :class:`~.errors.SessionNotFoundError` when the project has no
    session with that id (this is a lookup, not a filter), and
    :class:`~.errors.QualityError` for a corrupt record row.
    """
    if not isinstance(session_id, str):
        raise TypeError(f"session_id must be a string, got {session_id!r}")
    row = project.db.execute(
        "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if row is None:
        raise SessionNotFoundError(
            f"project {project.slug!r} has no session {session_id!r}"
        )
    rows = project.db.execute(
        f"{_SELECT_SQL} WHERE f.session_id = ? ORDER BY f.jd, f.id", (session_id,)
    ).fetchall()
    return SessionQuality(
        session_id=session_id, frames=tuple(_row_to_quality(r) for r in rows)
    )
