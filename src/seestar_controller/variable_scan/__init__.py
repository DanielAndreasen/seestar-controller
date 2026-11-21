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
  :class:`~.persist.ScanResultRecord`;
- :mod:`.render` — ticket 02, the pure blink rendering: the star cutout per
  sub-frame (:func:`~.render.render_blink_frames`) and the animated GIF of
  the cutouts (:func:`~.render.render_blink_gif`);
- :mod:`.export` — ticket 02, the candidates CSV export:
  :func:`~.export.export_candidates_csv`;
- :mod:`.controller` — ticket 02, the app-level shared
  :class:`~.controller.ScanController` (ranked candidates + threshold filter
  + selected candidate's lightcurve points/blink + the points sidecar
  :func:`~.controller.save_scan_points` / :func:`~.controller.load_scan_points`)
  with the frozen :class:`~.controller.ScanSnapshot`;
- :mod:`.view` — ticket 02, the WIDGET layer
  (:class:`~.view.VariableScanView`, imports ``gi`` at module top; re-exported
  here lazily via PEP 562 ``__getattr__`` so importing this package stays
  gi-free).
"""

from __future__ import annotations

from .controller import (
    DB_CALL_TIMEOUT_S,
    SCAN_POINTS_FILE,
    SCAN_POINTS_SCHEMA_VERSION,
    InvokeOnMain,
    ScanController,
    ScanListener,
    ScanSnapshot,
    load_scan_points,
    points_path,
    save_scan_points,
)
from .errors import ScanError
from .export import (
    CANDIDATES_CSV_FILE,
    CANDIDATES_CSV_HEADER,
    export_candidates_csv,
)
from .persist import (
    SCAN_RESULTS_TABLE,
    ScanResultRecord,
    latest_scan_results,
    save_scan_results,
    scan_results,
)
from .render import (
    DEFAULT_BLINK_DURATION_MS,
    DEFAULT_CUTOUT_SIZE,
    DEFAULT_HI_PERCENTILE,
    DEFAULT_LO_PERCENTILE,
    extract_cutout,
    render_blink_frames,
    render_blink_gif,
    render_cutout_image,
    tonemap_gray,
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

#: Widget-layer exports, imported lazily (PEP 562): accessing one of these
#: names imports the widget module, which needs ``gi`` (headless-safe import
#: of this package never touches it).
_WIDGET_EXPORTS = {
    "VariableScanView": "view",
}


def __getattr__(name: str):
    if name in _WIDGET_EXPORTS:
        from importlib import import_module

        module = import_module(f".{_WIDGET_EXPORTS[name]}", __name__)
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "CANDIDATES_CSV_FILE",
    "CANDIDATES_CSV_HEADER",
    "DB_CALL_TIMEOUT_S",
    "DEFAULT_BLINK_DURATION_MS",
    "DEFAULT_CUTOUT_SIZE",
    "DEFAULT_EXCLUDE_RADIUS_PX",
    "DEFAULT_HI_PERCENTILE",
    "DEFAULT_LO_PERCENTILE",
    "DEFAULT_MIN_FRAMES",
    "DEFAULT_THRESHOLD",
    "ENSEMBLE_COMPS",
    "ENSEMBLE_FIELD_MEDIAN",
    "SCAN_POINTS_FILE",
    "SCAN_POINTS_SCHEMA_VERSION",
    "SCAN_RESULTS_TABLE",
    "THRESHOLD_CONFIG_KEY",
    "FieldStar",
    "InvokeOnMain",
    "LightcurvePoint",
    "ScanController",
    "ScanError",
    "ScanListener",
    "ScanReport",
    "ScanResultRecord",
    "ScanSnapshot",
    "SkippedScanFrame",
    "StarScanResult",
    # widget layer (lazy, imports gi):
    "VariableScanView",
    "export_candidates_csv",
    "extract_cutout",
    "latest_scan_results",
    "load_scan_points",
    "load_scan_threshold",
    "mad",
    "median",
    "points_path",
    "render_blink_frames",
    "render_blink_gif",
    "render_cutout_image",
    "save_scan_points",
    "save_scan_results",
    "scan_project",
    "scan_results",
    "set_scan_threshold",
    "tonemap_gray",
    "validate_threshold",
    "variability_index",
]
