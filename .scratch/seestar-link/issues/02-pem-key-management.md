# 02: PEM key management

**What to build:** the auth key lifecycle — load the PEM key from `~/.config/seestar-controller/key.pem` (path configurable), a clear first-run error explaining exactly how to extract the key from the official Seestar Android app (seestarpy `extract_pem`), and a small CLI helper that runs the extraction and saves the key to the config path.

**Blocked by:** 01 (transport must exist to know what the key signs)

**Status:** ready-for-agent

- [ ] Client loads the key from the configured path and uses it for the handshake
- [ ] Missing/invalid key produces an actionable error message naming the extraction step
- [ ] CLI helper extracts the key from the official app and writes it to the config path
- [ ] Tests: handshake signing with a test key; error path when key file is absent
