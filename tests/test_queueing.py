from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import tempfile
import unittest

from hermes_codex_operator.adapters.app_server import EXPERIMENTAL_QUEUE_API_VERSION
from hermes_codex_operator.adapters.base import (
    CatalogTurn,
    DeliveryUnknown,
    ProtocolIntegrityError,
    QueuedSubmission,
    ThreadCatalogReadback,
    ThreadSummary,
    UnsupportedCapability,
)
from hermes_codex_operator.adapters.fake import FakeBackend, FakeCodexAdapter
from hermes_codex_operator.journal import SQLiteJournal
from hermes_codex_operator.models import OperationState, WorkContract
from hermes_codex_operator.queueing import QueueContinuationOperator
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
                self.assertEqual(backend.queue_list_calls, 1)
                self.assertEqual(backend.queue_start_calls, 1)

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
        self.assertEqual(outcome.operation.failure_code, "queue_enqueue_unconfirmed")
        self.assertEqual(backend.queue_add_calls, 1)
        self.assertEqual(backend.queue_list_calls, 1)
        self.assertEqual(backend.queue_start_calls, 0)

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
