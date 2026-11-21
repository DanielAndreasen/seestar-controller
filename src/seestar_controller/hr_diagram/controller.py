"""The H-R diagram controller — the app-level glue of hr-diagram ticket 03.

This is the STORE-BOUNDARY module of :mod:`seestar_controller.hr_diagram` (the
same role as :mod:`seestar_controller.gaia.overlay`): **no ``gi`` import** —
stdlib + numpy + PIL + :mod:`seestar_controller.store` + the sibling pure
modules only — so it is fully unit-testable headless (a subprocess test
enforces the boundary). The widget layer (:mod:`.view`) imports ``gi`` at
module top and is never imported by this module or eagerly by the package
``__init__``. No network: the isochrone fetch is injectable (``fetcher``) and
falls back to the bundled grid on any failure, so tests never touch the
network.

What lives here:

- :class:`HrDiagramSnapshot` — the frozen snapshot the widget renders:
  the project name, the cross-matched stacks, the selected stack's label,
  the raw cross-match table, the membership, the CMD + H-R points, the
  persisted isochrone params, the isochrone overlay points, the tone-mapped
  stack image (PIL) + its original pixel size, a status line, and the last
  error;
- :class:`HrDiagramController` — the app-level shared controller (the same
  role as the lightcurve / cross-match controllers): it follows the shared
  viewer controller (the open project becomes the diagram's project, and an
  open stack WITH a persisted cross-match table is auto-selected), lists the
  cross-matched stacks (:func:`~seestar_controller.store.crossmatch.list_crossmatched_stacks`),
  :meth:`~HrDiagramController.select_stack` loads one stack's table + image
  + isochrone overlay, :meth:`~HrDiagramController.refresh` reloads, and
  :meth:`~HrDiagramController.export_table` writes the table CSV (reusing
  :func:`~.export.export_table_csv`). Listener notifications are marshalled
  through ``invoke_on_main`` (the app binds ``GLib.idle_add``).

Threading / sqlite thread-affinity: like the other shared controllers, every
STORE call goes through :meth:`HrDiagramController._call_store` (direct on
the db-owning thread, otherwise marshalled through ``invoke_on_main`` with a
wait), so the widget can run :meth:`~HrDiagramController.select_stack` on a
worker thread. The isochrone fetch itself runs on the calling (worker)
thread — never on the db thread.

Error policy: argument validation raises immediately (:class:`HrError`, a
:class:`ValueError`); runtime failures (store, unreadable frame, isochrone)
are recorded in the snapshot's ``error`` and the listeners are notified —
never raised — because the widget runs the operations on a worker thread and
renders ``snapshot.error`` as the status.

Documented choices:

- **Stack image.** The stack FITS is read via
  :func:`~seestar_controller.store.frames.read_frame_bytes` with a LAZY
  astropy import, converted to a 2-D float array (RGB -> luminance), and
  tone-mapped with a LOCAL percentile stretch (deliberately NOT
  :mod:`seestar_controller.enhance.render` — the module boundary forbids
  importing enhance; the diagram only needs a plain linear stretch,
  NaN -> 0), exactly like the gaia overlay. The snapshot carries the FULL
  resolution PIL image + ``image_size``; the widget downscales when drawing
  the markers (see :func:`~.plot.render_stack_png`).
- **Isochrone overlay.** The params come from
  :func:`~seestar_controller.store.isochrone.isochrone_params` (ticket 02's
  persisted singleton row — NO schema change here); the overlay points come
  from :func:`~.isochrone.isochrone` with the injectable ``fetcher`` /
  ``cache_dir``. No params -> empty overlay. Any isochrone failure -> empty
  overlay + the error in the snapshot (the diagram still renders).
"""

from __future__ import annotations

import io
import logging
import math
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from seestar_controller.store.crossmatch import (
    CrossmatchedStack,
    list_crossmatched_stacks,
    load_crossmatch,
)
from seestar_controller.store.frames import (
    KIND_STACK,
    Frame,
    list_frames,
    read_frame_bytes,
)
from seestar_controller.store.isochrone import isochrone_params
from seestar_controller.store.project import Project

from .core import HrError, HrPoint, Membership, cmd_points, hr_points, membership_filter
from .export import export_table_csv
from .isochrone import IsochronePoint, isochrone

logger = logging.getLogger(__name__)

#: Timeout for a store call marshalled to the db-owning thread, in seconds.
DB_CALL_TIMEOUT_S = 60.0

#: Default lower percentile of the finite pixel values for the stack
#: tone-map black point.
DEFAULT_LO_PERCENTILE = 0.1

#: Default upper percentile of the finite pixel values for the stack
#: tone-map white point.
DEFAULT_HI_PERCENTILE = 99.5

#: A listener: called with a frozen :class:`HrDiagramSnapshot` on any change.
HrDiagramListener = Callable[["HrDiagramSnapshot"], None]

#: The marshalling callable for listener notifications (the app binds
#: ``GLib.idle_add``; the default calls directly).
InvokeOnMain = Callable[[Callable[[], None]], Any]


def _direct_invoke(callback: Callable[[], None]) -> None:
    """The default ``invoke_on_main``: run the callback directly (tests)."""
    callback()


def _gray(data: Any) -> np.ndarray:
    """A 2-D float32 view of image data (RGB ``(H, W, 3)`` -> luminance)."""
    arr = np.asarray(data, dtype=np.float32)
    if arr.ndim == 3:
        arr = arr.mean(axis=2)
    if arr.ndim != 2:
        raise HrError(f"stack image must be 2-D (or RGB), got shape {arr.shape}")
    return arr


def tonemap_gray(
    data: Any,
    *,
    lo_percentile: float = DEFAULT_LO_PERCENTILE,
    hi_percentile: float = DEFAULT_HI_PERCENTILE,
) -> np.ndarray:
    """Tone-map a 2-D float image to a uint8 gray image (NaN/inf -> 0).

    The black/white points are the ``lo_percentile`` / ``hi_percentile``
    percentiles of the FINITE values; a degenerate range (``hi <= lo``, e.g.
    a constant frame) is widened so the mapping never divides by zero.
    Raises :class:`HrError` for a non-2-D input or bad percentiles.
    """
    arr = np.asarray(data, dtype=np.float32)
    if arr.ndim != 2:
        raise HrError(f"tonemap_gray expects a 2-D (H, W) array, got shape {arr.shape}")
    for name, value in (("lo_percentile", lo_percentile), ("hi_percentile", hi_percentile)):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0.0 <= value <= 100.0
        ):
            raise HrError(f"{name} must be a finite number in [0, 100], got {value!r}")
    finite = np.isfinite(arr)
    values = arr[finite]
    if values.size == 0:
        return np.zeros(arr.shape, dtype=np.uint8)
    lo = float(np.percentile(values, lo_percentile))
    hi = float(np.percentile(values, hi_percentile))
    if hi <= lo:
        hi = lo + 1.0
    scaled = (arr - lo) / (hi - lo)
    scaled = np.where(finite, scaled, 0.0)
    np.clip(scaled, 0.0, 1.0, out=scaled)
    return (scaled * 255.0).astype(np.uint8)


def load_stack_image(project: Project, frame: Frame) -> tuple[Image.Image, tuple[int, int]]:
    """Read one stored stack's FITS (lazy astropy import) and tone-map it to
    an RGB :class:`PIL.Image.Image`; returns ``(image, (width, height))`` of
    the ORIGINAL pixel grid (the crossmatch table's ``x`` / ``y`` are in
    those pixels).

    Raises :class:`HrError` when the FITS cannot be read or is not an image.
    """
    from astropy.io import fits as pyfits

    try:
        raw = read_frame_bytes(project, frame)
        with pyfits.open(io.BytesIO(raw)) as hdul:
            data = _gray(hdul[0].data)
    except HrError:
        raise
    except Exception as exc:
        raise HrError(f"cannot read the FITS of frame {frame.id}: {exc}") from exc
    height, width = data.shape
    gray = tonemap_gray(data)
    image = Image.fromarray(gray, mode="L").convert("RGB")
    return image, (int(width), int(height))


@dataclass(frozen=True)
class HrDiagramSnapshot:
    """An immutable snapshot of everything the H-R diagram view renders,
    handed to the listeners on any change.

    - ``project_name`` — the current project's display name (None when no
      project is set);
    - ``stacks`` — the project's cross-matched stacks
      (:class:`~seestar_controller.store.crossmatch.CrossmatchedStack`
      summaries, ordered by frame id);
    - ``frame_id`` — the selected stack's frame id (None when none);
    - ``stack_label`` — the selected stack's one-line label (None when none);
    - ``table`` — the selected stack's cross-match rows (empty when none);
    - ``membership`` — the cluster :class:`~.core.Membership` of the table
      (None when no table);
    - ``cmd`` — the apparent CMD points (:func:`~.core.cmd_points`);
    - ``hr`` — the H-R points (:func:`~.core.hr_points`, parallax SNR cut);
    - ``params`` — the persisted isochrone params (the store's
      ``IsochroneParams`` record, duck-typed), None when never set;
    - ``overlay`` — the isochrone overlay points (empty when no params or
      on failure);
    - ``image`` — the tone-mapped stack image (RGB PIL, full resolution),
      None when no stack is selected;
    - ``image_size`` — the original ``(width, height)`` pixel grid of the
      stack (the crossmatch ``x`` / ``y`` units), None when no image;
    - ``status`` — a short human-readable status, else None;
    - ``error`` — the last error message, else None.
    """

    project_name: str | None
    stacks: tuple[CrossmatchedStack, ...]
    frame_id: int | None
    stack_label: str | None
    table: tuple[Any, ...]
    membership: Membership | None
    cmd: tuple[HrPoint, ...]
    hr: tuple[HrPoint, ...]
    params: Any | None
    overlay: tuple[IsochronePoint, ...]
    image: Image.Image | None
    image_size: tuple[int, int] | None
    status: str | None
    error: str | None


class HrDiagramController:
    """The app-level H-R diagram controller: the current project, the
    cross-matched stacks, the selected stack's table + CMD/H-R points +
    isochrone overlay + tone-mapped image, and observer notification.

    - ``project`` — the store project (None = follow the attached viewer
      controller's project, see :meth:`attach_viewer`);
    - ``viewer_controller`` — an optional duck-typed viewer controller (the
      shared :class:`~seestar_controller.enhance.viewer_controller.ViewerController`
      — NOT imported here): its project is adopted whenever no explicit
      project is set, and its currently open stack is auto-selected when it
      has a persisted cross-match table;
    - ``invoke_on_main`` — the marshalling callable for listener
      notifications AND for store calls made from a non-db thread (the app
      binds ``GLib.idle_add``; the default calls directly);
    - ``isochrone_fetcher`` — the optional isochrone fetch callable
      (``fetcher(url=..., params=..., timeout=...)``); None uses the module
      default (tests inject a raising fetcher -> the bundled fallback grid,
      so no test ever touches the network);
    - ``cache_dir`` — the isochrone disk-cache dir (None = the default app
      cache dir).

    Operations: :attr:`snapshot` (never raises), :meth:`set_project`,
    :meth:`attach_viewer` / :meth:`detach_viewer`, :meth:`refresh_stacks`,
    :meth:`select_stack`, :meth:`refresh`, :meth:`export_table`, and
    listeners with frozen snapshots.

    Documented choice: the snapshot keeps the stack image at FULL resolution
    (``image_size`` == the image's pixel grid == the crossmatch ``x`` / ``y``
    units); the WIDGET downscales when drawing the markers
    (:func:`~.plot.render_stack_png` scales the markers with the image), so
    the click-to-highlight mapping stays exact.
    """

    def __init__(
        self,
        project: Project | None = None,
        viewer_controller: Any | None = None,
        *,
        invoke_on_main: InvokeOnMain | None = None,
        isochrone_fetcher: Callable[..., Any] | None = None,
        cache_dir: str | Path | None = None,
    ) -> None:
        self._project: Project | None = None
        self._viewer_controller: Any | None = None
        self._invoke_on_main: InvokeOnMain = invoke_on_main or _direct_invoke
        self._isochrone_fetcher = isochrone_fetcher
        self._cache_dir = cache_dir
        self._lock = threading.Lock()
        self._listeners: list[HrDiagramListener] = []
        self._db_thread_id: int | None = None
        self._stacks: tuple[CrossmatchedStack, ...] = ()
        self._frame_id: int | None = None
        self._stack_label: str | None = None
        self._table: tuple[Any, ...] = ()
        self._membership: Membership | None = None
        self._cmd: tuple[HrPoint, ...] = ()
        self._hr: tuple[HrPoint, ...] = ()
        self._params: Any | None = None
        self._overlay: tuple[IsochronePoint, ...] = ()
        self._image: Image.Image | None = None
        self._image_size: tuple[int, int] | None = None
        self._status: str | None = None
        self._error: str | None = None
        if viewer_controller is not None:
            self.attach_viewer(viewer_controller)
        if project is not None:
            self.set_project(project)

    # -- project + viewer -----------------------------------------------------------

    @property
    def project(self) -> Project | None:
        """The current project (explicit, else the viewer controller's)."""
        if self._project is not None:
            return self._project
        if self._viewer_controller is not None:
            return getattr(self._viewer_controller, "project", None)
        return None

    @property
    def viewer_controller(self) -> Any | None:
        """The viewer controller this controller follows (if any)."""
        return self._viewer_controller

    @property
    def invoke_on_main(self) -> InvokeOnMain:
        """The marshalling callable (the app binds ``GLib.idle_add``)."""
        return self._invoke_on_main

    def set_project(self, project: Project | None) -> None:
        """Set/replace the explicit project (None clears it — the viewer
        controller's project is then followed again). The db-owning thread is
        captured here (sqlite is thread-affine). The stack list is re-listed
        and the selection cleared. Notifies."""
        self._project = project
        if project is not None:
            self._db_thread_id = threading.get_ident()
        with self._lock:
            self._stacks = ()
            self._clear_stack()
        if project is not None:
            try:
                self.refresh_stacks()
                return  # refresh_stacks already notified
            except HrError as exc:
                with self._lock:
                    self._error = str(exc)
        self._emit(self.snapshot)

    def attach_viewer(self, viewer_controller: Any) -> None:
        """Follow ``viewer_controller`` (duck-typed — the shared viewer
        controller): register as its listener and adopt its project whenever
        no explicit project is set, and auto-select its open stack when that
        stack has a persisted cross-match table. Re-attaching detaches the
        previous viewer first."""
        if self._viewer_controller is not None:
            self._viewer_controller.remove_listener(self._on_viewer_snapshot)
        self._viewer_controller = viewer_controller
        viewer_controller.add_listener(self._on_viewer_snapshot)
        if self._project is None:
            project = getattr(viewer_controller, "project", None)
            if project is not None:
                self._db_thread_id = threading.get_ident()
            self._emit(self.snapshot)
        snapshot = getattr(viewer_controller, "snapshot", None)
        if snapshot is not None:
            self._on_viewer_snapshot(snapshot)

    def detach_viewer(self) -> None:
        """Stop following the viewer controller."""
        if self._viewer_controller is not None:
            self._viewer_controller.remove_listener(self._on_viewer_snapshot)
            self._viewer_controller = None

    def _on_viewer_snapshot(self, snapshot: Any) -> None:
        """Viewer listener: adopt the viewer's project (when no explicit
        project is set) and auto-select its open stack when that stack is
        cross-matched, so the diagram follows the stack open in the image
        viewer. Emits on every viewer change, so closing the project clears
        the diagram."""
        viewer = self._viewer_controller
        if viewer is None:
            return
        if self._project is None:
            project = getattr(viewer, "project", None)
            if project is not None:
                self._db_thread_id = threading.get_ident()
            self._emit(self.snapshot)
        current = getattr(snapshot, "current", None)
        frame_id = getattr(current, "frame_id", None) if current is not None else None
        if isinstance(frame_id, bool) or not isinstance(frame_id, int):
            frame_id = None
        if frame_id is None:
            if current is None and self._frame_id is not None:
                with self._lock:
                    self._clear_stack()
                self._emit(self.snapshot)
            return
        if frame_id == self._frame_id:
            return
        if frame_id not in {s.frame_id for s in self._stacks}:
            # the open stack has no cross-match table — try refreshing the
            # stack list once (a table may have been persisted meanwhile)
            try:
                self.refresh_stacks()
            except HrError:
                pass
            if frame_id not in {s.frame_id for s in self._stacks}:
                return
        try:
            self.select_stack(frame_id)
        except HrError as exc:
            with self._lock:
                self._error = str(exc)
            self._emit(self.snapshot)

    # -- stacks -----------------------------------------------------------

    @property
    def stacks(self) -> tuple[CrossmatchedStack, ...]:
        """The current project's cross-matched stacks (ordered by frame id)."""
        return self._stacks

    @property
    def frame_id(self) -> int | None:
        """The selected stack's frame id (None when none)."""
        return self._frame_id

    @property
    def stack_label(self) -> str | None:
        """The one-line label of the selected stack (None when none)."""
        return self._stack_label

    def refresh_stacks(self) -> tuple[CrossmatchedStack, ...]:
        """(Re-)list the project's cross-matched stacks via the store;
        notifies and returns them. Raises :class:`HrError` when no project
        is set or the store call fails."""
        project = self._require_project()
        try:
            stacks = self._call_store(lambda: tuple(list_crossmatched_stacks(project)))
        except Exception as exc:
            raise HrError(
                f"cannot list the cross-matched stacks of project {project.name!r}: {exc}"
            ) from exc
        with self._lock:
            self._stacks = stacks
            self._error = None
        self._emit(self.snapshot)
        return self._stacks

    def select_stack(self, frame_id: int) -> HrDiagramSnapshot:
        """Select the cross-matched stack with frame id ``frame_id``: load
        its cross-match table, compute the membership + CMD/H-R points, read
        the persisted isochrone params + overlay, load + tone-map the stack
        image, notify, and return the snapshot.

        Raises :class:`HrError` when no project is set, ``frame_id`` is not
        an int, or the frame is not a stack of the project. Runtime failures
        (unreadable table/FITS, isochrone) are recorded in the snapshot's
        ``error`` — never raised.
        """
        if isinstance(frame_id, bool) or not isinstance(frame_id, int):
            raise HrError(f"frame_id must be an int, got {frame_id!r}")
        project = self._require_project()
        frame = next(
            (
                f
                for f in self._call_store(lambda: list_frames(project, kind=KIND_STACK))
                if f.id == frame_id
            ),
            None,
        )
        if frame is None:
            raise HrError(
                f"project {project.name!r} has no stack with frame id {frame_id!r}"
            )
        error: str | None = None
        try:
            table = self._call_store(lambda: tuple(load_crossmatch(project, frame_id)))
        except Exception as exc:
            raise HrError(f"cannot load the cross-match table of stack {frame_id!r}: {exc}") from exc
        membership = membership_filter(table)
        cmd = cmd_points(table, membership=membership)
        hr = hr_points(table, membership=membership)
        try:
            params = self._call_store(lambda: isochrone_params(project))
        except Exception as exc:  # surfaced as snapshot.error, never raised  # noqa: BLE001
            params = None
            error = f"{type(exc).__name__}: {exc}"
        overlay = self._build_overlay(params)
        image: Image.Image | None = None
        image_size: tuple[int, int] | None = None
        try:
            image, image_size = self._call_store(lambda: load_stack_image(project, frame))
        except Exception as exc:  # surfaced as snapshot.error, never raised  # noqa: BLE001
            logger.info("loading the stack image failed: %s", exc)
            image, image_size = None, None
            error = error or f"{type(exc).__name__}: {exc}"
        matched = sum(1 for row in table if getattr(row, "matched", False))
        with self._lock:
            self._frame_id = frame_id
            self._stack_label = f"{frame.utc} — {frame.target or 'no target'}"
            self._table = table
            self._membership = membership
            self._cmd = cmd
            self._hr = hr
            self._params = params
            self._overlay = overlay
            self._image = image
            self._image_size = image_size
            self._error = error
            self._status = (
                f"{matched}/{len(table)} matched · {len(cmd)} CMD / {len(hr)} H-R points · "
                f"overlay {len(overlay)} pts"
                if table
                else "no cross-match table for this stack"
            )
        self._emit(self.snapshot)
        return self.snapshot

    def refresh(self) -> HrDiagramSnapshot:
        """Reload the current state: re-list the stacks and, when a stack is
        selected, reload its table/overlay/image; notify and return the
        fresh snapshot. Raises :class:`HrError` when no project is set."""
        self._require_project()
        self.refresh_stacks()
        frame_id = self._frame_id
        if frame_id is not None and frame_id in {s.frame_id for s in self._stacks}:
            return self.select_stack(frame_id)
        self._emit(self.snapshot)
        return self.snapshot

    # -- export -----------------------------------------------------------

    def export_table(self, path: str | Path) -> Path:
        """Export the selected stack's cross-match table as a CSV to
        ``path`` (reusing :func:`~.export.export_table_csv`, the same
        conventions as :mod:`seestar_controller.analysis.export`); returns
        the written :class:`~pathlib.Path`.

        Raises :class:`HrError` when no stack is selected (no table).
        """
        with self._lock:
            table, membership = self._table, self._membership
        if not table:
            raise HrError("no stack selected — nothing to export")
        return export_table_csv(table, path, membership=membership)

    # -- snapshot + listeners ---------------------------------------------------------------

    @property
    def snapshot(self) -> HrDiagramSnapshot:
        """The frozen :class:`HrDiagramSnapshot` of the current state
        (never raises)."""
        project = self.project
        with self._lock:
            return HrDiagramSnapshot(
                project_name=project.name if project is not None else None,
                stacks=self._stacks,
                frame_id=self._frame_id,
                stack_label=self._stack_label,
                table=self._table,
                membership=self._membership,
                cmd=self._cmd,
                hr=self._hr,
                params=self._params,
                overlay=self._overlay,
                image=self._image,
                image_size=self._image_size,
                status=self._status,
                error=self._error,
            )

    @property
    def listeners(self) -> tuple[HrDiagramListener, ...]:
        """The registered observer callbacks, in registration order."""
        return tuple(self._listeners)

    def add_listener(self, listener: HrDiagramListener) -> HrDiagramListener:
        """Register an observer callback (idempotent); returns it, so it can
        be used as a decorator. The listener receives a frozen
        :class:`HrDiagramSnapshot` on any change (marshalled via
        ``invoke_on_main``)."""
        if listener not in self._listeners:
            self._listeners.append(listener)
        return listener

    def remove_listener(self, listener: HrDiagramListener) -> None:
        """Unregister an observer callback (no-op when not registered)."""
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _notify(self, snapshot: HrDiagramSnapshot) -> None:
        """Hand a frozen snapshot to every listener; a failing listener is
        logged and isolated (never breaks the others or the caller)."""
        for listener in list(self._listeners):
            try:
                listener(snapshot)
            except Exception:
                logger.exception("hr-diagram listener failed")

    def _emit(self, snapshot: HrDiagramSnapshot) -> None:
        """Marshal a listener notification through ``invoke_on_main``."""
        self._invoke_on_main(lambda: self._notify(snapshot))

    # -- internals ---------------------------------------------------------------------------

    def _require_project(self) -> Project:
        project = self.project
        if project is None:
            raise HrError("no project set — call set_project() first")
        return project

    def _clear_stack(self) -> None:
        """Clear the selected-stack state (caller holds the lock)."""
        self._frame_id = None
        self._stack_label = None
        self._table = ()
        self._membership = None
        self._cmd = ()
        self._hr = ()
        self._params = None
        self._overlay = ()
        self._image = None
        self._image_size = None
        self._status = None
        self._error = None

    def _build_overlay(self, params: Any) -> tuple[IsochronePoint, ...]:
        """The isochrone overlay for the persisted ``params`` (None -> empty).
        Runs on the CALLING thread (never marshalled to the db thread — it
        may fetch). Any failure -> empty overlay (the diagram still renders)."""
        if params is None:
            return ()
        try:
            return tuple(
                isochrone(
                    params.age,
                    params.feh,
                    params.dist_mod,
                    cache_dir=self._cache_dir,
                    fetcher=self._isochrone_fetcher,
                )
            )
        except Exception as exc:  # surfaced as snapshot.error by the caller  # noqa: BLE001
            logger.info("isochrone overlay failed: %s", exc)
            return ()

    def _call_store(self, fn: Callable[[], Any]) -> Any:
        """Run the store callable ``fn`` and return its result.

        When already on the db-owning thread (or when no db thread was
        captured), ``fn`` runs directly. Otherwise it is marshalled through
        ``invoke_on_main`` (the app: ``GLib.idle_add``) and the calling thread
        waits for the result — the same pattern as the other shared
        controllers.
        """
        db_thread = self._db_thread_id
        if db_thread is None or db_thread == threading.get_ident():
            return fn()
        box: dict[str, Any] = {}
        done = threading.Event()

        def wrapper() -> None:
            try:
                box["result"] = fn()
            except BaseException as exc:  # re-raised on the calling thread  # noqa: BLE001
                box["error"] = exc
            finally:
                done.set()

        self._invoke_on_main(wrapper)
        if not done.wait(DB_CALL_TIMEOUT_S):
            raise HrError(
                "timed out waiting for the project db thread — is the main "
                "loop running?"
            )
        if "error" in box:
            raise box["error"]
        return box["result"]


__all__ = [
    "DB_CALL_TIMEOUT_S",
    "DEFAULT_HI_PERCENTILE",
    "DEFAULT_LO_PERCENTILE",
    "HrDiagramController",
    "HrDiagramListener",
    "HrDiagramSnapshot",
    "InvokeOnMain",
    "load_stack_image",
    "tonemap_gray",
]
