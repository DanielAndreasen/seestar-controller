"""Photometry — differential photometry of a target star against comparison
stars, measured from the sub-frames and saved into the project
(``.scratch/photometry/spec.md``).

Strict two-layer split (the sandbox has no GTK, see docs/wiki/assumptions.md):

- **Pure layer** — :mod:`.picking` (stdlib + numpy + store + catalog only;
  :mod:`astropy.wcs` lazy inside the WCS helpers): :func:`~.picking.pixel_to_sky`
  / :func:`~.picking.sky_to_pixel` (FITS WCS pixel <-> J2000, 0-based pixels,
  1-based WCS internally; missing WCS -> :class:`~.picking.PickingError`),
  :class:`~.picking.StarPickSpec` (role/x/y/coord/label), :class:`~.picking.ApertureParams`
  (aperture radius + sky annulus with defaults + validation),
  :class:`~.picking.PickSession` (the frozen snapshot: image + picks +
  aperture), and :class:`~.picking.PickController` — the app-level shared
  controller: :meth:`~.picking.PickController.pick_at` (click -> optional snap
  to the local brightness centroid -> WCS -> pick; one target with replace
  semantics, comps multiple), :meth:`~.picking.PickController.remove_pick` /
  :meth:`~.picking.PickController.set_role` / :meth:`~.picking.PickController.clear`,
  aperture settings, :meth:`~.picking.PickController.save` /
  :meth:`~.picking.PickController.load` into the project via
  :mod:`seestar_controller.store.stars`, following the shared
  :class:`~seestar_controller.enhance.viewer_controller.ViewerController`'s
  open stack, and listeners with frozen snapshots. All import **nothing** from
  ``gi`` — fully unit-testable headless; a subprocess test enforces the
  boundary.
- **Widget layer** — :mod:`.star_picker` (:class:`~.star_picker.StarPickerView`,
  the Photometry view's image + click-to-pick + aperture overlay + role
  selector + aperture spins + picks list + Save picks button). It imports
  ``gi`` at module top and is re-exported here **lazily** (PEP 562
  ``__getattr__``), so ``import seestar_controller.photometry`` never imports
  ``gi``.

On the desktop, PyGObject is not a pip dependency — install the system
packages ``python3-gi gir1.2-gtk-4.0 gir1.2-adw-1`` (see the README).

Ticket 01 covers star picking + persistence; later tickets add catalog assist
(02), the aperture-photometry core (03) and the run/results (04).
"""

from seestar_controller.store.stars import ROLE_COMP, ROLE_TARGET, ROLES

from .picking import (
    DEFAULT_APERTURE_RADIUS_PX,
    DEFAULT_SKY_IN_PX,
    DEFAULT_SKY_OUT_PX,
    SNAP_WINDOW_PX,
    ApertureParams,
    PickController,
    PickListener,
    PickSession,
    PickingError,
    StarPickSpec,
    pixel_to_sky,
    sky_to_pixel,
)

#: Widget-layer names re-exported lazily (importing the module pulls in
#: ``gi`` — only the GUI path does).
_WIDGET_EXPORTS = {
    "StarPickerView": "star_picker",
}


def __getattr__(name: str):
    if name in _WIDGET_EXPORTS:
        from importlib import import_module

        module = import_module(f".{_WIDGET_EXPORTS[name]}", __name__)
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "DEFAULT_APERTURE_RADIUS_PX",
    "DEFAULT_SKY_IN_PX",
    "DEFAULT_SKY_OUT_PX",
    "ROLE_COMP",
    "ROLE_TARGET",
    "ROLES",
    "SNAP_WINDOW_PX",
    "ApertureParams",
    "PickController",
    "PickListener",
    "PickSession",
    "PickingError",
    "StarPickSpec",
    "pixel_to_sky",
    "sky_to_pixel",
    # widget layer (lazy, imports gi):
    "StarPickerView",
]
