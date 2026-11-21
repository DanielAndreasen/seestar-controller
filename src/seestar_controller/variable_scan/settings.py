"""The variable-scan settings persisted in the project's ``project.json``.

Pure Python — no GTK, no link. The **threshold** (the variability-ratio
cutoff above which a field star is flagged as a variable candidate) is a
project setting, stored as ``config['variable_scan_threshold']`` and
persisted atomically via :meth:`~seestar_controller.store.project.Project.save_config`
— the same pattern as the found period (``config['period']``,
:mod:`seestar_controller.analysis.fold`).

- :data:`THRESHOLD_CONFIG_KEY` — the ``project.json`` key;
- :data:`DEFAULT_THRESHOLD` — used when the setting is missing or corrupt;
- :data:`DEFAULT_MIN_FRAMES` — the default minimum-frame count of a scan
  (a per-call argument of :func:`~.scan.scan_project`, not persisted);
- :func:`validate_threshold` — validate a threshold value (raises
  :class:`~.errors.ScanError`);
- :func:`load_scan_threshold` — the project's threshold (default when
  missing/corrupt — a corrupt value is ignored, never a crash);
- :func:`set_scan_threshold` — validate + persist a new threshold.
"""

from __future__ import annotations

import math
from typing import Any

from seestar_controller.store.project import Project

from .errors import ScanError

#: The ``project.json`` config key of the variable-scan threshold.
THRESHOLD_CONFIG_KEY = "variable_scan_threshold"

#: The default threshold: a star is a variable candidate when its robust
#: scatter (MAD) is at least this many times its median photometric error.
DEFAULT_THRESHOLD = 3.0

#: The default minimum number of frames a star must be measured on before
#: it can be flagged (a per-call argument of :func:`~.scan.scan_project`).
DEFAULT_MIN_FRAMES = 5


def _check_project(project: Any) -> Project:
    """Validate that ``project`` is an open
    :class:`~seestar_controller.store.project.Project` handle;
    :class:`TypeError` otherwise."""
    if not isinstance(project, Project):
        raise TypeError(
            "project must be an open seestar_controller.store Project handle "
            f"(got {type(project).__name__})"
        )
    return project


def validate_threshold(threshold: Any) -> float:
    """Validate a scan ``threshold`` and return it as a float.

    A threshold must be a finite number > 0 (bools rejected); anything else
    raises :class:`~.errors.ScanError` naming the value.
    """
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        raise ScanError(f"threshold must be a number, got {threshold!r}")
    result = float(threshold)
    if not math.isfinite(result):
        raise ScanError(f"threshold must be finite, got {threshold!r}")
    if not result > 0.0:
        raise ScanError(f"threshold must be > 0, got {threshold!r}")
    return result


def load_scan_threshold(project: Any) -> float:
    """The variable-scan threshold of ``project``: the persisted
    ``config['variable_scan_threshold']`` when it is a valid threshold,
    :data:`DEFAULT_THRESHOLD` when the key is missing, null, or corrupt
    (a corrupt value is ignored — never a crash, same as the period)."""
    project = _check_project(project)
    raw = project.config.get(THRESHOLD_CONFIG_KEY)
    if raw is None:
        return DEFAULT_THRESHOLD
    try:
        return validate_threshold(raw)
    except ScanError:
        return DEFAULT_THRESHOLD


def set_scan_threshold(project: Any, threshold: Any) -> float:
    """Validate ``threshold`` and persist it into the project's
    ``project.json`` (``config['variable_scan_threshold']``, atomic write
    via :meth:`~seestar_controller.store.project.Project.save_config`);
    return the validated float.

    Raises :class:`~.errors.ScanError` for an invalid threshold (nothing is
    written then) and :class:`TypeError` for a non-Project handle.
    """
    project = _check_project(project)
    value = validate_threshold(threshold)
    project.config[THRESHOLD_CONFIG_KEY] = value
    project.save_config()
    return value
