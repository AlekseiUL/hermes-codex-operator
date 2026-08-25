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

## Controls

- durable state is written before dispatch;
- ambiguous dispatch becomes `UNKNOWN` and is never blindly repeated;
- exact IDs plus prompt digest are required for reconciliation;
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

- real name, email, username, phone, chat/user ID, or private hostname;
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
