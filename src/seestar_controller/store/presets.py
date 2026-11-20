"""Per-image enhancement presets — the saved pipeline parameters of a frame.

Pure-Python (module boundary per ``.scratch/project-store/spec.md``): no GTK
imports, no :mod:`seestar_controller.link` imports, stdlib only.

A **preset** (image-enhance ticket 04) is the saved enhancement pipeline of
ONE stored frame (a stack): when the image is opened again in the enhance
view, the preset is reapplied instead of the default pipeline. Presets live
in the ``presets`` table of the project db (see :mod:`.schema`, migration
2 -> 3): ``frame_id`` (TEXT primary key — the ``frames`` row id as a string),
``pipeline`` (the pipeline as a JSON dict, the same shape
:meth:`seestar_controller.enhance.pipeline.Pipeline.to_dict` produces), and
``updated_at`` (UTC ISO-8601, :func:`.project.utc_now_iso`).

API (all take the open :class:`~.project.Project` handle first):

- :func:`save_preset` — save/replace the preset of one frame, returns the
  :class:`Preset`;
- :func:`load_preset` — the preset of one frame, or None when it has none;
- :func:`delete_preset` — remove one frame's preset, returns whether a row
  was deleted;
- :func:`list_presets` — all presets of the project, ordered by frame id.

Documented choices:

- **Frame must exist.** :func:`save_preset` checks the ``frames`` row inside
  the same transaction as the write and raises :class:`~.errors.PresetError`
  naming the frame id — a clear error instead of silently storing a preset
  for a frame that is gone.
- **JSON-able pipeline.** ``pipeline`` must be a mapping that
  :func:`json.dumps` can serialize (dicts/lists/str/num/bool/None); anything
  else raises :class:`~.errors.PresetError` BEFORE anything is written.
  Structural type errors (``frame_id`` not an int) raise :class:`TypeError`.
- **Upsert.** Saving twice for the same frame replaces the preset
  (``INSERT OR REPLACE``) and refreshes ``updated_at`` — one preset per
  frame, the latest wins.
- **Reads are tolerant.** :func:`load_preset` returns None when the frame has
  no preset; a corrupt row (invalid JSON, or JSON that is not a dict) raises
  :class:`~.errors.PresetError` — silent data loss is worse than a loud
  error. :func:`delete_preset` on a frame without a preset is not an error
  (it returns False, same semantics as :func:`.measurements.set_excluded`
  being an update, not a lookup).
- **Writes** — every mutation runs in one transaction (``with project.db:``)
  on the db-owning thread (the same rule as every other store module: the
  connection is not thread-safe, so all access happens on the thread that
  opened the project).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .errors import PresetError
from .project import Project, utc_now_iso

#: Columns selected for a :class:`Preset`, in field order.
_PRESET_COLUMNS = "frame_id, pipeline, updated_at"


@dataclass(frozen=True)
class Preset:
    """One saved enhancement preset of a frame (the ``presets`` table row).

    - ``frame_id`` — the ``frames`` row id the preset belongs to;
    - ``pipeline`` — the pipeline as a JSON-able dict (the
      :meth:`~seestar_controller.enhance.pipeline.Pipeline.to_dict` shape);
    - ``updated_at`` — the last save time as a UTC ISO-8601 string
      (``+00:00``, millisecond precision).
    """

    frame_id: int
    pipeline: dict[str, Any]
    updated_at: str


def _row_to_preset(row: sqlite3.Row | tuple[Any, ...]) -> Preset:
    pipeline_raw = str(row[1])
    try:
        pipeline = json.loads(pipeline_raw)
    except (TypeError, ValueError) as exc:
        raise PresetError(
            f"preset of frame {row[0]!r} has a corrupt pipeline JSON: {exc}"
        ) from exc
    if not isinstance(pipeline, dict):
        raise PresetError(
            f"preset of frame {row[0]!r} is not a JSON object: {pipeline_raw!r}"
        )
    return Preset(frame_id=int(row[0]), pipeline=pipeline, updated_at=str(row[2]))


def _check_frame_id(frame_id: Any) -> int:
    """Validate a frame id argument (an int, bools rejected);
    :class:`TypeError` otherwise."""
    if isinstance(frame_id, bool) or not isinstance(frame_id, int):
        raise TypeError(f"frame_id must be an int, got {frame_id!r}")
    return frame_id


def _check_pipeline(pipeline: Any) -> dict[str, Any]:
    """Validate the ``pipeline`` argument: a mapping that serializes to JSON
    (else :class:`PresetError`, before anything is written)."""
    if not isinstance(pipeline, Mapping):
        raise PresetError(
            f"pipeline must be a mapping (a Pipeline.to_dict() dict), got {pipeline!r}"
        )
    try:
        json.dumps(dict(pipeline))
    except (TypeError, ValueError) as exc:
        raise PresetError(f"pipeline is not JSON-serializable: {exc}") from exc
    return dict(pipeline)


def save_preset(project: Project, frame_id: int, pipeline: Mapping[str, Any]) -> Preset:
    """Save (or replace) the enhancement preset of the frame ``frame_id`` in
    ``project`` and return it as a :class:`Preset`.

    - ``frame_id`` — the ``frames`` row id; must exist, else
      :class:`~.errors.PresetError` is raised (before anything is written);
    - ``pipeline`` — the pipeline as a JSON-able mapping (the
      :meth:`~seestar_controller.enhance.pipeline.Pipeline.to_dict` shape).

    The frame-exists check and the upsert run in ONE transaction
    (``with project.db:``). Saving again for the same frame replaces the
    preset and refreshes ``updated_at``.
    """
    frame_id = _check_frame_id(frame_id)
    pipeline_dict = _check_pipeline(pipeline)
    pipeline_json = json.dumps(pipeline_dict, sort_keys=True)
    with project.db:  # one transaction: the frame check and the upsert together
        row = project.db.execute(
            "SELECT 1 FROM frames WHERE id = ?", (frame_id,)
        ).fetchone()
        if row is None:
            raise PresetError(
                f"project {project.slug!r} has no frame {frame_id!r} — "
                "presets need an existing frame (see store_frame)"
            )
        updated_at = utc_now_iso()
        project.db.execute(
            "INSERT OR REPLACE INTO presets (frame_id, pipeline, updated_at) "
            "VALUES (?, ?, ?)",
            (str(frame_id), pipeline_json, updated_at),
        )
    return Preset(frame_id=frame_id, pipeline=pipeline_dict, updated_at=updated_at)


def load_preset(project: Project, frame_id: int) -> Preset | None:
    """The enhancement preset of the frame ``frame_id`` in ``project`` as a
    :class:`Preset`, or None when the frame has no preset.

    An unknown frame id is a filter, not an error — it yields None (same
    semantics as :func:`.frames.list_frames` with an unknown session). A
    corrupt preset row raises :class:`~.errors.PresetError`.
    """
    frame_id = _check_frame_id(frame_id)
    row = project.db.execute(
        f"SELECT {_PRESET_COLUMNS} FROM presets WHERE frame_id = ?", (str(frame_id),)
    ).fetchone()
    if row is None:
        return None
    return _row_to_preset(row)


def delete_preset(project: Project, frame_id: int) -> bool:
    """Delete the enhancement preset of the frame ``frame_id`` in ``project``.

    Returns True when a preset row was deleted, False when the frame had none
    (deleting a missing preset is not an error). The delete runs in one
    transaction (``with project.db:``).
    """
    frame_id = _check_frame_id(frame_id)
    with project.db:
        cur = project.db.execute(
            "DELETE FROM presets WHERE frame_id = ?", (str(frame_id),)
        )
    return cur.rowcount > 0


def list_presets(project: Project) -> list[Preset]:
    """All enhancement presets of ``project`` as :class:`Preset` records,
    ordered by frame id (numeric, not lexicographic). An empty project yields
    ``[]``."""
    rows = project.db.execute(
        f"SELECT {_PRESET_COLUMNS} FROM presets ORDER BY CAST(frame_id AS INTEGER)"
    ).fetchall()
    return [_row_to_preset(row) for row in rows]
