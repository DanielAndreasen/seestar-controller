"""Persistence glue: store a cross-match table into the project db (issue 02).

Pure-Python glue between the gaia-crossmatch pure core and the project store
(module boundary per ``.scratch/gaia-crossmatch/spec.md``): no GTK imports,
no :mod:`seestar_controller.link` imports — the only seestar import is the
pure :mod:`seestar_controller.store` (which itself imports neither GTK nor
link). A subprocess boundary test enforces this.

The store side (:mod:`seestar_controller.store.crossmatch`, schema v6) owns
the ``crossmatch`` table and the validated transactional API; this module is
the thin gaia-flavored wrapper over it, so the pipeline output of issue 01 —
a list of :class:`~.matching.CrossmatchRow` — can be persisted directly:

- :func:`save_crossmatch` — persist (replace) the cross-match table of one
  stack in the project, in one transaction;
- :func:`load_crossmatch` — read a stack's persisted table back;
- :func:`list_crossmatched_stacks` — which stacks of the project have a
  persisted table.

Row mapping: the store rows are duck-typed, so a
:class:`~.matching.CrossmatchRow` passes as-is — its ``x``/``y``/
``ra_deg``/``dec_deg``/``matched``/``source_id``/``g``/``bp``/``rp``/
``parallax``/``pmra``/``pmdec``/``separation_arcsec`` map one-to-one. The
error columns (``parallax_error``, ``pmra_error``, ``pmdec_error``) are
optional on the input rows: a plain ``CrossmatchRow`` has no such attributes,
so they are stored as NULL; any row object that does carry them (e.g. a Gaia
row with the error columns selected) has them persisted.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from seestar_controller.store import (
    CrossmatchedStack,
    CrossmatchRecord,
    Project,
)
from seestar_controller.store import (
    list_crossmatched_stacks as _store_list_crossmatched_stacks,
)
from seestar_controller.store import (
    load_crossmatch as _store_load_crossmatch,
)
from seestar_controller.store import (
    save_crossmatch as _store_save_crossmatch,
)

__all__ = [
    "list_crossmatched_stacks",
    "load_crossmatch",
    "save_crossmatch",
]


def save_crossmatch(
    project: Project, frame_id: int, rows: Iterable[Any]
) -> tuple[CrossmatchRecord, ...]:
    """Persist the cross-match table of the stack ``frame_id`` in ``project``.

    ``rows`` is the pipeline output — an iterable of
    :class:`~.matching.CrossmatchRow` (or any duck-typed row with the same
    fields; ``parallax_error``/``pmra_error``/``pmdec_error`` are optional
    and default to NULL). Saving again for the same stack replaces its whole
    table in one transaction. Returns the saved rows as
    :class:`~seestar_controller.store.CrossmatchRecord` tuples.

    Raises :class:`~seestar_controller.store.CrossmatchError` when the stack
    (frame) does not exist or a row is invalid — nothing is written then.
    """
    return _store_save_crossmatch(project, frame_id, rows)


def load_crossmatch(project: Project, frame_id: int) -> tuple[CrossmatchRecord, ...]:
    """The persisted cross-match table of the stack ``frame_id`` in
    ``project`` (empty tuple when it has none)."""
    return _store_load_crossmatch(project, frame_id)


def list_crossmatched_stacks(project: Project) -> tuple[CrossmatchedStack, ...]:
    """The stacks of ``project`` that have a persisted cross-match table."""
    return _store_list_crossmatched_stacks(project)
