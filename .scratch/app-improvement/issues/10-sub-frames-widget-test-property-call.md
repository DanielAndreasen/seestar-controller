# 10: sub-frames widget test calls `current_stack`/`current_image` properties as methods

**What to build:** fix the row-click test so its final assertions actually execute.

Evidence: `tests/test_sub_frames_analysis_widgets.py:227-229` calls `viewer.current_stack()` / `viewer.current_image()` → `TypeError: 'StackInfo' object is not callable`; both are `@property` (`src/seestar_controller/enhance/viewer_controller.py:176-177,181-182`); passing usages elsewhere: `tests/test_enhance_widgets.py:199-200`, `tests/test_sub_frames_viewer.py:245`. Failing test: `test_star_picker_lists_sub_and_row_click_opens_it`.

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] The test uses property access (`viewer.current_stack`) and passes with gi importable; full suite green; ty + ruff clean

## Comments

Audit finding (kind: failing-test / test bug, severity: low). Test-side only.
