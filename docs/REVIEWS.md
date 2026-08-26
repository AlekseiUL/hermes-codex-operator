# Release reviews

**Status:** local release gates passed for the current candidate
**Reviewed:** 2026-08-26
**Boundary:** source, synthetic tests, documentation, and local release process

No review below replaces owner authorization. Real Codex account smoke tests
remain separate, explicit actions; every pushed revision still requires public
CI and public-source install readback.

## Lifecycle and crash-safety review

An independent read-only audit exercised duplicate callers, lost responses,
restart recovery, cancellation, verification side effects, and SQLite
finalization. Findings resolved before this record was written include:

- process-safe ownership of one operation;
- `UNKNOWN` after ambiguous post-dispatch failures;
- exact recovery of persisted thread and turn identities;
- cancellation-safe verification and final evidence capture;
- cleanup of POSIX background descendants before final scope evidence;
- segment-aware change globs;
- atomic terminal state plus immutable receipt, with no legacy transition
  bypass.

Final verdict: no lifecycle blocker.

## App Server protocol review

An independent read-only protocol audit checked JSONL correlation, initialize
ordering, authoritative `thread/read`, cancellation, timeout, process-group
cleanup, resume permissions, and malformed successful responses. A malformed
`turn/start` response without a trustworthy turn ID now becomes `UNKNOWN` and
cannot be dispatched again automatically; a valid explicit server rejection
remains `FAILED`.

Final verdict: no protocol blocker.

## Hermes reliability review

An independent Hermes reliability reviewer inspected only this clean-room
repository in read-only mode. The verdict was `READY FOR LOCAL
PRE-PUBLICATION REVIEW`: no private paths, names, infrastructure bindings,
runtime databases, hidden packages, or unresolved reliability blockers were
found.

The review suggested small documentation/schema alignment and a digest binding
for authoritative results. Those changes are included.

## Public release surface review

An independent read-only audit then treated filenames, Git objects, source and
wheel distributions, package metadata, CI, documentation, and product claims as
one publication surface. Findings resolved before this record was written
include:

- sensitive filenames are fingerprinted instead of echoed by the privacy gate;
- source archives and ZIP/wheel artifacts receive the same bounded structural
  and content scan;
- generated sdists are atomically normalized so local account names cannot
  survive in tar owner metadata;
- the reviewed hero image is allowlisted by one exact path and digest;
- the build frontend, backend, and GitHub Actions are pinned to exact versions
  or commit SHAs;
- the source distribution includes the Hermes skill while the wheel is
  explicitly documented as core-only;
- empty change-scope rules are rejected instead of meaning unrestricted scope;
- verification commands are described as trusted host code outside the Codex
  sandbox;
- App Server, OS, Python, Hermes, Desktop, notification, and restart claims are
  qualified to the evidence actually available.

Final verdict: no known publication-surface blocker in the reviewed source.
The exact publication commit and its rebuilt artifacts still require the gates
below; this review is not permission to publish a different tree.

## Residual limits

- This is a pre-alpha local MVP, not a security audit or an OS sandbox.
- The automated suite uses a scripted App Server and synthetic workspaces.
- A live App Server smoke test can create real local Codex state and is not part
  of the default test suite. The explicit release smoke passed on Codex
  0.149.0: one synthetic turn reached `VERIFIED`, while duplicate submit and
  collect both proved `dispatched=false`. A separate same-App-Server-session
  active-queue smoke passed on 2026-08-26: server-normalized input identity,
  automatic queue consumption, exact artifact/readback, and
  same-process/journal-restart non-dispatch were verified.
- A subsequent independent adversarial review found that one unrelated new turn
  could be accepted if the exact submission disappeared between two protocol
  reads. Resulting turns are now bound only when their user message carries the
  exact queued client ID and normalized-input digest, with a regression proving
  `UNKNOWN`, one enqueue, zero queue-start calls, and no restart dispatch.
- The final-tree live recheck used one bounded invocation on Codex 0.149.0.
  Two synthetic owner setups were interrupted before `queue/add`; the third
  reached authoritative `active/inProgress`, after which exactly one queue
  submission reached `VERIFIED`. The exact artifact digest matched, while both
  same-process and journal-restart repeats remained non-dispatch observations.
- Active-task queue receipts intentionally use end-state rather than per-turn
  filesystem attribution: their workspace interval includes the selected active
  owner. Exact queued input/turn/result and final checks are enforced, while
  callers needing causal changed-path isolation must use idle `continue`.
- The earlier Codex 0.124.0 qualification attempt remains recorded as blocked
  before dispatch because the host configured unsupported reasoning value
  `ultra`; it is not evidence for or against 0.149.0.
- Hermes skill discovery and loading passed in an isolated home, but a live
  Hermes-model-to-tool-to-Codex run remains unqualified.
- Desktop Local Projects and Desktop UI automation are outside this milestone.
