"""At-most-once experimental dispatch with exact shared-queue correlation."""

from __future__ import annotations

import asyncio
from pathlib import Path

from .adapters.app_server import (
    EXPERIMENTAL_QUEUE_API_VERSION,
    QueueIdentityConflict,
)
from .adapters.base import (
    AdapterError,
    AdapterTimeout,
    CodexAdapter,
    ProtocolIntegrityError,
    QueuedSubmission,
    ReadbackMissingResult,
    RemoteTurnFailed,
    TurnReadback,
    UnsupportedCapability,
)
from .continuation import MAX_BASELINE_TURNS
from .engine import AdapterFactory, Operator, OperatorOutcome
from .journal import BindingConflict, SQLiteJournal
from .models import OperationState, WorkContract, canonical_json, sha256_text, validate_external_id
from .workspace import WorkspaceError, WorkspacePolicy, resolve_workspace, snapshot_workspace


MAX_RUNTIME_PAGES = 20
QUEUE_AUTOCONSUME_POLLS = 5
QUEUE_AUTOCONSUME_POLL_SECONDS = 0.1


class QueueContinuationOperator(Operator):
    """Separate opt-in mutation for one proven active loaded thread."""

    def __init__(
        self,
        journal: SQLiteJournal,
        adapter_factory: AdapterFactory,
        workspace_policy: WorkspacePolicy,
        *,
        experimental_api_version: str,
    ) -> None:
        if experimental_api_version != EXPERIMENTAL_QUEUE_API_VERSION:
            raise UnsupportedCapability("UNSUPPORTED_CAPABILITY")
        super().__init__(journal, adapter_factory, workspace_policy)
        self.experimental_api_version = experimental_api_version

    async def continue_active(
        self, contract: WorkContract, *, thread_id: str
    ) -> OperatorOutcome:
        expected_thread = validate_external_id(thread_id, "thread_id")
        reservation = self.journal.reserve_operation(
            contract, dispatch_kind="queue"
        )
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
                    active_turn_id = self._validate_active_identity(
                        remote, expected_thread, resolved_workspace
                    )
                    baseline_ids = remote.thread.turn_ids
                    if len(baseline_ids) > MAX_BASELINE_TURNS:
                        raise ProtocolIntegrityError("baseline turn set exceeds its bound")
                except UnsupportedCapability:
                    return self._terminal_outcome(
                        contract,
                        OperationState.FAILED,
                        "UNSUPPORTED_CAPABILITY",
                        dispatched=False,
                    )
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
                except QueueIdentityConflict:
                    return self._terminal_outcome(
                        contract,
                        OperationState.UNKNOWN,
                        "queue_enqueue_identity_conflict",
                        dispatched=True,
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
                else:
                    try:
                        self._validate_submission(
                            submission, client_message_id, input_digest
                        )
                    except (ProtocolIntegrityError, KeyError, ValueError):
                        return self._terminal_outcome(
                            contract,
                            OperationState.UNKNOWN,
                            "queue_enqueue_identity_conflict",
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
                    owner_terminal = await adapter.wait_for_completion(
                        expected_thread,
                        active_turn_id,
                        float(contract.timeout_seconds),
                    )
                    if owner_terminal not in {"completed", "failed", "interrupted"}:
                        raise ProtocolIntegrityError(
                            "active owner turn returned an unknown terminal status"
                        )
                    owner_readback = await adapter.read_thread(expected_thread)
                    self._validate_thread_identity(
                        owner_readback, expected_thread, resolved_workspace
                    )
                    authoritative_owner = tuple(
                        turn
                        for turn in owner_readback.turns
                        if turn.turn_id == active_turn_id
                    )
                    if (
                        len(authoritative_owner) != 1
                        or authoritative_owner[0].status
                        not in {"completed", "failed", "interrupted"}
                        or authoritative_owner[0].status != owner_terminal
                    ):
                        raise ProtocolIntegrityError(
                            "active owner terminal notification and readback disagree"
                        )
                except (AdapterError, KeyError, ValueError):
                    return self._terminal_outcome(
                        contract,
                        OperationState.UNKNOWN,
                        "queue_owner_turn_completion_unknown",
                        dispatched=True,
                    )

                try:
                    turn_id, submission_present = await self._await_auto_consumed_turn(
                        adapter,
                        expected_thread,
                        resolved_workspace,
                        baseline_ids,
                        submission,
                    )
                except (AdapterError, KeyError, ValueError):
                    return self._terminal_outcome(
                        contract,
                        OperationState.UNKNOWN,
                        "queue_turn_correlation_unknown",
                        dispatched=True,
                    )

                if turn_id is None:
                    if not submission_present:
                        return self._terminal_outcome(
                            contract,
                            OperationState.UNKNOWN,
                            "queue_turn_correlation_unknown",
                            dispatched=True,
                        )
                    try:
                        turn = await adapter.start_queued_turn(
                            expected_thread, queue_baseline.submission_id
                        )
                    except (AdapterError, KeyError, ValueError):
                        try:
                            turn_id, _ = await self._await_auto_consumed_turn(
                                adapter,
                                expected_thread,
                                resolved_workspace,
                                baseline_ids,
                                submission,
                            )
                        except (AdapterError, KeyError, ValueError):
                            turn_id = None
                        if turn_id is None:
                            return self._terminal_outcome(
                                contract,
                                OperationState.UNKNOWN,
                                "queue_turn_correlation_unknown",
                                dispatched=True,
                            )
                    else:
                        if turn.thread_id != expected_thread:
                            return self._terminal_outcome(
                                contract,
                                OperationState.UNKNOWN,
                                "queue_turn_identity_conflict",
                                dispatched=True,
                            )
                        turn_id = turn.turn_id
                        try:
                            submission_present = await self._submission_is_present(
                                adapter, expected_thread, submission
                            )
                        except (AdapterError, KeyError, ValueError):
                            return self._terminal_outcome(
                                contract,
                                OperationState.UNKNOWN,
                                "queue_turn_identity_conflict",
                                dispatched=True,
                            )
                        if submission_present:
                            return self._terminal_outcome(
                                contract,
                                OperationState.UNKNOWN,
                                "queue_turn_identity_conflict",
                                dispatched=True,
                            )
                        try:
                            immediate = await adapter.read_thread(expected_thread)
                            immediate_ids = self._new_turn_ids(
                                immediate.thread.turn_ids, baseline_ids
                            )
                        except (AdapterError, KeyError, ValueError):
                            immediate_ids = ()
                        if immediate_ids and immediate_ids != (turn_id,):
                            return self._terminal_outcome(
                                contract,
                                OperationState.UNKNOWN,
                                "queue_turn_identity_conflict",
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
                    correlated = await adapter.read_thread(expected_thread)
                    self._validate_thread_identity(
                        correlated, expected_thread, resolved_workspace
                    )
                    if (
                        self._correlated_new_turn(
                            correlated, baseline_ids, submission
                        )
                        != turn_id
                    ):
                        raise ProtocolIntegrityError(
                            "authoritative resulting-turn identity is inconsistent"
                        )
                    terminal_turns = tuple(
                        turn for turn in correlated.turns if turn.turn_id == turn_id
                    )
                    if (
                        len(terminal_turns) != 1
                        or terminal_turns[0].status
                        not in {"completed", "failed", "interrupted"}
                        or terminal_turns[0].status != terminal
                    ):
                        raise ProtocolIntegrityError(
                            "target terminal notification and readback disagree"
                        )
                    if terminal != "completed":
                        if await self._submission_is_present(
                            adapter, expected_thread, submission
                        ):
                            raise ProtocolIntegrityError(
                                "exact queued message remains pending after termination"
                            )
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
                    readback = await self._read_completed_remote_evidence(
                        adapter,
                        expected_thread,
                        resolved_workspace,
                        baseline_ids,
                        submission,
                        turn_id,
                    )
                except AdapterTimeout:
                    return self._terminal_outcome(
                        contract,
                        OperationState.TIMED_OUT,
                        "turn_timeout",
                        dispatched=True,
                    )
                except RemoteTurnFailed:
                    return self._terminal_outcome(
                        contract,
                        OperationState.UNKNOWN,
                        "queue_exact_turn_readback_unknown",
                        dispatched=True,
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

                async def confirm_remote_evidence() -> None:
                    await self._read_completed_remote_evidence(
                        adapter,
                        expected_thread,
                        resolved_workspace,
                        baseline_ids,
                        submission,
                        turn_id,
                        expected_final_text=readback.final_text,
                    )

                return await self._verify(
                    contract,
                    baseline_workspace,
                    readback.final_text,
                    True,
                    before_verified=confirm_remote_evidence,
                    before_verified_failure_code="queue_exact_turn_readback_unknown",
                )
            finally:
                await adapter.close()

    async def _read_completed_remote_evidence(
        self,
        adapter: CodexAdapter,
        thread_id: str,
        workspace: Path,
        baseline_ids: tuple[str, ...],
        submission: QueuedSubmission,
        turn_id: str,
        *,
        expected_final_text: str | None = None,
    ) -> TurnReadback:
        readback = await adapter.read_turn(thread_id, turn_id)
        if (
            readback.thread_id != thread_id
            or readback.turn_id != turn_id
            or readback.status != "completed"
            or not readback.final_text
            or (
                expected_final_text is not None
                and readback.final_text != expected_final_text
            )
        ):
            raise ProtocolIntegrityError(
                "authoritative exact-turn completion is unavailable"
            )
        correlated = await adapter.read_thread(thread_id)
        self._validate_thread_identity(correlated, thread_id, workspace)
        if self._correlated_new_turn(correlated, baseline_ids, submission) != turn_id:
            raise ProtocolIntegrityError(
                "authoritative resulting-turn identity is inconsistent"
            )
        final_turns = tuple(
            turn for turn in correlated.turns if turn.turn_id == turn_id
        )
        if len(final_turns) != 1:
            raise ProtocolIntegrityError(
                "authoritative resulting turn is absent or duplicated"
            )
        final_turn = final_turns[0]
        if (
            final_turn.status != "completed"
            or not final_turn.final_text
            or final_turn.final_text != readback.final_text
        ):
            raise ProtocolIntegrityError(
                "authoritative resulting-turn readbacks disagree"
            )
        if await self._submission_is_present(adapter, thread_id, submission):
            raise ProtocolIntegrityError(
                "exact queued message remains pending after completion"
            )
        return readback

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
        client_matches = [
            item
            for item in page.submissions
            if item.client_message_id == client_message_id
        ]
        if any(item.input_sha256 != input_digest for item in client_matches):
            raise ProtocolIntegrityError(
                "queue recovery reused the client message id with different input"
            )
        if len(client_matches) != 1:
            return None
        return client_matches[0]

    async def _submission_is_present(
        self,
        adapter: CodexAdapter,
        thread_id: str,
        submission: QueuedSubmission,
    ) -> bool:
        page = await adapter.list_queue(thread_id, limit=100)
        if page.next_cursor is not None:
            raise ProtocolIntegrityError("queue inspection exceeds one page")
        ids = tuple(item.submission_id for item in page.submissions)
        if len(set(ids)) != len(ids):
            raise ProtocolIntegrityError("queue inspection returned duplicate ids")
        id_matches = [
            item
            for item in page.submissions
            if item.submission_id == submission.submission_id
        ]
        if len(id_matches) > 1:
            raise ProtocolIntegrityError("queue submission identity is ambiguous")
        if id_matches:
            self._validate_submission(
                id_matches[0],
                submission.client_message_id,
                submission.input_sha256,
            )
        client_matches = [
            item
            for item in page.submissions
            if item.client_message_id == submission.client_message_id
        ]
        if any(
            item.input_sha256 != submission.input_sha256 for item in client_matches
        ):
            raise ProtocolIntegrityError(
                "queue reused the client message id with different input"
            )
        exact_identity_matches = [
            item
            for item in page.submissions
            if item.client_message_id == submission.client_message_id
            and item.input_sha256 == submission.input_sha256
        ]
        if len(exact_identity_matches) > 1:
            raise ProtocolIntegrityError("exact queue message identity is ambiguous")
        return bool(id_matches or exact_identity_matches)

    async def _await_auto_consumed_turn(
        self,
        adapter: CodexAdapter,
        thread_id: str,
        workspace: Path,
        baseline_ids: tuple[str, ...],
        submission: QueuedSubmission,
    ) -> tuple[str | None, bool]:
        submission_present = False
        for attempt in range(QUEUE_AUTOCONSUME_POLLS):
            current = await adapter.read_thread(thread_id)
            self._validate_thread_identity(current, thread_id, workspace)
            new_ids = self._new_turn_ids(current.thread.turn_ids, baseline_ids)
            correlated_turn_id = self._correlated_new_turn(
                current, baseline_ids, submission
            )
            submission_present = await self._submission_is_present(
                adapter, thread_id, submission
            )
            if submission_present:
                if new_ids:
                    raise ProtocolIntegrityError(
                        "a new turn appeared while the exact queued submission "
                        "remains pending"
                    )
            elif correlated_turn_id is not None:
                return correlated_turn_id, False
            if attempt + 1 < QUEUE_AUTOCONSUME_POLLS:
                await asyncio.sleep(QUEUE_AUTOCONSUME_POLL_SECONDS)
        return None, submission_present

    @classmethod
    def _correlated_new_turn(
        cls,
        current,
        baseline_ids: tuple[str, ...],
        submission: QueuedSubmission,
    ) -> str | None:
        new_ids = cls._new_turn_ids(current.thread.turn_ids, baseline_ids)
        if current.thread.turn_ids != tuple(turn.turn_id for turn in current.turns):
            raise ProtocolIntegrityError("thread turn identity views disagree")
        new_id_set = set(new_ids)
        matching_turns: list[str] = []
        for turn in current.turns:
            if turn.turn_id not in new_id_set:
                continue
            client_messages = [
                message
                for message in turn.user_messages
                if message.client_message_id == submission.client_message_id
            ]
            if any(
                message.input_sha256 != submission.input_sha256
                for message in client_messages
            ):
                raise ProtocolIntegrityError(
                    "a resulting turn reused the client message id "
                    "with different input"
                )
            exact_messages = [
                message
                for message in client_messages
                if message.input_sha256 == submission.input_sha256
            ]
            if exact_messages and len(turn.user_messages) != 1:
                raise ProtocolIntegrityError(
                    "the exact queued message shares a turn with other user input"
                )
            if exact_messages:
                matching_turns.append(turn.turn_id)
        if len(matching_turns) > 1:
            raise ProtocolIntegrityError(
                "multiple resulting turns contain the exact queued message"
            )
        return matching_turns[0] if matching_turns else None

    @staticmethod
    def _validate_active_identity(remote, thread_id: str, workspace: Path) -> str:
        QueueContinuationOperator._validate_thread_identity(
            remote, thread_id, workspace
        )
        if remote.thread.status != "active":
            raise ProtocolIntegrityError("thread is not authoritatively active")
        active_turn_ids = tuple(
            turn.turn_id for turn in remote.turns if turn.status == "inProgress"
        )
        if len(active_turn_ids) != 1:
            raise ProtocolIntegrityError("active turn ownership is absent or ambiguous")
        return active_turn_ids[0]

    @staticmethod
    def _validate_thread_identity(remote, thread_id: str, workspace: Path) -> None:
        if remote.thread.thread_id != thread_id:
            raise ProtocolIntegrityError("thread/read returned a different thread id")
        try:
            remote_workspace = remote.thread.cwd.resolve(strict=True)
        except OSError as exc:
            raise ProtocolIntegrityError("thread workspace is unavailable") from exc
        if remote_workspace != workspace:
            raise ProtocolIntegrityError("thread workspace identity mismatch")
        ids = remote.thread.turn_ids
        if ids != tuple(turn.turn_id for turn in remote.turns):
            raise ProtocolIntegrityError("thread turn identity views disagree")
        if len(set(ids)) != len(ids):
            raise ProtocolIntegrityError("thread/read returned duplicate turn ids")

    @staticmethod
    def _new_turn_ids(
        current_ids: tuple[str, ...], baseline_ids: tuple[str, ...]
    ) -> tuple[str, ...]:
        if len(set(current_ids)) != len(current_ids):
            raise ProtocolIntegrityError("post-queue turn set contains duplicates")
        baseline = set(baseline_ids)
        current = set(current_ids)
        if baseline - current:
            raise ProtocolIntegrityError(
                "post-queue turn set dropped a baseline turn"
            )
        return tuple(turn_id for turn_id in current_ids if turn_id not in baseline)

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
            canonical_json(
                {
                    "input": [
                        {"text": prompt, "text_elements": [], "type": "text"}
                    ]
                }
            )
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
