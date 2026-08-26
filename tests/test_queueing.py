from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import tempfile
from typing import cast
import unittest

from hermes_codex_operator.adapters.app_server import EXPERIMENTAL_QUEUE_API_VERSION
from hermes_codex_operator.adapters.base import (
    CatalogTurn,
    CatalogUserMessage,
    DeliveryUnknown,
    ProtocolIntegrityError,
    QueuedSubmission,
    ThreadCatalogReadback,
    ThreadSummary,
    TurnHandle,
    TurnTerminalStatus,
    UnsupportedCapability,
)
from hermes_codex_operator.adapters.fake import FakeBackend, FakeCodexAdapter
from hermes_codex_operator.engine import Operator
from hermes_codex_operator.journal import SQLiteJournal
from hermes_codex_operator.models import OperationState, WorkContract
from hermes_codex_operator.queueing import (
    QUEUE_AUTOCONSUME_POLLS,
    QueueContinuationOperator,
)
from hermes_codex_operator.workspace import WorkspacePolicy


def queue_contract(workspace: Path, operation_id: str = "synthetic-queue-001") -> WorkContract:
    return WorkContract.from_dict(
        {
            "schema_version": "1",
            "task_id": "synthetic-active-task",
            "operation_id": operation_id,
            "workspace": str(workspace),
            "prompt": "Queue the exact synthetic active task once.",
            "allowed_change_globs": ["src/**"],
            "verification": [[sys.executable, "-c", "raise SystemExit(0)"]],
            "timeout_seconds": 5,
        }
    )


class QueueContinuationOperatorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        (self.workspace / "src").mkdir(parents=True)
        (self.workspace / "src" / "app.py").write_text("OLD = True\n", encoding="utf-8")
        self.database = self.root / "state.sqlite3"
        self.journal = SQLiteJournal(self.database)
        self.thread_id = "thread-synthetic-active"
        self.old_turn = "turn-synthetic-active"
        self.new_turn = "turn-synthetic-queued"

    def tearDown(self) -> None:
        self.journal.close()
        self.temp.cleanup()

    def backend(self, **kwargs) -> FakeBackend:
        backend = FakeBackend(
            thread_id=self.thread_id,
            turn_id=self.new_turn,
            **kwargs,
        )
        backend.threads[self.thread_id] = self.workspace
        backend.turns[(self.thread_id, self.old_turn)] = "inProgress"  # type: ignore[assignment]
        backend.thread_statuses[self.thread_id] = "active"
        backend.loaded_thread_ids = [self.thread_id]
        return backend

    def operator(self, backend: FakeBackend, adapter=FakeCodexAdapter, journal=None):
        return QueueContinuationOperator(
            journal or self.journal,
            lambda: adapter(backend),
            WorkspacePolicy((self.root,)),
            experimental_api_version=EXPERIMENTAL_QUEUE_API_VERSION,
        )

    async def test_exact_opt_in_happy_path_binds_submission_and_turn(self) -> None:
        def mutate(workspace: Path) -> None:
            (workspace / "src" / "app.py").write_text("NEW = True\n", encoding="utf-8")

        backend = self.backend(mutate_workspace=mutate)
        contract = queue_contract(self.workspace)
        outcome = await self.operator(backend).continue_active(
            contract, thread_id=self.thread_id
        )

        self.assertEqual(outcome.operation.state, OperationState.VERIFIED)
        self.assertTrue(outcome.dispatched)
        self.assertEqual(outcome.operation.turn_id, self.new_turn)
        self.assertEqual(backend.queue_add_calls, 1)
        self.assertEqual(backend.queue_start_calls, 1)
        self.assertEqual(backend.wait_turn_ids, [self.old_turn, self.new_turn])
        baseline = self.journal.get_queue_baseline(contract.operation_id)
        self.assertEqual(baseline.thread_id, self.thread_id)
        self.assertEqual(baseline.turn_ids, (self.old_turn,))
        self.assertEqual(baseline.submission_id, backend.submission_id)

    async def test_repeat_and_restart_never_enqueue_again(self) -> None:
        backend = self.backend()
        contract = queue_contract(self.workspace)
        first = await self.operator(backend).continue_active(contract, thread_id=self.thread_id)
        self.assertEqual(first.operation.state, OperationState.VERIFIED)
        duplicate = await self.operator(backend).continue_active(contract, thread_id=self.thread_id)
        reopened = SQLiteJournal(self.database)
        try:
            restarted = await self.operator(backend, journal=reopened).continue_active(
                contract, thread_id=self.thread_id
            )
        finally:
            reopened.close()
        self.assertFalse(duplicate.dispatched)
        self.assertFalse(restarted.dispatched)
        self.assertEqual(backend.queue_add_calls, 1)
        self.assertEqual(backend.queue_start_calls, 1)

    async def test_queue_start_waits_for_active_owner_turn_terminal(self) -> None:
        backend = self.backend()

        class OrderingAdapter(FakeCodexAdapter):
            async def start_queued_turn(self, thread_id, submission_id):
                if self.backend.turns[(thread_id, "turn-synthetic-active")] == "inProgress":
                    raise AssertionError(
                        "queue/start ran before the active owner turn ended"
                    )
                return await super().start_queued_turn(thread_id, submission_id)

        outcome = await self.operator(backend, OrderingAdapter).continue_active(
            queue_contract(self.workspace, "queue-ordering-001"),
            thread_id=self.thread_id,
        )

        self.assertEqual(outcome.operation.state, OperationState.VERIFIED)
        self.assertEqual(backend.wait_turn_ids, [self.old_turn, self.new_turn])

    async def test_owner_notification_cannot_replace_terminal_thread_read(self) -> None:
        backend = self.backend()

        class PrematureOwnerNotificationAdapter(FakeCodexAdapter):
            async def wait_for_completion(self, thread_id, turn_id, timeout):
                if turn_id == "turn-synthetic-active":
                    self.backend.wait_turn_ids.append(turn_id)
                    return "completed"
                return await super().wait_for_completion(thread_id, turn_id, timeout)

        outcome = await self.operator(
            backend, PrematureOwnerNotificationAdapter
        ).continue_active(
            queue_contract(self.workspace, "queue-owner-readback-001"),
            thread_id=self.thread_id,
        )

        self.assertEqual(outcome.operation.state, OperationState.UNKNOWN)
        self.assertEqual(
            outcome.operation.failure_code,
            "queue_owner_turn_completion_unknown",
        )
        self.assertTrue(outcome.dispatched)
        self.assertEqual(backend.queue_add_calls, 1)
        self.assertEqual(backend.queue_start_calls, 0)

    async def test_active_queue_auto_consumption_does_not_call_queue_start(self) -> None:
        def mutate(workspace: Path) -> None:
            (workspace / "src" / "app.py").write_text("AUTO = True\n", encoding="utf-8")

        backend = self.backend(mutate_workspace=mutate)

        class AutoConsumingAdapter(FakeCodexAdapter):
            async def wait_for_completion(self, thread_id, turn_id, timeout):
                terminal = await super().wait_for_completion(
                    thread_id, turn_id, timeout
                )
                if turn_id == "turn-synthetic-active":
                    submission = self.backend.queued[thread_id][0]
                    self.backend.queued[thread_id].clear()
                    self.backend.turn_submissions[
                        (thread_id, self.backend.turn_id)
                    ] = submission
                    if self.backend.mutate_workspace is not None:
                        self.backend.mutate_workspace(self.backend.threads[thread_id])
                    self.backend.turns[(thread_id, self.backend.turn_id)] = "completed"
                return terminal

        outcome = await self.operator(backend, AutoConsumingAdapter).continue_active(
            queue_contract(self.workspace, "queue-auto-consume-001"),
            thread_id=self.thread_id,
        )

        self.assertEqual(outcome.operation.state, OperationState.VERIFIED)
        self.assertTrue(outcome.dispatched)
        self.assertEqual(backend.queue_add_calls, 1)
        self.assertEqual(backend.queue_start_calls, 0)
        self.assertEqual(backend.wait_turn_ids, [self.old_turn, self.new_turn])

    async def test_unrelated_new_turn_while_submission_remains_queued_fails_closed(
        self,
    ) -> None:
        backend = self.backend()
        contract = queue_contract(self.workspace, "queue-unrelated-turn-001")

        class UnrelatedTurnAdapter(FakeCodexAdapter):
            async def wait_for_completion(self, thread_id, turn_id, timeout):
                terminal = await super().wait_for_completion(thread_id, turn_id, timeout)
                if turn_id == "turn-synthetic-active":
                    self.backend.turns[(thread_id, "turn-unrelated")] = "completed"
                return terminal

        first = await self.operator(backend, UnrelatedTurnAdapter).continue_active(
            contract, thread_id=self.thread_id
        )
        repeat = await self.operator(backend).continue_active(
            contract, thread_id=self.thread_id
        )

        self.assertEqual(first.operation.state, OperationState.UNKNOWN)
        self.assertEqual(first.operation.failure_code, "queue_turn_correlation_unknown")
        self.assertTrue(first.dispatched)
        self.assertFalse(repeat.dispatched)
        self.assertEqual(backend.queue_add_calls, 1)
        self.assertEqual(backend.queue_start_calls, 0)
        self.assertEqual(len(backend.queued[self.thread_id]), 1)

    async def test_queue_disappearance_cannot_bind_an_unrelated_turn(self) -> None:
        backend = self.backend()
        contract = queue_contract(self.workspace, "queue-disappearance-race-001")

        class DisappearingBetweenReadsAdapter(FakeCodexAdapter):
            async def wait_for_completion(self, thread_id, turn_id, timeout):
                terminal = await super().wait_for_completion(thread_id, turn_id, timeout)
                if turn_id == "turn-synthetic-active":
                    self.backend.turns[(thread_id, "turn-unrelated")] = "completed"
                return terminal

            async def list_queue(self, thread_id, *, cursor=None, limit=100):
                if self.backend.queue_list_calls == 0:
                    self.backend.queued[thread_id].clear()
                return await super().list_queue(
                    thread_id, cursor=cursor, limit=limit
                )

        outcome = await self.operator(
            backend, DisappearingBetweenReadsAdapter
        ).continue_active(contract, thread_id=self.thread_id)

        self.assertEqual(outcome.operation.state, OperationState.UNKNOWN)
        self.assertEqual(
            outcome.operation.failure_code, "queue_turn_correlation_unknown"
        )
        self.assertTrue(outcome.dispatched)
        self.assertEqual(backend.queue_add_calls, 1)
        self.assertEqual(backend.queue_start_calls, 0)
        self.assertNotEqual(outcome.operation.turn_id, "turn-unrelated")

        reopened = SQLiteJournal(self.database)
        try:
            restarted = await self.operator(
                backend, journal=reopened
            ).continue_active(contract, thread_id=self.thread_id)
        finally:
            reopened.close()
        self.assertFalse(restarted.dispatched)
        self.assertEqual(backend.queue_add_calls, 1)

    async def test_auto_consumed_turn_requires_exact_client_and_input_identity(
        self,
    ) -> None:
        for mode in ("client", "input"):
            with self.subTest(mode=mode):
                journal = SQLiteJournal(self.root / f"auto-identity-{mode}.sqlite3")
                backend = self.backend()
                contract = queue_contract(
                    self.workspace, f"queue-auto-identity-{mode}-001"
                )

                class WrongIdentityAutoConsumeAdapter(FakeCodexAdapter):
                    async def wait_for_completion(self, thread_id, turn_id, timeout):
                        terminal = await super().wait_for_completion(
                            thread_id, turn_id, timeout
                        )
                        if turn_id == "turn-synthetic-active":
                            exact = self.backend.queued[thread_id][0]
                            self.backend.queued[thread_id].clear()
                            self.backend.turn_submissions[
                                (thread_id, self.backend.turn_id)
                            ] = QueuedSubmission(
                                exact.submission_id,
                                "client-unrelated"
                                if mode == "client"
                                else exact.client_message_id,
                                QueueContinuationOperator._input_digest(
                                    "Different queued input."
                                )
                                if mode == "input"
                                else exact.input_sha256,
                            )
                            self.backend.turns[
                                (thread_id, self.backend.turn_id)
                            ] = "completed"
                        return terminal

                try:
                    outcome = await self.operator(
                        backend,
                        WrongIdentityAutoConsumeAdapter,
                        journal=journal,
                    ).continue_active(contract, thread_id=self.thread_id)
                finally:
                    journal.close()

                self.assertEqual(outcome.operation.state, OperationState.UNKNOWN)
                self.assertEqual(
                    outcome.operation.failure_code,
                    "queue_turn_correlation_unknown",
                )
                self.assertTrue(outcome.dispatched)
                self.assertEqual(backend.queue_add_calls, 1)
                self.assertEqual(backend.queue_start_calls, 0)

    async def test_auto_consumed_turn_with_extra_user_input_is_unknown(self) -> None:
        backend = self.backend()
        contract = queue_contract(self.workspace, "queue-auto-steered-001")

        class SteeredAutoConsumeAdapter(FakeCodexAdapter):
            async def wait_for_completion(self, thread_id, turn_id, timeout):
                terminal = await super().wait_for_completion(
                    thread_id, turn_id, timeout
                )
                if turn_id == "turn-synthetic-active":
                    submission = self.backend.queued[thread_id][0]
                    self.backend.queued[thread_id].clear()
                    self.backend.turn_submissions[
                        (thread_id, self.backend.turn_id)
                    ] = submission
                    self.backend.turns[
                        (thread_id, self.backend.turn_id)
                    ] = "completed"
                return terminal

            async def read_thread(self, thread_id):
                value = await super().read_thread(thread_id)
                turns = tuple(
                    CatalogTurn(
                        turn.turn_id,
                        turn.status,
                        turn.final_text,
                        turn.user_messages
                        + (
                            CatalogUserMessage(
                                "client-steer-unrelated",
                                QueueContinuationOperator._input_digest(
                                    "Additional instruction."
                                ),
                            ),
                        ),
                    )
                    if turn.turn_id == self.backend.turn_id
                    else turn
                    for turn in value.turns
                )
                return ThreadCatalogReadback(value.thread, turns)

        outcome = await self.operator(
            backend, SteeredAutoConsumeAdapter
        ).continue_active(contract, thread_id=self.thread_id)

        self.assertEqual(outcome.operation.state, OperationState.UNKNOWN)
        self.assertEqual(
            outcome.operation.failure_code, "queue_turn_correlation_unknown"
        )
        self.assertTrue(outcome.dispatched)
        self.assertEqual(backend.queue_add_calls, 1)
        self.assertEqual(backend.queue_start_calls, 0)

    async def test_auto_consume_rejects_same_client_id_with_different_input(
        self,
    ) -> None:
        backend = self.backend()
        contract = queue_contract(
            self.workspace,
            "queue-auto-client-input-conflict-001",
        )

        class ConflictingClientAutoConsumeAdapter(FakeCodexAdapter):
            async def wait_for_completion(self, thread_id, turn_id, timeout):
                terminal = await super().wait_for_completion(
                    thread_id, turn_id, timeout
                )
                if turn_id == "turn-synthetic-active":
                    exact = self.backend.queued[thread_id][0]
                    self.backend.queued[thread_id].clear()
                    self.backend.turn_submissions[
                        (thread_id, self.backend.turn_id)
                    ] = exact
                    self.backend.turns[
                        (thread_id, self.backend.turn_id)
                    ] = "completed"
                    self.backend.turn_submissions[
                        (thread_id, "turn-conflicting-client")
                    ] = QueuedSubmission(
                        "submission-conflicting-client",
                        exact.client_message_id,
                        "0" * 64,
                    )
                    self.backend.turns[
                        (thread_id, "turn-conflicting-client")
                    ] = "completed"
                return terminal

        outcome = await self.operator(
            backend, ConflictingClientAutoConsumeAdapter
        ).continue_active(contract, thread_id=self.thread_id)

        self.assertEqual(outcome.operation.state, OperationState.UNKNOWN)
        self.assertEqual(
            outcome.operation.failure_code,
            "queue_turn_correlation_unknown",
        )
        self.assertTrue(outcome.dispatched)
        self.assertEqual(backend.queue_add_calls, 1)
        self.assertEqual(backend.queue_start_calls, 0)

    async def test_final_thread_read_must_match_completed_exact_turn_readback(
        self,
    ) -> None:
        for mode in ("status", "text"):
            with self.subTest(mode=mode):
                journal = SQLiteJournal(self.root / f"final-read-{mode}.sqlite3")
                backend = self.backend()
                contract = queue_contract(
                    self.workspace, f"queue-final-read-{mode}-001"
                )

                class ContradictoryFinalReadAdapter(FakeCodexAdapter):
                    async def read_thread(self, thread_id):
                        value = await super().read_thread(thread_id)
                        if self.backend.read_turn_calls == 0:
                            return value
                        turns = tuple(
                            CatalogTurn(
                                turn.turn_id,
                                "inProgress" if mode == "status" else turn.status,
                                None
                                if mode == "status"
                                else "Different authoritative result.",
                                turn.user_messages,
                            )
                            if turn.turn_id == self.backend.turn_id
                            else turn
                            for turn in value.turns
                        )
                        return ThreadCatalogReadback(value.thread, turns)

                try:
                    outcome = await self.operator(
                        backend,
                        ContradictoryFinalReadAdapter,
                        journal=journal,
                    ).continue_active(contract, thread_id=self.thread_id)
                finally:
                    journal.close()

                self.assertEqual(outcome.operation.state, OperationState.UNKNOWN)
                self.assertEqual(
                    outcome.operation.failure_code,
                    "queue_exact_turn_readback_unknown",
                )
                self.assertTrue(outcome.dispatched)
                self.assertEqual(backend.queue_add_calls, 1)

    async def test_final_thread_read_must_keep_exact_workspace(self) -> None:
        backend = self.backend()
        other_workspace = self.root / "other-workspace"
        other_workspace.mkdir()

        class MovedFinalThreadAdapter(FakeCodexAdapter):
            async def read_thread(self, thread_id):
                value = await super().read_thread(thread_id)
                if self.backend.read_turn_calls == 0:
                    return value
                summary = ThreadSummary(
                    value.thread.thread_id,
                    other_workspace,
                    value.thread.preview,
                    value.thread.status,
                    value.thread.created_at,
                    value.thread.updated_at,
                    value.thread.turn_ids,
                )
                return ThreadCatalogReadback(summary, value.turns)

        outcome = await self.operator(
            backend, MovedFinalThreadAdapter
        ).continue_active(
            queue_contract(self.workspace, "queue-final-workspace-001"),
            thread_id=self.thread_id,
        )

        self.assertEqual(outcome.operation.state, OperationState.UNKNOWN)
        self.assertEqual(
            outcome.operation.failure_code, "queue_exact_turn_readback_unknown"
        )
        self.assertTrue(outcome.dispatched)
        self.assertEqual(backend.queue_add_calls, 1)

    async def test_target_terminal_notification_must_match_authoritative_status(
        self,
    ) -> None:
        cases: tuple[tuple[TurnTerminalStatus, TurnTerminalStatus], ...] = (
            ("failed", "completed"),
            ("completed", "failed"),
            ("interrupted", "failed"),
        )
        for notification, authoritative in cases:
            with self.subTest(
                notification=notification,
                authoritative=authoritative,
            ):
                journal = SQLiteJournal(
                    self.root
                    / f"target-terminal-{notification}-{authoritative}.sqlite3"
                )
                backend = self.backend(terminal_status=authoritative)
                contract = queue_contract(
                    self.workspace,
                    f"queue-target-terminal-{notification}-{authoritative}",
                )

                class ContradictoryTargetTerminalAdapter(FakeCodexAdapter):
                    async def wait_for_completion(
                        self, thread_id, turn_id, timeout
                    ) -> TurnTerminalStatus:
                        if turn_id == self.backend.turn_id:
                            self.backend.wait_turn_ids.append(turn_id)
                            return cast(TurnTerminalStatus, notification)
                        return await super().wait_for_completion(
                            thread_id, turn_id, timeout
                        )

                try:
                    outcome = await self.operator(
                        backend,
                        ContradictoryTargetTerminalAdapter,
                        journal=journal,
                    ).continue_active(contract, thread_id=self.thread_id)
                finally:
                    journal.close()

                self.assertEqual(outcome.operation.state, OperationState.UNKNOWN)
                self.assertEqual(
                    outcome.operation.failure_code,
                    "queue_exact_turn_readback_unknown",
                )
                self.assertTrue(outcome.dispatched)
                self.assertEqual(backend.read_turn_calls, 0)

    async def test_authoritatively_confirmed_target_failure_and_interrupt(
        self,
    ) -> None:
        cases: tuple[tuple[TurnTerminalStatus, OperationState], ...] = (
            ("failed", OperationState.FAILED),
            ("interrupted", OperationState.CANCELLED),
        )
        for terminal, expected_state in cases:
            with self.subTest(terminal=terminal):
                journal = SQLiteJournal(
                    self.root / f"target-confirmed-{terminal}.sqlite3"
                )
                backend = self.backend(terminal_status=terminal)
                contract = queue_contract(
                    self.workspace,
                    f"queue-target-confirmed-{terminal}",
                )
                try:
                    outcome = await self.operator(
                        backend, journal=journal
                    ).continue_active(contract, thread_id=self.thread_id)
                finally:
                    journal.close()

                self.assertEqual(outcome.operation.state, expected_state)
                self.assertEqual(
                    outcome.operation.failure_code,
                    "remote_turn_failed",
                )
                self.assertTrue(outcome.dispatched)
                self.assertGreaterEqual(backend.read_thread_calls, 1)
                self.assertEqual(backend.read_turn_calls, 0)

    async def test_owner_turn_wait_timeout_is_unknown_and_never_reenqueues(self) -> None:
        backend = self.backend(fail_at="wait_timeout")
        contract = queue_contract(self.workspace, "queue-owner-timeout-001")

        first = await self.operator(backend).continue_active(
            contract, thread_id=self.thread_id
        )
        repeat = await self.operator(backend).continue_active(
            contract, thread_id=self.thread_id
        )
        reopened = SQLiteJournal(self.database)
        try:
            restarted = await self.operator(
                backend, journal=reopened
            ).continue_active(contract, thread_id=self.thread_id)
        finally:
            reopened.close()

        self.assertEqual(first.operation.state, OperationState.UNKNOWN)
        self.assertEqual(
            first.operation.failure_code, "queue_owner_turn_completion_unknown"
        )
        self.assertTrue(first.dispatched)
        self.assertFalse(repeat.dispatched)
        self.assertFalse(restarted.dispatched)
        self.assertEqual(backend.queue_add_calls, 1)
        self.assertEqual(backend.queue_start_calls, 0)

    async def test_cancellation_after_enqueue_never_reenqueues(self) -> None:
        backend = self.backend()
        contract = queue_contract(self.workspace, "queue-owner-cancel-001")

        class CancellingOwnerWaitAdapter(FakeCodexAdapter):
            async def wait_for_completion(self, thread_id, turn_id, timeout):
                if turn_id == "turn-synthetic-active":
                    raise asyncio.CancelledError
                return await super().wait_for_completion(thread_id, turn_id, timeout)

        with self.assertRaises(asyncio.CancelledError):
            await self.operator(backend, CancellingOwnerWaitAdapter).continue_active(
                contract, thread_id=self.thread_id
            )
        repeat = await self.operator(backend).continue_active(
            contract, thread_id=self.thread_id
        )
        reopened = SQLiteJournal(self.database)
        try:
            restarted = await self.operator(
                backend, journal=reopened
            ).continue_active(contract, thread_id=self.thread_id)
        finally:
            reopened.close()

        self.assertEqual(
            self.journal.get_operation(contract.operation_id).state,
            OperationState.QUEUE_DISPATCH_IN_FLIGHT,
        )
        self.assertFalse(repeat.dispatched)
        self.assertFalse(restarted.dispatched)
        self.assertEqual(backend.queue_add_calls, 1)
        self.assertEqual(backend.queue_start_calls, 0)

    async def test_absent_or_ambiguous_loaded_owner_is_busy_unknown(self) -> None:
        for ids in ([], [self.thread_id, self.thread_id]):
            with self.subTest(ids=ids):
                database = self.root / f"owner-{len(ids)}.sqlite3"
                journal = SQLiteJournal(database)
                backend = self.backend()
                backend.loaded_thread_ids = ids
                if not ids:
                    backend.threads["other-thread"] = self.workspace
                    backend.loaded_thread_ids = ["other-thread"]
                try:
                    outcome = await self.operator(backend, journal=journal).continue_active(
                        queue_contract(self.workspace, f"queue-owner-{len(ids)}"),
                        thread_id=self.thread_id,
                    )
                finally:
                    journal.close()
                self.assertEqual(outcome.operation.state, OperationState.UNKNOWN)
                self.assertEqual(outcome.operation.failure_code, "BUSY_UNKNOWN")
                self.assertEqual(backend.queue_add_calls, 0)

    async def test_lost_or_malformed_enqueue_response_recovers_one_match_once(self) -> None:
        for mode in ("queue_add_delivery_unknown", "queue_add_malformed"):
            with self.subTest(mode=mode):
                journal = SQLiteJournal(self.root / f"{mode}.sqlite3")
                backend = self.backend(fail_at=mode)

                class RecoveringAdapter(FakeCodexAdapter):
                    async def add_queue(self, thread_id, prompt, client_message_id):
                        try:
                            return await super().add_queue(thread_id, prompt, client_message_id)
                        finally:
                            self.backend.fail_at = None

                try:
                    outcome = await self.operator(
                        backend, RecoveringAdapter, journal=journal
                    ).continue_active(
                        queue_contract(self.workspace, f"queue-{mode}"),
                        thread_id=self.thread_id,
                    )
                finally:
                    journal.close()
                self.assertEqual(outcome.operation.state, OperationState.VERIFIED)
                self.assertEqual(backend.queue_add_calls, 1)
                self.assertEqual(
                    backend.queue_list_calls, 4 + QUEUE_AUTOCONSUME_POLLS
                )
                self.assertEqual(backend.queue_start_calls, 1)

    async def test_malformed_start_response_recovers_one_unique_turn_once(self) -> None:
        backend = self.backend(fail_at="queue_start_malformed")
        contract = queue_contract(self.workspace, "queue-start-malformed")

        outcome = await self.operator(backend).continue_active(
            contract, thread_id=self.thread_id
        )

        self.assertEqual(outcome.operation.state, OperationState.VERIFIED)
        self.assertTrue(outcome.dispatched)
        self.assertEqual(backend.queue_add_calls, 1)
        self.assertEqual(backend.queue_start_calls, 1)
        self.assertEqual(backend.read_turn_calls, 2)

    async def test_lost_enqueue_zero_or_multiple_matches_is_unknown_without_reenqueue(self) -> None:
        for mode in ("zero", "multiple"):
            with self.subTest(mode=mode):
                journal = SQLiteJournal(self.root / f"recover-{mode}.sqlite3")
                backend = self.backend()

                class AmbiguousAddAdapter(FakeCodexAdapter):
                    async def add_queue(self, thread_id, prompt, client_message_id):
                        self.backend.queue_add_calls += 1
                        if mode == "multiple":
                            digest = QueueContinuationOperator._input_digest(prompt)
                            self.backend.queued[thread_id] = [
                                QueuedSubmission("submission-a", client_message_id, digest),
                                QueuedSubmission("submission-b", client_message_id, digest),
                            ]
                        raise DeliveryUnknown("thread/queue/add", 9, timeout_seconds=0.01)

                try:
                    outcome = await self.operator(
                        backend, AmbiguousAddAdapter, journal=journal
                    ).continue_active(
                        queue_contract(self.workspace, f"queue-recover-{mode}"),
                        thread_id=self.thread_id,
                    )
                finally:
                    journal.close()
                self.assertEqual(outcome.operation.state, OperationState.UNKNOWN)
                self.assertEqual(backend.queue_add_calls, 1)
                self.assertEqual(backend.queue_list_calls, 1)
                self.assertEqual(backend.queue_start_calls, 0)

    async def test_duplicate_submission_ids_in_recovery_fail_closed(self) -> None:
        backend = self.backend()

        class DuplicateSubmissionAdapter(FakeCodexAdapter):
            async def add_queue(self, thread_id, prompt, client_message_id):
                self.backend.queue_add_calls += 1
                digest = QueueContinuationOperator._input_digest(prompt)
                duplicate = QueuedSubmission("submission-duplicate", client_message_id, digest)
                self.backend.queued[thread_id] = [
                    duplicate,
                    QueuedSubmission("submission-duplicate", "client-other", digest),
                ]
                raise DeliveryUnknown("thread/queue/add", 10, timeout_seconds=0.01)

        outcome = await self.operator(backend, DuplicateSubmissionAdapter).continue_active(
            queue_contract(self.workspace), thread_id=self.thread_id
        )
        self.assertEqual(outcome.operation.state, OperationState.UNKNOWN)
        self.assertEqual(outcome.operation.failure_code, "queue_enqueue_unconfirmed")
        self.assertEqual(backend.queue_add_calls, 1)
        self.assertEqual(backend.queue_list_calls, 1)
        self.assertEqual(backend.queue_start_calls, 0)

    async def test_enqueue_identity_mismatch_without_durable_match_fails_closed(self) -> None:
        backend = self.backend()

        class MismatchedSubmissionAdapter(FakeCodexAdapter):
            async def add_queue(self, thread_id, prompt, client_message_id):
                self.backend.queue_add_calls += 1
                wrong = QueuedSubmission(
                    "submission-wrong",
                    "client-wrong",
                    QueueContinuationOperator._input_digest("Different input."),
                )
                self.backend.queued[thread_id] = [wrong]
                return wrong

        outcome = await self.operator(backend, MismatchedSubmissionAdapter).continue_active(
            queue_contract(self.workspace), thread_id=self.thread_id
        )
        self.assertEqual(outcome.operation.state, OperationState.UNKNOWN)
        self.assertEqual(
            outcome.operation.failure_code, "queue_enqueue_identity_conflict"
        )
        self.assertEqual(backend.queue_add_calls, 1)
        self.assertEqual(backend.queue_list_calls, 0)
        self.assertEqual(backend.queue_start_calls, 0)

    async def test_explicit_enqueue_identity_conflict_ignores_exact_durable_match(self) -> None:
        backend = self.backend()
        contract = queue_contract(self.workspace, "queue-explicit-add-conflict")

        class ContradictoryAddAdapter(FakeCodexAdapter):
            async def add_queue(self, thread_id, prompt, client_message_id):
                self.backend.queue_add_calls += 1
                digest = QueueContinuationOperator._input_digest(prompt)
                self.backend.queued[thread_id] = [
                    QueuedSubmission("submission-exact", client_message_id, digest)
                ]
                return QueuedSubmission(
                    "submission-response-conflict", "client-conflict", digest
                )

        first = await self.operator(backend, ContradictoryAddAdapter).continue_active(
            contract, thread_id=self.thread_id
        )
        repeat = await self.operator(backend, ContradictoryAddAdapter).continue_active(
            contract, thread_id=self.thread_id
        )
        reopened = SQLiteJournal(self.database)
        try:
            restarted = await self.operator(
                backend, ContradictoryAddAdapter, journal=reopened
            ).continue_active(contract, thread_id=self.thread_id)
        finally:
            reopened.close()

        self.assertEqual(first.operation.state, OperationState.UNKNOWN)
        self.assertEqual(
            first.operation.failure_code, "queue_enqueue_identity_conflict"
        )
        self.assertTrue(first.dispatched)
        self.assertFalse(repeat.dispatched)
        self.assertFalse(restarted.dispatched)
        self.assertEqual(backend.queue_add_calls, 1)
        self.assertEqual(backend.queue_list_calls, 0)
        self.assertEqual(backend.queue_start_calls, 0)
        self.assertEqual(backend.read_turn_calls, 0)

    async def test_explicit_start_identity_conflict_is_not_recovered_from_readback(self) -> None:
        for mode in ("thread", "turn"):
            with self.subTest(mode=mode):
                database = self.root / f"start-identity-{mode}.sqlite3"
                journal = SQLiteJournal(database)
                backend = self.backend()
                contract = queue_contract(
                    self.workspace, f"queue-explicit-start-{mode}-conflict"
                )

                class ContradictoryStartAdapter(FakeCodexAdapter):
                    async def start_queued_turn(self, thread_id, submission_id):
                        actual = await super().start_queued_turn(thread_id, submission_id)
                        if mode == "thread":
                            return TurnHandle("thread-response-conflict", actual.turn_id)
                        return TurnHandle(actual.thread_id, "turn-response-conflict")

                try:
                    first = await self.operator(
                        backend, ContradictoryStartAdapter, journal=journal
                    ).continue_active(contract, thread_id=self.thread_id)
                    repeat = await self.operator(
                        backend, ContradictoryStartAdapter, journal=journal
                    ).continue_active(contract, thread_id=self.thread_id)
                finally:
                    journal.close()
                reopened = SQLiteJournal(database)
                try:
                    restarted = await self.operator(
                        backend, ContradictoryStartAdapter, journal=reopened
                    ).continue_active(contract, thread_id=self.thread_id)
                finally:
                    reopened.close()

                self.assertEqual(first.operation.state, OperationState.UNKNOWN)
                self.assertEqual(
                    first.operation.failure_code, "queue_turn_identity_conflict"
                )
                self.assertTrue(first.dispatched)
                self.assertFalse(repeat.dispatched)
                self.assertFalse(restarted.dispatched)
                self.assertEqual(backend.queue_add_calls, 1)
                self.assertEqual(backend.queue_start_calls, 1)
                self.assertEqual(backend.read_turn_calls, 0)

    async def test_queue_disappearance_without_new_turn_is_not_success(self) -> None:
        backend = self.backend()

        class DisappearingAdapter(FakeCodexAdapter):
            async def start_queued_turn(self, thread_id, submission_id):
                self.backend.queue_start_calls += 1
                self.backend.queued[thread_id].clear()
                raise ProtocolIntegrityError("response malformed after queue disappeared")

        outcome = await self.operator(backend, DisappearingAdapter).continue_active(
            queue_contract(self.workspace), thread_id=self.thread_id
        )
        self.assertEqual(outcome.operation.state, OperationState.UNKNOWN)
        self.assertEqual(outcome.operation.failure_code, "queue_turn_correlation_unknown")

    async def test_generic_lifecycle_cannot_upgrade_ambiguous_queue_operation(
        self,
    ) -> None:
        backend = self.backend()
        contract = queue_contract(
            self.workspace,
            "queue-generic-recovery-bypass-001",
        )

        class UnrelatedStartedTurnAdapter(FakeCodexAdapter):
            async def start_queued_turn(self, thread_id, submission_id):
                self.backend.queue_start_calls += 1
                exact = self.backend.queued[thread_id][0]
                self.backend.queued[thread_id].remove(exact)
                self.backend.turns[(thread_id, "turn-unrelated")] = "completed"
                return TurnHandle(thread_id, "turn-unrelated")

        queue_outcome = await self.operator(
            backend, UnrelatedStartedTurnAdapter
        ).continue_active(contract, thread_id=self.thread_id)

        self.assertEqual(queue_outcome.operation.state, OperationState.UNKNOWN)
        self.assertEqual(
            queue_outcome.operation.failure_code,
            "queue_exact_turn_readback_unknown",
        )
        self.assertEqual(queue_outcome.operation.turn_id, "turn-unrelated")

        generic = Operator(
            self.journal,
            lambda: FakeCodexAdapter(backend),
            WorkspacePolicy((self.root,)),
        )
        collected = await generic.collect(contract)
        recovered = await generic.recover(contract)
        submitted = await generic.submit(contract)

        for outcome in (collected, recovered, submitted):
            self.assertEqual(outcome.operation.state, OperationState.UNKNOWN)
            self.assertEqual(
                outcome.operation.failure_code,
                "queue_exact_turn_readback_unknown",
            )
            self.assertIsNone(outcome.final_text)
            self.assertFalse(outcome.dispatched)
        self.assertEqual(backend.resume_thread_calls, 0)
        self.assertEqual(backend.read_turn_calls, 0)

    async def test_successful_queue_start_must_remove_exact_submission(self) -> None:
        backend = self.backend()

        class StickyQueueStartAdapter(FakeCodexAdapter):
            async def start_queued_turn(self, thread_id, submission_id):
                exact = self.backend.queued[thread_id][0]
                turn = await super().start_queued_turn(thread_id, submission_id)
                self.backend.queued[thread_id].append(exact)
                return turn

        outcome = await self.operator(
            backend, StickyQueueStartAdapter
        ).continue_active(
            queue_contract(self.workspace, "queue-sticky-start-001"),
            thread_id=self.thread_id,
        )

        self.assertEqual(outcome.operation.state, OperationState.UNKNOWN)
        self.assertEqual(
            outcome.operation.failure_code, "queue_turn_identity_conflict"
        )
        self.assertTrue(outcome.dispatched)
        self.assertEqual(backend.queue_add_calls, 1)
        self.assertEqual(backend.queue_start_calls, 1)
        self.assertEqual(len(backend.queued[self.thread_id]), 1)

    async def test_queue_start_reidentified_exact_submission_is_still_pending(
        self,
    ) -> None:
        backend = self.backend()

        class ReidentifiedStickyQueueAdapter(FakeCodexAdapter):
            async def start_queued_turn(self, thread_id, submission_id):
                exact = self.backend.queued[thread_id][0]
                turn = await super().start_queued_turn(thread_id, submission_id)
                self.backend.queued[thread_id].append(
                    QueuedSubmission(
                        "submission-reidentified",
                        exact.client_message_id,
                        exact.input_sha256,
                    )
                )
                return turn

        outcome = await self.operator(
            backend, ReidentifiedStickyQueueAdapter
        ).continue_active(
            queue_contract(self.workspace, "queue-reidentified-start-001"),
            thread_id=self.thread_id,
        )

        self.assertEqual(outcome.operation.state, OperationState.UNKNOWN)
        self.assertEqual(
            outcome.operation.failure_code, "queue_turn_identity_conflict"
        )
        self.assertTrue(outcome.dispatched)
        self.assertEqual(backend.queue_add_calls, 1)
        self.assertEqual(backend.queue_start_calls, 1)
        self.assertEqual(len(backend.queued[self.thread_id]), 1)

    async def test_same_client_id_with_different_input_is_identity_conflict(
        self,
    ) -> None:
        backend = self.backend()

        class ConflictingInputQueueAdapter(FakeCodexAdapter):
            async def add_queue(self, thread_id, prompt, client_message_id):
                exact = await super().add_queue(
                    thread_id, prompt, client_message_id
                )
                self.backend.queued[thread_id].append(
                    QueuedSubmission(
                        "submission-conflicting-input",
                        exact.client_message_id,
                        QueueContinuationOperator._input_digest(
                            "Conflicting queued input."
                        ),
                    )
                )
                return exact

        outcome = await self.operator(
            backend, ConflictingInputQueueAdapter
        ).continue_active(
            queue_contract(
                self.workspace,
                "queue-client-input-conflict-001",
            ),
            thread_id=self.thread_id,
        )

        self.assertEqual(outcome.operation.state, OperationState.UNKNOWN)
        self.assertEqual(
            outcome.operation.failure_code,
            "queue_turn_correlation_unknown",
        )
        self.assertTrue(outcome.dispatched)
        self.assertEqual(backend.queue_add_calls, 1)
        self.assertEqual(backend.queue_start_calls, 0)

    async def test_exact_submission_cannot_reappear_before_verification(self) -> None:
        backend = self.backend()

        class LateReappearingQueueAdapter(FakeCodexAdapter):
            async def wait_for_completion(self, thread_id, turn_id, timeout):
                terminal = await super().wait_for_completion(
                    thread_id, turn_id, timeout
                )
                if turn_id == self.backend.turn_id:
                    exact = self.backend.turn_submissions[(thread_id, turn_id)]
                    self.backend.queued[thread_id].append(exact)
                return terminal

        outcome = await self.operator(
            backend, LateReappearingQueueAdapter
        ).continue_active(
            queue_contract(self.workspace, "queue-late-reappearance-001"),
            thread_id=self.thread_id,
        )

        self.assertEqual(outcome.operation.state, OperationState.UNKNOWN)
        self.assertEqual(
            outcome.operation.failure_code, "queue_exact_turn_readback_unknown"
        )
        self.assertTrue(outcome.dispatched)
        self.assertEqual(backend.queue_add_calls, 1)
        self.assertEqual(backend.queue_start_calls, 1)
        self.assertEqual(len(backend.queued[self.thread_id]), 1)

    async def test_remote_evidence_is_rechecked_after_slow_verification(
        self,
    ) -> None:
        backend = self.backend()
        contract = WorkContract.from_dict(
            {
                "schema_version": "1",
                "task_id": "synthetic-active-task",
                "operation_id": "queue-verification-window-001",
                "workspace": str(self.workspace),
                "prompt": "Queue the exact synthetic active task once.",
                "allowed_change_globs": ["src/**"],
                "verification": [
                    [
                        sys.executable,
                        "-c",
                        "import time; time.sleep(0.2)",
                    ]
                ],
                "timeout_seconds": 5,
            }
        )

        class ReappearingDuringVerificationAdapter(FakeCodexAdapter):
            reappearance_scheduled = False

            async def read_turn(self, thread_id, turn_id):
                readback = await super().read_turn(thread_id, turn_id)
                if (
                    turn_id == self.backend.turn_id
                    and not self.reappearance_scheduled
                ):
                    self.reappearance_scheduled = True
                    exact = self.backend.turn_submissions[(thread_id, turn_id)]
                    asyncio.get_running_loop().call_later(
                        0.05,
                        self.backend.queued.setdefault(thread_id, []).append,
                        exact,
                    )
                return readback

        outcome = await self.operator(
            backend, ReappearingDuringVerificationAdapter
        ).continue_active(contract, thread_id=self.thread_id)

        self.assertEqual(outcome.operation.state, OperationState.UNKNOWN)
        self.assertEqual(
            outcome.operation.failure_code,
            "queue_exact_turn_readback_unknown",
        )
        self.assertTrue(outcome.dispatched)
        self.assertGreaterEqual(backend.read_turn_calls, 2)
        self.assertEqual(len(backend.queued[self.thread_id]), 1)

    async def test_zero_or_multiple_resulting_turns_is_unknown(self) -> None:
        for mode in ("zero", "multiple"):
            with self.subTest(mode=mode):
                journal = SQLiteJournal(self.root / f"turn-{mode}.sqlite3")
                backend = self.backend()

                class BadCorrelationAdapter(FakeCodexAdapter):
                    async def start_queued_turn(self, thread_id, submission_id):
                        turn = await super().start_queued_turn(thread_id, submission_id)
                        if mode == "zero":
                            self.backend.turns.pop((thread_id, self.backend.turn_id))
                        else:
                            self.backend.turns[(thread_id, "turn-extra")] = "completed"
                        return turn

                try:
                    outcome = await self.operator(
                        backend, BadCorrelationAdapter, journal=journal
                    ).continue_active(
                        queue_contract(self.workspace, f"queue-turn-{mode}"),
                        thread_id=self.thread_id,
                    )
                finally:
                    journal.close()
                self.assertEqual(outcome.operation.state, OperationState.UNKNOWN)
                self.assertEqual(backend.queue_add_calls, 1)

    async def test_duplicate_or_oversized_baseline_fails_before_enqueue(self) -> None:
        for mode in ("duplicate", "oversized"):
            with self.subTest(mode=mode):
                journal = SQLiteJournal(self.root / f"baseline-{mode}.sqlite3")
                backend = self.backend()

                class BadBaselineAdapter(FakeCodexAdapter):
                    async def read_thread(self, thread_id):
                        value = await super().read_thread(thread_id)
                        ids = (
                            (self.backend.turn_id, self.backend.turn_id)
                            if mode == "duplicate"
                            else tuple(f"turn-{index}" for index in range(1002))
                        )
                        summary = ThreadSummary(
                            value.thread.thread_id,
                            value.thread.cwd,
                            value.thread.preview,
                            "active",
                            value.thread.created_at,
                            value.thread.updated_at,
                            ids,
                        )
                        turns = tuple(
                            CatalogTurn(
                                turn_id,
                                "inProgress" if index == 0 else "completed",
                                None if index == 0 else "Synthetic completed result.",
                            )
                            for index, turn_id in enumerate(ids)
                        )
                        return ThreadCatalogReadback(summary, turns)

                try:
                    outcome = await self.operator(
                        backend, BadBaselineAdapter, journal=journal
                    ).continue_active(
                        queue_contract(self.workspace, f"queue-baseline-{mode}"),
                        thread_id=self.thread_id,
                    )
                finally:
                    journal.close()
                self.assertEqual(outcome.operation.state, OperationState.UNKNOWN)
                self.assertEqual(backend.queue_add_calls, 0)

    async def test_two_process_guard_has_one_enqueue_owner(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        backend = self.backend()

        class BlockingAddAdapter(FakeCodexAdapter):
            async def add_queue(self, thread_id, prompt, client_message_id):
                started.set()
                await release.wait()
                return await super().add_queue(thread_id, prompt, client_message_id)

        second_journal = SQLiteJournal(self.database)
        contract = queue_contract(self.workspace)
        try:
            first_task = asyncio.create_task(
                self.operator(backend, BlockingAddAdapter).continue_active(
                    contract, thread_id=self.thread_id
                )
            )
            await asyncio.wait_for(started.wait(), timeout=1)
            duplicate = await self.operator(
                backend, BlockingAddAdapter, journal=second_journal
            ).continue_active(contract, thread_id=self.thread_id)
            self.assertFalse(duplicate.dispatched)
            release.set()
            first = await first_task
            self.assertEqual(first.operation.state, OperationState.VERIFIED)
            self.assertEqual(backend.queue_add_calls, 1)
        finally:
            release.set()
            second_journal.close()

    def test_unsupported_versions_fail_before_adapter_or_side_effect(self) -> None:
        backend = self.backend()
        for version in ("0.148.0", "0.150.0", "v2", ""):
            with self.subTest(version=version):
                with self.assertRaises(UnsupportedCapability):
                    QueueContinuationOperator(
                        self.journal,
                        lambda: FakeCodexAdapter(backend),
                        WorkspacePolicy((self.root,)),
                        experimental_api_version=version,
                    )
        self.assertEqual(backend.queue_add_calls, 0)


if __name__ == "__main__":
    unittest.main()
