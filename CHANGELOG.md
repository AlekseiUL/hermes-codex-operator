# Changelog

All notable changes to this project will be documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Distinct `continue --thread-id` mutation for one exact existing idle task.
- Immutable SQLite v3 continuation baselines containing only exact turn IDs and
  their digest; atomic v1/v2 migration remains backward compatible.
- Exact workspace/idle preflight, one-turn post-dispatch correlation, restart
  idempotency, and two-process reservation coverage.
- Separate exact-version `queue` mutation for one proven active loaded thread,
  using the qualified Codex 0.149.0 experimental queue schemas.
- Immutable SQLite v5 dispatch kind plus queue baselines with
  message/turn-set digests and atomic submission binding; v1-v4 migrations
  remain backward compatible.
- Explicit disposable `scripts/live_queue_smoke.py` for opt-in active-queue
  qualification against the caller's real local Codex account.
- Bounded, strictly parsed `thread/list` catalog pagination and exact
  `catalog read` through `thread/read` with opt-in raw result text.
- Exact `threadId` + `turnId` read-only adoption and restart-durable watches.
- Idempotent transport-neutral completion-event outbox with result SHA-256.
- `catalog`, `catalog read`, `adopt`, `watch`, and `events list/ack` CLI commands.
- Atomic SQLite v1-to-v2 migration and two-process watch concurrency coverage.
- Explicit `dispatch_kind` in lifecycle and status JSON for simple recovery
  diagnostics.

### Security

- Resume/start delivery ambiguity becomes `UNKNOWN` and is never automatically
  resubmitted. Catalog/read/adopt/watch/events remain strictly read-only.
- Stable commands keep experimental capabilities disabled. Queue opt-in is
  exact-version only; lost/malformed enqueue delivery gets one inspection and
  never re-enqueues, while ambiguous identity or turn correlation is `UNKNOWN`.
- Explicit queue add/start response identity contradictions now fail closed as
  `UNKNOWN` and cannot be recovered through a different matching queue item or
  unique readback turn.
- A new turn can never satisfy queue correlation while the exact queued
  submission remains pending; this concurrent-turn race fails closed as
  `UNKNOWN` and never re-enqueues.
- Catalog, adoption, and watch polling use only `thread/list` and `thread/read`;
  they never resume or start a thread or turn.
- Missing, duplicate, or conflicting exact identities fail closed as `UNKNOWN`.
- Generic recovery cannot reinterpret an ambiguous queue operation as an
  ordinary submission. Explicit `abandon` closes only local queue or
  pre-correlation continuation state; recoverable correlated continuations
  remain protected.
- No raw prompt or result was added to continuation or queue journal rows.

### Fixed

- The live queue harness uses a direct shell hold instead of nested Python and
  can report up to three explicit owner-preflight attempts while preserving the
  one-`queue/add` limit.
- Match Codex 0.149.0 queued-text normalization (`text_elements: []`) so live
  semantic identity does not fail on a server-added default field.
- Use one fixed `codex app-server proxy` transport for queue operations and
  require the caller to opt into an already-running managed App Server with
  `--shared-app-server`.
- Wait for active-owner terminal state and inspect bounded queue state before
  using `thread/queue/start` when the server has not consumed the item.
- Correlate an automatically consumed turn by the exact queued client-message ID
  and normalized-input digest, preventing a concurrent unrelated turn from
  producing false `VERIFIED` even if the queue changes between reads.
- Require authoritative owner-terminal readback, exact queue removal after
  manual start, stable thread/workspace identity, and mutually consistent final
  turn status and text before queue verification.
- Repeat the complete authoritative identity, turn, result, workspace, and
  queue-absence check immediately before recording `VERIFIED`.

## [0.0.1] - 2026-08-25

### Added

- WorkContract v1 and privacy-safe receipt schemas.
- Durable SQLite lifecycle journal with at-most-once automatic dispatch.
- Codex App Server thread/turn adapter with authoritative exact-turn readback.
- Restart-safe recovery and continuation when the exact private contract is
  retained.
- Workspace scope checks and argv-only verification.
- Hermes technical-partner skill and operator protocol reference.
- Repository, Git-history, sdist, and wheel privacy gates with sensitive-path
  redaction.
- Atomic, deterministic sdist metadata normalization before release scanning.
- Public documentation, compatibility policy, roadmap, and release profile.

### Fixed

- Open tree-scan files in binary mode so Windows newline translation cannot
  invalidate byte-count checks.
- Make the synthetic App Server fixture use native absolute paths on every
  supported platform.
- Make the final-snapshot cancellation test release its worker on every exit
  path and use bounded synchronization on slower CI runners.
- Bound every CI job to ten minutes so a future deadlock fails visibly.

### Changed

- Switched the project license from Apache-2.0 to MIT before the first release.

[Unreleased]: https://github.com/AlekseiUL/hermes-codex-operator/compare/v0.0.1...HEAD
[0.0.1]: https://github.com/AlekseiUL/hermes-codex-operator/releases/tag/v0.0.1
