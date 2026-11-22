"""The plate-solve core — one frame (FITS path, FITS bytes, or data+header)
to a solved WCS.

Pure-Python (module boundary per ``.scratch/astrometry/spec.md``): no GTK
imports, no :mod:`seestar_controller.link` imports. Module level is stdlib +
:mod:`numpy` + the pure :mod:`seestar_controller.catalog` /
:mod:`seestar_controller.gaia` / :mod:`seestar_controller.store` packages +
the sibling pure :mod:`seestar_controller.astrometry.annotate` (the shared
RGB -> luminance helper); astropy, photutils and the ``solve-field``
subprocess enter only inside the functions that need them (a subprocess
boundary test enforces the module boundary).

Two solvers (``solver=``):

- ``"solve-field"`` — the local astrometry.net binary, found with
  :func:`shutil.which` and run as a SUBPROCESS (the only subprocess in the
  package): the frame is written to a temp FITS, ``solve-field --overwrite
  --out <prefix> <fits>`` runs with a timeout, and the ``<prefix>.wcs`` it
  writes is parsed with astropy. The matched-star count and RMS of the solve
  are parsed from what it leaves behind — the row count of the
  ``<prefix>.match`` FITS table and the "solved with N stars, RMS x arcsec"
  stdout line. A nonzero exit, a timeout, or a missing
  ``.wcs`` raises :class:`SolveError` with the solver's own message.
- ``"gaia"`` — the pure-Python fallback built on the merged gaia-crossmatch
  API: a PROVISIONAL TAN WCS is built from a center hint (``center_hint`` or
  the header's ``CRVAL``/``RA``-``DEC``) and a pixel-scale hint
  (``pixel_scale_hint`` or the header's ``CDELT``/``CD``, else
  :data:`DEFAULT_PIXEL_SCALE_ARCSEC`), :func:`~seestar_controller.gaia.detect.detect_sources`
  detects the stars on that provisional sky grid, the Gaia DR3 cone around
  the field (radius = field diagonal + margin, via
  :func:`~seestar_controller.gaia.detect.field_center_and_radius`) is queried
  through the injectable ``gaia_query`` (default
  :func:`~seestar_controller.gaia.query.query_gaia` — the only network
  boundary; tests inject a fake, so no test touches the network), and
  :func:`~seestar_controller.gaia.matching.crossmatch` pairs detections with
  Gaia stars. A generous first pass (radius = ``5 * pixel scale``) fits a
  WCS with :func:`astropy.wcs.utils.fit_wcs_from_points`; a second pass with
  the refined WCS and the tight ``match_radius_arcsec`` refits and yields
  the final matches + RMS. Fewer than ``min_matches`` matches or an RMS
  above ``rms_tolerance_arcsec`` raises :class:`SolveError`.

``solver="auto"`` (default) tries ``solve-field`` when the binary is on PATH
and falls back to the Gaia pattern match otherwise.

:func:`solve_frame` returns a :class:`SolveResult` (the astropy WCS, the WCS
as FITS cards, the solver, the center, the RMS/star count, and the matched
stars); :func:`write_wcs_to_fits` writes the cards into the FITS header and
:mod:`seestar_controller.astrometry.session` persists them in the project db.
"""

from __future__ import annotations

import math
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from seestar_controller.catalog.coords import angular_distance
from seestar_controller.gaia.detect import (
    DEFAULT_THRESHOLD_SIGMA,
    detect_sources,
    field_center_and_radius,
)
from seestar_controller.gaia.errors import GaiaError
from seestar_controller.gaia.matching import crossmatch
from seestar_controller.gaia.query import ARCSEC_PER_DEG, DEFAULT_G_LIMIT, query_gaia
from seestar_controller.store.frames import header_to_dict
from seestar_controller.store.project import utc_now_iso

from .annotate import _gray as _rgb_luminance
from .errors import ExportError, SolveError
from .wcs import (
    has_celestial_wcs,
    header_center,
    header_pixel_scale_arcsec,
    provisional_wcs_cards,
    wcs_to_cards,
)

#: Solver names accepted by :func:`solve_frame`.
SOLVER_AUTO = "auto"
SOLVER_SOLVE_FIELD = "solve-field"
SOLVER_GAIA = "gaia"
SOLVERS = (SOLVER_AUTO, SOLVER_SOLVE_FIELD, SOLVER_GAIA)

#: The astrometry.net binary name looked up with :func:`shutil.which`.
SOLVE_FIELD_BINARY = "solve-field"

#: The default pixel scale (arcsec/px) assumed when neither
#: ``pixel_scale_hint`` nor the header carries one (roughly a Seestar S50
#: sub at native resolution).
DEFAULT_PIXEL_SCALE_ARCSEC = 2.0

#: The default tight match radius (arcsec) of the refinement pass — the same
#: default as the gaia-crossmatch cross-match.
DEFAULT_MATCH_RADIUS_ARCSEC = 1.5

#: The first (generous) pass matches within this factor times the pixel scale,
#: so a slightly off center/scale hint still matches.
FIRST_PASS_RADIUS_FACTOR = 5.0

#: The default minimum number of matched stars required for a solve.
DEFAULT_MIN_MATCHES = 6

#: The default RMS tolerance (arcsec) of the accepted solve.
DEFAULT_RMS_TOLERANCE_ARCSEC = 1.0

#: The default ``solve-field`` subprocess timeout (seconds).
DEFAULT_SOLVE_FIELD_TIMEOUT_S = 120.0

#: A Gaia reference query: ``(center_ra_deg, center_dec_deg, radius_deg) ->
#: iterable of Gaia rows`` (anything with ``source_id``, ``ra_deg``,
#: ``dec_deg`` — e.g. :class:`~seestar_controller.gaia.query.GaiaStar`).
GaiaQuery = Callable[[float, float, float], Any]


@dataclass(frozen=True)
class StarMatch:
    """One matched star of a Gaia-pattern solve: the detected pixel position
    ``x``/``y`` (0-based), the matched Gaia sky position ``ra_deg``/
    ``dec_deg`` (J2000 degrees), and the residual ``separation_arcsec`` of the
    final WCS at that pixel."""

    x: float
    y: float
    ra_deg: float
    dec_deg: float
    separation_arcsec: float


@dataclass(frozen=True)
class SolveResult:
    """The result of :func:`solve_frame`.

    - ``wcs`` — the solved :class:`astropy.wcs.WCS` (built inside the solver,
      so this field only exists after a solve);
    - ``cards`` — the same WCS as a plain dict of FITS cards (what
      :func:`~.wcs.write_wcs_to_fits` writes into the header and what the
      project store persists);
    - ``solver`` — the solver that produced it (``"solve-field"`` or
      ``"gaia"``);
    - ``solved_at`` — the solve time as a UTC ISO-8601 string;
    - ``center_ra_deg`` / ``center_dec_deg`` — the solved field center: the
      sky position of the image center pixel (J2000 degrees, RA normalized
      to ``[0, 360)``);
    - ``n_stars`` — the number of matched stars used (for ``solve-field``:
      parsed from its ``.match`` output / stdout line, 0 when neither
      reports one);
    - ``rms_arcsec`` — the match RMS in arcsec (for ``solve-field``: parsed
      from its "RMS ... arcsec" stdout line, None when it reports none);
    - ``matches`` — the :class:`StarMatch` tuple of the final pass (empty for
      ``solve-field``);
    - ``rows`` — the final cross-match rows of the Gaia solver (the
      :class:`~seestar_controller.gaia.matching.CrossmatchRow` objects, ready
      for :func:`seestar_controller.store.crossmatch.save_crossmatch`);
      empty for ``solve-field``.
    """

    wcs: Any
    cards: dict[str, Any]
    solver: str
    solved_at: str
    center_ra_deg: float
    center_dec_deg: float
    n_stars: int
    rms_arcsec: float | None
    matches: tuple[StarMatch, ...]
    rows: tuple[Any, ...]


# --- input handling ---------------------------------------------------------------------


def _squeeze_2d(arr: np.ndarray, what: str) -> np.ndarray:
    """Squeeze ``arr`` to the 2-D image to solve. An RGB/RGBA frame — either
    channel-first ``(3|4, H, W)`` (a Seestar stack) or channel-last
    ``(H, W, 3|4)`` — is collapsed to a 2-D luminance float array with the
    same helper the annotate/export path uses
    (:func:`~seestar_controller.astrometry.annotate._gray`); anything that is
    not 2-D (or RGB/RGBA) raises :class:`SolveError`."""
    squeezed = np.squeeze(arr)
    if squeezed.ndim == 3:
        try:
            return _rgb_luminance(squeezed)
        except ExportError as exc:
            raise SolveError(
                f"{what} must be a 2-D image (or RGB/RGBA), got shape {arr.shape}"
            ) from exc
    if squeezed.ndim != 2:
        raise SolveError(f"{what} must be a 2-D image, got shape {arr.shape}")
    return squeezed


def _is_naxis_card(key: str) -> bool:
    upper = key.upper()
    return upper == "NAXIS" or (upper.startswith("NAXIS") and upper[5:].isdigit())


def _as_2d_with_header(
    raw: np.ndarray, header: dict[str, Any], what: str
) -> tuple[np.ndarray, dict[str, Any]]:
    """The solve input pair ``(data, header)``: :func:`_squeeze_2d` on the
    data plus the header side of the same conversion — when the raw frame was
    not 2-D (an RGB/RGBA frame collapsed to luminance, or a squeezed
    singleton axis), its ``NAXIS``/``NAXISn`` cards no longer describe the
    2-D image, so they are dropped (a stale ``NAXIS=3`` would make every WCS
    built from the header 3-dimensional)."""
    data = _squeeze_2d(raw, what)
    if raw.ndim != 2 and data.ndim == 2:
        header = {k: v for k, v in header.items() if not _is_naxis_card(k)}
    return data, header


def read_fits_bytes(fits_bytes: bytes) -> tuple[np.ndarray, dict[str, Any]]:
    """Read the primary HDU of ``fits_bytes`` into ``(data, header)`` —
    ``data`` a 2-D float array (an RGB/RGBA frame, channel-first
    ``(3|4, H, W)`` or channel-last ``(H, W, 3|4)``, is collapsed to
    luminance first and its stale ``NAXIS`` cards dropped), ``header`` a
    sanitized plain dict (lazy astropy import). Raises :class:`SolveError`
    when the bytes are not a readable FITS image (or the image is not
    2-D/RGB)."""
    import io

    from astropy.io import fits as pyfits

    try:
        with pyfits.open(io.BytesIO(fits_bytes), memmap=False) as hdul:
            raw = hdul[0].data
            header = header_to_dict(hdul[0].header)
    except Exception as exc:
        raise SolveError(f"not a readable FITS file: {exc}") from exc
    if raw is None:
        raise SolveError("the FITS file has no image data in its primary HDU")
    return _as_2d_with_header(np.asarray(raw), header, "the frame data")


def _read_fits_source(path: Path) -> tuple[np.ndarray, dict[str, Any]]:
    from astropy.io import fits as pyfits

    try:
        with pyfits.open(path, memmap=False) as hdul:
            raw = hdul[0].data
            header = header_to_dict(hdul[0].header)
    except Exception as exc:
        raise SolveError(f"cannot read the FITS file {path}: {exc}") from exc
    if raw is None:
        raise SolveError(f"the FITS file {path} has no image data in its primary HDU")
    return _as_2d_with_header(np.asarray(raw), header, f"the frame data of {path}")


def _resolve_input(
    source: str | Path | None,
    data: Any,
    header: Any,
) -> tuple[np.ndarray, dict[str, Any]]:
    if source is not None:
        if data is not None:
            raise ValueError("pass either source (a FITS path) or data+header, not both")
        if not isinstance(source, (str, Path)):
            raise TypeError(f"source must be a FITS path (str/Path), got {source!r}")
        return _read_fits_source(Path(source))
    if data is None:
        raise ValueError(
            "give a FITS path (source) or an image array (data) with an optional header"
        )
    hdr = {} if header is None else header_to_dict(header)
    return _as_2d_with_header(np.asarray(data), hdr, "data")


# --- parameter validation -----------------------------------------------------------------


def _check_solver(solver: Any) -> str:
    if solver not in SOLVERS:
        raise ValueError(f"solver must be one of {SOLVERS}, got {solver!r}")
    return str(solver)


def _positive_float(value: Any, *, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{what} must be a number, got {value!r}")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{what} must be a finite number > 0, got {value!r}")
    return result


def _check_min_matches(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"min_matches must be an int, got {value!r}")
    if value < 3:
        raise ValueError(f"min_matches must be >= 3 (a WCS fit needs 3 points), got {value!r}")
    return value


def _check_center_hint(value: Any) -> tuple[float, float] | None:
    if value is None:
        return None
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise ValueError(
            f"center_hint must be a (ra_deg, dec_deg) pair or None, got {value!r}"
        )
    ra_raw, dec_raw = value
    if (
        isinstance(ra_raw, bool)
        or not isinstance(ra_raw, (int, float))
        or isinstance(dec_raw, bool)
        or not isinstance(dec_raw, (int, float))
    ):
        raise TypeError(f"center_hint must be a (ra, dec) pair of numbers, got {value!r}")
    ra = float(ra_raw) % 360.0
    dec = float(dec_raw)
    if not math.isfinite(ra) or not math.isfinite(dec) or not -90.0 <= dec <= 90.0:
        raise ValueError(f"center_hint must be finite with dec in [-90, 90], got {value!r}")
    return ra, dec


# --- solve-field subprocess ---------------------------------------------------------------

#: Matches the matched-star count in a solve-field output line such as
#: "Field 1: solved with 24 stars" (case-insensitive; the LAST match wins —
#: it belongs to the final solution).
_SOLVE_FIELD_N_STARS_RE = re.compile(r"(\d+)\s+stars\b", re.IGNORECASE)

#: Matches the RMS in a solve-field output line such as "RMS 0.42 arcsec".
_SOLVE_FIELD_RMS_RE = re.compile(
    r"RMS\s*[:=]?\s*([0-9]*\.?[0-9]+(?:[eE][+-]?\d+)?)\s*arcsec\b", re.IGNORECASE
)


def _parse_solve_field_output(text: str) -> tuple[int | None, float | None]:
    """Parse ``(n_stars, rms_arcsec)`` from the solve-field stdout/stderr text
    — its "solved with N stars, RMS x arcsec" line (the last occurrence wins,
    it is the final solution). Either element is None when the output does
    not report it."""
    n_stars: int | None = None
    for match in _SOLVE_FIELD_N_STARS_RE.finditer(text):
        n_stars = int(match.group(1))
    rms_arcsec: float | None = None
    for match in _SOLVE_FIELD_RMS_RE.finditer(text):
        value = float(match.group(1))
        if math.isfinite(value):
            rms_arcsec = value
    return n_stars, rms_arcsec


def _match_file_rows(match_file: Path) -> int | None:
    """The matched-star count of the solve — the row count of the
    ``<out>.match`` FITS BINTABLE solve-field writes next to the solution
    (None when the file is missing or unreadable)."""
    if not match_file.exists():
        return None
    from astropy.io import fits as pyfits

    try:
        with pyfits.open(match_file, memmap=False) as hdul:
            for hdu in hdul[1:]:
                if getattr(hdu, "data", None) is not None:
                    return len(hdu.data)
    except Exception:  # noqa: BLE001 - an unreadable .match just means "no count"
        return None
    return None


def _solve_with_solve_field(
    data: np.ndarray,
    header: dict[str, Any],
    *,
    binary: str,
    timeout_s: float,
) -> SolveResult:
    """Run the local astrometry.net ``solve-field`` binary on the frame (the
    package's only subprocess) and parse the ``.wcs`` it writes plus the
    matched-star count / RMS from its ``.match`` table and stdout line."""
    from astropy.io import fits as pyfits
    from astropy.wcs import WCS

    with tempfile.TemporaryDirectory(prefix="seestar-solve-") as td:
        tdir = Path(td)
        fits_path = tdir / "input.fits"
        prefix = tdir / "solve"
        hdu = pyfits.PrimaryHDU(
            data=np.asarray(data, dtype=np.float64), header=pyfits.Header(dict(header))
        )
        hdu.writeto(fits_path)
        # solve-field resolves --out relative to the input file's directory even
        # when given an absolute path (0.93 joins dirname(input) + out), so pass
        # only the basename — the outputs land next to input.fits in tdir
        cmd = [binary, "--overwrite", "--out", prefix.name, str(fits_path)]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout_s, check=False
            )
        except subprocess.TimeoutExpired as exc:
            raise SolveError(
                f"solve-field timed out after {timeout_s:g}s ({' '.join(cmd)})"
            ) from exc
        except OSError as exc:
            raise SolveError(f"cannot run solve-field {binary!r}: {exc}") from exc
        if proc.returncode != 0:
            lines = (proc.stderr or proc.stdout or "").strip().splitlines()
            detail = lines[-1] if lines else "no output"
            raise SolveError(
                f"solve-field failed with exit code {proc.returncode}: {detail}"
            )
        wcs_file = tdir / "solve.wcs"
        if not wcs_file.exists():
            raise SolveError(
                "solve-field exited 0 but wrote no solve.wcs file — "
                "cannot read the solution"
            )
        try:
            wcs = WCS(pyfits.getheader(str(wcs_file)))
        except Exception as exc:
            raise SolveError(f"cannot parse the solve-field WCS file: {exc}") from exc
        # the matched-star count and RMS of the solve: the row count of the
        # <out>.match table solve-field wrote (authoritative) and its
        # "solved with N stars, RMS x arcsec" stdout line (fallback)
        n_stars, rms_arcsec = _parse_solve_field_output(
            "\n".join(part for part in (proc.stdout, proc.stderr) if part)
        )
        match_rows = _match_file_rows(tdir / "solve.match")
        if match_rows is not None:
            n_stars = match_rows
    if wcs.celestial is None:
        raise SolveError("solve-field wrote a non-celestial WCS — cannot use it")
    cards = wcs_to_cards(wcs)
    center = _image_center_sky(wcs, data.shape)
    return SolveResult(
        wcs=wcs,
        cards=cards,
        solver=SOLVER_SOLVE_FIELD,
        solved_at=utc_now_iso(),
        center_ra_deg=center[0],
        center_dec_deg=center[1],
        n_stars=n_stars if n_stars is not None else 0,
        rms_arcsec=rms_arcsec,
        matches=(),
        rows=(),
    )


# --- Gaia pattern-match fallback ----------------------------------------------------------


def _default_gaia_query(g_limit: float) -> GaiaQuery:
    """The default ``gaia_query``: a Gaia DR3 cone search via
    :func:`~seestar_controller.gaia.query.query_gaia` (the network boundary)."""

    def query(center_ra: float, center_dec: float, radius_deg: float) -> Any:
        return query_gaia(center_ra, center_dec, radius_deg, g_limit=g_limit)

    return query


def _matched_pairs(rows: Any, stars_by_id: Mapping[Any, Any]) -> list[tuple[float, float, float, float]]:
    """The ``(x, y, ra_deg, dec_deg)`` pairs of the matched cross-match rows —
    the detection's pixel position paired with its matched Gaia star's sky
    position (looked up by ``source_id``)."""
    pairs: list[tuple[float, float, float, float]] = []
    for row in rows:
        if not getattr(row, "matched", False):
            continue
        star = stars_by_id.get(getattr(row, "source_id", None))
        if star is None:
            continue
        pairs.append((float(row.x), float(row.y), float(star.ra_deg), float(star.dec_deg)))
    return pairs


def _image_center_sky(wcs: Any, shape: tuple[int, int]) -> tuple[float, float]:
    """The solved field center as ``(ra_deg, dec_deg)`` — the sky position of
    the image center pixel (0-based ``((nx-1)/2, (ny-1)/2)``), RA normalized
    to ``[0, 360)``."""
    ny, nx = int(shape[0]), int(shape[1])
    sky = wcs.pixel_to_world((nx - 1) / 2.0, (ny - 1) / 2.0)
    return float(sky.ra.deg) % 360.0, float(sky.dec.deg)


def _fit_wcs(pairs: list[tuple[float, float, float, float]]) -> Any:
    """Fit a TAN WCS from ``(x, y, ra, dec)`` pairs (0-based pixels — the
    astropy convention of :func:`astropy.wcs.utils.fit_wcs_from_points`)."""
    import astropy.units as u
    from astropy.coordinates import SkyCoord
    from astropy.wcs.utils import fit_wcs_from_points

    xs = np.asarray([p[0] for p in pairs], dtype=float)
    ys = np.asarray([p[1] for p in pairs], dtype=float)
    world = SkyCoord(
        np.asarray([p[2] for p in pairs], dtype=float) * u.deg,
        np.asarray([p[3] for p in pairs], dtype=float) * u.deg,
    )
    try:
        return fit_wcs_from_points((xs, ys), world, projection="TAN")
    except Exception as exc:
        raise SolveError(
            f"cannot fit a WCS from {len(pairs)} matched stars: {exc}"
        ) from exc


def _solve_with_gaia(
    data: np.ndarray,
    header: dict[str, Any],
    *,
    center_hint: tuple[float, float] | None,
    pixel_scale_hint: float | None,
    gaia_query: GaiaQuery,
    match_radius_arcsec: float,
    min_matches: int,
    rms_tolerance_arcsec: float,
    threshold: float,
    fwhm: float | None,
) -> SolveResult:
    """The pure-Python Gaia pattern match: provisional WCS -> detect -> Gaia
    cone query -> generous cross-match -> WCS fit -> refined re-match -> RMS."""
    shape = (int(data.shape[0]), int(data.shape[1]))
    center = center_hint if center_hint is not None else header_center(header)
    if center is None:
        raise SolveError(
            "cannot solve blind: no center hint — pass center_hint=(ra, dec) or "
            "a header with CRVAL/RA-DEC cards; the Gaia fallback needs an "
            "approximate field center"
        )
    scale = (
        pixel_scale_hint
        if pixel_scale_hint is not None
        else (header_pixel_scale_arcsec(header) or DEFAULT_PIXEL_SCALE_ARCSEC)
    )
    if has_celestial_wcs(header):
        # the header already carries a celestial WCS — use it as-is for the
        # detection pass (its CRVAL need not sit at the image center, so
        # rebuilding a provisional around CRVAL would shift the sky grid)
        prov_header = dict(header)
    else:
        provisional = provisional_wcs_cards(center[0], center[1], scale, shape)
        prov_header = {**header, **provisional}
    try:
        detections = detect_sources(data, prov_header, threshold=threshold, fwhm=fwhm)
        cone_center, radius_deg = field_center_and_radius(prov_header, shape)
    except GaiaError as exc:
        raise SolveError(f"Gaia fallback failed while detecting sources: {exc}") from exc
    if len(detections) < min_matches:
        raise SolveError(
            f"only {len(detections)} source(s) detected on the frame, need at "
            f"least {min_matches} — cannot solve"
        )
    try:
        stars = list(gaia_query(cone_center.ra_deg, cone_center.dec_deg, radius_deg))
    except Exception as exc:
        raise SolveError(f"Gaia reference query failed: {exc}") from exc
    stars_by_id = {
        star.source_id: star
        for star in stars
        if getattr(star, "source_id", None) is not None
    }
    first_radius = max(match_radius_arcsec, FIRST_PASS_RADIUS_FACTOR * scale)
    try:
        rows1 = crossmatch(detections, stars, radius_arcsec=first_radius)
    except ValueError as exc:
        raise SolveError(f"Gaia fallback cross-match failed: {exc}") from exc
    pairs1 = _matched_pairs(rows1, stars_by_id)
    if len(pairs1) < min_matches:
        raise SolveError(
            f"only {len(pairs1)} of {len(detections)} detected sources matched a "
            f"Gaia star within {first_radius:.1f} arcsec (need {min_matches}) — "
            "the center/scale hint may be off, or the field has no Gaia "
            "reference stars"
        )
    wcs1 = _fit_wcs(pairs1)
    refined_header = {**header, **wcs_to_cards(wcs1)}
    try:
        detections2 = detect_sources(data, refined_header, threshold=threshold, fwhm=fwhm)
        rows2 = crossmatch(detections2, stars, radius_arcsec=match_radius_arcsec)
    except (GaiaError, ValueError) as exc:
        raise SolveError(f"Gaia fallback refinement pass failed: {exc}") from exc
    pairs2 = _matched_pairs(rows2, stars_by_id)
    if len(pairs2) < min_matches:
        raise SolveError(
            f"only {len(pairs2)} of {len(detections2)} detected sources matched a "
            f"Gaia star within {match_radius_arcsec:g} arcsec after refinement "
            f"(need {min_matches}) — cannot solve"
        )
    wcs2 = _fit_wcs(pairs2)
    matches: list[StarMatch] = []
    sum_sq = 0.0
    for x, y, ra, dec in pairs2:
        sky = wcs2.pixel_to_world(x, y)  # origin=0, same convention as the fit
        sep = angular_distance((sky.ra.deg, sky.dec.deg), (ra, dec)) * ARCSEC_PER_DEG
        sum_sq += sep * sep
        matches.append(
            StarMatch(x=x, y=y, ra_deg=ra, dec_deg=dec, separation_arcsec=sep)
        )
    rms = math.sqrt(sum_sq / len(pairs2))
    if rms > rms_tolerance_arcsec:
        raise SolveError(
            f"solve RMS {rms:.2f} arcsec over {len(pairs2)} matches exceeds the "
            f"tolerance {rms_tolerance_arcsec:g} arcsec — not confident enough to "
            "accept the WCS"
        )
    center = _image_center_sky(wcs2, shape)
    return SolveResult(
        wcs=wcs2,
        cards=wcs_to_cards(wcs2),
        solver=SOLVER_GAIA,
        solved_at=utc_now_iso(),
        center_ra_deg=center[0],
        center_dec_deg=center[1],
        n_stars=len(pairs2),
        rms_arcsec=rms,
        matches=tuple(matches),
        rows=tuple(rows2),
    )


# --- the public solver --------------------------------------------------------------------


def solve_frame(
    source: str | Path | None = None,
    *,
    data: Any = None,
    header: Any = None,
    solver: str = SOLVER_AUTO,
    center_hint: tuple[float, float] | None = None,
    pixel_scale_hint: float | None = None,
    gaia_query: GaiaQuery | None = None,
    match_radius_arcsec: float = DEFAULT_MATCH_RADIUS_ARCSEC,
    min_matches: int = DEFAULT_MIN_MATCHES,
    rms_tolerance_arcsec: float = DEFAULT_RMS_TOLERANCE_ARCSEC,
    threshold: float = DEFAULT_THRESHOLD_SIGMA,
    fwhm: float | None = None,
    g_limit: float = DEFAULT_G_LIMIT,
    solve_field_timeout_s: float = DEFAULT_SOLVE_FIELD_TIMEOUT_S,
) -> SolveResult:
    """Plate-solve one frame and return the :class:`SolveResult`.

    - ``source`` — a path to a FITS file (read with astropy, lazy); pass
      EITHER ``source`` OR ``data`` (+ optional ``header``), not both;
    - ``data`` / ``header`` — the image array (2-D after squeezing; an
      RGB/RGBA frame in either channel layout is converted to a 2-D
      luminance array first) and its FITS header (a mapping of cards;
      None = empty header);
    - ``solver`` — ``"auto"`` (default: ``solve-field`` when the binary is on
      PATH, else the Gaia pattern match), ``"solve-field"``, or ``"gaia"``;
    - ``center_hint`` — ``(ra_deg, dec_deg)`` approximate field center for the
      Gaia fallback; None (default) takes the header's ``CRVAL`` or
      ``RA``/``DEC`` cards; a blind frame (no hint anywhere) raises
      :class:`SolveError`;
    - ``pixel_scale_hint`` — approximate pixel scale in arcsec/px for the Gaia
      fallback; None (default) takes the header's ``CDELT``/``CD``, else
      :data:`DEFAULT_PIXEL_SCALE_ARCSEC`;
    - ``gaia_query`` — the injectable Gaia reference query
      ``(center_ra, center_dec, radius_deg) -> rows``; None (default) uses
      :func:`~seestar_controller.gaia.query.query_gaia` (network!); tests
      inject a fake so no test touches the network;
    - ``match_radius_arcsec`` — the tight refinement-pass match radius
      (default 1.5 arcsec);
    - ``min_matches`` — the minimum matched-star count (default 6, must be
      >= 3);
    - ``rms_tolerance_arcsec`` — the maximum accepted match RMS (default
      1.0 arcsec);
    - ``threshold`` / ``fwhm`` — forwarded to
      :func:`~seestar_controller.gaia.detect.detect_sources` in the Gaia
      fallback (detection threshold in background sigmas, DAOStarFinder FWHM
      in px; None estimates it from the image);
    - ``g_limit`` — the Gaia G-magnitude limit of the default query;
    - ``solve_field_timeout_s`` — the ``solve-field`` subprocess timeout.

    Raises :class:`SolveError` with a clear message when the frame cannot be
    solved (no solver, solver failure, too few stars/matches, RMS too high,
    blind frame), :class:`ValueError` for bad parameters, :class:`TypeError`
    for structurally wrong arguments.
    """
    arr, hdr = _resolve_input(source, data, header)
    checked_solver = _check_solver(solver)
    checked_center = _check_center_hint(center_hint)
    if pixel_scale_hint is not None:
        pixel_scale_hint = _positive_float(pixel_scale_hint, what="pixel_scale_hint")
    if gaia_query is not None and not callable(gaia_query):
        raise TypeError(f"gaia_query must be a callable or None, got {gaia_query!r}")
    checked_radius = _positive_float(match_radius_arcsec, what="match_radius_arcsec")
    checked_min = _check_min_matches(min_matches)
    checked_rms_tol = _positive_float(rms_tolerance_arcsec, what="rms_tolerance_arcsec")
    checked_timeout = _positive_float(solve_field_timeout_s, what="solve_field_timeout_s")

    if checked_solver == SOLVER_AUTO:
        binary = shutil.which(SOLVE_FIELD_BINARY)
        checked_solver = SOLVER_SOLVE_FIELD if binary is not None else SOLVER_GAIA
    if checked_solver == SOLVER_SOLVE_FIELD:
        binary = shutil.which(SOLVE_FIELD_BINARY)
        if binary is None:
            raise SolveError(
                f"solver={SOLVER_SOLVE_FIELD!r} but no {SOLVE_FIELD_BINARY!r} binary "
                "was found on PATH (shutil.which) — install astrometry.net or use "
                f"solver={SOLVER_AUTO!r}/{SOLVER_GAIA!r}"
            )
        return _solve_with_solve_field(
            arr, hdr, binary=binary, timeout_s=checked_timeout
        )
    query = gaia_query if gaia_query is not None else _default_gaia_query(g_limit)
    return _solve_with_gaia(
        arr,
        hdr,
        center_hint=checked_center,
        pixel_scale_hint=pixel_scale_hint,
        gaia_query=query,
        match_radius_arcsec=checked_radius,
        min_matches=checked_min,
        rms_tolerance_arcsec=checked_rms_tol,
        threshold=threshold,
        fwhm=fwhm,
    )


def solve_fits_file(path: str | Path, **kwargs: Any) -> SolveResult:
    """Convenience wrapper: :func:`solve_frame` with ``source=path``."""
    return solve_frame(path, **kwargs)
