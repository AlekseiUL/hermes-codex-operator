# Changelog

All notable changes to this project will be documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-08-26

### Added

- Bounded, strictly parsed `thread/list` catalog pagination and exact
  `catalog read` through `thread/read` with opt-in raw result text.
- Exact `threadId` + `turnId` read-only adoption and restart-durable watches.
- Idempotent transport-neutral completion-event outbox with result SHA-256.
- `catalog`, `catalog read`, `adopt`, `watch`, and `events list/ack` CLI commands.
- Atomic SQLite v1-to-v2 migration and two-process watch concurrency coverage.

### Security

- Catalog, adoption, and watch polling use only `thread/list` and `thread/read`;
  they never resume or start a thread or turn.
- Missing, duplicate, or conflicting exact identities fail closed as `UNKNOWN`.
- Experimental API capability remains disabled; `project/list` is not included.

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

[Unreleased]: https://github.com/AlekseiUL/hermes-codex-operator/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/AlekseiUL/hermes-codex-operator/releases/tag/v0.1.0
[0.0.1]: https://github.com/AlekseiUL/hermes-codex-operator/releases/tag/v0.0.1
