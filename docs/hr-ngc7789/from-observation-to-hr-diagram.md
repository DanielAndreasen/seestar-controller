# From observation to H-R diagram

Step-by-step guide for the `seestar-controller` GTK4 app (and its headless,
PURE-layer equivalent) that takes a captured Seestar session all the way to a
colour–magnitude diagram (CMD) and an H-R diagram of a star cluster, using the
existing project `/home/daniel/SeestarProjects/ngc7789` (session
`20261120T213247325993Z`, frames 1/2 = subs, frame 3 = stack, target "M 31")
as the worked example.

Two ways to follow the pipeline, both using **exactly the same controller
logic**:

* **GUI** — the views and buttons described in §1–§7.
* **Headless** — `uv run python docs/hr-ngc7789/headless_hr_ngc7789.py` from a
  repo worktree root, which drives the same controller classes directly
  (§8). No `gi`/GTK is imported.

The GUI wires everything in `src/seestar_controller/ui/app.py` around a single
shared `ViewerController` (`app.py:1251`, also exposed as `viewer_controller`
at `app.py:423`):

| Controller | Constructed at | Depends on |
|---|---|---|
| `ProjectController` | `app.py:590` | settings state |
| `ObserveController` | `app.py:1240` | connection / queue / capture |
| `ViewerController` | `app.py:1251` | — (shared) |
| `CrossmatchController` | `app.py:524-525` | `viewer_controller=self._viewer_controller, invoke_on_main=...` |
| `HrDiagramController` | `app.py:537-538` | same |
| `SolveController` | `app.py:563-564` | same |

All controllers take an `invoke_on_main` callable; when driving them
single-threaded headlessly, a direct callable (`lambda fn: fn()`) is exactly
what `observing/observe_controller.py:222` (`_direct_invoke`) does.

---

## 1. Observe / capture (Observe view)

* Connect the scope via the connection panel (`ConnectionController`).
* Build the night plan in the queue editor
  (`observing/queue_editor.py`): type a target, **Resolve** it via the catalog
  (`queue_editor.py:165`), then **Add to queue** (`queue_editor.py:167`).
* In the Observe view (`observing/observe_view.py`, `ObserveView`) press
  **Start night** (`observe_view.py:178-182` → `_on_start_clicked` at :448 →
  `ObserveController.start_night`, `observing/observe_controller.py:562`).
  The `NightExecutor` runs the `SessionCapture`, writing sub frames into a new
  session directory of the current project. **Pause** (:533), **Skip**,
  **Stop**, **Park** (:187-191) control the night.
* Stacking subs / mosaics / session report happen from the *Stored data* panel
  of the app window (`ui/views.py:1478-1501`: **Stack subs**, **Stitch mosaic**,
  **Session report**).

For NGC7789 this already happened: session `20261120T213247325993Z` with
frames 1, 2 (subs) and 3 (stack, `stacks/20261120T213247325993Z/2461365.398364.fits`,
1920×1080, target `M 31`).

## 2. Open the project

* **Projects view** (`ui/views.py:1073`, `ProjectsView`): each project row has
  an **Open** button (`views.py:1612`) → `_on_open_clicked` (:1668) →
  `ProjectController.open_project(path)`; the current project is pushed to the
  shared viewer via `viewer.set_project(project)`.

Headless: `store.open_project('/home/daniel/SeestarProjects/ngc7789')`, then
`viewer_controller.set_project(project)` (`enhance/viewer_controller.py:141`).

## 3. Open the stack in the Enhance view

* In the **Stored data** panel, click the frame row for the stack
  (`ui/views.py:1770` → `_open_frame_in_viewer` → `viewer.open_stack(frame_id)`,
  `enhance/viewer_controller.py:215`) and the app switches to the **Enhance**
  view. The Enhance view (`ui/views.py:505-621`) shows the image plus, below
  it, the cross-match overlay (`CrossmatchOverlayView`, `views.py:581`) and
  the **Solve** panel (`SolvePanel`, `views.py:587`).

Headless: `viewer.open_stack(3)` — the open stack is what the Solve,
Cross-match and H-R controllers all operate on.

## 4. Solve (Enhance view → Solve panel)

* Press the **Solve** button in the `SolvePanel`
  (`astrometry/view.py:195-205`, `_on_solve_clicked`) →
  `SolveController.solve_open_stack()` (`astrometry/controller.py:313`), run on
  a worker thread. With `solver="auto"` this uses `solve-field`
  (`/usr/bin/solve-field` with the Tycho-2 indexes), writes the WCS into the
  FITS header and persists it via `store.wcs.save_frame_wcs` /
  `load_frame_wcs`. **Export annotated PNG** writes the solved image with the
  catalogue overlay (`SolvePanel.export_annotated_to`,
  `astrometry/view.py:190-192`).

Headless: `snapshot = solve_controller.solve_open_stack()`; check
`snapshot.status` / `snapshot.error`. For NGC7789 all three frames already had
a persisted WCS, so re-solving just re-persisted it.

## 5. Cross-match against Gaia (Enhance view → Cross-match overlay)

* In the `CrossmatchOverlayView` (`gaia/overlay_view.py:75`), set radius and
  G-magnitude limit (spin buttons, `_on_radius_changed` :221+) and press the
  **Run** button (`_on_run_clicked`, `gaia/overlay_view.py:217-218`) →
  `CrossmatchController.run_crossmatch()` (`gaia/overlay.py:644`).
  The controller reads the open stack from the viewer controller, loads its
  WCS (`store.wcs.load_frame_wcs`), queries Gaia DR3 by cone search
  (`gaia.query.query_gaia_cached` — disk cache first
  `~/.config/seestar-controller/cache/gaia/`, then the VizieR TAP endpoint),
  matches detected stars within the radius (`gaia/matching.py`), and persists
  the result with `store.crossmatch` (visible afterwards via
  `store.crossmatch.list_crossmatched_stacks(project)` → e.g. `[(3, 47, 104)]`
  = frame 3, 47 matched of 104 detected).

## 6. Open the H-R diagram view

* Switch to the **H-R diagram** view. `HrDiagramView`
  (`hr_diagram/view.py:179+`) has:
  * a **stack dropdown** (`view.py:181`, `_on_stack_selected` :467 →
    `HrDiagramController.select_stack(frame_id)`, `hr_diagram/controller.py:450`)
    listing cross-matched stacks from `store.crossmatch.list_crossmatched_stacks`,
  * a **Refresh** button (`view.py:186`, `_on_refresh_clicked` :471 →
    `HrDiagramController.refresh()`, `hr_diagram/controller.py:521`), which
    recomputes CMD/H-R points from the persisted cross-match
    (`hr_diagram/core.py`: `cmd_points`, `hr_points`, `membership_filter`),
  * a `Gtk.Stack` with the **CMD** and **H-R** pages (plus the stack image with
    matched stars marked),
  * an **Error bars** toggle (`view.py:191-192`), **Reset zoom** (:198),
  * **Export PNG** (`view.py:203` → `HrDiagramView.export_png`, :443-456) and
    **Export CSV** (`view.py:207` → `export_csv` :458-460 →
    `HrDiagramController.export_table(path)`, `hr_diagram/controller.py:535`).
* The H-R page is `M_G` vs effective temperature (derived from Gaia BP−RP);
  rows are kept only when `parallax` and its error pass the parallax-SNR cut in
  `hr_diagram/core.py:hr_points` (:366, checks at :399-407). An isochrone
  overlay is drawn when isochrone parameters are persisted for the project
  (`store.isochrone.set_isochrone_params(project, age=…, feh=…, dist_mod=…)`);
  none are set for NGC7789, so the overlay is the bundled fallback grid.

## 7. Read the diagrams and export

* CMD page: G magnitude vs BP−RP colour for all matched stars; membership
  flag (parallax/proper-motion sample) is shown and included in the CSV.
* H-R page: absolute G magnitude vs Teff for high-parallax-SNR members.
* **Export PNG** saves the current page (720×560 px, the view's
  `PLOT_WIDTH_PX`/`PLOT_HEIGHT_PX`); **Export CSV** saves the full cross-match
  table (all detected stars, matched or not, with Gaia photometry, parallax,
  proper motions, separation, membership).

## 8. Headless equivalent (PURE layer, same controllers)

From a repo worktree root (branch `wt/hr-ngc7789`):

```bash
uv run python docs/hr-ngc7789/headless_hr_ngc7789.py
```

The script (`docs/hr-ngc7789/headless_hr_ngc7789.py`) does, in order:

```python
from seestar_controller import store
from seestar_controller.enhance.viewer_controller import ViewerController
from seestar_controller.astrometry.controller import SolveController
from seestar_controller.gaia.overlay import CrossmatchController
from seestar_controller.hr_diagram.controller import HrDiagramController
from seestar_controller.hr_diagram.plot import full_window, DiagramModel, render_diagram_png, render_stack_png

invoke = lambda fn: fn()                      # single-threaded invoke_on_main
project = store.open_project('/home/daniel/SeestarProjects/ngc7789')

viewer = ViewerController()
viewer.set_project(project)                   # as app.py pushes the open project
viewer.open_stack(3)                          # the stack frame = open stack

solve = SolveController(viewer_controller=viewer, invoke_on_main=invoke)
solve.refresh_solved()                        # status from persisted WCS
snap = solve.solve_open_stack()               # Solve button equivalent

xmatch = CrossmatchController(viewer_controller=viewer, invoke_on_main=invoke)
xsnap = xmatch.run_crossmatch()               # Cross-match button equivalent
# -> store.crossmatch.list_crossmatched_stacks(project) == [(3, 47, 104)]

hr = HrDiagramController(viewer_controller=viewer, invoke_on_main=invoke)
hr.refresh()                                  # stack list
hr.select_stack(3)                            # stack dropdown
hsnap = hr.refresh()                          # Refresh button
# hsnap.cmd_points / hsnap.hr_points / hsnap.table

# render exactly like HrDiagramView._render_page (hr_diagram/view.py:733+)
window = full_window(kind, points)
model = DiagramModel(kind, points, hsnap.overlay, show_errors=False, highlighted=None, title=label)
png = render_diagram_png(model, window, width_px=720, height_px=560)
hr.export_table('.../crossmatch-table.csv')   # Export CSV equivalent
```

Outputs land in the project's `reports/hr-ngc7789/` directory and are copied
next to the script into `docs/hr-ngc7789/artifacts/`:

| Artifact | Meaning |
|---|---|
| `/home/daniel/SeestarProjects/ngc7789/reports/hr-ngc7789/cmd.png` | CMD page, 46 plotted points (720×560) |
| `.../hr-ngc7789/hr.png` | H-R page render (currently an empty page — see ISSUE 2) |
| `.../hr-ngc7789/stack-marked.png` | stack image with matched stars marked |
| `.../hr-ngc7789/crossmatch-overlay.png` | cross-match overlay render |
| `.../hr-ngc7789/crossmatch-table.csv` | full cross-match table (header + 104 rows) |
| `.../hr-ngc7789/run-log.json` | statuses, errors and timings of the whole run |

Repo-side copies: `docs/hr-ngc7789/artifacts/` (same six files).

## 9. ISSUES (observed, NOT fixed — report only)

### ISSUE 1 — Gaia cross-match hard-fails when the hardcoded VizieR TAP endpoint is down; no mirror fallback

* **Exact error** (captured in `run-log.json`, first `run_crossmatch()` attempt):
  `GaiaUnavailableError: Gaia DR3 cone search around (ra=0.9653, dec=55.3454) radius=0.7765 deg failed and no cached result is available: could not reach VizieR Gaia TAP at https://tapvizier.cds.unistra.fr/TAPVizieR/TAP/sync: <urlopen error timed out>`
* **file:line**: URL constant `src/seestar_controller/gaia/query.py:69`
  (`VIZIER_TAP_SYNC_URL`, with the comment at :66-68 noting the endpoint has
  been down since 2026-10-29); raised at `query.py:333` (`fetch_gaia_tap_json`)
  and `query.py:600` (`query_gaia_cached`); surfaced to the UI via
  `gaia/overlay.py:698-701` (`run_crossmatch` at :644 catches and records
  `snapshot.error`).
* **Repro**: clear/empty the Gaia disk cache
  (`~/.config/seestar-controller/cache/gaia/`), then run
  `CrossmatchController.run_crossmatch()` on a solved stack — first attempt
  fails as above because every HTTP path goes to the dead VizieR URL.
* **Root cause**: `query_gaia` / `query_gaia_cached` never pass a `url=` to
  `fetch_gaia_tap_json`, even though that function already accepts one. The
  working ESA mirror `https://gea.esac.esa.int/tap-server/tap/sync` (verified
  HTTP 200 during this run) is unreachable through the public API.
* **Workaround used here (no code change)**: seed the repo's own disk cache
  (`QueryCache` at the default cache dir, key
  `gaia_cache_key(ra, dec, radius)`, `gaia/query.py:94` +
  `gaia_cache_key`) with Gaia DR3 rows fetched from the ESA mirror using
  `build_cone_query` output translated to archive column names
  (`parse_gaia_response` accepts both via `_COLUMN_ALIASES`); the second
  `run_crossmatch()` then succeeds cache-only (47/104 matched).
* **Suggested fix direction**: add endpoint fallback (try
  `gea.esac.esa.int/tap-server/tap/sync` after the VizieR timeout) or make the
  endpoint configurable, exposed through `query_gaia_cached(..., url=...)`.

### ISSUE 2 — H-R page is always empty: parallax/pm error columns are never fetched, stored, or used

* **Symptom**: `HrDiagramController.refresh()` on the cross-matched stack
  reports `46 CMD / 0 H-R points`; `hr.png` is an empty page and
  `HrDiagramView.export_png` would raise
  `HrError("nothing to export — no diagram has been rendered yet")`
  (`hr_diagram/view.py:449`, because `_render_page` stores `b""` for an empty
  page at `view.py:733-736`).
* **Evidence**: in the project DB, `crossmatch.parallax_error` (and
  `pmra_error`/`pmdec_error`) are NULL for all 104 rows.
* **file:line / root-cause chain**:
  * `gaia/query.py:109` `GAIA_COLUMNS` selects only 7 columns — no
    `parallax_error`, `pmra_error`, `pmdec_error`;
  * `gaia/query.py:131-150` `GaiaStar` has no `*_error` fields;
  * `gaia/matching.py:64-89` `CrossmatchRow` has no `*_error` fields, and
    `_gaia_fields` (`matching.py:121-143`) extracts only 7 fields, so
    `store/crossmatch.py` `_validate_row` (:243-304) stores NULLs;
  * `hr_diagram/core.py:hr_points` (:366) drops every row at :399-407
    (`parallax_error is None → continue`) because the parallax-SNR cut needs
    the error.
* **Repro**: run the headless driver end-to-end (steps 1–6 above) — CMD has
  46 points, H-R has 0, CSV shows empty `parallax_error` column.
* **Suggested fix direction**: add `PlxError`/`pmRAError`/`pmDEError` (archive:
  `parallax_error`, `pmra_error`, `pmdec_error`) to `GAIA_COLUMNS`, `GaiaStar`,
  `CrossmatchRow`/`_gaia_fields`, and the store row mapping; the H-R page then
  populates from the same persisted table.

### ISSUE 3 (minor) — Solve panel always reports "0 stars" for solve-field

* **file:line**: `astrometry/solve.py:349-350` — the solve-field branch
  hardcodes `n_stars=0, rms_arcsec=None`, so
  `SolveController.solve_open_stack()` returns status
  `solved: 0 stars (solve-field)` even when the solve succeeded and the WCS
  was persisted (it was, for all 3 frames).
* **Suggested fix direction**: parse the solve-field output (or the persisted
  WCS) for the matched-star count / RMS instead of the hardcoded zeros.

### Minor behavioural note (not a bug): after cross-matching, `refresh()` leaves `frame_id=None`\n
`HrDiagramController.refresh()` only auto-selects a stack when the viewer's
open stack changed (snapshot event); right after `run_crossmatch()` the first
`refresh()` returns `frame_id=None`, so pick the stack explicitly
(`select_stack(3)` — the GUI stack dropdown) before reading points, or re-open
the stack in the viewer first.
