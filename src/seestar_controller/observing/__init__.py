"""Observing — tonight's plan (queue) + editor, the night executor, the
Observe view wiring (ticket 03), and session capture (ticket 04).

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
  Observe view renders — ticket 03, plus the ticket-08 first-sub preview
  fallback (:class:`~.observe_controller.FirstSubFrame`,
  :func:`~.observe_controller.load_first_sub_frame`); and :mod:`.capture`
  (:class:`~.capture.SessionCapture` + :class:`~.capture.SessionLog`,
  :class:`~.capture.LogEvent` — sub-frames/Stacks downloaded into the current
  session as they arrive + the per-session event log — ticket 04). These
  import **no ``gi``** but do import the link + store + ui controllers
  (stdlib + link + store + ui.connection / ui.notifications +
  :mod:`.queue`), so they are re-exported here **lazily** (PEP 562) —
  importing this package alone never pulls the link into ``sys.modules``.
- **Widget layer** — :mod:`.queue_editor` (:class:`~.queue_editor.QueueEditor`,
  the Gtk.ListBox editor with per-row reorder/remove/duplicate buttons and the
  add-by-name / add-by-RA-Dec area), :mod:`.observe_view`
  (:class:`~.observe_view.ObserveView`, the two-pane Observe view: queue
  editor + night controls on the left, live preview + progress on the right),
  :mod:`.session_log_dialog` (:class:`~.session_log_dialog.SessionLogDialog`,
  the per-session event log + stored frames dialog opened from the Observe
  view — ticket 04), :mod:`.stale_session_dialog`
  (:class:`~.stale_session_dialog.StaleSessionDialog`, the resume /
  close-and-start-new offer shown when Start night hits a stale open session —
  ticket 05), and :mod:`.report_panel` (:class:`~.report_panel.SessionReportPanel`,
  the per-session "Generate report" / "Export PDF" actions + saved-reports
  list embedded in the session log dialog — session-report ticket 03). All
  import ``gi`` at module top and are re-exported here
  **lazily** (PEP 562 ``__getattr__``), so ``import seestar_controller.observing``
  never imports ``gi``.

On the desktop, PyGObject is not a pip dependency — install the system
packages ``python3-gi gir1.2-gtk-4.0 gir1.2-adw-1`` (see the README).
"""

from .queue import (
    DEFAULT_EXPOSURE_S,
    DEFAULT_GAIN,
    DEFAULT_MOSAIC,
    EVENT_ADDED,
    EVENT_CLEARED,
    EVENT_DUPLICATED,
    EVENT_MOVED,
    EVENT_REMOVED,
    EVENT_RESOLVED,
    EXPOSURE_MAX_S,
    EXPOSURE_MIN_S,
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
    "SessionLogDialog": "session_log_dialog",
    "SessionReportPanel": "report_panel",
    "StaleSessionDialog": "stale_session_dialog",
}

#: Executor-layer names re-exported lazily from :mod:`.executor`,
#: :mod:`.observe_controller` and :mod:`.capture` (importing them pulls in the
#: link + store — only the executor path does; importing this package alone
#: stays link-free).
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
    "CAPTURE_SOURCE": "capture",
    "KIND_CAPTURE": "capture",
    "KIND_ERROR": "capture",
    "KIND_EXECUTOR": "capture",
    "KIND_FRAME": "capture",
    "KIND_GOTO": "capture",
    "KIND_SESSION": "capture",
    "KIND_STACK": "capture",
    "LOG_KINDS": "capture",
    "LOG_SOURCE_CAPTURE": "capture",
    "LOG_SOURCE_EXECUTOR": "capture",
    "LOG_SOURCES": "capture",
    "SESSION_LOG_FILE": "capture",
    "LogEvent": "capture",
    "LogListener": "capture",
    "SessionCapture": "capture",
    "SessionLog": "capture",
    "classify_executor_event": "capture",
    "session_log_path": "capture",
    "DEFAULT_PREVIEW_MAX_FPS": "observe_controller",
    "CaptureFactory": "observe_controller",
    "ExecutorFactory": "observe_controller",
    "FIRST_SUB_PREVIEW_MAX_PX": "observe_controller",
    "FIRST_SUB_SOURCE": "observe_controller",
    "FirstSubFrame": "observe_controller",
    "InvokeOnMain": "observe_controller",
    "OBSERVE_SOURCE": "observe_controller",
    "ObserveController": "observe_controller",
    "ObserveStateError": "observe_controller",
    "ObserveViewListener": "observe_controller",
    "ObserveViewState": "observe_controller",
    "PreviewFrameListener": "observe_controller",
    "PreviewStreamFactory": "observe_controller",
    "StaleSessionError": "observe_controller",
    "default_capture_factory": "observe_controller",
    "default_executor_factory": "observe_controller",
    "default_preview_stream_factory": "observe_controller",
    "format_elapsed": "observe_controller",
    "format_session_time": "observe_controller",
    "format_target": "observe_controller",
    "load_first_sub_frame": "observe_controller",
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
    # session capture (lazy, imports link + store + ui.notifications):
    "CAPTURE_SOURCE",
    "DEFAULT_EXPOSURE_S",
    "DEFAULT_GAIN",
    "DEFAULT_MOSAIC",
    # observe controller (lazy, imports link + store + ui controllers):
    "DEFAULT_PREVIEW_MAX_FPS",
    "ERROR_POLICIES",
    "ERROR_POLICY_ABORT",
    "ERROR_POLICY_SKIP",
    "EVENT_ADDED",
    "EVENT_CLEARED",
    "EVENT_DUPLICATED",
    "EVENT_MOVED",
    "EVENT_REMOVED",
    "EVENT_RESOLVED",
    # executor layer (lazy, imports link + store):
    "EXECUTOR_STATES",
    "EXPOSURE_MAX_S",
    "EXPOSURE_MIN_S",
    "FIRST_SUB_PREVIEW_MAX_PX",
    "FIRST_SUB_SOURCE",
    "GAIN_MAX",
    "GAIN_MIN",
    "KIND_CAPTURE",
    "KIND_ERROR",
    "KIND_EXECUTOR",
    "KIND_FRAME",
    "KIND_GOTO",
    "KIND_SESSION",
    "KIND_STACK",
    "LOG_KINDS",
    "LOG_SOURCES",
    "LOG_SOURCE_CAPTURE",
    "LOG_SOURCE_EXECUTOR",
    "MOSAIC_MAX_DEG",
    "MOSAIC_MIN_DEG",
    "OBSERVE_SOURCE",
    "QUEUE_EVENT_KINDS",
    "SESSION_LOG_FILE",
    "CaptureFactory",
    "ExecutorEvent",
    "ExecutorFactory",
    "ExecutorListener",
    "ExecutorSnapshot",
    "ExecutorState",
    "ExecutorStateError",
    "FirstSubFrame",
    "InvokeOnMain",
    "LogEvent",
    "LogListener",
    "Mosaic",
    "NameResolver",
    "NightExecutor",
    "ObserveController",
    "ObserveStateError",
    # widget layer (lazy, imports gi):
    "ObserveView",
    "ObserveViewListener",
    "ObserveViewState",
    "PlanQueue",
    "PlanQueueController",
    "PreviewFrameListener",
    "PreviewStreamFactory",
    "QueueEditor",
    "QueueEntry",
    "QueueError",
    "QueueEvent",
    "QueueListener",
    "ResolutionReport",
    "SessionCapture",
    "SessionLog",
    "SessionLogDialog",
    "SessionReportPanel",
    "StaleSessionDialog",
    "StaleSessionError",
    "classify_executor_event",
    "default_capture_factory",
    "default_executor_factory",
    "default_name_resolver",
    "default_preview_stream_factory",
    "describe_mosaic",
    "format_elapsed",
    "format_session_time",
    "format_target",
    "load_first_sub_frame",
    "normalize_mosaic",
    "parse_entry_coords",
    "resolve_names",
    "session_log_path",
]
