from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "live_queue_smoke.py"


class LiveQueueSmokeSafetyTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
