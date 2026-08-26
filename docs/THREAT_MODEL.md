# Threat model and privacy boundary

## Protected assets

- credentials and authentication material;
- prompt and result contents;
- local usernames, paths, hostnames, and device metadata;
- messaging identifiers and internal agent/team topology;
- project/thread/turn identifiers;
- source code and files outside the selected workspace;
- task identity and evidence integrity.

## Main threats

1. Duplicate execution after timeout, restart, or a lost App Server response.
2. Workspace escape through path traversal or symlinks.
3. Codex reports success without the expected file or verification evidence.
4. Secret or personal data enters logs, receipts, fixtures, Git history, or a
   release archive.
5. Two callers reuse an operation ID with different content.
6. A stale or unrelated thread/turn is mistaken for the requested operation.
7. Child App Server or verification processes survive cancellation/timeout.
8. An adapter silently broadens filesystem or network permissions.
9. A queued operation is later resumed through a different dispatch path.
10. Remote queue or turn state changes after verification but before success is
    recorded.

## Controls

- durable state is written before dispatch;
- ambiguous dispatch becomes `UNKNOWN` and is never blindly repeated;
- exact IDs plus prompt digest are required for reconciliation;
- dispatch kind is reserved atomically and cannot be reinterpreted on recovery;
- authoritative turn, workspace, result, and queue absence are read again
  immediately before `VERIFIED`;
- immutable terminal state and receipt are inserted in one transaction;
- canonical allowlisted workspace roots;
- no followed symlinks in MVP;
- before/after manifests and explicit changed-path policy;
- argv-only verification with time and output bounds;
- safe Codex sandbox defaults and no automatic escalation;
- prompt/result content omitted from default receipts;
- runtime files live outside Git and are permission-restricted;
- synthetic tests and examples;
- source/history/archive privacy scans before release;
- neutral, deterministic sdist ownership and timestamp metadata before release.

## Release blockers

Publishing is blocked by any of the following:

- unapproved personal name or identity, private email, username, phone,
  chat/user ID, or private hostname; intentional public author attribution is
  allowed only when the owner has approved it;
- absolute user-home path or identifiable machine layout;
- token, cookie, key, credential path, or private endpoint;
- private project, team, profile, Wiki, Kanban, or routing name;
- real prompt, result, receipt, log, session, thread, turn, or task identifier;
- runtime database, workspace snapshot, screenshot, or private Git metadata;
- hidden dependency on an untracked path, private package, alias, or helper;
- failing synthetic clean-environment test.

## Out of scope

The MVP is not an OS sandbox, secrets vault, remote execution service, or
multi-user authorization system. Public ingress and credential storage are
explicitly excluded.

Verification argv is trusted input and runs outside the Codex sandbox with the
operator user's permissions. `shell=false` prevents shell interpretation, not
malicious executable behavior; scope snapshots only detect workspace changes
after execution and do not constrain writes elsewhere.

Workspace snapshots cannot assign causality when another process or turn writes
concurrently. In particular, experimental active-task `queue` receipts describe
the combined final workspace window, not per-turn file attribution. Exact turn
identity and final-state verification remain enforced; causal isolation requires
an idle task or an externally exclusive workspace.
