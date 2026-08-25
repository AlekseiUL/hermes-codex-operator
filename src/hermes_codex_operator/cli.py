from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Sequence

from .adapters.app_server import AppServerAdapter
from .adapters.base import AdapterError, CodexAdapter
from .engine import AdapterFactory, Operator, OperatorOutcome
from .journal import JournalError, SQLiteJournal
from .models import ContractValidationError, WorkContract
from .workspace import WorkspacePolicy


def default_state_path() -> Path:
    return Path.home() / ".local" / "state" / "hermes-codex-operator" / "state.sqlite3"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hermes-codex",
        description="Run and verify one durable Codex task lifecycle.",
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=default_state_path(),
        help="local SQLite journal path",
    )
    parser.add_argument("--json", action="store_true", help="emit stable JSON")
    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor", help="check local runtime prerequisites")
    doctor.add_argument(
        "--json", dest="command_json", action="store_true", help=argparse.SUPPRESS
    )

    for name in ("submit", "recover", "collect", "abandon"):
        command = commands.add_parser(name, help=f"{name} a WorkContract")
        command.add_argument(
            "--json", dest="command_json", action="store_true", help=argparse.SUPPRESS
        )
        command.add_argument("--contract", required=True, help="JSON file or - for stdin")
        command.add_argument(
            "--allow-root",
            type=Path,
            action="append",
            default=[],
            help="allowed workspace root; repeatable; defaults to current directory",
        )

    status = commands.add_parser("status", help="read local operation state")
    status.add_argument(
        "--json", dest="command_json", action="store_true", help=argparse.SUPPRESS
    )
    status.add_argument("operation_id")
    return parser


def _read_contract(source: str) -> WorkContract:
    if source == "-":
        raw = sys.stdin.read()
    else:
        raw = Path(source).read_text(encoding="utf-8")
    return WorkContract.from_json(raw)


def _outcome_dict(outcome: OperatorOutcome) -> dict[str, object]:
    value: dict[str, object] = {
        "ok": outcome.operation.state.value == "VERIFIED",
        "task_id": outcome.operation.task_id,
        "operation_id": outcome.operation.operation_id,
        "status": outcome.operation.state.value,
        "dispatched": outcome.dispatched,
        "failure_code": outcome.operation.failure_code,
        "result": outcome.final_text,
        "receipt": outcome.receipt.to_dict() if outcome.receipt else None,
    }
    return value


def _operation_dict(operation: object) -> dict[str, object]:
    status = getattr(operation, "state").value
    return {
        "ok": status == "VERIFIED",
        "task_id": getattr(operation, "task_id"),
        "operation_id": getattr(operation, "operation_id"),
        "status": status,
        "failure_code": getattr(operation, "failure_code"),
        "created_at": getattr(operation, "created_at"),
        "updated_at": getattr(operation, "updated_at"),
    }


def _doctor() -> dict[str, object]:
    codex = shutil.which("codex")
    version: str | None = None
    if codex is not None:
        try:
            completed = subprocess.run(
                [codex, "--version"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
                check=False,
            )
            if completed.returncode == 0:
                version = completed.stdout.strip()[:128]
        except (OSError, subprocess.TimeoutExpired):
            version = None
    return {
        "ok": codex is not None and version is not None and sys.version_info >= (3, 11),
        "python_supported": sys.version_info >= (3, 11),
        "codex_found": codex is not None,
        "codex_version": version,
        "app_server_transport": "stdio",
    }


def _emit(value: dict[str, object], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return
    status = value.get("status")
    if status is not None:
        print(f"{value.get('operation_id')}: {status}")
    elif value.get("ok"):
        print("OK")
    else:
        print(f"ERROR: {value.get('error_code', 'failed')}")


def _error_value(exc: BaseException) -> dict[str, object]:
    if isinstance(exc, ContractValidationError):
        code = "invalid_contract"
    elif isinstance(exc, JournalError):
        code = exc.__class__.__name__.lower()
    elif isinstance(exc, FileNotFoundError):
        code = "file_not_found"
    else:
        code = "operation_failed"
    return {"ok": False, "error_code": code, "message": str(exc)[:500]}


def main(
    argv: Sequence[str] | None = None,
    *,
    adapter_factory: AdapterFactory | None = None,
) -> int:
    args = _parser().parse_args(argv)
    as_json = bool(args.json or getattr(args, "command_json", False))
    try:
        if args.command == "doctor":
            value = _doctor()
            _emit(value, as_json=as_json)
            return 0 if value["ok"] else 2

        with SQLiteJournal(args.state) as journal:
            if args.command == "status":
                value = _operation_dict(journal.get_operation(args.operation_id))
            else:
                contract = _read_contract(args.contract)
                roots = tuple(args.allow_root) or (Path.cwd(),)
                policy = WorkspacePolicy(tuple(Path(root).resolve() for root in roots))
                factory = adapter_factory or (lambda: AppServerAdapter())
                operator = Operator(journal, factory, policy)
                if args.command == "submit":
                    outcome = asyncio.run(operator.submit(contract))
                elif args.command == "collect":
                    outcome = asyncio.run(operator.collect(contract))
                elif args.command == "abandon":
                    outcome = asyncio.run(operator.abandon(contract))
                else:
                    outcome = asyncio.run(operator.recover(contract))
                value = _outcome_dict(outcome)
        _emit(value, as_json=as_json)
        status = value.get("status")
        return 0 if status == "VERIFIED" or args.command == "status" else 3
    except (
        OSError,
        ValueError,
        AdapterError,
        JournalError,
        ContractValidationError,
    ) as exc:
        value = _error_value(exc)
        _emit(value, as_json=as_json)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
