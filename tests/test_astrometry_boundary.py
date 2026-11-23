"""Package boundary tests for seestar_controller.astrometry — the pure core
must never import GTK (``gi``) or the widget/link/ui/enhance/photometry
layers, and must stay astropy/photutils/matplotlib-free at import time
(those are lazy inside functions), enforced in a fresh subprocess plus a
source scan. The single widget module (``view``) must import ``gi`` at the
top, before any ``from gi.repository`` import, and is exported lazily via
PEP 562."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ASTROMETRY_PACKAGE_DIR = (
    Path(__file__).resolve().parent.parent / "src" / "seestar_controller" / "astrometry"
)

#: Pure modules must never import gi / link / ui / enhance / photometry; the
#: widget module (view) is the single exception and must import gi at the
#: top, before any ``from gi.repository`` import.
PURE_MODULES = {
    "__init__",
    "annotate",
    "controller",
    "drift",
    "errors",
    "export",
    "plot",
    "solve",
    "session",
    "wcs",
}
WIDGET_MODULES = {"view"}


def test_astrometry_package_imports_no_gi_no_link_no_ui_no_astropy():
    code = (
        "import sys;"
        "import seestar_controller.astrometry;"
        "import seestar_controller.astrometry.errors;"
        "import seestar_controller.astrometry.wcs;"
        "import seestar_controller.astrometry.solve;"
        "import seestar_controller.astrometry.session;"
        "import seestar_controller.astrometry.drift;"
        "import seestar_controller.astrometry.annotate;"
        "import seestar_controller.astrometry.export;"
        "import seestar_controller.astrometry.plot;"
        "import seestar_controller.astrometry.controller;"
        "assert 'gi' not in sys.modules, 'GTK imported';"
        "assert 'seestar_controller.astrometry.view' not in sys.modules, "
        "'widget module imported eagerly';"
        "assert 'seestar_controller.link' not in sys.modules, 'link imported eagerly';"
        "assert 'seestar_controller.ui' not in sys.modules, 'ui imported eagerly';"
        "assert 'seestar_controller.enhance' not in sys.modules, 'enhance imported eagerly';"
        "assert 'seestar_controller.photometry' not in sys.modules, 'photometry imported';"
        "assert 'astropy' not in sys.modules, 'astropy imported at module level';"
        "assert 'photutils' not in sys.modules, 'photutils imported at module level';"
        "assert 'matplotlib' not in sys.modules, 'matplotlib imported at module level';"
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


def test_astrometry_modules_source_scan_no_gi_import():
    for path in sorted(ASTROMETRY_PACKAGE_DIR.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        if path.stem in WIDGET_MODULES:
            gi_line = re.search(r"^\s*import gi\b", source, re.MULTILINE)
            assert gi_line is not None, f"{path.name} must import gi at module top"
            first_repo = re.search(r"^\s*from gi\.repository", source, re.MULTILINE)
            assert first_repo is not None, f"{path.name} must use gi.repository"
            assert gi_line.start() < first_repo.start(), (
                f"{path.name}: 'import gi' must come before 'from gi.repository'"
            )
            continue
        assert path.stem in PURE_MODULES, f"unexpected module {path.name}"
        assert not re.search(r"^\s*(import gi|from gi)\b", source, re.MULTILINE), (
            f"{path.name} imports gi"
        )
        assert not re.search(
            r"^\s*(import|from)\s+seestar_controller\.(link|ui|enhance|photometry)\b",
            source,
            re.MULTILINE,
        ), f"{path.name} imports link/ui/enhance/photometry"
        # astropy/photutils/matplotlib must be lazy: no module-level
        # (column-0) import
        assert not re.search(
            r"^(import|from)\s+(astropy|photutils|matplotlib)\b", source, re.MULTILINE
        ), f"{path.name} imports astropy/photutils/matplotlib at module level"


def test_astrometry_package_module_set():
    modules = {path.stem for path in ASTROMETRY_PACKAGE_DIR.glob("*.py")}
    assert modules == PURE_MODULES | WIDGET_MODULES


def test_astrometry_widget_exports_lazy():
    from seestar_controller import astrometry

    assert astrometry._WIDGET_EXPORTS == {"DriftPlotView": "view", "SolvePanel": "view"}
    for name in astrometry._WIDGET_EXPORTS:
        assert name in astrometry.__all__, name
    # accessing the widget names must NOT happen at import time; only the
    # attribute lookup triggers the lazy import (and needs gi):
    assert "seestar_controller.astrometry.view" not in sys.modules or "gi" in sys.modules


def test_astrometry_public_api_is_exported_from_package():
    from seestar_controller import astrometry

    for name in astrometry.__all__:
        if name in astrometry._WIDGET_EXPORTS:
            continue  # lazy widget export: importing it needs gi (headless here)
        assert hasattr(astrometry, name), name
    for name in (
        "AstrometryError",
        "SolveError",
        "ExportError",
        "SolveResult",
        "StarMatch",
        "solve_frame",
        "solve_fits_file",
        "solve_project_frame",
        "solve_session_frame",
        "read_fits_bytes",
        "has_celestial_wcs",
        "header_center",
        "header_pixel_scale_arcsec",
        "provisional_wcs_cards",
        "wcs_to_cards",
        "cards_to_wcs",
        "write_wcs_to_fits",
        "SOLVERS",
        "SOLVER_AUTO",
        "SOLVER_SOLVE_FIELD",
        "SOLVER_GAIA",
        "DriftError",
        "DriftPoint",
        "DriftSeries",
        "PointingError",
        "drift_series",
        "pointing_error",
        "separation_arcsec",
        # astrometry ticket 03: annotated export + solve/drift controllers
        "render_annotated_stack",
        "annotated_png_bytes",
        "fov_corner_sky",
        "scale_bar_arcsec",
        "format_scale_bar",
        "FOV_COLOR",
        "SCALE_BAR_COLOR",
        "NICE_SCALE_BAR_ARCSEC",
        "annotated_stack_png",
        "export_annotated_png",
        "default_export_name",
        "DEFAULT_EXPORT_MAX_SIZE",
        "render_drift_png",
        "DEFAULT_WIDTH_PX",
        "DEFAULT_HEIGHT_PX",
        "DEFAULT_DPI",
        "MAX_RENDER_PX",
        "SolveController",
        "SolveSnapshot",
        "DriftController",
        "DriftSnapshot",
        "DB_CALL_TIMEOUT_S",
        "DriftPlotView",
        "SolvePanel",
        # issue 10: batch plate-solve of a session's unsolved subs
        "SessionSolveReport",
        "SessionWcsCoverage",
        "SolvedFrame",
        "SkippedSolve",
        "SolveProgressCallback",
        "StoreCall",
        "solve_session_unsolved",
        "session_wcs_coverage",
    ):
        assert name in astrometry.__all__, name
    assert issubclass(astrometry.SolveError, ValueError)
    assert issubclass(astrometry.SolveError, astrometry.AstrometryError)
    assert issubclass(astrometry.ExportError, ValueError)
    assert issubclass(astrometry.ExportError, astrometry.AstrometryError)
    assert issubclass(astrometry.DriftError, ValueError)
    assert issubclass(astrometry.DriftError, astrometry.AstrometryError)
