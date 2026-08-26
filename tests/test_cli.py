from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hermes_codex_operator.adapters.fake import FakeBackend, FakeCodexAdapter
from hermes_codex_operator.adapters.app_server import EXPERIMENTAL_QUEUE_API_VERSION
from hermes_codex_operator.cli import default_state_path, main


class CliTests(unittest.TestCase):
    def test_default_state_path_uses_public_product_identity(self) -> None:
        self.assertEqual(
            default_state_path().parts[-3:],
            ("state", "hermes-codex-operator", "state.sqlite3"),
        )

    def test_doctor_reports_missing_codex_without_crashing(self) -> None:
        output = StringIO()
        with patch("hermes_codex_operator.cli.shutil.which", return_value=None):
            with redirect_stdout(output):
                code = main(["doctor", "--json"])
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["codex_found"])
        self.assertIsNone(payload["codex_version"])

    def test_doctor_reports_a_working_codex_executable(self) -> None:
        output = StringIO()
        completed = SimpleNamespace(returncode=0, stdout="codex-cli synthetic\n")
        with patch(
            "hermes_codex_operator.cli.shutil.which",
            return_value="/synthetic/bin/codex",
        ), patch(
            "hermes_codex_operator.cli.subprocess.run",
            return_value=completed,
        ):
            with redirect_stdout(output):
                code = main(["doctor", "--json"])
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["codex_version"], "codex-cli synthetic")

    def test_doctor_rejects_python_315_and_newer(self) -> None:
        output = StringIO()
        completed = SimpleNamespace(returncode=0, stdout="codex-cli synthetic\n")
        with patch(
            "hermes_codex_operator.cli.sys.version_info",
            (3, 15, 0),
        ), patch(
            "hermes_codex_operator.cli.shutil.which",
            return_value="/synthetic/bin/codex",
        ), patch(
            "hermes_codex_operator.cli.subprocess.run",
            return_value=completed,
        ):
            with redirect_stdout(output):
                code = main(["doctor", "--json"])
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["python_supported"])

    def test_submit_and_status_emit_stable_json_without_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            workspace = root_path / "acme-widget"
            (workspace / "src").mkdir(parents=True)
            (workspace / "src" / "app.py").write_text("OLD = True\n", encoding="utf-8")
            raw_prompt = "SYNTHETIC-PRIVATE-PROMPT-DO-NOT-PERSIST-001"
            contract = root_path / "contract.json"
            contract.write_text(
                json.dumps(
                    {
                        "schema_version": "1",
                        "task_id": "acme-widget",
                        "operation_id": "acme-widget-001",
                        "workspace": str(workspace),
                        "prompt": raw_prompt,
                        "allowed_change_globs": ["src/**"],
                        "verification": [[sys.executable, "-c", "raise SystemExit(0)"]],
                        "timeout_seconds": 5,
                    }
                ),
                encoding="utf-8",
            )
            state = root_path / "state" / "journal.sqlite3"
            backend = FakeBackend()
            output = StringIO()
            with redirect_stdout(output):
                code = main(
                    [
                        "--state",
                        str(state),
                        "submit",
                        "--json",
                        "--contract",
                        str(contract),
                        "--allow-root",
                        str(root_path),
                    ],
                    adapter_factory=lambda: FakeCodexAdapter(backend),
                )
            self.assertEqual(code, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["status"], "VERIFIED")
            self.assertEqual(payload["dispatch_kind"], "ordinary")
            self.assertNotIn(raw_prompt, output.getvalue())

            output = StringIO()
            with redirect_stdout(output):
                code = main(
                    [
                        "--state",
                        str(state),
                        "status",
                        "--json",
                        "acme-widget-001",
                    ]
                )
            self.assertEqual(code, 0)
            status = json.loads(output.getvalue())
            self.assertEqual(status["status"], "VERIFIED")
            self.assertEqual(status["dispatch_kind"], "ordinary")

    def test_contract_outside_allowed_root_fails_before_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as other:
            root_path = Path(root)
            workspace = Path(other, "acme-widget")
            workspace.mkdir()
            contract = root_path / "contract.json"
            contract.write_text(
                json.dumps(
                    {
                        "schema_version": "1",
                        "task_id": "acme-widget",
                        "operation_id": "acme-widget-001",
                        "workspace": str(workspace),
                        "prompt": "Synthetic task.",
                        "allowed_change_globs": ["**"],
                        "verification": [[sys.executable, "-c", "raise SystemExit(0)"]],
                        "timeout_seconds": 5,
                    }
                ),
                encoding="utf-8",
            )
            backend = FakeBackend()
            output = StringIO()
            with redirect_stdout(output):
                code = main(
                    [
                        "--state",
                        str(root_path / "state.sqlite3"),
                        "--json",
                        "submit",
                        "--contract",
                        str(contract),
                        "--allow-root",
                        str(root_path),
                    ],
                    adapter_factory=lambda: FakeCodexAdapter(backend),
                )
            self.assertEqual(code, 3)
            self.assertEqual(json.loads(output.getvalue())["status"], "FAILED")
            self.assertEqual(backend.start_turn_calls, 0)

    def test_catalog_adopt_watch_and_event_outbox_cli(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            state = root_path / "state.sqlite3"
            backend = FakeBackend()
            backend.threads[backend.thread_id] = root_path
            backend.turns[(backend.thread_id, backend.turn_id)] = "completed"
            def factory() -> FakeCodexAdapter:
                return FakeCodexAdapter(backend)

            output = StringIO()
            with redirect_stdout(output):
                code = main(
                    ["--state", str(state), "catalog", "--json"],
                    adapter_factory=factory,
                )
            catalog = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(catalog["threads"][0]["thread_id"], backend.thread_id)

            output = StringIO()
            with redirect_stdout(output):
                code = main(
                    [
                        "--state", str(state), "catalog", "read",
                        backend.thread_id, "--json",
                    ],
                    adapter_factory=factory,
                )
            readback = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(readback["thread"]["turns"][0]["turn_id"], backend.turn_id)
            self.assertIsNone(readback["thread"]["turns"][0]["result"])
            self.assertIsNotNone(readback["thread"]["turns"][0]["result_sha256"])

            output = StringIO()
            with redirect_stdout(output):
                code = main(
                    [
                        "--state", str(state), "catalog", "read",
                        backend.thread_id, "--include-result", "--json",
                    ],
                    adapter_factory=factory,
                )
            self.assertEqual(code, 0)
            self.assertEqual(
                json.loads(output.getvalue())["thread"]["turns"][0]["result"],
                backend.final_text,
            )

            output = StringIO()
            with redirect_stdout(output):
                code = main(
                    [
                        "--state", str(state), "adopt", "--json",
                        backend.thread_id, backend.turn_id,
                    ],
                    adapter_factory=factory,
                )
            adopted = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(adopted["watches"][0]["status"], "COMPLETED")
            self.assertEqual(len(adopted["events"]), 1)
            watch_id = adopted["watches"][0]["watch_id"]
            event_id = adopted["events"][0]["event_id"]

            output = StringIO()
            with redirect_stdout(output):
                code = main(
                    ["--state", str(state), "watch", "--json", watch_id],
                    adapter_factory=factory,
                )
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())["events"], [])

            output = StringIO()
            with redirect_stdout(output):
                code = main(["--state", str(state), "events", "list", "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())["events"][0]["event_id"], event_id)

            output = StringIO()
            with redirect_stdout(output):
                code = main(
                    ["--state", str(state), "events", "ack", event_id, "--json"]
                )
            self.assertEqual(code, 0)
            self.assertIsNotNone(json.loads(output.getvalue())["event"]["acked_at"])
            self.assertEqual(backend.start_thread_calls, 0)
            self.assertEqual(backend.resume_thread_calls, 0)
            self.assertEqual(backend.start_turn_calls, 0)
            self.assertEqual(backend.read_thread_calls, 2)

    def test_continue_cli_is_distinct_and_duplicate_is_not_dispatched(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            workspace = root_path / "workspace"
            (workspace / "src").mkdir(parents=True)
            contract = root_path / "contract.json"
            contract.write_text(
                json.dumps(
                    {
                        "schema_version": "1",
                        "task_id": "existing-task",
                        "operation_id": "continuation-001",
                        "workspace": str(workspace),
                        "prompt": "Continue the synthetic task once.",
                        "allowed_change_globs": ["src/**"],
                        "verification": [[sys.executable, "-c", "raise SystemExit(0)"]],
                        "timeout_seconds": 5,
                    }
                ),
                encoding="utf-8",
            )
            state = root_path / "state.sqlite3"
            backend = FakeBackend(
                thread_id="thread-existing",
                turn_id="turn-new",
            )
            backend.threads[backend.thread_id] = workspace
            backend.turns[(backend.thread_id, "turn-old")] = "completed"
            command = [
                "--state", str(state), "continue", "--json",
                "--contract", str(contract), "--thread-id", backend.thread_id,
                "--allow-root", str(root_path),
            ]
            output = StringIO()
            with redirect_stdout(output):
                code = main(command, adapter_factory=lambda: FakeCodexAdapter(backend))
            self.assertEqual(code, 0)
            continued = json.loads(output.getvalue())
            self.assertTrue(continued["dispatched"])
            self.assertEqual(continued["dispatch_kind"], "continue")

            output = StringIO()
            with redirect_stdout(output):
                code = main(command, adapter_factory=lambda: FakeCodexAdapter(backend))
            self.assertEqual(code, 0)
            self.assertFalse(json.loads(output.getvalue())["dispatched"])
            self.assertEqual(backend.start_turn_calls, 1)

    def test_queue_cli_requires_exact_experimental_version_and_active_owner(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            workspace = root_path / "workspace"
            (workspace / "src").mkdir(parents=True)
            contract = root_path / "contract.json"
            contract.write_text(
                json.dumps(
                    {
                        "schema_version": "1",
                        "task_id": "active-task",
                        "operation_id": "queue-001",
                        "workspace": str(workspace),
                        "prompt": "Queue the synthetic active task once.",
                        "allowed_change_globs": ["src/**"],
                        "verification": [[sys.executable, "-c", "raise SystemExit(0)"]],
                        "timeout_seconds": 5,
                    }
                ),
                encoding="utf-8",
            )
            backend = FakeBackend(thread_id="thread-active", turn_id="turn-queued")
            backend.threads[backend.thread_id] = workspace
            backend.turns[(backend.thread_id, "turn-active")] = "inProgress"  # type: ignore[assignment]
            backend.thread_statuses[backend.thread_id] = "active"
            backend.loaded_thread_ids = [backend.thread_id]
            base = [
                "--state", str(root_path / "state.sqlite3"), "queue", "--json",
                "--contract", str(contract), "--thread-id", backend.thread_id,
                "--allow-root", str(root_path), "--shared-app-server",
                "--experimental-api-version",
            ]

            output = StringIO()
            without_shared = [
                item for item in base if item != "--shared-app-server"
            ]
            with redirect_stdout(output):
                code = main(
                    [*without_shared, EXPERIMENTAL_QUEUE_API_VERSION],
                    adapter_factory=lambda: FakeCodexAdapter(backend),
                )
            self.assertEqual(code, 2)
            self.assertEqual(
                json.loads(output.getvalue())["error_code"],
                "UNSUPPORTED_CAPABILITY",
            )
            self.assertEqual(backend.queue_add_calls, 0)

            adapter_socket_paths: list[Path] = []

            def shared_adapter(*, experimental_api_version, unix_socket_path):
                adapter_socket_paths.append(unix_socket_path)
                self.assertEqual(
                    experimental_api_version,
                    EXPERIMENTAL_QUEUE_API_VERSION,
                )
                return FakeCodexAdapter(backend)

            output = StringIO()
            endpoint = SimpleNamespace(socket_path=Path("/synthetic/app-server.sock"))
            with patch(
                "hermes_codex_operator.cli.discover_managed_app_server",
                return_value=endpoint,
            ), patch(
                "hermes_codex_operator.cli.AppServerAdapter", side_effect=shared_adapter
            ):
                with redirect_stdout(output):
                    code = main([*base, EXPERIMENTAL_QUEUE_API_VERSION])
            self.assertEqual(code, 0)
            queued = json.loads(output.getvalue())
            self.assertEqual(queued["status"], "VERIFIED")
            self.assertEqual(queued["dispatch_kind"], "queue")
            self.assertEqual(
                adapter_socket_paths,
                [Path("/synthetic/app-server.sock")],
            )
            self.assertEqual(backend.queue_add_calls, 1)

            output = StringIO()
            with redirect_stdout(output):
                code = main(
                    [*base, "0.150.0"],
                    adapter_factory=lambda: FakeCodexAdapter(backend),
                )
            self.assertEqual(code, 2)
            self.assertEqual(
                json.loads(output.getvalue())["error_code"], "UNSUPPORTED_CAPABILITY"
            )
            self.assertEqual(backend.queue_add_calls, 1)


if __name__ == "__main__":
    unittest.main()
