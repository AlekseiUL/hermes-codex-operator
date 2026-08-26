# Quality and reliability

Hermes Codex Operator is a pre-alpha, local-first control layer for Codex App
Server. Its reliability claim is narrow: for one declared operation, preserve
exact task identity, avoid blind duplicate dispatch, read the authoritative
turn, verify the declared workspace scope and checks, and return a durable
receipt.

It is not an operating-system sandbox, a hosted service, a replacement for
human review, or a guarantee that model-generated code is correct.

## Guarantee matrix

| Area | What the operator enforces | Evidence | Limit |
|---|---|---|---|
| Dispatch identity | One operation ID maps to at most one automatic Codex dispatch | Journal, idempotency, concurrency, lost-response, restart, and cancellation tests | `UNKNOWN` can require human reconciliation |
| Final result | The exact turn is reread through authoritative `thread/read` | Scripted protocol suite and live lifecycle smoke | Streamed text and model self-report are never proof |
| Workspace scope | Final changed paths must match declared globs | Before/after manifests and scope-violation tests | This is post-hoc evidence, not write confinement |
| Verification | Declared argv-only commands must pass | Verification timeout, exit-code, output-bound, and cancellation tests | Commands are trusted host executables and can have side effects |
| Recovery | Persisted operations can be inspected after process restart | SQLite migration and restart tests | The exact private WorkContract must be retained |
| Existing idle task | One exact idle thread can receive one bounded continuation | Synthetic identity, ambiguity, restart, and contention suite | No live continuation qualification is claimed yet |
| Active task queue | One exact active loaded thread can receive one queue submission | Synthetic failure suite plus an opt-in macOS live managed-daemon smoke | Experimental; qualified only for Codex `0.149.0` on the tested macOS host |
| Privacy | Tree, staged state, reachable history, sdist, and wheel are scanned | Fail-closed privacy suite and release artifact scan | Pattern matching cannot prove that all text is non-sensitive |

## Evidence snapshot

The current public candidate was qualified on 2026-08-26 with:

- 203 unit, protocol, and documentation-gate tests;
- Python 3.11, 3.12, 3.13, and 3.14;
- Linux, macOS, and Windows CI runners;
- a package smoke that built, normalized, scanned, installed, and exercised both
  the sdist and wheel;
- a local live lifecycle smoke on Codex App Server `0.149.0`;
- an opt-in managed-daemon queue smoke on macOS arm64 that used one queue
  submission and proved restart did not dispatch again;
- a fresh public-clone history scan and installation smoke.

The 13-job public CI run for this candidate is
[GitHub Actions run 32997466917](https://github.com/AlekseiUL/hermes-codex-operator/actions/runs/32997466917).
The live queue smoke ran against runtime source commit `1bfe767`; the promoted
main commit `366735a` had the same source tree. Documentation-only commits do
not extend that live qualification to a different Codex version or runtime
implementation.

## Stable and experimental surfaces

### Core lifecycle

`submit`, `status`, `recover`, `collect`, and `abandon` use the ordinary App
Server lifecycle. Their protocol and failure behavior have synthetic coverage;
the ordinary lifecycle also has a live macOS qualification on Codex `0.149.0`.

### Read-only observation

`catalog`, `catalog read`, `adopt`, `watch`, and `events` do not start or resume
Codex work. They have synthetic protocol coverage. No separate live
catalog/adopt/watch qualification is claimed.

### Existing-task mutations

`continue` is bounded to one exact idle thread and has synthetic coverage.
`queue` is a separate experimental command for one exact active thread in the
same managed App Server. It is not a retry or fallback for `continue` and
rejects every version except the explicitly selected `0.149.0` baseline.

## Release rule

A release claim requires all of the following against the candidate tree:

1. complete unit and protocol suite;
2. lint, type, compile, and diff-integrity checks;
3. tree, staged, and reachable-history privacy scans;
4. normalized sdist and wheel build, scan, install, and CLI smoke;
5. documentation link and command verification;
6. a new opt-in live smoke after any runtime or protocol change;
7. explicit remaining-risk notes for every unqualified surface.

See [Compatibility](COMPATIBILITY.md), [Threat model](THREAT_MODEL.md), and
[Privacy and release gate](PRIVACY_RELEASE.md) for the detailed boundaries.
