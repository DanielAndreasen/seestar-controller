"""Variable scan — automatic variability scan of the field stars.

Pure Python package (no ``gi`` — a subprocess boundary test enforces it;
see ``.scratch/variable-scan/spec.md``): take the Gaia cross-match source
list of a stack (gaia-crossmatch ticket 02,
:mod:`seestar_controller.store.crossmatch`) as the field-star picks, run
the existing aperture-photometry pipeline
(:mod:`seestar_controller.photometry.measure`) over ALL sub-frames of ALL
sessions of a project, build a differential lightcurve per field star
(against the picked comp ensemble, falling back to the leave-one-out field
median when no comps are picked), compute the robust scatter (MAD) and the
median photometric error per star, and rank the variable candidates — the
picked target/comps/check are excluded. Results are persisted in the
project db (``scan_results`` table, schema v7) so they accumulate across
nights; the threshold is a project setting (``project.json``).

Modules:

- :mod:`.errors` — :class:`~.errors.ScanError`;
- :mod:`.stats` — the pure robust math: :func:`~.stats.median`,
  :func:`~.stats.mad`, :func:`~.stats.variability_index`;
- :mod:`.settings` — the persisted threshold setting:
  :func:`~.settings.load_scan_threshold` / :func:`~.settings.set_scan_threshold`;
- :mod:`.scan` — the core: :func:`~.scan.scan_project` and the records
  (:class:`~.scan.ScanReport`, :class:`~.scan.StarScanResult`,
  :class:`~.scan.FieldStar`, :class:`~.scan.LightcurvePoint`,
  :class:`~.scan.SkippedScanFrame`);
- :mod:`.persist` — the db persistence: :func:`~.persist.save_scan_results`,
  :func:`~.persist.scan_results`, :func:`~.persist.latest_scan_results`,
  :class:`~.persist.ScanResultRecord`.
"""

from __future__ import annotations

from .errors import ScanError
from .persist import (
    SCAN_RESULTS_TABLE,
    ScanResultRecord,
    latest_scan_results,
    save_scan_results,
    scan_results,
)
from .scan import (
    DEFAULT_EXCLUDE_RADIUS_PX,
    ENSEMBLE_COMPS,
    ENSEMBLE_FIELD_MEDIAN,
    FieldStar,
    LightcurvePoint,
    ScanReport,
    SkippedScanFrame,
    StarScanResult,
    scan_project,
)
from .settings import (
    DEFAULT_MIN_FRAMES,
    DEFAULT_THRESHOLD,
    THRESHOLD_CONFIG_KEY,
    load_scan_threshold,
    set_scan_threshold,
    validate_threshold,
)
from .stats import mad, median, variability_index

__all__ = [
    "DEFAULT_EXCLUDE_RADIUS_PX",
    "DEFAULT_MIN_FRAMES",
    "DEFAULT_THRESHOLD",
    "ENSEMBLE_COMPS",
    "ENSEMBLE_FIELD_MEDIAN",
    "SCAN_RESULTS_TABLE",
    "THRESHOLD_CONFIG_KEY",
    "FieldStar",
    "LightcurvePoint",
    "ScanError",
    "ScanReport",
    "ScanResultRecord",
    "SkippedScanFrame",
    "StarScanResult",
    "latest_scan_results",
    "load_scan_threshold",
    "mad",
    "median",
    "save_scan_results",
    "scan_project",
    "scan_results",
    "set_scan_threshold",
    "validate_threshold",
    "variability_index",
]
