# 03: Record the upgrade + final verification pass

**What to build:** document the upgrade as a dated entry in docs/wiki (before/after Python + library versions, what broke and how it was fixed), and do a final end-to-end verification: fresh `uv sync` from the committed lockfile in a clean venv, full suite, ty, ruff, GUI launch, offline project open. Demoable: a new contributor can clone, `uv sync`, run tests, and launch the GUI on the new versions.

**Blocked by:** dependency-upgrade 01, 02

**Status:** implemented in 3af6e31 (merged)

- [ ] docs/wiki dated entry with version table + breakage notes
- [ ] Clean-venv verification: `uv sync --fresh`, `uv run pytest -q`, `uv tool run ty check`, `uv tool run ruff check`, GUI launch, offline project open
- [ ] Commit docs as one ticket commit

## Comments

(implementor fills in)
