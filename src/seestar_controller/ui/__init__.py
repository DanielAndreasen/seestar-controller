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
  :func:`~.connection.describe_scope_state`), and :mod:`.notifications`:
  the notification bus + controller (:class:`~.notifications.NotificationBus`,
  :class:`~.notifications.Notification`, :class:`~.notifications.NotificationSeverity`,
  :class:`~.notifications.NotificationController`, ticket 04). All import
  **nothing** from ``gi`` — stdlib only, fully unit-testable headless; a
  subprocess test enforces the boundary.
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
from .settings import (
    SettingsController,
    SettingsFieldError,
    SettingsListener,
    flag_settings,
    validate_settings,
)
from .state import (
    DEFAULT_CONFIG_DIR,
    DEFAULT_DARK_THEME,
    DEFAULT_KEY_PATH,
    DEFAULT_PROJECT_ROOT,
    DEFAULT_SETTINGS,
    DEFAULT_VIEW_ID,
    DEFAULT_WINDOW_HEIGHT,
    DEFAULT_WINDOW_WIDTH,
    MAX_WINDOW_COORD,
    MAX_WINDOW_SIZE,
    MIN_WINDOW_SIZE,
    SETTINGS_FILE_NAME,
    SETTINGS_KEYS,
    SETTINGS_PATH_ENV_VAR,
    SETTINGS_SCHEMA_VERSION,
    UIStateError,
    UnknownViewError,
    SettingsValidationError,
    VIEWS,
    VIEW_IDS,
    AppState,
    View,
    default_settings_path,
    load_state,
    resolve_settings_path,
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
    "AppState",
    "View",
    "VIEWS",
    "VIEW_IDS",
    "DEFAULT_VIEW_ID",
    "DEFAULT_DARK_THEME",
    "DEFAULT_WINDOW_WIDTH",
    "DEFAULT_WINDOW_HEIGHT",
    "MIN_WINDOW_SIZE",
    "MAX_WINDOW_SIZE",
    "MAX_WINDOW_COORD",
    "SETTINGS_KEYS",
    "DEFAULT_SETTINGS",
    "DEFAULT_CONFIG_DIR",
    "DEFAULT_KEY_PATH",
    "DEFAULT_PROJECT_ROOT",
    "SETTINGS_FILE_NAME",
    "SETTINGS_PATH_ENV_VAR",
    "SETTINGS_SCHEMA_VERSION",
    "UIStateError",
    "UnknownViewError",
    "SettingsValidationError",
    "default_settings_path",
    "resolve_settings_path",
    "load_state",
    "view_by_id",
    "SettingsController",
    "SettingsFieldError",
    "SettingsListener",
    "validate_settings",
    "flag_settings",
    "ConnectionController",
    "ConnectionStatus",
    "ConnectionSnapshot",
    "ConnectionStateError",
    "ConnectionListener",
    "DeviceFactory",
    "InvokeOnMain",
    "default_device_factory",
    "describe_scope_state",
    "CONNECTION_LOST_EVENT",
    "RECONNECTING_EVENT",
    "RECONNECTED_EVENT",
    "LIFECYCLE_EVENTS",
    "NotificationSeverity",
    "Notification",
    "NotificationBus",
    "NotificationController",
    "NotificationListener",
    "EventSubscribe",
    "default_event_subscribe",
    "DEFAULT_LOG_CAPACITY",
    "CONNECTION_SOURCE",
    "SCOPE_ERROR_EVENT",
    "STACK_EVENT",
    "STACK_FINISHED_EVENT",
    "DEVICE_EVENTS",
    # widget layer (lazy, imports gi):
    "SeestarApplication",
    "SeestarWindow",
    "apply_dark_theme",
    "run",
    "main",
    "SettingsDialog",
    "ConnectionPanel",
    "NotificationToasts",
    "NotificationLogDialog",
    "ObserveView",
    "EnhanceView",
    "PhotometryView",
    "LightcurveView",
    "ProjectsView",
    "VIEW_CLASSES",
    "build_view",
]
