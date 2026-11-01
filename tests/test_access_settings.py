from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Sequence
import unittest
from unittest.mock import patch

from hermes_codex_operator.adapters.app_server import EXPERIMENTAL_QUEUE_API_VERSION
from hermes_codex_operator.adapters.fake import FakeBackend, FakeCodexAdapter
from hermes_codex_operator.cli import ALLOW_SYMLINKS_ENV, SANDBOX_ENV, main
from hermes_codex_operator.engine import Operator
from hermes_codex_operator.journal import SQLiteJournal
from hermes_codex_operator.workspace import (
    SnapshotLimitExceeded,
    WorkspacePolicy,
    changed_paths,
    snapshot_from_dict,
    snapshot_to_dict,
    snapshot_workspace,
)


class AccessSettingsTests(unittest.TestCase):
    """Broader Codex access is caller configuration, never contract input."""

    def setUp(self) -> None:
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop(SANDBOX_ENV, None)
        os.environ.pop(ALLOW_SYMLINKS_ENV, None)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.workspace = self.root / "acme-widget"
        (self.workspace / "src").mkdir(parents=True)
        (self.workspace / "src" / "app.py").write_text("OLD = True\n", encoding="utf-8")
        self.backend = FakeBackend()

    def _contract(self, operation_id: str = "acme-widget-001", **extra: object) -> Path:
        path = self.root / f"{operation_id}.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": "1",
                    "task_id": "acme-widget",
                    "operation_id": operation_id,
                    "workspace": str(self.workspace),
                    "prompt": "Synthetic task.",
                    "allowed_change_globs": ["src/**"],
                    "verification": [[sys.executable, "-c", "raise SystemExit(0)"]],
                    "timeout_seconds": 5,
                    **extra,
                }
            ),
            encoding="utf-8",
        )
        return path

    def _run(
        self,
        *global_args: str,
        command: Sequence[str] = ("submit",),
        contract: Path | None = None,
    ) -> tuple[int, dict[str, Any]]:
        output = StringIO()
        with redirect_stdout(output):
            code = main(
                [
                    "--state",
                    str(self.root / "state.sqlite3"),
                    "--json",
                    *global_args,
                    *command,
                    "--contract",
                    str(contract or self._contract()),
                    "--allow-root",
                    str(self.root),
                ],
                adapter_factory=lambda: FakeCodexAdapter(self.backend),
            )
        return code, json.loads(output.getvalue())

    def _symlink(self, link: Path, target: Path) -> None:
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")

    def _sandbox_used(self) -> str:
        options = self.backend.last_thread_options
        self.assertIsNotNone(options)
        assert options is not None
        return options.sandbox

    # Sandbox selection

    def test_default_sandbox_is_workspace_write(self) -> None:
        code, payload = self._run()
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["status"], "VERIFIED")
        self.assertEqual(self._sandbox_used(), "workspace-write")
        assert self.backend.last_thread_options is not None
        self.assertEqual(self.backend.last_thread_options.approval_policy, "never")

    def test_flag_selects_full_access(self) -> None:
        code, payload = self._run("--sandbox", "danger-full-access")
        self.assertEqual(code, 0, payload)
        self.assertEqual(self._sandbox_used(), "danger-full-access")

    def test_environment_is_the_central_switch(self) -> None:
        os.environ[SANDBOX_ENV] = "danger-full-access"
        code, payload = self._run()
        self.assertEqual(code, 0, payload)
        self.assertEqual(self._sandbox_used(), "danger-full-access")

    def test_flag_overrides_environment(self) -> None:
        os.environ[SANDBOX_ENV] = "danger-full-access"
        code, payload = self._run("--sandbox", "read-only")
        self.assertEqual(code, 0, payload)
        self.assertEqual(self._sandbox_used(), "read-only")

    def test_follow_up_resume_uses_the_current_invocation_setting(self) -> None:
        code, payload = self._run("--sandbox", "danger-full-access")
        self.assertEqual(code, 0, payload)
        follow_up = self._contract("acme-widget-002")
        self.backend.turn_id = "turn-synthetic-002"
        code, payload = self._run("--sandbox", "read-only", contract=follow_up)
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.backend.resume_thread_calls, 1)
        self.assertEqual(self._sandbox_used(), "read-only")

    def test_continue_forwards_the_sandbox(self) -> None:
        self.backend.thread_id = "thread-existing"
        self.backend.turn_id = "turn-new"
        self.backend.threads[self.backend.thread_id] = self.workspace
        self.backend.turns[(self.backend.thread_id, "turn-old")] = "completed"
        code, payload = self._run(
            "--sandbox",
            "danger-full-access",
            command=("continue", "--thread-id", self.backend.thread_id),
        )
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["dispatch_kind"], "continue")
        self.assertEqual(self._sandbox_used(), "danger-full-access")

    def test_invalid_environment_sandbox_fails_before_dispatch(self) -> None:
        os.environ[SANDBOX_ENV] = "full"
        code, payload = self._run()
        self.assertEqual(code, 2)
        self.assertEqual(payload["error_code"], "invalid_configuration")
        self.assertEqual(self.backend.start_thread_calls, 0)

    def test_invalid_flag_sandbox_returns_json_error(self) -> None:
        code, payload = self._run("--sandbox", "full")
        self.assertEqual(code, 2)
        self.assertEqual(payload["error_code"], "invalid_configuration")
        self.assertIn("--sandbox", payload["message"])
        self.assertEqual(self.backend.start_thread_calls, 0)

    def test_empty_flag_sandbox_is_invalid_even_with_inherited_full_access(self) -> None:
        for env in (None, "danger-full-access"):
            with self.subTest(env=env):
                if env is None:
                    os.environ.pop(SANDBOX_ENV, None)
                else:
                    os.environ[SANDBOX_ENV] = env
                code, payload = self._run("--sandbox", "")
                self.assertEqual(code, 2)
                self.assertEqual(payload["error_code"], "invalid_configuration")
                self.assertEqual(self.backend.start_thread_calls, 0)

    def test_empty_environment_sandbox_means_unset(self) -> None:
        os.environ[SANDBOX_ENV] = ""
        code, payload = self._run()
        self.assertEqual(code, 0, payload)
        self.assertEqual(self._sandbox_used(), "workspace-write")

    def test_contract_cannot_select_its_own_sandbox(self) -> None:
        contract = self._contract(sandbox="danger-full-access")
        code, payload = self._run(contract=contract)
        self.assertEqual(code, 2)
        self.assertEqual(payload["error_code"], "invalid_contract")
        self.assertEqual(self.backend.start_thread_calls, 0)

    def test_queue_rejects_a_sandbox_it_cannot_apply(self) -> None:
        queue = (
            "queue",
            "--thread-id",
            "thread-active",
            "--shared-app-server",
            "--experimental-api-version",
            EXPERIMENTAL_QUEUE_API_VERSION,
        )
        for flag, env in ((("--sandbox", "read-only"), None), ((), "danger-full-access")):
            with self.subTest(flag=flag, env=env):
                if env is None:
                    os.environ.pop(SANDBOX_ENV, None)
                else:
                    os.environ[SANDBOX_ENV] = env
                code, payload = self._run(*flag, command=queue)
                self.assertEqual(code, 2)
                self.assertEqual(payload["error_code"], "invalid_configuration")
                self.assertEqual(self.backend.queue_add_calls, 0)

    def test_operator_rejects_unknown_sandbox(self) -> None:
        journal = SQLiteJournal(self.root / "direct.sqlite3")
        self.addCleanup(journal.close)
        with self.assertRaises(ValueError):
            Operator(
                journal,
                FakeCodexAdapter,
                WorkspacePolicy((self.root,)),
                sandbox="full",  # type: ignore[arg-type]
            )

    # Symlinks

    def test_symlink_is_rejected_by_default(self) -> None:
        self._symlink(self.workspace / "local-config", self.root / "outside.txt")
        code, payload = self._run()
        self.assertEqual(code, 3)
        self.assertEqual(payload["status"], "FAILED")
        self.assertEqual(payload["failure_code"], "workspace_policy_failed")
        self.assertEqual(self.backend.start_turn_calls, 0)

    def test_unchanged_symlink_is_recorded_but_not_a_change(self) -> None:
        self._symlink(self.workspace / "local-config", self.root / "outside.txt")
        code, payload = self._run("--allow-symlinks")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["status"], "VERIFIED")
        self.assertEqual(payload["receipt"]["changed_paths"], [])

    def test_allow_symlinks_environment(self) -> None:
        self._symlink(self.workspace / "local-config", self.root / "outside.txt")
        os.environ[ALLOW_SYMLINKS_ENV] = "1"
        code, payload = self._run()
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["status"], "VERIFIED")

    def test_new_symlink_outside_scope_is_a_scope_violation(self) -> None:
        def create_link(workspace: Path) -> None:
            os.symlink(workspace / "src" / "app.py", workspace / "out-of-scope-link")

        self._symlink(self.root / "symlink-support-probe", self.root / "outside.txt")
        self.backend.mutate_workspace = create_link
        code, payload = self._run("--allow-symlinks")
        self.assertEqual(code, 3)
        self.assertEqual(payload["status"], "SCOPE_VIOLATION")
        self.assertEqual(payload["failure_code"], "changed_path_outside_scope")
        changed = [item["path"] for item in payload["receipt"]["changed_paths"]]
        self.assertIn("out-of-scope-link", changed)

    def test_retargeted_symlink_in_scope_is_change_evidence(self) -> None:
        self._symlink(self.workspace / "src" / "current", self.workspace / "src" / "app.py")
        (self.workspace / "src" / "next.py").write_text("NEW = True\n", encoding="utf-8")

        def retarget(workspace: Path) -> None:
            link = workspace / "src" / "current"
            link.unlink()
            os.symlink(workspace / "src" / "next.py", link)

        self.backend.mutate_workspace = retarget
        code, payload = self._run("--allow-symlinks")
        self.assertEqual(code, 0, payload)
        changed = [item["path"] for item in payload["receipt"]["changed_paths"]]
        self.assertEqual(changed, ["src/current"])

    def test_symlinked_workspace_root_stays_rejected(self) -> None:
        alias = self.root / "alias"
        self._symlink(alias, self.workspace)
        contract = self._contract(workspace=str(alias))
        code, payload = self._run("--allow-symlinks", contract=contract)
        self.assertEqual(code, 3)
        self.assertEqual(payload["failure_code"], "workspace_policy_failed")
        self.assertEqual(self.backend.start_turn_calls, 0)

    def test_positional_policy_arguments_keep_their_meaning(self) -> None:
        policy = WorkspacePolicy((self.root,), True, 1)
        self.assertEqual(policy.max_files, 1)
        self.assertFalse(policy.record_symlinks)

    def test_recorded_links_are_not_followed_and_count_against_limits(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "unread.txt").write_text("synthetic", encoding="utf-8")
        workspace = self.root / "links"
        workspace.mkdir()
        (workspace / "ordinary").write_bytes(b"ab")
        try:
            (workspace / "directory-link").symlink_to(outside, target_is_directory=True)
            (workspace / "dangling").symlink_to("missing")
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        policy = WorkspacePolicy((self.root,), max_files=3, record_symlinks=True)

        before = snapshot_workspace(workspace, policy)
        self.assertEqual(set(before.files), {"ordinary", "directory-link", "dangling"})
        self.assertEqual(before.total_bytes, 2)
        self.assertEqual(before, snapshot_from_dict(json.loads(json.dumps(snapshot_to_dict(before)))))

        (outside / "unread.txt").write_text("changed outside", encoding="utf-8")
        self.assertEqual(snapshot_workspace(workspace, policy), before)

        (workspace / "directory-link").unlink()
        (workspace / "directory-link").symlink_to(self.root, target_is_directory=True)
        after = snapshot_workspace(workspace, policy)
        self.assertEqual(changed_paths(before, after), ("directory-link",))
        self.assertNotEqual(before.root_sha256, after.root_sha256)

        (workspace / "extra-link").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(SnapshotLimitExceeded):
            snapshot_workspace(workspace, policy)

    def test_link_baseline_survives_unknown_and_recovery(self) -> None:
        self._symlink(self.workspace / "src" / "current", Path("old-target"))

        def retarget(workspace: Path) -> None:
            link = workspace / "src" / "current"
            link.unlink()
            link.symlink_to("new-target")

        self.backend.mutate_workspace = retarget
        self.backend.fail_at = "wait_transport_lost"
        contract = self._contract()
        code, payload = self._run(
            "--allow-symlinks", "--sandbox", "danger-full-access", contract=contract
        )
        self.assertEqual(payload["status"], "UNKNOWN", payload)

        self.backend.fail_at = None
        code, payload = self._run(
            "--allow-symlinks",
            "--sandbox",
            "read-only",
            command=("recover",),
            contract=contract,
        )
        self.assertEqual(payload["status"], "VERIFIED", payload)
        changed = [item["path"] for item in payload["receipt"]["changed_paths"]]
        self.assertEqual(changed, ["src/current"])
        self.assertEqual(self.backend.start_turn_calls, 1)
        self.assertEqual(self._sandbox_used(), "read-only")

    def test_invalid_allow_symlinks_environment_fails(self) -> None:
        os.environ[ALLOW_SYMLINKS_ENV] = "maybe"
        code, payload = self._run()
        self.assertEqual(code, 2)
        self.assertEqual(payload["error_code"], "invalid_configuration")
        self.assertEqual(self.backend.start_thread_calls, 0)

    # Reporting

    def test_doctor_reports_effective_settings(self) -> None:
        os.environ[SANDBOX_ENV] = "danger-full-access"
        os.environ[ALLOW_SYMLINKS_ENV] = "true"
        output = StringIO()
        with patch("hermes_codex_operator.cli.shutil.which", return_value=None):
            with redirect_stdout(output):
                main(["doctor", "--json"])
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["sandbox"], "danger-full-access")
        self.assertTrue(payload["allow_symlinks"])


if __name__ == "__main__":
    unittest.main()
