# 04: Adopt Python 3.14 performance/security features where they are free wins

**What to build:** evaluate the Python 3.14 language/runtime features (e.g. t-strings PEP 750, deferred annotation evaluation PEP 649/749, `compression.zstd` PEP 784, `concurrent.interpreters` PEP 734, free-threaded build, startup/JIT interpreter improvements, security hardening) and adopt ONLY the ones that improve this codebase WITHOUT adding complexity — no new abstractions, no config surface, no behavior change visible to users. Each adoption must be a small, local change (e.g. swap a stdlib call for its 3.14-native equivalent, use zstd where we already compress, replace an f-string SQL/HTML-ish construction with a t-string ONLY if it is strictly simpler and safer). If a feature does not apply or would add complexity, record that decision (one line, dated) in docs/wiki instead of forcing it. Demoable: suite green, ty/ruff clean, and a dated wiki entry lists adopted vs rejected features with reasons.

**Blocked by:** dependency-upgrade 01, 02, 03

**Status:** implemented in 157a1a0 (merged)

- [x] Dated docs/wiki entry: each candidate 3.14 feature -> adopted (with commit) or rejected (with reason)
- [x] Adopted changes are minimal and local; no new dependencies, no new config
- [x] Full suite green; `uv tool run ty check` + `uv tool run ruff check` clean

## Comments

Retroactive tick (app-improvement issue 17, docs-only): the Status line above already records the merge, so the acceptance boxes are checked to match it. Implementing commit `157a1a0`; test list: docs/wiki/dependency-upgrade.md issue-04 section (adopted vs rejected features); full suite green. Re-verified during the issue-17 audit: all referenced commits exist on main and the listed targeted tests pass (`uv run pytest <files> -q -n auto`).
