"""Pure-Python project controller for the ui shell (projects-view ticket 01).

This is another PURE module of :mod:`seestar_controller.ui` (the two-layer
split required by ``.scratch/ui-shell/spec.md``): **no ``gi`` import anywhere
in this module** — it imports only :mod:`seestar_controller.store` (itself
pure: no GTK, no ``link``) and :mod:`.state`, so it is fully unit-testable
headless (see docs/wiki/assumptions.md; a subprocess test enforces the
boundary). The widget layer (:mod:`.views` — the :class:`~.views.ProjectsView`
widget, :mod:`.app`) imports ``gi`` at module top and is never imported by
this module or eagerly by the package ``__init__``.

What lives here:

- :class:`ProjectEvent` — the frozen listener event fired on every change
  (the project list and/or the current project);
- :class:`ProjectRootError` — an :class:`OSError` raised when the project
  root cannot be used (not a directory, or not writable); the message always
  carries the hint to set a project root in Settings (projects-view ticket
  02);
- :class:`ProjectController` — wraps the store's
  :func:`~seestar_controller.store.create_project` /
  :func:`~seestar_controller.store.open_project` /
  :func:`~seestar_controller.store.list_projects` /
  :func:`~seestar_controller.store.delete_project` against the **settings
  project root** (``project_root`` setting; empty -> the store's
  :func:`~seestar_controller.store.default_project_root`, i.e.
  ``SEESTAR_PROJECT_ROOT`` > ``~/SeestarProjects/``; an explicit ``root=``
  constructor argument overrides both, for tests/standalone use), holds the
  current :class:`~seestar_controller.store.Project`, persists the last
  opened project path in the ``last_project`` setting (validated: empty or
  an existing project directory containing ``project.json``), restores it
  on startup via :meth:`ProjectController.restore`, and notifies listeners
  with a frozen :class:`ProjectEvent` on every change. A **missing root is
  created on demand** by :meth:`ProjectController.create_project` (ticket
  02); :attr:`ProjectController.root_error` reports an existing-but-unusable
  root (not a directory / not writable) with the Settings hint;
- :meth:`ProjectController.attach_controllers` — registers the app's shared
  controllers (observe / viewer / pick / run / lightcurve / period) so
  ``set_project(current)`` is called on each whenever the current project
  changes (and once immediately, to apply a restored project).

Store errors (:class:`~seestar_controller.store.errors.ProjectExistsError`,
:class:`~seestar_controller.store.errors.ProjectNotFoundError`,
:class:`~seestar_controller.store.errors.InvalidProjectError`,
``ValueError`` for an unusable name, :class:`ProjectRootError` /
``OSError`` for an unusable root) propagate to the caller — the widget layer
shows them inline; on failure the current project and the persisted
``last_project`` are left unchanged.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from seestar_controller.store import (
    Project,
    ProjectSummary,
    create_project,
    default_project_root,
    delete_project,
    list_projects,
    open_project,
)
from seestar_controller.store.errors import ProjectError

from .state import AppState

#: A project-change listener: called with a frozen :class:`ProjectEvent`
#: after every successful change (list refresh, create, open, restore).
ProjectListener = Callable[["ProjectEvent"], None]


class ProjectRootError(OSError):
    """The project root cannot be used (projects-view ticket 02).

    Raised by :meth:`ProjectController.create_project` when the root is not a
    directory or not writable (or cannot be created). Subclasses
    :class:`OSError`, so the widget layer's existing ``except OSError``
    inline-error handling catches it; the message always carries the hint to
    set a (writable) project root in Settings.
    """


@dataclass(frozen=True)
class ProjectEvent:
    """The frozen event fired on every :class:`ProjectController` change.

    - ``root`` — the project root in use (as a string);
    - ``projects`` — the current :class:`~seestar_controller.store.ProjectSummary`
      tuple (oldest first, see
      :func:`~seestar_controller.store.list_projects`);
    - ``current`` — the current open
      :class:`~seestar_controller.store.Project`, or ``None``.
    """

    root: str
    projects: tuple[ProjectSummary, ...]
    current: Project | None


class ProjectController:
    """Observer-able controller over the store's projects + the current one.

    - :attr:`root` — the project root in use (explicit ``root=`` argument >
      the ``project_root`` setting > the store default);
    - :attr:`root_error` — ``None`` when the root is usable, else a message
      (with the Settings hint) for an existing-but-unusable root; a missing
      root is created on demand by :meth:`create_project`;
    - :attr:`projects` — the listed projects (refreshed by
      :meth:`refresh` and after every create/open/restore);
    - :attr:`current` — the open project (``None`` until one is created or
      opened);
    - :meth:`create_project` / :meth:`open_project` — create/open a project
      and make it current (persisting ``last_project``); store errors
      (including :class:`ProjectRootError` for an unusable root) propagate
      and change nothing;
    - :meth:`delete_project` — delete a project (store
      :func:`~seestar_controller.store.delete_project`; when the deleted
      project is the current one it is closed first, ``current`` becomes
      ``None`` and ``last_project`` is cleared); store errors propagate and
      change nothing;
    - :meth:`restore` — startup: open the persisted ``last_project`` when it
      still exists, else clear the stale setting;
    - :meth:`attach_controllers` — push the current project to the app's
      shared controllers on every change;
    - :meth:`add_listener` / :meth:`remove_listener` — the observer callback
      list; every change calls each listener (in registration order) with a
      frozen :class:`ProjectEvent`.

    Documented choice: switching the current project does NOT close the
    previous project's db connection — sqlite WAL allows several connections
    to the same db, and closing could break an in-flight night that still
    uses the old project.
    """

    def __init__(
        self,
        state: AppState,
        *,
        root: str | Path | None = None,
    ) -> None:
        self._state = state
        self._root_override = Path(root) if root is not None else None
        self._current: Project | None = None
        self._projects: tuple[ProjectSummary, ...] = ()
        self._listeners: list[ProjectListener] = []
        self.refresh()

    # -- reading ---------------------------------------------------------------

    @property
    def state(self) -> AppState:
        """The wrapped pure-layer app state (settings source)."""
        return self._state

    @property
    def root(self) -> Path:
        """The project root in use: the explicit ``root=`` argument when
        given, else the ``project_root`` setting when set, else the store
        default (:func:`~seestar_controller.store.default_project_root`)."""
        if self._root_override is not None:
            return self._root_override
        configured = self._state.get_setting("project_root").strip()
        if configured:
            return Path(configured)
        return default_project_root()

    @property
    def root_error(self) -> str | None:
        """A human-readable problem with :attr:`root`, or ``None`` when the
        root is usable (projects-view ticket 02).

        A **missing** root is NOT an error — it is created on demand by
        :meth:`create_project`. An existing root that is not a directory, or
        is not writable, is an error whose message points to Settings (the
        Projects view shows it in its root line).
        """
        root = self.root
        if not root.exists():
            return None  # created on demand at create time
        if not root.is_dir():
            return f"project root {root} is not a directory — set a project root in Settings"
        if not os.access(root, os.W_OK | os.X_OK):
            return f"project root {root} is not writable — set a writable project root in Settings"
        return None

    @property
    def projects(self) -> tuple[ProjectSummary, ...]:
        """The listed projects under :attr:`root` (oldest first)."""
        return self._projects

    @property
    def current(self) -> Project | None:
        """The current open project (``None`` when none is open)."""
        return self._current

    @property
    def listeners(self) -> tuple[ProjectListener, ...]:
        """The registered observer callbacks, in registration order."""
        return tuple(self._listeners)

    # -- listeners ---------------------------------------------------------------

    def add_listener(self, listener: ProjectListener) -> ProjectListener:
        """Register an observer callback (idempotent); returns it, so it can
        be used as a decorator."""
        if listener not in self._listeners:
            self._listeners.append(listener)
        return listener

    def remove_listener(self, listener: ProjectListener) -> None:
        """Unregister an observer callback (no-op when not registered)."""
        if listener in self._listeners:
            self._listeners.remove(listener)

    # -- operations ---------------------------------------------------------------

    def refresh(self) -> tuple[ProjectSummary, ...]:
        """Re-list the projects under :attr:`root`; returns the new tuple and
        notifies the listeners."""
        self._projects = tuple(list_projects(self.root))
        self._emit()
        return self._projects

    def create_project(self, name: str) -> Project:
        """Create a project named ``name`` under :attr:`root`, make it the
        current project, and persist it as ``last_project``; returns it.

        A missing root is created on demand here (the store's
        ``mkdir(parents=True)`` creates it together with the project
        directory). Raises ``ValueError`` (unusable name),
        :class:`ProjectExistsError` (slug collision), or
        :class:`ProjectRootError` (root not a directory / not writable /
        not creatable — the message carries the Settings hint) — in all
        cases the current project, the list, and the persisted setting are
        unchanged and no event is fired.
        """
        root_error = self.root_error
        if root_error is not None:
            raise ProjectRootError(root_error)
        try:
            project = create_project(name, root=self.root)
        except OSError as exc:
            # e.g. the root (or a parent) turned out unwritable mid-create
            raise ProjectRootError(
                f"cannot create a project under {self.root}: {exc} — "
                "set a writable project root in Settings"
            ) from exc
        self._projects = tuple(list_projects(self.root))
        self._set_current(project)
        return project

    def open_project(self, path: str | Path) -> Project:
        """Open the project at ``path``, make it the current project, and
        persist it as ``last_project``; returns it.

        Raises :class:`ProjectNotFoundError` / :class:`InvalidProjectError`
        (or ``OSError``) — in all cases the current project, the list, and
        the persisted setting are unchanged and no event is fired.
        """
        project = open_project(path)
        self._set_current(project)
        return project

    def delete_project(self, path: str | Path) -> Path:
        """Delete the project at ``path`` via the store's
        :func:`~seestar_controller.store.delete_project`, re-list the
        projects, and fire an event; returns the deleted path.

        When the deleted project is the current one, it is closed first (a
        project being deleted has no in-flight use — the store refuses to
        delete an open project), :attr:`current` becomes ``None``, the
        persisted ``last_project`` is cleared, and the attached controllers
        are notified with the ``None`` current.

        Raises :class:`~seestar_controller.store.errors.ProjectOpenError`
        when the project is still open through another handle,
        :class:`~seestar_controller.store.errors.ProjectNotFoundError` /
        :class:`~seestar_controller.store.errors.InvalidProjectError` for a
        missing / non-project path, or ``OSError`` from the filesystem — in
        all cases the controller state is left unchanged (a current project
        closed for the attempt is reopened) and no event is fired.
        """
        path = Path(path)
        current = self._current
        was_current = current is not None and Path(current.path).resolve() == path.resolve()
        previous = str(current.path) if was_current else ""
        if current is not None and was_current:
            current.close()  # unregisters the handle, so the store allows the delete
            self._current = None
        try:
            delete_project(path)
        except (ProjectError, OSError):
            if was_current:
                # roll back: the delete was refused — reopen and restore the
                # current project exactly as it was (no event was fired yet)
                self._current = open_project(path)
                self._state.set_setting("last_project", previous)
                self._state.save()
            raise
        if was_current:
            self._state.set_setting("last_project", "")
            self._state.save()
        self._projects = tuple(list_projects(self.root))
        self._emit()
        return path

    def restore(self) -> Project | None:
        """Startup: open the persisted ``last_project`` when it still exists.

        Returns the opened project (also set as :attr:`current`, with an
        event fired), or ``None`` when no ``last_project`` is stored. A stale
        stored path (project deleted since the last save) is cleared (the
        setting is reset to ``""`` and persisted) and ``None`` is returned.
        """
        raw = self._state.get_setting("last_project").strip()
        if not raw:
            return None
        try:
            project = open_project(raw)
        except (ProjectError, ValueError, OSError):
            # stale: the project directory is gone or invalid — clear it
            self._state.set_setting("last_project", "")
            self._state.save()
            return None
        self._current = project
        self._projects = tuple(list_projects(self.root))
        self._emit()
        return project

    def attach_controllers(self, **controllers: Any) -> None:
        """Register app controllers that follow the current project.

        On every change, ``set_project(event.current)`` is called on each
        given controller (``None`` values are skipped); the current project
        is also applied once immediately (so a project restored at startup
        reaches controllers attached afterwards). Keyword names are free-form
        (``observe=...``, ``viewer=...``, ...).
        """
        followers = tuple(controller for controller in controllers.values() if controller is not None)

        def _follow(event: ProjectEvent) -> None:
            for controller in followers:
                controller.set_project(event.current)

        self.add_listener(_follow)
        _follow(self._event())  # apply the current project right away

    # -- internals ---------------------------------------------------------------

    def _set_current(self, project: Project) -> None:
        """Make ``project`` current, persist it as ``last_project``, notify."""
        self._current = project
        self._state.set_setting("last_project", str(project.path))
        self._state.save()
        self._emit()

    def _event(self) -> ProjectEvent:
        return ProjectEvent(
            root=str(self.root),
            projects=self._projects,
            current=self._current,
        )

    def _emit(self) -> None:
        event = self._event()
        for listener in list(self._listeners):
            listener(event)
