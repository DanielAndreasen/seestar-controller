"""Fit-parameter persistence — the PURE layer of transit-fit ticket 01
(:mod:`seestar_controller.transit.persist`).

This is a PURE module: **no ``gi`` import** (the sandbox has no GTK, see
docs/wiki/assumptions.md; a subprocess test enforces the boundary) — stdlib +
:mod:`seestar_controller.store` (duck-typed: anything with a ``config`` dict
and ``save_config()``, i.e. :class:`seestar_controller.store.project.Project`)
only. No :mod:`seestar_controller.link`, no ``ui``, no ``photometry``, no
``enhance`` imports, and no network.

What lives here:

- :data:`FIT_CONFIG_KEY` — the ``project.json`` config key (``"transit_fit"``)
  under which the last fit is persisted. ``project.json`` is a free-form
  config document (only ``name`` / ``slug`` / ``created_at`` /
  ``schema_version`` are required), so this needs NO store schema bump — the
  same pattern as the persisted ``period`` of period-analysis;
- :func:`save_fit` — write a :class:`~.fit.FitResult` into the project config
  (plus a ``fitted_at`` UTC timestamp) and atomically save ``project.json``
  via ``project.save_config()``;
- :func:`load_fit` — read the persisted fit back into a
  :class:`~.fit.FitResult`; None when no fit was saved yet,
  :class:`~.model.TransitError` when the persisted value is corrupt;
- :func:`export_fit_json` — export a fit result as a standalone JSON file
  (atomic write: temp file in the same directory + fsync + :func:`os.replace`).
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .fit import FitResult
from .model import TransitError

#: The ``project.json`` config key of the persisted transit fit.
FIT_CONFIG_KEY = "transit_fit"


def save_fit(project: Any, result: FitResult) -> None:
    """Persist ``result`` into the project config (``config['transit_fit']``,
    plus a ``fitted_at`` UTC ISO timestamp) and atomically save
    ``project.json`` via ``project.save_config()``.

    Raises :class:`TransitError` when ``result`` is not a
    :class:`~.fit.FitResult`.
    """
    if not isinstance(result, FitResult):
        raise TransitError(f"save_fit expects a FitResult, got {result!r}")
    payload = result.to_dict()
    payload["fitted_at"] = datetime.now(UTC).isoformat()
    project.config[FIT_CONFIG_KEY] = payload
    project.save_config()


def load_fit(project: Any) -> FitResult | None:
    """Load the persisted fit from ``project.config['transit_fit']``.

    Returns None when no fit was saved yet (missing or null key);
    raises :class:`TransitError` when the persisted value is corrupt.
    """
    raw = project.config.get(FIT_CONFIG_KEY)
    if raw is None:
        return None
    try:
        return FitResult.from_dict(raw)
    except TransitError:
        raise
    except Exception as exc:
        raise TransitError(
            f"corrupt persisted transit fit of project "
            f"{getattr(project, 'slug', project)!r}: {exc}"
        ) from exc


def _atomic_write_json(path: Path, obj: Any) -> Path:
    """Write ``obj`` as pretty JSON to ``path`` atomically (temp file in the
    same directory, flush + fsync, then :func:`os.replace`; on any failure the
    temp file is removed and the destination is left untouched)."""
    data = (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
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
    return path


def export_fit_json(result: FitResult, path: str | Path) -> Path:
    """Export ``result`` as a standalone JSON file at ``path`` (atomic write)
    and return the path. Raises :class:`TransitError` when ``result`` is not a
    :class:`~.fit.FitResult`."""
    if not isinstance(result, FitResult):
        raise TransitError(f"export_fit_json expects a FitResult, got {result!r}")
    return _atomic_write_json(Path(path), result.to_dict())
