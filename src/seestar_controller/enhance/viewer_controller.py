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
- :meth:`refresh_stacks` / :attr:`stacks` — (re-)list the project's listed
  entries via :func:`~.fits_io.list_project_frames` — ALL stored frames (subs,
  stacks, enhanced; sub-frames ticket 01) — PLUS the file-based stitched
  mosaics via :func:`~.fits_io.list_project_mosaics` (app-improvement ticket
  08), each carrying its ``kind``. The list is sorted NEWEST-FIRST
  (descending ``jd``) so the latest entry is on top, and it is FILTERED by
  default (image-enhance issue 05): :attr:`stacks` shows the ``kind="stack"``
  frames plus the file-based mosaics; :meth:`set_show_all` (the left panes'
  "show all" toggle) also lists the subs and enhanced frames — with 100+ subs
  the single stack would otherwise be buried among the sub rows. The full,
  unfiltered listing stays available as :attr:`all_stacks` (and
  :meth:`open_stack` opens any kind from it);
- :meth:`open_stack` — open one frame by frame id (any kind — a sub opens
  exactly like a stack): the FITS is loaded through
  :func:`~.fits_io.load_fits` (cached by ``path`` + mtime, so re-opening an
  unchanged file is free and a changed file is re-read) and a tone-mapped
  preview is rendered through :func:`~.render.render_preview`;
- :meth:`open_path` — the open-file path: open ANY FITS file by path (a
  stitched mosaic is file-based, not a ``frames`` row, so this is how it is
  opened — app-improvement ticket 08), listed or not;
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
from pathlib import Path
from typing import NoReturn

from PIL import Image

from seestar_controller.mosaic import MOSAICS_DIR
from seestar_controller.store.project import Project

from .fits_io import (
    KIND_FILE,
    KIND_MOSAIC,
    KIND_STACK,
    EnhanceError,
    FitsImage,
    StackInfo,
    list_project_frames,
    list_project_mosaics,
    load_fits,
    stack_info_for_path,
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
    - ``stacks`` — the listed entries (tuple of :class:`StackInfo`) AS SHOWN
      in the viewer lists: newest-first, filtered to the stacks + file-based
      mosaics unless :attr:`show_all` is on (each entry carries its ``kind``);
    - ``current`` — the currently open entry (None when none is open);
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
    show_all: bool = False


#: A viewer listener: called with a :class:`ViewerSnapshot` on any change.
ViewerListener = Callable[[ViewerSnapshot], None]


class ViewerController:
    """The app-level Enhance-viewer controller: the current project, its
    listed entries (the stacks + file-based mosaics by default; subs and
    enhanced frames too when :attr:`show_all` is on), the open entry +
    preview, and observer notification.

    - ``project`` — the store project (None = no project yet; the view shows
      a placeholder then);
    - ``preview_max_size`` — the preview downscale bound (see
      :func:`~.render.render_preview`);
    - ``show_all`` — the initial list filter: False (the default) lists the
      ``kind="stack"`` frames plus the file-based mosaics only (image-enhance
      issue 05 — with 100+ subs the stack row would be buried otherwise);
      True lists every stored frame (subs, stacks, enhanced) plus the
      mosaics. Change it later with :meth:`set_show_all`.
    """

    def __init__(
        self,
        project: Project | None = None,
        *,
        preview_max_size: int = DEFAULT_MAX_PREVIEW_SIZE,
        show_all: bool = False,
    ) -> None:
        self._project = project
        self._preview_max_size = preview_max_size
        self._listeners: list[ViewerListener] = []
        self._show_all = show_all
        #: the FULL listing (all stored frames + mosaics, newest-first) —
        #: what open_stack/open_path look entries up in (any kind opens)
        self._all_stacks: tuple[StackInfo, ...] = ()
        #: the VISIBLE list (the filtered view of _all_stacks) — what
        #: :attr:`stacks` / the snapshot expose to the viewer lists
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
            self._all_stacks = ()
            self._stacks = ()
        self._notify()

    @property
    def stacks(self) -> tuple[StackInfo, ...]:
        """The listed entries of the current project AS SHOWN in the viewer
        lists, NEWEST-FIRST (descending ``jd`` — the latest stack is on top).

        Filtered by default (image-enhance issue 05): only the
        ``kind="stack"`` frames PLUS the file-based stitched mosaics
        (``kind="mosaic"``, ``frame_id=None``; app-improvement ticket 08) —
        with 100+ subs the single stack would be buried among the sub rows.
        With :attr:`show_all` on, ALL stored frames (subs, stacks, enhanced)
        plus the mosaics are listed. Empty when no project. The unfiltered
        listing is :attr:`all_stacks`."""
        return self._stacks

    @property
    def all_stacks(self) -> tuple[StackInfo, ...]:
        """The FULL listing of the current project — ALL stored frames (any
        kind: sub, stack, enhanced) plus the file-based mosaics, newest-first
        — regardless of the :attr:`show_all` filter. This is what
        :meth:`open_stack` / :meth:`open_path` look entries up in, so any
        kind stays openable."""
        return self._all_stacks

    @property
    def show_all(self) -> bool:
        """Whether the list filter is off: True lists every stored frame
        (subs/enhanced included) plus the mosaics; False (the default) lists
        the stacks + mosaics only. See :meth:`set_show_all`."""
        return self._show_all

    def set_show_all(self, show_all: bool) -> None:
        """Toggle the list filter: ``show_all=False`` (the default) shows the
        ``kind="stack"`` frames plus the file-based mosaics only;
        ``show_all=True`` also shows the subs and enhanced frames. The
        visible :attr:`stacks` list is re-filtered from the full listing and
        the listeners are notified (a no-op when the value is unchanged)."""
        show_all = bool(show_all)
        if show_all == self._show_all:
            return
        self._show_all = show_all
        self._stacks = self._visible(self._all_stacks)
        self._notify()

    def _visible(self, entries: tuple[StackInfo, ...]) -> tuple[StackInfo, ...]:
        """The filtered view of ``entries``: everything when
        :attr:`show_all` is on, else the stacks (``kind="stack"``) plus the
        file-based entries (mosaics — ``frame_id is None``)."""
        if self._show_all:
            return entries
        return tuple(
            entry
            for entry in entries
            if entry.kind == KIND_STACK or entry.frame_id is None
        )

    @property
    def mosaics(self) -> tuple[StackInfo, ...]:
        """The file-based entries of the listing — the stitched mosaics
        (``frame_id=None``)."""
        return tuple(s for s in self._all_stacks if s.frame_id is None)

    def refresh_stacks(self) -> tuple[StackInfo, ...]:
        """(Re-)list the current project's entries — ALL stored frames (all
        kinds — subs included) plus the file-based mosaics, sorted
        NEWEST-FIRST (descending ``jd``) — and apply the current list filter
        (:attr:`show_all`, default: stacks + mosaics only). Returns the
        VISIBLE entries (same as :attr:`stacks`) and notifies the listeners;
        the full listing stays in :attr:`all_stacks`. Raises
        :class:`EnhanceError` when no project is set or the listing fails
        (the error is also recorded in the snapshot)."""
        if self._project is None:
            self._all_stacks = ()
            self._stacks = ()
            self._error = None
            self._notify()
            raise EnhanceError(
                "no project — set one with set_project() first "
                "(the Projects view is a later ticket)"
            )
        frames = list_project_frames(self._project)
        mosaics = list_project_mosaics(self._project)
        # newest-first: the latest stack is the top row of the viewer list
        all_entries = tuple(
            sorted((*frames, *mosaics), key=lambda info: info.jd, reverse=True)
        )
        self._all_stacks = all_entries
        self._stacks = self._visible(all_entries)
        self._error = None
        self._notify()
        return self._stacks

    # -- opening a stack ------------------------------------------------------------

    @property
    def current_stack(self) -> StackInfo | None:
        """The currently open entry (a stored frame or a file-based mosaic;
        None when none is open)."""
        return self._current

    @property
    def current_image(self) -> FitsImage | None:
        """The loaded :class:`FitsImage` of the current frame."""
        return self._image

    @property
    def preview(self) -> Image.Image | None:
        """The tone-mapped preview of the current frame."""
        return self._preview

    def open_stack(self, frame_id: int | None) -> FitsImage:
        """Open the frame with frame id ``frame_id`` — any kind (a sub opens
        exactly like a stack): load its FITS (cached by path + mtime), render
        the preview, set it as current, notify the listeners, and return the
        :class:`FitsImage`.

        The lookup goes through the FULL listing (:attr:`all_stacks`), not
        the filtered :attr:`stacks`, so a sub or enhanced frame opens even
        while the list shows stacks only (image-enhance issue 05).

        Raises :class:`EnhanceError` when no project is set, ``frame_id`` is
        ``None`` (a file-based entry such as a mosaic has no frame id — open
        it by path with :meth:`open_path`), the id is unknown (the entry list
        is refreshed once first, so newly stored frames are found), the file
        is missing/unreadable, or the render fails. The error is also
        recorded in the snapshot and the listeners are notified before the
        raise.
        """
        if self._project is None:
            self._fail(
                EnhanceError(
                    "no project — set one with set_project() first "
                    "(the Projects view is a later ticket)"
                )
            )
        if frame_id is None:
            self._fail(
                EnhanceError(
                    "file-based entries (mosaics, arbitrary files) have no frame "
                    "id — open them by path with open_path()"
                )
            )
        stack = next((s for s in self._all_stacks if s.frame_id == frame_id), None)
        if stack is None:
            # maybe a new frame was stored since the last listing: refresh once
            try:
                self.refresh_stacks()
            except EnhanceError:
                pass  # keep the old list; the lookup below reports the miss
            stack = next((s for s in self._all_stacks if s.frame_id == frame_id), None)
        if stack is None:
            self._fail(
                EnhanceError(
                    f"project {self._project.slug!r} has no frame with frame id "
                    f"{frame_id!r} (listed: "
                    f"{[s.frame_id for s in self._all_stacks if s.frame_id is not None] or 'none'})"
                )
            )
        return self._open(stack)

    def open_path(self, path: str | Path) -> FitsImage:
        """Open ANY FITS file by ``path`` — the open-file path for file-based
        products: a stitched mosaic (``mosaics/<session>.fits``) is NOT a
        ``frames`` row, so this is how it is opened in the Enhance view
        (app-improvement ticket 08).

        A relative ``path`` is resolved against the current project's
        directory. When the file is one of the listed entries (a stored frame
        or a listed mosaic), that entry is opened; otherwise a transient
        :class:`StackInfo` is built from the file (``kind="mosaic"`` inside the
        project's ``mosaics/`` directory, else ``"file"``; ``frame_id=None``).
        The FITS is loaded (cached by path + mtime), the preview rendered, the
        entry set as current, the listeners notified, and the
        :class:`FitsImage` returned.

        Raises :class:`EnhanceError` when no project is set, the file cannot
        be stat-ed/read, or the render fails (recorded in the snapshot and
        notified before the raise).
        """
        if self._project is None:
            self._fail(
                EnhanceError(
                    "no project — set one with set_project() first "
                    "(the Projects view is a later ticket)"
                )
            )
        file_path = Path(path)
        if not file_path.is_absolute():
            file_path = self._project.path / file_path
        info = next((s for s in self._all_stacks if s.path == file_path), None)
        if info is None:
            # maybe a new file was written since the last listing: refresh once
            try:
                self.refresh_stacks()
            except EnhanceError:
                pass  # keep the old list; the transient entry below is built
            info = next((s for s in self._all_stacks if s.path == file_path), None)
        if info is None:
            kind = (
                KIND_MOSAIC
                if file_path.parent == self._project.path / MOSAICS_DIR
                else KIND_FILE
            )
            try:
                info = stack_info_for_path(file_path, kind=kind)
            except EnhanceError as exc:
                self._fail(exc)  # records the error, notifies, raises
        return self._open(info)

    def _open(self, info: StackInfo) -> FitsImage:
        """Open the listed entry ``info``: load its FITS (cached by path +
        mtime), render the preview, set it as current, notify, and return
        the :class:`FitsImage`. Errors go through :meth:`_fail`."""
        try:
            mtime = info.path.stat().st_mtime
        except OSError as exc:
            self._fail(EnhanceError(f"cannot stat frame file {info.path}: {exc}"))
        key = (str(info.path), mtime)
        try:
            image = self._cache.get(key)
            if image is None:
                image = load_fits(info.path)
                self._cache[key] = image
                while len(self._cache) > MAX_CACHE_ENTRIES:
                    self._cache.pop(next(iter(self._cache)))
            preview = render_preview(image, max_size=self._preview_max_size)
        except EnhanceError as exc:
            self._fail(exc)  # records the error, notifies, raises
        self._current = info
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
            show_all=self._show_all,
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
