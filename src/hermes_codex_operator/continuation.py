"""At-most-once continuation of one exact, authoritative idle Codex thread."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

from .adapters.base import (
    AdapterError,
    AdapterTimeout,
    CodexAdapter,
    ProtocolIntegrityError,
    ReadbackMissingResult,
    ReasoningEffort,
    RemoteTurnFailed,
    TurnOptions,
)
from .engine import AdapterFactory, Operator, OperatorOutcome
from .journal import BindingConflict, SQLiteJournal
from .models import OperationState, WorkContract, validate_external_id
from .workspace import WorkspaceError, WorkspacePolicy, resolve_workspace, snapshot_workspace


MAX_BASELINE_TURNS = 1000


class ContinuationOperator(Operator):
    """Distinct mutation service for one existing idle thread.

    A repeated invocation only observes the durable operation. It never resumes
    or starts a turn, including when the first process stopped after reservation.
    """

    def __init__(
        self,
        journal: SQLiteJournal,
        adapter_factory: AdapterFactory,
        workspace_policy: WorkspacePolicy,
    ) -> None:
        super().__init__(journal, adapter_factory, workspace_policy)

    async def continue_idle(
        self, contract: WorkContract, *, thread_id: str
    ) -> OperatorOutcome:
        expected_thread = validate_external_id(thread_id, "thread_id")
        reservation = self.journal.reserve_operation(
            contract, dispatch_kind="continue"
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

            adapter: CodexAdapter = self.adapter_factory()
            try:
                try:
                    remote = await adapter.read_thread(expected_thread)
                    self._validate_idle_identity(
                        remote, expected_thread, resolved_workspace
                    )
                    baseline_ids = remote.thread.turn_ids
                    if len(baseline_ids) > MAX_BASELINE_TURNS:
                        raise ProtocolIntegrityError("baseline turn set exceeds its bound")
                except (AdapterError, KeyError, ValueError):
                    return self._terminal_outcome(
                        contract,
                        OperationState.FAILED,
                        "continuation_identity_unconfirmed",
                        dispatched=False,
                    )

                self.journal.bind_thread(contract.task_id, expected_thread)
                self.journal.store_baseline(contract.operation_id, baseline_workspace)
                self.journal.store_continuation_baseline(
                    contract.operation_id, expected_thread, baseline_ids
                )
                self.journal.transition(contract.operation_id, OperationState.READY)
                self.journal.transition(contract.operation_id, OperationState.DISPATCHING)

                try:
                    resumed = await adapter.resume_thread(
                        expected_thread,
                        workspace,
                        self._thread_options(contract),
                    )
                    if resumed.thread_id != expected_thread or resumed.workspace != workspace:
                        raise ProtocolIntegrityError(
                            "thread/resume returned mismatched authoritative identity"
                        )
                    turn = await adapter.start_turn(
                        expected_thread,
                        contract.prompt,
                        TurnOptions(
                            model=contract.model,
                            effort=cast(ReasoningEffort | None, contract.reasoning_effort),
                            approval_policy="never",
                        ),
                    )
                    if turn.thread_id != expected_thread:
                        raise ProtocolIntegrityError(
                            "turn/start returned a different thread identity"
                        )
                    try:
                        self.journal.bind_turn(contract.operation_id, turn.turn_id)
                    except BindingConflict:
                        return self._terminal_outcome(
                            contract,
                            OperationState.UNKNOWN,
                            "continuation_turn_binding_conflict",
                            dispatched=True,
                        )
                    correlated = await adapter.read_thread(expected_thread)
                    self._validate_correlated_turn(
                        correlated.thread.turn_ids, baseline_ids, turn.turn_id
                    )
                except (AdapterError, KeyError, ValueError):
                    return self._terminal_outcome(
                        contract,
                        OperationState.UNKNOWN,
                        "continuation_delivery_unknown",
                        dispatched=True,
                    )

                self.journal.transition(contract.operation_id, OperationState.RUNNING)
                try:
                    terminal = await adapter.wait_for_completion(
                        expected_thread, turn.turn_id, float(contract.timeout_seconds)
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
                    readback = await adapter.read_turn(expected_thread, turn.turn_id)
                    if (
                        readback.thread_id != expected_thread
                        or readback.turn_id != turn.turn_id
                        or readback.status != "completed"
                    ):
                        raise ProtocolIntegrityError(
                            "authoritative readback returned mismatched identity or state"
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
                        OperationState.FAILED,
                        "exact_turn_readback_failed",
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

    @staticmethod
    def _validate_idle_identity(remote, thread_id: str, workspace: Path) -> None:
        if remote.thread.thread_id != thread_id:
            raise ProtocolIntegrityError("thread/read returned a different thread id")
        try:
            remote_workspace = remote.thread.cwd.resolve(strict=True)
        except OSError as exc:
            raise ProtocolIntegrityError(
                "thread/read workspace identity is unavailable"
            ) from exc
        if remote_workspace != workspace:
            raise ProtocolIntegrityError("thread/read workspace identity mismatch")
        if remote.thread.status != "idle":
            raise ProtocolIntegrityError("thread is not authoritatively idle")
        ids = remote.thread.turn_ids
        if ids != tuple(turn.turn_id for turn in remote.turns):
            raise ProtocolIntegrityError("thread/read turn identity views disagree")
        if len(set(ids)) != len(ids):
            raise ProtocolIntegrityError("thread/read returned duplicate turn ids")

    @staticmethod
    def _validate_correlated_turn(
        current_ids: tuple[str, ...], baseline_ids: tuple[str, ...], turn_id: str
    ) -> None:
        if len(set(current_ids)) != len(current_ids):
            raise ProtocolIntegrityError("post-dispatch turn set contains duplicates")
        baseline = set(baseline_ids)
        current = set(current_ids)
        if turn_id in baseline or current - baseline != {turn_id} or baseline - current:
            raise ProtocolIntegrityError(
                "post-dispatch turn set does not correlate to exactly one returned turn"
            )


__all__ = ["ContinuationOperator", "MAX_BASELINE_TURNS"]
