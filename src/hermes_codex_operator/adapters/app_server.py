from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import signal
import subprocess
from typing import Any, Mapping, Sequence, cast

from .base import (
    AdapterClosed,
    AdapterError,
    AdapterTimeout,
    AppServerProcessError,
    DeliveryUnknown,
    LoadedThreadPage,
    ProtocolIntegrityError,
    QueuePage,
    QueuedSubmission,
    ReadbackMissingResult,
    RemoteRequestError,
    RemoteTurnFailed,
    ThreadHandle,
    CatalogTurn,
    ThreadCatalogReadback,
    ThreadOptions,
    ThreadPage,
    ThreadSummary,
    TurnHandle,
    TurnInProgress,
    TurnOptions,
    TurnReadback,
    TurnTerminalStatus,
    validate_command,
)
from ..models import canonical_json, sha256_text


_TERMINAL_STATUSES = frozenset({"completed", "failed", "interrupted"})
_KNOWN_TURN_STATUSES = _TERMINAL_STATUSES | {"inProgress"}
_SANDBOX_TYPES = {
    "read-only": "readOnly",
    "workspace-write": "workspaceWrite",
    "danger-full-access": "dangerFullAccess",
}
EXPERIMENTAL_QUEUE_API_VERSION = "0.149.0"


@dataclass(slots=True)
class _PendingRequest:
    request_id: int
    method: str
    future: asyncio.Future[Any]
    side_effect: bool
    sent: bool = False


class AppServerAdapter:
    """Minimal JSONL client for the official Codex App Server stdio surface.

    The wire format is JSONL, not JSON-RPC 2.0: messages intentionally omit a
    ``jsonrpc`` member. Streamed notifications are observed only for lifecycle
    coordination; ``thread/read`` is always the source of returned model text.
    """

    def __init__(
        self,
        command: Sequence[str] = ("codex", "app-server", "--listen", "stdio://"),
        *,
        request_timeout: float = 30.0,
        shutdown_timeout: float = 1.0,
        stderr_limit_bytes: int = 64 * 1024,
        stdout_line_limit_bytes: int = 4 * 1024 * 1024,
        client_name: str = "hermes-codex-operator",
        client_version: str = "0.1.0",
        experimental_api_version: str | None = None,
    ) -> None:
        self._command = validate_command(command)
        if request_timeout <= 0 or shutdown_timeout <= 0:
            raise ValueError("timeouts must be positive")
        if stderr_limit_bytes < 1 or stdout_line_limit_bytes < 1024:
            raise ValueError("stream limits are too small")
        if not client_name or not client_version:
            raise ValueError("client identity must be non-empty")
        if experimental_api_version not in {None, EXPERIMENTAL_QUEUE_API_VERSION}:
            from .base import UnsupportedCapability

            raise UnsupportedCapability("UNSUPPORTED_CAPABILITY")

        self._request_timeout = request_timeout
        self._shutdown_timeout = shutdown_timeout
        self._stderr_limit = stderr_limit_bytes
        self._stdout_line_limit = stdout_line_limit_bytes
        self._client_name = client_name
        self._client_version = client_version
        self._experimental_api_version = experimental_api_version

        self._process: asyncio.subprocess.Process | None = None
        self._process_group_id: int | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[int, _PendingRequest] = {}
        self._completion_waiters: dict[
            tuple[str, str], list[asyncio.Future[TurnTerminalStatus]]
        ] = {}
        self._terminal_notifications: dict[tuple[str, str], TurnTerminalStatus] = {}
        self._request_id = 0
        self._write_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._terminate_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None
        self._fatal: AdapterError | None = None
        self._started = False
        self._closed = False
        self._closing = False
        self._stderr_tail = bytearray()
        self._stderr_total_bytes = 0

    @property
    def stderr_excerpt(self) -> str:
        """Return a bounded tail for local diagnostics (never a protocol result)."""

        return bytes(self._stderr_tail).decode("utf-8", errors="replace")

    @property
    def stderr_truncated(self) -> bool:
        return self._stderr_total_bytes > len(self._stderr_tail)

    async def __aenter__(self) -> AppServerAdapter:
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self.close()

    async def start(self) -> None:
        if self._closed or self._closing:
            raise AdapterClosed("adapter is closed")
        if self._started:
            self._raise_if_fatal()
            return

        async with self._start_lock:
            if self._started:
                self._raise_if_fatal()
                return
            spawn_options: dict[str, Any] = {}
            if os.name == "posix":
                spawn_options["start_new_session"] = True
            elif os.name == "nt":
                spawn_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

            try:
                process = await asyncio.create_subprocess_exec(
                    *self._command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=self._stdout_line_limit,
                    **spawn_options,
                )
            except (OSError, ValueError) as exc:
                raise AppServerProcessError("could not start Codex App Server") from exc

            self._process = process
            if os.name == "posix":
                self._process_group_id = process.pid
            # The reader must exist before initialize: the server may emit a
            # notification before its initialize response.
            self._reader_task = asyncio.create_task(
                self._reader_loop(), name="codex-app-server-stdout"
            )
            self._stderr_task = asyncio.create_task(
                self._stderr_loop(), name="codex-app-server-stderr"
            )
            try:
                initialize_result = await self._request(
                    "initialize",
                    {
                        "clientInfo": {
                            "name": self._client_name,
                            "title": "Hermes Codex Operator",
                            "version": self._client_version,
                        },
                        "capabilities": {
                            "experimentalApi": self._experimental_api_version is not None
                        },
                    },
                    side_effect=False,
                )
                self._validate_initialize_result(initialize_result)
                await self._write_message({"method": "initialized"})
            except BaseException:
                await self.close()
                raise
            self._started = True

    async def start_thread(
        self,
        workspace: Path,
        options: ThreadOptions | None = None,
    ) -> ThreadHandle:
        await self._ensure_started()
        workspace = self._absolute_workspace(workspace)
        chosen = options or ThreadOptions()
        params: dict[str, Any] = {
            "cwd": os.fspath(workspace),
            "sandbox": chosen.sandbox,
            "approvalPolicy": chosen.approval_policy,
        }
        self._include(params, "model", chosen.model)
        self._include(params, "developerInstructions", chosen.developer_instructions)
        self._include(params, "serviceTier", chosen.service_tier)
        result = await self._request("thread/start", params, side_effect=True)
        thread_id = self._validate_thread_result(
            result,
            method="thread/start",
            workspace=workspace,
            options=chosen,
        )
        return ThreadHandle(thread_id=thread_id, workspace=workspace)

    async def list_threads(
        self, *, cursor: str | None = None, limit: int = 50
    ) -> ThreadPage:
        """Read one bounded catalog page without resuming or mutating threads."""

        await self._ensure_started()
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("thread/list limit must be an integer from 1 to 100")
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = self._nonempty_string(cursor, "cursor")
        result = await self._request("thread/list", params, side_effect=False)
        if not isinstance(result, Mapping):
            raise ProtocolIntegrityError("thread/list result must be an object")
        data = result.get("data")
        if not isinstance(data, list):
            raise ProtocolIntegrityError("thread/list result.data must be an array")
        if len(data) > limit:
            raise ProtocolIntegrityError("thread/list returned more rows than requested")
        raw_cursor = result.get("nextCursor")
        if raw_cursor is not None and not isinstance(raw_cursor, str):
            raise ProtocolIntegrityError("thread/list nextCursor must be a string or null")
        if raw_cursor == "":
            raise ProtocolIntegrityError("thread/list nextCursor must not be empty")
        summaries = tuple(self._parse_thread_summary(item) for item in data)
        identities = tuple(item.thread_id for item in summaries)
        if len(set(identities)) != len(identities):
            raise ProtocolIntegrityError("thread/list returned duplicate thread ids")
        return ThreadPage(summaries, raw_cursor)

    async def read_thread(self, thread_id: str) -> ThreadCatalogReadback:
        """Read one exact stored thread without resuming or subscribing to it."""

        await self._ensure_started()
        expected = self._nonempty_string(thread_id, "thread_id")
        result = await self._request(
            "thread/read",
            {"threadId": expected, "includeTurns": True},
            side_effect=False,
        )
        thread = self._result_object(result, "thread/read", "thread")
        summary = self._parse_thread_summary(thread)
        if summary.thread_id != expected:
            raise ProtocolIntegrityError("thread/read returned a different thread id")

        raw_turns = thread.get("turns")
        assert isinstance(raw_turns, list)  # validated by _parse_thread_summary
        turns = tuple(self._parse_catalog_turn(turn) for turn in raw_turns)
        identities = tuple(turn.turn_id for turn in turns)
        if len(set(identities)) != len(identities):
            raise ProtocolIntegrityError("thread/read returned duplicate turn ids")
        return ThreadCatalogReadback(summary, turns)

    async def list_loaded_threads(
        self, *, cursor: str | None = None, limit: int = 100
    ) -> LoadedThreadPage:
        self._require_queue_capability()
        await self._ensure_started()
        self._validate_page_limit(limit, "thread/loaded/list")
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = self._nonempty_string(cursor, "cursor")
        result = await self._request("thread/loaded/list", params, side_effect=False)
        data, next_cursor = self._parse_page(result, "thread/loaded/list", limit)
        ids = tuple(self._nonempty_string(item, "thread/loaded/list data") for item in data)
        return LoadedThreadPage(ids, next_cursor)

    async def list_queue(
        self, thread_id: str, *, cursor: str | None = None, limit: int = 100
    ) -> QueuePage:
        self._require_queue_capability()
        await self._ensure_started()
        expected = self._nonempty_string(thread_id, "thread_id")
        self._validate_page_limit(limit, "thread/queue/list")
        params: dict[str, Any] = {"threadId": expected, "limit": limit}
        if cursor is not None:
            params["cursor"] = self._nonempty_string(cursor, "cursor")
        result = await self._request("thread/queue/list", params, side_effect=False)
        data, next_cursor = self._parse_page(result, "thread/queue/list", limit)
        submissions = tuple(self._parse_queued_submission(item) for item in data)
        identities = tuple(item.submission_id for item in submissions)
        if len(set(identities)) != len(identities):
            raise ProtocolIntegrityError("thread/queue/list returned duplicate submission ids")
        return QueuePage(submissions, next_cursor)

    async def add_queue(
        self, thread_id: str, prompt: str, client_message_id: str
    ) -> QueuedSubmission:
        self._require_queue_capability()
        await self._ensure_started()
        expected = self._nonempty_string(thread_id, "thread_id")
        text = self._nonempty_string(prompt, "prompt")
        client_id = self._nonempty_string(client_message_id, "client_message_id")
        result = await self._request(
            "thread/queue/add",
            {
                "threadId": expected,
                "clientUserMessageId": client_id,
                "input": [{"type": "text", "text": text}],
            },
            side_effect=True,
        )
        queued = self._result_object(result, "thread/queue/add", "queuedSubmission")
        submission = self._parse_queued_submission(queued)
        if submission.client_message_id != client_id:
            raise ProtocolIntegrityError("thread/queue/add changed client message identity")
        expected_digest = sha256_text(
            canonical_json({"input": [{"text": text, "type": "text"}]})
        )
        if submission.input_sha256 != expected_digest:
            raise ProtocolIntegrityError("thread/queue/add changed queued input")
        return submission

    async def start_queued_turn(
        self, thread_id: str, submission_id: str
    ) -> TurnHandle:
        self._require_queue_capability()
        await self._ensure_started()
        expected = self._nonempty_string(thread_id, "thread_id")
        submission = self._nonempty_string(submission_id, "submission_id")
        result = await self._request(
            "thread/queue/start",
            {"threadId": expected, "queuedSubmissionId": submission},
            side_effect=True,
        )
        turn = self._result_object(result, "thread/queue/start", "turn")
        return TurnHandle(
            expected,
            self._nonempty_string(turn.get("id"), "thread/queue/start turn.id"),
        )

    async def resume_thread(
        self,
        thread_id: str,
        workspace: Path,
        options: ThreadOptions | None = None,
    ) -> ThreadHandle:
        await self._ensure_started()
        expected = self._nonempty_string(thread_id, "thread_id")
        workspace = self._absolute_workspace(workspace)
        chosen = options or ThreadOptions()
        params: dict[str, Any] = {
            "threadId": expected,
            "cwd": os.fspath(workspace),
            "sandbox": chosen.sandbox,
            "approvalPolicy": chosen.approval_policy,
        }
        self._include(params, "model", chosen.model)
        self._include(params, "developerInstructions", chosen.developer_instructions)
        self._include(params, "serviceTier", chosen.service_tier)
        result = await self._request(
            "thread/resume",
            params,
            # Resuming is part of the continuation mutation boundary. A lost
            # response is delivery-ambiguous and must never be retried.
            side_effect=True,
        )
        actual = self._validate_thread_result(
            result,
            method="thread/resume",
            workspace=workspace,
            options=chosen,
        )
        if actual != expected:
            raise ProtocolIntegrityError("thread/resume returned a different thread id")
        return ThreadHandle(thread_id=actual, workspace=workspace)

    async def start_turn(
        self,
        thread_id: str,
        prompt: str,
        options: TurnOptions | None = None,
    ) -> TurnHandle:
        await self._ensure_started()
        expected_thread = self._nonempty_string(thread_id, "thread_id")
        prompt = self._nonempty_string(prompt, "prompt")
        chosen = options or TurnOptions()
        params: dict[str, Any] = {
            "threadId": expected_thread,
            "input": [{"type": "text", "text": prompt}],
        }
        self._include(params, "model", chosen.model)
        self._include(params, "effort", chosen.effort)
        self._include(params, "approvalPolicy", chosen.approval_policy)
        self._include(params, "serviceTier", chosen.service_tier)
        result = await self._request("turn/start", params, side_effect=True)
        turn = self._result_object(result, "turn/start", "turn")
        turn_id = self._nonempty_string(turn.get("id"), "turn/start turn.id")
        return TurnHandle(thread_id=expected_thread, turn_id=turn_id)

    async def wait_for_completion(
        self,
        thread_id: str,
        turn_id: str,
        timeout: float,
    ) -> TurnTerminalStatus:
        await self._ensure_started()
        if timeout <= 0:
            raise ValueError("completion timeout must be positive")
        key = (
            self._nonempty_string(thread_id, "thread_id"),
            self._nonempty_string(turn_id, "turn_id"),
        )
        cached = self._terminal_notifications.get(key)
        if cached is not None:
            return cached

        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[TurnTerminalStatus] = loop.create_future()
        self._completion_waiters.setdefault(key, []).append(waiter)
        # No await occurs between the cache check and registration, so the
        # event loop cannot deliver a notification into that gap.
        try:
            return await asyncio.wait_for(asyncio.shield(waiter), timeout=timeout)
        except asyncio.TimeoutError as exc:
            if not waiter.done():
                waiter.cancel()
            raise AdapterTimeout("turn/completed", timeout) from exc
        finally:
            waiters = self._completion_waiters.get(key)
            if waiters is not None:
                try:
                    waiters.remove(waiter)
                except ValueError:
                    pass
                if not waiters:
                    self._completion_waiters.pop(key, None)

    async def read_turn(self, thread_id: str, turn_id: str) -> TurnReadback:
        await self._ensure_started()
        expected_thread = self._nonempty_string(thread_id, "thread_id")
        expected_turn = self._nonempty_string(turn_id, "turn_id")
        result = await self._request(
            "thread/read",
            {"threadId": expected_thread, "includeTurns": True},
            side_effect=False,
        )
        thread = self._result_object(result, "thread/read", "thread")
        actual_thread = self._nonempty_string(thread.get("id"), "thread/read thread.id")
        if actual_thread != expected_thread:
            raise ProtocolIntegrityError("thread/read returned a different thread id")

        turns = thread.get("turns")
        if not isinstance(turns, list):
            raise ProtocolIntegrityError("thread/read thread.turns must be an array")
        matching = [
            turn
            for turn in turns
            if isinstance(turn, Mapping) and turn.get("id") == expected_turn
        ]
        if not matching:
            raise ReadbackMissingResult(expected_thread, expected_turn, "exact turn is absent")
        if len(matching) != 1:
            raise ProtocolIntegrityError("thread/read returned duplicate turn ids")
        turn = matching[0]

        status_value = turn.get("status")
        if status_value not in _KNOWN_TURN_STATUSES:
            raise ProtocolIntegrityError("authoritative turn has an unknown status")
        if status_value == "inProgress":
            raise TurnInProgress(expected_thread, expected_turn)
        status = cast(TurnTerminalStatus, status_value)
        if status != "completed":
            raise RemoteTurnFailed(
                expected_thread,
                expected_turn,
                status,
                error=turn.get("error"),
            )

        raw_items = turn.get("items")
        if not isinstance(raw_items, list):
            raise ProtocolIntegrityError("thread/read turn.items must be an array")
        items: tuple[Mapping[str, Any], ...] = tuple(
            dict(item) for item in raw_items if isinstance(item, Mapping)
        )
        if len(items) != len(raw_items):
            raise ProtocolIntegrityError("thread/read turn.items contains a non-object")

        final_messages: list[str] = []
        legacy_messages: list[str] = []
        for item in items:
            if item.get("type") != "agentMessage":
                continue
            text = item.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            phase = item.get("phase")
            if phase == "final_answer":
                final_messages.append(text)
            elif phase is None:
                legacy_messages.append(text)
        candidates = final_messages or legacy_messages
        if not candidates:
            raise ReadbackMissingResult(
                expected_thread,
                expected_turn,
                "final agent message is absent",
            )
        return TurnReadback(
            thread_id=expected_thread,
            turn_id=expected_turn,
            status=status,
            final_text=candidates[-1],
            items=items,
        )

    async def interrupt(self, thread_id: str, turn_id: str) -> None:
        await self._ensure_started()
        await self._request(
            "turn/interrupt",
            {
                "threadId": self._nonempty_string(thread_id, "thread_id"),
                "turnId": self._nonempty_string(turn_id, "turn_id"),
            },
            side_effect=True,
        )

    async def close(self) -> None:
        async with self._close_lock:
            if self._close_task is None:
                self._closing = True
                self._close_task = asyncio.create_task(
                    self._close_impl(), name="codex-app-server-close"
                )
            close_task = self._close_task
            if self._closed and close_task.done():
                return

        try:
            await asyncio.shield(close_task)
        except asyncio.CancelledError:
            # Cleanup is bounded and must complete even if the caller is
            # cancelled; only then is cancellation propagated.
            await asyncio.shield(close_task)
            raise

    async def _close_impl(self) -> None:
        try:
            self._fail_all(AdapterClosed("adapter closed"))
            await self._terminate_process_group()

            current = asyncio.current_task()
            tasks = (self._reader_task, self._stderr_task)
            for task in tasks:
                if task is not None and task is not current and not task.done():
                    task.cancel()
            for task in tasks:
                if task is None or task is current:
                    continue
                try:
                    await task
                except (asyncio.CancelledError, AdapterError):
                    pass
                except Exception:
                    pass
        finally:
            self._closed = True
            self._closing = False

    async def _ensure_started(self) -> None:
        if not self._started:
            await self.start()
        self._raise_if_fatal()

    async def _request(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        side_effect: bool,
    ) -> Any:
        self._raise_if_fatal()
        if self._closed or self._closing:
            raise AdapterClosed("adapter is closed")
        self._request_id += 1
        request_id = self._request_id
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        pending = _PendingRequest(
            request_id=request_id,
            method=method,
            future=future,
            side_effect=side_effect,
        )
        self._pending[request_id] = pending
        message = {"id": request_id, "method": method, "params": dict(params)}
        try:
            await self._write_message(message, pending=pending)
        except BaseException:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            if side_effect and pending.sent:
                error = DeliveryUnknown(
                    method,
                    request_id,
                    sent=True,
                    timeout_seconds=self._request_timeout,
                )
                self._set_fatal(error)
                await self._finish_termination()
                raise error
            raise

        try:
            return await asyncio.wait_for(
                asyncio.shield(future), timeout=self._request_timeout
            )
        except asyncio.TimeoutError as exc:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            if side_effect and pending.sent:
                error: AdapterError = DeliveryUnknown(
                    method,
                    request_id,
                    sent=True,
                    timeout_seconds=self._request_timeout,
                )
            else:
                error = AdapterTimeout(method, self._request_timeout)
            self._set_fatal(error)
            await self._finish_termination()
            raise error from exc
        except asyncio.CancelledError as exc:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            if side_effect and pending.sent:
                error = DeliveryUnknown(
                    method,
                    request_id,
                    sent=True,
                    timeout_seconds=self._request_timeout,
                )
                self._set_fatal(error)
                await self._finish_termination()
                raise error from exc
            self._set_fatal(AdapterClosed("request cancelled; connection discarded"))
            await self._finish_termination()
            raise
        finally:
            self._pending.pop(request_id, None)

    async def _write_message(
        self,
        message: Mapping[str, Any],
        *,
        pending: _PendingRequest | None = None,
    ) -> None:
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise AppServerProcessError(
                "Codex App Server stdin is unavailable",
                returncode=None if process is None else process.returncode,
            )
        try:
            encoded = (
                json.dumps(
                    dict(message),
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )
        except (TypeError, ValueError) as exc:
            raise ProtocolIntegrityError("outgoing message is not valid JSON") from exc

        async with self._write_lock:
            process.stdin.write(encoded)
            if pending is not None:
                pending.sent = True
            await process.stdin.drain()

    async def _reader_loop(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    if not self._closing:
                        error = AppServerProcessError(
                            "Codex App Server closed stdout",
                            returncode=process.returncode,
                        )
                        self._set_fatal(error)
                        asyncio.create_task(self._terminate_process_group())
                    return
                try:
                    message = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise ProtocolIntegrityError("App Server emitted invalid JSONL") from exc
                if not isinstance(message, dict):
                    raise ProtocolIntegrityError("App Server message must be an object")
                await self._handle_message(message)
        except asyncio.CancelledError:
            raise
        except AdapterError as exc:
            self._set_fatal(exc)
            if not self._closing:
                asyncio.create_task(self._terminate_process_group())
        except (ValueError, OSError) as exc:
            error = ProtocolIntegrityError("failed while reading App Server JSONL")
            error.__cause__ = exc
            self._set_fatal(error)
            if not self._closing:
                asyncio.create_task(self._terminate_process_group())

    async def _stderr_loop(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        try:
            while chunk := await process.stderr.read(4096):
                self._stderr_total_bytes += len(chunk)
                self._stderr_tail.extend(chunk)
                overflow = len(self._stderr_tail) - self._stderr_limit
                if overflow > 0:
                    del self._stderr_tail[:overflow]
        except asyncio.CancelledError:
            raise

    async def _handle_message(self, message: dict[str, Any]) -> None:
        if "method" in message:
            method = message.get("method")
            if not isinstance(method, str) or not method:
                raise ProtocolIntegrityError("App Server method must be a non-empty string")
            if "id" in message:
                await self._reject_server_request(message)
                raise ProtocolIntegrityError(
                    f"unsupported App Server request received: {method}"
                )
            self._handle_notification(method, message.get("params"))
            return

        if "id" not in message:
            raise ProtocolIntegrityError("App Server response is missing id")
        response_id = message.get("id")
        if isinstance(response_id, bool) or not isinstance(response_id, int):
            raise ProtocolIntegrityError("App Server response id has an unexpected type")
        pending = self._pending.pop(response_id, None)
        if pending is None:
            raise ProtocolIntegrityError("App Server response id is not pending")
        if "error" in message:
            error = message.get("error")
            if not isinstance(error, Mapping):
                pending.future.set_exception(
                    ProtocolIntegrityError("App Server error response is malformed")
                )
                return
            code = error.get("code")
            pending.future.set_exception(
                RemoteRequestError(
                    pending.method,
                    code=code if isinstance(code, int) and not isinstance(code, bool) else None,
                    message=str(error.get("message", "remote error")),
                    data=error.get("data"),
                )
            )
            return
        if "result" not in message:
            pending.future.set_exception(
                ProtocolIntegrityError("App Server response has neither result nor error")
            )
            return
        pending.future.set_result(message.get("result"))

    async def _reject_server_request(self, message: Mapping[str, Any]) -> None:
        request_id = message.get("id")
        if isinstance(request_id, bool) or not isinstance(request_id, (int, str)):
            raise ProtocolIntegrityError("App Server request id has an unexpected type")
        await self._write_message(
            {
                "id": request_id,
                "error": {
                    "code": -32601,
                    "message": "server requests are unsupported by this non-interactive client",
                },
            }
        )

    def _handle_notification(self, method: str, params: Any) -> None:
        if method != "turn/completed":
            return
        if not isinstance(params, Mapping):
            raise ProtocolIntegrityError("turn/completed params must be an object")
        thread_id = self._nonempty_string(
            params.get("threadId"), "turn/completed threadId"
        )
        turn = params.get("turn")
        if not isinstance(turn, Mapping):
            raise ProtocolIntegrityError("turn/completed turn must be an object")
        turn_id = self._nonempty_string(turn.get("id"), "turn/completed turn.id")
        status_value = turn.get("status")
        if status_value not in _TERMINAL_STATUSES:
            raise ProtocolIntegrityError("turn/completed has a non-terminal status")
        status = cast(TurnTerminalStatus, status_value)
        key = (thread_id, turn_id)
        existing = self._terminal_notifications.get(key)
        if existing is not None and existing != status:
            raise ProtocolIntegrityError("turn/completed status changed for the same turn")
        self._terminal_notifications[key] = status
        for waiter in self._completion_waiters.pop(key, []):
            if not waiter.done():
                waiter.set_result(status)

    def _set_fatal(self, error: AdapterError) -> None:
        if self._fatal is None:
            self._fatal = error
        self._fail_all(self._fatal)

    def _fail_all(self, error: AdapterError) -> None:
        for pending in tuple(self._pending.values()):
            if pending.future.done():
                continue
            if pending.side_effect and pending.sent:
                pending.future.set_exception(
                    DeliveryUnknown(
                        pending.method,
                        pending.request_id,
                        sent=True,
                        timeout_seconds=self._request_timeout,
                    )
                )
            else:
                pending.future.set_exception(error)
        for waiters in tuple(self._completion_waiters.values()):
            for waiter in waiters:
                if not waiter.done():
                    waiter.set_exception(error)

    def _raise_if_fatal(self) -> None:
        if self._fatal is not None:
            raise self._fatal

    async def _finish_termination(self) -> None:
        task = asyncio.create_task(self._terminate_process_group())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.shield(task)

    async def _terminate_process_group(self) -> None:
        async with self._terminate_lock:
            process = self._process
            if process is None:
                return
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except (BrokenPipeError, RuntimeError):
                    pass

            if os.name == "posix" and self._process_group_id is not None:
                group_id = self._process_group_id
                self._signal_process_group(group_id, signal.SIGTERM)
                if not await self._wait_for_process_group_exit(
                    group_id, self._shutdown_timeout
                ):
                    self._signal_process_group(group_id, signal.SIGKILL)
                    await self._wait_for_process_group_exit(
                        group_id, self._shutdown_timeout
                    )
                try:
                    await asyncio.wait_for(
                        asyncio.shield(process.wait()), timeout=self._shutdown_timeout
                    )
                except asyncio.TimeoutError:
                    pass
                return

            if process.returncode is None:
                try:
                    process.terminate()
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=self._shutdown_timeout)
                except asyncio.TimeoutError:
                    try:
                        process.kill()
                    except (ProcessLookupError, PermissionError):
                        pass
                    try:
                        await asyncio.wait_for(
                            process.wait(), timeout=self._shutdown_timeout
                        )
                    except asyncio.TimeoutError:
                        pass

    @staticmethod
    def _signal_process_group(group_id: int, chosen_signal: signal.Signals) -> None:
        try:
            os.killpg(group_id, chosen_signal)
        except (ProcessLookupError, PermissionError):
            pass

    @staticmethod
    def _process_group_exists(group_id: int) -> bool:
        try:
            os.killpg(group_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    async def _wait_for_process_group_exit(
        self, group_id: int, timeout: float
    ) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout
        while self._process_group_exists(group_id):
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(0.02)
        return True

    @staticmethod
    def _absolute_workspace(workspace: Path) -> Path:
        path = Path(workspace)
        if not path.is_absolute():
            raise ValueError("workspace must be an absolute path")
        return path

    def _validate_initialize_result(self, result: Any) -> None:
        if not isinstance(result, Mapping):
            raise ProtocolIntegrityError("initialize result must be an object")
        codex_home = result.get("codexHome")
        if not isinstance(codex_home, str) or not Path(codex_home).is_absolute():
            raise ProtocolIntegrityError("initialize codexHome must be absolute")
        for field in ("platformFamily", "platformOs", "userAgent"):
            self._nonempty_string(result.get(field), f"initialize {field}")
        if self._experimental_api_version is not None:
            user_agent = result["userAgent"]
            version = re.search(
                r"(?:^|[\s/])(\d+\.\d+\.\d+)(?:$|[\s;(])", user_agent
            )
            if version is None or version.group(1) != self._experimental_api_version:
                from .base import UnsupportedCapability

                raise UnsupportedCapability("UNSUPPORTED_CAPABILITY")

    def _validate_thread_result(
        self,
        result: Any,
        *,
        method: str,
        workspace: Path,
        options: ThreadOptions,
    ) -> str:
        if not isinstance(result, Mapping):
            raise ProtocolIntegrityError(f"{method} result must be an object")
        thread = self._result_object(result, method, "thread")
        thread_id = self._nonempty_string(thread.get("id"), f"{method} thread.id")

        self._validate_effective_cwd(result.get("cwd"), workspace, f"{method} cwd")
        self._validate_effective_cwd(
            thread.get("cwd"), workspace, f"{method} thread.cwd"
        )
        if result.get("approvalPolicy") != options.approval_policy:
            raise ProtocolIntegrityError(f"{method} changed the approval policy")
        reviewer = result.get("approvalsReviewer")
        if reviewer not in {"user", "auto_review", "guardian_subagent"}:
            raise ProtocolIntegrityError(f"{method} returned an invalid approvals reviewer")
        self._validate_effective_sandbox(
            result.get("sandbox"), result.get("permissionProfile"), workspace, options
        )

        effective_model = self._nonempty_string(result.get("model"), f"{method} model")
        self._nonempty_string(result.get("modelProvider"), f"{method} modelProvider")
        if options.model is not None and effective_model != options.model:
            raise ProtocolIntegrityError(f"{method} changed the requested model")

        required_thread_fields = {
            "cliVersion",
            "createdAt",
            "cwd",
            "ephemeral",
            "id",
            "modelProvider",
            "preview",
            "source",
            "status",
            "turns",
            "updatedAt",
        }
        if not required_thread_fields.issubset(thread):
            raise ProtocolIntegrityError(f"{method} thread is missing required fields")
        if not isinstance(thread.get("turns"), list):
            raise ProtocolIntegrityError(f"{method} thread.turns must be an array")
        if not isinstance(thread.get("ephemeral"), bool):
            raise ProtocolIntegrityError(f"{method} thread.ephemeral must be boolean")
        for field in ("createdAt", "updatedAt"):
            value = thread.get(field)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ProtocolIntegrityError(f"{method} thread.{field} must be an integer")
        for field in ("cliVersion", "modelProvider"):
            self._nonempty_string(thread.get(field), f"{method} thread.{field}")
        if not isinstance(thread.get("preview"), str):
            raise ProtocolIntegrityError(f"{method} thread.preview must be a string")
        return thread_id

    def _parse_thread_summary(self, value: Any) -> ThreadSummary:
        if not isinstance(value, Mapping):
            raise ProtocolIntegrityError("thread/list data contains a non-object")
        thread_id = self._nonempty_string(value.get("id"), "thread/list thread.id")
        cwd_raw = self._nonempty_string(value.get("cwd"), "thread/list thread.cwd")
        cwd = Path(cwd_raw)
        if not cwd.is_absolute():
            raise ProtocolIntegrityError("thread/list thread.cwd must be absolute")
        preview = value.get("preview")
        if not isinstance(preview, str):
            raise ProtocolIntegrityError("thread/list thread.preview must be a string")
        status_value = value.get("status")
        if isinstance(status_value, Mapping):
            status = self._nonempty_string(status_value.get("type"), "thread/list status.type")
        elif isinstance(status_value, str) and status_value:
            status = status_value
        else:
            raise ProtocolIntegrityError("thread/list thread.status is invalid")
        timestamps: list[int] = []
        for field in ("createdAt", "updatedAt"):
            raw = value.get(field)
            if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
                raise ProtocolIntegrityError(f"thread/list thread.{field} must be non-negative")
            timestamps.append(raw)
        raw_turns = value.get("turns")
        if not isinstance(raw_turns, list):
            raise ProtocolIntegrityError("thread/list thread.turns must be an array")
        turn_ids: list[str] = []
        for turn in raw_turns:
            if not isinstance(turn, Mapping):
                raise ProtocolIntegrityError("thread/list thread.turns contains a non-object")
            turn_ids.append(self._nonempty_string(turn.get("id"), "thread/list turn.id"))
        if len(set(turn_ids)) != len(turn_ids):
            raise ProtocolIntegrityError("thread/list thread contains duplicate turn ids")
        return ThreadSummary(
            thread_id=thread_id,
            cwd=cwd,
            preview=preview,
            status=status,
            created_at=timestamps[0],
            updated_at=timestamps[1],
            turn_ids=tuple(turn_ids),
        )

    def _parse_catalog_turn(self, value: Any) -> CatalogTurn:
        if not isinstance(value, Mapping):
            raise ProtocolIntegrityError("thread/read thread.turns contains a non-object")
        turn_id = self._nonempty_string(value.get("id"), "thread/read turn.id")
        status = value.get("status")
        if status not in _KNOWN_TURN_STATUSES:
            raise ProtocolIntegrityError("thread/read turn has an unknown status")
        raw_items = value.get("items")
        if not isinstance(raw_items, list) or any(
            not isinstance(item, Mapping) for item in raw_items
        ):
            raise ProtocolIntegrityError("thread/read turn.items must be an object array")
        final_messages: list[str] = []
        legacy_messages: list[str] = []
        for item in raw_items:
            if item.get("type") != "agentMessage":
                continue
            text = item.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            if item.get("phase") == "final_answer":
                final_messages.append(text)
            elif item.get("phase") is None:
                legacy_messages.append(text)
        candidates = final_messages or legacy_messages
        return CatalogTurn(turn_id, status, candidates[-1] if candidates else None)

    @staticmethod
    def _validate_effective_cwd(value: Any, workspace: Path, field: str) -> None:
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ProtocolIntegrityError(f"{field} must be an absolute path")
        try:
            expected = workspace.resolve(strict=True)
            actual = Path(value).resolve(strict=True)
        except OSError as exc:
            raise ProtocolIntegrityError(f"{field} is not a resolvable directory") from exc
        if actual != expected:
            raise ProtocolIntegrityError(f"{field} differs from the requested workspace")

    def _validate_effective_sandbox(
        self,
        sandbox: Any,
        permission_profile: Any,
        workspace: Path,
        options: ThreadOptions,
    ) -> None:
        if not isinstance(sandbox, Mapping):
            raise ProtocolIntegrityError("effective sandbox must be an object")
        if sandbox.get("type") != _SANDBOX_TYPES[options.sandbox]:
            raise ProtocolIntegrityError("effective sandbox differs from the request")
        if options.sandbox != "danger-full-access" and sandbox.get("networkAccess") is True:
            raise ProtocolIntegrityError("effective sandbox unexpectedly enables network")
        writable_roots = sandbox.get("writableRoots", [])
        if not isinstance(writable_roots, list):
            raise ProtocolIntegrityError("effective sandbox writableRoots must be an array")
        for raw_root in writable_roots:
            if not isinstance(raw_root, str) or not Path(raw_root).is_absolute():
                raise ProtocolIntegrityError("effective sandbox has an invalid writable root")
            try:
                resolved = Path(raw_root).resolve(strict=False)
                expected = workspace.resolve(strict=True)
            except OSError as exc:
                raise ProtocolIntegrityError("effective writable root is invalid") from exc
            if resolved != expected and expected not in resolved.parents:
                raise ProtocolIntegrityError("effective sandbox broadens writable roots")

        if permission_profile is None:
            return
        if not isinstance(permission_profile, Mapping):
            raise ProtocolIntegrityError("permissionProfile must be an object or null")
        network = permission_profile.get("network")
        if network is not None:
            if not isinstance(network, Mapping):
                raise ProtocolIntegrityError("permissionProfile.network must be an object")
            if options.sandbox != "danger-full-access" and network.get("enabled") is True:
                raise ProtocolIntegrityError("permissionProfile unexpectedly enables network")
        filesystem = permission_profile.get("fileSystem")
        if filesystem is None:
            return
        if not isinstance(filesystem, Mapping) or not isinstance(
            filesystem.get("entries"), list
        ):
            raise ProtocolIntegrityError("permissionProfile.fileSystem is malformed")
        for entry in filesystem["entries"]:
            if not isinstance(entry, Mapping):
                raise ProtocolIntegrityError("permissionProfile entry must be an object")
            if entry.get("access") != "write":
                continue
            if options.sandbox == "read-only":
                raise ProtocolIntegrityError("permissionProfile broadens read-only access")
            target = entry.get("path")
            if not isinstance(target, Mapping):
                raise ProtocolIntegrityError("write permission path is malformed")
            target_type = target.get("type")
            if target_type == "path":
                raw_path = target.get("path")
                if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
                    raise ProtocolIntegrityError("write permission path is invalid")
                resolved = Path(raw_path).resolve(strict=False)
                expected = workspace.resolve(strict=True)
                if resolved != expected and expected not in resolved.parents:
                    raise ProtocolIntegrityError("permissionProfile broadens write access")
            elif target_type == "special":
                special = target.get("value")
                kind = special.get("kind") if isinstance(special, Mapping) else None
                if kind not in {
                    "current_working_directory",
                    "project_roots",
                    "tmpdir",
                    "slash_tmp",
                }:
                    raise ProtocolIntegrityError("permissionProfile has broad special write access")
            else:
                raise ProtocolIntegrityError("permissionProfile has unsupported write access")

    @staticmethod
    def _include(target: dict[str, Any], key: str, value: Any) -> None:
        if value is not None:
            target[key] = value

    def _require_queue_capability(self) -> None:
        if self._experimental_api_version != EXPERIMENTAL_QUEUE_API_VERSION:
            from .base import UnsupportedCapability

            raise UnsupportedCapability("UNSUPPORTED_CAPABILITY")

    @staticmethod
    def _validate_page_limit(limit: int, method: str) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError(f"{method} limit must be an integer from 1 to 100")

    @staticmethod
    def _parse_page(result: Any, method: str, limit: int) -> tuple[list[Any], str | None]:
        if not isinstance(result, Mapping):
            raise ProtocolIntegrityError(f"{method} result must be an object")
        data = result.get("data")
        if not isinstance(data, list) or len(data) > limit:
            raise ProtocolIntegrityError(f"{method} result.data is invalid")
        cursor = result.get("nextCursor")
        if cursor is not None and (not isinstance(cursor, str) or not cursor):
            raise ProtocolIntegrityError(f"{method} nextCursor is invalid")
        return data, cursor

    def _parse_queued_submission(self, value: Any) -> QueuedSubmission:
        if not isinstance(value, Mapping):
            raise ProtocolIntegrityError("queued submission must be an object")
        submission_id = self._nonempty_string(value.get("id"), "queued submission id")
        client_id = self._nonempty_string(
            value.get("clientUserMessageId"),
            "queued submission clientUserMessageId",
        )
        raw_input = value.get("input")
        if not isinstance(raw_input, list) or not raw_input or any(
            not isinstance(item, Mapping) for item in raw_input
        ):
            raise ProtocolIntegrityError(
                "queued submission input must be a non-empty object array"
            )
        normalized = [dict(item) for item in raw_input]
        return QueuedSubmission(
            submission_id,
            client_id,
            sha256_text(canonical_json({"input": normalized})),
        )

    @staticmethod
    def _nonempty_string(value: Any, field: str) -> str:
        if not isinstance(value, str) or not value:
            raise ProtocolIntegrityError(f"{field} must be a non-empty string")
        return value

    @staticmethod
    def _result_object(result: Any, method: str, field: str) -> Mapping[str, Any]:
        if not isinstance(result, Mapping):
            raise ProtocolIntegrityError(f"{method} result must be an object")
        value = result.get(field)
        if not isinstance(value, Mapping):
            raise ProtocolIntegrityError(f"{method} result.{field} must be an object")
        return value
