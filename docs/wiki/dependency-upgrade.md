# Dependency upgrade record — Python 3.12 → 3.14 + latest libraries

Dated 2026-11-21. Spec `dependency-upgrade` (3 issues) completed 2026-11-21; this page is the durable record. Commits: issue 01 implemented `3c07d0f` (merged `bed5056`), issue 02 implemented `9a3fa47` (merged `8233ee5`), issue 03 (this record + verification) on branch `wt/dependency-upgrade-03`. Verified tree: main @ `390f885`.

## What changed

| item | before (2026-11-21, pre-upgrade) | after |
|---|---|---|
| Python | 3.12 (`requires-python = ">=3.12,<3.13"`, `.python-version` 3.12) | **3.14.7** (`requires-python = ">=3.14,<3.15"`, `.python-version` 3.14) |
| pyyaml | 6.0.3 | **6.0.4** (patch, released 2026-11-20; only package with a newer stable release — `uv lock --upgrade`) |
| astropy | 8.0.3 | 8.0.3 (already latest stable admitting CPython 3.14) |
| astropy-iers-data | 0.2026.11.16.0.37.44 | same (latest) |
| cryptography | 50.0.2 | same (latest) |
| matplotlib | 3.11.2 | same (latest) |
| numpy | 2.5.5 | same (latest) |
| photutils | 3.1.1 | same (latest) |
| pillow | 13.0.0 | same (latest) |
| reportlab | 5.1.0 | same (latest) |
| scipy (transitive) | 1.18.1 | same (latest) |
| pyerfa, pytest, cffi, contourpy, cycler, fonttools, kiwisolver, packaging, python-dateutil, charset-normalizer, colorama, iniconfig, pluggy, pycparser, pygments, pyparsing, tzdata | — | all already at latest stable admitting CPython 3.14 (verified per package against PyPI `requires_python`, not just via the resolver) |

"Latest compatible" = latest stable (non-prerelease) release whose `requires_python` admits the project's `>=3.14,<3.15`. After the upgrade, `uv lock --upgrade --dry-run` reports **"No lockfile changes detected"** (re-verified 2026-11-21 on `390f885`).

## What broke and how it was fixed

- **Python 3.12 → 3.14 (issue 01, `3c07d0f`)**: nothing broke beyond the interpreter bump. No removed-stdlib usage, no changed defaults, no 3.14 API incompatibilities were found in `src/` or `tests/` — zero code changes were needed; only `pyproject.toml` (`requires-python`), `.python-version`, `uv.lock` (regenerated with CPython 3.14.7 wheels), README + assumptions stack line changed.
- **Library upgrades (issue 02, `9a3fa47`)**: only pyyaml moved (6.0.3 → 6.0.4, a patch release with no API changes; the repo does not import yaml directly — it is transitive via astropy). No src/tests breakage fixes were required. No `pyproject.toml` constraint change was needed (all latest versions satisfy the existing `>=` lower bounds).
- **Known pre-existing headless failures, unchanged by the upgrade** (identical on main before/after, verified 2026-11-21): `test_report_sections.py::test_movers_section_omitted_when_no_moving_objects_package`, `test_visibility_planner.py::test_every_visibility_module_is_pure_or_widget_and_the_public_api_is_exported`. gi-gated widget suites skip in this sandbox (system gi is cpython-312-only — see assumptions.md 2026-11-21 entry).

## Final end-to-end verification (2026-11-21, commit `390f885`)

Done in a **scratch copy** (`git archive 390f885` → `/tmp`, never in a checkout), fresh clean venv:

- `rm -rf .venv && uv sync` from the committed lockfile: **green**, CPython 3.14.7, `uv pip list` shows the versions in the table above.
- `uv run pytest -q` (full suite): **2573 passed, 18 skipped, 2 failed in 460.92s** — the 2 failures are the known pre-existing ones above; the 18 skips are gi/reproject-gated widget suites (no gi under the 3.14 venv in this sandbox).
- `uv tool run ty check`: **All checks passed!**
- `uv tool run ruff check`: **All checks passed!**
- `uv run seestar-controller --help`: works (CLI entrypoint).
- `uv run seestar-controller gui`: prints the clean system-gi error (`No module named 'gi'`) in this headless sandbox — expected; on the desktop with a 3.14-built PyGObject the GUI launches (see assumptions.md).
- Offline project open, headless: create project → close → `store.open_project(path)` re-open from disk → sessions listed: **OK** (the store path `SeestarWindow.open_project_offline` uses, minus the GTK window).
- `uv lock --upgrade --dry-run`: **No lockfile changes detected**.

A new contributor can therefore clone, `uv sync`, run the suite, and launch the GUI on the new versions.
