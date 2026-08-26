#!/usr/bin/env python3
"""Run an explicit disposable live smoke for the experimental shared queue.

The workspace and journal are removed by default. Codex may retain the synthetic
thread in the caller's normal local history.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Iterator, Sequence

from hermes_codex_operator.adapters.app_server import (
    AppServerAdapter,
    EXPERIMENTAL_QUEUE_API_VERSION,
)
from hermes_codex_operator.adapters.base import AdapterError
from hermes_codex_operator.journal import SQLiteJournal
from hermes_codex_operator.models import OperationState, WorkContract
from hermes_codex_operator.queueing import QueueContinuationOperator
from hermes_codex_operator.workspace import WorkspacePolicy


EXPECTED_BYTES = b"HCO_QUEUE_LIVE_OK\n"
TASK_ID = "hermes-codex-operator-queue-live-smoke"
OPERATION_ID = "hermes-codex-operator-queue-live-smoke-001"
OWNER_PROMPT = (
    "Run exactly this terminal command before replying: sleep 30. "
    "Do not create, delete, or modify any file. After the command exits, "
    "reply with the single word READY."
)

class SmokeFailure(RuntimeError):
    pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a synthetic active-thread queue through the real Codex App Server."
    )
    parser.add_argument(
        "--confirm-live-codex",
        action="store_true",
        help="acknowledge that this uses the local Codex account and leaves task history",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="keep the disposable workspace and journal for manual inspection",
    )
    parser.add_argument(
        "--owner-attempts",
        type=int,
        choices=(1, 2, 3),
        default=1,
        help=(
            "bounded synthetic owner setup attempts before queue/add; "
            "default: 1, maximum: 3"
        ),
    )
    return parser


@contextmanager
def _smoke_root(*, keep: bool) -> Iterator[Path]:
    if keep:
        root = Path(tempfile.mkdtemp(prefix="hermes-codex-queue-live-smoke-"))
        yield root
        return
    with tempfile.TemporaryDirectory(
        prefix="hermes-codex-queue-live-smoke-"
    ) as value:
        yield Path(value)


def _contract(root: Path) -> WorkContract:
    workspace = root / "workspace"
    workspace.mkdir()
    verification_code = (
        "from pathlib import Path; "
        f"raise SystemExit(0 if Path('queue-live.txt').read_bytes() == {EXPECTED_BYTES!r} else 1)"
    )
    return WorkContract.from_dict(
        {
            "schema_version": "1",
            "task_id": TASK_ID,
            "operation_id": OPERATION_ID,
            "workspace": os.fspath(workspace),
            "prompt": (
                "Create exactly one file named queue-live.txt with the exact UTF-8 "
                "content HCO_QUEUE_LIVE_OK followed by one newline. Do not create, "
                "delete, or modify any other file. Then report what you did."
            ),
            "allowed_change_globs": ["queue-live.txt"],
            "verification": [[sys.executable, "-c", verification_code]],
            "timeout_seconds": 300,
        }
    )


async def _run(root: Path, *, owner_attempts: int) -> dict[str, object]:
    contract = _contract(root)
    workspace = Path(contract.workspace)
    journal_path = root / "state.sqlite3"
    journal = SQLiteJournal(journal_path)
    adapter = AppServerAdapter(
        experimental_api_version=EXPERIMENTAL_QUEUE_API_VERSION,
        request_timeout=60.0,
    )
    try:
        thread = None
        preflight_interruptions: list[dict[str, object]] = []
        for attempt in range(1, owner_attempts + 1):
            candidate = await adapter.start_thread(workspace)
            await adapter.start_turn(candidate.thread_id, OWNER_PROMPT)
            owner = await adapter.read_thread(candidate.thread_id)
            owner_statuses = tuple(item.status for item in owner.turns)
            if owner.thread.status == "active" and sum(
                status == "inProgress" for status in owner_statuses
            ) == 1:
                thread = candidate
                break
            preflight_interruptions.append(
                {
                    "attempt": attempt,
                    "thread_status": owner.thread.status,
                    "turn_statuses": list(owner_statuses),
                }
            )
        if thread is None:
            raise SmokeFailure(
                "synthetic owner setup exhausted before queue dispatch: "
                f"attempts={preflight_interruptions!r}"
            )
        operator = QueueContinuationOperator(
            journal,
            lambda: adapter,
            WorkspacePolicy((root,)),
            experimental_api_version=EXPERIMENTAL_QUEUE_API_VERSION,
        )
        first = await operator.continue_active(contract, thread_id=thread.thread_id)
        if first.operation.state is not OperationState.VERIFIED or not first.dispatched:
            raise SmokeFailure(
                "queue did not reach a first-dispatch VERIFIED result: "
                f"state={first.operation.state} failure={first.operation.failure_code}"
            )
        artifact = workspace / "queue-live.txt"
        if artifact.read_bytes() != EXPECTED_BYTES:
            raise SmokeFailure("queued artifact content does not match the contract")

        duplicate = await operator.continue_active(contract, thread_id=thread.thread_id)
        if duplicate.operation.state is not OperationState.VERIFIED or duplicate.dispatched:
            raise SmokeFailure("same-process duplicate was not a non-dispatch observation")

        journal.close()
        reopened = SQLiteJournal(journal_path)
        try:
            restarted = await QueueContinuationOperator(
                reopened,
                lambda: AppServerAdapter(
                    experimental_api_version=EXPERIMENTAL_QUEUE_API_VERSION
                ),
                WorkspacePolicy((root,)),
                experimental_api_version=EXPERIMENTAL_QUEUE_API_VERSION,
            ).continue_active(contract, thread_id=thread.thread_id)
        finally:
            reopened.close()
        if restarted.operation.state is not OperationState.VERIFIED or restarted.dispatched:
            raise SmokeFailure("restart duplicate was not a non-dispatch observation")

        return {
            "ok": True,
            "status": "VERIFIED",
            "codex_version": EXPERIMENTAL_QUEUE_API_VERSION,
            "active_turn_created": True,
            "owner_attempts": len(preflight_interruptions) + 1,
            "preflight_interruptions": preflight_interruptions,
            "queued_turn_verified": True,
            "first_dispatched": True,
            "duplicate_dispatched": False,
            "restart_dispatched": False,
            "changed_paths": ["queue-live.txt"],
            "artifact_sha256": hashlib.sha256(EXPECTED_BYTES).hexdigest(),
            "codex_history_note": "synthetic thread may remain in local Codex history",
        }
    finally:
        journal.close()
        await adapter.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.confirm_live_codex:
        print(
            "Refusing to use the real local Codex account without "
            "--confirm-live-codex.",
            file=sys.stderr,
        )
        return 2
    if shutil.which("codex") is None:
        print("Codex CLI was not found on PATH.", file=sys.stderr)
        return 2

    try:
        with _smoke_root(keep=args.keep) as root:
            output = asyncio.run(_run(root, owner_attempts=args.owner_attempts))
            if args.keep:
                output["kept_workspace"] = os.fspath(root)
            print(json.dumps(output, sort_keys=True, separators=(",", ":")))
            return 0
    except (AdapterError, OSError, SmokeFailure, ValueError) as exc:
        detail = str(exc) or type(exc).__name__
        print(f"Live queue smoke failed: {detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
