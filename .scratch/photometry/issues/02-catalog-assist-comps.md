# 02: Catalog assist for comps

**What to build:** standard-star suggestions — given the target's coordinates, list nearby AAVSO/VizieR standard stars from the catalog module; clicking a suggestion adds it as a comparison star. Demoable: with mocked catalog, suggestions appear next to the image and can be added as comps.

**Blocked by:** 01, catalog 04

**Status:** ready-for-agent

- [ ] Target coords → list of nearby standard stars (coords + mag)
- [ ] Selecting a suggestion adds it as a comp pick
- [ ] Suggestions respect the configured search radius
- [ ] Tests: mocked catalog responses → suggestion list → comp added
