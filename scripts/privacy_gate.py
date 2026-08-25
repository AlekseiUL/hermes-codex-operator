#!/usr/bin/env python3
"""Fail-closed privacy gate for source trees, Git objects, and tar releases.

The scanner intentionally reports no excerpts.  A finding contains only a
rule identifier, a repository-relative location, a line number, and a
one-way fingerprint of the matched value.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tarfile
import unicodedata
import zipfile
from typing import Iterable, Sequence


_IGNORED_TREE_DIRS = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "venv",
    }
)
_FORBIDDEN_DIRS = frozenset(
    {
        ".aws",
        ".codex",
        ".hermes",
        ".openclaw",
        ".ssh",
        ".hermes-codex-operator",
        ".steward",
        "artifacts",
        "logs",
        "receipts",
        "runtime",
        "runtimes",
        "sessions",
        "worktrees",
    }
)
_FORBIDDEN_EXACT_FILES = frozenset({".env", ".npmrc", ".pypirc", "credentials"})
_FORBIDDEN_SUFFIXES = (
    ".db",
    ".jsonl",
    ".key",
    ".log",
    ".mobileprovision",
    ".p12",
    ".pem",
    ".sqlite",
    ".sqlite3",
)
_FORBIDDEN_RUNTIME_NAME_ENDINGS = (
    ".db.locks",
    ".sqlite.locks",
    ".sqlite3.locks",
    ".db-shm",
    ".db-wal",
    ".sqlite-shm",
    ".sqlite-wal",
    ".sqlite3-shm",
    ".sqlite3-wal",
)
_MAX_FILES = 50_000
_MAX_FILE_BYTES = 16 * 1024 * 1024
_MAX_TOTAL_BYTES = 512 * 1024 * 1024
_MAX_DENYLIST_BYTES = 64 * 1024
_ALLOWED_PUBLIC_BINARY_SHA256 = {
    "docs/assets/hermes-codex-operator-hero.png": (
        "3d6b500a20b3135bdd5a568f7f8924b4856dac3d6d17450772429486f5cdb7a1"
    ),
}


@dataclass(frozen=True, slots=True)
class Finding:
    rule_id: str
    path: str
    line: int
    fingerprint: str


@dataclass(frozen=True, slots=True)
class ScanReport:
    mode: str
    findings: tuple[Finding, ...]
    scanned_files: int

    @property
    def ok(self) -> bool:
        return not self.findings


class GateConfigurationError(ValueError):
    """Configuration error whose code is safe to display."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(slots=True)
class _Budget:
    files: int = 0
    total_bytes: int = 0

    def charge(self, size: int) -> str | None:
        if size < 0 or size > _MAX_FILE_BYTES:
            return "FILE_SIZE_LIMIT"
        if self.files + 1 > _MAX_FILES:
            return "FILE_COUNT_LIMIT"
        if self.total_bytes + size > _MAX_TOTAL_BYTES:
            return "TOTAL_SIZE_LIMIT"
        self.files += 1
        self.total_bytes += size
        return None


def _normal(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _fingerprint(rule_id: str, value: str | bytes) -> str:
    encoded = value if isinstance(value, bytes) else value.encode("utf-8", "surrogatepass")
    digest = hashlib.sha256(rule_id.encode("ascii") + b"\0" + encoded).hexdigest()
    return f"sha256:{digest[:20]}"


def _finding(rule_id: str, path: str, line: int, evidence: str | bytes) -> Finding:
    return Finding(
        rule_id=rule_id,
        path=path or ".",
        line=max(1, line),
        fingerprint=_fingerprint(rule_id, evidence),
    )


def _deduplicate(findings: Iterable[Finding]) -> tuple[Finding, ...]:
    return tuple(
        sorted(
            set(findings),
            key=lambda item: (item.path, item.line, item.rule_id, item.fingerprint),
        )
    )


_HOME_PATTERNS = (
    re.compile(r"(?<![\w])/(?:Users|home)/[^/\s\"'<>]+(?:/[^\s\"'<>]*)?"),
    re.compile(r"(?i)(?<![\w])[a-z]:[\\/]Users[\\/][^\\/\s\"'<>]+(?:[\\/][^\s\"'<>]*)?"),
)
_EMAIL_RE = re.compile(r"(?i)(?<![\w.+-])([a-z0-9.!#$%&'*+/=?^_`{|}~-]+)@([a-z0-9-]+(?:\.[a-z0-9-]+)+)")
_PHONE_PATTERNS = (
    re.compile(r"(?<![\w])\+(?:\d[\s().-]?){9,14}\d(?![\w])"),
    re.compile(
        r"(?i)\b(?:phone|mobile|tel(?:ephone)?|телефон)\b\s*[:=]\s*"
        r"(?:\+?\d[\s().-]?){7,15}\d(?![\w])"
    ),
)
_TELEGRAM_PATTERNS = (
    re.compile(r"(?<![\w])\d{6,12}:[A-Za-z0-9_-]{30,}(?![\w])"),
    re.compile(r"(?i)https?://(?:t|telegram)\.me/[a-z][a-z0-9_]{4,31}\b"),
    re.compile(r"(?i)https?://(?:t|telegram)\.me/\+[A-Za-z0-9_-]{8,}\b"),
    re.compile(
        r"(?i)\b(?:telegram|tg|chat)[_-]?(?:user[_-]?)?id\b\s*[:=]\s*[\"']?-?\d{5,15}\b"
    ),
)
_ALLOWED_PUBLIC_TELEGRAM_URLS = frozenset(
    {
        "https://t.me/Sprut_AI",
        "https://t.me/+eH-qNIDmud8zNDZi",
        "https://t.me/tribute/app?startapp=sJyg",
    }
)
_KEY_PATTERNS = (
    re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?(?:PRIVATE KEY|CERTIFICATE)-----"),
    re.compile(r"\bssh-(?:rsa|ed25519)\s+[A-Za-z0-9+/]{32,}={0,3}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"\b(?:github_pat_|gh[pousr]_|xox[baprs]-)[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|client[_-]?secret|access[_-]?token|auth[_-]?token|"
        r"bot[_-]?token|private[_-]?key|secret|token|password|passwd)\b\s*[:=]\s*"
        r"[\"']?([A-Za-z0-9_./+=-]{12,})"
    ),
    re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s:/]+:[^\s/@]{8,}@"),
)


def _allowed_email(local: str, domain: str) -> bool:
    domain = domain.casefold().rstrip(".")
    if domain == "example.invalid" or domain.endswith(".example.invalid"):
        return True
    if domain == "users.noreply.github.com":
        return True
    return local.casefold() == "noreply" and domain in {
        "example.com",
        "example.net",
        "example.org",
    }


def _scan_text(
    text: str,
    path: str,
    deny_terms: Sequence[str],
    findings: list[Finding],
) -> None:
    lines = text.splitlines() or [""]
    for line_number, line in enumerate(lines, start=1):
        for pattern in _HOME_PATTERNS:
            for match in pattern.finditer(line):
                findings.append(_finding("HOME_PATH", path, line_number, match.group(0)))

        for match in _EMAIL_RE.finditer(line):
            if not _allowed_email(match.group(1), match.group(2)):
                findings.append(_finding("EMAIL", path, line_number, match.group(0)))

        for pattern in _PHONE_PATTERNS:
            for match in pattern.finditer(line):
                findings.append(_finding("PHONE", path, line_number, match.group(0)))

        for pattern in _TELEGRAM_PATTERNS:
            for match in pattern.finditer(line):
                matched = match.group(0)
                if any(
                    allowed in line and allowed.casefold().startswith(matched.casefold())
                    for allowed in _ALLOWED_PUBLIC_TELEGRAM_URLS
                ):
                    continue
                findings.append(_finding("TELEGRAM_IDENTIFIER", path, line_number, match.group(0)))

        for pattern in _KEY_PATTERNS:
            for match in pattern.finditer(line):
                findings.append(_finding("KEY_MATERIAL", path, line_number, match.group(0)))

        normalized_line = _normal(line)
        for term in deny_terms:
            if term in normalized_line:
                findings.append(_finding("PRIVATE_DENYLIST", path, line_number, term))


def _decode_and_scan(
    data: bytes,
    path: str,
    deny_terms: Sequence[str],
    findings: list[Finding],
    *,
    mode: str,
) -> None:
    if _is_pinned_public_binary(path, data, mode=mode):
        return
    if b"\0" in data:
        findings.append(_finding("BINARY_FILE", path, 1, data[:256]))
        return
    try:
        text = data.decode("utf-8-sig", "strict")
    except UnicodeDecodeError:
        findings.append(_finding("BINARY_FILE", path, 1, data[:256]))
        return
    _scan_text(text, path, deny_terms, findings)


def _is_pinned_public_binary(path: str, data: bytes, *, mode: str) -> bool:
    """Accept only reviewed public assets whose exact bytes are pinned."""

    digest = hashlib.sha256(data).hexdigest()
    for approved_path, approved_digest in _ALLOWED_PUBLIC_BINARY_SHA256.items():
        if digest != approved_digest:
            continue
        if mode in {"tree", "staged"}:
            return path == approved_path
        if mode == "history":
            return path == f"history/blobs/{approved_path}"
        if mode == "release":
            actual_parts = PurePosixPath(path).parts
            approved_parts = PurePosixPath(approved_path).parts
            return actual_parts == approved_parts or (
                len(actual_parts) == len(approved_parts) + 1
                and actual_parts[1:] == approved_parts
            )
    return False


def _safe_relative_path(value: str) -> str | None:
    if not value or "\0" in value or "\\" in value:
        return None
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    parts = tuple(part for part in candidate.parts if part not in {"", "."})
    if not parts:
        return None
    return PurePosixPath(*parts).as_posix()


def _safe_display_path(path: str, deny_terms: Sequence[str]) -> str:
    """Return a printable path, replacing sensitive names with a digest."""

    probes: list[Finding] = []
    _scan_text(path, "@path-probe", deny_terms, probes)
    if not probes:
        return path
    digest = hashlib.sha256(path.encode("utf-8", "surrogatepass")).hexdigest()
    return f"@redacted-path/sha256:{digest[:20]}"


def _path_findings(path: str, deny_terms: Sequence[str]) -> list[Finding]:
    findings: list[Finding] = []
    normalized = _safe_relative_path(path)
    if normalized is None:
        return [_finding("UNSAFE_PATH", "@invalid-path", 1, path)]

    display_path = _safe_display_path(normalized, deny_terms)
    parts = tuple(_normal(part) for part in PurePosixPath(normalized).parts)
    name = parts[-1]
    forbidden = any(part in _FORBIDDEN_DIRS for part in parts[:-1])
    forbidden = forbidden or name in _FORBIDDEN_EXACT_FILES
    forbidden = forbidden or (name.startswith(".env.") and name != ".env.example")
    forbidden = forbidden or name.endswith(_FORBIDDEN_SUFFIXES)
    forbidden = forbidden or any(
        part.endswith(_FORBIDDEN_RUNTIME_NAME_ENDINGS) for part in parts
    )
    if forbidden:
        findings.append(_finding("FORBIDDEN_RUNTIME_PATH", display_path, 1, normalized))
    _scan_text(normalized, display_path, deny_terms, findings)
    return findings


def _read_path_no_follow(path: Path, expected_size: int) -> bytes:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise GateConfigurationError("NOT_REGULAR_FILE")
        if metadata.st_size != expected_size:
            raise GateConfigurationError("FILE_CHANGED_DURING_SCAN")
        chunks: list[bytes] = []
        remaining = expected_size + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) != expected_size:
            raise GateConfigurationError("FILE_CHANGED_DURING_SCAN")
        return data
    finally:
        os.close(descriptor)


def load_private_denylist(repo_root: Path | str, denylist_path: Path | str) -> tuple[str, ...]:
    root = Path(repo_root).absolute()
    supplied = Path(denylist_path).expanduser().absolute()
    try:
        root_resolved = root.resolve(strict=True)
        metadata = supplied.lstat()
        supplied_resolved = supplied.resolve(strict=True)
    except OSError as exc:
        raise GateConfigurationError("DENYLIST_UNREADABLE") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise GateConfigurationError("DENYLIST_NOT_REGULAR")
    if supplied_resolved == root_resolved or root_resolved in supplied_resolved.parents:
        raise GateConfigurationError("DENYLIST_MUST_BE_OUTSIDE_REPOSITORY")
    if os.name == "posix" and stat.S_IMODE(metadata.st_mode) != 0o600:
        raise GateConfigurationError("DENYLIST_MODE_MUST_BE_0600")
    if metadata.st_size > _MAX_DENYLIST_BYTES:
        raise GateConfigurationError("DENYLIST_SIZE_LIMIT")
    try:
        data = _read_path_no_follow(supplied, metadata.st_size)
        text = data.decode("utf-8-sig", "strict")
    except (OSError, UnicodeDecodeError, GateConfigurationError) as exc:
        raise GateConfigurationError("DENYLIST_UNREADABLE") from exc
    if "\0" in text:
        raise GateConfigurationError("DENYLIST_BINARY")

    terms: set[str] = set()
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        term = _normal(stripped)
        if len(term) < 4:
            raise GateConfigurationError("DENYLIST_TERM_TOO_SHORT")
        terms.add(term)
    if not terms:
        raise GateConfigurationError("DENYLIST_EMPTY")
    return tuple(sorted(terms))


def scan_tree(root: Path | str, deny_terms: Sequence[str] = ()) -> ScanReport:
    base = Path(root).absolute()
    findings: list[Finding] = []
    budget = _Budget()
    try:
        root_metadata = base.lstat()
    except OSError:
        return ScanReport("tree", (_finding("UNREADABLE_DIRECTORY", ".", 1, b"root"),), 0)
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        rule = "SYMLINK_FORBIDDEN" if stat.S_ISLNK(root_metadata.st_mode) else "NOT_A_DIRECTORY"
        return ScanReport("tree", (_finding(rule, ".", 1, b"root"),), 0)

    def visit(directory: Path, relative_directory: PurePosixPath) -> None:
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError:
            relative = relative_directory.as_posix() if relative_directory.parts else "."
            findings.append(
                _finding(
                    "UNREADABLE_DIRECTORY",
                    _safe_display_path(relative, deny_terms),
                    1,
                    relative,
                )
            )
            return

        for entry in entries:
            relative_path = relative_directory / entry.name
            relative = relative_path.as_posix()
            display_path = _safe_display_path(relative, deny_terms)
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError:
                findings.append(_finding("UNREADABLE_FILE", display_path, 1, relative))
                continue
            if stat.S_ISLNK(metadata.st_mode):
                findings.append(_finding("SYMLINK_FORBIDDEN", display_path, 1, relative))
                continue
            if stat.S_ISDIR(metadata.st_mode):
                normalized_name = _normal(entry.name)
                if normalized_name in _IGNORED_TREE_DIRS:
                    continue
                if normalized_name in _FORBIDDEN_DIRS:
                    findings.append(
                        _finding("FORBIDDEN_RUNTIME_PATH", display_path, 1, relative)
                    )
                    continue
                findings.extend(_path_findings(relative, deny_terms))
                visit(Path(entry.path), relative_path)
                continue
            findings.extend(_path_findings(relative, deny_terms))
            if not stat.S_ISREG(metadata.st_mode):
                findings.append(_finding("SPECIAL_FILE_FORBIDDEN", display_path, 1, relative))
                continue
            limit = budget.charge(metadata.st_size)
            if limit is not None:
                findings.append(_finding(limit, display_path, 1, str(metadata.st_size)))
                continue
            try:
                data = _read_path_no_follow(Path(entry.path), metadata.st_size)
            except (OSError, GateConfigurationError):
                findings.append(_finding("UNREADABLE_FILE", display_path, 1, relative))
                continue
            _decode_and_scan(data, display_path, deny_terms, findings, mode="tree")

    visit(base, PurePosixPath())
    return ScanReport("tree", _deduplicate(findings), budget.files)


def _git(root: Path, args: Sequence[str], *, input_data: bytes | None = None) -> bytes:
    try:
        completed = subprocess.run(
            ["git", "-C", os.fspath(root), *args],
            input=input_data,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            shell=False,
        )
    except (OSError, ValueError) as exc:
        raise GateConfigurationError("GIT_UNAVAILABLE") from exc
    if completed.returncode != 0:
        raise GateConfigurationError("GIT_COMMAND_FAILED")
    return completed.stdout


def _require_git_root(root: Path | str) -> Path:
    base = Path(root).absolute()
    try:
        expected = base.resolve(strict=True)
        root_text = _git(base, ("rev-parse", "--show-toplevel")).decode("utf-8", "strict").strip()
        reported = Path(root_text).resolve(strict=True)
    except (OSError, UnicodeDecodeError) as exc:
        raise GateConfigurationError("GIT_ROOT_UNREADABLE") from exc
    if reported != expected:
        raise GateConfigurationError("ROOT_MUST_BE_GIT_TOPLEVEL")
    return expected


def _decode_git_path(raw: bytes) -> str | None:
    try:
        value = raw.decode("utf-8", "strict")
    except UnicodeDecodeError:
        return None
    return _safe_relative_path(value)


def _git_blob(root: Path, oid: str, budget: _Budget) -> tuple[bytes | None, str | None]:
    try:
        size_text = _git(root, ("cat-file", "-s", oid)).decode("ascii", "strict").strip()
        size = int(size_text)
    except (GateConfigurationError, UnicodeDecodeError, ValueError):
        return None, "GIT_OBJECT_UNREADABLE"
    limit = budget.charge(size)
    if limit is not None:
        return None, limit
    try:
        data = _git(root, ("cat-file", "blob", oid))
    except GateConfigurationError:
        return None, "GIT_OBJECT_UNREADABLE"
    if len(data) != size:
        return None, "GIT_OBJECT_UNREADABLE"
    return data, None


def scan_staged(root: Path | str, deny_terms: Sequence[str] = ()) -> ScanReport:
    base = _require_git_root(root)
    findings: list[Finding] = []
    budget = _Budget()
    changed = set(
        part
        for part in _git(
            base,
            ("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z", "--"),
        ).split(b"\0")
        if part
    )
    index_records = _git(base, ("ls-files", "--stage", "-z", "--")).split(b"\0")
    for record in index_records:
        if not record or b"\t" not in record:
            continue
        header, raw_path = record.split(b"\t", 1)
        if raw_path not in changed:
            continue
        relative = _decode_git_path(raw_path)
        if relative is None:
            findings.append(_finding("UNSAFE_PATH", "@git-index", 1, raw_path))
            continue
        display_path = _safe_display_path(relative, deny_terms)
        findings.extend(_path_findings(relative, deny_terms))
        fields = header.split()
        if len(fields) != 3:
            findings.append(_finding("GIT_INDEX_UNREADABLE", display_path, 1, header))
            continue
        mode, raw_oid, stage_number = fields
        if stage_number != b"0":
            findings.append(_finding("GIT_INDEX_UNMERGED", display_path, 1, header))
            continue
        if mode == b"120000":
            findings.append(_finding("SYMLINK_FORBIDDEN", display_path, 1, raw_oid))
            continue
        if mode not in {b"100644", b"100755"}:
            findings.append(_finding("SPECIAL_FILE_FORBIDDEN", display_path, 1, mode))
            continue
        try:
            oid = raw_oid.decode("ascii", "strict")
        except UnicodeDecodeError:
            findings.append(_finding("GIT_OBJECT_UNREADABLE", display_path, 1, raw_oid))
            continue
        data, error = _git_blob(base, oid, budget)
        if error is not None:
            findings.append(_finding(error, display_path, 1, raw_oid))
            continue
        assert data is not None
        _decode_and_scan(data, display_path, deny_terms, findings, mode="staged")
    return ScanReport("staged", _deduplicate(findings), budget.files)


def _scan_git_metadata(
    root: Path,
    object_type: str,
    oid: str,
    path: str,
    deny_terms: Sequence[str],
    budget: _Budget,
    findings: list[Finding],
) -> None:
    try:
        size = int(_git(root, ("cat-file", "-s", oid)).decode("ascii", "strict").strip())
    except (GateConfigurationError, UnicodeDecodeError, ValueError):
        findings.append(_finding("GIT_OBJECT_UNREADABLE", path, 1, oid))
        return
    limit = budget.charge(size)
    if limit is not None:
        findings.append(_finding(limit, path, 1, oid))
        return
    try:
        data = _git(root, ("cat-file", object_type, oid))
    except GateConfigurationError:
        findings.append(_finding("GIT_OBJECT_UNREADABLE", path, 1, oid))
        return
    if len(data) != size:
        findings.append(_finding("GIT_OBJECT_UNREADABLE", path, 1, oid))
        return
    _decode_and_scan(data, path, deny_terms, findings, mode="history")


def scan_history(root: Path | str, deny_terms: Sequence[str] = ()) -> ScanReport:
    base = _require_git_root(root)
    findings: list[Finding] = []
    budget = _Budget()
    commits = [part for part in _git(base, ("rev-list", "--all")).splitlines() if part]
    seen_metadata: set[str] = set()
    for raw_oid in commits:
        try:
            oid = raw_oid.decode("ascii", "strict")
        except UnicodeDecodeError:
            findings.append(_finding("GIT_OBJECT_UNREADABLE", "history/commits", 1, raw_oid))
            continue
        seen_metadata.add(oid)
        _scan_git_metadata(
            base,
            "commit",
            oid,
            f"history/commits/{oid[:12]}.metadata",
            deny_terms,
            budget,
            findings,
        )

    tag_lines = _git(
        base,
        (
            "for-each-ref",
            "--format=%(objectname) %(objecttype) %(refname:short)",
            "refs/tags",
        ),
    ).splitlines()
    for tag_line in tag_lines:
        fields = tag_line.split(b" ", 2)
        if len(fields) != 3:
            findings.append(_finding("GIT_TAG_UNREADABLE", "history/tags", 1, tag_line))
            continue
        raw_oid, raw_type, raw_name = fields
        tag_name = _decode_git_path(raw_name)
        if tag_name is None:
            findings.append(_finding("UNSAFE_PATH", "history/tags", 1, raw_name))
        else:
            tag_path = f"history/tags/{tag_name}"
            _scan_text(
                tag_name,
                _safe_display_path(tag_path, deny_terms),
                deny_terms,
                findings,
            )
        if raw_type != b"tag":
            continue
        try:
            oid = raw_oid.decode("ascii", "strict")
        except UnicodeDecodeError:
            findings.append(_finding("GIT_TAG_UNREADABLE", "history/tags", 1, raw_oid))
            continue
        if oid in seen_metadata:
            continue
        seen_metadata.add(oid)
        _scan_git_metadata(
            base,
            "tag",
            oid,
            f"history/tags/{oid[:12]}.metadata",
            deny_terms,
            budget,
            findings,
        )

    historical_names = _git(
        base,
        ("log", "--all", "--format=", "--name-only", "-z", "--no-renames"),
    ).split(b"\0")
    for raw_name in historical_names:
        raw_name = raw_name.strip(b"\n")
        if not raw_name:
            continue
        relative = _decode_git_path(raw_name)
        if relative is None:
            findings.append(_finding("UNSAFE_PATH", "history/paths", 1, raw_name))
            continue
        findings.extend(_path_findings(relative, deny_terms))

    object_lines = _git(base, ("rev-list", "--objects", "--all")).splitlines()
    seen_blobs: set[str] = set()
    for object_line in object_lines:
        raw_oid, separator, raw_name = object_line.partition(b" ")
        try:
            oid = raw_oid.decode("ascii", "strict")
            object_type = _git(base, ("cat-file", "-t", oid)).decode("ascii", "strict").strip()
        except (GateConfigurationError, UnicodeDecodeError):
            findings.append(_finding("GIT_OBJECT_UNREADABLE", "history/objects", 1, raw_oid))
            continue
        if object_type != "blob" or oid in seen_blobs:
            if object_type == "tag" and oid not in seen_metadata:
                seen_metadata.add(oid)
                _scan_git_metadata(
                    base,
                    "tag",
                    oid,
                    f"history/tags/{oid[:12]}.metadata",
                    deny_terms,
                    budget,
                    findings,
                )
            continue
        seen_blobs.add(oid)
        relative = _decode_git_path(raw_name) if separator else None
        display_path = (
            f"history/blobs/{relative}"
            if relative is not None
            else f"history/blobs/object-{_fingerprint('GIT_OBJECT', oid).split(':', 1)[1]}"
        )
        safe_display_path = _safe_display_path(display_path, deny_terms)
        data, error = _git_blob(base, oid, budget)
        if error is not None:
            findings.append(_finding(error, safe_display_path, 1, oid))
            continue
        assert data is not None
        _decode_and_scan(
            data,
            safe_display_path,
            deny_terms,
            findings,
            mode="history",
        )
    return ScanReport("history", _deduplicate(findings), budget.files)


def _archive_member_path(name: str) -> str | None:
    if not name or "\0" in name or "\\" in name or re.match(r"(?i)^[a-z]:", name):
        return None
    candidate = PurePosixPath(name)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    parts = tuple(part for part in candidate.parts if part not in {"", "."})
    if not parts:
        return None
    return PurePosixPath(*parts).as_posix()


def _archive_member_display(path: str, deny_terms: Sequence[str]) -> str:
    return _safe_display_path(path, deny_terms)


def _scan_tar_members(
    bundle: tarfile.TarFile,
    deny_terms: Sequence[str],
    findings: list[Finding],
    budget: _Budget,
) -> None:
    seen_paths: set[str] = set()
    member_count = 0
    for member in bundle:
        member_count += 1
        if member_count > _MAX_FILES:
            findings.append(_finding("FILE_COUNT_LIMIT", "@release", 1, str(member_count)))
            break
        relative = _archive_member_path(member.name)
        if relative is None:
            findings.append(_finding("UNSAFE_ARCHIVE_PATH", "@release", 1, member.name))
            continue
        display_path = _archive_member_display(relative, deny_terms)
        if relative in seen_paths:
            findings.append(
                _finding("DUPLICATE_ARCHIVE_PATH", display_path, 1, relative)
            )
            continue
        seen_paths.add(relative)
        findings.extend(_path_findings(relative, deny_terms))
        if member.isdir() and _normal(PurePosixPath(relative).name) in _FORBIDDEN_DIRS:
            findings.append(
                _finding("FORBIDDEN_RUNTIME_PATH", display_path, 1, relative)
            )
        metadata_text = "\n".join(
            (
                member.uname or "",
                member.gname or "",
                *(f"{key}={value}" for key, value in sorted(member.pax_headers.items())),
            )
        )
        _scan_text(metadata_text, display_path, deny_terms, findings)
        if (
            member.uid != 0
            or member.gid != 0
            or (member.uname and member.uname != "root")
            or (member.gname and member.gname != "root")
        ):
            findings.append(
                _finding(
                    "ARCHIVE_OWNER_METADATA",
                    display_path,
                    1,
                    f"{member.uid}:{member.gid}:{member.uname}:{member.gname}",
                )
            )
        if member.issym() or member.islnk():
            findings.append(
                _finding("SYMLINK_FORBIDDEN", display_path, 1, member.linkname)
            )
            continue
        if member.isdir():
            continue
        if not member.isreg():
            findings.append(
                _finding("SPECIAL_FILE_FORBIDDEN", display_path, 1, member.type)
            )
            continue
        limit = budget.charge(member.size)
        if limit is not None:
            findings.append(_finding(limit, display_path, 1, str(member.size)))
            continue
        extracted = bundle.extractfile(member)
        if extracted is None:
            findings.append(
                _finding("ARCHIVE_MEMBER_UNREADABLE", display_path, 1, relative)
            )
            continue
        try:
            data = extracted.read(member.size + 1)
        except (OSError, tarfile.TarError):
            findings.append(
                _finding("ARCHIVE_MEMBER_UNREADABLE", display_path, 1, relative)
            )
            continue
        if len(data) != member.size:
            findings.append(
                _finding("ARCHIVE_MEMBER_UNREADABLE", display_path, 1, relative)
            )
            continue
        _decode_and_scan(data, display_path, deny_terms, findings, mode="release")


def _zip_member_is_symlink(member: zipfile.ZipInfo) -> bool:
    if member.create_system != 3:
        return False
    return stat.S_ISLNK((member.external_attr >> 16) & 0xFFFF)


def _scan_zip_members(
    bundle: zipfile.ZipFile,
    deny_terms: Sequence[str],
    findings: list[Finding],
    budget: _Budget,
) -> None:
    if bundle.comment:
        _decode_and_scan(
            bundle.comment,
            "@release/archive-comment",
            deny_terms,
            findings,
            mode="release",
        )
    seen_paths: set[str] = set()
    for member_count, member in enumerate(bundle.infolist(), start=1):
        if member_count > _MAX_FILES:
            findings.append(_finding("FILE_COUNT_LIMIT", "@release", 1, str(member_count)))
            break
        relative = _archive_member_path(member.filename)
        if relative is None:
            findings.append(
                _finding("UNSAFE_ARCHIVE_PATH", "@release", 1, member.filename)
            )
            continue
        display_path = _archive_member_display(relative, deny_terms)
        if relative in seen_paths:
            findings.append(
                _finding("DUPLICATE_ARCHIVE_PATH", display_path, 1, relative)
            )
            continue
        seen_paths.add(relative)
        findings.extend(_path_findings(relative, deny_terms))
        if member.comment:
            _decode_and_scan(
                member.comment,
                display_path,
                deny_terms,
                findings,
                mode="release",
            )
        if member.is_dir() and _normal(PurePosixPath(relative).name) in _FORBIDDEN_DIRS:
            findings.append(
                _finding("FORBIDDEN_RUNTIME_PATH", display_path, 1, relative)
            )
        if _zip_member_is_symlink(member):
            findings.append(
                _finding(
                    "SYMLINK_FORBIDDEN",
                    display_path,
                    1,
                    str(member.external_attr),
                )
            )
            continue
        if member.is_dir():
            continue
        if member.flag_bits & 0x1:
            findings.append(
                _finding("ARCHIVE_MEMBER_UNREADABLE", display_path, 1, b"encrypted")
            )
            continue
        limit = budget.charge(member.file_size)
        if limit is not None:
            findings.append(_finding(limit, display_path, 1, str(member.file_size)))
            continue
        try:
            with bundle.open(member, "r") as extracted:
                data = extracted.read(member.file_size + 1)
        except (OSError, RuntimeError, zipfile.BadZipFile):
            findings.append(
                _finding("ARCHIVE_MEMBER_UNREADABLE", display_path, 1, relative)
            )
            continue
        if len(data) != member.file_size:
            findings.append(
                _finding("ARCHIVE_MEMBER_UNREADABLE", display_path, 1, relative)
            )
            continue
        _decode_and_scan(data, display_path, deny_terms, findings, mode="release")


def scan_release_archive(
    archive: Path | str,
    deny_terms: Sequence[str] = (),
) -> ScanReport:
    archive_path = Path(archive).expanduser().absolute()
    findings: list[Finding] = []
    budget = _Budget()
    try:
        metadata = archive_path.lstat()
    except OSError:
        return ScanReport("release", (_finding("ARCHIVE_UNREADABLE", "@release", 1, b"missing"),), 0)
    if stat.S_ISLNK(metadata.st_mode):
        return ScanReport("release", (_finding("SYMLINK_FORBIDDEN", "@release", 1, b"archive"),), 0)
    if not stat.S_ISREG(metadata.st_mode):
        return ScanReport("release", (_finding("ARCHIVE_UNREADABLE", "@release", 1, b"type"),), 0)
    if metadata.st_size > _MAX_TOTAL_BYTES:
        return ScanReport(
            "release",
            (_finding("ARCHIVE_SIZE_LIMIT", "@release", 1, str(metadata.st_size)),),
            0,
        )

    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(archive_path, flags)
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = None
            file_metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(file_metadata.st_mode) or file_metadata.st_size != metadata.st_size:
                raise GateConfigurationError("FILE_CHANGED_DURING_SCAN")
            is_zip = zipfile.is_zipfile(handle)
            handle.seek(0)
            if is_zip:
                with zipfile.ZipFile(handle, mode="r") as bundle:
                    _scan_zip_members(bundle, deny_terms, findings, budget)
            else:
                with tarfile.open(fileobj=handle, mode="r:*") as bundle:
                    _scan_tar_members(bundle, deny_terms, findings, budget)
    except (OSError, tarfile.TarError, zipfile.BadZipFile, EOFError, GateConfigurationError):
        findings.append(_finding("ARCHIVE_UNREADABLE", "@release", 1, b"invalid"))
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return ScanReport("release", _deduplicate(findings), budget.files)


def render_finding(finding: Finding) -> str:
    return json.dumps(
        {
            "fingerprint": finding.fingerprint,
            "line": finding.line,
            "path": finding.path,
            "rule_id": finding.rule_id,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _configuration_report(mode: str, code: str) -> ScanReport:
    return ScanReport(mode, (_finding(code, "@configuration", 1, code),), 0)


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scan a repository without printing matched content.")
    parser.add_argument("root", nargs="?", default=".", help="repository root")
    parser.add_argument(
        "--mode",
        action="append",
        choices=("tree", "staged", "history", "release", "all"),
        help="scan mode; repeat to combine (default: tree)",
    )
    parser.add_argument(
        "--archive",
        action="append",
        help="tar, wheel, or ZIP archive for release mode; repeatable",
    )
    parser.add_argument("--denylist", help="private 0600 denylist outside the repository")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    options = _parse_args(tuple(argv) if argv is not None else tuple(sys.argv[1:]))
    root = Path(options.root).absolute()
    modes = list(options.mode or ["tree"])
    if "all" in modes:
        modes = ["tree", "staged", "history"] + (["release"] if options.archive else [])
    modes = list(dict.fromkeys(modes))

    try:
        deny_terms = load_private_denylist(root, options.denylist) if options.denylist else ()
    except GateConfigurationError as exc:
        reports = [_configuration_report("configuration", exc.code)]
    except Exception:
        reports = [_configuration_report("configuration", "INTERNAL_SCAN_ERROR")]
    else:
        reports: list[ScanReport] = []
        for mode in modes:
            try:
                if mode == "tree":
                    reports.append(scan_tree(root, deny_terms))
                elif mode == "staged":
                    reports.append(scan_staged(root, deny_terms))
                elif mode == "history":
                    reports.append(scan_history(root, deny_terms))
                elif mode == "release":
                    if not options.archive:
                        reports.append(_configuration_report("release", "ARCHIVE_REQUIRED"))
                    else:
                        reports.extend(
                            scan_release_archive(archive, deny_terms)
                            for archive in options.archive
                        )
            except GateConfigurationError as exc:
                reports.append(_configuration_report(mode, exc.code))
            except Exception:
                reports.append(_configuration_report(mode, "INTERNAL_SCAN_ERROR"))

    findings = _deduplicate(item for report in reports for item in report.findings)
    if findings:
        for finding in findings:
            print(render_finding(finding), file=sys.stderr)
        print(
            f"privacy-gate: FAIL findings={len(findings)} "
            f"modes={','.join(report.mode for report in reports)}",
            file=sys.stderr,
        )
        return 1
    print(
        f"privacy-gate: PASS files={sum(report.scanned_files for report in reports)} "
        f"modes={','.join(report.mode for report in reports)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
