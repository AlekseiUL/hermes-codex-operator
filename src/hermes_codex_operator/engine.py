from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import threading
from typing import Callable, cast

from .adapters.base import (
    AdapterError,
    AdapterTimeout,
    CodexAdapter,
    DeliveryUnknown,
    ProtocolIntegrityError,
    ReadbackMissingResult,
    ReasoningEffort,
    RemoteRequestError,
    RemoteTurnFailed,
    ThreadOptions,
    TurnInProgress,
    TurnOptions,
)
from .journal import OperationRecord, RecordNotFound, SQLiteJournal
from .models import (
    ChangedPathEvidence,
    DELIVERABLE_STATES,
    OperationState,
    Receipt,
    VerificationEvidence,
    WorkContract,
    sha256_text,
)
from .verification import VerificationResult, run_verifications
from .workspace import (
    ScopeViolation,
    WorkspaceError,
    WorkspacePolicy,
    WorkspaceSnapshot,
    changed_paths,
    enforce_scope,
    snapshot_workspace,
)


AdapterFactory = Callable[[], CodexAdapter]


@dataclass(frozen=True, slots=True)
class OperatorOutcome:
    operation: OperationRecord
    receipt: Receipt | None
    final_text: str | None
    dispatched: bool


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


class Operator:
    """Foreground, crash-conservative lifecycle coordinator.

    A caller may place this foreground operation inside its own durable worker.
    Operator itself intentionally does not add a daemon or scheduler.
    """

    def __init__(
        self,
        journal: SQLiteJournal,
        adapter_factory: AdapterFactory,
        workspace_policy: WorkspacePolicy,
    ) -> None:
        self.journal = journal
        self.adapter_factory = adapter_factory
        self.workspace_policy = workspace_policy

    def status(self, operation_id: str) -> OperationRecord:
        return self.journal.get_operation(operation_id)

    async def submit(self, contract: WorkContract) -> OperatorOutcome:
        reservation = self.journal.reserve_operation(contract)
        with self.journal.operation_guard(contract.operation_id) as owned:
            if not owned:
                return self._observe_existing(contract)
            operation = self.journal.get_operation(contract.operation_id)
            if not reservation.created:
                return await self._resume_existing(contract, operation)

            try:
                baseline = await self._load_or_capture_baseline(contract)
            except WorkspaceError:
                return self._terminal_outcome(
                    contract,
                    OperationState.FAILED,
                    "workspace_policy_failed",
                    dispatched=False,
                )
            return await self._dispatch(contract, baseline)

    async def recover(self, contract: WorkContract) -> OperatorOutcome:
        """Read and finish an existing operation without creating a new turn."""

        try:
            self.journal.get_operation(contract.operation_id)
        except RecordNotFound:
            raise RecordNotFound("cannot recover an operation that was never reserved")
        # Re-run the full idempotency comparison without creating a new record.
        reservation = self.journal.reserve_operation(contract)
        with self.journal.operation_guard(contract.operation_id) as owned:
            if not owned:
                return self._observe_existing(contract)
            operation = self.journal.get_operation(contract.operation_id)
            return await self._resume_existing(
                contract,
                operation,
                allow_dispatch=False,
                recover_ambiguous=True,
            )

    async def abandon(self, contract: WorkContract) -> OperatorOutcome:
        """Explicitly close an ambiguity that cannot be authoritatively recovered."""

        try:
            self.journal.get_operation(contract.operation_id)
        except RecordNotFound:
            raise RecordNotFound("cannot abandon an operation that was never reserved")
        self.journal.reserve_operation(contract)
        with self.journal.operation_guard(contract.operation_id) as owned:
            if not owned:
                return self._observe_existing(contract)
            return self._finalize_outcome(
                contract,
                OperationState.FAILED,
                "operator_abandoned_unrecoverable_operation",
                (),
                (),
                None,
                False,
                mode="abandon",
            )

    async def collect(self, contract: WorkContract) -> OperatorOutcome:
        """Return the verified receipt and re-read the exact final Codex result."""

        outcome = await self.recover(contract)
        if outcome.final_text is not None or outcome.operation.state != OperationState.VERIFIED:
            return outcome
        task = self.journal.get_task(contract.task_id)
        if task.thread_id is None or outcome.operation.turn_id is None:
            return outcome
        adapter = self.adapter_factory()
        try:
            await adapter.resume_thread(
                task.thread_id,
                Path(contract.workspace),
                self._thread_options(contract),
            )
            readback = await adapter.read_turn(task.thread_id, outcome.operation.turn_id)
            if (
                outcome.receipt is not None
                and outcome.receipt.result_sha256 is not None
                and sha256_text(readback.final_text)
                != outcome.receipt.result_sha256
            ):
                raise ProtocolIntegrityError(
                    "authoritative result digest changed after verification"
                )
        finally:
            await adapter.close()
        return OperatorOutcome(
            outcome.operation,
            outcome.receipt,
            readback.final_text,
            dispatched=False,
        )

    def _observe_existing(self, contract: WorkContract) -> OperatorOutcome:
        operation = self.journal.get_operation(contract.operation_id)
        try:
            receipt = self.journal.get_receipt(contract.operation_id)
        except RecordNotFound:
            receipt = None
        return OperatorOutcome(operation, receipt, None, dispatched=False)

    async def _resume_existing(
        self,
        contract: WorkContract,
        operation: OperationRecord,
        *,
        allow_dispatch: bool = True,
        recover_ambiguous: bool = False,
    ) -> OperatorOutcome:
        if recover_ambiguous and operation.state in {
            OperationState.UNKNOWN,
            OperationState.TIMED_OUT,
        }:
            return await self._recover_bound_turn(contract, operation)
        if operation.state in DELIVERABLE_STATES:
            try:
                receipt = self.journal.get_receipt(contract.operation_id)
            except RecordNotFound:
                receipt = None
            return OperatorOutcome(operation, receipt, None, dispatched=False)

        if operation.state == OperationState.QUEUED:
            if not allow_dispatch:
                return OperatorOutcome(operation, None, None, dispatched=False)
            baseline = await self._load_or_capture_baseline(contract)
            return await self._dispatch(contract, baseline)

        if operation.state == OperationState.READY:
            if not allow_dispatch:
                return OperatorOutcome(operation, None, None, dispatched=False)
            baseline = await self._load_or_capture_baseline(contract)
            return await self._dispatch(contract, baseline)

        if operation.state == OperationState.STARTING_THREAD:
            task = self.journal.get_task(contract.task_id)
            if task.thread_id is not None:
                operation = self.journal.transition(
                    contract.operation_id, OperationState.READY
                )
                if not allow_dispatch:
                    return OperatorOutcome(operation, None, None, dispatched=False)
                baseline = await self._load_or_capture_baseline(contract)
                return await self._dispatch(contract, baseline)
            operation = self.journal.transition(
                contract.operation_id,
                OperationState.UNKNOWN,
                failure_code="thread_dispatch_identity_missing",
            )
            return self._ambiguous_outcome(contract, operation, (), (), None, False)

        if operation.state == OperationState.DISPATCHING:
            if operation.turn_id is not None:
                return await self._recover_bound_turn(contract, operation)
            operation = self.journal.transition(
                contract.operation_id,
                OperationState.UNKNOWN,
                failure_code="turn_dispatch_identity_missing",
            )
            return self._ambiguous_outcome(contract, operation, (), (), None, False)

        if operation.state in {
            OperationState.RUNNING,
            OperationState.UNKNOWN,
            OperationState.TIMED_OUT,
            OperationState.TURN_COMPLETED,
            OperationState.VERIFYING,
        }:
            return await self._recover_bound_turn(contract, operation)

        return OperatorOutcome(operation, None, None, dispatched=False)

    async def _load_or_capture_baseline(
        self, contract: WorkContract
    ) -> WorkspaceSnapshot:
        try:
            return self.journal.get_baseline(contract.operation_id)
        except RecordNotFound:
            baseline = await asyncio.to_thread(
                snapshot_workspace, Path(contract.workspace), self.workspace_policy
            )
            return self.journal.store_baseline(contract.operation_id, baseline)

    async def _dispatch(
        self, contract: WorkContract, baseline: WorkspaceSnapshot
    ) -> OperatorOutcome:
        adapter = self.adapter_factory()
        dispatched = False
        final_text: str | None = None
        try:
            task = self.journal.get_task(contract.task_id)
            operation = self.journal.get_operation(contract.operation_id)
            if task.thread_id is None:
                if operation.state == OperationState.QUEUED:
                    self.journal.transition(
                        contract.operation_id, OperationState.STARTING_THREAD
                    )
                handle = await adapter.start_thread(
                    Path(contract.workspace),
                    self._thread_options(contract),
                )
                self.journal.bind_thread(contract.task_id, handle.thread_id)
            else:
                await adapter.resume_thread(
                    task.thread_id,
                    Path(contract.workspace),
                    self._thread_options(contract),
                )

            operation = self.journal.get_operation(contract.operation_id)
            if operation.state != OperationState.READY:
                self.journal.transition(contract.operation_id, OperationState.READY)
            self.journal.transition(contract.operation_id, OperationState.DISPATCHING)
            dispatched = True
            turn = await adapter.start_turn(
                self.journal.get_task(contract.task_id).thread_id or "",
                contract.prompt,
                TurnOptions(
                    model=contract.model,
                    effort=cast(ReasoningEffort | None, contract.reasoning_effort),
                    approval_policy="never",
                ),
            )
            self.journal.bind_turn(contract.operation_id, turn.turn_id)
            self.journal.transition(contract.operation_id, OperationState.RUNNING)
            try:
                terminal = await adapter.wait_for_completion(
                    turn.thread_id, turn.turn_id, float(contract.timeout_seconds)
                )
            except AdapterTimeout:
                try:
                    await adapter.interrupt(turn.thread_id, turn.turn_id)
                except AdapterError:
                    pass
                return self._terminal_outcome(
                    contract,
                    OperationState.TIMED_OUT,
                    "turn_timeout",
                    dispatched=True,
                )
            if terminal == "failed":
                return self._terminal_outcome(
                    contract,
                    OperationState.FAILED,
                    "remote_turn_failed",
                    dispatched=True,
                )
            if terminal == "interrupted":
                return self._terminal_outcome(
                    contract,
                    OperationState.CANCELLED,
                    "remote_turn_interrupted",
                    dispatched=True,
                )
            readback = await adapter.read_turn(turn.thread_id, turn.turn_id)
            final_text = readback.final_text
            self.journal.transition(contract.operation_id, OperationState.TURN_COMPLETED)
        except DeliveryUnknown as exc:
            code = "thread_start_delivery_unknown" if exc.method == "thread/start" else "turn_start_delivery_unknown"
            return self._terminal_outcome(
                contract,
                OperationState.UNKNOWN,
                code,
                dispatched=dispatched or exc.method == "turn/start",
            )
        except ReadbackMissingResult:
            return self._terminal_outcome(
                contract,
                OperationState.FAILED,
                "readback_missing_result",
                dispatched=dispatched,
            )
        except RemoteTurnFailed as exc:
            state = (
                OperationState.CANCELLED
                if exc.status == "interrupted"
                else OperationState.FAILED
            )
            return self._terminal_outcome(
                contract, state, "remote_turn_failed", dispatched=dispatched
            )
        except RemoteRequestError as exc:
            current = self.journal.get_operation(contract.operation_id)
            if exc.method == "turn/start" and current.turn_id is None:
                return self._terminal_outcome(
                    contract,
                    OperationState.FAILED,
                    "remote_turn_start_rejected",
                    dispatched=True,
                )
            if current.turn_id is not None or dispatched:
                return self._terminal_outcome(
                    contract,
                    OperationState.UNKNOWN,
                    "authoritative_turn_state_unknown",
                    dispatched=dispatched,
                )
            return self._terminal_outcome(
                contract,
                OperationState.FAILED,
                "remote_request_rejected",
                dispatched=False,
            )
        except AdapterError:
            current = self.journal.get_operation(contract.operation_id)
            if current.turn_id is not None or dispatched:
                return self._terminal_outcome(
                    contract,
                    OperationState.UNKNOWN,
                    "authoritative_turn_state_unknown",
                    dispatched=True,
                )
            return self._terminal_outcome(
                contract,
                OperationState.FAILED,
                "adapter_failed",
                dispatched=dispatched,
            )
        finally:
            await adapter.close()

        return await self._verify(contract, baseline, final_text, dispatched)

    async def _recover_bound_turn(
        self, contract: WorkContract, operation: OperationRecord
    ) -> OperatorOutcome:
        task = self.journal.get_task(contract.task_id)
        if task.thread_id is None or operation.turn_id is None:
            if operation.state not in {OperationState.UNKNOWN, OperationState.TIMED_OUT}:
                operation = self.journal.transition(
                    contract.operation_id,
                    OperationState.UNKNOWN,
                    failure_code="recovery_identity_missing",
                )
            return self._ambiguous_outcome(contract, operation, (), (), None, False)

        baseline = await self._load_or_capture_baseline(contract)
        adapter = self.adapter_factory()
        try:
            await adapter.resume_thread(
                task.thread_id,
                Path(contract.workspace),
                self._thread_options(contract),
            )
            readback = await adapter.read_turn(task.thread_id, operation.turn_id)
        except TurnInProgress:
            current = self.journal.get_operation(contract.operation_id)
            if current.state in {OperationState.UNKNOWN, OperationState.TIMED_OUT}:
                current = self.journal.reconcile(
                    contract.operation_id, OperationState.RUNNING
                )
            elif current.state == OperationState.DISPATCHING:
                current = self.journal.transition(
                    contract.operation_id, OperationState.RUNNING
                )
            elif current.state != OperationState.RUNNING:
                current = self.journal.transition(
                    contract.operation_id,
                    OperationState.UNKNOWN,
                    failure_code="remote_state_conflict",
                )
            return OperatorOutcome(current, None, None, dispatched=False)
        except RemoteTurnFailed as exc:
            state = (
                OperationState.CANCELLED
                if exc.status == "interrupted"
                else OperationState.FAILED
            )
            if operation.state in {OperationState.UNKNOWN, OperationState.TIMED_OUT}:
                return self._finalize_outcome(
                    contract,
                    state,
                    "remote_turn_failed",
                    (),
                    (),
                    None,
                    False,
                    mode="reconcile",
                )
            return self._terminal_outcome(
                contract, state, "remote_turn_failed", dispatched=False
            )
        except (AdapterError, KeyError):
            if operation.state not in {OperationState.UNKNOWN, OperationState.TIMED_OUT}:
                operation = self.journal.transition(
                    contract.operation_id,
                    OperationState.UNKNOWN,
                    failure_code="authoritative_readback_unavailable",
                )
            return self._ambiguous_outcome(contract, operation, (), (), None, False)
        finally:
            await adapter.close()

        current = self.journal.get_operation(contract.operation_id)
        if current.state in {OperationState.UNKNOWN, OperationState.TIMED_OUT}:
            self.journal.reconcile(contract.operation_id, OperationState.TURN_COMPLETED)
        elif current.state == OperationState.DISPATCHING:
            self.journal.transition(contract.operation_id, OperationState.RUNNING)
            self.journal.transition(contract.operation_id, OperationState.TURN_COMPLETED)
        elif current.state == OperationState.RUNNING:
            self.journal.transition(contract.operation_id, OperationState.TURN_COMPLETED)
        return await self._verify(contract, baseline, readback.final_text, False)

    async def _verify(
        self,
        contract: WorkContract,
        baseline: WorkspaceSnapshot,
        final_text: str | None,
        dispatched: bool,
    ) -> OperatorOutcome:
        try:
            codex_snapshot = await asyncio.to_thread(
                snapshot_workspace, Path(contract.workspace), self.workspace_policy
            )
            codex_paths = changed_paths(baseline, codex_snapshot)
            current = self.journal.get_operation(contract.operation_id)
            if current.state == OperationState.TURN_COMPLETED:
                self.journal.transition(contract.operation_id, OperationState.VERIFYING)
            enforce_scope(codex_paths, contract.allowed_change_globs)
        except ScopeViolation:
            return self._finalize_outcome(
                contract,
                OperationState.SCOPE_VIOLATION,
                "changed_path_outside_scope",
                self._changed_evidence(baseline, codex_snapshot, codex_paths),
                (),
                final_text,
                dispatched,
            )
        except WorkspaceError:
            return self._terminal_outcome(
                contract,
                OperationState.FAILED,
                "workspace_evidence_failed",
                dispatched=dispatched,
            )

        cancel_event = threading.Event()
        worker = asyncio.create_task(
            asyncio.to_thread(
                run_verifications,
                contract.verification,
                workspace=Path(contract.workspace),
                timeout_seconds=float(contract.timeout_seconds),
                cancel_event=cancel_event,
            ),
            name=f"operator-verification-{contract.operation_id}",
        )
        cancellation: asyncio.CancelledError | None = None
        while True:
            try:
                results = await asyncio.shield(worker)
                break
            except asyncio.CancelledError as exc:
                if cancellation is None:
                    cancellation = exc
                cancel_event.set()
            except Exception:
                outcome = self._terminal_outcome(
                    contract,
                    OperationState.FAILED,
                    "verification_runner_failed",
                    dispatched=dispatched,
                )
                if cancellation is not None:
                    raise cancellation
                return outcome

        evidence = self._verification_evidence(results)
        snapshot_worker = asyncio.create_task(
            asyncio.to_thread(
                snapshot_workspace, Path(contract.workspace), self.workspace_policy
            ),
            name=f"operator-final-snapshot-{contract.operation_id}",
        )
        while True:
            try:
                final_snapshot = await asyncio.shield(snapshot_worker)
                break
            except asyncio.CancelledError as exc:
                # The thread behind asyncio.to_thread cannot be cancelled.
                # Finish the evidence capture, record a terminal receipt, and
                # only then propagate cancellation to the caller.
                if cancellation is None:
                    cancellation = exc
        try:
            final_paths = changed_paths(baseline, final_snapshot)
            enforce_scope(final_paths, contract.allowed_change_globs)
        except ScopeViolation:
            outcome = self._finalize_outcome(
                contract,
                OperationState.SCOPE_VIOLATION,
                "changed_path_outside_scope",
                self._changed_evidence(
                    baseline,
                    final_snapshot,
                    changed_paths(baseline, final_snapshot),
                ),
                evidence,
                final_text,
                dispatched,
            )
            if cancellation is not None:
                raise cancellation
            return outcome
        except WorkspaceError:
            outcome = self._terminal_outcome(
                contract,
                OperationState.FAILED,
                "workspace_evidence_failed",
                dispatched=dispatched,
            )
            if cancellation is not None:
                raise cancellation
            return outcome

        changed = self._changed_evidence(baseline, final_snapshot, final_paths)
        if cancellation is not None:
            self._finalize_outcome(
                contract,
                OperationState.CANCELLED,
                "verification_cancelled",
                changed,
                evidence,
                final_text,
                dispatched,
            )
            raise cancellation

        failed = next((result for result in results if not result.passed), None)
        if failed is not None:
            state = OperationState.VERIFICATION_FAILED
            failure_code = failed.failure_code or "verification_failed"
        else:
            state = OperationState.VERIFIED
            failure_code = None
        return self._finalize_outcome(
            contract,
            state,
            failure_code,
            changed,
            evidence,
            final_text,
            dispatched,
        )

    def _terminal_outcome(
        self,
        contract: WorkContract,
        state: OperationState,
        failure_code: str,
        *,
        dispatched: bool,
    ) -> OperatorOutcome:
        if state in {
            OperationState.UNKNOWN,
            OperationState.TIMED_OUT,
        }:
            current = self.journal.get_operation(contract.operation_id)
            if current.state != state:
                current = self.journal.transition(
                    contract.operation_id, state, failure_code=failure_code
                )
            return self._ambiguous_outcome(
                contract, current, (), (), None, dispatched
            )
        return self._finalize_outcome(
            contract,
            state,
            failure_code,
            (),
            (),
            None,
            dispatched,
        )

    def _finalize_outcome(
        self,
        contract: WorkContract,
        state: OperationState,
        failure_code: str | None,
        changed: tuple[ChangedPathEvidence, ...],
        verification: tuple[VerificationEvidence, ...],
        final_text: str | None,
        dispatched: bool,
        *,
        mode: str = "ordinary",
    ) -> OperatorOutcome:
        operation = self.journal.get_operation(contract.operation_id)
        receipt = self._make_receipt(
            contract,
            operation,
            state,
            failure_code,
            changed,
            verification,
            final_text,
        )
        if mode == "ordinary":
            finalization = self.journal.finalize_operation(
                contract.operation_id, receipt
            )
        elif mode == "reconcile":
            finalization = self.journal.finalize_reconciliation(
                contract.operation_id, receipt
            )
        elif mode == "abandon":
            finalization = self.journal.abandon_unrecoverable(
                contract.operation_id, receipt
            )
        else:
            raise ValueError("unknown finalization mode")
        return OperatorOutcome(
            finalization.operation,
            finalization.receipt,
            final_text,
            dispatched,
        )

    def _ambiguous_outcome(
        self,
        contract: WorkContract,
        operation: OperationRecord,
        changed: tuple[ChangedPathEvidence, ...],
        verification: tuple[VerificationEvidence, ...],
        final_text: str | None,
        dispatched: bool,
    ) -> OperatorOutcome:
        if operation.state not in {
            OperationState.UNKNOWN,
            OperationState.TIMED_OUT,
        }:
            raise ValueError("ambiguous outcome requires a recoverable blocking state")
        receipt = self._make_receipt(
            contract,
            operation,
            operation.state,
            operation.failure_code,
            changed,
            verification,
            final_text,
        )
        return OperatorOutcome(operation, receipt, final_text, dispatched)

    def _make_receipt(
        self,
        contract: WorkContract,
        operation: OperationRecord,
        state: OperationState,
        failure_code: str | None,
        changed: tuple[ChangedPathEvidence, ...],
        verification: tuple[VerificationEvidence, ...],
        final_text: str | None,
    ) -> Receipt:
        task = self.journal.get_task(contract.task_id)
        return Receipt.from_dict(
            {
                "schema_version": "1",
                "task_id": contract.task_id,
                "operation_id": contract.operation_id,
                "status": state.value,
                "prompt_sha256": contract.prompt_sha256,
                "workspace_sha256": sha256_text(contract.workspace),
                "thread_sha256": sha256_text(task.thread_id) if task.thread_id else None,
                "turn_sha256": sha256_text(operation.turn_id) if operation.turn_id else None,
                "result_sha256": sha256_text(final_text) if final_text is not None else None,
                "changed_paths": [item.to_dict() for item in changed],
                "verification": [item.to_dict() for item in verification],
                "started_at": operation.created_at,
                "completed_at": _utc_now(),
                "failure_code": failure_code,
            }
        )

    @staticmethod
    def _changed_evidence(
        before: WorkspaceSnapshot,
        after: WorkspaceSnapshot,
        paths: tuple[str, ...],
    ) -> tuple[ChangedPathEvidence, ...]:
        return tuple(
            ChangedPathEvidence(path=path, sha256=after.files[path].sha256 if path in after.files else None)
            for path in paths
        )

    @staticmethod
    def _verification_evidence(
        results: tuple[VerificationResult, ...]
    ) -> tuple[VerificationEvidence, ...]:
        evidence = []
        for result in results:
            combined = hashlib.sha256(
                f"{result.stdout_sha256}:{result.stderr_sha256}".encode("ascii")
            ).hexdigest()
            evidence.append(
                VerificationEvidence(
                    command_sha256=result.argv_sha256,
                    exit_code=result.exit_code,
                    duration_ms=result.duration_ms,
                    output_sha256=combined,
                )
            )
        return tuple(evidence)

    @staticmethod
    def _thread_options(contract: WorkContract) -> ThreadOptions:
        return ThreadOptions(
            sandbox="workspace-write",
            approval_policy="never",
            model=contract.model,
        )
