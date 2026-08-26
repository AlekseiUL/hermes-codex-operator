"""Durable SQLite lifecycle journal.

The journal is the local source of truth for idempotency and dispatch state.
It never receives or persists the original prompt: only its SHA-256 digest and
the remaining bounded contract metadata are written to SQLite.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import threading
from typing import Any, Iterator, Mapping

from .models import (
    ACTIVE_STATES,
    ContractValidationError,
    OperationState,
    Receipt,
    WorkContract,
    allowed_transitions,
    canonical_json,
    parse_state,
    sha256_text,
    validate_external_id,
)
from .workspace import WorkspaceSnapshot, snapshot_from_dict, snapshot_to_dict


DATABASE_VERSION = 5
DISPATCH_KINDS = frozenset({"ordinary", "continue", "queue"})
WATCH_STATES = frozenset({"ACTIVE", "COMPLETED", "FAILED", "INTERRUPTED", "UNKNOWN"})
WATCH_TERMINAL_STATES = WATCH_STATES - {"ACTIVE"}


class JournalError(RuntimeError):
    """Base class for journal failures that are safe to expose to callers."""


class JournalClosed(JournalError):
    pass


class RecordNotFound(JournalError):
    pass


class IdempotencyConflict(JournalError):
    pass


class TaskBusy(JournalError):
    """Another operation may still own the task's Codex thread."""


class InvalidTransition(JournalError):
    pass


class BindingConflict(JournalError):
    pass


class ReceiptConflict(JournalError):
    pass


class BaselineConflict(JournalError):
    pass


_RECEIPT_FINAL_STATES = frozenset(
    {
        OperationState.VERIFIED,
        OperationState.FAILED,
        OperationState.CANCELLED,
        OperationState.VERIFICATION_FAILED,
        OperationState.SCOPE_VIOLATION,
    }
)


@dataclass(frozen=True, slots=True)
class TaskRecord:
    task_id: str
    workspace: str
    thread_id: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class OperationRecord:
    task_id: str
    operation_id: str
    prompt_sha256: str
    contract_sha256: str
    dispatch_kind: str
    state: OperationState
    turn_id: str | None
    failure_code: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class Reservation:
    created: bool
    task: TaskRecord
    operation: OperationRecord


@dataclass(frozen=True, slots=True)
class Finalization:
    operation: OperationRecord
    receipt: Receipt


@dataclass(frozen=True, slots=True)
class WatchRecord:
    watch_id: str
    thread_id: str
    turn_id: str
    state: str
    result_sha256: str | None
    failure_code: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class CompletionEvent:
    event_id: str
    watch_id: str
    thread_id: str
    turn_id: str
    status: str
    result_sha256: str | None
    created_at: str
    acked_at: str | None


@dataclass(frozen=True, slots=True)
class ContinuationBaseline:
    operation_id: str
    thread_id: str
    turn_ids: tuple[str, ...]
    turn_ids_sha256: str
    created_at: str


@dataclass(frozen=True, slots=True)
class QueueBaseline:
    operation_id: str
    thread_id: str
    message_sha256: str
    turn_ids: tuple[str, ...]
    turn_ids_sha256: str
    submission_id: str | None
    created_at: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _failure_code(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 128:
        raise JournalError("failure_code must be a bounded non-empty string")
    if not all(character.isalnum() or character in "._:-" for character in value):
        raise JournalError("failure_code contains unsupported characters")
    return value


class SQLiteJournal:
    """Thread-safe SQLite journal with process-safe write transactions.

    Separate ``SQLiteJournal`` instances may point at the same database.  Every
    mutation uses ``BEGIN IMMEDIATE`` so duplicate reservations and bindings
    are decided atomically across both threads and processes.
    """

    def __init__(self, path: str | os.PathLike[str], *, timeout_seconds: float = 5.0):
        if isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        raw_path = os.fspath(path)
        if not raw_path:
            raise ValueError("journal path must not be empty")
        self._memory = raw_path == ":memory:"
        self._path = Path(raw_path) if not self._memory else None
        self._lock = threading.RLock()
        self._memory_operation_locks: dict[str, threading.Lock] = {}
        self._closed = False

        if self._path is not None:
            self._prepare_file(self._path)

        self._connection = sqlite3.connect(
            raw_path,
            timeout=float(timeout_seconds),
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        try:
            self._configure(timeout_seconds)
            self._initialize_schema()
            self._tighten_permissions()
        except BaseException:
            self._connection.close()
            self._closed = True
            raise

    @property
    def path(self) -> Path | None:
        return self._path

    def _prepare_file(self, path: Path) -> None:
        parent = path.parent
        if not parent.exists():
            parent.mkdir(mode=0o700, parents=True)
        if not parent.is_dir():
            raise JournalError("journal parent is not a directory")
        if path.is_symlink():
            raise JournalError("journal path must not be a symlink")
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags, 0o600)
        except OSError as exc:
            raise JournalError("cannot safely create or open journal") from exc
        else:
            os.close(descriptor)
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            # Some filesystems do not implement POSIX permissions.  The mode is
            # a best-effort hardening measure, not an integrity assumption.
            pass

    def _configure(self, timeout_seconds: float) -> None:
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute(
            f"PRAGMA busy_timeout = {max(1, round(timeout_seconds * 1000))}"
        )
        if not self._memory:
            mode = self._connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                raise JournalError("SQLite WAL mode is unavailable")
        self._connection.execute("PRAGMA synchronous = FULL")

    def _initialize_schema(self) -> None:
        with self._transaction():
            version = int(
                self._connection.execute("PRAGMA user_version").fetchone()[0]
            )
            if version not in (0, 1, 2, 3, 4, DATABASE_VERSION):
                raise JournalError("unsupported journal schema version")
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    workspace TEXT NOT NULL,
                    thread_id TEXT UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    prompt_sha256 TEXT NOT NULL CHECK(length(prompt_sha256) = 64),
                    contract_sha256 TEXT NOT NULL CHECK(length(contract_sha256) = 64),
                    contract_json TEXT NOT NULL,
                    dispatch_kind TEXT NOT NULL DEFAULT 'ordinary'
                        CHECK(dispatch_kind IN ('ordinary', 'continue', 'queue')),
                    state TEXT NOT NULL,
                    turn_id TEXT UNIQUE,
                    failure_code TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            operation_columns = {
                row[1]
                for row in self._connection.execute(
                    "PRAGMA table_info(operations)"
                ).fetchall()
            }
            if "dispatch_kind" not in operation_columns:
                self._connection.execute(
                    """
                    ALTER TABLE operations ADD COLUMN dispatch_kind TEXT NOT NULL
                    DEFAULT 'ordinary'
                    CHECK(dispatch_kind IN ('ordinary', 'continue', 'queue'))
                    """
                )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS receipts (
                    operation_id TEXT PRIMARY KEY REFERENCES operations(operation_id),
                    receipt_sha256 TEXT NOT NULL CHECK(length(receipt_sha256) = 64),
                    receipt_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS baselines (
                    operation_id TEXT PRIMARY KEY REFERENCES operations(operation_id),
                    snapshot_sha256 TEXT NOT NULL CHECK(length(snapshot_sha256) = 64),
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS receipts_are_immutable_on_update
                BEFORE UPDATE ON receipts
                BEGIN
                    SELECT RAISE(ABORT, 'receipts are immutable');
                END
                """
            )
            self._connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS receipts_are_immutable_on_delete
                BEFORE DELETE ON receipts
                BEGIN
                    SELECT RAISE(ABORT, 'receipts are immutable');
                END
                """
            )
            self._connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS baselines_are_immutable_on_update
                BEFORE UPDATE ON baselines
                BEGIN
                    SELECT RAISE(ABORT, 'baselines are immutable');
                END
                """
            )
            self._connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS baselines_are_immutable_on_delete
                BEFORE DELETE ON baselines
                BEGIN
                    SELECT RAISE(ABORT, 'baselines are immutable');
                END
                """
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS operations_task_id_idx ON operations(task_id)"
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS watches (
                    watch_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    result_sha256 TEXT,
                    failure_code TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(thread_id, turn_id)
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS completion_events (
                    event_id TEXT PRIMARY KEY,
                    watch_id TEXT NOT NULL UNIQUE REFERENCES watches(watch_id),
                    thread_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_sha256 TEXT,
                    created_at TEXT NOT NULL,
                    acked_at TEXT
                )
                """
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS completion_events_ack_idx "
                "ON completion_events(acked_at, created_at)"
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS continuation_baselines (
                    operation_id TEXT PRIMARY KEY REFERENCES operations(operation_id),
                    thread_id TEXT NOT NULL,
                    turn_ids_sha256 TEXT NOT NULL CHECK(length(turn_ids_sha256) = 64),
                    turn_ids_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS continuation_baselines_are_immutable_on_update
                BEFORE UPDATE ON continuation_baselines
                BEGIN
                    SELECT RAISE(ABORT, 'continuation baselines are immutable');
                END
                """
            )
            self._connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS continuation_baselines_are_immutable_on_delete
                BEFORE DELETE ON continuation_baselines
                BEGIN
                    SELECT RAISE(ABORT, 'continuation baselines are immutable');
                END
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS queue_baselines (
                    operation_id TEXT PRIMARY KEY REFERENCES operations(operation_id),
                    thread_id TEXT NOT NULL,
                    message_sha256 TEXT NOT NULL CHECK(length(message_sha256) = 64),
                    turn_ids_sha256 TEXT NOT NULL CHECK(length(turn_ids_sha256) = 64),
                    turn_ids_json TEXT NOT NULL,
                    submission_id TEXT UNIQUE,
                    created_at TEXT NOT NULL
                )
                """
            )
            if version in (0, 1, 2, 3, 4):
                self._connection.execute(
                    """
                    UPDATE operations SET dispatch_kind = 'continue'
                    WHERE operation_id IN (
                        SELECT operation_id FROM continuation_baselines
                    )
                    """
                )
                self._connection.execute(
                    """
                    UPDATE operations SET dispatch_kind = 'queue'
                    WHERE operation_id IN (
                        SELECT operation_id FROM queue_baselines
                    )
                    """
                )
            self._connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS operations_dispatch_kind_is_immutable
                BEFORE UPDATE OF dispatch_kind ON operations
                WHEN OLD.dispatch_kind != NEW.dispatch_kind
                BEGIN
                    SELECT RAISE(ABORT, 'operation dispatch kind is immutable');
                END
                """
            )
            self._connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS queue_baselines_identity_is_immutable
                BEFORE UPDATE ON queue_baselines
                WHEN OLD.operation_id != NEW.operation_id
                  OR OLD.thread_id != NEW.thread_id
                  OR OLD.message_sha256 != NEW.message_sha256
                  OR OLD.turn_ids_sha256 != NEW.turn_ids_sha256
                  OR OLD.turn_ids_json != NEW.turn_ids_json
                  OR (OLD.submission_id IS NOT NULL AND OLD.submission_id != NEW.submission_id)
                BEGIN
                    SELECT RAISE(ABORT, 'queue baseline identity is immutable');
                END
                """
            )
            self._connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS queue_baselines_are_immutable_on_delete
                BEFORE DELETE ON queue_baselines
                BEGIN
                    SELECT RAISE(ABORT, 'queue baselines are immutable');
                END
                """
            )
            try:
                self._connection.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS one_open_operation_per_task_idx
                    ON operations(task_id)
                    WHERE state IN (
                        'QUEUED', 'STARTING_THREAD', 'READY', 'DISPATCHING',
                        'QUEUE_DISPATCH_IN_FLIGHT',
                        'RUNNING', 'TURN_COMPLETED', 'VERIFYING', 'TIMED_OUT',
                        'UNKNOWN'
                    )
                    """
                )
            except sqlite3.IntegrityError as exc:
                raise JournalError(
                    "journal already contains multiple open operations for one task"
                ) from exc
            if version in (0, 1, 2, 3, 4):
                self._connection.execute(f"PRAGMA user_version = {DATABASE_VERSION}")

    def _tighten_permissions(self) -> None:
        if self._path is None:
            return
        for candidate in (
            self._path,
            Path(f"{self._path}-wal"),
            Path(f"{self._path}-shm"),
        ):
            if not candidate.exists() or candidate.is_symlink():
                continue
            try:
                os.chmod(candidate, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass

    def _ensure_open(self) -> None:
        if self._closed:
            raise JournalClosed("journal is closed")

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            self._ensure_open()
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()
                self._tighten_permissions()

    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> TaskRecord:
        return TaskRecord(
            task_id=row["task_id"],
            workspace=row["workspace"],
            thread_id=row["thread_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _operation_from_row(row: sqlite3.Row) -> OperationRecord:
        try:
            state = OperationState(row["state"])
        except ValueError as exc:
            raise JournalError("journal contains an unknown operation state") from exc
        dispatch_kind = row["dispatch_kind"]
        if dispatch_kind not in DISPATCH_KINDS:
            raise JournalError("journal contains an unknown dispatch kind")
        return OperationRecord(
            task_id=row["task_id"],
            operation_id=row["operation_id"],
            prompt_sha256=row["prompt_sha256"],
            contract_sha256=row["contract_sha256"],
            dispatch_kind=dispatch_kind,
            state=state,
            turn_id=row["turn_id"],
            failure_code=row["failure_code"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _select_task(self, task_id: str) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()

    def _select_operation(self, operation_id: str) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
        ).fetchone()

    def reserve_operation(
        self,
        contract: WorkContract,
        *,
        dispatch_kind: str | None = "ordinary",
    ) -> Reservation:
        """Atomically reserve an operation, or return its existing record.

        Reuse is idempotent only when the complete redacted contract matches.
        The raw prompt is neither serialized nor passed to SQLite.
        """

        if not isinstance(contract, WorkContract):
            raise TypeError("contract must be a WorkContract")
        if dispatch_kind is not None and dispatch_kind not in DISPATCH_KINDS:
            raise ValueError("dispatch_kind is invalid")
        requested_kind = dispatch_kind or "ordinary"
        persisted = contract.persistence_dict()
        contract_json = canonical_json(persisted)
        contract_sha256 = sha256_text(contract_json)
        prompt_sha256 = contract.prompt_sha256
        created = False

        with self._transaction():
            operation_row = self._select_operation(contract.operation_id)
            if operation_row is not None:
                if (
                    operation_row["task_id"] != contract.task_id
                    or operation_row["prompt_sha256"] != prompt_sha256
                    or operation_row["contract_sha256"] != contract_sha256
                    or (
                        dispatch_kind is not None
                        and operation_row["dispatch_kind"] != dispatch_kind
                    )
                ):
                    raise IdempotencyConflict(
                        "operation_id is already reserved for different content"
                    )
                task_row = self._select_task(contract.task_id)
                if task_row is None:
                    raise JournalError("operation references a missing task")
                return Reservation(
                    created=False,
                    task=self._task_from_row(task_row),
                    operation=self._operation_from_row(operation_row),
                )

            task_row = self._select_task(contract.task_id)
            now = _utc_now()
            if task_row is None:
                self._connection.execute(
                    """
                    INSERT INTO tasks(task_id, workspace, thread_id, created_at, updated_at)
                    VALUES (?, ?, NULL, ?, ?)
                    """,
                    (contract.task_id, contract.workspace, now, now),
                )
            elif task_row["workspace"] != contract.workspace:
                raise IdempotencyConflict(
                    "task_id is already bound to a different workspace"
                )

            blocking = self._connection.execute(
                """
                SELECT operation_id FROM operations
                WHERE task_id = ? AND state IN (
                    'QUEUED', 'STARTING_THREAD', 'READY', 'DISPATCHING',
                    'QUEUE_DISPATCH_IN_FLIGHT',
                    'RUNNING', 'TURN_COMPLETED', 'VERIFYING', 'TIMED_OUT',
                    'UNKNOWN'
                )
                LIMIT 1
                """,
                (contract.task_id,),
            ).fetchone()
            if blocking is not None:
                raise TaskBusy("task already has an operation that may still be active")

            try:
                self._connection.execute(
                    """
                    INSERT INTO operations(
                        operation_id, task_id, prompt_sha256, contract_sha256,
                        contract_json, dispatch_kind, state, turn_id, failure_code,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                    """,
                    (
                        contract.operation_id,
                        contract.task_id,
                        prompt_sha256,
                        contract_sha256,
                        contract_json,
                        requested_kind,
                        OperationState.QUEUED.value,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                # BEGIN IMMEDIATE normally serializes this path.  Converting a
                # residual constraint race keeps the public failure explicit.
                raise TaskBusy("task operation reservation conflicted") from exc
            created = True
            task_row = self._select_task(contract.task_id)
            operation_row = self._select_operation(contract.operation_id)
            assert task_row is not None and operation_row is not None
            result = Reservation(
                created=created,
                task=self._task_from_row(task_row),
                operation=self._operation_from_row(operation_row),
            )
        return result

    reserve = reserve_operation

    def store_continuation_baseline(
        self, operation_id: str, thread_id: str, turn_ids: tuple[str, ...]
    ) -> ContinuationBaseline:
        """Persist the exact pre-dispatch remote turn set once and immutably."""

        validate_external_id(thread_id, "thread_id")
        if not isinstance(turn_ids, tuple) or len(turn_ids) > 1000:
            raise JournalError("continuation baseline turn set is invalid or oversized")
        for turn_id in turn_ids:
            validate_external_id(turn_id, "turn_id")
        if len(set(turn_ids)) != len(turn_ids):
            raise JournalError("continuation baseline contains duplicate turn ids")
        ordered = tuple(sorted(turn_ids))
        payload = canonical_json({"turn_ids": list(ordered)})
        digest = sha256_text(payload)
        with self._transaction():
            operation = self._select_operation(operation_id)
            if operation is None:
                raise RecordNotFound("operation does not exist")
            task = self._select_task(operation["task_id"])
            if task is None or task["thread_id"] != thread_id:
                raise BindingConflict("continuation thread is not bound to the operation task")
            existing = self._connection.execute(
                "SELECT * FROM continuation_baselines WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["thread_id"] != thread_id
                    or existing["turn_ids_sha256"] != digest
                    or existing["turn_ids_json"] != payload
                ):
                    raise BaselineConflict("continuation baseline is immutable")
                return self._continuation_baseline_from_row(existing)
            now = _utc_now()
            self._connection.execute(
                """
                INSERT INTO continuation_baselines(
                    operation_id, thread_id, turn_ids_sha256, turn_ids_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (operation_id, thread_id, digest, payload, now),
            )
            row = self._connection.execute(
                "SELECT * FROM continuation_baselines WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            assert row is not None
            return self._continuation_baseline_from_row(row)

    @staticmethod
    def _continuation_baseline_from_row(row: sqlite3.Row) -> ContinuationBaseline:
        try:
            value = json.loads(row["turn_ids_json"])
            if not isinstance(value, dict) or set(value) != {"turn_ids"}:
                raise TypeError
            raw_ids = value["turn_ids"]
            if not isinstance(raw_ids, list) or len(raw_ids) > 1000:
                raise TypeError
            turn_ids = tuple(raw_ids)
            for turn_id in turn_ids:
                validate_external_id(turn_id, "turn_id")
            if len(set(turn_ids)) != len(turn_ids) or turn_ids != tuple(sorted(turn_ids)):
                raise TypeError
        except (ContractValidationError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise JournalError("continuation baseline is malformed") from exc
        if canonical_json({"turn_ids": list(turn_ids)}) != row["turn_ids_json"]:
            raise JournalError("continuation baseline encoding is non-canonical")
        if sha256_text(row["turn_ids_json"]) != row["turn_ids_sha256"]:
            raise JournalError("continuation baseline digest mismatch")
        return ContinuationBaseline(
            operation_id=row["operation_id"],
            thread_id=row["thread_id"],
            turn_ids=turn_ids,
            turn_ids_sha256=row["turn_ids_sha256"],
            created_at=row["created_at"],
        )

    def get_continuation_baseline(self, operation_id: str) -> ContinuationBaseline:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT * FROM continuation_baselines WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise RecordNotFound("continuation baseline does not exist")
            return self._continuation_baseline_from_row(row)

    def store_queue_baseline(
        self,
        operation_id: str,
        thread_id: str,
        message_sha256: str,
        turn_ids: tuple[str, ...],
    ) -> QueueBaseline:
        validate_external_id(thread_id, "thread_id")
        if len(message_sha256) != 64 or any(c not in "0123456789abcdef" for c in message_sha256):
            raise JournalError("queue message digest is invalid")
        if not isinstance(turn_ids, tuple) or len(turn_ids) > 1000:
            raise JournalError("queue baseline turn set is invalid or oversized")
        for turn_id in turn_ids:
            validate_external_id(turn_id, "turn_id")
        if len(set(turn_ids)) != len(turn_ids):
            raise JournalError("queue baseline contains duplicate turn ids")
        ordered = tuple(sorted(turn_ids))
        payload = canonical_json({"turn_ids": list(ordered)})
        digest = sha256_text(payload)
        with self._transaction():
            operation = self._select_operation(operation_id)
            if operation is None:
                raise RecordNotFound("operation does not exist")
            task = self._select_task(operation["task_id"])
            if task is None or task["thread_id"] != thread_id:
                raise BindingConflict("queue thread is not bound to the operation task")
            existing = self._connection.execute(
                "SELECT * FROM queue_baselines WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            if existing is not None:
                if (
                    existing["thread_id"] != thread_id
                    or existing["message_sha256"] != message_sha256
                    or existing["turn_ids_sha256"] != digest
                    or existing["turn_ids_json"] != payload
                ):
                    raise BaselineConflict("queue baseline is immutable")
                return self._queue_baseline_from_row(existing)
            now = _utc_now()
            self._connection.execute(
                """
                INSERT INTO queue_baselines(
                    operation_id, thread_id, message_sha256, turn_ids_sha256,
                    turn_ids_json, submission_id, created_at
                ) VALUES (?, ?, ?, ?, ?, NULL, ?)
                """,
                (operation_id, thread_id, message_sha256, digest, payload, now),
            )
            row = self._connection.execute(
                "SELECT * FROM queue_baselines WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            assert row is not None
            return self._queue_baseline_from_row(row)

    def bind_queue_submission(self, operation_id: str, submission_id: str) -> QueueBaseline:
        validate_external_id(submission_id, "submission_id")
        with self._transaction():
            row = self._connection.execute(
                "SELECT * FROM queue_baselines WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            if row is None:
                raise RecordNotFound("queue baseline does not exist")
            if row["submission_id"] is not None:
                if row["submission_id"] != submission_id:
                    raise BindingConflict("operation is already bound to another queue submission")
                return self._queue_baseline_from_row(row)
            try:
                self._connection.execute(
                    "UPDATE queue_baselines SET submission_id = ? WHERE operation_id = ?",
                    (submission_id, operation_id),
                )
            except sqlite3.IntegrityError as exc:
                raise BindingConflict("queue submission is already bound") from exc
            updated = self._connection.execute(
                "SELECT * FROM queue_baselines WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            assert updated is not None
            return self._queue_baseline_from_row(updated)

    @staticmethod
    def _queue_baseline_from_row(row: sqlite3.Row) -> QueueBaseline:
        try:
            value = json.loads(row["turn_ids_json"])
            raw_ids = value["turn_ids"] if isinstance(value, dict) and set(value) == {"turn_ids"} else None
            if not isinstance(raw_ids, list) or len(raw_ids) > 1000:
                raise TypeError
            turn_ids = tuple(raw_ids)
            for turn_id in turn_ids:
                validate_external_id(turn_id, "turn_id")
            if len(set(turn_ids)) != len(turn_ids) or turn_ids != tuple(sorted(turn_ids)):
                raise TypeError
        except (ContractValidationError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise JournalError("queue baseline is malformed") from exc
        payload = canonical_json({"turn_ids": list(turn_ids)})
        if payload != row["turn_ids_json"] or sha256_text(payload) != row["turn_ids_sha256"]:
            raise JournalError("queue baseline digest mismatch")
        return QueueBaseline(
            operation_id=row["operation_id"],
            thread_id=row["thread_id"],
            message_sha256=row["message_sha256"],
            turn_ids=turn_ids,
            turn_ids_sha256=row["turn_ids_sha256"],
            submission_id=row["submission_id"],
            created_at=row["created_at"],
        )

    def get_queue_baseline(self, operation_id: str) -> QueueBaseline:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT * FROM queue_baselines WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            if row is None:
                raise RecordNotFound("queue baseline does not exist")
            return self._queue_baseline_from_row(row)

    def get_task(self, task_id: str) -> TaskRecord:
        with self._lock:
            self._ensure_open()
            row = self._select_task(task_id)
            if row is None:
                raise RecordNotFound("task does not exist")
            return self._task_from_row(row)

    def get_operation(self, operation_id: str) -> OperationRecord:
        with self._lock:
            self._ensure_open()
            row = self._select_operation(operation_id)
            if row is None:
                raise RecordNotFound("operation does not exist")
            return self._operation_from_row(row)

    def list_operations(self, task_id: str | None = None) -> tuple[OperationRecord, ...]:
        with self._lock:
            self._ensure_open()
            if task_id is None:
                rows = self._connection.execute(
                    "SELECT * FROM operations ORDER BY created_at, operation_id"
                ).fetchall()
            else:
                rows = self._connection.execute(
                    """
                    SELECT * FROM operations
                    WHERE task_id = ? ORDER BY created_at, operation_id
                    """,
                    (task_id,),
                ).fetchall()
            return tuple(self._operation_from_row(row) for row in rows)

    @contextmanager
    def operation_guard(self, operation_id: str) -> Iterator[bool]:
        """Try to own one operation across threads and processes.

        The guard is non-blocking. A duplicate caller that receives ``False``
        must observe state only. OS locks are released automatically after a
        process crash, which lets a later invocation perform conservative
        recovery without a time-based lease.
        """

        self._ensure_open()
        if self._path is None:
            with self._lock:
                local = self._memory_operation_locks.setdefault(
                    operation_id, threading.Lock()
                )
            acquired = local.acquire(blocking=False)
            try:
                yield acquired
            finally:
                if acquired:
                    local.release()
            return

        lock_directory = Path(f"{self._path}.locks")
        try:
            lock_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            metadata = lock_directory.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise JournalError("operation lock directory is unsafe")
            try:
                os.chmod(lock_directory, stat.S_IRWXU)
            except OSError:
                pass
            lock_path = lock_directory / f"{sha256_text(operation_id)}.lock"
            flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(lock_path, flags, 0o600)
            try:
                os.chmod(lock_path, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
        except OSError as exc:
            raise JournalError("cannot safely open operation lock") from exc

        acquired = False
        try:
            if os.name == "posix":
                import fcntl

                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError:
                    acquired = False
            elif os.name == "nt":
                import msvcrt

                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"0")
                os.lseek(descriptor, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    acquired = True
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                        raise
            else:
                raise JournalError("operation locks are unsupported on this platform")

            yield acquired
        finally:
            if acquired:
                try:
                    if os.name == "posix":
                        import fcntl

                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                    elif os.name == "nt":
                        import msvcrt

                        os.lseek(descriptor, 0, os.SEEK_SET)
                        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            os.close(descriptor)

    def bind_thread(self, task_id: str, thread_id: str) -> TaskRecord:
        validate_external_id(thread_id, "thread_id")
        with self._transaction():
            row = self._select_task(task_id)
            if row is None:
                raise RecordNotFound("task does not exist")
            existing = row["thread_id"]
            if existing is not None:
                if existing != thread_id:
                    raise BindingConflict("task is already bound to another thread")
                return self._task_from_row(row)
            now = _utc_now()
            try:
                self._connection.execute(
                    "UPDATE tasks SET thread_id = ?, updated_at = ? WHERE task_id = ?",
                    (thread_id, now, task_id),
                )
            except sqlite3.IntegrityError as exc:
                raise BindingConflict("thread is already bound to another task") from exc
            updated = self._select_task(task_id)
            assert updated is not None
            result = self._task_from_row(updated)
        return result

    def bind_turn(self, operation_id: str, turn_id: str) -> OperationRecord:
        validate_external_id(turn_id, "turn_id")
        with self._transaction():
            row = self._select_operation(operation_id)
            if row is None:
                raise RecordNotFound("operation does not exist")
            existing = row["turn_id"]
            if existing is not None:
                if existing != turn_id:
                    raise BindingConflict("operation is already bound to another turn")
                return self._operation_from_row(row)
            current = OperationState(row["state"])
            if current not in {
                OperationState.DISPATCHING,
                OperationState.QUEUE_DISPATCH_IN_FLIGHT,
                OperationState.RUNNING,
                OperationState.UNKNOWN,
                OperationState.TIMED_OUT,
            }:
                raise BindingConflict("turn can only be bound after dispatch begins")
            task = self._select_task(row["task_id"])
            if task is None or task["thread_id"] is None:
                raise BindingConflict("thread must be bound before turn")
            now = _utc_now()
            try:
                self._connection.execute(
                    """
                    UPDATE operations SET turn_id = ?, updated_at = ?
                    WHERE operation_id = ?
                    """,
                    (turn_id, now, operation_id),
                )
            except sqlite3.IntegrityError as exc:
                raise BindingConflict("turn is already bound to another operation") from exc
            updated = self._select_operation(operation_id)
            assert updated is not None
            result = self._operation_from_row(updated)
        return result

    def transition(
        self,
        operation_id: str,
        target: OperationState | str,
        *,
        failure_code: str | None = None,
    ) -> OperationRecord:
        """Apply one ordinary, explicit state-machine transition."""

        try:
            target_state = parse_state(target)
        except ContractValidationError as exc:
            raise InvalidTransition("target operation state is invalid") from exc
        if target_state in _RECEIPT_FINAL_STATES:
            raise InvalidTransition(
                "terminal states require atomic finalize_operation"
            )
        code = _failure_code(failure_code)
        with self._transaction():
            row = self._select_operation(operation_id)
            if row is None:
                raise RecordNotFound("operation does not exist")
            current = OperationState(row["state"])
            if target_state == current:
                if code is not None and row["failure_code"] not in (None, code):
                    raise InvalidTransition("state already has a different failure_code")
                return self._operation_from_row(row)
            if target_state not in allowed_transitions(current):
                raise InvalidTransition(
                    f"transition {current.value} -> {target_state.value} is not allowed"
                )
            if target_state not in {
                OperationState.FAILED,
                OperationState.CANCELLED,
                OperationState.TIMED_OUT,
                OperationState.UNKNOWN,
                OperationState.VERIFICATION_FAILED,
                OperationState.SCOPE_VIOLATION,
            } and code is not None:
                raise InvalidTransition("failure_code is only valid for failure states")
            self._require_identity_for_target(row, target_state)
            now = _utc_now()
            self._connection.execute(
                """
                UPDATE operations SET state = ?, failure_code = ?, updated_at = ?
                WHERE operation_id = ?
                """,
                (target_state.value, code, now, operation_id),
            )
            updated = self._select_operation(operation_id)
            assert updated is not None
            result = self._operation_from_row(updated)
        return result

    def _require_identity_for_target(
        self, operation_row: sqlite3.Row, target: OperationState
    ) -> None:
        if target not in {
            OperationState.READY,
            OperationState.DISPATCHING,
            OperationState.RUNNING,
            OperationState.TURN_COMPLETED,
            OperationState.VERIFYING,
            OperationState.VERIFIED,
            OperationState.VERIFICATION_FAILED,
            OperationState.SCOPE_VIOLATION,
        }:
            return
        task = self._select_task(operation_row["task_id"])
        if task is None or task["thread_id"] is None:
            raise InvalidTransition("target state requires a bound thread")
        if target in {
            OperationState.RUNNING,
            OperationState.TURN_COMPLETED,
            OperationState.VERIFYING,
            OperationState.VERIFIED,
            OperationState.VERIFICATION_FAILED,
            OperationState.SCOPE_VIOLATION,
        } and operation_row["turn_id"] is None:
            raise InvalidTransition("target state requires a bound turn")

    def reconcile(
        self,
        operation_id: str,
        target: OperationState | str,
        *,
        failure_code: str | None = None,
    ) -> OperationRecord:
        """Reconcile an ambiguous/timeout operation using exact bound IDs.

        This separate method makes recovery visible in code review and prevents
        an ordinary state transition from turning ``UNKNOWN`` into a retry.
        """

        try:
            target_state = parse_state(target)
        except ContractValidationError as exc:
            raise InvalidTransition("reconciliation target is invalid") from exc
        if target_state not in {
            OperationState.RUNNING,
            OperationState.TURN_COMPLETED,
        }:
            raise InvalidTransition(
                "terminal reconciliation requires atomic finalize_reconciliation"
            )
        code = _failure_code(failure_code)
        if code:
            raise InvalidTransition("successful reconciliation cannot have a failure_code")

        with self._transaction():
            row = self._select_operation(operation_id)
            if row is None:
                raise RecordNotFound("operation does not exist")
            current = OperationState(row["state"])
            if current not in {OperationState.UNKNOWN, OperationState.TIMED_OUT}:
                raise InvalidTransition("only UNKNOWN or TIMED_OUT can be reconciled")
            task = self._select_task(row["task_id"])
            if task is None or task["thread_id"] is None or row["turn_id"] is None:
                raise InvalidTransition("reconciliation requires exact thread and turn IDs")
            now = _utc_now()
            self._connection.execute(
                """
                UPDATE operations SET state = ?, failure_code = ?, updated_at = ?
                WHERE operation_id = ?
                """,
                (target_state.value, code, now, operation_id),
            )
            updated = self._select_operation(operation_id)
            assert updated is not None
            result = self._operation_from_row(updated)
        return result

    def finalize_operation(
        self, operation_id: str, receipt: Receipt | Mapping[str, Any]
    ) -> Finalization:
        """Atomically apply an ordinary terminal transition and its receipt."""

        return self._finalize(operation_id, receipt, mode="ordinary")

    def finalize_reconciliation(
        self, operation_id: str, receipt: Receipt | Mapping[str, Any]
    ) -> Finalization:
        """Atomically reconcile an exact ambiguous turn to a terminal receipt."""

        return self._finalize(operation_id, receipt, mode="reconcile")

    def abandon_unrecoverable(
        self, operation_id: str, receipt: Receipt | Mapping[str, Any]
    ) -> Finalization:
        """Explicitly close an open operation that generic recovery cannot resolve.

        This is never called automatically. Ordinary operations with exact
        thread and turn identities must still use authoritative recovery.
        Queue operations may be closed from any open state because their
        stronger correlation contract deliberately blocks generic recovery.
        Continuations may be closed only before authoritative correlation;
        correlated continuation timeouts must be recovered.
        """

        return self._finalize(operation_id, receipt, mode="abandon")

    def _finalize(
        self,
        operation_id: str,
        receipt: Receipt | Mapping[str, Any],
        *,
        mode: str,
    ) -> Finalization:
        parsed = receipt if isinstance(receipt, Receipt) else Receipt.from_dict(receipt)
        target = parsed.status
        code = _failure_code(parsed.failure_code)
        if target not in _RECEIPT_FINAL_STATES:
            raise InvalidTransition("finalization requires an immutable terminal receipt")
        if mode not in {"ordinary", "reconcile", "abandon"}:
            raise ValueError("unknown finalization mode")

        with self._transaction():
            row = self._select_operation(operation_id)
            if row is None:
                raise RecordNotFound("operation does not exist")
            current = OperationState(row["state"])
            task = self._select_task(row["task_id"])
            if task is None:
                raise JournalError("operation references a missing task")

            if current != target:
                if mode == "ordinary":
                    if target not in allowed_transitions(current):
                        raise InvalidTransition(
                            f"transition {current.value} -> {target.value} is not allowed"
                        )
                elif mode == "reconcile":
                    if current not in {OperationState.UNKNOWN, OperationState.TIMED_OUT}:
                        raise InvalidTransition(
                            "only UNKNOWN or TIMED_OUT can be reconciled"
                        )
                    if target not in {OperationState.FAILED, OperationState.CANCELLED}:
                        raise InvalidTransition(
                            "terminal reconciliation must be FAILED or CANCELLED"
                        )
                    if task["thread_id"] is None or row["turn_id"] is None:
                        raise InvalidTransition(
                            "reconciliation requires exact thread and turn IDs"
                        )
                else:
                    exact_turn = (
                        task["thread_id"] is not None
                        and row["turn_id"] is not None
                    )
                    ambiguous = current in {
                        OperationState.UNKNOWN,
                        OperationState.TIMED_OUT,
                    }
                    queue_open = (
                        row["dispatch_kind"] == "queue"
                        and (current in ACTIVE_STATES or ambiguous)
                    )
                    continuation_unrecoverable = (
                        row["dispatch_kind"] == "continue"
                        and current
                        in {
                            OperationState.QUEUED,
                            OperationState.READY,
                            OperationState.DISPATCHING,
                            OperationState.UNKNOWN,
                        }
                    )
                    identityless_ambiguity = ambiguous and not exact_turn
                    if not (
                        queue_open
                        or continuation_unrecoverable
                        or identityless_ambiguity
                    ):
                        raise InvalidTransition(
                            "operation has an authoritative recovery path or "
                            "is not open"
                        )
                    if target != OperationState.FAILED or code != (
                        "operator_abandoned_unrecoverable_operation"
                    ):
                        raise InvalidTransition("abandonment receipt has invalid outcome")

                self._require_identity_for_target(row, target)
                self._connection.execute(
                    """
                    UPDATE operations SET state = ?, failure_code = ?, updated_at = ?
                    WHERE operation_id = ?
                    """,
                    (target.value, code, _utc_now(), operation_id),
                )

            updated = self._select_operation(operation_id)
            assert updated is not None
            self._validate_receipt_binding(parsed, task, updated)
            receipt_json = parsed.to_json()
            receipt_sha256 = sha256_text(receipt_json)
            existing = self._connection.execute(
                "SELECT * FROM receipts WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            if existing is not None:
                if existing["receipt_sha256"] != receipt_sha256:
                    raise ReceiptConflict("operation already has a different receipt")
                stored = Receipt.from_json(existing["receipt_json"])
            else:
                self._connection.execute(
                    """
                    INSERT INTO receipts(
                        operation_id, receipt_sha256, receipt_json, created_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (operation_id, receipt_sha256, receipt_json, _utc_now()),
                )
                stored = parsed
            result = Finalization(self._operation_from_row(updated), stored)
        return result

    def store_receipt(
        self, operation_id: str, receipt: Receipt | Mapping[str, Any]
    ) -> Receipt:
        """Insert an immutable privacy-safe receipt.

        Re-inserting byte-equivalent canonical content is idempotent.  Any
        other attempt to replace the operation's receipt is rejected.
        """

        parsed = receipt if isinstance(receipt, Receipt) else Receipt.from_dict(receipt)
        receipt_json = parsed.to_json()
        receipt_sha256 = sha256_text(receipt_json)

        with self._transaction():
            operation = self._select_operation(operation_id)
            if operation is None:
                raise RecordNotFound("operation does not exist")
            task = self._select_task(operation["task_id"])
            if task is None:
                raise JournalError("operation references a missing task")
            self._validate_receipt_binding(parsed, task, operation)
            existing = self._connection.execute(
                "SELECT * FROM receipts WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            if existing is not None:
                if existing["receipt_sha256"] != receipt_sha256:
                    raise ReceiptConflict("operation already has a different receipt")
                return Receipt.from_json(existing["receipt_json"])
            self._connection.execute(
                """
                INSERT INTO receipts(operation_id, receipt_sha256, receipt_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (operation_id, receipt_sha256, receipt_json, _utc_now()),
            )
        return parsed

    def store_baseline(
        self, operation_id: str, snapshot: WorkspaceSnapshot
    ) -> WorkspaceSnapshot:
        """Persist immutable relative-path evidence needed after a restart."""

        if not isinstance(snapshot, WorkspaceSnapshot):
            raise TypeError("snapshot must be a WorkspaceSnapshot")
        snapshot_json = canonical_json(snapshot_to_dict(snapshot))
        snapshot_sha256 = sha256_text(snapshot_json)
        with self._transaction():
            if self._select_operation(operation_id) is None:
                raise RecordNotFound("operation does not exist")
            existing = self._connection.execute(
                "SELECT * FROM baselines WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            if existing is not None:
                if existing["snapshot_sha256"] != snapshot_sha256:
                    raise BaselineConflict("operation already has a different baseline")
                return snapshot_from_dict(json.loads(existing["snapshot_json"]))
            self._connection.execute(
                """
                INSERT INTO baselines(operation_id, snapshot_sha256, snapshot_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (operation_id, snapshot_sha256, snapshot_json, _utc_now()),
            )
        return snapshot

    def get_baseline(self, operation_id: str) -> WorkspaceSnapshot:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT snapshot_json FROM baselines WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise RecordNotFound("baseline does not exist")
            try:
                value = json.loads(row["snapshot_json"])
            except json.JSONDecodeError as exc:
                raise JournalError("baseline JSON is corrupted") from exc
            if not isinstance(value, Mapping):
                raise JournalError("baseline JSON is not an object")
            return snapshot_from_dict(value)

    def _validate_receipt_binding(
        self, receipt: Receipt, task: sqlite3.Row, operation: sqlite3.Row
    ) -> None:
        if receipt.operation_id != operation["operation_id"]:
            raise ReceiptConflict("receipt operation_id does not match")
        if receipt.task_id != operation["task_id"]:
            raise ReceiptConflict("receipt task_id does not match")
        if receipt.status.value != operation["state"]:
            raise ReceiptConflict("receipt status does not match journal state")
        if receipt.prompt_sha256 != operation["prompt_sha256"]:
            raise ReceiptConflict("receipt prompt digest does not match")
        if receipt.workspace_sha256 != sha256_text(task["workspace"]):
            raise ReceiptConflict("receipt workspace digest does not match")
        expected_thread = sha256_text(task["thread_id"]) if task["thread_id"] else None
        expected_turn = (
            sha256_text(operation["turn_id"]) if operation["turn_id"] else None
        )
        if receipt.thread_sha256 != expected_thread:
            raise ReceiptConflict("receipt thread digest does not match")
        if receipt.turn_sha256 != expected_turn:
            raise ReceiptConflict("receipt turn digest does not match")
        if receipt.failure_code != operation["failure_code"]:
            raise ReceiptConflict("receipt failure_code does not match journal state")

    def get_receipt(self, operation_id: str) -> Receipt:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT receipt_json FROM receipts WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise RecordNotFound("receipt does not exist")
            return Receipt.from_json(row["receipt_json"])

    @staticmethod
    def _watch_from_row(row: sqlite3.Row) -> WatchRecord:
        return WatchRecord(
            watch_id=row["watch_id"], thread_id=row["thread_id"], turn_id=row["turn_id"],
            state=row["state"], result_sha256=row["result_sha256"],
            failure_code=row["failure_code"], created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> CompletionEvent:
        return CompletionEvent(
            event_id=row["event_id"], watch_id=row["watch_id"],
            thread_id=row["thread_id"], turn_id=row["turn_id"], status=row["status"],
            result_sha256=row["result_sha256"], created_at=row["created_at"],
            acked_at=row["acked_at"],
        )

    def adopt_watch(self, thread_id: str, turn_id: str) -> WatchRecord:
        """Idempotently persist an exact read-only thread/turn identity."""

        validate_external_id(thread_id, "thread_id")
        validate_external_id(turn_id, "turn_id")
        with self._transaction():
            existing = self._connection.execute(
                "SELECT * FROM watches WHERE thread_id = ? AND turn_id = ?",
                (thread_id, turn_id),
            ).fetchone()
            if existing is not None:
                return self._watch_from_row(existing)
            now = _utc_now()
            watch_id = "watch-" + secrets.token_hex(16)
            self._connection.execute(
                """
                INSERT INTO watches(
                    watch_id, thread_id, turn_id, state, result_sha256,
                    failure_code, created_at, updated_at
                ) VALUES (?, ?, ?, 'ACTIVE', NULL, NULL, ?, ?)
                """,
                (watch_id, thread_id, turn_id, now, now),
            )
            row = self._connection.execute(
                "SELECT * FROM watches WHERE watch_id = ?", (watch_id,)
            ).fetchone()
            assert row is not None
            return self._watch_from_row(row)

    def get_watch(self, watch_id: str) -> WatchRecord:
        validate_external_id(watch_id, "watch_id")
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT * FROM watches WHERE watch_id = ?", (watch_id,)
            ).fetchone()
            if row is None:
                raise RecordNotFound("watch does not exist")
            return self._watch_from_row(row)

    def list_watches(self, *, active_only: bool = False) -> tuple[WatchRecord, ...]:
        with self._lock:
            self._ensure_open()
            sql = "SELECT * FROM watches"
            if active_only:
                sql += " WHERE state = 'ACTIVE'"
            sql += " ORDER BY created_at, watch_id"
            return tuple(self._watch_from_row(row) for row in self._connection.execute(sql))

    def finish_watch(
        self,
        watch_id: str,
        status: str,
        *,
        result_sha256: str | None = None,
        failure_code: str | None = None,
    ) -> tuple[WatchRecord, CompletionEvent]:
        """Atomically terminalize a watch and insert its single outbox event."""

        validate_external_id(watch_id, "watch_id")
        if status not in WATCH_TERMINAL_STATES:
            raise JournalError("watch terminal status is invalid")
        if result_sha256 is not None and (
            len(result_sha256) != 64
            or any(character not in "0123456789abcdef" for character in result_sha256)
        ):
            raise JournalError("result_sha256 must be a lowercase SHA-256 digest")
        code = _failure_code(failure_code)
        if status == "COMPLETED" and result_sha256 is None:
            raise JournalError("completed watch requires result_sha256")
        with self._transaction():
            row = self._connection.execute(
                "SELECT * FROM watches WHERE watch_id = ?", (watch_id,)
            ).fetchone()
            if row is None:
                raise RecordNotFound("watch does not exist")
            if row["state"] != "ACTIVE" and (
                row["state"] != status
                or row["result_sha256"] != result_sha256
                or row["failure_code"] != code
            ):
                raise BindingConflict("watch already has a different terminal result")
            now = _utc_now()
            if row["state"] == "ACTIVE":
                self._connection.execute(
                    """
                    UPDATE watches
                    SET state = ?, result_sha256 = ?, failure_code = ?, updated_at = ?
                    WHERE watch_id = ?
                    """,
                    (status, result_sha256, code, now, watch_id),
                )
            event_id = "event-" + sha256_text(
                f"{watch_id}\0{status}\0{result_sha256 or ''}"
            )[:32]
            self._connection.execute(
                """
                INSERT OR IGNORE INTO completion_events(
                    event_id, watch_id, thread_id, turn_id, status,
                    result_sha256, created_at, acked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (event_id, watch_id, row["thread_id"], row["turn_id"], status,
                 result_sha256, now),
            )
            watch_row = self._connection.execute(
                "SELECT * FROM watches WHERE watch_id = ?", (watch_id,)
            ).fetchone()
            event_row = self._connection.execute(
                "SELECT * FROM completion_events WHERE watch_id = ?", (watch_id,)
            ).fetchone()
            assert watch_row is not None and event_row is not None
            return self._watch_from_row(watch_row), self._event_from_row(event_row)

    def list_events(self, *, include_acked: bool = False) -> tuple[CompletionEvent, ...]:
        with self._lock:
            self._ensure_open()
            sql = "SELECT * FROM completion_events"
            if not include_acked:
                sql += " WHERE acked_at IS NULL"
            sql += " ORDER BY created_at, event_id"
            return tuple(self._event_from_row(row) for row in self._connection.execute(sql))

    def ack_event(self, event_id: str) -> CompletionEvent:
        validate_external_id(event_id, "event_id")
        with self._transaction():
            row = self._connection.execute(
                "SELECT * FROM completion_events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if row is None:
                raise RecordNotFound("completion event does not exist")
            if row["acked_at"] is None:
                self._connection.execute(
                    "UPDATE completion_events SET acked_at = ? WHERE event_id = ?",
                    (_utc_now(), event_id),
                )
                row = self._connection.execute(
                    "SELECT * FROM completion_events WHERE event_id = ?", (event_id,)
                ).fetchone()
                assert row is not None
            return self._event_from_row(row)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._connection.close()
            self._closed = True
            self._tighten_permissions()

    def __enter__(self) -> "SQLiteJournal":
        self._ensure_open()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


Journal = SQLiteJournal


__all__ = [
    "BaselineConflict",
    "BindingConflict",
    "CompletionEvent",
    "DATABASE_VERSION",
    "Finalization",
    "IdempotencyConflict",
    "InvalidTransition",
    "Journal",
    "JournalClosed",
    "JournalError",
    "OperationRecord",
    "ReceiptConflict",
    "RecordNotFound",
    "Reservation",
    "SQLiteJournal",
    "TaskRecord",
    "TaskBusy",
    "WatchRecord",
    "WATCH_STATES",
    "WATCH_TERMINAL_STATES",
]
