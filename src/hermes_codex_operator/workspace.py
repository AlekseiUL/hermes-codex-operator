from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from functools import lru_cache
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence


class WorkspaceError(ValueError):
    """Base class for workspace policy failures."""


class WorkspaceOutsideRoots(WorkspaceError):
    pass


class UnsafeWorkspaceEntry(WorkspaceError):
    pass


class SnapshotLimitExceeded(WorkspaceError):
    pass


class ScopeViolation(WorkspaceError):
    def __init__(self, paths: Sequence[str]) -> None:
        self.paths = tuple(paths)
        super().__init__("changed paths are outside the declared scope")


@dataclass(frozen=True, slots=True)
class WorkspacePolicy:
    allowed_roots: tuple[Path, ...]
    reject_symlinks: bool = True
    max_files: int = 20_000
    max_bytes: int = 256 * 1024 * 1024
    excluded_names: frozenset[str] = frozenset(
        {
            ".git",
            ".hermes-codex-operator",
            ".steward",
            "__pycache__",
            ".pytest_cache",
            ".mypy_cache",
        }
    )

    def __post_init__(self) -> None:
        if not self.allowed_roots:
            raise WorkspaceError("at least one allowed root is required")
        if self.max_files < 1 or self.max_bytes < 1:
            raise WorkspaceError("snapshot limits must be positive")


@dataclass(frozen=True, slots=True)
class FileEvidence:
    sha256: str
    size: int
    mode: int


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    root_sha256: str
    files: Mapping[str, FileEvidence]
    total_bytes: int


def snapshot_to_dict(snapshot: WorkspaceSnapshot) -> dict[str, Any]:
    return {
        "root_sha256": snapshot.root_sha256,
        "total_bytes": snapshot.total_bytes,
        "files": [
            {
                "path": path,
                "sha256": item.sha256,
                "size": item.size,
                "mode": item.mode,
            }
            for path, item in sorted(snapshot.files.items())
        ],
    }


def snapshot_from_dict(value: Mapping[str, Any]) -> WorkspaceSnapshot:
    if set(value) != {"root_sha256", "total_bytes", "files"}:
        raise WorkspaceError("snapshot has unknown or missing fields")
    root_sha256 = value["root_sha256"]
    total_bytes = value["total_bytes"]
    raw_files = value["files"]
    if (
        not isinstance(root_sha256, str)
        or len(root_sha256) != 64
        or any(character not in "0123456789abcdef" for character in root_sha256)
    ):
        raise WorkspaceError("snapshot root digest is invalid")
    if isinstance(total_bytes, bool) or not isinstance(total_bytes, int) or total_bytes < 0:
        raise WorkspaceError("snapshot total_bytes is invalid")
    if not isinstance(raw_files, list):
        raise WorkspaceError("snapshot files must be an array")
    files: dict[str, FileEvidence] = {}
    measured_bytes = 0
    for raw in raw_files:
        if not isinstance(raw, Mapping) or set(raw) != {"path", "sha256", "size", "mode"}:
            raise WorkspaceError("snapshot file evidence is invalid")
        path = raw["path"]
        digest = raw["sha256"]
        size = raw["size"]
        mode = raw["mode"]
        if (
            not isinstance(path, str)
            or not path
            or path.startswith("/")
            or "\\" in path
            or ".." in PurePosixPath(path).parts
            or path in files
        ):
            raise WorkspaceError("snapshot path is invalid")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise WorkspaceError("snapshot file digest is invalid")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise WorkspaceError("snapshot file size is invalid")
        if isinstance(mode, bool) or not isinstance(mode, int) or not 0 <= mode <= 0o7777:
            raise WorkspaceError("snapshot file mode is invalid")
        files[path] = FileEvidence(sha256=digest, size=size, mode=mode)
        measured_bytes += size
    if measured_bytes != total_bytes:
        raise WorkspaceError("snapshot byte total does not match file evidence")
    return WorkspaceSnapshot(
        root_sha256=root_sha256,
        files=MappingProxyType(files),
        total_bytes=total_bytes,
    )


def resolve_workspace(workspace: Path | str, policy: WorkspacePolicy) -> Path:
    raw = Path(workspace).expanduser()
    if not raw.is_absolute():
        raise WorkspaceError("workspace must be an absolute path")
    if policy.reject_symlinks and raw.is_symlink():
        raise UnsafeWorkspaceEntry("workspace root must not be a symlink")
    try:
        resolved = raw.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceError("workspace does not exist") from exc
    if not resolved.is_dir():
        raise WorkspaceError("workspace must be a directory")

    roots: list[Path] = []
    for root in policy.allowed_roots:
        try:
            roots.append(Path(root).expanduser().resolve(strict=True))
        except OSError as exc:
            raise WorkspaceError("an allowed root does not exist") from exc
    if not any(resolved == root or root in resolved.parents for root in roots):
        raise WorkspaceOutsideRoots("workspace is outside the allowed roots")
    return resolved


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_workspace(workspace: Path | str, policy: WorkspacePolicy) -> WorkspaceSnapshot:
    root = resolve_workspace(workspace, policy)
    evidence: dict[str, FileEvidence] = {}
    total_bytes = 0

    for current, dirnames, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        kept_dirs: list[str] = []
        for name in sorted(dirnames):
            candidate = current_path / name
            if name in policy.excluded_names:
                continue
            if candidate.is_symlink():
                if policy.reject_symlinks:
                    relative = candidate.relative_to(root).as_posix()
                    raise UnsafeWorkspaceEntry(f"symlink is not allowed: {relative}")
                continue
            kept_dirs.append(name)
        dirnames[:] = kept_dirs

        for name in sorted(filenames):
            if name in policy.excluded_names:
                continue
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                if policy.reject_symlinks:
                    raise UnsafeWorkspaceEntry(f"symlink is not allowed: {relative}")
                continue
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise UnsafeWorkspaceEntry(f"special file is not allowed: {relative}")
            if len(evidence) + 1 > policy.max_files:
                raise SnapshotLimitExceeded("workspace file-count limit exceeded")
            total_bytes += metadata.st_size
            if total_bytes > policy.max_bytes:
                raise SnapshotLimitExceeded("workspace byte limit exceeded")
            evidence[relative] = FileEvidence(
                sha256=_file_sha256(path),
                size=metadata.st_size,
                mode=stat.S_IMODE(metadata.st_mode),
            )

    root_digest = hashlib.sha256()
    for relative, item in sorted(evidence.items()):
        root_digest.update(relative.encode("utf-8"))
        root_digest.update(b"\0")
        root_digest.update(item.sha256.encode("ascii"))
        root_digest.update(b"\0")
        root_digest.update(str(item.size).encode("ascii"))
        root_digest.update(b"\0")
        root_digest.update(str(item.mode).encode("ascii"))
        root_digest.update(b"\0")
    return WorkspaceSnapshot(
        root_sha256=root_digest.hexdigest(),
        files=MappingProxyType(evidence),
        total_bytes=total_bytes,
    )


def changed_paths(before: WorkspaceSnapshot, after: WorkspaceSnapshot) -> tuple[str, ...]:
    paths = set(before.files) | set(after.files)
    return tuple(sorted(path for path in paths if before.files.get(path) != after.files.get(path)))


def _valid_scope_pattern(pattern: str) -> str:
    normalized = pattern.replace("\\", "/")
    path = PurePosixPath(normalized)
    if not normalized or normalized.startswith("/") or ".." in path.parts:
        raise WorkspaceError("scope patterns must be non-empty relative paths")
    if normalized.endswith("/"):
        normalized += "**"
    return normalized


def _matches_scope(relative: str, pattern: str) -> bool:
    """Match path segments, with ``**`` as the only recursive wildcard."""

    path_parts = PurePosixPath(relative).parts
    pattern_parts = PurePosixPath(pattern).parts

    @lru_cache(maxsize=None)
    def match(path_index: int, pattern_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return path_index == len(path_parts)
        segment = pattern_parts[pattern_index]
        if segment == "**":
            return match(path_index, pattern_index + 1) or (
                path_index < len(path_parts) and match(path_index + 1, pattern_index)
            )
        return (
            path_index < len(path_parts)
            and fnmatchcase(path_parts[path_index], segment)
            and match(path_index + 1, pattern_index + 1)
        )

    return match(0, 0)


def outside_scope(paths: Iterable[str], allowed_globs: Sequence[str]) -> tuple[str, ...]:
    if not allowed_globs:
        return ()
    patterns = tuple(_valid_scope_pattern(pattern) for pattern in allowed_globs)
    rejected = []
    for relative in paths:
        normalized = PurePosixPath(relative).as_posix()
        if not any(_matches_scope(normalized, pattern) for pattern in patterns):
            rejected.append(normalized)
    return tuple(sorted(rejected))


def enforce_scope(paths: Iterable[str], allowed_globs: Sequence[str]) -> None:
    rejected = outside_scope(paths, allowed_globs)
    if rejected:
        raise ScopeViolation(rejected)
