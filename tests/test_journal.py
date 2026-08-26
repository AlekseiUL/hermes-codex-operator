from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import threading
import unittest

from hermes_codex_operator.journal import (
    BaselineConflict,
    BindingConflict,
    IdempotencyConflict,
    InvalidTransition,
    ReceiptConflict,
    RecordNotFound,
    SQLiteJournal,
    TaskBusy,
)
from hermes_codex_operator.models import OperationState, Receipt, WorkContract, sha256_text
from hermes_codex_operator.workspace import WorkspacePolicy, snapshot_workspace


def make_contract(
    workspace: Path,
    *,
    task_id: str = "synthetic-task",
    operation_id: str = "synthetic-task-001",
    prompt: str = "Perform the synthetic change.",
    timeout_seconds: int = 60,
) -> WorkContract:
    return WorkContract.from_dict(
        {
            "schema_version": "1",
            "task_id": task_id,
            "operation_id": operation_id,
            "workspace": str(workspace),
            "prompt": prompt,
            "allowed_change_globs": ["src/**", "tests/**"],
            "verification": [["python3", "-m", "unittest"]],
            "timeout_seconds": timeout_seconds,
        }
    )


def move_to_verified(journal: SQLiteJournal, contract: WorkContract) -> None:
    journal.transition(contract.operation_id, OperationState.STARTING_THREAD)
    journal.bind_thread(contract.task_id, "thread-synthetic-001")
    journal.transition(contract.operation_id, OperationState.READY)
    journal.transition(contract.operation_id, OperationState.DISPATCHING)
    journal.bind_turn(contract.operation_id, "turn-synthetic-001")
    journal.transition(contract.operation_id, OperationState.RUNNING)
    journal.transition(contract.operation_id, OperationState.TURN_COMPLETED)
    journal.transition(contract.operation_id, OperationState.VERIFYING)
    journal.finalize_operation(contract.operation_id, make_receipt(contract))


def make_receipt(
    contract: WorkContract,
    *,
    changed_digest: str | None = None,
    status: OperationState = OperationState.VERIFIED,
    failure_code: str | None = None,
    thread_id: str | None = "thread-synthetic-001",
    turn_id: str | None = "turn-synthetic-001",
    include_verification: bool = True,
) -> Receipt:
    digest = changed_digest or sha256_text("synthetic file")
    return Receipt.from_dict(
        {
            "schema_version": "1",
            "task_id": contract.task_id,
            "operation_id": contract.operation_id,
            "status": status.value,
            "prompt_sha256": contract.prompt_sha256,
            "workspace_sha256": sha256_text(contract.workspace),
            "thread_sha256": sha256_text(thread_id) if thread_id else None,
            "turn_sha256": sha256_text(turn_id) if turn_id else None,
            "result_sha256": (
                sha256_text("synthetic result")
                if status == OperationState.VERIFIED
                else None
            ),
            "changed_paths": [{"path": "src/example.py", "sha256": digest}],
            "verification": (
                [
                    {
                        "command_sha256": sha256_text("synthetic argv"),
                        "exit_code": 0,
                        "duration_ms": 10,
                        "output_sha256": sha256_text("synthetic output"),
                    }
                ]
                if include_verification
                else []
            ),
            "started_at": "2026-01-01T00:00:00Z",
            "completed_at": "2026-01-01T00:00:01Z",
            "failure_code": failure_code,
        }
    )


class JournalReservationTests(unittest.TestCase):
    def test_v2_journal_migrates_to_v5_without_losing_operations(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            workspace = root_path / "workspace"
            workspace.mkdir()
            database = root_path / "journal.sqlite3"
            contract = make_contract(workspace)
            with SQLiteJournal(database) as journal:
                journal.reserve(contract)
            with closing(sqlite3.connect(database)) as legacy:
                legacy.execute("DROP TRIGGER operations_dispatch_kind_is_immutable")
                legacy.execute("ALTER TABLE operations DROP COLUMN dispatch_kind")
                legacy.execute("DROP TRIGGER continuation_baselines_are_immutable_on_update")
                legacy.execute("DROP TRIGGER continuation_baselines_are_immutable_on_delete")
                legacy.execute("DROP TABLE continuation_baselines")
                legacy.execute("PRAGMA user_version = 2")
                legacy.commit()
            with SQLiteJournal(database) as migrated:
                self.assertEqual(migrated.get_operation(contract.operation_id).operation_id, contract.operation_id)
                with closing(sqlite3.connect(database)) as check:
                    self.assertEqual(check.execute("PRAGMA user_version").fetchone()[0], 5)
                    columns = {
                        row[1]
                        for row in check.execute("PRAGMA table_info(operations)")
                    }
                    self.assertIn("dispatch_kind", columns)
                    self.assertEqual(
                        check.execute(
                            "SELECT dispatch_kind FROM operations WHERE operation_id = ?",
                            (contract.operation_id,),
                        ).fetchone()[0],
                        "ordinary",
                    )
                    self.assertIsNotNone(
                        check.execute(
                            "SELECT name FROM sqlite_master WHERE name = 'continuation_baselines'"
                        ).fetchone()
                    )
                    self.assertIsNotNone(
                        check.execute(
                            "SELECT name FROM sqlite_master WHERE name = 'queue_baselines'"
                        ).fetchone()
                    )

    def test_v4_migration_backfills_continue_and_queue_dispatch_kinds(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            workspace = root_path / "workspace"
            workspace.mkdir()
            database = root_path / "journal.sqlite3"
            continuation = make_contract(
                workspace,
                task_id="synthetic-continuation-task",
                operation_id="synthetic-continuation-001",
            )
            queue = make_contract(
                workspace,
                task_id="synthetic-queue-task",
                operation_id="synthetic-queue-001",
            )
            with SQLiteJournal(database) as journal:
                journal.reserve_operation(
                    continuation, dispatch_kind="continue"
                )
                journal.bind_thread(
                    continuation.task_id,
                    "thread-synthetic-continuation",
                )
                journal.store_continuation_baseline(
                    continuation.operation_id,
                    "thread-synthetic-continuation",
                    (),
                )
                journal.reserve_operation(queue, dispatch_kind="queue")
                journal.bind_thread(queue.task_id, "thread-synthetic-queue")
                journal.store_queue_baseline(
                    queue.operation_id,
                    "thread-synthetic-queue",
                    queue.prompt_sha256,
                    (),
                )
            with closing(sqlite3.connect(database)) as legacy:
                legacy.execute("DROP TRIGGER operations_dispatch_kind_is_immutable")
                legacy.execute("ALTER TABLE operations DROP COLUMN dispatch_kind")
                legacy.execute("PRAGMA user_version = 4")
                legacy.commit()

            with SQLiteJournal(database) as migrated:
                self.assertEqual(
                    migrated.get_operation(
                        continuation.operation_id
                    ).dispatch_kind,
                    "continue",
                )
                self.assertEqual(
                    migrated.get_operation(queue.operation_id).dispatch_kind,
                    "queue",
                )
                with self.assertRaises(IdempotencyConflict):
                    migrated.reserve_operation(
                        continuation, dispatch_kind="queue"
                    )
                with self.assertRaises(IdempotencyConflict):
                    migrated.reserve_operation(
                        queue, dispatch_kind="continue"
                    )

    def test_reservation_is_idempotent_and_conflicts_on_changed_content(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            workspace = root_path / "workspace"
            workspace.mkdir()
            contract = make_contract(workspace)
            with SQLiteJournal(root_path / "journal.sqlite3") as journal:
                first = journal.reserve_operation(contract)
                duplicate = journal.reserve_operation(contract)
                self.assertTrue(first.created)
                self.assertFalse(duplicate.created)
                self.assertEqual(first.operation, duplicate.operation)

                conflict = make_contract(workspace, prompt="Different synthetic content.")
                with self.assertRaises(IdempotencyConflict):
                    journal.reserve_operation(conflict)

                changed_policy = make_contract(workspace, timeout_seconds=61)
                with self.assertRaises(IdempotencyConflict):
                    journal.reserve_operation(changed_policy)

    def test_dispatch_kind_is_reserved_atomically_and_cannot_change(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            workspace = root_path / "workspace"
            workspace.mkdir()
            contract = make_contract(workspace)
            with SQLiteJournal(root_path / "journal.sqlite3") as journal:
                first = journal.reserve_operation(
                    contract, dispatch_kind="queue"
                )
                duplicate = journal.reserve_operation(
                    contract, dispatch_kind="queue"
                )

                self.assertTrue(first.created)
                self.assertFalse(duplicate.created)
                self.assertEqual(first.operation.dispatch_kind, "queue")
                with self.assertRaises(IdempotencyConflict):
                    journal.reserve_operation(
                        contract, dispatch_kind="ordinary"
                    )
                observed = journal.reserve_operation(
                    contract, dispatch_kind=None
                )
                self.assertFalse(observed.created)
                self.assertEqual(observed.operation.dispatch_kind, "queue")

    def test_task_and_operation_identity_cannot_be_rebound(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            first_workspace = root_path / "first"
            second_workspace = root_path / "second"
            first_workspace.mkdir()
            second_workspace.mkdir()
            with SQLiteJournal(root_path / "journal.sqlite3") as journal:
                journal.reserve(make_contract(first_workspace))
                with self.assertRaises(IdempotencyConflict):
                    journal.reserve(
                        make_contract(
                            second_workspace,
                            operation_id="synthetic-task-002",
                        )
                    )
                with self.assertRaises(IdempotencyConflict):
                    journal.reserve(
                        make_contract(
                            first_workspace,
                            task_id="another-task",
                            operation_id="synthetic-task-001",
                        )
                    )

    def test_concurrent_duplicate_reservation_creates_exactly_one_record(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            workspace = root_path / "workspace"
            workspace.mkdir()
            database = root_path / "journal.sqlite3"
            first = SQLiteJournal(database)
            second = SQLiteJournal(database)
            barrier = threading.Barrier(2)

            def reserve(journal: SQLiteJournal) -> bool:
                barrier.wait(timeout=2)
                return journal.reserve(make_contract(workspace)).created

            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    results = list(executor.map(reserve, (first, second)))
                self.assertEqual(sorted(results), [False, True])
                self.assertEqual(len(first.list_operations()), 1)
            finally:
                first.close()
                second.close()

    def test_operation_guard_has_one_owner_and_releases_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            database = Path(root, "journal.sqlite3")
            first = SQLiteJournal(database)
            second = SQLiteJournal(database)
            try:
                with first.operation_guard("synthetic-task-001") as first_owned:
                    self.assertTrue(first_owned)
                    with second.operation_guard("synthetic-task-001") as second_owned:
                        self.assertFalse(second_owned)
                with second.operation_guard("synthetic-task-001") as released:
                    self.assertTrue(released)
            finally:
                first.close()
                second.close()

    def test_one_open_operation_per_task_including_unknown_and_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            workspace = root_path / "workspace"
            workspace.mkdir()
            database = root_path / "journal.sqlite3"
            first = make_contract(workspace)
            second = make_contract(workspace, operation_id="synthetic-task-002")
            third = make_contract(workspace, operation_id="synthetic-task-003")
            with SQLiteJournal(database) as journal:
                journal.reserve(first)
                with self.assertRaises(TaskBusy):
                    journal.reserve(second)

                journal.transition(first.operation_id, "UNKNOWN", failure_code="lost_response")
                with self.assertRaises(TaskBusy):
                    journal.reserve(second)
                journal.bind_thread(first.task_id, "thread-synthetic-001")
                journal.bind_turn(first.operation_id, "turn-synthetic-001")
                journal.finalize_reconciliation(
                    first.operation_id,
                    make_receipt(
                        first,
                        status=OperationState.FAILED,
                        failure_code="readback_failed",
                        include_verification=False,
                    ),
                )

                self.assertTrue(journal.reserve(second).created)
                journal.transition(second.operation_id, "TIMED_OUT", failure_code="deadline")
                with self.assertRaises(TaskBusy):
                    journal.reserve(third)
                journal.bind_turn(second.operation_id, "turn-synthetic-002")
                journal.finalize_reconciliation(
                    second.operation_id,
                    make_receipt(
                        second,
                        status=OperationState.FAILED,
                        failure_code="readback_failed",
                        turn_id="turn-synthetic-002",
                        include_verification=False,
                    ),
                )
                self.assertTrue(journal.reserve(third).created)

    def test_database_uses_wal_private_mode_and_never_contains_raw_prompt(self) -> None:
        private_prompt = "SYNTHETIC-RAW-PROMPT-MUST-NOT-BE-STORED-9371"
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            workspace = root_path / "workspace"
            workspace.mkdir()
            database = root_path / "journal.sqlite3"
            journal = SQLiteJournal(database)
            journal.reserve(make_contract(workspace, prompt=private_prompt))
            with closing(sqlite3.connect(database)) as check:
                mode = check.execute("PRAGMA journal_mode").fetchone()[0]
                contract_json = check.execute(
                    "SELECT contract_json FROM operations"
                ).fetchone()[0]
            self.assertEqual(mode.lower(), "wal")
            self.assertNotIn(private_prompt, contract_json)
            self.assertIn(sha256_text(private_prompt), contract_json)
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(database.stat().st_mode), 0o600)
            journal.close()
            for candidate in root_path.glob("journal.sqlite3*"):
                self.assertNotIn(private_prompt.encode("utf-8"), candidate.read_bytes())


class JournalLifecycleTests(unittest.TestCase):
    def test_restart_preserves_immutable_workspace_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            workspace = root_path / "workspace"
            workspace.mkdir()
            Path(workspace, "synthetic.txt").write_text("before\n", encoding="utf-8")
            database = root_path / "journal.sqlite3"
            contract = make_contract(workspace)
            baseline = snapshot_workspace(workspace, WorkspacePolicy((root_path,)))
            with SQLiteJournal(database) as journal:
                journal.reserve(contract)
                self.assertEqual(journal.store_baseline(contract.operation_id, baseline), baseline)
                self.assertEqual(journal.store_baseline(contract.operation_id, baseline), baseline)

            Path(workspace, "synthetic.txt").write_text("after\n", encoding="utf-8")
            with SQLiteJournal(database) as reopened:
                self.assertEqual(reopened.get_baseline(contract.operation_id), baseline)
                with self.assertRaises(BaselineConflict):
                    reopened.store_baseline(
                        contract.operation_id,
                        snapshot_workspace(workspace, WorkspacePolicy((root_path,))),
                    )

    def test_valid_lifecycle_requires_and_preserves_exact_bindings(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            workspace = root_path / "workspace"
            workspace.mkdir()
            contract = make_contract(workspace)
            with SQLiteJournal(root_path / "journal.sqlite3") as journal:
                journal.reserve(contract)
                journal.transition(contract.operation_id, "STARTING_THREAD")
                with self.assertRaises(InvalidTransition):
                    journal.transition(contract.operation_id, "READY")
                first_thread = journal.bind_thread(contract.task_id, "thread-synthetic-001")
                same_thread = journal.bind_thread(contract.task_id, "thread-synthetic-001")
                self.assertEqual(first_thread, same_thread)
                with self.assertRaises(BindingConflict):
                    journal.bind_thread(contract.task_id, "thread-synthetic-002")

                journal.transition(contract.operation_id, "READY")
                journal.transition(contract.operation_id, "DISPATCHING")
                first_turn = journal.bind_turn(contract.operation_id, "turn-synthetic-001")
                same_turn = journal.bind_turn(contract.operation_id, "turn-synthetic-001")
                self.assertEqual(first_turn, same_turn)
                with self.assertRaises(BindingConflict):
                    journal.bind_turn(contract.operation_id, "turn-synthetic-002")
                journal.transition(contract.operation_id, "RUNNING")
                journal.transition(contract.operation_id, "TURN_COMPLETED")
                journal.transition(contract.operation_id, "VERIFYING")
                receipt = make_receipt(contract)
                final = journal.finalize_operation(contract.operation_id, receipt)
                self.assertEqual(final.operation.state, OperationState.VERIFIED)
                self.assertEqual(
                    journal.finalize_operation(contract.operation_id, receipt), final
                )
                with self.assertRaises(InvalidTransition):
                    journal.transition(contract.operation_id, "VERIFIED")
                with self.assertRaises(InvalidTransition):
                    journal.transition(contract.operation_id, "RUNNING")

    def test_unknown_forbids_ordinary_retry_and_reconcile_requires_exact_ids(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            workspace = root_path / "workspace"
            workspace.mkdir()
            contract = make_contract(workspace)
            with SQLiteJournal(root_path / "journal.sqlite3") as journal:
                journal.reserve(contract)
                journal.transition(contract.operation_id, "UNKNOWN", failure_code="lost_response")
                with self.assertRaises(InvalidTransition):
                    journal.transition(contract.operation_id, "DISPATCHING")
                with self.assertRaises(InvalidTransition):
                    journal.reconcile(contract.operation_id, "RUNNING")

                journal.bind_thread(contract.task_id, "thread-synthetic-001")
                journal.bind_turn(contract.operation_id, "turn-synthetic-001")
                reconciled = journal.reconcile(contract.operation_id, "RUNNING")
                self.assertEqual(reconciled.state, OperationState.RUNNING)

    def test_explicit_abandon_only_closes_unrecoverable_open_state(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            workspace = root_path / "workspace"
            workspace.mkdir()
            with SQLiteJournal(root_path / "journal.sqlite3") as journal:
                first = make_contract(workspace)
                journal.reserve(first)
                journal.transition(first.operation_id, "UNKNOWN", failure_code="lost_response")
                abandoned = journal.abandon_unrecoverable(
                    first.operation_id,
                    make_receipt(
                        first,
                        status=OperationState.FAILED,
                        failure_code="operator_abandoned_unrecoverable_operation",
                        thread_id=None,
                        turn_id=None,
                        include_verification=False,
                    ),
                )
                self.assertEqual(abandoned.operation.state, OperationState.FAILED)
                self.assertEqual(abandoned.receipt.status, OperationState.FAILED)
                self.assertTrue(
                    journal.reserve(
                        make_contract(workspace, operation_id="synthetic-task-002")
                    ).created
                )

                second_id = "synthetic-task-002"
                journal.bind_thread(first.task_id, "thread-synthetic-001")
                journal.transition(second_id, "READY")
                journal.transition(second_id, "DISPATCHING")
                journal.bind_turn(second_id, "turn-synthetic-002")
                journal.transition(second_id, "UNKNOWN", failure_code="transport_lost")
                with self.assertRaises(InvalidTransition):
                    journal.abandon_unrecoverable(
                        second_id,
                        make_receipt(
                            make_contract(
                                workspace, operation_id="synthetic-task-002"
                            ),
                            status=OperationState.FAILED,
                            failure_code=(
                                "operator_abandoned_unrecoverable_operation"
                            ),
                            turn_id="turn-synthetic-002",
                            include_verification=False,
                        ),
                    )

                continuation = make_contract(
                    workspace,
                    task_id="synthetic-continuation-task",
                    operation_id="synthetic-continuation-001",
                )
                journal.reserve_operation(
                    continuation,
                    dispatch_kind="continue",
                )
                journal.bind_thread(
                    continuation.task_id,
                    "thread-synthetic-continuation",
                )
                journal.transition(continuation.operation_id, "READY")
                journal.transition(continuation.operation_id, "DISPATCHING")
                journal.bind_turn(
                    continuation.operation_id,
                    "turn-synthetic-continuation",
                )
                journal.transition(
                    continuation.operation_id,
                    "UNKNOWN",
                    failure_code="transport_lost",
                )
                abandoned = journal.abandon_unrecoverable(
                    continuation.operation_id,
                    make_receipt(
                        continuation,
                        status=OperationState.FAILED,
                        failure_code=(
                            "operator_abandoned_unrecoverable_operation"
                        ),
                        thread_id="thread-synthetic-continuation",
                        turn_id="turn-synthetic-continuation",
                        include_verification=False,
                    ),
                )
                self.assertEqual(
                    abandoned.operation.state,
                    OperationState.FAILED,
                )

    def test_thread_and_turn_cannot_be_shared_by_different_records(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            workspace = root_path / "workspace"
            workspace.mkdir()
            with SQLiteJournal(root_path / "journal.sqlite3") as journal:
                first = make_contract(workspace)
                second = make_contract(
                    workspace,
                    task_id="synthetic-task-two",
                    operation_id="synthetic-task-two-001",
                )
                journal.reserve(first)
                journal.reserve(second)
                journal.transition(first.operation_id, "STARTING_THREAD")
                journal.transition(second.operation_id, "STARTING_THREAD")
                journal.bind_thread(first.task_id, "thread-shared")
                with self.assertRaises(BindingConflict):
                    journal.bind_thread(second.task_id, "thread-shared")

                journal.bind_thread(second.task_id, "thread-second")
                journal.transition(first.operation_id, "READY")
                journal.transition(second.operation_id, "READY")
                journal.transition(first.operation_id, "DISPATCHING")
                journal.transition(second.operation_id, "DISPATCHING")
                journal.bind_turn(first.operation_id, "turn-shared")
                with self.assertRaises(BindingConflict):
                    journal.bind_turn(second.operation_id, "turn-shared")

    def test_restart_preserves_state_and_bindings(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            workspace = root_path / "workspace"
            workspace.mkdir()
            database = root_path / "journal.sqlite3"
            contract = make_contract(workspace)
            with SQLiteJournal(database) as journal:
                journal.reserve(contract)
                journal.transition(contract.operation_id, "STARTING_THREAD")
                journal.bind_thread(contract.task_id, "thread-synthetic-001")
                journal.transition(contract.operation_id, "READY")
                journal.transition(contract.operation_id, "DISPATCHING")
                journal.bind_turn(contract.operation_id, "turn-synthetic-001")
                journal.transition(contract.operation_id, "RUNNING")

            with SQLiteJournal(database) as reopened:
                self.assertEqual(
                    reopened.get_task(contract.task_id).thread_id,
                    "thread-synthetic-001",
                )
                operation = reopened.get_operation(contract.operation_id)
                self.assertEqual(operation.turn_id, "turn-synthetic-001")
                self.assertEqual(operation.state, OperationState.RUNNING)
                self.assertFalse(reopened.reserve(contract).created)


class JournalReceiptTests(unittest.TestCase):
    def test_terminal_transition_and_receipt_are_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            workspace = root_path / "workspace"
            workspace.mkdir()
            contract = make_contract(workspace)
            with SQLiteJournal(root_path / "journal.sqlite3") as journal:
                journal.reserve(contract)
                journal.transition(contract.operation_id, OperationState.STARTING_THREAD)
                journal.bind_thread(contract.task_id, "thread-synthetic-001")
                journal.transition(contract.operation_id, OperationState.READY)
                journal.transition(contract.operation_id, OperationState.DISPATCHING)
                journal.bind_turn(contract.operation_id, "turn-synthetic-001")
                journal.transition(contract.operation_id, OperationState.RUNNING)
                journal.transition(contract.operation_id, OperationState.TURN_COMPLETED)
                journal.transition(contract.operation_id, OperationState.VERIFYING)

                invalid_value = make_receipt(contract).to_dict()
                invalid_value["workspace_sha256"] = sha256_text("unrelated-workspace")
                with self.assertRaises(ReceiptConflict):
                    journal.finalize_operation(
                        contract.operation_id, Receipt.from_dict(invalid_value)
                    )
                self.assertEqual(
                    journal.get_operation(contract.operation_id).state,
                    OperationState.VERIFYING,
                )
                with self.assertRaises(RecordNotFound):
                    journal.get_receipt(contract.operation_id)

                finalization = journal.finalize_operation(
                    contract.operation_id, make_receipt(contract)
                )
                self.assertEqual(finalization.operation.state, OperationState.VERIFIED)
                self.assertEqual(
                    journal.get_receipt(contract.operation_id), finalization.receipt
                )

    def test_receipt_is_immutable_and_bound_to_exact_operation(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            workspace = root_path / "workspace"
            workspace.mkdir()
            contract = make_contract(workspace)
            with SQLiteJournal(root_path / "journal.sqlite3") as journal:
                journal.reserve(contract)
                move_to_verified(journal, contract)
                receipt = make_receipt(contract)
                self.assertEqual(journal.store_receipt(contract.operation_id, receipt), receipt)
                self.assertEqual(journal.store_receipt(contract.operation_id, receipt), receipt)
                self.assertEqual(journal.get_receipt(contract.operation_id), receipt)

                replacement = make_receipt(
                    contract, changed_digest=sha256_text("different synthetic file")
                )
                with self.assertRaises(ReceiptConflict):
                    journal.store_receipt(contract.operation_id, replacement)

            with closing(
                sqlite3.connect(root_path / "journal.sqlite3")
            ) as connection:
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "UPDATE receipts SET receipt_sha256 = ? WHERE operation_id = ?",
                        (sha256_text("replacement"), contract.operation_id),
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "DELETE FROM receipts WHERE operation_id = ?",
                        (contract.operation_id,),
                    )

    def test_receipt_binding_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            workspace = root_path / "workspace"
            workspace.mkdir()
            contract = make_contract(workspace)
            with SQLiteJournal(root_path / "journal.sqlite3") as journal:
                journal.reserve(contract)
                move_to_verified(journal, contract)
                value = make_receipt(contract).to_dict()
                value["thread_sha256"] = sha256_text("unrelated-thread")
                with self.assertRaises(ReceiptConflict):
                    journal.store_receipt(contract.operation_id, Receipt.from_dict(value))


if __name__ == "__main__":
    unittest.main()
