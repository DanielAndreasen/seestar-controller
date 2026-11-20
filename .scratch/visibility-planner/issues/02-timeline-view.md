# 02: Night timeline view + apply suggested order

**What to build:** the GUI for the visibility planner: a night timeline plot (altitude curves for all queued targets, twilight bands, moon track — matplotlib Agg -> PNG -> PIL texture, same path as the analysis plots), PNG export, and the suggested queue order shown as a hint with an "Apply" button that reorders the PlanQueue. Demoable: with a queued plan, the timeline renders in the GUI with twilight/moon marked; Apply reorders the queue in the Observe view.

**Blocked by:** visibility-planner 01

**Status:** ready-for-agent

- [ ] Widget `VisibilityView` following analysis/lightcurve_view.py patterns (DrawingArea + Agg render): altitude curves per queued target, twilight shading, moon altitude/phase annotation
- [ ] Suggested order list + "Apply" button writing the reordered entries back to the observing PlanQueue (queue_controller)
- [ ] PNG export of the timeline
- [ ] Wired into the app (Observe view or its own tab — decide with existing patterns)
- [ ] Widget tests via the ui harness; full suite green

## Comments

(implementor fills in)
