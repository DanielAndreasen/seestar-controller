# 02: MPC lookup for movers + annotated overlay view

**What to build:** (a) optional MPC query for each detected mover (catalog module HTTP+cache pattern, mocked in tests) matching by sky track/speed, persisted alongside movers.json; (b) the GUI: annotated overlay/blink on session frames showing mover tracks (reuse the analysis/enhance viewer patterns). Demoable: synthetic session with a mover -> MPC lookup mocked returns a known-object match recorded in the project; overlay view renders the track on the frame.

**Blocked by:** moving-objects 01

**Status:** ready-for-agent

- [ ] Pure MPC lookup in moving_objects/ (catalog HTTP+cache pattern, no network in tests — mocked); match result persisted in movers.json sidecar (schema_version bump within the file, no store schema change)
- [ ] Widget: overlay/blink view rendering mover tracks on session frames (gi-at-top, PEP 562 lazy export, ty-override + audit lists); wired into the app (session/results view — follow existing patterns)
- [ ] Widget tests via ui harness; full suite green

## Comments

(implementor fills in)
