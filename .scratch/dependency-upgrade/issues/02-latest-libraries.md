# 02: Upgrade all libraries to latest compatible versions

**What to build:** upgrade every dependency in pyproject.toml to its latest stable release compatible with Python 3.14 (astropy, cryptography, matplotlib, numpy, photutils, pillow, reportlab, and any transitive pins worth bumping): `uv lock --upgrade`, `uv sync`, read changelogs for breaking changes, fix API breakage properly in code/tests, keep full suite green + ty/ruff clean. Demoable: `uv run pip list` shows latest versions; all features work; tests pass.

**Blocked by:** dependency-upgrade 01

**Status:** implemented in 9a3fa47 (merged as 8233ee5)

- [x] `uv lock --upgrade` green; each library at latest stable compatible with py3.14
- [x] Breaking-change fixes in src/ and tests/ (no workarounds, no behavior change)
- [x] Full suite green (known pre-existing headless/gi failures excepted), ty + ruff clean
- [x] GUI launches and core flows work (open project offline, run a photometry measurement on a synthetic project)
- [x] Commit lock + fixes as one ticket commit

## Comments

Retroactive tick (app-improvement issue 17, docs-only): the Status line above already records the merge, so the acceptance boxes are checked to match it. Implementing commit `9a3fa47 (merged 8233ee5)`; test list: uv.lock (uv lock --check green; wiki records uv lock --upgrade --dry-run = no changes); full suite green. Re-verified during the issue-17 audit: all referenced commits exist on main and the listed targeted tests pass (`uv run pytest <files> -q -n auto`).
