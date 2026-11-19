# 07: Crash safety + schema migrations

**What to build:** hardening the store — all writes atomic (SQLite transactions, temp+rename for files), the db opens cleanly after a simulated kill mid-write, and a schema version pragma enables future migrations. Demoable: kill a write mid-transaction in a test, reopen, db is consistent.

**Blocked by:** 01

**Status:** ready-for-agent

- [ ] Schema has a `user_version` pragma and a migration runner
- [ ] Simulated crash mid-write leaves db consistent on reopen
- [ ] File writes are temp+rename atomic
- [ ] Tests: crash simulation, migration from version 0 to 1
