# Issue 05: the Enhance/Photometry viewer list shows ALL frames — the stack is buried among dozens of subs

**Status:** implemented in bfee3c4 (pending review)

## Found while
Live session 2026-11-22, project `vw-cep` (78 subs + 1 stack). In the Enhance view's left stack list the single stack (`stacks/<session>/2461367.314371.fits`) is nearly impossible to find among the sub rows.

## Cause
`src/seestar_controller/enhance/viewer_controller.py` — `refresh_stacks()` lists **all** stored frames (subs, stacks, enhanced) plus file-based mosaics; each row only carries a `kind` attribute, there is no filter, sort, or grouping, and no kind label in the row text.

## Expected
- The list defaults to (or offers a filter for) **stacks only** — subs/enhanced/mosaics behind a toggle or separate group.
- Rows show the kind (e.g. "stack", "sub", "mosaic") and newest-first ordering, so the latest stack is on top.
- Same for the Photometry star-picker stack list (it shares the viewer controller).

## Scope
`src/seestar_controller/enhance/viewer_controller.py`, `src/seestar_controller/enhance/viewer.py`, `src/seestar_controller/photometry/star_picker.py`, tests in `tests/test_enhance_viewer*.py` / `tests/test_photometry*.py`.
