"""Package-boundary tests for :mod:`seestar_controller.stacking` (sub-frames
issue 03): the package is a STRICT PURE layer — a subprocess import test
plus a source scan enforce that nothing imports ``gi`` / the link / ui /
enhance packages, and that the heavy :mod:`astropy` import stays lazy
(inside functions), the same pattern as tests/test_mosaic_stitch.py.
"""

import re
import subprocess
import sys
from pathlib import Path

from seestar_controller import stacking

STACKING_PACKAGE_DIR = Path(stacking.__file__).resolve().parent

PURE_STACKING_MODULES = ("core", "errors", "session")


def test_stacking_package_imports_no_gi_no_link_no_ui_no_enhance_no_astropy():
    code = (
        "import sys;"
        "import seestar_controller.stacking;"
        "import seestar_controller.stacking.core;"
        "import seestar_controller.stacking.errors;"
        "import seestar_controller.stacking.session;"
        "assert 'gi' not in sys.modules, 'GTK imported';"
        "assert 'seestar_controller.link' not in sys.modules, 'link imported eagerly';"
        "assert 'seestar_controller.ui' not in sys.modules, 'ui imported eagerly';"
        "assert 'seestar_controller.enhance' not in sys.modules, 'enhance imported eagerly';"
        "assert 'astropy' not in sys.modules, 'astropy imported eagerly';"
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


def test_stacking_package_source_scan_no_gi_and_module_set():
    for path in sorted(STACKING_PACKAGE_DIR.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        assert not re.search(
            r"^\s*(import|from)\s+seestar_controller\.(link|ui|enhance)\b", source, re.MULTILINE
        ), f"{path.name} imports a widget/link package"
        assert not re.search(r"^\s*(import gi|from gi)\b", source, re.MULTILINE), (
            f"{path.name} imports gi"
        )
        # the heavy third-party import stays lazy (inside functions): no
        # module-level (column 0) astropy import
        assert not re.search(r"^(import astropy|from astropy)\b", source, re.MULTILINE), (
            f"{path.name} imports astropy at module level"
        )
    modules = {path.stem for path in STACKING_PACKAGE_DIR.glob("*.py")}
    assert modules == {"__init__", *PURE_STACKING_MODULES}


def test_stacking_public_api_exports():
    for name in stacking.__all__:
        assert hasattr(stacking, name), f"stacking.{name} missing"
    assert set(stacking.__all__) == {
        "DEROTATE_THRESHOLD_PX",
        "SkippedSub",
        "StackSubsError",
        "StackSubsResult",
        "derotate_array",
        "header_rotation_deg",
        "max_rotation_displacement_px",
        "median_combine",
        "rotation_exceeds_threshold",
        "stack_session_subs",
        "total_field_rotation_deg",
    }
    # the key names are the documented API
    assert callable(stacking.median_combine)
    assert callable(stacking.stack_session_subs)
    assert issubclass(stacking.StackSubsError, ValueError)
