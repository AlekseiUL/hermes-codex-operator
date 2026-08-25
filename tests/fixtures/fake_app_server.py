#!/usr/bin/env python3
"""Scripted synthetic Codex App Server used by protocol tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any


SCENARIO = sys.argv[1]
THREAD_ID = "thread-synthetic-001"
TURN_ID = "turn-synthetic-001"


def receive() -> dict[str, Any]:
    line = sys.stdin.buffer.readline()
    if not line:
        raise EOFError
    message = json.loads(line)
    assert isinstance(message, dict)
    assert "jsonrpc" not in message
    return message


def expect(method: str) -> dict[str, Any]:
    message = receive()
    assert message.get("method") == method, message
    return message


def send(message: dict[str, Any]) -> None:
    encoded = json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def response(request: dict[str, Any], result: Any) -> None:
    send({"id": request["id"], "result": result})


def completion(turn_id: str = TURN_ID, status: str = "completed") -> None:
    send(
        {
            "method": "turn/completed",
            "params": {
                "threadId": THREAD_ID,
                "turn": {"id": turn_id, "status": status, "items": []},
            },
        }
    )


def turn_record(
    turn_id: str = TURN_ID,
    *,
    status: str = "completed",
    include_final: bool = True,
) -> dict[str, Any]:
    items: list[dict[str, Any]] = [
        {
            "id": "message-progress",
            "type": "agentMessage",
            "phase": "commentary",
            "text": "Synthetic progress, not the result.",
        }
    ]
    if include_final:
        items.extend(
            [
                {
                    "id": "message-legacy",
                    "type": "agentMessage",
                    "phase": None,
                    "text": "Synthetic legacy fallback.",
                },
                {
                    "id": "message-final",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "Synthetic verified implementation report.",
                },
            ]
        )
    record: dict[str, Any] = {"id": turn_id, "status": status, "items": items}
    if status == "failed":
        record["error"] = {"message": "synthetic remote failure"}
    return record


def thread_record(workspace: str, turns: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": THREAD_ID,
        "cliVersion": "0.0.0-synthetic",
        "createdAt": 1,
        "updatedAt": 1,
        "cwd": workspace,
        "ephemeral": False,
        "modelProvider": "openai",
        "preview": "",
        "source": "appServer",
        "status": {"type": "idle"},
        "turns": turns,
    }


def thread_response(
    workspace: str,
    turns: list[dict[str, Any]],
    *,
    approval_policy: str = "never",
    sandbox_type: str = "workspaceWrite",
) -> dict[str, Any]:
    return {
        "approvalPolicy": approval_policy,
        "approvalsReviewer": "user",
        "cwd": workspace,
        "model": "synthetic-model",
        "modelProvider": "openai",
        "permissionProfile": None,
        "sandbox": {
            "type": sandbox_type,
            "networkAccess": False,
            "writableRoots": [],
        },
        "thread": thread_record(workspace, turns),
    }


def wait_until_killed() -> None:
    while True:
        time.sleep(1)


def main() -> None:
    initialize = expect("initialize")
    assert initialize["params"]["clientInfo"]["name"] == "hermes-codex-operator"
    assert initialize["params"]["clientInfo"]["title"] == "Hermes Codex Operator"
    assert initialize["params"]["clientInfo"]["version"] == "0.0.1"
    # Proves that the client starts its stdout reader before waiting on the
    # initialize response.
    send({"method": "account/updated", "params": {}})
    response(
        initialize,
        {
            "codexHome": os.path.abspath(".synthetic-codex-home"),
            "platformFamily": "unix",
            "platformOs": "synthetic",
            "userAgent": "synthetic-app-server",
        },
    )
    initialized = expect("initialized")
    assert set(initialized) == {"method"}

    expected_thread_method = (
        "thread/resume" if SCENARIO in {"resume", "resume_read_only"} else "thread/start"
    )
    thread_request = expect(expected_thread_method)
    thread_params = thread_request["params"]
    assert thread_params["cwd"].startswith("/")
    expected_sandbox = "read-only" if SCENARIO == "resume_read_only" else "workspace-write"
    expected_approval = "untrusted" if SCENARIO == "resume_read_only" else "never"
    assert thread_params["sandbox"] == expected_sandbox
    assert thread_params["approvalPolicy"] == expected_approval
    assert "model" not in thread_params
    if expected_thread_method == "thread/resume":
        assert thread_params["threadId"] == THREAD_ID
    workspace = thread_params["cwd"]
    sandbox_type = {
        "read-only": "readOnly",
        "workspace-write": "workspaceWrite",
    }[expected_sandbox]
    if SCENARIO == "policy_broadened":
        sandbox_type = "dangerFullAccess"
    response(
        thread_request,
        thread_response(
            workspace,
            [],
            approval_policy=expected_approval,
            sandbox_type=sandbox_type,
        ),
    )

    if SCENARIO == "orphan_child":
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        sys.stderr.write(f"child-pid:{child.pid}\n")
        sys.stderr.flush()
        os._exit(0)

    if SCENARIO in {"resume", "stderr"}:
        if SCENARIO == "stderr":
            sys.stderr.buffer.write(b"s" * 8192)
            sys.stderr.buffer.flush()
        wait_until_killed()

    turn_request = expect("turn/start")
    turn_params = turn_request["params"]
    assert turn_params["threadId"] == THREAD_ID
    assert turn_params["input"] == [{"type": "text", "text": "Build the synthetic widget."}]
    assert "model" not in turn_params
    assert "effort" not in turn_params

    if SCENARIO == "turn_timeout":
        wait_until_killed()

    if SCENARIO == "turn_delayed_response":
        wait_until_killed()

    if SCENARIO == "turn_start_missing_id":
        response(turn_request, {"turn": {"id": "", "status": "inProgress", "items": []}})
        wait_until_killed()

    if SCENARIO == "notification_before_response":
        completion()
        response(turn_request, {"turn": {"id": TURN_ID, "status": "inProgress", "items": []}})
    else:
        response(turn_request, {"turn": {"id": TURN_ID, "status": "inProgress", "items": []}})
        if SCENARIO == "unknown_server_request":
            send(
                {
                    "id": "approval-synthetic-001",
                    "method": "item/commandExecution/requestApproval",
                    "params": {"threadId": THREAD_ID, "turnId": TURN_ID},
                }
            )
            rejection = receive()
            assert rejection["id"] == "approval-synthetic-001"
            assert rejection["error"]["code"] == -32601
            wait_until_killed()
        if SCENARIO != "read_in_progress":
            completion("turn-unrelated-999", "failed")
            completion(status="failed" if SCENARIO == "remote_failed" else "completed")

    read_request = expect("thread/read")
    assert read_request["params"] == {"threadId": THREAD_ID, "includeTurns": True}
    if SCENARIO == "response_without_result":
        send({"id": read_request["id"]})
        wait_until_killed()

    if SCENARIO == "missing_exact_turn":
        turns = [turn_record("turn-unrelated-999")]
    elif SCENARIO == "missing_final_message":
        turns = [turn_record(include_final=False)]
    elif SCENARIO == "remote_failed":
        turns = [turn_record(status="failed")]
    elif SCENARIO == "read_in_progress":
        turns = [turn_record(status="inProgress", include_final=False)]
    else:
        turns = [turn_record("turn-unrelated-999"), turn_record()]
    response(read_request, {"thread": thread_record(workspace, turns)})
    wait_until_killed()


if __name__ == "__main__":
    try:
        main()
    except EOFError:
        pass
