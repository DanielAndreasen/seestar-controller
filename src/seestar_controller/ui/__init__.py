"""UI shell — the GTK4/libadwaita desktop app (``.scratch/ui-shell/spec.md``).

Strict two-layer split (the sandbox has no GTK, see
docs/wiki/assumptions.md):

- **Pure layer** — :mod:`.state`: app state + navigation controller
  (:class:`~.state.AppState`, the five :class:`~.state.View`\\ s, window
  geometry, dark-theme flag, the settings dict, JSON persistence under the
  app config dir), :mod:`.settings`: the settings-dialog controller
  (:class:`~.settings.SettingsController`, :func:`~.settings.validate_settings`
  / :func:`~.settings.flag_settings`, :class:`~.settings.SettingsFieldError`),
  :mod:`.connection`: the connection state machine
  (:class:`~.connection.ConnectionController`, :class:`~.connection.ConnectionStatus`,
  :class:`~.connection.ConnectionSnapshot`, :class:`~.connection.ConnectionStateError`,
  :func:`~.connection.describe_scope_state`), :mod:`.notifications`:
  the notification bus + controller (:class:`~.notifications.NotificationBus`,
  :class:`~.notifications.Notification`, :class:`~.notifications.NotificationSeverity`,
  :class:`~.notifications.NotificationController`, ticket 04), and
  :mod:`.project_controller`: the project controller
  (:class:`~.project_controller.ProjectController`,
  :class:`~.project_controller.ProjectEvent`,
  :func:`~.project_controller.ProjectListener`, projects-view ticket 01 —
  wraps the store's create/open/list against the settings project root,
  holds the current project, persists ``last_project``, restores it on
  startup), and :mod:`.mode`: the app/window mode flag
  (:class:`~.mode.AppMode`, :class:`~.mode.ModeController`,
  :func:`~.mode.ModeListener`, :data:`~.mode.SCOPE_ACTIONS`,
  offline-analysis ticket 01 — ``offline`` vs ``connected``, the
  offline-open path that never touches the link layer, and the pure
  scope-action gating helpers), and :mod:`.recent`: the recent-projects
  list (:class:`~.recent.RecentProjects`, :class:`~.recent.RecentEntry`,
  :func:`~.recent.load_recent` / :func:`~.recent.save_recent` /
  :func:`~.recent.record_recent`, offline-analysis ticket 03 — persisted in
  the app config dir, atomic writes, capped most-recent-first dedup by
  path). All import **nothing** from ``gi`` — stdlib
  only (the project controller imports only the pure
  :mod:`seestar_controller.store` and :mod:`.state`), fully unit-testable
  headless; a subprocess test enforces the boundary.
- **Widget layer** — :mod:`.views` (the five placeholder view widgets),
  :mod:`.settings_dialog` (the settings dialog), :mod:`.connection_panel`
  (the header-bar connection panel, ticket 03), :mod:`.toast` +
  :mod:`.notification_log` (the toast presenter + notification log dialog,
  ticket 04) and :mod:`.app`
  (:class:`~.app.SeestarApplication` / :class:`~.app.SeestarWindow`,
  header bar + sidebar navigation, dark theme, geometry save/restore).
  These import ``gi`` at module top and are re-exported here **lazily** (PEP
  562 ``__getattr__``), so ``import seestar_controller.ui`` never imports
  ``gi``.

On the desktop, PyGObject is not a pip dependency — install the system
packages ``python3-gi gir1.2-gtk-4.0 gir1.2-adw-1`` and launch with
``seestar-controller gui`` or ``uv run python -m seestar_controller.ui.app``
(see the README).
"""

from .connection import (
    CONNECTION_LOST_EVENT,
    LIFECYCLE_EVENTS,
    RECONNECTED_EVENT,
    RECONNECTING_EVENT,
    ConnectionController,
    ConnectionListener,
    ConnectionSnapshot,
    ConnectionStateError,
    ConnectionStatus,
    DeviceFactory,
    InvokeOnMain,
    default_device_factory,
    describe_scope_state,
)
from .mode import (
    OFFLINE_BANNER_TITLE,
    OFFLINE_PREVIEW_LABEL,
    OFFLINE_SCOPE_TOOLTIP,
    SCOPE_ACTIONS,
    AppMode,
    ModeController,
    ModeListener,
    UnknownScopeActionError,
)
from .notifications import (
    CONNECTION_SOURCE,
    DEFAULT_LOG_CAPACITY,
    DEVICE_EVENTS,
    SCOPE_ERROR_EVENT,
    STACK_EVENT,
    STACK_FINISHED_EVENT,
    EventSubscribe,
    Notification,
    NotificationBus,
    NotificationController,
    NotificationListener,
    NotificationSeverity,
    default_event_subscribe,
)
from .project_controller import (
    ProjectController,
    ProjectEvent,
    ProjectListener,
)
from .recent import (
    DEFAULT_RECENT_CAP,
    RECENT_FILE_NAME,
    RECENT_PATH_ENV_VAR,
    RECENT_SCHEMA_VERSION,
    RecentEntry,
    RecentListener,
    RecentProjects,
    RecentProjectsError,
    default_recent_path,
    load_recent,
    record_recent,
    resolve_recent_path,
    save_recent,
)
from .settings import (
    SettingsController,
    SettingsFieldError,
    SettingsListener,
    flag_settings,
    validate_settings,
)
from .state import (
    DEFAULT_COMP_SEARCH_RADIUS_DEG,
    DEFAULT_CONFIG_DIR,
    DEFAULT_DARK_THEME,
    DEFAULT_KEY_PATH,
    DEFAULT_PROJECT_ROOT,
    DEFAULT_SETTINGS,
    DEFAULT_VIEW_ID,
    DEFAULT_WINDOW_HEIGHT,
    DEFAULT_WINDOW_WIDTH,
    EDITABLE_SETTINGS_KEYS,
    MAX_COMP_SEARCH_RADIUS_DEG,
    MAX_WINDOW_COORD,
    MAX_WINDOW_SIZE,
    MIN_WINDOW_SIZE,
    SETTINGS_FILE_NAME,
    SETTINGS_KEYS,
    SETTINGS_PATH_ENV_VAR,
    SETTINGS_SCHEMA_VERSION,
    VIEW_IDS,
    VIEWS,
    AppState,
    SettingsValidationError,
    UIStateError,
    UnknownViewError,
    View,
    default_settings_path,
    load_state,
    resolve_settings_path,
    validate_comp_search_radius,
    validate_last_project,
    validate_path_setting,
    validate_scope_ip,
    view_by_id,
)

#: Widget-layer names re-exported lazily from :mod:`.app` / :mod:`.views` /
#: :mod:`.settings_dialog` / :mod:`.connection_panel` / :mod:`.toast` /
#: :mod:`.notification_log` (importing them pulls in ``gi`` — only the
#: ``gui`` entry point does).
_WIDGET_EXPORTS = {
    "SeestarApplication": "app",
    "SeestarWindow": "app",
    "apply_dark_theme": "app",
    "run": "app",
    "main": "app",
    "SettingsDialog": "settings_dialog",
    "ConnectionPanel": "connection_panel",
    "NotificationToasts": "toast",
    "NotificationLogDialog": "notification_log",
    "ObserveView": "views",
    "EnhanceView": "views",
    "PhotometryView": "views",
    "LightcurveView": "views",
    "ProjectsView": "views",
    "VIEW_CLASSES": "views",
    "build_view": "views",
}


def __getattr__(name: str):
    if name in _WIDGET_EXPORTS:
        from importlib import import_module

        module = import_module(f".{_WIDGET_EXPORTS[name]}", __name__)
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "CONNECTION_LOST_EVENT",
    "CONNECTION_SOURCE",
    "DEFAULT_COMP_SEARCH_RADIUS_DEG",
    "DEFAULT_CONFIG_DIR",
    "DEFAULT_DARK_THEME",
    "DEFAULT_KEY_PATH",
    "DEFAULT_LOG_CAPACITY",
    "DEFAULT_PROJECT_ROOT",
    "DEFAULT_RECENT_CAP",
    "DEFAULT_SETTINGS",
    "DEFAULT_VIEW_ID",
    "DEFAULT_WINDOW_HEIGHT",
    "DEFAULT_WINDOW_WIDTH",
    "DEVICE_EVENTS",
    "EDITABLE_SETTINGS_KEYS",
    "LIFECYCLE_EVENTS",
    "MAX_COMP_SEARCH_RADIUS_DEG",
    "MAX_WINDOW_COORD",
    "MAX_WINDOW_SIZE",
    "MIN_WINDOW_SIZE",
    "OFFLINE_BANNER_TITLE",
    "OFFLINE_PREVIEW_LABEL",
    "OFFLINE_SCOPE_TOOLTIP",
    "RECENT_FILE_NAME",
    "RECENT_PATH_ENV_VAR",
    "RECENT_SCHEMA_VERSION",
    "RECONNECTED_EVENT",
    "RECONNECTING_EVENT",
    "SCOPE_ACTIONS",
    "SCOPE_ERROR_EVENT",
    "SETTINGS_FILE_NAME",
    "SETTINGS_KEYS",
    "SETTINGS_PATH_ENV_VAR",
    "SETTINGS_SCHEMA_VERSION",
    "STACK_EVENT",
    "STACK_FINISHED_EVENT",
    "VIEWS",
    "VIEW_CLASSES",
    "VIEW_IDS",
    "AppMode",
    "AppState",
    "ConnectionController",
    "ConnectionListener",
    "ConnectionPanel",
    "ConnectionSnapshot",
    "ConnectionStateError",
    "ConnectionStatus",
    "DeviceFactory",
    "EnhanceView",
    "EventSubscribe",
    "InvokeOnMain",
    "LightcurveView",
    "ModeController",
    "ModeListener",
    "Notification",
    "NotificationBus",
    "NotificationController",
    "NotificationListener",
    "NotificationLogDialog",
    "NotificationSeverity",
    "NotificationToasts",
    "ObserveView",
    "PhotometryView",
    "ProjectController",
    "ProjectEvent",
    "ProjectListener",
    "ProjectsView",
    "RecentEntry",
    "RecentListener",
    "RecentProjects",
    "RecentProjectsError",
    # widget layer (lazy, imports gi):
    "SeestarApplication",
    "SeestarWindow",
    "SettingsController",
    "SettingsDialog",
    "SettingsFieldError",
    "SettingsListener",
    "SettingsValidationError",
    "UIStateError",
    "UnknownScopeActionError",
    "UnknownViewError",
    "View",
    "apply_dark_theme",
    "build_view",
    "default_device_factory",
    "default_event_subscribe",
    "default_recent_path",
    "default_settings_path",
    "describe_scope_state",
    "flag_settings",
    "load_recent",
    "load_state",
    "main",
    "record_recent",
    "resolve_recent_path",
    "resolve_settings_path",
    "run",
    "save_recent",
    "validate_comp_search_radius",
    "validate_last_project",
    "validate_path_setting",
    "validate_scope_ip",
    "validate_settings",
    "view_by_id",
]
