"""Persisted solved WCS — one per plate-solved frame, in the project db.

Pure-Python (module boundary per ``.scratch/project-store/spec.md``): no GTK
imports, no :mod:`seestar_controller.link` imports, stdlib only. The WCS
itself is stored as a JSON dict of FITS cards (``cards_json``), so this
module never imports astropy — :meth:`FrameWcs.to_wcs` rebuilds the
:class:`astropy.wcs.WCS` lazily for callers that want it.

A **solved-WCS record** (astrometry ticket 01) is the persisted result of
plate-solving ONE stored frame: the solver that produced it (the local
astrometry.net ``solve-field`` binary or the pure-Python Gaia pattern match),
the solve time, the match RMS in arcsec and star count, the solved field
center, and the full WCS as FITS cards. The record lives in the
``frame_wcs`` table of the project db (see :mod:`.schema`, migration 6 -> 7),
keyed by the ``frame_id`` FK (``REFERENCES frames (id) ON DELETE CASCADE`` —
deleting the frame deletes its WCS record).

API (all take the open :class:`~.project.Project` handle first):

- :func:`save_frame_wcs` — validated upsert of one frame's WCS record in ONE
  transaction, returns the saved :class:`FrameWcs`;
- :func:`load_frame_wcs` — one frame's record, or None when it has none;
- :func:`list_solved_frames` — the solved frames (optionally of one session),
  ordered by the frames' ``jd``.

Documented choices:

- **Frame must exist.** :func:`save_frame_wcs` checks the ``frames`` row
  inside the same transaction as the write and raises
  :class:`~.errors.WcsStoreError` naming the frame id — a clear error instead
  of the raw FK :class:`sqlite3.IntegrityError`.
- **Upsert.** Saving twice for the same frame replaces the record
  (``INSERT OR REPLACE``) and refreshes ``solved_at`` — re-solving a frame
  overwrites the previous WCS, the latest solve wins.
- **Validation** (all raise :class:`~.errors.WcsStoreError` with the field and
  value in the message, BEFORE anything is written): ``solver`` is a
  non-empty string; ``rms_arcsec`` is None or a finite number ``>= 0``;
  ``n_stars`` is an int ``>= 0``; ``center_ra_deg`` is a finite number
  (normalized to ``[0, 360)``); ``center_dec_deg`` is a finite number in
  ``[-90, 90]``; ``cards`` is a mapping with string keys whose values are
  JSON-serializable (stored as a JSON object). Structural type errors
  (``frame_id`` not an int, ``cards`` not a mapping) raise :class:`TypeError`.
- **Reads are tolerant of missing rows.** :func:`load_frame_wcs` returns None
  when the frame has no record (an unknown frame id is a filter, not an
  error — same semantics as :func:`.quality.frame_quality_for_frame`).
  :func:`list_solved_frames` simply lists the frames that have a record.
- **Writes** — every mutation runs in one transaction (``with project.db:``),
  so a rejected save leaves no partial row behind.
"""

from __future__ import annotations

import json
import math
import numbers
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .errors import SessionNotFoundError, WcsStoreError
from .project import Project, utc_now_iso

#: Columns selected for a :class:`FrameWcs`, in field order.
_SELECT_SQL = (
    "SELECT w.frame_id, f.session_id, w.solver, w.solved_at, w.rms_arcsec, "
    "w.n_stars, w.center_ra_deg, w.center_dec_deg, w.cards_json "
    "FROM frame_wcs w JOIN frames f ON f.id = w.frame_id"
)


@dataclass(frozen=True)
class FrameWcs:
    """One persisted solved-WCS record (a ``frame_wcs`` table row).

    - ``frame_id`` — the ``frames`` row id the WCS belongs to;
    - ``session_id`` — the session of that frame (joined);
    - ``solver`` — the solver that produced the WCS (``"solve-field"`` or
      ``"gaia"``);
    - ``solved_at`` — the solve time as a UTC ISO-8601 string;
    - ``rms_arcsec`` — the match RMS in arcsec (None when the solver
      reported no RMS);
    - ``n_stars`` — the number of matched stars used for the solve;
    - ``center_ra_deg`` / ``center_dec_deg`` — the solved field center
      (J2000 degrees, ``center_ra_deg`` normalized to ``[0, 360)``);
    - ``cards`` — the full WCS as a dict of FITS cards (from ``cards_json``);
      do not mutate it.
    """

    frame_id: int
    session_id: str
    solver: str
    solved_at: str
    rms_arcsec: float | None
    n_stars: int
    center_ra_deg: float
    center_dec_deg: float
    cards: dict[str, Any]

    def to_wcs(self) -> Any:
        """Rebuild the :class:`astropy.wcs.WCS` from :attr:`cards` (lazy
        astropy import — this module itself stays astropy-free)."""
        from astropy.io import fits as pyfits
        from astropy.wcs import WCS

        return WCS(pyfits.Header(dict(self.cards)), naxis=2)


def _row_to_frame_wcs(row: sqlite3.Row | tuple[Any, ...]) -> FrameWcs:
    cards_raw = str(row[8])
    try:
        cards = json.loads(cards_raw)
    except (TypeError, ValueError) as exc:
        raise WcsStoreError(
            f"WCS record of frame {row[0]!r} has corrupt cards_json: {exc}"
        ) from exc
    if not isinstance(cards, dict) or not all(isinstance(k, str) for k in cards):
        raise WcsStoreError(
            f"WCS record of frame {row[0]!r} has invalid cards_json "
            f"(not a JSON object with string keys): {cards_raw!r}"
        )
    return FrameWcs(
        frame_id=int(row[0]),
        session_id=str(row[1]),
        solver=str(row[2]),
        solved_at=str(row[3]),
        rms_arcsec=None if row[4] is None else float(row[4]),
        n_stars=int(row[5]),
        center_ra_deg=float(row[6]) % 360.0,
        center_dec_deg=float(row[7]),
        cards=cards,
    )


def _check_id(value: Any, *, what: str) -> int:
    """Validate a db row id argument (an int, bools rejected);
    :class:`TypeError` otherwise."""
    if isinstance(value, bool) or not isinstance(value, numbers.Integral):
        raise TypeError(f"{what} must be an int, got {value!r}")
    return int(value)


def _finite_float(value: Any, *, what: str, allow_none: bool = False) -> float | None:
    """Coerce ``value`` to a finite float (bools rejected); None stays None
    when ``allow_none``; raise :class:`WcsStoreError` otherwise."""
    if value is None:
        if allow_none:
            return None
        raise WcsStoreError(f"{what} must be a number, got None")
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise WcsStoreError(f"{what} must be a number, got {value!r}")
    result = float(value)
    if not math.isfinite(result):
        raise WcsStoreError(f"{what} must be finite, got {value!r}")
    return result


def _check_n_stars(value: Any) -> int:
    """Validate ``n_stars``: an int ``>= 0`` (bools rejected)."""
    if isinstance(value, bool) or not isinstance(value, numbers.Integral):
        raise WcsStoreError(f"n_stars must be an int, got {value!r}")
    if value < 0:
        raise WcsStoreError(f"n_stars must be >= 0, got {value!r}")
    return value


def _check_solver(value: Any) -> str:
    """Validate ``solver``: a non-empty string."""
    if not isinstance(value, str) or not value:
        raise WcsStoreError(f"solver must be a non-empty string, got {value!r}")
    return value


def _check_center(value: Any, *, what: str, lo: float, hi: float) -> float:
    """Validate a field-center coordinate: a finite number in ``[lo, hi]``."""
    result = _finite_float(value, what=what)
    assert result is not None  # _finite_float without allow_none never returns None
    if not lo <= result <= hi:
        raise WcsStoreError(f"{what} must be in [{lo}, {hi}], got {value!r}")
    return result


def _check_cards(value: Any) -> dict[str, Any]:
    """Validate ``cards``: a mapping with string keys and JSON-serializable
    values; returns it as a plain dict."""
    if not isinstance(value, Mapping):
        raise TypeError(f"cards must be a mapping of FITS cards, got {value!r}")
    cards = {str(key): item for key, item in value.items()}
    try:
        json.dumps(cards)
    except (TypeError, ValueError) as exc:
        raise WcsStoreError(f"cards must be JSON-serializable: {exc}") from exc
    return cards


def save_frame_wcs(
    project: Project,
    frame_id: int,
    *,
    solver: str,
    n_stars: int,
    center_ra_deg: float,
    center_dec_deg: float,
    cards: Mapping[str, Any],
    rms_arcsec: float | None = None,
    solved_at: str | None = None,
) -> FrameWcs:
    """Save (or replace) the solved-WCS record of the frame ``frame_id`` in
    ``project`` and return it as a :class:`FrameWcs`.

    - ``frame_id`` — the ``frames`` row id; must exist, else
      :class:`~.errors.WcsStoreError` is raised (before anything is written);
    - ``solver`` — the solver name (a non-empty string, e.g. ``"solve-field"``
      or ``"gaia"``);
    - ``n_stars`` — the matched star count; an int ``>= 0``;
    - ``center_ra_deg`` / ``center_dec_deg`` — the solved field center in
      J2000 degrees (RA normalized to ``[0, 360)``, Dec in ``[-90, 90]``);
    - ``cards`` — the full WCS as a mapping of FITS cards (JSON-serializable
      values, string keys);
    - ``rms_arcsec`` — the match RMS in arcsec; None (default) or a finite
      number ``>= 0``;
    - ``solved_at`` — the solve time as a UTC ISO-8601 string; None (default)
      uses :func:`.project.utc_now_iso`.

    The frame-exists check and the upsert run in ONE transaction
    (``with project.db:``). Saving again for the same frame replaces the
    record and refreshes ``solved_at``. The returned record carries
    ``session_id`` joined from the frame.
    """
    frame_id = _check_id(frame_id, what="frame_id")
    solver_value = _check_solver(solver)
    n_stars_value = _check_n_stars(n_stars)
    # RA is any finite number, normalized to [0, 360); Dec must be in [-90, 90]
    ra_raw = _finite_float(center_ra_deg, what="center_ra_deg")
    assert ra_raw is not None  # _finite_float without allow_none never returns None
    ra_value = ra_raw % 360.0
    dec_value = _check_center(center_dec_deg, what="center_dec_deg", lo=-90.0, hi=90.0)
    rms_value = _finite_float(rms_arcsec, what="rms_arcsec", allow_none=True)
    if rms_value is not None and rms_value < 0:
        raise WcsStoreError(f"rms_arcsec must be >= 0, got {rms_arcsec!r}")
    cards_value = _check_cards(cards)
    if solved_at is None:
        solved_at = utc_now_iso()
    elif not isinstance(solved_at, str) or not solved_at:
        raise WcsStoreError(
            f"solved_at must be a non-empty string or None, got {solved_at!r}"
        )
    cards_json = json.dumps(cards_value)
    with project.db:  # one transaction: the frame check and the upsert together
        row = project.db.execute(
            "SELECT session_id FROM frames WHERE id = ?", (frame_id,)
        ).fetchone()
        if row is None:
            raise WcsStoreError(
                f"project {project.slug!r} has no frame {frame_id!r} — "
                "WCS records need an existing frame (see store_frame)"
            )
        project.db.execute(
            "INSERT OR REPLACE INTO frame_wcs "
            "(frame_id, solver, solved_at, rms_arcsec, n_stars, "
            " center_ra_deg, center_dec_deg, cards_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                frame_id,
                solver_value,
                solved_at,
                rms_value,
                n_stars_value,
                ra_value,
                dec_value,
                cards_json,
            ),
        )
        session_id = str(row[0])
    return FrameWcs(
        frame_id=frame_id,
        session_id=session_id,
        solver=solver_value,
        solved_at=solved_at,
        rms_arcsec=rms_value,
        n_stars=n_stars_value,
        center_ra_deg=ra_value,
        center_dec_deg=dec_value,
        cards=cards_value,
    )


def load_frame_wcs(project: Project, frame_id: int) -> FrameWcs | None:
    """The solved-WCS record of the frame ``frame_id`` in ``project`` as a
    :class:`FrameWcs`, or None when the frame has no record.

    An unknown frame id is a filter, not an error — it yields None (same
    semantics as :func:`.quality.frame_quality_for_frame`). A corrupt record
    row raises :class:`~.errors.WcsStoreError`.
    """
    frame_id = _check_id(frame_id, what="frame_id")
    row = project.db.execute(
        f"{_SELECT_SQL} WHERE w.frame_id = ?", (frame_id,)
    ).fetchone()
    if row is None:
        return None
    return _row_to_frame_wcs(row)


def list_solved_frames(
    project: Project, session_id: str | None = None
) -> tuple[FrameWcs, ...]:
    """The solved-WCS records of ``project`` as a tuple of :class:`FrameWcs`,
    ordered by the frames' ``jd`` (ties broken by frame id).

    ``session_id`` — when given, only the solved frames of that session are
    returned; None (default) lists all solved frames of the project. An
    unknown session id raises :class:`~.errors.SessionNotFoundError` (this is
    a lookup, not a filter — same semantics as
    :func:`.quality.session_quality`).
    """
    if session_id is None:
        rows = project.db.execute(
            f"{_SELECT_SQL} ORDER BY f.jd, f.id"
        ).fetchall()
    else:
        if not isinstance(session_id, str):
            raise TypeError(f"session_id must be a string or None, got {session_id!r}")
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
    return tuple(_row_to_frame_wcs(r) for r in rows)
