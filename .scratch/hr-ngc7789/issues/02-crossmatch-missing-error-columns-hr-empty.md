# 02: H-R page always empty — parallax/pm error columns are never fetched, stored, or used

**What to build:** fetch and persist the Gaia error columns so `hr_diagram.hr_points` can apply its parallax-SNR cut: add `parallax_error` / `pmra_error` / `pmdec_error` (VizieR `PlxError`, `pmRAError`, `pmDEError`) to `GAIA_COLUMNS` (`gaia/query.py:109`), the `GaiaStar` dataclass (`query.py:131-150`), `CrossmatchRow` / `_gaia_fields` (`gaia/matching.py:64-89`, `:121-143`), and the store mapping in `store/crossmatch.py:243-304` (currently stores NULLs — check whether the crossmatch table schema needs a migration; `frame_wcs` went to schema v7 for a similar addition). Then the H-R diagram view shows real points.

Evidence: headless GUI-path run on `/home/daniel/SeestarProjects/ngc7789` stack frame 3: `HrDiagramController.refresh()` reports 46 CMD / 0 H-R points; `hr.png` is an empty page and `HrDiagramView.export_png` raises `HrError("nothing to export — no diagram has been rendered yet")` (`hr_diagram/view.py:449`, empty page `png=b""` at `:733-736`). DB evidence: `crossmatch.parallax_error` is NULL for all 104 rows. Root-cause chain: `hr_diagram/core.py:399-407` drops every row because `parallax_error is None`.

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] `query_gaia` returns rows with parallax/pm error fields populated
- [ ] `save_crossmatch`/`load_crossmatch` round-trip the error columns (schema migration if needed)
- [ ] `hr_points` yields points for rows passing the SNR cut; `membership_filter` can use pm errors
- [ ] Existing gaia/crossmatch/hr_diagram tests pass + new coverage for the error columns; full suite `uv run pytest -q -n auto` green; `ty`/`ruff` clean

## Comments

Found by the hr-ngc7789 driver subagent. Kind: missing feature, severity: high — the H-R (absolute-G) page is unusable without parallax errors; the CMD page works (46 points rendered).
