from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import signal
import socket
import sys
import tempfile
from types import SimpleNamespace
import unittest
from typing import Any, AsyncIterator
from unittest.mock import patch

from websockets.asyncio.server import Server, ServerConnection, unix_serve
from websockets.exceptions import ConnectionClosed

from hermes_codex_operator.adapters import (
    AppServerAdapter,
    AppServerProcessError,
    DeliveryUnknown,
    ProtocolIntegrityError,
    ReadbackMissingResult,
    RemoteTurnFailed,
    ThreadOptions,
    TurnInProgress,
    UnsupportedCapability,
)
from hermes_codex_operator.adapters.app_server import (
    EXPERIMENTAL_QUEUE_API_VERSION,
    QueueIdentityConflict,
    discover_managed_app_server,
)
from hermes_codex_operator.models import canonical_json, sha256_text


FIXTURE = Path(__file__).parent / "fixtures" / "fake_app_server.py"
THREAD_ID = "thread-synthetic-001"
TURN_ID = "turn-synthetic-001"


@asynccontextmanager
async def unix_queue_server(
    socket_path: Path,
    *,
    scenario: str = "happy",
) -> AsyncIterator[tuple[Server, list[str], asyncio.Event]]:
    methods: list[str] = []
    connection_closed = asyncio.Event()

    async def handler(connection: ServerConnection) -> None:
        assert connection.request is not None
        assert connection.request.path == "/rpc"

        async def send_result(request: dict[str, Any], result: Any) -> None:
            await connection.send(
                json.dumps(
                    {"id": request["id"], "result": result},
                    separators=(",", ":"),
                )
            )

        try:
            while True:
                raw = await connection.recv()
                message = json.loads(raw)
                assert isinstance(message, dict)
                method = message["method"]
                methods.append(method)
                if method == "initialize":
                    await send_result(
                        message,
                        {
                            "codexHome": os.fspath(socket_path.parent),
                            "platformFamily": "unix",
                            "platformOs": "synthetic",
                            "userAgent": (
                                "codex_cli_rs/0.150.0"
                                if scenario == "bad_version"
                                else "codex_cli_rs/0.149.0"
                            ),
                        },
                    )
                elif method == "initialized":
                    if scenario == "bad_json":
                        await connection.send("{")
                elif method == "thread/queue/list":
                    await send_result(message, {"data": [], "nextCursor": None})
                elif method == "thread/queue/add":
                    if scenario == "break_after_side_effect":
                        await connection.close()
                        return
                    await send_result(
                        message,
                        {
                            "queuedSubmission": {
                                "id": "submission-synthetic-001",
                                "clientUserMessageId": message["params"][
                                    "clientUserMessageId"
                                ],
                                "input": message["params"]["input"],
                            }
                        },
                    )
                elif method == "thread/queue/start":
                    await send_result(
                        message,
                        {"turn": {"id": TURN_ID, "status": "inProgress", "items": []}},
                    )
                else:
                    raise AssertionError(f"unexpected method: {method}")
        except ConnectionClosed:
            pass
        finally:
            connection_closed.set()

    server = await unix_serve(
        handler,
        path=os.fspath(socket_path),
        compression=None,
        ping_interval=None,
    )
    try:
        yield server, methods, connection_closed
    finally:
        server.close()
        await server.wait_closed()


class AppServerProtocolTests(unittest.IsolatedAsyncioTestCase):
    def adapter(
        self,
        scenario: str,
        *,
        request_timeout: float = 1.0,
        stderr_limit_bytes: int = 64 * 1024,
    ) -> AppServerAdapter:
        return AppServerAdapter(
            (sys.executable, os.fspath(FIXTURE), scenario),
            request_timeout=request_timeout,
            shutdown_timeout=0.5,
            stderr_limit_bytes=stderr_limit_bytes,
        )

    async def run_success(self, scenario: str = "happy"):
        with tempfile.TemporaryDirectory() as workspace_name:
            adapter = self.adapter(scenario)
            try:
                thread = await adapter.start_thread(Path(workspace_name))
                self.assertEqual(thread.thread_id, THREAD_ID)
                turn = await adapter.start_turn(
                    thread.thread_id, "Build the synthetic widget."
                )
                self.assertEqual(turn.turn_id, TURN_ID)
                status = await adapter.wait_for_completion(
                    thread.thread_id, turn.turn_id, 1.0
                )
                self.assertEqual(status, "completed")
                return await adapter.read_turn(thread.thread_id, turn.turn_id)
            finally:
                await adapter.close()

    def test_daemon_discovery_rejects_malformed_json(self) -> None:
        completed = SimpleNamespace(returncode=0, stdout="{")
        with patch(
            "hermes_codex_operator.adapters.app_server.subprocess.run",
            return_value=completed,
        ):
            with self.assertRaises(ProtocolIntegrityError):
                discover_managed_app_server(
                    expected_version=EXPERIMENTAL_QUEUE_API_VERSION
                )

    def test_daemon_discovery_rejects_missing_and_non_socket_path(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            regular_file = root_path / "not-a-socket"
            regular_file.write_text("synthetic", encoding="utf-8")
            for candidate in (root_path / "missing.sock", regular_file):
                with self.subTest(candidate=candidate.name):
                    completed = SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps(
                            {
                                "socketPath": os.fspath(candidate),
                                "appServerVersion": EXPERIMENTAL_QUEUE_API_VERSION,
                            }
                        ),
                    )
                    with patch(
                        "hermes_codex_operator.adapters.app_server.subprocess.run",
                        return_value=completed,
                    ):
                        with self.assertRaises(AppServerProcessError):
                            discover_managed_app_server(
                                expected_version=EXPERIMENTAL_QUEUE_API_VERSION
                            )

    @unittest.skipUnless(os.name == "posix", "Unix sockets are POSIX-specific")
    def test_daemon_discovery_rejects_version_mismatch_before_connection(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            socket_path = Path(root) / "app-server.sock"
            bound = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                bound.bind(os.fspath(socket_path))
                completed = SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "socketPath": os.fspath(socket_path),
                            "appServerVersion": "0.150.0",
                        }
                    ),
                )
                with patch(
                    "hermes_codex_operator.adapters.app_server.subprocess.run",
                    return_value=completed,
                ):
                    with self.assertRaises(UnsupportedCapability):
                        discover_managed_app_server(
                            expected_version=EXPERIMENTAL_QUEUE_API_VERSION
                        )
            finally:
                bound.close()

    @unittest.skipUnless(os.name == "posix", "Unix sockets are POSIX-specific")
    async def test_unix_socket_queue_end_to_end_uses_websockets_library(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            socket_path = Path(root) / "app-server.sock"
            async with unix_queue_server(socket_path) as (_, methods, _):
                adapter = AppServerAdapter(
                    experimental_api_version=EXPERIMENTAL_QUEUE_API_VERSION,
                    unix_socket_path=socket_path,
                    request_timeout=1.0,
                    shutdown_timeout=0.5,
                )
                try:
                    queue = await adapter.list_queue(THREAD_ID)
                    submission = await adapter.add_queue(
                        THREAD_ID, "Synthetic queued input.", "client-synthetic-001"
                    )
                    turn = await adapter.start_queued_turn(
                        THREAD_ID, submission.submission_id
                    )
                    self.assertEqual(queue.submissions, ())
                    self.assertEqual(turn.turn_id, TURN_ID)
                    self.assertIsNone(adapter._process)
                    self.assertEqual(
                        methods,
                        [
                            "initialize",
                            "initialized",
                            "thread/queue/list",
                            "thread/queue/add",
                            "thread/queue/start",
                        ],
                    )
                finally:
                    await adapter.close()

    @unittest.skipUnless(os.name == "posix", "Unix sockets are POSIX-specific")
    async def test_unix_socket_break_after_side_effect_is_delivery_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            socket_path = Path(root) / "app-server.sock"
            async with unix_queue_server(
                socket_path, scenario="break_after_side_effect"
            ) as (_, methods, _):
                adapter = AppServerAdapter(
                    experimental_api_version=EXPERIMENTAL_QUEUE_API_VERSION,
                    unix_socket_path=socket_path,
                    request_timeout=1.0,
                    shutdown_timeout=0.5,
                )
                try:
                    with self.assertRaises(DeliveryUnknown) as caught:
                        await adapter.add_queue(
                            THREAD_ID,
                            "Synthetic queued input.",
                            "client-synthetic-001",
                        )
                    self.assertEqual(caught.exception.method, "thread/queue/add")
                    self.assertTrue(caught.exception.sent)
                    self.assertEqual(methods.count("thread/queue/add"), 1)
                finally:
                    await adapter.close()

    @unittest.skipUnless(os.name == "posix", "Unix sockets are POSIX-specific")
    async def test_unix_socket_close_does_not_stop_listening_server(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            socket_path = Path(root) / "app-server.sock"
            async with unix_queue_server(socket_path) as (
                server,
                _,
                connection_closed,
            ):
                adapter = AppServerAdapter(
                    experimental_api_version=EXPERIMENTAL_QUEUE_API_VERSION,
                    unix_socket_path=socket_path,
                    request_timeout=1.0,
                    shutdown_timeout=0.5,
                )
                await adapter.start()
                await adapter.close()
                await asyncio.wait_for(connection_closed.wait(), timeout=1.0)
                self.assertTrue(server.is_serving())

    @unittest.skipUnless(os.name == "posix", "Unix sockets are POSIX-specific")
    async def test_unix_socket_rejects_bad_version_and_json(self) -> None:
        for scenario, expected in (
            ("bad_version", UnsupportedCapability),
            ("bad_json", ProtocolIntegrityError),
        ):
            with self.subTest(scenario=scenario):
                with tempfile.TemporaryDirectory() as root:
                    socket_path = Path(root) / "app-server.sock"
                    async with unix_queue_server(socket_path, scenario=scenario):
                        adapter = AppServerAdapter(
                            experimental_api_version=EXPERIMENTAL_QUEUE_API_VERSION,
                            unix_socket_path=socket_path,
                            request_timeout=1.0,
                            shutdown_timeout=0.5,
                        )
                        try:
                            if scenario == "bad_version":
                                with self.assertRaises(expected):
                                    await adapter.start()
                            else:
                                await adapter.start()
                                with self.assertRaises(expected):
                                    await adapter.list_queue(THREAD_ID)
                        finally:
                            await adapter.close()

    @unittest.skipUnless(os.name == "posix", "Unix sockets are POSIX-specific")
    async def test_unix_socket_missing_path_fails_without_spawning_process(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            adapter = AppServerAdapter(
                experimental_api_version=EXPERIMENTAL_QUEUE_API_VERSION,
                unix_socket_path=Path(root) / "missing.sock",
                request_timeout=1.0,
                shutdown_timeout=0.5,
            )
            try:
                with self.assertRaises(AppServerProcessError):
                    await adapter.start()
                self.assertIsNone(adapter._process)
            finally:
                await adapter.close()

    async def test_reads_exact_turn_and_final_agent_message_authoritatively(self) -> None:
        readback = await self.run_success()
        self.assertEqual(readback.thread_id, THREAD_ID)
        self.assertEqual(readback.turn_id, TURN_ID)
        self.assertEqual(readback.status, "completed")
        self.assertEqual(
            readback.final_text, "Synthetic verified implementation report."
        )
        self.assertEqual(readback.items[-1]["phase"], "final_answer")

    async def test_completion_notification_may_precede_turn_start_response(self) -> None:
        readback = await self.run_success("notification_before_response")
        self.assertEqual(readback.final_text, "Synthetic verified implementation report.")

    async def test_resume_requires_and_returns_exact_thread(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_name:
            adapter = self.adapter("resume")
            try:
                handle = await adapter.resume_thread(THREAD_ID, Path(workspace_name))
                self.assertEqual(handle.thread_id, THREAD_ID)
                self.assertEqual(handle.workspace, Path(workspace_name))
            finally:
                await adapter.close()

    async def test_resume_timeout_is_delivery_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_name:
            adapter = self.adapter("resume_timeout", request_timeout=0.15)
            try:
                with self.assertRaises(DeliveryUnknown) as caught:
                    await adapter.resume_thread(THREAD_ID, Path(workspace_name))
                self.assertEqual(caught.exception.method, "thread/resume")
                self.assertTrue(caught.exception.sent)
            finally:
                await adapter.close()

    async def test_catalog_paginates_with_strict_read_only_protocol(self) -> None:
        adapter = self.adapter("catalog_pages")
        try:
            first = await adapter.list_threads(limit=1)
            second = await adapter.list_threads(cursor=first.next_cursor, limit=1)
            self.assertEqual(first.threads[0].thread_id, THREAD_ID)
            self.assertEqual(first.threads[0].turn_ids, (TURN_ID,))
            self.assertEqual(first.next_cursor, "cursor-synthetic-2")
            self.assertEqual(second.threads[0].thread_id, "thread-synthetic-002")
            self.assertIsNone(second.next_cursor)
        finally:
            await adapter.close()

    async def test_catalog_rejects_malformed_thread_rows(self) -> None:
        adapter = self.adapter("catalog_malformed")
        try:
            with self.assertRaises(ProtocolIntegrityError):
                await adapter.list_threads(limit=1)
        finally:
            await adapter.close()

    async def test_catalog_reads_exact_thread_without_resuming_it(self) -> None:
        adapter = self.adapter("catalog_read")
        try:
            readback = await adapter.read_thread(THREAD_ID)
            self.assertEqual(readback.thread.thread_id, THREAD_ID)
            self.assertEqual(readback.thread.turn_ids, (TURN_ID,))
            self.assertEqual(readback.turns[0].status, "completed")
            self.assertEqual(
                readback.turns[0].final_text,
                "Synthetic verified implementation report.",
            )
            self.assertEqual(
                readback.turns[0].user_messages[0].client_message_id,
                "client-synthetic-001",
            )
            self.assertEqual(
                readback.turns[0].user_messages[0].input_sha256,
                sha256_text(
                    canonical_json(
                        {
                            "input": [
                                {
                                    "type": "text",
                                    "text": "Build the synthetic widget.",
                                    "text_elements": [],
                                }
                            ]
                        }
                    )
                ),
            )
        finally:
            await adapter.close()

    async def test_exact_experimental_queue_schema_is_opt_in(self) -> None:
        adapter = AppServerAdapter(
            (sys.executable, os.fspath(FIXTURE), "queue_protocol"),
            request_timeout=1.0,
            shutdown_timeout=0.5,
            experimental_api_version=EXPERIMENTAL_QUEUE_API_VERSION,
        )
        try:
            loaded = await adapter.list_loaded_threads(limit=100)
            self.assertEqual(loaded.thread_ids, (THREAD_ID,))
            queue = await adapter.list_queue(THREAD_ID, limit=100)
            self.assertEqual(queue.submissions, ())
            submission = await adapter.add_queue(
                THREAD_ID, "Synthetic queued input.", "client-synthetic-001"
            )
            self.assertEqual(submission.submission_id, "submission-synthetic-001")
            turn = await adapter.start_queued_turn(THREAD_ID, submission.submission_id)
            self.assertEqual(turn.turn_id, TURN_ID)
        finally:
            await adapter.close()

    async def test_queue_add_explicit_identity_conflict_is_distinct_from_malformed(self) -> None:
        for mode in ("client", "input"):
            with self.subTest(mode=mode):
                class ContradictoryQueueAdapter(AppServerAdapter):
                    async def _ensure_started(self) -> None:
                        return None

                    async def _request(self, method, params, *, side_effect):
                        queued_input = params["input"]
                        if mode == "input":
                            queued_input = [{"type": "text", "text": "Different input."}]
                        return {
                            "queuedSubmission": {
                                "id": "submission-response-conflict",
                                "clientUserMessageId": (
                                    "client-response-conflict"
                                    if mode == "client"
                                    else params["clientUserMessageId"]
                                ),
                                "input": queued_input,
                            }
                        }

                adapter = ContradictoryQueueAdapter(
                    experimental_api_version=EXPERIMENTAL_QUEUE_API_VERSION
                )
                try:
                    with self.assertRaises(QueueIdentityConflict):
                        await adapter.add_queue(
                            THREAD_ID,
                            "Synthetic queued input.",
                            "client-synthetic-001",
                        )
                finally:
                    await adapter.close()

    async def test_stable_adapter_rejects_queue_without_starting_transport(self) -> None:
        adapter = self.adapter("queue_protocol")
        try:
            with self.assertRaises(UnsupportedCapability):
                await adapter.list_queue(THREAD_ID)
            self.assertIsNone(adapter._process)
        finally:
            await adapter.close()

    def test_non_allowlisted_experimental_version_is_rejected(self) -> None:
        with self.assertRaises(UnsupportedCapability):
            AppServerAdapter(experimental_api_version="0.150.0")

    async def test_runtime_version_mismatch_rejects_before_queue_request(self) -> None:
        adapter = AppServerAdapter(
            (sys.executable, os.fspath(FIXTURE), "queue_protocol_wrong_version"),
            request_timeout=1.0,
            shutdown_timeout=0.5,
            experimental_api_version=EXPERIMENTAL_QUEUE_API_VERSION,
        )
        try:
            with self.assertRaises(UnsupportedCapability):
                await adapter.list_loaded_threads()
        finally:
            await adapter.close()

    async def test_missing_exact_turn_is_not_replaced_by_another_turn(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_name:
            adapter = self.adapter("missing_exact_turn")
            try:
                thread = await adapter.start_thread(Path(workspace_name))
                turn = await adapter.start_turn(thread.thread_id, "Build the synthetic widget.")
                await adapter.wait_for_completion(thread.thread_id, turn.turn_id, 1.0)
                with self.assertRaises(ReadbackMissingResult):
                    await adapter.read_turn(thread.thread_id, turn.turn_id)
            finally:
                await adapter.close()

    async def test_missing_final_readback_message_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_name:
            adapter = self.adapter("missing_final_message")
            try:
                thread = await adapter.start_thread(Path(workspace_name))
                turn = await adapter.start_turn(thread.thread_id, "Build the synthetic widget.")
                await adapter.wait_for_completion(thread.thread_id, turn.turn_id, 1.0)
                with self.assertRaises(ReadbackMissingResult):
                    await adapter.read_turn(thread.thread_id, turn.turn_id)
            finally:
                await adapter.close()

    async def test_response_without_result_is_protocol_error(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_name:
            adapter = self.adapter("response_without_result")
            try:
                thread = await adapter.start_thread(Path(workspace_name))
                turn = await adapter.start_turn(thread.thread_id, "Build the synthetic widget.")
                await adapter.wait_for_completion(thread.thread_id, turn.turn_id, 1.0)
                with self.assertRaises(ProtocolIntegrityError):
                    await adapter.read_turn(thread.thread_id, turn.turn_id)
            finally:
                await adapter.close()

    async def test_failed_turn_cannot_produce_successful_readback(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_name:
            adapter = self.adapter("remote_failed")
            try:
                thread = await adapter.start_thread(Path(workspace_name))
                turn = await adapter.start_turn(thread.thread_id, "Build the synthetic widget.")
                status = await adapter.wait_for_completion(
                    thread.thread_id, turn.turn_id, 1.0
                )
                self.assertEqual(status, "failed")
                with self.assertRaises(RemoteTurnFailed):
                    await adapter.read_turn(thread.thread_id, turn.turn_id)
            finally:
                await adapter.close()

    async def test_unknown_server_request_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_name:
            adapter = self.adapter("unknown_server_request")
            try:
                thread = await adapter.start_thread(Path(workspace_name))
                await adapter.start_turn(thread.thread_id, "Build the synthetic widget.")
                for _ in range(20):
                    await asyncio.sleep(0.01)
                    try:
                        await adapter.wait_for_completion(THREAD_ID, TURN_ID, 0.05)
                    except ProtocolIntegrityError as error:
                        self.assertIn("unsupported App Server request", str(error))
                        break
                    except Exception:
                        continue
                else:
                    self.fail("unknown server request did not fail the adapter")
            finally:
                await adapter.close()

    async def test_turn_start_timeout_is_delivery_unknown_and_not_retried(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_name:
            adapter = self.adapter("turn_timeout", request_timeout=0.15)
            try:
                thread = await adapter.start_thread(Path(workspace_name))
                with self.assertRaises(DeliveryUnknown) as caught:
                    await adapter.start_turn(
                        thread.thread_id, "Build the synthetic widget."
                    )
                self.assertEqual(caught.exception.method, "turn/start")
                self.assertTrue(caught.exception.sent)
                self.assertGreater(caught.exception.request_id, 0)
            finally:
                await adapter.close()

    async def test_cancelling_sent_turn_start_becomes_delivery_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_name:
            adapter = self.adapter("turn_delayed_response", request_timeout=5.0)
            try:
                thread = await adapter.start_thread(Path(workspace_name))
                pending = asyncio.create_task(
                    adapter.start_turn(thread.thread_id, "Build the synthetic widget.")
                )
                await asyncio.sleep(0.05)
                pending.cancel()
                with self.assertRaises(DeliveryUnknown) as caught:
                    await pending
                self.assertEqual(caught.exception.method, "turn/start")
                self.assertGreater(caught.exception.request_id, 0)
            finally:
                await adapter.close()

    async def test_successful_turn_start_response_without_identity_is_protocol_error(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_name:
            adapter = self.adapter("turn_start_missing_id")
            try:
                thread = await adapter.start_thread(Path(workspace_name))
                with self.assertRaises(ProtocolIntegrityError):
                    await adapter.start_turn(
                        thread.thread_id, "Build the synthetic widget."
                    )
            finally:
                await adapter.close()

    async def test_authoritative_read_reports_in_progress_without_protocol_failure(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_name:
            adapter = self.adapter("read_in_progress")
            try:
                thread = await adapter.start_thread(Path(workspace_name))
                turn = await adapter.start_turn(
                    thread.thread_id, "Build the synthetic widget."
                )
                with self.assertRaises(TurnInProgress):
                    await adapter.read_turn(thread.thread_id, turn.turn_id)
            finally:
                await adapter.close()

    async def test_resume_preserves_explicit_narrower_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_name:
            adapter = self.adapter("resume_read_only")
            try:
                handle = await adapter.resume_thread(
                    THREAD_ID,
                    Path(workspace_name),
                    ThreadOptions(sandbox="read-only", approval_policy="untrusted"),
                )
                self.assertEqual(handle.thread_id, THREAD_ID)
            finally:
                await adapter.close()

    async def test_effective_permission_broadening_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_name:
            adapter = self.adapter("policy_broadened")
            try:
                with self.assertRaises(ProtocolIntegrityError):
                    await adapter.start_thread(Path(workspace_name))
            finally:
                await adapter.close()

    async def test_stderr_is_drained_but_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_name:
            adapter = self.adapter("stderr", stderr_limit_bytes=128)
            try:
                await adapter.start_thread(Path(workspace_name))
                for _ in range(50):
                    if adapter.stderr_truncated:
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(adapter.stderr_truncated)
                self.assertEqual(len(adapter.stderr_excerpt.encode("utf-8")), 128)
            finally:
                await adapter.close()

    @unittest.skipUnless(os.name == "posix", "process-group assertion is POSIX-specific")
    async def test_close_reaps_the_app_server_process_group_leader(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_name:
            adapter = self.adapter("resume")
            await adapter.resume_thread(THREAD_ID, Path(workspace_name))
            process = adapter._process
            self.assertIsNotNone(process)
            assert process is not None
            pid = process.pid
            await adapter.close()
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, signal.SIGCONT)

    @unittest.skipUnless(os.name == "posix", "process-group assertion is POSIX-specific")
    async def test_close_finishes_cleanup_before_propagating_cancellation(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_name:
            adapter = self.adapter("resume")
            await adapter.resume_thread(THREAD_ID, Path(workspace_name))
            process = adapter._process
            assert process is not None
            original = adapter._terminate_process_group
            entered = asyncio.Event()
            release = asyncio.Event()

            async def delayed_termination() -> None:
                entered.set()
                await release.wait()
                await original()

            adapter._terminate_process_group = delayed_termination  # type: ignore[method-assign]
            closing = asyncio.create_task(adapter.close())
            await asyncio.wait_for(entered.wait(), timeout=1)
            closing.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await closing
            await adapter.close()
            with self.assertRaises(ProcessLookupError):
                os.kill(process.pid, signal.SIGCONT)

    @unittest.skipUnless(os.name == "posix", "process-group assertion is POSIX-specific")
    async def test_close_reaps_child_after_group_leader_exits(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_name:
            adapter = self.adapter("orphan_child")
            await adapter.start_thread(Path(workspace_name))
            child_pid = None
            for _ in range(100):
                for line in adapter.stderr_excerpt.splitlines():
                    if line.startswith("child-pid:"):
                        child_pid = int(line.split(":", 1)[1])
                        break
                if child_pid is not None:
                    break
                await asyncio.sleep(0.01)
            self.assertIsNotNone(child_pid)
            await adapter.close()
            assert child_pid is not None
            for _ in range(100):
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(0.01)
            else:
                self.fail("orphaned App Server child survived close")


if __name__ == "__main__":
    unittest.main()
