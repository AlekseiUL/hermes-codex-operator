from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from hermes_codex_operator.models import (
    ChangedPathEvidence,
    ContractValidationError,
    OperationState,
    Receipt,
    VerificationEvidence,
    WorkContract,
    allowed_transitions,
    sha256_text,
)


def contract_value(workspace: Path) -> dict[str, object]:
    return {
        "schema_version": "1",
        "task_id": "synthetic-task",
        "operation_id": "synthetic-task-001",
        "workspace": str(workspace),
        "prompt": "Implement the synthetic behavior and run its tests.",
        "allowed_change_globs": ["src/**", "tests/**"],
        "verification": [["python3", "-m", "unittest", "discover", "-s", "tests"]],
        "timeout_seconds": 900,
        "model": "example-model",
        "reasoning_effort": "high",
    }


class WorkContractTests(unittest.TestCase):
    def test_parses_v1_and_builds_prompt_free_persistence_value(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            value = contract_value(Path(root))
            contract = WorkContract.from_dict(value)
            prompt = value["prompt"]
            assert isinstance(prompt, str)

        self.assertEqual(contract.schema_version, "1")
        self.assertEqual(contract.allowed_change_globs, ("src/**", "tests/**"))
        self.assertIsInstance(contract.verification[0], tuple)
        self.assertEqual(contract.prompt_sha256, sha256_text(prompt))
        self.assertNotIn("prompt", contract.persistence_dict())
        self.assertEqual(
            contract.persistence_dict()["prompt_sha256"], contract.prompt_sha256
        )
        self.assertNotIn(str(value["prompt"]), repr(contract))

    def test_json_parser_rejects_duplicate_unknown_and_unsupported_fields(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            value = contract_value(Path(root))
            value["unexpected"] = True
            with self.assertRaises(ContractValidationError):
                WorkContract.from_dict(value)

            value = contract_value(Path(root))
            value["schema_version"] = "2"
            with self.assertRaises(ContractValidationError):
                WorkContract.from_dict(value)

            raw = '{"schema_version":"1","schema_version":"1"}'
            with self.assertRaises(ContractValidationError):
                WorkContract.from_json(raw)

    def test_rejects_missing_required_field(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            value = contract_value(Path(root))
            del value["verification"]
            with self.assertRaises(ContractValidationError):
                WorkContract.from_dict(value)

    def test_rejects_invalid_identifiers_paths_and_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            cases = {
                "unsafe task ID": ("task_id", "../task"),
                "empty operation ID": ("operation_id", ""),
                "relative workspace": ("workspace", "relative/project"),
                "empty prompt": ("prompt", "  \n"),
                "NUL prompt": ("prompt", "do this\x00now"),
            }
            for label, (field, replacement) in cases.items():
                with self.subTest(label=label):
                    value = contract_value(Path(root))
                    value[field] = replacement
                    with self.assertRaises(ContractValidationError):
                        WorkContract.from_dict(value)

    def test_rejects_unsafe_globs_and_non_argv_verification(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            invalid_values = [
                ("allowed_change_globs", []),
                ("allowed_change_globs", ["../private/**"]),
                ("allowed_change_globs", ["/absolute/**"]),
                ("allowed_change_globs", "src/**"),
                ("verification", ["python3 -m unittest"]),
                ("verification", []),
                ("verification", [[]]),
                ("verification", [["python3", ""]]),
                ("verification", [["python3", 7]]),
            ]
            for field, replacement in invalid_values:
                with self.subTest(field=field, replacement=replacement):
                    value = contract_value(Path(root))
                    value[field] = replacement
                    with self.assertRaises(ContractValidationError):
                        WorkContract.from_dict(value)

    def test_rejects_invalid_timeout_and_settings(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            invalid_values = [
                ("timeout_seconds", True),
                ("timeout_seconds", 0),
                ("timeout_seconds", 86_401),
                ("model", "contains spaces"),
                ("reasoning_effort", 5),
                ("reasoning_effort", "ultra"),
            ]
            for field, replacement in invalid_values:
                with self.subTest(field=field):
                    value = contract_value(Path(root))
                    value[field] = replacement
                    with self.assertRaises(ContractValidationError):
                        WorkContract.from_dict(value)

    def test_direct_construction_cannot_bypass_validation(self) -> None:
        with self.assertRaises(ContractValidationError):
            WorkContract(
                schema_version="1",
                task_id="bad/id",
                operation_id="op-1",
                workspace="/synthetic/workspace",
                prompt="synthetic",
                allowed_change_globs=(),
                verification=(("python3", "-V"),),
                timeout_seconds=30,
            )


class StateAndReceiptTests(unittest.TestCase):
    def test_state_machine_has_no_ordinary_retry_from_unknown(self) -> None:
        self.assertIn(
            OperationState.STARTING_THREAD,
            allowed_transitions(OperationState.QUEUED),
        )
        self.assertIn(OperationState.UNKNOWN, allowed_transitions(OperationState.RUNNING))
        self.assertEqual(allowed_transitions(OperationState.UNKNOWN), frozenset())
        self.assertEqual(allowed_transitions(OperationState.VERIFIED), frozenset())

    def test_receipt_is_strict_and_contains_digests_not_private_values(self) -> None:
        digest = sha256_text("synthetic")
        value = {
            "schema_version": "1",
            "task_id": "synthetic-task",
            "operation_id": "synthetic-task-001",
            "status": "VERIFIED",
            "prompt_sha256": digest,
            "workspace_sha256": digest,
            "thread_sha256": digest,
            "turn_sha256": digest,
            "result_sha256": digest,
            "changed_paths": [{"path": "src/example.py", "sha256": digest}],
            "verification": [
                {
                    "command_sha256": digest,
                    "exit_code": 0,
                    "duration_ms": 12,
                    "output_sha256": digest,
                }
            ],
            "started_at": "2026-01-01T00:00:00Z",
            "completed_at": "2026-01-01T00:00:01Z",
            "failure_code": None,
        }
        receipt = Receipt.from_json(json.dumps(value))
        self.assertEqual(receipt.status, OperationState.VERIFIED)
        self.assertIsInstance(receipt.changed_paths[0], ChangedPathEvidence)
        self.assertIsInstance(receipt.verification[0], VerificationEvidence)
        self.assertEqual(Receipt.from_json(receipt.to_json()), receipt)
        self.assertEqual(len(receipt.sha256), 64)

        invalid = dict(value)
        invalid["prompt"] = "raw content must not be accepted"
        with self.assertRaises(ContractValidationError):
            Receipt.from_dict(invalid)

    def test_receipt_rejects_active_status_and_unsafe_changed_path(self) -> None:
        digest = sha256_text("synthetic")
        base = {
            "schema_version": "1",
            "task_id": "synthetic-task",
            "operation_id": "synthetic-task-001",
            "status": "RUNNING",
            "prompt_sha256": digest,
            "workspace_sha256": digest,
            "thread_sha256": digest,
            "turn_sha256": digest,
            "result_sha256": None,
            "changed_paths": [],
            "verification": [],
            "started_at": "2026-01-01T00:00:00+00:00",
            "completed_at": "2026-01-01T00:00:01+00:00",
            "failure_code": None,
        }
        with self.assertRaises(ContractValidationError):
            Receipt.from_dict(base)

        base["status"] = "VERIFIED"
        base["changed_paths"] = [{"path": "../escape", "sha256": digest}]
        with self.assertRaises(ContractValidationError):
            Receipt.from_dict(base)

        base["changed_paths"] = []
        with self.assertRaises(ContractValidationError):
            Receipt.from_dict(base)


if __name__ == "__main__":
    unittest.main()
