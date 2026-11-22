# Issue 06: a VizieR asu-tsv mirror answering with a non-TSV page aborts the whole standards lookup instead of trying the next mirror

**Status:** needs-triage

## Found while
Live session 2026-11-22 (project vw-cep): the Catalog comps **Search** failed with
`catalog lookup failed: VizieR asu-tsv response is missing column(s) ['RAJ2000', 'DEC2000', 'Vmag']`.
Verified afterwards against the live CfA and IDIA mirrors: both return a proper TSV header
(`RAJ2000, DEJ2000, ..., Vmag, ...`) and the current `find_standard_stars` code returns 100
standards around VW Cep — so the failure was a malformed/error-page response at that moment.

## Cause
`catalog/standards.py::fetch_standards_payload` tries CDS TAP, then the asu-tsv mirrors in
order, but a **malformed body** from an endpoint that did answer raises
`StandardsResponseError` immediately (documented behavior) — an HTML error page, a rate-limit
page, or a truncated TSV from one mirror kills the whole lookup instead of falling through to
the next mirror. Also the error lists the *expected* columns without showing what the response
actually contained, which made the failure hard to diagnose (the quoted list does not even match
the real header).

## Expected
- `parse_asu_tsv` failures (missing columns, no header) are treated like endpoint failures:
  recorded and the next mirror is tried; `StandardsResponseError` only when every endpoint
  answered with a malformed body (include a snippet of the first response line in the message).
- Network/HTTP failures already fall through; keep that.

## Scope
`src/seestar_controller/catalog/standards.py` (`fetch_standards_payload` attempt loop,
`parse_asu_tsv` error text), tests in `tests/test_catalog_standards*.py` (mirror returns garbage
-> next mirror used; all garbage -> aggregated error with snippet).
