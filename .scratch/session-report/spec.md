# Spec: session-report

Status: ready-for-agent (3 issues; 02 blocked by frame-quality/moving-objects)

## Problem Statement

After a night the user wants a one-page summary of what happened and what was measured — images, lightcurve, period, quality — without assembling it by hand.

## Solution

A session-report module: generate a self-contained HTML report per session (and a PDF export), embedding the best stack (enhanced thumbnail), the session log, the lightcurve with the current period/fold, frame-quality summary, and detected movers; saved into the project.

## User Stories

1. As an observer, I want a report generated per session with one click, so that I don't assemble it by hand.
2. As an observer, I want the report to include the enhanced stack thumbnail, session log, lightcurve + fold, quality summary, and movers, so that it covers the night.
3. As an observer, I want the report as a self-contained HTML file, so that I can open and share it.
4. As an observer, I want a PDF export of the same report, so that I can archive/print it.
5. As an observer, I want the report saved into the project, so that it persists.
6. As an observer, I want to regenerate the report after more data arrives, so that it stays current.

## Implementation Decisions

- HTML: single file, embedded base64 PNGs (thumbnails, plots rendered via matplotlib Agg); PDF via headless browser print or reportlab — decide in implementation (open question).
- Content pulled from project-store (sessions, measurements), period-analysis (period/fold), frame-quality, moving-objects, image-enhance (thumbnail).
- Module boundary: pure Python (project data → HTML string + assets); view triggers and opens.

## Testing Decisions

- Good test = temp project with one synthetic session: report HTML contains the expected sections and embedded images; PDF export produces a non-empty file; regeneration overwrites.
- Prior art: project-store session tests, period-analysis CSV export tests.

## Out of Scope

- Multi-session campaign reports, public sharing links.

## Further Notes

- Depends on project-store, observing, photometry, period-analysis, frame-quality, moving-objects, image-enhance.

## Open Questions

- PDF path: headless Chromium print-to-PDF vs reportlab rebuild — propose Chromium if available on the desktop, else reportlab.
- Report per session or per project (all sessions)? Propose per session, with a project-level index later.
