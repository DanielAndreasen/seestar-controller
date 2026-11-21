"""The Enhance-viewer controller — the PURE controller layer of
:mod:`seestar_controller.enhance` (image-enhance ticket 01).

This is a PURE module: **no ``gi`` import** (the sandbox has no GTK, see
docs/wiki/assumptions.md; a subprocess test enforces the boundary) — stdlib +
numpy/PIL + :mod:`seestar_controller.store` only. The widget layer
(:mod:`.viewer`) imports ``gi`` at module top and is never imported by this
module or eagerly by the package ``__init__``.

:class:`ViewerController` is the object the app owns (one shared instance, like
the ui :class:`~seestar_controller.ui.connection.ConnectionController` and the
observing :class:`~seestar_controller.observing.queue_controller.PlanQueueController`)
and the Enhance view's :class:`~.viewer.ImageViewer` widget renders:

- :meth:`set_project` — set/replace the current store project (the Projects
  view is a later ticket, so for now the app sets it programmatically; with no
  project the view shows a "no project" placeholder);
- :meth:`refresh_stacks` / :attr:`stacks` — (re-)list the project's stored
  frames via :func:`~.fits_io.list_project_frames` — ALL of them (subs,
  stacks, enhanced; sub-frames ticket 01), each carrying its ``kind``;
- :meth:`open_stack` — open one frame by frame id (any kind — a sub opens
  exactly like a stack): the FITS is loaded through
  :func:`~.fits_io.load_fits` (cached by ``path`` + mtime, so re-opening an
  unchanged file is free and a changed file is re-read) and a tone-mapped
  preview is rendered through :func:`~.render.render_preview`;
- listeners — registered with :meth:`add_listener` / :meth:`remove_listener`,
  notified with a frozen :class:`ViewerSnapshot` after every change (stack
  list refreshed, stack opened, error). Listener exceptions are isolated
  (logged, never propagated), mirroring the link transport / executor.

NO enhancement operations yet — those are ticket 02 (the pipeline); this
controller only loads + renders the original frame.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import NoReturn

from PIL import Image

from seestar_controller.store.project import Project

from .fits_io import (
    EnhanceError,
    FitsImage,
    StackInfo,
    list_project_frames,
    load_fits,
)
from .render import DEFAULT_MAX_PREVIEW_SIZE, render_preview

logger = logging.getLogger(__name__)

#: Maximum number of loaded FITS images kept in the cache (oldest evicted).
MAX_CACHE_ENTRIES = 8


@dataclass(frozen=True)
class ViewerSnapshot:
    """An immutable snapshot of everything the Enhance view renders, handed
    to the listeners on any change.

    - ``project_name`` — the current project's display name (None when no
      project is set);
    - ``stacks`` — the listed frames (tuple of :class:`StackInfo` — all
      stored frames, subs included, each with its ``kind``);
    - ``current`` — the currently open frame (None when none is open);
    - ``image`` — the loaded :class:`FitsImage` of the current frame;
    - ``preview`` — the tone-mapped preview (:class:`PIL.Image.Image`) of the
      current frame;
    - ``error`` — the last error message, else None.
    """

    project_name: str | None
    stacks: tuple[StackInfo, ...]
    current: StackInfo | None
    image: FitsImage | None
    preview: Image.Image | None
    error: str | None


#: A viewer listener: called with a :class:`ViewerSnapshot` on any change.
ViewerListener = Callable[[ViewerSnapshot], None]


class ViewerController:
    """The app-level Enhance-viewer controller: the current project, its
    stored frames (all kinds — subs, stacks, enhanced), the open frame +
    preview, and observer notification.

    - ``project`` — the store project (None = no project yet; the view shows
      a placeholder then);
    - ``preview_max_size`` — the preview downscale bound (see
      :func:`~.render.render_preview`).
    """

    def __init__(
        self,
        project: Project | None = None,
        *,
        preview_max_size: int = DEFAULT_MAX_PREVIEW_SIZE,
    ) -> None:
        self._project = project
        self._preview_max_size = preview_max_size
        self._listeners: list[ViewerListener] = []
        self._stacks: tuple[StackInfo, ...] = ()
        self._current: StackInfo | None = None
        self._image: FitsImage | None = None
        self._preview: Image.Image | None = None
        self._error: str | None = None
        #: loaded FITS cache: (str(path), mtime) -> FitsImage (insertion-ordered)
        self._cache: dict[tuple[str, float], FitsImage] = {}
        if project is not None:
            try:
                self.refresh_stacks()
            except EnhanceError as exc:
                self._error = str(exc)

    # -- project + stacks ---------------------------------------------------------

    @property
    def project(self) -> Project | None:
        """The current store project (None when not set yet)."""
        return self._project

    def set_project(self, project: Project | None) -> None:
        """Set/replace the current project (None clears it): the frame list
        is re-listed, the open frame is closed, and the listeners are
        notified. A listing failure is recorded as the snapshot's ``error``
        (not raised)."""
        self._project = project
        self._current = None
        self._image = None
        self._preview = None
        self._error = None
        if project is not None:
            try:
                self.refresh_stacks()
                return  # refresh_stacks already notified
            except EnhanceError as exc:
                self._error = str(exc)
        else:
            self._stacks = ()
        self._notify()

    @property
    def stacks(self) -> tuple[StackInfo, ...]:
        """The listed frames of the current project — ALL stored frames
        (subs, stacks, enhanced; empty when no project)."""
        return self._stacks

    def refresh_stacks(self) -> tuple[StackInfo, ...]:
        """(Re-)list the current project's stored frames (all kinds — subs
        included); returns them and notifies the listeners. Raises
        :class:`EnhanceError` when no project is set or the listing fails
        (the error is also recorded in the snapshot)."""
        if self._project is None:
            self._stacks = ()
            self._error = None
            self._notify()
            raise EnhanceError(
                "no project — set one with set_project() first "
                "(the Projects view is a later ticket)"
            )
        stacks = tuple(list_project_frames(self._project))
        self._stacks = stacks
        self._error = None
        self._notify()
        return stacks

    # -- opening a stack ------------------------------------------------------------

    @property
    def current_stack(self) -> StackInfo | None:
        """The currently open frame (None when none is open)."""
        return self._current

    @property
    def current_image(self) -> FitsImage | None:
        """The loaded :class:`FitsImage` of the current frame."""
        return self._image

    @property
    def preview(self) -> Image.Image | None:
        """The tone-mapped preview of the current frame."""
        return self._preview

    def open_stack(self, frame_id: int) -> FitsImage:
        """Open the frame with frame id ``frame_id`` — any kind (a sub opens
        exactly like a stack): load its FITS (cached by path + mtime), render
        the preview, set it as current, notify the listeners, and return the
        :class:`FitsImage`.

        Raises :class:`EnhanceError` when no project is set, the id is unknown
        (the frame list is refreshed once first, so newly stored frames are
        found), the file is missing/unreadable, or the render fails. The
        error is also recorded in the snapshot and the listeners are
        notified before the raise.
        """
        if self._project is None:
            self._fail(
                EnhanceError(
                    "no project — set one with set_project() first "
                    "(the Projects view is a later ticket)"
                )
            )
        stack = next((s for s in self._stacks if s.frame_id == frame_id), None)
        if stack is None:
            # maybe a new frame was stored since the last listing: refresh once
            try:
                self.refresh_stacks()
            except EnhanceError:
                pass  # keep the old list; the lookup below reports the miss
            stack = next((s for s in self._stacks if s.frame_id == frame_id), None)
        if stack is None:
            self._fail(
                EnhanceError(
                    f"project {self._project.slug!r} has no frame with frame id "
                    f"{frame_id!r} (listed: "
                    f"{[s.frame_id for s in self._stacks] or 'none'})"
                )
            )
        try:
            mtime = stack.path.stat().st_mtime
        except OSError as exc:
            self._fail(EnhanceError(f"cannot stat frame file {stack.path}: {exc}"))
        key = (str(stack.path), mtime)
        try:
            image = self._cache.get(key)
            if image is None:
                image = load_fits(stack.path)
                self._cache[key] = image
                while len(self._cache) > MAX_CACHE_ENTRIES:
                    self._cache.pop(next(iter(self._cache)))
            preview = render_preview(image, max_size=self._preview_max_size)
        except EnhanceError as exc:
            self._fail(exc)  # records the error, notifies, raises
        self._current = stack
        self._image = image
        self._preview = preview
        self._error = None
        self._notify()
        return image

    def _fail(self, exc: EnhanceError) -> NoReturn:
        """Record ``exc`` in the snapshot, notify, and raise it."""
        self._error = str(exc)
        self._notify()
        raise exc

    # -- snapshot + listeners ---------------------------------------------------------

    @property
    def snapshot(self) -> ViewerSnapshot:
        """The frozen :class:`ViewerSnapshot` of the current state."""
        return ViewerSnapshot(
            project_name=self._project.name if self._project is not None else None,
            stacks=self._stacks,
            current=self._current,
            image=self._image,
            preview=self._preview,
            error=self._error,
        )

    @property
    def listeners(self) -> tuple[ViewerListener, ...]:
        """The registered observer callbacks, in registration order."""
        return tuple(self._listeners)

    def add_listener(self, listener: ViewerListener) -> ViewerListener:
        """Register an observer callback (idempotent); returns it, so it can
        be used as a decorator. The listener receives a frozen
        :class:`ViewerSnapshot` on any change."""
        if listener not in self._listeners:
            self._listeners.append(listener)
        return listener

    def remove_listener(self, listener: ViewerListener) -> None:
        """Unregister an observer callback (no-op when not registered)."""
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _notify(self) -> None:
        """Hand a frozen snapshot to every listener; a failing listener is
        logged and isolated (never breaks the others or the caller)."""
        snapshot = self.snapshot
        for listener in list(self._listeners):
            try:
                listener(snapshot)
            except Exception:
                logger.exception("enhance viewer listener failed")
