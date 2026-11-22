# 08: Show the first stored sub as preview in the Observe view

**Found during:** live guided test (project ngc869, 2026-11-22), indoors with the solar filter. The Observe view's right-pane preview is fed only by the scope's live preview stream (`link.preview.PreviewStream`); when that stream is absent (or not informative, e.g. solar filter indoors) the pane stays on "no preview yet" even though subs are being stored. The user wants to see the **first stored sub/exposure** in the Observe page to confirm there is light on the sensor.

**What to build:** when the first sub of the current session is stored (`SessionCapture` -> `store_frame`), load it (downscaled) and display it in the Observe view preview area (fallback when the live preview stream has no frame yet, or a dedicated "first exposure" thumbnail next to the live preview). Must be main-loop safe (`invoke_on_main`) and cheap (decode once, cache texture). Acceptance: start a night, wait for sub 1 to be stored -> Observe view shows that image even with the scope preview stream idle; live preview still takes over once streaming.

**Blocked by:** None (observing 01-05 done; relates to 07 live refresh)

**Status:** open — found in live test 2026-11-22

- [ ] Observe view can render a stored FITS (downscaled) as preview fallback / first-exposure thumbnail
- [ ] Wired to the first stored frame of the running session
- [ ] Tests: store first sub -> observe view preview updates without live stream
- [ ] Full suite green
