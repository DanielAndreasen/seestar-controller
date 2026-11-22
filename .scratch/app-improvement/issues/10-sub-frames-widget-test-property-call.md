# 10: sub-frames widget test calls `current_stack`/`current_image` properties as methods

**What to build:** fix the row-click test so its final assertions actually execute.

Evidence: `tests/test_sub_frames_analysis_widgets.py:227-229` calls `viewer.current_stack()` / `viewer.current_image()` → `TypeError: 'StackInfo' object is not callable`; both are `@property` (`src/seestar_controller/enhance/viewer_controller.py:176-177,181-182`); passing usages elsewhere: `tests/test_enhance_widgets.py:199-200`, `tests/test_sub_frames_viewer.py:245`. Failing test: `test_star_picker_lists_sub_and_row_click_opens_it`.

**Blocked by:** None (can start immediately)

**Status:** completed — reviewed (approved) and merged to main in d69311a

- [x] The test uses property access (`viewer.current_stack`) and passes with gi importable; full suite green; ty + ruff clean

## Comments

Audit finding (kind: failing-test / test bug, severity: low). Test-side only.

Implemented in 7d6914b: `tests/test_sub_frames_analysis_widgets.py:227-229` now uses property access (`viewer.current_stack` / `viewer.current_image`) instead of calling them, matching the passing usages in `test_enhance_widgets.py` and `test_sub_frames_viewer.py`. Verified the test failed before the fix (`TypeError: 'StackInfo' object is not callable`) and passes after.

- Targeted tests: `uv run pytest tests/test_sub_frames_analysis_widgets.py -q` → 3 passed (gi importable, GTK4 widgets ran, no skips); the named test alone → 1 passed.
- `uv tool run ruff check` → All checks passed!
- `uv tool run ty check` → 108 diagnostics, identical to the pre-change baseline and to `main` (all pre-existing, mostly gi import resolution); the fix adds none.
- Full suite not run here (coordinator runs it at merge time), per ticket instructions.
