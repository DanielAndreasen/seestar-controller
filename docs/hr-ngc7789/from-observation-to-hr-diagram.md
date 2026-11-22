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
  `load_frame_wcs`. Since the issue-03 fix (main @5f36978), the matched-star
  count and RMS are parsed from the `.match` FITS table / the
  "solved with N stars, RMS x arcsec" stdout line
  (`astrometry/solve.py:296-320`, `_parse_solve_field_output`,
  `_match_file_rows`) — the panel now reports e.g. `solved: 1 stars
  (solve-field)` instead of a hardcoded 0. **Export annotated PNG** writes the
  solved image with the catalogue overlay (`SolvePanel.export_annotated_to`,
  `astrometry/view.py:190-192`).

Headless: `snapshot = solve_controller.solve_open_stack()`; check
`snapshot.status` / `snapshot.error` / `snapshot.last_n_stars` /
`snapshot.last_rms_arcsec`. For NGC7789 all three frames already had a
persisted WCS; re-solving re-persisted it (n_stars=1 parsed from the
`.match` file).

## 5. Cross-match against Gaia (Enhance view → Cross-match overlay)

* In the `CrossmatchOverlayView` (`gaia/overlay_view.py:75`), set radius and
  G-magnitude limit (spin buttons, `_on_radius_changed` :221+) and press the
  **Run** button (`_on_run_clicked`, `gaia/overlay_view.py:217-218`) →
  `CrossmatchController.run_crossmatch()` (`gaia/overlay.py:644`).
  The controller reads the open stack from the viewer controller, loads its
  WCS (`store.wcs.load_frame_wcs`), queries Gaia DR3 by cone search, matches
  detected stars within the radius (`gaia/matching.py`), and persists the
  result with `store.crossmatch` (visible afterwards via
  `store.crossmatch.list_crossmatched_stacks(project)` → `[(3, 47, 104)]`
  = frame 3, 47 matched of 104 detected).
* The Gaia query (`gaia/query.py`) is **cache-first**: the disk cache
  `~/.config/seestar-controller/cache/gaia/` (`QueryCache`, key
  `gaia_cache_key(ra, dec, radius, g_limit, maxrec)`, TTL `DEFAULT_TTL_S`
  = 7 days) always wins with no HTTP at all and also serves an expired entry
  as a stale fallback when the network fails. On a cache miss the endpoint
  **chain** `GAIA_TAP_SYNC_URLS` (:87) is walked in order — VizieR
  `https://tapvizier.cds.unistra.fr/TAPVizieR/TAP/sync` first, then the ESA
  mirror `https://gea.esac.esa.int/tap-server/tap/sync` (`ESA_GAIA_TAP_SYNC_URL`,
  :82) — each with the ADQL of its own dialect (`_TAP_DIALECTS`, :207;
  `build_cone_query(..., url=...)`, :359; `query_gaia` / `query_gaia_cached`
  accept a `url=` to pin one endpoint). Only when every endpoint fails does
  `GaiaUnavailableError` surface (`gaia/overlay.py:698-701` →
  `snapshot.error`). Since the issue-02 fix, the cone search selects 12
  columns including `PlxError` / `pmRAError` / `pmDEError`
  (`GAIA_COLUMNS`, :133) and they are carried through `GaiaStar`
  (:239-243), `CrossmatchRow` (`gaia/matching.py`) into the persisted
  cross-match table.
* The disk cache key is **versioned** (`CACHE_FORMAT_VERSION = 2`,
  `gaia/query.py:127`, included as `"v": 2` in `gaia_cache_key` :285-310) and a
  cached row must carry **exactly the 12 star fields**
  (`_is_gaia_rows`, :658-667), so entries written by an older cache format
  are never served — they are simply refetched.
* Since the issue-04/05 fixes (main past d554d28) the ESA fallback **works
  end-to-end**: `build_cone_query` schema-quotes a dotted table
  (`_quote_table_ref`, :375-385 → `FROM "gaiadr3"."gaia_source"`, which the
  archive resolves) and `ESA_GAIA_COLUMNS` (:165-178) selects all 12 columns
  incl. `parallax_error`/`pmra_error`/`pmdec_error`. Verified by the re-run:
  with VizieR down, `run_crossmatch()` attempt 1 succeeded in ~45 s (30 s
  VizieR timeout, then the ESA fetch) — `47/104 matched`, no cache seeding
  needed.
* Offline path (still supported): if every endpoint fails, a valid — even
  expired — v2 cache entry is served as a stale fallback; with no cache at
  all, `GaiaUnavailableError` surfaces. The headless driver can additionally
  seed the cache from the ESA mirror with a hand-built unquoted 12-column
  ADQL (§8) as a last resort.

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
  rows are kept only when `parallax` / `parallax_error` pass the
  parallax-SNR cut in `hr_diagram/core.py:hr_points` (:366, checks at
  :399-407). Since the issue-02 fix the error columns are persisted, so the
  page **populates** — the NGC7789 re-run shows `46 CMD / 44 H-R points`.
  An isochrone overlay is drawn when isochrone parameters are persisted for
  the project (`store.isochrone.set_isochrone_params(project, age=…, feh=…,
  dist_mod=…)`); none are set for NGC7789, so the overlay is the bundled
  fallback grid (0 pts).

## 7. Read the diagrams and export

* CMD page: G magnitude vs BP−RP colour for all matched stars; membership
  flag (parallax/proper-motion sample) is shown and included in the CSV.
* H-R page: absolute G magnitude vs Teff for high-parallax-SNR members
  (44 points for NGC7789; `hr.png`).
* **Export PNG** saves the current page (720×560 px, the view's
  `PLOT_WIDTH_PX`/`PLOT_HEIGHT_PX`); **Export CSV** saves the full cross-match
  table (all 104 detected stars with Gaia photometry, parallax **and its
  error**, proper motions **and errors**, separation, membership).

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
# attempt 1: disk cache (v2 key) first, then the endpoint chain VizieR -> ESA
# (on the re-run VizieR timed out and the ESA mirror answered with the fixed
# schema-quoted 12-column ADQL — success in ~45 s, no seeding needed);
# only if every endpoint fails does the driver seed the repo's Gaia disk
# cache from the ESA mirror (hand-built unquoted 12-column ADQL) and call
# run_crossmatch() again — served from cache with no network.
# -> store.crossmatch.list_crossmatched_stacks(project) == [(3, 47, 104)]

hr = HrDiagramController(viewer_controller=viewer, invoke_on_main=invoke)
hr.refresh()                                  # stack list
hr.select_stack(3)                            # stack dropdown
hsnap = hr.refresh()                          # Refresh button
# hsnap.cmd_points / hsnap.hr_points / hsnap.table   (46 / 44 / 104 for NGC7789)

# render exactly like HrDiagramView._render_page (hr_diagram/view.py:733+)
window = full_window(kind, points)
model = DiagramModel(kind, points, hsnap.overlay, show_errors=False, highlighted=None, title=label)
png = render_diagram_png(model, window, width_px=720, height_px=560)
hr.export_table('.../crossmatch-table.csv')   # Export CSV equivalent
```

Outputs land in the project's `reports/hr-ngc7789/` directory and are copied
next to the script into `docs/hr-ngc7789/artifacts/`:

| Artifact | Meaning (re-run on main past d554d28, ESA fallback working) |
|---|---|
| `/home/daniel/SeestarProjects/ngc7789/reports/hr-ngc7789/cmd.png` | CMD page, 46 points (720×560) |
| `.../hr-ngc7789/hr.png` | H-R page, **44 points** (was 0 before the issue-02 fix) |
| `.../hr-ngc7789/stack-marked.png` | stack image with matched stars marked |
| `.../hr-ngc7789/crossmatch-overlay.png` | cross-match overlay render |
| `.../hr-ngc7789/crossmatch-table.csv` | full cross-match table (header + 104 rows, now incl. `parallax_error`/`pmra_error`/`pmdec_error`) |
| `.../hr-ngc7789/run-log.json` | statuses, errors and timings of the whole run |

Repo-side copies: `docs/hr-ngc7789/artifacts/` (same six files).

## 9. ISSUES

Issues 01–06 were all reported on this branch and are now fixed and merged to
main — **verified fixed** by the re-runs:

* 01–03: merges 81af987 / d82cda6 / 81f1f89 (main @5f36978), verified in the
  first re-run (44 H-R points).
* 04–06: merges cd708da / 3cfa7ff / d554d28 (main past d554d28), verified in
  the second re-run: `run_crossmatch()` attempt 1 now succeeds via the ESA
  mirror fallback with the fixed ADQL (45.4 s, no cache seeding), writing a
  v2-keyed cache entry; H-R still 44 points, CMD 46.

**No new issues were found in the second re-run.**

### ISSUE 01 (FIXED on main, verified) — Gaia cross-match hard-failed when the hardcoded VizieR TAP endpoint was down

* Was: `GaiaUnavailableError: ... could not reach VizieR Gaia TAP at https://tapvizier.cds.unistra.fr/TAPVizieR/TAP/sync: <urlopen error timed out>` — `gaia/query.py:69` (old `VIZIER_TAP_SYNC_URL`), raised at old :333/:600, surfaced via `gaia/overlay.py:698-701`.
* Fix (verified): endpoint chain `GAIA_TAP_SYNC_URLS` (:87, VizieR → ESA), per-endpoint ADQL dialects, `url=` parameter on `query_gaia` / `query_gaia_cached`; disk cache still wins without network. Re-run attempt 1 now fails with the *aggregated* error naming both endpoints (VizieR timeout **and** the ESA 400 of ISSUE 04), proving the chain is walked.

### ISSUE 02 (FIXED on main, verified) — H-R page was always empty: parallax/pm error columns never fetched or persisted

* Was: `GAIA_COLUMNS` had 7 columns (`query.py:109`), `GaiaStar`/`CrossmatchRow`/`_gaia_fields` had no `*_error` fields, `store/crossmatch.py` stored NULLs, `hr_diagram/core.py:hr_points` (:366, :399-407) dropped every row → `46 CMD / 0 H-R points`, `export_png` would raise `HrError` at `hr_diagram/view.py:449`.
* Fix (verified): 12 columns incl. `PlxError`/`pmRAError`/`pmDEError` are fetched, carried through `GaiaStar` (:239-243) / `CrossmatchRow`, and persisted. Re-run: **`46 CMD / 44 H-R points`**, CSV has `parallax_error` populated for the 46 matched rows with values, `hr.png` renders 44 points.

### ISSUE 03 (FIXED on main, verified) — solve-field result hardcoded `n_stars=0, rms_arcsec=None`

* Was: `astrometry/solve.py:349-350` hardcoded zeros → panel always showed "solved: 0 stars".
* Fix (verified): `_parse_solve_field_output` / `_match_file_rows` (`solve.py:296-320+`) parse the `.match` file and the "solved with N stars, RMS x arcsec" line. Re-run: `solved: 1 stars (solve-field)` (`n_stars=1`, `rms_arcsec=None` — allowed, "None when it reports none"; solve-field printed no RMS line for a 1-star match).

### ISSUE 04 (FIXED on main, verified) — ESA fallback answered HTTP 400: `build_cone_query` quoted the dotted ESA table name as one identifier

* **Exact error** (run-log attempt 1, and reproduced manually): `Gaia TAP at https://gea.esac.esa.int/tap-server/tap/sync returned HTTP 400 (400) for the query`; server body: `Cannot parse query '... FROM "gaiadr3.gaia_source" ...': 14 unresolved identifiers: gaiadr3.gaia_source [l.1 c.126 - l.1 c.147], ra [...], ... - Unknown table ""gaiadr3.gaia_source"" !`
* **file:line**: `src/seestar_controller/gaia/query.py` — `build_cone_query` FROM clause `f'FROM "{dialect.table}"'` (~:379-380), `ESA_GAIA_DR3_TABLE = "gaiadr3.gaia_source"` (:96), `_TAP_DIALECTS` (:207-216). The archive resolves `gaiadr3.gaia_source` unquoted or `"gaiadr3"."gaia_source"`, but not the single quoted `"gaiadr3.gaia_source"` (verified: quoted table → 400, unquoted or schema-quoted → 200; quoted *columns* are fine).
* **Repro**: `build_cone_query(0.9653, 55.3454, 0.7765, url=ESA_GAIA_TAP_SYNC_URL)` → `fetch_gaia_tap_json(adql, url=ESA_GAIA_TAP_SYNC_URL)` → `GaiaUnavailableError` HTTP 400. Also visible end-to-end: `CrossmatchController.run_crossmatch()` with an empty cache fails with the aggregated `GaiaUnavailableError` listing VizieR (timeout) and ESA (400).
* **Root cause**: the VizieR dialect's table `I/355/gaiadr3` is safely quoted as one identifier on VizieR, but the same quoting breaks for the dotted ESA table on the archive.
* **Fix (verified)**: `_quote_table_ref` (`query.py:375-385`) splits a dotted
  table name and quotes each part (`FROM "gaiadr3"."gaia_source"`), which the
  archive resolves (HTTP 200 in the re-run).

### ISSUE 05 (FIXED on main, verified) — `ESA_GAIA_COLUMNS` lacked the error columns, so even a successful ESA fallback would have left the H-R page empty

* **file:line**: `src/seestar_controller/gaia/query.py:150-160` — `ESA_GAIA_COLUMNS` selects only the 9 original columns; `GAIA_COLUMNS` (:133-146) has 12 incl. `PlxError`/`pmRAError`/`pmDEError`. `_COLUMN_ALIASES` (:163-178) already maps the archive spellings, and `_row_to_star`/`parse_gaia_response` handle them — only the ESA select list is missing them.
* **Repro**: `build_cone_query(0.9653, 55.3454, 0.7765, url=ESA_GAIA_TAP_SYNC_URL)` (with the ISSUE-04 quoting fixed, e.g. unquoted table) returns stars with `parallax_error=None` → `hr_points` drops them all → 0 H-R points.
* **Fix (verified)**: `ESA_GAIA_COLUMNS` (`query.py:165-178`) now lists all
  12 columns incl. `parallax_error`/`pmra_error`/`pmdec_error`; the ESA
  fetch in the re-run returned them (46/104 persisted rows with
  `parallax_error`, 44 H-R points).

### ISSUE 06 (FIXED on main, verified) — pre-fix Gaia cache entries were shape-valid but error-less and served fresh for up to 7 days

* **file:line**: `gaia/query.py:583` `_REQUIRED_STAR_FIELDS = _STAR_FIELDS - {"parallax_error", "pmra_error", "pmdec_error"}`; `:626-630` `_is_gaia_rows` accepts any payload with required ⊆ keys ⊆ star fields; `:604-623` `_row_to_star` reads errors with `.get` → `None`; cache key `gaia_cache_key` (:267-292) has **no schema/column version**, TTL `DEFAULT_TTL_S = 7*24*3600` (:111).
* **Repro (verified)**: write a 9-field (pre-issue-02) row into `QueryCache` under `gaia_cache_key(10.0, 20.0, 0.1, ...)`, then `query_gaia_cached(10.0, 20.0, 0.1, cache, timeout=5)` returns it **with no HTTP at all** and `parallax_error=None` → cross-match "succeeds" but `hr_points` is empty until the entry expires (7 days) or the cache is cleared manually.
* **Fix (verified)**: `CACHE_FORMAT_VERSION = 2` (`query.py:127`) is part of
  `gaia_cache_key` (`"v": 2`, :307) and `_is_gaia_rows` (:658-667) now requires
  exactly the 12 `_STAR_FIELDS` keys per row. The re-run wrote a new v2 entry
  (`a3dd8709….json`) alongside the orphaned v1 file, which is never matched.

### Minor behavioural note (not a bug): after cross-matching, `refresh()` leaves `frame_id=None`\n
`HrDiagramController.refresh()` only auto-selects a stack when the viewer's
open stack changed (snapshot event); right after `run_crossmatch()` the first
`refresh()` may return `frame_id=None`, so pick the stack explicitly
(`select_stack(3)` — the GUI stack dropdown) before reading points, or re-open
the stack in the viewer first. (In the latest re-run the viewer still had the
stack open, so `refresh()` auto-selected frame 3.)
