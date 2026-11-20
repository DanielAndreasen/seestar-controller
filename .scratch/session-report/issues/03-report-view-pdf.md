# 03: Report trigger in the GUI + PDF export

**What to build:** the GUI: a "Generate report" action per session (Observe session log dialog or results view) that runs the report on a worker thread, saves it into the project, and opens it (Gtk.show_uri); plus PDF export of the same report (headless Chromium print-to-PDF if available on the desktop, else reportlab rebuild — decide in implementation and document). Demoable: click generate -> HTML in project + opens; PDF export produces a non-empty file.

**Blocked by:** session-report 01 (02 optional for this slice)

**Status:** ready-for-agent

- [ ] "Generate report" button wired via the app (worker thread, GLib.idle_add marshalling per existing patterns); opens the HTML; lists saved reports per session
- [ ] PDF export producing a non-empty PDF next to the HTML
- [ ] Widget tests via the ui harness; full suite green

## Comments

(implementor fills in)
