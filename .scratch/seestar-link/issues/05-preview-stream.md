# 05: Preview stream

**What to build:** decoding and streaming of the live preview — frames arriving on the tele (:4800) and wide (:4804) image sockets are decoded into images and delivered to a caller-provided callback at displayable rate. Demoable: mock scope pushes frames, client callback receives decoded images.

**Blocked by:** 01

**Status:** ready-for-agent

- [ ] Frames from :4800 and :4804 are decoded (format per protocol) into image arrays
- [ ] Callback receives frames without blocking the socket reader
- [ ] Stream can be started/stopped independently of the control connection
- [ ] Tests: mock scope streams N frames, client receives all N decoded correctly
