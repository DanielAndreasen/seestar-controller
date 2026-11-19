# 03: Enhance view — sliders, before/after, undo/redo

**What to build:** the Enhance view UI — per-op sliders, live before/after comparison, undo/redo of parameter changes. Demoable: open a stack, move sliders, image updates live, undo restores previous state.

**Blocked by:** 01, 02, ui-shell 01

**Status:** ready-for-agent

- [ ] Slider per pipeline parameter, live re-render
- [ ] Before/after toggle or split view
- [ ] Undo/redo stack for parameter changes
- [ ] UI stays responsive during re-render (worker thread)
- [ ] Tests: view controller with mock pipeline; undo/redo logic
