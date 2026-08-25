# Contributing

Thanks for helping make the Hermes-to-Codex handoff simpler and more reliable.

## Development setup

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
```

Run the complete local gate:

```bash
PYTHONPATH=src PYTHONWARNINGS=error::ResourceWarning \
  python3 -m unittest discover -s tests -v
python3 scripts/privacy_gate.py . --mode tree
python3 -m hermes_codex_operator --help
```

## Design invariants

Changes must preserve these properties:

- one operation ID maps to at most one automatically dispatched Codex turn;
- ambiguity becomes `UNKNOWN`, never an automatic duplicate;
- authoritative `thread/read` owns the final result;
- `TURN_COMPLETED` is not `VERIFIED`;
- verification commands remain argv arrays with no shell interpretation;
- verification commands are treated as trusted host executables, not as a
  sandbox boundary;
- workspace paths are canonical, scoped, and symlink-safe;
- terminal state and immutable receipt are committed atomically;
- receipts and journals do not store raw prompts or model results;
- the portable core remains independent of private profiles, Telegram, Kanban,
  Codex Desktop UI automation, and other coding harnesses.

## Pull requests

- Keep each change focused.
- Add a regression test for changed behavior.
- Update README, protocol, compatibility, or threat-model text when the public
  contract changes.
- Use only synthetic examples.
- Do not commit generated runtime state or real Codex task history.
- Explain the failure mode being fixed and the evidence that proves the fix.

Protocol changes require an RFC update and an explicit schema-version decision.
New adapters must be optional, version-gated, and fail closed when they cannot
identify the exact task.

## Live tests

Live Codex or Hermes tests use the caller's local account and may leave a task
in local history. They must be opt-in, use disposable synthetic workspaces, and
must never run in ordinary pull-request CI.

```bash
python3 scripts/live_smoke.py --confirm-live-codex
```

Before claiming a release is ready, follow
[PRIVACY_RELEASE.md](docs/PRIVACY_RELEASE.md).
Generated source distributions must pass through `scripts/normalize_sdist.py`
before they are scanned or published; a raw builder tarball is not a release
candidate.
