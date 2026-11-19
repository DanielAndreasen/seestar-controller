# 01: FITS loading + viewer

**What to build:** open a stack FITS from the current project and render it tone-mapped in the Enhance view. Demoable: create a project with a synthetic stack, open it in the app, image is visible.

**Blocked by:** ui-shell 01, project-store 03

**Status:** ready-for-agent

- [ ] Stacks of the current project are listed and openable
- [ ] FITS loads via astropy and renders as an 8-bit preview image
- [ ] Rendering is fast enough for slider interaction
- [ ] Tests: load synthetic FITS, verify pixel mapping; list stacks from temp project
