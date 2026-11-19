# Progress

Ticket status log. Updated as tickets complete. Dates are commit dates.

## seestar-link

- 2026-11-19: **01 scaffold + mock scope + signed connect** — implemented `353cfa2`, review fixes `6660003`, ticket marked `ab9e667`. Review verdict: APPROVE. 52 tests green. Learnings: wire shapes from seestar_alp (RSA-SHA1 PKCS#1 v1.5 handshake, 9.75 `key_envelope` reply, 20-byte image frame header `>HHHIHHBBHH`); Python pinned to 3.12 (`>=3.12,<3.13` + `.python-version`); heartbeat liveness = last inbound activity, heartbeat_timeout 30 s >> request_timeout 10 s; `RequestTimeoutError` distinct from `ConnectionLostError`; image frames capped at 64 MiB.
- 02–07 pending.

## Other specs

- Not started. Order: catalog, project-store, ui-shell, observing, image-enhance, photometry, period-analysis.
