"""Package boundary tests for seestar_controller.variable_scan — the pure
core must never import GTK (``gi``) or the widget/link layers, enforced in a
fresh subprocess like photometry's/gaia's/moving-objects' boundary test, plus
a source scan and the module-set / public-API checks."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

VARIABLE_SCAN_PACKAGE_DIR = (
    Path(__file__).resolve().parent.parent / "src" / "seestar_controller" / "variable_scan"
)


def test_variable_scan_package_imports_no_gi_no_link_no_ui_no_enhance():
    code = (
        "import sys;"
        "import seestar_controller.variable_scan;"
        "import seestar_controller.variable_scan.errors;"
        "import seestar_controller.variable_scan.stats;"
        "import seestar_controller.variable_scan.settings;"
        "import seestar_controller.variable_scan.scan;"
        "import seestar_controller.variable_scan.persist;"
        "import seestar_controller.variable_scan.render;"
        "import seestar_controller.variable_scan.export;"
        "import seestar_controller.variable_scan.controller;"
        "assert 'gi' not in sys.modules, 'GTK imported';"
        "assert 'seestar_controller.link' not in sys.modules, 'link imported eagerly';"
        "assert 'seestar_controller.ui' not in sys.modules, 'ui imported eagerly';"
        "assert 'seestar_controller.enhance' not in sys.modules, 'enhance imported eagerly';"
        "assert 'seestar_controller.variable_scan.view' not in sys.modules, ("
        "'widget module imported eagerly');"
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


#: Pure modules must never import gi / link / ui / enhance; the widget module
#: (view) is the single exception and must import gi at the top, before any
#: ``from gi.repository`` import.
PURE_MODULES = {
    "__init__",
    "errors",
    "stats",
    "settings",
    "scan",
    "persist",
    "render",
    "export",
    "controller",
}
WIDGET_MODULES = {"view"}


def test_variable_scan_modules_source_scan_no_gi_import():
    for path in sorted(VARIABLE_SCAN_PACKAGE_DIR.glob("*.py")):
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
            r"^\s*(import|from)\s+seestar_controller\.(link|ui|enhance)\b",
            source,
            re.MULTILINE,
        ), f"{path.name} imports link/ui/enhance"


def test_variable_scan_package_module_set():
    modules = {path.stem for path in VARIABLE_SCAN_PACKAGE_DIR.glob("*.py")}
    assert modules == PURE_MODULES | WIDGET_MODULES


def test_variable_scan_widget_exports_lazy():
    from seestar_controller import variable_scan

    assert variable_scan._WIDGET_EXPORTS == {"VariableScanView": "view"}
    assert "VariableScanView" in variable_scan.__all__
    # accessing the widget name must NOT happen at import time; only the
    # attribute lookup triggers the lazy import (and needs gi):
    assert "view" not in sys.modules or "gi" in sys.modules


def test_variable_scan_public_api_is_exported_from_package():
    from seestar_controller import variable_scan

    for name in variable_scan.__all__:
        if name in variable_scan._WIDGET_EXPORTS:
            continue  # lazy widget export: importing it needs gi (headless here)
        assert hasattr(variable_scan, name), name
    for name in (
        "ScanError",
        "FieldStar",
        "LightcurvePoint",
        "StarScanResult",
        "SkippedScanFrame",
        "ScanReport",
        "ScanResultRecord",
        "ScanController",
        "ScanSnapshot",
        "scan_project",
        "save_scan_results",
        "scan_results",
        "latest_scan_results",
        "load_scan_threshold",
        "set_scan_threshold",
        "validate_threshold",
        "median",
        "mad",
        "variability_index",
        "DEFAULT_THRESHOLD",
        "DEFAULT_MIN_FRAMES",
        "DEFAULT_EXCLUDE_RADIUS_PX",
        "ENSEMBLE_COMPS",
        "ENSEMBLE_FIELD_MEDIAN",
        "SCAN_RESULTS_TABLE",
        "THRESHOLD_CONFIG_KEY",
        "render_blink_frames",
        "render_blink_gif",
        "render_cutout_image",
        "extract_cutout",
        "tonemap_gray",
        "export_candidates_csv",
        "save_scan_points",
        "load_scan_points",
        "points_path",
    ):
        assert name in variable_scan.__all__, name
