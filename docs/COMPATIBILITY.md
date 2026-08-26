# Compatibility

Hermes Codex Operator integrates with a versioned local protocol.
Compatibility is recorded from executed evidence, not inferred from a product
name or a passing test on another platform.

## Current qualification

| Component | Declared target | Verified evidence | Honest boundary |
|---|---|---|---|
| Python | 3.11–3.14 | Public CI passed on all four versions | Python 3.15 and later are rejected by `doctor` and package metadata |
| Operating systems | Linux, macOS, Windows | The 197-test runtime suite passed on all three CI runner families; the current 203-test docs candidate passed locally on macOS | Candidate CI remains required; live Codex smokes were run only on macOS arm64 |
| Codex CLI / App Server | `0.149.0` | Generated schemas, protocol/failure suite, live ordinary lifecycle, and live managed-daemon queue smoke | Queue support is experimental and exact-version only |
| Hermes Agent | `0.20.0` evidence baseline | Isolated discovery and complete skill load passed | No full Hermes-model → operator → Codex conversation is claimed yet |
| Package | Python package plus `websockets==15.0.1` | sdist and wheel build, privacy scan, clean install, import, and CLI smoke passed | The wheel contains the core CLI; install the repository skill separately |

The public CI matrix proves the synthetic suite and packaging on hosted runners.
It does not turn a macOS-only live Codex observation into a Linux or Windows
live-compatibility claim.

## Protocol surface

The ordinary lifecycle uses Codex App Server v2 over stdio:

- `initialize` and `initialized`;
- `thread/list` for bounded catalog pages;
- `thread/start` and `thread/resume`;
- `turn/start` and matching lifecycle notifications;
- `thread/read` with turns for authoritative exact-turn readback.

Stable commands initialize with `experimentalApi: false`. The separate `queue`
command initializes with `experimentalApi: true` and uses the generated
Codex `0.149.0` methods:

- `thread/loaded/list`;
- `thread/queue/list`;
- `thread/queue/add`;
- `thread/queue/start`.

The queue CLI requires `--shared-app-server`. It reads `socketPath` and
`appServerVersion` from `codex app-server daemon version`, requires exact
`0.149.0` evidence, and opens a WebSocket connection to `/rpc` on that managed
Unix socket. It does not accept an arbitrary transport command or start a
private App Server to claim ownership of another process's active thread.

Codex documents App Server as the interface for deep client integration and
marks the App Server/WebSocket surface experimental. See the official
[Codex App Server documentation](https://developers.openai.com/codex/app-server).

## Capability qualification

### Read-only catalog, adoption, and watch

`catalog`, `catalog read`, `adopt`, and `watch` use only `thread/list` and
`thread/read`. They never start, resume, or enqueue work. Their protocol and
failure behavior have synthetic coverage. A separate live catalog/adopt/watch
qualification has not been recorded.

### Idle continuation

`continue` requires exact thread, workspace, and idle-state readback, stores the
complete bounded baseline turn-ID set, and permits at most one `thread/resume`
plus one `turn/start`. An unconfirmed response becomes `UNKNOWN` and is never
automatically resubmitted. This path has synthetic protocol/failure coverage;
no live mutating continuation is claimed.

### Active-thread shared queue

`queue` requires one exact loaded thread and active owner, persists a queue
baseline before dispatch, and enqueues once. Completion requires a resulting
turn whose user message matches the queued client-message ID and normalized
input digest, followed by completed authoritative `thread/read`. Available
notifications are cross-checked; disagreement fails closed.

Codex `0.149.0` may consume the queued item automatically. The operator calls
`thread/queue/start` only when bounded readback still shows that exact item
queued. Synthetic tests cover both branches. The 2026-08-26 live
managed-daemon smoke proved one real queue submission and restart
non-dispatch, but did not claim which server branch consumed the item.

Queue workspace evidence is end-state evidence. Its baseline begins while the
owner turn is active, so `changed_paths` can include owner and queued-turn
changes. Use idle `continue`, or an externally exclusive/non-mutating owner,
when per-turn filesystem causality matters.

## Version qualification procedure

For every newly supported Codex CLI version:

1. record `codex --version`;
2. generate that binary's App Server JSON schemas;
3. compare every method and field used by the adapter;
4. run the complete unit and scripted protocol/failure suite;
5. run `scripts/live_smoke.py` with explicit opt-in;
6. start the managed daemon explicitly and run `scripts/live_queue_smoke.py`;
7. repeat the same operation and prove `dispatched=false`;
8. scan the exact source tree and release artifacts;
9. record the exact platform, version, result, and limit below.

Live commands use the caller's authenticated Codex installation and may leave
synthetic threads in local history:

```bash
python3 scripts/live_smoke.py --confirm-live-codex
codex app-server daemon start
python3 scripts/live_queue_smoke.py --confirm-live-codex --owner-attempts 3
```

`--owner-attempts` applies only before `queue/add`. Once one owner reaches
`active/inProgress`, the smoke permits at most one queue submission.

## Live qualification log

| Date | OS | Codex | Hermes | Result | Scope proved |
|---|---|---|---|---|---|
| 2026-08-25 | macOS 26.5 arm64 | `0.124.0` | — | BLOCKED before dispatch | Host selected unsupported reasoning `ultra`; no turn was created |
| 2026-08-25 | macOS 26.5 arm64 | `0.149.0` | — | PASS | Ordinary lifecycle reached `VERIFIED`; duplicate submit and collect did not dispatch |
| 2026-08-26 | macOS 26.5 arm64 | `0.149.0` | — | Historical partial PASS | Embedded same-adapter queue mutation; not managed-daemon CLI evidence |
| 2026-08-26 | macOS 26.5 arm64 | `0.149.0` | — | PASS | Managed daemon, separate queue CLI, exact result verification, and restart non-dispatch |
| 2026-08-25 | macOS 26.5 arm64 | — | `0.20.0` | PASS | Isolated skill discovery and full load; no model call |

A row qualifies only the exact surface named in its final column.
