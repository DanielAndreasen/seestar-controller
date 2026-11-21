"""Persisted isochrone overlay parameters — the project's chosen PARSEC
isochrone (age, [Fe/H], distance modulus), in the project db.

Pure-Python (module boundary per ``.scratch/project-store/spec.md``): no GTK
imports, no :mod:`seestar_controller.link` imports, stdlib only.

An **isochrone-params record** (hr-diagram ticket 02) is the persisted choice
of isochrone overlay parameters of the WHOLE project: ``age``
(``log10(age/yr)`` — the PARSEC convention), ``feh`` ([Fe/H]) and ``dist_mod``
(the distance modulus in mag), plus the update time. The record lives in the
``isochrone_params`` table of the project db (see :mod:`.schema`, migration
8 -> 9) as a SINGLETON row (``id`` fixed to 1) — there is no frame FK, the
parameters belong to the project, not to one frame.

API (all take the open :class:`~.project.Project` handle first):

- :func:`set_isochrone_params` — validated upsert of the singleton row in ONE
  transaction, returns the saved :class:`IsochroneParams`;
- :func:`isochrone_params` — the saved record, or None when never set.

Documented choices:

- **Upsert.** Setting the parameters again replaces the singleton row and
  refreshes ``updated_at`` — the latest choice wins.
- **Validation** (all raise :class:`~.errors.IsochroneStoreError` with the
  field and value in the message, BEFORE anything is written): ``age`` is a
  finite number in ``[6.0, 10.5]`` (log10 yr, ~1 Myr .. ~30 Gyr); ``feh`` is
  a finite number in ``[-4.0, 0.5]``; ``dist_mod`` is a finite number in
  ``[-5.0, 30.0]`` mag. Structural type errors (non-numbers, bools) raise
  :class:`TypeError`. The ranges mirror the pure
  :mod:`seestar_controller.hr_diagram.isochrone` module (kept in sync by
  hand — the store never imports hr_diagram).
- **Reads are tolerant of a missing row.** :func:`isochrone_params` returns
  None when no parameters were ever saved (a fresh project). A corrupt row
  raises :class:`~.errors.IsochroneStoreError`.
- **Writes** — the upsert runs in one transaction (``with project.db:``), so
  a rejected save leaves no partial row behind.
"""

from __future__ import annotations

import math
import numbers
import sqlite3
from dataclasses import dataclass
from typing import Any

from .errors import IsochroneStoreError
from .project import Project, utc_now_iso

#: Parameter validation ranges (mirror of hr_diagram.isochrone's constants).
MIN_AGE_LOG = 6.0
MAX_AGE_LOG = 10.5
MIN_FEH = -4.0
MAX_FEH = 0.5
MIN_DIST_MOD = -5.0
MAX_DIST_MOD = 30.0

#: Columns selected for an :class:`IsochroneParams`, in field order.
_SELECT_SQL = "SELECT age, feh, dist_mod, updated_at FROM isochrone_params WHERE id = 1"


@dataclass(frozen=True)
class IsochroneParams:
    """The persisted isochrone overlay parameters of a project (the singleton
    ``isochrone_params`` row).

    - ``age`` — ``log10(age / yr)`` (the PARSEC convention);
    - ``feh`` — [Fe/H];
    - ``dist_mod`` — the distance modulus in mag;
    - ``updated_at`` — the update time as a UTC ISO-8601 string.
    """

    age: float
    feh: float
    dist_mod: float
    updated_at: str


def _row_to_params(row: sqlite3.Row | tuple[Any, ...]) -> IsochroneParams:
    try:
        return IsochroneParams(
            age=float(row[0]),
            feh=float(row[1]),
            dist_mod=float(row[2]),
            updated_at=str(row[3]),
        )
    except (TypeError, ValueError) as exc:
        raise IsochroneStoreError(
            f"corrupt isochrone_params row {tuple(row)!r}: {exc}"
        ) from exc


def _check_param(value: Any, name: str, lo: float, hi: float) -> float:
    """Validate one parameter: a real number (bools rejected —
    :class:`TypeError`) in ``[lo, hi]`` and finite
    (:class:`~.errors.IsochroneStoreError`); returns it as a float."""
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise TypeError(f"{name} must be a number, got {value!r}")
    result = float(value)
    if not math.isfinite(result):
        raise IsochroneStoreError(f"{name} must be finite, got {value!r}")
    if not lo <= result <= hi:
        raise IsochroneStoreError(f"{name} must be in [{lo}, {hi}], got {value!r}")
    return result


def set_isochrone_params(
    project: Project,
    *,
    age: float,
    feh: float,
    dist_mod: float,
    updated_at: str | None = None,
) -> IsochroneParams:
    """Save (or replace) the project's isochrone overlay parameters and
    return them as an :class:`IsochroneParams`.

    - ``age`` — ``log10(age / yr)``, a finite number in
      ``[6.0, 10.5]``;
    - ``feh`` — [Fe/H], a finite number in ``[-4.0, 0.5]``;
    - ``dist_mod`` — the distance modulus in mag, a finite number in
      ``[-5.0, 30.0]``;
    - ``updated_at`` — the update time as a UTC ISO-8601 string; None
      (default) uses :func:`.project.utc_now_iso`.

    All validation happens BEFORE anything is written; the upsert runs in
    ONE transaction (``with project.db:``). Setting the parameters again
    replaces the singleton row and refreshes ``updated_at``.
    """
    age_value = _check_param(age, "age", MIN_AGE_LOG, MAX_AGE_LOG)
    feh_value = _check_param(feh, "feh", MIN_FEH, MAX_FEH)
    dist_mod_value = _check_param(dist_mod, "dist_mod", MIN_DIST_MOD, MAX_DIST_MOD)
    if updated_at is None:
        updated_at = utc_now_iso()
    elif not isinstance(updated_at, str) or not updated_at:
        raise IsochroneStoreError(
            f"updated_at must be a non-empty string or None, got {updated_at!r}"
        )
    with project.db:  # one transaction
        project.db.execute(
            "INSERT OR REPLACE INTO isochrone_params (id, age, feh, dist_mod, updated_at) "
            "VALUES (1, ?, ?, ?, ?)",
            (age_value, feh_value, dist_mod_value, updated_at),
        )
    return IsochroneParams(
        age=age_value, feh=feh_value, dist_mod=dist_mod_value, updated_at=updated_at
    )


def isochrone_params(project: Project) -> IsochroneParams | None:
    """The project's saved isochrone overlay parameters as an
    :class:`IsochroneParams`, or None when no parameters were ever saved
    (a fresh project — a missing row is not an error). A corrupt row raises
    :class:`~.errors.IsochroneStoreError`.
    """
    row = project.db.execute(_SELECT_SQL).fetchone()
    if row is None:
        return None
    return _row_to_params(row)
