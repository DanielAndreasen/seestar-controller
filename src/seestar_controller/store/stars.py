"""Star picks — the persisted photometry star selections of a project.

Pure-Python (module boundary per ``.scratch/project-store/spec.md``): no GTK
imports, no :mod:`seestar_controller.link` imports, stdlib only.

A **star pick** is one star the user picked on an image for photometry: the
**target star** (GLOSSARY.md — the usually variable star whose brightness is
monitored), a **comparison star (comp)** used as reference, or the **check
star** (a comp-like star measured against the ensemble to reveal systematics
— check-star-plot ticket 01; it is EXCLUDED from the comp ensemble). A pick
carries its role, a user-given ``label`` (e.g. ``"TIC 123"`` or ``"comp A"``), its
pixel position (``x``/``y``, image coordinates), optionally its sky position
(``ra_deg``/``dec_deg``, J2000 degrees — nullable, picks may be pixel-only),
and its aperture settings (``aperture_radius``, ``sky_annulus_inner`` /
``sky_annulus_outer``, all in pixels).

API (all take the open :class:`~.project.Project` handle first):

- :func:`save_star_picks` — replace ALL picks of the project, returns the
  saved picks;
- :func:`load_star_picks` — the picks as :class:`StarPick` records, target
  first, then the check star, then comps by label.

Documented choices:

- **Dual write: db is authoritative, ``project.json`` is the mirror.** Picks
  live in the ``stars`` table of the project db (see :mod:`.schema`) — that is
  the queryable source of truth, and :func:`load_star_picks` reads ONLY the
  db. On every save, a human-readable copy is also written into
  ``project.json`` as ``config['star_picks']`` (shape
  ``{"target": {...} | null, "comps": [{...}, ...], "check": {...} | null}``,
  the placeholder the spec reserves for star picks) via
  :meth:`.project.Project.save_config`. The db transaction runs FIRST (one
  transaction: ``DELETE FROM stars`` + the new rows); the mirror is written
  after. If the mirror write fails, the db still holds the picks — the db wins
  on any conflict, and the next save rewrites the mirror.
- **Replace semantics.** :func:`save_star_picks` REPLACES all existing picks:
  one transaction does ``DELETE FROM stars`` then inserts the new rows, so a
  rejected or failed save leaves the old picks untouched (rollback) and a
  successful save never mixes old and new picks. Saving an EMPTY list is
  allowed and clears all picks (mirror becomes ``{"target": null, "comps":
  [], "check": null}``).
- **Exactly one target, at most one check.** A non-empty selection must contain
  exactly one ``target`` pick — zero targets (comps only) or more than one
  target raise :class:`~.errors.StarPickError`. At most one ``check`` pick is
  allowed (the check-star-plot spec proposes a single check star; a second
  check raises :class:`~.errors.StarPickError`). Comps are unconstrained
  (``comps >= 0``). The empty list is the one zero-target case allowed (it
  clears).
- **Aperture is per-pick, not project-global.** The ticket says picks carry
  the aperture radius + sky annulus, so every ``stars`` row stores its own
  ``aperture_radius`` / ``sky_annulus_inner`` / ``sky_annulus_outer`` (the
  schema has one column set per pick) — different stars may need different
  apertures. There is no project-global aperture setting.
- **Ordering.** :func:`load_star_picks` returns the target first, then the
  check star, then the comps ordered by ``label`` (unnamed picks — ``label``
  NULL — last), ties broken by the row ``id`` (insertion order).
- **Validation** (all raise :class:`~.errors.StarPickError`, which is both a
  :class:`~.errors.ProjectError` and a :class:`ValueError`, with messages
  naming the field and value): ``role`` must be ``"target"``, ``"comp"`` or
  ``"check"``; ``label`` a string or None; ``x``/``y`` required finite floats
  (image coords); ``ra_deg``/``dec_deg`` None or finite floats with
  ``ra_deg`` in ``[0, 360)`` and ``dec_deg`` in ``[-90, 90]`` (the J2000 ranges
  used by ``catalog.coords.J2000Coord``); ``aperture_radius`` positive;
  annulus radii positive with ``sky_annulus_inner < sky_annulus_outer``.
  Field validation runs in :class:`StarPick.__post_init__`, so both save and
  load validate every pick; the ensemble rules (exactly one target, at most
  one check) are checked by :func:`save_star_picks` before anything is
  written.

.. note::
   The ``check`` role is stored in the EXISTING ``stars.role`` column
   (check-star-plot ticket 01): the 4 -> 5 migration in :mod:`.schema`
   rebuilds the ``stars`` table with the role CHECK widened to also allow
   ``'check'``, so both fresh and pre-existing project databases accept it.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .errors import StarPickError
from .project import Project

#: Pick role: the **target star** (GLOSSARY.md).
ROLE_TARGET = "target"
#: Pick role: a **comparison star (comp)** (GLOSSARY.md).
ROLE_COMP = "comp"
#: Pick role: the **check star** — measured against the comp ensemble like the
#: target, but EXCLUDED from the ensemble itself (check-star-plot ticket 01).
ROLE_CHECK = "check"
#: The valid pick roles.
ROLES = (ROLE_TARGET, ROLE_COMP, ROLE_CHECK)

#: The StarPick fields, in dataclass order (also the db column order after id).
STAR_PICK_FIELDS = (
    "role",
    "label",
    "x",
    "y",
    "ra_deg",
    "dec_deg",
    "aperture_radius",
    "sky_annulus_inner",
    "sky_annulus_outer",
)

#: Columns read/written in the ``stars`` table (after ``id``).
_STARS_COLUMNS = ", ".join(STAR_PICK_FIELDS)

_INSERT_SQL = (
    f"INSERT INTO stars ({_STARS_COLUMNS}) "
    f"VALUES ({', '.join('?' for _ in STAR_PICK_FIELDS)})"
)

#: Load order: target first, then the check star, then comps by label
#: (NULLs last), ties by id.
_SELECT_SQL = (
    f"SELECT {_STARS_COLUMNS} FROM stars "
    "ORDER BY CASE role WHEN 'target' THEN 0 WHEN 'check' THEN 1 ELSE 2 END, "
    "label IS NULL, label, id"
)


def normalize_role(role: Any) -> str:
    """Validate a pick ``role`` ("target", "comp" or "check"); raise
    :class:`~.errors.StarPickError` otherwise."""
    if role not in ROLES:
        raise StarPickError(f"role must be one of {ROLES}, got {role!r}")
    return str(role)


def _number(value: Any, *, what: str) -> float:
    """Coerce ``value`` to a finite float (bools rejected); raise
    :class:`~.errors.StarPickError` with a clear message otherwise."""
    if isinstance(value, bool):
        raise StarPickError(f"{what} must be a number, got {value!r}")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise StarPickError(f"{what} must be a number, got {value!r}") from None
    if not math.isfinite(result):
        raise StarPickError(f"{what} must be finite, got {value!r}")
    return result


@dataclass(frozen=True)
class StarPick:
    """One saved star pick (target, comp or check) of a project.

    - ``role`` — ``"target"``, ``"comp"`` or ``"check"`` (see :data:`ROLES`);
    - ``label`` — the user-given name (e.g. ``"TIC 123"``, ``"comp A"``), or
      None when unnamed;
    - ``x`` / ``y`` — the pixel position in image coordinates (required,
      finite floats);
    - ``ra_deg`` / ``dec_deg`` — the sky position in J2000 degrees, or None
      (picks may be pixel-only);
    - ``aperture_radius`` — the photometry aperture radius in pixels
      (positive);
    - ``sky_annulus_inner`` / ``sky_annulus_outer`` — the sky annulus radii
      in pixels (positive, inner < outer).

    All fields are validated (and numeric fields coerced to float) in
    ``__post_init__`` — an invalid pick cannot be constructed; violations
    raise :class:`~.errors.StarPickError`.
    """

    role: str
    label: str | None
    x: float
    y: float
    ra_deg: float | None
    dec_deg: float | None
    aperture_radius: float
    sky_annulus_inner: float
    sky_annulus_outer: float

    def __post_init__(self) -> None:
        role = normalize_role(self.role)
        if self.label is not None and not isinstance(self.label, str):
            raise StarPickError(
                f"label must be a string or None, got {self.label!r}"
            )
        x = _number(self.x, what="x")
        y = _number(self.y, what="y")
        ra_deg = None if self.ra_deg is None else _number(self.ra_deg, what="ra_deg")
        dec_deg = None if self.dec_deg is None else _number(self.dec_deg, what="dec_deg")
        if ra_deg is not None and not 0.0 <= ra_deg < 360.0:
            raise StarPickError(
                f"ra_deg must be in [0, 360) degrees, got {self.ra_deg!r}"
            )
        if dec_deg is not None and not -90.0 <= dec_deg <= 90.0:
            raise StarPickError(
                f"dec_deg must be in [-90, 90] degrees, got {self.dec_deg!r}"
            )
        aperture_radius = _number(self.aperture_radius, what="aperture_radius")
        if aperture_radius <= 0.0:
            raise StarPickError(
                f"aperture_radius must be positive, got {self.aperture_radius!r}"
            )
        inner = _number(self.sky_annulus_inner, what="sky_annulus_inner")
        outer = _number(self.sky_annulus_outer, what="sky_annulus_outer")
        if inner <= 0.0 or outer <= 0.0:
            raise StarPickError(
                f"sky annulus radii must be positive, got "
                f"sky_annulus_inner={self.sky_annulus_inner!r}, "
                f"sky_annulus_outer={self.sky_annulus_outer!r}"
            )
        if not inner < outer:
            raise StarPickError(
                f"sky_annulus_inner ({inner}) must be smaller than "
                f"sky_annulus_outer ({outer})"
            )
        # frozen dataclass: set the coerced values directly
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "x", x)
        object.__setattr__(self, "y", y)
        object.__setattr__(self, "ra_deg", ra_deg)
        object.__setattr__(self, "dec_deg", dec_deg)
        object.__setattr__(self, "aperture_radius", aperture_radius)
        object.__setattr__(self, "sky_annulus_inner", inner)
        object.__setattr__(self, "sky_annulus_outer", outer)

    def to_dict(self) -> dict[str, Any]:
        """This pick as a plain JSON-serializable dict (all fields, in
        :data:`STAR_PICK_FIELDS` order) — the shape stored in the
        ``project.json`` mirror."""
        return {field: getattr(self, field) for field in STAR_PICK_FIELDS}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> StarPick:
        """Build a :class:`StarPick` from a mapping (e.g. one read back from
        the ``project.json`` mirror). Missing keys raise
        :class:`~.errors.StarPickError`; extra keys are ignored."""
        if not isinstance(data, Mapping):
            raise StarPickError(f"star pick must be a mapping, got {data!r}")
        missing = [field for field in STAR_PICK_FIELDS if field not in data]
        if missing:
            raise StarPickError(f"star pick dict is missing keys {missing}: {data!r}")
        return cls(**{field: data[field] for field in STAR_PICK_FIELDS})


def _row_to_pick(row: sqlite3.Row | tuple[Any, ...]) -> StarPick:
    return StarPick(**{field: row[i] for i, field in enumerate(STAR_PICK_FIELDS)})


def _mirror_dict(picks: list[StarPick]) -> dict[str, Any]:
    """The ``project.json`` mirror of ``picks``:
    ``{"target": {...} | None, "comps": [{...}, ...], "check": {...} | None}``."""
    target: dict[str, Any] | None = None
    comps: list[dict[str, Any]] = []
    check: dict[str, Any] | None = None
    for pick in picks:
        if pick.role == ROLE_TARGET:
            target = pick.to_dict()
        elif pick.role == ROLE_CHECK:
            check = pick.to_dict()
        else:
            comps.append(pick.to_dict())
    return {"target": target, "comps": comps, "check": check}


def save_star_picks(project: Project, picks: Iterable[StarPick]) -> list[StarPick]:
    """Replace ALL star picks of ``project`` with ``picks`` and return them.

    - ``picks`` — an iterable of :class:`StarPick` (anything else raises
      :class:`TypeError`). A non-empty selection must contain EXACTLY ONE
      ``"target"`` pick (zero targets or several targets raise
      :class:`~.errors.StarPickError`) and AT MOST ONE ``"check"`` pick (a
      second check star raises :class:`~.errors.StarPickError` — the
      check-star-plot spec proposes a single check star); comps are
      unconstrained. An EMPTY iterable is allowed and clears all picks.

    The db write is ONE transaction (``with project.db:``): ``DELETE FROM
    stars`` followed by the inserts of the new rows — a failure rolls back
    and leaves the old picks untouched, a success never mixes old and new.
    Afterwards a human-readable copy is persisted into ``project.json`` as
    ``config['star_picks']`` via :meth:`.project.Project.save_config` (the db
    stays the authoritative source — see the module docstring).

    Raises :class:`~.errors.StarPickError` on an invalid ensemble (before
    anything is written) or when the db write fails (rolled back, chained).
    """
    picks = list(picks)
    for pick in picks:
        if not isinstance(pick, StarPick):
            raise TypeError(
                f"picks must contain only StarPick instances, got {pick!r}"
            )
    targets = [pick for pick in picks if pick.role == ROLE_TARGET]
    if picks and len(targets) != 1:
        raise StarPickError(
            f"a non-empty star-pick selection must contain exactly one "
            f"{ROLE_TARGET!r} pick, got {len(targets)} (project {project.slug!r}) "
            f"— pass an empty list to clear all picks"
        )
    checks = [pick for pick in picks if pick.role == ROLE_CHECK]
    if len(checks) > 1:
        raise StarPickError(
            f"a star-pick selection may contain at most one {ROLE_CHECK!r} pick, "
            f"got {len(checks)} (project {project.slug!r})"
        )

    try:
        with project.db:  # one transaction: DELETE + all inserts, or nothing
            project.db.execute("DELETE FROM stars")
            for pick in picks:
                project.db.execute(
                    _INSERT_SQL, tuple(getattr(pick, field) for field in STAR_PICK_FIELDS)
                )
    except sqlite3.DatabaseError as exc:
        raise StarPickError(
            f"could not save star picks in project {project.slug!r}: {exc}"
        ) from exc

    # dual write: mirror into project.json (db already committed and stays
    # authoritative — load_star_picks reads the db only)
    project.config["star_picks"] = _mirror_dict(picks)
    project.save_config()
    return picks


def load_star_picks(project: Project) -> list[StarPick]:
    """All star picks of ``project`` as :class:`StarPick` records, read from
    the ``stars`` table (the authoritative source — the ``project.json``
    mirror is NOT consulted).

    Ordered: the target pick first, then the check pick, then the comps by
    ``label`` (unnamed picks last), ties broken by the row ``id`` (insertion
    order). An empty project yields ``[]``.
    """
    rows = project.db.execute(_SELECT_SQL).fetchall()
    return [_row_to_pick(row) for row in rows]
