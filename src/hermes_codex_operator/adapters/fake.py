from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .base import (
    AdapterClosed,
    AdapterTimeout,
    AppServerProcessError,
    DeliveryUnknown,
    ReadbackMissingResult,
    RemoteTurnFailed,
    ThreadHandle,
    ThreadOptions,
    TurnHandle,
    TurnInProgress,
    TurnOptions,
    TurnReadback,
    TurnTerminalStatus,
)


@dataclass(slots=True)
class FakeBackend:
    final_text: str = "Synthetic Codex result."
    terminal_status: TurnTerminalStatus = "completed"
    fail_at: str | None = None
    mutate_workspace: Callable[[Path], None] | None = None
    thread_id: str = "thread-synthetic-001"
    turn_id: str = "turn-synthetic-001"
    threads: dict[str, Path] = field(default_factory=dict)
    turns: dict[tuple[str, str], TurnTerminalStatus] = field(default_factory=dict)
    start_thread_calls: int = 0
    resume_thread_calls: int = 0
    start_turn_calls: int = 0
    read_turn_calls: int = 0
    last_workspace: Path | None = None


class FakeCodexAdapter:
    """Deterministic in-memory adapter for lifecycle and crash tests."""

    def __init__(self, backend: FakeBackend | None = None) -> None:
        self.backend = backend or FakeBackend()
        self._closed = False

    def _ensure_open(self) -> None:
        if self._closed:
            raise AdapterClosed("fake adapter is closed")

    async def start_thread(
        self, workspace: Path, options: ThreadOptions | None = None
    ) -> ThreadHandle:
        self._ensure_open()
        self.backend.start_thread_calls += 1
        self.backend.last_workspace = workspace
        if self.backend.fail_at == "start_thread_delivery_unknown":
            raise DeliveryUnknown("thread/start", 1, timeout_seconds=0.01)
        self.backend.threads[self.backend.thread_id] = workspace
        return ThreadHandle(self.backend.thread_id, workspace)

    async def resume_thread(
        self,
        thread_id: str,
        workspace: Path,
        options: ThreadOptions | None = None,
    ) -> ThreadHandle:
        self._ensure_open()
        self.backend.resume_thread_calls += 1
        known = self.backend.threads.get(thread_id)
        if known is not None and known != workspace:
            raise RuntimeError("fake thread workspace mismatch")
        self.backend.threads[thread_id] = workspace
        self.backend.last_workspace = workspace
        return ThreadHandle(thread_id, workspace)

    async def start_turn(
        self,
        thread_id: str,
        prompt: str,
        options: TurnOptions | None = None,
    ) -> TurnHandle:
        self._ensure_open()
        self.backend.start_turn_calls += 1
        if self.backend.fail_at == "start_turn_delivery_unknown":
            raise DeliveryUnknown("turn/start", 2, timeout_seconds=0.01)
        workspace = self.backend.threads[thread_id]
        if self.backend.mutate_workspace is not None:
            self.backend.mutate_workspace(workspace)
        key = (thread_id, self.backend.turn_id)
        self.backend.turns[key] = self.backend.terminal_status
        return TurnHandle(thread_id, self.backend.turn_id)

    async def wait_for_completion(
        self, thread_id: str, turn_id: str, timeout: float
    ) -> TurnTerminalStatus:
        self._ensure_open()
        if self.backend.fail_at == "wait_timeout":
            raise AdapterTimeout("turn/completed", timeout)
        if self.backend.fail_at == "wait_transport_lost":
            raise AppServerProcessError("synthetic transport loss")
        return self.backend.turns[(thread_id, turn_id)]

    async def read_turn(self, thread_id: str, turn_id: str) -> TurnReadback:
        self._ensure_open()
        self.backend.read_turn_calls += 1
        if self.backend.fail_at == "readback_missing":
            raise ReadbackMissingResult(thread_id, turn_id, "synthetic missing result")
        status = self.backend.turns[(thread_id, turn_id)]
        if status == "inProgress":
            raise TurnInProgress(thread_id, turn_id)
        if status != "completed":
            raise RemoteTurnFailed(thread_id, turn_id, status)
        return TurnReadback(
            thread_id=thread_id,
            turn_id=turn_id,
            status="completed",
            final_text=self.backend.final_text,
            items=(
                {
                    "id": "item-synthetic-001",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": self.backend.final_text,
                },
            ),
        )

    async def interrupt(self, thread_id: str, turn_id: str) -> None:
        self._ensure_open()
        if self.backend.fail_at == "interrupt_delivery_unknown":
            raise DeliveryUnknown("turn/interrupt", 3, timeout_seconds=0.01)
        self.backend.turns[(thread_id, turn_id)] = "interrupted"

    async def close(self) -> None:
        self._closed = True
