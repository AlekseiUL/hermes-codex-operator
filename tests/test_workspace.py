from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

from hermes_codex_operator.verification import run_verification
from hermes_codex_operator.workspace import (
    ScopeViolation,
    SnapshotLimitExceeded,
    UnsafeWorkspaceEntry,
    WorkspaceOutsideRoots,
    WorkspacePolicy,
    changed_paths,
    enforce_scope,
    resolve_workspace,
    snapshot_workspace,
)


class WorkspaceTests(unittest.TestCase):
    def test_resolves_workspace_under_allowed_root(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            project = Path(root, "acme-widget")
            project.mkdir()
            policy = WorkspacePolicy((Path(root),))
            self.assertEqual(resolve_workspace(project, policy), project.resolve())

    def test_rejects_workspace_outside_allowed_root(self) -> None:
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as other:
            policy = WorkspacePolicy((Path(root),))
            with self.assertRaises(WorkspaceOutsideRoots):
                resolve_workspace(Path(other), policy)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_rejects_internal_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as other:
            project = Path(root, "acme-widget")
            project.mkdir()
            Path(other, "private.txt").write_text("synthetic", encoding="utf-8")
            Path(project, "escape").symlink_to(other, target_is_directory=True)
            policy = WorkspacePolicy((Path(root),))
            with self.assertRaises(UnsafeWorkspaceEntry):
                snapshot_workspace(project, policy)

    def test_snapshot_detects_add_modify_delete_and_scope(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            project = Path(root, "acme-widget")
            Path(project, "src").mkdir(parents=True)
            Path(project, "src", "app.py").write_text("OLD = True\n", encoding="utf-8")
            Path(project, "obsolete.txt").write_text("old\n", encoding="utf-8")
            policy = WorkspacePolicy((Path(root),))
            before = snapshot_workspace(project, policy)
            Path(project, "src", "app.py").write_text("NEW = True\n", encoding="utf-8")
            Path(project, "tests").mkdir()
            Path(project, "tests", "test_app.py").write_text("pass\n", encoding="utf-8")
            Path(project, "obsolete.txt").unlink()
            after = snapshot_workspace(project, policy)
            changed = changed_paths(before, after)
            self.assertEqual(changed, ("obsolete.txt", "src/app.py", "tests/test_app.py"))
            with self.assertRaises(ScopeViolation) as caught:
                enforce_scope(changed, ("src/**", "tests/**"))
            self.assertEqual(caught.exception.paths, ("obsolete.txt",))

    def test_snapshot_enforces_limits(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            project = Path(root, "acme-widget")
            project.mkdir()
            Path(project, "large.bin").write_bytes(b"x" * 11)
            policy = WorkspacePolicy((Path(root),), max_bytes=10)
            with self.assertRaises(SnapshotLimitExceeded):
                snapshot_workspace(project, policy)

    def test_single_star_does_not_cross_a_path_separator(self) -> None:
        enforce_scope(("src/app.py",), ("src/*.py",))
        with self.assertRaises(ScopeViolation):
            enforce_scope(("src/deep/app.py",), ("src/*.py",))
        enforce_scope(("src/deep/app.py",), ("src/**",))


class VerificationTests(unittest.TestCase):
    def test_verification_passes_and_hashes_output(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            result = run_verification(
                (sys.executable, "-c", "print('synthetic-ok')"),
                workspace=Path(root),
                timeout_seconds=2,
            )
            self.assertTrue(result.passed)
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.failure_code, None)
            self.assertEqual(len(result.stdout_sha256), 64)

    def test_verification_failure_and_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            failed = run_verification(
                (sys.executable, "-c", "raise SystemExit(7)"),
                workspace=Path(root),
                timeout_seconds=2,
            )
            self.assertFalse(failed.passed)
            self.assertEqual(failed.exit_code, 7)
            self.assertEqual(failed.failure_code, "nonzero_exit")

            timed_out = run_verification(
                (sys.executable, "-c", "import time; time.sleep(2)"),
                workspace=Path(root),
                timeout_seconds=0.05,
            )
            self.assertFalse(timed_out.passed)
            self.assertEqual(timed_out.failure_code, "timed_out")

    def test_verification_never_interprets_shell_syntax(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            sentinel = Path(root, "should-not-exist")
            argument = f";touch {sentinel}"
            result = run_verification(
                (sys.executable, "-c", "import sys; print(sys.argv[1])", argument),
                workspace=Path(root),
                timeout_seconds=2,
            )
            self.assertTrue(result.passed)
            self.assertFalse(sentinel.exists())

    def test_verification_start_error_is_a_failed_result(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            command = Path(root, "not-executable")
            command.write_text("synthetic\n", encoding="utf-8")
            command.chmod(0o600)
            result = run_verification((str(command),), workspace=Path(root))
            self.assertFalse(result.passed)
            self.assertEqual(result.failure_code, "command_start_failed")

    @unittest.skipUnless(os.name == "posix", "process-group cleanup is POSIX-specific")
    def test_successful_verification_cannot_leave_late_writer(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            child = (
                "import time; from pathlib import Path; "
                "time.sleep(0.35); Path('late.txt').write_text('late')"
            )
            leader = (
                "import subprocess, sys; "
                f"subprocess.Popen([sys.executable, '-c', {child!r}], "
                "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
                "stderr=subprocess.DEVNULL)"
            )
            result = run_verification(
                (sys.executable, "-c", leader),
                workspace=Path(root),
                timeout_seconds=2,
            )
            self.assertTrue(result.passed)
            time.sleep(0.5)
            self.assertFalse(Path(root, "late.txt").exists())


if __name__ == "__main__":
    unittest.main()
