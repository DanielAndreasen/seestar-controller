# 02: H-R page always empty — parallax/pm error columns are never fetched, stored, or used

**What to build:** fetch and persist the Gaia error columns so `hr_diagram.hr_points` can apply its parallax-SNR cut: add `parallax_error` / `pmra_error` / `pmdec_error` (VizieR `PlxError`, `pmRAError`, `pmDEError`) to `GAIA_COLUMNS` (`gaia/query.py:109`), the `GaiaStar` dataclass (`query.py:131-150`), `CrossmatchRow` / `_gaia_fields` (`gaia/matching.py:64-89`, `:121-143`), and the store mapping in `store/crossmatch.py:243-304` (currently stores NULLs — check whether the crossmatch table schema needs a migration; `frame_wcs` went to schema v7 for a similar addition). Then the H-R diagram view shows real points.

Evidence: headless GUI-path run on `/home/daniel/SeestarProjects/ngc7789` stack frame 3: `HrDiagramController.refresh()` reports 46 CMD / 0 H-R points; `hr.png` is an empty page and `HrDiagramView.export_png` raises `HrError("nothing to export — no diagram has been rendered yet")` (`hr_diagram/view.py:449`, empty page `png=b""` at `:733-736`). DB evidence: `crossmatch.parallax_error` is NULL for all 104 rows. Root-cause chain: `hr_diagram/core.py:399-407` drops every row because `parallax_error is None`.

**Blocked by:** None (can start immediately)

**Status:** implemented in 3fa2966 (pending review)

- [x] `query_gaia` returns rows with parallax/pm error fields populated
- [x] `save_crossmatch`/`load_crossmatch` round-trip the error columns (schema migration if needed) — **no migration needed**: the v6 `crossmatch` DDL (`_CROSSMATCH_DDL` in `store/schema.py`) already created `parallax_error`/`pmra_error`/`pmdec_error` since the table was introduced (commit 221cbed), and `store/crossmatch.py` already selected/validated/inserted them; only the gaia-package values were missing
- [x] `hr_points` yields points for rows passing the SNR cut; `membership_filter` can use pm errors
- [x] Existing gaia/crossmatch/hr_diagram tests pass + new coverage for the error columns; full suite `uv run pytest -q -n auto` green; `ty`/`ruff` clean

## Comments

Found by the hr-ngc7789 driver subagent. Kind: missing feature, severity: high — the H-R (absolute-G) page is unusable without parallax errors; the CMD page works (46 points rendered).

Implemented on branch `wt/hr-ngc7789-02` (worktree `hr-ngc7789-02`), commit `3fa2966`:

- `gaia/query.py`: `GAIA_COLUMNS` now selects `PlxError`/`pmRAError`/`pmDEError`; `_COLUMN_ALIASES` maps them (VizieR + Gaia-archive spellings); `GaiaStar` gained `parallax_error`/`pmra_error`/`pmdec_error`; `_tap_row_to_star` parses them; the disk-cache rows (`_STAR_FIELDS`/`_star_to_row`/`_row_to_star`) serialize them. Cache entries written before the error columns were selected remain valid (error fields read `None`), so the offline stale fallback keeps serving existing caches — covered by dedicated tests.
- `gaia/matching.py`: `CrossmatchRow` gained the three error fields (same order as `store.CrossmatchRecord`), `_gaia_fields` extracts them duck-typed, `crossmatch()` populates them; unmatched rows have them `None`.
- No schema change: the v6 `crossmatch` table already had the columns, so no v10 migration was added (verified against the original v6 DDL in git history).
- Tests: wire-payload/ADQL/parse assertions for the error columns, cache round-trip + pre-error-columns cache compatibility (fresh and stale-offline), crossmatch row assertions (incl. the brute-force equivalence helper), and an end-to-end `crossmatch -> save_crossmatch -> load_crossmatch -> hr.hr_points` test proving the SNR cut now keeps high-SNR rows (111 kept, 222 at SNR 2.5 dropped) while `cmd_points` keeps both.

Verification (worktree `~/.vibe/worktrees/seestar-controller-dec7d727fea1/hr-ngc7789-02`): `uv run pytest -q -n auto` → 2864 passed, 2 skipped (env-gated skips; the two gi-less-venv failures seen before recreating the venv with the pyproject-documented `--system-site-packages` recipe were environment-only, identical on main); `uv tool run ruff check` → all checks passed; `uv tool run ty check` → no diagnostics in any touched file (the 108 repo-wide diagnostics are pre-existing gi-stub noise, identical count on main).
