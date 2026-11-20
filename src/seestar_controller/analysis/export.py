"""CSV export — the PURE layer of period-analysis ticket 04
(:mod:`seestar_controller.analysis.export`).

This is a PURE module: **no ``gi`` import** (the sandbox has no GTK, see
docs/wiki/assumptions.md; a subprocess test enforces the boundary) — stdlib
(:mod:`csv`, :mod:`os`, :mod:`tempfile`) + :mod:`seestar_controller.store` +
the sibling pure modules :mod:`.lightcurve` / :mod:`.fold` only. No
:mod:`seestar_controller.link`, no :mod:`ui`, no :mod:`photometry`, no
:mod:`enhance` imports. The widget layer (the export buttons on the
Lightcurve view page, :mod:`seestar_controller.ui.views`) imports ``gi`` at
module top and calls into this module.

What lives here:

- :class:`ExportError` — the error type (an
  :class:`~seestar_controller.analysis.lightcurve.AnalysisError`, i.e. a
  :class:`ValueError`);
- :func:`export_lightcurve_csv` — the lightcurve CSV (``jd,utc,mag,mag_err,
  session,target``), a thin wrapper DELEGATING to
  :func:`seestar_controller.store.csv_export.export_csv` (same header, same
  atomic write, same exclusion policy);
- :func:`export_folded_csv` — the phase-folded CSV with an extra ``phase``
  column (``jd,utc,mag,mag_err,session,target,phase``), written from either
  a project handle or a plain iterable of points.

Documented choices:

- **Lightcurve export delegates.** :func:`export_lightcurve_csv` adds no
  formatting of its own: it validates its arguments and calls
  :func:`seestar_controller.store.csv_export.export_csv`, so the file format
  (header :data:`~seestar_controller.store.csv_export.CSV_HEADER`,
  ``repr(float)`` round-trip floats, NULL ``mag_err`` -> empty, exclusion
  policy OMIT, atomic write, default ``<project>/lightcurve.csv``) is exactly
  the store exporter's. Only the error contract is widened: an unexpected
  failure of the store call is wrapped in :class:`ExportError` (chained),
  while :class:`TypeError` (bad argument types) and :class:`OSError` (the
  write itself failed, previous file intact) propagate unchanged — the same
  contract as the store exporter.
- **Folded CSV.** :func:`export_folded_csv` folds the points with
  :func:`~seestar_controller.analysis.fold.fold_points` (so the ``phase``
  column is exactly :func:`~seestar_controller.analysis.fold.phase_of`,
  always in ``[0, 1)``, and ``jd0`` defaults to the first observation = the
  min jd of the INCLUDED points) and writes the rows **sorted by phase**
  (the folded order — ties keep the fold's stable order). The header is
  :data:`FOLDED_CSV_HEADER` = the store header + ``phase`` as the last
  column; all other columns are formatted exactly like the store exporter
  (``repr(float)`` for ``jd``/``mag``/``mag_err``/``phase``, empty ``mag_err``
  when NULL, ``session`` = the session id, ``target`` = the project config's
  target name or the explicit ``target=`` argument). The default destination
  is ``<project>/folded.csv`` (:data:`FOLDED_CSV_FILE`) when a project is
  given; when a plain point iterable is given, ``path`` is REQUIRED (there
  is no project directory to default to) and :class:`ExportError` is raised
  when it is None.
- **Source detection.** ``project_or_points`` is a project handle when it
  has ``db`` / ``config`` / ``path`` attributes (the store's
  :class:`~seestar_controller.store.project.Project`), else it is treated as
  an iterable of duck-typed points (anything with ``jd`` / ``mag`` /
  ``mag_err`` / ``utc`` / ``session_id`` / ``excluded`` —
  :class:`~seestar_controller.analysis.lightcurve.LightcurvePoint` and the
  store's :class:`~seestar_controller.store.measurements.Measurement` both
  qualify). A project's points come from
  :func:`seestar_controller.store.measurements.lightcurve` with the same
  ``include_excluded`` value (default: excluded points OMITTED).
- **Errors.** Structural type errors (``path`` not str/Path/None,
  ``include_excluded`` not a bool, ``measurement`` ids not ints) raise
  :class:`TypeError`, matching the store exporter. Analysis-level failures —
  an invalid ``period``, an empty point set without an explicit ``jd0``,
  points missing required attributes, no ``path`` for a points-only export,
  a store read failure — are wrapped in :class:`ExportError` (chained from
  the cause; :class:`~seestar_controller.analysis.fold.FoldError` from the
  fold math is re-wrapped so callers catch ONE analysis error type).
  :class:`OSError` from the write itself propagates (previous file intact),
  exactly like the store exporter.
- **Atomic write.** Same pattern as
  :func:`seestar_controller.store.csv_export.export_csv` (re-declared here:
  this package must not import the store's private helpers): temp file in
  the SAME directory + flush + fsync + :func:`os.replace`, temp removed on
  any failure incl. :class:`KeyboardInterrupt` — a crash never leaves a
  partial CSV and no ``*.tmp`` is left behind.
"""

from __future__ import annotations

import csv
import os
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from seestar_controller.store.csv_export import export_csv as _store_export_csv
from seestar_controller.store.measurements import lightcurve as _store_lightcurve

from .fold import FoldError, fold_points
from .lightcurve import AnalysisError


class ExportError(AnalysisError):
    """A CSV-export failure: an invalid period/jd0, an empty point set
    without an explicit epoch, points missing required attributes, a missing
    destination for a points-only export, or a store read failure — always a
    clean :class:`AnalysisError`, never a crash."""


#: File name of the default folded export inside a project directory.
FOLDED_CSV_FILE = "folded.csv"

#: The folded CSV header columns, in order — the store header + ``phase``.
FOLDED_CSV_HEADER = ("jd", "utc", "mag", "mag_err", "session", "target", "phase")


def _target_name(project: Any, override: str | None) -> str:
    """The ``target`` column value: the explicit ``override`` when given,
    else the project config's target name (a mapping's ``name`` entry, a
    plain string as-is, else ``""`` — the same rule as the store exporter)."""
    if override is not None:
        if not isinstance(override, str):
            raise TypeError(f"target must be a string or None, got {override!r}")
        return override
    target = project.config.get("target")
    if target is None:
        return ""
    if isinstance(target, str):
        return target
    if isinstance(target, Mapping):
        name = target.get("name")
        return "" if name is None else str(name)
    return ""


def _is_project(obj: Any) -> bool:
    """Whether ``obj`` is a project handle (has ``db`` / ``config`` /
    ``path``) rather than an iterable of points."""
    return hasattr(obj, "db") and hasattr(obj, "config") and hasattr(obj, "path")


def _check_path(path: Any) -> Path | None:
    """Validate the ``path`` argument (None, str, or Path); :class:`TypeError`
    otherwise."""
    if path is None:
        return None
    if not isinstance(path, (str, Path)):
        raise TypeError(f"path must be a string, a Path, or None, got {path!r}")
    return Path(path)


def _check_include_excluded(include_excluded: Any) -> bool:
    if not isinstance(include_excluded, bool):
        raise TypeError(f"include_excluded must be a bool, got {include_excluded!r}")
    return include_excluded


def export_lightcurve_csv(
    project: Any,
    path: str | Path | None = None,
    include_excluded: bool = False,
) -> Path:
    """Export the lightcurve of ``project`` as a CSV file and return its path.

    A thin wrapper DELEGATING to
    :func:`seestar_controller.store.csv_export.export_csv` (same arguments,
    same file format, same atomic write, same default destination
    ``<project>/lightcurve.csv``): the header is exactly
    ``jd,utc,mag,mag_err,session,target``, floats are ``repr(float)``
    (exact round-trip), NULL ``mag_err`` is an empty field, and excluded
    points are OMITTED unless ``include_excluded=True``.

    Raises :class:`TypeError` for bad argument types and :class:`OSError`
    when the write fails (the previous file stays intact) — both propagate
    from the store exporter unchanged; any OTHER store failure is wrapped in
    :class:`ExportError` (chained).
    """
    _check_include_excluded(include_excluded)
    _check_path(path)
    try:
        return _store_export_csv(project, path=path, include_excluded=include_excluded)
    except (TypeError, OSError):
        raise
    except Exception as exc:
        raise ExportError(
            f"cannot export the lightcurve of project "
            f"{getattr(project, 'slug', project)!r}: {exc}"
        ) from exc


def _folded_rows(points: Iterable[Any], period: Any, jd0: Any, target_name: str) -> list[list[str]]:
    """Fold ``points`` by ``period`` (``jd0`` default = first observation) and
    return the CSV rows (sorted by phase, the fold order): the store row
    format plus ``repr(phase)`` as the last column. Fold/validation failures
    are wrapped in :class:`ExportError`."""
    try:
        folded = fold_points(points, period, jd0=jd0)
    except FoldError as exc:
        raise ExportError(f"cannot fold the points for the CSV export: {exc}") from exc
    rows: list[list[str]] = []
    for fp in folded:
        point = fp.point
        try:
            jd = point.jd
            utc = point.utc
            mag = point.mag
            mag_err = point.mag_err
            session_id = point.session_id
        except AttributeError as exc:
            raise ExportError(
                "points must expose jd/utc/mag/mag_err/session_id attributes "
                f"(LightcurvePoint or store Measurement): missing {exc}"
            ) from exc
        rows.append(
            [
                repr(float(jd)),
                str(utc),
                repr(float(mag)),
                "" if mag_err is None else repr(float(mag_err)),
                str(session_id),
                target_name,
                repr(float(fp.phase)),
            ]
        )
    return rows


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


def export_folded_csv(
    project_or_points: Any,
    period: Any,
    path: str | Path | None = None,
    jd0: Any = None,
    include_excluded: bool = False,
    target: str | None = None,
) -> Path:
    """Export the phase-folded lightcurve as a CSV file and return its path.

    - ``project_or_points`` — a project handle (anything with ``db`` /
      ``config`` / ``path``, e.g. the store's
      :class:`~seestar_controller.store.project.Project`): its points come
      from :func:`seestar_controller.store.measurements.lightcurve` with the
      same ``include_excluded`` value, and the default destination is
      ``<project>/folded.csv`` (:data:`FOLDED_CSV_FILE`). Otherwise it is an
      iterable of duck-typed points
      (:class:`~seestar_controller.analysis.lightcurve.LightcurvePoint` or
      store :class:`~seestar_controller.store.measurements.Measurement`) —
      then ``path`` is REQUIRED (:class:`ExportError` when None).
    - ``period`` — the fold period in days (validated by
      :func:`~seestar_controller.analysis.fold.validate_period`);
    - ``path`` — the destination (None = the project default, see above);
    - ``jd0`` — the fold epoch (JD); None (default) = the first observation
      (the min jd of the INCLUDED points);
    - ``include_excluded`` — when False (default), points flagged
      ``excluded`` are OMITTED (the store policy); when True they are
      written too;
    - ``target`` — an explicit ``target`` column value (default: the project
      config's target name, or ``""`` for a points-only export).

    The header is exactly :data:`FOLDED_CSV_HEADER`
    (``jd,utc,mag,mag_err,session,target,phase``); the rows are sorted by
    ``phase`` (the folded order) and the ``phase`` column is
    :func:`~seestar_controller.analysis.fold.phase_of` — always in ``[0, 1)``
    — written as ``repr(float)`` like the other float columns (exact
    round-trip). The file is written ATOMICALLY (temp file in the same
    directory + fsync + :func:`os.replace`, temp cleaned on any failure), so
    no ``*.tmp`` is left behind and a crash never leaves a partial CSV.

    Raises :class:`TypeError` for bad argument types, :class:`ExportError`
    for analysis-level failures (invalid period, empty points without an
    explicit ``jd0``, points missing attributes, missing ``path`` for a
    points-only export, store read failure — all chained from the cause),
    and :class:`OSError` when the write itself fails (previous file intact).
    """
    include_excluded = _check_include_excluded(include_excluded)
    dest = _check_path(path)
    if _is_project(project_or_points):
        project = project_or_points
        target_name = _target_name(project, target)
        if dest is None:
            dest = project.path / FOLDED_CSV_FILE
        try:
            points = _store_lightcurve(project, include_excluded=include_excluded)
        except Exception as exc:
            raise ExportError(
                f"cannot read the lightcurve of project "
                f"{getattr(project, 'slug', project)!r}: {exc}"
            ) from exc
    else:
        target_name = _target_name_from_target_only(target)
        if dest is None:
            raise ExportError(
                "path is required when exporting a plain point iterable "
                "(there is no project directory to default to)"
            )
        try:
            pts = tuple(project_or_points)
        except TypeError as exc:
            raise ExportError(
                f"project_or_points must be a project handle or an iterable of "
                f"points: {exc}"
            ) from exc
        if not include_excluded:
            pts = tuple(p for p in pts if not bool(getattr(p, "excluded", False)))
        points = pts
    rows = _folded_rows(points, period, jd0, target_name)
    _atomic_write_csv(dest, FOLDED_CSV_HEADER, rows)
    return dest


def _target_name_from_target_only(target: str | None) -> str:
    """The ``target`` column for a points-only export (no project config)."""
    if target is None:
        return ""
    if not isinstance(target, str):
        raise TypeError(f"target must be a string or None, got {target!r}")
    return target
