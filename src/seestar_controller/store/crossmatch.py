"""Persisted Gaia cross-match tables — one per stack, in the project db.

Pure-Python (module boundary per ``.scratch/project-store/spec.md``): no GTK
imports, no :mod:`seestar_controller.link` imports, stdlib only. Rows are
duck-typed (see :func:`save_crossmatch`), so the pure
:mod:`seestar_controller.gaia` core can hand its
:class:`~seestar_controller.gaia.matching.CrossmatchRow` objects straight to
:func:`save_crossmatch` without this module importing that package.

A **cross-match table** (gaia-crossmatch ticket 02) is the persisted result of
cross-matching the sources detected on ONE session stack against Gaia DR3:
one row per detection. The table lives in the ``crossmatch`` table of the
project db (see :mod:`.schema`, migration 5 -> 6), keyed by the ``frame_id``
FK (``REFERENCES frames (id) ON DELETE CASCADE`` — deleting the frame deletes
its cross-match rows). Each row carries:

- the detection's pixel position ``x``/``y`` and J2000 sky position
  ``ra_deg``/``dec_deg`` (degrees);
- the ``matched`` flag;
- the matched Gaia DR3 fields — ``source_id``, ``g``/``bp``/``rp``,
  ``parallax`` + ``parallax_error`` (mas), ``pmra``/``pmra_error``,
  ``pmdec``/``pmdec_error`` (mas/yr) — all ``None`` when unmatched;
- the match ``separation_arcsec`` (``None`` when unmatched).

API (all take the open :class:`~.project.Project` handle first):

- :func:`save_crossmatch` — validated upsert of one frame's whole table in
  ONE transaction, returns the saved :class:`CrossmatchRecord` rows;
- :func:`load_crossmatch` — one frame's table as a tuple of
  :class:`CrossmatchRecord` (empty when the frame has none);
- :func:`list_crossmatched_stacks` — the frames that have a cross-match
  table, as :class:`CrossmatchedStack` summaries (frame id + session + kind
  + row/matched counts).

Documented choices:

- **Frame must exist.** :func:`save_crossmatch` checks the ``frames`` row
  inside the same transaction as the write and raises
  :class:`~.errors.CrossmatchError` naming the frame id — a clear error
  instead of the raw FK :class:`sqlite3.IntegrityError`.
- **Upsert = replace the frame's whole table.** Saving again for the same
  frame deletes its previous rows and inserts the new ones in the same
  transaction — re-running a cross-match overwrites the previous table, the
  latest run wins (same semantics as :func:`.stars.save_star_picks`).
- **Rows are duck-typed.** Each row may be a mapping (``row["x"]``) or any
  object with attributes (``row.x``) — e.g. a
  :class:`~seestar_controller.gaia.matching.CrossmatchRow`. Required fields:
  ``x``, ``y``, ``ra_deg``, ``dec_deg``, ``matched``. Optional fields (default
  ``None``): ``source_id``, ``g``, ``bp``, ``rp``, ``parallax``,
  ``parallax_error``, ``pmra``, ``pmra_error``, ``pmdec``, ``pmdec_error``,
  ``separation_arcsec`` — the error columns exist for callers whose Gaia
  rows carry them (a plain ``CrossmatchRow`` has none, they stay NULL).
- **Validation** (all raise :class:`~.errors.CrossmatchError` with the field
  and value in the message, BEFORE anything is written): ``x``/``y`` are
  finite numbers; ``ra_deg`` is a finite number (normalized to ``[0, 360)``);
  ``dec_deg`` is a finite number in ``[-90, 90]``; ``source_id`` is None or an
  int that fits a SQLite INTEGER; the magnitude/astrometry fields are None or
  finite numbers; ``separation_arcsec`` is None or a finite number ``>= 0``.
  Structural type errors (``frame_id`` not an int, ``rows`` not an iterable,
  ``matched`` not a bool) raise :class:`TypeError`.
- **Reads are tolerant of missing rows.** :func:`load_crossmatch` returns an
  empty tuple when the frame has no table (an unknown frame id is a filter,
  not an error — same semantics as :func:`.quality.frame_quality_for_frame`).
  :func:`list_crossmatched_stacks` simply lists the frames that have rows.
- **Writes** — every mutation runs in one transaction (``with project.db:``),
  so a rejected save leaves no partial rows behind.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .errors import CrossmatchError
from .project import Project

#: Columns selected for a :class:`CrossmatchRecord`, in field order (the
#: ``id`` column is only the insertion order and is not exposed).
_CROSSMATCH_COLUMNS = (
    "frame_id, x, y, ra_deg, dec_deg, matched, source_id, g, bp, rp, "
    "parallax, parallax_error, pmra, pmra_error, pmdec, pmdec_error, "
    "separation_arcsec"
)

#: SQLite INTEGER range (signed 64-bit) — ``source_id`` must fit.
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1

#: The optional (nullable) float row fields, in column order (``source_id`` is
#: handled separately — it is an int, not a float).
_OPTIONAL_FLOAT_FIELDS = (
    "g",
    "bp",
    "rp",
    "parallax",
    "parallax_error",
    "pmra",
    "pmra_error",
    "pmdec",
    "pmdec_error",
    "separation_arcsec",
)


@dataclass(frozen=True)
class CrossmatchRecord:
    """One persisted cross-match row (a ``crossmatch`` table row).

    - ``frame_id`` — the ``frames`` row id (the stack) the row belongs to;
    - ``x`` / ``y`` — the detection's 0-based pixel position;
    - ``ra_deg`` / ``dec_deg`` — the detection's J2000 sky position (degrees,
      ``ra_deg`` normalized to ``[0, 360)``);
    - ``matched`` — whether a Gaia row lies within the match radius;
    - ``source_id`` — the matched Gaia DR3 source id (None when unmatched);
    - ``g`` / ``bp`` / ``rp`` — the matched G / BP / RP magnitudes;
    - ``parallax`` / ``parallax_error`` — parallax in mas + its error;
    - ``pmra`` / ``pmra_error``, ``pmdec`` / ``pmdec_error`` — proper motion
      in mas/yr (RA includes the cos(Dec) factor) + errors;
    - ``separation_arcsec`` — the sky separation of the matched pair.

    All Gaia fields and the separation are ``None`` for an unmatched
    detection.
    """

    frame_id: int
    x: float
    y: float
    ra_deg: float
    dec_deg: float
    matched: bool
    source_id: int | None = None
    g: float | None = None
    bp: float | None = None
    rp: float | None = None
    parallax: float | None = None
    parallax_error: float | None = None
    pmra: float | None = None
    pmra_error: float | None = None
    pmdec: float | None = None
    pmdec_error: float | None = None
    separation_arcsec: float | None = None


@dataclass(frozen=True)
class CrossmatchedStack:
    """Summary of one stack that has a persisted cross-match table.

    - ``frame_id`` — the ``frames`` row id (the stack);
    - ``session_id`` — the stack's session (the night);
    - ``kind`` — the frame kind (``'stack'`` for a cross-matched stack);
    - ``row_count`` — the number of cross-match rows of the frame;
    - ``matched_count`` — how many of them matched a Gaia row.
    """

    frame_id: int
    session_id: str
    kind: str
    row_count: int
    matched_count: int

    @property
    def unmatched_count(self) -> int:
        """The number of unmatched (flagged) rows: ``row_count - matched_count``."""
        return self.row_count - self.matched_count


def _row_to_record(row: sqlite3.Row | tuple[Any, ...]) -> CrossmatchRecord:
    return CrossmatchRecord(
        frame_id=int(row[0]),
        x=float(row[1]),
        y=float(row[2]),
        ra_deg=float(row[3]),
        dec_deg=float(row[4]),
        matched=bool(row[5]),
        source_id=None if row[6] is None else int(row[6]),
        g=None if row[7] is None else float(row[7]),
        bp=None if row[8] is None else float(row[8]),
        rp=None if row[9] is None else float(row[9]),
        parallax=None if row[10] is None else float(row[10]),
        parallax_error=None if row[11] is None else float(row[11]),
        pmra=None if row[12] is None else float(row[12]),
        pmra_error=None if row[13] is None else float(row[13]),
        pmdec=None if row[14] is None else float(row[14]),
        pmdec_error=None if row[15] is None else float(row[15]),
        separation_arcsec=None if row[16] is None else float(row[16]),
    )


def _check_frame_id(frame_id: Any) -> int:
    """Validate a frame id argument (an int, bools rejected);
    :class:`TypeError` otherwise."""
    if isinstance(frame_id, bool) or not isinstance(frame_id, int):
        raise TypeError(f"frame_id must be an int, got {frame_id!r}")
    return frame_id


def _has(row: Any, name: str) -> bool:
    """Whether the duck-typed row carries the (optional) field ``name``."""
    if isinstance(row, Mapping):
        return name in row
    return hasattr(row, name)


def _field(row: Any, name: str, index: int) -> Any:
    """Read the required field ``name`` from a duck-typed row: mapping key
    first, then attribute. Raises :class:`CrossmatchError` when the row has
    neither."""
    if isinstance(row, Mapping):
        if name in row:
            return row[name]
    elif hasattr(row, name):
        return getattr(row, name)
    raise CrossmatchError(
        f"crossmatch row {index} is missing the required field {name!r} (got {row!r})"
    )


def _finite_float(
    value: Any, *, what: str, index: int, allow_none: bool = False
) -> float | None:
    """Coerce ``value`` to a finite float (bools rejected); None stays None
    when ``allow_none``; raise :class:`CrossmatchError` otherwise."""
    if value is None:
        if allow_none:
            return None
        raise CrossmatchError(f"row {index}: {what} must be a number, got None")
    if isinstance(value, bool):
        raise CrossmatchError(f"row {index}: {what} must be a number, got {value!r}")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise CrossmatchError(
            f"row {index}: {what} must be a number, got {value!r}"
        ) from None
    if not math.isfinite(result):
        raise CrossmatchError(f"row {index}: {what} must be finite, got {value!r}")
    return result


def _validate_row(row: Any, index: int, frame_id: int) -> CrossmatchRecord:
    """Validate one duck-typed input row and return it as a
    :class:`CrossmatchRecord`. Value problems raise
    :class:`CrossmatchError`; structural problems raise :class:`TypeError`."""
    x = _finite_float(_field(row, "x", index), what="x", index=index)
    y = _finite_float(_field(row, "y", index), what="y", index=index)
    ra_deg = _finite_float(_field(row, "ra_deg", index), what="ra_deg", index=index)
    dec_deg = _finite_float(_field(row, "dec_deg", index), what="dec_deg", index=index)
    assert x is not None and y is not None and ra_deg is not None and dec_deg is not None
    ra_deg = ra_deg % 360.0
    if not -90.0 <= dec_deg <= 90.0:
        raise CrossmatchError(
            f"row {index}: dec_deg must be within [-90, 90], got {dec_deg!r}"
        )
    matched = _field(row, "matched", index)
    if not isinstance(matched, bool):
        raise TypeError(f"row {index}: matched must be a bool, got {matched!r}")
    source_id: int | None = None
    if _has(row, "source_id"):
        raw_source_id = _field(row, "source_id", index)
        if raw_source_id is not None:
            if isinstance(raw_source_id, bool) or not isinstance(raw_source_id, int):
                raise CrossmatchError(
                    f"row {index}: source_id must be an int or None, got {raw_source_id!r}"
                )
            if not _INT64_MIN <= raw_source_id <= _INT64_MAX:
                raise CrossmatchError(
                    f"row {index}: source_id does not fit a SQLite INTEGER, "
                    f"got {raw_source_id!r}"
                )
            source_id = raw_source_id
    values: dict[str, float | None] = {}
    for name in _OPTIONAL_FLOAT_FIELDS:
        raw = _field(row, name, index) if _has(row, name) else None
        values[name] = _finite_float(raw, what=name, index=index, allow_none=True)
    separation = values["separation_arcsec"]
    if separation is not None and separation < 0:
        raise CrossmatchError(
            f"row {index}: separation_arcsec must be >= 0, got {separation!r}"
        )
    return CrossmatchRecord(
        frame_id=frame_id,
        x=x,
        y=y,
        ra_deg=ra_deg,
        dec_deg=dec_deg,
        matched=matched,
        source_id=source_id,
        g=values["g"],
        bp=values["bp"],
        rp=values["rp"],
        parallax=values["parallax"],
        parallax_error=values["parallax_error"],
        pmra=values["pmra"],
        pmra_error=values["pmra_error"],
        pmdec=values["pmdec"],
        pmdec_error=values["pmdec_error"],
        separation_arcsec=separation,
    )


def save_crossmatch(
    project: Project, frame_id: int, rows: Iterable[Any]
) -> tuple[CrossmatchRecord, ...]:
    """Save (replace) the whole cross-match table of the frame ``frame_id``
    in ``project`` and return the saved rows as a tuple of
    :class:`CrossmatchRecord`, in input order.

    ``rows`` is an iterable of duck-typed rows — mappings or objects with
    attributes (e.g. :class:`~seestar_controller.gaia.matching.CrossmatchRow`).
    Required per row: ``x``, ``y``, ``ra_deg``, ``dec_deg``, ``matched``;
    optional (None default): ``source_id``, ``g``, ``bp``, ``rp``,
    ``parallax``, ``parallax_error``, ``pmra``, ``pmra_error``, ``pmdec``,
    ``pmdec_error``, ``separation_arcsec``.

    The frame-exists check, the delete of the frame's previous rows, and the
    inserts all run in ONE transaction (``with project.db:``). Raises
    :class:`~.errors.CrossmatchError` when the frame does not exist (the
    "missing stack" case) or a row value is invalid — in both cases nothing
    is written. Structural type errors (``frame_id`` not an int, ``rows`` not
    an iterable, ``matched`` not a bool) raise :class:`TypeError`.
    """
    frame_id = _check_frame_id(frame_id)
    if isinstance(rows, (str, bytes)) or not isinstance(rows, Iterable):
        raise TypeError(f"rows must be an iterable of crossmatch rows, got {rows!r}")
    records = tuple(
        _validate_row(row, index, frame_id) for index, row in enumerate(rows)
    )
    with project.db:  # one transaction: the frame check, the delete, the inserts
        row = project.db.execute(
            "SELECT 1 FROM frames WHERE id = ?", (frame_id,)
        ).fetchone()
        if row is None:
            raise CrossmatchError(
                f"project {project.slug!r} has no frame {frame_id!r} — "
                "a cross-match table needs an existing stack (see store_frame)"
            )
        project.db.execute("DELETE FROM crossmatch WHERE frame_id = ?", (frame_id,))
        project.db.executemany(
            "INSERT INTO crossmatch "
            "(frame_id, x, y, ra_deg, dec_deg, matched, source_id, g, bp, rp, "
            "parallax, parallax_error, pmra, pmra_error, pmdec, pmdec_error, "
            "separation_arcsec) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    r.frame_id,
                    r.x,
                    r.y,
                    r.ra_deg,
                    r.dec_deg,
                    int(r.matched),
                    r.source_id,
                    r.g,
                    r.bp,
                    r.rp,
                    r.parallax,
                    r.parallax_error,
                    r.pmra,
                    r.pmra_error,
                    r.pmdec,
                    r.pmdec_error,
                    r.separation_arcsec,
                )
                for r in records
            ],
        )
    return records


def load_crossmatch(project: Project, frame_id: int) -> tuple[CrossmatchRecord, ...]:
    """The persisted cross-match table of the frame ``frame_id`` in
    ``project`` as a tuple of :class:`CrossmatchRecord`, in insertion order.

    Returns an empty tuple when the frame has no table — an unknown frame id
    is a filter, not an error (same semantics as
    :func:`.quality.frame_quality_for_frame` returning None).
    """
    frame_id = _check_frame_id(frame_id)
    rows = project.db.execute(
        f"SELECT {_CROSSMATCH_COLUMNS} FROM crossmatch WHERE frame_id = ? ORDER BY id",
        (frame_id,),
    ).fetchall()
    return tuple(_row_to_record(row) for row in rows)


def list_crossmatched_stacks(project: Project) -> tuple[CrossmatchedStack, ...]:
    """The frames of ``project`` that have a persisted cross-match table, as
    a tuple of :class:`CrossmatchedStack` summaries ordered by frame id.

    The summary joins the ``frames`` row (session id + kind) with the row and
    matched counts of the frame's ``crossmatch`` rows.
    """
    rows = project.db.execute(
        "SELECT f.id, f.session_id, f.kind, COUNT(c.id), COALESCE(SUM(c.matched), 0) "
        "FROM frames f JOIN crossmatch c ON c.frame_id = f.id "
        "GROUP BY f.id ORDER BY f.id"
    ).fetchall()
    return tuple(
        CrossmatchedStack(
            frame_id=int(row[0]),
            session_id=str(row[1]),
            kind=str(row[2]),
            row_count=int(row[3]),
            matched_count=int(row[4]),
        )
        for row in rows
    )
