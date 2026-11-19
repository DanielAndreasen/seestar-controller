# Progress

Ticket status log. Updated as tickets complete. Dates are commit dates.

## seestar-link

- 2026-11-19: **01 scaffold + mock scope + signed connect** — implemented `353cfa2`, review fixes `6660003`, ticket marked `ab9e667`. Review verdict: APPROVE. 52 tests green. Learnings: wire shapes from seestar_alp (RSA-SHA1 PKCS#1 v1.5 handshake, 9.75 `key_envelope` reply, 20-byte image frame header `>HHHIHHBBHH`); Python pinned to 3.12 (`>=3.12,<3.13` + `.python-version`); heartbeat liveness = last inbound activity, heartbeat_timeout 30 s >> request_timeout 10 s; `RequestTimeoutError` distinct from `ConnectionLostError`; image frames capped at 64 MiB.
- 2026-11-19: **02 PEM key management** — implemented `9665b2e`. 81 tests green. Key path resolution: explicit arg > `SEESTAR_KEY_PATH` env var (same name seestarpy auto-discovers) > `~/.config/seestar-controller/key.pem`. Missing/invalid key raises `MissingKeyError`/`InvalidKeyError` naming the extraction step (`python -m seestarpy.extract_pem <apk>` / `uv run seestar-controller extract-key <apk>`); key file written 0600 (chmod after write so pre-existing looser files are tightened). Extraction implements seestarpy's documented flow locally (seestarpy is not a dependency): APK/XAPK as zip → strings-scan `lib/*/libopenssllib.so`, fallback to all entries, XAPK nested base APKs; first block parsing as an unencrypted RSA private key wins. CLI: `seestar-controller extract-key <apk|xapk> [--key-path P] [--force]` (console script + `python -m seestar_controller`), plus `key-path`. Transport loads the handshake key via `keys.load_key()` so `connect()` signs with the configured key; `pem=`/`private_key=` overrides kept for tests. Still unverified against a real app package / real scope (user AFK).
- 03–07 pending.

## Other specs

- Not started. Order: catalog, project-store, ui-shell, observing, image-enhance, photometry, period-analysis.
- 2026-11-19: **02 PEM key management** — implemented `9665b2e`, marked `a996183`. Review verdict: APPROVE (81 tests). Follow-up nits (low): F1 write-then-chmod window in keys.save_key (prefer chmod-after-open or temp+rename); F2 CLI extract-key should catch OSError for clean error line; F3 warn on world-readable key file; F4 add no-key-material-in-logs test.
