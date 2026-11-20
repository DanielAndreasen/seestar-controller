# 02: Lomb-Scargle core

**What to build:** the pure periodogram engine — astropy LombScargle over a frequency grid derived from the observation baseline and cadence, returning the top-N candidate peaks with their power. Demoable: synthetic sinusoid (known period, noise, gaps) recovers the period within tolerance.

**Blocked by:** seestar-link 01 (uv project scaffold)

**Status:** implemented in 575fe94

- [x] Frequency grid spans from ~2 cycles over the baseline up to the cadence limit
- [x] Returns top-N peaks (period + power), sorted
- [x] Handles uneven sampling and gaps (that's the point of Lomb-Scargle)
- [x] Tests: synthetic sinusoid recovery; pure noise → no dominant peak; excluded points honored
