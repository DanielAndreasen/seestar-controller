# 05: A frame whose store fails is never re-downloaded — capture watermark advances at download time

**What to build:** make SessionCapture advance the per-kind `since` watermark only after the frame is successfully stored, so a transient store failure (disk full, db lock, unreadable FITS) is retried on a later poll instead of silently dropping the frame for the rest of the night.

Evidence: `src/seestar_controller/observing/capture.py:589` sets `self._since[kind] = max(meta.epoch, self._since[kind])` inside `_download_new_frames`, while `capture.py:606-631` (`_apply`) catches the `store_frame` exception, logs a KIND_ERROR event, and continues — the id is added to `_stored_ids` only on success, but the already-advanced watermark excludes the frame from every future `download_frames(since=...)` poll. Only a capture restart (watermark re-seeded to session start) would recover it.

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] A simulated `store_frame` failure leaves the frame eligible for re-download on the next poll; after the failure is fixed, the frame is stored
- [ ] Watermark never advances past an unstored frame
- [ ] `uv run pytest tests/test_observing_capture.py -q` (plus new regression test) passes; full suite green; ty + ruff clean

## Comments

Audit finding (kind: bad-implementation, severity: medium; silent data loss). Fix direction: move the watermark update into `_apply` after `store_frame` succeeds, or roll it back on failure. Covered-by pre-audit: `.scratch/observing/issues/04-session-logging-frame-downloads.md`.
