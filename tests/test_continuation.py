from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import tempfile
import unittest

from hermes_codex_operator.adapters.base import (
    DeliveryUnknown,
    ProtocolIntegrityError,
    ThreadCatalogReadback,
    ThreadSummary,
)
from hermes_codex_operator.adapters.fake import FakeBackend, FakeCodexAdapter
from hermes_codex_operator.continuation import ContinuationOperator, MAX_BASELINE_TURNS
from hermes_codex_operator.journal import SQLiteJournal
from hermes_codex_operator.models import OperationState, WorkContract
from hermes_codex_operator.workspace import WorkspacePolicy


def continuation_contract(workspace: Path) -> WorkContract:
    return WorkContract.from_dict(
        {
            "schema_version": "1",
            "task_id": "synthetic-existing-task",
            "operation_id": "synthetic-continuation-001",
            "workspace": str(workspace),
            "prompt": "Continue the exact synthetic idle task once.",
            "allowed_change_globs": ["src/**"],
            "verification": [[sys.executable, "-c", "raise SystemExit(0)"]],
            "timeout_seconds": 5,
        }
    )


class ContinuationOperatorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        (self.workspace / "src").mkdir(parents=True)
        (self.workspace / "src" / "app.py").write_text("OLD = True\n", encoding="utf-8")
        self.database = self.root / "state.sqlite3"
        self.journal = SQLiteJournal(self.database)
        self.thread_id = "thread-synthetic-existing"
        self.old_turn = "turn-synthetic-old"
        self.new_turn = "turn-synthetic-new"

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
        backend.turns[(self.thread_id, self.old_turn)] = "completed"
        return backend

    def operator(self, backend: FakeBackend, adapter=FakeCodexAdapter, journal=None):
        return ContinuationOperator(
            journal or self.journal,
            lambda: adapter(backend),
            WorkspacePolicy((self.root,)),
        )

    async def test_happy_idle_continuation_binds_exact_new_turn(self) -> None:
        def mutate(workspace: Path) -> None:
            (workspace / "src" / "app.py").write_text("NEW = True\n", encoding="utf-8")

        backend = self.backend(mutate_workspace=mutate)
        contract = continuation_contract(self.workspace)
        outcome = await self.operator(backend).continue_idle(
            contract, thread_id=self.thread_id
        )

        self.assertEqual(outcome.operation.state, OperationState.VERIFIED)
        self.assertTrue(outcome.dispatched)
        self.assertEqual(outcome.operation.turn_id, self.new_turn)
        self.assertEqual(backend.resume_thread_calls, 1)
        self.assertEqual(backend.start_turn_calls, 1)
        baseline = self.journal.get_continuation_baseline(contract.operation_id)
        self.assertEqual(baseline.thread_id, self.thread_id)
        self.assertEqual(baseline.turn_ids, (self.old_turn,))

    async def test_repeat_and_restart_never_dispatch_again(self) -> None:
        backend = self.backend()
        contract = continuation_contract(self.workspace)
        first = await self.operator(backend).continue_idle(contract, thread_id=self.thread_id)
        self.assertEqual(first.operation.state, OperationState.VERIFIED)

        reopened = SQLiteJournal(self.database)
        try:
            duplicate = await self.operator(backend, journal=reopened).continue_idle(
                contract, thread_id=self.thread_id
            )
        finally:
            reopened.close()
        self.assertFalse(duplicate.dispatched)
        self.assertEqual(backend.resume_thread_calls, 1)
        self.assertEqual(backend.start_turn_calls, 1)

    async def test_workspace_mismatch_blocks_before_resume_or_start(self) -> None:
        backend = self.backend()
        backend.threads[self.thread_id] = self.root
        outcome = await self.operator(backend).continue_idle(
            continuation_contract(self.workspace), thread_id=self.thread_id
        )
        self.assertEqual(outcome.operation.state, OperationState.FAILED)
        self.assertEqual(outcome.operation.failure_code, "continuation_identity_unconfirmed")
        self.assertEqual(backend.resume_thread_calls, 0)
        self.assertEqual(backend.start_turn_calls, 0)

    async def test_non_idle_thread_blocks_before_dispatch(self) -> None:
        backend = self.backend()

        class BusyAdapter(FakeCodexAdapter):
            async def read_thread(self, thread_id):
                value = await super().read_thread(thread_id)
                summary = ThreadSummary(
                    value.thread.thread_id,
                    value.thread.cwd,
                    value.thread.preview,
                    "active",
                    value.thread.created_at,
                    value.thread.updated_at,
                    value.thread.turn_ids,
                )
                return ThreadCatalogReadback(summary, value.turns)

        outcome = await self.operator(backend, BusyAdapter).continue_idle(
            continuation_contract(self.workspace), thread_id=self.thread_id
        )
        self.assertEqual(outcome.operation.state, OperationState.FAILED)
        self.assertEqual(backend.resume_thread_calls, 0)
        self.assertEqual(backend.start_turn_calls, 0)

    async def test_duplicate_and_oversized_baseline_fail_closed(self) -> None:
        for mode in ("duplicate", "oversized"):
            with self.subTest(mode=mode):
                database = self.root / f"{mode}.sqlite3"
                journal = SQLiteJournal(database)
                backend = self.backend()

                class BadBaselineAdapter(FakeCodexAdapter):
                    async def read_thread(self, thread_id):
                        value = await super().read_thread(thread_id)
                        ids = (
                            (self.backend.turn_id, self.backend.turn_id)
                            if mode == "duplicate"
                            else tuple(f"turn-{index}" for index in range(MAX_BASELINE_TURNS + 1))
                        )
                        summary = ThreadSummary(
                            value.thread.thread_id,
                            value.thread.cwd,
                            value.thread.preview,
                            "idle",
                            value.thread.created_at,
                            value.thread.updated_at,
                            ids,
                        )
                        turns = tuple(value.turns[0] for _ in ids)
                        return ThreadCatalogReadback(summary, turns)

                try:
                    outcome = await self.operator(
                        backend, BadBaselineAdapter, journal=journal
                    ).continue_idle(
                        continuation_contract(self.workspace), thread_id=self.thread_id
                    )
                finally:
                    journal.close()
                self.assertEqual(outcome.operation.state, OperationState.FAILED)
                self.assertEqual(backend.start_turn_calls, 0)

    async def test_lost_resume_becomes_unknown_and_never_retries(self) -> None:
        backend = self.backend()

        class LostResumeAdapter(FakeCodexAdapter):
            async def resume_thread(self, thread_id, workspace, options=None):
                self.backend.resume_thread_calls += 1
                raise DeliveryUnknown("thread/resume", 7, timeout_seconds=0.01)

        contract = continuation_contract(self.workspace)
        operator = self.operator(backend, LostResumeAdapter)
        first = await operator.continue_idle(contract, thread_id=self.thread_id)
        second = await operator.continue_idle(contract, thread_id=self.thread_id)
        self.assertEqual(first.operation.state, OperationState.UNKNOWN)
        self.assertTrue(first.dispatched)
        self.assertFalse(second.dispatched)
        self.assertEqual(backend.resume_thread_calls, 1)
        self.assertEqual(backend.start_turn_calls, 0)

    async def test_lost_or_malformed_turn_start_is_unknown_without_retry(self) -> None:
        for mode in ("lost", "malformed"):
            with self.subTest(mode=mode):
                database = self.root / f"turn-{mode}.sqlite3"
                journal = SQLiteJournal(database)
                backend = self.backend(
                    fail_at="start_turn_delivery_unknown" if mode == "lost" else None
                )

                class MalformedAdapter(FakeCodexAdapter):
                    async def start_turn(self, thread_id, prompt, options=None):
                        await super().start_turn(thread_id, prompt, options)
                        raise ProtocolIntegrityError("synthetic malformed turn/start")

                adapter = MalformedAdapter if mode == "malformed" else FakeCodexAdapter
                contract = continuation_contract(self.workspace)
                operator = self.operator(backend, adapter, journal=journal)
                try:
                    first = await operator.continue_idle(contract, thread_id=self.thread_id)
                    second = await operator.continue_idle(contract, thread_id=self.thread_id)
                finally:
                    journal.close()
                self.assertEqual(first.operation.state, OperationState.UNKNOWN)
                self.assertFalse(second.dispatched)
                self.assertEqual(backend.start_turn_calls, 1)

    async def test_returned_turn_binding_conflict_is_unknown_without_retry(self) -> None:
        collision_turn = "turn-synthetic-collision"

        class ConflictingJournal(SQLiteJournal):
            def bind_turn(self, operation_id, turn_id):
                super().bind_turn(operation_id, collision_turn)
                return super().bind_turn(operation_id, turn_id)

        self.journal.close()
        self.journal = ConflictingJournal(self.database)
        backend = self.backend()
        contract = continuation_contract(self.workspace)

        first = await self.operator(backend).continue_idle(
            contract, thread_id=self.thread_id
        )
        duplicate = await self.operator(backend).continue_idle(
            contract, thread_id=self.thread_id
        )
        reopened = SQLiteJournal(self.database)
        try:
            restarted = await self.operator(backend, journal=reopened).continue_idle(
                contract, thread_id=self.thread_id
            )
        finally:
            reopened.close()

        self.assertEqual(first.operation.state, OperationState.UNKNOWN)
        self.assertEqual(
            first.operation.failure_code, "continuation_turn_binding_conflict"
        )
        self.assertTrue(first.dispatched)
        self.assertFalse(duplicate.dispatched)
        self.assertFalse(restarted.dispatched)
        self.assertEqual(backend.resume_thread_calls, 1)
        self.assertEqual(backend.start_turn_calls, 1)

    async def test_unexpected_multiple_new_turns_cannot_be_accepted(self) -> None:
        backend = self.backend()

        class AmbiguousCorrelationAdapter(FakeCodexAdapter):
            async def start_turn(self, thread_id, prompt, options=None):
                turn = await super().start_turn(thread_id, prompt, options)
                self.backend.turns[(thread_id, "turn-synthetic-unexpected")] = "completed"
                return turn

        outcome = await self.operator(backend, AmbiguousCorrelationAdapter).continue_idle(
            continuation_contract(self.workspace), thread_id=self.thread_id
        )
        self.assertEqual(outcome.operation.state, OperationState.UNKNOWN)
        self.assertEqual(outcome.operation.turn_id, self.new_turn)

    async def test_missing_final_readback_fails_closed(self) -> None:
        backend = self.backend(fail_at="readback_missing")
        outcome = await self.operator(backend).continue_idle(
            continuation_contract(self.workspace), thread_id=self.thread_id
        )
        self.assertEqual(outcome.operation.state, OperationState.FAILED)
        self.assertEqual(outcome.operation.failure_code, "exact_turn_readback_failed")

    async def test_two_process_guard_has_one_dispatch_owner(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        backend = self.backend()

        class BlockingResumeAdapter(FakeCodexAdapter):
            async def resume_thread(self, thread_id, workspace, options=None):
                started.set()
                await release.wait()
                return await super().resume_thread(thread_id, workspace, options)

        second_journal = SQLiteJournal(self.database)
        contract = continuation_contract(self.workspace)
        try:
            first_task = asyncio.create_task(
                self.operator(backend, BlockingResumeAdapter).continue_idle(
                    contract, thread_id=self.thread_id
                )
            )
            await asyncio.wait_for(started.wait(), timeout=1)
            duplicate = await self.operator(backend, journal=second_journal).continue_idle(
                contract, thread_id=self.thread_id
            )
            self.assertFalse(duplicate.dispatched)
            release.set()
            first = await first_task
            self.assertEqual(first.operation.state, OperationState.VERIFIED)
            self.assertEqual(backend.start_turn_calls, 1)
        finally:
            release.set()
            second_journal.close()


if __name__ == "__main__":
    unittest.main()
