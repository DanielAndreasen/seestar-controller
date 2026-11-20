# Spec: seestar-link

Status: completed 2026-11-20 — all 7 issues implemented and reviewed

## Problem Statement

The Seestar S50 has no native desktop control on Linux — the official app is mobile-only. The user wants to sit in the office and control the scope standing in the garden over the home LAN: connect, point, stack, watch the live preview, and download frames.

## Solution

A Python protocol client that speaks the scope's native protocol directly: JSON-RPC-style messages on TCP :4700 (requests, replies, pushed events, heartbeat) and image streams on :4800 (tele) and :4804 (wide). Firmware 9.75 requires the signed handshake, so the client signs with a PEM key extracted once from the official app and stored locally. The client exposes a device facade: connect/disconnect, goto, start/stop stacking, exposure/gain, mosaic framing, park, live preview, and frame downloads.

## User Stories

1. As an observer, I want to enter the scope's IP and connect, so that I control it from my laptop.
2. As an observer, I want the app to use my extracted PEM key automatically, so that firmware 9.75's signed handshake succeeds without manual steps each time.
3. As an observer, I want to be guided through extracting the PEM key on first run, so that setup is a one-time step.
4. As an observer, I want to see the scope's current state (mode, target, stacking progress) after connecting, so that I know what it is doing.
5. As an observer, I want to slew to RA/Dec coordinates, so that I can point at any target.
6. As an observer, I want to start and stop stacking, so that I control data collection.
7. As an observer, I want to set exposure and gain per target, so that I optimize for bright vs. faint targets.
8. As an observer, I want to set mosaic framing, so that large targets fit the field of view.
9. As an observer, I want to park the scope, so that it shuts down safely at the end of the night.
10. As an observer, I want a live preview stream of the current target, so that I see what the scope sees.
11. As an observer, I want to download sub-frames and stacks to my project, so that I can process and measure them locally.
12. As an observer, I want the connection to survive brief network hiccups (heartbeat + reconnect), so that a WiFi blip doesn't kill the session.
13. As an observer, I want pushed events (stack progress, errors) surfaced, so that I can react without polling.
14. As a developer, I want a mock scope server, so that the whole app is testable without the physical scope.

## Implementation Decisions

- Protocol: JSON messages with id/method/params envelope on TCP :4700; replies matched by id; events dispatched by method name; `test_connection` heartbeat.
- Image: separate sockets :4800 (tele) and :4804 (wide) delivering preview/stacked frames.
- Auth: firmware >= 7.18 signed handshake; PEM key extracted via seestarpy's `extract_pem` from the official Android app; stored at `~/.config/seestar-controller/key.pem`; path configurable.
- Reference implementation for message shapes: the open-source `seestar_alp` / `seestarpy` projects; vendor them as a dependency or reimplement the needed subset — decide during implementation, prefer dependency if it covers fw 9.75.
- Modules: protocol transport (socket + JSON-RPC correlation), auth (signing), image stream (frame decoding), device facade (high-level operations), mock scope (test double implementing the same wire protocol).
- Async model: integrate with the GLib main loop (GTK app); all socket IO off the UI thread; thread-safe event queue into the UI.
- Reconnect with exponential backoff; surface connection state to the UI.

## Testing Decisions

- Good test = wire-level behavior against a mock scope server: correct request envelopes, reply correlation, event dispatch, handshake signing, image frame decoding, reconnect after dropped socket.
- Modules tested: transport, auth, image stream, device facade — all against the mock scope.
- No test requires the physical scope; an optional manual integration script may target a real scope.

## Out of Scope

- ZWO cloud relay / remote access outside the LAN (VPN is the user's own concern).
- INDI/Alpaca bridge, KStars integration.
- SSH jailbreak, firmware modification, on-device scripting.
- Autoguiding (the S50 is alt-az, no guiding).

## Further Notes

- Firmware 9.75 confirmed by the user; signed handshake mandatory.
- Same-LAN direct IP confirmed; scope IP configurable in settings.
