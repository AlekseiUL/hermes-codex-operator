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
            self.assertEqual(json.loads(output.getvalue())["status"], "VERIFIED")

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
            factory = lambda: FakeCodexAdapter(backend)

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


if __name__ == "__main__":
    unittest.main()
