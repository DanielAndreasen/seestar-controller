# 02: PARSEC isochrone overlay + parameter persistence

**What to build:** isochrone overlay data for the H-R diagram: fetch a PARSEC isochrone for given age/metallicity/distance-modulus from a fixed URL and cache it on disk (bundled small fallback grid if the fetch fails — tests must never touch the network), and persist the chosen isochrone parameters (age, [Fe/H], distance modulus) in the project so they survive restarts. Demoable: set parameters on a project, reload, parameters still there; overlay points computed for a synthetic table.

**Blocked by:** hr-diagram 01

**Status:** ready-for-agent

- [ ] Pure isochrone module: `isochrone(age, feh, dist_mod)` -> overlay points (abs G vs BP-RP); HTTP fetch with disk cache under the app config dir + bundled fallback grid; cache keyed by (age, feh)
- [ ] Project persistence of isochrone params (schema bump + migration per project-store 07 pattern; store API `set_isochrone_params` / `isochrone_params`)
- [ ] Tests: cached/bundled path only (no network); migration; params round-trip; no-gi boundary
- [ ] Full suite green

## Comments

(implementor fills in)
