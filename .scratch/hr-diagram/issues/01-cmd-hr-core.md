# 01: CMD / H-R computation core (pure module)

**What to build:** the pure (no-gi) core of the hr-diagram spec: take a gaia-crossmatch table of a stack and produce (a) apparent CMD points (G vs BP-RP) and (b) H-R points (absolute G = G + 5 - 5*log10(1000/parallax_mas) vs BP-RP, with parallax SNR cut parallax/parallax_error > 5), plus cluster membership filtering by sigma-clipping around the median parallax and proper motion. Demoable: synthetic crossmatch table with an injected cluster main sequence + field stars -> membership filter keeps cluster stars, absolute-G math correct, CMD/H-R point lists and CSV export produced.

**Blocked by:** gaia-crossmatch 01 (crossmatch table per stack — not yet ticketed; write gaia-crossmatch issues first or together with this)

**Status:** implemented in 0c27429 (merged)

- [ ] Pure `hr_diagram/` (or `analysis/hr.py`) package, no gi import (subprocess boundary test like photometry's)
- [ ] `cmd_points(table)` and `hr_points(table, parallax_snr=5.0)` -> frozen point records (source_id, g, bp_rp, abs_g, member flag, errors)
- [ ] `membership_filter(table)` sigma-clip on parallax + pm around median (documented defaults)
- [ ] CSV export of the table (round-trip test), following analysis/export.py conventions
- [ ] Tests: synthetic table with known cluster -> filter keeps cluster, drops field; abs-G math within tolerance; CSV round-trip; no-gi boundary
- [ ] Full suite green

## Comments

(implementor fills in)
