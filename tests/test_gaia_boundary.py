"""Package boundary tests for seestar_controller.gaia — the pure core must
never import GTK (``gi``) or the widget/link/ui/enhance layers, enforced in a
fresh subprocess like photometry's boundary test, plus a source scan."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

GAIA_PACKAGE_DIR = (
    Path(__file__).resolve().parent.parent / "src" / "seestar_controller" / "gaia"
)

#: Pure modules must never import gi / link / ui / enhance; the widget module
#: (overlay_view) is the single exception and must import gi at the top,
#: before any ``from gi.repository`` import.
PURE_MODULES = {
    "__init__",
    "errors",
    "detect",
    "query",
    "matching",
    "persistence",
    "overlay",
}
WIDGET_MODULES = {"overlay_view"}


def test_gaia_package_imports_no_gi_no_link_no_ui_no_enhance():
    code = (
        "import sys;"
        "import seestar_controller.gaia;"
        "import seestar_controller.gaia.detect;"
        "import seestar_controller.gaia.query;"
        "import seestar_controller.gaia.matching;"
        "import seestar_controller.gaia.persistence;"
        "import seestar_controller.gaia.errors;"
        "import seestar_controller.gaia.overlay;"
        "assert 'gi' not in sys.modules, 'GTK imported';"
        "assert 'seestar_controller.gaia.overlay_view' not in sys.modules, "
        "'widget module imported eagerly';"
        "assert 'seestar_controller.link' not in sys.modules, 'link imported eagerly';"
        "assert 'seestar_controller.ui' not in sys.modules, 'ui imported eagerly';"
        "assert 'seestar_controller.enhance' not in sys.modules, 'enhance imported eagerly';"
        "assert 'seestar_controller.photometry' not in sys.modules, 'photometry imported';"
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


def test_gaia_modules_source_scan_no_gi_import():
    for path in sorted(GAIA_PACKAGE_DIR.glob("*.py")):
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


def test_gaia_package_module_set():
    modules = {path.stem for path in GAIA_PACKAGE_DIR.glob("*.py")}
    assert modules == PURE_MODULES | WIDGET_MODULES


def test_gaia_widget_exports_lazy():
    from seestar_controller import gaia

    assert gaia._WIDGET_EXPORTS == {"CrossmatchOverlayView": "overlay_view"}
    assert "CrossmatchOverlayView" in gaia.__all__
    # accessing the widget name must NOT happen at import time; only the
    # attribute lookup triggers the lazy import (and needs gi):
    assert "overlay_view" not in sys.modules or "gi" in sys.modules


def test_gaia_public_api_is_exported_from_package():
    from seestar_controller import gaia

    for name in gaia.__all__:
        if name in gaia._WIDGET_EXPORTS:
            continue  # lazy widget export: importing it needs gi (headless here)
        assert hasattr(gaia, name), name
    for name in (
        "DetectedSource",
        "GaiaStar",
        "CrossmatchRow",
        "GaiaError",
        "DetectionError",
        "WcsMissingError",
        "GaiaUnavailableError",
        "GaiaResponseError",
        "detect_sources",
        "estimate_fwhm",
        "pixel_to_sky",
        "field_center_and_radius",
        "build_cone_query",
        "fetch_gaia_tap_json",
        "parse_gaia_response",
        "query_gaia",
        "query_gaia_cached",
        "gaia_cache_key",
        "default_cache_dir",
        "crossmatch",
        "crossmatch_stack",
        "save_crossmatch",
        "load_crossmatch",
        "list_crossmatched_stacks",
        "tonemap_gray",
        "render_stack_overlay",
        "CrossmatchController",
        "CrossmatchSnapshot",
        "CrossmatchOverlayView",
    ):
        assert name in gaia.__all__, name
