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
  semantics, comps multiple), :meth:`~.picking.PickController.add_at_sky`
  (the inverse: sky -> pixel via the WCS -> pick, ticket 02),
  :meth:`~.picking.PickController.remove_pick` /
  :meth:`~.picking.PickController.set_role` / :meth:`~.picking.PickController.clear`,
  aperture settings, :meth:`~.picking.PickController.save` /
  :meth:`~.picking.PickController.load` into the project via
  :mod:`seestar_controller.store.stars`, following the shared
  :class:`~seestar_controller.enhance.viewer_controller.ViewerController`'s
  open stack, and listeners with frozen snapshots. All import **nothing** from
  ``gi`` — fully unit-testable headless; a subprocess test enforces the
  boundary.
- **Pure layer** — :mod:`.suggest` (ticket 02, catalog assist for comps):
  :class:`~.suggest.SuggestionController` lists the standard stars near the
  target pick via an injectable catalog callable (default: the lazy
  :class:`~seestar_controller.catalog.facade.Catalog`), re-filtered against the
  configured radius and sorted nearest-first, and adds one as a comp pick via
  :meth:`~.picking.PickController.add_at_sky`. Frozen
  :class:`~.suggest.CompSuggestion` / :class:`~.suggest.SuggestionSnapshot`
  snapshots, listener pattern like :class:`~.picking.PickController`, and
  catalog failures surfaced as a clean snapshot ``error`` (never a traceback).
  The pure layer is settings-agnostic: the app injects the configured radius
  (see :func:`seestar_controller.ui.settings.comp_search_radius_deg`). Also
  imports **nothing** from ``gi``.
- **Pure layer** — :mod:`.measure` (ticket 03, the aperture-photometry core):
  :func:`~.measure.measure_frame_data` (raw array + header + picks →
  :class:`~.measure.FrameMeasurement`: photutils circular aperture + sky
  annulus per pick, comp-ensemble mean, differential mag = target − ensemble,
  error = ``sqrt(target_err² + mean(comp_err²))``, JD from the FITS
  ``DATE-OBS``), :func:`~.measure.measure_frame` (one stored frame →
  :class:`~seestar_controller.store.measurements.Measurement` via the store)
  and :func:`~.measure.measure_project` (all sub-frames of a project →
  :class:`~.measure.MeasureReport` with per-frame skips). The pure math
  (:func:`~.measure.flux_to_mag`, :func:`~.measure.ensemble_mag`,
  :func:`~.measure.differential_mag_err`, ...) is unit-tested on its own;
  errors are :class:`~.measure.MeasurementError` /
  :class:`~.measure.FrameSkippedError` (subclasses of
  :class:`~.picking.PhotometryError`, distinct from the store's
  ``MeasurementError``). Also imports **nothing** from ``gi`` (photutils and
  astropy are lazy inside the functions).
- **Pure layer** — :mod:`.run` (ticket 04, the run + results controller):
  :class:`~.run.RunController` — the app-level shared controller bound to the
  shared :class:`~.picking.PickController`: :meth:`~.run.RunController.run_session`
  measures ALL sub-frames of one session (stacks/enhanced never) on the
  calling thread — safe from a worker thread, with store writes marshalled to
  the db-owning thread — appending each result via the store's
  ``add_measurement`` (jd, mag, mag_err), collecting skipped frames, reporting
  progress via an ``on_progress(done, total, frame)`` callback, and returning
  a frozen :class:`~.run.RunReport`; the results accessor
  (:meth:`~.run.RunController.sessions` /
  :meth:`~.run.RunController.lightcurve_points` /
  :meth:`~.run.RunController.set_excluded` for outlier flagging) is exposed on
  every frozen :class:`~.run.RunSnapshot` handed to the listeners. Errors are
  :class:`~.run.RunError` (a :class:`~.picking.PhotometryError`). Also imports
  **nothing** from ``gi``.
- **Widget layer** — :mod:`.star_picker` (:class:`~.star_picker.StarPickerView`,
  the Photometry view's image + click-to-pick + aperture overlay + role
  selector + aperture spins + picks list + Save picks button),
  :mod:`.suggestions_panel` (:class:`~.suggestions_panel.SuggestionsPanel`,
  the catalog-assist list next to the image: Search button, one row per
  suggestion with an Add button, clean error status), and
  :mod:`.results_view` (:class:`~.results_view.ResultsPanel`, ticket 04: the
  session chooser + Run button + progress, the per-frame measurements table
  with outlier checkboxes, and the mag-vs-jd scatter plot on a
  :class:`Gtk.DrawingArea`). They import ``gi`` at
  module top and are re-exported here **lazily** (PEP 562 ``__getattr__``), so
  ``import seestar_controller.photometry`` never imports ``gi``.

On the desktop, PyGObject is not a pip dependency — install the system
packages ``python3-gi gir1.2-gtk-4.0 gir1.2-adw-1`` (see the README).

Ticket 01 covers star picking + persistence; ticket 02 catalog assist for
comps; ticket 03 the aperture-photometry core; ticket 04 the run/results
view.
"""

from seestar_controller.store.stars import ROLE_COMP, ROLE_TARGET, ROLES

from .picking import (
    DEFAULT_APERTURE_RADIUS_PX,
    DEFAULT_SKY_IN_PX,
    DEFAULT_SKY_OUT_PX,
    SNAP_WINDOW_PX,
    ApertureParams,
    PhotometryError,
    PickController,
    PickListener,
    PickSession,
    PickingError,
    StarPickSpec,
    pixel_to_sky,
    sky_to_pixel,
)
from .measure import (
    MAG_ERR_COEFF,
    FrameMeasurement,
    FrameSkippedError,
    MeasureReport,
    MeasurementError,
    SkippedFrame,
    StarMeasurement,
    differential_mag,
    differential_mag_err,
    ensemble_mag,
    ensemble_mag_err,
    flux_err_to_mag_err,
    flux_to_mag,
    measure_frame,
    measure_frame_data,
    measure_project,
    measure_star,
)
from .run import (
    DB_CALL_TIMEOUT_S,
    ProgressCallback,
    RunController,
    RunError,
    RunListener,
    RunProgress,
    RunReport,
    RunSnapshot,
)
from .suggest import (
    DEFAULT_COMP_SEARCH_RADIUS_DEG,
    MAX_COMP_SEARCH_RADIUS_DEG,
    CompSuggestion,
    LookupFn,
    SearchRunner,
    SuggestListener,
    SuggestionController,
    SuggestionError,
    SuggestionSnapshot,
    default_standards_lookup,
)

#: Widget-layer names re-exported lazily (importing the module pulls in
#: ``gi`` — only the GUI path does).
_WIDGET_EXPORTS = {
    "ResultsPanel": "results_view",
    "StarPickerView": "star_picker",
    "SuggestionsPanel": "suggestions_panel",
}


def __getattr__(name: str):
    if name in _WIDGET_EXPORTS:
        from importlib import import_module

        module = import_module(f".{_WIDGET_EXPORTS[name]}", __name__)
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "DB_CALL_TIMEOUT_S",
    "DEFAULT_APERTURE_RADIUS_PX",
    "DEFAULT_COMP_SEARCH_RADIUS_DEG",
    "DEFAULT_SKY_IN_PX",
    "DEFAULT_SKY_OUT_PX",
    "MAG_ERR_COEFF",
    "MAX_COMP_SEARCH_RADIUS_DEG",
    "ROLE_COMP",
    "ROLE_TARGET",
    "ROLES",
    "SNAP_WINDOW_PX",
    "ApertureParams",
    "CompSuggestion",
    "FrameMeasurement",
    "FrameSkippedError",
    "LookupFn",
    "MeasureReport",
    "MeasurementError",
    "PhotometryError",
    "PickController",
    "PickListener",
    "PickSession",
    "PickingError",
    "ProgressCallback",
    "RunController",
    "RunError",
    "RunListener",
    "RunProgress",
    "RunReport",
    "RunSnapshot",
    "SearchRunner",
    "SkippedFrame",
    "StarMeasurement",
    "StarPickSpec",
    "SuggestListener",
    "SuggestionController",
    "SuggestionError",
    "SuggestionSnapshot",
    "default_standards_lookup",
    "differential_mag",
    "differential_mag_err",
    "ensemble_mag",
    "ensemble_mag_err",
    "flux_err_to_mag_err",
    "flux_to_mag",
    "measure_frame",
    "measure_frame_data",
    "measure_project",
    "measure_star",
    "pixel_to_sky",
    "sky_to_pixel",
    # widget layer (lazy, imports gi):
    "ResultsPanel",
    "StarPickerView",
    "SuggestionsPanel",
]
