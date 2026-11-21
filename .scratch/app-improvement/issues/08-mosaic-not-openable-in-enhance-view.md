# 08: Stitched mosaic cannot be opened in the enhance view (mosaic-stitch spec story 4)

**What to build:** make the stitched mosaic openable in the Enhance view like any other image, as the mosaic-stitch spec promises ("open the stitched image in the enhance view" / "enhance pipeline consumes it unchanged") — or formally descope story 4 in the spec if a UI flow is not wanted.

Evidence: `.scratch/mosaic-stitch/spec.md` stories 2-4 + Implementation Decisions vs `src/seestar_controller/mosaic/session.py:70-96` (file-only `mosaics/<sid>.fits`, "no store schema bump" per issue 01), `enhance/viewer_controller.py:191` `open_stack(frame_id)` and `enhance/fits_io.py:282-284` `list_project_stacks(kind="stack")` — the Enhance view lists/opens only store frames; `ui/views.py:1683-1689` "stitch" action only writes the FITS and prints the path. No code path registers or opens the mosaic in the enhance viewer.

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] Either: after stitching, the mosaic can be opened in the Enhance view (register the mosaic FITS as a frame — store schema bump — or add an "open file" path to the viewer plus a mosaic row in the Enhance view); or: spec story 4 is moved to Out of Scope with a note
- [ ] Chosen path covered by tests; `uv run pytest tests/test_mosaic_stitch.py tests/test_enhance_widgets.py -q` passes; full suite green; ty + ruff clean

## Comments

Audit finding (kind: missing-feature, severity: medium). Spec story vs implementation conflict.
