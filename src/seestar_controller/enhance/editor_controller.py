"""The Enhance-editor controller — the PURE controller layer of
:mod:`seestar_controller.enhance` (image-enhance ticket 03).

This is a PURE module: **no ``gi`` import** (the sandbox has no GTK, see
docs/wiki/assumptions.md; a subprocess test enforces the boundary) — stdlib +
numpy/PIL + the sibling pure modules only. The widget layer
(:mod:`.enhance_view`) imports ``gi`` at module top and is never imported by
this module or eagerly by the package ``__init__``.

:class:`EditorController` hangs off the app-level shared
:class:`~.viewer_controller.ViewerController` (it listens to its snapshots)
and owns the **editing state of the open stack**: the current
:class:`~.pipeline.Pipeline`, the undo/redo stacks of parameter changes, and
the two previews — the **before/after** pair is simply
:attr:`EditorSnapshot.original_preview` (the raw :class:`~.fits_io.FitsImage`
tone-mapped via :func:`~.render.render_preview`) vs
:attr:`EditorSnapshot.enhanced_preview` (the pipeline applied via
:func:`~.pipeline.render_pipeline`).

- :meth:`set_param` — ``set_param(op_name, **changes)`` updates the pipeline
  via :meth:`~.pipeline.Pipeline.with_param` (immutable, re-validated), pushes
  the previous pipeline onto a bounded undo stack (:data:`MAX_UNDO_ENTRIES`),
  clears the redo stack, and schedules a re-render. Invalid changes raise
  :class:`~.fits_io.EnhanceError` and leave the undo stack untouched;
- :meth:`undo` / :meth:`redo` — swap the stacks and re-render (return whether
  anything changed); :meth:`reset` — back to :func:`~.pipeline.default_pipeline`;
- **re-render never blocks the caller**: the render runs on a worker
  :class:`threading.Thread` (the same pattern as the ui
  :class:`~seestar_controller.ui.connection.ConnectionController`) and its
  result is marshalled back through ``invoke_on_main`` (the app binds
  ``GLib.idle_add``; the default calls it directly, so tests are synchronous
  once :meth:`wait` joins the worker);
- **debounced**: rapid slider moves coalesce into ONE render (latest wins) —
  a :class:`threading.Timer` of ``debounce_s`` reschedules on every change
  (``debounce_s=0`` renders immediately on a worker), and a generation
  counter discards stale worker results at apply time;
- listeners — :meth:`add_listener` / :meth:`remove_listener`, notified with a
  frozen :class:`EditorSnapshot` after every change (parameter change,
  undo/redo/reset, stack opened/closed, render result). Listener exceptions
  are isolated (logged, never propagated), mirroring the viewer controller;
- **errors never crash**: a failing render lands in
  :attr:`EditorSnapshot.error` (and the listeners are notified); the
  controller stays usable and the next change re-renders.

Opening/closing a stack is driven by the viewer controller: when its snapshot
shows a different open stack, the editor resets to
:func:`~.pipeline.default_pipeline`, clears undo/redo, and re-renders both
previews (a stale in-flight render of the previous stack is discarded by the
generation counter).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

from PIL import Image

from .fits_io import EnhanceError, FitsImage, StackInfo
from .pipeline import Pipeline, default_pipeline, render_pipeline
from .render import DEFAULT_MAX_PREVIEW_SIZE, render_preview
from .viewer_controller import ViewerController, ViewerSnapshot

logger = logging.getLogger(__name__)

#: Maximum number of pipelines kept on the undo stack (oldest evicted).
MAX_UNDO_ENTRIES = 50

#: Default debounce interval (seconds) between a parameter change and the
#: re-render — rapid slider moves coalesce into one render.
DEFAULT_DEBOUNCE_S = 0.05


@dataclass(frozen=True)
class EditorSnapshot:
    """An immutable snapshot of the editor state, handed to the listeners on
    any change.

    - ``stack`` — the currently open stack (None when none is open);
    - ``pipeline`` — the current :class:`~.pipeline.Pipeline` for the open
      stack (None when no stack is open);
    - ``enhanced_preview`` — the "after" preview: the pipeline applied to the
      stack and tone-mapped (:class:`PIL.Image.Image`), None until the first
      render completes;
    - ``original_preview`` — the "before" preview: the raw
      :class:`~.fits_io.FitsImage` tone-mapped via
      :func:`~.render.render_preview` (the before/after comparison is just
      these two fields);
    - ``rendering`` — True while a re-render is scheduled or in flight;
    - ``can_undo`` / ``can_redo`` — whether :meth:`EditorController.undo` /
      :meth:`EditorController.redo` would change anything;
    - ``error`` — the last render error message, else None.
    """

    stack: StackInfo | None
    pipeline: Pipeline | None
    enhanced_preview: Image.Image | None
    original_preview: Image.Image | None
    rendering: bool
    can_undo: bool
    can_redo: bool
    error: str | None


#: An editor listener: called with an :class:`EditorSnapshot` on any change.
EditorListener = Callable[[EditorSnapshot], None]

#: Marshals a zero-argument callback onto the "main" thread. The widget
#: layer binds this to ``GLib.idle_add``; the default calls it directly.
InvokeOnMain = Callable[[Callable[[], None]], Any]

#: The render function: ``render_fn(pipeline, fits_image, max_size) -> PIL
#: image`` (the enhanced/"after" preview). Injectable for tests.
RenderFn = Callable[[Pipeline, FitsImage, int], Image.Image]


def _direct_invoke(callback: Callable[[], None]) -> None:
    """The default ``invoke_on_main``: run the callback directly (tests)."""
    callback()
    return None


def default_render_fn(
    pipeline: Pipeline, fits_image: FitsImage, max_size: int
) -> Image.Image:
    """The default :data:`RenderFn`: :func:`~.pipeline.render_pipeline`."""
    return render_pipeline(pipeline, fits_image, max_size)


class EditorController:
    """The editor state of the open stack: pipeline + undo/redo + previews.

    - ``viewer_controller`` — the shared
      :class:`~.viewer_controller.ViewerController`; the editor listens to
      it and follows the open stack (a different stack -> fresh
      :func:`~.pipeline.default_pipeline`, cleared undo/redo, re-render);
    - ``invoke_on_main`` — the marshalling callable for worker results
      (default: direct call);
    - ``render_max_size`` — the preview downscale bound (see
      :func:`~.render.render_preview`);
    - ``debounce_s`` — the debounce interval of the re-render (0 = render
      immediately on the worker);
    - ``render_fn`` — the enhanced-preview render (default
      :func:`default_render_fn`).
    """

    def __init__(
        self,
        viewer_controller: ViewerController,
        *,
        invoke_on_main: InvokeOnMain | None = None,
        render_max_size: int = DEFAULT_MAX_PREVIEW_SIZE,
        debounce_s: float = DEFAULT_DEBOUNCE_S,
        render_fn: RenderFn | None = None,
    ) -> None:
        if isinstance(render_max_size, bool) or not isinstance(render_max_size, int):
            raise EnhanceError(f"render_max_size must be an int, got {render_max_size!r}")
        if render_max_size <= 0:
            raise EnhanceError(f"render_max_size must be > 0, got {render_max_size!r}")
        if isinstance(debounce_s, bool) or not isinstance(debounce_s, (int, float)):
            raise EnhanceError(f"debounce_s must be a number, got {debounce_s!r}")
        if float(debounce_s) < 0.0:
            raise EnhanceError(f"debounce_s must be >= 0, got {debounce_s!r}")
        self._viewer_controller = viewer_controller
        self._invoke_on_main: InvokeOnMain = invoke_on_main or _direct_invoke
        self._render_max_size = int(render_max_size)
        self._debounce_s = float(debounce_s)
        self._render_fn: RenderFn = render_fn or default_render_fn
        self._lock = threading.Lock()
        self._listeners: list[EditorListener] = []
        self._stack: StackInfo | None = None
        self._image: FitsImage | None = None
        self._pipeline: Pipeline | None = None
        self._undo: deque[Pipeline] = deque(maxlen=MAX_UNDO_ENTRIES)
        self._redo: deque[Pipeline] = deque()
        self._original_preview: Image.Image | None = None
        self._enhanced_preview: Image.Image | None = None
        self._rendering = False
        self._error: str | None = None
        self._generation = 0
        self._timer: threading.Timer | None = None
        self._worker: threading.Thread | None = None
        viewer_controller.add_listener(self._on_viewer_snapshot)
        self._on_viewer_snapshot(viewer_controller.snapshot)

    # -- state ---------------------------------------------------------------

    @property
    def viewer_controller(self) -> ViewerController:
        """The viewer controller this editor follows."""
        return self._viewer_controller

    @property
    def stack(self) -> StackInfo | None:
        """The currently open stack (None when none is open)."""
        with self._lock:
            return self._stack

    @property
    def pipeline(self) -> Pipeline | None:
        """The current pipeline of the open stack (None when no stack)."""
        with self._lock:
            return self._pipeline

    @property
    def rendering(self) -> bool:
        """True while a re-render is scheduled or in flight."""
        with self._lock:
            return self._rendering

    @property
    def error(self) -> str | None:
        """The last render error message, else None."""
        with self._lock:
            return self._error

    @property
    def can_undo(self) -> bool:
        with self._lock:
            return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        with self._lock:
            return bool(self._redo)

    @property
    def undo_depth(self) -> int:
        """The number of pipelines on the undo stack (test/debug helper)."""
        with self._lock:
            return len(self._undo)

    @property
    def redo_depth(self) -> int:
        """The number of pipelines on the redo stack (test/debug helper)."""
        with self._lock:
            return len(self._redo)

    @property
    def snapshot(self) -> EditorSnapshot:
        """The frozen :class:`EditorSnapshot` of the current state."""
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> EditorSnapshot:
        return EditorSnapshot(
            stack=self._stack,
            pipeline=self._pipeline,
            enhanced_preview=self._enhanced_preview,
            original_preview=self._original_preview,
            rendering=self._rendering,
            can_undo=bool(self._undo),
            can_redo=bool(self._redo),
            error=self._error,
        )

    # -- listeners --------------------------------------------------------------

    @property
    def listeners(self) -> tuple[EditorListener, ...]:
        """The registered observer callbacks, in registration order."""
        return tuple(self._listeners)

    def add_listener(self, listener: EditorListener) -> EditorListener:
        """Register an observer callback (idempotent); returns it, so it can
        be used as a decorator. The listener receives a frozen
        :class:`EditorSnapshot` on any change."""
        if listener not in self._listeners:
            self._listeners.append(listener)
        return listener

    def remove_listener(self, listener: EditorListener) -> None:
        """Unregister an observer callback (no-op when not registered)."""
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _notify(self, snapshot: EditorSnapshot) -> None:
        """Hand a frozen snapshot to every listener; a failing listener is
        logged and isolated (never breaks the others or the caller)."""
        for listener in list(self._listeners):
            try:
                listener(snapshot)
            except Exception:
                logger.exception("enhance editor listener failed")

    # -- parameter changes + undo/redo/reset --------------------------------------

    def set_param(self, op_name: str, **changes: Any) -> None:
        """Change one op's params: ``set_param("denoise", strength=0.9)``.

        The pipeline is updated via :meth:`~.pipeline.Pipeline.with_param`
        (immutable, re-validated), the PREVIOUS pipeline is pushed onto the
        bounded undo stack, the redo stack is cleared, and a debounced
        re-render is scheduled (never blocking the caller). A change that
        results in an equal pipeline is a no-op (no undo entry, no render).

        Raises :class:`EnhanceError` when no stack is open, the op is not in
        the pipeline, or a change is invalid — in that case the undo stack
        and the pipeline are left untouched.
        """
        with self._lock:
            if self._pipeline is None or self._image is None:
                raise EnhanceError(
                    "no stack open — open one via the ViewerController first"
                )
            try:
                new_pipeline = self._pipeline.with_param(op_name, **changes)
            except EnhanceError:
                raise
            except TypeError as exc:
                raise EnhanceError(f"set_param({op_name!r}): {exc}") from exc
            if new_pipeline == self._pipeline:
                return  # no change — no undo entry, no re-render
            self._undo.append(self._pipeline)
            self._pipeline = new_pipeline
            self._redo.clear()
            self._error = None
            start = self._schedule_locked()
            snapshot = self._snapshot_locked()
        start()
        self._notify(snapshot)

    def undo(self) -> bool:
        """Restore the previous pipeline (undo stack -> redo stack) and
        re-render. Returns False (no-op) when there is nothing to undo."""
        with self._lock:
            if not self._undo or self._pipeline is None:
                return False
            self._redo.append(self._pipeline)
            self._pipeline = self._undo.pop()
            self._error = None
            start = self._schedule_locked()
            snapshot = self._snapshot_locked()
        start()
        self._notify(snapshot)
        return True

    def redo(self) -> bool:
        """Re-apply the last undone pipeline (redo stack -> undo stack) and
        re-render. Returns False (no-op) when there is nothing to redo."""
        with self._lock:
            if not self._redo or self._pipeline is None:
                return False
            self._undo.append(self._pipeline)
            self._pipeline = self._redo.pop()
            self._error = None
            start = self._schedule_locked()
            snapshot = self._snapshot_locked()
        start()
        self._notify(snapshot)
        return True

    def reset(self) -> bool:
        """Reset the pipeline to :func:`~.pipeline.default_pipeline` (the
        current pipeline is pushed onto the undo stack, so reset is
        undoable). Returns False (no-op) when no stack is open or the
        pipeline is already the default."""
        with self._lock:
            if self._pipeline is None:
                return False
            if self._pipeline == default_pipeline():
                return False
            self._undo.append(self._pipeline)
            self._pipeline = default_pipeline()
            self._redo.clear()
            self._error = None
            start = self._schedule_locked()
            snapshot = self._snapshot_locked()
        start()
        self._notify(snapshot)
        return True

    # -- viewer-controller binding -------------------------------------------------

    def _on_viewer_snapshot(self, vsnap: ViewerSnapshot) -> None:
        """Viewer listener: a different open stack -> reset the editor state
        (default pipeline, cleared undo/redo) and re-render both previews;
        no stack -> clear everything."""
        with self._lock:
            current, image = vsnap.current, vsnap.image
            same = (current is None and self._stack is None) or (
                current is not None
                and self._stack is not None
                and current.frame_id == self._stack.frame_id
                and image is self._image
            )
            if same:
                return
            self._stack = current
            self._image = image
            if current is not None and image is not None:
                self._pipeline = default_pipeline()
                self._error = None
                start = self._schedule_locked()
            else:
                self._pipeline = None
                self._error = vsnap.error
                self._cancel_timer_locked()
                self._generation += 1  # discard any in-flight render
                self._rendering = False
                start = None
            self._undo.clear()
            self._redo.clear()
            self._original_preview = None
            self._enhanced_preview = None
            snapshot = self._snapshot_locked()
        if start is not None:
            start()
        self._notify(snapshot)

    # -- rendering (worker thread + debounce + generation) ---------------------------

    def _cancel_timer_locked(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _schedule_locked(self) -> Callable[[], None]:
        """(lock held) Schedule a debounced re-render of the current pipeline;
        returns the starter to call OUTSIDE the lock. Latest wins: every
        schedule bumps the generation, so stale worker results are discarded
        at apply time."""
        self._generation += 1
        generation = self._generation
        self._rendering = True
        self._cancel_timer_locked()
        if self._debounce_s <= 0.0:
            worker = self._make_worker_locked(generation)
            self._worker = worker
            return worker.start
        timer = threading.Timer(
            self._debounce_s,
            self._fire,
            args=(generation,),
        )
        timer.name = "seestar-enhance-debounce"
        timer.daemon = True
        self._timer = timer
        return timer.start

    def _make_worker_locked(self, generation: int) -> threading.Thread:
        """(lock held) Build the render worker for ``generation``, capturing
        the current pipeline + image."""
        pipeline, image = self._pipeline, self._image

        def run() -> None:
            self._render_worker(generation, pipeline, image)

        return threading.Thread(
            target=run, name="seestar-enhance-render", daemon=True
        )

    def _fire(self, generation: int) -> None:
        """Debounce timer fired: start the render worker (unless superseded)."""
        with self._lock:
            if generation != self._generation:
                return  # a newer change rescheduled
            self._timer = None
            worker = self._make_worker_locked(generation)
            self._worker = worker
        worker.start()

    def _render_worker(
        self, generation: int, pipeline: Pipeline | None, image: FitsImage | None
    ) -> None:
        """Worker thread body: render the before/after previews (the original
        via :func:`~.render.render_preview`, the enhanced via ``render_fn``),
        then marshal the result back via ``invoke_on_main``. Errors are
        captured, never raised."""
        original: Image.Image | None = None
        enhanced: Image.Image | None = None
        errors: list[str] = []
        if image is not None:
            try:
                original = render_preview(image, max_size=self._render_max_size)
            except Exception as exc:
                errors.append(f"original preview: {type(exc).__name__}: {exc}")
            if pipeline is not None:
                try:
                    enhanced = self._render_fn(pipeline, image, self._render_max_size)
                except Exception as exc:
                    errors.append(f"enhanced preview: {type(exc).__name__}: {exc}")
        error = "; ".join(errors) if errors else None
        if error is not None:
            logger.warning("enhance render failed (generation %s): %s", generation, error)
        self._invoke_on_main(
            lambda: self._apply_render(generation, original, enhanced, error)
        )

    def _apply_render(
        self,
        generation: int,
        original: Image.Image | None,
        enhanced: Image.Image | None,
        error: str | None,
    ) -> None:
        """Apply a worker result (runs via ``invoke_on_main``): store the
        previews, clear the rendering flag, notify. A stale generation (a
        newer change or stack happened meanwhile) is discarded."""
        with self._lock:
            if generation != self._generation:
                return  # stale — a newer render is scheduled/in flight
            self._rendering = False
            if original is not None:
                self._original_preview = original
            if enhanced is not None:
                self._enhanced_preview = enhanced
            self._error = error
            snapshot = self._snapshot_locked()
        self._notify(snapshot)

    def wait(self, timeout: float | None = None) -> bool:
        """Join the in-flight debounce timer and render worker (if any);
        True when neither is running (or both finished within ``timeout``).

        With the default direct ``invoke_on_main`` the result is already
        applied when :meth:`wait` returns True; with an async marshalling
        (``GLib.idle_add``, or a queued test double) the transition is applied
        when the main loop drains the queued callback.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                timer, worker = self._timer, self._worker
            for thread in (timer, worker):
                if thread is None:
                    continue
                remaining = (
                    None if deadline is None else max(0.0, deadline - time.monotonic())
                )
                thread.join(remaining)
                if thread.is_alive():
                    return False
            with self._lock:
                if self._timer is timer and self._worker is worker:
                    return True  # stable: nothing new appeared while joining
            if deadline is not None and time.monotonic() >= deadline:
                return False
