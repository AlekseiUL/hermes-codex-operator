#!/usr/bin/env python3
"""Run an explicit disposable smoke against the caller's real Codex App Server.

The temporary workspace and local operator journal are deleted by default, but
Codex may retain the synthetic thread in the caller's normal local history.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Iterator, Sequence


EXPECTED_BYTES = b"HERMES_CODEX_OPERATOR_SMOKE_OK\n"
TASK_ID = "hermes-codex-operator-live-smoke"
OPERATION_ID = "hermes-codex-operator-live-smoke-001"


class SmokeFailure(RuntimeError):
    pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a synthetic task through the real local Codex App Server."
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
    return parser


@contextmanager
def _smoke_root(*, keep: bool) -> Iterator[Path]:
    if keep:
        root = Path(tempfile.mkdtemp(prefix="hermes-codex-live-smoke-"))
        yield root
        return
    with tempfile.TemporaryDirectory(prefix="hermes-codex-live-smoke-") as value:
        yield Path(value)


def _command(repo: Path, root: Path, action: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "hermes_codex_operator.cli",
        "--state",
        os.fspath(root / "state.sqlite3"),
        action,
        "--contract",
        os.fspath(root / "work-contract.json"),
        "--allow-root",
        os.fspath(root),
        "--json",
    ]


def _run(repo: Path, root: Path, action: str) -> dict[str, object]:
    env = os.environ.copy()
    source = os.fspath(repo / "src")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = source if not existing else source + os.pathsep + existing
    try:
        completed = subprocess.run(
            _command(repo, root, action),
            cwd=repo,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=420,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SmokeFailure(f"{action} exceeded the outer smoke timeout") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise SmokeFailure(f"{action} exited {completed.returncode}: {detail[:1000]}")
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise SmokeFailure(f"{action} did not return one JSON object") from exc
    if not isinstance(value, dict):
        raise SmokeFailure(f"{action} returned a non-object JSON value")
    return value


def _write_contract(root: Path) -> Path:
    workspace = root / "workspace"
    workspace.mkdir()
    expected_literal = EXPECTED_BYTES.decode("ascii").replace("\n", "\\n")
    verification_code = (
        "from pathlib import Path; "
        f"raise SystemExit(0 if Path('artifact.txt').read_bytes() == {EXPECTED_BYTES!r} else 1)"
    )
    contract = {
        "schema_version": "1",
        "task_id": TASK_ID,
        "operation_id": OPERATION_ID,
        "workspace": os.fspath(workspace),
        "prompt": (
            "Create exactly one file named artifact.txt in the workspace with the "
            f"exact UTF-8 content {expected_literal}. Do not create or modify any "
            "other file. Then report what you did."
        ),
        "allowed_change_globs": ["artifact.txt"],
        "verification": [[sys.executable, "-c", verification_code]],
        "timeout_seconds": 300,
    }
    path = root / "work-contract.json"
    path.write_text(
        json.dumps(contract, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _changed_paths(value: dict[str, object]) -> list[str]:
    receipt = value.get("receipt")
    if not isinstance(receipt, dict):
        raise SmokeFailure("VERIFIED response has no receipt object")
    raw = receipt.get("changed_paths")
    if not isinstance(raw, list):
        raise SmokeFailure("receipt changed_paths is not an array")
    result: list[str] = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise SmokeFailure("receipt contains invalid changed-path evidence")
        result.append(item["path"])
    return result


def _require_verified(value: dict[str, object], *, dispatched: bool) -> None:
    if value.get("status") != "VERIFIED" or value.get("ok") is not True:
        raise SmokeFailure(f"expected VERIFIED, received {value.get('status')!r}")
    if value.get("dispatched") is not dispatched:
        raise SmokeFailure(
            f"expected dispatched={dispatched}, received {value.get('dispatched')!r}"
        )


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

    repo = Path(__file__).resolve().parent.parent
    try:
        with _smoke_root(keep=args.keep) as root:
            _write_contract(root)
            first = _run(repo, root, "submit")
            _require_verified(first, dispatched=True)
            if _changed_paths(first) != ["artifact.txt"]:
                raise SmokeFailure("first receipt did not contain exactly artifact.txt")
            artifact = root / "workspace" / "artifact.txt"
            if artifact.read_bytes() != EXPECTED_BYTES:
                raise SmokeFailure("artifact content does not match the contract")

            duplicate = _run(repo, root, "submit")
            _require_verified(duplicate, dispatched=False)

            collected = _run(repo, root, "collect")
            _require_verified(collected, dispatched=False)
            receipt = collected.get("receipt")
            receipt_digest = hashlib.sha256(
                json.dumps(
                    receipt,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            output: dict[str, object] = {
                "ok": True,
                "status": "VERIFIED",
                "first_dispatched": True,
                "duplicate_dispatched": False,
                "collect_dispatched": False,
                "changed_paths": ["artifact.txt"],
                "artifact_sha256": hashlib.sha256(EXPECTED_BYTES).hexdigest(),
                "receipt_sha256": receipt_digest,
                "codex_history_note": "synthetic thread may remain in local Codex history",
            }
            if args.keep:
                output["kept_workspace"] = os.fspath(root)
            print(json.dumps(output, sort_keys=True, separators=(",", ":")))
            return 0
    except (OSError, SmokeFailure, json.JSONDecodeError) as exc:
        print(f"Live smoke failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
