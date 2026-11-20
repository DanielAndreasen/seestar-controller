# 04: Save enhanced + export + presets

**What to build:** persistence and sharing — save the enhanced image into the project (original FITS untouched), export PNG/JPEG, and store per-image parameter presets in the project so reopening shows the last edit. Demoable: enhance, save, export PNG, reopen — preset applied, original hash unchanged.

**Blocked by:** 02, 03, project-store 01

**Status:** implemented in e9d0f273c1a517973e8fea04632a4fde9a854901

- [x] Enhanced image saved into the project; original FITS byte-identical after save
- [x] PNG/JPEG export of the current render
- [x] Parameter presets stored per image in the project db and reapplied on open
- [x] Tests: save → original hash unchanged; preset round-trip; export file valid

## Comments

- Store (additive only): `KIND_ENHANCED = "enhanced"` + `ENHANCED_DIR` in `store/frames.py` (KINDS now sub/stack/enhanced); schema v3 migration `_migrate_2_to_3` creates `presets(frame_id TEXT PRIMARY KEY, pipeline TEXT NOT NULL, updated_at TEXT NOT NULL)` and rebuilds `frames` with a CHECK that allows 'enhanced' (FK off during migration, `foreign_key_check` after); new `store/presets.py` with `save_preset` / `load_preset` / `delete_preset` / `list_presets` (+ `Preset`, `PresetError`) using `with project.db:` transactions and a frame-exists check.
- Pure layer `enhance/persistence.py` (no gi): `save_enhanced(project, stack, pipeline, fits_image) -> Frame` renders the pipeline at FULL resolution and writes a FITS (header copied minus structural cards + HISTORY card) via `store_frame(kind="enhanced")` — original stack file untouched (sha256-verified in tests); `export_render(image, path, format="PNG"|"JPEG", quality=95) -> Path` (PIL/uint8/float inputs, atomic tempfile+os.replace, suffix validation); `save_preset_for` / `load_preset_for` / `delete_preset_for`; `apply_preset_on_open(project, stack) -> Pipeline | None` (corrupt preset logged + ignored → default).
- `EditorController`: `open_stack` applies the stored preset instead of the default when present; new `save_preset()` / `save_enhanced()` / `export(path, format, quality)` passthroughs; snapshot gained `preset_saved` / `saved_frame`.
- Widget: EnhanceEditor toolbar gained Save enhanced / Save preset / Export PNG / Export JPEG buttons (export via async `Gtk.FileDialog.save`, callback marshalled with `invoke_on_main`); EnhanceView exposes the four button properties.
- Tests: 19 new pure tests in `tests/test_enhance_persistence.py` (save + original-hash-unchanged + FITS round-trip allclose, export PNG/JPEG validity + bad-format raises, preset round-trip at persistence and store level incl. corrupt rows, EditorController open-applies-preset + passthroughs, schema v3 fresh + simulated v2→v3 migration, no-gi boundary subprocess + source scan). Widget tests for the new buttons skip headless. Full suite: 1306 passed + 3 skipped (1287 + 19 new; 3 skips are the gi widget modules).
- Assumptions: presets keyed by `frame_id` TEXT without a FK to frames (deleted frames leave an orphan row — `delete_preset` provided); `enhanced/` dir created lazily by `store_frame`; export creates parent dirs and appends a missing suffix; corrupt preset on open falls back to the default pipeline.
