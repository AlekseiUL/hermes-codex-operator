from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time
from typing import Sequence


@dataclass(frozen=True, slots=True)
class VerificationResult:
    argv_sha256: str
    exit_code: int | None
    duration_ms: int
    stdout_sha256: str
    stderr_sha256: str
    stdout_bytes: int
    stderr_bytes: int
    passed: bool
    failure_code: str | None = None


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest_argv(argv: Sequence[str]) -> str:
    encoded = json.dumps(list(argv), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if os.name == "posix":
        # Verification commands run in their own session.  Signal the whole
        # process group even when the leader has already exited: a successful
        # command must not leave a late-writing descendant behind after the
        # workspace has been verified.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        if process.poll() is None:
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass

        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                return
            except PermissionError:
                break
            time.sleep(0.02)

        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        if process.poll() is None:
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass
        return

    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=1.0)
    except (OSError, subprocess.TimeoutExpired):
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass


def run_verification(
    argv: Sequence[str],
    *,
    workspace: Path,
    timeout_seconds: float = 300.0,
    max_output_bytes: int = 1024 * 1024,
    cancel_event: threading.Event | None = None,
) -> VerificationResult:
    if not argv or any(not isinstance(part, str) or not part for part in argv):
        raise ValueError("verification argv must contain non-empty strings")
    if timeout_seconds <= 0 or max_output_bytes < 1:
        raise ValueError("verification limits must be positive")

    started = time.monotonic()
    failure_code: str | None = None
    exit_code: int | None = None
    stdout = b""
    stderr = b""

    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        try:
            process = subprocess.Popen(
                list(argv),
                cwd=workspace,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                shell=False,
                start_new_session=(os.name == "posix"),
            )
        except (OSError, subprocess.SubprocessError):
            process = None
            failure_code = "command_start_failed"

        if process is not None:
            try:
                deadline = started + timeout_seconds
                while process.poll() is None:
                    if cancel_event is not None and cancel_event.is_set():
                        failure_code = "cancelled"
                        _stop_process(process)
                        break
                    if time.monotonic() >= deadline:
                        failure_code = "timed_out"
                        _stop_process(process)
                        break
                    output_size = os.fstat(stdout_file.fileno()).st_size + os.fstat(stderr_file.fileno()).st_size
                    if output_size > max_output_bytes:
                        failure_code = "output_limit_exceeded"
                        _stop_process(process)
                        break
                    time.sleep(0.02)
                exit_code = process.poll()
            finally:
                if os.name == "posix":
                    _stop_process(process)

        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout = stdout_file.read(max_output_bytes + 1)
        stderr = stderr_file.read(max_output_bytes + 1)

    if failure_code is None and len(stdout) + len(stderr) > max_output_bytes:
        failure_code = "output_limit_exceeded"
    if failure_code is None and exit_code != 0:
        failure_code = "nonzero_exit"
    duration_ms = max(0, round((time.monotonic() - started) * 1000))
    return VerificationResult(
        argv_sha256=_digest_argv(argv),
        exit_code=exit_code,
        duration_ms=duration_ms,
        stdout_sha256=_digest_bytes(stdout),
        stderr_sha256=_digest_bytes(stderr),
        stdout_bytes=len(stdout),
        stderr_bytes=len(stderr),
        passed=failure_code is None,
        failure_code=failure_code,
    )


def run_verifications(
    commands: Sequence[Sequence[str]],
    *,
    workspace: Path,
    timeout_seconds: float = 300.0,
    max_output_bytes: int = 1024 * 1024,
    cancel_event: threading.Event | None = None,
) -> tuple[VerificationResult, ...]:
    results = []
    for argv in commands:
        result = run_verification(
            argv,
            workspace=workspace,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            cancel_event=cancel_event,
        )
        results.append(result)
        if not result.passed:
            break
    return tuple(results)
