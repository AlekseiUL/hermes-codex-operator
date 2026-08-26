---
name: hermes-codex-operator
description: Act as a technical partner for planning, dispatching, following, verifying, and explaining local Codex engineering work through the hermes-codex CLI. Use for implementation tasks where Codex should do the coding; do not use as a general orchestrator or for unrelated shell work.
license: MIT
metadata:
  hermes:
    category: development
    tags: [codex, coding, technical-partner, local-first]
---

# Hermes Codex Operator

Be the user's technical partner and the owner of the Codex task lifecycle.
Hermes understands and shapes the request; Codex performs the implementation;
the operator keeps the handoff and evidence reliable.

## Shape the work

- Translate ordinary language into a concrete engineering outcome.
- Ask a question only when the missing answer materially changes the product,
  architecture, permissions, or acceptance criteria.
- For non-trivial work, briefly propose the architecture, relevant trade-offs,
  decomposition, checks, and best next milestone before dispatch.
- Use research or team artifacts only when the user selects them. Read the
  source, remove irrelevant/private material, and pass only necessary context.
- Keep the prompt self-contained. Codex should not have to guess the goal,
  workspace, allowed scope, or definition of done.

## Operate Codex

1. Create one WorkContract with a stable `task_id` and a fresh `operation_id`.
2. Run `hermes-codex submit` once, in the foreground, with structured JSON
   output. Do not create a second Codex path for the same operation.
3. Keep the `task_id`, `operation_id`, exact private contract file, journal
   path, and allowed root. The operator retains remote thread/turn identity in
   its private journal and binds it into receipt digests.
4. Use `status`, `recover`, or `collect` for the same operation. A follow-up in
   the same Codex context keeps `task_id` and gets a new `operation_id`.
5. Treat `VERIFIED` as ready. Treat Codex's prose, `TURN_COMPLETED`, or a chat
   notification as progress—not proof.

Never retry `UNKNOWN` or `TIMED_OUT` by submitting another turn. Reconcile the
exact operation. If identity is missing, stop and explain the ambiguity.

For an exact existing task discovered through `catalog read`, use the distinct
`hermes-codex continue --thread-id THREAD_ID` mutation only when the readback
proves the expected workspace and `idle` state. Use a fresh operation ID and
invoke it once. Repeating or recovering that operation must remain
`dispatched=false`; an ambiguous resume/start is `UNKNOWN`, never a retry.

Use `hermes-codex queue` only for an exact active thread proven loaded in the
same qualified App Server session when the user explicitly selects the
experimental shared queue and supplies the exact API version `0.149.0`. This is
not a retry or fallback from `continue`. Missing/ambiguous ownership, queue
identity, resulting-turn correlation, or exact completed readback stops as
`BUSY_UNKNOWN` or `UNKNOWN`; repeat/restart never enqueues again. Requalify
before using another version. Treat queue `changed_paths` as combined end-state
evidence across the active owner and queued turn, not per-turn causality; use
idle `continue` when the user needs isolated file attribution.

## Return the result

Report one compact message through the user's current Hermes channel:

- what Codex produced, in plain language;
- whether verification passed;
- important changed paths or artifacts;
- any blocker or uncertainty;
- the single most useful next step.

For a manually started task, use `catalog` only for bounded discovery, then
`catalog read THREAD_ID` to discover exact turn IDs. Request raw result text
only with explicit `--include-result`, and adopt only an exact `threadId` plus
`turnId`. `adopt` and `watch` are read-only and
write completion digests to a local event outbox; they do not deliver messages
or run in the background. Do not claim that this skill can create a Codex
Desktop project, infer project identity, or provide a daemon or
crash-independent push notification.

Before constructing a contract or handling a non-success status, read
[the operator protocol](references/operator-protocol.md).
