# 03: Cross-match overlay on the stack image

**What to build:** the GUI overlay: show the cross-match on the stack image in the image viewer (labels + magnitudes on matched stars, distinct marker on unmatched detections), with configurable match radius and magnitude limit (re-runs the query on change, served from cache when possible). Demoable: open a crossmatched stack; stars labeled on the image; unmatched flagged; radius/limit controls work.

**Blocked by:** gaia-crossmatch 01, gaia-crossmatch 02

**Status:** implemented in 4f4b1fc (merged)

- [ ] Overlay rendering in the image-enhance viewer path (PIL draw over the tone-mapped image -> texture): matched stars labeled with G mag, unmatched with a distinct marker
- [ ] Controls for match radius + G limit; re-query on change (cache-first)
- [ ] Wired into the app (image viewer toolbar or a crossmatch panel — decide with existing patterns)
- [ ] Widget tests via the ui harness; full suite green

## Comments

(implementor fills in)
