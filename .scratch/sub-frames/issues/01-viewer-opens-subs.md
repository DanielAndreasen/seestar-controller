# 01: Viewer opens any stored frame (subs included)

**What to build:** the image viewer (Enhance view) lists and opens ALL stored frames of the current project — subs, stacks, enhanced — not just kind='stack'. The Stored-data browser frame rows in the Projects view open the clicked frame in the viewer. Demoable: open NGC7789 offline (two subs, empty stacks), the Enhance stack/frame list shows both subs, click one -> image renders in the viewer.

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] `enhance/viewer_controller.py` lists all frames (new `list_project_frames` or generalize `list_project_stacks`); `open_stack`/open works for sub frame ids (RGB->luminance path reused)
- [ ] Enhance view list shows all frames with kind label; click opens in viewer
- [ ] Projects view "Stored data" frame rows clickable -> open frame in viewer
- [ ] Widget tests via ui harness (gi-gated, `.emit("clicked")`, no destroy()); pure tests for listing/opening subs; full suite green; ty + ruff clean

## Comments

(implementor fills in)
