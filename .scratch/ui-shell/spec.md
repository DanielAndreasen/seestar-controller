# Spec: ui-shell

Status: ready-for-agent

## Problem Statement

The app must feel native on the user's Linux Mint / XFCE desktop and give access to all features: observing, enhancement, photometry, lightcurves, projects.

## Solution

A GTK4/libadwaita application (PyGObject, managed with `uv`) with a header bar (connection status + connect button), a sidebar navigating between views — Observe, Enhance, Photometry, Lightcurve, Projects — and a settings dialog for scope IP, PEM key path, and project root. Dark theme. Window state persisted.

## User Stories

1. As a user, I want the app to look and behave like a native Linux app, so that it fits my desktop.
2. As a user, I want a single window with sidebar navigation between Observe, Enhance, Photometry, Lightcurve, Projects, so that I always know where I am.
3. As a user, I want a connect button with clear status (disconnected/connecting/connected/error), so that I know the link state.
4. As a user, I want a settings dialog for scope IP, PEM key path, project root, so that I configure the app once.
5. As a user, I want the window size/position remembered, so that the app opens as I left it.
6. As a user, I want a dark theme, so that night-time use doesn't blind me.
7. As a user, I want long-running operations (connect, goto, download) to not freeze the UI, so that the app stays responsive.
8. As a user, I want errors (connection failed, scope busy) shown as notifications, so that I'm informed without modal dialogs.

## Implementation Decisions

- Toolkit: GTK4 + libadwaita via PyGObject; Python 3.12; `uv` for the venv and dependencies.
- Architecture: MVC-ish — each view is a widget module; a controller layer holds app state and talks to seestar-link / project-store / etc.; controllers contain the logic, widgets stay thin, so controllers are testable headless.
- Async: GLib main loop; blocking work (socket IO, photometry) on worker threads with results marshalled back via `GLib.idle_add`.
- Views: Observe (plan/queue + preview), Enhance (image + sliders), Photometry (star picking + run), Lightcurve (plots), Projects (list/create/open).
- Settings: GSettings or a JSON file in the config dir; JSON simpler, decide at implementation.

## Testing Decisions

- Good test = controller logic without a display: navigation state, settings load/save, connection state machine transitions driven by a mock link.
- Widget smoke test: instantiate each view headless (GTK can run with a dummy backend) to catch construction errors.

## Out of Scope

- Mobile/responsive layouts, custom theming beyond dark mode.
- System tray integration (nice-to-have, not required).

## Further Notes

- Desktop is XFCE on Linux Mint 22.3; GTK is the native toolkit there.
