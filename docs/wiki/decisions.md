# Decisions (grilling session, 2026-05-19)

- Native UI: Python + GTK4/libadwaita via PyGObject; `uv` for packages.
- Network: same home LAN, direct IP to the scope. No cloud relay.
- Firmware 9.75 → signed PEM handshake required (fw >= 7.18).
- Photometry uses individual 10 s sub-frames (linear); stacks only for preview/enhance.
- Project = local folder `~/SeestarProjects/<name>/` (SQLite + FITS + lightcurve.csv + project.json); no cloud.
- Star selection: click on image + catalog assist; comp ensemble = mean of comps.
- Period: Lomb-Scargle auto + manual override + phase fold + CSV export.
- Control scope: connect, goto (name/RA-Dec), start/stop stack, exposure/gain, mosaic, park, preview, downloads, nightly plan/queue.
- Enhance: non-destructive stretch/asinh, denoise, sharpen, white balance, background subtraction.
- Specs split into 8 areas under `.scratch/<slug>/spec.md`; tickets under `.scratch/<slug>/issues/NN-*.md`.
- Each ticket = its own git commit(s). Local-only repo, no remote.
