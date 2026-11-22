# Issue 01: stacks are blurry vs the subs — alt-az field rotation not compensated

**Status:** needs-triage

## Found while
Live session 2026-11-22, project `vw-cep` (session 20261122T192149035964Z, 10 s subs, Seestar S50 alt-az). The downloaded device stack (`stacks/<session>/2461367.314371.fits`) is visibly much blurrier than individual subs and fails to plate-solve; a sharp sub solves fine.

## Cause
- App-side: `stacking/session.py::stack_session_subs` median-combines subs **pixel-wise with no registration/derotation** (documented assumption "scope subs are already aligned"). On an alt-az mount the field rotates during a session, so a long pixel-wise median smears stars into arcs.
- Device-side: the scope's own live stack shows the same drift/derotation blur and the app just downloads it as-is.

## Expected
- App-side stacking registers + derotates subs (or at least warns when total field rotation over the session exceeds ~1 star FWHM and offers a cropped/limited subset).
- The UI steers users to solve a sub (sharp) rather than the blurry stack; solve-failure hint could say "try a single sub".
- Documented assumption in stacking/session.py updated once registration exists.

## Scope
`src/seestar_controller/stacking/session.py`, `src/seestar_controller/stacking/core.py`, Projects view **Stack subs** flow, astrometry solve error hint, tests in `tests/test_stacking*.py`.
