"""Image-enhance ticket 03: pure-layer tests for the Enhance editor
controller — per-op parameter changes with bounded undo/redo, debounced
worker-thread re-renders marshalled via ``invoke_on_main``, and the
before/after previews. No gi, no GTK, no network.

Covers :mod:`seestar_controller.enhance.editor_controller`:

- :class:`~seestar_controller.enhance.editor_controller.EditorController`
  hanging off a (fake or real)
  :class:`~seestar_controller.enhance.viewer_controller.ViewerController`:
  opening a stack resets to :func:`~seestar_controller.enhance.pipeline.default_pipeline`
  and renders BOTH previews (the "before" = the raw FITS tone-mapped via
  :func:`~seestar_controller.enhance.render.render_preview`, the "after" =
  :func:`~seestar_controller.enhance.pipeline.render_pipeline`); clearing /
  switching the stack resets the editor state;
- :meth:`~seestar_controller.enhance.editor_controller.EditorController.set_param`
  — updates the pipeline via :meth:`~seestar_controller.enhance.pipeline.Pipeline.with_param`,
  pushes the previous pipeline onto the bounded undo stack
  (:data:`~seestar_controller.enhance.editor_controller.MAX_UNDO_ENTRIES`),
  clears the redo stack, notifies synchronously with ``rendering=True`` and
  never blocks; equal-value changes are no-ops; invalid changes (unknown op /
  field, out-of-range value, no stack open) raise :class:`EnhanceError` and
  leave the undo stack untouched;
- :meth:`~seestar_controller.enhance.editor_controller.EditorController.undo`
  / ``redo`` / ``reset`` — swap the stacks and re-render (undo restores the
  previous params EXACTLY, a new change clears the redo stack, the undo stack
  is bounded with oldest-evicted, reset is undoable);
- the render runs on a worker thread and the result is marshalled via
  ``invoke_on_main`` — with a QUEUED invoke the snapshot is NOT updated until
  the queue is drained; rapid changes are debounced into ONE render (latest
  wins) and a generation counter discards stale worker results;
- errors never crash: a failing render lands in
  :attr:`~seestar_controller.enhance.editor_controller.EditorSnapshot.error`
  (the before preview still renders) and the controller stays usable;
- listeners — add/remove/idempotent/decorator, exceptions isolated; the
  frozen :class:`~seestar_controller.enhance.editor_controller.EditorSnapshot`;
- the package boundary: a subprocess test (no ``gi``, no ``link``, no
  ``ui``, the widget layer stays lazy) + a source-scan of the new modules;
- an integration test with a real
  :class:`~seestar_controller.enhance.viewer_controller.ViewerController`
  over a tmp store project.

The widget tests live in tests/test_enhance_widgets.py and skip without ``gi``.
"""

import collections
import dataclasses
import re
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits as pyfits
from PIL import Image

from seestar_controller import enhance
from seestar_controller.enhance import (
    MAX_UNDO_ENTRIES,
    EditorController,
    EditorSnapshot,
    EnhanceError,
    FitsImage,
    StackInfo,
    ViewerController,
    ViewerSnapshot,
    default_pipeline,
    editor_controller,
    render_pipeline,
    render_preview,
)
from seestar_controller.store import (
    KIND_STACK,
    create_project,
    start_session,
    store_frame,
)

#: Directory of the enhance package (source-scan audit).
ENHANCE_PACKAGE_DIR = Path(enhance.__file__).resolve().parent

T0 = datetime(2026, 11, 20, 22, 0, 0, tzinfo=UTC)
DATE1 = "2026-11-20T22:00:00.000"
DATE2 = "2026-11-20T22:00:10.000"


# --- test doubles ---------------------------------------------------------------------------


class FakeViewer:
    """A minimal stand-in for :class:`ViewerController`: the listener
    interface + a settable :class:`ViewerSnapshot` (no store, no FITS)."""

    def __init__(self) -> None:
        self._listeners: list = []
        self._snapshot = ViewerSnapshot(
            project_name="fake",
            stacks=(),
            current=None,
            image=None,
            preview=None,
            error=None,
        )

    @property
    def snapshot(self) -> ViewerSnapshot:
        return self._snapshot

    def add_listener(self, listener):
        if listener not in self._listeners:
            self._listeners.append(listener)
        return listener

    def remove_listener(self, listener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _emit(self, snapshot: ViewerSnapshot) -> None:
        self._snapshot = snapshot
        for listener in list(self._listeners):
            listener(snapshot)

    def open(self, stack: StackInfo, image: FitsImage) -> None:
        self._emit(
            ViewerSnapshot("fake", (stack,), stack, image, None, None)
        )

    def clear(self, error: str | None = None) -> None:
        self._emit(ViewerSnapshot("fake", (), None, None, None, error))


class InvokeQueue:
    """A queued ``invoke_on_main``: callbacks are NOT run until
    :meth:`drain` (mimics ``GLib.idle_add`` + a main loop that has not run
    yet)."""

    def __init__(self) -> None:
        self._queue: collections.deque = collections.deque()
        self._lock = threading.Lock()

    def __call__(self, callback) -> None:
        with self._lock:
            self._queue.append(callback)

    def pending(self) -> int:
        with self._lock:
            return len(self._queue)

    def drain(self) -> None:
        while True:
            with self._lock:
                if not self._queue:
                    return
                callback = self._queue.popleft()
            callback()


class CountingRender:
    """A ``render_fn`` that records every call (pipeline, max_size) and
    renders for real."""

    def __init__(self) -> None:
        self.calls: list = []
        self._lock = threading.Lock()

    def __call__(self, pipeline, fits_image, max_size):
        with self._lock:
            self.calls.append((pipeline, max_size))
        return render_pipeline(pipeline, fits_image, max_size)


class GatedRender:
    """A ``render_fn`` whose FIRST call blocks on ``gate`` (so a second,
    newer render can finish first and queue its result before the stale
    one)."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.gate = threading.Event()
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self, pipeline, fits_image, max_size):
        with self._lock:
            self.calls += 1
            first = self.calls == 1
        if first:
            self.entered.set()
            assert self.gate.wait(timeout=10), "gate never released"
        return render_pipeline(pipeline, fits_image, max_size)


class FlakyRender:
    """A ``render_fn`` that raises while ``fail`` is set."""

    def __init__(self) -> None:
        self.fail = True
        self.calls = 0

    def __call__(self, pipeline, fits_image, max_size):
        self.calls += 1
        if self.fail:
            raise RuntimeError("render boom")
        return render_pipeline(pipeline, fits_image, max_size)


# --- helpers ----------------------------------------------------------------------------------


def make_image(size: int = 64, seed: int = 1) -> FitsImage:
    """A small synthetic FitsImage (gradient + star peaks + noise)."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:size, 0:size]
    data = ((xx + yy) * 10.0).astype(np.float32)
    for cy, cx, peak in ((16, 16, 5000.0), (40, 48, 8000.0)):
        data = data + (peak * np.exp(-(((yy - cy) ** 2 + (xx - cx) ** 2) / 8.0))).astype(
            np.float32
        )
    data = data + rng.normal(0.0, 5.0, size=(size, size)).astype(np.float32)
    return FitsImage(data=np.ascontiguousarray(data, dtype=np.float32), header={}, path=None)


def make_stack(frame_id: int) -> StackInfo:
    return StackInfo(
        frame_id=frame_id,
        session_id="s1",
        session_started_at=None,
        path=Path(f"/tmp/fake-stack-{frame_id}.fits"),
        jd=2460000.0 + frame_id,
        utc="2026-11-20T22:00:00.000+00:00",
        target="M 42",
        exposure=10.0,
        gain=50.0,
    )


def make_editor(**kwargs):
    """A ``(FakeViewer, EditorController)`` pair with fast settings
    (32px previews, no debounce, direct invoke unless overridden)."""
    viewer = FakeViewer()
    kwargs.setdefault("render_max_size", 32)
    kwargs.setdefault("debounce_s", 0.0)
    return viewer, EditorController(viewer, **kwargs)  # ty: ignore[invalid-argument-type]


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


# --- fixtures: a tmp project with stored stacks (integration test) -----------------------------


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


# --- initial state + opening a stack -------------------------------------------------------------


def test_initial_snapshot_is_empty():
    viewer = FakeViewer()
    ec = EditorController(viewer)  # ty: ignore[invalid-argument-type]
    assert ec.viewer_controller is viewer
    snap = ec.snapshot
    assert isinstance(snap, EditorSnapshot)
    assert snap.stack is None and snap.pipeline is None
    assert snap.enhanced_preview is None and snap.original_preview is None
    assert snap.rendering is False
    assert not snap.can_undo and not snap.can_redo
    assert snap.error is None
    assert ec.stack is None and ec.pipeline is None
    assert ec.undo_depth == 0 and ec.redo_depth == 0
    assert ec.wait(1.0) is True  # idle


def test_open_stack_renders_before_and_after_previews():
    viewer, ec = make_editor()
    image = make_image()
    stack = make_stack(1)
    viewer.open(stack, image)
    assert ec.wait(5.0)
    snap = ec.snapshot
    assert snap.stack is stack
    assert snap.pipeline == default_pipeline()
    assert snap.original_preview is not None and snap.enhanced_preview is not None
    assert isinstance(snap.original_preview, Image.Image)
    assert max(snap.original_preview.size) <= 32
    assert max(snap.enhanced_preview.size) <= 32
    # the before preview is the RAW image tone-mapped; the after is the pipeline
    assert snap.original_preview.tobytes() == render_preview(image, 32).tobytes()
    assert snap.enhanced_preview.tobytes() == render_pipeline(
        default_pipeline(), image, 32
    ).tobytes()
    # before != after: the default pipeline changes the pixels
    assert snap.original_preview.tobytes() != snap.enhanced_preview.tobytes()
    assert snap.rendering is False and snap.error is None
    assert ec.stack is stack and ec.pipeline == default_pipeline()


def test_switching_stacks_resets_editor_state():
    viewer, ec = make_editor()
    s1, s2 = make_stack(1), make_stack(2)
    img1, img2 = make_image(seed=1), make_image(seed=2)
    viewer.open(s1, img1)
    assert ec.wait(5.0)
    ec.set_param("denoise", strength=0.8)
    assert ec.wait(5.0)
    assert ec.undo_depth == 1
    viewer.open(s2, img2)
    assert ec.wait(5.0)
    snap = ec.snapshot
    assert snap.stack is s2
    assert snap.pipeline == default_pipeline()
    assert ec.undo_depth == 0 and ec.redo_depth == 0
    assert snap.original_preview is not None and snap.enhanced_preview is not None
    # the previews are of the NEW image
    assert snap.original_preview.tobytes() == render_preview(img2, 32).tobytes()


def test_clearing_the_stack_clears_the_editor():
    viewer, ec = make_editor()
    viewer.open(make_stack(1), make_image())
    assert ec.wait(5.0)
    ec.set_param("denoise", strength=0.8)
    viewer.clear(error="project gone")
    snap = ec.snapshot
    assert snap.stack is None and snap.pipeline is None
    assert snap.original_preview is None and snap.enhanced_preview is None
    assert snap.error == "project gone"
    assert not snap.can_undo and not snap.can_redo
    assert not snap.rendering
    with pytest.raises(EnhanceError):
        ec.set_param("denoise", strength=0.5)


# --- set_param + undo/redo/reset ------------------------------------------------------------------


def test_set_param_updates_pipeline_and_notifies_synchronously():
    viewer, ec = make_editor()
    viewer.open(make_stack(1), make_image())
    assert ec.wait(5.0)
    seen: list[EditorSnapshot] = []
    ec.add_listener(seen.append)
    ec.set_param("denoise", strength=0.8)
    # the synchronous notification already carries the new pipeline + rendering=True
    assert seen and seen[-1].pipeline is not None
    assert seen[-1].pipeline.ops[1].params.strength == pytest.approx(0.8)
    assert seen[-1].rendering is True
    assert seen[-1].can_undo and not seen[-1].can_redo
    assert ec.pipeline.ops[1].params.strength == pytest.approx(0.8)
    assert ec.undo_depth == 1
    assert ec.wait(5.0)
    assert seen[-1].rendering is False  # the render-apply notification
    assert seen[-1].enhanced_preview is not None


def test_undo_redo_restore_params_exactly_and_new_change_clears_redo():
    viewer, ec = make_editor()
    viewer.open(make_stack(1), make_image())
    assert ec.wait(5.0)
    default = ec.pipeline
    assert default == default_pipeline()
    ec.set_param("denoise", strength=0.8)
    assert ec.wait(5.0)
    assert ec.undo() is True
    assert ec.pipeline == default  # restored EXACTLY
    assert ec.can_redo and not ec.can_undo
    assert ec.wait(5.0)
    assert ec.redo() is True  # re-applied
    assert ec.pipeline.ops[1].params.strength == pytest.approx(0.8)
    assert ec.can_undo and not ec.can_redo
    assert ec.wait(5.0)
    # a new change clears the redo stack
    ec.set_param("sharpen", amount=1.5)
    assert not ec.can_redo
    assert ec.undo_depth == 2
    assert ec.wait(5.0)
    assert ec.undo() is True
    assert ec.pipeline.ops[1].params.strength == pytest.approx(0.8)
    assert ec.pipeline.ops[2].params.amount == pytest.approx(0.5)
    assert ec.wait(5.0)
    assert ec.undo() is True
    assert ec.pipeline == default
    assert ec.undo() is False  # nothing left
    assert ec.redo() is True
    assert ec.wait(5.0)


def test_undo_stack_is_bounded_oldest_evicted():
    viewer, ec = make_editor()
    viewer.open(make_stack(1), make_image())
    assert ec.wait(5.0)
    recorded = [ec.pipeline]
    for i in range(60):
        ec.set_param("denoise", strength=0.01 * (i + 1))
        recorded.append(ec.pipeline)
    assert ec.undo_depth == MAX_UNDO_ENTRIES == 50
    for _ in range(50):
        assert ec.undo() is True
    assert ec.undo() is False
    assert ec.pipeline == recorded[10]  # the oldest 10 entries were evicted
    assert ec.wait(5.0)


def test_reset_restores_default_and_is_undoable():
    viewer, ec = make_editor()
    viewer.open(make_stack(1), make_image())
    assert ec.wait(5.0)
    assert ec.reset() is False  # already the default -> no-op
    ec.set_param("denoise", strength=0.9)
    ec.set_param("sharpen", amount=2.0)
    assert ec.wait(5.0)
    assert ec.reset() is True
    assert ec.pipeline == default_pipeline()
    assert ec.can_undo
    assert ec.wait(5.0)
    assert ec.undo() is True  # reset is undoable
    assert ec.pipeline.ops[1].params.strength == pytest.approx(0.9)
    assert ec.pipeline.ops[2].params.amount == pytest.approx(2.0)
    assert ec.wait(5.0)


def test_set_param_without_stack_raises():
    _viewer, ec = make_editor()
    with pytest.raises(EnhanceError) as excinfo:
        ec.set_param("denoise", strength=0.5)
    assert "no stack open" in str(excinfo.value)
    assert ec.undo() is False and ec.redo() is False and ec.reset() is False


def test_set_param_invalid_changes_raise_and_leave_undo_untouched():
    viewer, ec = make_editor()
    viewer.open(make_stack(1), make_image())
    assert ec.wait(5.0)
    before = ec.pipeline
    with pytest.raises(EnhanceError):
        ec.set_param("denoise", strength=5.0)  # out of range
    with pytest.raises(EnhanceError):
        ec.set_param("no-such-op", strength=0.5)  # unknown op
    with pytest.raises(EnhanceError):
        ec.set_param("denoise", bogus_field=1)  # unknown field
    assert ec.pipeline is before
    assert ec.undo_depth == 0 and not ec.can_redo
    assert not ec.rendering


def test_set_param_with_equal_value_is_a_noop():
    viewer, ec = make_editor()
    viewer.open(make_stack(1), make_image())
    assert ec.wait(5.0)
    ec.set_param("denoise", strength=0.25)  # the default value
    assert ec.undo_depth == 0
    assert not ec.rendering
    assert ec.snapshot.pipeline == default_pipeline()


def test_constructor_validation():
    viewer = FakeViewer()
    with pytest.raises(EnhanceError):
        EditorController(viewer, render_max_size=0)  # ty: ignore[invalid-argument-type]
    with pytest.raises(EnhanceError):
        EditorController(viewer, render_max_size=True)  # ty: ignore[invalid-argument-type]
    with pytest.raises(EnhanceError):
        EditorController(viewer, render_max_size="32")  # ty: ignore[invalid-argument-type]
    with pytest.raises(EnhanceError):
        EditorController(viewer, debounce_s=-0.1)  # ty: ignore[invalid-argument-type]
    with pytest.raises(EnhanceError):
        EditorController(viewer, debounce_s="x")  # ty: ignore[invalid-argument-type]


# --- worker thread + invoke_on_main + debounce + generation ----------------------------------------


def test_queued_invoke_on_main_applies_only_on_drain():
    viewer = FakeViewer()
    queue = InvokeQueue()
    ec = EditorController(
        viewer, invoke_on_main=queue, render_max_size=32, debounce_s=0.0  # ty: ignore[invalid-argument-type]
    )
    viewer.open(make_stack(1), make_image())
    assert ec.wait(5.0)  # the worker finished and QUEUED its result
    snap = ec.snapshot
    assert snap.rendering is True  # not applied yet
    assert snap.enhanced_preview is None and snap.original_preview is None
    assert queue.pending() == 1
    queue.drain()
    snap = ec.snapshot
    assert snap.rendering is False
    assert snap.enhanced_preview is not None and snap.original_preview is not None
    assert snap.error is None


def test_debounce_coalesces_rapid_changes_into_one_render():
    viewer = FakeViewer()
    counting = CountingRender()
    ec = EditorController(
        viewer, render_max_size=32, debounce_s=0.2, render_fn=counting  # ty: ignore[invalid-argument-type]
    )
    viewer.open(make_stack(1), make_image())
    assert ec.wait(5.0)
    assert len(counting.calls) == 1  # the open render
    for i in range(10):
        ec.set_param("denoise", strength=0.1 + i * 0.09)
    assert ec.wait(5.0)
    assert len(counting.calls) == 2  # ONE render for the 10 rapid changes
    pipeline, max_size = counting.calls[-1]
    assert max_size == 32
    assert pipeline is ec.pipeline  # latest wins
    assert pipeline.ops[1].params.strength == pytest.approx(0.1 + 9 * 0.09)


def test_stale_render_results_are_discarded_by_generation():
    viewer = FakeViewer()
    queue = InvokeQueue()
    gated = GatedRender()
    ec = EditorController(
        viewer, invoke_on_main=queue, render_max_size=32, debounce_s=0.0,  # ty: ignore[invalid-argument-type]
        render_fn=gated,
    )
    image = make_image()
    viewer.open(make_stack(1), image)
    assert gated.entered.wait(5.0)  # the first render is blocked in the gate
    assert queue.pending() == 0
    ec.set_param("denoise", strength=0.9)  # a newer change -> generation 2
    assert ec.wait(5.0)  # the second worker finished and queued its result
    assert queue.pending() == 1
    gated.gate.set()  # release the stale first render
    deadline = time.monotonic() + 5.0
    while queue.pending() < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert queue.pending() == 2
    queue.drain()  # applies gen 2, discards the stale gen 1
    snap = ec.snapshot
    assert snap.error is None
    assert snap.rendering is False
    assert snap.enhanced_preview is not None
    expected = render_pipeline(ec.pipeline, image, 32)  # ty: ignore[invalid-argument-type]
    assert snap.enhanced_preview.tobytes() == expected.tobytes()


def test_set_param_never_blocks_the_caller():
    viewer = FakeViewer()

    def slow(pipeline, fits_image, max_size):
        time.sleep(0.5)
        return render_pipeline(pipeline, fits_image, max_size)

    ec = EditorController(
        viewer, render_max_size=32, debounce_s=0.0, render_fn=slow  # ty: ignore[invalid-argument-type]
    )
    viewer.open(make_stack(1), make_image())
    start = time.perf_counter()
    ec.set_param("denoise", strength=0.4)
    elapsed = time.perf_counter() - start
    assert elapsed < 0.1, f"set_param blocked for {elapsed:.3f}s"
    assert ec.wait(5.0)


def test_render_error_lands_in_snapshot_and_controller_stays_usable():
    viewer = FakeViewer()
    flaky = FlakyRender()
    ec = EditorController(
        viewer, render_max_size=32, debounce_s=0.0, render_fn=flaky  # ty: ignore[invalid-argument-type]
    )
    seen: list[EditorSnapshot] = []
    ec.add_listener(seen.append)
    viewer.open(make_stack(1), make_image())
    assert ec.wait(5.0)
    snap = ec.snapshot
    assert snap.error is not None and "render boom" in snap.error
    assert snap.enhanced_preview is None
    assert snap.original_preview is not None  # the before preview still rendered
    assert snap.rendering is False
    assert seen[-1].error is not None  # listeners notified of the error
    # still usable: fix the render and change a param
    flaky.fail = False
    ec.set_param("denoise", strength=0.7)
    assert ec.wait(5.0)
    snap = ec.snapshot
    assert snap.error is None
    assert snap.enhanced_preview is not None
    assert snap.pipeline.ops[1].params.strength == pytest.approx(0.7)  # ty: ignore[missing-union-attribute]


# --- listeners + frozen snapshot --------------------------------------------------------------------


def test_listeners_add_remove_idempotent_and_isolated():
    viewer, ec = make_editor()
    calls: list[EditorSnapshot] = []

    def raising(_snapshot):
        raise RuntimeError("boom")

    ec.add_listener(calls.append)
    ec.add_listener(calls.append)  # idempotent
    ec.add_listener(raising)
    assert len(ec.listeners) == 2

    @ec.add_listener
    def on_change(snapshot):
        on_change.seen = snapshot

    assert len(ec.listeners) == 3
    viewer.open(make_stack(1), make_image())  # the raising listener must not break the others
    assert len(calls) == 1
    assert on_change.seen.stack is not None
    ec.remove_listener(calls.append)
    ec.remove_listener(calls.append)  # no-op when absent
    assert len(ec.listeners) == 2
    assert ec.wait(5.0)


def test_snapshot_is_frozen():
    _viewer, ec = make_editor()
    snap = ec.snapshot
    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.error = "x"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.rendering = True  # type: ignore[misc]


# --- package boundary + exports ----------------------------------------------------------------------


def test_editor_controller_module_imports_no_gi_no_link_no_ui_widget_stays_lazy():
    code = (
        "import sys;"
        "import seestar_controller.enhance;"
        "import seestar_controller.enhance.editor_controller;"
        "assert 'gi' not in sys.modules, 'GTK imported';"
        "assert 'seestar_controller.link' not in sys.modules, 'link imported eagerly';"
        "assert 'seestar_controller.ui' not in sys.modules, 'ui imported eagerly';"
        "assert 'seestar_controller.enhance.enhance_view' not in sys.modules, "
        "'enhance_view imported eagerly';"
        "assert 'seestar_controller.enhance.viewer' not in sys.modules, "
        "'viewer imported eagerly';"
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


def test_editor_controller_source_has_no_gi_and_widget_module_ordered():
    source = (ENHANCE_PACKAGE_DIR / "editor_controller.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(import gi|from gi)\b", source, re.MULTILINE), (
        "pure module editor_controller imports gi"
    )
    lines = (ENHANCE_PACKAGE_DIR / "enhance_view.py").read_text(encoding="utf-8").splitlines()
    gi_lines = [i for i, line in enumerate(lines) if line.strip() == "import gi"]
    assert gi_lines, "widget module enhance_view has no 'import gi' at module top"
    repo_line = next(
        i for i, line in enumerate(lines) if line.startswith("from gi.repository")
    )
    assert gi_lines[0] < repo_line, "'import gi' must come before 'from gi.repository'"
    assert enhance._WIDGET_EXPORTS == {
        "EnhanceEditor": "enhance_view",
        "ImageViewer": "viewer",
    }


def test_editor_api_exported_from_package():
    for name in (
        "EditorController",
        "EditorSnapshot",
        "EditorListener",
        "InvokeOnMain",
        "RenderFn",
        "default_render_fn",
        "MAX_UNDO_ENTRIES",
        "DEFAULT_DEBOUNCE_S",
    ):
        assert name in enhance.__all__, name
        assert hasattr(enhance, name), name
    assert "EnhanceEditor" in enhance.__all__ and "EnhanceEditor" in enhance._WIDGET_EXPORTS
    assert enhance.MAX_UNDO_ENTRIES == 50
    assert enhance.DEFAULT_DEBOUNCE_S == 0.05
    assert enhance.EditorController is editor_controller.EditorController
    assert enhance.EditorSnapshot is editor_controller.EditorSnapshot
    assert enhance.default_render_fn is editor_controller.default_render_fn


# --- integration with a real ViewerController ---------------------------------------------------------


def test_integration_with_real_viewer_controller(project, stored_stacks):
    stack_frames = stored_stacks
    vc = ViewerController(project)
    ec = EditorController(vc, render_max_size=32, debounce_s=0.0)
    assert ec.snapshot.stack is None
    vc.open_stack(stack_frames[0].id)
    assert ec.wait(5.0)
    snap = ec.snapshot
    assert snap.stack is not None and snap.stack.frame_id == stack_frames[0].id
    assert snap.pipeline == default_pipeline()
    assert snap.original_preview is not None and snap.enhanced_preview is not None
    ec.set_param("denoise", strength=0.9)
    assert ec.wait(5.0)
    assert ec.snapshot.pipeline.ops[1].params.strength == pytest.approx(0.9)  # ty: ignore[missing-union-attribute]
    assert ec.snapshot.enhanced_preview is not None
    assert ec.undo() is True
    assert ec.wait(5.0)
    assert ec.snapshot.pipeline == default_pipeline()
    # switching to the other stack resets the editor
    vc.open_stack(stack_frames[1].id)
    assert ec.wait(5.0)
    assert ec.snapshot.stack.frame_id == stack_frames[1].id  # ty: ignore[missing-attribute]
    assert ec.snapshot.pipeline == default_pipeline()
    assert ec.undo_depth == 0
