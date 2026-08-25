from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from hermes_codex_operator.adapters.fake import FakeBackend, FakeCodexAdapter
from hermes_codex_operator.adapters.base import ProtocolIntegrityError
from hermes_codex_operator.engine import Operator
from hermes_codex_operator.journal import SQLiteJournal, TaskBusy
from hermes_codex_operator.models import OperationState, WorkContract, sha256_text
from hermes_codex_operator.workspace import WorkspacePolicy, snapshot_workspace


def contract_for(
    workspace: Path,
    *,
    task_id: str = "acme-widget",
    operation_id: str = "acme-widget-001",
    allowed: list[str] | None = None,
    verification: list[list[str]] | None = None,
) -> WorkContract:
    return WorkContract.from_dict(
        {
            "schema_version": "1",
            "task_id": task_id,
            "operation_id": operation_id,
            "workspace": str(workspace),
            "prompt": "Perform the synthetic engineering task.",
            "allowed_change_globs": ["src/**"] if allowed is None else allowed,
            "verification": (
                [[sys.executable, "-c", "raise SystemExit(0)"]]
                if verification is None
                else verification
            ),
            "timeout_seconds": 5,
        }
    )


class OperatorEngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "acme-widget"
        (self.workspace / "src").mkdir(parents=True)
        (self.workspace / "src" / "app.py").write_text("OLD = True\n", encoding="utf-8")
        self.journal = SQLiteJournal(self.root / "state" / "journal.sqlite3")

    def tearDown(self) -> None:
        self.journal.close()
        self.temp.cleanup()

    def operator(self, backend: FakeBackend) -> Operator:
        return Operator(
            self.journal,
            lambda: FakeCodexAdapter(backend),
            WorkspacePolicy((self.root,)),
        )

    async def test_verified_happy_path_and_duplicate_never_dispatches_twice(self) -> None:
        def mutate(workspace: Path) -> None:
            (workspace / "src" / "app.py").write_text("NEW = True\n", encoding="utf-8")

        backend = FakeBackend(mutate_workspace=mutate)
        contract = contract_for(self.workspace)
        first = await self.operator(backend).submit(contract)
        self.assertEqual(first.operation.state, OperationState.VERIFIED)
        self.assertEqual(first.final_text, "Synthetic Codex result.")
        self.assertEqual(backend.start_turn_calls, 1)
        self.assertEqual(first.receipt.changed_paths[0].path, "src/app.py")
        self.assertEqual(
            first.receipt.result_sha256,
            sha256_text("Synthetic Codex result."),
        )
        self.assertNotIn(contract.prompt, first.receipt.to_json())

        duplicate = await self.operator(backend).submit(contract)
        self.assertEqual(duplicate.operation.state, OperationState.VERIFIED)
        self.assertEqual(backend.start_turn_calls, 1)
        self.assertFalse(duplicate.dispatched)
        collected = await self.operator(backend).collect(contract)
        self.assertEqual(collected.final_text, "Synthetic Codex result.")
        self.assertFalse(collected.dispatched)

    async def test_collect_rejects_changed_authoritative_result(self) -> None:
        backend = FakeBackend()
        contract = contract_for(self.workspace)
        first = await self.operator(backend).submit(contract)
        self.assertEqual(first.operation.state, OperationState.VERIFIED)
        backend.final_text = "Different synthetic result."
        with self.assertRaises(ProtocolIntegrityError):
            await self.operator(backend).collect(contract)

    async def test_continuation_reuses_thread_with_new_operation(self) -> None:
        first_backend = FakeBackend(turn_id="turn-synthetic-001")
        first = contract_for(self.workspace)
        await self.operator(first_backend).submit(first)

        second_backend = FakeBackend(
            thread_id=first_backend.thread_id,
            turn_id="turn-synthetic-002",
        )
        second = contract_for(self.workspace, operation_id="acme-widget-002")
        result = await self.operator(second_backend).submit(second)
        self.assertEqual(result.operation.state, OperationState.VERIFIED)
        self.assertEqual(second_backend.start_thread_calls, 0)
        self.assertEqual(second_backend.resume_thread_calls, 1)
        self.assertEqual(self.journal.get_task(first.task_id).thread_id, first_backend.thread_id)

    async def test_ambiguous_turn_start_is_unknown_and_never_retried(self) -> None:
        backend = FakeBackend(fail_at="start_turn_delivery_unknown")
        contract = contract_for(self.workspace)
        first = await self.operator(backend).submit(contract)
        self.assertEqual(first.operation.state, OperationState.UNKNOWN)
        self.assertEqual(backend.start_turn_calls, 1)
        second = await self.operator(backend).submit(contract)
        self.assertEqual(second.operation.state, OperationState.UNKNOWN)
        self.assertEqual(backend.start_turn_calls, 1)

    async def test_malformed_successful_turn_start_is_unknown_and_never_retried(self) -> None:
        class MalformedTurnStartAdapter(FakeCodexAdapter):
            async def start_turn(self, thread_id, prompt, options=None):
                await super().start_turn(thread_id, prompt, options)
                raise ProtocolIntegrityError("synthetic turn/start response lacks turn.id")

        backend = FakeBackend()
        contract = contract_for(self.workspace)
        operator = Operator(
            self.journal,
            lambda: MalformedTurnStartAdapter(backend),
            WorkspacePolicy((self.root,)),
        )
        first = await operator.submit(contract)
        self.assertEqual(first.operation.state, OperationState.UNKNOWN)
        self.assertEqual(first.operation.failure_code, "authoritative_turn_state_unknown")
        second = await operator.submit(contract)
        self.assertEqual(second.operation.state, OperationState.UNKNOWN)
        self.assertEqual(backend.start_turn_calls, 1)

    async def test_verification_failure_is_not_verified(self) -> None:
        backend = FakeBackend()
        contract = contract_for(
            self.workspace,
            verification=[[sys.executable, "-c", "raise SystemExit(9)"]],
        )
        result = await self.operator(backend).submit(contract)
        self.assertEqual(result.operation.state, OperationState.VERIFICATION_FAILED)
        self.assertEqual(result.receipt.verification[0].exit_code, 9)

    async def test_scope_violation_is_not_verified(self) -> None:
        def mutate(workspace: Path) -> None:
            (workspace / "outside.txt").write_text("synthetic\n", encoding="utf-8")

        backend = FakeBackend(mutate_workspace=mutate)
        result = await self.operator(backend).submit(contract_for(self.workspace))
        self.assertEqual(result.operation.state, OperationState.SCOPE_VIOLATION)
        self.assertEqual(result.receipt.changed_paths[0].path, "outside.txt")

    async def test_wait_timeout_stays_bound_and_blocks_retry(self) -> None:
        backend = FakeBackend(fail_at="wait_timeout")
        contract = contract_for(self.workspace)
        result = await self.operator(backend).submit(contract)
        self.assertEqual(result.operation.state, OperationState.TIMED_OUT)
        self.assertIsNotNone(result.operation.turn_id)
        again = await self.operator(backend).submit(contract)
        self.assertEqual(again.operation.state, OperationState.TIMED_OUT)
        self.assertEqual(backend.start_turn_calls, 1)
        recovered = await self.operator(backend).recover(contract)
        self.assertEqual(recovered.operation.state, OperationState.CANCELLED)
        self.assertIsNotNone(recovered.receipt)

    async def test_concurrent_duplicate_has_exactly_one_dispatch_owner(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        class BlockingAdapter(FakeCodexAdapter):
            async def start_turn(self, thread_id, prompt, options=None):
                started.set()
                await release.wait()
                return await super().start_turn(thread_id, prompt, options)

        backend = FakeBackend()
        contract = contract_for(self.workspace)
        second_journal = SQLiteJournal(self.journal.path)
        try:
            first_operator = Operator(
                self.journal,
                lambda: BlockingAdapter(backend),
                WorkspacePolicy((self.root,)),
            )
            second_operator = Operator(
                second_journal,
                lambda: FakeCodexAdapter(backend),
                WorkspacePolicy((self.root,)),
            )
            first_task = asyncio.create_task(first_operator.submit(contract))
            await asyncio.wait_for(started.wait(), timeout=1)
            duplicate = await second_operator.submit(contract)
            self.assertFalse(duplicate.dispatched)
            release.set()
            first = await first_task
            self.assertEqual(first.operation.state, OperationState.VERIFIED)
            self.assertEqual(backend.start_turn_calls, 1)
        finally:
            release.set()
            second_journal.close()

    async def test_transport_loss_after_bound_turn_is_unknown_and_blocks_followup(self) -> None:
        backend = FakeBackend(fail_at="wait_transport_lost")
        contract = contract_for(self.workspace)
        result = await self.operator(backend).submit(contract)
        self.assertEqual(result.operation.state, OperationState.UNKNOWN)
        self.assertIsNotNone(result.operation.turn_id)
        with self.assertRaises(TaskBusy):
            await self.operator(backend).submit(
                contract_for(self.workspace, operation_id="acme-widget-002")
            )
        self.assertEqual(backend.start_turn_calls, 1)

    async def test_verification_side_effect_is_included_in_final_scope_check(self) -> None:
        contract = contract_for(
            self.workspace,
            verification=[
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; Path('outside.txt').write_text('synthetic')",
                ]
            ],
        )
        result = await self.operator(FakeBackend()).submit(contract)
        self.assertEqual(result.operation.state, OperationState.SCOPE_VIOLATION)
        self.assertEqual(result.receipt.changed_paths[0].path, "outside.txt")

    async def test_verification_cancellation_kills_child_and_finalizes_state(self) -> None:
        contract = contract_for(
            self.workspace,
            allowed=["src/**"],
            verification=[
                [
                    sys.executable,
                    "-c",
                    "import time; from pathlib import Path; time.sleep(2); Path('late.txt').write_text('late')",
                ]
            ],
        )
        task = asyncio.create_task(self.operator(FakeBackend()).submit(contract))
        for _ in range(100):
            try:
                state = self.journal.get_operation(contract.operation_id).state
            except Exception:
                state = None
            if state == OperationState.VERIFYING:
                break
            await asyncio.sleep(0.01)
        else:
            self.fail("operation never entered VERIFYING")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.05)
        self.assertFalse((self.workspace / "late.txt").exists())
        self.assertEqual(
            self.journal.get_operation(contract.operation_id).state,
            OperationState.CANCELLED,
        )

    async def test_cancellation_during_final_snapshot_finalizes_state(self) -> None:
        contract = contract_for(self.workspace)
        entered = threading.Event()
        release = threading.Event()
        calls = 0
        calls_lock = threading.Lock()
        real_snapshot = snapshot_workspace

        def blocking_snapshot(*args, **kwargs):
            nonlocal calls
            with calls_lock:
                calls += 1
                current = calls
            if current == 3:
                entered.set()
                if not release.wait(timeout=2):
                    raise RuntimeError("test snapshot release timed out")
            return real_snapshot(*args, **kwargs)

        with patch("hermes_codex_operator.engine.snapshot_workspace", blocking_snapshot):
            task = asyncio.create_task(self.operator(FakeBackend()).submit(contract))
            reached = await asyncio.to_thread(entered.wait, 1)
            self.assertTrue(reached, "final snapshot was not reached")
            task.cancel()
            await asyncio.sleep(0)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task

        operation = self.journal.get_operation(contract.operation_id)
        self.assertEqual(operation.state, OperationState.CANCELLED)
        self.assertEqual(
            self.journal.get_receipt(contract.operation_id).status,
            OperationState.CANCELLED,
        )

    async def test_recover_running_turn_keeps_running_without_corrupting_state(self) -> None:
        contract = contract_for(self.workspace)
        backend = FakeBackend()
        backend.threads[backend.thread_id] = self.workspace
        backend.turns[(backend.thread_id, backend.turn_id)] = "inProgress"  # type: ignore[assignment]
        self.journal.reserve(contract)
        self.journal.store_baseline(
            contract.operation_id,
            snapshot_workspace(self.workspace, WorkspacePolicy((self.root,))),
        )
        self.journal.bind_thread(contract.task_id, backend.thread_id)
        self.journal.transition(contract.operation_id, OperationState.READY)
        self.journal.transition(contract.operation_id, OperationState.DISPATCHING)
        self.journal.bind_turn(contract.operation_id, backend.turn_id)
        self.journal.transition(contract.operation_id, OperationState.RUNNING)
        result = await self.operator(backend).recover(contract)
        self.assertEqual(result.operation.state, OperationState.RUNNING)
        self.assertEqual(backend.start_turn_calls, 0)

    async def test_restart_uses_persisted_thread_and_turn_identities(self) -> None:
        first = contract_for(self.workspace)
        first_backend = FakeBackend()
        self.journal.reserve(first)
        self.journal.bind_thread(first.task_id, first_backend.thread_id)
        self.journal.transition(first.operation_id, OperationState.STARTING_THREAD)
        first_result = await self.operator(first_backend).submit(first)
        self.assertEqual(first_result.operation.state, OperationState.VERIFIED)
        self.assertEqual(first_backend.start_thread_calls, 0)
        self.assertEqual(first_backend.start_turn_calls, 1)

        second = contract_for(self.workspace, operation_id="acme-widget-002")
        second_backend = FakeBackend(
            thread_id=first_backend.thread_id,
            turn_id="turn-synthetic-002",
        )
        second_backend.threads[second_backend.thread_id] = self.workspace
        second_backend.turns[
            (second_backend.thread_id, second_backend.turn_id)
        ] = "completed"
        self.journal.reserve(second)
        self.journal.store_baseline(
            second.operation_id,
            snapshot_workspace(self.workspace, WorkspacePolicy((self.root,))),
        )
        self.journal.transition(second.operation_id, OperationState.READY)
        self.journal.transition(second.operation_id, OperationState.DISPATCHING)
        self.journal.bind_turn(second.operation_id, second_backend.turn_id)
        second_result = await self.operator(second_backend).submit(second)
        self.assertEqual(second_result.operation.state, OperationState.VERIFIED)
        self.assertEqual(second_backend.start_turn_calls, 0)


if __name__ == "__main__":
    unittest.main()
