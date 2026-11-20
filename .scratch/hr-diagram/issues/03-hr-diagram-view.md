# 03: H-R diagram view (CMD + H-R, click-to-highlight, export)

**What to build:** the GUI view for hr-diagram: render the apparent CMD and H-R diagram of a stack's crossmatched stars (matplotlib Agg -> PNG -> PIL -> Gtk texture, the same path image-enhance uses), with isochrone overlay, zoom/pan, error-bar toggle, click a star in the diagram to highlight it on the stack image (and vice versa), and export plot PNG + table CSV. Demoable: open a project stack with a crossmatch table; both diagrams render in the GUI with overlay; clicking a point highlights the star on the image; PNG/CSV export works.

**Blocked by:** hr-diagram 01, hr-diagram 02, gaia-crossmatch view (star positions on image)

**Status:** ready-for-agent

- [ ] Widget `HrDiagramView` following analysis/lightcurve_view.py patterns (DrawingArea + matplotlib Agg render to PNG, PIL texture); CMD and H-R as switchable pages
- [ ] Zoom/pan (re-render on zoom) + error-bar toggle; click-to-highlight both ways with the image viewer
- [ ] PNG export of the plot + CSV export of the table (reuse analysis/export.py)
- [ ] Wired into the app (new tab/section next to the lightcurve view; decide with existing ui-shell patterns)
- [ ] Widget tests via the ui harness; full suite green

## Comments

(implementor fills in)
