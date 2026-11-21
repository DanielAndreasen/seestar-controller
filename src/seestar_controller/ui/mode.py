"""Pure-Python app/window mode model — offline-analysis ticket 01.

This is another PURE module of :mod:`seestar_controller.ui` (the two-layer
split required by ``.scratch/ui-shell/spec.md``): **no ``gi`` import anywhere
in this module** — it imports only the pure :mod:`.connection`,
:mod:`.project_controller`, :mod:`.state` and :mod:`seestar_controller.store`,
so it is fully unit-testable headless (see docs/wiki/assumptions.md; a
subprocess test enforces the boundary). The widget layer (:mod:`.app`) imports
``gi`` at module top and is never imported by this module.

What lives here (offline-analysis issue 01 — offline mode core):

- :class:`AppMode` — the app/window mode flag: ``connected`` (the default —
  today's behavior) or ``offline`` (a project opened from the store WITHOUT
  starting the link layer or any session);
- :data:`SCOPE_ACTIONS` — the scope-dependent action ids gated in offline
  mode (``connect``, ``goto_slew``, ``live_preview``, ``start_night``,
  ``pause_resume_stop``), plus the banner title / tooltip texts;
- :class:`ModeController` — the observer-able mode flag: :meth:`set_mode`
  notifies the listeners (the window re-renders the banner + gating from
  them), :meth:`scope_action_enabled` / :meth:`scope_action_tooltip` are the
  pure gating helpers the widget layer applies, and:

  - :meth:`open_project_offline` — the offline-open path: opens the store
    project through the :class:`~.project_controller.ProjectController`
    (pure store access — the link layer is NEVER touched, no device factory
    is called, no session is started) and switches the mode to ``offline``;
  - :meth:`attach_connection` — a successful connect (status ``connected``)
    switches the mode back to ``connected``;
  - :meth:`attach_project_controller` — opening/creating a project through
    the NORMAL path (anything but :meth:`open_project_offline`) switches the
    mode back to ``connected``; a mere list refresh (same current project)
    does not change the mode.

Documented semantics: the mode starts ``connected``; only the explicit
offline-open action sets ``offline``; a successful connect or a normal
project open/create returns to ``connected``. Store errors from
:meth:`open_project_offline` propagate (the mode is left unchanged).
"""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum
from pathlib import Path

from seestar_controller.store import Project

from .connection import ConnectionController, ConnectionStatus
from .project_controller import ProjectController
from .state import UIStateError


class AppMode(str, Enum):
    """The app/window mode flag (offline-analysis ticket 01)."""

    CONNECTED = "connected"
    OFFLINE = "offline"

    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.value


class UnknownScopeActionError(UIStateError, ValueError):
    """An unknown scope-action id was passed to the gating helpers.

    Subclasses both :class:`~.state.UIStateError` and :class:`ValueError`.
    """


#: The scope-dependent action ids gated in offline mode (disabled with a
#: tooltip): connecting to the scope, goto/slew, the live preview, starting
#: a night, and pause/resume/stop of the running night.
SCOPE_ACTIONS: tuple[str, ...] = (
    "connect",
    "goto_slew",
    "live_preview",
    "start_night",
    "pause_resume_stop",
)

#: The offline banner title shown while the mode is ``offline`` — it states
#: plainly that no telescope is connected (offline-analysis ticket 03).
OFFLINE_BANNER_TITLE = (
    "Offline mode — no telescope connected: the project is opened from local "
    "storage only; scope actions (connect, slew, live preview, night controls) "
    "are disabled"
)

#: The tooltip set on every gated scope action while in offline mode.
OFFLINE_SCOPE_TOOLTIP = (
    "Unavailable in offline mode — open a project normally or connect "
    "to the scope to enable scope actions"
)

#: The preview-area label shown while in offline mode (no live preview).
OFFLINE_PREVIEW_LABEL = "live preview unavailable in offline mode"

#: A mode-change listener: called with the new :class:`AppMode` after every
#: change (in registration order).
ModeListener = Callable[[AppMode], None]


class ModeController:
    """Observer-able app/window mode flag (offline-analysis ticket 01).

    - :attr:`mode` / :attr:`is_offline` — the current :class:`AppMode`;
    - :meth:`set_mode` — change the mode and notify the listeners (a no-op
      when the mode is unchanged — no notification);
    - :meth:`scope_action_enabled` / :meth:`scope_action_tooltip` — the pure
      gating helpers: in offline mode every :data:`SCOPE_ACTIONS` id is
      disabled and carries :data:`OFFLINE_SCOPE_TOOLTIP`;
    - :meth:`open_project_offline` — open a store project without touching
      the link layer and switch to ``offline``;
    - :meth:`attach_connection` / :meth:`attach_project_controller` — the
      bindings that switch back to ``connected`` (see the module docstring).
    """

    def __init__(self, mode: AppMode | str = AppMode.CONNECTED) -> None:
        self._mode = AppMode(mode)
        self._listeners: list[ModeListener] = []
        #: guard while :meth:`open_project_offline` runs: the project event
        #: fired inside must not switch the mode back to ``connected``
        self._offline_open_guard = False

    # -- reading ---------------------------------------------------------------

    @property
    def mode(self) -> AppMode:
        """The current :class:`AppMode`."""
        return self._mode

    @property
    def is_offline(self) -> bool:
        """Whether the app is currently in offline mode."""
        return self._mode == AppMode.OFFLINE

    @property
    def listeners(self) -> tuple[ModeListener, ...]:
        """The registered observer callbacks, in registration order."""
        return tuple(self._listeners)

    # -- listeners ---------------------------------------------------------------

    def add_listener(self, listener: ModeListener) -> ModeListener:
        """Register an observer callback (idempotent); returns it, so it can
        be used as a decorator."""
        if listener not in self._listeners:
            self._listeners.append(listener)
        return listener

    def remove_listener(self, listener: ModeListener) -> None:
        """Unregister an observer callback (no-op when not registered)."""
        if listener in self._listeners:
            self._listeners.remove(listener)

    # -- mode changes --------------------------------------------------------------

    def set_mode(self, mode: AppMode | str) -> AppMode:
        """Set the mode and notify the listeners; returns the new mode.

        A no-op (no notification) when the mode is unchanged.
        """
        mode = AppMode(mode)
        if mode == self._mode:
            return self._mode
        self._mode = mode
        for listener in list(self._listeners):
            listener(mode)
        return mode

    # -- gating helpers --------------------------------------------------------------

    def scope_action_enabled(self, action: str) -> bool:
        """Whether the scope-dependent ``action`` (a :data:`SCOPE_ACTIONS`
        id) is enabled in the current mode — ``False`` in offline mode.

        Raises :class:`UnknownScopeActionError` for an unknown id.
        """
        if action not in SCOPE_ACTIONS:
            raise UnknownScopeActionError(
                f"unknown scope action {action!r} — expected one of {SCOPE_ACTIONS}"
            )
        return not self.is_offline

    def scope_action_tooltip(self, action: str) -> str | None:
        """The tooltip for the scope-dependent ``action`` in the current
        mode: :data:`OFFLINE_SCOPE_TOOLTIP` in offline mode, ``None``
        otherwise (the widget keeps its own tooltip).

        Raises :class:`UnknownScopeActionError` for an unknown id.
        """
        if action not in SCOPE_ACTIONS:
            raise UnknownScopeActionError(
                f"unknown scope action {action!r} — expected one of {SCOPE_ACTIONS}"
            )
        return OFFLINE_SCOPE_TOOLTIP if self.is_offline else None

    # -- bindings ---------------------------------------------------------------------

    def attach_connection(self, connection_controller: ConnectionController) -> None:
        """Bind a connection controller: a successful connect (status
        ``connected``) switches the mode to ``connected``."""

        def on_snapshot(snapshot) -> None:
            if snapshot.status == ConnectionStatus.CONNECTED:
                self.set_mode(AppMode.CONNECTED)

        connection_controller.add_listener(on_snapshot)

    def attach_project_controller(self, project_controller: ProjectController) -> None:
        """Bind a project controller: opening/creating a project through the
        NORMAL path (a current-project change that is not the guarded
        offline open) switches the mode to ``connected``. A mere list
        refresh (same current project) leaves the mode unchanged."""
        current = project_controller.current
        last_path: str | None = str(current.path) if current is not None else None

        def on_event(event) -> None:
            nonlocal last_path
            path = str(event.current.path) if event.current is not None else None
            changed = path != last_path
            last_path = path
            if changed and not self._offline_open_guard:
                self.set_mode(AppMode.CONNECTED)

        project_controller.add_listener(on_event)

    # -- the offline-open path -----------------------------------------------------------

    def open_project_offline(
        self, project_controller: ProjectController, path: str | Path
    ) -> Project:
        """Open the store project at ``path`` WITHOUT the link layer and
        switch the mode to ``offline``; returns the opened project.

        This is the pure offline-open path (offline-analysis issue 01): it
        goes through the project controller's store open only — no device
        factory is called, no session is started, nothing in
        :mod:`seestar_controller.link` is touched. The project event fired
        inside is guarded, so it does not switch the mode back. Store errors
        (:class:`~seestar_controller.store.errors.ProjectError`,
        ``ValueError``, ``OSError``) propagate and leave the mode unchanged.
        """
        self._offline_open_guard = True
        try:
            project = project_controller.open_project(path)
        finally:
            self._offline_open_guard = False
        self.set_mode(AppMode.OFFLINE)
        return project
