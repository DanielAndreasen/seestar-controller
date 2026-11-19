# Assumptions (user AFK)

Recorded 2026-05-19 by chief of staff. User is AFK and cannot provide the PEM key or the scope. Revisit when back.

- No PEM key available → all `seestar-link` work is implemented and tested against the **mock scope** only. Real-scope integration is deferred and unverified.
- Scope IP is a placeholder in settings; same-LAN direct connection assumed (TCP :4700, images :4800/:4804).
- Firmware 9.75 → signed handshake implemented per seestarpy's approach (`extract_pem` from the official Android app, key at `~/.config/seestar-controller/key.pem`), but unverified against real hardware.
- Stack: Python 3.12 + GTK4/libadwaita (PyGObject), managed with `uv`, on Linux Mint 22.3 / XFCE.
