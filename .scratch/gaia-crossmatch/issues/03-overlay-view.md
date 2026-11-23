# 03: Cross-match overlay on the stack image

**What to build:** the GUI overlay: show the cross-match on the stack image in the image viewer (labels + magnitudes on matched stars, distinct marker on unmatched detections), with configurable match radius and magnitude limit (re-runs the query on change, served from cache when possible). Demoable: open a crossmatched stack; stars labeled on the image; unmatched flagged; radius/limit controls work.

**Blocked by:** gaia-crossmatch 01, gaia-crossmatch 02

**Status:** implemented in 4f4b1fc (merged)

- [x] Overlay rendering in the image-enhance viewer path (PIL draw over the tone-mapped image -> texture): matched stars labeled with G mag, unmatched with a distinct marker
- [x] Controls for match radius + G limit; re-query on change (cache-first)
- [x] Wired into the app (image viewer toolbar or a crossmatch panel — decide with existing patterns)
- [x] Widget tests via the ui harness; full suite green

## Comments

Retroactive tick (app-improvement issue 17, docs-only): the Status line above already records the merge, so the acceptance boxes are checked to match it. Implementing commit `4f4b1fc`; test list: tests/test_gaia_overlay.py, tests/test_gaia_overlay_widgets.py. Re-verified during the issue-17 audit: all referenced commits exist on main and the listed targeted tests pass (`uv run pytest <files> -q -n auto`).
