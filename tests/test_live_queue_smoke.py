from __future__ import annotations

import asyncio
from contextlib import redirect_stderr
import importlib.util
from io import StringIO
import os
from pathlib import Path
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hermes_codex_operator.adapters.base import RemoteRequestError


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "live_queue_smoke.py"


def load_script_module():
    spec = importlib.util.spec_from_file_location(
        "synthetic_live_queue_smoke",
        SCRIPT,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load live queue smoke module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LiveQueueSmokeSafetyTests(unittest.TestCase):
    def test_owner_readiness_retries_only_empty_session_race(self) -> None:
        module = load_script_module()

        class Adapter:
            calls = 0

            async def read_thread(self, thread_id):
                self.calls += 1
                if self.calls < 3:
                    raise RemoteRequestError(
                        "thread/read",
                        code=-1,
                        message=(
                            "failed to read thread: failed to read session metadata "
                            "/synthetic/rollout.jsonl: rollout is empty"
                        ),
                    )
                return SimpleNamespace(thread_id=thread_id)

        adapter = Adapter()
        result = asyncio.run(
            module._read_owner_when_ready(
                adapter, "thread-synthetic", attempts=3, delay_seconds=0
            )
        )
        self.assertEqual(result.thread_id, "thread-synthetic")
        self.assertEqual(adapter.calls, 3)

    def test_owner_readiness_does_not_retry_other_remote_errors(self) -> None:
        module = load_script_module()

        class Adapter:
            calls = 0

            async def read_thread(self, thread_id):
                self.calls += 1
                raise RemoteRequestError(
                    "thread/read", code=-1, message="synthetic permission failure"
                )

        adapter = Adapter()
        with self.assertRaises(RemoteRequestError):
            asyncio.run(
                module._read_owner_when_ready(
                    adapter, "thread-synthetic", attempts=3, delay_seconds=0
                )
            )
        self.assertEqual(adapter.calls, 1)

    def run_script(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        source = os.fspath(ROOT / "src")
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = source if not existing else source + os.pathsep + existing
        return subprocess.run(
            [sys.executable, os.fspath(SCRIPT), *args],
            cwd=ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )

    def test_refuses_live_account_without_explicit_confirmation(self) -> None:
        completed = self.run_script()

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, "")
        self.assertIn("--confirm-live-codex", completed.stderr)

    def test_help_is_offline_and_documents_confirmation_gate(self) -> None:
        completed = self.run_script("--help")

        self.assertEqual(completed.returncode, 0)
        self.assertIn("--confirm-live-codex", completed.stdout)
        self.assertIn("--owner-attempts {1,2,3}", completed.stdout)
        self.assertIn("default: 1, maximum: 3", completed.stdout)
        self.assertEqual(completed.stderr, "")

    def test_owner_fixture_uses_direct_sleep_without_nested_python(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")

        self.assertIn("terminal command before replying: sleep 30", source)
        self.assertNotIn("python3 -c 'import time; time.sleep", source)

    def test_live_path_uses_direct_unix_socket_and_the_public_queue_cli(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")

        self.assertIn("discover_managed_app_server", source)
        self.assertIn("unix_socket_path=socket_path", source)
        self.assertIn('"hermes_codex_operator.cli"', source)
        self.assertIn('"--shared-app-server"', source)
        self.assertIn('"app-server", "daemon", "version"', source)

    def test_confirmed_path_fails_cleanly_when_managed_daemon_is_absent(self) -> None:
        module = load_script_module()
        stderr = StringIO()
        completed = subprocess.CompletedProcess(
            args=["synthetic-codex"],
            returncode=1,
            stdout="",
            stderr="synthetic absent daemon",
        )
        with patch.object(module.shutil, "which", return_value="synthetic-codex"):
            with patch(
                "hermes_codex_operator.adapters.app_server.subprocess.run",
                return_value=completed,
            ):
                with redirect_stderr(stderr):
                    code = module.main(["--confirm-live-codex"])

        self.assertEqual(code, 2)
        self.assertIn("daemon start", stderr.getvalue())

    def test_verified_payload_binds_result_artifact_and_receipt(self) -> None:
        module = load_script_module()
        result = "synthetic authoritative result"
        artifact_sha256 = module.hashlib.sha256(module.EXPECTED_BYTES).hexdigest()
        payload = {
            "task_id": module.TASK_ID,
            "operation_id": module.OPERATION_ID,
            "status": "VERIFIED",
            "dispatch_kind": "queue",
            "result": result,
            "receipt": {
                "task_id": module.TASK_ID,
                "operation_id": module.OPERATION_ID,
                "status": "VERIFIED",
                "changed_paths": [
                    {"path": "queue-live.txt", "sha256": artifact_sha256}
                ],
                "result_sha256": module.hashlib.sha256(
                    result.encode("utf-8")
                ).hexdigest(),
                "prompt_sha256": "d" * 64,
                "workspace_sha256": "e" * 64,
                "thread_sha256": "a" * 64,
                "turn_sha256": "b" * 64,
                "verification": [{"exit_code": 0}],
            },
        }

        receipt = module._validate_verified_payload(payload)
        self.assertEqual(receipt, payload["receipt"])
        payload["receipt"]["changed_paths"][0]["sha256"] = "c" * 64
        with self.assertRaises(module.SmokeFailure):
            module._validate_verified_payload(payload)

    def test_restart_payload_keeps_receipt_without_exposing_result(self) -> None:
        module = load_script_module()
        artifact_sha256 = module.hashlib.sha256(module.EXPECTED_BYTES).hexdigest()
        receipt = {
            "task_id": module.TASK_ID,
            "operation_id": module.OPERATION_ID,
            "status": "VERIFIED",
            "changed_paths": [
                {"path": "queue-live.txt", "sha256": artifact_sha256}
            ],
            "prompt_sha256": "d" * 64,
            "workspace_sha256": "e" * 64,
            "result_sha256": "f" * 64,
            "thread_sha256": "a" * 64,
            "turn_sha256": "b" * 64,
            "verification": [{"exit_code": 0}],
        }
        payload = {
            "task_id": module.TASK_ID,
            "operation_id": module.OPERATION_ID,
            "status": "VERIFIED",
            "dispatch_kind": "queue",
            "result": None,
            "receipt": receipt,
        }

        self.assertEqual(
            module._validate_verified_payload(payload, expect_result=False),
            receipt,
        )
        payload["result"] = "must stay private on duplicate observation"
        with self.assertRaises(module.SmokeFailure):
            module._validate_verified_payload(payload, expect_result=False)


if __name__ == "__main__":
    unittest.main()
