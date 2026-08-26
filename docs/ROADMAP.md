# Roadmap

The product goal is a small, reliable Hermes specialist for Codex—not a new
general-purpose harness. Items are marked by implementation state so planned
work is never presented as shipped behavior.

## Shipped in `v0.0.1`

- versioned WorkContract;
- durable task, operation, thread, and turn identity;
- at-most-once automatic dispatch;
- authoritative Codex App Server readback;
- changed-path scope and argv-only verification;
- immutable privacy-safe receipts;
- Hermes technical-partner skill;
- local privacy and release gates.

## Implemented after `v0.0.1` — next tagged release

### Observe work started elsewhere

- bounded `thread/list` discovery;
- exact `catalog read`;
- read-only `adopt` and restart-durable `watch`;
- transport-neutral digest-only completion outbox.

These commands do not create, resume, or enqueue Codex work. Delivery through
Telegram or another channel belongs to the host Hermes agent.

### Continue or queue one exact existing task

- stable `continue` for one exact idle thread;
- experimental exact-version `queue` for one active thread loaded in the same
  managed App Server;
- direct managed Unix-socket transport for queue operations;
- exact message/turn correlation, authoritative completion, and restart
  non-dispatch.

The queue path is qualified only for Codex `0.149.0` on the tested macOS host.
It is not a fallback for `continue`, and its final workspace evidence cannot
attribute each changed file to one concurrent turn.

## Candidate: optional Codex Desktop visibility

Investigate a version-gated macOS adapter for creating or associating visible
Codex Desktop projects and tasks. Prefer documented APIs or supported app
state. Accessibility automation is a fallback, not the portable core. Direct
writes to private Codex databases remain out of scope.

## Candidate: durable host recipes

Publish narrow recipes for running the foreground operator inside an existing
Hermes worker or gateway when completion delivery must survive a process
restart. The portable core should remain daemon-free and transport-neutral.

## Ongoing qualification

- qualify new Codex CLI/App Server versions one at a time;
- keep Python and OS matrices green;
- add live evidence only for exact surfaces actually exercised;
- improve receipts and diagnostics without storing private payloads;
- add adapters only when a real workflow justifies their maintenance cost.

## Explicitly not planned

- support for every coding harness;
- built-in Kanban or general multi-agent orchestration;
- a vector memory database;
- a hosted control plane or telemetry service;
- automatic commits, pushes, deployments, or publication;
- broad background polling with hidden retries.
