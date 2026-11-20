# Assumptions (user AFK)

Recorded 2026-05-19 by chief of staff. User is AFK and cannot provide the PEM key or the scope. Revisit when back.

- No PEM key available → all `seestar-link` work is implemented and tested against the **mock scope** only. Real-scope integration is deferred and unverified.
- Scope IP is a placeholder in settings; same-LAN direct connection assumed (TCP :4700, images :4800/:4804).
- Firmware 9.75 → signed handshake implemented per seestarpy's approach (`extract_pem` from the official Android app, key at `~/.config/seestar-controller/key.pem`), but unverified against real hardware.
- Stack: Python 3.12 + GTK4/libadwaita (PyGObject), managed with `uv`, on Linux Mint 22.3 / XFCE.
- **Sandbox has no GTK**: checked 2026-11-20 — no `gi` module, no gtk4/libadwaita system libs (pkg-config fails), no Xvfb. PyGObject cannot be installed/built here. Therefore the `ui` package must keep ALL logic in pure-Python controller/state modules (fully tested headless), import `gi` only inside the widget/app modules, and widget smoke tests must use `pytest.importorskip("gi")` so they skip in this sandbox and run on the user's real desktop. `uv add pygobject` is NOT possible here — do not add it as a hard dependency; document that the user installs system packages (`python3-gi gir1.2-gtk-4.0 gir1.2-adw-1`) on the desktop machine.
