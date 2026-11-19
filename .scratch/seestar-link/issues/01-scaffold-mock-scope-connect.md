# 01: Scaffold + mock scope + signed connect

**What to build:** the foundation slice — a `uv`-managed Python project, a mock scope server that speaks the real wire protocol (JSON-RPC on TCP :4700, image sockets :4800/:4804), and a client transport that performs the PEM-signed handshake (firmware 9.75), correlates requests/replies by id, dispatches pushed events, and maintains a `test_connection` heartbeat. Demoable: client connects to the mock scope and stays connected.

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] Mock scope server runs standalone and accepts a JSON-RPC connection on :4700
- [ ] Client connects to the mock scope with signed handshake and receives a successful reply
- [ ] Request/reply correlation by id works for concurrent requests
- [ ] Pushed events from the scope are dispatched to a registered handler
- [ ] Heartbeat keeps the connection alive; dropping it is detected
- [ ] Unit tests cover envelope encode/decode and id correlation without real sockets where possible
