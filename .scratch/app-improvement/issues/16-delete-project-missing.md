# 16: Deleting a project is promised by the project-store spec but implemented nowhere

**What to build:** resolve the spec conflict and provide the capability: project-store spec user story 9 ("list and delete old projects") has no implementation, while the projects-view spec explicitly lists "Deleting/renaming projects" as Out of Scope. Either implement project deletion (store-level `delete_project` plus UI affordance) or move story 9 to Out of Scope and align both specs.

Evidence: `.scratch/project-store/spec.md` user story 9 vs `grep -rn "def delete" src/seestar_controller/` → only preset deletion (`enhance/persistence.py`, `store/presets.py`); no delete_project in `store/`; none of the 7 project-store issues covers deletion; `.scratch/projects-view/spec.md` declares it out of scope.

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] Decision recorded in both specs (implement vs descope) and acted on: if implemented, `store.delete_project` removes the project directory/DB safely (refuse when open, tests) and the Projects view offers deletion; if descoped, story 9 is moved to Out of Scope in the project-store spec
- [ ] Full suite green; ty + ruff clean

## Comments

Audit finding (kind: missing-feature, severity: low). Spec-vs-spec conflict — nothing implements it either way.
