"""Transit-fit — fit a transit model to a project's folded lightcurve (the
``.scratch/transit-fit/spec.md`` package: "pure Python (lightcurve + params →
model + fit); view renders").

Strict two-layer split (the sandbox has no GTK, see docs/wiki/assumptions.md):

- **Pure layer** — this package, ticket 01 (the fit core): :mod:`.model` (the
  analytic Mandel-Agol-style trapezoid transit model in numpy — no ``batman``
  dependency — :func:`~.model.transit_model` with the circular phase distance,
  the ingress/egress ramps of :data:`~.model.INGRESS_FRACTION`, and
  :class:`~.model.TransitError`), :mod:`.fit` (:func:`~.fit.fit_transit` —
  :func:`scipy.optimize.least_squares` on the inverse-variance-weighted
  residuals with free params ``t0`` / ``depth`` / ``duration`` and an optional
  refit of the ``period`` (then from ``jd``), uncertainties from the covariance
  matrix ``inv(J^T J) * chi2/dof`` — returning the frozen
  :class:`~.fit.FitResult` with ``params`` / ``errors`` / ``residuals`` /
  ``chi2``; plus :func:`~.fit.fit_project_transit`, the store-backed wrapper
  that folds the project lightcurve with period-analysis and takes the period
  from the project config), and :mod:`.persist` (the fit parameters saved in
  the project config ``project.json`` under ``config['transit_fit']`` — NO
  store schema bump — via :func:`~.persist.save_fit` / :func:`~.persist.load_fit`,
  and the standalone JSON export :func:`~.persist.export_fit_json`). The pure
  layer imports **nothing** from ``gi``, ``link``, ``ui``, ``photometry``, or
  ``enhance`` — fully unit-testable headless; a subprocess test enforces the
  boundary. It depends only on numpy / scipy / stdlib, the sibling pure
  :mod:`seestar_controller.analysis` (period-analysis fold), and
  :mod:`seestar_controller.store` (project-store).
- **Widget layer** — later tickets (the transit plot overplotted on the folded
  lightcurve with residuals); those modules will import ``gi`` at module top
  and be re-exported here lazily (PEP 562 ``__getattr__``), so importing this
  package never imports ``gi``.

On the desktop, PyGObject is not a pip dependency — install the system
packages ``python3-gi gir1.2-gtk-4.0 gir1.2-adw-1`` (see the README).

Ticket 01 covers the pure fit core: model + least-squares fit + persistence.
"""

from .fit import (
    FitResult,
    TransitErrors,
    TransitParams,
    fit_project_transit,
    fit_transit,
)
from .model import (
    INGRESS_FRACTION,
    TransitError,
    transit_model,
)
from .persist import (
    FIT_CONFIG_KEY,
    export_fit_json,
    load_fit,
    save_fit,
)

__all__ = [
    "FIT_CONFIG_KEY",
    "INGRESS_FRACTION",
    "FitResult",
    "TransitError",
    "TransitErrors",
    "TransitParams",
    "export_fit_json",
    "fit_project_transit",
    "fit_transit",
    "load_fit",
    "save_fit",
    "transit_model",
]
