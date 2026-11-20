"""Observing — tonight's plan (queue) + editor, the night executor, and the
Observe view wiring (ticket 03).

Spec: ``.scratch/observing/spec.md``. Strict two-layer split (the sandbox has
no GTK, see docs/wiki/assumptions.md):

- **Pure layer** — :mod:`.queue` (the plan model: :class:`~.queue.QueueEntry`,
  :class:`~.queue.PlanQueue`, :class:`~.queue.QueueEvent`,
  :func:`~.queue.resolve_names`, :class:`~.queue.ResolutionReport`,
  :func:`~.queue.parse_entry_coords`, the stacking ranges mirrored from the
  link) and :mod:`.queue_controller` (:class:`~.queue_controller.PlanQueueController`
  — the app-level shared controller the Observe view drives). Both import
  **nothing** from ``gi`` and nothing from the link package —
  stdlib + :mod:`seestar_controller.catalog` only, fully unit-testable
  headless; a subprocess test enforces the boundary.
- **Executor layer** — :mod:`.executor` (:class:`~.executor.NightExecutor`,
  :class:`~.executor.ExecutorState`, :class:`~.executor.ExecutorSnapshot`,
  :class:`~.executor.ExecutorEvent`, the error policies): the headless night
  state machine driving the link Device facade through the queue; and
  :mod:`.observe_controller` (:class:`~.observe_controller.ObserveController`
  + :class:`~.observe_controller.ObserveViewState`, the pure binding of
  connection + queue + executor + preview stream + notification bus that the
  Observe view renders — ticket 03). Both import **no ``gi``** but do import
  the link + store + ui controllers (stdlib + link + store + ui.connection /
  ui.notifications + :mod:`.queue`), so they are re-exported here **lazily**
  (PEP 562) — importing this package alone never pulls the link into
  ``sys.modules``.
- **Widget layer** — :mod:`.queue_editor` (:class:`~.queue_editor.QueueEditor`,
  the Gtk.ListBox editor with per-row reorder/remove/duplicate buttons and the
  add-by-name / add-by-RA-Dec area) and :mod:`.observe_view`
  (:class:`~.observe_view.ObserveView`, the two-pane Observe view: queue
  editor + night controls on the left, live preview + progress on the right).
  Both import ``gi`` at module top and are re-exported here **lazily**
  (PEP 562 ``__getattr__``), so ``import seestar_controller.observing``
  never imports ``gi``.

On the desktop, PyGObject is not a pip dependency — install the system
packages ``python3-gi gir1.2-gtk-4.0 gir1.2-adw-1`` (see the README).
"""

from .queue import (
    DEFAULT_EXPOSURE_S,
    DEFAULT_GAIN,
    DEFAULT_MOSAIC,
    EXPOSURE_MAX_S,
    EXPOSURE_MIN_S,
    EVENT_ADDED,
    EVENT_CLEARED,
    EVENT_DUPLICATED,
    EVENT_MOVED,
    EVENT_REMOVED,
    EVENT_RESOLVED,
    GAIN_MAX,
    GAIN_MIN,
    MOSAIC_MAX_DEG,
    MOSAIC_MIN_DEG,
    QUEUE_EVENT_KINDS,
    Mosaic,
    NameResolver,
    PlanQueue,
    QueueEntry,
    QueueError,
    QueueEvent,
    QueueListener,
    ResolutionReport,
    default_name_resolver,
    describe_mosaic,
    normalize_mosaic,
    parse_entry_coords,
    resolve_names,
)
from .queue_controller import PlanQueueController

#: Widget-layer names re-exported lazily (importing the modules pulls in
#: ``gi`` — only the GUI path does).
_WIDGET_EXPORTS = {
    "ObserveView": "observe_view",
    "QueueEditor": "queue_editor",
}

#: Executor-layer names re-exported lazily from :mod:`.executor` and
#: :mod:`.observe_controller` (importing them pulls in the link + store —
#: only the executor path does; importing this package alone stays link-free).
_EXECUTOR_EXPORTS = {
    "EXECUTOR_STATES": "executor",
    "ERROR_POLICIES": "executor",
    "ERROR_POLICY_ABORT": "executor",
    "ERROR_POLICY_SKIP": "executor",
    "ExecutorEvent": "executor",
    "ExecutorListener": "executor",
    "ExecutorSnapshot": "executor",
    "ExecutorState": "executor",
    "ExecutorStateError": "executor",
    "NightExecutor": "executor",
    "DEFAULT_PREVIEW_MAX_FPS": "observe_controller",
    "ExecutorFactory": "observe_controller",
    "InvokeOnMain": "observe_controller",
    "OBSERVE_SOURCE": "observe_controller",
    "ObserveController": "observe_controller",
    "ObserveStateError": "observe_controller",
    "ObserveViewListener": "observe_controller",
    "ObserveViewState": "observe_controller",
    "PreviewFrameListener": "observe_controller",
    "PreviewStreamFactory": "observe_controller",
    "default_executor_factory": "observe_controller",
    "default_preview_stream_factory": "observe_controller",
    "format_elapsed": "observe_controller",
    "format_target": "observe_controller",
}


def __getattr__(name: str):
    if name in _WIDGET_EXPORTS:
        from importlib import import_module

        module = import_module(f".{_WIDGET_EXPORTS[name]}", __name__)
        return getattr(module, name)
    if name in _EXECUTOR_EXPORTS:
        from importlib import import_module

        module = import_module(f".{_EXECUTOR_EXPORTS[name]}", __name__)
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "DEFAULT_EXPOSURE_S",
    "DEFAULT_GAIN",
    "DEFAULT_MOSAIC",
    "EXPOSURE_MAX_S",
    "EXPOSURE_MIN_S",
    "EVENT_ADDED",
    "EVENT_CLEARED",
    "EVENT_DUPLICATED",
    "EVENT_MOVED",
    "EVENT_REMOVED",
    "EVENT_RESOLVED",
    "GAIN_MAX",
    "GAIN_MIN",
    "MOSAIC_MAX_DEG",
    "MOSAIC_MIN_DEG",
    "QUEUE_EVENT_KINDS",
    "Mosaic",
    "NameResolver",
    "PlanQueue",
    "PlanQueueController",
    "QueueEntry",
    "QueueError",
    "QueueEvent",
    "QueueListener",
    "ResolutionReport",
    "default_name_resolver",
    "describe_mosaic",
    "normalize_mosaic",
    "parse_entry_coords",
    "resolve_names",
    # executor layer (lazy, imports link + store):
    "EXECUTOR_STATES",
    "ERROR_POLICIES",
    "ERROR_POLICY_ABORT",
    "ERROR_POLICY_SKIP",
    "ExecutorEvent",
    "ExecutorListener",
    "ExecutorSnapshot",
    "ExecutorState",
    "ExecutorStateError",
    "NightExecutor",
    # observe controller (lazy, imports link + store + ui controllers):
    "DEFAULT_PREVIEW_MAX_FPS",
    "ExecutorFactory",
    "InvokeOnMain",
    "OBSERVE_SOURCE",
    "ObserveController",
    "ObserveStateError",
    "ObserveViewListener",
    "ObserveViewState",
    "PreviewFrameListener",
    "PreviewStreamFactory",
    "default_executor_factory",
    "default_preview_stream_factory",
    "format_elapsed",
    "format_target",
    # widget layer (lazy, imports gi):
    "ObserveView",
    "QueueEditor",
]
