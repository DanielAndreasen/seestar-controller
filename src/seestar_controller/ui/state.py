"""Pure-Python app state + navigation controller for the ui shell.

This is the PURE layer of :mod:`seestar_controller.ui` (the two-layer split
required by ``.scratch/ui-shell/spec.md``): **no ``gi`` import anywhere in
this module** — stdlib only — so it is fully unit-testable headless (the
sandbox has no GTK, see docs/wiki/assumptions.md; a subprocess test enforces
the boundary). The widget layer (:mod:`.views`, :mod:`.app`) imports ``gi``
at module top and is never imported by this module or by the package
``__init__`` eagerly.

What lives here:

- the five views as an ordered list of :class:`View` (id, title) —
  Observe, Enhance, Photometry, Lightcurve, Projects — and navigation:
  :meth:`AppState.select_view` validates the id and raises
  :class:`UnknownViewError` on anything else;
- window geometry (width/height/x/y) with validation;
- the dark-theme flag (default :data:`DEFAULT_DARK_THEME` = ``True`` —
  night-time use must not blind the user);
- the settings dict (``scope_ip``, ``key_path``, ``project_root``) with
  defaults + validation;
- persistence: one JSON file at ``~/.config/seestar-controller/settings.json``
  (the app config dir — the same dir as the PEM key and the protocol log;
  re-declared here because this package must not import
  :mod:`seestar_controller.link`), overridable with the
  ``SEESTAR_SETTINGS_PATH`` environment variable — the same
  explicit-arg > env-var > default precedence as ``SEESTAR_KEY_PATH`` /
  ``SEESTAR_CACHE_DIR`` / ``SEESTAR_PROJECT_ROOT``.

On-disk shape (``schema_version`` :data:`SETTINGS_SCHEMA_VERSION`)::

    {
      "schema_version": 1,
      "current_view": "observe",
      "dark_theme": true,
      "window": {"width": 1280, "height": 800, "x": null, "y": null},
      "settings": {
        "scope_ip": "",                                  # unset placeholder
        "key_path": "/home/.../.config/seestar-controller/key.pem",
        "project_root": "/home/.../SeestarProjects"
      }
    }

Robustness (documented choice): :meth:`AppState.load` NEVER raises for a
missing, unreadable, corrupt, or wrongly-shaped settings file — every field
falls back to its default independently, so a corrupt file yields defaults
and no crash. Writes are atomic (temp file in the same dir + fsync +
:func:`os.replace`, the same pattern as ``catalog/cache.py`` and
``link/frames.py`` — re-declared here because this package must not import
them), so a crash never leaves a partial settings file and no ``*.tmp``
leftover survives a failed write.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

#: Config dir of the app — the same dir as the PEM key and the protocol log
#: (``seestar_controller.link.keys`` / ``link.protocol_log`` use the same
#: default; re-declared here because this package must not import link).
DEFAULT_CONFIG_DIR = Path.home() / ".config" / "seestar-controller"

#: File name of the UI settings inside the config dir.
SETTINGS_FILE_NAME = "settings.json"

#: Environment variable that overrides the default settings path (same
#: explicit-arg > env-var > default precedence as the key path / cache dir).
SETTINGS_PATH_ENV_VAR = "SEESTAR_SETTINGS_PATH"

#: Version of the on-disk settings schema; a file with any other version is
#: treated as corrupt and falls back to defaults.
SETTINGS_SCHEMA_VERSION = 1

#: Default PEM key path mirrored into the settings defaults — the same value
#: as ``seestar_controller.link.keys.DEFAULT_KEY_PATH`` (re-declared, see
#: module docstring).
DEFAULT_KEY_PATH = DEFAULT_CONFIG_DIR / "key.pem"

#: Default project root mirrored into the settings defaults — the same value
#: as ``seestar_controller.store.project.DEFAULT_PROJECT_ROOT`` (re-declared).
DEFAULT_PROJECT_ROOT = Path.home() / "SeestarProjects"

@dataclass(frozen=True)
class View:
    """One navigable view: a stable ``id`` (used as the stack page name and
    persisted in settings) and a human ``title`` (sidebar row / page title)."""

    id: str
    title: str


#: The five views, in sidebar order.
VIEWS: tuple[View, ...] = (
    View("observe", "Observe"),
    View("enhance", "Enhance"),
    View("photometry", "Photometry"),
    View("lightcurve", "Lightcurve"),
    View("projects", "Projects"),
)

#: The view ids, in sidebar order.
VIEW_IDS: tuple[str, ...] = tuple(view.id for view in VIEWS)

#: The view selected on first launch.
DEFAULT_VIEW_ID = VIEWS[0].id

#: Default window size (position starts unset: ``x``/``y`` are ``None`` until
#: the window manager has placed the window once).
DEFAULT_WINDOW_WIDTH = 1280
DEFAULT_WINDOW_HEIGHT = 800

#: Window-size validation bounds (pixels).
MIN_WINDOW_SIZE = 200
MAX_WINDOW_SIZE = 16384

#: Window-position validation bound (pixels; positions may be negative on
#: multi-monitor setups).
MAX_WINDOW_COORD = 32768

#: Dark theme is ON by default (night-time observing).
DEFAULT_DARK_THEME = True

#: The keys of the settings dict.
SETTINGS_KEYS: tuple[str, ...] = ("scope_ip", "key_path", "project_root")

#: The default settings dict (``scope_ip`` empty = unset placeholder, see
#: docs/wiki/assumptions.md).
DEFAULT_SETTINGS: dict[str, str] = {
    "scope_ip": "",
    "key_path": str(DEFAULT_KEY_PATH),
    "project_root": str(DEFAULT_PROJECT_ROOT),
}

#: A scope address: an IPv4 address or a hostname (labels of letters, digits,
#: underscores, hyphens; total length <= 253).
_HOST_RE = re.compile(
    r"^(?=.{1,253}$)"
    r"([a-zA-Z0-9_]([a-zA-Z0-9_-]{0,61}[a-zA-Z0-9_])?\.)*"
    r"[a-zA-Z0-9_]([a-zA-Z0-9_-]{0,61}[a-zA-Z0-9_])?$"
)

#: A dotted-quad IPv4 candidate (octets validated separately).
_IPV4_RE = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")


class UIStateError(Exception):
    """Base class for ui-shell state failures."""


class UnknownViewError(UIStateError, ValueError):
    """The given view id is not one of :data:`VIEW_IDS`.

    Subclasses both :class:`UIStateError` and :class:`ValueError` — an
    unknown view is a value error, so callers may catch either.
    """


class SettingsValidationError(UIStateError, ValueError):
    """An invalid settings value: unknown settings key, bad scope address,
    empty path setting, or out-of-range window geometry.

    Subclasses both :class:`UIStateError` and :class:`ValueError`.
    """


def view_by_id(view_id: Any) -> View:
    """Look up a :class:`View` by id; raises :class:`UnknownViewError`."""
    for view in VIEWS:
        if view.id == view_id:
            return view
    raise UnknownViewError(
        f"unknown view {view_id!r} — valid views are {', '.join(VIEW_IDS)}"
    )


def default_settings_path(env: Mapping[str, str] | None = None) -> Path:
    """Resolve the default settings path.

    Precedence: the ``SEESTAR_SETTINGS_PATH`` environment variable > the
    default ``~/.config/seestar-controller/settings.json``.
    """
    environ = os.environ if env is None else env
    value = environ.get(SETTINGS_PATH_ENV_VAR)
    if value:
        return Path(value)
    return DEFAULT_CONFIG_DIR / SETTINGS_FILE_NAME


def resolve_settings_path(
    path: str | Path | None = None, *, env: Mapping[str, str] | None = None
) -> Path:
    """Resolve the settings file path.

    Precedence: explicit ``path`` > ``SEESTAR_SETTINGS_PATH`` env var > the
    default ``~/.config/seestar-controller/settings.json``.
    """
    if path is not None:
        return Path(path)
    return default_settings_path(env)


def _validate_int(
    value: Any, name: str, *, minimum: int | None = None, maximum: int | None = None
) -> int:
    """Validate an int setting (bools rejected); raise SettingsValidationError."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise SettingsValidationError(f"{name} must be an int, got {value!r}")
    if minimum is not None and value < minimum:
        raise SettingsValidationError(f"{name} must be >= {minimum}, got {value!r}")
    if maximum is not None and value > maximum:
        raise SettingsValidationError(f"{name} must be <= {maximum}, got {value!r}")
    return value


def _validate_geometry_field(value: Any, name: str) -> Any:
    """Coerce one loaded window field: int in range, or None for x/y."""
    if value is None:
        return None
    if name in ("width", "height"):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not MIN_WINDOW_SIZE <= value <= MAX_WINDOW_SIZE
        ):
            return None  # invalid -> caller falls back to the default
        return value
    # x / y
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not -MAX_WINDOW_COORD <= value <= MAX_WINDOW_COORD
    ):
        return None
    return value


def _validate_scope_ip(value: Any) -> str:
    """Validate a scope address: empty (unset), an IPv4 address (octets
    0-255), or a hostname string."""
    if not isinstance(value, str):
        raise SettingsValidationError(f"scope_ip must be a string, got {value!r}")
    address = value.strip()
    if not address:
        return ""
    ipv4 = _IPV4_RE.match(address)
    if ipv4:
        if all(0 <= int(part) <= 255 for part in ipv4.groups()):
            return address
        raise SettingsValidationError(
            f"scope_ip {value!r} is not a valid IPv4 address (octets must be 0-255)"
        )
    if not _HOST_RE.match(address):
        raise SettingsValidationError(
            f"scope_ip {value!r} is not a valid IPv4 address or hostname "
            "(e.g. '192.168.1.50' or 'seestar.local')"
        )
    return address


def _validate_path_setting(value: Any, name: str) -> str:
    """Validate a path setting: a non-empty string."""
    if not isinstance(value, str) or isinstance(value, bool):
        raise SettingsValidationError(f"{name} must be a string, got {value!r}")
    if not value.strip():
        raise SettingsValidationError(
            f"{name} must not be empty — point it at a path (the default is "
            f"{DEFAULT_SETTINGS[name]!r})"
        )
    return value


def _atomic_write_json(path: Path, obj: Any) -> Path:
    """Write ``obj`` as pretty JSON to ``path`` atomically.

    Same pattern as ``catalog/cache.py``'s writer: temp file in the same
    directory, flush + fsync, then :func:`os.replace` onto the destination.
    On any failure (including :class:`KeyboardInterrupt`) the temp file is
    removed and the destination is left untouched — a reader never sees a
    partial file and no ``*.tmp`` leftover survives. Returns ``path``.
    """
    data = (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.close(fd)  # already closed when the with-block exited
        except OSError:
            pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


class AppState:
    """The pure-Python application state: current view, window geometry,
    dark-theme flag, and the settings dict, with JSON persistence.

    - ``settings_path`` — the settings file (default:
      :func:`resolve_settings_path`, i.e. ``SEESTAR_SETTINGS_PATH`` >
      ``~/.config/seestar-controller/settings.json``);
    - ``env`` — environment mapping used for path resolution when
      ``settings_path`` is None (default :data:`os.environ`; tests inject a
      mapping).

    A fresh instance starts at the defaults; :meth:`load` reads the file
    (missing/corrupt -> defaults, never raises) and :meth:`save` writes it
    atomically.
    """

    def __init__(
        self,
        settings_path: str | Path | None = None,
        *,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.settings_path = resolve_settings_path(settings_path, env=env)
        self._view_id: str = DEFAULT_VIEW_ID
        self._dark_theme: bool = DEFAULT_DARK_THEME
        self._window: dict[str, Any] = {
            "width": DEFAULT_WINDOW_WIDTH,
            "height": DEFAULT_WINDOW_HEIGHT,
            "x": None,
            "y": None,
        }
        self._settings: dict[str, str] = dict(DEFAULT_SETTINGS)

    # -- navigation -----------------------------------------------------------

    @property
    def views(self) -> tuple[View, ...]:
        """The five views, in sidebar order."""
        return VIEWS

    @property
    def current_view_id(self) -> str:
        """The id of the currently selected view."""
        return self._view_id

    @property
    def current_view(self) -> View:
        """The currently selected :class:`View`."""
        return view_by_id(self._view_id)

    def select_view(self, view_id: Any) -> View:
        """Select a view by id; returns the selected :class:`View`.

        Raises :class:`UnknownViewError` (a :class:`ValueError`) for any id
        not in :data:`VIEW_IDS`; the current view is left unchanged then.
        """
        view = view_by_id(view_id)
        self._view_id = view.id
        return view

    # -- dark theme -----------------------------------------------------------

    @property
    def dark_theme(self) -> bool:
        """Whether dark theme is requested (default True)."""
        return self._dark_theme

    def set_dark_theme(self, dark: Any) -> bool:
        """Set the dark-theme flag (must be a bool); returns it."""
        if not isinstance(dark, bool):
            raise SettingsValidationError(f"dark_theme must be a bool, got {dark!r}")
        self._dark_theme = dark
        return self._dark_theme

    # -- window geometry ------------------------------------------------------

    @property
    def window_width(self) -> int:
        return self._window["width"]

    @property
    def window_height(self) -> int:
        return self._window["height"]

    @property
    def window_x(self) -> int | None:
        return self._window["x"]

    @property
    def window_y(self) -> int | None:
        return self._window["y"]

    def window_geometry(self) -> dict[str, Any]:
        """The window geometry as a dict ``{"width", "height", "x", "y"}``
        (``x``/``y`` are ``None`` until the window has been positioned)."""
        return dict(self._window)

    def set_window_geometry(
        self,
        width: Any,
        height: Any,
        x: Any = None,
        y: Any = None,
    ) -> dict[str, Any]:
        """Set the window geometry; returns :meth:`window_geometry`.

        ``width``/``height`` must be ints in
        [MIN_WINDOW_SIZE, MAX_WINDOW_SIZE]; ``x``/``y`` must be ``None``
        (unset) or ints in [-MAX_WINDOW_COORD, MAX_WINDOW_COORD] (positions
        may be negative on multi-monitor setups). Raises
        :class:`SettingsValidationError` otherwise.
        """
        width = _validate_int(
            width, "window width", minimum=MIN_WINDOW_SIZE, maximum=MAX_WINDOW_SIZE
        )
        height = _validate_int(
            height, "window height", minimum=MIN_WINDOW_SIZE, maximum=MAX_WINDOW_SIZE
        )
        if x is not None:
            x = _validate_int(
                x, "window x", minimum=-MAX_WINDOW_COORD, maximum=MAX_WINDOW_COORD
            )
        if y is not None:
            y = _validate_int(
                y, "window y", minimum=-MAX_WINDOW_COORD, maximum=MAX_WINDOW_COORD
            )
        self._window = {"width": width, "height": height, "x": x, "y": y}
        return self.window_geometry()

    # -- settings dict --------------------------------------------------------

    @property
    def settings(self) -> dict[str, str]:
        """A copy of the settings dict (``scope_ip``, ``key_path``,
        ``project_root``)."""
        return dict(self._settings)

    def get_setting(self, key: str) -> str:
        """The value of one settings ``key``; raises
        :class:`SettingsValidationError` for an unknown key."""
        if key not in SETTINGS_KEYS:
            raise SettingsValidationError(
                f"unknown setting {key!r} — valid settings are {', '.join(SETTINGS_KEYS)}"
            )
        return self._settings[key]

    def set_setting(self, key: str, value: Any) -> str:
        """Set one settings ``key`` to ``value`` (validated); returns it.

        - ``scope_ip`` — empty string (unset) or an IPv4 address / hostname;
        - ``key_path`` / ``project_root`` — a non-empty path string.

        Raises :class:`SettingsValidationError` for an unknown key or an
        invalid value.
        """
        if key not in SETTINGS_KEYS:
            raise SettingsValidationError(
                f"unknown setting {key!r} — valid settings are {', '.join(SETTINGS_KEYS)}"
            )
        if key == "scope_ip":
            value = _validate_scope_ip(value)
        else:
            value = _validate_path_setting(value, key)
        self._settings[key] = value
        return value

    # -- persistence ----------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """The serializable state: the exact on-disk shape (see module
        docstring)."""
        return {
            "schema_version": SETTINGS_SCHEMA_VERSION,
            "current_view": self._view_id,
            "dark_theme": self._dark_theme,
            "window": dict(self._window),
            "settings": dict(self._settings),
        }

    def load(self, path: str | Path | None = None) -> AppState:
        """Load state from the settings file into this instance; returns self.

        ``path`` overrides (and updates) :attr:`settings_path`. A missing,
        unreadable, corrupt, or wrongly-shaped file NEVER raises: every field
        falls back to its default independently (a file whose
        ``schema_version`` is not :data:`SETTINGS_SCHEMA_VERSION` is treated
        as corrupt wholesale).
        """
        if path is not None:
            self.settings_path = Path(path)
        try:
            raw = self.settings_path.read_bytes()
        except OSError:
            return self  # missing/unreadable -> keep the defaults
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self  # corrupt -> defaults
        if not isinstance(data, dict):
            return self
        version = data.get("schema_version", SETTINGS_SCHEMA_VERSION)
        if version != SETTINGS_SCHEMA_VERSION:
            return self  # unknown schema version -> defaults
        view_id = data.get("current_view")
        if view_id in VIEW_IDS:
            self._view_id = view_id
        dark = data.get("dark_theme")
        if isinstance(dark, bool):
            self._dark_theme = dark
        window = data.get("window")
        if isinstance(window, dict):
            width = _validate_geometry_field(
                window.get("width", DEFAULT_WINDOW_WIDTH), "width"
            )
            height = _validate_geometry_field(
                window.get("height", DEFAULT_WINDOW_HEIGHT), "height"
            )
            x = _validate_geometry_field(window.get("x"), "x")
            y = _validate_geometry_field(window.get("y"), "y")
            self._window = {
                "width": DEFAULT_WINDOW_WIDTH if width is None else width,
                "height": DEFAULT_WINDOW_HEIGHT if height is None else height,
                "x": x,
                "y": y,
            }
        settings = data.get("settings")
        if isinstance(settings, dict):
            for key in SETTINGS_KEYS:
                value = settings.get(key)
                if value is None:
                    continue
                try:
                    self.set_setting(key, value)
                except SettingsValidationError:
                    pass  # invalid value -> keep the default for this key
        return self

    def save(self, path: str | Path | None = None) -> Path:
        """Atomically write the state to the settings file; returns the path.

        ``path`` overrides (and updates) :attr:`settings_path`. The write is
        temp-file + fsync + :func:`os.replace` (see :func:`_atomic_write_json`),
        so no partial file is ever visible and a failed write leaves no
        ``*.tmp`` leftover.
        """
        if path is not None:
            self.settings_path = Path(path)
        return _atomic_write_json(self.settings_path, self.to_dict())


def load_state(
    path: str | Path | None = None, *, env: Mapping[str, str] | None = None
) -> AppState:
    """Construct an :class:`AppState` and :meth:`~AppState.load` it from disk.

    Convenience for the app entry point: ``load_state()`` gives the state
    with the resolved settings path already applied (missing/corrupt file ->
    defaults, never raises).
    """
    return AppState(path, env=env).load()
