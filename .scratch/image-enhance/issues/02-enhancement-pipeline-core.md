# 02: Enhancement pipeline core

**What to build:** the pure, GTK-free enhancement pipeline — composable ops (histogram/asinh stretch, denoise, unsharp-mask sharpen, white balance, background subtraction), each image+params → image, applied in order. Demoable: synthetic FITS with known content passes through each op with measurable expected effect.

**Blocked by:** seestar-link 01 (uv project scaffold)

**Status:** ready-for-agent

- [ ] Each op is a pure function with a params object
- [ ] Pipeline applies ops in order and is re-runnable from the original
- [ ] Stretch preserves ordering; denoise reduces injected noise; sharpen increases edge contrast; WB neutralizes a cast; background subtraction flattens a gradient
- [ ] Tests: synthetic FITS per op + golden-hash regression for fixed input+params
