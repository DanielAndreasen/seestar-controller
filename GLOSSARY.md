# GLOSSARY.md

Canonical vocabulary for the Seestar Controller project. No implementation details here.

- **Scope** — the Seestar S50 unit standing in the garden.
- **Link** — the control connection to the scope: JSON-RPC on TCP :4700 plus image sockets :4800 (tele) and :4804 (wide).
- **Preview** — the live image stream of the current target from the scope.
- **Sub-frame** — one individual exposure frame (10 s, linear, unstretched). The unit of photometry.
- **Stack** — the stacked FITS image the scope produces. Used for preview/enhancement, not for photometry.
- **Plan (queue)** — the ordered list of targets for one night.
- **Project** — an on-disk observing campaign for one target: a directory holding its database, frames, stacks, and lightcurve. Accumulates across nights.
- **Session** — one night's observing run within a project.
- **Target star** — the (usually variable) star whose brightness is monitored.
- **Comparison star (comp)** — a standard star used as reference. The **comp ensemble** is the average of all selected comps.
- **Measurement** — one differential magnitude data point (target minus comp ensemble) at a point in time.
- **Lightcurve** — the series of measurements over time, across all sessions of a project.
- **Period** — the variability period of the target star, found automatically or given manually.
- **Fold** — phase-folding the lightcurve by a period to cover the full curve with one cycle.
- **Enhance** — non-destructive image processing applied to a stack (stretch, denoise, sharpen, white balance, background subtraction).
