"""Package boundary tests for seestar_controller.report — the pure core must
never import GTK (``gi``) or the widget/link/observing layers, enforced in a
fresh subprocess like photometry's boundary test, plus a source scan."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPORT_PACKAGE_DIR = (
    Path(__file__).resolve().parent.parent / "src" / "seestar_controller" / "report"
)

#: The report package's modules (the exact set — a new module must be added
#: here deliberately).
REPORT_MODULES = {"__init__", "data", "plots", "thumbnail", "html", "generate"}

#: Packages the pure report core must never import (gi + the layers that pull
#: in gi/link: link, ui, enhance, photometry, observing).
FORBIDDEN_PACKAGES = ("gi", "link", "ui", "enhance", "photometry", "observing")


def test_report_package_imports_no_gi_no_link_no_ui_no_enhance_no_photometry_no_observing():
    code = (
        "import sys;"
        "import seestar_controller.report;"
        "import seestar_controller.report.data;"
        "import seestar_controller.report.plots;"
        "import seestar_controller.report.thumbnail;"
        "import seestar_controller.report.html;"
        "import seestar_controller.report.generate;"
        "assert 'gi' not in sys.modules, 'GTK imported';"
        "assert 'seestar_controller.link' not in sys.modules, 'link imported eagerly';"
        "assert 'seestar_controller.ui' not in sys.modules, 'ui imported eagerly';"
        "assert 'seestar_controller.enhance' not in sys.modules, 'enhance imported eagerly';"
        "assert 'seestar_controller.photometry' not in sys.modules, 'photometry imported';"
        "assert 'seestar_controller.observing' not in sys.modules, 'observing imported';"
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


def test_report_modules_source_scan_no_forbidden_imports():
    forbidden = re.compile(
        r"^\s*(import|from)\s+(gi\b|seestar_controller\.(link|ui|enhance|photometry|observing)\b)",
        re.MULTILINE,
    )
    for path in sorted(REPORT_PACKAGE_DIR.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        assert not re.search(r"^\s*(import gi|from gi)\b", source, re.MULTILINE), (
            f"{path.name} imports gi"
        )
        assert not forbidden.search(source), f"{path.name} imports a forbidden package"


def test_report_package_module_set():
    modules = {path.stem for path in REPORT_PACKAGE_DIR.glob("*.py")}
    assert modules == REPORT_MODULES


def test_report_public_api_is_exported_from_package():
    from seestar_controller import report

    for name in report.__all__:
        assert hasattr(report, name), name
    for name in (
        "LogEntry",
        "MeasurementsSummary",
        "ReportData",
        "ReportError",
        "collect_report_data",
        "fold_png",
        "generate_report",
        "lightcurve_png",
        "load_session_log",
        "render_html",
        "report_path",
        "session_log_path",
        "stack_thumbnail_png",
        "summarize_measurements",
    ):
        assert name in report.__all__, name
    assert report.REPORTS_DIR == "reports"
    assert report.REPORT_FILE_SUFFIX == ".html"
    assert report.SESSION_LOG_FILE == "session_log.jsonl"
