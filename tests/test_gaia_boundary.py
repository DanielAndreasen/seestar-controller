"""Package boundary tests for seestar_controller.gaia — the pure core must
never import GTK (``gi``) or the widget/link layers, enforced in a fresh
subprocess like photometry's boundary test, plus a source scan."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

GAIA_PACKAGE_DIR = (
    Path(__file__).resolve().parent.parent / "src" / "seestar_controller" / "gaia"
)


def test_gaia_package_imports_no_gi_no_link_no_ui_no_enhance():
    code = (
        "import sys;"
        "import seestar_controller.gaia;"
        "import seestar_controller.gaia.detect;"
        "import seestar_controller.gaia.query;"
        "import seestar_controller.gaia.matching;"
        "import seestar_controller.gaia.persistence;"
        "import seestar_controller.gaia.errors;"
        "assert 'gi' not in sys.modules, 'GTK imported';"
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
    assert modules == {"__init__", "errors", "detect", "query", "matching", "persistence"}


def test_gaia_public_api_is_exported_from_package():
    from seestar_controller import gaia

    for name in gaia.__all__:
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
    ):
        assert name in gaia.__all__, name
