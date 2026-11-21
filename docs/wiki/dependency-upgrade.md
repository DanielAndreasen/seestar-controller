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

## Issue 05 — test-suite performance (2026-11-21, branch `wt/dependency-upgrade-05`)

Goal: reduce wall-clock of `uv run pytest -q` without weakening tests. Reference tree: main @ `95813e0`, headless sandbox venv (CPython 3.14, no gi → 19 gi-gated skips; the 2 known pre-existing failures listed above are present on main too and were left untouched).

### `--durations` analysis (before)

Baseline `uv run pytest -q` on main @ `95813e0`: **505.14s — 2586 passed, 19 skipped, 2 failed** (the 2 known). Slowest ~10 tests were the analysis suites (`test_analysis_fold/periodogram/export`, 15–17s each). Root cause: the `_project_with_sinusoid` fixture builds a 710-point lightcurve where each point costs a `store_frame` (FITS write + `os.fsync` + sqlite commit ≈ 21ms) plus an `add_measurement` (sqlite commit ≈ 11ms) → ~32ms/point, ~22s per test. Measured breakdown: FITS bytes 0.6ms, `store_frame` 21.4ms (≈11ms file fsync + ≈10ms sqlite commit), `add_measurement` 10.8ms (all sqlite commit).

### Fixes applied

1. **Test-only I/O shims in `tests/conftest.py`** (session-scoped, restored on teardown): `os.fsync` → no-op and a `sqlite3.connect` wrapper executing `PRAGMA synchronous=OFF` per connection. Cost per fixture point drops 32ms → 0.95ms (34×). Safe because: durability against power loss is unobservable in-process; atomicity (temp + `os.replace`, no partial files) is still fully exercised by the failure-path tests that monkeypatch `os.fsync` themselves (verified passing); `journal_mode` is untouched (`test_store_project.py` asserts WAL); all src modules call `sqlite3.connect` at call time. Result: the 15–22s analysis tests are now 1.1–1.7s (remaining time is genuine Lomb-Scargle compute).
2. **pytest-xdist (`-n auto`) added to the dev dependency group** and verified reliable: hazard scan found no fixed ports (all bind port 0), no cross-test env mutation (all via `monkeypatch`), `SEESTAR_PROTOCOL_LOG=0` set per worker, no network. Two order-racy listener assertions in `tests/test_enhance_editor.py` flaked once under CPU contention (with the direct `invoke_on_main` test double, the render worker's apply-notification can be reordered before the synchronous "rendering started" one); fixed test-side to be order-agnostic — no src change. After that, **3 consecutive `-n auto` runs gave the identical pass/fail set as serial** (2586 passed, 19 skipped, 2 known failed).
3. **`slow` marker** registered in `pyproject.toml`; the 7 tests ≥ ~2s serial (visibility_planner ×2, visibility_timeline ×3, preview ×1, transport ×1 — genuine astropy/scipy compute or timing tests) are marked `@pytest.mark.slow`. They always run in the full suite; `-m "not slow"` deselects exactly those 7 for a fast loop.

### Before/after timing table (2026-11-21)

| run | command | wall-clock | result |
|---|---|---|---|
| before — main @ `95813e0` | `uv run pytest -q` (serial) | **505.14s** | 2586 passed, 19 skipped, 2 failed (known) |
| after — `wt/dependency-upgrade-05` | `uv run pytest -q` (serial, run 1) | **165.18s** | 2586 passed, 19 skipped, 2 failed (identical set) |
| after — `wt/dependency-upgrade-05` | `uv run pytest -q` (serial, run 2, `--durations=20`) | **154.03s** | 2586 passed, 19 skipped, 2 failed (identical set) |
| after — `wt/dependency-upgrade-05` | `uv run pytest -q -n auto` (3 consecutive runs) | **20.6–21.7s** | 2586 passed, 19 skipped, 2 failed — identical set every run |
| after — fast loop | `uv run pytest -q -n auto -m "not slow"` | **20.58s** | 2579 passed, 19 skipped, 2 failed (7 `slow` deselected) |

After the fix the serial duration distribution is flat (slowest single test 3.9s, down from 22s). The parallel invocation is the documented default (see AGENTS.md "Running tests"); the ticket commit is the head of `wt/dependency-upgrade-05` (merge hash recorded on merge, as for issues 01–03).
