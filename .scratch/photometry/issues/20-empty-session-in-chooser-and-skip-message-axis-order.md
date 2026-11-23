# Issue 20: minor — empty session appears in the Run chooser; skip-reason message prints width×height as "1080x1920" which reads like height×width

**Status:** completed (fixed in 8ae9909, merged to main; implemented+reviewed by main agent — subagent name limit reached)

## Found while
Headless lightcurve run 2026-11-22 (vw-cep): session `20261122T221755525766Z` has 0 subs but appears in the session chooser (a valid but useless empty run); the outside-image skip message renders the frame shape as "1080x1920" (width×height) which users read as height×width.

## Expected
- Either hide zero-sub sessions from the chooser or mark them "(empty)"; skip message should say e.g. "outside the 1920×1080 px image (height×width)" or use unambiguous wording.

## Scope
`photometry/run.py` (`sessions()`), `photometry/measure.py` (message), tests.
