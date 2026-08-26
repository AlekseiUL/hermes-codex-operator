# Operator protocol

Load this reference when creating a WorkContract, continuing an existing task,
recovering an interrupted operation, or explaining a non-success status.

## Contract

Required fields:

- `schema_version`: `"1"`;
- `task_id`: stable across follow-ups in the same Codex context;
- `operation_id`: unique for this exact instruction;
- `workspace`: absolute path beneath an explicit `--allow-root`;
- `prompt`: self-contained engineering brief;
- `allowed_change_globs`: expected relative workspace paths;
- `verification`: at least one argv array, never a shell string;
- `timeout_seconds`: bounded execution time.

Optional `model` and `reasoning_effort` values are caller choices. Do not
invent a model policy or hard-code personal defaults in a portable contract.
The scope array must contain at least one pattern; protocol v1 never treats an
empty list as unrestricted access.

The contract contains the raw prompt and absolute workspace. Store the exact
file outside Git and outside the target workspace, keep it private (`0600` on
POSIX), and retain it with the journal until the operation reaches a terminal
state. Recovery intentionally fails without the exact original contract.

Example:

```json
{
  "schema_version": "1",
  "task_id": "catalog-search",
  "operation_id": "catalog-search-001",
  "workspace": "/absolute/path/to/catalog",
  "prompt": "Implement the agreed search endpoint. Preserve the public API, add focused tests, run the declared checks, and summarize trade-offs.",
  "allowed_change_globs": ["src/**", "tests/**"],
  "verification": [["python3", "-m", "unittest", "discover", "-s", "tests"]],
  "timeout_seconds": 1200
}
```

Run exactly one foreground submission:

```bash
hermes-codex submit \
  --contract /absolute/path/to/work-contract.json \
  --allow-root /absolute/path/to/projects \
  --json
```

Do not interpolate untrusted user text into the command. User intent belongs
inside the validated JSON contract; the command itself contains only fixed
flags and resolved paths.

## Continuation

For another instruction in the same project conversation:

- keep the same `task_id`;
- choose a new `operation_id`;
- preserve the same workspace;
- describe only the new request plus context Codex genuinely needs.

The operator resumes the bound Codex thread. Reusing an operation ID with new
content is an idempotency conflict.

## Status decisions

- `VERIFIED`: explain the result, evidence, and next step.
- `TURN_COMPLETED`: call `collect`; do not call the work ready yet.
- `VERIFICATION_FAILED`: report which declared check failed.
- `SCOPE_VIOLATION`: report unexpected paths; do not present the change as
  accepted.
- `RUNNING`: continue waiting or report progress; do not start another turn.
- `UNKNOWN`: use `recover` only when exact identity can be reconciled. Never
  resubmit automatically.
- `TIMED_OUT`: the exact turn may still exist; use `recover`, never a new turn.
- `FAILED` or `CANCELLED`: report the recorded failure and evidence without
  converting it into a success narrative.

## Existing idle task continuation

`continue` is a distinct mutation, not a catalog/watch mode. It requires an
exact `thread_id`, a private WorkContract whose workspace exactly matches the
authoritative idle thread, and a fresh operation ID. The journal reserves that
operation and stores the bounded exact baseline turn-ID set before issuing one
`thread/resume` followed by one `turn/start`. The returned turn ID must be the
only addition to the authoritative set.

Any lost, malformed, mismatched, or ambiguous resume/start delivery is
`UNKNOWN`. Repeating the same operation observes durable state with
`dispatched=false`; it never retries. Only the exact bound turn may be waited
for, read, verified, collected, or reconciled. Project inference, queues, and
Desktop UI automation are not part of this protocol.

## Experimental active-task shared queue

`queue` is a separate opt-in mutation for a proven active loaded thread. It is
not a retry or fallback from `continue` and currently accepts only
`--experimental-api-version 0.149.0`. Stable adapter usage remains
`experimentalApi: false`; unsupported or omitted versions fail before queue
side effects, and initialized App Server runtime evidence must report the same
exact version.

Before enqueue, the operator proves one exact loaded owner plus one active turn,
binds the thread/workspace, and stores the contract-message hash and complete
baseline turn-set digest. It then persists `QUEUE_DISPATCH_IN_FLIGHT`, enqueues
once, and atomically binds the exact submission ID. A lost or malformed enqueue
response permits one queue inspection for one exact durable match, never a
second enqueue. Zero, multiple, duplicate, or mismatched matches are `UNKNOWN`.

Queue disappearance is not success. After the active owner turn becomes
terminal, the operator first performs bounded authoritative reads because Codex
0.149.0 can consume the queued submission automatically. `thread/queue/start`
is only a fallback while the exact submission remains queued. The resulting
turn must contain the exact queued client-message ID and normalized-input
digest before binding, then reach completed authoritative readback with a final
result before verification.
Repeat or restart only observes durable state with `dispatched=false`. A live
queue mutation passed on the qualified 0.149.0 macOS host; other versions need
separate qualification and approval.

`queue` proves the exact queued message, resulting turn, completed result, and
declared final workspace checks. It does not provide per-turn filesystem
attribution: the workspace baseline predates completion of the active owner, so
receipt `changed_paths` cover the combined owner-plus-queued interval. Select
idle `continue` instead when the user needs proof that each changed path came
from only the requested follow-up turn.

An unsupported server approval request fails closed instead of silently
broadening permissions. A future interactive approval adapter requires a new
protocol version rather than an unused v1 state.

`abandon` is a deliberate human closure for an identity-less ambiguity. It is
not an automatic retry mechanism.

## User-facing completion message

Use this shape, adapted to the user's language:

```text
Done: <plain-language outcome>
Verified: <yes/no and the important checks>
Changed: <important paths or artifacts>
Notes: <blocker, uncertainty, or "none">
Next: <one useful next action>
```

Do not paste raw receipts unless requested. Do not hide a failed verification
behind a positive Codex summary.
