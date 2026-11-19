# 04: Save enhanced + export + presets

**What to build:** persistence and sharing — save the enhanced image into the project (original FITS untouched), export PNG/JPEG, and store per-image parameter presets in the project so reopening shows the last edit. Demoable: enhance, save, export PNG, reopen — preset applied, original hash unchanged.

**Blocked by:** 02, 03, project-store 01

**Status:** ready-for-agent

- [ ] Enhanced image saved into the project; original FITS byte-identical after save
- [ ] PNG/JPEG export of the current render
- [ ] Parameter presets stored per image in the project db and reapplied on open
- [ ] Tests: save → original hash unchanged; preset round-trip; export file valid
