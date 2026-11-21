# 02: Solve + crossmatch + H-R + photometry accept solved subs

**What to build:** the analysis chain works on subs once they are plate-solved: the viewer's Solve action accepts any open frame (sub included) and persists WCS via store.wcs for sub frame ids; Gaia crossmatch overlay runs on a solved sub; the H-R diagram view's frame selector includes solved subs and builds CMD/H-R from their crossmatch; photometry picking accepts a solved sub. Demoable: open NGC7789 offline, open a sub, Solve -> WCS saved, crossmatch -> stars with G mags/parallax, H-R view shows the diagram for that sub.

**Blocked by:** sub-frames 01

**Status:** ready-for-agent

- [ ] SolveController accepts any open frame; store.wcs persists/loads WCS for sub ids (schema check — bump + migrate only if needed)
- [ ] Crossmatch overlay + H-R view frame selector include solved subs (WCS + crossmatch present)
- [ ] Photometry picker accepts solved sub
- [ ] Tests: pure (solve sub -> WCS round-trip, crossmatch on sub, H-R from sub crossmatch; solver + Gaia HTTP monkeypatched, no network) + gi-gated widget tests; full suite green; ty + ruff clean

## Comments

(implementor fills in)
