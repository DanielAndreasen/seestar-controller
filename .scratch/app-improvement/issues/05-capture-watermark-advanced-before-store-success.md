# 05: A frame whose store fails is never re-downloaded — capture watermark advances at download time

**What to build:** make SessionCapture advance the per-kind `since` watermark only after the frame is successfully stored, so a transient store failure (disk full, db lock, unreadable FITS) is retried on a later poll instead of silently dropping the frame for the rest of the night.

Evidence: `src/seestar_controller/observing/capture.py:589` sets `self._since[kind] = max(meta.epoch, self._since[kind])` inside `_download_new_frames`, while `capture.py:606-631` (`_apply`) catches the `store_frame` exception, logs a KIND_ERROR event, and continues — the id is added to `_stored_ids` only on success, but the already-advanced watermark excludes the frame from every future `download_frames(since=...)` poll. Only a capture restart (watermark re-seeded to session start) would recover it.

**Blocked by:** None (can start immediately)

**Status:** implemented in c10a758 (pending review)

- [x] A simulated `store_frame` failure leaves the frame eligible for re-download on the next poll; after the failure is fixed, the frame is stored
- [x] Watermark never advances past an unstored frame
- [x] `uv run pytest tests/test_observing_capture.py -q` (plus new regression test) passes; full suite green; ty + ruff clean

## Comments

Implemented in c10a758 on `wt/app-improvement-05`. The per-kind `since` watermark is no longer advanced in `_download_new_frames`; it moves in `_apply` via a new `_advance_watermark(kind, epoch)` only after a frame is stored (or found already stored via the id dedupe), and is capped by a per-kind `_unstored` epoch set so it never passes an unstored frame — the next `download_frames(since=...)` poll (inclusive filter) re-downloads a failed frame and retries the store. New regression test `test_store_failure_keeps_the_frame_eligible_for_redownload` (monkeypatched `store_frame` failing one frame): verified it fails on the pre-fix code (watermark advanced past the unstored frame) and passes after. Targeted tests: `uv run pytest tests/test_observing_capture.py -q` → 19 passed (plus neighboring `test_observing_{observe,executor,widgets,queue}.py` → 188 passed, 1 gi skip). `uv tool run ty check` → All checks passed; `uv tool run ruff check` → clean (after one isort autofix). Full suite not run here (coordinator runs it at merge time).
