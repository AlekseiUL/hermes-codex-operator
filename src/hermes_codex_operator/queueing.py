"""Exact-once experimental continuation through the Codex shared queue."""

from __future__ import annotations

import asyncio
from pathlib import Path

from .adapters.app_server import EXPERIMENTAL_QUEUE_API_VERSION
from .adapters.base import (
    AdapterError,
    AdapterTimeout,
    CodexAdapter,
    ProtocolIntegrityError,
    QueuedSubmission,
    ReadbackMissingResult,
    RemoteTurnFailed,
)
from .continuation import MAX_BASELINE_TURNS
from .engine import AdapterFactory, Operator, OperatorOutcome
from .journal import BindingConflict, SQLiteJournal
from .models import OperationState, WorkContract, canonical_json, sha256_text, validate_external_id
from .workspace import WorkspaceError, WorkspacePolicy, resolve_workspace, snapshot_workspace


MAX_RUNTIME_PAGES = 20


class QueueContinuationOperator(Operator):
    """Separate opt-in mutation for one proven active Desktop-owned thread."""

    def __init__(
        self,
        journal: SQLiteJournal,
        adapter_factory: AdapterFactory,
        workspace_policy: WorkspacePolicy,
        *,
        experimental_api_version: str,
    ) -> None:
        if experimental_api_version != EXPERIMENTAL_QUEUE_API_VERSION:
            from .adapters.base import UnsupportedCapability

            raise UnsupportedCapability("UNSUPPORTED_CAPABILITY")
        super().__init__(journal, adapter_factory, workspace_policy)
        self.experimental_api_version = experimental_api_version

    async def continue_active(
        self, contract: WorkContract, *, thread_id: str
    ) -> OperatorOutcome:
        expected_thread = validate_external_id(thread_id, "thread_id")
        reservation = self.journal.reserve_operation(contract)
        with self.journal.operation_guard(contract.operation_id) as owned:
            if not owned or not reservation.created:
                return self._observe_existing(contract)

            try:
                workspace = Path(contract.workspace)
                resolved_workspace = await asyncio.to_thread(
                    resolve_workspace, workspace, self.workspace_policy
                )
                baseline_workspace = await asyncio.to_thread(
                    snapshot_workspace, workspace, self.workspace_policy
                )
            except WorkspaceError:
                return self._terminal_outcome(
                    contract,
                    OperationState.FAILED,
                    "workspace_policy_failed",
                    dispatched=False,
                )

            adapter = self.adapter_factory()
            try:
                try:
                    await self._prove_active_ownership(adapter, expected_thread)
                    remote = await adapter.read_thread(expected_thread)
                    self._validate_active_identity(
                        remote, expected_thread, resolved_workspace
                    )
                    baseline_ids = remote.thread.turn_ids
                    if len(baseline_ids) > MAX_BASELINE_TURNS:
                        raise ProtocolIntegrityError("baseline turn set exceeds its bound")
                except (AdapterError, KeyError, ValueError):
                    return self._terminal_outcome(
                        contract,
                        OperationState.UNKNOWN,
                        "BUSY_UNKNOWN",
                        dispatched=False,
                    )

                self.journal.bind_thread(contract.task_id, expected_thread)
                self.journal.store_baseline(contract.operation_id, baseline_workspace)
                queue_baseline = self.journal.store_queue_baseline(
                    contract.operation_id,
                    expected_thread,
                    contract.prompt_sha256,
                    baseline_ids,
                )
                self.journal.transition(contract.operation_id, OperationState.READY)
                self.journal.transition(
                    contract.operation_id,
                    OperationState.QUEUE_DISPATCH_IN_FLIGHT,
                )

                client_message_id = self._client_message_id(contract)
                input_digest = self._input_digest(contract.prompt)
                try:
                    submission = await adapter.add_queue(
                        expected_thread, contract.prompt, client_message_id
                    )
                    self._validate_submission(
                        submission, client_message_id, input_digest
                    )
                except (AdapterError, KeyError, ValueError):
                    await adapter.close()
                    adapter = self.adapter_factory()
                    try:
                        submission = await self._inspect_queue_once(
                            adapter,
                            expected_thread,
                            client_message_id,
                            input_digest,
                        )
                    except (AdapterError, KeyError, ValueError):
                        return self._terminal_outcome(
                            contract,
                            OperationState.UNKNOWN,
                            "queue_enqueue_unconfirmed",
                            dispatched=True,
                        )
                    if submission is None:
                        return self._terminal_outcome(
                            contract,
                            OperationState.UNKNOWN,
                            "queue_enqueue_unconfirmed",
                            dispatched=True,
                        )

                try:
                    queue_baseline = self.journal.bind_queue_submission(
                        contract.operation_id, submission.submission_id
                    )
                except BindingConflict:
                    return self._terminal_outcome(
                        contract,
                        OperationState.UNKNOWN,
                        "queue_submission_binding_conflict",
                        dispatched=True,
                    )
                assert queue_baseline.submission_id is not None

                try:
                    turn = await adapter.start_queued_turn(
                        expected_thread, queue_baseline.submission_id
                    )
                    if turn.thread_id != expected_thread:
                        raise ProtocolIntegrityError(
                            "thread/queue/start returned a different thread"
                        )
                    correlated = await adapter.read_thread(expected_thread)
                    turn_id = self._unique_new_turn(
                        correlated.thread.turn_ids, baseline_ids
                    )
                    if turn.turn_id != turn_id:
                        raise ProtocolIntegrityError(
                            "thread/queue/start returned an uncorrelated turn"
                        )
                except (AdapterError, KeyError, ValueError):
                    await adapter.close()
                    adapter = self.adapter_factory()
                    try:
                        correlated = await adapter.read_thread(expected_thread)
                        turn_id = self._unique_new_turn(
                            correlated.thread.turn_ids, baseline_ids
                        )
                    except (AdapterError, KeyError, ValueError):
                        return self._terminal_outcome(
                            contract,
                            OperationState.UNKNOWN,
                            "queue_turn_correlation_unknown",
                            dispatched=True,
                        )

                try:
                    self.journal.bind_turn(contract.operation_id, turn_id)
                except BindingConflict:
                    return self._terminal_outcome(
                        contract,
                        OperationState.UNKNOWN,
                        "queue_turn_binding_conflict",
                        dispatched=True,
                    )
                self.journal.transition(contract.operation_id, OperationState.RUNNING)

                try:
                    terminal = await adapter.wait_for_completion(
                        expected_thread, turn_id, float(contract.timeout_seconds)
                    )
                    if terminal != "completed":
                        state = (
                            OperationState.CANCELLED
                            if terminal == "interrupted"
                            else OperationState.FAILED
                        )
                        return self._terminal_outcome(
                            contract,
                            state,
                            "remote_turn_failed",
                            dispatched=True,
                        )
                    readback = await adapter.read_turn(expected_thread, turn_id)
                    if (
                        readback.thread_id != expected_thread
                        or readback.turn_id != turn_id
                        or readback.status != "completed"
                    ):
                        raise ProtocolIntegrityError(
                            "authoritative exact-turn completion is unavailable"
                        )
                except AdapterTimeout:
                    return self._terminal_outcome(
                        contract,
                        OperationState.TIMED_OUT,
                        "turn_timeout",
                        dispatched=True,
                    )
                except RemoteTurnFailed as exc:
                    state = (
                        OperationState.CANCELLED
                        if exc.status == "interrupted"
                        else OperationState.FAILED
                    )
                    return self._terminal_outcome(
                        contract, state, "remote_turn_failed", dispatched=True
                    )
                except (ReadbackMissingResult, AdapterError, KeyError, ValueError):
                    return self._terminal_outcome(
                        contract,
                        OperationState.UNKNOWN,
                        "queue_exact_turn_readback_unknown",
                        dispatched=True,
                    )

                self.journal.transition(
                    contract.operation_id, OperationState.TURN_COMPLETED
                )
                return await self._verify(
                    contract, baseline_workspace, readback.final_text, True
                )
            finally:
                await adapter.close()

    async def _prove_active_ownership(
        self, adapter: CodexAdapter, thread_id: str
    ) -> None:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        matches = 0
        for _ in range(MAX_RUNTIME_PAGES):
            page = await adapter.list_loaded_threads(cursor=cursor, limit=100)
            matches += sum(candidate == thread_id for candidate in page.thread_ids)
            if page.next_cursor is None:
                if matches != 1:
                    raise ProtocolIntegrityError(
                        "active thread ownership is absent or ambiguous"
                    )
                return
            if page.next_cursor in seen_cursors:
                raise ProtocolIntegrityError("loaded-thread pagination repeated a cursor")
            seen_cursors.add(page.next_cursor)
            cursor = page.next_cursor
        raise ProtocolIntegrityError("loaded-thread ownership proof exceeded its bound")

    async def _inspect_queue_once(
        self,
        adapter: CodexAdapter,
        thread_id: str,
        client_message_id: str,
        input_digest: str,
    ) -> QueuedSubmission | None:
        page = await adapter.list_queue(thread_id, limit=100)
        if page.next_cursor is not None:
            raise ProtocolIntegrityError("queue recovery exceeds one bounded inspection")
        submission_ids = tuple(item.submission_id for item in page.submissions)
        if len(set(submission_ids)) != len(submission_ids):
            raise ProtocolIntegrityError("queue recovery returned duplicate submission ids")
        matches = [
            item
            for item in page.submissions
            if item.client_message_id == client_message_id
            and item.input_sha256 == input_digest
        ]
        if len(matches) != 1:
            return None
        return matches[0]

    @staticmethod
    def _validate_active_identity(remote, thread_id: str, workspace: Path) -> None:
        if remote.thread.thread_id != thread_id:
            raise ProtocolIntegrityError("thread/read returned a different thread id")
        try:
            remote_workspace = remote.thread.cwd.resolve(strict=True)
        except OSError as exc:
            raise ProtocolIntegrityError("thread workspace is unavailable") from exc
        if remote_workspace != workspace:
            raise ProtocolIntegrityError("thread workspace identity mismatch")
        if remote.thread.status != "active":
            raise ProtocolIntegrityError("thread is not authoritatively active")
        ids = remote.thread.turn_ids
        if ids != tuple(turn.turn_id for turn in remote.turns):
            raise ProtocolIntegrityError("thread turn identity views disagree")
        if len(set(ids)) != len(ids):
            raise ProtocolIntegrityError("thread/read returned duplicate turn ids")
        if sum(turn.status == "inProgress" for turn in remote.turns) != 1:
            raise ProtocolIntegrityError("active turn ownership is absent or ambiguous")

    @staticmethod
    def _unique_new_turn(
        current_ids: tuple[str, ...], baseline_ids: tuple[str, ...]
    ) -> str:
        if len(set(current_ids)) != len(current_ids):
            raise ProtocolIntegrityError("post-queue turn set contains duplicates")
        baseline = set(baseline_ids)
        current = set(current_ids)
        candidates = current - baseline
        if len(candidates) != 1 or baseline - current:
            raise ProtocolIntegrityError(
                "post-queue turn set does not contain exactly one correlated new turn"
            )
        return next(iter(candidates))

    @staticmethod
    def _client_message_id(contract: WorkContract) -> str:
        digest = sha256_text(
            canonical_json(
                {
                    "operation_id": contract.operation_id,
                    "prompt_sha256": contract.prompt_sha256,
                }
            )
        )
        return f"hco-{digest[:40]}"

    @staticmethod
    def _input_digest(prompt: str) -> str:
        return sha256_text(
            canonical_json({"input": [{"text": prompt, "type": "text"}]})
        )

    @staticmethod
    def _validate_submission(
        submission: QueuedSubmission,
        client_message_id: str,
        input_digest: str,
    ) -> None:
        validate_external_id(submission.submission_id, "submission_id")
        if (
            submission.client_message_id != client_message_id
            or submission.input_sha256 != input_digest
        ):
            raise ProtocolIntegrityError("queued submission identity mismatch")


__all__ = ["QueueContinuationOperator"]
