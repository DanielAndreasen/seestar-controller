# 01: FITS loading + viewer

**What to build:** open a stack FITS from the current project and render it tone-mapped in the Enhance view. Demoable: create a project with a synthetic stack, open it in the app, image is visible.

**Blocked by:** ui-shell 01, project-store 03

**Status:** implemented in 53a64d3

- [x] Stacks of the current project are listed and openable
- [x] FITS loads via astropy and renders as an 8-bit preview image
- [x] Rendering is fast enough for slider interaction
- [x] Tests: load synthetic FITS, verify pixel mapping; list stacks from temp project

## Comments

New package `src/seestar_controller/enhance/` with the strict pure/widget split (subprocess + source-scan enforced, mirroring observing 01):

- Pure `enhance/fits_io.py` (stdlib + numpy + store only, astropy lazy inside `load_fits`): `EnhanceError`; `FitsImage` frozen dataclass (float32 `(H,W)`/`(H,W,3)` NaN-safe data, header dict, path); `load_fits(path)` opens with `do_not_scale_image_data=True` and applies BZERO/BSCALE/BLANK manually (non-finite -> NaN, RGB `(3,H,W)` -> `(H,W,3)`, first HDU with data wins, errors -> `EnhanceError`); `StackInfo` frozen dataclass (frame_id, session_id, session_started_at, absolute path, jd/utc, target/exposure/gain, `label()`/`subtitle()`); `list_project_stacks(project)` via `store.frames.list_frames(kind="stack")` + sessions + `get_frame_path`, ordered by jd, store errors wrapped, None project -> `EnhanceError`.
- Pure `enhance/render.py` (numpy + PIL only, no scipy): `tonemap(data, lo, hi, stretch, gamma)` — percentile default lo/hi (0.1%/99.5% of finite values), linear/asinh/log/sqrt stretches, gamma, NaN/inf -> 0, RGB uses global percentiles, uint8 out, bad params -> `EnhanceError`; `to_pil_image` (L/RGB); `downscale` (integer-factor mean pooling); `render_preview(fits_image, max_size=1024)` — downscale FIRST then tonemap, so a 2048² stack renders in well under 0.5 s (asserted in tests) keeping slider interaction smooth.
- Pure `enhance/viewer_controller.py`: `ViewerController(project=None, preview_max_size=1024)` — the app-level shared controller: `set_project()`, `refresh_stacks()`/`stacks`, `open_stack(frame_id)` (FITS cached by path+mtime, MAX_CACHE_ENTRIES=8, errors recorded in the snapshot + notified + raised), `current_stack`/`current_image`/`preview`, frozen `ViewerSnapshot` listeners (add/remove idempotent, decorator-capable, raising listeners isolated via logger.exception). No enhancement ops yet — ticket 02.
- Widget `enhance/viewer.py` (gi at top, lazy PEP 562 export via `_WIDGET_EXPORTS`): `ImageViewer` — Gtk.Paned with the stack Gtk.ListBox + Refresh button + inline status on the left, Gtk.Picture (`content_fit=CONTAIN`) fed from the PIL preview via PNG bytes -> `Gdk.Texture.new_from_bytes` (same as observe_view) over a placeholder overlay ("no project" / "no stack open") + info label on the right; re-renders on every snapshot.
- Wiring: `SeestarApplication` owns `viewer_controller = ViewerController()`; `SeestarWindow` takes/passes it to `build_view(view.id, ..., viewer_controller=...)`; `EnhanceView` (ui/views.py) now embeds the `ImageViewer` instead of the placeholder. No project is selected at startup (Projects view is a later ticket) — the controller starts empty and the view shows the "no project" placeholder until `set_project()` is called.
- Tests: `tests/test_enhance_fits.py` (64 pure headless: load_fits round-trip/uint16-BZERO/byte-patched BSCALE+BZERO/BLANK/RGB/non-finite/error cases, list_project_stacks from a tmp project with stored stacks + a sub that must not appear, tonemap monotonicity/percentiles/NaN/stretches/gamma/RGB/errors, to_pil_image/downscale/render_preview incl. the <0.5 s 2048² fast path, ViewerController list/open/cache-by-mtime/errors/set_project/frozen snapshot/listeners, subprocess + source-scan boundary) and `tests/test_enhance_widgets.py` (importorskip gi; skips in sandbox, runs on desktop/xvfb: build_view wiring, stack rows, row selection opens the stack, texture shown, no-project placeholder, app owns the shared controller and the window passes it down). Full suite: 1152 passed + 3 skipped.

Assumptions: no project-selection UI yet, so the app sets the project programmatically via `set_project()` and the view shows a "no project" placeholder meanwhile; preview downscales before tone mapping (fast path for ticket 02's sliders); FITS cache keyed by path+mtime; numpy only (no scipy needed); widget tests skip in the sandbox (no GTK).
