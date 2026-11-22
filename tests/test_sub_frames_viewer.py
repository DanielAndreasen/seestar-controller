"""Sub-frames issue 01: PURE tests — the image viewer lists and opens ALL
stored frames of the current project (subs, stacks, enhanced), not just
kind='stack'. No gi, no GTK, no network.

Covers (``.scratch/sub-frames/issues/01-viewer-opens-subs.md``):

- :func:`~seestar_controller.enhance.fits_io.list_project_frames` — a tmp
  project with a sub, a stack, an RGB sub, and an enhanced frame: all are
  listed ordered by ``jd`` with their ``kind``, absolute path, and metadata;
  the ``kind=`` filter restricts; ``list_project_stacks`` stays the
  stack-only view; None project -> :class:`EnhanceError`; empty project -> [];
- :class:`~seestar_controller.enhance.viewer_controller.ViewerController` —
  a project with ONLY subs (the "interrupted session" case: empty stacks/)
  lists nothing by default (issue 05: the default list is stacks only) and
  the subs behind the ``show_all`` toggle, newest-first;
  ``open_stack`` on any frame id (sub, stack, enhanced — visible or not)
  loads the FITS and renders the preview exactly like a stack (grayscale sub
  -> mode ``L`` preview, RGB sub -> mode ``RGB`` preview — the RGB path of
  :func:`~.fits_io.load_fits` + :func:`~.render.render_preview`); the
  snapshot carries the open frame; unknown id -> :class:`EnhanceError`
  recorded in the snapshot;
- the package boundary: a subprocess test asserting the touched pure modules
  still import no ``gi`` (mirrors tests/test_enhance_fits.py);
- ``list_project_frames`` is exported from the package.

The widget tests (Enhance-view list shows the subs with the kind label, row
click opens a sub; Projects-view frame rows open the frame in the viewer)
live in tests/test_sub_frames_widgets.py and skip without ``gi``.
"""

import io
import subprocess
import sys
from datetime import UTC, datetime

import numpy as np
import pytest
from astropy.io import fits as pyfits
from PIL import Image

from seestar_controller import enhance
from seestar_controller.enhance import (
    EnhanceError,
    ViewerController,
    ViewerSnapshot,
    fits_io,
    list_project_frames,
    list_project_stacks,
)
from seestar_controller.store import (
    KIND_ENHANCED,
    KIND_STACK,
    KIND_SUB,
    create_project,
    get_frame_path,
    start_session,
    store_frame,
)

T0 = datetime(2026, 11, 20, 22, 0, 0, tzinfo=UTC)
DATE1 = "2026-11-20T22:00:00.000"
DATE2 = "2026-11-20T22:00:10.000"
DATE3 = "2026-11-20T22:00:20.000"
DATE4 = "2026-11-20T22:00:30.000"


def frozen(dt: datetime):
    """A frozen clock returning ``dt`` on every call."""
    return lambda: dt


def synthetic_image(size: int = 32, seed: int = 1) -> np.ndarray:
    """A small synthetic grayscale image (gradient + star peak + noise)."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:size, 0:size]
    data = ((xx + yy) * 10.0).astype(np.float32)
    data = data + (8000.0 * np.exp(-(((yy - 16) ** 2 + (xx - 16) ** 2) / 8.0))).astype(
        np.float32
    )
    return (data + rng.normal(0.0, 5.0, size=(size, size))).astype(np.float32)


def synthetic_rgb(size: int = 32, seed: int = 7) -> np.ndarray:
    """A small synthetic RGB image in FITS axis order ``(3, H, W)``."""
    channels = [
        synthetic_image(size, seed=seed + c) for c in range(3)
    ]
    return np.stack(channels, axis=0).astype(np.float32)


def fits_bytes(data: np.ndarray, *, date_obs: str, obj: str = "NGC7789") -> bytes:
    """Serialize ``data`` (grayscale ``(H, W)`` or RGB ``(3, H, W)``) as FITS
    bytes with the observation metadata cards."""
    hdu = pyfits.PrimaryHDU(data=data)
    hdu.header["DATE-OBS"] = date_obs
    hdu.header["EXPTIME"] = 10.0
    hdu.header["GAIN"] = 50
    hdu.header["OBJECT"] = obj
    buf = io.BytesIO()
    pyfits.HDUList([hdu]).writeto(buf)
    return buf.getvalue()


@pytest.fixture()
def root(tmp_path):
    """A fresh project root."""
    return tmp_path / "projects"


@pytest.fixture()
def project(root):
    """One created project, closed after the test."""
    p = create_project("NGC7789", root=root)
    yield p
    p.close()


@pytest.fixture()
def session(project):
    """One open session with a deterministic start time."""
    return start_session(project, clock=frozen(T0))


@pytest.fixture()
def stored_frames(project, session):
    """One sub, one stack, one RGB sub, one enhanced frame (in jd order);
    returns them as a dict keyed by kind label."""
    sub = store_frame(
        project, session, KIND_SUB,
        fits_bytes(synthetic_image(32, seed=1), date_obs=DATE1),
        meta={"target": "NGC7789", "exposure": 10.0, "gain": 50},
    )
    stack = store_frame(
        project, session, KIND_STACK,
        fits_bytes(synthetic_image(48, seed=2), date_obs=DATE2),
        meta={"target": "NGC7789"},
    )
    rgb_sub = store_frame(
        project, session, KIND_SUB,
        fits_bytes(synthetic_rgb(24, seed=3), date_obs=DATE3),
    )
    enhanced = store_frame(
        project, session, KIND_ENHANCED,
        fits_bytes(synthetic_image(48, seed=4), date_obs=DATE4),
    )
    return {"sub": sub, "stack": stack, "rgb_sub": rgb_sub, "enhanced": enhanced}


@pytest.fixture()
def subs_only_project(root):
    """The issue's demo case: a project with ONLY two subs (empty stacks/)."""
    p = create_project("NGC7789 subs only", root=root)
    sid = start_session(p, clock=frozen(T0))
    sub1 = store_frame(
        p, sid, KIND_SUB,
        fits_bytes(synthetic_image(32, seed=11), date_obs=DATE1),
        meta={"target": "NGC7789"},
    )
    sub2 = store_frame(
        p, sid, KIND_SUB,
        fits_bytes(synthetic_image(32, seed=12), date_obs=DATE2),
        meta={"target": "NGC7789"},
    )
    yield p, [sub1, sub2]
    p.close()


# --- list_project_frames -------------------------------------------------------------


def test_list_project_frames_lists_all_kinds_ordered_by_jd(project, session, stored_frames):
    infos = list_project_frames(project)
    assert [i.frame_id for i in infos] == [
        stored_frames["sub"].id,
        stored_frames["stack"].id,
        stored_frames["rgb_sub"].id,
        stored_frames["enhanced"].id,
    ]
    assert [i.kind for i in infos] == ["sub", "stack", "sub", "enhanced"]
    first = infos[0]
    assert first.session_id == session
    assert first.session_started_at == "2026-11-20T22:00:00.000+00:00"
    assert first.path.is_absolute() and first.path.exists()
    assert first.path == get_frame_path(project, stored_frames["sub"])
    assert first.target == "NGC7789" and first.exposure == 10.0 and first.gain == 50.0
    assert first.utc.startswith("2026-11-20T22:00:00")
    # the kind shows up in the list subtitle (the Enhance-view rows render it)
    assert infos[0].subtitle().startswith("sub")
    assert infos[1].subtitle().startswith("stack")
    assert infos[3].subtitle().startswith("enhanced")
    assert "session" in first.subtitle() and "jd" in first.subtitle()
    assert first.label() == f"{first.utc} — NGC7789"


def test_list_project_frames_kind_filter(project, stored_frames):
    subs = list_project_frames(project, kind=KIND_SUB)
    assert [i.frame_id for i in subs] == [
        stored_frames["sub"].id, stored_frames["rgb_sub"].id
    ]
    assert all(i.kind == "sub" for i in subs)
    stacks = list_project_frames(project, kind=KIND_STACK)
    assert [i.frame_id for i in stacks] == [stored_frames["stack"].id]
    enhanced = list_project_frames(project, kind=KIND_ENHANCED)
    assert [i.frame_id for i in enhanced] == [stored_frames["enhanced"].id]


def test_list_project_stacks_stays_stack_only(project, stored_frames):
    """The stack-only view is unchanged (other callers rely on it)."""
    stacks = list_project_stacks(project)
    assert [i.frame_id for i in stacks] == [stored_frames["stack"].id]
    assert all(i.kind == "stack" for i in stacks)


def test_list_project_frames_empty_project(project):
    assert list_project_frames(project) == []


def test_list_project_frames_none_project_raises():
    with pytest.raises(EnhanceError) as excinfo:
        list_project_frames(None)
    assert "no project" in str(excinfo.value)


# --- ViewerController over a subs-only project ----------------------------------------


def test_viewer_lists_subs_of_subs_only_project(subs_only_project):
    project, subs = subs_only_project
    vc = ViewerController(project)
    # image-enhance issue 05: the DEFAULT list is stacks only, so a subs-only
    # project lists nothing until the show-all toggle is switched on
    assert not vc.stacks
    assert not vc.show_all
    vc.set_show_all(True)
    assert [s.frame_id for s in vc.stacks] == [subs[1].id, subs[0].id]  # newest first
    assert [s.kind for s in vc.stacks] == ["sub", "sub"]
    snap = vc.snapshot
    assert isinstance(snap, ViewerSnapshot)
    assert snap.project_name == "NGC7789 subs only"
    assert len(snap.stacks) == 2
    assert snap.show_all is True
    assert snap.current is None and snap.error is None


def test_viewer_open_sub_loads_and_renders_preview(subs_only_project):
    """open_stack works for a sub frame id exactly like for a stack."""
    project, subs = subs_only_project
    vc = ViewerController(project)
    seen = []
    vc.add_listener(seen.append)
    image = vc.open_stack(subs[0].id)
    assert image.shape == (32, 32)
    assert not image.is_rgb
    assert vc.current_stack is not None
    assert vc.current_stack.frame_id == subs[0].id
    assert vc.current_stack.kind == "sub"
    assert vc.current_image is image
    assert isinstance(vc.preview, Image.Image)
    assert vc.preview.mode == "L"  # grayscale sub -> luminance preview
    assert max(vc.preview.size) <= 1024
    snap = vc.snapshot
    assert snap.current is vc.current_stack and snap.image is image
    assert snap.preview is vc.preview and snap.error is None
    assert seen and seen[-1].current is not None
    # the second sub opens too (and is a different image)
    other = vc.open_stack(subs[1].id)
    assert other is not image
    assert vc.current_stack is not None and vc.current_stack.frame_id == subs[1].id


def test_viewer_open_rgb_sub_renders_rgb_preview(project, session, stored_frames):
    """An RGB sub goes through the RGB path of load_fits (axis normalization)
    and renders an RGB preview — no luminance conversion needed in enhance."""
    vc = ViewerController(project)
    rgb_sub = stored_frames["rgb_sub"]
    image = vc.open_stack(rgb_sub.id)
    assert image.is_rgb
    assert image.shape == (24, 24, 3)
    assert vc.preview is not None and vc.preview.mode == "RGB"
    assert vc.current_stack is not None and vc.current_stack.kind == "sub"


def test_viewer_open_stack_and_enhanced_still_work(project, stored_frames):
    vc = ViewerController(project)
    stack_image = vc.open_stack(stored_frames["stack"].id)
    assert stack_image.shape == (48, 48)
    assert vc.current_stack is not None and vc.current_stack.kind == "stack"
    enh_image = vc.open_stack(stored_frames["enhanced"].id)
    assert enh_image.shape == (48, 48)
    assert vc.current_stack is not None and vc.current_stack.kind == "enhanced"


# --- image-enhance issue 05: default stack list is stacks-only, newest-first, show-all toggle ------


def test_viewer_default_lists_only_stacks_newest_first(project, stored_frames):
    """Issue 05: with subs + a stack + an enhanced frame stored, the default
    list contains ONLY the stack; the show-all toggle lists everything,
    always NEWEST-FIRST (descending jd)."""
    vc = ViewerController(project)
    assert vc.show_all is False
    assert [s.frame_id for s in vc.stacks] == [stored_frames["stack"].id]
    assert [s.kind for s in vc.stacks] == ["stack"]
    # the full listing (any kind) stays available and is newest-first too
    assert [s.frame_id for s in vc.all_stacks] == [
        stored_frames["enhanced"].id,  # DATE4
        stored_frames["rgb_sub"].id,   # DATE3
        stored_frames["stack"].id,     # DATE2
        stored_frames["sub"].id,       # DATE1
    ]
    # the toggle switches the visible list, not the stored listing
    seen = []
    vc.add_listener(seen.append)
    vc.set_show_all(True)
    assert vc.show_all and vc.snapshot.show_all is True
    assert [s.frame_id for s in vc.stacks] == [s.frame_id for s in vc.all_stacks]
    assert [s.kind for s in vc.stacks] == ["enhanced", "sub", "stack", "sub"]
    assert seen and seen[-1].show_all is True
    vc.set_show_all(False)
    assert not vc.show_all and vc.snapshot.show_all is False
    assert [s.frame_id for s in vc.stacks] == [stored_frames["stack"].id]
    # re-setting the same value is a no-op (no extra notification)
    n = len(seen)
    vc.set_show_all(False)
    assert len(seen) == n


def test_viewer_open_stack_opens_any_kind_even_when_filtered_out(project, stored_frames):
    """Issue 05: open_stack still opens a sub/enhanced frame even though the
    default list hides it (lookup goes through the full listing), and with
    show-all on the same frames are visible in the list too."""
    vc = ViewerController(project)
    assert all(s.kind == "stack" for s in vc.stacks)
    image = vc.open_stack(stored_frames["sub"].id)  # sub not in the visible list
    assert image.shape == (32, 32)
    assert vc.current_stack is not None and vc.current_stack.kind == "sub"
    assert vc.current_stack.frame_id == stored_frames["sub"].id
    vc.set_show_all(True)
    rgb = vc.open_stack(stored_frames["rgb_sub"].id)
    assert rgb.is_rgb and vc.current_stack is not None and vc.current_stack.kind == "sub"
    enh = vc.open_stack(stored_frames["enhanced"].id)
    assert enh.shape == (48, 48) and vc.current_stack.kind == "enhanced"


def test_viewer_open_unknown_id_raises_and_records(subs_only_project):
    project, _subs = subs_only_project
    vc = ViewerController(project)
    with pytest.raises(EnhanceError) as excinfo:
        vc.open_stack(99999)
    assert "no frame with frame id" in str(excinfo.value)
    assert vc.snapshot.error is not None and "99999" in vc.snapshot.error
    assert vc.current_stack is None


def test_viewer_refresh_picks_up_newly_stored_sub(subs_only_project):
    """A sub stored after the listing is found by open_stack (refresh-once)."""
    project, subs = subs_only_project
    vc = ViewerController(project)
    vc.set_show_all(True)  # issue 05: subs are behind the show-all toggle
    assert len(vc.stacks) == 2
    late = store_frame(
        project, subs[0].session_id, KIND_SUB,
        fits_bytes(synthetic_image(16, seed=99), date_obs=DATE4),
    )
    image = vc.open_stack(late.id)  # not in the stale list -> refresh once
    assert image.shape == (16, 16)
    assert len(vc.stacks) == 3
    # newest-first: the late sub (DATE4) is on top
    assert vc.stacks[0].frame_id == late.id


# --- package boundary + exports --------------------------------------------------------


def test_list_project_frames_exported_from_package():
    assert "list_project_frames" in enhance.__all__
    assert enhance.list_project_frames is fits_io.list_project_frames


def test_touched_pure_modules_import_no_gi():
    """Subprocess boundary: the modules this ticket touched stay gi-free."""
    code = (
        "import sys;"
        "import seestar_controller.enhance.fits_io;"
        "import seestar_controller.enhance.viewer_controller;"
        "assert 'gi' not in sys.modules, 'GTK imported';"
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout
