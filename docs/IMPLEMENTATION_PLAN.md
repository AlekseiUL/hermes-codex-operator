# Hermes Codex Operator - Implementation Plan

**Goal:** Build a clean-room local MVP that safely dispatches, tracks, reads,
verifies, and receipts one Codex task lifecycle.
**Dependencies:** Python 3.11 through 3.14, Git, and Codex CLI only for
real-adapter tests.

### Step 1: Define versioned domain objects

**What:** Implement request validation, operation states, results, and receipts.
**Files:** `src/hermes_codex_operator/models.py`, `tests/test_models.py`
**Commands:** `python3 -m unittest tests.test_models -v`
**Check:** Invalid IDs, prompts, globs, argv, timeouts, and unknown fields fail.
**Result:** Self-contained protocol v1 domain model.

### Step 2: Implement workspace boundary and evidence

**What:** Canonicalize roots, reject unsafe symlinks, snapshot a bounded tree,
compare changes, and run argv-only verification commands.
**Files:** `src/hermes_codex_operator/workspace.py`,
`src/hermes_codex_operator/verification.py`, `tests/test_workspace.py`
**Commands:** `python3 -m unittest tests.test_workspace -v`
**Check:** Traversal, escaping symlink, out-of-scope change, timeout, and
non-zero verification are covered.
**Result:** Deterministic evidence independent of model self-report.

### Step 3: Implement durable journal

**What:** Add SQLite task/operation records, atomic reservation, transitions,
immutable baselines and receipts, atomic terminal finalization, and idempotency
conflicts.
**Files:** `src/hermes_codex_operator/journal.py`, `tests/test_journal.py`
**Commands:** `python3 -m unittest tests.test_journal -v`
**Check:** Duplicate, conflicting, concurrent, restart, invalid-transition,
and terminal-state-without-receipt tests pass.
**Result:** One durable source of task lifecycle truth.

### Step 4: Define adapter protocol and fake adapter

**What:** Define the transport-neutral Codex adapter and a deterministic fake
that can inject lost responses, timeouts, failures, and readback mismatches.
**Files:** `src/hermes_codex_operator/adapters/base.py`,
`src/hermes_codex_operator/adapters/fake.py`, `tests/test_engine.py`
**Commands:** `python3 -m unittest tests.test_engine -v`
**Check:** Failure injection never causes a blind duplicate dispatch.
**Result:** Testable lifecycle without a live Codex account.

### Step 5: Implement App Server stdio adapter

**What:** Implement initialize, thread start/resume, turn start, completion,
authoritative readback, bounded stderr, timeout, and process cleanup.
**Files:** `src/hermes_codex_operator/adapters/app_server.py`,
`tests/test_app_server_protocol.py`
**Commands:** `python3 -m unittest tests.test_app_server_protocol -v`
**Check:** A scripted fake JSONL subprocess validates message order and
correlation; manual smoke test is opt-in.
**Result:** Minimal official Codex protocol integration.

### Step 6: Add CLI and Hermes contract

**What:** Add `doctor`, `submit`, `status`, `collect`, `recover`, and explicit
`abandon`
commands plus a small JSON mapping guide for Hermes.
**Files:** `src/hermes_codex_operator/cli.py`,
`skills/hermes-codex-operator/SKILL.md`,
`examples/work-contract.json`, `tests/test_cli.py`
**Commands:** `python3 -m hermes_codex_operator.cli --help`
**Check:** CLI output is stable JSON on stdout; diagnostics go to stderr.
**Result:** Thin interface usable by Hermes without a Telegram implementation.

### Step 6a: Continue one exact existing idle task once

**What:** Add a separate `continue` service/command with exact thread and
workspace preflight, immutable baseline turn IDs, at-most-once resume/start,
atomic returned-turn binding, exact correlation/readback, and no automatic
retry from ambiguity.
**Files:** `src/hermes_codex_operator/continuation.py`,
`src/hermes_codex_operator/journal.py`, `src/hermes_codex_operator/cli.py`,
`tests/test_continuation.py`
**Commands:** `python3 -m unittest tests.test_continuation -v`
**Check:** idle happy path, duplicate/restart, identity mismatch, malformed or
oversized baselines, lost/malformed delivery, multiple-turn correlation,
terminal failure, migration, and two-process contention are covered with
synthetic fixtures only.
**Result:** One bounded continuation mutation; catalog/read/adopt/watch/events
remain strictly read-only and project/queue/Desktop APIs remain excluded.

### Step 7: Add privacy and release gates

**What:** Scan source, staged Git state, reachable history, generated source
archives, and wheels for secrets, personal paths, identifiers, runtime state,
and private bindings.
**Files:** `scripts/privacy_gate.py`, `tests/test_privacy_gate.py`,
`docs/THREAT_MODEL.md`, `.gitignore`
**Commands:** `python3 scripts/privacy_gate.py .`
**Check:** Synthetic leaks make the gate fail; the repository itself passes.
**Result:** Privacy by construction plus reproducible enforcement.

### Step 8: Independent review and clean-environment test

**What:** Give the RFC and local MVP to an independent Hermes reliability
reviewer, resolve findings, then test from a fresh temporary clone/archive.
**Files:** `docs/REVIEWS.md`
**Commands:** `python3 -m unittest discover -s tests -v`; `python3
scripts/privacy_gate.py .`
**Check:** Review has no unresolved release blocker and clean test passes
without private infrastructure.
**Result:** Evidence-backed local MVP ready for the user's publication decision.
