# 02: Solve + crossmatch + H-R + photometry accept solved subs

**What to build:** the analysis chain works on subs once they are plate-solved: the viewer's Solve action accepts any open frame (sub included) and persists WCS via store.wcs for sub frame ids; Gaia crossmatch overlay runs on a solved sub; the H-R diagram view's frame selector includes solved subs and builds CMD/H-R from their crossmatch; photometry picking accepts a solved sub. Demoable: open NGC7789 offline, open a sub, Solve -> WCS saved, crossmatch -> stars with G mags/parallax, H-R view shows the diagram for that sub.

**Blocked by:** sub-frames 01

**Status:** implemented in fbc19ec (merged)

- [x] SolveController accepts any open frame; store.wcs persists/loads WCS for sub ids (schema check — bump + migrate only if needed)
- [x] Crossmatch overlay + H-R view frame selector include solved subs (WCS + crossmatch present)
- [x] Photometry picker accepts solved sub
- [x] Tests: pure (solve sub -> WCS round-trip, crossmatch on sub, H-R from sub crossmatch; solver + Gaia HTTP monkeypatched, no network) + gi-gated widget tests; full suite green; ty + ruff clean

## Comments

Retroactive tick (app-improvement issue 17, docs-only): the Status line above already records the merge, so the acceptance boxes are checked to match it. Implementing commit `fbc19ec`; test list: `tests/test_sub_frames_analysis.py` and `tests/test_sub_frames_analysis_widgets.py` (added there).
