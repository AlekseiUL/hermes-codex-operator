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
    discover_managed_app_server,
)
from hermes_codex_operator.adapters.base import AdapterError, RemoteRequestError
from hermes_codex_operator.models import WorkContract


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


async def _read_owner_when_ready(
    adapter: AppServerAdapter,
    thread_id: str,
    *,
    attempts: int = 20,
    delay_seconds: float = 0.25,
):
    """Retry only Codex's transient empty-session race before queue dispatch."""

    for attempt in range(1, attempts + 1):
        try:
            return await adapter.read_thread(thread_id)
        except RemoteRequestError as exc:
            transient = (
                exc.method == "thread/read"
                and "failed to read session metadata " in exc.remote_message
                and " is empty" in exc.remote_message
            )
            if not transient:
                raise
            if attempt == attempts:
                raise SmokeFailure(
                    "synthetic owner session metadata stayed empty during bounded readiness"
                ) from exc
            await asyncio.sleep(delay_seconds)
    raise AssertionError("bounded readiness loop did not return or raise")


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


async def _run_queue_cli(
    root: Path,
    contract_path: Path,
    thread_id: str,
) -> dict[str, object]:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "hermes_codex_operator.cli",
        "--state",
        os.fspath(root / "state.sqlite3"),
        "queue",
        "--json",
        "--contract",
        os.fspath(contract_path),
        "--thread-id",
        thread_id,
        "--allow-root",
        os.fspath(root),
        "--shared-app-server",
        "--experimental-api-version",
        EXPERIMENTAL_QUEUE_API_VERSION,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=360.0
        )
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise SmokeFailure("shared-daemon queue CLI timed out") from exc
    output = stdout.decode("utf-8", errors="replace").strip()
    detail = stderr.decode("utf-8", errors="replace").strip()
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise SmokeFailure(
            "queue CLI did not return one JSON object: " + detail[-500:]
        ) from exc
    if not isinstance(payload, dict):
        raise SmokeFailure("queue CLI returned a non-object JSON value")
    if process.returncode not in {0, 3}:
        raise SmokeFailure(
            f"queue CLI exited {process.returncode}: " + detail[-500:]
        )
    return payload


def _validate_verified_payload(
    payload: dict[str, object],
    *,
    expect_result: bool = True,
) -> dict[str, object]:
    if (
        payload.get("task_id") != TASK_ID
        or payload.get("operation_id") != OPERATION_ID
        or payload.get("status") != "VERIFIED"
        or payload.get("dispatch_kind") != "queue"
    ):
        raise SmokeFailure("queue CLI result is not bound to the smoke contract")
    result = payload.get("result")
    receipt = payload.get("receipt")
    if expect_result:
        if not isinstance(result, str) or not result:
            raise SmokeFailure("queue CLI omitted the authoritative final result")
    elif result is not None:
        raise SmokeFailure("duplicate queue observation unexpectedly exposed result text")
    if not isinstance(receipt, dict):
        raise SmokeFailure("queue CLI omitted the immutable receipt")
    if (
        receipt.get("task_id") != TASK_ID
        or receipt.get("operation_id") != OPERATION_ID
        or receipt.get("status") != "VERIFIED"
    ):
        raise SmokeFailure("receipt is not bound to the verified smoke operation")
    expected_artifact = hashlib.sha256(EXPECTED_BYTES).hexdigest()
    if receipt.get("changed_paths") != [
        {"path": "queue-live.txt", "sha256": expected_artifact}
    ]:
        raise SmokeFailure("receipt does not bind the exact artifact path and digest")
    for key in (
        "prompt_sha256",
        "workspace_sha256",
        "thread_sha256",
        "turn_sha256",
        "result_sha256",
    ):
        digest = receipt.get(key)
        if not isinstance(digest, str) or len(digest) != 64:
            raise SmokeFailure(f"receipt omitted a valid {key}")
    if expect_result:
        assert isinstance(result, str)
        if receipt.get("result_sha256") != hashlib.sha256(
            result.encode("utf-8")
        ).hexdigest():
            raise SmokeFailure("receipt result digest does not match CLI readback")
    verification = receipt.get("verification")
    if (
        not isinstance(verification, list)
        or len(verification) != 1
        or not isinstance(verification[0], dict)
        or verification[0].get("exit_code") != 0
    ):
        raise SmokeFailure("receipt does not contain one passing verification")
    return receipt


async def _run(
    root: Path, *, owner_attempts: int, socket_path: Path
) -> dict[str, object]:
    contract = _contract(root)
    workspace = Path(contract.workspace)
    contract_path = root / "work-contract.json"
    contract_path.write_text(
        json.dumps(contract.to_dict(), sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    contract_path.chmod(0o600)
    adapter = AppServerAdapter(
        experimental_api_version=EXPERIMENTAL_QUEUE_API_VERSION,
        request_timeout=60.0,
        unix_socket_path=socket_path,
    )
    try:
        thread = None
        preflight_interruptions: list[dict[str, object]] = []
        for attempt in range(1, owner_attempts + 1):
            candidate = await adapter.start_thread(workspace)
            await adapter.start_turn(candidate.thread_id, OWNER_PROMPT)
            owner = await _read_owner_when_ready(adapter, candidate.thread_id)
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
        first = await _run_queue_cli(root, contract_path, thread.thread_id)
        if first.get("status") != "VERIFIED" or first.get("dispatched") is not True:
            raise SmokeFailure(
                "queue did not reach a first-dispatch VERIFIED result: "
                f"status={first.get('status')} failure={first.get('failure_code')}"
            )
        first_receipt = _validate_verified_payload(first)
        artifact = workspace / "queue-live.txt"
        if artifact.read_bytes() != EXPECTED_BYTES:
            raise SmokeFailure("queued artifact content does not match the contract")

        restarted = await _run_queue_cli(root, contract_path, thread.thread_id)
        if (
            restarted.get("status") != "VERIFIED"
            or restarted.get("dispatched") is not False
        ):
            raise SmokeFailure("second CLI process was not a non-dispatch observation")
        if (
            _validate_verified_payload(restarted, expect_result=False)
            != first_receipt
        ):
            raise SmokeFailure("second CLI process returned a different receipt")

        return {
            "ok": True,
            "status": "VERIFIED",
            "codex_version": EXPERIMENTAL_QUEUE_API_VERSION,
            "transport": "managed-daemon-unix-socket",
            "active_turn_created": True,
            "owner_attempts": len(preflight_interruptions) + 1,
            "preflight_interruptions": preflight_interruptions,
            "queued_turn_verified": True,
            "first_dispatched": True,
            "restart_dispatched": False,
            "changed_paths": ["queue-live.txt"],
            "artifact_sha256": hashlib.sha256(EXPECTED_BYTES).hexdigest(),
            "codex_history_note": "synthetic thread may remain in local Codex history",
        }
    finally:
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
    codex = shutil.which("codex")
    if codex is None:
        print("Codex CLI was not found on PATH.", file=sys.stderr)
        return 2
    try:
        endpoint = discover_managed_app_server(
            expected_version=EXPERIMENTAL_QUEUE_API_VERSION,
            command=(codex, "app-server", "daemon", "version"),
            timeout=10.0,
        )
    except AdapterError as exc:
        detail = str(exc) or type(exc).__name__
        print(
            f"Managed Codex App Server is unavailable; daemon start may be required: {detail}",
            file=sys.stderr,
        )
        return 2
    try:
        with _smoke_root(keep=args.keep) as root:
            output = asyncio.run(
                _run(
                    root,
                    owner_attempts=args.owner_attempts,
                    socket_path=endpoint.socket_path,
                )
            )
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
