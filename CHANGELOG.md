# Changelog

All notable changes to this project will be documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/AlekseiUL/hermes-codex-operator/compare/v0.0.1...HEAD
[0.0.1]: https://github.com/AlekseiUL/hermes-codex-operator/releases/tag/v0.0.1
