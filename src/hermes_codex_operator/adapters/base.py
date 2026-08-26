from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol, Sequence, runtime_checkable


JSONValue = (
    None
    | bool
    | int
    | float
    | str
    | list["JSONValue"]
    | dict[str, "JSONValue"]
)
SandboxMode = Literal["read-only", "workspace-write", "danger-full-access"]
ApprovalPolicy = Literal["untrusted", "on-failure", "on-request", "never"]
ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh"]
TurnTerminalStatus = Literal["completed", "failed", "interrupted"]


class AdapterError(RuntimeError):
    """Base class for transport-neutral adapter failures."""


class AdapterClosed(AdapterError):
    """Raised when an operation is attempted after the adapter is closed."""


class AdapterTimeout(AdapterError):
    def __init__(self, method: str, timeout_seconds: float) -> None:
        self.method = method
        self.timeout_seconds = timeout_seconds
        super().__init__(f"timed out waiting for {method}")


class DeliveryUnknown(AdapterTimeout):
    """A side-effecting request may have reached Codex, so it must not be retried."""

    def __init__(
        self,
        method: str,
        request_id: int,
        *,
        sent: bool = True,
        timeout_seconds: float = 0.0,
    ) -> None:
        self.request_id = request_id
        self.sent = sent
        super().__init__(method, timeout_seconds)


class ProtocolIntegrityError(AdapterError):
    """The peer violated an identity, correlation, or message-shape invariant."""


class UnsupportedCapability(AdapterError):
    """An explicitly requested experimental capability is not allowlisted."""


class AppServerProcessError(AdapterError):
    def __init__(self, message: str, *, returncode: int | None = None) -> None:
        self.returncode = returncode
        super().__init__(message)


class RemoteRequestError(AdapterError):
    def __init__(
        self,
        method: str,
        *,
        code: int | None,
        message: str,
        data: Any = None,
    ) -> None:
        self.method = method
        self.code = code
        self.remote_message = message
        self.data = data
        super().__init__(f"Codex App Server rejected {method}: {message}")


class RemoteTurnFailed(AdapterError):
    def __init__(
        self,
        thread_id: str,
        turn_id: str,
        status: str,
        *,
        error: Any = None,
    ) -> None:
        self.thread_id = thread_id
        self.turn_id = turn_id
        self.status = status
        self.error = error
        super().__init__(f"turn {turn_id} ended with status {status}")


class TurnInProgress(AdapterError):
    def __init__(self, thread_id: str, turn_id: str) -> None:
        self.thread_id = thread_id
        self.turn_id = turn_id
        super().__init__(f"turn {turn_id} is still in progress")


class ReadbackMissingResult(AdapterError):
    def __init__(self, thread_id: str, turn_id: str, reason: str) -> None:
        self.thread_id = thread_id
        self.turn_id = turn_id
        self.reason = reason
        super().__init__(f"authoritative readback for turn {turn_id} is incomplete: {reason}")


@dataclass(frozen=True, slots=True)
class ThreadOptions:
    sandbox: SandboxMode = "workspace-write"
    # With no interactive approval bridge in v0, ``never`` is fail-closed:
    # Codex stays in workspace-write and cannot pause on an unhandled prompt.
    approval_policy: ApprovalPolicy = "never"
    model: str | None = None
    developer_instructions: str | None = None
    service_tier: str | None = None


@dataclass(frozen=True, slots=True)
class TurnOptions:
    model: str | None = None
    effort: ReasoningEffort | None = None
    approval_policy: ApprovalPolicy | None = None
    service_tier: str | None = None


@dataclass(frozen=True, slots=True)
class ThreadHandle:
    thread_id: str
    workspace: Path


@dataclass(frozen=True, slots=True)
class TurnHandle:
    thread_id: str
    turn_id: str


@dataclass(frozen=True, slots=True)
class TurnReadback:
    thread_id: str
    turn_id: str
    status: TurnTerminalStatus
    final_text: str
    items: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True, slots=True)
class ThreadSummary:
    thread_id: str
    cwd: Path
    preview: str
    status: str
    created_at: int
    updated_at: int
    turn_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ThreadPage:
    threads: tuple[ThreadSummary, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class CatalogUserMessage:
    client_message_id: str | None
    input_sha256: str


@dataclass(frozen=True, slots=True)
class CatalogTurn:
    turn_id: str
    status: str
    final_text: str | None
    user_messages: tuple[CatalogUserMessage, ...] = ()


@dataclass(frozen=True, slots=True)
class ThreadCatalogReadback:
    thread: ThreadSummary
    turns: tuple[CatalogTurn, ...]


@dataclass(frozen=True, slots=True)
class QueuedSubmission:
    submission_id: str
    client_message_id: str
    input_sha256: str


@dataclass(frozen=True, slots=True)
class QueuePage:
    submissions: tuple[QueuedSubmission, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class LoadedThreadPage:
    thread_ids: tuple[str, ...]
    next_cursor: str | None


@runtime_checkable
class CodexAdapter(Protocol):
    async def list_threads(
        self, *, cursor: str | None = None, limit: int = 50
    ) -> ThreadPage: ...

    async def read_thread(self, thread_id: str) -> ThreadCatalogReadback: ...

    async def list_loaded_threads(
        self, *, cursor: str | None = None, limit: int = 100
    ) -> LoadedThreadPage: ...

    async def list_queue(
        self, thread_id: str, *, cursor: str | None = None, limit: int = 100
    ) -> QueuePage: ...

    async def add_queue(
        self, thread_id: str, prompt: str, client_message_id: str
    ) -> QueuedSubmission: ...

    async def start_queued_turn(
        self, thread_id: str, submission_id: str
    ) -> TurnHandle: ...

    async def start_thread(
        self,
        workspace: Path,
        options: ThreadOptions | None = None,
    ) -> ThreadHandle: ...

    async def resume_thread(
        self,
        thread_id: str,
        workspace: Path,
        options: ThreadOptions | None = None,
    ) -> ThreadHandle: ...

    async def start_turn(
        self,
        thread_id: str,
        prompt: str,
        options: TurnOptions | None = None,
    ) -> TurnHandle: ...

    async def wait_for_completion(
        self,
        thread_id: str,
        turn_id: str,
        timeout: float,
    ) -> TurnTerminalStatus: ...

    async def read_turn(self, thread_id: str, turn_id: str) -> TurnReadback: ...

    async def interrupt(self, thread_id: str, turn_id: str) -> None: ...

    async def close(self) -> None: ...


def validate_command(command: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(command)
    if not normalized or any(not isinstance(part, str) or not part for part in normalized):
        raise ValueError("App Server command must contain non-empty strings")
    return normalized
