# 01: Star picking on image

**What to build:** click-to-pick stars — click the target and comparison stars on the preview or a stack, convert click pixel → RA/Dec via the FITS WCS, draw apertures over picked stars, and save picks into the project. Demoable: open a stack, click three stars, picks saved and reloaded.

**Blocked by:** ui-shell 01, image-enhance 01, project-store 04, catalog 02

**Status:** ready-for-agent

- [ ] Click on image selects a star (nearest detected source or exact pixel)
- [ ] Click pixel → RA/Dec via FITS WCS
- [ ] Target/comp roles assignable per picked star; apertures drawn on the image
- [ ] Picks saved to the project and restored on reopen
- [ ] Tests: WCS round-trip; pick save/load on temp project
