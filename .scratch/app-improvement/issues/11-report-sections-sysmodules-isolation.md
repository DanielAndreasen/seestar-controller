# 11: movers-section report test is order-dependent (assumes pristine `sys.modules`)

**What to build:** make `test_movers_section_omitted_when_no_moving_objects_package` deterministic under `pytest -n auto`, where another test in the same worker may already have imported the moving-objects package.

Evidence: `tests/test_report_sections.py:271` asserts `"seestar_controller.moving_objects" not in sys.modules`; it passes standalone but fails in the full parallel run once any moving-objects test ran in the same worker. One of the two "known pre-existing failures" in `docs/wiki/dependency-upgrade.md`, but there attributed to headless-without-gi — in a gi-present environment it fails purely from in-worker module pollution.

**Blocked by:** None (can start immediately)

**Status:** completed — reviewed (approved) and merged to main in d102826

- [x] The test passes both standalone and in the full `uv run pytest -q -n auto` run (enforce isolation via monkeypatch/subprocess like the gi-boundary tests, or drop the sys.modules premise)
- [x] Full suite green; ty + ruff clean

## Comments

Audit finding (kind: failing-test / test-isolation bug, severity: low). Test-side only; do not weaken the assertion's intent (movers section still omitted when no movers data exists).

Implemented in 7f8b24f. `test_movers_section_omitted_when_no_moving_objects_package` no longer asserts on pristine `sys.modules`; instead a new `_block_moving_objects_import` helper (same trick as `test_cli_gui_gi_error._block_app_import`) sets `sys.modules["seestar_controller.moving_objects"] = None` via monkeypatch — importlib then raises ImportError, which the test asserts explicitly — and deletes the parent package attribute. The behavioral intent is unchanged: `data.movers is None` and the movers section is omitted from HTML + PDF. Test-side only, one file touched (`tests/test_report_sections.py`), docstring bullet updated to match.

Results (worktree `ai-11`, branch `wt/app-improvement-11`):
- `uv run pytest tests/test_report_sections.py -q` — 15 passed (standalone).
- Pollution proxy, previously failing, now green: `uv run pytest tests/test_moving_objects.py tests/test_report_sections.py -q` — 79 passed (same process, moving-objects imported first). Wider same-process mix (`test_moving_objects{,_mpc,_overlay}.py test_report_sections.py test_report_boundary.py test_offline_analysis.py test_gaia_overlay_widgets.py`) — 160 passed, 1 skipped (gi not importable in this shell; widget suite skips, unrelated to this change).
- `uv tool run ty check` — All checks passed. `uv tool run ruff check` — All checks passed.
- Full `uv run pytest -q -n auto` not run here per ticket rules (coordinator runs it at merge time); the failing condition (in-worker module pollution) is reproduced and fixed by the same-process runs above.
