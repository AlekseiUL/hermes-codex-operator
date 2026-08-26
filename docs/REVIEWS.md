# Release reviews

**Status:** public pre-alpha source; `v0.1.0` runtime candidate qualified, tag not yet published

**Reviewed:** 2026-08-26

**Boundary:** source, tests, live compatibility evidence, documentation, Git history, and release artifacts

This record separates historical evidence from current proof. A passing review
does not authorize deployment or publication side effects, and a live result
qualifies only the exact platform, Codex version, and protocol surface named.

## Current release verdict

The runtime source promoted to public `main` passed the bounded release gate:

- 197 runtime unit and protocol tests in the public matrix;
- Python 3.11–3.14 on Linux, macOS, and Windows;
- Ruff, Pyright, compilation, diff-integrity, and Git-integrity checks;
- tree, staged, reachable-history, sdist, and wheel privacy scans;
- normalized sdist and wheel build/install/CLI smoke;
- ordinary Codex App Server lifecycle live smoke on macOS arm64 with `0.149.0`;
- managed-daemon queue live smoke with one submission and restart
  `dispatched=false`;
- fresh public-clone privacy and installation smoke.

The documentation-hardening candidate adds six docs-gate tests and passes 203
tests locally. Candidate matrix CI remains required before merge.

Verdict: no known source, packaging, or reachable-history blocker for the
current public runtime tree. The project remains pre-alpha because App Server
and queue transport are experimental and several integrations are intentionally
unqualified.

## Lifecycle and crash-safety review

The lifecycle review covered duplicate callers, lost responses, restart
recovery, cancellation, verification side effects, and SQLite finalization.
Resolved controls include:

- process-safe ownership of one operation;
- `UNKNOWN` after ambiguous post-dispatch failures;
- exact recovery of persisted thread and turn identities;
- cancellation-safe verification and final evidence capture;
- cleanup of POSIX background descendants before final scope evidence;
- segment-aware change globs;
- atomic terminal state plus immutable receipt.

Verdict: no known lifecycle blocker in the reviewed runtime tree.

## App Server and shared-queue review

The protocol review checked JSON correlation, initialization ordering,
authoritative `thread/read`, cancellation, timeout, process cleanup, malformed
successful responses, and queue races. The active-queue hardening now requires:

- exact managed-daemon version and socket evidence;
- one loaded thread and active owner;
- one queue submission per operation;
- exact client-message ID and normalized-input digest;
- authoritative owner and target completion readback;
- exact queue absence and stable workspace/thread identity immediately before
  `VERIFIED`;
- no dispatch after repeat or restart.

A historical embedded queue smoke proved only a partial same-adapter path. The
later managed-daemon smoke exercised the public direct Unix-socket CLI and
proved restart non-dispatch. It did not claim whether server-side automatic
consumption or the bounded manual-start fallback consumed the item.

Verdict: qualified for experimental use only with exact Codex `0.149.0` on the
tested macOS host.

## Public release surface review

The release-surface audit treats filenames, Git objects, source distributions,
wheels, metadata, CI, documentation, and product claims as one boundary.
Controls include:

- fingerprint-only privacy findings;
- bounded source/archive inspection;
- deterministic sdist owner/timestamp normalization;
- exact allowlisting of the reviewed hero image;
- pinned build tooling and GitHub Actions;
- core-only wheel and repository-bundled Hermes skill documented separately;
- explicit verification-command and active-queue causality limits.

A post-merge audit found a personal author email in one GitHub-generated merge
commit. The reachable `main` history was replaced with a content-identical
noreply commit and a fresh public-clone history scan passed. GitHub may retain
an unreachable object by direct SHA until server-side garbage collection; that
object is not reachable from `main` or a normal clone.

## Documentation review

The 2026-08-26 documentation hardening reconciled README, compatibility,
reviews, roadmap, RFC status, implementation record, skill protocol, package
dependency, commands, live evidence, and GitHub settings. Every command shown
was checked against CLI help; internal links and Mermaid sources are release
gates.

## Residual limits

- This is a pre-alpha local operator, not an OS sandbox or security audit.
- The public CI suite uses a scripted App Server and synthetic workspaces.
- Live Codex evidence exists only for macOS arm64 and Codex `0.149.0`.
- Idle `continue` and read-only catalog/adopt/watch have synthetic coverage but
  no separately recorded live qualification.
- A full Hermes-model → operator → Codex conversation remains unqualified.
- Queue receipts prove final scope, not per-turn filesystem causality while an
  owner turn writes concurrently.
- Desktop Local Projects, UI automation, daemon delivery, Kanban, and general
  multi-agent routing remain outside this repository.

See [Quality and reliability](QUALITY.md) and
[Compatibility](COMPATIBILITY.md) for the evidence matrix.
