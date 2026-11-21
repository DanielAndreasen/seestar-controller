"""Package boundary tests for seestar_controller.astrometry — the pure core
must never import GTK (``gi``) or the widget/link layers, and must stay
astropy-free at import time (astropy is lazy inside functions), enforced in
a fresh subprocess plus a source scan."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ASTROMETRY_PACKAGE_DIR = (
    Path(__file__).resolve().parent.parent / "src" / "seestar_controller" / "astrometry"
)


def test_astrometry_package_imports_no_gi_no_link_no_ui_no_astropy():
    code = (
        "import sys;"
        "import seestar_controller.astrometry;"
        "import seestar_controller.astrometry.errors;"
        "import seestar_controller.astrometry.wcs;"
        "import seestar_controller.astrometry.solve;"
        "import seestar_controller.astrometry.session;"
        "assert 'gi' not in sys.modules, 'GTK imported';"
        "assert 'seestar_controller.link' not in sys.modules, 'link imported eagerly';"
        "assert 'seestar_controller.ui' not in sys.modules, 'ui imported eagerly';"
        "assert 'seestar_controller.enhance' not in sys.modules, 'enhance imported eagerly';"
        "assert 'seestar_controller.photometry' not in sys.modules, 'photometry imported';"
        "assert 'astropy' not in sys.modules, 'astropy imported at module level';"
        "assert 'photutils' not in sys.modules, 'photutils imported at module level';"
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
        assert not re.search(r"^\s*(import gi|from gi)\b", source, re.MULTILINE), (
            f"{path.name} imports gi"
        )
        assert not re.search(
            r"^\s*(import|from)\s+seestar_controller\.(link|ui|enhance|photometry)\b",
            source,
            re.MULTILINE,
        ), f"{path.name} imports link/ui/enhance/photometry"
        # astropy/photutils must be lazy: no module-level (column-0) import
        assert not re.search(r"^(import|from)\s+(astropy|photutils)\b", source, re.MULTILINE), (
            f"{path.name} imports astropy/photutils at module level"
        )


def test_astrometry_package_module_set():
    modules = {path.stem for path in ASTROMETRY_PACKAGE_DIR.glob("*.py")}
    assert modules == {"__init__", "errors", "wcs", "solve", "session"}


def test_astrometry_public_api_is_exported_from_package():
    from seestar_controller import astrometry

    for name in astrometry.__all__:
        assert hasattr(astrometry, name), name
    for name in (
        "AstrometryError",
        "SolveError",
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
    ):
        assert name in astrometry.__all__, name
    assert issubclass(astrometry.SolveError, ValueError)
    assert issubclass(astrometry.SolveError, astrometry.AstrometryError)
