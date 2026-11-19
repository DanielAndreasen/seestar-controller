# 04: Star picks

**What to build:** persisting photometry star selections — target and comparison stars with pixel position, RA/Dec, and aperture settings (radius, sky annulus), saved in the project so they survive across nights. Demoable: save picks, reopen project, picks are back.

**Blocked by:** 01

**Status:** ready-for-agent

- [ ] Save star picks (role target/comp, x/y, RA/Dec, aperture radius, annulus) into the project
- [ ] Load picks on project open
- [ ] Replacing picks overwrites cleanly
- [ ] Tests: save → reopen → load round-trip on temp project
