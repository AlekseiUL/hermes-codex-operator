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
from .catalog import CatalogOperator
from .engine import AdapterFactory, Operator, OperatorOutcome
from .journal import CompletionEvent, JournalError, SQLiteJournal, WatchRecord
from .models import ContractValidationError, WorkContract, sha256_text
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

    catalog = commands.add_parser("catalog", help="list Codex threads read-only")
    catalog.add_argument("--json", dest="command_json", action="store_true", help=argparse.SUPPRESS)
    catalog.add_argument("--page-size", type=int, default=50)
    catalog.add_argument("--max-pages", type=int, default=20)
    catalog.add_argument("--cursor")
    catalog_commands = catalog.add_subparsers(dest="catalog_command")
    catalog_read = catalog_commands.add_parser("read", help="read one exact thread")
    catalog_read.add_argument("thread_id")
    catalog_read.add_argument("--include-result", action="store_true")
    catalog_read.add_argument(
        "--json",
        dest="command_json",
        action="store_true",
        default=argparse.SUPPRESS,
        help=argparse.SUPPRESS,
    )

    adopt = commands.add_parser("adopt", help="adopt an exact thread and turn read-only")
    adopt.add_argument("--json", dest="command_json", action="store_true", help=argparse.SUPPRESS)
    adopt.add_argument("thread_id")
    adopt.add_argument("turn_id")

    watch = commands.add_parser("watch", help="poll durable exact-ID watches")
    watch.add_argument("--json", dest="command_json", action="store_true", help=argparse.SUPPRESS)
    watch.add_argument("watch_id", nargs="?")

    events = commands.add_parser("events", help="list or acknowledge completion events")
    events.add_argument("--json", dest="command_json", action="store_true", help=argparse.SUPPRESS)
    event_commands = events.add_subparsers(dest="events_command", required=True)
    event_list = event_commands.add_parser("list", help="list completion events")
    event_list.add_argument(
        "--json",
        dest="command_json",
        action="store_true",
        default=argparse.SUPPRESS,
        help=argparse.SUPPRESS,
    )
    event_list.add_argument("--all", action="store_true", dest="include_acked")
    event_ack = event_commands.add_parser("ack", help="acknowledge one completion event")
    event_ack.add_argument(
        "--json",
        dest="command_json",
        action="store_true",
        default=argparse.SUPPRESS,
        help=argparse.SUPPRESS,
    )
    event_ack.add_argument("event_id")
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


def _watch_dict(watch: WatchRecord) -> dict[str, object]:
    return {
        "watch_id": watch.watch_id,
        "thread_id": watch.thread_id,
        "turn_id": watch.turn_id,
        "status": watch.state,
        "result_sha256": watch.result_sha256,
        "failure_code": watch.failure_code,
        "created_at": watch.created_at,
        "updated_at": watch.updated_at,
    }


def _event_dict(event: CompletionEvent) -> dict[str, object]:
    return {
        "event_id": event.event_id,
        "watch_id": event.watch_id,
        "thread_id": event.thread_id,
        "turn_id": event.turn_id,
        "status": event.status,
        "result_sha256": event.result_sha256,
        "created_at": event.created_at,
        "acked_at": event.acked_at,
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
            elif args.command == "events":
                if args.events_command == "ack":
                    value = {"ok": True, "event": _event_dict(journal.ack_event(args.event_id))}
                else:
                    value = {
                        "ok": True,
                        "events": [
                            _event_dict(item)
                            for item in journal.list_events(include_acked=args.include_acked)
                        ],
                    }
            elif args.command in {"catalog", "adopt", "watch"}:
                factory = adapter_factory or (lambda: AppServerAdapter())
                catalog_operator = CatalogOperator(journal, factory)
                if args.command == "catalog":
                    if args.catalog_command == "read":
                        result = asyncio.run(catalog_operator.read(args.thread_id))
                        value = {
                            "ok": True,
                            "thread": {
                                "thread_id": result.thread.thread_id,
                                "cwd": str(result.thread.cwd),
                                "preview": result.thread.preview,
                                "status": result.thread.status,
                                "created_at": result.thread.created_at,
                                "updated_at": result.thread.updated_at,
                                "turns": [
                                    {
                                        "turn_id": turn.turn_id,
                                        "status": turn.status,
                                        "result_sha256": (
                                            sha256_text(turn.final_text)
                                            if turn.final_text is not None
                                            else None
                                        ),
                                        "result": (
                                            turn.final_text if args.include_result else None
                                        ),
                                    }
                                    for turn in result.turns
                                ],
                            },
                        }
                    else:
                        result = asyncio.run(
                            catalog_operator.catalog(
                                page_size=args.page_size,
                                max_pages=args.max_pages,
                                cursor=args.cursor,
                            )
                        )
                        value = {
                            "ok": True,
                            "threads": [
                                {
                                    "thread_id": item.thread_id,
                                    "cwd": str(item.cwd),
                                    "preview": item.preview,
                                    "status": item.status,
                                    "created_at": item.created_at,
                                    "updated_at": item.updated_at,
                                    "turn_ids": list(item.turn_ids),
                                }
                                for item in result.threads
                            ],
                            "pages_read": result.pages_read,
                            "truncated": result.truncated,
                            "next_cursor": result.next_cursor,
                        }
                else:
                    result = asyncio.run(
                        catalog_operator.adopt(args.thread_id, args.turn_id)
                        if args.command == "adopt"
                        else catalog_operator.poll(args.watch_id)
                    )
                    value = {
                        "ok": all(item.state != "UNKNOWN" for item in result.watches),
                        "watches": [_watch_dict(item) for item in result.watches],
                        "events": [_event_dict(item) for item in result.events],
                    }
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
        if args.command in {"catalog", "events", "watch", "adopt"}:
            return 0 if value.get("ok") else 3
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
