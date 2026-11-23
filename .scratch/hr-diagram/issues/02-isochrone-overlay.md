# 02: PARSEC isochrone overlay + parameter persistence

**What to build:** isochrone overlay data for the H-R diagram: fetch a PARSEC isochrone for given age/metallicity/distance-modulus from a fixed URL and cache it on disk (bundled small fallback grid if the fetch fails — tests must never touch the network), and persist the chosen isochrone parameters (age, [Fe/H], distance modulus) in the project so they survive restarts. Demoable: set parameters on a project, reload, parameters still there; overlay points computed for a synthetic table.

**Blocked by:** hr-diagram 01

**Status:** implemented in 376b9cd (merged)

- [x] Pure isochrone module: `isochrone(age, feh, dist_mod)` -> overlay points (abs G vs BP-RP); HTTP fetch with disk cache under the app config dir + bundled fallback grid; cache keyed by (age, feh)
- [x] Project persistence of isochrone params (schema bump + migration per project-store 07 pattern; store API `set_isochrone_params` / `isochrone_params`)
- [x] Tests: cached/bundled path only (no network); migration; params round-trip; no-gi boundary
- [x] Full suite green

## Comments

Retroactive tick (app-improvement issue 17, docs-only): the Status line above already records the merge, so the acceptance boxes are checked to match it. Implementing commit `376b9cd`; test list: tests/test_hr_isochrone.py, tests/test_hr_isochrone_store.py. Re-verified during the issue-17 audit: all referenced commits exist on main and the listed targeted tests pass (`uv run pytest <files> -q -n auto`).
