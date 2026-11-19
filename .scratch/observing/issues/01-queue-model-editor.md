# 01: Queue model + editor

**What to build:** tonight's plan as an editable queue — entries with name or RA/Dec (names resolved via catalog), mosaic, exposure, gain; add, reorder, remove, duplicate. Demoable: build and edit a queue in the Observe view.

**Blocked by:** ui-shell 01, catalog 01

**Status:** ready-for-agent

- [ ] Queue entries hold name-or-coords, mosaic, exposure, gain
- [ ] Add by name resolves via catalog; add by RA/Dec validates input
- [ ] Reorder / remove / duplicate entries in the UI
- [ ] Queue persists for the session (not necessarily to disk yet)
- [ ] Tests: model operations + name resolution with mocked catalog
