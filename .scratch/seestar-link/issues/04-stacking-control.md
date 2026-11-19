# 04: Stacking control

**What to build:** stacking operations on the facade — start/stop stacking, set exposure and gain, set mosaic framing, and receive stack progress events (frame count, elapsed). Demoable: against the mock scope, start a stack with chosen exposure/gain/mosaic, watch progress events, stop it.

**Blocked by:** 03

**Status:** implemented in 5d3b33d

- [x] `start_stack(exposure, gain, mosaic)` begins stacking on the mock scope
- [x] `stop_stack()` ends it cleanly
- [x] Progress events (frames stacked, elapsed) reach the caller
- [x] Invalid parameter values are rejected with a clear error
- [x] Tests against the mock scope cover start/progress/stop
