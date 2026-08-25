#!/usr/bin/env python3
"""Create a privacy-safe, canonical ``.tar.gz`` source distribution.

Some sdist builders preserve local account names, timestamps, modes, and gzip
filenames. This utility freezes one generated artifact, validates a strict
single-root sdist, and atomically rewrites it with neutral metadata.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import gzip
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tarfile
import tempfile
from typing import BinaryIO, Iterator, Sequence
import unicodedata


_MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
_MAX_FILES = 50_000
_MAX_FILE_BYTES = 16 * 1024 * 1024
_MAX_TOTAL_BYTES = 64 * 1024 * 1024
_MAX_PATH_BYTES = 4096
_MAX_COMPONENT_BYTES = 255
_COPY_CHUNK = 1024 * 1024
_NORMALIZED_MTIME = 315_532_800  # 1980-01-01 UTC; valid in ZIP/wheel too.
_WINDOWS_DEVICE = re.compile(
    r"(?i)^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$"
)
_WINDOWS_FORBIDDEN = frozenset('<>:"|?*')


class NormalizationError(ValueError):
    """A fail-closed normalization error with a safe public code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class NormalizedSdist:
    members: int
    input_sha256: str
    output_sha256: str
    root: str
    epoch: int = _NORMALIZED_MTIME

    @property
    def sha256(self) -> str:
        """Compatibility alias for the canonical output digest."""
        return self.output_sha256


@dataclass(frozen=True, slots=True)
class _MemberRecord:
    name: str
    is_directory: bool
    offset: int = 0
    size: int = 0


def _safe_member_name(raw: str) -> str:
    if (
        not raw
        or "\0" in raw
        or "\\" in raw
        or re.match(r"(?i)^[a-z]:", raw)
        or any(ord(character) < 32 or ord(character) == 127 for character in raw)
        or unicodedata.normalize("NFC", raw) != raw
    ):
        raise NormalizationError("UNSAFE_ARCHIVE_PATH")
    candidate = PurePosixPath(raw)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise NormalizationError("UNSAFE_ARCHIVE_PATH")
    parts = tuple(part for part in candidate.parts if part not in {"", "."})
    if not parts:
        raise NormalizationError("UNSAFE_ARCHIVE_PATH")
    if any(
        part.rstrip(" .") != part
        or _WINDOWS_DEVICE.match(part)
        or any(character in _WINDOWS_FORBIDDEN for character in part)
        for part in parts
    ):
        raise NormalizationError("UNSAFE_ARCHIVE_PATH")
    try:
        encoded_parts = tuple(part.encode("utf-8") for part in parts)
        encoded_path = "/".join(parts).encode("utf-8")
    except UnicodeEncodeError as error:
        raise NormalizationError("UNSAFE_ARCHIVE_PATH") from error
    if len(encoded_path) > _MAX_PATH_BYTES or any(
        len(part) > _MAX_COMPONENT_BYTES for part in encoded_parts
    ):
        raise NormalizationError("ARCHIVE_PATH_LIMIT")
    normalized = PurePosixPath(*parts).as_posix()
    if raw not in {normalized, normalized + "/"}:
        raise NormalizationError("NONCANONICAL_ARCHIVE_PATH")
    return normalized


def _collision_key(name: str) -> str:
    return unicodedata.normalize("NFKC", name).casefold()


@contextmanager
def _source_snapshot(path: Path) -> Iterator[tuple[BinaryIO, str]]:
    try:
        before = path.lstat()
    except OSError as error:
        raise NormalizationError("SOURCE_UNREADABLE") from error
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise NormalizationError("SOURCE_NOT_REGULAR")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise NormalizationError("SOURCE_UNREADABLE") from error
    with os.fdopen(descriptor, "rb") as source, tempfile.TemporaryFile(
        mode="w+b"
    ) as frozen:
        opened = os.fstat(source.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise NormalizationError("SOURCE_NOT_REGULAR")
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise NormalizationError("SOURCE_CHANGED")
        if opened.st_size > _MAX_ARCHIVE_BYTES:
            raise NormalizationError("SOURCE_SIZE_LIMIT")

        digest = hashlib.sha256()
        copied = 0
        while True:
            chunk = source.read(_COPY_CHUNK)
            if not chunk:
                break
            copied += len(chunk)
            if copied > _MAX_ARCHIVE_BYTES:
                raise NormalizationError("SOURCE_SIZE_LIMIT")
            digest.update(chunk)
            frozen.write(chunk)

        after = os.fstat(source.fileno())
        stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if copied != opened.st_size or any(
            getattr(opened, field) != getattr(after, field) for field in stable_fields
        ):
            raise NormalizationError("SOURCE_CHANGED")
        frozen.flush()
        frozen.seek(0)
        yield frozen, digest.hexdigest()


def _copy_member(
    source: BinaryIO,
    payloads: BinaryIO,
    expected_size: int,
) -> tuple[int, int]:
    offset = payloads.tell()
    remaining = expected_size
    while remaining:
        chunk = source.read(min(_COPY_CHUNK, remaining))
        if not chunk:
            raise NormalizationError("MEMBER_UNREADABLE")
        payloads.write(chunk)
        remaining -= len(chunk)
    if source.read(1):
        raise NormalizationError("MEMBER_SIZE_MISMATCH")
    return offset, expected_size


def _read_members(
    source: Path,
    expected_root: str,
    payloads: BinaryIO,
) -> tuple[list[_MemberRecord], str]:
    files: list[_MemberRecord] = []
    explicit_directories: set[str] = set()
    registered: dict[str, tuple[str, str]] = {}
    total_bytes = 0

    def register(name: str, kind: str) -> None:
        key = _collision_key(name)
        prior = registered.get(key)
        if prior is not None:
            if prior[0] == name:
                raise NormalizationError("DUPLICATE_ARCHIVE_PATH")
            raise NormalizationError("COLLIDING_ARCHIVE_PATH")
        registered[key] = (name, kind)

    try:
        with _source_snapshot(source) as (frozen, input_sha256):
            with tarfile.open(fileobj=frozen, mode="r:gz") as bundle:
                for count, member in enumerate(bundle, start=1):
                    if count > _MAX_FILES:
                        raise NormalizationError("FILE_COUNT_LIMIT")
                    name = _safe_member_name(member.name)
                    if member.name.endswith("/") and not member.isdir():
                        raise NormalizationError("NONCANONICAL_ARCHIVE_PATH")
                    if name.split("/", 1)[0] != expected_root:
                        raise NormalizationError("ARCHIVE_ROOT_MISMATCH")
                    register(name, "directory" if member.isdir() else "file")

                    if member.isdir():
                        explicit_directories.add(name)
                        continue
                    if not member.isreg() or getattr(member, "sparse", None) is not None:
                        raise NormalizationError("SPECIAL_MEMBER_FORBIDDEN")
                    if member.size > _MAX_FILE_BYTES:
                        raise NormalizationError("FILE_SIZE_LIMIT")
                    total_bytes += member.size
                    if total_bytes > _MAX_TOTAL_BYTES:
                        raise NormalizationError("TOTAL_SIZE_LIMIT")
                    extracted = bundle.extractfile(member)
                    if extracted is None:
                        raise NormalizationError("MEMBER_UNREADABLE")
                    offset, size = _copy_member(extracted, payloads, member.size)
                    files.append(_MemberRecord(name, False, offset, size))
    except NormalizationError:
        raise
    except (OSError, EOFError, tarfile.TarError) as error:
        raise NormalizationError("INVALID_TAR_GZIP") from error

    if not registered:
        raise NormalizationError("EMPTY_ARCHIVE")
    root_entry = registered.get(_collision_key(expected_root))
    if root_entry != (expected_root, "directory"):
        raise NormalizationError("MISSING_ARCHIVE_ROOT")
    required_files = {
        f"{expected_root}/PKG-INFO",
        f"{expected_root}/pyproject.toml",
    }
    file_names = {record.name for record in files}
    if not required_files <= file_names:
        raise NormalizationError("REQUIRED_SDIST_FILE_MISSING")

    all_names = tuple(explicit_directories) + tuple(file_names)
    directories = set(explicit_directories)
    for name in all_names:
        parts = PurePosixPath(name).parts
        for depth in range(1, len(parts)):
            parent = PurePosixPath(*parts[:depth]).as_posix()
            key = _collision_key(parent)
            prior = registered.get(key)
            if prior is not None and prior[1] == "file":
                raise NormalizationError("FILE_PARENT_CONFLICT")
            if prior is not None and prior[0] != parent:
                raise NormalizationError("COLLIDING_ARCHIVE_PATH")
            if prior is None:
                registered[key] = (parent, "directory")
            directories.add(parent)

    records = [_MemberRecord(name, True) for name in directories] + files
    records.sort(key=lambda record: record.name.encode("utf-8"))
    if len(records) > _MAX_FILES:
        raise NormalizationError("FILE_COUNT_LIMIT")
    payloads.flush()
    return records, input_sha256


def _write_normalized(
    records: Sequence[_MemberRecord],
    payloads: BinaryIO,
    destination: Path,
) -> str:
    try:
        parent_metadata = destination.parent.lstat()
    except OSError as error:
        raise NormalizationError("DESTINATION_PARENT_UNUSABLE") from error
    if not stat.S_ISDIR(parent_metadata.st_mode) or stat.S_ISLNK(parent_metadata.st_mode):
        raise NormalizationError("DESTINATION_PARENT_UNUSABLE")
    try:
        existing = destination.lstat()
    except FileNotFoundError:
        existing = None
    except OSError as error:
        raise NormalizationError("DESTINATION_UNUSABLE") from error
    if existing is not None and (
        stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode)
    ):
        raise NormalizationError("DESTINATION_NOT_REGULAR")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".normalize-sdist-",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    digest = ""
    try:
        with os.fdopen(descriptor, "w+b") as raw_output:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                compresslevel=9,
                fileobj=raw_output,
                mtime=_NORMALIZED_MTIME,
            ) as compressed:
                with tarfile.open(
                    fileobj=compressed,
                    mode="w",
                    format=tarfile.PAX_FORMAT,
                ) as output:
                    for record in records:
                        member = tarfile.TarInfo(record.name)
                        member.type = (
                            tarfile.DIRTYPE if record.is_directory else tarfile.REGTYPE
                        )
                        member.size = 0 if record.is_directory else record.size
                        member.mode = 0o755 if record.is_directory else 0o644
                        member.uid = 0
                        member.gid = 0
                        member.uname = "root"
                        member.gname = "root"
                        member.mtime = _NORMALIZED_MTIME
                        member.pax_headers = {}
                        stream = None
                        if not record.is_directory:
                            payloads.seek(record.offset)
                            stream = payloads
                        output.addfile(member, stream)
            raw_output.flush()
            os.fsync(raw_output.fileno())
            raw_output.seek(0)
            digest = hashlib.file_digest(raw_output, "sha256").hexdigest()
            if hasattr(os, "fchmod"):
                os.fchmod(raw_output.fileno(), 0o644)
        if not hasattr(os, "fchmod"):
            os.chmod(temporary, 0o644)
        os.replace(temporary, destination)
        if hasattr(os, "O_DIRECTORY"):
            directory_fd = os.open(
                destination.parent,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0),
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    return digest


def normalize_sdist(
    source: Path | str,
    *,
    expected_root: str,
    destination: Path | str | None = None,
) -> NormalizedSdist:
    source_path = Path(source).expanduser().absolute()
    destination_path = (
        source_path if destination is None else Path(destination).expanduser().absolute()
    )
    normalized_root = _safe_member_name(expected_root)
    if normalized_root != expected_root or "/" in normalized_root:
        raise NormalizationError("INVALID_EXPECTED_ROOT")
    expected_filename = f"{expected_root}.tar.gz"
    if source_path.name != expected_filename or destination_path.name != expected_filename:
        raise NormalizationError("SDIST_FILENAME_MISMATCH")

    with tempfile.TemporaryFile(mode="w+b") as payloads:
        records, input_sha256 = _read_members(
            source_path,
            expected_root,
            payloads,
        )
        output_sha256 = _write_normalized(records, payloads, destination_path)
    return NormalizedSdist(
        members=len(records),
        input_sha256=input_sha256,
        output_sha256=output_sha256,
        root=expected_root,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create one canonical, privacy-safe .tar.gz sdist."
    )
    parser.add_argument("archive", help="generated raw .tar.gz source distribution")
    parser.add_argument(
        "--expected-root",
        required=True,
        help="exact PEP 625 archive root and filename stem",
    )
    parser.add_argument(
        "--output",
        help="optional destination; by default the input is replaced atomically",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    options = _parser().parse_args(argv)
    try:
        result = normalize_sdist(
            options.archive,
            expected_root=options.expected_root,
            destination=options.output,
        )
    except NormalizationError as error:
        print(f"normalize-sdist: FAIL code={error.code}", file=sys.stderr)
        return 1
    print(
        "normalize-sdist: PASS "
        f"members={result.members} root={result.root} epoch={result.epoch} "
        f"input_sha256={result.input_sha256} output_sha256={result.output_sha256}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
