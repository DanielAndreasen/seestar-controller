# 01: Viewer opens any stored frame (subs included)

**What to build:** the image viewer (Enhance view) lists and opens ALL stored frames of the current project — subs, stacks, enhanced — not just kind='stack'. The Stored-data browser frame rows in the Projects view open the clicked frame in the viewer. Demoable: open NGC7789 offline (two subs, empty stacks), the Enhance stack/frame list shows both subs, click one -> image renders in the viewer.

**Blocked by:** None (can start immediately)

**Status:** implemented in e216f83 (merged)

- [x] `enhance/viewer_controller.py` lists all frames (new `list_project_frames` or generalize `list_project_stacks`); `open_stack`/open works for sub frame ids (RGB->luminance path reused)
- [x] Enhance view list shows all frames with kind label; click opens in viewer
- [x] Projects view "Stored data" frame rows clickable -> open frame in viewer
- [x] Widget tests via ui harness (gi-gated, `.emit("clicked")`, no destroy()); pure tests for listing/opening subs; full suite green; ty + ruff clean

## Comments

Retroactive tick (app-improvement issue 17, docs-only): the Status line above already records the merge, so the acceptance boxes are checked to match it. Implementing commit `e216f83`; test list: `tests/test_sub_frames_viewer.py` and `tests/test_sub_frames_widgets.py` (added there), plus `tests/test_enhance_fits.py`, `tests/test_enhance_widgets.py`, `tests/test_offline_analysis.py`, `tests/test_offline_analysis_widgets.py`.
