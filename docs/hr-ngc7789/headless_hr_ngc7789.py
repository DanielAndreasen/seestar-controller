#!/usr/bin/env python3
"""Headless driver: "From observation to H-R diagram" for the NGC7789 project.

Replicates, with the PURE layer only (no gi/GTK), the exact controller path
the GUI wires in ``seestar_controller/ui/views.py`` / ``ui/app.py``:

    Projects view  -> ProjectController.open_project / viewer.set_project
    Enhance view   -> ViewerController.open_stack(frame_id)          (image viewer)
                     SolveController.solve_open_stack()              (Solve panel button)
                     CrossmatchController.run_crossmatch()           (Cross-match button)
    H-R diagram    -> HrDiagramController.refresh() / select_stack()  (view Refresh button
                     HrDiagramView stack dropdown)
                     export PNG (render_diagram_png / render_stack_png, as the
                     view's _render_page/_render_stack_image do) + export CSV
                     (HrDiagramController.export_table)

Run from the repo worktree root:

    uv run python docs/hr-ngc7789/headless_hr_ngc7789.py

Outputs (PNG + CSV + run log) go to
``/home/daniel/SeestarProjects/ngc7789/reports/hr-ngc7789/`` and are copied
to ``docs/hr-ngc7789/artifacts/`` next to this script.

The script never patches library code: every failure is captured verbatim
(snapshot.error / traceback) into the run log and reported, see the guide's
ISSUES section.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PROJECT_PATH = Path("/home/daniel/SeestarProjects/ngc7789")
STACK_FRAME_ID = 3  # the stack frame (subs are 1, 2)
OUT_PROJECT = PROJECT_PATH / "reports" / "hr-ngc7789"
OUT_REPO = Path(__file__).resolve().parent / "artifacts"

# The working ESA Gaia TAP mirror, used ONLY to seed the repo's own disk
# cache (seestar_controller.catalog.cache.QueryCache under the default
# gaia cache dir) when the endpoint chain fails — the cache is the repo's
# documented offline path (query_gaia_cached serves it with no HTTP at all).
# The driver builds the ESA-dialect ADQL itself with UNQUOTED identifiers
# (the archive rejects FROM "gaiadr3.gaia_source" as one quoted identifier,
# HTTP 400 — see ISSUES 04 in the guide) and all 12 Gaia columns including
# the error fields; parse_gaia_response accepts the archive spellings via
# _COLUMN_ALIASES.
ESA_TAP_SYNC_URL = "https://gea.esac.esa.int/tap-server/tap/sync"

LOG: list[dict] = []


def log(step: str, **fields) -> None:
    entry = {"step": step, "t": round(time.time(), 3), **fields}
    LOG.append(entry)
    print(f"[{step}] " + " ".join(f"{k}={v}" for k, v in fields.items()), flush=True)


def _direct_invoke(callback) -> None:
    """The single-threaded stand-in for the app's ``GLib.idle_add``."""
    callback()


def main() -> int:
    from seestar_controller.astrometry.controller import SolveController
    from seestar_controller.catalog.cache import QueryCache
    from seestar_controller.enhance.viewer_controller import ViewerController
    from seestar_controller.gaia.detect import field_center_and_radius
    from seestar_controller.gaia.overlay import CrossmatchController
    from seestar_controller.gaia.query import (
        DEFAULT_MAXREC,
        DEFAULT_TTL_S,
        default_cache_dir,
        fetch_gaia_tap_json,
        gaia_cache_key,
        parse_gaia_response,
    )
    from seestar_controller.hr_diagram import plot as hr_plot
    from seestar_controller.hr_diagram.controller import HrDiagramController
    from seestar_controller.store import crossmatch as store_crossmatch
    from seestar_controller.store import wcs as store_wcs
    from seestar_controller.store.project import open_project

    OUT_PROJECT.mkdir(parents=True, exist_ok=True)
    OUT_REPO.mkdir(parents=True, exist_ok=True)

    # -- 1. open the project (Projects view -> ProjectController.open_project) --
    try:
        project = open_project(PROJECT_PATH)
        log("open_project", path=str(project.path), name=project.name, slug=project.slug)
    except Exception as exc:  # noqa: BLE001 - TRY-and-REPORT driver: capture any failure verbatim
        log("open_project FAILED", error=f"{type(exc).__name__}: {exc}",
            traceback=traceback.format_exc())
        return 1

    # -- 2. the shared viewer controller + the app's shared controllers, wired
    #       exactly like ui/app.py (viewer first, then the followers) ----------
    viewer = ViewerController()
    solve = SolveController(viewer_controller=viewer, invoke_on_main=_direct_invoke)
    cross = CrossmatchController(viewer_controller=viewer, invoke_on_main=_direct_invoke)
    hr = HrDiagramController(viewer_controller=viewer, invoke_on_main=_direct_invoke)
    # project_controller.attach_controllers pushes set_project() on every change
    viewer.set_project(project)
    solve.set_project(project)
    cross.set_project(project)
    hr.set_project(project)
    log("controllers wired", viewer_project=viewer.project.name if viewer.project else None,
        crossmatch_project=cross.project.name if cross.project else None,
        hr_project=hr.project.name if hr.project else None)

    # -- 3. open the stack frame in the Enhance viewer (frame id 3) ------------
    try:
        image = viewer.open_stack(STACK_FRAME_ID)
        cur = viewer.current_stack  # property (ViewerController.current_stack)
        log("open_stack", frame_id=cur.frame_id if cur else None,
            label=cur.label() if cur else None, shape=list(image.data.shape),
            kind=cur.kind if cur else None)
    except Exception as exc:  # noqa: BLE001 - TRY-and-REPORT driver: capture any failure verbatim
        log("open_stack FAILED", frame_id=STACK_FRAME_ID,
            error=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc())
        return 1

    # persisted WCS of the open stack (what the Solve panel shows on adopt)
    try:
        solved = store_wcs.load_frame_wcs(project, STACK_FRAME_ID)
        log("load_frame_wcs", frame_id=STACK_FRAME_ID,
            solver=solved.solver if solved else None,
            n_stars=solved.n_stars if solved else None,
            center=(solved.center_ra_deg, solved.center_dec_deg) if solved else None)
    except Exception as exc:  # noqa: BLE001 - TRY-and-REPORT driver: capture any failure verbatim
        log("load_frame_wcs FAILED", error=f"{type(exc).__name__}: {exc}")
    snap = solve.refresh_solved()
    log("solve.refresh_solved", status=snap.status, error=snap.error)

    # -- 4. Solve panel: the Solve button -> SolveController.solve_open_stack()
    t0 = time.time()
    snap = solve.solve_open_stack()  # exactly what astrometry/view.py _on_solve_clicked runs
    log("solve_open_stack", status=snap.status, error=snap.error,
        solver=snap.last_solver, n_stars=snap.last_n_stars,
        rms_arcsec=snap.last_rms_arcsec, secs=round(time.time() - t0, 1))

    # -- 5. Enhance cross-match panel: Cross-match button -> run_crossmatch() --
    log("crossmatch before", crossmatched=[s.frame_id for s in store_crossmatch.list_crossmatched_stacks(project)])
    t0 = time.time()
    snap = cross.run_crossmatch()  # exactly what gaia/overlay_view.py _on_run_clicked runs
    log("run_crossmatch (attempt 1, default cache)", status=snap.status, error=snap.error,
        stack_frame_id=snap.stack_frame_id, n_rows=len(snap.rows),
        radius_arcsec=snap.radius_arcsec, g_limit=snap.g_limit,
        secs=round(time.time() - t0, 1))
    attempt1_error = snap.error

    # -- 5b. offline path: seed the repo's gaia disk cache (QueryCache) from the
    #        working ESA mirror, then re-run the very same controller call ----
    if snap.error is not None:
        log("offline cache seeding: attempt", note="repo offline path = gaia disk cache; seeding it from the ESA Gaia archive mirror")
        try:
            import numpy as _np
            data = _np.asarray(image.data, dtype=_np.float32)
            if data.ndim == 3:
                data = data.mean(axis=2)
            header = dict(image.header)
            center, radius_deg = field_center_and_radius(header, data.shape)
            g_limit = cross.g_limit
            key = gaia_cache_key(center.ra_deg, center.dec_deg, radius_deg,
                                 g_limit=g_limit, maxrec=DEFAULT_MAXREC)
            digits = 4  # KEY_ROUND_DIGITS
            ra_r = round(center.ra_deg, digits)
            dec_r = round(center.dec_deg, digits)
            radius_r = round(radius_deg, digits) or radius_deg
            esa_columns = (
                "ra", "dec", "source_id", "phot_g_mean_mag", "phot_bp_mean_mag",
                "phot_rp_mean_mag", "parallax", "parallax_error",
                "pmra", "pmra_error", "pmdec", "pmdec_error",
            )
            adql_esa = (
                f"SELECT {', '.join(esa_columns)} FROM gaiadr3.gaia_source "
                f"WHERE CONTAINS(POINT('ICRS', ra, dec), "
                f"CIRCLE('ICRS', {ra_r!r}, {dec_r!r}, {radius_r!r})) = 1 "
                f"AND phot_g_mean_mag < {g_limit!r} ORDER BY phot_g_mean_mag"
            )
            log("seed query", center=(center.ra_deg, center.dec_deg), radius_deg=radius_deg,
                key=key, adql_esa=adql_esa)
            t0 = time.time()
            payload = fetch_gaia_tap_json(adql_esa, maxrec=DEFAULT_MAXREC, timeout=240,
                                          url=ESA_TAP_SYNC_URL)
            stars = parse_gaia_response(payload)
            rows = [
                {"source_id": s.source_id, "ra_deg": s.ra_deg, "dec_deg": s.dec_deg,
                 "g": s.g, "bp": s.bp, "rp": s.rp, "parallax": s.parallax,
                 "parallax_error": s.parallax_error, "pmra": s.pmra,
                 "pmra_error": s.pmra_error, "pmdec": s.pmdec,
                 "pmdec_error": s.pmdec_error}
                for s in stars
            ]
            cache = QueryCache(cache_dir=default_cache_dir(), ttl=DEFAULT_TTL_S)
            cache_path = cache.set(key, rows)
            log("cache seeded", n_stars=len(rows), cache_file=str(cache_path),
                secs=round(time.time() - t0, 1))
            t0 = time.time()
            snap = cross.run_crossmatch()  # same GUI call again — now served from disk cache
            log("run_crossmatch (attempt 2, seeded cache)", status=snap.status, error=snap.error,
                stack_frame_id=snap.stack_frame_id, n_rows=len(snap.rows),
                matched=sum(1 for r in snap.rows if r.matched),
                secs=round(time.time() - t0, 1))
        except Exception as exc:  # noqa: BLE001 - TRY-and-REPORT driver: capture any failure verbatim
            log("offline cache seeding FAILED", error=f"{type(exc).__name__}: {exc}",
                traceback=traceback.format_exc())
            snap = cross.snapshot
    log("crossmatch after", crossmatched=[(s.frame_id, s.matched_count, s.row_count)
                                          for s in store_crossmatch.list_crossmatched_stacks(project)])

    # -- 6. H-R diagram view: Refresh button -> HrDiagramController.refresh(),
    #       stack dropdown -> select_stack(frame_id), Refresh again -----------
    try:
        snap = hr.refresh()
        log("hr.refresh (1)", stacks=[s.frame_id for s in snap.stacks],
            frame_id=snap.frame_id, status=snap.status, error=snap.error)
        if snap.frame_id != STACK_FRAME_ID:
            snap = hr.select_stack(STACK_FRAME_ID)  # the view's stack dropdown
            log("hr.select_stack", frame_id=snap.frame_id, stack_label=snap.stack_label,
                status=snap.status, error=snap.error)
        snap = hr.refresh()
        log("hr.refresh (2)", frame_id=snap.frame_id, stack_label=snap.stack_label,
            n_table=len(snap.table), n_cmd=len(snap.cmd), n_hr=len(snap.hr),
            n_overlay=len(snap.overlay), params=snap.params,
            image_size=snap.image_size, status=snap.status, error=snap.error)
    except Exception as exc:  # noqa: BLE001 - TRY-and-REPORT driver: capture any failure verbatim
        log("hr FAILED", error=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc())
        snap = hr.snapshot

    # -- 7. exports, exactly what the H-R view's Export PNG / Export CSV do ----
    exports: dict[str, str] = {}
    try:
        points_by_kind = {hr_plot.CMD: snap.cmd, hr_plot.HR: snap.hr}
        for kind, points in points_by_kind.items():
            name = f"{kind}.png"
            if points:
                # mirror hr_diagram/view.py _render_page: full_window + DiagramModel
                # at PLOT_WIDTH_PX/PLOT_HEIGHT_PX (= DEFAULT_WIDTH_PX/HEIGHT_PX)
                window = hr_plot.full_window(kind, points)
                model = hr_plot.DiagramModel(
                    kind=kind, points=points, overlay=snap.overlay,
                    show_errors=False, highlighted=None,
                    title=snap.stack_label or "",
                )
                png = hr_plot.render_diagram_png(
                    model, window,
                    width_px=hr_plot.DEFAULT_WIDTH_PX, height_px=hr_plot.DEFAULT_HEIGHT_PX,
                )
                for out in (OUT_PROJECT / name, OUT_REPO / name):
                    out.write_bytes(png)
                exports[name] = str(OUT_PROJECT / name)
                log("export png", kind=kind, n_points=len(points), path=str(OUT_PROJECT / name))
            else:
                # the GUI view renders NOTHING for an empty page (_render_page leaves
                # png b"" and export_png raises HrError) — still render a diagnostic
                # PNG here so the empty H-R page is visible in the artifacts
                window = hr_plot.full_window(kind, points)
                model = hr_plot.DiagramModel(kind=kind, points=points, overlay=snap.overlay,
                                             show_errors=False, highlighted=None,
                                             title=(snap.stack_label or "") + " (EMPTY)")
                png = hr_plot.render_diagram_png(model, window)
                for out in (OUT_PROJECT / name, OUT_REPO / name):
                    out.write_bytes(png)
                exports[name] = str(OUT_PROJECT / name)
                log("export png (EMPTY page — GUI export_png would raise HrError)",
                    kind=kind, n_points=0, path=str(OUT_PROJECT / name))
        if snap.image is not None:
            png = hr_plot.render_stack_png(snap.image, snap.cmd, None)  # _render_stack_image
            for out in (OUT_PROJECT / "stack-marked.png", OUT_REPO / "stack-marked.png"):
                out.write_bytes(png)
            exports["stack-marked.png"] = str(OUT_PROJECT / "stack-marked.png")
            log("export png", kind="stack-marked", path=str(OUT_PROJECT / "stack-marked.png"))
        csv_path = hr.export_table(OUT_PROJECT / "crossmatch-table.csv")  # view export_csv
        shutil.copy2(csv_path, OUT_REPO / "crossmatch-table.csv")
        exports["crossmatch-table.csv"] = str(csv_path)
        log("export csv", path=str(csv_path), n_rows=len(snap.table))
    except Exception as exc:  # noqa: BLE001 - TRY-and-REPORT driver: capture any failure verbatim
        log("export FAILED", error=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc())
    if cross.snapshot.image is not None:
        try:
            cross.snapshot.image.save(OUT_PROJECT / "crossmatch-overlay.png")
            shutil.copy2(OUT_PROJECT / "crossmatch-overlay.png", OUT_REPO / "crossmatch-overlay.png")
            exports["crossmatch-overlay.png"] = str(OUT_PROJECT / "crossmatch-overlay.png")
            log("export png", kind="crossmatch-overlay", path=str(OUT_PROJECT / "crossmatch-overlay.png"))
        except Exception as exc:  # noqa: BLE001 - TRY-and-REPORT driver: capture any failure verbatim
            log("overlay export FAILED", error=f"{type(exc).__name__}: {exc}")

    log("done", exports=exports, attempt1_error=attempt1_error)
    log_path = OUT_PROJECT / "run-log.json"
    log_path.write_text(json.dumps(LOG, indent=2, default=str) + "\n")
    shutil.copy2(log_path, OUT_REPO / "run-log.json")
    print(f"run log: {log_path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
