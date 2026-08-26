"""Versioned, privacy-conscious domain objects for Hermes Codex Operator.

The public protocol is deliberately small.  Parsing is strict so a caller
cannot silently opt in to behavior introduced by a newer contract version.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "1"
MAX_ID_LENGTH = 128
MAX_WORKSPACE_LENGTH = 4096
MAX_PROMPT_LENGTH = 262_144
MAX_GLOBS = 128
MAX_GLOB_LENGTH = 512
MAX_VERIFICATION_COMMANDS = 32
MAX_ARGV_ITEMS = 128
MAX_ARG_LENGTH = 4096
MAX_TIMEOUT_SECONDS = 86_400
REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh"})

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*\Z")
_SETTING_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]*\Z")
_FAILURE_CODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class ContractValidationError(ValueError):
    """The caller supplied an invalid or unsupported protocol object."""


class OperationState(StrEnum):
    QUEUED = "QUEUED"
    STARTING_THREAD = "STARTING_THREAD"
    READY = "READY"
    DISPATCHING = "DISPATCHING"
    QUEUE_DISPATCH_IN_FLIGHT = "QUEUE_DISPATCH_IN_FLIGHT"
    RUNNING = "RUNNING"
    TURN_COMPLETED = "TURN_COMPLETED"
    VERIFYING = "VERIFYING"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    UNKNOWN = "UNKNOWN"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    SCOPE_VIOLATION = "SCOPE_VIOLATION"


ACTIVE_STATES = frozenset(
    {
        OperationState.QUEUED,
        OperationState.STARTING_THREAD,
        OperationState.READY,
        OperationState.DISPATCHING,
        OperationState.QUEUE_DISPATCH_IN_FLIGHT,
        OperationState.RUNNING,
        OperationState.TURN_COMPLETED,
        OperationState.VERIFYING,
    }
)

INTERRUPTION_STATES = frozenset(
    {
        OperationState.FAILED,
        OperationState.CANCELLED,
        OperationState.TIMED_OUT,
        OperationState.UNKNOWN,
    }
)

DELIVERABLE_STATES = frozenset(
    {
        OperationState.VERIFIED,
        OperationState.FAILED,
        OperationState.CANCELLED,
        OperationState.TIMED_OUT,
        OperationState.UNKNOWN,
        OperationState.VERIFICATION_FAILED,
        OperationState.SCOPE_VIOLATION,
    }
)

_NORMAL_TRANSITIONS: dict[OperationState, frozenset[OperationState]] = {
    OperationState.QUEUED: frozenset(
        {OperationState.STARTING_THREAD, OperationState.READY}
    ),
    OperationState.STARTING_THREAD: frozenset({OperationState.READY}),
    OperationState.READY: frozenset(
        {OperationState.DISPATCHING, OperationState.QUEUE_DISPATCH_IN_FLIGHT}
    ),
    OperationState.QUEUE_DISPATCH_IN_FLIGHT: frozenset({OperationState.RUNNING}),
    OperationState.DISPATCHING: frozenset({OperationState.RUNNING}),
    OperationState.RUNNING: frozenset({OperationState.TURN_COMPLETED}),
    OperationState.TURN_COMPLETED: frozenset({OperationState.VERIFYING}),
    OperationState.VERIFYING: frozenset(
        {
            OperationState.VERIFIED,
            OperationState.VERIFICATION_FAILED,
            OperationState.SCOPE_VIOLATION,
        }
    ),
}


def allowed_transitions(state: OperationState | str) -> frozenset[OperationState]:
    """Return ordinary lifecycle transitions.

    ``UNKNOWN`` and ``TIMED_OUT`` intentionally have no ordinary outgoing
    transitions.  They can only be changed through the journal's explicit
    reconciliation operation after exact thread and turn identities exist.
    """

    current = parse_state(state)
    normal = _NORMAL_TRANSITIONS.get(current, frozenset())
    if current in ACTIVE_STATES:
        return normal | INTERRUPTION_STATES
    return normal


def parse_state(value: OperationState | str) -> OperationState:
    if isinstance(value, OperationState):
        return value
    if not isinstance(value, str):
        raise ContractValidationError("operation state must be a string")
    try:
        return OperationState(value)
    except ValueError as exc:
        raise ContractValidationError("unknown operation state") from exc


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractValidationError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _load_json_object(raw: str | bytes | bytearray) -> dict[str, Any]:
    if not isinstance(raw, (str, bytes, bytearray)):
        raise ContractValidationError("JSON input must be text or bytes")
    try:
        value = json.loads(raw, object_pairs_hook=_strict_object)
    except ContractValidationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractValidationError("invalid JSON") from exc
    if not isinstance(value, dict):
        raise ContractValidationError("protocol object must be a JSON object")
    return value


def _require_exact_fields(
    value: Mapping[str, Any], *, required: frozenset[str], optional: frozenset[str]
) -> None:
    if not isinstance(value, Mapping):
        raise ContractValidationError("protocol value must be an object")
    keys = set(value)
    unknown = keys - required - optional
    missing = required - keys
    if unknown:
        raise ContractValidationError(
            "unknown field(s): " + ", ".join(sorted(str(item) for item in unknown))
        )
    if missing:
        raise ContractValidationError(
            "missing field(s): " + ", ".join(sorted(missing))
        )


def _validate_schema_version(value: Any) -> str:
    if value != SCHEMA_VERSION:
        raise ContractValidationError(
            f"unsupported schema_version; expected {SCHEMA_VERSION!r}"
        )
    return SCHEMA_VERSION


def _validate_id(value: Any, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_ID_LENGTH
        or _ID_RE.fullmatch(value) is None
    ):
        raise ContractValidationError(
            f"{name} must be 1-{MAX_ID_LENGTH} safe ASCII characters"
        )
    return value


def validate_external_id(value: Any, name: str) -> str:
    """Validate an opaque App Server identifier without logging its value."""

    if not isinstance(value, str) or not value or len(value) > 512:
        raise ContractValidationError(f"{name} must be a non-empty string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ContractValidationError(f"{name} contains control characters")
    return value


def _validate_workspace(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_WORKSPACE_LENGTH
        or "\x00" in value
        or not Path(value).is_absolute()
    ):
        raise ContractValidationError("workspace must be a bounded absolute path")
    return value


def _validate_prompt(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractValidationError("prompt must be a non-empty string")
    if len(value) > MAX_PROMPT_LENGTH or "\x00" in value:
        raise ContractValidationError("prompt exceeds its size limit or contains NUL")
    return value


def _validate_glob(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_GLOB_LENGTH
        or "\x00" in value
        or "\\" in value
        or value.startswith("/")
    ):
        raise ContractValidationError("change globs must be bounded POSIX-relative patterns")
    parts = PurePosixPath(value).parts
    if ".." in parts:
        raise ContractValidationError("change globs must not contain parent traversal")
    return value


def _parse_globs(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ContractValidationError("allowed_change_globs must be a JSON array")
    if not value:
        raise ContractValidationError("allowed_change_globs must contain at least one pattern")
    if len(value) > MAX_GLOBS:
        raise ContractValidationError("too many allowed_change_globs")
    result = tuple(_validate_glob(item) for item in value)
    if len(set(result)) != len(result):
        raise ContractValidationError("allowed_change_globs contains duplicates")
    return result


def _parse_verification(value: Any) -> tuple[tuple[str, ...], ...]:
    if not isinstance(value, list):
        raise ContractValidationError("verification must be an array of argv arrays")
    if not value:
        raise ContractValidationError("verification must contain at least one command")
    if len(value) > MAX_VERIFICATION_COMMANDS:
        raise ContractValidationError("too many verification commands")
    commands: list[tuple[str, ...]] = []
    for command in value:
        if not isinstance(command, list) or not command:
            raise ContractValidationError("each verification command must be a non-empty argv array")
        if len(command) > MAX_ARGV_ITEMS:
            raise ContractValidationError("verification argv has too many items")
        argv: list[str] = []
        for item in command:
            if (
                not isinstance(item, str)
                or not item
                or len(item) > MAX_ARG_LENGTH
                or "\x00" in item
            ):
                raise ContractValidationError(
                    "verification argv items must be bounded non-empty strings"
                )
            argv.append(item)
        commands.append(tuple(argv))
    return tuple(commands)


def _validate_timeout(value: Any) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
        or value > MAX_TIMEOUT_SECONDS
    ):
        raise ContractValidationError(
            f"timeout_seconds must be an integer from 1 to {MAX_TIMEOUT_SECONDS}"
        )
    return value


def _validate_setting(value: Any, name: str, maximum: int) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or _SETTING_RE.fullmatch(value) is None
    ):
        raise ContractValidationError(f"{name} is invalid")
    return value


def _validate_reasoning_effort(value: Any) -> str | None:
    effort = _validate_setting(value, "reasoning_effort", 64)
    if effort is not None and effort not in REASONING_EFFORTS:
        raise ContractValidationError("reasoning_effort is unsupported by protocol v1")
    return effort


@dataclass(frozen=True, slots=True)
class WorkContract:
    schema_version: str
    task_id: str
    operation_id: str
    workspace: str
    prompt: str = field(repr=False)
    allowed_change_globs: tuple[str, ...]
    verification: tuple[tuple[str, ...], ...]
    timeout_seconds: int
    model: str | None = None
    reasoning_effort: str | None = None

    def __post_init__(self) -> None:
        _validate_schema_version(self.schema_version)
        _validate_id(self.task_id, "task_id")
        _validate_id(self.operation_id, "operation_id")
        _validate_workspace(self.workspace)
        _validate_prompt(self.prompt)
        if not isinstance(self.allowed_change_globs, tuple):
            raise ContractValidationError("allowed_change_globs must be an immutable tuple")
        if not self.allowed_change_globs:
            raise ContractValidationError(
                "allowed_change_globs must contain at least one pattern"
            )
        if len(self.allowed_change_globs) > MAX_GLOBS:
            raise ContractValidationError("too many allowed_change_globs")
        for item in self.allowed_change_globs:
            _validate_glob(item)
        if len(set(self.allowed_change_globs)) != len(self.allowed_change_globs):
            raise ContractValidationError("allowed_change_globs contains duplicates")
        if not isinstance(self.verification, tuple):
            raise ContractValidationError("verification must be an immutable tuple")
        if not self.verification:
            raise ContractValidationError("verification must contain at least one command")
        if len(self.verification) > MAX_VERIFICATION_COMMANDS:
            raise ContractValidationError("too many verification commands")
        for command in self.verification:
            if not isinstance(command, tuple) or not command or len(command) > MAX_ARGV_ITEMS:
                raise ContractValidationError("verification commands must be argv tuples")
            for item in command:
                if (
                    not isinstance(item, str)
                    or not item
                    or len(item) > MAX_ARG_LENGTH
                    or "\x00" in item
                ):
                    raise ContractValidationError(
                        "verification argv items must be bounded non-empty strings"
                    )
        _validate_timeout(self.timeout_seconds)
        _validate_setting(self.model, "model", 128)
        _validate_reasoning_effort(self.reasoning_effort)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WorkContract":
        required = frozenset(
            {
                "schema_version",
                "task_id",
                "operation_id",
                "workspace",
                "prompt",
                "allowed_change_globs",
                "verification",
                "timeout_seconds",
            }
        )
        optional = frozenset({"model", "reasoning_effort"})
        _require_exact_fields(value, required=required, optional=optional)
        return cls(
            schema_version=_validate_schema_version(value["schema_version"]),
            task_id=_validate_id(value["task_id"], "task_id"),
            operation_id=_validate_id(value["operation_id"], "operation_id"),
            workspace=_validate_workspace(value["workspace"]),
            prompt=_validate_prompt(value["prompt"]),
            allowed_change_globs=_parse_globs(value["allowed_change_globs"]),
            verification=_parse_verification(value["verification"]),
            timeout_seconds=_validate_timeout(value["timeout_seconds"]),
            model=_validate_setting(value.get("model"), "model", 128),
            reasoning_effort=_validate_reasoning_effort(value.get("reasoning_effort")),
        )

    @classmethod
    def from_json(cls, raw: str | bytes | bytearray) -> "WorkContract":
        return cls.from_dict(_load_json_object(raw))

    @property
    def prompt_sha256(self) -> str:
        return sha256_text(self.prompt)

    def to_dict(self, *, include_prompt: bool = True) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "operation_id": self.operation_id,
            "workspace": self.workspace,
            "allowed_change_globs": list(self.allowed_change_globs),
            "verification": [list(command) for command in self.verification],
            "timeout_seconds": self.timeout_seconds,
        }
        if include_prompt:
            value["prompt"] = self.prompt
        else:
            value["prompt_sha256"] = self.prompt_sha256
        if self.model is not None:
            value["model"] = self.model
        if self.reasoning_effort is not None:
            value["reasoning_effort"] = self.reasoning_effort
        return value

    def persistence_dict(self) -> dict[str, Any]:
        """Return journal-safe metadata; the original prompt is never included."""

        return self.to_dict(include_prompt=False)


def _validate_sha256(value: Any, name: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ContractValidationError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _validate_relative_path(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_WORKSPACE_LENGTH
        or "\x00" in value
        or "\\" in value
        or value.startswith("/")
        or ".." in PurePosixPath(value).parts
    ):
        raise ContractValidationError("receipt paths must be bounded POSIX-relative paths")
    return value


def _validate_timestamp(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ContractValidationError(f"{name} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractValidationError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ContractValidationError(f"{name} must include a timezone")
    return value


def _validate_failure_code(value: Any) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) > 128
        or _FAILURE_CODE_RE.fullmatch(value) is None
    ):
        raise ContractValidationError("failure_code is invalid")
    return value


@dataclass(frozen=True, slots=True)
class ChangedPathEvidence:
    path: str
    sha256: str | None

    def __post_init__(self) -> None:
        _validate_relative_path(self.path)
        _validate_sha256(self.sha256, "sha256", optional=True)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ChangedPathEvidence":
        _require_exact_fields(
            value,
            required=frozenset({"path", "sha256"}),
            optional=frozenset(),
        )
        return cls(
            path=_validate_relative_path(value["path"]),
            sha256=_validate_sha256(value["sha256"], "sha256", optional=True),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class VerificationEvidence:
    command_sha256: str
    exit_code: int | None
    duration_ms: int
    output_sha256: str

    def __post_init__(self) -> None:
        _validate_sha256(self.command_sha256, "command_sha256")
        if self.exit_code is not None and (
            isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int)
        ):
            raise ContractValidationError("verification exit_code must be an integer or null")
        if (
            isinstance(self.duration_ms, bool)
            or not isinstance(self.duration_ms, int)
            or self.duration_ms < 0
        ):
            raise ContractValidationError("verification duration_ms must be non-negative")
        _validate_sha256(self.output_sha256, "output_sha256")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "VerificationEvidence":
        _require_exact_fields(
            value,
            required=frozenset(
                {"command_sha256", "exit_code", "duration_ms", "output_sha256"}
            ),
            optional=frozenset(),
        )
        exit_code = value["exit_code"]
        if exit_code is not None and (
            isinstance(exit_code, bool) or not isinstance(exit_code, int)
        ):
            raise ContractValidationError("verification exit_code must be an integer or null")
        duration_ms = value["duration_ms"]
        if (
            isinstance(duration_ms, bool)
            or not isinstance(duration_ms, int)
            or duration_ms < 0
        ):
            raise ContractValidationError("verification duration_ms must be non-negative")
        command_digest = _validate_sha256(value["command_sha256"], "command_sha256")
        output_digest = _validate_sha256(value["output_sha256"], "output_sha256")
        assert command_digest is not None and output_digest is not None
        return cls(
            command_sha256=command_digest,
            exit_code=exit_code,
            duration_ms=duration_ms,
            output_sha256=output_digest,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_sha256": self.command_sha256,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "output_sha256": self.output_sha256,
        }


@dataclass(frozen=True, slots=True)
class Receipt:
    schema_version: str
    task_id: str
    operation_id: str
    status: OperationState
    prompt_sha256: str
    workspace_sha256: str
    thread_sha256: str | None
    turn_sha256: str | None
    result_sha256: str | None
    changed_paths: tuple[ChangedPathEvidence, ...]
    verification: tuple[VerificationEvidence, ...]
    started_at: str
    completed_at: str
    failure_code: str | None = None

    def __post_init__(self) -> None:
        _validate_schema_version(self.schema_version)
        _validate_id(self.task_id, "task_id")
        _validate_id(self.operation_id, "operation_id")
        state = parse_state(self.status)
        object.__setattr__(self, "status", state)
        if state not in DELIVERABLE_STATES:
            raise ContractValidationError("receipt status is not deliverable")
        _validate_sha256(self.prompt_sha256, "prompt_sha256")
        _validate_sha256(self.workspace_sha256, "workspace_sha256")
        _validate_sha256(self.thread_sha256, "thread_sha256", optional=True)
        _validate_sha256(self.turn_sha256, "turn_sha256", optional=True)
        _validate_sha256(self.result_sha256, "result_sha256", optional=True)
        if not isinstance(self.changed_paths, tuple) or len(self.changed_paths) > 20_000:
            raise ContractValidationError("changed_paths must be a bounded tuple")
        if any(not isinstance(item, ChangedPathEvidence) for item in self.changed_paths):
            raise ContractValidationError("changed_paths contains invalid evidence")
        paths = tuple(item.path for item in self.changed_paths)
        if len(set(paths)) != len(paths):
            raise ContractValidationError("changed_paths contains duplicates")
        if (
            not isinstance(self.verification, tuple)
            or len(self.verification) > MAX_VERIFICATION_COMMANDS
            or any(not isinstance(item, VerificationEvidence) for item in self.verification)
        ):
            raise ContractValidationError("verification contains invalid evidence")
        if state == OperationState.VERIFIED:
            if not self.verification or any(
                item.exit_code != 0 for item in self.verification
            ):
                raise ContractValidationError(
                    "VERIFIED receipts require at least one successful verification"
                )
            if self.failure_code is not None:
                raise ContractValidationError("VERIFIED receipts cannot have failure_code")
            if self.result_sha256 is None:
                raise ContractValidationError(
                    "VERIFIED receipts require an authoritative result digest"
                )
        _validate_timestamp(self.started_at, "started_at")
        _validate_timestamp(self.completed_at, "completed_at")
        _validate_failure_code(self.failure_code)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Receipt":
        required = frozenset(
            {
                "schema_version",
                "task_id",
                "operation_id",
                "status",
                "prompt_sha256",
                "workspace_sha256",
                "thread_sha256",
                "turn_sha256",
                "result_sha256",
                "changed_paths",
                "verification",
                "started_at",
                "completed_at",
                "failure_code",
            }
        )
        _require_exact_fields(value, required=required, optional=frozenset())
        status = parse_state(value["status"])
        if status not in DELIVERABLE_STATES:
            raise ContractValidationError("receipt status is not deliverable")
        changed = value["changed_paths"]
        checks = value["verification"]
        if not isinstance(changed, list) or len(changed) > 20_000:
            raise ContractValidationError("changed_paths must be a bounded array")
        if not isinstance(checks, list) or len(checks) > MAX_VERIFICATION_COMMANDS:
            raise ContractValidationError("verification evidence must be a bounded array")
        prompt_digest = _validate_sha256(value["prompt_sha256"], "prompt_sha256")
        workspace_digest = _validate_sha256(value["workspace_sha256"], "workspace_sha256")
        assert prompt_digest is not None and workspace_digest is not None
        return cls(
            schema_version=_validate_schema_version(value["schema_version"]),
            task_id=_validate_id(value["task_id"], "task_id"),
            operation_id=_validate_id(value["operation_id"], "operation_id"),
            status=status,
            prompt_sha256=prompt_digest,
            workspace_sha256=workspace_digest,
            thread_sha256=_validate_sha256(
                value["thread_sha256"], "thread_sha256", optional=True
            ),
            turn_sha256=_validate_sha256(
                value["turn_sha256"], "turn_sha256", optional=True
            ),
            result_sha256=_validate_sha256(
                value["result_sha256"], "result_sha256", optional=True
            ),
            changed_paths=tuple(ChangedPathEvidence.from_dict(item) for item in changed),
            verification=tuple(VerificationEvidence.from_dict(item) for item in checks),
            started_at=_validate_timestamp(value["started_at"], "started_at"),
            completed_at=_validate_timestamp(value["completed_at"], "completed_at"),
            failure_code=_validate_failure_code(value["failure_code"]),
        )

    @classmethod
    def from_json(cls, raw: str | bytes | bytearray) -> "Receipt":
        return cls.from_dict(_load_json_object(raw))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "operation_id": self.operation_id,
            "status": self.status.value,
            "prompt_sha256": self.prompt_sha256,
            "workspace_sha256": self.workspace_sha256,
            "thread_sha256": self.thread_sha256,
            "turn_sha256": self.turn_sha256,
            "result_sha256": self.result_sha256,
            "changed_paths": [item.to_dict() for item in self.changed_paths],
            "verification": [item.to_dict() for item in self.verification],
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "failure_code": self.failure_code,
        }

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @property
    def sha256(self) -> str:
        return sha256_text(self.to_json())


__all__ = [
    "ACTIVE_STATES",
    "ChangedPathEvidence",
    "ContractValidationError",
    "DELIVERABLE_STATES",
    "INTERRUPTION_STATES",
    "OperationState",
    "Receipt",
    "REASONING_EFFORTS",
    "SCHEMA_VERSION",
    "VerificationEvidence",
    "WorkContract",
    "allowed_transitions",
    "canonical_json",
    "parse_state",
    "sha256_text",
    "validate_external_id",
]
