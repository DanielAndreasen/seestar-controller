# 03: Exclude flagged frames from photometry runs

**What to build:** photometry runs accept the session's flagged-frame set and exclude those frames from measurement, with the exclusion recorded on the measurements. Demoable: run photometry on a session; flagged frames are skipped; results view shows how many frames were excluded and why.

**Blocked by:** frame-quality 01, photometry 04 (done)

**Status:** done

- [x] `RunController`/photometry run takes optional excluded-frame set (default: flagged frames of the session); excluded frames skipped in `photometry/run.py`, exclusion count surfaced in results
- [x] Measurements record exclusion (flag/count), CSV export unchanged or extended consistently
- [x] Tests: exclusion removes exactly the flagged frames from photometry input; results reflect it; full suite green

## Comments

Implemented on branch `wt/frame-quality-03-exclusion`.

- **Excluded-frame set**: `RunController.run_session(session_id, on_progress=None, *, excluded_frames=None)`. `None` (default) resolves to the session's flagged frames via the new glue `frame_quality.session.flagged_frame_reasons(project, session_id)` → `frame_id -> flag_reasons` map (reads `store.session_quality(...).flagged`). An explicit collection of ids excludes exactly those (no reasons); a mapping `frame_id -> reasons` also carries reasons; an explicit EMPTY collection excludes nothing (overrides the default). Invalid shapes (str/bytes, non-int ids, non-string reasons) raise `RunError` before the run starts.
- **Skipping**: excluded frames are skipped BEFORE any measuring — no FITS read, no store write — but still advance the `on_progress` `1..N` sequence. They are recorded on the frozen `RunReport` as `excluded: tuple[ExcludedFrame, ...]` (new `ExcludedFrame(frame_id, reasons)` dataclass), with `excluded_count`, `excluded_reasons` (reason→count, empty reasons count under `"flagged"`), and `exclusion_summary` (e.g. `"2 frames excluded (bg_std×1, fwhm×1)"`, `"no frames excluded"`). `RunReport.frames` now counts measured + skipped + excluded. New pure helper `format_exclusion_reasons(counts)`; both exported from `seestar_controller.photometry`.
- **Results view**: `ResultsPanel` gained an `exclusion_label` (Gtk.Label) showing `report.exclusion_summary` after a run; the progress label now reads `"N measured, N skipped, N excluded"` and the status line includes the excluded count. The Run button tooltip notes flagged frames are excluded by default.
- **Recording the exclusion — NO store schema change** (schema is owned by another workstream): the per-run exclusion is recorded best-effort into the project's `project.json` config under `config["photometry"] = {"exclude_flagged_frames": bool, "last_run": {"session_id", "at", "measured", "skipped", "excluded_count", "excluded": [{"frame_id", "reasons"}]}}` (written on the db-owning thread via `_call_store`, persisted with `project.save_config()`; any failure is logged and never fails the run). The run report itself carries the full `excluded` tuple in memory.
- **CSV export unchanged**: excluded frames never get measurement rows, and the export's existing OMIT policy for `excluded` points already keeps the file the clean lightcurve — so no CSV change was needed (consistent, not extended).
- **Tests**: `tests/test_photometry_run.py` (+9: default flagged exclusion incl. reasons/progress/lightcurve/config record + persisted json, explicit collection/empty/mapping overrides, exclusion-before-measuring via an all-zero flagged frame, validation error paths, no-quality-records run, pure `format_exclusion_reasons`/`ExcludedFrame`/`RunReport` defaults, public-API exports); `tests/test_frame_quality.py` (+3 glue tests for `flagged_frame_reasons` + export name); `tests/test_photometry_widgets.py` (+1: results panel exclusion label/progress/status after a run with one flagged sub; gi-skipped headless). No gi in pure modules, no network, module-set boundary tests untouched (all changes in existing modules).
