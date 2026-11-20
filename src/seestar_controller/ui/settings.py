"""Pure-Python settings-dialog controller for the ui shell (ticket 02).

This is the second PURE module of :mod:`seestar_controller.ui` (the two-layer
split required by ``.scratch/ui-shell/spec.md``): **no ``gi`` import anywhere
in this module** — stdlib only — so it is fully unit-testable headless (the
sandbox has no GTK, see docs/wiki/assumptions.md; a subprocess test enforces
the boundary). The widget layer (:mod:`.settings_dialog`) imports ``gi`` at
module top and is never imported by this module or eagerly by the package
``__init__``.

What lives here (the settings *model* itself — defaults, per-key format
validation, JSON persistence — stays in :mod:`.state`; this module adds the
dialog-level semantics on top of an :class:`~.state.AppState`):

- :func:`validate_settings` — per-field HARD validation of a settings mapping,
  returning ``dict[field -> error message]`` (empty dict = valid = saveable):

  - ``scope_ip`` — empty (unset) or a valid IPv4 address / hostname (reuses
    :func:`.state.validate_scope_ip`);
  - ``key_path`` — empty (unset) or a *plausible* path string (non-empty, no
    NUL bytes). Existence is NOT required at validation time — the user may
    not have extracted the PEM key yet (docs/wiki/assumptions.md) — a missing
    file is only *flagged* by :func:`flag_settings` and does NOT block saving;
  - ``project_root`` — empty (unset) or a plausible path that IS an existing
    directory (the app writes projects under it, so a typo must not save);
  - unknown keys are errors too.

- :func:`flag_settings` — per-field SOFT flags (warnings) for the dialog:
  currently ``key_path`` set but the file does not exist. Flags are shown in
  the dialog but never block saving. Empty dict = nothing to flag.

- :class:`SettingsFieldError` — a :class:`.state.SettingsValidationError`
  carrying ``field`` + ``message`` (plus the full ``errors`` dict).

- :class:`SettingsController` — observer-able controller over an
  :class:`~.state.AppState`: :meth:`~SettingsController.update` validates the
  candidate values (hard errors -> :class:`SettingsFieldError`, nothing
  applied/persisted), applies them via ``AppState.set_setting``, persists
  atomically via ``AppState.save``, then notifies the registered listeners
  (observer callback list) with the new values so the UI refreshes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

from .state import (
    SETTINGS_KEYS,
    AppState,
    SettingsValidationError,
    validate_scope_ip,
)

#: A settings-change listener: called with a copy of the new settings dict
#: after every successful :meth:`SettingsController.update`.
SettingsListener = Callable[[dict[str, str]], None]


class SettingsFieldError(SettingsValidationError):
    """A settings validation failure tied to one field.

    - ``field`` — the settings key (``scope_ip`` / ``key_path`` /
      ``project_root``, or the unknown key);
    - ``message`` — the human-readable problem (the per-field error string);
    - ``errors`` — the full ``dict[field -> message]`` from
      :func:`validate_settings`.

    ``str(err)`` is ``"<field>: <message>"``. Subclasses
    :class:`~.state.SettingsValidationError` (hence :class:`ValueError`).
    """

    def __init__(
        self,
        field: str,
        message: str,
        errors: Mapping[str, str] | None = None,
    ) -> None:
        self.field = field
        self.message = message
        self.errors: dict[str, str] = dict(errors) if errors else {field: message}
        super().__init__(f"{field}: {message}")


def _is_plausible_path(value: str) -> bool:
    """A plausible path string: non-empty after stripping and no NUL bytes.

    Existence is deliberately NOT checked here (see :func:`validate_settings`).
    """
    return bool(value.strip()) and "\0" not in value


def validate_settings(values: Mapping[str, Any]) -> dict[str, str]:
    """Validate a settings mapping; returns per-field error messages.

    The result maps field -> human-readable error; an EMPTY dict means the
    values are valid (saveable). Keys absent from ``values`` are not checked
    (the controller keeps the current value for them).

    Rules (documented choices, ticket 02):

    - ``scope_ip`` — empty (unset) or a valid IPv4 address / hostname
      (reuses :func:`.state.validate_scope_ip`);
    - ``key_path`` — empty (unset) or a plausible path string; existence is
      NOT required at validation time (the PEM key may not exist yet — a
      missing file is only flagged by :func:`flag_settings`);
    - ``project_root`` — empty (unset) or a plausible path that IS an
      existing directory;
    - any other key — an "unknown setting" error.
    """
    errors: dict[str, str] = {}
    for key in SETTINGS_KEYS:
        if key not in values:
            continue  # absent -> the controller keeps the current value
        value = values[key]
        if key == "scope_ip":
            try:
                validate_scope_ip(value)
            except SettingsValidationError as exc:
                errors[key] = str(exc)
        else:  # key_path / project_root
            if not isinstance(value, str) or isinstance(value, bool):
                errors[key] = f"{key} must be a string, got {value!r}"
            elif not value.strip():
                pass  # empty = unset, allowed
            elif not _is_plausible_path(value):
                errors[key] = f"{key} {value!r} is not a plausible path"
            elif key == "project_root" and not Path(value).is_dir():
                errors[key] = f"project_root {value!r} is not an existing directory"
    for key in values:
        if key not in SETTINGS_KEYS:
            errors[str(key)] = (
                f"unknown setting {key!r} — valid settings are {', '.join(SETTINGS_KEYS)}"
            )
    return errors


def flag_settings(values: Mapping[str, Any]) -> dict[str, str]:
    """Soft per-field flags (warnings) for the dialog — never save-blocking.

    Currently: ``key_path`` set but the file does not exist (the PEM key may
    not have been extracted yet — flagged with an actionable hint, but saving
    is allowed). Empty dict = nothing to flag.
    """
    flags: dict[str, str] = {}
    key_path = values.get("key_path")
    if isinstance(key_path, str) and key_path.strip() and not Path(key_path).is_file():
        flags["key_path"] = (
            f"key file not found: {key_path} "
            "(extract it with `seestar-controller extract-key <apk>`)"
        )
    return flags


class SettingsController:
    """Observer-able controller over an :class:`~.state.AppState`'s settings.

    - :attr:`values` — a copy of the current settings dict;
    - :meth:`validate` / :meth:`flags` — per-field errors / flags for the
      current values (or a given mapping);
    - :meth:`update` — validate -> apply -> persist -> notify (see below);
    - :meth:`add_listener` / :meth:`remove_listener` — the observer callback
      list; every successful :meth:`update` calls each listener (in
      registration order) with a copy of the new settings dict, so the UI
      refreshes.

    :meth:`update` semantics (documented choice): keyword arguments override
    the current value (``None`` = keep current; pass ``""`` to unset a path);
    the FULL candidate mapping is validated first — on any hard error a
    :class:`SettingsFieldError` is raised (carrying ``field`` + ``message`` +
    the full ``errors`` dict) and NOTHING is applied or persisted; on success
    each value goes through ``AppState.set_setting`` (format validation),
    the state is persisted atomically via ``AppState.save``, and the
    listeners are notified.
    """

    def __init__(self, state: AppState) -> None:
        self._state = state
        self._listeners: list[SettingsListener] = []

    @property
    def state(self) -> AppState:
        """The wrapped pure-layer app state."""
        return self._state

    @property
    def values(self) -> dict[str, str]:
        """A copy of the current settings dict."""
        return self._state.settings

    @property
    def listeners(self) -> tuple[SettingsListener, ...]:
        """The registered observer callbacks, in registration order."""
        return tuple(self._listeners)

    def add_listener(self, listener: SettingsListener) -> SettingsListener:
        """Register an observer callback (idempotent); returns it, so it can
        be used as a decorator."""
        if listener not in self._listeners:
            self._listeners.append(listener)
        return listener

    def remove_listener(self, listener: SettingsListener) -> None:
        """Unregister an observer callback (no-op when not registered)."""
        if listener in self._listeners:
            self._listeners.remove(listener)

    def validate(self, values: Mapping[str, Any] | None = None) -> dict[str, str]:
        """Per-field hard errors for ``values`` (default: current values)."""
        return validate_settings(self.values if values is None else values)

    def flags(self, values: Mapping[str, Any] | None = None) -> dict[str, str]:
        """Per-field soft flags for ``values`` (default: current values)."""
        return flag_settings(self.values if values is None else values)

    def update(
        self,
        *,
        scope_ip: Any = None,
        key_path: Any = None,
        project_root: Any = None,
    ) -> dict[str, str]:
        """Validate, apply, persist, and notify; returns the new settings dict.

        Each keyword overrides the current value (``None`` = keep current,
        ``""`` = unset). Raises :class:`SettingsFieldError` on any hard
        validation error — in that case nothing is applied, nothing is
        persisted, and no listener is notified.
        """
        candidate = self._state.settings
        overrides = {
            "scope_ip": scope_ip,
            "key_path": key_path,
            "project_root": project_root,
        }
        for key, value in overrides.items():
            if value is not None:
                candidate[key] = value
        errors = validate_settings(candidate)
        if errors:
            field = next((key for key in SETTINGS_KEYS if key in errors), next(iter(errors)))
            raise SettingsFieldError(field, errors[field], errors)
        for key in SETTINGS_KEYS:
            self._state.set_setting(key, candidate[key])
        self._state.save()
        new_values = self._state.settings
        for listener in list(self._listeners):
            listener(dict(new_values))
        return new_values
