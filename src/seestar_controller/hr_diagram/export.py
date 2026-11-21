"""CSV export of a cross-match table — the PURE layer of hr-diagram ticket 01.

This is a PURE module: **no ``gi`` import** (the sandbox has no GTK, see
docs/wiki/assumptions.md; a subprocess test enforces the boundary) — stdlib
(:mod:`csv`, :mod:`os`, :mod:`tempfile`) + the sibling pure module
:mod:`.core` only. No :mod:`seestar_controller.link`, no :mod:`ui`, no
:mod:`seestar_controller.store` import (the table is duck-typed, see
:mod:`.core`).

What lives here:

- :class:`ExportError` — the error type (an
  :class:`~seestar_controller.hr_diagram.core.HrError`, i.e. a
  :class:`ValueError`);
- :func:`export_table_csv` — the whole cross-match table as a CSV file
  (detections + matched Gaia fields + the computed ``bp_rp`` / ``abs_g`` /
  ``member`` columns), following the :mod:`seestar_controller.analysis.export`
  conventions.

Documented choices (mirroring :mod:`seestar_controller.analysis.export` /
:mod:`seestar_controller.store.csv_export`):

- **Header** — exactly :data:`CSV_HEADER`, in this order, UTF-8,
  ``\\n`` line terminator (:class:`csv.writer` with ``lineterminator="\\n"``).
- **Rows** — one per input row, in input order (matched AND unmatched — the
  export is the whole table). Floats are ``repr(float)`` (round-trip exactly
  through ``float()``), ``None`` fields are EMPTY, booleans (``matched``,
  ``member``) are written as ``1`` / ``0`` (the db convention). The computed
  columns: ``bp_rp`` = ``bp - rp`` (empty when either is missing),
  ``abs_g`` = :func:`~seestar_controller.hr_diagram.core.absolute_g` (empty
  when there is no positive parallax), ``member`` from the membership filter.
- **Membership.** ``membership`` defaults to
  :func:`~seestar_controller.hr_diagram.core.membership_filter` applied to
  the same table.
- **Atomic write.** Same pattern as the store exporter (re-declared here:
  this package must not import the store's private helpers): temp file in
  the SAME directory + flush + fsync + :func:`os.replace`, temp removed on
  any failure incl. :class:`KeyboardInterrupt` — a crash never leaves a
  partial CSV and no ``*.tmp`` is left behind.
- **Errors.** :class:`TypeError` for bad argument types (``path`` not
  str/Path, ``table`` not an iterable, ``membership`` not a Membership);
  :class:`ExportError` for a row missing a required field (``x`` / ``y`` /
  ``ra_deg`` / ``dec_deg`` / ``matched`` — the same required fields as
  :func:`seestar_controller.store.crossmatch.save_crossmatch`) and for a
  non-positive-finite ``parallax_snr``; :class:`OSError` from the write
  itself propagates (previous file intact).
"""

from __future__ import annotations

import csv
import os
import tempfile
from pathlib import Path
from typing import Any

from .core import (
    HrError,
    _check_membership,
    _check_table,
    _finite,
    _get,
    absolute_g,
    membership_filter,
)


class ExportError(HrError):
    """A CSV-export failure: a table row missing a required field, or an
    invalid ``parallax_snr`` — always a clean :class:`HrError`, never a
    crash."""


#: The CSV header columns, in order — the first line of every export.
CSV_HEADER = (
    "source_id",
    "x",
    "y",
    "ra_deg",
    "dec_deg",
    "matched",
    "g",
    "bp",
    "rp",
    "bp_rp",
    "parallax",
    "parallax_error",
    "pmra",
    "pmra_error",
    "pmdec",
    "pmdec_error",
    "separation_arcsec",
    "abs_g",
    "member",
)

#: The fields every exported row must carry (the crossmatch required fields).
_REQUIRED_FIELDS = ("x", "y", "ra_deg", "dec_deg", "matched")


def _check_path(path: Any) -> Path:
    """Validate the required ``path`` argument (str or Path);
    :class:`TypeError` otherwise."""
    if not isinstance(path, (str, Path)):
        raise TypeError(f"path must be a string or a Path, got {path!r}")
    return Path(path)


def _float_field(value: Any) -> str:
    """One float column: ``repr(float)`` (exact round-trip) or EMPTY when the
    value is None / not a finite number."""
    finite = _finite(value)
    return "" if finite is None else repr(finite)


def _row_strings(row: Any, index: int, member_ids: frozenset[int]) -> list[str]:
    """One duck-typed table row as a CSV row of strings (see the module
    docstring). Raises :class:`ExportError` when a required field is missing."""
    for name in _REQUIRED_FIELDS:
        if _get(row, name) is None:
            raise ExportError(
                f"table row {index} is missing the required field {name!r} "
                f"(got {row!r})"
            )
    matched = _get(row, "matched")
    if not isinstance(matched, bool):
        raise ExportError(f"table row {index}: matched must be a bool, got {matched!r}")
    source_id = _get(row, "source_id")
    if source_id is not None and (isinstance(source_id, bool) or not isinstance(source_id, int)):
        raise ExportError(
            f"table row {index}: source_id must be an int or None, got {source_id!r}"
        )
    g = _finite(_get(row, "g"))
    bp = _finite(_get(row, "bp"))
    rp = _finite(_get(row, "rp"))
    parallax = _finite(_get(row, "parallax"))
    bp_rp = "" if g is None or bp is None or rp is None else repr(bp - rp)
    abs_g = "" if g is None else _float_field(absolute_g(g, parallax))
    member = "1" if source_id in member_ids else "0"
    return [
        "" if source_id is None else str(source_id),
        _float_field(_get(row, "x")),
        _float_field(_get(row, "y")),
        _float_field(_get(row, "ra_deg")),
        _float_field(_get(row, "dec_deg")),
        "1" if matched else "0",
        _float_field(_get(row, "g")),
        _float_field(_get(row, "bp")),
        _float_field(_get(row, "rp")),
        bp_rp,
        _float_field(_get(row, "parallax")),
        _float_field(_get(row, "parallax_error")),
        _float_field(_get(row, "pmra")),
        _float_field(_get(row, "pmra_error")),
        _float_field(_get(row, "pmdec")),
        _float_field(_get(row, "pmdec_error")),
        _float_field(_get(row, "separation_arcsec")),
        abs_g,
        member,
    ]


def _atomic_write_csv(dest: Path, header: tuple[str, ...], rows: list[list[str]]) -> None:
    """Write the CSV ATOMICALLY (the store exporter's pattern): temp file in
    the same directory + flush + fsync + :func:`os.replace`, temp removed on
    any failure incl. :class:`KeyboardInterrupt`. UTF-8, ``\\n`` terminator."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=dest.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh, lineterminator="\n")
            writer.writerow(header)
            for row in rows:
                writer.writerow(row)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.close(fd)  # already closed when the with-block exited
        except OSError:
            pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def export_table_csv(
    table: Any,
    path: str | Path,
    membership: Any = None,
) -> Path:
    """Export the whole cross-match ``table`` (matched AND unmatched rows, in
    input order) as a CSV file and return its path.

    - ``table`` — an iterable of duck-typed rows (mappings or objects with
      attributes, e.g. :class:`~seestar_controller.store.crossmatch.CrossmatchRecord`);
    - ``path`` — the destination (REQUIRED — there is no project handle here
      to default to);
    - ``membership`` — an explicit :class:`~.core.Membership` for the
      ``member`` column (default: :func:`~.core.membership_filter` applied to
      the same table).

    The header is exactly :data:`CSV_HEADER`; floats are ``repr(float)``
    (exact round-trip), ``None`` fields are empty, ``matched`` / ``member``
    are ``1`` / ``0``, and ``bp_rp`` / ``abs_g`` are the computed columns
    (empty when not computable). The file is written ATOMICALLY (temp file in
    the same directory + fsync + :func:`os.replace`, temp cleaned on any
    failure), so no ``*.tmp`` is left behind and a crash never leaves a
    partial CSV.

    Raises :class:`TypeError` for bad argument types, :class:`ExportError`
    when a row is missing a required field (``x`` / ``y`` / ``ra_deg`` /
    ``dec_deg`` / ``matched``) or has a bad ``matched`` / ``source_id`` value,
    and :class:`OSError` when the write itself fails (previous file intact).
    """
    table_rows = tuple(_check_table(table))
    dest = _check_path(path)
    if membership is None:
        membership = membership_filter(table_rows)
    else:
        membership = _check_membership(membership)
    rows = [
        _row_strings(row, index, membership.member_ids)
        for index, row in enumerate(table_rows)
    ]
    _atomic_write_csv(dest, CSV_HEADER, rows)
    return dest
