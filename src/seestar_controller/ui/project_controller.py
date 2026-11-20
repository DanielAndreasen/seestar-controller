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
- :class:`ProjectController` — wraps the store's
  :func:`~seestar_controller.store.create_project` /
  :func:`~seestar_controller.store.open_project` /
  :func:`~seestar_controller.store.list_projects` against the **settings
  project root** (``project_root`` setting; empty -> the store's
  :func:`~seestar_controller.store.default_project_root`, i.e.
  ``SEESTAR_PROJECT_ROOT`` > ``~/SeestarProjects/``; an explicit ``root=``
  constructor argument overrides both, for tests/standalone use), holds the
  current :class:`~seestar_controller.store.Project`, persists the last
  opened project path in the ``last_project`` setting (validated: empty or
  an existing project directory containing ``project.json``), restores it
  on startup via :meth:`ProjectController.restore`, and notifies listeners
  with a frozen :class:`ProjectEvent` on every change;
- :meth:`ProjectController.attach_controllers` — registers the app's shared
  controllers (observe / viewer / pick / run / lightcurve / period) so
  ``set_project(current)`` is called on each whenever the current project
  changes (and once immediately, to apply a restored project).

Store errors (:class:`~seestar_controller.store.errors.ProjectExistsError`,
:class:`~seestar_controller.store.errors.ProjectNotFoundError`,
:class:`~seestar_controller.store.errors.InvalidProjectError`,
``ValueError`` for an unusable name, ``OSError`` for an unwritable root)
propagate to the caller — the widget layer shows them inline; on failure the
current project and the persisted ``last_project`` are left unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from seestar_controller.store import (
    Project,
    ProjectSummary,
    create_project,
    default_project_root,
    list_projects,
    open_project,
)
from seestar_controller.store.errors import ProjectError

from .state import AppState

#: A project-change listener: called with a frozen :class:`ProjectEvent`
#: after every successful change (list refresh, create, open, restore).
ProjectListener = Callable[["ProjectEvent"], None]


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
    - :attr:`projects` — the listed projects (refreshed by
      :meth:`refresh` and after every create/open/restore);
    - :attr:`current` — the open project (``None`` until one is created or
      opened);
    - :meth:`create_project` / :meth:`open_project` — create/open a project
      and make it current (persisting ``last_project``); store errors
      propagate and change nothing;
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

        Raises ``ValueError`` (unusable name), :class:`ProjectExistsError`
        (slug collision), or ``OSError`` (unwritable root) — in all cases the
        current project, the list, and the persisted setting are unchanged
        and no event is fired.
        """
        project = create_project(name, root=self.root)
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
