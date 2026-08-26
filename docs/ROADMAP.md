# Roadmap

The product goal is a small, excellent Hermes specialist for Codex—not a new
general-purpose harness.

## v0.0.1 — reliable managed task

- versioned WorkContract;
- durable task/operation/thread/turn identity;
- at-most-once automatic dispatch;
- exact Codex App Server readback;
- changed-path scope and argv-only verification;
- immutable, privacy-safe receipts;
- lightweight Hermes technical-partner skill;
- local privacy and release gates.

## v0.1 — observe work started elsewhere (implemented)

Bounded `thread/list` discovery, exact-ID `adopt` / `watch`, restart-durable
watches, and a transport-neutral completion-event outbox are implemented. The
surface is read-only and does not create a second turn.

The host may consume the outbox and explain that exact task through its own
channel without screen automation; transport delivery is not part of the core.

## v0.2 — optional Codex Desktop visibility

Before Desktop integration, the portable core now implements the stable Phase 3
slice: one exact existing idle thread may be continued once through a durable
reservation, immutable baseline turn set, exact returned-turn binding, and
fail-closed `UNKNOWN` delivery handling. Project discovery, shared queues, and
Desktop automation remain outside this slice.

Investigate a version-gated macOS adapter for creating or associating visible
Codex Desktop projects and tasks. Prefer documented APIs or supported app state.
Accessibility automation is a fallback, not the portable core, and direct
writes to private Codex databases remain out of scope.

## v0.3 — durable host recipes

Publish narrow recipes for placing the foreground operator inside an existing
Hermes worker or gateway so completion notifications survive a process restart.
The core should remain daemon-free.

## Ongoing

- qualify current Codex CLI/App Server versions;
- test Python and operating-system matrices;
- improve receipts and diagnostics without storing private payloads;
- add focused adapters only when a real user workflow justifies them.

## Explicitly not planned

- support for every coding harness;
- built-in Kanban or general multi-agent orchestration;
- a vector memory database;
- a hosted control plane or telemetry service;
- automatic commits, pushes, deployments, or publication;
- broad background polling with hidden retries.
