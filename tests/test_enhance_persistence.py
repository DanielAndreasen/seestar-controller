"""Image-enhance ticket 04: pure-layer tests for save enhanced + export +
presets — :mod:`seestar_controller.enhance.persistence`,
:mod:`seestar_controller.store.presets`, the v3→v4 schema migrations, and the
:class:`~seestar_controller.enhance.editor_controller.EditorController`
integration (preset applied on open, save_preset / save_enhanced / export).
No gi, no GTK, no network.

Covers:

- :func:`~seestar_controller.enhance.persistence.save_enhanced` — renders the
  pipeline at FULL resolution and stores the result as a new ``enhanced``
  frame (``enhanced/<session>/<jd>.fits``) in the project; the ORIGINAL stack
  FITS is untouched (sha256 before/after); the enhanced FITS loads back via
  :func:`~seestar_controller.enhance.fits_io.load_fits` and matches
  ``pipeline.apply(fits_image.data)`` (allclose, NaN-safe), with the header
  copied (OBJECT) and a ``HISTORY`` card added; saving twice yields a ``-2``
  collision suffix;
- :func:`~seestar_controller.enhance.persistence.export_render` — PNG and JPEG
  exports are valid PIL images (format + size > 0); float arrays are
  tone-mapped first, uint8 arrays and PIL images pass through; a missing
  suffix gets the default; a mismatched suffix / unknown format / bad quality
  raise :class:`EnhanceError`; JPEG converts RGBA to RGB;
- presets — :func:`~seestar_controller.enhance.persistence.save_preset_for` /
  ``load_preset_for`` / ``delete_preset_for`` round-trip a
  :class:`~seestar_controller.enhance.pipeline.Pipeline`
  (``Pipeline.from_dict(preset.pipeline) == pipeline``), and the store-level
  :mod:`seestar_controller.store.presets` API (save/load/list/delete,
  :class:`~seestar_controller.store.errors.PresetError` on unknown frame /
  bad pipeline / corrupt row);
- the editor controller — opening a stack applies the STORED preset instead
  of the default (snapshot ``preset_saved``), ``save_preset`` persists the
  current pipeline, ``save_enhanced`` stores an enhanced frame (original hash
  unchanged, snapshot ``saved_frame``), ``export`` writes PNG/JPEG; all raise
  :class:`EnhanceError` with no stack open;
- the schema — a fresh db is at v6 with the ``presets`` table; a simulated old
  v2 db (presets dropped, frames CHECK rebuilt without ``'enhanced'``,
  ``user_version=2``) migrates to v6 on open with frames + measurements rows
  preserved and ``enhanced`` frames storable afterwards;
- the package boundary: a subprocess test (no ``gi``, no ``link``, no
  ``ui``) + a source-scan of the new pure modules.

The widget tests (the Save/Export buttons) live in
tests/test_enhance_widgets.py and skip without ``gi``.
"""

import hashlib
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits as pyfits
from PIL import Image

from seestar_controller import enhance, store
from seestar_controller.enhance import (
    EditorController,
    EnhanceError,
    ViewerController,
    default_pipeline,
    list_project_stacks,
    load_fits,
    persistence,
)
from seestar_controller.enhance.fits_io import StackInfo
from seestar_controller.enhance.pipeline import Pipeline
from seestar_controller.enhance.render import to_pil_image, tonemap
from seestar_controller.store import (
    KIND_ENHANCED,
    KIND_STACK,
    create_project,
    get_frame_path,
    list_frames,
    open_project,
    start_session,
    store_frame,
)
from seestar_controller.store.errors import PresetError
from seestar_controller.store.presets import (
    Preset,
    delete_preset,
    list_presets,
    load_preset,
    save_preset,
)

#: Directory of the enhance package (source-scan audit).
ENHANCE_PACKAGE_DIR = Path(enhance.__file__).resolve().parent

T0 = datetime(2026, 11, 20, 22, 0, 0, tzinfo=UTC)
DATE1 = "2026-11-20T22:00:00.000"
DATE2 = "2026-11-20T22:00:10.000"


# --- helpers ----------------------------------------------------------------------------------


def frozen(dt: datetime):
    return lambda: dt


def synthetic_image(size: int = 64, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:size, 0:size]
    data = ((xx + yy) * 10.0).astype(np.float32)
    data = data + (8000.0 * np.exp(-(((yy - 32) ** 2 + (xx - 32) ** 2) / 8.0))).astype(
        np.float32
    )
    return (data + rng.normal(0.0, 5.0, size=(size, size))).astype(np.float32)


def fits_bytes(data: np.ndarray, *, date_obs: str, obj: str) -> bytes:
    import io

    hdu = pyfits.PrimaryHDU(data=data)
    hdu.header["DATE-OBS"] = date_obs
    hdu.header["EXPTIME"] = 10.0
    hdu.header["GAIN"] = 50
    hdu.header["OBJECT"] = obj
    buf = io.BytesIO()
    pyfits.HDUList([hdu]).writeto(buf)
    return buf.getvalue()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def history_cards(header: dict) -> list[str]:
    raw = header.get("HISTORY", [])
    if isinstance(raw, str):
        return [raw]
    return [str(card) for card in raw]


# --- fixtures: a tmp project with stored stacks ---------------------------------------------------


@pytest.fixture()
def project(tmp_path):
    p = create_project("M 42 night", root=tmp_path / "projects")
    yield p
    p.close()


@pytest.fixture()
def stored_stacks(project):
    session = start_session(project, clock=frozen(T0))
    s1 = store_frame(
        project, session, KIND_STACK,
        fits_bytes(synthetic_image(64, seed=1), date_obs=DATE1, obj="M 42"),
        meta={"target": "M 42"},
    )
    s2 = store_frame(
        project, session, KIND_STACK,
        fits_bytes(synthetic_image(64, seed=2), date_obs=DATE2, obj="M 31"),
        meta={"target": "M 31"},
    )
    return [s1, s2]


@pytest.fixture()
def open_stack(project, stored_stacks):
    """The first stored stack as a :class:`StackInfo` + its loaded FitsImage."""
    stack = next(s for s in list_project_stacks(project) if s.frame_id == stored_stacks[0].id)
    return stack, load_fits(stack.path)


def custom_pipeline() -> Pipeline:
    return default_pipeline().with_param("denoise", strength=0.9).with_param(
        "sharpen", amount=1.5
    )


# --- save_enhanced ---------------------------------------------------------------------------------


def test_save_enhanced_stores_frame_and_leaves_original_untouched(project, stored_stacks, open_stack):
    stack, fits_image = open_stack
    original_path = get_frame_path(project, stored_stacks[0])
    digest_before = sha256(original_path)

    pipeline = custom_pipeline()
    frame = persistence.save_enhanced(project, stack, pipeline, fits_image)

    assert frame.kind == KIND_ENHANCED == "enhanced"
    assert frame.session_id == stack.session_id
    assert frame.target == "M 42" and frame.exposure == 10.0 and frame.gain == 50.0
    enhanced_path = get_frame_path(project, frame)
    assert enhanced_path.is_file()
    assert enhanced_path.parent.name == stack.session_id
    assert enhanced_path.parts[-3] == "enhanced"
    assert enhanced_path.read_bytes()[:6] == b"SIMPLE"  # a real FITS file

    # the ORIGINAL stack file is untouched
    assert sha256(original_path) == digest_before
    assert list_frames(project, kind=KIND_ENHANCED) == [frame]
    assert list_frames(project, kind=KIND_STACK) == stored_stacks  # stacks unchanged

    # the enhanced FITS loads back and matches the full-resolution pipeline output
    reloaded = load_fits(enhanced_path)
    expected = pipeline.apply(fits_image.data)
    assert reloaded.data.shape == fits_image.data.shape  # FULL resolution, no downscale
    assert np.allclose(reloaded.data, expected, equal_nan=True)
    assert reloaded.header.get("OBJECT") == "M 42"  # header copied
    assert any(
        "enhanced by seestar-controller" in card for card in history_cards(reloaded.header)
    )


def test_save_enhanced_twice_uses_collision_suffix(project, open_stack):
    stack, fits_image = open_stack
    pipeline = default_pipeline()
    first = persistence.save_enhanced(project, stack, pipeline, fits_image)
    second = persistence.save_enhanced(project, stack, pipeline, fits_image)
    assert first.id != second.id
    p1, p2 = get_frame_path(project, first), get_frame_path(project, second)
    assert p1.is_file() and p2.is_file() and p1 != p2
    assert "-2" in p2.name
    assert {f.id for f in list_frames(project, kind=KIND_ENHANCED)} == {first.id, second.id}


def test_save_enhanced_validates_arguments(project, open_stack):
    stack, fits_image = open_stack
    pipeline = default_pipeline()
    with pytest.raises(EnhanceError):
        persistence.save_enhanced(None, stack, pipeline, fits_image)
    with pytest.raises(EnhanceError):
        persistence.save_enhanced(project, None, pipeline, fits_image)  # ty: ignore[invalid-argument-type]
    with pytest.raises(EnhanceError):
        persistence.save_enhanced(project, stack, "not-a-pipeline", fits_image)  # ty: ignore[invalid-argument-type]
    with pytest.raises(EnhanceError):
        persistence.save_enhanced(project, stack, pipeline, "not-an-image")  # ty: ignore[invalid-argument-type]
    # an unknown session is a store error, wrapped into EnhanceError
    bogus = StackInfo(
        frame_id=stack.frame_id, session_id="no-such-session",
        session_started_at=None, path=stack.path, jd=stack.jd, utc=stack.utc,
        target=None, exposure=None, gain=None,
    )
    with pytest.raises(EnhanceError, match="no session"):
        persistence.save_enhanced(project, bogus, pipeline, fits_image)


# --- export_render -----------------------------------------------------------------------------------


def test_export_png_and_jpeg_are_valid_images(tmp_path, open_stack):
    _stack, fits_image = open_stack
    pil_image = to_pil_image(tonemap(default_pipeline().apply(fits_image.data)))

    png = persistence.export_render(pil_image, tmp_path / "out.png", format="PNG")
    assert png == tmp_path / "out.png"
    with Image.open(png) as img:
        assert img.format == "PNG"
        assert img.size == (fits_image.width, fits_image.height)
    assert png.stat().st_size > 0

    jpg = persistence.export_render(pil_image, tmp_path / "out.jpg", format="JPEG", quality=80)
    with Image.open(jpg) as img:
        assert img.format == "JPEG"
        assert img.size == (fits_image.width, fits_image.height)
    assert jpg.stat().st_size > 0


def test_export_accepts_float_and_uint8_arrays_and_appends_missing_suffix(tmp_path, open_stack):
    _stack, fits_image = open_stack
    float_out = default_pipeline().apply(fits_image.data)
    # float array -> tone-mapped first
    png = persistence.export_render(float_out, tmp_path / "float")  # no suffix -> .png
    assert png.name == "float.png" and png.is_file()
    with Image.open(png) as img:
        assert img.format == "PNG" and img.size == (64, 64)
    # uint8 array passes through
    png2 = persistence.export_render(tonemap(float_out), tmp_path / "u8.png")
    with Image.open(png2) as img:
        assert img.format == "PNG"
    # .jpeg is accepted for JPEG
    jpg = persistence.export_render(tonemap(float_out), tmp_path / "x.jpeg", format="jpeg")
    assert jpg.name == "x.jpeg"
    with Image.open(jpg) as img:
        assert img.format == "JPEG"


def test_export_jpeg_converts_non_rgb_modes(tmp_path):
    rgba = Image.new("RGBA", (16, 16), (255, 0, 0, 128))
    jpg = persistence.export_render(rgba, tmp_path / "a.jpg", format="JPEG")
    with Image.open(jpg) as img:
        assert img.format == "JPEG" and img.mode == "RGB"


def test_export_rejects_bad_format_quality_and_suffix(tmp_path, open_stack):
    _stack, fits_image = open_stack
    pil_image = to_pil_image(tonemap(fits_image.data))
    with pytest.raises(EnhanceError, match="format"):
        persistence.export_render(pil_image, tmp_path / "x.gif", format="GIF")
    with pytest.raises(EnhanceError, match="quality"):
        persistence.export_render(pil_image, tmp_path / "x.jpg", format="JPEG", quality=0)
    with pytest.raises(EnhanceError, match="quality"):
        persistence.export_render(pil_image, tmp_path / "x.jpg", format="JPEG", quality=101)
    with pytest.raises(EnhanceError, match="quality"):
        persistence.export_render(pil_image, tmp_path / "x.jpg", format="JPEG", quality="95")  # ty: ignore[invalid-argument-type]
    with pytest.raises(EnhanceError, match="suffix"):
        persistence.export_render(pil_image, tmp_path / "x.png", format="JPEG")
    with pytest.raises(EnhanceError, match="suffix"):
        persistence.export_render(pil_image, tmp_path / "x.gif", format="PNG")
    with pytest.raises(EnhanceError):
        persistence.export_render(object(), tmp_path / "x.png")  # ty: ignore[invalid-argument-type]


# --- presets: persistence layer + store layer ---------------------------------------------------------


def test_preset_roundtrip_via_persistence(project, open_stack):
    stack, _image = open_stack
    assert persistence.load_preset_for(project, stack) is None  # no preset yet
    pipeline = custom_pipeline()
    preset = persistence.save_preset_for(project, stack, pipeline)
    assert isinstance(preset, Preset)
    assert preset.frame_id == stack.frame_id
    assert isinstance(preset.pipeline, dict) and preset.updated_at
    loaded = persistence.load_preset_for(project, stack)
    assert loaded == pipeline
    assert Pipeline.from_dict(preset.pipeline) == pipeline
    # saving again replaces (upsert)
    other = default_pipeline()
    persistence.save_preset_for(project, stack, other)
    assert persistence.load_preset_for(project, stack) == other
    # delete
    assert persistence.delete_preset_for(project, stack) is True
    assert persistence.load_preset_for(project, stack) is None
    assert persistence.delete_preset_for(project, stack) is False


def test_preset_for_validates_arguments(project, open_stack):
    stack, _image = open_stack
    pipeline = default_pipeline()
    with pytest.raises(EnhanceError):
        persistence.save_preset_for(None, stack, pipeline)
    with pytest.raises(EnhanceError):
        persistence.load_preset_for(project, None)  # ty: ignore[invalid-argument-type]
    with pytest.raises(EnhanceError):
        persistence.delete_preset_for(None, stack)  # ty: ignore[invalid-argument-type]
    with pytest.raises(EnhanceError):
        persistence.save_preset_for(project, stack, "not-a-pipeline")  # ty: ignore[invalid-argument-type]
    # (a mapping that is not JSON-serializable is rejected at the store level,
    #  see test_store_presets_module_roundtrip_and_errors)


def test_store_presets_module_roundtrip_and_errors(project, stored_stacks):
    f1, f2 = stored_stacks
    p1 = save_preset(project, f1.id, default_pipeline().to_dict())
    p2 = save_preset(project, f2.id, custom_pipeline().to_dict())
    assert p1.frame_id == f1.id and p2.frame_id == f2.id
    assert load_preset(project, f1.id) == p1
    assert load_preset(project, 999) is None  # unknown frame: None, not an error
    assert [p.frame_id for p in list_presets(project)] == [f1.id, f2.id]
    assert delete_preset(project, f1.id) is True
    assert delete_preset(project, f1.id) is False
    assert [p.frame_id for p in list_presets(project)] == [f2.id]
    # errors
    with pytest.raises(PresetError, match="no frame"):
        save_preset(project, 999, {"ops": []})
    with pytest.raises(PresetError, match="mapping"):
        save_preset(project, f2.id, ["not", "a", "mapping"])  # ty: ignore[invalid-argument-type]
    with pytest.raises(PresetError, match="JSON"):
        save_preset(project, f2.id, {"ops": object()})
    with pytest.raises(TypeError):
        save_preset(project, "1", {"ops": []})  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError):
        load_preset(project, True)
    # a corrupt row raises on load
    project.db.execute(
        "INSERT OR REPLACE INTO presets (frame_id, pipeline, updated_at) VALUES (?, ?, ?)",
        (str(f2.id), "{not json", "2026-11-20T00:00:00.000+00:00"),
    )
    with pytest.raises(PresetError, match="corrupt"):
        load_preset(project, f2.id)
    project.db.execute(
        "INSERT OR REPLACE INTO presets (frame_id, pipeline, updated_at) VALUES (?, ?, ?)",
        (str(f1.id), "[1, 2]", "2026-11-20T00:00:00.000+00:00"),
    )
    with pytest.raises(PresetError, match="not a JSON object"):
        load_preset(project, f1.id)


def test_apply_preset_on_open(project, open_stack):
    stack, _image = open_stack
    assert persistence.apply_preset_on_open(None, stack) is None
    assert persistence.apply_preset_on_open(project, None) is None
    assert persistence.apply_preset_on_open(project, stack) is None  # no preset yet
    pipeline = custom_pipeline()
    persistence.save_preset_for(project, stack, pipeline)
    assert persistence.apply_preset_on_open(project, stack) == pipeline
    # a corrupt preset is ignored (logged), not raised
    project.db.execute(
        "INSERT OR REPLACE INTO presets (frame_id, pipeline, updated_at) VALUES (?, ?, ?)",
        (str(stack.frame_id), "{oops", "2026-11-20T00:00:00.000+00:00"),
    )
    assert persistence.apply_preset_on_open(project, stack) is None


# --- editor controller integration ---------------------------------------------------------------------


def make_editor(project, **kwargs):
    vc = ViewerController(project)
    kwargs.setdefault("render_max_size", 32)
    kwargs.setdefault("debounce_s", 0.0)
    return vc, EditorController(vc, **kwargs)


def test_editor_open_stack_applies_stored_preset(project, stored_stacks):
    vc, ec = make_editor(project)
    vc.open_stack(stored_stacks[0].id)
    assert ec.wait(5.0)
    assert ec.snapshot.pipeline == default_pipeline()
    assert ec.snapshot.preset_saved is False

    # change params + save the preset
    ec.set_param("denoise", strength=0.9)
    assert ec.wait(5.0)
    saved = ec.save_preset()
    assert saved.frame_id == stored_stacks[0].id
    assert ec.snapshot.preset_saved is True
    assert ec.preset_saved is True

    # reopen the other stack, then the first again -> the preset is applied
    vc.open_stack(stored_stacks[1].id)
    assert ec.wait(5.0)
    assert ec.snapshot.pipeline == default_pipeline()
    assert ec.snapshot.preset_saved is False
    vc.open_stack(stored_stacks[0].id)
    assert ec.wait(5.0)
    snap = ec.snapshot
    assert snap.stack.frame_id == stored_stacks[0].id
    assert snap.pipeline.ops[1].params.strength == pytest.approx(0.9)
    assert snap.pipeline != default_pipeline()
    assert snap.preset_saved is True


def test_editor_save_enhanced_and_export(project, stored_stacks, tmp_path):
    vc, ec = make_editor(project)
    original_path = get_frame_path(project, stored_stacks[0])
    digest_before = sha256(original_path)

    vc.open_stack(stored_stacks[0].id)
    assert ec.wait(5.0)
    ec.set_param("sharpen", amount=2.0)
    assert ec.wait(5.0)

    frame = ec.save_enhanced()
    assert frame.kind == KIND_ENHANCED
    assert get_frame_path(project, frame).is_file()
    assert sha256(original_path) == digest_before  # original untouched
    assert ec.snapshot.saved_frame == frame
    assert ec.saved_frame == frame

    png = ec.export(tmp_path / "enhanced.png")
    with Image.open(png) as img:
        assert img.format == "PNG" and img.size == (64, 64)
    jpg = ec.export(tmp_path / "enhanced.jpg", format="JPEG", quality=90)
    with Image.open(jpg) as img:
        assert img.format == "JPEG"
    assert callable(ec.invoke_on_main)


def test_editor_persistence_methods_require_open_stack(project):
    _vc, ec = make_editor(project)
    with pytest.raises(EnhanceError, match="no stack open"):
        ec.save_preset()
    with pytest.raises(EnhanceError, match="no stack open"):
        ec.save_enhanced()
    with pytest.raises(EnhanceError, match="no stack open"):
        ec.export("x.png")


# --- schema v6 + migration ------------------------------------------------------------------------------


def test_fresh_db_is_v6_with_presets_table(project):
    assert store.SCHEMA_VERSION == 7
    assert set(store.MIGRATIONS) == {0, 1, 2, 3, 4, 5, 6}
    assert project.db.execute("PRAGMA user_version").fetchone()[0] == 7
    assert project.config["schema_version"] == 7
    tables = {
        row[0]
        for row in project.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert "presets" in tables
    cols = [row[1] for row in project.db.execute("PRAGMA table_info(presets)")]
    assert cols == ["frame_id", "pipeline", "updated_at"]
    # the frames CHECK now allows 'enhanced'
    sql = project.db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='frames'"
    ).fetchone()[0]
    assert "'enhanced'" in sql


def _downgrade_to_v2(project) -> None:
    """Turn the open project's db into a simulated OLD v2 db: drop the
    presets table, rebuild ``frames`` with the old CHECK (no 'enhanced'),
    stamp ``user_version=2`` and mirror it into project.json."""
    db = project.db
    db.execute("PRAGMA foreign_keys=OFF")  # the rebuild drops a parent table
    with db:  # one transaction: the whole downgrade commits together
        db.execute("DROP TABLE IF EXISTS presets")
        db.execute(
            """
            CREATE TABLE frames__old (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
                kind TEXT NOT NULL CHECK (kind IN ('sub', 'stack')),
                path TEXT NOT NULL,
                jd REAL NOT NULL,
                utc TEXT NOT NULL,
                exposure REAL,
                gain REAL,
                target TEXT
            )
            """
        )
        db.execute(
            "INSERT INTO frames__old (id, session_id, kind, path, jd, utc, exposure, gain, target) "
            "SELECT id, session_id, kind, path, jd, utc, exposure, gain, target FROM frames"
        )
        db.execute("DROP TABLE frames")
        db.execute("ALTER TABLE frames__old RENAME TO frames")
        db.execute("CREATE INDEX IF NOT EXISTS idx_frames_session_id ON frames (session_id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_frames_jd ON frames (jd)")
        db.execute("PRAGMA user_version=2")
    db.execute("PRAGMA foreign_keys=ON")
    config = json.loads(project.config_path.read_text(encoding="utf-8"))
    config["schema_version"] = 2
    project.config_path.write_text(json.dumps(config), encoding="utf-8")


def test_old_v2_db_migrates_to_v6_preserving_rows(project, stored_stacks, tmp_path):
    from seestar_controller.store import add_measurement

    add_measurement(project, stored_stacks[0].id, mag=12.5, mag_err=0.05)
    frame_count = len(list_frames(project))
    measurement_count = project.db.execute("SELECT COUNT(*) FROM measurements").fetchone()[0]
    _downgrade_to_v2(project)
    project.close()

    reopened = open_project(project.path)
    try:
        assert reopened.db.execute("PRAGMA user_version").fetchone()[0] == 7
        assert reopened.config["schema_version"] == 7
        on_disk = json.loads(reopened.config_path.read_text(encoding="utf-8"))
        assert on_disk["schema_version"] == 7  # mirrored into project.json
        tables = {
            row[0]
            for row in reopened.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "presets" in tables
        # rows preserved (the frames rebuild must not cascade-delete measurements)
        assert len(list_frames(reopened)) == frame_count
        assert (
            reopened.db.execute("SELECT COUNT(*) FROM measurements").fetchone()[0]
            == measurement_count
        )
        # 'enhanced' frames are storable now, and presets work
        session = reopened.db.execute("SELECT id FROM sessions").fetchone()[0]
        enhanced = store_frame(
            reopened, session, KIND_ENHANCED,
            fits_bytes(synthetic_image(32, seed=3), date_obs=DATE1, obj="M 42"),
        )
        assert enhanced.kind == "enhanced"
        preset = save_preset(reopened, stored_stacks[0].id, default_pipeline().to_dict())
        assert load_preset(reopened, stored_stacks[0].id) == preset
    finally:
        reopened.close()


# --- package boundary + exports ---------------------------------------------------------------------------


def test_persistence_module_imports_no_gi_no_link_no_ui():
    code = (
        "import sys;"
        "import seestar_controller.enhance;"
        "import seestar_controller.enhance.persistence;"
        "import seestar_controller.store.presets;"
        "assert 'gi' not in sys.modules, 'GTK imported';"
        "assert 'seestar_controller.link' not in sys.modules, 'link imported eagerly';"
        "assert 'seestar_controller.ui' not in sys.modules, 'ui imported eagerly';"
        "assert 'seestar_controller.enhance.enhance_view' not in sys.modules, "
        "'enhance_view imported eagerly';"
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


def test_persistence_and_presets_source_has_no_gi():
    for path in (
        ENHANCE_PACKAGE_DIR / "persistence.py",
        Path(store.presets.__file__).resolve(),
    ):
        source = path.read_text(encoding="utf-8")
        assert not re.search(r"^\s*(import gi|from gi)\b", source, re.MULTILINE), (
            f"pure module {path.name} imports gi"
        )


def test_persistence_api_exported_from_package():
    for name in (
        "save_enhanced",
        "export_render",
        "save_preset_for",
        "load_preset_for",
        "delete_preset_for",
        "apply_preset_on_open",
        "EXPORT_FORMATS",
        "DEFAULT_EXPORT_QUALITY",
        "ENHANCED_HISTORY",
    ):
        assert name in enhance.__all__, name
        assert hasattr(enhance, name), name
    assert enhance.EXPORT_FORMATS == ("PNG", "JPEG")
    assert enhance.DEFAULT_EXPORT_QUALITY == 95
    assert enhance.save_enhanced is persistence.save_enhanced
    assert enhance.export_render is persistence.export_render
    # store exports
    for name in (
        "KIND_ENHANCED",
        "ENHANCED_DIR",
        "KIND_DIRS",
        "Preset",
        "PresetError",
        "save_preset",
        "load_preset",
        "delete_preset",
        "list_presets",
    ):
        assert name in store.__all__, name
        assert hasattr(store, name), name
    assert store.KIND_ENHANCED == "enhanced"
    assert store.ENHANCED_DIR == "enhanced"
    assert store.KIND_DIRS[store.KIND_ENHANCED] == "enhanced"
