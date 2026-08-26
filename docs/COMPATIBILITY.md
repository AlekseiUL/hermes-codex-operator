# Compatibility

Hermes Codex Operator integrates with a versioned local protocol. Compatibility
is declared from evidence, not assumed from a product name.

## Target baseline and current evidence

| Component | Target | Evidence available now | Qualification status |
|---|---|---|---|
| Python | 3.11–3.14 | Standard-library runtime; local suite on 3.14; four-version CI matrix defined | CI not yet run publicly |
| Codex CLI / App Server | 0.149.0 | Generated schemas, scripted protocol/failure suite, and disposable live turn | Qualified on the macOS host listed below |
| Hermes Agent | 0.20.0 | Isolated discovery and full skill load | Live model/tool run pending |
| Operating systems | macOS, Linux, Windows | Local macOS tests; three-OS CI matrix defined | Linux/Windows unqualified until CI runs |

The package has no third-party runtime dependencies. Building from source uses
Setuptools through the standard PEP 517 interface.

## Codex protocol surface used

The core uses a documented v2 stdio protocol surface through the currently
experimental `codex app-server` command:

- `initialize` and `initialized`;
- `thread/list` for bounded read-only catalog pages;
- `thread/start` and `thread/resume`;
- `turn/start` and matching `turn/completed` notifications;
- `thread/read` with full turns for authoritative exact-turn readback.

Stable commands do not opt into experimental API capabilities or methods and
initialize with `experimentalApi: false`. The separate `queue` command accepts
only an explicit exact `--experimental-api-version 0.149.0`, initializes with
`experimentalApi: true`, and uses the generated 0.149.0 schemas for
`thread/loaded/list`, `thread/queue/list`, `thread/queue/add`, and
`thread/queue/start`. Unqualified, older, or newer versions fail with
`UNSUPPORTED_CAPABILITY`; the initialized App Server `userAgent` must also
report the exact selected version before any queue side effect.
Unknown server requests and permission broadening fail closed.

`project/list` is deliberately excluded from v0.1.0. Catalog/read/adopt/watch do
not call `thread/resume`, `thread/start`, or `turn/start`; they use only
`thread/list` and authoritative `thread/read`. The release slice has both
synthetic protocol coverage and a bounded read-only live smoke proving that
catalog/read/adopt leaves the exact turn set unchanged.

The explicit `continue` command is the only new mutation surface in the current
unreleased slice. It requires exact `thread/read` workspace and idle-state
proof, stores a bounded immutable turn-ID baseline, then allows exactly one
`thread/resume` and one `turn/start`. It does not use project or queue APIs.
`thread/resume` is treated as delivery-affecting for timeout/cancellation
purposes: an unconfirmed response is `UNKNOWN` and is never auto-resubmitted.
This path currently has synthetic protocol/failure qualification only; no live
mutating continuation has been run.

The experimental active-thread queue path is separate from idle continuation,
catalog, and watch. It requires exact loaded-thread and active-turn ownership,
persists an immutable SQLite v4 queue baseline before dispatch, enqueues once,
and never treats a lost response or restart as permission to enqueue again.
Queue inspection is bounded to one page after malformed/lost enqueue delivery;
completion requires unique new-turn correlation and authoritative exact-turn
readback. It has synthetic protocol/failure qualification only; no live
mutating queue smoke has been authorized or run.

The official App Server documentation states that stdio uses newline-delimited
JSON, requires the initialization handshake, and models conversation as
threads containing turns and items. See
[Codex App Server](https://developers.openai.com/codex/app-server).

## Model settings

`model` and `reasoning_effort` are optional WorkContract fields. The portable
repository does not impose a personal default model.

Protocol v1 accepts the reasoning values proven by the declared App Server
baseline: `none`, `minimal`, `low`, `medium`, `high`, and `xhigh`. A newer Codex
release may expose more values, but they must not be accepted until its
generated schema and a live turn confirm the wire behavior.

## Version qualification procedure

For every newly supported Codex CLI version:

1. record `codex --version`;
2. generate that binary's App Server JSON schemas;
3. compare the methods and fields used by the adapter;
4. run the complete scripted protocol/failure suite;
5. run the disposable live smoke from `scripts/live_smoke.py`;
6. repeat the same operation ID and prove `dispatched=false`;
7. record the result in the table below.

The live command is explicit because it uses the caller's authenticated Codex
installation and may leave a synthetic thread in local history:

```bash
python3 scripts/live_smoke.py --confirm-live-codex
```

Do not silently widen enums or ignore unexpected response fields merely to make
a newer version pass.

## Live qualification log

| Date | OS | Codex | Hermes | Result | Notes |
|---|---|---|---|---|---|
| 2026-08-25 | macOS 26.5 arm64 | 0.124.0 | — | BLOCKED before dispatch | Host config selected unsupported reasoning value `ultra`; App Server accepts only through `xhigh`; no model turn was created |
| 2026-08-25 | macOS 26.5 arm64 | 0.149.0 | — | PASS: live operator lifecycle | One synthetic turn reached `VERIFIED`; duplicate submit and collect both returned `dispatched=false`; exact changed path and receipt digests were checked |
| 2026-08-25 | macOS 26.5 arm64 | — | 0.20.0 | PASS: discovery/load | Isolated Hermes home discovered and loaded the complete external skill; no model call was made |

A full Hermes-model-to-tool-to-Codex conversation remains an unqualified
integration target. The rows above qualify only the exact combinations and
surfaces they name.
