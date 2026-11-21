# 02: Upgrade all libraries to latest compatible versions

**What to build:** upgrade every dependency in pyproject.toml to its latest stable release compatible with Python 3.14 (astropy, cryptography, matplotlib, numpy, photutils, pillow, reportlab, and any transitive pins worth bumping): `uv lock --upgrade`, `uv sync`, read changelogs for breaking changes, fix API breakage properly in code/tests, keep full suite green + ty/ruff clean. Demoable: `uv run pip list` shows latest versions; all features work; tests pass.

**Blocked by:** dependency-upgrade 01

**Status:** ready-for-agent

- [ ] `uv lock --upgrade` green; each library at latest stable compatible with py3.14
- [ ] Breaking-change fixes in src/ and tests/ (no workarounds, no behavior change)
- [ ] Full suite green (known pre-existing headless/gi failures excepted), ty + ruff clean
- [ ] GUI launches and core flows work (open project offline, run a photometry measurement on a synthetic project)
- [ ] Commit lock + fixes as one ticket commit

## Comments

(implementor fills in)
